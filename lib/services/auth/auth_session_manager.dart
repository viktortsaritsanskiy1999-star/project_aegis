import 'dart:async';
import 'dart:convert';
import 'package:flutter/foundation.dart';
import '../api/aegis_api_client.dart';
import '../api/vitals_api_models.dart';
import 'auth_models.dart';
import 'auth_token_storage.dart';

/// High-level lifecycle state of the mobile device authentication session.
enum AuthSessionStatus {
  /// No paired device session exists locally, or the session was explicitly revoked.
  unauthenticated,

  /// A valid paired device session is active.
  authenticated,

  /// The session has expired, refresh was rejected (401/403), or refresh failed with
  /// an indeterminate outcome after dispatch (suspending automatic refresh until re-pairing).
  expired,

  /// Authentication or token refresh is temporarily rate-limited by the server (HTTP 429).
  rateLimited,
}

/// Manages the mobile device's authenticated session lifecycle, including:
/// - Loading and persisting [AuthSession] via [IAuthTokenStorage] (`SecureAuthTokenStorage` in production);
/// - Persistent pre-dispatch protective guard against single-use refresh token replay across app restarts;
/// - Pairing via `POST /api/v1/auth/pair`;
/// - Pre-emptive access token validation and Single-Flight refresh via `POST /api/v1/auth/refresh`;
/// - Protection against Single-Use Refresh Token Reuse Detection on indeterminate network failures;
/// - Confirmed self-revocation via `POST /api/v1/auth/revoke`.
class AuthSessionManager extends ChangeNotifier {
  static final Set<IAuthTokenStorage> _activeStorages =
      Set<IAuthTokenStorage>.identity();
  static final Map<String, AuthSessionManager> _activeDeviceManagers =
      <String, AuthSessionManager>{};
  static bool _hasActiveSecureStorageManager = false;

  /// Resets static single-instance guards for isolated test teardowns.
  @visibleForTesting
  static void resetActiveInstancesForTesting() {
    _activeStorages.clear();
    _activeDeviceManagers.clear();
    _hasActiveSecureStorageManager = false;
  }

  final IAuthTokenStorage _storage;
  final AegisApiClient _apiClient;
  final Duration refreshSkewBuffer;
  final DateTime Function() _nowProvider;
  final Future<bool> Function()? _connectivityCheck;

  AuthSession? _session;
  String? _lastKnownPatientId;
  AuthSessionStatus _status = AuthSessionStatus.unauthenticated;
  bool _isRefreshIndeterminate = false;
  DateTime? _rateLimitedUntil;
  Duration? _lastRetryAfter;
  AegisApiException? _lastError;
  SecureStorageUnavailableException? _lastStorageError;
  bool _isDisposed = false;
  bool _isInitialized = false;
  Future<AuthSession?>? _initFuture;
  bool _boundTokenProviderToClient = false;
  Future<String?> Function()? _boundTokenProvider;

  /// Single-flight lock ensuring concurrent callers share a single `POST /api/v1/auth/refresh` call.
  Completer<String>? _inFlightRefreshCompleter;

  /// Tracks all refresh tokens that have already been dispatched to `POST /api/v1/auth/refresh`
  /// or consumed during rotation, strictly preventing client-side reuse of any old refresh token.
  final Set<String> _dispatchedRefreshTokens = <String>{};

  AuthSessionManager({
    required AegisApiClient apiClient,
    IAuthTokenStorage? storage,
    this.refreshSkewBuffer = const Duration(seconds: 60),
    DateTime Function()? nowProvider,
    Future<bool> Function()? connectivityCheck,
  })  : _apiClient = apiClient,
        _storage = storage ?? SecureAuthTokenStorage(),
        _nowProvider = nowProvider ?? (() => DateTime.now().toUtc()),
        _connectivityCheck = connectivityCheck {
    if (_activeStorages.contains(_storage) ||
        (_storage is SecureAuthTokenStorage && _hasActiveSecureStorageManager)) {
      throw StateError(
        'Parallel independent AuthSessionManager instances for the same active token storage are prohibited.',
      );
    }
    _activeStorages.add(_storage);
    if (_storage is SecureAuthTokenStorage) {
      _hasActiveSecureStorageManager = true;
    }
    if (_apiClient.tokenProvider == null) {
      _boundTokenProvider = getValidAccessToken;
      _apiClient.bindTokenProvider(_boundTokenProvider);
      _boundTokenProviderToClient = true;
    }
    _apiClient.attachSessionManager(this);
  }

  /// Underlying HTTP client bound to this session manager.
  AegisApiClient get apiClient => _apiClient;

  /// Whether [initialize] or [loadSession] or [pairDevice] has completed at least once.
  bool get isInitialized => _isInitialized;

  /// Current session state.
  AuthSessionStatus get status => _status;

  /// Current loaded or paired session metadata, if available.
  AuthSession? get currentSession => _session;

  /// Patient ID of the active session, or the last known bound patient ID prior to expiry/revocation.
  String? get lastKnownPatientId => _session?.patientId ?? _lastKnownPatientId;

