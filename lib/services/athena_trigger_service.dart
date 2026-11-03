import 'dart:async';
import 'package:flutter/material.dart';
import 'vault_service.dart';
import 'time_service.dart';
import 'auth/auth_session_manager.dart';
import 'api/aegis_api_client.dart';
import 'api/vitals_api_models.dart';
import 'api/medical_ai_api_models.dart';
import 'aegis_cascade_controller.dart';
import 'vitals_alert_context.dart';

class AthenaTriggerService {
  Timer? _checkTimer;
  VoidCallback? _timeListener;
  TimeService? _timeServiceRef;
  VaultService? _vaultServiceRef;
  AegisApiClient? _apiClientRef;
  AuthSessionManager? _authSessionManagerRef;
  VoidCallback? _onWeeklyConsiliumAvailable;
  bool _isProcessing = false;

  bool get isProcessing => _isProcessing;

  void startMonitoring({
    required VaultService vaultService,
    required TimeService timeService,
    required AegisApiClient apiClient,
    required AuthSessionManager authSessionManager,
    AegisCascadeController? cascadeController,
    VoidCallback? onWeeklyConsiliumAvailable,
  }) {
    // 1. Remove old timer and listener
    _cancelSubscriptions();

    _vaultServiceRef = vaultService;
    _timeServiceRef = timeService;
    _apiClientRef = apiClient;
    _authSessionManagerRef = authSessionManager;
    _onWeeklyConsiliumAvailable = onWeeklyConsiliumAvailable;

    // Background check every 10 seconds
    _checkTimer = Timer.periodic(const Duration(seconds: 10), (_) {
      checkAndTrigger();
    });

    // 🎯 Instant reaction when time changes on the DebugTimePanel
    _timeListener = () {
      debugPrint("⏰ [AthenaTriggerService]: Rewind/Forward click caught!");
      checkAndTrigger();
    };

    _timeServiceRef?.addListener(_timeListener!);

    // Initial check right upon startup
    checkAndTrigger();
  }

