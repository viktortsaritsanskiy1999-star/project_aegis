# PROJECT AEGIS — ПРИОРИТЕТНЫЙ GAP-АНАЛИЗ И ROADMAP РЕАЛИЗАЦИИ (ARGON)

**Дата аудита:** 2026-11-04  
**Модель аудитора:** Argon  
**Режим:** STRICT READ-ONLY (исходный код, тесты, БД и конфигурация не изменялись)  
**Текущий Git HEAD:** `2786b3b17be9a93f8bdb03140c401edd45d912b3` (ветка `main`)  
**Эталонная спецификация:** `docs/architecture_review/AEGIS_MASTER_SPECIFICATION_v1.0_DRAFT.md`  
**Техническое задание аудита:** `docs/architecture_review/AEGIS_OPUS_READ_ONLY_AUDIT_TASK_RU.md`

---

## 1. Снимок состояния репозитория на HEAD `2786b3b17be9a93f8bdb03140c401edd45d912b3`

На текущем коммите `2786b3b17be9a93f8bdb03140c401edd45d912b3` в кодовой базе уже реализованы и протестированы (567 backend pytest + 422 Flutter test):
- **Stage A1 (`daef94e8`)**: серверный воркер клинической автоматизации `ClinicalAutomationService` / `ClinicalAutomationWorker` (`backend/app/services/clinical_automation_service.py`) с таблицей `clinical_automation_jobs`, защитой `BEGIN IMMEDIATE`, арендными блокировками (leases) и автоматической постановкой задачи `post_primary_init` после завершения Primary Consilium.
- **Stage A2.1 (`2786b3b1`)**: ответно-ориентированное сопровождение Athena (`ATHENA_ROUTINE_QUESTION_REPORT_TYPE` в `backend/app/services/athena_chat_service.py:49, 137-375, 537-598`) — правило не более одного неотвеченного планового вопроса и ограниченные детерминированные напоминания (`max_reminders = 2`, 0 вызовов Gemini).
- **Stage A2.2 (`2786b3b1`)**: четырёхсекционный серверный дневной журнал `AthenaDailyJournalService` (`backend/app/services/athena_daily_journal_service.py`, `SERVER_ONLY`, тип `athena_daily_journal`) с пословным сохранением ответов пациента (Layer 1) и внутренней клинической оценкой через сценарий Gemini Flash (`AthenaDailyAssessmentModelOutput`, Layer 2, 0 вызовов Gemini в пустые дни), а также подача 7 дневных журналов в `WeeklyConsiliumService` (`backend/app/services/weekly_consilium_service.py:400-515, 1875-1895`).

---

## 2. Актуальный статус исторических дефектов Argon (`BUG-01` … `BUG-11`)

Все 11 исторических дефектов из более раннего аудита Argon (`independent_architecture_security_audit_argon.md`) **полностью исправлены** к коммиту `2786b3b17be9a93f8bdb03140c401edd45d912b3` и покрыты регрессионными тестами. Повторного исправления они не требуют:

| ID | Краткое описание исторического дефекта | Статус на `2786b3b` | Доказательство в коде и тестах |
|---|---|---|---|
| **BUG-01** | `WeeklyConsiliumService` не извлекал логи Athena при авто-резолве `sprint_id` | **ИСПРАВЛЕН** | `backend/app/services/weekly_consilium_service.py:820-845` (`_resolve_or_create_sprint_id`), `1875-1895` (`format_seven_day_journals_for_consilium` + fallback `get_seven_day_athena_logs`) |
| **BUG-02** | `ClientCascadeStatusResponse` во Flutter терял поля недельного спринта | **ИСПРАВЛЕН** | `lib/services/api/medical_ai_api_models.dart:227-313` (парсинг `currentWeeklySprintId`, `weeklySprintNumber`, `canStartNextSprint`, `nextSprintId`, `currentCalendarWeek`, `currentCalendarDay` и полей A2.1) |
| **BUG-03** | `AthenaTriggerService.checkAndTrigger` был заглушкой без вызова сервера | **ИСПРАВЛЕН** | `lib/services/athena_trigger_service.dart:58-277` (вызов `api.getCascadeStatus()` + `api.sendAthenaMessage` с проверкой `sessionEpoch` и `activeCycleId`) |
| **BUG-04** | Timeout recovery консилиума во Flutter не сверял `activeCycleId` | **ИСПРАВЛЕН** | `lib/screens/aegis_consilium_screen.dart:145-190`, `lib/screens/atlas_screen.dart:118-175`, тесты `test/weekly_consilium_flutter_test.dart` |
| **BUG-05** | `LabRevisionConsiliumService` переиспользовал request DB-сессию в фоновом heartbeat и не брал общий координационный лок | **ИСПРАВЛЕН** | `backend/app/services/lab_revision_consilium_service.py` (изолированная `hb_session`, общий `lock_consilium_coord_{cycle_id}`), тест `backend/tests/test_lab_revision_lease_safety.py` |
| **BUG-06** | Обход 168-часового гейта в `run_client_weekly_consilium` при наличии `active` строки спринта | **ИСПРАВЛЕН** | `backend/app/routers/client_cascade.py:775-795` (строгая проверка `if not lifecycle.can_start_next_sprint or clean_sprint_id != lifecycle.next_sprint_id: raise HTTPException(409)`) |
| **BUG-07** | Наивный `datetime.now()` вместо `self._now_utc()` в `WeeklyConsiliumService` | **ИСПРАВЛЕН** | `backend/app/services/weekly_consilium_service.py:315-395` (`self._now_utc()` и расчёт окон от якоря `T0`) |
| **BUG-08** | Два раздельных `commit()` в `AthenaChatService.handle_turn` и ` IntegrityError` при гонке | **ИСПРАВЛЕН** | `backend/app/services/athena_chat_service.py:456-667` (`BEGIN IMMEDIATE`, единый атомарный `self.db.commit()`, retry-цикл `max_persist_retries = 8`), тест `backend/tests/test_athena_atomic_persistence_and_concurrency.py` |
| **BUG-09** | `VaultService._clearSensitiveMemoryData()` не очищал in-memory кэши Weekly/Labs при `resetVault()` | **ИСПРАВЛЕН** | `lib/services/vault_service.dart:459-491` (полная очистка кэшей Primary, Weekly, `_cachedLabLedgerState`, `_anamnesisSyncStatus`) |
| **BUG-10** | Несовпадение ключа `text` vs `text_range` в `LabReferenceRange.fromJson` | **ИСПРАВЛЕН** | `lib/services/api/medical_ai_api_models.dart` (поддержка обоих ключей `text` и `text_range`) |
| **BUG-11** | Ручной ввод АД/пульса/SpO2 на `SmartWatchImportScreen` игнорировался | **ИСПРАВЛЕН** | `lib/screens/smart_watch_import_screen.dart:113-160` (`saveMarkersAndProceed()` валидирует и отправляет данные в `VitalsSyncQueue`) |

