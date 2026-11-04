import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.models.base import Base
from app.models.health_measurement import HealthMeasurement
from app.models.medical_cycle import MedicalCycle
from app.models.model_report import ModelReport
from app.models.patient import Patient
from app.repositories.cycle_repository import MedicalCycleRepository
from app.repositories.measurement_repository import HealthMeasurementRepository
from app.repositories.report_repository import ModelReportRepository
from app.repositories.sprint_repository import WeeklySprintRepository
from app.schemas.chat import (
    AthenaChatRequest,
    AthenaDailyAssessmentModelOutput,
    AthenaDailyJournalRecord,
)
from app.schemas.consilium import (
    AegisAgentRole,
    AthenaClinicalLogEntry,
)
from app.schemas.privacy import PrivacyLevel
from app.services.athena_chat_service import (
    AthenaChatService,
    ATHENA_ROUTINE_QUESTION_REPORT_TYPE,
)
from app.services.athena_daily_journal_service import (
    AthenaDailyJournalService,
    ATHENA_DAILY_JOURNAL_REPORT_TYPE,
    ATHENA_CLINICAL_LOG_REPORT_TYPE,
)
from app.services.clinical_automation_service import (
    AutomationJobStatus,
    AutomationJobType,
    ClinicalAutomationService,
    ClinicalAutomationWorker,
)
from app.services.gemini_gateway import GeminiGateway, GeminiModelRole
from app.services.primary_consilium_service import PrimaryConsiliumService
from app.services.weekly_consilium_service import WeeklyConsiliumService


class ControlledClock:
    def __init__(self, initial_dt: datetime):
        if initial_dt.tzinfo is None:
            self._current = initial_dt.replace(tzinfo=timezone.utc)
        else:
            self._current = initial_dt.astimezone(timezone.utc)

    def now(self) -> datetime:
        return self._current

    def advance(self, **kwargs) -> datetime:
        self._current = self._current + timedelta(**kwargs)
        return self._current

    def set(self, dt: datetime) -> None:
        if dt.tzinfo is None:
            self._current = dt.replace(tzinfo=timezone.utc)
        else:
            self._current = dt.astimezone(timezone.utc)


def _create_file_db(db_path: Path):
    engine = create_engine(
        f"sqlite:///{db_path.as_posix()}",
        connect_args={"check_same_thread": False, "timeout": 30.0},
    )
    Base.metadata.create_all(bind=engine)
    session_factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    return engine, session_factory


def _seed_completed_cycle(
    session: Session,
    patient_id: str,
    cycle_id: str,
    anchor_dt: datetime,
) -> None:
    now_dt = anchor_dt
    p = Patient(
        patient_id=patient_id,
        status="active",
        created_at=now_dt,
    )
    c = MedicalCycle(
        cycle_id=cycle_id,
        patient_id=patient_id,
        status="active",
        created_at=now_dt,
    )
    session.add(p)
    session.add(c)
    session.commit()

    # Seed completed Primary Consilium reports
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
        created_at=now_dt,
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
            "pro_general_shelf": "Monitor blood pressure and sleep",
            "pro_training_shelf": "Background cardiovascular training log",
        },
        created_at=now_dt,
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
        created_at=now_dt,
    )
    session.add(primary_overall)
    session.add(primary_pro)
    session.add(primary_flash)
    session.commit()

    # Seed confirmed schedule anchor T0
    w_service = WeeklyConsiliumService(db=session, now_provider=lambda: anchor_dt)
    w_service.record_schedule_anchor(
        cycle_id=cycle_id,
        anchor_dt=anchor_dt,
        source="post_primary_greeting",
    )


def _build_mock_gateway(call_log: List[Dict[str, Any]]) -> GeminiGateway:
    class MockGateway(GeminiGateway):
        def __init__(self):
            super().__init__()
            self._api_key = "dummy-test-key"

        async def generate_text(self, prompt: str, **kwargs) -> str:
            call_log.append({"prompt": prompt, "method": "generate_text", "kwargs": kwargs})
            return "Mock Gemini reply"

        async def generate_raw_text(self, contents: List[Dict[str, Any]], **kwargs) -> str:
            call_log.append({"contents": contents, "method": "generate_raw_text", "kwargs": kwargs})
            return (
                "Здравствуйте! Как вы себя чувствуете сегодня? "
                "[CLINICAL_REPORT] {\"status\": \"stable\", \"symptoms_mentioned\": [], \"red_flags\": false, \"summary_for_master\": \"Patient reports stable status.\"} [/CLINICAL_REPORT]"
            )

        async def generate_structured(self, prompt: str, response_schema: Any, **kwargs) -> Any:
            call_log.append({
                "prompt": prompt,
                "method": "generate_structured",
                "schema": getattr(response_schema, "__name__", str(response_schema)),
                "kwargs": kwargs,
            })
            if response_schema is AthenaDailyAssessmentModelOutput:
                return AthenaDailyAssessmentModelOutput(
                    status="ALERT",
                    interpretation="Athena Flash synthesis: patient reported morning headache relieved by coffee; biometric monitoring shows stable resting pulse.",
                    observations=["morning_headache", "caffeine_relief"],
                    relevant_changes=["Transient morning headache resolved during Day 1"],
                    contradictions=[],
                    uncertainties=["No sleep stage telemetry recorded for Day 1"],
                    red_flags=False,
                )
            raise RuntimeError("Unexpected structured schema in daily journal test.")
    return MockGateway()


# ==============================================================================
# 1. PATIENT ANSWER PRESERVED VERBATIM
# ==============================================================================

@pytest.mark.anyio
async def test_01_patient_answer_preserved_verbatim(tmp_path: Path):
    """
    Verifies Section A:
    Patient answers are preserved verbatim in PatientAnswerEntry.text.
    Never rewritten, normalized, or paraphrased.
    """
    db_file = tmp_path / "test_01.db"
    engine, SessionFactory = _create_file_db(db_file)
    t0 = datetime(2026, 11, 3, 8, 0, 0, tzinfo=timezone.utc)
    clock = ControlledClock(t0)

    with SessionFactory() as session:
        _seed_completed_cycle(session, "pat_01", "cyc_01", t0)
        athena_calls: List[Dict[str, Any]] = []
        gw = _build_mock_gateway(athena_calls)
        athena_service = AthenaChatService(db=session, gateway=gw, now_provider=clock.now)

        # 1. Day 1: Routine question asked
        await athena_service.handle_turn(
            patient_id="pat_01",
            cycle_id="cyc_01",
            request=AthenaChatRequest(
                message="Start accompaniment",
                event_type="initial_greeting",
                idempotency_key="init_cyc_01",
            ),
        )
        clock.advance(seconds=30)
        q_resp = await athena_service.handle_turn(
            patient_id="pat_01",
            cycle_id="cyc_01",
            request=AthenaChatRequest(
                message="First question",
                event_type="first_planned_question",
                idempotency_key="q1_cyc_01",
            ),
        )

        # 2. Patient answers with colloquial verbatim phrasing
        verbatim_text = "   У меня с утра адски раскалывалась голова, но потом выпил кофе и отпустило!   "
        clock.advance(hours=4)
        await athena_service.handle_turn(
            patient_id="pat_01",
            cycle_id="cyc_01",
            request=AthenaChatRequest(
                message=verbatim_text,
                event_type="routine_answer",
                idempotency_key="ans_cyc_01_d1",
            ),
        )

        # 3. Advance to Day 1 window completion (24h)
        clock.set(t0 + timedelta(hours=24, minutes=5))
        journal_service = AthenaDailyJournalService(db=session, now_provider=clock.now)
        journal = journal_service.build_daily_journal("cyc_01", calendar_week=1, calendar_day=1)

        # Verify Section A verbatim preservation
        pe = journal.patient_reported_evidence
        assert pe.has_patient_interaction is True
        assert len(pe.patient_answers) == 1
        assert pe.patient_answers[0].text == verbatim_text.strip()
        assert pe.routine_question.was_answered is True
        assert pe.routine_question.unanswered_declaration is None
    engine.dispose()