  /// Whether the manager currently holds an usable authenticated session.
  bool get isAuthenticated =>
      _status == AuthSessionStatus.authenticated &&
      _session != null &&
      !_isRefreshIndeterminate;

  /// True when a `POST /api/v1/auth/refresh` request was dispatched over the network
  /// and terminated with an unknown/indeterminate outcome (timeout, connection drop, 5xx, or save failure).
  /// When true, automatic refresh with the old token is strictly suspended until re-pairing.
  bool get isRefreshIndeterminate => _isRefreshIndeterminate;

  /// UTC timestamp until which rate limiting (`429 Too Many Requests`) remains in effect.
  DateTime? get rateLimitedUntil => _rateLimitedUntil;

  /// Last `Retry-After` duration received from a 429 response.
  Duration? get lastRetryAfter => _lastRetryAfter;

  /// Most recent API exception encountered by the session manager.
  AegisApiException? get lastError => _lastError;

  /// Most recent secure storage exception encountered by the session manager.
  SecureStorageUnavailableException? get lastStorageError => _lastStorageError;

  /// Remaining cooldown duration if currently rate-limited, or null otherwise.
  Duration? get rateLimitRemaining {
    if (_rateLimitedUntil == null) return null;
    final now = _nowProvider().toUtc();
    if (!now.isBefore(_rateLimitedUntil!)) return null;
    return _rateLimitedUntil!.difference(now);
  }

  /// Validates that [token] is a structurally well-formed mobile JWT (`header.payload.signature`)
  /// and strictly rejects static secrets (such as `AEGIS_API_SECRET`) or refresh tokens.
  static bool isValidMobileAccessTokenJwt(String token) {
    final trimmed = token.trim();
    if (trimmed.isEmpty) return false;
    final parts = trimmed.split('.');
    if (parts.length != 3) return false;

    final base64UrlRegex = RegExp(r'^[A-Za-z0-9_-]+$');
    for (final part in parts) {
      if (part.isEmpty || !base64UrlRegex.hasMatch(part)) {
        return false;
      }
    }

    // JWT JSON header in base64url must start with "eyJ" (`{"`)
    if (!parts[0].startsWith('eyJ')) {
      return false;
    }

    try {
      final headerBytes = base64Url.decode(base64Url.normalize(parts[0]));
      final headerMap = json.decode(utf8.decode(headerBytes));
      if (headerMap is! Map<String, dynamic>) return false;
    } catch (_) {
      return false;
    }

    try {
      final payloadBytes = base64Url.decode(base64Url.normalize(parts[1]));
      final payloadMap = json.decode(utf8.decode(payloadBytes));
      if (payloadMap is! Map<String, dynamic>) return false;
      if (payloadMap.containsKey('type') && payloadMap['type'] != 'access') {
        return false;
      }
    } catch (_) {
      return false;
    }

    return true;
  }

  /// Extracts the `sub` claim from a valid mobile access token JWT,
  /// or returns `null` if [token] is not a valid mobile JWT or lacks a non-empty `sub` claim.
  /// If both `sub` and `patient_id` claims are present, they must be consistent.
  static String? extractPatientIdFromJwt(String? token) {
    if (token == null || !isValidMobileAccessTokenJwt(token)) return null;
    try {
      final parts = token.trim().split('.');
      final payloadBytes = base64Url.decode(base64Url.normalize(parts[1]));
      final payloadMap = json.decode(utf8.decode(payloadBytes));
      if (payloadMap is Map<String, dynamic>) {
        final sub = payloadMap['sub'];
        if (sub is! String || sub.trim().isEmpty) {
          return null;
        }
        final cleanSub = sub.trim();
        final patientId = payloadMap['patient_id'];
        if (patientId is String && patientId.trim().isNotEmpty) {
          if (patientId.trim() != cleanSub) {
            return null; // Inconsistent claims
          }
        }
        return cleanSub;
      }
    } catch (_) {}
    return null;
  }

  void _registerActiveDevice(String deviceId) {
    final existing = _activeDeviceManagers[deviceId];
    if (existing != null && !identical(existing, this)) {
      throw StateError(
        'Parallel independent AuthSessionManager instances for the same active device session ($deviceId) are prohibited.',
      );
    }
    _activeDeviceManagers[deviceId] = this;
  }

  void _unregisterActiveDevice(String? deviceId) {
    if (deviceId == null) return;
    if (identical(_activeDeviceManagers[deviceId], this)) {
      _activeDeviceManagers.remove(deviceId);
    }
  }

  void _log(String message) {
    final sanitized = AegisApiClient.sanitizeLogMessage(message);
    debugPrint('[AuthSessionManager] $sanitized');
  }

