import 'package:flutter/material.dart';
import 'package:provider/provider.dart';
import 'package:file_picker/file_picker.dart';
import 'package:path/path.dart' as p;
import 'package:image_picker/image_picker.dart';
import '../services/vault_service.dart';
import '../services/lab_parser_service.dart';
import 'aegis_consilium_screen.dart';

class UploadAnalysisScreen extends StatefulWidget {
  const UploadAnalysisScreen({super.key});

  @override
  State<UploadAnalysisScreen> createState() => _UploadAnalysisScreenState();
}

class _UploadAnalysisScreenState extends State<UploadAnalysisScreen> {
  final LabParserService _parserService = LabParserService();

  bool _isLoading = false;
  String? _statusMessage;
  List<XFile> _selectedFiles = [];
  String _fileName = "Click to select report pages";

  Future<void> _pickFiles() async {
    try {
      FilePickerResult? result = await FilePicker.platform.pickFiles(
        type: FileType.custom,
        allowedExtensions: ['jpg', 'jpeg', 'png', 'webp', 'heic'],
        allowMultiple: true, // 👈 Разрешаем мультивыбор
      );

      if (result != null && result.files.isNotEmpty) {
        final validFiles = result.files
            .where((f) => f.path != null)
            .map((f) => XFile(f.path!))
            .toList();

        setState(() {
          _selectedFiles = validFiles;
          _fileName = _selectedFiles.length == 1
              ? p.basename(_selectedFiles.first.path)
              : "Selected files/pages: ${_selectedFiles.length}";
          _statusMessage = null;
        });
      }
    } catch (e) {
      setState(() => _statusMessage = "Error selecting files");
    }
  }

  Future<void> _startParsing() async {
    if (_selectedFiles.isEmpty) return;

    setState(() {
      _isLoading = true;
      _statusMessage = "🤖 Gemini Flash Vision is analyzing report pages (${_selectedFiles.length})...";
    });

    try {
      final vault = context.read<VaultService>();

      // Отправляем массив файлов в мультимодальный запрос
      bool success = await _parserService.parseImages(_selectedFiles, vault);

      if (mounted) {
        setState(() => _isLoading = false);
        if (success) {
          _showSuccessDialog();
        } else {
          setState(() => _statusMessage = "❌ Failed to extract metrics.");
        }
      }
    } catch (e) {
      if (mounted) {
        setState(() {
          _isLoading = false;
          _statusMessage = "❌ Processing error: $e";
        });
      }
    }
  }

  void _showSuccessDialog() {
    showDialog(
      context: context,
      barrierDismissible: false,
      builder: (context) => AlertDialog(
        backgroundColor: const Color(0xFF242424),
        title: const Text("Success!", style: TextStyle(color: Colors.white)),
        content: Text(
          "Data from ${_selectedFiles.length} pages recognized and merged into Vault.",
          style: const TextStyle(color: Colors.white70),
        ),
        actions: [
          TextButton(
            onPressed: () {
              Navigator.pushAndRemoveUntil(
                context,
                MaterialPageRoute(builder: (context) => const AegisConsiliumScreen()),
                    (r) => false,
              );
            },
            child: const Text("To Consilium", style: TextStyle(color: Colors.blueAccent)),
          )
        ],
      ),
    );
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      backgroundColor: const Color(0xFF1A1A1A),
      appBar: AppBar(
        title: const Text("Upload Lab Tests", style: TextStyle(color: Colors.white)),
        backgroundColor: const Color(0xFF242424),
      ),
      body: SafeArea(
        child: SingleChildScrollView(
          padding: const EdgeInsets.all(24.0),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              const Text(
                "Step 3: Lab Reports 📄",
                style: TextStyle(color: Colors.white, fontSize: 20, fontWeight: FontWeight.bold),
              ),
              const SizedBox(height: 10),
              const Text(
                "You can select multiple pages of the same examination at once",
                style: TextStyle(color: Colors.white54, fontSize: 13),
              ),
              const SizedBox(height: 20),
              GestureDetector(
                onTap: _isLoading ? null : _pickFiles,
                child: Container(
                  width: double.infinity,
                  height: 150,
                  decoration: BoxDecoration(
                    color: const Color(0xFF242424),
                    borderRadius: BorderRadius.circular(12),
                    border: Border.all(
                      color: _selectedFiles.isNotEmpty ? Colors.blueAccent : Colors.white10,
                      width: 1.5,
                    ),
                  ),
                  child: Column(
                    mainAxisAlignment: MainAxisAlignment.center,
                    children: [
                      Icon(
                        Icons.collections_outlined,
                        size: 42,
                        color: _selectedFiles.isNotEmpty ? Colors.blueAccent : Colors.white38,
                      ),
                      const SizedBox(height: 10),
                      Padding(
                        padding: const EdgeInsets.symmetric(horizontal: 16),
                        child: Text(
                          _fileName,
                          textAlign: TextAlign.center,
                          style: const TextStyle(color: Colors.white, fontSize: 14),
                        ),
                      ),
                    ],
                  ),
                ),
              ),
              const SizedBox(height: 20),
              if (_statusMessage != null)
                Center(
                  child: Text(
                    _statusMessage!,
                    textAlign: TextAlign.center,
                    style: const TextStyle(color: Colors.white70),
                  ),
                ),
              const SizedBox(height: 20),
              SizedBox(
                width: double.infinity,
                height: 55,
                child: ElevatedButton(
                  style: ElevatedButton.styleFrom(
                    backgroundColor: Colors.blueAccent,
                    disabledBackgroundColor: Colors.grey.shade800,
                  ),
                  onPressed: _isLoading || _selectedFiles.isEmpty ? null : _startParsing,
                  child: _isLoading
                      ? const CircularProgressIndicator(color: Colors.white)
                      : const Text("Analyze 🤖", style: TextStyle(fontSize: 16, color: Colors.white)),
                ),
              ),
            ],
          ),
        ),
      ),
    );
  }
}