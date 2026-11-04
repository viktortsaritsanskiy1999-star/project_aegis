import asyncio
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from typing import Any, Callable, Dict, List
import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from app.config import settings
from app.database import Base, get_db
from app.main import app
from app.repositories.cycle_repository import MedicalCycleRepository
from app.repositories.patient_repository import PatientRepository
from app.repositories.report_repository import ModelReportRepository
from app.routers.client_cascade import (
    get_gemini_gateway as client_get_gemini_gateway,
    get_server_now_provider as client_get_server_now_provider,
)
from app.routers.client_chat import (
    get_gemini_gateway as chat_get_gemini_gateway,
    get_server_now_provider as chat_get_server_now_provider,
)
from app.schemas.chat import AthenaChatRequest, AthenaVitalsAlertContext
from app.security.tokens import create_access_token
from app.services.athena_chat_service import (
    ATHENA_CHAT_TURN_REPORT_TYPE,
    ATHENA_ROUTINE_QUESTION_REPORT_TYPE,
    AthenaChatService,
    DEFAULT_MAX_ROUTINE_REMINDERS,
)
from app.services.gemini_gateway import GeminiGateway
from app.services.primary_consilium_service import PrimaryConsiliumService
from app.services.weekly_consilium_service import (
    ATHENA_SCHEDULE_ANCHOR_REPORT_TYPE,
    WeeklyConsiliumService,
)
from tests.test_primary_consilium import (
    _build_consilium_mock_gateway,
    _gemini_envelope,
    _sample_anamnesis_output,
)
from tests.test_weekly_consilium import (
    _sample_weekly_flash_output,
    _sample_weekly_pro_output,
)

TEST_JWT_SECRET = "test_jwt_stage_a2_1_secret_1234567890!"


class ControlledClock:
    def __init__(self, start_dt: datetime):
        self.current = start_dt

    def now(self) -> datetime:
        return self.current

    def advance(self, **kwargs: Any) -> datetime:
        self.current = self.current + timedelta(**kwargs)
        return self.current


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


