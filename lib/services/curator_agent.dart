import 'dart:convert';
import 'package:flutter/foundation.dart';
import 'package:google_generative_ai/google_generative_ai.dart';
import 'vault_service.dart';
import 'vitals_alert_context.dart';

/// Curator conversational agent management service (Athena)
class CuratorAgent {
  static const String _defaultApiKey = "YOUR_API_KEY_HERE";
  static const String _defaultModelName = "gemini-3.6-flash";

  final String _apiKey;
  final String _modelName;
  GenerativeModel? _model;
  ChatSession? _chatSession;

  CuratorAgent({
    String apiKey = _defaultApiKey,
    String modelName = _defaultModelName,
  })  : _apiKey = apiKey,
        _modelName = modelName;

  /// Initialize the Curator's chat session via VaultService
  void initializeSession({
    required VaultService vault,
    List<Content>? history,
    bool forceReinit = false,
  }) {
    if (_chatSession != null && !forceReinit) {
      return;
    }

    final String fullProResponse = vault.getCuratorContext();
    final rawChatHistory = vault.getCuratorChatHistory();
    List<Content> restoredHistory = [];

    if (rawChatHistory.isNotEmpty) {
      String? lastRole;
      for (var msg in rawChatHistory) {
        final role = (msg['sender'] == 'user') ? 'user' : 'model';
        final text = (msg['text'] ?? '').toString();

        if (text.trim().isEmpty) continue;

        if (role != lastRole) {
          restoredHistory.add(Content(role, [TextPart(text)]));
          lastRole = role;
        }
      }
    }

    if (restoredHistory.isNotEmpty && restoredHistory.first.role == 'model') {
      restoredHistory.insert(
        0,
        Content('user', [TextPart('Athena chat initialization.')]),
      );
    }

    final String systemPrompt = '''
You are Athena, a personal curator for Wellness and healthy lifestyle in the Aegis Health ecosystem.
The name "Athena" remains the same in any language.

LEGAL DISCLAIMER AND STRICT RESTRICTIONS:
1. IT IS FORBIDDEN to call yourself or system colleagues doctors, physicians, medical specialists, or medics.
2. IT IS FORBIDDEN to mention "diagnoses", "treatment", "medical appointments", or "medications".
3. You DO NOT diagnose and DO NOT prescribe treatment. Your role is a personal mentor for tracking habits, fitness, and general well-being.

AEGIS HEALTH SYSTEM ARCHITECTURE:
Behind your back, an analytical board works (Pro model, Organ agents). 
Your task is to be a caring, empathetic wellness curator for the user, translating technical conclusions into simple, warm language.

INSTRUCTIONS AND ROUTE FROM THE CHIEF METHODOLOGIST (PRO):
$fullProResponse

==================================================
HIDDEN CLINICAL REPORTING RULE (MANDATORY):
==================================================
In EVERY response of yours (both in routine and emergency), you MUST generate a hidden system block in JSON format at the very end of the text on a new line, enclosed in the [CLINICAL_REPORT] tag. The user does not evaluate it, but the system will log it into the medical record for the board.

Block format:
[CLINICAL_REPORT]
{
  "status": "NORMAL" | "ALERT" | "CRITICAL",
  "symptoms_mentioned": ["symptom1", "symptom2"],
  "subjective_wellbeing_score": 1-10 (or null if unclear),
  "adherence_to_plan": "HIGH" | "MEDIUM" | "LOW" | "UNKNOWN",
  "red_flags": true | false,
  "summary_for_master": "Short (10-15 words) clinical summary of the user's response for the Chief Physician"
}
[/CLINICAL_REPORT]

TWO MODES OF OPERATION:

MODE 1: ROUTINE AND 7-DAY SPRINT (DEFAULT)
- Your task is to conduct daily tracking along the Pro route.
- Ask ONLY 1 targeted question per day during the daily pulse.
- When the user answers — record the response, provide empathetic support, and BE SURE TO SAY GOODBYE UNTIL TOMORROW.

MODE 2: FORCE MAJEURE / POOR WELL-BEING / HEART RATE SPIKE
- When complaining of pain, weakness, nausea, or during vital spikes:
  1. THE ROUTINE MODE AND THE "GOODBYE UNTIL TOMORROW" RULE ARE CANCELED!
  2. Switch to active support and live communication mode.
  3. Do not say goodbye. Clarify details, ground the user, help them recover.
  4. If the situation is critical, suggest calling an ambulance and strictly add on a new line at the very end (AFTER [CLINICAL_REPORT]): [EMERGENCY_TRIGGER].
''';

    _model = GenerativeModel(
      model: _modelName,
      apiKey: _apiKey,
      systemInstruction: Content.system(systemPrompt),
    );

    _chatSession = _model!.startChat(
      history: history ?? restoredHistory,
    );
  }

