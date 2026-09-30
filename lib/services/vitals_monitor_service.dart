import 'package:flutter/foundation.dart';
import 'vitals_alert_context.dart';
import '../services/time_service.dart';
import '../services/vault_service.dart';
import '../services/athena_trigger_service.dart';
import 'package:aegis_health/services/aegis_cascade_controller.dart';

/// Pulse monitor operation states
enum AlertState {
  idle,                  // Standby mode, no anomalies
  waitingUserResponse,   // Spike detected, waiting for "Are you exercising?" response
  trackingInBackground,  // User confirmed workout, 10-min background tracking
  inChatWithAthena       // User answered "No", control handed over to Athena
}

/// Pulse analysis and tachycardia/arrhythmic spike detection service
class VitalsMonitorService extends ChangeNotifier {
  TimeService _timeService;
  final VaultService _vaultService; // 🎯 Added VaultService to fetch Chief Doctor thresholds

  AlertState currentState = AlertState.idle;

  final List<int> _pulseBuffer = []; // Pulse measurement history
  int _baselinePulse = 60;          // Baseline pulse level before spike

  VitalsAlertContext? currentEvent;
  DateTime? _lastCheckTime;

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

  /// Main entry method: called when a new pulse value arrives from the watch/panel
  void onNewPulseRead(int newPulse) {
    _pulseBuffer.add(newPulse);
    if (_pulseBuffer.length > 10) {
      _pulseBuffer.removeAt(0); // Keep only the last 10 points
    }

    debugPrint("📥 [VitalsMonitor]: New pulse: $newPulse bpm (Total points: ${_pulseBuffer.length}, State: $currentState)");

    switch (currentState) {
      case AlertState.idle:
        _analyzeForSpike(newPulse);
        break;
      case AlertState.trackingInBackground:
        _checkBackgroundRecovery(newPulse);
        break;
      case AlertState.waitingUserResponse:
      case AlertState.inChatWithAthena:
      // Wait for user actions in the UI
        break;
    }
  }

  /// Sharp spike detection algorithm based on Chief Doctor thresholds
  void _analyzeForSpike(int currentPulse) {
    // 🎯 KEY STEP: Take Chief Doctor thresholds from the vault
    final VitalsThresholds? doctorThresholds = _vaultService.getVitalsThresholds();

    // Until the Chief Doctor issues a verdict — do not analyze anything and exit!
    if (doctorThresholds == null) {
      debugPrint("⏸️ [VitalsMonitor]: Chief Doctor thresholds are not formed yet. Analysis blocked.");
      return;
    }

    // 1. Ignore incorrect measurement artifacts (e.g., below 30 bpm)
    if (currentPulse < 30) {
      debugPrint("⚠️ [VitalsMonitor]: Pulse $currentPulse bpm is below artifact threshold (30). Skipping.");
      return;
    }

    // 2. Wait for at least 3 real measurements
    if (_pulseBuffer.length < 3) {
      debugPrint("⏳ [VitalsMonitor]: Accumulated ${_pulseBuffer.length}/3 points. Analysis not required yet.");
      return;
    }

    // 3. Calculate resting baseline (minimum value from past points)
    final previousValues = _pulseBuffer.sublist(0, _pulseBuffer.length - 1);
    _baselinePulse = previousValues.reduce((a, b) => a < b ? a : b);

    // 4. Check pulse via personal VitalsThresholds methods
    final bool isSpike = doctorThresholds.isSpikeDetected(
      currentPulse: currentPulse,
      baselinePulse: _baselinePulse,
    );

    debugPrint("📊 [VitalsMonitor]: Baseline: $_baselinePulse bpm, Peak: $currentPulse bpm | Thresholds: [Min: ${doctorThresholds.minPessimumBpm}, Max: ${doctorThresholds.maxPessimumBpm}, Delta: ${doctorThresholds.maxSpikeDeltaBpm}]");

    if (isSpike) {
      currentState = AlertState.waitingUserResponse;
      currentEvent = VitalsAlertContext(
        timestamp: _timeService.now,
        baselinePulse: _baselinePulse,
        currentPulse: currentPulse,
        pulseHistoryLast5Min: List.from(_pulseBuffer),
        thresholds: doctorThresholds, // 🎯 Pass Chief Doctor personal thresholds to context
      );
      _lastCheckTime = _timeService.now;

      debugPrint("🚨 [VitalsMonitor]: SPIKE DETECTED BY DOCTOR THRESHOLDS! Transition to AlertState.waitingUserResponse.");
      notifyListeners(); // 🚀 Send signal to UI
    }
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
      currentState = AlertState.trackingInBackground;
      _lastCheckTime = _timeService.now;
      debugPrint("🏃 [VitalsMonitor]: Workout confirmed. Transitioning to background mode.");
      notifyListeners();
    } else {
      currentState = AlertState.inChatWithAthena;
      debugPrint("🩺 [VitalsMonitor]: Workout declined. Launching emergency Athena trigger...");
      notifyListeners();

      // 🎯 Instant Athena activation outside the 4-hour schedule
      await athenaTriggerService.triggerVitalsSpike(
        vaultService: _vaultService,
        timeService: _timeService,
        cascadeController: cascadeController,
        alertContext: currentEvent!,
      );
    }
  }

  /// Time simulator listener (TimeService)
  void _onTimeShifted() {
    // 🎯 1. In standby mode during time shift, recheck the last pulse (if at least 3 points exist)
    if (currentState == AlertState.idle && _pulseBuffer.length >= 3) {
      debugPrint("⏰ [VitalsMonitor]: Time jump intercepted. Analyzing the last reading...");
      _analyzeForSpike(_pulseBuffer.last);
    }

    // 2. In background workout tracking
    if (currentState == AlertState.trackingInBackground && _lastCheckTime != null) {
      final minutesPassed = _timeService.now.difference(_lastCheckTime!).inMinutes;

      // Check every 10 simulated minutes
      if (minutesPassed >= 10 && _pulseBuffer.isNotEmpty) {
        _lastCheckTime = _timeService.now;
        _checkBackgroundRecovery(_pulseBuffer.last);
      }
    }
  }

  /// Background check for pulse recovery to normal
  void _checkBackgroundRecovery(int currentPulse) {
    // Recovery is considered successful if pulse returned to baseline (+15%)
    if (currentPulse <= (_baselinePulse * 1.15)) {
      debugPrint("✅ [VitalsMonitor]: Pulse recovered ($currentPulse bpm). Resetting state.");
      reset();
    }
  }

  /// Complete state reset to standby
  void reset() {
    currentState = AlertState.idle;
    currentEvent = null;
    _lastCheckTime = null;
    _pulseBuffer.clear();
    debugPrint("🔄 [VitalsMonitor]: Service reset to standby mode (AlertState.idle)");
    notifyListeners();
  }

  @override
  void dispose() {
    _timeService.removeListener(_onTimeShifted);
    super.dispose();
  }
}