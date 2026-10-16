import 'dart:async';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:aegis_health/services/health_data_provider.dart';
import 'package:aegis_health/services/vault_service.dart';
import 'package:aegis_health/services/vitals_monitor_service.dart';
import 'package:aegis_health/services/time_service.dart';
import 'package:aegis_health/services/athena_trigger_service.dart';
import 'package:aegis_health/services/aegis_cascade_controller.dart';

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  late TimeService timeService;
  late VaultService vaultService;
  late VitalsMonitorService vitalsService;
  late HealthDataProvider healthDataProvider;

  setUp(() async {
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
}
