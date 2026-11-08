import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
import uuid

import httpx
import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from app.config import settings
from app.database import Base
from app.models.medical_cycle import MedicalCycle
from app.models.model_report import ModelReport
from app.models.patient import Patient
from app.models.weekly_sprint import WeeklySprint
from app.models.health_measurement import HealthMeasurement
from app.repositories.cycle_repository import MedicalCycleRepository
from app.repositories.patient_repository import PatientRepository
from app.repositories.report_repository import ModelReportRepository
from app.repositories.sprint_repository import WeeklySprintRepository
from app.schemas.chat import (
    AthenaChatRequest,
    AthenaChatResponse,
    AthenaDailyAssessmentModelOutput,
    AthenaDailyJournalRecord,
    AthenaWeeklyPlanRecord,
    AthenaGoalItem,
    AthenaGoalStatus,
    AthenaWeeklyWrapupOutput,
)
from app.schemas.consilium import (
    AegisAgentRole,
    WeeklyFlashMasterOutput,
    WeeklyProMasterOutput,
    WeeklyConsiliumClientResponse,
    SYSTEM_KEYS,
)
from app.schemas.privacy import PrivacyLevel
from app.services.athena_chat_service import (
    AthenaChatService,
    ATHENA_CHAT_TURN_REPORT_TYPE,
    ATHENA_ROUTINE_QUESTION_REPORT_TYPE,
)
from app.services.athena_daily_journal_service import (
    AthenaDailyJournalService,
    ATHENA_DAILY_JOURNAL_REPORT_TYPE,
    ATHENA_DAILY_JOURNAL_MODEL,
)
from app.services.athena_weekly_plan_service import (
    AthenaWeeklyPlanService,
    ATHENA_WEEKLY_PLAN_REPORT_TYPE,
)
from app.services.clinical_automation_service import (
    AutomationJobRecord,
    AutomationJobStatus,
    AutomationJobType,
    ClinicalAutomationService,
    ClinicalAutomationWorker,
)
from app.services.gemini_gateway import GeminiGateway, GeminiModelRole
from app.services.primary_consilium_service import (
    PrimaryConsiliumService,
    PREPARATORY_ANAMNESIS_REPORT_TYPE,
    CYCLE_CONSILIUM_COORDINATION_LOCK_REPORT_TYPE,
)

CHIEF_PHYSICIAN_INTERNAL_REPORT_TYPE = "chief_physician_internal"
CHIEF_PHYSICIAN_USER_REPORT_TYPE = "chief_physician_user"
from app.services.weekly_consilium_service import (
    WeeklyConsiliumService,
    ATHENA_ACTIVE_STRATEGY_REPORT_TYPE,
    ATHENA_SCHEDULE_ANCHOR_REPORT_TYPE,
    WEEKLY_OVERALL_CLIENT_REPORT_TYPE,
    SYSTEM_KEYS,
)
from app.services.wellness_daily_service import (
    WellnessDailyService,
    WELLNESS_DAILY_REPORT_TYPE,
)
from app.schemas.wellness import (
    WellnessModelOutput,
    WellnessStatus,
    WellnessDailyReport,
)


class ControlledClock:
    def __init__(self, start_dt: datetime):
        self._current = start_dt

    def now(self) -> datetime:
        return self._current

    def advance(self, duration: timedelta) -> None:
        self._current += duration

    def set(self, target_dt: datetime) -> None:
        self._current = target_dt