  /// 1. First introduction
  Future<String?> generateInitialGreeting({VaultService? vault}) async {
    _ensureInitialized();
    const prompt = '''
Service pulse: FIRST INTRODUCTION.
Briefly and sincerely introduce yourself (Athena), say that you will help follow the route.
Formulate the first targeted guiding question on the first step of the Pro route with 2-3 everyday response options.
Don't forget to generate the initial [CLINICAL_REPORT].
''';

    final rawResponse = await _executePrompt(prompt);
    return _processResponse(rawResponse, vault);
  }

  /// 2. User response (with 7th day logic support)
  Future<String?> handleUserReply(
      String userReply, {
        required int sprintDay,
        VaultService? vault,
      }) async {
    _ensureInitialized();

    final bool isSprintEnd = (sprintDay == 7);

    final prompt = '''
User replied: "$userReply"

YOUR INSTRUCTION:
1. If the response is ordinary and routine:
   - Briefly comment on the response with empathy.
   - Confirm data recording.
   - Say goodbye until tomorrow.
   ${isSprintEnd ? '- SINCE TODAY IS DAY 7 OF THE SPRINT: At the very end of the response on a new line, add a natural teaser phrase: "By the way, wait a minute, let me show you how you spent this week!"' : ''}
   - Fill in [CLINICAL_REPORT] with "NORMAL" or "ALERT" status (depending on plan adherence).

2. If the user reported POOR WELL-BEING (pain, weakness, nausea, anxiety):
   - DO NOT SAY GOODBYE! Activate Mode 2 (Force Majeure).
   - Ask about symptoms, reassure, help ground.
   - Fill in [CLINICAL_REPORT] with "ALERT" or "CRITICAL" status and detail "symptoms_mentioned" and "red_flags".
   - In case of a real life-threatening situation, add strictly at the very end on a new line: [EMERGENCY_TRIGGER].
''';

    final rawResponse = await _executePrompt(prompt);
    return _processResponse(rawResponse, vault);
  }

  /// 3. Daily pulse (+24h) taking into account the sprint day
  Future<String?> handleDailyPulse({
    required int sprintDay,
    VaultService? vault,
  }) async {
    _ensureInitialized();

    final prompt = '''
Service pulse: A NEW DAY HAS COME (ROUTINE MODE). Current sprint day: $sprintDay of 7.
Study the history of past answers and the current step of the Pro route.
1. Gently greet the user.
2. Formulate 1 NEXT question along the route with 2-3 response options.
3. Generate the current [CLINICAL_REPORT].
''';

    final rawResponse = await _executePrompt(prompt);
    return _processResponse(rawResponse, vault);
  }

