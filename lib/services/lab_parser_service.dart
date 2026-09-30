import 'dart:convert';
import 'package:google_generative_ai/google_generative_ai.dart';
import 'package:image_picker/image_picker.dart';
import 'vault_service.dart';

class LabParserService {
  final GenerativeModel _visionModel = GenerativeModel(
    model: 'gemini-3.6-flash',
    apiKey: 'YOUR_API_KEY_HERE',
  );

  /// Multimodal parsing with complete extraction of indicators and categorization
  Future<bool> parseImages(List<XFile> imageFiles, VaultService vault) async {
    if (imageFiles.isEmpty) return false;

    try {
      final List<Part> parts = [];

      for (final file in imageFiles) {
        final bytes = await file.readAsBytes();
        final mimeType = _getMimeType(file.path);
        parts.add(DataPart(mimeType, bytes));
      }

      const prompt = """
You are a medical analyst-normalizer for the Aegis expert system.
One or more images of laboratory test report forms are provided as input.

YOUR MAIN TASK:
1. Recognize and extract absolutely ALL laboratory indicators present in the images (do not limit yourself only to well-known or frequent ones).
2. Categorize the found indicators by biological fluids / analysis types.
3. Strictly IGNORE non-medical clutter: full names, dates, addresses, hospital names, doctor names, phone numbers, stamps, norms/references, and units of measurement.

CATEGORY STRUCTURE (use ONLY these top-level keys):
- "blood_count" : Complete Blood Count (CBC). Examples: wbc, rbc, hgb, plt, esr, hct, mcv, mchc, lymphocytes, monocytes, etc.
- "biochemistry" : Blood biochemistry (BMP/CMP). Examples: alt, ast, glucose, bilirubin_total, urea, creatinine, crp, ferritin, ldl, hdl, total_protein, electrolytes, etc.
- "urine" : Urinalysis (UA). Examples: rbc (urine red blood cells), wbc (urine white blood cells), protein, glucose, density, ph, epithelial_cells, bacteria, ketones, bilirubin, etc.
- "coagulation" : Coagulation panel / clotting. Examples: inr, pt, aptt, fibrinogen, d_dimer, etc.
- "other" : Other markers (hormones TSH, T3, T4, vitamins, tumor markers, infections).

EXTRACTION AND NORMALIZATION RULES:
- If an indicator is not in the examples, name it using standard Latin snake_case (e.g., 'cholesterol_total', 'esr_pantschenkow', 'ketones_urine').
- Convert values strictly to a NUMBER (double or int).
- Under no circumstances confuse BLOOD and URINE indicators!

OUTPUT FORMAT (STRICTLY JSON, without Markdown json, without explanations):
{
  "blood_count": {
    "wbc": 6.5,
    "rbc": 4.8,
    "hgb": 142.0
  },
  "biochemistry": {
    "alt": 25.0,
    "ast": 22.0,
    "glucose": 5.1
  },
  "urine": {
    "rbc": 0.0,
    "wbc": 2.0,
    "protein": 0.0
  }
}
If no indicators are found in the images, return an empty object {}.
""";

      parts.add(TextPart(prompt));

      final response = await _visionModel.generateContent([Content.multi(parts)]);

      final String rawText = response.text ?? '';
      String cleanJson = rawText
          .replaceAll('```json', '')
          .replaceAll('```', '')
          .trim();

      final Map<String, dynamic> parsedJson = jsonDecode(cleanJson);

      // Put the entire collected array with categories into the Vault
      vault.injectParsedLaboratories(parsedJson);
      return true;
    } catch (e) {
      print("❌ Error during multi-form Vision parsing: $e");
      return false;
    }
  }

  String _getMimeType(String filePath) {
    final path = filePath.toLowerCase();
    if (path.endsWith('.png')) return 'image/png';
    if (path.endsWith('.webp')) return 'image/webp';
    if (path.endsWith('.heic')) return 'image/heic';
    return 'image/jpeg';
  }
}