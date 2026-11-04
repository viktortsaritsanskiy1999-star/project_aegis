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
    AthenaGoalProgressionDecision,
    AthenaGoalStatus,
    AthenaPlanExecutionLogEntry,
    AthenaVitalsAlertContext,
    AthenaWeeklyPlanRecord,
)
from app.schemas.privacy import PrivacyLevel
from app.services.athena_chat_service import (
    ATHENA_CHAT_TURN_REPORT_TYPE,
    ATHENA_ROUTINE_QUESTION_REPORT_TYPE,
    AthenaChatService,
)
from app.services.athena_daily_journal_service import (
    ATHENA_DAILY_JOURNAL_REPORT_TYPE,
    AthenaDailyJournalService,
)
from app.services.athena_weekly_plan_service import (
    ATHENA_PLAN_EXECUTION_LOG_REPORT_TYPE,
    ATHENA_WEEKLY_PLAN_REPORT_TYPE,
    AthenaWeeklyPlanService,
)
from app.services.clinical_automation_service import (
    ClinicalAutomationService,
)
from app.services.gemini_gateway import GeminiGateway, GeminiModelRole
from app.services.weekly_consilium_service import (
    ATHENA_CLINICAL_LOG_REPORT_TYPE,
    ATHENA_SCHEDULE_ANCHOR_REPORT_TYPE,
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
            version=1,
            report_type="primary_user_overall_report",
            privacy_level=PrivacyLevel.CLIENT_FACING,
            content={"title": "Primary Consilium", "summary": "Baseline assessment completed."},
            created_at=now_dt,
        )
    )

    # Chief Physician internal shelf
    session.add(
        ModelReport(
            report_id=f"rep_{cycle_id}_chief_internal_v1",
            cycle_id=cycle_id,
            model="aegis-primary-pro",
            version=1,
            report_type="chief_physician_internal",
            privacy_level=PrivacyLevel.SERVER_ONLY,
            content={
                "pro_general_shelf": "Динамическое наблюдение за утренним артериальным давлением и реакцией на умеренную ходьбу."
            },
            created_at=now_dt,
        )
    )

    # Active strategy report
    session.add(
        ModelReport(
            report_id=f"rep_{cycle_id}_active_strat_v1",
            cycle_id=cycle_id,
            model="aegis-primary-pro",
            version=1,
            report_type="athena_active_strategy",
            privacy_level=PrivacyLevel.SERVER_ONLY,
            content={
                "active_strategy_text": "Отслеживать динамику утреннего пульса и давления, реакцию на физическую нагрузку.",
                "clinical_background_text": "Гемодинамический мониторинг адаптации сердечно-сосудистой системы.",
                "deputy_synthesis_text": "Синтез показателей: пульс в покое 60-75 bpm, АД до 130/85 мм рт. ст.",
            },
            created_at=now_dt,
        )
    )

    # Schedule anchor
    session.add(
        ModelReport(
            report_id=f"rep_{cycle_id}_anchor_v1",
            cycle_id=cycle_id,
            model="aegis-schedule-anchor",
            version=1,
            report_type=ATHENA_SCHEDULE_ANCHOR_REPORT_TYPE,
            privacy_level=PrivacyLevel.SERVER_ONLY,
            content={
                "anchor_timestamp_utc": anchor_dt.isoformat(),
                "source": "initial_greeting_turn",
            },
            created_at=now_dt,
        )
    )
    session.commit()


def _seed_weekly_plan(
    session: Session,
    cycle_id: str,
    goals: List[AthenaGoalItem],
    anchor_dt: datetime,
) -> AthenaWeeklyPlanRecord:
    plan = AthenaWeeklyPlanRecord(
        plan_id=f"plan_{cycle_id}_w1",
        cycle_id=cycle_id,
        week_number=1,
        source_strategy_report_id="rep_strat_test",
        source_strategy_version=1,
        created_at_utc=anchor_dt.isoformat(),
        summary_for_curator="Weekly observation plan for cardiovascular baseline",
        goals=goals,
        status="active",
    )
    session.add(
        ModelReport(
            report_id=f"rep_{cycle_id}_plan_v1",
            cycle_id=cycle_id,
            model="aegis-athena-weekly-plan",
            version=1,
            report_type=ATHENA_WEEKLY_PLAN_REPORT_TYPE,
            privacy_level=PrivacyLevel.SERVER_ONLY,
            content=plan.model_dump(),
            created_at=anchor_dt,
        )
    )
    session.commit()
    return plan


def _gemini_text_envelope(text: str) -> Dict[str, Any]:
    return {
        "candidates": [
            {
                "content": {
                    "parts": [{"text": text}]
                }
            }
        ]
    }


def _make_gateway(handler) -> GeminiGateway:
    return GeminiGateway(
        api_key="test-stage-2-key",
        transport=httpx.MockTransport(handler),
        max_retries=1,
        sleep_func=lambda _: None,
    )


def _format_gemini_raw_reply(
    reply_text: str,
    *,
    status: str = "NORMAL",
    symptoms: Optional[List[str]] = None,
    summary_for_master: str = "Athena clinical report",
    progression: Optional[Dict[str, Any]] = None,
    emergency_trigger: bool = False,
) -> str:
    parts = [reply_text]

    clin_rep = {
        "status": status,
        "symptoms_mentioned": symptoms or [],
        "subjective_wellbeing_score": 8,
        "adherence_to_plan": "HIGH",
        "red_flags": False,
        "summary_for_master": summary_for_master,
    }
    parts.append(f"\n[CLINICAL_REPORT]\n{json.dumps(clin_rep, ensure_ascii=False, indent=2)}\n[/CLINICAL_REPORT]")

    if progression is not None:
        parts.append(f"\n[GOAL_PROGRESSION]\n{json.dumps(progression, ensure_ascii=False, indent=2)}\n[/GOAL_PROGRESSION]")

    if emergency_trigger:
        parts.append("\n[EMERGENCY_TRIGGER]")

    return "\n".join(parts)


