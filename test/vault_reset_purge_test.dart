import 'package:flutter_test/flutter_test.dart';

import 'package:aegis_health/services/api/medical_ai_api_models.dart';
import 'package:aegis_health/services/vault_service.dart';
import 'package:aegis_health/services/vault_storage_service.dart';

/// Helper to build all 8 system reports for weekly response
Map<String, ClientModelReportResponse> _sampleWeeklySystemReports({
  required String cycleId,
  required String sprintId,
  String reportDate = '2026-10-29',
}) {
  const systems = [
    'cvs',
    'gi_tract',
    'respiratory',
    'nervous',
    'musculoskeletal',
    'kidneys',
    'immune',
    'endocrine',
  ];

  final Map<String, ClientModelReportResponse> map = {};
  for (final sys in systems) {
    map[sys] = ClientModelReportResponse(
      reportId: 'rep_${cycleId}_${sprintId}_$sys',
      cycleId: cycleId,
      sprintId: sprintId,
      model: 'gemini-3.6-flash',
      reportType: 'system_user_report_$sys',
      privacyLevel: 'client_facing',
      version: 1,
      content: ClientReportContent(
        title: 'Weekly ${sys.toUpperCase()} Evaluation',
        summary: 'Weekly dynamics for $sys within normal range.',
        reportDate: reportDate,
        systemKey: sys,
        highlights: ['Stable condition.'],
      ),
      createdAt: DateTime.parse('${reportDate}T10:00:00Z'),
    );
  }
  return map;
}

/// Helper to build Weekly overall report
ClientModelReportResponse _sampleWeeklyOverallReport({
  required String cycleId,
  required String sprintId,
  int sprintNumber = 1,
  String reportDate = '2026-10-29',
}) {
  return ClientModelReportResponse(
    reportId: 'rep_${cycleId}_${sprintId}_overall',
    cycleId: cycleId,
    sprintId: sprintId,
    model: 'gemini-3.1-pro-preview',
    reportType: 'weekly_user_overall_report',
    privacyLevel: 'client_facing',
    version: sprintNumber,
    content: ClientReportContent(
      title: 'Weekly Sprint $sprintNumber Synthesis',
      summary: 'Week $sprintNumber monitoring stable.',
      reportDate: reportDate,
      highlights: ['Adherence is optimal.'],
      metrics: {'sprint_number': sprintNumber},
    ),
    createdAt: DateTime.parse('${reportDate}T10:00:00Z'),
  );
}

/// Helper to build Primary Consilium reports
List<ClientModelReportResponse> _samplePrimaryReports({
  required String cycleId,
  String reportDate = '2026-10-22',
}) {
  const systems = [
    'cvs',
    'gi_tract',
    'respiratory',
    'nervous',
    'musculoskeletal',
    'kidneys',
    'immune',
    'endocrine',
  ];

  final list = <ClientModelReportResponse>[
    ClientModelReportResponse(
      reportId: 'rep_${cycleId}_overall',
      cycleId: cycleId,
      model: 'gemini-3.1-pro-preview',
      reportType: 'user_overall_report',
      privacyLevel: 'client_facing',
      version: 1,
      content: ClientReportContent(
        title: 'Интегральное заключение первичного консилиума',
        summary: 'Базовый статус систем в норме.',
        reportDate: reportDate,
        highlights: ['Ритм синусовый', 'Давление целевое'],
        metrics: {'min_pessimum_bpm': 50, 'max_pessimum_bpm': 140, 'max_spike_delta_bpm': 30},
      ),
      createdAt: DateTime.parse('${reportDate}T08:00:00Z'),
    ),
  ];

  for (final sys in systems) {
    list.add(
      ClientModelReportResponse(
        reportId: 'rep_${cycleId}_$sys',
        cycleId: cycleId,
        model: 'gemini-3.6-flash',
        reportType: 'system_user_report_$sys',
        privacyLevel: 'client_facing',
        version: 1,
        content: ClientReportContent(
          title: 'Система $sys',
          summary: 'Норма',
          reportDate: reportDate,
          systemKey: sys,
        ),
        createdAt: DateTime.parse('${reportDate}T08:00:00Z'),
      ),
    );
  }

  return list;
}

