import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from app.config import settings
from app.database import Base, get_db
from app.main import app
from app.models.health_measurement import HealthMeasurement
from app.models.model_report import ModelReport
from app.models.patient import Patient
from app.models.medical_cycle import MedicalCycle
from app.repositories.cycle_repository import MedicalCycleRepository
from app.repositories.patient_repository import PatientRepository
from app.repositories.report_repository import ModelReportRepository
from app.schemas.chat import (
    AthenaChatRequest,
    AthenaChatResponse,
    AthenaGoalItem,
    AthenaGoalStatus,
    AthenaWeeklyPlanRecord,
)
from app.schemas.privacy import PrivacyLevel
from app.services.athena_chat_service import (
    ATHENA_CHAT_TURN_REPORT_TYPE,
    ATHENA_ROUTINE_QUESTION_REPORT_TYPE,
    AthenaChatService,
)
from app.services.athena_weekly_plan_service import (
    ATHENA_WEEKLY_PLAN_REPORT_TYPE,
    AthenaWeeklyPlanService,
)
from app.services.clinical_automation_service import (
    AutomationJobStatus,
    AutomationJobType,
    ClinicalAutomationService,
    ClinicalAutomationWorker,
)
from app.services.gemini_gateway import GeminiGateway, GeminiModelRole, GeminiValidationError
from app.services.weekly_consilium_service import (
    ATHENA_CLINICAL_LOG_REPORT_TYPE,
    WeeklyConsiliumService,
)
from tests.test_athena_response_driven_accompaniment_stage_a2_1 import ControlledClock


def _create_file_db(db_path: Path, busy_timeout_ms: int = 5000):
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False, "timeout": busy_timeout_ms / 1000.0},
        pool_pre_ping=True,
    )

    @event.listens_for(engine, "connect")
    def _set_pragmas(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)};")
        cursor.close()

    Base.metadata.create_all(bind=engine)
    SessionFactory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    return engine, SessionFactory


def _seed_completed_primary(
    session: Session,
    patient_id: str,
    cycle_id: str,
    anchor_dt: datetime,
) -> None:
    now_dt = anchor_dt
    p = Patient(patient_id=patient_id, status="active", created_at=now_dt)
    c = MedicalCycle(cycle_id=cycle_id, patient_id=patient_id, status="active", created_at=now_dt)
    session.add(p)
    session.add(c)
    session.commit()

    # User overall report
    session.add(
        ModelReport(
            report_id=f"rep_{cycle_id}_user_overall_v1",
            cycle_id=cycle_id,
            model="aegis-primary-pro",
            report_type="user_overall_report",
            privacy_level=PrivacyLevel.CLIENT_FACING.value,
            version=1,
            content={"title": "Первичный консилиум", "summary": "Базовое обследование завершено."},
            created_at=now_dt,
        )
    )

    # Chief Physician strategy
    session.add(
        ModelReport(
            report_id=f"rep_{cycle_id}_chief_physician_internal_v1",
            cycle_id=cycle_id,
            model="aegis-primary-pro",
            report_type="chief_physician_internal",
            privacy_level=PrivacyLevel.SERVER_ONLY.value,
            version=1,
            content={
                "pro_general_shelf": "Динамический мониторинг адаптации к умеренным аэробным нагрузкам и контроль утреннего АД.",
                "pro_training_shelf": "Аэробные тренировки умеренной интенсивности 3 раза в неделю.",
            },
            created_at=now_dt,
        )
    )

    # Preparatory Anamnesis with known facts
    session.add(
        ModelReport(
            report_id=f"rep_{cycle_id}_anamnesis_v1",
            cycle_id=cycle_id,
            model="aegis-anamnesis-m1",
            report_type="preparatory_anamnesis_m1",
            privacy_level=PrivacyLevel.SERVER_ONLY.value,
            version=1,
            content={
                "lifestyle": {
                    "smoking": "Не курит",
                    "alcohol": "Редко, по праздникам",
                    "physical_activity": "Сидячий образ жизни, редкие прогулки",
                },
                "complaints": ["Утренняя сонливость", "Редкие головные боли напряжения"],
                "allergies": "Аллергических реакций не отмечает",
            },
            created_at=now_dt,
        )
    )

    # Raw user anamnesis
    session.add(
        ModelReport(
            report_id=f"rep_{cycle_id}_raw_anamnesis_v1",
            cycle_id=cycle_id,
            model="aegis-anamnesis-raw",
            report_type="raw_user_anamnesis",
            privacy_level=PrivacyLevel.SERVER_ONLY.value,
            version=1,
            content={
                "answers": {
                    "age": 42,
                    "gender": "male",
                    "chronic_diseases": "None",
                    "medications": "None",
                }
            },
            created_at=now_dt,
        )
    )
    session.commit()