def _build_athena_gateway(recorded_calls: List[Dict[str, Any]]) -> GeminiGateway:
    state = {"calls": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        idx = state["calls"]
        state["calls"] += 1
        body = json.loads(request.content.decode("utf-8"))
        prompt_text = body["contents"][-1]["parts"][0]["text"]
        recorded_calls.append(
            {
                "index": idx,
                "url_path": request.url.path,
                "prompt": prompt_text,
            }
        )

        # Weekly Consilium Pro Chief
        if "you are the chief physician" in prompt_text.lower() or "pro" in request.url.path.lower():
            return httpx.Response(200, json=_gemini_envelope(_sample_weekly_pro_output()))

        # Weekly Consilium Flash Deputy
        if "you are the head of department" in prompt_text.lower():
            return httpx.Response(200, json=_gemini_envelope(_sample_weekly_flash_output()))

        if "первое знакомство" in prompt_text.lower() or "initialization" in prompt_text.lower() or "initial_greeting" in prompt_text.lower():
            reply = (
                "Здравствуйте! Меня зовут Афина. Я ваш персональный AI-ассистент в системе Aegis. "
                "Я буду рядом, чтобы помогать вам беречь здоровье.\n\n"
                "[CLINICAL_REPORT]\n"
                "{\n"
                '  "status": "INITIALIZATION",\n'
                '  "symptoms_mentioned": [],\n'
                '  "adherence_to_plan": "UNKNOWN",\n'
                '  "red_flags": false,\n'
                '  "summary_for_master": "Athena onboarding completed. Baseline initialization."\n'
                "}\n"
                "[/CLINICAL_REPORT]"
            )
            return httpx.Response(200, json=_gemini_text_envelope(reply))

        reply = (
            f"Здравствуйте! Я Афина. Вопрос шага {idx + 1}: Как вы оцениваете качество своего сна сегодня?\n\n"
            "[CLINICAL_REPORT]\n"
            "{\n"
            '  "status": "NORMAL",\n'
            '  "symptoms_mentioned": [],\n'
            '  "subjective_wellbeing_score": 8,\n'
            '  "adherence_to_plan": "HIGH",\n'
            '  "red_flags": false,\n'
            '  "summary_for_master": "Athena clinical check-in recorded."\n'
            "}\n"
            "[/CLINICAL_REPORT]"
        )
        return httpx.Response(200, json=_gemini_text_envelope(reply))

    return GeminiGateway(
        api_key="test-stage-a2-1-key",
        transport=httpx.MockTransport(handler),
        max_retries=1,
        sleep_func=lambda _: None,
    )


async def _seed_completed_primary(
    session: Session,
    clock: ControlledClock,
    *,
    patient_id: str,
    cycle_id: str,
) -> None:
    if PatientRepository(session).get(patient_id) is None:
        PatientRepository(session).create(patient_id=patient_id, status="active")
    cycle_repo = MedicalCycleRepository(session)
    cycle = cycle_repo.get(cycle_id)
    if cycle is None:
        cycle_repo.create(cycle_id=cycle_id, patient_id=patient_id, status="active")
    else:
        cycle.patient_id = patient_id
        session.commit()

    service = PrimaryConsiliumService(db=session)
    service.save_preparatory_anamnesis_result(
        cycle_id=cycle_id,
        anamnesis_output=_sample_anamnesis_output(),
    )
    service.save_preparatory_ocr_labs(
        cycle_id=cycle_id,
        shared_labs_table={
            "hemoglobin": "144 g/L",
            "ferritin": "42 ng/mL",
            "creatinine": "82 umol/L",
            "glucose": "5.1 mmol/L",
        },
        created_at=datetime(2026, 10, 18, 9, 0, tzinfo=timezone.utc),
    )
    from app.services.lab_revision_consilium_service import LabRevisionConsiliumService
    rev_service = LabRevisionConsiliumService(db=session)
    rev_service._ensure_baseline_ledger_seeded(cycle_id)
    service.save_preparatory_ecg_summary(
        cycle_id=cycle_id,
        ecg_summary="Sinus rhythm, HR 64 bpm, normal electrical axis, no ST-T shifts.",
    )
    session.commit()

    primary_calls: List[Dict[str, Any]] = []
    primary_gw = _build_consilium_mock_gateway(primary_calls)
    primary_service = PrimaryConsiliumService(
        db=session,
        gateway=primary_gw,
        now_provider=clock.now,
    )
    await primary_service.run_primary_consilium(
        cycle_id=cycle_id,
        report_date=clock.now().strftime("%Y-%m-%d"),
    )
    assert primary_service.is_primary_consilium_completed(cycle_id) is True


@pytest.fixture(autouse=True)
def _patch_jwt_secret(monkeypatch):
    monkeypatch.setattr(settings, "jwt_secret", TEST_JWT_SECRET)


@pytest.mark.anyio
async def test_01_first_question_awaits_patient_response(tmp_path: Path):
    """
    Scenario 1:
    - Post-Primary greeting turn executes (initial_greeting).
    - Unanswered routine question is recorded in model_reports (status='unanswered').
    - has_unanswered_routine_question is True.
    - Cascade status reports has_unanswered_routine_question=True.
    """
    db_file = tmp_path / "test_a2_1_s1.db"
    engine, SessionFactory = _create_file_db(db_file)
    clock = ControlledClock(datetime(2026, 11, 1, 9, 0, 0, tzinfo=timezone.utc))
    calls: List[Dict[str, Any]] = []
    gateway = _build_athena_gateway(calls)

    with SessionFactory() as session:
        await _seed_completed_primary(
            session, clock, patient_id="pat_01", cycle_id="cycle_01"
        )
        athena_service = AthenaChatService(
            db=session, gateway=gateway, now_provider=clock.now
        )

        assert athena_service.has_unanswered_routine_question("cycle_01") is False

        # Execute post-Primary initial greeting (greeting only at T0)
        greeting_resp = await athena_service.handle_turn(
            patient_id="pat_01",
            cycle_id="cycle_01",
            request=AthenaChatRequest(
                event_type="initial_greeting",
                idempotency_key="init_pat_01_cycle_01",
            ),
        )
        assert greeting_resp.routine_question_status is None
        assert athena_service.has_unanswered_routine_question("cycle_01") is False

        # First planned question at T0 + 30 seconds
        clock.advance(seconds=30)
        resp = await athena_service.handle_turn(
            patient_id="pat_01",
            cycle_id="cycle_01",
            request=AthenaChatRequest(
                event_type="first_planned_question",
                idempotency_key="q1_pat_01_cycle_01",
            ),
        )

        assert resp.routine_question_status == "unanswered"
        assert resp.question_id is not None
        assert athena_service.has_unanswered_routine_question("cycle_01") is True

        active_q = athena_service.get_active_routine_question("cycle_01")
        assert active_q is not None
        assert active_q["status"] == "unanswered"
        assert active_q["reminders_count"] == 0
        assert active_q["question_id"] == resp.question_id


@pytest.mark.anyio
async def test_02_unanswered_question_blocks_new_routine_questions_over_multiple_days(
    tmp_path: Path,
):
    """
    Scenario 2:
    - Unanswered question blocks new routine questions across multiple days.
    - daily_pulse returns HTTP 409 Conflict with zero Gemini calls.
    - No question backlog generated.
    """
    db_file = tmp_path / "test_a2_1_s2.db"
    engine, SessionFactory = _create_file_db(db_file)
    clock = ControlledClock(datetime(2026, 11, 1, 9, 0, 0, tzinfo=timezone.utc))
    calls: List[Dict[str, Any]] = []
    gateway = _build_athena_gateway(calls)

    with SessionFactory() as session:
        await _seed_completed_primary(
            session, clock, patient_id="pat_02", cycle_id="cycle_02"
        )
        athena_service = AthenaChatService(
            db=session, gateway=gateway, now_provider=clock.now
        )

        # Initial greeting and first planned question
        await athena_service.handle_turn(
            patient_id="pat_02",
            cycle_id="cycle_02",
            request=AthenaChatRequest(
                event_type="initial_greeting",
                idempotency_key="init_02",
            ),
        )
        clock.advance(seconds=30)
        await athena_service.handle_turn(
            patient_id="pat_02",
            cycle_id="cycle_02",
            request=AthenaChatRequest(
                event_type="first_planned_question",
                idempotency_key="q1_02",
            ),
        )
        initial_calls_count = len(calls)

        # Day 2: 24 hours later, patient has not replied
        clock.advance(hours=24)
        with pytest.raises(HTTPException) as exc_d2:
            await athena_service.handle_turn(
                patient_id="pat_02",
                cycle_id="cycle_02",
                request=AthenaChatRequest(
                    event_type="daily_pulse",
                    sprint_number=1,
                    sprint_day=2,
                    idempotency_key="dp_d2",
                ),
            )
        assert exc_d2.value.status_code == 409
        assert "unanswered" in str(exc_d2.value.detail).lower()

        # Day 3: 48 hours later, patient still has not replied
        clock.advance(hours=24)
        with pytest.raises(HTTPException) as exc_d3:
            await athena_service.handle_turn(
                patient_id="pat_02",
                cycle_id="cycle_02",
                request=AthenaChatRequest(
                    event_type="daily_pulse",
                    sprint_number=1,
                    sprint_day=3,
                    idempotency_key="dp_d3",
                ),
            )
        assert exc_d3.value.status_code == 409
        assert "unanswered" in str(exc_d3.value.detail).lower()

        # Zero additional Gemini calls were made!
        assert len(calls) == initial_calls_count

        # Exactly 1 routine question report exists in database (no backlog)
        reports = ModelReportRepository(session).list_server_only(
            cycle_id="cycle_02",
            report_type=ATHENA_ROUTINE_QUESTION_REPORT_TYPE,
        )
        assert len(reports) == 1
        assert reports[0].content["status"] == "unanswered"


@pytest.mark.anyio
async def test_03_bounded_reminders_without_gemini_calls(tmp_path: Path):
    """
    Scenario 3:
    - Bounded reminder with zero Gemini calls.
    - Reminder 1 succeeds, increments reminders_count to 1.
    - Reminder 2 succeeds, increments reminders_count to 2.
    - Reminder 3 rejected with HTTP 409 Conflict (max reminders reached).
    - Zero Gemini calls for all reminders.
    """
    db_file = tmp_path / "test_a2_1_s3.db"
    engine, SessionFactory = _create_file_db(db_file)
    clock = ControlledClock(datetime(2026, 11, 1, 9, 0, 0, tzinfo=timezone.utc))
    calls: List[Dict[str, Any]] = []
    gateway = _build_athena_gateway(calls)

    with SessionFactory() as session:
        await _seed_completed_primary(
            session, clock, patient_id="pat_03", cycle_id="cycle_03"
        )
        athena_service = AthenaChatService(
            db=session,
            gateway=gateway,
            now_provider=clock.now,
            reminder_cooldown_seconds=0,
        )

        # Initial greeting and first planned question
        await athena_service.handle_turn(
            patient_id="pat_03",
            cycle_id="cycle_03",
            request=AthenaChatRequest(
                event_type="initial_greeting",
                idempotency_key="init_03",
            ),
        )
        clock.advance(seconds=30)
        await athena_service.handle_turn(
            patient_id="pat_03",
            cycle_id="cycle_03",
            request=AthenaChatRequest(
                event_type="first_planned_question",
                idempotency_key="q1_03",
            ),
        )
        calls_after_init = len(calls)

        # Reminder 1
        rem1 = await athena_service.handle_turn(
            patient_id="pat_03",
            cycle_id="cycle_03",
            request=AthenaChatRequest(
                event_type="routine_reminder",
                idempotency_key="rem_01",
            ),
        )
        assert rem1.reminders_count == 1
        assert rem1.routine_question_status == "unanswered"
        assert "Напоминание от Афины" in rem1.reply
        assert len(calls) == calls_after_init  # 0 Gemini calls!

        # Reminder 2
        rem2 = await athena_service.handle_turn(
            patient_id="pat_03",
            cycle_id="cycle_03",
            request=AthenaChatRequest(
                event_type="routine_reminder",
                idempotency_key="rem_02",
            ),
        )
        assert rem2.reminders_count == 2
        assert rem2.routine_question_status == "unanswered"
        assert len(calls) == calls_after_init  # Still 0 Gemini calls!

        # Reminder 3: Cap reached (max 2)
        with pytest.raises(HTTPException) as exc_rem3:
            await athena_service.handle_turn(
                patient_id="pat_03",
                cycle_id="cycle_03",
                request=AthenaChatRequest(
                    event_type="routine_reminder",
                    idempotency_key="rem_03",
                ),
            )
        assert exc_rem3.value.status_code == 409
        assert "maximum routine question reminders" in str(exc_rem3.value.detail).lower()
        assert len(calls) == calls_after_init  # Zero Gemini calls!


@pytest.mark.anyio
async def test_04_patient_answer_permits_next_strategy_driven_question(tmp_path: Path):
    """
    Scenario 4:
    - Patient answers the question via routine_answer (or user_message).
    - Status transitions to answered.
    - Subsequent daily_pulse succeeds and generates the next strategy-driven question.
    """
    db_file = tmp_path / "test_a2_1_s4.db"
    engine, SessionFactory = _create_file_db(db_file)
    clock = ControlledClock(datetime(2026, 11, 1, 9, 0, 0, tzinfo=timezone.utc))
    calls: List[Dict[str, Any]] = []
    gateway = _build_athena_gateway(calls)

    with SessionFactory() as session:
        await _seed_completed_primary(
            session, clock, patient_id="pat_04", cycle_id="cycle_04"
        )
        athena_service = AthenaChatService(
            db=session, gateway=gateway, now_provider=clock.now
        )

        # Initial greeting and first planned question
        await athena_service.handle_turn(
            patient_id="pat_04",
            cycle_id="cycle_04",
            request=AthenaChatRequest(
                event_type="initial_greeting",
                idempotency_key="init_04",
            ),
        )
        clock.advance(seconds=30)
        await athena_service.handle_turn(
            patient_id="pat_04",
            cycle_id="cycle_04",
            request=AthenaChatRequest(
                event_type="first_planned_question",
                idempotency_key="q1_04",
            ),
        )
        assert athena_service.has_unanswered_routine_question("cycle_04") is True

        # Patient replies via routine_answer
        ans_resp = await athena_service.handle_turn(
            patient_id="pat_04",
            cycle_id="cycle_04",
            request=AthenaChatRequest(
                event_type="routine_answer",
                message="Спал отлично, около 8 часов, проснулся бодрым.",
                idempotency_key="ans_04_1",
            ),
        )
        assert ans_resp.routine_question_status == "answered"
        assert athena_service.has_unanswered_routine_question("cycle_04") is False

        # Advance to Day 2 and send daily_pulse
        clock.advance(hours=24)
        dp_resp = await athena_service.handle_turn(
            patient_id="pat_04",
            cycle_id="cycle_04",
            request=AthenaChatRequest(
                event_type="daily_pulse",
                sprint_number=1,
                sprint_day=2,
                idempotency_key="dp_04_d2",
            ),
        )
        assert dp_resp.routine_question_status == "unanswered"
        assert athena_service.has_unanswered_routine_question("cycle_04") is True
        active_q2 = athena_service.get_active_routine_question("cycle_04")
        assert active_q2 is not None
        assert active_q2["sprint_day"] == 2
        assert active_q2["question_id"] == dp_resp.question_id


@pytest.mark.anyio
async def test_05_exceptional_events_do_not_consume_routine_question(tmp_path: Path):
    """
    Scenario 5:
    - Spontaneous out_of_turn_message or vitals_alert / wearable_event
      does NOT answer or clear the pending routine question.
    - Routine question remains unanswered.
    - Vitals alert triggers Mode 2 / emergency notice safely.
    """
    db_file = tmp_path / "test_a2_1_s5.db"
    engine, SessionFactory = _create_file_db(db_file)
    clock = ControlledClock(datetime(2026, 11, 1, 9, 0, 0, tzinfo=timezone.utc))
    calls: List[Dict[str, Any]] = []
    gateway = _build_athena_gateway(calls)

    with SessionFactory() as session:
        await _seed_completed_primary(
            session, clock, patient_id="pat_05", cycle_id="cycle_05"
        )
        athena_service = AthenaChatService(
            db=session, gateway=gateway, now_provider=clock.now
        )

        # Initial greeting and first planned question
        await athena_service.handle_turn(
            patient_id="pat_05",
            cycle_id="cycle_05",
            request=AthenaChatRequest(
                event_type="initial_greeting",
                idempotency_key="init_05",
            ),
        )
        clock.advance(seconds=30)
        q1_resp = await athena_service.handle_turn(
            patient_id="pat_05",
            cycle_id="cycle_05",
            request=AthenaChatRequest(
                event_type="first_planned_question",
                idempotency_key="q1_05",
            ),
        )
        q1_id = q1_resp.question_id
        assert athena_service.has_unanswered_routine_question("cycle_05") is True

        # 1. Out-of-turn inquiry from patient
        oot_resp = await athena_service.handle_turn(
            patient_id="pat_05",
            cycle_id="cycle_05",
            request=AthenaChatRequest(
                event_type="out_of_turn_message",
                message="Можно ли мне выпить чашку кофе перед тренировкой?",
                idempotency_key="oot_05_1",
            ),
        )
        assert athena_service.has_unanswered_routine_question("cycle_05") is True
        active_q = athena_service.get_active_routine_question("cycle_05")
        assert active_q is not None
        assert active_q["question_id"] == q1_id
        assert active_q["status"] == "unanswered"

        # 2. Vitals alert: heart rate spike to 160 bpm
        va_resp = await athena_service.handle_turn(
            patient_id="pat_05",
            cycle_id="cycle_05",
            request=AthenaChatRequest(
                event_type="vitals_alert",
                vitals_alert=AthenaVitalsAlertContext(
                    current_pulse=160,
                    baseline_pulse=70,
                    notes="Sudden palpitations while sitting",
                ),
                idempotency_key="va_05_1",
            ),
        )
        assert va_resp.emergency_trigger is True
        assert "CRITICAL_VITALS_ALERT" in (va_resp.emergency_notice or "")
        # Routine question still NOT consumed
        assert athena_service.has_unanswered_routine_question("cycle_05") is True
        assert athena_service.get_active_routine_question("cycle_05")["question_id"] == q1_id

        # 3. Wearable event notification
        wear_resp = await athena_service.handle_turn(
            patient_id="pat_05",
            cycle_id="cycle_05",
            request=AthenaChatRequest(
                event_type="wearable_event",
                message="Smartwatch detected high stress level.",
                idempotency_key="wear_05_1",
            ),
        )
        assert athena_service.has_unanswered_routine_question("cycle_05") is True
        assert athena_service.get_active_routine_question("cycle_05")["question_id"] == q1_id

        # 4. daily_pulse is still blocked because question 1 was not answered
        clock.advance(hours=24)
        with pytest.raises(HTTPException) as exc_blocked:
            await athena_service.handle_turn(
                patient_id="pat_05",
                cycle_id="cycle_05",
                request=AthenaChatRequest(
                    event_type="daily_pulse",
                    sprint_number=1,
                    sprint_day=2,
                    idempotency_key="dp_05_d2",
                ),
            )
        assert exc_blocked.value.status_code == 409


@pytest.mark.anyio
async def test_06_duplicate_retry_and_idempotency_safety(tmp_path: Path):
    """
    Scenario 6:
    - Retrying routine_answer with same idempotency_key returns cached response.
    - Retrying routine_reminder with same idempotency_key returns cached reminder
      without incrementing reminders_count twice.
    """
    db_file = tmp_path / "test_a2_1_s6.db"
    engine, SessionFactory = _create_file_db(db_file)
    clock = ControlledClock(datetime(2026, 11, 1, 9, 0, 0, tzinfo=timezone.utc))
    calls: List[Dict[str, Any]] = []
    gateway = _build_athena_gateway(calls)

    with SessionFactory() as session:
        await _seed_completed_primary(
            session, clock, patient_id="pat_06", cycle_id="cycle_06"
        )
        athena_service = AthenaChatService(
            db=session,
            gateway=gateway,
            now_provider=clock.now,
            reminder_cooldown_seconds=0,
        )

        # Initial greeting and first planned question
        await athena_service.handle_turn(
            patient_id="pat_06",
            cycle_id="cycle_06",
            request=AthenaChatRequest(
                event_type="initial_greeting",
                idempotency_key="init_06",
            ),
        )
        clock.advance(seconds=30)
        await athena_service.handle_turn(
            patient_id="pat_06",
            cycle_id="cycle_06",
            request=AthenaChatRequest(
                event_type="first_planned_question",
                idempotency_key="q1_06",
            ),
        )

        # Reminder with key
        rem_a = await athena_service.handle_turn(
            patient_id="pat_06",
            cycle_id="cycle_06",
            request=AthenaChatRequest(
                event_type="routine_reminder",
                idempotency_key="rem_idem_key_1",
            ),
        )
        assert rem_a.reminders_count == 1

        # Re-send exact same reminder key
        rem_b = await athena_service.handle_turn(
            patient_id="pat_06",
            cycle_id="cycle_06",
            request=AthenaChatRequest(
                event_type="routine_reminder",
                idempotency_key="rem_idem_key_1",
            ),
        )
        assert rem_b.message_id == rem_a.message_id
        assert rem_b.reply == rem_a.reply

        # Active question reminders_count remains 1
        active_q = athena_service.get_active_routine_question("cycle_06")
        assert active_q["reminders_count"] == 1

        # Answer with key
        ans_a = await athena_service.handle_turn(
            patient_id="pat_06",
            cycle_id="cycle_06",
            request=AthenaChatRequest(
                event_type="routine_answer",
                message="Самочувствие в норме.",
                idempotency_key="ans_idem_key_1",
            ),
        )

        # Re-send exact same answer key
        ans_b = await athena_service.handle_turn(
            patient_id="pat_06",
            cycle_id="cycle_06",
            request=AthenaChatRequest(
                event_type="routine_answer",
                message="Самочувствие в норме.",
                idempotency_key="ans_idem_key_1",
            ),
        )
        assert ans_b.message_id == ans_a.message_id
        assert ans_b.reply == ans_a.reply


@pytest.mark.anyio
async def test_07_patient_and_cycle_isolation(tmp_path: Path):
    """
    Scenario 7:
    - Patient A has an unanswered question.
    - Patient B answered their question.
    - Patient A's unanswered question does not block Patient B.
    """
    db_file = tmp_path / "test_a2_1_s7.db"
    engine, SessionFactory = _create_file_db(db_file)
    clock = ControlledClock(datetime(2026, 11, 1, 9, 0, 0, tzinfo=timezone.utc))
    calls: List[Dict[str, Any]] = []
    gateway = _build_athena_gateway(calls)

    with SessionFactory() as session:
        await _seed_completed_primary(
            session, clock, patient_id="pat_A", cycle_id="cycle_A"
        )
        await _seed_completed_primary(
            session, clock, patient_id="pat_B", cycle_id="cycle_B"
        )
        athena_service = AthenaChatService(
            db=session, gateway=gateway, now_provider=clock.now
        )

        # Patient A gets initial question and DOES NOT answer
        await athena_service.handle_turn(
            patient_id="pat_A",
            cycle_id="cycle_A",
            request=AthenaChatRequest(
                event_type="initial_greeting",
                idempotency_key="init_A",
            ),
        )
        clock.advance(seconds=30)
        await athena_service.handle_turn(
            patient_id="pat_A",
            cycle_id="cycle_A",
            request=AthenaChatRequest(
                event_type="first_planned_question",
                idempotency_key="q1_A",
            ),
        )
        assert athena_service.has_unanswered_routine_question("cycle_A") is True

        # Patient B gets initial question and ANSWERS
        await athena_service.handle_turn(
            patient_id="pat_B",
            cycle_id="cycle_B",
            request=AthenaChatRequest(
                event_type="initial_greeting",
                idempotency_key="init_B",
            ),
        )
        clock.advance(seconds=30)
        await athena_service.handle_turn(
            patient_id="pat_B",
            cycle_id="cycle_B",
            request=AthenaChatRequest(
                event_type="first_planned_question",
                idempotency_key="q1_B",
            ),
        )
        await athena_service.handle_turn(
            patient_id="pat_B",
            cycle_id="cycle_B",
            request=AthenaChatRequest(
                event_type="routine_answer",
                message="У меня всё отлично.",
                idempotency_key="ans_B",
            ),
        )
        assert athena_service.has_unanswered_routine_question("cycle_B") is False

        # Advance 24 hours
        clock.advance(hours=24)

        # Patient A is blocked
        with pytest.raises(HTTPException) as exc_A:
            await athena_service.handle_turn(
                patient_id="pat_A",
                cycle_id="cycle_A",
                request=AthenaChatRequest(
                    event_type="daily_pulse",
                    sprint_number=1,
                    sprint_day=2,
                    idempotency_key="dp_A_d2",
                ),
            )
        assert exc_A.value.status_code == 409

        # Patient B can generate Day 2 question successfully!
        dp_B = await athena_service.handle_turn(
            patient_id="pat_B",
            cycle_id="cycle_B",
            request=AthenaChatRequest(
                event_type="daily_pulse",
                sprint_number=1,
                sprint_day=2,
                idempotency_key="dp_B_d2",
            ),
        )
        assert dp_B.routine_question_status == "unanswered"


@pytest.mark.anyio
async def test_08_weekly_schedule_unaffected_by_missing_replies(tmp_path: Path):
    """
    Scenario 8:
    - Patient never replies to routine questions.
    - 168 hours elapse from anchor T0.
    - Weekly sprint lifecycle state advances to Week 2 and marks can_start_next_sprint=True.
    - Weekly Consilium executes successfully despite 0 patient answers.
    """
    db_file = tmp_path / "test_a2_1_s8.db"
    engine, SessionFactory = _create_file_db(db_file)
    clock = ControlledClock(datetime(2026, 11, 1, 9, 0, 0, tzinfo=timezone.utc))
    calls: List[Dict[str, Any]] = []
    gateway = _build_athena_gateway(calls)

    with SessionFactory() as session:
        await _seed_completed_primary(
            session, clock, patient_id="pat_08", cycle_id="cycle_08"
        )
        athena_service = AthenaChatService(
            db=session, gateway=gateway, now_provider=clock.now
        )
        weekly_service = WeeklyConsiliumService(
            db=session, gateway=gateway, now_provider=clock.now
        )

        # Initial greeting establishes anchor T0
        await athena_service.handle_turn(
            patient_id="pat_08",
            cycle_id="cycle_08",
            request=AthenaChatRequest(
                event_type="initial_greeting",
                idempotency_key="init_08",
            ),
        )
        clock.advance(seconds=30)
        await athena_service.handle_turn(
            patient_id="pat_08",
            cycle_id="cycle_08",
            request=AthenaChatRequest(
                event_type="first_planned_question",
                idempotency_key="q1_08",
            ),
        )
        assert athena_service.has_unanswered_routine_question("cycle_08") is True

        # Before 168 hours: Sprint 1 is ongoing, cannot start next sprint
        state_early = weekly_service.get_weekly_sprint_lifecycle_state("cycle_08")
        assert state_early.can_start_next_sprint is False
        assert state_early.weekly_sprint_number == 1

        # Advance exactly 168 hours (7 days) without patient ever answering
        clock.advance(hours=168)

        # Weekly state is eligible
        state_due = weekly_service.get_weekly_sprint_lifecycle_state("cycle_08")
        assert state_due.can_start_next_sprint is True
        assert state_due.current_calendar_week == 2

        # Weekly Consilium execution succeeds!
        weekly_resp = await weekly_service.run_weekly_consilium(
            cycle_id="cycle_08",
            sprint_id="sprint_08_w1",
            idempotency_key="weekly_consilium_08_w1",
        )
        assert weekly_resp is not None
        assert weekly_resp.sprint_id == "sprint_08_w1"
        state_after = weekly_service.get_weekly_sprint_lifecycle_state("cycle_08")
        assert state_after.is_weekly_consilium_completed is True


@pytest.mark.anyio
async def test_09_cascade_status_endpoint_reports_routine_question_state(tmp_path: Path):
    """
    Scenario 9:
    - FastAPI endpoint GET /api/v1/client/cascade/status reports
      has_unanswered_routine_question, active_routine_question_id,
      routine_question_reminders_count, and can_send_routine_reminder.
    """
    db_file = tmp_path / "test_a2_1_s9.db"
    engine, SessionFactory = _create_file_db(db_file)
    clock = ControlledClock(datetime(2026, 11, 1, 9, 0, 0, tzinfo=timezone.utc))
    calls: List[Dict[str, Any]] = []
    gateway = _build_athena_gateway(calls)

    with SessionFactory() as session:
        await _seed_completed_primary(
            session, clock, patient_id="pat_09", cycle_id="cycle_09"
        )
        athena_service = AthenaChatService(
            db=session, gateway=gateway, now_provider=clock.now
        )
        await athena_service.handle_turn(
            patient_id="pat_09",
            cycle_id="cycle_09",
            request=AthenaChatRequest(
                event_type="initial_greeting",
                idempotency_key="init_09",
            ),
        )
        clock.advance(seconds=30)
        await athena_service.handle_turn(
            patient_id="pat_09",
            cycle_id="cycle_09",
            request=AthenaChatRequest(
                event_type="first_planned_question",
                idempotency_key="q1_09",
            ),
        )

    def _override_db():
        with SessionFactory() as s:
            yield s

    from app.security.auth import DeviceSession, require_device_session, verify_client_token

    test_device_session = DeviceSession(
        patient_id="pat_09",
        device_id="dev_09",
        family_id="fam_09",
        jti="jti_09",
        scope="cycle:read chat:read chat:write",
        scopes=["cycle:read", "chat:read", "chat:write"],
        issued_at=datetime.now(timezone.utc),
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[client_get_gemini_gateway] = lambda: gateway
    app.dependency_overrides[client_get_server_now_provider] = lambda: clock.now
    app.dependency_overrides[chat_get_gemini_gateway] = lambda: gateway
    app.dependency_overrides[chat_get_server_now_provider] = lambda: clock.now
    app.dependency_overrides[require_device_session] = lambda: test_device_session
    app.dependency_overrides[verify_client_token] = lambda: test_device_session

    try:
        client = TestClient(app)
        token = create_access_token(
            patient_id="pat_09",
            device_id="dev_09",
            family_id="fam_09",
            scope="cycle:read chat:read chat:write",
        )
        headers = {"Authorization": f"Bearer {token}"}

        resp = client.get("/api/v1/client/cascade/status", headers=headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["has_unanswered_routine_question"] is True
        assert data["active_routine_question_id"] is not None
        assert data["routine_question_reminders_count"] == 0
        assert data["can_send_routine_reminder"] is True
    finally:
        app.dependency_overrides.clear()
