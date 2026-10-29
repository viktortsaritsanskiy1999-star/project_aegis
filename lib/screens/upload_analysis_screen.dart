import 'package:flutter/material.dart';
import 'package:provider/provider.dart';
import 'package:file_picker/file_picker.dart';
import 'package:path/path.dart' as p;
import 'package:image_picker/image_picker.dart';
import '../services/api/aegis_api_client.dart';
import '../services/api/medical_ai_api_models.dart';
import '../services/api/vitals_api_models.dart';
import '../services/auth/auth_models.dart';
import '../services/auth/auth_session_manager.dart';
import '../services/vault_service.dart';
import 'aegis_consilium_screen.dart';

class UploadAnalysisScreen extends StatefulWidget {
  final AegisApiClient? apiClient;
  final AuthSessionManager? authSessionManager;
  final List<XFile>? initialFiles;

  const UploadAnalysisScreen({
    super.key,
    this.apiClient,
    this.authSessionManager,
    this.initialFiles,
  });

  @override
  State<UploadAnalysisScreen> createState() => _UploadAnalysisScreenState();
}

class _UploadAnalysisScreenState extends State<UploadAnalysisScreen> {
  bool _isLoading = false;
  String? _statusMessage;
  String? _errorMessage;
  bool _isRetryable = false;
  List<XFile> _selectedFiles = [];
  String _fileName = "Click to select report pages";

  @override
  void initState() {
    super.initState();
    if (widget.initialFiles != null && widget.initialFiles!.isNotEmpty) {
      _selectedFiles = List.from(widget.initialFiles!);
      _fileName = _selectedFiles.length == 1
          ? p.basename(_selectedFiles.first.name)
          : "${_selectedFiles.length} pages selected";
    }
  }