def _sample_weekly_plan(cycle_id: str, num_goals: int = 4) -> AthenaWeeklyPlanRecord:
    goals = [
        AthenaGoalItem(
            goal_id=f"goal_{i+1}",
            title=f"Наблюдение за аспектом {i+1}",
            clinical_rationale=f"Клиническое обоснование для цели {i+1}",
            priority=i + 1 if i < 5 else 5,
            known_facts=["Не курит", "Возраст 42 года"],
            target_clarification=f"Уточнить самочувствие по аспекту {i+1}",
            sufficiency_criteria=f"Получен ответ о статусе аспекта {i+1}",
            status=AthenaGoalStatus.ACTIVE if i == 0 else AthenaGoalStatus.ACTIVE,
        )
        for i in range(num_goals)
    ]
    return AthenaWeeklyPlanRecord(
        plan_id=f"plan_{cycle_id}_w1_test",
        cycle_id=cycle_id,
        week_number=1,
        source_strategy_report_id=f"rep_{cycle_id}_chief_physician_internal_v1",
        source_strategy_version=1,
        created_at_utc=datetime.now(timezone.utc).isoformat(),
        summary_for_curator="План динамического наблюдения Афины на 1-ю неделю",
        goals=goals,
        status="active",
    )


def _build_stage_1_mock_gateway(
    call_log: List[Dict[str, Any]],
    plan_to_return: Optional[AthenaWeeklyPlanRecord] = None,
) -> GeminiGateway:
    class MockStage1Gateway(GeminiGateway):
        def __init__(self):
            super().__init__()
            self._api_key = "test-stage-1-key"

        async def generate_raw_text(self, contents: List[Dict[str, Any]], **kwargs) -> str:
            call_log.append({"contents": contents, "method": "generate_raw_text", "kwargs": kwargs})
            system_inst = str(kwargs.get("system_instruction") or "")
            last_text = contents[-1]["parts"][0].get("text", "") if contents and contents[-1].get("parts") else ""

            if "initial introduction" in last_text.lower() or "первое знакомство" in last_text.lower() or "initialization" in last_text.lower() or "initial_greeting" in last_text.lower() or "(t0)" in last_text.lower():
                return (
                    "Здравствуйте! Меня зовут Афина. Я ваш персональный AI-ассистент в системе Aegis. "
                    "Моя задача — бережно сопровождать вас каждый день и помогать сохранять здоровье.\n\n"
                    "[CLINICAL_REPORT]\n"
                    "{\n"
                    '  "status": "INITIALIZATION",\n'
                    '  "symptoms_mentioned": [],\n'
                    '  "adherence_to_plan": "UNKNOWN",\n'
                    '  "red_flags": false,\n'
                    '  "summary_for_master": "Athena initialization completed at T0."\n'
                    "}\n"
                    "[/CLINICAL_REPORT]"
                )

            # First planned question or daily pulse
            return (
                "Как ваше самочувствие сегодня утром? Удалось ли выспаться?\n"
                "1. Отлично, полон сил.\n"
                "2. Нормально, но долго просыпался.\n"
                "3. Чувствую усталость.\n\n"
                "[CLINICAL_REPORT]\n"
                "{\n"
                '  "status": "NORMAL",\n'
                '  "symptoms_mentioned": [],\n'
                '  "subjective_wellbeing_score": 8,\n'
                '  "adherence_to_plan": "HIGH",\n'
                '  "red_flags": false,\n'
                '  "summary_for_master": "First planned question delivered."\n'
                "}\n"
                "[/CLINICAL_REPORT]"
            )

        async def generate_structured(self, prompt: str, response_schema: Any, **kwargs) -> Any:
            call_log.append({
                "prompt": prompt,
                "method": "generate_structured",
                "schema": getattr(response_schema, "__name__", str(response_schema)),
                "kwargs": kwargs,
            })
            if response_schema is AthenaWeeklyPlanRecord:
                if plan_to_return is not None:
                    return plan_to_return
                # Default 4-goal plan
                return _sample_weekly_plan("default_cycle", num_goals=4)
            return response_schema()

    return MockStage1Gateway()


# ==============================================================================
# TESTS FOR STAGE 1 — ATHENA WEEKLY PLAN
# ==============================================================================

@pytest.mark.anyio
async def test_01_weekly_plan_autonomous_generation(tmp_path: Path):
    """
    Verifies Section 1:
    - Athena autonomously generates a structured AthenaWeeklyPlanRecord via Gemini Flash.
    - Persisted in model_reports with report_type='athena_weekly_plan' and PrivacyLevel.SERVER_ONLY.
    - Goals specify clinical rationale, target clarification, known facts, sufficiency criteria.
    """
    db_file = tmp_path / "test_s1_01.db"
    engine, SessionFactory = _create_file_db(db_file)
    t0 = datetime(2026, 11, 4, 9, 0, 0, tzinfo=timezone.utc)
    clock = ControlledClock(t0)
    calls: List[Dict[str, Any]] = []

    plan_mock = _sample_weekly_plan("cyc_01", num_goals=5)
    gw = _build_stage_1_mock_gateway(calls, plan_to_return=plan_mock)

    with SessionFactory() as session:
        _seed_completed_primary(session, "pat_01", "cyc_01", t0)
        plan_service = AthenaWeeklyPlanService(db=session, gateway=gw, now_provider=clock.now)

        plan = await plan_service.create_or_get_weekly_plan("cyc_01", week_number=1)

        # Verify structured Gemini Flash call was made
        assert len(calls) == 1
        assert calls[0]["method"] == "generate_structured"
        assert calls[0]["schema"] == "AthenaWeeklyPlanRecord"
        assert "cyc_01" in calls[0]["prompt"]
        assert "Контроль утреннего АД" in calls[0]["prompt"] or "динамический мониторинг" in calls[0]["prompt"].lower()

        # Verify returned plan record
        assert plan.cycle_id == "cyc_01"
        assert plan.week_number == 1
        assert len(plan.goals) == 5
        assert plan.status == "active"

        # Verify goal attributes
        g1 = plan.goals[0]
        assert g1.goal_id == "goal_1"
        assert g1.priority == 1
        assert len(g1.clinical_rationale) > 0
        assert len(g1.target_clarification) > 0
        assert len(g1.sufficiency_criteria) > 0
        assert "Не курит" in g1.known_facts

        # Verify persisted under SERVER_ONLY
        rep_repo = ModelReportRepository(session)
        reports = rep_repo.list_server_only(cycle_id="cyc_01", report_type=ATHENA_WEEKLY_PLAN_REPORT_TYPE)
        assert len(reports) == 1
        persisted = reports[0]
        assert persisted.privacy_level == PrivacyLevel.SERVER_ONLY.value
        assert persisted.content["plan_id"] == plan.plan_id
        assert len(persisted.content["goals"]) == 5
    engine.dispose()


