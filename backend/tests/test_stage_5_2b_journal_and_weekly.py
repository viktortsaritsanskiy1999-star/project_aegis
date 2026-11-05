import asyncio
from datetime import datetime, timedelta, timezone
import json
from typing import Any, Dict, List, Optional
import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.models import MedicalCycle, Patient, HealthMeasurement
from app.models.model_report import ModelReport
from app.repositories.cycle_repository import MedicalCycleRepository
from app.repositories.measurement_repository import HealthMeasurementRepository
from app.repositories.patient_repository import PatientRepository
from app.repositories.report_repository import ModelReportRepository
from app.schemas.chat import (
    AthenaDailyJournalRecord,
    BiometricsSummary,
    ExceptionalEventEntry,
    WellnessEvidence,
)
from app.schemas.health import HealthMeasurementCreate
from app.schemas.mode2 import (
    Mode2EpisodeRecord,
    Mode2EpisodeStatus,
    Mode2WorkoutContext,
)
from app.schemas.privacy import PrivacyLevel
from app.schemas.wellness import (
    Mode2EpisodeWellnessSummary,
    WellnessDailyReport,
    WellnessModelOutput,
    WellnessStatus,
    WellnessTriggerType,
)
from app.services.athena_chat_service import (
    ATHENA_CHAT_HISTORY_REPORT_TYPE,
    ATHENA_CHAT_TURN_REPORT_TYPE,
    ATHENA_ROUTINE_QUESTION_REPORT_TYPE,
    AthenaChatService,
)
from app.services.athena_weekly_plan_service import AthenaWeeklyPlanService
from app.services.athena_daily_journal_service import (
    ATHENA_DAILY_JOURNAL_REPORT_TYPE,
    AthenaDailyJournalService,
)
from app.services.gemini_gateway import GeminiGateway, GeminiModelRole
from app.services.mode2_vitals_service import (
    ATHENA_MODE2_EPISODE_REPORT_TYPE,
    Mode2VitalsMonitoringService,
)
from app.services.weekly_consilium_service import (
    ATHENA_ACTIVE_STRATEGY_REPORT_TYPE,
    WEEKLY_CHIEF_INTERNAL_REPORT_TYPE,
    WEEKLY_OVERALL_CLIENT_REPORT_TYPE,
    WeeklyConsiliumService,
)
from app.services.wellness_daily_service import (
    WELLNESS_DAILY_REPORT_TYPE,
    WellnessDailyService,
)


class MockClock:
    def __init__(self, start: datetime):
        self._current = start

    def now(self) -> datetime:
        return self._current

    def advance(self, **kwargs):
        self._current += timedelta(**kwargs)

    def set(self, dt: datetime):
        self._current = dt


def _gemini_structured_envelope(content_dict: dict) -> dict:
    return {
        "candidates": [
            {
                "content": {
                    "parts": [{"text": json.dumps(content_dict, ensure_ascii=False)}],
                    "role": "model",
                },
                "finishReason": "STOP",
            }
        ]
    }


def _make_gemini_gateway(mock_handler) -> GeminiGateway:
    transport = httpx.MockTransport(mock_handler)
    return GeminiGateway(
        api_key="test-api-key-mode2-mock",
        transport=transport,
        max_retries=1,
        sleep_func=lambda _: asyncio.sleep(0),
    )


def _smart_gemini_handler(req: httpx.Request):
    body = json.loads(req.content.decode("utf-8"))
    prompt = ""
    for part in body.get("contents", [{}])[0].get("parts", []):
        if "text" in part:
            prompt += part["text"]

    if "Internal Daily Clinical Assessment Scenario" in prompt or "AthenaDailyAssessmentModelOutput" in prompt:
        return httpx.Response(200, json=_gemini_structured_envelope({
            "status": "NORMAL",
            "interpretation": "Curator daily clinical synthesis.",
            "observations": ["Stable vitals"],
            "relevant_changes": [],
            "contradictions": [],
            "uncertainties": [],
            "red_flags": False,
        }))
    elif "Head of Department (Aegis Clinical Analyst)" in prompt or "weekly_deputy_analysis" in prompt:
        return httpx.Response(200, json=_gemini_structured_envelope({
            "weekly_deputy_analysis": "WEEK/WEEKLY SUMMARY:\n\n1. SYMPTOM DYNAMICS: Good stability.\n2. BOTTLENECKS: Monitored resting episodes.\n3. ADJUSTMENT: Keep monitoring.",
            "user_system_reports": {
                "cvs": {"summary": "Cardiovascular system stable with monitored episodes.", "has_changes": True},
                "gi_tract": {"summary": "GI tract stable.", "has_changes": False},
                "respiratory": {"summary": "Respiratory stable.", "has_changes": False},
                "nervous": {"summary": "Nervous system stable.", "has_changes": False},
                "musculoskeletal": {"summary": "Musculoskeletal stable.", "has_changes": False},
                "kidneys": {"summary": "Renal system stable.", "has_changes": False},
                "immune": {"summary": "Immune system stable.", "has_changes": False},
                "endocrine": {"summary": "Endocrine system stable.", "has_changes": False},
            },
        }))
    elif "Chief Physician of the Aegis expert system" in prompt or "updated_weekly_strategy" in prompt:
        return httpx.Response(200, json=_gemini_structured_envelope({
            "updated_weekly_strategy": "Continue active cardiac monitoring with focus on post-workout recovery.",
            "strategy_status": "updated",
            "weekly_overall_report": {
                "title": "Aegis Weekly Roadmap",
                "summary": "Week 1 complete. Overall cardiovascular state is well controlled.",
                "highlights": ["Heart rate dynamics within normal limits"],
            },
            "flash_quality_review": {
                "approved_for_publication": True,
                "rejection_reason": None,
                "corrected_user_system_reports": {},
            },
        }))
    elif "Weekly Plan" in prompt or "build_athena_weekly_plan_prompt" in prompt or "AthenaWeeklyPlanRecord" in prompt:
        return httpx.Response(200, json=_gemini_structured_envelope({
            "plan_id": "plan_w2_1",
            "cycle_id": "cycle_s52b_15",
            "week_number": 2,
            "source_strategy_report_id": "rep_strat_w2",
            "source_strategy_version": 1,
            "created_at_utc": "2026-11-09T06:00:00Z",
            "summary_for_curator": "Week 2 plan: monitor recovery and resting pulse",
            "goals": [
                {
                    "goal_id": "goal_w2_1",
                    "title": "Evaluate resting pulse dynamics",
                    "clinical_rationale": "Verify stability after Day 3 spike.",
                    "priority": 1,
                    "known_facts": ["Day 3 spike normalized within 6 minutes"],
                    "target_clarification": "Check for morning dizziness or fatigue",
                    "sufficiency_criteria": "Clarified twice over week",
                    "status": "active",
                    "questions_asked_count": 0,
                }
            ],
            "status": "active",
        }))
    elif "AthenaWeeklyWrapupOutput" in prompt or "weekly_wrapup" in prompt or "итог" in prompt.lower() or "medical vault" in prompt.lower():
        return httpx.Response(200, json=_gemini_structured_envelope({
            "message": "Неделя №1 завершена! Ознакомьтесь с подробным отчётом в медицинском сейфе (нажмите на силуэт человека). Week 1 complete.",
        }))
    else:
        return httpx.Response(200, json=_gemini_structured_envelope({
            "observed_trends": ["Heart rate dynamics within normal limits"],
            "limitations_and_gaps": [],
            "questionable_measurements": [],
            "episode_evaluations": [],
            "summary_for_athena": "Daily wellness report ready with objective telemetry analyzed.",
        }))


