import 'dart:async';
import 'dart:convert';
import 'dart:io';
import 'package:flutter/foundation.dart';
import 'package:http/http.dart' as http;
import '../auth/auth_models.dart';
import 'medical_ai_api_models.dart';
import 'vitals_api_models.dart';

typedef TokenProvider = Future<String?> Function();

class AegisApiClient {
  final String baseUrl;
  TokenProvider? _tokenProvider;
  Object? _attachedSessionManager;
  void Function(Object? manager)? _onSessionManagerChanged;
  final Duration timeout;
  final http.Client _client;

  AegisApiClient({
    String? baseUrl,
    TokenProvider? tokenProvider,
    this.timeout = const Duration(seconds: 10),
    http.Client? client,
  })  : baseUrl = (baseUrl ?? _defaultBaseUrl()).replaceAll(RegExp(r'/+$'), ''),
        _tokenProvider = tokenProvider,
        _client = client ?? http.Client();

  TokenProvider? get tokenProvider => _tokenProvider;

  /// Returns the currently attached [AuthSessionManager] instance, if any.
  Object? get attachedSessionManager => _attachedSessionManager;

  /// Binds or updates the [TokenProvider] delegate (e.g., `authSessionManager.getValidAccessToken`).
  void bindTokenProvider(TokenProvider? provider) {
    _tokenProvider = provider;
  }

  /// Attaches an [AuthSessionManager] to this client for automatic discovery by dependent queues.
  void attachSessionManager(Object? manager) {
    _attachedSessionManager = manager;
    _onSessionManagerChanged?.call(manager);
  }

  /// Registers a listener notified when an [AuthSessionManager] is attached or detached.
  void setSessionManagerListener(void Function(Object? manager)? listener) {
    _onSessionManagerChanged = listener;
  }

  static String _defaultBaseUrl() {
    // Android emulator loopback alias is 10.0.2.2; desktop / iOS / tests use 127.0.0.1
    if (!kIsWeb && Platform.isAndroid) {
      return 'http://10.0.2.2:8000';
    }
    return 'http://127.0.0.1:8000';
  }

  Map<String, String> _buildJsonHeaders() {
    return <String, String>{
      'Content-Type': 'application/json',
      'Accept': 'application/json',
    };
  }

  Future<Map<String, String>> _buildHeaders({String? accessToken}) async {
    final headers = _buildJsonHeaders();
    if (accessToken != null && accessToken.trim().isNotEmpty) {
      headers['Authorization'] = 'Bearer ${accessToken.trim()}';
    } else if (_tokenProvider != null) {
      final token = await _tokenProvider!();
      if (token != null && token.trim().isNotEmpty) {
        headers['Authorization'] = 'Bearer ${token.trim()}';
      }
    }
    return headers;
  }

  /// Returns whether a valid authorization token is currently provided.
  /// If a tokenProvider is configured, verifies it resolves to a non-empty token string.
  /// If tokenProvider is omitted (null), defaults to true for test mock compatibility.
  Future<bool> hasAuthToken() async {
    if (_tokenProvider == null) return true;
    try {
      final token = await _tokenProvider!();
      return token != null && token.trim().isNotEmpty;
    } catch (_) {
      return false;
    }
  }

  /// Resolves the current authorization token string, or null if unauthenticated.
  Future<String?> getAuthToken() async {
    if (_tokenProvider == null) return null;
    try {
      final token = await _tokenProvider!();
      return (token != null && token.trim().isNotEmpty) ? token.trim() : null;
    } catch (_) {
      return null;
    }
  }

  /// Redacts sensitive tokens, JWTs, refresh tokens, and pairing codes from log strings.
  static String sanitizeLogMessage(String message) {
    var sanitized = message.replaceAll(
      RegExp(r'Bearer\s+[^\s",}\])]+', caseSensitive: false),
      'Bearer [REDACTED]',
    );
    sanitized = sanitized.replaceAll(
      RegExp(r'eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+'),
      '[REDACTED_JWT]',
    );
    sanitized = sanitized.replaceAll(
      RegExp(r'aegis_rt_[^\s",}\])]+'),
      '[REDACTED_RT]',
    );
    sanitized = sanitized.replaceAllMapped(
      RegExp(
        r'(["\x27]?(?:access_token|refresh_token|pairing_code|accessToken|refreshToken|pairingCode)["\x27]?\s*[:=]\s*["\x27]?)([^"\x27,\s}\])]+)(["\x27]?)',
        caseSensitive: false,
      ),
      (m) => '${m.group(1)}[REDACTED]${m.group(3)}',
    );
    return sanitized;
  }

  void _log(String message) {
    final sanitized = sanitizeLogMessage(message);
    debugPrint('[AegisApiClient] $sanitized');
  }

  Duration? _extractRetryAfter(Map<String, String> headers) {
    String? rawValue;
    for (final entry in headers.entries) {
      if (entry.key.toLowerCase() == 'retry-after') {
        rawValue = entry.value.trim();
        break;
      }
    }
    if (rawValue == null || rawValue.isEmpty) return null;
    final seconds = int.tryParse(rawValue);
    if (seconds != null && seconds >= 0) {
      return Duration(seconds: seconds);
    }
    return null;
  }