def _create_isolated_db(db_path: Path):
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False, "timeout": 30.0},
    )

    @event.listens_for(engine, "connect")
    def set_sqlite_pragma(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.close()

    Base.metadata.create_all(bind=engine)
    SessionFactory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    return engine, SessionFactory


def _gemini_envelope(payload: Any) -> Dict[str, Any]:
    text = json.dumps(payload, ensure_ascii=False) if not isinstance(payload, str) else payload
    return {
        "candidates": [
            {
                "content": {
                    "parts": [{"text": text}],
                    "role": "model",
                },
                "finishReason": "STOP",
                "index": 0,
            }
        ]
    }


def _sample_weekly_flash_output() -> Dict[str, Any]:
    system_reports = {
        k: {
            "summary": f"Стабильная положительная динамика системы {k}.",
            "has_changes": True,
        }
        for k in SYSTEM_KEYS
    }
    return {
        "weekly_deputy_analysis": "Заместитель главного врача: клинические показатели стабильны.",
        "user_system_reports": system_reports,
    }


def _sample_weekly_pro_output(
    strategy_status: str = "updated",
    approved: bool = True,
    updated_strategy: Optional[str] = None,
) -> Dict[str, Any]:
    default_strategy = (
        "УТВЕРЖДЁННАЯ СТРАТЕГИЯ PRO НА СЛЕДУЮЩУЮ НЕДЕЛЮ:\n"
        "1. Контроль адаптации к вечерней нагрузке.\n"
        "2. Уточнение гидратации во второй половине дня.\n"
        "3. Оценка динамики восстановления ЧСС после умеренной ходьбы."
    )
    return {
        "updated_weekly_strategy": updated_strategy or default_strategy,
        "strategy_status": strategy_status,
        "weekly_overall_report": {
            "title": "Итоги недели 1: Стабильный прогресс",
            "summary": "Отличная неделя: пульс в покое стабилизировался, уровень дневной энергии вырос.",
            "highlights": [
                "Пульс покоя снизился в среднем на 3 уд/мин",
                "Физическая активность переносится удовлетворительно",
            ],
        },
        "flash_quality_review": {
            "approved_for_publication": approved,
            "rejection_reason": None,
            "corrected_user_system_reports": {},
        },
    }


def _sample_weekly_plan(cycle_id: str, week_number: int = 1) -> Dict[str, Any]:
    return {
        "plan_id": f"plan_{cycle_id}_w{week_number}",
        "cycle_id": cycle_id,
        "week_number": week_number,
        "source_strategy_report_id": f"rep_{cycle_id}_strat_v1",
        "source_strategy_version": 1,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "summary_for_curator": f"План недели {week_number}: контроль восстановления ЧСС",
        "goals": [
            {
                "goal_id": f"goal_{week_number}_1",
                "title": "Оценка самочувствия после вечерней ходьбы",
                "clinical_rationale": "Необходимо убедиться в отсутствии одышки при физической нагрузке",
                "priority": 1,
                "known_facts": ["Базовая переносимость удовлетворительная"],
                "target_clarification": "Уточнить наличие одышки или сердцебиения",
                "sufficiency_criteria": "Пациент ответил о наличии или отсутствии симптомов",
                "status": "active",
                "questions_asked_count": 0,
            },
            {
                "goal_id": f"goal_{week_number}_2",
                "title": "Контроль вечернего сна и засыпания",
                "clinical_rationale": "Оценить влияние дневной активности на качество сна",
                "priority": 2,
                "known_facts": ["Продолжительность сна ~7.5 ч"],
                "target_clarification": "Уточнить время засыпания и пробуждений",
                "sufficiency_criteria": "Ответ о субъективном качестве сна",
                "status": "active",
                "questions_asked_count": 0,
            },
        ],
        "status": "active",
    }


def _build_stage_4_mock_gateway(
    recorded_calls: List[Dict[str, Any]],
    fail_on_step_a: bool = False,
    fail_on_step_b: bool = False,
    fail_on_step_c: bool = False,
    weekly_plan_override: Optional[Dict[str, Any]] = None,
    pro_output_override: Optional[Dict[str, Any]] = None,
) -> GeminiGateway:
    step_a_failures = {"count": 0}
    step_b_failures = {"count": 0}
    step_c_failures = {"count": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        prompt = body["contents"][0]["parts"][0]["text"]
        url_path = request.url.path

        recorded_calls.append({
            "url_path": url_path,
            "prompt": prompt,
        })

        # 1. Weekly Chief Physician (Gemini Pro)
        if "You are the Chief Physician" in prompt:
            assert "flash-lite" not in url_path, f"Flash-Lite called during Weekly: {url_path}"
            assert "pro" in url_path
            if fail_on_step_a and step_a_failures["count"] == 0:
                step_a_failures["count"] += 1
                return httpx.Response(503, json={"error": {"message": "Transient Pro Chief Physician overload"}})
            pro_payload = pro_output_override or _sample_weekly_pro_output()
            return httpx.Response(200, json=_gemini_envelope(pro_payload))

        # 2. Weekly Deputy Chief Physician (Gemini Flash)
        if "You are the Head of Department" in prompt or "Deputy Chief Physician" in prompt or "weekly_deputy" in prompt.lower():
            assert "flash-lite" not in url_path, f"Flash-Lite called during Weekly: {url_path}"
            assert "flash" in url_path
            if fail_on_step_a and step_a_failures["count"] == 0:
                step_a_failures["count"] += 1
                return httpx.Response(503, json={"error": {"message": "Transient Flash Deputy overload"}})
            return httpx.Response(200, json=_gemini_envelope(_sample_weekly_flash_output()))

        # 3. Step C: Athena Weekly Wrap-up Message (Gemini Flash)
        if "Internal Athena Weekly Wrap-up Message Formulation" in prompt:
            assert "flash-lite" not in url_path, f"Flash-Lite called during wrap-up: {url_path}"
            assert "flash" in url_path
            if fail_on_step_c and step_c_failures["count"] == 0:
                step_c_failures["count"] += 1
                return httpx.Response(503, json={"error": {"message": "Transient Athena wrap-up overload"}})
            wrapup_resp = {
                "message": (
                    "Здравствуйте! Подошла к концу первая неделя нашего совместного наблюдения. "
                    "Ваш организм отлично адаптируется: показатели пульса стабильны, а уровень энергии повысился. "
                    "Нажмите на силуэт человека на главном экране приложения Aegis, чтобы посмотреть подробные обновлённые отчёты по всем системам в медицинском сейфе!"
                )
            }
            return httpx.Response(200, json=_gemini_envelope(wrapup_resp))

        # 4. Step B: Athena Autonomous Weekly Plan Formulation (Gemini Flash)
        if "Internal Weekly Plan Formulation Scenario" in prompt:
            assert "flash" in url_path
            if fail_on_step_b and step_b_failures["count"] == 0:
                step_b_failures["count"] += 1
                return httpx.Response(503, json={"error": {"message": "Transient Athena weekly plan overload"}})
            # Determine target week number from prompt
            w_num = 2 if "Неделя №2" in prompt else 1
            plan_payload = weekly_plan_override or _sample_weekly_plan("cyc_test", week_number=w_num)
            return httpx.Response(200, json=_gemini_envelope(plan_payload))

        # 5. Athena routine question / greeting / progression
        if "Athena Dialogue Turn" in prompt or "Start accompaniment" in prompt or "Ask first planned question" in prompt:
            athena_turn_resp = {
                "reply": "Добрый день! Как вы себя чувствуете после вчерашней прогулки?",
                "emergency_trigger": False,
                "emergency_notice": None,
                "goal_progression": {
                    "goal_id": "goal_1_1",
                    "decision": "completed",
                    "is_sufficient": True,
                    "reasoning": "Ответ понятен и закрывает цель",
                    "unresolved_points": [],
                    "next_action": "conclude_day",
                },
            }
            return httpx.Response(200, json=_gemini_envelope(athena_turn_resp))

        # 6. Primary Consilium mock if triggered
        if "Primary Consilium" in prompt:
            return httpx.Response(200, json=_gemini_envelope({"pro_general_shelf": "Primary base roadmap"}))

        # Default fallback
        return httpx.Response(200, json=_gemini_envelope({"status": "ok", "message": "Standard response"}))

    return GeminiGateway(
        api_key="test-stage-4-key",
        transport=httpx.MockTransport(handler),
        max_retries=1,
        sleep_func=lambda _: asyncio.sleep(0),
    )


def _seed_completed_primary_and_anchor(
    session: Session,
    patient_id: str,
    cycle_id: str,
    t0: datetime,
) -> None:
    p_repo = PatientRepository(session)
    if p_repo.get(patient_id) is None:
        p_repo.create(patient_id=patient_id, status="active")

    c_repo = MedicalCycleRepository(session)
    if c_repo.get(cycle_id) is None:
        cycle = MedicalCycle(cycle_id=cycle_id, patient_id=patient_id, status="active")
        session.add(cycle)
        session.commit()

    r_repo = ModelReportRepository(session)
    r_repo.create_next_version(
        cycle_id=cycle_id,
        model="gemini-3.1-pro-preview",
        report_type=CHIEF_PHYSICIAN_INTERNAL_REPORT_TYPE,
        privacy_level=PrivacyLevel.SERVER_ONLY,
        content={
            "pro_general_shelf": "Первичная стратегия Pro: фокус на кардио-респираторном профиле",
            "pro_training_shelf": "Базовые параметры адаптации",
        },
        commit=False,
    )
    r_repo.create_next_version(
        cycle_id=cycle_id,
        model="gemini-3.1-flash",
        report_type="deputy_chief_physician_internal",
        privacy_level=PrivacyLevel.SERVER_ONLY,
        content={
            "flash_general_shelf": "Первичный синтез заместителя главного врача: гемодинамика стабильна",
            "flash_training_shelf": "Базовые параметры адаптации",
        },
        commit=False,
    )
    r_repo.create_next_version(
        cycle_id=cycle_id,
        model="gemini-3.1-pro-preview",
        report_type="user_overall_report",
        privacy_level=PrivacyLevel.CLIENT_FACING,
        content={
            "title": "Первичное заключение консилиума",
            "summary": "Первичное обследование пройдено успешно, базовые показатели в норме.",
            "highlights": ["Продолжайте ежедневную умеренную ходьбу."],
        },
        commit=False,
    )
    r_repo.create_next_version(
        cycle_id=cycle_id,
        model="gemini-3.1-pro-preview",
        report_type=CHIEF_PHYSICIAN_USER_REPORT_TYPE,
        privacy_level=PrivacyLevel.CLIENT_FACING,
        content={"summary": "План на первую неделю наблюдения"},
        commit=False,
    )
    r_repo.create_next_version(
        cycle_id=cycle_id,
        model="gemini-3.1-pro-preview",
        report_type=ATHENA_ACTIVE_STRATEGY_REPORT_TYPE,
        privacy_level=PrivacyLevel.SERVER_ONLY,
        content={
            "strategy_status": "initial",
            "active_strategy_text": "Первичная стратегия Pro: отслеживание ЧСС и сна",
            "daily_question_limit": 3,
        },
        commit=False,
    )

    w_svc = WeeklyConsiliumService(db=session)
    w_svc.record_schedule_anchor(
        cycle_id=cycle_id,
        anchor_dt=t0,
        source="post_primary_init",
        commit=True,
    )


def _seed_daily_journals(
    session: Session,
    cycle_id: str,
    patient_id: str,
    t0: datetime,
    days_count: int = 7,
    calendar_week: int = 1,
) -> None:
    r_repo = ModelReportRepository(session)
    for day in range(1, days_count + 1):
        day_start = t0 + timedelta(days=day - 1)
        day_end = t0 + timedelta(days=day)
        journal = AthenaDailyJournalRecord(
            journal_id=f"adj_{cycle_id}_w{calendar_week}_d{day}",
            cycle_id=cycle_id,
            patient_id=patient_id,
            calendar_week=calendar_week,
            calendar_day=day,
            window_start_utc=day_start.isoformat(),
            window_end_utc=day_end.isoformat(),
            closure_timestamp_utc=day_end.isoformat(),
            closure_type="wellness_ready_immediate",
            patient_reported_evidence={
                "patient_answers": [
                    {
                        "text": f"День {day}: самочувствие отличное, ходьба без одышки.",
                        "timestamp_utc": (day_start + timedelta(hours=14)).isoformat(),
                        "source_event_id": f"msg_d{day}_ans",
                        "event_type": "routine_answer",
                    }
                ],
                "routine_question": {
                    "question_id": f"rq_w{calendar_week}_d{day}",
                    "was_answered": True,
                    "answered_at_utc": (day_start + timedelta(hours=14)).isoformat(),
                },
                "out_of_turn_messages": [],
            },
            wellness_evidence={
                "report_reference": {
                    "is_available": True,
                    "report_id": f"wel_{cycle_id}_w{calendar_week}_d{day}",
                    "report_type": WELLNESS_DAILY_REPORT_TYPE,
                    "version": 1,
                },
                "wellness_assessment": f"День {day}: показатели ЧСС и сна в пределах нормы.",
                "biometrics": {
                    "heart_rate_summary": {"min_bpm": 56, "avg_bpm": 68, "max_bpm": 112, "sample_count": 800},
                    "blood_pressure_summary": {"avg_systolic": 118, "avg_diastolic": 76, "sample_count": 3},
                    "spo2_summary": {"avg_spo2": 98.2, "min_spo2": 96.0},
                },
                "missing_data_declarations": [],
            },
            exceptional_events=[],
            athena_clinical_assessment={
                "is_available": True,
                "label": "ATHENA_GENERATED_INTERPRETATION_NOT_RAW_EVIDENCE",
                "status": "NORMAL",
                "red_flags": False,
                "interpretation": f"День {day}: стабильный кардио-профиль.",
                "observations": ["Хорошая переносимость прогулки"],
                "relevant_changes": [],
                "contradictions": [],
                "uncertainties": [],
                "source_references": [],
            },
        )
        report_id = f"rep_journal_{cycle_id}_w{calendar_week}_d{day}"[:64]
        day_report_type = f"{ATHENA_DAILY_JOURNAL_REPORT_TYPE}_w{calendar_week}_d{day}"
        row = ModelReport(
            report_id=report_id,
            cycle_id=cycle_id,
            sprint_id=None,
            session_id=None,
            model=ATHENA_DAILY_JOURNAL_MODEL,
            report_type=day_report_type,
            privacy_level=PrivacyLevel.SERVER_ONLY.value,
            version=1,
            content=journal.model_dump(),
            created_at=day_end,
        )
        session.add(row)
    session.commit()


# ==============================================================================
# SECTION A: SCHEDULE VERIFICATION (TESTS 1 - 6)
# ==============================================================================

@pytest.mark.anyio
async def test_01_weekly_not_run_before_t0_plus_168h(tmp_path: Path):
    """1. Weekly Consilium cannot run before T0 + 168 hours."""
    db_file = tmp_path / "test_stage_4_01.db"
    _, SessionFactory = _create_isolated_db(db_file)
    t0 = datetime(2026, 11, 4, 10, 0, 0, tzinfo=timezone.utc)
    clock = ControlledClock(t0)
    calls: List[Dict[str, Any]] = []
    gw = _build_stage_4_mock_gateway(calls)

    with SessionFactory() as session:
        _seed_completed_primary_and_anchor(session, "pat_01", "cyc_01", t0)

    worker = ClinicalAutomationWorker(
        session_factory=SessionFactory,
        gateway_factory=lambda: gw,
        now_provider=clock.now,
        enable_automatic_weekly=True,
    )

    # Advance to T0 + 167 hours (1 hour before boundary)
    clock.advance(timedelta(hours=167))
    processed = await worker.run_once(discover=True)

    # Step A job should be enqueued (discovered) with next_attempt_at_utc = T0 + 168h,
    # but NOT claimable / NOT executed yet!
    with SessionFactory() as session:
        w_svc = WeeklyConsiliumService(db=session)
        assert not w_svc.is_weekly_consilium_completed("cyc_01", sprint_number=1)
        auto_svc = ClinicalAutomationService(db=session)
        step_a_job = auto_svc.get_job("cyc_01", AutomationJobType.WEEKLY_CONSILIUM_A)
        assert step_a_job is not None
        assert step_a_job.status == AutomationJobStatus.PENDING
    assert len(calls) == 0


@pytest.mark.anyio
async def test_02_weekly_available_at_exactly_t0_plus_168h(tmp_path: Path):
    """2. Weekly Consilium becomes claimable and runs at exactly T0 + 168 hours."""
    db_file = tmp_path / "test_stage_4_02.db"
    _, SessionFactory = _create_isolated_db(db_file)
    t0 = datetime(2026, 11, 4, 10, 0, 0, tzinfo=timezone.utc)
    clock = ControlledClock(t0)
    calls: List[Dict[str, Any]] = []
    gw = _build_stage_4_mock_gateway(calls)

    with SessionFactory() as session:
        _seed_completed_primary_and_anchor(session, "pat_02", "cyc_02", t0)
        _seed_daily_journals(session, "cyc_02", "pat_02", t0, days_count=7)

    worker = ClinicalAutomationWorker(
        session_factory=SessionFactory,
        gateway_factory=lambda: gw,
        now_provider=clock.now,
        enable_automatic_weekly=True,
    )

    # Advance to exactly T0 + 168 hours
    clock.set(t0 + timedelta(hours=168))
    processed = await worker.run_once(discover=True)

    with SessionFactory() as session:
        w_svc = WeeklyConsiliumService(db=session)
        assert w_svc.is_weekly_consilium_completed("cyc_02", sprint_number=1)


@pytest.mark.anyio
async def test_03_weekly_runs_automatically_without_user_button(tmp_path: Path):
    """3. Automatic server execution: no client API calls or user buttons required."""
    db_file = tmp_path / "test_stage_4_03.db"
    _, SessionFactory = _create_isolated_db(db_file)
    t0 = datetime(2026, 11, 4, 10, 0, 0, tzinfo=timezone.utc)
    clock = ControlledClock(t0 + timedelta(hours=168))
    calls: List[Dict[str, Any]] = []
    gw = _build_stage_4_mock_gateway(calls)

    with SessionFactory() as session:
        _seed_completed_primary_and_anchor(session, "pat_03", "cyc_03", t0)

    worker = ClinicalAutomationWorker(
        session_factory=SessionFactory,
        gateway_factory=lambda: gw,
        now_provider=clock.now,
        enable_automatic_weekly=True,
    )

    # Single run_once() tick automatically discovers and executes Step A
    await worker.run_once(discover=True)
    with SessionFactory() as session:
        w_svc = WeeklyConsiliumService(db=session)
        assert w_svc.is_weekly_consilium_completed("cyc_03", sprint_number=1)


@pytest.mark.anyio
async def test_04_weekly_flag_disabled_prevents_automatic_execution(tmp_path: Path):
    """4. When enable_automatic_weekly=False, no weekly jobs are scheduled or executed."""
    db_file = tmp_path / "test_stage_4_04.db"
    _, SessionFactory = _create_isolated_db(db_file)
    t0 = datetime(2026, 11, 4, 10, 0, 0, tzinfo=timezone.utc)
    clock = ControlledClock(t0 + timedelta(hours=200))
    calls: List[Dict[str, Any]] = []
    gw = _build_stage_4_mock_gateway(calls)

    with SessionFactory() as session:
        _seed_completed_primary_and_anchor(session, "pat_04", "cyc_04", t0)

    worker = ClinicalAutomationWorker(
        session_factory=SessionFactory,
        gateway_factory=lambda: gw,
        now_provider=clock.now,
        enable_automatic_weekly=False,  # Disabled!
    )

    await worker.run_once(discover=True)
    with SessionFactory() as session:
        w_svc = WeeklyConsiliumService(db=session)
        assert not w_svc.is_weekly_consilium_completed("cyc_04", sprint_number=1)
        auto_svc = ClinicalAutomationService(db=session)
        assert auto_svc.get_job("cyc_04", AutomationJobType.WEEKLY_CONSILIUM_A) is None
    assert len(calls) == 0


@pytest.mark.anyio
async def test_05_schedule_independent_of_wellness_wakeup_and_user_timezone(tmp_path: Path):
    """5. Fixed server schedule: completely decoupled from user waking or wellness state."""
    db_file = tmp_path / "test_stage_4_05.db"
    _, SessionFactory = _create_isolated_db(db_file)
    t0 = datetime(2026, 11, 4, 10, 0, 0, tzinfo=timezone.utc)
    # Clock is at T0 + 168 hours regardless of any morning wakeup
    clock = ControlledClock(t0 + timedelta(hours=168))
    calls: List[Dict[str, Any]] = []
    gw = _build_stage_4_mock_gateway(calls)

    with SessionFactory() as session:
        _seed_completed_primary_and_anchor(session, "pat_05", "cyc_05", t0)
        # Note: ZERO wellness reports seeded, no wakeup event.

    worker = ClinicalAutomationWorker(
        session_factory=SessionFactory,
        gateway_factory=lambda: gw,
        now_provider=clock.now,
        enable_automatic_weekly=True,
    )

    await worker.run_once(discover=True)
    with SessionFactory() as session:
        w_svc = WeeklyConsiliumService(db=session)
        assert w_svc.is_weekly_consilium_completed("cyc_05", sprint_number=1)


@pytest.mark.anyio
async def test_06_next_weekly_boundary_remains_t0_plus_336h(tmp_path: Path):
    """6. Week 2 boundary is scheduled strictly at T0 + 336 hours."""
    db_file = tmp_path / "test_stage_4_06.db"
    _, SessionFactory = _create_isolated_db(db_file)
    t0 = datetime(2026, 11, 4, 10, 0, 0, tzinfo=timezone.utc)
    clock = ControlledClock(t0 + timedelta(hours=168))
    calls: List[Dict[str, Any]] = []
    gw = _build_stage_4_mock_gateway(calls)

    with SessionFactory() as session:
        _seed_completed_primary_and_anchor(session, "pat_06", "cyc_06", t0)

    worker = ClinicalAutomationWorker(
        session_factory=SessionFactory,
        gateway_factory=lambda: gw,
        now_provider=clock.now,
        enable_automatic_weekly=True,
    )

    # Complete week 1 (Steps A, B, C)
    await worker.run_once(discover=True)  # Step A
    await worker.run_once(discover=True)  # Step B
    await worker.run_once(discover=True)  # Step C

    # Next discovery should discover Step A for week 2 scheduled at T0 + 336h
    await worker.run_once(discover=True)
    with SessionFactory() as session:
        auto_svc = ClinicalAutomationService(db=session)
        w2_job = auto_svc.get_job("cyc_06", AutomationJobType.WEEKLY_CONSILIUM_A, "weekly_consilium_step_a_cyc_06_w2")
        assert w2_job is not None
        assert w2_job.status == AutomationJobStatus.PENDING
        expected_w2_due = (t0 + timedelta(hours=336)).isoformat()
        assert w2_job.next_attempt_at_utc == expected_w2_due


# ==============================================================================
# SECTION B: INPUT DATA VERIFICATION (TESTS 7 - 13)
# ==============================================================================

@pytest.mark.anyio
async def test_07_weekly_works_with_seven_daily_journals(tmp_path: Path):
    """7. Weekly Consilium correctly processes 7 complete daily journals."""
    db_file = tmp_path / "test_stage_4_07.db"
    _, SessionFactory = _create_isolated_db(db_file)
    t0 = datetime(2026, 11, 4, 10, 0, 0, tzinfo=timezone.utc)
    clock = ControlledClock(t0 + timedelta(hours=168))
    calls: List[Dict[str, Any]] = []
    gw = _build_stage_4_mock_gateway(calls)

    with SessionFactory() as session:
        _seed_completed_primary_and_anchor(session, "pat_07", "cyc_07", t0)
        _seed_daily_journals(session, "cyc_07", "pat_07", t0, days_count=7)

    worker = ClinicalAutomationWorker(
        session_factory=SessionFactory,
        gateway_factory=lambda: gw,
        now_provider=clock.now,
        enable_automatic_weekly=True,
    )

    await worker.run_once(discover=True)
    # Check that Flash and Pro prompts received all 7 days
    flash_call = next(c for c in calls if ("Deputy Chief Physician" in c["prompt"] or "Head of Department" in c["prompt"]))
    for day in range(1, 8):
        assert f"--- DAY {day} ---" in flash_call["prompt"]
        assert f"День {day}: самочувствие отличное" in flash_call["prompt"]


@pytest.mark.anyio
async def test_08_weekly_works_with_fewer_than_seven_journals(tmp_path: Path):
    """8. Weekly Consilium succeeds when fewer than 7 journals exist (e.g., 3 journals)."""
    db_file = tmp_path / "test_stage_4_08.db"
    _, SessionFactory = _create_isolated_db(db_file)
    t0 = datetime(2026, 11, 4, 10, 0, 0, tzinfo=timezone.utc)
    clock = ControlledClock(t0 + timedelta(hours=168))
    calls: List[Dict[str, Any]] = []
    gw = _build_stage_4_mock_gateway(calls)

    with SessionFactory() as session:
        _seed_completed_primary_and_anchor(session, "pat_08", "cyc_08", t0)
        _seed_daily_journals(session, "cyc_08", "pat_08", t0, days_count=3)  # Only 3 days

    worker = ClinicalAutomationWorker(
        session_factory=SessionFactory,
        gateway_factory=lambda: gw,
        now_provider=clock.now,
        enable_automatic_weekly=True,
    )

    await worker.run_once(discover=True)
    with SessionFactory() as session:
        w_svc = WeeklyConsiliumService(db=session)
        assert w_svc.is_weekly_consilium_completed("cyc_08", sprint_number=1)


@pytest.mark.anyio
async def test_09_explicit_missing_day_declarations_for_absent_journals(tmp_path: Path):
    """9. Missing observation days explicitly contain [MISSING DAY DECLARATION]."""
    db_file = tmp_path / "test_stage_4_09.db"
    _, SessionFactory = _create_isolated_db(db_file)
    t0 = datetime(2026, 11, 4, 10, 0, 0, tzinfo=timezone.utc)
    clock = ControlledClock(t0 + timedelta(hours=168))
    calls: List[Dict[str, Any]] = []
    gw = _build_stage_4_mock_gateway(calls)

    with SessionFactory() as session:
        _seed_completed_primary_and_anchor(session, "pat_09", "cyc_09", t0)
        _seed_daily_journals(session, "cyc_09", "pat_09", t0, days_count=4)  # Days 1-4 present, 5-7 absent

    worker = ClinicalAutomationWorker(
        session_factory=SessionFactory,
        gateway_factory=lambda: gw,
        now_provider=clock.now,
        enable_automatic_weekly=True,
    )

    await worker.run_once(discover=True)
    flash_call = next(c for c in calls if ("Deputy Chief Physician" in c["prompt"] or "Head of Department" in c["prompt"]))
    for day in (5, 6, 7):
        assert f"[MISSING DAY DECLARATION]: Daily journal not recorded for Day {day}" in flash_call["prompt"]


@pytest.mark.anyio
async def test_10_unjournaled_meaningful_patient_answers_preserved(tmp_path: Path):
    """10. Unjournaled patient messages prior to weekly cutoff are captured verbatim with source tag."""
    db_file = tmp_path / "test_stage_4_10.db"
    _, SessionFactory = _create_isolated_db(db_file)
    t0 = datetime(2026, 11, 4, 10, 0, 0, tzinfo=timezone.utc)
    clock = ControlledClock(t0 + timedelta(hours=168))
    calls: List[Dict[str, Any]] = []
    gw = _build_stage_4_mock_gateway(calls)

    with SessionFactory() as session:
        _seed_completed_primary_and_anchor(session, "pat_10", "cyc_10", t0)
        _seed_daily_journals(session, "cyc_10", "pat_10", t0, days_count=3)
        # Add an unjournaled user message at Day 6 (T0 + 140h)
        w_svc = WeeklyConsiliumService(db=session)
        w_svc.save_athena_chat_message(
            cycle_id="cyc_10",
            sender="user",
            text="Вечером почувствовал легкую тяжесть в груди при подъеме по лестнице",
            timestamp=(t0 + timedelta(hours=140)).isoformat(),
            commit=True,
        )

    worker = ClinicalAutomationWorker(
        session_factory=SessionFactory,
        gateway_factory=lambda: gw,
        now_provider=clock.now,
        enable_automatic_weekly=True,
    )

    await worker.run_once(discover=True)
    flash_call = next(c for c in calls if ("Deputy Chief Physician" in c["prompt"] or "Head of Department" in c["prompt"]))
    assert "=== UNJOURNALED PATIENT RESPONSES (PRIOR TO WEEKLY BOUNDARY) ===" in flash_call["prompt"]
    assert "Вечером почувствовал легкую тяжесть в груди" in flash_call["prompt"]
    assert "Direct Chat Turn" in flash_call["prompt"]


@pytest.mark.anyio
async def test_11_no_duplicate_answers_or_measurements(tmp_path: Path):
    """11. Patient statements already captured in a daily journal are not duplicated in unjournaled section."""
    db_file = tmp_path / "test_stage_4_11.db"
    _, SessionFactory = _create_isolated_db(db_file)
    t0 = datetime(2026, 11, 4, 10, 0, 0, tzinfo=timezone.utc)
    clock = ControlledClock(t0 + timedelta(hours=168))
    calls: List[Dict[str, Any]] = []
    gw = _build_stage_4_mock_gateway(calls)

    with SessionFactory() as session:
        _seed_completed_primary_and_anchor(session, "pat_11", "cyc_11", t0)
        # Seed Day 1 journal containing message with timestamp T0 + 14h
        _seed_daily_journals(session, "cyc_11", "pat_11", t0, days_count=1)
        # Save identical message into chat history with same timestamp
        w_svc = WeeklyConsiliumService(db=session)
        w_svc.save_athena_chat_message(
            cycle_id="cyc_11",
            sender="user",
            text="День 1: самочувствие отличное, ходьба без одышки.",
            timestamp=(t0 + timedelta(hours=14)).isoformat(),
            commit=True,
        )

    worker = ClinicalAutomationWorker(
        session_factory=SessionFactory,
        gateway_factory=lambda: gw,
        now_provider=clock.now,
        enable_automatic_weekly=True,
    )

    await worker.run_once(discover=True)
    flash_call = next(c for c in calls if ("Deputy Chief Physician" in c["prompt"] or "Head of Department" in c["prompt"]))
    # It should appear in DAY 1, but NOT in unjournaled responses
    assert "=== UNJOURNALED PATIENT RESPONSES" not in flash_call["prompt"]


@pytest.mark.anyio
async def test_12_events_after_weekly_boundary_excluded(tmp_path: Path):
    """12. Messages and biometric events strictly >= T0 + 168h are excluded from week 1."""
    db_file = tmp_path / "test_stage_4_12.db"
    _, SessionFactory = _create_isolated_db(db_file)
    t0 = datetime(2026, 11, 4, 10, 0, 0, tzinfo=timezone.utc)
    clock = ControlledClock(t0 + timedelta(hours=175))
    calls: List[Dict[str, Any]] = []
    gw = _build_stage_4_mock_gateway(calls)

    with SessionFactory() as session:
        _seed_completed_primary_and_anchor(session, "pat_12", "cyc_12", t0)
        # Message strictly inside week 1
        w_svc = WeeklyConsiliumService(db=session)
        w_svc.save_athena_chat_message(
            cycle_id="cyc_12",
            sender="user",
            text="Сообщение недели 1 (T0 + 100ч)",
            timestamp=(t0 + timedelta(hours=100)).isoformat(),
            commit=True,
        )
        # Message strictly after week 1 boundary (T0 + 169ч)
        w_svc.save_athena_chat_message(
            cycle_id="cyc_12",
            sender="user",
            text="Сообщение недели 2 НЕЛЬЗЯ ВКЛЮЧАТЬ В НЕДЕЛЮ 1 (T0 + 169ч)",
            timestamp=(t0 + timedelta(hours=169)).isoformat(),
            commit=True,
        )

    worker = ClinicalAutomationWorker(
        session_factory=SessionFactory,
        gateway_factory=lambda: gw,
        now_provider=clock.now,
        enable_automatic_weekly=True,
    )

    await worker.run_once(discover=True)
    flash_call = next(c for c in calls if ("Deputy Chief Physician" in c["prompt"] or "Head of Department" in c["prompt"]))
    assert "Сообщение недели 1 (T0 + 100ч)" in flash_call["prompt"]
    assert "Сообщение недели 2 НЕЛЬЗЯ ВКЛЮЧАТЬ В НЕДЕЛЮ 1" not in flash_call["prompt"]


@pytest.mark.anyio
async def test_13_cycle_and_patient_isolation(tmp_path: Path):
    """13. Data from separate patients and cycles are strictly isolated."""
    db_file = tmp_path / "test_stage_4_13.db"
    _, SessionFactory = _create_isolated_db(db_file)
    t0 = datetime(2026, 11, 4, 10, 0, 0, tzinfo=timezone.utc)
    clock = ControlledClock(t0 + timedelta(hours=168))
    calls: List[Dict[str, Any]] = []
    gw = _build_stage_4_mock_gateway(calls)

    with SessionFactory() as session:
        _seed_completed_primary_and_anchor(session, "pat_13A", "cyc_13A", t0)
        _seed_completed_primary_and_anchor(session, "pat_13B", "cyc_13B", t0)
        # Seed message in cycle B
        w_svc = WeeklyConsiliumService(db=session)
        w_svc.save_athena_chat_message(
            cycle_id="cyc_13B",
            sender="user",
            text="СЕКРЕТНЫЙ_СИМПТОМ_ПАЦИЕНТА_13B",
            timestamp=(t0 + timedelta(hours=50)).isoformat(),
            commit=True,
        )

    worker = ClinicalAutomationWorker(
        session_factory=SessionFactory,
        gateway_factory=lambda: gw,
        now_provider=clock.now,
        enable_automatic_weekly=True,
    )

    await worker.run_once(discover=True)
    # Check that patient 13A's consilium did NOT receive patient 13B's message
    call_13a = next(c for c in calls if "cyc_13A" in c["prompt"] or ("Deputy Chief Physician" in c["prompt"] or "Head of Department" in c["prompt"]))
    assert "СЕКРЕТНЫЙ_СИМПТОМ_ПАЦИЕНТА_13B" not in call_13a["prompt"]


# ==============================================================================
# SECTION C: CONSILIUM & PUBLICATION VERIFICATION (TESTS 14 - 18)
# ==============================================================================

@pytest.mark.anyio
async def test_14_weekly_cascade_strictly_flash_to_pro_no_eight_lite_models(tmp_path: Path):
    """14. Weekly Consilium calls ONLY Flash (Deputy) -> Pro (Chief); never the 8 Flash-Lite models."""
    db_file = tmp_path / "test_stage_4_14.db"
    _, SessionFactory = _create_isolated_db(db_file)
    t0 = datetime(2026, 11, 4, 10, 0, 0, tzinfo=timezone.utc)
    clock = ControlledClock(t0 + timedelta(hours=168))
    calls: List[Dict[str, Any]] = []
    gw = _build_stage_4_mock_gateway(calls)

    with SessionFactory() as session:
        _seed_completed_primary_and_anchor(session, "pat_14", "cyc_14", t0)

    worker = ClinicalAutomationWorker(
        session_factory=SessionFactory,
        gateway_factory=lambda: gw,
        now_provider=clock.now,
        enable_automatic_weekly=True,
    )

    # Run Step A
    await worker.run_once(discover=True)
    # Filter calls from Step A (Weekly Consilium)
    weekly_calls = [c for c in calls if ("Deputy Chief Physician" in c["prompt"] or "Head of Department" in c["prompt"]) or "Chief Physician" in c["prompt"]]
    assert len(weekly_calls) == 2
    assert "flash" in weekly_calls[0]["url_path"] and "flash-lite" not in weekly_calls[0]["url_path"]
    assert "pro" in weekly_calls[1]["url_path"]
    # Check that Flash-Lite was never called across all recorded calls
    for c in calls:
        assert "flash-lite" not in c["url_path"]


@pytest.mark.anyio
async def test_15_model_response_validation_and_quality_gate(tmp_path: Path):
    """15. Rejection by Chief Physician quality review prevents atomic publication."""
    db_file = tmp_path / "test_stage_4_15.db"
    _, SessionFactory = _create_isolated_db(db_file)
    t0 = datetime(2026, 11, 4, 10, 0, 0, tzinfo=timezone.utc)
    clock = ControlledClock(t0 + timedelta(hours=168))
    calls: List[Dict[str, Any]] = []

    # Pro quality review rejects publication
    pro_rejected = _sample_weekly_pro_output(approved=False)
    pro_rejected["flash_quality_review"]["rejection_reason"] = "Clinical contradiction in respiratory section"
    gw = _build_stage_4_mock_gateway(calls, pro_output_override=pro_rejected)

    with SessionFactory() as session:
        _seed_completed_primary_and_anchor(session, "pat_15", "cyc_15", t0)

    worker = ClinicalAutomationWorker(
        session_factory=SessionFactory,
        gateway_factory=lambda: gw,
        now_provider=clock.now,
        enable_automatic_weekly=True,
    )

    await worker.run_once(discover=True)
    with SessionFactory() as session:
        w_svc = WeeklyConsiliumService(db=session)
        assert not w_svc.is_weekly_consilium_completed("cyc_15", sprint_number=1)
        r_repo = ModelReportRepository(session)
        overall = r_repo.list_client_facing(cycle_id="cyc_15", report_type=WEEKLY_OVERALL_CLIENT_REPORT_TYPE)
        assert len(overall) == 0


@pytest.mark.anyio
async def test_16_atomic_publication_of_eight_system_reports_and_overall_report(tmp_path: Path):
    """16. All 8 client-facing system reports + overall report are published atomically."""
    db_file = tmp_path / "test_stage_4_16.db"
    _, SessionFactory = _create_isolated_db(db_file)
    t0 = datetime(2026, 11, 4, 10, 0, 0, tzinfo=timezone.utc)
    clock = ControlledClock(t0 + timedelta(hours=168))
    calls: List[Dict[str, Any]] = []
    gw = _build_stage_4_mock_gateway(calls)

    with SessionFactory() as session:
        _seed_completed_primary_and_anchor(session, "pat_16", "cyc_16", t0)

    worker = ClinicalAutomationWorker(
        session_factory=SessionFactory,
        gateway_factory=lambda: gw,
        now_provider=clock.now,
        enable_automatic_weekly=True,
    )

    await worker.run_once(discover=True)
    with SessionFactory() as session:
        r_repo = ModelReportRepository(session)
        # Check overall report
        overalls = r_repo.list_client_facing(cycle_id="cyc_16", report_type=WEEKLY_OVERALL_CLIENT_REPORT_TYPE)
        assert len(overalls) == 1
        assert overalls[0].privacy_level == PrivacyLevel.CLIENT_FACING.value
        # Check all 8 system reports
        for sys_key in SYSTEM_KEYS:
            sys_reps = r_repo.list_client_facing(cycle_id="cyc_16", report_type=f"system_user_report_{sys_key}")
            assert len(sys_reps) == 1
            assert sys_reps[0].privacy_level == PrivacyLevel.CLIENT_FACING.value


@pytest.mark.anyio
async def test_17_updated_pro_strategy_remains_server_only(tmp_path: Path):
    """17. Updated Chief Physician Pro strategy remains strictly SERVER_ONLY."""
    db_file = tmp_path / "test_stage_4_17.db"
    _, SessionFactory = _create_isolated_db(db_file)
    t0 = datetime(2026, 11, 4, 10, 0, 0, tzinfo=timezone.utc)
    clock = ControlledClock(t0 + timedelta(hours=168))
    calls: List[Dict[str, Any]] = []
    gw = _build_stage_4_mock_gateway(calls)

    with SessionFactory() as session:
        _seed_completed_primary_and_anchor(session, "pat_17", "cyc_17", t0)

    worker = ClinicalAutomationWorker(
        session_factory=SessionFactory,
        gateway_factory=lambda: gw,
        now_provider=clock.now,
        enable_automatic_weekly=True,
    )

    await worker.run_once(discover=True)
    with SessionFactory() as session:
        r_repo = ModelReportRepository(session)
        # Client facing must NEVER contain athena_active_strategy
        client_strat = r_repo.list_client_facing(cycle_id="cyc_17", report_type=ATHENA_ACTIVE_STRATEGY_REPORT_TYPE)
        assert len(client_strat) == 0
        # Server only must contain the new version
        server_strat = r_repo.list_server_only(cycle_id="cyc_17", report_type=ATHENA_ACTIVE_STRATEGY_REPORT_TYPE)
        assert len(server_strat) >= 2  # Seeded v1 from primary + new v2 from weekly
        latest_strat = server_strat[0]
        assert latest_strat.privacy_level == PrivacyLevel.SERVER_ONLY.value
        assert "УТВЕРЖДЁННАЯ СТРАТЕГИЯ PRO" in latest_strat.content.get("active_strategy_text", "")


@pytest.mark.anyio
async def test_18_user_facing_reports_contain_no_internal_materials(tmp_path: Path):
    """18. Client-facing reports do not leak Pro internal directives, hypotheses or SERVER_ONLY content."""
    db_file = tmp_path / "test_stage_4_18.db"
    _, SessionFactory = _create_isolated_db(db_file)
    t0 = datetime(2026, 11, 4, 10, 0, 0, tzinfo=timezone.utc)
    clock = ControlledClock(t0 + timedelta(hours=168))
    calls: List[Dict[str, Any]] = []
    gw = _build_stage_4_mock_gateway(calls)

    with SessionFactory() as session:
        _seed_completed_primary_and_anchor(session, "pat_18", "cyc_18", t0)

    worker = ClinicalAutomationWorker(
        session_factory=SessionFactory,
        gateway_factory=lambda: gw,
        now_provider=clock.now,
        enable_automatic_weekly=True,
    )

    await worker.run_once(discover=True)
    with SessionFactory() as session:
        r_repo = ModelReportRepository(session)
        client_reports = r_repo.list_client_facing(cycle_id="cyc_18")
        forbidden_markers = ["SERVER_ONLY", "differential_diagnosis", "pro_general_shelf", "deputy_analysis"]
        for cr in client_reports:
            raw_json = json.dumps(cr.content, ensure_ascii=False)
            for marker in forbidden_markers:
                assert marker not in raw_json


# ==============================================================================
# SECTION D: ATHENA VERIFICATION (TESTS 19 - 28)
# ==============================================================================

@pytest.mark.anyio
async def test_19_step_b_creates_new_weekly_plan_for_next_week(tmp_path: Path):
    """19. Step B creates a new Athena goal plan for Week 2 (N+1)."""
    db_file = tmp_path / "test_stage_4_19.db"
    _, SessionFactory = _create_isolated_db(db_file)
    t0 = datetime(2026, 11, 4, 10, 0, 0, tzinfo=timezone.utc)
    clock = ControlledClock(t0 + timedelta(hours=168))
    calls: List[Dict[str, Any]] = []
    gw = _build_stage_4_mock_gateway(calls)

    with SessionFactory() as session:
        _seed_completed_primary_and_anchor(session, "pat_19", "cyc_19", t0)

    worker = ClinicalAutomationWorker(
        session_factory=SessionFactory,
        gateway_factory=lambda: gw,
        now_provider=clock.now,
        enable_automatic_weekly=True,
    )

    # Tick 1: Executes Step A, enqueues Step B
    await worker.run_once(discover=True)
    # Tick 2: Executes Step B
    await worker.run_once(discover=True)

    with SessionFactory() as session:
        plan_svc = AthenaWeeklyPlanService(db=session)
        w2_plan = plan_svc.get_active_weekly_plan("cyc_19", week_number=2)
        assert w2_plan is not None
        assert w2_plan.week_number == 2
        assert len(w2_plan.goals) >= 1


@pytest.mark.anyio
async def test_20_new_plan_based_on_new_pro_strategy(tmp_path: Path):
    """20. New Athena plan is built using the newly updated Pro strategy from Step A."""
    db_file = tmp_path / "test_stage_4_20.db"
    _, SessionFactory = _create_isolated_db(db_file)
    t0 = datetime(2026, 11, 4, 10, 0, 0, tzinfo=timezone.utc)
    clock = ControlledClock(t0 + timedelta(hours=168))
    calls: List[Dict[str, Any]] = []

    custom_strat = "СПЕЦИФИЧЕСКАЯ_СТРАТЕГИЯ_PRO_НЕДЕЛЯ_2: Увеличить целевой темп ходьбы"
    pro_output = _sample_weekly_pro_output(updated_strategy=custom_strat)
    gw = _build_stage_4_mock_gateway(calls, pro_output_override=pro_output)

    with SessionFactory() as session:
        _seed_completed_primary_and_anchor(session, "pat_20", "cyc_20", t0)

    worker = ClinicalAutomationWorker(
        session_factory=SessionFactory,
        gateway_factory=lambda: gw,
        now_provider=clock.now,
        enable_automatic_weekly=True,
    )

    await worker.run_once(discover=True)  # Step A
    await worker.run_once(discover=True)  # Step B

    # Find the prompt that generated Week 2 plan
    plan_calls = [c for c in calls if "Internal Weekly Plan Formulation Scenario" in c["prompt"]]
    assert len(plan_calls) >= 1
    assert custom_strat in plan_calls[-1]["prompt"]


@pytest.mark.anyio
async def test_21_plan_linked_to_correct_sprint_and_pro_strategy(tmp_path: Path):
    """21. Plan metadata correctly records the source strategy version and sprint number."""
    db_file = tmp_path / "test_stage_4_21.db"
    _, SessionFactory = _create_isolated_db(db_file)
    t0 = datetime(2026, 11, 4, 10, 0, 0, tzinfo=timezone.utc)
    clock = ControlledClock(t0 + timedelta(hours=168))
    calls: List[Dict[str, Any]] = []
    gw = _build_stage_4_mock_gateway(calls)

    with SessionFactory() as session:
        _seed_completed_primary_and_anchor(session, "pat_21", "cyc_21", t0)

    worker = ClinicalAutomationWorker(
        session_factory=SessionFactory,
        gateway_factory=lambda: gw,
        now_provider=clock.now,
        enable_automatic_weekly=True,
    )

    await worker.run_once(discover=True)  # Step A
    await worker.run_once(discover=True)  # Step B

    with SessionFactory() as session:
        plan_svc = AthenaWeeklyPlanService(db=session)
        w2_plan = plan_svc.get_active_weekly_plan("cyc_21", week_number=2)
        assert w2_plan is not None
        assert w2_plan.week_number == 2
        assert w2_plan.source_strategy_version is not None
        assert w2_plan.source_strategy_version >= 2


@pytest.mark.anyio
async def test_22_global_t0_remains_unchanged(tmp_path: Path):
    """22. Global T0 anchor timestamp is never modified by Step A, Step B, or Step C."""
    db_file = tmp_path / "test_stage_4_22.db"
    _, SessionFactory = _create_isolated_db(db_file)
    t0 = datetime(2026, 11, 4, 10, 0, 0, tzinfo=timezone.utc)
    clock = ControlledClock(t0 + timedelta(hours=168))
    calls: List[Dict[str, Any]] = []
    gw = _build_stage_4_mock_gateway(calls)

    with SessionFactory() as session:
        _seed_completed_primary_and_anchor(session, "pat_22", "cyc_22", t0)

    worker = ClinicalAutomationWorker(
        session_factory=SessionFactory,
        gateway_factory=lambda: gw,
        now_provider=clock.now,
        enable_automatic_weekly=True,
    )

    await worker.run_once(discover=True)  # Step A
    await worker.run_once(discover=True)  # Step B
    await worker.run_once(discover=True)  # Step C

    with SessionFactory() as session:
        w_svc = WeeklyConsiliumService(db=session)
        current_t0 = w_svc.find_schedule_anchor_timestamp("cyc_22")
        assert current_t0 == t0  # Untouched!


@pytest.mark.anyio
async def test_23_athena_sends_separate_wrapup_message_to_chat(tmp_path: Path):
    """23. Step C sends a dedicated summary message from Athena to the chat history."""
    db_file = tmp_path / "test_stage_4_23.db"
    _, SessionFactory = _create_isolated_db(db_file)
    t0 = datetime(2026, 11, 4, 10, 0, 0, tzinfo=timezone.utc)
    clock = ControlledClock(t0 + timedelta(hours=168))
    calls: List[Dict[str, Any]] = []
    gw = _build_stage_4_mock_gateway(calls)

    with SessionFactory() as session:
        _seed_completed_primary_and_anchor(session, "pat_23", "cyc_23", t0)

    worker = ClinicalAutomationWorker(
        session_factory=SessionFactory,
        gateway_factory=lambda: gw,
        now_provider=clock.now,
        enable_automatic_weekly=True,
    )

    await worker.run_once(discover=True)  # Step A
    await worker.run_once(discover=True)  # Step B
    await worker.run_once(discover=True)  # Step C

    with SessionFactory() as session:
        w_svc = WeeklyConsiliumService(db=session)
        chat_msgs = w_svc.get_athena_chat_history("cyc_23")
        wrapup_msg = next((m for m in chat_msgs if m.get("sender") == "curator" and "силуэт человека" in m.get("text", "")), None)
        assert wrapup_msg is not None


@pytest.mark.anyio
async def test_24_wrapup_message_sourced_strictly_from_client_overall_report(tmp_path: Path):
    """24. Step C wrap-up prompt receives ONLY the published client-facing overall report."""
    db_file = tmp_path / "test_stage_4_24.db"
    _, SessionFactory = _create_isolated_db(db_file)
    t0 = datetime(2026, 11, 4, 10, 0, 0, tzinfo=timezone.utc)
    clock = ControlledClock(t0 + timedelta(hours=168))
    calls: List[Dict[str, Any]] = []
    gw = _build_stage_4_mock_gateway(calls)

    with SessionFactory() as session:
        _seed_completed_primary_and_anchor(session, "pat_24", "cyc_24", t0)

    worker = ClinicalAutomationWorker(
        session_factory=SessionFactory,
        gateway_factory=lambda: gw,
        now_provider=clock.now,
        enable_automatic_weekly=True,
    )

    await worker.run_once(discover=True)  # Step A
    await worker.run_once(discover=True)  # Step B
    await worker.run_once(discover=True)  # Step C

    wrapup_call = next(c for c in calls if "Internal Athena Weekly Wrap-up Message Formulation" in c["prompt"])
    prompt = wrapup_call["prompt"]
    # Check that client summary is present
    assert "Отличная неделя: пульс в покое стабилизировался" in prompt
    # Check that Pro internal strategy is NOT present in this prompt
    assert "УТВЕРЖДЁННАЯ СТРАТЕГИЯ PRO" not in prompt


@pytest.mark.anyio
async def test_25_wrapup_message_invites_to_silhouette_and_vault(tmp_path: Path):
    """25. Step C message explicitly invites the user to open the human silhouette / medical vault."""
    db_file = tmp_path / "test_stage_4_25.db"
    _, SessionFactory = _create_isolated_db(db_file)
    t0 = datetime(2026, 11, 4, 10, 0, 0, tzinfo=timezone.utc)
    clock = ControlledClock(t0 + timedelta(hours=168))
    calls: List[Dict[str, Any]] = []
    gw = _build_stage_4_mock_gateway(calls)

    with SessionFactory() as session:
        _seed_completed_primary_and_anchor(session, "pat_25", "cyc_25", t0)

    worker = ClinicalAutomationWorker(
        session_factory=SessionFactory,
        gateway_factory=lambda: gw,
        now_provider=clock.now,
        enable_automatic_weekly=True,
    )

    await worker.run_once(discover=True)  # Step A
    await worker.run_once(discover=True)  # Step B
    await worker.run_once(discover=True)  # Step C

    with SessionFactory() as session:
        w_svc = WeeklyConsiliumService(db=session)
        chat_msgs = w_svc.get_athena_chat_history("cyc_25")
        curator_texts = " ".join(m.get("text", "") for m in chat_msgs if m.get("sender") == "curator")
        assert "силуэт человека" in curator_texts or "сейф" in curator_texts


@pytest.mark.anyio
async def test_26_wrapup_message_does_not_consume_question_quota(tmp_path: Path):
    """26. Step C wrap-up message does not decrement the 3 questions/day quota."""
    db_file = tmp_path / "test_stage_4_26.db"
    _, SessionFactory = _create_isolated_db(db_file)
    t0 = datetime(2026, 11, 4, 10, 0, 0, tzinfo=timezone.utc)
    clock = ControlledClock(t0 + timedelta(hours=168))
    calls: List[Dict[str, Any]] = []
    gw = _build_stage_4_mock_gateway(calls)

    with SessionFactory() as session:
        _seed_completed_primary_and_anchor(session, "pat_26", "cyc_26", t0)

    worker = ClinicalAutomationWorker(
        session_factory=SessionFactory,
        gateway_factory=lambda: gw,
        now_provider=clock.now,
        enable_automatic_weekly=True,
    )

    await worker.run_once(discover=True)  # Step A
    await worker.run_once(discover=True)  # Step B
    await worker.run_once(discover=True)  # Step C

    with SessionFactory() as session:
        plan_svc = AthenaWeeklyPlanService(db=session)
        q_count = plan_svc.get_daily_planned_questions_count("cyc_26", as_of=clock.now())
        assert q_count == 0  # Zero questions consumed!


@pytest.mark.anyio
async def test_27_wrapup_message_does_not_create_daily_journal(tmp_path: Path):
    """27. Step C wrap-up message does not create or trigger a daily journal closure."""
    db_file = tmp_path / "test_stage_4_27.db"
    _, SessionFactory = _create_isolated_db(db_file)
    t0 = datetime(2026, 11, 4, 10, 0, 0, tzinfo=timezone.utc)
    clock = ControlledClock(t0 + timedelta(hours=168))
    calls: List[Dict[str, Any]] = []
    gw = _build_stage_4_mock_gateway(calls)

    with SessionFactory() as session:
        _seed_completed_primary_and_anchor(session, "pat_27", "cyc_27", t0)

    worker = ClinicalAutomationWorker(
        session_factory=SessionFactory,
        gateway_factory=lambda: gw,
        now_provider=clock.now,
        enable_automatic_weekly=True,
    )

    await worker.run_once(discover=True)  # Step A
    await worker.run_once(discover=True)  # Step B
    await worker.run_once(discover=True)  # Step C

    with SessionFactory() as session:
        j_svc = AthenaDailyJournalService(db=session)
        journals = j_svc.list_daily_journals("cyc_27")
        assert len(journals) == 0  # No daily journal was created!


@pytest.mark.anyio
async def test_28_no_pro_internal_data_in_chat_message(tmp_path: Path):
    """28. Chat message sent to patient contains zero Pro internal diagnostic terms."""
    db_file = tmp_path / "test_stage_4_28.db"
    _, SessionFactory = _create_isolated_db(db_file)
    t0 = datetime(2026, 11, 4, 10, 0, 0, tzinfo=timezone.utc)
    clock = ControlledClock(t0 + timedelta(hours=168))
    calls: List[Dict[str, Any]] = []
    gw = _build_stage_4_mock_gateway(calls)

    with SessionFactory() as session:
        _seed_completed_primary_and_anchor(session, "pat_28", "cyc_28", t0)

    worker = ClinicalAutomationWorker(
        session_factory=SessionFactory,
        gateway_factory=lambda: gw,
        now_provider=clock.now,
        enable_automatic_weekly=True,
    )

    await worker.run_once(discover=True)
    await worker.run_once(discover=True)
    await worker.run_once(discover=True)

    with SessionFactory() as session:
        w_svc = WeeklyConsiliumService(db=session)
        chat_msgs = w_svc.get_athena_chat_history("cyc_28")
        curator_texts = " ".join(m.get("text", "") for m in chat_msgs if m.get("sender") == "curator")
        assert "SERVER_ONLY" not in curator_texts
        assert "pro_strategy" not in curator_texts
        assert "chief_physician" not in curator_texts


@pytest.mark.anyio
async def test_28b_wrapup_message_honest_about_no_data_and_missing_telemetry(tmp_path: Path):
    """28b. Wrap-up message strictly reflects limitations without false claims of stability on NO_DATA / missing telemetry."""
    db_file = tmp_path / "test_stage_4_28b.db"
    _, SessionFactory = _create_isolated_db(db_file)
    t0 = datetime(2026, 11, 4, 10, 0, 0, tzinfo=timezone.utc)
    clock = ControlledClock(t0 + timedelta(hours=168))
    calls: List[Dict[str, Any]] = []

    pro_no_data_output = _sample_weekly_pro_output()
    pro_no_data_output["weekly_overall_report"] = {
        "title": "Итоги недели 1: Недостаточно телеметрии (NO_DATA)",
        "summary": "За прошедшую неделю данные с носимых устройств поступали фрагментарно, зафиксирован статус NO_DATA. Достоверно оценить динамику пульса и сна невозможно.",
        "highlights": [
            "Пропуски измерений 5 из 7 дней наблюдения",
            "Динамика показателей не может быть достоверно подтверждена",
        ],
    }

    async def custom_handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        prompt = body["contents"][0]["parts"][0]["text"]
        url_path = request.url.path
        calls.append({"url_path": url_path, "prompt": prompt})

        if "You are the Chief Physician" in prompt:
            return httpx.Response(200, json=_gemini_envelope(pro_no_data_output))
        if "You are the Head of Department" in prompt or "weekly_deputy" in prompt.lower():
            return httpx.Response(200, json=_gemini_envelope(_sample_weekly_flash_output()))
        if "Internal Athena Weekly Wrap-up Message Formulation" in prompt:
            assert (
                "NEVER claim guaranteed stability" in prompt
                or "КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО утверждать о стабильности" in prompt
            )
            assert "Недостаточно телеметрии" in prompt or "NO_DATA" in prompt
            honest_resp = {
                "message": (
                    "Здравствуйте! Подошла к концу первая неделя наблюдения. "
                    "В этот период данных с устройств поступило немного, поэтому пока нельзя сделать окончательный вывод о динамике показателей. "
                    "Мы продолжим бережное наблюдение на следующей неделе. "
                    "Вы можете нажать на силуэт человека на главном экране приложения, чтобы посмотреть текущий отчёт в медицинском сейфе."
                )
            }
            return httpx.Response(200, json=_gemini_envelope(honest_resp))
        if "Internal Weekly Plan Formulation Scenario" in prompt:
            return httpx.Response(200, json=_gemini_envelope(_sample_weekly_plan("cyc_28b", week_number=2)))
        return httpx.Response(200, json=_gemini_envelope({"status": "ok"}))

    gw = GeminiGateway(
        api_key="test-stage-4-key",
        transport=httpx.MockTransport(custom_handler),
        max_retries=1,
        sleep_func=lambda _: asyncio.sleep(0),
    )

    with SessionFactory() as session:
        _seed_completed_primary_and_anchor(session, "pat_28b", "cyc_28b", t0)

    worker = ClinicalAutomationWorker(
        session_factory=SessionFactory,
        gateway_factory=lambda: gw,
        now_provider=clock.now,
        enable_automatic_weekly=True,
    )

    await worker.run_once(discover=True)
    await worker.run_once(discover=True)
    await worker.run_once(discover=True)

    with SessionFactory() as session:
        w_svc = WeeklyConsiliumService(db=session)
        chat_msgs = w_svc.get_athena_chat_history("cyc_28b")
        wrapup_msg = next(m for m in chat_msgs if m.get("sender") == "curator" and "силуэт человека" in m.get("text", ""))
        text = wrapup_msg["text"]
        assert "данных с устройств поступило немного" in text or "пока нельзя сделать" in text
        assert "показатели пульса стабильны" not in text
        assert "отличная адаптация" not in text
        assert "уровень энергии повысился" not in text


# ==============================================================================
# SECTION E: FAILURES, RETRIES & CONCURRENCY (TESTS 29 - 36)
# ==============================================================================

@pytest.mark.anyio
async def test_29_step_a_transient_failure_and_retry(tmp_path: Path):
    """29. Step A fails with transient error on attempt 1, retries and succeeds on attempt 2."""
    db_file = tmp_path / "test_stage_4_29.db"
    _, SessionFactory = _create_isolated_db(db_file)
    t0 = datetime(2026, 11, 4, 10, 0, 0, tzinfo=timezone.utc)
    clock = ControlledClock(t0 + timedelta(hours=168))
    calls: List[Dict[str, Any]] = []

    # Fail on attempt 1
    gw = _build_stage_4_mock_gateway(calls, fail_on_step_a=True)

    with SessionFactory() as session:
        _seed_completed_primary_and_anchor(session, "pat_29", "cyc_29", t0)

    worker = ClinicalAutomationWorker(
        session_factory=SessionFactory,
        gateway_factory=lambda: gw,
        now_provider=clock.now,
        base_backoff_seconds=10,
        enable_automatic_weekly=True,
    )

    # Attempt 1: fails
    await worker.run_once(discover=True)
    with SessionFactory() as session:
        auto_svc = ClinicalAutomationService(db=session)
        job = auto_svc.get_job("cyc_29", AutomationJobType.WEEKLY_CONSILIUM_A)
        assert job is not None
        assert job.status == AutomationJobStatus.RETRYABLE_FAILURE
        assert job.attempts == 1

    # Advance clock beyond backoff
    clock.advance(timedelta(seconds=15))

    # Attempt 2: succeeds
    await worker.run_once(discover=True)
    with SessionFactory() as session:
        auto_svc = ClinicalAutomationService(db=session)
        job = auto_svc.get_job("cyc_29", AutomationJobType.WEEKLY_CONSILIUM_A)
        assert job is not None
        assert job.status == AutomationJobStatus.COMPLETED
        w_svc = WeeklyConsiliumService(db=session)
        assert w_svc.is_weekly_consilium_completed("cyc_29", sprint_number=1)


@pytest.mark.anyio
async def test_30_step_b_failure_does_not_repeat_step_a(tmp_path: Path):
    """30. Failure in Step B retries Step B independently without re-running Step A."""
    db_file = tmp_path / "test_stage_4_30.db"
    _, SessionFactory = _create_isolated_db(db_file)
    t0 = datetime(2026, 11, 4, 10, 0, 0, tzinfo=timezone.utc)
    clock = ControlledClock(t0 + timedelta(hours=168))
    calls: List[Dict[str, Any]] = []

    # Inject Step B transient failure
    gw = _build_stage_4_mock_gateway(calls, fail_on_step_b=True)

    with SessionFactory() as session:
        _seed_completed_primary_and_anchor(session, "pat_30", "cyc_30", t0)

    worker = ClinicalAutomationWorker(
        session_factory=SessionFactory,
        gateway_factory=lambda: gw,
        now_provider=clock.now,
        base_backoff_seconds=10,
        enable_automatic_weekly=True,
    )

    # Tick 1: Step A succeeds, Step B enqueued
    await worker.run_once(discover=True)
    step_a_calls_count = len([c for c in calls if ("Deputy Chief Physician" in c["prompt"] or "Head of Department" in c["prompt"]) or "Chief Physician" in c["prompt"]])
    assert step_a_calls_count == 2  # Flash + Pro

    # Tick 2: Step B runs and fails
    await worker.run_once(discover=True)
    with SessionFactory() as session:
        auto_svc = ClinicalAutomationService(db=session)
        b_job = auto_svc.get_job("cyc_30", AutomationJobType.WEEKLY_CONSILIUM_B)
        assert b_job is not None
        assert b_job.status == AutomationJobStatus.RETRYABLE_FAILURE

    # Advance clock past backoff
    clock.advance(timedelta(seconds=15))

    # Tick 3: Step B retries and succeeds!
    await worker.run_once(discover=True)
    with SessionFactory() as session:
        auto_svc = ClinicalAutomationService(db=session)
        b_job = auto_svc.get_job("cyc_30", AutomationJobType.WEEKLY_CONSILIUM_B)
        assert b_job is not None
        assert b_job.status == AutomationJobStatus.COMPLETED

    # Step A must NOT have been called again!
    total_step_a_calls = len([c for c in calls if ("Deputy Chief Physician" in c["prompt"] or "Head of Department" in c["prompt"]) or "Chief Physician" in c["prompt"]])
    assert total_step_a_calls == 2


@pytest.mark.anyio
async def test_31_step_c_failure_does_not_repeat_step_a_or_b(tmp_path: Path):
    """31. Failure in Step C retries Step C independently without re-running Step A or Step B."""
    db_file = tmp_path / "test_stage_4_31.db"
    _, SessionFactory = _create_isolated_db(db_file)
    t0 = datetime(2026, 11, 4, 10, 0, 0, tzinfo=timezone.utc)
    clock = ControlledClock(t0 + timedelta(hours=168))
    calls: List[Dict[str, Any]] = []

    # Inject Step C transient failure
    gw = _build_stage_4_mock_gateway(calls, fail_on_step_c=True)

    with SessionFactory() as session:
        _seed_completed_primary_and_anchor(session, "pat_31", "cyc_31", t0)

    worker = ClinicalAutomationWorker(
        session_factory=SessionFactory,
        gateway_factory=lambda: gw,
        now_provider=clock.now,
        base_backoff_seconds=10,
        enable_automatic_weekly=True,
    )

    await worker.run_once(discover=True)  # Step A succeeds
    await worker.run_once(discover=True)  # Step B succeeds, Step C enqueued
    await worker.run_once(discover=True)  # Step C fails

    with SessionFactory() as session:
        auto_svc = ClinicalAutomationService(db=session)
        c_job = auto_svc.get_job("cyc_31", AutomationJobType.WEEKLY_CONSILIUM_C)
        assert c_job is not None
        assert c_job.status == AutomationJobStatus.RETRYABLE_FAILURE

    # Advance clock past backoff
    clock.advance(timedelta(seconds=15))

    # Step C retries and succeeds
    await worker.run_once(discover=True)
    with SessionFactory() as session:
        auto_svc = ClinicalAutomationService(db=session)
        c_job = auto_svc.get_job("cyc_31", AutomationJobType.WEEKLY_CONSILIUM_C)
        assert c_job is not None
        assert c_job.status == AutomationJobStatus.COMPLETED

    # Verify Step A was called exactly 2 times (Flash + Pro) and Step B exactly 1 time
    step_a_calls = len([c for c in calls if ("Deputy Chief Physician" in c["prompt"] or "Head of Department" in c["prompt"]) or "Chief Physician" in c["prompt"]])
    step_b_calls = len([c for c in calls if "Internal Weekly Plan Formulation Scenario" in c["prompt"]])
    assert step_a_calls == 2
    assert step_b_calls == 1


@pytest.mark.anyio
async def test_32_server_restart_between_step_a_and_step_b(tmp_path: Path):
    """32. Server restart after Step A resumes from Step B with zero extra calls for Step A."""
    db_file = tmp_path / "test_stage_4_32.db"
    _, SessionFactory = _create_isolated_db(db_file)
    t0 = datetime(2026, 11, 4, 10, 0, 0, tzinfo=timezone.utc)
    clock = ControlledClock(t0 + timedelta(hours=168))
    calls_worker1: List[Dict[str, Any]] = []
    gw1 = _build_stage_4_mock_gateway(calls_worker1)

    with SessionFactory() as session:
        _seed_completed_primary_and_anchor(session, "pat_32", "cyc_32", t0)

    # Worker 1 runs Step A then "crashes"
    worker_1 = ClinicalAutomationWorker(
        session_factory=SessionFactory,
        gateway_factory=lambda: gw1,
        now_provider=clock.now,
        enable_automatic_weekly=True,
    )
    await worker_1.run_once(discover=True)  # Step A completed, Step B enqueued

    calls_worker2: List[Dict[str, Any]] = []
    gw2 = _build_stage_4_mock_gateway(calls_worker2)

    # Worker 2 restarts
    worker_2 = ClinicalAutomationWorker(
        session_factory=SessionFactory,
        gateway_factory=lambda: gw2,
        now_provider=clock.now,
        enable_automatic_weekly=True,
    )
    await worker_2.run_once(discover=True)  # Should claim and execute Step B

    with SessionFactory() as session:
        auto_svc = ClinicalAutomationService(db=session)
        b_job = auto_svc.get_job("cyc_32", AutomationJobType.WEEKLY_CONSILIUM_B)
        assert b_job is not None
        assert b_job.status == AutomationJobStatus.COMPLETED

    # Worker 2 must NOT have called Flash Deputy or Pro Chief!
    worker2_step_a = [c for c in calls_worker2 if ("Deputy Chief Physician" in c["prompt"] or "Head of Department" in c["prompt"]) or "Chief Physician" in c["prompt"]]
    assert len(worker2_step_a) == 0


@pytest.mark.anyio
async def test_33_server_restart_between_step_b_and_step_c(tmp_path: Path):
    """33. Server restart after Step B resumes from Step C with zero extra calls for Step A or B."""
    db_file = tmp_path / "test_stage_4_33.db"
    _, SessionFactory = _create_isolated_db(db_file)
    t0 = datetime(2026, 11, 4, 10, 0, 0, tzinfo=timezone.utc)
    clock = ControlledClock(t0 + timedelta(hours=168))
    calls_worker1: List[Dict[str, Any]] = []
    gw1 = _build_stage_4_mock_gateway(calls_worker1)

    with SessionFactory() as session:
        _seed_completed_primary_and_anchor(session, "pat_33", "cyc_33", t0)

    worker_1 = ClinicalAutomationWorker(
        session_factory=SessionFactory,
        gateway_factory=lambda: gw1,
        now_provider=clock.now,
        enable_automatic_weekly=True,
    )
    await worker_1.run_once(discover=True)  # Step A
    await worker_1.run_once(discover=True)  # Step B

    calls_worker2: List[Dict[str, Any]] = []
    gw2 = _build_stage_4_mock_gateway(calls_worker2)

    # Worker 2 restarts
    worker_2 = ClinicalAutomationWorker(
        session_factory=SessionFactory,
        gateway_factory=lambda: gw2,
        now_provider=clock.now,
        enable_automatic_weekly=True,
    )
    await worker_2.run_once(discover=True)  # Executes Step C

    with SessionFactory() as session:
        auto_svc = ClinicalAutomationService(db=session)
        c_job = auto_svc.get_job("cyc_33", AutomationJobType.WEEKLY_CONSILIUM_C)
        assert c_job is not None
        assert c_job.status == AutomationJobStatus.COMPLETED

    # Worker 2 only called wrap-up; never Step A or B
    assert len(calls_worker2) == 1
    assert "Internal Athena Weekly Wrap-up Message Formulation" in calls_worker2[0]["prompt"]


@pytest.mark.anyio
async def test_34_subsequent_worker_ticks_produce_no_duplicates(tmp_path: Path):
    """34. Repeated worker ticks after completion produce zero extra calls, plans, or messages."""
    db_file = tmp_path / "test_stage_4_34.db"
    _, SessionFactory = _create_isolated_db(db_file)
    t0 = datetime(2026, 11, 4, 10, 0, 0, tzinfo=timezone.utc)
    clock = ControlledClock(t0 + timedelta(hours=168))
    calls: List[Dict[str, Any]] = []
    gw = _build_stage_4_mock_gateway(calls)

    with SessionFactory() as session:
        _seed_completed_primary_and_anchor(session, "pat_34", "cyc_34", t0)

    worker = ClinicalAutomationWorker(
        session_factory=SessionFactory,
        gateway_factory=lambda: gw,
        now_provider=clock.now,
        enable_automatic_weekly=True,
    )

    await worker.run_once(discover=True)  # Step A
    await worker.run_once(discover=True)  # Step B
    await worker.run_once(discover=True)  # Step C
    calls_count_after_completion = len(calls)

    # Subsequent ticks at T0 + 170h, T0 + 175h, etc.
    for offset_h in (170, 175, 180):
        clock.set(t0 + timedelta(hours=offset_h))
        await worker.run_once(discover=True)

    # Zero additional Gemini calls made!
    assert len(calls) == calls_count_after_completion

    with SessionFactory() as session:
        w_svc = WeeklyConsiliumService(db=session)
        chat_msgs = w_svc.get_athena_chat_history("cyc_34")
        wrapup_msgs = [m for m in chat_msgs if m.get("sender") == "curator" and "силуэт человека" in m.get("text", "")]
        assert len(wrapup_msgs) == 1  # Exactly 1 message, no duplicates!


@pytest.mark.anyio
async def test_35_concurrency_protection_two_workers(tmp_path: Path):
    """35. Two concurrent workers cannot execute the same step simultaneously (SQLite lease fencing)."""
    db_file = tmp_path / "test_stage_4_35.db"
    _, SessionFactory = _create_isolated_db(db_file)
    t0 = datetime(2026, 11, 4, 10, 0, 0, tzinfo=timezone.utc)
    clock = ControlledClock(t0 + timedelta(hours=168))

    with SessionFactory() as session:
        _seed_completed_primary_and_anchor(session, "pat_35", "cyc_35", t0)
        auto_svc = ClinicalAutomationService(db=session, now_provider=clock.now)
        step_a_job = auto_svc.enqueue_weekly_consilium_a(
            cycle_id="cyc_35",
            patient_id="pat_35",
            sprint_number=1,
            available_at_utc=t0 + timedelta(hours=168),
        )
        assert step_a_job is not None

    # Worker A claims the job
    with SessionFactory() as session_a:
        svc_a = ClinicalAutomationService(db=session_a, now_provider=clock.now)
        claimed_a = svc_a.claim_job(step_a_job.job_id)
        assert claimed_a is not None
        assert claimed_a.status == AutomationJobStatus.RUNNING

        # Worker B attempts to claim the same job while Worker A holds active lease
        with SessionFactory() as session_b:
            svc_b = ClinicalAutomationService(db=session_b, now_provider=clock.now)
            claimed_b = svc_b.claim_job(step_a_job.job_id)
            assert claimed_b is None  # Fenced out!


@pytest.mark.anyio
async def test_36_completed_weekly_consilium_idempotency_zero_gemini_calls(tmp_path: Path):
    """36. Direct call to run_weekly_consilium on completed sprint returns recovered response with 0 AI calls."""
    db_file = tmp_path / "test_stage_4_36.db"
    _, SessionFactory = _create_isolated_db(db_file)
    t0 = datetime(2026, 11, 4, 10, 0, 0, tzinfo=timezone.utc)
    clock = ControlledClock(t0 + timedelta(hours=168))
    calls: List[Dict[str, Any]] = []
    gw = _build_stage_4_mock_gateway(calls)

    with SessionFactory() as session:
        _seed_completed_primary_and_anchor(session, "pat_36", "cyc_36", t0)
        w_svc = WeeklyConsiliumService(db=session, gateway=gw, now_provider=clock.now)
        resp1 = await w_svc.run_weekly_consilium(
            cycle_id="cyc_36",
            sprint_id="sprint_cyc_36_w1",
            idempotency_key="weekly_core_cyc_36_w1",
        )
        assert resp1 is not None
        calls_first_run = len(calls)
        assert calls_first_run == 2

        # Second call with same parameters
        resp2 = await w_svc.run_weekly_consilium(
            cycle_id="cyc_36",
            sprint_id="sprint_cyc_36_w1",
            idempotency_key="weekly_core_cyc_36_w1",
        )
        assert resp2 is not None
        assert resp2.overall_report.report_id == resp1.overall_report.report_id
        # Zero extra AI calls!
        assert len(calls) == calls_first_run


# ==============================================================================
# SECTION F: FULL SEVEN-DAY E2E INTEGRATION TEST (TEST 37)
# ==============================================================================

@pytest.mark.anyio
async def test_37_full_seven_day_e2e_lifecycle(tmp_path: Path):
    """
    37. Comprehensive 7-Day E2E Integration Test:
    Primary Consilium → стратегия Pro → Athena Weekly Plan → приветствие T₀ →
    первый вопрос через 30 секунд → ответы пользователя → Wellness READY/NO_DATA →
    внутренние Daily Journals → T₀ + 168 часов → автоматический Weekly Step A →
    новая стратегия Pro → Step B новый план Athena Week 2 → Step C отдельное итоговое сообщение в чат.
    """
    db_file = tmp_path / "test_stage_4_37_e2e.db"
    engine, SessionFactory = _create_isolated_db(db_file)
    t0 = datetime(2026, 11, 4, 9, 0, 0, tzinfo=timezone.utc)
    clock = ControlledClock(t0)
    recorded_calls: List[Dict[str, Any]] = []
    gw = _build_stage_4_mock_gateway(recorded_calls)

    patient_id = "patient_e2e_7d"
    cycle_id = "cycle_e2e_7d"

    # 1. Seed completed manual Primary Consilium & Initial T0 greeting
    with SessionFactory() as session:
        _seed_completed_primary_and_anchor(session, patient_id, cycle_id, t0)

    # 2. Worker configured with all automation capabilities enabled
    worker = ClinicalAutomationWorker(
        session_factory=SessionFactory,
        gateway_factory=lambda: gw,
        now_provider=clock.now,
        enable_weekly_plan=True,
        enable_daily_journal=True,
        enable_wellness_daily=True,
        enable_automatic_weekly=True,
    )

    # 3. Simulate Day 1..7: Advance virtual clock day by day
    # Day 1: T0 + 30 seconds -> first planned question
    clock.advance(timedelta(seconds=30))
    await worker.run_once(discover=True)

    # Simulate patient answers and daily journals across days 1..6
    with SessionFactory() as session:
        # Seed 6 daily journals (Day 7 deliberately missing to test partial telemetry handling)
        _seed_daily_journals(session, cycle_id, patient_id, t0, days_count=6, calendar_week=1)
        # Add an unjournaled patient answer at Day 7, 160 hours (before 168h cutoff)
        w_svc = WeeklyConsiliumService(db=session)
        w_svc.save_athena_chat_message(
            cycle_id=cycle_id,
            sender="user",
            text="День 7: чувствую бодрость, прогулка 45 минут прошла легко",
            timestamp=(t0 + timedelta(hours=160)).isoformat(),
            commit=True,
        )

    # 4. Advance virtual time to EXACTLY T0 + 168 hours!
    clock.set(t0 + timedelta(hours=168))

    # 5. Worker Tick 1: Discovers and executes Step A (Automatic Weekly Consilium)
    processed_step_a = await worker.run_once(discover=True)
    step_a_rec = next((j for j in processed_step_a if j.job_type == AutomationJobType.WEEKLY_CONSILIUM_A), None)
    assert step_a_rec is not None
    assert step_a_rec.status == AutomationJobStatus.COMPLETED

    # Verify Step A outputs in database:
    with SessionFactory() as session:
        w_svc = WeeklyConsiliumService(db=session)
        assert w_svc.is_weekly_consilium_completed(cycle_id, sprint_number=1)
        r_repo = ModelReportRepository(session)
        # Check overall report published for client
        overalls = r_repo.list_client_facing(cycle_id=cycle_id, report_type=WEEKLY_OVERALL_CLIENT_REPORT_TYPE)
        assert len(overalls) == 1
        # Check new Pro active strategy saved as SERVER_ONLY
        strats = r_repo.list_server_only(cycle_id=cycle_id, report_type=ATHENA_ACTIVE_STRATEGY_REPORT_TYPE)
        assert len(strats) >= 2
        # Check that Step B job was automatically enqueued
        auto_svc = ClinicalAutomationService(db=session)
        b_job = auto_svc.get_job(cycle_id, AutomationJobType.WEEKLY_CONSILIUM_B)
        assert b_job is not None

    # 6. Worker Tick 2: Discovers and executes Step B (Athena Week 2 Goal Plan)
    processed_step_b = await worker.run_once(discover=True)
    step_b_rec = next((j for j in processed_step_b if j.job_type == AutomationJobType.WEEKLY_CONSILIUM_B), None)
    assert step_b_rec is not None
    assert step_b_rec.status == AutomationJobStatus.COMPLETED

    # Verify Step B outputs in database:
    with SessionFactory() as session:
        plan_svc = AthenaWeeklyPlanService(db=session)
        w2_plan = plan_svc.get_active_weekly_plan(cycle_id, week_number=2)
        assert w2_plan is not None
        assert w2_plan.week_number == 2
        assert len(w2_plan.goals) >= 1
        # Check that Step C job was automatically enqueued
        auto_svc = ClinicalAutomationService(db=session)
        c_job = auto_svc.get_job(cycle_id, AutomationJobType.WEEKLY_CONSILIUM_C)
        assert c_job is not None

    # 7. Worker Tick 3: Discovers and executes Step C (Athena Weekly Wrap-up Message)
    processed_step_c = await worker.run_once(discover=True)
    step_c_rec = next((j for j in processed_step_c if j.job_type == AutomationJobType.WEEKLY_CONSILIUM_C), None)
    assert step_c_rec is not None
    assert step_c_rec.status == AutomationJobStatus.COMPLETED

    # Verify Step C outputs in database:
    with SessionFactory() as session:
        w_svc = WeeklyConsiliumService(db=session)
        chat_msgs = w_svc.get_athena_chat_history(cycle_id)
        # Check wrap-up message in chat history
        wrapup_msg = next((m for m in chat_msgs if m.get("sender") == "curator" and "силуэт человека" in m.get("text", "")), None)
        assert wrapup_msg is not None
        # Verify question limits were untouched
        plan_svc = AthenaWeeklyPlanService(db=session)
        assert plan_svc.get_daily_planned_questions_count(cycle_id, as_of=clock.now()) == 0
        # Verify T0 anchor unchanged
        assert w_svc.find_schedule_anchor_timestamp(cycle_id) == t0
        # Verify next week (week 2) is scheduled for T0 + 336h
        auto_svc = ClinicalAutomationService(db=session)
        await worker.run_once(discover=True)
        w2_job = auto_svc.get_job(cycle_id, AutomationJobType.WEEKLY_CONSILIUM_A, f"weekly_consilium_step_a_{cycle_id}_w2")
        assert w2_job is not None
        assert w2_job.next_attempt_at_utc == (t0 + timedelta(hours=336)).isoformat()