@pytest.fixture
def test_setup(tmp_path: Path):
    db_path = tmp_path / "test_athena_stage_2.db"
    engine, SessionFactory = _create_file_db(db_path)
    session = SessionFactory()
    anchor_dt = datetime(2026, 11, 4, 8, 0, 0, tzinfo=timezone.utc)
    clock = ControlledClock(anchor_dt)

    patient_id = "patient_stage_2_001"
    cycle_id = "cycle_stage_2_001"
    _seed_completed_primary(session, patient_id, cycle_id, anchor_dt)

    goals = [
        AthenaGoalItem(
            goal_id="goal_1",
            title="Динамика утреннего давления и пульса",
            clinical_rationale="Оценить базовые гемодинамические показатели в покое",
            priority=1,
            known_facts=["Гипертония в анамнезе не зафиксирована"],
            target_clarification="Уточнить значения утреннего АД и ЧСС в покое",
            sufficiency_criteria="Указаны конкретные числовые значения АД (систолическое/диастолическое) и пульс",
            status=AthenaGoalStatus.ACTIVE,
            questions_asked_count=0,
        ),
        AthenaGoalItem(
            goal_id="goal_2",
            title="Реакция на физическую активность",
            clinical_rationale="Исключить скрытую одышку и перегрузку миокарда при быстрой ходьбе",
            priority=2,
            known_facts=[],
            target_clarification="Уточнить переносимость подъема по лестнице или быстрой ходьбы",
            sufficiency_criteria="Пациент описал реакцию на нагрузку и наличие/отсутствие одышки",
            status=AthenaGoalStatus.ACTIVE,
            questions_asked_count=0,
        ),
        AthenaGoalItem(
            goal_id="goal_3",
            title="Режим гидратации и вечерней усталости",
            clinical_rationale="Контроль достаточного водно-солевого баланса",
            priority=3,
            known_facts=[],
            target_clarification="Уточнить объем выпиваемой чистой воды и уровень вечерней усталости",
            sufficiency_criteria="Указан приблизительный объем воды и субъективная усталость",
            status=AthenaGoalStatus.ACTIVE,
            questions_asked_count=0,
        ),
    ]
    _seed_weekly_plan(session, cycle_id, goals, anchor_dt)

    yield {
        "session": session,
        "patient_id": patient_id,
        "cycle_id": cycle_id,
        "anchor_dt": anchor_dt,
        "clock": clock,
        "goals": goals,
    }

    session.close()


# ==============================================================================
# TESTS
# ==============================================================================

@pytest.mark.anyio
async def test_sufficient_answer_completes_goal(test_setup):
    """
    Test 1: When patient provides a clinically sufficient answer meeting sufficiency_criteria:
    - Gemini Flash evaluates is_sufficient=True, decision="completed".
    - Active goal status transitions to AthenaGoalStatus.COMPLETED.
    - Previous question is marked answered.
    - Technical execution log AthenaPlanExecutionLogEntry is recorded under SERVER_ONLY.
    - Client reply text has [GOAL_PROGRESSION] and [CLINICAL_REPORT] completely stripped.
    """
    s = test_setup["session"]
    cid = test_setup["cycle_id"]
    pid = test_setup["patient_id"]
    clock = test_setup["clock"]

    # 1. Ask first planned question
    recorded_calls = []

    def mock_handler(request: httpx.Request) -> httpx.Response:
        recorded_calls.append(request)
        reply = (
            "Доброе утро! Подскажите, пожалуйста, удалось ли сегодня измерить утреннее давление и пульс?"
        )
        raw = _format_gemini_raw_reply(reply, summary_for_master="First question asked")
        return httpx.Response(200, json=_gemini_text_envelope(raw))

    gw = _make_gateway(mock_handler)
    chat_svc = AthenaChatService(db=s, gateway=gw, now_provider=clock.now)

    resp1 = await chat_svc.handle_turn(
        patient_id=pid,
        cycle_id=cid,
        request=AthenaChatRequest(event_type="first_planned_question", message="Ask"),
    )
    assert resp1.question_id is not None
    assert resp1.routine_question_status == "unanswered"

    plan_svc = AthenaWeeklyPlanService(db=s, gateway=gw, now_provider=clock.now)
    cur_g = plan_svc.get_current_active_goal(cid)
    assert cur_g.goal_id == "goal_1"
    assert cur_g.questions_asked_count == 1

    # 2. Patient provides complete answer satisfying sufficiency criteria
    clock.advance(minutes=15)

    def mock_answer_handler(request: httpx.Request) -> httpx.Response:
        reply = (
            "Спасибо за подробные данные! Показатели 120/80 и пульс 68 в покое — отличный ориентир. "
            "Я зафиксировала их. Хорошего вам дня!"
        )
        prog = {
            "goal_id": "goal_1",
            "is_sufficient": True,
            "decision": "completed",
            "reasoning": "Пациент предоставил конкретные цифры АД 120/80 и ЧСС 68 в покое, критерий закрыт.",
            "unresolved_points": [],
            "next_action": "conclude_day",
            "clarifying_question": None,
        }
        raw = _format_gemini_raw_reply(reply, progression=prog)
        return httpx.Response(200, json=_gemini_text_envelope(raw))

    chat_svc_2 = AthenaChatService(
        db=s, gateway=_make_gateway(mock_answer_handler), now_provider=clock.now
    )
    resp2 = await chat_svc_2.handle_turn(
        patient_id=pid,
        cycle_id=cid,
        request=AthenaChatRequest(
            event_type="routine_answer",
            message="Да, измерил сидя в покое: давление 120 на 80, пульс 68 ударов.",
        ),
    )

    # Invariants verification
    assert "[GOAL_PROGRESSION]" not in resp2.reply
    assert "[CLINICAL_REPORT]" not in resp2.reply
    assert "Спасибо за подробные данные!" in resp2.reply

    # Question is answered
    assert resp2.routine_question_status == "answered"
    assert chat_svc_2.get_active_routine_question(cid) is None

    # Goal status updated to COMPLETED
    updated_plan = plan_svc.get_active_weekly_plan(cid)
    g1 = next(g for g in updated_plan.goals if g.goal_id == "goal_1")
    assert g1.status == AthenaGoalStatus.COMPLETED

    # Technical execution log entry recorded
    exec_logs = plan_svc.list_plan_execution_logs(cid)
    assert len(exec_logs) == 1
    log = exec_logs[0]
    assert log.goal_id == "goal_1"
    assert log.goal_status == AthenaGoalStatus.COMPLETED
    assert log.next_action == "conclude_day"
    assert "120/80" in log.transition_reason


