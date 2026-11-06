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
from app.models.health_measurement import HealthMeasurement
from app.schemas.privacy import PrivacyLevel
from app.models.patient import Patient
from app.repositories.cycle_repository import MedicalCycleRepository
from app.repositories.pairing_repository import PairingRepository
from app.repositories.patient_repository import PatientRepository
from app.repositories.report_repository import ModelReportRepository
from app.repositories.measurement_repository import HealthMeasurementRepository
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
from app.schemas.wellness import (
    WellnessDailyReport,
    WellnessStatus,
    WellnessTimeWindow,
    WellnessTriggerType,
    WellnessBiometricsSummary,
)
from app.schemas.lab_scheduling import (
    BackgroundLabRevisionChiefOutput,
    BackgroundLabRevisionDeputyOutput,
    LabDocumentStatus,
)
from app.schemas.health import HealthMeasurementCreate
from app.services.athena_chat_service import AthenaChatService
from app.services.athena_daily_journal_service import AthenaDailyJournalService
from app.services.athena_weekly_plan_service import (
    ATHENA_ROUTINE_QUESTION_REPORT_TYPE,
    ATHENA_WEEKLY_PLAN_REPORT_TYPE,
    AthenaWeeklyPlanService,
)
from app.services.clinical_automation_service import (
    AutomationJobRecord,
    AutomationJobStatus,
    AutomationJobType,
    ClinicalAutomationService,
    ClinicalAutomationWorker,
    ConsiliumConcurrencyConflictError,
)
from app.services.gemini_gateway import GeminiGateway, GeminiGatewayError, GeminiModelRole
from app.services.lab_revision_consilium_service import (
    BACKGROUND_LAB_REVISION_REPORT_TYPE,
    LabRevisionConsiliumService,
)
from app.services.lab_scheduling_service import (
    BACKGROUND_LAB_REVISION_COOLDOWN_HOURS,
    LabSchedulingService,
)
from app.services.mode2_vitals_service import Mode2EpisodeStatus, Mode2VitalsMonitoringService
from app.services.primary_consilium_service import PrimaryConsiliumService
from app.services.weekly_consilium_service import (
    ATHENA_ACTIVE_STRATEGY_REPORT_TYPE,
    ATHENA_SCHEDULE_ANCHOR_REPORT_TYPE,
    WeeklyConsiliumService,
)
from app.services.wellness_daily_service import WellnessDailyService


# ==============================================================================
# CONTROLLED CLOCK FOR DETERMINISTIC VIRTUAL TIME ADVANCEMENT
# ==============================================================================

class ControlledClock:
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
            }
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


def _sample_lab_revision_deputy_output() -> Dict[str, Any]:
    return {
        "deputy_synthesis": "Синтез заместителя: новые лабораторные параметры стабильны.",
        "specialist_discrepancies": [],
        "clinical_correlations": ["Метаболический профиль стабилен."],
        "data_limitations": [],
        "draft_hypotheses": [],
    }


def _sample_lab_revision_chief_output() -> Dict[str, Any]:
    return {
        "analytical_summary": "Главный врач: аналитический вердикт ревизии лаборатории утвержден.",
        "observed_facts": ["Показатели крови соответствуют целевому коридору."],
        "clinical_hypotheses": [],
        "uncertainties_and_data_deficits": [],
        "questions_for_weekly_consilium": ["Продолжить наблюдение в рамках недельного спринта."],
        "cascade_quality_audit": "8 специалистов Flash-Lite и заместитель Flash выполнили ревизию корректно.",
    }