  /// Fails closed if secure storage cleanup fails, ensuring in-memory session is wiped
  /// and logging only safe, non-leaking messages without raw platform stack traces.
  Future<void> _clearStorageFailClosed() async {
    try {
      await _storage.clearSession();

      // Invariant: Verify that stored session is genuinely removed
      // BEFORE deleting the protective in-flight guard.
      AuthSession? remainingSession;
      try {
        remainingSession = await _storage.loadSession();
      } catch (e) {
        // Verification read failed: storage state is indeterminate.
        // DO NOT delete guard! Keep fail-closed.
        _isRefreshIndeterminate = true;
        _lastStorageError = e is SecureStorageUnavailableException
            ? e
            : const SecureStorageUnavailableException(
                'Failed to verify session deletion in secure storage.',
              );
        return;
      }

      if (remainingSession != null) {
        // clearSession() failed to reliably wipe the session.
        // The old token is still on disk; preserve the guard!
        _isRefreshIndeterminate = true;
        _lastStorageError = const SecureStorageUnavailableException(
          'Failed to reliably clear auth session from storage.',
        );
        return;
      }

      await _storage.clearRefreshInFlightRecord();
    } on SecureStorageUnavailableException catch (e) {
      _log('Secure storage cleanup error: ${e.message}');
      _lastStorageError = e;
      _isRefreshIndeterminate = true;
    } catch (_) {
      _log('Secure storage cleanup error: unexpected failure.');
      _lastStorageError = const SecureStorageUnavailableException(
        'Failed to securely clear storage record.',
      );
      _isRefreshIndeterminate = true;
    }
  }

  void _applyRateLimit(Duration? retryAfter) {
    final effectiveDuration = retryAfter ?? const Duration(seconds: 60);
    _lastRetryAfter = effectiveDuration;
    _rateLimitedUntil = _nowProvider().toUtc().add(effectiveDuration);
    _status = AuthSessionStatus.rateLimited;
  }

  /// Loads any persisted [AuthSession] from [IAuthTokenStorage] on startup.
  /// Concurrent calls while initialization is in flight share the same [Future].
  Future<AuthSession?> initialize() {
    if (_initFuture != null) return _initFuture!;
    final future = loadSession();
    _initFuture = future;
    future.whenComplete(() {
      if (identical(_initFuture, future)) {
        _initFuture = null;
      }
    }).ignore();
    return future;
  }

  /// Loads and validates the stored [AuthSession] from [IAuthTokenStorage].
  /// Detects and blocks any refresh token that was left in-flight from a prior process run.
  Future<AuthSession?> loadSession() async {
    _ensureNotDisposed();
    try {
      return await _loadSessionInternal();
    } finally {
      _isInitialized = true;
    }
  }

  Future<AuthSession?> _loadSessionInternal() async {
    // 1. Check for persistent in-flight refresh record from prior run
    RefreshInFlightRecord? inFlightRecord;
    try {
      inFlightRecord = await _storage.loadRefreshInFlightRecord();
    } on SecureStorageUnavailableException catch (e) {
      _log('Failed to read in-flight refresh record: ${e.message}');
      _lastStorageError = e;
      _unregisterActiveDevice(_session?.deviceId);
      _session = null;
      _isRefreshIndeterminate = true;
      _status = AuthSessionStatus.expired;
      notifyListeners();
      // Fail closed: do NOT delete records, do NOT transition to authenticated
      return null;
    } catch (_) {
      const safeErr = SecureStorageUnavailableException(
        'Failed to read in-flight refresh record from secure storage.',
      );
      _log('Failed to read in-flight refresh record: unexpected error.');
      _lastStorageError = safeErr;
      _unregisterActiveDevice(_session?.deviceId);
      _session = null;
      _isRefreshIndeterminate = true;
      _status = AuthSessionStatus.expired;
      notifyListeners();
      return null;
    }

    AuthSession? stored;
    try {
      stored = await _storage.loadSession();
    } on SecureStorageUnavailableException catch (e) {
      _log('Failed to read auth session from storage: ${e.message}');
      _lastStorageError = e;
      _unregisterActiveDevice(_session?.deviceId);
      _session = null;
      _isRefreshIndeterminate = true;
      _status = AuthSessionStatus.expired;
      notifyListeners();
      return null;
    } catch (_) {
      const safeErr = SecureStorageUnavailableException(
        'Failed to read auth session from secure storage.',
      );
      _log('Failed to read auth session: unexpected error.');
      _lastStorageError = safeErr;
      _unregisterActiveDevice(_session?.deviceId);
      _session = null;
      _isRefreshIndeterminate = true;
      _status = AuthSessionStatus.expired;
      notifyListeners();
      return null;
    }
    if (stored == null) {
      if (inFlightRecord != null) {
        try {
          await _storage.clearRefreshInFlightRecord();
        } on SecureStorageUnavailableException catch (e) {
          _log('Notice: Failed to clear orphaned in-flight record: ${e.message}');
          _lastStorageError = e;
        } catch (_) {}
      }
      _unregisterActiveDevice(_session?.deviceId);
      _session = null;
      _status = AuthSessionStatus.unauthenticated;
      _isRefreshIndeterminate = false;
      notifyListeners();
      return null;
    }

    if (!isValidMobileAccessTokenJwt(stored.accessToken)) {
      _log('Rejected non-JWT access token found in token storage.');
      await _clearStorageFailClosed();
      _unregisterActiveDevice(_session?.deviceId);
      _session = null;
      _status = AuthSessionStatus.unauthenticated;
      notifyListeners();
      throw const AegisAuthException(
        'Stored access token is not a valid mobile JWT (static secrets are prohibited).',
        statusCode: 401,
      );
    }

    _registerActiveDevice(stored.deviceId);
    _session = stored;
    _lastKnownPatientId = stored.patientId;

    final now = _nowProvider().toUtc();

    // 2. Persistent Single-Use Token Reuse Guard:
    // If an in-flight record matches stored.refreshToken, this token was dispatched
    // in a prior run and never safely completed. Re-sending it is strictly prohibited.
    if (inFlightRecord != null &&
        inFlightRecord.dispatchedRefreshToken == stored.refreshToken) {
      _dispatchedRefreshTokens.add(stored.refreshToken);
      _isRefreshIndeterminate = true;
      _status = AuthSessionStatus.expired;
      _log('Detected uncommitted in-flight refresh token upon restart. Session marked expired.');
      notifyListeners();
      return stored;
    }

    // Clean up any stale in-flight record from an older token
    if (inFlightRecord != null) {
      _dispatchedRefreshTokens.add(inFlightRecord.dispatchedRefreshToken);
      try {
        await _storage.clearRefreshInFlightRecord();
      } on SecureStorageUnavailableException catch (e) {
        _log('Notice: Failed to clear stale in-flight record upon load: ${e.message}');
        _lastStorageError = e;
      } catch (_) {}
    }

    if (_dispatchedRefreshTokens.contains(stored.refreshToken) ||
        stored.isRefreshTokenExpired(now: now)) {
      _status = AuthSessionStatus.expired;
    } else {
      _status = AuthSessionStatus.authenticated;
      _isRefreshIndeterminate = false;
    }

    notifyListeners();
    return stored;
  }

