import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:aegis_health/services/vault_service.dart';
import 'package:aegis_health/services/vault_storage_service.dart';
import 'package:aegis_health/services/vitals_monitor_service.dart';
import 'package:aegis_health/services/time_service.dart';
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

    vaultService.saveVitalsThresholds({
      'min_pessimum_bpm': 50,
      'max_pessimum_bpm': 130,
      'max_spike_delta_bpm': 35,
      'clinical_rationale': 'Test safety thresholds',
    });
  });

  tearDown(() {
    vitalsService.dispose();
  });

  test('1. Сохранение тренировки в smart_watch', () async {
    vitalsService.onNewPulseRead(70);
    vitalsService.onNewPulseRead(70);
    vitalsService.onNewPulseRead(135);

    expect(vitalsService.currentState, AlertState.waitingUserResponse);

    await vitalsService.handleUserActivityResponse(
      isActivity: true,
      athenaTriggerService: AthenaTriggerService(),
      cascadeController: AegisCascadeController(vaultService: vaultService),
    );

    final sessions = vaultService.getWorkoutSessions();
    expect(sessions, hasLength(1));
    final session = sessions.first;
    expect(session['session_id'], vitalsService.currentSessionId);
    expect(session['status'], 'active');
    expect(session['is_data_collection_finished'], isFalse);
    expect(session['anomaly_detected_at'], isNotNull);
    expect(session['activity_confirmed_at'], isNotNull);
    expect(session['workout_context'], isNotEmpty);
    expect(session['workout_samples'], isNotEmpty);
  });

  test('2. Завершение и физиологическое восстановление', () async {
    vitalsService.onNewPulseRead(70);
    vitalsService.onNewPulseRead(70);
    vitalsService.onNewPulseRead(135);

    await vitalsService.handleUserActivityResponse(
      isActivity: true,
      athenaTriggerService: AthenaTriggerService(),
      cascadeController: AegisCascadeController(vaultService: vaultService),
    );

    final sessionId = vitalsService.currentSessionId!;

    // Complete workout by user tap
    timeService.fastForward(const Duration(minutes: 25));
    vitalsService.finishWorkout();

    final sessionAfterTap = vaultService.getWorkoutSession(sessionId)!;
    expect(sessionAfterTap['workout_completed_at'], isNotNull);
    expect(sessionAfterTap['status'], 'recovering');
    expect(sessionAfterTap['physiological_end_time'], isNull);

    // Pulse returns to baseline recovery (75 <= 70 * 1.15 = 80.5)
    timeService.fastForward(const Duration(minutes: 6));
    vitalsService.onNewPulseRead(75);

    final sessionRecovered = vaultService.getWorkoutSession(sessionId)!;
    expect(sessionRecovered['physiological_end_time'], isNotNull);
    expect(sessionRecovered['physiological_end_time'], isNot(sessionRecovered['workout_completed_at']));
  });

  test('3. Сохранение журнала тренировок после reset() и resetVault()', () async {
    vitalsService.onNewPulseRead(70);
    vitalsService.onNewPulseRead(70);
    vitalsService.onNewPulseRead(135);

    await vitalsService.handleUserActivityResponse(
      isActivity: true,
      athenaTriggerService: AthenaTriggerService(),
      cascadeController: AegisCascadeController(vaultService: vaultService),
    );
    vitalsService.finishWorkout();

    expect(vaultService.getWorkoutSessions(), hasLength(1));
    expect(vitalsService.workoutPulseLog, isNotEmpty);

    // Call vitalsService.reset()
    vitalsService.reset();
    expect(vitalsService.currentState, AlertState.idle);
    expect(vaultService.getWorkoutSessions(), hasLength(1));
    expect(vitalsService.workoutPulseLog, isNotEmpty);

    // Call vaultService.resetVault()
    await vaultService.resetVault(keepWorkoutHistory: true);
    expect(vaultService.getWorkoutSessions(), hasLength(1));
    expect(vaultService.getHeartRateSamples(), isNotEmpty);
  });

  test('4. Перезапуск приложения и восстановление истории из SharedPreferences', () async {
    vitalsService.onNewPulseRead(70);
    vitalsService.onNewPulseRead(70);
    vitalsService.onNewPulseRead(135);

    await vitalsService.handleUserActivityResponse(
      isActivity: true,
      athenaTriggerService: AthenaTriggerService(),
      cascadeController: AegisCascadeController(vaultService: vaultService),
    );
    vitalsService.onNewPulseRead(145);
    vitalsService.finishWorkout();

    final originalSessions = vaultService.getWorkoutSessions();
    final originalSamples = vaultService.getHeartRateSamples();
    expect(originalSessions, hasLength(1));

    // Simulate app reboot: new VaultService instance reading persistent storage
    final newVault = VaultService();
    // Allow async _initVault to complete
    await Future.delayed(const Duration(milliseconds: 100));

    final restoredSessions = newVault.getWorkoutSessions();
    final restoredSamples = newVault.getHeartRateSamples();

    expect(restoredSessions.length, originalSessions.length);
    expect(restoredSessions.first['session_id'], originalSessions.first['session_id']);
    expect(restoredSamples.length, originalSamples.length);
  });

  test('5. Несколько тренировок подряд (новая во время восстановления предыдущей)', () async {
    // Session 1
    vitalsService.onNewPulseRead(70);
    vitalsService.onNewPulseRead(70);
    vitalsService.onNewPulseRead(135);

    await vitalsService.handleUserActivityResponse(
      isActivity: true,
      athenaTriggerService: AthenaTriggerService(),
      cascadeController: AegisCascadeController(vaultService: vaultService),
    );
    final session1Id = vitalsService.currentSessionId!;
    vitalsService.finishWorkout();

    expect(vaultService.getWorkoutSession(session1Id)!['status'], 'recovering');

    // Sudden spike during recovery -> Starts Session 2
    timeService.fastForward(const Duration(minutes: 8));
    vitalsService.onNewPulseRead(142);

    expect(vitalsService.currentState, AlertState.waitingUserResponse);
    final session2Id = vitalsService.currentSessionId!;
    expect(session2Id, isNot(session1Id));

    await vitalsService.handleUserActivityResponse(
      isActivity: true,
      athenaTriggerService: AthenaTriggerService(),
      cascadeController: AegisCascadeController(vaultService: vaultService),
    );

    final allSessions = vaultService.getWorkoutSessions();
    expect(allSessions, hasLength(2));

    final session1 = vaultService.getWorkoutSession(session1Id)!;
    final session2 = vaultService.getWorkoutSession(session2Id)!;

    expect(session1['is_data_collection_finished'], isTrue);
    expect(session2['status'], 'active');
  });

  test('6. Пропущенные значения пульса (missed_samples без фабрикации синтетических точек)', () async {
    vitalsService.onNewPulseRead(70);
    vitalsService.onNewPulseRead(70);
    vitalsService.onNewPulseRead(135);

    await vitalsService.handleUserActivityResponse(
      isActivity: true,
      athenaTriggerService: AthenaTriggerService(),
      cascadeController: AegisCascadeController(vaultService: vaultService),
    );

    final sessionId = vitalsService.currentSessionId!;
    final samplesBeforeJump = vaultService.getHeartRateSamples(sessionId: sessionId).length;

    // Fast forward 10 minutes without sensor readings
    timeService.fastForward(const Duration(minutes: 10));

    // Confirm that 10 fake measurements were NOT added to samples
    final samplesAfterJump = vaultService.getHeartRateSamples(sessionId: sessionId).length;
    expect(samplesAfterJump, samplesBeforeJump);

    // Confirm that missed gap was logged in session's missed_samples
    final session = vaultService.getWorkoutSession(sessionId)!;
    final missed = session['missed_samples'] as List;
    expect(missed, isNotEmpty);
    expect(missed.first['missed_count'], 9);
    expect(missed.first['reason'], contains('time_jump'));
  });

  test('7. Симулированные измерения пульса (is_simulated flag)', () async {
    vitalsService.onNewPulseRead(70);
    vitalsService.onNewPulseRead(70);
    vitalsService.onNewPulseRead(135);

    await vitalsService.handleUserActivityResponse(
      isActivity: true,
      athenaTriggerService: AthenaTriggerService(),
      cascadeController: AegisCascadeController(vaultService: vaultService),
    );

    // Provide test chip reading (simulated)
    vitalsService.onNewPulseRead(145, source: 'test_chip', isSimulated: true);

    // Provide real sensor reading
    vitalsService.onNewPulseRead(130, source: 'watch_sensor', isSimulated: false);

    final samples = vaultService.getHeartRateSamples();
    final chipSample = samples.firstWhere((s) => s['source'] == 'test_chip');
    final sensorSample = samples.firstWhere((s) => s['source'] == 'watch_sensor');

    expect(chipSample['is_simulated'], isTrue);
    expect(sensorSample['is_simulated'], isFalse);
  });

  test('8. Совместимость со старым форматом Vault', () async {
    // Write an old format snapshot into storage (missing workout collections)
    final oldFormatSnapshot = {
      'health_systems': {
        'cvs': {
          'name': 'Cardiovascular',
          'completeness': 50.0,
          'color': '0xFFFF0000',
          'markers': {
            'smart_watch': {
              'pressure': {'systolic': 120, 'diastolic': 80, 'date': '2026-10-15'},
              'pulse': {'value': 72, 'date': '2026-10-15', 'unit': 'bpm'},
            }
          }
        }
      },
      'system_folders': {},
    };
    await VaultStorageService.saveVault(oldFormatSnapshot);

    final legacyVault = VaultService();
    await Future.delayed(const Duration(milliseconds: 100));

    // Must preserve old pressure & pulse
    expect(legacyVault.systolic, 120);
    expect(legacyVault.diastolic, 80);
    expect(legacyVault.pulse, 72);

    // Must safely initialize the 4 new collections without errors
    expect(legacyVault.getWorkoutSessions(), isEmpty);
    expect(legacyVault.getHeartRateSamples(), isEmpty);
    expect(legacyVault.getWorkoutContext(), isEmpty);
    expect(legacyVault.getWorkoutAnalysisReports(), isEmpty);
  });

  test('9. Подготовка для Workout Intelligence (3 независимых поля и версии отчетов)', () {
    vitalsService.onNewPulseRead(70);
    vitalsService.onNewPulseRead(70);
    vitalsService.onNewPulseRead(135);

    final sessionId = 'ws_test_intelligence';
    vaultService.createWorkoutSession(
      sessionId: sessionId,
      anomalyDetectedAt: DateTime.now(),
      activityConfirmedAt: DateTime.now(),
    );

    // Version 1 of report (with empty / null placeholders)
    vaultService.saveWorkoutReport(
      sessionId: sessionId,
      internalAnalysis: null,
      userWorkoutReport: null,
      athenaHandoff: null,
    );

    final v1 = vaultService.getLatestWorkoutReport(sessionId)!;
    expect(v1['version'], 1);
    expect(v1.containsKey('internal_analysis'), isTrue);
    expect(v1.containsKey('user_workout_report'), isTrue);
    expect(v1.containsKey('athena_handoff'), isTrue);

    // Version 2 of report without overwriting version 1
    vaultService.saveWorkoutReport(
      sessionId: sessionId,
      internalAnalysis: {'model': 'Gemini 3.1 Pro', 'score': 95},
      userWorkoutReport: {'summary': 'Great aerobic cardio workout'},
      athenaHandoff: {'advice': 'Recommend hydration and protein'},
    );

    final reports = vaultService.getWorkoutAnalysisReports(sessionId: sessionId);
    expect(reports, hasLength(2));

    final v2 = vaultService.getLatestWorkoutReport(sessionId)!;
    expect(v2['version'], 2);
    expect(v2['internal_analysis']?['model'], 'Gemini 3.1 Pro');
    expect(reports.first['version'], 1);
  });
}
