import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:aegis_health/services/vitals_monitor_service.dart';
import 'package:aegis_health/services/time_service.dart';
import 'package:aegis_health/services/vault_service.dart';
import 'package:aegis_health/services/athena_trigger_service.dart';
import 'package:aegis_health/services/aegis_cascade_controller.dart';

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  late TimeService timeService;
  late VaultService vaultService;
  late VitalsMonitorService vitalsService;

  setUp(() async {
    SharedPreferences.setMockInitialValues({});
    timeService = TimeService();
    timeService.resetToRealTime();
    vaultService = VaultService();
    await vaultService.initFuture;
    vitalsService = VitalsMonitorService(timeService, vaultService);

    // Save default doctor thresholds into vault for testing
    vaultService.saveVitalsThresholds({
      'min_pessimum_bpm': 50,
      'max_pessimum_bpm': 130,
      'max_spike_delta_bpm': 35,
      'clinical_rationale': 'Test thresholds',
    });
  });

  tearDown(() {
    vitalsService.dispose();
  });

  test('Workout flow: Spike -> Confirmation -> Workout Active -> Finish -> Post-Workout Intervals', () async {
    // 1. Initial idle state
    expect(vitalsService.currentState, AlertState.idle);

    // 2. Feed baseline pulse readings (need 3 readings)
    vitalsService.onNewPulseRead(70);
    vitalsService.onNewPulseRead(72);
    expect(vitalsService.currentState, AlertState.idle);

    // 3. Trigger pulse spike (135 bpm, jump > 35 delta)
    final alertTime = timeService.now;
    vitalsService.onNewPulseRead(135);

    expect(vitalsService.currentState, AlertState.waitingUserResponse);
    expect(vitalsService.workoutStartTime, isNotNull);
    expect(vitalsService.workoutStartTime!.difference(alertTime).inSeconds, 0);

    // 4. User confirms physical activity
    final athenaTrigger = AthenaTriggerService();
    final cascadeController = AegisCascadeController(vaultService: vaultService);

    await vitalsService.handleUserActivityResponse(
      isActivity: true,
      athenaTriggerService: athenaTrigger,
      cascadeController: cascadeController,
    );

    expect(vitalsService.currentState, AlertState.workoutActive);
    expect(vitalsService.getRequiredIntervalMinutes(), 1);

    // 5. Simulate time passing and check missed samples, plus provide a simulated test reading
    timeService.fastForward(const Duration(minutes: 3));
    final session = vaultService.getWorkoutSession(vitalsService.currentSessionId!);
    expect(session, isNotNull);
    final missed = session!['missed_samples'] as List;
    expect(missed, isNotEmpty);

    // Provide a simulated pulse reading
    vitalsService.onNewPulseRead(140, source: 'test_chip', isSimulated: true);
    final simulatedEntries = vitalsService.workoutPulseLog.where((e) => e.isSimulated);
    expect(simulatedEntries, isNotEmpty);
    expect(simulatedEntries.first.source, 'test_chip');

    // 6. User finishes workout
    vitalsService.finishWorkout();
    // Pulse cools down to normal post-workout recovery level
    vitalsService.onNewPulseRead(95);

    expect(vitalsService.currentState, AlertState.postWorkoutRecovery);
    expect(vitalsService.workoutEndTime, isNotNull);

    // 7. Verify post-workout interval schedule:
    // 0-10 min -> interval is 2 minutes
    expect(vitalsService.getRequiredIntervalMinutes(), 2);

    // Fast forward 15 minutes (now at 15m since end) -> interval is 5 minutes
    timeService.fastForward(const Duration(minutes: 15));
    expect(vitalsService.getRequiredIntervalMinutes(), 5);

    // Fast forward 40 minutes (now at 55m since end) -> interval is 10 minutes (Samsung Health standard)
    timeService.fastForward(const Duration(minutes: 40));
    expect(vitalsService.getRequiredIntervalMinutes(), 10);
  });

  test('Post-workout anomaly: Pulse >= 140 bpm restarts trigger cycle', () {
    // Put into post workout recovery
    vitalsService.onNewPulseRead(70);
    vitalsService.onNewPulseRead(70);
    vitalsService.onNewPulseRead(135);

    vitalsService.finishWorkout();
    expect(vitalsService.currentState, AlertState.postWorkoutRecovery);

    // Normal post-workout pulse
    vitalsService.onNewPulseRead(95);
    expect(vitalsService.currentState, AlertState.postWorkoutRecovery);

    // Anomaly: Sudden spike to 142 bpm
    vitalsService.onNewPulseRead(142);
    expect(vitalsService.currentState, AlertState.waitingUserResponse);
  });

  test('Critical anomaly: Pulse 0 for >= 2 minutes triggers critical anomaly', () {
    vitalsService.onNewPulseRead(75);
    expect(vitalsService.currentState, AlertState.idle);

    // Pulse drops to 0
    vitalsService.onNewPulseRead(0);
    expect(vitalsService.currentState, AlertState.idle);

    // Fast forward 2 virtual minutes
    timeService.fastForward(const Duration(minutes: 2));
    expect(vitalsService.currentState, AlertState.criticalAnomaly);
    expect(vitalsService.criticalAnomalyReason, contains('0 уд/мин'));
  });
}