@pytest.mark.anyio
async def test_insufficient_answer_triggers_single_clarification(test_setup):
    """
    Test 2: When patient provides an incomplete or vague answer:
    - Gemini Flash evaluates is_sufficient=False, decision="clarify".
    - Athena formulates ONE clarifying question.
    - It is registered as 'unanswered' routine question linked to the SAME goal_id ("goal_1").
    - Goal status remains ACTIVE, questions_asked_count increments to 2.
    - Exactly ONE unanswered routine question exists at this time.
    """
    s = test_setup["session"]
    cid = test_setup["cycle_id"]
    pid = test_setup["patient_id"]
    clock = test_setup["clock"]

    # First question
    gw_q1 = _make_gateway(
        lambda _: httpx.Response(
            200,
            json=_gemini_text_envelope(_format_gemini_raw_reply("Как ваше давление?")),
        )
    )
    chat_svc = AthenaChatService(db=s, gateway=gw_q1, now_provider=clock.now)
    resp1 = await chat_svc.handle_turn(
        patient_id=pid,
        cycle_id=cid,
        request=AthenaChatRequest(event_type="first_planned_question", message="Ask"),
    )
    first_qid = resp1.question_id

    # Patient replies vaguely
    clock.advance(minutes=20)

    def mock_clarify_handler(request: httpx.Request) -> httpx.Response:
        reply = (
            "Поняла вас! А подскажите, есть ли возможность измерить тонометром прямо сейчас, "
            "или помните приблизительные цифры недавнего замера?"
        )
        prog = {
            "goal_id": "goal_1",
            "is_sufficient": False,
            "decision": "clarify",
            "reasoning": "Пациент не указал числовые значения АД и ЧСС, требуется уточнение.",
            "unresolved_points": ["Отсутствуют конкретные значения АД и ЧСС"],
            "next_action": "ask_clarification",
            "clarifying_question": reply,
        }
        raw = _format_gemini_raw_reply(reply, progression=prog)
        return httpx.Response(200, json=_gemini_text_envelope(raw))

    gw_clarify = _make_gateway(mock_clarify_handler)
    chat_svc_clarify = AthenaChatService(db=s, gateway=gw_clarify, now_provider=clock.now)

    resp2 = await chat_svc_clarify.handle_turn(
        patient_id=pid,
        cycle_id=cid,
        request=AthenaChatRequest(
            event_type="routine_answer",
            message="Да вроде нормальное, точно не мерил сегодня.",
        ),
    )

    # Invariants
    assert resp2.routine_question_status == "unanswered"
    assert resp2.question_id != first_qid  # New distinct question instance

    # Verify new routine question in DB has goal_id = goal_1
    active_q = chat_svc_clarify.get_active_routine_question(cid)
    assert active_q is not None
    assert active_q.get("goal_id") == "goal_1"
    assert active_q.get("question_id") == resp2.question_id

    # Goal remains active with questions_asked_count = 2
    plan_svc = AthenaWeeklyPlanService(db=s, gateway=gw_clarify, now_provider=clock.now)
    updated_plan = plan_svc.get_active_weekly_plan(cid)
    g1 = next(g for g in updated_plan.goals if g.goal_id == "goal_1")
    assert g1.status == AthenaGoalStatus.ACTIVE
    assert g1.questions_asked_count == 2

    # Technical execution log records clarification
    logs = plan_svc.list_plan_execution_logs(cid)
    assert len(logs) == 1
    assert logs[0].goal_id == "goal_1"
    assert logs[0].next_action == "ask_clarification"
    assert logs[0].goal_status == AthenaGoalStatus.ACTIVE