# ==============================================================================
# 2. CONTRADICTION PRESERVED VISIBLE WITHOUT REWRITING
# ==============================================================================

@pytest.mark.anyio
async def test_02_contradiction_between_patient_and_wellness_preserved(tmp_path: Path):
    """
    Verifies Rule 1 & Rule 2:
    Contradictions between patient statements and Wellness findings remain visible
    in AthenaClinicalAssessment.contradictions.
    Raw patient text is NOT normalized to match sensor data, and sensor data is untouched.
    """
    db_file = tmp_path / "test_02.db"
    engine, SessionFactory = _create_file_db(db_file)
    t0 = datetime(2026, 11, 3, 8, 0, 0, tzinfo=timezone.utc)
    clock = ControlledClock(t0)

    with SessionFactory() as session:
        _seed_completed_cycle(session, "pat_02", "cyc_02", t0)
        athena_calls: List[Dict[str, Any]] = []
        gw = _build_mock_gateway(athena_calls)
        athena_service = AthenaChatService(db=session, gateway=gw, now_provider=clock.now)

        # Patient says they did not sleep at all
        await athena_service.handle_turn(
            patient_id="pat_02",
            cycle_id="cyc_02",
            request=AthenaChatRequest(
                message="Start accompaniment",
                event_type="initial_greeting",
                idempotency_key="init_cyc_02",
            ),
        )
        clock.advance(hours=3)
        await athena_service.handle_turn(
            patient_id="pat_02",
            cycle_id="cyc_02",
            request=AthenaChatRequest(
                message="Всю ночь вообще не спал, ужасная бессонница!",
                event_type="routine_answer",
                idempotency_key="ans_cyc_02_d1",
            ),
        )

        # But watch sensor recorded 8.2 hours of sleep
        m_repo = HealthMeasurementRepository(session)
        m_repo.create_batch(
            measurements=[
                {
                    "pulse": 62,
                    "systolic": 118,
                    "diastolic": 76,
                    "spo2": 98,
                    "source": "watch_sensor",
                    "measured_at": t0 + timedelta(hours=6),
                    "raw_metadata": {"sleep_hours": 8.2, "quality": "restorative"},
                }
            ],
            default_cycle_id="cyc_02",
        )

        # Close Day 1
        clock.set(t0 + timedelta(hours=24, minutes=1))
        journal_service = AthenaDailyJournalService(db=session, now_provider=clock.now)
        journal = journal_service.build_daily_journal("cyc_02", calendar_week=1, calendar_day=1)

        # 1. Patient answer remains verbatim
        assert "не спал" in journal.patient_reported_evidence.patient_answers[0].text

        # 2. Wellness recorded actual 8.2 hours
        assert journal.wellness_evidence.biometrics.sleep_summary["sleep_hours"] == 8.2

        # 3. Contradiction is explicitly highlighted in Athena clinical assessment
        aa = journal.athena_clinical_assessment
        assert aa.is_available is True
        assert len(aa.contradictions) >= 1
        assert "8.2 hours" in aa.contradictions[0]
        assert "Contradiction" in aa.contradictions[0]
    engine.dispose()


# ==============================================================================
# 3. EXISTING WELLNESS REPORT & VERSION CORRECTLY REFERENCED
# ==============================================================================

@pytest.mark.anyio
async def test_03_existing_wellness_report_and_version_referenced(tmp_path: Path):
    """
    Verifies Section B:
    When an independent Wellness report exists in model_reports, its report_id,
    version, and assessment are correctly referenced.
    """
    db_file = tmp_path / "test_03.db"
    engine, SessionFactory = _create_file_db(db_file)
    t0 = datetime(2026, 11, 3, 8, 0, 0, tzinfo=timezone.utc)
    clock = ControlledClock(t0)

    with SessionFactory() as session:
        _seed_completed_cycle(session, "pat_03", "cyc_03", t0)

        # Persist independent wellness report in model_reports
        s_repo = WeeklySprintRepository(session)
        s_repo.create(
            sprint_id="sprint_cyc_03_w1",
            cycle_id="cyc_03",
            sprint_number=1,
            status="active",
            commit=True,
        )
        wellness_rep = ModelReport(
            report_id="rep_wellness_cyc_03_d1_v2",
            cycle_id="cyc_03",
            sprint_id="sprint_cyc_03_w1",
            session_id=None,
            model="aegis-wellness-analyzer",
            report_type="wellness_daily_summary",
            privacy_level=PrivacyLevel.SERVER_ONLY.value,
            version=2,
            content={
                "assessment": "Daily physical recovery optimal. Sleep score 88/100.",
                "sleep_hours": 7.5,
                "step_count": 8450,
            },
            created_at=t0 + timedelta(hours=10),
        )
        session.add(wellness_rep)
        session.commit()

        clock.set(t0 + timedelta(hours=24, minutes=1))
        journal_service = AthenaDailyJournalService(db=session, now_provider=clock.now)
        journal = journal_service.build_daily_journal("cyc_03", calendar_week=1, calendar_day=1)

        we = journal.wellness_evidence
        assert we.report_reference.is_available is True
        assert we.report_reference.report_id == "rep_wellness_cyc_03_d1_v2"
        assert we.report_reference.version == 2
        assert we.report_reference.report_type == "wellness_daily_summary"
        assert we.wellness_assessment == "Daily physical recovery optimal. Sleep score 88/100."
    engine.dispose()


# ==============================================================================
# 4. NO RESPONSE BUT AVAILABLE WELLNESS DATA
# ==============================================================================