@pytest.mark.anyio
async def test_02_weekly_plan_goal_count_is_flexible_not_constrained_to_7_days(tmp_path: Path):
    """
    Verifies Section 1 constraint:
    - Number of goals is NOT artificially constrained by the 7 days of the week.
    - Supports e.g. 3 goals, 6 goals, 10 goals.
    """
    db_file = tmp_path / "test_s1_02.db"
    engine, SessionFactory = _create_file_db(db_file)
    t0 = datetime(2026, 11, 4, 9, 0, 0, tzinfo=timezone.utc)
    clock = ControlledClock(t0)

    for goal_count in (3, 7, 10):
        calls: List[Dict[str, Any]] = []
        plan_mock = _sample_weekly_plan(f"cyc_{goal_count}", num_goals=goal_count)
        gw = _build_stage_1_mock_gateway(calls, plan_to_return=plan_mock)

        with SessionFactory() as session:
            _seed_completed_primary(session, f"pat_{goal_count}", f"cyc_{goal_count}", t0)
            plan_service = AthenaWeeklyPlanService(db=session, gateway=gw, now_provider=clock.now)

            plan = await plan_service.create_or_get_weekly_plan(f"cyc_{goal_count}", week_number=1)
            assert len(plan.goals) == goal_count
            assert plan.goals[-1].goal_id == f"goal_{goal_count}"
    engine.dispose()


@pytest.mark.anyio
async def test_03_weekly_plan_known_facts_excluded_from_questions(tmp_path: Path):
    """
    Verifies Section 1 known facts rule:
    - Known facts from Primary Consilium / anamnesis are passed to the weekly plan prompt.
    - System explicitly commands not to ask questions about established facts.
    """
    db_file = tmp_path / "test_s1_03.db"
    engine, SessionFactory = _create_file_db(db_file)
    t0 = datetime(2026, 11, 4, 9, 0, 0, tzinfo=timezone.utc)
    clock = ControlledClock(t0)
    calls: List[Dict[str, Any]] = []
    gw = _build_stage_1_mock_gateway(calls)

    with SessionFactory() as session:
        _seed_completed_primary(session, "pat_03", "cyc_03", t0)
        plan_service = AthenaWeeklyPlanService(db=session, gateway=gw, now_provider=clock.now)

        await plan_service.create_or_get_weekly_plan("cyc_03", week_number=1)

        assert len(calls) == 1
        prompt = calls[0]["prompt"]
        # Known facts must be in the prompt
        assert "Не курит" in prompt
        assert "Аллергических реакций не отмечает" in prompt
        assert "Возраст: 42" in prompt
        # Explicit instruction not to repeat
        assert "Запрещено ставить целью выяснение того, что уже однозначно известно" in prompt
    engine.dispose()


@pytest.mark.anyio
async def test_04_weekly_plan_idempotency_and_no_duplicate_gemini_calls(tmp_path: Path):
    """
    Verifies Section 1 idempotency:
    - Calling create_or_get_weekly_plan multiple times reuses the persisted plan.
    - Exactly 1 Gemini call is made.
    """
    db_file = tmp_path / "test_s1_04.db"
    engine, SessionFactory = _create_file_db(db_file)
    t0 = datetime(2026, 11, 4, 9, 0, 0, tzinfo=timezone.utc)
    clock = ControlledClock(t0)
    calls: List[Dict[str, Any]] = []
    plan_mock = _sample_weekly_plan("cyc_04", num_goals=4)
    gw = _build_stage_1_mock_gateway(calls, plan_to_return=plan_mock)

    with SessionFactory() as session:
        _seed_completed_primary(session, "pat_04", "cyc_04", t0)
        plan_service = AthenaWeeklyPlanService(db=session, gateway=gw, now_provider=clock.now)

        p1 = await plan_service.create_or_get_weekly_plan("cyc_04", week_number=1)
        assert len(calls) == 1

        # Second call
        p2 = await plan_service.create_or_get_weekly_plan("cyc_04", week_number=1)
        assert len(calls) == 1  # 0 additional Gemini calls!
        assert p1.plan_id == p2.plan_id
        assert len(p2.goals) == 4
    engine.dispose()


