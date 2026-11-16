import 'dart:io';

import 'package:aegis_health/prototypes/aegis_splash_hologram_prototype.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  group('AEGIS Splash + ECG Hologram Prototype V4', () {
    test(
      '1. V4 7500ms budget (5550ms + 1950ms), 1.08s ECG, -100ms Phase C (0.57s), audio_aura.MP3 & -690ms shift (5240ms Aura + 6362ms R-peak)',
      () {
        expect(
          AegisSplashPrototypeScreen.defaultTransitionStartOffset,
          const Duration(milliseconds: 5550),
        );
        expect(
          AegisHologramTransition.totalDuration,
          const Duration(milliseconds: 1950),
        );
        expect(AegisHologramTransition.totalSeconds, closeTo(1.95, 0.001));
        expect(
          AegisSplashPrototypeScreen.defaultTransitionStartOffset +
              AegisHologramTransition.totalDuration,
          const Duration(milliseconds: 7500),
        );
        expect(
          AegisSplashPrototypeScreen.defaultAuraAudioAssetPath,
          'assets/audio/candidates/audio_aura.MP3',
        );
        expect(
          AegisSplashPrototypeScreen.auraLeadingSilenceInFile,
          const Duration(milliseconds: 5930),
        );
        expect(
          AegisSplashPrototypeScreen.targetAudibleAuraStart,
          const Duration(milliseconds: 5240),
        );
        expect(
          AegisSplashPrototypeScreen.targetRPeakGlobalTime,
          const Duration(milliseconds: 6362),
        );
        expect(
          AegisSplashPrototypeScreen.defaultAuraTimelineShift,
          const Duration(milliseconds: -690),
        );
        // 5930ms first non-silent frame + (-690ms) shift = 5240ms exact audible Aura start.
        expect(
          AegisSplashPrototypeScreen.auraLeadingSilenceInFile +
              AegisSplashPrototypeScreen.defaultAuraTimelineShift,
          AegisSplashPrototypeScreen.targetAudibleAuraStart,
        );

        const defaultScreen = AegisSplashPrototypeScreen();
        expect(
          defaultScreen.transitionStartOffset,
          const Duration(milliseconds: 5550),
        );
        expect(
          defaultScreen.auraAudioAssetPath,
          'assets/audio/candidates/audio_aura.MP3',
        );
        expect(
          defaultScreen.auraTimelineShift,
          const Duration(milliseconds: -690),
        );

        final auraFile = File(AegisSplashPrototypeScreen.defaultAuraAudioAssetPath);
        expect(auraFile.existsSync(), isTrue);
        final auraBytes = auraFile.readAsBytesSync();
        expect(auraBytes.length, greaterThan(100000));
        expect(String.fromCharCodes(auraBytes.sublist(0, 3)), 'ID3');

        // Approved splash video asset exists on disk.
        expect(File('assets/video/aegis_splash.mp4').existsSync(), isTrue);

        expect(AegisHologramTransition.phaseBDurationSeconds, closeTo(1.08, 0.001));
        expect(AegisHologramTransition.phaseCDurationSeconds, closeTo(0.57, 0.001));
        expect(AegisHologramTransition.hasAnamorphicRPeakFlare, isTrue);

        const sizes = [
          Size(360, 640), // 9:16
          Size(390, 844), // ~19.5:9
          Size(412, 915), // 20:9
        ];

        for (final size in sizes) {
          final ratio = AegisHologramTransition.computeRPeakArcRatio(size);
          expect(ratio, greaterThan(0.32));
          expect(ratio, lessThan(0.54));
          expect(ratio, isNot(closeTo(0.465, 0.005)));

          final rPeakInTransitionMs =
              (AegisHologramTransition.phaseBStartSeconds +
                  ratio * AegisHologramTransition.phaseBDurationSeconds) *
              1000.0;
          final rPeakGlobalMs =
              AegisSplashPrototypeScreen.defaultTransitionStartOffset.inMilliseconds +
              rPeakInTransitionMs;
          // Visual R-peak lands at ~6362 ms; with -690ms shift (pre-seeked 690ms into audio_aura.MP3),
          // 5930ms Aura start lands at 5240ms and the CapCut heartbeat strike aligns with 6362ms.
          expect(rPeakGlobalMs, inInclusiveRange(6340.0, 6380.0));

          final initialFlareW =
              AegisHologramTransition.computeAnamorphicFlareWidth(size, 0.0);
          final peakFlareW =
              AegisHologramTransition.computeAnamorphicFlareWidth(size, 1.0);
          expect(peakFlareW, greaterThan(initialFlareW));
          expect(peakFlareW, closeTo(size.width * 0.48, 0.5));
        }
      },
    );

    testWidgets(
      '2. R-peak haptic triggers strictly at PathMetric R-peak during 1.08s ECG and Phase C finishes cleanly at 1950ms',
      (tester) async {
        tester.view.physicalSize = const Size(1080, 2340);
        tester.view.devicePixelRatio = 2.5;
        addTearDown(() {
          tester.view.resetPhysicalSize();
          tester.view.resetDevicePixelRatio();
        });

        int hapticCount = 0;
        int phaseACompleteCount = 0;
        int completedCount = 0;
        bool started = false;
        late StateSetter triggerSetState;

        await tester.pumpWidget(
          MaterialApp(
            home: StatefulBuilder(
              builder: (context, setState) {
                triggerSetState = setState;
                return Scaffold(
                  body: AegisHologramTransition(
                    startTransition: started,
                    splashChild: Container(
                      key: const Key('test_splash_child'),
                      color: const Color(0xFF0A1420),
                    ),
                    consentChild: Container(
                      key: const Key('test_consent_child'),
                      color: const Color(0xFF070B12),
                    ),
                    onPhaseACompleted: () {
                      phaseACompleteCount++;
                    },
                    onRPeakHaptic: () async {
                      hapticCount++;
                    },
                    onTransitionCompleted: () {
                      completedCount++;
                    },
                  ),
                );
              },
            ),
          ),
        );

        final size = tester.getSize(find.byType(AegisHologramTransition));
        final rRatio = AegisHologramTransition.computeRPeakArcRatio(size);
        final rPeakSeconds =
            AegisHologramTransition.phaseBStartSeconds +
            rRatio * AegisHologramTransition.phaseBDurationSeconds;
        final rPeakMs = (rPeakSeconds * 1000).round();

        // Consent child must NOT exist before transition starts
        expect(find.byKey(const Key('test_consent_child')), findsNothing);

        triggerSetState(() => started = true);
        await tester.pump();

        // Before Phase B R-peak: Phase A completes (~380 ms), haptic still 0, consent screen NOT mounted
        await tester.pump(Duration(milliseconds: rPeakMs - 80));
        expect(phaseACompleteCount, 1);
        expect(hapticCount, 0);
        expect(completedCount, 0);
        expect(find.byKey(const Key('test_consent_child')), findsNothing);

        // Cross R-peak arc-length threshold -> haptic fires once, consent still hidden during Phase B
        await tester.pump(const Duration(milliseconds: 110));
        expect(hapticCount, 1);
        expect(find.byKey(const Key('test_consent_child')), findsNothing);

        // Rebuild repeatedly -> haptic stays 1
        triggerSetState(() {});
        await tester.pump(const Duration(milliseconds: 500));
        expect(hapticCount, 1);

        // Finish Phase C (1950 ms total) -> consent screen revealed
        await tester.pump(const Duration(milliseconds: 800));
        await tester.pumpAndSettle();
        expect(hapticCount, 1);
        expect(completedCount, 1);
        expect(find.byKey(const Key('test_consent_child')), findsOneWidget);
      },
    );

    testWidgets(
      '3. Default -690ms aura start at t=0, 5550+1950ms flow, Phase C curtain reveal to edges & clean Replay Demo without double start',
      (tester) async {
        tester.view.physicalSize = const Size(1080, 1920);
        tester.view.devicePixelRatio = 3.0;
        addTearDown(() {
          tester.view.resetPhysicalSize();
          tester.view.resetDevicePixelRatio();
        });

        int hapticCalls = 0;
        int auraStartedCalls = 0;

        await tester.pumpWidget(
          MaterialApp(
            home: AegisSplashPrototypeScreen(
              transitionStartOffset: const Duration(milliseconds: 5550),
              debugSkipVideoInitialization: true,
              debugSimulatedVideoDuration: const Duration(milliseconds: 300),
              onRPeakHaptic: () async {
                hapticCalls++;
              },
              onAuraAudioStarted: () {
                auraStartedCalls++;
              },
            ),
          ),
        );

        // Immediately on build: zero-delay aura timer hasn't pumped yet, splash hold visible, consent NOT mounted
        expect(auraStartedCalls, 0);
        expect(
          find.byKey(const Key('aegis_splash_fallback_frame')),
          findsOneWidget,
        );
        expect(find.byKey(const Key('aegis_demo_consent_screen')), findsNothing);

        // Pump 20ms: with default -690ms shift (pre-seeked into silent intro), aura launches at t=0ms once cleanly
        await tester.pump(const Duration(milliseconds: 20));
        expect(auraStartedCalls, 1);
        expect(find.byKey(const Key('aegis_demo_consent_screen')), findsNothing);

        // At t=320ms (20ms into transition): ECG painter mounted, consent still absent
        await tester.pump(const Duration(milliseconds: 300));
        expect(
          find.byKey(const Key('aegis_ecg_hologram_painter')),
          findsOneWidget,
        );
        expect(find.byKey(const Key('aegis_demo_consent_screen')), findsNothing);

        // Mid Phase A (~220ms into transition): fallback splash still collapsing cleanly
        await tester.pump(const Duration(milliseconds: 200));
        expect(
          find.byKey(const Key('aegis_splash_fallback_frame')),
          findsOneWidget,
        );
        expect(find.byKey(const Key('aegis_demo_consent_screen')), findsNothing);

        // Mid Phase B (~950ms into transition): haptic fired once after 812ms R-peak, consent absent
        await tester.pump(const Duration(milliseconds: 730));
        expect(hapticCalls, 1);
        expect(auraStartedCalls, 1);
        expect(find.byKey(const Key('aegis_demo_consent_screen')), findsNothing);

        // Mid Phase C (~1660ms into 1950ms transition): curtains parting smoothly
        await tester.pump(const Duration(milliseconds: 710));
        expect(
          find.byKey(const Key('aegis_demo_consent_screen')),
          findsOneWidget,
        );
        final midRevealClipRect = tester.getSize(find.byType(ClipRect).first);
        final midClipWidget = tester.widget<ClipRect>(find.byType(ClipRect).first);
        final midClipBounds = midClipWidget.clipper!.getClip(midRevealClipRect);
        expect(midClipBounds.top, greaterThan(0.0));
        expect(midClipBounds.bottom, lessThan(midRevealClipRect.height));

        // Near end of Phase C (~1910ms): curtains have traveled almost to top (0) & bottom (height)
        await tester.pump(const Duration(milliseconds: 250));
        final nearEndClipWidget =
            tester.widget<ClipRect>(find.byType(ClipRect).first);
        final nearEndBounds =
            nearEndClipWidget.clipper!.getClip(midRevealClipRect);
        expect(nearEndBounds.top, lessThan(25.0));
        expect(nearEndBounds.bottom, greaterThan(midRevealClipRect.height - 25.0));

        // Settle completely (proves zero infinite animations & V4 demo label)
        await tester.pumpAndSettle();
        expect(find.text('AEGIS // VISUAL PROTOTYPE V4'), findsOneWidget);
        expect(find.text('Age Confirmation'), findsOneWidget);
        expect(find.byKey(const Key('demo_age_confirm_element')), findsOneWidget);
        expect(hapticCalls, 1);
        expect(auraStartedCalls, 1);

        // Replay Demo cleanly hides consent screen again and runs fresh V4 cycle without double audio
        await tester.tap(find.byKey(const Key('replay_splash_demo_button')));
        await tester.pump();
        expect(
          find.byKey(const Key('aegis_splash_fallback_frame')),
          findsOneWidget,
        );
        expect(find.byKey(const Key('aegis_demo_consent_screen')), findsNothing);
        expect(auraStartedCalls, 1);

        await tester.pump(const Duration(milliseconds: 320));
        expect(auraStartedCalls, 2);
        await tester.pump(const Duration(milliseconds: 2000));
        await tester.pumpAndSettle();
        expect(find.text('Age Confirmation'), findsOneWidget);
        expect(hapticCalls, 2);
        expect(auraStartedCalls, 2);
      },
    );

    testWidgets(
      '4. Background lifecycle pause/resume suppresses haptic while paused and disposes cleanly',
      (tester) async {
        tester.view.physicalSize = const Size(1080, 2400);
        tester.view.devicePixelRatio = 3.0;
        addTearDown(() {
          tester.view.resetPhysicalSize();
          tester.view.resetDevicePixelRatio();
        });

        int hapticCalls = 0;
        int completedCount = 0;

        await tester.pumpWidget(
          MaterialApp(
            home: AegisHologramTransition(
              startTransition: true,
              splashChild: const ColoredBox(color: Color(0xFF0A1420)),
              consentChild: const ColoredBox(
                key: Key('lifecycle_consent'),
                color: Color(0xFF070B12),
              ),
              onRPeakHaptic: () async {
                hapticCalls++;
              },
              onTransitionCompleted: () {
                completedCount++;
              },
            ),
          ),
        );

        // Advance into Phase A (220 ms)
        await tester.pump(const Duration(milliseconds: 220));
        expect(completedCount, 0);
        expect(hapticCalls, 0);

        // Simulate app backgrounding mid-transition -> no haptic fires while paused
        tester.binding.handleAppLifecycleStateChanged(AppLifecycleState.paused);
        await tester.pump(const Duration(milliseconds: 1600));
        expect(completedCount, 0);
        expect(hapticCalls, 0);

        // Return to foreground -> finishes remaining 1950ms V4 timeline cleanly
        tester.binding.handleAppLifecycleStateChanged(AppLifecycleState.resumed);
        await tester.pump();
        await tester.pump(const Duration(milliseconds: 1800));
        await tester.pumpAndSettle();
        expect(completedCount, 1);
        expect(hapticCalls, 1);
        expect(find.byKey(const Key('lifecycle_consent')), findsOneWidget);

        // Unmount cleanly -> no leaked Ticker or Timer assertions
        await tester.pumpWidget(const SizedBox.shrink());
        await tester.pumpAndSettle();
      },
    );

    testWidgets(
      '5. AegisSplashPrototypeScreen pauses aura launch & transition while backgrounded and completes on resume',
      (tester) async {
        tester.view.physicalSize = const Size(1080, 1920);
        tester.view.devicePixelRatio = 3.0;
        addTearDown(() {
          tester.view.resetPhysicalSize();
          tester.view.resetDevicePixelRatio();
        });

        int hapticCalls = 0;
        int auraStartedCalls = 0;

        await tester.pumpWidget(
          MaterialApp(
            home: AegisSplashPrototypeScreen(
              auraTimelineShift: const Duration(milliseconds: 145),
              debugSkipVideoInitialization: true,
              debugSimulatedVideoDuration: const Duration(milliseconds: 250),
              onRPeakHaptic: () async {
                hapticCalls++;
              },
              onAuraAudioStarted: () {
                auraStartedCalls++;
              },
            ),
          ),
        );

        // Pause before +145ms aura offset -> neither aura nor transition has fired
        await tester.pump(const Duration(milliseconds: 100));
        expect(auraStartedCalls, 0);
        expect(hapticCalls, 0);

        tester.binding.handleAppLifecycleStateChanged(AppLifecycleState.paused);
        await tester.pump(const Duration(milliseconds: 1500));
        expect(auraStartedCalls, 0);
        expect(hapticCalls, 0);
        expect(find.byKey(const Key('aegis_demo_consent_screen')), findsNothing);

        // Resume -> aura launches once and full transition completes cleanly
        tester.binding.handleAppLifecycleStateChanged(AppLifecycleState.resumed);
        await tester.pump(const Duration(milliseconds: 300));
        expect(auraStartedCalls, 1);
        await tester.pump(const Duration(milliseconds: 2000));
        await tester.pumpAndSettle();
        expect(auraStartedCalls, 1);
        expect(hapticCalls, 1);
        expect(find.byKey(const Key('aegis_demo_consent_screen')), findsOneWidget);

        await tester.pumpWidget(const SizedBox.shrink());
        await tester.pumpAndSettle();
      },
    );
  });
}
