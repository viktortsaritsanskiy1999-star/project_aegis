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
  String deviceId = 'dev_test_atlas',
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

/// Helper to build sample client reports list matching backend PrimaryConsiliumService output
List<Map<String, dynamic>> _sampleClientReportsJson({
  required String cycleId,
  String reportDate = '2026-10-29',
}) {
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
        'summary': 'Сердечно-сосудистая и дыхательная системы стабильны. Метаболический профиль в норме.',
        'report_date': reportDate,
        'highlights': [
          'Ритм синусовый, ЧСС покоя 68 уд/мин',
          'Артериальное давление в целевом диапазоне',
          'Аэробная зона тренировок: 110-140 уд/мин',
        ],
        'metrics': {
          'min_pessimum_bpm': 45,
          'max_pessimum_bpm': 165,
          'aerobic_zone_min_bpm': 110,
          'aerobic_zone_max_bpm': 140,
        },
      },
      'created_at': '${reportDate}T08:00:00Z',
    },
    {
      'report_id': 'rep_${cycleId}_cvs',
      'cycle_id': cycleId,
      'model': 'gemini-3.6-flash',
      'report_type': 'system_user_report_cvs',
      'privacy_level': 'client_facing',
      'version': 1,
      'content': {
        'title': 'Сердечно-сосудистая система',
        'summary': 'Нормосистолия, патологических нарушений ритма и проводимости не выявлено.',
        'report_date': reportDate,
        'system_key': 'cvs',
        'has_changes': false,
        'highlights': [
          'ЭКГ: нормальный синусовый ритм',
          'Пульс покоя оптимальный',
        ],
        'entries': [],
      },
      'created_at': '${reportDate}T08:00:00Z',
    },
    {
      'report_id': 'rep_${cycleId}_gi_tract',
      'cycle_id': cycleId,
      'model': 'gemini-3.6-flash',
      'report_type': 'system_user_report_gi_tract',
      'privacy_level': 'client_facing',
      'version': 1,
      'content': {
        'title': 'Желудочно-кишечный тракт',
        'summary': 'Ферментативная функция печени и поджелудочной железы сохранна.',
        'report_date': reportDate,
        'system_key': 'gi_tract',
        'has_changes': false,
        'highlights': ['Биохимия печени в пределах референсов'],
        'entries': [],
      },
      'created_at': '${reportDate}T08:00:00Z',
    },
    {
      'report_id': 'rep_${cycleId}_respiratory',
      'cycle_id': cycleId,
      'model': 'gemini-3.6-flash',
      'report_type': 'system_user_report_respiratory',
      'privacy_level': 'client_facing',
      'version': 1,
      'content': {
        'title': 'Дыхательная система',
        'summary': 'Оксигенация стабильная (SpO2 98%). Признаков дыхательной недостаточности нет.',
        'report_date': reportDate,
        'system_key': 'respiratory',
        'has_changes': false,
        'highlights': ['SpO2 98-99% в покое'],
        'entries': [],
      },
      'created_at': '${reportDate}T08:00:00Z',
    },
  ];
}

