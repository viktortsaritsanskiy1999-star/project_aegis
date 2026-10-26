import 'package:flutter_test/flutter_test.dart';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'package:aegis_health/services/api/vitals_api_models.dart';
import 'package:aegis_health/services/auth/auth_models.dart';
import 'package:aegis_health/services/auth/auth_token_storage.dart';

/// Fake test double for FlutterSecureStorage simulating OS Keystore behavior
class FakeFlutterSecureStorage extends FlutterSecureStorage {
  final Map<String, String> values = {};
  bool shouldThrow = false;
  bool shouldThrowOnDelete = false;
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
    if (shouldThrow) {
      throw Exception(failureMessage ?? 'Simulated Android Keystore write failure');
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
    if (shouldThrow) {
      throw Exception(failureMessage ?? 'Simulated Android Keystore read failure');
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
    if (shouldThrow || shouldThrowOnDelete) {
      throw Exception(failureMessage ?? 'Simulated Android Keystore delete failure');
    }
    values.remove(key);
  }
}

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  setUp(() {
    SharedPreferences.setMockInitialValues({});
  });

  group('Stage 5.3C-1: Device Pairing Contract (POST /api/v1/auth/pair)', () {
    test('1. DevicePairRequest serialization conforms strictly to FastAPI schema', () {
      final req = DevicePairRequest(
        pairingCode: '042918', // preserves leading zero!
        deviceName: 'Pixel 8 Pro',
        deviceId: 'dev_550e8400_e29b',
        platform: 'android',
      );

      final jsonMap = req.toJson();
      expect(jsonMap['pairing_code'], equals('042918'));
      expect(jsonMap['device_name'], equals('Pixel 8 Pro'));
      expect(jsonMap['device_id'], equals('dev_550e8400_e29b'));
      expect(jsonMap['platform'], equals('android'));
      // Ensure extra fields forbidden by backend extra="forbid" are NOT added
      expect(jsonMap.containsKey('patient_id'), isFalse);
      expect(jsonMap.containsKey('scope'), isFalse);
    });

    test('2. DevicePairRequest omits device_id when null', () {
      final req = DevicePairRequest(
        pairingCode: '123456',
        deviceName: 'iPhone 15',
        platform: 'ios',
      );

      final jsonMap = req.toJson();
      expect(jsonMap['pairing_code'], equals('123456'));
      expect(jsonMap['device_name'], equals('iPhone 15'));
      expect(jsonMap['platform'], equals('ios'));
      expect(jsonMap.containsKey('device_id'), isFalse);
    });

    test('3. Pairing code validation accepts 6-digit numeric codes with leading zeros', () {
      expect(() => DevicePairRequest(pairingCode: '000000', deviceName: 'Phone'), returnsNormally);
      expect(() => DevicePairRequest(pairingCode: '042918', deviceName: 'Phone'), returnsNormally);
      expect(() => DevicePairRequest(pairingCode: '999999', deviceName: 'Phone'), returnsNormally);
    });

    test('4. Pairing code validation rejects non-6-digit, non-numeric, or whitespace-padded strings', () {
      expect(
        () => DevicePairRequest(pairingCode: '12345', deviceName: 'Phone'),
        throwsA(isA<AegisValidationException>()),
      );
      expect(
        () => DevicePairRequest(pairingCode: '1234567', deviceName: 'Phone'),
        throwsA(isA<AegisValidationException>()),
      );
      expect(
        () => DevicePairRequest(pairingCode: '04291a', deviceName: 'Phone'),
        throwsA(isA<AegisValidationException>()),
      );
      expect(
        () => DevicePairRequest(pairingCode: '      ', deviceName: 'Phone'),
        throwsA(isA<AegisValidationException>()),
      );
      // Strict: Reject whitespace padding around valid 6-digit codes
      expect(
        () => DevicePairRequest(pairingCode: ' 042918', deviceName: 'Phone'),
        throwsA(isA<AegisValidationException>()),
      );
      expect(
        () => DevicePairRequest(pairingCode: '042918 ', deviceName: 'Phone'),
        throwsA(isA<AegisValidationException>()),
      );
      expect(
        () => DevicePairRequest(pairingCode: ' 042918 ', deviceName: 'Phone'),
        throwsA(isA<AegisValidationException>()),
      );
      expect(
        () => DevicePairRequest(pairingCode: '042918\n', deviceName: 'Phone'),
        throwsA(isA<AegisValidationException>()),
      );
      expect(
        () => DevicePairRequest(pairingCode: '\t042918', deviceName: 'Phone'),
        throwsA(isA<AegisValidationException>()),
      );
    });

    test('5. DevicePairRequest validates device_name and device_id admissibility', () {
      expect(
        () => DevicePairRequest(pairingCode: '123456', deviceName: '   '),
        throwsA(isA<AegisValidationException>()),
      );
      expect(
        () => DevicePairRequest(pairingCode: '123456', deviceName: 'Valid', deviceId: '   '),
        throwsA(isA<AegisValidationException>()),
      );
      expect(
        () => DevicePairRequest(
          pairingCode: '123456',
          deviceName: 'Valid',
          deviceId: 'a' * 65, // exceeds 64 chars
        ),
        throwsA(isA<AegisValidationException>()),
      );
    });

    test('6. DevicePairResponse deserializes valid backend 200 payload into UTC DateTime', () {
      final backendJson = {
        'access_token': 'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.dummy',
        'token_type': 'Bearer',
        'expires_in': 1800,
        'expires_at': '2026-10-26T14:30:00Z',
        'refresh_token': 'art_test_refresh_token_abcdef1234567890',
        'refresh_expires_at': '2026-11-25T14:30:00Z',
        'patient_id': 'pat_11111111-2222-3333-4444-555555555555',
        'device_id': 'dev_test_device_01',
        'scope': 'vitals:read vitals:write cycle:read',
      };

      final response = DevicePairResponse.fromJson(backendJson);
      expect(response.accessToken, equals('eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.dummy'));
      expect(response.tokenType, equals('Bearer'));
      expect(response.expiresIn, equals(1800));
      expect(response.expiresAt.isUtc, isTrue);
      expect(response.expiresAt, equals(DateTime.utc(2026, 10, 26, 14, 30, 0)));
      expect(response.refreshToken, equals('art_test_refresh_token_abcdef1234567890'));
      expect(response.refreshExpiresAt.isUtc, isTrue);
      expect(response.refreshExpiresAt, equals(DateTime.utc(2026, 11, 25, 14, 30, 0)));
      expect(response.patientId, equals('pat_11111111-2222-3333-4444-555555555555'));
      expect(response.deviceId, equals('dev_test_device_01'));
      expect(response.scope, equals('vitals:read vitals:write cycle:read'));
    });

    test('7. DevicePairResponse throws on missing required fields', () {
      final incomplete = {
        'access_token': 'token',
        'patient_id': 'pat_1',
        // missing refresh_token, device_id, etc.
      };
      expect(
        () => DevicePairResponse.fromJson(incomplete),
        throwsA(isA<AegisValidationException>()),
      );
    });

    test('8. DevicePairResponse throws on invalid datetime formats', () {
      final corrupted = {
        'access_token': 'token',
        'token_type': 'Bearer',
        'expires_in': 1800,
        'expires_at': 'not-a-datetime',
        'refresh_token': 'art_token',
        'refresh_expires_at': 'not-a-datetime',
        'patient_id': 'pat_1',
        'device_id': 'dev_1',
        'scope': 'vitals:read',
      };
      expect(
        () => DevicePairResponse.fromJson(corrupted),
        throwsA(isA<AegisValidationException>()),
      );
    });
  });

