import 'dart:async';
import 'dart:math' as math;
import 'dart:ui' as ui;

import 'package:flutter/material.dart';
import 'package:flutter/scheduler.dart';
import 'package:flutter/services.dart';
import 'package:video_player/video_player.dart';

/// Isolated standalone prototype application V4 demonstrating:
/// AEGIS Splash Video + Continuous Music + Synchronized CapCut Aura Track -> Energy Collapse -> Volumetric ECG Pulse -> 7500 ms Curtain Reveal -> Demo Consent Screen.
///
/// Does not import or touch VaultService, RootGateScreen, AnamnesisScreen, or backend clients.
class AegisSplashPrototypeApp extends StatelessWidget {
  const AegisSplashPrototypeApp({super.key});

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: 'AEGIS Splash Hologram Prototype V4',
      debugShowCheckedModeBanner: false,
      theme: ThemeData.dark().copyWith(
        scaffoldBackgroundColor: const Color(0xFF070B12),
      ),
      home: const AegisSplashPrototypeScreen(),
    );
  }
}

/// Hosts the MP4 splash video alongside the synchronized user MP3 track (`audio_aura.MP3`),
/// keeps the matrix background continuous across all phases, triggers the holographic
/// transition at [transitionStartOffset] (default 5550 ms) over a 1950 ms transition
/// completing cleanly at 7500 ms, lets both audio tracks complete their natural fade-outs
/// without abrupt cut-offs, and guarantees [AegisDemoConsentScreen] is never mounted prior
/// to Phase C reveal.
class AegisSplashPrototypeScreen extends StatefulWidget {
  const AegisSplashPrototypeScreen({
    super.key,
    this.videoAssetPath = 'assets/video/aegis_splash.mp4',
    this.auraAudioAssetPath = defaultAuraAudioAssetPath,
    this.transitionStartOffset = defaultTransitionStartOffset,
    this.auraTimelineShift = defaultAuraTimelineShift,
    this.destinationChild,
    this.onTransitionCompleted,
    this.enableFrameTimingDiagnostics =
        AegisHologramTransition.defaultEnableFrameTimingDiagnostics,
    this.onFrameTimingReport,
    this.debugSkipVideoInitialization = false,
    this.debugSimulatedVideoDuration,
    this.onRPeakHaptic,
    this.onAuraAudioStarted,
  });

  /// Default start time of the transition after video playback begins (5550 ms).
  /// Combined with [AegisHologramTransition.totalDuration] (1950 ms), total splash = 7500 ms.
  static const Duration defaultTransitionStartOffset =
      Duration(milliseconds: 5550);

  /// User CapCut audio composition (`audio_aura.MP3`) replacing legacy synthesized WAV.
  static const String defaultAuraAudioAssetPath =
      'assets/audio/candidates/audio_aura.MP3';

  /// Verified leading digital silence inside `audio_aura.MP3` (frames 0..226 = 0..5930 ms).
  static const Duration auraLeadingSilenceInFile =
      Duration(milliseconds: 5930);

  /// Target audible start of Magical Aura relative to splash MP4 start (5240 ms).
  static const Duration targetAudibleAuraStart =
      Duration(milliseconds: 5240);

  /// Target global visual R-peak timestamp (~6362 ms from splash MP4 start).
  static const Duration targetRPeakGlobalTime =
      Duration(milliseconds: 6362);

  /// Compensates for the +690 ms extra leading silence baked into `audio_aura.MP3`
  /// (`5930 ms` first non-silent frame vs `5240 ms` target Aura start): pre-seeking
  /// to `690 ms` at `t = 0 ms` places audible Aura onset at `5240 ms`, the heartbeat
  /// window at `6140..7040 ms` with its strike at `~6362 ms` (R-peak), and decay at `7270 ms`.
  static const Duration defaultAuraTimelineShift =
      Duration(milliseconds: -690);

  /// Registered MP4 asset path in pubspec.yaml (`assets/video/aegis_splash.mp4`).
  final String videoAssetPath;

  /// Registered MP3 asset path in pubspec.yaml (`assets/audio/candidates/audio_aura.MP3`).
  final String auraAudioAssetPath;

  /// Configurable offset from playback start to trigger the transition before MP4 ends.
  final Duration transitionStartOffset;

  /// Tunable timeline offset applied when launching [auraAudioAssetPath] relative to video start.
  final Duration auraTimelineShift;

  /// Optional production destination (e.g., `RootGateScreen`) revealed by the holographic
  /// curtains instead of the isolated demo consent screen.
  final Widget? destinationChild;

  /// Optional callback fired once the full splash and transition complete.
  final VoidCallback? onTransitionCompleted;

  /// Enables temporary `FrameTiming` collection during Phase A -> Phase B for
  /// Profile Mode diagnostics. Disabled by default.
  final bool enableFrameTimingDiagnostics;

  /// Optional observer invoked once at transition completion with the
  /// formatted `FrameTiming` summary when [enableFrameTimingDiagnostics] is true.
  final ValueChanged<SplashTransitionTimingReport>? onFrameTimingReport;

  /// Allows deterministic widget tests without platform video plugins.
  final bool debugSkipVideoInitialization;

  /// Optional simulated video duration when [debugSkipVideoInitialization] is true.
  final Duration? debugSimulatedVideoDuration;

  /// Optional haptic callback override for testing; defaults to [HapticFeedback.lightImpact].
  final Future<void> Function()? onRPeakHaptic;

  /// Optional callback fired once per cycle when the synchronized aura track begins playing.
  final VoidCallback? onAuraAudioStarted;

  @override
  State<AegisSplashPrototypeScreen> createState() =>
      _AegisSplashPrototypeScreenState();
}