def _make_smart_gateway() -> GeminiGateway:
    return _make_gemini_gateway(_smart_gemini_handler)


@pytest.fixture
def db_session() -> Session:
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def _seed_patient_and_cycle(
    session: Session,
    patient_id: str,
    cycle_id: str,
    anchor_dt: datetime,
) -> None:
    PatientRepository(session).create(patient_id=patient_id, status="active")
    cycle = MedicalCycle(
        cycle_id=cycle_id,
        patient_id=patient_id,
        status="active",
        created_at=anchor_dt,
    )
    session.add(cycle)
    session.commit()

    # Seed baseline Primary Consilium reports
    primary_overall = ModelReport(
        report_id=f"rep_{cycle_id}_user_overall_report_v1",
        cycle_id=cycle_id,
        sprint_id=None,
        session_id=None,
        model="aegis-primary-pro",
        report_type="user_overall_report",
        privacy_level=PrivacyLevel.CLIENT_FACING.value,
        version=1,
        content={"title": "Primary Consilium", "summary": "Baseline established"},
        created_at=anchor_dt,
    )
    primary_pro = ModelReport(
        report_id=f"rep_{cycle_id}_chief_physician_internal_v1",
        cycle_id=cycle_id,
        sprint_id=None,
        session_id=None,
        model="aegis-primary-pro",
        report_type="chief_physician_internal",
        privacy_level=PrivacyLevel.SERVER_ONLY.value,
        version=1,
        content={
            "pro_general_shelf": "Monitor blood pressure and exercise recovery",
            "pro_training_shelf": "Cardiovascular endurance training protocol",
        },
        created_at=anchor_dt,
    )
    primary_flash = ModelReport(
        report_id=f"rep_{cycle_id}_deputy_chief_physician_internal_v1",
        cycle_id=cycle_id,
        sprint_id=None,
        session_id=None,
        model="aegis-primary-flash",
        report_type="deputy_chief_physician_internal",
        privacy_level=PrivacyLevel.SERVER_ONLY.value,
        version=1,
        content={"flash_general_shelf": "Cardiovascular stability priority"},
        created_at=anchor_dt,
    )
    session.add_all([primary_overall, primary_pro, primary_flash])
    session.commit()

    weekly_svc = WeeklyConsiliumService(db=session, now_provider=lambda: anchor_dt)
    weekly_svc.record_schedule_anchor(
        cycle_id=cycle_id,
        anchor_dt=anchor_dt,
        commit=True,
    )


# ==============================================================================
# 15 MANDATORY TEST SCENARIOS FOR STAGE 5.2B
# ==============================================================================

@pytest.mark.anyio
async def test_01_morning_spike_without_workout(db_session: Session):
    """
    Scenario 1: Morning pulse spike without workout.
    Mode 2 registers episode (not_working_out) -> Wellness READY -> Athena Daily Journal Section B & C.
    """
    t0 = datetime(2026, 11, 2, 6, 0, tzinfo=timezone.utc)
    clock = MockClock(t0)
    cycle_id = "cycle_s52b_01"
    patient_id = "patient_s52b_01"
    _seed_patient_and_cycle(db_session, patient_id, cycle_id, t0)

    # 1. Mode 2 tachycardia spike at 07:15
    clock.advance(minutes=75)  # 07:15
    gateway = _make_smart_gateway()
    mode2_svc = Mode2VitalsMonitoringService(db=db_session, gateway=gateway, now_provider=clock.now)

    res = await mode2_svc.process_measurement(
        cycle_id=cycle_id,
        pulse=142,
        spo2=97,
        source="wearable_sensor",
        measured_at=clock.now(),
    )
    assert res is not None
    ep_id = res.episode_id

    # Patient replies: sitting, no workout, feel slightly dizzy
    await mode2_svc.handle_user_statement(
        cycle_id=cycle_id,
        user_text="I'm sitting drinking coffee, not working out. Feeling slightly dizzy.",
        is_workout=False,
        symptoms="dizzy",
        as_of=clock.now() + timedelta(minutes=1),
    )

    # Normalize pulse and close episode
    clock.advance(minutes=5)
    await mode2_svc.process_measurement(cycle_id=cycle_id, pulse=78, measured_at=clock.now())
    clock.advance(minutes=1)
    await mode2_svc.process_measurement(cycle_id=cycle_id, pulse=75, measured_at=clock.now())
    assert mode2_svc.has_active_episode(cycle_id) is False

    # 2. Run Wellness Daily Service
    clock.set(t0 + timedelta(hours=6))  # 12:00
    wellness_svc = WellnessDailyService(db=db_session, gateway=gateway, now_provider=clock.now)
    well_rep, _ = await wellness_svc.process_wellness_for_day(
        cycle_id=cycle_id,
        calendar_week=1,
        calendar_day=1,
        trigger=WellnessTriggerType.SCHEDULED_MIDDAY,
        as_of=clock.now(),
    )
    assert well_rep.status == WellnessStatus.READY

    # 3. Close Daily Journal Day 1
    journal_svc = AthenaDailyJournalService(db=db_session, gateway=gateway, now_provider=clock.now)
    day_start = t0
    day_end = t0 + timedelta(days=1)
    journal, _, _ = await journal_svc.close_daily_journal(cycle_id=cycle_id, calendar_week=1, calendar_day=1, as_of=clock.now())

    # Assert Section B Mode 2 evidence
    assert len(journal.wellness_evidence.mode2_episodes) == 1
    we_ep = journal.wellness_evidence.mode2_episodes[0]
    assert we_ep.episode_id == ep_id
    assert we_ep.objective_metrics.trigger_pulse == 142
    assert we_ep.user_facts.workout_context == "not_working_out"
    assert any("dizzy" in s.lower() for s in we_ep.user_facts.confirmed_symptoms)
    assert any("sitting" in s.lower() for s in we_ep.user_facts.user_statements)

    # Assert Section C Exceptional Events
    mode2_events = [e for e in journal.exceptional_events if e.event_type == "mode2_episode"]
    assert len(mode2_events) == 1
    assert mode2_events[0].event_id == ep_id
    assert mode2_events[0].safety_tier == "Mode 2"


