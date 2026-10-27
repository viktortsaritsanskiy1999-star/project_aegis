// ignore_for_file: depend_on_referenced_packages
import 'dart:convert';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'dart:async';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:shared_preferences_platform_interface/shared_preferences_platform_interface.dart';
import 'package:shared_preferences_platform_interface/types.dart';

import 'package:aegis_health/services/api/aegis_api_client.dart';
import 'package:aegis_health/services/sync/vitals_sync_queue.dart';
import 'package:aegis_health/services/auth/auth_session_manager.dart';
import 'package:aegis_health/services/auth/auth_token_storage.dart';
import 'package:aegis_health/services/auth/auth_models.dart';
import 'package:aegis_health/services/health_data_provider.dart';
import 'package:aegis_health/services/vault_service.dart';
import 'package:aegis_health/services/vitals_monitor_service.dart';
import 'package:aegis_health/services/time_service.dart';
import 'package:aegis_health/services/athena_trigger_service.dart';
import 'package:aegis_health/services/aegis_cascade_controller.dart';
import 'package:aegis_health/main.dart';

class TestSharedPreferencesStore extends SharedPreferencesStorePlatform {
  bool shouldFail = false;
  int successWritesRemaining = -1;
  Completer<void>? delayNextWrite;
  final Map<String, Object> _data = {};

  TestSharedPreferencesStore({Map<String, Object>? initialData}) {
    if (initialData != null) {
      _data.addAll(initialData);
    }
  }

