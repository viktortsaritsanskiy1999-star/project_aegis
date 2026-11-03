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
  String deviceId = 'dev_multi_week_test',
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
  String deviceId = 'dev_multi_week_test',
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

  const testCycleId = 'cycle_multi_week_001';
  const patientAlpha = 'patient_alpha_multi';
  const patientBeta = 'patient_beta_multi';

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

  group('PROJECT AEGIS — ATHENA MULTI-WEEK DAILY PULSE IDEMPOTENCY', () {
    test('1. Week 1 vs Week 2 keys: Week 2 Day 1 generates _w2_d1 and cannot collision-replay Week 1 Day 1', () async {
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
      vault.setAthenaSprintDay(7); // Simulate patient completed all 7 days of Week 1

      final serverRequests = <Map<String, dynamic>>[];

      // Mock server where Week 1 Weekly Consilium is now COMPLETED
      final client = MockClient((request) async {
        if (request.url.path.endsWith('/api/v1/client/cascade/status')) {
          return http.Response(
            json.encode({
              'patient_id': patientAlpha,
              'device_id': 'dev_multi_week_test',
              'has_active_cycle': true,
              'active_cycle_id': testCycleId,
              'is_primary_consilium_completed': true,
              'current_weekly_sprint_id': 'sprint_${testCycleId}_w1',
              'weekly_sprint_number': 1,
              'is_weekly_consilium_completed': true, // Week 1 is finished!
              'can_start_next_sprint': true,
              'next_sprint_id': 'sprint_${testCycleId}_w2',
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
              'reply': 'Athena Week 2 Day 1 clinical check-in: Welcome to Week 2!',
              'emergency_trigger': false,
              'cycle_id': testCycleId,
              'sprint_day': 1,
              'message_id': 'msg_athena_w2_d1',
              'created_at': '2026-11-09T10:00:00Z',
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
      final req = serverRequests.first;
      expect(req['event_type'], 'daily_pulse');
      expect(req['sprint_day'], 1);

      // CRITICAL ASSERTION: Key contains _w2_d1, distinct from _w1_d1!
      expect(req['idempotency_key'], 'athena_daily_pulse_${testCycleId}_w2_d1');
      expect(req['idempotency_key'], isNot(equals('athena_daily_pulse_${testCycleId}_w1_d1')));

      // Vault persistence check
      expect(vault.getAthenaSprintDay(), 1);
      expect(
        vault.getSystemContext('global')['last_daily_pulse_key'],
        'athena_daily_pulse_${testCycleId}_w2_d1',
      );
    });

    test('2. Multiple subsequent weeks: authoritative progression across Week 1, Week 2, and Week 3', () async {
      await vault.recordClientReports(
        cycleId: testCycleId,
        reports: [_buildSamplePrimaryReport(testCycleId)],
        expectedPatientId: patientAlpha,
      );

      vault.saveCuratorChatMessage({'sender': 'curator', 'text': 'Greeting'});
      vault.updateAthenaLastPromptTime(DateTime.now().subtract(const Duration(hours: 25)).millisecondsSinceEpoch);

      // Verify resolveAuthoritativeSprintNumber helper logic directly
      // Scenario A: Week 1 in progress
      expect(
        AthenaTriggerService.resolveAuthoritativeSprintNumber(
          status: const ClientCascadeStatusResponse(
            patientId: patientAlpha,
            deviceId: 'dev',
            hasActiveCycle: true,
            isPrimaryConsiliumCompleted: true,
            weeklySprintNumber: 1,
            isWeeklyConsiliumCompleted: false,
          ),
          vault: vault,
        ),
        1,
      );

      // Scenario B: Week 1 completed, transitioning to Week 2
      expect(
        AthenaTriggerService.resolveAuthoritativeSprintNumber(
          status: const ClientCascadeStatusResponse(
            patientId: patientAlpha,
            deviceId: 'dev',
            hasActiveCycle: true,
            isPrimaryConsiliumCompleted: true,
            weeklySprintNumber: 1,
            isWeeklyConsiliumCompleted: true,
            nextSprintId: 'sprint_${testCycleId}_w2',
          ),
          vault: vault,
        ),
        2,
      );

      // Scenario C: Week 2 in progress
      expect(
        AthenaTriggerService.resolveAuthoritativeSprintNumber(
          status: const ClientCascadeStatusResponse(
            patientId: patientAlpha,
            deviceId: 'dev',
            hasActiveCycle: true,
            isPrimaryConsiliumCompleted: true,
            weeklySprintNumber: 2,
            isWeeklyConsiliumCompleted: false,
          ),
          vault: vault,
        ),
        2,
      );

      // Scenario D: Week 2 completed, transitioning to Week 3
      expect(
        AthenaTriggerService.resolveAuthoritativeSprintNumber(
          status: const ClientCascadeStatusResponse(
            patientId: patientAlpha,
            deviceId: 'dev',
            hasActiveCycle: true,
            isPrimaryConsiliumCompleted: true,
            weeklySprintNumber: 2,
            isWeeklyConsiliumCompleted: true,
            nextSprintId: 'sprint_${testCycleId}_w3',
          ),
          vault: vault,
        ),
        3,
      );

      // Scenario E: Week 3 active
      expect(
        AthenaTriggerService.resolveAuthoritativeSprintNumber(
          status: const ClientCascadeStatusResponse(
            patientId: patientAlpha,
            deviceId: 'dev',
            hasActiveCycle: true,
            isPrimaryConsiliumCompleted: true,
            weeklySprintNumber: 3,
            isWeeklyConsiliumCompleted: false,
          ),
          vault: vault,
        ),
        3,
      );
    });

    test('3. Repeated callbacks: periodic timer ticks do not duplicate turn or issue extra network calls', () async {
      await vault.recordClientReports(
        cycleId: testCycleId,
        reports: [_buildSamplePrimaryReport(testCycleId)],
        expectedPatientId: patientAlpha,
      );

      vault.saveCuratorChatMessage({'sender': 'curator', 'text': 'Day 1 Question'});
      final t0 = DateTime.now().subtract(const Duration(hours: 25));
      vault.updateAthenaLastPromptTime(t0.millisecondsSinceEpoch);
      vault.setAthenaSprintDay(1);

      int athenaChatCalls = 0;

      final client = MockClient((request) async {
        if (request.url.path.endsWith('/api/v1/client/cascade/status')) {
          return http.Response(
            json.encode({
              'patient_id': patientAlpha,
              'device_id': 'dev_multi_week_test',
              'has_active_cycle': true,
              'active_cycle_id': testCycleId,
              'is_primary_consilium_completed': true,
              'weekly_sprint_number': 1,
              'is_weekly_consilium_completed': false,
            }),
            200,
            headers: {'content-type': 'application/json'},
          );
        }

        if (request.url.path.endsWith('/api/v1/client/chat/athena')) {
          athenaChatCalls++;
          return http.Response(
            json.encode({
              'reply': 'Athena Day 2 Question',
              'emergency_trigger': false,
              'cycle_id': testCycleId,
              'sprint_day': 2,
              'message_id': 'msg_d2',
              'created_at': '2026-11-03T10:00:00Z',
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

      // Tick 1: Successfully sends pulse
      final res1 = await athenaTrigger.checkAndTrigger(
        vaultService: vault,
        timeService: timeService,
        apiClient: apiClient,
        authSessionManager: authManager,
      );
      expect(res1, isTrue);
      expect(athenaChatCalls, 1);
      expect(vault.getAthenaSprintDay(), 2);

      // Tick 2 (simulating 10-second periodic timer callback): Skipped because key recorded
      final res2 = await athenaTrigger.checkAndTrigger(
        vaultService: vault,
        timeService: timeService,
        apiClient: apiClient,
        authSessionManager: authManager,
      );
      expect(res2, isFalse);
      expect(athenaChatCalls, 1); // ZERO extra network calls

      // Tick 3 (another periodic timer callback): Still skipped
      final res3 = await athenaTrigger.checkAndTrigger(
        vaultService: vault,
        timeService: timeService,
        apiClient: apiClient,
        authSessionManager: authManager,
      );
      expect(res3, isFalse);
      expect(athenaChatCalls, 1);
    });

    test('4. Patient / cycle switching: fences sessionEpoch and cycle isolation without cross-patient leakage', () async {
      await vault.recordClientReports(
        cycleId: testCycleId,
        reports: [_buildSamplePrimaryReport(testCycleId)],
        expectedPatientId: patientAlpha,
      );

      vault.saveCuratorChatMessage({'sender': 'curator', 'text': 'Day 1 Question'});
      final t0 = DateTime.now().subtract(const Duration(hours: 25));
      vault.updateAthenaLastPromptTime(t0.millisecondsSinceEpoch);
      vault.setAthenaSprintDay(1);

      late AuthSessionManager authManager;

      final client = MockClient((request) async {
        if (request.url.path.endsWith('/api/v1/client/cascade/status')) {
          return http.Response(
            json.encode({
              'patient_id': patientAlpha,
              'device_id': 'dev_multi_week_test',
              'has_active_cycle': true,
              'active_cycle_id': testCycleId,
              'is_primary_consilium_completed': true,
              'weekly_sprint_number': 1,
              'is_weekly_consilium_completed': false,
            }),
            200,
            headers: {'content-type': 'application/json'},
          );
        }

        if (request.url.path.endsWith('/api/v1/client/chat/athena')) {
          // Switch patient context while network request is inflight
          authManager.setSessionForTesting(
            _buildTestSession(patientId: patientBeta),
            status: AuthSessionStatus.authenticated,
          );

          return http.Response(
            json.encode({
              'reply': 'Athena Day 2 for Alpha',
              'emergency_trigger': false,
              'cycle_id': testCycleId,
              'sprint_day': 2,
              'message_id': 'msg_cross_leak',
              'created_at': '2026-11-03T10:00:00Z',
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

      final res = await athenaTrigger.checkAndTrigger(
        vaultService: vault,
        timeService: timeService,
        apiClient: apiClient,
        authSessionManager: authManager,
      );

      // Dropped at post-async boundary fence
      expect(res, isFalse);
      expect(vault.getAthenaSprintDay(), 1); // Sprint day unchanged
      expect(vault.getCuratorChatHistory().length, 1); // Unchanged
    });

    test('5. Delayed Weekly Consilium: Continuous accompaniment smoothly proceeds across calendar weeks while Weekly is delayed', () async {
      await vault.recordClientReports(
        cycleId: testCycleId,
        reports: [_buildSamplePrimaryReport(testCycleId)],
        expectedPatientId: patientAlpha,
      );

      vault.saveCuratorChatMessage({'sender': 'curator', 'text': 'Day 7 Question'});
      final t0 = DateTime.now().subtract(const Duration(hours: 25));
      vault.updateAthenaLastPromptTime(t0.millisecondsSinceEpoch);
      vault.setAthenaSprintDay(7); // Day 7 finished

      int weeklyNotificationCount = 0;
      int athenaChatCalls = 0;
      final capturedKeys = <String>[];
      final capturedSprintNumbers = <int?>[];
      final capturedSprintDays = <int?>[];

      final client = MockClient((request) async {
        if (request.url.path.endsWith('/api/v1/client/cascade/status')) {
          final isDay9 = vault.getAthenaSprintDay() == 1;
          return http.Response(
            json.encode({
              'patient_id': patientAlpha,
              'device_id': 'dev_multi_week_test',
              'has_active_cycle': true,
              'active_cycle_id': testCycleId,
              'is_primary_consilium_completed': true,
              'weekly_sprint_number': 1,
              'is_weekly_consilium_completed': false, // Weekly Consilium has NOT run yet
              'can_start_next_sprint': true,
              'next_sprint_id': 'sprint_cycle_multi_week_001_w1',
              'current_calendar_week': 2,
              'current_calendar_day': isDay9 ? 2 : 1,
            }),
            200,
            headers: {'content-type': 'application/json'},
          );
        }

        if (request.url.path.endsWith('/api/v1/client/chat/athena')) {
          athenaChatCalls++;
          final body = json.decode(request.body) as Map<String, dynamic>;
          capturedKeys.add(body['idempotency_key'] as String);
          capturedSprintNumbers.add(body['sprint_number'] as int?);
          capturedSprintDays.add(body['sprint_day'] as int?);
          final sDay = body['sprint_day'] as int? ?? 1;

          return http.Response(
            json.encode({
              'reply': 'Athena Week 2 Day $sDay observation during delayed Weekly.',
              'emergency_trigger': false,
              'cycle_id': testCycleId,
              'sprint_day': sDay,
              'sprint_number': body['sprint_number'],
              'message_id': 'msg_delayed_w2_d$sDay',
              'created_at': DateTime.now().toUtc().toIso8601String(),
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

      // Day 8 arrival (25h elapsed, new calendar day -> Week 2 Day 1)
      final day8Attempt = await athenaTrigger.checkAndTrigger(
        vaultService: vault,
        timeService: timeService,
        apiClient: apiClient,
        authSessionManager: authManager,
        onWeeklyConsiliumAvailable: () {
          weeklyNotificationCount++;
        },
      );

      // Continuous accompaniment executes Week 2 Day 1 while notifying Weekly availability
      expect(day8Attempt, isTrue);
      expect(weeklyNotificationCount, 1);
      expect(athenaChatCalls, 1);
      expect(capturedKeys.first, 'athena_daily_pulse_${testCycleId}_w2_d1');
      expect(capturedSprintNumbers.first, 2);
      expect(capturedSprintDays.first, 1);
      expect(vault.getAthenaSprintDay(), 1);

      // Delay continues: Day 9 arrival (another 24h elapsed -> Week 2 Day 2)
      timeService.fastForward(const Duration(hours: 24));
      final day9Attempt = await athenaTrigger.checkAndTrigger(
        vaultService: vault,
        timeService: timeService,
        apiClient: apiClient,
        authSessionManager: authManager,
        onWeeklyConsiliumAvailable: () {
          weeklyNotificationCount++;
        },
      );

      expect(day9Attempt, isTrue);
      expect(weeklyNotificationCount, 2);
      expect(athenaChatCalls, 2);
      expect(capturedKeys.last, 'athena_daily_pulse_${testCycleId}_w2_d2');
      expect(capturedSprintNumbers.last, 2);
      expect(capturedSprintDays.last, 2);
      expect(vault.getAthenaSprintDay(), 2);

      // Repeated timer tick on same Day 9: skips duplicate execution
      final repeatAttempt = await athenaTrigger.checkAndTrigger(
        vaultService: vault,
        timeService: timeService,
        apiClient: apiClient,
        authSessionManager: authManager,
      );
      expect(repeatAttempt, isFalse);
      expect(athenaChatCalls, 2);
    });
  });
}