  /// Atomically pairs the device using [request] (`POST /api/v1/auth/pair`)
  /// and persists the resulting [AuthSession] in [IAuthTokenStorage].
  Future<AuthSession> pairDevice(DevicePairRequest request) async {
    _ensureNotDisposed();

    final now = _nowProvider().toUtc();
    if (_status == AuthSessionStatus.rateLimited &&
        _rateLimitedUntil != null &&
        now.isBefore(_rateLimitedUntil!)) {
      final remaining = _rateLimitedUntil!.difference(now);
      final rateErr = AegisRateLimitException(
        'Pairing is rate-limited. Please retry later.',
        statusCode: 429,
        retryAfter: remaining,
      );
      _lastError = rateErr;
      throw rateErr;
    }

    try {
      final response = await _apiClient.pairDevice(request);

      if (!isValidMobileAccessTokenJwt(response.accessToken)) {
        const err = AegisAuthException(
          'Server returned an invalid mobile access token (JWT required).',
          statusCode: 401,
        );
        _lastError = err;
        throw err;
      }

      final newSession = AuthSession.fromPairResponse(response);
      _registerActiveDevice(newSession.deviceId);

      await _storage.saveSession(newSession);

      // Verify that new session is reliably committed before clearing old guard
      AuthSession? verifiedPairSession;
      try {
        verifiedPairSession = await _storage.loadSession();
      } on SecureStorageUnavailableException catch (e) {
        _log('Failed to verify persisted session after pair: ${e.message}');
        _isRefreshIndeterminate = true;
        _status = AuthSessionStatus.expired;
        _session = null;
        _lastStorageError = e;
        notifyListeners();
        rethrow;
      } catch (_) {
        _log('Failed to verify persisted session after pair: unexpected error.');
        _isRefreshIndeterminate = true;
        _status = AuthSessionStatus.expired;
        _session = null;
        const verifyErr = SecureStorageUnavailableException(
          'Failed to verify persisted paired session in secure storage.',
        );
        _lastStorageError = verifyErr;
        notifyListeners();
        throw verifyErr;
      }

      if (verifiedPairSession == null ||
          verifiedPairSession.refreshToken != newSession.refreshToken) {
        _isRefreshIndeterminate = true;
        _status = AuthSessionStatus.expired;
        _session = null;
        const verifyErr = SecureStorageUnavailableException(
          'Persisted session validation failed after pairing.',
        );
        _lastStorageError = verifyErr;
        notifyListeners();
        throw verifyErr;
      }

      try {
        await _storage.clearRefreshInFlightRecord();
      } on SecureStorageUnavailableException catch (e) {
        _log('Notice: Failed to clear old in-flight record after pairing: ${e.message}');
        _lastStorageError = e;
      } catch (_) {}

      if (_session != null && _session!.deviceId != newSession.deviceId) {
        _unregisterActiveDevice(_session!.deviceId);
      }

      _session = newSession;
      _lastKnownPatientId = newSession.patientId;
      _isInitialized = true;
      _status = AuthSessionStatus.authenticated;
      _isRefreshIndeterminate = false;
      _rateLimitedUntil = null;
      _lastRetryAfter = null;
      _lastError = null;
      _lastStorageError = null;
      _log('Device paired successfully (deviceId: ${newSession.deviceId}, patientId: ${newSession.patientId})');
      notifyListeners();
      return newSession;
    } on AegisRateLimitException catch (e) {
      _lastError = e;
      _applyRateLimit(e.retryAfter);
      notifyListeners();
      rethrow;
    } on AegisApiException catch (e) {
      _lastError = e;
      notifyListeners();
      rethrow;
    }
  }

