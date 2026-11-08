import 'dart:io';
import 'dart:typed_data';
import 'package:file_picker/file_picker.dart';
import 'package:flutter/material.dart';
import 'package:path/path.dart' as p;
import 'package:provider/provider.dart';

import '../services/api/aegis_api_client.dart';
import '../services/api/medical_ai_api_models.dart';

/// Callback signature for saving exported document bytes via platform dialog.
typedef DoctorDocumentFileSaver = Future<String?> Function({
  required String dialogTitle,
  required String fileName,
  required List<String> allowedExtensions,
  required Uint8List bytes,
});

/// Screen displaying the complete client-safe doctor observation document in Aegis visual style.
/// Preserves all 7 clinical sections, original measurements, and units.
/// Excludes internal SERVER_ONLY and raw prompt data.
class DoctorDocumentViewScreen extends StatefulWidget {
  final String reportId;
  final AegisApiClient? apiClient;
  final DoctorDocumentFileSaver? customFileSaver;

  const DoctorDocumentViewScreen({
    super.key,
    required this.reportId,
    this.apiClient,
    this.customFileSaver,
  });

  @override
  State<DoctorDocumentViewScreen> createState() => _DoctorDocumentViewScreenState();
}

class _DoctorDocumentViewScreenState extends State<DoctorDocumentViewScreen> {
  bool _isLoading = true;
  String? _errorMessage;
  ClientReferralDocumentDetailResponse? _document;
  bool _isDownloadingPdf = false;
  bool _isDownloadingDocx = false;

  AegisApiClient _resolveApiClient() {
    if (widget.apiClient != null) return widget.apiClient!;
    return context.read<AegisApiClient>();
  }

  @override
  void initState() {
    super.initState();
    _loadDocument();
  }

  Future<void> _loadDocument() async {
    setState(() {
      _isLoading = true;
      _errorMessage = null;
    });

    final client = _resolveApiClient();
    try {
      final doc = await client.getReferralDocumentDetail(widget.reportId);
      if (!mounted) return;
      setState(() {
        _document = doc;
        _isLoading = false;
        _errorMessage = null;
      });
    } catch (e) {
      if (!mounted) return;
      setState(() {
        _isLoading = false;
        _errorMessage = 'Failed to load doctor document: $e';
      });
    }
  }

