import 'package:flutter/material.dart';
import 'package:provider/provider.dart';
import '../services/api/aegis_api_client.dart';
import '../services/api/medical_ai_api_models.dart';
import '../services/api/vitals_api_models.dart';
import '../services/auth/auth_session_manager.dart';
import '../services/vault_service.dart';
import '../main.dart';

enum ConsiliumScreenState {
  checkingReadiness,
  readyToRun,
  contextMissing,
  executing,
  alreadyCompleted,
  temporarilyBusy,
  error,
}

class AegisConsiliumScreen extends StatefulWidget {
  final AegisApiClient? apiClient;
  final AuthSessionManager? authSessionManager;
  final VaultService? vaultService;

  const AegisConsiliumScreen({
    super.key,
    this.apiClient,
    this.authSessionManager,
    this.vaultService,
  });

  @override
  State<AegisConsiliumScreen> createState() => _AegisConsiliumScreenState();
}

class _AegisConsiliumScreenState extends State<AegisConsiliumScreen> {
  ConsiliumScreenState _state = ConsiliumScreenState.checkingReadiness;
  String? _statusExplanation;
  ClientCascadeStatusResponse? _cascadeStatus;
  int _requestEpoch = 0;
  String? _trackedPatientId;
  String? _activeConsiliumCycleId;
  String? _stableIdempotencyKey;

  AegisApiClient _resolveApiClient() =>
      widget.apiClient ?? context.read<AegisApiClient>();

  AuthSessionManager? _resolveAuthManager() {
    if (widget.authSessionManager != null) return widget.authSessionManager;
    try {
      return context.read<AuthSessionManager>();
    } catch (_) {
      return null;
    }
  }

