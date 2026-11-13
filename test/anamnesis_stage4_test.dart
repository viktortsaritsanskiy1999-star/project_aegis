import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:provider/provider.dart';

import 'package:aegis_health/anamnesis_screen.dart';
import 'package:aegis_health/services/api/aegis_api_client.dart';
import 'package:aegis_health/services/api/medical_ai_api_models.dart';
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

  group('PROJECT AEGIS — ANAMNESIS CARDS V2: STAGE 4 TESTS', () {
    test('1. DTO Roundtrip: AnamnesisQuestionnaireAnswers Stage 4 serialization', () {
      final answers = AnamnesisQuestionnaireAnswers(
        ageConfirmation: 'Confirmed',
        gender: 'Female',
        age: 40,
        height: 165,
        weight: 60,
        nicotineStatus: 'Non-user',
        alcoholStatus: 'Non-drinker',
        specFactors: 'None',
        sleepQuality: 'Normal',
        dietTypes: 'Balanced',
        dietHabits: 'Regular',
        giSignals: 'None',
        workType: 'Mostly sitting',
        heredityRisks: 'None',
        medications: 'None',
        // Stage 4 fields
        chronicDiseases: 'Cancer or oncological history (Breast cancer)',
        oncoStatus: 'Remission',
        chronicCategories: ['Cancer or oncological history'],
        chronicConditionDetails: {
          'Cancer or oncological history': {
            'diagnosis': 'Breast cancer',
            'diagnosed_when': '2020',
            'onco_status': 'Remission',
            'current_status': 'In remission (if applicable)',
            'currently_bothers': "No, it doesn't currently bother me",
          },
        },
        userTextChronicDiseases: 'Appendectomy in 2010',
        recentIllness: 'Cold, flu, COVID-19 or another respiratory viral infection (Within the last month, Improving)',
        recentEvents: ['Cold, flu, COVID-19 or another respiratory viral infection'],
        recentEventDetails: {
          'Cold, flu, COVID-19 or another respiratory viral infection': {
            'timeframe': 'Within the last month',
            'timeframe_code': '8_30_days',
            'recovery_status': 'Improving',
            'details': 'Mild fever for 2 days',
          },
        },
        userTextRecentIllness: 'Feeling stronger now',
        criticalInfections: 'Negative test results reported',
        infectiousHistoryStatus: 'I have had negative test results',
        infectiousTestedNegative: ['Hepatitis B', 'HIV'],
        infectiousLastTested: 'About 6 months ago',
      );

      final json = answers.toJson();
      expect(json['onco_status'], 'Remission');
      expect(json['chronic_categories'], ['Cancer or oncological history']);
      expect(json['chronic_condition_details']['Cancer or oncological history']['diagnosis'], 'Breast cancer');
      expect(json['recent_events'], ['Cold, flu, COVID-19 or another respiratory viral infection']);
      expect(json['recent_event_details']['Cold, flu, COVID-19 or another respiratory viral infection']['timeframe_code'], '8_30_days');
      expect(json['infectious_history_status'], 'I have had negative test results');
      expect(json['infectious_tested_negative'], ['Hepatitis B', 'HIV']);
      expect(json['infectious_last_tested'], 'About 6 months ago');

      final deserialized = AnamnesisQuestionnaireAnswers.fromMap(json);
      expect(deserialized.oncoStatus, 'Remission');
      expect(deserialized.chronicCategories, answers.chronicCategories);
      expect(deserialized.chronicConditionDetails?['Cancer or oncological history']?['onco_status'], 'Remission');
      expect(deserialized.recentEvents, answers.recentEvents);
      expect(deserialized.recentEventDetails?['Cold, flu, COVID-19 or another respiratory viral infection']?['timeframe_code'], '8_30_days');
      expect(deserialized.infectiousHistoryStatus, 'I have had negative test results');
      expect(deserialized.infectiousTestedNegative, ['Hepatitis B', 'HIV']);
      expect(deserialized.infectiousLastTested, 'About 6 months ago');
    });

    testWidgets('2. Card 12: Chronic Conditions — Category hints, complaints, onco_status & collapse', (tester) async {
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
      pageView.controller!.jumpToPage(12);
      await tester.pumpAndSettle();

      expect(find.text("Chronic Conditions & Medical History"), findsOneWidget);

      // Select Heart or blood pressure conditions
      final heartOpt = find.text("Heart or blood pressure conditions");
      await tester.ensureVisible(heartOpt);
      await tester.tap(heartOpt);
      await tester.pumpAndSettle();

      // Verify category-specific diagnosis hint
      expect(
        find.byWidgetPredicate((w) => w is TextField && w.decoration?.hintText == 'e.g. Arterial hypertension, congenital heart defect, arrhythmia'),
        findsOneWidget,
      );

      // Enter diagnosis & when
      final diagField = find.byWidgetPredicate((w) => w is TextField && w.decoration?.hintText == 'e.g. Arterial hypertension, congenital heart defect, arrhythmia');
      await tester.enterText(diagField, "Arterial hypertension");
      await tester.pumpAndSettle();

      // Select current status
      await tester.ensureVisible(find.text("Controlled or stable"));
      await tester.tap(find.text("Controlled or stable"));
      await tester.pumpAndSettle();

      // Select occasional symptoms -> reveals symptom field with heart-specific hint
      await tester.ensureVisible(find.text("Yes, I occasionally experience symptoms"));
      await tester.tap(find.text("Yes, I occasionally experience symptoms"));
      await tester.pumpAndSettle();

      final symField = find.byWidgetPredicate((w) => w is TextField && w.decoration?.hintText == 'e.g. Occasional palpitations or shortness of breath');
      expect(symField, findsOneWidget);
      await tester.enterText(symField, "Mild dizziness in morning");
      await tester.pumpAndSettle();

      // Switch to No, it doesn't currently bother me -> hides & clears symptom field
      await tester.ensureVisible(find.text("No, it doesn't currently bother me"));
      await tester.tap(find.text("No, it doesn't currently bother me"));
      await tester.pumpAndSettle();
      expect(symField, findsNothing);

      // Save and collapse
      await tester.ensureVisible(find.text("Save and collapse"));
      await tester.tap(find.text("Save and collapse"));
      await tester.pumpAndSettle();

      // Collapsed view shows diagnosis and current status
      expect(find.text("Arterial hypertension • Controlled or stable"), findsOneWidget);

      // Tap saved summary to re-open editor, then collapse again
      await tester.tap(find.text("Arterial hypertension • Controlled or stable"));
      await tester.pumpAndSettle();
      expect(find.text("Save and collapse"), findsOneWidget);
      await tester.tap(find.text("Save and collapse"));
      await tester.pumpAndSettle();

      // Select Cancer or oncological history -> opens cancer matryoshka with onco_status
      final cancerOpt = find.text("Cancer or oncological history");
      await tester.ensureVisible(cancerOpt);
      await tester.tap(cancerOpt);
      await tester.pumpAndSettle();

      final cancerDiagField = find.byWidgetPredicate((w) => w is TextField && w.decoration?.hintText == 'e.g. Breast cancer, colorectal cancer, lymphoma');
      expect(cancerDiagField, findsOneWidget);
      await tester.enterText(cancerDiagField, "Breast cancer");
      await tester.pumpAndSettle();

      // Select onco_status = Remission
      await tester.ensureVisible(find.text("Remission"));
      await tester.tap(find.text("Remission"));
      await tester.pumpAndSettle();

      await tester.ensureVisible(find.text("In remission (if applicable)"));
      await tester.tap(find.text("In remission (if applicable)"));
      await tester.pumpAndSettle();

      await tester.ensureVisible(find.text("No, it doesn't currently bother me"));
      await tester.tap(find.text("No, it doesn't currently bother me"));
      await tester.pumpAndSettle();

      await tester.ensureVisible(find.text("Save and collapse"));
      await tester.tap(find.text("Save and collapse"));
      await tester.pumpAndSettle();

      // Next -> navigates to Card 13 and persists multiple diagnoses + onco_status
      final nextBtn = find.text("Next ➡️");
      await tester.ensureVisible(nextBtn);
      await tester.tap(nextBtn);
      await tester.pumpAndSettle();

      expect(find.text("Recent Illnesses & Health Events"), findsOneWidget);

      final global = vault.getSystemContext('global');
      expect(global['chronic_categories'], containsAll(['Heart or blood pressure conditions', 'Cancer or oncological history']));
      expect(global['onco_status'], 'Remission');
      final details = global['chronic_condition_details'] as Map;
      expect(details['Heart or blood pressure conditions']['diagnosis'], 'Arterial hypertension');
      expect(details['Heart or blood pressure conditions']['current_symptoms_text'], isNull);
      expect(details['Cancer or oncological history']['onco_status'], 'Remission');

      vault.dispose();
      authManager.dispose();
      apiClient.close();
    });

    testWidgets('3. Card 13: Recent Illnesses — Merged COVID-19, non-overlapping timeframes & mutual exclusion', (tester) async {
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
      pageView.controller!.jumpToPage(13);
      await tester.pumpAndSettle();

      expect(find.text("Recent Illnesses & Health Events"), findsOneWidget);
      // COVID-19 must NOT be a standalone item
      expect(find.text("COVID-19"), findsNothing);
      expect(find.text("Cold, flu, COVID-19 or another respiratory viral infection"), findsOneWidget);

      // Select respiratory viral infection
      await tester.ensureVisible(find.text("Cold, flu, COVID-19 or another respiratory viral infection"));
      await tester.tap(find.text("Cold, flu, COVID-19 or another respiratory viral infection"));
      await tester.pumpAndSettle();

      await tester.ensureVisible(find.text("Within the last month"));
      await tester.tap(find.text("Within the last month"));
      await tester.pumpAndSettle();

      await tester.ensureVisible(find.text("Improving"));
      await tester.tap(find.text("Improving"));
      await tester.pumpAndSettle();

      await tester.ensureVisible(find.text("Save and collapse"));
      await tester.tap(find.text("Save and collapse"));
      await tester.pumpAndSettle();

      expect(find.text("Within the last month • Improving"), findsOneWidget);

      // Test mutual exclusion with None of the above -> clears previous event
      await tester.ensureVisible(find.text("None of the above"));
      await tester.tap(find.text("None of the above"));
      await tester.pumpAndSettle();
      expect(find.text("Within the last month • Improving"), findsNothing);

      // Re-select Surgery or medical procedure -> clears None of the above
      await tester.ensureVisible(find.text("Surgery or medical procedure"));
      await tester.tap(find.text("Surgery or medical procedure"));
      await tester.pumpAndSettle();

      await tester.ensureVisible(find.text("1–3 months ago"));
      await tester.tap(find.text("1–3 months ago"));
      await tester.pumpAndSettle();

      await tester.ensureVisible(find.text("Fully recovered"));
      await tester.tap(find.text("Fully recovered"));
      await tester.pumpAndSettle();

      await tester.ensureVisible(find.text("Save and collapse"));
      await tester.tap(find.text("Save and collapse"));
      await tester.pumpAndSettle();

      final nextBtn = find.text("Next ➡️");
      await tester.ensureVisible(nextBtn);
      await tester.tap(nextBtn);
      await tester.pumpAndSettle();

      expect(find.text("Infectious Disease History"), findsOneWidget);

      final global = vault.getSystemContext('global');
      expect(global['recent_events'], ['Surgery or medical procedure']);
      final details = global['recent_event_details'] as Map;
      expect(details['Surgery or medical procedure']['timeframe_code'], '31_90_days');
      expect(details['Surgery or medical procedure']['recovery_status'], 'Fully recovered');

      vault.dispose();
      authManager.dispose();
      apiClient.close();
    });

    testWidgets('4. Card 14: Infectious Disease History — Delicate UI, conflict cleanup & Prefer not to answer', (tester) async {
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
      pageView.controller!.jumpToPage(14);
      await tester.pumpAndSettle();

      expect(find.text("Infectious Disease History"), findsOneWidget);
      expect(
        find.text("Your health information is personal and treated with care. These questions help us understand your medical history. You can choose not to answer."),
        findsOneWidget,
      );

      // Select Diagnosed branch -> check empty validation -> select Hepatitis B
      await tester.ensureVisible(find.text("I have been diagnosed with one or more of these infections"));
      await tester.tap(find.text("I have been diagnosed with one or more of these infections"));
      await tester.pumpAndSettle();

      final nextBtn = find.text("Next ➡️");
      await tester.ensureVisible(nextBtn);
      await tester.tap(nextBtn);
      await tester.pumpAndSettle();
      expect(find.text("Please select at least one diagnosed infection"), findsOneWidget);

      await tester.ensureVisible(find.text("Hepatitis B"));
      await tester.tap(find.text("Hepatitis B"));
      await tester.pumpAndSettle();

      // Switch to Negative test results -> clears diagnosed branch & shows optional tested list
      await tester.ensureVisible(find.text("I have had negative test results"));
      await tester.tap(find.text("I have had negative test results"));
      await tester.pumpAndSettle();

      expect(find.text("Which infections were you tested for? (optional)"), findsOneWidget);
      await tester.ensureVisible(find.text("HIV"));
      await tester.tap(find.text("HIV"));
      await tester.pumpAndSettle();

      final lastTestedField = find.byWidgetPredicate((w) => w is TextField && w.decoration?.hintText == 'e.g. About 6 months ago');
      await tester.enterText(lastTestedField, "3 months ago");
      await tester.pumpAndSettle();

      // Tap Next -> saves negative test branch cleanly without leaking HIV into critical_infections
      await tester.ensureVisible(nextBtn);
      await tester.tap(nextBtn);
      await tester.pumpAndSettle();

      expect(find.text("Family Archive 🌳"), findsOneWidget);
      var global = vault.getSystemContext('global');
      expect(global['infectious_history_status'], 'I have had negative test results');
      expect(global['infectious_diagnosed'], isNull);
      expect(global['infectious_tested_negative'], ['HIV']);
      expect(global['infectious_last_tested'], '3 months ago');
      expect(global['critical_infections'], 'Negative test results reported');

      // Go back via Back button and test "Prefer not to answer"
      await tester.tap(find.byIcon(Icons.arrow_back));
      await tester.pumpAndSettle();

      await tester.ensureVisible(find.text("Prefer not to answer"));
      await tester.tap(find.text("Prefer not to answer"));
      await tester.pumpAndSettle();

      // Subfields hidden
      expect(find.text("Which infections were you tested for? (optional)"), findsNothing);

      await tester.ensureVisible(nextBtn);
      await tester.tap(nextBtn);
      await tester.pumpAndSettle();

      global = vault.getSystemContext('global');
      expect(global['infectious_history_status'], 'Prefer not to answer');
      expect(global['infectious_tested_negative'], isNull);
      expect(global['infectious_last_tested'], isNull);
      expect(global['critical_infections'], 'User declined to answer');

      vault.dispose();
      authManager.dispose();
      apiClient.close();
    });

    testWidgets('5. Guest draft restore & Patient Isolation for Stage 4', (tester) async {
      tester.view.physicalSize = const Size(1080, 2400);
      tester.view.devicePixelRatio = 1.0;
      addTearDown(() => tester.view.resetPhysicalSize());

      final mockClient = MockClient((request) async => http.Response('{}', 200));
      final apiClient = AegisApiClient(client: mockClient);
      final authManager = AuthSessionManager(apiClient: apiClient, storage: memoryAuthStorage);
      final vault1 = VaultService(storage: memoryVaultStorage, authSessionManager: authManager);
      await vault1.initFuture;

      // 1. Save Stage 4 guest draft (unauthenticated session)
      vault1.saveAnamnesis('chronic_categories', ['Diabetes or blood sugar disorders']);
      vault1.saveAnamnesis('chronic_condition_details', {
        'Diabetes or blood sugar disorders': {
          'diagnosis': 'Type 2 diabetes',
          'current_status': 'Controlled or stable',
          'currently_bothers': "No, it doesn't currently bother me",
        },
      });
      vault1.saveAnamnesis('chronic_diseases', 'Diabetes or blood sugar disorders (Type 2 diabetes)');
      await vault1.flushPendingWrites();
      vault1.dispose();

      // 2. New VaultService simulates cold restart & verifies guest draft persistence contract
      VaultService.resetWriteQueueForTesting();
      final vault2 = VaultService(storage: memoryVaultStorage, authSessionManager: authManager);
      await vault2.initFuture;

      expect(await vault2.hasGuestAnamnesisDraft(), isTrue);
      expect(vault2.getSystemContext('global')['chronic_categories'], ['Diabetes or blood sugar disorders']);

      // 3. Activate Patient A via actual VaultService API: guest draft is isolated until associated
      await vault2.setActivePatientId('pat_stage4_A');
      expect(vault2.getSystemContext('global')['chronic_categories'], isNull);

      // Explicitly associate guest draft to Patient A
      final associated = await vault2.associateGuestDraftToActivePatient();
      expect(associated, isTrue);

      await tester.pumpWidget(_buildTestApp(vault: vault2, authManager: authManager, apiClient: apiClient));
      await tester.pumpAndSettle();

      final pageView = tester.widget<PageView>(find.byType(PageView));
      pageView.controller!.jumpToPage(12);
      await tester.pumpAndSettle();

      // Verify restored collapsed diagnosis badge on Patient A UI
      expect(find.text("Type 2 diabetes • Controlled or stable"), findsOneWidget);

      // 4. Switch to Patient B via setActivePatientId -> immediate purge & zero leakage
      await vault2.setActivePatientId('pat_stage4_B');
      await tester.pumpAndSettle();

      expect(vault2.activePatientId, 'pat_stage4_B');
      expect(vault2.getSystemContext('global')['chronic_categories'], isNull);
      expect(vault2.getSystemContext('global')['chronic_condition_details'], isNull);

      // Verify Patient B Page 12 has no leaked Patient A state
      pageView.controller!.jumpToPage(12);
      await tester.pumpAndSettle();
      expect(find.text("Type 2 diabetes • Controlled or stable"), findsNothing);

      // 5. Switch back to Patient A -> Patient A Stage 4 data remains intact in isolated storage
      await vault2.setActivePatientId('pat_stage4_A');
      await tester.pumpAndSettle();
      expect(vault2.getSystemContext('global')['chronic_categories'], ['Diabetes or blood sugar disorders']);
      expect(
        (vault2.getSystemContext('global')['chronic_condition_details'] as Map)['Diabetes or blood sugar disorders']['diagnosis'],
        'Type 2 diabetes',
      );

      vault2.dispose();
      authManager.dispose();
      apiClient.close();
    });
  });
}
