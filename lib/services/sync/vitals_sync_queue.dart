import 'dart:async';
import 'dart:convert';
import 'package:flutter/foundation.dart';
import 'package:shared_preferences/shared_preferences.dart';
import '../api/aegis_api_client.dart';
import '../api/vitals_api_models.dart';
import '../auth/auth_session_manager.dart';
import '../health_data_provider.dart';

/// Exception thrown when local queue state persistence fails
class VitalsQueuePersistenceException implements Exception {
  final String message;
  final dynamic cause;

  const VitalsQueuePersistenceException(this.message, [this.cause]);

  @override
  String toString() => 'VitalsQueuePersistenceException: $message${cause != null ? ' ($cause)' : ''}';
}

/// Status of the sync flush operation
enum SyncStatus {
  idle,
  success,
  retryableError,
  paused,
  conflict,
  persistenceError,
}

/// Diagnostic status of medical cycle resolution
enum CycleResolutionStatus {
  idle,
  awaitingAuthorization,
  active,
  noActiveCycle,
  authError,
  networkError,
}

class SyncResult {
  final SyncStatus status;
  final int processedCount;
  final String? message;
  final bool persistenceFailed;

  const SyncResult({
    required this.status,
    this.processedCount = 0,
    this.message,
    this.persistenceFailed = false,
  });

  static const SyncResult idle = SyncResult(status: SyncStatus.idle);
}

/// An immutable item in the persistent sync queue
@immutable
class VitalsQueueItem {
  final String eventId;
  final String? idempotencyKey;
  final String? cycleId;
  final String? patientId;
  final HealthMeasurementCreate dto;
  final DateTime enqueuedAt;
  final int retryCount;
  final String? lastError;
  final bool isQuarantined;

  const VitalsQueueItem({
    required this.eventId,
    this.idempotencyKey,
    this.cycleId,
    this.patientId,
    required this.dto,
    required this.enqueuedAt,
    this.retryCount = 0,
    this.lastError,
    this.isQuarantined = false,
  });

  Map<String, dynamic> toJson() => {
    'event_id': eventId,
    'idempotency_key': idempotencyKey,
    'cycle_id': cycleId,
    'patient_id': patientId,
    'dto': dto.toJson(),
    'enqueued_at': enqueuedAt.toIso8601String(),
    'retry_count': retryCount,
    'last_error': lastError,
    'is_quarantined': isQuarantined,
  };

  factory VitalsQueueItem.fromJson(Map<String, dynamic> json) => VitalsQueueItem(
    eventId: json['event_id'] as String,
    idempotencyKey: json['idempotency_key'] as String?,
    cycleId: json['cycle_id'] as String?,
    patientId: json['patient_id'] as String?,
    dto: HealthMeasurementCreate.fromJson(json['dto'] as Map<String, dynamic>),
    enqueuedAt: DateTime.parse(json['enqueued_at'] as String),
    retryCount: json['retry_count'] as int? ?? 0,
    lastError: json['last_error'] as String?,
    isQuarantined: json['is_quarantined'] == true,
  );

  VitalsQueueItem copyWith({
    String? idempotencyKey,
    String? cycleId,
    String? patientId,
    HealthMeasurementCreate? dto,
    int? retryCount,
    String? lastError,
    bool? isQuarantined,
  }) {
    return VitalsQueueItem(
      eventId: eventId,
      idempotencyKey: idempotencyKey ?? this.idempotencyKey,
      cycleId: cycleId ?? this.cycleId,
      patientId: patientId ?? this.patientId,
      dto: dto ?? this.dto,
      enqueuedAt: enqueuedAt,
      retryCount: retryCount ?? this.retryCount,
      lastError: lastError ?? this.lastError,
      isQuarantined: isQuarantined ?? this.isQuarantined,
    );
  }
}

/// Persistent, resilient offline-first telemetry synchronization queue
/// Backed by a single unified storage snapshot in SharedPreferences.
class VitalsSyncQueue extends ChangeNotifier {
  static const String _snapshotStorageKey = 'aegis_vitals_sync_snapshot_v2';
  static const String _legacyPendingKey = 'aegis_vitals_sync_pending_v1';
  static const String _legacyQuarantineKey = 'aegis_vitals_sync_quarantine_v1';

  AegisApiClient _apiClient;
  AuthSessionManager? _authSessionManager;
  String? _activePatientId;
  bool _patientIsolationEnabled = false;

  String? _activeCycleId;
  bool _isCycleConfirmedByServer = false;
  CycleResolutionStatus _cycleStatus = CycleResolutionStatus.idle;
  int _sessionGeneration = 0;

  final List<VitalsQueueItem> _pendingItems = [];
  final List<VitalsQueueItem> _quarantinedItems = [];

  bool _isFlushing = false;
  bool _isPaused = false;
  String? _pauseReason;
  DateTime? _nextRetryAllowedAfter;
  int _consecutiveFailures = 0;

  Future<void>? _initFuture;
  bool _isInitialized = false;
  Object? _initError;
  String? _lastSyncError;
  DateTime? _lastSyncAttemptAt;
  DateTime? _lastSyncSuccessAt;
  Timer? _debouncedFlushTimer;
  Timer? _periodicSyncTimer;

  static int _eventCounter = 0;

  // Mutual exclusion lock for serializing asynchronous queue and storage operations
  Future<void> _lock = Future.value();

  Future<T> _synchronized<T>(Future<T> Function() action) async {
    final previousLock = _lock;
    final completer = Completer<void>();
    _lock = completer.future;

    try {
      await previousLock;
      return await action();
    } finally {
      completer.complete();
    }
  }

  List<VitalsQueueItem> get pendingItems => List.unmodifiable(_pendingItems);
  List<VitalsQueueItem> get quarantinedItems => List.unmodifiable(_quarantinedItems);
  List<VitalsQueueItem> get activePatientPendingItems => List.unmodifiable(
        _pendingItems.where(
          (i) => !_patientIsolationEnabled || (_activePatientId != null && i.patientId == _activePatientId),
        ),
      );
  List<VitalsQueueItem> pendingItemsForPatient(String patientId) =>
      List.unmodifiable(_pendingItems.where((i) => i.patientId == patientId));

  String? get activePatientId => _activePatientId;
  bool get isPatientIsolationEnabled => _patientIsolationEnabled;
  AuthSessionManager? get authSessionManager => _authSessionManager;

  bool get isPaused => _isPaused;
  bool get paused => _isPaused;
  String? get pauseReason => _pauseReason;
  String? get activeCycleId => _activeCycleId;
  CycleResolutionStatus get cycleStatus => _cycleStatus;
  bool get isAwaitingAuthorization => _cycleStatus == CycleResolutionStatus.awaitingAuthorization;
  bool get awaitingAuthorization => isAwaitingAuthorization;
  bool get isCycleActive => _cycleStatus == CycleResolutionStatus.active && _activeCycleId != null && _isCycleConfirmedByServer;
  bool get isCycleConfirmedByServer => _isCycleConfirmedByServer;
  int get sessionGeneration => _sessionGeneration;

  // Runtime diagnostics
  bool get isInitialized => _isInitialized;
  bool get initialized => _isInitialized;
  Object? get initError => _initError;
  int get pendingCount => _pendingItems.length;
  int get quarantinedCount => _quarantinedItems.length;
  bool get isSyncing => _isFlushing;
  bool get syncing => _isFlushing;
  bool get isPeriodicSyncRunning => _periodicSyncTimer != null && _periodicSyncTimer!.isActive;
  String? get lastSyncError => _lastSyncError;
  DateTime? get lastSyncAttemptAt => _lastSyncAttemptAt;
  DateTime? get lastSyncSuccessAt => _lastSyncSuccessAt;

  VitalsSyncQueue({
    required AegisApiClient apiClient,
    String? activeCycleId,
    AuthSessionManager? authSessionManager,
    String? activePatientId,
    bool enforcePatientIsolation = false,
  })  : _apiClient = apiClient,
        _activeCycleId = (activeCycleId != null && activeCycleId.isNotEmpty) ? activeCycleId : null,
        _isCycleConfirmedByServer = (activeCycleId != null && activeCycleId.isNotEmpty),
        _cycleStatus = (activeCycleId != null && activeCycleId.isNotEmpty)
            ? CycleResolutionStatus.active
            : CycleResolutionStatus.idle {
    if (activePatientId != null && activePatientId.trim().isNotEmpty) {
      _activePatientId = activePatientId.trim();
      _patientIsolationEnabled = true;
    }
    if (enforcePatientIsolation) {
      _patientIsolationEnabled = true;
    }
    _apiClient.setSessionManagerListener(_onClientSessionManagerAttached);
    final effectiveManager = authSessionManager ??
        (_apiClient.attachedSessionManager is AuthSessionManager
            ? _apiClient.attachedSessionManager as AuthSessionManager
            : null);
    if (effectiveManager != null) {
      bindAuthSessionManager(effectiveManager);
    }
  }

  void _onClientSessionManagerAttached(Object? manager) {
    if (manager is AuthSessionManager && !identical(_authSessionManager, manager)) {
      bindAuthSessionManager(manager);
    }
  }

  /// Binds an AuthSessionManager to enforce patient-level queue isolation and lifecycle sync.
  void bindAuthSessionManager(AuthSessionManager? manager) {
    if (identical(_authSessionManager, manager)) return;
    _authSessionManager?.removeListener(_onAuthSessionChanged);
    _authSessionManager = manager;
    if (manager != null) {
      _patientIsolationEnabled = true;
      manager.addListener(_onAuthSessionChanged);
      _onAuthSessionChanged();
    }
  }