@pytest.mark.anyio
async def test_05_initial_greeting_at_t0_persona_and_zero_clinical_questions(tmp_path: Path):
    """
    Verifies Section 2 initial greeting contract:
    - Warm Russian greeting introducing Athena (AI companion in Aegis referencing shield/care).
    - Completely excludes titles: 'врач', 'медицинский куратор', 'клинический консультант', 'главный методолог'.
    - STRICTLY FORBIDDEN to ask clinical questions in greeting.
    - Hidden [CLINICAL_REPORT] status: 'INITIALIZATION', adherence_to_plan: 'UNKNOWN'.
    - Does NOT register an unanswered routine question.
    """
    db_file = tmp_path / "test_s1_05.db"
    engine, SessionFactory = _create_file_db(db_file)
    t0 = datetime(2026, 11, 4, 9, 0, 0, tzinfo=timezone.utc)
    clock = ControlledClock(t0)
    calls: List[Dict[str, Any]] = []
    gw = _build_stage_1_mock_gateway(calls)

    with SessionFactory() as session:
        _seed_completed_primary(session, "pat_05", "cyc_05", t0)
        athena_service = AthenaChatService(db=session, gateway=gw, now_provider=clock.now)

        resp = await athena_service.handle_turn(
            patient_id="pat_05",
            cycle_id="cyc_05",
            request=AthenaChatRequest(
                message="Start accompaniment",
                event_type="initial_greeting",
                idempotency_key="init_cyc_05",
            ),
        )

        # Check persona and reply content
        assert "Афина" in resp.reply
        assert "персональный AI-ассистент" in resp.reply
        # Excluded titles check
        for forbidden in ["врач", "медицинский куратор", "клинический консультант", "главный методолог"]:
            assert forbidden not in resp.reply.lower()

        # No question asked in greeting
        assert resp.routine_question_status is None
        assert resp.question_id is None
        assert athena_service.has_unanswered_routine_question("cyc_05") is False

        # Hidden clinical report check
        w_service = WeeklyConsiliumService(db=session, now_provider=clock.now)
        logs = w_service.get_seven_day_athena_logs("cyc_05")
        assert len(logs) == 1
        log = logs[0]
        assert log.status == "INITIALIZATION"
        assert log.adherence_to_plan == "UNKNOWN"
        assert log.status not in ("NORMAL", "HIGH")
    engine.dispose()


@pytest.mark.anyio
async def test_06_first_planned_question_at_t0_plus_30s_linked_to_goal_1(tmp_path: Path):
    """
    Verifies Section 3 first planned question contract:
    - Sent at T0 + 30 seconds (event_type='first_planned_question').
    - Formulated in simple non-medical language linked to Goal 1 of weekly plan.
    - Registered as unanswered routine question in ATHENA_ROUTINE_QUESTION_REPORT_TYPE.
    - Goal questions counter is incremented.
    """
    db_file = tmp_path / "test_s1_06.db"
    engine, SessionFactory = _create_file_db(db_file)
    t0 = datetime(2026, 11, 4, 9, 0, 0, tzinfo=timezone.utc)
    clock = ControlledClock(t0)
    calls: List[Dict[str, Any]] = []
    plan_mock = _sample_weekly_plan("cyc_06", num_goals=4)
    gw = _build_stage_1_mock_gateway(calls, plan_to_return=plan_mock)

    with SessionFactory() as session:
        _seed_completed_primary(session, "pat_06", "cyc_06", t0)
        plan_service = AthenaWeeklyPlanService(db=session, gateway=gw, now_provider=clock.now)
        await plan_service.create_or_get_weekly_plan("cyc_06", week_number=1)

        athena_service = AthenaChatService(db=session, gateway=gw, now_provider=clock.now)

        # T0 greeting
        await athena_service.handle_turn(
            patient_id="pat_06",
            cycle_id="cyc_06",
            request=AthenaChatRequest(
                message="Start accompaniment",
                event_type="initial_greeting",
                idempotency_key="init_cyc_06",
            ),
        )

        # Advance 30 seconds
        clock.advance(seconds=30)

        # T0 + 30s: First planned question
        q1_resp = await athena_service.handle_turn(
            patient_id="pat_06",
            cycle_id="cyc_06",
            request=AthenaChatRequest(
                message="First question",
                event_type="first_planned_question",
                idempotency_key="q1_cyc_06",
            ),
        )

        # Verifications
        assert q1_resp.routine_question_status == "unanswered"
        assert q1_resp.question_id is not None
        assert athena_service.has_unanswered_routine_question("cyc_06") is True

        # Goal question count incremented
        updated_plan = plan_service.get_active_weekly_plan("cyc_06", week_number=1)
        assert updated_plan is not None
        g1 = updated_plan.goals[0]
        assert g1.goal_id == "goal_1"
        assert g1.questions_asked_count == 1

        # Routine question registered in model_reports
        active_q = athena_service.get_active_routine_question("cyc_06")
        assert active_q is not None
        assert active_q["status"] == "unanswered"
        assert active_q["question_id"] == q1_resp.question_id
        assert active_q["reminders_count"] == 0
    engine.dispose()