---

## 3. Доказательный GAP-анализ по 6 ключевым направлениям

Ниже приведена сверка требований `AEGIS_MASTER_SPECIFICATION_v1.0_DRAFT.md` с фактической реализацией на коммите `2786b3b17be9a93f8bdb03140c401edd45d912b3`.

### 3.1. Направление 1: Athena (собственный 7-дневный план, прогресс целей, до 3 вопросов/день, T0 + 30 сек)

| Требование Master Specification (Разделы 3, 4, 7.2) | Статус | Фактическое состояние в коде (файлы и строки) | Описание расхождения (GAP) |
|---|---|---|---|
| **Собственный сохраняемый 7-дневный план Athena до первого вопроса** (`goal_id`, версия стратегии Pro, основание, приоритет, известные данные, что выяснить, критерий достаточности, статус `active/completed/deferred/unresolved`, прогресс) | **ОТСУТСТВУЕТ** | `backend/app/services/clinical_automation_service.py:339-370, 960-1035`; `backend/app/services/athena_chat_service.py:1070-1122` | После Primary Consilium сохраняется только стратегия Pro (`pro_general_shelf` / `athena_active_strategy`). Воркер `post_primary_init` сразу вызывает `handle_turn(event_type="initial_greeting")`. Отдельной серверной записи собственного недельного плана Athena (`athena_weekly_plan`), создаваемой **до** первого вопроса, нет. |
| **Точка `T0` (приветствие) и отложенный на 30 секунд первый плановый вопрос** (переживающий перезапуск приложения) | **ЧАСТИЧНО** | `backend/app/services/athena_chat_service.py:562-590, 606-617, 1226-1233`; `backend/app/routers/client_cascade.py:629-639` | Сейчас `initial_greeting` фиксирует якорь `T0` (`athena_schedule_anchor`), но объединяет приветствие и первый клинический вопрос в **одно сообщение** сразу при выполнении `post_primary_init`. Нет разделения на: (1) приветствие в `T0` и (2) отдельный первый вопрос из плана с `available_at_utc = T0 + 30s`. |
| **До 3 плановых вопросов/уточнений в сутки и оценка достаточности ответа** (`active / completed / deferred / unresolved`) | **ЧАСТИЧНО** | `backend/app/services/athena_chat_service.py:537-598, 1200-1260`; `backend/app/services/consilium_prompts.py:711-712` | В A2.1 реализована блокировка нового вопроса при неотвеченном предыдущем и напоминания (`max_reminders = 2`). Но при ответе пациента (`routine_answer` / `user_message`) вопрос безусловно помечается `answered` (`line 548`), а промпт (`athena_chat_service.py:1204, 1257`, `consilium_prompts.py:711-712`) требует: *«Ask ONLY 1 targeted question per day... BE SURE TO SAY GOODBYE UNTIL TOMORROW»*. Нет счётчика `questions_asked_today <= 3`, нет оценки полноты ответа и нет трекинга уточняющего вопроса (строка 562 создаёт `unanswered` запись только для `initial_greeting` и `daily_pulse`, но не для уточняющего вопроса внутри `routine_answer`). |
| **Согласованность клиентского триггера Flutter с серверной сеткой `T0 + k*24h`** | **ЧАСТИЧНО** | `lib/services/athena_trigger_service.dart:131-140, 298` | Сервер отсчитывает окна строго как `[T0 + k*24h, T0 + (k+1)*24h)`. Однако клиентский `AthenaTriggerService.checkAndTrigger` блокирует вызов локальным условием `hoursDiff < 20 \|\| !isNewDay` (`line 138`), а `triggerVitalsSpike` (`line 298`) перезаписывает `updateAthenaLastPromptTime(now)`, из-за чего скачок пульса сбрасывает 20-часовой таймер планового вопроса на клиенте. |
| **Закрытый операционный журнал Athena для разработчиков** (Раздел 7.2: цели, прогресс, препятствия, счётчик вопросов; `SERVER_ONLY`, не передаётся в Weekly) | **ОТСУТСТВУЕТ** | `backend/app/services/athena_chat_service.py`; `backend/app/services/athena_daily_journal_service.py` | Существуют `athena_clinical_log` и `athena_daily_journal`, но оба подаются в Weekly Consilium. Отдельного технического журнала исполнения плана Athena для разработчиков (исключённого из контекста Weekly Consilium) нет. |

---

### 3.2. Направление 2: Wellness (объективные данные, отчёт после пробуждения, `NO_DATA` без Gemini, триггер Daily Journal)

