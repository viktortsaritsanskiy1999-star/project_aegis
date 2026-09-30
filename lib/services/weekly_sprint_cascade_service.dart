import 'vault_service.dart';

/// Background weekly board service (Deputy Flash -> Chief Physician Pro)
class WeeklySprintCascadeService {
  static const String _flashModel = "gemini-3.6-flash";
  static const String _proModel = "gemini-3.1-pro-preview";

  String _ensureString(dynamic input, {String fallback = ''}) {
    if (input == null) return fallback;
    if (input is String) return input;
    if (input is List) return input.map((e) => e.toString()).join('\n');
    if (input is Map) return input.entries.map((e) => '${e.key}: ${e.value}').join('\n');
    return input.toString();
  }

  String _extractKeyFallback(String rawJson, String keyName) {
    try {
      final pattern = RegExp('"$keyName"\\s*:\\s*"([\\s\\S]*?)"(?=\\s*,\\s*"|\\s*})');
      final match = pattern.firstMatch(rawJson);
      if (match != null && match.groupCount >= 1) {
        return match.group(1)!.replaceAll(r'\"', '"').replaceAll(r'\n', '\n');
      }
    } catch (_) {}
    return '';
  }

  /// Launching the weekly background strategy recalculation relay
  Future<String> executeWeeklySprintCascade({
    required VaultService vault,
    required Future<String> Function(String prompt, String model) callGemini,
    required Map<String, dynamic> Function(String raw) parseJson,
    required Function(double progress) onProgressUpdate,
  }) async {
    onProgressUpdate(0.2);

    // 1. Collection of context and Athena logs for the past week
    final String initialContext = vault.getAggregatedContextForMasterWithFlash();
    final String athenaDiary = vault.getAthenaLogsSummaryForPro();

    // -----------------------------------------------------------------
    // STAGE 1: GEMINI 3.6 FLASH (DEPUTY - WEEKLY PROGRESS ANALYSIS)
    // -----------------------------------------------------------------
    final String flashPrompt = """
You are the Head of Department (Aegis Clinical Analyst). 
1 week (7 days) of patient monitoring has passed. Your task is to compare the weekly dynamics with the original plan and prepare a summary for the Chief Physician.

INITIAL PATIENT STATUS AND PLAN:
$initialContext

ATHENA'S WEEKLY DIARY (ATHENA LOGS):
$athenaDiary

YOUR TASK:
1. Highlight the patient's key successes over 7 days (symptom dynamics, adherence to recommendations).
2. Identify bottlenecks or emerging complaints.
3. Formulate a short proposal for the Chief Physician: whether the current strategy needs to be adjusted for the next week.

CRITICAL JSON FORMATTING RULE:
Inside string values, it is STRICTLY FORBIDDEN to use unescaped double quotes. Use guillemets « » or ' '.

MANDATORY JSON RESPONSE FORMAT:
{
  "weekly_deputy_analysis": "WEEK/WEEKLY SUMMARY:\n\n1. SYMPTOM DYNAMICS OVER 7 DAYS:\n[Analysis]\n\n2. BOTTLENECKS AND DEVIATIONS:\n[Description]\n\n3. ADJUSTMENT PROPOSAL:\n[Ideas for the Chief Physician]"
}
""";

    String flashAnalysis = "";
    try {
      final String rawFlash = await callGemini(flashPrompt, _flashModel);
      try {
        final rawParsed = parseJson(rawFlash);
        if (rawParsed is Map<String, dynamic>) {
          flashAnalysis = _ensureString(rawParsed['weekly_deputy_analysis']);
        }
      } catch (_) {
        flashAnalysis = _extractKeyFallback(rawFlash, "weekly_deputy_analysis");
        if (flashAnalysis.isEmpty) flashAnalysis = rawFlash;
      }
    } catch (e) {
      flashAnalysis = "Flash analysis error: $e";
    }

    onProgressUpdate(0.6);
    // 🎯 SAVE FLASH CONCLUSIONS TO VAULT SHELVES
    vault.updateFlashShelves(
      general: flashAnalysis,
      training: "Weekly analytical slice from the Deputy Chief Physician (Flash)",
    );

    // -----------------------------------------------------------------
    // STAGE 2: GEMINI 3.1 PRO (CHIEF PHYSICIAN — STRATEGY CORRECTION AND PROMPTER FOR ATHENA)
    // -----------------------------------------------------------------
    final String proPrompt = """
You are the Chief Physician of the Aegis expert system.
Based on the weekly review by the Head of Department (Flash) and the Curator's (Athena) diary, update the working strategy for the NEXT WEEK.

WEEKLY ANALYSIS FROM FLASH:
$flashAnalysis

ATHENA'S DIARY:
$athenaDiary

YOUR TASK:
1. Confirm or adjust the current medical strategy.
2. Give Athena updated focus tasks (what to look out for in the next 7 days).

MANDATORY JSON RESPONSE FORMAT:
{
  "updated_weekly_strategy": "UPDATED WEEKLY STRATEGY:\n[Strategy text for the Curator]"
}
""";

    String updatedStrategy = "";
    try {
      final String rawPro = await callGemini(proPrompt, _proModel);
      try {
        final rawParsed = parseJson(rawPro);
        if (rawParsed is Map<String, dynamic>) {
          updatedStrategy = _ensureString(rawParsed['updated_weekly_strategy']);
        }
      } catch (_) {
        updatedStrategy = _extractKeyFallback(rawPro, "updated_weekly_strategy");
        if (updatedStrategy.isEmpty) updatedStrategy = rawPro;
      }

      // Save Pro conclusions to cascade vault shelves
      vault.updateProShelves(
        general: updatedStrategy,
        training: "Deep correction of the Chief Physician's strategy based on weekly dynamics",
      );

      // Save updated strategy to the global block
      vault.updateSystemFolder('global', {
        'current_weekly_strategy': updatedStrategy,
        'last_weekly_cascade_timestamp': DateTime.now().toIso8601String(),
      });

    } catch (e) {
      updatedStrategy = "Pro strategy generation error: $e";
    }

    onProgressUpdate(1.0);
    return updatedStrategy;
  }
}