  group('Stage 5.3C-1: Token Refresh Contract (POST /api/v1/auth/refresh)', () {
    test('9. TokenRefreshRequest serialization conforms strictly to FastAPI schema', () {
      final req = TokenRefreshRequest(
        deviceId: 'dev_test_01',
        refreshToken: 'art_refresh_0123456789',
      );

      final jsonMap = req.toJson();
      expect(jsonMap['device_id'], equals('dev_test_01'));
      expect(jsonMap['refresh_token'], equals('art_refresh_0123456789'));
      expect(jsonMap.length, equals(2));
    });

    test('10. TokenRefreshRequest rejects empty credentials', () {
      expect(
        () => TokenRefreshRequest(deviceId: '', refreshToken: 'art_token'),
        throwsA(isA<AegisValidationException>()),
      );
      expect(
        () => TokenRefreshRequest(deviceId: 'dev_1', refreshToken: '   '),
        throwsA(isA<AegisValidationException>()),
      );
    });

    test('11. TokenRefreshResponse deserializes rotated pair correctly', () {
      final backendJson = {
        'access_token': 'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.rotated',
        'token_type': 'Bearer',
        'expires_in': 1800,
        'expires_at': '2026-10-26T15:00:00Z',
        'refresh_token': 'art_new_rotated_refresh_token',
        'refresh_expires_at': '2026-11-25T15:00:00Z',
        'patient_id': 'pat_test_01',
        'device_id': 'dev_test_01',
        'scope': 'vitals:read vitals:write cycle:read',
      };

      final response = TokenRefreshResponse.fromJson(backendJson);
      expect(response.accessToken, equals('eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.rotated'));
      expect(response.refreshToken, equals('art_new_rotated_refresh_token'));
      expect(response.expiresAt, equals(DateTime.utc(2026, 10, 26, 15, 0, 0)));
      expect(response.refreshExpiresAt, equals(DateTime.utc(2026, 11, 25, 15, 0, 0)));
      expect(response.patientId, equals('pat_test_01'));
      expect(response.deviceId, equals('dev_test_01'));
      expect(response.scope, equals('vitals:read vitals:write cycle:read'));
    });
  });

