# PROJECT AEGIS — FULL SYSTEM COVERAGE MATRIX
## Комплексная матрица функционального покрытия и инвентаризация системы (Audited Version)

**Проект:** Project Aegis  
**Версия спецификации:** Master Specification v1.0 & Stage 8 Roadmap  
**Git baseline:** `4241c468e1da9c631863eff6ea8bbef174aa4059`  
**Дата аудита:** 10 ноября 2026 г.  
**Статус аудита:** VERIFIED & RECONCILED

---

### Легенда статусов матрицы:
- **`MAIN_JOURNEY`** — Функция непосредственно вызвана и проверена строгими assertions в основном непрерывном сквозном сценарии единого пациента (Фазы A–H).
- **`BRANCH_TEST`** — Функция проверена строгими assertions в одном из 10 специализированных ветвящихся интеграционных тестов на изолированном снимке состояния (`test_branch_01` — `test_branch_10`).
- **`EXISTING_VERIFIED`** — Функция подтверждена существующими специализированными регрессионными тестами (`test_auth_api_isolation.py`, `test_client_cascade_isolation.py` и др.) без дублирования в непрерывном сценарии симуляции.
- **`NOT_TESTED`** — Функция не протестирована в рамках доступных тестов (в текущем наборе таких функций: 0).
- **`NOT_IMPLEMENTED`** — Функция заявлена в спецификации, но отсутствует в коде (в текущем наборе: 0).

---

## 1. Реестр функциональных доменов и компонентов

| # | Домен / Подсистема | Основные компоненты и сервисы | Маршруты API / Точки входа |
|---|---|---|---|
| **D1** | **Аутентификация и изоляция устройств** | `AuthService`, `TokenRepository`, `DeviceRepository` | `/api/v1/auth/pair`, `/refresh`, `/revoke` |
| **D2** | **Медицинские циклы (Lifecycle)** | `MedicalCycleRepository`, `NeoCycleService` | `/api/v1/cycle/start`, `/current`, `/close`, `/archive`, `/start-neo-reassessment` |
| **D3** | **Сбор первичного анамнеза (M1 Intake)** | `PrimaryConsiliumService`, `ModelReportRepository` | `/api/v1/client/cascade/intake/anamnesis` |
| **D4** | **Ввод биометрии и телеметрии** | `HealthMeasurementRepository`, `Mode2VitalsMonitoringService` | `/api/v1/vitals/measurement`, `/vitals/batch`, `/vitals/latest` |
| **D5** | **Инструментальная диагностика (Vision OCR & ECG)** | `LabVisionOcrService`, `EcgVisionService`, `EcgContextService` | `/api/v1/client/labs/upload`, `/api/v1/client/ecg/upload` |
| **D6** | **Первичный консилиум 10 моделей (Initial Consilium)** | `PrimaryConsiliumService`, `GeminiGateway` | `/api/v1/client/cascade/run-primary` |
| **D7** | **Сейф здоровья и приватность данных** | `CuratorVaultService`, `ModelReportRepository` | `/api/v1/reports/client`, `/api/v1/client/cascade/status` |
| **D8** | **Фоновый оркестратор и автоматизация** | `ClinicalAutomationService`, `ClinicalAutomationWorker` | Фоновый планировщик задач SQLite |
| **D9** | **Сопровождение Афины и планировщик недели** | `AthenaChatService`, `AthenaWeeklyPlanService` | `/api/v1/client/chat/athena`, `/athena/history` |
| **D10** | **Суточный велнес и мониторинг сна (Wellness Daily)** | `WellnessDailyService` | Задача `wellness_daily` |
| **D11** | **Клинический суточный дневник (Athena Daily Journal)** | `AthenaDailyJournalService` | Задача `daily_journal_closure` |
| **D12** | **Мониторинг ЧСС, тренировок и инцидентов (Mode 2)** | `Mode2VitalsMonitoringService` | `/api/v1/vitals/mode2/*` |
| **D13** | **Внеочередной консилиум анализов (Lab Revision)** | `LabRevisionConsiliumService`, `LabSchedulingService` | `/api/v1/cascade/run-lab-revision`, `/api/v1/client/labs/upload` |
| **D14** | **Еженедельный консилиум (Weekly Consilium)** | `WeeklyConsiliumService`, `SprintRepository` | `/api/v1/client/cascade/run-weekly`, задачи Step A/B/C |
| **D15** | **Врачебное направление (Referral & Safe Document)** | `ReferralDocumentService`, `DoctorDocumentExportService` | `/api/v1/client/referral/documents`, `/export` |
| **D16** | **Повторное обследование (Neo Consilium & Activation)** | `NeoCycleService`, `NeoConsiliumActivationService` | `/api/v1/cycles/start-neo-reassessment`, `/activate-neo` |

