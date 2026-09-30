import 'vault_service.dart';

class MasterCascadeService {
  static const String _flashModel = "gemini-3.6-flash";
  static const String _proModel = "gemini-3.1-pro-preview";

  String _ensureString(dynamic input, {String fallback = ''}) {
    if (input == null) return fallback;
    if (input is String) return input;
    if (input is List) return input.map((e) => e.toString()).join('\n');
    if (input is Map) return input.entries.map((e) => '${e.key}: ${e.value}').join('\n');
    return input.toString();
  }

  /// Fallback extraction of a key value from a JSON string if the parser fails due to quotes
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

  /// Launching the Master Cascade relay
  Future<void> executeCascade({
    required VaultService vault,
    required Future<String> Function(String prompt, String model) callGemini,
    required Map<String, dynamic> Function(String raw) parseJson,
    required Function(double progress) onProgressUpdate,
  }) async {
    // -----------------------------------------------------------------
    // STAGE 1: GEMINI 3.6 FLASH (DEPUTY CHIEF PHYSICIAN)
    // -----------------------------------------------------------------
    onProgressUpdate(0.75);

    try {
      final String initialContext = vault.getAggregatedContextForMaster();

      final String flashPrompt = """
You are the senior analytical module "Synthesis and Control" (Head of Department) of the Aegis expert medical system. 
You are the SECOND AND FINAL LINK of the cascade. A primary medical board has already processed the data before you:
1. "Anamnesis" module (M1) — performs STRICTLY primary pathophysiological preprocessing of raw complaints and context. It WORKS BLINDLY and DOES NOT SEE laboratory/instrumental data.
2. 8 organ-specific agents (Flash-Lite) — each deeply analyzes ONLY ITS OWN specialized system based on the anamnesis and general labs.

INPUT DATA (Anamnesis, Laboratory, Conclusions of 8 system agents):
$initialContext

Your goal is to conduct an integral intersystem synthesis, form your own picture of the patient's condition, and provide a strict, objective audit of the previous links' work.

CRITICAL JSON FORMATTING RULE:
Inside string values, it is STRICTLY FORBIDDEN to use unescaped double quotes. To highlight terms and quotes, use exclusively guillemets « » or single quotes ' '.

MANDATORY JSON RESPONSE FORMAT (STRICTLY OBSERVE KEYS):
{
  "flash_general_shelf": "GENERAL CONCLUSION FOR THE USER (STRICTLY WITHOUT DIAGNOSIS FORMULATION).\n\n1. GENERAL SUMMARY:\n[Brief integral summary of the organism's functional state]\n\n2. SYSTEM SYNTHESIS:\n- CVS: [Flash facts and brief commentary up to 15 words]\n- GI TRACT: [Flash facts and brief commentary up to 15 words]\n- KIDNEYS: [Flash facts and brief commentary up to 15 words]\n- ENDOCRINOLOGY: [Flash facts and brief commentary up to 15 words]\n- IMMUNE: [Flash facts and brief commentary up to 15 words]\n- NERVOUS SYSTEM: [Flash facts and brief commentary up to 15 words]\n- RESPIRATORY: [Flash facts and brief commentary up to 15 words]\n- MUSCULOSKELETAL: [Flash facts and brief commentary up to 15 words]",

  "flash_training_shelf": "FLASH TRAINING SANDBOX:\n\n1. PATHOPHYSIOLOGICAL HYPOTHESES AND DIAGNOSES:\n[Full freedom: put forward any reasonable clinical hypotheses, syndromes, and nosologies. Do not hold back on expressions and terms, but STRICTLY without far-fetched conclusions].\n\n2. DATA DEFICIT AND INSTRUMENTATION:\n[A clear list of what was critically lacking to confirm diagnoses: specific laboratory markers, CT, MRI, Doppler ultrasound, biopsy, etc.].\n\n3. ERROR AND NOISE ASSESSMENT:\n[Assessment of the probability of error or noise in the input data (e.g., Low 15%, Medium 40%, High)].\n\n4. AUDIT OF PREVIOUS MODELS:\n- General assessment: [Brief summary of the entire primary board's work].\n- Point-by-point analysis of errors and blind spots: [Go through the models: M1 and 8 Flash-Lite organ complexes (do not evaluate the ECG model). Keep in mind that M1 does not see lab tests — do not criticize it for their absence! Indicate specifically which model made an error, exactly what the error was, and why it happened]."
}
""";

      final String rawFlash = await callGemini(flashPrompt, _flashModel);

      print("=================== [DEBUG FLASH: RAW RESPONSE] ===================");
      print(rawFlash);
      print("==================================================================");

      Map<String, dynamic> flashParsed = {};

      try {
        final dynamic rawParsed = parseJson(rawFlash);
        if (rawParsed is Map<String, dynamic>) {
          flashParsed = rawParsed;
        } else if (rawParsed is List && rawParsed.isNotEmpty && rawParsed.first is Map) {
          flashParsed = Map<String, dynamic>.from(rawParsed.first);
        }
      } catch (jsonErr) {
        print("⚠️ [MasterCascade Flash]: jsonDecode error ($jsonErr). Running fallback parser...");
        final String generalFallback = _extractKeyFallback(rawFlash, "flash_general_shelf");
        final String trainingFallback = _extractKeyFallback(rawFlash, "flash_training_shelf");

        flashParsed = {
          'flash_general_shelf': generalFallback.isNotEmpty ? generalFallback : rawFlash,
          'flash_training_shelf': trainingFallback.isNotEmpty ? trainingFallback : 'Raw logical response recovered from text.',
        };
      }

      vault.updateFlashShelves(
        general: _ensureString(flashParsed['flash_general_shelf'], fallback: 'Flash summary unavailable'),
        training: _ensureString(flashParsed['flash_training_shelf'], fallback: 'Flash logs unavailable'),
      );

      print("✅ [VaultService]: Deputy Chief Physician (Flash) successfully saved!");
    } catch (e) {
      print("❌ Error during Gemini Flash stage: $e");
      vault.updateFlashShelves(
        general: 'Flash synthesis execution error',
        training: 'Flash model failure: $e',
      );
    }

    onProgressUpdate(0.85);

    // -----------------------------------------------------------------
    // STAGE 2: GEMINI 3.1 PRO (CHIEF PHYSICIAN — SUPREME SYNTHESIS)
    // -----------------------------------------------------------------
    try {
      final String fullMasterContext = vault.getAggregatedContextForMasterWithFlash();

      final String proPrompt = """
You are the supreme clinical expert "Chief Physician" of the Aegis expert medical system. 
You are the FINAL CONCLUDING LINK of the entire Aegis architecture.

FULL PATIENT DOSSIER AND BOARD PROGRESS:
$fullMasterContext

CASCADE STRUCTURE FOR YOUR UNDERSTANDING:
1. "Anamnesis" module (M1) — works STRICTLY without lab tests (blindly), filters primary anamnesis.
2. 8 organ-specific agents (Flash-Lite) — narrow specialists by organ complexes.
3. Head of Department (Flash 3.6) — conducts primary intersystem synthesis and audit.

Your goal is to study the primary context, conclusions of the 8 agents, and the interim audit from the Head of Department (Flash), after which you will issue the FINAL VERDICT of the Aegis system.

IMPORTANT (SMARTWATCHES AND PESSIMUM ZONE):
The user wears a smartwatch / fitness tracker that monitors their heart rate in real-time. Based on age, baseline state, normal resting heart rate, existing cardiac risks, and medications taken, YOU MUST CALCULATE AND INDIVIDUALLY SET the personal **Pessimum Zone (Critical Heart Rate Thresholds)**. 
Do not use standard default values; calculate the figures based on the patient's specific clinical case!
If these boundaries are breached, the Aegis system will trigger an emergency survey of the user (clarifying physical activity, chest pain, and well-being) with the involvement of the Curator.

CRITICAL JSON FORMATTING RULE:
Inside string values, it is STRICTLY FORBIDDEN to use unescaped double quotes. To highlight terms, use guillemets « » or single quotes ' '.

MANDATORY JSON RESPONSE FORMAT (STRICTLY OBSERVE KEYS):
{
  "vitals_thresholds": {
    "min_pessimum_bpm": <number: individual critical minimum resting heart rate threshold for this patient>,
    "max_pessimum_bpm": <number: individual critical maximum resting heart rate threshold for this patient>,
    "max_spike_delta_bpm": <number: allowable sharp heart rate spike in a short time from the baseline resting level>,
    "clinical_rationale": "<string: brief clinical rationale for these specific heart rate thresholds>"
  },

  "pro_general_shelf": "STRICT CLINICAL PROMPTER AND STRATEGY FOR THE CURATOR MODEL:\n\n1. UNQUESTIONABLE CLINICAL BASIS (HIGHEST ACCURACY):\n[Precisely accurate, academic, and unambiguous extraction of key pathological findings, syndromes, and laboratory-instrumental deviations that you are 100% confident in. No simplifications and 'popular medicine' — provide deep clinical essence for the receiving model].\n\n2. PERSONAL HEART RATE MONITORING (SMARTWATCHES):\n- Justify the heart rate thresholds set in vitals_thresholds (min_pessimum_bpm, max_pessimum_bpm, and allowable spike delta max_spike_delta_bpm).\n- Instruct the Curator on what specific symptoms to watch out for if the heart rate spike trigger activates outside of physical activity (e.g., pain radiation, shortness of breath, dizziness, sensation of skipped heartbeats).\n\n3. ROUTE AND ADAPTIVE SCREENING FOR THE CURATOR:\n- Assign a limit to the Curator: no more than 3 clarifying questions per day (especially for the first week).\n- Clearly outline milestones and the vector of questions that the Curator should progressively gather from the user.\n- Describe response evaluation criteria:\n  * IF THE RESPONSE IS HIGH-QUALITY (informative, accurate, on point): The Curator records the fact and moves to the next point of the screening plan.\n  * IF THE RESPONSE IS LOW-QUALITY (blurry, disjointed, evasive, contradictory): The Curator STRICTLY DOES NOT MOVE FURTHER ALONG THE PLAN. They instantly change tactics: break down the question, provide alternative options, or approach the symptom from another pathophysiological angle.",

  "pro_training_shelf": "SUPREME TRAINING SANDBOX OF THE CHIEF PHYSICIAN (PRO):\n\n1. FINAL DIAGNOSTIC NOSOLOGIES AND SYNDROMES:\n[Full clinical freedom: put forward your own autonomous pathophysiological hypotheses, syndromes, and diagnoses. Use deep medical terminology. Be an adequate reviewer: question Flash's versions only in the presence of real contradictions or if the hypothesis is strongly far-fetched. Constructive continuity is welcome].\n\n2. FINAL FURTHER EXAMINATION PLAN:\n[The most accurate and justified list of necessary laboratory and instrumental studies (CT, MRI, specific panel tests) to fully close questions regarding diagnoses].\n\n3. SUPREME CASCADE AUDIT:\n- General board verdict: [How well did the entire chain work: M1 -> 8 Systems -> Flash].\n- Specific errors and blind spots: [Indicate specifically for each model (including Flash and M1) where there were omissions, erroneous conclusions, or missed intersystem connections, explaining the reasons. Reminder: M1 works in blind mode and DOES NOT SEE laboratory/tests, it is strictly forbidden to scold M1 for the absence of tests and blood parameters in its conclusions!]."
}
""";

      final String rawPro = await callGemini(proPrompt, _proModel);

      print("=================== [DEBUG PRO: RAW RESPONSE] ===================");
      print(rawPro);
      print("================================================================");

      Map<String, dynamic> proParsed = {};

      try {
        final dynamic rawParsed = parseJson(rawPro);
        if (rawParsed is Map<String, dynamic>) {
          proParsed = rawParsed;
        } else if (rawParsed is List && rawParsed.isNotEmpty && rawParsed.first is Map) {
          proParsed = Map<String, dynamic>.from(rawParsed.first);
        }
      } catch (jsonErr) {
        print("⚠️ [MasterCascade Pro]: jsonDecode error ($jsonErr). Running fallback parser...");
        final String generalFallback = _extractKeyFallback(rawPro, "pro_general_shelf");
        final String trainingFallback = _extractKeyFallback(rawPro, "pro_training_shelf");

        proParsed = {
          'pro_general_shelf': generalFallback.isNotEmpty ? generalFallback : rawPro,
          'pro_training_shelf': trainingFallback.isNotEmpty ? trainingFallback : 'Raw logical response recovered from text.',
        };
      }

      // 1. Extract and save shelves
      vault.updateProShelves(
        general: _ensureString(proParsed['pro_general_shelf'], fallback: 'Pro summary unavailable'),
        training: _ensureString(proParsed['pro_training_shelf'], fallback: 'Pro logs unavailable'),
      );

      // 2. Extract and save vitals_thresholds directly into Vault
      if (proParsed.containsKey('vitals_thresholds') && proParsed['vitals_thresholds'] is Map) {
        final Map<String, dynamic> vitalsMap = Map<String, dynamic>.from(proParsed['vitals_thresholds']);
        vault.saveVitalsThresholds(vitalsMap);
      } else {
        print("⚠️ [MasterCascade] vitals_thresholds are missing in the Pro response");
      }

    } catch (e, stackTrace) {
      print("❌ Error during Gemini Pro stage: $e");
      print(stackTrace);
      vault.updateProShelves(
        general: 'Pro verdict execution error',
        training: 'Pro model failure: $e',
      );
    }

    onProgressUpdate(1.0);
  }
}