import 'dart:async';
import 'dart:convert';
import 'dart:io';
import 'package:flutter/foundation.dart';
import 'package:http/http.dart' as http;
import '../auth/auth_models.dart';
import 'vitals_api_models.dart';

typedef TokenProvider = Future<String?> Function();

class AegisApiClient {
  final String baseUrl;
  final TokenProvider? tokenProvider;
  final Duration timeout;
  final http.Client _client;

  AegisApiClient({
    String? baseUrl,
    this.tokenProvider,
    this.timeout = const Duration(seconds: 10),
    http.Client? client,
  })  : baseUrl = (baseUrl ?? _defaultBaseUrl()).replaceAll(RegExp(r'/+$'), ''),
        _client = client ?? http.Client();

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

  Future<Map<String, String>> _buildHeaders() async {
    final headers = _buildJsonHeaders();
    if (tokenProvider != null) {
      final token = await tokenProvider!();
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
    if (tokenProvider == null) return true;
    try {
      final token = await tokenProvider!();
      return token != null && token.trim().isNotEmpty;
    } catch (_) {
      return false;
    }
  }

  /// Resolves the current authorization token string, or null if unauthenticated.
  Future<String?> getAuthToken() async {
    if (tokenProvider == null) return null;
    try {
      final token = await tokenProvider!();
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
        throw AegisServerException(safeDetail, statusCode: 503, responseBody: safeBody);
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
  Future<MedicalCycleResponse> getCurrentCycle() async {
    final uri = Uri.parse('$baseUrl/api/v1/cycle/current');
    try {
      final headers = await _buildHeaders();
      _log('GET $uri');
      final response = await _client.get(uri, headers: headers).timeout(timeout);

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
  Future<HealthMeasurementBatchResponse> postVitalsBatch(HealthMeasurementCreateBatch batch) async {
    final uri = Uri.parse('$baseUrl/api/v1/vitals/batch');
    final payloadJson = json.encode(batch.toJson());
    try {
      final headers = await _buildHeaders();
      _log('POST $uri (${batch.measurements.length} items for cycle ${batch.cycleId})');
      final response = await _client.post(uri, headers: headers, body: payloadJson).timeout(timeout);

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
  Future<HealthMeasurementResponse> postSingleMeasurement(HealthMeasurementCreate measurement) async {
    final uri = Uri.parse('$baseUrl/api/v1/vitals/measurement');
    final payloadJson = json.encode(measurement.toJson());
    try {
      final headers = await _buildHeaders();
      _log('POST $uri (pulse: ${measurement.pulse}, source: ${measurement.source})');
      final response = await _client.post(uri, headers: headers, body: payloadJson).timeout(timeout);

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
  Future<VitalsStreamResponse> getLatestVitals(String cycleId, {bool includeSimulated = false}) async {
    final uri = Uri.parse('$baseUrl/api/v1/vitals/latest').replace(queryParameters: {
      'cycle_id': cycleId,
      if (includeSimulated) 'include_simulated': 'true',
    });
    try {
      final headers = await _buildHeaders();
      _log('GET $uri');
      final response = await _client.get(uri, headers: headers).timeout(timeout);

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

  void close() {
    _client.close();
  }
}
