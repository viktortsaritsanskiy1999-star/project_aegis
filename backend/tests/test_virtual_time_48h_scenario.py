"""
Project Aegis — 48-Hour Deterministic Virtual Time Scenario
Validates that the entire clinical automation and accompaniment stack
functions with accelerated virtual time without live Gemini API calls,
real-world sleeps, or non-deterministic race conditions.
"""

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
from typing import Any, Dict, List, Optional

import httpx
import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session, sessionmaker

from app.database import Base
from app.models.health_measurement import HealthMeasurement
from app.models.medical_cycle import MedicalCycle
from app.models.model_report import ModelReport
from app.models.patient import Patient
from app.repositories.cycle_repository import MedicalCycleRepository
from app.repositories.measurement_repository import HealthMeasurementRepository
from app.repositories.report_repository import ModelReportRepository
from app.schemas.chat import (
    AthenaChatRequest,
    AthenaDailyAssessmentModelOutput,
    AthenaDailyJournalRecord,
)
from app.schemas.privacy import PrivacyLevel
from app.schemas.wellness import (
    WellnessDailyReport,
    WellnessEpisodeEvaluation,
    WellnessModelOutput,
    WellnessStatus,
    WellnessTriggerType,
)
from app.services.athena_chat_service import AthenaChatService
from app.services.athena_daily_journal_service import (
    ATHENA_DAILY_JOURNAL_REPORT_TYPE,
    AthenaDailyJournalService,
)
from app.services.clinical_automation_service import (
    AutomationJobType,
    ClinicalAutomationService,
    ClinicalAutomationWorker,
)
from app.services.gemini_gateway import GeminiGateway, GeminiModelRole
from app.services.weekly_consilium_service import (
    ATHENA_SCHEDULE_ANCHOR_REPORT_TYPE,
    WeeklyConsiliumService,
)
from app.services.wellness_daily_service import (
    PATIENT_TIMEZONE_REPORT_TYPE,
    WELLNESS_DAILY_REPORT_TYPE,
    WellnessDailyService,
)


class ControlledVirtualClock:
    """Thread-safe virtual clock providing deterministic UTC time for tests."""

    def __init__(self, start_dt: datetime):
        if start_dt.tzinfo is None:
            self._current: datetime = start_dt.replace(tzinfo=timezone.utc)
        else:
            self._current: datetime = start_dt.astimezone(timezone.utc)

    def now(self) -> datetime:
        return self._current

    def advance(self, days: int = 0, hours: int = 0, minutes: int = 0, seconds: int = 0) -> datetime:
        self._current += timedelta(days=days, hours=hours, minutes=minutes, seconds=seconds)
        return self._current

    def set(self, dt: datetime) -> datetime:
        if dt.tzinfo is None:
            self._current = dt.replace(tzinfo=timezone.utc)
        else:
            self._current = dt.astimezone(timezone.utc)
        return self._current


class DeterministicMockGateway(GeminiGateway):
    """Deterministic offline GeminiGateway mock for 48h simulation."""

    def __init__(self):
        super().__init__()
        self._api_key = "test-virtual-time-key"

    async def generate_text(self, prompt: str, **kwargs) -> str:
        return "Deterministic text response"

    async def generate_raw_text(self, contents: List[Dict[str, Any]], **kwargs) -> str:
        return (
            "Здравствуйте! Как ваше самочувствие сегодня? "
            "[CLINICAL_REPORT] {\"status\": \"stable\", \"symptoms_mentioned\": [], \"red_flags\": false, \"summary_for_master\": \"Patient reports good rest.\"} [/CLINICAL_REPORT]"
        )

    async def generate_structured(self, prompt: str, response_schema: Any, **kwargs) -> Any:
        if response_schema is WellnessModelOutput:
            return WellnessModelOutput(
                observed_trends=["Resting heart rate in normal range (60-70 bpm)", "Adequate sleep duration (7.5-8.0h)"],
                limitations_and_gaps=[],
                questionable_measurements=[],
                episode_evaluations=[],
                summary_for_athena="Пациент выспался, гемодинамика стабильна.",
            )
        if response_schema is AthenaDailyAssessmentModelOutput:
            return AthenaDailyAssessmentModelOutput(
                status="NORMAL",
                interpretation="Athena Flash синтез: пациент сообщил об отличном самочувствии, подтвержденном телеметрией стабильного пульса.",
                observations=["отличное_самочувствие", "полноценный_сон"],
                relevant_changes=["Гемодинамика стабильна в течение суток"],
                contradictions=[],
                uncertainties=[],
                red_flags=False,
            )
        raise RuntimeError(f"Unexpected schema requested: {response_schema}")


