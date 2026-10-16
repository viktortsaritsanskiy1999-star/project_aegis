import 'dart:async';
import 'package:flutter/material.dart';
import 'package:provider/provider.dart';
import '../services/vitals_monitor_service.dart';
import '../services/time_service.dart';
import 'debug_time_panel.dart';

/// Non-dismissible full-screen workout tracking modal.
/// Blocks the whole application UI while keeping DebugTimePanel accessible for testing.
class WorkoutSessionModal extends StatefulWidget {
  const WorkoutSessionModal({super.key});

  static Future<void> show(BuildContext context) {
    return showModalBottomSheet(
      context: context,
      isScrollControlled: true,
      isDismissible: false,
      enableDrag: false,
      backgroundColor: Colors.transparent,
      builder: (_) => const WorkoutSessionModal(),
    );
  }

  @override
  State<WorkoutSessionModal> createState() => _WorkoutSessionModalState();
}

class _WorkoutSessionModalState extends State<WorkoutSessionModal> {
  final TextEditingController _customPulseController = TextEditingController();

  @override
  void dispose() {
    _customPulseController.dispose();
    super.dispose();
  }

  String _formatDuration(Duration duration) {
    final hours = duration.inHours.toString().padLeft(2, '0');
    final minutes = (duration.inMinutes % 60).toString().padLeft(2, '0');
    final seconds = (duration.inSeconds % 60).toString().padLeft(2, '0');
    return "$hours:$minutes:$seconds";
  }

  String _formatTime(DateTime time) {
    final h = time.hour.toString().padLeft(2, '0');
    final m = time.minute.toString().padLeft(2, '0');
    final s = time.second.toString().padLeft(2, '0');
    return "$h:$m:$s";
  }

