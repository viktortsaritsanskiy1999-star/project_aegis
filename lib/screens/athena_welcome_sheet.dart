import 'package:flutter/material.dart';

class AthenaWelcomeSheet extends StatelessWidget {
  final VoidCallback onConnect;
  final bool isNeoReassessment;
  final String? customTitle;
  final String? customSubtitle;
  final String? customButtonText;

  const AthenaWelcomeSheet({
    super.key,
    required this.onConnect,
    this.isNeoReassessment = false,
    this.customTitle,
    this.customSubtitle,
    this.customButtonText,
  });

  static Future<void> show(
    BuildContext context, {
    required VoidCallback onConnect,
    bool isNeoReassessment = false,
    String? customTitle,
    String? customSubtitle,
    String? customButtonText,
  }) {
    return showModalBottomSheet(
      context: context,
      isScrollControlled: true,
      backgroundColor: Colors.transparent,
      barrierColor: const Color(0xB3000000), // Equivalent to black70
      builder: (context) => AthenaWelcomeSheet(
        onConnect: onConnect,
        isNeoReassessment: isNeoReassessment,
        customTitle: customTitle,
        customSubtitle: customSubtitle,
        customButtonText: customButtonText,
      ),
    );
  }

  @override
  Widget build(BuildContext context) {
    final title = customTitle ??
        (isNeoReassessment
            ? 'Thank you for updating your health information!'
            : 'Data processed!');
    final subtitle = customSubtitle ??
        (isNeoReassessment
            ? 'Athena is waiting for you.'
            : 'The medical board has finished analyzing your metrics. Now we know a little more about your health.\n\nJust go about your business, and we will always be near. Shall I introduce you to Athena?');
    final buttonText = customButtonText ??
        (isNeoReassessment ? 'Go to Chat' : 'Meet Athena');

    return Container(
      decoration: const BoxDecoration(
        color: Color(0xFF0D1117),
        borderRadius: BorderRadius.vertical(top: Radius.circular(24)),
        border: Border(
          top: BorderSide(color: Color(0xFF00FFFF), width: 1.5),
        ),
      ),
      child: SafeArea(
        child: SingleChildScrollView(
          padding: const EdgeInsets.all(24),
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              Container(
                width: 40,
                height: 4,
                decoration: BoxDecoration(
                  color: Colors.white24,
                  borderRadius: BorderRadius.circular(2),
                ),
              ),
              const SizedBox(height: 24),
              Container(
                padding: const EdgeInsets.all(16),
                decoration: BoxDecoration(
                  shape: BoxShape.circle,
                  color: const Color(0xFF00FFFF).withOpacity(0.1),
                  border: Border.all(color: const Color(0xFF00FFFF), width: 1.5),
                ),
                child: const Icon(
                  Icons.auto_awesome,
                  size: 36,
                  color: Color(0xFF00FFFF),
                ),
              ),
              const SizedBox(height: 20),
              Text(
                title,
                style: const TextStyle(
                  color: Colors.white,
                  fontSize: 22,
                  fontWeight: FontWeight.bold,
                ),
                textAlign: TextAlign.center,
              ),
              const SizedBox(height: 12),
              Text(
                subtitle,
                style: const TextStyle(
                  color: Colors.white70,
                  fontSize: 15,
                  height: 1.4,
                ),
                textAlign: TextAlign.center,
              ),
              const SizedBox(height: 28),
              SizedBox(
                width: double.infinity,
                height: 52,
                child: ElevatedButton(
                  key: const Key('athena_welcome_connect_btn'),
                  onPressed: () {
                    Navigator.of(context).pop();
                    onConnect();
                  },
                  style: ElevatedButton.styleFrom(
                    backgroundColor: const Color(0xFF00FFFF),
                    foregroundColor: Colors.black,
                    shape: RoundedRectangleBorder(
                      borderRadius: BorderRadius.circular(12),
                    ),
                    elevation: 0,
                  ),
                  child: Row(
                    mainAxisAlignment: MainAxisAlignment.center,
                    children: [
                      Text(
                        buttonText,
                        style: const TextStyle(
                          fontSize: 16,
                          fontWeight: FontWeight.bold,
                        ),
                      ),
                      const SizedBox(width: 8),
                      const Icon(Icons.arrow_forward_rounded, size: 20),
                    ],
                  ),
                ),
              ),
              const SizedBox(height: 12),
              TextButton(
                onPressed: () => Navigator.of(context).pop(),
                child: const Text(
                  'Explore Main Screen',
                  style: TextStyle(color: Colors.white38, fontSize: 14),
                ),
              ),
            ],
          ),
        ),
      ),
    );
  }
}