@pytest.mark.anyio
async def test_04_no_response_but_available_wellness_data(tmp_path: Path):
    """
    Verifies:
    When patient does not reply during the day, but wearable measurements arrive:
    - routine question is marked unanswered with explicit declaration;
    - patient answers is empty;
    - measurements are captured in Section B;
    - does NOT fabricate a patient reply or AI consultation.
    """
    db_file = tmp_path / "test_04.db"
    engine, SessionFactory = _create_file_db(db_file)
    t0 = datetime(2026, 11, 3, 8, 0, 0, tzinfo=timezone.utc)
    clock = ControlledClock(t0)

    with SessionFactory() as session:
        _seed_completed_cycle(session, "pat_04", "cyc_04", t0)
        gw = _build_mock_gateway([])
        athena_service = AthenaChatService(db=session, gateway=gw, now_provider=clock.now)

        # Question formulated on Day 1
        await athena_service.handle_turn(
            patient_id="pat_04",
            cycle_id="cyc_04",
            request=AthenaChatRequest(
                message="Start accompaniment",
                event_type="initial_greeting",
                idempotency_key="init_cyc_04",
            ),
        )
        clock.advance(seconds=30)
        await athena_service.handle_turn(
            patient_id="pat_04",
            cycle_id="cyc_04",
            request=AthenaChatRequest(
                message="First question",
                event_type="first_planned_question",
                idempotency_key="q1_cyc_04",
            ),
        )

        # Patient does NOT respond all day, but smart watch logs pulse & BP
        m_repo = HealthMeasurementRepository(session)
        m_repo.create_batch(
            measurements=[
                {
                    "pulse": 70,
                    "systolic": 122,
                    "diastolic": 78,
                    "spo2": 99,
                    "source": "watch_sensor",
                    "measured_at": t0 + timedelta(hours=14),
                }
            ],
            default_cycle_id="cyc_04",
        )

        # Day 1 ends
        clock.set(t0 + timedelta(hours=24, minutes=1))
        journal_service = AthenaDailyJournalService(db=session, now_provider=clock.now)
        journal = journal_service.build_daily_journal("cyc_04", calendar_week=1, calendar_day=1)

        # Verify evidence
        pe = journal.patient_reported_evidence
        assert pe.has_patient_interaction is False
        assert len(pe.patient_answers) == 0
        assert pe.routine_question.was_answered is False
        assert "unanswered" in pe.routine_question.unanswered_declaration.lower()

        we = journal.wellness_evidence
        assert we.biometrics.raw_records_count == 1
        assert we.biometrics.heart_rate_summary["avg_bpm"] == 70.0
        assert we.biometrics.blood_pressure_summary["avg_systolic"] == 122.0
    engine.dispose()


# ==============================================================================
# 5. NO RESPONSE AND NO MEASUREMENTS
# ==============================================================================

@pytest.mark.anyio
async def test_05_no_response_and_no_measurements(tmp_path: Path):
    """
    Verifies:
    When neither patient interaction nor biometric measurements exist:
    - Explicit declarations for missing data across all sections;
    - Zero fabricated values;
    - Athena assessment honestly states absence reason.
    """
    db_file = tmp_path / "test_05.db"
    engine, SessionFactory = _create_file_db(db_file)
    t0 = datetime(2026, 11, 3, 8, 0, 0, tzinfo=timezone.utc)
    clock = ControlledClock(t0)

    with SessionFactory() as session:
        _seed_completed_cycle(session, "pat_05", "cyc_05", t0)

        # Day 1 closes with zero data
        clock.set(t0 + timedelta(hours=24, minutes=1))
        journal_service = AthenaDailyJournalService(db=session, now_provider=clock.now)
        journal = journal_service.build_daily_journal("cyc_05", calendar_week=1, calendar_day=1)

        assert journal.patient_reported_evidence.has_patient_interaction is False
        assert journal.patient_reported_evidence.patient_answers == []
        assert journal.wellness_evidence.biometrics.raw_records_count == 0
        assert len(journal.wellness_evidence.missing_data_declarations) >= 2
        assert any("biometric" in d.lower() for d in journal.wellness_evidence.missing_data_declarations)

        aa = journal.athena_clinical_assessment
        assert aa.is_available is False
        assert aa.label == "NO_ATHENA_ASSESSMENT_AVAILABLE"
        assert aa.absence_reason is not None
    engine.dispose()


# ==============================================================================
# 6. OUT-OF-TURN MESSAGE STORED SEPARATELY
# ==============================================================================

@pytest.mark.anyio
async def test_06_out_of_turn_message_stored_separately(tmp_path: Path):
    """
    Verifies Rule 2 & Rule 3:
    Spontaneous out-of-turn messages are stored in out_of_turn_messages,
    kept strictly separate from routine answers, and do not close pending question.
    """
    db_file = tmp_path / "test_06.db"
    engine, SessionFactory = _create_file_db(db_file)
    t0 = datetime(2026, 11, 3, 8, 0, 0, tzinfo=timezone.utc)
    clock = ControlledClock(t0)

    with SessionFactory() as session:
        _seed_completed_cycle(session, "pat_06", "cyc_06", t0)
        gw = _build_mock_gateway([])
        athena_service = AthenaChatService(db=session, gateway=gw, now_provider=clock.now)

        # 1. Scheduled question asked
        await athena_service.handle_turn(
            patient_id="pat_06",
            cycle_id="cyc_06",
            request=AthenaChatRequest(
                message="Start accompaniment",
                event_type="initial_greeting",
                idempotency_key="init_cyc_06",
            ),
        )

        # 2. Spontaneous out-of-turn message sent
        clock.advance(hours=5)
        spontaneous_text = "Забыл сказать, что вчера выпил таблетку аспирина."
        await athena_service.handle_turn(
            patient_id="pat_06",
            cycle_id="cyc_06",
            request=AthenaChatRequest(
                message=spontaneous_text,
                event_type="out_of_turn_message",
                idempotency_key="oot_cyc_06",
            ),
        )

        # 3. Close Day 1
        clock.set(t0 + timedelta(hours=24, minutes=1))
        journal_service = AthenaDailyJournalService(db=session, now_provider=clock.now)
        journal = journal_service.build_daily_journal("cyc_06", calendar_week=1, calendar_day=1)

        pe = journal.patient_reported_evidence
        # Out-of-turn message is preserved separately
        assert len(pe.out_of_turn_messages) == 1
        assert pe.out_of_turn_messages[0].text == spontaneous_text
        # Routine question was NOT closed by the out-of-turn message
        assert pe.routine_question.was_answered is False
        assert len(pe.patient_answers) == 0
    engine.dispose()


# ==============================================================================
# 7. HEART-RATE / WEARABLE ALERT STORED AS SEPARATE EXCEPTIONAL EVENT
# ==============================================================================

@pytest.mark.anyio
async def test_07_heart_rate_and_wearable_alert_stored_as_exceptional_event(tmp_path: Path):
    """
    Verifies Section C:
    Wearable alerts and pulse spikes are stored in exceptional_events
    with event_type, source, timestamp, description, and safety tier.
    """
    db_file = tmp_path / "test_07.db"
    engine, SessionFactory = _create_file_db(db_file)
    t0 = datetime(2026, 11, 3, 8, 0, 0, tzinfo=timezone.utc)
    clock = ControlledClock(t0)

    with SessionFactory() as session:
        _seed_completed_cycle(session, "pat_07", "cyc_07", t0)
        gw = _build_mock_gateway([])
        athena_service = AthenaChatService(db=session, gateway=gw, now_provider=clock.now)

        await athena_service.handle_turn(
            patient_id="pat_07",
            cycle_id="cyc_07",
            request=AthenaChatRequest(
                message="Start accompaniment",
                event_type="initial_greeting",
                idempotency_key="init_cyc_07",
            ),
        )

        # Trigger vitals spike alert
        from app.schemas.chat import AthenaVitalsAlertContext
        clock.advance(hours=8)
        await athena_service.handle_turn(
            patient_id="pat_07",
            cycle_id="cyc_07",
            request=AthenaChatRequest(
                message="Пульс подскочил",
                event_type="vitals_alert",
                vitals_alert=AthenaVitalsAlertContext(
                    current_pulse=152,
                    baseline_pulse=70,
                    notes="Sudden resting spike",
                ),
                idempotency_key="alert_cyc_07",
            ),
        )

        # Close Day 1
        clock.set(t0 + timedelta(hours=24, minutes=1))
        journal_service = AthenaDailyJournalService(db=session, now_provider=clock.now)
        journal = journal_service.build_daily_journal("cyc_07", calendar_week=1, calendar_day=1)

        assert len(journal.exceptional_events) >= 1
        ee = journal.exceptional_events[0]
        assert ee.event_type in ("vitals_alert", "emergency_trigger")
        assert "152" in ee.description or "Mode 2" in (ee.safety_tier or "")
        assert ee.source in ("wearable_sensor", "safety_guard")
    engine.dispose()