class _AegisSplashPrototypeScreenState extends State<AegisSplashPrototypeScreen>
    with WidgetsBindingObserver {
  static const Color _matrixBg = Color(0xFF070B12);

  VideoPlayerController? _videoController;
  VideoPlayerController? _auraAudioController;
  Timer? _transitionTriggerTimer;
  Timer? _auraStartTimer;
  Timer? _simulatedVideoTimer;
  Timer? _fallbackHoldTimer;
  Timer? _mediaInitTimeoutTimer;

  bool _isVideoInitialized = false;
  bool _isAuraAudioInitialized = false;
  bool _videoInitFailed = false;
  bool _hasStartedTransition = false;
  bool _pausedVideoByLifecycle = false;
  bool _pausedAuraByLifecycle = false;
  bool _isAppInBackground = false;
  bool _isReplayingSeek = false;
  bool _isDisposed = false;
  bool _isSplashCompleted = false;
  bool _reducedMotionBypassed = false;
  bool _auraAudioStartedInCycle = false;
  bool _auraDriftCorrectedInCycle = false;
  bool _auraCompletionListenerAttached = false;
  int _replayToken = 0;

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addObserver(this);
    _initializeMediaFlow();
  }

  @override
  void didChangeDependencies() {
    super.didChangeDependencies();
    final bool disableAnimations =
        MediaQuery.maybeDisableAnimationsOf(context) ?? false;
    if (widget.destinationChild != null &&
        disableAnimations &&
        !_reducedMotionBypassed) {
      _reducedMotionBypassed = true;
      _hasStartedTransition = true;
      _transitionTriggerTimer?.cancel();
      _auraStartTimer?.cancel();
      _simulatedVideoTimer?.cancel();
      _fallbackHoldTimer?.cancel();
      _mediaInitTimeoutTimer?.cancel();
      unawaited(_releaseMediaControllers(keepAuraBriefly: false));
    }
  }

  Duration _effectiveTriggerOffset(Duration videoDuration) {
    if (videoDuration <= Duration.zero) {
      return widget.transitionStartOffset;
    }
    if (widget.transitionStartOffset < videoDuration) {
      return widget.transitionStartOffset;
    }
    final fallback = videoDuration - const Duration(milliseconds: 45);
    return fallback > Duration.zero ? fallback : Duration.zero;
  }

  void _armTransitionTriggerTimer(VideoPlayerController controller) {
    _transitionTriggerTimer?.cancel();
    if (_hasStartedTransition || !mounted) return;

    final value = controller.value;
    if (value.hasError && !_hasStartedTransition) {
      _startTransitionOnce();
      return;
    }
    if (!value.isInitialized) return;

    final targetOffset = _effectiveTriggerOffset(value.duration);
    final remaining = targetOffset - value.position;
    if (remaining <= Duration.zero) {
      _startTransitionOnce();
      return;
    }

    _transitionTriggerTimer = Timer(remaining, () {
      if (!mounted || _hasStartedTransition || _isReplayingSeek) return;
      if (_isAppInBackground) return;
      _startTransitionOnce();
    });
  }

  Future<void> _safeDisposeController(VideoPlayerController? controller) async {
    if (controller == null) return;
    try {
      await controller.dispose();
    } catch (_) {
      // Ignore platform channel disposal errors when initialization failed or plugin is absent.
    }
  }

  void _onAuraAudioTick() {
    final aura = _auraAudioController;
    if (aura == null || !_isSplashCompleted || _isDisposed) return;
    final val = aura.value;
    final bool finished = val.hasError ||
        val.isCompleted ||
        (val.duration > Duration.zero && val.position >= val.duration) ||
        (!val.isPlaying && val.position > Duration.zero);
    if (finished) {
      unawaited(_releaseMediaControllers(keepAuraBriefly: false));
    }
  }

  Future<void> _releaseMediaControllers({required bool keepAuraBriefly}) async {
    final controller = _videoController;
    _videoController = null;
    if (controller != null) {
      controller.removeListener(_onVideoControllerTick);
      await _safeDisposeController(controller);
    }

    final auraController = _auraAudioController;
    if (auraController != null) {
      final bool canFinishNaturalFade = keepAuraBriefly &&
          _isAuraAudioInitialized &&
          !_isAppInBackground &&
          auraController.value.isInitialized &&
          auraController.value.isPlaying &&
          !auraController.value.isCompleted;
      if (canFinishNaturalFade) {
        if (!_auraCompletionListenerAttached) {
          _auraCompletionListenerAttached = true;
          auraController.addListener(_onAuraAudioTick);
        }
        return;
      }

      if (_auraCompletionListenerAttached) {
        _auraCompletionListenerAttached = false;
        auraController.removeListener(_onAuraAudioTick);
      }
      _auraAudioController = null;
      _isAuraAudioInitialized = false;
      await _safeDisposeController(auraController);
    }
  }

  Future<void> _initializeMediaFlow() async {
    _transitionTriggerTimer?.cancel();
    _auraStartTimer?.cancel();
    _simulatedVideoTimer?.cancel();
    _fallbackHoldTimer?.cancel();
    _mediaInitTimeoutTimer?.cancel();
    _mediaInitTimeoutTimer?.cancel();

    if (_isSplashCompleted || _reducedMotionBypassed) return;

    if (widget.debugSkipVideoInitialization) {
      _armDebugTimers();
      return;
    }

    VideoPlayerController? controller;
    VideoPlayerController? auraController;
    bool timedOut = false;

    void triggerMediaFallback() {
      if (identical(_videoController, controller)) {
        _videoController = null;
      }
      if (identical(_auraAudioController, auraController)) {
        _auraAudioController = null;
      }
      unawaited(_safeDisposeController(controller));
      unawaited(_safeDisposeController(auraController));

      if (!mounted || _isDisposed || _isSplashCompleted || _reducedMotionBypassed) {
        return;
      }
      setState(() {
        _isVideoInitialized = false;
        _videoInitFailed = true;
      });
      if (widget.debugSimulatedVideoDuration != null) {
        _fallbackHoldTimer = Timer(
          widget.debugSimulatedVideoDuration!,
          _startTransitionOnce,
        );
      } else {
        // Real decoder / audio failure must never block startup; proceed immediately
        // to the transition / RootGateScreen fallback.
        _startTransitionOnce();
      }
    }

    try {
      controller = VideoPlayerController.asset(
        widget.videoAssetPath,
        videoPlayerOptions: VideoPlayerOptions(mixWithOthers: true),
      );
      _videoController = controller;

      auraController = VideoPlayerController.asset(
        widget.auraAudioAssetPath,
        videoPlayerOptions: VideoPlayerOptions(mixWithOthers: true),
      );
      _auraAudioController = auraController;

      _mediaInitTimeoutTimer = Timer(const Duration(milliseconds: 2500), () {
        if (_isDisposed || _isSplashCompleted || _reducedMotionBypassed) return;
        timedOut = true;
        triggerMediaFallback();
      });

      // Pre-initialize both splash MP4 and user MP3 in parallel with an explicit
      // cancellable timeout guard so a stalled platform decoder cannot freeze boot.
      await Future.wait([
        controller.initialize(),
        _prepareAuraAudioController(auraController),
      ]);
      _mediaInitTimeoutTimer?.cancel();

      if (timedOut ||
          !mounted ||
          _isDisposed ||
          _isSplashCompleted ||
          _reducedMotionBypassed) {
        if (identical(_videoController, controller)) {
          _videoController = null;
        }
        if (identical(_auraAudioController, auraController)) {
          _auraAudioController = null;
        }
        unawaited(_safeDisposeController(controller));
        unawaited(_safeDisposeController(auraController));
        return;
      }

      if (!controller.value.isInitialized || controller.value.hasError) {
        throw StateError('Splash video controller did not initialize cleanly');
      }

      await controller.setLooping(false);
      await controller.setVolume(0.86);
      if (timedOut ||
          !mounted ||
          _isDisposed ||
          _isSplashCompleted ||
          _reducedMotionBypassed) {
        await _releaseMediaControllers(keepAuraBriefly: false);
        return;
      }
      controller.addListener(_onVideoControllerTick);
      setState(() {
        _isVideoInitialized = true;
        _videoInitFailed = false;
      });
      if (_isAppInBackground) {
        _pausedVideoByLifecycle = true;
        return;
      }
      await controller.play();
      if (!timedOut &&
          mounted &&
          !_isDisposed &&
          !_isAppInBackground &&
          !_isSplashCompleted &&
          !_reducedMotionBypassed) {
        _armAuraStartTimer(controller);
        if (!_hasStartedTransition) {
          _armTransitionTriggerTimer(controller);
        }
      }
    } catch (_) {
      _mediaInitTimeoutTimer?.cancel();
      if (!timedOut) {
        triggerMediaFallback();
      }
    }
  }

  void _armDebugTimers() {
    _auraStartTimer?.cancel();
    _simulatedVideoTimer?.cancel();
    if (!_auraAudioStartedInCycle && !_isAppInBackground) {
      final auraDelay = widget.auraTimelineShift > Duration.zero
          ? widget.auraTimelineShift
          : Duration.zero;
      _auraStartTimer = Timer(auraDelay, () {
        unawaited(_startAuraAudioOnce());
      });
    }
    if (widget.debugSimulatedVideoDuration != null && !_hasStartedTransition) {
      _simulatedVideoTimer = Timer(
        widget.debugSimulatedVideoDuration!,
        _startTransitionOnce,
      );
    }
  }

  Duration _initialAuraSeekOffset(Duration auraDuration) {
    if (widget.auraTimelineShift >= Duration.zero) {
      return Duration.zero;
    }
    final seekStart = -widget.auraTimelineShift;
    if (auraDuration > Duration.zero && seekStart >= auraDuration) {
      return Duration.zero;
    }
    return seekStart;
  }

  Future<void> _prepareAuraAudioController(
    VideoPlayerController audioController,
  ) async {
    try {
      await audioController.initialize();
      await audioController.setLooping(false);
      await audioController.setVolume(0.94);
      final seekStart = _initialAuraSeekOffset(audioController.value.duration);
      if (seekStart > Duration.zero) {
        await audioController.seekTo(seekStart);
      }
      if (!mounted || _isDisposed || _isSplashCompleted) {
        if (identical(_auraAudioController, audioController)) {
          _auraAudioController = null;
          await _safeDisposeController(audioController);
        }
        return;
      }
      _isAuraAudioInitialized = true;
    } catch (_) {
      if (identical(_auraAudioController, audioController)) {
        _auraAudioController = null;
        await _safeDisposeController(audioController);
      }
      _isAuraAudioInitialized = false;
    }
  }

  void _armAuraStartTimer(VideoPlayerController videoController) {
    _auraStartTimer?.cancel();
    if (_auraAudioStartedInCycle || _isAppInBackground || !mounted || _isDisposed) {
      return;
    }

    final targetOffset = widget.auraTimelineShift > Duration.zero
        ? widget.auraTimelineShift
        : Duration.zero;
    final remaining = targetOffset - videoController.value.position;
    if (remaining <= Duration.zero) {
      unawaited(_startAuraAudioOnce());
      return;
    }

    _auraStartTimer = Timer(remaining, () {
      unawaited(_startAuraAudioOnce());
    });
  }

  Future<void> _startAuraAudioOnce() async {
    if (_auraAudioStartedInCycle ||
        _isAppInBackground ||
        _isSplashCompleted ||
        _reducedMotionBypassed ||
        !mounted ||
        _isDisposed) {
      return;
    }
    _auraAudioStartedInCycle = true;
    widget.onAuraAudioStarted?.call();

    final audioController = _auraAudioController;
    if (audioController != null && _isAuraAudioInitialized) {
      try {
        final seekStart = _initialAuraSeekOffset(audioController.value.duration);
        final posDelta = (audioController.value.position - seekStart).abs();
        if (posDelta > const Duration(milliseconds: 25)) {
          await audioController.seekTo(seekStart);
        }
        if (!_isAppInBackground && mounted && !_isDisposed) {
          await audioController.play();
        }
      } catch (_) {
        // Keep the visual splash and original MP4 playback resilient.
      }
    }
  }

  void _onVideoControllerTick() {
    final controller = _videoController;
    if (controller == null || !mounted || _isDisposed || _isReplayingSeek) {
      return;
    }

    final value = controller.value;
    if (value.hasError && !_hasStartedTransition) {
      _startTransitionOnce();
      return;
    }
    if (!value.isInitialized) return;
    final duration = value.duration;
    if (duration <= Duration.zero) return;

    // Ensure user MP3 starts once video reaches auraTimelineShift even if timer slipped.
    final triggerAt = widget.auraTimelineShift > Duration.zero
        ? widget.auraTimelineShift
        : Duration.zero;
    if (!_auraAudioStartedInCycle &&
        !_isAppInBackground &&
        value.isPlaying &&
        value.position >= triggerAt) {
      _auraStartTimer?.cancel();
      unawaited(_startAuraAudioOnce());
    }

    // One-time silent-region sync correction (1.2s..4.5s video time, well inside
    // leading silence before 5240ms audible Aura onset).
    final auraController = _auraAudioController;
    if (_auraAudioStartedInCycle &&
        !_auraDriftCorrectedInCycle &&
        _isAuraAudioInitialized &&
        auraController != null &&
        auraController.value.isInitialized &&
        auraController.value.isPlaying &&
        value.isPlaying &&
        value.position >= const Duration(milliseconds: 1200) &&
        value.position <= const Duration(milliseconds: 4500)) {
      final expectedAuraPos = value.position - widget.auraTimelineShift;
      if (expectedAuraPos >= Duration.zero &&
          expectedAuraPos < auraController.value.duration) {
        final driftMs =
            (auraController.value.position.inMilliseconds -
                    expectedAuraPos.inMilliseconds)
                .abs();
        _auraDriftCorrectedInCycle = true;
        if (driftMs > 40) {
          unawaited(auraController.seekTo(expectedAuraPos));
        }
      }
    }

    if (_hasStartedTransition) {
      return;
    }

    // Prevent spurious pre-playback completion triggers before position advances.
    if (value.position < const Duration(milliseconds: 250) && !value.isCompleted) {
      return;
    }

    final targetOffset = _effectiveTriggerOffset(duration);
    final reachedTargetOffset = value.position >= targetOffset;
    final remaining = duration - value.position;
    final endedEarlyOnDevice =
        value.isCompleted ||
        (!value.isPlaying && remaining <= const Duration(milliseconds: 120));

    if (reachedTargetOffset || endedEarlyOnDevice) {
      // Keep the controller playing during Phase A so the live frame merges seamlessly
      // into the central beam without an intermediate pause or black frame.
      _startTransitionOnce();
    }
  }

  void _startTransitionOnce() {
    if (!mounted || _hasStartedTransition) return;
    _transitionTriggerTimer?.cancel();
    setState(() {
      _hasStartedTransition = true;
    });
  }

  void _onPhaseACompleted() {
    // Intentionally keep both _videoController and _auraAudioController playing so
    // the original MP4 music tail fades out smoothly without interruption.
  }

  void _onTransitionCompleted() {
    if (_isSplashCompleted) return;
    _isSplashCompleted = true;
    widget.onTransitionCompleted?.call();
    if (widget.destinationChild != null) {
      _transitionTriggerTimer?.cancel();
      _auraStartTimer?.cancel();
      _simulatedVideoTimer?.cancel();
      _fallbackHoldTimer?.cancel();
      unawaited(_releaseMediaControllers(keepAuraBriefly: true));
    }
  }

  Future<void> _stopAndResetAuraAudio() async {
    _auraAudioStartedInCycle = false;
    _auraDriftCorrectedInCycle = false;
    _pausedAuraByLifecycle = false;
    final audioController = _auraAudioController;
    if (audioController != null && _isAuraAudioInitialized) {
      try {
        if (audioController.value.isPlaying) {
          await audioController.pause();
        }
        final seekStart = _initialAuraSeekOffset(audioController.value.duration);
        if ((audioController.value.position - seekStart).abs() >
            const Duration(milliseconds: 25)) {
          await audioController.seekTo(seekStart);
        }
      } catch (_) {
        // Ignore platform reset issues so replay proceeds cleanly.
      }
    }
  }

  Future<void> _replayDemo() async {
    _transitionTriggerTimer?.cancel();
    _auraStartTimer?.cancel();
    _simulatedVideoTimer?.cancel();
    _fallbackHoldTimer?.cancel();
    _mediaInitTimeoutTimer?.cancel();
    if (!mounted || _isDisposed) return;

    await _stopAndResetAuraAudio();
    if (!mounted || _isDisposed) return;

    final controller = _videoController;
    if (controller != null && _isVideoInitialized) {
      _isReplayingSeek = true;
      try {
        await controller.pause();
        await controller.seekTo(Duration.zero);
        if (!mounted || _isDisposed) return;
        setState(() {
          _hasStartedTransition = false;
          _isSplashCompleted = false;
          _pausedVideoByLifecycle = false;
          _pausedAuraByLifecycle = false;
          _auraAudioStartedInCycle = false;
          _auraDriftCorrectedInCycle = false;
          _replayToken++;
        });
      } finally {
        _isReplayingSeek = false;
      }
      if (mounted && !_isDisposed && !_isAppInBackground) {
        await controller.play();
        if (mounted && !_isDisposed && !_isAppInBackground) {
          _armAuraStartTimer(controller);
          if (!_hasStartedTransition) {
            _armTransitionTriggerTimer(controller);
          }
        }
      }
      return;
    }

    setState(() {
      _hasStartedTransition = false;
      _isSplashCompleted = false;
      _pausedVideoByLifecycle = false;
      _pausedAuraByLifecycle = false;
      _auraAudioStartedInCycle = false;
      _auraDriftCorrectedInCycle = false;
      _replayToken++;
    });

    if (widget.debugSkipVideoInitialization) {
      _armDebugTimers();
    } else if (_videoInitFailed) {
      final holdDuration =
          widget.debugSimulatedVideoDuration ?? const Duration(milliseconds: 650);
      _fallbackHoldTimer = Timer(holdDuration, _startTransitionOnce);
    }
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    if (_isSplashCompleted || _reducedMotionBypassed) {
      if (state == AppLifecycleState.paused ||
          state == AppLifecycleState.hidden ||
          state == AppLifecycleState.detached) {
        _isAppInBackground = true;
        unawaited(_releaseMediaControllers(keepAuraBriefly: false));
      } else if (state == AppLifecycleState.resumed) {
        _isAppInBackground = false;
      }
      return;
    }

    final controller = _videoController;
    final auraController = _auraAudioController;

    switch (state) {
      case AppLifecycleState.inactive:
      case AppLifecycleState.paused:
      case AppLifecycleState.hidden:
      case AppLifecycleState.detached:
        _isAppInBackground = true;
        _transitionTriggerTimer?.cancel();
        _auraStartTimer?.cancel();
        if (controller != null &&
            _isVideoInitialized &&
            controller.value.isPlaying) {
          _pausedVideoByLifecycle = true;
          unawaited(controller.pause());
        }
        if (auraController != null &&
            _isAuraAudioInitialized &&
            auraController.value.isPlaying) {
          _pausedAuraByLifecycle = true;
          unawaited(auraController.pause());
        }
        break;
      case AppLifecycleState.resumed:
        _isAppInBackground = false;
        if (_pausedVideoByLifecycle &&
            controller != null &&
            _isVideoInitialized) {
          _pausedVideoByLifecycle = false;
          if (!controller.value.isCompleted) {
            unawaited(controller.play());
          }
          if (!_hasStartedTransition) {
            _armTransitionTriggerTimer(controller);
          }
          if (!_auraAudioStartedInCycle) {
            _armAuraStartTimer(controller);
          }
        }
        if (_pausedAuraByLifecycle &&
            auraController != null &&
            _isAuraAudioInitialized) {
          _pausedAuraByLifecycle = false;
          if (!auraController.value.isCompleted) {
            unawaited(auraController.play());
          }
        } else if (widget.debugSkipVideoInitialization &&
            !_auraAudioStartedInCycle) {
          _armDebugTimers();
        }
        break;
    }
  }

  @override
  void dispose() {
    _isDisposed = true;
    WidgetsBinding.instance.removeObserver(this);
    _transitionTriggerTimer?.cancel();
    _auraStartTimer?.cancel();
    _simulatedVideoTimer?.cancel();
    _fallbackHoldTimer?.cancel();
    _mediaInitTimeoutTimer?.cancel();
    final controller = _videoController;
    _videoController = null;
    if (controller != null) {
      controller.removeListener(_onVideoControllerTick);
      unawaited(_safeDisposeController(controller));
    }
    final auraController = _auraAudioController;
    _auraAudioController = null;
    if (auraController != null) {
      if (_auraCompletionListenerAttached) {
        _auraCompletionListenerAttached = false;
        auraController.removeListener(_onAuraAudioTick);
      }
      unawaited(_safeDisposeController(auraController));
    }
    super.dispose();
  }

  Widget _buildSplashVisual() {
    final controller = _videoController;
    if (controller != null && _isVideoInitialized) {
      final rawSize = controller.value.size;
      final videoSize = rawSize.isEmpty ? const Size(1080, 1920) : rawSize;
      return SizedBox.expand(
        child: FittedBox(
          fit: BoxFit.cover,
          alignment: Alignment.center,
          child: SizedBox(
            width: videoSize.width,
            height: videoSize.height,
            child: VideoPlayer(controller),
          ),
        ),
      );
    }

    if (_videoInitFailed || widget.debugSkipVideoInitialization) {
      return const _AegisFallbackHoldFrame();
    }

    // Continuous matrix background while video initializes — zero dead black screen.
    return const _ContinuousMatrixBackground();
  }

  @override
  Widget build(BuildContext context) {
    final transition = AegisHologramTransition(
      key: ValueKey<int>(_replayToken),
      startTransition: _hasStartedTransition,
      onRPeakHaptic: widget.onRPeakHaptic,
      onPhaseACompleted: _onPhaseACompleted,
      onTransitionCompleted: _onTransitionCompleted,
      splashChild: Material(
        type: MaterialType.transparency,
        child: _buildSplashVisual(),
      ),
      enableFrameTimingDiagnostics: widget.enableFrameTimingDiagnostics,
      onFrameTimingReport: widget.onFrameTimingReport,
      consentChild: widget.destinationChild ??
          AegisDemoConsentScreen(
            onReplayPressed: _replayDemo,
          ),
    );

    if (widget.destinationChild != null) {
      return RepaintBoundary(
        child: ColoredBox(
          color: _matrixBg,
          child: transition,
        ),
      );
    }

    return Scaffold(
      backgroundColor: _matrixBg,
      body: transition,
    );
  }
}