@pytest.mark.anyio
async def test_02_evening_repeat_spike_distinct_episode(db_session: Session):
    """
    Scenario 2: Evening repeat spike gets distinct episode_id and both are captured.
    """
    t0 = datetime(2026, 11, 2, 6, 0, tzinfo=timezone.utc)
    clock = MockClock(t0)
    cycle_id = "cycle_s52b_02"
    patient_id = "patient_s52b_02"
    _seed_patient_and_cycle(db_session, patient_id, cycle_id, t0)

    gateway = _make_smart_gateway()
    mode2_svc = Mode2VitalsMonitoringService(db=db_session, gateway=gateway, now_provider=clock.now)

    # Episode 1 in morning (08:00)
    clock.advance(hours=2)
    res1 = await mode2_svc.process_measurement(cycle_id=cycle_id, pulse=136, measured_at=clock.now())
    assert res1 is not None
    ep1_id = res1.episode_id
    await mode2_svc.handle_user_statement(cycle_id=cycle_id, user_text="No workout", is_workout=False, as_of=clock.now())
    clock.advance(minutes=5)
    await mode2_svc.process_measurement(cycle_id=cycle_id, pulse=76, measured_at=clock.now())
    clock.advance(minutes=1)
    await mode2_svc.process_measurement(cycle_id=cycle_id, pulse=74, measured_at=clock.now())
    assert mode2_svc.has_active_episode(cycle_id) is False

    # Episode 2 in evening (20:00)
    clock.advance(hours=12)
    res2 = await mode2_svc.process_measurement(cycle_id=cycle_id, pulse=139, measured_at=clock.now())
    assert res2 is not None
    ep2_id = res2.episode_id
    assert ep1_id != ep2_id
    await mode2_svc.handle_user_statement(cycle_id=cycle_id, user_text="Resting watching TV", is_workout=False, as_of=clock.now())
    clock.advance(minutes=5)
    await mode2_svc.process_measurement(cycle_id=cycle_id, pulse=77, measured_at=clock.now())
    clock.advance(minutes=1)
    await mode2_svc.process_measurement(cycle_id=cycle_id, pulse=73, measured_at=clock.now())

    # Close Day 1 journal
    journal_svc = AthenaDailyJournalService(db=db_session, gateway=gateway, now_provider=clock.now)
    journal, _, _ = await journal_svc.close_daily_journal(cycle_id=cycle_id, calendar_week=1, calendar_day=1, as_of=clock.now())


    assert len(journal.wellness_evidence.mode2_episodes) == 2
    ep_ids = [e.episode_id for e in journal.wellness_evidence.mode2_episodes]
    assert ep1_id in ep_ids
    assert ep2_id in ep_ids


@pytest.mark.anyio
async def test_03_confirmed_workout_and_recovery(db_session: Session):
    """
    Scenario 3: Confirmed workout and recovery dynamics in Daily Journal.
    """
    t0 = datetime(2026, 11, 2, 6, 0, tzinfo=timezone.utc)
    clock = MockClock(t0)
    cycle_id = "cycle_s52b_03"
    patient_id = "patient_s52b_03"
    _seed_patient_and_cycle(db_session, patient_id, cycle_id, t0)

    gateway = _make_smart_gateway()
    mode2_svc = Mode2VitalsMonitoringService(db=db_session, gateway=gateway, now_provider=clock.now)

    # 10:00 - High pulse during workout
    clock.advance(hours=4)
    res = await mode2_svc.process_measurement(
        cycle_id=cycle_id,
        pulse=152,
        phase="workout",
        measured_at=clock.now(),
    )
    assert res is not None
    ep_id = res.episode_id

    await mode2_svc.handle_user_statement(
        cycle_id=cycle_id,
        user_text="Running outside in the park, finished workout now",
        is_workout=True,
        as_of=clock.now() + timedelta(minutes=1),
    )

    # Recovery telemetry: elevated during recovery then normalizes
    clock.advance(minutes=3)
    await mode2_svc.process_measurement(cycle_id=cycle_id, pulse=130, phase="recovery_0_10", measured_at=clock.now())
    clock.advance(minutes=3)
    await mode2_svc.process_measurement(cycle_id=cycle_id, pulse=118, phase="recovery_0_10", measured_at=clock.now())
    clock.advance(minutes=6)
    await mode2_svc.process_measurement(cycle_id=cycle_id, pulse=98, phase="recovery_10_50", measured_at=clock.now())
    clock.advance(minutes=5)
    await mode2_svc.process_measurement(cycle_id=cycle_id, pulse=76, phase="recovery_10_50", measured_at=clock.now())
    clock.advance(minutes=1)
    await mode2_svc.process_measurement(cycle_id=cycle_id, pulse=72, phase="standard", measured_at=clock.now())

    # Close Day 1 journal
    journal_svc = AthenaDailyJournalService(db=db_session, gateway=gateway, now_provider=clock.now)
    journal, _, _ = await journal_svc.close_daily_journal(cycle_id=cycle_id, calendar_week=1, calendar_day=1, as_of=clock.now())


    assert len(journal.wellness_evidence.mode2_episodes) == 1
    ep = journal.wellness_evidence.mode2_episodes[0]
    assert ep.user_facts.workout_context == "confirmed_workout"
    assert ep.objective_metrics.max_pulse == 152
    assert ep.objective_metrics.min_pulse <= 76
    assert ep.objective_metrics.duration_minutes >= 15.0


