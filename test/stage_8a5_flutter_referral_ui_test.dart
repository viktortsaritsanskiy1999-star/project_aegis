import 'dart:convert';
import 'dart:io';
import 'dart:typed_data';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';

import 'package:provider/provider.dart';

import 'package:aegis_health/main.dart';
import 'package:aegis_health/screens/doctor_document_view_screen.dart';
import 'package:aegis_health/services/api/aegis_api_client.dart';
import 'package:aegis_health/services/api/medical_ai_api_models.dart';
import 'package:aegis_health/services/api/vitals_api_models.dart';
import 'package:aegis_health/services/vault_service.dart';
import 'package:aegis_health/services/time_service.dart';
import 'package:aegis_health/services/vitals_monitor_service.dart';
import 'package:aegis_health/widgets/doctor_documents_section.dart';
import 'package:aegis_health/widgets/referral_banner.dart';

void main() {
  group('Stage 8A.5: Client Models & Serialization', () {
    test('1. ClientReferralStatus and ClientCascadeStatusResponse parse active referral states', () {
      final jsonCascade = {
        'patient_id': 'pat_test_01',
        'device_id': 'dev_test_01',
        'has_active_cycle': true,
        'active_cycle_id': 'cyc_01',
        'has_active_referral': true,
        'referral_status': {
          'has_active_referral': true,
          'status': 'document_ready',
          'document_generation_state': 'ready',
          'urgency': 'priority',
          'target_specialties': ['Cardiology', 'Internal Medicine'],
          'clinical_reason_summary': 'Blood pressure elevation with tachyarrhythmia dynamics',
          'document_report_id': 'rep_doc_123',
          'created_at_utc': '2026-11-08T12:00:00Z',
        },
      };

      final status = ClientCascadeStatusResponse.fromJson(jsonCascade);
      expect(status.hasActiveReferral, isTrue);
      expect(status.referralStatus.hasActiveReferral, isTrue);
      expect(status.referralStatus.status, equals('document_ready'));
      expect(status.referralStatus.documentGenerationState, equals('ready'));
      expect(status.referralStatus.urgency, equals('priority'));
      expect(status.referralStatus.targetSpecialties, containsAll(['Cardiology', 'Internal Medicine']));
      expect(status.referralStatus.documentReportId, equals('rep_doc_123'));

      // Round-trip serialization
      final serialized = status.toJson();
      expect(serialized['has_active_referral'], isTrue);
      expect(serialized['referral_status']['status'], equals('document_ready'));
    });

    test('2. ClientReferralDocumentDetailResponse parses all 7 clinical observation sections without SERVER_ONLY data', () {
      final jsonDetail = {
        'report_id': 'rep_doc_456',
        'cycle_id': 'cyc_01',
        'sprint_id': 'sprint_cyc_01_w2',
        'referral_id': 'REF-20261108-01',
        'created_at_utc': '2026-11-08T14:30:00Z',
        'report_date': '2026-11-08',
        'title': 'AEGIS — HEALTH OBSERVATION SUMMARY FOR YOUR DOCTOR',
        'urgency': 'priority',
        'target_specialties': ['Cardiology'],
        'informational_disclaimer': 'Informational summary for licensed clinician discussion.',
        'executive_summary': 'Patient exhibits sustained stage 2 systolic hypertension with nocturnal dips missing.',
        'sections': [
          {
            'section_number': 1,
            'section_title': 'Patient and Observation Period',
            'content': 'Observation from 2026-11-01 to 2026-11-08 (Sprint 2).',
            'is_bullet_list': false,
          },
          {
            'section_number': 2,
            'section_title': 'Reason for Recommending an In-Person Consultation',
            'content': 'Persistent BP elevation exceeding 150/95 mmHg with tachycardia episodes.',
            'is_bullet_list': false,
          },
          {
            'section_number': 3,
            'section_title': 'Objective Health Measurements and Examination Results',
            'content': [
              'Blood pressure: Mean 152/96 mmHg (systolic range 144–162 mmHg)',
              'Heart rate: Resting mean 88 bpm (tachycardia spikes up to 108 bpm)',
              'SpO₂: 97–99% at rest; no desaturations observed',
              'Laboratory: Troponin I < 0.01 µg/L; NT-proBNP 74 ng/L',
            ],
            'is_bullet_list': true,
          },
          {
            'section_number': 4,
            'section_title': 'Timeline of Reported Symptoms and Observed Changes',
            'content': [
              'Day 3: Mild evening occipital headache reported',
              'Day 6: Palpitations during light stair climbing',
            ],
            'is_bullet_list': true,
          },
          {
            'section_number': 5,
            'section_title': 'Important Patterns and Unresolved Concerns',
            'content': ['Absence of expected nocturnal systolic dip (-2% vs typical -10%)'],
            'is_bullet_list': true,
          },
          {
            'section_number': 6,
            'section_title': 'AI-Generated Observations Requiring Professional Verification',
            'content': ['Algorithmic detection of autonomic cardiovascular dysregulation signature'],
            'is_bullet_list': true,
          },
          {
            'section_number': 7,
            'section_title': 'Questions and Possible Examinations to Discuss with a Licensed Clinician',
            'content': [
              'Consider 24-hour ambulatory blood pressure monitoring (ABPM)',
              'Consider transthoracic echocardiogram to assess left ventricular wall thickness',
            ],
            'is_bullet_list': true,
          },
        ],
        'rendered_markdown': '# Doctor Document...',
        'available_formats': ['pdf', 'docx', 'md'],
        'cycle_status': 'active',
        'document_version': 1,
      };

      final doc = ClientReferralDocumentDetailResponse.fromJson(jsonDetail);
      expect(doc.reportId, equals('rep_doc_456'));
      expect(doc.sections.length, equals(7));
      expect(doc.sections[2].bulletItems.length, equals(4));
      expect(doc.sections[2].bulletItems[3], contains('µg/L'));
      expect(doc.sections[2].bulletItems[2], contains('SpO₂'));
      expect(doc.availableFormats, containsAll(['pdf', 'docx', 'md']));

      // Confirm no SERVER_ONLY fields present in client model
      final map = doc.toJson();
      expect(map.containsKey('clinical_reason_private'), isFalse);
      expect(map.containsKey('deputy_flash_evaluation'), isFalse);
      expect(map.containsKey('internal_reasoning_prompt'), isFalse);
    });
  });

  group('Stage 8A.5: AegisApiClient Referral Endpoints', () {
    test('3. getReferralDocuments sends authenticated GET /api/v1/client/referral/documents', () async {
      final mockClient = MockClient((request) async {
        expect(request.method, equals('GET'));
        expect(request.url.path, equals('/api/v1/client/referral/documents'));
        expect(request.headers['authorization'], equals('Bearer valid_jwt_token'));

        final responseBody = {
          'patient_id': 'pat_100',
          'total_count': 2,
          'documents': [
            {
              'report_id': 'rep_curr',
              'cycle_id': 'cyc_active',
              'sprint_id': 'sprint_cyc_active_w1',
              'referral_id': 'REF-001',
              'created_at_utc': '2026-11-08T10:00:00Z',
              'report_date': '2026-11-08',
              'title': 'AEGIS — HEALTH OBSERVATION SUMMARY FOR YOUR DOCTOR',
              'available_formats': ['pdf', 'docx', 'md'],
              'summary': 'Active cycle referral summary.',
              'urgency': 'priority',
              'target_specialties': ['Cardiology'],
              'cycle_status': 'active',
              'document_version': 1,
            },
            {
              'report_id': 'rep_hist',
              'cycle_id': 'cyc_old',
              'sprint_id': 'sprint_cyc_old_w4',
              'referral_id': 'REF-000',
              'created_at_utc': '2026-10-15T10:00:00Z',
              'report_date': '2026-10-15',
              'title': 'AEGIS — HEALTH OBSERVATION SUMMARY FOR YOUR DOCTOR',
              'available_formats': ['pdf', 'docx', 'md'],
              'summary': 'Historical cycle referral summary.',
              'urgency': 'routine',
              'target_specialties': ['Endocrinology'],
              'cycle_status': 'completed',
              'document_version': 1,
            },
          ],
        };
        return http.Response(json.encode(responseBody), 200, headers: {'content-type': 'application/json'});
      });

      final apiClient = AegisApiClient(
        baseUrl: 'http://127.0.0.1:8000',
        tokenProvider: () async => 'valid_jwt_token',
        client: mockClient,
      );

      final res = await apiClient.getReferralDocuments();
      expect(res.totalCount, equals(2));
      expect(res.documents.first.reportId, equals('rep_curr'));
      expect(res.documents.first.cycleStatus, equals('active'));
      expect(res.documents.last.reportId, equals('rep_hist'));
      expect(res.documents.last.cycleStatus, equals('completed'));
    });

    test('4. exportReferralDocumentBytes sends authenticated GET with format parameter and returns bytes', () async {
      final expectedBytes = Uint8List.fromList([0x25, 0x50, 0x44, 0x46, 0x2D, 0x31, 0x2E, 0x37]); // %PDF-1.7

      final mockClient = MockClient((request) async {
        expect(request.method, equals('GET'));
        expect(request.url.path, equals('/api/v1/client/referral/documents/rep_curr/export'));
        expect(request.url.queryParameters['format'], equals('pdf'));
        expect(request.headers['authorization'], equals('Bearer valid_jwt_token'));

        return http.Response.bytes(expectedBytes, 200, headers: {
          'content-type': 'application/pdf',
          'content-disposition': 'attachment; filename="aegis_doctor_summary.pdf"',
        });
      });

      final apiClient = AegisApiClient(
        baseUrl: 'http://127.0.0.1:8000',
        tokenProvider: () async => 'valid_jwt_token',
        client: mockClient,
      );

      final downloadedBytes = await apiClient.exportReferralDocumentBytes('rep_curr', format: 'pdf');
      expect(downloadedBytes, equals(expectedBytes));
    });

    test('5. Unauthenticated session throws AegisAuthException without disclosing document contents', () async {
      final mockClient = MockClient((request) async {
        return http.Response(
          json.encode({'detail': 'Invalid or expired session token.'}),
          401,
          headers: {'content-type': 'application/json'},
        );
      });

      final apiClient = AegisApiClient(
        baseUrl: 'http://127.0.0.1:8000',
        tokenProvider: () async => null,
        client: mockClient,
      );

      expect(
        () async => await apiClient.getReferralDocuments(),
        throwsA(isA<AegisAuthException>()),
      );
    });
  });

  group('Stage 8A.5: ReferralBanner Widget Tests', () {
    testWidgets('6. Inactive referral renders SizedBox.shrink with 0 banner widgets', (tester) async {
      const statusInactive = ClientReferralStatus(hasActiveReferral: false, status: 'none');

      await tester.pumpWidget(
        MaterialApp(
          home: Scaffold(
            body: ReferralBanner(
              referralStatus: statusInactive,
              onOpenSafe: () {},
            ),
          ),
        ),
      );

      expect(find.byKey(const Key('referral_active_banner')), findsNothing);
    });

    testWidgets('7. generating_document state displays preparation notice without claiming document is ready', (tester) async {
      const statusGenerating = ClientReferralStatus(
        hasActiveReferral: true,
        status: 'generating_document',
        documentGenerationState: 'generating',
      );

      bool safeOpened = false;

      await tester.pumpWidget(
        MaterialApp(
          home: Scaffold(
            body: ReferralBanner(
              referralStatus: statusGenerating,
              onOpenSafe: () {
                safeOpened = true;
              },
            ),
          ),
        ),
      );

      expect(find.byKey(const Key('referral_active_banner')), findsOneWidget);
      expect(
        find.text('An in-person medical consultation has been recommended. Your doctor summary is being prepared.'),
        findsOneWidget,
      );
      // Strictly does NOT claim ready
      expect(find.text('available in Safe'), findsNothing);

      // Tapping action opens Safe
      await tester.tap(find.byKey(const Key('referral_banner_action_btn')));
      await tester.pump();
      expect(safeOpened, isTrue);
    });

    testWidgets('8. document_ready state displays availability notice and opens document on tap', (tester) async {
      const statusReady = ClientReferralStatus(
        hasActiveReferral: true,
        status: 'document_ready',
        documentGenerationState: 'ready',
        documentReportId: 'doc_ready_999',
      );

      String? openedDocId;

      await tester.pumpWidget(
        MaterialApp(
          home: Scaffold(
            body: ReferralBanner(
              referralStatus: statusReady,
              onOpenSafe: () {},
              onOpenDocument: (id) {
                openedDocId = id;
              },
            ),
          ),
        ),
      );

      expect(find.byKey(const Key('referral_active_banner')), findsOneWidget);
      expect(
        find.text('An in-person medical consultation has been recommended. Your doctor summary is available in Safe.'),
        findsOneWidget,
      );

      await tester.tap(find.byKey(const Key('referral_banner_action_btn')));
      await tester.pump();
      expect(openedDocId, equals('doc_ready_999'));
    });

    testWidgets('9. visit_confirmed state hides active banner', (tester) async {
      const statusConfirmed = ClientReferralStatus(
        hasActiveReferral: false,
        status: 'visit_confirmed',
        documentReportId: 'doc_ready_999',
      );

      await tester.pumpWidget(
        MaterialApp(
          home: Scaffold(
            body: ReferralBanner(
              referralStatus: statusConfirmed,
              onOpenSafe: () {},
            ),
          ),
        ),
      );

      expect(find.byKey(const Key('referral_active_banner')), findsNothing);
    });
  });

  group('Stage 8A.5: DoctorDocumentsSection & Safe UI Tests', () {
    testWidgets('10. Documents for Your Doctor renders current and historical items with View, PDF, and DOCX actions', (tester) async {
      final mockClient = MockClient((request) async {
        final listResponse = {
          'patient_id': 'pat_safe_test',
          'total_count': 2,
          'documents': [
            {
              'report_id': 'doc_active_1',
              'cycle_id': 'cycle_active',
              'referral_id': 'REF-ACT',
              'created_at_utc': '2026-11-08T10:00:00Z',
              'report_date': '2026-11-08',
              'title': 'AEGIS — HEALTH OBSERVATION SUMMARY FOR YOUR DOCTOR',
              'available_formats': ['pdf', 'docx', 'md'],
              'summary': 'Summary of active observations.',
              'urgency': 'priority',
              'target_specialties': ['Cardiology'],
              'cycle_status': 'active',
              'document_version': 1,
            },
            {
              'report_id': 'doc_hist_2',
              'cycle_id': 'cycle_completed',
              'referral_id': 'REF-HIST',
              'created_at_utc': '2026-10-10T10:00:00Z',
              'report_date': '2026-10-10',
              'title': 'AEGIS — HEALTH OBSERVATION SUMMARY FOR YOUR DOCTOR',
              'available_formats': ['pdf', 'docx', 'md'],
              'summary': 'Summary of past observations.',
              'urgency': 'routine',
              'target_specialties': ['Endocrinology'],
              'cycle_status': 'completed',
              'document_version': 1,
            },
          ],
        };
        return http.Response(json.encode(listResponse), 200, headers: {'content-type': 'application/json'});
      });

      final apiClient = AegisApiClient(
        baseUrl: 'http://127.0.0.1:8000',
        tokenProvider: () async => 'jwt_token',
        client: mockClient,
      );

      await tester.pumpWidget(
        MaterialApp(
          home: Scaffold(
            body: SingleChildScrollView(
              child: DoctorDocumentsSection(
                apiClient: apiClient,
                referralStatus: const ClientReferralStatus(
                  hasActiveReferral: true,
                  status: 'document_ready',
                ),
              ),
            ),
          ),
        ),
      );

      // Wait for documents to load
      await tester.pumpAndSettle();

      expect(find.text('DOCUMENTS FOR YOUR DOCTOR'), findsOneWidget);
      expect(find.text('Current Cycle'), findsOneWidget);
      expect(find.text('Historical'), findsOneWidget);
      expect(find.text('PRIORITY'), findsOneWidget);
      expect(find.text('Recommended: Cardiology'), findsOneWidget);
      expect(find.text('Recommended: Endocrinology'), findsOneWidget);

      // Each document has View, PDF, and DOCX buttons
      expect(find.byKey(const Key('view_doc_doc_active_1')), findsOneWidget);
      expect(find.byKey(const Key('pdf_doc_doc_active_1')), findsOneWidget);
      expect(find.byKey(const Key('docx_doc_doc_active_1')), findsOneWidget);

      expect(find.byKey(const Key('view_doc_doc_hist_2')), findsOneWidget);
      expect(find.byKey(const Key('pdf_doc_doc_hist_2')), findsOneWidget);
      expect(find.byKey(const Key('docx_doc_doc_hist_2')), findsOneWidget);
    });

    testWidgets('11. DoctorDocumentViewScreen renders all 7 clinical sections preserving units and disclaimers', (tester) async {
      final mockClient = MockClient((request) async {
        final detail = {
          'report_id': 'doc_view_test',
          'cycle_id': 'cyc_01',
          'referral_id': 'REF-VIEW',
          'created_at_utc': '2026-11-08T12:00:00Z',
          'report_date': '2026-11-08',
          'title': 'AEGIS — HEALTH OBSERVATION SUMMARY FOR YOUR DOCTOR',
          'urgency': 'priority',
          'target_specialties': ['Cardiology', 'Internal Medicine'],
          'informational_disclaimer': 'Safe clinical observation summary for clinician discussion.',
          'executive_summary': 'Sustained elevation of systolic blood pressure.',
          'sections': [
            {'section_number': 1, 'section_title': 'Patient and Observation Period', 'content': 'Sprint 1.', 'is_bullet_list': false},
            {'section_number': 2, 'section_title': 'Reason for Recommending an In-Person Consultation', 'content': 'Hypertension.', 'is_bullet_list': false},
            {
              'section_number': 3,
              'section_title': 'Objective Health Measurements and Examination Results',
              'content': [
                'Troponin I: < 0.01 µg/L (normal)',
                'SpO₂: 98% (normal)',
                'Temperature: 36.6 °C',
              ],
              'is_bullet_list': true,
            },
            {'section_number': 4, 'section_title': 'Timeline of Reported Symptoms and Observed Changes', 'content': ['Mild headache'], 'is_bullet_list': true},
            {'section_number': 5, 'section_title': 'Important Patterns and Unresolved Concerns', 'content': ['Nocturnal non-dipper'], 'is_bullet_list': true},
            {'section_number': 6, 'section_title': 'AI-Generated Observations Requiring Professional Verification', 'content': ['Dysregulation candidate'], 'is_bullet_list': true},
            {'section_number': 7, 'section_title': 'Questions and Possible Examinations to Discuss with a Licensed Clinician', 'content': ['24h ABPM advised'], 'is_bullet_list': true},
          ],
          'rendered_markdown': '# Rendered doc',
          'available_formats': ['pdf', 'docx', 'md'],
          'cycle_status': 'active',
          'document_version': 1,
        };
        return http.Response(json.encode(detail), 200, headers: {'content-type': 'application/json'});
      });

      final apiClient = AegisApiClient(
        baseUrl: 'http://127.0.0.1:8000',
        tokenProvider: () async => 'jwt_token',
        client: mockClient,
      );

      await tester.pumpWidget(
        MaterialApp(
          home: DoctorDocumentViewScreen(
            reportId: 'doc_view_test',
            apiClient: apiClient,
          ),
        ),
      );

      await tester.pumpAndSettle();

      expect(find.text('DOCUMENT FOR YOUR DOCTOR'), findsOneWidget);
      expect(find.text('AEGIS — HEALTH OBSERVATION SUMMARY FOR YOUR DOCTOR'), findsOneWidget);
      expect(find.text('IMPORTANT INFORMATIONAL NOTICE'), findsOneWidget);
      expect(find.text('EXECUTIVE SUMMARY FOR PATIENT'), findsOneWidget);
      expect(find.text('1. Patient and Observation Period'), findsOneWidget);
      expect(find.text('3. Objective Health Measurements and Examination Results'), findsOneWidget);
      expect(find.text('7. Questions and Possible Examinations to Discuss with a Licensed Clinician'), findsOneWidget);

      // Check preservation of clinical notation
      expect(find.text('Troponin I: < 0.01 µg/L (normal)'), findsOneWidget);
      expect(find.text('SpO₂: 98% (normal)'), findsOneWidget);
      expect(find.text('Temperature: 36.6 °C'), findsOneWidget);

      // Download buttons present
      expect(find.byKey(const Key('download_pdf_btn')), findsOneWidget);
      expect(find.byKey(const Key('download_docx_btn')), findsOneWidget);
    });

    testWidgets('12. CuratorVaultSheet renders DoctorDocumentsSection, Stage 8B Neo placeholder, and retains separate reset button', (tester) async {
      final mockClient = MockClient((request) async {
        final listResponse = {
          'patient_id': 'pat_safe_test',
          'total_count': 0,
          'documents': [],
        };
        return http.Response(json.encode(listResponse), 200, headers: {'content-type': 'application/json'});
      });

      final apiClient = AegisApiClient(
        baseUrl: 'http://127.0.0.1:8000',
        tokenProvider: () async => 'jwt_token',
        client: mockClient,
      );

      final vaultService = VaultService();
      final timeService = TimeService();
      final vitalsService = VitalsMonitorService(timeService, vaultService);

      await tester.pumpWidget(
        MultiProvider(
          providers: [
            ChangeNotifierProvider<VaultService>.value(value: vaultService),
            ChangeNotifierProvider<TimeService>.value(value: timeService),
            ChangeNotifierProvider<VitalsMonitorService>.value(value: vitalsService),
            Provider<AegisApiClient>.value(value: apiClient),
          ],
          child: const MaterialApp(
            home: Scaffold(
              body: CuratorVaultSheet(),
            ),
          ),
        ),
      );

      await tester.pumpAndSettle();

      // Check header
      expect(find.text('CURATOR VAULT :: AEGIS'), findsOneWidget);

      // Check delete/reset button remains separate on top right
      expect(find.byIcon(Icons.delete_forever_rounded), findsOneWidget);

      // Check Documents for Your Doctor section is rendered
      expect(find.text('DOCUMENTS FOR YOUR DOCTOR'), findsOneWidget);

      // Check Stage 8B Neo placeholder exists and is non-active
      expect(find.byKey(const Key('update_health_info_placeholder')), findsOneWidget);
      expect(find.text('Update Health Information'), findsOneWidget);
      expect(find.text('Future incremental update (Stage 8B). Available after in-person doctor visit.'), findsOneWidget);
    });

    testWidgets('13. CuratorVaultSheet hides resetVault button in release mode while keeping Update Health Information inactive', (tester) async {
      final mockClient = MockClient((request) async {
        final listResponse = {
          'patient_id': 'pat_safe_release',
          'total_count': 0,
          'documents': [],
        };
        return http.Response(json.encode(listResponse), 200, headers: {'content-type': 'application/json'});
      });

      final apiClient = AegisApiClient(
        baseUrl: 'http://127.0.0.1:8000',
        tokenProvider: () async => 'jwt_token',
        client: mockClient,
      );

      final vaultService = VaultService();
      final timeService = TimeService();
      final vitalsService = VitalsMonitorService(timeService, vaultService);

      await tester.pumpWidget(
        MultiProvider(
          providers: [
            ChangeNotifierProvider<VaultService>.value(value: vaultService),
            ChangeNotifierProvider<TimeService>.value(value: timeService),
            ChangeNotifierProvider<VitalsMonitorService>.value(value: vitalsService),
            Provider<AegisApiClient>.value(value: apiClient),
          ],
          child: const MaterialApp(
            home: Scaffold(
              body: CuratorVaultSheet(debugShowResetButton: false),
            ),
          ),
        ),
      );

      await tester.pumpAndSettle();

      // Destructive reset button is completely hidden when debugShowResetButton is false (release mode)
      expect(find.byKey(const Key('debug_reset_vault_btn')), findsNothing);
      expect(find.byIcon(Icons.delete_forever_rounded), findsNothing);

      // Update Health Information (Neo placeholder) remains present and inactive
      expect(find.byKey(const Key('update_health_info_placeholder')), findsOneWidget);
      expect(find.text('Update Health Information'), findsOneWidget);
    });

    testWidgets('14. Export security: user cancel writes 0 files, platform error writes 0 temp files and shows error, valid save overwrites placeholder', (tester) async {
      final expectedPdfBytes = Uint8List.fromList([0x25, 0x50, 0x44, 0x46, 0x2D, 0x31, 0x2E, 0x37]);
      final mockClient = MockClient((request) async {
        if (request.url.path.endsWith('/export')) {
          return http.Response.bytes(expectedPdfBytes, 200, headers: {'content-type': 'application/pdf'});
        }
        final detail = {
          'report_id': 'doc_export_sec',
          'cycle_id': 'cyc_01',
          'referral_id': 'REF-EXP',
          'created_at_utc': '2026-11-08T12:00:00Z',
          'report_date': '2026-11-08',
          'title': 'AEGIS — HEALTH OBSERVATION SUMMARY FOR YOUR DOCTOR',
          'urgency': 'priority',
          'target_specialties': ['Cardiology'],
          'informational_disclaimer': 'Safe clinical observation summary.',
          'executive_summary': 'Summary.',
          'sections': [
            {'section_number': 1, 'section_title': 'Patient and Observation Period', 'content': 'Sprint 1.', 'is_bullet_list': false},
            {'section_number': 2, 'section_title': 'Reason for Recommending an In-Person Consultation', 'content': 'Reason.', 'is_bullet_list': false},
            {'section_number': 3, 'section_title': 'Objective Health Measurements and Examination Results', 'content': ['HR 72 bpm'], 'is_bullet_list': true},
            {'section_number': 4, 'section_title': 'Timeline of Reported Symptoms and Observed Changes', 'content': ['None'], 'is_bullet_list': true},
            {'section_number': 5, 'section_title': 'Important Patterns and Unresolved Concerns', 'content': ['Pattern'], 'is_bullet_list': true},
            {'section_number': 6, 'section_title': 'AI-Generated Observations Requiring Professional Verification', 'content': ['Obs'], 'is_bullet_list': true},
            {'section_number': 7, 'section_title': 'Questions and Possible Examinations to Discuss with a Licensed Clinician', 'content': ['Q1'], 'is_bullet_list': true},
          ],
          'rendered_markdown': '# Doc',
          'available_formats': ['pdf', 'docx', 'md'],
          'cycle_status': 'active',
          'document_version': 1,
        };
        return http.Response(json.encode(detail), 200, headers: {'content-type': 'application/json'});
      });

      final apiClient = AegisApiClient(
        baseUrl: 'http://127.0.0.1:8000',
        tokenProvider: () async => 'jwt_token',
        client: mockClient,
      );

      final testTempDir = Directory.systemTemp.createTempSync('aegis_export_test_');
      try {
        int mode = 0; // 0 = cancel (null), 1 = throw platform error, 2 = save to placeholder file
        final targetFile = File('${testTempDir.path}/saved_summary.pdf');
        targetFile.writeAsBytesSync(const []); // 0-byte placeholder created by OS dialog

        await tester.pumpWidget(
          MaterialApp(
            home: DoctorDocumentViewScreen(
              reportId: 'doc_export_sec',
              apiClient: apiClient,
              customFileSaver: ({
                required dialogTitle,
                required fileName,
                required allowedExtensions,
                required bytes,
              }) async {
                if (mode == 0) return null;
                if (mode == 1) throw Exception('Unsupported platform file dialog');
                return targetFile.path;
              },
            ),
          ),
        );
        await tester.pumpAndSettle();

        await tester.ensureVisible(find.byKey(const Key('download_pdf_btn')));
        await tester.pumpAndSettle();

        // Case A: User cancels dialog -> no SnackBar, 0 bytes written
        mode = 0;
        await tester.tap(find.byKey(const Key('download_pdf_btn')));
        await tester.pumpAndSettle();
        expect(find.textContaining('Document successfully saved'), findsNothing);
        expect(find.textContaining('temporary storage'), findsNothing);
        expect(targetFile.lengthSync(), equals(0));

        // Case B: Platform throws exception -> honest error SnackBar, NO temporary storage fallback
        mode = 1;
        await tester.tap(find.byKey(const Key('download_pdf_btn')));
        await tester.pumpAndSettle();
        expect(find.textContaining('Failed to export pdf:'), findsOneWidget);
        expect(find.textContaining('temporary storage'), findsNothing);
        expect(targetFile.lengthSync(), equals(0));
        ScaffoldMessenger.of(tester.element(find.byType(DoctorDocumentViewScreen))).clearSnackBars();
        await tester.pumpAndSettle();

        // Case C: User selects target file (even if 0-byte placeholder exists) -> full bytes written
        mode = 2;
        await tester.tap(find.byKey(const Key('download_pdf_btn')));
        await tester.pumpAndSettle();
        expect(find.textContaining('Document successfully saved: saved_summary.pdf'), findsOneWidget);
        expect(targetFile.readAsBytesSync(), equals(expectedPdfBytes));
      } finally {
        if (testTempDir.existsSync()) {
          testTempDir.deleteSync(recursive: true);
        }
      }
    });

    testWidgets('15. Full E2E Referral Lifecycle in Flutter: generating -> ready -> view 7 sections -> visit_confirmed hides banner while document remains in Safe history', (tester) async {
      final mockClient = MockClient((request) async {
        if (request.url.path == '/api/v1/client/referral/documents') {
          return http.Response(
            json.encode({
              'patient_id': 'pt_e2e',
              'total_count': 2,
              'documents': [
                {
                  'report_id': 'rep_v2_latest',
                  'cycle_id': 'cyc_active',
                  'sprint_id': 'sprint_cyc_active_w2',
                  'referral_id': 'REF-E2E-02',
                  'created_at_utc': '2026-11-08T12:00:00Z',
                  'report_date': '2026-11-08',
                  'title': 'AEGIS — HEALTH OBSERVATION SUMMARY FOR YOUR DOCTOR',
                  'available_formats': ['pdf', 'docx', 'md'],
                  'summary': 'Latest observation summary.',
                  'urgency': 'priority',
                  'target_specialties': ['cardiologist', 'endocrinologist'],
                  'cycle_status': 'active',
                  'document_version': 2,
                },
                {
                  'report_id': 'rep_v1_superseded',
                  'cycle_id': 'cyc_active',
                  'sprint_id': 'sprint_cyc_active_w1',
                  'referral_id': 'REF-E2E-01',
                  'created_at_utc': '2026-11-01T12:00:00Z',
                  'report_date': '2026-11-01',
                  'title': 'AEGIS — HEALTH OBSERVATION SUMMARY FOR YOUR DOCTOR',
                  'available_formats': ['pdf', 'docx', 'md'],
                  'summary': 'Earlier observation summary.',
                  'urgency': 'routine',
                  'target_specialties': ['cardiologist'],
                  'cycle_status': 'active',
                  'document_version': 1,
                },
              ],
            }),
            200,
            headers: {'content-type': 'application/json'},
          );
        }
        return http.Response('Not found', 404);
      });

      final apiClient = AegisApiClient(
        baseUrl: 'http://127.0.0.1:8000',
        tokenProvider: () async => 'jwt_token',
        client: mockClient,
      );

      // Step 1: Parse cascade status after visit_confirmed (when user replied "I already did")
      final cascadeAfterConfirmJson = <dynamic, dynamic>{
        'patient_id': 'pt_e2e',
        'device_id': 'dev_e2e',
        'has_active_cycle': true,
        'active_cycle_id': 'cyc_active',
        'has_active_referral': false,
        'referral_status': <dynamic, dynamic>{
          'has_active_referral': false,
          'status': 'visit_confirmed',
          'document_generation_state': 'ready',
          'urgency': 'priority',
          'target_specialties': ['cardiologist', 'endocrinologist'],
          'clinical_reason_summary': 'Resting tachycardia and elevated TSH.',
          'document_report_id': 'rep_v2_latest',
          'created_at_utc': '2026-11-01T10:00:00+00:00',
        },
      };
      final parsedCascade = ClientCascadeStatusResponse.fromJson(
        Map<String, dynamic>.from(cascadeAfterConfirmJson),
      );
      expect(parsedCascade.hasActiveReferral, isFalse);
      expect(parsedCascade.referralStatus.status, equals('visit_confirmed'));
      expect(parsedCascade.referralStatus.documentReportId, equals('rep_v2_latest'));

      // Step 2: Active banner disappears when status == visit_confirmed, while Safe still lists both documents
      // and badges only the latest version (v2) as Current Cycle and superseded v1 as Historical.
      await tester.pumpWidget(
        MaterialApp(
          home: Scaffold(
            body: SingleChildScrollView(
              child: Column(
                children: [
                  ReferralBanner(
                    referralStatus: parsedCascade.referralStatus,
                    onOpenSafe: () {},
                  ),
                  DoctorDocumentsSection(
                    apiClient: apiClient,
                    referralStatus: parsedCascade.referralStatus,
                  ),
                ],
              ),
            ),
          ),
        ),
      );
      await tester.pumpAndSettle();

      // Active banner is gone
      expect(find.byKey(const Key('referral_active_banner')), findsNothing);

      // Safe still displays both documents (v2 latest and v1 historical)
      expect(find.byKey(const Key('doctor_doc_card_rep_v2_latest')), findsOneWidget);
      expect(find.byKey(const Key('doctor_doc_card_rep_v1_superseded')), findsOneWidget);
      expect(find.text('Current Cycle'), findsOneWidget);
      expect(find.text('Historical'), findsOneWidget);
    });
  });
}