/// Persistent dark matrix backdrop shared across splash hold, collapse, ECG,
/// shutters, and demo consent screen so luminance/grid lines remain unbroken.
class _ContinuousMatrixBackground extends StatelessWidget {
  const _ContinuousMatrixBackground();

  @override
  Widget build(BuildContext context) {
    return const SizedBox.expand(
      child: ColoredBox(
        color: Color(0xFF070B12),
        child: CustomPaint(
          painter: _MatrixBackdropPainter(),
        ),
      ),
    );
  }
}

class _MatrixBackdropPainter extends CustomPainter {
  const _MatrixBackdropPainter();

  @override
  void paint(Canvas canvas, Size size) {
    if (size.isEmpty) return;

    final center = Offset(size.width * 0.5, size.height * 0.5);
    final radialShader = ui.Gradient.radial(
      center,
      size.height * 0.45,
      [
        const Color(0xFF00E5FF).withValues(alpha: 0.065),
        const Color(0xFF00E5FF).withValues(alpha: 0.02),
        const Color(0xFF070B12).withValues(alpha: 0.0),
      ],
      const [0.0, 0.42, 1.0],
    );
    canvas.drawRect(
      Offset.zero & size,
      Paint()..shader = radialShader,
    );

    final linePaint = Paint()
      ..color = const Color(0xFF00E5FF).withValues(alpha: 0.04)
      ..strokeWidth = 1.0;
    const spacing = 20.0;
    for (double y = 0; y < size.height; y += spacing) {
      canvas.drawLine(Offset(0, y), Offset(size.width, y), linePaint);
    }
  }

  @override
  bool shouldRepaint(covariant _MatrixBackdropPainter oldDelegate) => false;
}

/// Static dark AEGIS hold frame used when platform video decoding is bypassed
/// in widget tests or unavailable.
class _AegisFallbackHoldFrame extends StatelessWidget {
  const _AegisFallbackHoldFrame();

  @override
  Widget build(BuildContext context) {
    return Stack(
      key: const Key('aegis_splash_fallback_frame'),
      fit: StackFit.expand,
      children: [
        const _ContinuousMatrixBackground(),
        Center(
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              Container(
                width: 76,
                height: 76,
                decoration: BoxDecoration(
                  shape: BoxShape.circle,
                  border: Border.all(
                    color: const Color(0xFF00E5FF).withValues(alpha: 0.8),
                    width: 1.5,
                  ),
                  boxShadow: [
                    BoxShadow(
                      color: const Color(0xFF00E5FF).withValues(alpha: 0.3),
                      blurRadius: 26,
                      spreadRadius: 3,
                    ),
                  ],
                ),
                child: const Icon(
                  Icons.monitor_heart_outlined,
                  color: Color(0xFF00E5FF),
                  size: 36,
                ),
              ),
              const SizedBox(height: 18),
              const Text(
                'AEGIS',
                style: TextStyle(
                  color: Color(0xFFE8FFFF),
                  fontSize: 24,
                  fontWeight: FontWeight.w600,
                  letterSpacing: 8,
                ),
              ),
            ],
          ),
        ),
      ],
    );
  }
}

/// Coordinates the 1.95s V4 three-phase holographic transition:
/// - Phase A (0.00-0.38s): Optical energy collapse to central glowing cyan beam
/// - Phase B (0.32-1.40s, 1.08s duration): Volumetric P-Q-R-S-T ECG pulse with
///   multi-layer glow, long comet tail, horizontal R-peak anamorphic flare & single haptic
/// - Phase C (1.38-1.95s, 0.57s duration): Holographic curtain reveal traveling all the way to
///   top and bottom screen edges before final fade-out, completing splash at 7500 ms
class AegisHologramTransition extends StatefulWidget {
  const AegisHologramTransition({
    super.key,
    required this.startTransition,
    required this.splashChild,
    required this.consentChild,
    this.onTransitionCompleted,
    this.onPhaseACompleted,
    this.onRPeakHaptic,
    this.enableFrameTimingDiagnostics = defaultEnableFrameTimingDiagnostics,
    this.onFrameTimingReport,
  });

  static const Duration totalDuration = Duration(milliseconds: 1950);
  static const double totalSeconds = 1.95;
  static const double phaseAEndSeconds = 0.38;
  static const double phaseBStartSeconds = 0.32;
  static const double phaseBEndSeconds = 1.40;
  static const double phaseBDurationSeconds =
      phaseBEndSeconds - phaseBStartSeconds; // 1.08s
  static const double phaseCStartSeconds = 1.38;
  static const double phaseCDurationSeconds =
      totalSeconds - phaseCStartSeconds; // 0.57s (-100ms from V3)
  static const bool hasAnamorphicRPeakFlare = true;

  /// Compile-time switch (`--dart-define=AEGIS_SPLASH_PROFILE_TIMINGS=true`)
  /// enabling one-shot Phase A -> B FrameTiming diagnostics in Profile Mode.
  static const bool defaultEnableFrameTimingDiagnostics = bool.fromEnvironment(
    'AEGIS_SPLASH_PROFILE_TIMINGS',
    defaultValue: false,
  );

  /// Flips from false -> true once to trigger the transition cleanly once.
  final bool startTransition;

  /// Live splash video frame (or fallback emblem) to collapse into beam in Phase A.
  final Widget splashChild;

  /// Destination screen revealed exclusively when Phase C begins.
  final Widget consentChild;

  /// Optional completion callback fired once the 1.95s animation completes.
  final VoidCallback? onTransitionCompleted;

  /// Optional callback fired once Phase A finishes merging video into the central beam.
  final VoidCallback? onPhaseACompleted;

  /// Optional override for R-peak haptic trigger (defaults to [HapticFeedback.lightImpact]).
  final Future<void> Function()? onRPeakHaptic;

  /// Optional Profile Mode FrameTiming diagnostics flag.
  final bool enableFrameTimingDiagnostics;