@pytest.mark.anyio
async def test_07_clinical_automation_end_to_end_post_primary_with_weekly_plan(tmp_path: Path):
    """
    Verifies Section 4 Automation pipeline integration:
    - post_primary_init job generates Athena weekly plan, runs initial greeting,
      and enqueues first_planned_question job at T0 + 30s.
    - Advancing clock to T0 + 30s executes first_planned_question via worker.
    """
    db_file = tmp_path / "test_s1_07.db"
    engine, SessionFactory = _create_file_db(db_file)
    t0 = datetime(2026, 11, 4, 9, 0, 0, tzinfo=timezone.utc)
    clock = ControlledClock(t0)
    calls: List[Dict[str, Any]] = []
    plan_mock = _sample_weekly_plan("cyc_07", num_goals=4)
    gw = _build_stage_1_mock_gateway(calls, plan_to_return=plan_mock)

    with SessionFactory() as session:
        _seed_completed_primary(session, "pat_07", "cyc_07", t0)

        auto_service = ClinicalAutomationService(
            db=session,
            gateway=gw,
            now_provider=clock.now,
            enable_weekly_plan=True,
        )

        # Discover & enqueue post_primary_init
        init_jobs = auto_service.discover_and_enqueue_jobs()
        assert len(init_jobs) >= 1
        post_primary_job = next(j for j in init_jobs if j.job_type == AutomationJobType.POST_PRIMARY_INIT)

        # Process post_primary_init at T0
        processed_t0 = await auto_service.process_due_jobs()
        init_done = next(j for j in processed_t0 if j.job_type == AutomationJobType.POST_PRIMARY_INIT)
        assert init_done.status == AutomationJobStatus.COMPLETED

        # Verify weekly plan was created
        plan_service = AthenaWeeklyPlanService(db=session, gateway=gw, now_provider=clock.now)
        plan = plan_service.get_active_weekly_plan("cyc_07", week_number=1)
        assert plan is not None
        assert len(plan.goals) == 4

        # Verify anchor T0 was established
        weekly_service = WeeklyConsiliumService(db=session, now_provider=clock.now)
        anchor_dt = weekly_service.find_schedule_anchor_timestamp("cyc_07")
        assert anchor_dt is not None

        # At T0: first_planned_question job is enqueued but NOT yet due (scheduled for T0 + 30s)
        assert auto_service.athena_service.has_unanswered_routine_question("cyc_07") is False

        # Advance clock to T0 + 30s
        clock.advance(seconds=30)

        # Process due jobs at T0 + 30s
        processed_t30 = await auto_service.process_due_jobs()
        q1_jobs = [j for j in processed_t30 if j.job_type == AutomationJobType.FIRST_PLANNED_QUESTION]
        assert len(q1_jobs) == 1
        assert q1_jobs[0].status == AutomationJobStatus.COMPLETED

        # Verify routine question is now active and awaiting patient response
        assert auto_service.athena_service.has_unanswered_routine_question("cyc_07") is True
        active_q = auto_service.athena_service.get_active_routine_question("cyc_07")
        assert active_q is not None
        assert active_q["status"] == "unanswered"
    engine.dispose()


@pytest.mark.anyio
async def test_08_server_only_isolation_no_leakage_to_patient(tmp_path: Path):
    """
    Verifies Section 5 Security and Privacy:
    - Weekly plan is stored with SERVER_ONLY privacy level.
    - Client chat history endpoint strips hidden [CLINICAL_REPORT] and never leaks plan.
    """
    db_file = tmp_path / "test_s1_08.db"
    engine, SessionFactory = _create_file_db(db_file)
    t0 = datetime(2026, 11, 4, 9, 0, 0, tzinfo=timezone.utc)
    clock = ControlledClock(t0)
    calls: List[Dict[str, Any]] = []
    plan_mock = _sample_weekly_plan("cyc_08", num_goals=4)
    gw = _build_stage_1_mock_gateway(calls, plan_to_return=plan_mock)

    with SessionFactory() as session:
        _seed_completed_primary(session, "pat_08", "cyc_08", t0)
        plan_service = AthenaWeeklyPlanService(db=session, gateway=gw, now_provider=clock.now)
        await plan_service.create_or_get_weekly_plan("cyc_08", week_number=1)

        athena_service = AthenaChatService(db=session, gateway=gw, now_provider=clock.now)
        await athena_service.handle_turn(
            patient_id="pat_08",
            cycle_id="cyc_08",
            request=AthenaChatRequest(
                message="Start accompaniment",
                event_type="initial_greeting",
                idempotency_key="init_cyc_08",
            ),
        )

        # Inspect model_reports directly: verify privacy_level is SERVER_ONLY
        rep_repo = ModelReportRepository(session)
        reports = rep_repo.list_server_only(cycle_id="cyc_08", report_type=ATHENA_WEEKLY_PLAN_REPORT_TYPE)
        assert len(reports) == 1
        assert reports[0].privacy_level == PrivacyLevel.SERVER_ONLY.value

        # Inspect chat history: hidden [CLINICAL_REPORT] is sanitized and never stored in curator message text
        w_service = WeeklyConsiliumService(db=session, now_provider=clock.now)
        history = w_service.get_athena_chat_history("cyc_08")
        for msg in history:
            assert "[CLINICAL_REPORT]" not in msg.get("text", "")
            assert "[/CLINICAL_REPORT]" not in msg.get("text", "")
            assert "AthenaWeeklyPlanRecord" not in msg.get("text", "")
    engine.dispose()