  /// Resolves a valid JWT access token for [AegisApiClient] requests.
  /// Automatically performs a pre-emptive Single-Flight refresh when the current access token
  /// is expired or within [refreshSkewBuffer] of expiration.
  /// Returns `null` if unauthenticated, expired, rate-limited, or if refresh fails.
  Future<String?> getValidAccessToken() async {
    _ensureNotDisposed();

    if (_session == null || _status == AuthSessionStatus.unauthenticated) {
      return null;
    }

    if (_isRefreshIndeterminate || _status == AuthSessionStatus.expired) {
      return null;
    }

    if (!isValidMobileAccessTokenJwt(_session!.accessToken)) {
      _status = AuthSessionStatus.expired;
      notifyListeners();
      return null;
    }

    final now = _nowProvider().toUtc();

    if (_status == AuthSessionStatus.rateLimited) {
      if (_rateLimitedUntil != null && now.isBefore(_rateLimitedUntil!)) {
        if (!_session!.isAccessTokenExpired(skewBuffer: refreshSkewBuffer, now: now)) {
          return _session!.accessToken;
        }
        return null;
      } else {
        _status = AuthSessionStatus.authenticated;
        _rateLimitedUntil = null;
      }
    }

    // If a refresh is already in flight, join the single-flight Future
    if (_inFlightRefreshCompleter != null) {
      try {
        return await _inFlightRefreshCompleter!.future;
      } on AegisApiException {
        return null;
      } on SecureStorageUnavailableException {
        return null;
      }
    }

    // Fast path: access token is still valid outside the pre-emptive skew window
    if (!_session!.isAccessTokenExpired(skewBuffer: refreshSkewBuffer, now: now)) {
      return _session!.accessToken;
    }

    // Access token is expired or expiring soon; verify refresh token has not expired
    if (_session!.isRefreshTokenExpired(now: now)) {
      _status = AuthSessionStatus.expired;
      notifyListeners();
      return null;
    }

    try {
      return await refreshSession();
    } on AegisApiException {
      return null;
    } on SecureStorageUnavailableException {
      return null;
    }
  }

  /// Executes or joins a Single-Flight token refresh (`POST /api/v1/auth/refresh`).
  ///
  /// Concurrency guarantee:
  /// If 5 callers invoke [getValidAccessToken] or [refreshSession] simultaneously,
  /// all 5 await the same [_inFlightRefreshCompleter] and the server receives
  /// strictly 1 `POST /api/v1/auth/refresh` request.
  ///
  /// Persistent protective guarantee:
  /// The in-flight dispatch state is persisted in secure storage BEFORE sending HTTP /refresh.
  /// If writing this protective state fails, the HTTP request is aborted without dispatch.
  /// If the request fails or saving the rotated session fails, the session transitions to
  /// [AuthSessionStatus.expired] requiring re-pairing, and the old token is never re-sent.
  Future<String> refreshSession() async {
    _ensureNotDisposed();

    if (_inFlightRefreshCompleter != null) {
      return _inFlightRefreshCompleter!.future;
    }

    final completer = Completer<String>();
    _inFlightRefreshCompleter = completer;

    try {
      final newAccessToken = await _performSingleFlightRefresh();
      completer.complete(newAccessToken);
      return newAccessToken;
    } catch (e, st) {
      completer.future.ignore();
      completer.completeError(e, st);
      rethrow;
    } finally {
      _inFlightRefreshCompleter = null;
    }
  }