@pytest.mark.anyio
async def test_04_routine_workout_without_mode2_alert(db_session: Session):
    """
    Scenario 4: Routine workout without tachycardia alarm.
    HR dynamics and recovery captured in ActivitySummary & Daily Journal without Mode 2 alert.
    """
    t0 = datetime(2026, 11, 2, 6, 0, tzinfo=timezone.utc)
    clock = MockClock(t0)
    cycle_id = "cycle_s52b_04"
    patient_id = "patient_s52b_04"
    _seed_patient_and_cycle(db_session, patient_id, cycle_id, t0)

    # Feed routine workout measurements: 105-115 bpm (under default 130 bpm Mode 2 threshold)
    meas_repo = HealthMeasurementRepository(db_session)
    clock.advance(hours=3)  # 09:00
    meas_repo.create(
        cycle_id=cycle_id,
        source="wearable_sensor",
        pulse=110,
        phase="workout",
        measured_at=clock.now(),
        raw_metadata={"workout_type": "cycling", "steps": 2500},
    )
    clock.advance(minutes=15)
    meas_repo.create(
        cycle_id=cycle_id,
        source="wearable_sensor",
        pulse=115,
        phase="workout",
        measured_at=clock.now(),
        raw_metadata={"workout_type": "cycling"},
    )
    clock.advance(minutes=10)
    meas_repo.create(
        cycle_id=cycle_id,
        source="wearable_sensor",
        pulse=88,
        phase="recovery_0_10",
        measured_at=clock.now(),
        raw_metadata={"phase": "recovery"},
    )
    clock.advance(minutes=10)
    meas_repo.create(
        cycle_id=cycle_id,
        source="wearable_sensor",
        pulse=76,
        phase="recovery_10_50",
        measured_at=clock.now(),
    )

    # Verify no Mode 2 episode was generated
    mode2_svc = Mode2VitalsMonitoringService(db=db_session, now_provider=clock.now)
    assert mode2_svc.get_episode_history(cycle_id) == []

    # Run Wellness Daily Service
    gateway = _make_smart_gateway()
    clock.set(t0 + timedelta(hours=14))
    wellness_svc = WellnessDailyService(db=db_session, gateway=gateway, now_provider=clock.now)
    rep, _ = await wellness_svc.process_wellness_for_day(
        cycle_id=cycle_id,
        calendar_week=1,
        calendar_day=1,
        trigger=WellnessTriggerType.SCHEDULED_MIDDAY,
        as_of=clock.now(),
    )
    assert rep.status == WellnessStatus.READY
    assert rep.factual_summary.activity is not None
    assert rep.factual_summary.activity.workout_type == "cycling"
    assert rep.factual_summary.activity.workout_avg_bpm == 112.5
    assert rep.factual_summary.activity.workout_max_bpm == 115
    assert rep.factual_summary.activity.recovery_min_bpm == 76

    # Close Daily Journal Day 1
    journal_svc = AthenaDailyJournalService(db=db_session, gateway=gateway, now_provider=clock.now)
    journal, _, _ = await journal_svc.close_daily_journal(cycle_id=cycle_id, calendar_week=1, calendar_day=1, as_of=clock.now())

    # Zero Mode 2 episodes
    assert len(journal.wellness_evidence.mode2_episodes) == 0

    # Activity summary preserved in biometrics
    act = journal.wellness_evidence.biometrics.activity_summary
    assert act is not None
    assert act["workout_type"] == "cycling"
    assert act["workout_max_bpm"] == 115
    assert act["recovery_min_bpm"] == 76

    # Format 7-day diary and verify activity is shown
    diary = journal_svc.format_seven_day_journals_for_consilium(cycle_id, sprint_number=1)
    assert "Activity & Routine Workout:" in diary
    assert "Workout: cycling" in diary
    assert "max 115 bpm" in diary


@pytest.mark.anyio
async def test_05_multiple_episodes_different_contexts(db_session: Session):
    """
    Scenario 5: Multiple episodes with different workout contexts (confirmed_workout, unknown).
    """
    t0 = datetime(2026, 11, 2, 6, 0, tzinfo=timezone.utc)
    clock = MockClock(t0)
    cycle_id = "cycle_s52b_05"
    patient_id = "patient_s52b_05"
    _seed_patient_and_cycle(db_session, patient_id, cycle_id, t0)

    gateway = _make_smart_gateway()
    mode2_svc = Mode2VitalsMonitoringService(db=db_session, gateway=gateway, now_provider=clock.now)

    # Ep 1: confirmed workout at 08:00
    clock.advance(hours=2)
    await mode2_svc.process_measurement(cycle_id=cycle_id, pulse=145, phase="workout", measured_at=clock.now())
    await mode2_svc.handle_user_statement(cycle_id=cycle_id, user_text="Gym workout", is_workout=True, as_of=clock.now())
    clock.advance(minutes=5)
    await mode2_svc.process_measurement(cycle_id=cycle_id, pulse=75, measured_at=clock.now())
    clock.advance(minutes=1)
    await mode2_svc.process_measurement(cycle_id=cycle_id, pulse=72, measured_at=clock.now())

    # Ep 2: unconfirmed spike at 15:00 (no reply -> unknown)
    clock.advance(hours=7)
    await mode2_svc.process_measurement(cycle_id=cycle_id, pulse=138, measured_at=clock.now())
    clock.advance(minutes=5)
    await mode2_svc.process_measurement(cycle_id=cycle_id, pulse=76, measured_at=clock.now())
    clock.advance(minutes=1)
    await mode2_svc.process_measurement(cycle_id=cycle_id, pulse=71, measured_at=clock.now())

    # Close journal Day 1
    journal_svc = AthenaDailyJournalService(db=db_session, gateway=gateway, now_provider=clock.now)
    journal, _, _ = await journal_svc.close_daily_journal(cycle_id=cycle_id, calendar_week=1, calendar_day=1, as_of=clock.now())


    eps = journal.wellness_evidence.mode2_episodes
    assert len(eps) == 2
    contexts = [e.user_facts.workout_context for e in eps]
    assert "confirmed_workout" in contexts
    assert "unknown" in contexts


@pytest.mark.anyio
async def test_06_absence_of_spo2_no_fake_values(db_session: Session):
    """
    Scenario 6: Absence of SpO2 without fake values.
    Missing data declaration explicitly generated, no invented numbers.
    """
    t0 = datetime(2026, 11, 2, 6, 0, tzinfo=timezone.utc)
    clock = MockClock(t0)
    cycle_id = "cycle_s52b_06"
    patient_id = "patient_s52b_06"
    _seed_patient_and_cycle(db_session, patient_id, cycle_id, t0)

    gateway = _make_smart_gateway()
    mode2_svc = Mode2VitalsMonitoringService(db=db_session, gateway=gateway, now_provider=clock.now)

    # Process pulse only (spo2=None)
    clock.advance(hours=1)
    await mode2_svc.process_measurement(cycle_id=cycle_id, pulse=137, spo2=None, measured_at=clock.now())
    await mode2_svc.handle_user_statement(cycle_id=cycle_id, user_text="No workout", is_workout=False, as_of=clock.now())
    clock.advance(minutes=5)
    await mode2_svc.process_measurement(cycle_id=cycle_id, pulse=75, spo2=None, measured_at=clock.now())
    clock.advance(minutes=1)
    await mode2_svc.process_measurement(cycle_id=cycle_id, pulse=72, spo2=None, measured_at=clock.now())

    journal_svc = AthenaDailyJournalService(db=db_session, gateway=gateway, now_provider=clock.now)
    journal, _, _ = await journal_svc.close_daily_journal(cycle_id=cycle_id, calendar_week=1, calendar_day=1, as_of=clock.now())


    ep = journal.wellness_evidence.mode2_episodes[0]
    assert ep.objective_metrics.spo2_min is None
    assert ep.objective_metrics.spo2_max is None
    assert ep.objective_metrics.spo2_history == []

    # Check missing data declaration
    decls = journal.wellness_evidence.missing_data_declarations
    assert any("SpO2 telemetry not available" in d for d in decls)


