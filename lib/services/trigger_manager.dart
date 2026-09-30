// lib/core/services/trigger_manager.dart
import 'dart:async';
import 'package:flutter/foundation.dart';
import 'time_service.dart';

enum AthenaState {
  notStarted,
  onboardingDone, // Прошли приветствие, ждем +30 мин
  waitingForUserResponse, // Вопрос задан, ждем ответ
  completedForToday, // Ответил, спячка 24ч
}

class TriggerManager extends ChangeNotifier {
  final TimeService _timeService = TimeService();

  DateTime? _onboardingTime;
  DateTime? _questionSentTime;
  DateTime? _lastCompletedTime;

  AthenaState _state = AthenaState.notStarted;
  AthenaState get state => _state;

  TriggerManager() {
    _timeService.addListener(_checkTriggers);
  }

  /// Фиксируем завершение первичного знакомства
  void completeOnboarding() {
    _onboardingTime = _timeService.now;
    _state = AthenaState.onboardingDone;
    notifyListeners();
  }

  /// Фиксируем, что пользователь ответил на дневной вопрос
  void completeDailyQuestion() {
    _lastCompletedTime = _timeService.now;
    _state = AthenaState.completedForToday;
    notifyListeners();
  }

  void _checkTriggers() {
    final now = _timeService.now;

    // 1. Прошло 30 минут после знакомства -> пора задавать первый вопрос
    if (_state == AthenaState.onboardingDone && _onboardingTime != null) {
      if (now.difference(_onboardingTime!) >= const Duration(minutes: 30)) {
        _state = AthenaState.waitingForUserResponse;
        _questionSentTime = now;
        notifyListeners();
        // ТУТ: Вызов CuratorAgent для генерации первого вопроса
      }
    }

    // 2. Прошло 4 часа без ответа -> высылаем мягкое напоминание
    if (_state == AthenaState.waitingForUserResponse && _questionSentTime != null) {
      if (now.difference(_questionSentTime!) >= const Duration(hours: 4)) {
        // ТУТ: Отправка мягкого Push/Notification ("У вас 1 пропущенное от Афины")
      }
    }

    // 3. Прошло 24 часа с последнего выполненного вопроса -> новый дневной вопрос
    if (_state == AthenaState.completedForToday && _lastCompletedTime != null) {
      if (now.difference(_lastCompletedTime!) >= const Duration(hours: 24)) {
        _state = AthenaState.waitingForUserResponse;
        _questionSentTime = now;
        notifyListeners();
        // ТУТ: Вызов CuratorAgent для следующего вопроса по плану
      }
    }
  }

  @override
  void dispose() {
    _timeService.removeListener(_checkTriggers);
    super.dispose();
  }
}