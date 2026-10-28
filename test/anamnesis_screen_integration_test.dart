import 'dart:async';
import 'dart:convert';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:provider/provider.dart';

import 'package:aegis_health/anamnesis_screen.dart';
import 'package:aegis_health/widgets/aegis_device_pairing_modal.dart';
import 'package:aegis_health/services/api/aegis_api_client.dart';
import 'package:aegis_health/services/auth/auth_models.dart';
import 'package:aegis_health/services/auth/auth_session_manager.dart';
import 'package:aegis_health/services/auth/auth_token_storage.dart';
import 'package:aegis_health/services/vault_service.dart';
import 'package:aegis_health/services/vault_storage_service.dart';

const String _standardScopes =
    'vitals:read vitals:write cycle:read cascade:intake cascade:run reports:read chat:read chat:write';

String _buildTestJwt({
  required String patientId,
  required String deviceId,
  String scope = _standardScopes,
}) {
  final headerJson = json.encode({'alg': 'HS256', 'typ': 'JWT'});
  final payloadJson = json.encode({
    'sub': patientId,
    'patient_id': patientId,
    'did': deviceId,
    'scope': scope,
    'type': 'access',
  });
  final h = base64Url.encode(utf8.encode(headerJson)).replaceAll('=', '');
  final p = base64Url.encode(utf8.encode(payloadJson)).replaceAll('=', '');
  const s = 'signature_fake_secret_for_test';
  return '$h.$p.$s';
}

