// ignore_for_file: depend_on_referenced_packages
import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';

import 'package:aegis_health/services/api/aegis_api_client.dart';
import 'package:aegis_health/services/api/medical_ai_api_models.dart';
import 'package:aegis_health/services/athena_trigger_service.dart';
import 'package:aegis_health/services/auth/auth_models.dart';
import 'package:aegis_health/services/auth/auth_session_manager.dart';
import 'package:aegis_health/services/auth/auth_token_storage.dart';
import 'package:aegis_health/services/time_service.dart';
import 'package:aegis_health/services/vault_service.dart';
import 'package:aegis_health/services/vault_storage_service.dart';

String _buildTestJwt({
  required String patientId,
  String deviceId = 'dev_test_daily_pulse',
  List<String> scopes = const [
    'cascade:intake',
    'cascade:run',
    'cycle:read',
    'reports:read',
    'chat:read',
    'chat:write',
  ],
}) {
  final header = base64Url
      .encode(utf8.encode(json.encode({'alg': 'HS256', 'typ': 'JWT'})))
      .replaceAll('=', '');
  final payload = base64Url
      .encode(utf8.encode(json.encode({
        'sub': patientId,
        'patient_id': patientId,
        'device_id': deviceId,
        'did': deviceId,
        'scope': scopes.join(' '),
        'scopes': scopes,
        'type': 'access',
        'jti': 'jti_${DateTime.now().microsecondsSinceEpoch}',
        'exp': DateTime.now().add(const Duration(hours: 1)).millisecondsSinceEpoch ~/ 1000,
      })))
      .replaceAll('=', '');
  return '$header.$payload.signature_mock_bytes';
}

AuthSession _buildTestSession({
  required String patientId,
  String deviceId = 'dev_test_daily_pulse',
}) {
  return AuthSession(
    accessToken: _buildTestJwt(patientId: patientId, deviceId: deviceId),
    refreshToken: 'rf_$patientId',
    expiresIn: 3600,
    expiresAt: DateTime.now().toUtc().add(const Duration(hours: 1)),
    refreshExpiresAt: DateTime.now().toUtc().add(const Duration(days: 30)),
    patientId: patientId,
    deviceId: deviceId,
    scope: 'cascade:intake cascade:run cycle:read reports:read chat:read chat:write',
  );
}