  /// Evaluates schedule eligibility and executes authenticated server turn for daily pulse.
  /// Returns true if a daily pulse turn was successfully executed and persisted.
  Future<bool> checkAndTrigger({
    VaultService? vaultService,
    TimeService? timeService,
    AegisApiClient? apiClient,
    AuthSessionManager? authSessionManager,
    VoidCallback? onWeeklyConsiliumAvailable,
  }) async {
    final vault = vaultService ?? _vaultServiceRef;
    final time = timeService ?? _timeServiceRef;
    final api = apiClient ?? _apiClientRef;
    final auth = authSessionManager ?? _authSessionManagerRef;
    final onWeekly = onWeeklyConsiliumAvailable ?? _onWeeklyConsiliumAvailable;

    if (vault == null || time == null || api == null || auth == null) {
      debugPrint("⏳ [AthenaTriggerService]: Missing required dependencies for daily pulse check.");
      return false;
    }

    if (_isProcessing) {
      debugPrint("⏳ [AthenaTriggerService]: Daily pulse check already in progress. Skipping.");
      return false;
    }

    // 1. Authentication and Patient Attribution pre-check
    if (!auth.isAuthenticated) {
      debugPrint("🔒 [AthenaTriggerService]: User not authenticated. Daily pulse sleeping.");
      return false;
    }

    final String? startingPatientId = auth.confirmedAttributionPatientId;
    if (startingPatientId == null || startingPatientId.isEmpty) {
      debugPrint("🔒 [AthenaTriggerService]: No confirmed patient attribution. Daily pulse sleeping.");
      return false;
    }

    // 2. Fencing pre-capture: session epoch and cycle ID
    final int startingEpoch = vault.sessionEpoch;
    final String? startingCycleId = vault.consiliumCycleId;

    // Requirement 5: Never send a daily_pulse before confirmed Primary Consilium completion
    if (!vault.isConsiliumCompleted || startingCycleId == null || startingCycleId.isEmpty) {
      debugPrint("⏳ [AthenaTriggerService]: Primary Consilium not completed for active cycle. Daily pulse sleeping.");
      return false;
    }

    // 3. Anchor T0 check: initial post-Primary greeting must precede daily pulse
    // Requirement 3: Preserve the first post-Primary initial_greeting as anchor T0.
    final bool isHistoryEmpty = vault.getCuratorChatHistory().isEmpty;
    int? lastPromptTimestamp = vault.getAthenaLastPromptTime();
    final DateTime now = time.now;

    // If chat history is empty — wait for initial greeting turn from CuratorChatScreen
    if (isHistoryEmpty) {
      debugPrint("🌱 [AthenaTriggerService]: Chat history is empty. Waiting for first post-Primary greeting...");
      return false;
    }

    // If history is not empty, but timestamp is null/0 — anchor T0 to current time
    if (lastPromptTimestamp == null || lastPromptTimestamp == 0) {
      vault.updateAthenaLastPromptTime(now.millisecondsSinceEpoch);
      debugPrint("📌 [AthenaTriggerService]: Schedule anchor T0 initialized from existing initial greeting.");
      return false;
    }

    // If time was reset backwards (clock rewind) — synchronize timestamp
    if (lastPromptTimestamp > now.millisecondsSinceEpoch) {
      vault.updateAthenaLastPromptTime(now.millisecondsSinceEpoch);
      debugPrint("🔄 [AthenaTriggerService]: Clock rewound backwards. Athena timestamp synchronized.");
      return false;
    }

    // 4. Elapsed time & calendar boundary check (>= 20 hours and new calendar day)
    final DateTime lastPromptDate = DateTime.fromMillisecondsSinceEpoch(lastPromptTimestamp);
    final int hoursDiff = now.difference(lastPromptDate).inHours;
    final bool isNewDay = time.isNewCalendarDay(lastPromptDate);

    debugPrint("🔍 [AthenaTriggerService]: $hoursDiff hrs elapsed. New calendar day: $isNewDay");

    if (hoursDiff < 20 || !isNewDay) {
      return false;
    }

    // Mark processing to prevent concurrent triggers
    _isProcessing = true;

    try {
      // 5. Query authoritative server cascade status (Requirement 6)
      final status = await api.getCascadeStatus();

      // Async boundary fence check after getCascadeStatus
      if (!auth.isAuthenticated ||
          auth.confirmedAttributionPatientId != startingPatientId ||
          vault.sessionEpoch != startingEpoch ||
          vault.consiliumCycleId != startingCycleId) {
        debugPrint("⚠️ [AthenaTriggerService]: Session/Patient/Cycle changed during status check. Aborting daily pulse.");
        return false;
      }

      // Authoritative validation: cycle must be active and Primary must be confirmed completed on server
      if (!status.hasActiveCycle ||
          !status.isPrimaryConsiliumCompleted ||
          status.activeCycleId != startingCycleId) {
        debugPrint("⚠️ [AthenaTriggerService]: Server cascade status indicates Primary incomplete or cycle mismatch. Aborting.");
        return false;
      }

      // One Outstanding Routine Question (Stage A2.1):
      // Do not generate another scheduled routine question while previous routine question is unanswered
      if (status.hasUnansweredRoutineQuestion) {
        debugPrint("⏳ [AthenaTriggerService]: Server has unanswered routine question '${status.activeRoutineQuestionId}'. Skipping daily pulse.");
        return false;
      }

      final int currentSprintDay = vault.getAthenaSprintDay();

      // Continuous Accompaniment:
      // When current sprint day has reached 7, or next sprint can be started,
      // or calendar week > 1 with weekly consilium unfinished,
      // notify UI/Atlas that Weekly Consilium is eligible, but do NOT block daily pulse.
      final bool weeklyEligible = currentSprintDay >= 7 ||
          status.canStartNextSprint ||
          (status.currentCalendarWeek != null &&
              status.currentCalendarWeek! > 1 &&
              !status.isWeeklyConsiliumCompleted);
      if (weeklyEligible) {
        debugPrint("🏁 [AthenaTriggerService]: Weekly Consilium eligible. Notifying availability.");
        onWeekly?.call();
      }

      // Authoritatively resolve weekly sprint number from server status and vault
      final int sprintNumber = status.currentCalendarWeek ?? resolveAuthoritativeSprintNumber(
        status: status,
        vault: vault,
      );

      // Calculate target sprint day (bounded 1..7)
      final int targetSprintDay = status.currentCalendarDay ?? ((currentSprintDay >= 7) ? 1 : (currentSprintDay + 1));

      // Authoritative multi-week sprint-scoped idempotency key
      final String idempotencyKey =
          'athena_daily_pulse_${startingCycleId}_w${sprintNumber}_d$targetSprintDay';

      // Check if this exact daily pulse key was already recorded in vault
      final Map<String, dynamic> globalFolder = vault.getSystemContext('global');
      final String? lastPulseKey = globalFolder['last_daily_pulse_key'] as String?;
      if (lastPulseKey == idempotencyKey) {
        debugPrint("🔁 [AthenaTriggerService]: Daily pulse for day $targetSprintDay ($idempotencyKey) was already recorded. Skipping.");
        return false;
      }

      debugPrint("🚀 [AthenaTriggerService]: Sending authenticated daily_pulse (Week $sprintNumber Day $targetSprintDay, key: $idempotencyKey)...");

      // Requirement 1 & 2: Send turn to authenticated backend Athena endpoint (Zero local Gemini SDK)
      final request = AthenaChatRequest.createTurn(
        eventType: 'daily_pulse',
        sprintNumber: sprintNumber,
        sprintDay: targetSprintDay,
        idempotencyKey: idempotencyKey,
      );

      final response = await api.sendAthenaMessage(request);

      // Requirement 5 & 8: Post-async boundary fence check
      if (!auth.isAuthenticated ||
          auth.confirmedAttributionPatientId != startingPatientId ||
          vault.sessionEpoch != startingEpoch ||
          vault.consiliumCycleId != startingCycleId ||
          response.cycleId != startingCycleId) {
        debugPrint("⚠️ [AthenaTriggerService]: Session/Patient/Cycle mismatch after server response. Dropping response.");
        return false;
      }

      // Requirement 4 & 9: Persist to chat history with deduplication
      final history = vault.getCuratorChatHistory();
      final bool alreadyInHistory = history.isNotEmpty &&
          history.last['sender'] == 'curator' &&
          history.last['text'] == response.reply;

      if (!alreadyInHistory && response.reply.isNotEmpty) {
        vault.saveCuratorChatMessage({
          'sender': 'curator',
          'text': response.reply,
        });
        vault.setUnreadAthenaMessage(true);
      }

      final int nowMs = time.now.millisecondsSinceEpoch;
      vault.updateAthenaLastPromptTime(nowMs);
      vault.setAthenaSprintDay(response.sprintDay ?? targetSprintDay);
      vault.updateSystemFolder('global', {
        'last_daily_pulse_key': idempotencyKey,
      });

      debugPrint("✅ [AthenaTriggerService]: Athena daily pulse day $targetSprintDay successfully processed and persisted.");

      // Requirement 7: Day 7 boundary handling
      if (targetSprintDay >= 7) {
        debugPrint("🏁 [AthenaTriggerService]: Sprint Day 7 completed. Presenting Weekly Consilium availability.");
        onWeekly?.call();
      }

      return true;
    } on AegisNetworkException catch (e) {
      debugPrint("📡 [AthenaTriggerService Network Error]: ${e.message}");
      return false;
    } on AegisTimeoutException catch (e) {
      debugPrint("⏱️ [AthenaTriggerService Timeout Error]: ${e.message}");
      return false;
    } on AegisApiException catch (e) {
      debugPrint("❌ [AthenaTriggerService API Error]: ${e.statusCode} ${e.message}");
      return false;
    } catch (e) {
      debugPrint("❌ [AthenaTriggerService Error]: $e");
      return false;
    } finally {
      _isProcessing = false;
    }
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

  /// Resolves the authoritative weekly sprint number from server cascade status and vault state.
  /// Never guesses or advances based solely on local state.
  static int resolveAuthoritativeSprintNumber({
    required ClientCascadeStatusResponse status,
    required VaultService vault,
  }) {
    // 0. Server authoritative calendar week takes top priority
    if (status.currentCalendarWeek != null && status.currentCalendarWeek! > 0) {
      return status.currentCalendarWeek!;
    }

    // 1. If server status provides explicit weekly_sprint_number
    if (status.weeklySprintNumber != null && status.weeklySprintNumber! > 0) {
      if (status.isWeeklyConsiliumCompleted) {
        // The sprint in status has completed consilium; subsequent daily questions belong to next week (N + 1)
        return status.weeklySprintNumber! + 1;
      }
      return status.weeklySprintNumber!;
    }

    // 2. Parse from currentWeeklySprintId (e.g. 'sprint_cycle_w2' -> 2)
    if (status.currentWeeklySprintId != null) {
      final match = RegExp(r'[_-]w(\d+)$').firstMatch(status.currentWeeklySprintId!);
      if (match != null) {
        final parsed = int.tryParse(match.group(1)!);
        if (parsed != null && parsed > 0) {
          if (status.isWeeklyConsiliumCompleted) {
            return parsed + 1;
          }
          return parsed;
        }
      }
    }

    // 3. Parse from nextSprintId (e.g. 'sprint_cycle_w1' -> 1)
    if (status.nextSprintId != null) {
      final match = RegExp(r'[_-]w(\d+)$').firstMatch(status.nextSprintId!);
      if (match != null) {
        final parsed = int.tryParse(match.group(1)!);
        if (parsed != null && parsed > 0) {
          return parsed;
        }
      }
    }

    // 4. Fallback to Vault's recorded active weekly sprint number
    final vaultSprint = vault.activeWeeklySprintNumber;
    if (vaultSprint != null && vaultSprint > 0) {
      if (status.isWeeklyConsiliumCompleted) {
        return vaultSprint + 1;
      }
      return vaultSprint;
    }

    // 5. Default authoritative baseline for initial cycle post-Primary is Week 1
    return 1;
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
    _vaultServiceRef = null;
    _apiClientRef = null;
    _authSessionManagerRef = null;
    _onWeeklyConsiliumAvailable = null;
  }
}