/// Helper to build complete widget tree for AtlasScreen testing
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

  // Suppress Athena welcome popup in widget tests so that underlying screen is unobstructed
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

  group('PROJECT AEGIS — STAGE 5.4E-2: ATLAS PRIMARY CONSILIUM INTEGRATION', () {
    // -------------------------------------------------------------------------
    // 1. AegisApiClient UNIT TESTS
    // -------------------------------------------------------------------------
    group('1. AegisApiClient Client Reports Transport', () {
      test('getClientReports returns strictly client_facing reports on HTTP 200', () async {
        final mockClient = MockClient((request) async {
          expect(request.method, equals('GET'));
          expect(request.url.path, equals('/api/v1/reports/client'));
          expect(request.url.queryParameters['cycle_id'], equals('cycle_100'));
          expect(request.headers['Authorization'], startsWith('Bearer '));

          final body = json.encode(_sampleClientReportsJson(cycleId: 'cycle_100'));
          return http.Response(body, 200, headers: {'content-type': 'application/json'});
        });

        final client = AegisApiClient(
          baseUrl: 'http://127.0.0.1:8000',
          client: mockClient,
          tokenProvider: () async => _buildTestJwt(patientId: 'pat_001'),
        );

        final reports = await client.getClientReports(cycleId: 'cycle_100');
        expect(reports.length, equals(4));

        final overall = reports.firstWhere((r) => r.reportType == 'user_overall_report');
        expect(overall.privacyLevel, equals('client_facing'));
        expect(overall.content.summary, contains('Сердечно-сосудистая и дыхательная системы'));
        expect(overall.content.highlights.length, equals(3));
        expect(overall.content.metrics['min_pessimum_bpm'], equals(45));

        final cvs = reports.firstWhere((r) => r.reportType == 'system_user_report_cvs');
        expect(cvs.privacyLevel, equals('client_facing'));
        expect(cvs.content.systemKey, equals('cvs'));
      });

      test('getClientReports filters by report_type query parameter', () async {
        final mockClient = MockClient((request) async {
          expect(request.url.queryParameters['cycle_id'], equals('cycle_100'));
          expect(request.url.queryParameters['report_type'], equals('user_overall_report'));

          final body = json.encode([
            _sampleClientReportsJson(cycleId: 'cycle_100').first,
          ]);
          return http.Response(body, 200, headers: {'content-type': 'application/json'});
        });

        final client = AegisApiClient(
          baseUrl: 'http://127.0.0.1:8000',
          client: mockClient,
          tokenProvider: () async => _buildTestJwt(patientId: 'pat_001'),
        );

        final reports = await client.getClientReports(
          cycleId: 'cycle_100',
          reportType: 'user_overall_report',
        );
        expect(reports.length, equals(1));
        expect(reports.first.reportType, equals('user_overall_report'));
      });

      test('getClientReports throws AegisValidationException on empty cycleId', () async {
        final client = AegisApiClient(baseUrl: 'http://127.0.0.1:8000');
        expect(
          () => client.getClientReports(cycleId: '   '),
          throwsA(isA<AegisValidationException>()),
        );
      });

      test('getClientReports retries on 401 with refreshed token', () async {
        int attempts = 0;
        final now = DateTime.now().toUtc();
        final mockClient = MockClient((request) async {
          if (request.url.path == '/api/v1/auth/refresh') {
            return http.Response(
              json.encode({
                'token_type': 'Bearer',
                'access_token': _buildTestJwt(patientId: 'pat_001', jti: 'refreshed_token'),
                'refresh_token': 'new_refresh_token',
                'expires_in': 3600,
                'expires_at': now.add(const Duration(hours: 1)).toIso8601String(),
                'refresh_expires_at': now.add(const Duration(days: 30)).toIso8601String(),
                'patient_id': 'pat_001',
                'device_id': 'dev_test_atlas',
                'scope': 'cascade:intake cascade:run cycle:read reports:read',
              }),
              200,
              headers: {'content-type': 'application/json'},
            );
          }

          if (request.url.path == '/api/v1/reports/client') {
            attempts++;
            if (attempts == 1) {
              return http.Response(json.encode({'detail': 'Token expired'}), 401);
            }
            return http.Response(
              json.encode(_sampleClientReportsJson(cycleId: 'cycle_100')),
              200,
              headers: {'content-type': 'application/json'},
            );
          }
          return http.Response('Not Found', 404);
        });

        final client = AegisApiClient(baseUrl: 'http://127.0.0.1:8000', client: mockClient);
        final authManager = AuthSessionManager(
          apiClient: client,
          storage: memoryAuthStorage,
        );
        authManager.setSessionForTesting(
          AuthSession(
            accessToken: _buildTestJwt(patientId: 'pat_001', jti: 'initial_token'),
            refreshToken: 'refresh_tok',
            expiresIn: 3600,
            expiresAt: DateTime.now().toUtc().add(const Duration(hours: 1)),
            refreshExpiresAt: DateTime.now().toUtc().add(const Duration(days: 30)),
            patientId: 'pat_001',
            deviceId: 'dev_test_atlas',
            scope: 'cascade:intake cascade:run cycle:read reports:read',
          ),
          status: AuthSessionStatus.authenticated,
        );

        final reports = await client.getClientReports(cycleId: 'cycle_100');
        expect(reports.length, equals(4));
        expect(attempts, equals(2));
      });

      test('getClientReports throws AegisForbiddenException on 403', () async {
        final mockClient = MockClient((request) async {
          return http.Response(json.encode({'detail': 'Cycle access forbidden'}), 403);
        });

        final client = AegisApiClient(
          baseUrl: 'http://127.0.0.1:8000',
          client: mockClient,
          tokenProvider: () async => _buildTestJwt(patientId: 'pat_001'),
        );

        expect(
          () => client.getClientReports(cycleId: 'cycle_unauthorized'),
          throwsA(isA<AegisForbiddenException>()),
        );
      });

      test('getClientReports handles network timeout gracefully', () async {
        final mockClient = MockClient((request) async {
          await Future.delayed(const Duration(milliseconds: 100));
          return http.Response('[]', 200);
        });

        final client = AegisApiClient(
          baseUrl: 'http://127.0.0.1:8000',
          client: mockClient,
          timeout: const Duration(milliseconds: 20),
          tokenProvider: () async => _buildTestJwt(patientId: 'pat_001'),
        );

        expect(
          () => client.getClientReports(cycleId: 'cycle_timeout'),
          throwsA(isA<AegisTimeoutException>()),
        );
      });

      test('getSingleClientReport fetches report by ID', () async {
        final mockClient = MockClient((request) async {
          expect(request.url.path, equals('/api/v1/reports/client/rep_cycle_100_overall'));
          final rep = _sampleClientReportsJson(cycleId: 'cycle_100').first;
          return http.Response(json.encode(rep), 200, headers: {'content-type': 'application/json'});
        });

        final client = AegisApiClient(
          baseUrl: 'http://127.0.0.1:8000',
          client: mockClient,
          tokenProvider: () async => _buildTestJwt(patientId: 'pat_001'),
        );

        final report = await client.getSingleClientReport(reportId: 'rep_cycle_100_overall');
        expect(report.reportId, equals('rep_cycle_100_overall'));
        expect(report.content.title, contains('Интегральное заключение'));
      });
    });

    // -------------------------------------------------------------------------
    // 2. VaultService & PATIENT ISOLATION TESTS
    // -------------------------------------------------------------------------
    group('2. VaultService Client Reports & Patient Isolation', () {
      test('recordClientReports updates organ system general shelves and overall route', () async {
        final vault = VaultService(storage: memoryVaultStorage);
        await vault.initFuture;
        await vault.setActivePatientId('pat_001');

        final reports = _sampleClientReportsJson(cycleId: 'cycle_100')
            .map((j) => ClientModelReportResponse.fromJson(j))
            .toList();

        final confirmed = await vault.recordClientReports(
          cycleId: 'cycle_100',
          reports: reports,
          expectedPatientId: 'pat_001',
        );
        expect(confirmed, isTrue);
        expect(vault.isConsiliumCompleted, isTrue);
        expect(vault.consiliumCycleId, equals('cycle_100'));

        // Overall pro general shelf is updated with client summary
        final coreCascade = vault.getSystemContext('core_cascade');
        expect(coreCascade['pro_general_shelf'], contains('Сердечно-сосудистая и дыхательная системы'));

        // CVS general shelf is updated with verified client CVS summary
        final cvs = vault.getSystemContext('cvs');
        expect(cvs['system_general_shelf'], contains('Нормосистолия, патологических нарушений'));

        // Vitals thresholds from overall metrics are mapped
        final global = vault.getSystemContext('global');
        expect(global['vitals_thresholds']['min_pessimum_bpm'], equals(45));

        // Typed getter works
        final cvsReport = vault.getClientReportForSystem('cvs');
        expect(cvsReport, isNotNull);
        expect(cvsReport!.content.title, equals('Сердечно-сосудистая система'));
      });

      test('Patient switching clears previous patient reports from memory synchronously', () async {
        final vault = VaultService(storage: memoryVaultStorage);
        await vault.initFuture;
        await vault.setActivePatientId('pat_001');

        final reports = _sampleClientReportsJson(cycleId: 'cycle_100')
            .map((j) => ClientModelReportResponse.fromJson(j))
            .toList();

        await vault.recordClientReports(cycleId: 'cycle_100', reports: reports, expectedPatientId: 'pat_001');
        expect(vault.isConsiliumCompleted, isTrue);
        expect(vault.getClientReportForSystem('cvs'), isNotNull);

        // Switch patient to pat_002
        await vault.setActivePatientId('pat_002');

        // Immediately, pat_001's reports must NOT exist in pat_002's memory
        expect(vault.isConsiliumCompleted, isFalse);
        expect(vault.consiliumCycleId, isNull);
        expect(vault.cachedOverallReport, isNull);
        expect(vault.cachedSystemReports, isEmpty);
        expect(vault.getClientReportForSystem('cvs'), isNull);

        // Switching back to pat_001 restores saved snapshot from storage
        await vault.setActivePatientId('pat_001');
        expect(vault.isConsiliumCompleted, isTrue);
        expect(vault.consiliumCycleId, equals('cycle_100'));
        expect(vault.getClientReportForSystem('cvs'), isNotNull);
      });

      test('Patient attribution mismatch rejects recordClientReports', () async {
        final vault = VaultService(storage: memoryVaultStorage);
        await vault.initFuture;
        await vault.setActivePatientId('pat_001');

        final reports = _sampleClientReportsJson(cycleId: 'cycle_100')
            .map((j) => ClientModelReportResponse.fromJson(j))
            .toList();

        // Stale network response from previous patient 'pat_legacy'
        final result = await vault.recordClientReports(
          cycleId: 'cycle_100',
          reports: reports,
          expectedPatientId: 'pat_legacy',
        );

        expect(result, isFalse);
        expect(vault.isConsiliumCompleted, isFalse);
      });

      test('ClientModelReportResponse throws FormatException on server_only privacy level', () {
        final illegalJson = {
          'report_id': 'rep_secret',
          'cycle_id': 'cycle_1',
          'model': 'gemini-3.1-pro-preview',
          'report_type': 'chief_physician_internal',
          'privacy_level': 'server_only',
          'version': 1,
          'content': {'summary': 'Internal medical reasoning'},
          'created_at': '2026-10-29T08:00:00Z',
        };

        expect(
          () => ClientModelReportResponse.fromJson(illegalJson),
          throwsA(isA<FormatException>().having(
            (e) => e.message,
            'message',
            contains('Security Violation: Received report with non-client_facing privacy_level'),
          )),
        );
      });

      test('Incomplete or empty client_facing reports reject recordClientReports and do not mark completed', () async {
        final vault = VaultService(storage: memoryVaultStorage);
        await vault.initFuture;
        await vault.setActivePatientId('pat_001');

        // Case 1: Empty reports list
        final resultEmpty = await vault.recordClientReports(
          cycleId: 'cycle_100',
          reports: [],
          expectedPatientId: 'pat_001',
        );
        expect(resultEmpty, isFalse);
        expect(vault.isConsiliumCompleted, isFalse);
        expect(vault.cachedOverallReport, isNull);

        // Case 2: Only organ reports without user_overall_report
        final onlyOrganReports = _sampleClientReportsJson(cycleId: 'cycle_100')
            .where((j) => j['report_type'] != 'user_overall_report')
            .map((j) => ClientModelReportResponse.fromJson(j))
            .toList();
        expect(onlyOrganReports.isNotEmpty, isTrue);

        final resultIncomplete = await vault.recordClientReports(
          cycleId: 'cycle_100',
          reports: onlyOrganReports,
          expectedPatientId: 'pat_001',
        );
        expect(resultIncomplete, isFalse);
        expect(vault.isConsiliumCompleted, isFalse);
        expect(vault.cachedOverallReport, isNull);
      });

      test('Prevention of mixing results of different cycles: cleans old system shelves and archives history', () async {
        final vault = VaultService(storage: memoryVaultStorage);
        await vault.initFuture;
        await vault.setActivePatientId('pat_001');

        // Cycle 1 contains CVS, GI, Respiratory
        final cycle1Reports = _sampleClientReportsJson(cycleId: 'cycle_1')
            .map((j) => ClientModelReportResponse.fromJson(j))
            .toList();
        final success1 = await vault.recordClientReports(
          cycleId: 'cycle_1',
          reports: cycle1Reports,
          expectedPatientId: 'pat_001',
        );
        expect(success1, isTrue);
        expect(vault.consiliumCycleId, equals('cycle_1'));
        expect(vault.cachedSystemReports.containsKey('respiratory'), isTrue);
        expect(vault.getClientReportForSystem('respiratory'), isNotNull);

        // Cycle 2 contains ONLY CVS and GI (Respiratory is absent in Cycle 2)
        final cycle2Reports = _sampleClientReportsJson(cycleId: 'cycle_2')
            .where((j) => j['report_type'] != 'system_user_report_respiratory')
            .map((j) => ClientModelReportResponse.fromJson(j))
            .toList();

        final success2 = await vault.recordClientReports(
          cycleId: 'cycle_2',
          reports: cycle2Reports,
          expectedPatientId: 'pat_001',
        );
        expect(success2, isTrue);
        expect(vault.consiliumCycleId, equals('cycle_2'));

        // Respiratory from Cycle 1 is completely purged from active state to prevent mixing
        expect(vault.cachedSystemReports.containsKey('respiratory'), isFalse);
        expect(vault.getClientReportForSystem('respiratory'), isNull);
        expect(vault.getSystemContext('respiratory')['system_user_report'], isNull);
        expect(vault.getSystemContext('respiratory')['system_general_shelf'], isNull);

        // CVS in active state belongs strictly to cycle_2
        expect(vault.getClientReportForSystem('cvs')!.cycleId, equals('cycle_2'));

        // Historical data of Cycle 1 and Cycle 2 are both preserved
        expect(vault.consiliumHistory.containsKey('cycle_1'), isTrue);
        expect(vault.consiliumHistory.containsKey('cycle_2'), isTrue);
        final histCycle1 = vault.getHistoricalConsilium('cycle_1');
        expect(histCycle1, isNotNull);
        expect(histCycle1!['cycle_id'], equals('cycle_1'));
      });

      test('New cycle after completed old cycle: markConsiliumPending archives history and resets active view', () async {
        final vault = VaultService(storage: memoryVaultStorage);
        await vault.initFuture;
        await vault.setActivePatientId('pat_001');

        final cycle1Reports = _sampleClientReportsJson(cycleId: 'cycle_1')
            .map((j) => ClientModelReportResponse.fromJson(j))
            .toList();
        await vault.recordClientReports(
          cycleId: 'cycle_1',
          reports: cycle1Reports,
          expectedPatientId: 'pat_001',
        );
        expect(vault.isConsiliumCompleted, isTrue);

        // Server enters Cycle 2 which is pending
        final marked = await vault.markConsiliumPending(
          cycleId: 'cycle_2',
          expectedPatientId: 'pat_001',
        );
        expect(marked, isTrue);

        // Active state is pending, does NOT show past conclusion as ready
        expect(vault.isConsiliumCompleted, isFalse);
        expect(vault.consiliumCycleId, equals('cycle_2'));
        expect(vault.cachedOverallReport, isNull);
        expect(vault.cachedSystemReports, isEmpty);
        expect(vault.getSystemContext('cvs')['system_general_shelf'], isNull);
        expect(vault.getSystemContext('core_cascade')['pro_general_shelf'], isNull);

        // Past cycle conclusion is safely archived in history
        expect(vault.getHistoricalConsilium('cycle_1'), isNotNull);
      });

      test('Vault storage failure during recordClientReports returns false', () async {
        final vault = VaultService(storage: memoryVaultStorage);
        await vault.initFuture;
        await vault.setActivePatientId('pat_001');

        memoryVaultStorage.failWithException = true;

        final reports = _sampleClientReportsJson(cycleId: 'cycle_100')
            .map((j) => ClientModelReportResponse.fromJson(j))
            .toList();

        final result = await vault.recordClientReports(
          cycleId: 'cycle_100',
          reports: reports,
          expectedPatientId: 'pat_001',
        );

        expect(result, isFalse);
      });

      test('Patient change during local storage persistence aborts and returns false', () async {
        final vault = VaultService(storage: memoryVaultStorage);
        await vault.initFuture;
        await vault.setActivePatientId('pat_001');

        memoryVaultStorage.onSavePatientVault = (key, data) async {
          // Simulate concurrent patient switch during asynchronous persistence
          await vault.setActivePatientId('pat_002');
        };

        final reports = _sampleClientReportsJson(cycleId: 'cycle_100')
            .map((j) => ClientModelReportResponse.fromJson(j))
            .toList();

        final result = await vault.recordClientReports(
          cycleId: 'cycle_100',
          reports: reports,
          expectedPatientId: 'pat_001',
        );

        expect(result, isFalse);
        expect(vault.activePatientId, equals('pat_002'));
        expect(vault.isConsiliumCompleted, isFalse);
      });
    });

    // -------------------------------------------------------------------------
    // 3. AtlasScreen WIDGET INTEGRATION TESTS
    // -------------------------------------------------------------------------
    group('3. AtlasScreen Primary Consilium Integration & UI State', () {
      testWidgets('Displays pending state when Primary Consilium is not yet completed on server', (tester) async {
        bool postRunPrimaryCalled = false;
        final mockClient = MockClient((request) async {
          if (request.method == 'POST' && request.url.path.contains('run-primary')) {
            postRunPrimaryCalled = true;
          }
          if (request.url.path == '/api/v1/client/cascade/status') {
            return http.Response(
              json.encode({
                'patient_id': 'pat_001',
                'device_id': 'dev_test_atlas',
                'has_active_cycle': true,
                'active_cycle_id': 'cycle_100',
                'has_preparatory_anamnesis': true,
                'has_preparatory_labs': true,
                'has_preparatory_ecg': true,
                'is_primary_consilium_completed': false, // Not completed!
                'has_unreviewed_lab_changes': false,
              }),
              200,
              headers: {'content-type': 'application/json'},
            );
          }
          return http.Response('Not Found', 404);
        });

        final client = AegisApiClient(baseUrl: 'http://127.0.0.1:8000', client: mockClient);
        final authManager = AuthSessionManager(apiClient: client, storage: memoryAuthStorage);
        authManager.setSessionForTesting(
          AuthSession(
            accessToken: _buildTestJwt(patientId: 'pat_001'),
            refreshToken: 'refresh_tok',
            expiresIn: 3600,
            expiresAt: DateTime.now().toUtc().add(const Duration(hours: 1)),
            refreshExpiresAt: DateTime.now().toUtc().add(const Duration(days: 30)),
            patientId: 'pat_001',
            deviceId: 'dev_test_atlas',
            scope: 'cascade:intake cascade:run cycle:read reports:read',
          ),
          status: AuthSessionStatus.authenticated,
        );

        final vault = VaultService(storage: memoryVaultStorage, authSessionManager: authManager);
        await vault.initFuture;

        await tester.pumpWidget(
          _buildAtlasTestApp(
            apiClient: client,
            authManager: authManager,
            vaultService: vault,
          ),
        );

        // Allow post-frame callbacks and async HTTP resolution
        await tester.pumpAndSettle();

        // 1. Pending badge is visible
        expect(find.byKey(const Key('consilium_status_pending')), findsOneWidget);
        expect(find.text('Ожидание первичного консилиума'), findsOneWidget);

        // 2. POST run-primary was NEVER called implicitly
        expect(postRunPrimaryCalled, isFalse);

        // 3. Organ system dialog shows pending consilium status and NO training shelves
        final cvsBtn = find.byKey(const Key('folder_btn_cvs'));
        expect(cvsBtn, findsOneWidget);
        await tester.tap(cvsBtn);
        await tester.pumpAndSettle();

        expect(find.text('Primary consilium analysis pending'), findsOneWidget);
        expect(find.textContaining('TRAINING OUTPUT SHELF'), findsNothing);
        expect(find.textContaining('LOGS FOR FINE-TUNING'), findsNothing);

        // Close dialog
        await tester.tap(find.text('CLOSE'));
        await tester.pumpAndSettle();

        // 4. Cascade Core Vault FAB shows pending consilium message and NO training shelves
        final cascadeFab = find.byKey(const Key('cascade_vault_fab'));
        await tester.tap(cascadeFab);
        await tester.pumpAndSettle();

        expect(find.text('STATUS: PENDING CONSILIUM'), findsOneWidget);
        expect(find.textContaining('Primary Consilium analysis has not been performed yet'), findsOneWidget);
        expect(find.textContaining('flash_training_shelf'), findsNothing);
        expect(find.textContaining('pro_training_shelf'), findsNothing);
        expect(find.textContaining('deficiency logs'), findsNothing);
      });

      testWidgets('Loads and displays verified Primary Consilium reports when completed', (tester) async {
        final mockClient = MockClient((request) async {
          if (request.url.path == '/api/v1/client/cascade/status') {
            return http.Response(
              json.encode({
                'patient_id': 'pat_001',
                'device_id': 'dev_test_atlas',
                'has_active_cycle': true,
                'active_cycle_id': 'cycle_100',
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
              json.encode(_sampleClientReportsJson(cycleId: 'cycle_100')),
              200,
              headers: {'content-type': 'application/json'},
            );
          }
          return http.Response('Not Found', 404);
        });

        final client = AegisApiClient(baseUrl: 'http://127.0.0.1:8000', client: mockClient);
        final authManager = AuthSessionManager(apiClient: client, storage: memoryAuthStorage);
        authManager.setSessionForTesting(
          AuthSession(
            accessToken: _buildTestJwt(patientId: 'pat_001'),
            refreshToken: 'refresh_tok',
            expiresIn: 3600,
            expiresAt: DateTime.now().toUtc().add(const Duration(hours: 1)),
            refreshExpiresAt: DateTime.now().toUtc().add(const Duration(days: 30)),
            patientId: 'pat_001',
            deviceId: 'dev_test_atlas',
            scope: 'cascade:intake cascade:run cycle:read reports:read',
          ),
          status: AuthSessionStatus.authenticated,
        );

        final vault = VaultService(storage: memoryVaultStorage, authSessionManager: authManager);
        await vault.initFuture;

        await tester.pumpWidget(
          _buildAtlasTestApp(
            apiClient: client,
            authManager: authManager,
            vaultService: vault,
          ),
        );

        await tester.pumpAndSettle();

        // 1. Verified badge is visible
        expect(find.byKey(const Key('consilium_status_ready')), findsOneWidget);
        expect(find.text('Консилиум сформирован'), findsOneWidget);

        // 2. Open CVS system log dialog via folder button
        final cvsBtn = find.byKey(const Key('folder_btn_cvs'));
        expect(cvsBtn, findsOneWidget);
        await tester.tap(cvsBtn);
        await tester.pumpAndSettle();

        expect(find.text('SYSTEM_LOG :: CVS'), findsOneWidget);
        expect(find.text('Сердечно-сосудистая система'), findsOneWidget);
        expect(find.textContaining('Нормосистолия, патологических нарушений'), findsOneWidget);
        expect(find.textContaining('ЭКГ: нормальный синусовый ритм'), findsOneWidget);
        expect(find.textContaining('TRAINING OUTPUT SHELF'), findsNothing);

        await tester.tap(find.text('CLOSE'));
        await tester.pumpAndSettle();

        // 3. Open Cascade Core Vault dialog
        final cascadeFab = find.byKey(const Key('cascade_vault_fab'));
        await tester.tap(cascadeFab);
        await tester.pumpAndSettle();

        expect(find.text('PRIMARY CONSILIUM :: CLINICAL SUMMARY'), findsOneWidget);
        expect(find.textContaining('ИНТЕГРАЛЬНОЕ ЗАКЛЮЧЕНИЕ'), findsOneWidget);
        expect(find.textContaining('Сердечно-сосудистая и дыхательная системы стабильны'), findsOneWidget);
        expect(find.textContaining('Ритм синусовый, ЧСС покоя 68 уд/мин'), findsOneWidget);
        expect(find.textContaining('flash_training_shelf'), findsNothing);
        expect(find.textContaining('pro_training_shelf'), findsNothing);
      });

      testWidgets('Network error displays error badge and preserves existing vault data', (tester) async {
        final mockClient = MockClient((request) async {
          throw const SocketException('Connection refused');
        });

        final client = AegisApiClient(baseUrl: 'http://127.0.0.1:8000', client: mockClient);
        final authManager = AuthSessionManager(apiClient: client, storage: memoryAuthStorage);
        authManager.setSessionForTesting(
          AuthSession(
            accessToken: _buildTestJwt(patientId: 'pat_001'),
            refreshToken: 'refresh_tok',
            expiresIn: 3600,
            expiresAt: DateTime.now().toUtc().add(const Duration(hours: 1)),
            refreshExpiresAt: DateTime.now().toUtc().add(const Duration(days: 30)),
            patientId: 'pat_001',
            deviceId: 'dev_test_atlas',
            scope: 'cascade:intake cascade:run cycle:read reports:read',
          ),
          status: AuthSessionStatus.authenticated,
        );

        final vault = VaultService(storage: memoryVaultStorage, authSessionManager: authManager);
        await vault.initFuture;

        // Pre-populate vault with client reports to verify network failure does not erase them
        final reports = _sampleClientReportsJson(cycleId: 'cycle_100')
            .map((j) => ClientModelReportResponse.fromJson(j))
            .toList();
        await vault.recordClientReports(
          cycleId: 'cycle_100',
          reports: reports,
          expectedPatientId: 'pat_001',
        );

        await tester.pumpWidget(
          _buildAtlasTestApp(
            apiClient: client,
            authManager: authManager,
            vaultService: vault,
          ),
        );

        await tester.pumpAndSettle();

        // 1. Error badge is displayed on network failure
        expect(find.byKey(const Key('consilium_status_error')), findsOneWidget);
        expect(find.textContaining('Связь прервана'), findsOneWidget);

        // 2. CRITICAL: Previously recorded vault data is NOT deleted on network error!
        expect(vault.isConsiliumCompleted, isTrue);
        expect(vault.cachedOverallReport, isNotNull);
        expect(vault.getClientReportForSystem('cvs'), isNotNull);

        // 3. User can still view existing reports in CVS folder dialog even during network outage
        final cvsBtn = find.byKey(const Key('folder_btn_cvs'));
        await tester.tap(cvsBtn);
        await tester.pumpAndSettle();

        expect(find.text('SYSTEM_LOG :: CVS'), findsOneWidget);
        expect(find.textContaining('Нормосистолия, патологических нарушений'), findsOneWidget);

        await tester.tap(find.text('CLOSE'));
        await tester.pumpAndSettle();
      });

      testWidgets('Zero direct Gemini calls are triggered from AtlasScreen flow', (tester) async {
        final brain = BrainService();
        expect(brain.analysisProgress, equals(0.0));

        final mockClient = MockClient((request) async {
          if (request.url.path == '/api/v1/client/cascade/status') {
            return http.Response(
              json.encode({
                'patient_id': 'pat_001',
                'device_id': 'dev_test_atlas',
                'has_active_cycle': true,
                'active_cycle_id': 'cycle_100',
                'is_primary_consilium_completed': true,
              }),
              200,
              headers: {'content-type': 'application/json'},
            );
          }
          if (request.url.path == '/api/v1/reports/client') {
            return http.Response(
              json.encode(_sampleClientReportsJson(cycleId: 'cycle_100')),
              200,
              headers: {'content-type': 'application/json'},
            );
          }
          return http.Response('Not Found', 404);
        });

        final client = AegisApiClient(baseUrl: 'http://127.0.0.1:8000', client: mockClient);
        final authManager = AuthSessionManager(apiClient: client, storage: memoryAuthStorage);
        authManager.setSessionForTesting(
          AuthSession(
            accessToken: _buildTestJwt(patientId: 'pat_001'),
            refreshToken: 'refresh_tok',
            expiresIn: 3600,
            expiresAt: DateTime.now().toUtc().add(const Duration(hours: 1)),
            refreshExpiresAt: DateTime.now().toUtc().add(const Duration(days: 30)),
            patientId: 'pat_001',
            deviceId: 'dev_test_atlas',
            scope: 'cascade:intake cascade:run cycle:read reports:read',
          ),
          status: AuthSessionStatus.authenticated,
        );

        final vault = VaultService(storage: memoryVaultStorage, authSessionManager: authManager);
        await vault.initFuture;

        await tester.pumpWidget(
          _buildAtlasTestApp(
            apiClient: client,
            authManager: authManager,
            vaultService: vault,
            brainService: brain,
          ),
        );

        await tester.pumpAndSettle();

        // Verify brain progress remains 0.0 (BrainService pipeline was never executed)
        expect(brain.analysisProgress, equals(0.0));
      });

      testWidgets('Server pending status for current cycle does not show completed conclusion of past cycle', (tester) async {
        final mockClient = MockClient((request) async {
          if (request.url.path == '/api/v1/client/cascade/status') {
            return http.Response(
              json.encode({
                'patient_id': 'pat_001',
                'device_id': 'dev_test_atlas',
                'has_active_cycle': true,
                'active_cycle_id': 'cycle_200',
                'is_primary_consilium_completed': false, // Cycle 200 is pending!
              }),
              200,
              headers: {'content-type': 'application/json'},
            );
          }
          return http.Response('Not Found', 404);
        });

        final client = AegisApiClient(baseUrl: 'http://127.0.0.1:8000', client: mockClient);
        final authManager = AuthSessionManager(apiClient: client, storage: memoryAuthStorage);
        authManager.setSessionForTesting(
          AuthSession(
            accessToken: _buildTestJwt(patientId: 'pat_001'),
            refreshToken: 'refresh_tok',
            expiresIn: 3600,
            expiresAt: DateTime.now().toUtc().add(const Duration(hours: 1)),
            refreshExpiresAt: DateTime.now().toUtc().add(const Duration(days: 30)),
            patientId: 'pat_001',
            deviceId: 'dev_test_atlas',
            scope: 'cascade:intake cascade:run cycle:read reports:read',
          ),
          status: AuthSessionStatus.authenticated,
        );

        final vault = VaultService(storage: memoryVaultStorage, authSessionManager: authManager);
        await vault.initFuture;

        // Pre-populate with previous completed cycle_100
        final pastReports = _sampleClientReportsJson(cycleId: 'cycle_100')
            .map((j) => ClientModelReportResponse.fromJson(j))
            .toList();
        await vault.recordClientReports(cycleId: 'cycle_100', reports: pastReports, expectedPatientId: 'pat_001');
        expect(vault.isConsiliumCompleted, isTrue);

        await tester.pumpWidget(
          _buildAtlasTestApp(
            apiClient: client,
            authManager: authManager,
            vaultService: vault,
          ),
        );
        await tester.pumpAndSettle();

        // Must display pending badge, NOT ready
        expect(find.byKey(const Key('consilium_status_pending')), findsOneWidget);
        expect(find.byKey(const Key('consilium_status_ready')), findsNothing);
        expect(vault.isConsiliumCompleted, isFalse);
        expect(vault.consiliumCycleId, equals('cycle_200'));

        // Cascade FAB dialog must show PENDING, not past cycle conclusion
        final cascadeFab = find.byKey(const Key('cascade_vault_fab'));
        await tester.tap(cascadeFab);
        await tester.pumpAndSettle();

        expect(find.text('STATUS: PENDING CONSILIUM'), findsOneWidget);
        expect(find.textContaining('Primary Consilium analysis has not been performed yet'), findsOneWidget);
      });

      testWidgets('Incomplete reports from server sets error badge and does not mark consilium ready', (tester) async {
        final mockClient = MockClient((request) async {
          if (request.url.path == '/api/v1/client/cascade/status') {
            return http.Response(
              json.encode({
                'patient_id': 'pat_001',
                'device_id': 'dev_test_atlas',
                'has_active_cycle': true,
                'active_cycle_id': 'cycle_100',
                'is_primary_consilium_completed': true,
              }),
              200,
              headers: {'content-type': 'application/json'},
            );
          }
          if (request.url.path == '/api/v1/reports/client') {
            // Return empty list of reports (incomplete)
            return http.Response('[]', 200, headers: {'content-type': 'application/json'});
          }
          return http.Response('Not Found', 404);
        });

        final client = AegisApiClient(baseUrl: 'http://127.0.0.1:8000', client: mockClient);
        final authManager = AuthSessionManager(apiClient: client, storage: memoryAuthStorage);
        authManager.setSessionForTesting(
          AuthSession(
            accessToken: _buildTestJwt(patientId: 'pat_001'),
            refreshToken: 'refresh_tok',
            expiresIn: 3600,
            expiresAt: DateTime.now().toUtc().add(const Duration(hours: 1)),
            refreshExpiresAt: DateTime.now().toUtc().add(const Duration(days: 30)),
            patientId: 'pat_001',
            deviceId: 'dev_test_atlas',
            scope: 'cascade:intake cascade:run cycle:read reports:read',
          ),
          status: AuthSessionStatus.authenticated,
        );

        final vault = VaultService(storage: memoryVaultStorage, authSessionManager: authManager);
        await vault.initFuture;

        await tester.pumpWidget(
          _buildAtlasTestApp(
            apiClient: client,
            authManager: authManager,
            vaultService: vault,
          ),
        );
        await tester.pumpAndSettle();

        // Must display error badge
        expect(find.byKey(const Key('consilium_status_error')), findsOneWidget);
        expect(find.byKey(const Key('consilium_status_ready')), findsNothing);
        expect(vault.isConsiliumCompleted, isFalse);
      });

      testWidgets('Vault storage error displays error badge and prevents success announcement', (tester) async {
        final mockClient = MockClient((request) async {
          if (request.url.path == '/api/v1/client/cascade/status') {
            return http.Response(
              json.encode({
                'patient_id': 'pat_001',
                'device_id': 'dev_test_atlas',
                'has_active_cycle': true,
                'active_cycle_id': 'cycle_100',
                'is_primary_consilium_completed': true,
              }),
              200,
              headers: {'content-type': 'application/json'},
            );
          }
          if (request.url.path == '/api/v1/reports/client') {
            return http.Response(
              json.encode(_sampleClientReportsJson(cycleId: 'cycle_100')),
              200,
              headers: {'content-type': 'application/json'},
            );
          }
          return http.Response('Not Found', 404);
        });

        final client = AegisApiClient(baseUrl: 'http://127.0.0.1:8000', client: mockClient);
        final authManager = AuthSessionManager(apiClient: client, storage: memoryAuthStorage);
        authManager.setSessionForTesting(
          AuthSession(
            accessToken: _buildTestJwt(patientId: 'pat_001'),
            refreshToken: 'refresh_tok',
            expiresIn: 3600,
            expiresAt: DateTime.now().toUtc().add(const Duration(hours: 1)),
            refreshExpiresAt: DateTime.now().toUtc().add(const Duration(days: 30)),
            patientId: 'pat_001',
            deviceId: 'dev_test_atlas',
            scope: 'cascade:intake cascade:run cycle:read reports:read',
          ),
          status: AuthSessionStatus.authenticated,
        );

        final vault = VaultService(storage: memoryVaultStorage, authSessionManager: authManager);
        await vault.initFuture;

        // Fail vault storage writes
        memoryVaultStorage.failWithException = true;

        await tester.pumpWidget(
          _buildAtlasTestApp(
            apiClient: client,
            authManager: authManager,
            vaultService: vault,
          ),
        );
        await tester.pumpAndSettle();

        // Must display error badge
        expect(find.byKey(const Key('consilium_status_error')), findsOneWidget);
        expect(find.byKey(const Key('consilium_status_ready')), findsNothing);
      });
    });
  });
}
