import 'dart:async';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:provider/provider.dart';

import '../services/api/vitals_api_models.dart';
import '../services/auth/auth_models.dart';
import '../services/auth/auth_session_manager.dart';
import '../services/health_data_provider.dart';

/// Modal bottom sheet allowing patients to bind their mobile device to an Aegis profile
/// using a 6-digit numeric pairing code.
///
/// Implements Stage 5.3C-3B-3A requirements:
/// - Exact 6-digit input with leading zeros preserved;
/// - Disables action button unless exactly 6 digits are provided;
/// - Handles 401 (invalid/expired/reused code), 422 (format error), 429 (rate limit cooldown),
///   and network errors safely without leaking tokens or pairing codes;
/// - Provides full diagnostic status when device is already paired;
/// - Enforces confirmation before disconnecting device session;
/// - Keeps atomic re-pairing locked when manager lacks atomic migration;
/// - Safely handles virtual keyboard and screen padding.
class AegisDevicePairingModal extends StatefulWidget {
  const AegisDevicePairingModal({super.key});

  /// Displays the pairing modal as a controlled modal bottom sheet.
  static Future<void> show(BuildContext context) {
    return showModalBottomSheet<void>(
      context: context,
      isScrollControlled: true,
      backgroundColor: const Color(0xFF0D1117),
      shape: const RoundedRectangleBorder(
        side: BorderSide(color: Colors.cyanAccent, width: 1),
        borderRadius: BorderRadius.vertical(top: Radius.circular(20)),
      ),
      builder: (BuildContext modalContext) {
        return const AegisDevicePairingModal();
      },
    );
  }

  @override
  State<AegisDevicePairingModal> createState() => _AegisDevicePairingModalState();
}

class _AegisDevicePairingModalState extends State<AegisDevicePairingModal> {
  final TextEditingController _codeController = TextEditingController();
  final TextEditingController _deviceNameController =
      TextEditingController(text: 'Aegis Mobile Client');

  bool _isSubmitting = false;
  bool _isSuccess = false;
  String? _successPatientId;
  String? _errorMessage;

  Timer? _cooldownTimer;
  int _cooldownRemainingSeconds = 0;
  Timer? _autoCloseTimer;