  @override
  Future<bool> setValue(String valueType, String key, Object value) async {
    if (delayNextWrite != null) {
      final completer = delayNextWrite!;
      delayNextWrite = null; // one-shot delay
      await completer.future;
    }
    if (shouldFail) {
      throw Exception('Simulated disk I/O failure (Disk full / permission denied)');
    }
    if (successWritesRemaining == 0) {
      throw Exception('Simulated disk I/O failure after planned successful writes');
    }
    if (successWritesRemaining > 0) {
      successWritesRemaining--;
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

/// Builds a structurally valid 3-part base64url JWT string for tests.
String _buildTestJwt({
  String patientId = 'test_patient_52',
  String deviceId = 'test_device_52',
}) {
  final headerJson = json.encode({'alg': 'HS256', 'typ': 'JWT'});
  final payloadJson = json.encode({
    'sub': patientId,
    'did': deviceId,
    'type': 'access',
  });
  final h = base64Url.encode(utf8.encode(headerJson)).replaceAll('=', '');
  final p = base64Url.encode(utf8.encode(payloadJson)).replaceAll('=', '');
  const s = 'c2lnbmF0dXJlX2Zha2Vfc2VjcmV0X2FkZHM';
  return '$h.$p.$s';
}

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  setUp(() {
    SharedPreferences.resetStatic();
    SharedPreferences.setMockInitialValues({});
    AuthSessionManager.resetActiveInstancesForTesting();
  });

  group('Stage 5.2B: Flutter Runtime Wiring & Lifecycle Integration', () {
    test('1. Startup restoration from local storage and re-initialization guard', () async {
      final initialSnapshot = {
        'version': 2,
        'saved_at': DateTime.utc(2026, 10, 23, 10, 0, 0).toIso8601String(),
        'active_cycle_id': 'cycle_startup_001',
        'event_counter': 10,
        'pending': [
          {
            'event_id': 'evt_1001',
            'idempotency_key': 'idem_cycle_startup_001_evt_1001',
            'cycle_id': 'cycle_startup_001',
            'dto': {
              'cycle_id': 'cycle_startup_001',
              'measured_at': DateTime.utc(2026, 10, 23, 10, 0, 0).toIso8601String(),
              'source': 'watch_sensor',
              'pulse': 72,
              'is_simulated': false,
              'is_historical': false,
              'phase': 'standby',
            },
            'enqueued_at': DateTime.utc(2026, 10, 23, 10, 0, 0).toIso8601String(),
            'retry_count': 0,
            'is_quarantined': false,
          },
          {
            'event_id': 'evt_1002',
            'idempotency_key': 'idem_cycle_startup_001_evt_1002',
            'cycle_id': 'cycle_startup_001',
            'dto': {
              'cycle_id': 'cycle_startup_001',
              'measured_at': DateTime.utc(2026, 10, 23, 10, 1, 0).toIso8601String(),
              'source': 'manual_input',
              'pulse': 85,
              'is_simulated': true,
              'is_historical': false,
              'phase': 'standby',
            },
            'enqueued_at': DateTime.utc(2026, 10, 23, 10, 1, 0).toIso8601String(),
            'retry_count': 0,
            'is_quarantined': false,
          },
        ],
        'quarantine': [
          {
            'event_id': 'evt_1000',
            'idempotency_key': 'idem_cycle_startup_001_evt_1000',
            'cycle_id': 'cycle_startup_001',
            'dto': {
              'cycle_id': 'cycle_startup_001',
              'measured_at': DateTime.utc(2026, 10, 23, 9, 59, 0).toIso8601String(),
              'source': 'test_chip',
              'pulse': 140,
              'is_simulated': true,
              'is_historical': false,
              'phase': 'standby',
            },
            'enqueued_at': DateTime.utc(2026, 10, 23, 9, 59, 0).toIso8601String(),
            'retry_count': 1,
            'last_error': '409 Conflict',
            'is_quarantined': true,
          }
        ],
      };

      SharedPreferences.setMockInitialValues({
        'aegis_vitals_sync_snapshot_v2': json.encode(initialSnapshot),
      });

      final mockClient = MockClient((request) async {
        return http.Response('{"status": "ok"}', 200);
      });
      final apiClient = AegisApiClient(
        baseUrl: 'http://127.0.0.1:8000',
        tokenProvider: () async => 'test_cycle_token',
        client: mockClient,
      );

      final queue = VitalsSyncQueue(apiClient: apiClient);
      expect(queue.isInitialized, isFalse);
      expect(queue.initialized, isFalse);
      expect(queue.pendingCount, equals(0));

      await queue.init();

      expect(queue.isInitialized, isTrue);
      expect(queue.initialized, isTrue);
      expect(queue.pendingCount, equals(2));
      expect(queue.quarantinedCount, equals(1));
      expect(queue.activeCycleId, equals('cycle_startup_001'));
      expect(queue.lastSyncError, isNull);

      // Re-initialization guard: calling init() again does not overwrite or fail
      await queue.init();
      expect(queue.pendingCount, equals(2));
      expect(queue.quarantinedCount, equals(1));
    });

    test('2. Offline startup: network absence does not prevent queue initialization or crash app', () async {
      final mockClient = MockClient((request) async {
        throw http.ClientException('Network unreachable (Offline / airplane mode)');
      });
      final apiClient = AegisApiClient(
        baseUrl: 'http://127.0.0.1:8000',
        tokenProvider: () async => _buildTestJwt(patientId: 'p_offline'),
        client: mockClient,
      );

      final queue = VitalsSyncQueue(
        apiClient: apiClient,
        activeCycleId: 'c_offline',
        activePatientId: 'p_offline',
      );

      // Queue initialization uses local storage only, so it succeeds even offline
      await queue.init();
      expect(queue.isInitialized, isTrue);
      expect(queue.pendingCount, equals(0));

      // Enqueueing while offline succeeds locally
      final item = await queue.enqueue(HealthMeasurement(
        timestamp: DateTime.now(),
        pulse: 70,
        source: HealthDataSource.watchSensor,
      ));
      expect(item.eventId, isNotEmpty);
      expect(queue.pendingCount, equals(1));

      // Attempting flush handles network exception gracefully without throwing uncaught exceptions
      final result = await queue.flush();
      expect(result.status, equals(SyncStatus.retryableError));
      expect(queue.pendingCount, equals(1));
      expect(queue.lastSyncError, contains('Network unreachable'));
    });

    test('3. Medical cycle: real cycle_id becomes available later and binds pending items without dummy cycle', () async {
      int getCycleCalls = 0;
      final mockClient = MockClient((request) async {
        if (request.url.path == '/api/v1/cycle/current') {
          getCycleCalls++;
          if (getCycleCalls == 1) {
            return http.Response(json.encode({'detail': 'No active cycle'}), 404);
          }
          return http.Response(json.encode({
            'cycle_id': 'cycle_late_999',
            'patient_id': 'patient_01',
            'status': 'active',
            'started_at': DateTime.now().toUtc().toIso8601String(),
          }), 200);
        }
        if (request.url.path == '/api/v1/vitals/batch') {
          return http.Response(json.encode({
            'status': 'success',
            'cycle_id': 'cycle_late_999',
            'recorded_count': 1,
            'deduplicated_count': 0,
            'batch_size': 1,
            'received_at': DateTime.now().toUtc().toIso8601String(),
          }), 200);
        }
        return http.Response('Not Found', 404);
      });

      final apiClient = AegisApiClient(
        baseUrl: 'http://127.0.0.1:8000',
        tokenProvider: () async => 'test_cycle_token',
        client: mockClient,
      );

      final queue = VitalsSyncQueue(apiClient: apiClient);
      await queue.init();

      // Enqueue with no cycle: never use fictitious cycle
      await queue.enqueue(HealthMeasurement(
        timestamp: DateTime.now(),
        pulse: 75,
        source: HealthDataSource.watchSensor,
      ));

      expect(queue.pendingItems.first.cycleId, isNull);
      expect(queue.pendingItems.first.idempotencyKey, isNull);

      // First flush: cycle query returns 404 -> pause queue safely
      final result1 = await queue.flush();
      expect(result1.status, equals(SyncStatus.paused));
      expect(queue.isPaused, isTrue);
      expect(queue.lastSyncError, contains('404 Cycle Not Found'));
      expect(queue.pendingCount, equals(1));

      // Later, cycle is activated and queue is notified
      await queue.setActiveCycleId('cycle_late_999');

      expect(queue.isPaused, isFalse);
      expect(queue.activeCycleId, equals('cycle_late_999'));
      expect(queue.pendingItems.first.cycleId, equals('cycle_late_999'));
      expect(queue.pendingItems.first.idempotencyKey, startsWith('idem_cycle_late_999_'));

      // Next flush successfully sends batch to real cycle
      final result2 = await queue.flush();
      expect(result2.status, equals(SyncStatus.success));
      expect(queue.pendingCount, equals(0));
      expect(queue.lastSyncError, isNull);
      expect(queue.lastSyncSuccessAt, isNotNull);
    });

    test('4. Restoration of pending items across simulated app restarts', () async {
      final mockClient = MockClient((request) async => http.Response('ok', 200));
      final apiClient = AegisApiClient(baseUrl: 'http://127.0.0.1:8000', client: mockClient);

      // Session 1
      final queue1 = VitalsSyncQueue(apiClient: apiClient, activeCycleId: 'c_restart');
      await queue1.init();

      final m1 = HealthMeasurement(
        timestamp: DateTime.utc(2026, 10, 23, 11, 0, 0),
        pulse: 80,
        source: HealthDataSource.watchSensor,
      );
      final m2 = HealthMeasurement(
        timestamp: DateTime.utc(2026, 10, 23, 11, 1, 0),
        pulse: 90,
        source: HealthDataSource.manualInput,
        isSimulated: true,
      );

      await queue1.enqueue(m1);
      await queue1.enqueue(m2);
      expect(queue1.pendingCount, equals(2));

      // Simulate App Termination & Restart (Session 2)
      final queue2 = VitalsSyncQueue(apiClient: apiClient);
      await queue2.init();

      expect(queue2.isInitialized, isTrue);
      expect(queue2.pendingCount, equals(2));
      expect(queue2.activeCycleId, equals('c_restart'));
      expect(queue2.pendingItems[0].dto.pulse, equals(80));
      expect(queue2.pendingItems[0].dto.source, equals('watch_sensor'));
      expect(queue2.pendingItems[1].dto.pulse, equals(90));
      expect(queue2.pendingItems[1].dto.source, equals('manual_input'));
      expect(queue2.pendingItems[1].dto.isSimulated, isTrue);
    });

    test('5. Flush triggers: debounced flush, manual flush, and no parallel flushes', () async {
      int batchRequests = 0;
      final mockClient = MockClient((request) async {
        if (request.url.path == '/api/v1/vitals/batch') {
          batchRequests++;
          // Simulate some network latency
          await Future.delayed(const Duration(milliseconds: 50));
          return http.Response(json.encode({
            'status': 'success',
            'cycle_id': 'c_parallel',
            'recorded_count': 1,
            'deduplicated_count': 0,
            'batch_size': 1,
            'received_at': DateTime.now().toUtc().toIso8601String(),
          }), 200);
        }
        return http.Response('Not Found', 404);
      });

      final apiClient = AegisApiClient(
        baseUrl: 'http://127.0.0.1:8000',
        tokenProvider: () async => _buildTestJwt(patientId: 'c_parallel'),
        client: mockClient,
      );
      final queue = VitalsSyncQueue(
        apiClient: apiClient,
        activeCycleId: 'c_parallel',
        activePatientId: 'c_parallel',
      );
      await queue.init();

      await queue.enqueue(HealthMeasurement(
        timestamp: DateTime.now(),
        pulse: 72,
        source: HealthDataSource.watchSensor,
      ));

      // Trigger multiple concurrent flushes simultaneously
      final future1 = queue.flush();
      final future2 = queue.flush();
      final future3 = queue.flush();

      final results = await Future.wait([future1, future2, future3]);

      // Exactly ONE flush should process the batch; the other calls should observe empty pending or idle
      expect(batchRequests, equals(1));
      expect(results.any((r) => r.status == SyncStatus.success), isTrue);
      expect(queue.pendingCount, equals(0));
    });

    test('6. Handling 401 Unauthorized: pauses queue, records diagnostic, preserves items', () async {
      final mockClient = MockClient((request) async {
        return http.Response(json.encode({'detail': 'Invalid or expired token'}), 401);
      });

      final apiClient = AegisApiClient(
        baseUrl: 'http://127.0.0.1:8000',
        tokenProvider: () async => _buildTestJwt(patientId: 'c_auth_test'),
        client: mockClient,
      );
      final queue = VitalsSyncQueue(
        apiClient: apiClient,
        activeCycleId: 'c_auth_test',
        activePatientId: 'c_auth_test',
      );
      await queue.init();

      await queue.enqueue(HealthMeasurement(
        timestamp: DateTime.now(),
        pulse: 77,
        source: HealthDataSource.watchSensor,
      ));

      final result = await queue.flush();
      expect(result.status, equals(SyncStatus.paused));
      expect(queue.isPaused, isTrue);
      expect(queue.paused, isTrue);
      expect(queue.lastSyncError, contains('401 Unauthorized'));
      // Data is retained in queue, NEVER lost
      expect(queue.pendingCount, equals(1));

      // Subsequent flushes while paused are immediately skipped
      final result2 = await queue.flush();
      expect(result2.status, equals(SyncStatus.paused));
    });

    test('7. Handling 404 Cycle Not Found: pauses queue, records diagnostic, preserves items', () async {
      final mockClient = MockClient((request) async {
        return http.Response(json.encode({'detail': 'Medical cycle not found'}), 404);
      });

      final apiClient = AegisApiClient(
        baseUrl: 'http://127.0.0.1:8000',
        tokenProvider: () async => _buildTestJwt(patientId: 'c_nonexistent'),
        client: mockClient,
      );
      final queue = VitalsSyncQueue(
        apiClient: apiClient,
        activeCycleId: 'c_nonexistent',
        activePatientId: 'c_nonexistent',
      );
      await queue.init();

      await queue.enqueue(HealthMeasurement(
        timestamp: DateTime.now(),
        pulse: 88,
        source: HealthDataSource.watchSensor,
      ));

      final result = await queue.flush();
      expect(result.status, equals(SyncStatus.paused));
      expect(queue.isPaused, isTrue);
      expect(queue.lastSyncError, contains('404 Cycle Not Found'));
      expect(queue.pendingCount, equals(1));
    });

    test('8. Retryable network error with backoff followed by successful recovery', () async {
      int attempts = 0;
      final mockClient = MockClient((request) async {
        attempts++;
        if (attempts == 1) {
          return http.Response('Service Temporarily Unavailable', 503);
        }
        return http.Response(json.encode({
          'status': 'success',
          'cycle_id': 'c_retry_test',
          'recorded_count': 1,
          'deduplicated_count': 0,
          'batch_size': 1,
          'received_at': DateTime.now().toUtc().toIso8601String(),
        }), 200);
      });

      final apiClient = AegisApiClient(
        baseUrl: 'http://127.0.0.1:8000',
        tokenProvider: () async => _buildTestJwt(patientId: 'c_retry_test'),
        client: mockClient,
      );
      final queue = VitalsSyncQueue(
        apiClient: apiClient,
        activeCycleId: 'c_retry_test',
        activePatientId: 'c_retry_test',
      );
      await queue.init();

      await queue.enqueue(HealthMeasurement(
        timestamp: DateTime.now(),
        pulse: 80,
        source: HealthDataSource.watchSensor,
      ));

      // 1st attempt fails with 503
      final result1 = await queue.flush();
      expect(result1.status, equals(SyncStatus.retryableError));
      expect(queue.pendingCount, equals(1));
      expect(queue.lastSyncError, contains('Service Temporarily Unavailable'));
      expect(queue.pendingItems.first.retryCount, equals(1));

      // Reset backoff delay for immediate retry in test
      queue.resume();

      // 2nd attempt succeeds
      final result2 = await queue.flush();
      expect(result2.status, equals(SyncStatus.success));
      expect(queue.pendingCount, equals(0));
      expect(queue.lastSyncError, isNull);
      expect(queue.lastSyncSuccessAt, isNotNull);
    });

    test('9. Resource cleanup on dispose cancels scheduled timers without errors', () async {
      final mockClient = MockClient((request) async => http.Response('ok', 200));
      final apiClient = AegisApiClient(baseUrl: 'http://127.0.0.1:8000', client: mockClient);
      final queue = VitalsSyncQueue(apiClient: apiClient, activeCycleId: 'c_cleanup');
      await queue.init();

      queue.startPeriodicSync(interval: const Duration(milliseconds: 100));
      queue.scheduleFlush(delay: const Duration(milliseconds: 500));

      expect(() => queue.dispose(), returnsNormally);
    });

    test('10. Single pulse processing from WorkoutSessionModal with exact session & simulated flags', () async {
      final timeService = TimeService();
      final vaultService = VaultService();
      await vaultService.initFuture;

      vaultService.saveVitalsThresholds({
        'min_pessimum_bpm': 50,
        'max_pessimum_bpm': 130,
        'max_spike_delta_bpm': 35,
        'clinical_rationale': 'Test thresholds',
      });

      final vitalsService = VitalsMonitorService(timeService, vaultService);

      final mockClient = MockClient((request) async => http.Response('ok', 200));
      final apiClient = AegisApiClient(baseUrl: 'http://127.0.0.1:8000', client: mockClient);
      final syncQueue = VitalsSyncQueue(apiClient: apiClient, activeCycleId: 'c_workout_test');
      await syncQueue.init();

      final healthProvider = HealthDataProvider(
        vaultService: vaultService,
        vitalsMonitorService: vitalsService,
        syncQueue: syncQueue,
      );

      // Start active workout in VitalsMonitorService
      vitalsService.onNewPulseRead(70);
      vitalsService.onNewPulseRead(72);
      vitalsService.onNewPulseRead(135);
      final athenaTrigger = AthenaTriggerService();
      final cascadeController = AegisCascadeController(vaultService: vaultService);
      await vitalsService.handleUserActivityResponse(
        isActivity: true,
        athenaTriggerService: athenaTrigger,
        cascadeController: cascadeController,
      );

      expect(vitalsService.currentState, equals(AlertState.workoutActive));
      final activeSessionId = vitalsService.currentSessionId;
      expect(activeSessionId, isNotNull);
      final initialLogCount = vitalsService.workoutPulseLog.length;

      // 1. Dispatch test chip pulse (simulating _buildPulseChip tap)
      final chipSuccess = await healthProvider.dispatchTestChip(pulse: 145);
      expect(chipSuccess, isTrue);

      // 2. Dispatch custom pulse input (simulating custom pulse OK button)
      final customSuccess = await healthProvider.dispatchManualInput(pulse: 152, isSimulated: true);
      expect(customSuccess, isTrue);

      // Check single processing in VitalsMonitorService workout log
      expect(vitalsService.workoutPulseLog.length, equals(initialLogCount + 2));
      final chipLog = vitalsService.workoutPulseLog[initialLogCount];
      final customLog = vitalsService.workoutPulseLog[initialLogCount + 1];

      expect(chipLog.pulse, equals(145));
      expect(chipLog.isSimulated, isTrue);
      expect(chipLog.source, equals('test_chip'));

      expect(customLog.pulse, equals(152));
      expect(customLog.isSimulated, isTrue);
      expect(customLog.source, equals('manual_input'));

      // Check sync queue enqueued items
      expect(syncQueue.pendingCount, equals(2));
      final item1 = syncQueue.pendingItems[0];
      final item2 = syncQueue.pendingItems[1];

      // Item 1: Test chip
      expect(item1.dto.pulse, equals(145));
      expect(item1.dto.source, equals('test_chip'));
      expect(item1.dto.isSimulated, isTrue);
      expect(item1.dto.sessionId, equals(activeSessionId));
      expect(item1.dto.phase, equals('workout'));

      // Item 2: Custom input
      expect(item2.dto.pulse, equals(152));
      expect(item2.dto.source, equals('manual_input'));
      expect(item2.dto.isSimulated, isTrue);
      expect(item2.dto.sessionId, equals(activeSessionId));
      expect(item2.dto.phase, equals('workout'));

      // Diagnostics check on healthProvider
      expect(healthProvider.hasEnqueueError, isFalse);
      expect(healthProvider.enqueueError, isFalse);
      expect(healthProvider.isSyncQueueInitialized, isTrue);
      expect(healthProvider.syncPendingCount, equals(2));

      healthProvider.dispose();
      syncQueue.dispose();
      vitalsService.dispose();
      vaultService.dispose();
    });

    test('11. Preservation of simulated and historical flags without launching alerts', () async {
      final timeService = TimeService();
      final vaultService = VaultService();
      await vaultService.initFuture;
      final vitalsService = VitalsMonitorService(timeService, vaultService);

      final mockClient = MockClient((request) async => http.Response('ok', 200));
      final apiClient = AegisApiClient(baseUrl: 'http://127.0.0.1:8000', client: mockClient);
      final syncQueue = VitalsSyncQueue(apiClient: apiClient, activeCycleId: 'c_hist_test');
      await syncQueue.init();

      final healthProvider = HealthDataProvider(
        vaultService: vaultService,
        vitalsMonitorService: vitalsService,
        syncQueue: syncQueue,
      );

      // Ingest historical tachycardic measurement (180 bpm)
      final historical = HealthMeasurement(
        timestamp: DateTime.now().subtract(const Duration(hours: 5)),
        pulse: 180,
        source: HealthDataSource.watchSensor,
        isHistorical: true,
      );

      await healthProvider.ingestHistoricalMeasurement(historical);

      // Must NOT trigger active alarms or workouts in VitalsMonitorService
      expect(vitalsService.currentState, equals(AlertState.idle));
      expect(vitalsService.currentPulse, equals(70)); // unchanged default

      // Outbound sync item retains historical flag and phase
      expect(syncQueue.pendingCount, equals(1));
      expect(syncQueue.pendingItems.first.dto.isHistorical, isTrue);
      expect(syncQueue.pendingItems.first.dto.phase, equals('historical'));
      expect(syncQueue.pendingItems.first.dto.pulse, equals(180));

      healthProvider.dispose();
      syncQueue.dispose();
      vitalsService.dispose();
      vaultService.dispose();
    });

    test('12. Local standby monitoring and VaultService sample storage behavior remains intact', () async {
      final timeService = TimeService();
      final vaultService = VaultService();
      await vaultService.initFuture;
      final vitalsService = VitalsMonitorService(timeService, vaultService);

      final mockClient = MockClient((request) async => http.Response('ok', 200));
      final apiClient = AegisApiClient(baseUrl: 'http://127.0.0.1:8000', client: mockClient);
      final syncQueue = VitalsSyncQueue(apiClient: apiClient, activeCycleId: 'c_standby_test');
      await syncQueue.init();

      final healthProvider = HealthDataProvider(
        vaultService: vaultService,
        vitalsMonitorService: vitalsService,
        syncQueue: syncQueue,
      );

      expect(vitalsService.currentState, equals(AlertState.idle));

      // Ingest live resting measurement
      await healthProvider.dispatchWatchSensor(pulse: 68);

      expect(vitalsService.currentPulse, equals(68));

      // Stored in smart_watch heart_rate_samples
      final samples = vaultService.getHeartRateSamples();
      expect(samples.isNotEmpty, isTrue);
      expect(samples.last['pulse'], equals(68));
      expect(samples.last['phase'], equals('standby'));

      healthProvider.dispose();
      syncQueue.dispose();
      vitalsService.dispose();
      vaultService.dispose();
    });

    test('13. Review Issue 1: standardRuntimeTokenProvider returns null and AegisApiClient omits Authorization header', () async {
      // Standard runtime token provider must return null
      final token = await standardRuntimeTokenProvider();
      expect(token, isNull);

      // AegisApiClient with standard token provider omits Authorization header
      String? authHeader;
      final mockClient = MockClient((request) async {
        authHeader = request.headers['Authorization'];
        return http.Response(json.encode({
          'cycle_id': 'cycle_auth_none',
          'patient_id': 'p1',
          'status': 'active',
          'started_at': DateTime.now().toUtc().toIso8601String(),
        }), 200);
      });

      final client = AegisApiClient(
        baseUrl: 'http://127.0.0.1:8000',
        tokenProvider: standardRuntimeTokenProvider,
        client: mockClient,
      );

      final cycle = await client.getCurrentCycle();
      expect(cycle, isNotNull);
      expect(authHeader, isNull);

      // Custom token provider passes Authorization header correctly
      String? customAuth;
      final authedMock = MockClient((request) async {
        customAuth = request.headers['Authorization'];
        return http.Response(json.encode({
          'cycle_id': 'cycle_auth_token',
          'patient_id': 'p1',
          'status': 'active',
          'started_at': DateTime.now().toUtc().toIso8601String(),
        }), 200);
      });

      final authedClient = AegisApiClient(
        baseUrl: 'http://127.0.0.1:8000',
        tokenProvider: () async => 'test_jwt_session_token',
        client: authedMock,
      );

      await authedClient.getCurrentCycle();
      expect(customAuth, equals('Bearer test_jwt_session_token'));
      client.close();
      authedClient.close();
    });

    test('14. Review Issue 2: Queue initialization protection - enqueue and flush await running init()', () async {
      final mockClient = MockClient((request) async {
        return http.Response(json.encode({
          'status': 'success',
          'cycle_id': 'cycle_init_wait',
          'recorded_count': 1,
          'deduplicated_count': 0,
          'batch_size': 1,
          'received_at': DateTime.now().toUtc().toIso8601String(),
        }), 200);
      });
      final apiClient = AegisApiClient(
        baseUrl: 'http://127.0.0.1:8000',
        tokenProvider: () async => _buildTestJwt(patientId: 'cycle_init_wait'),
        client: mockClient,
      );
      final queue = VitalsSyncQueue(
        apiClient: apiClient,
        activeCycleId: 'cycle_init_wait',
        activePatientId: 'cycle_init_wait',
      );

      // Call enqueue and flush while init is initiated concurrently
      final initFuture = queue.init();
      final enqueueFuture = queue.enqueue(HealthMeasurement(
        timestamp: DateTime.now(),
        pulse: 78,
        source: HealthDataSource.watchSensor,
      ));

      await Future.wait([initFuture, enqueueFuture]);

      expect(queue.isInitialized, isTrue);
      expect(queue.pendingCount, equals(1));

      final flushResult = await queue.flush();
      expect(flushResult.status, equals(SyncStatus.success));
      expect(queue.pendingCount, equals(0));
      queue.dispose();
      apiClient.close();
    });

    test('15. Review Issue 2: Corrupted snapshot fails init, protects disk, rejects enqueue and sets hasEnqueueError', () async {
      // Set corrupted JSON in snapshot storage
      const corruptJson = '{"pending": [corrupted_json_syntax';
      SharedPreferences.setMockInitialValues({
        'aegis_vitals_sync_snapshot_v2': corruptJson,
      });

      final mockClient = MockClient((request) async => http.Response('ok', 200));
      final apiClient = AegisApiClient(baseUrl: 'http://127.0.0.1:8000', client: mockClient);
      final queue = VitalsSyncQueue(apiClient: apiClient);

      // init() must throw VitalsQueuePersistenceException
      expect(() => queue.init(), throwsA(isA<VitalsQueuePersistenceException>()));

      try {
        await queue.init();
      } catch (_) {}

      expect(queue.isInitialized, isFalse);
      expect(queue.initError, isNotNull);

      // Verify that disk snapshot was NOT overwritten with empty state
      final prefs = await SharedPreferences.getInstance();
      expect(prefs.getString('aegis_vitals_sync_snapshot_v2'), equals(corruptJson));

      // enqueue() on uninitialized queue must throw VitalsQueuePersistenceException
      expect(
        () => queue.enqueue(HealthMeasurement(
          timestamp: DateTime.now(),
          pulse: 82,
          source: HealthDataSource.watchSensor,
        )),
        throwsA(isA<VitalsQueuePersistenceException>()),
      );

      // flush() on uninitialized queue returns persistenceError result
      final flushResult = await queue.flush();
      expect(flushResult.status, equals(SyncStatus.persistenceError));
      expect(flushResult.persistenceFailed, isTrue);

      // HealthDataProvider observation of enqueue failure
      final timeService = TimeService();
      final vaultService = VaultService();
      await vaultService.initFuture;
      final vitalsService = VitalsMonitorService(timeService, vaultService);

      final healthProvider = HealthDataProvider(
        vaultService: vaultService,
        vitalsMonitorService: vitalsService,
        syncQueue: queue,
      );

      expect(healthProvider.hasEnqueueError, isFalse);
      final success = await healthProvider.dispatchWatchSensor(pulse: 84);
      // Outbound enqueue failed so returns false, while local processing (vault and vitals) continued
      expect(success, isFalse);
      expect(vitalsService.currentPulse, equals(84));
      // But outbound enqueue error flag is raised
      expect(healthProvider.hasEnqueueError, isTrue);
      expect(healthProvider.enqueueError, isTrue);

      healthProvider.dispose();
      queue.dispose();
      vitalsService.dispose();
      vaultService.dispose();
      apiClient.close();
    });

    test('16. Review Issue 3: Safe automatic flush handles network and persistence errors safely', () async {
      // Simulate client that throws uncaught exception during batch flush
      final mockClient = MockClient((request) async {
        throw Exception('Simulated fatal socket reset during batch flush');
      });
      final apiClient = AegisApiClient(
        baseUrl: 'http://127.0.0.1:8000',
        tokenProvider: () async => _buildTestJwt(patientId: 'cycle_auto_flush'),
        client: mockClient,
      );
      final queue = VitalsSyncQueue(
        apiClient: apiClient,
        activeCycleId: 'cycle_auto_flush',
        activePatientId: 'cycle_auto_flush',
      );
      await queue.init();

      await queue.enqueue(HealthMeasurement(
        timestamp: DateTime.now(),
        pulse: 74,
        source: HealthDataSource.watchSensor,
      ));

      expect(queue.pendingCount, equals(1));

      // Schedule flush with short delay
      queue.scheduleFlush(delay: const Duration(milliseconds: 50));
      await Future.delayed(const Duration(milliseconds: 150));

      // Automatic flush handled exception via _safeFlush without crashing process
      expect(queue.lastSyncError, isNotNull);
      expect(queue.lastSyncError, contains('Simulated fatal socket reset'));
      // Data remains intact in pending queue
      expect(queue.pendingCount, equals(1));

      queue.dispose();
      apiClient.close();
    });

    test('17. Review Issue 4: Lifecycle transitions - pause cancels periodic sync and debounced flush; resume restarts periodic sync without duplicate timers', () async {
      final mockClient = MockClient((request) async => http.Response('ok', 200));
      final apiClient = AegisApiClient(baseUrl: 'http://127.0.0.1:8000', client: mockClient);
      final queue = VitalsSyncQueue(apiClient: apiClient, activeCycleId: 'cycle_lifecycle');
      await queue.init();

      expect(queue.isPeriodicSyncRunning, isFalse);

      // Start periodic sync
      queue.startPeriodicSync(interval: const Duration(seconds: 15));
      expect(queue.isPeriodicSyncRunning, isTrue);

      // Calling startPeriodicSync again does NOT duplicate timer
      queue.startPeriodicSync(interval: const Duration(seconds: 15));
      expect(queue.isPeriodicSyncRunning, isTrue);

      // Schedule debounced flush
      queue.scheduleFlush(delay: const Duration(seconds: 5));

      // Simulate AppLifecycleState transition to paused / inactive
      queue.stopPeriodicSync();
      queue.cancelScheduledFlush();
      expect(queue.isPeriodicSyncRunning, isFalse);

      // Simulate AppLifecycleState transition back to resumed
      queue.startPeriodicSync(interval: const Duration(seconds: 15));
      expect(queue.isPeriodicSyncRunning, isTrue);

      queue.stopPeriodicSync();
      queue.dispose();
      apiClient.close();
    });

    test('18. Cycle Resolution: Absence of auth token prevents network requests, preserves pending items, and signals awaitingAuthorization', () async {
      int httpCalls = 0;
      final mockClient = MockClient((request) async {
        httpCalls++;
        return http.Response('Unexpected request should not be made', 500);
      });

      final apiClient = AegisApiClient(
        baseUrl: 'http://127.0.0.1:8000',
        tokenProvider: () async => null,
        client: mockClient,
      );

      final queue = VitalsSyncQueue(apiClient: apiClient);
      await queue.init();

      await queue.enqueue(HealthMeasurement(
        timestamp: DateTime.now(),
        pulse: 72,
        source: HealthDataSource.watchSensor,
      ));
      await queue.enqueue(HealthMeasurement(
        timestamp: DateTime.now(),
        pulse: 74,
        source: HealthDataSource.watchSensor,
      ));

      expect(queue.pendingCount, equals(2));

      // Resolve cycle without auth token: MUST NOT send any network requests
      final resolved = await queue.resolveActiveCycle();
      expect(resolved, isFalse);
      expect(httpCalls, equals(0), reason: 'Zero network requests should be executed when unauthenticated');
      expect(queue.cycleStatus, equals(CycleResolutionStatus.awaitingAuthorization));
      expect(queue.isAwaitingAuthorization, isTrue);
      expect(queue.awaitingAuthorization, isTrue);
      expect(queue.isPaused, isTrue);
      expect(queue.pauseReason, contains('Awaiting authorization'));
      expect(queue.activeCycleId, isNull);
      expect(queue.pendingCount, equals(2));

      // Flush without auth token: skips network calls and safely pauses
      final flushResult = await queue.flush();
      expect(flushResult.status, equals(SyncStatus.paused));
      expect(httpCalls, equals(0));
      expect(queue.pendingCount, equals(2));

      queue.dispose();
      apiClient.close();
    });

    test('19. Cycle Resolution: Authorization appearing later resolves real cycle_id, binds unresolved items, preserves other cycles, and resumes sync', () async {
      String? dynamicToken;
      int cycleRequests = 0;
      int batchRequests = 0;

      final mockClient = MockClient((request) async {
        if (request.url.path == '/api/v1/cycle/current') {
          cycleRequests++;
          expect(request.headers['Authorization'], equals('Bearer valid_jwt_token'));
          return http.Response(json.encode({
            'cycle_id': 'cycle_real_456',
            'patient_id': 'patient_01',
            'status': 'active',
            'started_at': DateTime.now().toUtc().toIso8601String(),
          }), 200);
        }
        if (request.url.path == '/api/v1/vitals/batch') {
          batchRequests++;
          return http.Response(json.encode({
            'status': 'success',
            'cycle_id': 'cycle_real_456',
            'recorded_count': 1,
            'deduplicated_count': 0,
            'batch_size': 1,
            'received_at': DateTime.now().toUtc().toIso8601String(),
          }), 200);
        }
        return http.Response('Not Found', 404);
      });

      final apiClient = AegisApiClient(
        baseUrl: 'http://127.0.0.1:8000',
        tokenProvider: () async => dynamicToken,
        client: mockClient,
      );

      final queue = VitalsSyncQueue(apiClient: apiClient);
      await queue.init();

      // 1. Enqueue item 1 under an explicit prior cycle (e.g. from an earlier clinical episode)
      await queue.setActiveCycleId('cycle_prior_111');
      await queue.enqueue(HealthMeasurement(
        timestamp: DateTime.utc(2026, 10, 23, 9, 0, 0),
        pulse: 65,
        source: HealthDataSource.watchSensor,
      ));
      expect(queue.pendingItems[0].cycleId, equals('cycle_prior_111'));

      // Active cycle becomes unknown (e.g. user logs out or cycle transitions)
      await queue.setActiveCycleId(null);
      expect(queue.activeCycleId, isNull);

      // 2. Enqueue item 2 while active cycle is unresolved
      await queue.enqueue(HealthMeasurement(
        timestamp: DateTime.utc(2026, 10, 23, 10, 0, 0),
        pulse: 82,
        source: HealthDataSource.watchSensor,
      ));
      expect(queue.pendingItems[1].cycleId, isNull);
      expect(queue.pendingItems[1].idempotencyKey, isNull);

      // While dynamicToken is null, resolution does not perform network calls
      expect(await queue.resolveActiveCycle(), isFalse);
      expect(cycleRequests, equals(0));

      // 3. Authorization token is provided later
      dynamicToken = 'valid_jwt_token';

      // 4. Resolve cycle with active token
      final resolveSuccess = await queue.resolveActiveCycle();
      expect(resolveSuccess, isTrue);
      expect(cycleRequests, equals(1));
      expect(queue.activeCycleId, equals('cycle_real_456'));
      expect(queue.cycleStatus, equals(CycleResolutionStatus.active));
      expect(queue.isPaused, isFalse);

      // Invariant check:
      // Item 1 bound to cycle_prior_111 MUST NOT be altered
      expect(queue.pendingItems[0].cycleId, equals('cycle_prior_111'));
      expect(queue.pendingItems[0].idempotencyKey, startsWith('idem_cycle_prior_111_'));

      // Item 2 previously unresolved MUST now be bound to cycle_real_456
      expect(queue.pendingItems[1].cycleId, equals('cycle_real_456'));
      expect(queue.pendingItems[1].idempotencyKey, startsWith('idem_cycle_real_456_'));

      // 5. Synchronization proceeds seamlessly
      final flushResult = await queue.flush();
      expect(flushResult.status, equals(SyncStatus.success));
      expect(batchRequests, equals(1));

      queue.dispose();
      apiClient.close();
    });

    test('20. Cycle Resolution: 401 Unauthorized, 404 Cycle Not Found, and Network Errors maintain distinct diagnostics and zero data loss', () async {
      int testScenario = 1; // 1: 401, 2: 404, 3: network error

      final mockClient = MockClient((request) async {
        if (request.url.path == '/api/v1/cycle/current') {
          if (testScenario == 1) {
            return http.Response(json.encode({'detail': 'Token signature has expired'}), 401);
          } else if (testScenario == 2) {
            return http.Response(json.encode({'detail': 'No active cycle currently'}), 404);
          } else {
            throw http.ClientException('Socket reset / Connection refused');
          }
        }
        return http.Response('Not Found', 404);
      });

      final apiClient = AegisApiClient(
        baseUrl: 'http://127.0.0.1:8000',
        tokenProvider: () async => 'simulated_test_token',
        client: mockClient,
      );

      final queue = VitalsSyncQueue(apiClient: apiClient);
      await queue.init();

      await queue.enqueue(HealthMeasurement(
        timestamp: DateTime.now(),
        pulse: 77,
        source: HealthDataSource.watchSensor,
      ));

      // Scenario 1: 401 Unauthorized
      testScenario = 1;
      expect(await queue.resolveActiveCycle(force: true), isFalse);
      expect(queue.cycleStatus, equals(CycleResolutionStatus.authError));
      expect(queue.isPaused, isTrue);
      expect(queue.pauseReason, contains('401 Unauthorized'));
      expect(queue.pendingCount, equals(1));

      // Scenario 2: 404 Cycle Not Found
      testScenario = 2;
      queue.resume();
      expect(await queue.resolveActiveCycle(force: true), isFalse);
      expect(queue.cycleStatus, equals(CycleResolutionStatus.noActiveCycle));
      expect(queue.isPaused, isTrue);
      expect(queue.pauseReason, contains('404 Cycle Not Found'));
      expect(queue.pendingCount, equals(1));

      // Scenario 3: Network error
      testScenario = 3;
      queue.resume();
      expect(await queue.resolveActiveCycle(force: true), isFalse);
      expect(queue.cycleStatus, equals(CycleResolutionStatus.networkError));
      expect(queue.lastSyncError, contains('Network error'));
      expect(queue.pendingCount, equals(1));

      queue.dispose();
      apiClient.close();
    });

    test('21. Cycle Resolution: HealthDataProvider diagnostics and updateApiClient cycle propagation', () async {
      final timeService = TimeService();
      final vaultService = VaultService();
      await vaultService.initFuture;
      final vitalsService = VitalsMonitorService(timeService, vaultService);

      final unauthedMock = MockClient((request) async {
        return http.Response('Should not be called', 500);
      });
      final unauthedClient = AegisApiClient(
        baseUrl: 'http://127.0.0.1:8000',
        tokenProvider: standardRuntimeTokenProvider,
        client: unauthedMock,
      );

      final syncQueue = VitalsSyncQueue(apiClient: unauthedClient);
      await syncQueue.init();

      final healthProvider = HealthDataProvider(
        vaultService: vaultService,
        vitalsMonitorService: vitalsService,
        syncQueue: syncQueue,
      );

      // Trigger cycle check while unauthenticated
      await syncQueue.resolveActiveCycle();

      expect(healthProvider.isAwaitingCycleAuthorization, isTrue);
      expect(healthProvider.cycleStatus, equals(CycleResolutionStatus.awaitingAuthorization));
      expect(healthProvider.activeCycleId, isNull);

      // Ingest live measurement
      await healthProvider.dispatchWatchSensor(pulse: 73);
      expect(healthProvider.syncPendingCount, equals(1));
      expect(syncQueue.pendingItems.first.cycleId, isNull);

      // User logs in: updateApiClient with authenticated client
      final authedMock = MockClient((request) async {
        if (request.url.path == '/api/v1/cycle/current') {
          return http.Response(json.encode({
            'cycle_id': 'cycle_update_999',
            'patient_id': 'patient_01',
            'status': 'active',
            'started_at': DateTime.now().toUtc().toIso8601String(),
          }), 200);
        }
        return http.Response('ok', 200);
      });

      final authedClient = AegisApiClient(
        baseUrl: 'http://127.0.0.1:8000',
        tokenProvider: () async => 'valid_user_jwt',
        client: authedMock,
      );

      syncQueue.updateApiClient(authedClient);

      // Allow background resolution from updateApiClient to finish
      await syncQueue.resolveActiveCycle();

      expect(healthProvider.isAwaitingCycleAuthorization, isFalse);
      expect(healthProvider.cycleStatus, equals(CycleResolutionStatus.active));
      expect(healthProvider.activeCycleId, equals('cycle_update_999'));
      expect(syncQueue.pendingItems.first.cycleId, equals('cycle_update_999'));

      healthProvider.dispose();
      syncQueue.dispose();
      vitalsService.dispose();
      vaultService.dispose();
      unauthedClient.close();
      authedClient.close();
    });


    test('22. Session / Cycle Isolation: Restored activeCycleId without authorization remains unconfirmed and blocks outbound transmission without network traffic', () async {
      int networkCalls = 0;
      final mockClient = MockClient((request) async {
        networkCalls++;
        return http.Response('Unexpected network call', 500);
      });

      // Prepare snapshot with a previously saved cycle ID and pending measurements
      final storedSnapshot = {
        'version': 2,
        'saved_at': DateTime.utc(2026, 10, 23, 12, 0, 0).toIso8601String(),
        'active_cycle_id': 'cycle_patient_A',
        'event_counter': 5,
        'pending': [
          {
            'event_id': 'evt_101',
            'idempotency_key': 'idem_cycle_patient_A_evt_101',
            'cycle_id': 'cycle_patient_A',
            'dto': {
              'cycle_id': 'cycle_patient_A',
              'measured_at': DateTime.utc(2026, 10, 23, 12, 0, 0).toIso8601String(),
              'source': 'watch_sensor',
              'pulse': 72,
              'is_simulated': false,
              'is_historical': false,
              'phase': 'standby',
            },
            'enqueued_at': DateTime.utc(2026, 10, 23, 12, 0, 0).toIso8601String(),
            'retry_count': 0,
            'is_quarantined': false,
          }
        ],
        'quarantine': [],
      };

      SharedPreferences.setMockInitialValues({
        'aegis_vitals_sync_snapshot_v2': json.encode(storedSnapshot),
      });

      // Client with standard runtime unauthenticated provider (token is null)
      final apiClient = AegisApiClient(
        baseUrl: 'http://127.0.0.1:8000',
        tokenProvider: standardRuntimeTokenProvider,
        client: mockClient,
      );

      final queue = VitalsSyncQueue(apiClient: apiClient);
      await queue.init();

      // Cycle was restored, but is strictly UNCONFIRMED for this session
      expect(queue.activeCycleId, equals('cycle_patient_A'));
      expect(queue.isCycleConfirmedByServer, isFalse);
      expect(queue.pendingCount, equals(1));

      // Attempting cycle resolution without auth token: skips network and sets awaitingAuthorization
      final resolveResult = await queue.resolveActiveCycle();
      expect(resolveResult, isFalse);
      expect(networkCalls, equals(0));
      expect(queue.isAwaitingAuthorization, isTrue);
      expect(queue.cycleStatus, equals(CycleResolutionStatus.awaitingAuthorization));
      expect(queue.isPaused, isTrue);

      // Attempting flush without server confirmation and without auth: blocks transmission
      final flushResult = await queue.flush();
      expect(flushResult.status, equals(SyncStatus.paused));
      expect(networkCalls, equals(0)); // Zero network requests sent!

      // Item remains intact in queue without loss
      expect(queue.pendingCount, equals(1));
      expect(queue.pendingItems.first.cycleId, equals('cycle_patient_A'));

      queue.dispose();
      apiClient.close();
    });

    test('23. Session / Cycle Isolation: Changing client session invalidates cycle confirmation, requires server re-validation, and prevents cross-session transmission', () async {
      String? currentToken = 'token_patient_1';
      String activeServerCycle = 'cycle_patient_1';
      final sentBatchCycles = <String>[];

      final mockClient = MockClient((request) async {
        if (request.url.path == '/api/v1/cycle/current') {
          return http.Response(json.encode({
            'cycle_id': activeServerCycle,
            'patient_id': 'patient_id_x',
            'status': 'active',
            'started_at': DateTime.now().toUtc().toIso8601String(),
          }), 200);
        }
        if (request.url.path == '/api/v1/vitals/batch') {
          final decoded = json.decode(request.body) as Map<String, dynamic>;
          sentBatchCycles.add(decoded['cycle_id'] as String);
          return http.Response(json.encode({
            'status': 'success',
            'cycle_id': decoded['cycle_id'],
            'recorded_count': 1,
            'deduplicated_count': 0,
            'batch_size': 1,
            'received_at': DateTime.now().toUtc().toIso8601String(),
          }), 200);
        }
        return http.Response('Not Found', 404);
      });

      final client1 = AegisApiClient(
        baseUrl: 'http://127.0.0.1:8000',
        tokenProvider: () async => currentToken,
        client: mockClient,
      );

      final queue = VitalsSyncQueue(apiClient: client1);
      await queue.init();

      // Confirm cycle for Patient 1
      final p1Confirmed = await queue.resolveActiveCycle();
      expect(p1Confirmed, isTrue);
      expect(queue.activeCycleId, equals('cycle_patient_1'));
      expect(queue.isCycleConfirmedByServer, isTrue);

      // Ingest measurement for Patient 1
      await queue.enqueue(HealthMeasurement(
        timestamp: DateTime.now(),
        pulse: 72,
        source: HealthDataSource.watchSensor,
      ));
      expect(queue.pendingItems.first.cycleId, equals('cycle_patient_1'));

      // Now switch session to Patient 2
      currentToken = 'token_patient_2';
      activeServerCycle = 'cycle_patient_2';

      final client2 = AegisApiClient(
        baseUrl: 'http://127.0.0.1:8000',
        tokenProvider: () async => currentToken,
        client: mockClient,
      );

      // Updating client invalidates previous confirmation and resets active cycle
      queue.updateApiClient(client2);

      // Ingest unassigned measurement while switching session (prior to new cycle confirmation)
      await queue.enqueue(HealthMeasurement(
        timestamp: DateTime.now(),
        pulse: 88,
        source: HealthDataSource.watchSensor,
      ));
      expect(queue.pendingCount, equals(2));
      expect(queue.pendingItems[1].cycleId, isNull);

      await queue.resolveActiveCycle(force: true);

      expect(queue.activeCycleId, equals('cycle_patient_2'));
      expect(queue.isCycleConfirmedByServer, isTrue);

      // Item from Patient 1 is NOT altered or re-bound
      expect(queue.pendingItems[0].cycleId, equals('cycle_patient_1'));
      // Unassigned item is bound to Patient 2
      expect(queue.pendingItems[1].cycleId, equals('cycle_patient_2'));

      // Flush under Patient 2 session transmits ONLY Patient 2 telemetry!
      final flushResult = await queue.flush();
      expect(flushResult.status, equals(SyncStatus.success));

      // Verify that cycle_patient_1 was NEVER sent to Patient 2 session!
      expect(sentBatchCycles, contains('cycle_patient_2'));
      expect(sentBatchCycles, isNot(contains('cycle_patient_1')));

      // Patient 1 telemetry remains safely preserved in pendingItems
      expect(queue.pendingCount, equals(1));
      expect(queue.pendingItems.first.cycleId, equals('cycle_patient_1'));

      queue.dispose();
      client1.close();
      client2.close();
    });

    test('24. Session / Cycle Isolation: Server returning different active cycle preserves alien events and binds only unassigned telemetry', () async {
      final mockClient = MockClient((request) async {
        if (request.url.path == '/api/v1/cycle/current') {
          return http.Response(json.encode({
            'cycle_id': 'cycle_patient_Z',
            'patient_id': 'patient_Z',
            'status': 'active',
            'started_at': DateTime.now().toUtc().toIso8601String(),
          }), 200);
        }
        if (request.url.path == '/api/v1/vitals/batch') {
          final decoded = json.decode(request.body) as Map<String, dynamic>;
          expect(decoded['cycle_id'], equals('cycle_patient_Z'));
          return http.Response(json.encode({
            'status': 'success',
            'cycle_id': 'cycle_patient_Z',
            'recorded_count': 1,
            'deduplicated_count': 0,
            'batch_size': 1,
            'received_at': DateTime.now().toUtc().toIso8601String(),
          }), 200);
        }
        return http.Response('Not Found', 404);
      });

      final apiClient = AegisApiClient(
        baseUrl: 'http://127.0.0.1:8000',
        tokenProvider: () async => 'token_patient_Z',
        client: mockClient,
      );

      final queue = VitalsSyncQueue(apiClient: apiClient);
      await queue.init();

      // Enqueue Item 1 explicitly for cycle X
      await queue.setActiveCycleId('cycle_patient_X');
      await queue.enqueue(HealthMeasurement(
        timestamp: DateTime.now(),
        pulse: 60,
        source: HealthDataSource.watchSensor,
      ));

      // Enqueue Item 2 explicitly for cycle Y
      await queue.setActiveCycleId('cycle_patient_Y');
      await queue.enqueue(HealthMeasurement(
        timestamp: DateTime.now(),
        pulse: 70,
        source: HealthDataSource.watchSensor,
      ));

      // Enqueue Item 3 while cycle is unset
      await queue.setActiveCycleId(null);
      await queue.enqueue(HealthMeasurement(
        timestamp: DateTime.now(),
        pulse: 80,
        source: HealthDataSource.watchSensor,
      ));

      expect(queue.pendingCount, equals(3));
      expect(queue.pendingItems[0].cycleId, equals('cycle_patient_X'));
      expect(queue.pendingItems[1].cycleId, equals('cycle_patient_Y'));
      expect(queue.pendingItems[2].cycleId, isNull);

      // Now resolve active cycle for current session (server returns cycle_patient_Z)
      final resolved = await queue.resolveActiveCycle(force: true);
      expect(resolved, isTrue);
      expect(queue.activeCycleId, equals('cycle_patient_Z'));

      // Alien cycle items are NEVER re-bound
      expect(queue.pendingItems[0].cycleId, equals('cycle_patient_X'));
      expect(queue.pendingItems[1].cycleId, equals('cycle_patient_Y'));
      // Unassigned item is bound to cycle_patient_Z
      expect(queue.pendingItems[2].cycleId, equals('cycle_patient_Z'));

      // Flush transmits ONLY item 3 (cycle_patient_Z)
      final flushResult = await queue.flush();
      expect(flushResult.status, equals(SyncStatus.success));

      // Items 1 and 2 remain safely preserved
      expect(queue.pendingCount, equals(2));
      expect(queue.pendingItems[0].cycleId, equals('cycle_patient_X'));
      expect(queue.pendingItems[1].cycleId, equals('cycle_patient_Y'));

      queue.dispose();
      apiClient.close();
    });

    test('25. Stale Session Response: Belated response from previous client A is discarded, and current session B applies only valid cycle', () async {
      final completerA = Completer<http.Response>();
      int cycleCallsA = 0;
      int cycleCallsB = 0;
      final sentBatchCycles = <String>[];

      final mockClientA = MockClient((request) async {
        if (request.url.path == '/api/v1/cycle/current') {
          cycleCallsA++;
          return completerA.future;
        }
        return http.Response('Not Found', 404);
      });

      final mockClientB = MockClient((request) async {
        if (request.url.path == '/api/v1/cycle/current') {
          cycleCallsB++;
          return http.Response(json.encode({
            'cycle_id': 'cycle_patient_B',
            'status': 'active',
            'started_at': DateTime.now().toUtc().toIso8601String(),
          }), 200);
        }
        if (request.url.path == '/api/v1/vitals/batch') {
          final decoded = json.decode(request.body) as Map<String, dynamic>;
          sentBatchCycles.add(decoded['cycle_id'] as String);
          return http.Response(json.encode({
            'status': 'success',
            'cycle_id': decoded['cycle_id'],
            'recorded_count': 1,
            'deduplicated_count': 0,
            'batch_size': 1,
            'received_at': DateTime.now().toUtc().toIso8601String(),
          }), 200);
        }
        return http.Response('Not Found', 404);
      });

      final clientA = AegisApiClient(
        baseUrl: 'http://127.0.0.1:8000',
        tokenProvider: () async => 'token_patient_A',
        client: mockClientA,
      );

      final clientB = AegisApiClient(
        baseUrl: 'http://127.0.0.1:8000',
        tokenProvider: () async => 'token_patient_B',
        client: mockClientB,
      );

      final queue = VitalsSyncQueue(apiClient: clientA);
      await queue.init();

      // Enqueue item 1 explicitly bound to prior cycle
      await queue.setActiveCycleId('cycle_patient_prior');
      await queue.enqueue(HealthMeasurement(
        timestamp: DateTime.now(),
        pulse: 71,
        source: HealthDataSource.watchSensor,
      ));
      expect(queue.pendingItems[0].cycleId, equals('cycle_patient_prior'));

      // Enqueue item 2 unassigned (cycleId is null)
      await queue.setActiveCycleId(null);
      await queue.enqueue(HealthMeasurement(
        timestamp: DateTime.now(),
        pulse: 82,
        source: HealthDataSource.watchSensor,
      ));
      expect(queue.pendingItems[1].cycleId, isNull);

      // Client A begins asynchronous cycle resolution (delayed by completer)
      final resolveFutureA = queue.resolveActiveCycle(force: true);
      await Future<void>.delayed(const Duration(milliseconds: 20));
      expect(cycleCallsA, equals(1));
      expect(queue.sessionGeneration, equals(0));

      // Client switch to Client B occurs BEFORE Client A responds!
      // updateApiClient automatically initiates cycle resolution for Client B
      queue.updateApiClient(clientB);
      expect(queue.sessionGeneration, equals(1));

      // Allow Client B's resolution to complete
      await Future<void>.delayed(const Duration(milliseconds: 30));
      expect(cycleCallsB, greaterThanOrEqualTo(1));
      expect(queue.activeCycleId, equals('cycle_patient_B'));
      expect(queue.isCycleConfirmedByServer, isTrue);

      // Now Client A belatedly responds with cycle_patient_A
      completerA.complete(http.Response(json.encode({
        'cycle_id': 'cycle_patient_A',
        'status': 'active',
        'started_at': DateTime.now().toUtc().toIso8601String(),
      }), 200));

      final resultA = await resolveFutureA;
      // Stale response A was safely discarded!
      expect(resultA, isFalse);
      expect(queue.activeCycleId, equals('cycle_patient_B'));
      expect(queue.isCycleConfirmedByServer, isTrue);

      // Prior item was NEVER rebound to Client A or B
      expect(queue.pendingItems[0].cycleId, equals('cycle_patient_prior'));
      // Unassigned item was bound to active session B (never A!)
      expect(queue.pendingItems[1].cycleId, equals('cycle_patient_B'));

      // Flush transmits ONLY item for cycle_patient_B
      final flushRes = await queue.flush();
      expect(flushRes.status, equals(SyncStatus.success));
      expect(sentBatchCycles, contains('cycle_patient_B'));
      expect(sentBatchCycles, isNot(contains('cycle_patient_A')));
      expect(sentBatchCycles, isNot(contains('cycle_patient_prior')));

      // Item from prior cycle remains safely preserved
      expect(queue.pendingCount, equals(1));
      expect(queue.pendingItems.first.cycleId, equals('cycle_patient_prior'));

      queue.dispose();
      clientA.close();
      clientB.close();
    });

    test('26. Confirmation Persistence Rollback: Storage write failure during cycle confirmation rolls back in-memory confirmation, preserves items, and blocks outbound flush', () async {
      final initialSnapshot = {
        'version': 2,
        'saved_at': DateTime.utc(2026, 10, 23, 10, 0, 0).toIso8601String(),
        'active_cycle_id': 'cycle_prior_session',
        'event_counter': 1,
        'pending': [
          {
            'event_id': 'evt_prior_001',
            'idempotency_key': 'idem_cycle_prior_session_evt_prior_001',
            'cycle_id': 'cycle_prior_session',
            'dto': {
              'cycle_id': 'cycle_prior_session',
              'measured_at': DateTime.utc(2026, 10, 23, 10, 0, 0).toIso8601String(),
              'source': 'watch_sensor',
              'pulse': 75,
              'is_simulated': false,
              'is_historical': false,
              'phase': 'standby',
            },
            'enqueued_at': DateTime.utc(2026, 10, 23, 10, 0, 0).toIso8601String(),
            'retry_count': 0,
            'is_quarantined': false,
          }
        ],
        'quarantine': [
          {
            'event_id': 'evt_prior_quarantine_001',
            'idempotency_key': 'idem_cycle_prior_session_evt_prior_quarantine_001',
            'cycle_id': 'cycle_prior_session',
            'dto': {
              'cycle_id': 'cycle_prior_session',
              'measured_at': DateTime.utc(2026, 10, 23, 9, 30, 0).toIso8601String(),
              'source': 'manual_input',
              'pulse': 120,
              'is_simulated': false,
              'is_historical': false,
              'phase': 'standby',
            },
            'enqueued_at': DateTime.utc(2026, 10, 23, 9, 30, 0).toIso8601String(),
            'retry_count': 1,
            'last_error': '409 Conflict',
            'is_quarantined': true,
          }
        ],
      };

      final customStore = TestSharedPreferencesStore(initialData: {
        'flutter.aegis_vitals_sync_snapshot_v2': json.encode(initialSnapshot),
      });
      SharedPreferences.resetStatic();
      SharedPreferencesStorePlatform.instance = customStore;

      int batchNetworkCalls = 0;
      final mockClient = MockClient((request) async {
        if (request.url.path == '/api/v1/cycle/current') {
          return http.Response(json.encode({
            'cycle_id': 'cycle_server_confirmed_999',
            'status': 'active',
            'started_at': DateTime.now().toUtc().toIso8601String(),
          }), 200);
        }
        if (request.url.path == '/api/v1/vitals/batch') {
          batchNetworkCalls++;
          return http.Response(json.encode({
            'status': 'success',
            'cycle_id': 'cycle_server_confirmed_999',
            'recorded_count': 1,
            'deduplicated_count': 0,
            'batch_size': 1,
            'received_at': DateTime.now().toUtc().toIso8601String(),
          }), 200);
        }
        return http.Response('Not Found', 404);
      });

      final apiClient = AegisApiClient(
        baseUrl: 'http://127.0.0.1:8000',
        tokenProvider: () async => 'token_valid',
        client: mockClient,
      );

      final queue = VitalsSyncQueue(apiClient: apiClient);
      await queue.init();

      expect(queue.pendingCount, equals(1));
      expect(queue.quarantinedCount, equals(1));
      expect(queue.isCycleConfirmedByServer, isFalse);

      // Enqueue an unassigned measurement while unconfirmed
      await queue.enqueue(HealthMeasurement(
        timestamp: DateTime.now(),
        pulse: 85,
        source: HealthDataSource.watchSensor,
      ));
      expect(queue.pendingCount, equals(2));
      expect(queue.pendingItems[1].cycleId, isNull);

      // Rig storage to fail on next write
      customStore.shouldFail = true;

      // Attempt cycle resolution which must fail at _persist() and roll back confirmation!
      final resolveResult = await queue.resolveActiveCycle(force: true);
      expect(resolveResult, isFalse);

      // Confirmation MUST NOT be true if persistence failed!
      expect(queue.isCycleConfirmedByServer, isFalse, reason: '_isCycleConfirmedByServer must be rolled back');
      // In-memory active cycle and pending items are rolled back:
      expect(queue.activeCycleId, equals('cycle_prior_session'));
      expect(queue.pendingItems[1].cycleId, isNull, reason: 'unassigned item must not remain bound to unpersisted cycle');
      // Old items in pending and quarantine remain completely preserved
      expect(queue.pendingCount, equals(2));
      expect(queue.quarantinedCount, equals(1));
      expect(queue.quarantinedItems.first.eventId, equals('evt_prior_quarantine_001'));
      expect(queue.lastSyncError, contains('Failed to persist cycle confirmation'));

      // Attempting flush when cycle is unconfirmed and persistence failed:
      final flushResult = await queue.flush();
      expect(flushResult.status, equals(SyncStatus.persistenceError));
      expect(batchNetworkCalls, equals(0), reason: 'no batch network transmission when cycle confirmation persistence failed');

      // Now repair storage
      customStore.shouldFail = false;
      customStore.successWritesRemaining = -1;

      // Retry cycle resolution: succeeds and binds cleanly
      final retryResolve = await queue.resolveActiveCycle(force: true);
      expect(retryResolve, isTrue);
      expect(queue.isCycleConfirmedByServer, isTrue);
      expect(queue.activeCycleId, equals('cycle_server_confirmed_999'));
      expect(queue.pendingItems[1].cycleId, equals('cycle_server_confirmed_999'));

      // Flush now successfully transmits ONLY cycle_server_confirmed_999
      final retryFlush = await queue.flush();
      expect(retryFlush.status, equals(SyncStatus.success));
      expect(batchNetworkCalls, equals(1));

      // Prior items from older session and quarantine are preserved intact
      expect(queue.pendingCount, equals(1));
      expect(queue.pendingItems.first.cycleId, equals('cycle_prior_session'));
      expect(queue.quarantinedCount, equals(1));

      queue.dispose();
      apiClient.close();
      SharedPreferences.resetStatic();
      SharedPreferences.setMockInitialValues({});
    });

    test('27. Delayed Persistence Session Race: Snapshot write of Client A delayed across updateApiClient(B) is discarded and sanitized', () async {
      final customStore = TestSharedPreferencesStore();
      SharedPreferences.resetStatic();
      SharedPreferencesStorePlatform.instance = customStore;

      int batchCallsA = 0;
      int batchCallsB = 0;
      final sentBatchCycles = <String>[];

      final mockClientA = MockClient((request) async {
        if (request.url.path == '/api/v1/cycle/current') {
          return http.Response(json.encode({
            'cycle_id': 'cycle_patient_A',
            'status': 'active',
            'started_at': DateTime.now().toUtc().toIso8601String(),
          }), 200);
        }
        if (request.url.path == '/api/v1/vitals/batch') {
          batchCallsA++;
          return http.Response(json.encode({
            'status': 'success',
            'cycle_id': 'cycle_patient_A',
            'recorded_count': 1,
            'deduplicated_count': 0,
            'batch_size': 1,
            'received_at': DateTime.now().toUtc().toIso8601String(),
          }), 200);
        }
        return http.Response('Not Found', 404);
      });

      final mockClientB = MockClient((request) async {
        if (request.url.path == '/api/v1/cycle/current') {
          return http.Response(json.encode({
            'cycle_id': 'cycle_patient_B',
            'status': 'active',
            'started_at': DateTime.now().toUtc().toIso8601String(),
          }), 200);
        }
        if (request.url.path == '/api/v1/vitals/batch') {
          batchCallsB++;
          final decoded = json.decode(request.body) as Map<String, dynamic>;
          sentBatchCycles.add(decoded['cycle_id'] as String);
          return http.Response(json.encode({
            'status': 'success',
            'cycle_id': decoded['cycle_id'],
            'recorded_count': 1,
            'deduplicated_count': 0,
            'batch_size': 1,
            'received_at': DateTime.now().toUtc().toIso8601String(),
          }), 200);
        }
        return http.Response('Not Found', 404);
      });

      final clientA = AegisApiClient(
        baseUrl: 'http://127.0.0.1:8000',
        tokenProvider: () async => 'token_patient_A',
        client: mockClientA,
      );

      final clientB = AegisApiClient(
        baseUrl: 'http://127.0.0.1:8000',
        tokenProvider: () async => 'token_patient_B',
        client: mockClientB,
      );

      final queue = VitalsSyncQueue(apiClient: clientA);
      await queue.init();

      // 1. Enqueue item 1 explicitly bound to Client A's prior cycle
      await queue.setActiveCycleId('cycle_patient_A_prior');
      await queue.enqueue(HealthMeasurement(
        timestamp: DateTime.now(),
        pulse: 70,
        source: HealthDataSource.watchSensor,
      ));
      expect(queue.pendingItems[0].cycleId, equals('cycle_patient_A_prior'));

      // 2. Enqueue item 2 unassigned (cycleId is null)
      await queue.setActiveCycleId(null);
      await queue.enqueue(HealthMeasurement(
        timestamp: DateTime.now(),
        pulse: 84,
        source: HealthDataSource.watchSensor,
      ));
      expect(queue.pendingCount, equals(2));
      expect(queue.pendingItems[1].cycleId, isNull);

      // 3. Client A begins confirmation of cycle_patient_A, but its snapshot persistence is artificially delayed!
      final writeCompleterA = Completer<void>();
      customStore.delayNextWrite = writeCompleterA;

      final resolveFutureA = queue.resolveActiveCycle(force: true);

      // Allow resolution to reach and suspend inside setValue (persisting snapshot)
      await Future<void>.delayed(const Duration(milliseconds: 20));

      // 4. While Client A's snapshot write is in-flight, updateApiClient(clientB) occurs!
      queue.updateApiClient(clientB);
      expect(queue.sessionGeneration, equals(1));
      expect(queue.activeCycleId, isNull);
      expect(queue.isCycleConfirmedByServer, isFalse);

      // 5. Now release Client A's delayed snapshot write
      writeCompleterA.complete();
      final resultA = await resolveFutureA;

      // 6. Verify that Client A's cycle confirmation is NOT applied to session B!
      expect(resultA, isFalse);
      expect(queue.isCycleConfirmedByServer, isFalse);
      expect(queue.activeCycleId, isNot(equals('cycle_patient_A')));

      // 7. Resolve Client B's active cycle
      final resolveFutureB = queue.resolveActiveCycle(force: true);
      final resultB = await resolveFutureB;
      expect(resultB, isTrue);
      expect(queue.activeCycleId, equals('cycle_patient_B'));
      expect(queue.isCycleConfirmedByServer, isTrue);

      // Item 2 (unassigned) is now bound to cycle_patient_B
      expect(queue.pendingItems[1].cycleId, equals('cycle_patient_B'));
      // Item 1 retains cycle_patient_A_prior
      expect(queue.pendingItems[0].cycleId, equals('cycle_patient_A_prior'));

      // 8. Flush under Session B transmits ONLY cycle_patient_B
      final flushResult = await queue.flush();
      expect(flushResult.status, equals(SyncStatus.success));
      expect(batchCallsB, equals(1));
      expect(batchCallsA, equals(0));
      expect(sentBatchCycles, contains('cycle_patient_B'));
      expect(sentBatchCycles, isNot(contains('cycle_patient_A_prior')));
      expect(sentBatchCycles, isNot(contains('cycle_patient_A')));

      // Client A's item remains preserved
      expect(queue.pendingCount, equals(1));
      expect(queue.pendingItems.first.cycleId, equals('cycle_patient_A_prior'));

      // 9. Simulate app restart: both groups of events are safely recovered from storage
      queue.dispose();

      final restoredQueue = VitalsSyncQueue(apiClient: clientB);
      await restoredQueue.init();

      expect(restoredQueue.pendingCount, equals(1));
      expect(restoredQueue.pendingItems.first.cycleId, equals('cycle_patient_A_prior'));

      restoredQueue.dispose();
      clientA.close();
      clientB.close();
      SharedPreferences.resetStatic();
      SharedPreferences.setMockInitialValues({});
    });

    test('28. Sanitization Persistence Failure: Sanitization write failure blocks sync, surfaces error, and protects session B', () async {
      final customStore = TestSharedPreferencesStore();
      SharedPreferences.resetStatic();
      SharedPreferencesStorePlatform.instance = customStore;

      int batchCallsA = 0;
      int batchCallsB = 0;
      final sentBatchCycles = <String>[];

      final mockClientA = MockClient((request) async {
        if (request.url.path == '/api/v1/cycle/current') {
          return http.Response(json.encode({
            'cycle_id': 'cycle_patient_A',
            'status': 'active',
            'started_at': DateTime.now().toUtc().toIso8601String(),
          }), 200);
        }
        if (request.url.path == '/api/v1/vitals/batch') {
          batchCallsA++;
          return http.Response(json.encode({
            'status': 'success',
            'cycle_id': 'cycle_patient_A',
            'recorded_count': 1,
            'deduplicated_count': 0,
            'batch_size': 1,
            'received_at': DateTime.now().toUtc().toIso8601String(),
          }), 200);
        }
        return http.Response('Not Found', 404);
      });

      final clientBGetCycleCompleter = Completer<void>();

      final mockClientB = MockClient((request) async {
        if (request.url.path == '/api/v1/cycle/current') {
          if (!clientBGetCycleCompleter.isCompleted) {
            await clientBGetCycleCompleter.future;
          }
          return http.Response(json.encode({
            'cycle_id': 'cycle_patient_B',
            'status': 'active',
            'started_at': DateTime.now().toUtc().toIso8601String(),
          }), 200);
        }
        if (request.url.path == '/api/v1/vitals/batch') {
          batchCallsB++;
          final decoded = json.decode(request.body) as Map<String, dynamic>;
          sentBatchCycles.add(decoded['cycle_id'] as String);
          return http.Response(json.encode({
            'status': 'success',
            'cycle_id': decoded['cycle_id'],
            'recorded_count': 1,
            'deduplicated_count': 0,
            'batch_size': 1,
            'received_at': DateTime.now().toUtc().toIso8601String(),
          }), 200);
        }
        return http.Response('Not Found', 404);
      });

      final clientA = AegisApiClient(
        baseUrl: 'http://127.0.0.1:8000',
        tokenProvider: () async => 'token_patient_A',
        client: mockClientA,
      );

      final clientB = AegisApiClient(
        baseUrl: 'http://127.0.0.1:8000',
        tokenProvider: () async => 'token_patient_B',
        client: mockClientB,
      );

      final queue = VitalsSyncQueue(apiClient: clientA);
      await queue.init();

      // 1. Enqueue item 1 explicitly bound to Client A's prior cycle
      await queue.setActiveCycleId('cycle_patient_A_prior');
      await queue.enqueue(HealthMeasurement(
        timestamp: DateTime.now(),
        pulse: 70,
        source: HealthDataSource.watchSensor,
      ));
      expect(queue.pendingItems[0].cycleId, equals('cycle_patient_A_prior'));

      // 2. Enqueue item 2 unassigned (cycleId is null)
      await queue.setActiveCycleId(null);
      await queue.enqueue(HealthMeasurement(
        timestamp: DateTime.now(),
        pulse: 84,
        source: HealthDataSource.watchSensor,
      ));
      expect(queue.pendingCount, equals(2));
      expect(queue.pendingItems[1].cycleId, isNull);

      // 3. Client A begins confirmation of cycle_patient_A, with delayed persistence:
      // Allow exactly 1 successful write (Client A's delayed write), so the subsequent
      // sanitization write will encounter disk failure!
      final writeCompleterA = Completer<void>();
      customStore.delayNextWrite = writeCompleterA;
      customStore.successWritesRemaining = 1;

      final resolveFutureA = queue.resolveActiveCycle(force: true);

      // Allow resolution to reach and suspend inside setValue (persisting snapshot)
      await Future<void>.delayed(const Duration(milliseconds: 20));

      // 4. While Client A's snapshot write is in-flight, updateApiClient(clientB) occurs!
      queue.updateApiClient(clientB);
      expect(queue.sessionGeneration, equals(1));
      expect(queue.activeCycleId, isNull);
      expect(queue.isCycleConfirmedByServer, isFalse);

      // 5. Complete Client A's delayed write:
      // Write 1 succeeds (consuming the remaining success write).
      // Client A detects session generation changed (0 != 1).
      // Client A attempts sanitization write (_persist()), which encounters successWritesRemaining == 0 and throws!
      writeCompleterA.complete();
      final resultA = await resolveFutureA;

      // 6. Verify that Client A's resolution failed and error is observable:
      expect(resultA, isFalse);
      expect(queue.isCycleConfirmedByServer, isFalse);
      expect(queue.activeCycleId, isNull);
      expect(queue.lastSyncError, isNotNull);
      expect(queue.lastSyncError, contains('sanitization failed'));
      expect(queue.isPaused, isTrue);
      expect(queue.pauseReason, contains('sanitization failed'));

      // Both pending items remain preserved in memory:
      expect(queue.pendingCount, equals(2));
      expect(queue.pendingItems[0].cycleId, equals('cycle_patient_A_prior'));
      expect(queue.pendingItems[1].cycleId, isNull);

      // 7. Outbound data transmission (flush) is strictly blocked while in error / paused state
      final blockedFlush = await queue.flush();
      expect(blockedFlush.status, isNot(equals(SyncStatus.success)));
      expect(blockedFlush.status, equals(SyncStatus.paused));
      expect(batchCallsA, equals(0));
      expect(batchCallsB, equals(0));
      expect(queue.pendingCount, equals(2));

      // 8. Storage recovers:
      customStore.successWritesRemaining = -1;

      // 9. Client B resolves its cycle: release mockClientB GET cycle completer
      clientBGetCycleCompleter.complete();
      final resolveResultB = await queue.resolveActiveCycle(force: true);
      expect(resolveResultB, isTrue);
      expect(queue.activeCycleId, equals('cycle_patient_B'));
      expect(queue.isCycleConfirmedByServer, isTrue);
      expect(queue.isPaused, isFalse);
      expect(queue.lastSyncError, isNull);

      // Item 2 (unassigned) is now bound to cycle_patient_B
      expect(queue.pendingItems[1].cycleId, equals('cycle_patient_B'));
      // Item 1 retains cycle_patient_A_prior
      expect(queue.pendingItems[0].cycleId, equals('cycle_patient_A_prior'));

      // 10. Flush under Session B transmits ONLY cycle_patient_B
      final flushResult = await queue.flush();
      expect(flushResult.status, equals(SyncStatus.success));
      expect(batchCallsB, equals(1));
      expect(batchCallsA, equals(0));
      expect(sentBatchCycles, contains('cycle_patient_B'));
      expect(sentBatchCycles, isNot(contains('cycle_patient_A_prior')));
      expect(sentBatchCycles, isNot(contains('cycle_patient_A')));

      // Client A's item remains preserved
      expect(queue.pendingCount, equals(1));
      expect(queue.pendingItems.first.cycleId, equals('cycle_patient_A_prior'));

      // 11. Simulate app restart: both groups of events are safely recovered from storage
      queue.dispose();

      final restoredQueue = VitalsSyncQueue(apiClient: clientB);
      await restoredQueue.init();

      expect(restoredQueue.pendingCount, equals(1));
      expect(restoredQueue.pendingItems.first.cycleId, equals('cycle_patient_A_prior'));

      restoredQueue.dispose();
      clientA.close();
      clientB.close();
      SharedPreferences.resetStatic();
      SharedPreferences.setMockInitialValues({});
    });
  });

  group('Stage 5.3C-3A-FIX: Patient Isolation Security & Rollback Invariants (D1 + D2)', () {
    test('D1: Rollback during session change preserves A1 in pending and keeps legacy L strictly quarantined', () async {
      final initialLegacyItem = {
        'event_id': 'evt_legacy_L',
        'idempotency_key': null,
        'cycle_id': null,
        'patient_id': null,
        'dto': {
          'cycle_id': null,
          'measured_at': DateTime.utc(2026, 10, 26, 10, 0, 0).toIso8601String(),
          'source': 'watch_sensor',
          'pulse': 70,
          'is_simulated': false,
          'is_historical': false,
          'phase': 'standby',
        },
        'enqueued_at': DateTime.utc(2026, 10, 26, 10, 0, 0).toIso8601String(),
        'retry_count': 0,
        'is_quarantined': false,
      };

      final initialPatientItem = {
        'event_id': 'evt_patient_A1',
        'idempotency_key': null,
        'cycle_id': null,
        'patient_id': 'patient_A',
        'dto': {
          'cycle_id': null,
          'measured_at': DateTime.utc(2026, 10, 26, 10, 5, 0).toIso8601String(),
          'source': 'watch_sensor',
          'pulse': 75,
          'is_simulated': false,
          'is_historical': false,
          'phase': 'standby',
        },
        'enqueued_at': DateTime.utc(2026, 10, 26, 10, 5, 0).toIso8601String(),
        'retry_count': 0,
        'is_quarantined': false,
      };

      final customStore = TestSharedPreferencesStore(initialData: {
        'flutter.aegis_vitals_sync_snapshot_v2': json.encode({
          'version': 2,
          'saved_at': DateTime.utc(2026, 10, 26, 10, 0, 0).toIso8601String(),
          'active_cycle_id': null,
          'active_patient_id': 'patient_A',
          'event_counter': 10,
          'pending': [initialLegacyItem, initialPatientItem],
          'quarantine': [],
        }),
      });
      SharedPreferences.resetStatic();
      SharedPreferencesStorePlatform.instance = customStore;

      final mockClientA = MockClient((request) async {
        if (request.url.path == '/api/v1/cycle/current') {
          return http.Response(json.encode({
            'cycle_id': 'cycle_patient_A',
            'patient_id': 'patient_A',
            'status': 'active',
            'started_at': DateTime.now().toUtc().toIso8601String(),
          }), 200);
        }
        return http.Response('Not Found', 404);
      });

      final clientA = AegisApiClient(
        baseUrl: 'http://127.0.0.1:8000',
        tokenProvider: () async => _buildTestJwt(patientId: 'patient_A'),
        client: mockClientA,
      );

      final clientB = AegisApiClient(
        baseUrl: 'http://127.0.0.1:8000',
        tokenProvider: () async => _buildTestJwt(patientId: 'patient_B'),
        client: MockClient((r) async => http.Response('Not Found', 404)),
      );

      final queue = VitalsSyncQueue(apiClient: clientA, activePatientId: 'patient_A');
      await queue.init();

      // On init with patient isolation, legacy L was quarantined
      expect(queue.quarantinedCount, equals(1));
      expect(queue.quarantinedItems.first.eventId, equals('evt_legacy_L'));
      expect(queue.pendingCount, equals(1));
      expect(queue.pendingItems.first.eventId, equals('evt_patient_A1'));

      // Now initiate cycle resolution for Patient A, but delay the snapshot persist!
      final writeCompleter = Completer<void>();
      customStore.delayNextWrite = writeCompleter;

      final resolveFuture = queue.resolveActiveCycle(force: true);
      await Future<void>.delayed(const Duration(milliseconds: 20));

      // While persist is paused, patient session switches to Client B!
      queue.updateApiClient(clientB);
      expect(queue.sessionGeneration, equals(1));

      // Complete delayed write
      writeCompleter.complete();
      final resultA = await resolveFuture;
      expect(resultA, isFalse); // Stale resolution discarded

      // CRITICAL INVARIANT: A1 must remain in pending exactly once!
      expect(queue.pendingCount, equals(1));
      expect(queue.pendingItems.first.eventId, equals('evt_patient_A1'));
      expect(queue.pendingItems.first.cycleId, isNull); // cycle binding rolled back

      // L must remain strictly in quarantine only, never duplicated back to pending!
      expect(queue.quarantinedCount, equals(1));
      expect(queue.quarantinedItems.first.eventId, equals('evt_legacy_L'));
      expect(queue.pendingItems.any((i) => i.eventId == 'evt_legacy_L'), isFalse);

      // Now verify persistence across restart
      queue.dispose();
      final restoredQueue = VitalsSyncQueue(apiClient: clientB);
      await restoredQueue.init();

      expect(restoredQueue.pendingCount, equals(1));
      expect(restoredQueue.pendingItems.first.eventId, equals('evt_patient_A1'));
      expect(restoredQueue.quarantinedCount, equals(1));
      expect(restoredQueue.quarantinedItems.first.eventId, equals('evt_legacy_L'));

      restoredQueue.dispose();
      clientA.close();
      clientB.close();
      SharedPreferences.resetStatic();
      SharedPreferences.setMockInitialValues({});
    });

    test('D2-A: Non-JWT token is rejected and pauses queue without HTTP dispatch', () async {
      int httpCalls = 0;
      final mockClient = MockClient((request) async {
        httpCalls++;
        return http.Response(json.encode({'status': 'success'}), 200);
      });

      final apiClient = AegisApiClient(
        baseUrl: 'http://127.0.0.1:8000',
        tokenProvider: () async => 'not_a_valid_jwt_token_string',
        client: mockClient,
      );

      final queue = VitalsSyncQueue(
        apiClient: apiClient,
        activeCycleId: 'cycle_test_01',
        activePatientId: 'patient_A',
      );
      await queue.init();

      await queue.enqueue(HealthMeasurement(
        timestamp: DateTime.now(),
        pulse: 72,
        source: HealthDataSource.watchSensor,
      ));

      final result = await queue.flush();
      expect(result.status, equals(SyncStatus.paused));
      expect(queue.isPaused, isTrue);
      expect(queue.lastSyncError, contains('valid mobile JWT with patient sub claim is required'));
      expect(httpCalls, equals(0)); // 0 HTTP requests dispatched
      queue.dispose();
      apiClient.close();
    });

    test('D2-B: JWT without sub claim is rejected and pauses queue without HTTP dispatch', () async {
      int httpCalls = 0;
      final mockClient = MockClient((request) async {
        httpCalls++;
        return http.Response(json.encode({'status': 'success'}), 200);
      });

      // Build JWT with missing sub claim
      final headerJson = json.encode({'alg': 'HS256', 'typ': 'JWT'});
      final payloadJson = json.encode({'did': 'device_01', 'type': 'access'});
      final h = base64Url.encode(utf8.encode(headerJson)).replaceAll('=', '');
      final p = base64Url.encode(utf8.encode(payloadJson)).replaceAll('=', '');
      final jwtWithoutSub = '$h.$p.fakesig';

      final apiClient = AegisApiClient(
        baseUrl: 'http://127.0.0.1:8000',
        tokenProvider: () async => jwtWithoutSub,
        client: mockClient,
      );

      final queue = VitalsSyncQueue(
        apiClient: apiClient,
        activeCycleId: 'cycle_test_02',
        activePatientId: 'patient_A',
      );
      await queue.init();

      await queue.enqueue(HealthMeasurement(
        timestamp: DateTime.now(),
        pulse: 74,
        source: HealthDataSource.watchSensor,
      ));

      final result = await queue.flush();
      expect(result.status, equals(SyncStatus.paused));
      expect(queue.isPaused, isTrue);
      expect(queue.lastSyncError, contains('valid mobile JWT with patient sub claim is required'));
      expect(httpCalls, equals(0)); // 0 HTTP requests dispatched
      queue.dispose();
      apiClient.close();
    });

    test('D2-C: activePatientId=A with JWT sub=B pauses queue without HTTP dispatch', () async {
      int httpCalls = 0;
      final mockClient = MockClient((request) async {
        httpCalls++;
        return http.Response(json.encode({'status': 'success'}), 200);
      });

      final apiClient = AegisApiClient(
        baseUrl: 'http://127.0.0.1:8000',
        tokenProvider: () async => _buildTestJwt(patientId: 'patient_B'),
        client: mockClient,
      );

      final queue = VitalsSyncQueue(
        apiClient: apiClient,
        activeCycleId: 'cycle_test_03',
        activePatientId: 'patient_A',
      );
      await queue.init();

      await queue.enqueue(HealthMeasurement(
        timestamp: DateTime.now(),
        pulse: 76,
        source: HealthDataSource.watchSensor,
      ));

      final result = await queue.flush();
      expect(result.status, equals(SyncStatus.paused));
      expect(queue.isPaused, isTrue);
      expect(queue.lastSyncError, contains('Patient isolation mismatch'));
      expect(httpCalls, equals(0)); // 0 HTTP requests dispatched
      queue.dispose();
      apiClient.close();
    });

    test('D2-D: Correct JWT sub=A permits dispatch of only patient A measurements', () async {
      int batchCalls = 0;
      String? sentAuthHeader;
      List<dynamic> sentMeasurements = [];

      final mockClient = MockClient((request) async {
        if (request.url.path == '/api/v1/vitals/batch') {
          batchCalls++;
          sentAuthHeader = request.headers['Authorization'];
          final body = json.decode(request.body) as Map<String, dynamic>;
          sentMeasurements = body['measurements'] as List<dynamic>;
          return http.Response(json.encode({
            'status': 'success',
            'cycle_id': 'cycle_A',
            'recorded_count': sentMeasurements.length,
            'deduplicated_count': 0,
            'batch_size': sentMeasurements.length,
            'received_at': DateTime.now().toUtc().toIso8601String(),
          }), 200);
        }
        return http.Response('Not Found', 404);
      });

      final expectedJwt = _buildTestJwt(patientId: 'patient_A');
      final apiClient = AegisApiClient(
        baseUrl: 'http://127.0.0.1:8000',
        tokenProvider: () async => expectedJwt,
        client: mockClient,
      );

      final queue = VitalsSyncQueue(
        apiClient: apiClient,
        activeCycleId: 'cycle_A',
        activePatientId: 'patient_A',
      );
      await queue.init();

      await queue.enqueue(HealthMeasurement(
        timestamp: DateTime.now(),
        pulse: 77,
        source: HealthDataSource.watchSensor,
      ));

      final result = await queue.flush();
      expect(result.status, equals(SyncStatus.success));
      expect(batchCalls, equals(1));
      expect(sentAuthHeader, equals('Bearer $expectedJwt'));
      expect(sentMeasurements.length, equals(1));
      expect(queue.pendingCount, equals(0));
      queue.dispose();
      apiClient.close();
    });

    test('D2-E: Legacy items without patientId are quarantined and never transmitted', () async {
      int batchCalls = 0;
      final mockClient = MockClient((request) async {
        if (request.url.path == '/api/v1/vitals/batch') {
          batchCalls++;
          return http.Response(json.encode({
            'status': 'success',
            'cycle_id': 'cycle_A',
            'recorded_count': 1,
            'deduplicated_count': 0,
            'batch_size': 1,
            'received_at': DateTime.now().toUtc().toIso8601String(),
          }), 200);
        }
        return http.Response('Not Found', 404);
      });

      final customStore = TestSharedPreferencesStore(initialData: {
        'flutter.aegis_vitals_sync_snapshot_v2': json.encode({
          'version': 2,
          'saved_at': DateTime.utc(2026, 10, 26, 10, 0, 0).toIso8601String(),
          'active_cycle_id': 'cycle_A',
          'active_patient_id': 'patient_A',
          'event_counter': 10,
          'pending': [
            {
              'event_id': 'legacy_evt_99',
              'idempotency_key': 'idem_cycle_A_legacy_evt_99',
              'cycle_id': 'cycle_A',
              'patient_id': null,
              'dto': {
                'cycle_id': 'cycle_A',
                'measured_at': DateTime.utc(2026, 10, 26, 10, 0, 0).toIso8601String(),
                'source': 'watch_sensor',
                'pulse': 80,
                'is_simulated': false,
                'is_historical': false,
                'phase': 'standby',
              },
              'enqueued_at': DateTime.utc(2026, 10, 26, 10, 0, 0).toIso8601String(),
              'retry_count': 0,
              'is_quarantined': false,
            },
            {
              'event_id': 'patient_A_evt_01',
              'idempotency_key': 'idem_cycle_A_patient_A_evt_01',
              'cycle_id': 'cycle_A',
              'patient_id': 'patient_A',
              'dto': {
                'cycle_id': 'cycle_A',
                'measured_at': DateTime.utc(2026, 10, 26, 10, 5, 0).toIso8601String(),
                'source': 'watch_sensor',
                'pulse': 82,
                'is_simulated': false,
                'is_historical': false,
                'phase': 'standby',
              },
              'enqueued_at': DateTime.utc(2026, 10, 26, 10, 5, 0).toIso8601String(),
              'retry_count': 0,
              'is_quarantined': false,
            },
          ],
          'quarantine': [],
        }),
      });
      SharedPreferences.resetStatic();
      SharedPreferencesStorePlatform.instance = customStore;

      final apiClient = AegisApiClient(
        baseUrl: 'http://127.0.0.1:8000',
        tokenProvider: () async => _buildTestJwt(patientId: 'patient_A'),
        client: mockClient,
      );

      final queue = VitalsSyncQueue(
        apiClient: apiClient,
        activeCycleId: 'cycle_A',
        activePatientId: 'patient_A',
      );
      await queue.init();

      expect(queue.quarantinedCount, equals(1));
      expect(queue.quarantinedItems.first.eventId, equals('legacy_evt_99'));
      expect(queue.pendingCount, equals(1));
      expect(queue.pendingItems.first.eventId, equals('patient_A_evt_01'));

      final flushResult = await queue.flush();
      expect(flushResult.status, equals(SyncStatus.success));
      expect(batchCalls, equals(1));
      expect(queue.pendingCount, equals(0));

      expect(queue.quarantinedCount, equals(1));
      expect(queue.quarantinedItems.first.eventId, equals('legacy_evt_99'));

      queue.dispose();
      apiClient.close();
      SharedPreferences.resetStatic();
      SharedPreferences.setMockInitialValues({});
    });

    test('D2-F: Swapping tokenProvider to another patient immediately aborts flush without HTTP dispatch', () async {
      int httpCalls = 0;
      final mockClient = MockClient((request) async {
        httpCalls++;
        return http.Response(json.encode({'status': 'success'}), 200);
      });

      String currentToken = _buildTestJwt(patientId: 'patient_A');
      final apiClient = AegisApiClient(
        baseUrl: 'http://127.0.0.1:8000',
        tokenProvider: () async => currentToken,
        client: mockClient,
      );

      final queue = VitalsSyncQueue(
        apiClient: apiClient,
        activeCycleId: 'cycle_A',
        activePatientId: 'patient_A',
      );
      await queue.init();

      await queue.enqueue(HealthMeasurement(
        timestamp: DateTime.now(),
        pulse: 79,
        source: HealthDataSource.watchSensor,
      ));

      // Token provider is swapped to patient B's JWT before flush executes
      currentToken = _buildTestJwt(patientId: 'patient_B');

      final result = await queue.flush();
      expect(result.status, equals(SyncStatus.paused));
      expect(queue.isPaused, isTrue);
      expect(queue.lastSyncError, contains('Patient isolation mismatch'));
      expect(httpCalls, equals(0)); // No data sent with hijacked token!
      expect(queue.pendingCount, equals(1)); // Data intact

      queue.dispose();
      apiClient.close();
    });

    test('D2-G: JWT with conflicting sub and patient_id claims is rejected as invalid', () async {
      final headerJson = json.encode({'alg': 'HS256', 'typ': 'JWT'});
      final payloadJson = json.encode({
        'sub': 'patient_A',
        'patient_id': 'patient_FORGED', // conflicting claim!
        'type': 'access',
      });
      final h = base64Url.encode(utf8.encode(headerJson)).replaceAll('=', '');
      final p = base64Url.encode(utf8.encode(payloadJson)).replaceAll('=', '');
      final forgedJwt = '$h.$p.fakesig';

      expect(AuthSessionManager.extractPatientIdFromJwt(forgedJwt), isNull);

      int httpCalls = 0;
      final mockClient = MockClient((r) async {
        httpCalls++;
        return http.Response('ok', 200);
      });

      final apiClient = AegisApiClient(
        baseUrl: 'http://127.0.0.1:8000',
        tokenProvider: () async => forgedJwt,
        client: mockClient,
      );

      final queue = VitalsSyncQueue(
        apiClient: apiClient,
        activeCycleId: 'cycle_A',
        activePatientId: 'patient_A',
      );
      await queue.init();

      await queue.enqueue(HealthMeasurement(
        timestamp: DateTime.now(),
        pulse: 71,
        source: HealthDataSource.watchSensor,
      ));

      final result = await queue.flush();
      expect(result.status, equals(SyncStatus.paused));
      expect(httpCalls, equals(0));
      queue.dispose();
      apiClient.close();
    });
  });

  group('Stage 5.3C-3A-FIX: Medium Security Invariants (D3 + D4 + D5)', () {
    // ---------------------------------------------------------
    // D3: Stale HTTP Response Isolation
    // ---------------------------------------------------------

    test('D3-A: Stale 401 response from patient A request does not pause patient B queue', () async {
      final completerA = Completer<http.Response>();
      final requestInFlight = Completer<void>();
      final jwtA = _buildTestJwt(patientId: 'patient_A');
      final jwtB = _buildTestJwt(patientId: 'patient_B');

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
          if (request.headers['authorization'] == 'Bearer $jwtA') {
            if (!requestInFlight.isCompleted) requestInFlight.complete();
            return await completerA.future;
          }
          if (request.headers['authorization'] == 'Bearer $jwtB') {
            return http.Response(json.encode({
              'status': 'success',
              'cycle_id': 'cycle_B',
              'recorded_count': 1,
              'deduplicated_count': 0,
              'batch_size': 1,
              'received_at': DateTime.now().toUtc().toIso8601String(),
            }), 200);
          }
        }
        return http.Response('Not Found', 404);
      });

      final apiClientA = AegisApiClient(
        baseUrl: 'http://127.0.0.1:8000',
        tokenProvider: () async => jwtA,
        client: mockClient,
      );

      final queue = VitalsSyncQueue(
        apiClient: apiClientA,
        activeCycleId: 'cycle_A',
        activePatientId: 'patient_A',
      );
      await queue.init();

      await queue.enqueue(HealthMeasurement(
        timestamp: DateTime.now(),
        pulse: 75,
        source: HealthDataSource.watchSensor,
      ));

      // 1. Trigger flush for patient A
      final flushAFuture = queue.flush();
      await requestInFlight.future; // Guaranteed in-flight!

      // 2. Prepare patient B session manager and switch client while A's HTTP request is in-flight
      final storageB = InMemoryAuthTokenStorage();
      await storageB.saveSession(AuthSession(
        accessToken: jwtB,
        refreshToken: 'rt_patient_B',
        tokenType: 'Bearer',
        expiresIn: 3600,
        expiresAt: DateTime.now().add(const Duration(hours: 1)),
        refreshExpiresAt: DateTime.now().add(const Duration(days: 30)),
        deviceId: 'device_D3A',
        patientId: 'patient_B',
        scope: 'vitals:write vitals:read',
      ));
      final apiClientB = AegisApiClient(
        baseUrl: 'http://127.0.0.1:8000',
        client: mockClient,
      );
      final managerB = AuthSessionManager(
        apiClient: apiClientB,
        storage: storageB,
      );
      await managerB.initialize();

      // Switch to client B (updates sessionGeneration, client, activePatientId synchronously)
      queue.updateApiClient(apiClientB);
      expect(queue.activePatientId, equals('patient_B'));
      expect(queue.isPaused, isFalse);

      // 3. Complete A request with 401 Unauthorized
      completerA.complete(http.Response('{"detail": "Session expired for A"}', 401));
      final resultA = await flushAFuture;

      // 4. Verify stale 401 was discarded and did NOT pause queue B
      expect(resultA.status, equals(SyncStatus.paused));
      expect(resultA.message, contains('Stale HTTP response discarded'));
      expect(queue.isPaused, isFalse);
      expect(queue.lastSyncError, isNull);

      queue.dispose();
      apiClientA.close();
      managerB.dispose();
      apiClientB.close();
    });

    test('D3-B: Stale 503 response from patient A does not increment backoff or failures for patient B', () async {
      final completerA = Completer<http.Response>();
      final requestInFlight = Completer<void>();
      final jwtA = _buildTestJwt(patientId: 'patient_A');
      final jwtB = _buildTestJwt(patientId: 'patient_B');

      final mockClient = MockClient((request) async {
        if (request.url.path == '/api/v1/vitals/batch') {
          if (!requestInFlight.isCompleted) requestInFlight.complete();
          return await completerA.future;
        }
        return http.Response('Not Found', 404);
      });

      final apiClientA = AegisApiClient(
        baseUrl: 'http://127.0.0.1:8000',
        tokenProvider: () async => jwtA,
        client: mockClient,
      );

      final queue = VitalsSyncQueue(
        apiClient: apiClientA,
        activeCycleId: 'cycle_A',
        activePatientId: 'patient_A',
      );
      await queue.init();

      await queue.enqueue(HealthMeasurement(
        timestamp: DateTime.now(),
        pulse: 75,
        source: HealthDataSource.watchSensor,
      ));

      final flushAFuture = queue.flush();
      await requestInFlight.future; // Guaranteed in-flight!

      // Switch to B
      final storageB = InMemoryAuthTokenStorage();
      await storageB.saveSession(AuthSession(
        accessToken: jwtB,
        refreshToken: 'rt_patient_B',
        tokenType: 'Bearer',
        expiresIn: 3600,
        expiresAt: DateTime.now().add(const Duration(hours: 1)),
        refreshExpiresAt: DateTime.now().add(const Duration(days: 30)),
        deviceId: 'device_D3B',
        patientId: 'patient_B',
        scope: 'vitals:write vitals:read',
      ));
      final apiClientB = AegisApiClient(
        baseUrl: 'http://127.0.0.1:8000',
        client: mockClient,
      );
      final managerB = AuthSessionManager(
        apiClient: apiClientB,
        storage: storageB,
      );
      await managerB.initialize();

      queue.updateApiClient(apiClientB);
      expect(queue.activePatientId, equals('patient_B'));

      // Complete A with 503 Service Unavailable
      completerA.complete(http.Response('Service Unavailable', 503));
      final resultA = await flushAFuture;

      expect(resultA.status, equals(SyncStatus.paused));
      expect(resultA.message, contains('Stale HTTP response discarded'));
      expect(queue.isPaused, isFalse);
      expect(queue.lastSyncError, isNull);

      queue.dispose();
      apiClientA.close();
      managerB.dispose();
      apiClientB.close();
    });

    test('D3-C: Stale 404 response from patient A does not unconfirm cycle or pause patient B', () async {
      final completerA = Completer<http.Response>();
      final requestInFlight = Completer<void>();
      final jwtA = _buildTestJwt(patientId: 'patient_A');
      final jwtB = _buildTestJwt(patientId: 'patient_B');

      final mockClient = MockClient((request) async {
        if (request.url.path == '/api/v1/vitals/batch') {
          if (!requestInFlight.isCompleted) requestInFlight.complete();
          return await completerA.future;
        }
        return http.Response('Not Found', 404);
      });

      final apiClientA = AegisApiClient(
        baseUrl: 'http://127.0.0.1:8000',
        tokenProvider: () async => jwtA,
        client: mockClient,
      );

      final queue = VitalsSyncQueue(
        apiClient: apiClientA,
        activeCycleId: 'cycle_A',
        activePatientId: 'patient_A',
      );
      await queue.init();

      await queue.enqueue(HealthMeasurement(
        timestamp: DateTime.now(),
        pulse: 75,
        source: HealthDataSource.watchSensor,
      ));

      final flushAFuture = queue.flush();
      await requestInFlight.future; // Guaranteed in-flight!

      // Switch to B
      final storageB = InMemoryAuthTokenStorage();
      await storageB.saveSession(AuthSession(
        accessToken: jwtB,
        refreshToken: 'rt_patient_B',
        tokenType: 'Bearer',
        expiresIn: 3600,
        expiresAt: DateTime.now().add(const Duration(hours: 1)),
        refreshExpiresAt: DateTime.now().add(const Duration(days: 30)),
        deviceId: 'device_D3C',
        patientId: 'patient_B',
        scope: 'vitals:write vitals:read',
      ));
      final apiClientB = AegisApiClient(
        baseUrl: 'http://127.0.0.1:8000',
        client: mockClient,
      );
      final managerB = AuthSessionManager(
        apiClient: apiClientB,
        storage: storageB,
      );
      await managerB.initialize();

      queue.updateApiClient(apiClientB);
      expect(queue.activePatientId, equals('patient_B'));

      // Complete A with 404 Not Found
      completerA.complete(http.Response('{"detail": "Cycle A not found"}', 404));
      final resultA = await flushAFuture;

      expect(resultA.status, equals(SyncStatus.paused));
      expect(resultA.message, contains('Stale HTTP response discarded'));
      expect(queue.isPaused, isFalse);

      queue.dispose();
      apiClientA.close();
      managerB.dispose();
      apiClientB.close();
    });

    test('D3-D: Stale 200 ACK correctly removes A items from pending and preserves B items intact', () async {
      final completerA = Completer<http.Response>();
      final requestInFlight = Completer<void>();
      final jwtA = _buildTestJwt(patientId: 'patient_A');
      final jwtB = _buildTestJwt(patientId: 'patient_B');

      final mockClient = MockClient((request) async {
        if (request.url.path == '/api/v1/vitals/batch') {
          if (request.headers['authorization'] == 'Bearer $jwtA') {
            if (!requestInFlight.isCompleted) requestInFlight.complete();
            return await completerA.future;
          }
        }
        return http.Response('Not Found', 404);
      });

      final apiClientA = AegisApiClient(
        baseUrl: 'http://127.0.0.1:8000',
        tokenProvider: () async => jwtA,
        client: mockClient,
      );

      final queue = VitalsSyncQueue(
        apiClient: apiClientA,
        activeCycleId: 'cycle_A',
        activePatientId: 'patient_A',
      );
      await queue.init();

      final itemA = await queue.enqueue(HealthMeasurement(
        timestamp: DateTime.now(),
        pulse: 72,
        source: HealthDataSource.watchSensor,
      ));

      final flushAFuture = queue.flush();
      await requestInFlight.future; // Guaranteed in-flight!

      // Switch to B
      final storageB = InMemoryAuthTokenStorage();
      await storageB.saveSession(AuthSession(
        accessToken: jwtB,
        refreshToken: 'rt_patient_B',
        tokenType: 'Bearer',
        expiresIn: 3600,
        expiresAt: DateTime.now().add(const Duration(hours: 1)),
        refreshExpiresAt: DateTime.now().add(const Duration(days: 30)),
        deviceId: 'device_D3D',
        patientId: 'patient_B',
        scope: 'vitals:write vitals:read',
      ));
      final apiClientB = AegisApiClient(
        baseUrl: 'http://127.0.0.1:8000',
        client: mockClient,
      );
      final managerB = AuthSessionManager(
        apiClient: apiClientB,
        storage: storageB,
      );
      await managerB.initialize();

      queue.updateApiClient(apiClientB);

      // Complete A with 200 OK
      completerA.complete(http.Response(json.encode({
        'status': 'success',
        'cycle_id': 'cycle_A',
        'recorded_count': 1,
        'deduplicated_count': 0,
        'batch_size': 1,
        'received_at': DateTime.now().toUtc().toIso8601String(),
      }), 200));

      final resultA = await flushAFuture;

      // Result of A flush reflects stale ACK handling
      expect(itemA.eventId, isNotEmpty);
      expect(resultA.processedCount, equals(1));
      // Item A was removed from pending because server acknowledged it
      expect(queue.pendingCount, equals(0));

      // Now enqueue item for patient B
      final itemB = await queue.enqueue(HealthMeasurement(
        timestamp: DateTime.now(),
        pulse: 88,
        source: HealthDataSource.watchSensor,
      ));

      expect(queue.pendingCount, equals(1));
      expect(queue.pendingItemsForPatient('patient_B').length, equals(1));
      expect(queue.pendingItemsForPatient('patient_B').first.eventId, equals(itemB.eventId));
      expect(queue.pendingItemsForPatient('patient_A').length, equals(0));

      queue.dispose();
      apiClientA.close();
      managerB.dispose();
      apiClientB.close();
    });

    // ---------------------------------------------------------
    // D4: Explicit Error Handling (403, 429, 422)
    // ---------------------------------------------------------

    test('D4-A: 403 Forbidden pauses queue and prevents repetitive network retries', () async {
      int postCalls = 0;
      final mockClient = MockClient((request) async {
        if (request.url.path == '/api/v1/vitals/batch') {
          postCalls++;
          return http.Response(json.encode({'detail': 'Forbidden: missing vitals:write scope'}), 403);
        }
        return http.Response('Not Found', 404);
      });

      final jwt = _buildTestJwt(patientId: 'patient_A');
      final apiClient = AegisApiClient(
        baseUrl: 'http://127.0.0.1:8000',
        tokenProvider: () async => jwt,
        client: mockClient,
      );

      final queue = VitalsSyncQueue(
        apiClient: apiClient,
        activeCycleId: 'cycle_A',
        activePatientId: 'patient_A',
      );
      await queue.init();

      await queue.enqueue(HealthMeasurement(
        timestamp: DateTime.now(),
        pulse: 80,
        source: HealthDataSource.watchSensor,
      ));

      final result = await queue.flush();
      expect(result.status, equals(SyncStatus.paused));
      expect(result.message, contains('403 Forbidden'));
      expect(queue.isPaused, isTrue);
      expect(postCalls, equals(1));

      // Attempt second flush: must be blocked with 0 new HTTP calls
      final secondResult = await queue.flush();
      expect(secondResult.status, equals(SyncStatus.paused));
      expect(postCalls, equals(1)); // No repeated call!

      queue.dispose();
      apiClient.close();
    });

    test('D4-B: 429 Rate Limit respects Retry-After duration and avoids data loss', () async {
      int postCalls = 0;
      final mockClient = MockClient((request) async {
        if (request.url.path == '/api/v1/vitals/batch') {
          postCalls++;
          return http.Response(
            json.encode({'detail': 'Rate limit exceeded'}),
            429,
            headers: {'retry-after': '30'},
          );
        }
        return http.Response('Not Found', 404);
      });

      final jwt = _buildTestJwt(patientId: 'patient_A');
      final apiClient = AegisApiClient(
        baseUrl: 'http://127.0.0.1:8000',
        tokenProvider: () async => jwt,
        client: mockClient,
      );

      final queue = VitalsSyncQueue(
        apiClient: apiClient,
        activeCycleId: 'cycle_A',
        activePatientId: 'patient_A',
      );
      await queue.init();

      await queue.enqueue(HealthMeasurement(
        timestamp: DateTime.now(),
        pulse: 76,
        source: HealthDataSource.watchSensor,
      ));

      final result = await queue.flush();
      expect(result.status, equals(SyncStatus.retryableError));
      expect(result.message, contains('429 Rate Limit'));
      expect(postCalls, equals(1));
      expect(queue.pendingCount, equals(1)); // Data preserved!

      // Attempt immediate second flush during cooldown: backoff blocks HTTP dispatch
      final secondResult = await queue.flush();
      expect(secondResult.status, equals(SyncStatus.retryableError));
      expect(secondResult.message, contains('Backoff active cooldown'));
      expect(postCalls, equals(1)); // 0 HTTP calls during cooldown

      queue.dispose();
      apiClient.close();
    });

    test('D4-C: 422 Validation error safely isolates poison pill into quarantine without data loss', () async {
      int batchCalls = 0;
      final recordedEvents = <String>[];

      final mockClient = MockClient((request) async {
        if (request.url.path == '/api/v1/vitals/batch') {
          batchCalls++;
          final body = json.decode(request.body) as Map<String, dynamic>;
          final measurements = body['measurements'] as List<dynamic>;

          // Poison pill simulation: if any measurement has pulse == 999, server rejects whole batch with 422
          final hasInvalid = measurements.any((m) => m['pulse'] == 999);
          if (hasInvalid) {
            return http.Response(json.encode({'detail': 'Validation error: invalid pulse 999'}), 422);
          }

          recordedEvents.addAll(measurements.map((m) => m['pulse'].toString()));
          return http.Response(json.encode({
            'status': 'success',
            'cycle_id': 'cycle_A',
            'recorded_count': measurements.length,
            'deduplicated_count': 0,
            'batch_size': measurements.length,
            'received_at': DateTime.now().toUtc().toIso8601String(),
          }), 200);
        }
        return http.Response('Not Found', 404);
      });

      final jwt = _buildTestJwt(patientId: 'patient_A');
      final apiClient = AegisApiClient(
        baseUrl: 'http://127.0.0.1:8000',
        tokenProvider: () async => jwt,
        client: mockClient,
      );

      final queue = VitalsSyncQueue(
        apiClient: apiClient,
        activeCycleId: 'cycle_A',
        activePatientId: 'patient_A',
      );
      await queue.init();

      // Enqueue 3 items: valid, invalid (pulse: 999), valid
      await queue.enqueue(HealthMeasurement(
        timestamp: DateTime.now(),
        pulse: 70,
        source: HealthDataSource.watchSensor,
      ));
      final itemBad = await queue.enqueue(HealthMeasurement(
        timestamp: DateTime.now(),
        pulse: 999,
        source: HealthDataSource.watchSensor,
      ));
      await queue.enqueue(HealthMeasurement(
        timestamp: DateTime.now(),
        pulse: 72,
        source: HealthDataSource.watchSensor,
      ));

      expect(queue.pendingCount, equals(3));

      final result = await queue.flush();

      // Verify that splitting occurred, 2 healthy items delivered, 1 poison pill quarantined
      expect(batchCalls, greaterThan(1));
      expect(result.status, equals(SyncStatus.conflict));
      expect(recordedEvents, containsAll(['70', '72']));
      expect(queue.pendingCount, equals(0));
      expect(queue.quarantinedCount, equals(1));
      expect(queue.quarantinedItems.first.eventId, equals(itemBad.eventId));
      expect(queue.quarantinedItems.first.lastError, contains('422 Validation'));

      queue.dispose();
      apiClient.close();
    });

    test('D4-D: Errors of prior patient do not block new patient from syncing', () async {
      int batchCalls = 0;
      final jwtA = _buildTestJwt(patientId: 'patient_A');
      final jwtB = _buildTestJwt(patientId: 'patient_B');

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
          if (request.headers['authorization'] == 'Bearer $jwtA') {
            return http.Response(json.encode({'detail': 'Forbidden for A'}), 403);
          }
          if (request.headers['authorization'] == 'Bearer $jwtB') {
            return http.Response(json.encode({
              'status': 'success',
              'cycle_id': 'cycle_B',
              'recorded_count': 1,
              'deduplicated_count': 0,
              'batch_size': 1,
              'received_at': DateTime.now().toUtc().toIso8601String(),
            }), 200);
          }
        }
        return http.Response('Not Found', 404);
      });