  Future<String> _performSingleFlightRefresh() async {
    if (_session == null || _status == AuthSessionStatus.unauthenticated) {
      const err = AegisAuthException(
        'Cannot refresh tokens: session is unauthenticated.',
        statusCode: 401,
      );
      _lastError = err;
      throw err;
    }

    if (_isRefreshIndeterminate) {
      const err = AegisAuthException(
        'Token refresh is suspended after a previous indeterminate refresh outcome. Please re-pair device.',
        statusCode: 401,
      );
      _lastError = err;
      throw err;
    }

    if (_status == AuthSessionStatus.expired) {
      const err = AegisAuthException(
        'Cannot refresh tokens: session has expired.',
        statusCode: 401,
      );
      _lastError = err;
      throw err;
    }

    final now = _nowProvider().toUtc();
    if (_status == AuthSessionStatus.rateLimited &&
        _rateLimitedUntil != null &&
        now.isBefore(_rateLimitedUntil!)) {
      final remaining = _rateLimitedUntil!.difference(now);
      final rateErr = AegisRateLimitException(
        'Token refresh is rate-limited. Please retry later.',
        statusCode: 429,
        retryAfter: remaining,
      );
      _lastError = rateErr;
      throw rateErr;
    }

    final previousSession = _session!;
    final oldRefreshToken = previousSession.refreshToken;

    if (_dispatchedRefreshTokens.contains(oldRefreshToken)) {
      _status = AuthSessionStatus.expired;
      notifyListeners();
      const err = AegisAuthException(
        'Refresh token has already been used or dispatched. Reuse is strictly prohibited.',
        statusCode: 401,
      );
      _lastError = err;
      throw err;
    }

    if (previousSession.isRefreshTokenExpired(now: now)) {
      _status = AuthSessionStatus.expired;
      notifyListeners();
      const err = AegisAuthException(
        'Refresh token has expired.',
        statusCode: 401,
      );
      _lastError = err;
      throw err;
    }

    // Pre-dispatch connectivity check: if offline BEFORE sending the request,
    // abort without dispatching and without touching storage or burning the refresh token.
    if (_connectivityCheck != null) {
      bool isOnline = false;
      try {
        isOnline = await _connectivityCheck();
      } catch (_) {
        isOnline = false;
      }
      if (!isOnline) {
        const netErr = AegisNetworkException(
          'Network unavailable before dispatching refresh request. Session preserved in secure storage.',
        );
        _lastError = netErr;
        notifyListeners();
        throw netErr;
      }
    }

    // MANDATORY PERSISTENT PRE-DISPATCH PROTECTION:
    // Persistently commit in-flight state to secure storage BEFORE sending HTTP /refresh.
    // If saving this protective state fails, we MUST NOT send the refresh request!
    try {
      await _storage.markRefreshInFlight(
        deviceId: previousSession.deviceId,
        dispatchedRefreshToken: oldRefreshToken,
      );
    } on SecureStorageUnavailableException catch (e) {
      _log('Failed to persist in-flight refresh record: ${e.message}');
      _lastStorageError = e;
      notifyListeners();
      throw SecureStorageUnavailableException(
        'Cannot dispatch token refresh: failed to persist in-flight protective state.',
        e,
      );
    } catch (_) {
      _log('Unexpected error persisting in-flight refresh record.');
      const safeErr = SecureStorageUnavailableException(
        'Cannot dispatch token refresh: failed to persist in-flight protective state.',
      );
      _lastStorageError = safeErr;
      notifyListeners();
      throw safeErr;
    }

    // Mark single-use refresh token as dispatched in-memory immediately before HTTP call.
    _dispatchedRefreshTokens.add(oldRefreshToken);

    TokenRefreshResponse response;
    try {
      response = await _apiClient.refreshTokens(
        TokenRefreshRequest(
          deviceId: previousSession.deviceId,
          refreshToken: oldRefreshToken,
        ),
      );
    } on AegisAuthException catch (e) {
      // 401 on refresh: session/refresh_token is definitively invalid or revoked
      _lastError = e;
      _isRefreshIndeterminate = false;
      _status = AuthSessionStatus.expired;
      _session = null;
      await _clearStorageFailClosed();
      notifyListeners();
      rethrow;
    } on AegisForbiddenException catch (e) {
      // 403 on refresh: insufficient permissions / forbidden, do not retry
      _lastError = e;
      _isRefreshIndeterminate = false;
      _status = AuthSessionStatus.expired;
      _session = null;
      await _clearStorageFailClosed();
      notifyListeners();
      rethrow;
    } on AegisRateLimitException catch (e) {
      // 429 on refresh: record Retry-After; old token was dispatched so suspend reuse
      _lastError = e;
      _isRefreshIndeterminate = true;
      _applyRateLimit(e.retryAfter);
      _session = null;
      await _clearStorageFailClosed();
      notifyListeners();
      rethrow;
    } on AegisTimeoutException catch (e) {
      // Network timeout AFTER refresh request was sent: INDETERMINATE rotation state!
      _lastError = e;
      _isRefreshIndeterminate = true;
      _status = AuthSessionStatus.expired;
      notifyListeners();
      rethrow;
    } on AegisServerException catch (e) {
      // 503 / 5xx AFTER refresh request was sent: INDETERMINATE rotation state!
      _lastError = e;
      _isRefreshIndeterminate = true;
      _status = AuthSessionStatus.expired;
      notifyListeners();
      rethrow;
    } on AegisNetworkException catch (e) {
      // Network drop AFTER refresh request was sent: INDETERMINATE rotation state!
      _lastError = e;
      _isRefreshIndeterminate = true;
      _status = AuthSessionStatus.expired;
      notifyListeners();
      rethrow;
    } on AegisApiException catch (e) {
      _lastError = e;
      _isRefreshIndeterminate = true;
      _status = AuthSessionStatus.expired;
      notifyListeners();
      rethrow;
    }

    // Response validations
    if (!isValidMobileAccessTokenJwt(response.accessToken)) {
      _status = AuthSessionStatus.expired;
      _session = null;
      await _clearStorageFailClosed();
      notifyListeners();
      const err = AegisAuthException(
        'Server returned a non-JWT access token during refresh.',
        statusCode: 401,
      );
      _lastError = err;
      throw err;
    }

    if (response.patientId != previousSession.patientId ||
        response.deviceId != previousSession.deviceId) {
      _status = AuthSessionStatus.expired;
      _unregisterActiveDevice(previousSession.deviceId);
      _session = null;
      await _clearStorageFailClosed();
      notifyListeners();
      const err = AegisAuthException(
        'Security violation: patient_id or device_id changed during token refresh.',
        statusCode: 401,
      );
      _lastError = err;
      throw err;
    }

    if (response.refreshToken == oldRefreshToken) {
      _status = AuthSessionStatus.expired;
      _session = null;
      await _clearStorageFailClosed();
      notifyListeners();
      const err = AegisAuthException(
        'Security violation: server did not rotate single-use refresh token.',
        statusCode: 401,
      );
      _lastError = err;
      throw err;
    }

    // PERSISTENCE OF ROTATED SESSION:
    // If saving the new session fails, the old token in storage is dead on the server.
    // We MUST NOT leave the old token as active! Fail-closed transition to expired.
    final rotatedSession = AuthSession.fromRefreshResponse(response);
    try {
      await _storage.saveSession(rotatedSession);
    } on SecureStorageUnavailableException catch (e) {
      _log('Failed to persist rotated session: ${e.message}');
      _lastStorageError = e;
      _status = AuthSessionStatus.expired;
      _isRefreshIndeterminate = true;
      _session = null;
      _unregisterActiveDevice(previousSession.deviceId);
      notifyListeners();
      throw SecureStorageUnavailableException(
        'Rotated auth session could not be securely persisted. Re-pairing is required.',
        e,
      );
    } catch (_) {
      _log('Unexpected error persisting rotated session.');
      const safeErr = SecureStorageUnavailableException(
        'Rotated auth session could not be securely persisted. Re-pairing is required.',
      );
      _lastStorageError = safeErr;
      _status = AuthSessionStatus.expired;
      _isRefreshIndeterminate = true;
      _session = null;
      _unregisterActiveDevice(previousSession.deviceId);
      notifyListeners();
      throw safeErr;
    }

    // Requirement 4: Verify the newly persisted session before clearing the old guard
    AuthSession? verifiedSession;
    try {
      verifiedSession = await _storage.loadSession();
    } on SecureStorageUnavailableException catch (e) {
      _log('Failed to verify persisted rotated session: ${e.message}');
      _status = AuthSessionStatus.expired;
      _isRefreshIndeterminate = true;
      _session = null;
      _unregisterActiveDevice(previousSession.deviceId);
      _lastStorageError = e;
      notifyListeners();
      rethrow;
    } catch (_) {
      _log('Failed to verify persisted rotated session: unexpected error.');
      _status = AuthSessionStatus.expired;
      _isRefreshIndeterminate = true;
      _session = null;
      _unregisterActiveDevice(previousSession.deviceId);
      const verifyErr = SecureStorageUnavailableException(
        'Failed to verify persisted rotated session in secure storage.',
      );
      _lastStorageError = verifyErr;
      notifyListeners();
      throw verifyErr;
    }

    if (verifiedSession == null ||
        verifiedSession.refreshToken != rotatedSession.refreshToken ||
        verifiedSession.deviceId != rotatedSession.deviceId) {
      _log('Security violation: verified persisted session does not match rotated credentials.');
      _status = AuthSessionStatus.expired;
      _isRefreshIndeterminate = true;
      _session = null;
      _unregisterActiveDevice(previousSession.deviceId);
      const verifyErr = SecureStorageUnavailableException(
        'Persisted session validation failed after rotation. Re-pairing is required.',
      );
      _lastStorageError = verifyErr;
      notifyListeners();
      throw verifyErr;
    }

    // Both HTTP rotation and saveSession verified; safely clear in-flight record
    try {
      await _storage.clearRefreshInFlightRecord();
    } on SecureStorageUnavailableException catch (e) {
      _log('Notice: Failed to clear in-flight record after rotation: ${e.message}');
      _lastStorageError = e;
    } catch (_) {
      _log('Notice: Failed to clear in-flight record after rotation.');
    }

    _session = rotatedSession;
    _status = AuthSessionStatus.authenticated;
    _isRefreshIndeterminate = false;
    _rateLimitedUntil = null;
    _lastRetryAfter = null;
    _lastError = null;
    _lastStorageError = null;
    _log('Refreshed device session tokens (deviceId: ${rotatedSession.deviceId})');
    notifyListeners();
    return rotatedSession.accessToken;
  }

