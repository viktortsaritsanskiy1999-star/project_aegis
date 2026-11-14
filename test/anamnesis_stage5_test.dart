import 'dart:convert';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:provider/provider.dart';

import 'package:aegis_health/anamnesis_screen.dart';
import 'package:aegis_health/services/api/aegis_api_client.dart';
import 'package:aegis_health/services/api/medical_ai_api_models.dart';
import 'package:aegis_health/services/auth/auth_models.dart';
import 'package:aegis_health/services/auth/auth_session_manager.dart';
import 'package:aegis_health/services/auth/auth_token_storage.dart';
import 'package:aegis_health/services/vault_service.dart';
import 'package:aegis_health/services/vault_storage_service.dart';

Widget _buildTestApp({
  required VaultService vault,
  required AuthSessionManager authManager,
  required AegisApiClient apiClient,
}) {
  return MultiProvider(
    providers: [
      ChangeNotifierProvider<VaultService>.value(value: vault),
      ChangeNotifierProvider<AuthSessionManager>.value(value: authManager),
      Provider<AegisApiClient>.value(value: apiClient),
    ],
    child: MaterialApp(
      home: AnamnesisScreen(),
    ),
  );
}

String _buildTestJwt({required String patientId, required String deviceId}) {
  final header = base64Url.encode(utf8.encode('{"alg":"HS256","typ":"JWT"}')).replaceAll('=', '');
  final exp = DateTime.now().toUtc().add(const Duration(hours: 1)).millisecondsSinceEpoch ~/ 1000;
  final payload = base64Url.encode(utf8.encode(json.encode({
    'sub': deviceId,
    'patient_id': patientId,
    'device_id': deviceId,
    'scope': AegisScopes.standardDeviceScopes.join(' '),
    'exp': exp,
  }))).replaceAll('=', '');
  return '$header.$payload.sig';
}