  Never _handleErrorResponse(http.Response response) {
    String detail = 'HTTP error ${response.statusCode}';
    try {
      final decoded = json.decode(response.body);
      if (decoded is Map && decoded.containsKey('detail')) {
        detail = decoded['detail'].toString();
      }
    } catch (_) {
      detail = response.body.isNotEmpty ? response.body : detail;
    }

    final safeDetail = sanitizeLogMessage(detail);
    final safeBody = sanitizeLogMessage(response.body);
    _log('Error ${response.statusCode}: $safeDetail');

    switch (response.statusCode) {
      case 400:
        throw AegisBadRequestException(safeDetail, statusCode: 400, responseBody: safeBody);
      case 401:
        throw AegisAuthException(safeDetail, statusCode: 401, responseBody: safeBody);
      case 403:
        if (safeDetail.toLowerCase().contains('scope')) {
          final scopeMatch = RegExp(
            r'(?:Missing required scope|insufficient scope)[:\s]+([a-zA-Z0-9_:-]+)',
            caseSensitive: false,
          ).firstMatch(safeDetail);
          throw AegisInsufficientScopeException(
            safeDetail,
            statusCode: 403,
            responseBody: safeBody,
            requiredScope: scopeMatch?.group(1),
          );
        }
        throw AegisForbiddenException(safeDetail, statusCode: 403, responseBody: safeBody);
      case 404:
        throw AegisCycleNotFoundException(safeDetail, statusCode: 404, responseBody: safeBody);
      case 409:
        throw AegisConflictException(safeDetail, statusCode: 409, responseBody: safeBody);
      case 422:
        throw AegisValidationException(safeDetail, statusCode: 422, responseBody: safeBody);
      case 429:
        throw AegisRateLimitException(
          safeDetail,
          statusCode: 429,
          responseBody: safeBody,
          retryAfter: _extractRetryAfter(response.headers),
        );
      case 503:
        throw AegisServiceUnavailableException(safeDetail, statusCode: 503, responseBody: safeBody);
      default:
        if (response.statusCode >= 500) {
          throw AegisServerException(
            safeDetail,
            statusCode: response.statusCode,
            responseBody: safeBody,
          );
        }
        throw AegisNetworkException(
          safeDetail,
          statusCode: response.statusCode,
          responseBody: safeBody,
        );
    }
  }

  /// Sends an authenticated request with automatic single token refresh retry on HTTP 401.
  /// Strictly retries at most once. Never retries if an explicit access token was provided.
  Future<http.Response> _sendWithAuthRetry({
    required Future<http.Response> Function(String? token) sendRequest,
    String? explicitAccessToken,
  }) async {
    String? token = explicitAccessToken;
    if (token == null && _tokenProvider != null) {
      token = await _tokenProvider!();
    }

    var response = await sendRequest(token);

    if (response.statusCode == 401 && explicitAccessToken == null) {
      _log('Received 401 Unauthorized. Attempting single token refresh...');
      String? refreshedToken;
      try {
        if (_attachedSessionManager != null) {
          final dynamic mgr = _attachedSessionManager;
          refreshedToken = await mgr.refreshSession();
        } else if (_tokenProvider != null) {
          refreshedToken = await _tokenProvider!();
        }
      } catch (e) {
        _log('Token refresh attempt failed: $e');
      }

      if (refreshedToken != null && refreshedToken.isNotEmpty) {
        _log('Retrying request with refreshed access token...');
        response = await sendRequest(refreshedToken);
      }
    }

    return response;
  }

  /// Atomically pair a mobile device using a single-use 6-digit pairing code (`POST /api/v1/auth/pair`).
  /// Does not use `tokenProvider` and never logs `pairingCode` or returned tokens.
  Future<DevicePairResponse> pairDevice(DevicePairRequest request) async {
    final uri = Uri.parse('$baseUrl/api/v1/auth/pair');
    final payloadJson = json.encode(request.toJson());
    try {
      final headers = _buildJsonHeaders();
      _log('POST $uri (deviceName: ${request.deviceName}, platform: ${request.platform})');
      final response = await _client.post(uri, headers: headers, body: payloadJson).timeout(timeout);

      if (response.statusCode == 200) {
        final data = json.decode(response.body) as Map<String, dynamic>;
        return DevicePairResponse.fromJson(data);
      }
      _handleErrorResponse(response);
    } on TimeoutException catch (_) {
      _log('Timeout on $uri');
      throw AegisTimeoutException('Device pairing timed out after ${timeout.inSeconds}s');
    } on SocketException catch (e) {
      _log('Socket error on $uri: ${e.message}');
      throw AegisNetworkException('Network connection failed: ${e.message}');
    } on http.ClientException catch (e) {
      _log('Client error on $uri: ${e.message}');
      throw AegisNetworkException('HTTP client error: ${e.message}');
    }
  }

