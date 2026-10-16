import 'package:flutter/foundation.dart';
import 'vitals_alert_context.dart';
import '../services/time_service.dart';
import '../services/vault_service.dart';
import '../services/athena_trigger_service.dart';
import 'package:aegis_health/services/aegis_cascade_controller.dart';

/// Pulse monitor operation states
enum AlertState {
  idle,                  // Standby / standard monitoring mode (every 10 min)
  waitingUserResponse,   // Spike detected, waiting for "Are you exercising?" response
  workoutActive,         // Workout in progress: 1-min interval, blocking screen active
  postWorkoutRecovery,   // Recovery mode: 0-10m: 2m, 10-50m: 5m, 50m+: 10m
  inChatWithAthena,      // User answered "No", control handed over to Athena
  criticalAnomaly,       // Pulse 0 for >= 2 minutes (sensor lost / detached)
  trackingInBackground,  // Kept for backward compatibility
}

/// Model for historical pulse sampling entries
class PulseRecord {
  final DateTime timestamp;
  final int pulse;
  final String phase; // 'workout', 'recovery_0_10', 'recovery_10_50', 'standard'
  final String note;
  final bool isSimulated;
  final String source; // 'watch_sensor', 'manual', 'test_chip', 'time_simulation'
  final String? sessionId;

  const PulseRecord({
    required this.timestamp,
    required this.pulse,
    required this.phase,
    this.note = '',
    this.isSimulated = false,
    this.source = 'watch_sensor',
    this.sessionId,
  });

  Map<String, dynamic> toJson() => {
    'timestamp': timestamp.toIso8601String(),
    'pulse': pulse,
    'phase': phase,
    'note': note,
    'is_simulated': isSimulated,
    'source': source,
    if (sessionId != null) 'session_id': sessionId,
  };

  factory PulseRecord.fromJson(Map<String, dynamic> json) => PulseRecord(
    timestamp: DateTime.tryParse(json['timestamp']?.toString() ?? '') ?? DateTime.now(),
    pulse: (json['pulse'] as num?)?.toInt() ?? 0,
    phase: json['phase']?.toString() ?? 'workout',
    note: json['note']?.toString() ?? '',
    isSimulated: json['is_simulated'] == true,
    source: json['source']?.toString() ?? 'watch_sensor',
    sessionId: json['session_id']?.toString(),
  );
}

/// Pulse analysis, workout tracking, and adaptive monitoring service
class VitalsMonitorService extends ChangeNotifier {
  TimeService _timeService;
  final VaultService _vaultService;

  AlertState currentState = AlertState.idle;

  final List<int> _pulseBuffer = []; // Pulse measurement history (last 10 points)
  int _baselinePulse = 60;          // Baseline pulse level before spike
  int _currentPulse = 70;           // Latest pulse reading

  int get currentPulse => _currentPulse;

  VitalsAlertContext? currentEvent;

  // Workout timing & logging
  DateTime? _workoutStartTime;
  DateTime? _workoutEndTime;
  DateTime? _lastRecordedTime;
  DateTime? _zeroPulseStartTime;
  DateTime? _anomalyDetectedTime;
  DateTime? _activityConfirmedTime;
  String? _currentSessionId;
  String? criticalAnomalyReason;

  DateTime? get workoutStartTime => _workoutStartTime;
  DateTime? get workoutEndTime => _workoutEndTime;
  String? get currentSessionId => _currentSessionId;

  final List<PulseRecord> _workoutPulseLog = [];
  List<PulseRecord> get workoutPulseLog => List.unmodifiable(_workoutPulseLog);

  /// Constructor accepts TimeService and VaultService
  VitalsMonitorService(this._timeService, this._vaultService) {
    _timeService.addListener(_onTimeShifted);
  }

  /// Update TimeService reference on provider rebuild
  void updateTimeService(TimeService newTimeService) {
    _timeService.removeListener(_onTimeShifted);
    _timeService = newTimeService;
    _timeService.addListener(_onTimeShifted);
  }

  /// Helper to record a pulse point into the workout/recovery log
  void _recordLog(
    DateTime time,
    int pulse,
    String phase, [
    String note = '',
    bool isSimulated = false,
    String source = 'watch_sensor',
    String? sessionId,
  ]) {
    _workoutPulseLog.add(PulseRecord(
      timestamp: time,
      pulse: pulse,
      phase: phase,
      note: note,
      isSimulated: isSimulated,
      source: source,
      sessionId: sessionId ?? _currentSessionId,
    ));
    _lastRecordedTime = time;
    debugPrint("📝 [VitalsMonitor Log]: $pulse bpm ($phase${note.isNotEmpty ? ', $note' : ''}${isSimulated ? ', SIMULATED' : ''}) at $time");
  }

