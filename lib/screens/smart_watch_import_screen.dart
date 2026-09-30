import 'dart:io';
import 'package:flutter/material.dart';
import 'package:provider/provider.dart';
import 'package:image_picker/image_picker.dart';
import '../services/vault_service.dart';
import '../services/brain_service.dart';
import 'upload_analysis_screen.dart';

class SmartWatchImportScreen extends StatefulWidget {
  const SmartWatchImportScreen({super.key});

  @override
  State<SmartWatchImportScreen> createState() => _SmartWatchImportScreenState();
}

class _SmartWatchImportScreenState extends State<SmartWatchImportScreen> {
  final TextEditingController _systolicController = TextEditingController();
  final TextEditingController _diastolicController = TextEditingController();
  final TextEditingController _pulseController = TextEditingController();
  final TextEditingController _spo2Controller = TextEditingController();

  String _ecgStatusText = "Upload ECG image";
  bool _isEcgUploaded = false;
  bool _isEcgAnalyzing = false; // Background recognition flag
  String? _parsedEcgText;

  @override
  void dispose() {
    _systolicController.dispose();
    _diastolicController.dispose();
    _pulseController.dispose();
    _spo2Controller.dispose();
    super.dispose();
  }

  // METHOD FOR DOWNLOADING AND ANALYZING ECG VIA VISION
  Future<void> _pickAndAnalyzeEcg() async {
    final ImagePicker picker = ImagePicker();
    final XFile? image = await picker.pickImage(source: ImageSource.gallery);

    if (image == null) return;

    setState(() {
      _isEcgAnalyzing = true;
      _ecgStatusText = "Analyzing ECG with neural network...";
    });

    try {
      final brainService = Provider.of<BrainService>(context, listen: false);
      // Send file to Vision model for decryption
      final String ecgResult = await brainService.analyzeEcgImage(File(image.path));

      if (!mounted) return;

      setState(() {
        _isEcgUploaded = true;
        _isEcgAnalyzing = false;
        _parsedEcgText = ecgResult;
        _ecgStatusText = "ECG decrypted ✓";
      });
    } catch (e) {
      if (!mounted) return;

      setState(() {
        _isEcgAnalyzing = false;
        _ecgStatusText = "ECG reading error. Please try again";
      });
    }
  }