  /// Optional callback invoked once with the completed transition timing summary.
  final ValueChanged<SplashTransitionTimingReport>? onFrameTimingReport;

  /// Computes exact normalized arc-length fraction [0..1] along the full ECG path
  /// where the moving pulse head reaches the R-peak apex.
  static double computeRPeakArcRatio(Size size) {
    return _EcgGeometryCache.compute(size).rPeakArcRatio;
  }

  /// Computes the width of the horizontal anamorphic R-peak flare for a given progress [0..1].
  static double computeAnamorphicFlareWidth(Size size, double flareProgress) {
    final t = Curves.easeOutCubic.transform(flareProgress.clamp(0.0, 1.0));
    return ui.lerpDouble(size.width * 0.18, size.width * 0.48, t)!;
  }

  @override
  State<AegisHologramTransition> createState() =>
      _AegisHologramTransitionState();
}

class _AegisHologramTransitionState extends State<AegisHologramTransition>
    with SingleTickerProviderStateMixin, WidgetsBindingObserver {
  late final AnimationController _controller;
  final GlobalKey _splashSubtreeKey =
      GlobalKey(debugLabel: 'aegis_splash_visual_subtree');
  final GlobalKey _consentSubtreeKey =
      GlobalKey(debugLabel: 'aegis_splash_destination_subtree');

  bool _hasStarted = false;
  bool _phaseACompleteNotified = false;
  bool _hasCompleted = false;
  bool _scheduledDisableAnimComplete = false;
  bool _rPeakHapticTriggered = false;
  bool _wasAnimatingOnPause = false;
  bool _isAppForeground = true;
  Size? _lastLayoutSize;

  // Short-lived 2-frame GPU warm-up executed during splash start (occluded behind
  // _ContinuousMatrixBackground) and unmounted permanently once complete.
  static const int _targetWarmupFrames = 2;
  int _warmupFramesRendered = 0;
  bool _gpuWarmupCompleted = false;
  bool _warmupPostFrameScheduled = false;
  bool _warmupCompletedBeforeTransition = false;
  final List<FrameTiming> _warmupCollectedTimings = [];

  @visibleForTesting
  bool get isGpuWarmupCompleted => _gpuWarmupCompleted;

  // Optional FrameTiming diagnostics (inactive unless enableFrameTimingDiagnostics == true)
  static const int _maxCapturedTimingEntries = 512;
  bool _timingsCallbackRegistered = false;
  int? _firstTransitionVsyncUs;
  int _recordedEventFlags = 0;
  final List<_UiTransitionFrameSnapshot> _uiFrameSnapshots = [];
  final List<SplashTransitionEvent> _structuralEvents = [];
  final List<FrameTiming> _collectedTimings = [];

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addObserver(this);
    _controller = AnimationController(
      vsync: this,
      duration: AegisHologramTransition.totalDuration,
    )
      ..addListener(_handleAnimationTick)
      ..addStatusListener(_handleAnimationStatus);

    if (widget.enableFrameTimingDiagnostics) {
      _startDiagnosticsIfEnabled();
    }

    if (widget.startTransition) {
      _triggerStartIfNeeded();
    }
  }

  void _scheduleWarmupTickIfNeeded() {
    if (_gpuWarmupCompleted || _hasStarted || _warmupPostFrameScheduled || !mounted) {
      return;
    }
    _warmupPostFrameScheduled = true;
    WidgetsBinding.instance.addPostFrameCallback((_) {
      _warmupPostFrameScheduled = false;
      if (!mounted || _gpuWarmupCompleted || _hasStarted) return;
      _warmupFramesRendered++;
      if (_warmupFramesRendered >= _targetWarmupFrames) {
        setState(() {
          _gpuWarmupCompleted = true;
        });
      } else {
        setState(() {});
      }
    });
  }

  @override
  void didUpdateWidget(covariant AegisHologramTransition oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (widget.startTransition && !oldWidget.startTransition) {
      _triggerStartIfNeeded();
    } else if (!widget.startTransition && oldWidget.startTransition) {
      _stopAndClearDiagnostics(emitReport: false);
      _controller.stop();
      _controller.reset();
      _hasStarted = false;
      _phaseACompleteNotified = false;
      _hasCompleted = false;
      _scheduledDisableAnimComplete = false;
      _rPeakHapticTriggered = false;
      _wasAnimatingOnPause = false;
      if (widget.enableFrameTimingDiagnostics) {
        _startDiagnosticsIfEnabled();
      }
    }
  }

  void _startDiagnosticsIfEnabled() {
    if (!widget.enableFrameTimingDiagnostics || _timingsCallbackRegistered) {
      return;
    }
    _clearDiagnosticBuffers();
    _timingsCallbackRegistered = true;
    SchedulerBinding.instance.addTimingsCallback(_onFrameTimings);
  }

  void _onFrameTimings(List<FrameTiming> timings) {
    if (!widget.enableFrameTimingDiagnostics ||
        !_timingsCallbackRegistered ||
        _hasCompleted) {
      return;
    }
    for (final timing in timings) {
      if (!_hasStarted) {
        if (_warmupCollectedTimings.length < 8) {
          _warmupCollectedTimings.add(timing);
        }
        continue;
      }
      if (_collectedTimings.length >= _maxCapturedTimingEntries) {
        break;
      }
      _collectedTimings.add(timing);
    }
  }

  void _clearDiagnosticBuffers() {
    _firstTransitionVsyncUs = null;
    _recordedEventFlags = 0;
    _uiFrameSnapshots.clear();
    _structuralEvents.clear();
    _collectedTimings.clear();
    _warmupCollectedTimings.clear();
  }

  void _stopAndClearDiagnostics({required bool emitReport}) {
    if (_timingsCallbackRegistered) {
      SchedulerBinding.instance.removeTimingsCallback(_onFrameTimings);
      _timingsCallbackRegistered = false;
    }
    if (emitReport && widget.enableFrameTimingDiagnostics) {
      final report = SplashTransitionTimingReport._fromCaptured(
        timings: _collectedTimings,
        snapshots: _uiFrameSnapshots,
        structuralEvents: _structuralEvents,
        firstTransitionVsyncUs: _firstTransitionVsyncUs,
        warmupTimings: _warmupCollectedTimings,
        warmupCompletedBeforeTransition: _warmupCompletedBeforeTransition,
      );
      debugPrint(report.formattedSummary);
      widget.onFrameTimingReport?.call(report);
    }
    _clearDiagnosticBuffers();
  }

  void _recordUiTransitionSnapshot({
    required double elapsedMs,
    required double pA,
    required double pB,
    required double pC,
    required bool hasShaderMask,
    required bool hasOpacityBelowOne,
    required bool splashLayerRemoved,
    required bool phaseCRevealStarted,
  }) {
    if (!widget.enableFrameTimingDiagnostics || !_hasStarted || _hasCompleted) {
      return;
    }
    final vsyncUs =
        SchedulerBinding.instance.currentSystemFrameTimeStamp.inMicroseconds;
    _firstTransitionVsyncUs ??= vsyncUs;

    final List<String> frameEvents = [];
    void checkEvent(int bit, String label, bool condition) {
      if (condition && (_recordedEventFlags & bit) == 0) {
        _recordedEventFlags |= bit;
        frameEvents.add(label);
        _structuralEvents.add(
          SplashTransitionEvent(
            label: label,
            elapsedMs: elapsedMs,
            pA: pA,
            pB: pB,
            pC: pC,
          ),
        );
      }
    }

    checkEvent(1 << 0, 'TRANSITION_START', _hasStarted);
    checkEvent(1 << 1, 'SHADER_MASK_MOUNT', hasShaderMask);
    checkEvent(1 << 2, 'OPACITY_BELOW_1', hasOpacityBelowOne);
    checkEvent(1 << 3, 'PHASE_B_START', pB > 0.0);
    checkEvent(1 << 4, 'SPLASH_LAYER_REMOVE', splashLayerRemoved);
    checkEvent(1 << 5, 'PHASE_C_START', phaseCRevealStarted);

    if (_uiFrameSnapshots.length < _maxCapturedTimingEntries) {
      _uiFrameSnapshots.add(
        _UiTransitionFrameSnapshot(
          vsyncUs: vsyncUs,
          elapsedMs: elapsedMs,
          pA: pA,
          pB: pB,
          pC: pC,
          events: List.unmodifiable(frameEvents),
        ),
      );
    }
  }

  void _triggerStartIfNeeded() {
    if (_hasStarted || !mounted) return;
    _warmupCompletedBeforeTransition = _gpuWarmupCompleted;
    _hasStarted = true;
    _phaseACompleteNotified = false;
    _rPeakHapticTriggered = false;
    _startDiagnosticsIfEnabled();
    if (_isAppForeground) {
      _controller.forward(from: 0.0);
    } else {
      _wasAnimatingOnPause = true;
    }
  }

  void _handleAnimationTick() {
    if (!_hasStarted) return;

    final elapsedSec = _controller.value * AegisHologramTransition.totalSeconds;

    if (!_phaseACompleteNotified &&
        elapsedSec >= AegisHologramTransition.phaseAEndSeconds) {
      _phaseACompleteNotified = true;
      widget.onPhaseACompleted?.call();
    }

    if (_rPeakHapticTriggered ||
        elapsedSec < AegisHologramTransition.phaseBStartSeconds) {
      return;
    }

    final pB = ((elapsedSec - AegisHologramTransition.phaseBStartSeconds) /
            AegisHologramTransition.phaseBDurationSeconds)
        .clamp(0.0, 1.0);

    final size = _lastLayoutSize;
    if (size == null || size.isEmpty) return;

    final rPeakArcRatio = _EcgGeometryCache.forSize(size).rPeakArcRatio;
    if (pB >= rPeakArcRatio) {
      // Guarded against repeated frame builds so haptic feedback fires at most once.
      _rPeakHapticTriggered = true;
      if (_isAppForeground) {
        final hapticCallback = widget.onRPeakHaptic ?? HapticFeedback.lightImpact;
        unawaited(hapticCallback());
      }
    }
  }

  void _handleAnimationStatus(AnimationStatus status) {
    if (status == AnimationStatus.completed && !_hasCompleted) {
      if (!mounted) return;
      _stopAndClearDiagnostics(emitReport: true);
      setState(() {
        _hasCompleted = true;
      });
      widget.onTransitionCompleted?.call();
    }
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    switch (state) {
      case AppLifecycleState.inactive:
      case AppLifecycleState.paused:
      case AppLifecycleState.hidden:
      case AppLifecycleState.detached:
        _isAppForeground = false;
        if (_controller.isAnimating) {
          _wasAnimatingOnPause = true;
          _controller.stop(canceled: false);
        }
        break;
      case AppLifecycleState.resumed:
        _isAppForeground = true;
        if (_wasAnimatingOnPause &&
            _hasStarted &&
            !_hasCompleted &&
            mounted) {
          _wasAnimatingOnPause = false;
          _controller.forward();
        }
        break;
    }
  }

  @override
  void dispose() {
    _stopAndClearDiagnostics(emitReport: false);
    WidgetsBinding.instance.removeObserver(this);
    _controller.removeListener(_handleAnimationTick);
    _controller.removeStatusListener(_handleAnimationStatus);
    _controller.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final bool disableAnimations =
        MediaQuery.maybeDisableAnimationsOf(context) ?? false;
    if (disableAnimations &&
        (widget.startTransition || _hasStarted) &&
        !_hasCompleted) {
      _stopAndClearDiagnostics(emitReport: false);
      _controller.stop();
      if (!_scheduledDisableAnimComplete) {
        _scheduledDisableAnimComplete = true;
        WidgetsBinding.instance.addPostFrameCallback((_) {
          if (!mounted || _hasCompleted) return;
          setState(() {
            _hasCompleted = true;
          });
          widget.onTransitionCompleted?.call();
        });
      }
      return KeyedSubtree(
        key: _consentSubtreeKey,
        child: widget.consentChild,
      );
    }

    return LayoutBuilder(
      builder: (context, constraints) {
        final layoutSize = Size(constraints.maxWidth, constraints.maxHeight);
        _lastLayoutSize = layoutSize;
        if (!layoutSize.isEmpty) {
          _EcgGeometryCache.forSize(layoutSize);
        }

        if (_hasCompleted) {
          return KeyedSubtree(
            key: _consentSubtreeKey,
            child: widget.consentChild,
          );
        }

        _scheduleWarmupTickIfNeeded();

        return AnimatedBuilder(
          animation: _controller,
          builder: (context, _) {
            final t = _controller.value;
            final elapsedSec = t * AegisHologramTransition.totalSeconds;

            final pA = _hasStarted
                ? (elapsedSec / AegisHologramTransition.phaseAEndSeconds)
                    .clamp(0.0, 1.0)
                : 0.0;

            final pB = (_hasStarted &&
                    elapsedSec >= AegisHologramTransition.phaseBStartSeconds)
                ? ((elapsedSec - AegisHologramTransition.phaseBStartSeconds) /
                        AegisHologramTransition.phaseBDurationSeconds)
                    .clamp(0.0, 1.0)
                : 0.0;

            final pC = (_hasStarted &&
                    elapsedSec >= AegisHologramTransition.phaseCStartSeconds)
                ? Curves.easeInOutCubic.transform(
                    ((elapsedSec - AegisHologramTransition.phaseCStartSeconds) /
                            (AegisHologramTransition.totalSeconds -
                                AegisHologramTransition.phaseCStartSeconds))
                        .clamp(0.0, 1.0),
                  )
                : 0.0;

            // Smooth feathered energy convergence from live video frame into horizontal beam.
            final double scaleY = _hasStarted
                ? ui.lerpDouble(
                    1.0,
                    0.025,
                    Curves.easeInOutCubic.transform(pA),
                  )!
                : 1.0;
            final double scaleX = _hasStarted
                ? ui.lerpDouble(
                    1.0,
                    1.05,
                    Curves.easeOutQuad.transform(pA),
                  )!
                : 1.0;
            final double rawSplashOpacity = _hasStarted
                ? (1.0 -
                        const Interval(0.35, 0.98, curve: Curves.easeInOut)
                            .transform(pA))
                    .clamp(0.0, 1.0)
                : 1.0;
            final double splashOpacity = !_hasStarted
                ? (!_gpuWarmupCompleted ? 254.0 / 255.0 : 1.0)
                : math.min(254.0 / 255.0, rawSplashOpacity);
            final double rawEdgeFeather = _hasStarted
                ? 0.44 * Curves.easeOutQuad.transform(pA)
                : 0.0;
            final double effectiveEdgeFeather = !_hasStarted
                ? (!_gpuWarmupCompleted ? 0.006 : 0.0)
                : math.max(0.006, rawEdgeFeather);

            final showSplashLayer = !_hasStarted || pA < 1.0;
            final showRevealLayer = _hasStarted && pC > 0.0;

            if (widget.enableFrameTimingDiagnostics && _hasStarted) {
              _recordUiTransitionSnapshot(
                elapsedMs: elapsedSec * 1000.0,
                pA: pA,
                pB: pB,
                pC: pC,
                hasShaderMask: showSplashLayer && rawEdgeFeather > 0.005,
                hasOpacityBelowOne: showSplashLayer && rawSplashOpacity < 1.0,
                splashLayerRemoved: _hasStarted && !showSplashLayer,
                phaseCRevealStarted: showRevealLayer,
              );
            }

            final centerY = layoutSize.height * 0.5;
            final topCurtainY = ui.lerpDouble(centerY, 0.0, pC)!;
            final bottomCurtainY =
                ui.lerpDouble(centerY, layoutSize.height, pC)!;
            final consentRevealOpacity =
                const Interval(0.02, 0.45, curve: Curves.easeOut).transform(pC);

            return Stack(
              fit: StackFit.expand,
              children: [
                // 2-frame GPU warm-up occluded behind opaque _ContinuousMatrixBackground
                // so Impeller (Vulkan) executes Opacity, 4-stop dstIn ShaderMask, and
                // ECG blur shaders before video ends, then unmounts permanently.
                if (!_gpuWarmupCompleted && !_hasStarted)
                  RepaintBoundary(
                    key: const ValueKey<String>('aegis_gpu_warmup_layer'),
                    child: Opacity(
                      opacity: 254.0 / 255.0,
                      child: ShaderMask(
                        blendMode: BlendMode.dstIn,
                        shaderCallback: (rect) => ui.Gradient.linear(
                          rect.topCenter,
                          rect.bottomCenter,
                          const [
                            Colors.transparent,
                            Colors.white,
                            Colors.white,
                            Colors.transparent,
                          ],
                          const [0.0, 0.22, 0.78, 1.0],
                        ),
                        child: CustomPaint(
                          size: layoutSize,
                          painter: AegisEcgHologramPainter(
                            elapsedSeconds: 0.90,
                            phaseAProgress: 0.50,
                            phaseBProgress: 0.55,
                            phaseCProgress: 0.05,
                          ),
                        ),
                      ),
                    ),
                  ),

                // Continuous dark matrix base present throughout every stage
                const RepaintBoundary(
                  child: _ContinuousMatrixBackground(),
                ),

                // Consent screen is NEVER mounted until Phase C reveals the curtains
                if (showRevealLayer)
                  ClipRect(
                    clipper: _HorizontalSplitClipper(
                      top: topCurtainY,
                      bottom: bottomCurtainY,
                    ),
                    child: Opacity(
                      opacity: consentRevealOpacity,
                      child: IgnorePointer(
                        ignoring: pC < 1.0,
                        child: RepaintBoundary(
                          child: KeyedSubtree(
                            key: _consentSubtreeKey,
                            child: widget.consentChild,
                          ),
                        ),
                      ),
                    ),
                  ),

                // Isolated splash layer with RepaintBoundary + stabilized Opacity/ShaderMask:
                // stays fast-path during video hold (after 2 warm-up frames), enters Phase A
                // with active saveLayer & 4-stop gradient from t = +0ms without mid-phase
                // layer allocation switches, and defers dispose until 7500ms completion.
                Visibility(
                  key: const ValueKey<String>('aegis_splash_visibility_layer'),
                  visible: showSplashLayer,
                  maintainState: true,
                  maintainAnimation: true,
                  maintainSize: true,
                  child: RepaintBoundary(
                    child: IgnorePointer(
                      ignoring: _hasStarted,
                      child: Opacity(
                        opacity: splashOpacity,
                        child: Transform(
                          alignment: Alignment.center,
                          transform:
                              Matrix4.diagonal3Values(scaleX, scaleY, 1.0),
                          child: ShaderMask(
                            blendMode: BlendMode.dstIn,
                            shaderCallback: (rect) {
                              if (effectiveEdgeFeather <= 0.005) {
                                return ui.Gradient.linear(
                                  rect.topCenter,
                                  rect.bottomCenter,
                                  const [Colors.white, Colors.white],
                                  const [0.0, 1.0],
                                );
                              }
                              return ui.Gradient.linear(
                                rect.topCenter,
                                rect.bottomCenter,
                                const [
                                  Colors.transparent,
                                  Colors.white,
                                  Colors.white,
                                  Colors.transparent,
                                ],
                                [
                                  0.0,
                                  effectiveEdgeFeather,
                                  1.0 - effectiveEdgeFeather,
                                  1.0,
                                ],
                              );
                            },
                            child: KeyedSubtree(
                              key: _splashSubtreeKey,
                              child: widget.splashChild,
                            ),
                          ),
                        ),
                      ),
                    ),
                  ),
                ),

                if (_hasStarted)
                  RepaintBoundary(
                    child: IgnorePointer(
                      child: CustomPaint(
                        key: const Key('aegis_ecg_hologram_painter'),
                        painter: AegisEcgHologramPainter(
                          elapsedSeconds: elapsedSec,
                          phaseAProgress: pA,
                          phaseBProgress: pB,
                          phaseCProgress: pC,
                        ),
                        size: layoutSize,
                      ),
                    ),
                  ),
              ],
            );
          },
        );
      },
    );
  }
}