@pytest.mark.anyio
async def test_07_episode_continuing_after_wellness_report(db_session: Session):
    """
    Scenario 7: Episode continuing after Day 1 Daily Journal closure.
    Day 1 captures ongoing episode up to midnight; Day 2 captures its completion without crash.
    """
    t0 = datetime(2026, 11, 2, 6, 0, tzinfo=timezone.utc)
    clock = MockClock(t0)
    cycle_id = "cycle_s52b_07"
    patient_id = "patient_s52b_07"
    _seed_patient_and_cycle(db_session, patient_id, cycle_id, t0)

    gateway = _make_smart_gateway()
    mode2_svc = Mode2VitalsMonitoringService(db=db_session, gateway=gateway, now_provider=clock.now)

    # Start episode at 23:45 of Day 1
    clock.advance(hours=17, minutes=45)
    res = await mode2_svc.process_measurement(cycle_id=cycle_id, pulse=144, measured_at=clock.now())
    assert res is not None
    ep_id = res.episode_id

    # Close Day 1 journal at 00:00 (Day 2 start) while episode is still ongoing
    clock.set(t0 + timedelta(days=1))
    journal_svc = AthenaDailyJournalService(db=db_session, gateway=gateway, now_provider=clock.now)
    journal_d1, _, _ = await journal_svc.close_daily_journal(cycle_id=cycle_id, calendar_week=1, calendar_day=1, as_of=clock.now())


    assert len(journal_d1.wellness_evidence.mode2_episodes) == 1
    assert journal_d1.wellness_evidence.mode2_episodes[0].episode_id == ep_id

    # Advance clock to 00:20 of Day 2, normalize and close episode
    clock.advance(minutes=20)
    await mode2_svc.process_measurement(cycle_id=cycle_id, pulse=76, measured_at=clock.now())
    clock.advance(minutes=1)
    await mode2_svc.process_measurement(cycle_id=cycle_id, pulse=73, measured_at=clock.now())
    assert mode2_svc.has_active_episode(cycle_id) is False

    # Close Day 2 journal
    clock.set(t0 + timedelta(days=2))
    journal_d2, _, _ = await journal_svc.close_daily_journal(cycle_id=cycle_id, calendar_week=1, calendar_day=2, as_of=clock.now())

    # Day 2 captures the completed episode trajectory
    assert len(journal_d2.wellness_evidence.mode2_episodes) == 1
    assert journal_d2.wellness_evidence.mode2_episodes[0].episode_id == ep_id
    assert journal_d2.wellness_evidence.mode2_episodes[0].closed_at_utc is not None


@pytest.mark.anyio
async def test_08_boundary_events_between_two_daily_journals(db_session: Session):
    """
    Scenario 8: Boundary events between Day 1 and Day 2 without loss or duplicate inclusion.
    """
    t0 = datetime(2026, 11, 2, 6, 0, tzinfo=timezone.utc)
    clock = MockClock(t0)
    cycle_id = "cycle_s52b_08"
    patient_id = "patient_s52b_08"
    _seed_patient_and_cycle(db_session, patient_id, cycle_id, t0)

    gateway = _make_smart_gateway()
    mode2_svc = Mode2VitalsMonitoringService(db=db_session, gateway=gateway, now_provider=clock.now)

    # Episode 1: 23:10 -> 23:30 (Day 1)
    clock.advance(hours=17, minutes=10)
    res1 = await mode2_svc.process_measurement(cycle_id=cycle_id, pulse=135, measured_at=clock.now())
    assert res1 is not None
    ep1_id = res1.episode_id
    clock.advance(minutes=5)
    await mode2_svc.process_measurement(cycle_id=cycle_id, pulse=75, measured_at=clock.now())
    clock.advance(minutes=1)
    await mode2_svc.process_measurement(cycle_id=cycle_id, pulse=72, measured_at=clock.now())

    # Episode 2: 00:05 of Day 2
    clock.set(t0 + timedelta(days=1, minutes=5))
    res2 = await mode2_svc.process_measurement(cycle_id=cycle_id, pulse=140, measured_at=clock.now())
    assert res2 is not None
    ep2_id = res2.episode_id
    clock.advance(minutes=5)
    await mode2_svc.process_measurement(cycle_id=cycle_id, pulse=74, measured_at=clock.now())
    clock.advance(minutes=1)
    await mode2_svc.process_measurement(cycle_id=cycle_id, pulse=71, measured_at=clock.now())

    journal_svc = AthenaDailyJournalService(db=db_session, gateway=gateway, now_provider=clock.now)

    # Day 1: strictly Ep 1
    j1, _, _ = await journal_svc.close_daily_journal(cycle_id=cycle_id, calendar_week=1, calendar_day=1, as_of=clock.now())

    assert [e.episode_id for e in j1.wellness_evidence.mode2_episodes] == [ep1_id]

    # Day 2: strictly Ep 2
    j2, _, _ = await journal_svc.close_daily_journal(cycle_id=cycle_id, calendar_week=1, calendar_day=2, as_of=clock.now())

    assert [e.episode_id for e in j2.wellness_evidence.mode2_episodes] == [ep2_id]