  /// Returns required sampling interval in minutes depending on the current phase
  int getRequiredIntervalMinutes() {
    if (currentState == AlertState.workoutActive) {
      return 1; // During workout: every 1 minute
    }
    if (currentState == AlertState.postWorkoutRecovery && _workoutEndTime != null) {
      final elapsed = _timeService.now.difference(_workoutEndTime!).inMinutes;
      if (elapsed < 10) {
        return 2; // 0 to 10 min: every 2 minutes
      } else if (elapsed < 50) {
        return 5; // 10 to 50 min: every 5 minutes
      } else {
        return 10; // 50+ min: standard Samsung Health mode (every 10 minutes)
      }
    }
    return 10; // Default standard mode: every 10 minutes
  }

  /// Main entry method: called when a new pulse value arrives from the watch/panel/tests
  void onNewPulseRead(
    int newPulse, {
    DateTime? timestamp,
    String source = 'watch_sensor',
    bool isSimulated = false,
  }) {
    final DateTime effectiveTime = timestamp ?? _timeService.now;
    _currentPulse = newPulse;

    if (newPulse > 0) {
      _pulseBuffer.add(newPulse);
      if (_pulseBuffer.length > 10) {
        _pulseBuffer.removeAt(0); // Keep only last 10 points
      }
    }

    debugPrint("📥 [VitalsMonitor]: New pulse: $newPulse bpm at $effectiveTime (Buffer: ${_pulseBuffer.length}, State: $currentState, Source: $source, Simulated: $isSimulated)");

    // 1. Zero pulse check (sensor lost / detached anomaly)
    _checkZeroPulseAnomaly(newPulse, effectiveTime);
    if (currentState == AlertState.criticalAnomaly) {
      notifyListeners();
      return;
    }

    // 2. State-specific handling
    switch (currentState) {
      case AlertState.idle:
      case AlertState.trackingInBackground:
        if (newPulse >= 140) {
          _triggerSpikeAlert(newPulse, timestamp: effectiveTime, reason: "Высокий пульс ($newPulse bpm >= 140)", source: source, isSimulated: isSimulated);
        } else {
          _analyzeForSpike(newPulse, timestamp: effectiveTime);
        }
        break;

      case AlertState.workoutActive:
        _recordLog(effectiveTime, newPulse, 'workout', '', isSimulated, source, _currentSessionId);
        if (_currentSessionId != null) {
          _vaultService.addHeartRateSample(
            sessionId: _currentSessionId,
            timestamp: effectiveTime,
            pulse: newPulse,
            phase: 'workout',
            source: source,
            isSimulated: isSimulated,
          );
        }
        notifyListeners();
        break;

      case AlertState.postWorkoutRecovery:
        // Anomaly: Pulse 140+ during recovery triggers the alert cycle again!
        if (newPulse >= 140) {
          debugPrint("🚨 [VitalsMonitor]: Pulse $newPulse bpm >= 140 during recovery! Restarting alert cycle.");
          _triggerSpikeAlert(newPulse, timestamp: effectiveTime, reason: "Скачок пульса после тренировки ($newPulse bpm)", source: source, isSimulated: isSimulated);
          return;
        }

        // Check adaptive sampling interval
        final interval = getRequiredIntervalMinutes();
        if (_lastRecordedTime == null ||
            effectiveTime.difference(_lastRecordedTime!).inMinutes >= interval) {
          final elapsed = _workoutEndTime != null
              ? effectiveTime.difference(_workoutEndTime!).inMinutes
              : 0;
          final phase = elapsed < 10
              ? 'recovery_0_10 (каждые 2 мин)'
              : (elapsed < 50 ? 'recovery_10_50 (каждые 5 мин)' : 'standard (каждые 10 мин)');

          _recordLog(effectiveTime, newPulse, phase, '', isSimulated, source, _currentSessionId);
          if (_currentSessionId != null) {
            _vaultService.addHeartRateSample(
              sessionId: _currentSessionId,
              timestamp: effectiveTime,
              pulse: newPulse,
              phase: phase,
              source: source,
              isSimulated: isSimulated,
            );
          }
        }

        // Check physiological recovery end
        if (newPulse <= (_baselinePulse * 1.15) && _currentSessionId != null) {
          _vaultService.recordPhysiologicalEnd(
            sessionId: _currentSessionId!,
            physiologicalEndTime: effectiveTime,
          );
        }
        notifyListeners();
        break;

      case AlertState.waitingUserResponse:
      case AlertState.inChatWithAthena:
      case AlertState.criticalAnomaly:
        notifyListeners();
        break;
    }
  }