  Future<void> _pickFiles() async {
    try {
      FilePickerResult? result = await FilePicker.platform.pickFiles(
        type: FileType.custom,
        allowedExtensions: ['jpg', 'jpeg', 'png', 'webp', 'heic', 'heif'],
        allowMultiple: true,
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
          _errorMessage = null;
          _isRetryable = false;
        });
      }
    } catch (e) {
      setState(() {
        _errorMessage = "Error selecting files: $e";
        _isRetryable = false;
      });
    }
  }

  Future<void> _startParsing() async {
    // 1. Double-click and empty guard
    if (_isLoading || _selectedFiles.isEmpty) return;

    setState(() {
      _isLoading = true;
      _statusMessage = "🤖 Server Vision OCR is analyzing report pages (${_selectedFiles.length})...";
      _errorMessage = null;
      _isRetryable = false;
    });

    try {
      // 2. Resolve client and session dependencies
      final client = widget.apiClient ?? context.read<AegisApiClient>();
      final authMgr = widget.authSessionManager ?? (mounted ? _tryReadAuthManager() : null);
      final vault = context.read<VaultService>();

      // 3. Convert XFiles to typed LabUploadFileItem objects
      final uploadItems = <LabUploadFileItem>[];
      for (final file in _selectedFiles) {
        uploadItems.add(await LabUploadFileItem.fromXFile(file));
      }

      // 4. Ensure required scope if session manager is available
      if (authMgr != null && authMgr.isAuthenticated) {
        final session = authMgr.currentSession;
        if (session != null && !session.hasScope(AegisScopes.cascadeIntake)) {
          final upgraded = await authMgr.ensureRequiredScopes(
            requiredScopes: const [AegisScopes.cascadeIntake],
          );
          if (!upgraded && mounted) {
            setState(() {
              _isLoading = false;
              _errorMessage = "Access denied: device session lacks required permission (cascade:intake).";
              _isRetryable = false;
            });
            return;
          }
        }
      }

      // 5. Patient attribution epoch fencing: capture patient before network dispatch
      final String? capturedPatientId = vault.activePatientId;

      // 6. Server Vision OCR intake dispatch
      final response = await client.uploadLabDocuments(files: uploadItems);

      // 7. Post-network epoch fence verification
      if (!mounted) return;
      if (vault.activePatientId != capturedPatientId) {
        setState(() {
          _isLoading = false;
          _statusMessage = null;
          _errorMessage = "Patient profile changed during upload; operation aborted.";
          _isRetryable = false;
        });
        return;
      }

      // 8. Server is authoritative source of truth: fetch updated ledger state
      LabLedgerStateResponse? ledgerState;
      try {
        ledgerState = await client.getLabLedgerState();
      } catch (e) {
        debugPrint('[UploadAnalysisScreen] Warning: getLabLedgerState failed after upload: $e');
      }

      if (!mounted) return;
      if (vault.activePatientId != capturedPatientId) return;

      // 9. Update secure patient Vault snapshot (fallback to intake response on ledger query error to prevent data loss)
      if (ledgerState != null) {
        vault.updateLabLedgerSnapshot(ledgerState);
      } else {
        vault.updateLabLedgerFromIntakeResponse(response);
      }

      setState(() {
        _isLoading = false;
        _statusMessage = "Successfully processed ${response.extractedCount} indicators.";
      });

      _showSuccessDialog(response, ledgerState ?? vault.cachedLabLedgerState);
    } on AegisTimeoutException catch (_) {
      if (mounted) {
        setState(() {
          _isLoading = false;
          _errorMessage = "Upload timed out. Please check your network connection and retry.";
          _isRetryable = true;
        });
      }
    } on AegisInsufficientScopeException catch (_) {
      if (mounted) {
        setState(() {
          _isLoading = false;
          _errorMessage = "Access denied: session lacks required scope (cascade:intake).";
          _isRetryable = false;
        });
      }
    } on AegisAuthException catch (_) {
      if (mounted) {
        setState(() {
          _isLoading = false;
          _errorMessage = "Authorization expired. Please re-pair your mobile device.";
          _isRetryable = false;
        });
      }
    } on AegisValidationException catch (e) {
      if (mounted) {
        setState(() {
          _isLoading = false;
          _errorMessage = "Validation error: ${e.message}";
          _isRetryable = false;
        });
      }
    } on AegisConflictException catch (_) {
      if (mounted) {
        setState(() {
          _isLoading = false;
          _errorMessage = "A concurrent upload or revision is in progress. Please retry.";
          _isRetryable = true;
        });
      }
    } on AegisNetworkException catch (e) {
      if (mounted) {
        setState(() {
          _isLoading = false;
          _errorMessage = "Network error: ${e.message}. Please retry.";
          _isRetryable = true;
        });
      }
    } on AegisServerException catch (e) {
      if (mounted) {
        setState(() {
          _isLoading = false;
          _errorMessage = "Server error (${e.statusCode}): ${e.message}";
          _isRetryable = true;
        });
      }
    } catch (e) {
      if (mounted) {
        setState(() {
          _isLoading = false;
          _errorMessage = "Processing error: $e";
          _isRetryable = true;
        });
      }
    }
  }

  AuthSessionManager? _tryReadAuthManager() {
    try {
      return context.read<AuthSessionManager>();
    } catch (_) {
      return null;
    }
  }

  void _showSuccessDialog(LabVisionIntakeClientResponse response, LabLedgerStateResponse? ledgerState) {
    showDialog(
      context: context,
      barrierDismissible: false,
      builder: (context) => AlertDialog(
        backgroundColor: const Color(0xFF242424),
        title: Row(
          children: [
            const Icon(Icons.check_circle, color: Colors.greenAccent, size: 24),
            const SizedBox(width: 8),
            Text(
              response.unconfirmedMarkers.isNotEmpty ? "Processed with Notices" : "Success!",
              style: const TextStyle(color: Colors.white, fontSize: 18),
            ),
          ],
        ),
        content: SingleChildScrollView(
          child: Column(
            mainAxisSize: MainAxisSize.min,
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Text(
                "Document data extracted and synchronized with Ledger (v${response.ledgerVersion}).",
                style: const TextStyle(color: Colors.white70, fontSize: 13),
              ),
              const SizedBox(height: 12),
              // Confirmed indicators summary
              Text(
                "✅ Confirmed indicators: ${response.confirmedMarkers.length}",
                style: const TextStyle(color: Colors.greenAccent, fontWeight: FontWeight.bold, fontSize: 14),
              ),
              if (response.confirmedMarkers.isNotEmpty) ...[
                const SizedBox(height: 4),
                Wrap(
                  spacing: 6,
                  runSpacing: 4,
                  children: response.confirmedMarkers.map((m) {
                    final unitStr = m.unit != null ? ' ${m.unit}' : '';
                    return Chip(
                      backgroundColor: const Color(0xFF1E2D1E),
                      side: BorderSide(color: Colors.green.shade800),
                      labelPadding: const EdgeInsets.symmetric(horizontal: 4),
                      label: Text(
                        "${m.originalName}: ${m.rawValue}$unitStr",
                        style: const TextStyle(color: Colors.white70, fontSize: 11),
                      ),
                    );
                  }).toList(),
                ),
              ],
              // Unconfirmed / ambiguous indicators warning
              if (response.unconfirmedMarkers.isNotEmpty) ...[
                const SizedBox(height: 16),
                Container(
                  padding: const EdgeInsets.all(10),
                  decoration: BoxDecoration(
                    color: const Color(0xFF332511),
                    borderRadius: BorderRadius.circular(8),
                    border: Border.all(color: Colors.amber.shade700, width: 1),
                  ),
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Row(
                        children: [
                          Icon(Icons.warning_amber_rounded, color: Colors.amber.shade400, size: 18),
                          const SizedBox(width: 6),
                          Text(
                            "Requires Verification (${response.unconfirmedMarkers.length})",
                            style: TextStyle(color: Colors.amber.shade300, fontWeight: FontWeight.bold, fontSize: 13),
                          ),
                        ],
                      ),
                      const SizedBox(height: 6),
                      const Text(
                        "The following indicators were flagged as ambiguous or low-confidence. They are NOT confirmed in the active ledger history:",
                        style: TextStyle(color: Colors.white70, fontSize: 11),
                      ),
                      const SizedBox(height: 6),
                      ...response.unconfirmedMarkers.map((m) {
                        final note = m.uncertaintyNote ?? 'Recognition uncertain';
                        return Padding(
                          padding: const EdgeInsets.symmetric(vertical: 2),
                          child: Text(
                            "• ${m.originalName}: ${m.rawValue} ($note)",
                            style: TextStyle(color: Colors.amber.shade200, fontSize: 11),
                          ),
                        );
                      }),
                    ],
                  ),
                ),
              ],
            ],
          ),
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
          ),
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
              if (_statusMessage != null && _errorMessage == null)
                Center(
                  child: Text(
                    _statusMessage!,
                    textAlign: TextAlign.center,
                    style: const TextStyle(color: Colors.white70),
                  ),
                ),
              if (_errorMessage != null)
                Center(
                  child: Padding(
                    padding: const EdgeInsets.symmetric(horizontal: 8.0),
                    child: Text(
                      _errorMessage!,
                      textAlign: TextAlign.center,
                      style: const TextStyle(color: Colors.redAccent, fontSize: 13),
                    ),
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
              if (_errorMessage != null && _isRetryable && !_isLoading && _selectedFiles.isNotEmpty) ...[
                const SizedBox(height: 12),
                SizedBox(
                  width: double.infinity,
                  height: 48,
                  child: OutlinedButton.icon(
                    style: OutlinedButton.styleFrom(
                      foregroundColor: Colors.blueAccent,
                      side: const BorderSide(color: Colors.blueAccent),
                    ),
                    onPressed: _startParsing,
                    icon: const Icon(Icons.refresh, size: 18),
                    label: const Text("Retry Upload", style: TextStyle(fontSize: 15)),
                  ),
                ),
              ],
            ],
          ),
        ),
      ),
    );
  }
}
