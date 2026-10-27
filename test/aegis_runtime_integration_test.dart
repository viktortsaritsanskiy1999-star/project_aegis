// ---------------------------------------------------------------------------
// PROJECT AEGIS — STAGE 5.3C-3B-2C
// END-TO-END FLUTTER RUNTIME INTEGRATION TESTS (F1–F15)
//
// Verifies complete Aegis runtime dependency graph:
//   AegisApiClient -> AuthSessionManager -> VitalsSyncQueue -> HealthDataProvider
// Tested with InMemoryAuthTokenStorage, SharedPreferences mock, and MockClient.
// ---------------------------------------------------------------------------

import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:provider/provider.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:shared_preferences_platform_interface/shared_preferences_platform_interface.dart';

// ignore_for_file: depend_on_referenced_packages
import 'package:shared_preferences_platform_interface/types.dart';

import 'package:aegis_health/main.dart';
import 'package:aegis_health/services/api/aegis_api_client.dart';
import 'package:aegis_health/services/auth/auth_session_manager.dart';
import 'package:aegis_health/services/auth/auth_token_storage.dart';
import 'package:aegis_health/services/auth/auth_models.dart';
import 'package:aegis_health/services/health_data_provider.dart';
import 'package:aegis_health/services/sync/vitals_sync_queue.dart';
import 'package:aegis_health/services/time_service.dart';
import 'package:aegis_health/services/vault_service.dart';
import 'package:aegis_health/services/vitals_monitor_service.dart';
import 'package:aegis_health/services/brain_service.dart';
import 'package:aegis_health/services/athena_trigger_service.dart';
import 'package:aegis_health/services/aegis_cascade_controller.dart';

/// Test SharedPreferences store supporting intentional failure injection and initial data seeding.
class TestSharedPreferencesStore extends SharedPreferencesStorePlatform {
  bool shouldFail = false;
  final Map<String, Object> _data = {};

  TestSharedPreferencesStore({Map<String, Object>? initialData}) {
    if (initialData != null) {
      _data.addAll(initialData);
    }
  }

  @override
  Future<bool> setValue(String valueType, String key, Object value) async {
    if (shouldFail) {
      throw Exception('Simulated disk I/O failure (Disk full / permission denied)');
    }
    _data[key] = value;
    return true;
  }

  @override
  Future<bool> clear() async {
    _data.clear();
    return true;
  }

  @override
  Future<Map<String, Object>> getAll() async {
    return Map<String, Object>.from(_data);
  }

  @override
  Future<Map<String, Object>> getAllWithParameters(GetAllParameters parameters) async {
    return Map<String, Object>.from(_data);
  }

  @override
  Future<bool> remove(String key) async {
    _data.remove(key);
    return true;
  }
}

