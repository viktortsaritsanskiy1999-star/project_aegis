import 'package:flutter/material.dart';
import 'package:provider/provider.dart';
import '../services/api/aegis_api_client.dart';
import '../services/vault_service.dart';

class LabManualEditModal extends StatefulWidget {
  final String markerKey;
  final String displayName;
  final String initialValue;
  final String? initialUnit;
  final String? initialDate;

  const LabManualEditModal({
    super.key,
    required this.markerKey,
    required this.displayName,
    required this.initialValue,
    this.initialUnit,
    this.initialDate,
  });

  static Future<bool?> show(
    BuildContext context, {
    required String markerKey,
    required String displayName,
    required String initialValue,
    String? initialUnit,
    String? initialDate,
  }) {
    return showDialog<bool>(
      context: context,
      builder: (ctx) => LabManualEditModal(
        markerKey: markerKey,
        displayName: displayName,
        initialValue: initialValue,
        initialUnit: initialUnit,
        initialDate: initialDate,
      ),
    );
  }

  @override
  State<LabManualEditModal> createState() => _LabManualEditModalState();
}

class _LabManualEditModalState extends State<LabManualEditModal> {
  late TextEditingController _valueController;
  late TextEditingController _unitController;
  late TextEditingController _dateController;
  bool _isSaving = false;
  String? _errorMessage;

  @override
  void initState() {
    super.initState();
    _valueController = TextEditingController(text: widget.initialValue);
    _unitController = TextEditingController(text: widget.initialUnit ?? '');
    _dateController = TextEditingController(text: widget.initialDate ?? '');
  }

  @override
  void dispose() {
    _valueController.dispose();
    _unitController.dispose();
    _dateController.dispose();
    super.dispose();
  }

  Future<void> _onApplyPressed() async {
    final confirmed = await showDialog<bool>(
      context: context,
      builder: (ctx) => AlertDialog(
        backgroundColor: const Color(0xFF1E1E1E),
        title: const Text("Confirm Changes", style: TextStyle(color: Colors.white)),
        content: const Text(
          "Apply changes to your lab results?",
          style: TextStyle(color: Colors.white70),
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.of(ctx).pop(false),
            child: const Text("Cancel", style: TextStyle(color: Colors.grey)),
          ),
          ElevatedButton(
            style: ElevatedButton.styleFrom(backgroundColor: Colors.blueAccent),
            onPressed: () => Navigator.of(ctx).pop(true),
            child: const Text("Apply changes", style: TextStyle(color: Colors.white)),
          ),
        ],
      ),
    );

    if (confirmed != true) {
      // Cancel does not save changes or trigger processing
      return;
    }

    setState(() {
      _isSaving = true;
      _errorMessage = null;
    });

    try {
      final client = context.read<AegisApiClient>();
      final numVal = double.tryParse(_valueController.text.trim().replaceAll(',', '.'));
      final mutations = [
        {
          'marker_key': widget.markerKey,
          'display_name': widget.displayName,
          'value': numVal ?? _valueController.text.trim(),
          'unit': _unitController.text.trim().isEmpty ? null : _unitController.text.trim(),
          'sample_date': _dateController.text.trim().isEmpty ? null : _dateController.text.trim(),
          'is_user_corrected': true,
        }
      ];

      await client.submitManualLabEdit(mutations: mutations);
      if (mounted) {
        Navigator.of(context).pop(true);
      }
    } catch (e) {
      if (mounted) {
        setState(() {
          _isSaving = false;
          _errorMessage = "Failed to save: $e";
        });
      }
    }
  }

  @override
  Widget build(BuildContext context) {
    return AlertDialog(
      backgroundColor: const Color(0xFF161B22),
      title: Text("Edit ${widget.displayName}", style: const TextStyle(color: Colors.blueAccent, fontSize: 16)),
      content: SingleChildScrollView(
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            TextField(
              controller: _valueController,
              keyboardType: const TextInputType.numberWithOptions(decimal: true),
              style: const TextStyle(color: Colors.white),
              decoration: const InputDecoration(
                labelText: "Value",
                labelStyle: TextStyle(color: Colors.white70),
              ),
            ),
            const SizedBox(height: 12),
            TextField(
              controller: _unitController,
              style: const TextStyle(color: Colors.white),
              decoration: const InputDecoration(
                labelText: "Unit (optional)",
                labelStyle: TextStyle(color: Colors.white70),
              ),
            ),
            const SizedBox(height: 12),
            TextField(
              controller: _dateController,
              style: const TextStyle(color: Colors.white),
              decoration: const InputDecoration(
                labelText: "Sample Date (YYYY-MM-DD)",
                labelStyle: TextStyle(color: Colors.white70),
              ),
            ),
            if (_errorMessage != null) ...[
              const SizedBox(height: 12),
              Text(_errorMessage!, style: const TextStyle(color: Colors.redAccent, fontSize: 12)),
            ],
          ],
        ),
      ),
      actions: [
        TextButton(
          onPressed: _isSaving ? null : () => Navigator.of(context).pop(false),
          child: const Text("Cancel", style: TextStyle(color: Colors.grey)),
        ),
        ElevatedButton(
          style: ElevatedButton.styleFrom(backgroundColor: Colors.blueAccent),
          onPressed: _isSaving ? null : _onApplyPressed,
          child: _isSaving
              ? const SizedBox(width: 16, height: 16, child: CircularProgressIndicator(strokeWidth: 2, color: Colors.white))
              : const Text("Apply changes", style: TextStyle(color: Colors.white)),
        ),
      ],
    );
  }
}
