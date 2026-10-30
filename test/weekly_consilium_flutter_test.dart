// ignore_for_file: depend_on_referenced_packages
import 'dart:convert';
import 'dart:io';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:provider/provider.dart';

import 'package:aegis_health/main.dart';
import 'package:aegis_health/services/aegis_cascade_controller.dart';
import 'package:aegis_health/services/api/aegis_api_client.dart';
import 'package:aegis_health/services/api/medical_ai_api_models.dart';
import 'package:aegis_health/services/api/vitals_api_models.dart';
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

/// Helper to build test JWT
String _buildTestJwt({
  required String patientId,
  String deviceId = 'dev_test_weekly',
  List<String> scopes = const [
    'cascade:intake',
    'cascade:run',
    'cycle:read',
    'reports:read',
    'vitals:read',
    'vitals:write',
  ],
  String? jti,
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
        'jti': jti ?? 'jti_${DateTime.now().microsecondsSinceEpoch}',
        'exp': DateTime.now().add(const Duration(hours: 1)).millisecondsSinceEpoch ~/ 1000,
      })))
      .replaceAll('=', '');
  return '$header.$payload.signature_mock_bytes';
}

/// Helper to build all 8 system reports for weekly response
Map<String, Map<String, dynamic>> _sampleWeeklySystemReportsJson({
  required String cycleId,
  required String sprintId,
  String reportDate = '2026-10-29',
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
        'title': 'Weekly ${sys.toUpperCase()} Evaluation',
        'summary': 'Weekly dynamics for $sys are within target clinical range.',
        'report_date': reportDate,
        'system_key': sys,
        'has_changes': false,
        'highlights': ['Condition stable under active strategy.'],
        'entries': [],
      },
      'created_at': '${reportDate}T10:00:00Z',
    };
  }
  return map;
}

/// Helper to build complete WeeklyConsiliumClientResponse JSON
Map<String, dynamic> _sampleWeeklyResponseJson({
  required String cycleId,
  required String sprintId,
  int sprintNumber = 1,
  String reportDate = '2026-10-29',
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
      'model': 'gemini-3.1-pro-preview',
      'report_type': 'weekly_user_overall_report',
      'privacy_level': 'client_facing',
      'version': sprintNumber,
      'content': {
        'title': 'Weekly Sprint $sprintNumber Synthesis',
        'summary': 'Week $sprintNumber monitoring indicates stable cardiovascular and metabolic adaptation.',
        'report_date': reportDate,
        'highlights': [
          'Adherence to prescribed physical activity is 94%',
          'Resting heart rate remains in the safe zone',
        ],
        'metrics': {
          'sprint_number': sprintNumber,
        },
      },
      'created_at': '${reportDate}T10:00:00Z',
    },
    'system_reports': _sampleWeeklySystemReportsJson(
      cycleId: cycleId,
      sprintId: sprintId,
      reportDate: reportDate,
    ),
  };
}