/// Helper to build sample LabLedgerStateResponse
LabLedgerStateResponse _sampleLabLedger({required String cycleId}) {
  return LabLedgerStateResponse(
    cycleId: cycleId,
    ledgerVersion: 1,
    lastEvaluatedLedgerVersion: 1,
    hasUnreviewedLabChanges: false,
    evaluationStatus: 'completed',
    evaluationNotice: 'Evaluation completed',
    safetyNotice: 'All parameters normal',
    activeMarkers: {
      'glucose': const LabObservationEntry(
        observationId: 'obs_1',
        markerKey: 'glucose',
        displayName: 'Глюкоза',
        value: '5.4',
        numericValue: 5.4,
        unit: 'mmol/L',
        sampleDate: '2026-10-25',
        eventType: 'initial',
        isActive: true,
        recordedAt: '2026-10-25T10:00:00Z',
      ),
    },
    observationsHistory: [
      const LabObservationEntry(
        observationId: 'obs_1',
        markerKey: 'glucose',
        displayName: 'Глюкоза',
        value: '5.4',
        numericValue: 5.4,
        unit: 'mmol/L',
        sampleDate: '2026-10-25',
        eventType: 'initial',
        isActive: true,
        recordedAt: '2026-10-25T10:00:00Z',
      ),
    ],
  );
}

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  late InMemoryVaultStorage memoryStorage;

  setUp(() {
    VaultService.resetWriteQueueForTesting();
    memoryStorage = InMemoryVaultStorage();
    VaultStorageService.setStorageForTesting(memoryStorage);
  });

  tearDown(() {
    VaultService.resetWriteQueueForTesting();
    VaultStorageService.setStorageForTesting(null);
  });

  group('PROJECT AEGIS — ARGON BUG-09: VAULT RESET DATA PURGE', () {
    // -------------------------------------------------------------------------
    // 1. COMPLETE CLINICAL DATA REMOVAL FROM MEMORY
    // -------------------------------------------------------------------------
    test('1. resetVault completely purges all sensitive clinical state from memory', () async {
      final vault = VaultService(storage: memoryStorage);
      await vault.initFuture;
      await vault.setActivePatientId('patient_reset_1');

      // Populate full clinical state
      vault.saveAnamnesis('gender', 'Male');
      vault.saveAnamnesis('complaints', 'Chest pressure');
      vault.saveVitalsThresholds({
        'min_pessimum_bpm': 48,
        'max_pessimum_bpm': 142,
        'max_spike_delta_bpm': 32,
      });
      vault.saveCuratorChatMessage({'role': 'user', 'text': 'I feel irregular pulse'});
      vault.saveAthenaClinicalReport({
        'consultation_id': 'ath_log_1',
        'rationale': 'Detailed clinical observation note',
      });
      vault.updateAthenaLastPromptTime(1793500000);
      vault.updateSystemFolder('global', {'athena_sprint_day': 5});
      vault.setUnreadAthenaMessage(true);

      vault.updateLabLedgerSnapshot(_sampleLabLedger(cycleId: 'cycle_100'));

      final primaryReports = _samplePrimaryReports(cycleId: 'cycle_100');
      final primarySaved = await vault.recordClientReports(
        cycleId: 'cycle_100',
        reports: primaryReports,
        expectedPatientId: 'patient_reset_1',
      );
      expect(primarySaved, isTrue);

      final weeklyOverall = _sampleWeeklyOverallReport(cycleId: 'cycle_100', sprintId: 'sprint_100_w1');
      final weeklySystems = _sampleWeeklySystemReports(cycleId: 'cycle_100', sprintId: 'sprint_100_w1');
      final weeklySaved = await vault.recordWeeklyClientReports(
        cycleId: 'cycle_100',
        sprintId: 'sprint_100_w1',
        sprintNumber: 1,
        reportDate: '2026-10-29',
        overallReport: weeklyOverall,
        systemReports: weeklySystems,
        expectedPatientId: 'patient_reset_1',
      );
      expect(weeklySaved, isTrue);

      // Pre-reset assertions: all clinical data is confirmed active
      expect(vault.isConsiliumCompleted, isTrue);
      expect(vault.cachedOverallReport, isNotNull);
      expect(vault.cachedSystemReports.length, equals(8));
      expect(vault.isWeeklyConsiliumCompleted, isTrue);
      expect(vault.activeWeeklySprintId, equals('sprint_100_w1'));
      expect(vault.activeWeeklyOverallReport, isNotNull);
      expect(vault.activeWeeklySystemReports.length, equals(8));
      expect(vault.cachedLabLedgerState, isNotNull);
      expect(vault.labObservationsHistory, isNotEmpty);
      expect(vault.activeLabMarkers, isNotEmpty);
      expect(vault.getAthenaClinicalLogs(), isNotEmpty);
      expect(vault.getCuratorChatHistory(), isNotEmpty);
      expect(vault.getVitalsThresholds(), isNotNull);
      expect(vault.hasUnreadAthenaMessage, isTrue);
      expect(vault.getAthenaSprintDay(), equals(5));
      expect(vault.getAthenaLastPromptTime(), equals(1793500000));
      expect(vault.getSystemContext('global')['gender'], equals('Male'));
      expect(vault.getSystemContext('global')['shared_labs_shelf'], isNotNull);
      expect(vault.getSystemContext('global')['lab_ledger_snapshot'], isNotNull);

      // Execute reset
      await vault.resetVault();

      // Post-reset assertions: ALL clinical data MUST be wiped clean
      expect(vault.isConsiliumCompleted, isFalse);
      expect(vault.consiliumCycleId, isNull);
      expect(vault.cachedOverallReport, isNull);
      expect(vault.cachedSystemReports, isEmpty);
      expect(vault.consiliumHistory, isEmpty);

      expect(vault.isWeeklyConsiliumCompleted, isFalse);
      expect(vault.activeWeeklySprintId, isNull);
      expect(vault.activeWeeklySprintNumber, isNull);
      expect(vault.activeWeeklyReportDate, isNull);
      expect(vault.activeWeeklyOverallReport, isNull);
      expect(vault.activeWeeklySystemReports, isEmpty);
      expect(vault.weeklyConsiliumHistory, isEmpty);

      expect(vault.anamnesisSyncStatus, equals(AnamnesisSyncStatus.idle));
      expect(vault.anamnesisCycleId, isNull);
      expect(vault.isAnamnesisSynced, isFalse);

      expect(vault.cachedLabLedgerState, isNull);
      expect(vault.labObservationsHistory, isEmpty);
      expect(vault.activeLabMarkers, isEmpty);

      expect(vault.getAthenaClinicalLogs(), isEmpty);
      expect(vault.getCuratorChatHistory(), isEmpty);
      expect(vault.getVitalsThresholds(), isNull);
      expect(vault.hasUnreadAthenaMessage, isFalse);
      expect(vault.getAthenaSprintDay(), equals(1));
      expect(vault.getAthenaLastPromptTime(), isNull);

      expect(vault.getSystemContext('global')['gender'], isNull);
      expect(vault.getSystemContext('global')['complaints'], isNull);
      expect(vault.getSystemContext('global')['shared_labs_shelf'], isNull);
      expect(vault.getSystemContext('global')['lab_ledger_snapshot'], isNull);
      expect(vault.getSystemContext('global')['vitals_thresholds'], isNull);

      vault.dispose();
    });

    // -------------------------------------------------------------------------
    // 2. PERSISTENCE AND COLD-START RELOAD INTEGRITY
    // -------------------------------------------------------------------------
    test('2. Subsequent persist and cold start reload cannot resurrect deleted clinical data', () async {
      final vault1 = VaultService(storage: memoryStorage);
      await vault1.initFuture;
      await vault1.setActivePatientId('patient_cold_reload');

      // Populate Primary and Weekly reports and lab ledger
      await vault1.recordClientReports(
        cycleId: 'cycle_200',
        reports: _samplePrimaryReports(cycleId: 'cycle_200'),
        expectedPatientId: 'patient_cold_reload',
      );
      await vault1.recordWeeklyClientReports(
        cycleId: 'cycle_200',
        sprintId: 'sprint_200_w1',
        sprintNumber: 1,
        reportDate: '2026-10-29',
        overallReport: _sampleWeeklyOverallReport(cycleId: 'cycle_200', sprintId: 'sprint_200_w1'),
        systemReports: _sampleWeeklySystemReports(cycleId: 'cycle_200', sprintId: 'sprint_200_w1'),
        expectedPatientId: 'patient_cold_reload',
      );
      vault1.updateLabLedgerSnapshot(_sampleLabLedger(cycleId: 'cycle_200'));
      vault1.saveAthenaClinicalReport({'consultation_id': 'ath_cold_1', 'rationale': 'eval'});

      // Reset vault
      await vault1.resetVault();

      // Trigger a subsequent write/persist to prove persist does not revive memory ghosts
      vault1.updateSystemFolder('cvs', {'diagnostic_flag': 'clean_start'});
      await Future.delayed(const Duration(milliseconds: 20));

      // Inspect persisted JSON snapshot in storage
      final key = VaultStorageService.derivePatientStorageKey('patient_cold_reload');
      final storedData = await memoryStorage.loadPatientVault(key);
      expect(storedData, isNotEmpty);

      // Verify client_consilium in storage is cleanly reset
      final storedConsilium = storedData['client_consilium'] as Map<String, dynamic>;
      expect(storedConsilium['is_completed'], isFalse);
      expect(storedConsilium['cycle_id'], isNull);
      expect(storedConsilium['overall_report'], isNull);
      expect((storedConsilium['system_reports'] as Map).isEmpty, isTrue);

      // Verify weekly_consilium in storage is cleanly reset
      final storedWeekly = storedData['weekly_consilium'] as Map<String, dynamic>;
      expect(storedWeekly['active_sprint_id'], isNull);
      expect(storedWeekly['overall_report'], isNull);
      expect((storedWeekly['system_reports'] as Map).isEmpty, isTrue);

      // Verify anamnesis_sync in storage is idle
      final storedSync = storedData['anamnesis_sync'] as Map<String, dynamic>;
      expect(storedSync['status'], equals('idle'));
      expect(storedSync['cycle_id'], isNull);

      // Verify athena logs in storage is empty
      expect((storedData['athena_clinical_logs'] as List).isEmpty, isTrue);

      vault1.dispose();

      // Simulate App Restart with a fresh VaultService
      final vault2 = VaultService(storage: memoryStorage);
      await vault2.initFuture;
      await vault2.setActivePatientId('patient_cold_reload');

      // Assert that upon cold reload, no deleted clinical data is restored
      expect(vault2.isConsiliumCompleted, isFalse);
      expect(vault2.cachedOverallReport, isNull);
      expect(vault2.cachedSystemReports, isEmpty);
      expect(vault2.isWeeklyConsiliumCompleted, isFalse);
      expect(vault2.activeWeeklySprintId, isNull);
      expect(vault2.activeWeeklyOverallReport, isNull);
      expect(vault2.activeWeeklySystemReports, isEmpty);
      expect(vault2.cachedLabLedgerState, isNull);
      expect(vault2.getAthenaClinicalLogs(), isEmpty);
      expect(vault2.getCuratorChatHistory(), isEmpty);

      vault2.dispose();
    });

    // -------------------------------------------------------------------------
    // 3. RESET AFTER PRIMARY AND WEEKLY REPORTS (INCLUDING HISTORY ARCHIVES)
    // -------------------------------------------------------------------------
    test('3. Reset after multiple Primary and Weekly sprints clears active and archived history', () async {
      final vault = VaultService(storage: memoryStorage);
      await vault.initFuture;
      await vault.setActivePatientId('patient_history_test');

      // 1. Record Cycle 1 Primary Consilium
      await vault.recordClientReports(
        cycleId: 'cycle_h1',
        reports: _samplePrimaryReports(cycleId: 'cycle_h1'),
        expectedPatientId: 'patient_history_test',
      );

      // 2. Record Cycle 2 Primary Consilium (archives cycle_h1 into consiliumHistory)
      await vault.recordClientReports(
        cycleId: 'cycle_h2',
        reports: _samplePrimaryReports(cycleId: 'cycle_h2'),
        expectedPatientId: 'patient_history_test',
      );
      expect(vault.consiliumHistory.containsKey('cycle_h1'), isTrue);

      // 3. Record Week 1 Consilium
      await vault.recordWeeklyClientReports(
        cycleId: 'cycle_h2',
        sprintId: 'sprint_h2_w1',
        sprintNumber: 1,
        reportDate: '2026-10-22',
        overallReport: _sampleWeeklyOverallReport(cycleId: 'cycle_h2', sprintId: 'sprint_h2_w1', sprintNumber: 1),
        systemReports: _sampleWeeklySystemReports(cycleId: 'cycle_h2', sprintId: 'sprint_h2_w1'),
        expectedPatientId: 'patient_history_test',
      );

      // 4. Record Week 2 Consilium (archives sprint_h2_w1 into weeklyConsiliumHistory)
      await vault.recordWeeklyClientReports(
        cycleId: 'cycle_h2',
        sprintId: 'sprint_h2_w2',
        sprintNumber: 2,
        reportDate: '2026-10-29',
        overallReport: _sampleWeeklyOverallReport(cycleId: 'cycle_h2', sprintId: 'sprint_h2_w2', sprintNumber: 2),
        systemReports: _sampleWeeklySystemReports(cycleId: 'cycle_h2', sprintId: 'sprint_h2_w2'),
        expectedPatientId: 'patient_history_test',
      );
      expect(vault.weeklyConsiliumHistory.containsKey('sprint_h2_w1'), isTrue);

      // Perform reset
      await vault.resetVault();

      // Assert active reports are cleared
      expect(vault.isConsiliumCompleted, isFalse);
      expect(vault.consiliumCycleId, isNull);
      expect(vault.isWeeklyConsiliumCompleted, isFalse);
      expect(vault.activeWeeklySprintId, isNull);

      // Assert archived histories are completely cleared
      expect(vault.consiliumHistory, isEmpty);
      expect(vault.weeklyConsiliumHistory, isEmpty);

      vault.dispose();
    });

    // -------------------------------------------------------------------------
    // 4. PRESERVATION OF EXPLICITLY EXEMPTED NONCLINICAL DATA
    // -------------------------------------------------------------------------
    test('4. keepWorkoutHistory true preserves workout history; false wipes workouts', () async {
      // Part A: keepWorkoutHistory = true (default)
      final vaultA = VaultService(storage: memoryStorage);
      await vaultA.initFuture;
      await vaultA.setActivePatientId('patient_keep_workouts');

      vaultA.createWorkoutSession(
        sessionId: 'ws_001',
        anomalyDetectedAt: DateTime.parse('2026-10-28T09:00:00Z'),
        activityConfirmedAt: DateTime.parse('2026-10-28T09:05:00Z'),
      );
      vaultA.addHeartRateSample(
        sessionId: 'ws_001',
        timestamp: DateTime.parse('2026-10-28T09:02:00Z'),
        pulse: 135,
        phase: 'exercise',
      );
      // Also add clinical data that MUST be cleared
      await vaultA.recordClientReports(
        cycleId: 'cycle_w_a',
        reports: _samplePrimaryReports(cycleId: 'cycle_w_a'),
        expectedPatientId: 'patient_keep_workouts',
      );

      await vaultA.resetVault(keepWorkoutHistory: true);

      // Identity & session must be preserved
      expect(vaultA.activePatientId, equals('patient_keep_workouts'));
      expect(vaultA.isAuthenticated, isTrue);

      // Workout data must survive
      final sessionsA = vaultA.getWorkoutSessions();
      expect(sessionsA, hasLength(1));
      expect(sessionsA.first['session_id'], equals('ws_001'));
      expect(vaultA.getHeartRateSamples(), hasLength(1));

      // Clinical data must be cleared
      expect(vaultA.isConsiliumCompleted, isFalse);
      expect(vaultA.cachedOverallReport, isNull);

      vaultA.dispose();

      // Part B: keepWorkoutHistory = false
      final vaultB = VaultService(storage: memoryStorage);
      await vaultB.initFuture;
      await vaultB.setActivePatientId('patient_wipe_workouts');

      vaultB.createWorkoutSession(
        sessionId: 'ws_002',
        anomalyDetectedAt: DateTime.parse('2026-10-28T10:00:00Z'),
        activityConfirmedAt: DateTime.parse('2026-10-28T10:05:00Z'),
      );
      vaultB.addHeartRateSample(
        sessionId: 'ws_002',
        timestamp: DateTime.parse('2026-10-28T10:02:00Z'),
        pulse: 140,
        phase: 'exercise',
      );

      await vaultB.resetVault(keepWorkoutHistory: false);

      // Workout data must be completely wiped
      expect(vaultB.getWorkoutSessions(), isEmpty);
      expect(vaultB.getHeartRateSamples(), isEmpty);
      expect(vaultB.activePatientId, equals('patient_wipe_workouts'));

      vaultB.dispose();
    });

    // -------------------------------------------------------------------------
    // 5. PATIENT SWITCHING & DATA ISOLATION
    // -------------------------------------------------------------------------
    test('5. Resetting Patient A never corrupts or deletes Patient B clinical data', () async {
      final vault = VaultService(storage: memoryStorage);
      await vault.initFuture;

      // 1. Setup Patient A with clinical data
      await vault.setActivePatientId('pat_A');
      await vault.recordClientReports(
        cycleId: 'cycle_A',
        reports: _samplePrimaryReports(cycleId: 'cycle_A'),
        expectedPatientId: 'pat_A',
      );
      vault.saveAnamnesis('gender', 'Male');

      // 2. Setup Patient B with distinct clinical data
      await vault.setActivePatientId('pat_B');
      await vault.recordClientReports(
        cycleId: 'cycle_B',
        reports: _samplePrimaryReports(cycleId: 'cycle_B'),
        expectedPatientId: 'pat_B',
      );
      await vault.recordWeeklyClientReports(
        cycleId: 'cycle_B',
        sprintId: 'sprint_B_w1',
        sprintNumber: 1,
        reportDate: '2026-10-29',
        overallReport: _sampleWeeklyOverallReport(cycleId: 'cycle_B', sprintId: 'sprint_B_w1'),
        systemReports: _sampleWeeklySystemReports(cycleId: 'cycle_B', sprintId: 'sprint_B_w1'),
        expectedPatientId: 'pat_B',
      );
      vault.saveAnamnesis('gender', 'Female');

      // 3. Switch back to Patient A and reset Patient A
      await vault.setActivePatientId('pat_A');
      expect(vault.isConsiliumCompleted, isTrue);
      await vault.resetVault();
      expect(vault.isConsiliumCompleted, isFalse);

      // 4. Switch to Patient B: Patient B clinical data MUST remain 100% intact
      await vault.setActivePatientId('pat_B');
      expect(vault.activePatientId, equals('pat_B'));
      expect(vault.isConsiliumCompleted, isTrue);
      expect(vault.consiliumCycleId, equals('cycle_B'));
      expect(vault.isWeeklyConsiliumCompleted, isTrue);
      expect(vault.activeWeeklySprintId, equals('sprint_B_w1'));
      expect(vault.getSystemContext('global')['gender'], equals('Female'));

      // 5. Switch back to Patient A: Patient A remains reset
      await vault.setActivePatientId('pat_A');
      expect(vault.isConsiliumCompleted, isFalse);
      expect(vault.getSystemContext('global')['gender'], isNull);

      vault.dispose();
    });

    // -------------------------------------------------------------------------
    // 6. SESSION EPOCH FENCING & RACE CONDITION PREVENTION
    // -------------------------------------------------------------------------
    test('6. Session epoch fencing aborts reset persistence if patient switches during reset', () async {
      final vault = VaultService(storage: memoryStorage);
      await vault.initFuture;
      await vault.setActivePatientId('pat_race_A');

      // Save Patient A data
      vault.saveAnamnesis('gender', 'Male');

      // Intercept storage clear to trigger patient switch concurrently
      memoryStorage.onClearPatientVault = (key) async {
        // While Patient A's clear is in flight, switch active patient to pat_race_B
        await vault.setActivePatientId('pat_race_B');
        vault.saveAnamnesis('gender', 'Female');
      };

      // Run reset on Patient A (will be fenced out when returning)
      await vault.resetVault();

      // Active patient is now pat_race_B
      expect(vault.activePatientId, equals('pat_race_B'));
      // Patient B data was not overwritten by Patient A's reset persist
      expect(vault.getSystemContext('global')['gender'], equals('Female'));

      vault.dispose();
    });
  });
}
