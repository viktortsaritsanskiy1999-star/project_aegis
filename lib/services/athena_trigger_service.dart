import 'dart:async';
import 'package:flutter/material.dart';
import 'vault_service.dart';
import 'time_service.dart';
import 'package:aegis_health/services/aegis_cascade_controller.dart';
import 'vitals_alert_context.dart';

class AthenaTriggerService {
  Timer? _checkTimer;
  VoidCallback? _timeListener;
  TimeService? _timeServiceRef;
  bool _isProcessing = false;

  void startMonitoring({
    required VaultService vaultService,
    required TimeService timeService,
    required AegisCascadeController cascadeController,
  }) {
    // 1. Remove old timer and listener
    _cancelSubscriptions();

    _timeServiceRef = timeService;

    // Background check every 10 seconds
    _checkTimer = Timer.periodic(const Duration(seconds: 10), (_) {
      _checkAndTrigger(vaultService, timeService, cascadeController);
    });

    // 🎯 Instant reaction when time changes on the DebugTimePanel
    _timeListener = () {
      debugPrint("⏰ [AthenaTriggerService]: Rewind click caught!");
      _checkAndTrigger(vaultService, timeService, cascadeController);
    };

    _timeServiceRef?.addListener(_timeListener!);

    // Initial check right upon startup
    _checkAndTrigger(vaultService, timeService, cascadeController);
  }

  /// 🚨 URGENT CALL: Emergency trigger on heart rate spike (bypassing the daily timer)
  Future<void> triggerVitalsSpike({
    required VaultService vaultService,
    required TimeService timeService,
    required AegisCascadeController cascadeController,
    required VitalsAlertContext alertContext,
  }) async {
    if (_isProcessing) return;
    _isProcessing = true;

    try {
      debugPrint("🩺 [AthenaTriggerService]: Activating Athena emergency call (Heart rate spike)...");

      final reply = await cascadeController.handleVitalsSpikeAlert(alertContext);

      if (reply != null && reply.isNotEmpty) {
        final DateTime now = timeService.now;

        // Record new contact time
        vaultService.updateAthenaLastPromptTime(now.millisecondsSinceEpoch);

        // Save Athena's response to the chat history
        vaultService.saveCuratorChatMessage({
          'sender': 'curator',
          'text': reply,
        });

        // Set unread message flag
        vaultService.setUnreadAthenaMessage(true);

        debugPrint("✅ [AthenaTriggerService]: Athena successfully took over control during the heart rate spike!");
      }
    } catch (e) {
      debugPrint("❌ [AthenaTriggerService Vitals Error]: $e");
    } finally {
      _isProcessing = false;
    }
  }

  Future<void> _checkAndTrigger(
      VaultService vaultService,
      TimeService timeService,
      AegisCascadeController cascadeController,
      ) async {
    if (_isProcessing) return;

    final bool isHistoryEmpty = vaultService.getCuratorChatHistory().isEmpty;
    int? lastPromptTimestamp = vaultService.getAthenaLastPromptTime();
    final DateTime now = timeService.now;

    // 🎯 1. If history is empty — the timer SLEEPS! Waiting for the first manual generateInitialGreeting() call from UI/Controller.
    if (isHistoryEmpty) {
      debugPrint("🌱 [AthenaTriggerService]: History is empty. Background monitoring is waiting for the first greeting from Athena...");
      return;
    }

    // 2. If history is not empty, but the timestamp was not set yet — record the start
    if (lastPromptTimestamp == null || lastPromptTimestamp == 0) {
      vaultService.updateAthenaLastPromptTime(now.millisecondsSinceEpoch);
      debugPrint("📌 [AthenaTriggerService]: Time reference point initialized after the first introduction.");
      return;
    }

    // 3. If time was reset backwards (rewind) — synchronize the timestamp
    if (lastPromptTimestamp > now.millisecondsSinceEpoch) {
      vaultService.updateAthenaLastPromptTime(now.millisecondsSinceEpoch);
      debugPrint("🔄 [AthenaTriggerService]: Time reset backwards. Athena timestamp synchronized.");
      return;
    }

    final DateTime lastPromptDate = DateTime.fromMillisecondsSinceEpoch(lastPromptTimestamp);
    final int hoursDiff = now.difference(lastPromptDate).inHours;
    final bool isNewDay = timeService.isNewCalendarDay(lastPromptDate);

    debugPrint("🔍 [AthenaTriggerService]: $hoursDiff hrs elapsed. New calendar day: $isNewDay");

    // 4. CHECK FOR 24 HOURS (OR START OF A NEW CALENDAR DAY AT >= 20 HOURS)
    if (hoursDiff >= 20 && isNewDay) {
      _isProcessing = true;

      try {
        // Increment weekly sprint step (1...7)
        final int currentSprintDay = vaultService.incrementAthenaSprintDay();
        debugPrint("🚀 [AthenaTriggerService]: Sending daily pulse to Athena (Day $currentSprintDay of 7)...");

        final reply = await cascadeController.handleDailyPulse();

        if (reply != null && reply.isNotEmpty) {
          vaultService.updateAthenaLastPromptTime(now.millisecondsSinceEpoch);
          vaultService.saveCuratorChatMessage({
            'sender': 'curator',
            'text': reply,
          });
          vaultService.setUnreadAthenaMessage(true);

          debugPrint("✅ [AthenaTriggerService]: Athena successfully asked the daily question!");

          // -------------------------------------------------------------
          // 🔄 BACKGROUND CASCADE TRIGGER ON DAY 7
          // -------------------------------------------------------------
          if (currentSprintDay == 7) {
            debugPrint("🏁 [AthenaTriggerService]: Sprint Day 7 completed! Launching background board...");
            unawaited(cascadeController.runWeeklyBackgroundCascade());
          }
        }
      } catch (e) {
        debugPrint("❌ [AthenaTriggerService Error]: $e");
      } finally {
        _isProcessing = false;
      }
    }
  }

  void _cancelSubscriptions() {
    _checkTimer?.cancel();
    _checkTimer = null;

    if (_timeListener != null && _timeServiceRef != null) {
      _timeServiceRef!.removeListener(_timeListener!);
      _timeListener = null;
    }
  }

  void dispose() {
    _cancelSubscriptions();
    _timeServiceRef = null;
  }
}