@pytest.mark.anyio
async def test_clarification_answered_completes_goal(test_setup):
    """
    Test 3: Patient answers the clarifying question with required data -> goal is marked COMPLETED.
    """
    s = test_setup["session"]
    cid = test_setup["cycle_id"]
    pid = test_setup["patient_id"]
    clock = test_setup["clock"]

    # 1. Ask Q1
    gw1 = _make_gateway(lambda _: httpx.Response(200, json=_gemini_text_envelope(_format_gemini_raw_reply("Вопрос 1"))))
    chat_svc = AthenaChatService(db=s, gateway=gw1, now_provider=clock.now)
    await chat_svc.handle_turn(
        patient_id=pid, cycle_id=cid, request=AthenaChatRequest(event_type="first_planned_question", message="Ask")
    )

    # 2. Vague answer -> clarification
    def clarify_handler(_):
        reply = "Подскажите точные цифры, пожалуйста."
        prog = {
            "goal_id": "goal_1",
            "is_sufficient": False,
            "decision": "clarify",
            "reasoning": "Нужны цифры",
            "next_action": "ask_clarification",
        }
        return httpx.Response(200, json=_gemini_text_envelope(_format_gemini_raw_reply(reply, progression=prog)))

    chat_svc_2 = AthenaChatService(db=s, gateway=_make_gateway(clarify_handler), now_provider=clock.now)
    await chat_svc_2.handle_turn(
        patient_id=pid, cycle_id=cid, request=AthenaChatRequest(event_type="routine_answer", message="Не помню точно")
    )

    # 3. Patient answers clarification with full data
    def complete_handler(_):
        reply = "Отлично, 125/82 и 70 — зафиксировала. Хорошего дня!"
        prog = {
            "goal_id": "goal_1",
            "is_sufficient": True,
            "decision": "completed",
            "reasoning": "Точные цифры получены",
            "next_action": "conclude_day",
        }
        return httpx.Response(200, json=_gemini_text_envelope(_format_gemini_raw_reply(reply, progression=prog)))

    chat_svc_3 = AthenaChatService(db=s, gateway=_make_gateway(complete_handler), now_provider=clock.now)
    resp3 = await chat_svc_3.handle_turn(
        patient_id=pid, cycle_id=cid, request=AthenaChatRequest(event_type="routine_answer", message="Измерил: 125/82, пульс 70.")
    )

    assert resp3.routine_question_status == "answered"
    assert chat_svc_3.get_active_routine_question(cid) is None

    plan_svc = AthenaWeeklyPlanService(db=s, gateway=gw1, now_provider=clock.now)
    plan = plan_svc.get_active_weekly_plan(cid)
    g1 = next(g for g in plan.goals if g.goal_id == "goal_1")
    assert g1.status == AthenaGoalStatus.COMPLETED

    logs = plan_svc.list_plan_execution_logs(cid)
    assert len(logs) == 2
    assert logs[0].next_action == "ask_clarification"
    assert logs[1].next_action == "conclude_day"
    assert logs[1].goal_status == AthenaGoalStatus.COMPLETED


@pytest.mark.anyio
async def test_goal_transition_to_next_active_goal(test_setup):
    """
    Test 4: When Goal 1 is completed and daily budget allows, Athena transitions to Goal 2:
    - Goal 1 is marked COMPLETED.
    - New unanswered routine question is created for Goal 2.
    - Exactly ONE unanswered question exists in the database.
    """
    s = test_setup["session"]
    cid = test_setup["cycle_id"]
    pid = test_setup["patient_id"]
    clock = test_setup["clock"]

    # 1. Ask Q1 for Goal 1
    gw1 = _make_gateway(lambda _: httpx.Response(200, json=_gemini_text_envelope(_format_gemini_raw_reply("Вопрос 1"))))
    chat_svc = AthenaChatService(db=s, gateway=gw1, now_provider=clock.now)
    await chat_svc.handle_turn(
        patient_id=pid, cycle_id=cid, request=AthenaChatRequest(event_type="first_planned_question", message="Ask")
    )

    # 2. Patient answers completely, Athena transitions to Goal 2 (ask_next_goal)
    def transition_handler(_):
        reply = "Давление 120/80 зафиксировала! А как вы обычно переносите подъем по лестнице или быструю ходьбу?"
        prog = {
            "goal_id": "goal_1",
            "is_sufficient": True,
            "decision": "completed",
            "reasoning": "АД и ЧСС полностью указаны",
            "next_action": "ask_next_goal",
        }
        return httpx.Response(200, json=_gemini_text_envelope(_format_gemini_raw_reply(reply, progression=prog)))

    chat_svc_2 = AthenaChatService(db=s, gateway=_make_gateway(transition_handler), now_provider=clock.now)
    resp2 = await chat_svc_2.handle_turn(
        patient_id=pid, cycle_id=cid, request=AthenaChatRequest(event_type="routine_answer", message="120/80, пульс 68")
    )

    assert resp2.routine_question_status == "unanswered"
    assert "как вы обычно переносите" in resp2.reply

    # Check database state
    plan_svc = AthenaWeeklyPlanService(db=s, gateway=gw1, now_provider=clock.now)
    plan = plan_svc.get_active_weekly_plan(cid)
    g1 = next(g for g in plan.goals if g.goal_id == "goal_1")
    g2 = next(g for g in plan.goals if g.goal_id == "goal_2")

    assert g1.status == AthenaGoalStatus.COMPLETED
    assert g2.status == AthenaGoalStatus.ACTIVE
    assert g2.questions_asked_count == 1

    active_q = chat_svc_2.get_active_routine_question(cid)
    assert active_q is not None
    assert active_q.get("goal_id") == "goal_2"

    logs = plan_svc.list_plan_execution_logs(cid)
    assert len(logs) == 1
    assert logs[0].goal_id == "goal_1"
    assert logs[0].next_action == "ask_next_goal"
    assert logs[0].goal_status == AthenaGoalStatus.COMPLETED


