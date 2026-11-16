import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:provider/provider.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'package:aegis_health/anamnesis_screen.dart';
import 'package:aegis_health/main.dart';
import 'package:aegis_health/prototypes/aegis_splash_hologram_prototype.dart';
import 'package:aegis_health/services/aegis_cascade_controller.dart';
import 'package:aegis_health/services/api/aegis_api_client.dart';
import 'package:aegis_health/services/athena_trigger_service.dart';
import 'package:aegis_health/services/auth/auth_session_manager.dart';
import 'package:aegis_health/services/auth/auth_token_storage.dart';
import 'package:aegis_health/services/brain_service.dart';
import 'package:aegis_health/services/health_data_provider.dart';
import 'package:aegis_health/services/sync/vitals_sync_queue.dart';
import 'package:aegis_health/services/time_service.dart';
import 'package:aegis_health/services/vault_service.dart';
import 'package:aegis_health/services/vault_storage_service.dart';
import 'package:aegis_health/services/vitals_monitor_service.dart';

Widget _buildProductionGraph({
  required VaultService vault,
  required AuthSessionManager authManager,
  required AegisApiClient apiClient,
  required VitalsSyncQueue syncQueue,
  required Widget child,
}) {
  return MultiProvider(
    providers: [
      ChangeNotifierProvider<VaultService>.value(value: vault),
      ChangeNotifierProvider<BrainService>(create: (_) => BrainService()),
      ChangeNotifierProvider<TimeService>.value(value: TimeService()),
      Provider<AthenaTriggerService>(create: (_) => AthenaTriggerService()),
      ChangeNotifierProxyProvider<VaultService, AegisCascadeController>(
        create: (ctx) =>
            AegisCascadeController(vaultService: ctx.read<VaultService>()),
        update: (ctx, v, prev) => prev ?? AegisCascadeController(vaultService: v),
      ),
      ChangeNotifierProxyProvider2<TimeService, VaultService,
          VitalsMonitorService>(
        create: (ctx) => VitalsMonitorService(
          ctx.read<TimeService>(),
          ctx.read<VaultService>(),
        ),
        update: (ctx, time, v, prev) => prev!..updateTimeService(time),
      ),
      Provider<AegisApiClient>.value(value: apiClient),
      ChangeNotifierProvider<AuthSessionManager>.value(value: authManager),
      ChangeNotifierProvider<VitalsSyncQueue>.value(value: syncQueue),
      ChangeNotifierProxyProvider3<VaultService, VitalsMonitorService,
          VitalsSyncQueue, HealthDataProvider>(
        create: (ctx) => HealthDataProvider(
          vaultService: ctx.read<VaultService>(),
          vitalsMonitorService: ctx.read<VitalsMonitorService>(),
          syncQueue: ctx.read<VitalsSyncQueue>(),
        ),
        update: (ctx, v, vitals, q, prev) =>
            (prev ??
                HealthDataProvider(
                  vaultService: v,
                  vitalsMonitorService: vitals,
                  syncQueue: q,
                ))
              ..updateDependencies(
                vaultService: v,
                vitalsMonitorService: vitals,
                syncQueue: q,
              ),
      ),
    ],
    child: child,
  );
}

class _LifecycleProbeWidget extends StatefulWidget {
  const _LifecycleProbeWidget({
    required this.onInit,
    required this.onDispose,
  });

  final VoidCallback onInit;
  final VoidCallback onDispose;

  @override
  State<_LifecycleProbeWidget> createState() => _LifecycleProbeWidgetState();
}

class _LifecycleProbeWidgetState extends State<_LifecycleProbeWidget> {
  @override
  void initState() {
    super.initState();
    widget.onInit();
  }

