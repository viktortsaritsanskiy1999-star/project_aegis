/// Centralized Google Gemini model configuration for Project Aegis.
class AegisModelConfig {
  /// Low-latency model for 8 organ specialists and subagent tasks.
  static const String flashLiteModel = "gemini-3.5-flash-lite";

  /// Workhorse model for Anamnesis, Deputy Chief, Athena, and Wellness.
  static const String flashModel = "gemini-3.8-flash";

  /// Flagship reasoning model for Chief Physician, Quality Gate, and medical strategy.
  static const String proModel = "gemini-4-argon";

  /// Multimodal vision model for ECG and lab form OCR.
  static const String visionModel = "gemini-3.8-flash";
}