  group('Stage 5.3C-1: Session Revocation Contract (POST /api/v1/auth/revoke)', () {
    test('12. DeviceRevokeRequest serializes optional parameters', () {
      const full = DeviceRevokeRequest(
        deviceId: 'dev_1',
        refreshToken: 'art_1',
      );
      expect(full.toJson(), equals({'device_id': 'dev_1', 'refresh_token': 'art_1'}));

      const empty = DeviceRevokeRequest();
      expect(empty.toJson(), equals({}));
    });

    test('13. DeviceRevokeResponse deserializes 200 OK status', () {
      final jsonMap = {
        'status': 'revoked',
        'device_id': 'dev_test_01',
        'revoked': true,
      };

      final res = DeviceRevokeResponse.fromJson(jsonMap);
      expect(res.status, equals('revoked'));
      expect(res.deviceId, equals('dev_test_01'));
      expect(res.revoked, isTrue);
    });

    test('14. DeviceRevokeResponse throws when device_id is missing', () {
      final jsonMap = {'status': 'revoked', 'revoked': true};
      expect(() => DeviceRevokeResponse.fromJson(jsonMap), throwsA(isA<AegisValidationException>()));
    });
  });

  group('Stage 5.3C-1: AuthSession Model & Expiration Timing', () {
    test('15. AuthSession creates from Pair and Refresh responses', () {
      final pairRes = DevicePairResponse(
        accessToken: 'access_1',
        expiresIn: 1800,
        expiresAt: DateTime.utc(2026, 10, 26, 14, 30),
        refreshToken: 'refresh_1',
        refreshExpiresAt: DateTime.utc(2026, 11, 25, 14, 30),
        patientId: 'pat_1',
        deviceId: 'dev_1',
        scope: 'vitals:read vitals:write cycle:read',
      );
      final sessionFromPair = AuthSession.fromPairResponse(pairRes);
      expect(sessionFromPair.accessToken, equals('access_1'));
      expect(sessionFromPair.patientId, equals('pat_1'));

      final refreshRes = TokenRefreshResponse(
        accessToken: 'access_2',
        expiresIn: 1800,
        expiresAt: DateTime.utc(2026, 10, 26, 15, 00),
        refreshToken: 'refresh_2',
        refreshExpiresAt: DateTime.utc(2026, 11, 25, 15, 00),
        patientId: 'pat_1',
        deviceId: 'dev_1',
        scope: 'vitals:read vitals:write cycle:read',
      );
      final sessionFromRefresh = AuthSession.fromRefreshResponse(refreshRes);
      expect(sessionFromRefresh.accessToken, equals('access_2'));
      expect(sessionFromRefresh.refreshToken, equals('refresh_2'));
    });

    test('16. Scopes parsing and permission checks', () {
      final session = AuthSession(
        accessToken: 'token',
        expiresIn: 1800,
        expiresAt: DateTime.now().toUtc().add(const Duration(minutes: 30)),
        refreshToken: 'refresh',
        refreshExpiresAt: DateTime.now().toUtc().add(const Duration(days: 30)),
        patientId: 'pat_1',
        deviceId: 'dev_1',
        scope: 'vitals:read vitals:write cycle:read',
      );

      expect(session.scopes, equals({'vitals:read', 'vitals:write', 'cycle:read'}));
      expect(session.hasScope('vitals:read'), isTrue);
      expect(session.hasScope('cycle:read'), isTrue);
      expect(session.hasScope('admin:all'), isFalse);
      expect(session.hasAllScopes(['vitals:read', 'vitals:write']), isTrue);
      expect(session.hasAllScopes(['vitals:read', 'admin:all']), isFalse);
    });

    test('17. JWT Access Token expiration respects 60-second skew buffer', () {
      final nowUtc = DateTime.now().toUtc();

      // Expiring in 45 seconds (inside the 60s skew window): considered expired for safety!
      final aboutToExpire = AuthSession(
        accessToken: 'token_expiring_soon',
        expiresIn: 45,
        expiresAt: nowUtc.add(const Duration(seconds: 45)),
        refreshToken: 'art_valid',
        refreshExpiresAt: nowUtc.add(const Duration(days: 30)),
        patientId: 'pat_1',
        deviceId: 'dev_1',
        scope: 'vitals:read',
      );
      expect(aboutToExpire.isAccessTokenExpired(skewBuffer: const Duration(seconds: 60)), isTrue);

      // Expiring in 90 seconds (outside 60s skew window): considered still valid
      final stillValid = AuthSession(
        accessToken: 'token_valid',
        expiresIn: 90,
        expiresAt: nowUtc.add(const Duration(seconds: 90)),
        refreshToken: 'art_valid',
        refreshExpiresAt: nowUtc.add(const Duration(days: 30)),
        patientId: 'pat_1',
        deviceId: 'dev_1',
        scope: 'vitals:read',
      );
      expect(stillValid.isAccessTokenExpired(skewBuffer: const Duration(seconds: 60)), isFalse);

      // Already expired in past
      final alreadyExpired = AuthSession(
        accessToken: 'token_expired',
        expiresIn: 0,
        expiresAt: nowUtc.subtract(const Duration(seconds: 10)),
        refreshToken: 'art_valid',
        refreshExpiresAt: nowUtc.add(const Duration(days: 30)),
        patientId: 'pat_1',
        deviceId: 'dev_1',
        scope: 'vitals:read',
      );
      expect(alreadyExpired.isAccessTokenExpired(), isTrue);
    });

    test('18. Refresh token expiration check', () {
      final nowUtc = DateTime.now().toUtc();

      final validRefresh = AuthSession(
        accessToken: 'token',
        expiresIn: 1800,
        expiresAt: nowUtc.add(const Duration(minutes: 30)),
        refreshToken: 'refresh_active',
        refreshExpiresAt: nowUtc.add(const Duration(days: 5)),
        patientId: 'pat_1',
        deviceId: 'dev_1',
        scope: 'vitals:read',
      );
      expect(validRefresh.isRefreshTokenExpired(), isFalse);

      final deadRefresh = AuthSession(
        accessToken: 'token',
        expiresIn: 1800,
        expiresAt: nowUtc.add(const Duration(minutes: 30)),
        refreshToken: 'refresh_dead',
        refreshExpiresAt: nowUtc.subtract(const Duration(minutes: 1)),
        patientId: 'pat_1',
        deviceId: 'dev_1',
        scope: 'vitals:read',
      );
      expect(deadRefresh.isRefreshTokenExpired(), isTrue);
    });

    test('19. AuthSession JSON serialization round-trip', () {
      final original = AuthSession(
        accessToken: 'jwt_access_123',
        tokenType: 'Bearer',
        expiresIn: 1800,
        expiresAt: DateTime.utc(2026, 10, 26, 14, 30),
        refreshToken: 'art_refresh_456',
        refreshExpiresAt: DateTime.utc(2026, 11, 25, 14, 30),
        patientId: 'pat_abc',
        deviceId: 'dev_xyz',
        scope: 'vitals:read vitals:write',
      );

      final map = original.toJson();
      final restored = AuthSession.fromJson(map);

      expect(restored.accessToken, equals(original.accessToken));
      expect(restored.refreshToken, equals(original.refreshToken));
      expect(restored.patientId, equals(original.patientId));
      expect(restored.deviceId, equals(original.deviceId));
      expect(restored.scope, equals(original.scope));
      expect(restored.expiresAt, equals(original.expiresAt));
      expect(restored.refreshExpiresAt, equals(original.refreshExpiresAt));
    });

    test('20. AuthSession.fromJson throws on corrupted or incomplete stored JSON', () {
      final corrupted = {
        'access_token': 'jwt',
        // missing patient_id, refresh_token, dates
      };
      expect(() => AuthSession.fromJson(corrupted), throwsA(isA<AegisValidationException>()));
    });
  });

