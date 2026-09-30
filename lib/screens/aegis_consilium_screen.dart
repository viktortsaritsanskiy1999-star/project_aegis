import 'package:flutter/material.dart';
import 'package:provider/provider.dart';
import '../services/vault_service.dart';
import '../services/brain_service.dart';
import '../main.dart';

class AegisConsiliumScreen extends StatefulWidget {
  const AegisConsiliumScreen({super.key});

  @override
  State<AegisConsiliumScreen> createState() => _AegisConsiliumScreenState();
}

class _AegisConsiliumScreenState extends State<AegisConsiliumScreen> {
  bool _isProcessing = false;

  Future<void> _runConsilium() async {
    setState(() => _isProcessing = true);

    final vault = context.read<VaultService>();
    final brain = context.read<BrainService>();

    // 🔥 Run the FULL cascade: 8 systems -> Flash -> Pro
    await brain.runFullPipeline(vault);

    // After the entire cascade completes — navigate to Atlas
    if (mounted) {
      Navigator.pushReplacement(
        context,
        MaterialPageRoute(builder: (context) => const AtlasScreen()),
      );
    }
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      backgroundColor: Colors.black,
      body: SafeArea(
        child: Padding(
          padding: const EdgeInsets.all(24.0),
          child: Column(
            mainAxisAlignment: MainAxisAlignment.center,
            children: [
              const Icon(Icons.psychology_alt, size: 80, color: Colors.blueAccent),
              const SizedBox(height: 30),
              const Text("AEGIS :: CONSILIUM", style: TextStyle(color: Colors.blueAccent, fontSize: 24, fontWeight: FontWeight.bold, letterSpacing: 2)),
              const SizedBox(height: 20),
              const Text(
                "All data collected. The system is ready to activate 8 neural agents for pathophysiological profile analysis.",
                textAlign: TextAlign.center,
                style: TextStyle(color: Colors.white70, fontSize: 16),
              ),
              const SizedBox(height: 40),
              if (_isProcessing)
                const CircularProgressIndicator(color: Colors.orangeAccent)
              else
                ElevatedButton(
                  style: ElevatedButton.styleFrom(
                    backgroundColor: Colors.blueAccent.withOpacity(0.2),
                    side: const BorderSide(color: Colors.blueAccent),
                    padding: const EdgeInsets.symmetric(horizontal: 40, vertical: 15),
                  ),
                  onPressed: _runConsilium,
                  child: const Text("RUN ANALYSIS", style: TextStyle(fontSize: 16, fontWeight: FontWeight.bold)),
                ),
            ],
          ),
        ),
      ),
    );
  }
}