---

## 2. Подробная матрица функционального покрытия

### Домен D1: Аутентификация, сессии и безопасность устройств
| ID | Функция / Метод | Статус | Триггер / Входные данные | Ограничения и инварианты | Сохранение и Downstream | Тестовая привязка |
|---|---|---|---|---|---|---|
| **F1.1** | `AuthService.pair_device` | `EXISTING_VERIFIED` | `POST /api/v1/auth/pair`, pairing code | Одноразовый код, rate limit, изоляция `patient_id` | Создаёт `Device`, выдаёт пару JWT (Access + Refresh) | `test_auth_api_isolation.py::test_01` |
| **F1.2** | `AuthService.refresh_tokens` | `EXISTING_VERIFIED` | `POST /api/v1/auth/refresh`, refresh_token | Проверка отзыва токена, ротация Refresh-токена | Обновляет `DeviceRefreshToken` | `test_auth_api_isolation.py::test_12` |
| **F1.3** | `AuthService.revoke_device` | `EXISTING_VERIFIED` | `POST /api/v1/auth/revoke`, device_id | Немедленный отзыв доступа устройства | Помечает сессию недействительной | `test_auth_api_isolation.py::test_15` |

---

### Домен D2: Жизненный цикл медицинского цикла (Medical Cycle)
| ID | Функция / Метод | Статус | Триггер / Входные данные | Ограничения и инварианты | Сохранение и Downstream | Тестовая привязка |
|---|---|---|---|---|---|---|
| **F2.1** | `MedicalCycleRepository.create` | `MAIN_JOURNEY` | First-run / старт онбординга | Привязка к `patient_id`, статус `active` | Запись в `medical_cycles` | `test_full_system_continuous_lifecycle_journey` (Phase A) |
| **F2.2** | `MedicalCycleRepository.archive_cycle` | `MAIN_JOURNEY` | Запуск Neo Reassessment | Старый цикл `completed`/`archived` | `closed_at`, фиксация истории | `test_full_system_continuous_lifecycle_journey` (Phase G) |
| **F2.3** | `MedicalCycleRepository.get_active_for_patient` | `MAIN_JOURNEY` | Все авторизованные запросы | Строго 1 активный цикл на пациента | Чтение актуального `cycle_id` | `test_full_system_continuous_lifecycle_journey` (All Phases) |

---

### Домен D3: Сбор первичного анамнеза (M1 Intake)
| ID | Функция / Метод | Статус | Триггер / Входные данные | Ограничения и инварианты | Сохранение и Downstream | Тестовая привязка |
|---|---|---|---|---|---|---|
| **F3.1** | `PrimaryConsiliumService.process_anamnesis_intake` | `MAIN_JOURNEY` | `POST /api/v1/client/cascade/intake/anamnesis` | Валидация 18 полей схемы, Blind Mode | Сохранение `anamnesis_questionnaire` | `test_full_system_continuous_lifecycle_journey` (Phase A) |
| **F3.2** | `PrimaryConsiliumService.run_preparatory_blind_anamnesis` | `MAIN_JOURNEY` | Завершение ввода анамнеза | Формирование первичного отчёта M1 `SERVER_ONLY` | Создание отчёта `preparatory_anamnesis` | `test_full_system_continuous_lifecycle_journey` (Phase A) |
| **F3.3** | Повторный вызов intake M1 (Idempotency / Correction) | `EXISTING_VERIFIED` | Повторный ввод той же или исправленной анкеты | Запрет искажения данных при повторе | Обновление ответов без дублирования | `test_client_cascade_isolation.py::test_anamnesis_intake_correction` |

---

