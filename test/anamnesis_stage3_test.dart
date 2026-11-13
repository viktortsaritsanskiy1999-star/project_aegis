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

  group('PROJECT AEGIS — ANAMNESIS CARDS V2: STAGE 3 TESTS', () {
    test('1. DTO Roundtrip: AnamnesisQuestionnaireAnswers Stage 3 fields', () {
      final answers = AnamnesisQuestionnaireAnswers(
        ageConfirmation: 'Confirmed',
        gender: 'Male',
        age: 32,
        height: 180,
        weight: 78,
        nicotineStatus: 'Non-user',
        alcoholStatus: 'Non-drinker',
        specFactors: 'None',
        sleepQuality: 'Normal',
        dietTypes: 'Balanced',
        dietHabits: 'Regular',
        chronicDiseases: 'None',
        recentIllness: 'None',
        criticalInfections: 'None',
        heredityRisks: 'None',
        medications: 'None',
        // Stage 3 fields
        giSignals: 'Heartburn or acid reflux, Bloating or abdominal fullness',
        giSymptoms: ['Heartburn or acid reflux', 'Bloating or abdominal fullness'],
        giSymptomDetails: {
          'Heartburn or acid reflux': {
            'frequency': 'Several times a week',
            'duration': '1–6 months',
          },
          'Bloating or abdominal fullness': {
            'frequency': 'Occasional (1–2 times a month)',
            'duration': 'Less than 1 month',
          },
        },
        userTextGiSignals: 'Noticeable after eating late',
        workType: 'Mostly sitting',
        workOccupation: 'Software Architect',
        workSchedule: 'Regular daytime schedule',
        workStress: 'Moderate',
        userTextWork: 'Adjustable standing desk',
        sportLevel: 'Moderate — regular movement or exercise',
        activityDaysPerWeek: '3–4 days',
        activityDurationMinutes: '30–60 minutes',
        activityTypes: ['Running / Jogging', 'Gym / Strength training'],
        activityOtherDetails: null,
        userTextSport: 'Morning jogging 5km',
      );

      final json = answers.toJson();
      expect(json['gi_signals'], 'Heartburn or acid reflux, Bloating or abdominal fullness');
      expect(json['gi_symptoms'], ['Heartburn or acid reflux', 'Bloating or abdominal fullness']);
      expect(json['gi_symptom_details']['Heartburn or acid reflux']['frequency'], 'Several times a week');
      expect(json['user_text_gi_signals'], 'Noticeable after eating late');
      expect(json['work_occupation'], 'Software Architect');
      expect(json['work_type'], 'Mostly sitting');
      expect(json['work_schedule'], 'Regular daytime schedule');
      expect(json['work_stress'], 'Moderate');
      expect(json['user_text_work'], 'Adjustable standing desk');
      expect(json['sport_level'], 'Moderate — regular movement or exercise');
      expect(json['activity_days_per_week'], '3–4 days');
      expect(json['activity_duration_minutes'], '30–60 minutes');
      expect(json['activity_types'], ['Running / Jogging', 'Gym / Strength training']);
      expect(json['user_text_sport'], 'Morning jogging 5km');

      final deserialized = AnamnesisQuestionnaireAnswers.fromMap(json);
      expect(deserialized.giSignals, answers.giSignals);
      expect(deserialized.giSymptoms, answers.giSymptoms);
      expect(deserialized.giSymptomDetails?['Heartburn or acid reflux']?['duration'], '1–6 months');
      expect(deserialized.userTextGiSignals, answers.userTextGiSignals);
      expect(deserialized.workOccupation, answers.workOccupation);
      expect(deserialized.workSchedule, answers.workSchedule);
      expect(deserialized.workStress, answers.workStress);
      expect(deserialized.sportLevel, answers.sportLevel);
      expect(deserialized.activityTypes, answers.activityTypes);
      expect(deserialized.userTextSport, answers.userTextSport);
    });

    testWidgets('2. Card 9: Digestion & GI Signals — Matryoshka refinement & validation', (tester) async {
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
      pageView.controller!.jumpToPage(9);
      await tester.pumpAndSettle();

      expect(find.text("Digestion & Gastrointestinal Health 🎈"), findsOneWidget);

      // Tap Next without selecting any symptom -> error
      final nextBtn = find.text("Next ➡️");
      await tester.ensureVisible(nextBtn);
      await tester.tap(nextBtn);
      await tester.pumpAndSettle();
      expect(find.text("Please select your digestive symptoms or «None of the above»"), findsOneWidget);

      // Select "Heartburn / Acid taste"
      final heartburnOption = find.text("Heartburn / Acid taste");
      await tester.ensureVisible(heartburnOption);
      await tester.tap(heartburnOption);
      await tester.pumpAndSettle();

      // Matryoshka is expanded: "Save and collapse" is visible
      expect(find.text("Save and collapse"), findsOneWidget);

      // Try tapping Next without selecting frequency & duration -> error
      await tester.ensureVisible(nextBtn);
      await tester.tap(nextBtn);
      await tester.pumpAndSettle();
      expect(find.text("Please complete frequency and duration for «Heartburn / Acid taste»"), findsOneWidget);

      // Select frequency "1–3 times a week"
      final freqChip = find.text("1–3 times a week");
      await tester.ensureVisible(freqChip);
      await tester.tap(freqChip);
      await tester.pumpAndSettle();

      // Select duration "For several months"
      final durChip = find.text("For several months");
      await tester.ensureVisible(durChip);
      await tester.tap(durChip);
      await tester.pumpAndSettle();

      // Tap "Save and collapse"
      final saveCollapseBtn = find.text("Save and collapse");
      await tester.ensureVisible(saveCollapseBtn);
      await tester.tap(saveCollapseBtn);
      await tester.pumpAndSettle();

      // Refinement collapsed, badge shown with edit link
      expect(find.text("Details: 1–3 times a week, For several months"), findsOneWidget);
      final editBtn = find.text("[Edit ✏️]");
      expect(editBtn, findsOneWidget);

      // Tap Edit -> expands again
      await tester.tap(editBtn);
      await tester.pumpAndSettle();
      expect(find.text("Save and collapse"), findsOneWidget);

      // Collapse it again
      await tester.tap(find.text("Save and collapse"));
      await tester.pumpAndSettle();

      // Mutual exclusion test: Tap "None of the above"
      final noneOption = find.text("None of the above");
      await tester.ensureVisible(noneOption);
      await tester.tap(noneOption);
      await tester.pumpAndSettle();

      // Heartburn is now cleared
      expect(find.text("Details: 1–3 times a week, For several months"), findsNothing);

      // Re-selecting a symptom clears "None of the above"
      final nauseaOption = find.text("Nausea / Vomiting");
      await tester.ensureVisible(nauseaOption);
      await tester.tap(nauseaOption);
      await tester.pumpAndSettle();

      // Fill Nausea details
      final freqRare = find.text("Less than once a month");
      await tester.ensureVisible(freqRare);
      await tester.tap(freqRare);
      await tester.pumpAndSettle();

      final durFewDays = find.text("For the past few days");
      await tester.ensureVisible(durFewDays);
      await tester.tap(durFewDays);
      await tester.pumpAndSettle();

      await tester.tap(find.text("Save and collapse"));
      await tester.pumpAndSettle();

      // Tap Next -> moves to Card 10 (Work & Daily Routine)
      await tester.ensureVisible(nextBtn);
      await tester.tap(nextBtn);
      await tester.pumpAndSettle();

      expect(find.text("Work & Daily Routine 💼"), findsOneWidget);

      final global = vault.getSystemContext('global');
      expect(global['gi_symptoms'], ['Nausea / Vomiting']);
      final details = global['gi_symptom_details'] as Map;
      expect(details['Nausea / Vomiting']['frequency'], 'Less than once a month');
      expect(details['Nausea / Vomiting']['duration'], 'For the past few days');

      vault.dispose();
      authManager.dispose();
      apiClient.close();
    });

    testWidgets('3. Card 10: Work & Daily Routine — Occupation, schedule & stress validation', (tester) async {
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
      pageView.controller!.jumpToPage(10);
      await tester.pumpAndSettle();

      expect(find.text("Work & Daily Routine 💼"), findsOneWidget);

      final nextBtn = find.text("Next ➡️");

      // Tap Next with no selection -> requires daily work activity
      await tester.ensureVisible(nextBtn);
      await tester.tap(nextBtn);
      await tester.pumpAndSettle();
      expect(find.text("Please select how you spend most of your day"), findsOneWidget);

      // Select activity
      final sittingOption = find.text("Mostly sitting");
      await tester.ensureVisible(sittingOption);
      await tester.tap(sittingOption);
      await tester.pumpAndSettle();

      // Tap Next without schedule -> requires work schedule
      await tester.ensureVisible(nextBtn);
      await tester.tap(nextBtn);
      await tester.pumpAndSettle();
      expect(find.text("Please select your usual schedule"), findsOneWidget);

      // Select schedule
      final daytimeOption = find.text("Regular daytime schedule");
      await tester.ensureVisible(daytimeOption);
      await tester.tap(daytimeOption);
      await tester.pumpAndSettle();

      // Tap Next without stress level -> requires stress level
      await tester.ensureVisible(nextBtn);
      await tester.tap(nextBtn);
      await tester.pumpAndSettle();
      expect(find.text("Please rate your daily stress level"), findsOneWidget);

      // Select stress level
      final stressOption = find.text("Moderate");
      await tester.ensureVisible(stressOption);
      await tester.tap(stressOption);
      await tester.pumpAndSettle();

      // Enter optional occupation
      final occField = find.byType(TextField).first;
      await tester.ensureVisible(occField);
      await tester.enterText(occField, "Neurologist");
      await tester.pumpAndSettle();

      // Tap Next -> moves to Card 11 (Physical Activity)
      await tester.ensureVisible(nextBtn);
      await tester.tap(nextBtn);
      await tester.pumpAndSettle();

      expect(find.text("Physical Activity 🏃"), findsOneWidget);

      final global = vault.getSystemContext('global');
      expect(global['work_occupation'], 'Neurologist');
      expect(global['work_type'], 'Mostly sitting');
      expect(global['work_schedule'], 'Regular daytime schedule');
      expect(global['work_stress'], 'Moderate');

      vault.dispose();
      authManager.dispose();
      apiClient.close();
    });

    testWidgets('4. Card 11: Physical Activity — Conditional visibility & free text preservation', (tester) async {
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
      pageView.controller!.jumpToPage(11);
      await tester.pumpAndSettle();

      expect(find.text("Physical Activity 🏃"), findsOneWidget);

      final nextBtn = find.text("Next ➡️");

      // Tap Next with no selection -> requires sport level
      await tester.ensureVisible(nextBtn);
      await tester.tap(nextBtn);
      await tester.pumpAndSettle();
      expect(find.text("Please select your physical activity level"), findsOneWidget);

      // Select "Almost no physical activity"
      final noActOption = find.text("Almost no physical activity");
      await tester.ensureVisible(noActOption);
      await tester.tap(noActOption);
      await tester.pumpAndSettle();

      // Quantitative fields should NOT be visible
      expect(find.text("How many days per week are you usually active?"), findsNothing);
      expect(find.text("How long does your activity usually last on those days?"), findsNothing);
      expect(find.text("What activities do you do? (select all that apply)"), findsNothing);

      // Free text field IS visible even for Almost no physical activity
      expect(find.text("Tell us a little more about your activity (optional)"), findsOneWidget);
      final notesField = find.byType(TextField).first;
      await tester.ensureVisible(notesField);
      await tester.enterText(notesField, "Recovering from lumbar spine injury, temporary bed rest");
      await tester.pumpAndSettle();

      // Switch to Moderate activity -> quantitative fields appear
      final modActOption = find.text("Moderate — regular movement or exercise");
      await tester.ensureVisible(modActOption);
      await tester.tap(modActOption);
      await tester.pumpAndSettle();

      expect(find.text("How many days per week are you usually active?"), findsOneWidget);
      expect(find.text("How long does your activity usually last on those days?"), findsOneWidget);
      expect(find.text("What activities do you do? (select all that apply)"), findsOneWidget);

      // Select days and duration
      final daysChip = find.text("3–4 days");
      await tester.ensureVisible(daysChip);
      await tester.tap(daysChip);
      await tester.pumpAndSettle();

      final durChip = find.text("30–60 minutes");
      await tester.ensureVisible(durChip);
      await tester.tap(durChip);
      await tester.pumpAndSettle();

      // Select "Other" activity type
      final otherTypeChip = find.text("Other");
      await tester.ensureVisible(otherTypeChip);
      await tester.tap(otherTypeChip);
      await tester.pumpAndSettle();

      // Tap Next without specifying Other -> error
      await tester.ensureVisible(nextBtn);
      await tester.tap(nextBtn);
      await tester.pumpAndSettle();
      expect(find.text("Please specify your activity for «Other»"), findsOneWidget);

      // Enter Other activity detail
      final otherSpecField = find.widgetWithText(TextField, "e.g. Martial arts, climbing, Pilates");
      await tester.ensureVisible(otherSpecField);
      await tester.enterText(otherSpecField, "Rock climbing");
      await tester.pumpAndSettle();

      // Now switch BACK to "Almost no physical activity":
      // Quantitative options are cleared, but notes text remains intact!
      await tester.ensureVisible(noActOption);
      await tester.tap(noActOption);
      await tester.pumpAndSettle();

      // Quantitative options gone
      expect(find.text("How many days per week are you usually active?"), findsNothing);
      // Notes field still has the text
      expect(find.text("Recovering from lumbar spine injury, temporary bed rest"), findsOneWidget);

      // Tap Next -> successfully navigates to Card 12
      await tester.ensureVisible(nextBtn);
      await tester.tap(nextBtn);
      await tester.pumpAndSettle();

      expect(find.text("Chronic Conditions & Medical History"), findsOneWidget);

      final global = vault.getSystemContext('global');
      expect(global['sport_level'], 'Almost no physical activity');
      expect(global['activity_days_per_week'], isNull);
      expect(global['activity_duration_minutes'], isNull);
      expect(global['activity_types'], isNull);
      expect(global['activity_other_details'], isNull);
      expect(global['user_text_sport'], 'Recovering from lumbar spine injury, temporary bed rest');

      vault.dispose();
      authManager.dispose();
      apiClient.close();
    });
  });
}