  VaultService _resolveVaultService() =>
      widget.vaultService ?? context.read<VaultService>();

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (mounted) {
        final auth = _resolveAuthManager();
        _trackedPatientId = auth?.confirmedAttributionPatientId;
        auth?.addListener(_onAuthChanged);
        _checkReadiness();
      }
    });
  }

  @override
  void dispose() {
    final auth = _resolveAuthManager();
    auth?.removeListener(_onAuthChanged);
    super.dispose();
  }

  void _onAuthChanged() {
    if (!mounted) return;
    final auth = _resolveAuthManager();
    final currentId = auth?.confirmedAttributionPatientId;
    if (currentId != _trackedPatientId) {
      _trackedPatientId = currentId;
      _stableIdempotencyKey = null;
      _activeConsiliumCycleId = null;
      _checkReadiness();
    }
  }

  /// Checks server readiness without initiating an execution cascade.
  Future<void> _checkReadiness() async {
    if (!mounted) return;

    final auth = _resolveAuthManager();
    if (auth == null || !auth.isAuthenticated) {
      setState(() {
        _state = ConsiliumScreenState.error;
        _statusExplanation = "Device authentication required.";
      });
      return;
    }

    final patientId = auth.confirmedAttributionPatientId;
    if (patientId == null) {
      setState(() {
        _state = ConsiliumScreenState.error;
        _statusExplanation = "Active patient session not found.";
      });
      return;
    }

    final int epoch = ++_requestEpoch;
    setState(() {
      _state = ConsiliumScreenState.checkingReadiness;
      _statusExplanation = "Checking server medical cycle readiness...";
    });

    final apiClient = _resolveApiClient();
    final vault = _resolveVaultService();

    try {
      final status = await apiClient.getCascadeStatus();

      // Epoch & attribution fence check after network hop
      if (!mounted || _requestEpoch != epoch || auth.confirmedAttributionPatientId != patientId) {
        return;
      }

      _cascadeStatus = status;

      if (!status.hasActiveCycle) {
        setState(() {
          _state = ConsiliumScreenState.contextMissing;
          _statusExplanation = "No active clinical cycle found for patient.";
        });
        return;
      }

      if (status.isPrimaryConsiliumCompleted) {
        // Recover reports if not yet in vault
        if (!vault.isConsiliumCompleted && status.activeCycleId != null) {
          try {
            final reports = await apiClient.getClientReports(cycleId: status.activeCycleId!);
            if (mounted && _requestEpoch == epoch && auth.confirmedAttributionPatientId == patientId) {
              final bool saved = await vault.recordClientReports(
                cycleId: status.activeCycleId!,
                reports: reports,
                expectedPatientId: patientId,
              );
              if (!mounted || _requestEpoch != epoch || auth.confirmedAttributionPatientId != patientId) {
                return;
              }
              if (!saved) {
                setState(() {
                  _state = ConsiliumScreenState.error;
                  _statusExplanation = "Failed to securely persist recovered consilium reports locally.";
                });
                return;
              }
            }
          } catch (_) {}
        }

        if (mounted && _requestEpoch == epoch && auth.confirmedAttributionPatientId == patientId) {
          _stableIdempotencyKey = null;
          _activeConsiliumCycleId = null;
          setState(() {
            _state = ConsiliumScreenState.alreadyCompleted;
            _statusExplanation = "Primary Consilium has already concluded for this cycle.";
          });
        }
        return;
      }

      if (!status.hasPreparatoryAnamnesis) {
        setState(() {
          _state = ConsiliumScreenState.contextMissing;
          _statusExplanation =
              "Required preparatory anamnesis (M1) is missing on the server. "
              "Please complete and synchronize the anamnesis questionnaire before running Primary Consilium.";
        });
        return;
      }

      // Ready to execute
      setState(() {
        _state = ConsiliumScreenState.readyToRun;
        _statusExplanation = "Medical cycle context verified. Ready to activate neural synthesis.";
      });
    } on AegisNetworkException catch (e) {
      if (mounted && _requestEpoch == epoch && auth.confirmedAttributionPatientId == patientId) {
        setState(() {
          _state = ConsiliumScreenState.error;
          _statusExplanation = "Network connection failed: ${e.message}";
        });
      }
    } on AegisTimeoutException catch (_) {
      if (mounted && _requestEpoch == epoch && auth.confirmedAttributionPatientId == patientId) {
        setState(() {
          _state = ConsiliumScreenState.error;
          _statusExplanation = "Request timed out while checking cycle status.";
        });
      }
    } on AegisApiException catch (e) {
      if (mounted && _requestEpoch == epoch && auth.confirmedAttributionPatientId == patientId) {
        setState(() {
          _state = ConsiliumScreenState.error;
          _statusExplanation = e.message;
        });
      }
    } catch (e) {
      if (mounted && _requestEpoch == epoch && auth.confirmedAttributionPatientId == patientId) {
        setState(() {
          _state = ConsiliumScreenState.error;
          _statusExplanation = "Error: $e";
        });
      }
    }
  }

  /// Explicitly runs Primary Consilium on user action.
  /// Strictly prevents double-tapping and uses idempotency headers.
  Future<void> _runConsilium() async {
    if (_state == ConsiliumScreenState.executing) return;

    final auth = _resolveAuthManager();
    if (auth == null || !auth.isAuthenticated) {
      setState(() {
        _state = ConsiliumScreenState.error;
        _statusExplanation = "Session expired or unauthenticated. Please re-pair device.";
      });
      return;
    }

    final patientId = auth.confirmedAttributionPatientId;
    if (patientId == null) {
      setState(() {
        _state = ConsiliumScreenState.error;
        _statusExplanation = "Patient attribution required.";
      });
      return;
    }

    final int epoch = ++_requestEpoch;
    setState(() {
      _state = ConsiliumScreenState.executing;
      _statusExplanation = "Verifying server readiness...";
    });

    final apiClient = _resolveApiClient();
    final vault = _resolveVaultService();

    try {
      // 1. Verify server status before executing or retrying
      final status = await apiClient.getCascadeStatus();

      if (!mounted || _requestEpoch != epoch || auth.confirmedAttributionPatientId != patientId) {
        return;
      }

      _cascadeStatus = status;

      if (!status.hasActiveCycle) {
        setState(() {
          _state = ConsiliumScreenState.contextMissing;
          _statusExplanation = "No active clinical cycle found for patient.";
        });
        return;
      }

      if (!status.hasPreparatoryAnamnesis) {
        setState(() {
          _state = ConsiliumScreenState.contextMissing;
          _statusExplanation =
              "Cannot run Primary Consilium: preparatory anamnesis (M1) is missing on the server. "
              "Please complete and synchronize anamnesis first.";
        });
        return;
      }

      final String cycleId = status.activeCycleId!;

      // If already completed on server (e.g. following previous timeout), recover via GET without POST
      if (status.isPrimaryConsiliumCompleted) {
        final reports = await apiClient.getClientReports(cycleId: cycleId);

        if (!mounted || _requestEpoch != epoch || auth.confirmedAttributionPatientId != patientId) {
          return;
        }

        final bool saved = await vault.recordClientReports(
          cycleId: cycleId,
          reports: reports,
          expectedPatientId: patientId,
        );

        if (!mounted || _requestEpoch != epoch || auth.confirmedAttributionPatientId != patientId) {
          return;
        }

        if (!saved) {
          setState(() {
            _state = ConsiliumScreenState.error;
            _statusExplanation = "Failed to securely persist recovered consilium reports locally.";
          });
          return;
        }

        _stableIdempotencyKey = null;
        _activeConsiliumCycleId = null;

        if (mounted) {
          Navigator.pushReplacement(
            context,
            MaterialPageRoute(builder: (context) => const AtlasScreen()),
          );
        }
        return;
      }

      // 2. Stable idempotency key for this medical cycle across retries
      if (_activeConsiliumCycleId != cycleId || _stableIdempotencyKey == null) {
        _activeConsiliumCycleId = cycleId;
        _stableIdempotencyKey = 'run_primary_$cycleId';
      }

      setState(() {
        _statusExplanation = "Primary Consilium executing: synthesizing pathophysiological profile...";
      });

      final response = await apiClient.runPrimaryConsilium(
        idempotencyKey: _stableIdempotencyKey,
      );

      // Epoch & attribution fence check after network hop
      if (!mounted || _requestEpoch != epoch || auth.confirmedAttributionPatientId != patientId) {
        return;
      }

      final allReports = <ClientModelReportResponse>[
        response.overallReport,
        ...response.systemReports.values,
      ];

      final bool saved = await vault.recordClientReports(
        cycleId: response.cycleId,
        reports: allReports,
        expectedPatientId: patientId,
      );

      // Epoch & attribution fence check after vault persistence
      if (!mounted || _requestEpoch != epoch || auth.confirmedAttributionPatientId != patientId) {
        return;
      }

      if (!saved) {
        setState(() {
          _state = ConsiliumScreenState.error;
          _statusExplanation = "Failed to securely persist consilium results to local storage.";
        });
        return;
      }

      _stableIdempotencyKey = null;
      _activeConsiliumCycleId = null;

      // Consilium successfully executed and persisted — navigate to Atlas
      if (mounted) {
        Navigator.pushReplacement(
          context,
          MaterialPageRoute(builder: (context) => const AtlasScreen()),
        );
      }
    } on AegisConflictException catch (e) {
      if (!mounted || _requestEpoch != epoch || auth.confirmedAttributionPatientId != patientId) {
        return;
      }

      final lowerMsg = e.message.toLowerCase();
      if (lowerMsg.contains('lease') ||
          lowerMsg.contains('concurrency') ||
          lowerMsg.contains('in-flight') ||
          lowerMsg.contains('concurrent')) {
        setState(() {
          _state = ConsiliumScreenState.temporarilyBusy;
          _statusExplanation =
              "Consilium is temporarily busy: another analysis process holds the execution lock. "
              "Please wait or retry shortly.";
        });
      } else if (lowerMsg.contains('anamnesis') || lowerMsg.contains('missing')) {
        setState(() {
          _state = ConsiliumScreenState.contextMissing;
          _statusExplanation = "Preparatory context missing: ${e.message}";
        });
      } else {
        setState(() {
          _state = ConsiliumScreenState.error;
          _statusExplanation = e.message;
        });
      }
    } on AegisInsufficientScopeException catch (e) {
      if (mounted && _requestEpoch == epoch && auth.confirmedAttributionPatientId == patientId) {
        setState(() {
          _state = ConsiliumScreenState.error;
          _statusExplanation = "Access denied: session lacks required scope (${e.requiredScope ?? 'cascade:run'}).";
        });
      }
    } on AegisNetworkException catch (e) {
      // In case of network drop / socket timeout after server accepted request, check server status first
      try {
        final recoveryStatus = await apiClient.getCascadeStatus();
        if (mounted && _requestEpoch == epoch && auth.confirmedAttributionPatientId == patientId) {
          if (recoveryStatus.isPrimaryConsiliumCompleted && recoveryStatus.activeCycleId != null) {
            final reports = await apiClient.getClientReports(cycleId: recoveryStatus.activeCycleId!);
            if (mounted && _requestEpoch == epoch && auth.confirmedAttributionPatientId == patientId) {
              final bool saved = await vault.recordClientReports(
                cycleId: recoveryStatus.activeCycleId!,
                reports: reports,
                expectedPatientId: patientId,
              );
              if (mounted && _requestEpoch == epoch && auth.confirmedAttributionPatientId == patientId) {
                if (saved) {
                  _stableIdempotencyKey = null;
                  _activeConsiliumCycleId = null;
                  Navigator.pushReplacement(
                    context,
                    MaterialPageRoute(builder: (context) => const AtlasScreen()),
                  );
                  return;
                }
              }
            }
          }
        }
      } catch (_) {}

      if (mounted && _requestEpoch == epoch && auth.confirmedAttributionPatientId == patientId) {
        setState(() {
          _state = ConsiliumScreenState.error;
          _statusExplanation = "Network connection failed: ${e.message}. Vault data preserved.";
        });
      }
    } on AegisTimeoutException catch (_) {
      // In case of timeout after server accepted request, check server status first
      try {
        final recoveryStatus = await apiClient.getCascadeStatus();
        if (mounted && _requestEpoch == epoch && auth.confirmedAttributionPatientId == patientId) {
          if (recoveryStatus.isPrimaryConsiliumCompleted && recoveryStatus.activeCycleId != null) {
            final reports = await apiClient.getClientReports(cycleId: recoveryStatus.activeCycleId!);
            if (mounted && _requestEpoch == epoch && auth.confirmedAttributionPatientId == patientId) {
              final bool saved = await vault.recordClientReports(
                cycleId: recoveryStatus.activeCycleId!,
                reports: reports,
                expectedPatientId: patientId,
              );
              if (mounted && _requestEpoch == epoch && auth.confirmedAttributionPatientId == patientId) {
                if (saved) {
                  _stableIdempotencyKey = null;
                  _activeConsiliumCycleId = null;
                  Navigator.pushReplacement(
                    context,
                    MaterialPageRoute(builder: (context) => const AtlasScreen()),
                  );
                  return;
                }
              }
            }
          }
        }
      } catch (_) {}

      if (mounted && _requestEpoch == epoch && auth.confirmedAttributionPatientId == patientId) {
        setState(() {
          _state = ConsiliumScreenState.error;
          _statusExplanation = "Consilium execution timed out. Existing data preserved.";
        });
      }
    } on AegisApiException catch (e) {
      if (mounted && _requestEpoch == epoch && auth.confirmedAttributionPatientId == patientId) {
        setState(() {
          _state = ConsiliumScreenState.error;
          _statusExplanation = e.message;
        });
      }
    } catch (e) {
      if (mounted && _requestEpoch == epoch && auth.confirmedAttributionPatientId == patientId) {
        setState(() {
          _state = ConsiliumScreenState.error;
          _statusExplanation = "Unexpected error: $e";
        });
      }
    }
  }

  void _navigateToAtlas() {
    Navigator.pushReplacement(
      context,
      MaterialPageRoute(builder: (context) => const AtlasScreen()),
    );
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
              const SizedBox(height: 24),
              const Text(
                "AEGIS :: CONSILIUM",
                style: TextStyle(
                  color: Colors.blueAccent,
                  fontSize: 24,
                  fontWeight: FontWeight.bold,
                  letterSpacing: 2,
                ),
              ),
              const SizedBox(height: 16),
              Text(
                _statusExplanation ?? "Preparing clinical synthesis pipeline...",
                key: const Key('consilium_status_explanation'),
                textAlign: TextAlign.center,
                style: const TextStyle(color: Colors.white70, fontSize: 14),
              ),
              const SizedBox(height: 24),

              // Checklist card
              if (_cascadeStatus != null)
                Container(
                  padding: const EdgeInsets.all(16),
                  margin: const EdgeInsets.symmetric(horizontal: 16),
                  decoration: BoxDecoration(
                    color: const Color(0xFF0D1117),
                    borderRadius: BorderRadius.circular(12),
                    border: Border.all(color: Colors.white12),
                  ),
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      _buildChecklistItem(
                        "Preparatory Anamnesis (M1)",
                        _cascadeStatus!.hasPreparatoryAnamnesis,
                        isMandatory: true,
                      ),
                      const SizedBox(height: 8),
                      _buildChecklistItem(
                        "ECG Recording",
                        _cascadeStatus!.hasPreparatoryEcg,
                        isMandatory: false,
                      ),
                      const SizedBox(height: 8),
                      _buildChecklistItem(
                        "Laboratory Ledger",
                        _cascadeStatus!.hasPreparatoryLabs,
                        isMandatory: false,
                      ),
                    ],
                  ),
                ),

              const SizedBox(height: 36),

              // Dynamic action controls based on state
              _buildActionControls(),
            ],
          ),
        ),
      ),
    );
  }

  Widget _buildChecklistItem(String label, bool isPresent, {required bool isMandatory}) {
    return Row(
      children: [
        Icon(
          isPresent ? Icons.check_circle_outline : (isMandatory ? Icons.error_outline : Icons.help_outline),
          color: isPresent ? Colors.greenAccent : (isMandatory ? Colors.redAccent : Colors.white38),
          size: 16,
        ),
        const SizedBox(width: 8),
        Expanded(
          child: Text(
            label,
            style: TextStyle(
              fontSize: 12,
              color: isPresent ? Colors.white : Colors.white60,
              fontWeight: isMandatory ? FontWeight.bold : FontWeight.normal,
            ),
          ),
        ),
        Text(
          isPresent ? "Verified" : (isMandatory ? "Required" : "Optional"),
          style: TextStyle(
            fontSize: 11,
            color: isPresent ? Colors.greenAccent : (isMandatory ? Colors.redAccent : Colors.white38),
            fontFamily: 'Courier',
          ),
        ),
      ],
    );
  }

  Widget _buildActionControls() {
    switch (_state) {
      case ConsiliumScreenState.checkingReadiness:
        return const Column(
          children: [
            CircularProgressIndicator(
              key: Key('consilium_checking_indicator'),
              color: Colors.cyanAccent,
            ),
            SizedBox(height: 12),
            Text("Verifying cycle readiness...", style: TextStyle(color: Colors.cyanAccent, fontSize: 12)),
          ],
        );

      case ConsiliumScreenState.executing:
        return const Column(
          children: [
            CircularProgressIndicator(
              key: Key('consilium_executing_indicator'),
              color: Colors.orangeAccent,
            ),
            SizedBox(height: 16),
            Text(
              "Running Primary Consilium...\nAnalyzing 8 organ systems with Gemini Flash & Pro",
              textAlign: TextAlign.center,
              style: TextStyle(color: Colors.orangeAccent, fontSize: 12),
            ),
          ],
        );

      case ConsiliumScreenState.readyToRun:
        return ElevatedButton(
          key: const Key('run_consilium_btn'),
          style: ElevatedButton.styleFrom(
            backgroundColor: Colors.blueAccent.withOpacity(0.2),
            side: const BorderSide(color: Colors.blueAccent),
            padding: const EdgeInsets.symmetric(horizontal: 40, vertical: 15),
          ),
          onPressed: _runConsilium,
          child: const Text("RUN ANALYSIS", style: TextStyle(fontSize: 16, fontWeight: FontWeight.bold, color: Colors.blueAccent)),
        );

      case ConsiliumScreenState.alreadyCompleted:
        return ElevatedButton.icon(
          key: const Key('goto_atlas_btn'),
          style: ElevatedButton.styleFrom(
            backgroundColor: Colors.greenAccent.withOpacity(0.2),
            side: const BorderSide(color: Colors.greenAccent),
            padding: const EdgeInsets.symmetric(horizontal: 36, vertical: 15),
          ),
          onPressed: _navigateToAtlas,
          icon: const Icon(Icons.analytics_outlined, color: Colors.greenAccent),
          label: const Text("VIEW IN ATLAS", style: TextStyle(fontSize: 16, fontWeight: FontWeight.bold, color: Colors.greenAccent)),
        );

      case ConsiliumScreenState.temporarilyBusy:
        return Column(
          children: [
            ElevatedButton.icon(
              key: const Key('retry_busy_btn'),
              style: ElevatedButton.styleFrom(
                backgroundColor: Colors.amberAccent.withOpacity(0.2),
                side: const BorderSide(color: Colors.amberAccent),
                padding: const EdgeInsets.symmetric(horizontal: 36, vertical: 15),
              ),
              onPressed: _runConsilium,
              icon: const Icon(Icons.refresh_rounded, color: Colors.amberAccent),
              label: const Text("RETRY EXECUTION", style: TextStyle(fontSize: 16, fontWeight: FontWeight.bold, color: Colors.amberAccent)),
            ),
            const SizedBox(height: 12),
            TextButton(
              onPressed: _checkReadiness,
              child: const Text("Check Status", style: TextStyle(color: Colors.white60, fontSize: 12)),
            ),
          ],
        );

      case ConsiliumScreenState.contextMissing:
        return ElevatedButton.icon(
          key: const Key('check_status_btn'),
          style: ElevatedButton.styleFrom(
            backgroundColor: Colors.white10,
            side: const BorderSide(color: Colors.white24),
            padding: const EdgeInsets.symmetric(horizontal: 36, vertical: 15),
          ),
          onPressed: _checkReadiness,
          icon: const Icon(Icons.refresh_rounded, color: Colors.white70),
          label: const Text("RECHECK READINESS", style: TextStyle(fontSize: 14, color: Colors.white)),
        );

      case ConsiliumScreenState.error:
        return Column(
          children: [
            ElevatedButton.icon(
              key: const Key('retry_consilium_btn'),
              style: ElevatedButton.styleFrom(
                backgroundColor: Colors.redAccent.withOpacity(0.2),
                side: const BorderSide(color: Colors.redAccent),
                padding: const EdgeInsets.symmetric(horizontal: 36, vertical: 15),
              ),
              onPressed: _runConsilium,
              icon: const Icon(Icons.refresh_rounded, color: Colors.redAccent),
              label: const Text("RETRY", style: TextStyle(fontSize: 16, fontWeight: FontWeight.bold, color: Colors.redAccent)),
            ),
            const SizedBox(height: 12),
            TextButton(
              onPressed: _navigateToAtlas,
              child: const Text("Open Atlas with Cached Data", style: TextStyle(color: Colors.white60, fontSize: 12)),
            ),
          ],
        );
    }
  }
}
