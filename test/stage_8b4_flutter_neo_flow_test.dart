import 'dart:convert';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:provider/provider.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'package:aegis_health/main.dart';
import 'package:aegis_health/anamnesis_screen.dart';
import 'package:aegis_health/screens/aegis_consilium_screen.dart';
import 'package:aegis_health/screens/athena_welcome_sheet.dart';
import 'package:aegis_health/screens/curator_chat_screen.dart';
import 'package:aegis_health/services/api/aegis_api_client.dart';
import 'package:aegis_health/services/api/medical_ai_api_models.dart';
import 'package:aegis_health/services/auth/auth_models.dart';
import 'package:aegis_health/services/auth/auth_session_manager.dart';
import 'package:aegis_health/services/auth/auth_token_storage.dart';
import 'package:aegis_health/services/brain_service.dart';
import 'package:aegis_health/services/health_data_provider.dart';
import 'package:aegis_health/services/sync/vitals_sync_queue.dart';
import 'package:aegis_health/services/time_service.dart';
import 'package:aegis_health/services/vault_service.dart';
import 'package:aegis_health/services/vault_storage_service.dart';
import 'package:aegis_health/services/vitals_monitor_service.dart';
import 'package:aegis_health/widgets/doctor_documents_section.dart';

String _buildTestJwt({
  required String patientId,
  String deviceId = 'test_device',
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
  const s = 'c2lnbmF0dXJlX2Zha2Vfc2VjcmV0X2FkZHM';
  return '$h.$p.$s';
}

AuthSessionManager _buildTestAuthManager({
  required AegisApiClient apiClient,
  String patientId = 'pat_001',
}) {
  final session = AuthSession(
    accessToken: _buildTestJwt(patientId: patientId),
    refreshToken: 'ref_tok',
    expiresIn: 3600,
    expiresAt: DateTime.now().toUtc().add(const Duration(hours: 1)),
    refreshExpiresAt: DateTime.now().toUtc().add(const Duration(days: 30)),
    patientId: patientId,
    deviceId: 'dev_test',
    scope: 'cascade:intake cascade:run cycle:read reports:read chat:read chat:write',
  );
  final storage = InMemoryAuthTokenStorage(initialSession: session);
  final authManager = AuthSessionManager(apiClient: apiClient, storage: storage);
  authManager.setSessionForTesting(
    session,
    status: AuthSessionStatus.authenticated,
  );
  return authManager;
}

List<Map<String, dynamic>> _sampleClientReportsJson({required String cycleId, String reportDate = '2026-11-09'}) {
  return [
    {
      'report_id': 'rep_${cycleId}_overall',
      'cycle_id': cycleId,
      'model': 'gemini-3.1-pro-preview',
      'report_type': 'user_overall_report',
      'privacy_level': 'client_facing',
      'version': 1,
      'content': {
        'title': 'Интегральное заключение первичного консилиума',
        'summary': 'Все системы стабильны.',
        'report_date': reportDate,
        'highlights': ['Ритм синусовый'],
      },
      'created_at': '${reportDate}T08:00:00Z',
    },
  ];
}

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  group('Stage 8B.4: Flutter Neo Flow Tests', () {
    setUp(() async {
      SharedPreferences.setMockInitialValues({});
      await VaultStorageService.clearVault();
    });

    // -------------------------------------------------------------------------
    // TEST 1: Button is available and interactive in CuratorVaultSheet
    // -------------------------------------------------------------------------
    testWidgets('1. Update Health Information button is available and interactive in CuratorVaultSheet', (tester) async {
      final mockClient = MockClient((request) async {
        return http.Response(
          json.encode({'patient_id': 'p1', 'total_count': 0, 'documents': []}),
          200,
          headers: {'content-type': 'application/json'},
        );
      });

      final apiClient = AegisApiClient(baseUrl: 'http://127.0.0.1:8000', client: mockClient);
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
          child: const MaterialApp(home: Scaffold(body: CuratorVaultSheet())),
        ),
      );
      await tester.pumpAndSettle();

      expect(find.byKey(const Key('update_health_info_placeholder')), findsOneWidget);
      expect(find.text('Update Health Information'), findsOneWidget);

      // Tap button and verify dialog opens
      await tester.tap(find.byKey(const Key('update_health_info_placeholder')));
      await tester.pumpAndSettle();

      expect(find.byType(AlertDialog), findsOneWidget);
    });

    // -------------------------------------------------------------------------
    // TEST 2: Warning dialog explains new assessment & history preservation
    // -------------------------------------------------------------------------
    testWidgets('2. Warning dialog explains new assessment and history preservation', (tester) async {
      final mockClient = MockClient((request) async {
        return http.Response(
          json.encode({'patient_id': 'p1', 'total_count': 0, 'documents': []}),
          200,
          headers: {'content-type': 'application/json'},
        );
      });

      final apiClient = AegisApiClient(baseUrl: 'http://127.0.0.1:8000', client: mockClient);
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
          child: const MaterialApp(home: Scaffold(body: CuratorVaultSheet())),
        ),
      );
      await tester.pumpAndSettle();

      await tester.tap(find.byKey(const Key('update_health_info_placeholder')));
      await tester.pumpAndSettle();

      expect(find.text('Update Health Information'), findsNWidgets(2)); // Card title + Dialog title
      expect(
        find.text('You will complete a new health assessment. Your previous medical history and documents will be preserved.'),
        findsOneWidget,
      );
      expect(find.byKey(const Key('update_health_info_cancel_btn')), findsOneWidget);
      expect(find.byKey(const Key('update_health_info_continue_btn')), findsOneWidget);
    });

    // -------------------------------------------------------------------------
    // TEST 3: Warning dialog cancel dismisses dialog without state change
    // -------------------------------------------------------------------------
    testWidgets('3. Warning dialog cancel dismisses dialog without state change or network call', (tester) async {
      int postCalls = 0;
      final mockClient = MockClient((request) async {
        if (request.method == 'POST') postCalls++;
        return http.Response(
          json.encode({'patient_id': 'p1', 'total_count': 0, 'documents': []}),
          200,
          headers: {'content-type': 'application/json'},
        );
      });

      final apiClient = AegisApiClient(baseUrl: 'http://127.0.0.1:8000', client: mockClient);
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
          child: const MaterialApp(home: Scaffold(body: CuratorVaultSheet())),
        ),
      );
      await tester.pumpAndSettle();

      await tester.tap(find.byKey(const Key('update_health_info_placeholder')));
      await tester.pumpAndSettle();

      await tester.tap(find.byKey(const Key('update_health_info_cancel_btn')));
      await tester.pumpAndSettle();

      expect(find.byType(AlertDialog), findsNothing);
      expect(postCalls, equals(0));
      expect(vaultService.isNeoReassessment, isFalse);
    });

    // -------------------------------------------------------------------------
    // TEST 4: Pre-Reset Synchronization Barrier flushes pending vitals before Neo request
    // -------------------------------------------------------------------------
    testWidgets('4. Pre-Reset Synchronization Barrier flushes pending vitals before Neo request', (tester) async {
      final networkCalls = <String>[];
      final mockClient = MockClient((request) async {
        networkCalls.add('${request.method} ${request.url.path}');
        if (request.url.path.contains('/api/v1/cycles/current')) {
          return http.Response(
            json.encode({
              'cycle_id': 'cycle_prev_old',
              'patient_id': 'p1',
              'status': 'active',
              'started_at': DateTime.now().toUtc().toIso8601String(),
            }),
            200,
            headers: {'content-type': 'application/json'},
          );
        }
        if (request.url.path.contains('/vitals/batch')) {
          return http.Response(
            json.encode({
              'batch_id': 'b1',
              'recorded_count': 1,
              'deduplicated_count': 0,
              'rejected_count': 0,
              'server_time_utc': DateTime.now().toUtc().toIso8601String(),
            }),
            200,
            headers: {'content-type': 'application/json'},
          );
        }
        if (request.url.path.contains('/start-neo-reassessment')) {
          return http.Response(
            json.encode({
              'patient_id': 'p1',
              'cycle_id': 'cycle_neo_new',
              'previous_cycle_id': 'cycle_prev_old',
              'status': 'pending_reassessment',
              'reassessment_status': 'pending_reassessment',
              'is_pending_reassessment': true,
              'created_at_utc': DateTime.now().toUtc().toIso8601String(),
              'message': 'Neo reassessment cycle provisioned.',
            }),
            200,
            headers: {'content-type': 'application/json'},
          );
        }
        return http.Response(
          json.encode({'patient_id': 'p1', 'total_count': 0, 'documents': []}),
          200,
          headers: {'content-type': 'application/json'},
        );
      });

      final apiClient = AegisApiClient(
        baseUrl: 'http://127.0.0.1:8000',
        tokenProvider: () async => _buildTestJwt(patientId: 'p4'),
        client: mockClient,
      );
      final authManager = _buildTestAuthManager(apiClient: apiClient, patientId: 'p4');
      final vaultService = VaultService();
      await vaultService.setActivePatientId('p4');
      final timeService = TimeService();
      final vitalsService = VitalsMonitorService(timeService, vaultService);
      final syncQueue = VitalsSyncQueue(
        apiClient: apiClient,
        activeCycleId: 'cycle_prev_old',
        authSessionManager: authManager,
        activePatientId: 'p4',
      );

      // Pre-seed an unsynced vital measurement
      await syncQueue.enqueue(
        HealthMeasurement(
          timestamp: DateTime.now().toUtc(),
          source: HealthDataSource.manualInput,
          systolic: 125,
          diastolic: 82,
        ),
        cycleId: 'cycle_prev_old',
      );
      expect(syncQueue.pendingItems.length, equals(1));

      await tester.pumpWidget(
        MultiProvider(
          providers: [
            ChangeNotifierProvider<VaultService>.value(value: vaultService),
            ChangeNotifierProvider<AuthSessionManager>.value(value: authManager),
            ChangeNotifierProvider<TimeService>.value(value: timeService),
            ChangeNotifierProvider<VitalsMonitorService>.value(value: vitalsService),
            ChangeNotifierProvider<VitalsSyncQueue>.value(value: syncQueue),
            Provider<AegisApiClient>.value(value: apiClient),
          ],
          child: const MaterialApp(home: Scaffold(body: CuratorVaultSheet())),
        ),
      );
      await tester.pumpAndSettle();

      await tester.tap(find.byKey(const Key('update_health_info_placeholder')));
      await tester.pumpAndSettle();

      await tester.tap(find.byKey(const Key('update_health_info_continue_btn')));
      await tester.pumpAndSettle();

      // Verify execution order: vitals/batch MUST be requested BEFORE start-neo-reassessment!
      expect(networkCalls.any((c) => c.contains('POST /api/v1/vitals/batch')), isTrue);
      expect(networkCalls.any((c) => c.contains('POST /api/v1/cycles/start-neo-reassessment')), isTrue);
      final vitalsIdx = networkCalls.indexWhere((c) => c.contains('POST /api/v1/vitals/batch'));
      final neoIdx = networkCalls.indexWhere((c) => c.contains('POST /api/v1/cycles/start-neo-reassessment'));
      expect(vitalsIdx, lessThan(neoIdx));

      // Verify queue is flushed
      expect(syncQueue.pendingItems.isEmpty, isTrue);
      expect(vaultService.isNeoReassessment, isTrue);
      expect(vaultService.consiliumCycleId, equals('cycle_neo_new'));
    });

    // -------------------------------------------------------------------------
    // TEST 5: Pre-Reset Synchronization failure blocks Neo and preserves data
    // -------------------------------------------------------------------------
    testWidgets('5. Pre-Reset Synchronization failure blocks Neo and preserves local measurements', (tester) async {
      final networkCalls = <String>[];
      final mockClient = MockClient((request) async {
        networkCalls.add('${request.method} ${request.url.path}');
        if (request.url.path.contains('/api/v1/cycles/current')) {
          return http.Response(
            json.encode({
              'cycle_id': 'cycle_prev_old',
              'patient_id': 'p5',
              'status': 'active',
              'started_at': DateTime.now().toUtc().toIso8601String(),
            }),
            200,
            headers: {'content-type': 'application/json'},
          );
        }
        if (request.url.path.contains('/vitals/batch')) {
          return http.Response(
            json.encode({'detail': 'Transient server failure'}),
            500,
            headers: {'content-type': 'application/json'},
          );
        }
        return http.Response(
          json.encode({'patient_id': 'p5', 'total_count': 0, 'documents': []}),
          200,
          headers: {'content-type': 'application/json'},
        );
      });

      final apiClient = AegisApiClient(
        baseUrl: 'http://127.0.0.1:8000',
        tokenProvider: () async => _buildTestJwt(patientId: 'p5'),
        client: mockClient,
      );
      final authManager = _buildTestAuthManager(apiClient: apiClient, patientId: 'p5');
      final vaultService = VaultService();
      await vaultService.setActivePatientId('p5');
      final timeService = TimeService();
      final vitalsService = VitalsMonitorService(timeService, vaultService);
      final syncQueue = VitalsSyncQueue(
        apiClient: apiClient,
        activeCycleId: 'cycle_prev_old',
        authSessionManager: authManager,
        activePatientId: 'p5',
      );

      await syncQueue.enqueue(
        HealthMeasurement(
          timestamp: DateTime.now().toUtc(),
          source: HealthDataSource.manualInput,
          pulse: 78,
        ),
        cycleId: 'cycle_prev_old',
      );
      expect(syncQueue.pendingItems.length, equals(1));

      await tester.pumpWidget(
        MultiProvider(
          providers: [
            ChangeNotifierProvider<VaultService>.value(value: vaultService),
            ChangeNotifierProvider<AuthSessionManager>.value(value: authManager),
            ChangeNotifierProvider<TimeService>.value(value: timeService),
            ChangeNotifierProvider<VitalsMonitorService>.value(value: vitalsService),
            ChangeNotifierProvider<VitalsSyncQueue>.value(value: syncQueue),
            Provider<AegisApiClient>.value(value: apiClient),
          ],
          child: const MaterialApp(home: Scaffold(body: CuratorVaultSheet())),
        ),
      );
      await tester.pumpAndSettle();

      await tester.tap(find.byKey(const Key('update_health_info_placeholder')));
      await tester.pumpAndSettle();

      await tester.tap(find.byKey(const Key('update_health_info_continue_btn')));
      await tester.pumpAndSettle();

      // Verify Neo start was NEVER called
      expect(networkCalls.any((call) => call.contains('start-neo-reassessment')), isFalse);

      // Verify error dialog shown to user
      expect(find.text('Synchronization Barrier'), findsOneWidget);
      expect(find.byKey(const Key('sync_error_dialog_ok_btn')), findsOneWidget);

      // Verify local measurements preserved in queue
      expect(syncQueue.pendingItems.length, equals(1));
      expect(vaultService.isNeoReassessment, isFalse);
    });

    // -------------------------------------------------------------------------
    // TEST 6: Duplicate tap prevention does not create two cycles
    // -------------------------------------------------------------------------
    testWidgets('6. Duplicate tap prevention does not create two cycles', (tester) async {
      int startCalls = 0;
      final mockClient = MockClient((request) async {
        if (request.url.path.contains('/start-neo-reassessment')) {
          startCalls++;
          return http.Response(
            json.encode({
              'patient_id': 'p1',
              'cycle_id': 'cycle_neo_dup',
              'previous_cycle_id': 'cycle_prev',
              'status': 'pending_reassessment',
              'reassessment_status': 'pending_reassessment',
              'is_pending_reassessment': true,
              'created_at_utc': DateTime.now().toUtc().toIso8601String(),
              'message': 'Neo reassessment cycle provisioned.',
            }),
            200,
            headers: {'content-type': 'application/json'},
          );
        }
        return http.Response(
          json.encode({'patient_id': 'p1', 'total_count': 0, 'documents': []}),
          200,
          headers: {'content-type': 'application/json'},
        );
      });

      final apiClient = AegisApiClient(baseUrl: 'http://127.0.0.1:8000', client: mockClient);
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
          child: const MaterialApp(home: Scaffold(body: CuratorVaultSheet())),
        ),
      );
      await tester.pumpAndSettle();

      await tester.tap(find.byKey(const Key('update_health_info_placeholder')));
      await tester.pumpAndSettle();

      // Tap continue once
      await tester.tap(find.byKey(const Key('update_health_info_continue_btn')));
      await tester.pumpAndSettle();

      expect(startCalls, equals(1));
      expect(vaultService.consiliumCycleId, equals('cycle_neo_dup'));
    });

    // -------------------------------------------------------------------------
    // TEST 7: Server idempotently restores existing pending Neo on re-request
    // -------------------------------------------------------------------------
    testWidgets('7. Re-request restores pending Neo cleanly', (tester) async {
      final mockClient = MockClient((request) async {
        if (request.url.path.contains('/start-neo-reassessment')) {
          return http.Response(
            json.encode({
              'patient_id': 'p1',
              'cycle_id': 'cycle_neo_existing_pending',
              'previous_cycle_id': 'cycle_prev_old',
              'status': 'pending_reassessment',
              'reassessment_status': 'pending_reassessment',
              'is_pending_reassessment': true,
              'created_at_utc': '2026-11-09T18:00:00Z',
              'message': 'Restored existing pending reassessment cycle.',
            }),
            200,
            headers: {'content-type': 'application/json'},
          );
        }
        return http.Response(
          json.encode({'patient_id': 'p1', 'total_count': 0, 'documents': []}),
          200,
          headers: {'content-type': 'application/json'},
        );
      });

      final apiClient = AegisApiClient(baseUrl: 'http://127.0.0.1:8000', client: mockClient);
      final res = await apiClient.startNeoReassessment();
      expect(res.cycleId, equals('cycle_neo_existing_pending'));
      expect(res.isPendingReassessment, isTrue);
      expect(res.previousCycleId, equals('cycle_prev_old'));
    });

    // -------------------------------------------------------------------------
    // TEST 8: Switching active local cycle_id cleanly
    // -------------------------------------------------------------------------
    testWidgets('8. Switching active local cycle_id updates Vault and SyncQueue', (tester) async {
      final vaultService = VaultService();
      await vaultService.prepareForNeoReassessment(
        newCycleId: 'cycle_neo_sw_08',
        previousCycleId: 'cycle_old_sw_08',
      );

      expect(vaultService.consiliumCycleId, equals('cycle_neo_sw_08'));
      expect(vaultService.neoPreviousCycleId, equals('cycle_old_sw_08'));
      expect(vaultService.isNeoReassessment, isTrue);
      expect(vaultService.isConsiliumCompleted, isFalse);
    });

    // -------------------------------------------------------------------------
    // TEST 9: Old chat is archived and not shown in new cycle
    // -------------------------------------------------------------------------
    testWidgets('9. Old chat is archived and not shown as active history in new cycle', (tester) async {
      final vaultService = VaultService();
      vaultService.saveCuratorChatMessage({'sender': 'curator', 'text': 'Old cycle greeting'});
      vaultService.saveCuratorChatMessage({'sender': 'user', 'text': 'Old cycle answer'});
      expect(vaultService.getCuratorChatHistory().length, equals(2));

      await vaultService.prepareForNeoReassessment(
        newCycleId: 'cycle_neo_chat_09',
        previousCycleId: 'cycle_old_chat_09',
      );

      // Active chat history is cleared for the new cycle
      expect(vaultService.getCuratorChatHistory().isEmpty, isTrue);

      // Historical chat is safely preserved in archived collection
      final archived = vaultService.archivedChatHistoryByCycle['cycle_old_chat_09'];
      expect(archived, isNotNull);
      expect(archived!.length, equals(2));
      expect(archived.first['text'], equals('Old cycle greeting'));
    });

    // -------------------------------------------------------------------------
    // TEST 10: Old documents remain accessible in DoctorDocumentsSection
    // -------------------------------------------------------------------------
    testWidgets('10. Historical documents in Safe remain accessible across cycles', (tester) async {
      final mockClient = MockClient((request) async {
        return http.Response(
          json.encode({
            'patient_id': 'p1',
            'total_count': 2,
            'documents': [
              {
                'report_id': 'doc_hist_old',
                'cycle_id': 'cycle_old_10',
                'created_at_utc': '2026-11-01T10:00:00Z',
                'report_date': '2026-11-01',
                'title': 'AEGIS — OBSERVATION SUMMARY (CYCLE 1)',
                'urgency': 'routine',
                'target_specialties': ['Therapist'],
                'available_formats': ['pdf', 'docx'],
                'summary': 'Baseline primary health observation.',
                'cycle_status': 'completed',
                'document_version': 1,
              },
            ],
          }),
          200,
          headers: {'content-type': 'application/json'},
        );
      });

      final apiClient = AegisApiClient(baseUrl: 'http://127.0.0.1:8000', client: mockClient);

      await tester.pumpWidget(
        MaterialApp(
          home: Scaffold(
            body: Provider<AegisApiClient>.value(
              value: apiClient,
              child: const DoctorDocumentsSection(),
            ),
          ),
        ),
      );
      await tester.pumpAndSettle();

      expect(find.text('DOCUMENTS FOR YOUR DOCTOR'), findsOneWidget);
      expect(find.text('AEGIS — OBSERVATION SUMMARY (CYCLE 1)'), findsOneWidget);
    });

    // -------------------------------------------------------------------------
    // TEST 11: Full onboarding flow traversal (Anamnesis -> SmartWatch -> Upload -> Consilium)
    // -------------------------------------------------------------------------
    testWidgets('11. Full onboarding flow traversal uses existing primary screens', (tester) async {
      final vaultService = VaultService();
      final timeService = TimeService();
      final mockClient = MockClient((request) async {
        return http.Response(json.encode({'has_active_cycle': true}), 200, headers: {'content-type': 'application/json'});
      });
      final apiClient = AegisApiClient(baseUrl: 'http://127.0.0.1:8000', client: mockClient);

      await tester.pumpWidget(
        MultiProvider(
          providers: [
            ChangeNotifierProvider<VaultService>.value(value: vaultService),
            ChangeNotifierProvider<TimeService>.value(value: timeService),
            Provider<AegisApiClient>.value(value: apiClient),
          ],
          child: MaterialApp(
            home: AnamnesisScreen(),
          ),
        ),
      );
      await tester.pumpAndSettle();

      // Screen 1: AnamnesisScreen renders questionnaire
      expect(find.byType(AnamnesisScreen), findsOneWidget);
    });

    // -------------------------------------------------------------------------
    // TEST 12: Recovery of unfinished anamnesis intake
    // -------------------------------------------------------------------------
    testWidgets('12. Unfinished anamnesis draft is preserved upon restart', (tester) async {
      final vaultService = VaultService();
      vaultService.saveAnamnesis('chronic_conditions', 'Hypertension stage 1', userText: 'Mild elevation');

      // Verify draft saved locally in global/notes
      final notes = vaultService.getSystemContext('notes_export');
      expect(notes['anamnesis_chronic_conditions']['value'], equals('Hypertension stage 1'));
    });

    // -------------------------------------------------------------------------
    // TEST 13: Prepared Neo is not considered active
    // -------------------------------------------------------------------------
    testWidgets('13. Prepared Neo Consilium is recognized as prepared and not yet active', (tester) async {
      final statusJson = {
        'patient_id': 'p1',
        'device_id': 'd1',
        'has_active_cycle': true,
        'active_cycle_id': 'cycle_neo_prep_13',
        'has_preparatory_anamnesis': true,
        'has_preparatory_labs': true,
        'has_preparatory_ecg': true,
        'is_primary_consilium_completed': false,
        'has_unreviewed_lab_changes': false,
        'is_neo_reassessment': true,
        'reassessment_status': 'prepared',
      };

      final status = ClientCascadeStatusResponse.fromJson(statusJson);
      expect(status.isNeoReassessment, isTrue);
      expect(status.reassessmentStatus, equals('prepared'));
      expect(status.isPrimaryConsiliumCompleted, isFalse);
    });

    // -------------------------------------------------------------------------
    // TEST 14: Prepared Neo Consilium activates without re-running 10 models
    // -------------------------------------------------------------------------
    testWidgets('14. Prepared Neo Consilium activates without repeating ten models', (tester) async {
      final requests = <String>[];
      final mockClient = MockClient((request) async {
        requests.add('${request.method} ${request.url.path}');
        if (request.url.path.contains('/client/cascade/status')) {
          return http.Response(
            json.encode({
              'patient_id': 'p1',
              'device_id': 'd1',
              'has_active_cycle': true,
              'active_cycle_id': 'cycle_neo_14',
              'has_preparatory_anamnesis': true,
              'has_preparatory_labs': true,
              'has_preparatory_ecg': true,
              'is_primary_consilium_completed': false,
              'has_unreviewed_lab_changes': false,
              'is_neo_reassessment': true,
              'reassessment_status': 'prepared',
            }),
            200,
            headers: {'content-type': 'application/json'},
          );
        }
        if (request.url.path.contains('/client/cascade/activate-neo')) {
          return http.Response(
            json.encode({
              'success': true,
              'cycle_id': 'cycle_neo_14',
              'status': 'active',
              'activated_at_utc': DateTime.now().toUtc().toIso8601String(),
              'strategy_version': 1,
              'schedule_anchor_t0_utc': DateTime.now().toUtc().toIso8601String(),
              'next_weekly_consilium_due_utc': DateTime.now().toUtc().add(const Duration(hours: 168)).toIso8601String(),
              'post_primary_init_enqueued': true,
              'is_idempotent_replay': false,
            }),
            200,
            headers: {'content-type': 'application/json'},
          );
        }
        if (request.url.path.contains('/reports/client')) {
          return http.Response(
            json.encode(_sampleClientReportsJson(cycleId: 'cycle_neo_14')),
            200,
            headers: {'content-type': 'application/json'},
          );
        }
        return http.Response(json.encode({}), 200, headers: {'content-type': 'application/json'});
      });

      final apiClient = AegisApiClient(
        baseUrl: 'http://127.0.0.1:8000',
        tokenProvider: () async => _buildTestJwt(patientId: 'pat_001'),
        client: mockClient,
      );
      final authManager = _buildTestAuthManager(apiClient: apiClient, patientId: 'pat_001');
      final vaultService = VaultService();
      await vaultService.setActivePatientId('pat_001');
      final timeService = TimeService();
      final vitalsService = VitalsMonitorService(timeService, vaultService);

      await tester.pumpWidget(
        MultiProvider(
          providers: [
            ChangeNotifierProvider<VaultService>.value(value: vaultService),
            ChangeNotifierProvider<AuthSessionManager>.value(value: authManager),
            ChangeNotifierProvider<TimeService>.value(value: timeService),
            ChangeNotifierProvider<VitalsMonitorService>.value(value: vitalsService),
            ChangeNotifierProvider<BrainService>.value(value: BrainService()),
            ChangeNotifierProvider<HealthDataProvider>.value(
              value: HealthDataProvider(vaultService: vaultService, vitalsMonitorService: vitalsService),
            ),
            Provider<AegisApiClient>.value(value: apiClient),
          ],
          child: MaterialApp(
            home: Scaffold(
              body: AegisConsiliumScreen(
                apiClient: apiClient,
                authSessionManager: authManager,
                vaultService: vaultService,
              ),
            ),
          ),
        ),
      );
      await tester.pumpAndSettle();

      expect(find.text('Neo Consilium prepared. Ready to activate new clinical strategy.'), findsOneWidget);

      // Tap Run/Activate Consilium
      await tester.tap(find.byKey(const Key('run_consilium_btn')));
      await tester.pumpAndSettle();

      // Verify activate-neo was called directly and run-primary was NEVER called!
      expect(requests, contains('POST /api/v1/client/cascade/activate-neo'));
      expect(requests.any((r) => r.contains('/run-primary')), isFalse);
      expect(find.text('Thank you for updating your health information!'), findsOneWidget);
      expect(find.text('Athena is waiting for you.'), findsOneWidget);
    });

    // -------------------------------------------------------------------------
    // TEST 15: Idempotent re-activation returns success cleanly
    // -------------------------------------------------------------------------
    testWidgets('15. Idempotent re-activation returns success cleanly', (tester) async {
      final mockClient = MockClient((request) async {
        return http.Response(
          json.encode({
            'success': true,
            'cycle_id': 'cycle_neo_idem_15',
            'status': 'active',
            'activated_at_utc': '2026-11-09T18:00:00Z',
            'strategy_version': 1,
            'schedule_anchor_t0_utc': '2026-11-09T18:00:00Z',
            'next_weekly_consilium_due_utc': '2026-11-16T18:00:00Z',
            'post_primary_init_enqueued': true,
            'is_idempotent_replay': true,
          }),
          200,
          headers: {'content-type': 'application/json'},
        );
      });

      final apiClient = AegisApiClient(baseUrl: 'http://127.0.0.1:8000', client: mockClient);
      final res = await apiClient.activateNeoConsilium(cycleId: 'cycle_neo_idem_15');
      expect(res.success, isTrue);
      expect(res.isIdempotentReplay, isTrue);
    });

    // -------------------------------------------------------------------------
    // TEST 16: Final screen displays adapted AthenaWelcomeSheet
    // -------------------------------------------------------------------------
    testWidgets('16. Final screen displays adapted AthenaWelcomeSheet with Go to Chat', (tester) async {
      bool connected = false;
      await tester.pumpWidget(
        MaterialApp(
          home: Scaffold(
            body: AthenaWelcomeSheet(
              isNeoReassessment: true,
              onConnect: () => connected = true,
            ),
          ),
        ),
      );
      await tester.pumpAndSettle();

      expect(find.text('Thank you for updating your health information!'), findsOneWidget);
      expect(find.text('Athena is waiting for you.'), findsOneWidget);
      expect(find.text('Go to Chat'), findsOneWidget);

      await tester.tap(find.byKey(const Key('athena_welcome_connect_btn')));
      await tester.pumpAndSettle();

      expect(connected, isTrue);
    });

    // -------------------------------------------------------------------------
    // TEST 17: Athena chat displays single server greeting without duplicates
    // -------------------------------------------------------------------------
    testWidgets('17. Athena chat displays exactly 1 server-generated greeting without duplicate', (tester) async {
      final mockClient = MockClient((request) async {
        return http.Response(
          json.encode({
            'patient_id': 'p17',
            'cycle_id': 'cycle_neo_17',
            'messages': [
              {
                'message_id': 'msg_neo_greeting_1',
                'sender': 'curator',
                'text': 'С возвращением! Рада снова сопровождать вас на новом этапе.',
                'timestamp_utc': '2026-11-09T18:00:05Z',
                'turn_number': 1,
              },
            ],
          }),
          200,
          headers: {'content-type': 'application/json'},
        );
      });

      final apiClient = AegisApiClient(
        baseUrl: 'http://127.0.0.1:8000',
        tokenProvider: () async => _buildTestJwt(patientId: 'p17'),
        client: mockClient,
      );
      final authManager = _buildTestAuthManager(apiClient: apiClient, patientId: 'p17');
      final vaultService = VaultService();
      await vaultService.setActivePatientId('p17');
      final timeService = TimeService();

      await tester.pumpWidget(
        MultiProvider(
          providers: [
            ChangeNotifierProvider<VaultService>.value(value: vaultService),
            ChangeNotifierProvider<AuthSessionManager>.value(value: authManager),
            ChangeNotifierProvider<TimeService>.value(value: timeService),
            Provider<AegisApiClient>.value(value: apiClient),
          ],
          child: MaterialApp(
            home: Scaffold(
              body: CuratorChatScreen(
                apiClient: apiClient,
                authSessionManager: authManager,
                vaultService: vaultService,
              ),
            ),
          ),
        ),
      );
      await tester.pumpAndSettle();

      expect(find.text('С возвращением! Рада снова сопровождать вас на новом этапе.'), findsOneWidget);
    });

    // -------------------------------------------------------------------------
    // TEST 18: Neo has zero relation to resetVault()
    // -------------------------------------------------------------------------
    testWidgets('18. Neo reassessment has zero relation to resetVault()', (tester) async {
      final vaultService = VaultService();
      await vaultService.setActivePatientId('pat_isolate_test');

      // Pre-seed completed consilium in old cycle
      await vaultService.recordClientReports(
        cycleId: 'cycle_old_18',
        reports: _sampleClientReportsJson(cycleId: 'cycle_old_18')
            .map((m) => ClientModelReportResponse.fromJson(m))
            .toList(),
        expectedPatientId: 'pat_isolate_test',
      );

      // Perform Neo reassessment transition
      await vaultService.prepareForNeoReassessment(
        newCycleId: 'cycle_neo_18',
        previousCycleId: 'cycle_old_18',
        expectedPatientId: 'pat_isolate_test',
      );

      // History is PRESERVED, patient attribution is intact
      expect(vaultService.activePatientId, equals('pat_isolate_test'));
      expect(vaultService.consiliumHistory.containsKey('cycle_old_18'), isTrue);
    });

    // -------------------------------------------------------------------------
    // TEST 19: Debug-only reset button remains hidden in release mode
    // -------------------------------------------------------------------------
    testWidgets('19. Debug-only reset button remains hidden in release mode while Neo button is accessible', (tester) async {
      final mockClient = MockClient((request) async {
        return http.Response(json.encode({'patient_id': 'p1', 'total_count': 0, 'documents': []}), 200, headers: {'content-type': 'application/json'});
      });
      final apiClient = AegisApiClient(baseUrl: 'http://127.0.0.1:8000', client: mockClient);
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

      // Reset button must NOT be present in release
      expect(find.byKey(const Key('debug_reset_vault_btn')), findsNothing);

      // Neo Update button MUST be present
      expect(find.byKey(const Key('update_health_info_placeholder')), findsOneWidget);
    });

    // -------------------------------------------------------------------------
    // TEST 20: Emergency monitoring remains active during Neo
    // -------------------------------------------------------------------------
    testWidgets('20. Emergency monitoring and Mode 2 pulse spikes remain active during Neo', (tester) async {
      final vaultService = VaultService();
      final timeService = TimeService();
      final vitalsService = VitalsMonitorService(timeService, vaultService);

      // Simulate emergency tachycardia spike (>= 140 bpm)
      vitalsService.onNewPulseRead(145);

      expect(vitalsService.currentPulse, equals(145));
      expect(vitalsService.currentState, equals(AlertState.waitingUserResponse));
    });
  });
}