class _HorizontalSplitClipper extends CustomClipper<Rect> {
  const _HorizontalSplitClipper({
    required this.top,
    required this.bottom,
  });

  final double top;
  final double bottom;

  @override
  Rect getClip(Size size) {
    final safeTop = top.clamp(0.0, size.height);
    final safeBottom = bottom.clamp(safeTop, size.height);
    return Rect.fromLTRB(0.0, safeTop, size.width, safeBottom);
  }

  @override
  bool shouldReclip(covariant _HorizontalSplitClipper oldClipper) {
    return oldClipper.top != top || oldClipper.bottom != bottom;
  }
}

class _EcgGeometryData {
  const _EcgGeometryData({
    required this.size,
    required this.fullPath,
    required this.fullMetric,
    required this.totalLength,
    required this.rPeakApex,
    required this.rPeakArcLength,
    required this.rPeakArcRatio,
  });

  final Size size;
  final Path fullPath;
  final ui.PathMetric fullMetric;
  final double totalLength;
  final Offset rPeakApex;
  final double rPeakArcLength;
  final double rPeakArcRatio;
}

class _EcgGeometryCache {
  static _EcgGeometryData? _cached;

  static _EcgGeometryData forSize(Size size) {
    final existing = _cached;
    if (existing != null && existing.size == size) {
      return existing;
    }
    final built = compute(size);
    _cached = built;
    return built;
  }

  static _EcgGeometryData compute(Size size) {
    final w = size.width;
    final h = size.height;
    final cy = h * 0.5;
    final amp = h * 0.145;

    final rApex = Offset(0.465 * w, cy - amp);

    // Exact shared prefix up to the R-peak apex guarantees PathMetric arc-length
    // synchronizes volumetric pulse head, flare, and haptic feedback identically.
    void addPrefixToR(Path p) {
      p.moveTo(0.0, cy);
      p.lineTo(0.18 * w, cy);
      // Distinct P-wave
      p.quadraticBezierTo(0.235 * w, cy - amp * 0.26, 0.29 * w, cy);
      // PR segment
      p.lineTo(0.35 * w, cy);
      // Q-wave dip
      p.lineTo(0.395 * w, cy + amp * 0.28);
      // Sharp R-wave upstroke to apex
      p.lineTo(rApex.dx, rApex.dy);
    }

    final prefixPath = Path();
    addPrefixToR(prefixPath);
    final prefixMetrics = prefixPath.computeMetrics().toList();
    final prefixLength =
        prefixMetrics.isNotEmpty ? prefixMetrics.first.length : 0.0;

    final fullPath = Path();
    addPrefixToR(fullPath);
    // Sharp S-wave downstroke
    fullPath.lineTo(0.530 * w, cy + amp * 0.42);
    // Return to ST baseline
    fullPath.lineTo(0.575 * w, cy);
    fullPath.lineTo(0.630 * w, cy);
    // Clear broad T-wave
    fullPath.quadraticBezierTo(0.705 * w, cy - amp * 0.38, 0.780 * w, cy);
    // Trailing baseline
    fullPath.lineTo(w, cy);

    final fullMetric = fullPath.computeMetrics().first;
    final totalLength = fullMetric.length;
    final arcRatio =
        totalLength > 0 ? (prefixLength / totalLength).clamp(0.0, 1.0) : 0.5;

    return _EcgGeometryData(
      size: size,
      fullPath: fullPath,
      fullMetric: fullMetric,
      totalLength: totalLength,
      rPeakApex: rApex,
      rPeakArcLength: prefixLength,
      rPeakArcRatio: arcRatio,
    );
  }
}