  /// 4. Heart rate spike handling
  Future<String?> handleVitalsSpikeAlert({
    required VitalsAlertContext alertContext,
    VaultService? vault,
  }) async {
    _ensureInitialized();

    final prompt = '''
Service pulse: Heart rate spike detected (peak up to ${alertContext.currentPulse} bpm with a baseline of${alertContext.baselinePulse} bpm at rest).

YOUR INSTRUCTION (MODE 2: ATTENTION AND SUPPORT):
1. THE "GOODBYE UNTIL TOMORROW" RULE IS STRICTLY CANCELED!
2. Speak warmly and calmly. Ask what happened, ask them to sit down, unbutton their collar, and breathe.
3. Filter out "red flags" (chest pain, shortness of breath, lack of air, darkening in the eyes).
4. Be sure to formulate [CLINICAL_REPORT] with "ALERT" (or "CRITICAL") status, specifying "red_flags" and a summary for the Chief Physician.
5. If the condition is critical, add to the very end of the response (after [CLINICAL_REPORT]): [EMERGENCY_TRIGGER].
''';

    final rawResponse = await _executePrompt(prompt);
    return _processResponse(rawResponse, vault);
  }

  /// 5. Generation of the weekly report by Athena based on FRESH Pro data from Day 7
  Future<String?> generateWeeklyReport({
    required VaultService vault,
  }) async {
    _ensureInitialized();

    // Get the latest context from the vault, which is already updated by the cascade
    final String latestProStrategy = vault.getCuratorContext();

    final prompt = '''
Service pulse: GENERATION OF DAY 7 RESULTS (SPRINT END).

The Board (Pro) has just finished reviewing the past 7 days and has passed you the FRESH strategy and conclusions:
"""
$latestProStrategy
"""

YOUR TASK:
1. You have seen the entire dialogue with the user over these 7 days. Based on your communication experience with them and the FRESH conclusions of the Board, write a warm, encouraging, and human weekly summary to the user.
2. Highlight their successes for the week, praise them for discipline, and gently state the vector for the next 7 days.
3. Speak on your own behalf (as Athena) — without dry medical bureaucratic jargon.
4. Be sure to generate the final [CLINICAL_REPORT] for the past week.
''';

    final rawResponse = await _executePrompt(prompt);
    return _processResponse(rawResponse, vault);
  }

  /// Single processing point: extracts JSON, saves to Vault and returns clean text to the UI
  String? _processResponse(String? rawResponse, VaultService? vault) {
    if (rawResponse == null) return null;

    if (vault != null) {
      _parseAndSaveClinicalReport(rawResponse, vault);
    }

    // Extract the [CLINICAL_REPORT]...[/CLINICAL_REPORT] block from the UI text
    final regExp = RegExp(r'\[CLINICAL_REPORT\]([\s\S]*?)\[/CLINICAL_REPORT\]');
    final cleanText = rawResponse.replaceAll(regExp, '').trim();

    return cleanText;
  }

  /// Safe parsing with markdown tag cleaning
  void _parseAndSaveClinicalReport(String rawResponse, VaultService vault) {
    try {
      final regExp = RegExp(r'\[CLINICAL_REPORT\]([\s\S]*?)\[/CLINICAL_REPORT\]');
      final match = regExp.firstMatch(rawResponse);

      if (match != null && match.group(1) != null) {
        String jsonString = match.group(1)!.trim();

        // Clean from potential ```json ... ```
        jsonString = jsonString.replaceAll(RegExp(r'^```json\s*'), '');
        jsonString = jsonString.replaceAll(RegExp(r'^```\s*'), '');
        jsonString = jsonString.replaceAll(RegExp(r'\s*```$'), '');

        final Map<String, dynamic> parsedJson = jsonDecode(jsonString.trim()) as Map<String, dynamic>;

        vault.saveAthenaClinicalReport(parsedJson);
      }
    } catch (e) {
      debugPrint('[CuratorAgent Log Error]: Hidden report parsing error: $e');
    }
  }

  void _ensureInitialized() {
    if (_chatSession == null) {
      throw StateError(
        'Curator session is not initialized. Call initializeSession() before sending messages.',
      );
    }
  }

  Future<String?> _executePrompt(String promptText) async {
    try {
      final response = await _chatSession!.sendMessage(
        Content.text(promptText),
      );
      return response.text;
    } catch (e) {
      debugPrint('[CuratorAgent Error]: $e');
      rethrow;
    }
  }
}