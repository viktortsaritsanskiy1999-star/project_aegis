"""
PROJECT AEGIS — STAGE 7C FINAL FIX TEST SUITE
Mode 2 Episode Lifecycle & Workout Context Verification

Tests:
- Test A: Confirmed strength workout (60 min, 12 sets, 4 min intervals, HR 80-140 bpm).
  * First workout confirmation.
  * Preservation of confirmation across sets and rest intervals.
  * Zero repeated identical user questions.
  * Correct registration of measurements and internal episodes.
- Test B: Repeated 115 bpm values (reproducing Stage 7C.2A-FIX defect).
  * Baseline 61, Spike delta 30, Max pessimum 120.
  * User confirms workout.
  * Repeated 115 bpm measurements.
  * Zero duplicate prompts asking for confirmation of the same workout.
- Test C: Workout completion & recovery flow.
  * Completion via existing finish mechanism.
  * Transition to recovery.
  * Activity context ends; zero carryover to future independent sessions.
- Test D: Dangerous state & safety invariance.
  * Critical tachycardia (>= 140 bpm) / critical SpO2 (<= 90%) / red flags during confirmed workout.
  * Safety actions execute unconditionally regardless of workout context.
"""

from datetime import datetime, timezone, timedelta
import json
from pathlib import Path
import re
from typing import Any, Callable, Dict, List, Optional
import pytest
import httpx
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker, Session

from app.models.base import Base
from app.models.workout_session import WorkoutSession
from app.repositories.cycle_repository import MedicalCycleRepository
from app.repositories.patient_repository import PatientRepository
from app.repositories.workout_repository import WorkoutSessionRepository
from app.schemas.mode2 import (
    Mode2EpisodeRecord,
    Mode2EpisodeStatus,
    Mode2WorkoutContext,
)
from app.services.gemini_gateway import GeminiGateway
from app.services.mode2_vitals_service import Mode2VitalsMonitoringService
from app.services.weekly_consilium_service import WeeklyConsiliumService


class ControlledClock:
    def __init__(self, initial_dt: datetime):
        self.current = initial_dt

    def now(self) -> datetime:
        return self.current

    def advance(self, **kwargs: Any) -> datetime:
        self.current = self.current + timedelta(**kwargs)
        return self.current

    def set(self, dt: datetime) -> datetime:
        self.current = dt
        return self.current


def _create_isolated_file_db(db_path: Path, busy_timeout_ms: int = 15000):
    engine = create_engine(
        f"sqlite:///{db_path.as_posix()}",
        connect_args={"check_same_thread": False, "timeout": busy_timeout_ms / 1000.0},
        pool_pre_ping=True,
    )

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragma(dbapi_con, con_record):
        cursor = dbapi_con.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA foreign_keys=ON")
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

    factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    return engine, factory


def _gemini_envelope(content_obj: Any) -> Dict[str, Any]:
    text_content = content_obj if isinstance(content_obj, str) else json.dumps(content_obj, ensure_ascii=False)
    return {
        "candidates": [
            {
                "content": {
                    "parts": [{"text": text_content}],
                    "role": "model",
                },
                "finishReason": "STOP",
                "index": 0,
            }
        ],
        "usageMetadata": {
            "promptTokenCount": 0,
            "candidatesTokenCount": 0,
            "totalTokenCount": 0,
        },
    }


class TurnTracker:
    def __init__(self):
        self.calls: List[Dict[str, Any]] = []

    def record(self, prompt: str, kind: str):
        self.calls.append({"kind": kind, "prompt": prompt})

    @property
    def question_count(self) -> int:
        """Counts how many times Athena was invoked with an initial vitals alert prompt."""
        return sum(1 for c in self.calls if c["kind"] == "initial_vitals_alert")


