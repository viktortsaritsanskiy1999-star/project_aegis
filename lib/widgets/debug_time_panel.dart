// lib/widgets/debug_time_panel.dart
import 'dart:async';
import 'package:flutter/material.dart';
import 'package:provider/provider.dart';
import '../services/time_service.dart';

class DebugTimePanel extends StatelessWidget {
  const DebugTimePanel({super.key});

  @override
  Widget build(BuildContext context) {
    final timeService = context.watch<TimeService>();

    return Column(
      mainAxisSize: MainAxisSize.min,
      children: [
        // 🕒 LARGE TIME INDICATOR WITH TICKING SECONDS
        StreamBuilder<int>(
          stream: Stream.periodic(const Duration(seconds: 1), (i) => i),
          builder: (context, snapshot) {
            final now = timeService.now;
            final timeStr =
                "${now.hour.toString().padLeft(2, '0')}:${now.minute.toString().padLeft(2, '0')}:${now.second.toString().padLeft(2, '0')}";

            return Container(
              padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 8),
              decoration: BoxDecoration(
                color: Colors.cyanAccent.withOpacity(0.1),
                borderRadius: BorderRadius.circular(12),
                border: Border.all(color: Colors.cyanAccent.withOpacity(0.3)),
              ),
              child: Text(
                "🕒 $timeStr",
                style: const TextStyle(
                  color: Colors.cyanAccent,
                  fontWeight: FontWeight.bold,
                  fontSize: 18,
                  fontFamily: 'Courier',
                  letterSpacing: 1.5,
                ),
              ),
            );
          },
        ),

        const SizedBox(height: 16),

        // ⏱ ROW 1: MINUTES
        Row(
          mainAxisAlignment: MainAxisAlignment.spaceEvenly,
          children: [
            Expanded(child: _buildBtn("+10m", () => timeService.fastForward(const Duration(minutes: 10)))),
            const SizedBox(width: 8),
            Expanded(child: _buildBtn("+1m", () => timeService.fastForward(const Duration(minutes: 1)))),
            const SizedBox(width: 8),
            Expanded(child: _buildBtn("+30m", () => timeService.fastForward(const Duration(minutes: 30)))),
          ],
        ),

        const SizedBox(height: 10),

        // ⏳ ROW 2: HOURS AND DAYS
        Row(
          mainAxisAlignment: MainAxisAlignment.spaceEvenly,
          children: [
            Expanded(child: _buildBtn("+1h", () => timeService.fastForward(const Duration(hours: 1)))),
            const SizedBox(width: 8),
            Expanded(child: _buildBtn("+4h", () => timeService.fastForward(const Duration(hours: 4)))),
            const SizedBox(width: 8),
            Expanded(child: _buildBtn("+24h", () => timeService.fastForward(const Duration(hours: 24)))),
          ],
        ),

        const SizedBox(height: 16),

        // 🔄 RESET BUTTON
        SizedBox(
          width: double.infinity,
          child: _buildResetBtn(() => timeService.resetToRealTime()),
        ),
      ],
    );
  }

  Widget _buildBtn(String label, VoidCallback onTap) {
    return InkWell(
      onTap: onTap,
      borderRadius: BorderRadius.circular(10),
      child: Container(
        padding: const EdgeInsets.symmetric(vertical: 10),
        alignment: Alignment.center,
        decoration: BoxDecoration(
          color: Colors.cyanAccent.withOpacity(0.12),
          borderRadius: BorderRadius.circular(10),
          border: Border.all(color: Colors.cyanAccent.withOpacity(0.4), width: 1),
        ),
        child: Text(
          label,
          style: const TextStyle(
            color: Colors.cyanAccent,
            fontSize: 13,
            fontWeight: FontWeight.bold,
          ),
        ),
      ),
    );
  }

  Widget _buildResetBtn(VoidCallback onTap) {
    return InkWell(
      onTap: onTap,
      borderRadius: BorderRadius.circular(10),
      child: Container(
        padding: const EdgeInsets.symmetric(vertical: 10),
        alignment: Alignment.center,
        decoration: BoxDecoration(
          color: Colors.redAccent.withOpacity(0.15),
          borderRadius: BorderRadius.circular(10),
          border: Border.all(color: Colors.redAccent.withOpacity(0.5), width: 1),
        ),
        child: const Row(
          mainAxisAlignment: MainAxisAlignment.center,
          children: [
            Icon(Icons.refresh_rounded, color: Colors.redAccent, size: 16),
            SizedBox(width: 6),
            Text(
              "RESET TO REAL TIME",
              style: TextStyle(
                color: Colors.redAccent,
                fontSize: 11,
                fontWeight: FontWeight.bold,
                letterSpacing: 1.0,
              ),
            ),
          ],
        ),
      ),
    );
  }
}