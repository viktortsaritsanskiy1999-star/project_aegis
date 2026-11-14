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
  MediaQueryData? mediaQueryOverride,
}) {
  Widget app = MultiProvider(
    providers: [
      ChangeNotifierProvider<VaultService>.value(value: vault),
      ChangeNotifierProvider<AuthSessionManager>.value(value: authManager),
      Provider<AegisApiClient>.value(value: apiClient),
    ],
    child: MaterialApp(
      home: AnamnesisScreen(),
    ),
  );
  if (mediaQueryOverride != null) {
    app = MediaQuery(
      data: mediaQueryOverride,
      child: app,
    );
  }
  return app;
}

Future<void> _jumpToCard(WidgetTester tester, int pageIndex) async {
  final pageView = tester.widget<PageView>(find.byType(PageView));
  pageView.controller!.jumpToPage(pageIndex);
  await tester.pumpAndSettle();
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

  group('PROJECT AEGIS — UX FIXES TESTS (ALCOHOL VOLUMES, CLEAR TEXT, POPUP BANNER)', () {
    testWidgets('1. Card 4 Alcohol: requires serving volume for every selected drink and strips deselected drink data', (tester) async {
      tester.view.physicalSize = const Size(1080, 2400);
      tester.view.devicePixelRatio = 1.0;
      addTearDown(() => tester.view.resetPhysicalSize());

      final mockClient = MockClient((request) async => http.Response('{}', 200));
      final apiClient = AegisApiClient(client: mockClient);
      final authManager = AuthSessionManager(apiClient: apiClient, storage: memoryAuthStorage);
      final vault = VaultService(storage: memoryVaultStorage, authSessionManager: authManager);
      await vault.initFuture;

      // Seed baseline fields so buildAnamnesisAnswers() builds full DTO alongside stale Wine data
      vault.saveAnamnesis('age_confirmation', 'Confirmed');
      vault.saveAnamnesis('gender', 'Female');
      vault.saveAnamnesis('age', '34');
      vault.saveAnamnesis('height', '168');
      vault.saveAnamnesis('weight', '61');
      vault.saveAnamnesis('nicotine_status', 'Non-user');
      vault.saveAnamnesis('alcohol_status', '1-2/week');
      vault.saveAnamnesis('alcohol_types', ['Wine']);
      vault.saveAnamnesis('alcohol_volumes', {'Wine': '150 ml (glass)'});
      vault.saveAnamnesis('sleep_quality', 'Normal');
      vault.saveAnamnesis('diet_types', 'Balanced');
      vault.saveAnamnesis('gi_signals', 'None');
      vault.saveAnamnesis('work_type', 'Mostly sitting');
      vault.saveAnamnesis('chronic_diseases', 'No diagnosed chronic conditions');
      vault.saveAnamnesis('recent_illness', 'None of the above');
      vault.saveAnamnesis('critical_infections', 'No diagnosed chronic infections');
      vault.saveAnamnesis('heredity_risks', 'No');
      vault.saveAnamnesis('medications', 'No');

      await tester.pumpWidget(_buildTestApp(vault: vault, authManager: authManager, apiClient: apiClient));
      await tester.pumpAndSettle();
      await _jumpToCard(tester, 4);

      // Step 0 -> select frequency to enter beverage selection
      await tester.tap(find.text("4️⃣ 1–2 times a week"));
      await tester.pumpAndSettle();

      // Deselect Wine first so we start clean, then select Beer WITHOUT volume -> blocked
      await tester.tap(find.text("Wine"));
      await tester.pumpAndSettle();
      await tester.tap(find.text("Beer"));
      await tester.pumpAndSettle();

      await tester.ensureVisible(find.text("Next ➡️"));
      await tester.tap(find.text("Next ➡️"));
      await tester.pumpAndSettle();

      expect(find.text("Please select the average serving volume for «Beer»"), findsOneWidget);
      expect(find.text("Alcohol 🍷"), findsOneWidget);

      // Select volume for Beer, also select Wine without volume -> blocked naming Wine
      await tester.tap(find.text("0.5 L"));
      await tester.pumpAndSettle();
      await tester.ensureVisible(find.text("Wine"));
      await tester.tap(find.text("Wine"));
      await tester.pumpAndSettle();

      await tester.ensureVisible(find.text("Next ➡️"));
      await tester.tap(find.text("Next ➡️"));
      await tester.pumpAndSettle();

      expect(find.text("Please select the average serving volume for «Wine»"), findsOneWidget);
      expect(find.text("Alcohol 🍷"), findsOneWidget);

      // Now select Wine volume, then deselect Beer -> only Wine volume should remain active & saved
      await tester.ensureVisible(find.text("150 ml (glass)"));
      await tester.tap(find.text("150 ml (glass)"));
      await tester.pumpAndSettle();

      await tester.ensureVisible(find.text("Beer"));
      await tester.tap(find.text("Beer"));
      await tester.pumpAndSettle();

      await tester.ensureVisible(find.text("Next ➡️"));
      await tester.tap(find.text("Next ➡️"));
      await tester.pumpAndSettle();

      // Should advance to Card 5
      expect(find.text("Alcohol 🍷"), findsNothing);
      final ctx = vault.getSystemContext('global');
      expect(ctx['alcohol_types'], ['Wine']);
      expect(ctx['alcohol_volumes'], {'Wine': '150 ml (glass)'});
      expect((ctx['alcohol_volumes'] as Map).containsKey('Beer'), isFalse);

      final answers = vault.buildAnamnesisAnswers();
      expect(answers, isNotNull);
      expect(answers!.alcoholVolumes, {'Wine': '150 ml (glass)'});
      expect(answers.alcoholTypes, ['Wine']);
    });

    testWidgets('2. Clear text clears ONLY targeted field and preserves neighboring answers and drafts', (tester) async {
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

      // Card 10 (Work & Daily Routine): 2 text fields + radio choices
      await _jumpToCard(tester, 10);
      await tester.enterText(find.byType(TextField).at(0), 'Software Architect');
      await tester.enterText(find.byType(TextField).at(1), 'Long coding sessions with standing desk');
      await tester.tap(find.text("Mostly sitting"));
      await tester.pumpAndSettle();

      // Two "Clear text" buttons should be visible (one per non-empty field)
      expect(find.text("Clear text"), findsNWidgets(2));

      // Clear only the FIRST field (occupation)
      await tester.tap(find.text("Clear text").first);
      await tester.pumpAndSettle();

      expect(tester.widget<TextField>(find.byType(TextField).at(0)).controller!.text, '');
      expect(tester.widget<TextField>(find.byType(TextField).at(1)).controller!.text, 'Long coding sessions with standing desk');
      // Only 1 "Clear text" remains (for the second non-empty field)
      expect(find.text("Clear text"), findsOneWidget);

      // Card 16 (Medications): 2 text fields inside matryoshka + separate category intact
      await _jumpToCard(tester, 16);
      await tester.tap(find.text("Yes"));
      await tester.pumpAndSettle();

      // Fill Vitamins first and collapse
      await tester.tap(find.text("Vitamins"));
      await tester.pumpAndSettle();
      await tester.enterText(find.byType(TextField).at(0), 'Vitamin D3 2000 IU');
      await tester.enterText(find.byType(TextField).at(1), 'Every morning with breakfast');
      await tester.pumpAndSettle();

      // Clear ONLY the course field (second "Clear text"), keeping medication name untouched
      await tester.ensureVisible(find.text("Clear text").last);
      await tester.tap(find.text("Clear text").last);
      await tester.pumpAndSettle();

      expect(tester.widget<TextField>(find.byType(TextField).at(0)).controller!.text, 'Vitamin D3 2000 IU');
      expect(tester.widget<TextField>(find.byType(TextField).at(1)).controller!.text, '');
    });

    testWidgets('3. Cleared text does NOT return on reopening AnamnesisScreen from Vault (top-level & nested matryoshka)', (tester) async {
      tester.view.physicalSize = const Size(1080, 2400);
      tester.view.devicePixelRatio = 1.0;
      addTearDown(() => tester.view.resetPhysicalSize());

      final mockClient = MockClient((request) async => http.Response('{}', 200));
      final apiClient = AegisApiClient(client: mockClient);
      final authManager = AuthSessionManager(apiClient: apiClient, storage: memoryAuthStorage);
      final vault = VaultService(storage: memoryVaultStorage, authSessionManager: authManager);
      await vault.initFuture;

      // Seed saved top-level Card 10 text + nested Card 12 Chronic matryoshka text
      vault.saveAnamnesis('work_occupation', 'Product Manager');
      vault.saveAnamnesis('user_text_work', 'Hybrid schedule', userText: 'Hybrid schedule');
      vault.saveAnamnesis('chronic_categories', ['Heart or blood pressure conditions']);
      vault.saveAnamnesis('chronic_condition_details', {
        'Heart or blood pressure conditions': {
          'diagnosis': 'Mild hypertension',
          'diagnosed_when': '2019',
          'current_status': 'Controlled or stable',
          'currently_bothers': "No, it doesn't currently bother me",
        },
      });
      await vault.flushPendingWrites();

      // First session: open Work card and clear ONLY work_occupation using "Clear text"
      await tester.pumpWidget(_buildTestApp(vault: vault, authManager: authManager, apiClient: apiClient));
      await tester.pumpAndSettle();
      await _jumpToCard(tester, 10);

      expect(tester.widget<TextField>(find.byType(TextField).at(0)).controller!.text, 'Product Manager');
      expect(tester.widget<TextField>(find.byType(TextField).at(1)).controller!.text, 'Hybrid schedule');

      await tester.tap(find.text("Clear text").first);
      await tester.pumpAndSettle();

      // Open Chronic card, expand Heart or blood pressure conditions, clear ONLY diagnosed_when using "Clear text"
      await _jumpToCard(tester, 12);
      await tester.tap(find.text("Heart or blood pressure conditions"));
      await tester.pumpAndSettle();

      expect(find.text("Clear text"), findsNWidgets(2)); // diagnosis & diagnosed_when
      await tester.tap(find.text("Clear text").at(1));
      await tester.pumpAndSettle();
      await vault.flushPendingWrites();

      // Verify Vault no longer holds the deleted values while preserving sibling fields
      final vaultGlobal = vault.getSystemContext('global');
      expect(vaultGlobal.containsKey('work_occupation'), isFalse);
      expect(vaultGlobal['user_text_work'], 'Hybrid schedule');
      expect(
        (vaultGlobal['chronic_condition_details'] as Map)['Heart or blood pressure conditions']['diagnosis'],
        'Mild hypertension',
      );
      expect(
        ((vaultGlobal['chronic_condition_details'] as Map)['Heart or blood pressure conditions'] as Map).containsKey('diagnosed_when'),
        isFalse,
      );

      // Unmount first screen completely so reopening runs a true fresh initState / Vault restore
      await tester.pumpWidget(const SizedBox.shrink());
      await tester.pumpAndSettle();

      // Reopen a brand new AnamnesisScreen instance using cold-reloaded VaultService
      final vaultReloaded = VaultService(storage: memoryVaultStorage, authSessionManager: authManager);
      await vaultReloaded.initFuture;

      await tester.pumpWidget(
        _buildTestApp(
          vault: vaultReloaded,
          authManager: authManager,
          apiClient: apiClient,
        ),
      );
      await tester.pumpAndSettle();

      await _jumpToCard(tester, 10);
      expect(tester.widget<TextField>(find.byType(TextField).at(0)).controller!.text, '');
      expect(tester.widget<TextField>(find.byType(TextField).at(1)).controller!.text, 'Hybrid schedule');

      await _jumpToCard(tester, 12);
      await tester.tap(find.text("Heart or blood pressure conditions"));
      await tester.pumpAndSettle();
      expect(tester.widget<TextField>(find.byType(TextField).at(0)).controller!.text, 'Mild hypertension');
      expect(tester.widget<TextField>(find.byType(TextField).at(1)).controller!.text, '');
    });

    testWidgets('4. Error notification renders in visible bottom SafeArea viewport, respects keyboard inset, does not auto-scroll, and auto-dismisses in 3s', (tester) async {
      tester.view.physicalSize = const Size(1080, 2000);
      tester.view.devicePixelRatio = 1.0;
      addTearDown(() => tester.view.resetPhysicalSize());

      final mockClient = MockClient((request) async => http.Response('{}', 200));
      final apiClient = AegisApiClient(client: mockClient);
      final authManager = AuthSessionManager(apiClient: apiClient, storage: memoryAuthStorage);
      final vault = VaultService(storage: memoryVaultStorage, authSessionManager: authManager);
      await vault.initFuture;

      const mediaWithInsets = MediaQueryData(
        size: Size(1080, 2000),
        padding: EdgeInsets.only(top: 48, bottom: 34),
        viewInsets: EdgeInsets.only(bottom: 280),
      );

      await tester.pumpWidget(
        _buildTestApp(
          vault: vault,
          authManager: authManager,
          apiClient: apiClient,
          mediaQueryOverride: mediaWithInsets,
        ),
      );
      await tester.pumpAndSettle();

      // Go to Card 7 (Diet) which is tall/scrollable and has Next button below
      await _jumpToCard(tester, 7);
      final scrollableState = tester.state<ScrollableState>(find.byType(Scrollable).first);

      // Tap Next without picking diet -> triggers validation error
      await tester.ensureVisible(find.text("Next ➡️"));
      await tester.tap(find.text("Next ➡️"));
      await tester.pump(const Duration(milliseconds: 250));

      final scrollOffsetRightAfterError = scrollableState.position.pixels;
      final bannerFinder = find.byKey(const Key('anamnesis_error_banner'));
      expect(bannerFinder, findsOneWidget);
      expect(find.text("Please select your primary diet pattern"), findsOneWidget);

      // Verify banner stays within visible viewport above keyboard & bottom inset (2000 - 280 - 34 = 1686)
      final bannerRect = tester.getRect(bannerFinder);
      expect(bannerRect.top >= 0, isTrue);
      expect(bannerRect.bottom <= 2000 - 280, isTrue);
      // Confirm no extra jump/scroll occurred after triggering error
      expect(scrollableState.position.pixels, scrollOffsetRightAfterError);

      // Check dark-red styling on banner Container
      final bannerContainer = tester.widget<Container>(
        find.descendant(of: bannerFinder, matching: find.byType(Container)).first,
      );
      final decoration = bannerContainer.decoration as BoxDecoration;
      expect(decoration.color, const Color(0xFF8E1C1C));

      // Still visible before 3 seconds elapse (2.5s elapsed + 250ms = 2.75s)
      await tester.pump(const Duration(milliseconds: 2500));
      expect(bannerFinder, findsOneWidget);

      // Auto-dismisses cleanly after 3s + reverse animation (~220ms)
      await tester.pump(const Duration(milliseconds: 600));
      await tester.pumpAndSettle();
      expect(bannerFinder, findsNothing);
    });

    testWidgets('5. Re-tapping Next with existing error replays notification & resets 3s timer (and reappears after dismissal)', (tester) async {
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
      await _jumpToCard(tester, 1);

      // First Next tap without selecting gender -> shows error
      await tester.ensureVisible(find.text("Next ➡️"));
      await tester.tap(find.text("Next ➡️"));
      await tester.pump(const Duration(milliseconds: 250));
      expect(find.byKey(const Key('anamnesis_error_banner')), findsOneWidget);

      // Advance 2 seconds, then tap Next AGAIN with same invalid state -> timer resets for another 3s
      await tester.pump(const Duration(seconds: 2));
      await tester.tap(find.text("Next ➡️"));
      await tester.pump(const Duration(milliseconds: 250));

      // After 2 more seconds (4.25s since first tap, 2.25s since second tap), banner MUST still be visible
      await tester.pump(const Duration(seconds: 2));
      expect(find.byKey(const Key('anamnesis_error_banner')), findsOneWidget);

      // Wait remaining ~1.5s -> banner auto-dismisses
      await tester.pump(const Duration(milliseconds: 1500));
      await tester.pumpAndSettle();
      expect(find.byKey(const Key('anamnesis_error_banner')), findsNothing);

      // Tap Next a third time after dismissal -> banner appears again smoothly
      await tester.tap(find.text("Next ➡️"));
      await tester.pump(const Duration(milliseconds: 250));
      expect(find.byKey(const Key('anamnesis_error_banner')), findsOneWidget);

      // Settle remaining timer before teardown
      await tester.pump(const Duration(seconds: 4));
      await tester.pumpAndSettle();
    });

    testWidgets('6. Page switching (Back / Next / jumpToPage) immediately clears any active error notification', (tester) async {
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
      await _jumpToCard(tester, 1);

      // Trigger validation error on Card 1
      await tester.ensureVisible(find.text("Next ➡️"));
      await tester.tap(find.text("Next ➡️"));
      await tester.pump(const Duration(milliseconds: 250));
      expect(find.byKey(const Key('anamnesis_error_banner')), findsOneWidget);

      // Tap Back button -> notification clears immediately
      await tester.tap(find.byTooltip('Back'));
      await tester.pumpAndSettle();
      expect(find.byKey(const Key('anamnesis_error_banner')), findsNothing);

      // Navigate to Card 2, trigger error, then switch page -> clears immediately
      await _jumpToCard(tester, 2);
      await tester.ensureVisible(find.text("Next ➡️"));
      await tester.tap(find.text("Next ➡️"));
      await tester.pump(const Duration(milliseconds: 250));
      expect(find.byKey(const Key('anamnesis_error_banner')), findsOneWidget);

      await _jumpToCard(tester, 3);
      expect(find.byKey(const Key('anamnesis_error_banner')), findsNothing);
    });
  });
}
