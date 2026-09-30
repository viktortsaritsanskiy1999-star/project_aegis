import 'dart:convert';
import 'dart:io';
import 'package:flutter/foundation.dart';
import 'package:flutter/material.dart';
import 'package:google_generative_ai/google_generative_ai.dart';
import 'master_cascade_service.dart';
import 'system_models_prompts.dart';
import 'vault_service.dart';

class BrainService extends ChangeNotifier {
  double _analysisProgress = 0.0;
  double get analysisProgress => _analysisProgress;
  static const String _apiKey = "YOUR_API_KEY_HERE";

  // ==========================================
  // GEMINI API CONNECTION SETTINGS
  // ==========================================
  // M1 (Anamnesis) and Vision ECG -> 3.6-flash
  static const String _modelName = "gemini-3.6-flash";

  // 8 system agents -> 3.1-flash-lite
  static const String _cascadeModelName = "gemini-3.1-flash-lite";

  late final GenerativeModel _visionModel;
  final MasterCascadeService _masterCascadeService = MasterCascadeService();

  BrainService() {
    _visionModel = GenerativeModel(
      model: 'gemini-3.6-flash',
      apiKey: _apiKey,
    );
  }

  // ==========================================
  // 📸 VISION ECG ANALYSIS
  // ==========================================
  Future<String> analyzeEcgImage(File imageFile) async {
    try {
      _analysisProgress = 0.2;
      notifyListeners();

      final imageBytes = await imageFile.readAsBytes();

      _analysisProgress = 0.5;
      notifyListeners();

      const String ecgPrompt = '''
Decrypt and digitize data from the ECG image. 
Highlight key metrics: rhythm, HR (bpm), electrical axis position, interval durations (P, PQ, QRS, QT/QTc), ST segment shift, and T wave morphology. 
Describe detected deviations or confirm their absence.
Provide an accurate, clean result without lyrical digressions.
''';

      final content = [
        Content.multi([
          TextPart(ecgPrompt),
          DataPart('image/jpeg', imageBytes),
        ])
      ];

      final response = await _visionModel.generateContent(content);

      _analysisProgress = 1.0;
      notifyListeners();

      if (response.text != null && response.text!.isNotEmpty) {
        return response.text!;
      } else {
        throw Exception("The neural network returned an empty response.");
      }
    } catch (e) {
      _analysisProgress = 0.0;
      notifyListeners();
      throw Exception("Vision ECG processing error: $e");
    }
  }

  // ==========================================
  // 🚀 UNIFIED END-TO-END METHOD FOR UI (CONDUCTOR)
  // ==========================================
  Future<void> runFullPipeline(VaultService vault) async {
    try {
      print("🚀 [AEGIS] Analysis start: 8 systems -> Flash -> Pro");

      // 1. Polling 8 system agents (0.0 -> 0.70)
      await runSystemCascade(vault);

      // 2. Master models relay: Flash 3.6 -> Pro 3.1 (0.70 -> 1.0)
      await _masterCascadeService.executeCascade(
        vault: vault,
        callGemini: (prompt, model) => _internalCallGemini(prompt, model),
        parseJson: (raw) => _parseJson(raw),
        onProgressUpdate: (progress) {
          _analysisProgress = progress;
          notifyListeners();
        },
      );

      print("🎉 ENTIRE AEGIS CASCADE SUCCESSFULLY COMPLETED!");
    } catch (e) {
      _analysisProgress = 0.0;
      notifyListeners();
      print("Critical cascade failure: $e");
    }
  }