  /// Atomically rotate a single-use refresh token and obtain a new JWT access token (`POST /api/v1/auth/refresh`).
  /// Strictly executes at most once per call without automatic retries to prevent Token Reuse Detection.
  Future<TokenRefreshResponse> refreshTokens(TokenRefreshRequest request) async {
    final uri = Uri.parse('$baseUrl/api/v1/auth/refresh');
    final payloadJson = json.encode(request.toJson());
    try {
      final headers = _buildJsonHeaders();
      _log('POST $uri (deviceId: ${request.deviceId})');
      final response = await _client.post(uri, headers: headers, body: payloadJson).timeout(timeout);

      if (response.statusCode == 200) {
        final data = json.decode(response.body) as Map<String, dynamic>;
        return TokenRefreshResponse.fromJson(data);
      }
      _handleErrorResponse(response);
    } on TimeoutException catch (_) {
      _log('Timeout on $uri');
      throw AegisTimeoutException('Token refresh timed out after ${timeout.inSeconds}s');
    } on SocketException catch (e) {
      _log('Socket error on $uri: ${e.message}');
      throw AegisNetworkException('Network connection failed: ${e.message}');
    } on http.ClientException catch (e) {
      _log('Client error on $uri: ${e.message}');
      throw AegisNetworkException('HTTP client error: ${e.message}');
    }
  }

  /// Revoke the authenticated device's own session (`POST /api/v1/auth/revoke`).
  /// Accepts an explicit [accessToken] and/or a [DeviceRevokeRequest] payload with `(device_id, refresh_token)`.
  Future<DeviceRevokeResponse> revokeDevice(
    DeviceRevokeRequest request, {
    String? accessToken,
  }) async {
    final uri = Uri.parse('$baseUrl/api/v1/auth/revoke');
    final bodyMap = request.toJson();
    final payloadJson = bodyMap.isNotEmpty ? json.encode(bodyMap) : null;
    try {
      final headers = _buildJsonHeaders();
      if (accessToken != null && accessToken.trim().isNotEmpty) {
        headers['Authorization'] = 'Bearer ${accessToken.trim()}';
      } else if (tokenProvider != null &&
          (request.refreshToken == null || request.refreshToken!.trim().isEmpty)) {
        final providedToken = await tokenProvider!();
        if (providedToken != null && providedToken.trim().isNotEmpty) {
          headers['Authorization'] = 'Bearer ${providedToken.trim()}';
        }
      }

      _log('POST $uri (deviceId: ${request.deviceId ?? "current"})');
      final response = await _client
          .post(uri, headers: headers, body: payloadJson)
          .timeout(timeout);

      if (response.statusCode == 200) {
        final data = json.decode(response.body) as Map<String, dynamic>;
        return DeviceRevokeResponse.fromJson(data);
      }
      _handleErrorResponse(response);
    } on TimeoutException catch (_) {
      _log('Timeout on $uri');
      throw AegisTimeoutException('Device revocation timed out after ${timeout.inSeconds}s');
    } on SocketException catch (e) {
      _log('Socket error on $uri: ${e.message}');
      throw AegisNetworkException('Network connection failed: ${e.message}');
    } on http.ClientException catch (e) {
      _log('Client error on $uri: ${e.message}');
      throw AegisNetworkException('HTTP client error: ${e.message}');
    }
  }

  /// Query current active medical cycle
  Future<MedicalCycleResponse> getCurrentCycle({String? accessToken}) async {
    final uri = Uri.parse('$baseUrl/api/v1/cycle/current');
    try {
      final response = await _sendWithAuthRetry(
        sendRequest: (token) async {
          final headers = await _buildHeaders(accessToken: token);
          _log('GET $uri');
          return await _client.get(uri, headers: headers).timeout(timeout);
        },
        explicitAccessToken: accessToken,
      );

      if (response.statusCode == 200) {
        final data = json.decode(response.body) as Map<String, dynamic>;
        return MedicalCycleResponse.fromJson(data);
      }
      _handleErrorResponse(response);
    } on TimeoutException catch (e) {
      _log('Timeout on $uri: $e');
      throw AegisTimeoutException('Request timed out after ${timeout.inSeconds}s');
    } on SocketException catch (e) {
      _log('Socket error on $uri: $e');
      throw AegisNetworkException('Network connection failed: ${e.message}');
    } on http.ClientException catch (e) {
      _log('Client error on $uri: $e');
      throw AegisNetworkException('HTTP client error: ${e.message}');
    }
  }

  /// Atomically ingest a batch of measurements
  Future<HealthMeasurementBatchResponse> postVitalsBatch(
    HealthMeasurementCreateBatch batch, {
    String? accessToken,
  }) async {
    final uri = Uri.parse('$baseUrl/api/v1/vitals/batch');
    final payloadJson = json.encode(batch.toJson());
    try {
      final response = await _sendWithAuthRetry(
        sendRequest: (token) async {
          final headers = await _buildHeaders(accessToken: token);
          _log('POST $uri (${batch.measurements.length} items for cycle ${batch.cycleId})');
          return await _client.post(uri, headers: headers, body: payloadJson).timeout(timeout);
        },
        explicitAccessToken: accessToken,
      );

      if (response.statusCode == 200 || response.statusCode == 201) {
        final data = json.decode(response.body) as Map<String, dynamic>;
        return HealthMeasurementBatchResponse.fromJson(data);
      }
      _handleErrorResponse(response);
    } on TimeoutException catch (e) {
      _log('Timeout on $uri: $e');
      throw AegisTimeoutException('Batch ingestion timed out after ${timeout.inSeconds}s');
    } on SocketException catch (e) {
      _log('Socket error on $uri: $e');
      throw AegisNetworkException('Network connection failed: ${e.message}');
    } on http.ClientException catch (e) {
      _log('Client error on $uri: $e');
      throw AegisNetworkException('HTTP client error: ${e.message}');
    }
  }

