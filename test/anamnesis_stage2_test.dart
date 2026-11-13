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

  group('PROJECT AEGIS — ANAMNESIS CARDS V2: STAGE 2 TESTS', () {
    testWidgets('1. Card 6: Sleep & Recovery — Options, mutual exclusion & save', (tester) async {
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
      pageView.controller!.jumpToPage(6);
      await tester.pumpAndSettle();

      expect(find.text("Sleep 🌙"), findsOneWidget);
      expect(find.text("Average sleep duration per night:"), findsOneWidget);

      // Tap Next without filling -> shows error
      final nextBtn = find.text("Next ➡️");
      await tester.ensureVisible(nextBtn);
      await tester.tap(nextBtn);
      await tester.pumpAndSettle();
      expect(find.text("Please select your average sleep duration"), findsOneWidget);

      // Select duration
      final durChip = find.text("7–8 hours");
      await tester.ensureVisible(durChip);
      await tester.tap(durChip);
      await tester.pumpAndSettle();

      // Select quality
      final qualBtn = find.text("Great");
      await tester.ensureVisible(qualBtn);
      await tester.tap(qualBtn);
      await tester.pumpAndSettle();

      // Select symptom
      final symDiff = find.text("Difficulty falling asleep");
      await tester.ensureVisible(symDiff);
      await tester.tap(symDiff);
      await tester.pumpAndSettle();

      // Select None of the above -> clears Difficulty falling asleep
      final symNone = find.text("None of the above");
      await tester.ensureVisible(symNone);
      await tester.tap(symNone);
      await tester.pumpAndSettle();

      // Select schedule
      final schReg = find.text("Regular schedule");
      await tester.ensureVisible(schReg);
      await tester.tap(schReg);
      await tester.pumpAndSettle();

      // Tap Next -> moves to Diet (page 7)
      await tester.ensureVisible(nextBtn);
      await tester.tap(nextBtn);
      await tester.pumpAndSettle();

      expect(find.text("Your Diet 🍽️"), findsOneWidget);

      final global = vault.getSystemContext('global');
      expect(global['sleep_duration'], '7–8 hours');
      expect(global['sleep_quality'], 'Great');
      expect(global['sleep_symptoms'], ['None of the above']);
      expect(global['sleep_schedule'], 'Regular schedule');

      vault.dispose();
      authManager.dispose();
      apiClient.close();
    });

    testWidgets('2. Card 7: Diet & Habits — Contradiction blocking & Matryoshka', (tester) async {
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
      pageView.controller!.jumpToPage(7);
      await tester.pumpAndSettle();

      expect(find.text("Your Diet 🍽️"), findsOneWidget);

      // Select Vegan
      final veganBtn = find.text("Vegan");
      await tester.ensureVisible(veganBtn);
      await tester.tap(veganBtn);
      await tester.pumpAndSettle();

      // Try selecting Meat and poultry -> blocked by contradiction
      final meatBtn = find.text("Meat and poultry");
      await tester.ensureVisible(meatBtn);
      await tester.tap(meatBtn);
      await tester.pumpAndSettle();
      expect(find.text("Vegan diet is incompatible with animal products (Meat and poultry)"), findsOneWidget);

      // Select valid staples: Vegetables and greens, Grains and cereals
      final vegBtn = find.text("Vegetables and greens");
      await tester.ensureVisible(vegBtn);
      await tester.tap(vegBtn);
      await tester.pumpAndSettle();

      final grainBtn = find.text("Grains and cereals");
      await tester.ensureVisible(grainBtn);
      await tester.tap(grainBtn);
      await tester.pumpAndSettle();

      // Meal and snack frequencies
      final mealChip = find.text("3 meals");
      await tester.ensureVisible(mealChip);
      await tester.tap(mealChip);
      await tester.pumpAndSettle();

      final snackChip = find.text("1–2 snacks");
      await tester.ensureVisible(snackChip);
      await tester.tap(snackChip);
      await tester.pumpAndSettle();

      // Excluded foods matryoshka
      final excludedTile = find.text("I avoid specific foods or ingredients (allergies, intolerances)");
      await tester.ensureVisible(excludedTile);
      await tester.tap(excludedTile);
      await tester.pumpAndSettle();

      final excludedField = find.byType(TextField).first;
      await tester.ensureVisible(excludedField);
      await tester.enterText(excludedField, "Gluten, Peanuts");
      await tester.pumpAndSettle();

      final collapseBtn = find.text("Save and collapse");
      await tester.ensureVisible(collapseBtn);
      await tester.tap(collapseBtn);
      await tester.pumpAndSettle();

      expect(find.text("Excluded: Gluten, Peanuts"), findsOneWidget);
      expect(find.text("Edit / Expand"), findsOneWidget);

      // Occasional foods matryoshka
      final sweetsBtn = find.text("Sweets and desserts");
      await tester.ensureVisible(sweetsBtn);
      await tester.tap(sweetsBtn);
      await tester.pumpAndSettle();

      expect(find.text("How often do you eat this?"), findsOneWidget);

      // Select frequency for Sweets -> auto collapses
      final freqChip = find.text("1–2 times a week");
      await tester.ensureVisible(freqChip);
      await tester.tap(freqChip);
      await tester.pumpAndSettle();

      expect(find.text("Frequency: 1–2 times a week"), findsOneWidget);
      expect(find.text("[Change ✏️]"), findsOneWidget);

      // Tap Next -> moves to Drinks (page 8)
      final nextBtn = find.text("Next ➡️");
      await tester.ensureVisible(nextBtn);
      await tester.tap(nextBtn);
      await tester.pumpAndSettle();

      expect(find.text("Drinks & Hydration 💧"), findsOneWidget);

      final global = vault.getSystemContext('global');
      expect(global['diet_types'], 'Vegan');
      expect(global['diet_staples'], contains('Vegetables and greens'));
      expect(global['diet_staples'], contains('Grains and cereals'));
      expect(global['diet_meals_per_day'], '3 meals');
      expect(global['diet_snacks_per_day'], '1–2 snacks');
      expect(global['diet_excluded_foods_text'], 'Gluten, Peanuts');
      expect(global['diet_occasional_foods'], ['Sweets and desserts']);
      expect(global['diet_food_frequencies'], {'Sweets and desserts': '1–2 times a week'});
      expect(global['diet_habits'], isNotNull);

      vault.dispose();
      authManager.dispose();
      apiClient.close();
    });

    testWidgets('3. Card 8: Drinks & Hydration — Nested Coffee Matryoshka & Decaf independence', (tester) async {
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
      pageView.controller!.jumpToPage(8);
      await tester.pumpAndSettle();

      expect(find.text("Drinks & Hydration 💧"), findsOneWidget);

      // Select volume
      final volChip = find.text("1.5–2 liters");
      await tester.ensureVisible(volChip);
      await tester.tap(volChip);
      await tester.pumpAndSettle();

      // Select Still water
      final waterBtn = find.text("Still water");
      await tester.ensureVisible(waterBtn);
      await tester.tap(waterBtn);
      await tester.pumpAndSettle();

      // Select Coffee -> expands coffee section
      final coffeeBtn = find.text("Coffee and coffee beverages");
      await tester.ensureVisible(coffeeBtn);
      await tester.tap(coffeeBtn);
      await tester.pumpAndSettle();

      expect(find.text("Coffee details ☕"), findsOneWidget);

      // Select coffee type
      final espressoChip = find.text("Espresso / Black coffee");
      await tester.ensureVisible(espressoChip);
      await tester.tap(espressoChip);
      await tester.pumpAndSettle();

      // Select coffee frequency
      final coffeeFreqChip = find.text("2–3 cups a day");
      await tester.ensureVisible(coffeeFreqChip);
      await tester.tap(coffeeFreqChip);
      await tester.pumpAndSettle();

      // Save and collapse coffee
      final collapseCoffeeBtn = find.text("Save and collapse coffee details");
      await tester.ensureVisible(collapseCoffeeBtn);
      await tester.tap(collapseCoffeeBtn);
      await tester.pumpAndSettle();

      expect(find.text("Coffee: Espresso / Black coffee (2–3 cups a day)"), findsOneWidget);
      expect(find.text("Edit coffee details"), findsOneWidget);

      // Select Decaf coffee independently
      final decafBtn = find.text("Decaf coffee");
      await tester.ensureVisible(decafBtn);
      await tester.tap(decafBtn);
      await tester.pumpAndSettle();

      // Tap Next -> moves to GI signals (page 9)
      final nextBtn = find.text("Next ➡️");
      await tester.ensureVisible(nextBtn);
      await tester.tap(nextBtn);
      await tester.pumpAndSettle();

      expect(find.text("Digestion & Gastrointestinal Health 🎈"), findsOneWidget);

      final global = vault.getSystemContext('global');
      expect(global['drinks_total_volume'], '1.5–2 liters');
      expect(global['drinks_types'], contains('Still water'));
      expect(global['drinks_types'], contains('Coffee and coffee beverages'));
      expect(global['drinks_types'], contains('Decaf coffee'));
      expect(global['drinks_coffee_types'], ['Espresso / Black coffee']);
      expect(global['drinks_coffee_frequency'], '2–3 cups a day');

      vault.dispose();
      authManager.dispose();
      apiClient.close();
    });

    test('4. DTO Roundtrip: AnamnesisQuestionnaireAnswers Stage 2 serialization', () {
      final answers = AnamnesisQuestionnaireAnswers(
        ageConfirmation: 'Confirmed',
        gender: 'Male',
        age: 35,
        height: 182,
        weight: 78.5,
        nicotineStatus: 'Non-user',
        alcoholStatus: 'Non-drinker',
        specFactors: 'None',
        sleepQuality: 'Great',
        dietTypes: 'Mixed / Balanced diet',
        dietHabits: '3 meals, 1 snack/day',
        giSignals: 'None',
        workType: 'Sedentary',
        chronicDiseases: 'None',
        recentIllness: 'None',
        criticalInfections: 'None',
        heredityRisks: 'None',
        medications: 'None',
        sleepDuration: '7–8 hours',
        sleepSymptoms: ['None of the above'],
        sleepSchedule: 'Regular schedule',
        sleepScheduleNotes: 'Sleep well with open window',
        dietMealsPerDay: '3 meals',
        dietSnacksPerDay: '1–2 snacks',
        dietStaples: ['Meat and poultry', 'Vegetables and greens'],
        dietOccasionalFoods: ['Sweets and desserts'],
        dietFoodFrequencies: {'Sweets and desserts': '1–2 times a week'},
        dietExcludedFoodsText: 'None',
        drinksTotalVolume: '1.5–2 liters',
        drinksTypes: ['Still water', 'Coffee and coffee beverages'],
        drinksCoffeeTypes: ['Espresso / Black coffee'],
        drinksCoffeeFrequency: '2–3 cups a day',
      );

      final map = answers.toJson();
      expect(map['sleep_duration'], '7–8 hours');
      expect(map['sleep_symptoms'], ['None of the above']);
      expect(map['diet_meals_per_day'], '3 meals');
      expect(map['diet_staples'], ['Meat and poultry', 'Vegetables and greens']);
      expect(map['drinks_coffee_frequency'], '2–3 cups a day');

      final reconstructed = AnamnesisQuestionnaireAnswers.fromMap(map);
      expect(reconstructed.sleepDuration, '7–8 hours');
      expect(reconstructed.sleepSymptoms, ['None of the above']);
      expect(reconstructed.dietStaples, ['Meat and poultry', 'Vegetables and greens']);
      expect(reconstructed.drinksCoffeeFrequency, '2–3 cups a day');
      expect(reconstructed.drinksCoffeeTypes, ['Espresso / Black coffee']);
    });
  });
}