      final apiClientA = AegisApiClient(
        baseUrl: 'http://127.0.0.1:8000',
        tokenProvider: () async => jwtA,
        client: mockClient,
      );

      final queue = VitalsSyncQueue(
        apiClient: apiClientA,
        activeCycleId: 'cycle_A',
        activePatientId: 'patient_A',
      );
      await queue.init();

      await queue.enqueue(HealthMeasurement(
        timestamp: DateTime.now(),
        pulse: 70,
        source: HealthDataSource.watchSensor,
      ));

      final resultA = await queue.flush();
      expect(resultA.status, equals(SyncStatus.paused));
      expect(queue.isPaused, isTrue);

      // Now switch to patient B with new credentials
      final storageB = InMemoryAuthTokenStorage();
      await storageB.saveSession(AuthSession(
        accessToken: jwtB,
        refreshToken: 'rt_patient_B',
        tokenType: 'Bearer',
        expiresIn: 3600,
        expiresAt: DateTime.now().add(const Duration(hours: 1)),
        refreshExpiresAt: DateTime.now().add(const Duration(days: 30)),
        deviceId: 'device_D4D',
        patientId: 'patient_B',
        scope: 'vitals:write vitals:read',
      ));
      final apiClientB = AegisApiClient(
        baseUrl: 'http://127.0.0.1:8000',
        client: mockClient,
      );
      final managerB = AuthSessionManager(
        apiClient: apiClientB,
        storage: storageB,
      );
      await managerB.initialize();