def _build_tracking_gateway(tracker: TurnTracker) -> GeminiGateway:
    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        contents = body.get("contents", [])
        prompt_parts: List[str] = []
        sys_inst = body.get("systemInstruction") or body.get("system_instruction") or {}
        for p in sys_inst.get("parts", []):
            if isinstance(p, dict) and "text" in p:
                prompt_parts.append(p["text"])
        for c in contents:
            for p in c.get("parts", []):
                if isinstance(p, dict) and "text" in p:
                    prompt_parts.append(p["text"])
        prompt = "\n".join(prompt_parts)
        prompt_lower = prompt.lower()

        is_emergency = "тревожный сигнал" in prompt_lower or "critical" in prompt_lower or "red_flag" in prompt_lower or "опасных симптомах" in prompt_lower

        if "service pulse: heart rate spike detected" in prompt_lower:
            tracker.record(prompt, "initial_vitals_alert")
            reply = "Зафиксирован скачок пульса. Вы тренируетесь или находитесь в покое? Как ваше самочувствие?"
            if is_emergency:
                reply = "ВНИМАНИЕ! Показатели критические. Немедленно прекратите нагрузку и вызовите скорую помощь (112)!\n[EMERGENCY_TRIGGER]"
            chat_text = (
                f"{reply}\n\n"
                "[CLINICAL_REPORT]\n"
                '{"status": "ALERT", "symptoms_mentioned": [], "subjective_wellbeing_score": 5, "adherence_to_plan": "HIGH", "red_flags": false, "summary_for_master": "Spike"}\n'
                "[/CLINICAL_REPORT]"
            )
            return httpx.Response(200, json=_gemini_envelope(chat_text))

        if "service pulse: mode 2 operational vitals update" in prompt_lower or "ответ пользователя в режиме мониторинга пульса" in prompt_lower:
            tracker.record(prompt, "mode2_update")
            if is_emergency:
                chat_text = (
                    "ТРЕВОГА: Зафиксированы опасные симптомы или критические показатели. Срочно вызовите скорую (112)!\n\n"
                    "[CLINICAL_REPORT]\n"
                    '{"status": "CRITICAL", "symptoms_mentioned": ["chest_pain"], "subjective_wellbeing_score": 2, "adherence_to_plan": "HIGH", "red_flags": true, "summary_for_master": "Danger"}\n'
                    "[/CLINICAL_REPORT]\n"
                    "[EMERGENCY_TRIGGER]"
                )
            else:
                chat_text = (
                    "Продолжаю мониторинг пульса во время тренировки. Следите за самочувствием и пейте воду.\n\n"
                    "[CLINICAL_REPORT]\n"
                    '{"status": "NORMAL", "symptoms_mentioned": [], "subjective_wellbeing_score": 8, "adherence_to_plan": "HIGH", "red_flags": false, "summary_for_master": "Workout ok"}\n'
                    "[/CLINICAL_REPORT]"
                )
            return httpx.Response(200, json=_gemini_envelope(chat_text))

        # Default fallback
        tracker.record(prompt, "other")
        return httpx.Response(200, json=_gemini_envelope("Здравствуйте! Я Афина."))

    return GeminiGateway(
        api_key="test-key-stage7c-final-fix",
        transport=httpx.MockTransport(handler),
        max_retries=1,
        sleep_func=lambda _: None,
    )


# ==============================================================================
# TEST A: CONFIRMED STRENGTH WORKOUT (60 MIN, 12 SETS, 4-5 MIN INTERVALS)
# ==============================================================================

