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

BoxDecoration _findAnimatedBoxDecorationByKey(WidgetTester tester, Key key) {
  final finder = find.descendant(
    of: find.byKey(key),
    matching: find.byType(AnimatedContainer),
  );
  final animatedContainer = tester.widget<AnimatedContainer>(finder.first);
  return animatedContainer.decoration! as BoxDecoration;
}

BoxDecoration _findButtonBoxDecoration(WidgetTester tester, Key key) {
  expect(
    find.descendant(
      of: find.byKey(key),
      matching: find.byType(AnimatedContainer),
    ),
    findsNothing,
  );
  expect(
    find.descendant(
      of: find.byKey(key),
      matching: find.byType(AnimatedDefaultTextStyle),
    ),
    findsNothing,
  );
  final finder = find.descendant(
    of: find.byKey(key),
    matching: find.byType(Container),
  );
  final container = tester.widget<Container>(finder.first);
  return container.decoration! as BoxDecoration;
}

const String _expectedNoticeHeader = 'REGULATORY & LIABILITY NOTICE';
const String _expectedNoticeBody =
    'AEGIS provides AI-generated health insights for informational purposes only. Results are not a medical diagnosis or treatment recommendation and do not replace professional medical advice. In an emergency, contact emergency services.';

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

  group('AEGIS 18+ CONSENT SCREEN REDESIGN (ARGON SPEC)', () {
    testWidgets(
      '1. Initial state: no progress bar or mini-header, all 3 cards unselected, SELECT ALL shown, INITIALIZE PROTOCOL disabled without glow',
      (tester) async {
        tester.view.physicalSize = const Size(390, 844);
        tester.view.devicePixelRatio = 1.0;
        addTearDown(() => tester.view.resetPhysicalSize());

        final mockClient = MockClient((_) async => http.Response('{}', 200));
        final apiClient = AegisApiClient(client: mockClient);
        final authManager = AuthSessionManager(
          apiClient: apiClient,
          storage: memoryAuthStorage,
        );
        final vault = VaultService(
          storage: memoryVaultStorage,
          authSessionManager: authManager,
        );
        await vault.initFuture;

        await tester.pumpWidget(
          _buildTestApp(
            vault: vault,
            authManager: authManager,
            apiClient: apiClient,
          ),
        );
        await tester.pumpAndSettle();

        // Deep Obsidian scaffold and NO LinearProgressIndicator or mini-header on Page 0
        final scaffold = tester.widget<Scaffold>(find.byType(Scaffold));
        expect(scaffold.backgroundColor, const Color(0xFF070B12));
        expect(find.byType(LinearProgressIndicator), findsNothing);
        expect(find.byIcon(Icons.arrow_back), findsNothing);
        expect(find.byIcon(Icons.close), findsNothing);

        // Header and 3 cards matching demo.png
        expect(find.text('Clinical & Age Clearance'), findsOneWidget);
        expect(
          find.text('Acknowledge operational parameters to initialize telemetry'),
          findsOneWidget,
        );
        expect(find.text('Adult Physiology (18+)'), findsOneWidget);
        expect(
          find.text('Calibrated strictly for adult reference ranges'),
          findsOneWidget,
        );
        expect(find.text('Informational AI Synthesis'), findsOneWidget);
        expect(
          find.text('Not a substitute for emergency or clinical diagnosis'),
          findsOneWidget,
        );
        expect(find.text('Encrypted Biometric Vault'), findsOneWidget);
        expect(
          find.text('Zero-knowledge local and cloud telemetry processing'),
          findsOneWidget,
        );

        // All 3 cards start unselected with inactive border (#1E2D3D) and zero glow
        for (final key in const [
          Key('age_gate_card_0'),
          Key('age_gate_card_1'),
          Key('age_gate_card_2'),
        ]) {
          final decoration = _findAnimatedBoxDecorationByKey(tester, key);
          expect((decoration.border! as Border).top.color, const Color(0xFF1E2D3D));
          expect(decoration.boxShadow, isEmpty);
        }
        expect(find.byType(BackdropFilter), findsNothing);

        // SELECT ALL and REGULATORY & LIABILITY NOTICE visible; DESELECT ALL must not exist
        expect(find.text('SELECT ALL'), findsOneWidget);
        expect(find.text('DESELECT ALL'), findsNothing);
        expect(find.text(_expectedNoticeHeader), findsOneWidget);
        expect(find.text(_expectedNoticeBody), findsOneWidget);

        final noticeHeaderWidget = tester.widget<Text>(
          find.text(_expectedNoticeHeader),
        );
        expect(noticeHeaderWidget.style!.color, const Color(0xFF5E768D));
        final noticeBodyWidget = tester.widget<Text>(
          find.text(_expectedNoticeBody),
        );
        expect(noticeBodyWidget.style!.color, const Color(0xFF5E768D));
        expect(noticeBodyWidget.style!.fontSize, 11.0);
        expect(noticeBodyWidget.style!.height, 1.4);

        // INITIALIZE PROTOCOL disabled, #1E2D3D fill, #7E92A6 text, zero boxShadow glow
        final initDecoration = _findButtonBoxDecoration(
          tester,
          const Key('age_gate_initialize_button'),
        );
        expect(initDecoration.color, const Color(0xFF1E2D3D));
        expect(initDecoration.gradient, isNull);
        expect(initDecoration.boxShadow, isEmpty);
        final disabledBtnText = tester.widget<Text>(
          find.text('INITIALIZE PROTOCOL'),
        );
        expect(disabledBtnText.style!.color, const Color(0xFF7E92A6));

        // Tapping disabled INITIALIZE PROTOCOL neither saves nor navigates
        await tester.ensureVisible(
          find.byKey(const Key('age_gate_initialize_button')),
        );
        await tester.tap(find.byKey(const Key('age_gate_initialize_button')));
        await tester.pumpAndSettle();
        expect(vault.getSystemContext('global')['age_confirmation'], isNull);
        expect(find.text('Biological Profile'), findsNothing);

        vault.dispose();
        authManager.dispose();
        apiClient.close();
      },
    );

    testWidgets(
      '2. Individual card toggling activates button only when all 3 selected, hides SELECT ALL, and restores inactive state on uncheck',
      (tester) async {
        tester.view.physicalSize = const Size(390, 844);
        tester.view.devicePixelRatio = 1.0;
        addTearDown(() => tester.view.resetPhysicalSize());

        final mockClient = MockClient((_) async => http.Response('{}', 200));
        final apiClient = AegisApiClient(client: mockClient);
        final authManager = AuthSessionManager(
          apiClient: apiClient,
          storage: memoryAuthStorage,
        );
        final vault = VaultService(
          storage: memoryVaultStorage,
          authSessionManager: authManager,
        );
        await vault.initFuture;

        await tester.pumpWidget(
          _buildTestApp(
            vault: vault,
            authManager: authManager,
            apiClient: apiClient,
          ),
        );
        await tester.pumpAndSettle();

        // Tap Card 0 -> Card 0 active (neon border + BackdropFilter), CTA still disabled
        await tester.tap(find.byKey(const Key('age_gate_card_0')));
        await tester.pumpAndSettle();
        final card0Active = _findAnimatedBoxDecorationByKey(
          tester,
          const Key('age_gate_card_0'),
        );
        expect((card0Active.border! as Border).top.color, const Color(0x9900E5FF));
        expect(card0Active.boxShadow, isNotEmpty);
        expect(find.byType(BackdropFilter), findsOneWidget);
        expect(find.text('SELECT ALL'), findsOneWidget);
        expect(
          _findButtonBoxDecoration(
            tester,
            const Key('age_gate_initialize_button'),
          ).boxShadow,
          isEmpty,
        );

        // Tap Card 1 -> 2 of 3 selected, CTA still disabled
        await tester.tap(find.byKey(const Key('age_gate_card_1')));
        await tester.pumpAndSettle();
        expect(find.text('SELECT ALL'), findsOneWidget);
        expect(
          _findButtonBoxDecoration(
            tester,
            const Key('age_gate_initialize_button'),
          ).gradient,
          isNull,
        );

        // Tap Card 2 -> 3 of 3 selected: verify instantaneous single-pump switch & zero vertical jump
        final double noticeYBeforeSelectAllHide = tester
            .getTopLeft(find.text(_expectedNoticeHeader))
            .dy;
        final double buttonYBeforeSelectAllHide = tester
            .getTopLeft(find.byKey(const Key('age_gate_initialize_button')))
            .dy;
        await tester.tap(find.byKey(const Key('age_gate_card_2')));
        await tester.pump();
        expect(find.text('SELECT ALL'), findsNothing);
        expect(find.text('DESELECT ALL'), findsNothing);
        expect(find.text(_expectedNoticeHeader), findsOneWidget);
        expect(find.text(_expectedNoticeBody), findsOneWidget);
        expect(
          tester.getTopLeft(find.text(_expectedNoticeHeader)).dy,
          equals(noticeYBeforeSelectAllHide),
        );
        expect(
          tester
              .getTopLeft(find.byKey(const Key('age_gate_initialize_button')))
              .dy,
          equals(buttonYBeforeSelectAllHide),
        );
        final double noticeBottom = tester
            .getBottomLeft(find.text(_expectedNoticeBody))
            .dy;
        final double buttonTop = tester
            .getTopLeft(find.byKey(const Key('age_gate_initialize_button')))
            .dy;
        expect(buttonTop - noticeBottom, greaterThanOrEqualTo(16.0));

        final activeButton = _findButtonBoxDecoration(
          tester,
          const Key('age_gate_initialize_button'),
        );
        expect(activeButton.gradient, isA<LinearGradient>());
        final gradient = activeButton.gradient! as LinearGradient;
        expect(
          gradient.colors,
          equals(const [Color(0xFF26F0FF), Color(0xFF00C4E0)]),
        );
        expect(activeButton.boxShadow!.length, 2);
        expect(activeButton.boxShadow![0].color, const Color(0x5900E5FF));
        expect(activeButton.boxShadow![0].blurRadius, 16.0);
        expect(activeButton.boxShadow![0].spreadRadius, 1.0);
        expect(activeButton.boxShadow![0].offset, Offset.zero);
        expect(activeButton.boxShadow![1].color, const Color(0x2E00E5FF));
        expect(activeButton.boxShadow![1].blurRadius, 30.0);
        expect(activeButton.boxShadow![1].spreadRadius, 0.0);
        expect(activeButton.boxShadow![1].offset, const Offset(0, 3));
        final activeBtnText = tester.widget<Text>(
          find.text('INITIALIZE PROTOCOL'),
        );
        expect(activeBtnText.style!.color, const Color(0xFF041016));
        await tester.pumpAndSettle();

        // Uncheck Card 1 -> instantaneous single-pump revert to muted disabled, SELECT ALL reappears
        await tester.tap(find.byKey(const Key('age_gate_card_1')));
        await tester.pump();
        expect(find.text('SELECT ALL'), findsOneWidget);
        final revertedButton = _findButtonBoxDecoration(
          tester,
          const Key('age_gate_initialize_button'),
        );
        expect(revertedButton.color, const Color(0xFF1E2D3D));
        expect(revertedButton.gradient, isNull);
        expect(revertedButton.boxShadow, isEmpty);
        final revertedBtnText = tester.widget<Text>(
          find.text('INITIALIZE PROTOCOL'),
        );
        expect(revertedBtnText.style!.color, const Color(0xFF7E92A6));

        vault.dispose();
        authManager.dispose();
        apiClient.close();
      },
    );

    testWidgets(
      '3. SELECT ALL selects all 3 cards, disappears without DESELECT ALL, and active CTA persists Vault + restores Progress Bar on Page 1',
      (tester) async {
        tester.view.physicalSize = const Size(390, 844);
        tester.view.devicePixelRatio = 1.0;
        addTearDown(() => tester.view.resetPhysicalSize());

        final mockClient = MockClient((_) async => http.Response('{}', 200));
        final apiClient = AegisApiClient(client: mockClient);
        final authManager = AuthSessionManager(
          apiClient: apiClient,
          storage: memoryAuthStorage,
        );
        final vault = VaultService(
          storage: memoryVaultStorage,
          authSessionManager: authManager,
        );
        await vault.initFuture;

        await tester.pumpWidget(
          _buildTestApp(
            vault: vault,
            authManager: authManager,
            apiClient: apiClient,
          ),
        );
        await tester.pumpAndSettle();

        // Tap SELECT ALL
        await tester.ensureVisible(
          find.byKey(const Key('age_gate_select_all_button')),
        );
        await tester.tap(find.byKey(const Key('age_gate_select_all_button')));
        await tester.pumpAndSettle();

        // SELECT ALL disappears, DESELECT ALL never appears, notice stays visible, all 3 cards active
        expect(find.text('SELECT ALL'), findsNothing);
        expect(find.text('DESELECT ALL'), findsNothing);
        expect(find.text(_expectedNoticeHeader), findsOneWidget);
        expect(find.text(_expectedNoticeBody), findsOneWidget);
        for (final key in const [
          Key('age_gate_card_0'),
          Key('age_gate_card_1'),
          Key('age_gate_card_2'),
        ]) {
          final decoration = _findAnimatedBoxDecorationByKey(tester, key);
          expect((decoration.border! as Border).top.color, const Color(0x9900E5FF));
        }

        // Tap INITIALIZE PROTOCOL -> saves age_confirmation and navigates to Card 1
        await tester.ensureVisible(
          find.byKey(const Key('age_gate_initialize_button')),
        );
        await tester.tap(find.byKey(const Key('age_gate_initialize_button')));
        await tester.pumpAndSettle();

        expect(
          vault.getSystemContext('global')['age_confirmation'],
          'Confirmed',
        );
        expect(find.text('Biological Profile'), findsOneWidget);

        // Progress bar, back button, and standard #1A1A1A scaffold are restored on Card 1
        expect(find.byType(LinearProgressIndicator), findsOneWidget);
        expect(find.byIcon(Icons.arrow_back), findsOneWidget);
        final card1Scaffold = tester.widget<Scaffold>(find.byType(Scaffold));
        expect(card1Scaffold.backgroundColor, const Color(0xFF1A1A1A));

        // Navigating back to Page 0 hides LinearProgressIndicator again
        await tester.tap(find.byIcon(Icons.arrow_back));
        await tester.pumpAndSettle();
        expect(find.byType(LinearProgressIndicator), findsNothing);
        expect(find.text('Clinical & Age Clearance'), findsOneWidget);

        vault.dispose();
        authManager.dispose();
        apiClient.close();
      },
    );

    testWidgets(
      '4. Compact viewport (360x640): renders and scrolls cleanly without overflow exceptions with notice block visible',
      (tester) async {
        tester.view.physicalSize = const Size(360, 640);
        tester.view.devicePixelRatio = 1.0;
        addTearDown(() => tester.view.resetPhysicalSize());

        final mockClient = MockClient((_) async => http.Response('{}', 200));
        final apiClient = AegisApiClient(client: mockClient);
        final authManager = AuthSessionManager(
          apiClient: apiClient,
          storage: memoryAuthStorage,
        );
        final vault = VaultService(
          storage: memoryVaultStorage,
          authSessionManager: authManager,
        );
        await vault.initFuture;

        await tester.pumpWidget(
          _buildTestApp(
            vault: vault,
            authManager: authManager,
            apiClient: apiClient,
          ),
        );
        await tester.pumpAndSettle();

        expect(tester.takeException(), isNull);
        expect(find.text('SELECT ALL'), findsOneWidget);
        expect(find.text(_expectedNoticeHeader), findsOneWidget);

        await tester.ensureVisible(
          find.byKey(const Key('age_gate_select_all_button')),
        );
        await tester.tap(find.byKey(const Key('age_gate_select_all_button')));
        await tester.pump();
        expect(tester.takeException(), isNull);
        expect(find.text('SELECT ALL'), findsNothing);
        expect(find.text(_expectedNoticeHeader), findsOneWidget);

        await tester.ensureVisible(
          find.byKey(const Key('age_gate_initialize_button')),
        );
        await tester.tap(find.byKey(const Key('age_gate_initialize_button')));
        await tester.pumpAndSettle();

        expect(tester.takeException(), isNull);
        expect(find.text('Biological Profile'), findsOneWidget);

        vault.dispose();
        authManager.dispose();
        apiClient.close();
      },
    );
  });
}