### Домен D4: Ввод биометрии и телеметрии
| ID | Функция / Метод | Статус | Триггер / Входные данные | Ограничения и инварианты | Сохранение и Downstream | Тестовая привязка |
|---|---|---|---|---|---|---|
| **F4.1** | `HealthMeasurementRepository.create_batch` | `MAIN_JOURNEY` | `POST /api/v1/vitals/batch` (смарт-часы) | Пакетная валидация, привязка к циклу и времени | Запись в `health_measurements` | `test_full_system_continuous_lifecycle_journey` (Phase A, B) |
| **F4.2** | Ручные измерения на онбординге | `EXISTING_VERIFIED` | `POST /api/v1/vitals/measurement` | Одиночное измерение, проверка допустимости диапазона | Запись разового замера | `test_vitals_router.py` |
| **F4.3** | Очередь синхронизации (VitalsSyncQueue barrier) | `EXISTING_VERIFIED` | Клиентские замеры в Flutter при смене цикла | Сброс буферов до активации Neo | Защита от утечки измерений | `stage_8c1_flutter_e2e_contract_test.dart` (Test 2) |

---

### Домен D5: Инструментальная диагностика (Vision OCR & ECG)
| ID | Функция / Метод | Статус | Триггер / Входные данные | Ограничения и инварианты | Сохранение и Downstream | Тестовая привязка |
|---|---|---|---|---|---|---|
| **F5.1** | `LabVisionOcrService.process_lab_images` | `MAIN_JOURNEY` | `POST /api/v1/client/labs/upload` | Gemini Vision Flash 3.8, нормализация единиц | Создание структурированных биомаркеров | `test_full_system_continuous_lifecycle_journey` (Phase A) |
| **F5.2** | `EcgVisionService.process_ecg_image` | `MAIN_JOURNEY` | `POST /api/v1/client/ecg/upload` | Оцифровка ритма, PR, QRS, QT/QTc Lead I | Запись отчёта `preparatory_ecg` | `test_full_system_continuous_lifecycle_journey` (Phase A) |
| **F5.3** | Сохранение оригиналов в Safe | `MAIN_JOURNEY` | Приём любых медицинских сканов | Доступность для врача и пациента | Хранение бинарных артефактов | `test_full_system_continuous_lifecycle_journey` (Phase A, F) |

---

### Домен D6: Первичный консилиум 10 моделей (Initial Consilium)
| ID | Функция / Метод | Статус | Триггер / Входные данные | Ограничения и инварианты | Сохранение и Downstream | Тестовая привязка |
|---|---|---|---|---|---|---|
| **F6.1** | `PrimaryConsiliumService.run_primary_consilium` | `MAIN_JOURNEY` | `POST /api/v1/client/cascade/run-primary` | Должны быть готовы M1, Labs, ECG | Атомарный запуск каскада 10 моделей | `test_full_system_continuous_lifecycle_journey` (Phase A) |
| **F6.2** | 8 специализированных органных агентов | `MAIN_JOURNEY` | Каскад первичного консилиума | 8 независимых вызовов Flash-Lite 3.5 | 8 отчётов `SERVER_ONLY` в Vault | `test_full_system_continuous_lifecycle_journey` (Phase A) |
| **F6.3** | Замглавврача (Deputy Flash Master) | `MAIN_JOURNEY` | После завершения 8 органов | Межсистемный синтез, Flash 3.8 | Отчёт `deputy_physician_internal` | `test_full_system_continuous_lifecycle_journey` (Phase A) |
| **F6.4** | Главврач (Chief Argon 4 Master) & Quality Gate | `MAIN_JOURNEY` | После отчёта Замглавврача | Стратегия, персональные пороги, Argon 4 | Отчёт `overall_report` (`CLIENT_FACING`) | `test_full_system_continuous_lifecycle_journey` (Phase A) |
| **F6.5** | Идемпотентность Primary Consilium | `BRANCH_TEST` | Повторный вызов для готового цикла | Возврат существующего результата без новых LLM | 0 дополнительных токенов | `test_branch_04_primary_consilium_idempotency_zero_extra_llm_calls` |

---

