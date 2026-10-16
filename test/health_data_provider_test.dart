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
    // Dispatch manual input
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

  test('2. Sources test_chip, simulated, and watch_sensor are strictly distinguished', () async {
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

    // Bind external async stream to HealthDataProvider
    healthDataProvider.bindSensorStream(sensorStreamController.stream);

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

    // Yield to event loop to allow async stream events to process
    await Future.delayed(const Duration(milliseconds: 20));
    expect(vitalsService.currentState, AlertState.idle);

    // Feed spike reading through the async stream
    sensorStreamController.add(HealthMeasurement(
      timestamp: DateTime.now(),
      pulse: 142,
      source: HealthDataSource.watchSensor,
    ));

    await Future.delayed(const Duration(milliseconds: 20));

    // Verify VitalsMonitorService intercepted the spike from async stream
    expect(vitalsService.currentState, AlertState.waitingUserResponse);
    expect(vitalsService.currentPulse, 142);
    expect(receivedOnBroadcast.length, 3);

    await subscription.cancel();
    await sensorStreamController.close();
  });

  test('5. Batch ingestion processes historical readings in sequence', () async {
    final now = DateTime.now();
    final batch = [
      HealthMeasurement(
        timestamp: now.subtract(const Duration(minutes: 2)),
        pulse: 71,
        source: HealthDataSource.watchSensor,
      ),
      HealthMeasurement(
        timestamp: now.subtract(const Duration(minutes: 1)),
        pulse: 73,
        source: HealthDataSource.watchSensor,
      ),
      HealthMeasurement(
        timestamp: now,
        pulse: 75,
        source: HealthDataSource.watchSensor,
      ),
    ];

    await healthDataProvider.ingestBatch(batch);

    expect(healthDataProvider.latestMeasurement!.pulse, 75);
    expect(vitalsService.currentPulse, 75);
  });
}