  void _onAuthSessionChanged() {
    final manager = _authSessionManager;
    if (manager == null) return;
    _patientIsolationEnabled = true;

    final currentSession = manager.currentSession;
    final newPatientId = currentSession?.patientId ?? manager.lastKnownPatientId;

    if (newPatientId != null && newPatientId.isNotEmpty) {
      if (_activePatientId == null) {
        _activePatientId = newPatientId;
        if (_isInitialized) {
          _synchronized(() async {
            final changed = _quarantineLegacyUnassignedItemsLocked();
            await _persist();
            if (changed) notifyListeners();
          }).catchError((e) {
            debugPrint('[VitalsSyncQueue] Persistence warning on initial patient binding: $e');
          });
        }
        if (manager.status == AuthSessionStatus.authenticated &&
            _isPaused &&
            (_pauseReason == null ||
                _pauseReason!.toLowerCase().contains('authoriz') ||
                _pauseReason!.toLowerCase().contains('401') ||
                _pauseReason!.toLowerCase().contains('session') ||
                _pauseReason!.toLowerCase().contains('refresh') ||
                _pauseReason!.toLowerCase().contains('expired') ||
                _pauseReason!.toLowerCase().contains('indeterminate'))) {
          _isPaused = false;
          _pauseReason = null;
          _lastSyncError = null;
          _consecutiveFailures = 0;
          _nextRetryAllowedAfter = null;
          if (_cycleStatus == CycleResolutionStatus.authError ||
              _cycleStatus == CycleResolutionStatus.awaitingAuthorization) {
            _cycleStatus = (_activeCycleId != null && _isCycleConfirmedByServer)
                ? CycleResolutionStatus.active
                : CycleResolutionStatus.idle;
          }
        }
        notifyListeners();
      } else if (_activePatientId != newPatientId) {
        // Patient switch detected (Patient A -> Patient B):
        // Increment sessionGeneration, invalidate cycle confirmation, preserve Patient A items untouched.
        _sessionGeneration++;
        final capturedEpoch = _sessionGeneration;
        _activePatientId = newPatientId;
        _activeCycleId = null;
        _isCycleConfirmedByServer = false;
        _cycleStatus = CycleResolutionStatus.idle;
        _isPaused = false;
        _pauseReason = null;
        _lastSyncError = null;
        _consecutiveFailures = 0;
        _nextRetryAllowedAfter = null;
        cancelScheduledFlush();
        notifyListeners();

        if (_isInitialized) {
          _synchronized(() async {
            _quarantineLegacyUnassignedItemsLocked();
            await _persist(sessionEpoch: capturedEpoch);
          }).catchError((e) {
            debugPrint('[VitalsSyncQueue] Persistence warning on patient switch: $e');
          });
        }
      } else {
        // Same patient (e.g. token rotation /refresh or re-authorization of the SAME Patient A):
        // Do NOT increment _sessionGeneration and do NOT reset active cycle or queue!
        if (manager.status == AuthSessionStatus.authenticated &&
            _isPaused &&
            (_pauseReason == null ||
                _pauseReason!.toLowerCase().contains('authoriz') ||
                _pauseReason!.toLowerCase().contains('401') ||
                _pauseReason!.toLowerCase().contains('session') ||
                _pauseReason!.toLowerCase().contains('refresh') ||
                _pauseReason!.toLowerCase().contains('expired') ||
                _pauseReason!.toLowerCase().contains('indeterminate'))) {
          _isPaused = false;
          _pauseReason = null;
          _lastSyncError = null;
          _consecutiveFailures = 0;
          _nextRetryAllowedAfter = null;
          if (_cycleStatus == CycleResolutionStatus.authError ||
              _cycleStatus == CycleResolutionStatus.awaitingAuthorization ||
              _cycleStatus == CycleResolutionStatus.networkError) {
            _cycleStatus = (_activeCycleId != null && _isCycleConfirmedByServer)
                ? CycleResolutionStatus.active
                : CycleResolutionStatus.idle;
          }
          notifyListeners();
        }
      }
    }

    // Handle non-authenticated states (expired, indeterminate refresh, unauthenticated)
    if (manager.isRefreshIndeterminate) {
      _isCycleConfirmedByServer = false;
      _cycleStatus = CycleResolutionStatus.networkError;
      _isPaused = true;
      _pauseReason = 'Session refresh indeterminate: outbound sync paused';
      _lastSyncError = _pauseReason;
      cancelScheduledFlush();
      notifyListeners();
    } else if (manager.status == AuthSessionStatus.expired) {
      _isCycleConfirmedByServer = false;
      _cycleStatus = CycleResolutionStatus.authError;
      _isPaused = true;
      _pauseReason = 'Session expired: awaiting re-authorization';
      _lastSyncError = _pauseReason;
      cancelScheduledFlush();
      notifyListeners();
    } else if (manager.status == AuthSessionStatus.unauthenticated && currentSession == null) {
      _isCycleConfirmedByServer = false;
      _cycleStatus = CycleResolutionStatus.awaitingAuthorization;
      _isPaused = true;
      _pauseReason = 'Awaiting authorization: active medical cycle requires user session';
      cancelScheduledFlush();
      notifyListeners();
    }
  }

  /// Explicitly sets or switches the active patient ID for queue isolation.
  Future<void> setActivePatientId(String? patientId) async {
    if (_initFuture != null) {
      try {
        await _initFuture;
      } catch (_) {}
    } else if (!_isInitialized) {
      try {
        await init();
      } catch (_) {}
    }

    await _synchronized(() async {
      _patientIsolationEnabled = true;
      final cleanId = (patientId != null && patientId.trim().isNotEmpty) ? patientId.trim() : null;
      bool modified = _quarantineLegacyUnassignedItemsLocked();

      if (cleanId != _activePatientId) {
        if (_activePatientId != null && cleanId != null && _activePatientId != cleanId) {
          _sessionGeneration++;
          _activeCycleId = null;
          _isCycleConfirmedByServer = false;
          _cycleStatus = CycleResolutionStatus.idle;
          _isPaused = false;
          _pauseReason = null;
          _lastSyncError = null;
          _consecutiveFailures = 0;
          _nextRetryAllowedAfter = null;
          cancelScheduledFlush();
        } else if (cleanId == null && _activePatientId != null) {
          _isCycleConfirmedByServer = false;
          _cycleStatus = CycleResolutionStatus.awaitingAuthorization;
          _isPaused = true;
          _pauseReason = 'Awaiting authorization: no active patient session';
          cancelScheduledFlush();
        }
        _activePatientId = cleanId;
        modified = true;
      }

      if (modified && _isInitialized) {
        await _persist();
        notifyListeners();
      }
    });
  }

  /// Moves any legacy pending items without `patient_id` into isolated quarantine.
  /// Never deletes or automatically assigns legacy unassigned items to a patient.
  bool _quarantineLegacyUnassignedItemsLocked() {
    if (!_patientIsolationEnabled) return false;
    bool moved = false;
    final remaining = <VitalsQueueItem>[];
    for (final item in _pendingItems) {
      if (item.patientId == null || item.patientId!.trim().isEmpty) {
        _quarantinedItems.add(
          item.copyWith(
            isQuarantined: true,
            lastError: item.lastError ??
                'Quarantined: legacy measurement without patient_id isolation binding',
          ),
        );
        moved = true;
      } else {
        remaining.add(item);
      }
    }
    if (moved) {
      _pendingItems
        ..clear()
        ..addAll(remaining);
    }
    return moved;
  }

  /// Updates the underlying AegisApiClient (e.g. when auth token becomes available, refreshed, or switched).
  void updateApiClient(AegisApiClient newClient) {
    if (identical(_apiClient, newClient)) return;
    _apiClient.setSessionManagerListener(null);
    _sessionGeneration++;
    _apiClient = newClient;
    _apiClient.setSessionManagerListener(_onClientSessionManagerAttached);
    if (_apiClient.attachedSessionManager is AuthSessionManager) {
      bindAuthSessionManager(_apiClient.attachedSessionManager as AuthSessionManager);
    }
    // Session context changed: cycle confirmation from previous client cannot be trusted for new client
    _activeCycleId = null;
    _isCycleConfirmedByServer = false;
    _cycleStatus = CycleResolutionStatus.idle;
    notifyListeners();
    resolveActiveCycle(force: true).catchError((error) {
      debugPrint('[VitalsSyncQueue] Cycle resolution error on client update: $error');
      return false;
    });
  }

  Future<String?> _resolveCurrentAuthToken(AegisApiClient client) async {
    final manager = _authSessionManager;
    if (manager != null) {
      if (!manager.isInitialized) {
        try {
          await manager.initialize();
        } catch (_) {}
      }
      if (manager.isRefreshIndeterminate ||
          manager.status == AuthSessionStatus.unauthenticated ||
          manager.status == AuthSessionStatus.expired) {
        return null;
      }
      try {
        return await manager.getValidAccessToken();
      } catch (_) {
        return null;
      }
    }
    return await client.getAuthToken();
  }

  /// Resolves the active medical cycle from backend if authorized.
  /// If no authorization token is present, skips network call and sets cycleStatus to awaitingAuthorization.
  /// If an authorized session is present, queries AegisApiClient.getCurrentCycle() and calls setActiveCycleId().
  Future<bool> resolveActiveCycle({bool force = false}) =>
      _resolveActiveCycleInternal(force: force, alreadyLocked: false);

