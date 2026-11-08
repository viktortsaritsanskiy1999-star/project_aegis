import 'dart:io';
import 'dart:typed_data';
import 'package:file_picker/file_picker.dart';
import 'package:flutter/material.dart';
import 'package:path/path.dart' as p;
import 'package:provider/provider.dart';

import '../screens/doctor_document_view_screen.dart';
import '../services/api/aegis_api_client.dart';
import '../services/api/medical_ai_api_models.dart';

/// Documents for Your Doctor list section in Aegis Safe.
/// Displays all observation documents across patient cycles (current and historical).
/// Provides View, PDF, and DOCX download actions.
class DoctorDocumentsSection extends StatefulWidget {
  final AegisApiClient? apiClient;
  final ClientReferralStatus? referralStatus;
  final VoidCallback? onDocumentOpened;
  final DoctorDocumentFileSaver? customFileSaver;

  const DoctorDocumentsSection({
    super.key,
    this.apiClient,
    this.referralStatus,
    this.onDocumentOpened,
    this.customFileSaver,
  });

  @override
  State<DoctorDocumentsSection> createState() => _DoctorDocumentsSectionState();
}

class _DoctorDocumentsSectionState extends State<DoctorDocumentsSection> {
  bool _isLoading = true;
  String? _errorMessage;
  List<ClientReferralDocumentItem> _documents = [];
  final Map<String, bool> _downloadingPdf = {};
  final Map<String, bool> _downloadingDocx = {};

  AegisApiClient _resolveApiClient() {
    if (widget.apiClient != null) return widget.apiClient!;
    return context.read<AegisApiClient>();
  }

  @override
  void initState() {
    super.initState();
    _loadDocuments();
  }

  Future<void> _loadDocuments() async {
    setState(() {
      _isLoading = true;
      _errorMessage = null;
    });

    final client = _resolveApiClient();
    try {
      final res = await client.getReferralDocuments();
      if (!mounted) return;
      setState(() {
        _documents = res.documents;
        _isLoading = false;
        _errorMessage = null;
      });
    } catch (e) {
      if (!mounted) return;
      setState(() {
        _isLoading = false;
        _errorMessage = 'Failed to load doctor documents: $e';
      });
    }
  }