  /// Handles an [AegisApiException] from a normal protected API call (e.g., `/vitals/batch`).
  /// - Does NOT clear [IAuthTokenStorage] on transient network errors, timeouts, or 503 responses.
  /// - Does NOT trigger token refresh or retry on `403 Forbidden` (insufficient scopes).
  /// - On `401 Unauthorized`, attempts at most one Single-Flight refresh if [alreadyRetried] is false.
  Future<bool> handleProtectedEndpointError(
    AegisApiException error, {
    bool alreadyRetried = false,
  }) async {
    _ensureNotDisposed();
    _lastError = error;

    if (error is AegisForbiddenException) {
      // 403 Forbidden: insufficient permissions; never retry or loop refresh
      notifyListeners();
      return false;
    }

    if (error is AegisRateLimitException) {
      _applyRateLimit(error.retryAfter);
      notifyListeners();
      return false;
    }

    if (error is AegisNetworkException ||
        error is AegisTimeoutException ||
        error is AegisServerException) {
      // Normal endpoint transient failure: preserve stored session in SecureAuthTokenStorage
      notifyListeners();
      return false;
    }

    if (error is AegisAuthException) {
      if (alreadyRetried) {
        // Prevent infinite retry loops when 401 persists after refresh
        _status = AuthSessionStatus.expired;
        _session = null;
        await _clearStorageFailClosed();
        notifyListeners();
        return false;
      }
      try {
        final newToken = await refreshSession();
        return newToken.isNotEmpty;
      } on AegisApiException {
        return false;
      } on SecureStorageUnavailableException {
        return false;
      }
    }

    notifyListeners();
    return false;
  }