void _fillCards1To14(VaultService vault) {
  vault.saveAnamnesis('age_confirmation', 'Confirmed');
  vault.saveAnamnesis('gender', 'Female');
  vault.saveAnamnesis('age', '34');
  vault.saveAnamnesis('height', '168');
  vault.saveAnamnesis('weight', '61');
  vault.saveAnamnesis('nicotine_status', 'Non-user');
  vault.saveAnamnesis('alcohol_status', 'Non-drinker');
  vault.saveAnamnesis('spec_factors', 'None');
  vault.saveAnamnesis('sleep_quality', 'Normal');
  vault.saveAnamnesis('diet_types', 'Balanced');
  vault.saveAnamnesis('diet_habits', 'Regular');
  vault.saveAnamnesis('gi_signals', 'None');
  vault.saveAnamnesis('work_type', 'Mostly sitting');
  vault.saveAnamnesis('chronic_diseases', 'No diagnosed chronic conditions');
  vault.saveAnamnesis('recent_illness', 'None of the above');
  vault.saveAnamnesis('critical_infections', 'No diagnosed chronic infections');
}

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  late InMemoryVaultStorage memoryVaultStorage;
  late InMemoryAuthTokenStorage memoryAuthStorage;

  setUp(() {
    VaultService.resetWriteQueueForTesting();
    AuthSessionManager.resetActiveInstancesForTesting();
    memoryVaultStorage = InMemoryVaultStorage();
    memoryAuthStorage = InMemoryAuthTokenStorage();
    VaultStorageService.setStorageForTesting(memoryVaultStorage);
  });

  tearDown(() {
    VaultService.resetWriteQueueForTesting();
    AuthSessionManager.resetActiveInstancesForTesting();
    VaultStorageService.setStorageForTesting(null);
  });

  group('PROJECT AEGIS — ANAMNESIS CARDS V2: STAGE 5 TESTS', () {
    test('1. DTO Roundtrip: AnamnesisQuestionnaireAnswers Stage 5 serialization', () {
      final answers = AnamnesisQuestionnaireAnswers(
        ageConfirmation: 'Confirmed',
        gender: 'Female',
        age: 34,
        height: 168,
        weight: 61,
        nicotineStatus: 'Non-user',
        alcoholStatus: 'Non-drinker',
        specFactors: 'None',
        sleepQuality: 'Normal',
        dietTypes: 'Balanced',
        dietHabits: 'Regular',
        giSignals: 'None',
        workType: 'Mostly sitting',
        chronicDiseases: 'No diagnosed chronic conditions',
        recentIllness: 'None of the above',
        criticalInfections: 'No diagnosed chronic infections',
        heredityRisks: 'Mother: Heart and vascular conditions, Diabetes mellitus; Other blood relatives (Aunt): Stroke',
        medications: 'Prescription medications (Amoxicillin, tablets); Vitamins (Vitamin D, tablets)',
        familyHistoryStatus: 'Yes',
        familyHistoryRelatives: ['Mother', 'Other blood relatives'],
        familyHistoryDetails: {
          'Mother': {
            'conditions': ['Heart and vascular conditions', 'Diabetes mellitus'],
            'details': 'Type 2 diabetes, hypertension',
          },
          'Other blood relatives': {
            'relationship_detail': 'Aunt',
            'conditions': ['Stroke'],
          },
        },
        medicationsStatus: 'Yes',
        medicationCategories: ['Prescription medications', 'Vitamins'],
        medicationDetails: {
          'Prescription medications': {
            'names': 'Amoxicillin, tablets; physician-prescribed injections',
            'course': 'I have been taking a prescribed antibiotic for a week, started November 10',
          },
          'Vitamins': {
            'names': 'Vitamin D, tablets',
          },
        },
      );

      final jsonMap = answers.toJson();
      expect(jsonMap['family_history_status'], 'Yes');
      expect(jsonMap['family_history_relatives'], ['Mother', 'Other blood relatives']);
      expect(jsonMap['family_history_details']['Mother']['conditions'], ['Heart and vascular conditions', 'Diabetes mellitus']);
      expect(jsonMap['family_history_details']['Other blood relatives']['relationship_detail'], 'Aunt');
      expect(jsonMap['medications_status'], 'Yes');
      expect(jsonMap['medication_categories'], ['Prescription medications', 'Vitamins']);
      expect(jsonMap['medication_details']['Vitamins']['course'], isNull);

      final reconstructed = AnamnesisQuestionnaireAnswers.fromMap(jsonMap);
      expect(reconstructed.familyHistoryStatus, 'Yes');
      expect(reconstructed.familyHistoryRelatives, ['Mother', 'Other blood relatives']);
      expect(reconstructed.familyHistoryDetails?['Mother']?['conditions'], ['Heart and vascular conditions', 'Diabetes mellitus']);
      expect(reconstructed.familyHistoryDetails?['Mother']?['details'], 'Type 2 diabetes, hypertension');
      expect(reconstructed.familyHistoryDetails?['Other blood relatives']?['relationship_detail'], 'Aunt');
      expect(reconstructed.medicationsStatus, 'Yes');
      expect(reconstructed.medicationCategories, ['Prescription medications', 'Vitamins']);
      expect(reconstructed.medicationDetails?['Prescription medications']?['names'], 'Amoxicillin, tablets; physician-prescribed injections');
      expect(reconstructed.medicationDetails?['Vitamins']?['course'], isNull);
    });

    testWidgets('2. Card 15: Family History — 4 states, multiple relatives/diseases, no auto-collapse, reopen & edit, Other clarification', (tester) async {
      tester.view.physicalSize = const Size(1080, 2400);
      tester.view.devicePixelRatio = 1.0;
      addTearDown(() => tester.view.resetPhysicalSize());

      final mockClient = MockClient((request) async => http.Response('{}', 200));
      final apiClient = AegisApiClient(client: mockClient);
      final authManager = AuthSessionManager(apiClient: apiClient, storage: memoryAuthStorage);
      final vault = VaultService(storage: memoryVaultStorage, authSessionManager: authManager);
      await vault.initFuture;

      await tester.pumpWidget(_buildTestApp(vault: vault, authManager: authManager, apiClient: apiClient));
      await tester.pumpAndSettle();

      final pageView = tester.widget<PageView>(find.byType(PageView));
      pageView.controller!.jumpToPage(15);
      await tester.pumpAndSettle();

      expect(find.text("Family Archive 🌳"), findsOneWidget);
      expect(find.text("Do any of your blood relatives have medical conditions you know about?"), findsOneWidget);

      // Verify all 4 primary options exist
      expect(find.text("Yes"), findsOneWidget);
      expect(find.text("No, as far as I know"), findsOneWidget);
      expect(find.text("I don't know my family history"), findsOneWidget);
      expect(find.text("Prefer not to answer"), findsOneWidget);

      // Verify negative statuses remain distinct in Vault (test "I don't know my family history")
      await tester.tap(find.text("I don't know my family history"));
      await tester.pumpAndSettle();
      await tester.ensureVisible(find.text("Next ➡️"));
      await tester.tap(find.text("Next ➡️"));
      await tester.pumpAndSettle();
      expect(vault.getSystemContext('global')['family_history_status'], "I don't know my family history");
      expect(vault.getSystemContext('global')['family_history_relatives'], isNull);

      // Go back to Card 15
      await tester.tap(find.byIcon(Icons.arrow_back));
      await tester.pumpAndSettle();

      // Switch to Yes
      await tester.tap(find.text("Yes"));
      await tester.pumpAndSettle();

      // Verify age of onset is NOT asked anywhere
      expect(find.textContaining("age of onset"), findsNothing);

      // 1) Open Mother, select MULTIPLE conditions without auto-collapse
      await tester.ensureVisible(find.text("Mother"));
      await tester.tap(find.text("Mother"));
      await tester.pumpAndSettle();

      expect(find.text("Family history for: Mother"), findsOneWidget);

      await tester.ensureVisible(find.text("Heart and vascular conditions"));
      await tester.tap(find.text("Heart and vascular conditions"));
      await tester.pumpAndSettle();
      // Must NOT auto-collapse after first disease click!
      expect(find.text("Save and collapse"), findsOneWidget);

      await tester.ensureVisible(find.text("Diabetes mellitus"));
      await tester.tap(find.text("Diabetes mellitus"));
      await tester.pumpAndSettle();
      expect(find.text("Save and collapse"), findsOneWidget);

      await tester.ensureVisible(find.text("Stroke"));
      await tester.tap(find.text("Stroke"));
      await tester.pumpAndSettle();

      final motherDetailsField = find.byWidgetPredicate(
        (w) => w is TextField && w.decoration?.hintText == "e.g. Type 2 diabetes, hypertension, colon cancer, other details",
      );
      await tester.ensureVisible(motherDetailsField);
      await tester.enterText(motherDetailsField, "Type 2 diabetes, hypertension");
      await tester.pumpAndSettle();

      await tester.ensureVisible(find.text("Save and collapse"));
      await tester.tap(find.text("Save and collapse"));
      await tester.pumpAndSettle();

      // Collapsed summary visible under Mother
      expect(find.text("Heart and vascular conditions, Stroke, Diabetes mellitus • Type 2 diabetes, hypertension"), findsOneWidget);

      // Re-open Mother via summary & REMOVE Stroke
      await tester.tap(find.text("Heart and vascular conditions, Stroke, Diabetes mellitus • Type 2 diabetes, hypertension"));
      await tester.pumpAndSettle();
      expect(find.text("Family history for: Mother"), findsOneWidget);
      await tester.ensureVisible(find.text("Stroke"));
      await tester.tap(find.text("Stroke"));
      await tester.pumpAndSettle();
      await tester.ensureVisible(find.text("Save and collapse"));
      await tester.tap(find.text("Save and collapse"));
      await tester.pumpAndSettle();
      expect(find.text("Heart and vascular conditions, Diabetes mellitus • Type 2 diabetes, hypertension"), findsOneWidget);

      // 2) Open Father independently -> verify Mother's selections do not bleed into Father
      await tester.ensureVisible(find.text("Father"));
      await tester.tap(find.text("Father"));
      await tester.pumpAndSettle();
      expect(find.text("Family history for: Father"), findsOneWidget);

      await tester.ensureVisible(find.text("Oncological / cancer conditions"));
      await tester.tap(find.text("Oncological / cancer conditions"));
      await tester.pumpAndSettle();
      await tester.ensureVisible(find.text("Save and collapse"));
      await tester.tap(find.text("Save and collapse"));
      await tester.pumpAndSettle();
      expect(find.text("Oncological / cancer conditions"), findsOneWidget);

      // 3) Open Other blood relatives -> check validation if relationship omitted, then enter "Aunt"
      await tester.ensureVisible(find.text("Other blood relatives"));
      await tester.tap(find.text("Other blood relatives"));
      await tester.pumpAndSettle();
      await tester.ensureVisible(find.text("Kidney conditions"));
      await tester.tap(find.text("Kidney conditions"));
      await tester.pumpAndSettle();

      await tester.ensureVisible(find.text("Save and collapse"));
      await tester.tap(find.text("Save and collapse"));
      await tester.pumpAndSettle();
      expect(find.text("Please specify the relationship for «Other blood relatives»"), findsOneWidget);

      final relField = find.byWidgetPredicate(
        (w) => w is TextField && w.decoration?.hintText == "e.g. Aunt, uncle, cousin",
      );
      await tester.ensureVisible(relField);
      await tester.enterText(relField, "Aunt");
      await tester.pumpAndSettle();
      await tester.ensureVisible(find.text("Save and collapse"));
      await tester.tap(find.text("Save and collapse"));
      await tester.pumpAndSettle();
      expect(find.text("Aunt: Kidney conditions"), findsOneWidget);

      // Save Card 15 -> proceeds to Card 16
      await tester.ensureVisible(find.text("Next ➡️"));
      await tester.tap(find.text("Next ➡️"));
      await tester.pumpAndSettle();
      expect(find.text("Medications and Supplements 💊"), findsOneWidget);

      var global = vault.getSystemContext('global');
      expect(global['family_history_status'], 'Yes');
      expect(global['family_history_relatives'], ['Mother', 'Father', 'Other blood relatives']);
      final details = global['family_history_details'] as Map;
      expect(details['Mother']['conditions'], ['Heart and vascular conditions', 'Diabetes mellitus']);
      expect(details['Mother']['details'], 'Type 2 diabetes, hypertension');
      expect(details['Father']['conditions'], ['Oncological / cancer conditions']);
      expect(details['Father']['details'], isNull);
      expect(details['Other blood relatives']['relationship_detail'], 'Aunt');
      expect(details['Other blood relatives']['conditions'], ['Kidney conditions']);

      // Go back and switch to "No, as far as I know" -> zero false positives in Vault
      await tester.tap(find.byIcon(Icons.arrow_back));
      await tester.pumpAndSettle();
      await tester.tap(find.text("No, as far as I know"));
      await tester.pumpAndSettle();
      await tester.ensureVisible(find.text("Next ➡️"));
      await tester.tap(find.text("Next ➡️"));
      await tester.pumpAndSettle();

      global = vault.getSystemContext('global');
      expect(global['family_history_status'], 'No, as far as I know');
      expect(global['family_history_relatives'], isNull);
      expect(global['family_history_details'], isNull);
      expect(global['heredity_risks'], 'No, as far as I know');

      vault.dispose();
      authManager.dispose();
      apiClient.close();
    });

    testWidgets('3. Card 16: Medications & Supplements — 4 states, multi-category matryoshka, accidental uncheck recovery & explicit clear', (tester) async {
      tester.view.physicalSize = const Size(1080, 2400);
      tester.view.devicePixelRatio = 1.0;
      addTearDown(() => tester.view.resetPhysicalSize());

      final mockClient = MockClient((request) async => http.Response('{}', 200));
      final apiClient = AegisApiClient(client: mockClient);
      final authManager = AuthSessionManager(apiClient: apiClient, storage: memoryAuthStorage);
      final vault = VaultService(storage: memoryVaultStorage, authSessionManager: authManager);
      await vault.initFuture;

      _fillCards1To14(vault);
      vault.saveAnamnesis('family_history_status', 'No, as far as I know');
      vault.saveAnamnesis('heredity_risks', 'No, as far as I know');

      await tester.pumpWidget(_buildTestApp(vault: vault, authManager: authManager, apiClient: apiClient));
      await tester.pumpAndSettle();

      final pageView = tester.widget<PageView>(find.byType(PageView));
      pageView.controller!.jumpToPage(16);
      await tester.pumpAndSettle();

      expect(find.text("Medications and Supplements 💊"), findsOneWidget);
      expect(find.text("Do you currently take any medications, vitamins, or supplements?"), findsOneWidget);
      expect(find.text("Yes"), findsOneWidget);
      expect(find.text("No"), findsOneWidget);
      expect(find.text("Not sure"), findsOneWidget);
      expect(find.text("Prefer not to answer"), findsOneWidget);

      await tester.tap(find.text("Yes"));
      await tester.pumpAndSettle();

      // 1) Open Prescription medications -> empty name check -> enter comma/newline items & free-text course
      await tester.ensureVisible(find.text("Prescription medications"));
      await tester.tap(find.text("Prescription medications"));
      await tester.pumpAndSettle();

      await tester.ensureVisible(find.text("Save and collapse"));
      await tester.tap(find.text("Save and collapse"));
      await tester.pumpAndSettle();
      expect(find.text("Please enter at least one medication or supplement name for «Prescription medications»"), findsOneWidget);

      final rxNameField = find.byWidgetPredicate(
        (w) => w is TextField && w.decoration?.hintText == "e.g. Amoxicillin, tablets; physician-prescribed injections",
      );
      await tester.ensureVisible(rxNameField);
      await tester.enterText(rxNameField, "Amoxicillin, tablets\nPhysician-prescribed injections");
      await tester.pumpAndSettle();

      final rxCourseField = find.byWidgetPredicate(
        (w) => w is TextField && w.decoration?.hintText == "e.g. I have been taking a prescribed antibiotic for a week, started November 10",
      );
      await tester.ensureVisible(rxCourseField);
      await tester.enterText(rxCourseField, "Taking antibiotic for a week");
      await tester.pumpAndSettle();

      await tester.ensureVisible(find.text("Save and collapse"));
      await tester.tap(find.text("Save and collapse"));
      await tester.pumpAndSettle();
      expect(find.text("Amoxicillin, tablets\nPhysician-prescribed injections • Taking antibiotic for a week"), findsOneWidget);

      // 2) Open Vitamins -> enter name ONLY, leave course EMPTY (optional)
      await tester.ensureVisible(find.text("Vitamins"));
      await tester.tap(find.text("Vitamins"));
      await tester.pumpAndSettle();

      final vitNameField = find.byWidgetPredicate(
        (w) => w is TextField && w.decoration?.hintText == "e.g. Vitamin B6, intramuscular injections; Vitamin D, tablets",
      );
      await tester.ensureVisible(vitNameField);
      await tester.enterText(vitNameField, "Vitamin B6, intramuscular injections; Vitamin D, tablets");
      await tester.pumpAndSettle();

      await tester.ensureVisible(find.text("Save and collapse"));
      await tester.tap(find.text("Save and collapse"));
      await tester.pumpAndSettle();
      expect(find.text("Vitamin B6, intramuscular injections; Vitamin D, tablets"), findsOneWidget);

      // 3) Accidental uncheck of Vitamins via Checkbox -> text must NOT be destroyed!
      await tester.ensureVisible(find.byKey(const ValueKey('med_checkbox_Vitamins')));
      await tester.tap(find.byKey(const ValueKey('med_checkbox_Vitamins')));
      await tester.pumpAndSettle();
      expect(find.text("Draft preserved — re-select category to restore"), findsOneWidget);

      // Re-check Vitamins -> entered text restores automatically!
      await tester.tap(find.byKey(const ValueKey('med_checkbox_Vitamins')));
      await tester.pumpAndSettle();
      expect(find.text("Vitamin B6, intramuscular injections; Vitamin D, tablets"), findsOneWidget);
      await tester.ensureVisible(find.text("Save and collapse"));
      await tester.tap(find.text("Save and collapse"));
      await tester.pumpAndSettle();

      // 4) Test explicit permanent clear on Supplements and herbal products
      await tester.ensureVisible(find.text("Supplements and herbal products"));
      await tester.tap(find.text("Supplements and herbal products"));
      await tester.pumpAndSettle();
      final suppField = find.byWidgetPredicate(
        (w) => w is TextField && w.decoration?.hintText == "e.g. Ashwagandha, herbal extract; other dietary supplements",
      );
      await tester.enterText(suppField, "Ashwagandha, herbal extract");
      await tester.pumpAndSettle();

      // Uncheck Supplements -> draft preserved -> click explicit "Discard draft" -> permanently wiped
      await tester.ensureVisible(find.byKey(const ValueKey('med_checkbox_Supplements and herbal products')));
      await tester.tap(find.byKey(const ValueKey('med_checkbox_Supplements and herbal products')));
      await tester.pumpAndSettle();
      expect(find.text("Discard draft"), findsOneWidget);
      await tester.tap(find.text("Discard draft"));
      await tester.pumpAndSettle();
      expect(find.text("Discard draft"), findsNothing);

      // Re-check Supplements -> confirms draft was cleared only upon explicit Discard action
      await tester.tap(find.byKey(const ValueKey('med_checkbox_Supplements and herbal products')));
      await tester.pumpAndSettle();
      expect(find.text("Ashwagandha, herbal extract"), findsNothing);
      // Uncheck Supplements so only Prescription + Vitamins remain active
      await tester.tap(find.byKey(const ValueKey('med_checkbox_Supplements and herbal products')));
      await tester.pumpAndSettle();

      // Switch to "No" and tap Finish -> must NOT store Prescription or Vitamins in Vault!
      await tester.ensureVisible(find.text("No"));
      await tester.tap(find.text("No"));
      await tester.pumpAndSettle();

      await tester.ensureVisible(find.text("Finish 🚀"));
      await tester.tap(find.text("Finish 🚀"));
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 300));
      // Close device pairing modal if opened
      if (find.byType(Dialog).evaluate().isNotEmpty || find.text("Pair Device").evaluate().isNotEmpty) {
        Navigator.of(tester.element(find.text("Finish 🚀"))).maybePop();
        await tester.pumpAndSettle();
      }

      var global = vault.getSystemContext('global');
      expect(global['medications_status'], 'No');
      expect(global['medication_categories'], isNull);
      expect(global['medication_details'], isNull);
      expect(global['medications'], 'No');

      vault.dispose();
      authManager.dispose();
      apiClient.close();
    });

    testWidgets('4. Vault Cold Reload, Patient Isolation & Full Stage 5 Mock API Payload Sync', (tester) async {
      tester.view.physicalSize = const Size(1080, 2400);
      tester.view.devicePixelRatio = 1.0;
      addTearDown(() => tester.view.resetPhysicalSize());

      Map<String, dynamic>? capturedIntakePayload;
      final mockClient = MockClient((request) async {
        if (request.url.path == '/api/v1/client/cascade/intake/anamnesis') {
          capturedIntakePayload = json.decode(request.body) as Map<String, dynamic>;
          return http.Response(
            json.encode({
              'cycle_id': 'cycle_stage5_001',
              'status': 'processed',
              'intake_version': 1,
              'created_at_utc': '2026-11-13T10:00:00Z',
              'privacy_notice': 'Processed in blind mode',
            }),
            200,
          );
        }
        return http.Response('{}', 200);
      });
      final apiClient = AegisApiClient(client: mockClient);
      final authManager = AuthSessionManager(apiClient: apiClient, storage: memoryAuthStorage);
      final vault1 = VaultService(storage: memoryVaultStorage, authSessionManager: authManager);
      await vault1.initFuture;

      // Save complete 17 cards including Stage 5 data in guest draft
      _fillCards1To14(vault1);
      vault1.saveAnamnesis('family_history_status', 'Yes');
      vault1.saveAnamnesis('family_history_relatives', ['Mother']);
      vault1.saveAnamnesis('family_history_details', {
        'Mother': {
          'conditions': ['Diabetes mellitus'],
          'details': 'Type 2 diabetes',
        },
      });
      vault1.saveAnamnesis('heredity_risks', 'Mother: Diabetes mellitus');

      vault1.saveAnamnesis('medications_status', 'Yes');
      vault1.saveAnamnesis('medication_categories', ['Over-the-counter medications']);
      vault1.saveAnamnesis('medication_details', {
        'Over-the-counter medications': {
          'names': 'Ibuprofen, tablets; nasal spray',
          'course': 'I only take a painkiller for headaches',
        },
      });
      vault1.saveAnamnesis('medications', 'Over-the-counter medications (Ibuprofen, tablets; nasal spray)');
      await vault1.flushPendingWrites();
      vault1.dispose();

      // Cold restart VaultService
      VaultService.resetWriteQueueForTesting();
      final vault2 = VaultService(storage: memoryVaultStorage, authSessionManager: authManager);
      await vault2.initFuture;
      expect(await vault2.hasGuestAnamnesisDraft(), isTrue);

      // Pair and activate Patient A -> associate guest draft
      authManager.setSessionForTesting(
        AuthSession(
          accessToken: _buildTestJwt(patientId: 'pat_stage5_A', deviceId: 'dev_5a'),
          refreshToken: 'rt_5a',
          expiresIn: 3600,
          expiresAt: DateTime.now().toUtc().add(const Duration(hours: 1)),
          refreshExpiresAt: DateTime.now().toUtc().add(const Duration(days: 30)),
          patientId: 'pat_stage5_A',
          deviceId: 'dev_5a',
          scope: AegisScopes.standardDeviceScopes.join(' '),
        ),
        status: AuthSessionStatus.authenticated,
      );
      await vault2.setActivePatientId('pat_stage5_A');
      await vault2.associateGuestDraftToActivePatient();

      await tester.pumpWidget(_buildTestApp(vault: vault2, authManager: authManager, apiClient: apiClient));
      await tester.pumpAndSettle();

      // Card 15 restores Mother summary cleanly
      final pageView = tester.widget<PageView>(find.byType(PageView));
      pageView.controller!.jumpToPage(15);
      await tester.pumpAndSettle();
      expect(find.text("Diabetes mellitus • Type 2 diabetes"), findsOneWidget);

      // Card 16 restores Over-the-counter summary cleanly & Finish submits full DTO to backend mock
      pageView.controller!.jumpToPage(16);
      await tester.pumpAndSettle();
      expect(find.text("Ibuprofen, tablets; nasal spray • I only take a painkiller for headaches"), findsOneWidget);

      await tester.ensureVisible(find.text("Finish 🚀"));
      await tester.tap(find.text("Finish 🚀"));
      await tester.pumpAndSettle();

      expect(capturedIntakePayload, isNotNull);
      final answersJson = capturedIntakePayload!['answers'] as Map<String, dynamic>;
      expect(answersJson['family_history_status'], 'Yes');
      expect(answersJson['family_history_relatives'], ['Mother']);
      expect(answersJson['family_history_details']['Mother']['conditions'], ['Diabetes mellitus']);
      expect(answersJson['medications_status'], 'Yes');
      expect(answersJson['medication_categories'], ['Over-the-counter medications']);
      expect(
        answersJson['medication_details']['Over-the-counter medications']['names'],
        'Ibuprofen, tablets; nasal spray',
      );
      expect(find.text("Your Profile is Configured! 🚀"), findsOneWidget);

      // Patient Isolation: switch to Patient B -> zero leakage on Cards 15 & 16
      await vault2.setActivePatientId('pat_stage5_B');
      await tester.pumpAndSettle();
      expect(vault2.getSystemContext('global')['family_history_status'], isNull);
      expect(vault2.getSystemContext('global')['medications_status'], isNull);

      pageView.controller!.jumpToPage(15);
      await tester.pumpAndSettle();
      expect(find.text("Diabetes mellitus • Type 2 diabetes"), findsNothing);

      // Switch back to Patient A -> Patient A Stage 5 data remains intact in isolated Vault
      await vault2.setActivePatientId('pat_stage5_A');
      await tester.pumpAndSettle();
      expect(vault2.getSystemContext('global')['family_history_status'], 'Yes');
      expect(vault2.getSystemContext('global')['medications_status'], 'Yes');

      vault2.dispose();
      authManager.dispose();
      apiClient.close();
    });

    testWidgets('5. Card 16: Manual backspace wipe drops stale draft & legacy medications requires explicit Card 16 status', (tester) async {
      tester.view.physicalSize = const Size(1080, 2400);
      tester.view.devicePixelRatio = 1.0;
      addTearDown(() => tester.view.resetPhysicalSize());

      final mockClient = MockClient((request) async => http.Response('{}', 200));
      final apiClient = AegisApiClient(client: mockClient);
      final authManager = AuthSessionManager(apiClient: apiClient, storage: memoryAuthStorage);
      final vault = VaultService(storage: memoryVaultStorage, authSessionManager: authManager);
      await vault.initFuture;

      _fillCards1To14(vault);
      vault.saveAnamnesis('family_history_status', 'No, as far as I know');
      vault.saveAnamnesis('heredity_risks', 'No, as far as I know');
      // Historical legacy record without medications_status
      vault.saveAnamnesis('medications', 'Vitamins', userText: 'Vitamin D3');

      await tester.pumpWidget(_buildTestApp(vault: vault, authManager: authManager, apiClient: apiClient));
      await tester.pumpAndSettle();

      final pageView = tester.widget<PageView>(find.byType(PageView));
      pageView.controller!.jumpToPage(16);
      await tester.pumpAndSettle();

      // 1) Legacy text alone does NOT bypass Card 16 status selection
      await tester.ensureVisible(find.text("Finish 🚀"));
      await tester.tap(find.text("Finish 🚀"));
      await tester.pumpAndSettle();
      expect(find.text("Please select one of the options above"), findsOneWidget);
      // Historical record remains untouched in Vault until user explicitly updates Card 16
      expect(vault.getSystemContext('global')['medications'], 'Vitamins');

      // 2) Manual deletion to empty via keyboard must evict stale draft from _medicationDraftArchive
      await tester.tap(find.text("Yes"));
      await tester.pumpAndSettle();

      await tester.ensureVisible(find.text("Over-the-counter medications"));
      await tester.tap(find.text("Over-the-counter medications"));
      await tester.pumpAndSettle();

      final otcNameField = find.byWidgetPredicate(
        (w) => w is TextField && w.decoration?.hintText == "e.g. Ibuprofen, tablets; nasal spray",
      );
      final otcCourseField = find.byWidgetPredicate(
        (w) => w is TextField && w.decoration?.hintText == "e.g. I only take a painkiller for headaches",
      );
      await tester.enterText(otcNameField, "Ibuprofen, tablets");
      await tester.enterText(otcCourseField, "For headaches");
      await tester.pumpAndSettle();

      // Now manually clear both fields to empty
      await tester.enterText(otcNameField, "");
      await tester.enterText(otcCourseField, "");
      await tester.pumpAndSettle();

      // Uncheck -> must NOT show "Draft preserved"
      await tester.tap(find.byKey(const ValueKey('med_checkbox_Over-the-counter medications')));
      await tester.pumpAndSettle();
      expect(find.text("Draft preserved — re-select category to restore"), findsNothing);

      // Re-check -> must NOT resurrect deleted text
      await tester.tap(find.byKey(const ValueKey('med_checkbox_Over-the-counter medications')));
      await tester.pumpAndSettle();
      expect(find.text("Ibuprofen, tablets"), findsNothing);
      expect(find.text("For headaches"), findsNothing);

      vault.dispose();
      authManager.dispose();
      apiClient.close();
    });
  });
}
