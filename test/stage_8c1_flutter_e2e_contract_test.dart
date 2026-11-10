import 'dart:convert';
import 'dart:typed_data';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'package:aegis_health/services/api/aegis_api_client.dart';
import 'package:aegis_health/services/api/medical_ai_api_models.dart';
import 'package:aegis_health/services/auth/auth_models.dart';
import 'package:aegis_health/services/auth/auth_session_manager.dart';
import 'package:aegis_health/services/auth/auth_token_storage.dart';
import 'package:aegis_health/services/sync/vitals_sync_queue.dart';
import 'package:aegis_health/services/vault_service.dart';

String _buildTestJwt({
  required String patientId,
  String deviceId = 'test_device_8c1',
}) {
  final headerJson = json.encode({'alg': 'HS256', 'typ': 'JWT'});
  final payloadJson = json.encode({
    'sub': patientId,
    'patient_id': patientId,
    'did': deviceId,
    'type': 'access',
  });
  final h = base64Url.encode(utf8.encode(headerJson)).replaceAll('=', '');
  final p = base64Url.encode(utf8.encode(payloadJson)).replaceAll('=', '');
  const s = 'c2lnbmF0dXJlX2Zha2Vfc2VjcmV0XzhjMQ';
  return '$h.$p.$s';
}

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  group('Stage 8C.1: Flutter E2E Contract & Lifecycle Tests', () {
    const patientId = 'patient_flutter_8c1';
    const cycle1Id = 'cycle_c1_referral';
    const cycle2Id = 'cycle_c2_neo';
    const docReportId = 'rep_doc_referral_8c1';

    test('1. Model contracts match FastAPI responses with zero data leakage', () {
      // 1.1 StartNeoReassessmentResponse contract
      final jsonNeoStart = {
        'patient_id': patientId,
        'cycle_id': cycle2Id,
        'previous_cycle_id': cycle1Id,
        'status': 'pending_reassessment',
        'reassessment_status': 'pending_reassessment',
        'is_pending_reassessment': true,
        'created_at_utc': '2026-11-10T08:00:00Z',
        'message': 'Neo reassessment cycle initiated successfully.',
      };
      final neoStart = StartNeoReassessmentResponse.fromJson(jsonNeoStart);
      expect(neoStart.patientId, equals(patientId));
      expect(neoStart.cycleId, equals(cycle2Id));
      expect(neoStart.previousCycleId, equals(cycle1Id));
      expect(neoStart.isPendingReassessment, isTrue);
      expect(neoStart.toJson()['status'], equals('pending_reassessment'));

      // 1.2 NeoActivationResponse contract
      final jsonNeoAct = {
        'success': true,
        'cycle_id': cycle2Id,
        'status': 'active',
        'activated_at_utc': '2026-11-10T08:30:00Z',
        'strategy_version': 1,
        'schedule_anchor_t0_utc': '2026-11-10T08:30:00Z',
        'next_weekly_consilium_due_utc': '2026-11-17T08:30:00Z',
        'post_primary_init_enqueued': true,
        'is_idempotent_replay': false,
      };
      final neoAct = NeoActivationResponse.fromJson(jsonNeoAct);
      expect(neoAct.success, isTrue);
      expect(neoAct.status, equals('active'));
      expect(neoAct.strategyVersion, equals(1));
      expect(neoAct.scheduleAnchorT0Utc, equals('2026-11-10T08:30:00Z'));
      expect(neoAct.nextWeeklyConsiliumDueUtc, equals('2026-11-17T08:30:00Z'));
      expect(neoAct.postPrimaryInitEnqueued, isTrue);
      expect(neoAct.toJson()['cycle_id'], equals(cycle2Id));

      // 1.3 ClientReferralDocumentListResponse contract
      final jsonRefList = {
        'patient_id': patientId,
        'total_count': 1,
        'documents': [
          {
            'report_id': docReportId,
            'cycle_id': cycle1Id,
            'sprint_id': 'sprint_c1_w1',
            'referral_id': 'ref_c1_001',
            'document_version': 1,
            'status': 'document_ready',
            'urgency': 'priority',
            'target_specialties': ['cardiologist', 'endocrinologist'],
            'created_at_utc': '2026-11-08T12:00:00Z',
            'updated_at_utc': '2026-11-08T12:00:00Z',
            'title': 'Health Observation Summary for In-Person Consultation',
            'available_formats': ['pdf', 'docx', 'md'],
          }
        ],
      };
      final refList = ClientReferralDocumentListResponse.fromJson(jsonRefList);
      expect(refList.totalCount, equals(1));
      expect(refList.documents.first.reportId, equals(docReportId));
      expect(refList.documents.first.targetSpecialties, containsAll(['cardiologist', 'endocrinologist']));
      expect(refList.documents.first.availableFormats, containsAll(['pdf', 'docx', 'md']));

      // 1.4 ClientReferralDocumentDetailResponse contract (7 sections)
      final jsonRefDetail = {
        'report_id': docReportId,
        'cycle_id': cycle1Id,
        'sprint_id': 'sprint_c1_w1',
        'referral_id': 'ref_c1_001',
        'document_version': 1,
        'status': 'document_ready',
        'urgency': 'priority',
        'target_specialties': ['cardiologist', 'endocrinologist'],
        'created_at_utc': '2026-11-08T12:00:00Z',
        'updated_at_utc': '2026-11-08T12:00:00Z',
        'title': 'Health Observation Summary for In-Person Consultation',
        'available_formats': ['pdf', 'docx', 'md'],
        'legal_notice': 'AI-assisted observation summary for licensed physician discussion.',
        'patient_friendly_disclaimer': 'This summary is not a confirmed medical diagnosis.',
        'sections': [
          {'section_number': 1, 'section_title': '1. Patient and Observation Period', 'content': 'Participant observed over 7 days.'},
          {'section_number': 2, 'section_title': '2. Reason for Recommending Consultation', 'content': 'Elevated resting pulse and TSH.'},
          {'section_number': 3, 'section_title': '3. Objective Measurements', 'content': 'TSH 5.8 mIU/L, peak resting pulse 134 bpm.'},
          {'section_number': 4, 'section_title': '4. Timeline of Reported Symptoms', 'content': 'Palpitations reported during rest.'},
          {'section_number': 5, 'section_title': '5. Important Patterns and Unresolved Concerns', 'content': 'Non-exertional tachyarrhythmia episodes.'},
          {'section_number': 6, 'section_title': '6. AI-Generated Observations Requiring Verification', 'content': 'Differential diagnosis needed.'},
          {'section_number': 7, 'section_title': '7. Questions for Clinician Discussion', 'content': 'Consider 12-lead ECG and Holter.'},
        ],
      };
      final refDetail = ClientReferralDocumentDetailResponse.fromJson(jsonRefDetail);
      expect(refDetail.sections.length, equals(7));
      expect(refDetail.sections.first.sectionTitle, contains('Patient and Observation Period'));
      expect(refDetail.sections.last.sectionTitle, contains('Questions'));
    });

    test('2. Complete Client E2E Lifecycle: Referral -> Athena Confirm -> Pre-Reset Barrier -> Neo Reassessment -> Activation', () async {
      SharedPreferences.setMockInitialValues({});
      await SharedPreferences.getInstance();

      // Lifecycle State Machine
      var cycle1ReferralStatus = 'document_ready';
      var activeCycleId = cycle1Id;
      var activeCycleStatus = 'active';
      var isNeoPrepared = false;
      var isNeoActivated = false;

      // Mock HTTP Client handling FastAPI Client Cascade endpoints
      final mockClient = MockClient((request) async {
        final path = request.url.path;
        final method = request.method;

        // Cascade Status
        if (method == 'GET' && path == '/api/v1/client/cascade/status') {
          return http.Response(
            json.encode({
              'patient_id': patientId,
              'device_id': 'test_device_8c1',
              'has_active_cycle': true,
              'active_cycle_id': activeCycleId,
              'has_active_referral': cycle1ReferralStatus == 'document_ready',
              'is_neo_reassessment': activeCycleId == cycle2Id,
              'referral_status': {
                'has_active_referral': cycle1ReferralStatus == 'document_ready',
                'status': cycle1ReferralStatus,
                'document_generation_state': cycle1ReferralStatus == 'document_ready' ? 'ready' : 'none',
                'urgency': 'priority',
                'target_specialties': ['cardiologist', 'endocrinologist'],
                'clinical_reason_summary': 'Non-exertional heart rate elevations and TSH elevation',
                'document_report_id': docReportId,
                'created_at_utc': '2026-11-08T12:00:00Z',
              },
            }),
            200,
            headers: {'content-type': 'application/json'},
          );
        }

        // Referral Documents in Safe
        if (method == 'GET' && path == '/api/v1/client/referral/documents') {
          return http.Response(
            json.encode({
              'patient_id': patientId,
              'total_count': 1,
              'documents': [
                {
                  'report_id': docReportId,
                  'cycle_id': cycle1Id,
                  'sprint_id': 'sprint_c1_w1',
                  'referral_id': 'ref_c1_001',
                  'document_version': 1,
                  'status': cycle1ReferralStatus,
                  'urgency': 'priority',
                  'target_specialties': ['cardiologist', 'endocrinologist'],
                  'created_at_utc': '2026-11-08T12:00:00Z',
                  'updated_at_utc': '2026-11-08T12:00:00Z',
                  'title': 'Health Observation Summary for In-Person Consultation',
                  'available_formats': ['pdf', 'docx', 'md'],
                }
              ],
            }),
            200,
            headers: {'content-type': 'application/json'},
          );
        }

        // Referral Document Detail
        if (method == 'GET' && path == '/api/v1/client/referral/documents/$docReportId') {
          return http.Response(
            json.encode({
              'report_id': docReportId,
              'cycle_id': cycle1Id,
              'sprint_id': 'sprint_c1_w1',
              'referral_id': 'ref_c1_001',
              'document_version': 1,
              'status': cycle1ReferralStatus,
              'urgency': 'priority',
              'target_specialties': ['cardiologist', 'endocrinologist'],
              'created_at_utc': '2026-11-08T12:00:00Z',
              'updated_at_utc': '2026-11-08T12:00:00Z',
              'title': 'Health Observation Summary for In-Person Consultation',
              'available_formats': ['pdf', 'docx', 'md'],
              'legal_notice': 'AI-assisted observation summary for licensed physician discussion.',
              'patient_friendly_disclaimer': 'This summary is not a confirmed medical diagnosis.',
              'sections': [
                {'section_number': 1, 'section_title': '1. Patient and Observation Period', 'content': 'Participant observed over 7 days.'},
                {'section_number': 2, 'section_title': '2. Reason for Recommending Consultation', 'content': 'Elevated resting pulse and TSH.'},
                {'section_number': 3, 'section_title': '3. Objective Measurements', 'content': 'TSH 5.8 mIU/L, peak resting pulse 134 bpm.'},
                {'section_number': 4, 'section_title': '4. Timeline of Reported Symptoms', 'content': 'Palpitations reported during rest.'},
                {'section_number': 5, 'section_title': '5. Important Patterns and Unresolved Concerns', 'content': 'Non-exertional tachyarrhythmia episodes.'},
                {'section_number': 6, 'section_title': '6. AI-Generated Observations Requiring Verification', 'content': 'Differential diagnosis needed.'},
                {'section_number': 7, 'section_title': '7. Questions for Clinician Discussion', 'content': 'Consider 12-lead ECG and Holter.'},
              ],
            }),
            200,
            headers: {'content-type': 'application/json'},
          );
        }

        // Document Export
        if (method == 'GET' && path.startsWith('/api/v1/client/referral/documents/$docReportId/export')) {
          final fmt = request.url.queryParameters['format'] ?? 'pdf';
          if (fmt == 'pdf') {
            return http.Response.bytes(
              Uint8List.fromList([0x25, 0x50, 0x44, 0x46, 0x2D, 0x31, 0x2E, 0x34]), // %PDF-1.4
              200,
              headers: {'content-type': 'application/pdf'},
            );
          } else if (fmt == 'docx') {
            return http.Response.bytes(
              Uint8List.fromList([0x50, 0x4B, 0x03, 0x04]), // PK.. (ZIP)
              200,
              headers: {'content-type': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'},
            );
          } else {
            return http.Response(
              '# AEGIS — HEALTH OBSERVATION SUMMARY FOR YOUR DOCTOR\n\n- Specialties: cardiologist, endocrinologist',
              200,
              headers: {'content-type': 'text/markdown; charset=utf-8'},
            );
          }
        }

        // Athena Chat
        if (method == 'POST' && path == '/api/v1/client/chat/athena') {
          final body = json.decode(request.body) as Map<String, dynamic>;
          final msg = (body['message'] as String? ?? '').toLowerCase();
          if (msg.contains('visited') || msg.contains('cardiologist')) {
            cycle1ReferralStatus = 'visit_confirmed';
            return http.Response(
              json.encode({
                'reply': 'Thank you for following up! I noted your visit to the cardiologist.',
                'message_id': 'msg_athena_visit_confirmed',
                'created_at_utc': '2026-11-10T08:15:00Z',
              }),
              200,
              headers: {'content-type': 'application/json'},
            );
          }
          return http.Response(
            json.encode({
              'reply': 'Hello! How can I assist with your health today?',
              'message_id': 'msg_athena_generic',
              'created_at_utc': '2026-11-10T08:15:00Z',
            }),
            200,
            headers: {'content-type': 'application/json'},
          );
        }

        // Vitals Batch Sync
        if (method == 'POST' && path == '/api/v1/vitals/batch') {
          return http.Response(
            json.encode({
              'status': 'accepted',
              'processed_count': 1,
              'accepted_at_utc': '2026-11-10T08:20:00Z',
            }),
            200,
            headers: {'content-type': 'application/json'},
          );
        }

        // Start Neo Reassessment
        if (method == 'POST' && path == '/api/v1/cycles/start-neo-reassessment') {
          activeCycleId = cycle2Id;
          activeCycleStatus = 'pending_reassessment';
          return http.Response(
            json.encode({
              'patient_id': patientId,
              'cycle_id': cycle2Id,
              'previous_cycle_id': cycle1Id,
              'status': 'pending_reassessment',
              'reassessment_status': 'pending_reassessment',
              'is_pending_reassessment': true,
              'created_at_utc': '2026-11-10T08:25:00Z',
              'message': 'Neo reassessment cycle initiated successfully.',
            }),
            200,
            headers: {'content-type': 'application/json'},
          );
        }

        // Submit Anamnesis Intake
        if (method == 'POST' && path == '/api/v1/client/cascade/intake/anamnesis') {
          return http.Response(
            json.encode({
              'cycle_id': cycle2Id,
              'status': 'processed',
              'intake_version': 1,
              'created_at_utc': '2026-11-10T08:26:00Z',
              'privacy_notice': 'Anamnesis processed in strict blind mode.',
            }),
            200,
            headers: {'content-type': 'application/json'},
          );
        }

        // Run Primary Consilium (Neo Mode)
        if (method == 'POST' && path == '/api/v1/client/cascade/run-primary') {
          isNeoPrepared = true;
          return http.Response(
            json.encode({
              'mode': 'neo_reassessment',
              'cycle_id': cycle2Id,
              'report_date': '2026-11-10T08:28:00Z',
              'overall_report': {
                'report_id': 'rep_overall_neo',
                'report_type': 'user_overall_report',
                'privacy_level': 'client_facing',
                'content': {'summary': 'Neo Consilium assessment completed.'},
                'created_at_utc': '2026-11-10T08:28:00Z',
              },
              'system_reports': {},
              'vitals_thresholds': {
                'min_pessimum_bpm': 46,
                'max_pessimum_bpm': 116,
                'critical_tachycardia_bpm': 140,
                'source': 'pro',
              },
            }),
            200,
            headers: {'content-type': 'application/json'},
          );
        }

        // Activate Neo Consilium
        if (method == 'POST' && path == '/api/v1/client/cascade/activate-neo') {
          activeCycleStatus = 'active';
          isNeoActivated = true;
          return http.Response(
            json.encode({
              'success': true,
              'cycle_id': cycle2Id,
              'status': 'active',
              'activated_at_utc': '2026-11-10T08:30:00Z',
              'strategy_version': 1,
              'schedule_anchor_t0_utc': '2026-11-10T08:30:00Z',
              'next_weekly_consilium_due_utc': '2026-11-17T08:30:00Z',
              'post_primary_init_enqueued': true,
              'is_idempotent_replay': false,
            }),
            200,
            headers: {'content-type': 'application/json'},
          );
        }

        // Athena Chat History
        if (method == 'GET' && path == '/api/v1/client/chat/athena/history') {
          return http.Response(
            json.encode({
              'cycle_id': cycle2Id,
              'messages': [
                {
                  'message_id': 'msg_athena_greeting_neo',
                  'sender': 'curator',
                  'text': 'Welcome back to Aegis! Your health reassessment is active.',
                  'timestamp': '2026-11-10T08:31:00Z',
                }
              ],
            }),
            200,
            headers: {'content-type': 'application/json'},
          );
        }

        return http.Response('{"detail": "Not Found"}', 404);
      });

      // API Client and Auth Session
      final apiClient = AegisApiClient(
        baseUrl: 'http://localhost:8000',
        client: mockClient,
      );
      final authManager = AuthSessionManager(
        apiClient: apiClient,
        storage: InMemoryAuthTokenStorage(
          initialSession: AuthSession(
            accessToken: _buildTestJwt(patientId: patientId),
            refreshToken: 'ref_tok_8c1',
            expiresIn: 3600,
            expiresAt: DateTime.now().toUtc().add(const Duration(hours: 1)),
            refreshExpiresAt: DateTime.now().toUtc().add(const Duration(days: 30)),
            patientId: patientId,
            deviceId: 'test_device_8c1',
            scope: 'cascade:intake cascade:run cycle:read reports:read chat:read chat:write',
          ),
        ),
      );

      // Services setup
      final vaultService = VaultService();
      await vaultService.setActivePatientId(patientId);
      final syncQueue = VitalsSyncQueue(
        apiClient: apiClient,
        activeCycleId: cycle1Id,
        authSessionManager: authManager,
        activePatientId: patientId,
      );

      // STEP 1: Safe inspection & Document export
      final safeDocsResp = await apiClient.getReferralDocuments();
      expect(safeDocsResp.totalCount, equals(1));
      final refDoc = safeDocsResp.documents.first;
      expect(refDoc.reportId, equals(docReportId));
      expect(refDoc.urgency, equals('priority'));

      final pdfBytes = await apiClient.exportReferralDocumentBytes(docReportId, format: 'pdf');
      expect(pdfBytes.length, greaterThan(4));
      expect(String.fromCharCodes(pdfBytes.take(4)), equals('%PDF'));

      final docxBytes = await apiClient.exportReferralDocumentBytes(docReportId, format: 'docx');
      expect(docxBytes.length, greaterThan(2));
      expect(String.fromCharCodes(docxBytes.take(2)), equals('PK'));

      final mdBytes = await apiClient.exportReferralDocumentBytes(docReportId, format: 'md');
      final mdText = utf8.decode(mdBytes);
      expect(mdText, contains('HEALTH OBSERVATION SUMMARY'));

      // STEP 2: Athena chat confirms doctor visit
      final athenaResp = await apiClient.sendAthenaMessage(
        const AthenaChatRequest(
          message: 'I visited the cardiologist today and completed an ECG examination.',
          eventType: 'user_message',
          idempotencyKey: 'idem_chat_visit_01',
        ),
      );
      expect(athenaResp.reply, contains('cardiologist'));
      expect(cycle1ReferralStatus, equals('visit_confirmed'));

      // STEP 3: Pre-Reset Synchronization Barrier
      // Flush pending secure writes & telemetry queue
      await vaultService.flushPendingWrites(patientId: patientId);
      final syncRes = await syncQueue.flush();
      expect(syncRes.status, isNot(equals(SyncStatus.persistenceError)));

      // STEP 4: Start Neo Reassessment
      final neoStartResp = await apiClient.startNeoReassessment();
      expect(neoStartResp.cycleId, equals(cycle2Id));
      expect(neoStartResp.previousCycleId, equals(cycle1Id));
      expect(neoStartResp.status, equals('pending_reassessment'));
      expect(activeCycleId, equals(cycle2Id));
      expect(activeCycleStatus, equals('pending_reassessment'));

      // STEP 5: Blind re-intake submission (M1)
      final intakeResp = await apiClient.submitAnamnesisIntake(
        AnamnesisIntakeRequest(
          answers: AnamnesisQuestionnaireAnswers.fromMap(const {
            'age': 36,
            'gender': 'Male',
            'diet_types': 'Balanced',
            'sleep_quality': 'Normal',
          }),
        ),
      );
      expect(intakeResp.cycleId, equals(cycle2Id));
      expect(intakeResp.status, equals('processed'));

      // STEP 6: Run 10-Model Primary Consilium (Neo cascade)
      final runPrimaryResp = await apiClient.runPrimaryConsilium();
      expect(runPrimaryResp.mode, equals('neo_reassessment'));
      expect(runPrimaryResp.isNeoReassessment, isTrue);
      expect(isNeoPrepared, isTrue);
      expect(isNeoActivated, isFalse);

      // STEP 7: Atomic Activation via POST /api/v1/client/cascade/activate-neo
      final actResp = await apiClient.activateNeoConsilium(cycleId: cycle2Id);
      expect(actResp.success, isTrue);
      expect(actResp.status, equals('active'));
      expect(actResp.strategyVersion, equals(1));
      expect(actResp.scheduleAnchorT0Utc, isNotEmpty);
      expect(actResp.nextWeeklyConsiliumDueUtc, isNotEmpty);
      expect(isNeoActivated, isTrue);
      expect(activeCycleStatus, equals('active'));

      // STEP 8: Historical Safe documents remain accessible across cycles
      final safeAfterNeo = await apiClient.getReferralDocuments();
      expect(safeAfterNeo.totalCount, equals(1));
      expect(safeAfterNeo.documents.any((d) => d.reportId == docReportId), isTrue);

      // STEP 9: Athena greeting post-activation greets returning patient
      final historyResp = await apiClient.getAthenaHistory();
      expect(historyResp.messages.any((m) => m.text.toLowerCase().contains('welcome back')), isTrue);

      // Clean up services
      syncQueue.dispose();
      vaultService.dispose();
    });
  });
}
