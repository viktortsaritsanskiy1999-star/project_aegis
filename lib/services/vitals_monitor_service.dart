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
  criticalAnomaly,       // Pulse 0 for >= 2 minutes (cardiac arrest / sensor detached)
  trackingInBackground,  // Kept for backward compatibility
}

/// Model for historical pulse sampling entries
class PulseRecord {
  final DateTime timestamp;
  final int pulse;
  final String phase; // 'workout', 'recovery_0_10', 'recovery_10_50', 'standard'
  final String note;
  final bool isSimulated;

  const PulseRecord({
    required this.timestamp,
    required this.pulse,
    required this.phase,
    this.note = '',
    this.isSimulated = false,
  });

  Map<String, dynamic> toJson() => {
    'timestamp': timestamp.toIso8601String(),
    'pulse': pulse,
    'phase': phase,
    'note': note,
    'is_simulated': isSimulated,
  };
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
  String? criticalAnomalyReason;

  DateTime? get workoutStartTime => _workoutStartTime;
  DateTime? get workoutEndTime => _workoutEndTime;

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
  void _recordLog(DateTime time, int pulse, String phase, [String note = '', bool isSimulated = false]) {
    _workoutPulseLog.add(PulseRecord(
      timestamp: time,
      pulse: pulse,
      phase: phase,
      note: note,
      isSimulated: isSimulated,
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
  void onNewPulseRead(int newPulse) {
    _currentPulse = newPulse;

    if (newPulse > 0) {
      _pulseBuffer.add(newPulse);
      if (_pulseBuffer.length > 10) {
        _pulseBuffer.removeAt(0); // Keep only last 10 points
      }
    }

    debugPrint("📥 [VitalsMonitor]: New pulse: $newPulse bpm (Buffer: ${_pulseBuffer.length}, State: $currentState)");

    // 1. Zero pulse check (asystole / sensor lost anomaly)
    _checkZeroPulseAnomaly(newPulse);
    if (currentState == AlertState.criticalAnomaly) {
      notifyListeners();
      return;
    }

    // 2. State-specific handling
    switch (currentState) {
      case AlertState.idle:
      case AlertState.trackingInBackground:
        if (newPulse >= 140) {
          _triggerSpikeAlert(newPulse, reason: "Высокий пульс ($newPulse bpm >= 140)");
        } else {
          _analyzeForSpike(newPulse);
        }
        break;

      case AlertState.workoutActive:
        // Record reading during workout if at least 1 minute elapsed since last record
        if (_lastRecordedTime == null ||
            _timeService.now.difference(_lastRecordedTime!).inMinutes >= 1) {
          _recordLog(_timeService.now, newPulse, 'workout');
        }
        notifyListeners();
        break;

      case AlertState.postWorkoutRecovery:
        // Anomaly: Pulse 140+ during recovery triggers the alert cycle again!
        if (newPulse >= 140) {
          debugPrint("🚨 [VitalsMonitor]: Pulse $newPulse bpm >= 140 during recovery! Restarting alert cycle.");
          _triggerSpikeAlert(newPulse, reason: "Скачок пульса после тренировки ($newPulse bpm)");
          return;
        }

        // Check adaptive sampling interval
        final interval = getRequiredIntervalMinutes();
        if (_lastRecordedTime == null ||
            _timeService.now.difference(_lastRecordedTime!).inMinutes >= interval) {
          final elapsed = _workoutEndTime != null
              ? _timeService.now.difference(_workoutEndTime!).inMinutes
              : 0;
          final phase = elapsed < 10
              ? 'recovery_0_10 (каждые 2 мин)'
              : (elapsed < 50 ? 'recovery_10_50 (каждые 5 мин)' : 'standard (каждые 10 мин)');
          _recordLog(_timeService.now, newPulse, phase);
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
  void _checkZeroPulseAnomaly(int pulse) {
    if (pulse == 0) {
      _zeroPulseStartTime ??= _timeService.now;
      final minutesZero = _timeService.now.difference(_zeroPulseStartTime!).inMinutes;
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
  void _analyzeForSpike(int currentPulse) {
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
        reason: "Превышение порогов Главврача (Пик: $currentPulse, База: $_baselinePulse)",
      );
    }
  }

  /// Activates the spike alert and prepares context
  void _triggerSpikeAlert(int currentPulse, {String? reason}) {
    final VitalsThresholds doctorThresholds = _vaultService.getVitalsThresholds() ??
        const VitalsThresholds(
          minPessimumBpm: 45,
          maxPessimumBpm: 140,
          maxSpikeDeltaBpm: 40,
          clinicalRationale: "Default safety thresholds",
        );

    // Workout start time is fixed at the EXACT moment the warning alert appears!
    _workoutStartTime = _timeService.now;
    currentState = AlertState.waitingUserResponse;

    currentEvent = VitalsAlertContext(
      timestamp: _workoutStartTime!,
      baselinePulse: _baselinePulse,
      currentPulse: currentPulse,
      pulseHistoryLast5Min: List.from(_pulseBuffer),
      thresholds: doctorThresholds,
    );

    _lastRecordedTime = _workoutStartTime;

    debugPrint("🚨 [VitalsMonitor]: SPIKE DETECTED ($reason)! Workout start marked at $_workoutStartTime. Transition to AlertState.waitingUserResponse.");
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
      _workoutEndTime = null;
      _workoutPulseLog.clear();

      // Record starting minute
      _recordLog(_workoutStartTime!, _currentPulse, 'workout', 'Старт тренировки');
      _lastRecordedTime = _workoutStartTime;

      debugPrint("🏃 [VitalsMonitor]: Workout confirmed! Start: $_workoutStartTime. Transition to AlertState.workoutActive.");
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

    _recordLog(
      _workoutEndTime!,
      _currentPulse,
      'recovery_0_10 (каждые 2 мин)',
      'Тренировка завершена (${duration.inMinutes} мин)',
    );

    debugPrint("🏁 [VitalsMonitor]: Workout ended at $_workoutEndTime. Duration: ${duration.inMinutes}m ${duration.inSeconds % 60}s. Switched to AlertState.postWorkoutRecovery.");
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

    // 2. Active workout phase: record minute-by-minute progression
    if (currentState == AlertState.workoutActive && _workoutStartTime != null) {
      final lastTime = _lastRecordedTime ?? _workoutStartTime!;
      final minutesElapsed = now.difference(lastTime).inMinutes;

      if (minutesElapsed >= 1) {
        // Record elapsed minutes explicitly marked as simulated
        for (int m = 1; m <= minutesElapsed && m <= 60; m++) {
          final entryTime = lastTime.add(Duration(minutes: m));
          _recordLog(entryTime, _currentPulse, 'workout', 'simulated', true);
        }
        _lastRecordedTime = now;
        notifyListeners();
      }
      return;
    }

    // 3. Post-workout recovery phase: adaptive sampling (0-10m: 2m, 10-50m: 5m, 50m+: 10m)
    if (currentState == AlertState.postWorkoutRecovery && _workoutEndTime != null) {
      // Check if pulse surged to 140+ during time jump
      if (_currentPulse >= 140) {
        _triggerSpikeAlert(_currentPulse, reason: "Скачок пульса после тренировки ($_currentPulse bpm)");
        return;
      }

      final interval = getRequiredIntervalMinutes();
      final lastTime = _lastRecordedTime ?? _workoutEndTime!;
      final minutesElapsed = now.difference(lastTime).inMinutes;

      if (minutesElapsed >= interval) {
        final totalElapsedSinceEnd = now.difference(_workoutEndTime!).inMinutes;
        final phase = totalElapsedSinceEnd < 10
            ? 'recovery_0_10 (каждые 2 мин)'
            : (totalElapsedSinceEnd < 50 ? 'recovery_10_50 (каждые 5 мин)' : 'standard (каждые 10 мин)');

        _recordLog(now, _currentPulse, phase, 'simulated', true);
        _lastRecordedTime = now;
        notifyListeners();
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

  /// Complete state reset to default
  void reset() {
    currentState = AlertState.idle;
    currentEvent = null;
    _lastRecordedTime = null;
    _workoutStartTime = null;
    _workoutEndTime = null;
    _zeroPulseStartTime = null;
    criticalAnomalyReason = null;
    _workoutPulseLog.clear();
    _pulseBuffer.clear();
    _currentPulse = 70;
    debugPrint("🔄 [VitalsMonitor]: Service completely reset to standby mode (AlertState.idle)");
    notifyListeners();
  }

  @override
  void dispose() {
    _timeService.removeListener(_onTimeShifted);
    super.dispose();
  }
}
