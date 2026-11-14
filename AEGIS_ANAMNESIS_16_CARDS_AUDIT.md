# PROJECT AEGIS — INDEPENDENT ANAMNESIS AUDIT
## Полный аудит медицинского анамнеза (карточки 1–16 + Age Gate)

- **Проект:** `E:\aegis_health`
- **Ветка:** `feature/anamnesis-stage5`
- **Контрольный коммит:** `d0c21a44b41932c9e45e1f6382bacf371927a910`
- **Исполнитель:** Gemini Argon (High)
- **Режим:** Независимый read-only аудит без модификации исходного кода и тестов

---

## A. Executive Summary

### 1. Общая оценка состояния карточек 1–16
Реализация 16 карточек медицинского анамнеза (Stages 1–5) образует единый сквозной конвейер:
`AnamnesisScreen (Flutter UI) -> VaultService (in-memory + SharedPreferences + patient scope) -> AnamnesisQuestionnaireAnswers (Dart API DTO) -> AnamnesisQuestionnaireAnswers (FastAPI Pydantic v2) -> build_anamnesis_m1_context_from_answers (M1 Context)`.

Архитектурный фундамент Stages 2–5 (карточки 6–16), двухуровневая «матрёшка» в карточках 15–16, очистка зависимых ключей при смене ветки (`removeAnamnesisKeys`), обратная совместимость со старыми сводными полями (`chronic_diseases`, `recent_illness`, `critical_infections`, `heredity_risks`, `medications`) и строгая серверная кросс-полевая валидация Pydantic реализованы качественно и подтверждены **41 интеграционным/виджет-тестом Flutter** и **71 модульным/схемным тестом backend (pytest)**.

Вместе с тем независимый построчный аудит выявил **2 проблемы высокого приоритета (High)**, **8 проблем среднего приоритета (Medium)** и **4 замечания низкого приоритета (Low)**, которые не обнаруживаются текущими поэтапными тестами (так как тесты переходят на нужную страницу через `jumpToPage(...)` или движутся только вперёд без нажатия кнопки «Назад» и без повторного открытия карточек 3–4).

### 2. Готовность к передаче данных в модуль M1
- **Транспортная и контрактная готовность (UI → Vault → DTO → Backend Pydantic):** условная готовность после устранения 2 дефектов High и 3 дефектов Medium, способных вызвать потерю введённых полей или ошибку `422 ValidationError` при редактировании ответов в карточках 1–4 и 8.
- **Полнота английской локализации UI:** карточки **6–16**, **Age Gate (шаг 0)** и **Final Screen (шаг 17)** полностью переведены на английский язык, однако карточки **1–5 (Stage 1)** и подсказка кнопки `Back` (`tooltip: 'Назад'`) содержат русскоязычные вопросы, варианты ответов и сообщения об ошибках.
- **Полнота извлечения новых полей Stages 2–5 в `build_anamnesis_m1_context_from_answers`:** базовые сводные поля и структурированные поля Stage 1 уже извлекаются в `system_folders` и `core_data_log`, но детализированные поля Stages 2–5 пока валидируются на уровне схемы и ожидают включения в `consilium_prompts.py` (зафиксировано в разделе **F. Отложенные вопросы M1**).

### 3. Сводная статистика найденных проблем

| Приоритет | Кол-во | Краткий перечень |
| :--- | :---: | :--- |
| **Critical** | **0** | Критических уязвимостей безопасности или потери изоляции между авторизованными пациентами не обнаружено. |
| **High** | **2** | **H-1:** `_nextPage()` при каждом переходе очищает контроллеры возраста, роста и веса (`_customInputController`, `hController`, `wController`), стирая ввод карточек 1 и 2 при возврате кнопкой «Назад».<br>**H-2:** Карточки 3 (Nicotine) и 4 (Alcohol): несоответствие кодов статуса при сохранении/восстановлении (`'Regular'` → `'Active'`, `'Former'` → `'Quit'`, `'1-2/week'` → `'Frequently'`), невозможность вернуться с подшага 1 на подшаг 0 внутри карточки и сохранение конфликтующих ключей (`nicotine_current_frequency` + `nicotine_quit_when`) при смене статуса с `Regular` на `Former` (вызывает `422 ValidationError` на сервере). |
| **Medium** | **8** | **M-1:** Карточки 1–5 частично или полностью на русском языке (вопросы, опции, ошибки валидации, `tooltip: 'Назад'`).<br>**M-2:** Карточка 8 (Drinks): снятие галочек с `'Black / Green tea'` и `'Energy drinks'` не очищает `_drinksCaffeineFrequency`, отправляя фантомную частоту кофеина.<br>**M-3:** Карточка 2 (Dimensions): вопрос об изменении веса за 3–6 мес. не проверяется на обязательность при нажатии `Next`; флаги подтверждения нестандартного возраста/роста/веса не сбрасываются при изменении текста.<br>**M-4:** Карточка 5 (SpecFactors): подвопрос `"Когда это было в последний раз?"` (`_substanceLastUse`) не проверяется на обязательность при выбранном веществе; опция `"Употреблял(а) ранее, сейчас нет"` смешивается в мультивыборе с конкретными веществами.<br>**M-5:** Карточка 7 (Diet): чекбокс `"I avoid specific foods or ingredients"` позволяет свернуть карточку и нажать `Next` с пустым полем ввода; выбор частоты для `diet_occasional_foods` мгновенно сворачивает подкарточку без кнопки `Save and collapse`.<br>**M-6:** Карточки 7, 9, 12, 13: повторное нажатие на заголовок уже заполненной и свёрнутой категории удаляет категорию и стирает введённые данные (в отличие от карточек 15–16 с разделением чекбокса и заголовка); сводные поля `chronic_diseases` (Card 12) и `recent_illness` (Card 13) не ограничены `_clampSummary`.<br>**M-7:** `AnamnesisScreen.didChangeDependencies()`: не восстанавливает черновик гостя (`activePatientId == null`) при повторном открытии экрана и не перечитывает состояние при асинхронной подгрузке профиля второго пациента, если `AnamnesisScreen` остаётся смонтированным.<br>**M-8:** `VaultService.buildAnamnesisAnswers()` и `_applySingleAnamnesisDerivation()`: неполный fallback для ключей `user_text_spec_factors`, `user_text_diet_habits`, `user_text_gi_signals`; локальные деривации в `_systemFolders` не очищаются при смене ответа с положительного на отрицательный. |
| **Low** | **4** | **L-1:** Прогресс-бар на финальном экране (`_stepFinal`, индекс 17) получает `value = 17 / 16 = 1.0625` из-за `_totalPages = 17` при 18 дочерних виджетах `PageView`.<br>**L-2:** Разнобой текста кнопок перехода (`"Next"`, `"Далее ➡️"`, `"Continue ➡️"`, `"Next ➡️"`, `"Finish 🚀"`) и ссылок редактирования (`"Edit / Expand"`, `"[Change ✏️]"`, `"Edit coffee details"`, `"[Edit ✏️]"`).<br>**L-3:** Карточка 12: при наличии более одной онкологической записи или при редактировании категории `'Cancer or oncological history'` статус `'Ongoing / Still present'` и `'In remission (if applicable)'` могут дублировать смысл.<br>**L-4:** Карточка 14: ветка `"I have had negative test results"` не содержит варианта `"More than 1 year ago"` в списке давности тестирования (`_infectiousLastTested`), если тест был сдан более года назад. |

---

## B. Проверка по карточкам (Age Gate + Карточки 1–16 + Final Screen)