@pytest.mark.anyio
async def test_09_unjournaled_episode_prior_to_weekly_boundary(db_session: Session):
    """
    Scenario 9: Episode prior to weekly boundary not yet in closed journal.
    Weekly Consilium captures it in the UNJOURNALED section of the diary without data loss.
    """
    t0 = datetime(2026, 11, 2, 6, 0, tzinfo=timezone.utc)
    clock = MockClock(t0)
    cycle_id = "cycle_s52b_09"
    patient_id = "patient_s52b_09"
    _seed_patient_and_cycle(db_session, patient_id, cycle_id, t0)

    gateway = _make_smart_gateway()
    journal_svc = AthenaDailyJournalService(db=db_session, gateway=gateway, now_provider=clock.now)

    # Close journals for Days 1 to 6
    for day in range(1, 7):
        w_start = t0 + timedelta(days=day - 1)
        w_end = t0 + timedelta(days=day)
        await journal_svc.close_daily_journal(cycle_id=cycle_id, calendar_week=1, calendar_day=day, as_of=clock.now())

    # On Day 7 afternoon (T0 + 160h), Mode 2 registers an episode
    mode2_svc = Mode2VitalsMonitoringService(db=db_session, gateway=gateway, now_provider=clock.now)
    clock.set(t0 + timedelta(hours=160))
    res = await mode2_svc.process_measurement(cycle_id=cycle_id, pulse=141, measured_at=clock.now())
    assert res is not None
    ep_id = res.episode_id
    await mode2_svc.handle_user_statement(cycle_id=cycle_id, user_text="Sudden palpitations while cooking", is_workout=False, symptoms="palpitations", as_of=clock.now())
    clock.advance(minutes=5)
    await mode2_svc.process_measurement(cycle_id=cycle_id, pulse=76, measured_at=clock.now())
    clock.advance(minutes=1)
    await mode2_svc.process_measurement(cycle_id=cycle_id, pulse=72, measured_at=clock.now())

    # Day 7 journal has NOT closed. Weekly Consilium runs at T0 + 168h
    clock.set(t0 + timedelta(hours=168))
    diary = journal_svc.format_seven_day_journals_for_consilium(
        cycle_id=cycle_id,
        sprint_number=1,
        sprint_end_dt=clock.now(),
    )

    assert "=== UNJOURNALED MODE 2 EPISODES (PRIOR TO WEEKLY BOUNDARY) ===" in diary
    assert ep_id in diary
    assert "Trigger: 141 bpm" in diary
    assert "Sudden palpitations while cooking" in diary


@pytest.mark.anyio
async def test_10_boundary_crossing_weekly_sprint_cutoff(db_session: Session):
    """
    Scenario 10: Episode starts shortly before T0 + 168h and ends into Week 2.
    Weekly Consilium Week 1 annotates [BOUNDARY-CROSSING], filters telemetry up to cutoff, preserves same episode_id.
    """
    t0 = datetime(2026, 11, 2, 6, 0, tzinfo=timezone.utc)
    clock = MockClock(t0)
    cycle_id = "cycle_s52b_10"
    patient_id = "patient_s52b_10"
    _seed_patient_and_cycle(db_session, patient_id, cycle_id, t0)

    gateway = _make_smart_gateway()
    mode2_svc = Mode2VitalsMonitoringService(db=db_session, gateway=gateway, now_provider=clock.now)

    # Starts at T0 + 167.5h (30 min before Week 1 cutoff)
    clock.set(t0 + timedelta(hours=167, minutes=30))
    res = await mode2_svc.process_measurement(cycle_id=cycle_id, pulse=146, measured_at=clock.now())
    assert res is not None
    ep_id = res.episode_id

    # Sample at 167.8h (still Week 1)
    clock.advance(minutes=18)
    await mode2_svc.process_measurement(cycle_id=cycle_id, pulse=144, measured_at=clock.now())

    # Sample at 168.2h (Week 2)
    clock.advance(minutes=24)
    await mode2_svc.process_measurement(cycle_id=cycle_id, pulse=135, measured_at=clock.now())

    # Normalizes at 168.5h
    clock.advance(minutes=18)
    await mode2_svc.process_measurement(cycle_id=cycle_id, pulse=75, measured_at=clock.now())
    clock.advance(minutes=1)
    await mode2_svc.process_measurement(cycle_id=cycle_id, pulse=72, measured_at=clock.now())

    # Format Week 1 consilium diary with cutoff at T0 + 168h
    journal_svc = AthenaDailyJournalService(db=db_session, gateway=gateway, now_provider=clock.now)
    cutoff_dt = t0 + timedelta(hours=168)
    diary = journal_svc.format_seven_day_journals_for_consilium(
        cycle_id=cycle_id,
        sprint_number=1,
        sprint_end_dt=cutoff_dt,
        allow_empty=True,
    )

    assert ep_id in diary
    assert "[BOUNDARY-CROSSING]" in diary
    assert "telemetry evaluated up to cutoff" in diary


@pytest.mark.anyio
async def test_11_no_duplicate_records_after_server_restart(db_session: Session):
    """
    Scenario 11: Idempotency after server restart.
    Re-instantiating services produces exact same journals and formatting with 0 duplicates.
    """
    t0 = datetime(2026, 11, 2, 6, 0, tzinfo=timezone.utc)
    clock = MockClock(t0)
    cycle_id = "cycle_s52b_11"
    patient_id = "patient_s52b_11"
    _seed_patient_and_cycle(db_session, patient_id, cycle_id, t0)

    gateway = _make_smart_gateway()
    mode2_svc = Mode2VitalsMonitoringService(db=db_session, gateway=gateway, now_provider=clock.now)

    clock.advance(hours=2)
    res = await mode2_svc.process_measurement(cycle_id=cycle_id, pulse=138, measured_at=clock.now())
    clock.advance(minutes=5)
    await mode2_svc.process_measurement(cycle_id=cycle_id, pulse=74, measured_at=clock.now())
    clock.advance(minutes=1)
    await mode2_svc.process_measurement(cycle_id=cycle_id, pulse=71, measured_at=clock.now())

    # Close journal with first instance
    j_svc1 = AthenaDailyJournalService(db=db_session, gateway=gateway, now_provider=clock.now)
    j1, _, _ = await j_svc1.close_daily_journal(cycle_id, 1, 1, as_of=clock.now())

    # Simulate restart: fresh service instance
    j_svc2 = AthenaDailyJournalService(db=db_session, gateway=gateway, now_provider=clock.now)
    j2, _, _ = await j_svc2.close_daily_journal(cycle_id, 1, 1, as_of=clock.now())

    assert j1.journal_id == j2.journal_id
    assert len(j2.wellness_evidence.mode2_episodes) == 1

    reports = db_session.query(ModelReport).filter(
        ModelReport.cycle_id == cycle_id,
        ModelReport.report_type.like(f"{ATHENA_DAILY_JOURNAL_REPORT_TYPE}%"),
    ).all()
    assert len(reports) == 1


