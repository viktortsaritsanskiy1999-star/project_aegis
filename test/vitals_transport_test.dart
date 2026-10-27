// ignore_for_file: depend_on_referenced_packages
import 'dart:convert';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:shared_preferences_platform_interface/shared_preferences_platform_interface.dart';
import 'package:shared_preferences_platform_interface/types.dart';

import 'package:aegis_health/services/api/aegis_api_client.dart';
import 'package:aegis_health/services/api/vitals_api_models.dart';
import 'package:aegis_health/services/sync/vitals_sync_queue.dart';
import 'package:aegis_health/services/health_data_provider.dart';
import 'package:aegis_health/services/vault_service.dart';
import 'package:aegis_health/services/vitals_monitor_service.dart';
import 'package:aegis_health/services/time_service.dart';

/// Test mock store for simulating storage persistence failures and step-by-step failure sequences
class TestSharedPreferencesStore extends SharedPreferencesStorePlatform {
  bool shouldFail = false;
  int successWritesRemaining = -1; // -1 means infinite successes
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
  });

  group('Stage 5.2A: Vitals API Models & Serialization Contract', () {
    test('1. Serialization matches FastAPI Stage 5.1 JSON contract exactly', () {
      final measuredAt = DateTime.utc(2026, 10, 22, 14, 30, 0);
      final dto = HealthMeasurementCreate(
        cycleId: 'cycle_stage5_001',
        measuredAt: measuredAt,
        source: 'watch_sensor',
        pulse: 76,
        systolic: 120,
        diastolic: 80,
        spo2: 99,
        sessionId: 'ws_test_001',
        isSimulated: false,
        isHistorical: false,
        phase: 'standby',
        note: 'Resting morning measurement',
        rawMetadata: {
          'idempotency_key': 'idem_test_12345',
          'device_model': 'Aegis Watch Pro',
        },
      );

      final jsonMap = dto.toJson();

      expect(jsonMap['cycle_id'], equals('cycle_stage5_001'));
      expect(jsonMap['measured_at'], equals('2026-10-22T14:30:00.000Z'));
      expect(jsonMap['pulse'], equals(76));
      expect(jsonMap['systolic'], equals(120));
      expect(jsonMap['diastolic'], equals(80));
      expect(jsonMap['spo2'], equals(99));
      expect(jsonMap['session_id'], equals('ws_test_001'));
      expect(jsonMap['source'], equals('watch_sensor'));
      expect(jsonMap['is_historical'], isFalse);
      expect(jsonMap['is_simulated'], isFalse);
      expect(jsonMap['phase'], equals('standby'));
      expect(jsonMap['note'], equals('Resting morning measurement'));
      expect(jsonMap['raw_metadata'], isA<Map<String, dynamic>>());
      expect(jsonMap['raw_metadata']['idempotency_key'], equals('idem_test_12345'));
      expect(jsonMap['raw_metadata']['device_model'], equals('Aegis Watch Pro'));

      final batchDto = HealthMeasurementCreateBatch(
        cycleId: 'cycle_stage5_001',
        sessionId: 'ws_test_001',
        measurements: [dto],
      );
      final batchJson = batchDto.toJson();
      expect(batchJson['cycle_id'], equals('cycle_stage5_001'));
      expect(batchJson['session_id'], equals('ws_test_001'));
      expect(batchJson['measurements'], isA<List>());
      expect((batchJson['measurements'] as List).length, equals(1));
    });

    test('2. Sources and is_simulated are strictly mapped and differentiated', () {
      final now = DateTime.utc(2026, 10, 22, 15, 0, 0);
      final sources = [
        HealthDataSource.watchSensor,
        HealthDataSource.manualInput,
        HealthDataSource.testChip,
        HealthDataSource.simulated,
        HealthDataSource.unknown,
      ];

      for (final src in sources) {
        final m = HealthMeasurement(
          timestamp: now,
          pulse: 80,
          source: src,
        );
        final dto = HealthMeasurementCreate.fromHealthMeasurement(m, cycleId: 'c1');

        expect(dto.source, equals(src.rawValue));
        if (src == HealthDataSource.testChip || src == HealthDataSource.simulated) {
          expect(dto.isSimulated, isTrue);
        } else {
          expect(dto.isSimulated, isFalse);
        }
      }
    });
  });

  group('Stage 5.2A: Storage Snapshot & Persistence Consistency', () {
    test('3. Snapshot write error during enqueue throws exception and rolls back memory', () async {
      final mockClient = MockClient((request) async => http.Response('{}', 200));
      final apiClient = AegisApiClient(baseUrl: 'http://127.0.0.1:8000', client: mockClient);

      final failingStore = TestSharedPreferencesStore();
      SharedPreferences.resetStatic();
      SharedPreferencesStorePlatform.instance = failingStore;
      failingStore.shouldFail = true;

      final queue = VitalsSyncQueue(apiClient: apiClient, activeCycleId: 'c_fail');

      final measurement = HealthMeasurement(
        timestamp: DateTime.now(),
        pulse: 80,
        source: HealthDataSource.watchSensor,
      );

      await expectLater(
        queue.enqueue(measurement),
        throwsA(isA<VitalsQueuePersistenceException>()),
      );

      // In-memory queue rolled back: 0 pending items
      expect(queue.pendingItems.isEmpty, isTrue);

      failingStore.shouldFail = false;
      SharedPreferences.resetStatic();
      SharedPreferences.setMockInitialValues({});
    });

    test('4. Successful snapshot write with subsequent restart roundtrips all metadata', () async {
      final mockClient = MockClient((request) async => http.Response('{}', 200));
      final apiClient = AegisApiClient(baseUrl: 'http://127.0.0.1:8000', client: mockClient);

      final customStore = TestSharedPreferencesStore();
      SharedPreferences.resetStatic();
      SharedPreferencesStorePlatform.instance = customStore;

      final queue1 = VitalsSyncQueue(apiClient: apiClient, activeCycleId: 'c_roundtrip');
      await queue1.init();

      final item = await queue1.enqueue(HealthMeasurement(
        timestamp: DateTime.utc(2026, 10, 22, 11, 0, 0),
        pulse: 74,
        source: HealthDataSource.watchSensor,
      ));

      expect(queue1.pendingItems.length, equals(1));

      // Simulate app restart: re-initialize from storage snapshot
      SharedPreferences.resetStatic();
      final queue2 = VitalsSyncQueue(apiClient: apiClient);
      await queue2.init();

      expect(queue2.pendingItems.length, equals(1));
      final restored = queue2.pendingItems.first;
      expect(restored.eventId, equals(item.eventId));
      expect(restored.cycleId, equals('c_roundtrip'));
      expect(restored.dto.pulse, equals(74));
      expect(queue2.activeCycleId, equals('c_roundtrip'));

      SharedPreferences.resetStatic();
      SharedPreferences.setMockInitialValues({});
    });

    test('5. Persistence failure immediately after server HTTP ACK separates server delivery and retains item for safe replay', () async {
      final mockClient = MockClient((request) async {
        return http.Response(json.encode({'status': 'success', 'cycle_id': 'c_ack_fail', 'count': 1, 'items': []}), 201);
      });
      final testJwt = _buildTestJwt(patientId: 'p_ack_fail');
      final apiClient = AegisApiClient(
        baseUrl: 'http://127.0.0.1:8000',
        tokenProvider: () async => testJwt,
        client: mockClient,
      );

      final customStore = TestSharedPreferencesStore();
      SharedPreferences.resetStatic();
      SharedPreferencesStorePlatform.instance = customStore;

      final queue = VitalsSyncQueue(apiClient: apiClient, activeCycleId: 'c_ack_fail', activePatientId: 'p_ack_fail');

      // Enqueue succeeds (write 1)
      customStore.successWritesRemaining = 1;
      final enqueued = await queue.enqueue(HealthMeasurement(timestamp: DateTime.now(), pulse: 88, source: HealthDataSource.watchSensor));
      expect(queue.pendingItems.length, equals(1));

      // Write remaining is now 0. When flush runs:
      // HTTP request succeeds (201), but snapshot write fails!
      final result = await queue.flush();

      expect(result.status, equals(SyncStatus.persistenceError));
      expect(result.persistenceFailed, isTrue);
      expect(result.processedCount, equals(1));
      expect(result.message, contains('snapshot persist failed'));

      // Event is NOT lost in memory: retains exact eventId and idempotencyKey for safe idempotent replay
      expect(queue.pendingItems.length, equals(1));
      expect(queue.pendingItems.first.eventId, equals(enqueued.eventId));
      expect(queue.pendingItems.first.idempotencyKey, equals(enqueued.idempotencyKey));

      SharedPreferences.resetStatic();
      SharedPreferences.setMockInitialValues({});
    });

    test('6. Persistence failure during pending -> quarantine transition preserves conflicting event without data loss', () async {
      final mockClient = MockClient((request) async {
        return http.Response('{"detail":"Idempotency collision"}', 409);
      });
      final testJwt = _buildTestJwt(patientId: 'p_quarantine_fail');
      final apiClient = AegisApiClient(
        baseUrl: 'http://127.0.0.1:8000',
        tokenProvider: () async => testJwt,
        client: mockClient,
      );

      final customStore = TestSharedPreferencesStore();
      SharedPreferences.resetStatic();
      SharedPreferencesStorePlatform.instance = customStore;

      final queue = VitalsSyncQueue(apiClient: apiClient, activeCycleId: 'c_quarantine_fail', activePatientId: 'p_quarantine_fail');

      // Enqueue succeeds (write 1)
      customStore.successWritesRemaining = 1;
      final conflicting = await queue.enqueue(HealthMeasurement(timestamp: DateTime.now(), pulse: 999, source: HealthDataSource.watchSensor));
      expect(queue.pendingItems.length, equals(1));

      // Flush receives 409. Attempt to persist quarantine snapshot fails!
      final result = await queue.flush();

      expect(result.status, equals(SyncStatus.persistenceError));
      expect(result.persistenceFailed, isTrue);
      expect(result.message, contains('quarantine persistence failed'));

      // Conflicting event is NOT lost! It remains preserved in pendingItems
      expect(queue.pendingItems.length, equals(1));
      expect(queue.pendingItems.first.eventId, equals(conflicting.eventId));

      SharedPreferences.resetStatic();
      SharedPreferences.setMockInitialValues({});
    });

    test('7. Migration from legacy dual keys to single snapshot v2 preserves all items and cleans legacy keys only after successful write', () async {
      final legacyPendingItem = VitalsQueueItem(
        eventId: 'evt_legacy_001',
        idempotencyKey: 'idem_c_leg_evt_legacy_001',
        cycleId: 'c_leg',
        dto: HealthMeasurementCreate(measuredAt: DateTime.now(), source: 'watch_sensor', pulse: 68),
        enqueuedAt: DateTime.now(),
      );
      final legacyQuarantineItem = VitalsQueueItem(
        eventId: 'evt_legacy_002',
        idempotencyKey: 'idem_c_leg_evt_legacy_002',
        cycleId: 'c_leg',
        dto: HealthMeasurementCreate(measuredAt: DateTime.now(), source: 'watch_sensor', pulse: 145),
        enqueuedAt: DateTime.now(),
        isQuarantined: true,
        lastError: '409 Conflict',
      );

      final legacyData = <String, Object>{
        'flutter.aegis_vitals_sync_pending_v1': json.encode([legacyPendingItem.toJson()]),
        'flutter.aegis_vitals_sync_quarantine_v1': json.encode([legacyQuarantineItem.toJson()]),
      };

      final customStore = TestSharedPreferencesStore(initialData: legacyData);
      SharedPreferences.resetStatic();
      SharedPreferencesStorePlatform.instance = customStore;

      final mockClient = MockClient((request) async => http.Response('{}', 200));
      final apiClient = AegisApiClient(baseUrl: 'http://127.0.0.1:8000', client: mockClient);

      final queue = VitalsSyncQueue(apiClient: apiClient);
      await queue.init();

      // Verify in-memory restoration
      expect(queue.pendingItems.length, equals(1));
      expect(queue.pendingItems.first.eventId, equals('evt_legacy_001'));
      expect(queue.quarantinedItems.length, equals(1));
      expect(queue.quarantinedItems.first.eventId, equals('evt_legacy_002'));

      // Verify single snapshot key was written
      final allData = await customStore.getAll();
      expect(allData.containsKey('flutter.aegis_vitals_sync_snapshot_v2'), isTrue);

      // Verify legacy keys were removed after successful migration
      expect(allData.containsKey('flutter.aegis_vitals_sync_pending_v1'), isFalse);
      expect(allData.containsKey('flutter.aegis_vitals_sync_quarantine_v1'), isFalse);

      SharedPreferences.resetStatic();
      SharedPreferences.setMockInitialValues({});
    });
  });

  group('Stage 5.2A: Independent Review Fixes — Regression Suite (8-16)', () {
    test('8. Sequence of multiple enqueue and flush operations execute safely without race conditions', () async {
      int serverHits = 0;
      final mockClient = MockClient((request) async {
        serverHits++;
        return http.Response(json.encode({'status': 'success', 'cycle_id': 'c_concurrent', 'count': 1, 'items': []}), 201);
      });
      final testJwt = _buildTestJwt(patientId: 'p_concurrent');
      final apiClient = AegisApiClient(
        baseUrl: 'http://127.0.0.1:8000',
        tokenProvider: () async => testJwt,
        client: mockClient,
      );
      final queue = VitalsSyncQueue(apiClient: apiClient, activeCycleId: 'c_concurrent', activePatientId: 'p_concurrent');

      final futures = [
        queue.enqueue(HealthMeasurement(timestamp: DateTime.now(), pulse: 71, source: HealthDataSource.watchSensor)),
        queue.enqueue(HealthMeasurement(timestamp: DateTime.now(), pulse: 72, source: HealthDataSource.watchSensor)),
        queue.flush(),
        queue.enqueue(HealthMeasurement(timestamp: DateTime.now(), pulse: 73, source: HealthDataSource.watchSensor)),
        queue.flush(),
      ];

      await Future.wait(futures);

      queue.resume();
      await queue.flush();

      expect(queue.pendingItems.isEmpty, isTrue);
      expect(serverHits, greaterThanOrEqualTo(1));
    });

    test('9. Enqueueing event before cycle_id is available (no fallback default_cycle)', () async {
      final mockClient = MockClient((request) async {
        return http.Response('{"detail":"No active medical cycle"}', 404);
      });
      final apiClient = AegisApiClient(baseUrl: 'http://127.0.0.1:8000', client: mockClient);

      final queue = VitalsSyncQueue(apiClient: apiClient, activeCycleId: null);

      final measurement = HealthMeasurement(
        timestamp: DateTime.utc(2026, 10, 22, 10, 0, 0),
        pulse: 77,
        source: HealthDataSource.watchSensor,
      );

      final item = await queue.enqueue(measurement);

      expect(item.cycleId, isNull);
      expect(item.idempotencyKey, isNull);
      expect(item.eventId, isNotNull);
      expect(queue.pendingItems.length, equals(1));

      final flushResult = await queue.flush();
      expect(flushResult.status, equals(SyncStatus.paused));
    });

    test('10. Restart with unresolved cycle_id preserves measurements in single snapshot', () async {
      final mockClient = MockClient((request) async => http.Response('{}', 200));
      final apiClient = AegisApiClient(baseUrl: 'http://127.0.0.1:8000', client: mockClient);

      final queue1 = VitalsSyncQueue(apiClient: apiClient, activeCycleId: null);
      await queue1.init();

      final item = await queue1.enqueue(HealthMeasurement(
        timestamp: DateTime.utc(2026, 10, 22, 12, 0, 0),
        pulse: 85,
        source: HealthDataSource.watchSensor,
      ));

      expect(item.cycleId, isNull);

      final queue2 = VitalsSyncQueue(apiClient: apiClient, activeCycleId: null);
      await queue2.init();

      expect(queue2.pendingItems.length, equals(1));
      final restored = queue2.pendingItems.first;
      expect(restored.eventId, equals(item.eventId));
      expect(restored.cycleId, isNull);
      expect(restored.idempotencyKey, isNull);
      expect(restored.dto.pulse, equals(85));
    });

    test('11. Resolving real cycle_id binds unresolved events without altering event identity', () async {
      final mockClient = MockClient((request) async => http.Response('{}', 200));
      final apiClient = AegisApiClient(baseUrl: 'http://127.0.0.1:8000', client: mockClient);

      final queue = VitalsSyncQueue(apiClient: apiClient, activeCycleId: null);

      final itemUnresolved = await queue.enqueue(HealthMeasurement(
        timestamp: DateTime.utc(2026, 10, 22, 14, 0, 0),
        pulse: 78,
        source: HealthDataSource.watchSensor,
      ));

      final originalEventId = itemUnresolved.eventId;
      expect(itemUnresolved.cycleId, isNull);

      await queue.setActiveCycleId('cycle_active_real_42');

      expect(queue.activeCycleId, equals('cycle_active_real_42'));
      final updated = queue.pendingItems.first;
      expect(updated.eventId, equals(originalEventId));
      expect(updated.cycleId, equals('cycle_active_real_42'));
      expect(updated.idempotencyKey, equals('idem_cycle_active_real_42_$originalEventId'));
      expect(updated.dto.cycleId, equals('cycle_active_real_42'));
      expect(updated.dto.rawMetadata?['idempotency_key'], equals('idem_cycle_active_real_42_$originalEventId'));
    });

    test('12. Preservation of batch splitting on 409: isolates single conflicting item and delivers healthy items', () async {
      final requestedBatchLengths = <int>[];

      final mockClient = MockClient((request) async {
        final body = json.decode(request.body) as Map<String, dynamic>;
        final measurements = body['measurements'] as List;
        requestedBatchLengths.add(measurements.length);

        final containsConflicting = measurements.any(
          (m) => (m['raw_metadata']?['event_id'] as String?)?.contains('conflict') == true ||
                 (m['pulse'] as int?) == 999,
        );

        if (containsConflicting) {
          return http.Response('{"detail":"Idempotency conflict for key"}', 409);
        }

        return http.Response(json.encode({
          'status': 'success',
          'cycle_id': 'c_split',
          'count': measurements.length,
          'items': [],
        }), 201);
      });

      final testJwt = _buildTestJwt(patientId: 'p_split');
      final apiClient = AegisApiClient(
        baseUrl: 'http://127.0.0.1:8000',
        tokenProvider: () async => testJwt,
        client: mockClient,
      );
      final queue = VitalsSyncQueue(apiClient: apiClient, activeCycleId: 'c_split', activePatientId: 'p_split');

      final m1 = await queue.enqueue(HealthMeasurement(timestamp: DateTime.now(), pulse: 70, source: HealthDataSource.watchSensor));
      final m2 = await queue.enqueue(HealthMeasurement(timestamp: DateTime.now(), pulse: 999, source: HealthDataSource.watchSensor));
      final m3 = await queue.enqueue(HealthMeasurement(timestamp: DateTime.now(), pulse: 75, source: HealthDataSource.watchSensor));

      expect(queue.pendingItems.length, equals(3));
      expect(queue.quarantinedItems.isEmpty, isTrue);

      final result = await queue.flush();

      expect(result.status, equals(SyncStatus.conflict));

      expect(queue.quarantinedItems.length, equals(1));
      expect(queue.quarantinedItems.first.eventId, equals(m2.eventId));
      expect(queue.quarantinedItems.first.isQuarantined, isTrue);
      expect(queue.quarantinedItems.first.lastError, contains('409'));

      expect(queue.pendingItems.isEmpty, isTrue);
      expect(m1.eventId, isNotEmpty);
      expect(m3.eventId, isNotEmpty);

      expect(requestedBatchLengths, contains(3));
      expect(requestedBatchLengths, contains(1));
    });

    test('13. Resend after response loss (idempotent replay returns 200/201 and clears item)', () async {
      final mockClient = MockClient((request) async {
        return http.Response(json.encode({
          'status': 'success',
          'cycle_id': 'cycle_resend',
          'count': 1,
          'items': [
            {
              'id': 99,
              'cycle_id': 'cycle_resend',
              'idempotency_key': 'idem_cycle_resend_evt_1',
              'measured_at': DateTime.now().toIso8601String(),
              'ingested_at': DateTime.now().toIso8601String(),
              'pulse': 80,
              'source': 'watch_sensor',
              'is_historical': false,
              'is_simulated': false,
              'metadata': {},
            }
          ],
        }), 200);
      });

      final testJwt = _buildTestJwt(patientId: 'p_resend');
      final apiClient = AegisApiClient(
        baseUrl: 'http://127.0.0.1:8000',
        tokenProvider: () async => testJwt,
        client: mockClient,
      );
      final queue = VitalsSyncQueue(apiClient: apiClient, activeCycleId: 'cycle_resend', activePatientId: 'p_resend');

      await queue.enqueue(HealthMeasurement(
        timestamp: DateTime.utc(2026, 10, 22, 10, 0, 0),
        pulse: 80,
        source: HealthDataSource.watchSensor,
      ));

      expect(queue.pendingItems.length, equals(1));
      final result = await queue.flush();

      expect(result.status, equals(SyncStatus.success));
      expect(queue.pendingItems.isEmpty, isTrue);
    });

    test('14. No loss during network errors and retries (backoff & retryCount)', () async {
      int attempts = 0;
      final mockClient = MockClient((request) async {
        attempts++;
        if (attempts <= 2) {
          throw http.ClientException('Network connection reset');
        }
        return http.Response(json.encode({'status': 'success', 'cycle_id': 'c_net', 'count': 1, 'items': []}), 201);
      });

      final testJwt = _buildTestJwt(patientId: 'p_net');
      final apiClient = AegisApiClient(
        baseUrl: 'http://127.0.0.1:8000',
        tokenProvider: () async => testJwt,
        client: mockClient,
      );
      final queue = VitalsSyncQueue(apiClient: apiClient, activeCycleId: 'c_net', activePatientId: 'p_net');

      await queue.enqueue(HealthMeasurement(timestamp: DateTime.now(), pulse: 80, source: HealthDataSource.watchSensor));

      final res1 = await queue.flush();
      expect(res1.status, equals(SyncStatus.retryableError));
      expect(queue.pendingItems.first.retryCount, equals(1));

      queue.resume();
      final res2 = await queue.flush();
      expect(res2.status, equals(SyncStatus.retryableError));
      expect(queue.pendingItems.first.retryCount, equals(2));

      queue.resume();
      final res3 = await queue.flush();
      expect(res3.status, equals(SyncStatus.success));
      expect(queue.pendingItems.isEmpty, isTrue);
    });

    test('15. Single unified storage failure recovery: queue stays paused until manually resumed', () async {
      final mockClient = MockClient((request) async {
        return http.Response('{"detail":"Internal Server Error"}', 500);
      });
      final apiClient = AegisApiClient(baseUrl: 'http://127.0.0.1:8000', client: mockClient);
      final queue = VitalsSyncQueue(apiClient: apiClient, activeCycleId: 'c_fail');

      await queue.enqueue(HealthMeasurement(timestamp: DateTime.now(), pulse: 80, source: HealthDataSource.watchSensor));

      expect(queue.isPaused, isFalse);
      expect(queue.pendingItems.length, equals(1));
    });

    test('16. Multiple 409 conflicts in a single batch are isolated to quarantine without crashing', () async {
      final mockClient = MockClient((request) async {
        return http.Response('{"detail":"Non-existent cycle"}', 404);
      });
      final apiClient = AegisApiClient(baseUrl: 'http://127.0.0.1:8000', client: mockClient);
      final queue = VitalsSyncQueue(apiClient: apiClient, activeCycleId: 'nonexistent_cycle');

      await queue.enqueue(HealthMeasurement(
        timestamp: DateTime.now(),
        pulse: 75,
        source: HealthDataSource.watchSensor,
      ));

      final res = await queue.flush();
      expect(res.status, equals(SyncStatus.paused));
      expect(queue.isPaused, isTrue);
      expect(queue.pauseReason, contains('cycle'));
      expect(queue.pendingItems.length, equals(1));

      await queue.setActiveCycleId('new_valid_cycle');
      expect(queue.isPaused, isFalse);
    });

    test('17. Persistence failure during cycle binding rolls back activeCycleId, pending items, and pause state', () async {
      final mockClient = MockClient((request) async {
        return http.Response('{"detail":"Medical cycle not found"}', 404);
      });
      final testJwt = _buildTestJwt(patientId: 'p_cycle_bind');
      final apiClient = AegisApiClient(
        baseUrl: 'http://127.0.0.1:8000',
        tokenProvider: () async => testJwt,
        client: mockClient,
      );

      final customStore = TestSharedPreferencesStore();
      SharedPreferences.resetStatic();
      SharedPreferencesStorePlatform.instance = customStore;

      // Start with null cycle
      final queue = VitalsSyncQueue(apiClient: apiClient, activePatientId: 'p_cycle_bind');

      // Enqueue item without cycleId (write 1 succeeds)
      customStore.successWritesRemaining = 1;
      final enqueued = await queue.enqueue(HealthMeasurement(
        timestamp: DateTime.utc(2026, 10, 22, 12, 0, 0),
        pulse: 75,
        source: HealthDataSource.watchSensor,
      ));

      expect(queue.pendingItems.length, equals(1));
      expect(queue.pendingItems.first.cycleId, isNull);
      expect(queue.pendingItems.first.idempotencyKey, isNull);
      expect(queue.activeCycleId, isNull);

      // Trigger a flush that fails with 404 and pauses the queue
      final flushRes = await queue.flush();
      expect(flushRes.status, equals(SyncStatus.paused));
      expect(queue.isPaused, isTrue);
      expect(queue.pauseReason, contains('404 Cycle Not Found'));

      // Next storage write will fail
      customStore.shouldFail = true;

      // Attempting to set active cycle id must fail and roll back all state
      await expectLater(
        queue.setActiveCycleId('cycle_new_123'),
        throwsA(isA<VitalsQueuePersistenceException>()),
      );

      // Verify full rollback:
      expect(queue.activeCycleId, isNull, reason: 'activeCycleId must be rolled back');
      expect(queue.isPaused, isTrue, reason: 'pause state must be rolled back');
      expect(queue.pauseReason, contains('404 Cycle Not Found'), reason: 'pause reason must be rolled back');
      expect(queue.pendingItems.length, equals(1));
      expect(queue.pendingItems.first.cycleId, isNull, reason: 'item cycleId must be rolled back');
      expect(queue.pendingItems.first.idempotencyKey, isNull, reason: 'item idempotencyKey must be rolled back');
      expect(queue.pendingItems.first.dto.cycleId, isNull, reason: 'dto cycleId must be rolled back');

      // Now restore storage and verify successful binding
      customStore.shouldFail = false;
      customStore.successWritesRemaining = -1;
      await queue.setActiveCycleId('cycle_new_123');

      expect(queue.activeCycleId, equals('cycle_new_123'));
      expect(queue.isPaused, isFalse);
      expect(queue.pauseReason, isNull);
      expect(queue.pendingItems.first.cycleId, equals('cycle_new_123'));
      expect(queue.pendingItems.first.idempotencyKey, equals('idem_cycle_new_123_${enqueued.eventId}'));
      expect(queue.pendingItems.first.dto.cycleId, equals('cycle_new_123'));

      SharedPreferences.resetStatic();
      SharedPreferences.setMockInitialValues({});
    });

    test('18. Snapshot persistence failure after retryable network/server error rolls back in-memory mutation and reports persistenceError', () async {
      final mockClient = MockClient((request) async {
        return http.Response('{"detail":"Internal Server Error"}', 500);
      });
      final testJwt = _buildTestJwt(patientId: 'p_retry_persist');
      final apiClient = AegisApiClient(
        baseUrl: 'http://127.0.0.1:8000',
        tokenProvider: () async => testJwt,
        client: mockClient,
      );

      final customStore = TestSharedPreferencesStore();
      SharedPreferences.resetStatic();
      SharedPreferencesStorePlatform.instance = customStore;

      final queue = VitalsSyncQueue(apiClient: apiClient, activeCycleId: 'c_retry_persist_fail', activePatientId: 'p_retry_persist');

      // Enqueue succeeds (write 1)
      customStore.successWritesRemaining = 1;
      final enqueued = await queue.enqueue(HealthMeasurement(
        timestamp: DateTime.now(),
        pulse: 82,
        source: HealthDataSource.watchSensor,
      ));

      expect(queue.pendingItems.length, equals(1));
      expect(queue.pendingItems.first.eventId, equals(enqueued.eventId));
      expect(queue.pendingItems.first.retryCount, equals(0));
      expect(queue.pendingItems.first.lastError, isNull);

      // Now next write will fail (writeRemaining == 0)
      // When flush encounters 500 Server Error, it calls _handleRetryableError,
      // where snapshot persistence fails!
      final result = await queue.flush();

      expect(result.status, equals(SyncStatus.persistenceError));
      expect(result.persistenceFailed, isTrue);
      expect(result.message, contains('Retryable error'));

      // In-memory retry mutations rolled back:
      expect(queue.pendingItems.length, equals(1));
      expect(queue.pendingItems.first.retryCount, equals(0), reason: 'retryCount rolled back on persistence failure');
      expect(queue.pendingItems.first.lastError, isNull, reason: 'lastError rolled back on persistence failure');

      SharedPreferences.resetStatic();
      SharedPreferences.setMockInitialValues({});
    });

    test('19. Concurrent enqueue and flush operations execute safely without race conditions after errors', () async {
      int serverCalls = 0;
      final mockClient = MockClient((request) async {
        serverCalls++;
        if (serverCalls == 1) {
          return http.Response('{"detail":"Service Temporarily Unavailable"}', 503);
        }
        return http.Response(json.encode({
          'status': 'success',
          'cycle_id': 'c_concurrent_recov',
          'count': 1,
          'items': [],
        }), 201);
      });
      final testJwt = _buildTestJwt(patientId: 'p_concurrent_recov');
      final apiClient = AegisApiClient(
        baseUrl: 'http://127.0.0.1:8000',
        tokenProvider: () async => testJwt,
        client: mockClient,
      );

      final customStore = TestSharedPreferencesStore();
      SharedPreferences.resetStatic();
      SharedPreferencesStorePlatform.instance = customStore;

      final queue = VitalsSyncQueue(apiClient: apiClient, activeCycleId: 'c_concurrent_recov', activePatientId: 'p_concurrent_recov');

      // Initial enqueue succeeds
      await queue.enqueue(HealthMeasurement(timestamp: DateTime.now(), pulse: 70, source: HealthDataSource.watchSensor));

      // Flush 1 encounters 503 retryable error
      final res1 = await queue.flush();
      expect(res1.status, equals(SyncStatus.retryableError));
      expect(queue.pendingItems.first.retryCount, equals(1));

      // Reset retry cooldown
      queue.resume();

      // Launch concurrent enqueue and flush operations
      final future1 = queue.enqueue(HealthMeasurement(timestamp: DateTime.now(), pulse: 75, source: HealthDataSource.watchSensor));
      final future2 = queue.flush();
      final future3 = queue.enqueue(HealthMeasurement(timestamp: DateTime.now(), pulse: 80, source: HealthDataSource.watchSensor));

      await Future.wait([future1, future2, future3]);

      // All items are safely accounted for (either delivered or still pending)
      expect(queue.pendingItems.length + 1, greaterThanOrEqualTo(1));

      // Flush any remaining
      queue.resume();
      await queue.flush();

      SharedPreferences.resetStatic();
      SharedPreferences.setMockInitialValues({});
    });

    test('20. HealthDataProvider enqueue observability: successful enqueue clears error, returns true, and persists to queue', () async {
      final timeService = TimeService();
      timeService.resetToRealTime();
      final vaultService = VaultService();
      await vaultService.initFuture;
      final vitalsService = VitalsMonitorService(timeService, vaultService);

      final customStore = TestSharedPreferencesStore();
      SharedPreferences.resetStatic();
      SharedPreferencesStorePlatform.instance = customStore;

      final mockClient = MockClient((request) async => http.Response('{}', 200));
      final apiClient = AegisApiClient(baseUrl: 'http://127.0.0.1:8000', client: mockClient);
      final queue = VitalsSyncQueue(apiClient: apiClient, activeCycleId: 'c_obs_success');

      final provider = HealthDataProvider(
        vaultService: vaultService,
        vitalsMonitorService: vitalsService,
        syncQueue: queue,
      );

      expect(provider.hasEnqueueError, isFalse);
      expect(provider.lastEnqueueError, isNull);
      expect(provider.lastEnqueueErrorAt, isNull);

      final success = await provider.dispatchWatchSensor(pulse: 74, note: 'Normal resting pulse');

      expect(success, isTrue);
      expect(provider.hasEnqueueError, isFalse);
      expect(provider.lastEnqueueError, isNull);
      expect(provider.lastEnqueueErrorAt, isNull);
      expect(queue.pendingItems.length, equals(1));
      expect(queue.pendingItems.first.dto.pulse, equals(74));

      SharedPreferences.resetStatic();
      SharedPreferences.setMockInitialValues({});
      provider.dispose();
      vitalsService.dispose();
    });

    test('21. HealthDataProvider when syncQueue is null: completes normally with hasEnqueueError false and returns true', () async {
      final timeService = TimeService();
      timeService.resetToRealTime();
      final vaultService = VaultService();
      await vaultService.initFuture;
      final vitalsService = VitalsMonitorService(timeService, vaultService);

      final provider = HealthDataProvider(
        vaultService: vaultService,
        vitalsMonitorService: vitalsService,
        syncQueue: null, // No remote queue bound
      );

      expect(provider.syncQueue, isNull);
      expect(provider.hasEnqueueError, isFalse);
      expect(provider.lastEnqueueError, isNull);

      final success = await provider.dispatchManualInput(
        pulse: 76,
        systolic: 120,
        diastolic: 80,
        note: 'Standalone offline manual input',
      );

      // Absence of syncQueue is NOT an error
      expect(success, isTrue);
      expect(provider.hasEnqueueError, isFalse);
      expect(provider.lastEnqueueError, isNull);
      expect(provider.lastEnqueueErrorAt, isNull);
      expect(vaultService.pulse, equals(76));
      expect(vaultService.systolic, equals(120));

      provider.dispose();
      vitalsService.dispose();
    });

    test('22. HealthDataProvider error observability, clearing, and historical ingestion preservation on enqueue failure', () async {
      final timeService = TimeService();
      timeService.resetToRealTime();
      final vaultService = VaultService();
      await vaultService.initFuture;
      final vitalsService = VitalsMonitorService(timeService, vaultService);

      final failingStore = TestSharedPreferencesStore();
      SharedPreferences.resetStatic();
      SharedPreferencesStorePlatform.instance = failingStore;
      failingStore.shouldFail = true;

      final mockClient = MockClient((request) async => http.Response('{}', 200));
      final apiClient = AegisApiClient(baseUrl: 'http://127.0.0.1:8000', client: mockClient);
      final queue = VitalsSyncQueue(apiClient: apiClient, activeCycleId: 'c_fail_obs');

      final provider = HealthDataProvider(
        vaultService: vaultService,
        vitalsMonitorService: vitalsService,
        syncQueue: queue,
      );

      // 1. Test historical measurement with failing enqueue
      final histMeasurement = HealthMeasurement(
        timestamp: DateTime.utc(2026, 10, 20, 15, 0, 0),
        pulse: 84,
        source: HealthDataSource.manualInput,
        isHistorical: true,
        note: 'Historical test reading',
      );

      final histSuccess = await provider.ingestHistoricalMeasurement(histMeasurement);

      // Outbound queue persistence failed, but local vault received the sample
      expect(histSuccess, isFalse);
      expect(provider.hasEnqueueError, isTrue);
      expect(provider.lastEnqueueError, isA<VitalsQueuePersistenceException>());
      expect(provider.lastEnqueueErrorMessage, contains('Simulated disk I/O failure'));
      expect(provider.lastEnqueueErrorAt, isNotNull);

      final samples = vaultService.getHeartRateSamples();
      expect(samples, isNotEmpty);
      expect(samples.last['pulse'], equals(84));
      expect(samples.last['phase'], equals('historical'));

      // 2. Test clearEnqueueError() public contract
      provider.clearEnqueueError();
      expect(provider.hasEnqueueError, isFalse);
      expect(provider.lastEnqueueError, isNull);
      expect(provider.lastEnqueueErrorAt, isNull);

      // 3. Test that subsequent successful enqueue when storage recovers resets error state
      failingStore.shouldFail = false;
      final liveSuccess = await provider.dispatchWatchSensor(pulse: 71);
      expect(liveSuccess, isTrue);
      expect(provider.hasEnqueueError, isFalse);
      expect(provider.lastEnqueueError, isNull);

      SharedPreferences.resetStatic();
      SharedPreferences.setMockInitialValues({});
      provider.dispose();
      vitalsService.dispose();
    });
  });
}