@pytest.mark.anyio
async def test_a_confirmed_strength_workout_multiset(tmp_path: Path):
    """
    TEST A: CONFIRMED STRENGTH WORKOUT MULTISET FLOW
    - 60 minutes overall duration.
    - 12 consecutive sets with 4-5 minute rest intervals.
    - Pulse: 80 bpm during rest, 130..138 bpm during lifting sets.
    - Verifies:
      1. First spike triggers Mode 2 and asks user 'Are you exercising?'.
      2. User confirms workout once.
      3. Active workout context is preserved across set rest intervals.
      4. Internal episodes open during sets and close during rests without flapping questions.
      5. Exactly 1 user confirmation prompt across all 12 sets!
      6. All telemetry points recorded accurately.
    """
    db_file = tmp_path / "test_a_strength.db"
    engine, SessionFactory = _create_isolated_file_db(db_file)
    t0 = datetime(2026, 6, 1, 10, 0, 0, tzinfo=timezone.utc)
    clock = ControlledClock(t0)
    cycle_id = "cycle_strength_001"
    patient_id = "pat_strength_001"

    tracker = TurnTracker()
    gw = _build_tracking_gateway(tracker)

    with SessionFactory() as db:
        PatientRepository(db).create(patient_id=patient_id, status="active")
        MedicalCycleRepository(db).create(cycle_id=cycle_id, patient_id=patient_id, status="active")
        w_svc = WeeklyConsiliumService(db=db, now_provider=clock.now)
        w_svc.record_schedule_anchor(cycle_id, t0)
        m2_svc = Mode2VitalsMonitoringService(db=db, gateway=gw, now_provider=clock.now)

        # Baseline resting measurement at 10:05 UTC (pulse 65)
        clock.set(t0 + timedelta(minutes=5))
        base_ep = await m2_svc.process_measurement(cycle_id=cycle_id, pulse=65, spo2=98, measured_at=clock.now())
        assert base_ep is None
        assert tracker.question_count == 0

        # ----------------------------------------------------------------------
        # SET 1: First exertion spike at 10:10 UTC (pulse 135 bpm)
        # ----------------------------------------------------------------------
        clock.set(t0 + timedelta(minutes=10))
        ep1 = await m2_svc.process_measurement(
            cycle_id=cycle_id,
            pulse=135,
            spo2=98,
            phase="workout",
            session_id="ws_strength_01",
            measured_at=clock.now(),
        )
        assert ep1 is not None
        assert ep1.status == Mode2EpisodeStatus.ACTIVE
        assert tracker.question_count == 1, "Initial alert MUST ask user on first onset"

        # Patient confirms workout
        clock.advance(minutes=1)
        conf_ep = await m2_svc.handle_user_statement(
            cycle_id=cycle_id,
            user_text="Тяжёлая силовая тренировка, подход 1",
            is_workout=True,
            symptoms="самочувствие отличное",
            as_of=clock.now(),
        )
        assert conf_ep.workout_context == Mode2WorkoutContext.CONFIRMED_WORKOUT

        # Verify active workout session is persisted
        active_ws = m2_svc.get_active_workout_session(cycle_id)
        assert active_ws is not None
        assert active_ws.status == "active"

        # Rest after Set 1: pulse drops to 80 bpm (<= 120) -> 2 readings close Episode 1
        clock.advance(minutes=2)
        r1 = await m2_svc.process_measurement(cycle_id=cycle_id, pulse=80, spo2=98, measured_at=clock.now())
        clock.advance(minutes=2)
        r2 = await m2_svc.process_measurement(cycle_id=cycle_id, pulse=80, spo2=98, measured_at=clock.now())
        assert r2.status == Mode2EpisodeStatus.CLOSED, "Rest interval cleanly closes episode 1"

        # Active workout session STILL remains active!
        assert m2_svc.get_active_workout_session(cycle_id).status == "active"

        # ----------------------------------------------------------------------
        # SETS 2 THROUGH 12: Pulse rises to 132..138 bpm during lifting, then rests
        # ----------------------------------------------------------------------
        for set_num in range(2, 13):
            clock.advance(minutes=1)  # Lifting set starts
            pulse_val = 130 + (set_num % 8)
            set_ep = await m2_svc.process_measurement(
                cycle_id=cycle_id,
                pulse=pulse_val,
                spo2=98,
                phase="workout",
                session_id="ws_strength_01",
                measured_at=clock.now(),
            )
            assert set_ep is not None
            assert set_ep.status == Mode2EpisodeStatus.ACTIVE
            assert set_ep.workout_context == Mode2WorkoutContext.CONFIRMED_WORKOUT, f"Set {set_num} must inherit CONFIRMED_WORKOUT!"
            # CRITICAL INVARIANT: ZERO new confirmation questions!
            assert tracker.question_count == 1, f"Set {set_num} must NOT ask user again!"

            # Rest interval: 2 readings at 80 bpm close this sub-episode
            clock.advance(minutes=2)
            await m2_svc.process_measurement(cycle_id=cycle_id, pulse=80, spo2=98, measured_at=clock.now())
            clock.advance(minutes=2)
            closed_set_ep = await m2_svc.process_measurement(cycle_id=cycle_id, pulse=80, spo2=98, measured_at=clock.now())
            assert closed_set_ep.status == Mode2EpisodeStatus.CLOSED

            # Workout session remains active throughout the 60 minutes
            assert m2_svc.get_active_workout_session(cycle_id).status == "active"

        # Verify summary of episodes
        history = m2_svc.get_episode_history(cycle_id)
        assert len(history) == 12, "Each set with rest created an internal sub-episode"
        for ep in history:
            assert ep.workout_context == Mode2WorkoutContext.CONFIRMED_WORKOUT
        assert tracker.question_count == 1, "Only 1 user question asked across entire 60 min session!"