  @override
  void initState() {
    super.initState();
    _codeController.addListener(_onCodeChanged);
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (!mounted) return;
      final authManager = context.read<AuthSessionManager>();
      final remaining = authManager.rateLimitRemaining;
      if (remaining != null && remaining.inSeconds > 0) {
        _startCooldown(remaining);
      }
    });
  }

  void _onCodeChanged() {
    if (mounted) setState(() {});
  }

  void _startCooldown(Duration duration) {
    _cooldownTimer?.cancel();
    final seconds = duration.inSeconds > 0 ? duration.inSeconds : 60;
    setState(() {
      _isSubmitting = false;
      _cooldownRemainingSeconds = seconds;
      _errorMessage = null;
    });

    _cooldownTimer = Timer.periodic(const Duration(seconds: 1), (timer) {
      if (!mounted) {
        timer.cancel();
        return;
      }
      setState(() {
        if (_cooldownRemainingSeconds > 1) {
          _cooldownRemainingSeconds--;
        } else {
          _cooldownRemainingSeconds = 0;
          timer.cancel();
          _cooldownTimer = null;
        }
      });
    });
  }

  @override
  void dispose() {
    _codeController.removeListener(_onCodeChanged);
    _codeController.dispose();
    _deviceNameController.dispose();
    _cooldownTimer?.cancel();
    _autoCloseTimer?.cancel();
    super.dispose();
  }

  static String _truncate(String? value, {int maxLength = 16}) {
    if (value == null || value.isEmpty) return 'Unknown';
    if (value.length <= maxLength) return value;
    final prefix = value.substring(0, 10);
    final suffix = value.substring(value.length - 4);
    return '$prefix...$suffix';
  }

  Future<void> _submitPairing(AuthSessionManager authManager) async {
    final code = _codeController.text.trim();
    if (code.length != 6) {
      setState(() {
        _errorMessage = 'Pairing code must be an exact 6-digit numeric string.';
      });
      return;
    }

    setState(() {
      _isSubmitting = true;
      _errorMessage = null;
    });

    final deviceName = _deviceNameController.text.trim().isNotEmpty
        ? _deviceNameController.text.trim()
        : 'Aegis Mobile Client';

    try {
      final request = DevicePairRequest(
        pairingCode: code,
        deviceName: deviceName,
        platform: 'mobile',
      );

      final session = await authManager.pairDevice(request);

      if (!mounted) return;
      setState(() {
        _isSubmitting = false;
        _isSuccess = true;
        _successPatientId = session.patientId;
        _errorMessage = null;
      });

      _autoCloseTimer = Timer(const Duration(milliseconds: 1800), () {
        if (mounted) {
          Navigator.of(context).maybePop();
        }
      });
    } on AegisRateLimitException catch (e) {
      if (!mounted) return;
      _startCooldown(e.retryAfter ?? const Duration(seconds: 60));
    } on AegisAuthException catch (_) {
      if (!mounted) return;
      setState(() {
        _isSubmitting = false;
        _errorMessage =
            'Invalid, expired, or already used pairing code. Please generate a new code in the portal.';
      });
      _codeController.clear();
    } on AegisValidationException catch (_) {
      if (!mounted) return;
      setState(() {
        _isSubmitting = false;
        _errorMessage =
            'Invalid code format. Pairing code must be exactly 6 digits.';
      });
    } on AegisApiException catch (e) {
      if (!mounted) return;
      final msg = (e.statusCode == 401)
          ? 'Invalid, expired, or already used pairing code. Please generate a new code in the portal.'
          : (e.statusCode == 422)
              ? 'Invalid code format. Pairing code must be exactly 6 digits.'
              : 'Connection to Aegis server failed. Please check your network and try again.';
      setState(() {
        _isSubmitting = false;
        _errorMessage = msg;
      });
      if (e.statusCode == 401) {
        _codeController.clear();
      }
    } catch (_) {
      if (!mounted) return;
      setState(() {
        _isSubmitting = false;
        _errorMessage =
            'Connection to Aegis server failed. Please check your network and try again.';
      });
    }
  }

  Future<void> _handleDisconnect(AuthSessionManager authManager) async {
    final bool? confirm = await showDialog<bool>(
      context: context,
      barrierDismissible: false,
      builder: (BuildContext dialogContext) {
        return AlertDialog(
          backgroundColor: const Color(0xFF0D1117),
          shape: RoundedRectangleBorder(
            side: const BorderSide(color: Colors.redAccent, width: 1),
            borderRadius: BorderRadius.circular(12),
          ),
          title: const Text(
            'Disconnect Profile?',
            style: TextStyle(color: Colors.redAccent, fontWeight: FontWeight.bold),
          ),
          content: const Text(
            'Are you sure you want to disconnect this device from the active Aegis profile? '
            'Outbound measurement sync will be paused and unauthenticated data will be quarantined.',
            style: TextStyle(color: Colors.white70, fontSize: 13, height: 1.4),
          ),
          actions: [
            TextButton(
              key: const Key('cancel_disconnect_button'),
              onPressed: () => Navigator.pop(dialogContext, false),
              child: const Text('CANCEL', style: TextStyle(color: Colors.white60)),
            ),
            TextButton(
              key: const Key('confirm_disconnect_button'),
              onPressed: () => Navigator.pop(dialogContext, true),
              child: const Text('DISCONNECT', style: TextStyle(color: Colors.redAccent, fontWeight: FontWeight.bold)),
            ),
          ],
        );
      },
    );

    if (confirm != true || !mounted) return;

    setState(() {
      _isSubmitting = true;
      _errorMessage = null;
    });

    try {
      await authManager.revokeSession();
      if (!mounted) return;
      setState(() {
        _isSubmitting = false;
        _isSuccess = false;
        _errorMessage = null;
      });
    } catch (_) {
      if (!mounted) return;
      setState(() {
        _isSubmitting = false;
        _errorMessage = 'Failed to revoke device session. Please check your network.';
      });
    }
  }

  @override
  Widget build(BuildContext context) {
    return Consumer<AuthSessionManager>(
      builder: (context, authManager, child) {
        final session = authManager.currentSession;
        final hasSession = session != null && authManager.status != AuthSessionStatus.unauthenticated;

        return Container(
          constraints: BoxConstraints(
            maxHeight: MediaQuery.of(context).size.height * 0.9,
          ),
          padding: EdgeInsets.only(
            left: 20,
            right: 20,
            top: 20,
            bottom: MediaQuery.of(context).viewInsets.bottom + 24,
          ),
          child: Column(
            mainAxisSize: MainAxisSize.min,
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              // Top Drag Handle
              Center(
                child: Container(
                  width: 40,
                  height: 4,
                  decoration: BoxDecoration(
                    color: Colors.white24,
                    borderRadius: BorderRadius.circular(2),
                  ),
                ),
              ),
              const SizedBox(height: 16),

              Expanded(
                child: SingleChildScrollView(
                  child: _isSuccess
                      ? _buildSuccessView(context)
                      : (hasSession
                          ? _buildPairedView(context, authManager, session)
                          : _buildUnpairedView(context, authManager)),
                ),
              ),
            ],
          ),
        );
      },
    );
  }

  Widget _buildSuccessView(BuildContext context) {
    return Column(
      key: const Key('pairing_success_banner'),
      mainAxisSize: MainAxisSize.min,
      crossAxisAlignment: CrossAxisAlignment.center,
      children: [
        const SizedBox(height: 20),
        const Icon(Icons.check_circle_rounded, color: Colors.greenAccent, size: 54),
        const SizedBox(height: 16),
        const Text(
          'DEVICE PAIRED SUCCESSFULLY',
          style: TextStyle(
            color: Colors.greenAccent,
            fontSize: 14,
            fontWeight: FontWeight.bold,
            letterSpacing: 1.1,
          ),
        ),
        const SizedBox(height: 8),
        Text(
          'Active profile: ${_truncate(_successPatientId)}',
          style: const TextStyle(
            color: Colors.white,
            fontSize: 13,
            fontFamily: 'Courier',
          ),
        ),
        const SizedBox(height: 12),
        const Text(
          'Biometric vitals will now be automatically linked to this patient profile.',
          textAlign: TextAlign.center,
          style: TextStyle(color: Colors.white70, fontSize: 12, height: 1.4),
        ),
        const SizedBox(height: 24),
        SizedBox(
          width: double.infinity,
          child: ElevatedButton(
            key: const Key('pairing_success_done_button'),
            style: ElevatedButton.styleFrom(
              backgroundColor: Colors.greenAccent,
              padding: const EdgeInsets.symmetric(vertical: 12),
            ),
            onPressed: () => Navigator.of(context).maybePop(),
            child: const Text(
              'DONE',
              style: TextStyle(color: Colors.black, fontWeight: FontWeight.bold),
            ),
          ),
        ),
      ],
    );
  }

  Widget _buildPairedView(
    BuildContext context,
    AuthSessionManager authManager,
    AuthSession session,
  ) {
    final isExpired = authManager.status == AuthSessionStatus.expired;
    final isRateLimited = authManager.status == AuthSessionStatus.rateLimited;
    final statusColor = isExpired
        ? Colors.amberAccent
        : (isRateLimited ? Colors.orangeAccent : Colors.greenAccent);
    final statusText = isExpired
        ? 'Session Expired'
        : (isRateLimited ? 'Rate Limited' : 'Connected & Active');

    return Column(
      key: const Key('paired_status_view'),
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        // Title Row
        Row(
          children: [
            Icon(
              isExpired ? Icons.warning_amber_rounded : Icons.verified_user_rounded,
              color: statusColor,
              size: 20,
            ),
            const SizedBox(width: 8),
            const Text(
              'AEGIS CLOUD PROFILE',
              style: TextStyle(
                color: Colors.cyanAccent,
                fontSize: 13,
                fontWeight: FontWeight.bold,
                letterSpacing: 1.1,
              ),
            ),
          ],
        ),
        const SizedBox(height: 16),

        // Session Information Card
        Container(
          width: double.infinity,
          padding: const EdgeInsets.all(12),
          decoration: BoxDecoration(
            color: const Color(0xFF161B22),
            borderRadius: BorderRadius.circular(10),
            border: Border.all(color: statusColor.withValues(alpha: 0.4), width: 1),
          ),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              _buildInfoRow('Status', statusText, valueColor: statusColor),
              const Divider(color: Colors.white10, height: 14),
              _buildInfoRow('Profile ID', _truncate(session.patientId), isCourier: true),
              const Divider(color: Colors.white10, height: 14),
              _buildInfoRow('Device ID', _truncate(session.deviceId), isCourier: true),
              const Divider(color: Colors.white10, height: 14),
              _buildInfoRow(
                'Token Expires',
                session.expiresAt.toLocal().toString().split('.').first,
                isCourier: true,
              ),
            ],
          ),
        ),
        const SizedBox(height: 16),

        // Queue Diagnostics Card (HealthDataProvider)
        Consumer<HealthDataProvider>(
          builder: (context, healthProvider, _) {
            final cycleStatusStr = healthProvider.cycleStatus.name;
            final isConfirmed = healthProvider.isCycleConfirmedByServer;
            final cycleDisplay = '$cycleStatusStr (${isConfirmed ? "Confirmed" : "Unconfirmed"})';

            String syncDisplay;
            if (healthProvider.isSyncPaused) {
              syncDisplay = 'Paused (${healthProvider.syncPauseReason ?? "queue"})';
            } else if (healthProvider.isSyncing) {
              syncDisplay = 'Syncing...';
            } else {
              syncDisplay = 'Idle';
            }

            return Container(
              width: double.infinity,
              padding: const EdgeInsets.all(12),
              decoration: BoxDecoration(
                color: const Color(0xFF161B22),
                borderRadius: BorderRadius.circular(10),
                border: Border.all(color: Colors.white12, width: 1),
              ),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  const Text(
                    'QUEUE DIAGNOSTICS',
                    style: TextStyle(
                      color: Colors.white70,
                      fontSize: 10,
                      fontWeight: FontWeight.bold,
                      letterSpacing: 1.0,
                    ),
                  ),
                  const SizedBox(height: 8),
                  _buildInfoRow('Active Patient Pending', '${healthProvider.syncPendingCount} items'),
                  const Divider(color: Colors.white10, height: 14),
                  _buildInfoRow('Quarantined Items', '${healthProvider.syncQuarantinedCount} items'),
                  const Divider(color: Colors.white10, height: 14),
                  _buildInfoRow('Sync Status', syncDisplay),
                  const Divider(color: Colors.white10, height: 14),
                  _buildInfoRow('Medical Cycle', cycleDisplay),
                ],
              ),
            );
          },
        ),
        const SizedBox(height: 16),

        if (_errorMessage != null) ...[
          Container(
            key: const Key('pairing_error_banner'),
            width: double.infinity,
            margin: const EdgeInsets.only(bottom: 14),
            padding: const EdgeInsets.all(10),
            decoration: BoxDecoration(
              color: const Color(0x33FF5252),
              borderRadius: BorderRadius.circular(8),
              border: Border.all(color: Colors.redAccent, width: 1),
            ),
            child: Row(
              children: [
                const Icon(Icons.error_outline, color: Colors.redAccent, size: 16),
                const SizedBox(width: 8),
                Expanded(
                  child: Text(
                    _errorMessage!,
                    style: const TextStyle(color: Colors.white, fontSize: 11, height: 1.3),
                  ),
                ),
              ],
            ),
          ),
        ],

        // Action Buttons
        Row(
          children: [
            Expanded(
              child: OutlinedButton(
                key: const Key('disconnect_device_button'),
                style: OutlinedButton.styleFrom(
                  side: const BorderSide(color: Colors.redAccent, width: 1),
                  padding: const EdgeInsets.symmetric(vertical: 12),
                ),
                onPressed: _isSubmitting ? null : () => _handleDisconnect(authManager),
                child: _isSubmitting
                    ? const SizedBox(
                        height: 14,
                        width: 14,
                        child: CircularProgressIndicator(strokeWidth: 2, color: Colors.redAccent),
                      )
                    : const Text(
                        'DISCONNECT',
                        style: TextStyle(color: Colors.redAccent, fontWeight: FontWeight.bold, fontSize: 12),
                      ),
              ),
            ),
            const SizedBox(width: 10),
            Expanded(
              child: Tooltip(
                message:
                    'Atomic profile switching without disconnecting is not supported. Please disconnect first.',
                child: OutlinedButton(
                  key: const Key('switch_profile_button'),
                  style: OutlinedButton.styleFrom(
                    side: const BorderSide(color: Colors.white24, width: 1),
                    padding: const EdgeInsets.symmetric(vertical: 12),
                  ),
                  onPressed: null, // Disabled per Stage 5.3C-3B-3A requirements
                  child: const Text(
                    'SWITCH PROFILE',
                    style: TextStyle(color: Colors.white24, fontWeight: FontWeight.bold, fontSize: 12),
                  ),
                ),
              ),
            ),
          ],
        ),
        const SizedBox(height: 8),
        const Text(
          'Note: Switching profiles requires disconnecting the active session first to maintain patient isolation.',
          style: TextStyle(color: Colors.white38, fontSize: 10, fontStyle: FontStyle.italic),
        ),
      ],
    );
  }

  Widget _buildUnpairedView(
    BuildContext context,
    AuthSessionManager authManager,
  ) {
    final bool isCodeValid = _codeController.text.trim().length == 6;
    final bool isRateLimited = _cooldownRemainingSeconds > 0;
    final bool canSubmit = isCodeValid && !_isSubmitting && !isRateLimited;

    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        const Row(
          children: [
            Icon(Icons.phonelink_lock_rounded, color: Colors.cyanAccent, size: 20),
            SizedBox(width: 8),
            Text(
              'PAIR AEGIS PROFILE',
              style: TextStyle(
                color: Colors.cyanAccent,
                fontSize: 13,
                fontWeight: FontWeight.bold,
                letterSpacing: 1.1,
              ),
            ),
          ],
        ),
        const SizedBox(height: 8),
        const Text(
          'Enter the 6-digit pairing code generated from the Aegis portal or provided by your clinician.',
          style: TextStyle(color: Colors.white70, fontSize: 12, height: 1.4),
        ),
        const SizedBox(height: 18),

        // 6-digit Code Input Field
        const Text(
          'PAIRING CODE (6 DIGITS)',
          style: TextStyle(
            color: Colors.white54,
            fontSize: 10,
            fontWeight: FontWeight.bold,
            letterSpacing: 1.0,
          ),
        ),
        const SizedBox(height: 6),
        TextField(
          key: const Key('pairing_code_input'),
          controller: _codeController,
          keyboardType: TextInputType.number,
          textAlign: TextAlign.center,
          style: const TextStyle(
            fontSize: 26,
            fontWeight: FontWeight.bold,
            letterSpacing: 10,
            color: Colors.cyanAccent,
            fontFamily: 'Courier',
          ),
          inputFormatters: [
            FilteringTextInputFormatter.digitsOnly,
            LengthLimitingTextInputFormatter(6),
          ],
          decoration: InputDecoration(
            hintText: '000000',
            hintStyle: const TextStyle(
              fontSize: 26,
              letterSpacing: 10,
              color: Colors.white24,
              fontFamily: 'Courier',
            ),
            filled: true,
            fillColor: const Color(0xFF161B22),
            contentPadding: const EdgeInsets.symmetric(vertical: 12),
            enabledBorder: OutlineInputBorder(
              borderRadius: BorderRadius.circular(10),
              borderSide: BorderSide(
                color: isCodeValid ? Colors.cyanAccent : Colors.white24,
                width: 1,
              ),
            ),
            focusedBorder: OutlineInputBorder(
              borderRadius: BorderRadius.circular(10),
              borderSide: const BorderSide(color: Colors.cyanAccent, width: 1.5),
            ),
          ),
        ),
        const SizedBox(height: 14),

        // Device Name Field
        const Text(
          'DEVICE NAME',
          style: TextStyle(
            color: Colors.white54,
            fontSize: 10,
            fontWeight: FontWeight.bold,
            letterSpacing: 1.0,
          ),
        ),
        const SizedBox(height: 6),
        TextField(
          key: const Key('device_name_input'),
          controller: _deviceNameController,
          style: const TextStyle(color: Colors.white, fontSize: 13),
          decoration: InputDecoration(
            filled: true,
            fillColor: const Color(0xFF161B22),
            contentPadding: const EdgeInsets.symmetric(horizontal: 12, vertical: 10),
            enabledBorder: OutlineInputBorder(
              borderRadius: BorderRadius.circular(10),
              borderSide: const BorderSide(color: Colors.white24, width: 1),
            ),
            focusedBorder: OutlineInputBorder(
              borderRadius: BorderRadius.circular(10),
              borderSide: const BorderSide(color: Colors.cyanAccent, width: 1.5),
            ),
          ),
        ),
        const SizedBox(height: 16),

        // Rate Limit Banner
        if (isRateLimited) ...[
          Container(
            key: const Key('pairing_rate_limit_banner'),
            width: double.infinity,
            margin: const EdgeInsets.only(bottom: 14),
            padding: const EdgeInsets.all(10),
            decoration: BoxDecoration(
              color: const Color(0x33FFB300),
              borderRadius: BorderRadius.circular(8),
              border: Border.all(color: Colors.amberAccent, width: 1),
            ),
            child: Row(
              children: [
                const Icon(Icons.timer_outlined, color: Colors.amberAccent, size: 16),
                const SizedBox(width: 8),
                Expanded(
                  child: Text(
                    'Rate limit active. Please wait $_cooldownRemainingSeconds s before retrying.',
                    style: const TextStyle(color: Colors.white, fontSize: 11),
                  ),
                ),
              ],
            ),
          ),
        ],

        // Error Banner
        if (_errorMessage != null) ...[
          Container(
            key: const Key('pairing_error_banner'),
            width: double.infinity,
            margin: const EdgeInsets.only(bottom: 14),
            padding: const EdgeInsets.all(10),
            decoration: BoxDecoration(
              color: const Color(0x33FF5252),
              borderRadius: BorderRadius.circular(8),
              border: Border.all(color: Colors.redAccent, width: 1),
            ),
            child: Row(
              children: [
                const Icon(Icons.error_outline, color: Colors.redAccent, size: 16),
                const SizedBox(width: 8),
                Expanded(
                  child: Text(
                    _errorMessage!,
                    style: const TextStyle(color: Colors.white, fontSize: 11, height: 1.3),
                  ),
                ),
              ],
            ),
          ),
        ],

        // Submit Button
        SizedBox(
          width: double.infinity,
          child: ElevatedButton(
            key: const Key('pair_device_button'),
            style: ElevatedButton.styleFrom(
              backgroundColor: canSubmit ? Colors.cyanAccent : const Color(0xFF21262D),
              disabledBackgroundColor: const Color(0xFF21262D),
              padding: const EdgeInsets.symmetric(vertical: 14),
              shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(10)),
            ),
            onPressed: canSubmit ? () => _submitPairing(authManager) : null,
            child: _isSubmitting
                ? const Row(
                    key: Key('pairing_in_flight_indicator'),
                    mainAxisAlignment: MainAxisAlignment.center,
                    children: [
                      SizedBox(
                        width: 16,
                        height: 16,
                        child: CircularProgressIndicator(strokeWidth: 2, color: Colors.black),
                      ),
                      SizedBox(width: 10),
                      Text(
                        'PAIRING DEVICE...',
                        style: TextStyle(color: Colors.black, fontWeight: FontWeight.bold, fontSize: 12),
                      ),
                    ],
                  )
                : Text(
                    'PAIR DEVICE',
                    style: TextStyle(
                      color: canSubmit ? Colors.black : Colors.white24,
                      fontWeight: FontWeight.bold,
                      fontSize: 12,
                      letterSpacing: 1.0,
                    ),
                  ),
          ),
        ),
        const SizedBox(height: 12),
        const Text(
          'Security notice: Pairing codes are single-use and valid for 10 minutes. '
          'Unauthenticated vitals remain isolated in local quarantine.',
          style: TextStyle(color: Colors.white38, fontSize: 10, height: 1.3),
        ),
      ],
    );
  }

  Widget _buildInfoRow(String label, String value, {Color? valueColor, bool isCourier = false}) {
    return Row(
      mainAxisAlignment: MainAxisAlignment.spaceBetween,
      children: [
        Text(
          label,
          style: const TextStyle(color: Colors.white60, fontSize: 11),
        ),
        Text(
          value,
          style: TextStyle(
            color: valueColor ?? Colors.white,
            fontSize: 11,
            fontWeight: FontWeight.bold,
            fontFamily: isCourier ? 'Courier' : null,
          ),
        ),
      ],
    );
  }
}