  Future<bool> _resolveActiveCycleInternal({bool force = false, required bool alreadyLocked}) async {
    final requestEpoch = _sessionGeneration;
    final client = _apiClient;

    if (_initFuture != null) {
      try {
        await _initFuture;
      } catch (_) {}
    } else if (!_isInitialized) {
      try {
        await init();
      } catch (_) {}
    }

    if (_sessionGeneration != requestEpoch || !identical(_apiClient, client)) {
      debugPrint('[VitalsSyncQueue] Stale cycle resolution discarded: session changed during init');
      return false;
    }

    if (!_isInitialized) {
      debugPrint('[VitalsSyncQueue] Cannot resolve cycle: queue not initialized');
      return false;
    }

    if (_activeCycleId != null && _isCycleConfirmedByServer && !force) {
      _cycleStatus = CycleResolutionStatus.active;
      return true;
    }

    // 1. Check if valid auth token is present (prevents pointless network requests)
    if (_sessionGeneration != requestEpoch || !identical(_apiClient, client)) return false;
    String? token = await _resolveCurrentAuthToken(client);
    if (_sessionGeneration != requestEpoch || !identical(_apiClient, client)) {
      debugPrint('[VitalsSyncQueue] Stale cycle resolution discarded: session changed during auth check');
      return false;
    }

    if (token == null || token.trim().isEmpty) {
      _isCycleConfirmedByServer = false;
      final manager = _authSessionManager;
      if (manager != null && manager.isRefreshIndeterminate) {
        _cycleStatus = CycleResolutionStatus.networkError;
        _pauseReason = 'Session refresh indeterminate: outbound sync paused';
        _lastSyncError = _pauseReason;
      } else if (manager != null && manager.status == AuthSessionStatus.expired) {
        _cycleStatus = CycleResolutionStatus.authError;
        _pauseReason = 'Session expired: awaiting re-authorization';
        _lastSyncError = _pauseReason;
      } else {
        _cycleStatus = CycleResolutionStatus.awaitingAuthorization;
        _pauseReason = 'Awaiting authorization: active medical cycle requires user session';
      }
      _isPaused = true;
      notifyListeners();
      debugPrint('[VitalsSyncQueue] Cycle resolution skipped: $_pauseReason (no network request)');
      return false;
    }

    final jwtPatientId = AuthSessionManager.extractPatientIdFromJwt(token);
    if (_patientIsolationEnabled) {
      if (jwtPatientId == null) {
        debugPrint('[VitalsSyncQueue] Cycle resolution aborted: valid mobile JWT with patient sub claim required');
        _isCycleConfirmedByServer = false;
        _cycleStatus = CycleResolutionStatus.authError;
        _isPaused = true;
        _pauseReason = 'Awaiting authorization: valid mobile JWT with patient sub claim is required';
        _lastSyncError = _pauseReason;
        notifyListeners();
        return false;
      }
      if (_activePatientId == null) {
        _activePatientId = jwtPatientId;
      } else if (_activePatientId != jwtPatientId) {
        debugPrint('[VitalsSyncQueue] Cycle resolution aborted: token patient_id mismatch with active queue patient');
        _isCycleConfirmedByServer = false;
        _cycleStatus = CycleResolutionStatus.authError;
        _isPaused = true;
        _pauseReason = 'Patient isolation mismatch between session token and active queue';
        _lastSyncError = _pauseReason;
        notifyListeners();
        return false;
      }
    } else if (jwtPatientId != null) {
      _patientIsolationEnabled = true;
      _activePatientId = jwtPatientId;
    }

    if (_patientIsolationEnabled && (_activePatientId == null || _activePatientId!.isEmpty)) {
      _isCycleConfirmedByServer = false;
      _cycleStatus = CycleResolutionStatus.awaitingAuthorization;
      _pauseReason = 'Awaiting authorization: patient identity required';
      _isPaused = true;
      notifyListeners();
      return false;
    }

    final expectedPatientId = _activePatientId;

    // 2. Query client.getCurrentCycle()
    try {
      MedicalCycleResponse cycle;
      try {
        cycle = await client.getCurrentCycle(accessToken: token);
      } on AegisAuthException catch (e) {
        if (_authSessionManager != null &&
            _sessionGeneration == requestEpoch &&
            identical(_apiClient, client)) {
          final recovered = await _authSessionManager!.handleProtectedEndpointError(e);
          final updatedToken = await _resolveCurrentAuthToken(client);
          final updatedPatient = _authSessionManager!.currentSession?.patientId;
          if (recovered &&
              updatedToken != null &&
              updatedToken.isNotEmpty &&
              _sessionGeneration == requestEpoch &&
              identical(_apiClient, client) &&
              (expectedPatientId == null || updatedPatient == expectedPatientId)) {
            token = updatedToken;
            cycle = await client.getCurrentCycle(accessToken: token);
          } else {
            rethrow;
          }
        } else {
          rethrow;
        }
      }

      if (_sessionGeneration != requestEpoch ||
          !identical(_apiClient, client) ||
          _activePatientId != expectedPatientId) {
        debugPrint('[VitalsSyncQueue] Discarding stale cycle resolution response from prior session generation');
        return false;
      }

      if (cycle.patientId != null &&
          expectedPatientId != null &&
          cycle.patientId != expectedPatientId) {
        _isCycleConfirmedByServer = false;
        _cycleStatus = CycleResolutionStatus.authError;
        _isPaused = true;
        _pauseReason = 'Cycle patient_id mismatch with active session patient';
        _lastSyncError = _pauseReason;
        notifyListeners();
        return false;
      }

      if (cycle.status == 'active') {
        if (alreadyLocked) {
          if (_sessionGeneration != requestEpoch || !identical(_apiClient, client)) return false;
          await _resolveActiveCycleLocked(
            cycle.cycleId,
            confirmedByServer: true,
            sessionEpoch: requestEpoch,
            client: client,
            expectedPatientId: expectedPatientId,
          );
        } else {
          await _synchronized(() async {
            if (_sessionGeneration != requestEpoch || !identical(_apiClient, client)) return;
            await _resolveActiveCycleLocked(
              cycle.cycleId,
              confirmedByServer: true,
              sessionEpoch: requestEpoch,
              client: client,
              expectedPatientId: expectedPatientId,
            );
          });
        }

        if (_sessionGeneration != requestEpoch ||
            !identical(_apiClient, client) ||
            _activePatientId != expectedPatientId) {
          debugPrint('[VitalsSyncQueue] Discarding cycle confirmation: session changed during lock');
          return false;
        }

        _isPaused = false;
        _pauseReason = null;
        _lastSyncError = null;
        notifyListeners();
        debugPrint('[VitalsSyncQueue] Active medical cycle resolved: ${cycle.cycleId}');
        return true;
      } else {
        if (_sessionGeneration != requestEpoch || !identical(_apiClient, client)) return false;
        _isCycleConfirmedByServer = false;
        _cycleStatus = CycleResolutionStatus.noActiveCycle;
        _isPaused = true;
        _pauseReason = 'No active cycle: status is "${cycle.status}"';
        _lastSyncError = _pauseReason;
        notifyListeners();
        debugPrint('[VitalsSyncQueue] Cycle not active: status "${cycle.status}"');
        return false;
      }
    } on AegisCycleNotFoundException catch (e) {
      if (_sessionGeneration != requestEpoch ||
          !identical(_apiClient, client) ||
          _activePatientId != expectedPatientId) {
        return false;
      }
      _isCycleConfirmedByServer = false;
      _cycleStatus = CycleResolutionStatus.noActiveCycle;
      _isPaused = true;
      _pauseReason = '404 Cycle Not Found: ${e.message}';
      _lastSyncError = _pauseReason;
      notifyListeners();
      debugPrint('[VitalsSyncQueue] Paused: No active cycle available on server (404)');
      return false;
    } on AegisAuthException catch (e) {
      if (_sessionGeneration != requestEpoch ||
          !identical(_apiClient, client) ||
          _activePatientId != expectedPatientId) {
        return false;
      }
      _isCycleConfirmedByServer = false;
      _cycleStatus = CycleResolutionStatus.authError;
      _isPaused = true;
      _pauseReason = '401 Unauthorized: ${e.message}';
      _lastSyncError = _pauseReason;
      notifyListeners();
      debugPrint('[VitalsSyncQueue] Paused on auth failure during cycle resolution (401)');
      return false;
    } on AegisForbiddenException catch (e) {
      if (_sessionGeneration != requestEpoch ||
          !identical(_apiClient, client) ||
          _activePatientId != expectedPatientId) {
        return false;
      }
      _isCycleConfirmedByServer = false;
      _cycleStatus = CycleResolutionStatus.authError;
      _isPaused = true;
      _pauseReason = '403 Forbidden: ${e.message}';
      _lastSyncError = _pauseReason;
      notifyListeners();
      debugPrint('[VitalsSyncQueue] Paused on forbidden access during cycle resolution (403)');
      return false;
    } on AegisTimeoutException catch (e) {
      if (_sessionGeneration != requestEpoch ||
          !identical(_apiClient, client) ||
          _activePatientId != expectedPatientId) {
        return false;
      }
      _cycleStatus = CycleResolutionStatus.networkError;
      _lastSyncError = 'Timeout during cycle resolution: ${e.message}';
      notifyListeners();
      debugPrint('[VitalsSyncQueue] Network timeout during cycle resolution');
      return false;
    } on AegisNetworkException catch (e) {
      if (_sessionGeneration != requestEpoch ||
          !identical(_apiClient, client) ||
          _activePatientId != expectedPatientId) {
        return false;
      }
      _cycleStatus = CycleResolutionStatus.networkError;
      _lastSyncError = 'Network error during cycle resolution: ${e.message}';
      notifyListeners();
      debugPrint('[VitalsSyncQueue] Network connection error during cycle resolution: ${e.message}');
      return false;
    } on VitalsQueuePersistenceException catch (e) {
      if (_sessionGeneration != requestEpoch ||
          !identical(_apiClient, client) ||
          _activePatientId != expectedPatientId) {
        if (_lastSyncError == null || !_lastSyncError!.contains('sanitization')) {
          _lastSyncError = 'Failed to persist cycle confirmation: ${e.message}';
        }
        notifyListeners();
        return false;
      }
      _lastSyncError = 'Failed to persist cycle confirmation: ${e.message}';
      notifyListeners();
      debugPrint('[VitalsSyncQueue] Error persisting cycle confirmation: $e');
      return false;
    } catch (e) {
      if (_sessionGeneration != requestEpoch ||
          !identical(_apiClient, client) ||
          _activePatientId != expectedPatientId) {
        return false;
      }
      _cycleStatus = CycleResolutionStatus.networkError;
      _lastSyncError = 'Unexpected error during cycle resolution: $e';
      notifyListeners();
      debugPrint('[VitalsSyncQueue] Unexpected error during cycle resolution: $e');
      return false;
    }
  }

  /// Set or update the active medical cycle ID.
  /// Binds unresolved pending events to this cycle without altering already bound events.
  Future<void> setActiveCycleId(String? cycleId) async {
    if (_initFuture != null) {
      try {
        await _initFuture;
      } catch (_) {}
    } else if (!_isInitialized) {
      try {
        await init();
      } catch (_) {}
    }

    return await _synchronized(() async {
      if (!_isInitialized) {
        throw VitalsQueuePersistenceException(
          'Cannot setActiveCycleId: VitalsSyncQueue is not initialized or storage snapshot restoration failed',
          _initError,
        );
      }
      await _resolveActiveCycleLocked(
        cycleId,
        confirmedByServer: (cycleId != null && cycleId.isNotEmpty),
        sessionEpoch: _sessionGeneration,
        client: _apiClient,
        expectedPatientId: _activePatientId,
      );
    });
  }