  @override
  Widget build(BuildContext context) {
    final vitalsService = context.watch<VitalsMonitorService>();
    final timeService = context.watch<TimeService>();

    final workoutStart = vitalsService.workoutStartTime ?? timeService.now;
    final elapsed = timeService.now.difference(workoutStart);
    final currentPulse = vitalsService.currentPulse;
    final pulseLog = vitalsService.workoutPulseLog;

    return PopScope(
      canPop: false, // Prevents Android hardware / gesture back navigation
      child: Container(
        height: MediaQuery.of(context).size.height * 0.95,
        decoration: const BoxDecoration(
          color: Color(0xFF0D1117),
          borderRadius: BorderRadius.vertical(top: Radius.circular(24)),
          border: Border(
            top: BorderSide(color: Colors.greenAccent, width: 2),
            left: BorderSide(color: Colors.greenAccent, width: 0.5),
            right: BorderSide(color: Colors.greenAccent, width: 0.5),
          ),
        ),
        child: SafeArea(
          child: Padding(
            padding: const EdgeInsets.symmetric(horizontal: 20, vertical: 16),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.stretch,
              children: [
                // Top header handle + badge
                Center(
                  child: Container(
                    padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 6),
                    decoration: BoxDecoration(
                      color: Colors.greenAccent.withValues(alpha: 0.15),
                      borderRadius: BorderRadius.circular(20),
                      border: Border.all(color: Colors.greenAccent.withValues(alpha: 0.5)),
                    ),
                    child: const Row(
                      mainAxisSize: MainAxisSize.min,
                      children: [
                        Icon(Icons.directions_run_rounded, color: Colors.greenAccent, size: 18),
                        SizedBox(width: 6),
                        Text(
                          "WORKOUT IN PROGRESS",
                          style: TextStyle(
                            color: Colors.greenAccent,
                            fontSize: 12,
                            fontWeight: FontWeight.bold,
                            letterSpacing: 1.2,
                          ),
                        ),
                      ],
                    ),
                  ),
                ),
                const SizedBox(height: 12),

                // Main instruction text
                const Text(
                  "Хорошо, тренировка зафиксирована!",
                  textAlign: TextAlign.center,
                  style: TextStyle(
                    color: Colors.white,
                    fontSize: 18,
                    fontWeight: FontWeight.bold,
                  ),
                ),
                const SizedBox(height: 6),
                const Text(
                  "Скрипт фиксирует пульс поминутно. Как закончишь — нажми кнопку «Тренировка завершена». Весь остальной интерфейс заблокирован для чистоты записи.",
                  textAlign: TextAlign.center,
                  style: TextStyle(
                    color: Colors.white70,
                    fontSize: 13,
                    height: 1.3,
                  ),
                ),
                const SizedBox(height: 12),

                // Scrollable content area
                Expanded(
                  child: SingleChildScrollView(
                    child: Column(
                      crossAxisAlignment: CrossAxisAlignment.stretch,
                      children: [
                        // Timer card
                        Container(
                          padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 12),
                          decoration: BoxDecoration(
                            color: const Color(0xFF161B22),
                            borderRadius: BorderRadius.circular(14),
                            border: Border.all(color: Colors.white12),
                          ),
                          child: Column(
                            children: [
                              const Text(
                                "ДЛИТЕЛЬНОСТЬ ТРЕНИРОВКИ",
                                style: TextStyle(
                                  color: Colors.white54,
                                  fontSize: 11,
                                  letterSpacing: 1.2,
                                  fontWeight: FontWeight.w600,
                                ),
                              ),
                              const SizedBox(height: 4),
                              Text(
                                _formatDuration(elapsed),
                                style: const TextStyle(
                                  color: Colors.greenAccent,
                                  fontSize: 30,
                                  fontWeight: FontWeight.bold,
                                  fontFamily: 'Courier',
                                  letterSpacing: 2,
                                ),
                              ),
                              const SizedBox(height: 4),
                              Text(
                                "Начало: ${_formatTime(workoutStart)} (момент алерта)",
                                style: const TextStyle(
                                  color: Colors.white54,
                                  fontSize: 12,
                                ),
                              ),
                            ],
                          ),
                        ),
                        const SizedBox(height: 12),

                        // Current Pulse + Testing Injection Controls
                        Container(
                          padding: const EdgeInsets.all(14),
                          decoration: BoxDecoration(
                            color: const Color(0xFF161B22),
                            borderRadius: BorderRadius.circular(14),
                            border: Border.all(color: Colors.cyanAccent.withValues(alpha: 0.3)),
                          ),
                          child: Column(
                            crossAxisAlignment: CrossAxisAlignment.start,
                            children: [
                              Row(
                                mainAxisAlignment: MainAxisAlignment.spaceBetween,
                                children: [
                                  Row(
                                    children: [
                                      const Icon(Icons.favorite_rounded, color: Colors.redAccent, size: 22),
                                      const SizedBox(width: 8),
                                      const Text(
                                        "Текущий пульс:",
                                        style: TextStyle(color: Colors.white, fontSize: 14, fontWeight: FontWeight.bold),
                                      ),
                                    ],
                                  ),
                                  Text(
                                    "$currentPulse bpm",
                                    style: TextStyle(
                                      color: currentPulse == 0
                                          ? Colors.redAccent
                                          : (currentPulse >= 140 ? Colors.orangeAccent : Colors.cyanAccent),
                                      fontSize: 20,
                                      fontWeight: FontWeight.bold,
                                    ),
                                  ),
                                ],
                              ),
                              const SizedBox(height: 10),
                              const Text(
                                "🧪 Быстрый тест пульса для симуляции:",
                                style: TextStyle(color: Colors.white60, fontSize: 11),
                              ),
                              const SizedBox(height: 6),
                              Wrap(
                                spacing: 8,
                                runSpacing: 6,
                                children: [
                                  _buildPulseChip(vitalsService, 135),
                                  _buildPulseChip(vitalsService, 142),
                                  _buildPulseChip(vitalsService, 150),
                                  _buildPulseChip(vitalsService, 85),
                                  _buildPulseChip(vitalsService, 0, isDanger: true),
                                ],
                              ),
                              const SizedBox(height: 8),
                              Row(
                                children: [
                                  Expanded(
                                    child: SizedBox(
                                      height: 38,
                                      child: TextField(
                                        controller: _customPulseController,
                                        keyboardType: TextInputType.number,
                                        style: const TextStyle(color: Colors.white, fontSize: 13),
                                        decoration: InputDecoration(
                                          hintText: "Задать свой пульс...",
                                          hintStyle: const TextStyle(color: Colors.white38, fontSize: 12),
                                          filled: true,
                                          fillColor: Colors.black26,
                                          contentPadding: const EdgeInsets.symmetric(horizontal: 10, vertical: 8),
                                          border: OutlineInputBorder(
                                            borderRadius: BorderRadius.circular(8),
                                            borderSide: const BorderSide(color: Colors.white24),
                                          ),
                                        ),
                                      ),
                                    ),
                                  ),
                                  const SizedBox(width: 8),
                                  SizedBox(
                                    height: 38,
                                    child: ElevatedButton(
                                      style: ElevatedButton.styleFrom(
                                        backgroundColor: Colors.cyanAccent.withValues(alpha: 0.2),
                                        foregroundColor: Colors.cyanAccent,
                                        side: const BorderSide(color: Colors.cyanAccent),
                                        padding: const EdgeInsets.symmetric(horizontal: 12),
                                      ),
                                      onPressed: () {
                                        final val = int.tryParse(_customPulseController.text.trim());
                                        if (val != null) {
                                          vitalsService.onNewPulseRead(val);
                                          _customPulseController.clear();
                                        }
                                      },
                                      child: const Text("OK", style: TextStyle(fontWeight: FontWeight.bold)),
                                    ),
                                  ),
                                ],
                              ),
                            ],
                          ),
                        ),
                        const SizedBox(height: 12),

                        // Embedded Virtual Time Panel (DebugTimePanel) for testing
                        Container(
                          padding: const EdgeInsets.all(12),
                          decoration: BoxDecoration(
                            color: const Color(0xFF161B22),
                            borderRadius: BorderRadius.circular(14),
                            border: Border.all(color: Colors.cyanAccent.withValues(alpha: 0.4)),
                          ),
                          child: const Column(
                            crossAxisAlignment: CrossAxisAlignment.start,
                            children: [
                              Row(
                                children: [
                                  Icon(Icons.access_time_rounded, color: Colors.cyanAccent, size: 16),
                                  SizedBox(width: 6),
                                  Text(
                                    "СИМУЛЯЦИЯ ВИРТУАЛЬНОГО ВРЕМЕНИ (ДЛЯ ТЕСТОВ)",
                                    style: TextStyle(
                                      color: Colors.cyanAccent,
                                      fontSize: 11,
                                      fontWeight: FontWeight.bold,
                                      letterSpacing: 1,
                                    ),
                                  ),
                                ],
                              ),
                              SizedBox(height: 10),
                              DebugTimePanel(),
                            ],
                          ),
                        ),
                        const SizedBox(height: 12),

                        // Minute-by-minute pulse log
                        Container(
                          padding: const EdgeInsets.all(14),
                          decoration: BoxDecoration(
                            color: const Color(0xFF161B22),
                            borderRadius: BorderRadius.circular(14),
                            border: Border.all(color: Colors.white12),
                          ),
                          child: Column(
                            crossAxisAlignment: CrossAxisAlignment.start,
                            children: [
                              Row(
                                mainAxisAlignment: MainAxisAlignment.spaceBetween,
                                children: [
                                  const Text(
                                    "ПОМИНУТНЫЙ ЖУРНАЛ ЗАМЕРОВ",
                                    style: TextStyle(
                                      color: Colors.white70,
                                      fontSize: 11,
                                      fontWeight: FontWeight.bold,
                                      letterSpacing: 1,
                                    ),
                                  ),
                                  Text(
                                    "Записей: ${pulseLog.length}",
                                    style: const TextStyle(color: Colors.greenAccent, fontSize: 11),
                                  ),
                                ],
                              ),
                              const SizedBox(height: 8),
                              if (pulseLog.isEmpty)
                                const Padding(
                                  padding: EdgeInsets.symmetric(vertical: 8),
                                  child: Text(
                                    "Журнал пуст. Замеры фиксируются каждую 1 минуту времени.",
                                    style: TextStyle(color: Colors.white38, fontSize: 12),
                                  ),
                                )
                              else
                                ListView.separated(
                                  shrinkWrap: true,
                                  physics: const NeverScrollableScrollPhysics(),
                                  reverse: true,
                                  itemCount: pulseLog.length,
                                  separatorBuilder: (context, index) => const Divider(color: Colors.white10, height: 8),
                                  itemBuilder: (context, index) {
                                    final item = pulseLog[pulseLog.length - 1 - index];
                                    final timeStr = _formatTime(item.timestamp);
                                    return Row(
                                      mainAxisAlignment: MainAxisAlignment.spaceBetween,
                                      children: [
                                        Text(
                                          timeStr,
                                          style: const TextStyle(color: Colors.white54, fontSize: 12, fontFamily: 'Courier'),
                                        ),
                                        Text(
                                          "${item.pulse} bpm",
                                          style: TextStyle(
                                            color: item.pulse == 0 ? Colors.redAccent : Colors.white,
                                            fontSize: 12,
                                            fontWeight: FontWeight.bold,
                                          ),
                                        ),
                                        Text(
                                          item.note.isNotEmpty ? item.note : item.phase,
                                          style: const TextStyle(color: Colors.white38, fontSize: 11),
                                        ),
                                      ],
                                    );
                                  },
                                ),
                            ],
                          ),
                        ),
                        const SizedBox(height: 16),
                      ],
                    ),
                  ),
                ),

                // Finish Workout Button
                SizedBox(
                  height: 52,
                  child: ElevatedButton(
                    style: ElevatedButton.styleFrom(
                      backgroundColor: Colors.greenAccent,
                      foregroundColor: Colors.black,
                      shape: RoundedRectangleBorder(
                        borderRadius: BorderRadius.circular(14),
                      ),
                      elevation: 0,
                    ),
                    onPressed: () {
                      vitalsService.finishWorkout();
                      Navigator.of(context).pop();
                    },
                    child: const Row(
                      mainAxisAlignment: MainAxisAlignment.center,
                      children: [
                        Icon(Icons.check_circle_outline_rounded, size: 22),
                        SizedBox(width: 8),
                        Text(
                          "ТРЕНИРОВКА ЗАВЕРШЕНА",
                          style: TextStyle(
                            fontSize: 16,
                            fontWeight: FontWeight.bold,
                            letterSpacing: 1,
                          ),
                        ),
                      ],
                    ),
                  ),
                ),
              ],
            ),
          ),
        ),
      ),
    );
  }

  Widget _buildPulseChip(VitalsMonitorService service, int bpm, {bool isDanger = false}) {
    return InkWell(
      onTap: () => service.onNewPulseRead(bpm),
      borderRadius: BorderRadius.circular(8),
      child: Container(
        padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 5),
        decoration: BoxDecoration(
          color: isDanger
              ? Colors.redAccent.withValues(alpha: 0.2)
              : Colors.cyanAccent.withValues(alpha: 0.15),
          borderRadius: BorderRadius.circular(8),
          border: Border.all(
            color: isDanger ? Colors.redAccent : Colors.cyanAccent.withValues(alpha: 0.5),
          ),
        ),
        child: Text(
          "$bpm bpm",
          style: TextStyle(
            color: isDanger ? Colors.redAccent : Colors.cyanAccent,
            fontSize: 12,
            fontWeight: FontWeight.bold,
          ),
        ),
      ),
    );
  }
}