  /// Checks if pulse has stayed at 0 bpm for 2 or more minutes
  void _checkZeroPulseAnomaly(int pulse, [DateTime? time]) {
    final effectiveTime = time ?? _timeService.now;
    if (pulse == 0) {
      _zeroPulseStartTime ??= effectiveTime;
      final minutesZero = effectiveTime.difference(_zeroPulseStartTime!).inMinutes;
      if (minutesZero >= 2) {
        _triggerCriticalAnomaly("Возможная потеря сигнала датчика: пульс 0 уд/мин зафиксирован в течение $minutesZero мин. Проверьте контакт устройства.");
      }
    } else {
      _zeroPulseStartTime = null;
    }
  }

  /// Triggers a critical medical anomaly (e.g. 0 pulse for 2 minutes)
  void _triggerCriticalAnomaly(String reason) {
    debugPrint("⚠️⚠️⚠️ [VitalsMonitor CRITICAL]: $reason");
    currentState = AlertState.criticalAnomaly;
    criticalAnomalyReason = reason;
    notifyListeners();
  }

  /// Sharp spike detection algorithm based on Chief Doctor thresholds
  void _analyzeForSpike(int currentPulse, {DateTime? timestamp}) {
    final VitalsThresholds? doctorThresholds = _vaultService.getVitalsThresholds();

    if (doctorThresholds == null) {
      debugPrint("⏸️ [VitalsMonitor]: Chief Doctor thresholds are not formed yet. Analysis blocked.");
      return;
    }

    // Ignore values below 30 bpm
    if (currentPulse < 30) {
      debugPrint("⚠️ [VitalsMonitor]: Pulse $currentPulse bpm is below artifact threshold (30). Skipping.");
      return;
    }

    // Need at least 3 measurements in buffer
    if (_pulseBuffer.length < 3) {
      debugPrint("⏳ [VitalsMonitor]: Accumulated ${_pulseBuffer.length}/3 points. Analysis not required yet.");
      return;
    }

    // Calculate baseline resting pulse
    final previousValues = _pulseBuffer.sublist(0, _pulseBuffer.length - 1);
    _baselinePulse = previousValues.reduce((a, b) => a < b ? a : b);

    final bool isSpike = doctorThresholds.isSpikeDetected(
      currentPulse: currentPulse,
      baselinePulse: _baselinePulse,
    );

    debugPrint("📊 [VitalsMonitor]: Baseline: $_baselinePulse bpm, Peak: $currentPulse bpm | Thresholds: [Min: ${doctorThresholds.minPessimumBpm}, Max: ${doctorThresholds.maxPessimumBpm}, Delta: ${doctorThresholds.maxSpikeDeltaBpm}]");

    if (isSpike) {
      _triggerSpikeAlert(
        currentPulse,
        timestamp: timestamp,
        reason: "Превышение порогов Главврача (Пик: $currentPulse, База: $_baselinePulse)",
      );
    }
  }

  /// Activates the spike alert and prepares context
  void _triggerSpikeAlert(
    int currentPulse, {
    DateTime? timestamp,
    String? reason,
    String source = 'watch_sensor',
    bool isSimulated = false,
  }) {
    final VitalsThresholds doctorThresholds = _vaultService.getVitalsThresholds() ??
        const VitalsThresholds(
          minPessimumBpm: 45,
          maxPessimumBpm: 140,
          maxSpikeDeltaBpm: 40,
          clinicalRationale: "Default safety thresholds",
        );

    // If a new workout starts during recovery of previous, close previous data collection cleanly
    if (currentState == AlertState.postWorkoutRecovery && _currentSessionId != null) {
      _vaultService.finishDataCollection(
        sessionId: _currentSessionId!,
        reason: 'interrupted_by_new_workout_session',
      );
    }

    // Workout start time is fixed at the EXACT moment the warning alert appears!
    final effectiveTime = timestamp ?? _timeService.now;
    _workoutStartTime = effectiveTime;
    _anomalyDetectedTime = _workoutStartTime;
    _currentSessionId = 'ws_${_workoutStartTime!.millisecondsSinceEpoch}';
    currentState = AlertState.waitingUserResponse;

    currentEvent = VitalsAlertContext(
      timestamp: _workoutStartTime!,
      baselinePulse: _baselinePulse,
      currentPulse: currentPulse,
      pulseHistoryLast5Min: List.from(_pulseBuffer),
      thresholds: doctorThresholds,
    );

    _lastRecordedTime = _workoutStartTime;

    debugPrint("🚨 [VitalsMonitor]: SPIKE DETECTED ($reason)! Session: $_currentSessionId, Workout start marked at $_workoutStartTime. Transition to AlertState.waitingUserResponse.");
    notifyListeners();
  }