  group('Stage 5.3C-1: Credential Redaction in Diagnostics & toString()', () {
    test('21. DevicePairRequest redacts pairing code', () {
      final req = DevicePairRequest(pairingCode: '042918', deviceName: 'Test Phone');
      final str = req.toString();
      expect(str.contains('042918'), isFalse);
      expect(str.contains('[REDACTED]'), isTrue);
    });

    test('22. DevicePairResponse redacts tokens', () {
      final res = DevicePairResponse(
        accessToken: 'super_secret_jwt_access_token',
        expiresIn: 1800,
        expiresAt: DateTime.utc(2026, 10, 26),
        refreshToken: 'super_secret_refresh_token_string',
        refreshExpiresAt: DateTime.utc(2026, 11, 25),
        patientId: 'pat_1',
        deviceId: 'dev_1',
        scope: 'vitals:read',
      );
      final str = res.toString();
      expect(str.contains('super_secret_jwt_access_token'), isFalse);
      expect(str.contains('super_secret_refresh_token_string'), isFalse);
      expect(str.contains('[REDACTED]'), isTrue);
    });

    test('23. TokenRefreshRequest redacts refresh token', () {
      final req = TokenRefreshRequest(deviceId: 'dev_1', refreshToken: 'sensitive_refresh_token');
      final str = req.toString();
      expect(str.contains('sensitive_refresh_token'), isFalse);
      expect(str.contains('[REDACTED]'), isTrue);
    });

    test('24. TokenRefreshResponse redacts tokens', () {
      final res = TokenRefreshResponse(
        accessToken: 'secret_new_access_token',
        expiresIn: 1800,
        expiresAt: DateTime.utc(2026, 10, 26),
        refreshToken: 'secret_new_refresh_token',
        refreshExpiresAt: DateTime.utc(2026, 11, 25),
        patientId: 'pat_1',
        deviceId: 'dev_1',
        scope: 'vitals:read',
      );
      final str = res.toString();
      expect(str.contains('secret_new_access_token'), isFalse);
      expect(str.contains('secret_new_refresh_token'), isFalse);
      expect(str.contains('[REDACTED]'), isTrue);
    });

    test('25. DeviceRevokeRequest redacts refresh token if present', () {
      const req = DeviceRevokeRequest(deviceId: 'dev_1', refreshToken: 'secret_revoke_token');
      final str = req.toString();
      expect(str.contains('secret_revoke_token'), isFalse);
      expect(str.contains('[REDACTED]'), isTrue);
    });

    test('26. AuthSession redacts both access and refresh tokens', () {
      final session = AuthSession(
        accessToken: 'sensitive_jwt_token',
        expiresIn: 1800,
        expiresAt: DateTime.utc(2026, 10, 26),
        refreshToken: 'sensitive_refresh_token',
        refreshExpiresAt: DateTime.utc(2026, 11, 25),
        patientId: 'pat_1',
        deviceId: 'dev_1',
        scope: 'vitals:read',
      );
      final str = session.toString();
      expect(str.contains('sensitive_jwt_token'), isFalse);
      expect(str.contains('sensitive_refresh_token'), isFalse);
      expect(str.contains('[REDACTED]'), isTrue);
    });
  });