  @override
  void dispose() {
    widget.onDispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return const Scaffold(
      body: Center(
        child: Text('Destination Ready', key: Key('probe_destination_text')),
      ),
    );
  }
}

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  late InMemoryVaultStorage memoryVaultStorage;
  late InMemoryAuthTokenStorage memoryAuthStorage;

  setUp(() {
    SharedPreferences.setMockInitialValues({});
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

  group('AEGIS PRODUCTION STARTUP INTEGRATION: Splash V4 -> RootGate', () {
    testWidgets(
      'Scenario A (New User): Splash V4 -> curtains block early taps -> 18+ consent screen -> Card 1 Biological Profile with zero demo & zero replay',
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
        final syncQueue = VitalsSyncQueue(
          apiClient: apiClient,
          authSessionManager: authManager,
          enforcePatientIsolation: true,
        );
        await syncQueue.init();

        int hapticCalls = 0;
        int auraStartedCalls = 0;

        await tester.pumpWidget(
          _buildProductionGraph(
            vault: vault,
            authManager: authManager,
            apiClient: apiClient,
            syncQueue: syncQueue,
            child: AegisApp(
              debugSkipSplashVideoInitialization: true,
              debugSimulatedSplashVideoDuration:
                  const Duration(milliseconds: 300),
              onSplashRPeakHaptic: () async {
                hapticCalls++;
              },
              onSplashAuraAudioStarted: () {
                auraStartedCalls++;
              },
            ),
          ),
        );

        // Immediately at boot: video hold visible, NO demo screen, NO RootGate/18+ screen mounted yet
        expect(find.byKey(const Key('aegis_splash_fallback_frame')), findsOneWidget);
        expect(find.byKey(const Key('aegis_demo_consent_screen')), findsNothing);
        expect(find.byKey(const Key('replay_splash_demo_button')), findsNothing);
        expect(find.text('Clinical & Age Clearance'), findsNothing);

        // Aura begins once at t=0; cross 300ms video hold cleanly then enter Phase B (~930ms)
        await tester.pump(const Duration(milliseconds: 20));
        expect(auraStartedCalls, 1);

        await tester.pump(const Duration(milliseconds: 300));
        await tester.pump(const Duration(milliseconds: 930));
        expect(hapticCalls, 1);
        expect(find.byKey(const Key('aegis_ecg_hologram_painter')), findsOneWidget);
        expect(find.text('Clinical & Age Clearance'), findsNothing);

        // Enter Phase C curtain reveal (~1630ms into transition, pC ~ 0.44):
        // real 18+ screen is visible under curtains, taps blocked by IgnorePointer.
        await tester.pump(const Duration(milliseconds: 700));
        expect(find.text('Clinical & Age Clearance'), findsOneWidget);
        expect(find.byType(AnamnesisScreen), findsOneWidget);
        expect(find.byKey(const Key('aegis_demo_consent_screen')), findsNothing);

        await tester.tap(
          find.byKey(const Key('age_gate_select_all_button')),
          warnIfMissed: false,
        );
        await tester.pump();
        // SELECT ALL did NOT trigger while curtains were still parting
        expect(find.text('SELECT ALL'), findsOneWidget);

        // Finish full Phase C reveal -> curtains and outer ClipRect removed, screen interactive
        await tester.pumpAndSettle();
        expect(find.byKey(const Key('aegis_ecg_hologram_painter')), findsNothing);
        expect(find.byKey(const Key('aegis_demo_consent_screen')), findsNothing);
        expect(find.text('Clinical & Age Clearance'), findsOneWidget);
        expect(find.text('REGULATORY & LIABILITY NOTICE'), findsOneWidget);
        expect(find.byType(LinearProgressIndicator), findsNothing);

        // Complete 3 confirmations -> INITIALIZE PROTOCOL -> Card 1 Biological Profile
        await tester.ensureVisible(
          find.byKey(const Key('age_gate_select_all_button')),
        );
        await tester.tap(find.byKey(const Key('age_gate_select_all_button')));
        await tester.pump();
        expect(find.text('SELECT ALL'), findsNothing);

        await tester.ensureVisible(
          find.byKey(const Key('age_gate_initialize_button')),
        );
        await tester.tap(find.byKey(const Key('age_gate_initialize_button')));
        await tester.pumpAndSettle();

        expect(vault.getSystemContext('global')['age_confirmation'], 'Confirmed');
        expect(find.text('Biological Profile'), findsOneWidget);
        expect(find.byType(LinearProgressIndicator), findsOneWidget);

        // Unrelated rebuild must NOT replay Splash V4
        vault.notifyListeners();
        await tester.pumpAndSettle();
        expect(find.text('Biological Profile'), findsOneWidget);
        expect(auraStartedCalls, 1);
        expect(hapticCalls, 1);

        await tester.pumpWidget(const SizedBox.shrink());
        syncQueue.dispose();
        vault.dispose();
        authManager.dispose();
        apiClient.close();
      },
    );

    testWidgets(
      'Scenario B (Existing Consilium User): Splash V4 -> RootGateScreen -> AtlasScreen directly without 18+ or Vault reset',
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

        // Pre-populate completed Consilium + existing patient data
        vault.saveAnamnesis('age_confirmation', 'Confirmed');
        vault.saveAnamnesis('gender', 'Female');
        vault.saveAnamnesis('age', '31');
        vault.updateSystemFolder('global', {'athena_welcomed': true});
        vault.updateFlashShelves(
          general: 'Completed baseline consilium summary.',
          training: 'Baseline readiness OK.',
        );
        vault.updateProShelves(
          general: 'Primary clinical synthesis ready.',
          training: 'Recovery nominal.',
        );

        final syncQueue = VitalsSyncQueue(
          apiClient: apiClient,
          authSessionManager: authManager,
          enforcePatientIsolation: true,
        );
        await syncQueue.init();

        await tester.pumpWidget(
          _buildProductionGraph(
            vault: vault,
            authManager: authManager,
            apiClient: apiClient,
            syncQueue: syncQueue,
            child: const AegisApp(
              debugSkipSplashVideoInitialization: true,
              debugSimulatedSplashVideoDuration: Duration(milliseconds: 250),
            ),
          ),
        );

        // Before reveal completes, AtlasScreen is not prematurely visible
        await tester.pump(const Duration(milliseconds: 100));
        expect(find.byType(AtlasScreen), findsNothing);
        expect(find.text('Clinical & Age Clearance'), findsNothing);

        // Cross 250ms video hold, then run 1950ms holographic transition to completion
        await tester.pump(const Duration(milliseconds: 200));
        await tester.pump(const Duration(milliseconds: 2100));
        await tester.pumpAndSettle();

        // Reveals AtlasScreen directly, skipping 18+ gate, and preserves Vault untouched
        expect(find.byType(AtlasScreen), findsOneWidget);
        expect(find.byType(AnamnesisScreen), findsNothing);
        expect(find.text('Clinical & Age Clearance'), findsNothing);
        expect(vault.getSystemContext('global')['age_confirmation'], 'Confirmed');
        expect(vault.getSystemContext('global')['gender'], 'Female');
        expect(vault.getSystemContext('global')['age'], '31');
        expect(
          vault.getSystemContext('core_cascade')['flash_general_shelf'],
          'Completed baseline consilium summary.',
        );

        await tester.pumpWidget(const SizedBox.shrink());
        syncQueue.dispose();
        vault.dispose();
        authManager.dispose();
        apiClient.close();
      },
    );

    testWidgets(
      'Scenario C: Destination child initializes ONCE across Phase C -> completion and survives rebuilds without re-init or splash replay',
      (tester) async {
        int initCount = 0;
        int disposeCount = 0;
        int rebuildTick = 0;
        late StateSetter setOuterState;

        await tester.pumpWidget(
          MaterialApp(
            home: StatefulBuilder(
              builder: (context, setState) {
                setOuterState = setState;
                return AegisSplashPrototypeScreen(
                  key: const Key('single_init_splash'),
                  debugSkipVideoInitialization: true,
                  debugSimulatedVideoDuration:
                      const Duration(milliseconds: 200),
                  destinationChild: KeyedSubtree(
                    key: ValueKey<int>(rebuildTick),
                    child: _LifecycleProbeWidget(
                      onInit: () => initCount++,
                      onDispose: () => disposeCount++,
                    ),
                  ),
                );
              },
            ),
          ),
        );

        // Before Phase C: destination not initialized yet
        expect(initCount, 0);
        expect(disposeCount, 0);

        // Pump across 200ms simulated video timer first, then into Phase C curtain reveal (~1450ms)
        await tester.pump(const Duration(milliseconds: 220));
        await tester.pump(const Duration(milliseconds: 1450));
        expect(initCount, 1);
        expect(disposeCount, 0);

        // Finish Phase C -> reparented in-place via GlobalKey without unmounting!
        await tester.pump(const Duration(milliseconds: 600));
        await tester.pumpAndSettle();
        expect(find.byKey(const Key('probe_destination_text')), findsOneWidget);
        expect(initCount, 1);
        expect(disposeCount, 0);

        // Even if parent wrapper rebuilds without changing destination key structure:
        setOuterState(() {});
        await tester.pumpAndSettle();
        expect(find.byKey(const Key('aegis_ecg_hologram_painter')), findsNothing);
        expect(initCount, 1);
        expect(disposeCount, 0);
      },
    );

    testWidgets(
      'Scenario D: AppLifecycle background/resume pauses video hold & ECG without duplicate audio or haptic',
      (tester) async {
        tester.view.physicalSize = const Size(390, 844);
        tester.view.devicePixelRatio = 1.0;
        addTearDown(() => tester.view.resetPhysicalSize());

        int auraStarted = 0;
        int hapticCalls = 0;
        int completed = 0;

        await tester.pumpWidget(
          MaterialApp(
            home: AegisSplashPrototypeScreen(
              debugSkipVideoInitialization: true,
              debugSimulatedVideoDuration: const Duration(milliseconds: 250),
              onAuraAudioStarted: () => auraStarted++,
              onRPeakHaptic: () async => hapticCalls++,
              onTransitionCompleted: () => completed++,
              destinationChild: const Scaffold(
                body: Center(
                  child: Text('RootGate Destination', key: Key('root_dest')),
                ),
              ),
            ),
          ),
        );

        // t=20ms: audio starts once
        await tester.pump(const Duration(milliseconds: 20));
        expect(auraStarted, 1);

        // Background during simulated video before 250ms transition start
        tester.binding.handleAppLifecycleStateChanged(AppLifecycleState.paused);
        await tester.pump(const Duration(milliseconds: 500));
        expect(hapticCalls, 0);
        expect(completed, 0);
        expect(auraStarted, 1);

        // Resume -> transition starts and advances into Phase B before R-peak
        tester.binding.handleAppLifecycleStateChanged(AppLifecycleState.resumed);
        await tester.pump(const Duration(milliseconds: 300));
        expect(find.byKey(const Key('aegis_ecg_hologram_painter')), findsOneWidget);

        // Background again mid-ECG
        tester.binding.handleAppLifecycleStateChanged(AppLifecycleState.paused);
        await tester.pump(const Duration(milliseconds: 1200));
        expect(hapticCalls, 0);
        expect(completed, 0);

        // Resume -> finishes ECG (1 haptic) and completes once
        tester.binding.handleAppLifecycleStateChanged(AppLifecycleState.resumed);
        await tester.pump();
        await tester.pump(const Duration(milliseconds: 2100));
        await tester.pumpAndSettle();

        expect(auraStarted, 1);
        expect(hapticCalls, 1);
        expect(completed, 1);
        expect(find.byKey(const Key('root_dest')), findsOneWidget);
      },
    );

    testWidgets(
      'Scenario E: Decoder timeout fallback & MediaQuery(disableAnimations: true) reach RootGate safely',
      (tester) async {
        // 1. Reduced-motion accessibility flag reveals destination immediately
        int completedCount = 0;
        int auraStartedCount = 0;

        await tester.pumpWidget(
          MediaQuery(
            data: const MediaQueryData(disableAnimations: true),
            child: MaterialApp(
              home: AegisSplashPrototypeScreen(
                debugSkipVideoInitialization: true,
                debugSimulatedVideoDuration:
                    const Duration(milliseconds: 500),
                onAuraAudioStarted: () => auraStartedCount++,
                onTransitionCompleted: () => completedCount++,
                destinationChild: const Scaffold(
                  body: Text('Accessible RootGate', key: Key('a11y_dest')),
                ),
              ),
            ),
          ),
        );

        // Immediately visible on first frame without splash motion or audio
        expect(find.byKey(const Key('a11y_dest')), findsOneWidget);
        expect(auraStartedCount, 0);
        await tester.pump();
        expect(completedCount, 1);

        // 2. Unmocked platform decoder stall hits startup timeout guard -> proceeds to RootGate cleanly
        int fallbackCompleted = 0;
        await tester.pumpWidget(
          MaterialApp(
            home: AegisSplashPrototypeScreen(
              debugSkipVideoInitialization: false,
              onTransitionCompleted: () => fallbackCompleted++,
              destinationChild: const Scaffold(
                body: Text('Fallback RootGate', key: Key('fallback_dest')),
              ),
            ),
          ),
        );

        await tester.pump(const Duration(milliseconds: 2550));
        await tester.pump(const Duration(milliseconds: 2050));
        await tester.pumpAndSettle();

        expect(fallbackCompleted, 1);
        expect(find.byKey(const Key('fallback_dest')), findsOneWidget);

        await tester.pumpWidget(const SizedBox.shrink());
      },
    );
  });
}