| Требование Master Specification (Разделы 3.4, 6) | Статус | Фактическое состояние в коде (файлы и строки) | Описание расхождения (GAP) |
|---|---|---|---|
| **Независимый ежедневный сервис Wellness** (формирует сводку после завершения сна и пробуждения пользователя по часам/трекеру) | **ОТСУТСТВУЕТ** | `backend/app/schemas/consilium.py:39, 323`; `backend/app/models/health_measurement.py`; `backend/app/models/workout_session.py` | В схемах есть роль `WORKOUT_INTELLIGENCE`, а в БД — таблицы `health_measurements` и `workout_sessions`, но нет сервиса `WellnessDailyService`, который бы генерировал ежедневный отчёт `wellness` (`model_reports`) после сигнала завершения сна/пробуждения. |
| **Контракт сна / пробуждения и шагов** | **ЧАСТИЧНО** | `backend/app/models/health_measurement.py:16-36`; `backend/app/services/athena_daily_journal_service.py:528-533` | `AthenaDailyJournalService._gather_wellness_evidence` уже умеет читать `sleep_hours` и `steps` из `HealthMeasurement.raw_metadata`, но нет явного API-контракта/события завершения ночного сна (awakening event) от смарт-часов/клиента. |
| **Бесплатный детерминированный `NO_DATA` отчёт/событие без вызова Gemini** | **ОТСУТСТВУЕТ** | `backend/app/services/athena_daily_journal_service.py:440-480`; `backend/app/services/clinical_automation_service.py:516-539` | Сейчас при отсутствии отчёта Wellness в `model_reports` дневной журнал просто фиксирует `wellness_report_available = False`. Нет предшествующего шага, который при отсутствии часов/телеметрии детерминированно (0 вызовов Gemini) создаёт запись `NO_DATA` и испускает единый сигнал готовности дня. |
| **Правило 3 дней подряд без объективных измерений** (Раздел 6.3: мягкое предложение пользователю внутри лимита вопросов) | **ОТСУТСТВУЕТ** | `backend/app/services/athena_chat_service.py` | Athena не отслеживает серию из 3 последовательных дней со статусом `NO_DATA` и не включает мягкую рекомендацию об измерениях в свой дневной план вопросов. |

---

### 3.3. Направление 3: Athena Daily Journal (связка `Wellness Ready / NO_DATA -> Daily Journal`, дословные ответы, события, периоды)

| Требование Master Specification (Раздел 7) | Статус | Фактическое состояние в коде (файлы и строки) | Описание расхождения (GAP) |
|---|---|---|---|
| **Четыре независимые секции журнала + внутренняя клиническая оценка Gemini Flash** (дословные ответы, данные Wellness без смешения со словами пациента, особые события, оценка Athena; `SERVER_ONLY`; 0 вызовов Gemini в пустые дни) | **РЕАЛИЗОВАНО** | `backend/app/services/athena_daily_journal_service.py:1-1513`; `backend/tests/test_server_automation_stage_a2_2.py`; `backend/tests/test_athena_daily_gemini_assessment.py` | Реализовано в Stage A2.2. Слой 1 (`patient_reported_evidence`, `wellness_evidence`, `exceptional_events`) сохраняется детерминированно и неизменно до вызова Gemini Flash (Слой 2 `athena_clinical_assessment`). |
| **Событийная цепочка `Wellness Ready / NO_DATA -> Athena Daily Journal`** | **ЧАСТИЧНО** | `backend/app/services/clinical_automation_service.py:516-539, 1037-1125` | Сейчас `DAILY_JOURNAL_CLOSURE` ставится в очередь воркером напрямую по истечении 24-часового окна `get_completed_daily_windows(cycle_id, now=now)` (`clinical_automation_service.py:519`), не дожидаясь сигнала завершения Wellness (`WELLNESS_READY` или `NO_DATA`). |
| **Использование исторической стратегии Pro на дату конкретного дня при ретроспективной сборке** (Раздел 7.3) | **ЧАСТИЧНО** | `backend/app/services/athena_daily_journal_service.py:956`; `backend/app/services/athena_chat_service.py:1070` | В `generate_daily_gemini_assessment` (`line 956`) метод `athena_chat._resolve_strategy_context(cycle_id)` вызывается **без** аргумента `as_of=window_end` (хотя сам `_resolve_strategy_context` уже принимает `as_of: Optional[datetime] = None` на строке 1070!). Кроме того, выборка `athena_active_strategy` (`athena_chat_service.py:1080-1089`) должна фильтроваться по `created_at <= as_of`, чтобы при восстановлении после многодневного простоя день Недели 1 не оценивался по стратегии Недели 2. |
| **Включение генерации Daily Journal в production lifespan воркера** | **ЧАСТИЧНО** | `backend/app/main.py:48`; `backend/app/services/clinical_automation_service.py:1173` | В `ClinicalAutomationWorker.__init__` параметр `enable_daily_journal` по умолчанию равен `False` (`line 1173`), а `backend/app/main.py:48` создаёт `ClinicalAutomationWorker(session_factory=worker_session_factory)` без `enable_daily_journal=True`. |

---

### 3.4. Направление 4: Weekly Consilium (автозапуск `T0 + 168*n` ч независимо от Wellness, публикация Flash/Pro, сообщение Athena)