  Future<void> _resolveActiveCycleLocked(
    String? cycleId, {
    bool confirmedByServer = false,
    int? sessionEpoch,
    AegisApiClient? client,
    String? expectedPatientId,
  }) async {
    final cleanId = (cycleId != null && cycleId.isNotEmpty) ? cycleId : null;

    if (sessionEpoch != null && (_sessionGeneration != sessionEpoch || !identical(_apiClient, client))) {
      debugPrint('[VitalsSyncQueue] Aborting cycle lock mutation: session changed before entry');
      return;
    }
    if (expectedPatientId != null && _activePatientId != expectedPatientId) {
      debugPrint('[VitalsSyncQueue] Aborting cycle lock mutation: patient changed before entry');
      return;
    }

    final prevActiveCycleId = _activeCycleId;
    final prevPendingItems = List<VitalsQueueItem>.from(_pendingItems);
    final prevQuarantinedItems = List<VitalsQueueItem>.from(_quarantinedItems);
    final prevIsPaused = _isPaused;
    final prevPauseReason = _pauseReason;
    final prevCycleStatus = _cycleStatus;
    final prevIsConfirmed = _isCycleConfirmedByServer;

    final newlyBoundEventIds = <String>{};
    Map<String, VitalsQueueItem> prevItemsById = const {};

    bool modified = cleanId != _activeCycleId;

    try {
      if (_patientIsolationEnabled) {
        if (_quarantineLegacyUnassignedItemsLocked()) {
          modified = true;
        }
      }

      prevItemsById = {for (final item in _pendingItems) item.eventId: item};

      _activeCycleId = cleanId;

      if (cleanId != null) {
        _cycleStatus = CycleResolutionStatus.active;
        for (int i = 0; i < _pendingItems.length; i++) {
          final item = _pendingItems[i];
          // Only bind items that do NOT have a resolved cycle yet AND belong to the active patient
          final matchesPatient = !_patientIsolationEnabled ||
              (_activePatientId != null && item.patientId == _activePatientId);
          if (item.cycleId == null && matchesPatient) {
            newlyBoundEventIds.add(item.eventId);
            final newKey = 'idem_${cleanId}_${item.eventId}';
            final updatedDto = item.dto.copyWith(
              cycleId: cleanId,
              rawMetadata: {
                ...?item.dto.rawMetadata,
                'idempotency_key': newKey,
                'event_id': item.eventId,
              },
            );
            _pendingItems[i] = item.copyWith(
              cycleId: cleanId,
              idempotencyKey: newKey,
              dto: updatedDto,
            );
            modified = true;
          }
        }

        if (_isPaused && (_pauseReason == null ||
            _pauseReason!.toLowerCase().contains('cycle') ||
            _pauseReason!.toLowerCase().contains('authoriz') ||
            _pauseReason!.toLowerCase().contains('401') ||
            _pauseReason!.toLowerCase().contains('404'))) {
          _isPaused = false;
          _pauseReason = null;
          modified = true;
        }
      } else if (_cycleStatus == CycleResolutionStatus.active) {
        _cycleStatus = CycleResolutionStatus.idle;
      }

      if (sessionEpoch != null && (_sessionGeneration != sessionEpoch || !identical(_apiClient, client))) {
        debugPrint('[VitalsSyncQueue] Session changed before snapshot write. Aborting and rolling back.');
        if (_activeCycleId == cleanId) {
          _activeCycleId = null;
          _isCycleConfirmedByServer = false;
          _cycleStatus = CycleResolutionStatus.idle;
        }
        _rollbackTentativeCycleBindingsLocked(newlyBoundEventIds, prevItemsById);
        notifyListeners();
        return;
      }

      if (modified) {
        await _persist(sessionEpoch: sessionEpoch);
      }

      if (sessionEpoch != null && (_sessionGeneration != sessionEpoch || !identical(_apiClient, client))) {
        debugPrint('[VitalsSyncQueue] Session changed during snapshot persistence. Discarding stale confirmation and sanitizing snapshot.');
        if (_activeCycleId == cleanId) {
          _activeCycleId = null;
          _isCycleConfirmedByServer = false;
          _cycleStatus = CycleResolutionStatus.idle;
        }
        _rollbackTentativeCycleBindingsLocked(newlyBoundEventIds, prevItemsById);
        try {
          await _persist();
        } catch (persistErr) {
          final errMsg = 'Snapshot sanitization failed after session transition: $persistErr';
          debugPrint('[VitalsSyncQueue] $errMsg');
          _lastSyncError = errMsg;
          _isPaused = true;
          _pauseReason = errMsg;
          if (_activeCycleId == cleanId) {
            _activeCycleId = null;
            _isCycleConfirmedByServer = false;
          }
          notifyListeners();
          throw VitalsQueuePersistenceException(errMsg, persistErr);
        }
        notifyListeners();
        return;
      }

      if (confirmedByServer) {
        _isCycleConfirmedByServer = (cleanId != null);
      }

      notifyListeners();
    } catch (e) {
      if (sessionEpoch != null && (_sessionGeneration != sessionEpoch || !identical(_apiClient, client))) {
        debugPrint('[VitalsSyncQueue] Session changed during cycle resolution error. Preserving current session state.');
        if (_activeCycleId == cleanId) {
          _activeCycleId = null;
          _isCycleConfirmedByServer = false;
          _cycleStatus = CycleResolutionStatus.idle;
        }
        _rollbackTentativeCycleBindingsLocked(newlyBoundEventIds, prevItemsById);
        _lastSyncError ??= 'Cycle resolution failed during session transition: $e';
        _isPaused = true;
        _pauseReason ??= _lastSyncError;
        notifyListeners();
        rethrow;
      }
      _activeCycleId = prevActiveCycleId;
      _pendingItems
        ..clear()
        ..addAll(prevPendingItems);
      _quarantinedItems
        ..clear()
        ..addAll(prevQuarantinedItems);
      _isPaused = prevIsPaused;
      _pauseReason = prevPauseReason;
      _cycleStatus = prevCycleStatus;
      _isCycleConfirmedByServer = prevIsConfirmed;
      notifyListeners();
      rethrow;
    }
  }

  void _rollbackTentativeCycleBindingsLocked(
    Set<String> newlyBoundEventIds,
    Map<String, VitalsQueueItem> prevItemsById,
  ) {
    if (newlyBoundEventIds.isEmpty) return;
    for (int i = 0; i < _pendingItems.length; i++) {
      final currentItem = _pendingItems[i];
      if (newlyBoundEventIds.contains(currentItem.eventId)) {
        final original = prevItemsById[currentItem.eventId];
        if (original != null) {
          _pendingItems[i] = original;
        } else {
          final rawMeta = Map<String, dynamic>.from(currentItem.dto.rawMetadata ?? {});
          rawMeta.remove('idempotency_key');
          _pendingItems[i] = currentItem.copyWith(
            cycleId: null,
            idempotencyKey: null,
            dto: currentItem.dto.copyWith(
              cycleId: null,
              rawMetadata: rawMeta.isNotEmpty ? rawMeta : null,
            ),
          );
        }
      }
    }
  }

  void resume() {
    _isPaused = false;
    _pauseReason = null;
    _lastSyncError = null;
    _consecutiveFailures = 0;
    _nextRetryAllowedAfter = null;
    if (_cycleStatus == CycleResolutionStatus.authError ||
        _cycleStatus == CycleResolutionStatus.noActiveCycle ||
        _cycleStatus == CycleResolutionStatus.networkError) {
      _cycleStatus = (_activeCycleId != null && _isCycleConfirmedByServer) ? CycleResolutionStatus.active : CycleResolutionStatus.idle;
    }
    notifyListeners();
  }

  /// Load persisted queue snapshot from local storage under serialized lock.
  /// Automatically migrates from legacy dual keys if present, preserving old data until migration succeeds.
  Future<void> init() {
    if (_isInitialized) return Future.value();
    if (_initFuture != null) return _initFuture!;

    _initFuture = _synchronized(() async {
      if (_isInitialized) {
        debugPrint('[VitalsSyncQueue] Already initialized, skipping init()');
        return;
      }

      try {
        if (_authSessionManager != null && !_authSessionManager!.isInitialized) {
          try {
            await _authSessionManager!.initialize();
          } catch (_) {}
        }

        final prefs = await SharedPreferences.getInstance();

        bool restoredCycleFromDisk = false;
        bool needsSnapshotPersist = false;
        String? diskPatientId;

        final snapshotRaw = prefs.getString(_snapshotStorageKey);
        if (snapshotRaw != null && snapshotRaw.isNotEmpty) {
          final decoded = json.decode(snapshotRaw) as Map<String, dynamic>;
          final pendingList = (decoded['pending'] as List<dynamic>?) ?? [];
          final quarantineList = (decoded['quarantine'] as List<dynamic>?) ?? [];

          _pendingItems.clear();
          for (final item in pendingList) {
            final parsed = VitalsQueueItem.fromJson(item as Map<String, dynamic>);
            if (parsed.patientId != null && parsed.patientId!.isNotEmpty) {
              _patientIsolationEnabled = true;
            }
            _pendingItems.add(parsed);
          }

          _quarantinedItems.clear();
          for (final item in quarantineList) {
            final parsed = VitalsQueueItem.fromJson(item as Map<String, dynamic>);
            if (parsed.patientId != null && parsed.patientId!.isNotEmpty) {
              _patientIsolationEnabled = true;
            }
            _quarantinedItems.add(parsed);
          }

          if (decoded.containsKey('active_patient_id') && decoded['active_patient_id'] != null) {
            final savedPatientId = (decoded['active_patient_id'] as String).trim();
            if (savedPatientId.isNotEmpty) {
              diskPatientId = savedPatientId;
              _patientIsolationEnabled = true;
            }
          }

          if (decoded['active_cycle_id'] != null && _activeCycleId == null) {
            _activeCycleId = decoded['active_cycle_id'] as String?;
            restoredCycleFromDisk = true;
          }
          if (decoded['event_counter'] != null) {
            final savedCounter = decoded['event_counter'] as int;
            if (savedCounter > _eventCounter) {
              _eventCounter = savedCounter;
            }
          }
        } else {
          // Migration from legacy dual keys if single snapshot does not exist yet
          bool migrationNeeded = false;
          final pendingRaw = prefs.getString(_legacyPendingKey);
          if (pendingRaw != null && pendingRaw.isNotEmpty) {
            final decoded = json.decode(pendingRaw) as List<dynamic>;
            _pendingItems.clear();
            for (final item in decoded) {
              _pendingItems.add(VitalsQueueItem.fromJson(item as Map<String, dynamic>));
            }
            migrationNeeded = true;
          }

          final quarantineRaw = prefs.getString(_legacyQuarantineKey);
          if (quarantineRaw != null && quarantineRaw.isNotEmpty) {
            final decoded = json.decode(quarantineRaw) as List<dynamic>;
            _quarantinedItems.clear();
            for (final item in decoded) {
              _quarantinedItems.add(VitalsQueueItem.fromJson(item as Map<String, dynamic>));
            }
            migrationNeeded = true;
          }

          if (migrationNeeded) {
            debugPrint('[VitalsSyncQueue] Migrating legacy dual keys to single snapshot v2...');
            if (_patientIsolationEnabled) {
              _quarantineLegacyUnassignedItemsLocked();
            }
            await _persist();
            // Remove legacy keys ONLY after snapshot write succeeds
            await prefs.remove(_legacyPendingKey);
            await prefs.remove(_legacyQuarantineKey);
            debugPrint('[VitalsSyncQueue] Migration to single snapshot v2 complete.');
          }
        }

        // Reconcile disk patient identity with AuthSessionManager / activePatientId
        final authPatientId = _authSessionManager?.currentSession?.patientId ??
            _authSessionManager?.lastKnownPatientId;

        if (authPatientId != null && authPatientId.isNotEmpty) {
          _patientIsolationEnabled = true;
          if (diskPatientId != null && diskPatientId != authPatientId) {
            // Patient switched across restart
            _sessionGeneration++;
            _activeCycleId = null;
            _isCycleConfirmedByServer = false;
            _cycleStatus = CycleResolutionStatus.idle;
            restoredCycleFromDisk = false;
            needsSnapshotPersist = true;
          }
          if (_activePatientId != authPatientId) {
            _activePatientId = authPatientId;
            needsSnapshotPersist = true;
          }
        } else if (_activePatientId != null && _activePatientId!.isNotEmpty) {
          _patientIsolationEnabled = true;
          if (diskPatientId != null && diskPatientId != _activePatientId) {
            _sessionGeneration++;
            _activeCycleId = null;
            _isCycleConfirmedByServer = false;
            _cycleStatus = CycleResolutionStatus.idle;
            restoredCycleFromDisk = false;
            needsSnapshotPersist = true;
          }
        } else if (diskPatientId != null && diskPatientId.isNotEmpty) {
          _activePatientId = diskPatientId;
          _patientIsolationEnabled = true;
        }

        // Quarantine any legacy unassigned pending items when patient isolation is active
        if (_patientIsolationEnabled) {
          if (_quarantineLegacyUnassignedItemsLocked()) {
            needsSnapshotPersist = true;
          }
        }

        // If activeCycleId was restored from disk (and not explicitly confirmed via constructor),
        // it must remain unconfirmed until server validates the current session.
        if (restoredCycleFromDisk) {
          _isCycleConfirmedByServer = false;
          _cycleStatus = CycleResolutionStatus.idle;
        } else if (_activeCycleId != null && _isCycleConfirmedByServer) {
          await _resolveActiveCycleLocked(
            _activeCycleId,
            confirmedByServer: true,
            sessionEpoch: _sessionGeneration,
            client: _apiClient,
            expectedPatientId: _activePatientId,
          );
          needsSnapshotPersist = false;
        }

        if (needsSnapshotPersist) {
          await _persist();
        }

        _isInitialized = true;
        _lastSyncError = null;
        if (_authSessionManager != null) {
          _onAuthSessionChanged();
        } else {
          notifyListeners();
        }
        debugPrint('[VitalsSyncQueue] Initialized snapshot: ${_pendingItems.length} pending, ${_quarantinedItems.length} quarantined (patient: $_activePatientId)');
      } catch (e) {
        _isInitialized = false;
        _initError = e;
        _initFuture = null; // Allow retry on subsequent init() call
        debugPrint('[VitalsSyncQueue] Error loading snapshot from storage: $e');
        if (e is VitalsQueuePersistenceException) rethrow;
        throw VitalsQueuePersistenceException('Failed to initialize queue snapshot from storage: $e', e);
      }
    });

    return _initFuture!;
  }