  /// Ingest single measurement
  Future<HealthMeasurementResponse> postSingleMeasurement(
    HealthMeasurementCreate measurement, {
    String? accessToken,
  }) async {
    final uri = Uri.parse('$baseUrl/api/v1/vitals/measurement');
    final payloadJson = json.encode(measurement.toJson());
    try {
      final response = await _sendWithAuthRetry(
        sendRequest: (token) async {
          final headers = await _buildHeaders(accessToken: token);
          _log('POST $uri (pulse: ${measurement.pulse}, source: ${measurement.source})');
          return await _client.post(uri, headers: headers, body: payloadJson).timeout(timeout);
        },
        explicitAccessToken: accessToken,
      );

      if (response.statusCode == 200 || response.statusCode == 201) {
        final data = json.decode(response.body) as Map<String, dynamic>;
        return HealthMeasurementResponse.fromJson(data);
      }
      _handleErrorResponse(response);
    } on TimeoutException catch (e) {
      _log('Timeout on $uri: $e');
      throw AegisTimeoutException('Measurement ingestion timed out after ${timeout.inSeconds}s');
    } on SocketException catch (e) {
      _log('Socket error on $uri: $e');
      throw AegisNetworkException('Network connection failed: ${e.message}');
    } on http.ClientException catch (e) {
      _log('Client error on $uri: $e');
      throw AegisNetworkException('HTTP client error: ${e.message}');
    }
  }

  /// Get authoritative latest vitals stream
  Future<VitalsStreamResponse> getLatestVitals(
    String cycleId, {
    bool includeSimulated = false,
    String? accessToken,
  }) async {
    final uri = Uri.parse('$baseUrl/api/v1/vitals/latest').replace(queryParameters: {
      'cycle_id': cycleId,
      if (includeSimulated) 'include_simulated': 'true',
    });
    try {
      final response = await _sendWithAuthRetry(
        sendRequest: (token) async {
          final headers = await _buildHeaders(accessToken: token);
          _log('GET $uri');
          return await _client.get(uri, headers: headers).timeout(timeout);
        },
        explicitAccessToken: accessToken,
      );

      if (response.statusCode == 200) {
        final data = json.decode(response.body) as Map<String, dynamic>;
        return VitalsStreamResponse.fromJson(data);
      }
      _handleErrorResponse(response);
    } on TimeoutException catch (e) {
      _log('Timeout on $uri: $e');
      throw AegisTimeoutException('Latest vitals query timed out after ${timeout.inSeconds}s');
    } on SocketException catch (e) {
      _log('Socket error on $uri: $e');
      throw AegisNetworkException('Network connection failed: ${e.message}');
    } on http.ClientException catch (e) {
      _log('Client error on $uri: $e');
      throw AegisNetworkException('HTTP client error: ${e.message}');
    }
  }

  /// Submit raw 17-card anamnesis questionnaire answers (`POST /api/v1/client/cascade/intake/anamnesis`).
  /// Requires `cascade:intake` scope.
  Future<AnamnesisIntakeClientResponse> submitAnamnesisIntake(
    AnamnesisIntakeRequest request, {
    String? accessToken,
  }) async {
    final uri = Uri.parse('$baseUrl/api/v1/client/cascade/intake/anamnesis');
    final payloadJson = json.encode(request.toJson());
    try {
      final response = await _sendWithAuthRetry(
        sendRequest: (token) async {
          final headers = await _buildHeaders(accessToken: token);
          _log('POST $uri');
          return await _client.post(uri, headers: headers, body: payloadJson).timeout(timeout);
        },
        explicitAccessToken: accessToken,
      );

      if (response.statusCode == 200) {
        final data = json.decode(response.body) as Map<String, dynamic>;
        return AnamnesisIntakeClientResponse.fromJson(data);
      }
      _handleErrorResponse(response);
    } on TimeoutException catch (e) {
      _log('Timeout on $uri: $e');
      throw AegisTimeoutException('Anamnesis intake timed out after ${timeout.inSeconds}s');
    } on SocketException catch (e) {
      _log('Socket error on $uri: $e');
      throw AegisNetworkException('Network connection failed: ${e.message}');
    } on http.ClientException catch (e) {
      _log('Client error on $uri: $e');
      throw AegisNetworkException('HTTP client error: ${e.message}');
    }
  }

