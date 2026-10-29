// ignore_for_file: depend_on_referenced_packages
import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:provider/provider.dart';

import 'package:aegis_health/main.dart';
import 'package:aegis_health/screens/aegis_consilium_screen.dart';
import 'package:aegis_health/services/aegis_cascade_controller.dart';
import 'package:aegis_health/services/api/aegis_api_client.dart';
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
  String deviceId = 'dev_test_consilium',
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

/// Helper to build sample Primary Consilium response JSON matching backend
Map<String, dynamic> _samplePrimaryConsiliumResponseJson({
  required String cycleId,
  String reportDate = '2026-10-29',
}) {
  return {
    'cycle_id': cycleId,
    'report_date': reportDate,
    'overall_report': {
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
        ],
        'metrics': {
          'min_pessimum_bpm': 45,
          'max_pessimum_bpm': 165,
        },
      },
      'created_at': '${reportDate}T08:00:00Z',
    },
    'system_reports': {
      'cvs': {
        'report_id': 'rep_${cycleId}_cvs',
        'cycle_id': cycleId,
        'model': 'gemini-3.6-flash',
        'report_type': 'system_user_report_cvs',
        'privacy_level': 'client_facing',
        'version': 1,
        'content': {
          'title': 'Сердечно-сосудистая система',
          'summary': 'Нормосистолия, гемодинамика компенсаторно адекватна.',
          'system_key': 'cvs',
          'recommendations': ['Кардиомониторинг в покое'],
        },
        'created_at': '${reportDate}T08:00:00Z',
      },
    },
    'vitals_thresholds': {
      'min_pessimum_bpm': 45,
      'max_pessimum_bpm': 165,
      'aerobic_zone_min_bpm': 110,
      'aerobic_zone_max_bpm': 140,
    },
  };
}

/// Helper to build sample client reports for getClientReports
List<Map<String, dynamic>> _sampleClientReportsJson({required String cycleId}) {
  final primary = _samplePrimaryConsiliumResponseJson(cycleId: cycleId);
  return [
    primary['overall_report'] as Map<String, dynamic>,
    ...((primary['system_reports'] as Map<String, dynamic>).values.map((v) => v as Map<String, dynamic>)),
  ];
}

/// Helper for building AegisConsiliumScreen test harness with full navigation support
Widget _buildConsiliumTestApp({
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
    child: MaterialApp(
      home: AegisConsiliumScreen(
        apiClient: apiClient,
        authSessionManager: authManager,
        vaultService: vaultService,
      ),
    ),
  );
}

