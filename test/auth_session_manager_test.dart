import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:flutter/foundation.dart';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'package:aegis_health/services/api/aegis_api_client.dart';
import 'package:aegis_health/services/api/vitals_api_models.dart';
import 'package:aegis_health/services/auth/auth_models.dart';
import 'package:aegis_health/services/auth/auth_session_manager.dart';
import 'package:aegis_health/services/auth/auth_token_storage.dart';

/// Fake test double for FlutterSecureStorage simulating OS Keystore / Keychain behavior
/// with granular per-key error injection for storage failure regression tests.
class FakeFlutterSecureStorage extends FlutterSecureStorage {
  final Map<String, String> values = {};
  bool shouldThrow = false;
  bool shouldThrowOnWrite = false;
  bool shouldThrowOnRead = false;
  bool shouldThrowOnDelete = false;

  String? throwOnWriteKey;
  String? throwOnReadKey;
  String? throwOnDeleteKey;
  void Function(String key)? onDeleteHook;
  String? failureMessage;

  FakeFlutterSecureStorage();

  @override
  Future<void> write({
    required String key,
    required String? value,
    AppleOptions? iOptions,
    AndroidOptions? aOptions,
    LinuxOptions? lOptions,
    WebOptions? webOptions,
    AppleOptions? mOptions,
    WindowsOptions? wOptions,
  }) async {
    if (shouldThrow || shouldThrowOnWrite || (throwOnWriteKey != null && throwOnWriteKey == key)) {
      throw Exception(failureMessage ?? 'Simulated Keystore write failure for key: $key');
    }
    if (value == null) {
      values.remove(key);
    } else {
      values[key] = value;
    }
  }

  @override
  Future<String?> read({
    required String key,
    AppleOptions? iOptions,
    AndroidOptions? aOptions,
    LinuxOptions? lOptions,
    WebOptions? webOptions,
    AppleOptions? mOptions,
    WindowsOptions? wOptions,
  }) async {
    if (shouldThrow || shouldThrowOnRead || (throwOnReadKey != null && throwOnReadKey == key)) {
      throw Exception(failureMessage ?? 'Simulated Keystore read failure for key: $key');
    }
    return values[key];
  }

  @override
  Future<void> delete({
    required String key,
    AppleOptions? iOptions,
    AndroidOptions? aOptions,
    LinuxOptions? lOptions,
    WebOptions? webOptions,
    AppleOptions? mOptions,
    WindowsOptions? wOptions,
  }) async {
    if (onDeleteHook != null) {
      onDeleteHook!(key);
    }
    if (shouldThrow || shouldThrowOnDelete || (throwOnDeleteKey != null && throwOnDeleteKey == key)) {
      throw Exception(failureMessage ?? 'Simulated Keystore delete failure for key: $key');
    }
    values.remove(key);
  }
}

/// Builds a structurally valid 3-part base64url JWT string for tests.
String buildTestJwt({
  required String patientId,
  required String deviceId,
  String tokenType = 'access',
  int sequence = 1,
}) {
  final headerJson = json.encode({'alg': 'HS256', 'typ': 'JWT'});
  final payloadJson = json.encode({
    'sub': patientId,
    'did': deviceId,
    'type': tokenType,
    'seq': sequence,
    'jti': 'jti_${deviceId}_$sequence',
  });
  final h = base64Url.encode(utf8.encode(headerJson)).replaceAll('=', '');
  final p = base64Url.encode(utf8.encode(payloadJson)).replaceAll('=', '');
  final s = base64Url
      .encode(utf8.encode('sig_${patientId}_${deviceId}_$sequence'))
      .replaceAll('=', '');
  return '$h.$p.$s';
}

