import 'package:flutter/foundation.dart';
import 'vault_service.dart';
import 'curator_agent.dart';
import 'vitals_alert_context.dart';
import 'weekly_sprint_cascade_service.dart';
import 'brain_service.dart';

class AegisCascadeController extends ChangeNotifier {
  final VaultService vaultService;
  final CuratorAgent curatorAgent;
  final WeeklySprintCascadeService weeklySprintCascadeService;
  final BrainService brainService; // 👈 Adding brainService field

  bool _isLoading = false;
  bool get isLoading => _isLoading;

  String? _lastCuratorResponse;
  String? get lastCuratorResponse => _lastCuratorResponse;

  AegisCascadeController({
    required this.vaultService,
    CuratorAgent? curatorAgent,
    WeeklySprintCascadeService? weeklySprintCascadeService,
    BrainService? brainService, // 👈 Pass or create by default
  })  : curatorAgent = curatorAgent ?? CuratorAgent(),
        weeklySprintCascadeService =
            weeklySprintCascadeService ?? WeeklySprintCascadeService(),
        brainService = brainService ?? BrainService();

  /// 1. Initial greeting (triggered after Pro-model analysis)
  Future<String?> generateInitialGreeting({
    required String proGeneralOutput,
    required String proTrainingOutput,
  }) async {
    _isLoading = true;
    notifyListeners();

    try {
      vaultService.updateProShelves(
        general: proGeneralOutput,
        training: proTrainingOutput,
      );

      curatorAgent.initializeSession(vault: vaultService);

      _lastCuratorResponse = await curatorAgent.generateInitialGreeting(
        vault: vaultService,
      );

      return _lastCuratorResponse;
    } catch (e) {
      debugPrint('[AegisCascadeController Greeting Error]: $e');
      rethrow;
    } finally {
      _isLoading = false;
      notifyListeners();
    }
  }

  /// 2. User reply
  Future<String?> handleUserReply(String text) async {
    _isLoading = true;
    notifyListeners();

    try {
      curatorAgent.initializeSession(vault: vaultService);

      final int currentDay = vaultService.getAthenaSprintDay();

      _lastCuratorResponse = await curatorAgent.handleUserReply(
        text,
        sprintDay: currentDay,
        vault: vaultService,
      );
      return _lastCuratorResponse;
    } catch (e) {
      debugPrint('[AegisCascadeController Reply Error]: $e');
      rethrow;
    } finally {
      _isLoading = false;
      notifyListeners();
    }
  }

  /// Daily pulse from timer / +24h
  Future<String?> handleDailyPulse() async {
    _isLoading = true;
    notifyListeners();

    try {
      curatorAgent.initializeSession(vault: vaultService);

      final int currentDay = vaultService.getAthenaSprintDay();

      _lastCuratorResponse = await curatorAgent.handleDailyPulse(
        sprintDay: currentDay,
        vault: vaultService,
      );
      return _lastCuratorResponse;
    } catch (e) {
      debugPrint('[AegisCascadeController Pulse Error]: $e');
      rethrow;
    } finally {
      _isLoading = false;
      notifyListeners();
    }
  }

  /// 4. Handling emergency pulse spike (transition from VitalsMonitor)
  Future<String?> handleVitalsSpikeAlert(VitalsAlertContext alertContext) async {
    _isLoading = true;
    notifyListeners();

    try {
      // Pull fresh session from Vault
      curatorAgent.initializeSession(vault: vaultService);

      // Generate Athena's reaction to tachycardia
      _lastCuratorResponse = await curatorAgent.handleVitalsSpikeAlert(
        alertContext: alertContext,
        vault: vaultService,
      );

      return _lastCuratorResponse;
    } catch (e) {
      debugPrint('[AegisCascadeController Vitals Alert Error]: $e');
      rethrow;
    } finally {
      _isLoading = false;
      notifyListeners();
    }
  }

  /// 5. Background strategy recalculation by the medical board (Flash -> Pro) and Athena's report on the 7th day of the sprint
  Future<void> runWeeklyBackgroundCascade() async {
    try {
      debugPrint("🌀 [AegisCascadeController]: Starting 7th-day background cascade (Deputy -> Chief Physician)...");

      // 1. Launch technical medical board (updates vault shelves)
      await weeklySprintCascadeService.executeWeeklySprintCascade(
        vault: vaultService,
        callGemini: (prompt, model) async {
          return await brainService.callGeminiRaw(prompt, model);
        },
        parseJson: (raw) => brainService.parseJsonHelper(raw),
        onProgressUpdate: (progress) {
          debugPrint("⏳ [Weekly Cascade Progress]: ${(progress * 100).toInt()}%");
        },
      );

      debugPrint("🎉 [AegisCascadeController]: Weekly technical recalculation completed. Requesting report from Athena...");

      // 2. Initialize Athena's session and request a live weekly summary
      curatorAgent.initializeSession(vault: vaultService);
      final String? athenaWeeklyReport = await curatorAgent.generateWeeklyReport(
        vault: vaultService,
      );

      // 3. Save Athena's report to the curator chat so the user can see it
      if (athenaWeeklyReport != null && athenaWeeklyReport.isNotEmpty) {
        vaultService.saveCuratorChatMessage({
          'sender': 'curator',
          'text': athenaWeeklyReport,
        });
        vaultService.setUnreadAthenaMessage(true);
        debugPrint("✅ [AegisCascadeController]: Athena's weekly report successfully delivered to chat!");
      }
    } catch (e) {
      debugPrint("❌ [AegisCascadeController Error]: 7th-day background cascade error: $e");
    }
  }
}