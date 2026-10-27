import 'dart:async';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:aegis_health/services/health_data_provider.dart';
import 'package:aegis_health/services/vault_service.dart';
import 'package:aegis_health/services/vitals_monitor_service.dart';
import 'package:aegis_health/services/time_service.dart';
import 'package:aegis_health/services/athena_trigger_service.dart';
import 'package:aegis_health/services/aegis_cascade_controller.dart';
import 'dart:convert';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:aegis_health/services/api/aegis_api_client.dart';
import 'package:aegis_health/services/sync/vitals_sync_queue.dart';
import 'package:aegis_health/services/auth/auth_session_manager.dart';
import 'package:aegis_health/services/auth/auth_token_storage.dart';
import 'package:aegis_health/services/auth/auth_models.dart';


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

  late TimeService timeService;
  late VaultService vaultService;
  late VitalsMonitorService vitalsService;
  late HealthDataProvider healthDataProvider;

  setUp(() async {
    AuthSessionManager.resetActiveInstancesForTesting();
    SharedPreferences.setMockInitialValues({});
    timeService = TimeService();
    timeService.resetToRealTime();
    vaultService = VaultService();
    await vaultService.initFuture;
    vitalsService = VitalsMonitorService(timeService, vaultService);

    healthDataProvider = HealthDataProvider(
      vaultService: vaultService,
      vitalsMonitorService: vitalsService,
    );

    vaultService.saveVitalsThresholds({
      'min_pessimum_bpm': 50,
      'max_pessimum_bpm': 130,
      'max_spike_delta_bpm': 35,
      'clinical_rationale': 'Test safety thresholds',
    });
  });

  tearDown(() {
    healthDataProvider.dispose();
    vitalsService.dispose();
    AuthSessionManager.resetActiveInstancesForTesting();
  });

  test('1. Explicit source tagging: manual_input is never tagged as watch_sensor', () async {
    await healthDataProvider.dispatchManualInput(
      pulse: 78,
      systolic: 125,
      diastolic: 82,
      spo2: 98,
      note: 'User typed in modal',
    );

    final latest = healthDataProvider.latestMeasurement;
    expect(latest, isNotNull);
    expect(latest!.source, HealthDataSource.manualInput);
    expect(latest.source.rawValue, 'manual_input');
    expect(latest.isSimulated, isFalse);

    // Verify Vault metrics updated directly without duplicate storage
    expect(vaultService.systolic, 125);
    expect(vaultService.diastolic, 82);
    expect(vaultService.pulse, 78);
    expect(vaultService.spo2, 98);
  });

  test('2. Sources test_chip, simulated, watch_sensor, and unknown are strictly distinguished', () async {
    // 1. Test chip
    await healthDataProvider.dispatchTestChip(pulse: 145, note: 'Quick chip tap');
    expect(healthDataProvider.latestMeasurement!.source, HealthDataSource.testChip);
    expect(healthDataProvider.latestMeasurement!.source.rawValue, 'test_chip');
    expect(healthDataProvider.latestMeasurement!.isSimulated, isTrue);

    // 2. Simulated
    await healthDataProvider.dispatchSimulated(pulse: 110, note: 'Virtual progression');
    expect(healthDataProvider.latestMeasurement!.source, HealthDataSource.simulated);
    expect(healthDataProvider.latestMeasurement!.source.rawValue, 'simulated');
    expect(healthDataProvider.latestMeasurement!.isSimulated, isTrue);

    // 3. Watch sensor
    await healthDataProvider.dispatchWatchSensor(pulse: 72, note: 'Hardware telemetry');
    expect(healthDataProvider.latestMeasurement!.source, HealthDataSource.watchSensor);
    expect(healthDataProvider.latestMeasurement!.source.rawValue, 'watch_sensor');
    expect(healthDataProvider.latestMeasurement!.isSimulated, isFalse);

    // 4. Unknown / invalid source is never defaulted to watch_sensor
    final unknownSource = HealthDataSource.fromString('arbitrary_unknown_protocol');
    expect(unknownSource, HealthDataSource.unknown);
    expect(unknownSource.rawValue, 'unknown');
  });

  test('3. No duplicate storage: routes into existing SmartWatch Vault and VitalsMonitorService', () async {
    // Put system in workout mode
    await healthDataProvider.dispatchWatchSensor(pulse: 70);
    await healthDataProvider.dispatchWatchSensor(pulse: 70);
    await healthDataProvider.dispatchWatchSensor(pulse: 135);

    expect(vitalsService.currentState, AlertState.waitingUserResponse);

    await vitalsService.handleUserActivityResponse(
      isActivity: true,
      athenaTriggerService: AthenaTriggerService(),
      cascadeController: AegisCascadeController(vaultService: vaultService),
    );

    // Dispatch pulse via HealthDataProvider
    await healthDataProvider.dispatchWatchSensor(pulse: 148);

    // Verify sample recorded in existing SmartWatch Vault
    final samples = vaultService.getHeartRateSamples();
    expect(samples, isNotEmpty);
    final lastSample = samples.last;
    expect(lastSample['pulse'], 148);
    expect(lastSample['source'], 'watch_sensor');

    final sessions = vaultService.getWorkoutSessions();
    expect(sessions, hasLength(1));
    expect(sessions.first['workout_samples'], isNotEmpty);
  });

  test('4. Asynchronous smartwatch stream compatibility', () async {
    final StreamController<HealthMeasurement> sensorStreamController =
        StreamController<HealthMeasurement>();

    healthDataProvider.bindSensorStream(sensorStreamController.stream);
    expect(healthDataProvider.isSensorStreamBound, isTrue);

    final List<HealthMeasurement> receivedOnBroadcast = [];
    final subscription = healthDataProvider.vitalsStream.listen((m) {
      receivedOnBroadcast.add(m);
    });

    // Feed baseline readings through the async stream
    sensorStreamController.add(HealthMeasurement(
      timestamp: DateTime.now(),
      pulse: 68,
      source: HealthDataSource.watchSensor,
    ));
    sensorStreamController.add(HealthMeasurement(
      timestamp: DateTime.now(),
      pulse: 70,
      source: HealthDataSource.watchSensor,
    ));

    await Future.delayed(const Duration(milliseconds: 20));
    expect(vitalsService.currentState, AlertState.idle);

    // Feed spike reading through the async stream
    sensorStreamController.add(HealthMeasurement(
      timestamp: DateTime.now(),
      pulse: 142,
      source: HealthDataSource.watchSensor,
    ));

    await Future.delayed(const Duration(milliseconds: 20));

    expect(vitalsService.currentState, AlertState.waitingUserResponse);
    expect(vitalsService.currentPulse, 142);
    expect(receivedOnBroadcast.length, 3);

    await subscription.cancel();
    await sensorStreamController.close();
  });

  test('5. Historical batch ingestion does NOT trigger current anomaly alarms or create workouts', () async {
    expect(vitalsService.currentState, AlertState.idle);

    final pastTime = DateTime.now().subtract(const Duration(hours: 3));
    final historicalBatch = [
      HealthMeasurement(
        timestamp: pastTime,
        pulse: 72,
        source: HealthDataSource.watchSensor,
        isHistorical: true,
      ),
      HealthMeasurement(
        timestamp: pastTime.add(const Duration(minutes: 1)),
        pulse: 155, // Spike in historical data!
        source: HealthDataSource.watchSensor,
        isHistorical: true,
      ),
      HealthMeasurement(
        timestamp: pastTime.add(const Duration(minutes: 2)),
        pulse: 160, // Another spike in historical data!
        source: HealthDataSource.watchSensor,
        isHistorical: true,
      ),
    ];

    await healthDataProvider.ingestBatch(historicalBatch, asHistorical: true);

    // 1. VitalsMonitorService remains in idle state (no tachycardia alert triggered!)
    expect(vitalsService.currentState, AlertState.idle);

    // 2. No workout session is created from imported historical data
    expect(vaultService.getWorkoutSessions(), isEmpty);

    // 3. Samples are preserved in Vault under phase 'historical' for medical review
    final samples = vaultService.getHeartRateSamples();
    expect(samples, hasLength(3));
    expect(samples[1]['pulse'], 155);
    expect(samples[1]['phase'], 'historical');
    expect(samples[1]['timestamp'], pastTime.add(const Duration(minutes: 1)).toIso8601String());
  });

  test('6. Timestamp propagation preserves original measurement timestamp vs ingested_at', () async {
    final measuredAt = DateTime(2026, 10, 15, 14, 30, 0); // Past measurement time

    final measurement = HealthMeasurement(
      timestamp: measuredAt,
      pulse: 88,
      source: HealthDataSource.manualInput,
      note: 'Doctor recorded yesterday',
    );

    await healthDataProvider.ingestMeasurement(measurement);

    final samples = vaultService.getHeartRateSamples();
    expect(samples, isNotEmpty);
    final savedSample = samples.last;

    // Measurement timestamp matches exactly measuredAt
    expect(savedSample['timestamp'], measuredAt.toIso8601String());

    // Ingested_at is recorded separately and reflects current ingestion
    expect(savedSample['ingested_at'], isNotNull);
    final ingestedTime = DateTime.parse(savedSample['ingested_at'] as String);
    expect(ingestedTime.year, DateTime.now().year);
    expect(savedSample['timestamp'], isNot(savedSample['ingested_at']));
  });

  test('7. Repeated bindSensorStream cancels old subscription and prevents duplicate event processing', () async {
    final streamController1 = StreamController<HealthMeasurement>.broadcast();
    final streamController2 = StreamController<HealthMeasurement>.broadcast();

    int receivedEventsCount = 0;
    final sub = healthDataProvider.vitalsStream.listen((_) {
      receivedEventsCount++;
    });

    // 1. Bind first stream
    healthDataProvider.bindSensorStream(streamController1.stream);
    streamController1.add(HealthMeasurement(
      timestamp: DateTime.now(),
      pulse: 70,
      source: HealthDataSource.watchSensor,
    ));
    await Future.delayed(const Duration(milliseconds: 20));
    expect(receivedEventsCount, 1);

    // 2. Re-bind to second stream (must cancel subscription to stream 1)
    healthDataProvider.bindSensorStream(streamController2.stream);

    // Event on old stream 1 must be ignored
    streamController1.add(HealthMeasurement(
      timestamp: DateTime.now(),
      pulse: 72,
      source: HealthDataSource.watchSensor,
    ));
    await Future.delayed(const Duration(milliseconds: 20));
    expect(receivedEventsCount, 1); // Still 1! Old stream was successfully unsubscribed

    // Event on new stream 2 is received exactly once
    streamController2.add(HealthMeasurement(
      timestamp: DateTime.now(),
      pulse: 74,
      source: HealthDataSource.watchSensor,
    ));
    await Future.delayed(const Duration(milliseconds: 20));
    expect(receivedEventsCount, 2);

    await sub.cancel();
    await streamController1.close();
    await streamController2.close();
  });

  // =========================================================================
  // Stage 5.3C-3B-2B: HealthDataProvider & VitalsSyncQueue Integration (A-G)
  // =========================================================================
  group('Stage 5.3C-3B-2B: HealthDataProvider & VitalsSyncQueue Integration', () {
    setUp(() {
      AuthSessionManager.resetActiveInstancesForTesting();
    });

    tearDown(() {
      AuthSessionManager.resetActiveInstancesForTesting();
    });

    test('A. PendingCount: In dual-patient queue, syncPendingCount reflects only active patient items', () async {
      final jwtA = _buildTestJwt(patientId: 'patient_A', deviceId: 'device_A');
      final jwtB = _buildTestJwt(patientId: 'patient_B', deviceId: 'device_A');

      final mockClient = MockClient((request) async {
        if (request.url.path == '/api/v1/cycle/current') {
          return http.Response(json.encode({
            'cycle_id': 'cycle_shared',
            'status': 'active',
            'started_at': DateTime.now().toUtc().toIso8601String(),
          }), 200);
        }
        return http.Response('Not Found', 404);
      });

      final apiClient = AegisApiClient(baseUrl: 'http://127.0.0.1:8000', client: mockClient);
      final storage = InMemoryAuthTokenStorage();
      final sessionA = AuthSession(
        accessToken: jwtA,
        refreshToken: 'rt_A',
        tokenType: 'Bearer',
        expiresIn: 3600,
        expiresAt: DateTime.now().add(const Duration(hours: 1)),
        refreshExpiresAt: DateTime.now().add(const Duration(days: 30)),
        deviceId: 'device_A',
        patientId: 'patient_A',
        scope: 'vitals:write vitals:read',
      );
      await storage.saveSession(sessionA);
      final manager = AuthSessionManager(apiClient: apiClient, storage: storage);
      await manager.initialize();

      final queue = VitalsSyncQueue(
        apiClient: apiClient,
        authSessionManager: manager,
        enforcePatientIsolation: true,
      );
      await queue.init();

      final provider = HealthDataProvider(
        vaultService: vaultService,
        vitalsMonitorService: vitalsService,
        syncQueue: queue,
      );

      // Ingest measurement for Patient A
      await provider.ingestMeasurement(HealthMeasurement(
        timestamp: DateTime.now(),
        pulse: 72,
        source: HealthDataSource.watchSensor,
      ));

      expect(provider.syncPendingCount, equals(1));
      expect(provider.syncTotalPendingCount, equals(1));
      expect(provider.activePatientId, equals('patient_A'));

      // Switch session to Patient B
      final sessionB = AuthSession(
        accessToken: jwtB,
        refreshToken: 'rt_B',
        tokenType: 'Bearer',
        expiresIn: 3600,
        expiresAt: DateTime.now().add(const Duration(hours: 1)),
        refreshExpiresAt: DateTime.now().add(const Duration(days: 30)),
        deviceId: 'device_A',
        patientId: 'patient_B',
        scope: 'vitals:write vitals:read',
      );
      await storage.saveSession(sessionB);
      manager.setSessionForTesting(sessionB, status: AuthSessionStatus.authenticated);

      // Ingest measurement for Patient B
      await provider.ingestMeasurement(HealthMeasurement(
        timestamp: DateTime.now(),
        pulse: 85,
        source: HealthDataSource.watchSensor,
      ));

      // Verification: syncPendingCount strictly counts Patient B only
      expect(provider.syncPendingCount, equals(1));
      // Diagnostic total includes both A and B
      expect(provider.syncTotalPendingCount, equals(2));
      expect(provider.activePatientId, equals('patient_B'));

      provider.dispose();
      queue.dispose();
      manager.dispose();
      apiClient.close();
    });

    test('B. QuarantineCount: Revocation sends subsequent measurements to quarantine and updates syncQuarantinedCount', () async {
      final jwtB = _buildTestJwt(patientId: 'patient_B_user', deviceId: 'device_B');
      final mockClient = MockClient((request) async => http.Response('Not Found', 404));
      final apiClient = AegisApiClient(baseUrl: 'http://127.0.0.1:8000', client: mockClient);
      final storage = InMemoryAuthTokenStorage();
      final sessionB = AuthSession(
        accessToken: jwtB,
        refreshToken: 'rt_B',
        tokenType: 'Bearer',
        expiresIn: 3600,
        expiresAt: DateTime.now().add(const Duration(hours: 1)),
        refreshExpiresAt: DateTime.now().add(const Duration(days: 30)),
        deviceId: 'device_B',
        patientId: 'patient_B_user',
        scope: 'vitals:write vitals:read',
      );
      await storage.saveSession(sessionB);
      final manager = AuthSessionManager(apiClient: apiClient, storage: storage);
      await manager.initialize();

      final queue = VitalsSyncQueue(
        apiClient: apiClient,
        authSessionManager: manager,
        enforcePatientIsolation: true,
      );
      await queue.init();

      final provider = HealthDataProvider(
        vaultService: vaultService,
        vitalsMonitorService: vitalsService,
        syncQueue: queue,
      );
      expect(provider.syncQuarantinedCount, equals(0));

      // Revoke session -> unauthenticated
      manager.setSessionForTesting(null, status: AuthSessionStatus.unauthenticated);
      expect(provider.activePatientId, isNull);

      // Ingest measurement while unauthenticated
      final success = await provider.ingestMeasurement(HealthMeasurement(
        timestamp: DateTime.now(),
        pulse: 78,
        source: HealthDataSource.watchSensor,
      ));

      expect(success, isTrue); // offline-first succeeds
      expect(provider.syncQuarantinedCount, equals(1));
      expect(provider.syncPendingCount, equals(0));
      expect(provider.syncPauseReason, contains('Awaiting authorization'));

      provider.dispose();
      queue.dispose();
      manager.dispose();
      apiClient.close();
    });

    test('C. Queue Notification: Successful flush updates counters on HealthDataProvider without new ingest', () async {
      final jwtC = _buildTestJwt(patientId: 'patient_C', deviceId: 'device_C');
      final mockClient = MockClient((request) async {
        if (request.url.path == '/api/v1/cycle/current') {
          return http.Response(json.encode({
            'cycle_id': 'cycle_C',
            'patient_id': 'patient_C',
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

      final apiClient = AegisApiClient(baseUrl: 'http://127.0.0.1:8000', client: mockClient);
      final storage = InMemoryAuthTokenStorage();
      final sessionC = AuthSession(
        accessToken: jwtC,
        refreshToken: 'rt_C',
        tokenType: 'Bearer',
        expiresIn: 3600,
        expiresAt: DateTime.now().add(const Duration(hours: 1)),
        refreshExpiresAt: DateTime.now().add(const Duration(days: 30)),
        deviceId: 'device_C',
        patientId: 'patient_C',
        scope: 'vitals:write vitals:read',
      );
      await storage.saveSession(sessionC);
      final manager = AuthSessionManager(apiClient: apiClient, storage: storage);
      await manager.initialize();

      final queue = VitalsSyncQueue(
        apiClient: apiClient,
        authSessionManager: manager,
        enforcePatientIsolation: true,
      );
      await queue.init();
      await queue.resolveActiveCycle();

      final provider = HealthDataProvider(
        vaultService: vaultService,
        vitalsMonitorService: vitalsService,
        syncQueue: queue,
      );

      await provider.ingestMeasurement(HealthMeasurement(
        timestamp: DateTime.now(),
        pulse: 74,
        source: HealthDataSource.watchSensor,
      ));
      expect(provider.syncPendingCount, equals(1));

      int providerNotificationCount = 0;
      provider.addListener(() => providerNotificationCount++);

      // Flush directly on queue
      final flushResult = await queue.flush();
      expect(flushResult.status, equals(SyncStatus.success));

      // Verification: provider pendingCount is now 0 and provider received notification
      expect(provider.syncPendingCount, equals(0));
      expect(providerNotificationCount, greaterThan(0));

      provider.dispose();
      queue.dispose();
      manager.dispose();
      apiClient.close();
    });

    test('D. Patient Switch: A -> B updates activePatientId and visible indicators', () async {
      final jwtD1 = _buildTestJwt(patientId: 'patient_D1', deviceId: 'device_D');
      final jwtD2 = _buildTestJwt(patientId: 'patient_D2', deviceId: 'device_D');

      final mockClient = MockClient((request) async => http.Response('Not Found', 404));
      final apiClient = AegisApiClient(baseUrl: 'http://127.0.0.1:8000', client: mockClient);
      final storage = InMemoryAuthTokenStorage();
      final sessionD1 = AuthSession(
        accessToken: jwtD1,
        refreshToken: 'rt_D1',
        tokenType: 'Bearer',
        expiresIn: 3600,
        expiresAt: DateTime.now().add(const Duration(hours: 1)),
        refreshExpiresAt: DateTime.now().add(const Duration(days: 30)),
        deviceId: 'device_D',
        patientId: 'patient_D1',
        scope: 'vitals:write vitals:read',
      );
      await storage.saveSession(sessionD1);
      final manager = AuthSessionManager(apiClient: apiClient, storage: storage);
      await manager.initialize();

      final queue = VitalsSyncQueue(
        apiClient: apiClient,
        authSessionManager: manager,
        enforcePatientIsolation: true,
      );
      await queue.init();

      final provider = HealthDataProvider(
        vaultService: vaultService,
        vitalsMonitorService: vitalsService,
        syncQueue: queue,
      );

      await provider.ingestMeasurement(HealthMeasurement(
        timestamp: DateTime.now(),
        pulse: 70,
        source: HealthDataSource.watchSensor,
      ));
      await provider.ingestMeasurement(HealthMeasurement(
        timestamp: DateTime.now(),
        pulse: 71,
        source: HealthDataSource.watchSensor,
      ));

      expect(provider.activePatientId, equals('patient_D1'));
      expect(provider.syncPendingCount, equals(2));

      int notifyCount = 0;
      provider.addListener(() => notifyCount++);

      // Switch patient
      final sessionD2 = AuthSession(
        accessToken: jwtD2,
        refreshToken: 'rt_D2',
        tokenType: 'Bearer',
        expiresIn: 3600,
        expiresAt: DateTime.now().add(const Duration(hours: 1)),
        refreshExpiresAt: DateTime.now().add(const Duration(days: 30)),
        deviceId: 'device_D',
        patientId: 'patient_D2',
        scope: 'vitals:write vitals:read',
      );
      await storage.saveSession(sessionD2);
      manager.setSessionForTesting(sessionD2, status: AuthSessionStatus.authenticated);

      // Verification: provider notified, patient updated, pending count reflects new patient
      expect(notifyCount, greaterThan(0));
      expect(provider.activePatientId, equals('patient_D2'));
      expect(provider.syncPendingCount, equals(0));
      expect(provider.syncTotalPendingCount, equals(2));

      provider.dispose();
      queue.dispose();
      manager.dispose();
      apiClient.close();
    });

    test('E. Dispose: Disposed HealthDataProvider removes queue listener and receives no notifications', () async {
      final mockClient = MockClient((request) async => http.Response('Not Found', 404));
      final apiClient = AegisApiClient(baseUrl: 'http://127.0.0.1:8000', client: mockClient);
      final queue = VitalsSyncQueue(apiClient: apiClient);
      await queue.init();

      final provider = HealthDataProvider(
        vaultService: vaultService,
        vitalsMonitorService: vitalsService,
        syncQueue: queue,
      );

      int notificationCount = 0;
      provider.addListener(() => notificationCount++);

      provider.dispose();

      // Queue notifies listeners
      await queue.enqueue(HealthMeasurement(
        timestamp: DateTime.now(),
        pulse: 75,
        source: HealthDataSource.watchSensor,
      ));

      // Disposed provider must not trigger listener or throw
      expect(notificationCount, equals(0));

      queue.dispose();
      apiClient.close();
    });

    test('F. Queue Rebinding: Replacing queue detaches old listener and attaches new listener', () async {
      final mockClient = MockClient((request) async => http.Response('Not Found', 404));
      final client1 = AegisApiClient(baseUrl: 'http://127.0.0.1:8000', client: mockClient);
      final client2 = AegisApiClient(baseUrl: 'http://127.0.0.1:8000', client: mockClient);

      final queue1 = VitalsSyncQueue(apiClient: client1);
      await queue1.init();
      final queue2 = VitalsSyncQueue(apiClient: client2);
      await queue2.init();

      final provider = HealthDataProvider(
        vaultService: vaultService,
        vitalsMonitorService: vitalsService,
        syncQueue: queue1,
      );

      int notificationCount = 0;
      provider.addListener(() => notificationCount++);

      // Re-bind to queue2 via updateDependencies
      provider.updateDependencies(
        vaultService: vaultService,
        vitalsMonitorService: vitalsService,
        syncQueue: queue2,
      );

      // Re-binding notifies provider listeners
      final countAfterRebind = notificationCount;
      expect(countAfterRebind, greaterThan(0));

      // queue1 notification must NOT affect provider
      await queue1.enqueue(HealthMeasurement(
        timestamp: DateTime.now(),
        pulse: 65,
        source: HealthDataSource.watchSensor,
      ));
      expect(notificationCount, equals(countAfterRebind));

      // queue2 notification MUST affect provider
      await queue2.enqueue(HealthMeasurement(
        timestamp: DateTime.now(),
        pulse: 75,
        source: HealthDataSource.watchSensor,
      ));
      expect(notificationCount, greaterThan(countAfterRebind));

      provider.dispose();
      queue1.dispose();
      queue2.dispose();
      client1.close();
      client2.close();
    });

    test('G. Offline: Without auth, measurements persist to VaultService and VitalsMonitorService with 0 network calls', () async {
      int httpCalls = 0;
      final mockClient = MockClient((request) async {
        httpCalls++;
        return http.Response('Unexpected request', 500);
      });
      final apiClient = AegisApiClient(baseUrl: 'http://127.0.0.1:8000', client: mockClient);
      final storage = InMemoryAuthTokenStorage();
      final manager = AuthSessionManager(apiClient: apiClient, storage: storage);

      final queue = VitalsSyncQueue(
        apiClient: apiClient,
        authSessionManager: manager,
        enforcePatientIsolation: true,
      );
      await queue.init();

      final provider = HealthDataProvider(
        vaultService: vaultService,
        vitalsMonitorService: vitalsService,
        syncQueue: queue,
      );

      expect(provider.activePatientId, isNull);
      expect(provider.isSyncPaused, isTrue);

      final initialVaultCount = vaultService.getHeartRateSamples().length;

      // Ingest measurement without session
      final measurement = HealthMeasurement(
        timestamp: DateTime.now(),
        pulse: 88,
        systolic: 125,
        diastolic: 82,
        source: HealthDataSource.manualInput,
      );
      final success = await provider.ingestMeasurement(measurement);

      // Verification:
      // 1. Ingestion succeeds
      expect(success, isTrue);
      // 2. Persisted to Vault
      expect(vaultService.getHeartRateSamples().length, equals(initialVaultCount + 1));
      expect(vaultService.systolic, equals(125));
      expect(vaultService.diastolic, equals(82));
      // 3. Quarantined in queue (R1)
      expect(provider.syncQuarantinedCount, equals(1));
      expect(provider.syncPendingCount, equals(0));
      // 4. Zero HTTP calls executed
      expect(httpCalls, equals(0));

      provider.dispose();
      queue.dispose();
      manager.dispose();
      apiClient.close();
    });
  });
}