  /// Handling user response to the question: "Are you exercising?"
  Future<void> handleUserActivityResponse({
    required bool isActivity,
    required AthenaTriggerService athenaTriggerService,
    required AegisCascadeController cascadeController,
  }) async {
    if (currentEvent == null) return;

    currentEvent = currentEvent!.copyWith(isPhysicalActivity: isActivity);

    if (isActivity) {
      // Workout confirmed -> switch to workout active mode
      currentState = AlertState.workoutActive;
      _workoutStartTime ??= currentEvent?.timestamp ?? _timeService.now;
      _activityConfirmedTime = _timeService.now;
      _workoutEndTime = null;
      _currentSessionId ??= 'ws_${_workoutStartTime!.millisecondsSinceEpoch}';

      // Record starting minute
      _recordLog(_workoutStartTime!, _currentPulse, 'workout', 'Старт тренировки', false, 'watch_sensor', _currentSessionId);
      _lastRecordedTime = _workoutStartTime;

      // Create permanent session in Vault
      _vaultService.createWorkoutSession(
        sessionId: _currentSessionId!,
        anomalyDetectedAt: _anomalyDetectedTime ?? _workoutStartTime!,
        activityConfirmedAt: _activityConfirmedTime!,
        baselinePulse: _baselinePulse,
        peakPulse: _currentPulse,
        triggerReason: currentEvent?.thresholds.clinicalRationale ?? 'Threshold spike exceeded',
        doctorThresholds: currentEvent?.thresholds.toJson() ?? {},
        initialTimestamp: _workoutStartTime!,
        initialPulse: _currentPulse,
        initialPhase: 'workout',
        initialSource: 'watch_sensor',
        initialIsSimulated: false,
        initialNote: 'Старт тренировки (алерт)',
      );

      debugPrint("🏃 [VitalsMonitor]: Workout confirmed! Session: $_currentSessionId, Start: $_workoutStartTime. Transition to AlertState.workoutActive.");
      notifyListeners();
    } else {
      currentState = AlertState.inChatWithAthena;
      debugPrint("🩺 [VitalsMonitor]: Workout declined. Launching emergency Athena trigger...");
      notifyListeners();

      await athenaTriggerService.triggerVitalsSpike(
        vaultService: _vaultService,
        timeService: _timeService,
        cascadeController: cascadeController,
        alertContext: currentEvent!,
      );
    }
  }

  /// Called when user taps "ТРЕНИРОВКА ЗАВЕРШЕНА"
  void finishWorkout() {
    _workoutEndTime = _timeService.now;
    currentState = AlertState.postWorkoutRecovery;
    _lastRecordedTime = _workoutEndTime;

    final duration = _workoutStartTime != null
        ? _workoutEndTime!.difference(_workoutStartTime!)
        : Duration.zero;

    if (_currentSessionId != null) {
      _vaultService.completeWorkoutSession(
        sessionId: _currentSessionId!,
        workoutCompletedAt: _workoutEndTime!,
      );

      _vaultService.addHeartRateSample(
        sessionId: _currentSessionId,
        timestamp: _workoutEndTime!,
        pulse: _currentPulse,
        phase: 'recovery_0_10 (каждые 2 мин)',
        source: 'watch_sensor',
        isSimulated: false,
        note: 'Тренировка завершена (${duration.inMinutes} мин)',
      );
    }

    _recordLog(
      _workoutEndTime!,
      _currentPulse,
      'recovery_0_10 (каждые 2 мин)',
      'Тренировка завершена (${duration.inMinutes} мин)',
      false,
      'watch_sensor',
      _currentSessionId,
    );

    debugPrint("🏁 [VitalsMonitor]: Workout ended at $_workoutEndTime. Session: $_currentSessionId, Duration: ${duration.inMinutes}m ${duration.inSeconds % 60}s. Switched to AlertState.postWorkoutRecovery.");
    notifyListeners();
  }

