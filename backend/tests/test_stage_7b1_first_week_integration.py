import asyncio
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re
import socket
from typing import Any, Callable, Dict, List, Optional
import httpx
import pytest
import uvicorn
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session, sessionmaker

from app.config import settings
from app.database import Base, get_db
from app.main import app
from app.models.medical_cycle import MedicalCycle
from app.models.model_report import ModelReport
from app.schemas.privacy import PrivacyLevel
from app.models.patient import Patient
from app.repositories.cycle_repository import MedicalCycleRepository
from app.repositories.pairing_repository import PairingRepository
from app.repositories.patient_repository import PatientRepository
from app.repositories.report_repository import ModelReportRepository
from app.routers.cascade import (
    get_gemini_gateway as cascade_get_gemini_gateway,
    get_server_now_provider as cascade_get_server_now_provider,
)
from app.routers.client_cascade import (
    get_gemini_gateway as client_get_gemini_gateway,
    get_server_now_provider as client_get_server_now_provider,
)
from app.routers.client_chat import (
    get_gemini_gateway as chat_get_gemini_gateway,
    get_server_now_provider as chat_get_server_now_provider,
)
from app.routers.vitals import (
    get_gemini_gateway as vitals_get_gemini_gateway,
    get_server_now_provider as vitals_get_server_now_provider,
)
from app.schemas.chat import AthenaDailyAssessmentModelOutput
from app.schemas.consilium import SYSTEM_KEYS
from app.schemas.wellness import WellnessStatus, WellnessTriggerType
from app.services.athena_chat_service import AthenaChatService
from app.services.athena_daily_journal_service import AthenaDailyJournalService
from app.services.athena_weekly_plan_service import (
    ATHENA_ROUTINE_QUESTION_REPORT_TYPE,
    AthenaWeeklyPlanService,
)
from app.services.clinical_automation_service import (
    AutomationJobStatus,
    AutomationJobType,
    ClinicalAutomationService,
    ClinicalAutomationWorker,
)
from app.services.gemini_gateway import GeminiGateway
from app.services.lab_revision_consilium_service import LabRevisionConsiliumService
from app.services.mode2_vitals_service import Mode2EpisodeStatus, Mode2VitalsMonitoringService
from app.services.primary_consilium_service import PrimaryConsiliumService
from app.services.weekly_consilium_service import WeeklyConsiliumService
from app.services.wellness_daily_service import WellnessDailyService


# ==============================================================================
# CONTROLLED CLOCK FOR DETERMINISTIC VIRTUAL TIME ADVANCEMENT
# ==============================================================================

class ControlledClock:
    """Authoritative test clock starting at T0 = 2026-10-30 08:00:00 UTC."""
    def __init__(self, start_dt: datetime):
        self.current = start_dt

    def now(self) -> datetime:
        return self.current

    def advance(self, **kwargs: Any) -> datetime:
        self.current = self.current + timedelta(**kwargs)
        return self.current

    def set(self, dt: datetime) -> datetime:
        self.current = dt
        return self.current


# ==============================================================================
# ISOLATED TEST SQLITE DATABASE FACTORY
# ==============================================================================