/// Helper for building AtlasScreen test harness
Widget _buildAtlasTestApp({
  required AegisApiClient apiClient,
  required AuthSessionManager authManager,
  required VaultService vaultService,
}) {
  final time = TimeService();
  final brain = BrainService();
  final vitals = VitalsMonitorService(time, vaultService);
  final health = HealthDataProvider(vaultService: vaultService, vitalsMonitorService: vitals);

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

  group('PROJECT AEGIS — STAGE 5.4E-3: EXPLICIT PRIMARY CONSILIUM TRIGGER & STATUS', () {
    // -------------------------------------------------------------------------
    // 1. PRE-FLIGHT READINESS CHECK & EXPLICIT TRIGGER
    // -------------------------------------------------------------------------
    testWidgets('1. Missing preparatory anamnesis shows contextMissing and blocks launch', (tester) async {
      int statusCheckCalls = 0;
      int runPrimaryCalls = 0;

      final mockClient = MockClient((request) async {
        if (request.url.path == '/api/v1/client/cascade/status') {
          statusCheckCalls++;
          return http.Response(
            json.encode({
              'patient_id': 'pat_001',
              'device_id': 'dev_test_consilium',
              'has_active_cycle': true,
              'active_cycle_id': 'cycle_100',
              'has_preparatory_anamnesis': false, // Context missing!
              'has_preparatory_labs': false,
              'has_preparatory_ecg': false,
              'is_primary_consilium_completed': false,
            }),
            200,
            headers: {'content-type': 'application/json'},
          );
        }
        if (request.url.path == '/api/v1/client/cascade/run-primary') {
          runPrimaryCalls++;
          return http.Response('{}', 200);
        }
        return http.Response('Not Found', 404);
      });

      final client = AegisApiClient(baseUrl: 'http://127.0.0.1:8000', client: mockClient);
      final authManager = AuthSessionManager(apiClient: client, storage: memoryAuthStorage);
      authManager.setSessionForTesting(
        AuthSession(
          accessToken: _buildTestJwt(patientId: 'pat_001'),
          refreshToken: 'ref_tok',
          expiresIn: 3600,
          expiresAt: DateTime.now().toUtc().add(const Duration(hours: 1)),
          refreshExpiresAt: DateTime.now().toUtc().add(const Duration(days: 30)),
          patientId: 'pat_001',
          deviceId: 'dev_test_consilium',
          scope: 'cascade:intake cascade:run cycle:read reports:read',
        ),
        status: AuthSessionStatus.authenticated,
      );

      final vault = VaultService(storage: memoryVaultStorage, authSessionManager: authManager);
      await vault.initFuture;

      await tester.pumpWidget(
        _buildConsiliumTestApp(
          apiClient: client,
          authManager: authManager,
          vaultService: vault,
        ),
      );
      await tester.pumpAndSettle();

      // Verified: status checked once
      expect(statusCheckCalls, equals(1));

      // Verified: run-primary was NEVER called
      expect(runPrimaryCalls, equals(0));

      // Verified: run button is NOT rendered
      expect(find.byKey(const Key('run_consilium_btn')), findsNothing);

      // Verified: clear explanation of missing M1 anamnesis
      expect(find.textContaining('preparatory anamnesis (M1) is missing'), findsOneWidget);
    });

    testWidgets('2. Verified cycle readiness enables explicit user launch only with stable key', (tester) async {
      int statusCheckCalls = 0;
      int runPrimaryCalls = 0;
      bool isConsiliumDone = false;
      String? recordedIdempotencyKey;

      final mockClient = MockClient((request) async {
        if (request.url.path == '/api/v1/client/cascade/status') {
          statusCheckCalls++;
          return http.Response(
            json.encode({
              'patient_id': 'pat_001',
              'device_id': 'dev_test_consilium',
              'has_active_cycle': true,
              'active_cycle_id': 'cycle_100',
              'has_preparatory_anamnesis': true, // Context ready!
              'has_preparatory_labs': true,
              'has_preparatory_ecg': true,
              'is_primary_consilium_completed': isConsiliumDone,
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
        if (request.url.path == '/api/v1/client/cascade/run-primary') {
          runPrimaryCalls++;
          isConsiliumDone = true;
          recordedIdempotencyKey = request.headers['Idempotency-Key'];
          return http.Response(
            json.encode(_samplePrimaryConsiliumResponseJson(cycleId: 'cycle_100')),
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
          refreshToken: 'ref_tok',
          expiresIn: 3600,
          expiresAt: DateTime.now().toUtc().add(const Duration(hours: 1)),
          refreshExpiresAt: DateTime.now().toUtc().add(const Duration(days: 30)),
          patientId: 'pat_001',
          deviceId: 'dev_test_consilium',
          scope: 'cascade:intake cascade:run cycle:read reports:read',
        ),
        status: AuthSessionStatus.authenticated,
      );

      final vault = VaultService(storage: memoryVaultStorage, authSessionManager: authManager);
      await vault.initFuture;

      await tester.pumpWidget(
        _buildConsiliumTestApp(
          apiClient: client,
          authManager: authManager,
          vaultService: vault,
        ),
      );
      await tester.pumpAndSettle();

      // Zero runs passively on load
      expect(statusCheckCalls, equals(1));
      expect(runPrimaryCalls, equals(0));

      // Launch button is present
      final runBtn = find.byKey(const Key('run_consilium_btn'));
      expect(runBtn, findsOneWidget);

      // Explicit user tap
      await tester.tap(runBtn);
      await tester.pumpAndSettle();

      // Consilium run dispatched exactly once with stable cycle key
      expect(runPrimaryCalls, equals(1));
      expect(recordedIdempotencyKey, equals('run_primary_cycle_100'));

      // Vault is updated and consilium marked completed
      expect(vault.isConsiliumCompleted, isTrue);
      expect(vault.cachedOverallReport, isNotNull);
      expect(vault.cachedOverallReport!.content.title, contains('Интегральное заключение'));
    });

    // -------------------------------------------------------------------------
    // 2. DOUBLE-TAP GUARD
    // -------------------------------------------------------------------------
    testWidgets('3. Rapid concurrent taps trigger exactly one HTTP execution request', (tester) async {
      int runPrimaryCalls = 0;
      bool isConsiliumDone = false;
      final completer = Completer<http.Response>();

      final mockClient = MockClient((request) async {
        if (request.url.path == '/api/v1/client/cascade/status') {
          return http.Response(
            json.encode({
              'patient_id': 'pat_001',
              'device_id': 'dev_test_consilium',
              'has_active_cycle': true,
              'active_cycle_id': 'cycle_100',
              'has_preparatory_anamnesis': true,
              'is_primary_consilium_completed': isConsiliumDone,
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
        if (request.url.path == '/api/v1/client/cascade/run-primary') {
          runPrimaryCalls++;
          isConsiliumDone = true;
          return completer.future; // In-flight delay
        }
        return http.Response('Not Found', 404);
      });

      final client = AegisApiClient(baseUrl: 'http://127.0.0.1:8000', client: mockClient);
      final authManager = AuthSessionManager(apiClient: client, storage: memoryAuthStorage);
      authManager.setSessionForTesting(
        AuthSession(
          accessToken: _buildTestJwt(patientId: 'pat_001'),
          refreshToken: 'ref_tok',
          expiresIn: 3600,
          expiresAt: DateTime.now().toUtc().add(const Duration(hours: 1)),
          refreshExpiresAt: DateTime.now().toUtc().add(const Duration(days: 30)),
          patientId: 'pat_001',
          deviceId: 'dev_test_consilium',
          scope: 'cascade:intake cascade:run cycle:read reports:read',
        ),
        status: AuthSessionStatus.authenticated,
      );

      final vault = VaultService(storage: memoryVaultStorage, authSessionManager: authManager);
      await vault.initFuture;

      await tester.pumpWidget(
        _buildConsiliumTestApp(
          apiClient: client,
          authManager: authManager,
          vaultService: vault,
        ),
      );
      await tester.pumpAndSettle();

      final runBtn = find.byKey(const Key('run_consilium_btn'));
      expect(runBtn, findsOneWidget);

      // First tap begins execution
      await tester.tap(runBtn);
      await tester.pump(); // Start execution

      // Verified: State is executing
      expect(find.byKey(const Key('consilium_executing_indicator')), findsOneWidget);

      // Attempt second tap while executing
      await tester.tap(find.byKey(const Key('consilium_executing_indicator')));
      await tester.pump();

      // Complete the pending HTTP call
      completer.complete(http.Response(
        json.encode(_samplePrimaryConsiliumResponseJson(cycleId: 'cycle_100')),
        200,
        headers: {'content-type': 'application/json'},
      ));
      await tester.pumpAndSettle();

      // Strictly only 1 call made
      expect(runPrimaryCalls, equals(1));
      expect(vault.isConsiliumCompleted, isTrue);
    });

    // -------------------------------------------------------------------------
    // 3. 409 CONFLICT (LEASE HELD) & RETRY WITH STABLE KEY
    // -------------------------------------------------------------------------
    testWidgets('4. HTTP 409 lease concurrency shows temporarilyBusy and retry reuses stable key', (tester) async {
      int runPrimaryAttempts = 0;
      bool isConsiliumDone = false;
      final List<String?> keysUsed = [];

      final mockClient = MockClient((request) async {
        if (request.url.path == '/api/v1/client/cascade/status') {
          return http.Response(
            json.encode({
              'patient_id': 'pat_001',
              'device_id': 'dev_test_consilium',
              'has_active_cycle': true,
              'active_cycle_id': 'cycle_100',
              'has_preparatory_anamnesis': true,
              'is_primary_consilium_completed': isConsiliumDone,
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
        if (request.url.path == '/api/v1/client/cascade/run-primary') {
          runPrimaryAttempts++;
          keysUsed.add(request.headers['Idempotency-Key']);
          if (runPrimaryAttempts == 1) {
            // First attempt: 409 Conflict with lease explanation
            return http.Response(
              json.encode({'detail': 'Primary consilium already in-flight with active lease: lease_worker_1'}),
              409,
              headers: {'content-type': 'application/json'},
            );
          } else {
            // Second attempt: Success
            isConsiliumDone = true;
            return http.Response(
              json.encode(_samplePrimaryConsiliumResponseJson(cycleId: 'cycle_100')),
              200,
              headers: {'content-type': 'application/json'},
            );
          }
        }
        return http.Response('Not Found', 404);
      });

      final client = AegisApiClient(baseUrl: 'http://127.0.0.1:8000', client: mockClient);
      final authManager = AuthSessionManager(apiClient: client, storage: memoryAuthStorage);
      authManager.setSessionForTesting(
        AuthSession(
          accessToken: _buildTestJwt(patientId: 'pat_001'),
          refreshToken: 'ref_tok',
          expiresIn: 3600,
          expiresAt: DateTime.now().toUtc().add(const Duration(hours: 1)),
          refreshExpiresAt: DateTime.now().toUtc().add(const Duration(days: 30)),
          patientId: 'pat_001',
          deviceId: 'dev_test_consilium',
          scope: 'cascade:intake cascade:run cycle:read reports:read',
        ),
        status: AuthSessionStatus.authenticated,
      );

      final vault = VaultService(storage: memoryVaultStorage, authSessionManager: authManager);
      await vault.initFuture;

      await tester.pumpWidget(
        _buildConsiliumTestApp(
          apiClient: client,
          authManager: authManager,
          vaultService: vault,
        ),
      );
      await tester.pumpAndSettle();

      // Launch consilium
      await tester.tap(find.byKey(const Key('run_consilium_btn')));
      await tester.pumpAndSettle();

      // Shows temporarily busy state with retry button
      expect(find.textContaining('another analysis process holds the execution lock'), findsOneWidget);
      expect(vault.isConsiliumCompleted, isFalse);

      final retryBtn = find.byKey(const Key('retry_busy_btn'));
      expect(retryBtn, findsOneWidget);

      // Tap retry
      await tester.tap(retryBtn);
      await tester.pumpAndSettle();

      // Succeeded on retry
      expect(runPrimaryAttempts, equals(2));
      expect(keysUsed.length, equals(2));
      expect(keysUsed[0], equals('run_primary_cycle_100'));
      expect(keysUsed[1], equals('run_primary_cycle_100')); // Exact same stable key!
      expect(vault.isConsiliumCompleted, isTrue);
    });

    // -------------------------------------------------------------------------
    // 4. 401 AUTH REFRESH RETRY
    // -------------------------------------------------------------------------
    testWidgets('5. HTTP 401 triggers single token refresh and retries successfully', (tester) async {
      int runCalls = 0;
      int refreshCalls = 0;
      bool isConsiliumDone = false;

      final now = DateTime.now().toUtc();

      final mockClient = MockClient((request) async {
        if (request.url.path == '/api/v1/client/cascade/status') {
          return http.Response(
            json.encode({
              'patient_id': 'pat_001',
              'device_id': 'dev_test_consilium',
              'has_active_cycle': true,
              'active_cycle_id': 'cycle_100',
              'has_preparatory_anamnesis': true,
              'is_primary_consilium_completed': isConsiliumDone,
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
        if (request.url.path == '/api/v1/client/cascade/run-primary') {
          runCalls++;
          if (runCalls == 1) {
            return http.Response(json.encode({'detail': 'Token expired'}), 401, headers: {'content-type': 'application/json'});
          }
          isConsiliumDone = true;
          return http.Response(
            json.encode(_samplePrimaryConsiliumResponseJson(cycleId: 'cycle_100')),
            200,
            headers: {'content-type': 'application/json'},
          );
        }
        if (request.url.path == '/api/v1/auth/refresh') {
          refreshCalls++;
          return http.Response(
            json.encode({
              'access_token': _buildTestJwt(patientId: 'pat_001', jti: 'jti_refreshed'),
              'token_type': 'Bearer',
              'refresh_token': 'ref_tok_new',
              'expires_in': 3600,
              'expires_at': now.add(const Duration(hours: 1)).toIso8601String(),
              'refresh_expires_at': now.add(const Duration(days: 30)).toIso8601String(),
              'patient_id': 'pat_001',
              'device_id': 'dev_test_consilium',
              'scope': 'cascade:intake cascade:run cycle:read reports:read',
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
          refreshToken: 'ref_tok',
          expiresIn: 3600,
          expiresAt: DateTime.now().toUtc().add(const Duration(hours: 1)),
          refreshExpiresAt: DateTime.now().toUtc().add(const Duration(days: 30)),
          patientId: 'pat_001',
          deviceId: 'dev_test_consilium',
          scope: 'cascade:intake cascade:run cycle:read reports:read',
        ),
        status: AuthSessionStatus.authenticated,
      );

      final vault = VaultService(storage: memoryVaultStorage, authSessionManager: authManager);
      await vault.initFuture;

      await tester.pumpWidget(
        _buildConsiliumTestApp(
          apiClient: client,
          authManager: authManager,
          vaultService: vault,
        ),
      );
      await tester.pumpAndSettle();

      await tester.tap(find.byKey(const Key('run_consilium_btn')));
      await tester.pumpAndSettle();

      expect(runCalls, equals(2));
      expect(refreshCalls, equals(1));
      expect(vault.isConsiliumCompleted, isTrue);
    });

    // -------------------------------------------------------------------------
    // 5. PATIENT SWITCHING ATTRIBUTION FENCE
    // -------------------------------------------------------------------------
    testWidgets('6. Patient switch during execution aborts response attribution', (tester) async {
      final completer = Completer<http.Response>();

      final mockClient = MockClient((request) async {
        if (request.url.path == '/api/v1/client/cascade/status') {
          return http.Response(
            json.encode({
              'patient_id': 'pat_001',
              'device_id': 'dev_test_consilium',
              'has_active_cycle': true,
              'active_cycle_id': 'cycle_100',
              'has_preparatory_anamnesis': true,
              'is_primary_consilium_completed': false,
            }),
            200,
            headers: {'content-type': 'application/json'},
          );
        }
        if (request.url.path == '/api/v1/client/cascade/run-primary') {
          return completer.future; // Hold request
        }
        return http.Response('Not Found', 404);
      });

      final client = AegisApiClient(baseUrl: 'http://127.0.0.1:8000', client: mockClient);
      final authManager = AuthSessionManager(apiClient: client, storage: memoryAuthStorage);
      authManager.setSessionForTesting(
        AuthSession(
          accessToken: _buildTestJwt(patientId: 'pat_001'),
          refreshToken: 'ref_tok_1',
          expiresIn: 3600,
          expiresAt: DateTime.now().toUtc().add(const Duration(hours: 1)),
          refreshExpiresAt: DateTime.now().toUtc().add(const Duration(days: 30)),
          patientId: 'pat_001',
          deviceId: 'dev_test_consilium',
          scope: 'cascade:intake cascade:run cycle:read reports:read',
        ),
        status: AuthSessionStatus.authenticated,
      );

      final vault = VaultService(storage: memoryVaultStorage, authSessionManager: authManager);
      await vault.initFuture;

      await tester.pumpWidget(
        _buildConsiliumTestApp(
          apiClient: client,
          authManager: authManager,
          vaultService: vault,
        ),
      );
      await tester.pumpAndSettle();

      // Launch for patient 1
      await tester.tap(find.byKey(const Key('run_consilium_btn')));
      await tester.pump();

      // Active patient session switches to pat_002
      authManager.setSessionForTesting(
        AuthSession(
          accessToken: _buildTestJwt(patientId: 'pat_002'),
          refreshToken: 'ref_tok_2',
          expiresIn: 3600,
          expiresAt: DateTime.now().toUtc().add(const Duration(hours: 1)),
          refreshExpiresAt: DateTime.now().toUtc().add(const Duration(days: 30)),
          patientId: 'pat_002',
          deviceId: 'dev_test_consilium',
          scope: 'cascade:intake cascade:run cycle:read reports:read',
        ),
        status: AuthSessionStatus.authenticated,
      );

      // Now server responds to pat_001 call
      completer.complete(http.Response(
        json.encode(_samplePrimaryConsiliumResponseJson(cycleId: 'cycle_100')),
        200,
        headers: {'content-type': 'application/json'},
      ));
      await tester.pumpAndSettle();

      // pat_002's Vault must NOT be contaminated
      expect(vault.isConsiliumCompleted, isFalse);
    });

    // -------------------------------------------------------------------------
    // 6. NETWORK OFFLINE / TIMEOUT
    // -------------------------------------------------------------------------
    testWidgets('7. Network failure transitions to error state and preserves data', (tester) async {
      int attempts = 0;

      final mockClient = MockClient((request) async {
        if (request.url.path == '/api/v1/client/cascade/status') {
          return http.Response(
            json.encode({
              'patient_id': 'pat_001',
              'device_id': 'dev_test_consilium',
              'has_active_cycle': true,
              'active_cycle_id': 'cycle_100',
              'has_preparatory_anamnesis': true,
              'is_primary_consilium_completed': false,
            }),
            200,
            headers: {'content-type': 'application/json'},
          );
        }
        if (request.url.path == '/api/v1/client/cascade/run-primary') {
          attempts++;
          throw const SocketException('Connection reset by peer');
        }
        return http.Response('Not Found', 404);
      });

      final client = AegisApiClient(baseUrl: 'http://127.0.0.1:8000', client: mockClient);
      final authManager = AuthSessionManager(apiClient: client, storage: memoryAuthStorage);
      authManager.setSessionForTesting(
        AuthSession(
          accessToken: _buildTestJwt(patientId: 'pat_001'),
          refreshToken: 'ref_tok',
          expiresIn: 3600,
          expiresAt: DateTime.now().toUtc().add(const Duration(hours: 1)),
          refreshExpiresAt: DateTime.now().toUtc().add(const Duration(days: 30)),
          patientId: 'pat_001',
          deviceId: 'dev_test_consilium',
          scope: 'cascade:intake cascade:run cycle:read reports:read',
        ),
        status: AuthSessionStatus.authenticated,
      );

      final vault = VaultService(storage: memoryVaultStorage, authSessionManager: authManager);
      await vault.initFuture;

      await tester.pumpWidget(
        _buildConsiliumTestApp(
          apiClient: client,
          authManager: authManager,
          vaultService: vault,
        ),
      );
      await tester.pumpAndSettle();

      await tester.tap(find.byKey(const Key('run_consilium_btn')));
      await tester.pumpAndSettle();

      expect(attempts, equals(1));
      expect(find.byKey(const Key('retry_consilium_btn')), findsOneWidget);
      expect(find.textContaining('Network connection failed'), findsOneWidget);
      expect(vault.isConsiliumCompleted, isFalse);
    });

    // -------------------------------------------------------------------------
    // 7. ALREADY COMPLETED RECOVERY (IDEMPOTENT REPLAY)
    // -------------------------------------------------------------------------
    testWidgets('8. Server completed cycle recovers reports without re-running Gemini', (tester) async {
      int runPrimaryCalls = 0;
      int getReportsCalls = 0;

      final mockClient = MockClient((request) async {
        if (request.url.path == '/api/v1/client/cascade/status') {
          return http.Response(
            json.encode({
              'patient_id': 'pat_001',
              'device_id': 'dev_test_consilium',
              'has_active_cycle': true,
              'active_cycle_id': 'cycle_completed',
              'has_preparatory_anamnesis': true,
              'is_primary_consilium_completed': true, // Already completed!
            }),
            200,
            headers: {'content-type': 'application/json'},
          );
        }
        if (request.url.path == '/api/v1/reports/client') {
          getReportsCalls++;
          return http.Response(
            json.encode(_sampleClientReportsJson(cycleId: 'cycle_completed')),
            200,
            headers: {'content-type': 'application/json'},
          );
        }
        if (request.url.path == '/api/v1/client/cascade/run-primary') {
          runPrimaryCalls++;
          return http.Response('{}', 200);
        }
        return http.Response('Not Found', 404);
      });

      final client = AegisApiClient(baseUrl: 'http://127.0.0.1:8000', client: mockClient);
      final authManager = AuthSessionManager(apiClient: client, storage: memoryAuthStorage);
      authManager.setSessionForTesting(
        AuthSession(
          accessToken: _buildTestJwt(patientId: 'pat_001'),
          refreshToken: 'ref_tok',
          expiresIn: 3600,
          expiresAt: DateTime.now().toUtc().add(const Duration(hours: 1)),
          refreshExpiresAt: DateTime.now().toUtc().add(const Duration(days: 30)),
          patientId: 'pat_001',
          deviceId: 'dev_test_consilium',
          scope: 'cascade:intake cascade:run cycle:read reports:read',
        ),
        status: AuthSessionStatus.authenticated,
      );

      final vault = VaultService(storage: memoryVaultStorage, authSessionManager: authManager);
      await vault.initFuture;

      await tester.pumpWidget(
        _buildConsiliumTestApp(
          apiClient: client,
          authManager: authManager,
          vaultService: vault,
        ),
      );
      await tester.pumpAndSettle();

      expect(runPrimaryCalls, equals(0));
      expect(getReportsCalls, equals(1));
      expect(vault.isConsiliumCompleted, isTrue);
      expect(find.textContaining('already concluded'), findsOneWidget);
    });

    // -------------------------------------------------------------------------
    // 8. ATLAS SCREEN EXPLICIT TRIGGER INTEGRATION
    // -------------------------------------------------------------------------
    testWidgets('9. Atlas Screen provides explicit trigger button in Cascade dialog and updates badge', (tester) async {
      int runPrimaryCalls = 0;

      final mockClient = MockClient((request) async {
        if (request.url.path == '/api/v1/client/cascade/status') {
          return http.Response(
            json.encode({
              'patient_id': 'pat_001',
              'device_id': 'dev_test_consilium',
              'has_active_cycle': true,
              'active_cycle_id': 'cycle_atlas',
              'has_preparatory_anamnesis': true, // Ready to run!
              'is_primary_consilium_completed': false,
            }),
            200,
            headers: {'content-type': 'application/json'},
          );
        }
        if (request.url.path == '/api/v1/client/cascade/run-primary') {
          runPrimaryCalls++;
          return http.Response(
            json.encode(_samplePrimaryConsiliumResponseJson(cycleId: 'cycle_atlas')),
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
          refreshToken: 'ref_tok',
          expiresIn: 3600,
          expiresAt: DateTime.now().toUtc().add(const Duration(hours: 1)),
          refreshExpiresAt: DateTime.now().toUtc().add(const Duration(days: 30)),
          patientId: 'pat_001',
          deviceId: 'dev_test_consilium',
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

      // Zero consilium calls on opening Atlas
      expect(runPrimaryCalls, equals(0));
      expect(find.byKey(const Key('consilium_status_pending')), findsOneWidget);

      // Open Cascade Vault dialog
      final cascadeFab = find.byKey(const Key('cascade_vault_fab'));
      expect(cascadeFab, findsOneWidget);
      await tester.tap(cascadeFab);
      await tester.pumpAndSettle();

      // Verified: dialog shows "STATUS: PENDING CONSILIUM" and context verified
      expect(find.text('STATUS: PENDING CONSILIUM'), findsOneWidget);
      expect(find.textContaining('Context verified: ready to launch'), findsOneWidget);

      // Explicit trigger button is present
      final runAtlasBtn = find.byKey(const Key('run_primary_consilium_btn'));
      expect(runAtlasBtn, findsOneWidget);

      // User explicitly taps button
      await tester.tap(runAtlasBtn);
      await tester.pumpAndSettle();

      // Consilium executed exactly once with stable atlas key
      expect(runPrimaryCalls, equals(1));
      expect(vault.isConsiliumCompleted, isTrue);

      // Status badge updated to ready
      expect(find.byKey(const Key('consilium_status_ready')), findsOneWidget);
      expect(find.text('Консилиум сформирован'), findsOneWidget);
    });

    // -------------------------------------------------------------------------
    // 9. TIMEOUT RECOVERY: RECOVERS COMPLETED VIA GET WITHOUT SECONDARY POST
    // -------------------------------------------------------------------------
    testWidgets('10. Timeout after server accepted request recovers completed consilium via GET without secondary POST', (tester) async {
      int runPrimaryCalls = 0;
      int getReportsCalls = 0;
      bool serverFinishedConsilium = false;

      final mockClient = MockClient((request) async {
        if (request.url.path == '/api/v1/client/cascade/status') {
          return http.Response(
            json.encode({
              'patient_id': 'pat_001',
              'device_id': 'dev_test_consilium',
              'has_active_cycle': true,
              'active_cycle_id': 'cycle_timeout_rec',
              'has_preparatory_anamnesis': true,
              'is_primary_consilium_completed': serverFinishedConsilium,
            }),
            200,
            headers: {'content-type': 'application/json'},
          );
        }
        if (request.url.path == '/api/v1/client/cascade/run-primary') {
          runPrimaryCalls++;
          // Server accepts the request, begins processing and completes it in background,
          // but the connection dropped / timed out for client
          serverFinishedConsilium = true;
          throw const SocketException('Connection timed out');
        }
        if (request.url.path == '/api/v1/reports/client') {
          getReportsCalls++;
          return http.Response(
            json.encode(_sampleClientReportsJson(cycleId: 'cycle_timeout_rec')),
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
          refreshToken: 'ref_tok',
          expiresIn: 3600,
          expiresAt: DateTime.now().toUtc().add(const Duration(hours: 1)),
          refreshExpiresAt: DateTime.now().toUtc().add(const Duration(days: 30)),
          patientId: 'pat_001',
          deviceId: 'dev_test_consilium',
          scope: 'cascade:intake cascade:run cycle:read reports:read',
        ),
        status: AuthSessionStatus.authenticated,
      );

      final vault = VaultService(storage: memoryVaultStorage, authSessionManager: authManager);
      await vault.initFuture;

      await tester.pumpWidget(
        _buildConsiliumTestApp(
          apiClient: client,
          authManager: authManager,
          vaultService: vault,
        ),
      );
      await tester.pumpAndSettle();

      await tester.tap(find.byKey(const Key('run_consilium_btn')));
      await tester.pumpAndSettle();

      // Exactly 1 POST was made
      expect(runPrimaryCalls, equals(1));

      // Client recovered reports via GET without repeating POST
      expect(getReportsCalls, equals(1));
      expect(vault.isConsiliumCompleted, isTrue);
    });

    // -------------------------------------------------------------------------
    // 10. GET RECOVERY OF COMPLETED CONSILIUM WITHOUT POST
    // -------------------------------------------------------------------------
    testWidgets('11. Trigger on server-completed consilium retrieves reports via GET without any POST', (tester) async {
      int runPrimaryCalls = 0;
      int getReportsCalls = 0;

      final mockClient = MockClient((request) async {
        if (request.url.path == '/api/v1/client/cascade/status') {
          return http.Response(
            json.encode({
              'patient_id': 'pat_001',
              'device_id': 'dev_test_consilium',
              'has_active_cycle': true,
              'active_cycle_id': 'cycle_already_done',
              'has_preparatory_anamnesis': true,
              'is_primary_consilium_completed': true, // Server says done!
            }),
            200,
            headers: {'content-type': 'application/json'},
          );
        }
        if (request.url.path == '/api/v1/client/cascade/run-primary') {
          runPrimaryCalls++;
          return http.Response('{}', 200);
        }
        if (request.url.path == '/api/v1/reports/client') {
          getReportsCalls++;
          return http.Response(
            json.encode(_sampleClientReportsJson(cycleId: 'cycle_already_done')),
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
          refreshToken: 'ref_tok',
          expiresIn: 3600,
          expiresAt: DateTime.now().toUtc().add(const Duration(hours: 1)),
          refreshExpiresAt: DateTime.now().toUtc().add(const Duration(days: 30)),
          patientId: 'pat_001',
          deviceId: 'dev_test_consilium',
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

      // Zero POST calls made
      expect(runPrimaryCalls, equals(0));
      expect(getReportsCalls, equals(1));
      expect(vault.isConsiliumCompleted, isTrue);
    });

    // -------------------------------------------------------------------------
    // 11. LOCAL STORAGE FAILURE DURING RECOVERY SHOWS ERROR
    // -------------------------------------------------------------------------
    testWidgets('12. Local storage failure during recovery displays error and does not announce success', (tester) async {
      final mockClient = MockClient((request) async {
        if (request.url.path == '/api/v1/client/cascade/status') {
          return http.Response(
            json.encode({
              'patient_id': 'pat_001',
              'device_id': 'dev_test_consilium',
              'has_active_cycle': true,
              'active_cycle_id': 'cycle_store_err',
              'has_preparatory_anamnesis': true,
              'is_primary_consilium_completed': true,
            }),
            200,
            headers: {'content-type': 'application/json'},
          );
        }
        if (request.url.path == '/api/v1/reports/client') {
          return http.Response(
            json.encode(_sampleClientReportsJson(cycleId: 'cycle_store_err')),
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
          refreshToken: 'ref_tok',
          expiresIn: 3600,
          expiresAt: DateTime.now().toUtc().add(const Duration(hours: 1)),
          refreshExpiresAt: DateTime.now().toUtc().add(const Duration(days: 30)),
          patientId: 'pat_001',
          deviceId: 'dev_test_consilium',
          scope: 'cascade:intake cascade:run cycle:read reports:read',
        ),
        status: AuthSessionStatus.authenticated,
      );

      final vault = VaultService(storage: memoryVaultStorage, authSessionManager: authManager);
      await vault.initFuture;

      // Simulate local secure storage failure
      memoryVaultStorage.failWithException = true;

      await tester.pumpWidget(
        _buildAtlasTestApp(
          apiClient: client,
          authManager: authManager,
          vaultService: vault,
        ),
      );
      await tester.pumpAndSettle();

      // Must display error badge, NOT success/ready
      expect(find.byKey(const Key('consilium_status_error')), findsOneWidget);
      expect(find.byKey(const Key('consilium_status_ready')), findsNothing);
    });

    // -------------------------------------------------------------------------
    // 12. PATIENT SWITCH DURING GET RECOVERY DISCARDS REPORTS
    // -------------------------------------------------------------------------
    testWidgets('13. Patient switch during GET recovery discards reports and prevents cross-patient contamination', (tester) async {
      final reportsCompleter = Completer<http.Response>();

      final mockClient = MockClient((request) async {
        if (request.url.path == '/api/v1/client/cascade/status') {
          final isPat1 = request.headers['Authorization']?.contains('pat_001') ?? true;
          return http.Response(
            json.encode({
              'patient_id': isPat1 ? 'pat_001' : 'pat_002',
              'device_id': 'dev_test_consilium',
              'has_active_cycle': true,
              'active_cycle_id': isPat1 ? 'cycle_100' : 'cycle_200',
              'has_preparatory_anamnesis': true,
              'is_primary_consilium_completed': isPat1, // pat_001 is completed, pat_002 is pending!
            }),
            200,
            headers: {'content-type': 'application/json'},
          );
        }
        if (request.url.path == '/api/v1/reports/client') {
          return reportsCompleter.future; // Hold reports request for pat_001 in-flight
        }
        return http.Response('Not Found', 404);
      });

      final client = AegisApiClient(baseUrl: 'http://127.0.0.1:8000', client: mockClient);
      final authManager = AuthSessionManager(apiClient: client, storage: memoryAuthStorage);
      authManager.setSessionForTesting(
        AuthSession(
          accessToken: _buildTestJwt(patientId: 'pat_001'),
          refreshToken: 'ref_tok_1',
          expiresIn: 3600,
          expiresAt: DateTime.now().toUtc().add(const Duration(hours: 1)),
          refreshExpiresAt: DateTime.now().toUtc().add(const Duration(days: 30)),
          patientId: 'pat_001',
          deviceId: 'dev_test_consilium',
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
      await tester.pump(); // Start GET reports request for pat_001

      // While in-flight, switch patient to pat_002
      authManager.setSessionForTesting(
        AuthSession(
          accessToken: _buildTestJwt(patientId: 'pat_002'),
          refreshToken: 'ref_tok_2',
          expiresIn: 3600,
          expiresAt: DateTime.now().toUtc().add(const Duration(hours: 1)),
          refreshExpiresAt: DateTime.now().toUtc().add(const Duration(days: 30)),
          patientId: 'pat_002',
          deviceId: 'dev_test_consilium',
          scope: 'cascade:intake cascade:run cycle:read reports:read',
        ),
        status: AuthSessionStatus.authenticated,
      );

      // Complete the delayed response for pat_001
      reportsCompleter.complete(http.Response(
        json.encode(_sampleClientReportsJson(cycleId: 'cycle_100')),
        200,
        headers: {'content-type': 'application/json'},
      ));
      await tester.pumpAndSettle();

      // pat_002's Vault must NOT be contaminated with pat_001's reports
      expect(vault.isConsiliumCompleted, isFalse);
    });
  });
}