@pytest.mark.anyio
async def test_09_pydantic_validation_error_prevents_corrupt_plan_persistence(tmp_path: Path):
    """
    Verifies Section 1 robustness:
    - If Gemini Flash returns a response that fails Pydantic schema validation,
      the service rejects it, does NOT persist a corrupt report to database, and raises GeminiValidationError.
    """
    db_file = tmp_path / "test_s1_09.db"
    engine, SessionFactory = _create_file_db(db_file)
    t0 = datetime(2026, 11, 4, 9, 0, 0, tzinfo=timezone.utc)
    clock = ControlledClock(t0)

    class CorruptGateway(GeminiGateway):
        async def generate_structured(self, prompt: str, response_schema: Any, **kwargs) -> Any:
            raise GeminiValidationError("Gemini structured validation failed: missing required field 'goals'")

    corrupt_gw = CorruptGateway()

    with SessionFactory() as session:
        _seed_completed_primary(session, "pat_09", "cyc_09", t0)
        plan_service = AthenaWeeklyPlanService(db=session, gateway=corrupt_gw, now_provider=clock.now)

        with pytest.raises(GeminiValidationError):
            await plan_service.create_or_get_weekly_plan("cyc_09", week_number=1)

        # Database must not contain any corrupt weekly plan
        rep_repo = ModelReportRepository(session)
        reports = rep_repo.list_server_only(cycle_id="cyc_09", report_type=ATHENA_WEEKLY_PLAN_REPORT_TYPE)
        assert len(reports) == 0
    engine.dispose()


@pytest.mark.anyio
async def test_10_goal_status_progression_and_active_goal_selection(tmp_path: Path):
    """
    Verifies goal lifecycle and prioritization:
    - Initially Goal 1 (priority 1) is selected.
    - When Goal 1 is marked SATISFIED, get_current_active_goal automatically advances to Goal 2.
    """
    db_file = tmp_path / "test_s1_10.db"
    engine, SessionFactory = _create_file_db(db_file)
    t0 = datetime(2026, 11, 4, 9, 0, 0, tzinfo=timezone.utc)
    clock = ControlledClock(t0)
    calls: List[Dict[str, Any]] = []
    plan_mock = _sample_weekly_plan("cyc_10", num_goals=3)
    gw = _build_stage_1_mock_gateway(calls, plan_to_return=plan_mock)

    with SessionFactory() as session:
        _seed_completed_primary(session, "pat_10", "cyc_10", t0)
        plan_service = AthenaWeeklyPlanService(db=session, gateway=gw, now_provider=clock.now)
        await plan_service.create_or_get_weekly_plan("cyc_10", week_number=1)

        # Initially active goal is Goal 1
        active_1 = plan_service.get_current_active_goal("cyc_10")
        assert active_1 is not None
        assert active_1.goal_id == "goal_1"

        # Mark Goal 1 completed
        plan_service.update_goal_status("cyc_10", "goal_1", AthenaGoalStatus.COMPLETED)

        # Now active goal is Goal 2
        active_2 = plan_service.get_current_active_goal("cyc_10")
        assert active_2 is not None
        assert active_2.goal_id == "goal_2"
    engine.dispose()


@pytest.mark.anyio
async def test_11_config_flag_disabled_by_default(tmp_path: Path):
    """
    Verifies Section 1 configuration safety:
    - Default settings have enable_weekly_plan=False and enable_daily_journal=False.
    - Default ClinicalAutomationWorker does not generate weekly plan or schedule
      first_planned_question for existing flows without explicit authorization.
    """
    assert settings.enable_weekly_plan is False
    assert settings.enable_daily_journal is False

    db_file = tmp_path / "test_s1_11.db"
    engine, SessionFactory = _create_file_db(db_file)
    t0 = datetime(2026, 11, 4, 9, 0, 0, tzinfo=timezone.utc)
    clock = ControlledClock(t0)
    calls: List[Dict[str, Any]] = []
    gw = _build_stage_1_mock_gateway(calls)

    with SessionFactory() as session:
        _seed_completed_primary(session, "pat_11", "cyc_11", t0)

    # Worker using default parameters (enable_weekly_plan=False)
    worker = ClinicalAutomationWorker(
        session_factory=SessionFactory,
        gateway_factory=lambda: gw,
        now_provider=clock.now,
    )
    assert worker.enable_weekly_plan is False
    assert worker.enable_daily_journal is False

    processed = await worker.run_once(discover=True)
    assert len(processed) >= 1
    init_job = next(j for j in processed if j.job_type == AutomationJobType.POST_PRIMARY_INIT)
    assert init_job.status == AutomationJobStatus.COMPLETED

    with SessionFactory() as session:
        # Weekly plan report was NOT created
        rep_repo = ModelReportRepository(session)
        plans = rep_repo.list_server_only(cycle_id="cyc_11", report_type=ATHENA_WEEKLY_PLAN_REPORT_TYPE)
        assert len(plans) == 0

        # first_planned_question job was NOT enqueued
        auto_svc = ClinicalAutomationService(db=session, gateway=gw, now_provider=clock.now)
        jobs = auto_svc.list_jobs(cycle_id="cyc_11")
        q1_jobs = [j for j in jobs if j.job_type == AutomationJobType.FIRST_PLANNED_QUESTION]
        assert len(q1_jobs) == 0
    engine.dispose()