  Future<void> _downloadFile(String format) async {
    if (_document == null) return;
    final isPdf = format.toLowerCase() == 'pdf';
    setState(() {
      if (isPdf) {
        _isDownloadingPdf = true;
      } else {
        _isDownloadingDocx = true;
      }
    });

    final client = _resolveApiClient();
    final cleanFormat = format.toLowerCase();
    final fileName = 'aegis_doctor_summary_${_document!.reportDate.replaceAll('-', '')}.$cleanFormat';

    try {
      final bytes = await client.exportReferralDocumentBytes(
        _document!.reportId,
        format: cleanFormat,
      );

      final uint8Bytes = Uint8List.fromList(bytes);

      final String? savePath;
      if (widget.customFileSaver != null) {
        savePath = await widget.customFileSaver!(
          dialogTitle: 'Save Doctor Document ($cleanFormat)',
          fileName: fileName,
          allowedExtensions: [cleanFormat],
          bytes: uint8Bytes,
        );
      } else {
        savePath = await FilePicker.platform.saveFile(
          dialogTitle: 'Save Doctor Document ($cleanFormat)',
          fileName: fileName,
          type: FileType.custom,
          allowedExtensions: [cleanFormat],
          bytes: uint8Bytes,
        );
      }

      // If the user cancelled the system file dialog, do not write any file or show false success.
      if (savePath == null || savePath.trim().isEmpty) {
        return;
      }

      final file = File(savePath);
      file.writeAsBytesSync(uint8Bytes, flush: true);
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(
            content: Text('Document successfully saved: ${p.basename(savePath)}'),
            backgroundColor: const Color(0xFF161B22),
          ),
        );
      }
    } catch (e) {
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(
            content: Text('Failed to export $cleanFormat: $e'),
            backgroundColor: Colors.redAccent.shade700,
          ),
        );
      }
    } finally {
      if (mounted) {
        setState(() {
          if (isPdf) {
            _isDownloadingPdf = false;
          } else {
            _isDownloadingDocx = false;
          }
        });
      }
    }
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      backgroundColor: const Color(0xFF0D1117),
      appBar: AppBar(
        backgroundColor: const Color(0xFF161B22),
        elevation: 0,
        title: const Text(
          'DOCUMENT FOR YOUR DOCTOR',
          style: TextStyle(
            color: Colors.cyanAccent,
            fontSize: 13,
            fontWeight: FontWeight.bold,
            letterSpacing: 1.2,
          ),
        ),
        actions: [
          if (_document != null) ...[
            IconButton(
              icon: _isDownloadingPdf
                  ? const SizedBox(
                      width: 16,
                      height: 16,
                      child: CircularProgressIndicator(strokeWidth: 2, color: Colors.cyanAccent),
                    )
                  : const Icon(Icons.picture_as_pdf_outlined, color: Colors.cyanAccent, size: 20),
              tooltip: 'Download PDF',
              onPressed: _isDownloadingPdf ? null : () => _downloadFile('pdf'),
            ),
            IconButton(
              icon: _isDownloadingDocx
                  ? const SizedBox(
                      width: 16,
                      height: 16,
                      child: CircularProgressIndicator(strokeWidth: 2, color: Colors.blueAccent),
                    )
                  : const Icon(Icons.description_outlined, color: Colors.blueAccent, size: 20),
              tooltip: 'Download DOCX',
              onPressed: _isDownloadingDocx ? null : () => _downloadFile('docx'),
            ),
          ],
        ],
      ),
      body: _buildBody(),
    );
  }

  Widget _buildBody() {
    if (_isLoading) {
      return const Center(
        child: CircularProgressIndicator(color: Colors.cyanAccent),
      );
    }

    if (_errorMessage != null) {
      return Center(
        child: Padding(
          padding: const EdgeInsets.all(24.0),
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              const Icon(Icons.error_outline_rounded, color: Colors.amberAccent, size: 36),
              const SizedBox(height: 12),
              Text(
                _errorMessage!,
                textAlign: TextAlign.center,
                style: const TextStyle(color: Colors.white70, fontSize: 13),
              ),
              const SizedBox(height: 16),
              ElevatedButton.icon(
                style: ElevatedButton.styleFrom(backgroundColor: Colors.cyanAccent),
                onPressed: _loadDocument,
                icon: const Icon(Icons.refresh, color: Colors.black, size: 16),
                label: const Text('Retry', style: TextStyle(color: Colors.black)),
              ),
            ],
          ),
        ),
      );
    }

    final doc = _document!;
    final bool isPriority = doc.urgency.toLowerCase() == 'priority';

    return SingleChildScrollView(
      padding: const EdgeInsets.symmetric(horizontal: 18, vertical: 16),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          // Subhead label
          const Text(
            'PROJECT AEGIS :: CLINICAL OBSERVATION SERVICE',
            style: TextStyle(
              color: Colors.white38,
              fontSize: 10,
              fontWeight: FontWeight.bold,
              letterSpacing: 1.1,
            ),
          ),
          const SizedBox(height: 6),

          // Canonical Title
          Text(
            doc.title,
            style: const TextStyle(
              color: Colors.white,
              fontSize: 17,
              fontWeight: FontWeight.bold,
              height: 1.3,
            ),
          ),
          const SizedBox(height: 12),

          // Metadata Chips Strip
          Wrap(
            spacing: 8,
            runSpacing: 8,
            children: [
              _buildChip(
                label: 'Date: ${doc.reportDate}',
                icon: Icons.calendar_today_outlined,
                color: Colors.cyanAccent,
              ),
              _buildChip(
                label: isPriority ? 'Priority Consultation' : 'Routine Consultation',
                icon: isPriority ? Icons.priority_high_rounded : Icons.schedule_rounded,
                color: isPriority ? Colors.orangeAccent : Colors.greenAccent,
              ),
              _buildChip(
                label: doc.cycleStatus == 'active' ? 'Current Cycle' : 'Historical Cycle',
                icon: Icons.history_rounded,
                color: doc.cycleStatus == 'active' ? Colors.cyanAccent : Colors.white54,
              ),
              if (doc.targetSpecialties.isNotEmpty)
                _buildChip(
                  label: doc.targetSpecialties.join(', '),
                  icon: Icons.local_hospital_outlined,
                  color: Colors.blueAccent,
                ),
            ],
          ),
          const SizedBox(height: 16),

          // Informational Notice Callout Box
          Container(
            width: double.infinity,
            padding: const EdgeInsets.all(12),
            decoration: BoxDecoration(
              color: const Color(0xFF161B22),
              borderRadius: BorderRadius.circular(8),
              border: Border.all(color: Colors.amberAccent.withValues(alpha: 0.4)),
            ),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                const Row(
                  children: [
                    Icon(Icons.info_outline_rounded, color: Colors.amberAccent, size: 14),
                    SizedBox(width: 6),
                    Text(
                      'IMPORTANT INFORMATIONAL NOTICE',
                      style: TextStyle(
                        color: Colors.amberAccent,
                        fontSize: 11,
                        fontWeight: FontWeight.bold,
                      ),
                    ),
                  ],
                ),
                const SizedBox(height: 6),
                Text(
                  doc.informationalDisclaimer,
                  style: const TextStyle(color: Colors.white70, fontSize: 11, height: 1.4),
                ),
                const SizedBox(height: 6),
                const Text(
                  '(This document is an AI-generated health observation summary prepared solely to facilitate discussion with a licensed healthcare professional. It is NOT an official medical discharge summary and does NOT represent a confirmed clinical diagnosis.)',
                  style: TextStyle(color: Colors.white38, fontSize: 10, fontStyle: FontStyle.italic),
                ),
              ],
            ),
          ),
          const SizedBox(height: 18),

          // Executive Summary Box
          const Text(
            'EXECUTIVE SUMMARY FOR PATIENT',
            style: TextStyle(
              color: Colors.purpleAccent,
              fontSize: 11,
              fontWeight: FontWeight.bold,
              letterSpacing: 1.1,
            ),
          ),
          const SizedBox(height: 6),
          Container(
            width: double.infinity,
            padding: const EdgeInsets.all(12),
            decoration: BoxDecoration(
              color: const Color(0xFF161B22),
              borderRadius: BorderRadius.circular(8),
              border: Border.all(color: Colors.white12),
            ),
            child: Text(
              doc.executiveSummary,
              style: const TextStyle(color: Colors.white, fontSize: 12, height: 1.4),
            ),
          ),
          const SizedBox(height: 20),

          // 7 Clinical Observation Sections
          const Text(
            'CLINICAL OBSERVATION SECTIONS',
            style: TextStyle(
              color: Colors.cyanAccent,
              fontSize: 11,
              fontWeight: FontWeight.bold,
              letterSpacing: 1.1,
            ),
          ),
          const Divider(color: Colors.white24, height: 16),

          ...doc.sections.map((sec) => _buildSectionWidget(sec)),

          const SizedBox(height: 24),

          // Bottom Download Buttons
          Row(
            children: [
              Expanded(
                child: ElevatedButton.icon(
                  key: const Key('download_pdf_btn'),
                  style: ElevatedButton.styleFrom(
                    backgroundColor: const Color(0xFF1F2937),
                    foregroundColor: Colors.cyanAccent,
                    side: const BorderSide(color: Colors.cyanAccent, width: 1),
                    padding: const EdgeInsets.symmetric(vertical: 12),
                  ),
                  onPressed: _isDownloadingPdf ? null : () => _downloadFile('pdf'),
                  icon: _isDownloadingPdf
                      ? const SizedBox(
                          width: 14,
                          height: 14,
                          child: CircularProgressIndicator(strokeWidth: 2, color: Colors.cyanAccent),
                        )
                      : const Icon(Icons.picture_as_pdf_outlined, size: 16),
                  label: const Text('Download PDF', style: TextStyle(fontSize: 12)),
                ),
              ),
              const SizedBox(width: 12),
              Expanded(
                child: ElevatedButton.icon(
                  key: const Key('download_docx_btn'),
                  style: ElevatedButton.styleFrom(
                    backgroundColor: const Color(0xFF1F2937),
                    foregroundColor: Colors.blueAccent,
                    side: const BorderSide(color: Colors.blueAccent, width: 1),
                    padding: const EdgeInsets.symmetric(vertical: 12),
                  ),
                  onPressed: _isDownloadingDocx ? null : () => _downloadFile('docx'),
                  icon: _isDownloadingDocx
                      ? const SizedBox(
                          width: 14,
                          height: 14,
                          child: CircularProgressIndicator(strokeWidth: 2, color: Colors.blueAccent),
                        )
                      : const Icon(Icons.description_outlined, size: 16),
                  label: const Text('Download DOCX', style: TextStyle(fontSize: 12)),
                ),
              ),
            ],
          ),
          const SizedBox(height: 24),
        ],
      ),
    );
  }

  Widget _buildChip({
    required String label,
    required IconData icon,
    required Color color,
  }) {
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 4),
      decoration: BoxDecoration(
        color: const Color(0xFF161B22),
        borderRadius: BorderRadius.circular(6),
        border: Border.all(color: color.withValues(alpha: 0.5), width: 0.8),
      ),
      child: Row(
        mainAxisSize: MainAxisSize.min,
        children: [
          Icon(icon, color: color, size: 12),
          const SizedBox(width: 5),
          Text(
            label,
            style: TextStyle(color: color, fontSize: 10, fontWeight: FontWeight.bold),
          ),
        ],
      ),
    );
  }

  Widget _buildSectionWidget(ClientReferralDocumentSection sec) {
    return Container(
      margin: const EdgeInsets.only(bottom: 14),
      padding: const EdgeInsets.all(12),
      decoration: BoxDecoration(
        color: const Color(0xFF161B22),
        borderRadius: BorderRadius.circular(8),
        border: Border.all(color: Colors.white10),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(
            '${sec.sectionNumber}. ${sec.sectionTitle}',
            style: const TextStyle(
              color: Colors.cyanAccent,
              fontSize: 12,
              fontWeight: FontWeight.bold,
            ),
          ),
          const SizedBox(height: 8),
          if (sec.isBulletList && sec.bulletItems.isNotEmpty)
            ...sec.bulletItems.map(
              (item) => Padding(
                padding: const EdgeInsets.only(bottom: 5.0),
                child: Row(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    const Text('• ', style: TextStyle(color: Colors.cyanAccent, fontSize: 12)),
                    Expanded(
                      child: Text(
                        item,
                        style: const TextStyle(color: Colors.white, fontSize: 11.5, height: 1.35),
                      ),
                    ),
                  ],
                ),
              ),
            )
          else
            Text(
              sec.asPlainText,
              style: const TextStyle(color: Colors.white, fontSize: 11.5, height: 1.35),
            ),
        ],
      ),
    );
  }
}