# ==============================================================================
# 8. ATHENA INTERPRETATION SEPARATED FROM SOURCE EVIDENCE
# ==============================================================================

@pytest.mark.anyio
async def test_08_athena_interpretation_separated_from_source_evidence(tmp_path: Path):
    """
    Verifies Section D:
    Athena's clinical assessment has label ATHENA_GENERATED_INTERPRETATION_NOT_RAW_EVIDENCE
    and is kept strictly separate from raw evidence in Section A, B, and C.
    """
    db_file = tmp_path / "test_08.db"
    engine, SessionFactory = _create_file_db(db_file)
    t0 = datetime(2026, 11, 3, 8, 0, 0, tzinfo=timezone.utc)
    clock = ControlledClock(t0)

    with SessionFactory() as session:
        _seed_completed_cycle(session, "pat_08", "cyc_08", t0)
        w_service = WeeklyConsiliumService(db=session, now_provider=clock.now)

        # Save an Athena clinical log
        w_service.save_athena_clinical_log(
            cycle_id="cyc_08",
            log_entry=AthenaClinicalLogEntry(
                timestamp=t0.isoformat(),
                status="stable",
                symptoms_mentioned=["mild_fatigue"],
                red_flags=False,
                summary_for_master="Patient exhibits mild fatigue; recommendation to monitor hydration.",
            ),
        )

        clock.set(t0 + timedelta(hours=24, minutes=1))
        journal_service = AthenaDailyJournalService(db=session, now_provider=clock.now)
        journal = journal_service.build_daily_journal("cyc_08", calendar_week=1, calendar_day=1)

        aa = journal.athena_clinical_assessment
        assert aa.is_available is True
        assert aa.label == "ATHENA_GENERATED_INTERPRETATION_NOT_RAW_EVIDENCE"
        assert "mild fatigue" in aa.interpretation
        assert "mild_fatigue" in aa.observations
    engine.dispose()


# ==============================================================================
# 9. NO INVENTED AI ASSESSMENT WHEN NONE EXISTS
# ==============================================================================

@pytest.mark.anyio
async def test_09_no_invented_ai_assessment_when_none_exists(tmp_path: Path):
    """
    Verifies:
    If no new Athena clinical log occurred for that day, assessment is marked
    is_available=False, label=NO_ATHENA_ASSESSMENT_AVAILABLE, with honest absence reason.
    A deterministic aggregation never masquerades as an AI clinical conclusion.
    """
    db_file = tmp_path / "test_09.db"
    engine, SessionFactory = _create_file_db(db_file)
    t0 = datetime(2026, 11, 3, 8, 0, 0, tzinfo=timezone.utc)
    clock = ControlledClock(t0)

    with SessionFactory() as session:
        _seed_completed_cycle(session, "pat_09", "cyc_09", t0)
        # Advance without any chat turns or clinical logs
        clock.set(t0 + timedelta(hours=24, minutes=1))
        journal_service = AthenaDailyJournalService(db=session, now_provider=clock.now)
        journal = journal_service.build_daily_journal("cyc_09", calendar_week=1, calendar_day=1)

        aa = journal.athena_clinical_assessment
        assert aa.is_available is False
        assert aa.label == "NO_ATHENA_ASSESSMENT_AVAILABLE"
        assert aa.interpretation is None
        assert aa.observations == []
        assert "No new Athena clinical model evaluation occurred" in aa.absence_reason
    engine.dispose()


# ==============================================================================
# 10. SEVEN JOURNALS REACH THE CORRECT WEEKLY CONSILIUM INPUT
# ==============================================================================

@pytest.mark.anyio
async def test_10_seven_journals_reach_weekly_consilium_input(tmp_path: Path):
    """
    Verifies Rule 6:
    Weekly Consilium receives both Layer 1 (evidence) and Layer 2 (assessment)
    for all seven observation days via format_seven_day_journals_for_consilium.
    """
    db_file = tmp_path / "test_10.db"
    engine, SessionFactory = _create_file_db(db_file)
    t0 = datetime(2026, 11, 3, 8, 0, 0, tzinfo=timezone.utc)
    clock = ControlledClock(t0)

    with SessionFactory() as session:
        _seed_completed_cycle(session, "pat_10", "cyc_10", t0)
        journal_service = AthenaDailyJournalService(db=session, now_provider=clock.now)

        # Create daily journals for Days 1 to 5 (Days 6 and 7 left unrecorded)
        for day in range(1, 6):
            clock.set(t0 + timedelta(days=day, minutes=5))
            j = journal_service.build_daily_journal("cyc_10", calendar_week=1, calendar_day=day)
            journal_service.save_daily_journal("cyc_10", j)

        # Format 7-day diary for Weekly Consilium
        diary_str = journal_service.format_seven_day_journals_for_consilium("cyc_10", sprint_number=1)
        assert diary_str is not None
        assert "WEEK 1, DAYS 1-7" in diary_str
        assert "LAYER 1: ORIGINAL EVIDENCE" in diary_str
        assert "LAYER 2: ATHENA CLINICAL ASSESSMENT" in diary_str

        # Days 1 to 5 present
        for day in range(1, 6):
            assert f"--- DAY {day} ---" in diary_str

        # Days 6 and 7 have explicit honest missing-day declarations
        assert "--- DAY 6 ---" in diary_str
        assert "[MISSING DAY DECLARATION]: Daily journal not recorded for Day 6" in diary_str
        assert "--- DAY 7 ---" in diary_str
        assert "[MISSING DAY DECLARATION]: Daily journal not recorded for Day 7" in diary_str
    engine.dispose()


# ==============================================================================
# 11. DELAYED WEEKLY EXECUTION CANNOT MIX ADJACENT WEEKS
# ==============================================================================