  /// Atomic sequential persistence of single snapshot to local SharedPreferences.
  /// Stores pending, quarantine, activeCycleId, activePatientId, and technical metadata in a single key.
  Future<void> _persist({int? sessionEpoch}) async {
    if (sessionEpoch != null && _sessionGeneration != sessionEpoch) {
      debugPrint('[VitalsSyncQueue] Stale snapshot write aborted: session generation changed');
      return;
    }
    try {
      final prefs = await SharedPreferences.getInstance();
      final snapshot = {
        'version': 2,
        'saved_at': DateTime.now().toUtc().toIso8601String(),
        'active_cycle_id': _activeCycleId,
        if (_patientIsolationEnabled || _activePatientId != null)
          'active_patient_id': _activePatientId,
        'event_counter': _eventCounter,
        'pending': _pendingItems.map((i) => i.toJson()).toList(),
        'quarantine': _quarantinedItems.map((i) => i.toJson()).toList(),
      };
      final encoded = json.encode(snapshot);
      final ok = await prefs.setString(_snapshotStorageKey, encoded);

      if (!ok) {
        throw const VitalsQueuePersistenceException('SharedPreferences returned false on snapshot write');
      }
    } catch (e) {
      if (e is VitalsQueuePersistenceException) rethrow;
      throw VitalsQueuePersistenceException('Error persisting queue snapshot: $e', e);
    }
  }

  /// Enqueue an incoming HealthMeasurement with atomic single-snapshot persistence.
  /// If cycleId is unknown, the item is persisted locally without a fictitious fallback cycle.
  Future<VitalsQueueItem> enqueue(
    HealthMeasurement measurement, {
    String? cycleId,
    String? sessionId,
    String? phase,
  }) async {
    // If initialization was requested, ensure it completes before mutating queue
    if (_initFuture != null) {
      try {
        await _initFuture;
      } catch (_) {}
    } else if (!_isInitialized) {
      try {
        await init();
      } catch (_) {}
    }

    return await _synchronized(() async {
      if (!_isInitialized) {
        throw VitalsQueuePersistenceException(
          'Cannot enqueue measurement: VitalsSyncQueue is not initialized or storage snapshot restoration failed',
          _initError,
        );
      }

      if (_authSessionManager != null) {
        _patientIsolationEnabled = true;
        final sessionPatient = _authSessionManager!.currentSession?.patientId ??
            _authSessionManager!.lastKnownPatientId;
        if (_activePatientId == null && sessionPatient != null && sessionPatient.isNotEmpty) {
          _activePatientId = sessionPatient;
        }
      }

      final itemPatientId = _activePatientId;
      final shouldQuarantineUnbound = _patientIsolationEnabled &&
          (itemPatientId == null || itemPatientId.trim().isEmpty);

      final isCurrentlyAuthenticated = _authSessionManager == null ||
          _authSessionManager!.status == AuthSessionStatus.authenticated;

      final effectiveCycleId = shouldQuarantineUnbound
          ? null
          : ((cycleId != null && cycleId.isNotEmpty)
              ? cycleId
              : ((_isCycleConfirmedByServer && isCurrentlyAuthenticated) ? _activeCycleId : null));

      final nowMicro = DateTime.now().microsecondsSinceEpoch;
      _eventCounter++;
      final eventId = 'evt_${nowMicro}_$_eventCounter';

      final idempotencyKey = effectiveCycleId != null
          ? 'idem_${effectiveCycleId}_$eventId'
          : null;

      final metadata = Map<String, dynamic>.from(measurement.metadata);
      metadata['event_id'] = eventId;
      if (idempotencyKey != null) {
        metadata['idempotency_key'] = idempotencyKey;
      }
      if (itemPatientId != null && itemPatientId.isNotEmpty) {
        metadata['patient_id'] = itemPatientId;
      }

      final dto = HealthMeasurementCreate(
        cycleId: effectiveCycleId,
        measuredAt: measurement.timestamp.toUtc(),
        source: measurement.source.rawValue,
        pulse: measurement.pulse,
        systolic: measurement.systolic,
        diastolic: measurement.diastolic,
        spo2: measurement.spo2,
        sessionId: sessionId,
        isSimulated: measurement.isSimulated ||
            measurement.source == HealthDataSource.testChip ||
            measurement.source == HealthDataSource.simulated,
        isHistorical: measurement.isHistorical,
        phase: phase ?? (measurement.isHistorical ? 'historical' : 'standby'),
        note: measurement.note,
        rawMetadata: metadata.isNotEmpty ? metadata : null,
      );

      final queueItem = VitalsQueueItem(
        eventId: eventId,
        idempotencyKey: idempotencyKey,
        cycleId: effectiveCycleId,
        patientId: itemPatientId,
        dto: dto,
        enqueuedAt: DateTime.now(),
        isQuarantined: shouldQuarantineUnbound,
        lastError: shouldQuarantineUnbound
            ? 'Quarantined: recorded without active or prior patient_id binding'
            : null,
      );

      final prevPending = List<VitalsQueueItem>.from(_pendingItems);
      final prevQuarantined = List<VitalsQueueItem>.from(_quarantinedItems);

      if (shouldQuarantineUnbound) {
        _quarantinedItems.add(queueItem);
      } else {
        _pendingItems.add(queueItem);
      }

      // Atomically persist snapshot. If persistence fails, roll back in-memory addition!
      try {
        await _persist();
      } catch (e) {
        _pendingItems
          ..clear()
          ..addAll(prevPending);
        _quarantinedItems
          ..clear()
          ..addAll(prevQuarantined);
        rethrow;
      }

      notifyListeners();
      debugPrint(
        '[VitalsSyncQueue] Enqueued item $eventId (patient: $itemPatientId, cycle: $effectiveCycleId, quarantined: $shouldQuarantineUnbound). Pending: ${_pendingItems.length}',
      );
      return queueItem;
    });
  }