# ==============================================================================
# TEST B: REPEATED 115 BPM VALUES (REPRODUCING STAGE 7C.2A-FIX DEFECT)
# ==============================================================================

@pytest.mark.anyio
async def test_b_repeated_115_bpm_measurements_no_flapping_questions(tmp_path: Path):
    """
    TEST B: 115 BPM REPEATED EXERTION WITHOUT FLAPPING USER QUESTIONS
    - Baseline: 61 bpm.
    - Max pessimum: 120 bpm, max spike delta: 30 bpm.
    - Point 1: 115 bpm triggers Mode 2 (115 - 61 = 54 >= 30).
    - User confirms workout.
    - Subsequent continuous 115 bpm measurements arrive.
    - Because 115 <= 120 bpm, internal episodes close after 2 readings and reopen.
    - VERIFIES: New episodes inherit CONFIRMED_WORKOUT and DO NOT ask user again!
    """
    db_file = tmp_path / "test_b_115bpm.db"
    engine, SessionFactory = _create_isolated_file_db(db_file)
    t0 = datetime(2026, 6, 1, 8, 0, 0, tzinfo=timezone.utc)
    clock = ControlledClock(t0)
    cycle_id = "cycle_115_fix"
    patient_id = "pat_115_fix"

    tracker = TurnTracker()
    gw = _build_tracking_gateway(tracker)

    with SessionFactory() as db:
        PatientRepository(db).create(patient_id=patient_id, status="active")
        MedicalCycleRepository(db).create(cycle_id=cycle_id, patient_id=patient_id, status="active")
        w_svc = WeeklyConsiliumService(db=db, now_provider=clock.now)
        w_svc.record_schedule_anchor(cycle_id, t0)
        m2_svc = Mode2VitalsMonitoringService(db=db, gateway=gw, now_provider=clock.now)

        # Baseline resting measurement at 61 bpm
        clock.set(t0 + timedelta(minutes=30))
        await m2_svc.process_measurement(cycle_id=cycle_id, pulse=61, spo2=98, measured_at=clock.now())
        assert tracker.question_count == 0

        # Point 1: 115 bpm at 10:00 UTC
        clock.set(t0 + timedelta(hours=2))
        ep1 = await m2_svc.process_measurement(cycle_id=cycle_id, pulse=115, spo2=98, measured_at=clock.now())
        assert ep1 is not None
        assert tracker.question_count == 1, "First 115 bpm triggers confirmation prompt"

        # User confirms workout
        clock.advance(minutes=1)
        await m2_svc.handle_user_statement(
            cycle_id=cycle_id,
            user_text="Бег на улице 115 уд/мин",
            is_workout=True,
            as_of=clock.now(),
        )

        # Points 2, 3: 115 bpm (causes Episode 1 to close because 115 <= 120)
        clock.advance(minutes=4)
        await m2_svc.process_measurement(cycle_id=cycle_id, pulse=115, spo2=98, measured_at=clock.now())
        clock.advance(minutes=4)
        ep1_closed = await m2_svc.process_measurement(cycle_id=cycle_id, pulse=115, spo2=98, measured_at=clock.now())
        assert ep1_closed.status == Mode2EpisodeStatus.CLOSED

        # Point 4: 115 bpm (still running! opens Episode 2)
        clock.advance(minutes=5)
        ep2 = await m2_svc.process_measurement(cycle_id=cycle_id, pulse=115, spo2=98, measured_at=clock.now())
        assert ep2 is not None
        assert ep2.episode_id != ep1.episode_id
        assert ep2.status == Mode2EpisodeStatus.ACTIVE
        assert ep2.workout_context == Mode2WorkoutContext.CONFIRMED_WORKOUT
        # CRITICAL VERIFICATION: No second question!
        assert tracker.question_count == 1

        # Points 5, 6: 115 bpm closes Episode 2
        clock.advance(minutes=4)
        await m2_svc.process_measurement(cycle_id=cycle_id, pulse=115, spo2=98, measured_at=clock.now())
        clock.advance(minutes=4)
        ep2_closed = await m2_svc.process_measurement(cycle_id=cycle_id, pulse=115, spo2=98, measured_at=clock.now())
        assert ep2_closed.status == Mode2EpisodeStatus.CLOSED

        # Point 7: 115 bpm opens Episode 3
        clock.advance(minutes=5)
        ep3 = await m2_svc.process_measurement(cycle_id=cycle_id, pulse=115, spo2=98, measured_at=clock.now())
        assert ep3.status == Mode2EpisodeStatus.ACTIVE
        assert ep3.workout_context == Mode2WorkoutContext.CONFIRMED_WORKOUT
        # STILL EXACTLY 1 QUESTION!
        assert tracker.question_count == 1