ClientModelReportResponse _buildSamplePrimaryReport(String cycleId) {
  return ClientModelReportResponse.fromJson({
    'report_id': 'rep_primary_overall',
    'cycle_id': cycleId,
    'model': 'gemini-3.1-pro',
    'report_type': 'user_overall_report',
    'privacy_level': 'client_facing',
    'version': 1,
    'content': {
      'title': 'Primary Consilium',
      'summary': 'Baseline established.',
      'report_date': '2026-10-25',
    },
    'created_at': '2026-10-25T10:00:00Z',
  });
}

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  const testCycleId = 'cycle_test_athena_dp';
  const patientAlpha = 'patient_alpha_123';
  const patientBeta = 'patient_beta_456';

  late VaultService vault;
  late TimeService timeService;
  late AthenaTriggerService athenaTrigger;

  setUp(() async {
    vault = VaultService(storage: InMemoryVaultStorage());
    await vault.initFuture;
    await vault.setActivePatientId(patientAlpha);
    timeService = TimeService();
    timeService.resetToRealTime();
    athenaTrigger = AthenaTriggerService();
  });

  tearDown(() {
    athenaTrigger.dispose();
    timeService.resetToRealTime();
  });

  group('ARGON BUG-03: Athena Daily Pulse Server Integration', () {
    test('1. Successful authenticated daily pulse via backend endpoint (zero local Gemini SDK)', () async {
      // Setup Primary Consilium completed in Vault
      await vault.recordClientReports(
        cycleId: testCycleId,
        reports: [_buildSamplePrimaryReport(testCycleId)],
        expectedPatientId: patientAlpha,
      );

      // Record initial greeting (anchor T0)
      vault.saveCuratorChatMessage({
        'sender': 'curator',
        'text': 'Initial Athena Greeting Question',
      });
      final t0 = DateTime.now().subtract(const Duration(hours: 25));
      vault.updateAthenaLastPromptTime(t0.millisecondsSinceEpoch);
      vault.setAthenaSprintDay(1);

      final serverRequests = <Map<String, dynamic>>[];

      final client = MockClient((request) async {
        if (request.url.path.endsWith('/api/v1/client/cascade/status')) {
          return http.Response(
            json.encode({
              'patient_id': patientAlpha,
              'device_id': 'dev_test_daily_pulse',
              'has_active_cycle': true,
              'active_cycle_id': testCycleId,
              'is_primary_consilium_completed': true,
              'current_weekly_sprint_id': 'sprint_week_1',
              'weekly_sprint_number': 1,
              'is_weekly_consilium_completed': false,
              'can_start_next_sprint': false,
            }),
            200,
            headers: {'content-type': 'application/json'},
          );
        }

        if (request.url.path.endsWith('/api/v1/client/chat/athena')) {
          final body = json.decode(request.body) as Map<String, dynamic>;
          serverRequests.add(body);

          return http.Response(
            json.encode({
              'reply': 'Athena Day 2 clinical check-in: Any morning fatigue or shortness of breath?',
              'emergency_trigger': false,
              'emergency_notice': null,
              'cycle_id': testCycleId,
              'sprint_day': 2,
              'message_id': 'msg_athena_d2_test',
              'created_at': '2026-11-02T10:00:00Z',
            }),
            200,
            headers: {'content-type': 'application/json'},
          );
        }

        return http.Response('Not Found', 404);
      });

      final apiClient = AegisApiClient(client: client, baseUrl: 'https://test.aegis.health');
      final authManager = AuthSessionManager(
        apiClient: apiClient,
        storage: InMemoryAuthTokenStorage(),
      );
      authManager.setSessionForTesting(
        _buildTestSession(patientId: patientAlpha),
        status: AuthSessionStatus.authenticated,
      );

      final triggered = await athenaTrigger.checkAndTrigger(
        vaultService: vault,
        timeService: timeService,
        apiClient: apiClient,
        authSessionManager: authManager,
      );

      expect(triggered, isTrue);
      expect(serverRequests.length, 1);
      expect(serverRequests.first['event_type'], 'daily_pulse');
      expect(serverRequests.first['sprint_day'], 2);
      expect(serverRequests.first['idempotency_key'], 'athena_daily_pulse_${testCycleId}_w1_d2');

      // Verify Vault persistence
      expect(vault.getAthenaSprintDay(), 2);
      expect(vault.hasUnreadAthenaMessage, isTrue);
      final history = vault.getCuratorChatHistory();
      expect(history.length, 2);
      expect(history.last['sender'], 'curator');
      expect(history.last['text'], contains('Athena Day 2 clinical check-in'));
      expect(
        vault.getSystemContext('global')['last_daily_pulse_key'],
        'athena_daily_pulse_${testCycleId}_w1_d2',
      );
    });

    test('2. Stable idempotency prevents duplicate questions on timer retries and clock rewinds', () async {
      await vault.recordClientReports(
        cycleId: testCycleId,
        reports: [_buildSamplePrimaryReport(testCycleId)],
        expectedPatientId: patientAlpha,
      );

      vault.saveCuratorChatMessage({
        'sender': 'curator',
        'text': 'Initial Greeting',
      });
      final t0 = DateTime.now().subtract(const Duration(hours: 25));
      vault.updateAthenaLastPromptTime(t0.millisecondsSinceEpoch);
      vault.setAthenaSprintDay(1);

      int chatEndpointCalls = 0;
      final client = MockClient((request) async {
        if (request.url.path.endsWith('/api/v1/client/cascade/status')) {
          return http.Response(
            json.encode({
              'patient_id': patientAlpha,
              'device_id': 'dev_test_daily_pulse',
              'has_active_cycle': true,
              'active_cycle_id': testCycleId,
              'is_primary_consilium_completed': true,
            }),
            200,
            headers: {'content-type': 'application/json'},
          );
        }
        if (request.url.path.endsWith('/api/v1/client/chat/athena')) {
          chatEndpointCalls++;
          return http.Response(
            json.encode({
              'reply': 'Athena Day 2 Question',
              'emergency_trigger': false,
              'cycle_id': testCycleId,
              'sprint_day': 2,
              'message_id': 'msg_athena_d2',
              'created_at': '2026-11-02T10:00:00Z',
            }),
            200,
            headers: {'content-type': 'application/json'},
          );
        }
        return http.Response('Not Found', 404);
      });

      final apiClient = AegisApiClient(client: client, baseUrl: 'https://test.aegis.health');
      final authManager = AuthSessionManager(
        apiClient: apiClient,
        storage: InMemoryAuthTokenStorage(),
      );
      authManager.setSessionForTesting(
        _buildTestSession(patientId: patientAlpha),
        status: AuthSessionStatus.authenticated,
      );

      // First trigger succeeds
      final firstTrigger = await athenaTrigger.checkAndTrigger(
        vaultService: vault,
        timeService: timeService,
        apiClient: apiClient,
        authSessionManager: authManager,
      );
      expect(firstTrigger, isTrue);
      expect(chatEndpointCalls, 1);
      expect(vault.getCuratorChatHistory().length, 2);

      // Immediate retry (e.g. periodic timer tick) -> should be skipped because last_daily_pulse_key already recorded
      final secondTrigger = await athenaTrigger.checkAndTrigger(
        vaultService: vault,
        timeService: timeService,
        apiClient: apiClient,
        authSessionManager: authManager,
      );
      expect(secondTrigger, isFalse);
      expect(chatEndpointCalls, 1);
      expect(vault.getCuratorChatHistory().length, 2);

      // Fast forward time slightly (1 hour, still same calendar day) -> skipped (hoursDiff < 20)
      timeService.fastForward(const Duration(hours: 1));
      final sameDayTrigger = await athenaTrigger.checkAndTrigger(
        vaultService: vault,
        timeService: timeService,
        apiClient: apiClient,
        authSessionManager: authManager,
      );
      expect(sameDayTrigger, isFalse);
      expect(chatEndpointCalls, 1);
      expect(vault.getCuratorChatHistory().length, 2);
    });

    test('3. Preserves initial_greeting as schedule anchor T0; waits if history is empty', () async {
      await vault.recordClientReports(
        cycleId: testCycleId,
        reports: [_buildSamplePrimaryReport(testCycleId)],
        expectedPatientId: patientAlpha,
      );

      // History is empty: user has not opened chat or completed post-primary intro
      expect(vault.getCuratorChatHistory(), isEmpty);

      timeService.fastForward(const Duration(hours: 30));

      int chatEndpointCalls = 0;
      final client = MockClient((request) async {
        if (request.url.path.endsWith('/api/v1/client/chat/athena')) {
          chatEndpointCalls++;
        }
        return http.Response('OK', 200);
      });

      final apiClient = AegisApiClient(client: client, baseUrl: 'https://test.aegis.health');
      final authManager = AuthSessionManager(
        apiClient: apiClient,
        storage: InMemoryAuthTokenStorage(),
      );
      authManager.setSessionForTesting(
        _buildTestSession(patientId: patientAlpha),
        status: AuthSessionStatus.authenticated,
      );

      final triggered = await athenaTrigger.checkAndTrigger(
        vaultService: vault,
        timeService: timeService,
        apiClient: apiClient,
        authSessionManager: authManager,
      );

      // Must NOT send daily_pulse when history is empty
      expect(triggered, isFalse);
      expect(chatEndpointCalls, 0);
    });

    test('4. Blocked if Primary Consilium is not completed locally or on server', () async {
      // Local vault reports Primary Consilium NOT completed
      expect(vault.isConsiliumCompleted, isFalse);

      vault.saveCuratorChatMessage({'sender': 'curator', 'text': 'Greeting'});
      final t0 = DateTime.now().subtract(const Duration(hours: 25));
      vault.updateAthenaLastPromptTime(t0.millisecondsSinceEpoch);

      int chatEndpointCalls = 0;
      final client = MockClient((request) async {
        chatEndpointCalls++;
        return http.Response('OK', 200);
      });

      final apiClient = AegisApiClient(client: client, baseUrl: 'https://test.aegis.health');
      final authManager = AuthSessionManager(
        apiClient: apiClient,
        storage: InMemoryAuthTokenStorage(),
      );
      authManager.setSessionForTesting(
        _buildTestSession(patientId: patientAlpha),
        status: AuthSessionStatus.authenticated,
      );

      final triggered = await athenaTrigger.checkAndTrigger(
        vaultService: vault,
        timeService: timeService,
        apiClient: apiClient,
        authSessionManager: authManager,
      );

      expect(triggered, isFalse);
      expect(chatEndpointCalls, 0);
    });

    test('5. Patient switch / session epoch mismatch across async boundary fails closed', () async {
      await vault.recordClientReports(
        cycleId: testCycleId,
        reports: [_buildSamplePrimaryReport(testCycleId)],
        expectedPatientId: patientAlpha,
      );

      vault.saveCuratorChatMessage({'sender': 'curator', 'text': 'Greeting'});
      final t0 = DateTime.now().subtract(const Duration(hours: 25));
      vault.updateAthenaLastPromptTime(t0.millisecondsSinceEpoch);
      vault.setAthenaSprintDay(1);

      late AuthSessionManager authManager;

      final client = MockClient((request) async {
        if (request.url.path.endsWith('/api/v1/client/cascade/status')) {
          return http.Response(
            json.encode({
              'patient_id': patientAlpha,
              'device_id': 'dev_test_daily_pulse',
              'has_active_cycle': true,
              'active_cycle_id': testCycleId,
              'is_primary_consilium_completed': true,
            }),
            200,
            headers: {'content-type': 'application/json'},
          );
        }

        if (request.url.path.endsWith('/api/v1/client/chat/athena')) {
          // Simulate patient switch while network request is in flight!
          authManager.setSessionForTesting(
            _buildTestSession(patientId: patientBeta),
            status: AuthSessionStatus.authenticated,
          );

          return http.Response(
            json.encode({
              'reply': 'Athena Day 2 Question for Alpha',
              'emergency_trigger': false,
              'cycle_id': testCycleId,
              'sprint_day': 2,
              'message_id': 'msg_leak_attempt',
              'created_at': '2026-11-02T10:00:00Z',
            }),
            200,
            headers: {'content-type': 'application/json'},
          );
        }

        return http.Response('Not Found', 404);
      });

      final apiClient = AegisApiClient(client: client, baseUrl: 'https://test.aegis.health');
      authManager = AuthSessionManager(
        apiClient: apiClient,
        storage: InMemoryAuthTokenStorage(),
      );
      authManager.setSessionForTesting(
        _buildTestSession(patientId: patientAlpha),
        status: AuthSessionStatus.authenticated,
      );

      final triggered = await athenaTrigger.checkAndTrigger(
        vaultService: vault,
        timeService: timeService,
        apiClient: apiClient,
        authSessionManager: authManager,
      );

      // Trigger must fail closed and NOT persist to vault
      expect(triggered, isFalse);
      expect(vault.getAthenaSprintDay(), 1);
      expect(vault.getCuratorChatHistory().length, 1); // Only initial greeting, no response added
    });

    test('6. Day 7 weekly boundary: executes Day 7 pulse, does NOT run client cascade, notifies onWeeklyConsiliumAvailable', () async {
      await vault.recordClientReports(
        cycleId: testCycleId,
        reports: [_buildSamplePrimaryReport(testCycleId)],
        expectedPatientId: patientAlpha,
      );

      vault.saveCuratorChatMessage({'sender': 'curator', 'text': 'Day 6 Question'});
      final t0 = DateTime.now().subtract(const Duration(hours: 25));
      vault.updateAthenaLastPromptTime(t0.millisecondsSinceEpoch);
      vault.setAthenaSprintDay(6);

      bool weeklyBoundaryNotified = false;

      final client = MockClient((request) async {
        if (request.url.path.endsWith('/api/v1/client/cascade/status')) {
          final isDay8 = vault.getAthenaSprintDay() == 7;
          return http.Response(
            json.encode({
              'patient_id': patientAlpha,
              'device_id': 'dev_test_daily_pulse',
              'has_active_cycle': true,
              'active_cycle_id': testCycleId,
              'is_primary_consilium_completed': true,
              'current_weekly_sprint_id': isDay8 ? 'sprint_week_2' : 'sprint_week_1',
              'weekly_sprint_number': isDay8 ? 2 : 1,
              'is_weekly_consilium_completed': false,
              'current_calendar_week': isDay8 ? 2 : 1,
              'current_calendar_day': isDay8 ? 1 : 7,
            }),
            200,
            headers: {'content-type': 'application/json'},
          );
        }

        if (request.url.path.endsWith('/api/v1/client/chat/athena')) {
          final body = json.decode(request.body) as Map<String, dynamic>;
          final sDay = body['sprint_day'] as int? ?? 7;
          return http.Response(
            json.encode({
              'reply': 'Athena Day $sDay question: Continuous accompaniment.',
              'emergency_trigger': false,
              'cycle_id': testCycleId,
              'sprint_day': sDay,
              'sprint_number': body['sprint_number'],
              'message_id': 'msg_athena_d$sDay',
              'created_at': '2026-11-07T10:00:00Z',
            }),
            200,
            headers: {'content-type': 'application/json'},
          );
        }

        return http.Response('Not Found', 404);
      });

      final apiClient = AegisApiClient(client: client, baseUrl: 'https://test.aegis.health');
      final authManager = AuthSessionManager(
        apiClient: apiClient,
        storage: InMemoryAuthTokenStorage(),
      );
      authManager.setSessionForTesting(
        _buildTestSession(patientId: patientAlpha),
        status: AuthSessionStatus.authenticated,
      );

      final triggered = await athenaTrigger.checkAndTrigger(
        vaultService: vault,
        timeService: timeService,
        apiClient: apiClient,
        authSessionManager: authManager,
        onWeeklyConsiliumAvailable: () {
          weeklyBoundaryNotified = true;
        },
      );

      expect(triggered, isTrue);
      expect(weeklyBoundaryNotified, isTrue);
      expect(vault.getAthenaSprintDay(), 7);

      // Next check on day 7+ (Day 8 / Week 2 Day 1) while weekly consilium is delayed:
      // Continuous accompaniment: notifies Weekly Consilium availability AND executes Day 1 of Week 2!
      timeService.fastForward(const Duration(hours: 26));
      bool day8Notified = false;
      final day8Trigger = await athenaTrigger.checkAndTrigger(
        vaultService: vault,
        timeService: timeService,
        apiClient: apiClient,
        authSessionManager: authManager,
        onWeeklyConsiliumAvailable: () {
          day8Notified = true;
        },
      );

      expect(day8Trigger, isTrue);
      expect(day8Notified, isTrue);
      expect(vault.getAthenaSprintDay(), 1);
    });

    test('7. Network error fails closed safely without advancing sprint day', () async {
      await vault.recordClientReports(
        cycleId: testCycleId,
        reports: [_buildSamplePrimaryReport(testCycleId)],
        expectedPatientId: patientAlpha,
      );

      vault.saveCuratorChatMessage({'sender': 'curator', 'text': 'Day 1 Question'});
      final t0 = DateTime.now().subtract(const Duration(hours: 25));
      vault.updateAthenaLastPromptTime(t0.millisecondsSinceEpoch);
      vault.setAthenaSprintDay(1);

      final client = MockClient((request) async {
        if (request.url.path.endsWith('/api/v1/client/cascade/status')) {
          return http.Response(
            json.encode({
              'patient_id': patientAlpha,
              'device_id': 'dev_test_daily_pulse',
              'has_active_cycle': true,
              'active_cycle_id': testCycleId,
              'is_primary_consilium_completed': true,
            }),
            200,
            headers: {'content-type': 'application/json'},
          );
        }
        if (request.url.path.endsWith('/api/v1/client/chat/athena')) {
          return http.Response(
            json.encode({'detail': 'Athena upstream timeout'}),
            504,
            headers: {'content-type': 'application/json'},
          );
        }
        return http.Response('Not Found', 404);
      });

      final apiClient = AegisApiClient(client: client, baseUrl: 'https://test.aegis.health');
      final authManager = AuthSessionManager(
        apiClient: apiClient,
        storage: InMemoryAuthTokenStorage(),
      );
      authManager.setSessionForTesting(
        _buildTestSession(patientId: patientAlpha),
        status: AuthSessionStatus.authenticated,
      );

      final triggered = await athenaTrigger.checkAndTrigger(
        vaultService: vault,
        timeService: timeService,
        apiClient: apiClient,
        authSessionManager: authManager,
      );

      expect(triggered, isFalse);
      expect(vault.getAthenaSprintDay(), 1); // Sprint day was NOT advanced on network failure
      expect(vault.getCuratorChatHistory().length, 1); // No message appended
    });
  });
}