@pytest.mark.anyio
async def test_11_delayed_weekly_execution_cannot_mix_adjacent_weeks(tmp_path: Path):
    """
    Verifies:
    Fixed 168-hour grid boundaries decouple observation periods from execution dates.
    Journals for Week 1 (Days 1..7) strictly belong to Sprint 1; Week 2 (Days 1..7) to Sprint 2.
    """
    db_file = tmp_path / "test_11.db"
    engine, SessionFactory = _create_file_db(db_file)
    t0 = datetime(2026, 11, 3, 8, 0, 0, tzinfo=timezone.utc)
    clock = ControlledClock(t0)

    with SessionFactory() as session:
        _seed_completed_cycle(session, "pat_11", "cyc_11", t0)
        journal_service = AthenaDailyJournalService(db=session, now_provider=clock.now)

        # Build Day 7 of Week 1
        clock.set(t0 + timedelta(days=7, minutes=5))
        j_w1_d7 = journal_service.build_daily_journal("cyc_11", calendar_week=1, calendar_day=7)
        saved_w1 = journal_service.save_daily_journal("cyc_11", j_w1_d7)
        assert saved_w1.sprint_id == "sprint_cyc_11_w1"

        # Build Day 1 of Week 2
        clock.set(t0 + timedelta(days=8, minutes=5))
        j_w2_d1 = journal_service.build_daily_journal("cyc_11", calendar_week=2, calendar_day=1)
        saved_w2 = journal_service.save_daily_journal("cyc_11", j_w2_d1)
        assert saved_w2.sprint_id == "sprint_cyc_11_w2"

        # Verify Sprint 1 list does NOT contain Week 2 Day 1
        sprint1_journals = journal_service.get_seven_day_daily_journals("cyc_11", sprint_number=1)
        assert sprint1_journals[6][1].calendar_week == 1
        assert sprint1_journals[6][1].calendar_day == 7

        sprint2_journals = journal_service.get_seven_day_daily_journals("cyc_11", sprint_number=2)
        assert sprint2_journals[0][1].calendar_week == 2
        assert sprint2_journals[0][1].calendar_day == 1
    engine.dispose()


# ==============================================================================
# 12. MISSED DAYS RECONSTRUCTED AFTER WORKER RESTART
# ==============================================================================

@pytest.mark.anyio
async def test_12_missed_days_reconstructed_after_worker_restart(tmp_path: Path):
    """
    Verifies Rule 4 & Rule 7:
    Server was offline for 3 days; upon restart, ClinicalAutomationWorker
    discovers and reconstructs all 3 missed daily journals from historical evidence.
    """
    db_file = tmp_path / "test_12.db"
    engine, SessionFactory = _create_file_db(db_file)
    t0 = datetime(2026, 11, 3, 8, 0, 0, tzinfo=timezone.utc)
    clock = ControlledClock(t0)

    # 1. Pre-outage: post_primary_init runs at T0
    with SessionFactory() as session:
        _seed_completed_cycle(session, "pat_12", "cyc_12", t0)
        athena_calls: List[Dict[str, Any]] = []
        worker = ClinicalAutomationWorker(
            session_factory=SessionFactory,
            gateway_factory=lambda: _build_mock_gateway(athena_calls),
            now_provider=clock.now,
            enable_daily_journal=True,
        )
        await worker.run_once()

    # 2. Simulate 3-day server downtime (75 hours elapsed)
    clock.advance(hours=75)

    # 3. Server restarts: worker runs with enable_daily_journal=True
    with SessionFactory() as session:
        worker_restart = ClinicalAutomationWorker(
            session_factory=SessionFactory,
            gateway_factory=lambda: _build_mock_gateway([]),
            now_provider=clock.now,
            enable_daily_journal=True,
        )
        processed = await worker_restart.run_once(limit=10)

        # 3 daily journal closure jobs processed (Days 1, 2, 3)
        assert len(processed) == 3
        for job in processed:
            assert job.status == AutomationJobStatus.COMPLETED
            assert job.job_type == AutomationJobType.DAILY_JOURNAL_CLOSURE

        # Verify all 3 journals are durably persisted in SQLite
        j_service = AthenaDailyJournalService(db=session, now_provider=clock.now)
        for day in (1, 2, 3):
            j = j_service.get_daily_journal("cyc_12", calendar_week=1, calendar_day=day)
            assert j is not None
            assert j.calendar_week == 1
            assert j.calendar_day == day
    engine.dispose()


# ==============================================================================
# 13. CONCURRENT WORKER EXECUTION AND IDEMPOTENCY
# ==============================================================================

@pytest.mark.anyio
async def test_13_concurrent_worker_execution_and_idempotency(tmp_path: Path):
    """
    Verifies Rule 7:
    Two parallel workers processing the same daily closure tick:
    SQLite atomic lease claim guarantees only one worker executes closure;
    subsequent calls recover existing results idempotently without duplicate rows.
    """
    db_file = tmp_path / "test_13.db"
    engine, SessionFactory = _create_file_db(db_file)
    t0 = datetime(2026, 11, 3, 8, 0, 0, tzinfo=timezone.utc)
    clock = ControlledClock(t0)

    with SessionFactory() as session:
        _seed_completed_cycle(session, "pat_13", "cyc_13", t0)

    # Advance 25 hours (Day 1 completed)
    clock.advance(hours=25)

    worker1 = ClinicalAutomationWorker(
        session_factory=SessionFactory,
        gateway_factory=lambda: _build_mock_gateway([]),
        now_provider=clock.now,
        enable_daily_journal=True,
    )
    worker2 = ClinicalAutomationWorker(
        session_factory=SessionFactory,
        gateway_factory=lambda: _build_mock_gateway([]),
        now_provider=clock.now,
        enable_daily_journal=True,
    )

    # Run concurrently
    results = await asyncio.gather(worker1.run_once(), worker2.run_once())
    flat_results = [job for sublist in results for job in sublist]
    # At least one worker executed the job
    assert len(flat_results) >= 1

    with SessionFactory() as session:
        journal_service = AthenaDailyJournalService(db=session, now_provider=clock.now)
        journals = journal_service.list_daily_journals("cyc_13")
        # Exactly one daily journal report created (zero duplicate reports)
        assert len(journals) == 1
    engine.dispose()


# ==============================================================================
# 14. PATIENT AND CYCLE ISOLATION
# ==============================================================================

@pytest.mark.anyio
async def test_14_patient_and_cycle_isolation(tmp_path: Path):
    """
    Verifies:
    Patient A's daily journals and measurements never mix into Patient B's records.
    """
    db_file = tmp_path / "test_14.db"
    engine, SessionFactory = _create_file_db(db_file)
    t0 = datetime(2026, 11, 3, 8, 0, 0, tzinfo=timezone.utc)
    clock = ControlledClock(t0)

    with SessionFactory() as session:
        _seed_completed_cycle(session, "pat_A", "cyc_A", t0)
        _seed_completed_cycle(session, "pat_B", "cyc_B", t0)

        # Seed measurement for Patient A only
        m_repo = HealthMeasurementRepository(session)
        m_repo.create_batch(
            measurements=[
                {
                    "pulse": 88,
                    "systolic": 130,
                    "diastolic": 85,
                    "spo2": 97,
                    "source": "watch_sensor",
                    "measured_at": t0 + timedelta(hours=5),
                }
            ],
            default_cycle_id="cyc_A",
        )

        clock.set(t0 + timedelta(hours=24, minutes=5))
        j_service = AthenaDailyJournalService(db=session, now_provider=clock.now)

        j_A = j_service.build_daily_journal("cyc_A", calendar_week=1, calendar_day=1)
        j_B = j_service.build_daily_journal("cyc_B", calendar_week=1, calendar_day=1)

        # Cycle A has 1 measurement point (88 bpm)
        assert j_A.wellness_evidence.biometrics.raw_records_count == 1
        assert j_A.wellness_evidence.biometrics.heart_rate_summary["avg_bpm"] == 88.0

        # Cycle B has 0 measurement points (isolated)
        assert j_B.wellness_evidence.biometrics.raw_records_count == 0
        assert j_B.wellness_evidence.biometrics.heart_rate_summary is None
    engine.dispose()