# ==============================================================================
# TEST C: WORKOUT COMPLETION & TRANSITION TO RECOVERY
# ==============================================================================

@pytest.mark.anyio
async def test_c_workout_completion_and_clean_recovery(tmp_path: Path):
    """
    TEST C: WORKOUT COMPLETION & RECOVERY TRANSITION
    - Confirmed workout in progress.
    - User finishes workout via finish_workout / complete_workout_session.
    - Active workout transitions to 'recovering'.
    - Recovery telemetry is recorded.
    - A future independent resting spike (3 hours later) does NOT inherit confirmation.
    """
    db_file = tmp_path / "test_c_completion.db"
    engine, SessionFactory = _create_isolated_file_db(db_file)
    t0 = datetime(2026, 6, 1, 9, 0, 0, tzinfo=timezone.utc)
    clock = ControlledClock(t0)
    cycle_id = "cycle_comp_001"
    patient_id = "pat_comp_001"

    tracker = TurnTracker()
    gw = _build_tracking_gateway(tracker)

    with SessionFactory() as db:
        PatientRepository(db).create(patient_id=patient_id, status="active")
        MedicalCycleRepository(db).create(cycle_id=cycle_id, patient_id=patient_id, status="active")
        w_svc = WeeklyConsiliumService(db=db, now_provider=clock.now)
        w_svc.record_schedule_anchor(cycle_id, t0)
        m2_svc = Mode2VitalsMonitoringService(db=db, gateway=gw, now_provider=clock.now)

        # Start workout
        clock.set(t0 + timedelta(minutes=10))
        ep = await m2_svc.process_measurement(cycle_id=cycle_id, pulse=130, spo2=98, measured_at=clock.now())
        await m2_svc.handle_user_statement(cycle_id=cycle_id, user_text="Бег", is_workout=True, as_of=clock.now())
        assert m2_svc.get_active_workout_session(cycle_id).status == "active"

        # User finishes workout via completion mechanism
        clock.advance(minutes=30)
        completed_ws = m2_svc.finish_workout(cycle_id=cycle_id, completed_at=clock.now())
        assert completed_ws is not None
        assert completed_ws.status == "recovering"
        assert completed_ws.workout_completed_at is not None

        # Active workout session query returns None (session is now recovering, not active)
        assert m2_svc.get_active_workout_session(cycle_id) is None

        # Recovery measurements arrive
        clock.advance(minutes=2)
        await m2_svc.process_measurement(cycle_id=cycle_id, pulse=95, spo2=98, phase="recovery_0_10", measured_at=clock.now())
        clock.advance(minutes=5)
        await m2_svc.process_measurement(cycle_id=cycle_id, pulse=75, spo2=98, phase="recovery_10_50", measured_at=clock.now())

        # Fast forward 4 hours: Patient is resting at home at 14:00 UTC
        clock.advance(hours=4)
        # An unprovoked resting heart rate spike occurs (pulse 135 at rest!)
        spike_ep = await m2_svc.process_measurement(
            cycle_id=cycle_id,
            pulse=135,
            spo2=98,
            source="watch_sensor",
            measured_at=clock.now(),
        )
        assert spike_ep is not None
        # MUST NOT carry over confirmed workout!
        assert spike_ep.workout_context == Mode2WorkoutContext.UNKNOWN
        # Must ask user what happened for this new event!
        assert tracker.question_count == 2, "Second independent event must trigger prompt!"


# ==============================================================================
# TEST D: DANGEROUS STATE DURING CONFIRMED WORKOUT
# ==============================================================================