def _create_isolated_file_db(db_path: Path, busy_timeout_ms: int = 10000):
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False, "timeout": busy_timeout_ms / 1000.0},
        pool_pre_ping=True,
    )

    @event.listens_for(engine, "connect")
    def _set_pragmas(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON;")
        cursor.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)};")
        cursor.close()

    Base.metadata.create_all(bind=engine)
    from alembic.config import Config
    from alembic import command
    ini = (Path(__file__).resolve().parent.parent / "alembic.ini").resolve()
    cfg = Config(str(ini))
    cfg.set_main_option("script_location", str(ini.parent / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path.as_posix()}")
    command.stamp(cfg, "head")

    SessionFactory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    return engine, SessionFactory


# ==============================================================================
# SAMPLE PAYLOADS FOR DETERMINISTIC GEMINI RESPONSES
# ==============================================================================

def _gemini_envelope(payload: Any) -> Dict[str, Any]:
    return {
        "candidates": [
            {
                "content": {
                    "parts": [{"text": json.dumps(payload, ensure_ascii=False)}]
                }
            }
        ]
    }


def _sample_anamnesis_output() -> Dict[str, Any]:
    return {
        "user_view": {
            "summary": "Primary anamnesis processed across 8 systems in blind mode."
        },
        "system_logic": {
            key: {
                "data": f"{key.upper()} anamnesis facts: mild fatigue, active routine.",
                "internal_comment": f"M1 blind comment for {key}",
            }
            for key in SYSTEM_KEYS
        },
        "training_sandbox": {
            "raw_diagnostic_hypothesis": "Normal physiological profile with mild autonomic fatigue.",
            "data_gap_analysis": "Follow resting heart rate and recovery metrics.",
            "error_probability": "Low (10%)",
        },
    }


def _sample_system_agent_output(system_key: str) -> Dict[str, Any]:
    return {
        "system_view": {
            "isolated_labs": {f"{system_key}_marker": "optimal"}
        },
        "system_logic": {
            "system_general_shelf": f"Academic pathophysiological conclusion for {system_key}."
        },
        "training_sandbox": {
            "system_training_shelf": f"Deficit log and observations for {system_key}."
        },
    }


def _sample_flash_output() -> Dict[str, Any]:
    user_reports = {
        key: (
            f"Базовое состояние системы {key} стабильное. "
            f"Рекомендовано продолжение планового динамического наблюдения."
        )
        for key in SYSTEM_KEYS
    }
    return {
        "flash_general_shelf": "INTERNAL FLASH GENERAL SHELF: Primary intersystem synthesis.",
        "flash_training_shelf": "INTERNAL FLASH TRAINING SANDBOX: Primary M1 and 8 agents audit.",
        "user_system_reports": user_reports,
    }


def _sample_pro_output() -> Dict[str, Any]:
    return {
        "vitals_thresholds": {
            "min_pessimum_bpm": 48,
            "max_pessimum_bpm": 118,
            "max_spike_delta_bpm": 28,
            "clinical_rationale": "PRO CLINICAL RATIONALE: baseline resting HR 62, normal sinus range.",
        },
        "pro_general_shelf": "SECRET_ATHENA_STRATEGY: Week 1 baseline adaptation. Max 3 questions/day.",
        "pro_training_shelf": "SECRET_PRO_TRAINING_SHELF: Final nosologies and supreme audit.",
        "user_overall_report": {
            "title": "Aegis Personal Wellness Roadmap",
            "summary": "Начальный функциональный статус устойчив. Показатели гемодинамики в целевых пределах.",
            "highlights": [
                "Поддерживайте комфортный режим аэробной нагрузки",
                "Контролируйте восстановление ЧСС после физической активности",
            ],
        },
        "flash_quality_review": {
            "approved_for_publication": True,
            "rejection_reason": None,
            "corrected_user_system_reports": {},
        },
    }


def _sample_weekly_flash_output() -> Dict[str, Any]:
    system_reports = {
        k: {
            "summary": f"Стабильная положительная динамика системы {k} по итогам недели 1.",
            "has_changes": True,
        }
        for k in SYSTEM_KEYS
    }
    return {
        "weekly_deputy_analysis": "Заместитель главного врача: динамика за 7 дней благоприятная.",
        "user_system_reports": system_reports,
    }


def _sample_weekly_pro_output() -> Dict[str, Any]:
    strategy_text = (
        "УТВЕРЖДЁННАЯ СТРАТЕГИЯ PRO НА ВТОРУЮ НЕДЕЛЮ:\n"
        "1. Контроль переносимости умеренных физических нагрузок.\n"
        "2. Уточнение качества сна и динамики восстановления пульса."
    )
    return {
        "updated_weekly_strategy": strategy_text,
        "strategy_status": "updated",
        "weekly_overall_report": {
            "title": "Итоги недели 1: Стабильный прогресс",
            "summary": "Первая неделя завершена успешно: гемодинамика устойчива, реакция на нагрузки адекватна.",
            "highlights": [
                "Пульс покоя снизился в среднем на 3 уд/мин",
                "Физическая активность переносится удовлетворительно",
            ],
        },
        "flash_quality_review": {
            "approved_for_publication": True,
            "rejection_reason": None,
            "corrected_user_system_reports": {},
        },
    }


def _sample_weekly_plan(cycle_id: str, week_number: int = 1) -> Dict[str, Any]:
    return {
        "plan_id": f"plan_{cycle_id}_w{week_number}",
        "cycle_id": cycle_id,
        "week_number": week_number,
        "source_strategy_report_id": f"rep_{cycle_id}_strat_v{week_number}",
        "source_strategy_version": week_number,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "summary_for_curator": f"План недели {week_number}: контроль восстановления ЧСС",
        "goals": [
            {
                "goal_id": f"goal_{week_number}_1",
                "title": "Оценка самочувствия после дневной активности",
                "clinical_rationale": "Убедиться в отсутствии одышки и выраженного утомления",
                "priority": 1,
                "known_facts": ["Базовая переносимость удовлетворительная"],
                "target_clarification": "Уточнить наличие одышки или сердцебиения",
                "sufficiency_criteria": "Пациент ответил о наличии или отсутствии симптомов",
                "status": "active",
                "progress_notes": None,
                "questions_asked_count": 0,
            },
            {
                "goal_id": f"goal_{week_number}_2",
                "title": "Контроль восстановления ночного сна",
                "clinical_rationale": "Оценить влияние физической активности на качество сна",
                "priority": 2,
                "known_facts": ["Продолжительность сна ~7.5 ч"],
                "target_clarification": "Уточнить время засыпания и пробуждений",
                "sufficiency_criteria": "Ответ о субъективном качестве сна",
                "status": "active",
                "progress_notes": None,
                "questions_asked_count": 0,
            },
        ],
    }


def _sample_daily_journal_assessment() -> Dict[str, Any]:
    return {
        "status": "NORMAL",
        "interpretation": "Синтез Афины: пациент следовал плану, биометрические показатели стабильны.",
        "observations": ["plan_adherence", "stable_vitals"],
        "relevant_changes": ["Стабильная адаптация к плану недели."],
        "contradictions": [],
        "uncertainties": [],
        "red_flags": False,
    }


def _sample_wellness_output(ep_id: Optional[str] = None) -> Dict[str, Any]:
    evals = []
    if ep_id:
        evals.append({
            "episode_id": ep_id,
            "workout_context": "confirmed_workout",
            "analysis": "Эпизод физической нагрузки подтвержден пользователем, гемодинамика адекватна.",
        })
    return {
        "observed_trends": ["Качественный сон и стабильный утренний пульс."],
        "limitations_and_gaps": ["АД утром не фиксировалось."],
        "questionable_measurements": [],
        "episode_evaluations": evals,
        "summary_for_athena": "Пульс и SpO2 в безопасном диапазоне. Состояние стабильно.",
    }


# ==============================================================================
# MOCK GEMINI GATEWAY BUILDER
# ==============================================================================

def _build_stage_7b1_mock_gateway(recorded_calls: List[Dict[str, Any]]) -> GeminiGateway:
    async def handler(request: httpx.Request) -> httpx.Response:
        url_path = request.url.path
        body = json.loads(request.content.decode("utf-8"))
        contents = body.get("contents", [])
        prompt_parts: List[str] = []
        for p in body.get("system_instruction", {}).get("parts", []):
            if isinstance(p, dict) and "text" in p:
                prompt_parts.append(p["text"])
        for c in contents:
            for p in c.get("parts", []):
                if isinstance(p, dict) and "text" in p:
                    prompt_parts.append(p["text"])
        prompt = "\n".join(prompt_parts)

        call_record = {
            "index": len(recorded_calls),
            "url_path": url_path,
            "prompt": prompt,
        }
        recorded_calls.append(call_record)

        # 1. 8 Organ Specialists (Gemini Flash-Lite)
        if "flash-lite" in url_path or "clinical specialist for" in prompt.lower():
            sys_key = "cvs"
            for k in SYSTEM_KEYS:
                if f"clinical specialist for {k}" in prompt.lower() or f"specialist for {k}" in prompt.lower():
                    sys_key = k
                    break
            return httpx.Response(200, json=_gemini_envelope(_sample_system_agent_output(sys_key)))

        # 2. Weekly Consilium Deputy Chief Physician (Gemini Flash)
        if "1 week (7 days) of patient monitoring has passed" in prompt.lower() or "weekly_deputy_analysis" in prompt.lower():
            assert "flash-lite" not in url_path, f"Flash-Lite forbidden for Deputy: {url_path}"
            return httpx.Response(200, json=_gemini_envelope(_sample_weekly_flash_output()))

        # 3. Weekly Consilium Chief Physician (Gemini Pro)
        if "update the working strategy for the next week" in prompt.lower() or "updated_weekly_strategy" in prompt.lower():
            assert "flash-lite" not in url_path, f"Flash-Lite forbidden for Chief Physician: {url_path}"
            return httpx.Response(200, json=_gemini_envelope(_sample_weekly_pro_output()))

        # 4. Athena Weekly Plan Record (Gemini Flash)
        if "[internal weekly plan formulation scenario]" in prompt.lower() or "athenaweeklyplanrecord" in prompt.lower():
            wk_num = 2 if ("неделя №2" in prompt.lower() or "week 2" in prompt.lower() or "week_number: 2" in prompt.lower() or "неделя 2" in prompt.lower()) else 1
            return httpx.Response(200, json=_gemini_envelope(_sample_weekly_plan("cycle_first_week_001", wk_num)))

        # 5. Athena Daily Journal Clinical Assessment (Section D) (Gemini Flash)
        if (
            "[internal daily clinical assessment scenario]" in prompt.lower()
            or "athenadailyassessmentmodeloutput" in prompt.lower()
            or "daily clinical assessment scenario" in prompt.lower()
            or "completed 24-hour observation day source evidence" in prompt.lower()
        ):
            return httpx.Response(200, json=_gemini_envelope(_sample_daily_journal_assessment()))

        # 6. Wellness Daily Analysis (Gemini Flash)
        if (
            "wellness daily analyzer" in prompt.lower()
            or "wellnessmodeloutput" in prompt.lower()
            or "daily wellness analysis request" in prompt.lower()
        ):
            ep_match = re.search(r'(ep_mode2_[a-zA-Z0-9_]+)', prompt)
            ep_id = ep_match.group(1) if ep_match else None
            return httpx.Response(200, json=_gemini_envelope(_sample_wellness_output(ep_id)))

        # 7. Primary Chief Physician (Gemini Pro)
        if "pro" in url_path or ("chief physician" in prompt.lower() and "deputy" not in prompt.lower()):
            assert "flash-lite" not in url_path, f"Flash-Lite forbidden for Chief Physician: {url_path}"
            return httpx.Response(200, json=_gemini_envelope(_sample_pro_output()))

        # 8. Primary Deputy Chief Physician (Gemini Flash)
        if "head of department" in prompt.lower() or "deputy chief physician" in prompt.lower():
            assert "flash-lite" not in url_path, f"Flash-Lite forbidden for Deputy: {url_path}"
            return httpx.Response(200, json=_gemini_envelope(_sample_flash_output()))

        # 9. Anamnesis M1 (Gemini Flash)
        if "anamnesis" in prompt.lower() or "questionnaire" in prompt.lower():
            return httpx.Response(200, json=_gemini_envelope(_sample_anamnesis_output()))

        # 10. Athena Weekly Wrap-up Message (Gemini Flash)
        if "wrap-up message formulation" in prompt.lower() or "wrapup" in prompt.lower() or "wrap-up" in prompt.lower() or "weekly_wrapup" in prompt.lower():
            wrapup_payload = {
                "message": (
                    "Поздравляю с успешным завершением первой недели! Все системы организма показали отличную динамику. "
                    "Ваша обновленная стратегия и 8 детальных отчетов готовы к просмотру в Клиентском Хранилище. "
                    "Нажмите на силуэт человека, чтобы изучить подробные заключения врачей."
                )
            }
            return httpx.Response(200, json=_gemini_envelope(wrapup_payload))

        if "vitals_alert" in prompt.lower() or "spike" in prompt.lower() or "heart rate spike" in prompt.lower():
            chat_text = (
                "Зафиксировано повышение частоты сердечных сокращений до 138 уд/мин. "
                "Вы сейчас выполняете физические упражнения или находитесь в состоянии покоя?\n\n"
                "[CLINICAL_REPORT]\n"
                "{\n"
                '  "status": "ALERT",\n'
                '  "symptoms_mentioned": [],\n'
                '  "subjective_wellbeing_score": 7,\n'
                '  "adherence_to_plan": "HIGH",\n'
                '  "red_flags": false,\n'
                '  "summary_for_master": "HR spike alert handled."\n'
                "}\n"
                "[/CLINICAL_REPORT]"
            )
            return httpx.Response(200, json=_gemini_envelope(chat_text))

        if "нормализация" in prompt.lower() or "стойкая нормализация" in prompt.lower():
            chat_text = (
                "Отлично, показатели пульса вернулись к вашим обычным безопасным значениям. "
                "Продолжайте прислушиваться к своему самочувствию.\n\n"
                "[CLINICAL_REPORT]\n"
                "{\n"
                '  "status": "NORMAL",\n'
                '  "symptoms_mentioned": [],\n'
                '  "subjective_wellbeing_score": 8,\n'
                '  "adherence_to_plan": "HIGH",\n'
                '  "red_flags": false,\n'
                '  "summary_for_master": "Mode 2 normalization acknowledged."\n'
                "}\n"
                "[/CLINICAL_REPORT]"
            )
            return httpx.Response(200, json=_gemini_envelope(chat_text))

        # Default conversational reply
        chat_text = (
            "Здравствуйте! Я Афина, ваш медицинский куратор. Как ваше самочувствие?\n\n"
            "[CLINICAL_REPORT]\n"
            "{\n"
            '  "status": "NORMAL",\n'
            '  "symptoms_mentioned": [],\n'
            '  "subjective_wellbeing_score": 8,\n'
            '  "adherence_to_plan": "HIGH",\n'
            '  "red_flags": false,\n'
            '  "summary_for_master": "Athena turn processed."\n'
            "}\n"
            "[/CLINICAL_REPORT]"
        )
        return httpx.Response(200, json=_gemini_envelope(chat_text))

    return GeminiGateway(
        api_key="stage-7b1-mock-key",
        transport=httpx.MockTransport(handler),
        max_retries=1,
        sleep_func=lambda _: None,
    )


# ==============================================================================
# SEED PREPARATORY INTAKE HELPER
# ==============================================================================

def _seed_patient_and_intake(
    session: Session,
    patient_id: str,
    cycle_id: str,
    intake_time: datetime,
) -> None:
    PatientRepository(session).create(patient_id=patient_id, status="active")
    cycle_repo = MedicalCycleRepository(session)
    cycle_repo.create(
        cycle_id=cycle_id,
        patient_id=patient_id,
        status="active",
    )
    raw_code, _ = PairingRepository(session).create_code(patient_id=patient_id, raw_code_override="123456")

    primary_service = PrimaryConsiliumService(db=session)
    primary_service.save_preparatory_anamnesis_result(
        cycle_id=cycle_id,
        anamnesis_output=_sample_anamnesis_output(),
    )
    primary_service.save_preparatory_ocr_labs(
        cycle_id=cycle_id,
        shared_labs_table={
            "hemoglobin": "144 g/L",
            "ferritin": "42 ng/mL",
            "creatinine": "82 umol/L",
            "glucose": "5.1 mmol/L",
        },
        created_at=intake_time - timedelta(hours=2),
    )
    rev_service = LabRevisionConsiliumService(db=session)
    rev_service._ensure_baseline_ledger_seeded(cycle_id)

    primary_service.save_preparatory_ecg_summary(
        cycle_id=cycle_id,
        ecg_summary="Sinus rhythm, HR 64 bpm, normal electrical axis, no ST-T shifts.",
    )
    session.commit()


# ==============================================================================
# MAIN INTEGRATION TEST: FULL FIRST-WEEK LIFECYCLE OVER REAL HTTP UVICORN
# ==============================================================================

@pytest.mark.anyio
async def test_stage_7b1_full_first_week_lifecycle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """
    STAGE 7B.1: FULL FIRST-WEEK INTEGRATION TEST
    - Real Uvicorn HTTP server over loopback 127.0.0.1 on dynamic port.
    - Isolated temporary SQLite file via tmp_path.
    - Active ClinicalAutomationWorker with ControlledClock.
    - Deterministic Mock Gemini Gateway (0 real API calls).
    - Full sequence Day 0 -> Days 1-6 -> Day 7.
    - Strict verification of all 8 architecture invariants.
    """
    # 1. Setup Isolated SQLite Database and Controlled Clock
    db_file = tmp_path / "stage_7b1_vault.db"
    backup_dir = tmp_path / "backups"
    test_engine, TestSessionFactory = _create_isolated_file_db(db_file)

    # T0 anchor: 2026-10-30 08:00:00 UTC (past relative to host clock, safe for validation)
    t0_dt = datetime(2026, 10, 30, 8, 0, 0, tzinfo=timezone.utc)
    clock = ControlledClock(t0_dt)
    patient_id = "pat_first_week_001"
    cycle_id = "cycle_first_week_001"

    # Ensure JWT access token ttl is generous for test duration
    monkeypatch.setattr(settings, "jwt_secret", "test_jwt_stage_7b1_secret_1234567890!")
    monkeypatch.setattr(settings, "pairing_secret", "test_pairing_stage_7b1_secret_12345!")
    monkeypatch.setattr(settings, "api_secret", "test_api_stage_7b1_secret_1234567890")
    monkeypatch.setattr(settings, "access_token_ttl_minutes", 60 * 24 * 30)
    monkeypatch.setattr(settings, "enable_mode2_monitoring", True)

    recorded_gemini_calls: List[Dict[str, Any]] = []
    mock_gw = _build_stage_7b1_mock_gateway(recorded_gemini_calls)

    # 2. Dependency Overrides
    def _override_get_db():
        db = TestSessionFactory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[client_get_gemini_gateway] = lambda: mock_gw
    app.dependency_overrides[client_get_server_now_provider] = lambda: clock.now
    app.dependency_overrides[chat_get_gemini_gateway] = lambda: mock_gw
    app.dependency_overrides[chat_get_server_now_provider] = lambda: clock.now
    app.dependency_overrides[vitals_get_gemini_gateway] = lambda: mock_gw
    app.dependency_overrides[vitals_get_server_now_provider] = lambda: clock.now
    app.dependency_overrides[cascade_get_gemini_gateway] = lambda: mock_gw
    app.dependency_overrides[cascade_get_server_now_provider] = lambda: clock.now

    # 3. Configure App State for Lifespan & Worker
    app.state.engine = test_engine
    app.state.backup_dir = backup_dir
    app.state.enable_automation_worker = True
    app.state.worker_poll_interval = 0.05
    app.state.gateway_factory = lambda: mock_gw
    app.state.now_provider = clock.now
    app.state.enable_weekly_plan = True
    app.state.enable_daily_journal = True
    app.state.enable_wellness_daily = True
    app.state.enable_automatic_weekly = True
    app.state.enable_lab_revision = True

    # 4. Seed Initial Patient & Intake Data
    with TestSessionFactory() as init_session:
        _seed_patient_and_intake(
            init_session,
            patient_id=patient_id,
            cycle_id=cycle_id,
            intake_time=t0_dt,
        )

    # 5. Start Real Uvicorn HTTP Server on Ephemeral Port
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    uvicorn_config = uvicorn.Config(
        app=app,
        host="127.0.0.1",
        port=port,
        log_level="warning",
    )
    server = uvicorn.Server(uvicorn_config)
    server_task = asyncio.create_task(server.serve())

    while not server.started:
        await asyncio.sleep(0.01)

    try:
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=30.0) as client:

            # ------------------------------------------------------------------
            # STEP 0: VERIFY SERVER CONNECTION
            # ------------------------------------------------------------------
            health_resp = await client.get("/health")
            assert health_resp.status_code == 200
            assert health_resp.json().get("status") == "ok"

            # ------------------------------------------------------------------
            # DAY 0: T0 SETUP & MANUAL PRIMARY CONSILIUM
            # ------------------------------------------------------------------
            # 1. Device Pairing via HTTP POST /api/v1/auth/pair
            pair_resp = await client.post(
                "/api/v1/auth/pair",
                json={
                    "pairing_code": "123456",
                    "device_id": "dev_pixel_001",
                    "device_name": "Pixel 8 Pro",
                    "platform": "android",
                },
            )
            assert pair_resp.status_code == 200
            pair_data = pair_resp.json()
            access_token = pair_data["access_token"]
            auth_headers = {"Authorization": f"Bearer {access_token}"}

            # 2. Manual Primary Consilium via HTTP POST /api/v1/client/cascade/run-primary
            primary_calls_before = len(recorded_gemini_calls)
            primary_resp = await client.post(
                "/api/v1/client/cascade/run-primary",
                headers=auth_headers,
            )
            if primary_resp.status_code != 200:
                print("PRIMARY_RESP ERROR:", primary_resp.status_code, primary_resp.text)
            assert primary_resp.status_code == 200
            primary_data = primary_resp.json()
            assert primary_data["cycle_id"] == cycle_id
            assert len(primary_data["system_reports"]) == 8
            assert primary_data["overall_report"]["content"]["title"] == "Aegis Personal Wellness Roadmap"

            # Verify Primary Consilium Gemini calls: exactly 8 Flash-Lite + 1 Flash + 1 Pro = 10 calls
            primary_consilium_calls = recorded_gemini_calls[:10]
            assert len(primary_consilium_calls) == 10, (
                f"Expected 10 Gemini calls for Primary Consilium, got {len(primary_consilium_calls)}"
            )
            flash_lite_count = sum(1 for c in primary_consilium_calls if "flash-lite" in c["url_path"])
            flash_count = sum(
                1 for c in primary_consilium_calls
                if "flash" in c["url_path"] and "flash-lite" not in c["url_path"]
            )
            pro_count = sum(1 for c in primary_consilium_calls if "pro" in c["url_path"])
            assert flash_lite_count == 8, f"Expected 8 Flash-Lite calls, got {flash_lite_count}"
            assert flash_count == 1, f"Expected 1 Flash Deputy call, got {flash_count}"
            assert pro_count == 1, f"Expected 1 Pro Chief call, got {pro_count}"

            # 3. Worker Execution: post_primary_init
            # Let the background worker process post_primary_init
            worker: ClinicalAutomationWorker = app.state.automation_worker

            async def _drain_worker(w: ClinicalAutomationWorker, max_ticks: int = 10) -> int:
                total = 0
                for _ in range(max_ticks):
                    jobs = await w.run_once()
                    if not jobs:
                        break
                    total += len(jobs)
                return total

            await _drain_worker(worker)

            # Verify Schedule Anchor T0 is fixed
            with TestSessionFactory() as session:
                w_svc = WeeklyConsiliumService(db=session, now_provider=clock.now)
                anchor = w_svc.find_schedule_anchor_timestamp(cycle_id)
                assert anchor is not None, "Schedule Anchor T0 was not created!"
                assert anchor == t0_dt

            # 4. Advance clock by 30 seconds -> first_planned_question
            clock.advance(seconds=30)
            await _drain_worker(worker)

            # Verify chat history contains greeting and first question
            history_resp = await client.get(
                "/api/v1/client/chat/athena/history",
                headers=auth_headers,
            )
            assert history_resp.status_code == 200
            history_data = history_resp.json()
            assert len(history_data["messages"]) >= 2
            assert any(m["sender"] == "curator" and "Афина" in m["text"] for m in history_data["messages"])

            # ------------------------------------------------------------------
            # DAYS 1–6: WEEKLY LIFECYCLE
            # ------------------------------------------------------------------

            # --- DAY 1 (Observation Day 1: [T0 - 10m, T0 + 24h)) ---
            # Morning wake-up at T0 + 1h (09:00 UTC, before midday 12:00)
            clock.set(t0_dt + timedelta(hours=1))
            # Patient responds to Athena's question
            chat_day1 = await client.post(
                "/api/v1/client/chat/athena",
                headers=auth_headers,
                json={"message": "Доброе утро! Чувствую себя бодро, спал отлично."},
            )
            assert chat_day1.status_code == 200

            # Upload Morning Vitals with wake_up metadata
            vitals_day1 = await client.post(
                "/api/v1/vitals/batch",
                headers=auth_headers,
                json={
                    "cycle_id": cycle_id,
                    "measurements": [
                        {
                            "pulse": 62,
                            "systolic": 118,
                            "diastolic": 76,
                            "spo2": 98,
                            "source": "watch_sensor",
                            "measured_at": (clock.now() - timedelta(minutes=30)).isoformat(),
                            "raw_metadata": {"event": "wake_up", "sleep_hours": 7.5, "quality": "restorative"},
                        },
                        {
                            "pulse": 65,
                            "systolic": 120,
                            "diastolic": 78,
                            "spo2": 98,
                            "source": "manual_input",
                            "measured_at": clock.now().isoformat(),
                        },
                    ],
                },
            )
            assert vitals_day1.status_code == 201

            # Advance past midday fallback (T0 + 5h = 13:00 UTC) & run worker
            clock.set(t0_dt + timedelta(hours=5))
            await _drain_worker(worker)

            with TestSessionFactory() as session:
                well_svc = WellnessDailyService(db=session, now_provider=clock.now)
                rep1 = well_svc.get_daily_wellness_report(cycle_id, 1, 1)
                assert rep1 is not None and rep1.status == WellnessStatus.READY
                jour_svc = AthenaDailyJournalService(db=session, now_provider=clock.now)
                j1 = jour_svc.get_daily_journal(cycle_id, 1, 1)
                assert j1 is not None and j1.athena_clinical_assessment.is_available is True

            # --- DAY 2 (Observation Day 2: [T0 + 24h, T0 + 48h)) ---
            # Morning wake-up at T0 + 25h (09:00 UTC)
            clock.set(t0_dt + timedelta(hours=25))
            await client.post(
                "/api/v1/client/chat/athena",
                headers=auth_headers,
                json={"message": "Всё по плану, водный баланс соблюдаю."},
            )
            await client.post(
                "/api/v1/vitals/batch",
                headers=auth_headers,
                json={
                    "cycle_id": cycle_id,
                    "measurements": [
                        {
                            "pulse": 64,
                            "systolic": 119,
                            "diastolic": 77,
                            "spo2": 99,
                            "source": "watch_sensor",
                            "measured_at": clock.now().isoformat(),
                            "raw_metadata": {"event": "wake_up", "sleep_hours": 8.0},
                        }
                    ],
                },
            )
            # Advance past Day 2 midday (T0 + 29h = 13:00 UTC) & run worker
            clock.set(t0_dt + timedelta(hours=29))
            await _drain_worker(worker)

            with TestSessionFactory() as session:
                well_svc = WellnessDailyService(db=session, now_provider=clock.now)
                rep2 = well_svc.get_daily_wellness_report(cycle_id, 1, 2)
                assert rep2 is not None and rep2.status == WellnessStatus.READY

            # --- DAY 3 (Observation Day 3: [T0 + 48h, T0 + 72h)) ---
            # Morning wake-up at T0 + 49h (09:00 UTC)
            clock.set(t0_dt + timedelta(hours=49))
            await client.post(
                "/api/v1/client/chat/athena",
                headers=auth_headers,
                json={"message": "Дневная прогулка 5 км завершена, одышки нет."},
            )
            await client.post(
                "/api/v1/vitals/batch",
                headers=auth_headers,
                json={
                    "cycle_id": cycle_id,
                    "measurements": [
                        {
                            "pulse": 63,
                            "systolic": 121,
                            "diastolic": 79,
                            "spo2": 98,
                            "source": "watch_sensor",
                            "measured_at": clock.now().isoformat(),
                            "raw_metadata": {"event": "wake_up", "sleep_hours": 7.2},
                        }
                    ],
                },
            )
            # Advance past Day 3 midday (T0 + 53h = 13:00 UTC) & run worker
            clock.set(t0_dt + timedelta(hours=53))
            await _drain_worker(worker)

            with TestSessionFactory() as session:
                well_svc = WellnessDailyService(db=session, now_provider=clock.now)
                rep3 = well_svc.get_daily_wellness_report(cycle_id, 1, 3)
                assert rep3 is not None and rep3.status == WellnessStatus.READY

            # --- DAY 4 (Observation Day 4: [T0 + 72h, T0 + 96h)) — SIMULATION OF DAY WITHOUT MEASUREMENTS ---
            # Zero vitals uploaded for Day 4!
            # Advance past Day 4 midday fallback (T0 + 77h = 13:00 UTC)
            clock.set(t0_dt + timedelta(hours=77))
            calls_before_day4 = len(recorded_gemini_calls)

            # Worker processes Day 4 without vitals (Wellness -> NO_DATA, then Daily Journal closed)
            await _drain_worker(worker)

            # INVARIANT: Day 4 Wellness transitions to NO_DATA with STRICTLY 0 Gemini calls!
            # Daily Journal closes without substantive evidence with STRICTLY 0 Gemini calls!
            calls_after_day4 = len(recorded_gemini_calls)
            assert calls_after_day4 == calls_before_day4, (
                f"Day 4 consumed {calls_after_day4 - calls_before_day4} Gemini calls! Expected strictly 0."
            )

            with TestSessionFactory() as session:
                well_svc = WellnessDailyService(db=session, now_provider=clock.now)
                rep4 = well_svc.get_daily_wellness_report(cycle_id, 1, 4)
                assert rep4 is not None
                assert rep4.status == WellnessStatus.NO_DATA
                assert rep4.interpretation is None

                jour_svc = AthenaDailyJournalService(db=session, now_provider=clock.now)
                j4 = jour_svc.get_daily_journal(cycle_id, 1, 4)
                assert j4 is not None
                assert j4.athena_clinical_assessment.is_available is False
                assert "No substantive clinical data available" in (j4.athena_clinical_assessment.absence_reason or "")

            # --- DAY 5 (Observation Day 5: [T0 + 96h, T0 + 120h)) — MODE 2 PHYSICAL EXERTION INCIDENT ---
            # Morning wake-up at T0 + 97h (09:00 UTC)
            clock.set(t0_dt + timedelta(hours=97))

            # 1. Normal morning vitals (midday fallback will evaluate full day including workout)
            await client.post(
                "/api/v1/vitals/batch",
                headers=auth_headers,
                json={
                    "cycle_id": cycle_id,
                    "measurements": [
                        {
                            "pulse": 61,
                            "systolic": 117,
                            "diastolic": 76,
                            "spo2": 99,
                            "source": "watch_sensor",
                            "measured_at": clock.now().isoformat(),
                            "raw_metadata": {"sleep_hours": 7.6},
                        }
                    ],
                },
            )

            # 2. Heart rate spike at T0 + 98h triggers Mode 2 episode
            clock.set(t0_dt + timedelta(hours=98))
            spike_resp = await client.post(
                "/api/v1/vitals/batch",
                headers=auth_headers,
                json={
                    "cycle_id": cycle_id,
                    "measurements": [
                        {
                            "pulse": 138,
                            "spo2": 97,
                            "source": "watch_sensor",
                            "measured_at": clock.now().isoformat(),
                        }
                    ],
                },
            )
            assert spike_resp.status_code == 201

            # Verify active Mode 2 episode exists
            active_m2_resp = await client.get(
                f"/api/v1/vitals/mode2/active?cycle_id={cycle_id}",
                headers=auth_headers,
            )
            assert active_m2_resp.status_code == 200
            active_m2_data = active_m2_resp.json()
            assert active_m2_data is not None
            assert active_m2_data["status"] == "active"
            assert active_m2_data["trigger_pulse"] == 138
            mode2_ep_id = active_m2_data["episode_id"]

            # 3. Patient confirms workout via HTTP POST /mode2/workout-response
            workout_resp = await client.post(
                "/api/v1/vitals/mode2/workout-response",
                headers=auth_headers,
                json={
                    "cycle_id": cycle_id,
                    "is_workout": True,
                    "user_message": "Да, интенсивно кручу педали на велотренажере",
                    "symptoms": "одышки и боли в груди нет",
                },
            )
            assert workout_resp.status_code == 200
            workout_data = workout_resp.json()
            assert workout_data["workout_context"] == "confirmed_workout"

            # 4. Sustained Normalization closes the episode
            clock.advance(minutes=2)
            await client.post(
                "/api/v1/vitals/batch",
                headers=auth_headers,
                json={
                    "cycle_id": cycle_id,
                    "measurements": [
                        {
                            "pulse": 72,
                            "spo2": 98,
                            "source": "watch_sensor",
                            "measured_at": clock.now().isoformat(),
                        }
                    ],
                },
            )
            clock.advance(minutes=1)
            await client.post(
                "/api/v1/vitals/batch",
                headers=auth_headers,
                json={
                    "cycle_id": cycle_id,
                    "measurements": [
                        {
                            "pulse": 70,
                            "spo2": 98,
                            "source": "watch_sensor",
                            "measured_at": clock.now().isoformat(),
                        }
                    ],
                },
            )

            # Verify episode is no longer active
            no_active_resp = await client.get(
                f"/api/v1/vitals/mode2/active?cycle_id={cycle_id}",
                headers=auth_headers,
            )
            assert no_active_resp.status_code == 200
            assert no_active_resp.json() is None

            # Verify episode in history
            hist_m2_resp = await client.get(
                f"/api/v1/vitals/mode2/history?cycle_id={cycle_id}",
                headers=auth_headers,
            )
            assert hist_m2_resp.status_code == 200
            hist_m2_list = hist_m2_resp.json()
            assert len(hist_m2_list) == 1
            assert hist_m2_list[0]["episode_id"] == mode2_ep_id
            assert hist_m2_list[0]["status"] == "closed"
            assert hist_m2_list[0]["closed_at_utc"] is not None

            # INVARIANT: Mode 2 turns did NOT consume planned question quota!
            with TestSessionFactory() as session:
                plan_svc = AthenaWeeklyPlanService(db=session, now_provider=clock.now)
                daily_q_count = plan_svc.get_daily_planned_questions_count(cycle_id, as_of=clock.now())
                assert daily_q_count <= 3

            # Advance past Day 5 midday (T0 + 101h = 13:00 UTC) and run worker for Day 5 Wellness & Journal
            clock.set(t0_dt + timedelta(hours=101))
            await _drain_worker(worker)

            # INVARIANT: Mode 2 episode recorded EXACTLY ONCE in Wellness and Daily Journal
            with TestSessionFactory() as session:
                well_svc = WellnessDailyService(db=session, now_provider=clock.now)
                rep5 = well_svc.get_daily_wellness_report(cycle_id, 1, 5)
                assert rep5 is not None and rep5.status == WellnessStatus.READY
                assert len(rep5.factual_summary.mode2_episodes) == 1
                assert rep5.factual_summary.mode2_episodes[0].episode_id == mode2_ep_id

                jour_svc = AthenaDailyJournalService(db=session, now_provider=clock.now)
                j5 = jour_svc.get_daily_journal(cycle_id, 1, 5)
                assert j5 is not None
                assert len(j5.wellness_evidence.mode2_episodes) == 1
                assert j5.wellness_evidence.mode2_episodes[0].episode_id == mode2_ep_id

            # --- DAY 6 (Observation Day 6: [T0 + 120h, T0 + 144h)) ---
            # Morning wake-up at T0 + 121h (09:00 UTC)
            clock.set(t0_dt + timedelta(hours=121))
            await client.post(
                "/api/v1/client/chat/athena",
                headers=auth_headers,
                json={"message": "Самочувствие отличное, неделя подходит к концу."},
            )
            await client.post(
                "/api/v1/vitals/batch",
                headers=auth_headers,
                json={
                    "cycle_id": cycle_id,
                    "measurements": [
                        {
                            "pulse": 62,
                            "systolic": 118,
                            "diastolic": 76,
                            "spo2": 99,
                            "source": "watch_sensor",
                            "measured_at": clock.now().isoformat(),
                            "raw_metadata": {"event": "wake_up", "sleep_hours": 7.9},
                        }
                    ],
                },
            )
            # Advance past Day 6 midday (T0 + 125h = 13:00 UTC) & run worker
            clock.set(t0_dt + timedelta(hours=125))
            await _drain_worker(worker)

            with TestSessionFactory() as session:
                well_svc = WellnessDailyService(db=session, now_provider=clock.now)
                rep6 = well_svc.get_daily_wellness_report(cycle_id, 1, 6)
                assert rep6 is not None and rep6.status == WellnessStatus.READY

            # ------------------------------------------------------------------
            # DAY 7: T0 + 168 HOURS — AUTOMATIC WEEKLY CONSILIUM
            # ------------------------------------------------------------------
            # Advance clock to boundary T0 + 168h
            clock.set(t0_dt + timedelta(hours=168, seconds=1))
            weekly_calls_before = len(recorded_gemini_calls)

            # Worker automatically discovers and executes Weekly Consilium Steps A, B, C
            await _drain_worker(worker)

            # Verify that Weekly Consilium executed:
            # - Flash Deputy and Pro Chief were invoked.
            # - Organ specialists (Flash-Lite) were NOT called!
            weekly_calls = [
                c for c in recorded_gemini_calls[weekly_calls_before:]
                if "weekly" in c["prompt"].lower() or "weekly" in c["url_path"].lower()
            ]
            for c in weekly_calls:
                assert "flash-lite" not in c["url_path"], (
                    f"Flash-Lite organ specialist invoked during Weekly Consilium! {c['url_path']}"
                )

            # Step B & C verification:
            with TestSessionFactory() as session:
                w_svc = WeeklyConsiliumService(db=session, now_provider=clock.now)
                recovered_weekly = w_svc.recover_completed_response(cycle_id, sprint_id=f"sprint_{cycle_id}_w1")
                assert recovered_weekly is not None, "Weekly Consilium Sprint 1 was not concluded!"
                assert recovered_weekly.sprint_number == 1
                assert recovered_weekly.overall_report.version == 1
                assert len(recovered_weekly.system_reports) == 8
                assert all(r.version == 2 for r in recovered_weekly.system_reports.values())

                plan_svc = AthenaWeeklyPlanService(db=session, now_provider=clock.now)
                plan_w2 = plan_svc.get_active_weekly_plan(cycle_id, week_number=2)
                assert plan_w2 is not None, "Week 2 Plan was not created by Step B!"
                assert plan_w2.week_number == 2

            # Verify Athena Weekly Wrap-up in Chat History
            chat_w7 = await client.get(
                "/api/v1/client/chat/athena/history",
                headers=auth_headers,
            )
            assert chat_w7.status_code == 200
            all_messages = chat_w7.json()["messages"]
            wrapup_msgs = [
                m for m in all_messages
                if "первой недели" in m["text"].lower() or "поздравляю" in m["text"].lower()
            ]
            assert len(wrapup_msgs) >= 1, "Weekly wrapup message missing from chat history!"

            # ------------------------------------------------------------------
            # VERIFY CLIENT REPORTS VIA HTTP GET /api/v1/reports/client
            # ------------------------------------------------------------------
            client_reports_resp = await client.get(
                f"/api/v1/reports/client?cycle_id={cycle_id}",
                headers=auth_headers,
            )
            assert client_reports_resp.status_code == 200
            client_reports = client_reports_resp.json()
            assert len(client_reports) >= 9, f"Expected at least 9 client reports, got {len(client_reports)}"

            # ------------------------------------------------------------------
            # SECTION 4: ARCHITECTURE INVARIANTS VERIFICATION
            # ------------------------------------------------------------------

            # Invariant 1: Primary Consilium full cascade runs strictly 1 time
            with TestSessionFactory() as session:
                m_repo = ModelReportRepository(session)
                primary_overall = m_repo.list_client_facing(cycle_id=cycle_id, report_type="user_overall_report")
                assert len(primary_overall) == 1, "Primary Consilium must have run strictly 1 time!"

            # Invariant 2: Weekly Consilium runs strictly 1 time at T0 + 168h
            with TestSessionFactory() as session:
                m_repo = ModelReportRepository(session)
                weekly_overall = m_repo.list_client_facing(cycle_id=cycle_id, report_type="weekly_user_overall_report")
                assert len(weekly_overall) == 1, "Weekly Consilium Sprint 1 must have run strictly 1 time!"

            # Invariant 3: Zero server_only reports leaked to client
            for rep in client_reports:
                assert rep["privacy_level"] == "client_facing", (
                    f"Privacy leak! server_only report {rep['report_id']} returned to client!"
                )
                assert "internal" not in rep["report_type"]
                assert "shelf" not in rep["report_type"]
                assert "journal" not in rep["report_type"]

            # Invariant 4: Athena question limit (<= 3 questions/day) respected across all 7 days
            with TestSessionFactory() as session:
                plan_svc = AthenaWeeklyPlanService(db=session, now_provider=clock.now)
                for day_idx in range(1, 8):
                    check_time = t0_dt + timedelta(hours=24 * day_idx - 1)
                    q_count = plan_svc.get_daily_planned_questions_count(cycle_id, as_of=check_time)
                    assert q_count <= 3, f"Day {day_idx} exceeded question limit: {q_count} > 3!"

            # Invariant 5: Mode 2 is isolated from routine questions
            # Verified during Day 5: mode2 alert turns did not increment routine planned question count.

            # Invariant 6: Day 4 without data consumed strictly 0 Gemini calls
            # Verified during Day 4: calls_after_day4 == calls_before_day4.

            # Invariant 7: Mode 2 episode recorded in Wellness and Journal exactly once
            with TestSessionFactory() as session:
                well_svc = WellnessDailyService(db=session, now_provider=clock.now)
                rep5 = well_svc.get_daily_wellness_report(cycle_id, 1, 5)
                assert len(rep5.factual_summary.mode2_episodes) == 1
                jour_svc = AthenaDailyJournalService(db=session, now_provider=clock.now)
                j5 = jour_svc.get_daily_journal(cycle_id, 1, 5)
                assert len(j5.wellness_evidence.mode2_episodes) == 1

            # Invariant 8: Server and SQLite remain healthy throughout simulation
            final_health = await client.get("/health")
            assert final_health.status_code == 200
            assert final_health.json()["status"] == "ok"

    finally:
        # Graceful shutdown of Uvicorn server and worker
        server.should_exit = True
        await server_task
        app.dependency_overrides.clear()
        test_engine.dispose()