def _seed_completed_cycle(
    session: Session,
    patient_id: str,
    cycle_id: str,
    anchor_dt: datetime,
) -> None:
    now_dt = anchor_dt
    p = Patient(patient_id=patient_id, status="active", created_at=now_dt)
    c = MedicalCycle(cycle_id=cycle_id, patient_id=patient_id, status="active", started_at=now_dt, created_at=now_dt)
    session.add(p)
    session.add(c)
    session.commit()

    # Seed baseline Primary Consilium reports
    primary_overall = ModelReport(
        report_id=f"rep_{cycle_id}_overall_v1",
        cycle_id=cycle_id,
        model="aegis-primary-pro",
        report_type="user_overall_report",
        privacy_level=PrivacyLevel.CLIENT_FACING.value,
        version=1,
        content={"title": "Primary Consilium", "summary": "Baseline established"},
        created_at=now_dt,
    )
    primary_pro = ModelReport(
        report_id=f"rep_{cycle_id}_chief_physician_v1",
        cycle_id=cycle_id,
        model="aegis-primary-pro",
        report_type="chief_physician_internal",
        privacy_level=PrivacyLevel.SERVER_ONLY.value,
        version=1,
        content={
            "pro_general_shelf": "Monitor blood pressure and sleep",
            "pro_training_shelf": "Cardiovascular monitoring protocol",
        },
        created_at=now_dt,
    )
    primary_flash = ModelReport(
        report_id=f"rep_{cycle_id}_deputy_v1",
        cycle_id=cycle_id,
        model="aegis-primary-flash",
        report_type="deputy_chief_physician_internal",
        privacy_level=PrivacyLevel.SERVER_ONLY.value,
        version=1,
        content={"flash_general_shelf": "Cardiovascular stability priority"},
        created_at=now_dt,
    )
    active_strat = ModelReport(
        report_id=f"rep_{cycle_id}_active_strat_v1",
        cycle_id=cycle_id,
        model="aegis-primary-pro",
        report_type="athena_active_strategy",
        privacy_level=PrivacyLevel.SERVER_ONLY.value,
        version=1,
        content={
            "active_strategy_text": "Отслеживать динамику утреннего пульса и сна.",
            "clinical_background_text": "Гемодинамический мониторинг.",
            "deputy_synthesis_text": "Пульс в покое 60-75 bpm.",
        },
        created_at=now_dt,
    )
    session.add_all([primary_overall, primary_pro, primary_flash, active_strat])
    session.commit()

    # Seed confirmed schedule anchor T0
    w_service = WeeklyConsiliumService(db=session, now_provider=lambda: anchor_dt)
    w_service.record_schedule_anchor(
        cycle_id=cycle_id,
        anchor_dt=anchor_dt,
        source="post_primary_greeting",
    )


@pytest.fixture
def sim_db_and_clock():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "sim_48h.db"
        engine = create_engine(
            f"sqlite:///{db_path}",
            connect_args={"check_same_thread": False},
        )

        @event.listens_for(engine, "connect")
        def _set_pragmas(dbapi_connection, _):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON;")
            cursor.close()

        Base.metadata.create_all(bind=engine)
        SessionFactory = sessionmaker(autocommit=False, autoflush=False, bind=engine)

        start_time = datetime(2026, 10, 19, 7, 0, 0, tzinfo=timezone.utc)
        clock = ControlledVirtualClock(start_time)

        yield SessionFactory, clock
        engine.dispose()