@pytest.mark.anyio
async def test_daily_question_budget_max_3_questions(test_setup):
    """
    Test 5 & 6: Daily Question Budget: strictly max 3 planned questions/clarifications in 24h window.
    When 3 questions have been asked, a 4th clarification is strictly prevented by server logic
    and the goal is deferred to the next day.
    """
    s = test_setup["session"]
    cid = test_setup["cycle_id"]
    pid = test_setup["patient_id"]
    clock = test_setup["clock"]
    plan_svc = AthenaWeeklyPlanService(db=s, now_provider=clock.now)

    # 1. Question 1 (Goal 1)
    chat_1 = AthenaChatService(
        db=s,
        gateway=_make_gateway(lambda _: httpx.Response(200, json=_gemini_text_envelope(_format_gemini_raw_reply("Вопрос 1")))),
        now_provider=clock.now,
    )
    await chat_1.handle_turn(patient_id=pid, cycle_id=cid, request=AthenaChatRequest(event_type="first_planned_question", message="Ask"))
    assert plan_svc.get_daily_planned_questions_count(cid) == 1

    # 2. Answer Q1 -> Transition to Goal 2 (Question 2)
    def trans_handler(_):
        return httpx.Response(200, json=_gemini_text_envelope(_format_gemini_raw_reply(
            "Вопрос 2 (Goal 2)", progression={"goal_id": "goal_1", "is_sufficient": True, "decision": "completed", "next_action": "ask_next_goal"}
        )))
    chat_2 = AthenaChatService(db=s, gateway=_make_gateway(trans_handler), now_provider=clock.now)
    await chat_2.handle_turn(patient_id=pid, cycle_id=cid, request=AthenaChatRequest(event_type="routine_answer", message="Ответ 1"))
    assert plan_svc.get_daily_planned_questions_count(cid) == 2

    # 3. Answer Q2 vaguely -> Ask Clarification on Goal 2 (Question 3)
    def clarify_handler(_):
        return httpx.Response(200, json=_gemini_text_envelope(_format_gemini_raw_reply(
            "Вопрос 3: Уточнение по Goal 2", progression={"goal_id": "goal_2", "is_sufficient": False, "decision": "clarify", "next_action": "ask_clarification"}
        )))
    chat_3 = AthenaChatService(db=s, gateway=_make_gateway(clarify_handler), now_provider=clock.now)
    await chat_3.handle_turn(patient_id=pid, cycle_id=cid, request=AthenaChatRequest(event_type="routine_answer", message="Ответ 2"))
    assert plan_svc.get_daily_planned_questions_count(cid) == 3

    # 4. Now budget = 0 remaining. Patient answers vaguely again, model attempts decision='clarify'
    def fourth_clarify_attempt(_):
        return httpx.Response(200, json=_gemini_text_envelope(_format_gemini_raw_reply(
            "Попытка задать 4-й вопрос", progression={"goal_id": "goal_2", "is_sufficient": False, "decision": "clarify", "next_action": "ask_clarification"}
        )))
    chat_4 = AthenaChatService(db=s, gateway=_make_gateway(fourth_clarify_attempt), now_provider=clock.now)
    resp4 = await chat_4.handle_turn(patient_id=pid, cycle_id=cid, request=AthenaChatRequest(event_type="routine_answer", message="Ответ 3"))

    # Server INVARIANT: 4th question is blocked, goal is DEFERRED
    assert plan_svc.get_daily_planned_questions_count(cid) == 3
    assert chat_4.get_active_routine_question(cid) is None  # No pending question!

    plan = plan_svc.get_active_weekly_plan(cid)
    g2 = next(g for g in plan.goals if g.goal_id == "goal_2")
    assert g2.status == AthenaGoalStatus.DEFERRED

    logs = plan_svc.list_plan_execution_logs(cid)
    last_log = logs[-1]
    assert last_log.goal_id == "goal_2"
    assert last_log.goal_status == AthenaGoalStatus.DEFERRED
    assert "Daily planned question limit (3) reached" in last_log.transition_reason


