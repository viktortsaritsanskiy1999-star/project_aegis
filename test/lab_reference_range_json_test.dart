// ARGON AUDIT FIX 08: BUG-10 LAB REFERENCE RANGE JSON COMPATIBILITY TEST
import 'package:flutter_test/flutter_test.dart';

import 'package:aegis_health/services/api/medical_ai_api_models.dart';

void main() {
  group('ARGON BUG-10: LabReferenceRange JSON Compatibility', () {
    test('1. Deserializes backend JSON using authoritative "text" field', () {
      final jsonMap = {
        'text': '< 5.0',
      };
      final range = LabReferenceRange.fromJson(jsonMap);

      expect(range.textRange, equals('< 5.0'));
      expect(range.low, isNull);
      expect(range.high, isNull);

      final cyrillicJson = {
        'text': 'не обнаружено',
      };
      final cyrillicRange = LabReferenceRange.fromJson(cyrillicJson);
      expect(cyrillicRange.textRange, equals('не обнаружено'));
      expect(cyrillicRange.low, isNull);
      expect(cyrillicRange.high, isNull);
    });

    test('2. Deserializes legacy JSON using "text_range" field (backward compatibility)', () {
      final legacyJson = {
        'text_range': '3.9 - 6.1',
      };
      final range = LabReferenceRange.fromJson(legacyJson);

      expect(range.textRange, equals('3.9 - 6.1'));
      expect(range.low, isNull);
      expect(range.high, isNull);
    });

    test('3. Deserializes numeric min/max ranges (low and high)', () {
      final numericJson = {
        'low': 120.0,
        'high': 160.0,
      };
      final range = LabReferenceRange.fromJson(numericJson);

      expect(range.low, equals(120.0));
      expect(range.high, equals(160.0));
      expect(range.textRange, isNull);

      // Integer input coerced to double
      final intJson = {
        'low': 70,
        'high': 99,
      };
      final intRange = LabReferenceRange.fromJson(intJson);
      expect(intRange.low, equals(70.0));
      expect(intRange.high, equals(99.0));

      // Partial bounds
      final lowOnly = LabReferenceRange.fromJson({'low': 3.5});
      expect(lowOnly.low, equals(3.5));
      expect(lowOnly.high, isNull);

      final highOnly = LabReferenceRange.fromJson({'high': 200.0});
      expect(highOnly.low, isNull);
      expect(highOnly.high, equals(200.0));
    });

    test('4. Handles missing and null fields gracefully', () {
      final emptyRange = LabReferenceRange.fromJson({});
      expect(emptyRange.low, isNull);
      expect(emptyRange.high, isNull);
      expect(emptyRange.textRange, isNull);

      final explicitNulls = LabReferenceRange.fromJson({
        'low': null,
        'high': null,
        'text': null,
        'text_range': null,
      });
      expect(explicitNulls.low, isNull);
      expect(explicitNulls.high, isNull);
      expect(explicitNulls.textRange, isNull);
    });

    test('5. Supports text and numeric values coexisting (backend and legacy)', () {
      // Backend format: low, high, text
      final backendCombined = {
        'low': 3.5,
        'high': 5.0,
        'text': '3.5 - 5.0',
      };
      final rangeBackend = LabReferenceRange.fromJson(backendCombined);
      expect(rangeBackend.low, equals(3.5));
      expect(rangeBackend.high, equals(5.0));
      expect(rangeBackend.textRange, equals('3.5 - 5.0'));

      // Legacy format: low, high, text_range
      final legacyCombined = {
        'low': 3.5,
        'high': 5.0,
        'text_range': '3.5 - 5.0',
      };
      final rangeLegacy = LabReferenceRange.fromJson(legacyCombined);
      expect(rangeLegacy.low, equals(3.5));
      expect(rangeLegacy.high, equals(5.0));
      expect(rangeLegacy.textRange, equals('3.5 - 5.0'));

      // Both present: authoritative "text" takes precedence over "text_range"
      final bothPresent = {
        'low': 3.5,
        'high': 5.0,
        'text': 'authoritative_backend',
        'text_range': 'legacy_fallback',
      };
      final rangePrecedence = LabReferenceRange.fromJson(bothPresent);
      expect(rangePrecedence.low, equals(3.5));
      expect(rangePrecedence.high, equals(5.0));
      expect(rangePrecedence.textRange, equals('authoritative_backend'));

      // "text" is explicitly null, falls back to "text_range"
      final nullTextFallback = {
        'text': null,
        'text_range': 'valid_fallback',
      };
      final rangeFallback = LabReferenceRange.fromJson(nullTextFallback);
      expect(rangeFallback.textRange, equals('valid_fallback'));
    });

    test('6. Serializes to JSON with both "text" and "text_range" for bidirectional compatibility', () {
      const range = LabReferenceRange(
        low: 3.9,
        high: 6.1,
        textRange: '3.9 - 6.1',
      );
      final json = range.toJson();

      expect(json['low'], equals(3.9));
      expect(json['high'], equals(6.1));
      expect(json['text'], equals('3.9 - 6.1'));
      expect(json['text_range'], equals('3.9 - 6.1'));

      // Roundtrip verification
      final restored = LabReferenceRange.fromJson(json);
      expect(restored.low, equals(3.9));
      expect(restored.high, equals(6.1));
      expect(restored.textRange, equals('3.9 - 6.1'));

      // Omission of null fields
      const emptyRange = LabReferenceRange();
      expect(emptyRange.toJson(), isEmpty);
    });

    test('7. Deserializes in LabObservationEntry from backend JSON with "text"', () {
      final obsJson = {
        'observation_id': 'obs_01',
        'marker_key': 'crp',
        'display_name': 'C-Reactive Protein',
        'value': '< 1.0',
        'unit': 'mg/L',
        'reference_range': {
          'text': '< 5.0',
        },
        'recorded_at': '2026-11-02T12:00:00Z',
      };
      final obs = LabObservationEntry.fromJson(obsJson);

      expect(obs.referenceRange, isNotNull);
      expect(obs.referenceRange!.textRange, equals('< 5.0'));
      expect(obs.referenceRange!.low, isNull);
      expect(obs.referenceRange!.high, isNull);

      final reserialized = obs.toJson();
      expect((reserialized['reference_range'] as Map)['text'], equals('< 5.0'));
      expect((reserialized['reference_range'] as Map)['text_range'], equals('< 5.0'));
    });

    test('8. Deserializes in VisionOcrMarkerItem from backend JSON with "text"', () {
      final visionJson = {
        'original_name': 'СОЭ',
        'marker_key': 'esr',
        'raw_value': '8',
        'numeric_value': 8.0,
        'unit': 'mm/h',
        'reference_range': {
          'low': 2.0,
          'high': 15.0,
          'text': '2 - 15',
        },
      };
      final item = VisionOcrMarkerItem.fromJson(visionJson);

      expect(item.referenceRange, isNotNull);
      expect(item.referenceRange!.low, equals(2.0));
      expect(item.referenceRange!.high, equals(15.0));
      expect(item.referenceRange!.textRange, equals('2 - 15'));
    });
  });
}