# ==============================================================================
# 15. INTERNAL JOURNAL CANNOT LEAK THROUGH CLIENT-FACING ENDPOINTS
# ==============================================================================

@pytest.mark.anyio
async def test_15_internal_journal_cannot_leak_to_client(tmp_path: Path):
    """
    Verifies:
    AthenaDailyJournal is strictly SERVER_ONLY and never exposed via
    list_client_facing or client routers.
    """
    db_file = tmp_path / "test_15.db"
    engine, SessionFactory = _create_file_db(db_file)
    t0 = datetime(2026, 11, 3, 8, 0, 0, tzinfo=timezone.utc)
    clock = ControlledClock(t0)

    with SessionFactory() as session:
        _seed_completed_cycle(session, "pat_15", "cyc_15", t0)
        clock.set(t0 + timedelta(hours=24, minutes=5))
        j_service = AthenaDailyJournalService(db=session, now_provider=clock.now)
        journal = j_service.build_daily_journal("cyc_15", calendar_week=1, calendar_day=1)
        saved = j_service.save_daily_journal("cyc_15", journal)

        # Verify privacy_level in DB
        assert saved.privacy_level == PrivacyLevel.SERVER_ONLY.value

        # Verify client-facing query returns 0 daily journals
        repo = ModelReportRepository(session)
        client_reports = repo.list_client_facing("cyc_15")
        for rep in client_reports:
            assert rep.report_type != ATHENA_DAILY_JOURNAL_REPORT_TYPE
            assert rep.privacy_level == PrivacyLevel.CLIENT_FACING.value
    engine.dispose()


# ==============================================================================
# 16. ZERO EXTRA GEMINI CALLS FOR DETERMINISTIC AGGREGATION
# ==============================================================================

@pytest.mark.anyio
async def test_16_zero_gemini_calls_for_daily_journal_aggregation(tmp_path: Path):
    """
    Verifies Strict Limits:
    Daily journal closure is a 100% deterministic local aggregation.
    Makes EXACTLY ZERO Gemini Gateway calls.
    """
    db_file = tmp_path / "test_16.db"
    engine, SessionFactory = _create_file_db(db_file)
    t0 = datetime(2026, 11, 3, 8, 0, 0, tzinfo=timezone.utc)
    clock = ControlledClock(t0)

    gemini_calls: List[Dict[str, Any]] = []
    gw = _build_mock_gateway(gemini_calls)

    with SessionFactory() as session:
        _seed_completed_cycle(session, "pat_16", "cyc_16", t0)
        clock.set(t0 + timedelta(hours=24, minutes=5))

        auto_service = ClinicalAutomationService(
            db=session,
            gateway=gw,
            now_provider=clock.now,
            enable_daily_journal=True,
        )

        job = auto_service.enqueue_daily_journal_closure(
            cycle_id="cyc_16",
            patient_id="pat_16",
            calendar_week=1,
            calendar_day=1,
        )
        assert job is not None

        claimed = auto_service.claim_job(job.job_id)
        assert claimed is not None
        outcome = await auto_service.execute_claimed_job(claimed)
        assert outcome.status == AutomationJobStatus.COMPLETED

        # STRICT ASSERTION: Exactly 0 Gemini calls
        assert len(gemini_calls) == 0
    engine.dispose()



# ==============================================================================
# 17. SUBSTANTIVE EVIDENCE TRIGGERS MOCKED GEMINI FLASH ASSESSMENT
# ==============================================================================

@pytest.mark.anyio
async def test_17_substantive_evidence_triggers_mocked_gemini_flash_assessment(tmp_path: Path):
    """
    Verifies Approved Contract:
    When substantive daily evidence exists on a completed observation day:
    - ClinicalAutomationWorker runs exactly ONE Gemini Flash structured assessment call;
    - Uses GeminiModelRole.FLASH and Athena's approved system instruction (build_athena_system_prompt);
    - Produces AthenaClinicalAssessment with label ATHENA_GENERATED_INTERPRETATION_NOT_RAW_EVIDENCE,
      status, interpretation, observations, relevant_changes, contradictions, uncertainties, red_flags,
      and source_references;
    - Does NOT create a patient-facing chat message or a new routine question, and does NOT alter
      pending-question state.
    """
    db_file = tmp_path / "test_17.db"
    engine, SessionFactory = _create_file_db(db_file)
    t0 = datetime(2026, 11, 3, 8, 0, 0, tzinfo=timezone.utc)
    clock = ControlledClock(t0)

    chat_calls: List[Dict[str, Any]] = []
    chat_gw = _build_mock_gateway(chat_calls)

    with SessionFactory() as session:
        _seed_completed_cycle(session, "pat_17", "cyc_17", t0)
        athena_service = AthenaChatService(db=session, gateway=chat_gw, now_provider=clock.now)

        # 1. Day 1 greeting establishes routine question
        await athena_service.handle_turn(
            patient_id="pat_17",
            cycle_id="cyc_17",
            request=AthenaChatRequest(
                message="Start accompaniment",
                event_type="initial_greeting",
                idempotency_key="init_cyc_17",
            ),
        )
        clock.advance(seconds=30)
        await athena_service.handle_turn(
            patient_id="pat_17",
            cycle_id="cyc_17",
            request=AthenaChatRequest(
                message="First question",
                event_type="first_planned_question",
                idempotency_key="q1_cyc_17",
            ),
        )

        # 2. Spontaneous out-of-turn message + wearable measurement (routine question remains pending!)
        clock.advance(hours=3)
        await athena_service.handle_turn(
            patient_id="pat_17",
            cycle_id="cyc_17",
            request=AthenaChatRequest(
                message="С утра немного болела голова, потом выпил кофе.",
                event_type="out_of_turn_message",
                idempotency_key="oot_cyc_17",
            ),
        )

        m_repo = HealthMeasurementRepository(session)
        m_repo.create_batch(
            measurements=[
                {
                    "pulse": 68,
                    "systolic": 120,
                    "diastolic": 78,
                    "spo2": 98,
                    "source": "watch_sensor",
                    "measured_at": t0 + timedelta(hours=5),
                }
            ],
            default_cycle_id="cyc_17",
        )

        # Snapshot chat history and pending question state BEFORE daily journal closure
        w_service = WeeklyConsiliumService(db=session, now_provider=clock.now)
        history_before = list(w_service.get_athena_chat_history("cyc_17"))
        pending_q_before = athena_service.get_active_routine_question("cyc_17")
        assert pending_q_before is not None
        assert pending_q_before["status"] == "unanswered"

        # 3. Advance past Day 1 window (24h + 5m) and run daily closure via ClinicalAutomationService
        clock.set(t0 + timedelta(hours=24, minutes=5))
        assessment_calls: List[Dict[str, Any]] = []
        assessment_gw = _build_mock_gateway(assessment_calls)

        auto_service = ClinicalAutomationService(
            db=session,
            gateway=assessment_gw,
            now_provider=clock.now,
            enable_daily_journal=True,
        )
        processed = await auto_service.process_due_jobs(discover=True)
        dj_jobs = [j for j in processed if j.job_type == AutomationJobType.DAILY_JOURNAL_CLOSURE]
        assert len(dj_jobs) == 1
        assert dj_jobs[0].status == AutomationJobStatus.COMPLETED
        assert dj_jobs[0].result_metadata["assessment_available"] is True

        # Verify exactly ONE Gemini Flash structured assessment call was made
        assert len(assessment_calls) == 1
        call = assessment_calls[0]
        assert call["method"] == "generate_structured"
        assert call["schema"] == "AthenaDailyAssessmentModelOutput"
        assert call["kwargs"]["model"] == GeminiModelRole.FLASH
        assert "You are Athena (" in call["kwargs"]["system_instruction"]
        assert "Monitor blood pressure and sleep" in call["kwargs"]["system_instruction"]

        # Verify persisted journal Section D
        journal = auto_service.journal_service.get_daily_journal("cyc_17", 1, 1)
        assert journal is not None
        aa = journal.athena_clinical_assessment
        assert aa.is_available is True
        assert aa.label == "ATHENA_GENERATED_INTERPRETATION_NOT_RAW_EVIDENCE"
        assert aa.status == "ALERT"
        assert "Athena Flash synthesis" in aa.interpretation
        assert "morning_headache" in aa.observations
        assert len(aa.relevant_changes) == 1
        assert aa.red_flags is False
        assert aa.provenance_model == GeminiModelRole.FLASH.value
        assert any(ref.startswith("out_of_turn:") for ref in aa.source_references)
        assert any(ref.startswith("biometric:") for ref in aa.source_references)

        # Verify NO patient-facing chat turn was added and pending question state was NOT altered
        history_after = list(w_service.get_athena_chat_history("cyc_17"))
        assert history_after == history_before
        pending_q_after = athena_service.get_active_routine_question("cyc_17")
        assert pending_q_after is not None
        assert pending_q_after["question_id"] == pending_q_before["question_id"]
        assert pending_q_after["status"] == "unanswered"
    engine.dispose()


