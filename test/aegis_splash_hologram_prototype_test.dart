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

    testWidgets(
      '6. Optional FrameTiming diagnostics: disabled by default, captures structural events & batched Top-5 correlation when enabled, cleans up on finish',
      (tester) async {
        // A. Default disabled state must NOT emit any report.
        expect(AegisHologramTransition.defaultEnableFrameTimingDiagnostics, isFalse);
        int defaultReports = 0;
        await tester.pumpWidget(
          MaterialApp(
            home: AegisSplashPrototypeScreen(
              debugSkipVideoInitialization: true,
              debugSimulatedVideoDuration: const Duration(milliseconds: 200),
              onFrameTimingReport: (_) => defaultReports++,
            ),
          ),
        );
        await tester.pump(const Duration(milliseconds: 220));
        await tester.pump(const Duration(milliseconds: 2100));
        await tester.pumpAndSettle();
        expect(defaultReports, 0);

        // B. When enabled, captures all Phase A->B->C structural events and emits once.
        SplashTransitionTimingReport? capturedReport;
        int reportCalls = 0;

        await tester.pumpWidget(
          MaterialApp(
            home: AegisHologramTransition(
              startTransition: true,
              enableFrameTimingDiagnostics: true,
              onFrameTimingReport: (report) {
                reportCalls++;
                capturedReport = report;
              },
              splashChild: const ColoredBox(color: Color(0xFF0A1420)),
              consentChild: const ColoredBox(color: Color(0xFF070B12)),
            ),
          ),
        );

        // Step across Phase A -> Phase B -> Phase C milestones
        await tester.pump(const Duration(milliseconds: 16));
        await tester.pump(const Duration(milliseconds: 140));
        await tester.pump(const Duration(milliseconds: 200));
        await tester.pump(const Duration(milliseconds: 50));
        await tester.pump(const Duration(milliseconds: 1100));
        await tester.pumpAndSettle();

        expect(reportCalls, 1);
        expect(capturedReport, isNotNull);
        final eventLabels =
            capturedReport!.structuralEvents.map((e) => e.label).toList();
        expect(
          eventLabels,
          containsAllInOrder([
            'TRANSITION_START',
            'SHADER_MASK_MOUNT',
            'OPACITY_BELOW_1',
            'PHASE_B_START',
            'SPLASH_LAYER_REMOVE',
            'PHASE_C_START',
          ]),
        );

        // C. Deterministic verification of batched/delayed FrameTiming correlation & Top-5 ranking
        const baseVsync = 100000000;
        final snapshots = [
          const SplashUiSnapshotSample(
            vsyncUs: baseVsync,
            elapsedMs: 0.0,
            pA: 0.0,
            pB: 0.0,
            pC: 0.0,
            events: ['TRANSITION_START'],
          ),
          const SplashUiSnapshotSample(
            vsyncUs: baseVsync + 16000,
            elapsedMs: 16.0,
            pA: 0.042,
            pB: 0.0,
            pC: 0.0,
            events: ['SHADER_MASK_MOUNT'],
          ),
          const SplashUiSnapshotSample(
            vsyncUs: baseVsync + 140000,
            elapsedMs: 140.0,
            pA: 0.368,
            pB: 0.0,
            pC: 0.0,
            events: ['OPACITY_BELOW_1'],
          ),
          const SplashUiSnapshotSample(
            vsyncUs: baseVsync + 385000,
            elapsedMs: 385.0,
            pA: 1.0,
            pB: 0.06,
            pC: 0.0,
            events: ['SPLASH_LAYER_REMOVE'],
          ),
        ];

        final rawTimings = [
          // Pre-transition frame delivered in first batch -> excluded safely
          const SplashRawTimingSample(
            vsyncStartUs: baseVsync - 32000,
            buildMs: 25.0,
            rasterMs: 25.0,
            totalSpanMs: 50.0,
          ),
          // Exact match at SHADER_MASK_MOUNT
          const SplashRawTimingSample(
            vsyncStartUs: baseVsync + 16200,
            buildMs: 9.5,
            rasterMs: 18.2,
            totalSpanMs: 27.7,
          ),
          // Delayed/offset match near OPACITY_BELOW_1
          const SplashRawTimingSample(
            vsyncStartUs: baseVsync + 145000,
            buildMs: 4.1,
            rasterMs: 11.4,
            totalSpanMs: 15.5,
          ),
          // Unmatched timestamp far from any snapshot -> must remain UNCORRELATED
          const SplashRawTimingSample(
            vsyncStartUs: baseVsync + 900000,
            buildMs: 3.0,
            rasterMs: 5.0,
            totalSpanMs: 8.0,
          ),
        ];

        final report = SplashTransitionTimingReport.correlateForTesting(
          rawTimings: rawTimings,
          snapshots: snapshots,
          structuralEvents: capturedReport!.structuralEvents,
          firstTransitionVsyncUs: baseVsync,
        );

        expect(report.preTransitionSkippedFrames, 1);
        expect(report.totalFrames, 3);
        expect(report.phaseABFrames, 2);
        expect(report.uncorrelatedFrames, 1);
        expect(report.uiOver8msCount, 1);
        expect(report.uiOver16msCount, 0);
        expect(report.rasterOver8msCount, 2);
        expect(report.rasterOver16msCount, 1);
        expect(report.uiP95Ms, closeTo(9.5, 0.001));
        expect(report.rasterP95Ms, closeTo(18.2, 0.001));
        expect(report.spanP95Ms, closeTo(27.7, 0.001));
        expect(report.topHeaviestFrames.first.correlationType, 'EXACT_VSYNC');
        expect(
          report.topHeaviestFrames.first.events,
          contains('SHADER_MASK_MOUNT'),
        );
        expect(report.topHeaviestFrames[1].correlationType, 'OFFSET_NEAREST');
        expect(report.topHeaviestFrames.last.phaseLabel, 'UNCORRELATED');
        expect(
          report.formattedSummary,
          contains('Percentiles (p95): UI=9.50ms | Raster=18.20ms | TotalSpan=27.70ms'),
        );
        expect(
          report.formattedSummary,
          contains('Top-3 Heaviest Frames:'),
        );

        final emptyReport = SplashTransitionTimingReport.correlateForTesting(
          rawTimings: const [],
          snapshots: const [],
          structuralEvents: const [],
          firstTransitionVsyncUs: null,
          warmupTimings: const [
            SplashRawTimingSample(
              vsyncStartUs: baseVsync - 5000000,
              buildMs: 1.2,
              rasterMs: 14.5,
              totalSpanMs: 15.7,
            ),
            SplashRawTimingSample(
              vsyncStartUs: baseVsync - 4983000,
              buildMs: 0.7,
              rasterMs: 4.2,
              totalSpanMs: 4.9,
            ),
          ],
          warmupCompletedBeforeTransition: true,
        );
        expect(emptyReport.uiP95Ms, 0.0);
        expect(emptyReport.rasterP95Ms, 0.0);
        expect(emptyReport.spanP95Ms, 0.0);
        expect(emptyReport.warmupFramesCount, 2);
        expect(emptyReport.warmupMaxUiMs, closeTo(1.2, 0.001));
        expect(emptyReport.warmupMaxRasterMs, closeTo(14.5, 0.001));
        expect(emptyReport.warmupCompletedBeforeTransition, isTrue);
        expect(
          emptyReport.formattedSummary,
          contains(
            'Warmup: completed=true, frames=2, maxUI=1.20ms, maxRaster=14.50ms',
          ),
        );

        await tester.pumpWidget(const SizedBox.shrink());
        await tester.pumpAndSettle();
      },
    );

    testWidgets(
      '7. Cold-start 2-frame GPU warmup executes then unmounts cleanly while RepaintBoundary + Opacity/ShaderMask isolate splashChild',
      (WidgetTester tester) async {
        tester.view.physicalSize = const Size(1080, 2400);
        tester.view.devicePixelRatio = 2.0;
        addTearDown(tester.view.resetPhysicalSize);
        addTearDown(tester.view.resetDevicePixelRatio);

        int initCount = 0;
        int disposeCount = 0;
        int completedCount = 0;

        Widget buildHarness({required bool startTransition}) {
          return MaterialApp(
            home: AegisHologramTransition(
              startTransition: startTransition,
              onTransitionCompleted: () => completedCount++,
              splashChild: _SplashLifecycleProbe(
                key: const Key('splash_lifecycle_probe'),
                onInit: () => initCount++,
                onDispose: () => disposeCount++,
              ),
              consentChild: const Scaffold(
                body: Center(child: Text('Destination Ready')),
              ),
            ),
          );
        }

        final warmupFinder =
            find.byKey(const ValueKey<String>('aegis_gpu_warmup_layer'));
        final visibilityFinder =
            find.byKey(const ValueKey<String>('aegis_splash_visibility_layer'));

        // Frame 1 (t = 0ms): 2-frame GPU warmup layer is mounted & occluded, splash layer isolated.
        await tester.pumpWidget(buildHarness(startTransition: false));
        expect(initCount, 1);
        expect(disposeCount, 0);
        expect(warmupFinder, findsOneWidget);
        expect(visibilityFinder, findsOneWidget);
        expect(
          tester.widget<Visibility>(visibilityFinder).visible,
          isTrue,
        );
        expect(
          find.descendant(
            of: visibilityFinder,
            matching: find.byType(RepaintBoundary),
          ),
          findsOneWidget,
        );
        expect(
          find.descendant(
            of: visibilityFinder,
            matching: find.byType(Opacity),
          ),
          findsOneWidget,
        );
        expect(
          find.descendant(
            of: visibilityFinder,
            matching: find.byType(ShaderMask),
          ),
          findsOneWidget,
        );
        expect(
          tester
              .widget<Opacity>(
                find.descendant(
                  of: visibilityFinder,
                  matching: find.byType(Opacity),
                ),
              )
              .opacity,
          closeTo(254.0 / 255.0, 0.0001),
        );

        // Pump Frame 2 + rebuild Frame 3 -> warmup unmounts permanently; video hold returns to fast-path opacity 1.0.
        await tester.pump();
        await tester.pump();
        expect(warmupFinder, findsNothing);
        expect(initCount, 1);
        expect(disposeCount, 0);
        expect(
          tester
              .widget<Opacity>(
                find.descendant(
                  of: visibilityFinder,
                  matching: find.byType(Opacity),
                ),
              )
              .opacity,
          1.0,
        );

        // Start Phase A (t = 5550ms -> +0ms): active OpacityLayer (<1.0) + ShaderMask from frame 0, warmup stays unmounted.
        await tester.pumpWidget(buildHarness(startTransition: true));
        await tester.pump();
        expect(warmupFinder, findsNothing);
        expect(
          tester
              .widget<Opacity>(
                find.descendant(
                  of: visibilityFinder,
                  matching: find.byType(Opacity),
                ),
              )
              .opacity,
          lessThan(1.0),
        );

        // Past edgeFeather > 0.005 (+20ms): no splash subtree recreation.
        await tester.pump(const Duration(milliseconds: 20));
        expect(initCount, 1);
        expect(disposeCount, 0);
        expect(
          find.descendant(
            of: visibilityFinder,
            matching: find.byType(ShaderMask),
          ),
          findsOneWidget,
        );

        // Past pA >= 0.35 (+160ms): continuous Opacity/ShaderMask fade without mid-phase layer switch.
        await tester.pump(const Duration(milliseconds: 140));
        expect(initCount, 1);
        expect(disposeCount, 0);
        expect(
          tester.widget<Visibility>(visibilityFinder).visible,
          isTrue,
        );

        // Past pA == 1.0 (+400ms, mid-Phase B): splash hidden via Visibility(maintainState: true), NOT disposed!
        await tester.pump(const Duration(milliseconds: 240));
        expect(initCount, 1);
        expect(disposeCount, 0);
        expect(completedCount, 0);
        expect(visibilityFinder, findsOneWidget);
        expect(
          tester.widget<Visibility>(visibilityFinder).visible,
          isFalse,
        );

        // Past Phase C start (+1000ms, ClipRect curtain revealed before Visibility in Stack): still not disposed!
        await tester.pump(const Duration(milliseconds: 600));
        expect(initCount, 1);
        expect(disposeCount, 0);
        expect(completedCount, 0);
        expect(visibilityFinder, findsOneWidget);

        // Transition completes (~7500ms): splash subtree is now unmounted & disposed cleanly.
        await tester.pumpAndSettle();
        expect(completedCount, 1);
        expect(initCount, 1);
        expect(disposeCount, 1);
        expect(find.text('Destination Ready'), findsOneWidget);
      },
    );
  });
}

class _SplashLifecycleProbe extends StatefulWidget {
  const _SplashLifecycleProbe({
    super.key,
    required this.onInit,
    required this.onDispose,
  });

  final VoidCallback onInit;
  final VoidCallback onDispose;

  @override
  State<_SplashLifecycleProbe> createState() => _SplashLifecycleProbeState();
}

class _SplashLifecycleProbeState extends State<_SplashLifecycleProbe> {
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
    return const ColoredBox(color: Color(0xFF0A1420));
  }
}