/// Renders V2 volumetric hologram phases:
/// - Central cold cyan energy beam convergence preserving matrix aesthetics (Phase A)
/// - Volumetric 4-layer P-Q-R-S-T ECG trace with long comet tail & horizontal
///   anamorphic R-peak flare (Phase B)
/// - Matrix shutters & dual glowing curtain borders traveling cleanly to top/bottom
///   screen edges before fading out (Phase C)
class AegisEcgHologramPainter extends CustomPainter {
  AegisEcgHologramPainter({
    required this.elapsedSeconds,
    required this.phaseAProgress,
    required this.phaseBProgress,
    required this.phaseCProgress,
  });

  final double elapsedSeconds;
  final double phaseAProgress;
  final double phaseBProgress;
  final double phaseCProgress;

  static const Color _matrixBg = Color(0xFF070B12);
  static const Color _ambientCyan = Color(0xFF00B8D4);
  static const Color _cyan = Color(0xFF00E5FF);
  static const Color _brightCyan = Color(0xFF52F6FF);
  static const Color _whiteCore = Color(0xFFF0FFFF);

  @override
  void paint(Canvas canvas, Size size) {
    if (size.isEmpty) return;
    final cy = size.height * 0.5;
    final geometry = _EcgGeometryCache.forSize(size);

    // 1. Central horizontal energy beam convergence (Phases A -> B -> start of C)
    if (phaseCProgress <= 0.12) {
      final collapseCurve = Curves.easeOutCubic.transform(phaseAProgress);
      final centerFadeOut = (1.0 - (phaseCProgress / 0.12)).clamp(0.0, 1.0);
      final baseIntensity = collapseCurve * centerFadeOut;

      if (baseIntensity > 0.005) {
        final wideBloomWidth = ui.lerpDouble(42.0, 14.0, collapseCurve)!;
        final left = Offset(0.0, cy);
        final right = Offset(size.width, cy);

        // Wide atmospheric optical bloom
        canvas.drawLine(
          left,
          right,
          Paint()
            ..color = _ambientCyan.withValues(alpha: 0.28 * baseIntensity)
            ..strokeWidth = wideBloomWidth
            ..maskFilter = const MaskFilter.blur(BlurStyle.normal, 16.0),
        );

        // Mid turquoise beam
        canvas.drawLine(
          left,
          right,
          Paint()
            ..color = _cyan.withValues(alpha: 0.56 * baseIntensity)
            ..strokeWidth = 6.5 * baseIntensity
            ..maskFilter = const MaskFilter.blur(BlurStyle.normal, 6.0),
        );

        // Concentrated white-blue energy core
        canvas.drawLine(
          left,
          right,
          Paint()
            ..color = _whiteCore.withValues(alpha: 0.85 * baseIntensity)
            ..strokeWidth = 2.0 * math.max(0.45, baseIntensity),
        );
      }
    }

    // 2. Phase B: Volumetric P-Q-R-S-T pulse + long comet tail + horizontal anamorphic flare
    final pulseFadeOut = elapsedSeconds >= 1.34
        ? (1.0 - ((elapsedSeconds - 1.34) / 0.14)).clamp(0.0, 1.0)
        : 1.0;

    if (phaseBProgress > 0.0 && pulseFadeOut > 0.01) {
      _paintEcgPulse(canvas, size, geometry, pulseFadeOut);
    }

    // 3. Phase C: Matrix shutters + dual glowing curtain borders reaching top/bottom edges
    if (phaseCProgress > 0.0) {
      _paintCurtainReveal(canvas, size, cy);
    }
  }

  void _paintEcgPulse(
    Canvas canvas,
    Size size,
    _EcgGeometryData geometry,
    double masterAlpha,
  ) {
    final metric = geometry.fullMetric;
    final totalLen = geometry.totalLength;
    if (totalLen <= 0.0) return;

    final headDist = (phaseBProgress * totalLen).clamp(0.0, totalLen);
    // Long luminous trail (~72% of total path) keeps the full P-Q-R-S-T waveform readable.
    final tailSpan = totalLen * 0.72;
    final tailStart = math.max(0.0, headDist - tailSpan);

    // Reusable stroke Paints to minimize per-frame heap allocations across 14 segments.
    final ambientPaint = Paint()
      ..style = PaintingStyle.stroke
      ..strokeCap = StrokeCap.round
      ..strokeJoin = StrokeJoin.round
      ..maskFilter = const MaskFilter.blur(BlurStyle.normal, 14.0);
    final outerGlowPaint = Paint()
      ..style = PaintingStyle.stroke
      ..strokeCap = StrokeCap.round
      ..strokeJoin = StrokeJoin.round
      ..maskFilter = const MaskFilter.blur(BlurStyle.normal, 6.5);
    final midGlowPaint = Paint()
      ..style = PaintingStyle.stroke
      ..strokeCap = StrokeCap.round
      ..strokeJoin = StrokeJoin.round
      ..maskFilter = const MaskFilter.blur(BlurStyle.normal, 2.2);
    final innerCorePaint = Paint()
      ..style = PaintingStyle.stroke
      ..strokeCap = StrokeCap.round
      ..strokeJoin = StrokeJoin.round;

    const segments = 14;
    final actualSpan = headDist - tailStart;
    if (actualSpan > 0.5) {
      for (int i = 0; i < segments; i++) {
        final s0 = tailStart + (actualSpan * i / segments);
        final s1 = tailStart + (actualSpan * (i + 1) / segments);
        final frac = (i + 1) / segments;
        final segAlpha = math.pow(frac, 1.35).toDouble() * masterAlpha;
        final segPath = metric.extractPath(s0, s1);

        ambientPaint
          ..strokeWidth = 14.0 + 8.0 * frac
          ..color = _ambientCyan.withValues(alpha: 0.20 * segAlpha);
        canvas.drawPath(segPath, ambientPaint);

        outerGlowPaint
          ..strokeWidth = 7.5 + 4.5 * frac
          ..color = _cyan.withValues(alpha: 0.46 * segAlpha);
        canvas.drawPath(segPath, outerGlowPaint);

        midGlowPaint
          ..strokeWidth = 3.4 + 1.6 * frac
          ..color = _brightCyan.withValues(alpha: 0.84 * segAlpha);
        canvas.drawPath(segPath, midGlowPaint);

        innerCorePaint
          ..strokeWidth = 1.9 + 0.9 * frac
          ..color = _whiteCore.withValues(alpha: 0.98 * segAlpha);
        canvas.drawPath(segPath, innerCorePaint);
      }
    }

    // Bright holographic leading pulse point
    final tangent = metric.getTangentForOffset(headDist);
    if (tangent != null) {
      final head = tangent.position;
      canvas.drawCircle(
        head,
        15.0,
        Paint()
          ..color = _cyan.withValues(alpha: 0.42 * masterAlpha)
          ..maskFilter = const MaskFilter.blur(BlurStyle.normal, 10.0),
      );
      canvas.drawCircle(
        head,
        6.5,
        Paint()
          ..color = _brightCyan.withValues(alpha: 0.78 * masterAlpha)
          ..maskFilter = const MaskFilter.blur(BlurStyle.normal, 3.0),
      );
      canvas.drawCircle(
        head,
        3.2,
        Paint()..color = _whiteCore.withValues(alpha: masterAlpha),
      );
    }

    // Horizontal anamorphic flare synced strictly with R-peak PathMetric phase
    if (phaseBProgress >= geometry.rPeakArcRatio) {
      final pR = ((phaseBProgress - geometry.rPeakArcRatio) /
              (1.0 - geometry.rPeakArcRatio))
          .clamp(0.0, 1.0);
      _paintRPeakAnamorphicFlare(
        canvas,
        size,
        geometry.rPeakApex,
        pR,
        masterAlpha,
      );
    }
  }

  void _paintRPeakAnamorphicFlare(
    Canvas canvas,
    Size size,
    Offset apex,
    double pR,
    double masterAlpha,
  ) {
    // Concentrate into a snappy ~215ms anamorphic optical streak (first 36% post-R).
    final rawT = (pR / 0.36).clamp(0.0, 1.0);
    if (rawT >= 1.0) return;

    final double envelope;
    if (rawT <= 0.18) {
      envelope = Curves.easeOut.transform(rawT / 0.18);
    } else {
      final decay = ((rawT - 0.18) / 0.82).clamp(0.0, 1.0);
      envelope = math.pow(1.0 - decay, 2.2).toDouble();
    }

    final alpha = envelope * masterAlpha;
    if (alpha <= 0.01) return;

    final outerWidth =
        AegisHologramTransition.computeAnamorphicFlareWidth(size, rawT);
    final midWidth = outerWidth * 0.75;
    final coreWidth = outerWidth * 0.54;

    // 1. Wide horizontal cyan streak
    canvas.drawLine(
      Offset(apex.dx - outerWidth * 0.5, apex.dy),
      Offset(apex.dx + outerWidth * 0.5, apex.dy),
      Paint()
        ..color = _cyan.withValues(alpha: 0.52 * alpha)
        ..strokeWidth = 7.5 * (0.4 + 0.6 * envelope)
        ..strokeCap = StrokeCap.round
        ..maskFilter = const MaskFilter.blur(BlurStyle.normal, 9.0),
    );

    // 2. Mid turquoise anamorphic blade
    canvas.drawLine(
      Offset(apex.dx - midWidth * 0.5, apex.dy),
      Offset(apex.dx + midWidth * 0.5, apex.dy),
      Paint()
        ..color = _brightCyan.withValues(alpha: 0.84 * alpha)
        ..strokeWidth = 3.0 * (0.4 + 0.6 * envelope)
        ..strokeCap = StrokeCap.round
        ..maskFilter = const MaskFilter.blur(BlurStyle.normal, 2.5),
    );

    // 3. Crisp horizontal white-blue needle
    canvas.drawLine(
      Offset(apex.dx - coreWidth * 0.5, apex.dy),
      Offset(apex.dx + coreWidth * 0.5, apex.dy),
      Paint()
        ..color = _whiteCore.withValues(alpha: 0.96 * alpha)
        ..strokeWidth = 1.4
        ..strokeCap = StrokeCap.round,
    );

    // 4. Compact elliptical optical core right at R-peak apex
    canvas.drawOval(
      Rect.fromCenter(center: apex, width: 24.0, height: 7.0),
      Paint()
        ..color = _whiteCore.withValues(alpha: 0.88 * alpha)
        ..maskFilter = const MaskFilter.blur(BlurStyle.normal, 3.0),
    );
  }