# ==============================================================================
# 18. EMPTY DAY MAKES NO GEMINI CALL EVEN WITH UNANSWERED GREETING
# ==============================================================================

@pytest.mark.anyio
async def test_18_empty_day_with_unanswered_greeting_makes_no_gemini_call(tmp_path: Path):
    """
    Verifies:
    When a day ends with only an unanswered routine question and zero patient replies,
    zero measurements, zero wellness reports, and zero exceptional events,
    close_daily_journal makes ZERO Gemini calls and persists a missing-data journal.
    """
    db_file = tmp_path / "test_18.db"
    engine, SessionFactory = _create_file_db(db_file)
    t0 = datetime(2026, 11, 3, 8, 0, 0, tzinfo=timezone.utc)
    clock = ControlledClock(t0)

    with SessionFactory() as session:
        _seed_completed_cycle(session, "pat_18", "cyc_18", t0)
        athena_service = AthenaChatService(
            db=session, gateway=_build_mock_gateway([]), now_provider=clock.now
        )
        await athena_service.handle_turn(
            patient_id="pat_18",
            cycle_id="cyc_18",
            request=AthenaChatRequest(
                message="Start accompaniment",
                event_type="initial_greeting",
                idempotency_key="init_cyc_18",
            ),
        )

        # Advance 25 hours with no reply and no biometrics
        clock.set(t0 + timedelta(hours=25))
        daily_calls: List[Dict[str, Any]] = []
        auto_service = ClinicalAutomationService(
            db=session,
            gateway=_build_mock_gateway(daily_calls),
            now_provider=clock.now,
            enable_daily_journal=True,
        )
        processed = await auto_service.process_due_jobs(discover=True)
        dj_jobs = [j for j in processed if j.job_type == AutomationJobType.DAILY_JOURNAL_CLOSURE]
        assert len(dj_jobs) == 1
        assert dj_jobs[0].status == AutomationJobStatus.COMPLETED
        assert len(daily_calls) == 0

        journal = auto_service.journal_service.get_daily_journal("cyc_18", 1, 1)
        assert journal is not None
        assert journal.athena_clinical_assessment.is_available is False
        assert journal.athena_clinical_assessment.label == "NO_ATHENA_ASSESSMENT_AVAILABLE"
    engine.dispose()


# ==============================================================================
# 19. PATIENT WORDING UNCHANGED AND CONTRADICTION VISIBLE AFTER GEMINI ASSESSMENT
# ==============================================================================

@pytest.mark.anyio
async def test_19_patient_wording_unchanged_and_contradiction_visible_after_gemini(tmp_path: Path):
    """
    Verifies:
    Even when Gemini Flash returns an interpretation and contradiction analysis,
    Layer 1 patient verbatim statements and Wellness/biometric records are NEVER modified,
    and contradictions remain visible in both the journal and Weekly Consilium input.
    """
    db_file = tmp_path / "test_19.db"
    engine, SessionFactory = _create_file_db(db_file)
    t0 = datetime(2026, 11, 3, 8, 0, 0, tzinfo=timezone.utc)
    clock = ControlledClock(t0)

    class ContradictionMockGateway(GeminiGateway):
        def __init__(self):
            super().__init__()
            self._api_key = "dummy-test-key"

        async def generate_raw_text(self, contents: List[Dict[str, Any]], **kwargs) -> str:
            return (
                "Поняла вас! Как спалось сегодня? "
                "[CLINICAL_REPORT] {\"status\": \"NORMAL\", \"symptoms_mentioned\": [\"insomnia\"], \"red_flags\": false, \"summary_for_master\": \"Patient claims insomnia.\"} [/CLINICAL_REPORT]"
            )

        async def generate_structured(self, prompt: str, response_schema: Any, **kwargs) -> Any:
            return AthenaDailyAssessmentModelOutput(
                status="ALERT",
                interpretation="Subjective sleep perception diverges from wearable telemetry.",
                observations=["subjective_insomnia"],
                relevant_changes=["Perceived poor sleep quality despite 8.5h sensor sleep duration"],
                contradictions=[
                    "Gemini detected discrepancy: patient stated 'не сомкнул глаз ни на минуту', whereas watch recorded 8.5 hours of sleep."
                ],
                uncertainties=[],
                red_flags=False,
            )

    with SessionFactory() as session:
        _seed_completed_cycle(session, "pat_19", "cyc_19", t0)
        gw = ContradictionMockGateway()
        athena_service = AthenaChatService(db=session, gateway=gw, now_provider=clock.now)

        await athena_service.handle_turn(
            patient_id="pat_19",
            cycle_id="cyc_19",
            request=AthenaChatRequest(
                message="Start accompaniment",
                event_type="initial_greeting",
                idempotency_key="init_cyc_19",
            ),
        )
        verbatim_quote = "Вообще не спал, не сомкнул глаз ни на минуту!"
        clock.advance(hours=2)
        await athena_service.handle_turn(
            patient_id="pat_19",
            cycle_id="cyc_19",
            request=AthenaChatRequest(
                message=verbatim_quote,
                event_type="routine_answer",
                idempotency_key="ans_cyc_19",
            ),
        )

        m_repo = HealthMeasurementRepository(session)
        m_repo.create_batch(
            measurements=[
                {
                    "pulse": 60,
                    "systolic": 116,
                    "diastolic": 74,
                    "spo2": 99,
                    "source": "watch_sensor",
                    "measured_at": t0 + timedelta(hours=4),
                    "raw_metadata": {"sleep_hours": 8.5},
                }
            ],
            default_cycle_id="cyc_19",
        )

        clock.set(t0 + timedelta(hours=24, minutes=5))
        j_service = AthenaDailyJournalService(db=session, now_provider=clock.now, gateway=gw)
        journal, _, _ = await j_service.close_daily_journal("cyc_19", 1, 1)

        # Layer 1 verbatim quote is 100% unchanged
        assert journal.patient_reported_evidence.patient_answers[0].text == verbatim_quote
        assert journal.wellness_evidence.biometrics.sleep_summary["sleep_hours"] == 8.5

        # Layer 2 Gemini assessment + contradiction visible
        aa = journal.athena_clinical_assessment
        assert aa.is_available is True
        assert aa.status == "ALERT"
        assert any("не сомкнул глаз" in c for c in aa.contradictions)
        assert any("8.5 hours" in c for c in aa.contradictions)

        consilium_text = j_service.format_seven_day_journals_for_consilium("cyc_19", 1)
        assert verbatim_quote in consilium_text
        assert "Status: ALERT" in consilium_text
        assert "Gemini detected discrepancy" in consilium_text
    engine.dispose()


