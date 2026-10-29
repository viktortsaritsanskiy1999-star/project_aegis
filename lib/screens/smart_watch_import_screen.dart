import 'package:flutter/material.dart';
import 'package:image_picker/image_picker.dart';
import 'package:provider/provider.dart';
import '../services/api/aegis_api_client.dart';
import '../services/api/medical_ai_api_models.dart';
import '../services/api/vitals_api_models.dart';
import '../services/auth/auth_models.dart';
import '../services/auth/auth_session_manager.dart';
import '../services/vault_service.dart';
import 'upload_analysis_screen.dart';

class SmartWatchImportScreen extends StatefulWidget {
  final AegisApiClient? apiClient;
  final AuthSessionManager? authSessionManager;
  final XFile? initialEcgFile;

  const SmartWatchImportScreen({
    super.key,
    this.apiClient,
    this.authSessionManager,
    this.initialEcgFile,
  });

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
  bool _isEcgAnalyzing = false;
  bool _isLocalPersistenceFailed = false;
  String? _errorMessage;
  bool _isRetryable = false;
  XFile? _selectedImageFile;
  EcgVisionIntakeClientResponse? _latestEcgResponse;

  @override
  void initState() {
    super.initState();
    if (widget.initialEcgFile != null) {
      _selectedImageFile = widget.initialEcgFile;
      _processSelectedEcgFile(widget.initialEcgFile!);
    }
  }

  @override
  void dispose() {
    _systolicController.dispose();
    _diastolicController.dispose();
    _pulseController.dispose();
    _spo2Controller.dispose();
    super.dispose();
  }

  AuthSessionManager? _tryReadAuthManager() {
    try {
      return Provider.of<AuthSessionManager>(context, listen: false);
    } catch (_) {
      return null;
    }
  }

  Future<void> _pickAndAnalyzeEcg() async {
    if (_isEcgAnalyzing) return;

    final ImagePicker picker = ImagePicker();
    final XFile? image = await picker.pickImage(source: ImageSource.gallery);

    if (image == null) return;
    _selectedImageFile = image;
    await _processSelectedEcgFile(image);
  }

