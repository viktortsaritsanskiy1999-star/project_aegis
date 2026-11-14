import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:provider/provider.dart';

import 'package:aegis_health/anamnesis_screen.dart';
import 'package:aegis_health/services/api/aegis_api_client.dart';
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

  group('PROJECT AEGIS — AUDIT REGRESSION TESTS (CARDS 1–16)', () {
    testWidgets('1. H-1 & H-2: Back navigation preserves age/height/weight and returns nicotine/alcohol sub-step 1 -> 0', (tester) async {
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

      // Card 0 -> Card 1
      await tester.tap(find.text("I'm 18+, let's go! 🚀"));
      await tester.pumpAndSettle();

      // Card 1: Male, 34
      await tester.tap(find.text("🧔 Male"));
      await tester.enterText(find.byType(TextField).first, '34');
      await tester.pumpAndSettle();
      await tester.ensureVisible(find.text("Next ➡️"));
      await tester.tap(find.text("Next ➡️"));
      await tester.pumpAndSettle();

      // Card 2: Height 182, Weight 79
      expect(find.text("Your Dimensions"), findsOneWidget);
      await tester.enterText(find.byType(TextField).at(0), '182');
      await tester.enterText(find.byType(TextField).at(1), '79');
      await tester.ensureVisible(find.text("Next ➡️"));
      await tester.tap(find.text("Next ➡️"));
      await tester.pumpAndSettle();

      // Card 3 (Nicotine): enter sub-step 1
      expect(find.text("Nicotine 🚬"), findsOneWidget);
      await tester.tap(find.text("3️⃣ Regularly"));
      await tester.pumpAndSettle();
      expect(find.text("Select products:"), findsOneWidget);

      // Back from Nicotine sub-step 1 -> stays on Nicotine sub-step 0!
      await tester.tap(find.byTooltip('Back'));
      await tester.pumpAndSettle();
      expect(find.text("Nicotine 🚬"), findsOneWidget);
      expect(find.text("3️⃣ Regularly"), findsOneWidget);
      expect(find.text("Select products:"), findsNothing);

      // Back from Nicotine sub-step 0 -> returns to Card 2 with height 182 & weight 79 intact!
      await tester.tap(find.byTooltip('Back'));
      await tester.pumpAndSettle();
      expect(find.text("Your Dimensions"), findsOneWidget);
      expect(find.widgetWithText(TextField, '182'), findsOneWidget);
      expect(find.widgetWithText(TextField, '79'), findsOneWidget);

      // Back from Card 2 -> returns to Card 1 with age 34 intact!
      await tester.tap(find.byTooltip('Back'));
      await tester.pumpAndSettle();
      expect(find.text("Biological Profile"), findsOneWidget);
      expect(find.widgetWithText(TextField, '34'), findsOneWidget);

      // Navigate forward again to Card 4 (Alcohol) and test sub-step Back
      await tester.ensureVisible(find.text("Next ➡️"));
      await tester.tap(find.text("Next ➡️"));
      await tester.pumpAndSettle();
      await tester.ensureVisible(find.text("Next ➡️"));
      await tester.tap(find.text("Next ➡️"));
      await tester.pumpAndSettle();
      await tester.tap(find.text("1️⃣ Never used"));
      await tester.pumpAndSettle();

      expect(find.text("Alcohol 🍷"), findsOneWidget);
      await tester.tap(find.text("4️⃣ 1–2 times a week"));
      await tester.pumpAndSettle();
      expect(find.text("What do you usually drink?"), findsOneWidget);

      await tester.tap(find.byTooltip('Back'));
      await tester.pumpAndSettle();
      expect(find.text("Alcohol 🍷"), findsOneWidget);
      expect(find.text("4️⃣ 1–2 times a week"), findsOneWidget);
      expect(find.text("What do you usually drink?"), findsNothing);

      vault.dispose();
      authManager.dispose();
      apiClient.close();
    });

    testWidgets('2. H-2: Nicotine Regular -> Former clears frequency & quantities and unchecking product removes its quantity; Alcohol saves exact frequency', (tester) async {
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
      pageView.controller!.jumpToPage(3);
      await tester.pumpAndSettle();

      // Choose Regularly -> select Cigarettes & Vape, quantities, frequency
      await tester.tap(find.text("3️⃣ Regularly"));
      await tester.pumpAndSettle();
      await tester.tap(find.text("Conventional cigarettes"));
      await tester.tap(find.text("E-cigarettes / vape"));
      await tester.pumpAndSettle();

      // Tenure for both
      await tester.ensureVisible(find.text("1–3 years").first);
      await tester.tap(find.text("1–3 years").first);
      await tester.ensureVisible(find.text("1–3 years").last);
      await tester.tap(find.text("1–3 years").last);
      await tester.pumpAndSettle();

      // Frequency + quantities
      await tester.ensureVisible(find.text("Daily"));
      await tester.tap(find.text("Daily"));
      await tester.ensureVisible(find.text("6–10").first);
      await tester.tap(find.text("6–10").first);
      await tester.ensureVisible(find.text("Salt nicotine"));
      await tester.tap(find.text("Salt nicotine"));
      await tester.pumpAndSettle();

      // Uncheck Cigarettes -> its quantity 'cigarettes_per_day' must be removed while vape remains!
      await tester.ensureVisible(find.text("Conventional cigarettes"));
      await tester.tap(find.text("Conventional cigarettes"));
      await tester.pumpAndSettle();

      await tester.ensureVisible(find.text("Next ➡️"));
      await tester.tap(find.text("Next ➡️"));
      await tester.pumpAndSettle();

      expect(find.text("Alcohol 🍷"), findsOneWidget);
      var global = vault.getSystemContext('global');
      expect(global['nicotine_status'], 'Regular');
      expect(global['nicotine_products'], ['E-cigarettes / vape']);
      expect(global['nicotine_quantities'], {'vape_nicotine_type': 'Salt nicotine'});

      // Go Back to Card 3 -> switch to Former smoker -> clears frequency & quantities cleanly!
      await tester.tap(find.byTooltip('Back'));
      await tester.pumpAndSettle();
      expect(find.text("3️⃣ Regularly"), findsOneWidget);

      await tester.tap(find.text("4️⃣ Former smoker / user"));
      await tester.pumpAndSettle();
      await tester.ensureVisible(find.text("6–12 months ago"));
      await tester.tap(find.text("6–12 months ago"));
      await tester.pumpAndSettle();

      await tester.ensureVisible(find.text("Next ➡️"));
      await tester.tap(find.text("Next ➡️"));
      await tester.pumpAndSettle();

      global = vault.getSystemContext('global');
      expect(global['nicotine_status'], 'Quit');
      expect(global['nicotine_quit_when'], '6–12 months ago');
      expect(global['nicotine_current_frequency'], isNull);
      expect(global['nicotine_quantities'], isNull);
      expect(global['nicotine_freq'], isNull);

      // Now on Card 4 (Alcohol): select '1-2/week', Wine, Next -> exact frequency saved!
      await tester.tap(find.text("4️⃣ 1–2 times a week"));
      await tester.pumpAndSettle();
      await tester.tap(find.text("Wine"));
      await tester.pumpAndSettle();
      await tester.ensureVisible(find.text("150 ml (glass)"));
      await tester.tap(find.text("150 ml (glass)"));
      await tester.pumpAndSettle();
      await tester.ensureVisible(find.text("Next ➡️"));
      await tester.tap(find.text("Next ➡️"));
      await tester.pumpAndSettle();

      global = vault.getSystemContext('global');
      expect(global['alcohol_status'], '1-2/week');
      expect(global['alcohol_types'], ['Wine']);
      expect(global['alcohol_volumes'], {'Wine': '150 ml (glass)'});

      vault.dispose();
      authManager.dispose();
      apiClient.close();
    });

    testWidgets('3. M-4 & M-5 & M-2: Card 5 requires substance_last_use; Card 7 blocks empty excluded foods; Card 8 clears stale caffeine frequency', (tester) async {
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

      // Card 5: select Cannabis without substance_last_use -> blocked!
      pageView.controller!.jumpToPage(5);
      await tester.pumpAndSettle();
      await tester.tap(find.text("Cannabis / Marijuana"));
      await tester.pumpAndSettle();
      await tester.ensureVisible(find.text("Next ➡️"));
      await tester.tap(find.text("Next ➡️"));
      await tester.pumpAndSettle();
      expect(find.text("Please specify when the last use occurred"), findsOneWidget);

      await tester.ensureVisible(find.text("More than 1 month ago"));
      await tester.tap(find.text("More than 1 month ago"));
      await tester.pumpAndSettle();
      await tester.ensureVisible(find.text("Next ➡️"));
      await tester.tap(find.text("Next ➡️"));
      await tester.pumpAndSettle();
      expect(find.text("Sleep 🌙"), findsOneWidget);
      expect(vault.getSystemContext('global')['substance_last_use'], 'More than 1 month ago');

      // Card 7: Excluded foods empty validation
      pageView.controller!.jumpToPage(7);
      await tester.pumpAndSettle();
      final excludedTile = find.text("I avoid specific foods or ingredients (allergies, intolerances)");
      await tester.ensureVisible(excludedTile);
      await tester.tap(excludedTile);
      await tester.pumpAndSettle();
      await tester.ensureVisible(find.text("Save and collapse"));
      await tester.tap(find.text("Save and collapse"));
      await tester.pumpAndSettle();
      expect(find.text("Please list the foods you avoid or uncheck the option"), findsOneWidget);

      // Card 8: Select Black / Green tea + caffeine freq, then uncheck -> clears drinks_caffeine_frequency!
      pageView.controller!.jumpToPage(8);
      await tester.pumpAndSettle();
      await tester.tap(find.text("1.5–2 liters"));
      await tester.tap(find.text("Still water"));
      await tester.tap(find.text("Black / Green tea"));
      await tester.pumpAndSettle();
      await tester.ensureVisible(find.text("1–2 servings a day"));
      await tester.tap(find.text("1–2 servings a day"));
      await tester.pumpAndSettle();

      // Now uncheck Black / Green tea
      await tester.ensureVisible(find.text("Black / Green tea"));
      await tester.tap(find.text("Black / Green tea"));
      await tester.pumpAndSettle();

      await tester.ensureVisible(find.text("Next ➡️"));
      await tester.tap(find.text("Next ➡️"));
      await tester.pumpAndSettle();

      expect(vault.getSystemContext('global')['drinks_caffeine_frequency'], isNull);

      vault.dispose();
      authManager.dispose();
      apiClient.close();
    });

    testWidgets('4. M-6: Matryoshka headers on Cards 7, 9, 12, 13 collapse/reopen safely without deleting details & clamp summaries', (tester) async {
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

      // Card 7: Occasional Foods — select Sweets, pick frequency, tap title again to re-expand without losing frequency!
      pageView.controller!.jumpToPage(7);
      await tester.pumpAndSettle();
      final sweetsTitle = find.text("Sweets and desserts");
      await tester.ensureVisible(sweetsTitle);
      await tester.tap(sweetsTitle);
      await tester.pumpAndSettle();
      await tester.ensureVisible(find.text("1–2 times a week"));
      await tester.tap(find.text("1–2 times a week"));
      await tester.pumpAndSettle();
      expect(find.text("Frequency: 1–2 times a week"), findsOneWidget);
      await tester.ensureVisible(sweetsTitle);
      await tester.tap(sweetsTitle);
      await tester.pumpAndSettle();
      expect(find.text("How often do you eat this?"), findsOneWidget);
      // Collapse again by re-selecting chip -> frequency still intact
      await tester.ensureVisible(find.text("1–2 times a week"));
      await tester.tap(find.text("1–2 times a week"));
      await tester.pumpAndSettle();
      expect(find.text("Frequency: 1–2 times a week"), findsOneWidget);

      // Card 9: Select Heartburn, fill frequency & duration, Save and collapse, then tap title again -> still intact!
      pageView.controller!.jumpToPage(9);
      await tester.pumpAndSettle();
      await tester.tap(find.text("Heartburn / Acid taste"));
      await tester.pumpAndSettle();
      await tester.tap(find.text("1–3 times a week"));
      await tester.tap(find.text("For several months"));
      await tester.tap(find.text("Save and collapse"));
      await tester.pumpAndSettle();
      expect(find.text("Details: 1–3 times a week, For several months"), findsOneWidget);

      // Tap title header again to reopen -> previously typed/selected choices remain!
      await tester.tap(find.text("Heartburn / Acid taste"));
      await tester.pumpAndSettle();
      expect(find.text("Save and collapse"), findsOneWidget);
      await tester.tap(find.text("Save and collapse"));
      await tester.pumpAndSettle();
      expect(find.text("Details: 1–3 times a week, For several months"), findsOneWidget);

      // Card 12: Chronic Conditions — enter diagnosis, collapse, tap title header again -> diagnosis preserved!
      pageView.controller!.jumpToPage(12);
      await tester.pumpAndSettle();
      final diabTitle = find.text("Diabetes or blood sugar disorders");
      await tester.ensureVisible(diabTitle);
      await tester.tap(diabTitle);
      await tester.pumpAndSettle();

      final diagField = find.byWidgetPredicate(
        (w) => w is TextField && w.decoration?.hintText == 'e.g. Type 1 diabetes, type 2 diabetes, prediabetes',
      );
      await tester.enterText(diagField, "Type 2 diabetes");
      await tester.ensureVisible(find.text("Controlled or stable"));
      await tester.tap(find.text("Controlled or stable"));
      await tester.ensureVisible(find.text("No, it doesn't currently bother me"));
      await tester.tap(find.text("No, it doesn't currently bother me"));
      await tester.ensureVisible(find.text("Save and collapse"));
      await tester.tap(find.text("Save and collapse"));
      await tester.pumpAndSettle();
      expect(find.text("Type 2 diabetes • Controlled or stable"), findsOneWidget);

      // Re-tap title -> opens editor with "Type 2 diabetes" still inside, does NOT delete!
      await tester.ensureVisible(diabTitle);
      await tester.tap(diabTitle);
      await tester.pumpAndSettle();
      expect(find.widgetWithText(TextField, "Type 2 diabetes"), findsOneWidget);
      // Only Checkbox unselects it!
      final diabCheckbox = find.byKey(const ValueKey('chronic_checkbox_Diabetes or blood sugar disorders'));
      await tester.ensureVisible(diabCheckbox);
      await tester.tap(diabCheckbox);
      await tester.pumpAndSettle();
      expect(find.widgetWithText(TextField, "Type 2 diabetes"), findsNothing);

      // Card 13: Recent Events — fill details, collapse, re-tap title -> details preserved!
      pageView.controller!.jumpToPage(13);
      await tester.pumpAndSettle();
      final infTitle = find.text("Cold, flu, COVID-19 or another respiratory viral infection");
      await tester.ensureVisible(infTitle);
      await tester.tap(infTitle);
      await tester.pumpAndSettle();
      await tester.ensureVisible(find.text("Within the last 7 days"));
      await tester.tap(find.text("Within the last 7 days"));
      await tester.ensureVisible(find.text("Fully recovered"));
      await tester.tap(find.text("Fully recovered"));
      final recDetailField = find.byWidgetPredicate(
        (w) => w is TextField && w.decoration?.hintText == 'e.g. Fever for 3 days, treated at home',
      );
      await tester.ensureVisible(recDetailField);
      await tester.enterText(recDetailField, "Mild seasonal flu");
      await tester.ensureVisible(find.text("Save and collapse"));
      await tester.tap(find.text("Save and collapse"));
      await tester.pumpAndSettle();

      await tester.ensureVisible(infTitle);
      await tester.tap(infTitle);
      await tester.pumpAndSettle();
      expect(find.widgetWithText(TextField, "Mild seasonal flu"), findsOneWidget);

      vault.dispose();
      authManager.dispose();
      apiClient.close();
    });

    testWidgets('5. M-7 & M-8: Guest draft restore, async post-switch restore, user_text priority, and stale derivation cleanup', (tester) async {
      final mockClient = MockClient((request) async => http.Response('{}', 200));
      final apiClient = AegisApiClient(client: mockClient);
      final authManager = AuthSessionManager(apiClient: apiClient, storage: memoryAuthStorage);

      // Seed guest draft directly into storage before creating VaultService
      await memoryVaultStorage.saveGuestDraft({
        'global': {
          'age_confirmation': 'Confirmed',
          'gender': 'Female',
          'age': '31',
          'pregnancy_status': 'possible',
          'alcohol_status': '1-2/week',
          'alcohol_types': ['Wine'],
          'user_text_spec_factors': 'Spec primary text',
          'text_spec_factors': 'Spec fallback text',
          'user_text_diet_habits': 'Diet primary text',
          'text_diet_habits': 'Diet fallback text',
          'user_text_gi_signals': 'GI primary text',
          'text_gi_signals': 'GI fallback text',
          // Complete remaining required keys for buildAnamnesisAnswers test
          'height': '168',
          'weight': '60',
          'nicotine_status': 'Non-user',
          'spec_factors': 'None',
          'sleep_quality': 'Normal',
          'diet_types': 'Balanced',
          'diet_habits': 'Regular',
          'gi_signals': 'None',
          'work_type': 'Sedentary',
          'chronic_diseases': 'None',
          'recent_illness': 'None',
          'critical_infections': 'None',
          'heredity_risks': 'None',
          'medications': 'None',
        },
      });

      final vault = VaultService(storage: memoryVaultStorage, authSessionManager: authManager);
      await vault.initFuture;

      // M-8 user_text_* priority check
      final dto = vault.buildAnamnesisAnswers();
      expect(dto, isNotNull);
      expect(dto!.userTextSpecFactors, 'Spec primary text');
      expect(dto.userTextDietHabits, 'Diet primary text');
      expect(dto.userTextGiSignals, 'GI primary text');

      // M-8 derivation cleanup: save positive chronic & alcohol, then negate/remove -> derived flags cleared!
      vault.saveAnamnesis('chronic_diseases', 'Артериальная гипертензия');
      expect(vault.getSystemContext('cvs')['chronic_cvs_pathology'], isNotNull);
      vault.saveAnamnesis('chronic_diseases', 'No diagnosed chronic conditions');
      expect(vault.getSystemContext('cvs')['chronic_cvs_pathology'], isNull);

      vault.saveAnamnesis('recent_illness', 'COVID-19');
      expect(vault.getSystemContext('immune')['postviral_exhaustion_formula_shift'], isNotNull);
      vault.removeAnamnesisKeys(['recent_illness']);
      expect(vault.getSystemContext('immune')['postviral_exhaustion_formula_shift'], isNull);

      vault.saveAnamnesis('alcohol_status', '1-2/week');
      vault.saveAnamnesis('alcohol_types', ['Beer']);
      expect(vault.getSystemContext('kidneys')['uric_acid_risk'], true);
      vault.saveAnamnesis('alcohol_status', 'Non-drinker');
      vault.removeAnamnesisKeys(['alcohol_types', 'alcohol_volumes']);
      expect(vault.getSystemContext('kidneys')['uric_acid_risk'], isNull);
      expect(vault.getSystemContext('gi_tract')['liver_tox_load'], isNull);

      // M-7 Guest draft restore when activePatientId == null
      await tester.pumpWidget(_buildTestApp(vault: vault, authManager: authManager, apiClient: apiClient));
      await tester.pumpAndSettle();

      final pageView = tester.widget<PageView>(find.byType(PageView));
      pageView.controller!.jumpToPage(1);
      await tester.pumpAndSettle();

      // Guest age '31' is restored even though activePatientId == null!
      expect(find.widgetWithText(TextField, '31'), findsOneWidget);

      // Now seed patient_B in storage with age '48', switch patient while mounted, let async load complete!
      await memoryVaultStorage.savePatientVault(
        VaultStorageService.derivePatientStorageKey('patient_B'),
        {
          'system_folders': {
            'global': {
              'gender': 'Male',
              'age': '48',
            },
          },
        },
      );

      vault.setActivePatientId('patient_B');
      await tester.pumpAndSettle();

      pageView.controller!.jumpToPage(1);
      await tester.pumpAndSettle();

      expect(find.widgetWithText(TextField, '48'), findsOneWidget);
      expect(find.widgetWithText(TextField, '31'), findsNothing);

      vault.dispose();
      authManager.dispose();
      apiClient.close();
    });
  });
}