  @override
  Widget build(BuildContext context) {
    final vault = Provider.of<VaultService>(context, listen: false);

    return Scaffold(
      backgroundColor: const Color(0xFF1A1A1A),
      body: SafeArea(
        child: SingleChildScrollView(
          padding: const EdgeInsets.symmetric(horizontal: 30, vertical: 40),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.center,
            children: [
              const Text(
                "Great! Base profile configured 🚀",
                textAlign: TextAlign.center,
                style: TextStyle(color: Colors.white, fontSize: 22, fontWeight: FontWeight.bold),
              ),
              const SizedBox(height: 15),
              const Text(
                "To unlock the full power of Aegis, we need vital markers. In the future, these data will be automatically calibrated from Samsung Health via your smartwatch. For now, we can enter them manually or upload them.",
                textAlign: TextAlign.center,
                style: TextStyle(color: Colors.white60, fontSize: 14, height: 1.5),
              ),
              const SizedBox(height: 40),

              // BLOCK 1: ECG
              _buildSectionTitle("1️⃣ Your ECG (Lead I / Image)"),
              const SizedBox(height: 10),
              Row(
                children: [
                  Expanded(
                    child: OutlinedButton.icon(
                      style: OutlinedButton.styleFrom(
                        side: BorderSide(color: _isEcgUploaded ? Colors.greenAccent : Colors.white24),
                        minimumSize: const Size(0, 50),
                      ),
                      icon: _isEcgAnalyzing
                          ? const SizedBox(
                          width: 18,
                          height: 18,
                          child: CircularProgressIndicator(strokeWidth: 2, color: Colors.greenAccent)
                      )
                          : Icon(
                          _isEcgUploaded ? Icons.check_circle_outline : Icons.add_photo_alternate_outlined,
                          color: _isEcgUploaded ? Colors.greenAccent : Colors.white
                      ),
                      label: Text(
                          _ecgStatusText,
                          style: TextStyle(color: _isEcgUploaded ? Colors.greenAccent : Colors.white, fontSize: 13)
                      ),
                      onPressed: _isEcgAnalyzing ? null : _pickAndAnalyzeEcg,
                    ),
                  ),
                  if (_isEcgUploaded || _isEcgAnalyzing) ...[
                    const SizedBox(width: 10),
                    IconButton(
                      icon: const Icon(Icons.close, color: Colors.redAccent),
                      onPressed: _isEcgAnalyzing ? null : () {
                        setState(() {
                          _ecgStatusText = "Upload ECG image";
                          _isEcgUploaded = false;
                          _parsedEcgText = null;
                        });
                      },
                    )
                  ]
                ],
              ),
              const SizedBox(height: 30),

              // BLOCK 2: Blood Pressure (BP)
              _buildSectionTitle("2️⃣ Blood Pressure (mmHg)"),
              const SizedBox(height: 10),
              Row(
                children: [
                  Expanded(
                    child: TextField(
                      controller: _systolicController,
                      keyboardType: TextInputType.number,
                      enabled: !_isEcgAnalyzing,
                      style: const TextStyle(color: Colors.white),
                      decoration: const InputDecoration(
                        hintText: "Sys (e.g. 120)",
                        hintStyle: TextStyle(color: Colors.white24),
                        enabledBorder: UnderlineInputBorder(borderSide: BorderSide(color: Colors.white24)),
                      ),
                    ),
                  ),
                  const SizedBox(width: 20),
                  const Text("/", style: TextStyle(color: Colors.white38, fontSize: 24)),
                  const SizedBox(width: 20),
                  Expanded(
                    child: TextField(
                      controller: _diastolicController,
                      keyboardType: TextInputType.number,
                      enabled: !_isEcgAnalyzing,
                      style: const TextStyle(color: Colors.white),
                      decoration: const InputDecoration(
                        hintText: "Dia (e.g. 80)",
                        hintStyle: TextStyle(color: Colors.white24),
                        enabledBorder: UnderlineInputBorder(borderSide: BorderSide(color: Colors.white24)),
                      ),
                    ),
                  ),
                ],
              ),
              const SizedBox(height: 30),

              // BLOCK 3: Pulse and Saturation
              Row(
                children: [
                  Expanded(
                    child: Column(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        _buildSectionTitle("3️⃣ Pulse (bpm)"),
                        TextField(
                          controller: _pulseController,
                          keyboardType: TextInputType.number,
                          enabled: !_isEcgAnalyzing,
                          style: const TextStyle(color: Colors.white),
                          decoration: const InputDecoration(
                            hintText: "E.g. 72",
                            hintStyle: TextStyle(color: Colors.white24),
                            enabledBorder: UnderlineInputBorder(borderSide: BorderSide(color: Colors.white24)),
                          ),
                        ),
                      ],
                    ),
                  ),
                  const SizedBox(width: 40),
                  Expanded(
                    child: Column(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        _buildSectionTitle("4️⃣ SpO₂ Saturation (%)"),
                        TextField(
                          controller: _spo2Controller,
                          keyboardType: TextInputType.number,
                          enabled: !_isEcgAnalyzing,
                          style: const TextStyle(color: Colors.white),
                          decoration: const InputDecoration(
                            hintText: "E.g. 98",
                            hintStyle: TextStyle(color: Colors.white24),
                            enabledBorder: UnderlineInputBorder(borderSide: BorderSide(color: Colors.white24)),
                          ),
                        ),
                      ],
                    ),
                  ),
                ],
              ),
              const SizedBox(height: 50),

              // SAVE DATA BUTTON (DISABLED DURING ANALYSIS)
              SizedBox(
                width: double.infinity,
                height: 55,
                child: ElevatedButton(
                  style: ElevatedButton.styleFrom(
                    backgroundColor: _isEcgAnalyzing
                        ? Colors.white10
                        : Colors.greenAccent.withOpacity(0.1),
                    disabledBackgroundColor: Colors.white10,
                    side: BorderSide(
                      color: _isEcgAnalyzing ? Colors.white12 : Colors.greenAccent,
                      width: 1,
                    ),
                    shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(12)),
                  ),
                  // 🔥 If analysis is running — pass null, Flutter freezes the button
                  onPressed: _isEcgAnalyzing
                      ? null
                      : () {
                    final int? sys = int.tryParse(_systolicController.text);
                    final int? dia = int.tryParse(_diastolicController.text);
                    final int? pulse = int.tryParse(_pulseController.text);
                    final int? spo2 = int.tryParse(_spo2Controller.text);

                    vault.updateSmartWatchMetrics(
                      systolic: (sys != null && dia != null) ? sys : null,
                      diastolic: (sys != null && dia != null) ? dia : null,
                      pulse: pulse,
                      spo2: spo2,
                      ecgSummary: _parsedEcgText,
                    );

                    _navigateToAtlas();
                  },
                  child: _isEcgAnalyzing
                      ? const Row(
                    mainAxisAlignment: MainAxisAlignment.center,
                    children: [
                      SizedBox(
                        width: 18,
                        height: 18,
                        child: CircularProgressIndicator(strokeWidth: 2, color: Colors.white38),
                      ),
                      SizedBox(width: 12),
                      Text("Analyzing image...", style: TextStyle(color: Colors.white38, fontSize: 15)),
                    ],
                  )
                      : const Text(
                    "Save markers ⌚",
                    style: TextStyle(color: Colors.greenAccent, fontSize: 16, fontWeight: FontWeight.bold),
                  ),
                ),
              ),
              const SizedBox(height: 15),

              // SKIP ALL BUTTON (ALSO FREEZES DURING ANALYSIS)
              TextButton(
                onPressed: _isEcgAnalyzing ? null : () => _navigateToAtlas(),
                child: Text(
                  "Skip this step",
                  style: TextStyle(color: _isEcgAnalyzing ? Colors.white12 : Colors.white38, fontSize: 14),
                ),
              ),
            ],
          ),
        ),
      ),
    );
  }

  Widget _buildSectionTitle(String title) {
    return Align(
      alignment: Alignment.centerLeft,
      child: Text(title, style: const TextStyle(color: Colors.white70, fontSize: 13, fontWeight: FontWeight.w500)),
    );
  }

  void _navigateToAtlas() {
    Navigator.pushReplacement(
        context,
        MaterialPageRoute(builder: (context) => const UploadAnalysisScreen())
    );
  }
}