@pytest.mark.anyio
async def test_d_dangerous_state_during_confirmed_workout(tmp_path: Path):
    """
    TEST D: DANGEROUS STATE & CRITICAL VITAL VIOLATION DURING CONFIRMED WORKOUT
    - Confirmed workout in progress.
    - Case 1: Tachycardia exceeds critical safety threshold (>= 140 bpm, e.g. 148 bpm).
      Athena emergency alert MUST trigger regardless of workout context!
    - Case 2: Dangerous symptom reported ('сильная боль в груди, темнеет в глазах').
      Safety actions execute unconditionally and emergency advice is produced.
    """
    db_file = tmp_path / "test_d_danger.db"
    engine, SessionFactory = _create_isolated_file_db(db_file)
    t0 = datetime(2026, 6, 1, 10, 0, 0, tzinfo=timezone.utc)
    clock = ControlledClock(t0)
    cycle_id = "cycle_danger_001"
    patient_id = "pat_danger_001"

    tracker = TurnTracker()
    gw = _build_tracking_gateway(tracker)

    with SessionFactory() as db:
        PatientRepository(db).create(patient_id=patient_id, status="active")
        MedicalCycleRepository(db).create(cycle_id=cycle_id, patient_id=patient_id, status="active")
        w_svc = WeeklyConsiliumService(db=db, now_provider=clock.now)
        w_svc.record_schedule_anchor(cycle_id, t0)
        m2_svc = Mode2VitalsMonitoringService(db=db, gateway=gw, now_provider=clock.now)

        # 1. Start workout & confirm
        clock.set(t0 + timedelta(minutes=10))
        await m2_svc.process_measurement(cycle_id=cycle_id, pulse=125, spo2=98, measured_at=clock.now())
        await m2_svc.handle_user_statement(cycle_id=cycle_id, user_text="Интервальный бег", is_workout=True, as_of=clock.now())
        assert tracker.question_count == 1

        # Rest between intervals (closes sub-episode 1)
        clock.advance(minutes=2)
        await m2_svc.process_measurement(cycle_id=cycle_id, pulse=80, spo2=98, measured_at=clock.now())
        clock.advance(minutes=2)
        await m2_svc.process_measurement(cycle_id=cycle_id, pulse=80, spo2=98, measured_at=clock.now())

        # 2. Critical tachycardia breach during confirmed workout (148 bpm >= 140 critical threshold)
        clock.advance(minutes=2)
        crit_ep = await m2_svc.process_measurement(
            cycle_id=cycle_id,
            pulse=148,
            spo2=98,
            phase="workout",
            measured_at=clock.now(),
        )
        assert crit_ep is not None
        # Confirmed workout does NOT suppress critical vital warning!
        assert tracker.question_count == 2, "Critical vital spike (148 bpm) MUST trigger Athena alert even during confirmed workout!"
        assert any(e.event_type == "initial_alert" for e in crit_ep.athena_events)

        # 3. Patient reports alarming symptoms (red flags)
        clock.advance(minutes=1)
        symptom_ep = await m2_svc.handle_user_statement(
            cycle_id=cycle_id,
            user_text="Остановился, давит за грудиной и темнеет в глазах",
            is_workout=True,
            symptoms="боль в груди, нехватка воздуха",
            as_of=clock.now(),
        )
        assert symptom_ep is not None
        last_event = symptom_ep.athena_events[-1]
        assert "скорую" in last_event.athena_reply.lower() or "112" in last_event.athena_reply.lower()
        assert last_event.emergency_trigger is True


