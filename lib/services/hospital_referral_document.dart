import 'vault_service.dart';

/// Service for generating a clinical discharge summary during dynamic decompensation
class DecompensationReportCascadeService {
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

  /// Launching the clinic document preparation relay
  Future<String> executeDecompensationReport({
    required VaultService vault,
    required Future<String> Function(String prompt, String model) callGemini,
    required Map<String, dynamic> Function(String raw) parseJson,
    required Function(double progress) onProgressUpdate,
  }) async {
    onProgressUpdate(0.2);

    // 1. Collection of context and Athena logs
    final String initialContext = vault.getAggregatedContextForMasterWithFlash();
    final String athenaDiary = vault.getAthenaLogsSummaryForPro();
    final List<Map<String, dynamic>> rawLogs = vault.getAthenaClinicalLogs();

    // 2. Dynamic calculation of the observation period
    String periodDescription = "For the current observation period";
    if (rawLogs.isNotEmpty) {
      final firstTimestamp = rawLogs.first['timestamp'] ?? '';
      final lastTimestamp = rawLogs.last['timestamp'] ?? '';
      periodDescription = "Observation period: from $firstTimestamp to $lastTimestamp (total records: ${rawLogs.length})";
    }

    // -----------------------------------------------------------------
    // STAGE 1: GEMINI 3.6 FLASH (DECOMPENSATION DYNAMICS ANALYSIS)
    // -----------------------------------------------------------------
    final String flashPrompt = """
You are the senior clinical analyst of the Aegis expert medical system.
Your task is to analyze the dynamics of the patient's condition over the entire period of their communication with the Curator (Athena) and identify key decompensation points.

OBSERVATION METADATA:
$periodDescription

INITIAL PATIENT STATUS AND DAY 1 CASCADE:
$initialContext

CURATOR CLINICAL OBSERVATION DIARY (ATHENA LOGS):
$athenaDiary

YOUR TASK:
Compare the patient's initial state with the current one, highlight negative trends, and form an initial chronological breakdown of the negative dynamics.

CRITICAL JSON FORMATTING RULE:
Inside string values, it is STRICTLY FORBIDDEN to use unescaped double quotes. Use guillemets « » or ' '.

MANDATORY JSON RESPONSE FORMAT:
{
  "decompensation_analysis": "CLINICAL DYNAMICS AND SHIFT POINTS:\n\n1. TREND BREAKING POINT:\n[Indicate the date/time from the diary when the condition steadily degraded].\n\n2. COMPARATIVE DYNAMICS (START VS CURRENT DAY):\n[Analysis of symptoms and vital markers in dynamics over the entire period].\n\n3. LEADING THREATS AND DECOMPENSATION SYNDROMES:\n[Clinical syndromes requiring an in-person doctor's examination]."
}
""";

    String flashAnalysis = "";
    try {
      final String rawFlash = await callGemini(flashPrompt, _flashModel);
      try {
        final rawParsed = parseJson(rawFlash);
        if (rawParsed is Map<String, dynamic>) {
          flashAnalysis = _ensureString(rawParsed['decompensation_analysis']);
        }
      } catch (_) {
        flashAnalysis = _extractKeyFallback(rawFlash, "decompensation_analysis");
        if (flashAnalysis.isEmpty) flashAnalysis = rawFlash;
      }
    } catch (e) {
      flashAnalysis = "Flash analysis error: $e";
    }

    onProgressUpdate(0.6);

    // -----------------------------------------------------------------
    // STAGE 2: GEMINI 3.1 PRO (GENERATING THE OFFICIAL DISCHARGE SUMMARY FOR THE PHYSICIAN)
    // -----------------------------------------------------------------
    final String proPrompt = """
You are the Chief Physician of the Aegis medical system. 
Based on the accumulated observations of the Curator (Athena) and the initial review by the Head of Department (Flash), form an OFFICIAL CLINICAL DISCHARGE SUMMARY FOR AN IN-PERSON CLINIC / HOSPITAL PHYSICIAN.

PERIOD INFORMATION:
$periodDescription

DYNAMICS ANALYSIS FROM FLASH:
$flashAnalysis

FULL ATHENA DIARY:
$athenaDiary

Form a FINAL DOCUMENT IN MARKDOWN FORMAT that the patient can print out or show to the doctor during the appointment.

DOCUMENT STRUCTURE (FORMAT BEAUTIFULLY IN MARKDOWN):
# CLINICAL DISCHARGE SUMMARY (AEGIS HEALTH RECORD)
**Generation Date:** [Current date and time]
**$periodDescription**

## 1. Summary of the Clinical Situation
[Brief medical introduction: why an in-person appointment is recommended for the patient, leading syndromes].

## 2. Decompensation Point Detection
**Date/time of the first manifestation of the negative trend:** [Indicate date from diary]
**Nature of the shift:** [Description]

## 3. Observation Chronomap (Dynamics over the entire period)
Form a clear Markdown table with columns:
| Date | Status | Subjective Complaints | Objective Markers (HR/SpO2/BP) | Risk Assessment |

## 4. Leading Pathophysiological Syndromes
[Academic description of identified syndromes for the receiving physician].

## 5. Recommended Primary Examination Plan for the Clinic
[List of tests, ultrasound, ECG, CT/MRI that should be performed first].

MANDATORY JSON RESPONSE FORMAT:
{
  "medical_report_markdown": "<Here is the full text of the ready Markdown document>"
}
""";

    String finalMarkdownReport = "";
    try {
      final String rawPro = await callGemini(proPrompt, _proModel);
      try {
        final rawParsed = parseJson(rawPro);
        if (rawParsed is Map<String, dynamic>) {
          finalMarkdownReport = _ensureString(rawParsed['medical_report_markdown']);
        }
      } catch (_) {
        finalMarkdownReport = _extractKeyFallback(rawPro, "medical_report_markdown");
        if (finalMarkdownReport.isEmpty) finalMarkdownReport = rawPro;
      }

      // Save the final document in VaultService
      vault.updateSystemFolder('global', {
        'hospital_referral_document': finalMarkdownReport,
        'hospital_referral_timestamp': DateTime.now().toIso8601String(),
      });

    } catch (e) {
      finalMarkdownReport = "# Summary Generation Error\nPro model failure: $e";
    }

    onProgressUpdate(1.0);
    return finalMarkdownReport;
  }
}