@pytest.mark.anyio
async def test_12_config_flag_enabled_triggers_stage_1_sequence(tmp_path: Path):
    """
    Verifies Section 1 sequence when enable_weekly_plan=True:
    1. Saving weekly plan;
    2. Greeting at T0 without a clinical question;
    3. Scheduling separate first question at T0 + 30 seconds.
    """
    db_file = tmp_path / "test_s1_12.db"
    engine, SessionFactory = _create_file_db(db_file)
    t0 = datetime(2026, 11, 4, 9, 0, 0, tzinfo=timezone.utc)
    clock = ControlledClock(t0)
    calls: List[Dict[str, Any]] = []
    plan_mock = _sample_weekly_plan("cyc_12", num_goals=4)
    gw = _build_stage_1_mock_gateway(calls, plan_to_return=plan_mock)

    with SessionFactory() as session:
        _seed_completed_primary(session, "pat_12", "cyc_12", t0)

    # Worker with enable_weekly_plan=True
    worker = ClinicalAutomationWorker(
        session_factory=SessionFactory,
        gateway_factory=lambda: gw,
        now_provider=clock.now,
        enable_weekly_plan=True,
    )

    # Tick 1 at T0
    processed_t0 = await worker.run_once(discover=True)
    init_job = next(j for j in processed_t0 if j.job_type == AutomationJobType.POST_PRIMARY_INIT)
    assert init_job.status == AutomationJobStatus.COMPLETED

    with SessionFactory() as session:
        # 1. Weekly plan saved
        plan_svc = AthenaWeeklyPlanService(db=session, gateway=gw, now_provider=clock.now)
        plan = plan_svc.get_active_weekly_plan("cyc_12", week_number=1)
        assert plan is not None
        assert len(plan.goals) == 4

        # 2. Greeting without question
        w_svc = WeeklyConsiliumService(db=session, now_provider=clock.now)
        history = w_svc.get_athena_chat_history("cyc_12")
        curator_msgs = [m for m in history if m.get("sender") == "curator"]
        assert len(curator_msgs) == 1
        assert "Афина" in curator_msgs[0]["text"]
        # Question not asked yet
        athena_svc = AthenaChatService(db=session, gateway=gw, now_provider=clock.now)
        assert athena_svc.has_unanswered_routine_question("cyc_12") is False

        # 3. First question scheduled for T0 + 30s
        auto_svc = ClinicalAutomationService(db=session, gateway=gw, now_provider=clock.now)
        jobs = auto_svc.list_jobs(cycle_id="cyc_12")
        q1_jobs = [j for j in jobs if j.job_type == AutomationJobType.FIRST_PLANNED_QUESTION]
        assert len(q1_jobs) == 1
        assert q1_jobs[0].status == AutomationJobStatus.PENDING
        expected_time = (t0 + timedelta(seconds=30)).isoformat()
        assert q1_jobs[0].next_attempt_at_utc == expected_time
    engine.dispose()