  /// Flush pending items sequentially in batches with deterministic 409 conflict isolation.
  Future<SyncResult> flush({int maxBatchSize = 50}) async {
    if (_initFuture != null) {
      try {
        await _initFuture;
      } catch (_) {}
    } else if (!_isInitialized) {
      try {
        await init();
      } catch (_) {}
    }

    return await _synchronized(() async {
      if (!_isInitialized) {
        final msg = 'Flush aborted: VitalsSyncQueue is not initialized (${_initError ?? "init() failed"})';
        debugPrint('[VitalsSyncQueue] $msg');
        _lastSyncError = msg;
        return SyncResult(
          status: SyncStatus.persistenceError,
          persistenceFailed: true,
          message: msg,
        );
      }

      if (_isFlushing) return SyncResult.idle;

      if (_patientIsolationEnabled) {
        final quarantinedLegacy = _quarantineLegacyUnassignedItemsLocked();
        if (quarantinedLegacy) {
          try {
            await _persist();
          } catch (persistErr) {
            final msg = 'Failed to persist legacy item quarantine before flush: $persistErr';
            _lastSyncError = msg;
            notifyListeners();
            return SyncResult(
              status: SyncStatus.persistenceError,
              persistenceFailed: true,
              message: msg,
            );
          }
          notifyListeners();
        }
      }

      if (_isPaused) {
        debugPrint('[VitalsSyncQueue] Flush skipped: Queue is paused ($_pauseReason)');
        return SyncResult(status: SyncStatus.paused, message: _pauseReason);
      }
      if (_pendingItems.isEmpty) return SyncResult.idle;

      if (_nextRetryAllowedAfter != null && DateTime.now().isBefore(_nextRetryAllowedAfter!)) {
        debugPrint('[VitalsSyncQueue] Flush delayed due to backoff until $_nextRetryAllowedAfter');
        return const SyncResult(status: SyncStatus.retryableError, message: 'Backoff active cooldown');
      }

      _isFlushing = true;
      _lastSyncAttemptAt = DateTime.now();
      notifyListeners();

      final flushEpoch = _sessionGeneration;
      final flushClient = _apiClient;
      final expectedPatientId = _activePatientId;
      List<VitalsQueueItem> batchItems = const [];

      try {
        // Verify active auth token before any cycle resolution or batch transmission
        String? verifiedToken = await _resolveCurrentAuthToken(flushClient);
        if (_sessionGeneration != flushEpoch || !identical(_apiClient, flushClient)) {
          return const SyncResult(
            status: SyncStatus.paused,
            message: 'Session changed during pre-flush authorization check',
          );
        }

        if (verifiedToken == null || verifiedToken.trim().isEmpty) {
          _isCycleConfirmedByServer = false;
          final manager = _authSessionManager;
          if (manager != null && manager.isRefreshIndeterminate) {
            _cycleStatus = CycleResolutionStatus.networkError;
            _pauseReason = 'Session refresh indeterminate: outbound sync paused';
            _lastSyncError = _pauseReason;
          } else if (manager != null && manager.status == AuthSessionStatus.expired) {
            _cycleStatus = CycleResolutionStatus.authError;
            _pauseReason = 'Session expired: awaiting re-authorization';
            _lastSyncError = _pauseReason;
          } else {
            _cycleStatus = CycleResolutionStatus.awaitingAuthorization;
            _pauseReason = 'Awaiting authorization: active medical cycle requires user session';
          }
          _isPaused = true;
          notifyListeners();
          return SyncResult(status: SyncStatus.paused, message: _pauseReason);
        }

        final jwtPatientId = AuthSessionManager.extractPatientIdFromJwt(verifiedToken);
        if (_patientIsolationEnabled) {
          if (jwtPatientId == null) {
            _isCycleConfirmedByServer = false;
            _cycleStatus = CycleResolutionStatus.authError;
            _isPaused = true;
            _pauseReason = 'Awaiting authorization: valid mobile JWT with patient sub claim is required';
            _lastSyncError = _pauseReason;
            notifyListeners();
            return SyncResult(status: SyncStatus.paused, message: _pauseReason);
          }
          if (_activePatientId == null) {
            _activePatientId = jwtPatientId;
            if (_quarantineLegacyUnassignedItemsLocked()) {
              await _persist(sessionEpoch: flushEpoch);
            }
          } else if (_activePatientId != jwtPatientId) {
            _isCycleConfirmedByServer = false;
            _cycleStatus = CycleResolutionStatus.authError;
            _isPaused = true;
            _pauseReason = 'Patient isolation mismatch between token and queue';
            _lastSyncError = _pauseReason;
            notifyListeners();
            return SyncResult(status: SyncStatus.paused, message: _pauseReason);
          }
        } else if (jwtPatientId != null) {
          _patientIsolationEnabled = true;
          _activePatientId = jwtPatientId;
          if (_quarantineLegacyUnassignedItemsLocked()) {
            await _persist(sessionEpoch: flushEpoch);
          }
        }

        if (_patientIsolationEnabled) {
          if (_quarantineLegacyUnassignedItemsLocked()) {
            await _persist(sessionEpoch: flushEpoch);
          }
          if (_activePatientId == null || _activePatientId!.isEmpty) {
            _isCycleConfirmedByServer = false;
            _cycleStatus = CycleResolutionStatus.awaitingAuthorization;
            _isPaused = true;
            _pauseReason = 'Awaiting authorization: patient identity required';
            notifyListeners();
            return SyncResult(status: SyncStatus.paused, message: _pauseReason);
          }
        }

        // If activeCycleId is unknown or unconfirmed, attempt cycle resolution via resolveActiveCycle()
        if (_activeCycleId == null || !_isCycleConfirmedByServer) {
          final resolved = await _resolveActiveCycleInternal(force: true, alreadyLocked: true);
          if (!resolved) {
            if (_lastSyncError != null && (_lastSyncError!.contains('VitalsQueuePersistenceException') ||
                _lastSyncError!.contains('Failed to persist cycle confirmation') ||
                _lastSyncError!.contains('sanitization') ||
                _lastSyncError!.contains('Sanitization'))) {
              return SyncResult(
                status: SyncStatus.persistenceError,
                persistenceFailed: true,
                message: _lastSyncError,
              );
            }
            switch (_cycleStatus) {
              case CycleResolutionStatus.awaitingAuthorization:
                return SyncResult(status: SyncStatus.paused, message: _pauseReason);
              case CycleResolutionStatus.authError:
                return SyncResult(status: SyncStatus.paused, message: _pauseReason);
              case CycleResolutionStatus.noActiveCycle:
                return SyncResult(status: SyncStatus.paused, message: _pauseReason);
              case CycleResolutionStatus.networkError:
                return SyncResult(status: SyncStatus.retryableError, message: _lastSyncError);
              default:
                return SyncResult(status: SyncStatus.paused, message: _pauseReason ?? 'Active medical cycle unconfirmed');
            }
          }
        }

        if (_sessionGeneration != flushEpoch || !identical(_apiClient, flushClient)) {
          return const SyncResult(
            status: SyncStatus.paused,
            message: 'Session changed during cycle resolution',
          );
        }

        // expectedPatientId captured at flush start

        // Bind any pending items for the active patient that were recorded while offline (cycleId == null)
        if (_activeCycleId != null && _isCycleConfirmedByServer) {
          final hasUnboundForActivePatient = _pendingItems.any(
            (item) =>
                item.cycleId == null &&
                (!_patientIsolationEnabled ||
                    (expectedPatientId != null && item.patientId == expectedPatientId)),
          );
          if (hasUnboundForActivePatient) {
            await _resolveActiveCycleLocked(
              _activeCycleId,
              confirmedByServer: true,
              sessionEpoch: flushEpoch,
              client: flushClient,
              expectedPatientId: expectedPatientId,
            );
          }
        }

        if (_sessionGeneration != flushEpoch ||
            !identical(_apiClient, flushClient) ||
            _activePatientId != expectedPatientId) {
          return const SyncResult(
            status: SyncStatus.paused,
            message: 'Session changed before batch selection',
          );
        }

        // Re-verify token in case cycle resolution refreshed it
        verifiedToken = await _resolveCurrentAuthToken(flushClient);
        final recheckPatientId = AuthSessionManager.extractPatientIdFromJwt(verifiedToken);
        if (verifiedToken == null ||
            verifiedToken.trim().isEmpty ||
            _sessionGeneration != flushEpoch ||
            !identical(_apiClient, flushClient) ||
            _activePatientId != expectedPatientId ||
            (_patientIsolationEnabled && (recheckPatientId == null || recheckPatientId != expectedPatientId))) {
          _isCycleConfirmedByServer = false;
          _isPaused = true;
          _pauseReason = (_patientIsolationEnabled && recheckPatientId != expectedPatientId)
              ? 'Patient isolation mismatch before batch dispatch'
              : 'Authorization lost before batch dispatch';
          _lastSyncError = _pauseReason;
          notifyListeners();
          return SyncResult(status: SyncStatus.paused, message: _pauseReason);
        }

        // Filter only items matching both the confirmed activeCycleId AND the active patientId
        final readyItems = _pendingItems.where((item) {
          if (item.cycleId == null || item.cycleId != _activeCycleId) return false;
          if (_patientIsolationEnabled) {
            if (expectedPatientId == null || expectedPatientId.isEmpty) return false;
            if (item.patientId == null || item.patientId != expectedPatientId) return false;
          }
          return true;
        }).toList();

        if (readyItems.isEmpty) {
          debugPrint('[VitalsSyncQueue] No items matching confirmed activeCycleId ($_activeCycleId) and patient ($expectedPatientId) available for flush');
          return const SyncResult(status: SyncStatus.idle, message: 'No items matching active medical cycle and patient');
        }

        final targetCycle = _activeCycleId!;
        batchItems = readyItems.take(maxBatchSize).toList();

        if (batchItems.isEmpty) {
          return SyncResult.idle;
        }

        // Execute transmission with deterministic conflict isolation and strict patient verification
        final result = await _sendBatchWithConflictIsolation(
          batchItems,
          targetCycle,
          sessionEpoch: flushEpoch,
          client: flushClient,
          expectedPatientId: expectedPatientId,
          accessToken: verifiedToken,
        );
        if (result.status == SyncStatus.persistenceError || result.persistenceFailed) {
          _lastSyncError = result.message;
        }
        return result;
      } on AegisAuthException catch (e) {
        if (_sessionGeneration != flushEpoch ||
            !identical(_apiClient, flushClient) ||
            _activePatientId != expectedPatientId) {
          debugPrint('[VitalsSyncQueue] Discarding 401 from stale session');
          return const SyncResult(
            status: SyncStatus.paused,
            message: 'Stale HTTP response discarded',
          );
        }
        _isCycleConfirmedByServer = false;
        _cycleStatus = CycleResolutionStatus.authError;
        _isPaused = true;
        _pauseReason = '401 Unauthorized: ${e.message}';
        _lastSyncError = _pauseReason;
        debugPrint('[VitalsSyncQueue] Paused on auth failure: $_pauseReason');
        notifyListeners();
        return SyncResult(status: SyncStatus.paused, message: _pauseReason);
      } on AegisForbiddenException catch (e) {
        if (_sessionGeneration != flushEpoch ||
            !identical(_apiClient, flushClient) ||
            _activePatientId != expectedPatientId) {
          debugPrint('[VitalsSyncQueue] Discarding 403 from stale session');
          return const SyncResult(
            status: SyncStatus.paused,
            message: 'Stale HTTP response discarded',
          );
        }
        _isCycleConfirmedByServer = false;
        _cycleStatus = CycleResolutionStatus.authError;
        _isPaused = true;
        _pauseReason = '403 Forbidden: ${e.message}';
        _lastSyncError = _pauseReason;
        debugPrint('[VitalsSyncQueue] Paused on forbidden access: $_pauseReason');
        notifyListeners();
        return SyncResult(status: SyncStatus.paused, message: _pauseReason);
      } on AegisCycleNotFoundException catch (e) {
        if (_sessionGeneration != flushEpoch ||
            !identical(_apiClient, flushClient) ||
            _activePatientId != expectedPatientId) {
          debugPrint('[VitalsSyncQueue] Discarding 404 from stale session');
          return const SyncResult(
            status: SyncStatus.paused,
            message: 'Stale HTTP response discarded',
          );
        }
        _isCycleConfirmedByServer = false;
        _cycleStatus = CycleResolutionStatus.noActiveCycle;
        _isPaused = true;
        _pauseReason = '404 Cycle Not Found: ${e.message}';
        _lastSyncError = _pauseReason;
        debugPrint('[VitalsSyncQueue] Paused on missing cycle: $_pauseReason');
        notifyListeners();
        return SyncResult(status: SyncStatus.paused, message: _pauseReason);
      } on AegisBadRequestException catch (e) {
        if (_sessionGeneration != flushEpoch ||
            !identical(_apiClient, flushClient) ||
            _activePatientId != expectedPatientId) {
          debugPrint('[VitalsSyncQueue] Discarding 400 from stale session');
          return const SyncResult(
            status: SyncStatus.paused,
            message: 'Stale HTTP response discarded',
          );
        }
        _isPaused = true;
        _pauseReason = '400 Bad Request: ${e.message}';
        _lastSyncError = _pauseReason;
        debugPrint('[VitalsSyncQueue] Paused on bad request: $_pauseReason');
        notifyListeners();
        return SyncResult(status: SyncStatus.paused, message: _pauseReason);
      } on AegisValidationException catch (e) {
        if (_sessionGeneration != flushEpoch ||
            !identical(_apiClient, flushClient) ||
            _activePatientId != expectedPatientId) {
          debugPrint('[VitalsSyncQueue] Discarding 422 from stale session');
          return const SyncResult(
            status: SyncStatus.paused,
            message: 'Stale HTTP response discarded',
          );
        }
        _isPaused = true;
        _pauseReason = '422 Validation Error: ${e.message}';
        _lastSyncError = _pauseReason;
        debugPrint('[VitalsSyncQueue] Paused on validation error: $_pauseReason');
        notifyListeners();
        return SyncResult(status: SyncStatus.paused, message: _pauseReason);
      } on AegisRateLimitException catch (e) {
        if (_sessionGeneration != flushEpoch ||
            !identical(_apiClient, flushClient) ||
            _activePatientId != expectedPatientId) {
          debugPrint('[VitalsSyncQueue] Discarding 429 from stale session');
          return const SyncResult(
            status: SyncStatus.paused,
            message: 'Stale HTTP response discarded',
          );
        }
        _lastSyncError = '429 Rate Limit: ${e.message}';
        if (e.retryAfter != null) {
          _nextRetryAllowedAfter = DateTime.now().add(e.retryAfter!);
        } else {
          _consecutiveFailures++;
          final delaySeconds = (2 << (_consecutiveFailures.clamp(0, 5))).clamp(5, 60);
          _nextRetryAllowedAfter = DateTime.now().add(Duration(seconds: delaySeconds));
        }
        for (int i = 0; i < _pendingItems.length; i++) {
          if (batchItems.any((b) => b.eventId == _pendingItems[i].eventId)) {
            _pendingItems[i] = _pendingItems[i].copyWith(
              retryCount: _pendingItems[i].retryCount + 1,
              lastError: _lastSyncError,
            );
          }
        }
        try {
          await _persist(sessionEpoch: flushEpoch);
        } catch (_) {}
        debugPrint('[VitalsSyncQueue] 429 Rate Limit: $_lastSyncError. Cooldown until $_nextRetryAllowedAfter');
        notifyListeners();
        return SyncResult(status: SyncStatus.retryableError, message: _lastSyncError);
      } on AegisServerException catch (e) {
        return await _handleRetryableError(
          batchItems,
          e.message,
          sessionEpoch: flushEpoch,
          client: flushClient,
          expectedPatientId: expectedPatientId,
        );
      } on AegisNetworkException catch (e) {
        return await _handleRetryableError(
          batchItems,
          e.message,
          sessionEpoch: flushEpoch,
          client: flushClient,
          expectedPatientId: expectedPatientId,
        );
      } on AegisTimeoutException catch (e) {
        return await _handleRetryableError(
          batchItems,
          e.message,
          sessionEpoch: flushEpoch,
          client: flushClient,
          expectedPatientId: expectedPatientId,
        );
      } finally {
        _isFlushing = false;
        notifyListeners();
      }
    });
  }