### Домен D7: Сейф здоровья и приватность данных
| ID | Функция / Метод | Статус | Триггер / Входные данные | Ограничения и инварианты | Сохранение и Downstream | Тестовая привязка |
|---|---|---|---|---|---|---|
| **F7.1** | Разделение `CLIENT_FACING` vs `SERVER_ONLY` | `MAIN_JOURNEY` | Публикация любого отчёта модели | Изоляция внутренней кухни врачей | Поле `privacy_level` в `model_reports` | `test_full_system_continuous_lifecycle_journey` (All Phases) |
| **F7.2** | Получение отчётов пациента | `MAIN_JOURNEY` | `GET /api/v1/reports/client` | Возврат строго `CLIENT_FACING` отчётов | Отображение в Сейфе клиента | `test_full_system_continuous_lifecycle_journey` (Phase A, G) |
| **F7.3** | Кросс-цикловый доступ к документам | `MAIN_JOURNEY` | `GET /api/v1/client/referral/documents` | Документы старых циклов остаются доступными | Пациент не теряет историю консультаций | `test_full_system_continuous_lifecycle_journey` (Phase H) |

---

### Домен D8: Фоновый оркестратор и автоматизация
| ID | Функция / Метод | Статус | Триггер / Входные данные | Ограничения и инварианты | Сохранение и Downstream | Тестовая привязка |
|---|---|---|---|---|---|---|
| **F8.1** | `ClinicalAutomationService.enqueue_post_primary_init` | `MAIN_JOURNEY` | Успех первичного консилиума | Автоматическая постановка в очередь SQLite | Задача `POST_PRIMARY_INIT` | `test_full_system_continuous_lifecycle_journey` (Phase A) |
| **F8.2** | `ClinicalAutomationService.enqueue_first_planned_question` | `MAIN_JOURNEY` | Завершение `POST_PRIMARY_INIT` | Задержка 30 секунд виртуального времени | Задача `FIRST_PLANNED_QUESTION` | `test_full_system_continuous_lifecycle_journey` (Phase A) |
| **F8.3** | `ClinicalAutomationService.process_due_jobs` | `MAIN_JOURNEY` | Фоновый цикл воркера / тики времени | Выборка созревших задач, аренда lease | Исполнение задач и перевод в `completed` | `test_full_system_continuous_lifecycle_journey` (All Phases) |
| **F8.4** | Восстановление задач после краша сервера | `BRANCH_TEST` | Истечение срока аренды `lease_expires_at_utc` | Перехват брошенных задач другим воркером | Повторное корректное исполнение | `test_branch_09_automation_worker_crash_and_lease_recovery` |

---

### Домен D9: Сопровождение Афины и планировщик недели
| ID | Функция / Метод | Статус | Триггер / Входные данные | Ограничения и инварианты | Сохранение и Downstream | Тестовая привязка |
|---|---|---|---|---|---|---|
| **F9.1** | Установка якоря `T0` | `MAIN_JOURNEY` | Обработка `POST_PRIMARY_INIT` | Единый опорный таймстамп для всех 168ч циклов | Запись в `schedule_anchor` | `test_full_system_continuous_lifecycle_journey` (Phase A) |
| **F9.2** | Формирование недельного плана (`formulate_weekly_plan`) | `MAIN_JOURNEY` | `AthenaWeeklyPlanService` на старте недели | 7 микроцелей на основе стратегии Главврача | Запись `athena_weekly_plan` | `test_full_system_continuous_lifecycle_journey` (Phase A, H) |
| **F9.3** | Лимит вопросов ($\le 3$ в сутки) | `EXISTING_VERIFIED` | Проактивные сообщения Афины | Строгое ограничение назойливости | Счётчик `questions_asked_count` | `test_athena_chat_service.py` |
| **F9.4** | Оценка достаточности ответа пользователя | `EXISTING_VERIFIED` | Ответ пациента на вопрос плана | Flash оценивает sufficiency_criteria | Закрытие цели плана `status = completed` | `test_athena_chat_service.py` |
| **F9.5** | Итоговое сообщение об окончании недели (`weekly_wrapup`) | `EXISTING_VERIFIED` | Завершение 7-го дня спринта | Дружелюбное резюме недели для пациента | Доставка сообщения wrap-up в чат | `test_weekly_consilium_service.py` |

---