  /// Time simulator listener (TimeService)
  void _onTimeShifted() {
    final now = _timeService.now;

    // 1. Check for zero pulse anomaly across time jumps
    if (_currentPulse == 0 && _zeroPulseStartTime != null) {
      final minutesZero = now.difference(_zeroPulseStartTime!).inMinutes;
      if (minutesZero >= 2) {
        _triggerCriticalAnomaly("Возможная потеря сигнала датчика: пульс 0 уд/мин зафиксирован в течение $minutesZero мин. Проверьте контакт устройства.");
        return;
      }
    }

    // 2. Active workout phase: record missed sample gaps across time jumps (NO synthetic measurement fabrication!)
    if (currentState == AlertState.workoutActive && _workoutStartTime != null && _currentSessionId != null) {
      final lastTime = _lastRecordedTime ?? _workoutStartTime!;
      final minutesElapsed = now.difference(lastTime).inMinutes;

      if (minutesElapsed > 1) {
        _vaultService.recordMissedSamples(
          sessionId: _currentSessionId!,
          from: lastTime,
          to: now,
          expectedIntervalMinutes: 1,
          missedCount: minutesElapsed - 1,
          reason: 'time_jump_no_sensor_reading',
        );
        _lastRecordedTime = now;
        notifyListeners();
      }
      return;
    }

    // 3. Post-workout recovery phase: adaptive sampling intervals and gap detection
    if (currentState == AlertState.postWorkoutRecovery && _workoutEndTime != null && _currentSessionId != null) {
      // Check if pulse surged to 140+ during time jump
      if (_currentPulse >= 140) {
        _triggerSpikeAlert(_currentPulse, reason: "Скачок пульса после тренировки ($_currentPulse bpm)");
        return;
      }

      final interval = getRequiredIntervalMinutes();
      final lastTime = _lastRecordedTime ?? _workoutEndTime!;
      final minutesElapsed = now.difference(lastTime).inMinutes;

      if (minutesElapsed > interval) {
        final missedCount = (minutesElapsed / interval).floor() - 1;
        if (missedCount > 0) {
          _vaultService.recordMissedSamples(
            sessionId: _currentSessionId!,
            from: lastTime,
            to: now,
            expectedIntervalMinutes: interval,
            missedCount: missedCount,
            reason: 'time_jump_no_sensor_reading',
          );
        }
        _lastRecordedTime = now;
        notifyListeners();
      }

      // Check if recovery period elapsed (50+ min)
      final totalElapsedSinceEnd = now.difference(_workoutEndTime!).inMinutes;
      if (totalElapsedSinceEnd >= 50) {
        _vaultService.finishDataCollection(
          sessionId: _currentSessionId!,
          reason: 'standard_recovery_period_completed',
        );
      }
      return;
    }

    // 4. Standby idle mode
    if (currentState == AlertState.idle) {
      if (_currentPulse >= 140) {
        _triggerSpikeAlert(_currentPulse, reason: "Высокий пульс при перемотке ($_currentPulse bpm)");
      } else if (_pulseBuffer.length >= 3) {
        _analyzeForSpike(_pulseBuffer.last);
      }
    }
  }

  /// Resets state back to standby mode from chat or anomaly screens
  void resetToIdle() {
    currentState = AlertState.idle;
    currentEvent = null;
    criticalAnomalyReason = null;
    _zeroPulseStartTime = null;
    debugPrint("🔄 [VitalsMonitor]: State returned to AlertState.idle.");
    notifyListeners();
  }

  /// Complete state reset to default without deleting persistent workout history
  void reset() {
    currentState = AlertState.idle;
    currentEvent = null;
    _lastRecordedTime = null;
    _workoutStartTime = null;
    _workoutEndTime = null;
    _anomalyDetectedTime = null;
    _activityConfirmedTime = null;
    _currentSessionId = null;
    _zeroPulseStartTime = null;
    criticalAnomalyReason = null;
    // NOTE: We do NOT clear _workoutPulseLog and do NOT delete Vault history!
    _pulseBuffer.clear();
    _currentPulse = 70;
    debugPrint("🔄 [VitalsMonitor]: Service reset to standby mode (AlertState.idle). Workout history preserved.");
    notifyListeners();
  }

  @override
  void dispose() {
    _timeService.removeListener(_onTimeShifted);
    super.dispose();
  }
}