  /// Collects the entire anamnesis and launches M1
  Future<void> processFullAnamnesis(VaultService vault) async {
    _analysisProgress = 0.05;
    notifyListeners();

    // Check Vault through the unified aggregator: if the Vault is completely empty, there is no point in running the neural network
    final String rawVaultContext = vault.getAggregatedContextForMaster();
    if (rawVaultContext.trim().isEmpty) {
      _analysisProgress = 0.0;
      notifyListeners();
      return;
    }

    final Map<String, dynamic> fullContext = {
      'system_folders': {
        'cvs': vault.getSystemContext('cvs'),
        'gi_tract': vault.getSystemContext('gi_tract'),
        'kidneys': vault.getSystemContext('kidneys'),
        'endocrine': vault.getSystemContext('endocrine'),
        'immune': vault.getSystemContext('immune'),
        'nervous': vault.getSystemContext('nervous'),
        'respiratory': vault.getSystemContext('respiratory'),
        'musculoskeletal': vault.getSystemContext('musculoskeletal'),
      },
      'core_data_log': vault.getSystemContext('global'),
    };

    if (fullContext['core_data_log'].isEmpty) {
      _analysisProgress = 0.0;
      notifyListeners();
      return;
    }

    _analysisProgress = 0.10;
    notifyListeners();

    const staticPrompt = """
You are the autonomous analytical module "Anamnesis" of the Aegis expert medical system. 
Your goal is to conduct primary pathophysiological data preprocessing, form sterile vulnerability markers, and package the context for adjacent AI models (nurses).

MANDATORY JSON RESPONSE FORMAT (STRICTLY OBSERVE ALL KEYS):
{
  "user_view": {
    "summary": "Brief summary for the user across all systems"
  },
  "system_logic": {
    "cvs": { "data": "CVS facts (up to 15 words)", "internal_comment": "M1 comment" },
    "gi_tract": { "data": "GI tract facts (up to 15 words)", "internal_comment": "M1 comment" },
    "kidneys": { "data": "Kidney facts (up to 15 words)", "internal_comment": "M1 comment" },
    "endocrine": { "data": "Endocrine facts (up to 15 words)", "internal_comment": "M1 comment" },
    "immune": { "data": "Immune facts (up to 15 words)", "internal_comment": "M1 comment" },
    "nervous": { "data": "Nervous system facts (up to 15 words)", "internal_comment": "M1 comment" },
    "respiratory": { "data": "Respiratory facts (up to 15 words)", "internal_comment": "M1 comment" },
    "musculoskeletal": { "data": "Musculoskeletal facts (up to 15 words)", "internal_comment": "M1 comment" }
  },
  "training_sandbox": {
    "raw_diagnostic_hypothesis": "Primary integral pathophysiological hypothesis at the intersection of all systems",
    "data_gap_analysis": "Critical data deficit and missing laboratory/instrumental markers",
    "error_probability": "Assessment of anamnesis error/noise probability (e.g., Low (15%), Medium (40%), High)"
  }
}
""";

    final finalPrompt = "$staticPrompt\n\nINPUT DATA PACKAGE FOR ANALYSIS:\n${json.encode(fullContext)}";

    try {
      _analysisProgress = 0.15;
      notifyListeners();

      final String rawResponse = await _internalCallGemini(finalPrompt, _modelName);

      print("=================== [DEBUG M1: RAW RESPONSE] ===================");
      print(rawResponse);
      print("================================================================");

      Map<String, dynamic> response = _parseJson(rawResponse);
      if (response.isEmpty) throw Exception("The model returned an invalid or empty JSON structure.");

      if (response.containsKey('user_view')) {
        final userViewMap = _ensureMap(response['user_view']);
        vault.updateSystemFolder('global', userViewMap);
        vault.saveAnamnesis(
          'global',
          _ensureString(userViewMap['summary'], fallback: 'Anamnesis processed'),
        );
      }
      if (response.containsKey('system_logic')) {
        final systemLogic = _ensureMap(response['system_logic']);
        systemLogic.forEach((systemKey, data) {
          vault.updateSystemFolder(systemKey, _ensureMap(data));
        });
      }
      if (response.containsKey('training_sandbox')) {
        vault.updateSystemFolder('global', {'training_sandbox': _ensureMap(response['training_sandbox'])});
      }

      _analysisProgress = 0.20;
      notifyListeners();
      print("M2M preprocessing successfully completed via Gemini API.");
    } catch (e) {
      _analysisProgress = 0.0;
      notifyListeners();
      print("Critical anamnesis processing failure: $e");
    }
  }

  /// Polls 8 system models sequentially (0.20 -> 0.70)
  Future<void> runSystemCascade(VaultService vault) async {
    _analysisProgress = 0.20;
    notifyListeners();

    try {
      final Map<String, dynamic> sharedLabsTable = vault.getSharedLabs();
      double progressStep = 0.50 / SystemModelsPrompts.systemKeys.length;

      for (String systemKey in SystemModelsPrompts.systemKeys) {
        final oldFolder = vault.getSystemContext(systemKey);

        final String extractedFacts = _ensureString(
          oldFolder['data'] ?? oldFolder['system_facts'] ?? oldFolder['facts'],
          fallback: 'Data is missing',
        );

        final String extractedComment = _ensureString(
          oldFolder['internal_comment'] ?? oldFolder['comment'],
          fallback: 'Conclusion is missing',
        );

        final Map<String, dynamic> inputPackage = {
          'system_key': systemKey,
          'facts': extractedFacts,
          'first_model_short_summary': extractedComment,
          'shared_labs_table': sharedLabsTable,
          'vitals_stream': {
            'blood_pressure': '${vault.systolic ?? '--'}/${vault.diastolic ?? '--'} mmHg',
            'heart_rate': '${vault.pulse ?? '--'} bpm',
            'spo2': '${vault.spo2 ?? '--'}%',
            'ecg_summary': vault.ecgSummary ?? 'Not conducted',
          },
        };

        final String systemPrompt = SystemModelsPrompts.getPromptForSystem(systemKey, inputPackage);
        final String rawResponse = await _internalCallGemini(systemPrompt, _cascadeModelName);
        final Map<String, dynamic> parsedResponse = _parseJson(rawResponse);

        if (parsedResponse.isNotEmpty) {
          final systemView = _ensureMap(parsedResponse['system_view']);
          final systemLogic = _ensureMap(parsedResponse['system_logic']);
          final trainingSandbox = _ensureMap(parsedResponse['training_sandbox']);

          vault.updateSystemCascadeShelves(
            systemKey: systemKey,
            selectedLabs: _ensureMap(systemView['isolated_labs']),
            generalOutput: _ensureString(systemLogic['system_general_shelf'], fallback: 'Analysis is empty'),
            trainingOutput: _ensureString(trainingSandbox['system_training_shelf'], fallback: 'Logs are empty'),
          );
        }

        _analysisProgress += progressStep;
        notifyListeners();
        await Future.delayed(const Duration(milliseconds: 500));
      }

      _analysisProgress = 0.70;
      notifyListeners();
      print("First stage of the medical cascade (8 systems) successfully completed.");

    } catch (e) {
      _analysisProgress = 0.0;
      notifyListeners();
      print("Critical failure in system cascade analysis: $e");
      rethrow;
    }
  }