### Домен D10: Суточный велнес и мониторинг сна (Wellness Daily)
| ID | Функция / Метод | Статус | Триггер / Входные данные | Ограничения и инварианты | Сохранение и Downstream | Тестовая привязка |
|---|---|---|---|---|---|---|
| **F10.1** | `WellnessDailyService.process_wellness_for_day` (с данными) | `MAIN_JOURNEY` | Окончание суток (06:00 утра), есть замеры | Анализ сна, покоя, активности, Flash 3.8 | Отчёт `WellnessDailyReport` | `test_full_system_continuous_lifecycle_journey` (Phase B) |
| **F10.2** | Генерация детерминированного отчёта `NO_DATA` | `MAIN_JOURNEY` | Окончание суток, 0 данных с датчиков | Детерминированный отчёт без сбоев | Статус `WellnessStatus.NO_DATA` | `test_full_system_continuous_lifecycle_journey` (Phase B) |
| **F10.3** | Переход к Daily Journal после готовности Wellness | `MAIN_JOURNEY` | Завершение суточного анализа велнеса | Передача витальных доказательств в дневник | Автосрабатывание `daily_journal_closure` | `test_full_system_continuous_lifecycle_journey` (Phase B) |

---

### Домен D11: Клинический суточный дневник (Athena Daily Journal)
| ID | Функция / Метод | Статус | Триггер / Входные данные | Ограничения и инварианты | Сохранение и Downstream | Тестовая привязка |
|---|---|---|---|---|---|---|
| **F11.1** | `AthenaDailyJournalService.build_daily_journal` | `MAIN_JOURNEY` | Закрытие дня воркером автоматизации | Объединение ответов пациента и велнеса | Запись `AthenaDailyJournalRecord` | `test_full_system_continuous_lifecycle_journey` (Phase B) |
| **F11.2** | Дневник при отсутствии данных велнеса (`NO_DATA`) | `MAIN_JOURNEY` | Сутки без носимых датчиков | Сохраняются только диалоги с пациентом | Запись журнала с пустым `wellness_evidence` | `test_full_system_continuous_lifecycle_journey` (Phase B) |
| **F11.3** | Форматирование 7-дневной сводки для Weekly | `EXISTING_VERIFIED` | Вызов Step A еженедельного консилиума | Агрегация 7 записей дневника в хронологию | Входные данные для Flash Master | `test_athena_daily_journal.py` |

---

### Домен D12: Мониторинг ЧСС, тренировок и инцидентов (Mode 2)
| ID | Функция / Метод | Статус | Триггер / Входные данные | Ограничения и инварианты | Сохранение и Downstream | Тестовая привязка |
|---|---|---|---|---|---|---|
| **F12.1** | Детекция скачка ЧСС (`is_deviation_detected`) | `MAIN_JOURNEY` | Пульс $\ge 115$ уд/мин или baseline + 35 | Переход в состояние опроса контекста | Создание активного эпизода Mode 2 | `test_full_system_continuous_lifecycle_journey` (Phase C) |
| **F12.2** | Триаж: подтверждена тренировка | `MAIN_JOURNEY` | Пользователь ответил «Да, бегу на дорожке» | Создание тренировочной сессии, подавление тревоги | `workout_context = confirmed_workout` | `test_full_system_continuous_lifecycle_journey` (Phase C) |
| **F12.3** | Триаж: человек отдыхает (Resting Spike Alert) | `BRANCH_TEST` | Ответ «Нет, лежу на диване с книгой» | Эскалация, запись клинического предупреждения | Клиническая тревога `status = alert` | `test_branch_02_mode2_resting_spike_triage_and_chat_escalation` |
| **F12.4** | Завершение эпизода и наблюдение восстановления | `MAIN_JOURNEY` | Вызов `finish_workout` / `close_episode` | Переход сессии в фазу восстановления | Закрытие эпизода Mode 2 | `test_full_system_continuous_lifecycle_journey` (Phase C) |

---

### Домен D13: Внеочередной консилиум анализов (Lab Revision)
| ID | Функция / Метод | Статус | Триггер / Входные данные | Ограничения и инварианты | Сохранение и Downstream | Тестовая привязка |
|---|---|---|---|---|---|---|
| **F13.1** | `LabRevisionConsiliumService.upsert_laboratory_markers` | `MAIN_JOURNEY` | Поступление новых анализов в леджер | Разрешено **в любое время с ZERO cooldown** | Инкремент версии леджера | `test_full_system_continuous_lifecycle_journey` (Phase D) |
| **F13.2** | `LabRevisionConsiliumService.compute_laboratory_diff` | `MAIN_JOURNEY` | Подготовка консилиума ревизии | Автоматический расчёт дифференциала анализов | Входные данные для 8 Flash-Lite | `test_full_system_continuous_lifecycle_journey` (Phase D) |
| **F13.3** | `LabRevisionConsiliumService.run_lab_revision_consilium` | `MAIN_JOURNEY` | Запуск консилиума (клиент или воркер) | 10 моделей (8 Lite $\to$ 1 Flash $\to$ 1 Argon 4) | Обновление стратегии, порогов и отчётов | `test_full_system_continuous_lifecycle_journey` (Phase D) |
| **F13.4** | Защита кулдауна 168 часов (Cooldown Guard) | `BRANCH_TEST` | Повторный запуск до истечения 168 часов | Запрет спама полными консилиумами | Выброс `ConsiliumCooldownError` | `test_branch_06_lab_revision_cooldown_rejection` |