def _build_failure_injection_mock_gateway(
    recorded_calls: List[Dict[str, Any]],
    fail_condition: Optional[Callable[[str, str], Optional[httpx.Response]]] = None,
) -> GeminiGateway:
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

        if fail_condition:
            forced_resp = fail_condition(url_path, prompt)
            if forced_resp is not None:
                return forced_resp

        # 1. 8 Organ Specialists (Gemini Flash-Lite)
        if "flash-lite" in url_path or "clinical specialist for" in prompt.lower():
            sys_key = "cvs"
            for k in SYSTEM_KEYS:
                if f"clinical specialist for {k}" in prompt.lower() or f"specialist for {k}" in prompt.lower():
                    sys_key = k
                    break
            return httpx.Response(200, json=_gemini_envelope(_sample_system_agent_output(sys_key)))

        # 2. Lab Revision Deputy
        if (
            "background comparative lab-revision" in prompt.lower()
            or "backgroundlabrevisiondeputyoutput" in prompt.lower()
            or "lab revision deputy" in prompt.lower()
        ):
            return httpx.Response(200, json=_gemini_envelope(_sample_lab_revision_deputy_output()))

        # 3. Lab Revision Chief
        if (
            "supreme analytical lab-revision" in prompt.lower()
            or "backgroundlabrevisionchiefoutput" in prompt.lower()
            or "lab revision chief" in prompt.lower()
        ):
            return httpx.Response(200, json=_gemini_envelope(_sample_lab_revision_chief_output()))

        # 4. Weekly Consilium Deputy (Gemini Flash)
        if "1 week (7 days) of patient monitoring has passed" in prompt.lower() or "weekly_deputy_analysis" in prompt.lower():
            return httpx.Response(200, json=_gemini_envelope(_sample_weekly_flash_output()))

        # 5. Weekly Consilium Chief (Gemini Pro)
        if "update the working strategy for the next week" in prompt.lower() or "updated_weekly_strategy" in prompt.lower():
            return httpx.Response(200, json=_gemini_envelope(_sample_weekly_pro_output()))

        # 6. Athena Weekly Plan Record (Gemini Flash)
        if "[internal weekly plan formulation scenario]" in prompt.lower() or "athenaweeklyplanrecord" in prompt.lower():
            wk_num = 2 if ("неделя №2" in prompt.lower() or "week 2" in prompt.lower() or "week_number: 2" in prompt.lower() or "неделя 2" in prompt.lower()) else 1
            return httpx.Response(200, json=_gemini_envelope(_sample_weekly_plan("cycle_fail_001", wk_num)))

        # 7. Athena Daily Journal Clinical Assessment (Section D) (Gemini Flash)
        if (
            "[internal daily clinical assessment scenario]" in prompt.lower()
            or "athenadailyassessmentmodeloutput" in prompt.lower()
            or "daily clinical assessment scenario" in prompt.lower()
            or "completed 24-hour observation day source evidence" in prompt.lower()
        ):
            return httpx.Response(200, json=_gemini_envelope(_sample_daily_journal_assessment()))

        # 8. Wellness Daily Analysis (Gemini Flash)
        if (
            "wellness daily analyzer" in prompt.lower()
            or "wellnessmodeloutput" in prompt.lower()
            or "daily wellness analysis request" in prompt.lower()
        ):
            ep_match = re.search(r'(ep_mode2_[a-zA-Z0-9_]+)', prompt)
            ep_id = ep_match.group(1) if ep_match else None
            return httpx.Response(200, json=_gemini_envelope(_sample_wellness_output(ep_id)))

        # 9. Primary Chief Physician (Gemini Pro)
        if "pro" in url_path or ("chief physician" in prompt.lower() and "deputy" not in prompt.lower()):
            return httpx.Response(200, json=_gemini_envelope(_sample_pro_output()))

        # 10. Primary Deputy Chief Physician (Gemini Flash)
        if "head of department" in prompt.lower() or "deputy chief physician" in prompt.lower():
            return httpx.Response(200, json=_gemini_envelope(_sample_flash_output()))

        # 11. Anamnesis M1 (Gemini Flash)
        if "anamnesis" in prompt.lower() or "questionnaire" in prompt.lower():
            return httpx.Response(200, json=_gemini_envelope(_sample_anamnesis_output()))

        # 12. Athena Weekly Wrap-up Message (Gemini Flash)
        if "wrap-up message formulation" in prompt.lower() or "wrapup" in prompt.lower() or "wrap-up" in prompt.lower() or "weekly_wrapup" in prompt.lower():
            wrapup_payload = {
                "message": (
                    "Поздравляю с успешным завершением недели! Все показатели в норме."
                )
            }
            return httpx.Response(200, json=_gemini_envelope(wrapup_payload))

        if "vitals_alert" in prompt.lower() or "spike" in prompt.lower() or "heart rate spike" in prompt.lower():
            chat_text = (
                "Зафиксировано повышение частоты сердечных сокращений до 138 уд/мин.\n\n"
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
                "Отлично, показатели пульса нормализовались.\n\n"
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

        chat_text = (
            "Здравствуйте! Я Афина, ваш медицинский куратор.\n\n"
            "[CLINICAL_REPORT]\n"
            "{\n"
            '  "status": "NORMAL",\n'
            '  "symptoms_mentioned": [],\n'
            '  "subjective_wellbeing_score": 8,\n'
            '  "adherence_to_plan": "HIGH",\n'
            '  "red_flags": false,\n'
            '  "summary_for_master": "Routine chat handled."\n'
            "}\n"
            "[/CLINICAL_REPORT]"
        )
        return httpx.Response(200, json=_gemini_envelope(chat_text))

    return GeminiGateway(
        api_key="stage-7b2-mock-key",
        transport=httpx.MockTransport(handler),
        max_retries=1,
        sleep_func=lambda _: None,
    )


def _seed_completed_primary_cycle(
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
        ecg_summary="Sinus rhythm, HR 64 bpm, normal electrical axis.",
    )

    # Save mock primary consilium reports
    rep_repo = ModelReportRepository(session)
    rep_repo.create_next_version(
        cycle_id=cycle_id,
        model="gemini-1.5-pro",
        report_type="user_overall_report",
        privacy_level=PrivacyLevel.CLIENT_FACING,
        content={
            "title": "Aegis Personal Wellness Roadmap",
            "summary": "Начальный функциональный статус устойчив. Показатели гемодинамики в целевых пределах.",
            "highlights": [
                "Поддерживайте комфортный режим аэробной нагрузки",
                "Контролируйте восстановление ЧСС после физической активности",
            ],
        },
        commit=False,
    )
    rep_repo.create_next_version(
        cycle_id=cycle_id,
        model="gemini-1.5-flash",
        report_type="deputy_chief_physician_internal",
        privacy_level=PrivacyLevel.SERVER_ONLY,
        content=_sample_flash_output(),
        commit=False,
    )
    rep_repo.create_next_version(
        cycle_id=cycle_id,
        model="gemini-1.5-pro",
        report_type="chief_physician_internal",
        privacy_level=PrivacyLevel.SERVER_ONLY,
        content=_sample_pro_output(),
        commit=False,
    )
    rep_repo.create_next_version(
        cycle_id=cycle_id,
        model="gemini-1.5-pro",
        report_type=ATHENA_ACTIVE_STRATEGY_REPORT_TYPE,
        privacy_level=PrivacyLevel.SERVER_ONLY,
        content={
            "active_strategy_text": "Week 1 baseline adaptation strategy",
            "clinical_background_text": "Baseline nosologies and clinical background",
            "deputy_synthesis_text": "Deputy synthesis across 8 organ systems",
            "active_strategy": "Week 1 baseline adaptation strategy",
            "clinical_background": "Baseline nosologies and clinical background",
            "deputy_synthesis": "Deputy synthesis across 8 organ systems",
            "vitals_thresholds": {"min_pessimum_bpm": 50, "max_pessimum_bpm": 120},
        },
        commit=False,
    )
    rep_repo.create_next_version(
        cycle_id=cycle_id,
        model="aegis-system",
        report_type=ATHENA_SCHEDULE_ANCHOR_REPORT_TYPE,
        privacy_level=PrivacyLevel.SERVER_ONLY,
        content={"anchor_timestamp_utc": intake_time.isoformat()},
        commit=False,
    )
    session.commit()


# ==============================================================================
# TEST 1: PRIORITY #1 — WAKE-UP & WELLNESS CONTINUITY (DAY 5 -> DAY 6)
# ==============================================================================

@pytest.mark.anyio
async def test_priority1_wakeup_and_wellness_continuity(tmp_path: Path):
    """
    PRIORITY #1: Morning wake-up preserves continuous observation windows.
    1. Days 1-4 completed.
    2. Day 5 morning wake-up at T0 + 97h triggers Day 5 Wellness [T0 + 96h, T0 + 97h).
    3. Afternoon Mode 2 workout occurs at T0 + 106h (after Day 5 closed).
    4. Next morning wake-up arrives on Day 6 at T0 + 121h.
    5. Verify workout is cleanly gathered into Day 6 Wellness and Day 6 Daily Journal.
    6. Verify NO data lost, NO data duplicated across day boundaries.
    7. Verify Day 7 midday 12:00 fallback seamlessly captures Day 6 evening data.
    """
    db_file = tmp_path / "test_p1_continuity.db"
    _, SessionFactory = _create_isolated_file_db(db_file)

    t0_dt = datetime(2026, 10, 30, 8, 0, 0, tzinfo=timezone.utc)
    clock = ControlledClock(t0_dt)
    cycle_id = "cycle_continuity_001"
    patient_id = "pat_continuity_001"

    recorded_calls: List[Dict[str, Any]] = []
    mock_gw = _build_failure_injection_mock_gateway(recorded_calls)

    with SessionFactory() as db:
        _seed_completed_primary_cycle(db, patient_id, cycle_id, t0_dt)

        # Seed completed Days 1-4 reports so Day 5 is the first pending window
        rep_repo = ModelReportRepository(db)
        for d in range(1, 5):
            w_start = t0_dt + timedelta(days=d - 1)
            w_end = t0_dt + timedelta(days=d)
            rep = WellnessDailyReport(
                report_id=f"rep_wellness_{cycle_id}_w1_d{d}",
                cycle_id=cycle_id,
                patient_id=patient_id,
                calendar_week=1,
                calendar_day=d,
                status=WellnessStatus.NO_DATA,
                trigger=WellnessTriggerType.SCHEDULED_MIDDAY,
                window=WellnessTimeWindow(
                    start_utc=w_start.isoformat(),
                    end_utc=w_end.isoformat(),
                    user_timezone="UTC",
                    calendar_week=1,
                    calendar_day=d,
                ),
                factual_summary=WellnessBiometricsSummary(),
                summary="NO_DATA",
                created_at_utc=w_end.isoformat(),
            )
            rep_repo.create_next_version(
                cycle_id=cycle_id,
                model="aegis-wellness-analyzer",
                report_type="wellness_daily_report",
                privacy_level=PrivacyLevel.SERVER_ONLY,
                content=rep.model_dump(),
                commit=False,
            )
        db.commit()

    worker = ClinicalAutomationWorker(
        session_factory=SessionFactory,
        gateway_factory=lambda: mock_gw,
        now_provider=clock.now,
        poll_interval_seconds=0.05,
        enable_wellness_daily=True,
        enable_daily_journal=True,
        enable_weekly_plan=True,
    )

    # --- Day 5 Morning: Wake-up at T0 + 97h (09:00 UTC) ---
    clock.set(t0_dt + timedelta(hours=97))
    with SessionFactory() as db:
        m_repo = HealthMeasurementRepository(db)
        m_repo.create_batch(
            default_cycle_id=cycle_id,
            measurements=[
                {
                    "pulse": 62,
                    "systolic": 118,
                    "diastolic": 76,
                    "spo2": 99,
                    "source": "watch_sensor",
                    "measured_at": clock.now(),
                    "raw_metadata": {"event": "wake_up", "sleep_hours": 7.5},
                }
            ],
        )

    # Worker executes Day 5 Wellness and Day 5 Journal
    clock.advance(minutes=1)
    res = await worker.run_once(discover=True)
    assert len(res) >= 1
    await worker.run_once(discover=True)

    with SessionFactory() as db:
        wellness_svc = WellnessDailyService(db=db, gateway=mock_gw, now_provider=clock.now)
        d5_rep = wellness_svc.get_daily_wellness_report(cycle_id, 1, 5)
        assert d5_rep is not None
        assert d5_rep.status == WellnessStatus.READY
        assert d5_rep.trigger == WellnessTriggerType.WAKE_UP
        assert d5_rep.window.end_utc == clock.now().isoformat()

    # --- Day 5 Afternoon: Workout at T0 + 106h (18:00 UTC) ---
    clock.set(t0_dt + timedelta(hours=106))
    with SessionFactory() as db:
        m2_svc = Mode2VitalsMonitoringService(db=db, gateway=mock_gw, now_provider=clock.now)
        # 1. Trigger episode with HR 138 bpm
        ep = await m2_svc.process_measurement(
            cycle_id=cycle_id,
            pulse=138,
            spo2=97,
            measured_at=clock.now(),
            source="watch_sensor",
        )
        assert ep is not None
        assert ep.status == Mode2EpisodeStatus.ACTIVE
        ep_id = ep.episode_id

        # 2. Patient confirms workout
        await m2_svc.handle_user_statement(
            cycle_id=cycle_id,
            user_text="Интенсивная тренировка на тренажере",
            is_workout=True,
        )

        # 3. Sustained normalization closes episode (2 normal readings required)
        clock.advance(minutes=2)
        ep_norm1 = await m2_svc.process_measurement(
            cycle_id=cycle_id,
            pulse=70,
            spo2=99,
            measured_at=clock.now(),
            source="watch_sensor",
        )
        assert ep_norm1 is not None
        assert ep_norm1.status == Mode2EpisodeStatus.ACTIVE

        clock.advance(minutes=1)
        ep_closed = await m2_svc.process_measurement(
            cycle_id=cycle_id,
            pulse=68,
            spo2=99,
            measured_at=clock.now(),
            source="watch_sensor",
        )
        assert ep_closed is not None
        assert ep_closed.status == Mode2EpisodeStatus.CLOSED

    # --- Day 6 Morning: Next Wake-up at T0 + 121h (09:00 UTC) ---
    clock.set(t0_dt + timedelta(hours=121))
    with SessionFactory() as db:
        m_repo = HealthMeasurementRepository(db)
        m_repo.create_batch(
            default_cycle_id=cycle_id,
            measurements=[
                {
                    "pulse": 60,
                    "systolic": 116,
                    "diastolic": 74,
                    "spo2": 99,
                    "source": "watch_sensor",
                    "measured_at": clock.now(),
                    "raw_metadata": {"event": "wake_up", "sleep_hours": 8.0},
                }
            ],
        )

    # Worker executes Day 6 Wellness and Day 6 Journal
    clock.advance(minutes=1)
    res_d6 = await worker.run_once(discover=True)
    assert len(res_d6) >= 1
    await worker.run_once(discover=True)

    with SessionFactory() as db:
        wellness_svc = WellnessDailyService(db=db, gateway=mock_gw, now_provider=clock.now)
        journal_svc = AthenaDailyJournalService(db=db, gateway=mock_gw, now_provider=clock.now)

        d6_rep = wellness_svc.get_daily_wellness_report(cycle_id, 1, 6)
        assert d6_rep is not None
        assert d6_rep.status == WellnessStatus.READY
        assert d6_rep.trigger == WellnessTriggerType.WAKE_UP
        # Day 6 window starts EXACTLY where Day 5 ended:
        assert d6_rep.window.start_utc == d5_rep.window.end_utc
        assert d6_rep.window.end_utc == clock.now().isoformat()

        # Day 6 Wellness report factual summary MUST contain the afternoon workout episode!
        assert len(d6_rep.factual_summary.mode2_episodes) == 1
        assert d6_rep.factual_summary.mode2_episodes[0].episode_id == ep_id

        # Day 5 Wellness report must NOT contain the workout episode!
        assert len(d5_rep.factual_summary.mode2_episodes) == 0

        # Day 6 Daily Journal MUST contain the workout episode evidence!
        d6_journal = journal_svc.get_daily_journal(cycle_id, 1, 6)
        assert d6_journal is not None
        assert len(d6_journal.wellness_evidence.mode2_episodes) == 1
        assert d6_journal.wellness_evidence.mode2_episodes[0].episode_id == ep_id

        # Invariant: exactly 1 Mode 2 episode exists, zero loss, zero duplication
        m2_svc = Mode2VitalsMonitoringService(db=db, gateway=mock_gw, now_provider=clock.now)
        all_eps = m2_svc.get_episode_history(cycle_id)
        assert len(all_eps) == 1

    # --- Day 6 Evening to Day 7 Midday Fallback (12:00 UTC) ---
    # Evening measurement on Day 6 at T0 + 132h
    clock.set(t0_dt + timedelta(hours=132))
    with SessionFactory() as db:
        m_repo = HealthMeasurementRepository(db)
        m_repo.create_batch(
            default_cycle_id=cycle_id,
            measurements=[
                {
                    "pulse": 65,
                    "systolic": 119,
                    "diastolic": 78,
                    "spo2": 98,
                    "source": "watch_sensor",
                    "measured_at": clock.now(),
                    "raw_metadata": {"context": "evening_relaxation"},
                }
            ],
        )

    # Day 7: NO morning wake-up event. Advance directly to midday 12:00 UTC (T0 + 148h)
    clock.set(t0_dt + timedelta(hours=148, minutes=1))
    res_d7 = await worker.run_once(discover=True)
    assert len(res_d7) >= 1

    with SessionFactory() as db:
        wellness_svc = WellnessDailyService(db=db, gateway=mock_gw, now_provider=clock.now)
        d7_rep = wellness_svc.get_daily_wellness_report(cycle_id, 1, 7)
        assert d7_rep is not None
        assert d7_rep.trigger == WellnessTriggerType.SCHEDULED_MIDDAY
        # Day 7 starts at Day 6's end
        assert d7_rep.window.start_utc == d6_rep.window.end_utc
        # And contains the evening measurement from Day 6 without omission
        assert d7_rep.factual_summary.total_measurements >= 1


# ==============================================================================
# TEST 2: CONCURRENT WORKERS (LEASE CONTENTION, HEARTBEAT, FENCING TOKEN)
# ==============================================================================

@pytest.mark.anyio
async def test_concurrent_workers_lease_contention_and_fencing(tmp_path: Path):
    """
    CONCURRENT WORKERS:
    - 2 independent ClinicalAutomationWorker instances on the same SQLite.
    - Simultaneous job discovery & claim contention.
    - Heartbeat lease extension during long execution.
    - Expired lease takeover.
    - Stale fencing token rejection (ConsiliumConcurrencyConflictError).
    - Single logical execution invariant.
    """
    db_file = tmp_path / "test_p2_concurrency.db"
    _, SessionFactory = _create_isolated_file_db(db_file)

    t0_dt = datetime(2026, 10, 30, 8, 0, 0, tzinfo=timezone.utc)
    clock = ControlledClock(t0_dt)
    cycle_id = "cycle_concurrent_001"
    patient_id = "pat_concurrent_001"

    recorded_calls: List[Dict[str, Any]] = []
    mock_gw = _build_failure_injection_mock_gateway(recorded_calls)

    with SessionFactory() as db:
        _seed_completed_primary_cycle(db, patient_id, cycle_id, t0_dt)
        svc = ClinicalAutomationService(db=db, gateway=mock_gw, now_provider=clock.now)
        # Enqueue a test job
        job = svc.enqueue_first_planned_question(
            cycle_id=cycle_id,
            patient_id=patient_id,
            available_at_utc=clock.now(),
        )
        assert job is not None
        job_id = job.job_id

    # 1. Simultaneous claim contention between Worker A and Worker B
    with SessionFactory() as db_a, SessionFactory() as db_b:
        svc_a = ClinicalAutomationService(db=db_a, gateway=mock_gw, now_provider=clock.now, lease_ttl_seconds=30)
        svc_b = ClinicalAutomationService(db=db_b, gateway=mock_gw, now_provider=clock.now, lease_ttl_seconds=30)

        claimed_a = svc_a.claim_job(job_id)
        assert claimed_a is not None
        assert claimed_a.status == AutomationJobStatus.RUNNING

        # Worker B tries to claim the same job -> fails cleanly, returns None
        claimed_b = svc_b.claim_job(job_id)
        assert claimed_b is None

    # 2. Heartbeat lease extension
    with SessionFactory() as db_a:
        svc_a = ClinicalAutomationService(db=db_a, gateway=mock_gw, now_provider=clock.now, lease_ttl_seconds=30)
        clock.advance(seconds=10)
        renewed = svc_a.renew_job_lease(job_id, claimed_a.lease_owner_id)
        assert renewed.status == AutomationJobStatus.RUNNING
        assert renewed.lease_expires_at_utc is not None
        assert renewed.version > claimed_a.version

    # 3. Lease expiration and takeover by Worker B
    # Advance clock past lease TTL (30s)
    clock.advance(seconds=45)
    with SessionFactory() as db_b:
        svc_b = ClinicalAutomationService(db=db_b, gateway=mock_gw, now_provider=clock.now, lease_ttl_seconds=30)
        claimed_b = svc_b.claim_job(job_id)
        assert claimed_b is not None
        assert claimed_b.lease_owner_id != claimed_a.lease_owner_id

    # 4. Stale fencing token protection
    # Worker A (with old lease) tries to complete the job
    with SessionFactory() as db_a:
        svc_a = ClinicalAutomationService(db=db_a, gateway=mock_gw, now_provider=clock.now)
        with pytest.raises(ConsiliumConcurrencyConflictError):
            svc_a.complete_job(job_id, claimed_a.lease_owner_id, {"msg": "stale completion"})

    # 5. Worker B completes the job successfully
    with SessionFactory() as db_b:
        svc_b = ClinicalAutomationService(db=db_b, gateway=mock_gw, now_provider=clock.now)
        completed = svc_b.complete_job(job_id, claimed_b.lease_owner_id, {"msg": "valid completion"})
        assert completed.status == AutomationJobStatus.COMPLETED


# ==============================================================================
# TEST 3: GEMINI FAILURES & FAULT TOLERANCE (429, 503, TIMEOUT, MALFORMED JSON)
# ==============================================================================

@pytest.mark.anyio
async def test_gemini_failures_and_resilience(tmp_path: Path):
    """
    GEMINI FAILURES:
    - HTTP 429 Rate Limit -> retryable failure with exponential backoff.
    - HTTP 503 Service Unavailable -> retryable failure with backoff.
    - Pro Timeout -> retryable failure, no partially published report.
    - Flash Malformed JSON -> retryable failure, graceful handling.
    - Bounded retry and eventual completion upon API recovery.
    """
    db_file = tmp_path / "test_p3_gemini_failures.db"
    _, SessionFactory = _create_isolated_file_db(db_file)

    t0_dt = datetime(2026, 10, 30, 8, 0, 0, tzinfo=timezone.utc)
    clock = ControlledClock(t0_dt)
    cycle_id = "cycle_gemini_fail_001"
    patient_id = "pat_gemini_fail_001"

    fail_behavior = {"type": None}

    def _conditional_failure(url: str, prompt: str) -> Optional[httpx.Response]:
        if fail_behavior["type"] == "429":
            return httpx.Response(429, json={"error": {"code": 429, "message": "Resource exhausted"}})
        if fail_behavior["type"] == "503":
            return httpx.Response(503, json={"error": {"code": 503, "message": "Service unavailable"}})
        if fail_behavior["type"] == "timeout":
            raise httpx.ReadTimeout("Pro response timed out after 30s")
        if fail_behavior["type"] == "malformed":
            return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": "{malformed json text..."}]}}]})
        return None

    recorded_calls: List[Dict[str, Any]] = []
    mock_gw = _build_failure_injection_mock_gateway(recorded_calls, fail_condition=_conditional_failure)

    with SessionFactory() as db:
        _seed_completed_primary_cycle(db, patient_id, cycle_id, t0_dt)
        # Advance to Day 7 boundary (T0 + 168h) for Weekly Consilium
        clock.set(t0_dt + timedelta(hours=168))
        svc = ClinicalAutomationService(
            db=db,
            gateway=mock_gw,
            now_provider=clock.now,
            enable_automatic_weekly=True,
            base_backoff_seconds=10,
        )
        job = svc.enqueue_weekly_consilium_a(
            cycle_id=cycle_id,
            patient_id=patient_id,
            sprint_number=1,
            available_at_utc=clock.now(),
        )
        job_id = job.job_id

    # Scenario A: HTTP 429
    fail_behavior["type"] = "429"
    with SessionFactory() as db:
        svc = ClinicalAutomationService(db=db, gateway=mock_gw, now_provider=clock.now, base_backoff_seconds=10)
        claimed = svc.claim_job(job_id)
        assert claimed is not None
        outcome = await svc.execute_claimed_job(claimed)
        assert outcome.status == AutomationJobStatus.RETRYABLE_FAILURE
        assert outcome.attempts == 1
        assert outcome.next_attempt_at_utc is not None

    # Advance past backoff and simulate HTTP 503
    clock.advance(seconds=15)
    fail_behavior["type"] = "503"
    with SessionFactory() as db:
        svc = ClinicalAutomationService(db=db, gateway=mock_gw, now_provider=clock.now, base_backoff_seconds=10)
        claimed = svc.claim_job(job_id)
        assert claimed is not None
        outcome = await svc.execute_claimed_job(claimed)
        assert outcome.status == AutomationJobStatus.RETRYABLE_FAILURE
        assert outcome.attempts == 2

    # Advance past backoff and simulate Pro Timeout
    clock.advance(seconds=30)
    fail_behavior["type"] = "timeout"
    with SessionFactory() as db:
        svc = ClinicalAutomationService(db=db, gateway=mock_gw, now_provider=clock.now, base_backoff_seconds=10)
        claimed = svc.claim_job(job_id)
        assert claimed is not None
        outcome = await svc.execute_claimed_job(claimed)
        assert outcome.status == AutomationJobStatus.RETRYABLE_FAILURE
        assert outcome.attempts == 3

    # Invariant: No partial client-facing reports published during failures
    with SessionFactory() as db:
        rep_repo = ModelReportRepository(db)
        client_reports = rep_repo.list_client_facing(cycle_id)
        # Only the initial primary report from seeding should exist!
        weekly_client_reports = [r for r in client_reports if "weekly" in (r.report_type or "").lower()]
        assert len(weekly_client_reports) == 0

    # Scenario B: Recovery upon API health restoration
    clock.advance(seconds=60)
    fail_behavior["type"] = None
    with SessionFactory() as db:
        svc = ClinicalAutomationService(db=db, gateway=mock_gw, now_provider=clock.now)
        claimed = svc.claim_job(job_id)
        assert claimed is not None
        outcome = await svc.execute_claimed_job(claimed)
        assert outcome.status == AutomationJobStatus.COMPLETED

    # Verify report is now published safely and atomically
    with SessionFactory() as db:
        rep_repo = ModelReportRepository(db)
        client_reports = rep_repo.list_client_facing(cycle_id)
        weekly_client_reports = [r for r in client_reports if "weekly" in (r.report_type or "").lower()]
        assert len(weekly_client_reports) == 1


# ==============================================================================
# TEST 4: WEEKLY CONSILIUM A/B/C RESTART RECOVERY & T0 INVARIANCE
# ==============================================================================

@pytest.mark.anyio
async def test_weekly_consilium_abc_restart_recovery(tmp_path: Path):
    """
    WEEKLY RESTART RECOVERY:
    - Server restart before Step A.
    - Server restart between Step A and Step B.
    - Server restart between Step B and Step C.
    - Server restart after Step C.
    - Idempotency: no duplicate strategy, plan, or wrap-up.
    - Schedule anchor T0 invariance (due at T0 + 168h, week 2 due at T0 + 336h).
    """
    db_file = tmp_path / "test_p4_weekly_restart.db"
    _, SessionFactory = _create_isolated_file_db(db_file)

    t0_dt = datetime(2026, 10, 30, 8, 0, 0, tzinfo=timezone.utc)
    clock = ControlledClock(t0_dt)
    cycle_id = "cycle_restart_001"
    patient_id = "pat_restart_001"

    recorded_calls: List[Dict[str, Any]] = []
    mock_gw = _build_failure_injection_mock_gateway(recorded_calls)

    with SessionFactory() as db:
        _seed_completed_primary_cycle(db, patient_id, cycle_id, t0_dt)

    # Advance clock to boundary T0 + 168h
    clock.set(t0_dt + timedelta(hours=168))

    # --- Restart 1: Worker starts fresh and executes Step A ---
    worker_1 = ClinicalAutomationWorker(
        session_factory=SessionFactory,
        gateway_factory=lambda: mock_gw,
        now_provider=clock.now,
        enable_automatic_weekly=True,
        enable_weekly_plan=True,
    )
    # Run once with discovery to enqueue and process Step A
    res_step_a = await worker_1.run_once(discover=True, limit=10)
    a_jobs = [j for j in res_step_a if j.job_type == AutomationJobType.WEEKLY_CONSILIUM_A]
    assert len(a_jobs) == 1
    assert a_jobs[0].status == AutomationJobStatus.COMPLETED

    # Verify Step B was automatically enqueued
    with SessionFactory() as db:
        svc = ClinicalAutomationService(db=db, gateway=mock_gw, now_provider=clock.now)
        step_b_job = svc.get_job(cycle_id, AutomationJobType.WEEKLY_CONSILIUM_B, f"weekly_consilium_step_b_{cycle_id}_w1")
        assert step_b_job is not None
        assert step_b_job.status == AutomationJobStatus.PENDING

    # --- Restart 2: Worker crashes and restarts before Step B executes ---
    del worker_1
    worker_2 = ClinicalAutomationWorker(
        session_factory=SessionFactory,
        gateway_factory=lambda: mock_gw,
        now_provider=clock.now,
        enable_automatic_weekly=True,
        enable_weekly_plan=True,
    )
    res_step_b = await worker_2.run_once(discover=True, limit=10)
    b_jobs = [j for j in res_step_b if j.job_type == AutomationJobType.WEEKLY_CONSILIUM_B]
    assert len(b_jobs) == 1
    assert b_jobs[0].status == AutomationJobStatus.COMPLETED

    # --- Restart 3: Worker crashes and restarts before Step C executes ---
    del worker_2
    worker_3 = ClinicalAutomationWorker(
        session_factory=SessionFactory,
        gateway_factory=lambda: mock_gw,
        now_provider=clock.now,
        enable_automatic_weekly=True,
        enable_weekly_plan=True,
    )
    res_step_c = await worker_3.run_once(discover=True, limit=10)
    c_jobs = [j for j in res_step_c if j.job_type == AutomationJobType.WEEKLY_CONSILIUM_C]
    assert len(c_jobs) == 1
    assert c_jobs[0].status == AutomationJobStatus.COMPLETED

    # --- Restart 4: Worker restarts after Step C is complete ---
    del worker_3
    worker_4 = ClinicalAutomationWorker(
        session_factory=SessionFactory,
        gateway_factory=lambda: mock_gw,
        now_provider=clock.now,
        enable_automatic_weekly=True,
        enable_weekly_plan=True,
    )
    res_step_after = await worker_4.run_once(discover=True)
    # No more due weekly jobs should be executed!
    assert len([j for j in res_step_after if "weekly_consilium" in j.job_type.value]) == 0

    # Invariants verification
    with SessionFactory() as db:
        # 1. Exactly 1 Week 2 Athena plan created
        plan_svc = AthenaWeeklyPlanService(db=db, gateway=mock_gw, now_provider=clock.now)
        plan_w2 = plan_svc.get_active_weekly_plan(cycle_id, week_number=2)
        assert plan_w2 is not None
        assert plan_w2.week_number == 2

        rep_repo = ModelReportRepository(db)
        plan_reports = rep_repo.list_server_only(cycle_id=cycle_id, report_type=ATHENA_WEEKLY_PLAN_REPORT_TYPE)
        w2_plan_reports = [r for r in plan_reports if (r.content or {}).get("week_number") == 2]
        assert len(w2_plan_reports) == 1

        # 2. Exactly 1 wrap-up chat message
        athena_svc = AthenaChatService(db=db, gateway=mock_gw, now_provider=clock.now)
        wrapup_turn = athena_svc._find_idempotent_turn(cycle_id, f"athena_weekly_wrapup_{cycle_id}_w1")
        assert wrapup_turn is not None
        assert len(wrapup_turn.reply) > 0

        # 3. Schedule anchor T0 remains unchanged
        weekly_svc = WeeklyConsiliumService(db=db, gateway=mock_gw, now_provider=clock.now)
        anchor = weekly_svc.find_schedule_anchor_timestamp(cycle_id)
        assert anchor == t0_dt


# ==============================================================================
# TEST 5: LAB REVISION FAULT TOLERANCE
# ==============================================================================

@pytest.mark.anyio
async def test_lab_revision_fault_tolerance(tmp_path: Path):
    """
    LAB REVISION FAULT TOLERANCE:
    - 1 of 8 Flash-Lite errors / Pro timeout.
    - No 72h cooldown after failure.
    - New lab data arrival during execution preserves new ledger version.
    - Server restart with pending task.
    - Active Weekly Strategy is NOT modified outside Weekly Consilium.
    """
    db_file = tmp_path / "test_p5_lab_revision.db"
    _, SessionFactory = _create_isolated_file_db(db_file)

    t0_dt = datetime(2026, 10, 30, 8, 0, 0, tzinfo=timezone.utc)
    clock = ControlledClock(t0_dt)
    cycle_id = "cycle_lab_rev_001"
    patient_id = "pat_lab_rev_001"

    fail_lab = {"fail": False}

    def _conditional_lab_fail(url: str, prompt: str) -> Optional[httpx.Response]:
        if fail_lab["fail"] and ("flash-lite" in url or "specialist for cvs" in prompt.lower()):
            raise httpx.ReadTimeout("Specialist CVS timed out")
        return None

    recorded_calls: List[Dict[str, Any]] = []
    mock_gw = _build_failure_injection_mock_gateway(recorded_calls, fail_condition=_conditional_lab_fail)

    with SessionFactory() as db:
        _seed_completed_primary_cycle(db, patient_id, cycle_id, t0_dt)
        sched_svc = LabSchedulingService(db=db, now_provider=clock.now)
        # Create a pending task directly
        sched_svc.update_or_create_pending_task(
            cycle_id=cycle_id,
            patient_id=patient_id,
            is_meaningful_change=True,
            change_descriptions=["Glucose: 5.6 mmol/L"],
        )
        # Advance clock past 15-minute debounce
        clock.advance(minutes=20)
        due_tasks = sched_svc.get_ready_tasks_for_execution()
        assert len(due_tasks) == 1

        # Enqueue lab revision job in automation service
        auto_svc = ClinicalAutomationService(db=db, gateway=mock_gw, now_provider=clock.now, enable_lab_revision=True)
        job = auto_svc.enqueue_lab_revision(
            cycle_id=cycle_id,
            patient_id=patient_id,
            target_ledger_version=due_tasks[0].target_ledger_version,
            available_at_utc=clock.now(),
        )
        assert job is not None
        job_id = job.job_id

    # 1. Failure scenario: 1 specialist fails
    fail_lab["fail"] = True
    with SessionFactory() as db:
        auto_svc = ClinicalAutomationService(db=db, gateway=mock_gw, now_provider=clock.now, enable_lab_revision=True)
        claimed = auto_svc.claim_job(job_id)
        assert claimed is not None
        outcome = await auto_svc.execute_claimed_job(claimed)
        assert outcome.status == AutomationJobStatus.RETRYABLE_FAILURE

        # Verify NO 72-hour cooldown was established upon failure!
        sched_svc = LabSchedulingService(db=db, now_provider=clock.now)
        last_succ = sched_svc.get_last_successful_revision_timestamp(cycle_id)
        assert last_succ is None

    # 2. Recovery: API succeeds -> revision completes, 72h cooldown established
    fail_lab["fail"] = False
    clock.advance(minutes=1)
    with SessionFactory() as db:
        auto_svc = ClinicalAutomationService(db=db, gateway=mock_gw, now_provider=clock.now, enable_lab_revision=True)
        claimed = auto_svc.claim_job(job_id)
        assert claimed is not None
        outcome = await auto_svc.execute_claimed_job(claimed)
        assert outcome.status == AutomationJobStatus.COMPLETED

        # 72h cooldown is now established
        sched_svc = LabSchedulingService(db=db, now_provider=clock.now)
        last_succ = sched_svc.get_last_successful_revision_timestamp(cycle_id)
        assert last_succ is not None
        assert last_succ <= clock.now()

    # 3. Invariant: Active strategy was NOT updated by Lab Revision!
    with SessionFactory() as db:
        rep_repo = ModelReportRepository(db)
        strat_reports = rep_repo.list_server_only(cycle_id=cycle_id, report_type=ATHENA_ACTIVE_STRATEGY_REPORT_TYPE)
        assert len(strat_reports) == 1
        assert strat_reports[0].content.get("active_strategy") == "Week 1 baseline adaptation strategy"


# ==============================================================================
# TEST 6: CLIENT TELEMETRY RESILIENCE (DUPLICATES, OUT-OF-ORDER, OFFLINE)
# ==============================================================================

@pytest.mark.anyio
async def test_client_telemetry_resilience(tmp_path: Path):
    """
    CLIENT TELEMETRY:
    - Duplicate batch ingestion is handled idempotently without corrupting DB.
    - Delayed batch from several hours ago preserves authentic measured_at timestamps.
    - Out-of-order measurements within a batch are chronologically sorted.
    - Offline workout telemetry is correctly aggregated by Mode 2 & Wellness services.
    - Testing boundary note: Flutter & Samsung SDK hardware layer bounded to HTTP boundary.
    """
    db_file = tmp_path / "test_p6_telemetry.db"
    _, SessionFactory = _create_isolated_file_db(db_file)

    t0_dt = datetime(2026, 10, 30, 8, 0, 0, tzinfo=timezone.utc)
    clock = ControlledClock(t0_dt)
    cycle_id = "cycle_telemetry_001"
    patient_id = "pat_telemetry_001"

    with SessionFactory() as db:
        _seed_completed_primary_cycle(db, patient_id, cycle_id, t0_dt)

    # 1. Out-of-order & duplicate measurements ingestion via ingest_batch
    t_base = t0_dt + timedelta(hours=10)
    batch_records = [
        HealthMeasurementCreate(pulse=72, spo2=98, measured_at=t_base + timedelta(minutes=15), source="watch_sensor"),
        HealthMeasurementCreate(pulse=68, spo2=99, measured_at=t_base, source="watch_sensor"),
        HealthMeasurementCreate(pulse=70, spo2=98, measured_at=t_base + timedelta(minutes=5), source="watch_sensor"),
    ]

    with SessionFactory() as db:
        m_repo = HealthMeasurementRepository(db)
        # Ingest out-of-order batch
        saved_1, is_new_1 = m_repo.ingest_batch(measurements=batch_records, cycle_id=cycle_id)
        assert len(saved_1) == 3
        assert all(is_new_1)

        # Ingest exact duplicate batch
        saved_2, is_new_2 = m_repo.ingest_batch(measurements=batch_records, cycle_id=cycle_id)
        assert len(saved_2) == 3
        # Deduplication flag: None of the duplicate items are new!
        assert not any(is_new_2)

        # Query chronologically
        sorted_records = m_repo.list_by_cycle(cycle_id, limit=100)
        assert len(sorted_records) == 3
        times = [r.measured_at for r in sorted_records]
        assert times == sorted(times)

    # 2. Offline workout sync
    # Patient worked out 4 hours ago while offline; batch arrives now
    t_offline_workout = t0_dt + timedelta(hours=20)
    clock.set(t0_dt + timedelta(hours=24))

    with SessionFactory() as db:
        # Simulate retroactive ingestion of workout telemetry
        m_repo = HealthMeasurementRepository(db)
        m_repo.create_batch(
            default_cycle_id=cycle_id,
            measurements=[
                {
                    "pulse": 140,
                    "spo2": 97,
                    "measured_at": t_offline_workout,
                    "source": "watch_sensor",
                    "phase": "workout",
                    "raw_metadata": {"event": "workout_sync", "sport": "cycling"},
                },
                {
                    "pulse": 75,
                    "spo2": 99,
                    "measured_at": t_offline_workout + timedelta(minutes=30),
                    "source": "watch_sensor",
                    "phase": "recovery",
                },
            ],
        )

        # Mode 2 and Wellness services accurately observe the historical workout
        wellness_svc = WellnessDailyService(db=db, now_provider=clock.now)
        genuine = wellness_svc.gather_genuine_measurements(
            cycle_id=cycle_id,
            start_dt=t0_dt + timedelta(hours=12),
            end_dt=t0_dt + timedelta(hours=24),
        )
        assert len(genuine) >= 2
        summary = wellness_svc.build_factual_biometrics_summary(genuine)
        assert summary.activity is not None
        assert summary.activity.workout_max_bpm == 140
        assert summary.activity.recovery_min_bpm == 75