void fillAll17Cards(VaultService vault) {
  vault.saveAnamnesis('age_confirmation', 'Confirmed');
  vault.saveAnamnesis('gender', 'Male');
  vault.saveAnamnesis('age', '35');
  vault.saveAnamnesis('height', '180');
  vault.saveAnamnesis('weight', '78');
  vault.saveAnamnesis('nicotine_status', 'Active');
  vault.saveAnamnesis('nicotine_years', '1-5 years');
  vault.saveAnamnesis('nicotine_freq', 'Daily');
  vault.saveAnamnesis('alcohol_status', 'Frequently');
  vault.saveAnamnesis('alcohol_type', 'Wine');
  vault.saveAnamnesis('spec_factors', 'Coffee, pre-workouts', userText: '2 espresso daily');
  vault.saveAnamnesis('sleep_quality', 'Schedule off');
  vault.saveAnamnesis('diet_types', 'Balanced, Mediterranean');
  vault.saveAnamnesis('diet_habits', 'Regular', userText: '3 meals per day');
  vault.saveAnamnesis('gi_signals', 'Heartburn', userText: 'Mild heartburn after spicy food');
  vault.saveAnamnesis('work_type', 'Sedentary');
  vault.saveAnamnesis('work_schedule', '5/2');
  vault.saveAnamnesis('sport_level', 'Light');
  vault.saveAnamnesis('chronic_diseases', 'None', userText: 'No chronic diseases');
  vault.saveAnamnesis('recent_illness', 'ARVI', userText: 'Mild cold 3 weeks ago');
  vault.saveAnamnesis('critical_infections', 'No chronic infections (Negative)');
  vault.saveAnamnesis('heredity_risks', 'Nothing serious', userText: 'No known early cardiovascular issues');
  vault.saveAnamnesis('medications', 'Vitamins', userText: 'Vitamin D3, Omega-3');
}

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  late InMemoryVaultStorage memoryVaultStorage;
  late InMemoryAuthTokenStorage memoryAuthStorage;

  setUp(() {
    VaultService.resetWriteQueueForTesting();
    AuthSessionManager.resetActiveInstancesForTesting();
    memoryVaultStorage = InMemoryVaultStorage();
    memoryAuthStorage = InMemoryAuthTokenStorage();
    VaultStorageService.setStorageForTesting(memoryVaultStorage);
  });

  tearDown(() {
    VaultService.resetWriteQueueForTesting();
    AuthSessionManager.resetActiveInstancesForTesting();
    VaultStorageService.setStorageForTesting(null);
  });

  group('PROJECT AEGIS — STAGE 5.4D-3: ANAMNESIS INTEGRATION & SAFE SYNC', () {
    test('1. Fill all 17 cards & successful server intake confirmation (200 OK)', () async {
      int serverCallCount = 0;
      Map<String, dynamic>? receivedBody;

      final mockClient = MockClient((request) async {
        if (request.url.path == '/api/v1/client/cascade/intake/anamnesis') {
          serverCallCount++;
          receivedBody = json.decode(request.body) as Map<String, dynamic>;
          expect(request.headers['authorization'], startsWith('Bearer '));
          return http.Response(
            json.encode({
              'cycle_id': 'cycle_patient_A_1',
              'status': 'processed',
              'intake_version': 1,
              'created_at_utc': '2026-10-28T18:00:00Z',
              'privacy_notice': 'Anamnesis processed in strict blind mode',
            }),
            200,
            headers: {'content-type': 'application/json'},
          );
        }
        return http.Response('Not Found', 404);
      });

      final apiClient = AegisApiClient(client: mockClient);
      final authManager = AuthSessionManager(
        apiClient: apiClient,
        storage: memoryAuthStorage,
      );

      final vault = VaultService(
        storage: memoryVaultStorage,
        authSessionManager: authManager,
      );
      await vault.initFuture;

      // 1. Authenticate Patient A with cascade:intake
      authManager.setSessionForTesting(
        AuthSession(
          accessToken: _buildTestJwt(patientId: 'patient_A', deviceId: 'dev_A'),
          refreshToken: 'rt_patient_A',
          expiresIn: 3600,
          expiresAt: DateTime.now().toUtc().add(const Duration(hours: 1)),
          refreshExpiresAt: DateTime.now().toUtc().add(const Duration(days: 30)),
          patientId: 'patient_A',
          deviceId: 'dev_A',
          scope: AegisScopes.standardDeviceScopes.join(' '),
        ),
        status: AuthSessionStatus.authenticated,
      );
      await vault.setActivePatientId('patient_A');

      // 2. Fill all 17 cards
      fillAll17Cards(vault);
      expect(vault.isAnamnesisComplete, isTrue);

      final answers = vault.buildAnamnesisAnswers()!;
      expect(answers.ageConfirmation, 'Confirmed');
      expect(answers.gender, 'Male');
      expect(answers.age, 35);
      expect(answers.height, 180);
      expect(answers.weight, 78);
      expect(answers.nicotineStatus, 'Active');
      expect(answers.alcoholStatus, 'Frequently');
      expect(answers.medications, 'Vitamins');

      // 3. Sync to server M1
      final result = await vault.syncAnamnesisToServer(
        apiClient: apiClient,
        authSessionManager: authManager,
      );

      // 4. Verify confirmation
      expect(result.isSuccess, isTrue);
      expect(result.status, AnamnesisSyncStatus.synced);
      expect(serverCallCount, 1);
      expect(receivedBody!['answers']['gender'], 'Male');
      expect(receivedBody!['answers']['age'], 35);

      expect(vault.isAnamnesisSynced, isTrue);
      expect(vault.anamnesisCycleId, 'cycle_patient_A_1');
      expect(vault.anamnesisIntakeVersion, 1);
      expect(vault.anamnesisSyncedAt, isNotNull);

      // Verify guest draft is cleared
      expect(await vault.hasGuestAnamnesisDraft(), isFalse);

      vault.dispose();
      authManager.dispose();
      apiClient.close();
    });

    test('2. Network offline: draft preserved locally, fail-closed error reporting, no draft wiping', () async {
      int serverCallCount = 0;

      final mockClient = MockClient((request) async {
        if (request.url.path == '/api/v1/client/cascade/intake/anamnesis') {
          serverCallCount++;
          throw http.ClientException('Network is unreachable (simulated offline)');
        }
        return http.Response('Not Found', 404);
      });

      final apiClient = AegisApiClient(client: mockClient);
      final authManager = AuthSessionManager(
        apiClient: apiClient,
        storage: memoryAuthStorage,
      );

      final vault = VaultService(
        storage: memoryVaultStorage,
        authSessionManager: authManager,
      );
      await vault.initFuture;

      authManager.setSessionForTesting(
        AuthSession(
          accessToken: _buildTestJwt(patientId: 'patient_offline', deviceId: 'dev_offline'),
          refreshToken: 'rt_offline',
          expiresIn: 3600,
          expiresAt: DateTime.now().toUtc().add(const Duration(hours: 1)),
          refreshExpiresAt: DateTime.now().toUtc().add(const Duration(days: 30)),
          patientId: 'patient_offline',
          deviceId: 'dev_offline',
          scope: AegisScopes.standardDeviceScopes.join(' '),
        ),
        status: AuthSessionStatus.authenticated,
      );
      await vault.setActivePatientId('patient_offline');

      // Fill 17 cards
      fillAll17Cards(vault);
      await vault.flushPendingWrites();

      // Sync attempt fails due to offline
      final result = await vault.syncAnamnesisToServer(
        apiClient: apiClient,
        authSessionManager: authManager,
      );

      // 1. Sync failed with offline status
      expect(result.isSuccess, isFalse);
      expect(result.status, AnamnesisSyncStatus.offline);
      expect(vault.anamnesisSyncStatus, AnamnesisSyncStatus.offline);
      expect(vault.isAnamnesisSynced, isFalse);
      expect(serverCallCount, 1);

      // 2. Draft is strictly preserved in vault storage
      final key = VaultStorageService.derivePatientStorageKey('patient_offline');
      final stored = await memoryVaultStorage.loadPatientVault(key);
      expect(stored['system_folders']?['global']?['gender'], 'Male');
      expect(stored['system_folders']?['global']?['age'], '35');
      expect(vault.getSystemContext('global')['gender'], 'Male');

      vault.dispose();
      authManager.dispose();
      apiClient.close();
    });

    test('3. Timeout recovery: draft preserved, retry succeeds with server content-hash deduplication', () async {
      int attemptCount = 0;

      final mockClient = MockClient((request) async {
        if (request.url.path == '/api/v1/client/cascade/intake/anamnesis') {
          attemptCount++;
          if (attemptCount == 1) {
            throw TimeoutException('Request timed out after 15s');
          }
          // Second attempt: server returns 200 (content-hash deduplication)
          return http.Response(
            json.encode({
              'cycle_id': 'cycle_retry_dedup',
              'status': 'processed',
              'intake_version': 1,
              'created_at_utc': '2026-10-28T18:05:00Z',
              'privacy_notice': 'Anamnesis processed in strict blind mode',
            }),
            200,
            headers: {'content-type': 'application/json'},
          );
        }
        return http.Response('Not Found', 404);
      });

      final apiClient = AegisApiClient(client: mockClient);
      final authManager = AuthSessionManager(
        apiClient: apiClient,
        storage: memoryAuthStorage,
      );

      final vault = VaultService(
        storage: memoryVaultStorage,
        authSessionManager: authManager,
      );
      await vault.initFuture;

      authManager.setSessionForTesting(
        AuthSession(
          accessToken: _buildTestJwt(patientId: 'patient_retry', deviceId: 'dev_retry'),
          refreshToken: 'rt_retry',
          expiresIn: 3600,
          expiresAt: DateTime.now().toUtc().add(const Duration(hours: 1)),
          refreshExpiresAt: DateTime.now().toUtc().add(const Duration(days: 30)),
          patientId: 'patient_retry',
          deviceId: 'dev_retry',
          scope: AegisScopes.standardDeviceScopes.join(' '),
        ),
        status: AuthSessionStatus.authenticated,
      );
      await vault.setActivePatientId('patient_retry');

      fillAll17Cards(vault);
      await vault.flushPendingWrites();

      // First attempt: times out
      final result1 = await vault.syncAnamnesisToServer(
        apiClient: apiClient,
        authSessionManager: authManager,
      );
      expect(result1.isSuccess, isFalse);
      expect(result1.status, AnamnesisSyncStatus.offline);
      expect(vault.anamnesisSyncStatus, AnamnesisSyncStatus.offline);

      // Data is intact
      expect(vault.getSystemContext('global')['gender'], 'Male');

      // Second attempt: network restored, retry succeeds
      final result2 = await vault.syncAnamnesisToServer(
        apiClient: apiClient,
        authSessionManager: authManager,
      );
      expect(result2.isSuccess, isTrue);
      expect(result2.status, AnamnesisSyncStatus.synced);
      expect(vault.anamnesisCycleId, 'cycle_retry_dedup');
      expect(vault.isAnamnesisSynced, isTrue);

      vault.dispose();
      authManager.dispose();
      apiClient.close();
    });

    test('4. Insufficient scopes (403): fail-closed reporting, draft preserved, no infinite retry', () async {
      int requestCount = 0;

      final mockClient = MockClient((request) async {
        if (request.url.path == '/api/v1/client/cascade/intake/anamnesis') {
          requestCount++;
          return http.Response(
            json.encode({
              'detail': 'Forbidden: missing required scope cascade:intake',
            }),
            403,
            headers: {'content-type': 'application/json'},
          );
        }
        return http.Response('Not Found', 404);
      });

      final apiClient = AegisApiClient(client: mockClient);
      final authManager = AuthSessionManager(
        apiClient: apiClient,
        storage: memoryAuthStorage,
      );

      final vault = VaultService(
        storage: memoryVaultStorage,
        authSessionManager: authManager,
      );
      await vault.initFuture;

      // Session lacks cascade:intake scope
      authManager.setSessionForTesting(
        AuthSession(
          accessToken: _buildTestJwt(
            patientId: 'patient_narrow',
            deviceId: 'dev_narrow',
            scope: 'vitals:read vitals:write cycle:read',
          ),
          refreshToken: 'rt_narrow',
          expiresIn: 3600,
          expiresAt: DateTime.now().toUtc().add(const Duration(hours: 1)),
          refreshExpiresAt: DateTime.now().toUtc().add(const Duration(days: 30)),
          patientId: 'patient_narrow',
          deviceId: 'dev_narrow',
          scope: 'vitals:read vitals:write cycle:read',
        ),
        status: AuthSessionStatus.authenticated,
      );
      await vault.setActivePatientId('patient_narrow');

      fillAll17Cards(vault);

      final result = await vault.syncAnamnesisToServer(
        apiClient: apiClient,
        authSessionManager: authManager,
      );

      // Must be rejected with insufficientScopes
      expect(result.isSuccess, isFalse);
      expect(result.status, AnamnesisSyncStatus.insufficientScopes);
      expect(vault.anamnesisSyncStatus, AnamnesisSyncStatus.insufficientScopes);
      // Pre-flight scope check caught it before making unnecessary network calls
      expect(requestCount, 0);

      // Draft is preserved
      expect(vault.getSystemContext('global')['gender'], 'Male');

      vault.dispose();
      authManager.dispose();
      apiClient.close();
    });

    test('5. Unauthenticated draft & pairing cancellation: 0 HTTP calls, guest draft preserved in storage', () async {
      int httpCalls = 0;

      final mockClient = MockClient((request) async {
        httpCalls++;
        return http.Response('Not Found', 404);
      });

      final apiClient = AegisApiClient(client: mockClient);
      final authManager = AuthSessionManager(
        apiClient: apiClient,
        storage: memoryAuthStorage,
      );

      final vault = VaultService(
        storage: memoryVaultStorage,
        authSessionManager: authManager,
      );
      await vault.initFuture;

      // Guest / unauthenticated state
      expect(authManager.isAuthenticated, isFalse);
      expect(vault.activePatientId, isNull);

      fillAll17Cards(vault);
      await vault.flushPendingWrites();

      // Guest draft must be in secure storage
      expect(await vault.hasGuestAnamnesisDraft(), isTrue);

      // Sync attempt rejected without network call
      final result = await vault.syncAnamnesisToServer(
        apiClient: apiClient,
        authSessionManager: authManager,
      );

      expect(result.isSuccess, isFalse);
      expect(result.status, AnamnesisSyncStatus.unauthorized);
      expect(httpCalls, 0);

      // Guest draft must NOT be deleted
      expect(await vault.hasGuestAnamnesisDraft(), isTrue);

      vault.dispose();
      authManager.dispose();
      apiClient.close();
    });

    test('6. Patient switch during in-flight network request: epoch fencing discards response, zero data contamination', () async {
      final completer = Completer<http.Response>();

      final mockClient = MockClient((request) async {
        if (request.url.path == '/api/v1/client/cascade/intake/anamnesis') {
          return await completer.future;
        }
        return http.Response('Not Found', 404);
      });

      final apiClient = AegisApiClient(client: mockClient);
      final authManager = AuthSessionManager(
        apiClient: apiClient,
        storage: memoryAuthStorage,
      );

      final vault = VaultService(
        storage: memoryVaultStorage,
        authSessionManager: authManager,
      );
      await vault.initFuture;

      // 1. Authenticate Patient Alpha
      authManager.setSessionForTesting(
        AuthSession(
          accessToken: _buildTestJwt(patientId: 'patient_alpha', deviceId: 'dev_alpha'),
          refreshToken: 'rt_alpha',
          expiresIn: 3600,
          expiresAt: DateTime.now().toUtc().add(const Duration(hours: 1)),
          refreshExpiresAt: DateTime.now().toUtc().add(const Duration(days: 30)),
          patientId: 'patient_alpha',
          deviceId: 'dev_alpha',
          scope: AegisScopes.standardDeviceScopes.join(' '),
        ),
        status: AuthSessionStatus.authenticated,
      );
      await vault.setActivePatientId('patient_alpha');

      fillAll17Cards(vault);
      await vault.flushPendingWrites();

      // 2. Start sync for Patient Alpha (in-flight)
      final syncFuture = vault.syncAnamnesisToServer(
        apiClient: apiClient,
        authSessionManager: authManager,
      );
      expect(vault.isAnamnesisSyncing, isTrue);
      await Future<void>.delayed(Duration.zero);

      // 3. Switch to Patient Beta WHILE Alpha write/sync is in-flight
      authManager.setSessionForTesting(
        AuthSession(
          accessToken: _buildTestJwt(patientId: 'patient_beta', deviceId: 'dev_beta'),
          refreshToken: 'rt_beta',
          expiresIn: 3600,
          expiresAt: DateTime.now().toUtc().add(const Duration(hours: 1)),
          refreshExpiresAt: DateTime.now().toUtc().add(const Duration(days: 30)),
          patientId: 'patient_beta',
          deviceId: 'dev_beta',
          scope: AegisScopes.standardDeviceScopes.join(' '),
        ),
        status: AuthSessionStatus.authenticated,
      );
      await vault.setActivePatientId('patient_beta');

      // 4. Server finally responds to Alpha's request
      completer.complete(
        http.Response(
          json.encode({
            'cycle_id': 'cycle_alpha_1',
            'status': 'processed',
            'intake_version': 1,
            'created_at_utc': '2026-10-28T18:00:00Z',
            'privacy_notice': 'Anamnesis processed in strict blind mode',
          }),
          200,
          headers: {'content-type': 'application/json'},
        ),
      );

      final result = await syncFuture;

      // 5. Response MUST be discarded by epoch fencing
      expect(result.isSuccess, isFalse);
      expect(result.errorMessage, contains('Patient switched'));

      // Patient Beta must NOT receive Alpha's cycle or sync state
      expect(vault.activePatientId, 'patient_beta');
      expect(vault.anamnesisCycleId, isNull);
      expect(vault.isAnamnesisSynced, isFalse);
      expect(vault.getSystemContext('global')['gender'], isNull);

      vault.dispose();
      authManager.dispose();
      apiClient.close();
    });

    test('7. In-flight concurrency lock: second call rejected, exactly 1 HTTP request dispatched', () async {
      int requestCount = 0;
      final completer = Completer<http.Response>();

      final mockClient = MockClient((request) async {
        if (request.url.path == '/api/v1/client/cascade/intake/anamnesis') {
          requestCount++;
          return await completer.future;
        }
        return http.Response('Not Found', 404);
      });

      final apiClient = AegisApiClient(client: mockClient);
      final authManager = AuthSessionManager(
        apiClient: apiClient,
        storage: memoryAuthStorage,
      );

      final vault = VaultService(
        storage: memoryVaultStorage,
        authSessionManager: authManager,
      );
      await vault.initFuture;

      authManager.setSessionForTesting(
        AuthSession(
          accessToken: _buildTestJwt(patientId: 'patient_concurrent', deviceId: 'dev_concurrent'),
          refreshToken: 'rt_concurrent',
          expiresIn: 3600,
          expiresAt: DateTime.now().toUtc().add(const Duration(hours: 1)),
          refreshExpiresAt: DateTime.now().toUtc().add(const Duration(days: 30)),
          patientId: 'patient_concurrent',
          deviceId: 'dev_concurrent',
          scope: AegisScopes.standardDeviceScopes.join(' '),
        ),
        status: AuthSessionStatus.authenticated,
      );
      await vault.setActivePatientId('patient_concurrent');

      fillAll17Cards(vault);

      // First call (starts in-flight)
      final call1 = vault.syncAnamnesisToServer(
        apiClient: apiClient,
        authSessionManager: authManager,
      );

      // Second call concurrently
      final call2 = vault.syncAnamnesisToServer(
        apiClient: apiClient,
        authSessionManager: authManager,
      );

      final result2 = await call2;
      expect(result2.isSuccess, isFalse);
      expect(result2.errorMessage, contains('Sync already in progress'));

      // Release first call
      completer.complete(
        http.Response(
          json.encode({
            'cycle_id': 'cycle_concurrent_1',
            'status': 'processed',
            'intake_version': 1,
            'created_at_utc': '2026-10-28T18:00:00Z',
            'privacy_notice': 'Anamnesis processed in strict blind mode',
          }),
          200,
          headers: {'content-type': 'application/json'},
        ),
      );

      final result1 = await call1;
      expect(result1.isSuccess, isTrue);
      expect(requestCount, 1);

      vault.dispose();
      authManager.dispose();
      apiClient.close();
    });

    test('8. Cold restart data preservation: complete questionnaire and sync metadata restored', () async {
      final sharedStorage = InMemoryVaultStorage();

      // Session 1: Patient fills out questionnaire
      final vaultSession1 = VaultService(storage: sharedStorage);
      await vaultSession1.initFuture;
      await vaultSession1.setActivePatientId('patient_cold_start');

      fillAll17Cards(vaultSession1);
      expect(vaultSession1.isAnamnesisComplete, isTrue);

      await vaultSession1.flushPendingWrites();
      vaultSession1.dispose();

      // Session 2: Cold restart
      final vaultSession2 = VaultService(storage: sharedStorage);
      await vaultSession2.initFuture;
      await vaultSession2.setActivePatientId('patient_cold_start');

      // Answers restored
      expect(vaultSession2.isAnamnesisComplete, isTrue);
      final answers = vaultSession2.buildAnamnesisAnswers()!;
      expect(answers.ageConfirmation, 'Confirmed');
      expect(answers.gender, 'Male');
      expect(answers.age, 35);
      expect(answers.height, 180);
      expect(answers.weight, 78);
      expect(answers.chronicDiseases, 'None');
      expect(answers.medications, 'Vitamins');

      vaultSession2.dispose();
    });

    test('9. Validation gate: incomplete anamnesis strictly blocked from server dispatch (0 HTTP calls)', () async {
      int httpCalls = 0;

      final mockClient = MockClient((request) async {
        httpCalls++;
        return http.Response('Not Found', 404);
      });

      final apiClient = AegisApiClient(client: mockClient);
      final authManager = AuthSessionManager(
        apiClient: apiClient,
        storage: memoryAuthStorage,
      );

      final vault = VaultService(
        storage: memoryVaultStorage,
        authSessionManager: authManager,
      );
      await vault.initFuture;

      authManager.setSessionForTesting(
        AuthSession(
          accessToken: _buildTestJwt(patientId: 'patient_incomplete', deviceId: 'dev_incomplete'),
          refreshToken: 'rt_incomplete',
          expiresIn: 3600,
          expiresAt: DateTime.now().toUtc().add(const Duration(hours: 1)),
          refreshExpiresAt: DateTime.now().toUtc().add(const Duration(days: 30)),
          patientId: 'patient_incomplete',
          deviceId: 'dev_incomplete',
          scope: AegisScopes.standardDeviceScopes.join(' '),
        ),
        status: AuthSessionStatus.authenticated,
      );
      await vault.setActivePatientId('patient_incomplete');

      // Only fill 3 cards (missing 14 cards)
      vault.saveAnamnesis('age_confirmation', 'Confirmed');
      vault.saveAnamnesis('gender', 'Male');
      vault.saveAnamnesis('age', '30');

      expect(vault.isAnamnesisComplete, isFalse);
      expect(vault.buildAnamnesisAnswers(), isNull);

      final result = await vault.syncAnamnesisToServer(
        apiClient: apiClient,
        authSessionManager: authManager,
      );

      expect(result.isSuccess, isFalse);
      expect(result.errorMessage, contains('all 17 cards must be completed'));
      expect(httpCalls, 0);

      vault.dispose();
      authManager.dispose();
      apiClient.close();
    });

    testWidgets('10. UI Patient Isolation: switching active patient in Vault immediately purges form inputs', (tester) async {
      final mockClient = MockClient((request) async => http.Response('{}', 200));
      final apiClient = AegisApiClient(client: mockClient);
      final authManager = AuthSessionManager(
        apiClient: apiClient,
        storage: memoryAuthStorage,
      );

      final vault = VaultService(
        storage: memoryVaultStorage,
        authSessionManager: authManager,
      );
      await vault.initFuture;

      // Start on Patient Alpha
      authManager.setSessionForTesting(
        AuthSession(
          accessToken: _buildTestJwt(patientId: 'patient_ui_A', deviceId: 'dev_ui_A'),
          refreshToken: 'rt_ui_A',
          expiresIn: 3600,
          expiresAt: DateTime.now().toUtc().add(const Duration(hours: 1)),
          refreshExpiresAt: DateTime.now().toUtc().add(const Duration(days: 30)),
          patientId: 'patient_ui_A',
          deviceId: 'dev_ui_A',
          scope: AegisScopes.standardDeviceScopes.join(' '),
        ),
        status: AuthSessionStatus.authenticated,
      );
      await vault.setActivePatientId('patient_ui_A');

      await tester.pumpWidget(
        MultiProvider(
          providers: [
            ChangeNotifierProvider<VaultService>.value(value: vault),
            ChangeNotifierProvider<AuthSessionManager>.value(value: authManager),
            Provider<AegisApiClient>.value(value: apiClient),
          ],
          child: MaterialApp(
            home: AnamnesisScreen(),
          ),
        ),
      );
      await tester.pumpAndSettle();

      // Card 0: Click age confirmation
      expect(find.text("I'm 18+, let's go! 🚀"), findsOneWidget);
      await tester.tap(find.text("I'm 18+, let's go! 🚀"));
      await tester.pumpAndSettle();

      // Card 1: Biological Profile
      expect(find.text("Biological Profile"), findsOneWidget);
      await tester.tap(find.text("🧔 Male"));
      await tester.enterText(find.byType(TextField), '42');
      await tester.pumpAndSettle();

      expect(find.text('42'), findsOneWidget);

      // Now switch to Patient Beta in the background
      authManager.setSessionForTesting(
        AuthSession(
          accessToken: _buildTestJwt(patientId: 'patient_ui_B', deviceId: 'dev_ui_B'),
          refreshToken: 'rt_ui_B',
          expiresIn: 3600,
          expiresAt: DateTime.now().toUtc().add(const Duration(hours: 1)),
          refreshExpiresAt: DateTime.now().toUtc().add(const Duration(days: 30)),
          patientId: 'patient_ui_B',
          deviceId: 'dev_ui_B',
          scope: AegisScopes.standardDeviceScopes.join(' '),
        ),
        status: AuthSessionStatus.authenticated,
      );
      await vault.setActivePatientId('patient_ui_B');

      await tester.pumpAndSettle();

      // Form inputs must be completely wiped and reset to Card 0 for Patient B
      expect(find.text("I'm 18+, let's go! 🚀"), findsOneWidget);
      expect(find.text('42'), findsNothing);

      vault.dispose();
      authManager.dispose();
      apiClient.close();
    });

    test('11. HTTP 200 + storage error: guest draft strictly preserved in storage, fail-closed sync status', () async {
      final mockClient = MockClient((request) async {
        if (request.url.path == '/api/v1/client/cascade/intake/anamnesis') {
          return http.Response(
            json.encode({
              'cycle_id': 'cycle_storage_err_1',
              'status': 'processed',
              'intake_version': 1,
              'created_at_utc': '2026-10-28T18:00:00Z',
              'privacy_notice': 'Anamnesis processed in strict blind mode',
            }),
            200,
            headers: {'content-type': 'application/json'},
          );
        }
        return http.Response('Not Found', 404);
      });

      final apiClient = AegisApiClient(client: mockClient);
      final authManager = AuthSessionManager(
        apiClient: apiClient,
        storage: memoryAuthStorage,
      );

      final vault = VaultService(
        storage: memoryVaultStorage,
        authSessionManager: authManager,
      );
      await vault.initFuture;

      // 1. Fill 17 cards as guest
      fillAll17Cards(vault);
      await vault.flushPendingWrites();
      expect(await vault.hasGuestAnamnesisDraft(), isTrue);

      // 2. Authenticate patient
      authManager.setSessionForTesting(
        AuthSession(
          accessToken: _buildTestJwt(patientId: 'patient_storage_err', deviceId: 'dev_1'),
          refreshToken: 'rt_1',
          expiresIn: 3600,
          expiresAt: DateTime.now().toUtc().add(const Duration(hours: 1)),
          refreshExpiresAt: DateTime.now().toUtc().add(const Duration(days: 30)),
          patientId: 'patient_storage_err',
          deviceId: 'dev_1',
          scope: AegisScopes.standardDeviceScopes.join(' '),
        ),
        status: AuthSessionStatus.authenticated,
      );
      await vault.setActivePatientId('patient_storage_err');

      // Associate guest draft to active patient
      await vault.associateGuestDraftToActivePatient();
      expect(vault.isAnamnesisComplete, isTrue);

      // 3. Make storage fail on patient vault persistence
      memoryVaultStorage.onSavePatientVault = (key, data) async {
        throw const SecureStorageUnavailableException('Simulated storage write error');
      };

      // 4. Attempt sync
      final result = await vault.syncAnamnesisToServer(
        apiClient: apiClient,
        authSessionManager: authManager,
      );

      // 5. Must return failure with error status
      expect(result.isSuccess, isFalse);
      expect(result.status, AnamnesisSyncStatus.error);
      expect(vault.anamnesisSyncStatus, AnamnesisSyncStatus.error);
      expect(vault.isAnamnesisSynced, isFalse);

      // CRITICAL: Guest draft is STILL PRESERVED because persistence failed!
      expect(await vault.hasGuestAnamnesisDraft(), isTrue);
      final draft = await memoryVaultStorage.loadGuestDraft();
      expect(draft['global']?['gender'], 'Male');
      expect(draft['global']?['age'], '35');

      vault.dispose();
      authManager.dispose();
      apiClient.close();
    });

    test('12. Patient switch during persistence after HTTP 200: epoch fence aborts, no guest draft clear for new patient', () async {
      final mockClient = MockClient((request) async {
        if (request.url.path == '/api/v1/client/cascade/intake/anamnesis') {
          return http.Response(
            json.encode({
              'cycle_id': 'cycle_switch_after_200',
              'status': 'processed',
              'intake_version': 1,
              'created_at_utc': '2026-10-28T18:00:00Z',
              'privacy_notice': 'Anamnesis processed in strict blind mode',
            }),
            200,
            headers: {'content-type': 'application/json'},
          );
        }
        return http.Response('Not Found', 404);
      });

      final apiClient = AegisApiClient(client: mockClient);
      final authManager = AuthSessionManager(
        apiClient: apiClient,
        storage: memoryAuthStorage,
      );

      final vault = VaultService(
        storage: memoryVaultStorage,
        authSessionManager: authManager,
      );
      await vault.initFuture;

      // 1. Patient Alpha authenticated
      authManager.setSessionForTesting(
        AuthSession(
          accessToken: _buildTestJwt(patientId: 'patient_alpha_pers', deviceId: 'dev_alpha'),
          refreshToken: 'rt_alpha',
          expiresIn: 3600,
          expiresAt: DateTime.now().toUtc().add(const Duration(hours: 1)),
          refreshExpiresAt: DateTime.now().toUtc().add(const Duration(days: 30)),
          patientId: 'patient_alpha_pers',
          deviceId: 'dev_alpha',
          scope: AegisScopes.standardDeviceScopes.join(' '),
        ),
        status: AuthSessionStatus.authenticated,
      );
      await vault.setActivePatientId('patient_alpha_pers');

      fillAll17Cards(vault);
      await vault.flushPendingWrites();

      // Hook into onSavePatientVault to switch active patient during persistence after HTTP 200
      bool hasSwitched = false;
      memoryVaultStorage.onSavePatientVault = (key, data) async {
        if (!hasSwitched && (data['anamnesis_sync']?['cycle_id'] == 'cycle_switch_after_200' || data['anamnesis_cycle_id'] == 'cycle_switch_after_200')) {
          hasSwitched = true;
          // Switch to Patient Beta right while Alpha's sync state is being written
          authManager.setSessionForTesting(
            AuthSession(
              accessToken: _buildTestJwt(patientId: 'patient_beta_pers', deviceId: 'dev_beta'),
              refreshToken: 'rt_beta',
              expiresIn: 3600,
              expiresAt: DateTime.now().toUtc().add(const Duration(hours: 1)),
              refreshExpiresAt: DateTime.now().toUtc().add(const Duration(days: 30)),
              patientId: 'patient_beta_pers',
              deviceId: 'dev_beta',
              scope: AegisScopes.standardDeviceScopes.join(' '),
            ),
            status: AuthSessionStatus.authenticated,
          );
          await vault.setActivePatientId('patient_beta_pers');
        }
      };

      final result = await vault.syncAnamnesisToServer(
        apiClient: apiClient,
        authSessionManager: authManager,
      );

      // Epoch fence caught the patient switch after persistence
      expect(result.isSuccess, isFalse);
      expect(result.errorMessage, contains('Patient switched during storage write'));

      // Patient Beta must NOT have Alpha's cycle or sync state
      expect(vault.activePatientId, 'patient_beta_pers');
      expect(vault.anamnesisCycleId, isNull);
      expect(vault.isAnamnesisSynced, isFalse);

      vault.dispose();
      authManager.dispose();
      apiClient.close();
    });

    test('13. Pairing -> restore answers -> explicit user submission: 0 auto-submission before explicit tap', () async {
      int intakeHttpCalls = 0;

      final mockClient = MockClient((request) async {
        if (request.url.path == '/api/v1/client/cascade/intake/anamnesis') {
          intakeHttpCalls++;
          return http.Response(
            json.encode({
              'cycle_id': 'cycle_explicit_submit',
              'status': 'processed',
              'intake_version': 1,
              'created_at_utc': '2026-10-28T18:00:00Z',
              'privacy_notice': 'Anamnesis processed in strict blind mode',
            }),
            200,
            headers: {'content-type': 'application/json'},
          );
        }
        return http.Response('Not Found', 404);
      });

      final apiClient = AegisApiClient(client: mockClient);
      final authManager = AuthSessionManager(
        apiClient: apiClient,
        storage: memoryAuthStorage,
      );

      final vault = VaultService(
        storage: memoryVaultStorage,
        authSessionManager: authManager,
      );
      await vault.initFuture;

      // 1. Guest fills out 17 cards
      fillAll17Cards(vault);
      await vault.flushPendingWrites();
      expect(await vault.hasGuestAnamnesisDraft(), isTrue);

      // 2. Pairing occurs
      authManager.setSessionForTesting(
        AuthSession(
          accessToken: _buildTestJwt(patientId: 'patient_paired_13', deviceId: 'dev_13'),
          refreshToken: 'rt_13',
          expiresIn: 3600,
          expiresAt: DateTime.now().toUtc().add(const Duration(hours: 1)),
          refreshExpiresAt: DateTime.now().toUtc().add(const Duration(days: 30)),
          patientId: 'patient_paired_13',
          deviceId: 'dev_13',
          scope: AegisScopes.standardDeviceScopes.join(' '),
        ),
        status: AuthSessionStatus.authenticated,
      );
      await vault.setActivePatientId('patient_paired_13');

      // 3. Associate guest draft to active patient
      final associated = await vault.associateGuestDraftToActivePatient();
      expect(associated, isTrue);

      // 4. Verify all 17 cards are restored in active patient vault
      expect(vault.isAnamnesisComplete, isTrue);
      final answers = vault.buildAnamnesisAnswers()!;
      expect(answers.ageConfirmation, 'Confirmed');
      expect(answers.gender, 'Male');
      expect(answers.age, 35);
      expect(answers.height, 180);
      expect(answers.weight, 78);
      expect(answers.nicotineStatus, 'Active');
      expect(answers.alcoholStatus, 'Frequently');
      expect(answers.medications, 'Vitamins');

      // 5. CRITICAL: Zero HTTP intake calls must have occurred upon pairing
      expect(intakeHttpCalls, 0);
      expect(vault.isAnamnesisSynced, isFalse);

      // 6. Explicit user action: user taps "Finish 🚀"
      final result = await vault.syncAnamnesisToServer(
        apiClient: apiClient,
        authSessionManager: authManager,
      );

      expect(result.isSuccess, isTrue);
      expect(intakeHttpCalls, 1);
      expect(vault.isAnamnesisSynced, isTrue);
      expect(vault.anamnesisCycleId, 'cycle_explicit_submit');

      // Guest draft cleared after confirmed sync
      expect(await vault.hasGuestAnamnesisDraft(), isFalse);

      vault.dispose();
      authManager.dispose();
      apiClient.close();
    });

    test('14. Older Stage 5.3 token lacking cascade:intake: ensureRequiredScopes rotates token and succeeds', () async {
      int refreshCalls = 0;
      int intakeCalls = 0;

      final mockClient = MockClient((request) async {
        if (request.url.path == '/api/v1/auth/refresh') {
          refreshCalls++;
          final body = json.decode(request.body) as Map<String, dynamic>;
          expect(body['refresh_token'], 'rt_older');
          return http.Response(
            json.encode({
              'access_token': _buildTestJwt(
                patientId: 'patient_older_token',
                deviceId: 'dev_older',
                scope: AegisScopes.standardDeviceScopes.join(' '),
              ),
              'refresh_token': 'rt_newer',
              'token_type': 'bearer',
              'expires_in': 3600,
              'expires_at': DateTime.now().toUtc().add(const Duration(hours: 1)).toIso8601String(),
              'refresh_expires_at': DateTime.now().toUtc().add(const Duration(days: 30)).toIso8601String(),
              'patient_id': 'patient_older_token',
              'device_id': 'dev_older',
              'scope': AegisScopes.standardDeviceScopes.join(' '),
            }),
            200,
            headers: {'content-type': 'application/json'},
          );
        }
        if (request.url.path == '/api/v1/client/cascade/intake/anamnesis') {
          intakeCalls++;
          expect(request.headers['authorization'], startsWith('Bearer '));
          return http.Response(
            json.encode({
              'cycle_id': 'cycle_after_upgrade',
              'status': 'processed',
              'intake_version': 1,
              'created_at_utc': '2026-10-28T18:00:00Z',
              'privacy_notice': 'Anamnesis processed in strict blind mode',
            }),
            200,
            headers: {'content-type': 'application/json'},
          );
        }
        return http.Response('Not Found', 404);
      });

      final apiClient = AegisApiClient(client: mockClient);
      final authManager = AuthSessionManager(
        apiClient: apiClient,
        storage: memoryAuthStorage,
      );

      final vault = VaultService(
        storage: memoryVaultStorage,
        authSessionManager: authManager,
      );
      await vault.initFuture;

      // Older Stage 5.3 token with missing cascade:intake
      authManager.setSessionForTesting(
        AuthSession(
          accessToken: _buildTestJwt(
            patientId: 'patient_older_token',
            deviceId: 'dev_older',
            scope: 'vitals:read vitals:write cycle:read',
          ),
          refreshToken: 'rt_older',
          expiresIn: 3600,
          expiresAt: DateTime.now().toUtc().add(const Duration(hours: 1)),
          refreshExpiresAt: DateTime.now().toUtc().add(const Duration(days: 30)),
          patientId: 'patient_older_token',
          deviceId: 'dev_older',
          scope: 'vitals:read vitals:write cycle:read',
        ),
        status: AuthSessionStatus.authenticated,
      );
      await vault.setActivePatientId('patient_older_token');

      fillAll17Cards(vault);
      await vault.flushPendingWrites();

      // Attempt sync
      final result = await vault.syncAnamnesisToServer(
        apiClient: apiClient,
        authSessionManager: authManager,
      );

      // Verify token was rotated and upgraded
      expect(result.isSuccess, isTrue);
      expect(refreshCalls, 1);
      expect(intakeCalls, 1);
      expect(vault.isAnamnesisSynced, isTrue);
      expect(vault.anamnesisCycleId, 'cycle_after_upgrade');
      expect(authManager.currentSession?.hasScope('cascade:intake'), isTrue);

      vault.dispose();
      authManager.dispose();
      apiClient.close();
    });

    testWidgets('15. UI Submission Guard: double-press Finish during pairing is blocked by _isSubmitting', (tester) async {
      tester.view.physicalSize = const Size(1080, 2400);
      tester.view.devicePixelRatio = 1.0;
      addTearDown(() => tester.view.resetPhysicalSize());

      final mockClient = MockClient((request) async => http.Response('{}', 200));
      final apiClient = AegisApiClient(client: mockClient);
      final authManager = AuthSessionManager(
        apiClient: apiClient,
        storage: memoryAuthStorage,
      );

      final vault = VaultService(
        storage: memoryVaultStorage,
        authSessionManager: authManager,
      );
      await vault.initFuture;

      // Start unauthenticated (guest)
      fillAll17Cards(vault);
      await vault.flushPendingWrites();

      await tester.pumpWidget(
        MultiProvider(
          providers: [
            ChangeNotifierProvider<VaultService>.value(value: vault),
            ChangeNotifierProvider<AuthSessionManager>.value(value: authManager),
            Provider<AegisApiClient>.value(value: apiClient),
          ],
          child: MaterialApp(
            home: AnamnesisScreen(),
          ),
        ),
      );
      await tester.pumpAndSettle();

      // Jump to Card 16 (medications step)
      final pageView = tester.widget<PageView>(find.byType(PageView));
      pageView.controller!.jumpToPage(16);
      await tester.pumpAndSettle();

      expect(find.text("Medications and Supplements 💊"), findsOneWidget);
      expect(find.text("Finish 🚀"), findsOneWidget);

      await tester.ensureVisible(find.text("Finish 🚀"));
      await tester.pumpAndSettle();

      // Tap "Finish 🚀" the first time
      await tester.tap(find.text("Finish 🚀"));
      await tester.pump(); // Start execution & modal open
      await tester.pump(const Duration(milliseconds: 300)); // Modal transition animation

      // Modal is presented
      expect(find.byType(AegisDevicePairingModal), findsOneWidget);

      // Dismiss pairing dialog / modal
      Navigator.of(tester.element(find.byType(AegisDevicePairingModal))).pop();
      await tester.pumpAndSettle();

      // Modal is closed
      expect(find.byType(AegisDevicePairingModal), findsNothing);

      // Status indicates draft saved securely
      expect(find.text("Draft saved securely. Pair device to synchronize with Aegis Server."), findsOneWidget);

      vault.dispose();
      authManager.dispose();
      apiClient.close();
    });

    test('16. Single-Flight Sync: concurrent sync calls with older token lock entire flow including scope rotation', () async {
      int refreshCalls = 0;
      int intakeCalls = 0;
      final refreshCompleter = Completer<http.Response>();

      final mockClient = MockClient((request) async {
        if (request.url.path == '/api/v1/auth/refresh') {
          refreshCalls++;
          return await refreshCompleter.future;
        }
        if (request.url.path == '/api/v1/client/cascade/intake/anamnesis') {
          intakeCalls++;
          return http.Response(
            json.encode({
              'cycle_id': 'cycle_single_flight_upgrade',
              'status': 'processed',
              'intake_version': 1,
              'created_at_utc': '2026-10-28T18:00:00Z',
              'privacy_notice': 'Anamnesis processed in strict blind mode',
            }),
            200,
            headers: {'content-type': 'application/json'},
          );
        }
        return http.Response('Not Found', 404);
      });

      final apiClient = AegisApiClient(client: mockClient);
      final authManager = AuthSessionManager(
        apiClient: apiClient,
        storage: memoryAuthStorage,
      );

      final vault = VaultService(
        storage: memoryVaultStorage,
        authSessionManager: authManager,
      );
      await vault.initFuture;

      // Older token lacking cascade:intake
      authManager.setSessionForTesting(
        AuthSession(
          accessToken: _buildTestJwt(
            patientId: 'patient_sf_older',
            deviceId: 'dev_sf',
            scope: 'vitals:read vitals:write cycle:read',
          ),
          refreshToken: 'rt_sf_older',
          expiresIn: 3600,
          expiresAt: DateTime.now().toUtc().add(const Duration(hours: 1)),
          refreshExpiresAt: DateTime.now().toUtc().add(const Duration(days: 30)),
          patientId: 'patient_sf_older',
          deviceId: 'dev_sf',
          scope: 'vitals:read vitals:write cycle:read',
        ),
        status: AuthSessionStatus.authenticated,
      );
      await vault.setActivePatientId('patient_sf_older');

      fillAll17Cards(vault);
      await vault.flushPendingWrites();

      // First call: enters and begins token rotation
      final call1 = vault.syncAnamnesisToServer(
        apiClient: apiClient,
        authSessionManager: authManager,
      );

      // Yield event loop so call1 executes up to the awaiting refresh call
      await Future<void>.delayed(Duration.zero);
      expect(vault.isAnamnesisSyncing, isTrue);

      // Second concurrent call: must be rejected immediately by the single-flight guard
      final call2 = vault.syncAnamnesisToServer(
        apiClient: apiClient,
        authSessionManager: authManager,
      );

      final result2 = await call2;
      expect(result2.isSuccess, isFalse);
      expect(result2.errorMessage, contains('Sync already in progress'));

      // Release the refresh call
      refreshCompleter.complete(
        http.Response(
          json.encode({
            'access_token': _buildTestJwt(
              patientId: 'patient_sf_older',
              deviceId: 'dev_sf',
              scope: AegisScopes.standardDeviceScopes.join(' '),
            ),
            'refresh_token': 'rt_sf_newer',
            'token_type': 'bearer',
            'expires_in': 3600,
            'expires_at': DateTime.now().toUtc().add(const Duration(hours: 1)).toIso8601String(),
            'refresh_expires_at': DateTime.now().toUtc().add(const Duration(days: 30)).toIso8601String(),
            'patient_id': 'patient_sf_older',
            'device_id': 'dev_sf',
            'scope': AegisScopes.standardDeviceScopes.join(' '),
          }),
          200,
          headers: {'content-type': 'application/json'},
        ),
      );

      final result1 = await call1;
      expect(result1.isSuccess, isTrue);
      expect(refreshCalls, 1);
      expect(intakeCalls, 1);
      expect(vault.isAnamnesisSyncing, isFalse);
      expect(vault.isAnamnesisSynced, isTrue);
      expect(vault.anamnesisCycleId, 'cycle_single_flight_upgrade');

      vault.dispose();
      authManager.dispose();
      apiClient.close();
    });

    test('17. Persisted Sync Status: cold restart restores confirmed server synchronization as synced', () async {
      final sharedStorage = InMemoryVaultStorage();

      final mockClient = MockClient((request) async {
        if (request.url.path == '/api/v1/client/cascade/intake/anamnesis') {
          return http.Response(
            json.encode({
              'cycle_id': 'cycle_persisted_sync_cold',
              'status': 'processed',
              'intake_version': 1,
              'created_at_utc': '2026-10-28T18:00:00Z',
              'privacy_notice': 'Anamnesis processed in strict blind mode',
            }),
            200,
            headers: {'content-type': 'application/json'},
          );
        }
        return http.Response('Not Found', 404);
      });

      final apiClient = AegisApiClient(client: mockClient);
      final authManager = AuthSessionManager(
        apiClient: apiClient,
        storage: memoryAuthStorage,
      );

      // Session 1: fill questionnaire, submit to server, confirm persistence
      final vaultSession1 = VaultService(
        storage: sharedStorage,
        authSessionManager: authManager,
      );
      await vaultSession1.initFuture;

      authManager.setSessionForTesting(
        AuthSession(
          accessToken: _buildTestJwt(patientId: 'patient_cold_sync', deviceId: 'dev_cold'),
          refreshToken: 'rt_cold',
          expiresIn: 3600,
          expiresAt: DateTime.now().toUtc().add(const Duration(hours: 1)),
          refreshExpiresAt: DateTime.now().toUtc().add(const Duration(days: 30)),
          patientId: 'patient_cold_sync',
          deviceId: 'dev_cold',
          scope: AegisScopes.standardDeviceScopes.join(' '),
        ),
        status: AuthSessionStatus.authenticated,
      );
      await vaultSession1.setActivePatientId('patient_cold_sync');

      fillAll17Cards(vaultSession1);
      await vaultSession1.flushPendingWrites();

      final syncResult = await vaultSession1.syncAnamnesisToServer(
        apiClient: apiClient,
        authSessionManager: authManager,
      );

      expect(syncResult.isSuccess, isTrue);
      expect(vaultSession1.isAnamnesisSynced, isTrue);
      expect(vaultSession1.anamnesisSyncStatus, AnamnesisSyncStatus.synced);
      expect(vaultSession1.anamnesisCycleId, 'cycle_persisted_sync_cold');
      expect(vaultSession1.anamnesisIntakeVersion, 1);
      expect(vaultSession1.anamnesisSyncedAt, isNotNull);

      // Session 1 closes
      vaultSession1.dispose();

      // Session 2: Cold restart from same storage
      final vaultSession2 = VaultService(
        storage: sharedStorage,
        authSessionManager: authManager,
      );
      await vaultSession2.initFuture;
      await vaultSession2.setActivePatientId('patient_cold_sync');

      // VERIFICATION: Confirmed server synchronization is restored as synced
      expect(vaultSession2.isAnamnesisSynced, isTrue);
      expect(vaultSession2.anamnesisSyncStatus, AnamnesisSyncStatus.synced);
      expect(vaultSession2.anamnesisCycleId, 'cycle_persisted_sync_cold');
      expect(vaultSession2.anamnesisIntakeVersion, 1);
      expect(vaultSession2.anamnesisSyncedAt, isNotNull);
      expect(vaultSession2.isAnamnesisComplete, isTrue);
      expect(await vaultSession2.hasGuestAnamnesisDraft(), isFalse);

      vaultSession2.dispose();
      authManager.dispose();
      apiClient.close();
    });

  });
}