  Future<void> _processSelectedEcgFile(XFile image) async {
    if (_isEcgAnalyzing) return;

    setState(() {
      _isEcgAnalyzing = true;
      _ecgStatusText = "Analyzing ECG with server Vision...";
      _errorMessage = null;
      _isRetryable = false;
    });

    try {
      final client = widget.apiClient ?? context.read<AegisApiClient>();
      final authMgr = widget.authSessionManager ?? (mounted ? _tryReadAuthManager() : null);
      final vault = context.read<VaultService>();

      // 1. Client-side pre-validation
      final imageBytes = await image.readAsBytes();
      if (imageBytes.isEmpty) {
        throw const AegisValidationException('Selected ECG image file is empty.');
      }
      if (imageBytes.length > 10 * 1024 * 1024) {
        throw AegisValidationException(
          'Selected image exceeds the maximum allowed size of 10 MB (${(imageBytes.length / (1024 * 1024)).toStringAsFixed(1)} MB).',
        );
      }
      final filename = image.name.isNotEmpty
          ? image.name
          : (image.path.isNotEmpty ? image.path.split(RegExp(r'[\\/]')).last : 'ecg_strip.jpg');

      if (!EcgUploadFileItem.isSupportedFormat(filename, imageBytes)) {
        throw AegisValidationException(
          'File "$filename" has an unsupported format. Supported formats: JPEG, PNG, WEBP, HEIC.',
        );
      }

      // 2. Ensure cascade:intake scope if session is authenticated
      if (authMgr != null && authMgr.isAuthenticated) {
        final session = authMgr.currentSession;
        if (session != null && !session.hasScope(AegisScopes.cascadeIntake)) {
          final upgraded = await authMgr.ensureRequiredScopes(
            requiredScopes: const [AegisScopes.cascadeIntake],
          );
          if (!upgraded && mounted) {
            setState(() {
              _isEcgAnalyzing = false;
              _errorMessage = "Access denied: device session lacks required permission (cascade:intake).";
              _isRetryable = false;
              _ecgStatusText = "Access denied (403)";
            });
            return;
          }
        }
      }

      // 3. Capture patient session epoch before network call
      final String? capturedPatientId = vault.activePatientId;
      final int capturedEpoch = vault.sessionEpoch;

      // 4. Dispatch upload to server
      final ecgItem = EcgUploadFileItem(
        filename: filename,
        bytes: imageBytes,
      );
      final response = await client.uploadEcgDocument(file: ecgItem);

      if (!mounted) return;

      // 5. Patient session epoch fencing check
      if (vault.activePatientId != capturedPatientId || vault.sessionEpoch != capturedEpoch) {
        setState(() {
          _isEcgAnalyzing = false;
          _errorMessage = "Patient session changed during upload. Upload discarded.";
          _isRetryable = false;
        });
        return;
      }

      // 6. Persist to Vault with local confirmation check
      final bool savedLocally = await vault.recordEcgIntake(
        response: response,
        localFilename: filename,
      );

      if (!mounted) return;

      // Re-verify patient session epoch after local persistence
      if (vault.activePatientId != capturedPatientId || vault.sessionEpoch != capturedEpoch) {
        setState(() {
          _isEcgAnalyzing = false;
          _errorMessage = "Patient session changed during persistence. Upload discarded.";
          _isRetryable = false;
          _isLocalPersistenceFailed = false;
        });
        return;
      }

      if (savedLocally) {
        setState(() {
          _isEcgUploaded = true;
          _isEcgAnalyzing = false;
          _isLocalPersistenceFailed = false;
          _latestEcgResponse = response;
          _errorMessage = null;
          _isRetryable = false;
          if (response.isUnreadable || response.isPoorQuality) {
            _ecgStatusText = "ECG recorded (${response.qualityDisplay}) ⚠️";
          } else {
            _ecgStatusText = "ECG recorded on server (v${response.intakeVersion}) ✓";
          }
        });
      } else {
        setState(() {
          _isEcgUploaded = false;
          _isEcgAnalyzing = false;
          _isLocalPersistenceFailed = true;
          _latestEcgResponse = response;
          _errorMessage = "ECG processed on server (v${response.intakeVersion}), but local secure vault save failed.";
          _isRetryable = false;
          _ecgStatusText = "Server OK, local save failed ⚠️";
        });
      }
    } on AegisValidationException catch (e) {
      if (!mounted) return;
      setState(() {
        _isEcgAnalyzing = false;
        _errorMessage = e.message;
        _isRetryable = false;
        _isLocalPersistenceFailed = false;
        _ecgStatusText = "Invalid file. Select another image.";
      });
    } on AegisForbiddenException catch (e) {
      if (!mounted) return;
      setState(() {
        _isEcgAnalyzing = false;
        _errorMessage = "Access denied (403): ${e.message}";
        _isRetryable = false;
        _isLocalPersistenceFailed = false;
        _ecgStatusText = "Access denied (403)";
      });
    } on AegisAuthException {
      if (!mounted) return;
      setState(() {
        _isEcgAnalyzing = false;
        _errorMessage = "Authentication failed (401). Please pair your device.";
        _isRetryable = false;
        _isLocalPersistenceFailed = false;
        _ecgStatusText = "Unauthorized (401)";
      });
    } on AegisTimeoutException catch (e) {
      if (!mounted) return;
      setState(() {
        _isEcgAnalyzing = false;
        _errorMessage = "Upload timed out: ${e.message}. Selected image is preserved.";
        _isRetryable = true;
        _isLocalPersistenceFailed = false;
        _ecgStatusText = "Timeout. Tap Retry Upload.";
      });
    } on AegisNetworkException catch (e) {
      if (!mounted) return;
      setState(() {
        _isEcgAnalyzing = false;
        _errorMessage = "Network connection failed: ${e.message}. Selected image is preserved.";
        _isRetryable = true;
        _isLocalPersistenceFailed = false;
        _ecgStatusText = "Offline / Connection error.";
      });
    } on AegisServerException catch (e) {
      if (!mounted) return;
      final retryable = e.statusCode == 502 || e.statusCode == 503 || e.statusCode == 504;
      setState(() {
        _isEcgAnalyzing = false;
        _errorMessage = "Server error (${e.statusCode}): ${e.message}";
        _isRetryable = retryable;
        _isLocalPersistenceFailed = false;
        _ecgStatusText = "Server error (${e.statusCode})";
      });
    } catch (e) {
      if (!mounted) return;
      setState(() {
        _isEcgAnalyzing = false;
        _errorMessage = "ECG upload error: $e";
        _isRetryable = true;
        _isLocalPersistenceFailed = false;
        _ecgStatusText = "Error during upload";
      });
    }
  }