      queue.updateApiClient(apiClientB);

      await queue.enqueue(HealthMeasurement(
        timestamp: DateTime.now(),
        pulse: 82,
        source: HealthDataSource.watchSensor,
      ));

      final resultB = await queue.flush();
      expect(batchCalls, equals(2));
      expect(resultB.status, equals(SyncStatus.success));
      expect(queue.isPaused, isFalse);

      queue.dispose();
      apiClientA.close();
      managerB.dispose();
      apiClientB.close();
    });

    // ---------------------------------------------------------
    // D5: Token Provider Lifecycle
    // ---------------------------------------------------------

    test('D5-A: AuthSessionManager.dispose() detaches own tokenProvider and attachedSessionManager', () async {
      final mockClient = MockClient((request) async => http.Response('ok', 200));
      final apiClient = AegisApiClient(
        baseUrl: 'http://127.0.0.1:8000',
        client: mockClient,
      );

      expect(apiClient.tokenProvider, isNull);
      expect(apiClient.attachedSessionManager, isNull);

      final storage1 = InMemoryAuthTokenStorage();
      final m1 = AuthSessionManager(
        apiClient: apiClient,
        storage: storage1,
      );

      expect(apiClient.tokenProvider, isNotNull);
      expect(identical(apiClient.attachedSessionManager, m1), isTrue);

      // Dispose M1
      m1.dispose();

      // TokenProvider and attachedSessionManager must be detached
      expect(apiClient.tokenProvider, isNull);
      expect(apiClient.attachedSessionManager, isNull);

      // M2 can now cleanly attach to the same apiClient
      final storage2 = InMemoryAuthTokenStorage();
      final m2 = AuthSessionManager(
        apiClient: apiClient,
        storage: storage2,
      );

      expect(apiClient.tokenProvider, isNotNull);
      expect(identical(apiClient.attachedSessionManager, m2), isTrue);

      m2.dispose();
      apiClient.close();
    });

    test('D5-B: AuthSessionManager.dispose() does not remove foreign tokenProvider', () async {
      final mockClient = MockClient((request) async => http.Response('ok', 200));
      final apiClient = AegisApiClient(
        baseUrl: 'http://127.0.0.1:8000',
        client: mockClient,
      );

      final storage = InMemoryAuthTokenStorage();
      final m1 = AuthSessionManager(
        apiClient: apiClient,
        storage: storage,
      );

      // External rebind
      Future<String?> foreignProvider() async => 'foreign_token';
      apiClient.bindTokenProvider(foreignProvider);

      // Dispose M1
      m1.dispose();

      // foreignProvider must NOT have been unbound
      expect(identical(apiClient.tokenProvider, foreignProvider), isTrue);

      apiClient.close();
    });
  });
}
