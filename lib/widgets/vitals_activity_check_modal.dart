import 'package:flutter/material.dart';
import 'package:provider/provider.dart';
import '../services/vitals_monitor_service.dart';
import '../services/athena_trigger_service.dart';
import 'package:aegis_health/services/aegis_cascade_controller.dart';
import '../screens/curator_chat_screen.dart';

class VitalsActivityCheckModal extends StatelessWidget {
  const VitalsActivityCheckModal({super.key});

  /// Static method for quickly opening the modal window
  static Future<void> show(BuildContext context) {
    return showModalBottomSheet(
      context: context,
      isScrollControlled: true,
      backgroundColor: Colors.transparent,
      barrierColor: Colors.black.withOpacity(0.75),
      builder: (_) => const VitalsActivityCheckModal(),
    );
  }

  @override
  Widget build(BuildContext context) {
    final vitalsService = Provider.of<VitalsMonitorService>(context, listen: false);
    final currentPulse = vitalsService.currentEvent?.currentPulse ?? 100;

    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 24, vertical: 28),
      decoration: const BoxDecoration(
        color: Color(0xFF161B22),
        borderRadius: BorderRadius.vertical(top: Radius.circular(24)),
        border: Border(
          top: BorderSide(color: Colors.cyanAccent, width: 1.5),
        ),
      ),
      child: Column(
        mainAxisSize: MainAxisSize.min,
        children: [
          // Drag indicator (Handle)
          Container(
            width: 40,
            height: 4,
            decoration: BoxDecoration(
              color: Colors.white24,
              borderRadius: BorderRadius.circular(2),
            ),
          ),
          const SizedBox(height: 24),

          // Pulse icon
          Container(
            padding: const EdgeInsets.all(16),
            decoration: BoxDecoration(
              color: Colors.cyanAccent.withOpacity(0.1),
              shape: BoxShape.circle,
            ),
            child: const Icon(
              Icons.favorite_rounded,
              color: Colors.cyanAccent,
              size: 32,
            ),
          ),
          const SizedBox(height: 16),

          // Title and current value
          Text(
            "Heart rate spike detected: $currentPulse bpm",
            textAlign: TextAlign.center,
            style: const TextStyle(
              color: Colors.white,
              fontSize: 18,
              fontWeight: FontWeight.bold,
            ),
          ),
          const SizedBox(height: 12),

          // Main question
          const Text(
            "Are you exercising?",
            textAlign: TextAlign.center,
            style: TextStyle(
              color: Colors.white70,
              fontSize: 16,
            ),
          ),
          const SizedBox(height: 32),

          // "Yes" button (Physical activity)
          SizedBox(
            width: double.infinity,
            height: 50,
            child: ElevatedButton(
              style: ElevatedButton.styleFrom(
                backgroundColor: Colors.cyanAccent,
                foregroundColor: Colors.black,
                shape: RoundedRectangleBorder(
                  borderRadius: BorderRadius.circular(14),
                ),
                elevation: 0,
              ),
              onPressed: () {
                vitalsService.handleUserActivityResponse(
                  isActivity: true,
                  athenaTriggerService: context.read<AthenaTriggerService>(),
                  cascadeController: context.read<AegisCascadeController>(),
                );
                Navigator.pop(context);
              },
              child: const Text(
                "Yes",
                style: TextStyle(
                  fontSize: 16,
                  fontWeight: FontWeight.bold,
                ),
              ),
            ),
          ),
          const SizedBox(height: 12),

          // "No" button (Transition to Athena)
          SizedBox(
            width: double.infinity,
            height: 50,
            child: OutlinedButton(
              style: OutlinedButton.styleFrom(
                foregroundColor: Colors.white,
                side: const BorderSide(color: Colors.white24, width: 1.5),
                shape: RoundedRectangleBorder(
                  borderRadius: BorderRadius.circular(14),
                ),
              ),
              onPressed: () async {
                final triggerService = context.read<AthenaTriggerService>();
                final cascadeController = context.read<AegisCascadeController>();

                // Close the modal immediately
                Navigator.pop(context);

                // Run spike processing and call Athena
                await vitalsService.handleUserActivityResponse(
                  isActivity: false,
                  athenaTriggerService: triggerService,
                  cascadeController: cascadeController,
                );

                // Navigate to the chat screen with Athena
                if (context.mounted) {
                  Navigator.push(
                    context,
                    MaterialPageRoute(
                      builder: (_) => const CuratorChatScreen(),
                    ),
                  );
                }
              },
              child: const Text(
                "No, I'm at rest",
                style: TextStyle(
                  fontSize: 16,
                  fontWeight: FontWeight.w600,
                ),
              ),
            ),
          ),
          const SizedBox(height: 12),
        ],
      ),
    );
  }
}