# ==============================================================================
# 20. TRANSIENT GEMINI FAILURE PRESERVES LAYER 1 & RETRY SUCCEEDS IDEMPOTENTLY
# ==============================================================================

@pytest.mark.anyio
async def test_20_transient_gemini_failure_preserves_layer1_and_retry_succeeds(tmp_path: Path):
    """
    Verifies Persistence & Recovery:
    - When Gemini Flash fails transiently during daily journal closure:
      1. Layer 1 source evidence is ALREADY durably saved in SQLite;
      2. Section D records explicit assessment-unavailable status (`is_available=False`);
      3. Weekly Consilium formatter exposes Layer 1 evidence + `Status: ASSESSMENT_UNAVAILABLE`;
      4. Automation job enters `RETRYABLE_FAILURE` with bounded backoff;
    - When the worker retries after backoff:
      5. Gemini Flash succeeds and updates Section D in-place without duplicating reports;
      6. Subsequent ticks do not re-call Gemini or publish duplicate assessments.
    """
    from app.services.gemini_gateway import GeminiGatewayError

    db_file = tmp_path / "test_20.db"
    engine, SessionFactory = _create_file_db(db_file)
    t0 = datetime(2026, 11, 3, 8, 0, 0, tzinfo=timezone.utc)
    clock = ControlledClock(t0)

    state = {"should_fail": True, "structured_calls": 0}

    class FlakyAssessmentGateway(GeminiGateway):
        def __init__(self):
            super().__init__()
            self._api_key = "dummy-test-key"

        async def generate_raw_text(self, contents: List[Dict[str, Any]], **kwargs) -> str:
            return (
                "Здравствуйте! Как вы себя чувствуете? "
                "[CLINICAL_REPORT] {\"status\": \"NORMAL\", \"symptoms_mentioned\": [], \"red_flags\": false, \"summary_for_master\": \"Initial greeting.\"} [/CLINICAL_REPORT]"
            )

        async def generate_structured(self, prompt: str, response_schema: Any, **kwargs) -> Any:
            state["structured_calls"] += 1
            if state["should_fail"]:
                raise GeminiGatewayError("503 Service Unavailable: transient Gemini Flash overload")
            return AthenaDailyAssessmentModelOutput(
                status="NORMAL",
                interpretation="Recovered Athena Flash daily assessment after retry.",
                observations=["stable_hemodynamics"],
                relevant_changes=[],
                contradictions=[],
                uncertainties=[],
                red_flags=False,
            )

    with SessionFactory() as session:
        _seed_completed_cycle(session, "pat_20", "cyc_20", t0)
        m_repo = HealthMeasurementRepository(session)
        m_repo.create_batch(
            measurements=[
                {
                    "pulse": 72,
                    "systolic": 121,
                    "diastolic": 79,
                    "spo2": 98,
                    "source": "watch_sensor",
                    "measured_at": t0 + timedelta(hours=6),
                }
            ],
            default_cycle_id="cyc_20",
        )

    # 1. Advance to end of Day 1; first worker tick encounters transient Gemini error
    clock.set(t0 + timedelta(hours=24, minutes=5))
    worker = ClinicalAutomationWorker(
        session_factory=SessionFactory,
        gateway_factory=FlakyAssessmentGateway,
        now_provider=clock.now,
        base_backoff_seconds=30,
        enable_daily_journal=True,
    )
    first_tick = await worker.run_once()
    dj_jobs_1 = [j for j in first_tick if j.job_type == AutomationJobType.DAILY_JOURNAL_CLOSURE]
    assert len(dj_jobs_1) == 1
    assert dj_jobs_1[0].status == AutomationJobStatus.RETRYABLE_FAILURE
    assert dj_jobs_1[0].attempts == 1
    assert state["structured_calls"] == 1

    # Verify Layer 1 evidence is preserved in SQLite and Weekly Consilium sees unavailable status
    with SessionFactory() as session:
        j_service = AthenaDailyJournalService(db=session, now_provider=clock.now)
        persisted_after_fail = j_service.get_daily_journal("cyc_20", 1, 1)
        assert persisted_after_fail is not None
        assert persisted_after_fail.wellness_evidence.biometrics.raw_records_count == 1
        assert persisted_after_fail.wellness_evidence.biometrics.heart_rate_summary["avg_bpm"] == 72.0
        assert persisted_after_fail.athena_clinical_assessment.is_available is False
        assert "unavailable due to AI error" in (
            persisted_after_fail.athena_clinical_assessment.absence_reason or ""
        )

        consilium_during_outage = j_service.format_seven_day_journals_for_consilium("cyc_20", 1)
        assert "Heart rate: min 72, avg 72.0, max 72 bpm" in consilium_during_outage
        assert "Status: ASSESSMENT_UNAVAILABLE" in consilium_during_outage

    # 2. Advance clock past 30s backoff; Gemini recovers
    state["should_fail"] = False
    clock.advance(seconds=35)
    second_tick = await worker.run_once()
    dj_jobs_2 = [j for j in second_tick if j.job_type == AutomationJobType.DAILY_JOURNAL_CLOSURE]
    assert len(dj_jobs_2) == 1
    assert dj_jobs_2[0].status == AutomationJobStatus.COMPLETED
    assert dj_jobs_2[0].attempts == 2
    assert state["structured_calls"] == 2

    # 3. Third tick makes 0 additional Gemini calls and creates 0 duplicate journal rows
    clock.advance(seconds=60)
    third_tick = await worker.run_once()
    assert third_tick == []
    assert state["structured_calls"] == 2

    with SessionFactory() as session:
        j_service = AthenaDailyJournalService(db=session, now_provider=clock.now)
        journals = j_service.list_daily_journals("cyc_20")
        assert len(journals) == 1
        final_journal = journals[0]
        assert final_journal.wellness_evidence.biometrics.raw_records_count == 1
        assert final_journal.athena_clinical_assessment.is_available is True
        assert final_journal.athena_clinical_assessment.status == "NORMAL"
        assert (
            final_journal.athena_clinical_assessment.interpretation
            == "Recovered Athena Flash daily assessment after retry."
        )

        consilium_after_recovery = j_service.format_seven_day_journals_for_consilium("cyc_20", 1)
        assert "Status: NORMAL" in consilium_after_recovery
        assert "Recovered Athena Flash daily assessment after retry." in consilium_after_recovery
    engine.dispose()