@pytest.mark.anyio
async def test_goal_marked_unresolved_when_inappropriate_to_continue(test_setup):
    """
    Test 7: When user declines to answer or topic is inapplicable:
    - Gemini evaluates decision="unresolved".
    - Goal status is marked UNRESOLVED.
    - No further routine questions asked.
    """
    s = test_setup["session"]
    cid = test_setup["cycle_id"]
    pid = test_setup["patient_id"]
    clock = test_setup["clock"]

    chat_1 = AthenaChatService(
        db=s,
        gateway=_make_gateway(lambda _: httpx.Response(200, json=_gemini_text_envelope(_format_gemini_raw_reply("Вопрос 1")))),
        now_provider=clock.now,
    )
    await chat_1.handle_turn(patient_id=pid, cycle_id=cid, request=AthenaChatRequest(event_type="first_planned_question", message="Ask"))

    def opt_out_handler(_):
        reply = "Хорошо, без проблем, эту тему отложим. Желаю вам спокойного дня!"
        prog = {
            "goal_id": "goal_1",
            "is_sufficient": False,
            "decision": "unresolved",
            "reasoning": "Пользователь явно отказался обсуждать показатели давления.",
            "unresolved_points": ["Отказ от измерения АД"],
            "next_action": "conclude_day",
        }
        return httpx.Response(200, json=_gemini_text_envelope(_format_gemini_raw_reply(reply, progression=prog)))

    chat_2 = AthenaChatService(db=s, gateway=_make_gateway(opt_out_handler), now_provider=clock.now)
    resp = await chat_2.handle_turn(
        patient_id=pid,
        cycle_id=cid,
        request=AthenaChatRequest(event_type="routine_answer", message="Я не хочу измерять давление и говорить об этом."),
    )

    assert resp.routine_question_status == "answered"
    assert chat_2.get_active_routine_question(cid) is None

    plan_svc = AthenaWeeklyPlanService(db=s, now_provider=clock.now)
    plan = plan_svc.get_active_weekly_plan(cid)
    g1 = next(g for g in plan.goals if g.goal_id == "goal_1")
    assert g1.status == AthenaGoalStatus.UNRESOLVED

    logs = plan_svc.list_plan_execution_logs(cid)
    assert len(logs) == 1
    assert logs[0].goal_status == AthenaGoalStatus.UNRESOLVED


@pytest.mark.anyio
async def test_single_pending_question_invariant(test_setup):
    """
    Test 8: System invariant: at any point in time, there is strictly AT MOST ONE
    unanswered routine question for the cycle.
    """
    s = test_setup["session"]
    cid = test_setup["cycle_id"]
    pid = test_setup["patient_id"]
    clock = test_setup["clock"]

    def count_unanswered_in_db() -> int:
        rep_repo = ModelReportRepository(s)
        reports = rep_repo.list_server_only(cycle_id=cid, report_type=ATHENA_ROUTINE_QUESTION_REPORT_TYPE)
        if not reports:
            return 0
        latest_c = reports[0].content or {}
        return 1 if latest_c.get("status") == "unanswered" else 0

    assert count_unanswered_in_db() == 0

    # Ask Q1
    chat_1 = AthenaChatService(
        db=s,
        gateway=_make_gateway(lambda _: httpx.Response(200, json=_gemini_text_envelope(_format_gemini_raw_reply("Вопрос 1")))),
        now_provider=clock.now,
    )
    await chat_1.handle_turn(patient_id=pid, cycle_id=cid, request=AthenaChatRequest(event_type="first_planned_question", message="Ask"))
    assert count_unanswered_in_db() == 1

    # Clarification
    def clarify_handler(_):
        return httpx.Response(200, json=_gemini_text_envelope(_format_gemini_raw_reply(
            "Уточняющий вопрос", progression={"goal_id": "goal_1", "is_sufficient": False, "decision": "clarify", "next_action": "ask_clarification"}
        )))
    chat_2 = AthenaChatService(db=s, gateway=_make_gateway(clarify_handler), now_provider=clock.now)
    await chat_2.handle_turn(patient_id=pid, cycle_id=cid, request=AthenaChatRequest(event_type="routine_answer", message="Ответ 1"))
    assert count_unanswered_in_db() == 1

    # Answer clarification
    def complete_handler(_):
        return httpx.Response(200, json=_gemini_text_envelope(_format_gemini_raw_reply(
            "Завершено", progression={"goal_id": "goal_1", "is_sufficient": True, "decision": "completed", "next_action": "conclude_day"}
        )))
    chat_3 = AthenaChatService(db=s, gateway=_make_gateway(complete_handler), now_provider=clock.now)
    await chat_3.handle_turn(patient_id=pid, cycle_id=cid, request=AthenaChatRequest(event_type="routine_answer", message="Ответ 2"))
    assert count_unanswered_in_db() == 0


@pytest.mark.anyio
async def test_early_completion_avoids_filler_questions(test_setup):
    """
    Test 9: When all weekly goals are completed early:
    - daily_pulse prompt receives instruction forbidding filler questions.
    - Athena greets warmly without inventing questions.
    - Zero unanswered routine questions created.
    """
    s = test_setup["session"]
    cid = test_setup["cycle_id"]
    pid = test_setup["patient_id"]
    clock = test_setup["clock"]
    plan_svc = AthenaWeeklyPlanService(db=s, now_provider=clock.now)

    # Mark all goals completed
    for g in test_setup["goals"]:
        plan_svc.mark_goal_completed(cid, g.goal_id, notes="Early completed in previous days")
    assert plan_svc.are_all_goals_completed(cid) is True

    recorded_prompt = []

    def daily_pulse_handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        prompt = body["contents"][-1]["parts"][0]["text"]
        recorded_prompt.append(prompt)
        reply = (
            "Доброе утро! Все намеченные на эту неделю цели наблюдения мы с вами уже успешно закрыли. "
            "Я рядом и на связи, отличного вам дня!"
        )
        return httpx.Response(200, json=_gemini_text_envelope(_format_gemini_raw_reply(reply)))

    chat_svc = AthenaChatService(db=s, gateway=_make_gateway(daily_pulse_handler), now_provider=clock.now)
    resp = await chat_svc.handle_turn(
        patient_id=pid,
        cycle_id=cid,
        request=AthenaChatRequest(event_type="daily_pulse", sprint_day=4),
    )

    assert len(recorded_prompt) == 1
    assert "ALL WEEKLY GOALS COMPLETED" in recorded_prompt[0]
    assert "ЗАПРЕЩЕНО выдумывать новые медицинские опросники" in recorded_prompt[0]

    # No routine question formulated
    assert resp.question_id is None
    assert resp.routine_question_status is None
    assert chat_svc.get_active_routine_question(cid) is None