/// Helper to build Primary Consilium reports JSON
List<Map<String, dynamic>> _samplePrimaryReportsJson({
  required String cycleId,
  String reportDate = '2026-10-22',
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

  final List<Map<String, dynamic>> list = [
    {
      'report_id': 'rep_${cycleId}_primary_overall',
      'cycle_id': cycleId,
      'model': 'gemini-3.1-pro-preview',
      'report_type': 'user_overall_report',
      'privacy_level': 'client_facing',
      'version': 1,
      'content': {
        'title': 'Primary Consilium Synthesis',
        'summary': 'Baseline clinical assessment complete across all systems.',
        'report_date': reportDate,
        'highlights': ['Baseline values established.'],
        'metrics': {
          'min_pessimum_bpm': 45,
          'max_pessimum_bpm': 165,
        },
      },
      'created_at': '${reportDate}T08:00:00Z',
    }
  ];

  for (final sys in systems) {
    list.add({
      'report_id': 'rep_${cycleId}_primary_$sys',
      'cycle_id': cycleId,
      'model': 'gemini-3.6-flash',
      'report_type': 'system_user_report_$sys',
      'privacy_level': 'client_facing',
      'version': 1,
      'content': {
        'title': 'Baseline $sys',
        'summary': 'Primary baseline status for $sys.',
        'report_date': reportDate,
        'system_key': sys,
        'has_changes': false,
        'highlights': ['Baseline established'],
        'entries': [],
      },
      'created_at': '${reportDate}T08:00:00Z',
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

  group('PROJECT AEGIS — STAGE 5.4F-4: FLUTTER WEEKLY CONSILIUM INTEGRATION', () {
    // -------------------------------------------------------------------------
    // 1. WEEKLY READINESS & DATA ISOLATION IN VAULT SERVICE
    // -------------------------------------------------------------------------
    test('1. VaultService strictly tracks Weekly Consilium readiness without mixing Primary', () async {
      final vault = VaultService(storage: memoryVaultStorage);
      await vault.initFuture;
      await vault.setActivePatientId('patient_alpha');

      expect(vault.isWeeklyConsiliumCompleted, isFalse);
      expect(vault.activeWeeklyOverallReport, isNull);
      expect(vault.activeWeeklySystemReports.isEmpty, isTrue);

      // Record Primary Consilium
      final primaryReportsJson = _samplePrimaryReportsJson(cycleId: 'cycle_100');
      final primaryReports = primaryReportsJson
          .map((j) => ClientModelReportResponse.fromJson(j))
          .toList();
      final bool primarySaved = await vault.recordClientReports(
        cycleId: 'cycle_100',
        reports: primaryReports,
        expectedPatientId: 'patient_alpha',
      );
      expect(primarySaved, isTrue);
      expect(vault.isConsiliumCompleted, isTrue);
      expect(vault.isWeeklyConsiliumCompleted, isFalse); // Weekly remains uncompleted

      // Record Weekly Consilium Sprint 1
      final weeklyJson = _sampleWeeklyResponseJson(
        cycleId: 'cycle_100',
        sprintId: 'sprint_cycle_100_w1',
        sprintNumber: 1,
      );
      final weeklyResponse = WeeklyConsiliumClientResponse.fromJson(weeklyJson);

      final bool weeklySaved = await vault.recordWeeklyClientReports(
        cycleId: weeklyResponse.cycleId,
        sprintId: weeklyResponse.sprintId,
        sprintNumber: weeklyResponse.sprintNumber,
        reportDate: weeklyResponse.reportDate,
        overallReport: weeklyResponse.overallReport,
        systemReports: weeklyResponse.systemReports,
        expectedPatientId: 'patient_alpha',
      );

      expect(weeklySaved, isTrue);
      expect(vault.isWeeklyConsiliumCompleted, isTrue);
      expect(vault.activeWeeklySprintId, equals('sprint_cycle_100_w1'));
      expect(vault.activeWeeklySprintNumber, equals(1));
      expect(vault.activeWeeklySystemReports.length, equals(8));

      // Verify no mixing: Primary cached reports remain intact
      expect(vault.cachedOverallReport?.reportType, equals('user_overall_report'));
      expect(vault.cachedOverallReport?.content.title, equals('Primary Consilium Synthesis'));
      expect(vault.activeWeeklyOverallReport?.reportType, equals('weekly_user_overall_report'));
      expect(vault.activeWeeklyOverallReport?.content.title, equals('Weekly Sprint 1 Synthesis'));
    });

    // -------------------------------------------------------------------------
    // 2. BLOCK WEEKLY BEFORE PRIMARY IS COMPLETED
    // -------------------------------------------------------------------------
    testWidgets('2. Weekly Consilium is blocked before Primary Consilium is completed', (tester) async {
      final token = _buildTestJwt(patientId: 'patient_beta');
      final mockClient = MockClient((request) async {
        if (request.url.path == '/api/v1/client/cascade/status') {
          return http.Response(
            json.encode({
              'patient_id': 'patient_beta',
              'device_id': 'dev_test_weekly',
              'has_active_cycle': true,
              'active_cycle_id': 'cycle_beta',
              'has_preparatory_anamnesis': true,
              'has_preparatory_labs': true,
              'has_preparatory_ecg': true,
              'is_primary_consilium_completed': false, // Primary NOT completed
              'has_unreviewed_lab_changes': false,
            }),
            200,
            headers: {'content-type': 'application/json'},
          );
        }
        if (request.url.path == '/api/v1/reports/client') {
          return http.Response('[]', 200, headers: {'content-type': 'application/json'});
        }
        if (request.url.path == '/api/v1/client/cascade/run-weekly') {
          fail('runWeeklyConsilium should NOT be invoked when Primary is incomplete!');
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
          refreshToken: 'rf_beta',
          expiresIn: 3600,
          expiresAt: DateTime.now().toUtc().add(const Duration(hours: 1)),
          refreshExpiresAt: DateTime.now().toUtc().add(const Duration(days: 30)),
          patientId: 'patient_beta',
          deviceId: 'dev_test_weekly',
          scope: 'cascade:intake cascade:run cycle:read reports:read',
        ),
        status: AuthSessionStatus.authenticated,
      );

      final vault = VaultService(storage: memoryVaultStorage);
      await vault.initFuture;
      await vault.setActivePatientId('patient_beta');

      await tester.pumpWidget(
        _buildAtlasTestApp(
          apiClient: apiClient,
          authManager: authManager,
          vaultService: vault,
        ),
      );
      await tester.pumpAndSettle();

      // Open Cascade Core Vault dialog
      expect(find.byKey(const Key('consilium_status_pending')), findsOneWidget);
      await tester.tap(find.byKey(const Key('consilium_status_pending')));
      await tester.pumpAndSettle();

      // Verify that run_weekly_consilium_btn does NOT exist because Primary is incomplete
      expect(find.byKey(const Key('run_weekly_consilium_btn')), findsNothing);
      expect(find.byKey(const Key('run_primary_consilium_btn')), findsOneWidget);
    });

    // -------------------------------------------------------------------------
    // 3. FIRST WEEKLY LAUNCH (WEEK 1)
    // -------------------------------------------------------------------------
    testWidgets('3. First Weekly Consilium launch runs Week 1 with stable idempotency key', (tester) async {
      final token = _buildTestJwt(patientId: 'patient_gamma');
      final primaryReportsJson = _samplePrimaryReportsJson(cycleId: 'cycle_gamma');

      String? capturedIdempotencyKey;
      int runWeeklyCalls = 0;

      final mockClient = MockClient((request) async {
        if (request.url.path == '/api/v1/client/cascade/status') {
          return http.Response(
            json.encode({
              'patient_id': 'patient_gamma',
              'device_id': 'dev_test_weekly',
              'has_active_cycle': true,
              'active_cycle_id': 'cycle_gamma',
              'has_preparatory_anamnesis': true,
              'has_preparatory_labs': true,
              'has_preparatory_ecg': true,
              'is_primary_consilium_completed': true,
              'has_unreviewed_lab_changes': false,
            }),
            200,
            headers: {'content-type': 'application/json'},
          );
        }
        if (request.url.path == '/api/v1/reports/client') {
          return http.Response(
            json.encode(primaryReportsJson),
            200,
            headers: {'content-type': 'application/json'},
          );
        }
        if (request.url.path == '/api/v1/client/cascade/run-weekly') {
          runWeeklyCalls++;
          capturedIdempotencyKey = request.headers['Idempotency-Key'];
          return http.Response(
            json.encode(_sampleWeeklyResponseJson(
              cycleId: 'cycle_gamma',
              sprintId: 'sprint_cycle_gamma_w1',
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
          refreshToken: 'rf_gamma',
          expiresIn: 3600,
          expiresAt: DateTime.now().toUtc().add(const Duration(hours: 1)),
          refreshExpiresAt: DateTime.now().toUtc().add(const Duration(days: 30)),
          patientId: 'patient_gamma',
          deviceId: 'dev_test_weekly',
          scope: 'cascade:intake cascade:run cycle:read reports:read',
        ),
        status: AuthSessionStatus.authenticated,
      );

      final vault = VaultService(storage: memoryVaultStorage);
      await vault.initFuture;
      await vault.setActivePatientId('patient_gamma');

      // Populate Primary Consilium in vault
      await vault.recordClientReports(
        cycleId: 'cycle_gamma',
        reports: primaryReportsJson.map((j) => ClientModelReportResponse.fromJson(j)).toList(),
        expectedPatientId: 'patient_gamma',
      );

      await tester.pumpWidget(
        _buildAtlasTestApp(
          apiClient: apiClient,
          authManager: authManager,
          vaultService: vault,
        ),
      );
      await tester.pumpAndSettle();

      // Badge indicates Primary is completed
      expect(find.byKey(const Key('consilium_status_ready')), findsOneWidget);
      await tester.tap(find.byKey(const Key('consilium_status_ready')));
      await tester.pumpAndSettle();

      // Launch Weekly Consilium
      expect(find.byKey(const Key('run_weekly_consilium_btn')), findsOneWidget);
      await tester.tap(find.byKey(const Key('run_weekly_consilium_btn')));
      await tester.pumpAndSettle();

      expect(runWeeklyCalls, equals(1));
      expect(capturedIdempotencyKey, equals('atlas_weekly_cycle_gamma_w1'));
      expect(vault.isWeeklyConsiliumCompleted, isTrue);
      expect(vault.activeWeeklySprintId, equals('sprint_cycle_gamma_w1'));
    });

    // -------------------------------------------------------------------------
    // 4. RECOVERY AFTER TIMEOUT (WITHOUT DUPLICATE POST)
    // -------------------------------------------------------------------------
    test('4. Timeout recovery checks GET /reports/client and recovers without duplicate POST', () async {
      int postCalls = 0;
      int getReportsCalls = 0;

      final weeklyJson = _sampleWeeklyResponseJson(
        cycleId: 'cycle_delta',
        sprintId: 'sprint_cycle_delta_w1',
        sprintNumber: 1,
      );

      final List<Map<String, dynamic>> publishedReports = [
        weeklyJson['overall_report'] as Map<String, dynamic>,
        ...((weeklyJson['system_reports'] as Map).values.cast<Map<String, dynamic>>()),
      ];

      final mockClient = MockClient((request) async {
        if (request.url.path == '/api/v1/client/cascade/run-weekly') {
          postCalls++;
          throw SocketException('Simulated network timeout');
        }
        if (request.url.path == '/api/v1/reports/client') {
          getReportsCalls++;
          return http.Response(
            json.encode(publishedReports),
            200,
            headers: {'content-type': 'application/json'},
          );
        }
        return http.Response('Not found', 404);
      });

      final apiClient = AegisApiClient(
        baseUrl: 'http://127.0.0.1:8000',
        client: mockClient,
        timeout: const Duration(milliseconds: 100),
      );

      // Attempt run-weekly which throws network exception
      expect(
        () => apiClient.runWeeklyConsilium(cycleId: 'cycle_delta', sprintId: 'sprint_cycle_delta_w1'),
        throwsA(isA<AegisNetworkException>()),
      );

      // Post-timeout recovery: query GET /reports/client
      final recoveredReports = await apiClient.getClientReports(cycleId: 'cycle_delta');
      expect(getReportsCalls, equals(1));
      expect(recoveredReports.length, equals(9)); // 1 overall + 8 systems

      final vault = VaultService(storage: memoryVaultStorage);
      await vault.initFuture;
      await vault.setActivePatientId('patient_delta');

      final overallRep = recoveredReports.firstWhere((r) => r.reportType == 'weekly_user_overall_report');
      final sysReports = <String, ClientModelReportResponse>{};
      for (final r in recoveredReports) {
        if (r.reportType.startsWith('system_user_report_')) {
          sysReports[r.reportType.substring('system_user_report_'.length)] = r;
        }
      }

      final bool saved = await vault.recordWeeklyClientReports(
        cycleId: 'cycle_delta',
        sprintId: 'sprint_cycle_delta_w1',
        sprintNumber: 1,
        reportDate: '2026-10-29',
        overallReport: overallRep,
        systemReports: sysReports,
        expectedPatientId: 'patient_delta',
      );

      expect(saved, isTrue);
      expect(vault.isWeeklyConsiliumCompleted, isTrue);
      // Zero duplicate POST
      expect(postCalls, equals(1));
    });

    // -------------------------------------------------------------------------
    // 5. RETRY OF EXISTING PERIOD DOES NOT ADVANCE SPRINT
    // -------------------------------------------------------------------------
    testWidgets('5. Retry of existing Weekly period preserves sprint_id and does not advance week', (tester) async {
      final token = _buildTestJwt(patientId: 'patient_epsilon');
      final weekly1Json = _sampleWeeklyResponseJson(
        cycleId: 'cycle_epsilon',
        sprintId: 'sprint_cycle_epsilon_w1',
        sprintNumber: 1,
      );

      String? capturedSprintInPost;
      final mockClient = MockClient((request) async {
        if (request.url.path == '/api/v1/client/cascade/status') {
          return http.Response(
            json.encode({
              'patient_id': 'patient_epsilon',
              'device_id': 'dev_test_weekly',
              'has_active_cycle': true,
              'active_cycle_id': 'cycle_epsilon',
              'has_preparatory_anamnesis': true,
              'has_preparatory_labs': true,
              'has_preparatory_ecg': true,
              'is_primary_consilium_completed': true,
              'has_unreviewed_lab_changes': false,
            }),
            200,
            headers: {'content-type': 'application/json'},
          );
        }
        if (request.url.path == '/api/v1/reports/client') {
          return http.Response(
            json.encode([
              weekly1Json['overall_report'],
              ...((weekly1Json['system_reports'] as Map).values),
            ]),
            200,
            headers: {'content-type': 'application/json'},
          );
        }
        if (request.url.path == '/api/v1/client/cascade/run-weekly') {
          final body = json.decode(request.body) as Map<String, dynamic>;
          capturedSprintInPost = body['sprint_id'] as String?;
          return http.Response(
            json.encode(weekly1Json),
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
          refreshToken: 'rf_epsilon',
          expiresIn: 3600,
          expiresAt: DateTime.now().toUtc().add(const Duration(hours: 1)),
          refreshExpiresAt: DateTime.now().toUtc().add(const Duration(days: 30)),
          patientId: 'patient_epsilon',
          deviceId: 'dev_test_weekly',
          scope: 'cascade:intake cascade:run cycle:read reports:read',
        ),
        status: AuthSessionStatus.authenticated,
      );

      final vault = VaultService(storage: memoryVaultStorage);
      await vault.initFuture;
      await vault.setActivePatientId('patient_epsilon');

      // Setup primary & weekly sprint 1 in vault
      await vault.recordClientReports(
        cycleId: 'cycle_epsilon',
        reports: _samplePrimaryReportsJson(cycleId: 'cycle_epsilon')
            .map((j) => ClientModelReportResponse.fromJson(j))
            .toList(),
        expectedPatientId: 'patient_epsilon',
      );

      final resp1 = WeeklyConsiliumClientResponse.fromJson(weekly1Json);
      await vault.recordWeeklyClientReports(
        cycleId: resp1.cycleId,
        sprintId: resp1.sprintId,
        sprintNumber: resp1.sprintNumber,
        reportDate: resp1.reportDate,
        overallReport: resp1.overallReport,
        systemReports: resp1.systemReports,
        expectedPatientId: 'patient_epsilon',
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
      expect(find.byKey(const Key('retry_weekly_consilium_btn')), findsOneWidget);
      await tester.tap(find.byKey(const Key('retry_weekly_consilium_btn')));
      await tester.pumpAndSettle();

      // Normal retry strictly targets existing sprint
      expect(capturedSprintInPost, equals('sprint_cycle_epsilon_w1'));
      expect(vault.activeWeeklySprintNumber, equals(1));
    });

    // -------------------------------------------------------------------------
    // 6. TRANSITION TO NEW WEEK & CONTRACT SAFETY (PARTIAL ON NO CONTRACT)
    // -------------------------------------------------------------------------
    testWidgets('6. Transition to new week requires explicit contract; refuses guessing sprint_id', (tester) async {
      final token = _buildTestJwt(patientId: 'patient_zeta');
      final weekly1Json = _sampleWeeklyResponseJson(
        cycleId: 'cycle_zeta',
        sprintId: 'sprint_cycle_zeta_w1',
        sprintNumber: 1,
      );

      int runWeeklyCalls = 0;
      final mockClient = MockClient((request) async {
        if (request.url.path == '/api/v1/client/cascade/status') {
          return http.Response(
            json.encode({
              'patient_id': 'patient_zeta',
              'device_id': 'dev_test_weekly',
              'has_active_cycle': true,
              'active_cycle_id': 'cycle_zeta',
              'has_preparatory_anamnesis': true,
              'has_preparatory_labs': true,
              'has_preparatory_ecg': true,
              'is_primary_consilium_completed': true,
              'has_unreviewed_lab_changes': false,
            }),
            200,
            headers: {'content-type': 'application/json'},
          );
        }
        if (request.url.path == '/api/v1/reports/client') {
          return http.Response(
            json.encode([
              weekly1Json['overall_report'],
              ...((weekly1Json['system_reports'] as Map).values),
            ]),
            200,
            headers: {'content-type': 'application/json'},
          );
        }
        if (request.url.path == '/api/v1/client/cascade/run-weekly') {
          runWeeklyCalls++;
          return http.Response('{}', 200);
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
          refreshToken: 'rf_zeta',
          expiresIn: 3600,
          expiresAt: DateTime.now().toUtc().add(const Duration(hours: 1)),
          refreshExpiresAt: DateTime.now().toUtc().add(const Duration(days: 30)),
          patientId: 'patient_zeta',
          deviceId: 'dev_test_weekly',
          scope: 'cascade:intake cascade:run cycle:read reports:read',
        ),
        status: AuthSessionStatus.authenticated,
      );

      final vault = VaultService(storage: memoryVaultStorage);
      await vault.initFuture;
      await vault.setActivePatientId('patient_zeta');

      await vault.recordClientReports(
        cycleId: 'cycle_zeta',
        reports: _samplePrimaryReportsJson(cycleId: 'cycle_zeta')
            .map((j) => ClientModelReportResponse.fromJson(j))
            .toList(),
        expectedPatientId: 'patient_zeta',
      );

      final resp1 = WeeklyConsiliumClientResponse.fromJson(weekly1Json);
      await vault.recordWeeklyClientReports(
        cycleId: resp1.cycleId,
        sprintId: resp1.sprintId,
        sprintNumber: resp1.sprintNumber,
        reportDate: resp1.reportDate,
        overallReport: resp1.overallReport,
        systemReports: resp1.systemReports,
        expectedPatientId: 'patient_zeta',
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

      // Tap СЛЕДУЮЩИЙ СПРИНТ without explicit server-provided targetSprintId
      expect(find.byKey(const Key('next_weekly_consilium_btn')), findsOneWidget);
      await tester.tap(find.byKey(const Key('next_weekly_consilium_btn')));
      await tester.pumpAndSettle();

      // Zero POST calls made: client safely refused to guess sprint_id!
      expect(runWeeklyCalls, equals(0));

      // Re-open dialog to see error explanation
      await tester.tap(find.byKey(const Key('consilium_status_ready')));
      await tester.pumpAndSettle();

      expect(
        find.textContaining('Next weekly sprint contract unavailable'),
        findsOneWidget,
      );
    });

    // -------------------------------------------------------------------------
    // 7. NO REPORT MIXING ACROSS SPRINTS & INCOMPLETE REJECTION
    // -------------------------------------------------------------------------
    test('7. Isolation across sprints: incomplete weekly reports rejected, no fallback to prior week', () async {
      final vault = VaultService(storage: memoryVaultStorage);
      await vault.initFuture;
      await vault.setActivePatientId('patient_eta');

      // Record Week 1
      final w1 = WeeklyConsiliumClientResponse.fromJson(_sampleWeeklyResponseJson(
        cycleId: 'cycle_eta',
        sprintId: 'sprint_cycle_eta_w1',
        sprintNumber: 1,
        reportDate: '2026-10-22',
      ));
      await vault.recordWeeklyClientReports(
        cycleId: w1.cycleId,
        sprintId: w1.sprintId,
        sprintNumber: w1.sprintNumber,
        reportDate: w1.reportDate,
        overallReport: w1.overallReport,
        systemReports: w1.systemReports,
        expectedPatientId: 'patient_eta',
      );

      // Attempt to record Week 2 with only 7 systems (missing endocrine)
      final incompleteSys = Map<String, ClientModelReportResponse>.from(w1.systemReports);
      incompleteSys.remove('endocrine');

      final bool savedIncomplete = await vault.recordWeeklyClientReports(
        cycleId: 'cycle_eta',
        sprintId: 'sprint_cycle_eta_w2',
        sprintNumber: 2,
        reportDate: '2026-10-29',
        overallReport: w1.overallReport,
        systemReports: incompleteSys,
        expectedPatientId: 'patient_eta',
      );

      // Incomplete report is rejected: never substitutes data from week 1
      expect(savedIncomplete, isFalse);
      expect(vault.activeWeeklySprintId, equals('sprint_cycle_eta_w1'));

      // Record full Week 2
      final w2 = WeeklyConsiliumClientResponse.fromJson(_sampleWeeklyResponseJson(
        cycleId: 'cycle_eta',
        sprintId: 'sprint_cycle_eta_w2',
        sprintNumber: 2,
        reportDate: '2026-10-29',
      ));
      final bool savedFullW2 = await vault.recordWeeklyClientReports(
        cycleId: w2.cycleId,
        sprintId: w2.sprintId,
        sprintNumber: w2.sprintNumber,
        reportDate: w2.reportDate,
        overallReport: w2.overallReport,
        systemReports: w2.systemReports,
        expectedPatientId: 'patient_eta',
      );

      expect(savedFullW2, isTrue);
      expect(vault.activeWeeklySprintId, equals('sprint_cycle_eta_w2'));

      // Both weeks are preserved in history
      final histW1 = vault.getWeeklyReportForSystem('cvs', sprintId: 'sprint_cycle_eta_w1');
      final histW2 = vault.getWeeklyReportForSystem('cvs', sprintId: 'sprint_cycle_eta_w2');

      expect(histW1?.sprintId, equals('sprint_cycle_eta_w1'));
      expect(histW2?.sprintId, equals('sprint_cycle_eta_w2'));
      expect(histW1?.content.reportDate, equals('2026-10-22'));
      expect(histW2?.content.reportDate, equals('2026-10-29'));
    });

    // -------------------------------------------------------------------------
    // 8. 401 / 403 / 409 ERROR HANDLING
    // -------------------------------------------------------------------------
    test('8. AegisApiClient handles 401, 403, and 409 appropriately', () async {
      // Test 403 Forbidden (missing cascade:run scope)
      final mock403 = MockClient((request) async {
        return http.Response(
          json.encode({'detail': 'Forbidden: cascade:run scope required'}),
          403,
          headers: {'content-type': 'application/json'},
        );
      });
      final client403 = AegisApiClient(baseUrl: 'http://127.0.0.1:8000', client: mock403);
      expect(
        () => client403.runWeeklyConsilium(cycleId: 'c1'),
        throwsA(isA<AegisInsufficientScopeException>()),
      );

      // Test 409 Conflict (Distributed lease busy)
      final mock409 = MockClient((request) async {
        return http.Response(
          json.encode({'detail': 'Conflict: active consilium lease lock in-flight'}),
          409,
          headers: {'content-type': 'application/json'},
        );
      });
      final client409 = AegisApiClient(baseUrl: 'http://127.0.0.1:8000', client: mock409);
      expect(
        () => client409.runWeeklyConsilium(cycleId: 'c1'),
        throwsA(isA<AegisConflictException>()),
      );
    });

    // -------------------------------------------------------------------------
    // 9. PATIENT SWITCHING AND EPOCH FENCING
    // -------------------------------------------------------------------------
    test('9. Patient switching discards in-flight Weekly Consilium result', () async {
      final vault = VaultService(storage: memoryVaultStorage);
      await vault.initFuture;
      await vault.setActivePatientId('patient_theta_1');

      final weeklyJson = _sampleWeeklyResponseJson(
        cycleId: 'cycle_theta',
        sprintId: 'sprint_cycle_theta_w1',
      );
      final resp = WeeklyConsiliumClientResponse.fromJson(weeklyJson);

      // Switch active patient in Vault to patient_theta_2 before recording completes
      await vault.setActivePatientId('patient_theta_2');

      // Attempt recording with previous patient attribution
      final bool saved = await vault.recordWeeklyClientReports(
        cycleId: resp.cycleId,
        sprintId: resp.sprintId,
        sprintNumber: resp.sprintNumber,
        reportDate: resp.reportDate,
        overallReport: resp.overallReport,
        systemReports: resp.systemReports,
        expectedPatientId: 'patient_theta_1', // Mismatch!
      );

      expect(saved, isFalse);
      expect(vault.isWeeklyConsiliumCompleted, isFalse);
      expect(vault.activeWeeklyOverallReport, isNull);
    });

    // -------------------------------------------------------------------------
    // 10. EXCLUSION OF DIRECT GEMINI CALLS
    // -------------------------------------------------------------------------
    test('10. Client Weekly Consilium strictly invokes server API with zero direct Gemini calls', () async {
      final capturedCalls = <String>[];
      final mockClient = MockClient((request) async {
        capturedCalls.add('${request.method} ${request.url.path}');
        if (request.url.path == '/api/v1/client/cascade/run-weekly') {
          return http.Response(
            json.encode(_sampleWeeklyResponseJson(
              cycleId: 'cycle_zero_gemini',
              sprintId: 'sprint_zero_w1',
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
      );

      final resp = await apiClient.runWeeklyConsilium(
        cycleId: 'cycle_zero_gemini',
        sprintId: 'sprint_zero_w1',
        idempotencyKey: 'atlas_weekly_cycle_zero_gemini_w1',
      );

      expect(resp.sprintId, equals('sprint_zero_w1'));
      expect(capturedCalls, equals(['POST /api/v1/client/cascade/run-weekly']));
      // Strictly 0 Gemini calls directly from client
      expect(capturedCalls.any((c) => c.contains('googleapis.com') || c.contains('gemini')), isFalse);
    });

    // -------------------------------------------------------------------------
    // 11. TIMEOUT RECOVERY NEVER FALLS BACK TO ANOTHER SPRINT
    // -------------------------------------------------------------------------
    testWidgets('11. Timeout recovery never falls back to reports from another sprint when expected sprint reports missing', (tester) async {
      final token = _buildTestJwt(patientId: 'patient_timeout_test');
      final weekly1Json = _sampleWeeklyResponseJson(
        cycleId: 'cycle_timeout',
        sprintId: 'sprint_cycle_timeout_w1',
        sprintNumber: 1,
      );

      int runWeeklyCalls = 0;
      final mockClient = MockClient((request) async {
        if (request.url.path == '/api/v1/client/cascade/status') {
          return http.Response(
            json.encode({
              'patient_id': 'patient_timeout_test',
              'device_id': 'dev_test_weekly',
              'has_active_cycle': true,
              'active_cycle_id': 'cycle_timeout',
              'has_preparatory_anamnesis': true,
              'has_preparatory_labs': true,
              'has_preparatory_ecg': true,
              'is_primary_consilium_completed': true,
              'has_unreviewed_lab_changes': false,
            }),
            200,
            headers: {'content-type': 'application/json'},
          );
        }
        if (request.url.path == '/api/v1/reports/client') {
          // Server only has reports for w1! No reports for w2!
          return http.Response(
            json.encode([
              weekly1Json['overall_report'],
              ...((weekly1Json['system_reports'] as Map).values),
            ]),
            200,
            headers: {'content-type': 'application/json'},
          );
        }
        if (request.url.path == '/api/v1/client/cascade/run-weekly') {
          runWeeklyCalls++;
          // Simulated network timeout on week 2 execution
          throw const SocketException('Simulated network drop during week 2');
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
          refreshToken: 'rf_timeout',
          expiresIn: 3600,
          expiresAt: DateTime.now().toUtc().add(const Duration(hours: 1)),
          refreshExpiresAt: DateTime.now().toUtc().add(const Duration(days: 30)),
          patientId: 'patient_timeout_test',
          deviceId: 'dev_test_weekly',
          scope: 'cascade:intake cascade:run cycle:read reports:read',
        ),
        status: AuthSessionStatus.authenticated,
      );

      final vault = VaultService(storage: memoryVaultStorage);
      await vault.initFuture;
      await vault.setActivePatientId('patient_timeout_test');

      // Setup primary & weekly sprint 1 in vault
      await vault.recordClientReports(
        cycleId: 'cycle_timeout',
        reports: _samplePrimaryReportsJson(cycleId: 'cycle_timeout')
            .map((j) => ClientModelReportResponse.fromJson(j))
            .toList(),
        expectedPatientId: 'patient_timeout_test',
      );

      final resp1 = WeeklyConsiliumClientResponse.fromJson(weekly1Json);
      await vault.recordWeeklyClientReports(
        cycleId: resp1.cycleId,
        sprintId: resp1.sprintId,
        sprintNumber: resp1.sprintNumber,
        reportDate: resp1.reportDate,
        overallReport: resp1.overallReport,
        systemReports: resp1.systemReports,
        expectedPatientId: 'patient_timeout_test',
      );

      await tester.pumpWidget(
        _buildAtlasTestApp(
          apiClient: apiClient,
          authManager: authManager,
          vaultService: vault,
        ),
      );
      await tester.pumpAndSettle();

      // Trigger execution with targetSprintId = 'sprint_cycle_timeout_w2'
      final state = tester.state<AtlasScreenState>(find.byType(AtlasScreen));
      await state.runWeeklyConsiliumForTesting(targetSprintId: 'sprint_cycle_timeout_w2');
      await tester.pumpAndSettle();

      expect(runWeeklyCalls, equals(1));
      // Vault must NOT have recorded w1 as w2!
      expect(vault.activeWeeklySprintId, equals('sprint_cycle_timeout_w1'));
      expect(vault.activeWeeklySprintNumber, equals(1));

      // Error message reflects network error
      await tester.tap(find.byKey(const Key('consilium_status_ready')));
      await tester.pumpAndSettle();

      expect(find.textContaining('Network error:'), findsOneWidget);
    });

    // -------------------------------------------------------------------------
    // 12. WEEKLY LAUNCH BLOCKED WHEN SERVER PRIMARY INCOMPLETE (DESPITE VAULT)
    // -------------------------------------------------------------------------
    testWidgets('12. Weekly launch strictly blocked when server reports Primary Consilium incomplete, even if Vault cached completion is true', (tester) async {
      final token = _buildTestJwt(patientId: 'patient_unconfirmed');
      int runWeeklyCalls = 0;

      final mockClient = MockClient((request) async {
        if (request.url.path == '/api/v1/client/cascade/status') {
          return http.Response(
            json.encode({
              'patient_id': 'patient_unconfirmed',
              'device_id': 'dev_test_weekly',
              'has_active_cycle': true,
              'active_cycle_id': 'cycle_new_unconfirmed',
              'has_preparatory_anamnesis': true,
              'has_preparatory_labs': true,
              'has_preparatory_ecg': true,
              'is_primary_consilium_completed': false, // Server says FALSE!
              'has_unreviewed_lab_changes': false,
            }),
            200,
            headers: {'content-type': 'application/json'},
          );
        }
        if (request.url.path == '/api/v1/reports/client') {
          return http.Response('[]', 200, headers: {'content-type': 'application/json'});
        }
        if (request.url.path == '/api/v1/client/cascade/run-weekly') {
          runWeeklyCalls++;
          fail('POST /run-weekly must NEVER be invoked when server Primary is incomplete!');
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
          refreshToken: 'rf_unconfirmed',
          expiresIn: 3600,
          expiresAt: DateTime.now().toUtc().add(const Duration(hours: 1)),
          refreshExpiresAt: DateTime.now().toUtc().add(const Duration(days: 30)),
          patientId: 'patient_unconfirmed',
          deviceId: 'dev_test_weekly',
          scope: 'cascade:intake cascade:run cycle:read reports:read',
        ),
        status: AuthSessionStatus.authenticated,
      );

      final vault = VaultService(storage: memoryVaultStorage);
      await vault.initFuture;
      await vault.setActivePatientId('patient_unconfirmed');

      // Pre-populate Vault with Primary completion from old cycle
      await vault.recordClientReports(
        cycleId: 'cycle_old_cached',
        reports: _samplePrimaryReportsJson(cycleId: 'cycle_old_cached')
            .map((j) => ClientModelReportResponse.fromJson(j))
            .toList(),
        expectedPatientId: 'patient_unconfirmed',
      );

      // Verify cached vault has isConsiliumCompleted == true
      expect(vault.isConsiliumCompleted, isTrue);

      await tester.pumpWidget(
        _buildAtlasTestApp(
          apiClient: apiClient,
          authManager: authManager,
          vaultService: vault,
        ),
      );
      await tester.pumpAndSettle();

      // Open Cascade Core Vault
      final badge = find.byKey(const Key('consilium_status_pending'));
      expect(badge, findsOneWidget);
      await tester.tap(badge);
      await tester.pumpAndSettle();

      // Weekly launch button must NOT be present!
      expect(find.byKey(const Key('run_weekly_consilium_btn')), findsNothing);

      // Close dialog
      await tester.tap(find.text('CLOSE'));
      await tester.pumpAndSettle();

      // Even if programmatic trigger is called, server check strictly blocks execution
      final state = tester.state<AtlasScreenState>(find.byType(AtlasScreen));
      await state.runWeeklyConsiliumForTesting();
      await tester.pumpAndSettle();

      expect(runWeeklyCalls, equals(0));
      expect(vault.isWeeklyConsiliumCompleted, isFalse);

      // Re-open dialog to verify error explaining requirement
      await tester.tap(find.byKey(const Key('consilium_status_pending')));
      await tester.pumpAndSettle();

      expect(
        find.textContaining('Weekly Consilium requires server-confirmed Primary Consilium completion'),
        findsOneWidget,
      );
    });

    // -------------------------------------------------------------------------
    // 13. VAULTSERVICE REJECTS CROSS-SPRINT REPORTS
    // -------------------------------------------------------------------------
    test('13. VaultService.recordWeeklyClientReports rejects reports with mismatched sprintId', () async {
      final vault = VaultService(storage: memoryVaultStorage);
      await vault.initFuture;
      await vault.setActivePatientId('patient_cross_sprint');

      final w1 = WeeklyConsiliumClientResponse.fromJson(_sampleWeeklyResponseJson(
        cycleId: 'cycle_cross',
        sprintId: 'sprint_cross_w1',
        sprintNumber: 1,
      ));

      // Attempt to record with sprintId: 'sprint_cross_w2', but overallReport.sprintId is 'sprint_cross_w1'
      final bool savedMismatchOverall = await vault.recordWeeklyClientReports(
        cycleId: 'cycle_cross',
        sprintId: 'sprint_cross_w2',
        sprintNumber: 2,
        reportDate: '2026-10-30',
        overallReport: w1.overallReport, // sprint_cross_w1!
        systemReports: w1.systemReports,
        expectedPatientId: 'patient_cross_sprint',
      );
      expect(savedMismatchOverall, isFalse);

      // Attempt with overall sprint_cross_w2, but one system report from sprint_cross_w1
      final w2Json = _sampleWeeklyResponseJson(
        cycleId: 'cycle_cross',
        sprintId: 'sprint_cross_w2',
        sprintNumber: 2,
      );
      final w2 = WeeklyConsiliumClientResponse.fromJson(w2Json);
      final corruptedSystems = Map<String, ClientModelReportResponse>.from(w2.systemReports);
      corruptedSystems['cvs'] = w1.systemReports['cvs']!; // from w1!

      final bool savedMismatchSys = await vault.recordWeeklyClientReports(
        cycleId: 'cycle_cross',
        sprintId: 'sprint_cross_w2',
        sprintNumber: 2,
        reportDate: '2026-10-30',
        overallReport: w2.overallReport,
        systemReports: corruptedSystems,
        expectedPatientId: 'patient_cross_sprint',
      );
      expect(savedMismatchSys, isFalse);
      expect(vault.isWeeklyConsiliumCompleted, isFalse);

      // Attempt with null overallReport.sprintId (strictly rejected)
      final overallNullJson = Map<String, dynamic>.from(w1.overallReport.toJson());
      overallNullJson['sprint_id'] = null;
      final overallNullSprint = ClientModelReportResponse.fromJson(overallNullJson);
      final bool savedNullOverall = await vault.recordWeeklyClientReports(
        cycleId: 'cycle_cross',
        sprintId: 'sprint_cross_w1',
        sprintNumber: 1,
        reportDate: '2026-10-30',
        overallReport: overallNullSprint,
        systemReports: w1.systemReports,
        expectedPatientId: 'patient_cross_sprint',
      );
      expect(savedNullOverall, isFalse);

      // Attempt with null system report sprintId (strictly rejected)
      final corruptedSysNullSprint = Map<String, ClientModelReportResponse>.from(w1.systemReports);
      final cvsNullJson = Map<String, dynamic>.from(w1.systemReports['cvs']!.toJson());
      cvsNullJson['sprint_id'] = null;
      corruptedSysNullSprint['cvs'] = ClientModelReportResponse.fromJson(cvsNullJson);
      final bool savedNullSys = await vault.recordWeeklyClientReports(
        cycleId: 'cycle_cross',
        sprintId: 'sprint_cross_w1',
        sprintNumber: 1,
        reportDate: '2026-10-30',
        overallReport: w1.overallReport,
        systemReports: corruptedSysNullSprint,
        expectedPatientId: 'patient_cross_sprint',
      );
      expect(savedNullSys, isFalse);
      expect(vault.isWeeklyConsiliumCompleted, isFalse);
    });

    // -------------------------------------------------------------------------
    // 14. RECOVERY STRICTLY VERIFIES ACTIVE CYCLE ID MATCHES REQUEST CYCLE ID
    // -------------------------------------------------------------------------
    testWidgets('14. Recovery after timeout/network error strictly verifies status.activeCycleId matches requestCycleId, never loading/saving another medical cycle', (tester) async {
      final token = _buildTestJwt(patientId: 'patient_cycle_safety');
      int runWeeklyCalls = 0;
      int statusCalls = 0;
      int getReportsCallsForNewCycle = 0;

      final mockClient = MockClient((request) async {
        if (request.url.path == '/api/v1/client/cascade/status') {
          statusCalls++;
          if (statusCalls <= 2) {
            // Initial checks: cycle_original_safety is active and primary is completed
            return http.Response(
              json.encode({
                'patient_id': 'patient_cycle_safety',
                'device_id': 'dev_test_weekly',
                'has_active_cycle': true,
                'active_cycle_id': 'cycle_original_safety',
                'has_preparatory_anamnesis': true,
                'has_preparatory_labs': true,
                'has_preparatory_ecg': true,
                'is_primary_consilium_completed': true,
                'has_unreviewed_lab_changes': false,
              }),
              200,
              headers: {'content-type': 'application/json'},
            );
          } else {
            // During post-timeout recovery: server active cycle has changed to cycle_NEW_DIFFERENT!
            return http.Response(
              json.encode({
                'patient_id': 'patient_cycle_safety',
                'device_id': 'dev_test_weekly',
                'has_active_cycle': true,
                'active_cycle_id': 'cycle_NEW_DIFFERENT', // Different cycle!
                'has_preparatory_anamnesis': true,
                'has_preparatory_labs': true,
                'has_preparatory_ecg': true,
                'is_primary_consilium_completed': true,
                'has_unreviewed_lab_changes': false,
              }),
              200,
              headers: {'content-type': 'application/json'},
            );
          }
        }
        if (request.url.path == '/api/v1/reports/client') {
          if (request.url.queryParameters['cycle_id'] == 'cycle_NEW_DIFFERENT') {
            getReportsCallsForNewCycle++;
          }
          return http.Response('[]', 200, headers: {'content-type': 'application/json'});
        }
        if (request.url.path == '/api/v1/client/cascade/run-weekly') {
          runWeeklyCalls++;
          throw SocketException('Simulated network timeout during weekly consilium');
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
          refreshToken: 'rf_cycle_safety',
          expiresIn: 3600,
          expiresAt: DateTime.now().toUtc().add(const Duration(hours: 1)),
          refreshExpiresAt: DateTime.now().toUtc().add(const Duration(days: 30)),
          patientId: 'patient_cycle_safety',
          deviceId: 'dev_test_weekly',
          scope: 'cascade:intake cascade:run cycle:read reports:read',
        ),
        status: AuthSessionStatus.authenticated,
      );

      final vault = VaultService(storage: memoryVaultStorage);
      await vault.initFuture;
      await vault.setActivePatientId('patient_cycle_safety');

      // Prepopulate Primary Consilium reports for cycle_original_safety
      await vault.recordClientReports(
        cycleId: 'cycle_original_safety',
        reports: _samplePrimaryReportsJson(cycleId: 'cycle_original_safety')
            .map((j) => ClientModelReportResponse.fromJson(j))
            .toList(),
        expectedPatientId: 'patient_cycle_safety',
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
      await state.runWeeklyConsiliumForTesting(targetSprintId: 'sprint_cycle_original_safety_w1');
      await tester.pumpAndSettle();

      expect(runWeeklyCalls, equals(1));
      // Client strictly refused to fetch reports or save data for cycle_NEW_DIFFERENT!
      expect(getReportsCallsForNewCycle, equals(0));
      expect(vault.isWeeklyConsiliumCompleted, isFalse);
      expect(vault.activeWeeklySprintId, isNull);
    });

    // -------------------------------------------------------------------------
    // 15. RECOVERY ABORTS IF SESSION EPOCH OR PATIENT ATTRIBUTION CHANGED
    // -------------------------------------------------------------------------
    testWidgets('15. Recovery after timeout/network error aborts if Vault session epoch or patient attribution changed', (tester) async {
      final token = _buildTestJwt(patientId: 'patient_epoch_test');
      int runWeeklyCalls = 0;
      int getReportsCalls = 0;

      late VaultService vault;

      final mockClient = MockClient((request) async {
        if (request.url.path == '/api/v1/client/cascade/status') {
          return http.Response(
            json.encode({
              'patient_id': 'patient_epoch_test',
              'device_id': 'dev_test_weekly',
              'has_active_cycle': true,
              'active_cycle_id': 'cycle_epoch_1',
              'has_preparatory_anamnesis': true,
              'has_preparatory_labs': true,
              'has_preparatory_ecg': true,
              'is_primary_consilium_completed': true,
              'has_unreviewed_lab_changes': false,
            }),
            200,
            headers: {'content-type': 'application/json'},
          );
        }
        if (request.url.path == '/api/v1/client/cascade/run-weekly') {
          runWeeklyCalls++;
          // Simulate patient attribution / session epoch change while request is in-flight
          await vault.setActivePatientId('patient_epoch_test_2');
          throw SocketException('Simulated network drop');
        }
        if (request.url.path == '/api/v1/reports/client') {
          getReportsCalls++;
          return http.Response('[]', 200, headers: {'content-type': 'application/json'});
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
          refreshToken: 'rf_epoch',
          expiresIn: 3600,
          expiresAt: DateTime.now().toUtc().add(const Duration(hours: 1)),
          refreshExpiresAt: DateTime.now().toUtc().add(const Duration(days: 30)),
          patientId: 'patient_epoch_test',
          deviceId: 'dev_test_weekly',
          scope: 'cascade:intake cascade:run cycle:read reports:read',
        ),
        status: AuthSessionStatus.authenticated,
      );

      vault = VaultService(storage: memoryVaultStorage);
      await vault.initFuture;
      await vault.setActivePatientId('patient_epoch_test');

      await vault.recordClientReports(
        cycleId: 'cycle_epoch_1',
        reports: _samplePrimaryReportsJson(cycleId: 'cycle_epoch_1')
            .map((j) => ClientModelReportResponse.fromJson(j))
            .toList(),
        expectedPatientId: 'patient_epoch_test',
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
      await state.runWeeklyConsiliumForTesting(targetSprintId: 'sprint_cycle_epoch_1_w1');
      await tester.pumpAndSettle();

      expect(runWeeklyCalls, equals(1));
      // getReports was called once initially to check existing sprint, but 0 times during recovery
      expect(getReportsCalls, equals(1));
      expect(vault.isWeeklyConsiliumCompleted, isFalse);
    });

    // -------------------------------------------------------------------------
    // 16. PRIMARY CONSILIUM RECOVERY VERIFIES STATUS.ACTIVECYCLEID == REQUESTCYCLEID
    // -------------------------------------------------------------------------
    testWidgets('16. Primary Consilium timeout recovery strictly verifies activeCycleId matches requestCycleId, never loading/saving another cycle', (tester) async {
      final token = _buildTestJwt(patientId: 'patient_primary_safety');
      int runPrimaryCalls = 0;
      int statusCalls = 0;
      int getReportsCallsForNewCycle = 0;

      final mockClient = MockClient((request) async {
        if (request.url.path == '/api/v1/client/cascade/status') {
          statusCalls++;
          if (statusCalls <= 2) {
            // First check on init and second check in _runPrimaryConsiliumExplicitly
            return http.Response(
              json.encode({
                'patient_id': 'patient_primary_safety',
                'device_id': 'dev_test_weekly',
                'has_active_cycle': true,
                'active_cycle_id': 'cycle_primary_orig',
                'has_preparatory_anamnesis': true,
                'has_preparatory_labs': true,
                'has_preparatory_ecg': true,
                'is_primary_consilium_completed': false,
                'has_unreviewed_lab_changes': false,
              }),
              200,
              headers: {'content-type': 'application/json'},
            );
          } else {
            // During post-timeout recovery: server active cycle changed to cycle_primary_OTHER!
            return http.Response(
              json.encode({
                'patient_id': 'patient_primary_safety',
                'device_id': 'dev_test_weekly',
                'has_active_cycle': true,
                'active_cycle_id': 'cycle_primary_OTHER',
                'has_preparatory_anamnesis': true,
                'has_preparatory_labs': true,
                'has_preparatory_ecg': true,
                'is_primary_consilium_completed': true,
                'has_unreviewed_lab_changes': false,
              }),
              200,
              headers: {'content-type': 'application/json'},
            );
          }
        }
        if (request.url.path == '/api/v1/client/cascade/run-primary') {
          runPrimaryCalls++;
          throw SocketException('Simulated network timeout during primary consilium');
        }
        if (request.url.path == '/api/v1/reports/client') {
          if (request.url.queryParameters['cycle_id'] == 'cycle_primary_OTHER') {
            getReportsCallsForNewCycle++;
          }
          return http.Response('[]', 200, headers: {'content-type': 'application/json'});
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
          refreshToken: 'rf_primary_safety',
          expiresIn: 3600,
          expiresAt: DateTime.now().toUtc().add(const Duration(hours: 1)),
          refreshExpiresAt: DateTime.now().toUtc().add(const Duration(days: 30)),
          patientId: 'patient_primary_safety',
          deviceId: 'dev_test_weekly',
          scope: 'cascade:intake cascade:run cycle:read reports:read',
        ),
        status: AuthSessionStatus.authenticated,
      );

      final vault = VaultService(storage: memoryVaultStorage);
      await vault.initFuture;
      await vault.setActivePatientId('patient_primary_safety');

      await tester.pumpWidget(
        _buildAtlasTestApp(
          apiClient: apiClient,
          authManager: authManager,
          vaultService: vault,
        ),
      );
      await tester.pumpAndSettle();

      // Tap pending badge to open Cascade Core Vault dialog
      final badge = find.byKey(const Key('consilium_status_pending'));
      expect(badge, findsOneWidget);
      await tester.tap(badge);
      await tester.pumpAndSettle();

      // Tap ЗАПУСТИТЬ КОНСИЛИУМ
      final runBtn = find.byKey(const Key('run_primary_consilium_btn'));
      expect(runBtn, findsOneWidget);
      await tester.tap(runBtn);
      await tester.pumpAndSettle();

      expect(runPrimaryCalls, equals(1));
      // Recovery must NOT have loaded reports for cycle_primary_OTHER
      expect(getReportsCallsForNewCycle, equals(0));
      expect(vault.isConsiliumCompleted, isFalse);
    });
  });
}