  /// Gemini gateway with automatic retry on 503 errors and overload
  Future<String> _internalCallGemini(String prompt, [String targetModel = _modelName]) async {
    int maxRetries = 3;
    int delaySeconds = 2;

    for (int attempt = 1; attempt <= maxRetries; attempt++) {
      try {
        final model = GenerativeModel(
          model: targetModel,
          apiKey: _apiKey,
          generationConfig: GenerationConfig(
            responseMimeType: 'application/json',
            maxOutputTokens: 8192,
          ),
        );

        final content = [Content.text(prompt)];
        final response = await model.generateContent(content);

        if (response.text == null) {
          throw Exception("Remote Gemini server returned an empty text response.");
        }

        return response.text!;
      } catch (e) {
        bool isUnavailable = e.toString().contains('503') ||
            e.toString().contains('429') ||
            e.toString().contains('UNAVAILABLE') ||
            e.toString().contains('RESOURCE_EXHAUSTED') ||
            e.toString().contains('quota');

        if (isUnavailable && attempt < maxRetries) {
          print("⚠️ [Gemini 503] Server overloaded. Attempt $attempt/$maxRetries. Waiting $delaySeconds sec...");
          await Future.delayed(Duration(seconds: delaySeconds));
          delaySeconds *= 2;
        } else {
          rethrow;
        }
      }
    }
    throw Exception("Failed to get response from Gemini after $maxRetries attempts.");
  }

  Future<List<String>> identifySystems(String text) async {
    _analysisProgress = 0.3;
    notifyListeners();
    try {
      await Future.delayed(const Duration(seconds: 1));
      List<String> detected = [];
      String lowText = text.toLowerCase();
      if (lowText.contains('stomach') || lowText.contains('diarrhea')) detected.add('gi_tract');
      if (lowText.contains('heart') || lowText.contains('pulse')) detected.add('cvs');
      if (lowText.contains('cough') || lowText.contains('breathe')) detected.add('respiratory');
      if (detected.isEmpty) detected.add('global');
      return detected;
    } catch (e) {
      return ['global'];
    }
  }

  void resetProgress() {
    _analysisProgress = 0.0;
    notifyListeners();
  }

  /// Secure JSON parser cleaned of Markdown tags
  Map<String, dynamic> _parseJson(String raw) {
    try {
      String cleaned = raw
          .replaceAll(RegExp(r'```json\s*'), '')
          .replaceAll(RegExp(r'```\s*'), '')
          .trim();

      final decoded = json.decode(cleaned);
      if (decoded is Map<String, dynamic>) {
        return decoded;
      }
    } catch (e) {
      print("JSON parsing error in BrainService: $e");
    }
    return {};
  }

  /// Guaranteed conversion of any data to String
  String _ensureString(dynamic input, {String fallback = ''}) {
    if (input == null) return fallback;
    if (input is String) return input;
    if (input is List) return input.map((e) => e.toString()).join('\n');
    if (input is Map) return input.entries.map((e) => '${e.key}: ${e.value}').join('\n');
    return input.toString();
  }

  /// Guaranteed conversion of data to Map<String, dynamic>
  Map<String, dynamic> _ensureMap(dynamic input) {
    if (input is Map<String, dynamic>) return input;
    if (input is Map) {
      return input.map((k, v) => MapEntry(k.toString(), v));
    }
    return {};
  }
  // ==========================================
  // PUBLIC GATEWAYS FOR CASCADES
  // ==========================================

  /// Public Gemini API call for isolated services (including the weekly cascade)
  Future<String> callGeminiRaw(String prompt, String targetModel) async {
    return await _internalCallGemini(prompt, targetModel);
  }

  /// Public safe JSON parsing helper method
  Map<String, dynamic> parseJsonHelper(String raw) {
    return _parseJson(raw);
  }
}