@pytest.mark.anyio
async def test_12_patient_isolation_two_cycles(db_session: Session):
    """
    Scenario 12: Patient and cycle isolation.
    Daily journals and weekly diaries strictly isolate episodes between different cycles.
    """
    t0 = datetime(2026, 11, 2, 6, 0, tzinfo=timezone.utc)
    clock = MockClock(t0)
    cycle_a = "cycle_s52b_12_a"
    patient_a = "patient_s52b_12_a"
    cycle_b = "cycle_s52b_12_b"
    patient_b = "patient_s52b_12_b"

    _seed_patient_and_cycle(db_session, patient_a, cycle_a, t0)
    _seed_patient_and_cycle(db_session, patient_b, cycle_b, t0)

    gateway = _make_smart_gateway()
    mode2_svc = Mode2VitalsMonitoringService(db=db_session, gateway=gateway, now_provider=clock.now)

    # Cycle A episode
    clock.advance(hours=1)
    res_a = await mode2_svc.process_measurement(cycle_id=cycle_a, pulse=145, measured_at=clock.now())
    assert res_a is not None
    ep_a = res_a.episode_id

    # Cycle B episode
    clock.advance(hours=1)
    res_b = await mode2_svc.process_measurement(cycle_id=cycle_b, pulse=150, measured_at=clock.now())
    assert res_b is not None
    ep_b = res_b.episode_id

    # Close journals
    journal_svc = AthenaDailyJournalService(db=db_session, gateway=gateway, now_provider=clock.now)
    j_a, _, _ = await journal_svc.close_daily_journal(cycle_a, 1, 1, as_of=clock.now())
    j_b, _, _ = await journal_svc.close_daily_journal(cycle_b, 1, 1, as_of=clock.now())

    assert [e.episode_id for e in j_a.wellness_evidence.mode2_episodes] == [ep_a]
    assert [e.episode_id for e in j_b.wellness_evidence.mode2_episodes] == [ep_b]

    diary_a = journal_svc.format_seven_day_journals_for_consilium(cycle_a, sprint_number=1)
    assert ep_a in diary_a
    assert ep_b not in diary_a


@pytest.mark.anyio
async def test_13_server_only_privacy(db_session: Session):
    """
    Scenario 13: SERVER_ONLY privacy preservation.
    Internal Athena Daily Journals and Mode 2 episodes never leak to client chat.
    """
    t0 = datetime(2026, 11, 2, 6, 0, tzinfo=timezone.utc)
    clock = MockClock(t0)
    cycle_id = "cycle_s52b_13"
    patient_id = "patient_s52b_13"
    _seed_patient_and_cycle(db_session, patient_id, cycle_id, t0)

    gateway = _make_smart_gateway()
    mode2_svc = Mode2VitalsMonitoringService(db=db_session, gateway=gateway, now_provider=clock.now)

    clock.advance(hours=2)
    await mode2_svc.process_measurement(cycle_id=cycle_id, pulse=142, measured_at=clock.now())
    clock.advance(minutes=5)
    await mode2_svc.process_measurement(cycle_id=cycle_id, pulse=75, measured_at=clock.now())

    journal_svc = AthenaDailyJournalService(db=db_session, gateway=gateway, now_provider=clock.now)
    journal, _, _ = await journal_svc.close_daily_journal(cycle_id, 1, 1, as_of=clock.now())
    reports = db_session.query(ModelReport).filter(
        ModelReport.cycle_id == cycle_id,
        ModelReport.report_type.like(f"{ATHENA_DAILY_JOURNAL_REPORT_TYPE}%"),
    ).all()
    assert len(reports) == 1
    assert reports[0].privacy_level == PrivacyLevel.SERVER_ONLY.value

    # Verify sanitized client chat history does NOT contain daily journal contents
    weekly_svc = WeeklyConsiliumService(db=db_session, now_provider=clock.now)
    chat_hist = weekly_svc.get_athena_chat_history(cycle_id)
    chat_hist_str = json.dumps(chat_hist)
    assert "AthenaDailyJournalRecord" not in chat_hist_str
    assert "ATHENA_GENERATED_INTERPRETATION" not in chat_hist_str


@pytest.mark.anyio
async def test_14_no_unnecessary_gemini_calls(db_session: Session):
    """
    Scenario 14: Zero unnecessary Gemini calls during journal closure and diary formatting.
    """
    t0 = datetime(2026, 11, 2, 6, 0, tzinfo=timezone.utc)
    clock = MockClock(t0)
    cycle_id = "cycle_s52b_14"
    patient_id = "patient_s52b_14"
    _seed_patient_and_cycle(db_session, patient_id, cycle_id, t0)

    call_count = 0
    def counting_handler(req):
        nonlocal call_count
        call_count += 1
        return httpx.Response(200, json=_gemini_structured_envelope({
            "status": "ready",
            "interpretation": "Interpretation text",
            "observations": [],
            "relevant_changes": [],
            "contradictions": [],
            "uncertainties": [],
            "red_flags": False,
        }))

    gateway = _make_gemini_gateway(counting_handler)
    journal_svc = AthenaDailyJournalService(db=db_session, gateway=gateway, now_provider=clock.now)

    # Close daily journal without calling Gemini assessment (generate_gemini_assessment=False)
    j, _, _ = await journal_svc.close_daily_journal(cycle_id=cycle_id, calendar_week=1, calendar_day=1, as_of=clock.now())
    assert call_count == 0

    # Format 7-day diary for Weekly Consilium
    diary = journal_svc.format_seven_day_journals_for_consilium(cycle_id, sprint_number=1)
    assert diary is not None
    assert call_count == 0