/// Builds a structurally valid 3-part base64url JWT string with patient and device claims.
String _buildTestJwt({
  required String patientId,
  required String deviceId,
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

/// Helper container holding the instantiated runtime graph for integration tests.
class RuntimeTestHarness {
  final MockClient mockClient;
  final AegisApiClient apiClient;
  final InMemoryAuthTokenStorage storage;
  final AuthSessionManager authManager;
  final VitalsSyncQueue syncQueue;
  final VaultService vaultService;
  final TimeService timeService;
  final VitalsMonitorService vitalsMonitorService;
  final HealthDataProvider healthDataProvider;

  RuntimeTestHarness({
    required this.mockClient,
    required this.apiClient,
    required this.storage,
    required this.authManager,
    required this.syncQueue,
    required this.vaultService,
    required this.timeService,
    required this.vitalsMonitorService,
    required this.healthDataProvider,
  });

  static Future<RuntimeTestHarness> create({
    required MockClient mockClient,
    InMemoryAuthTokenStorage? storage,
    bool enforcePatientIsolation = true,
  }) async {
    final effectiveStorage = storage ?? InMemoryAuthTokenStorage();
    final apiClient = AegisApiClient(baseUrl: 'http://127.0.0.1:8000', client: mockClient);
    final authManager = AuthSessionManager(apiClient: apiClient, storage: effectiveStorage);
    final syncQueue = VitalsSyncQueue(
      apiClient: apiClient,
      authSessionManager: authManager,
      enforcePatientIsolation: enforcePatientIsolation,
    );

    final timeService = TimeService();
    timeService.resetToRealTime();
    final vaultService = VaultService();
    await vaultService.initFuture;
    final vitalsMonitorService = VitalsMonitorService(timeService, vaultService);

    final healthDataProvider = HealthDataProvider(
      vaultService: vaultService,
      vitalsMonitorService: vitalsMonitorService,
      syncQueue: syncQueue,
    );

    return RuntimeTestHarness(
      mockClient: mockClient,
      apiClient: apiClient,
      storage: effectiveStorage,
      authManager: authManager,
      syncQueue: syncQueue,
      vaultService: vaultService,
      timeService: timeService,
      vitalsMonitorService: vitalsMonitorService,
      healthDataProvider: healthDataProvider,
    );
  }

  void dispose() {
    healthDataProvider.dispose();
    syncQueue.dispose();
    authManager.dispose();
    vitalsMonitorService.dispose();
    apiClient.close();
  }
}

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  setUp(() {
    SharedPreferences.resetStatic();
    SharedPreferences.setMockInitialValues({});
    AuthSessionManager.resetActiveInstancesForTesting();
  });

  tearDown(() {
    SharedPreferences.resetStatic();
    AuthSessionManager.resetActiveInstancesForTesting();
  });

  // =========================================================================
  // 1. RUNTIME STARTUP (F1–F3)
  // =========================================================================
  group('1. RUNTIME STARTUP (F1–F3)', () {
    test('F1: Cold start without session sets queue to awaitingAuthorization, quarantines measurements, makes 0 HTTP calls', () async {
      int httpCalls = 0;
      final mockClient = MockClient((request) async {
        httpCalls++;
        return http.Response('Unexpected network call', 500);
      });

      final harness = await RuntimeTestHarness.create(mockClient: mockClient);

      // Startup restoration without session
      await harness.syncQueue.init();

      expect(harness.authManager.status, equals(AuthSessionStatus.unauthenticated));
      expect(harness.syncQueue.isPaused, isTrue);
      expect(harness.syncQueue.pauseReason, contains('Awaiting authorization'));
      expect(harness.syncQueue.activePatientId, isNull);
      expect(harness.healthDataProvider.activePatientId, isNull);
      expect(harness.healthDataProvider.isSyncPaused, isTrue);

      // Attempting cycle resolution in unauthenticated cold start must not call network
      final cycleResolved = await harness.syncQueue.resolveActiveCycle();
      expect(cycleResolved, isFalse);
      expect(httpCalls, equals(0));

      // Ingest measurement through HealthDataProvider
      final success = await harness.healthDataProvider.ingestMeasurement(HealthMeasurement(
        timestamp: DateTime.now(),
        pulse: 76,
        systolic: 120,
        diastolic: 80,
        source: HealthDataSource.watchSensor,
      ));

      expect(success, isTrue);
      // Saved in Vault (offline-first)
      expect(harness.vaultService.pulse, equals(76));
      // In queue, quarantined because unauthenticated
      expect(harness.healthDataProvider.syncQuarantinedCount, equals(1));
      expect(harness.healthDataProvider.syncPendingCount, equals(0));
      expect(harness.healthDataProvider.syncTotalPendingCount, equals(0));

      // Flush queue: returns paused, 0 HTTP
      final flushResult = await harness.syncQueue.flush();
      expect(flushResult.status, equals(SyncStatus.paused));
      expect(httpCalls, equals(0));

      harness.dispose();
    });

    test('F2: Cold start with stored valid session of Patient A restores state, resolves cycle, and flushes only Patient A data', () async {
      final jwtA = _buildTestJwt(patientId: 'patient_A', deviceId: 'device_f2');
      int cycleCalls = 0;
      int batchCalls = 0;
      String? sentAuthHeader;
      String? batchPayload;

      final mockClient = MockClient((request) async {
        if (request.url.path == '/api/v1/cycle/current') {
          cycleCalls++;
          sentAuthHeader = request.headers['authorization'];
          return http.Response(json.encode({
            'cycle_id': 'cycle_A',
            'patient_id': 'patient_A',
            'status': 'active',
            'started_at': DateTime.now().toUtc().toIso8601String(),
          }), 200);
        }
        if (request.url.path == '/api/v1/vitals/batch') {
          batchCalls++;
          batchPayload = request.body;
          return http.Response(json.encode({
            'accepted': 1,
            'failed': 0,
            'quarantined': 0,
            'recorded': 1,
            'deduplicated': 0,
          }), 200);
        }
        return http.Response('Not Found', 404);
      });

      final storage = InMemoryAuthTokenStorage();
      final sessionA = AuthSession(
        accessToken: jwtA,
        refreshToken: 'rt_f2',
        tokenType: 'Bearer',
        expiresIn: 3600,
        expiresAt: DateTime.now().add(const Duration(hours: 1)),
        refreshExpiresAt: DateTime.now().add(const Duration(days: 30)),
        deviceId: 'device_f2',
        patientId: 'patient_A',
        scope: 'vitals:write vitals:read',
      );
      await storage.saveSession(sessionA);

      final harness = await RuntimeTestHarness.create(mockClient: mockClient, storage: storage);

      // Cold start restoration
      await harness.syncQueue.init();
      await harness.syncQueue.resolveActiveCycle();

      expect(cycleCalls, equals(1));
      expect(harness.authManager.status, equals(AuthSessionStatus.authenticated));
      expect(harness.syncQueue.activePatientId, equals('patient_A'));
      expect(harness.syncQueue.activeCycleId, equals('cycle_A'));
      expect(harness.syncQueue.isPaused, isFalse);
      expect(harness.healthDataProvider.activePatientId, equals('patient_A'));
      expect(sentAuthHeader, equals('Bearer $jwtA'));

      // Ingest measurement for Patient A
      await harness.healthDataProvider.ingestMeasurement(HealthMeasurement(
        timestamp: DateTime.now(),
        pulse: 74,
        source: HealthDataSource.watchSensor,
      ));

      expect(harness.healthDataProvider.syncPendingCount, equals(1));
      expect(harness.healthDataProvider.syncQuarantinedCount, equals(0));

      // Flush queue
      final flushResult = await harness.syncQueue.flush();
      expect(flushResult.status, equals(SyncStatus.success));
      expect(batchCalls, equals(1));
      expect(batchPayload, contains('cycle_A'));
      expect(harness.healthDataProvider.syncPendingCount, equals(0));

      harness.dispose();
    });

    test('F3: Cold start with expired session attributes locally to Patient A, keeps pause, and executes 0 network calls', () async {
      final jwtA = _buildTestJwt(patientId: 'patient_A', deviceId: 'device_f3');
      int httpCalls = 0;
      final mockClient = MockClient((request) async {
        httpCalls++;
        return http.Response('Unexpected request', 500);
      });

      final storage = InMemoryAuthTokenStorage();
      final expiredSession = AuthSession(
        accessToken: jwtA,
        refreshToken: 'rt_f3',
        tokenType: 'Bearer',
        expiresIn: 3600,
        expiresAt: DateTime.now().subtract(const Duration(hours: 2)), // Expired
        refreshExpiresAt: DateTime.now().subtract(const Duration(hours: 1)), // Expired
        deviceId: 'device_f3',
        patientId: 'patient_A',
        scope: 'vitals:write vitals:read',
      );
      await storage.saveSession(expiredSession);

      final harness = await RuntimeTestHarness.create(mockClient: mockClient, storage: storage);
      await harness.syncQueue.init();

      expect(harness.authManager.status, equals(AuthSessionStatus.expired));
      expect(harness.authManager.confirmedAttributionPatientId, equals('patient_A'));
      expect(harness.syncQueue.activePatientId, equals('patient_A'));
      expect(harness.syncQueue.isPaused, isTrue);
      expect(harness.healthDataProvider.isSyncPaused, isTrue);

      // Ingest measurement for Patient A while expired (R1-B rule)
      final success = await harness.healthDataProvider.ingestMeasurement(HealthMeasurement(
        timestamp: DateTime.now(),
        pulse: 75,
        source: HealthDataSource.watchSensor,
      ));

      expect(success, isTrue);
      // Attributed locally to Patient A, preserved in pending, NOT quarantined
      expect(harness.healthDataProvider.syncPendingCount, equals(1));
      expect(harness.healthDataProvider.syncQuarantinedCount, equals(0));

      // Flush must be paused, 0 HTTP calls
      final flushResult = await harness.syncQueue.flush();
      expect(flushResult.status, equals(SyncStatus.paused));
      expect(httpCalls, equals(0));

      harness.dispose();
    });
  });

  // =========================================================================
  // 2. PATIENT SWITCH & ATTRIBUTION (F4–F7)
  // =========================================================================
  group('2. PATIENT SWITCH & ATTRIBUTION (F4–F7)', () {
    test('F4: Pairing Patient A from unauthenticated state leaves prior quarantined items strictly isolated', () async {
      final mockClient = MockClient((request) async {
        if (request.url.path == '/api/v1/cycle/current') {
          return http.Response(json.encode({
            'cycle_id': 'cycle_A',
            'patient_id': 'patient_A',
            'status': 'active',
            'started_at': DateTime.now().toUtc().toIso8601String(),
          }), 200);
        }
        if (request.url.path == '/api/v1/vitals/batch') {
          return http.Response(json.encode({
            'accepted': 1,
            'failed': 0,
            'quarantined': 0,
            'recorded': 1,
            'deduplicated': 0,
          }), 200);
        }
        return http.Response('Not Found', 404);
      });

      final harness = await RuntimeTestHarness.create(mockClient: mockClient);
      await harness.syncQueue.init();

      // Ingest 2 measurements while unauthenticated
      await harness.healthDataProvider.ingestMeasurement(HealthMeasurement(
        timestamp: DateTime.now(),
        pulse: 70,
        source: HealthDataSource.watchSensor,
      ));
      await harness.healthDataProvider.ingestMeasurement(HealthMeasurement(
        timestamp: DateTime.now(),
        pulse: 71,
        source: HealthDataSource.watchSensor,
      ));

      expect(harness.healthDataProvider.syncQuarantinedCount, equals(2));
      expect(harness.healthDataProvider.syncPendingCount, equals(0));

      // User pairs device with Patient A
      final jwtA = _buildTestJwt(patientId: 'patient_A', deviceId: 'device_f4');
      final sessionA = AuthSession(
        accessToken: jwtA,
        refreshToken: 'rt_f4',
        tokenType: 'Bearer',
        expiresIn: 3600,
        expiresAt: DateTime.now().add(const Duration(hours: 1)),
        refreshExpiresAt: DateTime.now().add(const Duration(days: 30)),
        deviceId: 'device_f4',
        patientId: 'patient_A',
        scope: 'vitals:write vitals:read',
      );
      await harness.storage.saveSession(sessionA);
      harness.authManager.setSessionForTesting(sessionA, status: AuthSessionStatus.authenticated);
      await harness.syncQueue.resolveActiveCycle();

      // Verification: Prior quarantined items must NEVER be attributed to Patient A
      expect(harness.healthDataProvider.activePatientId, equals('patient_A'));
      expect(harness.healthDataProvider.syncQuarantinedCount, equals(2));
      expect(harness.healthDataProvider.syncPendingCount, equals(0));

      // Ingest new measurement for Patient A
      await harness.healthDataProvider.ingestMeasurement(HealthMeasurement(
        timestamp: DateTime.now(),
        pulse: 80,
        source: HealthDataSource.watchSensor,
      ));

      expect(harness.healthDataProvider.syncPendingCount, equals(1));
      expect(harness.healthDataProvider.syncQuarantinedCount, equals(2));

      // Flush queue: dispatches ONLY 1 item (Patient A), quarantined remains 2
      final flushResult = await harness.syncQueue.flush();
      expect(flushResult.status, equals(SyncStatus.success));
      expect(flushResult.processedCount, equals(1));
      expect(harness.healthDataProvider.syncPendingCount, equals(0));
      expect(harness.healthDataProvider.syncQuarantinedCount, equals(2));

      harness.dispose();
    });

    test('F5: A -> revoke -> B preserves pending A, quarantines revoked data, and flushes only B data', () async {
      int batchCalls = 0;
      final jwtA = _buildTestJwt(patientId: 'patient_A', deviceId: 'device_f5');
      final jwtB = _buildTestJwt(patientId: 'patient_B', deviceId: 'device_f5');

      final mockClient = MockClient((request) async {
        if (request.url.path == '/api/v1/cycle/current') {
          return http.Response(json.encode({
            'cycle_id': 'cycle_B',
            'patient_id': 'patient_B',
            'status': 'active',
            'started_at': DateTime.now().toUtc().toIso8601String(),
          }), 200);
        }
        if (request.url.path == '/api/v1/vitals/batch') {
          batchCalls++;
          final body = json.decode(request.body) as Map<String, dynamic>;
          final measurements = body['measurements'] as List<dynamic>;
          expect(measurements.length, equals(1));
          expect(request.headers['authorization'], equals('Bearer $jwtB'));
          return http.Response(json.encode({
            'accepted': 1,
            'failed': 0,
            'quarantined': 0,
            'recorded': 1,
            'deduplicated': 0,
          }), 200);
        }
        return http.Response('Not Found', 404);
      });

      final storage = InMemoryAuthTokenStorage();
      final sessionA = AuthSession(
        accessToken: jwtA,
        refreshToken: 'rt_f5_a',
        tokenType: 'Bearer',
        expiresIn: 3600,
        expiresAt: DateTime.now().add(const Duration(hours: 1)),
        refreshExpiresAt: DateTime.now().add(const Duration(days: 30)),
        deviceId: 'device_f5',
        patientId: 'patient_A',
        scope: 'vitals:write vitals:read',
      );
      await storage.saveSession(sessionA);

      final harness = await RuntimeTestHarness.create(mockClient: mockClient, storage: storage);
      await harness.syncQueue.init();

      // Ingest 2 measurements for Patient A
      await harness.healthDataProvider.ingestMeasurement(HealthMeasurement(
        timestamp: DateTime.now(),
        pulse: 70,
        source: HealthDataSource.watchSensor,
      ));
      await harness.healthDataProvider.ingestMeasurement(HealthMeasurement(
        timestamp: DateTime.now(),
        pulse: 71,
        source: HealthDataSource.watchSensor,
      ));

      expect(harness.healthDataProvider.syncPendingCount, equals(2));

      // Revoke session -> unauthenticated
      harness.authManager.setSessionForTesting(null, status: AuthSessionStatus.unauthenticated);
      expect(harness.healthDataProvider.activePatientId, isNull);

      // Ingest intermediate measurement while revoked -> quarantined
      await harness.healthDataProvider.ingestMeasurement(HealthMeasurement(
        timestamp: DateTime.now(),
        pulse: 75,
        source: HealthDataSource.watchSensor,
      ));
      expect(harness.healthDataProvider.syncQuarantinedCount, equals(1));

      // Patient B authenticates
      final sessionB = AuthSession(
        accessToken: jwtB,
        refreshToken: 'rt_f5_b',
        tokenType: 'Bearer',
        expiresIn: 3600,
        expiresAt: DateTime.now().add(const Duration(hours: 1)),
        refreshExpiresAt: DateTime.now().add(const Duration(days: 30)),
        deviceId: 'device_f5',
        patientId: 'patient_B',
        scope: 'vitals:write vitals:read',
      );
      await harness.storage.saveSession(sessionB);
      harness.authManager.setSessionForTesting(sessionB, status: AuthSessionStatus.authenticated);
      await harness.syncQueue.resolveActiveCycle();

      // Ingest 1 measurement for Patient B
      await harness.healthDataProvider.ingestMeasurement(HealthMeasurement(
        timestamp: DateTime.now(),
        pulse: 88,
        source: HealthDataSource.watchSensor,
      ));

      // Verification:
      // Active pending is strictly Patient B (1 item)
      expect(harness.healthDataProvider.syncPendingCount, equals(1));
      // Total pending contains Patient A (2) + Patient B (1) = 3 items
      expect(harness.healthDataProvider.syncTotalPendingCount, equals(3));
      // Quarantined remains 1
      expect(harness.healthDataProvider.syncQuarantinedCount, equals(1));

      // Flush queue: dispatches only Patient B data
      final flushResult = await harness.syncQueue.flush();
      expect(flushResult.status, equals(SyncStatus.success));
      expect(batchCalls, equals(1));

      expect(harness.healthDataProvider.syncPendingCount, equals(0));
      expect(harness.healthDataProvider.syncTotalPendingCount, equals(2)); // Patient A items preserved!

      harness.dispose();
    });

    test('F6: App restart between revoke A and pairing B preserves disk items, activePatientId is null, 0 HTTP', () async {
      int httpCalls = 0;
      final mockClient = MockClient((request) async {
        httpCalls++;
        return http.Response('Unexpected network request', 500);
      });

      // Pre-populate SharedPreferences snapshot with Patient A items
      final initialSnapshot = {
        'version': 2,
        'saved_at': DateTime.now().toUtc().toIso8601String(),
        'active_cycle_id': 'cycle_A',
        'disk_patient_id': 'patient_A',
        'event_counter': 1,
        'pending': [
          {
            'event_id': 'evt_a1',
            'idempotency_key': 'idem_a1',
            'patient_id': 'patient_A',
            'cycle_id': 'cycle_A',
            'dto': {
              'cycle_id': 'cycle_A',
              'measured_at': DateTime.now().toUtc().toIso8601String(),
              'source': 'watch_sensor',
              'pulse': 72,
              'is_simulated': false,
              'is_historical': false,
              'phase': 'standby',
            },
            'enqueued_at': DateTime.now().toUtc().toIso8601String(),
            'retry_count': 0,
            'is_quarantined': false,
          }
        ],
        'quarantine': [],
      };

      SharedPreferences.setMockInitialValues({
        'aegis_vitals_sync_snapshot_v2': json.encode(initialSnapshot),
      });

      // Storage is empty (revoked before restart)
      final harness = await RuntimeTestHarness.create(mockClient: mockClient);
      await harness.syncQueue.init();

      // Verification:
      // activePatientId must NOT be restored from snapshot without active auth session!
      expect(harness.syncQueue.activePatientId, isNull);
      expect(harness.healthDataProvider.activePatientId, isNull);
      expect(harness.syncQueue.isPaused, isTrue);
      expect(harness.syncQueue.pauseReason, contains('Awaiting authorization'));

      // Pending items of Patient A are preserved on disk
      expect(harness.healthDataProvider.syncPendingCount, equals(0));
      expect(harness.healthDataProvider.syncTotalPendingCount, equals(1));
      expect(httpCalls, equals(0));

      harness.dispose();
    });

    test('F7: Snapshot of patient A + authorized session B resets cycle, keeps A safe, and flushes only B', () async {
      int batchCalls = 0;
      String? sentPayload;

      final mockClient = MockClient((request) async {
        if (request.url.path == '/api/v1/cycle/current') {
          return http.Response(json.encode({
            'cycle_id': 'cycle_B_fresh',
            'patient_id': 'patient_B',
            'status': 'active',
            'started_at': DateTime.now().toUtc().toIso8601String(),
          }), 200);
        }
        if (request.url.path == '/api/v1/vitals/batch') {
          batchCalls++;
          sentPayload = request.body;
          return http.Response(json.encode({
            'accepted': 1,
            'failed': 0,
            'quarantined': 0,
            'recorded': 1,
            'deduplicated': 0,
          }), 200);
        }
        return http.Response('Not Found', 404);
      });

      // Pre-populate SharedPreferences snapshot with Patient A
      final snapshotA = {
        'version': 2,
        'saved_at': DateTime.now().toUtc().toIso8601String(),
        'active_cycle_id': 'cycle_A_old',
        'disk_patient_id': 'patient_A',
        'pending': [
          {
            'event_id': 'evt_a_legacy',
            'idempotency_key': 'idem_a_legacy',
            'patient_id': 'patient_A',
            'cycle_id': 'cycle_A_old',
            'dto': {
              'cycle_id': 'cycle_A_old',
              'measured_at': DateTime.now().toUtc().toIso8601String(),
              'source': 'watch_sensor',
              'pulse': 68,
              'is_simulated': false,
              'is_historical': false,
              'phase': 'standby',
            },
            'enqueued_at': DateTime.now().toUtc().toIso8601String(),
            'retry_count': 0,
            'is_quarantined': false,
          }
        ],
        'quarantine': [],
      };

      SharedPreferences.setMockInitialValues({
        'aegis_vitals_sync_snapshot_v2': json.encode(snapshotA),
      });

      // Auth storage has active session for Patient B
      final jwtB = _buildTestJwt(patientId: 'patient_B', deviceId: 'device_f7');
      final storage = InMemoryAuthTokenStorage();
      final sessionB = AuthSession(
        accessToken: jwtB,
        refreshToken: 'rt_f7_b',
        tokenType: 'Bearer',
        expiresIn: 3600,
        expiresAt: DateTime.now().add(const Duration(hours: 1)),
        refreshExpiresAt: DateTime.now().add(const Duration(days: 30)),
        deviceId: 'device_f7',
        patientId: 'patient_B',
        scope: 'vitals:write vitals:read',
      );
      await storage.saveSession(sessionB);

      final harness = await RuntimeTestHarness.create(mockClient: mockClient, storage: storage);
      await harness.syncQueue.init();
      await harness.syncQueue.resolveActiveCycle();

      // Verification:
      expect(harness.syncQueue.activePatientId, equals('patient_B'));
      expect(harness.syncQueue.activeCycleId, equals('cycle_B_fresh'));
      expect(harness.healthDataProvider.syncPendingCount, equals(0));
      expect(harness.healthDataProvider.syncTotalPendingCount, equals(1)); // Patient A item preserved

      // Ingest for Patient B
      await harness.healthDataProvider.ingestMeasurement(HealthMeasurement(
        timestamp: DateTime.now(),
        pulse: 92,
        source: HealthDataSource.watchSensor,
      ));

      expect(harness.healthDataProvider.syncPendingCount, equals(1));
      expect(harness.healthDataProvider.syncTotalPendingCount, equals(2));

      // Flush queue
      final flushResult = await harness.syncQueue.flush();
      expect(flushResult.status, equals(SyncStatus.success));
      expect(batchCalls, equals(1));
      expect(sentPayload, contains('cycle_B_fresh'));
      expect(sentPayload, isNot(contains('evt_a_legacy')));

      expect(harness.healthDataProvider.syncPendingCount, equals(0));
      expect(harness.healthDataProvider.syncTotalPendingCount, equals(1)); // Patient A still preserved

      harness.dispose();
    });
  });

  // =========================================================================
  // 3. PERSISTENCE SAFETY & REFRESH (F8–F9)
  // =========================================================================
  group('3. PERSISTENCE SAFETY & REFRESH (F8–F9)', () {
    test('F8: Persistence failure during session disassociation pauses queue and is NOT cleared by subsequent auth notification', () async {
      final customStore = TestSharedPreferencesStore();
      SharedPreferences.resetStatic();
      SharedPreferencesStorePlatform.instance = customStore;

      int batchHttpCalls = 0;
      final mockClient = MockClient((request) async {
        if (request.url.path == '/api/v1/vitals/batch') {
          batchHttpCalls++;
          return http.Response('OK', 200);
        }
        return http.Response('Not Found', 404);
      });

      final jwtA = _buildTestJwt(patientId: 'patient_A', deviceId: 'device_f8');
      final jwtB = _buildTestJwt(patientId: 'patient_B', deviceId: 'device_f8');

      final storage = InMemoryAuthTokenStorage();
      final sessionA = AuthSession(
        accessToken: jwtA,
        refreshToken: 'rt_f8_a',
        tokenType: 'Bearer',
        expiresIn: 3600,
        expiresAt: DateTime.now().add(const Duration(hours: 1)),
        refreshExpiresAt: DateTime.now().add(const Duration(days: 30)),
        deviceId: 'device_f8',
        patientId: 'patient_A',
        scope: 'vitals:write vitals:read',
      );
      await storage.saveSession(sessionA);

      final harness = await RuntimeTestHarness.create(mockClient: mockClient, storage: storage);
      await harness.syncQueue.init();

      // Ingest 1 measurement for Patient A successfully
      await harness.healthDataProvider.ingestMeasurement(HealthMeasurement(
        timestamp: DateTime.now(),
        pulse: 75,
        source: HealthDataSource.watchSensor,
      ));
      expect(harness.healthDataProvider.syncPendingCount, equals(1));

      // Inject disk I/O failure
      customStore.shouldFail = true;

      // Dissociate session -> triggers snapshot write which fails
      harness.authManager.setSessionForTesting(null, status: AuthSessionStatus.unauthenticated);
      await Future.delayed(const Duration(milliseconds: 50));

      // Verification: Queue entered fail-closed persistence pause
      expect(harness.syncQueue.isPaused, isTrue);
      expect(harness.syncQueue.pauseReason, contains('Persistence error on session disassociation'));

      // Patient B authenticates -> sends new auth notification
      final sessionB = AuthSession(
        accessToken: jwtB,
        refreshToken: 'rt_f8_b',
        tokenType: 'Bearer',
        expiresIn: 3600,
        expiresAt: DateTime.now().add(const Duration(hours: 1)),
        refreshExpiresAt: DateTime.now().add(const Duration(days: 30)),
        deviceId: 'device_f8',
        patientId: 'patient_B',
        scope: 'vitals:write vitals:read',
      );
      await harness.storage.saveSession(sessionB);
      harness.authManager.setSessionForTesting(sessionB, status: AuthSessionStatus.authenticated);

      // CRITICAL D6 HARDENING: Persistence pause MUST NOT be cleared by auth notification!
      expect(harness.syncQueue.isPaused, isTrue);
      expect(harness.syncQueue.pauseReason, contains('Persistence error on session disassociation'));
      expect(harness.healthDataProvider.isSyncPaused, isTrue);

      // Attempting flush while in persistence pause returns paused and executes 0 network calls
      final flushResult = await harness.syncQueue.flush();
      expect(flushResult.status, equals(SyncStatus.paused));
      expect(batchHttpCalls, equals(0));

      harness.dispose();
    });

    test('F9: Token refresh of the same patient does not increment sessionGeneration or reset active medical cycle', () async {
      final jwtA1 = _buildTestJwt(patientId: 'patient_A', deviceId: 'device_f9');
      final jwtA2 = _buildTestJwt(patientId: 'patient_A', deviceId: 'device_f9');

      final mockClient = MockClient((request) async {
        if (request.url.path == '/api/v1/cycle/current') {
          return http.Response(json.encode({
            'cycle_id': 'cycle_A_continuous',
            'patient_id': 'patient_A',
            'status': 'active',
            'started_at': DateTime.now().toUtc().toIso8601String(),
          }), 200);
        }
        return http.Response('Not Found', 404);
      });

      final storage = InMemoryAuthTokenStorage();
      final sessionA1 = AuthSession(
        accessToken: jwtA1,
        refreshToken: 'rt_f9_1',
        tokenType: 'Bearer',
        expiresIn: 3600,
        expiresAt: DateTime.now().add(const Duration(hours: 1)),
        refreshExpiresAt: DateTime.now().add(const Duration(days: 30)),
        deviceId: 'device_f9',
        patientId: 'patient_A',
        scope: 'vitals:write vitals:read',
      );
      await storage.saveSession(sessionA1);

      final harness = await RuntimeTestHarness.create(mockClient: mockClient, storage: storage);
      await harness.syncQueue.init();
      await harness.syncQueue.resolveActiveCycle();

      expect(harness.syncQueue.activeCycleId, equals('cycle_A_continuous'));
      expect(harness.syncQueue.isCycleConfirmedByServer, isTrue);

      await harness.healthDataProvider.ingestMeasurement(HealthMeasurement(
        timestamp: DateTime.now(),
        pulse: 77,
        source: HealthDataSource.watchSensor,
      ));
      expect(harness.healthDataProvider.syncPendingCount, equals(1));

      // Same patient token rotation
      final sessionA2 = AuthSession(
        accessToken: jwtA2,
        refreshToken: 'rt_f9_2',
        tokenType: 'Bearer',
        expiresIn: 3600,
        expiresAt: DateTime.now().add(const Duration(hours: 1)),
        refreshExpiresAt: DateTime.now().add(const Duration(days: 30)),
        deviceId: 'device_f9',
        patientId: 'patient_A', // Same patient!
        scope: 'vitals:write vitals:read',
      );
      await harness.storage.saveSession(sessionA2);
      harness.authManager.setSessionForTesting(sessionA2, status: AuthSessionStatus.authenticated);

      // Verification: Medical cycle is NOT reset, pending items remain active
      expect(harness.syncQueue.activeCycleId, equals('cycle_A_continuous'));
      expect(harness.syncQueue.isCycleConfirmedByServer, isTrue);
      expect(harness.healthDataProvider.syncPendingCount, equals(1));

      harness.dispose();
    });
  });

  // =========================================================================
  // 4. RUNTIME ARCHITECTURE & D10 (F10–F11, F15)
  // =========================================================================
  group('4. RUNTIME ARCHITECTURE & D10 (F10–F11, F15)', () {
    testWidgets('F10: Runtime MultiProvider graph wires single apiClient, authManager, syncQueue and healthDataProvider', (tester) async {
      final mockClient = MockClient((request) async => http.Response('Not Found', 404));
      final storage = InMemoryAuthTokenStorage();

      AegisApiClient? capturedClient;
      AuthSessionManager? capturedManager;
      VitalsSyncQueue? capturedQueue;
      HealthDataProvider? capturedProvider;

      await tester.pumpWidget(
        MultiProvider(
          providers: [
            ChangeNotifierProvider<TimeService>.value(value: TimeService()),
            ChangeNotifierProvider<VaultService>(create: (_) => VaultService()),
            ChangeNotifierProxyProvider2<TimeService, VaultService, VitalsMonitorService>(
              create: (ctx) => VitalsMonitorService(ctx.read<TimeService>(), ctx.read<VaultService>()),
              update: (ctx, time, vault, prev) => prev ?? VitalsMonitorService(time, vault),
            ),
            Provider<AegisApiClient>(
              create: (_) => AegisApiClient(baseUrl: 'http://127.0.0.1:8000', client: mockClient),
              dispose: (_, client) => client.close(),
            ),
            ChangeNotifierProvider<AuthSessionManager>(
              create: (ctx) => AuthSessionManager(apiClient: ctx.read<AegisApiClient>(), storage: storage),
            ),
            ChangeNotifierProvider<VitalsSyncQueue>(
              create: (ctx) => VitalsSyncQueue(
                apiClient: ctx.read<AegisApiClient>(),
                authSessionManager: ctx.read<AuthSessionManager>(),
                enforcePatientIsolation: true,
              ),
            ),
            ChangeNotifierProxyProvider3<VaultService, VitalsMonitorService, VitalsSyncQueue, HealthDataProvider>(
              create: (ctx) => HealthDataProvider(
                vaultService: ctx.read<VaultService>(),
                vitalsMonitorService: ctx.read<VitalsMonitorService>(),
                syncQueue: ctx.read<VitalsSyncQueue>(),
              ),
              update: (ctx, vault, vitals, queue, prev) =>
                  (prev ?? HealthDataProvider(vaultService: vault, vitalsMonitorService: vitals, syncQueue: queue))
                    ..updateDependencies(vaultService: vault, vitalsMonitorService: vitals, syncQueue: queue),
            ),
          ],
          child: MaterialApp(
            home: Builder(
              builder: (context) {
                capturedClient = context.read<AegisApiClient>();
                capturedManager = context.read<AuthSessionManager>();
                capturedQueue = context.read<VitalsSyncQueue>();
                capturedProvider = context.read<HealthDataProvider>();
                return const Scaffold(body: Text('Wired'));
              },
            ),
          ),
        ),
      );

      expect(capturedClient, isNotNull);
      expect(capturedManager, isNotNull);
      expect(capturedQueue, isNotNull);
      expect(capturedProvider, isNotNull);

      // Verify tokenProvider is bound to AuthSessionManager (R6)
      expect(capturedClient!.tokenProvider, isNotNull);
      expect(capturedQueue!.isPatientIsolationEnabled, isTrue);
      expect(identical(capturedProvider!.syncQueue, capturedQueue), isTrue);

      // Verify static guard prevents parallel manager on same storage/device
      final client2 = AegisApiClient(baseUrl: 'http://127.0.0.1:8000', client: mockClient);
      expect(
        () => AuthSessionManager(apiClient: client2, storage: storage),
        throwsA(isA<StateError>()),
      );
      client2.close();

      await tester.pumpWidget(const SizedBox.shrink());
    });

    test('F11 (D10): 401 -> refresh -> 401 triggers exactly 1 refresh and pauses queue and provider with expired reason', () async {
      final jwtInitial = _buildTestJwt(patientId: 'patient_A', deviceId: 'device_f11');
      final jwtRotated = _buildTestJwt(patientId: 'patient_A', deviceId: 'device_f11');

      int batchCalls = 0;
      int refreshCalls = 0;

      final mockClient = MockClient((request) async {
        if (request.url.path == '/api/v1/cycle/current') {
          return http.Response(json.encode({
            'cycle_id': 'cycle_A',
            'patient_id': 'patient_A',
            'status': 'active',
            'started_at': DateTime.now().toUtc().toIso8601String(),
          }), 200);
        }
        if (request.url.path == '/api/v1/vitals/batch') {
          batchCalls++;
          // Always return 401 Unauthorized
          return http.Response(json.encode({'detail': 'Batch rejected: invalid auth token'}), 401);
        }
        if (request.url.path == '/api/v1/auth/refresh') {
          refreshCalls++;
          final now = DateTime.now().toUtc();
          return http.Response(json.encode({
            'access_token': jwtRotated,
            'refresh_token': 'rt_rotated',
            'token_type': 'Bearer',
            'expires_in': 3600,
            'expires_at': now.add(const Duration(hours: 1)).toIso8601String(),
            'refresh_expires_at': now.add(const Duration(days: 30)).toIso8601String(),
            'device_id': 'device_f11',
            'patient_id': 'patient_A',
            'scope': 'vitals:write vitals:read',
          }), 200, headers: {'content-type': 'application/json'});
        }
        return http.Response('Not Found', 404);
      });

      final storage = InMemoryAuthTokenStorage();
      final sessionA = AuthSession(
        accessToken: jwtInitial,
        refreshToken: 'rt_f11',
        tokenType: 'Bearer',
        expiresIn: 3600,
        expiresAt: DateTime.now().add(const Duration(hours: 1)),
        refreshExpiresAt: DateTime.now().add(const Duration(days: 30)),
        deviceId: 'device_f11',
        patientId: 'patient_A',
        scope: 'vitals:write vitals:read',
      );
      await storage.saveSession(sessionA);

      final harness = await RuntimeTestHarness.create(mockClient: mockClient, storage: storage);
      await harness.syncQueue.init();
      await harness.syncQueue.resolveActiveCycle();

      await harness.healthDataProvider.ingestMeasurement(HealthMeasurement(
        timestamp: DateTime.now(),
        pulse: 82,
        source: HealthDataSource.watchSensor,
      ));

      // Flush triggers 401 -> refresh -> retry batch -> 401 -> expired
      final flushResult = await harness.syncQueue.flush();
      expect(flushResult.status, equals(SyncStatus.paused));

      // Assertions on D10 hardening:
      expect(refreshCalls, equals(1)); // Exactly ONE refresh call
      expect(batchCalls, equals(2));   // Exactly TWO batch calls (initial + retry)
      expect(harness.authManager.status, equals(AuthSessionStatus.expired));
      expect(harness.syncQueue.isPaused, isTrue);
      expect(harness.syncQueue.pauseReason, contains('Session expired'));
      expect(harness.healthDataProvider.isSyncPaused, isTrue);
      expect(harness.healthDataProvider.syncPauseReason, contains('Session expired'));

      // Subsequent flush attempt makes 0 HTTP calls
      final flushResult2 = await harness.syncQueue.flush();
      expect(flushResult2.status, equals(SyncStatus.paused));
      expect(batchCalls, equals(2));
      expect(refreshCalls, equals(1));

      harness.dispose();
    });

    testWidgets('F15: Runtime tree provides singleton VitalsSyncQueue preventing concurrent snapshot writers', (tester) async {
      final mockClient = MockClient((request) async => http.Response('Not Found', 404));
      final storage = InMemoryAuthTokenStorage();

      late BuildContext savedContext;

      await tester.pumpWidget(
        MultiProvider(
          providers: [
            ChangeNotifierProvider<TimeService>.value(value: TimeService()),
            ChangeNotifierProvider<VaultService>(create: (_) => VaultService()),
            ChangeNotifierProxyProvider2<TimeService, VaultService, VitalsMonitorService>(
              create: (ctx) => VitalsMonitorService(ctx.read<TimeService>(), ctx.read<VaultService>()),
              update: (ctx, time, vault, prev) => prev ?? VitalsMonitorService(time, vault),
            ),
            Provider<AegisApiClient>(
              create: (_) => AegisApiClient(baseUrl: 'http://127.0.0.1:8000', client: mockClient),
              dispose: (_, client) => client.close(),
            ),
            ChangeNotifierProvider<AuthSessionManager>(
              create: (ctx) => AuthSessionManager(apiClient: ctx.read<AegisApiClient>(), storage: storage),
            ),
            ChangeNotifierProvider<VitalsSyncQueue>(
              create: (ctx) => VitalsSyncQueue(
                apiClient: ctx.read<AegisApiClient>(),
                authSessionManager: ctx.read<AuthSessionManager>(),
                enforcePatientIsolation: true,
              ),
            ),
            ChangeNotifierProxyProvider3<VaultService, VitalsMonitorService, VitalsSyncQueue, HealthDataProvider>(
              create: (ctx) => HealthDataProvider(
                vaultService: ctx.read<VaultService>(),
                vitalsMonitorService: ctx.read<VitalsMonitorService>(),
                syncQueue: ctx.read<VitalsSyncQueue>(),
              ),
              update: (ctx, vault, vitals, queue, prev) =>
                  (prev ?? HealthDataProvider(vaultService: vault, vitalsMonitorService: vitals, syncQueue: queue))
                    ..updateDependencies(vaultService: vault, vitalsMonitorService: vitals, syncQueue: queue),
            ),
          ],
          child: MaterialApp(
            home: Builder(
              builder: (context) {
                savedContext = context;
                return const Scaffold(body: Text('Singleton Check'));
              },
            ),
          ),
        ),
      );

      final queue1 = savedContext.read<VitalsSyncQueue>();
      final queue2 = Provider.of<VitalsSyncQueue>(savedContext, listen: false);
      final provider = savedContext.read<HealthDataProvider>();

      expect(identical(queue1, queue2), isTrue);
      expect(identical(provider.syncQueue, queue1), isTrue);

      // Trigger dependency update (e.g. rebuild)
      savedContext.read<TimeService>().notifyListeners();
      await tester.pump();

      final queueAfterUpdate = savedContext.read<VitalsSyncQueue>();
      final providerAfterUpdate = savedContext.read<HealthDataProvider>();

      expect(identical(queue1, queueAfterUpdate), isTrue);
      expect(identical(providerAfterUpdate.syncQueue, queue1), isTrue);

      await tester.pumpWidget(const SizedBox.shrink());
    });
  });

  // =========================================================================
  // 5. APP LIFECYCLE, UI DIAGNOSTICS & TEARDOWN (F12–F14)
  // =========================================================================
  group('5. APP LIFECYCLE, UI DIAGNOSTICS & TEARDOWN (F12–F14)', () {
    testWidgets('F12: AppLifecycle paused/resumed stops/starts periodic sync and avoids HTTP flush when unauthenticated', (tester) async {
      int httpCalls = 0;
      final mockClient = MockClient((request) async {
        httpCalls++;
        return http.Response('Unexpected request', 500);
      });

      final storage = InMemoryAuthTokenStorage();
      late VitalsSyncQueue testQueue;

      await tester.pumpWidget(
        MultiProvider(
          providers: [
            ChangeNotifierProvider<VaultService>(create: (_) => VaultService()),
            ChangeNotifierProvider<BrainService>(create: (_) => BrainService()),
            ChangeNotifierProvider<TimeService>.value(value: TimeService()),
            Provider<AthenaTriggerService>(create: (_) => AthenaTriggerService()),
            ChangeNotifierProxyProvider<VaultService, AegisCascadeController>(
              create: (ctx) => AegisCascadeController(vaultService: ctx.read<VaultService>()),
              update: (ctx, vault, prev) => prev ?? AegisCascadeController(vaultService: vault),
            ),
            ChangeNotifierProxyProvider2<TimeService, VaultService, VitalsMonitorService>(
              create: (ctx) => VitalsMonitorService(ctx.read<TimeService>(), ctx.read<VaultService>()),
              update: (ctx, time, vault, prev) => prev!..updateTimeService(time),
            ),
            Provider<AegisApiClient>(
              create: (_) => AegisApiClient(baseUrl: 'http://127.0.0.1:8000', client: mockClient),
              dispose: (_, client) => client.close(),
            ),
            ChangeNotifierProvider<AuthSessionManager>(
              create: (ctx) => AuthSessionManager(apiClient: ctx.read<AegisApiClient>(), storage: storage),
            ),
            ChangeNotifierProvider<VitalsSyncQueue>(
              create: (ctx) {
                testQueue = VitalsSyncQueue(
                  apiClient: ctx.read<AegisApiClient>(),
                  authSessionManager: ctx.read<AuthSessionManager>(),
                  enforcePatientIsolation: true,
                );
                testQueue.init();
                return testQueue;
              },
            ),
            ChangeNotifierProxyProvider3<VaultService, VitalsMonitorService, VitalsSyncQueue, HealthDataProvider>(
              create: (ctx) => HealthDataProvider(
                vaultService: ctx.read<VaultService>(),
                vitalsMonitorService: ctx.read<VitalsMonitorService>(),
                syncQueue: ctx.read<VitalsSyncQueue>(),
              ),
              update: (ctx, vault, vitals, queue, prev) =>
                  (prev ?? HealthDataProvider(vaultService: vault, vitalsMonitorService: vitals, syncQueue: queue))
                    ..updateDependencies(vaultService: vault, vitalsMonitorService: vitals, syncQueue: queue),
            ),
          ],
          child: const AegisApp(),
        ),
      );

      await tester.pumpAndSettle();

      // Lifecycle transition: AppLifecycleState.paused
      tester.binding.handleAppLifecycleStateChanged(AppLifecycleState.paused);
      await tester.pump();
      expect(testQueue.isPeriodicSyncRunning, isFalse);

      // Lifecycle transition: AppLifecycleState.resumed
      tester.binding.handleAppLifecycleStateChanged(AppLifecycleState.resumed);
      await tester.pump();
      expect(testQueue.isPeriodicSyncRunning, isTrue);

      // When unauthenticated / paused, resume does NOT execute flush HTTP calls
      expect(httpCalls, equals(0));

      await tester.pumpWidget(const SizedBox.shrink());
    });

    testWidgets('F13: Complete graph teardown detaches all listeners, tokenProvider, and permits safe re-initialization', (tester) async {
      final mockClient = MockClient((request) async => http.Response('Not Found', 404));
      final storage = InMemoryAuthTokenStorage();

      AegisApiClient? client;
      AuthSessionManager? manager;
      VitalsSyncQueue? queue;
      HealthDataProvider? provider;

      await tester.pumpWidget(
        MultiProvider(
          providers: [
            ChangeNotifierProvider<TimeService>.value(value: TimeService()),
            ChangeNotifierProvider<VaultService>(create: (_) => VaultService()),
            ChangeNotifierProxyProvider2<TimeService, VaultService, VitalsMonitorService>(
              create: (ctx) => VitalsMonitorService(ctx.read<TimeService>(), ctx.read<VaultService>()),
              update: (ctx, time, vault, prev) => prev ?? VitalsMonitorService(time, vault),
            ),
            Provider<AegisApiClient>(
              create: (_) => AegisApiClient(baseUrl: 'http://127.0.0.1:8000', client: mockClient),
              dispose: (_, c) => c.close(),
            ),
            ChangeNotifierProvider<AuthSessionManager>(
              create: (ctx) => AuthSessionManager(apiClient: ctx.read<AegisApiClient>(), storage: storage),
            ),
            ChangeNotifierProvider<VitalsSyncQueue>(
              create: (ctx) => VitalsSyncQueue(
                apiClient: ctx.read<AegisApiClient>(),
                authSessionManager: ctx.read<AuthSessionManager>(),
                enforcePatientIsolation: true,
              ),
            ),
            ChangeNotifierProxyProvider3<VaultService, VitalsMonitorService, VitalsSyncQueue, HealthDataProvider>(
              create: (ctx) => HealthDataProvider(
                vaultService: ctx.read<VaultService>(),
                vitalsMonitorService: ctx.read<VitalsMonitorService>(),
                syncQueue: ctx.read<VitalsSyncQueue>(),
              ),
              update: (ctx, vault, vitals, q, prev) =>
                  (prev ?? HealthDataProvider(vaultService: vault, vitalsMonitorService: vitals, syncQueue: q))
                    ..updateDependencies(vaultService: vault, vitalsMonitorService: vitals, syncQueue: q),
            ),
          ],
          child: MaterialApp(
            home: Builder(
              builder: (context) {
                client = context.read<AegisApiClient>();
                manager = context.read<AuthSessionManager>();
                queue = context.read<VitalsSyncQueue>();
                provider = context.read<HealthDataProvider>();
                return const Scaffold(body: Text('Running'));
              },
            ),
          ),
        ),
      );

      expect(client, isNotNull);
      expect(manager, isNotNull);
      expect(queue, isNotNull);
      expect(provider, isNotNull);

      // Tear down widget tree
      await tester.pumpWidget(const SizedBox.shrink());

      // After tree unmount, tokenProvider is detached
      expect(client!.tokenProvider, isNull);

      // Verification: A new manager and queue can be created immediately without active instance collision
      final client2 = AegisApiClient(baseUrl: 'http://127.0.0.1:8000', client: mockClient);
      final manager2 = AuthSessionManager(apiClient: client2, storage: storage);
      final queue2 = VitalsSyncQueue(apiClient: client2, authSessionManager: manager2, enforcePatientIsolation: true);

      expect(manager2.isInitialized, isFalse);
      expect(queue2.isPatientIsolationEnabled, isTrue);

      queue2.dispose();
      manager2.dispose();
      client2.close();
    });

    testWidgets('F14: HealthDataProvider UI diagnostics update reactively on queue events', (tester) async {
      final mockClient = MockClient((request) async {
        if (request.url.path == '/api/v1/cycle/current') {
          return http.Response(json.encode({
            'cycle_id': 'cycle_f14',
            'patient_id': 'patient_A',
            'status': 'active',
            'started_at': DateTime.now().toUtc().toIso8601String(),
          }), 200);
        }
        if (request.url.path == '/api/v1/vitals/batch') {
          return http.Response(json.encode({
            'accepted': 1,
            'failed': 0,
            'quarantined': 0,
            'recorded': 1,
            'deduplicated': 0,
          }), 200);
        }
        return http.Response('Not Found', 404);
      });

      final jwtA = _buildTestJwt(patientId: 'patient_A', deviceId: 'device_f14');
      final jwtB = _buildTestJwt(patientId: 'patient_B', deviceId: 'device_f14');

      final storage = InMemoryAuthTokenStorage();
      final sessionA = AuthSession(
        accessToken: jwtA,
        refreshToken: 'rt_f14_a',
        tokenType: 'Bearer',
        expiresIn: 3600,
        expiresAt: DateTime.now().add(const Duration(hours: 1)),
        refreshExpiresAt: DateTime.now().add(const Duration(days: 30)),
        deviceId: 'device_f14',
        patientId: 'patient_A',
        scope: 'vitals:write vitals:read',
      );
      await storage.saveSession(sessionA);

      late HealthDataProvider providerRef;
      late VitalsSyncQueue queueRef;
      late AuthSessionManager managerRef;

      await tester.pumpWidget(
        MultiProvider(
          providers: [
            ChangeNotifierProvider<TimeService>.value(value: TimeService()),
            ChangeNotifierProvider<VaultService>(create: (_) => VaultService()),
            ChangeNotifierProxyProvider2<TimeService, VaultService, VitalsMonitorService>(
              create: (ctx) => VitalsMonitorService(ctx.read<TimeService>(), ctx.read<VaultService>()),
              update: (ctx, time, vault, prev) => prev ?? VitalsMonitorService(time, vault),
            ),
            Provider<AegisApiClient>(
              create: (_) => AegisApiClient(baseUrl: 'http://127.0.0.1:8000', client: mockClient),
              dispose: (_, c) => c.close(),
            ),
            ChangeNotifierProvider<AuthSessionManager>(
              create: (ctx) {
                managerRef = AuthSessionManager(apiClient: ctx.read<AegisApiClient>(), storage: storage);
                return managerRef;
              },
            ),
            ChangeNotifierProvider<VitalsSyncQueue>(
              create: (ctx) {
                queueRef = VitalsSyncQueue(
                  apiClient: ctx.read<AegisApiClient>(),
                  authSessionManager: ctx.read<AuthSessionManager>(),
                  enforcePatientIsolation: true,
                );
                return queueRef;
              },
            ),
            ChangeNotifierProxyProvider3<VaultService, VitalsMonitorService, VitalsSyncQueue, HealthDataProvider>(
              create: (ctx) {
                providerRef = HealthDataProvider(
                  vaultService: ctx.read<VaultService>(),
                  vitalsMonitorService: ctx.read<VitalsMonitorService>(),
                  syncQueue: ctx.read<VitalsSyncQueue>(),
                );
                return providerRef;
              },
              update: (ctx, vault, vitals, q, prev) =>
                  (prev ?? HealthDataProvider(vaultService: vault, vitalsMonitorService: vitals, syncQueue: q))
                    ..updateDependencies(vaultService: vault, vitalsMonitorService: vitals, syncQueue: q),
            ),
          ],
          child: MaterialApp(
            home: Scaffold(
              body: Consumer<HealthDataProvider>(
                builder: (context, provider, _) {
                  return Column(
                    children: [
                      Text('ActivePatient: ${provider.activePatientId ?? "none"}'),
                      Text('Pending: ${provider.syncPendingCount}'),
                      Text('TotalPending: ${provider.syncTotalPendingCount}'),
                      Text('Quarantined: ${provider.syncQuarantinedCount}'),
                    ],
                  );
                },
              ),
            ),
          ),
        ),
      );

      await queueRef.init();
      await queueRef.resolveActiveCycle();
      await tester.pump();

      expect(find.text('ActivePatient: patient_A'), findsOneWidget);
      expect(find.text('Pending: 0'), findsOneWidget);
      expect(find.text('TotalPending: 0'), findsOneWidget);

      // Ingest measurement for Patient A
      await providerRef.ingestMeasurement(HealthMeasurement(
        timestamp: DateTime.now(),
        pulse: 78,
        source: HealthDataSource.watchSensor,
      ));
      await tester.pump();

      expect(find.text('Pending: 1'), findsOneWidget);
      expect(find.text('TotalPending: 1'), findsOneWidget);
      expect(find.text('Quarantined: 0'), findsOneWidget);

      // Flush queue -> UI updates automatically without calling ingest
      await queueRef.flush();
      await tester.pump();

      expect(find.text('Pending: 0'), findsOneWidget);
      expect(find.text('TotalPending: 0'), findsOneWidget);

      // Switch to Patient B
      final sessionB = AuthSession(
        accessToken: jwtB,
        refreshToken: 'rt_f14_b',
        tokenType: 'Bearer',
        expiresIn: 3600,
        expiresAt: DateTime.now().add(const Duration(hours: 1)),
        refreshExpiresAt: DateTime.now().add(const Duration(days: 30)),
        deviceId: 'device_f14',
        patientId: 'patient_B',
        scope: 'vitals:write vitals:read',
      );
      await storage.saveSession(sessionB);
      managerRef.setSessionForTesting(sessionB, status: AuthSessionStatus.authenticated);
      await tester.pump();

      expect(find.text('ActivePatient: patient_B'), findsOneWidget);
      expect(find.text('Pending: 0'), findsOneWidget);

      await tester.pumpWidget(const SizedBox.shrink());
    });
  });
}
