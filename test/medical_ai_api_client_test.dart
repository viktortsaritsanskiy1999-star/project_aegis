// ignore_for_file: depend_on_referenced_packages
import 'dart:convert';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';

import 'package:aegis_health/services/api/aegis_api_client.dart';
import 'package:aegis_health/services/api/medical_ai_api_models.dart';
import 'package:aegis_health/services/api/vitals_api_models.dart';

/// Helper to build valid 3-part base64url JWT
String _buildTestJwt({
  required String patientId,
  String deviceId = 'dev_test_01',
  List<String> scopes = const ['*'],
  String? jti,
}) {
  final header = base64Url.encode(utf8.encode(json.encode({'alg': 'HS256', 'typ': 'JWT'}))).replaceAll('=', '');
  final payload = base64Url.encode(utf8.encode(json.encode({
    'sub': patientId,
    'patient_id': patientId,
    'device_id': deviceId,
    'type': 'access',
    'scopes': scopes,
    'jti': jti ?? 'jti_${DateTime.now().microsecondsSinceEpoch}',
    'exp': DateTime.now().add(const Duration(hours: 1)).millisecondsSinceEpoch ~/ 1000,
  }))).replaceAll('=', '');
  return '$header.$payload.signature_mock_bytes';
}

/// Sample 17-card answers helper
AnamnesisQuestionnaireAnswers _sampleAnswers() {
  return const AnamnesisQuestionnaireAnswers(
    ageConfirmation: 'Confirmed',
    gender: 'Male',
    age: 35,
    height: 180,
    weight: 78,
    nicotineStatus: 'Non-user',
    alcoholStatus: 'Rarely',
    specFactors: 'None',
    sleepQuality: 'Good',
    dietTypes: 'Mediterranean',
    dietHabits: 'Consistent',
    giSignals: 'None',
    workType: 'Sedentary',
    chronicDiseases: 'None',
    recentIllness: 'None',
    criticalInfections: 'None',
    heredityRisks: 'None',
    medications: 'Vitamins',
  );
}

/// Sample client model report helper
ClientModelReportResponse _sampleClientReport({
  required String reportId,
  required String reportType,
  String title = 'System Report',
  String summary = 'All systems nominal.',
}) {
  return ClientModelReportResponse(
    reportId: reportId,
    cycleId: 'cycle_test_01',
    model: 'gemini-3.6-flash',
    reportType: reportType,
    privacyLevel: 'client_facing',
    version: 1,
    content: ClientReportContent(
      title: title,
      summary: summary,
      reportDate: '2026-10-28',
      metrics: {'vitality_score': 95, 'risk_band': 'low'},
      highlights: ['Optimal resting heart rate', 'Consistent recovery profile'],
      entries: [
        const ClientDatedSystemEntry(
          date: '2026-10-28',
          summary: 'System stable with low variance.',
          hasChanges: false,
        ),
      ],
    ),
    createdAt: DateTime.parse('2026-10-28T08:00:00Z'),
  );
}