  /// Deterministic batch splitting algorithm to isolate 409 conflicting items.
  /// Uses single-snapshot persistence to guarantee atomic transitions between pending and quarantine.
  Future<SyncResult> _sendBatchWithConflictIsolation(
    List<VitalsQueueItem> items,
    String targetCycle, {
    required int sessionEpoch,
    required AegisApiClient client,
    required String? expectedPatientId,
    required String? accessToken,
  }) async {
    if (items.isEmpty) return SyncResult.idle;

    // Abort immediately if session or patient context changed before sending this batch/sub-batch
    if (_sessionGeneration != sessionEpoch ||
        !identical(_apiClient, client) ||
        _activePatientId != expectedPatientId) {
      debugPrint('[VitalsSyncQueue] Aborting batch send: session/patient switched before HTTP dispatch');
      return const SyncResult(
        status: SyncStatus.paused,
        processedCount: 0,
        message: 'Batch dispatch aborted due to session/patient switch',
      );
    }

    // Verify every item strictly belongs to expectedPatientId when patient isolation is enabled
    if (_patientIsolationEnabled) {
      if (expectedPatientId == null || expectedPatientId.isEmpty) {
        return const SyncResult(
          status: SyncStatus.paused,
          processedCount: 0,
          message: 'Batch dispatch aborted: missing expected patient_id',
        );
      }
      for (final item in items) {
        if (item.patientId != expectedPatientId) {
          return const SyncResult(
            status: SyncStatus.paused,
            processedCount: 0,
            message: 'Batch dispatch aborted: item patient_id mismatch',
          );
        }
      }
      final tokenSub = AuthSessionManager.extractPatientIdFromJwt(accessToken);
      if (tokenSub == null || tokenSub != expectedPatientId) {
        return const SyncResult(
          status: SyncStatus.paused,
          processedCount: 0,
          message: 'Batch dispatch aborted: valid mobile JWT sub matching item patient_id is required',
        );
      }
    }

    String? currentToken = accessToken;

    try {
      final batchDto = HealthMeasurementCreateBatch(
        cycleId: targetCycle,
        measurements: items.map((i) => i.dto).toList(),
      );

      HealthMeasurementBatchResponse response;
      try {
        response = await client.postVitalsBatch(batchDto, accessToken: currentToken);
      } on AegisAuthException catch (e) {
        if (_authSessionManager != null &&
            _sessionGeneration == sessionEpoch &&
            identical(_apiClient, client) &&
            _activePatientId == expectedPatientId) {
          final recovered = await _authSessionManager!.handleProtectedEndpointError(e);
          final updatedToken = await _resolveCurrentAuthToken(client);
          final updatedPatient = _authSessionManager!.currentSession?.patientId;
          final updatedSub = AuthSessionManager.extractPatientIdFromJwt(updatedToken);
          if (recovered &&
              updatedToken != null &&
              updatedToken.isNotEmpty &&
              _sessionGeneration == sessionEpoch &&
              identical(_apiClient, client) &&
              _activePatientId == expectedPatientId &&
              (expectedPatientId == null ||
                  (updatedPatient == expectedPatientId &&
                      (!_patientIsolationEnabled || updatedSub == expectedPatientId)))) {
            currentToken = updatedToken;
            response = await client.postVitalsBatch(batchDto, accessToken: currentToken);
          } else {
            rethrow;
          }
        } else {
          rethrow;
        }
      }

      final isStaleSession = _sessionGeneration != sessionEpoch ||
          !identical(_apiClient, client) ||
          _activePatientId != expectedPatientId;

      // Server successfully acknowledged delivery of `items`: remove those exact items from pending
      final prevPending = List<VitalsQueueItem>.from(_pendingItems);
      _pendingItems.removeWhere((item) => items.any((b) => b.eventId == item.eventId));
      if (!isStaleSession) {
        _consecutiveFailures = 0;
        _nextRetryAllowedAfter = null;
        _lastSyncError = null;
        _lastSyncSuccessAt = DateTime.now();
      }

      try {
        await _persist(sessionEpoch: isStaleSession ? null : sessionEpoch);
      } catch (persistErr) {
        debugPrint('[VitalsSyncQueue] Server acknowledged delivery, but snapshot persist failed: $persistErr');
        // Restore pending items in memory so they are not lost if process is killed or retried
        _pendingItems
          ..clear()
          ..addAll(prevPending);
        notifyListeners();
        return SyncResult(
          status: SyncStatus.persistenceError,
          persistenceFailed: true,
          processedCount: items.length,
          message: 'Server acknowledged delivery (${items.length} items), but snapshot persist failed: $persistErr',
        );
      }
      notifyListeners();

      if (isStaleSession) {
        debugPrint('[VitalsSyncQueue] Successfully flushed ${items.length} items for prior session. Preserving active session state.');
        return SyncResult(
          status: SyncStatus.paused,
          processedCount: items.length,
          message: 'Successfully flushed ${items.length} items for prior session',
        );
      }

      debugPrint('[VitalsSyncQueue] Successfully flushed ${items.length} items (recorded: ${response.recordedCount}, deduplicated: ${response.deduplicatedCount}). Pending: ${_pendingItems.length}');

      return SyncResult(
        status: SyncStatus.success,
        processedCount: items.length,
        message: 'Successfully synchronized ${items.length} items',
      );
    } on AegisConflictException catch (e) {
      return await _isolateFailedItemOrSplit(
        items,
        targetCycle,
        sessionEpoch: sessionEpoch,
        client: client,
        expectedPatientId: expectedPatientId,
        accessToken: currentToken,
        isConflict: true,
        errorMessage: e.message,
      );
    } on AegisValidationException catch (e) {
      return await _isolateFailedItemOrSplit(
        items,
        targetCycle,
        sessionEpoch: sessionEpoch,
        client: client,
        expectedPatientId: expectedPatientId,
        accessToken: currentToken,
        isConflict: false,
        errorMessage: e.message,
      );
    }
  }

