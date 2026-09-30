import 'package:flutter/material.dart';
import 'package:sensors_plus/sensors_plus.dart';
import 'dart:async';

class ParallaxWrapper extends StatefulWidget {
  final Widget child;
  final double intensity;

  const ParallaxWrapper({
    super.key,
    required this.child,
    this.intensity = 10.0, // Slightly reduced by default
  });

  @override
  State<ParallaxWrapper> createState() => _ParallaxWrapperState();
}

class _ParallaxWrapperState extends State<ParallaxWrapper> {
  double _posX = 0.0;
  double _posY = 0.0;
  StreamSubscription? _subscription;

  // Add a low-pass filter for smoothness
  double _smoothX = 0.0;
  double _smoothY = 0.0;
  final double _filterFactor = 0.15; // The smaller, the smoother and slower

  @override
  void initState() {
    super.initState();
    _subscription = gyroscopeEvents.listen((GyroscopeEvent event) {
      if (mounted) {
        setState(() {
          // 1. Coordinates from the sensor
          _posX += event.y * widget.intensity;
          _posY += event.x * widget.intensity;

          // 2. Clamp to prevent drifting too far
          _posX = _posX.clamp(-25.0, 25.0);
          _posY = _posY.clamp(-25.0, 25.0);

          // 3. Apply filter for buttery smoothness
          _smoothX = _smoothX * (1 - _filterFactor) + _posX * _filterFactor;
          _smoothY = _smoothY * (1 - _filterFactor) + _posY * _filterFactor;
        });
      }
    });
  }

  @override
  void dispose() {
    _subscription?.cancel();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    // We use ClipRect so the image doesn't overflow the edges during movement
    return ClipRect(
      child: Transform.translate(
        // Take offset from the filtered (_smooth) coordinates
        offset: Offset(_smoothX, _smoothY),
        // Transform.scale is needed to hide edges during shift
        child: Transform.scale(
          scale: 1.1, // Slightly increase to provide headroom for shifting
          child: widget.child,
        ),
      ),
    );
  }
}