| Требование Master Specification (Раздел 8) | Статус | Фактическое состояние в коде (файлы и строки) | Описание расхождения (GAP) |
|---|---|---|---|
| **Каскад 8 системных `Flash-Lite` + `Pro`, версионирование отчётов без перезаписи, передача 7 дневных журналов Athena** | **РЕАЛИЗОВАНО** | `backend/app/services/weekly_consilium_service.py:400-515, 1300-1980` | Реализовано. Отчёты сохраняются как новые версии в `model_reports`, стратегия Athena обновляется, 7 дневных журналов подаются с сохранением разделения фактов и оценки. |
| **Автоматический серверный запуск каждые `T0 + 168*n` часов без кнопки пользователя и независимо от Wellness** | **ОТСУТСТВУЕТ** | `backend/app/services/clinical_automation_service.py:53-65, 470-550`; `backend/app/routers/client_cascade.py:692` | В `AutomationJobType` (`clinical_automation_service.py:53-65`) есть только `POST_PRIMARY_INIT` и `DAILY_JOURNAL_CLOSURE`. Weekly Consilium сейчас запускается только по HTTP-запросу `POST /api/v1/client/cascade/run-weekly`. Необходимо добавить задачу `WEEKLY_CONSILIUM_RUN` в `ClinicalAutomationService.discover_and_enqueue_jobs`. |
| **Отдельное итоговое сообщение Athena в чате после публикации Weekly Consilium** (строго по опубликованному пользовательскому отчёту `Pro` без внутренних гипотез + приглашение открыть медицинский сейф — силуэт человека) | **ОТСУТСТВУЕТ** | `backend/app/services/weekly_consilium_service.py:1940-1995`; `backend/app/services/athena_chat_service.py` | После завершения `run_weekly_consilium` в чат пользователя ничего не отправляется. Нет отдельного идемпотентного сценария (`event_type="weekly_summary_announcement"`), формирующего сообщение строго по опубликованному `weekly_overall_report` и направляющего пользователя в медицинский сейф (силуэт человека), не смешивая это сообщение с вопросами новой недели. |
| **Генерация нового 7-дневного плана Athena на неделю `N+1` после Weekly Consilium** | **ОТСУТСТВУЕТ** | `backend/app/services/weekly_consilium_service.py` | После публикации новой `athena_active_strategy` не формируется новый структурированный 7-дневный план целей Athena на следующую неделю. |

---

### 3.5. Направление 5: Пульс, тренировка/восстановление и экстренный режим Mode 2 (пороги Pro, интервалы, следующий день)

| Требование Master Specification (Раздел 5) | Статус | Фактическое состояние в коде (файлы и строки) | Описание расхождения (GAP) |
|---|---|---|---|
| **Приоритет персональных порогов `Pro` (`min_pessimum_bpm`, `max_pessimum_bpm`, `max_spike_delta_bpm`) над жёсткими константами** | **ЧАСТИЧНО** | `lib/services/vitals_monitor_service.dart:175, 199, 270-310`; `backend/app/services/athena_chat_service.py:884` | 1) В `vitals_monitor_service.dart:175` мгновенная проверка срабатывает только при `newPulse >= 140`. Если `Pro` задал `maxPessimumBpm = 135`, то одиночный замер `137 bpm` попадает в `_analyzeForSpike` (`line 270`), который требует `_pulseBuffer.length >= 3` (`line 285`) и **игнорирует** первые 1–2 замера! <br>2) В фазе `postWorkoutRecovery` (`line 199`) зашит порог `newPulse >= 140` вместо `doctorThresholds.maxPessimumBpm`. <br>3) На бэкенде в `_evaluate_pre_emergency` (`athena_chat_service.py:884`) зашито `va.current_pulse >= 150 or va.current_pulse <= 40` без сверки с персональными порогами цикла из БД (`vitals_thresholds`). |
| **Режим тренировки и интервалы восстановления (5 / 15 / 30 минут)** | **РЕАЛИЗОВАНО** | `lib/services/vitals_monitor_service.dart:54-60, 185-268` | Таймеры `_timer5m`, `_timer15m`, `_timer30m` и подавление ложных тревог во время `activeWorkout` реализованы корректно. |
| **Экстренный сценарий Mode 2: учёт отрицаний в ключевых словах и локализация номера экстренной помощи** | **ЧАСТИЧНО** | `backend/app/services/athena_chat_service.py:59-76, 868-908, 1324` | `ACUTE_EMERGENCY_KEYWORDS` (`lines 59-76`) ищет подстроки без учёта отрицаний (например, фраза *«боли в груди нет, потери сознания не было»* вызовет ложный `is_acute_emergency`), а fallback-сообщение (`line 1324`) содержит только `(112)`. |
| **Обязательное деликатное уточнение на следующий день после эпизода Mode 2** (приоритет выше плановой темы; запрет додумывать госпитализацию/скорую) | **ОТСУТСТВУЕТ** | `backend/app/services/athena_chat_service.py:1215-1235` | На следующий день (`daily_pulse`) Athena не проверяет наличие вчерашнего события `vitals_alert` / `wearable_event` / `emergency_trigger` и не ставит деликатный вопрос о самочувствии после вчерашнего эпизода выше обычного недельного плана. |

---

### 3.6. Направление 6: Lab Revision Consilium (триггеры, координационные локи, API, взаимодействие с Weekly)

| Требование Master Specification (Раздел 9) | Статус | Фактическое состояние в коде (файлы и строки) | Описание расхождения (GAP) |
|---|---|---|---|
| **Координационная блокировка с Primary/Weekly и 168-часовой кулдаун** | **РЕАЛИЗОВАНО** | `backend/app/services/lab_revision_consilium_service.py:85, 1270-1450`; `backend/tests/test_lab_revision_lease_safety.py` | Используется общий `lock_consilium_coord_{cycle_id}` с изолированной сессией heartbeat и проверкой `LAB_REVISION_COOLDOWN_HOURS = 168`. |
| **Детекция новых анализов/ЭКГ (`has_unreviewed_lab_changes`)** | **РЕАЛИЗОВАНО** | `backend/app/routers/client_cascade.py:293-305` | `get_client_cascade_status` вычисляет `has_unreviewed_lab_changes` и `lab_revision_Gate` при появлении новых записей `LabResult` или `ECG Intake` после последнего консилиума. |
| **Клиентский/автоматический триггер запуска и пересборка плана Athena посреди недели** | **ЧАСТИЧНО** | `backend/app/routers/cascade.py` (`POST /api/v1/cascade/run-lab-revision`); `backend/app/routers/client_cascade.py` | Эндпоинт `run-lab-revision` сейчас доступен только в админском роутере `cascade.py` (`require_service_admin_session`). Нет ни клиентского эндпоинта в `client_cascade.py`, ни автоматической задачи в `ClinicalAutomationService`, ни пересборки оставшихся целей недельного плана Athena при обновлении `athena_active_strategy` посреди недели. |