@pytest.mark.anyio
async def test_48_hour_deterministic_virtual_time_scenario(sim_db_and_clock):
    """
    Executes a comprehensive 48-virtual-hour scenario verifying that all scheduled
    tasks, time windows, and user interaction delays behave deterministically.
    """
    SessionFactory, clock = sim_db_and_clock
    gateway = DeterministicMockGateway()

    patient_id = "pat_sim_001"
    cycle_id = "cycle_sim_001"

    # ==========================================================================
    # 0. INITIAL SEEDING: Active Patient, Active Cycle, T0 Anchor & Timezone
    # ==========================================================================
    with SessionFactory() as db:
        _seed_completed_cycle(db, patient_id, cycle_id, clock.now())

        # Save explicit patient timezone: Europe/Moscow (+03:00)
        wellness_svc = WellnessDailyService(db=db, gateway=gateway, now_provider=clock.now)
        wellness_svc.save_patient_timezone(cycle_id, "Europe/Moscow")

    # ==========================================================================
    # 1. DAY 1, 07:00 UTC: Patient Wake-Up & Sleep Telemetry
    # ==========================================================================
    with SessionFactory() as db:
        m_repo = HealthMeasurementRepository(db)
        # Sleep session ending at 06:45 UTC with confirmed wake-up event
        m_repo.create(
            cycle_id=cycle_id,
            source="wearable_sleep",
            measured_at=clock.now() - timedelta(minutes=5),
            pulse=58,
            systolic=115,
            diastolic=75,
            spo2=99,
            raw_metadata={
                "event": "wake_up",
                "sleep_hours": 7.5,
                "deep_hours": 2.0,
                "rem_hours": 1.8,
                "light_hours": 3.2,
                "awake_hours": 0.5,
                "wake_time": "06:45",
            },
        )

    # ==========================================================================
    # 2. DAY 1, 07:15 UTC: Morning Resting Vitals Ingestion
    # ==========================================================================
    clock.advance(minutes=15)  # 07:15 UTC
    with SessionFactory() as db:
        m_repo = HealthMeasurementRepository(db)
        m_repo.create(
            cycle_id=cycle_id,
            source="blood_pressure_monitor",
            measured_at=clock.now(),
            pulse=68,
            systolic=120,
            diastolic=80,
            spo2=98,
            raw_metadata={"posture": "sitting", "rest_minutes": 5},
        )

    # ==========================================================================
    # 3. DAY 1, 07:30 UTC: Wellness Daily Run (Wake-Up Event Triggered)
    # ==========================================================================
    clock.advance(minutes=15)  # 07:30 UTC
    with SessionFactory() as db:
        wellness_svc = WellnessDailyService(db=db, gateway=gateway, now_provider=clock.now)
        trig = wellness_svc.evaluate_trigger(cycle_id, calendar_week=1, calendar_day=1, as_of=clock.now())
        assert trig == WellnessTriggerType.WAKE_UP

        report_d1, is_new = await wellness_svc.process_wellness_for_day(
            cycle_id=cycle_id, calendar_week=1, calendar_day=1, as_of=clock.now()
        )
        assert is_new is True
        assert report_d1.status == WellnessStatus.READY
        assert report_d1.trigger == WellnessTriggerType.WAKE_UP
        assert report_d1.calendar_day == 1

        # Verify report persisted
        stored_d1 = wellness_svc.get_daily_wellness_report(cycle_id, calendar_week=1, calendar_day=1)
        assert stored_d1 is not None
        assert stored_d1.report_id == report_d1.report_id

    # ==========================================================================
    # 4. DAY 1, 08:00 UTC: Athena Proactive Accompaniment Question
    # ==========================================================================
    clock.advance(minutes=30)  # 08:00 UTC
    with SessionFactory() as db:
        athena_svc = AthenaChatService(db=db, gateway=gateway, now_provider=clock.now)
        # First planned routine question
        q_resp = await athena_svc.handle_turn(
            patient_id=patient_id,
            cycle_id=cycle_id,
            request=AthenaChatRequest(
                message="First question",
                event_type="first_planned_question",
                idempotency_key="q1_sim_001_d1",
            ),
        )
        assert q_resp is not None
        assert q_resp.reply != ""

    # ==========================================================================
    # 5. DAY 1, 10:30 UTC: Delayed Patient Response (+2.5 Virtual Hours)
    # ==========================================================================
    clock.advance(hours=2, minutes=30)  # 10:30 UTC
    verbatim_patient_answer = "Чувствую себя отлично, выспался."
    with SessionFactory() as db:
        athena_svc = AthenaChatService(db=db, gateway=gateway, now_provider=clock.now)
        ans_resp = await athena_svc.handle_turn(
            patient_id=patient_id,
            cycle_id=cycle_id,
            request=AthenaChatRequest(
                message=verbatim_patient_answer,
                event_type="routine_answer",
                idempotency_key="ans1_sim_001_d1",
            ),
        )
        assert ans_resp is not None

    # ==========================================================================
    # 6. DAY 1, 12:00 Local Time (09:00 UTC): Midday Fallback Idempotency Check
    # ==========================================================================
    # Clock is currently at 10:30 UTC (which is 13:30 Moscow time, past 12:00 local midday)
    with SessionFactory() as db:
        wellness_svc = WellnessDailyService(db=db, gateway=gateway, now_provider=clock.now)
        rep_repo = ModelReportRepository(db)
        count_before = len(db.scalars(select(ModelReport).where(ModelReport.cycle_id == cycle_id)).all())
        
        rep_repeat, is_new = await wellness_svc.process_wellness_for_day(
            cycle_id=cycle_id, calendar_week=1, calendar_day=1, as_of=clock.now()
        )
        assert is_new is False
        assert rep_repeat.report_id == report_d1.report_id  # Returns existing report

        count_after = len(db.scalars(select(ModelReport).where(ModelReport.cycle_id == cycle_id)).all())
        assert count_before == count_after, "Midday trigger duplicated existing Day 1 wellness report!"

    # ==========================================================================
    # 7. DAY 1, 19:00 UTC: Evening Vitals Ingestion
    # ==========================================================================
    clock.advance(hours=8, minutes=30)  # 19:00 UTC
    with SessionFactory() as db:
        m_repo = HealthMeasurementRepository(db)
        m_repo.create(
            cycle_id=cycle_id,
            source="wearable_vitals",
            measured_at=clock.now(),
            pulse=72,
            systolic=122,
            diastolic=82,
            spo2=98,
        )

    # ==========================================================================
    # 8. CROSSING MIDNIGHT -> DAY 2, 07:05 UTC (Exactly 24h Window Elapsed)
    # ==========================================================================
    clock.advance(hours=12, minutes=5)  # 2026-10-20 07:05:00 UTC (+24h + 5m mark)
    with SessionFactory() as db:
        journal_svc = AthenaDailyJournalService(db=db, gateway=gateway, now_provider=clock.now)
        
        # Check that Day 1 observation window [T0, T0 + 24h) is now completed
        windows = journal_svc.get_completed_daily_windows(cycle_id, as_of=clock.now())
        assert len(windows) >= 1
        w_num, d_num, win_start, win_end = windows[0]
        assert w_num == 1
        assert d_num == 1
        # Day 1 has 10-minute backward buffer (86400 + 600 = 87000 seconds)
        assert (win_end - win_start).total_seconds() == 87000

        # Build Day 1 Athena Daily Journal
        journal_d1 = journal_svc.build_daily_journal(cycle_id, calendar_week=1, calendar_day=1)
        assert journal_d1 is not None
        assert journal_d1.calendar_day == 1
        assert journal_d1.calendar_week == 1

        # Verify that the delayed patient answer from 10:30 UTC was captured in Day 1 journal
        assert journal_d1.patient_reported_evidence.has_patient_interaction is True
        assert len(journal_d1.patient_reported_evidence.patient_answers) >= 1
        assert journal_d1.patient_reported_evidence.patient_answers[0].text == verbatim_patient_answer

        # Verify Day 1 wellness report was properly linked
        assert journal_d1.wellness_evidence.report_reference.is_available is True
        assert journal_d1.wellness_evidence.report_reference.report_id == report_d1.report_id

        # Close Day 1 journal (runs Gemini assessment, attaches it, saves to DB)
        closed_journal, saved_row, is_cached = await journal_svc.close_daily_journal(
            cycle_id=cycle_id, calendar_week=1, calendar_day=1, as_of=clock.now()
        )
        assert saved_row is not None
        assert closed_journal.athena_clinical_assessment.is_available is True
        assert closed_journal.athena_clinical_assessment.status == "NORMAL"

    # ==========================================================================
    # 9. DAY 2, 07:15 UTC: Day 2 Wake-Up and Vitals
    # ==========================================================================
    clock.advance(minutes=10)  # Day 2, 07:15 UTC
    with SessionFactory() as db:
        m_repo = HealthMeasurementRepository(db)
        m_repo.create(
            cycle_id=cycle_id,
            source="wearable_sleep",
            measured_at=clock.now() - timedelta(minutes=5),
            pulse=60,
            systolic=118,
            diastolic=78,
            spo2=99,
            raw_metadata={"event": "wake_up", "sleep_hours": 8.0, "wake_time": "07:00"},
        )

        # Day 2 Wellness Summary
        wellness_svc = WellnessDailyService(db=db, gateway=gateway, now_provider=clock.now)
        report_d2, is_new2 = await wellness_svc.process_wellness_for_day(
            cycle_id=cycle_id, calendar_week=1, calendar_day=2, as_of=clock.now()
        )
        assert is_new2 is True
        assert report_d2.calendar_day == 2
        assert report_d2.status == WellnessStatus.READY

    # ==========================================================================
    # 10. REPEATED AUTOMATION TICK: Verify Full Idempotency Across Subsystems
    # ==========================================================================
    clock.advance(hours=4)  # Day 2, 11:15 UTC
    with SessionFactory() as db:
        worker = ClinicalAutomationWorker(
            session_factory=SessionFactory,
            gateway_factory=lambda: gateway,
            now_provider=clock.now,
            enable_wellness_daily=True,
            enable_daily_journal=True,
        )
        # Execute single deterministic polling tick
        jobs = await worker.run_once(discover=True)

        # Check total counts in DB: exactly 1 journal for Day 1, 1 wellness for Day 1, 1 wellness for Day 2
        wellness_reports = db.scalars(
            select(ModelReport).where(
                ModelReport.cycle_id == cycle_id,
                ModelReport.report_type == WELLNESS_DAILY_REPORT_TYPE,
            )
        ).all()
        j1_reports = db.scalars(
            select(ModelReport).where(
                ModelReport.cycle_id == cycle_id,
                ModelReport.report_type == f"{ATHENA_DAILY_JOURNAL_REPORT_TYPE}_w1_d1",
            )
        ).all()

        assert len(wellness_reports) == 2, f"Expected 2 wellness reports (Day 1 + Day 2), found {len(wellness_reports)}"
        assert len(j1_reports) == 1, f"Expected 1 Day 1 daily journal, found {len(j1_reports)}"

    # ==========================================================================
    # 11. SUBSYSTEM DECOUPLING: Delay in Chat Does Not Block Vitals or Wellness
    # ==========================================================================
    clock.advance(hours=12)  # Day 2, 23:15 UTC
    with SessionFactory() as db:
        # Chat has had no messages for 12 hours (patient busy)
        # Vitals ingestion continues smoothly:
        m_repo = HealthMeasurementRepository(db)
        m_repo.create(
            cycle_id=cycle_id,
            source="continuous_heart_rate",
            measured_at=clock.now(),
            pulse=64,
            systolic=116,
            diastolic=76,
            spo2=98,
        )

        # Wellness query executes with zero errors and preserves state
        wellness_svc = WellnessDailyService(db=db, gateway=gateway, now_provider=clock.now)
        latest_rep = wellness_svc.get_daily_wellness_report(cycle_id, calendar_week=1, calendar_day=2)
        assert latest_rep is not None
        assert latest_rep.calendar_day == 2