void main() {
  group('Stage 5.4C: Medical AI DTOs Serialization & Deserialization', () {
    test('1. AnamnesisQuestionnaireAnswers and Intake roundtrip without client cycle_id', () {
      final answers = _sampleAnswers();
      final request = AnamnesisIntakeRequest(answers: answers);
      final jsonMap = request.toJson();

      // Client must strictly NOT designate or serialize cycle_id
      expect(jsonMap.containsKey('cycle_id'), isFalse);
      expect(jsonMap['answers']['age'], equals(35));
      expect(jsonMap['answers']['gender'], equals('Male'));
      expect(jsonMap['answers']['age_confirmation'], equals('Confirmed'));

      final deserialized = AnamnesisIntakeRequest.fromJson(jsonMap);
      expect(deserialized.answers.age, equals(35));
      expect(deserialized.answers.gender, equals('Male'));
      expect(deserialized.answers.nicotineStatus, equals('Non-user'));

      final response = AnamnesisIntakeClientResponse.fromJson({
        'cycle_id': 'cycle_test_99',
        'status': 'processed',
        'intake_version': 1,
        'created_at_utc': '2026-10-28T09:00:00Z',
        'privacy_notice': 'Anamnesis processed in strict blind mode; raw answers stored securely under server_only privacy level.',
      });

      expect(response.cycleId, equals('cycle_test_99'));
      expect(response.status, equals('processed'));
      expect(response.intakeVersion, equals(1));
      expect(response.createdAtUtc, equals('2026-10-28T09:00:00Z'));
      expect(response.privacyNotice, contains('blind mode'));
      expect(response.toJson()['status'], equals('processed'));
    });

    test('2. ClientCascadeStatusResponse roundtrip and boolean defaults', () {
      final statusMap = {
        'patient_id': 'pat_101',
        'device_id': 'dev_101',
        'has_active_cycle': true,
        'active_cycle_id': 'cycle_101',
        'has_preparatory_anamnesis': true,
        'has_preparatory_labs': false,
        'has_preparatory_ecg': false,
        'is_primary_consilium_completed': false,
        'has_unreviewed_lab_changes': true,
      };

      final status = ClientCascadeStatusResponse.fromJson(statusMap);
      expect(status.patientId, equals('pat_101'));
      expect(status.deviceId, equals('dev_101'));
      expect(status.hasActiveCycle, isTrue);
      expect(status.activeCycleId, equals('cycle_101'));
      expect(status.hasPreparatoryAnamnesis, isTrue);
      expect(status.hasPreparatoryLabs, isFalse);
      expect(status.isPrimaryConsiliumCompleted, isFalse);
      expect(status.hasUnreviewedLabChanges, isTrue);

      final serialized = status.toJson();
      expect(serialized['patient_id'], equals('pat_101'));
      expect(serialized['has_unreviewed_lab_changes'], isTrue);
    });

    test('3. LabObservationEntry and LabLedgerStateResponse roundtrip', () {
      final ledgerMap = {
        'cycle_id': 'cycle_lab_01',
        'ledger_version': 2,
        'last_evaluated_ledger_version': 1,
        'has_unreviewed_lab_changes': true,
        'evaluation_status': 'UNREVIEWED_CHANGES_PRESENT',
        'evaluation_notice': 'New lab values have been recorded but not yet reviewed by a new Consilium.',
        'has_critical_flags': false,
        'safety_notice': 'Medical safety cooldown notice',
        'active_markers': {
          'glucose': {
            'observation_id': 'obs_glu_01',
            'marker_key': 'glucose',
            'display_name': 'Fasting Glucose',
            'value': 5.2,
            'numeric_value': 5.2,
            'unit': 'mmol/L',
            'sample_date': '2026-10-27',
            'reference_range': {'low': 3.9, 'high': 6.1, 'text_range': '3.9 - 6.1'},
            'event_type': 'initial',
            'is_active': true,
            'is_critical_flag': false,
            'is_uncertain': false,
            'recorded_at': '2026-10-27T10:00:00Z',
          },
        },
        'observations_history': [
          {
            'observation_id': 'obs_glu_01',
            'marker_key': 'glucose',
            'display_name': 'Fasting Glucose',
            'value': 5.2,
            'numeric_value': 5.2,
            'unit': 'mmol/L',
            'sample_date': '2026-10-27',
            'recorded_at': '2026-10-27T10:00:00Z',
          }
        ],
      };

      final ledger = LabLedgerStateResponse.fromJson(ledgerMap);
      expect(ledger.cycleId, equals('cycle_lab_01'));
      expect(ledger.ledgerVersion, equals(2));
      expect(ledger.hasUnreviewedLabChanges, isTrue);
      expect(ledger.activeMarkers.containsKey('glucose'), isTrue);

      final glucose = ledger.activeMarkers['glucose']!;
      expect(glucose.displayName, equals('Fasting Glucose'));
      expect(glucose.numericValue, equals(5.2));
      expect(glucose.unit, equals('mmol/L'));
      expect(glucose.referenceRange?.low, equals(3.9));
      expect(glucose.referenceRange?.high, equals(6.1));
      expect(glucose.isActive, isTrue);

      final reserialized = ledger.toJson();
      expect(reserialized['ledger_version'], equals(2));
      expect((reserialized['active_markers'] as Map)['glucose']['value'], equals(5.2));
    });

    test('4. PrimaryConsiliumClientResponse and ClientModelReportResponse roundtrip', () {
      final overall = _sampleClientReport(
        reportId: 'rep_overall_01',
        reportType: 'user_overall_report',
        title: 'Master Health Review',
      );
      final cvs = _sampleClientReport(
        reportId: 'rep_cvs_01',
        reportType: 'system_user_report_cvs',
        title: 'Cardiovascular Analysis',
      );

      final primaryResponse = PrimaryConsiliumClientResponse(
        cycleId: 'cycle_pc_01',
        reportDate: '2026-10-28',
        overallReport: overall,
        systemReports: {'cvs': cvs},
        vitalsThresholds: const ClientVitalsThresholds(
          minPessimumBpm: 48,
          maxPessimumBpm: 110,
          maxSpikeDeltaBpm: 22,
        ),
      );

      final jsonMap = primaryResponse.toJson();
      expect(jsonMap['cycle_id'], equals('cycle_pc_01'));
      expect(jsonMap['vitals_thresholds']['min_pessimum_bpm'], equals(48));
      expect((jsonMap['system_reports'] as Map).containsKey('cvs'), isTrue);

      final deserialized = PrimaryConsiliumClientResponse.fromJson(jsonMap);
      expect(deserialized.cycleId, equals('cycle_pc_01'));
      expect(deserialized.vitalsThresholds.minPessimumBpm, equals(48));
      expect(deserialized.vitalsThresholds.maxPessimumBpm, equals(110));
      expect(deserialized.vitalsThresholds.maxSpikeDeltaBpm, equals(22));
      expect(deserialized.overallReport.content.title, equals('Master Health Review'));
      expect(deserialized.systemReports['cvs']?.content.title, equals('Cardiovascular Analysis'));
    });

    test('5. Athena Chat Request, Response, and History roundtrip', () {
      final req = AthenaChatRequest(
        message: 'Hello Athena, how is my heart rate today?',
        eventType: 'user_message',
        vitalsAlert: const AthenaVitalsAlertContext(
          currentPulse: 118,
          baselinePulse: 72,
          measuredAt: '2026-10-28T09:15:00Z',
          notes: 'Sudden spike during rest',
        ),
        sprintDay: 3,
        idempotencyKey: 'idem_key_athena_42',
      );

      final reqJson = req.toJson();
      expect(reqJson['event_type'], equals('user_message'));
      expect(reqJson['vitals_alert']['current_pulse'], equals(118));
      expect(reqJson['idempotency_key'], equals('idem_key_athena_42'));

      final deserializedReq = AthenaChatRequest.fromJson(reqJson);
      expect(deserializedReq.vitalsAlert?.baselinePulse, equals(72));
      expect(deserializedReq.idempotencyKey, equals('idem_key_athena_42'));

      final resp = AthenaChatResponse.fromJson({
        'reply': 'Your heart rate elevated temporarily. Take 5 deep breaths and rest.',
        'emergency_trigger': false,
        'emergency_notice': null,
        'cycle_id': 'cycle_athena_01',
        'sprint_day': 3,
        'message_id': 'msg_resp_77',
        'created_at': '2026-10-28T09:15:02Z',
      });
      expect(resp.reply, contains('5 deep breaths'));
      expect(resp.emergencyTrigger, isFalse);
      expect(resp.cycleId, equals('cycle_athena_01'));

      final history = AthenaChatHistoryResponse.fromJson({
        'cycle_id': 'cycle_athena_01',
        'messages': [
          {'sender': 'user', 'text': 'Hello Athena', 'timestamp': '2026-10-28T09:00:00Z'},
          {'sender': 'curator', 'text': 'Welcome! How are you feeling?', 'timestamp': '2026-10-28T09:00:02Z'},
        ],
      });
      expect(history.cycleId, equals('cycle_athena_01'));
      expect(history.messages.length, equals(2));
      expect(history.messages[0].sender, equals('user'));
      expect(history.messages[1].sender, equals('curator'));
    });

    test('6. Security guarantee: ClientModelReportResponse rejects non-client_facing reports', () {
      final invalidMap = {
        'report_id': 'rep_secret_01',
        'cycle_id': 'cycle_01',
        'model': 'gemini-3.6-flash',
        'report_type': 'anamnesis_m1',
        'privacy_level': 'server_only',
        'version': 1,
        'content': {'summary': 'Private medical hypothesis'},
        'created_at': '2026-10-28T08:00:00Z',
      };

      expect(
        () => ClientModelReportResponse.fromJson(invalidMap),
        throwsA(isA<FormatException>().having(
          (e) => e.message,
          'message',
          contains('Security Violation: Received report with non-client_facing privacy_level'),
        )),
      );
    });
  });

  group('Stage 5.4C: AegisApiClient HTTP Methods (MockClient)', () {
    test('1. submitAnamnesisIntake sends POST /api/v1/client/cascade/intake/anamnesis with Bearer JWT', () async {
      String? capturedPath;
      String? capturedAuth;
      Map<String, dynamic>? capturedBody;

      final mockClient = MockClient((request) async {
        capturedPath = request.url.path;
        capturedAuth = request.headers['Authorization'];
        capturedBody = json.decode(request.body) as Map<String, dynamic>;

        return http.Response(
          json.encode({
            'cycle_id': 'cycle_prov_01',
            'status': 'processed',
            'intake_version': 1,
            'created_at_utc': '2026-10-28T09:20:00Z',
            'privacy_notice': 'Anamnesis processed in strict blind mode.',
          }),
          200,
          headers: {'content-type': 'application/json'},
        );
      });

      final testJwt = _buildTestJwt(patientId: 'patient_alpha', scopes: ['cascade:intake']);
      final apiClient = AegisApiClient(
        baseUrl: 'http://127.0.0.1:8000',
        tokenProvider: () async => testJwt,
        client: mockClient,
      );

      final result = await apiClient.submitAnamnesisIntake(
        AnamnesisIntakeRequest(answers: _sampleAnswers()),
      );

      expect(capturedPath, equals('/api/v1/client/cascade/intake/anamnesis'));
      expect(capturedAuth, equals('Bearer $testJwt'));
      expect(capturedBody?.containsKey('cycle_id'), isFalse, reason: 'Client must never transmit cycle_id to anamnesis intake endpoint');
      expect(capturedBody?['answers']['age'], equals(35));
      expect(result.cycleId, equals('cycle_prov_01'));
      expect(result.status, equals('processed'));
      expect(result.intakeVersion, equals(1));
    });

    test('2. getCascadeStatus sends GET /api/v1/client/cascade/status without client patient override', () async {
      String? capturedPath;
      String? capturedAuth;

      final mockClient = MockClient((request) async {
        capturedPath = request.url.path;
        capturedAuth = request.headers['Authorization'];

        return http.Response(
          json.encode({
            'patient_id': 'patient_alpha',
            'device_id': 'dev_test_01',
            'has_active_cycle': true,
            'active_cycle_id': 'cycle_prov_01',
            'has_preparatory_anamnesis': true,
            'has_preparatory_labs': false,
            'has_preparatory_ecg': false,
            'is_primary_consilium_completed': false,
            'has_unreviewed_lab_changes': false,
          }),
          200,
          headers: {'content-type': 'application/json'},
        );
      });

      final testJwt = _buildTestJwt(patientId: 'patient_alpha', scopes: ['cycle:read']);
      final apiClient = AegisApiClient(
        baseUrl: 'http://127.0.0.1:8000',
        tokenProvider: () async => testJwt,
        client: mockClient,
      );

      final status = await apiClient.getCascadeStatus();

      expect(capturedPath, equals('/api/v1/client/cascade/status'));
      expect(capturedAuth, equals('Bearer $testJwt'));
      expect(status.patientId, equals('patient_alpha'));
      expect(status.hasActiveCycle, isTrue);
      expect(status.activeCycleId, equals('cycle_prov_01'));
    });

    test('3. getLabLedgerState sends GET /api/v1/client/labs/state', () async {
      String? capturedPath;

      final mockClient = MockClient((request) async {
        capturedPath = request.url.path;
        return http.Response(
          json.encode({
            'cycle_id': 'cycle_prov_01',
            'ledger_version': 1,
            'last_evaluated_ledger_version': 1,
            'has_unreviewed_lab_changes': false,
            'evaluation_status': 'EVALUATED_UP_TO_DATE',
            'evaluation_notice': 'All active markers evaluated.',
            'has_critical_flags': false,
            'safety_notice': 'Standard safety protocols apply.',
            'active_markers': {},
            'observations_history': [],
          }),
          200,
          headers: {'content-type': 'application/json'},
        );
      });

      final testJwt = _buildTestJwt(patientId: 'patient_alpha', scopes: ['cycle:read']);
      final apiClient = AegisApiClient(
        baseUrl: 'http://127.0.0.1:8000',
        tokenProvider: () async => testJwt,
        client: mockClient,
      );

      final ledger = await apiClient.getLabLedgerState();
      expect(capturedPath, equals('/api/v1/client/labs/state'));
      expect(ledger.cycleId, equals('cycle_prov_01'));
      expect(ledger.hasUnreviewedLabChanges, isFalse);
    });

    test('4. runPrimaryConsilium with completed result returns PrimaryConsiliumClientResponse', () async {
      String? capturedPath;
      String? capturedIdempotencyHeader;

      final mockClient = MockClient((request) async {
        capturedPath = request.url.path;
        capturedIdempotencyHeader = request.headers['Idempotency-Key'];

        final overall = _sampleClientReport(
          reportId: 'rep_rec_overall',
          reportType: 'user_overall_report',
          title: 'Concluded Consilium',
        );
        final cvs = _sampleClientReport(
          reportId: 'rep_rec_cvs',
          reportType: 'system_user_report_cvs',
          title: 'Cardiovascular Evaluation',
        );

        return http.Response(
          json.encode({
            'cycle_id': 'cycle_prov_01',
            'report_date': '2026-10-28',
            'overall_report': overall.toJson(),
            'system_reports': {'cvs': cvs.toJson()},
            'vitals_thresholds': {
              'min_pessimum_bpm': 46,
              'max_pessimum_bpm': 112,
              'max_spike_delta_bpm': 24,
            },
          }),
          200,
          headers: {
            'content-type': 'application/json',
            'X-Aegis-Idempotent-Replay': 'true',
          },
        );
      });

      final testJwt = _buildTestJwt(patientId: 'patient_alpha', scopes: ['cascade:run']);
      final apiClient = AegisApiClient(
        baseUrl: 'http://127.0.0.1:8000',
        tokenProvider: () async => testJwt,
        client: mockClient,
      );

      final result = await apiClient.runPrimaryConsilium(idempotencyKey: 'key_consilium_run_01');

      expect(capturedPath, equals('/api/v1/client/cascade/run-primary'));
      expect(capturedIdempotencyHeader, equals('key_consilium_run_01'));
      expect(result.cycleId, equals('cycle_prov_01'));
      expect(result.overallReport.content.title, equals('Concluded Consilium'));
      expect(result.vitalsThresholds.minPessimumBpm, equals(46));
    });

    test('5. runPrimaryConsilium with un-fenced execution preserves HTTP 503 and missing guarantees', () async {
      final mockClient = MockClient((request) async {
        return http.Response(
          json.encode({
            'detail': {
              'error': 'Primary Consilium fresh execution via client API is temporarily inactive pending Stage 5.4E distributed lease & fencing protocol.',
              'status': 'BLOCKED_PENDING_DISTRIBUTED_LEASE',
              'cycle_id': 'cycle_prov_01',
              'blocker_code': 'PRIMARY_CONSILIUM_LEASE_GUARD_REQUIRED',
              'missing_guarantees': [
                'atomic_lease_acquisition_with_lease_owner_id',
                'background_heartbeat_lease_renewal',
                'fencing_write_protection_against_stale_owners',
                'persistent_idempotency_table_for_client_retry',
              ],
            },
          }),
          503,
          headers: {'content-type': 'application/json'},
        );
      });

      final testJwt = _buildTestJwt(patientId: 'patient_alpha', scopes: ['cascade:run']);
      final apiClient = AegisApiClient(
        baseUrl: 'http://127.0.0.1:8000',
        tokenProvider: () async => testJwt,
        client: mockClient,
      );

      expect(
        () => apiClient.runPrimaryConsilium(),
        throwsA(isA<AegisServiceUnavailableException>()
            .having((e) => e.statusCode, 'statusCode', equals(503))
            .having((e) => e.isBlockedPendingDistributedLease, 'isBlockedPendingDistributedLease', isTrue)
            .having((e) => e.responseBody.toString(), 'responseBody', contains('PRIMARY_CONSILIUM_LEASE_GUARD_REQUIRED'))),
      );
    });

    test('6. runWeeklyConsilium preserves HTTP 503 blocked status without faking success', () async {
      final mockClient = MockClient((request) async {
        return http.Response(
          json.encode({
            'detail': {
              'error': 'Weekly Consilium execution via client API is disabled pending Stage 5.4F distributed lease & sprint protocol.',
              'status': 'BLOCKED_PENDING_DISTRIBUTED_LEASE',
              'cycle_id': 'cycle_prov_01',
              'blocker_code': 'WEEKLY_CONSILIUM_LEASE_GUARD_REQUIRED',
            },
          }),
          503,
          headers: {'content-type': 'application/json'},
        );
      });

      final testJwt = _buildTestJwt(patientId: 'patient_alpha', scopes: ['cascade:run']);
      final apiClient = AegisApiClient(
        baseUrl: 'http://127.0.0.1:8000',
        tokenProvider: () async => testJwt,
        client: mockClient,
      );

      expect(
        () => apiClient.runWeeklyConsilium(),
        throwsA(isA<AegisServiceUnavailableException>()
            .having((e) => e.statusCode, 'statusCode', equals(503))
            .having((e) => e.isBlockedPendingDistributedLease, 'isBlockedPendingDistributedLease', isTrue)
            .having((e) => e.responseBody.toString(), 'responseBody', contains('WEEKLY_CONSILIUM_LEASE_GUARD_REQUIRED'))),
      );
    });

    test('7. sendAthenaMessage and getAthenaHistory work cleanly with 200 responses', () async {
      final historyList = [
        {'sender': 'user', 'text': 'How is my recovery?', 'timestamp': '2026-10-28T09:00:00Z'},
        {'sender': 'curator', 'text': 'Your recovery is within target parameters.', 'timestamp': '2026-10-28T09:00:02Z'},
      ];

      final mockClient = MockClient((request) async {
        if (request.url.path == '/api/v1/client/chat/athena' && request.method == 'POST') {
          return http.Response(
            json.encode({
              'reply': 'Recovery looks solid. Keep hydration up.',
              'emergency_trigger': false,
              'cycle_id': 'cycle_athena_99',
              'sprint_day': 2,
              'message_id': 'msg_001',
              'created_at': '2026-10-28T09:05:00Z',
            }),
            200,
            headers: {'content-type': 'application/json'},
          );
        } else if (request.url.path == '/api/v1/client/chat/athena/history' && request.method == 'GET') {
          return http.Response(
            json.encode({
              'cycle_id': 'cycle_athena_99',
              'messages': historyList,
            }),
            200,
            headers: {'content-type': 'application/json'},
          );
        }
        return http.Response('Not Found', 404);
      });

      final testJwt = _buildTestJwt(patientId: 'patient_alpha', scopes: ['chat:read', 'chat:write']);
      final apiClient = AegisApiClient(
        baseUrl: 'http://127.0.0.1:8000',
        tokenProvider: () async => testJwt,
        client: mockClient,
      );

      final chatResp = await apiClient.sendAthenaMessage(
        AthenaChatRequest.createTurn(message: 'How is my recovery?', sprintDay: 2),
      );
      expect(chatResp.reply, contains('Recovery looks solid'));
      expect(chatResp.messageId, equals('msg_001'));

      final historyResp = await apiClient.getAthenaHistory();
      expect(historyResp.cycleId, equals('cycle_athena_99'));
      expect(historyResp.messages.length, equals(2));
      expect(historyResp.messages[0].text, equals('How is my recovery?'));
    });
  });

  group('Stage 5.4C: Authentication, 401 Refresh, and 403 Scopes', () {
    test('8. Automatic 401 refresh retries request once with rotated token', () async {
      int requestCount = 0;
      final tokenV1 = _buildTestJwt(patientId: 'patient_alpha', deviceId: 'dev_rot', jti: 'jwt_v1');
      final tokenV2 = _buildTestJwt(patientId: 'patient_alpha', deviceId: 'dev_rot', jti: 'jwt_v2');
      String currentToken = tokenV1;

      final mockClient = MockClient((request) async {
        requestCount++;
        final authHeader = request.headers['Authorization'];

        if (authHeader == 'Bearer $tokenV1') {
          // Reject expired token
          return http.Response(
            json.encode({'detail': 'Token expired or invalid'}),
            401,
            headers: {'content-type': 'application/json'},
          );
        } else if (authHeader == 'Bearer $tokenV2') {
          // Accept refreshed token
          return http.Response(
            json.encode({
              'patient_id': 'patient_alpha',
              'device_id': 'dev_rot',
              'has_active_cycle': true,
              'active_cycle_id': 'cycle_refreshed_01',
              'has_preparatory_anamnesis': true,
              'has_preparatory_labs': false,
              'has_preparatory_ecg': false,
              'is_primary_consilium_completed': false,
              'has_unreviewed_lab_changes': false,
            }),
            200,
            headers: {'content-type': 'application/json'},
          );
        }
        return http.Response('Forbidden', 403);
      });

      // Mock session manager that simulates token rotation on refreshSession
      final mockSessionManager = _MockAuthSessionManager(
        onRefresh: () async {
          currentToken = tokenV2;
          return tokenV2;
        },
      );

      final apiClient = AegisApiClient(
        baseUrl: 'http://127.0.0.1:8000',
        tokenProvider: () async => currentToken,
        client: mockClient,
      );
      apiClient.attachSessionManager(mockSessionManager);

      final status = await apiClient.getCascadeStatus();

      expect(requestCount, equals(2)); // Strictly 1 retry
      expect(status.hasActiveCycle, isTrue);
      expect(status.activeCycleId, equals('cycle_refreshed_01'));
    });

    test('9. Repeated 401 failure terminates after single retry and throws AegisAuthException', () async {
      int requestCount = 0;
      final tokenDead = _buildTestJwt(patientId: 'patient_alpha', deviceId: 'dev_dead');

      final mockClient = MockClient((request) async {
        requestCount++;
        return http.Response(
          json.encode({'detail': 'Token has been revoked or invalidated'}),
          401,
          headers: {'content-type': 'application/json'},
        );
      });

      final mockSessionManager = _MockAuthSessionManager(
        onRefresh: () async => _buildTestJwt(patientId: 'patient_alpha', deviceId: 'dev_dead_2'),
      );

      final apiClient = AegisApiClient(
        baseUrl: 'http://127.0.0.1:8000',
        tokenProvider: () async => tokenDead,
        client: mockClient,
      );
      apiClient.attachSessionManager(mockSessionManager);

      await expectLater(
        apiClient.getCascadeStatus(),
        throwsA(isA<AegisAuthException>().having((e) => e.statusCode, 'statusCode', equals(401))),
      );
      expect(requestCount, equals(2)); // Refreshed once, then stopped cleanly
    });

    test('10. 403 Insufficient Scope is distinguished from 401 and throws AegisInsufficientScopeException', () async {
      final mockClient = MockClient((request) async {
        return http.Response(
          json.encode({'detail': 'Insufficient token scope for this operation.'}),
          403,
          headers: {'content-type': 'application/json'},
        );
      });

      final testJwt = _buildTestJwt(patientId: 'patient_alpha', scopes: ['cycle:read']); // Missing chat:write
      final apiClient = AegisApiClient(
        baseUrl: 'http://127.0.0.1:8000',
        tokenProvider: () async => testJwt,
        client: mockClient,
      );

      try {
        await apiClient.sendAthenaMessage(AthenaChatRequest.createTurn(message: 'Hello'));
        fail('Expected AegisInsufficientScopeException');
      } on AegisApiException catch (e) {
        expect(e, isA<AegisForbiddenException>());
        expect(e, isA<AegisInsufficientScopeException>());
        expect(e.statusCode, equals(403));
        expect(e.message, contains('Insufficient token scope'));
      }
    });

    test('11. 409 Conflict throws AegisConflictException cleanly', () async {
      final mockClient = MockClient((request) async {
        return http.Response(
          json.encode({'detail': 'Medical cycle is currently undergoing evaluation'}),
          409,
          headers: {'content-type': 'application/json'},
        );
      });

      final testJwt = _buildTestJwt(patientId: 'patient_alpha');
      final apiClient = AegisApiClient(
        baseUrl: 'http://127.0.0.1:8000',
        tokenProvider: () async => testJwt,
        client: mockClient,
      );

      await expectLater(
        apiClient.submitAnamnesisIntake(AnamnesisIntakeRequest(answers: _sampleAnswers())),
        throwsA(isA<AegisConflictException>().having((e) => e.statusCode, 'statusCode', equals(409))),
      );
    });
  });

  group('Stage 5.4C: Idempotency & Patient Isolation', () {
    test('12. Athena message retry preserves the exact same idempotency_key', () async {
      final capturedKeys = <String>[];
      final tokenV1 = _buildTestJwt(patientId: 'patient_alpha', jti: 'jwt_idem_1');
      final tokenV2 = _buildTestJwt(patientId: 'patient_alpha', jti: 'jwt_idem_2');
      String currentToken = tokenV1;
      const callerKey = 'idempotency_fixed_uuid_12345';

      final mockClient = MockClient((request) async {
        final body = json.decode(request.body) as Map<String, dynamic>;
        capturedKeys.add(body['idempotency_key'] as String);

        if (capturedKeys.length == 1) {
          // First attempt fails with 401
          return http.Response(json.encode({'detail': 'Token expired'}), 401);
        }
        // Second attempt succeeds
        return http.Response(
          json.encode({
            'reply': 'Acknowledged with exact key.',
            'emergency_trigger': false,
            'cycle_id': 'cycle_idem',
            'message_id': 'msg_idem_1',
            'created_at': '2026-10-28T09:30:00Z',
          }),
          200,
        );
      });

      final mockSessionManager = _MockAuthSessionManager(
        onRefresh: () async {
          currentToken = tokenV2;
          return tokenV2;
        },
      );

      final apiClient = AegisApiClient(
        baseUrl: 'http://127.0.0.1:8000',
        tokenProvider: () async => currentToken,
        client: mockClient,
      );
      apiClient.attachSessionManager(mockSessionManager);

      final response = await apiClient.sendAthenaMessage(
        const AthenaChatRequest(
          message: 'Idempotency verification message',
          idempotencyKey: callerKey,
        ),
      );

      expect(response.messageId, equals('msg_idem_1'));
      expect(capturedKeys.length, equals(2));
      expect(capturedKeys[0], equals(callerKey));
      expect(capturedKeys[1], equals(callerKey));
    });

    test('12b. Athena network timeout retry preserves caller request and reuses exact same idempotency_key', () async {
      final capturedKeys = <String>[];
      int attempt = 0;
      final testJwt = _buildTestJwt(patientId: 'patient_alpha');

      final mockClient = MockClient((request) async {
        attempt++;
        final body = json.decode(request.body) as Map<String, dynamic>;
        capturedKeys.add(body['idempotency_key'] as String);

        if (attempt == 1) {
          // Simulate network drop / timeout
          throw http.ClientException('Network drop simulated');
        }
        return http.Response(
          json.encode({
            'reply': 'Recovered after network retry.',
            'emergency_trigger': false,
            'cycle_id': 'cycle_net_01',
            'message_id': 'msg_net_1',
            'created_at': '2026-10-28T09:35:00Z',
          }),
          200,
        );
      });

      final apiClient = AegisApiClient(
        baseUrl: 'http://127.0.0.1:8000',
        tokenProvider: () async => testJwt,
        client: mockClient,
      );

      final stableTurnRequest = AthenaChatRequest.createTurn(
        message: 'Are my recovery markers normal?',
      );

      // Attempt 1: Fails with network exception
      try {
        await apiClient.sendAthenaMessage(stableTurnRequest);
        fail('Expected network exception on attempt 1');
      } on AegisNetworkException catch (_) {
        // Expected
      }

      // Attempt 2 (Caller retry using the same request instance)
      final resp = await apiClient.sendAthenaMessage(stableTurnRequest);

      expect(resp.messageId, equals('msg_net_1'));
      expect(capturedKeys.length, equals(2));
      expect(capturedKeys[0], equals(stableTurnRequest.idempotencyKey));
      expect(capturedKeys[1], equals(stableTurnRequest.idempotencyKey));
      expect(AthenaChatRequest.isValidUuidV4(stableTurnRequest.idempotencyKey), isTrue);
    });

    test('12c. New conversational turns generate distinct, collision-resistant UUID v4 keys', () {
      final turn1 = AthenaChatRequest.createTurn(message: 'Turn 1 message');
      final turn2 = AthenaChatRequest.createTurn(message: 'Turn 2 message');

      expect(turn1.idempotencyKey, isNotEmpty);
      expect(turn2.idempotencyKey, isNotEmpty);
      expect(turn1.idempotencyKey, isNot(equals(turn2.idempotencyKey)));
      expect(turn1.idempotencyKey.length, equals(36));
      expect(turn2.idempotencyKey.length, equals(36));

      expect(AthenaChatRequest.isValidUuidV4(turn1.idempotencyKey), isTrue);
      expect(AthenaChatRequest.isValidUuidV4(turn2.idempotencyKey), isTrue);

      // Verify collision resistance over 1,000 rapid sequential generations
      final keys = <String>{};
      for (int i = 0; i < 1000; i++) {
        final key = AthenaChatRequest.generateUuidV4();
        expect(AthenaChatRequest.isValidUuidV4(key), isTrue);
        expect(keys.contains(key), isFalse, reason: 'Duplicate UUID v4 detected');
        keys.add(key);
      }
      expect(keys.length, equals(1000));
    });

    test('12d. sendAthenaMessage eagerly rejects empty or whitespace idempotencyKey', () async {
      final apiClient = AegisApiClient(
        baseUrl: 'http://127.0.0.1:8000',
        tokenProvider: () async => _buildTestJwt(patientId: 'patient_alpha'),
      );

      expect(
        () => AthenaChatRequest(message: 'Hello', idempotencyKey: ''),
        throwsA(isA<AssertionError>()),
      );

      expect(
        () => apiClient.sendAthenaMessage(
          const AthenaChatRequest(message: 'Hello', idempotencyKey: '   '),
        ),
        throwsA(isA<ArgumentError>()),
      );
    });

    test('12e. AthenaChatRequest.fromJson strictly requires non-empty idempotency_key and throws FormatException', () {
      // Missing idempotency_key
      expect(
        () => AthenaChatRequest.fromJson({'message': 'Hello', 'event_type': 'user_message'}),
        throwsA(isA<FormatException>().having(
          (e) => e.message,
          'message',
          contains('Missing or invalid required "idempotency_key"'),
        )),
      );

      // Null idempotency_key
      expect(
        () => AthenaChatRequest.fromJson({'message': 'Hello', 'idempotency_key': null}),
        throwsA(isA<FormatException>()),
      );

      // Whitespace idempotency_key
      expect(
        () => AthenaChatRequest.fromJson({'message': 'Hello', 'idempotency_key': '   '}),
        throwsA(isA<FormatException>()),
      );

      // Valid non-empty idempotency_key deserializes correctly
      final valid = AthenaChatRequest.fromJson({
        'message': 'Hello',
        'idempotency_key': 'e4b3e8c0-82a1-4fc3-a1b2-c3d4e5f60718',
      });
      expect(valid.idempotencyKey, equals('e4b3e8c0-82a1-4fc3-a1b2-c3d4e5f60718'));
    });

    test('12f. Generated UUID v4 contains zero patientId, zero medical data, and zero PII', () {
      const patientId = 'patient_secret_pii_99';
      final turn = AthenaChatRequest.createTurn(message: 'Confidential message');

      expect(turn.idempotencyKey.contains(patientId), isFalse);
      expect(turn.idempotencyKey.contains('patient'), isFalse);
      expect(turn.idempotencyKey.contains('pii'), isFalse);
      expect(turn.idempotencyKey.length, equals(36));
      expect(AthenaChatRequest.isValidUuidV4(turn.idempotencyKey), isTrue);
    });

    test('13. Switching session manager isolates patient state and uses new patient token', () async {
      String? lastUsedToken;
      final mockClient = MockClient((request) async {
        lastUsedToken = request.headers['Authorization'];
        return http.Response(
          json.encode({
            'patient_id': 'patient_switched',
            'device_id': 'dev_switched',
            'has_active_cycle': false,
          }),
          200,
        );
      });

      final tokenAlpha = _buildTestJwt(patientId: 'patient_alpha');
      final tokenBeta = _buildTestJwt(patientId: 'patient_beta');

      final apiClient = AegisApiClient(
        baseUrl: 'http://127.0.0.1:8000',
        tokenProvider: () async => tokenAlpha,
        client: mockClient,
      );

      await apiClient.getCascadeStatus();
      expect(lastUsedToken, equals('Bearer $tokenAlpha'));

      // Switch to a new session manager for patient beta
      final managerBeta = _MockAuthSessionManager(
        patientId: 'patient_beta',
        onRefresh: () async => tokenBeta,
      );
      apiClient.bindTokenProvider(() async => tokenBeta);
      apiClient.attachSessionManager(managerBeta);

      await apiClient.getCascadeStatus();
      expect(lastUsedToken, equals('Bearer $tokenBeta'));
    });

    test('14. Client models contain zero hidden server-side AI reasoning fields', () {
      final answers = _sampleAnswers();
      final serialized = answers.toJson();

      // Verify absence of preparatory conclusions / hypotheses
      expect(serialized.containsKey('raw_diagnostic_hypothesis'), isFalse);
      expect(serialized.containsKey('data_gap_analysis'), isFalse);
      expect(serialized.containsKey('error_probability'), isFalse);
      expect(serialized.containsKey('training_sandbox'), isFalse);
      expect(serialized.containsKey('system_logic'), isFalse);
      expect(serialized.containsKey('internal_comment'), isFalse);

      final athenaResp = AthenaChatResponse(
        reply: 'User reply only',
        cycleId: 'cycle_test',
        messageId: 'msg_01',
        createdAt: '2026-10-28T09:00:00Z',
      );
      final athenaJson = athenaResp.toJson();
      expect(athenaJson.containsKey('prompt'), isFalse);
      expect(athenaJson.containsKey('clinical_score'), isFalse);
      expect(athenaJson.containsKey('hidden_reports'), isFalse);
    });
  });
}

/// Lightweight mock auth session manager for client tests
class _MockAuthSessionManager {
  final String patientId;
  final Future<String> Function()? onRefresh;

  _MockAuthSessionManager({
    this.patientId = 'patient_alpha',
    this.onRefresh,
  });

  Future<String> refreshSession() async {
    if (onRefresh != null) {
      return await onRefresh!();
    }
    return _buildTestJwt(patientId: patientId);
  }
}