---

## 4. Приоритетный ROADMAP реализации для Gemini Flash

Ниже приведён пошаговый план реализации для модели **Gemini Flash**, разбитый на **6 компактных, независимых этапов (Stage B1 … Stage B6)**. Каждый этап имеет чёткие границы, не ломает существующие 567 backend и 422 Flutter теста и может коммититься отдельно.

---

### Stage B1: Автоматический Weekly Consilium (`T0 + 168*n` ч), пост-недельное сообщение Athena и минорные доводки A2.2
**Приоритет:** КРИТИЧЕСКИЙ (закрывает главную петлю серверной автоматизации недели).  
**Зависимости:** Базируется на готовых Stage A1 + A2.2.

1. **Что изменить (конкретные файлы):**
   - `backend/app/main.py` (строка 48): передать `enable_daily_journal=True` и `enable_weekly_consilium=True` при создании `ClinicalAutomationWorker`.
   - `backend/app/services/athena_daily_journal_service.py` (строка 956) и `backend/app/services/athena_chat_service.py` (строки 1070–1095): передавать `as_of=window_end` в `_resolve_strategy_context(cycle_id, as_of=window_end)` и фильтровать `ModelReport.created_at <= as_of` при чтении `athena_active_strategy`, чтобы ретроспективные дневные журналы оценивались строго по исторической стратегии той недели.
   - `backend/app/services/clinical_automation_service.py`:
     - Добавить `WEEKLY_CONSILIUM_RUN = "weekly_consilium_run"` и `WEEKLY_SUMMARY_POST = "weekly_summary_post"` в `AutomationJobType`.
     - В `discover_and_enqueue_jobs` проверять через `WeeklyConsiliumService.get_sprint_lifecycle_state(cycle_id)`, наступил ли рубеж `T0 + 168*n` часов (`can_start_next_sprint == True`), и ставить идемпотентную задачу `weekly_consilium_run:{cycle_id}:week_{n}` **независимо от наличия отчётов Wellness и без участия кнопки клиента**.
     - Перед запуском `WeeklyConsiliumService.run_weekly_consilium` убеждаться, что для завершённых 7 дней (`days 1..7` данного спринта) вызвана сборка `AthenaDailyJournalService.build_and_persist_daily_journal` (если какой-то день ещё не был закрыт).
   - `backend/app/services/athena_chat_service.py` и `backend/app/services/consilium_prompts.py`:
     - Добавить внутренний серверный сценарий публикации итога недели (`generate_and_persist_weekly_announcement(cycle_id, sprint_id)`), который:
       1. Читает **только** опубликованный пользовательский отчёт `weekly_overall_report` модели `Pro` за данный `sprint_id` (строго без доступа к `pro_training_shelf` и внутренним черновикам).
       2. Формирует одно отдельное сообщение в чате Athena с кратким обзором итогов недели и приглашением перейти в медицинский сейф (нажать на силуэт человека на главном экране) для изучения обновлённых отчётов по системам организма.
       3. Не задаёт в этом сообщении плановый вопрос новой недели (идемпотентный ключ `weekly_announcement:{cycle_id}:{sprint_id}`).
2. **Требования безопасности и инварианты:**
   - Ни при каких условиях не передавать `pro_training_shelf` в промпт генерации пользовательского итогового сообщения Athena.
   - Использовать существующий `lock_consilium_coord_{cycle_id}` в `WeeklyConsiliumService`, чтобы автозапуск воркера и ручной запрос не могли выполниться параллельно.
3. **Что НЕ менять:**
   - Не менять логику 8 системных `Flash-Lite` агентов и `Pro` внутри `WeeklyConsiliumService`.
   - Не менять формат `AthenaDailyJournalRecord`.
4. **Критерии приёмки и тесты (`backend/tests/test_server_automation_stage_b1.py`):**
   - Тест автоматического обнаружения и выполнения `WEEKLY_CONSILIUM_RUN` при `now >= T0 + 168h` без вызова клиентского HTTP-эндпоинта и при полном отсутствии данных Wellness (`NO_DATA`).
   - Тест идемпотентности: повторный тик воркера не запускает второй Weekly Consilium за ту же неделю.
   - Тест пост-недельного сообщения Athena: сообщение появляется в `athena_chat_messages`, содержит приглашение в медицинский сейф, не содержит утечек `pro_training_shelf` и не помечается как неотвеченный вопрос (`unanswered`).
   - Тест исторической стратегии `as_of=window_end` при ретроспективной сборке журнала Недели 1 после публикации стратегии Недели 2.

---

### Stage B2: Слой Wellness (`WellnessDailyService`), детерминированный `NO_DATA` и цепочка `Wellness -> Daily Journal`
**Приоритет:** ВЫСОКИЙ (обеспечивает событийную архитектуру Разделов 6 и 7).  
**Зависимости:** Stage A2.2, Stage B1.