@pytest.mark.anyio
async def test_15_full_seven_day_e2e_weekly_consilium(db_session: Session):
    """
    Scenario 15: Full 7-day progression with ControlledClock.
    Mode 2 episodes (workout + resting spike), routine workouts, patient answers, daily journals closed.
    Automatic Weekly Consilium executes Flash -> Pro cascade and updates strategy & safe vault reports.
    """
    t0 = datetime(2026, 11, 2, 6, 0, tzinfo=timezone.utc)
    clock = MockClock(t0)
    cycle_id = "cycle_s52b_15"
    patient_id = "patient_s52b_15"
    _seed_patient_and_cycle(db_session, patient_id, cycle_id, t0)

    gateway = _make_smart_gateway()
    mode2_svc = Mode2VitalsMonitoringService(db=db_session, gateway=gateway, now_provider=clock.now)
    journal_svc = AthenaDailyJournalService(db=db_session, gateway=gateway, now_provider=clock.now)

    # Day 1: Routine workout without Mode 2 alert
    clock.advance(hours=3)
    meas_repo = HealthMeasurementRepository(db_session)
    meas_repo.create(
        cycle_id=cycle_id,
        source="wearable_sensor",
        pulse=112,
        phase="workout",
        measured_at=clock.now(),
        raw_metadata={"workout_type": "jogging"},
    )
    clock.advance(minutes=20)
    meas_repo.create(
        cycle_id=cycle_id,
        source="wearable_sensor",
        pulse=78,
        phase="recovery_0_10",
        measured_at=clock.now(),
    )

    # Day 1 closure
    clock.set(t0 + timedelta(days=1))
    await journal_svc.close_daily_journal(cycle_id, 1, 1, as_of=clock.now())

    # Day 2: Normal day, close journal
    clock.set(t0 + timedelta(days=2))
    await journal_svc.close_daily_journal(cycle_id, 1, 2, as_of=clock.now())

    # Day 3: Mode 2 resting spike (142 bpm)
    clock.set(t0 + timedelta(days=2, hours=4))
    res = await mode2_svc.process_measurement(cycle_id=cycle_id, pulse=142, measured_at=clock.now())
    await mode2_svc.handle_user_statement(cycle_id=cycle_id, user_text="Resting on couch, no workout", is_workout=False, as_of=clock.now())
    clock.advance(minutes=5)
    await mode2_svc.process_measurement(cycle_id=cycle_id, pulse=76, measured_at=clock.now())
    clock.advance(minutes=1)
    await mode2_svc.process_measurement(cycle_id=cycle_id, pulse=72, measured_at=clock.now())

    # Day 3 closure
    clock.set(t0 + timedelta(days=3))
    await journal_svc.close_daily_journal(cycle_id, 1, 3, as_of=clock.now())

    # Days 4 to 7 closure
    for day in range(4, 8):
        clock.set(t0 + timedelta(days=day))
        await journal_svc.close_daily_journal(cycle_id=cycle_id, calendar_week=1, calendar_day=day, as_of=clock.now())

    # Advance clock to T0 + 168h
    clock.set(t0 + timedelta(hours=168))

    # Run Automatic Weekly Consilium
    weekly_svc = WeeklyConsiliumService(db=db_session, gateway=gateway, now_provider=clock.now)
    res_consilium = await weekly_svc.run_weekly_consilium(cycle_id=cycle_id)
    assert res_consilium.sprint_number == 1
    assert res_consilium.overall_report is not None

    # Check published Vault reports (overall + 8 systems)
    report_repo = ModelReportRepository(db_session)
    overall_reps = report_repo.list_client_facing(cycle_id, WEEKLY_OVERALL_CLIENT_REPORT_TYPE)
    assert len(overall_reps) >= 1
    overall_rep = overall_reps[-1]
    assert "Week 1 complete" in overall_rep.content.get("summary", "")

    # Check Chief Physician Pro Strategy is SERVER_ONLY
    pro_reps = report_repo.list_server_only(cycle_id, ATHENA_ACTIVE_STRATEGY_REPORT_TYPE)
    assert len(pro_reps) >= 1
    pro_rep = pro_reps[0]
    strat_text = pro_rep.content.get("active_strategy_text") or pro_rep.content.get("updated_weekly_strategy", "")
    assert "Continue active cardiac monitoring" in strat_text

    # Check Step B: new Weekly Plan generated for Week 2
    plan_svc = AthenaWeeklyPlanService(db=db_session, gateway=gateway, now_provider=clock.now)
    plan_w2 = await plan_svc.create_or_get_weekly_plan(cycle_id=cycle_id, week_number=2)
    assert plan_w2.week_number == 2
    assert len(plan_w2.goals) >= 1

    # Check Step C: Wrapup message sent to Athena chat
    chat_svc = AthenaChatService(db=db_session, gateway=gateway, now_provider=clock.now)
    wrapup_turn = await chat_svc.send_weekly_wrapup_message(cycle_id=cycle_id, sprint_number=1)
    assert wrapup_turn is not None
    assert "Week 1 complete" in wrapup_turn.reply


@pytest.mark.anyio
async def test_16_mode2_episode_deduplication_between_section_b_and_c(db_session: Session):
    """
    Scenario 16: Deduplication check between Section B and Section C.
    Verifies that a single Mode 2 episode represented in both Section B and Section C of a daily journal
    shares identical episode_id, is recognized as a single event via get_unique_mode2_episode_ids,
    and is formatted with cross-referencing and deduplication rules for Weekly Consilium.
    """
    t0 = datetime(2026, 11, 2, 6, 0, tzinfo=timezone.utc)
    clock = MockClock(t0)
    cycle_id = "cycle_s52b_16"
    patient_id = "patient_s52b_16"
    _seed_patient_and_cycle(db_session, patient_id, cycle_id, t0)

    gateway = _make_smart_gateway()
    mode2_svc = Mode2VitalsMonitoringService(db=db_session, gateway=gateway, now_provider=clock.now)

    # 1. Trigger Mode 2 tachycardia spike
    clock.advance(minutes=45)
    res = await mode2_svc.process_measurement(cycle_id=cycle_id, pulse=145, measured_at=clock.now())
    ep_id = res.episode_id
    await mode2_svc.handle_user_statement(cycle_id=cycle_id, user_text="Resting, slight palpitations", is_workout=False, symptoms="palpitations", as_of=clock.now())
    clock.advance(minutes=5)
    await mode2_svc.process_measurement(cycle_id=cycle_id, pulse=75, measured_at=clock.now())
    clock.advance(minutes=1)
    await mode2_svc.process_measurement(cycle_id=cycle_id, pulse=72, measured_at=clock.now())

    # 2. Close Day 1 Daily Journal
    clock.set(t0 + timedelta(days=1))
    journal_svc = AthenaDailyJournalService(db=db_session, gateway=gateway, now_provider=clock.now)
    journal, _, _ = await journal_svc.close_daily_journal(cycle_id=cycle_id, calendar_week=1, calendar_day=1, as_of=clock.now())

    # 3. Assert Section B has the episode
    assert len(journal.wellness_evidence.mode2_episodes) == 1
    sec_b_ep = journal.wellness_evidence.mode2_episodes[0]
    assert sec_b_ep.episode_id == ep_id

    # 4. Assert Section C has the corresponding exceptional event
    mode2_events = [e for e in journal.exceptional_events if e.event_type == "mode2_episode"]
    assert len(mode2_events) == 1
    sec_c_ev = mode2_events[0]
    assert sec_c_ev.event_id == ep_id

    # 5. Programmatic deduplication assertion
    unique_ids = AthenaDailyJournalService.get_unique_mode2_episode_ids(journal)
    assert len(unique_ids) == 1
    assert ep_id in unique_ids

    # 6. Check formatted 7-day diary for Weekly Consilium
    formatted = journal_svc.format_seven_day_journals_for_consilium(cycle_id=cycle_id, sprint_number=1)
    assert formatted is not None
    assert "DEDUPLICATION RULE" in formatted
    assert f"episode_id: {ep_id}" in formatted
    assert f"[Cross-reference: Same clinical incident as Episode {ep_id} in Section B above; DO NOT count as a separate event]" in formatted