  group('Stage 5.3C-1: Typed Exceptions Hierarchy', () {
    test('27. AegisRateLimitException preserves retryAfter and status 429', () {
      const ex = AegisRateLimitException(
        'Too many attempts',
        retryAfter: Duration(seconds: 45),
      );
      expect(ex.statusCode, equals(429));
      expect(ex.retryAfter, equals(const Duration(seconds: 45)));
      expect(ex.toString(), contains('45s'));
      expect(ex.toString(), contains('429'));
    });

    test('28. SecureStorageUnavailableException records cause but redacts secrets from toString() and message', () {
      // Simulate raw platform exception containing sensitive tokens or credentials in its message
      final inner = Exception(
        'PlatformException(WRITE_ERROR, failed to persist {"access_token": "art_secret_jwt_999", "refresh_token": "art_secret_refresh_888", "pairing_code": "042918"}, null, null)',
      );
      final ex = SecureStorageUnavailableException('Failed to securely persist auth session.', inner);

      // Verify message is clean and safe
      expect(ex.message, equals('Failed to securely persist auth session.'));
      expect(ex.message.contains('art_secret_jwt_999'), isFalse);
      expect(ex.message.contains('art_secret_refresh_888'), isFalse);
      expect(ex.message.contains('042918'), isFalse);

      // Verify toString() does NOT expose the inner cause's raw text
      final str = ex.toString();
      expect(str, equals('SecureStorageUnavailableException: Failed to securely persist auth session.'));
      expect(str.contains('art_secret_jwt_999'), isFalse);
      expect(str.contains('art_secret_refresh_888'), isFalse);
      expect(str.contains('042918'), isFalse);
      expect(str.contains('PlatformException'), isFalse);

      // Verify typed cause is still preserved programmatically
      expect(ex.cause, isNotNull);
      expect(ex.cause, equals(inner));
    });
  });

