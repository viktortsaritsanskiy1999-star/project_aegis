// ignore_for_file: depend_on_referenced_packages
import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:provider/provider.dart';

import 'package:aegis_health/main.dart';
import 'package:aegis_health/services/aegis_cascade_controller.dart';
import 'package:aegis_health/services/api/aegis_api_client.dart';
import 'package:aegis_health/services/api/medical_ai_api_models.dart';
import 'package:aegis_health/services/athena_trigger_service.dart';
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

String _buildTestJwt({
  required String patientId,
  String deviceId = 'dev_test_lifecycle',
  List<String> scopes = const [
    'cascade:intake',
    'cascade:run',
    'cycle:read',
    'reports:read',
    'vitals:read',
    'vitals:write',
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

Map<String, Map<String, dynamic>> _sampleWeeklySystemReportsJson({
  required String cycleId,
  required String sprintId,
  String reportDate = '2026-11-02',
}) {
  const systems = [
    'cvs',
    'gi_tract',
    'respiratory',
    'nervous',
    'musculoskeletal',
    'kidneys',
    'immune',
    'endocrine',
  ];

  final Map<String, Map<String, dynamic>> map = {};
  for (final sys in systems) {
    map[sys] = {
      'report_id': 'rep_${cycleId}_${sprintId}_$sys',
      'cycle_id': cycleId,
      'sprint_id': sprintId,
      'model': 'gemini-3.6-flash',
      'report_type': 'system_user_report_$sys',
      'privacy_level': 'client_facing',
      'version': 1,
      'content': {
        'title': 'Weekly ${sys.toUpperCase()} Synthesis',
        'summary': 'All vital trends for $sys remain stable.',
        'report_date': reportDate,
        'system_key': sys,
        'has_changes': false,
        'highlights': ['Condition stable under active therapy.'],
        'entries': [],
      },
      'created_at': '${reportDate}T10:00:00Z',
    };
  }
  return map;
}

Map<String, dynamic> _sampleWeeklyResponseJson({
  required String cycleId,
  required String sprintId,
  int sprintNumber = 1,
  String reportDate = '2026-11-02',
}) {
  return {
    'cycle_id': cycleId,
    'sprint_id': sprintId,
    'sprint_number': sprintNumber,
    'report_date': reportDate,
    'overall_report': {
      'report_id': 'rep_${cycleId}_${sprintId}_overall',
      'cycle_id': cycleId,
      'sprint_id': sprintId,
      'model': 'gemini-3.1-pro',
      'report_type': 'weekly_user_overall_report',
      'privacy_level': 'client_facing',
      'version': 1,
      'content': {
        'title': 'Weekly Consilium Summary ($sprintId)',
        'summary': 'Overall hemodynamics and organ systems are well-controlled.',
        'report_date': reportDate,
        'highlights': ['Weekly targets achieved.'],
        'metrics': {
          'sprint_number': sprintNumber,
          'target_zone_min': 110,
          'target_zone_max': 150,
        },
      },
      'created_at': '${reportDate}T10:00:00Z',
    },
    'system_reports': _sampleWeeklySystemReportsJson(
      cycleId: cycleId,
      sprintId: sprintId,
      reportDate: reportDate,
    ),
    'created_at': '${reportDate}T10:00:00Z',
  };
}

List<Map<String, dynamic>> _samplePrimaryReportsJson({required String cycleId}) {
  const systems = [
    'cvs',
    'gi_tract',
    'respiratory',
    'nervous',
    'musculoskeletal',
    'kidneys',
    'immune',
    'endocrine',
  ];
  final list = <Map<String, dynamic>>[
    {
      'report_id': 'rep_primary_${cycleId}_overall',
      'cycle_id': cycleId,
      'sprint_id': null,
      'model': 'gemini-3.1-pro',
      'report_type': 'user_overall_report',
      'privacy_level': 'client_facing',
      'version': 1,
      'content': {
        'title': 'Primary Consilium Synthesis',
        'summary': 'Primary Consilium completed with initial baselines.',
        'report_date': '2026-10-25',
        'highlights': ['Baseline established.'],
        'metrics': {
          'target_zone_min': 110,
          'target_zone_max': 150,
          'max_spike_delta_bpm': 30,
        },
      },
      'created_at': '2026-10-25T10:00:00Z',
    }
  ];
  for (final sys in systems) {
    list.add({
      'report_id': 'rep_primary_${cycleId}_$sys',
      'cycle_id': cycleId,
      'sprint_id': null,
      'model': 'gemini-3.6-flash',
      'report_type': 'system_user_report_$sys',
      'privacy_level': 'client_facing',
      'version': 1,
      'content': {
        'title': 'Primary $sys Review',
        'summary': 'Primary assessment for $sys normal.',
        'report_date': '2026-10-25',
        'system_key': sys,
        'has_changes': false,
        'highlights': [],
        'entries': [],
      },
      'created_at': '2026-10-25T10:00:00Z',
    });
  }
  return list;
}

Widget _buildAtlasTestApp({
  required AegisApiClient apiClient,
  required AuthSessionManager authManager,
  required VaultService vaultService,
  TimeService? timeService,
  BrainService? brainService,
  VitalsMonitorService? vitalsService,
  HealthDataProvider? healthProvider,
}) {
  final time = timeService ?? TimeService();
  final brain = brainService ?? BrainService();
  final vitals = vitalsService ?? VitalsMonitorService(time, vaultService);
  final health = healthProvider ?? HealthDataProvider(vaultService: vaultService, vitalsMonitorService: vitals);

  // Suppress Athena welcome popup in widget tests
  vaultService.updateSystemFolder('global', {'athena_welcomed': true});

  return MultiProvider(
    providers: [
      ChangeNotifierProvider<VaultService>.value(value: vaultService),
      ChangeNotifierProvider<AuthSessionManager>.value(value: authManager),
      Provider<AegisApiClient>.value(value: apiClient),
      ChangeNotifierProvider<TimeService>.value(value: time),
      ChangeNotifierProvider<BrainService>.value(value: brain),
      ChangeNotifierProvider<VitalsMonitorService>.value(value: vitals),
      ChangeNotifierProvider<HealthDataProvider>.value(value: health),
      Provider<AthenaTriggerService>(create: (_) => AthenaTriggerService()),
      ChangeNotifierProxyProvider<VaultService, AegisCascadeController>(
        create: (context) => AegisCascadeController(vaultService: vaultService),
        update: (context, vault, prev) => prev ?? AegisCascadeController(vaultService: vault),
      ),
      ChangeNotifierProvider<VitalsSyncQueue>(
        create: (context) => VitalsSyncQueue(
          apiClient: apiClient,
          authSessionManager: authManager,
        ),
      ),
    ],
    child: const MaterialApp(
      home: AtlasScreen(),
    ),
  );
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
  });

  group('PROJECT AEGIS — ARGON BUG-02: WEEKLY SPRINT LIFECYCLE INTEGRATION', () {
    // -------------------------------------------------------------------------
    // 1. Week 1 availability after 168 hours
    // -------------------------------------------------------------------------
    testWidgets('1. Week 1 availability after 168 hours: user starts Week 1 and passes exact server next_sprint_id', (tester) async {
      final token = _buildTestJwt(patientId: 'patient_w1_ready');
      int runWeeklyCalls = 0;
      String? capturedSprintId;
      String? capturedIdempotencyKey;

      final mockClient = MockClient((request) async {
        if (request.url.path == '/api/v1/client/cascade/status') {
          return http.Response(
            json.encode({
              'patient_id': 'patient_w1_ready',
              'device_id': 'dev_test_lifecycle',
              'has_active_cycle': true,
              'active_cycle_id': 'cycle_w1_ready',
              'has_preparatory_anamnesis': true,
              'has_preparatory_labs': true,
              'has_preparatory_ecg': true,
              'is_primary_consilium_completed': true,
              'has_unreviewed_lab_changes': false,
              'can_start_next_sprint': true,
              'next_sprint_id': 'sprint_cycle_w1_ready_w1',
              'current_weekly_sprint_id': null,
              'weekly_sprint_number': null,
              'is_weekly_consilium_completed': false,
            }),
            200,
            headers: {'content-type': 'application/json'},
          );
        }
        if (request.url.path == '/api/v1/reports/client') {
          return http.Response(
            json.encode(_samplePrimaryReportsJson(cycleId: 'cycle_w1_ready')),
            200,
            headers: {'content-type': 'application/json'},
          );
        }
        if (request.url.path == '/api/v1/client/cascade/run-weekly') {
          runWeeklyCalls++;
          final body = json.decode(request.body) as Map<String, dynamic>;
          capturedSprintId = body['sprint_id'] as String?;
          capturedIdempotencyKey = request.headers['Idempotency-Key'];
          return http.Response(
            json.encode(_sampleWeeklyResponseJson(
              cycleId: 'cycle_w1_ready',
              sprintId: 'sprint_cycle_w1_ready_w1',
              sprintNumber: 1,
            )),
            200,
            headers: {'content-type': 'application/json'},
          );
        }
        return http.Response('Not found', 404);
      });

      final apiClient = AegisApiClient(
        baseUrl: 'http://127.0.0.1:8000',
        client: mockClient,
        tokenProvider: () async => token,
      );
      final authManager = AuthSessionManager(
        apiClient: apiClient,
        storage: memoryAuthStorage,
      );
      authManager.setSessionForTesting(
        AuthSession(
          accessToken: token,
          refreshToken: 'rf_w1',
          expiresIn: 3600,
          expiresAt: DateTime.now().toUtc().add(const Duration(hours: 1)),
          refreshExpiresAt: DateTime.now().toUtc().add(const Duration(days: 30)),
          patientId: 'patient_w1_ready',
          deviceId: 'dev_test_lifecycle',
          scope: 'cascade:run cycle:read reports:read',
        ),
        status: AuthSessionStatus.authenticated,
      );

      final vault = VaultService(storage: memoryVaultStorage);
      await vault.initFuture;
      await vault.setActivePatientId('patient_w1_ready');

      // Populate Primary Consilium in vault
      await vault.recordClientReports(
        cycleId: 'cycle_w1_ready',
        reports: _samplePrimaryReportsJson(cycleId: 'cycle_w1_ready')
            .map((j) => ClientModelReportResponse.fromJson(j))
            .toList(),
        expectedPatientId: 'patient_w1_ready',
      );

      await tester.pumpWidget(
        _buildAtlasTestApp(
          apiClient: apiClient,
          authManager: authManager,
          vaultService: vault,
        ),
      );
      await tester.pumpAndSettle();

      // Open consilium summary dialog
      expect(find.byKey(const Key('consilium_status_ready')), findsOneWidget);
      await tester.tap(find.byKey(const Key('consilium_status_ready')));
      await tester.pumpAndSettle();

      // Verify launch button is available
      final runBtn = find.byKey(const Key('run_weekly_consilium_btn'));
      expect(runBtn, findsOneWidget);
      await tester.tap(runBtn);
      await tester.pumpAndSettle();

      expect(runWeeklyCalls, equals(1));
      expect(capturedSprintId, equals('sprint_cycle_w1_ready_w1'));
      expect(capturedIdempotencyKey, equals('atlas_weekly_cycle_w1_ready_w1'));
      expect(vault.isWeeklyConsiliumCompleted, isTrue);
      expect(vault.activeWeeklySprintId, equals('sprint_cycle_w1_ready_w1'));
    });

    // -------------------------------------------------------------------------
    // 2. Early launch blocked (Week 1 before 168h boundary)
    // -------------------------------------------------------------------------
    testWidgets('2. Early launch blocked (Week 1): 0 calls made when 168h boundary not elapsed', (tester) async {
      final token = _buildTestJwt(patientId: 'patient_w1_early');
      int runWeeklyCalls = 0;

      final mockClient = MockClient((request) async {
        if (request.url.path == '/api/v1/client/cascade/status') {
          return http.Response(
            json.encode({
              'patient_id': 'patient_w1_early',
              'device_id': 'dev_test_lifecycle',
              'has_active_cycle': true,
              'active_cycle_id': 'cycle_w1_early',
              'has_preparatory_anamnesis': true,
              'has_preparatory_labs': true,
              'has_preparatory_ecg': true,
              'is_primary_consilium_completed': true,
              'has_unreviewed_lab_changes': false,
              'can_start_next_sprint': false, // Early! Boundary not elapsed yet!
              'next_sprint_id': null,
              'current_weekly_sprint_id': null,
              'weekly_sprint_number': null,
              'is_weekly_consilium_completed': false,
            }),
            200,
            headers: {'content-type': 'application/json'},
          );
        }
        if (request.url.path == '/api/v1/reports/client') {
          return http.Response(
            json.encode(_samplePrimaryReportsJson(cycleId: 'cycle_w1_early')),
            200,
            headers: {'content-type': 'application/json'},
          );
        }
        if (request.url.path == '/api/v1/client/cascade/run-weekly') {
          runWeeklyCalls++;
          fail('run-weekly must NEVER be called when can_start_next_sprint is false!');
        }
        return http.Response('Not found', 404);
      });

      final apiClient = AegisApiClient(
        baseUrl: 'http://127.0.0.1:8000',
        client: mockClient,
        tokenProvider: () async => token,
      );
      final authManager = AuthSessionManager(
        apiClient: apiClient,
        storage: memoryAuthStorage,
      );
      authManager.setSessionForTesting(
        AuthSession(
          accessToken: token,
          refreshToken: 'rf_w1_early',
          expiresIn: 3600,
          expiresAt: DateTime.now().toUtc().add(const Duration(hours: 1)),
          refreshExpiresAt: DateTime.now().toUtc().add(const Duration(days: 30)),
          patientId: 'patient_w1_early',
          deviceId: 'dev_test_lifecycle',
          scope: 'cascade:run cycle:read reports:read',
        ),
        status: AuthSessionStatus.authenticated,
      );

      final vault = VaultService(storage: memoryVaultStorage);
      await vault.initFuture;
      await vault.setActivePatientId('patient_w1_early');

      await vault.recordClientReports(
        cycleId: 'cycle_w1_early',
        reports: _samplePrimaryReportsJson(cycleId: 'cycle_w1_early')
            .map((j) => ClientModelReportResponse.fromJson(j))
            .toList(),
        expectedPatientId: 'patient_w1_early',
      );

      await tester.pumpWidget(
        _buildAtlasTestApp(
          apiClient: apiClient,
          authManager: authManager,
          vaultService: vault,
        ),
      );
      await tester.pumpAndSettle();

      // Open dialog
      await tester.tap(find.byKey(const Key('consilium_status_ready')));
      await tester.pumpAndSettle();

      // Tap launch button
      await tester.tap(find.byKey(const Key('run_weekly_consilium_btn')));
      await tester.pumpAndSettle();

      // Zero POST calls made
      expect(runWeeklyCalls, equals(0));
      expect(vault.isWeeklyConsiliumCompleted, isFalse);

      // Re-open dialog to verify error message explaining early launch blocked
      await tester.tap(find.byKey(const Key('consilium_status_ready')));
      await tester.pumpAndSettle();

      expect(find.textContaining('Early launch blocked'), findsOneWidget);
    });

    // -------------------------------------------------------------------------
    // 3. Early launch blocked (Week 2+ before boundary)
    // -------------------------------------------------------------------------
    testWidgets('3. Early launch blocked (Week 2+): next sprint blocked before its boundary', (tester) async {
      final token = _buildTestJwt(patientId: 'patient_w2_early');
      int runWeeklyCalls = 0;

      final mockClient = MockClient((request) async {
        if (request.url.path == '/api/v1/client/cascade/status') {
          return http.Response(
            json.encode({
              'patient_id': 'patient_w2_early',
              'device_id': 'dev_test_lifecycle',
              'has_active_cycle': true,
              'active_cycle_id': 'cycle_w2_early',
              'has_preparatory_anamnesis': true,
              'has_preparatory_labs': true,
              'has_preparatory_ecg': true,
              'is_primary_consilium_completed': true,
              'has_unreviewed_lab_changes': false,
              'is_weekly_consilium_completed': true,
              'current_weekly_sprint_id': 'sprint_cycle_w2_early_w1',
              'weekly_sprint_number': 1,
              'can_start_next_sprint': false, // Week 2 boundary not reached yet!
              'next_sprint_id': null,
            }),
            200,
            headers: {'content-type': 'application/json'},
          );
        }
        if (request.url.path == '/api/v1/reports/client') {
          final w1Resp = _sampleWeeklyResponseJson(
            cycleId: 'cycle_w2_early',
            sprintId: 'sprint_cycle_w2_early_w1',
            sprintNumber: 1,
          );
          return http.Response(
            json.encode([
              ..._samplePrimaryReportsJson(cycleId: 'cycle_w2_early'),
              w1Resp['overall_report'],
              ...((w1Resp['system_reports'] as Map).values),
            ]),
            200,
            headers: {'content-type': 'application/json'},
          );
        }
        if (request.url.path == '/api/v1/client/cascade/run-weekly') {
          runWeeklyCalls++;
          fail('run-weekly must NEVER be called when Week 2 boundary has not elapsed!');
        }
        return http.Response('Not found', 404);
      });

      final apiClient = AegisApiClient(
        baseUrl: 'http://127.0.0.1:8000',
        client: mockClient,
        tokenProvider: () async => token,
      );
      final authManager = AuthSessionManager(
        apiClient: apiClient,
        storage: memoryAuthStorage,
      );
      authManager.setSessionForTesting(
        AuthSession(
          accessToken: token,
          refreshToken: 'rf_w2_early',
          expiresIn: 3600,
          expiresAt: DateTime.now().toUtc().add(const Duration(hours: 1)),
          refreshExpiresAt: DateTime.now().toUtc().add(const Duration(days: 30)),
          patientId: 'patient_w2_early',
          deviceId: 'dev_test_lifecycle',
          scope: 'cascade:run cycle:read reports:read',
        ),
        status: AuthSessionStatus.authenticated,
      );

      final vault = VaultService(storage: memoryVaultStorage);
      await vault.initFuture;
      await vault.setActivePatientId('patient_w2_early');

      // Record Week 1
      final w1Data = WeeklyConsiliumClientResponse.fromJson(_sampleWeeklyResponseJson(
        cycleId: 'cycle_w2_early',
        sprintId: 'sprint_cycle_w2_early_w1',
        sprintNumber: 1,
      ));
      await vault.recordWeeklyClientReports(
        cycleId: w1Data.cycleId,
        sprintId: w1Data.sprintId,
        sprintNumber: w1Data.sprintNumber,
        reportDate: w1Data.reportDate,
        overallReport: w1Data.overallReport,
        systemReports: w1Data.systemReports,
        expectedPatientId: 'patient_w2_early',
      );

      await tester.pumpWidget(
        _buildAtlasTestApp(
          apiClient: apiClient,
          authManager: authManager,
          vaultService: vault,
        ),
      );
      await tester.pumpAndSettle();

      await tester.tap(find.byKey(const Key('consilium_status_ready')));
      await tester.pumpAndSettle();

      // Tap СЛЕДУЮЩИЙ СПРИНТ
      await tester.tap(find.byKey(const Key('next_weekly_consilium_btn')));
      await tester.pumpAndSettle();

      expect(runWeeklyCalls, equals(0));
      expect(vault.activeWeeklySprintId, equals('sprint_cycle_w2_early_w1'));

      // Re-open dialog to verify error explanation
      await tester.tap(find.byKey(const Key('consilium_status_ready')));
      await tester.pumpAndSettle();

      expect(find.textContaining('Early launch blocked'), findsOneWidget);
    });

    // -------------------------------------------------------------------------
    // 4. Week 2+ availability after its boundary
    // -------------------------------------------------------------------------
    testWidgets('4. Week 2+ availability after its boundary: launches Week 2 with server next_sprint_id', (tester) async {
      final token = _buildTestJwt(patientId: 'patient_w2_ready');
      int runWeeklyCalls = 0;
      String? capturedSprintId;
      String? capturedIdempotencyKey;

      final mockClient = MockClient((request) async {
        if (request.url.path == '/api/v1/client/cascade/status') {
          return http.Response(
            json.encode({
              'patient_id': 'patient_w2_ready',
              'device_id': 'dev_test_lifecycle',
              'has_active_cycle': true,
              'active_cycle_id': 'cycle_w2_ready',
              'has_preparatory_anamnesis': true,
              'has_preparatory_labs': true,
              'has_preparatory_ecg': true,
              'is_primary_consilium_completed': true,
              'has_unreviewed_lab_changes': false,
              'is_weekly_consilium_completed': true,
              'current_weekly_sprint_id': 'sprint_cycle_w2_ready_w1',
              'weekly_sprint_number': 1,
              'can_start_next_sprint': true,
              'next_sprint_id': 'sprint_cycle_w2_ready_w2',
            }),
            200,
            headers: {'content-type': 'application/json'},
          );
        }
        if (request.url.path == '/api/v1/reports/client') {
          final w1Resp = _sampleWeeklyResponseJson(
            cycleId: 'cycle_w2_ready',
            sprintId: 'sprint_cycle_w2_ready_w1',
            sprintNumber: 1,
          );
          return http.Response(
            json.encode([
              ..._samplePrimaryReportsJson(cycleId: 'cycle_w2_ready'),
              w1Resp['overall_report'],
              ...((w1Resp['system_reports'] as Map).values),
            ]),
            200,
            headers: {'content-type': 'application/json'},
          );
        }
        if (request.url.path == '/api/v1/client/cascade/run-weekly') {
          runWeeklyCalls++;
          final body = json.decode(request.body) as Map<String, dynamic>;
          capturedSprintId = body['sprint_id'] as String?;
          capturedIdempotencyKey = request.headers['Idempotency-Key'];
          return http.Response(
            json.encode(_sampleWeeklyResponseJson(
              cycleId: 'cycle_w2_ready',
              sprintId: 'sprint_cycle_w2_ready_w2',
              sprintNumber: 2,
            )),
            200,
            headers: {'content-type': 'application/json'},
          );
        }
        return http.Response('Not found', 404);
      });

      final apiClient = AegisApiClient(
        baseUrl: 'http://127.0.0.1:8000',
        client: mockClient,
        tokenProvider: () async => token,
      );
      final authManager = AuthSessionManager(
        apiClient: apiClient,
        storage: memoryAuthStorage,
      );
      authManager.setSessionForTesting(
        AuthSession(
          accessToken: token,
          refreshToken: 'rf_w2_ready',
          expiresIn: 3600,
          expiresAt: DateTime.now().toUtc().add(const Duration(hours: 1)),
          refreshExpiresAt: DateTime.now().toUtc().add(const Duration(days: 30)),
          patientId: 'patient_w2_ready',
          deviceId: 'dev_test_lifecycle',
          scope: 'cascade:run cycle:read reports:read',
        ),
        status: AuthSessionStatus.authenticated,
      );

      final vault = VaultService(storage: memoryVaultStorage);
      await vault.initFuture;
      await vault.setActivePatientId('patient_w2_ready');

      // Record Week 1 in Vault
      final w1Data = WeeklyConsiliumClientResponse.fromJson(_sampleWeeklyResponseJson(
        cycleId: 'cycle_w2_ready',
        sprintId: 'sprint_cycle_w2_ready_w1',
        sprintNumber: 1,
      ));
      await vault.recordWeeklyClientReports(
        cycleId: w1Data.cycleId,
        sprintId: w1Data.sprintId,
        sprintNumber: w1Data.sprintNumber,
        reportDate: w1Data.reportDate,
        overallReport: w1Data.overallReport,
        systemReports: w1Data.systemReports,
        expectedPatientId: 'patient_w2_ready',
      );

      await tester.pumpWidget(
        _buildAtlasTestApp(
          apiClient: apiClient,
          authManager: authManager,
          vaultService: vault,
        ),
      );
      await tester.pumpAndSettle();

      await tester.tap(find.byKey(const Key('consilium_status_ready')));
      await tester.pumpAndSettle();

      // Tap СЛЕДУЮЩИЙ СПРИНТ
      await tester.tap(find.byKey(const Key('next_weekly_consilium_btn')));
      await tester.pumpAndSettle();

      expect(runWeeklyCalls, equals(1));
      expect(capturedSprintId, equals('sprint_cycle_w2_ready_w2'));
      expect(capturedIdempotencyKey, equals('atlas_weekly_cycle_w2_ready_w2'));
      expect(vault.activeWeeklySprintId, equals('sprint_cycle_w2_ready_w2'));
      expect(vault.activeWeeklySprintNumber, equals(2));
    });

    // -------------------------------------------------------------------------
    // 5. Missing optional fields fail closed
    // -------------------------------------------------------------------------
    test('5. Missing optional fields fail closed: ClientCascadeStatusResponse defaults to false and null', () {
      final minimalJson = {
        'patient_id': 'p_minimal',
        'device_id': 'd_minimal',
        'has_active_cycle': true,
        'active_cycle_id': 'c_minimal',
        'is_primary_consilium_completed': true,
      };

      final parsed = ClientCascadeStatusResponse.fromJson(minimalJson);

      expect(parsed.canStartNextSprint, isFalse);
      expect(parsed.nextSprintId, isNull);
      expect(parsed.currentWeeklySprintId, isNull);
      expect(parsed.weeklySprintNumber, isNull);
      expect(parsed.isWeeklyConsiliumCompleted, isFalse);
    });

    // -------------------------------------------------------------------------
    // 6. Correct sprint_id passed to server without guessing
    // -------------------------------------------------------------------------
    testWidgets('6. Correct sprint_id passed to server: uses exact server next_sprint_id without guessing', (tester) async {
      final token = _buildTestJwt(patientId: 'patient_custom_auth');
      String? sentSprintId;

      final mockClient = MockClient((request) async {
        if (request.url.path == '/api/v1/client/cascade/status') {
          return http.Response(
            json.encode({
              'patient_id': 'patient_custom_auth',
              'device_id': 'dev_test_lifecycle',
              'has_active_cycle': true,
              'active_cycle_id': 'cycle_custom',
              'has_preparatory_anamnesis': true,
              'has_preparatory_labs': true,
              'has_preparatory_ecg': true,
              'is_primary_consilium_completed': true,
              'has_unreviewed_lab_changes': false,
              'can_start_next_sprint': true,
              'next_sprint_id': 'sprint_server_authority_custom_42',
            }),
            200,
            headers: {'content-type': 'application/json'},
          );
        }
        if (request.url.path == '/api/v1/reports/client') {
          return http.Response(
            json.encode(_samplePrimaryReportsJson(cycleId: 'cycle_custom')),
            200,
            headers: {'content-type': 'application/json'},
          );
        }
        if (request.url.path == '/api/v1/client/cascade/run-weekly') {
          final body = json.decode(request.body) as Map<String, dynamic>;
          sentSprintId = body['sprint_id'] as String?;
          return http.Response(
            json.encode(_sampleWeeklyResponseJson(
              cycleId: 'cycle_custom',
              sprintId: 'sprint_server_authority_custom_42',
              sprintNumber: 1,
            )),
            200,
            headers: {'content-type': 'application/json'},
          );
        }
        return http.Response('Not found', 404);
      });

      final apiClient = AegisApiClient(
        baseUrl: 'http://127.0.0.1:8000',
        client: mockClient,
        tokenProvider: () async => token,
      );
      final authManager = AuthSessionManager(
        apiClient: apiClient,
        storage: memoryAuthStorage,
      );
      authManager.setSessionForTesting(
        AuthSession(
          accessToken: token,
          refreshToken: 'rf_custom',
          expiresIn: 3600,
          expiresAt: DateTime.now().toUtc().add(const Duration(hours: 1)),
          refreshExpiresAt: DateTime.now().toUtc().add(const Duration(days: 30)),
          patientId: 'patient_custom_auth',
          deviceId: 'dev_test_lifecycle',
          scope: 'cascade:run cycle:read reports:read',
        ),
        status: AuthSessionStatus.authenticated,
      );

      final vault = VaultService(storage: memoryVaultStorage);
      await vault.initFuture;
      await vault.setActivePatientId('patient_custom_auth');

      await vault.recordClientReports(
        cycleId: 'cycle_custom',
        reports: _samplePrimaryReportsJson(cycleId: 'cycle_custom')
            .map((j) => ClientModelReportResponse.fromJson(j))
            .toList(),
        expectedPatientId: 'patient_custom_auth',
      );

      await tester.pumpWidget(
        _buildAtlasTestApp(
          apiClient: apiClient,
          authManager: authManager,
          vaultService: vault,
        ),
      );
      await tester.pumpAndSettle();

      await tester.tap(find.byKey(const Key('consilium_status_ready')));
      await tester.pumpAndSettle();

      await tester.tap(find.byKey(const Key('run_weekly_consilium_btn')));
      await tester.pumpAndSettle();

      expect(sentSprintId, equals('sprint_server_authority_custom_42'));
      expect(vault.activeWeeklySprintId, equals('sprint_server_authority_custom_42'));
    });

    // -------------------------------------------------------------------------
    // 7. Completed sprint replay
    // -------------------------------------------------------------------------
    testWidgets('7. Completed sprint replay: replaying completed sprint allowed even when can_start_next_sprint is false', (tester) async {
      final token = _buildTestJwt(patientId: 'patient_replay');
      int runWeeklyCalls = 0;
      String? sentSprintId;

      final mockClient = MockClient((request) async {
        if (request.url.path == '/api/v1/client/cascade/status') {
          return http.Response(
            json.encode({
              'patient_id': 'patient_replay',
              'device_id': 'dev_test_lifecycle',
              'has_active_cycle': true,
              'active_cycle_id': 'cycle_replay',
              'has_preparatory_anamnesis': true,
              'has_preparatory_labs': true,
              'has_preparatory_ecg': true,
              'is_primary_consilium_completed': true,
              'has_unreviewed_lab_changes': false,
              'is_weekly_consilium_completed': true,
              'current_weekly_sprint_id': 'sprint_cycle_replay_w1',
              'weekly_sprint_number': 1,
              'can_start_next_sprint': false, // Next sprint not ready, but replay MUST work!
              'next_sprint_id': null,
            }),
            200,
            headers: {'content-type': 'application/json'},
          );
        }
        if (request.url.path == '/api/v1/reports/client') {
          final w1Resp = _sampleWeeklyResponseJson(
            cycleId: 'cycle_replay',
            sprintId: 'sprint_cycle_replay_w1',
            sprintNumber: 1,
          );
          return http.Response(
            json.encode([
              ..._samplePrimaryReportsJson(cycleId: 'cycle_replay'),
              w1Resp['overall_report'],
              ...((w1Resp['system_reports'] as Map).values),
            ]),
            200,
            headers: {'content-type': 'application/json'},
          );
        }
        if (request.url.path == '/api/v1/client/cascade/run-weekly') {
          runWeeklyCalls++;
          final body = json.decode(request.body) as Map<String, dynamic>;
          sentSprintId = body['sprint_id'] as String?;
          // Server returns recovered replay results immediately
          return http.Response(
            json.encode(_sampleWeeklyResponseJson(
              cycleId: 'cycle_replay',
              sprintId: 'sprint_cycle_replay_w1',
              sprintNumber: 1,
            )),
            200,
            headers: {'content-type': 'application/json'},
          );
        }
        return http.Response('Not found', 404);
      });

      final apiClient = AegisApiClient(
        baseUrl: 'http://127.0.0.1:8000',
        client: mockClient,
        tokenProvider: () async => token,
      );
      final authManager = AuthSessionManager(
        apiClient: apiClient,
        storage: memoryAuthStorage,
      );
      authManager.setSessionForTesting(
        AuthSession(
          accessToken: token,
          refreshToken: 'rf_replay',
          expiresIn: 3600,
          expiresAt: DateTime.now().toUtc().add(const Duration(hours: 1)),
          refreshExpiresAt: DateTime.now().toUtc().add(const Duration(days: 30)),
          patientId: 'patient_replay',
          deviceId: 'dev_test_lifecycle',
          scope: 'cascade:run cycle:read reports:read',
        ),
        status: AuthSessionStatus.authenticated,
      );

      final vault = VaultService(storage: memoryVaultStorage);
      await vault.initFuture;
      await vault.setActivePatientId('patient_replay');

      // Record completed Week 1 in Vault
      final w1Data = WeeklyConsiliumClientResponse.fromJson(_sampleWeeklyResponseJson(
        cycleId: 'cycle_replay',
        sprintId: 'sprint_cycle_replay_w1',
        sprintNumber: 1,
      ));
      await vault.recordWeeklyClientReports(
        cycleId: w1Data.cycleId,
        sprintId: w1Data.sprintId,
        sprintNumber: w1Data.sprintNumber,
        reportDate: w1Data.reportDate,
        overallReport: w1Data.overallReport,
        systemReports: w1Data.systemReports,
        expectedPatientId: 'patient_replay',
      );

      await tester.pumpWidget(
        _buildAtlasTestApp(
          apiClient: apiClient,
          authManager: authManager,
          vaultService: vault,
        ),
      );
      await tester.pumpAndSettle();

      await tester.tap(find.byKey(const Key('consilium_status_ready')));
      await tester.pumpAndSettle();

      // Tap ПОВТОРИТЬ WEEKLY
      final retryBtn = find.byKey(const Key('retry_weekly_consilium_btn'));
      expect(retryBtn, findsOneWidget);
      await tester.tap(retryBtn);
      await tester.pumpAndSettle();

      // Replay executed successfully despite can_start_next_sprint == false
      expect(runWeeklyCalls, equals(1));
      expect(sentSprintId, equals('sprint_cycle_replay_w1'));
      expect(vault.activeWeeklySprintId, equals('sprint_cycle_replay_w1'));
    });

    // -------------------------------------------------------------------------
    // 8. Patient/cycle switch during request
    // -------------------------------------------------------------------------
    testWidgets('8. Patient/cycle switch during request: in-flight response discarded and epoch-fenced', (tester) async {
      final token = _buildTestJwt(patientId: 'patient_switch_src');
      late VaultService vault;
      int runWeeklyCalls = 0;

      final mockClient = MockClient((request) async {
        if (request.url.path == '/api/v1/client/cascade/status') {
          return http.Response(
            json.encode({
              'patient_id': 'patient_switch_src',
              'device_id': 'dev_test_lifecycle',
              'has_active_cycle': true,
              'active_cycle_id': 'cycle_switch_src',
              'has_preparatory_anamnesis': true,
              'has_preparatory_labs': true,
              'has_preparatory_ecg': true,
              'is_primary_consilium_completed': true,
              'has_unreviewed_lab_changes': false,
              'can_start_next_sprint': true,
              'next_sprint_id': 'sprint_cycle_switch_src_w1',
            }),
            200,
            headers: {'content-type': 'application/json'},
          );
        }
        if (request.url.path == '/api/v1/reports/client') {
          return http.Response(
            json.encode(_samplePrimaryReportsJson(cycleId: 'cycle_switch_src')),
            200,
            headers: {'content-type': 'application/json'},
          );
        }
        if (request.url.path == '/api/v1/client/cascade/run-weekly') {
          runWeeklyCalls++;
          // Simulate patient switch while in-flight
          await vault.setActivePatientId('patient_switch_target');
          return http.Response(
            json.encode(_sampleWeeklyResponseJson(
              cycleId: 'cycle_switch_src',
              sprintId: 'sprint_cycle_switch_src_w1',
              sprintNumber: 1,
            )),
            200,
            headers: {'content-type': 'application/json'},
          );
        }
        return http.Response('Not found', 404);
      });

      final apiClient = AegisApiClient(
        baseUrl: 'http://127.0.0.1:8000',
        client: mockClient,
        tokenProvider: () async => token,
      );
      final authManager = AuthSessionManager(
        apiClient: apiClient,
        storage: memoryAuthStorage,
      );
      authManager.setSessionForTesting(
        AuthSession(
          accessToken: token,
          refreshToken: 'rf_switch',
          expiresIn: 3600,
          expiresAt: DateTime.now().toUtc().add(const Duration(hours: 1)),
          refreshExpiresAt: DateTime.now().toUtc().add(const Duration(days: 30)),
          patientId: 'patient_switch_src',
          deviceId: 'dev_test_lifecycle',
          scope: 'cascade:run cycle:read reports:read',
        ),
        status: AuthSessionStatus.authenticated,
      );

      vault = VaultService(storage: memoryVaultStorage);
      await vault.initFuture;
      await vault.setActivePatientId('patient_switch_src');

      await vault.recordClientReports(
        cycleId: 'cycle_switch_src',
        reports: _samplePrimaryReportsJson(cycleId: 'cycle_switch_src')
            .map((j) => ClientModelReportResponse.fromJson(j))
            .toList(),
        expectedPatientId: 'patient_switch_src',
      );

      await tester.pumpWidget(
        _buildAtlasTestApp(
          apiClient: apiClient,
          authManager: authManager,
          vaultService: vault,
        ),
      );
      await tester.pumpAndSettle();

      final state = tester.state<AtlasScreenState>(find.byType(AtlasScreen));
      await state.runWeeklyConsiliumForTesting();
      await tester.pumpAndSettle();

      expect(runWeeklyCalls, equals(1));
      // Target patient vault was NOT contaminated with src patient's weekly report
      expect(vault.activePatientId, equals('patient_switch_target'));
      expect(vault.isWeeklyConsiliumCompleted, isFalse);
      expect(vault.activeWeeklySprintId, isNull);
    });
  });
}