### Шаг 0 — Age Gate (`_stepAgeGate`)
- **Файлы и строки:** [lib/anamnesis_screen.dart](file:///E:/aegis_health/lib/anamnesis_screen.dart#L789-L832)
- **Логика и интерфейс:** Экран подтверждения совершеннолетия (18+) и дисклеймера. Кнопка `"I'm 18+, let's go! 🚀"` сохраняет `age_confirmation = 'Confirmed'` и вызывает `_nextPage()`.
- **Хранение и контракт:** `age_confirmation` обязателен в DTO и `backend/app/schemas/consilium.py` (`min_length=1, max_length=64`).
- **Замечания:** Проблем не обнаружено (Status: **PASS**).

---

### Карточка 1 — Биологический профиль (`_stepBioProfile`)
- **Файлы и строки:** [lib/anamnesis_screen.dart](file:///E:/aegis_health/lib/anamnesis_screen.dart#L834-L1110), [consilium.py](file:///E:/aegis_health/backend/app/schemas/consilium.py#L345-L429)
- **Проверяемые сценарии:**
  - Пол `Male` / `Female`; переключение `Female → Male` гарантированно очищает `_pregnancyStatus`, `_pregnancyTrimester`, `_pregnancyWeeksController`, `_breastfeedingStatus` в памяти и вызывает `vault.removeAnamnesisKeys(['pregnancy_status', 'pregnancy_trimester', 'pregnancy_weeks', 'breastfeeding_status'])` (строки 877–885, 1104–1106).
  - Возраст `18–120` (санитизация через `_parseSanitizedInt`, строки 48–62), защита от `NaN`/`Inf`/букв/дробей.
  - Нестандартный возраст `101–120` требует явного подтверждения (`_ageConfirmedUnusual`).
  - Ветка беременности (`Да`, `Возможно, но не подтверждено`, `Нет`, `Предпочитаю не отвечать`), триместр и срок в неделях (`1–45`) с проверкой непротиворечивости триместра и недель:
    - 1-й триместр: `1–13` нед.;
    - 2-й триместр: `14–27` нед.;
    - 3-й триместр: `28–45` нед.
  - Грудное вскармливание (`_breastfeedingStatus`) опрашивается независимо от статуса беременности.
- **Выявленные проблемы:** **H-1** (стирание `_customInputController` в `_nextPage()`), **M-1** (русский текст вопросов/опций/ошибок в блоках беременности, лактации и подтверждения возраста), **M-3** (флаг `_ageConfirmedUnusual` не сбрасывается при редактировании поля возраста).

---

### Карточка 2 — Антропометрия и динамика веса (`_stepDimensions`)
- **Файлы и строки:** [lib/anamnesis_screen.dart](file:///E:/aegis_health/lib/anamnesis_screen.dart#L1112-L1330), [consilium.py](file:///E:/aegis_health/backend/app/schemas/consilium.py#L365-L448)
- **Проверяемые сценарии:**
  - Рост `50–260 см` (нестандартный `<100` или `>220` требует подтверждения `_heightConfirmedUnusual`), вес `20–500 кг` (нестандартный `<30` или `>250` требует подтверждения `_weightConfirmedUnusual`).
  - Санитизация запятой и точки (`_parseSanitizedDouble`, строки 64–75), нормализация `.0`.
  - Динамика веса за 3–6 месяцев (`Stable`, `Gained`, `Lost`, `Unknown`), количество кг (`> 0` и `< weight` при потере веса), намеренность изменения (`Yes`, `No`, `Unsure`).
- **Выявленные проблемы:** **H-1** (стирание `hController` и `wController` в `_nextPage()`), **M-1** (русский текст блока динамики веса и подтверждений), **M-3** (пропуск выбора `_weightChangeStatus == null` не блокирует `Next`, а флаги `_heightConfirmedUnusual` / `_weightConfirmedUnusual` не сбрасываются при изменении текста).

---

### Карточка 3 — Никотин (`_stepNicotine`)
- **Файлы и строки:** [lib/anamnesis_screen.dart](file:///E:/aegis_health/lib/anamnesis_screen.dart#L1332-L1579), [consilium.py](file:///E:/aegis_health/backend/app/schemas/consilium.py#L450-L468)
- **Проверяемые сценарии:**
  - 5 вариантов статуса: никогда (`Never` → сохраняется как `'Non-user'`), иногда (`Occasional` → `'Active'`), регулярно (`Regular` → `'Active'`), в прошлом (`Former` → `'Quit'`), отказ от ответа (`Prefer not to say`).
  - При `Never` и `Prefer not to say` вызывается `vault.removeAnamnesisKeys(['nicotine_products', 'nicotine_product_tenure', 'nicotine_current_frequency', 'nicotine_quantities', 'nicotine_quit_when', 'nicotine_freq'])` и выполняется мгновенный переход на следующую карточку.
  - При `Occasional`, `Regular`, `Former` открывается подшаг `_nicotineSubStep = 1` с мультивыбором продуктов, индивидуальным стажем по каждому продукту (`_nicotineProductTenure[prod]`), частотой и объёмом (или давностью отказа `_nicotineQuitWhen`).
- **Выявленные проблемы:** **H-2** (потеря навигации на подшаг 0, сохранение конфликтующих ключей при смене `Regular ↔ Former`, несовпадение значений `'Non-user'`/`'Active'`/`'Quit'` при восстановлении из Vault), **M-1** (все опции, подвопросы, кнопка `"Далее ➡️"` и ошибки валидации на русском языке).

---

### Карточка 4 — Алкоголь (`_stepAlcohol`)
- **Файлы и строки:** [lib/anamnesis_screen.dart](file:///E:/aegis_health/lib/anamnesis_screen.dart#L1581-L1739), [consilium.py](file:///E:/aegis_health/backend/app/schemas/consilium.py#L470-L473)
- **Проверяемые сценарии:**
  - 6 вариантов статуса: никогда (`Never`), только в прошлом (`Former`), редко (`Rare`), 1–3 раза в месяц (`1-3/month`), 1–2 раза в неделю (`1-2/week`), 3+ раза в неделю (`>=3/week`).
  - При `Never` и `Former` зависимые ключи (`alcohol_types`, `alcohol_volumes`, `alcohol_comment`, `alcohol_drinks`, `user_text_alcohol`) удаляются из Vault.
  - При употреблении открывается `_alcoholSubStep = 1`: выбор напитков (пиво, вино, крепкий алкоголь, коктейли, алкоэнергетики), пресеты разового объёма и комментарий.
- **Выявленные проблемы:** **H-2** (три разных статуса `'1-3/month'`, `'1-2/week'`, `'>=3/week'` перезаписываются в Vault одной строкой `'Frequently'`, из-за чего теряется точная частота употребления и ломается восстановление состояния; кнопка «Назад» на подшаге 1 перебрасывает на карточку 3 вместо возврата к выбору частоты), **M-1** (опции, напитки, объёмы, кнопка `"Далее ➡️"` и ошибки на русском языке).

---

### Карточка 5 — Психоактивные вещества (`_stepSpecFactors`)
- **Файлы и строки:** [lib/anamnesis_screen.dart](file:///E:/aegis_health/lib/anamnesis_screen.dart#L1741-L1850), [consilium.py](file:///E:/aegis_health/backend/app/schemas/consilium.py#L475-L482)
- **Проверяемые сценарии:**
  - Мультивыбор веществ (`Каннабис...`, `Стимуляторы...`, `Седативные...`, `Галлюциногены...`, `Ингалянты...`, `Опиоиды...`, `Другие вещества`, `Употреблял(а) ранее, сейчас нет`) и взаимоисключающие опции `"🚫 Не употребляю"` (`'Не употребляю'`) и `"🤐 Предпочитаю не отвечать"` (`'Предпочитаю не отвечать'`).
  - При выборе `"Не употребляю"` или `"Предпочитаю не отвечать"` очищаются `_substanceTypes`, `_substanceLastUse`, `_substanceNotesController` и удаляются ключи `substance_last_use`, `substance_notes`, `user_text_spec_factors`, `text_spec_factors`.
- **Выявленные проблемы:** **M-1** (заголовок, подзаголовок, все опции и ошибки на русском языке), **M-4** (подвопрос `"Когда это было в последний раз?"` не проверяется на обязательность при нажатии `"Continue ➡️"`, а пункт `"Употреблял(а) ранее, сейчас нет"` реализован как рядовой элемент списка веществ), **M-8** (`buildAnamnesisAnswers` читает только `global['text_spec_factors']`).

---

### Карточка 6 — Сон (`_stepSleep`)
- **Файлы и строки:** [lib/anamnesis_screen.dart](file:///E:/aegis_health/lib/anamnesis_screen.dart#L1852-L2020), [consilium.py](file:///E:/aegis_health/backend/app/schemas/consilium.py#L484-L491)
- **Проверяемые сценарии:**
  - 4 блока: оценка сна (`sleep_quality`), длительность (`sleep_duration`), симптомы сна (`sleep_symptoms` с взаимоисключающим `"None of the above"`), график сна (`sleep_schedule`) + свободная заметка (`sleep_schedule_notes` / `user_text_sleep` / `text_sleep`).
  - Все 4 основных вопроса валидируются на обязательность перед переходом на карточку 7.
  - Английский интерфейс, сохранение и восстановление из Vault работают корректно.
- **Замечания:** Проблем в логике и UI карточки не обнаружено (Status: **PASS**).

---

### Карточка 7 — Питание (`_stepDiet`)
- **Файлы и строки:** [lib/anamnesis_screen.dart](file:///E:/aegis_health/lib/anamnesis_screen.dart#L2022-L2423), [consilium.py](file:///E:/aegis_health/backend/app/schemas/consilium.py#L493-L523)
- **Проверяемые сценарии:**
  - Тип питания (`diet_types`), обязательное уточнение для `"Special diet / Other"` (`diet_custom_details`).
  - Автоматическая фильтрация и блокировка несовместимых базовых продуктов (`diet_staples`):
    - `Vegan` блокирует мясо, рыбу, молочные продукты и яйца;
    - `Vegetarian` блокирует мясо и рыбу;
    - `Pescatarian` блокирует мясо.
  - При переключении типа диеты конфликтующие продукты автоматически удаляются из `_dietStaples` (строки 2055–2063).
  - Приёмы пищи (`diet_meals_per_day`) и перекусы (`diet_snacks_per_day`).
  - Продукты «время от времени» (`diet_occasional_foods` + `diet_food_frequencies`), взаимоисключение с `"None of the above"`.
  - Исключённые продукты (`_hasExcludedFoods` + `diet_excluded_foods_text`) и описание обычного дня (`diet_day_description`).
- **Выявленные проблемы:** **M-5** (чекбокс `"I avoid specific foods or ingredients"` позволяет свернуть блок и пройти дальше с пустым полем ввода; выбор частоты для продукта в `diet_occasional_foods` мгновенно сворачивает подкарточку без кнопки `Save and collapse`), **M-6** (повторное нажатие на кнопку уже заполненного продукта в `diet_occasional_foods` снимает выбор и удаляет частоту вместо раскрытия/сворачивания).

---

### Карточка 8 — Напитки и водный режим (`_stepDrinks`)
- **Файлы и строки:** [lib/anamnesis_screen.dart](file:///E:/aegis_health/lib/anamnesis_screen.dart#L2425-L2713), [consilium.py](file:///E:/aegis_health/backend/app/schemas/consilium.py#L525-L537)
- **Проверяемые сценарии:**
  - Суточный объём жидкости (`drinks_total_volume`).
  - Обычные напитки (`drinks_types`).
  - Подкарточка кофе (`Coffee and coffee beverages`): типы кофе (`drinks_coffee_types`) и частота (`drinks_coffee_frequency`), кнопка `"Save and collapse"`, сворачивание в компактный бейдж `"Selected: ... • Frequency: ..."` с кнопкой `"Edit coffee details"`.
  - Независимость `"Decaf coffee"` от обычного кофе.
  - Уточнение частоты кофеиносодержащих напитков (`drinks_caffeine_frequency`) при выборе `'Black / Green tea'` или `'Energy drinks'`.
- **Выявленные проблемы:** **M-2** (снятие выбора с `'Black / Green tea'` и `'Energy drinks'` не сбрасывает `_drinksCaffeineFrequency` и сохраняет устаревший ключ `drinks_caffeine_frequency` в Vault и DTO).

---

### Карточка 9 — Пищеварение и ЖКТ (`_stepGISignals`)
- **Файлы и строки:** [lib/anamnesis_screen.dart](file:///E:/aegis_health/lib/anamnesis_screen.dart#L2715-L2984), [consilium.py](file:///E:/aegis_health/backend/app/schemas/consilium.py#L539-L568)
- **Проверяемые сценарии:**
  - Мультивыбор симптомов ЖКТ (`gi_symptoms`) с взаимоисключающим `"None of the above"`.
  - Для каждого выбранного симптома раскрывается подкарточка с обязательным выбором частоты (`frequency`) и давности (`duration`), кнопкой `"Save and collapse"` и компактным бейджем `"$freq • $dur"` + `"[Edit ✏️]"`.
  - При выборе `"None of the above"` все детали `gi_symptom_details` очищаются.
- **Выявленные проблемы:** **M-6** (повторное нажатие на основную строку свёрнутого симптома удаляет симптом и стирает его `frequency`/`duration` вместо раскрытия), **M-8** (`buildAnamnesisAnswers` читает только `global['text_gi_signals']` без приоритета `global['user_text_gi_signals']`).

---

### Карточка 10 — Работа и режим дня (`_stepWorkMode`)
- **Файлы и строки:** [lib/anamnesis_screen.dart](file:///E:/aegis_health/lib/anamnesis_screen.dart#L2986-L3125), [consilium.py](file:///E:/aegis_health/backend/app/schemas/consilium.py#L570-L573)
- **Проверяемые сценарии:**
  - Характер основной деятельности (`work_type`, 5 вариантов), необязательное поле профессии (`work_occupation`), рабочий график (`work_schedule`, 5 вариантов), уровень стресса (`work_stress`, 4 варианта), комментарий (`user_text_work`).
  - Обязательность `work_type`, `work_schedule`, `work_stress` проверяется перед переходом.
- **Замечания:** Проблем не обнаружено (Status: **PASS**).

---

### Карточка 11 — Физическая активность (`_stepSport`)
- **Файлы и строки:** [lib/anamnesis_screen.dart](file:///E:/aegis_health/lib/anamnesis_screen.dart#L3127-L3371), [consilium.py](file:///E:/aegis_health/backend/app/schemas/consilium.py#L575-L595)
- **Проверяемые сценарии:**
  - Уровень активности (`sport_level`, 4 варианта).
  - При выборе `"Almost no physical activity"` количественные поля (`activity_days_per_week`, `activity_duration_minutes`, `activity_types`, `activity_other_details`) скрываются и удаляются из Vault, но поле заметок (`_textSportController` / `user_text_sport`) **сохраняется** (позволяя указать причину, например травму или послеоперационный режим).
  - При выборе `"Light"`, `"Moderate"` или `"High"` обязательно заполняются дни в неделю, длительность тренировки и виды активности (а при выборе `"Other"` — текстовое уточнение `activity_other_details`).
- **Замечания:** Проблем не обнаружено (Status: **PASS**).

---

### Карточка 12 — Хронические заболевания (`_stepChronic`)
- **Файлы и строки:** [lib/anamnesis_screen.dart](file:///E:/aegis_health/lib/anamnesis_screen.dart#L3373-L3810), [consilium.py](file:///E:/aegis_health/backend/app/schemas/consilium.py#L597-L660)
- **Проверяемые сценарии:**
  - 14 положительных категорий хронических заболеваний + 2 взаимоисключающих статуса (`"No diagnosed chronic conditions"` и `"Not sure / Haven't been evaluated"`).
  - Для каждой выбранной категории: обязательное текстовое поле диагноза (`diagnosis`), необязательный год/возраст постановки (`diagnosed_when`), текущий статус (`current_status`), вопрос `"Does this condition currently cause symptoms or discomfort?"` (`currently_bothers`) и условное обязательное поле текущих жалоб (`current_symptoms_text`).
  - Для категории `"Cancer or oncological history"` дополнительно запрашивается и синхронизируется `onco_status` (`"Active treatment"` / `"Remission"`).
  - Формируется обратная сводка `chronic_diseases`.
- **Выявленные проблемы:** **M-6** (повторный тап по заголовку категории удаляет категорию и стирает контроллеры текста; сводная строка `chronic_diseases` не обрезается через `_clampSummary(..., 1024)`), **L-3** (в категории `'Cancer or oncological history'` пользователь выбирает и `onco_status`, и `current_status` со схожими пунктами `'In remission (if applicable)'`).

---

### Карточка 13 — Недавние заболевания и события (`_stepRecentEvents`)
- **Файлы и строки:** [lib/anamnesis_screen.dart](file:///E:/aegis_health/lib/anamnesis_screen.dart#L3812-L4125), [consilium.py](file:///E:/aegis_health/backend/app/schemas/consilium.py#L662-L711)
- **Проверяемые сценарии:**
  - 7 категорий событий за последние 3 месяца (COVID-19 объединён с ОРВИ/гриппом в `"Cold, flu, COVID-19 or another respiratory viral infection"`) + взаимоисключающий `"None of the above"`.
  - Для каждого события: непересекающиеся интервалы времени (`"Within the last 7 days"` → `0_7_days`, `"8–30 days ago"` / `"Within the last month"` → `8_30_days`, `"1–3 months ago"` → `31_90_days`), статус восстановления (`"Fully recovered"`, `"Improving"`, `"Still ongoing"`), необязательные детали (`details`).
  - Формируется обратная сводка `recent_illness`.
- **Выявленные проблемы:** **M-6** (повторный тап по заголовку выбранного события удаляет событие и стирает его детали; сводная строка `recent_illness` не обрезается через `_clampSummary(..., 512)`).

---

### Карточка 14 — Инфекционный анамнез (`_stepSafeSpace`)
- **Файлы и строки:** [lib/anamnesis_screen.dart](file:///E:/aegis_health/lib/anamnesis_screen.dart#L4127-L4336), [consilium.py](file:///E:/aegis_health/backend/app/schemas/consilium.py#L713-L755)
- **Проверяемые сценарии:**
  - 5 взаимоисключающих веток (`infectious_history_status`):
    1. `"No diagnosed chronic infections"`
    2. `"I have been diagnosed with one or more of these infections"` → мультивыбор `infectious_diagnosed` (`Hepatitis B`, `Hepatitis C`, `HIV`, `Syphilis`, `Tuberculosis`, `Other significant chronic or past infection`)
    3. `"I have had negative test results"` → мультивыбор `infectious_tested_negative` + давность тестирования `infectious_last_tested`
    4. `"Not sure / Never tested"`
    5. `"Prefer not to answer"`
  - Чёткое разделение подтверждённого диагноза и отрицательных тестов как в UI/Vault, так и в серверной схеме (`consilium.py:713–755`).
- **Выявленные проблемы:** **L-4** (в списке `_infectiousLastTestedOptions` максимальный интервал — `"About 1 year ago"`, отсутствует вариант `"More than 1 year ago"`).

---

### Карточка 15 — Семейная история и наследственность (`_stepHeredity`)
- **Файлы и строки:** [lib/anamnesis_screen.dart](file:///E:/aegis_health/lib/anamnesis_screen.dart#L4338-L4836), [consilium.py](file:///E:/aegis_health/backend/app/schemas/consilium.py#L127-L143), [consilium.py](file:///E:/aegis_health/backend/app/schemas/consilium.py#L757-L826)
- **Проверяемые сценарии:**
  - 4 статуса верхнего уровня (`family_history_status`): `"No known significant family history"`, `"Yes, there are conditions in my family"`, `"I don't know my family medical history"`, `"Prefer not to answer"`.
  - При переключении с `"Yes..."` на любой из 3 не-положительных статусов экранное состояние сворачивается, а при сохранении вызывается `vault.removeAnamnesisKeys(['family_history_relatives', 'family_history_details'])` + `VaultService.buildAnamnesisAnswers()` дополнительно гарантирует передачу `null` в `familyHistoryRelatives` и `familyHistoryDetails`.
  - При `"Yes..."` доступна двухуровневая «матрёшка»:
    - Уровень 1 — выбор родственников (`Mother`, `Father`, `Sister`, `Brother`, `Maternal grandmother`, `Maternal grandfather`, `Paternal grandmother`, `Paternal grandfather`, `Other close relative`). Заголовок карточки родственника снабжён **отдельным чекбоксом** (снятие выбора) и **кликом по тексту заголовка** (раскрытие/сворачивание без потери данных).
    - Уровень 2 — мультивыбор из 11 категорий заболеваний для каждого выбранного родственника + обязательное текстовое уточнение (`details`) для каждой выбранной категории + обязательное поле степени родства (`relationship_detail`) для `"Other close relative"`.
  - Обратная совместимость: формируется `heredity_risks` с ограничением длины `_clampSummary(..., 512)`.
- **Замечания:** Проблем не обнаружено (Status: **PASS**).

---

### Карточка 16 — Лекарства, витамины и добавки (`_stepMeds`)
- **Файлы и строки:** [lib/anamnesis_screen.dart](file:///E:/aegis_health/lib/anamnesis_screen.dart#L4838-L5203), [consilium.py](file:///E:/aegis_health/backend/app/schemas/consilium.py#L828-L876)
- **Проверяемые сценарии:**
  - 4 категории (`Prescription medications`, `Over-the-counter medications taken regularly`, `Vitamins or dietary supplements`, `Herbal or homeopathic remedies`) + взаимоисключающая опция `"No, I don't take any medications or supplements regularly"`.
  - Применяется компонент `_buildMatryoshkaCardHeader` (отдельный чекбокс для включения/снятия категории и нажатие на заголовок для сворачивания/разворачивания).
  - Для каждой выбранной категории обязательно заполняется поле названия/дозировки (`names`, до 500 символов) и необязательное поле длительности/курса (`course`, до 300 символов).
  - Защита от случайного снятия галочки: метод `_archiveMedicationDraft(cat)` архивирует непустой черновик в `_medicationDraftArchive[cat]` (а если пользователь очистил текст до снятия галочки — удаляет устаревший архив `_medicationDraftArchive.remove(cat)`), позволяя восстановить текст при повторном включении категории в рамках сессии, но **не** отправляя снятые категории в `VaultService` или API.
  - При нажатии `"Finish 🚀"` вызывается `_nextPage()` с переходом на финальный экран (`_stepFinal`).
- **Замечания:** Проблем не обнаружено (Status: **PASS**).

---

### Шаг 17 — Финальный экран (`_stepFinal`)
- **Файлы и строки:** [lib/anamnesis_screen.dart](file:///E:/aegis_health/lib/anamnesis_screen.dart#L5205-L5216)
- **Логика и интерфейс:** Отображает подтверждение `"Your Profile is Configured! 🚀"` и кнопку `"Continue ➡️"`, вызывающую `_finishAnamnesis(vault)`.
- **Выявленные проблемы:** **L-1** (`progress = 17 / 16 = 1.0625`).

---

### Детальный реестр выявленных проблем (по обязательному шаблону)

#### Проблема H-1
- **Приоритет:** High
- **Карточка:** Карточка 1 (Biological Profile) и Карточка 2 (Your Dimensions), влияет на возврат кнопкой `Back` со всех последующих карточек
- **Файл и строки:** [lib/anamnesis_screen.dart](file:///E:/aegis_health/lib/anamnesis_screen.dart#L5218-L5231) (особенно строки `5224–5226`)
- **Суть проблемы:** Метод `_nextPage()` содержит безусловную очистку трёх текстовых контроллеров при каждом переходе на следующую страницу:
  ```dart
  _customInputController.clear();
  hController.clear();
  wController.clear();
  ```
- **Сценарий воспроизведения:**
  1. Открыть анамнез, пройти Age Gate, на Карточке 1 ввести возраст `28`, выбрать пол `Male`, нажать `"Next"`. В этот момент `_nextPage()` сохраняет `age = 28` в Vault, но очищает `_customInputController.text = ""`.
  2. На Карточке 2 ввести рост `175`, вес `70`, выбрать стабильный вес, нажать `"Next"`. `_nextPage()` сохраняет рост и вес в Vault, но очищает `hController.text = ""` и `wController.text = ""`.
  3. На Карточке 3 нажать кнопку `Back` (`Icons.arrow_back`) в верхнем левом углу, чтобы вернуться на Карточку 2 и поправить вес или динамику веса.
  4. Поля `Height (cm)` и `Weight (kg)` оказываются пустыми, а нажатие `"Next"` блокируется ошибкой `"Please enter a valid height (50-260 cm)"`. Аналогично при возврате на Карточку 1 пустым оказывается поле `Age`.
- **Последствия:** Пользователь теряет введённые на экране значения возраста, роста и веса при любом нажатии кнопки «Назад» и вынужден вводить их заново.
- **Предлагаемое исправление:** Удалить строки `5224–5226` (`_customInputController.clear(); hController.clear(); wController.clear();`) из `_nextPage()`. Очистка этих контроллеров при сбросе сессии или смене пациента уже выполняется в `_resetLocalState()` (строки `538–540`).
- **Статус доказательства:** Подтверждено кодом (`lib/anamnesis_screen.dart:5224-5226`).

---

#### Проблема H-2
- **Приоритет:** High
- **Карточка:** Карточка 3 (Nicotine) и Карточка 4 (Alcohol)
- **Файл и строки:** [lib/anamnesis_screen.dart](file:///E:/aegis_health/lib/anamnesis_screen.dart#L237-L261), [lib/anamnesis_screen.dart](file:///E:/aegis_health/lib/anamnesis_screen.dart#L645-L663), [lib/anamnesis_screen.dart](file:///E:/aegis_health/lib/anamnesis_screen.dart#L1372-L1578), [lib/anamnesis_screen.dart](file:///E:/aegis_health/lib/anamnesis_screen.dart#L1612-L1738), [consilium.py](file:///E:/aegis_health/backend/app/schemas/consilium.py#L465-L467)
- **Суть проблемы:** В карточках 3 и 4 объединены три связанных дефекта управления состоянием и подшагами (`_nicotineSubStep`, `_alcoholSubStep`):
  1. **Отсутствие очистки взаимоисключающих ключей между `Occasional`/`Regular` и `Former` (Карточка 3):** Если пользователь сначала выбрал `'Regular'`, указал частоту `_nicotineCurrentFrequency` и нажал `"Далее ➡️"`, в Vault сохраняются `nicotine_current_frequency`, `nicotine_freq`, `nicotine_quantities`. Если затем пользователь возвращается на Карточку 3, выбирает `'Former'`, указывает `_nicotineQuitWhen` и нажимает `"Далее ➡️"`, строки `1559–1576` сохраняют `nicotine_status = 'Quit'` и `nicotine_quit_when`, но **не удаляют** `nicotine_current_frequency`, `nicotine_freq` и `nicotine_quantities` ни из локальных переменных, ни из Vault. На бэкенде это вызывает `ValidationError`: `"Для бывшего употребления табака текущая частота не должна указываться"` (`consilium.py:465–467`).
  2. **Несовпадение кодов статуса в UI и в Vault при восстановлении (`_restoreLocalStateFromVault`):**
     - На Карточке 3 UI проверяет `_nicotineStatus == 'Never' | 'Occasional' | 'Regular' | 'Former'` (строки `1373–1404`, `1465`, `1526`), но в Vault сохраняет `'Non-user'`, `'Active'`, `'Quit'` (строки `1382`, `1559–1560`). При повторном открытии экрана `_restoreLocalStateFromVault` (строка `238`) считывает `_nicotineStatus = 'Active'` или `'Quit'`, из-за чего ни одна опция на подшаге 0 не подсвечена как выбранная, а условия `if (_nicotineStatus == 'Occasional' || _nicotineStatus == 'Regular')` и `if (_nicotineStatus == 'Former')` на подшаге 1 оба возвращают `false`.
     - На Карточке 4 UI проверяет `_alcoholStatus == '1-3/month' | '1-2/week' | '>=3/week'` (строки `1629–1647`), но при сохранении (строка `1723`) записывает в `alcohol_status` одно общее значение `'Frequently'`, безвозвратно теряя выбранную градацию частоты (`1-3 раза в месяц` vs `1-2 раза в неделю` vs `3+ раза в неделю`).
  3. **Невозможность вернуться с подшага 1 на подшаг 0 внутри карточки:** При выборе употребления никотина или алкоголя экран переключается на `_nicotineSubStep = 1` / `_alcoholSubStep = 1`. Внутри карточки нет кнопки смены статуса, а нажатие верхней кнопки `Back` (`_previousPage()`, строки `645–663`) одновременно сбрасывает `_nicotineSubStep = 0` и вызывает `_controller.previousPage(...)`, выбрасывая пользователя на предыдущую карточку вместо возврата на подшаг 0 текущей карточки.
- **Сценарий воспроизведения:**
  1. На Карточке 3 выбрать `"3️⃣ Регулярно"`, отметить продукт, стаж, частоту `"Ежедневно"`, нажать `"Далее ➡️"`.
  2. Нажать кнопку `Back` на Карточке 4 → попадаем на Карточку 3 (`_nicotineSubStep = 0`).
  3. Выбрать `"4️⃣ Употреблял(а) раньше, сейчас не употребляю"`, выбрать давность отказа `"1–5 лет назад"`, нажать `"Далее ➡️"`.
  4. Вызвать `vault.buildAnamnesisAnswers()` и отправить в `AnamnesisQuestionnaireAnswers` на бэкенде → сервер выбрасывает `422 ValidationError` из-за одновременного присутствия `nicotine_status="Quit"` и `nicotine_current_frequency="Ежедневно"`.
- **Последствия:** Ошибка валидации `422` при отправке анамнеза после смены статуса курения с текущего на прошлое; потеря выбранной частоты употребления алкоголя; невозможность изменить случайно нажатый статус на подшаге 1 без ухода на предыдущую карточку.
- **Предлагаемое исправление:**
  1. В `_previousPage()` (строки `645–663`): если `_currentPage == 3 && _nicotineSubStep == 1`, выполнять только `setState(() { _nicotineSubStep = 0; _errorMessage = null; }); return;` (и аналогично для `_currentPage == 4 && _alcoholSubStep == 1`).
  2. На Карточке 3 при выборе `'Former'` очищать `_nicotineCurrentFrequency = null`, `_nicotineQuantities.clear()` и вызывать `vault.removeAnamnesisKeys(['nicotine_current_frequency', 'nicotine_quantities', 'nicotine_freq'])`; при выборе `'Occasional'`/`'Regular'` очищать `_nicotineQuitWhen = null` и вызывать `vault.removeAnamnesisKeys(['nicotine_quit_when'])`.
  3. Сохранять и восстанавливать точный UI-статус (либо нормализованный английский статус `'Never'`, `'Occasional'`, `'Regular'`, `'Former'`, `'Prefer not to say'` и для алкоголя `'Never'`, `'Former'`, `'Rare'`, `'1-3/month'`, `'1-2/week'`, `'>=3/week'`), маппя его в совместимые значения при необходимости.
- **Статус доказательства:** Подтверждено кодом (`lib/anamnesis_screen.dart:237-261, 645-663, 1398-1403, 1559-1576, 1722-1723`, `backend/app/schemas/consilium.py:465-467`).

---

#### Проблема M-1
- **Приоритет:** Medium
- **Карточка:** Карточки 1, 2, 3, 4, 5 + кнопка навигации `Back`
- **Файл и строки:** [lib/anamnesis_screen.dart](file:///E:/aegis_health/lib/anamnesis_screen.dart#L727) (`tooltip: 'Назад'`), [lib/anamnesis_screen.dart](file:///E:/aegis_health/lib/anamnesis_screen.dart#L933-L1085) (Card 1), [lib/anamnesis_screen.dart](file:///E:/aegis_health/lib/anamnesis_screen.dart#L1152-L1306) (Card 2), [lib/anamnesis_screen.dart](file:///E:/aegis_health/lib/anamnesis_screen.dart#L1364-L1555) (Card 3), [lib/anamnesis_screen.dart](file:///E:/aegis_health/lib/anamnesis_screen.dart#L1604-L1718) (Card 4), [lib/anamnesis_screen.dart](file:///E:/aegis_health/lib/anamnesis_screen.dart#L1745-L1827) (Card 5)
- **Суть проблемы:** Карточки Stage 1 (1–5) создавались до введения требования «100% английский пользовательский интерфейс» (которое применялось начиная со Stage 2, карточки 6–16). В результате карточки 1–5 содержат русскоязычные заголовки (Карточка 5 `"Дополнительные факторы здоровья 🛡️"`), вопросы, варианты ответов, плейсхолдеры, кнопки `"Далее ➡️"` (Карточки 3–4), подсказку `tooltip: 'Назад'` и сообщения об ошибках.
- **Сценарий воспроизведения:** Пройти опросник с 1-й по 6-ю карточку — интерфейс переключается между английским и русским языками внутри одной карточки (например, на Карточке 1 заголовок `"Biological Profile"`, но ниже `"Беременны ли вы сейчас?"`).
- **Последствия:** Нарушение требования единого англоязычного интерфейса опросника; отправка русскоязычных строковых значений из карточек 1–5 в DTO.
- **Предлагаемое исправление:** Перевести все строки карточек 1–5 и `tooltip: 'Back'` на английский язык, синхронизировав ожидаемые строки в `test/anamnesis_cards_v2_test.dart` и проверив списки допустимых значений в `backend/app/schemas/consilium.py` (большинство валидаторов Stage 1 в `consilium.py` уже принимают и русские, и английские эквиваленты, например `'yes'`/`'да'`, `'gained'`/`'набрал(а) вес'`, `'none'`/`'не употребляю'`).
- **Статус доказательства:** Подтверждено кодом (`lib/anamnesis_screen.dart:727, 933-1827`). Полный перечень строк приведён в разделе **E. Английский интерфейс**.

---

#### Проблема M-2
- **Приоритет:** Medium
- **Карточка:** Карточка 8 (Drinks & Hydration)
- **Файл и строки:** [lib/anamnesis_screen.dart](file:///E:/aegis_health/lib/anamnesis_screen.dart#L2474-L2530), [lib/anamnesis_screen.dart](file:///E:/aegis_health/lib/anamnesis_screen.dart#L2696-L2700)
- **Суть проблемы:** При выборе `'Black / Green tea'` или `'Energy drinks'` отображается блок выбора `_drinksCaffeineFrequency` (`"How often do you have caffeinated drinks (tea, energy drinks)?"`). Однако если пользователь сначала выбирает `'Black / Green tea'` и частоту `'3–4 servings a day'`, а затем снимает галочку с `'Black / Green tea'`, переменная `_drinksCaffeineFrequency` **не сбрасывается в `null`**, а при нажатии `"Next ➡️"` код в строках `2696–2700`:
  ```dart
  if (_drinksCaffeineFrequency != null) {
    vault.saveAnamnesis('drinks_caffeine_frequency', _drinksCaffeineFrequency);
  }
  ```
  сохраняет `drinks_caffeine_frequency` в Vault без проверки `hasCaffeinatedTeaOrEnergy`.
- **Сценарий воспроизведения:**
  1. На Карточке 8 выбрать объём воды `'1.5–2 liters'`, отметить `'Still water'` и `'Black / Green tea'`.
  2. Выбрать частоту кофеина `'3–4 servings a day'`.
  3. Снять галочку с `'Black / Green tea'` (оставив только `'Still water'`).
  4. Нажать `"Next ➡️"`. В Vault и в `AnamnesisQuestionnaireAnswers` уходит `drinks_types: ['Still water']` и одновременно `drinks_caffeine_frequency: '3–4 servings a day'`.
- **Последствия:** Передача фантомной частоты употребления кофеиносодержащих напитков при выборе только чистой воды.
- **Предлагаемое исправление:**
  1. При снятии выбора с элемента в `_drinksTypes` (строка `2509`), если `!_drinksTypes.contains('Black / Green tea') && !_drinksTypes.contains('Energy drinks')`, сбрасывать `_drinksCaffeineFrequency = null`.
  2. В обработчике `"Next ➡️"` (строка `2696`) проверять `if (hasCaffeinatedTeaOrEnergy && _drinksCaffeineFrequency != null)`.
- **Статус доказательства:** Подтверждено кодом (`lib/anamnesis_screen.dart:2508-2525, 2696-2700`).

---

#### Проблема M-3
- **Приоритет:** Medium
- **Карточка:** Карточка 1 (Biological Profile) и Карточка 2 (Your Dimensions)
- **Файл и строки:** [lib/anamnesis_screen.dart](file:///E:/aegis_health/lib/anamnesis_screen.dart#L910-L951), [lib/anamnesis_screen.dart](file:///E:/aegis_health/lib/anamnesis_screen.dart#L1129-L1326)
- **Суть проблемы:**
  1. **Необязательность вопроса о динамике веса на Карточке 2:** В обработчике кнопки `"Next"` на Карточке 2 (строки `1293–1306`) проверяется только случай `if (_weightChangeStatus == 'Gained' || _weightChangeStatus == 'Lost')`. Если пользователь вообще не выбрал ни один из 4 вариантов `"Изменился ли ваш вес за последние 3–6 месяцев?"` (`_weightChangeStatus == null`), переход на Карточку 3 проходит без ошибки, а если в Vault с прошлого раза остались ключи `weight_change_status`, `weight_change_amount`, `weight_change_intentional`, они не очищаются.
  2. **Отсутствие сброса флагов подтверждения нестандартных значений при редактировании текста:** Поля ввода возраста (`_customInputController`), роста (`hController`) и веса (`wController`) не имеют `onChanged`, который бы сбрасывал `_ageConfirmedUnusual = false` / `_heightConfirmedUnusual = false` / `_weightConfirmedUnusual = false`. Если пользователь ввёл возраст `105`, нажал `"Подтвердить правильность ввода"`, а затем исправил возраст на `118` (другое нестандартное значение) или на `35` (обычное значение), флаг `_ageConfirmedUnusual` остаётся `true` и сохраняется в Vault (`age_confirmed_unusual: true` даже для 35 лет). Кроме того, жёлтая плашка предупреждения не появляется во время набора текста, пока пользователь не нажмёт `"Next"`.
- **Сценарий воспроизведения:**
  1. На Карточке 2 ввести рост `175` и вес `70`, не трогать блок динамики веса и нажать `"Next"` — карточка пропускает вопрос без ответа.
  2. На Карточке 1 ввести возраст `105`, нажать `"Next"`, нажать `"Подтвердить правильность ввода"`, затем изменить текст на `30` и нажать `"Next"` — в Vault записывается `age: '30'` и `age_confirmed_unusual: true`.
- **Последствия:** Пропуск вопроса о динамике веса и некорректное состояние флагов `*_confirmed_unusual`.
- **Предлагаемое исправление:**
  1. Добавить в валидацию Карточки 2 проверку `if (_weightChangeStatus == null) { setState(() => _errorMessage = 'Please select whether your weight has changed'); return; }`.
  2. Добавить `onChanged` в текстовые поля возраста, роста и веса со сбросом соответствующего флага `*_confirmedUnusual = false`, а при сохранении в Vault записывать `*_confirmed_unusual` только если итоговое число действительно входит в нестандартный диапазон (иначе вызывать `vault.removeAnamnesisKeys(['age_confirmed_unusual'])`).
- **Статус доказательства:** Подтверждено кодом (`lib/anamnesis_screen.dart:910-951, 1091-1093, 1129-1209, 1293-1326`).

---

#### Проблема M-4
- **Приоритет:** Medium
- **Карточка:** Карточка 5 (SpecFactors / Psychoactive Substances)
- **Файл и строки:** [lib/anamnesis_screen.dart](file:///E:/aegis_health/lib/anamnesis_screen.dart#L1745-L1848)
- **Суть проблемы:**
  1. При выборе любого психоактивного вещества раскрывается подвопрос `"Когда это было в последний раз?"` (`_substanceLastUse`, строки `1802–1808`), но при нажатии `"Continue ➡️"` (строки `1824–1848`) **отсутствует валидация** `if (hasSubstanceSelected && _substanceLastUse == null)`. Пользователь может перейти дальше, не указав давность употребления.
  2. Вариант `'Употреблял(а) ранее, сейчас нет'` включён в общий массив `substanceOptions` (строка `1752`) наравне с конкретными веществами. Пользователь может выбрать только `'Употреблял(а) ранее, сейчас нет'` без указания вещества, либо выбрать его одновременно с текущими веществами без разграничения, к какому веществу относится прошлый опыт.
- **Сценарий воспроизведения:** На Карточке 5 выбрать `"Каннабис (марихуана, гашиш, ТГК-продукты)"`, не выбирать давность употребления и нажать `"Continue ➡️"` — переход на Карточку 6 выполняется без ошибки (`substance_last_use` остаётся `null`).
- **Последствия:** Неполные данные о давности употребления психоактивных веществ.
- **Предлагаемое исправление:** Добавить проверку `if (hasSubstanceSelected && _substanceLastUse == null)` при нажатии `"Continue ➡️"` (а при `else` удалять `'substance_last_use'` из Vault).
- **Статус доказательства:** Подтверждено кодом (`lib/anamnesis_screen.dart:1745-1848`).

---

#### Проблема M-5
- **Приоритет:** Medium
- **Карточка:** Карточка 7 (Your Diet)
- **Файл и строки:** [lib/anamnesis_screen.dart](file:///E:/aegis_health/lib/anamnesis_screen.dart#L2095-L2150), [lib/anamnesis_screen.dart](file:///E:/aegis_health/lib/anamnesis_screen.dart#L2293-L2302), [lib/anamnesis_screen.dart](file:///E:/aegis_health/lib/anamnesis_screen.dart#L2408-L2413)
- **Суть проблемы:**
  1. Если пользователь отмечает чекбокс `"I avoid specific foods or ingredients (allergies, intolerances)"` (`_hasExcludedFoods = true`), но оставляет текстовое поле пустым и нажимает `"Save and collapse"` (строка `2119`), карточка сворачивается в `"Excluded: None specified"`, а при нажатии `"Next ➡️"` (строки `2408–2413`) молча удаляет ключ `diet_excluded_foods_text` без предупреждения пользователя.
  2. В блоке `"Which of these foods do you consume at least occasionally?"` (строки `2293–2302`) нажатие на любой чип частоты (`_dietFoodFrequencies[prod] = freq; _activeOccasionalFood = null;`) мгновенно сворачивает подкарточку без кнопки `"Save and collapse"`, что отличается от паттерна всех остальных карточек с вложенными уточнениями (карточки 8, 9, 12, 13, 15, 16).
- **Сценарий воспроизведения:** На Карточке 7 поставить галочку `"I avoid specific foods or ingredients"`, не вводить текст, нажать `"Save and collapse"` → отображается `"Excluded: None specified"`, переход `"Next ➡️"` проходит без ошибки, а факт наличия пищевой аллергии/непереносимости теряется.
- **Последствия:** Потеря информации об исключённых продуктах/аллергиях, если пользователь отметил чекбокс, но забыл заполнить текстовое поле.
- **Предлагаемое исправление:** При `_hasExcludedFoods == true` требовать непустой текст в `_dietExcludedController` как при нажатии `"Save and collapse"`, так и при нажатии `"Next ➡️"` (либо попросить снять галочку).
- **Статус доказательства:** Подтверждено кодом (`lib/anamnesis_screen.dart:2118-2140, 2408-2413`).

---

#### Проблема M-6
- **Приоритет:** Medium
- **Карточка:** Карточки 7, 9, 12, 13
- **Файл и строки:** [lib/anamnesis_screen.dart](file:///E:/aegis_health/lib/anamnesis_screen.dart#L2259-L2272) (Card 7), [lib/anamnesis_screen.dart](file:///E:/aegis_health/lib/anamnesis_screen.dart#L2785-L2800) (Card 9), [lib/anamnesis_screen.dart](file:///E:/aegis_health/lib/anamnesis_screen.dart#L3452-L3471) (Card 12), [lib/anamnesis_screen.dart](file:///E:/aegis_health/lib/anamnesis_screen.dart#L3788) (Card 12 summary), [lib/anamnesis_screen.dart](file:///E:/aegis_health/lib/anamnesis_screen.dart#L3883-L3901) (Card 13), [lib/anamnesis_screen.dart](file:///E:/aegis_health/lib/anamnesis_screen.dart#L4101) (Card 13 summary)
- **Суть проблемы:**
  1. **Деструктивное поведение при нажатии на заголовок выбранной категории (Карточки 7, 9, 12, 13):** В карточках 15 и 16 используется `_buildMatryoshkaCardHeader`, где чекбокс отвечает за выбор/снятие категории, а нажатие на заголовок — за раскрытие/сворачивание. В карточках 7, 9, 12 и 13 основная кнопка категории реализована через `_buildSelectableOption`: если пользователь заполнил подробный диагноз, год, статус и симптомы в Карточке 12, свернул подкарточку кнопкой `"Save and collapse"`, а затем нажал на саму кнопку категории (вместо мелкой ссылки `"[Edit ✏️]"` под ней), срабатывает ветка `if (isSel)`, которая **немедленно удаляет категорию и безвозвратно очищает все введённые поля и контроллеры** (`_chronicCategories.remove(cat); _chronicConditionDetails.remove(cat); _clearChronicCategoryControllers(cat);`).
  2. **Отсутствие `_clampSummary` для сводных строк `chronic_diseases` (Карточка 12) и `recent_illness` (Карточка 13):** В Карточках 15 и 16 обратные сводки защищены `_clampSummary(..., 512)`, тогда как в Карточке 12 (`line 3788`: `vault.saveAnamnesis('chronic_diseases', summaryParts.join(', '))`, лимит на сервере `max_length=1024`) и Карточке 13 (`line 4101`: `vault.saveAnamnesis('recent_illness', summaryParts.join(', '))`, лимит на сервере `max_length=512`) конкатенация не ограничена по длине. При выборе нескольких категорий с подробным текстом длина `recent_illness` может превысить 512 символов и вызвать `422 ValidationError` на бэкенде.
- **Сценарий воспроизведения:**
  1. На Карточке 12 выбрать `"Diabetes or blood sugar disorders"`, ввести диагноз, дату, статус, жалобы, нажать `"Save and collapse"`.
  2. Нажать на плашку `"Diabetes or blood sugar disorders"` чтобы снова открыть её — категория снимается и весь введённый текст исчезает.
- **Последствия:** Случайная потеря введённых медицинских данных при клике по заголовку свёрнутой категории; потенциальное превышение `max_length=512` в `recent_illness` при множественных событиях.
- **Предлагаемое исправление:**
  1. Использовать `_buildMatryoshkaCardHeader` (или при клике на уже свёрнутую заполненную категорию раскрывать её для редактирования, а снятие выбора выполнять через чекбокс/иконку).
  2. Обернуть сохранение `chronic_diseases` в `_clampSummary(summaryParts.join(', '), 1024)` и `recent_illness` в `_clampSummary(summaryParts.join(', '), 512)`.
- **Статус доказательства:** Подтверждено кодом (`lib/anamnesis_screen.dart:2785-2800, 3452-3471, 3788, 3883-3901, 4101`).

---

#### Проблема M-7
- **Приоритет:** Medium
- **Карточка:** Все карточки (`AnamnesisScreen` lifecycle — восстановление черновика гостя и переключение пациентов)
- **Файл и строки:** [lib/anamnesis_screen.dart](file:///E:/aegis_health/lib/anamnesis_screen.dart#L173-L188), [lib/services/vault_service.dart](file:///E:/aegis_health/lib/services/vault_service.dart#L377-L426)
- **Суть проблемы:** В `AnamnesisScreen.didChangeDependencies()` логика инициализации и смены пациента записана так:
  ```dart
  if (_lastSeenPatientId != null &&
      (_lastSeenPatientId != vault.activePatientId || _lastSeenEpoch != vault.sessionEpoch)) {
    _resetLocalState();
  } else if (_lastSeenPatientId == null && vault.activePatientId != null) {
    _restoreLocalStateFromVault(vault);
  }
  _lastSeenPatientId = vault.activePatientId;
  _lastSeenEpoch = vault.sessionEpoch;
  ```
  Здесь не учтены два сценария:
  1. **Гостевой черновик (`vault.activePatientId == null`):** Если неавторизованный пользователь заполнил часть карточек (данные сохранились в guest draft в `VaultService`), закрыл экран анамнеза и открыл его снова до авторизации, условие `_lastSeenPatientId == null && vault.activePatientId != null` ложно, поэтому `_restoreLocalStateFromVault(vault)` **не вызывается**.
  2. **Асинхронная загрузка данных при переключении с пациента A на пациента B на открытом экране:** Метод `vault.setActivePatientId('pat_B')` сначала синхронно увеличивает `_sessionEpoch`, очищает память и вызывает `notifyListeners()`, а затем асинхронно читает `SharedPreferences` в `_loadPatientVaultAsync` и снова вызывает `notifyListeners()`. На первом уведомлении `didChangeDependencies` вызывает `_resetLocalState()` (и пробует `_restoreLocalStateFromVault(vault)`, когда данные `pat_B` ещё не загружены в память) и записывает `_lastSeenPatientId = 'pat_B'`, `_lastSeenEpoch = vault.sessionEpoch`. Когда `_loadPatientVaultAsync` завершает чтение из `SharedPreferences` и вызывает второе `notifyListeners()`, `_lastSeenPatientId` и `_lastSeenEpoch` уже совпадают, поэтому `_restoreLocalStateFromVault(vault)` повторно не запускается!
- **Сценарий воспроизведения:**
  1.В гостевом режиме (`activePatientId == null`) заполнить карточки 1–6, закрыть и заново открыть `AnamnesisScreen` — экран открывается пустым, хотя в `vault.getSystemContext('global')` данные гостя присутствуют.
  2.При смонтированном `AnamnesisScreen` переключиться с `pat_A` на `pat_B` (у которого в `SharedPreferences` уже сохранён анамнез) — в `VaultService` данные `pat_B` загружаются, но локальные поля `AnamnesisScreen` остаются сброшенными.
- **Последствия:** Невосстановление UI-состояния гостевого черновика при переоткрытии экрана и невосстановление UI-состояния второго пациента при переключении без пересоздания `AnamnesisScreen`.
- **Предлагаемое исправление:**
  1. Добавить флаг `bool _initialRestoreDone = false;` и при первом вызове `didChangeDependencies()` всегда выполнять `_restoreLocalStateFromVault(vault)` (включая гостевой режим, если `vault.getSystemContext('global').isNotEmpty`).
  2. При смене `_lastSeenPatientId != vault.activePatientId || _lastSeenEpoch != vault.sessionEpoch` выполнять `_resetLocalState(); _restoreLocalStateFromVault(vault);`, а в `VaultService._loadPatientVaultAsync` инкрементировать `_sessionEpoch` (либо выставлять флаг завершения загрузки), чтобы `didChangeDependencies` вызвал `_restoreLocalStateFromVault(vault)` после фактической загрузки данных из `SharedPreferences`.
- **Статус доказательства:** Подтверждено кодом (`lib/anamnesis_screen.dart:173-188`, `lib/services/vault_service.dart:377-426`).

---

#### Проблема M-8
- **Приоритет:** Medium
- **Карточка:** Карточки 5, 7, 9 и локальная деривация `VaultService`
- **Файл и строки:** [lib/services/vault_service.dart](file:///E:/aegis_health/lib/services/vault_service.dart#L1383-L1397), [lib/services/vault_service.dart](file:///E:/aegis_health/lib/services/vault_service.dart#L1838-L2108)
- **Суть проблемы:**
  1. В `VaultService.buildAnamnesisAnswers()` (строки `1388–1390`) поля `userTextSpecFactors`, `userTextDietHabits` и `userTextGiSignals` читаются только из legacy-ключей `global['text_spec_factors']`, `global['text_diet_habits']`, `global['text_gi_signals']` без приоритетной проверки `global['user_text_*']` (в отличие от всех остальных полей в строках `1383–1387` и `1391–1396`, где используется `global['user_text_*'] ?? global['text_*']`).
  2. В `VaultService._applySingleAnamnesisDerivation()` (строки `1838–2108`) при сохранении положительных ответов выставляются локальные маркеры в `_systemFolders` (например, `vessel_nicotine_status`, `toxic_alcohol_load`, `chronic_history`, `critical_infections`, `heredity_risks`, `active_medications`), но при последующем изменении ответа пользователем на отрицательный (`Non-user`, `Non-drinker`, `No diagnosed chronic conditions`, `No known significant family history`, `No, I don't take any medications...`) старые ключи в клиентском `_systemFolders` не удаляются (при этом серверный M1 контекст `build_anamnesis_m1_context_from_answers` формируется с нуля по `AnamnesisQuestionnaireAnswers` и данной проблеме не подвержен).
- **Сценарий воспроизведения:** Передать в `global` ключ `'user_text_spec_factors'` без `'text_spec_factors'` — `buildAnamnesisAnswers()` вернёт `userTextSpecFactors == null`.
- **Последствия:** Неконсистентность чтения `user_text_*` ключей и устаревшие маркеры в локальном клиентском `_systemFolders` при редактировании ответов в рамках одной сессии.
- **Предлагаемое исправление:**
  1. В `lib/services/vault_service.dart:1388–1390` использовать `(global['user_text_spec_factors'] ?? global['text_spec_factors'])`, `(global['user_text_diet_habits'] ?? global['text_diet_habits'])`, `(global['user_text_gi_signals'] ?? global['text_gi_signals'])`.
  2. В `saveAnamnesis` / `removeAnamnesisKeys` пересчитывать `_systemFolders` через `_rebuildDerivedAnamnesisState()` вместо только аддитивного добавления.
- **Статус доказательства:** Подтверждено кодом (`lib/services/vault_service.dart:1388-1390, 1838-2108`).

---

#### Проблемы низкого приоритета (L-1 .. L-4)

- **L-1 (Low — Финальный экран `_stepFinal`, прогресс-бар):**
  - **Файл и строки:** [lib/anamnesis_screen.dart](file:///E:/aegis_health/lib/anamnesis_screen.dart#L18), [lib/anamnesis_screen.dart](file:///E:/aegis_health/lib/anamnesis_screen.dart#L708-L754)
  - **Суть:** Константа `_totalPages = 17`, тогда как в `PageView` 18 страниц (индексы `0..17`). На индексе 17 (`_stepFinal`) `progress = 17 / 16 = 1.0625`. Рекомендуется использовать `.clamp(0.0, 1.0)` или `_totalPages = 18`.
  - **Статус доказательства:** Подтверждено кодом.

- **L-2 (Low — Единообразие текстов кнопок навигации и редактирования):**
  - **Файл и строки:** [lib/anamnesis_screen.dart](file:///E:/aegis_health/lib/anamnesis_screen.dart#L1107) (`"Next"`), [lib/anamnesis_screen.dart](file:///E:/aegis_health/lib/anamnesis_screen.dart#L1538) (`"Далее ➡️"`), [lib/anamnesis_screen.dart](file:///E:/aegis_health/lib/anamnesis_screen.dart#L1824) (`"Continue ➡️"`), [lib/anamnesis_screen.dart](file:///E:/aegis_health/lib/anamnesis_screen.dart#L2015) (`"Next ➡️"`), [lib/anamnesis_screen.dart](file:///E:/aegis_health/lib/anamnesis_screen.dart#L2143) (`"Edit / Expand"`), [lib/anamnesis_screen.dart](file:///E:/aegis_health/lib/anamnesis_screen.dart#L2282) (`"[Change ✏️]"`), [lib/anamnesis_screen.dart](file:///E:/aegis_health/lib/anamnesis_screen.dart#L2594) (`"Edit coffee details"`).
  - **Суть:** Разные подписи кнопок перехода и редактирования свёрнутых карточек. Рекомендуется унифицировать кнопку перехода карточек 1–15 как `"Next ➡️"`, карточки 16 — `"Finish 🚀"`, а кнопку редактирования свёрнутых карточек — `"[Edit ✏️]"`.
  - **Статус доказательства:** Подтверждено кодом.

- **L-3 (Low — Карточка 12, двойной выбор статуса для онкологии):**
  - **Файл и строки:** [lib/anamnesis_screen.dart](file:///E:/aegis_health/lib/anamnesis_screen.dart#L3531-L3580)
  - **Суть:** Для категории `'Cancer or oncological history'` пользователь сначала выбирает `onco_status` (`"Active treatment"` / `"Remission"`), и тут же ниже обязан выбрать общий `current_status` (`"Ongoing / Still present"`, `"Controlled or stable"`, `"In remission (if applicable)"`, `"Resolved in the past"`), причём вUI нет защиты от выбора `onco_status = "Active treatment"` одновременно с `current_status = "In remission (if applicable)"`.
  - **Статус доказательства:** Подтверждено кодом.

- **L-4 (Low — Карточка 14, варианты давности отрицательных тестов на инфекции):**
  - **Файл и строки:** [lib/anamnesis_screen.dart](file:///E:/aegis_health/lib/anamnesis_screen.dart#L118-L125)
  - **Суть:** В `_infectiousLastTestedOptions` доступны только `"Within the last 3 months"`, `"About 6 months ago"`, `"About 1 year ago"`, `"Not sure / Don't remember"`. Если пациент сдавал отрицательные тесты более 1 года назад и точно помнит это, подходящего варианта нет.
  - **Статус доказательства:** Подтверждено кодом.

---

## C. Проверка хранения и контрактов (`Flutter UI → VaultService → DTO → Backend Pydantic`)

### 1. Сквозная сверка контракта по всем 16 карточкам

| Карточка | Ключи в `VaultService` (`global`) | Поля `AnamnesisQuestionnaireAnswers` (Dart DTO & Python Pydantic) | Статус синхронизации |
| :--- | :--- | :--- | :--- |
| **0. Age Gate** | `age_confirmation` | `ageConfirmation` / `age_confirmation` | **OK** |
| **1. Bio Profile** | `gender`, `age`, `age_confirmed_unusual`, `pregnancy_status`, `pregnancy_trimester`, `pregnancy_weeks`, `breastfeeding_status` | Одноимённые поля (`camelCase` ↔ `snake_case`) | **OK** (с учётом M-1/M-3) |
| **2. Dimensions** | `height`, `weight`, `height_confirmed_unusual`, `weight_confirmed_unusual`, `weight_change_status`, `weight_change_amount`, `weight_change_intentional` | Одноимённые поля (`camelCase` ↔ `snake_case`) | **OK** (с учётом M-3) |
| **3. Nicotine** | `nicotine_status`, `nicotine_products`, `nicotine_product_tenure`, `nicotine_current_frequency`, `nicotine_quantities`, `nicotine_quit_when`, `nicotine_freq` | Одноимённые поля (`camelCase` ↔ `snake_case`) | **Требует исправления H-2** |
| **4. Alcohol** | `alcohol_status`, `alcohol_types`, `alcohol_volumes`, `alcohol_comment`, `alcohol_drinks`, `user_text_alcohol` | Одноимённые поля (`camelCase` ↔ `snake_case`) | **Требует исправления H-2** |
| **5. SpecFactors** | `spec_factors`, `substance_types`, `substance_last_use`, `substance_notes`, `user_text_spec_factors`, `text_spec_factors` | Одноимённые поля (`camelCase` ↔ `snake_case`) | **Требует исправления M-4, M-8** |
| **6. Sleep** | `sleep_quality`, `sleep_duration`, `sleep_symptoms`, `sleep_schedule`, `sleep_schedule_notes`, `user_text_sleep`, `text_sleep` | Одноимённые поля (`camelCase` ↔ `snake_case`) | **OK** |
| **7. Diet** | `diet_types`, `diet_custom_details`, `diet_habits`, `diet_meals_per_day`, `diet_snacks_per_day`, `diet_staples`, `diet_occasional_foods`, `diet_food_frequencies`, `diet_excluded_foods_text`, `diet_day_description`, `user_text_diet_types`, `user_text_diet_habits` | Одноимённые поля (`camelCase` ↔ `snake_case`) | **OK** (с учётом M-5, M-8) |
| **8. Drinks** | `drinks_total_volume`, `drinks_types`, `drinks_coffee_types`, `drinks_coffee_frequency`, `drinks_caffeine_frequency`, `drinks_day_description` | Одноимённые поля (`camelCase` ↔ `snake_case`) | **Требует исправления M-2** |
| **9. GI Health** | `gi_signals`, `gi_symptoms`, `gi_symptom_details`, `user_text_gi_signals`, `text_gi_signals` | Одноимённые поля (`camelCase` ↔ `snake_case`) | **OK** (с учётом M-8) |
| **10. Work** | `work_type`, `work_occupation`, `work_schedule`, `work_stress`, `user_text_work`, `text_work` | Одноимённые поля (`camelCase` ↔ `snake_case`) | **OK** |
| **11. Activity** | `sport_level`, `activity_days_per_week`, `activity_duration_minutes`, `activity_types`, `activity_other_details`, `user_text_sport`, `text_sport` | Одноимённые поля (`camelCase` ↔ `snake_case`) | **OK** |
| **12. Chronic** | `chronic_diseases`, `onco_status`, `chronic_categories`, `chronic_condition_details`, `user_text_chronic_diseases`, `text_chronic_diseases` | Одноимённые поля (`camelCase` ↔ `snake_case`) | **OK** (с учётом M-6 `_clampSummary`) |
| **13. Recent** | `recent_illness`, `recent_events`, `recent_event_details`, `user_text_recent_illness`, `text_recent_illness` | Одноимённые поля (`camelCase` ↔ `snake_case`) | **OK** (с учётом M-6 `_clampSummary`) |
| **14. Infections** | `critical_infections`, `infectious_history_status`, `infectious_diagnosed`, `infectious_tested_negative`, `infectious_last_tested`, `user_text_critical_infections`, `text_critical_infections` | Одноимённые поля (`camelCase` ↔ `snake_case`) | **OK** |
| **15. Family** | `heredity_risks`, `family_history_status`, `family_history_relatives`, `family_history_details`, `user_text_heredity_risks`, `text_heredity_risks` | Одноимённые поля + `FamilyHistoryRelativeDetail` (`consilium.py:127`) | **OK** |
| **16. Medications**| `medications`, `medications_status`, `medication_categories`, `medication_details`, `user_text_medications`, `text_medications` | Одноимённые поля (`camelCase` ↔ `snake_case`) | **OK** |

### 2. Проверка изоляции пациентов и гостевого черновика
- В `VaultService` ([lib/services/vault_service.dart](file:///E:/aegis_health/lib/services/vault_service.dart#L377-L445)) данные каждого авторизованного пациента хранятся под изолированным префиксом ключа `SharedPreferences` (`aegis_vault_v1_pat_<patientId>_...`), а гостевой черновик — под `aegis_vault_v1_guest_draft_...`.
- При вызове `setActivePatientId(nextId)` синхронно инкрементируется `_sessionEpoch` и очищаются in-memory структуры предыдущего пациента (`_clearInMemoryVaultStructures()`), что исключает утечку данных Пациента А в Пациента Б.
- Гостевой черновик переносится в профиль пациента только при явном вызове `associateGuestDraftToActivePatient()`, после чего гостевые ключи очищаются.
- Единственный нюанс жизненного цикла UI при переключении пациента и холодном старте гостя зафиксирован в **M-7**.

---

## D. Тестовое покрытие

### 1. Текущее покрытие по файлам (всего: 41 Flutter тест + 71 Backend тест)

#### Flutter Widget & Integration Tests (`test/`)
1. [test/anamnesis_cards_v2_test.dart](file:///E:/aegis_health/test/anamnesis_cards_v2_test.dart) — **7 тестов** (Stage 1: карточки 1–5, границы возраста/роста/веса, беременность и триместры, никотин, алкоголь, вещества, DTO сериализация).
2. [test/anamnesis_stage2_test.dart](file:///E:/aegis_health/test/anamnesis_stage2_test.dart) — **6 тестов** (Stage 2: карточки 6–8, сон, диета, блокировка конфликтующих продуктов для веганов/вегетарианцев/пескетарианцев, напитки и кофе, восстановление и переключение пациента).
3. [test/anamnesis_stage3_test.dart](file:///E:/aegis_health/test/anamnesis_stage3_test.dart) — **5 тестов** (Stage 3: карточки 9–11, матрёшка симптомов ЖКТ, работа и стресс, физическая активность и сохранение заметок при выборе `"Almost no physical activity"`).
4. [test/anamnesis_stage4_test.dart](file:///E:/aegis_health/test/anamnesis_stage4_test.dart) — **5 тестов** (Stage 4: карточки 12–14, хронические заболевания и `onco_status`, недавние события с `timeframe_code`, инфекционный анамнез, изоляция пациентов и ассоциация гостевого черновика).
5. [test/anamnesis_stage5_test.dart](file:///E:/aegis_health/test/anamnesis_stage5_test.dart) — **5 тестов** (Stage 5: карточки 15–16, двухуровневая матрёшка семейного анамнеза, переключение `Yes → No`, восстановление и удаление черновиков лекарств, холодный рестарт и изоляция `pat_stage5_A` / `pat_stage5_B`).
6. [test/anamnesis_screen_integration_test.dart](file:///E:/aegis_health/test/anamnesis_screen_integration_test.dart) — **13 тестов** (сквозное прохождение карточек 0–16, отправка `submitAnamnesis` на сервер, предзаполнение из Vault).

#### Backend Pydantic & M1 Tests (`backend/tests/`)
1. [backend/tests/test_anamnesis_cards_v2.py](file:///E:/aegis_health/backend/tests/test_anamnesis_cards_v2.py) — **8 тестов** (Stage 1 + извлечение M1 контекста).
2. [backend/tests/test_anamnesis_stage2.py](file:///E:/aegis_health/backend/tests/test_anamnesis_stage2.py) — **8 тестов** (Stage 2 + обратная совместимость).
3. [backend/tests/test_anamnesis_stage3.py](file:///E:/aegis_health/backend/tests/test_anamnesis_stage3.py) — **12 тестов** (Stage 3 + обратная совместимость).
4. [backend/tests/test_anamnesis_stage4.py](file:///E:/aegis_health/backend/tests/test_anamnesis_stage4.py) — **13 тестов** (Stage 4 + обратная совместимость).
5. [backend/tests/test_anamnesis_stage5.py](file:///E:/aegis_health/backend/tests/test_anamnesis_stage5.py) — **16 тестов** (Stage 5 + обратная совместимость).
6. Сопутствующие контрактные тесты в `backend/tests/test_consilium_api.py` и `backend/tests/test_stage5_intake_orchestration.py` — **14 тестов**.

### 2. Какие сценарии НЕ покрыты текущими тестами (причина пропуска багов H-1, H-2, M-2..M-7)
1. **Навигация «Вперёд → Назад (`Icons.arrow_back`) → Вперёд» на карточках 1–4:** Все существующие виджет-тесты либо двигаются только вперёд, либо переключают страницы напрямую через `pageView.controller!.jumpToPage(N)`. Ни один тест не нажимал кнопку `Back` после заполнения карточек 1–4, поэтому очистка `hController`/`wController`/`_customInputController` в `_nextPage()` (**H-1**) и перескок `_previousPage()` через подшаг 0 в карточках 3–4 (**H-2**) оставались незамеченными.
2. **Холодное восстановление (`_restoreLocalStateFromVault`) карточек 3 и 4:** Тесты проверяли холодное восстановление для карточек 6–8, 12–14 и 15–16, но не проверяли повторное открытие карточек 3 и 4 после сохранения статуса `'Regular'`/`'Former'`/`'1-2/week'` (**H-2**).
3. **Смена статуса `Regular → Former` на Карточке 3 с последующей валидацией в Pydantic:** В `test_anamnesis_cards_v2.py` есть тест на отказ Pydantic при одновременной передаче `nicotine_status="Quit"` и `nicotine_current_frequency="Daily"`, но во Flutter-тестах не проверялся сценарий переключения `Regular → Former` в UI (**H-2**).
4. **Снятие галочки с чая/энергетиков на Карточке 8 после выбора частоты кофеина (**M-2**).**
5. **Повторное открытие `AnamnesisScreen` в гостевом режиме (`activePatientId == null`) без предварительного `setActivePatientId` (**M-7**).**

---

## E. Английский интерфейс (аудит локализации)

### 1. Сводка по карточкам
- **Полностью на английском языке (PASS):**
  - Шаг 0 (`_stepAgeGate`)
  - Карточки 6–16 (`_stepSleep`, `_stepDiet`, `_stepDrinks`, `_stepGISignals`, `_stepWorkMode`, `_stepSport`, `_stepChronic`, `_stepRecentEvents`, `_stepSafeSpace`, `_stepHeredity`, `_stepMeds`)
  - Шаг 17 (`_stepFinal`)
- **Содержат русский текст в UI, опциях и сообщениях об ошибках (FAIL — требуют перевода):**
  - Общий AppBar `AnamnesisScreen`: строка `727` (`tooltip: 'Назад'`)
  - Карточка 1 (`_stepBioProfile`, строки `933–1085`)
  - Карточка 2 (`_stepDimensions`, строки `1152–1306`)
  - Карточка 3 (`_stepNicotine`, строки `1364–1555`)
  - Карточка 4 (`_stepAlcohol`, строки `1604–1718`)
  - Карточка 5 (`_stepSpecFactors`, строки `1745–1827`)

### 2. Полная таблица непереведённых элементов в `lib/anamnesis_screen.dart`

| Карточка / Место | Строки в `lib/anamnesis_screen.dart` | Текущий русский текст |
| :--- | :--- | :--- |
| **Навигация (Back)** | `727` | `tooltip: 'Назад'` |
| **Карточка 1 (Bio Profile)** | `933`, `946`, `1047` | `"Вы указали возраст: $parsedAge лет. Пожалуйста, проверьте правильность ввода."`, `"Подтвердить правильность ввода"`, `"Пожалуйста, подтвердите правильность указанного возраста"` |
| **Карточка 1 (Bio Profile)** | `955–1005`, `1053–1078` | `"Беременны ли вы сейчас?"`, `"Да"`, `"Возможно, но не подтверждено"`, `"Нет"`, `"Предпочитаю не отвечать"`, `"Триместр беременности:"`, `"1-й триместр (до 13 нед. 6 дн.)"`, `"2-й триместр (14–27 нед. 6 дн.)"`, `"3-й триместр (28 нед. и более)"`, `"Не знаю / не уверена"`, `"Срок беременности (в неделях, 1–45, необязательно)"`, `"Например: 12"`, русские тексты ошибок валидации триместра и недель |
| **Карточка 1 (Bio Profile)** | `1009–1019`, `1082` | `"Кормите ли вы грудью?"`, `"Да"`, `"Нет"`, `"Предпочитаю не отвечать"`, `"Пожалуйста, ответьте на вопрос о грудном вскармливании"` |
| **Карточка 2 (Dimensions)** | `1152`, `1163`, `1193`, `1204`, `1284`, `1289` | Предупреждения и кнопки подтверждения нестандартного роста и веса (`"Вы указали рост..."`, `"Вы указали вес..."`, `"Подтвердить правильность ввода"`) |
| **Карточка 2 (Dimensions)** | `1212–1260`, `1297–1306` | `"Изменился ли ваш вес за последние 3–6 месяцев?"`, `"Вес примерно одинаковый"`, `"Набрал(а) вес"`, `"Потерял(а) вес"`, `"Не знаю / не отслеживаю"`, `"На сколько килограммов? (например, 5)"`, `"Это изменение было намеренным?"`, `"Да"`, `"Нет"`, `"Не уверен(а)"`, русские тексты ошибок |
| **Карточка 3 (Nicotine)** | `1364–1555` | 5 вариантов статуса (`"1️⃣ Никогда не употреблял(а)"` .. `"5️⃣ Предпочитаю не отвечать"`), список `productOptions`, `tenureOptions`, `freqOptions`, `quitOptions`, подвопросы по количеству сигарет/стиков/вейпа/снюса/сигар/кальяна, кнопка `"Далее ➡️"`, русские ошибки валидации |
| **Карточка 4 (Alcohol)** | `1604–1718` | 6 вариантов статуса (`"1️⃣ Никогда не употреблял(а)"` .. `"6️⃣ 3 и более раз в неделю"`), список напитков `alcoholTypesList`, объёмы `volumePresets`, поле комментария `"Дополнительный комментарий (необязательно)"`, кнопка `"Далее ➡️"`, русские ошибки валидации |
| **Карточка 5 (SpecFactors)** | `1745–1827` | Заголовок `"Дополнительные факторы здоровья 🛡️"`, подзаголовок, список `substanceOptions`, `lastUseOptions`, `"🚫 Не употребляю"`, `"🤐 Предпочитаю не отвечать"`, `"Когда это было в последний раз?"`, `"Дополнительная информация (необязательно)"`, русская ошибка валидации |

---

## F. Отложенные вопросы M1 (**«Отложено до аудита M1»**)

В соответствии с границами задачи код `backend/app/services/consilium_prompts.py` (`build_anamnesis_m1_context_from_answers`) не изменялся. Ниже зафиксированы расхождения между новой структурой карточек 1–16 и текущим построителем контекста M1, которые необходимо учесть на следующем этапе (**Отложено до аудита M1**):

1. **Новые структурированные поля Stages 2–5 пока не включены в `core_data_log` и `system_folders` (`consilium_prompts.py:313–365`):**
   - В `core_data_log` сейчас записываются только базовые поля и поля Stage 1 (`pregnancy_*`, `weight_change_*`, `nicotine_*`, `alcohol_*`, `substance_*`).
   - Структурированные поля **Stage 2** (`sleep_duration`, `sleep_symptoms`, `sleep_schedule`, `sleep_schedule_notes`, `diet_custom_details`, `diet_meals_per_day`, `diet_snacks_per_day`, `diet_staples`, `diet_occasional_foods`, `diet_food_frequencies`, `diet_excluded_foods_text`, `diet_day_description`, `drinks_total_volume`, `drinks_types`, `drinks_coffee_types`, `drinks_coffee_frequency`, `drinks_caffeine_frequency`, `drinks_day_description`), **Stage 3** (`gi_symptoms`, `gi_symptom_details`, `work_occupation`, `work_stress`, `activity_days_per_week`, `activity_duration_minutes`, `activity_types`, `activity_other_details`), **Stage 4** (`chronic_categories`, `chronic_condition_details`, `recent_events`, `recent_event_details`, `infectious_history_status`, `infectious_diagnosed`, `infectious_tested_negative`, `infectious_last_tested`) и **Stage 5** (`family_history_status`, `family_history_relatives`, `family_history_details`, `medications_status`, `medication_categories`, `medication_details`) проходят Pydantic-валидацию, но ещё не пробрасываются в `core_data_log` (сейчас M1 видит для карточек 9, 12, 13, 14, 15, 16 только их текстовые сводки `gi_signals`, `chronic_diseases`, `recent_illness`, `critical_infections`, `heredity_risks`, `medications`).
2. **Ложное срабатывание триггера ЖКТ `symptoms_motility_sibr` при ответе `"None of the above"` на Карточке 9 (`consilium_prompts.py:221–225`):**
   - На Карточке 9 выбор `"None of the above"` сохраняет `gi_signals = "None"` (или `"None of the above"`).
   - В `consilium_prompts.py:221` условие проверяет:
     ```python
     if gi_signals and not any(s in gi_signals for s in ("gi_ok", "no discomfort", "нет дискомфорта", "норма")):
     ```
     Строки `"none"` в списке исключений нет, поэтому у здорового пациента с `gi_signals = "None"` в `system_folders["gi"]["symptoms_motility_sibr"]` ошибочно записывается `"None"`.
3. **Поиск базовых продуктов (`meat`, `fish`, `vegetables`, `milk`) в `diet_types` вместо `diet_staples` (`consilium_prompts.py:183–199`):**
   - После переработки Карточки 7 в `diet_types` хранится тип диеты (`"Mixed / Balanced diet"`, `"Vegetarian"`, `"Vegan"`, и т.д.), а конкретные группы продуктов (`"Meat and poultry"`, `"Fish and seafood"`, `"Vegetables and greens"`, `"Dairy products"`) перенесены в список `answers.diet_staples`. В `consilium_prompts.py:183–199` поиск всё ещё выполняется только по строке `answers.diet_types`, из-за чего при выборе `"Mixed / Balanced diet"` срабатывают ветки дефицита B12/железа (`diet_b12_iron_protein_source`), омега-3 (`diet_omega3_vitamin_d`) и клетчатки (`diet_fiber_microbiome`), даже если пациент отметил мясо, рыбу и овощи в `diet_staples`.
4. **Расширение маппинга категорий Карточек 12, 14, 15 и 16 по органным полкам `system_folders` (`consilium_prompts.py:242–311`):**
   - В Карточке 14 при выборе `"I have had negative test results"` сводная строка `critical_infections` равна `"Negative test results reported"`. В `consilium_prompts.py:276` условие `if not any(s in infections for s in ("inf_none", "no chronic", "нет хронических", "none")):` не включает `"negative test"`, `"not sure"`, `"declined"`, поэтому отрицательные тесты или отказ от ответа могут попадать в `system_folders["immune"]["critical_infections"]`.
   - В Карточке 15 новые категории (`"Kidney conditions"`, `"Neurological conditions"`, `"Mental health conditions"`, `"Autoimmune conditions"`, `"Blood clotting disorders"`, `"Genetic / hereditary conditions"`) и степень родства (`1-я линия` vs `2-я линия`) пока не распределяются по соответствующим органным папкам (`kidneys`, `nervous`, `immune`, `hematology`).

---

## G. План точечных исправлений

Ниже представлен упорядоченный план минимальных, безопасных исправлений (без переработки архитектуры анамнеза), разбитый на 4 логических шага:

### Шаг 1. Устранение дефектов потери ввода и навигации (High: H-1, H-2)
1. **Карточки 1 и 2 (`lib/anamnesis_screen.dart:5224–5226`):**
   - Убрать `_customInputController.clear(); hController.clear(); wController.clear();` из метода `_nextPage()`.
2. **Карточки 3 и 4 (`lib/anamnesis_screen.dart:237–261, 645–663, 1372–1578, 1612–1738`):**
   - В `_previousPage()` возвращать пользователя с `_nicotineSubStep == 1` на `_nicotineSubStep = 0` (и с `_alcoholSubStep == 1` на `_alcoholSubStep = 0`) без переключения страницы `PageView`.
   - При смене статуса курения на `'Former'` очищать `_nicotineCurrentFrequency`, `_nicotineQuantities` и удалять ключи `['nicotine_current_frequency', 'nicotine_quantities', 'nicotine_freq']` из Vault; при смене на `'Occasional'`/`'Regular'` очищать `_nicotineQuitWhen` и удалять `['nicotine_quit_when']`.
   - Сохранять и корректно восстанавливать в `_restoreLocalStateFromVault` выбранную градацию частоты курения (`Occasional` vs `Regular`) и алкоголя (`1-3/month`, `1-2/week`, `>=3/week`).

### Шаг 2. Устранение дефектов валидации, очистки ключей и восстановления состояния (Medium: M-2..M-8, Low: L-1..L-4)
1. **Карточка 8 (`lib/anamnesis_screen.dart:2508–2525, 2696–2700`):**
   - Сбрасывать `_drinksCaffeineFrequency = null` при снятии выбора с `'Black / Green tea'` и `'Energy drinks'`, и сохранять `drinks_caffeine_frequency` только при `hasCaffeinatedTeaOrEnergy == true`.
2. **Карточка 2 и Карточка 1 (`lib/anamnesis_screen.dart:910–951, 1129–1326`):**
   - Сделать выбор `_weightChangeStatus` обязательным на Карточке 2 и очищать `weight_change_amount` / `weight_change_intentional` во всех ветках, кроме `'Gained'`/`'Lost'`.
   - Сбрасывать `_ageConfirmedUnusual`, `_heightConfirmedUnusual`, `_weightConfirmedUnusual` в `onChanged` при изменении текста и сохранять их в Vault только если итоговое значение действительно нестандартное.
3. **Карточка 5 (`lib/anamnesis_screen.dart:1824–1848`) и Карточка 7 (`lib/anamnesis_screen.dart:2118–2140, 2408–2413`):**
   - Проверять обязательность `_substanceLastUse` при выбранном веществе на Карточке 5.
   - Проверять непустой текст `_dietExcludedController` при включённом чекбоксе `_hasExcludedFoods` на Карточке 7.
4. **Карточки 7, 9, 12, 13 (`lib/anamnesis_screen.dart`):**
   - Защитить свёрнутые заполненные категории от случайного удаления при клике по заголовку (по аналогии с `_buildMatryoshkaCardHeader` в карточках 15–16).
   - Добавить `_clampSummary(..., 1024)` для `chronic_diseases` (Карточка 12) и `_clampSummary(..., 512)` для `recent_illness` (Карточка 13).
5. **Восстановление состояния (`lib/anamnesis_screen.dart:173–188`, `lib/services/vault_service.dart:377–426, 1388–1390`):**
   - Обеспечить вызов `_restoreLocalStateFromVault` для гостевого черновика (`activePatientId == null`) и после завершения асинхронной загрузки профиля при переключении пациента.
   - Добавить приоритетное чтение `global['user_text_*']` для `userTextSpecFactors`, `userTextDietHabits`, `userTextGiSignals` в `VaultService.buildAnamnesisAnswers()`.
6. **Мелкие улучшения UI (`L-1..L-4`):**
   - Ограничить `progress.clamp(0.0, 1.0)` для `LinearProgressIndicator`, унифицировать подписи кнопок перехода и добавить `"More than 1 year ago"` в `_infectiousLastTestedOptions` на Карточке 14.

### Шаг 3. Полная англоязычная локализация карточек 1–5 (Medium: M-1)
1. Перевести `tooltip: 'Back'` и все вопросы, варианты ответов, плейсхолдеры и сообщения об ошибках в карточках 1–5 (`_stepBioProfile`, `_stepDimensions`, `_stepNicotine`, `_stepAlcohol`, `_stepSpecFactors`) на английский язык.
2. Убедиться, что валидаторы Stage 1 в `backend/app/schemas/consilium.py` принимают английские значения с сохранением обратной совместимости для уже существующих записей.

### Шаг 4. Расширение регрессионных тестов
1. Добавить виджет-тесты на навигацию кнопкой `Back` через карточки 1–4 (проверка сохранности возраста/роста/веса и возврата с подшага 1 на подшаг 0).
2. Добавить тест на переключение `Regular → Former` в Карточке 3 и снятие галочки с чая в Карточке 8 с проверкой валидации DTO в `AnamnesisQuestionnaireAnswers`.
3. Обновить строковые проверки Stage 1 в `test/anamnesis_cards_v2_test.dart` и `test/anamnesis_screen_integration_test.dart` под англоязычный интерфейс карточек 1–5.