  Future<void> _recoverLatestEcg() async {
    if (_isEcgAnalyzing) return;

    setState(() {
      _isEcgAnalyzing = true;
      _ecgStatusText = "Recovering latest ECG from server...";
      _errorMessage = null;
    });

    try {
      final client = widget.apiClient ?? context.read<AegisApiClient>();
      final vault = context.read<VaultService>();
      final capturedPatientId = vault.activePatientId;
      final capturedEpoch = vault.sessionEpoch;

      final latestResponse = await client.getLatestEcg();

      if (!mounted) return;
      if (vault.activePatientId != capturedPatientId || vault.sessionEpoch != capturedEpoch) {
        setState(() {
          _isEcgAnalyzing = false;
          _errorMessage = "Patient session changed during recovery.";
          _isLocalPersistenceFailed = false;
        });
        return;
      }

      final bool saved = await vault.recordEcgIntake(
        response: latestResponse,
        localFilename: _selectedImageFile?.name,
      );

      if (!mounted) return;
      if (vault.activePatientId != capturedPatientId || vault.sessionEpoch != capturedEpoch) {
        setState(() {
          _isEcgAnalyzing = false;
          _errorMessage = "Patient session changed during storage save.";
          _isLocalPersistenceFailed = false;
        });
        return;
      }

      if (saved) {
        setState(() {
          _isEcgUploaded = true;
          _isEcgAnalyzing = false;
          _isLocalPersistenceFailed = false;
          _latestEcgResponse = latestResponse;
          _errorMessage = null;
          _isRetryable = false;
          if (latestResponse.isUnreadable || latestResponse.isPoorQuality) {
            _ecgStatusText = "ECG recorded (${latestResponse.qualityDisplay}) ⚠️";
          } else {
            _ecgStatusText = "ECG recorded on server (v${latestResponse.intakeVersion}) ✓";
          }
        });
      } else {
        setState(() {
          _isEcgUploaded = false;
          _isEcgAnalyzing = false;
          _isLocalPersistenceFailed = true;
          _latestEcgResponse = latestResponse;
          _errorMessage = "Local secure vault save failed again. Please check device storage.";
          _ecgStatusText = "Server OK, local save failed ⚠️";
        });
      }
    } on AegisForbiddenException catch (e) {
      if (!mounted) return;
      setState(() {
        _isEcgAnalyzing = false;
        _errorMessage = "Access denied (403): ${e.message}";
        _isLocalPersistenceFailed = false;
      });
    } on AegisAuthException {
      if (!mounted) return;
      setState(() {
        _isEcgAnalyzing = false;
        _errorMessage = "Authentication failed (401). Please pair your device.";
        _isLocalPersistenceFailed = false;
      });
    } on AegisNetworkException catch (e) {
      if (!mounted) return;
      setState(() {
        _isEcgAnalyzing = false;
        _errorMessage = "Network connection failed: ${e.message}";
      });
    } catch (e) {
      if (!mounted) return;
      setState(() {
        _isEcgAnalyzing = false;
        _errorMessage = "Recovery error: $e";
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
                        side: BorderSide(
                          color: _isEcgUploaded
                              ? (_latestEcgResponse != null && _latestEcgResponse!.requiresVerification
                                  ? Colors.amberAccent
                                  : Colors.greenAccent)
                              : Colors.white24,
                        ),
                        minimumSize: const Size(0, 50),
                      ),
                      icon: _isEcgAnalyzing
                          ? const SizedBox(
                              width: 18,
                              height: 18,
                              child: CircularProgressIndicator(strokeWidth: 2, color: Colors.greenAccent),
                            )
                          : Icon(
                              _isEcgUploaded
                                  ? (_latestEcgResponse != null && _latestEcgResponse!.requiresVerification
                                      ? Icons.warning_amber_rounded
                                      : Icons.check_circle_outline)
                                  : Icons.add_photo_alternate_outlined,
                              color: _isEcgUploaded
                                  ? (_latestEcgResponse != null && _latestEcgResponse!.requiresVerification
                                      ? Colors.amberAccent
                                      : Colors.greenAccent)
                                  : Colors.white,
                            ),
                      label: Text(
                        _ecgStatusText,
                        style: TextStyle(
                          color: _isEcgUploaded
                              ? (_latestEcgResponse != null && _latestEcgResponse!.requiresVerification
                                  ? Colors.amberAccent
                                  : Colors.greenAccent)
                              : Colors.white,
                          fontSize: 13,
                        ),
                      ),
                      onPressed: _isEcgAnalyzing ? null : _pickAndAnalyzeEcg,
                    ),
                  ),
                  if (_isEcgUploaded || _isEcgAnalyzing || _selectedImageFile != null || _isLocalPersistenceFailed) ...[
                    const SizedBox(width: 10),
                    IconButton(
                      icon: const Icon(Icons.close, color: Colors.redAccent),
                      onPressed: _isEcgAnalyzing
                          ? null
                          : () {
                              setState(() {
                                _ecgStatusText = "Upload ECG image";
                                _isEcgUploaded = false;
                                _selectedImageFile = null;
                                _latestEcgResponse = null;
                                _errorMessage = null;
                                _isRetryable = false;
                                _isLocalPersistenceFailed = false;
                              });
                            },
                    ),
                  ],
                ],
              ),

              // ECG DETAILS CARD (ONLY CLIENT-PERMITTED METADATA)
              if (_latestEcgResponse != null) ...[
                const SizedBox(height: 12),
                Container(
                  width: double.infinity,
                  padding: const EdgeInsets.all(12),
                  decoration: BoxDecoration(
                    color: const Color(0xFF222222),
                    borderRadius: BorderRadius.circular(8),
                    border: Border.all(
                      color: _latestEcgResponse!.requiresVerification
                          ? Colors.amber.shade700
                          : Colors.green.shade800,
                      width: 1,
                    ),
                  ),
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Row(
                        children: [
                          Icon(
                            _latestEcgResponse!.requiresVerification
                                ? Icons.info_outline
                                : Icons.verified_outlined,
                            size: 16,
                            color: _latestEcgResponse!.requiresVerification
                                ? Colors.amberAccent
                                : Colors.greenAccent,
                          ),
                          const SizedBox(width: 6),
                          Text(
                            "ECG Intake Record (v${_latestEcgResponse!.intakeVersion})",
                            style: const TextStyle(color: Colors.white, fontWeight: FontWeight.bold, fontSize: 13),
                          ),
                        ],
                      ),
                      const SizedBox(height: 8),
                      Text(
                        "• Type: ${_latestEcgResponse!.recordingTypeDisplay}",
                        style: const TextStyle(color: Colors.white70, fontSize: 12),
                      ),
                      const SizedBox(height: 4),
                      Text(
                        "• Technical Quality: ${_latestEcgResponse!.qualityDisplay}",
                        style: TextStyle(
                          color: _latestEcgResponse!.requiresVerification
                              ? Colors.amberAccent
                              : Colors.white70,
                          fontSize: 12,
                        ),
                      ),
                      const SizedBox(height: 4),
                      Text(
                        "• Server Upload Time: ${_latestEcgResponse!.createdAtUtc}",
                        style: const TextStyle(color: Colors.white54, fontSize: 11),
                      ),
                      const SizedBox(height: 4),
                      Text(
                        "• Consilium Status: ${_latestEcgResponse!.primaryConsiliumConcluded ? 'Concluded' : 'Pending Consilium synthesis'}",
                        style: const TextStyle(color: Colors.white54, fontSize: 11),
                      ),
                      if (_latestEcgResponse!.requiresVerification) ...[
                        const SizedBox(height: 8),
                        Container(
                          padding: const EdgeInsets.all(8),
                          decoration: BoxDecoration(
                            color: const Color(0xFF332511),
                            borderRadius: BorderRadius.circular(6),
                          ),
                          child: Text(
                            "⚠️ Low technical quality or noise detected. Analysis during consilium may be limited. Please verify or take a clearer recording.",
                            style: TextStyle(color: Colors.amber.shade200, fontSize: 11),
                          ),
                        ),
                      ],
                    ],
                  ),
                ),
              ],

              // ERROR MESSAGE DISPLAY
              if (_errorMessage != null) ...[
                const SizedBox(height: 10),
                Align(
                  alignment: Alignment.centerLeft,
                  child: Text(
                    _errorMessage!,
                    style: const TextStyle(color: Colors.redAccent, fontSize: 12),
                  ),
                ),
              ],

              // RETRY BUTTON (ONLY FOR NETWORK/TIMEOUT/SERVER ERRORS - NEVER FOR LOCAL PERSISTENCE ERROR)
              if (_errorMessage != null && _isRetryable && !_isEcgAnalyzing && _selectedImageFile != null) ...[
                const SizedBox(height: 8),
                Align(
                  alignment: Alignment.centerLeft,
                  child: TextButton.icon(
                    style: TextButton.styleFrom(foregroundColor: Colors.blueAccent),
                    onPressed: () => _processSelectedEcgFile(_selectedImageFile!),
                    icon: const Icon(Icons.refresh, size: 16),
                    label: const Text("Retry Upload", style: TextStyle(fontSize: 13)),
                  ),
                ),
              ],

              // RECOVERY BUTTON (FOR LOCAL STORAGE FAILURE WITHOUT RE-RUNNING OCR)
              if (_isLocalPersistenceFailed && !_isEcgAnalyzing) ...[
                const SizedBox(height: 8),
                Align(
                  alignment: Alignment.centerLeft,
                  child: TextButton.icon(
                    style: TextButton.styleFrom(foregroundColor: Colors.amberAccent),
                    onPressed: _recoverLatestEcg,
                    icon: const Icon(Icons.sync, size: 16),
                    label: const Text("Recover from Server (GET Latest)", style: TextStyle(fontSize: 13)),
                  ),
                ),
              ],

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
                        : const Color(0x1A00FF7F),
                    disabledBackgroundColor: Colors.white10,
                    side: BorderSide(
                      color: _isEcgAnalyzing ? Colors.white12 : Colors.greenAccent,
                      width: 1,
                    ),
                    shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(12)),
                  ),
                  onPressed: _isEcgAnalyzing
                      ? null
                      : () {
                          final int? sys = int.tryParse(_systolicController.text);
                          final int? dia = int.tryParse(_diastolicController.text);
                          final int? pulse = int.tryParse(_pulseController.text);
                          final int? spo2 = int.tryParse(_spo2Controller.text);

                          final String? ecgSummary = _latestEcgResponse != null
                              ? "ECG recorded on server (v${_latestEcgResponse!.intakeVersion}, ${_latestEcgResponse!.qualityDisplay})"
                              : null;

                          vault.updateSmartWatchMetrics(
                            systolic: (sys != null && dia != null) ? sys : null,
                            diastolic: (sys != null && dia != null) ? dia : null,
                            pulse: pulse,
                            spo2: spo2,
                            ecgSummary: ecgSummary,
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

              // SKIP ALL BUTTON
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
      MaterialPageRoute(builder: (context) => const UploadAnalysisScreen()),
    );
  }
}
