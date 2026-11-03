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
  String deviceId = 'dev_test_a2_1',
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
  String deviceId = 'dev_test_a2_1',
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

  group('PROJECT AEGIS — STAGE A2.1: RESPONSE-DRIVEN ACCOMPANIMENT FLUTTER INTEGRATION', () {
    const testCycleId = 'cycle_test_a2_1';
    const patientAlpha = 'patient_alpha_123';

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

      await vault.recordClientReports(
        cycleId: testCycleId,
        reports: [_buildSamplePrimaryReport(testCycleId)],
        expectedPatientId: patientAlpha,
      );

      vault.saveCuratorChatMessage({
        'sender': 'curator',
        'text': 'Initial Athena Greeting Question',
      });
      final t0 = DateTime.now().subtract(const Duration(hours: 25));
      vault.updateAthenaLastPromptTime(t0.millisecondsSinceEpoch);
      vault.setAthenaSprintDay(1);
    });

    tearDown(() {
      athenaTrigger.dispose();
      timeService.resetToRealTime();
    });

    test('1. ClientCascadeStatusResponse serializes and deserializes routine question fields correctly', () {
      final jsonMap = {
        'patient_id': patientAlpha,
        'device_id': 'dev_test_a2_1',
        'has_active_cycle': true,
        'active_cycle_id': testCycleId,
        'is_primary_consilium_completed': true,
        'has_unanswered_routine_question': true,
        'active_routine_question_id': 'rq_test_w1_d1_1234',
        'active_routine_question_text': 'Как ваше утреннее самочувствие?',
        'routine_question_reminders_count': 1,
        'can_send_routine_reminder': true,
      };

      final response = ClientCascadeStatusResponse.fromJson(jsonMap);
      expect(response.hasUnansweredRoutineQuestion, isTrue);
      expect(response.activeRoutineQuestionId, 'rq_test_w1_d1_1234');
      expect(response.activeRoutineQuestionText, 'Как ваше утреннее самочувствие?');
      expect(response.routineQuestionRemindersCount, 1);
      expect(response.canSendRoutineReminder, isTrue);

      final serialized = response.toJson();
      expect(serialized['has_unanswered_routine_question'], isTrue);
      expect(serialized['active_routine_question_id'], 'rq_test_w1_d1_1234');
      expect(serialized['active_routine_question_text'], 'Как ваше утреннее самочувствие?');
      expect(serialized['routine_question_reminders_count'], 1);
      expect(serialized['can_send_routine_reminder'], isTrue);
    });

    test('2. AthenaChatResponse serializes and deserializes question_id and routine_question_status correctly', () {
      final jsonMap = {
        'reply': 'Здравствуйте! Спасибо за ответ.',
        'emergency_trigger': false,
        'cycle_id': testCycleId,
        'sprint_day': 2,
        'sprint_number': 1,
        'message_id': 'msg_test_1234',
        'created_at': '2026-11-03T12:00:00Z',
        'question_id': 'rq_test_w1_d2_abcd',
        'routine_question_status': 'unanswered',
        'reminders_count': 0,
      };

      final response = AthenaChatResponse.fromJson(jsonMap);
      expect(response.questionId, 'rq_test_w1_d2_abcd');
      expect(response.routineQuestionStatus, 'unanswered');
      expect(response.remindersCount, 0);

      final serialized = response.toJson();
      expect(serialized['question_id'], 'rq_test_w1_d2_abcd');
      expect(serialized['routine_question_status'], 'unanswered');
      expect(serialized['reminders_count'], 0);
    });

    test('3. AthenaTriggerService skips daily_pulse when server reports unanswered routine question', () async {
      int athenaChatCallCount = 0;
      final mockClient = MockClient((request) async {
        if (request.url.path.endsWith('/api/v1/client/cascade/status')) {
          return http.Response(
            json.encode({
              'patient_id': patientAlpha,
              'device_id': 'dev_test_a2_1',
              'has_active_cycle': true,
              'active_cycle_id': testCycleId,
              'is_primary_consilium_completed': true,
              'current_weekly_sprint_id': 'sprint_week_1',
              'weekly_sprint_number': 1,
              'current_calendar_week': 1,
              'current_calendar_day': 2,
              'has_unanswered_routine_question': true,
              'active_routine_question_id': 'rq_pending_question',
              'active_routine_question_text': 'Did you take your morning medication?',
              'routine_question_reminders_count': 0,
              'can_send_routine_reminder': true,
            }),
            200,
            headers: {'content-type': 'application/json'},
          );
        }

        if (request.url.path.endsWith('/api/v1/client/chat/athena')) {
          athenaChatCallCount++;
          return http.Response(
            json.encode({
              'reply': 'Next question',
              'cycle_id': testCycleId,
              'message_id': 'msg_test',
              'created_at': DateTime.now().toIso8601String(),
            }),
            200,
            headers: {'content-type': 'application/json'},
          );
        }

        return http.Response('Not found', 404);
      });

      final apiClient = AegisApiClient(client: mockClient, baseUrl: 'https://test.aegis.health');
      final authManager = AuthSessionManager(
        apiClient: apiClient,
        storage: InMemoryAuthTokenStorage(),
      );
      authManager.setSessionForTesting(
        _buildTestSession(patientId: patientAlpha),
        status: AuthSessionStatus.authenticated,
      );

      final executed = await athenaTrigger.checkAndTrigger(
        vaultService: vault,
        timeService: timeService,
        apiClient: apiClient,
        authSessionManager: authManager,
      );

      expect(executed, isFalse, reason: 'Must skip daily pulse when routine question is unanswered');
      expect(athenaChatCallCount, equals(0), reason: 'Zero Gemini/chat calls made while question is unanswered');
    });

    test('4. AthenaTriggerService triggers daily_pulse when server reports NO unanswered routine question', () async {
      int athenaChatCallCount = 0;
      final mockClient = MockClient((request) async {
        if (request.url.path.endsWith('/api/v1/client/cascade/status')) {
          return http.Response(
            json.encode({
              'patient_id': patientAlpha,
              'device_id': 'dev_test_a2_1',
              'has_active_cycle': true,
              'active_cycle_id': testCycleId,
              'is_primary_consilium_completed': true,
              'current_weekly_sprint_id': 'sprint_week_1',
              'weekly_sprint_number': 1,
              'current_calendar_week': 1,
              'current_calendar_day': 2,
              'has_unanswered_routine_question': false,
              'active_routine_question_id': null,
              'active_routine_question_text': null,
              'routine_question_reminders_count': 0,
              'can_send_routine_reminder': false,
            }),
            200,
            headers: {'content-type': 'application/json'},
          );
        }

        if (request.url.path.endsWith('/api/v1/client/chat/athena')) {
          athenaChatCallCount++;
          final body = json.decode(request.body) as Map<String, dynamic>;
          expect(body['event_type'], equals('daily_pulse'));

          return http.Response(
            json.encode({
              'reply': 'Next question for Day 2',
              'cycle_id': testCycleId,
              'sprint_day': 2,
              'sprint_number': 1,
              'message_id': 'msg_test_day2',
              'created_at': DateTime.now().toIso8601String(),
              'question_id': 'rq_day2_new',
              'routine_question_status': 'unanswered',
              'reminders_count': 0,
            }),
            200,
            headers: {'content-type': 'application/json'},
          );
        }

        return http.Response('Not found', 404);
      });

      final apiClient = AegisApiClient(client: mockClient, baseUrl: 'https://test.aegis.health');
      final authManager = AuthSessionManager(
        apiClient: apiClient,
        storage: InMemoryAuthTokenStorage(),
      );
      authManager.setSessionForTesting(
        _buildTestSession(patientId: patientAlpha),
        status: AuthSessionStatus.authenticated,
      );

      final executed = await athenaTrigger.checkAndTrigger(
        vaultService: vault,
        timeService: timeService,
        apiClient: apiClient,
        authSessionManager: authManager,
      );

      expect(executed, isTrue, reason: 'Must execute daily pulse when no routine question is unanswered');
      expect(athenaChatCallCount, equals(1), reason: 'Exactly one daily_pulse chat call made');
    });
  });
}