  /// Query clinical pipeline readiness status for authenticated patient (`GET /api/v1/client/cascade/status`).
  /// Requires `cycle:read` scope.
  Future<ClientCascadeStatusResponse> getCascadeStatus({String? accessToken}) async {
    final uri = Uri.parse('$baseUrl/api/v1/client/cascade/status');
    try {
      final response = await _sendWithAuthRetry(
        sendRequest: (token) async {
          final headers = await _buildHeaders(accessToken: token);
          _log('GET $uri');
          return await _client.get(uri, headers: headers).timeout(timeout);
        },
        explicitAccessToken: accessToken,
      );

      if (response.statusCode == 200) {
        final data = json.decode(response.body) as Map<String, dynamic>;
        return ClientCascadeStatusResponse.fromJson(data);
      }
      _handleErrorResponse(response);
    } on TimeoutException catch (e) {
      _log('Timeout on $uri: $e');
      throw AegisTimeoutException('Cascade status request timed out after ${timeout.inSeconds}s');
    } on SocketException catch (e) {
      _log('Socket error on $uri: $e');
      throw AegisNetworkException('Network connection failed: ${e.message}');
    } on http.ClientException catch (e) {
      _log('Client error on $uri: $e');
      throw AegisNetworkException('HTTP client error: ${e.message}');
    }
  }

  /// Uploads 1 to 15 laboratory document images for server-side Vision OCR and ledger integration.
  /// (`POST /api/v1/client/cascade/intake/labs`).
  /// Requires `cascade:intake` scope.
  Future<LabVisionIntakeClientResponse> uploadLabDocuments({
    required List<LabUploadFileItem> files,
    bool isCorrection = false,
    String? sampleDate,
    String? accessToken,
  }) async {
    // 1. Client-side pre-validation
    if (files.isEmpty) {
      throw const AegisValidationException('At least one laboratory report image page must be uploaded.');
    }
    if (files.length > 15) {
      throw AegisValidationException(
        'Maximum 15 pages allowed per laboratory upload (provided ${files.length}).',
      );
    }

    int totalBytes = 0;
    for (final file in files) {
      if (file.bytes.isEmpty) {
        throw AegisValidationException('File "${file.filename}" is empty.');
      }
      if (file.bytes.length > 10 * 1024 * 1024) {
        throw AegisValidationException(
          'File "${file.filename}" exceeds the maximum allowed size of 10 MB.',
        );
      }
      if (!LabUploadFileItem.isSupportedFormat(file.filename, file.bytes)) {
        throw AegisValidationException(
          'File "${file.filename}" has an unsupported format. Supported formats: JPEG, PNG, WEBP, HEIC.',
        );
      }
      totalBytes += file.bytes.length;
    }

    if (totalBytes > 30 * 1024 * 1024) {
      throw AegisValidationException(
        'Total upload size (${(totalBytes / (1024 * 1024)).toStringAsFixed(1)} MB) exceeds the 30 MB cumulative limit.',
      );
    }

    final uri = Uri.parse('$baseUrl/api/v1/client/cascade/intake/labs');

    try {
      final response = await _sendWithAuthRetry(
        sendRequest: (token) async {
          final request = http.MultipartRequest('POST', uri);
          if (token != null && token.trim().isNotEmpty) {
            request.headers['Authorization'] = 'Bearer ${token.trim()}';
          }
          request.headers['Accept'] = 'application/json';

          if (isCorrection) {
            request.fields['is_correction'] = 'true';
          }
          if (sampleDate != null && sampleDate.trim().isNotEmpty) {
            request.fields['sample_date'] = sampleDate.trim();
          }

          for (final f in files) {
            request.files.add(
              http.MultipartFile.fromBytes(
                'files',
                f.bytes,
                filename: f.filename,
              ),
            );
          }

          _log('POST (multipart) $uri with ${files.length} pages (${(totalBytes / 1024).toStringAsFixed(1)} KB)');
          final streamed = await _client.send(request).timeout(timeout);
          return await http.Response.fromStream(streamed);
        },
        explicitAccessToken: accessToken,
      );

      if (response.statusCode == 200) {
        final data = json.decode(response.body) as Map<String, dynamic>;
        return LabVisionIntakeClientResponse.fromJson(data);
      }
      _handleErrorResponse(response);
    } on TimeoutException catch (e) {
      _log('Timeout on $uri: $e');
      throw AegisTimeoutException('Laboratory document upload timed out after ${timeout.inSeconds}s');
    } on SocketException catch (e) {
      _log('Socket error on $uri: $e');
      throw AegisNetworkException('Network connection failed: ${e.message}');
    } on http.ClientException catch (e) {
      _log('Client error on $uri: $e');
      throw AegisNetworkException('HTTP client error: ${e.message}');
    }
  }

  /// Query laboratory ledger state and unreviewed changes indicator (`GET /api/v1/client/labs/state`).
  /// Requires `cycle:read` scope.
  Future<LabLedgerStateResponse> getLabLedgerState({String? accessToken}) async {
    final uri = Uri.parse('$baseUrl/api/v1/client/labs/state');
    try {
      final response = await _sendWithAuthRetry(
        sendRequest: (token) async {
          final headers = await _buildHeaders(accessToken: token);
          _log('GET $uri');
          return await _client.get(uri, headers: headers).timeout(timeout);
        },
        explicitAccessToken: accessToken,
      );

      if (response.statusCode == 200) {
        final data = json.decode(response.body) as Map<String, dynamic>;
        return LabLedgerStateResponse.fromJson(data);
      }
      _handleErrorResponse(response);
    } on TimeoutException catch (e) {
      _log('Timeout on $uri: $e');
      throw AegisTimeoutException('Lab ledger state request timed out after ${timeout.inSeconds}s');
    } on SocketException catch (e) {
      _log('Socket error on $uri: $e');
      throw AegisNetworkException('Network connection failed: ${e.message}');
    } on http.ClientException catch (e) {
      _log('Client error on $uri: $e');
      throw AegisNetworkException('HTTP client error: ${e.message}');
    }
  }