Map<String, dynamic> buildPairOrRefreshPayload({
  required String accessToken,
  required String refreshToken,
  required DateTime expiresAt,
  required DateTime refreshExpiresAt,
  String patientId = 'patient_alpha_01',
  String deviceId = 'dev_pixel8_01',
  String scope = 'vitals:read vitals:write cycle:read',
  int expiresIn = 900,
}) {
  return {
    'access_token': accessToken,
    'token_type': 'Bearer',
    'expires_in': expiresIn,
    'expires_at': expiresAt.toUtc().toIso8601String(),
    'refresh_token': refreshToken,
    'refresh_expires_at': refreshExpiresAt.toUtc().toIso8601String(),
    'patient_id': patientId,
    'device_id': deviceId,
    'scope': scope,
  };
}

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  late DateTime fakeNow;
  late FakeFlutterSecureStorage fakeKeystore;
  late SecureAuthTokenStorage secureStorage;
  AuthSessionManager? activeManager;

  setUp(() {
    SharedPreferences.setMockInitialValues({});
    AuthSessionManager.resetActiveInstancesForTesting();
    fakeNow = DateTime.utc(2026, 10, 26, 12, 0, 0);
    fakeKeystore = FakeFlutterSecureStorage();
    secureStorage = SecureAuthTokenStorage(storage: fakeKeystore);
    activeManager = null;
  });

  tearDown(() {
    activeManager?.dispose();
    AuthSessionManager.resetActiveInstancesForTesting();
  });

  group('Stage 5.3C-2: Pairing, Secure Storage & Restart Recovery', () {
    test('1. pairDevice() exchanges 6-digit code and persists session strictly in SecureAuthTokenStorage', () async {
      final expectedJwt = buildTestJwt(
        patientId: 'patient_alpha_01',
        deviceId: 'dev_pixel8_01',
        sequence: 1,
      );
      const expectedRt = 'aegis_rt_initial_secret_token_001';

      int pairCalls = 0;
      final mockClient = MockClient((request) async {
        expect(request.method, equals('POST'));
        expect(request.url.path, equals('/api/v1/auth/pair'));
        expect(request.headers.containsKey('Authorization'), isFalse);

        final body = json.decode(request.body) as Map<String, dynamic>;
        expect(body['pairing_code'], equals('042918'));
        expect(body['device_name'], equals('Pixel 8 Pro'));
        expect(body['device_id'], equals('dev_pixel8_01'));
        expect(body['platform'], equals('android'));

        pairCalls++;
        return http.Response(
          json.encode(
            buildPairOrRefreshPayload(
              accessToken: expectedJwt,
              refreshToken: expectedRt,
              expiresAt: fakeNow.add(const Duration(minutes: 15)),
              refreshExpiresAt: fakeNow.add(const Duration(days: 30)),
            ),
          ),
          200,
          headers: {'content-type': 'application/json'},
        );
      });

      final apiClient = AegisApiClient(
        baseUrl: 'http://127.0.0.1:8000',
        client: mockClient,
      );
      activeManager = AuthSessionManager(
        apiClient: apiClient,
        storage: secureStorage,
        nowProvider: () => fakeNow,
      );

      expect(activeManager!.status, equals(AuthSessionStatus.unauthenticated));
      expect(activeManager!.isAuthenticated, isFalse);

      final session = await activeManager!.pairDevice(
        DevicePairRequest(
          pairingCode: '042918',
          deviceName: 'Pixel 8 Pro',
          deviceId: 'dev_pixel8_01',
          platform: 'android',
        ),
      );

      expect(pairCalls, equals(1));
      expect(activeManager!.status, equals(AuthSessionStatus.authenticated));
      expect(activeManager!.isAuthenticated, isTrue);
      expect(session.accessToken, equals(expectedJwt));
      expect(session.refreshToken, equals(expectedRt));
      expect(session.patientId, equals('patient_alpha_01'));
      expect(session.deviceId, equals('dev_pixel8_01'));

      // Verify stored in OS Keystore fake and NOT in SharedPreferences
      expect(
        fakeKeystore.values.containsKey(SecureAuthTokenStorage.sessionStorageKey),
        isTrue,
      );
      final prefs = await SharedPreferences.getInstance();
      expect(prefs.getKeys(), isEmpty);
    });

    test('2. Session restoration after simulated application restart preserves auth state without network calls', () async {
      final jwt = buildTestJwt(
        patientId: 'patient_restart_02',
        deviceId: 'dev_restart_02',
        sequence: 1,
      );
      const rt = 'aegis_rt_persisted_across_restart_02';

      await secureStorage.saveSession(
        AuthSession(
          accessToken: jwt,
          expiresIn: 900,
          expiresAt: fakeNow.add(const Duration(minutes: 15)),
          refreshToken: rt,
          refreshExpiresAt: fakeNow.add(const Duration(days: 30)),
          patientId: 'patient_restart_02',
          deviceId: 'dev_restart_02',
          scope: 'vitals:read vitals:write',
        ),
      );

      int networkCalls = 0;
      final mockClient = MockClient((request) async {
        networkCalls++;
        return http.Response('Should not be called', 500);
      });

      final apiClient = AegisApiClient(
        baseUrl: 'http://127.0.0.1:8000',
        client: mockClient,
      );

      // First manager instance loads session on startup
      final manager1 = AuthSessionManager(
        apiClient: apiClient,
        storage: secureStorage,
        nowProvider: () => fakeNow,
      );
      final loaded1 = await manager1.initialize();
      expect(loaded1, isNotNull);
      expect(manager1.status, equals(AuthSessionStatus.authenticated));
      expect(await manager1.getValidAccessToken(), equals(jwt));
      expect(networkCalls, equals(0));

      // Simulate app termination and cold restart with a new manager instance
      manager1.dispose();

      activeManager = AuthSessionManager(
        apiClient: apiClient,
        storage: secureStorage,
        nowProvider: () => fakeNow,
      );
      final loaded2 = await activeManager!.initialize();
      expect(loaded2, isNotNull);
      expect(activeManager!.status, equals(AuthSessionStatus.authenticated));
      expect(activeManager!.currentSession!.patientId, equals('patient_restart_02'));
      expect(await activeManager!.getValidAccessToken(), equals(jwt));
      expect(networkCalls, equals(0));
    });
  });

  group('Stage 5.3C-2: Pre-emptive Refresh, Single-Flight Concurrency & Token Rotation', () {
    test('3. Pre-emptive refresh triggers automatically when access token is within 60s skew window', () async {
      final initialJwt = buildTestJwt(
        patientId: 'patient_03',
        deviceId: 'dev_03',
        sequence: 1,
      );
      final refreshedJwt = buildTestJwt(
        patientId: 'patient_03',
        deviceId: 'dev_03',
        sequence: 2,
      );
      const initialRt = 'aegis_rt_preemptive_v1';
      const rotatedRt = 'aegis_rt_preemptive_v2';

      // Access token expires in 45 seconds (< 60s skew buffer)
      await secureStorage.saveSession(
        AuthSession(
          accessToken: initialJwt,
          expiresIn: 900,
          expiresAt: fakeNow.add(const Duration(seconds: 45)),
          refreshToken: initialRt,
          refreshExpiresAt: fakeNow.add(const Duration(days: 30)),
          patientId: 'patient_03',
          deviceId: 'dev_03',
          scope: 'vitals:read vitals:write',
        ),
      );

      int refreshCount = 0;
      final mockClient = MockClient((request) async {
        expect(request.url.path, equals('/api/v1/auth/refresh'));
        final body = json.decode(request.body) as Map<String, dynamic>;
        expect(body['device_id'], equals('dev_03'));
        expect(body['refresh_token'], equals(initialRt));
        refreshCount++;

        return http.Response(
          json.encode(
            buildPairOrRefreshPayload(
              accessToken: refreshedJwt,
              refreshToken: rotatedRt,
              expiresAt: fakeNow.add(const Duration(minutes: 15)),
              refreshExpiresAt: fakeNow.add(const Duration(days: 30)),
              patientId: 'patient_03',
              deviceId: 'dev_03',
            ),
          ),
          200,
          headers: {'content-type': 'application/json'},
        );
      });

      final apiClient = AegisApiClient(client: mockClient);
      activeManager = AuthSessionManager(
        apiClient: apiClient,
        storage: secureStorage,
        nowProvider: () => fakeNow,
      );
      await activeManager!.initialize();

      final validToken = await activeManager!.getValidAccessToken();
      expect(validToken, equals(refreshedJwt));
      expect(refreshCount, equals(1));
      expect(activeManager!.currentSession!.refreshToken, equals(rotatedRt));
    });

    test('4. Single-Flight concurrency: 5 parallel calls trigger strictly 1 POST /api/v1/auth/refresh', () async {
      final expiredJwt = buildTestJwt(
        patientId: 'patient_sf_04',
        deviceId: 'dev_sf_04',
        sequence: 1,
      );
      final freshJwt = buildTestJwt(
        patientId: 'patient_sf_04',
        deviceId: 'dev_sf_04',
        sequence: 2,
      );
      const initialRt = 'aegis_rt_single_flight_v1';
      const rotatedRt = 'aegis_rt_single_flight_v2';

      await secureStorage.saveSession(
        AuthSession(
          accessToken: expiredJwt,
          expiresIn: 900,
          expiresAt: fakeNow.subtract(const Duration(seconds: 10)),
          refreshToken: initialRt,
          refreshExpiresAt: fakeNow.add(const Duration(days: 30)),
          patientId: 'patient_sf_04',
          deviceId: 'dev_sf_04',
          scope: 'vitals:read vitals:write',
        ),
      );

      int refreshHttpRequests = 0;
      final mockClient = MockClient((request) async {
        expect(request.url.path, equals('/api/v1/auth/refresh'));
        refreshHttpRequests++;
        await Future<void>.delayed(const Duration(milliseconds: 40));

        return http.Response(
          json.encode(
            buildPairOrRefreshPayload(
              accessToken: freshJwt,
              refreshToken: rotatedRt,
              expiresAt: fakeNow.add(const Duration(minutes: 15)),
              refreshExpiresAt: fakeNow.add(const Duration(days: 30)),
              patientId: 'patient_sf_04',
              deviceId: 'dev_sf_04',
            ),
          ),
          200,
          headers: {'content-type': 'application/json'},
        );
      });

      final apiClient = AegisApiClient(client: mockClient);
      activeManager = AuthSessionManager(
        apiClient: apiClient,
        storage: secureStorage,
        nowProvider: () => fakeNow,
      );
      await activeManager!.initialize();

      final results = await Future.wait(
        List.generate(5, (_) => activeManager!.getValidAccessToken()),
      );

      expect(refreshHttpRequests, equals(1));
      for (final token in results) {
        expect(token, equals(freshJwt));
      }

      final persisted = await secureStorage.loadSession();
      expect(persisted, isNotNull);
      expect(persisted!.accessToken, equals(freshJwt));
      expect(persisted.refreshToken, equals(rotatedRt));
    });

    test('5. Refresh token rotation: sequential refreshes always use newly rotated refresh token', () async {
      final jwt1 = buildTestJwt(patientId: 'p_rot', deviceId: 'd_rot', sequence: 1);
      final jwt2 = buildTestJwt(patientId: 'p_rot', deviceId: 'd_rot', sequence: 2);
      final jwt3 = buildTestJwt(patientId: 'p_rot', deviceId: 'd_rot', sequence: 3);
      const rt1 = 'aegis_rt_chain_1';
      const rt2 = 'aegis_rt_chain_2';
      const rt3 = 'aegis_rt_chain_3';

      await secureStorage.saveSession(
        AuthSession(
          accessToken: jwt1,
          expiresIn: 900,
          expiresAt: fakeNow.subtract(const Duration(seconds: 5)),
          refreshToken: rt1,
          refreshExpiresAt: fakeNow.add(const Duration(days: 30)),
          patientId: 'p_rot',
          deviceId: 'd_rot',
          scope: 'vitals:read',
        ),
      );

      final receivedRefreshTokens = <String>[];
      final mockClient = MockClient((request) async {
        final body = json.decode(request.body) as Map<String, dynamic>;
        final suppliedRt = body['refresh_token'] as String;
        receivedRefreshTokens.add(suppliedRt);

        if (suppliedRt == rt1) {
          return http.Response(
            json.encode(
              buildPairOrRefreshPayload(
                accessToken: jwt2,
                refreshToken: rt2,
                expiresAt: fakeNow.add(const Duration(minutes: 15)),
                refreshExpiresAt: fakeNow.add(const Duration(days: 30)),
                patientId: 'p_rot',
                deviceId: 'd_rot',
              ),
            ),
            200,
          );
        } else if (suppliedRt == rt2) {
          return http.Response(
            json.encode(
              buildPairOrRefreshPayload(
                accessToken: jwt3,
                refreshToken: rt3,
                expiresAt: fakeNow.add(const Duration(minutes: 30)),
                refreshExpiresAt: fakeNow.add(const Duration(days: 30)),
                patientId: 'p_rot',
                deviceId: 'd_rot',
              ),
            ),
            200,
          );
        }
        return http.Response('Unexpected refresh token', 401);
      });

      final apiClient = AegisApiClient(client: mockClient);
      activeManager = AuthSessionManager(
        apiClient: apiClient,
        storage: secureStorage,
        nowProvider: () => fakeNow,
      );
      await activeManager!.initialize();

      final resolved1 = await activeManager!.getValidAccessToken();
      expect(resolved1, equals(jwt2));
      expect(activeManager!.currentSession!.refreshToken, equals(rt2));

      fakeNow = fakeNow.add(const Duration(minutes: 16));
      final resolved2 = await activeManager!.getValidAccessToken();
      expect(resolved2, equals(jwt3));
      expect(activeManager!.currentSession!.refreshToken, equals(rt3));

      expect(receivedRefreshTokens, equals([rt1, rt2]));
    });
  });

  group('Stage 5.3C-2: Auth Error Handling (401, 403, 429 + Retry-After, 503)', () {
    test('6. HTTP 401 on refresh marks session expired, clears storage, and prevents retry loops', () async {
      final expiredJwt = buildTestJwt(patientId: 'p_401', deviceId: 'd_401', sequence: 1);
      const rt = 'aegis_rt_revoked_on_server';

      await secureStorage.saveSession(
        AuthSession(
          accessToken: expiredJwt,
          expiresIn: 900,
          expiresAt: fakeNow.subtract(const Duration(minutes: 1)),
          refreshToken: rt,
          refreshExpiresAt: fakeNow.add(const Duration(days: 30)),
          patientId: 'p_401',
          deviceId: 'd_401',
          scope: 'vitals:read',
        ),
      );

      int refreshAttempts = 0;
      final mockClient = MockClient((request) async {
        refreshAttempts++;
        return http.Response(
          json.encode({'detail': 'Invalid, expired, or revoked refresh token.'}),
          401,
          headers: {'content-type': 'application/json'},
        );
      });

      final apiClient = AegisApiClient(client: mockClient);
      activeManager = AuthSessionManager(
        apiClient: apiClient,
        storage: secureStorage,
        nowProvider: () => fakeNow,
      );
      await activeManager!.initialize();

      final token1 = await activeManager!.getValidAccessToken();
      expect(token1, isNull);
      expect(refreshAttempts, equals(1));
      expect(activeManager!.status, equals(AuthSessionStatus.expired));
      expect(activeManager!.lastError, isA<AegisAuthException>());
      expect(await secureStorage.loadSession(), isNull);

      final token2 = await activeManager!.getValidAccessToken();
      expect(token2, isNull);
      expect(refreshAttempts, equals(1));
    });

    test('7. HTTP 403 Forbidden is distinguished from 401 and never triggers infinite refresh retries', () async {
      final validJwt = buildTestJwt(patientId: 'p_403', deviceId: 'd_403', sequence: 1);
      const rt = 'aegis_rt_403_token';

      await secureStorage.saveSession(
        AuthSession(
          accessToken: validJwt,
          expiresIn: 900,
          expiresAt: fakeNow.add(const Duration(minutes: 15)),
          refreshToken: rt,
          refreshExpiresAt: fakeNow.add(const Duration(days: 30)),
          patientId: 'p_403',
          deviceId: 'd_403',
          scope: 'cycle:read',
        ),
      );

      int refreshCalls = 0;
      int cycleCalls = 0;
      final mockClient = MockClient((request) async {
        if (request.url.path == '/api/v1/auth/refresh') {
          refreshCalls++;
          return http.Response('Should not refresh on 403', 500);
        }
        cycleCalls++;
        return http.Response(
          json.encode({'detail': 'Insufficient device session scopes.'}),
          403,
          headers: {'content-type': 'application/json'},
        );
      });

      late AuthSessionManager manager;
      final apiClient = AegisApiClient(
        client: mockClient,
        tokenProvider: () => manager.getValidAccessToken(),
      );
      manager = AuthSessionManager(
        apiClient: apiClient,
        storage: secureStorage,
        nowProvider: () => fakeNow,
      );
      activeManager = manager;
      await manager.initialize();

      AegisForbiddenException? caught403;
      try {
        await apiClient.getCurrentCycle();
      } on AegisForbiddenException catch (e) {
        caught403 = e;
      }

      expect(caught403, isNotNull);
      expect(caught403!.statusCode, equals(403));
      expect(cycleCalls, equals(1));

      final shouldRetry = await manager.handleProtectedEndpointError(caught403);
      expect(shouldRetry, isFalse);
      expect(refreshCalls, equals(0));
    });

    test('8. HTTP 429 Too Many Requests extracts Retry-After and transitions to rateLimited state', () async {
      int pairHttpCalls = 0;
      final mockClient = MockClient((request) async {
        pairHttpCalls++;
        return http.Response(
          json.encode({'detail': 'Too many failed pairing attempts. Please retry later.'}),
          429,
          headers: {
            'content-type': 'application/json',
            'Retry-After': '900',
          },
        );
      });

      final apiClient = AegisApiClient(client: mockClient);
      activeManager = AuthSessionManager(
        apiClient: apiClient,
        storage: secureStorage,
        nowProvider: () => fakeNow,
      );

      AegisRateLimitException? rateLimitErr;
      try {
        await activeManager!.pairDevice(
          DevicePairRequest(
            pairingCode: '042918',
            deviceName: 'Pixel 8',
          ),
        );
      } on AegisRateLimitException catch (e) {
        rateLimitErr = e;
      }

      expect(rateLimitErr, isNotNull);
      expect(rateLimitErr!.statusCode, equals(429));
      expect(rateLimitErr.retryAfter, equals(const Duration(seconds: 900)));
      expect(activeManager!.status, equals(AuthSessionStatus.rateLimited));
      expect(activeManager!.lastRetryAfter, equals(const Duration(seconds: 900)));
      expect(activeManager!.rateLimitRemaining, equals(const Duration(seconds: 900)));
      expect(pairHttpCalls, equals(1));

      fakeNow = fakeNow.add(const Duration(seconds: 300));
      AegisRateLimitException? blockedErr;
      try {
        await activeManager!.pairDevice(
          DevicePairRequest(
            pairingCode: '042918',
            deviceName: 'Pixel 8',
          ),
        );
      } on AegisRateLimitException catch (e) {
        blockedErr = e;
      }
      expect(blockedErr, isNotNull);
      expect(blockedErr!.retryAfter, equals(const Duration(seconds: 600)));
      expect(pairHttpCalls, equals(1));
    });

    test('9. HTTP 503 Service Unavailable is surfaced as AegisServerException(statusCode: 503)', () async {
      final mockClient = MockClient((request) async {
        return http.Response(
          json.encode({'detail': 'Authentication rate limiter is unavailable (fail-closed).'}),
          503,
          headers: {'content-type': 'application/json'},
        );
      });

      final apiClient = AegisApiClient(client: mockClient);
      activeManager = AuthSessionManager(
        apiClient: apiClient,
        storage: secureStorage,
        nowProvider: () => fakeNow,
      );

      AegisServerException? serverErr;
      try {
        await activeManager!.pairDevice(
          DevicePairRequest(
            pairingCode: '123456',
            deviceName: 'Pixel 8',
          ),
        );
      } on AegisServerException catch (e) {
        serverErr = e;
      }

      expect(serverErr, isNotNull);
      expect(serverErr!.statusCode, equals(503));
      expect(activeManager!.lastError, isA<AegisServerException>());
    });
  });

  group('Stage 5.3C-2: Offline Pre-Check vs. Indeterminate Refresh & Token Reuse Prevention', () {
    test('10. Network loss BEFORE sending refresh preserves SecureAuthTokenStorage and allows later refresh', () async {
      final expiredJwt = buildTestJwt(patientId: 'p_net', deviceId: 'd_net', sequence: 1);
      final freshJwt = buildTestJwt(patientId: 'p_net', deviceId: 'd_net', sequence: 2);
      const initialRt = 'aegis_rt_preserved_offline_v1';
      const rotatedRt = 'aegis_rt_preserved_offline_v2';

      await secureStorage.saveSession(
        AuthSession(
          accessToken: expiredJwt,
          expiresIn: 900,
          expiresAt: fakeNow.subtract(const Duration(minutes: 2)),
          refreshToken: initialRt,
          refreshExpiresAt: fakeNow.add(const Duration(days: 30)),
          patientId: 'p_net',
          deviceId: 'd_net',
          scope: 'vitals:read vitals:write',
        ),
      );

      bool isOnline = false;
      int httpRefreshCount = 0;

      final mockClient = MockClient((request) async {
        httpRefreshCount++;
        return http.Response(
          json.encode(
            buildPairOrRefreshPayload(
              accessToken: freshJwt,
              refreshToken: rotatedRt,
              expiresAt: fakeNow.add(const Duration(minutes: 15)),
              refreshExpiresAt: fakeNow.add(const Duration(days: 30)),
              patientId: 'p_net',
              deviceId: 'd_net',
            ),
          ),
          200,
        );
      });

      final apiClient = AegisApiClient(client: mockClient);
      activeManager = AuthSessionManager(
        apiClient: apiClient,
        storage: secureStorage,
        nowProvider: () => fakeNow,
        connectivityCheck: () async => isOnline,
      );
      await activeManager!.initialize();

      final tokenWhileOffline = await activeManager!.getValidAccessToken();
      expect(tokenWhileOffline, isNull);
      expect(httpRefreshCount, equals(0));
      expect(activeManager!.isRefreshIndeterminate, isFalse);
      expect(activeManager!.status, equals(AuthSessionStatus.authenticated));

      final storedWhileOffline = await secureStorage.loadSession();
      expect(storedWhileOffline, isNotNull);
      expect(storedWhileOffline!.refreshToken, equals(initialRt));

      isOnline = true;
      final tokenWhenBackOnline = await activeManager!.getValidAccessToken();
      expect(tokenWhenBackOnline, equals(freshJwt));
      expect(httpRefreshCount, equals(1));
      expect(activeManager!.currentSession!.refreshToken, equals(rotatedRt));
    });

    test('11. Indeterminate outcome AFTER sending refresh (timeout / socket drop) suspends refresh and never reuses old token', () async {
      final expiredJwt = buildTestJwt(patientId: 'p_indet', deviceId: 'd_indet', sequence: 1);
      const initialRt = 'aegis_rt_possibly_rotated_on_server';

      await secureStorage.saveSession(
        AuthSession(
          accessToken: expiredJwt,
          expiresIn: 900,
          expiresAt: fakeNow.subtract(const Duration(seconds: 30)),
          refreshToken: initialRt,
          refreshExpiresAt: fakeNow.add(const Duration(days: 30)),
          patientId: 'p_indet',
          deviceId: 'd_indet',
          scope: 'vitals:read',
        ),
      );

      int refreshDispatches = 0;
      final mockClient = MockClient((request) async {
        refreshDispatches++;
        throw TimeoutException('Simulated read timeout after POST /auth/refresh sent');
      });

      final apiClient = AegisApiClient(client: mockClient);
      activeManager = AuthSessionManager(
        apiClient: apiClient,
        storage: secureStorage,
        nowProvider: () => fakeNow,
      );
      await activeManager!.initialize();

      final result1 = await activeManager!.getValidAccessToken();
      expect(result1, isNull);
      expect(refreshDispatches, equals(1));
      expect(activeManager!.isRefreshIndeterminate, isTrue);
      expect(activeManager!.status, equals(AuthSessionStatus.expired));
      expect(activeManager!.lastError, isA<AegisTimeoutException>());

      final result2 = await activeManager!.getValidAccessToken();
      expect(result2, isNull);
      await expectLater(
        () => activeManager!.refreshSession(),
        throwsA(isA<AegisAuthException>()),
      );
      expect(refreshDispatches, equals(1));
    });

    test('12. Prohibition of old refresh token reuse even if stale session is reloaded', () async {
      final jwt1 = buildTestJwt(patientId: 'p_reuse', deviceId: 'd_reuse', sequence: 1);
      final jwt2 = buildTestJwt(patientId: 'p_reuse', deviceId: 'd_reuse', sequence: 2);
      const oldRt = 'aegis_rt_single_use_old';
      const newRt = 'aegis_rt_single_use_new';

      final initialSession = AuthSession(
        accessToken: jwt1,
        expiresIn: 900,
        expiresAt: fakeNow.subtract(const Duration(seconds: 10)),
        refreshToken: oldRt,
        refreshExpiresAt: fakeNow.add(const Duration(days: 30)),
        patientId: 'p_reuse',
        deviceId: 'd_reuse',
        scope: 'vitals:read',
      );
      await secureStorage.saveSession(initialSession);

      int refreshHttpCount = 0;
      final mockClient = MockClient((request) async {
        refreshHttpCount++;
        return http.Response(
          json.encode(
            buildPairOrRefreshPayload(
              accessToken: jwt2,
              refreshToken: newRt,
              expiresAt: fakeNow.add(const Duration(minutes: 15)),
              refreshExpiresAt: fakeNow.add(const Duration(days: 30)),
              patientId: 'p_reuse',
              deviceId: 'd_reuse',
            ),
          ),
          200,
        );
      });

      final apiClient = AegisApiClient(client: mockClient);
      activeManager = AuthSessionManager(
        apiClient: apiClient,
        storage: secureStorage,
        nowProvider: () => fakeNow,
      );
      await activeManager!.initialize();

      final token = await activeManager!.getValidAccessToken();
      expect(token, equals(jwt2));
      expect(refreshHttpCount, equals(1));

      await secureStorage.saveSession(initialSession);
      await activeManager!.loadSession();

      expect(activeManager!.status, equals(AuthSessionStatus.expired));
      expect(await activeManager!.getValidAccessToken(), isNull);
      expect(refreshHttpCount, equals(1));
    });
  });

  group('Stage 5.3C-2: Device Revocation & Security Invariants', () {
    test('13. revokeSession() clears SecureAuthTokenStorage only after confirmed server revocation', () async {
      final validJwt = buildTestJwt(patientId: 'p_rev', deviceId: 'd_rev', sequence: 1);
      const rt = 'aegis_rt_to_be_revoked';

      await secureStorage.saveSession(
        AuthSession(
          accessToken: validJwt,
          expiresIn: 900,
          expiresAt: fakeNow.add(const Duration(minutes: 10)),
          refreshToken: rt,
          refreshExpiresAt: fakeNow.add(const Duration(days: 30)),
          patientId: 'p_rev',
          deviceId: 'd_rev',
          scope: 'vitals:read vitals:write',
        ),
      );

      bool failNetwork = true;
      int revokeCalls = 0;
      final mockClient = MockClient((request) async {
        expect(request.url.path, equals('/api/v1/auth/revoke'));
        expect(request.headers['Authorization'], equals('Bearer $validJwt'));
        final body = json.decode(request.body) as Map<String, dynamic>;
        expect(body['device_id'], equals('d_rev'));
        expect(body['refresh_token'], equals(rt));

        revokeCalls++;
        if (failNetwork) {
          throw const SocketException('Temporary offline during revoke');
        }
        return http.Response(
          json.encode({
            'status': 'revoked',
            'device_id': 'd_rev',
            'revoked': true,
          }),
          200,
          headers: {'content-type': 'application/json'},
        );
      });

      final apiClient = AegisApiClient(client: mockClient);
      activeManager = AuthSessionManager(
        apiClient: apiClient,
        storage: secureStorage,
        nowProvider: () => fakeNow,
      );
      await activeManager!.initialize();

      await expectLater(
        () => activeManager!.revokeSession(),
        throwsA(isA<AegisNetworkException>()),
      );
      expect(await secureStorage.loadSession(), isNotNull);

      failNetwork = false;
      final revokeResp = await activeManager!.revokeSession();
      expect(revokeResp.revoked, isTrue);
      expect(revokeResp.deviceId, equals('d_rev'));
      expect(revokeCalls, equals(2));
      expect(activeManager!.status, equals(AuthSessionStatus.unauthenticated));
      expect(activeManager!.currentSession, isNull);
      expect(await secureStorage.loadSession(), isNull);
    });

    test('14. Security guards: rejects AEGIS_API_SECRET, blocks patient_id mutation on refresh, and blocks parallel managers', () async {
      await secureStorage.saveSession(
        AuthSession(
          accessToken: 'test_device_secret_token_12345',
          expiresIn: 900,
          expiresAt: fakeNow.add(const Duration(minutes: 15)),
          refreshToken: 'aegis_rt_valid',
          refreshExpiresAt: fakeNow.add(const Duration(days: 30)),
          patientId: 'p_sec',
          deviceId: 'd_sec',
          scope: 'vitals:read',
        ),
      );

      final jwt1 = buildTestJwt(patientId: 'patient_original', deviceId: 'd_sec', sequence: 1);
      final jwtMutated = buildTestJwt(patientId: 'patient_hijacked', deviceId: 'd_sec', sequence: 2);

      final mockClient = MockClient((request) async {
        if (request.url.path == '/api/v1/auth/refresh') {
          return http.Response(
            json.encode(
              buildPairOrRefreshPayload(
                accessToken: jwtMutated,
                refreshToken: 'aegis_rt_rotated_hijack',
                expiresAt: fakeNow.add(const Duration(minutes: 15)),
                refreshExpiresAt: fakeNow.add(const Duration(days: 30)),
                patientId: 'patient_hijacked',
                deviceId: 'd_sec',
              ),
            ),
            200,
          );
        }
        return http.Response('Not found', 404);
      });

      final apiClient = AegisApiClient(client: mockClient);
      activeManager = AuthSessionManager(
        apiClient: apiClient,
        storage: secureStorage,
        nowProvider: () => fakeNow,
      );

      await expectLater(
        () => activeManager!.initialize(),
        throwsA(isA<AegisAuthException>()),
      );
      expect(await secureStorage.loadSession(), isNull);

      expect(
        () => AuthSessionManager(apiClient: apiClient, storage: secureStorage),
        throwsA(isA<StateError>()),
      );

      await secureStorage.saveSession(
        AuthSession(
          accessToken: jwt1,
          expiresIn: 900,
          expiresAt: fakeNow.subtract(const Duration(seconds: 10)),
          refreshToken: 'aegis_rt_original',
          refreshExpiresAt: fakeNow.add(const Duration(days: 30)),
          patientId: 'patient_original',
          deviceId: 'd_sec',
          scope: 'vitals:read',
        ),
      );
      await activeManager!.loadSession();

      await expectLater(
        () => activeManager!.refreshSession(),
        throwsA(isA<AegisAuthException>()),
      );
      expect(activeManager!.status, equals(AuthSessionStatus.expired));
      expect(activeManager!.currentSession, isNull);
      expect(await secureStorage.loadSession(), isNull);
    });

    test('15. Zero secrets in logs: pairing_code, access_token, and refresh_token never appear in debugPrint output', () async {
      const secretPairingCode = '042918';
      final secretJwt1 = buildTestJwt(
        patientId: 'p_log',
        deviceId: 'd_log',
        sequence: 1,
      );
      final secretJwt2 = buildTestJwt(
        patientId: 'p_log',
        deviceId: 'd_log',
        sequence: 2,
      );
      const secretRt1 = 'aegis_rt_super_secret_refresh_11111';
      const secretRt2 = 'aegis_rt_super_secret_refresh_22222';

      final capturedLogs = <String>[];
      final originalDebugPrint = debugPrint;
      debugPrint = (String? message, {int? wrapWidth}) {
        if (message != null) {
          capturedLogs.add(message);
        }
      };

      try {
        final mockClient = MockClient((request) async {
          if (request.url.path == '/api/v1/auth/pair') {
            return http.Response(
              json.encode(
                buildPairOrRefreshPayload(
                  accessToken: secretJwt1,
                  refreshToken: secretRt1,
                  expiresAt: fakeNow.subtract(const Duration(seconds: 5)),
                  refreshExpiresAt: fakeNow.add(const Duration(days: 30)),
                  patientId: 'p_log',
                  deviceId: 'd_log',
                ),
              ),
              200,
            );
          }
          if (request.url.path == '/api/v1/auth/refresh') {
            return http.Response(
              json.encode(
                buildPairOrRefreshPayload(
                  accessToken: secretJwt2,
                  refreshToken: secretRt2,
                  expiresAt: fakeNow.add(const Duration(minutes: 15)),
                  refreshExpiresAt: fakeNow.add(const Duration(days: 30)),
                  patientId: 'p_log',
                  deviceId: 'd_log',
                ),
              ),
              200,
            );
          }
          if (request.url.path == '/api/v1/auth/revoke') {
            return http.Response(
              json.encode({
                'status': 'revoked',
                'device_id': 'd_log',
                'revoked': true,
              }),
              200,
            );
          }
          return http.Response(
            json.encode({
              'detail': 'Error echoing pairing_code=$secretPairingCode and Bearer $secretJwt1 and $secretRt1',
            }),
            422,
          );
        });

        final apiClient = AegisApiClient(client: mockClient);
        activeManager = AuthSessionManager(
          apiClient: apiClient,
          storage: secureStorage,
          nowProvider: () => fakeNow,
        );

        await activeManager!.pairDevice(
          DevicePairRequest(
            pairingCode: secretPairingCode,
            deviceName: 'Pixel 8 Pro',
            deviceId: 'd_log',
          ),
        );
        await activeManager!.getValidAccessToken();
        await activeManager!.revokeSession();
      } finally {
        debugPrint = originalDebugPrint;
      }

      expect(capturedLogs, isNotEmpty);
      final combinedLogs = capturedLogs.join('\n');
      expect(combinedLogs.contains(secretPairingCode), isFalse);
      expect(combinedLogs.contains(secretJwt1), isFalse);
      expect(combinedLogs.contains(secretJwt2), isFalse);
      expect(combinedLogs.contains(secretRt1), isFalse);
      expect(combinedLogs.contains(secretRt2), isFalse);
    });
  });

  group('Stage 5.3C-2 Hardening: Persistent Pre-Dispatch Protection & Storage Failure Resilience', () {
    test('16. Failure to persist in-flight protective state before dispatch prevents HTTP /refresh dispatch', () async {
      final expiredJwt = buildTestJwt(patientId: 'p_hard_16', deviceId: 'd_hard_16', sequence: 1);
      const initialRt = 'aegis_rt_hard_16_initial';

      await secureStorage.saveSession(
        AuthSession(
          accessToken: expiredJwt,
          expiresIn: 900,
          expiresAt: fakeNow.subtract(const Duration(seconds: 10)),
          refreshToken: initialRt,
          refreshExpiresAt: fakeNow.add(const Duration(days: 30)),
          patientId: 'p_hard_16',
          deviceId: 'd_hard_16',
          scope: 'vitals:read',
        ),
      );

      // Inject write failure specifically on the in-flight protective key
      fakeKeystore.throwOnWriteKey = SecureAuthTokenStorage.inFlightStorageKey;
      fakeKeystore.failureMessage = 'Keystore failure writing in-flight lock';

      int httpRefreshDispatches = 0;
      final mockClient = MockClient((request) async {
        httpRefreshDispatches++;
        return http.Response('Should never reach server', 500);
      });

      final apiClient = AegisApiClient(client: mockClient);
      activeManager = AuthSessionManager(
        apiClient: apiClient,
        storage: secureStorage,
        nowProvider: () => fakeNow,
      );
      await activeManager!.initialize();

      // 1. refreshSession() throws SecureStorageUnavailableException
      await expectLater(
        () => activeManager!.refreshSession(),
        throwsA(isA<SecureStorageUnavailableException>()),
      );

      // 2. HTTP refresh request was NEVER dispatched!
      expect(httpRefreshDispatches, equals(0));

      // 3. Stored session remains uncorrupted in SecureAuthTokenStorage
      final persisted = await secureStorage.loadSession();
      expect(persisted, isNotNull);
      expect(persisted!.refreshToken, equals(initialRt));

      // 4. getValidAccessToken() fails closed and returns null
      final token = await activeManager!.getValidAccessToken();
      expect(token, isNull);
      expect(httpRefreshDispatches, equals(0));
    });

    test('17. Refresh succeeds on server, but saving rotated session fails -> transitions to expired requiring re-pairing', () async {
      final expiredJwt = buildTestJwt(patientId: 'p_hard_17', deviceId: 'd_hard_17', sequence: 1);
      final rotatedJwt = buildTestJwt(patientId: 'p_hard_17', deviceId: 'd_hard_17', sequence: 2);
      const initialRt = 'aegis_rt_hard_17_old';
      const rotatedRt = 'aegis_rt_hard_17_new';

      await secureStorage.saveSession(
        AuthSession(
          accessToken: expiredJwt,
          expiresIn: 900,
          expiresAt: fakeNow.subtract(const Duration(seconds: 10)),
          refreshToken: initialRt,
          refreshExpiresAt: fakeNow.add(const Duration(days: 30)),
          patientId: 'p_hard_17',
          deviceId: 'd_hard_17',
          scope: 'vitals:read',
        ),
      );

      // Allow writing inFlightStorageKey, but FAIL writing sessionStorageKey for the rotated session
      fakeKeystore.throwOnWriteKey = SecureAuthTokenStorage.sessionStorageKey;
      fakeKeystore.failureMessage = 'Keystore failure saving rotated session';

      int httpRefreshDispatches = 0;
      final mockClient = MockClient((request) async {
        httpRefreshDispatches++;
        return http.Response(
          json.encode(
            buildPairOrRefreshPayload(
              accessToken: rotatedJwt,
              refreshToken: rotatedRt,
              expiresAt: fakeNow.add(const Duration(minutes: 15)),
              refreshExpiresAt: fakeNow.add(const Duration(days: 30)),
              patientId: 'p_hard_17',
              deviceId: 'd_hard_17',
            ),
          ),
          200,
        );
      });

      final apiClient = AegisApiClient(client: mockClient);
      activeManager = AuthSessionManager(
        apiClient: apiClient,
        storage: secureStorage,
        nowProvider: () => fakeNow,
      );
      await activeManager!.initialize();

      // Refresh succeeds on server, but saveSession throws SecureStorageUnavailableException
      await expectLater(
        () => activeManager!.refreshSession(),
        throwsA(isA<SecureStorageUnavailableException>()),
      );

      expect(httpRefreshDispatches, equals(1));
      expect(activeManager!.status, equals(AuthSessionStatus.expired));
      expect(activeManager!.isRefreshIndeterminate, isTrue);
      expect(activeManager!.currentSession, isNull);

      // Verify that in storage, in-flight record has recorded initialRt
      final inFlight = await secureStorage.loadRefreshInFlightRecord();
      expect(inFlight, isNotNull);
      expect(inFlight!.dispatchedRefreshToken, equals(initialRt));

      // Simulate app restart: cold launch with a new manager instance
      activeManager!.dispose();
      fakeKeystore.throwOnWriteKey = null; // Clear write failure for subsequent reads

      final managerAfterRestart = AuthSessionManager(
        apiClient: apiClient,
        storage: secureStorage,
        nowProvider: () => fakeNow,
      );
      activeManager = managerAfterRestart;

      // loadSession detects the uncommitted in-flight record and blocks the session
      await managerAfterRestart.initialize();
      expect(managerAfterRestart.status, equals(AuthSessionStatus.expired));
      expect(managerAfterRestart.isRefreshIndeterminate, isTrue);
      expect(await managerAfterRestart.getValidAccessToken(), isNull);

      // Subsequent refresh attempt is blocked locally without sending HTTP request
      await expectLater(
        () => managerAfterRestart.refreshSession(),
        throwsA(isA<AegisAuthException>()),
      );
      expect(httpRefreshDispatches, equals(1)); // Still strictly 1 HTTP dispatch!
    });

    test('18. Failure to delete/clear session in secure storage does not hide the error and fails closed', () async {
      final validJwt = buildTestJwt(patientId: 'p_hard_18', deviceId: 'd_hard_18', sequence: 1);
      const rt = 'aegis_rt_hard_18';

      await secureStorage.saveSession(
        AuthSession(
          accessToken: validJwt,
          expiresIn: 900,
          expiresAt: fakeNow.add(const Duration(minutes: 10)),
          refreshToken: rt,
          refreshExpiresAt: fakeNow.add(const Duration(days: 30)),
          patientId: 'p_hard_18',
          deviceId: 'd_hard_18',
          scope: 'vitals:read',
        ),
      );

      // Simulate failure of OS Keystore delete operation
      fakeKeystore.shouldThrowOnDelete = true;
      fakeKeystore.failureMessage = 'Keystore delete failure during revoke';

      final mockClient = MockClient((request) async {
        return http.Response(
          json.encode({
            'status': 'revoked',
            'device_id': 'd_hard_18',
            'revoked': true,
          }),
          200,
        );
      });

      final apiClient = AegisApiClient(client: mockClient);
      activeManager = AuthSessionManager(
        apiClient: apiClient,
        storage: secureStorage,
        nowProvider: () => fakeNow,
      );
      await activeManager!.initialize();

      // Confirmed server revoke succeeds, but storage clear fails:
      // Manager MUST surface SecureStorageUnavailableException and NOT hide it!
      await expectLater(
        () => activeManager!.revokeSession(),
        throwsA(isA<SecureStorageUnavailableException>()),
      );

      // In memory: fail-closed state (session wiped)
      expect(activeManager!.status, equals(AuthSessionStatus.unauthenticated));
      expect(activeManager!.currentSession, isNull);
      expect(activeManager!.lastStorageError, isNotNull);
    });

    test('19. App restart after indeterminate refresh (timeout / socket drop) detects in-flight record and blocks old token reuse', () async {
      final expiredJwt = buildTestJwt(patientId: 'p_hard_19', deviceId: 'd_hard_19', sequence: 1);
      const initialRt = 'aegis_rt_hard_19_timeout';

      await secureStorage.saveSession(
        AuthSession(
          accessToken: expiredJwt,
          expiresIn: 900,
          expiresAt: fakeNow.subtract(const Duration(seconds: 10)),
          refreshToken: initialRt,
          refreshExpiresAt: fakeNow.add(const Duration(days: 30)),
          patientId: 'p_hard_19',
          deviceId: 'd_hard_19',
          scope: 'vitals:read',
        ),
      );

      int refreshDispatches = 0;
      final mockClient = MockClient((request) async {
        refreshDispatches++;
        throw const SocketException('Connection reset by peer after POST /auth/refresh dispatched');
      });

      final apiClient = AegisApiClient(client: mockClient);
      activeManager = AuthSessionManager(
        apiClient: apiClient,
        storage: secureStorage,
        nowProvider: () => fakeNow,
      );
      await activeManager!.initialize();

      // First run: dispatches refresh and encounters socket drop
      final token = await activeManager!.getValidAccessToken();
      expect(token, isNull);
      expect(refreshDispatches, equals(1));
      expect(activeManager!.isRefreshIndeterminate, isTrue);

      // Simulate process kill and cold launch
      activeManager!.dispose();

      final restartedManager = AuthSessionManager(
        apiClient: apiClient,
        storage: secureStorage,
        nowProvider: () => fakeNow,
      );
      activeManager = restartedManager;

      await restartedManager.initialize();
      expect(restartedManager.status, equals(AuthSessionStatus.expired));
      expect(restartedManager.isRefreshIndeterminate, isTrue);
      expect(await restartedManager.getValidAccessToken(), isNull);
      expect(refreshDispatches, equals(1)); // No new HTTP call!
    });

    test('20. After restart, used refresh token is NEVER sent over HTTP even across multiple subsequent calls', () async {
      final expiredJwt = buildTestJwt(patientId: 'p_hard_20', deviceId: 'd_hard_20', sequence: 1);
      const initialRt = 'aegis_rt_hard_20_dead';

      await secureStorage.saveSession(
        AuthSession(
          accessToken: expiredJwt,
          expiresIn: 900,
          expiresAt: fakeNow.subtract(const Duration(seconds: 10)),
          refreshToken: initialRt,
          refreshExpiresAt: fakeNow.add(const Duration(days: 30)),
          patientId: 'p_hard_20',
          deviceId: 'd_hard_20',
          scope: 'vitals:read',
        ),
      );

      // Pre-seed in-flight record to simulate an uncommitted previous refresh
      await secureStorage.markRefreshInFlight(
        deviceId: 'd_hard_20',
        dispatchedRefreshToken: initialRt,
      );

      int refreshDispatches = 0;
      int pairCalls = 0;
      final mockClient = MockClient((request) async {
        if (request.url.path == '/api/v1/auth/refresh') {
          refreshDispatches++;
          return http.Response('Should never be called', 500);
        }
        if (request.url.path == '/api/v1/auth/pair') {
          pairCalls++;
          return http.Response(
            json.encode(
              buildPairOrRefreshPayload(
                accessToken: buildTestJwt(patientId: 'p_hard_20', deviceId: 'd_hard_20', sequence: 2),
                refreshToken: 'aegis_rt_hard_20_new_paired',
                expiresAt: fakeNow.add(const Duration(minutes: 15)),
                refreshExpiresAt: fakeNow.add(const Duration(days: 30)),
                patientId: 'p_hard_20',
                deviceId: 'd_hard_20',
              ),
            ),
            200,
          );
        }
        return http.Response('Not found', 404);
      });

      final apiClient = AegisApiClient(client: mockClient);
      activeManager = AuthSessionManager(
        apiClient: apiClient,
        storage: secureStorage,
        nowProvider: () => fakeNow,
      );
      await activeManager!.initialize();

      // Repeated calls to getValidAccessToken and refreshSession MUST NOT dispatch initialRt
      for (int i = 0; i < 5; i++) {
        expect(await activeManager!.getValidAccessToken(), isNull);
        await expectLater(
          () => activeManager!.refreshSession(),
          throwsA(isA<AegisAuthException>()),
        );
      }
      expect(refreshDispatches, equals(0)); // Zero /refresh dispatches!

      // Re-pairing clears the blocked in-flight state and re-establishes a valid session
      final newSession = await activeManager!.pairDevice(
        DevicePairRequest(
          pairingCode: '042918',
          deviceName: 'Pixel 8',
          deviceId: 'd_hard_20',
        ),
      );
      expect(pairCalls, equals(1));
      expect(activeManager!.status, equals(AuthSessionStatus.authenticated));
      expect(activeManager!.isRefreshIndeterminate, isFalse);
      expect(newSession.refreshToken, equals('aegis_rt_hard_20_new_paired'));
      expect(await activeManager!.getValidAccessToken(), isNotNull);
    });

    test('21. Full diagnostics log sanitization: Keystore failure messages never leak raw causes or secrets', () async {
      const sensitiveToken = 'aegis_rt_secret_leak_attempt_99999';
      const sensitiveJwt = 'eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJzZWNyZXQifQ.c2lnbmF0dXJl';
      const rawPlatformCause = 'CRITICAL_ANDROID_KEYSTORE_HARDWARE_FAILURE_PRIVATE_KEY_UNAVAILABLE';

      final capturedLogs = <String>[];
      final originalDebugPrint = debugPrint;
      debugPrint = (String? message, {int? wrapWidth}) {
        if (message != null) {
          capturedLogs.add(message);
        }
      };

      try {
        fakeKeystore.shouldThrow = true;
        fakeKeystore.failureMessage = '$rawPlatformCause: token=$sensitiveToken jwt=$sensitiveJwt';

        final mockClient = MockClient((request) async => http.Response('OK', 200));
        final apiClient = AegisApiClient(client: mockClient);
        activeManager = AuthSessionManager(
          apiClient: apiClient,
          storage: secureStorage,
          nowProvider: () => fakeNow,
        );

        try {
          await activeManager!.initialize();
        } catch (_) {}

        try {
          await activeManager!.pairDevice(
            DevicePairRequest(pairingCode: '042918', deviceName: 'Phone'),
          );
        } catch (_) {}
      } finally {
        debugPrint = originalDebugPrint;
      }

      expect(capturedLogs, isNotEmpty);
      final combinedLogs = capturedLogs.join('\n');
      expect(combinedLogs.contains(rawPlatformCause), isFalse);
      expect(combinedLogs.contains(sensitiveToken), isFalse);
      expect(combinedLogs.contains(sensitiveJwt), isFalse);
      expect(combinedLogs.contains('042918'), isFalse);
    });

    test('22. Guard read failure + readable stored session -> authorization is NOT restored (fails closed)', () async {
      final validJwt = buildTestJwt(patientId: 'p_v3_22', deviceId: 'd_v3_22', sequence: 1);
      const rt = 'aegis_rt_v3_22';

      await secureStorage.saveSession(
        AuthSession(
          accessToken: validJwt,
          expiresIn: 900,
          expiresAt: fakeNow.add(const Duration(minutes: 15)),
          refreshToken: rt,
          refreshExpiresAt: fakeNow.add(const Duration(days: 30)),
          patientId: 'p_v3_22',
          deviceId: 'd_v3_22',
          scope: 'vitals:read',
        ),
      );

      // Simulate Keystore read error specifically on the guard key
      fakeKeystore.throwOnReadKey = SecureAuthTokenStorage.inFlightStorageKey;
      fakeKeystore.failureMessage = 'Keystore hardware timeout reading inFlightStorageKey';

      final mockClient = MockClient((request) async => http.Response('Should not be called', 500));
      final apiClient = AegisApiClient(client: mockClient);
      activeManager = AuthSessionManager(
        apiClient: apiClient,
        storage: secureStorage,
        nowProvider: () => fakeNow,
      );

      // loadSession fails closed: returns null, sets status to expired, records storage error
      final loaded = await activeManager!.initialize();
      expect(loaded, isNull);
      expect(activeManager!.status, equals(AuthSessionStatus.expired));
      expect(activeManager!.isAuthenticated, isFalse);
      expect(activeManager!.isRefreshIndeterminate, isTrue);
      expect(activeManager!.lastStorageError, isNotNull);
      expect(activeManager!.currentSession, isNull);

      // Guard failure MUST NOT erase the stored session or guard key from storage
      expect(fakeKeystore.values.containsKey(SecureAuthTokenStorage.sessionStorageKey), isTrue);

      // Credentials are NOT returned
      expect(await activeManager!.getValidAccessToken(), isNull);
    });

    test('23. Corrupted JSON syntax in guard record -> throws SecureStorageUnavailableException and DOES NOT delete corrupted file', () async {
      const corruptedJson = '{"syntax_error_unclosed_brace: "d_v3_23"';
      fakeKeystore.values[SecureAuthTokenStorage.inFlightStorageKey] = corruptedJson;

      // Direct read from SecureAuthTokenStorage fails closed
      await expectLater(
        () => secureStorage.loadRefreshInFlightRecord(),
        throwsA(
          isA<SecureStorageUnavailableException>().having(
            (e) => e.message,
            'message',
            contains('Corrupted syntax in persistent refresh guard record.'),
          ),
        ),
      );

      // CRITICAL: The corrupted file MUST NOT be wiped automatically (preserves forensic trail)
      expect(
        fakeKeystore.values[SecureAuthTokenStorage.inFlightStorageKey],
        equals(corruptedJson),
      );
    });

    test('24. Guard deserialization error (invalid fields) -> blocks refresh and keeps session expired', () async {
      final expiredJwt = buildTestJwt(patientId: 'p_v3_24', deviceId: 'd_v3_24', sequence: 1);
      const rt = 'aegis_rt_v3_24';

      await secureStorage.saveSession(
        AuthSession(
          accessToken: expiredJwt,
          expiresIn: 900,
          expiresAt: fakeNow.subtract(const Duration(seconds: 10)),
          refreshToken: rt,
          refreshExpiresAt: fakeNow.add(const Duration(days: 30)),
          patientId: 'p_v3_24',
          deviceId: 'd_v3_24',
          scope: 'vitals:read',
        ),
      );

      // Store a guard record with an unparseable datetime format
      fakeKeystore.values[SecureAuthTokenStorage.inFlightStorageKey] = json.encode({
        'device_id': 'd_v3_24',
        'dispatched_refresh_token': rt,
        'dispatched_at': 'NOT_A_VALID_ISO8601_DATETIME',
      });

      int refreshCalls = 0;
      final mockClient = MockClient((request) async {
        refreshCalls++;
        return http.Response('Should never reach server', 500);
      });

      final apiClient = AegisApiClient(client: mockClient);
      activeManager = AuthSessionManager(
        apiClient: apiClient,
        storage: secureStorage,
        nowProvider: () => fakeNow,
      );

      final loaded = await activeManager!.initialize();
      expect(loaded, isNull);
      expect(activeManager!.status, equals(AuthSessionStatus.expired));
      expect(activeManager!.isRefreshIndeterminate, isTrue);

      // Calling refreshSession or getValidAccessToken MUST NOT dispatch any HTTP calls
      expect(await activeManager!.getValidAccessToken(), isNull);
      await expectLater(
        () => activeManager!.refreshSession(),
        throwsA(isA<AegisAuthException>()),
      );
      expect(refreshCalls, equals(0));
    });

    test('25. Transient KeyStore failure on guard read, then recovery -> safe re-check restores authenticated state', () async {
      final validJwt = buildTestJwt(patientId: 'p_v3_25', deviceId: 'd_v3_25', sequence: 1);
      const rt = 'aegis_rt_v3_25';

      await secureStorage.saveSession(
        AuthSession(
          accessToken: validJwt,
          expiresIn: 900,
          expiresAt: fakeNow.add(const Duration(minutes: 15)),
          refreshToken: rt,
          refreshExpiresAt: fakeNow.add(const Duration(days: 30)),
          patientId: 'p_v3_25',
          deviceId: 'd_v3_25',
          scope: 'vitals:read',
        ),
      );

      // Step 1: Temporary KeyStore lockup causes guard read failure
      fakeKeystore.throwOnReadKey = SecureAuthTokenStorage.inFlightStorageKey;

      final mockClient = MockClient((request) async => http.Response('OK', 200));
      final apiClient = AegisApiClient(client: mockClient);
      activeManager = AuthSessionManager(
        apiClient: apiClient,
        storage: secureStorage,
        nowProvider: () => fakeNow,
      );

      await activeManager!.initialize();
      expect(activeManager!.status, equals(AuthSessionStatus.expired));
      expect(activeManager!.isAuthenticated, isFalse);
      expect(await activeManager!.getValidAccessToken(), isNull);

      // Step 2: KeyStore recovers (temporary fault resolved)
      fakeKeystore.throwOnReadKey = null;

      // Safe re-check via loadSession()
      final reloaded = await activeManager!.loadSession();
      expect(reloaded, isNotNull);
      expect(activeManager!.status, equals(AuthSessionStatus.authenticated));
      expect(activeManager!.isAuthenticated, isTrue);
      expect(activeManager!.isRefreshIndeterminate, isFalse);
      expect(await activeManager!.getValidAccessToken(), equals(validJwt));
    });

    test('26. Old refresh token is NEVER sent over network when guard state is indeterminate/unverified', () async {
      final expiredJwt = buildTestJwt(patientId: 'p_v3_26', deviceId: 'd_v3_26', sequence: 1);
      const initialRt = 'aegis_rt_v3_26_unverified';

      await secureStorage.saveSession(
        AuthSession(
          accessToken: expiredJwt,
          expiresIn: 900,
          expiresAt: fakeNow.subtract(const Duration(seconds: 10)),
          refreshToken: initialRt,
          refreshExpiresAt: fakeNow.add(const Duration(days: 30)),
          patientId: 'p_v3_26',
          deviceId: 'd_v3_26',
          scope: 'vitals:read',
        ),
      );

      // Indeterminate guard state (read throws)
      fakeKeystore.throwOnReadKey = SecureAuthTokenStorage.inFlightStorageKey;

      int httpRefreshDispatches = 0;
      final mockClient = MockClient((request) async {
        httpRefreshDispatches++;
        return http.Response('Disaster: token was sent!', 500);
      });

      final apiClient = AegisApiClient(client: mockClient);
      activeManager = AuthSessionManager(
        apiClient: apiClient,
        storage: secureStorage,
        nowProvider: () => fakeNow,
      );
      await activeManager!.initialize();

      // Launch 5 parallel attempts while guard state is unverified
      final results = await Future.wait(
        List.generate(5, (_) => activeManager!.getValidAccessToken()),
      );

      for (final res in results) {
        expect(res, isNull);
      }
      expect(httpRefreshDispatches, equals(0)); // Strictly 0 requests!
    });

    test('27. Final guard hardening: absent key -> null; empty string or whitespace -> error, record preserved, blocks refresh', () async {
      // 1. Physically absent key -> null
      fakeKeystore.values.remove(SecureAuthTokenStorage.inFlightStorageKey);
      final absentRecord = await secureStorage.loadRefreshInFlightRecord();
      expect(absentRecord, isNull);

      final inMemAbsent = InMemoryAuthTokenStorage();
      expect(await inMemAbsent.loadRefreshInFlightRecord(), isNull);

      // 2. Empty string -> throws SecureStorageUnavailableException
      fakeKeystore.values[SecureAuthTokenStorage.inFlightStorageKey] = '';
      await expectLater(
        () => secureStorage.loadRefreshInFlightRecord(),
        throwsA(
          isA<SecureStorageUnavailableException>().having(
            (e) => e.message,
            'message',
            contains('Empty or corrupted persistent refresh guard record.'),
          ),
        ),
      );
      // Verify damaged record is preserved on disk
      expect(fakeKeystore.values[SecureAuthTokenStorage.inFlightStorageKey], equals(''));

      final inMemEmpty = InMemoryAuthTokenStorage(rawInFlightRecord: '');
      await expectLater(
        () => inMemEmpty.loadRefreshInFlightRecord(),
        throwsA(
          isA<SecureStorageUnavailableException>().having(
            (e) => e.message,
            'message',
            contains('Empty or corrupted persistent refresh guard record.'),
          ),
        ),
      );

      // 3. Whitespace string -> throws SecureStorageUnavailableException
      const whitespaceGuard = '   \t\n  ';
      fakeKeystore.values[SecureAuthTokenStorage.inFlightStorageKey] = whitespaceGuard;
      await expectLater(
        () => secureStorage.loadRefreshInFlightRecord(),
        throwsA(
          isA<SecureStorageUnavailableException>().having(
            (e) => e.message,
            'message',
            contains('Empty or corrupted persistent refresh guard record.'),
          ),
        ),
      );
      // Verify damaged record is preserved on disk
      expect(fakeKeystore.values[SecureAuthTokenStorage.inFlightStorageKey], equals(whitespaceGuard));

      final inMemWhitespace = InMemoryAuthTokenStorage(rawInFlightRecord: whitespaceGuard);
      await expectLater(
        () => inMemWhitespace.loadRefreshInFlightRecord(),
        throwsA(
          isA<SecureStorageUnavailableException>().having(
            (e) => e.message,
            'message',
            contains('Empty or corrupted persistent refresh guard record.'),
          ),
        ),
      );

      // 4. AuthSessionManager.loadSession() with empty or whitespace guard does NOT restore session and does NOT send refresh
      final expiredJwt = buildTestJwt(patientId: 'p_v3_27', deviceId: 'd_v3_27', sequence: 1);
      const rt = 'aegis_rt_v3_27';
      await secureStorage.saveSession(
        AuthSession(
          accessToken: expiredJwt,
          expiresIn: 900,
          expiresAt: fakeNow.subtract(const Duration(seconds: 10)),
          refreshToken: rt,
          refreshExpiresAt: fakeNow.add(const Duration(days: 30)),
          patientId: 'p_v3_27',
          deviceId: 'd_v3_27',
          scope: 'vitals:read',
        ),
      );

      int refreshCalls = 0;
      final mockClient = MockClient((request) async {
        refreshCalls++;
        return http.Response('Should never reach server', 500);
      });
      final apiClient = AegisApiClient(client: mockClient);
      activeManager = AuthSessionManager(
        apiClient: apiClient,
        storage: secureStorage,
        nowProvider: () => fakeNow,
      );

      // Test with whitespace guard
      fakeKeystore.values[SecureAuthTokenStorage.inFlightStorageKey] = '   ';
      final loadedWs = await activeManager!.initialize();
      expect(loadedWs, isNull);
      expect(activeManager!.status, equals(AuthSessionStatus.expired));
      expect(activeManager!.isAuthenticated, isFalse);
      expect(activeManager!.isRefreshIndeterminate, isTrue);
      expect(
        activeManager!.lastStorageError?.message,
        contains('Empty or corrupted persistent refresh guard record.'),
      );
      // Guard and session remain in keystore
      expect(fakeKeystore.values[SecureAuthTokenStorage.inFlightStorageKey], equals('   '));
      expect(fakeKeystore.values.containsKey(SecureAuthTokenStorage.sessionStorageKey), isTrue);

      // Refresh attempts are completely blocked without network calls
      expect(await activeManager!.getValidAccessToken(), isNull);
      await expectLater(
        () => activeManager!.refreshSession(),
        throwsA(isA<AegisAuthException>()),
      );
      expect(refreshCalls, equals(0));

      // Test with empty string guard
      fakeKeystore.values[SecureAuthTokenStorage.inFlightStorageKey] = '';
      final loadedEmpty = await activeManager!.loadSession();
      expect(loadedEmpty, isNull);
      expect(activeManager!.status, equals(AuthSessionStatus.expired));
      expect(activeManager!.isAuthenticated, isFalse);
      expect(activeManager!.isRefreshIndeterminate, isTrue);
      expect(await activeManager!.getValidAccessToken(), isNull);
      await expectLater(
        () => activeManager!.refreshSession(),
        throwsA(isA<AegisAuthException>()),
      );
      expect(refreshCalls, equals(0));
      expect(fakeKeystore.values[SecureAuthTokenStorage.inFlightStorageKey], equals(''));
    });

    test('28. Final storage cleanup invariant: clearSession() failure / partial failure forbids deleting guard, preserves fail-closed across restart', () async {
      int httpRefreshCalls = 0;
      final mockClient = MockClient((request) async {
        if (request.url.path.contains('/auth/revoke')) {
          return http.Response(
            json.encode({
              'device_id': 'd28',
              'revoked': true,
              'status': 'revoked',
            }),
            200,
            headers: {'content-type': 'application/json'},
          );
        }
        httpRefreshCalls++;
        return http.Response('Disaster: old token reached server!', 500);
      });
      final apiClient = AegisApiClient(client: mockClient);

      // --- Scenario 1: clearSession() throws without deleting session ---
      const rt1 = 'aegis_rt_test28_1';
      final jwt1 = buildTestJwt(patientId: 'p28_1', deviceId: 'd28_1', sequence: 1);
      await secureStorage.saveSession(AuthSession(
        accessToken: jwt1,
        expiresIn: 900,
        expiresAt: fakeNow.subtract(const Duration(seconds: 10)),
        refreshToken: rt1,
        refreshExpiresAt: fakeNow.add(const Duration(days: 30)),
        patientId: 'p28_1',
        deviceId: 'd28_1',
        scope: 'vitals:read',
      ));
      await secureStorage.markRefreshInFlight(deviceId: 'd28_1', dispatchedRefreshToken: rt1);

      // Set delete to throw on sessionStorageKey (session is NOT removed)
      fakeKeystore.throwOnDeleteKey = SecureAuthTokenStorage.sessionStorageKey;
      activeManager = AuthSessionManager(
        apiClient: apiClient,
        storage: secureStorage,
        nowProvider: () => fakeNow,
      );
      await activeManager!.initialize();

      // Revoke attempt fails because clearSession throws
      await expectLater(
        () => activeManager!.revokeSession(),
        throwsA(isA<SecureStorageUnavailableException>()),
      );

      // INVARIANT 1: Guard MUST NOT be deleted when clearSession() failed!
      expect(fakeKeystore.values.containsKey(SecureAuthTokenStorage.inFlightStorageKey), isTrue);
      expect(fakeKeystore.values.containsKey(SecureAuthTokenStorage.sessionStorageKey), isTrue);

      // Restart after failure
      fakeKeystore.throwOnDeleteKey = null;
      activeManager?.dispose();
      activeManager = null;
      var restartedManager = AuthSessionManager(
        apiClient: apiClient,
        storage: secureStorage,
        nowProvider: () => fakeNow,
      );
      final reloaded1 = await restartedManager.initialize();
      expect(reloaded1, isNotNull);
      expect(restartedManager.status, equals(AuthSessionStatus.expired));
      expect(restartedManager.isRefreshIndeterminate, isTrue);
      // Stored token is blocked
      expect(await restartedManager.getValidAccessToken(), isNull);
      await expectLater(
        () => restartedManager.refreshSession(),
        throwsA(isA<AegisAuthException>()),
      );
      expect(httpRefreshCalls, equals(0)); // 0 calls!
      restartedManager.dispose();

      // --- Scenario 2: clearSession() partially deletes/corrupts session and throws ---
      const rt2 = 'aegis_rt_test28_2';
      final jwt2 = buildTestJwt(patientId: 'p28_2', deviceId: 'd28_2', sequence: 1);
      fakeKeystore.values.clear();
      await secureStorage.saveSession(AuthSession(
        accessToken: jwt2,
        expiresIn: 900,
        expiresAt: fakeNow.subtract(const Duration(seconds: 10)),
        refreshToken: rt2,
        refreshExpiresAt: fakeNow.add(const Duration(days: 30)),
        patientId: 'p28_2',
        deviceId: 'd28_2',
        scope: 'vitals:read',
      ));
      await secureStorage.markRefreshInFlight(deviceId: 'd28_2', dispatchedRefreshToken: rt2);

      // Hook corrupts session data on delete and throws
      fakeKeystore.onDeleteHook = (key) {
        if (key == SecureAuthTokenStorage.sessionStorageKey) {
          fakeKeystore.values[key] = '{"patient_id": "p28_corrupted_partial_data"';
          throw Exception('Hardware failure during partial session delete');
        }
      };

      activeManager = AuthSessionManager(
        apiClient: apiClient,
        storage: secureStorage,
        nowProvider: () => fakeNow,
      );
      await activeManager!.initialize();

      await expectLater(
        () => activeManager!.revokeSession(),
        throwsA(isA<SecureStorageUnavailableException>()),
      );

      // INVARIANT 2: Guard MUST NOT be deleted when session delete was partial/failed
      expect(fakeKeystore.values.containsKey(SecureAuthTokenStorage.inFlightStorageKey), isTrue);

      // Restart after partial delete failure
      fakeKeystore.onDeleteHook = null;
      activeManager?.dispose();
      activeManager = null;
      restartedManager = AuthSessionManager(
        apiClient: apiClient,
        storage: secureStorage,
        nowProvider: () => fakeNow,
      );
      final reloaded2 = await restartedManager.initialize();
      expect(reloaded2, isNull);
      expect(restartedManager.status, equals(AuthSessionStatus.unauthenticated));
      expect(await restartedManager.getValidAccessToken(), isNull);
      await expectLater(
        () => restartedManager.refreshSession(),
        throwsA(isA<AegisAuthException>()),
      );
      expect(httpRefreshCalls, equals(0)); // Strictly 0 calls!
      restartedManager.dispose();

      // --- Scenario 3: clearSession() succeeds, but clearRefreshInFlightRecord() fails ---
      const rt3 = 'aegis_rt_test28_3';
      final jwt3 = buildTestJwt(patientId: 'p28_3', deviceId: 'd28_3', sequence: 1);
      fakeKeystore.values.clear();
      await secureStorage.saveSession(AuthSession(
        accessToken: jwt3,
        expiresIn: 900,
        expiresAt: fakeNow.subtract(const Duration(seconds: 10)),
        refreshToken: rt3,
        refreshExpiresAt: fakeNow.add(const Duration(days: 30)),
        patientId: 'p28_3',
        deviceId: 'd28_3',
        scope: 'vitals:read',
      ));
      await secureStorage.markRefreshInFlight(deviceId: 'd28_3', dispatchedRefreshToken: rt3);

      fakeKeystore.throwOnDeleteKey = SecureAuthTokenStorage.inFlightStorageKey;
      activeManager = AuthSessionManager(
        apiClient: apiClient,
        storage: secureStorage,
        nowProvider: () => fakeNow,
      );
      await activeManager!.initialize();

      await expectLater(
        () => activeManager!.revokeSession(),
        throwsA(isA<SecureStorageUnavailableException>()),
      );

      // Session is reliably deleted, guard is still present
      expect(fakeKeystore.values.containsKey(SecureAuthTokenStorage.sessionStorageKey), isFalse);
      expect(fakeKeystore.values.containsKey(SecureAuthTokenStorage.inFlightStorageKey), isTrue);

      // Restart after failure
      fakeKeystore.throwOnDeleteKey = null;
      activeManager?.dispose();
      activeManager = null;
      restartedManager = AuthSessionManager(
        apiClient: apiClient,
        storage: secureStorage,
        nowProvider: () => fakeNow,
      );
      final reloaded3 = await restartedManager.initialize();
      expect(reloaded3, isNull);
      expect(restartedManager.status, equals(AuthSessionStatus.unauthenticated));
      expect(await restartedManager.getValidAccessToken(), isNull);
      expect(httpRefreshCalls, equals(0));
      restartedManager.dispose();
    });

    test('29. Final storage cleanup invariant: rotation verification & pairing recovery invariants', () async {
      const oldRt = 'aegis_rt_test29_old';
      final oldJwt = buildTestJwt(patientId: 'p29', deviceId: 'd29', sequence: 1);
      final newJwt = buildTestJwt(patientId: 'p29', deviceId: 'd29', sequence: 2);
      const newRt = 'aegis_rt_test29_new';

      fakeKeystore.values.clear();
      fakeKeystore.throwOnDeleteKey = null;
      fakeKeystore.throwOnReadKey = null;
      fakeKeystore.throwOnWriteKey = null;
      fakeKeystore.onDeleteHook = null;

      await secureStorage.saveSession(AuthSession(
        accessToken: oldJwt,
        expiresIn: 900,
        expiresAt: fakeNow.subtract(const Duration(seconds: 10)),
        refreshToken: oldRt,
        refreshExpiresAt: fakeNow.add(const Duration(days: 30)),
        patientId: 'p29',
        deviceId: 'd29',
        scope: 'vitals:read',
      ));

      final mockClient = MockClient((request) async {
        if (request.url.path.contains('/auth/refresh')) {
          return http.Response(
            json.encode({
              'access_token': newJwt,
              'token_type': 'bearer',
              'expires_in': 900,
              'expires_at': fakeNow.add(const Duration(minutes: 15)).toIso8601String(),
              'refresh_token': newRt,
              'refresh_expires_at': fakeNow.add(const Duration(days: 30)).toIso8601String(),
              'patient_id': 'p29',
              'device_id': 'd29',
              'scope': 'vitals:read',
            }),
            200,
            headers: {'content-type': 'application/json'},
          );
        }
        if (request.url.path.contains('/auth/pair')) {
          return http.Response(
            json.encode({
              'access_token': buildTestJwt(patientId: 'p29_new', deviceId: 'd29_new', sequence: 10),
              'token_type': 'bearer',
              'expires_in': 900,
              'expires_at': fakeNow.add(const Duration(minutes: 15)).toIso8601String(),
              'refresh_token': 'aegis_rt_paired_new',
              'refresh_expires_at': fakeNow.add(const Duration(days: 30)).toIso8601String(),
              'patient_id': 'p29_new',
              'device_id': 'd29_new',
              'scope': 'vitals:read',
            }),
            200,
            headers: {'content-type': 'application/json'},
          );
        }
        return http.Response('Not Found', 404);
      });

      final apiClient = AegisApiClient(client: mockClient);
      activeManager = AuthSessionManager(
        apiClient: apiClient,
        storage: secureStorage,
        nowProvider: () => fakeNow,
      );
      await activeManager!.initialize();

      // Step 1: Verification failure after rotation -> guard with oldRt MUST be preserved
      // Fail read verification of the saved rotated session
      fakeKeystore.throwOnReadKey = SecureAuthTokenStorage.sessionStorageKey;

      await expectLater(
        () => activeManager!.refreshSession(),
        throwsA(isA<SecureStorageUnavailableException>()),
      );

      // Guard MUST remain in keystore to prevent reuse of oldRt
      expect(fakeKeystore.values.containsKey(SecureAuthTokenStorage.inFlightStorageKey), isTrue);
      expect(activeManager!.status, equals(AuthSessionStatus.expired));
      expect(activeManager!.isRefreshIndeterminate, isTrue);

      // Step 2: Successful re-pairing clears the old guard and restores authenticated state
      fakeKeystore.throwOnReadKey = null;
      final pairedSession = await activeManager!.pairDevice(
        DevicePairRequest(
          pairingCode: '123456',
          deviceName: 'Pixel 8 Pro',
          platform: 'android',
        ),
      );

      expect(pairedSession.deviceId, equals('d29_new'));
      expect(activeManager!.status, equals(AuthSessionStatus.authenticated));
      expect(activeManager!.isAuthenticated, isTrue);
      expect(activeManager!.isRefreshIndeterminate, isFalse);

      // Old guard was safely cleared
      expect(fakeKeystore.values.containsKey(SecureAuthTokenStorage.inFlightStorageKey), isFalse);

      // Step 3: Restart app after pairing restores cleanly without false blocking
      activeManager?.dispose();
      activeManager = null;
      final restartedManager = AuthSessionManager(
        apiClient: apiClient,
        storage: secureStorage,
        nowProvider: () => fakeNow,
      );
      final reloaded = await restartedManager.initialize();
      expect(reloaded, isNotNull);
      expect(reloaded!.deviceId, equals('d29_new'));
      expect(restartedManager.status, equals(AuthSessionStatus.authenticated));
      expect(restartedManager.isAuthenticated, isTrue);
      restartedManager.dispose();
    });

    test('30. Single-Flight concurrency invariant: 5 concurrent refresh calls trigger strictly 1 HTTP POST /refresh across all storage hardening', () async {
      int httpRefreshCount = 0;
      final mockClient = MockClient((request) async {
        if (request.url.path.contains('/auth/refresh')) {
          httpRefreshCount++;
          await Future.delayed(const Duration(milliseconds: 30));
          final rotatedJwt = buildTestJwt(patientId: 'p30', deviceId: 'd30', sequence: 2);
          return http.Response(
            json.encode({
              'access_token': rotatedJwt,
              'token_type': 'bearer',
              'expires_in': 900,
              'expires_at': fakeNow.add(const Duration(minutes: 15)).toIso8601String(),
              'refresh_token': 'rt_rotated_30',
              'refresh_expires_at': fakeNow.add(const Duration(days: 30)).toIso8601String(),
              'patient_id': 'p30',
              'device_id': 'd30',
              'scope': 'vitals:read',
            }),
            200,
            headers: {'content-type': 'application/json'},
          );
        }
        return http.Response('Not Found', 404);
      });

      fakeKeystore.values.clear();
      fakeKeystore.throwOnDeleteKey = null;
      fakeKeystore.throwOnReadKey = null;
      fakeKeystore.throwOnWriteKey = null;
      fakeKeystore.onDeleteHook = null;

      final apiClient = AegisApiClient(client: mockClient);
      final initialJwt = buildTestJwt(patientId: 'p30', deviceId: 'd30', sequence: 1);
      await secureStorage.saveSession(AuthSession(
        accessToken: initialJwt,
        expiresIn: 900,
        expiresAt: fakeNow.subtract(const Duration(seconds: 5)),
        refreshToken: 'rt_initial_30',
        refreshExpiresAt: fakeNow.add(const Duration(days: 30)),
        patientId: 'p30',
        deviceId: 'd30',
        scope: 'vitals:read',
      ));

      activeManager = AuthSessionManager(
        apiClient: apiClient,
        storage: secureStorage,
        nowProvider: () => fakeNow,
      );
      await activeManager!.initialize();

      // Launch 5 parallel requests
      final results = await Future.wait([
        activeManager!.getValidAccessToken(),
        activeManager!.getValidAccessToken(),
        activeManager!.refreshSession(),
        activeManager!.getValidAccessToken(),
        activeManager!.refreshSession(),
      ]);

      expect(httpRefreshCount, equals(1)); // Strictly 1 HTTP call!
      for (final res in results) {
        expect(res, isNotNull);
        expect(res, contains('.'));
      }
      expect(activeManager!.status, equals(AuthSessionStatus.authenticated));
      expect(activeManager!.isAuthenticated, isTrue);
    });
  });
}
