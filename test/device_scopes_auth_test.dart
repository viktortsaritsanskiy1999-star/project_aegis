import 'dart:async';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';

import 'package:aegis_health/services/api/aegis_api_client.dart';
import 'package:aegis_health/services/api/vitals_api_models.dart';
import 'package:aegis_health/services/auth/auth_models.dart';
import 'package:aegis_health/services/auth/auth_session_manager.dart';
import 'package:aegis_health/services/auth/auth_token_storage.dart';

// In-memory test storage implementing IAuthTokenStorage
class MemoryTestStorage implements IAuthTokenStorage {
  AuthSession? session;
  RefreshInFlightRecord? inFlightRecord;

  @override
  Future<void> saveSession(AuthSession session) async {
    this.session = session;
  }

  @override
  Future<AuthSession?> loadSession() async => session;

  @override
  Future<void> clearSession() async {
    session = null;
  }

  @override
  Future<void> markRefreshInFlight({
    required String deviceId,
    required String dispatchedRefreshToken,
  }) async {
    inFlightRecord = RefreshInFlightRecord(
      deviceId: deviceId,
      dispatchedRefreshToken: dispatchedRefreshToken,
      dispatchedAt: DateTime.now().toUtc(),
    );
  }

  @override
  Future<RefreshInFlightRecord?> loadRefreshInFlightRecord() async => inFlightRecord;

  @override
  Future<void> clearRefreshInFlightRecord() async {
    inFlightRecord = null;
  }
}

// Helpers to mint valid dummy HS256 JWTs with arbitrary claims
String _createMockJwt({
  required String patientId,
  required String deviceId,
  required String scope,
  int ttlSeconds = 1800,
}) {
  const header = 'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9'; // {"alg":"HS256","typ":"JWT"}
  final now = DateTime.now().toUtc();
  final exp = now.add(Duration(seconds: ttlSeconds)).millisecondsSinceEpoch ~/ 1000;
  final iat = now.millisecondsSinceEpoch ~/ 1000;

  final payloadJson = '{"iss":"aegis-backend","aud":"aegis-mobile-client",'
      '"sub":"$patientId","patient_id":"$patientId","device_id":"$deviceId",'
      '"jti":"mock-jti-uuid","family_id":"mock-fam-uuid","scope":"$scope",'
      '"iat":$iat,"exp":$exp}';

  // Simple deterministic base64 mock
  final bytes = payloadJson.codeUnits;
  final b64 = _base64UrlEncode(bytes);
  return '$header.$b64.mock_signature_bytes_for_unit_tests';
}

String _base64UrlEncode(List<int> bytes) {
  const chars = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_';
  final buffer = StringBuffer();
  for (int i = 0; i < bytes.length; i += 3) {
    final b0 = bytes[i];
    final b1 = i + 1 < bytes.length ? bytes[i + 1] : 0;
    final b2 = i + 2 < bytes.length ? bytes[i + 2] : 0;

    buffer.write(chars[(b0 >> 2) & 0x3F]);
    buffer.write(chars[((b0 & 0x03) << 4) | ((b1 >> 4) & 0x0F)]);
    if (i + 1 < bytes.length) {
      buffer.write(chars[((b1 & 0x0F) << 2) | ((b2 >> 6) & 0x03)]);
    }
    if (i + 2 < bytes.length) {
      buffer.write(chars[b2 & 0x3F]);
    }
  }
  return buffer.toString();
}