1. **Что изменить (конкретные файлы):**
   - Создать `backend/app/services/wellness_daily_service.py`:
     - Определить структуру `WellnessDailyReportRecord` (`status: "READY" | "NO_DATA"`, `day_index`, `week_number`, `window_start_utc`, `window_end_utc`, `sleep_completed`, `objective_metrics_summary`, `gemini_wellness_summary`).
     - Если за суточное окно `[window_start, window_end)` нет объективных измерений часов/трекера (`HealthMeasurement`, `WorkoutSession`), формировать и сохранять в `model_reports` (`report_type="wellness_daily_status"`, `visibility_scope="SERVER_ONLY"`) детерминированную запись `status="NO_DATA"` **за 0 вызовов Gemini**.
     - Если объективные данные есть (после события завершения сна/пробуждения или по закрытию суточного окна), формировать версионированный отчёт `status="READY"` через сценарий Gemini Flash (`AegisAgentRole.WORKOUT_INTELLIGENCE` / `wellness`) **строго по объективной телеметрии** (без доступа к чату Athena и жалобам пациента).
   - `backend/app/routers/client_cascade.py` (или `vitals.py`):
     - При синхронизации данных часов с маркером завершённого сна (`phase="sleep_complete"` / `raw_metadata.sleep_completed == True`) вызывать `WellnessDailyService.evaluate_and_persist_daily_wellness`, что сразу инициирует закрытие соответствующего дня в `AthenaDailyJournalService`.
   - `backend/app/services/clinical_automation_service.py`:
     - Выстроить цепочку в воркере: сначала `WellnessDailyService.ensure_daily_wellness_status(cycle_id, day_index)` (возвращает `READY` или бесплатный `NO_DATA`), и сразу по его результату — запуск `AthenaDailyJournalService.build_and_persist_daily_journal(cycle_id, day_index)`.
2. **Требования безопасности и инварианты:**
   - Полная изоляция источников: `WellnessDailyService` никогда не читает `AthenaChatMessage` и `AthenaClinicalLog`.
   - При отсутствии объективных измерений — строго **0 вызовов Gemini API**.
3. **Что НЕ менять:**
   - Не изменять таблицы `health_measurements` и `workout_sessions` (использовать существующие колонки и `raw_metadata`).
4. **Критерии приёмки и тесты (`backend/tests/test_wellness_daily_service_stage_b2.py`):**
   - Тест дня без часов: `WellnessDailyService` создаёт `NO_DATA` за 0 вызовов Gemini и триггерит `AthenaDailyJournalService`.
   - Тест дня с телеметрией и завершённым сном: создаётся версионированный отчёт Wellness, который автоматически подхватывается секцией `wellness_evidence` в `AthenaDailyJournalRecord`.
   - Тест восстановления после простоя сервера: пропущенные дни последовательно получают `NO_DATA`/`READY` и закрывают `AthenaDailyJournalRecord`.

---

### Stage B3: Собственный 7-дневный план Athena (`AthenaWeeklyPlanService`) и отложенный на 30 секунд первый вопрос
**Приоритет:** ВЫСОКИЙ (выполняет требования Разделов 4.1 и 4.3).  
**Зависимости:** Stage B1.

1. **Что изменить (конкретные файлы):**
   - Создать `backend/app/services/athena_weekly_plan_service.py`:
     - Ввести Pydantic-схему `AthenaWeeklyPlanRecord` и `AthenaGoalItem` (`goal_id`, `source_strategy_report_id`, `clinical_rationale`, `priority`, `known_facts`, `target_clarification`, `sufficiency_criteria`, `status: Literal["active", "completed", "deferred", "unresolved"]`, `progress_notes`, `questions_asked_count`).
     - Сохранять план в `model_reports` (`report_type="athena_weekly_plan"`, `visibility_scope="SERVER_ONLY"`).
     - Вызывать построение плана:
       1. В `POST_PRIMARY_INIT` (`clinical_automation_service.py`) **до** первого вопроса Athena.
       2. Сразу после каждого `WeeklyConsiliumService.run_weekly_consilium` (для недели `N+1`).
   - `backend/app/services/athena_chat_service.py` и `backend/app/services/clinical_automation_service.py`:
     - Разделить `POST_PRIMARY_INIT` на два шага:
       1. В момент `T0`: сохранение `athena_schedule_anchor`, генерация `athena_weekly_plan` и публикация **только приветственного сообщения** `initial_greeting` (без клинического вопроса).
       2. Постановка задачи `FIRST_PLANNED_QUESTION` с `available_at = T0 + timedelta(seconds=30)`. Когда `now >= T0 + 30s`, воркер (или поллинг статуса клиента) идемпотентно публикует первый плановый вопрос из `athena_weekly_plan` и создаёт запись `athena_routine_question_state` со статусом `unanswered`.
2. **Требования безопасности и инварианты:**
   - Если приложение закрыто сразу после приветствия в `T0`, первый вопрос всё равно создаётся на сервере в `T0 + 30s` и отображается при следующем входе без дублей.
3. **Что НЕ менять:**
   - Не менять ручной запуск первого Primary Consilium (он остаётся строго по кнопке пользователя).
4. **Критерии приёмки и тесты (`backend/tests/test_athena_weekly_plan_stage_b3.py`):**
   - Тест создания `athena_weekly_plan` со списком `goal_id` до первого вопроса.
   - Тест разделения `T0` (приветствие) и `T0 + 30s` (первый вопрос по плану) + выживаемость при рестарте между `T0` и `T0 + 30s`.

---

### Stage B4: Диалоговый движок целей Athena (до 3 вопросов/день, прогресс целей, правило 3 дней без телеметрии, техжурнал) и синхронизация Flutter-триггера
**Приоритет:** СРЕДНИЙ/ВЫСОКИЙ (выполняет требования Разделов 4.2, 4.4, 6.3, 7.2).  
**Зависимости:** Stage B2, Stage B3.