  group('Stage 5.3C-1: Session Storage Implementations & Security Isolation', () {
    final sampleSession = AuthSession(
      accessToken: 'sample_jwt',
      expiresIn: 1800,
      expiresAt: DateTime.utc(2026, 10, 26, 16, 0),
      refreshToken: 'sample_refresh',
      refreshExpiresAt: DateTime.utc(2026, 11, 25, 16, 0),
      patientId: 'pat_sample',
      deviceId: 'dev_sample',
      scope: 'vitals:read vitals:write',
    );

    test('29. InMemoryAuthTokenStorage save, load, and clear lifecycle', () async {
      final storage = InMemoryAuthTokenStorage();
      expect(await storage.loadSession(), isNull);

      await storage.saveSession(sampleSession);
      final loaded = await storage.loadSession();
      expect(loaded, isNotNull);
      expect(loaded!.accessToken, equals('sample_jwt'));
      expect(loaded.patientId, equals('pat_sample'));

      await storage.clearSession();
      expect(await storage.loadSession(), isNull);
    });

    test('30. InMemoryAuthTokenStorage throws when shouldThrow is enabled', () async {
      final storage = InMemoryAuthTokenStorage()..shouldThrow = true;
      expect(() => storage.loadSession(), throwsA(isA<SecureStorageUnavailableException>()));
      expect(() => storage.saveSession(sampleSession), throwsA(isA<SecureStorageUnavailableException>()));
      expect(() => storage.clearSession(), throwsA(isA<SecureStorageUnavailableException>()));
    });

    test('31. SecureAuthTokenStorage persists to secure key without writing to SharedPreferences', () async {
      final fakeSecure = FakeFlutterSecureStorage();
      final secureStorage = SecureAuthTokenStorage(storage: fakeSecure);

      await secureStorage.saveSession(sampleSession);

      // Verify stored in secure storage
      expect(fakeSecure.values.containsKey(SecureAuthTokenStorage.sessionStorageKey), isTrue);
      final savedStr = fakeSecure.values[SecureAuthTokenStorage.sessionStorageKey]!;
      expect(savedStr.contains('sample_jwt'), isTrue);

      // Verify NEVER written to SharedPreferences (zero leakage!)
      final prefs = await SharedPreferences.getInstance();
      expect(prefs.getString(SecureAuthTokenStorage.sessionStorageKey), isNull);
      expect(prefs.getKeys().where((k) => k.contains('token') || k.contains('auth')), isEmpty);

      // Verify read-back
      final loaded = await secureStorage.loadSession();
      expect(loaded, isNotNull);
      expect(loaded!.accessToken, equals('sample_jwt'));
      expect(loaded.patientId, equals('pat_sample'));

      // Verify clear
      await secureStorage.clearSession();
      expect(await secureStorage.loadSession(), isNull);
      expect(fakeSecure.values.containsKey(SecureAuthTokenStorage.sessionStorageKey), isFalse);
    });

    test('32. SecureAuthTokenStorage handles corrupted JSON payload: clears record and returns null on success', () async {
      final fakeSecure = FakeFlutterSecureStorage();
      final secureStorage = SecureAuthTokenStorage(storage: fakeSecure);

      // Write corrupted JSON into storage
      fakeSecure.values[SecureAuthTokenStorage.sessionStorageKey] = '{"corrupted": true, "missing_fields": true}';

      final loaded = await secureStorage.loadSession();
      expect(loaded, isNull);
      // Ensure corrupted entry was scrubbed
      expect(fakeSecure.values.containsKey(SecureAuthTokenStorage.sessionStorageKey), isFalse);
    });

    test('33. SecureAuthTokenStorage handles corrupted JSON payload: throws SecureStorageUnavailableException if clear fails', () async {
      final fakeSecure = FakeFlutterSecureStorage();
      final secureStorage = SecureAuthTokenStorage(storage: fakeSecure);

      // Write corrupted JSON into storage
      fakeSecure.values[SecureAuthTokenStorage.sessionStorageKey] = '{"syntax_error_invalid_json:';

      // Simulate failure during delete operation (e.g. disk/keystore failure during cleanup)
      fakeSecure.shouldThrowOnDelete = true;
      fakeSecure.failureMessage = 'Keystore failure during delete';

      // Must throw SecureStorageUnavailableException instead of swallowing error!
      expect(
        () => secureStorage.loadSession(),
        throwsA(isA<SecureStorageUnavailableException>()),
      );

      // SharedPreferences remains untouched
      final prefs = await SharedPreferences.getInstance();
      expect(prefs.getKeys(), isEmpty);
    });

    test('34. SecureAuthTokenStorage fails-closed: throws SecureStorageUnavailableException when Keystore fails', () async {
      final fakeSecure = FakeFlutterSecureStorage()..shouldThrow = true;
      final secureStorage = SecureAuthTokenStorage(storage: fakeSecure);

      // Must throw SecureStorageUnavailableException — NEVER silently fallback to SharedPreferences or InMemory!
      expect(
        () => secureStorage.saveSession(sampleSession),
        throwsA(isA<SecureStorageUnavailableException>()),
      );
      expect(
        () => secureStorage.loadSession(),
        throwsA(isA<SecureStorageUnavailableException>()),
      );
      expect(
        () => secureStorage.clearSession(),
        throwsA(isA<SecureStorageUnavailableException>()),
      );

      // Confirm SharedPreferences remained completely untouched
      final prefs = await SharedPreferences.getInstance();
      expect(prefs.getKeys(), isEmpty);
    });
  });
}