---

### Домен D14: Еженедельный консилиум (Weekly Consilium)
| ID | Функция / Метод | Статус | Триггер / Входные данные | Ограничения и инварианты | Сохранение и Downstream | Тестовая привязка |
|---|---|---|---|---|---|---|
| **F14.1** | `WeeklyConsiliumService.run_weekly_consilium` | `MAIN_JOURNEY` | Наступление времени `T0 + N*168h` | Двухмодельный каскад (Flash Master $\to$ Argon 4) | Обновление еженедельной стратегии | `test_full_system_continuous_lifecycle_journey` (Phase E, H) |
| **F14.2** | Quality Gate Главврача Pro | `MAIN_JOURNEY` | Проверка согласованности выводов Flash | Не допускает галлюцинаций и искажения трендов | Утверждение `WeeklyProMasterOutput` | `test_full_system_continuous_lifecycle_journey` (Phase E) |
| **F14.3** | Формирование стратегии на следующую неделю | `MAIN_JOURNEY` | Завершение еженедельного консилиума | Обновление полки директив Афины | Инкремент `athena_active_strategy` | `test_full_system_continuous_lifecycle_journey` (Phase E) |
| **F14.4** | Идемпотентность Weekly Consilium | `BRANCH_TEST` | Повторный вызов для того же `sprint_id` | Защита от дублирования отчётов и перерасхода LLM | 0 повторных вызовов моделей | `test_branch_05_weekly_consilium_idempotency_zero_extra_llm_calls` |
| **F14.5** | Распределённая аренда консилиума в SQLite | `BRANCH_TEST` | Параллельный запуск двумя воркерами | Защита от состояния гонки (Race Condition Guard) | Блокировка с `ConsiliumConcurrencyConflictError` | `test_branch_10_sqlite_distributed_lease_cross_worker_fencing` |

---

### Домен D15: Врачебное направление (Referral & Safe Document)
| ID | Функция / Метод | Статус | Триггер / Входные данные | Ограничения и инварианты | Сохранение и Downstream | Тестовая привязка |
|---|---|---|---|---|---|---|
| **F15.1** | Принятие решения `ProReferralDecision` | `MAIN_JOURNEY` | Негативная динамика на Неделе 3 | Принимает **только Главврач Argon 4**, статус `generating_document` | `referral_lifecycle_state` | `test_full_system_continuous_lifecycle_journey` (Phase E, F) |
| **F15.2** | `ReferralDocumentService.generate_referral_document` | `MAIN_JOURNEY` | Фоновая задача `referral_document_generation` | Flash 3.8 (извлечение) $\to$ Argon 4 (автор документа) | `referral_doctor_document` в Сейфе | `test_full_system_continuous_lifecycle_journey` (Phase F) |
| **F15.3** | Экспорт документа (PDF, DOCX, Markdown) | `MAIN_JOURNEY` | `GET /api/v1/client/referral/documents/{id}/export` | Проверка целостности структуры, валидные байты | Безопасная выгрузка без файлов на диске | `test_full_system_continuous_lifecycle_journey` (Phase F) |
| **F15.4** | 168-часовые напоминания о визите к врачу | `MAIN_JOURNEY` | Фоновая задача `referral_followup_reminder` | Каждые 168 часов, пока `status != 'visit_confirmed'` | Напоминание в чат Афины | `test_full_system_continuous_lifecycle_journey` (Phase F) |
| **F15.5** | Распознавание состоявшегося визита в чате | `MAIN_JOURNEY` | Сообщение пациента «I visited cardiologist today» | Детекция факта, `visit_confirmed = True`, отмена напоминаний | Разблокировка обновления здоровья | `test_full_system_continuous_lifecycle_journey` (Phase F) |
| **F15.6** | Сообщение без подтверждения визита | `BRANCH_TEST` | Пациент пишет «I might see doctor next week» | Напоминания НЕ отменяются, статус остаётся активным | Продолжение цикла напоминаний | `test_branch_01_referral_non_confirmatory_reply_preserves_reminders` |
| **F15.7** | Восстановление после сбоя генерации документа | `BRANCH_TEST` | Временный сбой при обращении к модели | Перевод задачи в `RETRYABLE_FAILURE` и повторный прогон | Успешная генерация без дублей | `test_branch_03_referral_document_retryable_failure_and_backoff_recovery` |
| **F15.8** | Валидация формата экспорта и защита путей | `BRANCH_TEST` | Некорректный формат (`format=exe`) или неверный ID | Возврат HTTP 400 / 404, пресечение path traversal | Защита данных Сейфа | `test_branch_08_export_format_validation_and_security` |