1. **Что изменить (конкретные файлы):**
   - `backend/app/services/athena_chat_service.py` и `backend/app/services/consilium_prompts.py`:
     - Убрать из промптов жёсткую фразу *"Ask ONLY 1 targeted question per day... BE SURE TO SAY GOODBYE UNTIL TOMORROW"* (`consilium_prompts.py:711-712`, `athena_chat_service.py:1204, 1257`).
     - Ввести суточный лимит `MAX_PLANNED_QUESTIONS_PER_DAY = 3` в рамках текущего 24-часового окна `[T0 + k*24h, T0 + (k+1)*24h)`.
     - При получении ответа пациента (`routine_answer` / `user_message`) оценивать достаточность ответа по текущей `goal_id`:
       - если ответ полный — переводить цель в `completed`, и если лимит 3 вопросов за сегодня не исчерпан и уместно — переходить к следующей цели или завершать дневной контакт;
       - если ответ неполный и `questions_asked_today < 3` — задавать 1 короткий уточняющий вопрос и регистрировать его в `athena_routine_question_state` как `unanswered` (с привязкой к `goal_id`);
       - если `questions_asked_today == 3` и цель не закрыта — переводить цель в `deferred` на следующий день.
     - Проверять последние 3 дня `wellness_daily_status`: если 3 дня подряд `status == "NO_DATA"`, включать мягкое напоминание о способе измерения внутри лимита `<= 3` вопросов.
     - Записывать технический лог исполнения плана (`report_type="athena_developer_ops_log"`, `visibility_scope="SERVER_ONLY"`): `goal_id`, статус, препятствия, `questions_asked_today`, следующее действие. Этот отчёт **не** включается в `WeeklyConsiliumService`.
   - `lib/services/athena_trigger_service.dart`:
     - Убрать сброс `updateAthenaLastPromptTime` из `triggerVitalsSpike` (`line 298`), чтобы скачок пульса не блокировал плановые вопросы Athena.
     - Опираться на серверные флаги из `getCascadeStatus()` (`hasUnansweredRoutineQuestion`, `currentCalendarDay`, `routineQuestionDayIndex`) вместо жёсткого локального фильтра `hoursDiff < 20 || !isNewDay` (`line 138`).
2. **Критерии приёмки и тесты (`backend/tests/test_athena_goal_progression_stage_b4.py` + `test/athena_trigger_service_test.dart`):**
   - Тест короткого/уклончивого ответа: Athena задаёт уточняющий вопрос, счётчик `questions_asked_today` увеличивается до 2, а на 3-м вопросе цель переходит в `deferred`,4-й плановый вопрос в те же сутки блокируется.
   - Тест правила 3 дней `NO_DATA`: на 4-й день Athena мягко предлагает сдать базовые измерения в рамках лимита вопросов.
   - Тест изоляции `athena_developer_ops_log`: лог сохраняется для разработчиков, но не попадает в промпты `WeeklyConsiliumService`.

---

### Stage B5: Персональные пороги ЧСС `Pro`, завершение Mode 2 и обязательное уточнение на следующий день
**Приоритет:** СРЕДНИЙ/ВЫСОКИЙ (выполняет требования Раздела 5).  
**Зависимости:** Независим от B2–B4 (может выполняться параллельно или последовательно).

1. **Что изменить (конкретные файлы):**
   - `lib/services/vitals_monitor_service.dart`:
     - В `processNewPulse` (`line 175`) проверять `newPulse >= doctorThresholds.maxPessimumBpm || newPulse <= doctorThresholds.minPessimumBpm` **сразу на 1-м замере**, не дожидаясь накопления 3 точек в `_pulseBuffer` (буфер из 3 точек оставить для расчёта дельты скачка `maxSpikeDeltaBpm` в `_analyzeForSpike`).
     - В `postWorkoutRecovery` (`line 199`) заменить константу `140` на `doctorThresholds.maxPessimumBpm`.
   - `backend/app/services/athena_chat_service.py`:
     - В `_evaluate_pre_emergency` (`line 868-908`) загружать персональные пороги цикла (`vitals_thresholds` из `model_reports`) и использовать `min_pessimum_bpm` / `max_pessimum_bpm` вместо констант `150` / `40`.
     - Добавить фильтр отрицаний перед срабатыванием `ACUTE_EMERGENCY_KEYWORDS` (чтобы фразы *«боли в груди нет»*, *«потери сознания не было»* не вызывали ложный экстренный блок).
     - В `_build_turn_prompt_and_record` для `daily_pulse` проверять, было ли в предыдущем 24-часовом окне (`day_index - 1`) событие `vitals_alert` / `wearable_event` / `emergency_trigger`. Если было — первым вопросом дня ставить деликатное уточнение вчерашнего эпизода и самочувствия пациента (с явным запретом в промпте предполагать вызов скорой/госпитализацию, если пациент сам об этом не сообщил).
2. **Критерии приёмки и тесты (`backend/tests/test_mode2_and_thresholds_stage_b5.py` + `test/vitals_monitor_service_test.dart`):**
   - Flutter-тест: при `maxPessimumBpm = 135` первый же замер `137 bpm` в покое вызывает `onAnomalyDetected`.
   - Backend-тест: отрицание симптома (*«боли в груди нет»*) не переводит чат в статический emergency-блок.
   - Backend-тест: после вчерашнего `vitals_alert` первый вопрос следующего дня посвящён вчерашнему эпизоду.

---

### Stage B6: Интеграция триггера и координации `LabRevisionConsiliumService`
**Приоритет:** СРЕДНИЙ (выполняет требования Раздела 9).  
**Зависимости:** Stage B1, Stage B3.