  /// Upload an electrocardiogram tracing image for server-side Vision ECG analysis.
  /// (`POST /api/v1/client/cascade/intake/ecg`).
  /// Requires `cascade:intake` scope.
  Future<EcgVisionIntakeClientResponse> uploadEcgDocument({
    required EcgUploadFileItem file,
    String? recordDate,
    String? accessToken,
  }) async {
    // 1. Client-side pre-validation
    if (file.bytes.isEmpty) {
      throw const AegisValidationException('ECG file is empty.');
    }
    if (file.bytes.length > 10 * 1024 * 1024) {
      throw AegisValidationException(
        'File "${file.filename}" exceeds the maximum allowed size of 10 MB.',
      );
    }
    if (!EcgUploadFileItem.isSupportedFormat(file.filename, file.bytes)) {
      throw AegisValidationException(
        'File "${file.filename}" has an unsupported format. Supported formats: JPEG, PNG, WEBP, HEIC.',
      );
    }

    final uri = Uri.parse('$baseUrl/api/v1/client/cascade/intake/ecg');

    try {
      final response = await _sendWithAuthRetry(
        sendRequest: (token) async {
          final request = http.MultipartRequest('POST', uri);
          if (token != null && token.trim().isNotEmpty) {
            request.headers['Authorization'] = 'Bearer ${token.trim()}';
          }
          request.headers['Accept'] = 'application/json';

          if (recordDate != null && recordDate.trim().isNotEmpty) {
            request.fields['record_date'] = recordDate.trim();
          }

          request.files.add(
            http.MultipartFile.fromBytes(
              'file',
              file.bytes,
              filename: file.filename,
            ),
          );

          _log('POST (multipart) $uri: file "${file.filename}" (${(file.bytes.length / 1024).toStringAsFixed(1)} KB)');
          final streamed = await _client.send(request).timeout(timeout);
          return await http.Response.fromStream(streamed);
        },
        explicitAccessToken: accessToken,
      );

      if (response.statusCode == 200) {
        final data = json.decode(response.body) as Map<String, dynamic>;
        return EcgVisionIntakeClientResponse.fromJson(data);
      }
      _handleErrorResponse(response);
    } on TimeoutException catch (e) {
      _log('Timeout on $uri: $e');
      throw AegisTimeoutException('ECG intake request timed out after ${timeout.inSeconds}s');
    } on SocketException catch (e) {
      _log('Socket error on $uri: $e');
      throw AegisNetworkException('Network connection failed: ${e.message}');
    } on http.ClientException catch (e) {
      _log('Client error on $uri: $e');
      throw AegisNetworkException('HTTP client error: ${e.message}');
    }
  }

  /// Query latest confirmed electrocardiogram intake record (`GET /api/v1/client/cascade/ecg/latest`).
  /// Requires `cycle:read` scope.
  Future<EcgVisionIntakeClientResponse> getLatestEcg({String? accessToken}) async {
    final uri = Uri.parse('$baseUrl/api/v1/client/cascade/ecg/latest');
    try {
      final response = await _sendWithAuthRetry(
        sendRequest: (token) async {
          final headers = await _buildHeaders(accessToken: token);
          _log('GET $uri');
          return await _client.get(uri, headers: headers).timeout(timeout);
        },
        explicitAccessToken: accessToken,
      );

      if (response.statusCode == 200) {
        final data = json.decode(response.body) as Map<String, dynamic>;
        return EcgVisionIntakeClientResponse.fromJson(data);
      }
      _handleErrorResponse(response);
    } on TimeoutException catch (e) {
      _log('Timeout on $uri: $e');
      throw AegisTimeoutException('Latest ECG request timed out after ${timeout.inSeconds}s');
    } on SocketException catch (e) {
      _log('Socket error on $uri: $e');
      throw AegisNetworkException('Network connection failed: ${e.message}');
    } on http.ClientException catch (e) {
      _log('Client error on $uri: $e');
      throw AegisNetworkException('HTTP client error: ${e.message}');
    }
  }