void main() {
  setUp(() {
    AuthSessionManager.resetActiveInstancesForTesting();
  });

  tearDown(() {
    AuthSessionManager.resetActiveInstancesForTesting();
  });

  group('Stage 5.4D-1: Device Scopes & Auth Compatibility', () {
    test('1. AegisScopes constants define all standard and legacy scopes', () {
      expect(AegisScopes.vitalsRead, equals('vitals:read'));
      expect(AegisScopes.vitalsWrite, equals('vitals:write'));
      expect(AegisScopes.cycleRead, equals('cycle:read'));
      expect(AegisScopes.cascadeIntake, equals('cascade:intake'));
      expect(AegisScopes.cascadeRun, equals('cascade:run'));
      expect(AegisScopes.reportsRead, equals('reports:read'));
      expect(AegisScopes.chatRead, equals('chat:read'));
      expect(AegisScopes.chatWrite, equals('chat:write'));

      expect(AegisScopes.standardDeviceScopes.length, equals(8));
      expect(
        AegisScopes.standardDeviceScopes,
        containsAll([
          'vitals:read',
          'vitals:write',
          'cycle:read',
          'cascade:intake',
          'cascade:run',
          'reports:read',
          'chat:read',
          'chat:write',
        ]),
      );

      expect(AegisScopes.legacyStage53Scopes.length, equals(3));
      expect(
        AegisScopes.legacyStage53Scopes,
        containsAll(['vitals:read', 'vitals:write', 'cycle:read']),
      );
    });

    test('2. AuthSession correctly evaluates standard vs legacy scopes', () {
      final legacySession = AuthSession(
        accessToken: _createMockJwt(
          patientId: 'pat-1',
          deviceId: 'dev-1',
          scope: 'vitals:read vitals:write cycle:read',
        ),
        expiresIn: 1800,
        expiresAt: DateTime.now().toUtc().add(const Duration(minutes: 30)),
        refreshToken: 'rf-leg-1',
        refreshExpiresAt: DateTime.now().toUtc().add(const Duration(days: 30)),
        patientId: 'pat-1',
        deviceId: 'dev-1',
        scope: 'vitals:read vitals:write cycle:read',
      );

      expect(legacySession.hasStandardDeviceScopes, isFalse);
      expect(
        legacySession.missingScopes(),
        equals({
          'cascade:intake',
          'cascade:run',
          'reports:read',
          'chat:read',
          'chat:write',
        }),
      );

      final fullSession = AuthSession(
        accessToken: _createMockJwt(
          patientId: 'pat-1',
          deviceId: 'dev-1',
          scope:
              'vitals:read vitals:write cycle:read cascade:intake cascade:run reports:read chat:read chat:write',
        ),
        expiresIn: 1800,
        expiresAt: DateTime.now().toUtc().add(const Duration(minutes: 30)),
        refreshToken: 'rf-full-1',
        refreshExpiresAt: DateTime.now().toUtc().add(const Duration(days: 30)),
        patientId: 'pat-1',
        deviceId: 'dev-1',
        scope:
            'vitals:read vitals:write cycle:read cascade:intake cascade:run reports:read chat:read chat:write',
      );

      expect(fullSession.hasStandardDeviceScopes, isTrue);
      expect(fullSession.missingScopes(), isEmpty);
    });

    test('3. AuthSessionManager.needsScopeUpgrade indicates whether rotation is needed', () async {
      final storage = MemoryTestStorage();
      final mockClient = MockClient((request) async => http.Response('{}', 200));
      final apiClient = AegisApiClient(
        baseUrl: 'http://127.0.0.1:8000',
        client: mockClient,
      );

      final manager = AuthSessionManager(
        apiClient: apiClient,
        storage: storage,
      );

      // Unauthenticated -> needsScopeUpgrade is false
      expect(manager.needsScopeUpgrade, isFalse);

      // Set legacy Stage 5.3 session
      final legacySession = AuthSession(
        accessToken: _createMockJwt(
          patientId: 'pat-leg-1',
          deviceId: 'dev-leg-1',
          scope: 'vitals:read vitals:write cycle:read',
        ),
        expiresIn: 1800,
        expiresAt: DateTime.now().toUtc().add(const Duration(minutes: 30)),
        refreshToken: 'rf-leg-1',
        refreshExpiresAt: DateTime.now().toUtc().add(const Duration(days: 30)),
        patientId: 'pat-leg-1',
        deviceId: 'dev-leg-1',
        scope: 'vitals:read vitals:write cycle:read',
      );
      manager.setSessionForTesting(legacySession, status: AuthSessionStatus.authenticated);

      expect(manager.needsScopeUpgrade, isTrue);
      expect(manager.hasAttemptedScopeUpgrade, isFalse);
    });

    test('4. ensureRequiredScopes() upgrades legacy device permissions via single-flight refresh', () async {
      final storage = MemoryTestStorage();
      int refreshCallCount = 0;

      final standardScopeStr =
          'vitals:read vitals:write cycle:read cascade:intake cascade:run reports:read chat:read chat:write';
      final upgradedJwt = _createMockJwt(
        patientId: 'pat-upgrade-01',
        deviceId: 'dev-upgrade-01',
        scope: standardScopeStr,
      );

      final mockClient = MockClient((request) async {
        if (request.url.path == '/api/v1/auth/refresh') {
          refreshCallCount++;
          return http.Response(
            '{"access_token":"$upgradedJwt","token_type":"Bearer","expires_in":1800,'
            '"expires_at":"${DateTime.now().toUtc().add(const Duration(minutes: 30)).toIso8601String()}",'
            '"refresh_token":"rf-upgraded-token-999",'
            '"refresh_expires_at":"${DateTime.now().toUtc().add(const Duration(days: 30)).toIso8601String()}",'
            '"patient_id":"pat-upgrade-01","device_id":"dev-upgrade-01",'
            '"scope":"$standardScopeStr"}',
            200,
            headers: {'cache-control': 'no-store'},
          );
        }
        return http.Response('{"detail":"Not Found"}', 404);
      });

      final apiClient = AegisApiClient(
        baseUrl: 'http://127.0.0.1:8000',
        client: mockClient,
      );

      final manager = AuthSessionManager(
        apiClient: apiClient,
        storage: storage,
      );

      // Pre-load legacy session
      final legacySession = AuthSession(
        accessToken: _createMockJwt(
          patientId: 'pat-upgrade-01',
          deviceId: 'dev-upgrade-01',
          scope: 'vitals:read vitals:write cycle:read',
        ),
        expiresIn: 1800,
        expiresAt: DateTime.now().toUtc().add(const Duration(minutes: 30)),
        refreshToken: 'rf-old-01',
        refreshExpiresAt: DateTime.now().toUtc().add(const Duration(days: 30)),
        patientId: 'pat-upgrade-01',
        deviceId: 'dev-upgrade-01',
        scope: 'vitals:read vitals:write cycle:read',
      );
      manager.setSessionForTesting(legacySession, status: AuthSessionStatus.authenticated);

      expect(manager.needsScopeUpgrade, isTrue);

      // Execute ensureRequiredScopes
      final upgraded = await manager.ensureRequiredScopes();
      expect(upgraded, isTrue);
      expect(refreshCallCount, equals(1));
      expect(manager.needsScopeUpgrade, isFalse);
      expect(manager.currentSession!.hasStandardDeviceScopes, isTrue);
      expect(manager.currentSession!.hasScope(AegisScopes.cascadeIntake), isTrue);
      expect(manager.currentSession!.hasScope(AegisScopes.chatWrite), isTrue);

      // Calling ensureRequiredScopes again does NOT call refresh because scopes are now present
      final secondCheck = await manager.ensureRequiredScopes();
      expect(secondCheck, isTrue);
      expect(refreshCallCount, equals(1)); // zero extra network calls!
    });

    test('5. Loop prevention: ensureRequiredScopes() never enters infinite loop if server returns unchanged scopes', () async {
      final storage = MemoryTestStorage();
      int refreshCallCount = 0;

      // Mock server returns same old scopes
      final mockClient = MockClient((request) async {
        if (request.url.path == '/api/v1/auth/refresh') {
          refreshCallCount++;
          return http.Response(
            '{"access_token":"${_createMockJwt(patientId: 'pat-loop', deviceId: 'dev-loop', scope: 'vitals:read')}",'
            '"token_type":"Bearer","expires_in":1800,'
            '"expires_at":"${DateTime.now().toUtc().add(const Duration(minutes: 30)).toIso8601String()}",'
            '"refresh_token":"rf-token-$refreshCallCount",'
            '"refresh_expires_at":"${DateTime.now().toUtc().add(const Duration(days: 30)).toIso8601String()}",'
            '"patient_id":"pat-loop","device_id":"dev-loop",'
            '"scope":"vitals:read"}',
            200,
            headers: {'cache-control': 'no-store'},
          );
        }
        return http.Response('{"detail":"Not Found"}', 404);
      });

      final apiClient = AegisApiClient(
        baseUrl: 'http://127.0.0.1:8000',
        client: mockClient,
      );

      final manager = AuthSessionManager(
        apiClient: apiClient,
        storage: storage,
      );

      final initialSession = AuthSession(
        accessToken: _createMockJwt(
          patientId: 'pat-loop',
          deviceId: 'dev-loop',
          scope: 'vitals:read',
        ),
        expiresIn: 1800,
        expiresAt: DateTime.now().toUtc().add(const Duration(minutes: 30)),
        refreshToken: 'rf-initial-01',
        refreshExpiresAt: DateTime.now().toUtc().add(const Duration(days: 30)),
        patientId: 'pat-loop',
        deviceId: 'dev-loop',
        scope: 'vitals:read',
      );
      manager.setSessionForTesting(initialSession, status: AuthSessionStatus.authenticated);

      // First attempt tries refresh
      final firstResult = await manager.ensureRequiredScopes();
      expect(firstResult, isFalse);
      expect(refreshCallCount, equals(1));
      expect(manager.hasAttemptedScopeUpgrade, isTrue);

      // Second attempt immediately returns false without calling /refresh (strictly prevents infinite loop)
      final secondResult = await manager.ensureRequiredScopes();
      expect(secondResult, isFalse);
      expect(refreshCallCount, equals(1)); // NO extra refresh call!
    });

    test('6. Concurrent callers join the same Single-Flight scope upgrade refresh', () async {
      final storage = MemoryTestStorage();
      int refreshCallCount = 0;
      final completer = Completer<http.Response>();

      final standardScopeStr =
          'vitals:read vitals:write cycle:read cascade:intake cascade:run reports:read chat:read chat:write';
      final upgradedJwt = _createMockJwt(
        patientId: 'pat-conc-01',
        deviceId: 'dev-conc-01',
        scope: standardScopeStr,
      );

      final mockClient = MockClient((request) async {
        if (request.url.path == '/api/v1/auth/refresh') {
          refreshCallCount++;
          return completer.future;
        }
        return http.Response('{"detail":"Not Found"}', 404);
      });

      final apiClient = AegisApiClient(
        baseUrl: 'http://127.0.0.1:8000',
        client: mockClient,
      );

      final manager = AuthSessionManager(
        apiClient: apiClient,
        storage: storage,
      );

      final legacySession = AuthSession(
        accessToken: _createMockJwt(
          patientId: 'pat-conc-01',
          deviceId: 'dev-conc-01',
          scope: 'vitals:read vitals:write cycle:read',
        ),
        expiresIn: 1800,
        expiresAt: DateTime.now().toUtc().add(const Duration(minutes: 30)),
        refreshToken: 'rf-conc-01',
        refreshExpiresAt: DateTime.now().toUtc().add(const Duration(days: 30)),
        patientId: 'pat-conc-01',
        deviceId: 'dev-conc-01',
        scope: 'vitals:read vitals:write cycle:read',
      );
      manager.setSessionForTesting(legacySession, status: AuthSessionStatus.authenticated);

      // Launch 5 concurrent calls to ensureRequiredScopes
      final futures = List.generate(5, (_) => manager.ensureRequiredScopes());

      // Complete the pending server response
      completer.complete(
        http.Response(
          '{"access_token":"$upgradedJwt","token_type":"Bearer","expires_in":1800,'
          '"expires_at":"${DateTime.now().toUtc().add(const Duration(minutes: 30)).toIso8601String()}",'
          '"refresh_token":"rf-conc-new",'
          '"refresh_expires_at":"${DateTime.now().toUtc().add(const Duration(days: 30)).toIso8601String()}",'
          '"patient_id":"pat-conc-01","device_id":"dev-conc-01",'
          '"scope":"$standardScopeStr"}',
          200,
          headers: {'cache-control': 'no-store'},
        ),
      );

      final results = await Future.wait(futures);
      expect(results, equals([true, true, true, true, true]));
      expect(refreshCallCount, equals(1)); // exactly 1 network call!
    });

    test('7. getValidAccessToken(requiredScopes: ...) triggers planned upgrade and returns new token', () async {
      final storage = MemoryTestStorage();
      int refreshCallCount = 0;

      final standardScopeStr =
          'vitals:read vitals:write cycle:read cascade:intake cascade:run reports:read chat:read chat:write';
      final upgradedJwt = _createMockJwt(
        patientId: 'pat-acc-01',
        deviceId: 'dev-acc-01',
        scope: standardScopeStr,
      );

      final mockClient = MockClient((request) async {
        if (request.url.path == '/api/v1/auth/refresh') {
          refreshCallCount++;
          return http.Response(
            '{"access_token":"$upgradedJwt","token_type":"Bearer","expires_in":1800,'
            '"expires_at":"${DateTime.now().toUtc().add(const Duration(minutes: 30)).toIso8601String()}",'
            '"refresh_token":"rf-token-acc",'
            '"refresh_expires_at":"${DateTime.now().toUtc().add(const Duration(days: 30)).toIso8601String()}",'
            '"patient_id":"pat-acc-01","device_id":"dev-acc-01",'
            '"scope":"$standardScopeStr"}',
            200,
            headers: {'cache-control': 'no-store'},
          );
        }
        return http.Response('{"detail":"Not Found"}', 404);
      });

      final apiClient = AegisApiClient(
        baseUrl: 'http://127.0.0.1:8000',
        client: mockClient,
      );

      final manager = AuthSessionManager(
        apiClient: apiClient,
        storage: storage,
      );

      final legacySession = AuthSession(
        accessToken: _createMockJwt(
          patientId: 'pat-acc-01',
          deviceId: 'dev-acc-01',
          scope: 'vitals:read vitals:write cycle:read',
        ),
        expiresIn: 1800,
        expiresAt: DateTime.now().toUtc().add(const Duration(minutes: 30)),
        refreshToken: 'rf-acc-old',
        refreshExpiresAt: DateTime.now().toUtc().add(const Duration(days: 30)),
        patientId: 'pat-acc-01',
        deviceId: 'dev-acc-01',
        scope: 'vitals:read vitals:write cycle:read',
      );
      manager.setSessionForTesting(legacySession, status: AuthSessionStatus.authenticated);

      // Calling getValidAccessToken without requiredScopes returns existing valid token without refresh
      final defaultToken = await manager.getValidAccessToken();
      expect(defaultToken, equals(legacySession.accessToken));
      expect(refreshCallCount, equals(0));

      // Calling getValidAccessToken with cascade:intake requested triggers planned scope upgrade
      final upgradedToken = await manager.getValidAccessToken(
        requiredScopes: [AegisScopes.cascadeIntake],
      );
      expect(upgradedToken, equals(upgradedJwt));
      expect(refreshCallCount, equals(1));
    });

    test('8. 403 Forbidden is not confused with 401 and does not retry in loop', () async {
      int requestCount = 0;
      final mockClient = MockClient((request) async {
        requestCount++;
        return http.Response(
          '{"detail":"Insufficient token scope for this operation."}',
          403,
          headers: {'content-type': 'application/json'},
        );
      });

      final apiClient = AegisApiClient(
        baseUrl: 'http://127.0.0.1:8000',
        client: mockClient,
      );

      final manager = AuthSessionManager(
        apiClient: apiClient,
        storage: MemoryTestStorage(),
      );

      final session = AuthSession(
        accessToken: _createMockJwt(
          patientId: 'pat-403',
          deviceId: 'dev-403',
          scope: 'vitals:read',
        ),
        expiresIn: 1800,
        expiresAt: DateTime.now().toUtc().add(const Duration(minutes: 30)),
        refreshToken: 'rf-403',
        refreshExpiresAt: DateTime.now().toUtc().add(const Duration(days: 30)),
        patientId: 'pat-403',
        deviceId: 'dev-403',
        scope: 'vitals:read',
      );
      manager.setSessionForTesting(session, status: AuthSessionStatus.authenticated);

      // Request throws AegisInsufficientScopeException immediately without retrying
      await expectLater(
        apiClient.getCascadeStatus(),
        throwsA(isA<AegisInsufficientScopeException>()),
      );
      expect(requestCount, equals(1)); // exactly 1 request; no retry, no loop!
    });
  });
}