@pytest.mark.anyio
async def test_out_of_turn_message_does_not_consume_budget(test_setup):
    """
    Test 10: Patient out-of-turn inquiry:
    - Does NOT consume daily question budget.
    - Does NOT alter active goal status.
    - Leaves pending routine question intact.
    """
    s = test_setup["session"]
    cid = test_setup["cycle_id"]
    pid = test_setup["patient_id"]
    clock = test_setup["clock"]
    plan_svc = AthenaWeeklyPlanService(db=s, now_provider=clock.now)

    # 1. Ask Q1
    chat_1 = AthenaChatService(
        db=s,
        gateway=_make_gateway(lambda _: httpx.Response(200, json=_gemini_text_envelope(_format_gemini_raw_reply("Вопрос 1")))),
        now_provider=clock.now,
    )
    await chat_1.handle_turn(patient_id=pid, cycle_id=cid, request=AthenaChatRequest(event_type="first_planned_question", message="Ask"))
    assert plan_svc.get_daily_planned_questions_count(cid) == 1

    # 2. Patient sends out_of_turn_message
    def oot_handler(_):
        reply = "В приложении Aegis вы можете добавить замер через раздел Здоровье."
        return httpx.Response(200, json=_gemini_text_envelope(_format_gemini_raw_reply(reply)))

    chat_oot = AthenaChatService(db=s, gateway=_make_gateway(oot_handler), now_provider=clock.now)
    resp_oot = await chat_oot.handle_turn(
        patient_id=pid,
        cycle_id=cid,
        request=AthenaChatRequest(
            event_type="out_of_turn_message",
            message="Подскажите, как внести замер веса в приложение?",
        ),
    )

    # Invariants
    assert "В приложении Aegis" in resp_oot.reply
    # Planned questions count is still exactly 1!
    assert plan_svc.get_daily_planned_questions_count(cid) == 1
    # Pending question is still active and unanswered!
    active_q = chat_oot.get_active_routine_question(cid)
    assert active_q is not None
    assert active_q.get("status") == "unanswered"
    assert active_q.get("goal_id") == "goal_1"


@pytest.mark.anyio
async def test_mode_2_vitals_alert_unaffected_by_daily_budget(test_setup):
    """
    Test 11: Safety isolation: Mode 2 vitals alert is NEVER blocked by daily 3-question limit.
    """
    s = test_setup["session"]
    cid = test_setup["cycle_id"]
    pid = test_setup["patient_id"]
    clock = test_setup["clock"]
    plan_svc = AthenaWeeklyPlanService(db=s, now_provider=clock.now)

    # Manually seed 3 asked questions in the window
    for i in range(3):
        q_id = f"rq_test_budget_{i}"
        s.add(
            ModelReport(
                report_id=f"rep_{cid}_{q_id}",
                cycle_id=cid,
                model="aegis-test",
                version=i + 1,
                report_type=ATHENA_ROUTINE_QUESTION_REPORT_TYPE,
                privacy_level=PrivacyLevel.SERVER_ONLY,
                content={
                    "question_id": q_id,
                    "cycle_id": cid,
                    "status": "answered",
                    "asked_at_utc": clock.now().isoformat(),
                },
                created_at=clock.now(),
            )
        )
    s.commit()
    assert plan_svc.get_daily_planned_questions_count(cid) == 3
    assert plan_svc.get_remaining_daily_budget(cid) == 0

    # Vitals spike alert
    def va_handler(_):
        reply = "Внимание! Зафиксирован резкий скачок пульса до 160 уд/мин. Пожалуйста, сядьте и расстегните воротник."
        return httpx.Response(200, json=_gemini_text_envelope(_format_gemini_raw_reply(
            reply, status="CRITICAL", emergency_trigger=True
        )))

    chat_va = AthenaChatService(db=s, gateway=_make_gateway(va_handler), now_provider=clock.now)
    resp = await chat_va.handle_turn(
        patient_id=pid,
        cycle_id=cid,
        request=AthenaChatRequest(
            event_type="vitals_alert",
            vitals_alert=AthenaVitalsAlertContext(current_pulse=160, baseline_pulse=65, notes="Wearable spike"),
        ),
    )

    # Mode 2 processed immediately
    assert resp.emergency_trigger is True
    assert "Внимание!" in resp.reply