  void _paintCurtainReveal(Canvas canvas, Size size, double centerY) {
    final topY = ui.lerpDouble(centerY, 0.0, phaseCProgress)!;
    final bottomY = ui.lerpDouble(centerY, size.height, phaseCProgress)!;

    // Keep curtain borders at full luminosity all the way to top (0) & bottom (height)
    // edges, fading out only in the final 10% (phaseCProgress 0.90 -> 1.00).
    final edgeFade = (1.0 -
            const Interval(0.90, 1.0, curve: Curves.easeIn)
                .transform(phaseCProgress))
        .clamp(0.0, 1.0);

    // Dark shutter panels preserving the matrix backdrop until reaching screen bounds
    final shutterPaint = Paint()..color = _matrixBg;
    if (topY > 0.5) {
      canvas.drawRect(Rect.fromLTRB(0, 0, size.width, topY), shutterPaint);
    }
    if (bottomY < size.height - 0.5) {
      canvas.drawRect(
        Rect.fromLTRB(0, bottomY, size.width, size.height),
        shutterPaint,
      );
    }

    // Subtle matrix scanlines inside the parting shutters
    if (edgeFade > 0.02) {
      final scanPaint = Paint()
        ..color = _cyan.withValues(alpha: 0.045 * edgeFade)
        ..strokeWidth = 1.0;
      const spacing = 20.0;
      for (double y = topY - spacing; y > 0; y -= spacing) {
        canvas.drawLine(Offset(0, y), Offset(size.width, y), scanPaint);
      }
      for (double y = bottomY + spacing; y < size.height; y += spacing) {
        canvas.drawLine(Offset(0, y), Offset(size.width, y), scanPaint);
      }
    }

    if (edgeFade > 0.01) {
      final outerBorderPaint = Paint()
        ..color = _cyan.withValues(alpha: 0.62 * edgeFade)
        ..strokeWidth = 9.0
        ..maskFilter = const MaskFilter.blur(BlurStyle.normal, 7.0);
      final innerBorderPaint = Paint()
        ..color = _whiteCore.withValues(alpha: 0.96 * edgeFade)
        ..strokeWidth = 2.0;

      void drawBorderLine(double y) {
        final safeY = y.clamp(0.0, size.height);
        canvas.drawLine(
          Offset(0, safeY),
          Offset(size.width, safeY),
          outerBorderPaint,
        );
        canvas.drawLine(
          Offset(0, safeY),
          Offset(size.width, safeY),
          innerBorderPaint,
        );
      }

      drawBorderLine(topY);
      drawBorderLine(bottomY);
    }
  }

  @override
  bool shouldRepaint(covariant AegisEcgHologramPainter oldDelegate) {
    return oldDelegate.elapsedSeconds != elapsedSeconds ||
        oldDelegate.phaseAProgress != phaseAProgress ||
        oldDelegate.phaseBProgress != phaseBProgress ||
        oldDelegate.phaseCProgress != phaseCProgress;
  }
}

/// Minimal dark medical consent demo screen strictly for visual verification
/// of the holographic curtain reveal. Non-interactive with Vault or backend.
class AegisDemoConsentScreen extends StatelessWidget {
  const AegisDemoConsentScreen({
    super.key,
    required this.onReplayPressed,
  });

  final VoidCallback onReplayPressed;

  static const Color _cyan = Color(0xFF00E5FF);

  @override
  Widget build(BuildContext context) {
    return Stack(
      key: const Key('aegis_demo_consent_screen'),
      fit: StackFit.expand,
      children: [
        const _ContinuousMatrixBackground(),
        SafeArea(
          child: Padding(
            padding: const EdgeInsets.symmetric(horizontal: 28, vertical: 24),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.stretch,
              children: [
                const SizedBox(height: 16),
                Row(
                  children: [
                    Container(
                      width: 8,
                      height: 8,
                      decoration: const BoxDecoration(
                        shape: BoxShape.circle,
                        color: _cyan,
                      ),
                    ),
                    const SizedBox(width: 10),
                    Flexible(
                      child: Text(
                        'AEGIS // VISUAL PROTOTYPE V4',
                        overflow: TextOverflow.ellipsis,
                        maxLines: 1,
                        style: TextStyle(
                          color: _cyan.withValues(alpha: 0.78),
                          fontSize: 12,
                          letterSpacing: 1.8,
                          fontWeight: FontWeight.w500,
                        ),
                      ),
                    ),
                  ],
                ),
                const Spacer(),
                const Text(
                  'Age Confirmation',
                  style: TextStyle(
                    color: Color(0xFFEAFDFF),
                    fontSize: 28,
                    fontWeight: FontWeight.w600,
                    letterSpacing: 0.4,
                  ),
                ),
                const SizedBox(height: 12),
                Text(
                  'Confirm you are 18+ to continue. This screen is an isolated visual demo only.',
                  style: TextStyle(
                    color: Colors.white.withValues(alpha: 0.68),
                    fontSize: 15,
                    height: 1.45,
                  ),
                ),
                const SizedBox(height: 28),
                // Intentionally non-interactive visual placeholder
                IgnorePointer(
                  child: Container(
                    key: const Key('demo_age_confirm_element'),
                    padding: const EdgeInsets.symmetric(
                      horizontal: 20,
                      vertical: 18,
                    ),
                    decoration: BoxDecoration(
                      color: const Color(0xFF0D1724),
                      borderRadius: BorderRadius.circular(14),
                      border: Border.all(
                        color: _cyan.withValues(alpha: 0.45),
                        width: 1.2,
                      ),
                    ),
                    child: Row(
                      children: [
                        Icon(
                          Icons.verified_user_outlined,
                          color: _cyan.withValues(alpha: 0.9),
                          size: 22,
                        ),
                        const SizedBox(width: 14),
                        const Expanded(
                          child: Text(
                            "I'm 18+",
                            style: TextStyle(
                              color: Color(0xFFE6FFFF),
                              fontSize: 16,
                              fontWeight: FontWeight.w500,
                            ),
                          ),
                        ),
                        Text(
                          'DEMO',
                          style: TextStyle(
                            color: _cyan.withValues(alpha: 0.65),
                            fontSize: 11,
                            letterSpacing: 1.6,
                            fontWeight: FontWeight.w600,
                          ),
                        ),
                      ],
                    ),
                  ),
                ),
                const Spacer(),
                OutlinedButton.icon(
                  key: const Key('replay_splash_demo_button'),
                  onPressed: onReplayPressed,
                  style: OutlinedButton.styleFrom(
                    foregroundColor: _cyan,
                    side: BorderSide(color: _cyan.withValues(alpha: 0.55)),
                    padding: const EdgeInsets.symmetric(vertical: 14),
                    shape: RoundedRectangleBorder(
                      borderRadius: BorderRadius.circular(12),
                    ),
                  ),
                  icon: const Icon(Icons.replay_rounded, size: 19),
                  label: const Text(
                    'Replay Demo',
                    style: TextStyle(
                      fontSize: 14,
                      fontWeight: FontWeight.w500,
                      letterSpacing: 0.6,
                    ),
                  ),
                ),
                const SizedBox(height: 12),
              ],
            ),
          ),
        ),
      ],
    );
  }
}

/// Structural milestone captured on the UI thread during Splash V4 transition.
class SplashTransitionEvent {
  const SplashTransitionEvent({
    required this.label,
    required this.elapsedMs,
    required this.pA,
    required this.pB,
    required this.pC,
  });

  final String label;
  final double elapsedMs;
  final double pA;
  final double pB;
  final double pC;
}

class _UiTransitionFrameSnapshot {
  const _UiTransitionFrameSnapshot({
    required this.vsyncUs,
    required this.elapsedMs,
    required this.pA,
    required this.pB,
    required this.pC,
    required this.events,
  });

  final int vsyncUs;
  final double elapsedMs;
  final double pA;
  final double pB;
  final double pC;
  final List<String> events;
}

/// Public snapshot sample exposed for deterministic testing of UI/Timing correlation.
class SplashUiSnapshotSample {
  const SplashUiSnapshotSample({
    required this.vsyncUs,
    required this.elapsedMs,
    required this.pA,
    required this.pB,
    required this.pC,
    this.events = const [],
  });

  final int vsyncUs;
  final double elapsedMs;
  final double pA;
  final double pB;
  final double pC;
  final List<String> events;
}

/// Raw timing sample extracted from `ui.FrameTiming` (or supplied in tests).
class SplashRawTimingSample {
  const SplashRawTimingSample({
    required this.vsyncStartUs,
    required this.buildMs,
    required this.rasterMs,
    required this.totalSpanMs,
  });

  final int vsyncStartUs;
  final double buildMs;
  final double rasterMs;
  final double totalSpanMs;
}

/// Correlated frame entry ranked in [SplashTransitionTimingReport.topHeaviestFrames].
class SplashFrameTimingSample {
  const SplashFrameTimingSample({
    required this.buildMs,
    required this.rasterMs,
    required this.totalSpanMs,
    required this.phaseLabel,
    required this.correlationType,
    this.elapsedMs,
    this.pA,
    this.pB,
    this.pC,
    this.events = const [],
  });

  final double buildMs;
  final double rasterMs;
  final double totalSpanMs;
  final String phaseLabel;
  final String correlationType;
  final double? elapsedMs;
  final double? pA;
  final double? pB;
  final double? pC;
  final List<String> events;

  double get sortWeightMs => math.max(totalSpanMs, buildMs + rasterMs);
}

/// One-shot diagnostic report emitted at the end of the Splash V4 transition.
class SplashTransitionTimingReport {
  const SplashTransitionTimingReport({
    required this.totalFrames,
    required this.phaseABFrames,
    required this.uncorrelatedFrames,
    required this.preTransitionSkippedFrames,
    required this.uiOver8msCount,
    required this.uiOver16msCount,
    required this.rasterOver8msCount,
    required this.rasterOver16msCount,
    required this.spanOver8msCount,
    required this.spanOver16msCount,
    this.uiP95Ms = 0.0,
    this.rasterP95Ms = 0.0,
    this.spanP95Ms = 0.0,
    this.warmupFramesCount = 0,
    this.warmupMaxUiMs = 0.0,
    this.warmupMaxRasterMs = 0.0,
    this.warmupCompletedBeforeTransition = false,
    required this.structuralEvents,
    required this.topHeaviestFrames,
    required this.formattedSummary,
  });

  final int totalFrames;
  final int phaseABFrames;
  final int uncorrelatedFrames;
  final int preTransitionSkippedFrames;
  final int uiOver8msCount;
  final int uiOver16msCount;
  final int rasterOver8msCount;
  final int rasterOver16msCount;
  final int spanOver8msCount;
  final int spanOver16msCount;
  final double uiP95Ms;
  final double rasterP95Ms;
  final double spanP95Ms;
  final int warmupFramesCount;
  final double warmupMaxUiMs;
  final double warmupMaxRasterMs;
  final bool warmupCompletedBeforeTransition;
  final List<SplashTransitionEvent> structuralEvents;
  final List<SplashFrameTimingSample> topHeaviestFrames;
  final String formattedSummary;

  static double _computePercentile(List<double> values, double quantile) {
    if (values.isEmpty) return 0.0;
    final sorted = List<double>.from(values)..sort();
    final idx =
        ((sorted.length * quantile).ceil() - 1).clamp(0, sorted.length - 1);
    return sorted[idx];
  }