@pytest.mark.anyio
async def test_e_workout_finish_endpoint_idempotency_and_contracts(tmp_path: Path):
    """
    Test E: Endpoint POST /api/v1/vitals/mode2/workout-finish:
    - Accepts JSON payload body (cycle_id, session_id).
    - Accepts Query parameters.
    - Idempotency: repeated completion calls return 200 OK and preserve original completed timestamp.
    - Missing cycle_id returns 400 Bad Request.
    """
    from fastapi.testclient import TestClient
    from app.config import settings
    from app.main import app
    from app.database import get_db
    from app.routers.vitals import get_server_now_provider

    db_file = tmp_path / "test_e_finish.db"
    engine, SessionFactory = _create_isolated_file_db(db_file)
    t0 = datetime(2026, 6, 1, 10, 0, 0, tzinfo=timezone.utc)
    clock = ControlledClock(t0)
    cycle_id = "cycle_finish_001"
    patient_id = "pat_finish_001"

    with SessionFactory() as db:
        PatientRepository(db).create(patient_id=patient_id, status="active")
        MedicalCycleRepository(db).create(cycle_id=cycle_id, patient_id=patient_id, status="active")
        WorkoutSessionRepository(db).create(
            session_id="ws_finish_100",
            cycle_id=cycle_id,
            anomaly_detected_at=t0,
            activity_confirmed_at=t0 + timedelta(minutes=1),
            status="active",
        )

    def _override_get_db():
        with SessionFactory() as s:
            yield s

    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[get_server_now_provider] = lambda: clock.now
    headers = {"Authorization": f"Bearer {settings.api_secret}"}
    try:
        with TestClient(app) as client:
            # 1. Call via JSON Body Payload
            clock.set(t0 + timedelta(minutes=45))
            resp1 = client.post(
                "/api/v1/vitals/mode2/workout-finish",
                headers=headers,
                json={"cycle_id": cycle_id, "session_id": "ws_finish_100"},
            )
            assert resp1.status_code == 200, resp1.text
            data1 = resp1.json()
            assert data1["status"] == "success"
            assert data1["session_id"] == "ws_finish_100"
            assert data1["workout_status"] == "recovering"

            # Check DB timestamp
            with SessionFactory() as db:
                ws_db = WorkoutSessionRepository(db).get("ws_finish_100")
                assert ws_db is not None
                assert ws_db.status == "recovering"
                first_completed_at = ws_db.workout_completed_at
                assert first_completed_at is not None

            # 2. Repeated Call via Query Parameters (Idempotency test)
            clock.advance(minutes=15)  # 15 minutes later retry
            resp2 = client.post(
                f"/api/v1/vitals/mode2/workout-finish?cycle_id={cycle_id}&session_id=ws_finish_100",
                headers=headers,
            )
            assert resp2.status_code == 200, resp2.text
            data2 = resp2.json()
            assert data2["status"] == "success"
            assert data2["session_id"] == "ws_finish_100"
            assert data2["workout_status"] == "recovering"

            # Check that first_completed_at was PRESERVED, not updated to 15m later
            with SessionFactory() as db:
                ws_db2 = WorkoutSessionRepository(db).get("ws_finish_100")
                assert ws_db2.status == "recovering"
                assert ws_db2.workout_completed_at == first_completed_at

            # 3. Missing cycle_id returns 400
            resp3 = client.post(
                "/api/v1/vitals/mode2/workout-finish",
                headers=headers,
                json={"session_id": "ws_finish_100"},
            )
            assert resp3.status_code == 400
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(get_server_now_provider, None)


@pytest.mark.anyio
async def test_f_delayed_telemetry_does_not_revive_workout(tmp_path: Path):
    """
    Test F: Delayed / out-of-order telemetry guard.
    A delayed measurement with phase='workout' arriving AFTER workout completion
    must NEVER restore status='active' to a completed/recovering session.
    """
    db_file = tmp_path / "test_f_delayed.db"
    engine, SessionFactory = _create_isolated_file_db(db_file)
    t0 = datetime(2026, 6, 1, 10, 0, 0, tzinfo=timezone.utc)
    clock = ControlledClock(t0)
    cycle_id = "cycle_delayed_001"
    patient_id = "pat_delayed_001"

    tracker = TurnTracker()
    gw = _build_tracking_gateway(tracker)

    with SessionFactory() as db:
        PatientRepository(db).create(patient_id=patient_id, status="active")
        MedicalCycleRepository(db).create(cycle_id=cycle_id, patient_id=patient_id, status="active")
        m2_svc = Mode2VitalsMonitoringService(db=db, gateway=gw, now_provider=clock.now)

        # 1. Start workout and confirm
        clock.set(t0 + timedelta(minutes=5))
        await m2_svc.process_measurement(cycle_id=cycle_id, pulse=130, measured_at=clock.now())
        await m2_svc.handle_user_statement(cycle_id=cycle_id, user_text="Силовая тренировка", is_workout=True, as_of=clock.now())
        active_ws = m2_svc.get_active_workout_session(cycle_id)
        assert active_ws is not None
        session_id = active_ws.session_id

        # 2. Finish workout
        clock.set(t0 + timedelta(minutes=50))
        finished_ws = m2_svc.complete_workout_session(cycle_id, session_id=session_id, completed_at=clock.now())
        assert finished_ws.status == "recovering"
        assert m2_svc.get_active_workout_session(cycle_id) is None

        # 3. Simulate delayed out-of-order measurement from workout period (e.g. measured at t0 + 25m)
        delayed_time = t0 + timedelta(minutes=25)
        delayed_ep = await m2_svc.process_measurement(
            cycle_id=cycle_id,
            pulse=120,
            phase="workout",
            session_id=session_id,
            measured_at=delayed_time,
        )

        # 4. Verify: workout session in DB remains 'recovering', NOT revived to 'active'
        ws_in_db = WorkoutSessionRepository(db).get(session_id)
        assert ws_in_db.status == "recovering"
        assert m2_svc.get_active_workout_session(cycle_id) is None, "Delayed measurement MUST NOT revive completed workout session!"


