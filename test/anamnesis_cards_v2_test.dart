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

  group('PROJECT AEGIS — ANAMNESIS CARDS V2: STAGE 1 TESTS', () {
    testWidgets('1. Card 1: Biological Profile — Age confirmation prompt (100–120) & Male bypass pregnancy', (tester) async {
      final mockClient = MockClient((request) async => http.Response('{}', 200));
      final apiClient = AegisApiClient(client: mockClient);
      final authManager = AuthSessionManager(apiClient: apiClient, storage: memoryAuthStorage);
      final vault = VaultService(storage: memoryVaultStorage, authSessionManager: authManager);
      await vault.initFuture;

      await tester.pumpWidget(_buildTestApp(vault: vault, authManager: authManager, apiClient: apiClient));
      await tester.pumpAndSettle();

      // Card 0: Agree
      await tester.tap(find.text("I'm 18+, let's go! 🚀"));
      await tester.pumpAndSettle();

      // Card 1: Select Male, enter 105 (unusual age)
      expect(find.text("Biological Profile"), findsOneWidget);
      await tester.tap(find.text("🧔 Male"));
      await tester.enterText(find.byType(TextField).first, '105');
      await tester.pumpAndSettle();

      // Tap Next without confirming unusual age -> rejected with prompt
      await tester.tap(find.text("Next"));
      await tester.pumpAndSettle();

      expect(find.textContaining("Вы указали возраст: 105"), findsWidgets);
      expect(find.text("Подтвердить правильность ввода"), findsOneWidget);

      // Tap confirm button
      await tester.tap(find.text("Подтвердить правильность ввода"));
      await tester.pumpAndSettle();

      // Now tap Next -> succeeds and moves to Card 2
      await tester.tap(find.text("Next"));
      await tester.pumpAndSettle();

      expect(find.text("Your Dimensions"), findsOneWidget);
      expect(vault.getSystemContext('global')['gender'], 'Male');
      expect(vault.getSystemContext('global')['age'], '105');
      expect(vault.getSystemContext('global')['age_confirmed_unusual'], true);
      // Pregnancy fields are strictly null / cleared for male
      expect(vault.getSystemContext('global')['pregnancy_status'], isNull);

      vault.dispose();
      authManager.dispose();
      apiClient.close();
    });

    testWidgets('2. Card 1: Biological Profile — Female pregnancy trimester & weeks consistency', (tester) async {
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

      // Card 1: Female, 28 years
      await tester.tap(find.text("👩 Female"));
      await tester.enterText(find.byType(TextField).first, '28');
      await tester.pumpAndSettle();

      // Female reveals pregnancy question
      expect(find.text("Беременны ли вы сейчас?"), findsOneWidget);
      expect(find.text("Кормите ли вы грудью?"), findsOneWidget);

      // Select "Да"
      await tester.ensureVisible(find.text("Да").first);
      await tester.tap(find.text("Да").first);
      await tester.pumpAndSettle();

      // Select 1st trimester, but enter week 22 (conflict!)
      await tester.ensureVisible(find.text("1-й триместр (до 13 нед. 6 дн.)"));
      await tester.tap(find.text("1-й триместр (до 13 нед. 6 дн.)"));
      final weeksField = find.widgetWithText(TextField, "Срок беременности (в неделях, 1–45, необязательно)");
      await tester.ensureVisible(weeksField);
      await tester.enterText(weeksField, '22');

      // Select breastfeeding "Нет"
      await tester.ensureVisible(find.text("Нет").last);
      await tester.tap(find.text("Нет").last);
      await tester.pumpAndSettle();

      // Tap Next -> blocked due to inconsistency
      await tester.ensureVisible(find.text("Next"));
      await tester.tap(find.text("Next"));
      await tester.pumpAndSettle();

      expect(find.textContaining("не соответствует 1-му триместру"), findsOneWidget);

      // Correct weeks to 10
      await tester.enterText(weeksField, '10');
      await tester.pumpAndSettle();

      await tester.ensureVisible(find.text("Next"));
      await tester.tap(find.text("Next"));
      await tester.pumpAndSettle();

      expect(find.text("Your Dimensions"), findsOneWidget);
      expect(vault.getSystemContext('global')['pregnancy_status'], 'Да');
      expect(vault.getSystemContext('global')['pregnancy_trimester'], '1st trimester');
      expect(vault.getSystemContext('global')['pregnancy_weeks'], 10);
      expect(vault.getSystemContext('global')['breastfeeding_status'], 'No');

      vault.dispose();
      authManager.dispose();
      apiClient.close();
    });

    testWidgets('3. Card 2: Dimensions & Weight Dynamics — Loss cannot exceed current weight', (tester) async {
      tester.view.physicalSize = const Size(1080, 2400);
      tester.view.devicePixelRatio = 1.0;
      addTearDown(() => tester.view.resetPhysicalSize());

      final mockClient = MockClient((request) async => http.Response('{}', 200));
      final apiClient = AegisApiClient(client: mockClient);
      final authManager = AuthSessionManager(apiClient: apiClient, storage: memoryAuthStorage);
      final vault = VaultService(storage: memoryVaultStorage, authSessionManager: authManager);
      await vault.initFuture;

      vault.saveAnamnesis('age_confirmation', 'Confirmed');
      vault.saveAnamnesis('gender', 'Male');
      vault.saveAnamnesis('age', '30');

      await tester.pumpWidget(_buildTestApp(vault: vault, authManager: authManager, apiClient: apiClient));
      await tester.pumpAndSettle();

      final pageView = tester.widget<PageView>(find.byType(PageView));
      pageView.controller!.jumpToPage(2);
      await tester.pumpAndSettle();

      expect(find.text("Your Dimensions"), findsOneWidget);

      // Enter height 175, weight 70
      final fields = find.byType(TextField);
      await tester.enterText(fields.at(0), '175');
      await tester.enterText(fields.at(1), '70');

      // Select "Потерял(а) вес"
      await tester.ensureVisible(find.text("Потерял(а) вес"));
      await tester.tap(find.text("Потерял(а) вес"));
      await tester.pumpAndSettle();

      // Enter loss amount 75 (> 70 current weight!)
      final amtField = find.widgetWithText(TextField, "На сколько килограммов? (например, 5)");
      await tester.ensureVisible(amtField);
      await tester.enterText(amtField, '75');
      await tester.ensureVisible(find.text("Да").last);
      await tester.tap(find.text("Да").last); // Intentional
      await tester.pumpAndSettle();

      await tester.ensureVisible(find.text("Next"));
      await tester.tap(find.text("Next"));
      await tester.pumpAndSettle();

      expect(find.textContaining("не может превышать текущий вес"), findsOneWidget);

      // Correct loss to 5 kg
      await tester.enterText(amtField, '5');
      await tester.pumpAndSettle();

      await tester.ensureVisible(find.text("Next"));
      await tester.tap(find.text("Next"));
      await tester.pumpAndSettle();

      expect(find.text("Nicotine 🚬"), findsOneWidget);
      expect(vault.getSystemContext('global')['weight_change_status'], 'Lost');
      expect(vault.getSystemContext('global')['weight_change_amount'], 5);

      vault.dispose();
      authManager.dispose();
      apiClient.close();
    });

    testWidgets('4. Card 3: Nicotine — Never clears previous products and tenure in VaultService', (tester) async {
      final mockClient = MockClient((request) async => http.Response('{}', 200));
      final apiClient = AegisApiClient(client: mockClient);
      final authManager = AuthSessionManager(apiClient: apiClient, storage: memoryAuthStorage);
      final vault = VaultService(storage: memoryVaultStorage, authSessionManager: authManager);
      await vault.initFuture;

      // Pre-populate old nicotine data
      vault.saveAnamnesis('nicotine_status', 'Active');
      vault.saveAnamnesis('nicotine_products', ['cigarettes']);
      vault.saveAnamnesis('nicotine_product_tenure', {'cigarettes': '3-5 years'});

      await tester.pumpWidget(_buildTestApp(vault: vault, authManager: authManager, apiClient: apiClient));
      await tester.pumpAndSettle();

      final pageView = tester.widget<PageView>(find.byType(PageView));
      pageView.controller!.jumpToPage(3);
      await tester.pumpAndSettle();

      expect(find.text("Nicotine 🚬"), findsOneWidget);

      // User selects "1️⃣ Никогда не употреблял(а)"
      await tester.tap(find.text("1️⃣ Никогда не употреблял(а)"));
      await tester.pumpAndSettle();

      expect(vault.getSystemContext('global')['nicotine_status'], 'Non-user');
      expect(vault.getSystemContext('global')['nicotine_products'], isNull);
      expect(vault.getSystemContext('global')['nicotine_product_tenure'], isNull);

      vault.dispose();
      authManager.dispose();
      apiClient.close();
    });

    testWidgets('5. Card 5: Additional Health Factors — Mutual exclusion for "Не употребляю"', (tester) async {
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
      pageView.controller!.jumpToPage(5);
      await tester.pumpAndSettle();

      expect(find.text("Дополнительные факторы здоровья 🛡️"), findsOneWidget);

      // Select Cannabis
      await tester.ensureVisible(find.text("Каннабис / марихуана"));
      await tester.tap(find.text("Каннабис / марихуана"));
      await tester.pumpAndSettle();
      expect(find.text("Когда это было в последний раз?"), findsOneWidget);

      // Now select "🚫 Не употребляю" -> clears Cannabis and hides subquestion
      await tester.ensureVisible(find.text("🚫 Не употребляю"));
      await tester.tap(find.text("🚫 Не употребляю"));
      await tester.pumpAndSettle();

      expect(find.text("Когда это было в последний раз?"), findsNothing);

      await tester.ensureVisible(find.text("Continue ➡️"));
      await tester.tap(find.text("Continue ➡️"));
      await tester.pumpAndSettle();

      expect(find.text("Sleep 🌙"), findsOneWidget);
      expect(vault.getSystemContext('global')['substance_types'], ['Не употребляю']);
      expect(vault.getSystemContext('global')['substance_last_use'], isNull);

      vault.dispose();
      authManager.dispose();
      apiClient.close();
    });

    testWidgets('6. DTO Roundtrip: AnamnesisQuestionnaireAnswers Stage 1 serialization', (tester) async {
      final answers = AnamnesisQuestionnaireAnswers(
        ageConfirmation: 'Confirmed',
        gender: 'Female',
        age: 29,
        height: 168.5,
        weight: 62.4,
        ageConfirmedUnusual: false,
        pregnancyStatus: 'Да',
        pregnancyTrimester: '2nd trimester',
        pregnancyWeeks: 16,
        breastfeedingStatus: 'No',
        heightConfirmedUnusual: false,
        weightConfirmedUnusual: false,
        weightChangeStatus: 'Gained',
        weightChangeAmount: 2.5,
        weightChangeIntentional: 'Yes',
        nicotineStatus: 'Active',
        nicotineProducts: const ['Обычные сигареты', 'Вейп / электронные сигареты'],
        nicotineProductTenure: const {'Обычные сигареты': '3–5 лет', 'Вейп / электронные сигареты': '1–3 года'},
        nicotineCurrentFrequency: '1–3 дня в неделю',
        alcoholStatus: 'Frequently',
        alcoholTypes: const ['Вино', 'Пиво'],
        alcoholVolumes: const {'Вино': '150 мл (бокал)', 'Пиво': '0.5 л'},
        alcoholComment: 'По праздникам',
        substanceTypes: const ['Каннабис / марихуана'],
        substanceLastUse: '<1 month',
        specFactors: 'Каннабис / марихуана',
        sleepQuality: 'Great',
        dietTypes: 'Balanced',
        dietHabits: 'Regular',
        giSignals: 'None',
        workType: 'Sedentary',
        chronicDiseases: 'None',
        recentIllness: 'None',
        criticalInfections: 'None',
        heredityRisks: 'None',
        medications: 'None',
      );

      final jsonMap = answers.toJson();
      expect(jsonMap['height'], 168.5);
      expect(jsonMap['weight'], 62.4);
      expect(jsonMap['pregnancy_weeks'], 16);
      expect(jsonMap['nicotine_products'], contains('Обычные сигареты'));
      expect(jsonMap['alcohol_types'], contains('Вино'));

      final reconstructed = AnamnesisQuestionnaireAnswers.fromMap(jsonMap);
      expect(reconstructed.height, 168.5);
      expect(reconstructed.weight, 62.4);
      expect(reconstructed.pregnancyTrimester, '2nd trimester');
      expect(reconstructed.nicotineProductTenure?['Обычные сигареты'], '3–5 лет');
    });
  });
}