  /// Revokes the current device session on the server (`POST /api/v1/auth/revoke`)
  /// and clears [IAuthTokenStorage] ONLY after server confirmation.
  Future<DeviceRevokeResponse> revokeSession() async {
    _ensureNotDisposed();

    if (_session == null) {
      const err = AegisAuthException(
        'Cannot revoke session: no active session exists.',
        statusCode: 401,
      );
      _lastError = err;
      throw err;
    }

    final current = _session!;
    final now = _nowProvider().toUtc();
    final validJwt = (!current.isAccessTokenExpired(skewBuffer: Duration.zero, now: now) &&
            isValidMobileAccessTokenJwt(current.accessToken))
        ? current.accessToken
        : null;

    try {
      final response = await _apiClient.revokeDevice(
        DeviceRevokeRequest(
          deviceId: current.deviceId,
          refreshToken: current.refreshToken,
        ),
        accessToken: validJwt,
      );

      if (!response.revoked) {
        const err = AegisServerException(
          'Server did not confirm device revocation.',
          statusCode: 500,
        );
        _lastError = err;
        throw err;
      }

      _dispatchedRefreshTokens.add(current.refreshToken);
      _unregisterActiveDevice(current.deviceId);

      _session = null;
      _status = AuthSessionStatus.unauthenticated;
      _isRefreshIndeterminate = false;
      _rateLimitedUntil = null;
      _lastRetryAfter = null;
      _lastError = null;

      try {
        await _storage.clearSession();

        // Verification: ensure session is genuinely removed before clearing guard
        AuthSession? remainingSession;
        try {
          remainingSession = await _storage.loadSession();
        } catch (e) {
          _isRefreshIndeterminate = true;
          final storageErr = e is SecureStorageUnavailableException
              ? e
              : const SecureStorageUnavailableException(
                  'Failed to verify session deletion after revoke.',
                );
          _lastStorageError = storageErr;
          notifyListeners();
          throw storageErr;
        }

        if (remainingSession != null) {
          _isRefreshIndeterminate = true;
          const storageErr = SecureStorageUnavailableException(
            'Failed to reliably clear auth session from storage after revoke.',
          );
          _lastStorageError = storageErr;
          notifyListeners();
          throw storageErr;
        }

        await _storage.clearRefreshInFlightRecord();
      } on SecureStorageUnavailableException catch (e) {
        _log('Failed to clear token storage after confirmed revoke: ${e.message}');
        _lastStorageError = e;
        _isRefreshIndeterminate = true;
        notifyListeners();
        rethrow;
      } catch (_) {
        _log('Failed to clear token storage after confirmed revoke: unexpected failure.');
        const safeErr = SecureStorageUnavailableException(
          'Failed to clear token storage after confirmed revoke.',
        );
        _lastStorageError = safeErr;
        _isRefreshIndeterminate = true;
        notifyListeners();
        throw safeErr;
      }

      _log('Device session revoked and local storage cleared (deviceId: ${response.deviceId})');
      notifyListeners();
      return response;
    } on AegisApiException catch (e) {
      _lastError = e;
      notifyListeners();
      rethrow;
    }
  }

  void _ensureNotDisposed() {
    if (_isDisposed) {
      throw StateError('AuthSessionManager has already been disposed.');
    }
  }

  @override
  void dispose() {
    if (!_isDisposed) {
      _isDisposed = true;
      if (_boundTokenProviderToClient &&
          _boundTokenProvider != null &&
          identical(_apiClient.tokenProvider, _boundTokenProvider)) {
        _apiClient.bindTokenProvider(null);
      }
      if (identical(_apiClient.attachedSessionManager, this)) {
        _apiClient.attachSessionManager(null);
      }
      _activeStorages.remove(_storage);
      if (_storage is SecureAuthTokenStorage) {
        _hasActiveSecureStorageManager = false;
      }
      _unregisterActiveDevice(_session?.deviceId);
    }
    super.dispose();
  }
}