  /// Run or recover Primary Consilium for authenticated patient (`POST /api/v1/client/cascade/run-primary`).
  /// Requires `cascade:run` scope.
  /// If completed, recovers existing reports directly. If fresh un-fenced execution is requested,
  /// server blocks with HTTP 503 until distributed lease protocol is implemented in Stage 5.4E.
  Future<PrimaryConsiliumClientResponse> runPrimaryConsilium({
    String? idempotencyKey,
    String? accessToken,
  }) async {
    final uri = Uri.parse('$baseUrl/api/v1/client/cascade/run-primary');
    try {
      final response = await _sendWithAuthRetry(
        sendRequest: (token) async {
          final headers = await _buildHeaders(accessToken: token);
          if (idempotencyKey != null && idempotencyKey.trim().isNotEmpty) {
            headers['Idempotency-Key'] = idempotencyKey.trim();
          }
          _log('POST $uri (idempotencyKey: $idempotencyKey)');
          return await _client.post(uri, headers: headers).timeout(timeout);
        },
        explicitAccessToken: accessToken,
      );

      if (response.statusCode == 200) {
        final data = json.decode(response.body) as Map<String, dynamic>;
        return PrimaryConsiliumClientResponse.fromJson(data);
      }
      _handleErrorResponse(response);
    } on TimeoutException catch (e) {
      _log('Timeout on $uri: $e');
      throw AegisTimeoutException('Primary consilium request timed out after ${timeout.inSeconds}s');
    } on SocketException catch (e) {
      _log('Socket error on $uri: $e');
      throw AegisNetworkException('Network connection failed: ${e.message}');
    } on http.ClientException catch (e) {
      _log('Client error on $uri: $e');
      throw AegisNetworkException('HTTP client error: ${e.message}');
    }
  }

  /// Run Weekly Consilium for authenticated patient (`POST /api/v1/client/cascade/run-weekly`).
  /// Requires `cascade:run` scope.
  Future<WeeklyConsiliumClientResponse> runWeeklyConsilium({
    String? cycleId,
    String? sprintId,
    String? reportDate,
    String? idempotencyKey,
    String? accessToken,
  }) async {
    final uri = Uri.parse('$baseUrl/api/v1/client/cascade/run-weekly');
    final Map<String, dynamic> bodyMap = {};
    if (cycleId != null && cycleId.trim().isNotEmpty) {
      bodyMap['cycle_id'] = cycleId.trim();
    }
    if (sprintId != null && sprintId.trim().isNotEmpty) {
      bodyMap['sprint_id'] = sprintId.trim();
    }
    if (reportDate != null && reportDate.trim().isNotEmpty) {
      bodyMap['report_date'] = reportDate.trim();
    }
    final bodyJson = bodyMap.isNotEmpty ? json.encode(bodyMap) : null;

    try {
      final response = await _sendWithAuthRetry(
        sendRequest: (token) async {
          final headers = await _buildHeaders(accessToken: token);
          if (idempotencyKey != null && idempotencyKey.trim().isNotEmpty) {
            headers['Idempotency-Key'] = idempotencyKey.trim();
          }
          if (bodyJson != null) {
            headers['Content-Type'] = 'application/json';
          }
          _log('POST $uri (sprintId: $sprintId, idempotencyKey: $idempotencyKey)');
          return await _client
              .post(uri, headers: headers, body: bodyJson)
              .timeout(timeout);
        },
        explicitAccessToken: accessToken,
      );

      if (response.statusCode == 200) {
        final data = json.decode(response.body) as Map<String, dynamic>;
        return WeeklyConsiliumClientResponse.fromJson(data);
      }
      _handleErrorResponse(response);
    } on TimeoutException catch (e) {
      _log('Timeout on $uri: $e');
      throw AegisTimeoutException(
          'Weekly consilium request timed out after ${timeout.inSeconds}s');
    } on SocketException catch (e) {
      _log('Socket error on $uri: $e');
      throw AegisNetworkException('Network connection failed: ${e.message}');
    } on http.ClientException catch (e) {
      _log('Client error on $uri: $e');
      throw AegisNetworkException('HTTP client error: ${e.message}');
    }
  }

  /// Retrieve client-facing model reports for a cycle (`GET /api/v1/reports/client?cycle_id=...`).
  /// Requires `cycle:read` and `reports:read` scopes.
  /// Returns strictly client_facing reports. Server-only private clinical reasoning is excluded.
  Future<List<ClientModelReportResponse>> getClientReports({
    required String cycleId,
    String? reportType,
    String? accessToken,
  }) async {
    final cleanCycleId = cycleId.trim();
    if (cleanCycleId.isEmpty) {
      throw const AegisValidationException('cycleId must not be empty.');
    }
    final queryParams = <String, String>{
      'cycle_id': cleanCycleId,
    };
    if (reportType != null && reportType.trim().isNotEmpty) {
      queryParams['report_type'] = reportType.trim();
    }
    final uri = Uri.parse('$baseUrl/api/v1/reports/client').replace(
      queryParameters: queryParams,
    );
    try {
      final response = await _sendWithAuthRetry(
        sendRequest: (token) async {
          final headers = await _buildHeaders(accessToken: token);
          _log('GET $uri');
          return await _client.get(uri, headers: headers).timeout(timeout);
        },
        explicitAccessToken: accessToken,
      );

      if (response.statusCode == 200) {
        final decoded = json.decode(response.body);
        if (decoded is! List) {
          throw const FormatException('Expected JSON list from /api/v1/reports/client');
        }
        return decoded
            .map((e) => ClientModelReportResponse.fromJson(Map<String, dynamic>.from(e as Map)))
            .toList();
      }
      _handleErrorResponse(response);
    } on TimeoutException catch (e) {
      _log('Timeout on $uri: $e');
      throw AegisTimeoutException('Client reports request timed out after ${timeout.inSeconds}s');
    } on SocketException catch (e) {
      _log('Socket error on $uri: $e');
      throw AegisNetworkException('Network connection failed: ${e.message}');
    } on http.ClientException catch (e) {
      _log('Client error on $uri: $e');
      throw AegisNetworkException('HTTP client error: ${e.message}');
    }
  }