  /// Deterministic batch splitting algorithm to isolate 409 conflict and 422 validation failures.
  Future<SyncResult> _isolateFailedItemOrSplit(
    List<VitalsQueueItem> items,
    String targetCycle, {
    required int sessionEpoch,
    required AegisApiClient client,
    required String? expectedPatientId,
    required String? accessToken,
    required bool isConflict,
    required String errorMessage,
  }) async {
    final errorLabel = isConflict ? '409 Conflict' : '422 Validation';

    if (items.length == 1) {
      final failedItem = items.first;
      debugPrint('[VitalsSyncQueue] $errorLabel isolated to single item ${failedItem.eventId}. Quarantining.');

      final prevPending = List<VitalsQueueItem>.from(_pendingItems);
      final prevQuarantine = List<VitalsQueueItem>.from(_quarantinedItems);

      _quarantinedItems.add(failedItem.copyWith(
        isQuarantined: true,
        lastError: '$errorLabel: $errorMessage',
      ));
      _pendingItems.removeWhere((item) => item.eventId == failedItem.eventId);

      try {
        await _persist(sessionEpoch: sessionEpoch);
      } catch (persistErr) {
        debugPrint('[VitalsSyncQueue] Failed to persist quarantine snapshot on $errorLabel: $persistErr');
        _pendingItems
          ..clear()
          ..addAll(prevPending);
        _quarantinedItems
          ..clear()
          ..addAll(prevQuarantine);
        notifyListeners();
        return SyncResult(
          status: SyncStatus.persistenceError,
          persistenceFailed: true,
          processedCount: 0,
          message: '$errorLabel isolated for ${failedItem.eventId}, but quarantine persistence failed: $persistErr',
        );
      }

      notifyListeners();

      return SyncResult(
        status: SyncStatus.conflict,
        processedCount: 1,
        message: '$errorLabel isolated for item ${failedItem.eventId}: $errorMessage',
      );
    }

    // Before splitting into sub-batches, ensure session/patient has not switched during the failed batch call!
    if (_sessionGeneration != sessionEpoch ||
        !identical(_apiClient, client) ||
        _activePatientId != expectedPatientId) {
      debugPrint('[VitalsSyncQueue] $errorLabel batch split aborted: session/patient switched during batch request');
      return const SyncResult(
        status: SyncStatus.paused,
        processedCount: 0,
        message: 'Batch split aborted due to session/patient switch',
      );
    }

    // If batch had multiple items, split deterministically into smaller chunks
    debugPrint('[VitalsSyncQueue] Batch of ${items.length} items returned $errorLabel. Splitting to isolate failure.');
    final mid = items.length ~/ 2;
    final leftGroup = items.sublist(0, mid);
    final rightGroup = items.sublist(mid);

    final leftResult = await _sendBatchWithConflictIsolation(
      leftGroup,
      targetCycle,
      sessionEpoch: sessionEpoch,
      client: client,
      expectedPatientId: expectedPatientId,
      accessToken: accessToken,
    );

    // If session/patient switched during leftGroup, do not send rightGroup!
    if (_sessionGeneration != sessionEpoch ||
        !identical(_apiClient, client) ||
        _activePatientId != expectedPatientId) {
      debugPrint('[VitalsSyncQueue] Aborting right sub-batch after session/patient switch on $errorLabel');
      return SyncResult(
        status: leftResult.persistenceFailed ? SyncStatus.persistenceError : SyncStatus.paused,
        persistenceFailed: leftResult.persistenceFailed,
        processedCount: leftResult.processedCount,
        message: 'Batch split paused after left sub-batch due to session/patient switch',
      );
    }

    final rightResult = await _sendBatchWithConflictIsolation(
      rightGroup,
      targetCycle,
      sessionEpoch: sessionEpoch,
      client: client,
      expectedPatientId: expectedPatientId,
      accessToken: accessToken,
    );

    final totalProcessed = leftResult.processedCount + rightResult.processedCount;

    if (leftResult.persistenceFailed || rightResult.persistenceFailed) {
      return SyncResult(
        status: SyncStatus.persistenceError,
        persistenceFailed: true,
        processedCount: totalProcessed,
        message: 'Batch split processed items, but snapshot persistence failed in sub-batch',
      );
    }

    if (leftResult.status == SyncStatus.paused || rightResult.status == SyncStatus.paused) {
      return SyncResult(
        status: SyncStatus.paused,
        processedCount: totalProcessed,
        message: 'Batch split paused due to session/patient change',
      );
    }

    if (leftResult.status == SyncStatus.conflict || rightResult.status == SyncStatus.conflict) {
      return SyncResult(
        status: SyncStatus.conflict,
        processedCount: totalProcessed,
        message: 'Batch split successfully isolated issues and delivered healthy measurements',
      );
    }

    return SyncResult(
      status: SyncStatus.success,
      processedCount: totalProcessed,
      message: 'Batch split delivered all remaining items',
    );
  }

  Future<SyncResult> _handleRetryableError(
    List<VitalsQueueItem> batchItems,
    String errorMsg, {
    int? sessionEpoch,
    AegisApiClient? client,
    String? expectedPatientId,
  }) async {
    final isStale = (sessionEpoch != null && _sessionGeneration != sessionEpoch) ||
        (client != null && !identical(_apiClient, client)) ||
        (expectedPatientId != null && _activePatientId != expectedPatientId);

    if (isStale) {
      debugPrint('[VitalsSyncQueue] Discarding retryable error from stale session: $errorMsg');
      for (int i = 0; i < _pendingItems.length; i++) {
        if (batchItems.any((b) => b.eventId == _pendingItems[i].eventId)) {
          _pendingItems[i] = _pendingItems[i].copyWith(
            retryCount: _pendingItems[i].retryCount + 1,
            lastError: errorMsg,
          );
        }
      }
      return const SyncResult(
        status: SyncStatus.paused,
        message: 'Stale HTTP response discarded',
      );
    }
    final prevPending = List<VitalsQueueItem>.from(_pendingItems);
    final prevConsecutiveFailures = _consecutiveFailures;
    final prevNextRetryAllowedAfter = _nextRetryAllowedAfter;

    _lastSyncError = errorMsg;
    _consecutiveFailures++;
    final delaySeconds = (2 << (_consecutiveFailures.clamp(0, 5))).clamp(2, 60);
    _nextRetryAllowedAfter = DateTime.now().add(Duration(seconds: delaySeconds));

    for (int i = 0; i < _pendingItems.length; i++) {
      if (batchItems.any((b) => b.eventId == _pendingItems[i].eventId)) {
        _pendingItems[i] = _pendingItems[i].copyWith(
          retryCount: _pendingItems[i].retryCount + 1,
          lastError: errorMsg,
        );
      }
    }

    try {
      await _persist();
    } catch (persistErr) {
      debugPrint('[VitalsSyncQueue] Failed to persist retry state snapshot: $persistErr');
      _pendingItems
        ..clear()
        ..addAll(prevPending);
      _consecutiveFailures = prevConsecutiveFailures;
      _nextRetryAllowedAfter = prevNextRetryAllowedAfter;
      notifyListeners();
      return SyncResult(
        status: SyncStatus.persistenceError,
        persistenceFailed: true,
        processedCount: 0,
        message: 'Retryable error ($errorMsg), but snapshot persistence failed: $persistErr',
      );
    }

    debugPrint('[VitalsSyncQueue] Retryable Error: $errorMsg. Retry #$_consecutiveFailures scheduled in ${delaySeconds}s.');
    notifyListeners();
    return SyncResult(status: SyncStatus.retryableError, message: errorMsg);
  }

  /// Safely executes flush with comprehensive error handling to prevent unhandled asynchronous exceptions in timers.
  Future<SyncResult> _safeFlush() async {
    try {
      if (!_isInitialized || _isPaused || _isFlushing || _pendingItems.isEmpty) {
        return SyncResult.idle;
      }
      final result = await flush();
      if (result.status == SyncStatus.persistenceError || result.persistenceFailed) {
        _lastSyncError = result.message;
        debugPrint('[VitalsSyncQueue] Auto-flush persistence error: ${result.message}');
      }
      return result;
    } catch (e) {
      final errorMsg = 'Auto-flush unexpected error: $e';
      _lastSyncError = errorMsg;
      debugPrint('[VitalsSyncQueue] $errorMsg');
      return SyncResult(
        status: SyncStatus.persistenceError,
        persistenceFailed: true,
        message: errorMsg,
      );
    }
  }

  /// Schedules a debounced flush if the queue is initialized, not paused, and not currently flushing.
  void scheduleFlush({Duration delay = const Duration(milliseconds: 1000)}) {
    _debouncedFlushTimer?.cancel();
    if (!_isInitialized || _isPaused || _isFlushing) return;
    _debouncedFlushTimer = Timer(delay, () {
      _debouncedFlushTimer = null;
      _safeFlush();
    });
  }

  /// Cancels any scheduled debounced flush
  void cancelScheduledFlush() {
    _debouncedFlushTimer?.cancel();
    _debouncedFlushTimer = null;
  }

  /// Starts periodic background/runtime sync while app is active
  void startPeriodicSync({Duration interval = const Duration(seconds: 30)}) {
    _periodicSyncTimer?.cancel();
    _periodicSyncTimer = Timer.periodic(interval, (_) {
      _safeFlush();
    });
  }

  /// Stops periodic sync
  void stopPeriodicSync() {
    _periodicSyncTimer?.cancel();
    _periodicSyncTimer = null;
  }

  @override
  void dispose() {
    _authSessionManager?.removeListener(_onAuthSessionChanged);
    _apiClient.setSessionManagerListener(null);
    cancelScheduledFlush();
    stopPeriodicSync();
    super.dispose();
  }

  /// Clear all queue data (for test isolation)
  Future<void> clearAll() => _synchronized(() async {
    cancelScheduledFlush();
    _pendingItems.clear();
    _quarantinedItems.clear();
    _isPaused = false;
    _pauseReason = null;
    _lastSyncError = null;
    _consecutiveFailures = 0;
    _nextRetryAllowedAfter = null;
    _activeCycleId = null;
    _isCycleConfirmedByServer = false;
    _cycleStatus = CycleResolutionStatus.idle;
    _isInitialized = true;
    _initError = null;
    await _persist();
    final prefs = await SharedPreferences.getInstance();
    await prefs.remove(_legacyPendingKey);
    await prefs.remove(_legacyQuarantineKey);
    notifyListeners();
  });
}