  static SplashTransitionTimingReport _fromCaptured({
    required List<ui.FrameTiming> timings,
    required List<_UiTransitionFrameSnapshot> snapshots,
    required List<SplashTransitionEvent> structuralEvents,
    required int? firstTransitionVsyncUs,
    List<ui.FrameTiming> warmupTimings = const [],
    bool warmupCompletedBeforeTransition = false,
  }) {
    final rawSamples = timings
        .map(
          (t) => SplashRawTimingSample(
            vsyncStartUs: t.timestampInMicroseconds(ui.FramePhase.vsyncStart),
            buildMs: t.buildDuration.inMicroseconds / 1000.0,
            rasterMs: t.rasterDuration.inMicroseconds / 1000.0,
            totalSpanMs: t.totalSpan.inMicroseconds / 1000.0,
          ),
        )
        .toList(growable: false);

    final rawWarmupSamples = warmupTimings
        .map(
          (t) => SplashRawTimingSample(
            vsyncStartUs: t.timestampInMicroseconds(ui.FramePhase.vsyncStart),
            buildMs: t.buildDuration.inMicroseconds / 1000.0,
            rasterMs: t.rasterDuration.inMicroseconds / 1000.0,
            totalSpanMs: t.totalSpan.inMicroseconds / 1000.0,
          ),
        )
        .toList(growable: false);

    final uiSamples = snapshots
        .map(
          (s) => SplashUiSnapshotSample(
            vsyncUs: s.vsyncUs,
            elapsedMs: s.elapsedMs,
            pA: s.pA,
            pB: s.pB,
            pC: s.pC,
            events: s.events,
          ),
        )
        .toList(growable: false);

    return _correlateInternal(
      rawTimings: rawSamples,
      snapshots: uiSamples,
      structuralEvents: structuralEvents,
      firstTransitionVsyncUs: firstTransitionVsyncUs,
      warmupTimings: rawWarmupSamples,
      warmupCompletedBeforeTransition: warmupCompletedBeforeTransition,
    );
  }

  /// Deterministic test entry point for verifying vsync correlation, filtering of
  /// pre-transition batches, and Top-5 ranking without platform timing hooks.
  @visibleForTesting
  static SplashTransitionTimingReport correlateForTesting({
    required List<SplashRawTimingSample> rawTimings,
    required List<SplashUiSnapshotSample> snapshots,
    required List<SplashTransitionEvent> structuralEvents,
    int? firstTransitionVsyncUs,
    List<SplashRawTimingSample> warmupTimings = const [],
    bool warmupCompletedBeforeTransition = false,
  }) {
    return _correlateInternal(
      rawTimings: rawTimings,
      snapshots: snapshots,
      structuralEvents: structuralEvents,
      firstTransitionVsyncUs: firstTransitionVsyncUs,
      warmupTimings: warmupTimings,
      warmupCompletedBeforeTransition: warmupCompletedBeforeTransition,
    );
  }

  static String _describePhase({
    required double pA,
    required double pB,
    required double pC,
  }) {
    final List<String> active = [];
    if (pA < 1.0) active.add('A');
    if (pB > 0.0 && pC < 1.0) active.add('B');
    if (pC > 0.0) active.add('C');
    if (active.isEmpty) return 'DONE';
    return 'Phase(${active.join('+')})';
  }

  static SplashTransitionTimingReport _correlateInternal({
    required List<SplashRawTimingSample> rawTimings,
    required List<SplashUiSnapshotSample> snapshots,
    required List<SplashTransitionEvent> structuralEvents,
    required int? firstTransitionVsyncUs,
    List<SplashRawTimingSample> warmupTimings = const [],
    bool warmupCompletedBeforeTransition = false,
  }) {
    int preTransitionSkipped = 0;
    int uncorrelated = 0;
    int phaseABCount = 0;
    int uiOver8 = 0;
    int uiOver16 = 0;
    int rasterOver8 = 0;
    int rasterOver16 = 0;
    int spanOver8 = 0;
    int spanOver16 = 0;

    final List<SplashFrameTimingSample> correlated = [];

    for (final raw in rawTimings) {
      // Exclude batched frames queued prior to transition start (5550 ms).
      if (firstTransitionVsyncUs != null &&
          raw.vsyncStartUs > 0 &&
          raw.vsyncStartUs < firstTransitionVsyncUs - 2000) {
        preTransitionSkipped++;
        continue;
      }

      if (raw.buildMs > 8.33) uiOver8++;
      if (raw.buildMs > 16.67) uiOver16++;
      if (raw.rasterMs > 8.33) rasterOver8++;
      if (raw.rasterMs > 16.67) rasterOver16++;
      if (raw.totalSpanMs > 8.33) spanOver8++;
      if (raw.totalSpanMs > 16.67) spanOver16++;

      SplashUiSnapshotSample? matched;
      String corrType = 'UNCORRELATED';

      if (snapshots.isNotEmpty && raw.vsyncStartUs > 0) {
        int bestDiff = 1 << 60;
        SplashUiSnapshotSample? candidate;
        for (final snap in snapshots) {
          if (snap.vsyncUs <= 0) continue;
          final diff = (snap.vsyncUs - raw.vsyncStartUs).abs();
          if (diff < bestDiff) {
            bestDiff = diff;
            candidate = snap;
          }
        }
        if (candidate != null && bestDiff <= 2000) {
          matched = candidate;
          corrType = 'EXACT_VSYNC';
        } else if (firstTransitionVsyncUs != null) {
          final relMs = (raw.vsyncStartUs - firstTransitionVsyncUs) / 1000.0;
          double bestMsDiff = double.infinity;
          SplashUiSnapshotSample? offsetCandidate;
          for (final snap in snapshots) {
            final msDiff = (snap.elapsedMs - relMs).abs();
            if (msDiff < bestMsDiff) {
              bestMsDiff = msDiff;
              offsetCandidate = snap;
            }
          }
          if (offsetCandidate != null && bestMsDiff <= 16.0) {
            matched = offsetCandidate;
            corrType = 'OFFSET_NEAREST';
          }
        }
      }

      if (matched == null) {
        uncorrelated++;
        correlated.add(
          SplashFrameTimingSample(
            buildMs: raw.buildMs,
            rasterMs: raw.rasterMs,
            totalSpanMs: raw.totalSpanMs,
            phaseLabel: 'UNCORRELATED',
            correlationType: corrType,
          ),
        );
      } else {
        final phaseLabel = _describePhase(
          pA: matched.pA,
          pB: matched.pB,
          pC: matched.pC,
        );
        if (matched.pC == 0.0 || matched.pA < 1.0) {
          phaseABCount++;
        }
        correlated.add(
          SplashFrameTimingSample(
            buildMs: raw.buildMs,
            rasterMs: raw.rasterMs,
            totalSpanMs: raw.totalSpanMs,
            phaseLabel: phaseLabel,
            correlationType: corrType,
            elapsedMs: matched.elapsedMs,
            pA: matched.pA,
            pB: matched.pB,
            pC: matched.pC,
            events: matched.events,
          ),
        );
      }
    }

    final sorted = List<SplashFrameTimingSample>.from(correlated)
      ..sort((a, b) => b.sortWeightMs.compareTo(a.sortWeightMs));
    final top5 = sorted.take(5).toList(growable: false);
    final eventsCopy = List<SplashTransitionEvent>.unmodifiable(structuralEvents);
    final uiP95 = _computePercentile(
      correlated.map((s) => s.buildMs).toList(growable: false),
      0.95,
    );
    final rasterP95 = _computePercentile(
      correlated.map((s) => s.rasterMs).toList(growable: false),
      0.95,
    );
    final spanP95 = _computePercentile(
      correlated.map((s) => s.totalSpanMs).toList(growable: false),
      0.95,
    );
    double warmupMaxUi = 0.0;
    double warmupMaxRaster = 0.0;
    for (final w in warmupTimings) {
      if (w.buildMs > warmupMaxUi) warmupMaxUi = w.buildMs;
      if (w.rasterMs > warmupMaxRaster) warmupMaxRaster = w.rasterMs;
    }

    final buf = StringBuffer()
      ..writeln('=== [AEGIS SPLASH V4 FRAME TIMING REPORT] ===')
      ..writeln(
        'Warmup: completed=$warmupCompletedBeforeTransition, '
        'frames=${warmupTimings.length}, '
        'maxUI=${warmupMaxUi.toStringAsFixed(2)}ms, '
        'maxRaster=${warmupMaxRaster.toStringAsFixed(2)}ms',
      )
      ..writeln(
        'Frames: total=${correlated.length}, phaseA->B=$phaseABCount, '
        'uncorrelated=$uncorrelated, preTransitionSkipped=$preTransitionSkipped, '
        'uiSnapshots=${snapshots.length}',
      )
      ..writeln(
        'Thresholds: UI(>8.33ms=$uiOver8, >16.67ms=$uiOver16) | '
        'Raster(>8.33ms=$rasterOver8, >16.67ms=$rasterOver16) | '
        'TotalSpan(>8.33ms=$spanOver8, >16.67ms=$spanOver16)',
      )
      ..writeln(
        'Percentiles (p95): UI=${uiP95.toStringAsFixed(2)}ms | '
        'Raster=${rasterP95.toStringAsFixed(2)}ms | '
        'TotalSpan=${spanP95.toStringAsFixed(2)}ms',
      )
      ..writeln('Structural Events:');

    if (eventsCopy.isEmpty) {
      buf.writeln('  (none recorded)');
    } else {
      for (final e in eventsCopy) {
        buf.writeln(
          '  * ${e.label}: t=+${e.elapsedMs.toStringAsFixed(1)}ms '
          '(pA=${e.pA.toStringAsFixed(3)}, pB=${e.pB.toStringAsFixed(3)}, pC=${e.pC.toStringAsFixed(3)})',
        );
      }
    }

    buf.writeln('Top-${top5.length} Heaviest Frames:');
    if (top5.isEmpty) {
      buf.writeln('  (no FrameTiming samples delivered)');
    } else {
      for (int i = 0; i < top5.length; i++) {
        final f = top5[i];
        final tStr = f.elapsedMs != null
            ? 't=+${f.elapsedMs!.toStringAsFixed(1)}ms'
            : 't=UNCORRELATED';
        final evStr = f.events.isNotEmpty ? ' events=${f.events.join(',')}' : '';
        buf.writeln(
          '  #${i + 1}: UI=${f.buildMs.toStringAsFixed(2)}ms '
          'Raster=${f.rasterMs.toStringAsFixed(2)}ms '
          'Span=${f.totalSpanMs.toStringAsFixed(2)}ms | '
          '${f.phaseLabel} $tStr [${f.correlationType}]$evStr',
        );
      }
    }
    buf.writeln('=============================================');

    return SplashTransitionTimingReport(
      totalFrames: correlated.length,
      phaseABFrames: phaseABCount,
      uncorrelatedFrames: uncorrelated,
      preTransitionSkippedFrames: preTransitionSkipped,
      uiOver8msCount: uiOver8,
      uiOver16msCount: uiOver16,
      rasterOver8msCount: rasterOver8,
      rasterOver16msCount: rasterOver16,
      spanOver8msCount: spanOver8,
      spanOver16msCount: spanOver16,
      uiP95Ms: uiP95,
      rasterP95Ms: rasterP95,
      spanP95Ms: spanP95,
      warmupFramesCount: warmupTimings.length,
      warmupMaxUiMs: warmupMaxUi,
      warmupMaxRasterMs: warmupMaxRaster,
      warmupCompletedBeforeTransition: warmupCompletedBeforeTransition,
      structuralEvents: eventsCopy,
      topHeaviestFrames: top5,
      formattedSummary: buf.toString(),
    );
  }
}