@pytest.mark.anyio
async def test_13_worker_restart_deduplication_and_no_redundant_gemini_calls(tmp_path: Path):
    """
    Verifies Section 4 restart and deduplication resilience:
    - Worker 1 processes post_primary_init and stops.
    - Worker 2 restarts at T0 + 10s: makes 0 Gemini calls, no duplicate greeting.
    - At T0 + 30s: Worker 2 executes first_planned_question.
    - Worker 3 restarts at T0 + 40s: makes 0 Gemini calls, no duplicate question.
    """
    db_file = tmp_path / "test_s1_13.db"
    engine, SessionFactory = _create_file_db(db_file)
    t0 = datetime(2026, 11, 4, 9, 0, 0, tzinfo=timezone.utc)
    clock = ControlledClock(t0)
    calls: List[Dict[str, Any]] = []
    plan_mock = _sample_weekly_plan("cyc_13", num_goals=3)
    gw = _build_stage_1_mock_gateway(calls, plan_to_return=plan_mock)

    with SessionFactory() as session:
        _seed_completed_primary(session, "pat_13", "cyc_13", t0)

    # 1. Worker 1 runs at T0
    worker_1 = ClinicalAutomationWorker(
        session_factory=SessionFactory,
        gateway_factory=lambda: gw,
        now_provider=clock.now,
        enable_weekly_plan=True,
    )
    await worker_1.run_once(discover=True)
    calls_after_w1 = len(calls)
    assert calls_after_w1 == 2  # 1 structured plan generation + 1 initial greeting

    # 2. Worker 2 starts after simulated process restart at T0 + 10s
    clock.advance(seconds=10)
    worker_2 = ClinicalAutomationWorker(
        session_factory=SessionFactory,
        gateway_factory=lambda: gw,
        now_provider=clock.now,
        enable_weekly_plan=True,
    )
    await worker_2.run_once(discover=True)
    # Zero additional Gemini calls made!
    assert len(calls) == calls_after_w1

    with SessionFactory() as session:
        w_svc = WeeklyConsiliumService(db=session, now_provider=clock.now)
        history = w_svc.get_athena_chat_history("cyc_13")
        curator_msgs = [m for m in history if m.get("sender") == "curator"]
        assert len(curator_msgs) == 1  # Exactly 1 greeting, no duplicate

    # 3. Advance to T0 + 30s: question becomes due
    clock.advance(seconds=20)  # Total T0 + 30s
    processed_q1 = await worker_2.run_once(discover=True)
    q1_jobs = [j for j in processed_q1 if j.job_type == AutomationJobType.FIRST_PLANNED_QUESTION]
    assert len(q1_jobs) == 1
    assert q1_jobs[0].status == AutomationJobStatus.COMPLETED
    calls_after_q1 = len(calls)
    assert calls_after_q1 == calls_after_w1 + 1  # Exactly 1 call for first question

    with SessionFactory() as session:
        w_svc = WeeklyConsiliumService(db=session, now_provider=clock.now)
        history = w_svc.get_athena_chat_history("cyc_13")
        curator_msgs = [m for m in history if m.get("sender") == "curator"]
        assert len(curator_msgs) == 2  # 1 greeting + 1 question

    # 4. Worker 3 starts at T0 + 40s (subsequent restart)
    clock.advance(seconds=10)  # T0 + 40s
    worker_3 = ClinicalAutomationWorker(
        session_factory=SessionFactory,
        gateway_factory=lambda: gw,
        now_provider=clock.now,
        enable_weekly_plan=True,
    )
    await worker_3.run_once(discover=True)
    # Zero redundant Gemini calls
    assert len(calls) == calls_after_q1

    with SessionFactory() as session:
        w_svc = WeeklyConsiliumService(db=session, now_provider=clock.now)
        history = w_svc.get_athena_chat_history("cyc_13")
        curator_msgs = [m for m in history if m.get("sender") == "curator"]
        assert len(curator_msgs) == 2  # Still strictly 2, no duplication
    engine.dispose()


@pytest.mark.anyio
async def test_14_initialization_status_integrity_across_services(tmp_path: Path):
    """
    Verifies Section 6 INITIALIZATION status integrity:
    - Persisted as status='INITIALIZATION' in athena_clinical_log;
    - Not coerced to 'NORMAL', 'ALERT', or 'CRITICAL';
    - Does not trigger red flags or emergency alerts;
    - Consilium summary formats it explicitly as 'Status: INITIALIZATION'.
    """
    db_file = tmp_path / "test_s1_14.db"
    engine, SessionFactory = _create_file_db(db_file)
    t0 = datetime(2026, 11, 4, 9, 0, 0, tzinfo=timezone.utc)
    clock = ControlledClock(t0)
    calls: List[Dict[str, Any]] = []
    gw = _build_stage_1_mock_gateway(calls)

    with SessionFactory() as session:
        _seed_completed_primary(session, "pat_14", "cyc_14", t0)
        athena_service = AthenaChatService(db=session, gateway=gw, now_provider=clock.now)

        resp = await athena_service.handle_turn(
            patient_id="pat_14",
            cycle_id="cyc_14",
            request=AthenaChatRequest(
                message="Start accompaniment",
                event_type="initial_greeting",
                idempotency_key="init_cyc_14",
            ),
        )

        assert resp.emergency_trigger is False
        assert resp.emergency_notice is None

        # Check raw report in database
        rep_repo = ModelReportRepository(session)
        log_reports = rep_repo.list_server_only(cycle_id="cyc_14", report_type=ATHENA_CLINICAL_LOG_REPORT_TYPE)
        assert len(log_reports) == 1
        log_content = log_reports[0].content
        assert log_content["status"] == "INITIALIZATION"
        assert log_content["adherence_to_plan"] == "UNKNOWN"
        assert log_content["red_flags"] is False
        assert log_content["status"] not in ("NORMAL", "ALERT", "CRITICAL")

        # Check Consilium diary formatting
        w_service = WeeklyConsiliumService(db=session, now_provider=clock.now)
        seven_day_logs = w_service.get_seven_day_athena_logs("cyc_14")
        assert len(seven_day_logs) == 1
        assert seven_day_logs[0].status == "INITIALIZATION"
        formatted_summary = w_service.format_athena_diary_for_consilium(seven_day_logs)
        assert "📊 Status: INITIALIZATION" in formatted_summary
        assert "📊 Status: NORMAL" not in formatted_summary
        assert "📊 Status: ALERT" not in formatted_summary
        assert "📊 Status: CRITICAL" not in formatted_summary
    engine.dispose()
