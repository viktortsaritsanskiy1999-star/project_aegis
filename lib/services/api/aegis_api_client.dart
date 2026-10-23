import 'dart:async';
import 'dart:convert';
import 'dart:io';
import 'package:flutter/foundation.dart';
import 'package:http/http.dart' as http;
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

  Future<Map<String, String>> _buildHeaders() async {
    final headers = <String, String>{
      'Content-Type': 'application/json',
      'Accept': 'application/json',
    };
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

  void _log(String message) {
    // Redact tokens or sensitive auth patterns from debug output
    final sanitized = message.replaceAll(RegExp(r'Bearer\s+[^\s]+'), 'Bearer [REDACTED]');
    debugPrint('[AegisApiClient] $sanitized');
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

    _log('Error ${response.statusCode}: $detail');

    switch (response.statusCode) {
      case 400:
        throw AegisBadRequestException(detail, statusCode: 400, responseBody: response.body);
      case 401:
        throw AegisAuthException(detail, statusCode: 401, responseBody: response.body);
      case 404:
        throw AegisCycleNotFoundException(detail, statusCode: 404, responseBody: response.body);
      case 409:
        throw AegisConflictException(detail, statusCode: 409, responseBody: response.body);
      case 422:
        throw AegisValidationException(detail, statusCode: 422, responseBody: response.body);
      default:
        if (response.statusCode >= 500) {
          throw AegisServerException(detail, statusCode: response.statusCode, responseBody: response.body);
        }
        throw AegisNetworkException(detail, statusCode: response.statusCode, responseBody: response.body);
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