---

### Домен D16: Повторное обследование (Neo Consilium & Activation)
| ID | Функция / Метод | Статус | Триггер / Входные данные | Ограничения и инварианты | Сохранение и Downstream | Тестовая привязка |
|---|---|---|---|---|---|---|
| **F16.1** | `NeoCycleService.start_neo_reassessment` | `MAIN_JOURNEY` | Пользователь нажал «Обновить данные здоровья» | Синхронизация буферов, архивация Cycle 1, создание Cycle 2 | Переключение контекста на Cycle 2 | `test_full_system_continuous_lifecycle_journey` (Phase G) |
| **F16.2** | Повторный 10-модельный Neo Consilium | `MAIN_JOURNEY` | Завершение повторного ввода анкеты | 8 Neo Flash-Lite 3.5 $\to$ 1 Neo Flash 3.8 $\to$ 1 Neo Argon 4 | Состояние отчётов `prepared` | `test_full_system_continuous_lifecycle_journey` (Phase G) |
| **F16.3** | Атомарная активация Neo (`activate_prepared_neo`) | `MAIN_JOURNEY` | `POST /api/v1/client/cascade/activate-neo` | Атомарный перевод Cycle 2 в `active`, фиксация версий | Публикация в Vault, триггер Афины | `test_full_system_continuous_lifecycle_journey` (Phase G) |
| **F16.4** | Приветствие возвращающегося пациента | `MAIN_JOURNEY` | Первое открытие чата после активации Neo | Модель Flash 3.8 формирует контекстное приветствие Neo | Приветствие в чате Афины | `test_full_system_continuous_lifecycle_journey` (Phase G) |
| **F16.5** | Первая неделя после Neo (Week 1 of Cycle 2) | `MAIN_JOURNEY` | Виртуальное время `Neo T0 + 168h` | Первый еженедельный консилиум Нового Цикла | Подтверждение нормальной жизни Cycle 2 | `test_full_system_continuous_lifecycle_journey` (Phase H) |
| **F16.6** | Идемпотентная повторная активация Neo | `BRANCH_TEST` | Повторный вызов `POST /activate-neo` для активного цикла | Безопасный повтор без повторного сброса стратегии | `is_idempotent_replay = True` | `test_branch_07_neo_activation_idempotency_safe_replay` |

---

## 3. Сводная аудированная статистика покрытия

- **Всего уникальных функций в специфицированном реестре:** **65**
- **Проверено строгими assertions в основном непрерывном сценарии (MAIN_JOURNEY):** **45** (69.2%)
- **Проверено строгими assertions в 10 выделенных ветвящихся тестах (BRANCH_TEST):** **10** (15.4%)
- **Подтверждено в существующих специализированных тестах (EXISTING_VERIFIED):** **10** (15.4%)
  *(Аутентификация устройств, очередь Flutter VitalsSyncQueue, ручные замеры, повторный intake M1, лимиты вопросов Афины)*
- **Не удалось протестировать (NOT_TESTED):** **0** (0.0%)
- **Отсутствует в реализации (NOT_IMPLEMENTED):** **0** (0.0%)
- **Итоговое подтверждённое покрытие системы:** **100.0%** (65 из 65 функций подтверждены реальными тестами)
