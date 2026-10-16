# Project Aegis — Приватный локальный Backend

Приватный сервер клинической аналитики и агрегации витальных данных Project Aegis для локального компьютера под управлением Windows.

---

## 1. Назначение и Архитектура

Сервер обеспечивает:
- **Изоляцию клинических слоев ИИ:** Внутренние рассуждения моделей (`internal_analysis`, `system_training_shelf`, `pro_training_shelf`, `athena_handoff`) хранятся в защищённой локальной базе SQLite (`aegis_private_vault.db`) и не передаются в мобильное приложение.
- **Безопасность API-ключа:** Ключ Google Gemini хранится исключительно на сервере в `.env` / Windows DPAPI.
- **Хранение медицинских циклов:** Разделение глобального медицинского цикла (`MedicalCycle`, `cycle_id`) и вложенных недельных спринтов (`WeeklySprint`, `sprint_id`, `sprint_number`). Завершение 7-го дня спринта не закрывает медицинский цикл.
- **Интеграция с HealthDataProvider:** Прием и хранение замеров с разделением времени измерения (`measured_at`) и времени получения (`ingested_at`), а также типизацией источников (`watch_sensor`, `manual_input`, `test_chip`, `simulated`, `unknown`).

---

## 2. Быстрый старт на Windows (PowerShell)

### Шаг 1. Переход в директорию бэкенда
```powershell
cd E:\aegis_health\backend
```

### Шаг 2. Создание и активация виртуального окружения Python
```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
```
*(Если выполнение скриптов ограничено, выполните один раз: `Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser`)*

### Шаг 3. Установка зависимостей
```powershell
python -m pip install --upgrade pip
pip install -r requirements.txt
```

### Шаг 4. Настройка файла окружения (.env)
Скопируйте пример конфигурации:
```powershell
Copy-Item .env.example .env
```
В файле `.env` при необходимости укажите ваш `GEMINI_API_KEY` (он будет использоваться только сервером).

### Шаг 5. Применение миграций базы данных (Alembic)
```powershell
alembic upgrade head
```

### Шаг 6. Запуск автоматических тестов
```powershell
pytest -v
```

### Шаг 7. Запуск сервера для локальной разработки
```powershell
uvicorn app.main:app --host 127.0.0.1 --port 8080 --reload
```
Документация Swagger UI будет доступна в браузере по адресу:
`http://127.0.0.1:8080/docs`

Проверка статуса сервиса:
`http://127.0.0.1:8080/health`

---

## 3. Безопасность мобильного подключения (LAN Security Profile)

По умолчанию сервер слушает строго локальный адрес `127.0.0.1` (loopback) и недоступен из сети.

Для безопасного подключения смартфона по домашней сети Wi-Fi:
1. **Изоляция:** Компьютер и телефон должны находиться в одной доверенной домашней подсети.
2. **Аутентификация:** Мобильное приложение передает заголовок `Authorization: Bearer <AEGIS_API_SECRET>`.
3. **Шифрование:** В локальной сети рекомендуется использовать обратный прокси с HTTPS (например, Caddy с локальным сертификатом):
   ```powershell
   caddy reverse-proxy --from :8443 --to 127.0.0.1:8080
   ```
4. **Брандмауэр Windows:** Разрешайте входящие подключения к порту только из доверенного диапазона локальных адресов (`192.168.x.x`), никогда не выставляя порт в публичный интернет.

---

## 4. Структура проекта

```
backend/
├── .env.example              # Пример конфигурации окружения
├── alembic.ini               # Конфигурация миграций Alembic
├── requirements.txt          # Зависимости Python
├── README.md                 # Инструкция запуска
├── alembic/                  # Миграции схемы БД
│   ├── env.py
│   └── versions/
│       └── 0001_initial_schema.py
├── app/
│   ├── config.py             # Pydantic v2 Settings
│   ├── database.py           # SQLAlchemy Engine и сессии
│   ├── main.py               # FastAPI приложение и lifespan
│   ├── models/               # Реляционные сущности БД
│   │   ├── base.py
│   │   ├── medical_cycle.py  # MedicalCycle (макроцикл)
│   │   ├── weekly_sprint.py  # WeeklySprint (недельный спринт Афины)
│   │   ├── workout_session.py# WorkoutSession (сессии тренировок)
│   │   ├── health_measurement.py # HealthMeasurement (биометрия)
│   │   └── model_report.py   # ModelReport (server_only vs client_facing)
│   ├── schemas/              # Pydantic схемы валидации
│   │   └── health.py
│   └── routers/              # API эндпоинты
│       └── health.py         # GET /health
└── tests/                    # Набор автотестов pytest
    ├── conftest.py
    ├── test_health.py
    ├── test_models.py
    ├── test_measurements.py
    └── test_security.py
```