@pytest.mark.anyio
async def test_technical_execution_log_server_only_and_isolated(test_setup):
    """
    Test 12: Technical execution logs:
    - Persisted with PrivacyLevel.SERVER_ONLY.
    - Strictly FORBIDDEN to be exposed in Weekly Consilium or patient chat.
    - Verified that WeeklyConsiliumService and AthenaDailyJournalService never query or include it.
    """
    s = test_setup["session"]
    cid = test_setup["cycle_id"]
    clock = test_setup["clock"]
    plan_svc = AthenaWeeklyPlanService(db=s, now_provider=clock.now)

    log_entry = plan_svc.record_plan_execution_log(
        cycle_id=cid,
        goal_id="goal_1",
        goal_title="Динамика утреннего давления",
        goal_status=AthenaGoalStatus.COMPLETED,
        questions_asked_count=1,
        transition_reason="Критерий достаточности выполнен",
        unresolved_points=[],
        next_action="conclude_day",
        sprint_number=1,
        sprint_day=1,
    )
    assert log_entry.log_id.startswith("log_exec_")

    # Check database report record
    rep_repo = ModelReportRepository(s)
    reports = rep_repo.list_server_only(cycle_id=cid, report_type=ATHENA_PLAN_EXECUTION_LOG_REPORT_TYPE)
    assert len(reports) == 1
    assert reports[0].privacy_level == PrivacyLevel.SERVER_ONLY
    # Ensure not client facing
    client_reports = rep_repo.list_client_facing(cycle_id=cid)
    assert not any(r.report_type == ATHENA_PLAN_EXECUTION_LOG_REPORT_TYPE for r in client_reports)

    # Check isolation from WeeklyConsiliumService
    weekly_svc = WeeklyConsiliumService(db=s, now_provider=clock.now)
    logs = weekly_svc.get_seven_day_athena_logs(cid, sprint_id=None, reference_date=clock.now(), sprint_number=1)
    # The clinical logs should not contain the execution log
    assert not any(getattr(l, "log_id", None) == log_entry.log_id for l in logs)

    diary_str = weekly_svc.format_athena_diary_for_consilium(logs)
    assert "log_exec_" not in diary_str
    assert "athena_plan_execution_log" not in diary_str

    # Check isolation from AthenaDailyJournalService
    dj_svc = AthenaDailyJournalService(db=s, now_provider=clock.now)
    all_journals = dj_svc.list_daily_journals(cid)
    assert not any(getattr(j, "journal_id", None) == log_entry.log_id for j in all_journals)
    dj_diary = dj_svc.format_seven_day_journals_for_consilium(cid, sprint_number=1)
    if dj_diary:
        assert "log_exec_" not in dj_diary
        assert "athena_plan_execution_log" not in dj_diary


@pytest.mark.anyio
async def test_add_substantive_goal_requires_rationale(test_setup):
    """
    Test 13: add_substantive_goal:
    - Fails if prior goals are not all completed.
    - Fails if rationale is empty.
    - Succeeds when all prior goals are completed and rationale is provided.
    """
    s = test_setup["session"]
    cid = test_setup["cycle_id"]
    clock = test_setup["clock"]
    plan_svc = AthenaWeeklyPlanService(db=s, now_provider=clock.now)

    new_goal = AthenaGoalItem(
        goal_id="goal_substantive_4",
        title="Динамика отеков голеней",
        clinical_rationale="Симптом ХСН",
        priority=4,
        target_clarification="Наличие пастозности",
        sufficiency_criteria="Ответ о наличии отеков",
        status=AthenaGoalStatus.ACTIVE,
    )

    # 1. Cannot add while prior goals remain active
    with pytest.raises(ValueError, match="prior active/deferred goals remain"):
        plan_svc.add_substantive_goal(cid, new_goal, rationale="Клинически обосновано")

    # Mark existing goals completed
    for g in test_setup["goals"]:
        plan_svc.mark_goal_completed(cid, g.goal_id, notes="Completed")

    # 2. Cannot add with empty rationale
    with pytest.raises(ValueError, match="non-empty clinical rationale"):
        plan_svc.add_substantive_goal(cid, new_goal, rationale="")

    # 3. Succeeds with valid rationale
    added = plan_svc.add_substantive_goal(cid, new_goal, rationale="Новый кардиологический симптом")
    assert added.goal_id == "goal_substantive_4"

    plan = plan_svc.get_active_weekly_plan(cid)
    assert len(plan.goals) == 4
    assert plan.goals[-1].goal_id == "goal_substantive_4"


@pytest.mark.anyio
async def test_user_message_answering_routine_question_progresses_goal(test_setup):
    """
    Test 14: When user replies with event_type="user_message" while a routine question
    is pending unanswered, goal progression evaluates identically to routine_answer.
    """
    s = test_setup["session"]
    cid = test_setup["cycle_id"]
    pid = test_setup["patient_id"]
    clock = test_setup["clock"]

    # Ask Q1
    chat_1 = AthenaChatService(
        db=s,
        gateway=_make_gateway(lambda _: httpx.Response(200, json=_gemini_text_envelope(_format_gemini_raw_reply("Вопрос 1")))),
        now_provider=clock.now,
    )
    await chat_1.handle_turn(patient_id=pid, cycle_id=cid, request=AthenaChatRequest(event_type="first_planned_question", message="Ask"))

    # User answers using default event_type="user_message"
    def user_msg_handler(_):
        reply = "Зафиксировала ваши данные 120/80! Хорошего дня."
        prog = {
            "goal_id": "goal_1",
            "is_sufficient": True,
            "decision": "completed",
            "reasoning": "Давление 120/80 предоставлено",
            "next_action": "conclude_day",
        }
        return httpx.Response(200, json=_gemini_text_envelope(_format_gemini_raw_reply(reply, progression=prog)))

    chat_2 = AthenaChatService(db=s, gateway=_make_gateway(user_msg_handler), now_provider=clock.now)
    resp = await chat_2.handle_turn(
        patient_id=pid,
        cycle_id=cid,
        request=AthenaChatRequest(
            event_type="user_message",
            message="120 на 80, пульс 65",
        ),
    )

    assert resp.routine_question_status == "answered"
    plan_svc = AthenaWeeklyPlanService(db=s, now_provider=clock.now)
    plan = plan_svc.get_active_weekly_plan(cid)
    g1 = next(g for g in plan.goals if g.goal_id == "goal_1")
    assert g1.status == AthenaGoalStatus.COMPLETED