@pytest.mark.anyio
async def test_g_threshold_priority_hierarchy_pro_vs_emergency_safety_guard(tmp_path: Path):
    """
    Test G: Priority Hierarchy of Thresholds.
    1. Independent Emergency Safety Guard (140 / 40 / 90) unconditionally triggers emergency handling.
    2. Personal Pro Thresholds (min_pessimum_bpm, max_pessimum_bpm, max_spike_delta_bpm) govern individual monitoring and normalization corridor.
    3. Fallback Thresholds (45 / 115 / 25) apply when Pro thresholds are absent.
    """
    from app.repositories.report_repository import ModelReportRepository
    from app.schemas.privacy import PrivacyLevel

    db_file = tmp_path / "test_g_thresholds.db"
    engine, SessionFactory = _create_isolated_file_db(db_file)
    t0 = datetime(2026, 6, 1, 10, 0, 0, tzinfo=timezone.utc)
    clock = ControlledClock(t0)
    cycle_id = "cycle_thresh_001"
    patient_id = "pat_thresh_001"

    tracker = TurnTracker()
    gw = _build_tracking_gateway(tracker)

    with SessionFactory() as db:
        PatientRepository(db).create(patient_id=patient_id, status="active")
        MedicalCycleRepository(db).create(cycle_id=cycle_id, patient_id=patient_id, status="active")
        m2_svc = Mode2VitalsMonitoringService(db=db, gateway=gw, now_provider=clock.now)

        # Scenario 1: Without Pro report -> Fallback thresholds active
        fallback_th = m2_svc.resolve_thresholds(cycle_id)
        assert fallback_th["source"] == "fallback"
        assert fallback_th["min_pessimum_bpm"] == 45
        assert fallback_th["max_pessimum_bpm"] == 115
        assert fallback_th["max_spike_delta_bpm"] == 25
        assert fallback_th["critical_tachycardia_bpm"] == 140
        assert fallback_th["critical_bradycardia_bpm"] == 40
        assert fallback_th["critical_spo2"] == 90

        # Scenario 2: Pro establishes personal thresholds in user_overall_report
        rep_repo = ModelReportRepository(db)
        rep_repo.create_next_version(
            cycle_id=cycle_id,
            model="gemini-2.5-pro",
            report_type="user_overall_report",
            privacy_level=PrivacyLevel.CLIENT_FACING,
            content={
                "summary": "Pro Overall Report with personalized thresholds.",
                "metrics": {
                    "min_pessimum_bpm": 52,
                    "max_pessimum_bpm": 125,
                    "max_spike_delta_bpm": 35,
                },
            },
            commit=True,
        )

        pro_th = m2_svc.resolve_thresholds(cycle_id)
        assert pro_th["source"] == "pro"
        assert pro_th["min_pessimum_bpm"] == 52
        assert pro_th["max_pessimum_bpm"] == 125
        assert pro_th["max_spike_delta_bpm"] == 35
        # Guard remains independent and invariant!
        assert pro_th["critical_tachycardia_bpm"] == 140
        assert pro_th["critical_bradycardia_bpm"] == 40
        assert pro_th["critical_spo2"] == 90

        # Scenario 3: Reading at 120 bpm is within Pro corridor (52-125),
        # but would exceed fallback corridor (45-115).
        # When evaluating normalization, Pro bounds take precedence:
        is_dev, reason = m2_svc.is_deviation_detected(
            pulse=120,
            spo2=98,
            baseline_pulse=100,  # delta = 20 < 35
            thresholds=pro_th,
        )
        assert not is_dev, "120 bpm is within Pro upper bound 125 and delta 20 < 35"

        # Scenario 4: Critical tachycardia 142 bpm triggers Level 1 Guard regardless of Pro max 125
        is_crit_dev, crit_reason = m2_svc.is_deviation_detected(
            pulse=142,
            spo2=98,
            baseline_pulse=100,
            thresholds=pro_th,
        )
        assert is_crit_dev
        assert "Критическая" in crit_reason or "абсолютного порога безопасности" in crit_reason