  Future<void> _downloadDocument(ClientReferralDocumentItem doc, String format) async {
    final cleanFormat = format.toLowerCase();
    final isPdf = cleanFormat == 'pdf';
    final reportId = doc.reportId;

    setState(() {
      if (isPdf) {
        _downloadingPdf[reportId] = true;
      } else {
        _downloadingDocx[reportId] = true;
      }
    });

    final client = _resolveApiClient();
    final fileName = 'aegis_doctor_summary_${doc.reportDate.replaceAll('-', '')}.$cleanFormat';

    try {
      final bytes = await client.exportReferralDocumentBytes(
        doc.reportId,
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
            _downloadingPdf.remove(reportId);
          } else {
            _downloadingDocx.remove(reportId);
          }
        });
      }
    }
  }

  void _openDocumentView(String reportId) {
    widget.onDocumentOpened?.call();
    Navigator.of(context).push(
      MaterialPageRoute(
        builder: (context) => DoctorDocumentViewScreen(
          reportId: reportId,
          apiClient: widget.apiClient,
          customFileSaver: widget.customFileSaver,
        ),
      ),
    );
  }

  @override
  Widget build(BuildContext context) {
    final ref = widget.referralStatus;
    final bool isGenerating = ref?.hasActiveReferral == true &&
        (ref?.documentGenerationState == 'generating' || ref?.status == 'generating_document');
    final bool isTemporarilyFailed = ref?.hasActiveReferral == true &&
        ref?.documentGenerationState == 'temporarily_failed';

    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Row(
          mainAxisAlignment: MainAxisAlignment.spaceBetween,
          children: [
            const Row(
              children: [
                Icon(Icons.medical_services_outlined, color: Colors.cyanAccent, size: 16),
                SizedBox(width: 8),
                Text(
                  'DOCUMENTS FOR YOUR DOCTOR',
                  style: TextStyle(
                    color: Colors.cyanAccent,
                    fontSize: 11,
                    fontWeight: FontWeight.bold,
                    letterSpacing: 1.1,
                  ),
                ),
              ],
            ),
            if (!_isLoading)
              IconButton(
                icon: const Icon(Icons.refresh, color: Colors.white38, size: 16),
                padding: EdgeInsets.zero,
                constraints: const BoxConstraints(),
                tooltip: 'Refresh documents',
                onPressed: _loadDocuments,
              ),
          ],
        ),
        const SizedBox(height: 8),

        if (_isLoading)
          const Padding(
            padding: EdgeInsets.symmetric(vertical: 12.0),
            child: Center(
              child: SizedBox(
                width: 20,
                height: 20,
                child: CircularProgressIndicator(strokeWidth: 2, color: Colors.cyanAccent),
              ),
            ),
          )
        else if (_errorMessage != null)
          Container(
            padding: const EdgeInsets.all(10),
            decoration: BoxDecoration(
              color: const Color(0xFF161B22),
              borderRadius: BorderRadius.circular(8),
              border: Border.all(color: Colors.amberAccent.withValues(alpha: 0.4)),
            ),
            child: Row(
              children: [
                const Icon(Icons.error_outline, color: Colors.amberAccent, size: 16),
                const SizedBox(width: 8),
                Expanded(
                  child: Text(
                    _errorMessage!,
                    style: const TextStyle(color: Colors.white70, fontSize: 11),
                  ),
                ),
                TextButton(
                  onPressed: _loadDocuments,
                  child: const Text('Retry', style: TextStyle(color: Colors.cyanAccent, fontSize: 11)),
                ),
              ],
            ),
          )
        else ...[
          // In-progress active referral banner inside Safe
          if (isGenerating)
            Container(
              margin: const EdgeInsets.only(bottom: 10),
              padding: const EdgeInsets.all(10),
              decoration: BoxDecoration(
                color: const Color(0xFF161B22),
                borderRadius: BorderRadius.circular(8),
                border: Border.all(color: Colors.blueAccent.withValues(alpha: 0.5)),
              ),
              child: const Row(
                children: [
                  SizedBox(
                    width: 14,
                    height: 14,
                    child: CircularProgressIndicator(strokeWidth: 2, color: Colors.cyanAccent),
                  ),
                  SizedBox(width: 8),
                  Expanded(
                    child: Text(
                      'An in-person medical consultation has been recommended. Your doctor summary is being prepared in the background.',
                      style: TextStyle(color: Colors.white70, fontSize: 11),
                    ),
                  ),
                ],
              ),
            ),

          if (isTemporarilyFailed)
            Container(
              margin: const EdgeInsets.only(bottom: 10),
              padding: const EdgeInsets.all(10),
              decoration: BoxDecoration(
                color: const Color(0xFF161B22),
                borderRadius: BorderRadius.circular(8),
                border: Border.all(color: Colors.amberAccent.withValues(alpha: 0.5)),
              ),
              child: const Row(
                children: [
                  Icon(Icons.schedule, color: Colors.amberAccent, size: 16),
                  SizedBox(width: 8),
                  Expanded(
                    child: Text(
                      'An in-person medical consultation has been recommended. Summary preparation is delayed. Please check back later.',
                      style: TextStyle(color: Colors.white70, fontSize: 11),
                    ),
                  ),
                ],
              ),
            ),

          if (_documents.isEmpty && !isGenerating && !isTemporarilyFailed)
            Container(
              width: double.infinity,
              padding: const EdgeInsets.all(12),
              decoration: BoxDecoration(
                color: const Color(0xFF161B22),
                borderRadius: BorderRadius.circular(8),
                border: Border.all(color: Colors.white10),
              ),
              child: const Text(
                'No doctor documents currently requested or generated. All observation records remain securely stored.',
                style: TextStyle(color: Colors.white38, fontSize: 11),
              ),
            ),

          ..._documents.map((doc) => _buildDocumentCard(doc)),
        ],
      ],
    );
  }

  bool _isCurrentCycleLatestDocument(ClientReferralDocumentItem doc) {
    if (doc.cycleStatus != 'active') return false;
    final refDocId = widget.referralStatus?.documentReportId;
    if (refDocId != null && refDocId.isNotEmpty) {
      return doc.reportId == refDocId;
    }
    for (final other in _documents) {
      if (other.cycleId == doc.cycleId && other.documentVersion > doc.documentVersion) {
        return false;
      }
    }
    return true;
  }

  Widget _buildDocumentCard(ClientReferralDocumentItem doc) {
    final bool isPdfDownloading = _downloadingPdf[doc.reportId] == true;
    final bool isDocxDownloading = _downloadingDocx[doc.reportId] == true;
    final bool isCurrent = _isCurrentCycleLatestDocument(doc);
    final bool isPriority = doc.urgency.toLowerCase() == 'priority';

    return Container(
      key: Key('doctor_doc_card_${doc.reportId}'),
      margin: const EdgeInsets.only(bottom: 10),
      padding: const EdgeInsets.all(12),
      decoration: BoxDecoration(
        color: const Color(0xFF161B22),
        borderRadius: BorderRadius.circular(8),
        border: Border.all(
          color: isCurrent ? Colors.cyanAccent.withValues(alpha: 0.4) : Colors.white12,
        ),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Expanded(
                child: Text(
                  doc.title,
                  style: const TextStyle(
                    color: Colors.white,
                    fontSize: 12.5,
                    fontWeight: FontWeight.bold,
                  ),
                ),
              ),
              const SizedBox(width: 8),
              Container(
                padding: const EdgeInsets.symmetric(horizontal: 6, vertical: 2),
                decoration: BoxDecoration(
                  color: isCurrent ? Colors.cyanAccent.withValues(alpha: 0.15) : Colors.white10,
                  borderRadius: BorderRadius.circular(4),
                  border: Border.all(
                    color: isCurrent ? Colors.cyanAccent : Colors.white24,
                    width: 0.8,
                  ),
                ),
                child: Text(
                  isCurrent ? 'Current Cycle' : 'Historical',
                  style: TextStyle(
                    color: isCurrent ? Colors.cyanAccent : Colors.white54,
                    fontSize: 9.5,
                    fontWeight: FontWeight.bold,
                  ),
                ),
              ),
            ],
          ),
          const SizedBox(height: 6),

          Row(
            children: [
              const Icon(Icons.calendar_today, color: Colors.white38, size: 11),
              const SizedBox(width: 4),
              Text(
                'Date: ${doc.reportDate}',
                style: const TextStyle(color: Colors.white70, fontSize: 10.5),
              ),
              if (isPriority) ...[
                const SizedBox(width: 10),
                Container(
                  padding: const EdgeInsets.symmetric(horizontal: 5, vertical: 1),
                  decoration: BoxDecoration(
                    color: Colors.orangeAccent.withValues(alpha: 0.15),
                    borderRadius: BorderRadius.circular(4),
                  ),
                  child: const Text(
                    'PRIORITY',
                    style: TextStyle(
                      color: Colors.orangeAccent,
                      fontSize: 9,
                      fontWeight: FontWeight.bold,
                    ),
                  ),
                ),
              ],
            ],
          ),

          if (doc.targetSpecialties.isNotEmpty) ...[
            const SizedBox(height: 4),
            Row(
              children: [
                const Icon(Icons.local_hospital_outlined, color: Colors.blueAccent, size: 11),
                const SizedBox(width: 4),
                Expanded(
                  child: Text(
                    'Recommended: ${doc.targetSpecialties.join(", ")}',
                    overflow: TextOverflow.ellipsis,
                    style: const TextStyle(color: Colors.blueAccent, fontSize: 10.5),
                  ),
                ),
              ],
            ),
          ],
          const SizedBox(height: 10),

          // Actions: View, PDF, DOCX
          Row(
            children: [
              Expanded(
                child: OutlinedButton.icon(
                  key: Key('view_doc_${doc.reportId}'),
                  style: OutlinedButton.styleFrom(
                    foregroundColor: Colors.cyanAccent,
                    side: const BorderSide(color: Colors.cyanAccent, width: 0.8),
                    padding: const EdgeInsets.symmetric(vertical: 6),
                    visualDensity: VisualDensity.compact,
                  ),
                  onPressed: () => _openDocumentView(doc.reportId),
                  icon: const Icon(Icons.visibility_outlined, size: 14),
                  label: const Text('View', style: TextStyle(fontSize: 11)),
                ),
              ),
              const SizedBox(width: 8),
              OutlinedButton.icon(
                key: Key('pdf_doc_${doc.reportId}'),
                style: OutlinedButton.styleFrom(
                  foregroundColor: Colors.cyanAccent,
                  side: const BorderSide(color: Colors.cyanAccent, width: 0.8),
                  padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 6),
                  visualDensity: VisualDensity.compact,
                ),
                onPressed: isPdfDownloading ? null : () => _downloadDocument(doc, 'pdf'),
                icon: isPdfDownloading
                    ? const SizedBox(
                        width: 12,
                        height: 12,
                        child: CircularProgressIndicator(strokeWidth: 1.5, color: Colors.cyanAccent),
                      )
                    : const Icon(Icons.picture_as_pdf_outlined, size: 14),
                label: const Text('PDF', style: TextStyle(fontSize: 11)),
              ),
              const SizedBox(width: 8),
              OutlinedButton.icon(
                key: Key('docx_doc_${doc.reportId}'),
                style: OutlinedButton.styleFrom(
                  foregroundColor: Colors.blueAccent,
                  side: const BorderSide(color: Colors.blueAccent, width: 0.8),
                  padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 6),
                  visualDensity: VisualDensity.compact,
                ),
                onPressed: isDocxDownloading ? null : () => _downloadDocument(doc, 'docx'),
                icon: isDocxDownloading
                    ? const SizedBox(
                        width: 12,
                        height: 12,
                        child: CircularProgressIndicator(strokeWidth: 1.5, color: Colors.blueAccent),
                      )
                    : const Icon(Icons.description_outlined, size: 14),
                label: const Text('DOCX', style: TextStyle(fontSize: 11)),
              ),
            ],
          ),
        ],
      ),
    );
  }
}
