import 'package:flutter/material.dart';
import '../services/api/medical_ai_api_models.dart';

/// Non-emergency informational banner displayed on AtlasScreen when a doctor referral is active.
/// Reflects server referral_status state honestly without triggering emergency alerts or blocking the UI.
class ReferralBanner extends StatelessWidget {
  final ClientReferralStatus? referralStatus;
  final VoidCallback onOpenSafe;
  final void Function(String reportId)? onOpenDocument;

  const ReferralBanner({
    super.key,
    required this.referralStatus,
    required this.onOpenSafe,
    this.onOpenDocument,
  });

  @override
  Widget build(BuildContext context) {
    final status = referralStatus;
    if (status == null || !status.hasActiveReferral) {
      return const SizedBox.shrink();
    }

    // "visit_confirmed": do not show active banner requiring doctor consultation
    if (status.status == 'visit_confirmed') {
      return const SizedBox.shrink();
    }

    final bool isReady = status.status == 'document_ready' ||
        status.documentGenerationState == 'ready';
    final bool isTemporarilyFailed = status.documentGenerationState == 'temporarily_failed';

    final String messageText;
    final String actionLabel;
    final IconData statusIcon;

    if (isReady) {
      messageText =
          'An in-person medical consultation has been recommended. Your doctor summary is available in Safe.';
      actionLabel = 'View';
      statusIcon = Icons.medical_services_outlined;
    } else if (isTemporarilyFailed) {
      messageText =
          'An in-person medical consultation has been recommended. Summary preparation is delayed. Please check Safe.';
      actionLabel = 'Safe';
      statusIcon = Icons.schedule_outlined;
    } else {
      // generating_document / generating
      messageText =
          'An in-person medical consultation has been recommended. Your doctor summary is being prepared.';
      actionLabel = 'Safe';
      statusIcon = Icons.hourglass_top_outlined;
    }

    return Container(
      key: const Key('referral_active_banner'),
      margin: const EdgeInsets.symmetric(horizontal: 16, vertical: 4),
      padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 8),
      decoration: BoxDecoration(
        color: const Color(0xFF0F1E36), // Calm navy-blue, strictly non-emergency
        borderRadius: BorderRadius.circular(10),
        border: Border.all(
          color: isReady
              ? Colors.cyanAccent.withValues(alpha: 0.6)
              : Colors.blueAccent.withValues(alpha: 0.5),
          width: 1,
        ),
        boxShadow: [
          BoxShadow(
            color: Colors.black.withValues(alpha: 0.3),
            blurRadius: 4,
            offset: const Offset(0, 2),
          ),
        ],
      ),
      child: Row(
        children: [
          Icon(
            statusIcon,
            color: isReady ? Colors.cyanAccent : Colors.lightBlueAccent,
            size: 18,
          ),
          const SizedBox(width: 10),
          Expanded(
            child: Text(
              messageText,
              style: const TextStyle(
                color: Colors.white,
                fontSize: 11,
                height: 1.3,
              ),
            ),
          ),
          const SizedBox(width: 8),
          TextButton(
            key: const Key('referral_banner_action_btn'),
            style: TextButton.styleFrom(
              backgroundColor: isReady
                  ? Colors.cyanAccent.withValues(alpha: 0.15)
                  : Colors.blueAccent.withValues(alpha: 0.15),
              foregroundColor: isReady ? Colors.cyanAccent : Colors.lightBlueAccent,
              padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 4),
              minimumSize: const Size(40, 28),
              shape: RoundedRectangleBorder(
                borderRadius: BorderRadius.circular(6),
                side: BorderSide(
                  color: isReady ? Colors.cyanAccent : Colors.lightBlueAccent,
                  width: 0.8,
                ),
              ),
            ),
            onPressed: () {
              if (isReady && status.documentReportId != null && onOpenDocument != null) {
                onOpenDocument!(status.documentReportId!);
              } else {
                onOpenSafe();
              }
            },
            child: Text(
              actionLabel,
              style: const TextStyle(fontSize: 10.5, fontWeight: FontWeight.bold),
            ),
          ),
        ],
      ),
    );
  }
}