  /// Retrieve a single client-facing model report by ID (`GET /api/v1/reports/client/{report_id}`).
  /// Requires `cycle:read` and `reports:read` scopes.
  Future<ClientModelReportResponse> getSingleClientReport({
    required String reportId,
    String? accessToken,
  }) async {
    final cleanReportId = reportId.trim();
    if (cleanReportId.isEmpty) {
      throw const AegisValidationException('reportId must not be empty.');
    }
    final uri = Uri.parse('$baseUrl/api/v1/reports/client/$cleanReportId');
    try {
      final response = await _sendWithAuthRetry(
        sendRequest: (token) async {
          final headers = await _buildHeaders(accessToken: token);
          _log('GET $uri');
          return await _client.get(uri, headers: headers).timeout(timeout);
        },
        explicitAccessToken: accessToken,
      );

      if (response.statusCode == 200) {
        final data = json.decode(response.body) as Map<String, dynamic>;
        return ClientModelReportResponse.fromJson(data);
      }
      _handleErrorResponse(response);
    } on TimeoutException catch (e) {
      _log('Timeout on $uri: $e');
      throw AegisTimeoutException('Single client report request timed out after ${timeout.inSeconds}s');
    } on SocketException catch (e) {
      _log('Socket error on $uri: $e');
      throw AegisNetworkException('Network connection failed: ${e.message}');
    } on http.ClientException catch (e) {
      _log('Client error on $uri: $e');
      throw AegisNetworkException('HTTP client error: ${e.message}');
    }
  }

  /// Send an interactive chat turn to Athena wellness curator (`POST /api/v1/client/chat/athena`).
  /// Requires `chat:write` scope.
  /// Strictly preserves the caller's [AthenaChatRequest.idempotencyKey] across automatic 401 refresh retries
  /// and application-level network retries (e.g. after timeouts or connection drops).
  Future<AthenaChatResponse> sendAthenaMessage(
    AthenaChatRequest request, {
    String? accessToken,
  }) async {
    if (request.idempotencyKey.trim().isEmpty) {
      throw ArgumentError.value(
        request.idempotencyKey,
        'request.idempotencyKey',
        'AthenaChatRequest must possess a non-empty idempotencyKey to ensure replay safety.',
      );
    }
    final uri = Uri.parse('$baseUrl/api/v1/client/chat/athena');
    final payloadJson = json.encode(request.toJson());

    try {
      final response = await _sendWithAuthRetry(
        sendRequest: (token) async {
          final headers = await _buildHeaders(accessToken: token);
          _log('POST $uri (eventType: ${request.eventType}, idempotencyKey: ${request.idempotencyKey})');
          return await _client.post(uri, headers: headers, body: payloadJson).timeout(timeout);
        },
        explicitAccessToken: accessToken,
      );

      if (response.statusCode == 200) {
        final data = json.decode(response.body) as Map<String, dynamic>;
        return AthenaChatResponse.fromJson(data);
      }
      _handleErrorResponse(response);
    } on TimeoutException catch (e) {
      _log('Timeout on $uri: $e');
      throw AegisTimeoutException('Athena chat timed out after ${timeout.inSeconds}s');
    } on SocketException catch (e) {
      _log('Socket error on $uri: $e');
      throw AegisNetworkException('Network connection failed: ${e.message}');
    } on http.ClientException catch (e) {
      _log('Client error on $uri: $e');
      throw AegisNetworkException('HTTP client error: ${e.message}');
    }
  }

  /// Retrieve sanitized dialogue history for Athena curator (`GET /api/v1/client/chat/athena/history`).
  /// Requires `chat:read` scope.
  Future<AthenaChatHistoryResponse> getAthenaHistory({String? accessToken}) async {
    final uri = Uri.parse('$baseUrl/api/v1/client/chat/athena/history');
    try {
      final response = await _sendWithAuthRetry(
        sendRequest: (token) async {
          final headers = await _buildHeaders(accessToken: token);
          _log('GET $uri');
          return await _client.get(uri, headers: headers).timeout(timeout);
        },
        explicitAccessToken: accessToken,
      );

      if (response.statusCode == 200) {
        final data = json.decode(response.body) as Map<String, dynamic>;
        return AthenaChatHistoryResponse.fromJson(data);
      }
      _handleErrorResponse(response);
    } on TimeoutException catch (e) {
      _log('Timeout on $uri: $e');
      throw AegisTimeoutException('Athena history request timed out after ${timeout.inSeconds}s');
    } on SocketException catch (e) {
      _log('Socket error on $uri: $e');
      throw AegisNetworkException('Network connection failed: ${e.message}');
    } on http.ClientException catch (e) {
      _log('Client error on $uri: $e');
      throw AegisNetworkException('HTTP client error: ${e.message}');
    }
  }

  void close() {
    _client.close();
  }
}
