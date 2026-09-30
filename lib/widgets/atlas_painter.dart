import 'package:flutter/material.dart';
import 'dart:ui' as ui; // Нужен для градиентов
import '../services/vault_service.dart';

class AtlasPainter extends CustomPainter {
  final VaultService vault;
  AtlasPainter(this.vault) : super(repaint: vault);

  @override
  void paint(Canvas canvas, Size size) {

    Paint framePaint = Paint()
      ..color = const Color(0xFF1E88E5).withOpacity(0.2)
      ..style = PaintingStyle.stroke
      ..strokeWidth = 1.2;

    Path body = Path();
    // ... Твой код рисования контура (головы и торса) ...
    canvas.drawPath(body, framePaint);

    // --- ОТРИСОВКА ОРГАНОВ (ИСПРАВЛЕННАЯ БАЗА) ---

  }

  @override
  bool shouldRepaint(covariant AtlasPainter oldDelegate) => true;
}