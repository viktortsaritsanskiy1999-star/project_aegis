// lib/core/services/time_service.dart
import 'package:flutter/foundation.dart';

class TimeService extends ChangeNotifier {
  static final TimeService _instance = TimeService._internal();
  factory TimeService() => _instance;
  TimeService._internal();

  Duration _offset = Duration.zero;

  /// Current application time (accounting for time rewinding)
  DateTime get now => DateTime.now().add(_offset);

  /// Current time offset
  Duration get currentOffset => _offset;

  /// Rewind time forward (e.g., fastForward(Duration(hours: 24)))
  void fastForward(Duration duration) {
    _offset += duration;
    notifyListeners();
  }

  /// Reset to real time and date
  void resetToRealTime() {
    _offset = Duration.zero;
    notifyListeners();
  }

  /// Check: whether a new calendar day has arrived relative to the [lastDate] timestamp
  bool isNewCalendarDay(DateTime lastDate) {
    final currentDate = now;
    return currentDate.year != lastDate.year ||
        currentDate.month != lastDate.month ||
        currentDate.day != lastDate.day;
  }
}