1. **Что изменить (конкретные файлы):**
   - `backend/app/routers/client_cascade.py` и `backend/app/services/clinical_automation_service.py`:
     - Добавить клиентский эндпоинт `POST /api/v1/client/cascade/run-lab-revision` (или задачу `LAB_REVISION_RUN` в `ClinicalAutomationService` при `has_unreviewed_lab_changes == True` и открытом `lab_revision_gate`), использующий существующий `LabRevisionConsiliumService` и общий координационный лок `lock_consilium_coord_{cycle_id}`.
     - После успешного завершения `LabRevisionConsiliumService` вызывать `AthenaWeeklyPlanService.reconcile_plan_after_lab_revision(cycle_id)`, чтобы обновить незавершённые цели текущей недели в соответствии с обновлённой стратегией `Pro`, не сбрасывая якорь времени `T0` и график `Weekly Consilium`.
2. **Критерии приёмки и тесты (`backend/tests/test_lab_revision_automation_stage_b6.py`):**
   - Тест взаимного исключения: если `WeeklyConsiliumService` уже держит `lock_consilium_coord_{cycle_id}`, `LabRevisionConsiliumService` откладывается без ошибки повреждения данных.
   - Тест обновления недельного плана Athena посреди недели после `LabRevisionConsilium` с сохранением прежнего `T0`.

---

## 5. Технические и продуктовые решения, требующие согласования владельца (`[ТРЕБУЕТ РЕШЕНИЯ ВЛАДЕЛЬЦА]`)

Эти 5 вопросов выделены в `AEGIS_MASTER_SPECIFICATION_v1.0_DRAFT.md` (Раздел 12) как открытые (`[ОТКРЫТО]`). Архитектура Stage B1–B6 спроектирована так, чтобы поддерживать любой выбранный вариант через конфигурацию, однако для финальной фиксации контракта требуется решение владельца продукта:

| № | Открытый вопрос (`[ТРЕБУЕТ РЕШЕНИЯ ВЛАДЕЛЬЦА]`) | Вариант А (Рекомендуемый по умолчанию для Stage B1–B6) | Вариант Б (Альтернативный) | Влияние на реализацию |
|---|---|---|---|---|
| **1** | **Критерий закрытия дня при отсутствии часов/сна и политика опоздавших данных сна** (Разделы 6.2, 7.1, 12 п.1) | Если сигнал завершения сна от часов пришёл внутри суток — закрывать день сразу после пробуждения. Если часов нет — закрывать день по границе серверного окна `T0 + (k+1)*24h` со статусом `NO_DATA`. Опоздавшие данные сна (пришедшие после закрытия журнала) создавать как `wellness_daily_status` v2 и помечать в следующем дне без переписывания уже закрытого журнала. | Ждать фиксированного локального часа (например, `12:00` следующего дня) перед созданием `NO_DATA`, а при приходе опоздавшего сна пересоздавать `athena_daily_journal` v2 за прошлый день. | Вариант А проще, строго детерминирован по `T0 + k*24h` и гарантирует неизменность закрытых журналов. |
| **2** | **Критерий закрытия экстренного режима Mode 2 и защита от повторных срабатываний** (Разделы 5.4, 12 п.2) | Закрывать активный эпизод Mode 2 после ответа пользователя в экстренном диалоге **или** автоматически через 60 минут тишины при нормализации ЧСС (с 30-минутным cooldown на повторный алерт по тому же порогу). | Закрывать Mode 2 только при явном нажатии кнопки «Мне лучше / Эпизод завершён» на клиенте или при закрытии суточного окна. | Вариант А предотвращает бесконечное застревание чата в Mode 2, если пользователь отложил телефон после нормализации пульса. |
| **3** | **Связка публикации Weekly Consilium, итогового сообщения Athena и генерации плана новой недели** (Разделы 8.4, 12 п.3) | Выполнять как цепочку из 2 независимых идемпотентных шагов воркера: (Шаг 1) публикация отчётов `Flash + Pro` в сейф; (Шаг 2) отправка итогового сообщения Athena в чат + генерация `athena_weekly_plan` на неделю `N+1`. Если Шаг 2 временно упал по сети Gemini, отчёты в сейфе уже доступны, а воркер повторит только Шаг 2. | Выполнять публикацию отчётов консилиума, генерацию итогового сообщения Athena и генерацию плана новой недели внутри одной монолитной транзакции. | Вариант А намного надёжнее: сбой генерации короткого чат-сообщения никогда не откатит уже готовый 9-модельный Weekly Consilium. |
| **4** | **Политика запуска `Lab Revision Consilium` при загрузке анализов по частям** (Разделы 9, 12 п.4) | Запускать после явного подтверждения пользователем «Анализы загружены (Запустить пересмотр)» на клиенте **или** автоматически воркером через 15 минут после последней загрузки файла (debounce), если прошло `>= 168ч` с прошлого пересмотра и до планового Weekly осталось `> 24ч` (иначе включить анализы уже в плановый Weekly). | Запускать немедленно после загрузки первого же лабораторного показателя. | Вариант А защищает от преждевременного запуска 10-модельного каскада, когда пациент загружает 3 бланка анализов подряд с интервалом в 2 минуты. |
| **5** | **Объём смежного анамнеза для 8 узких `Flash-Lite` моделей** (Разделы 3.1, 12 п.5) | Передавать каждой узкой `Flash-Lite` модели: (1) полный профиль своей системы + (2) компактную кросс-системную сводку ключевых диагнозов, медикаментов и красных флагов остальных систем (без «сырых» простыней чужих разделов). | Передавать всем 8 `Flash-Lite` моделям абсолютно идентичный полный дамп всех 8 разделов анкеты. | Вариант А уже частично используется в `consilium_prompts.py` и снижает информационный шум для `Flash-Lite` при сохранении клинической связности. |
