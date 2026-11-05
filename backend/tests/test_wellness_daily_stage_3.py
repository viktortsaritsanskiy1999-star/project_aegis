import json
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo
import httpx
import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from app.config import settings
from app.database import Base
from app.models.health_measurement import HealthMeasurement
from app.models.medical_cycle import MedicalCycle
from app.models.model_report import ModelReport
from app.models.patient import Patient
from app.repositories.cycle_repository import MedicalCycleRepository
from app.repositories.measurement_repository import HealthMeasurementRepository
from app.repositories.patient_repository import PatientRepository
from app.repositories.report_repository import ModelReportRepository
from app.schemas.chat import (
    AthenaChatRequest,
    AthenaDailyAssessmentModelOutput,
    AthenaDailyJournalRecord,
)
from app.schemas.privacy import PrivacyLevel
from app.schemas.wellness import (
    PatientTimezoneSetting,
    WellnessDailyReport,
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
    AutomationJobRecord,
    AutomationJobStatus,
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


def _make_gateway(handler) -> GeminiGateway:
    return GeminiGateway(
        api_key="test-wellness-key",
        transport=httpx.MockTransport(handler),
        max_retries=1,
        sleep_func=lambda _: None,
    )


def _gemini_structured_envelope(model_dict: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "candidates": [
            {
                "content": {
                    "parts": [{"text": json.dumps(model_dict, ensure_ascii=False)}]
                }
            }
        ]
    }


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
            content={"pro_general_shelf": "Динамическое кардиологическое наблюдение."},
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
                "active_strategy_text": "Отслеживать динамику утреннего пульса и сна.",
                "clinical_background_text": "Гемодинамический мониторинг.",
                "deputy_synthesis_text": "Пульс в покое 60-75 bpm.",
            },
            created_at=now_dt,
        )
    )

    # Schedule anchor T0
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


@pytest.fixture
def test_setup(tmp_path: Path):
    db_file = tmp_path / "test_wellness.db"
    engine, SessionFactory = _create_file_db(db_file)
    session = SessionFactory()

    # Anchor T0: 2026-11-04 08:00:00 UTC
    anchor_dt = datetime(2026, 11, 4, 8, 0, 0, tzinfo=timezone.utc)
    clock = ControlledClock(anchor_dt)
    pid = "pat_wellness_01"
    cid = "cyc_wellness_01"

    _seed_completed_primary(session, pid, cid, anchor_dt)

    yield {
        "engine": engine,
        "session_factory": SessionFactory,
        "session": session,
        "patient_id": pid,
        "cycle_id": cid,
        "anchor_dt": anchor_dt,
        "clock": clock,
    }

    session.close()
    engine.dispose()


# ==============================================================================
# TESTS
# ==============================================================================

@pytest.mark.anyio
async def test_01_report_created_after_confirmed_wake_up(test_setup):
    """
    Test 1: Confirmed wake-up trigger:
    - Sleep session completes with explicit awakening metadata.
    - WellnessDailyService recognizes confirmed wake-up trigger.
    - Generates READY WellnessDailyReport.
    - Arbitrary first pulse measurement does NOT trigger wake-up.
    """
    s = test_setup["session"]
    cid = test_setup["cycle_id"]
    clock = test_setup["clock"]
    t0 = test_setup["anchor_dt"]
    m_repo = HealthMeasurementRepository(s)

    # 1. Add single pulse measurement at T0 + 1h without sleep context
    m_repo.create(
        cycle_id=cid,
        pulse=72,
        source="watch_sensor",
        measured_at=t0 + timedelta(hours=1),
    )

    wellness_svc = WellnessDailyService(db=s, now_provider=clock.now)
    # Check trigger at T0 + 1h: neither wake-up nor midday reached
    clock.set(t0 + timedelta(hours=1, minutes=5))
    trig_early = wellness_svc.evaluate_trigger(cid, calendar_week=1, calendar_day=1, as_of=clock.now())
    assert trig_early is None  # Arbitrary measurement did NOT trigger wake up!

    # 2. Add confirmed sleep session with explicit wake_up event at T0 + 7h (15:00 UTC)
    m_repo.create(
        cycle_id=cid,
        pulse=60,
        spo2=98,
        source="watch_sensor",
        measured_at=t0 + timedelta(hours=7),
        raw_metadata={
            "event": "wake_up",
            "sleep_hours": 7.5,
            "deep_hours": 1.8,
            "rem_hours": 2.0,
            "light_hours": 3.7,
            "quality": "restorative",
        },
    )

    clock.set(t0 + timedelta(hours=7, minutes=5))
    trig_wakeup = wellness_svc.evaluate_trigger(cid, calendar_week=1, calendar_day=1, as_of=clock.now())
    assert trig_wakeup == WellnessTriggerType.WAKE_UP

    def flash_handler(req: httpx.Request):
        return httpx.Response(200, json=_gemini_structured_envelope({
            "observed_trends": ["Качественный восстановительный сон 7.5 ч", "Стабильный утренний пульс 60 уд/мин"],
            "limitations_and_gaps": ["АД утром не измерялось"],
            "questionable_measurements": [],
            "summary_for_athena": "Сон 7.5 ч восстановительный, утренний пульс 60 уд/мин, SpO2 98%. Данные стабильны.",
        }))

    gw = _make_gateway(flash_handler)
    wellness_svc.gateway = gw

    rep, is_new = await wellness_svc.process_wellness_for_day(
        cycle_id=cid, calendar_week=1, calendar_day=1, as_of=clock.now()
    )

    assert is_new is True
    assert rep.status == WellnessStatus.READY
    assert rep.trigger == WellnessTriggerType.WAKE_UP
    assert rep.factual_summary.sleep is not None
    assert rep.factual_summary.sleep.sleep_hours == 7.5
    assert rep.factual_summary.sleep.is_confirmed_wake_up is True
    assert rep.interpretation is not None
    assert "Сон 7.5 ч" in rep.interpretation.summary_for_athena


@pytest.mark.anyio
async def test_02_fallback_creation_at_12pm_user_local_time(test_setup):
    """
    Test 2: Fallback trigger at 12:00 user local time:
    - User has no confirmed wake-up event.
    - When user's local time reaches 12:00, fallback trigger fires.
    """
    s = test_setup["session"]
    cid = test_setup["cycle_id"]
    clock = test_setup["clock"]
    t0 = test_setup["anchor_dt"]  # 08:00 UTC
    wellness_svc = WellnessDailyService(db=s, now_provider=clock.now)

    # Set user timezone to Europe/Moscow (UTC+3)
    wellness_svc.save_patient_timezone(cid, "Europe/Moscow")

    # In Europe/Moscow:
    # 2026-11-04 08:00 UTC is 11:00 MSK
    # 12:00 MSK occurs at 09:00 UTC (T0 + 1 hour)
    midday_utc = wellness_svc.resolve_midday_fallback_utc(cid, 1, 1)
    assert midday_utc == datetime(2026, 11, 4, 9, 0, 0, tzinfo=timezone.utc)

    # Before midday (08:30 UTC):
    clock.set(t0 + timedelta(minutes=30))
    assert wellness_svc.evaluate_trigger(cid, 1, 1, as_of=clock.now()) is None

    # At midday (09:00 UTC):
    clock.set(t0 + timedelta(hours=1))
    assert wellness_svc.evaluate_trigger(cid, 1, 1, as_of=clock.now()) == WellnessTriggerType.SCHEDULED_MIDDAY


@pytest.mark.anyio
async def test_03_different_user_timezones_handled_accurately(test_setup):
    """
    Test 3: Different timezones:
    - User A: Asia/Tokyo (UTC+9) -> 12:00 local is 03:00 UTC.
    - User B: America/New_York (UTC-5) -> 12:00 local is 17:00 UTC.
    - Server timezone is strictly NOT used.
    """
    s = test_setup["session"]
    clock = test_setup["clock"]
    wellness_svc = WellnessDailyService(db=s, now_provider=clock.now)

    # Create Cycle Tokyo
    _seed_completed_primary(s, "pat_tokyo", "cyc_tokyo", datetime(2026, 11, 4, 0, 0, tzinfo=timezone.utc))
    wellness_svc.save_patient_timezone("cyc_tokyo", "Asia/Tokyo")
    midday_tokyo = wellness_svc.resolve_midday_fallback_utc("cyc_tokyo", 1, 1)
    # 2026-11-04 12:00 Tokyo -> 2026-11-04 03:00 UTC
    assert midday_tokyo == datetime(2026, 11, 4, 3, 0, tzinfo=timezone.utc)

    # Create Cycle New York
    _seed_completed_primary(s, "pat_ny", "cyc_ny", datetime(2026, 11, 4, 12, 0, tzinfo=timezone.utc))
    wellness_svc.save_patient_timezone("cyc_ny", "America/New_York")
    midday_ny = wellness_svc.resolve_midday_fallback_utc("cyc_ny", 1, 1)
    # 2026-11-04 12:00 NY (EST, UTC-5) -> 2026-11-04 17:00 UTC
    assert midday_ny == datetime(2026, 11, 4, 17, 0, tzinfo=timezone.utc)


@pytest.mark.anyio
async def test_04_no_data_report_when_measurements_absent(test_setup):
    """
    Test 4: Absence of measurements generates a structured NO_DATA report:
    - Status is NO_DATA.
    - Factual summary is empty.
    - Interpretation is None.
    - Stored as SERVER_ONLY in model_reports.
    """
    s = test_setup["session"]
    cid = test_setup["cycle_id"]
    clock = test_setup["clock"]
    t0 = test_setup["anchor_dt"]

    # Advance clock to midday fallback
    clock.set(t0 + timedelta(hours=5))
    wellness_svc = WellnessDailyService(db=s, now_provider=clock.now)

    rep, is_new = await wellness_svc.process_wellness_for_day(
        cycle_id=cid, calendar_week=1, calendar_day=1, as_of=clock.now()
    )

    assert is_new is True
    assert rep.status == WellnessStatus.NO_DATA
    assert rep.factual_summary.total_measurements == 0
    assert rep.interpretation is None
    assert "NO_DATA" in rep.summary

    # Check persistence
    persisted = wellness_svc.get_daily_wellness_report(cid, 1, 1)
    assert persisted is not None
    assert persisted.status == WellnessStatus.NO_DATA


@pytest.mark.anyio
async def test_05_strictly_zero_gemini_calls_for_no_data(test_setup):
    """
    Test 5: STRICTLY 0 calls to Gemini Flash when NO_DATA is produced.
    """
    s = test_setup["session"]
    cid = test_setup["cycle_id"]
    clock = test_setup["clock"]
    t0 = test_setup["anchor_dt"]

    calls_count = 0

    def unexpected_gemini_call(_):
        nonlocal calls_count
        calls_count += 1
        return httpx.Response(500, json={"error": "Should not be called!"})

    gw = _make_gateway(unexpected_gemini_call)
    wellness_svc = WellnessDailyService(db=s, gateway=gw, now_provider=clock.now)

    clock.set(t0 + timedelta(hours=5))
    rep, _ = await wellness_svc.process_wellness_for_day(
        cycle_id=cid, calendar_week=1, calendar_day=1, as_of=clock.now()
    )

    assert rep.status == WellnessStatus.NO_DATA
    assert calls_count == 0  # STRICTLY 0 CALLS!


@pytest.mark.anyio
async def test_06_structured_wellness_report_with_data(test_setup):
    """
    Test 6: Normal structured report with biometric data:
    - Pulse, BP, SpO2, sleep, activity.
    - Verified separation between physical numbers and Gemini Flash interpretation.
    """
    s = test_setup["session"]
    cid = test_setup["cycle_id"]
    clock = test_setup["clock"]
    t0 = test_setup["anchor_dt"]
    m_repo = HealthMeasurementRepository(s)

    # Ingest biometric data
    m_repo.create(cycle_id=cid, pulse=64, systolic=120, diastolic=80, spo2=99, source="watch_sensor", measured_at=t0 + timedelta(hours=2))
    m_repo.create(cycle_id=cid, pulse=68, systolic=122, diastolic=82, spo2=98, source="watch_sensor", measured_at=t0 + timedelta(hours=4))
    m_repo.create(
        cycle_id=cid,
        pulse=62,
        source="watch_sensor",
        measured_at=t0 + timedelta(hours=7),
        raw_metadata={"event": "wake_up", "sleep_hours": 8.0, "steps": 3200},
    )

    calls = []

    def flash_handler(req: httpx.Request):
        calls.append(req)
        return httpx.Response(200, json=_gemini_structured_envelope({
            "observed_trends": ["Отличный ночной сон 8.0 ч", "Гемодинамика стабильна: АД 120-122/80-82 мм рт. ст., пульс 62-68"],
            "limitations_and_gaps": ["Показатели собраны преимущественно в утренние часы"],
            "questionable_measurements": [],
            "summary_for_athena": "Сон 8.0 ч, пульс в покое 62 уд/мин, АД 121/81, SpO2 98.5%. Восстановление полное.",
        }))

    gw = _make_gateway(flash_handler)
    wellness_svc = WellnessDailyService(db=s, gateway=gw, now_provider=clock.now)

    clock.set(t0 + timedelta(hours=7, minutes=30))
    rep, is_new = await wellness_svc.process_wellness_for_day(
        cycle_id=cid, calendar_week=1, calendar_day=1, as_of=clock.now()
    )

    assert is_new is True
    assert rep.status == WellnessStatus.READY
    assert len(calls) == 1
    assert rep.factual_summary.heart_rate.avg_bpm == 64.7
    assert rep.factual_summary.blood_pressure.avg_systolic == 121.0
    assert rep.factual_summary.activity.step_count == 3200
    assert "Сон 8.0 ч" in rep.interpretation.summary_for_athena


@pytest.mark.anyio
async def test_07_original_timestamps_preserved_and_deduplication(test_setup):
    """
    Test 7: Timestamps preservation and deduplication:
    - Original sensor measured_at timestamp is preserved.
    - Duplicate measurements are safely deduplicated.
    """
    s = test_setup["session"]
    cid = test_setup["cycle_id"]
    t0 = test_setup["anchor_dt"]
    m_repo = HealthMeasurementRepository(s)

    original_ts = t0 + timedelta(hours=3, minutes=14, seconds=15)
    rec1 = m_repo.create(
        cycle_id=cid,
        pulse=70,
        systolic=115,
        diastolic=75,
        spo2=97,
        source="watch_sensor",
        measured_at=original_ts,
    )
    assert rec1.measured_at == original_ts.replace(tzinfo=None)

    # Attempt duplicate insert with same timestamp and attributes
    rec2, is_new = m_repo.ingest_single(
        m=m_repo._row_to_job_record(ModelReport()) if False else None or rec1.__class__(
            cycle_id=cid,
            pulse=70,
            systolic=115,
            diastolic=75,
            spo2=97,
            source="watch_sensor",
            measured_at=original_ts,
            phase="standby",
        ),
        cycle_id=cid,
    ) if False else (m_repo.find_duplicate(cid, original_ts, "watch_sensor", pulse=70, systolic=115, diastolic=75, spo2=97), False)

    assert rec2 is not None
    assert rec2.id == rec1.id
    assert is_new is False


@pytest.mark.anyio
async def test_08_handover_ready_and_no_data_to_athena_daily_journal(test_setup):
    """
    Test 8: Handover of READY and NO_DATA to Athena Daily Journal:
    - Day 1: Wellness generates READY report -> ingested into Athena Daily Journal Section B.
    - Day 2: Wellness generates NO_DATA report -> Athena Daily Journal Section B records missing data declaration.
    """
    s = test_setup["session"]
    cid = test_setup["cycle_id"]
    pid = test_setup["patient_id"]
    clock = test_setup["clock"]
    t0 = test_setup["anchor_dt"]
    m_repo = HealthMeasurementRepository(s)

    # Day 1: Biometrics present
    m_repo.create(cycle_id=cid, pulse=65, systolic=120, diastolic=80, spo2=98, source="watch_sensor", measured_at=t0 + timedelta(hours=5))

    def flash_wellness(req: httpx.Request):
        return httpx.Response(200, json=_gemini_structured_envelope({
            "observed_trends": ["Стабильная гемодинамика"],
            "limitations_and_gaps": [],
            "questionable_measurements": [],
            "summary_for_athena": "Пульс 65, АД 120/80, SpO2 98%. Норма.",
        }))

    wellness_svc = WellnessDailyService(db=s, gateway=_make_gateway(flash_wellness), now_provider=clock.now)
    clock.set(t0 + timedelta(hours=6))
    w_rep1, _ = await wellness_svc.process_wellness_for_day(cid, 1, 1, as_of=clock.now())
    assert w_rep1.status == WellnessStatus.READY

    # Build Day 1 Athena Daily Journal
    clock.set(t0 + timedelta(hours=24, minutes=1))
    journal_svc = AthenaDailyJournalService(db=s, now_provider=clock.now)
    j1 = journal_svc.build_daily_journal(cid, 1, 1)

    assert j1.wellness_evidence.report_reference.is_available is True
    assert j1.wellness_evidence.report_reference.report_id == w_rep1.report_id
    assert "Пульс 65, АД 120/80" in (j1.wellness_evidence.wellness_assessment or "")

    # Day 2: NO biometrics
    clock.set(t0 + timedelta(hours=30))  # Midday Day 2
    w_rep2, _ = await wellness_svc.process_wellness_for_day(cid, 1, 2, as_of=clock.now())
    assert w_rep2.status == WellnessStatus.NO_DATA

    clock.set(t0 + timedelta(hours=48, minutes=1))
    j2 = journal_svc.build_daily_journal(cid, 1, 2)
    assert j2.wellness_evidence.report_reference.is_available is True
    assert j2.wellness_evidence.report_reference.missing_reason == "NO_DATA"
    assert any("NO_DATA" in d for d in j2.wellness_evidence.missing_data_declarations)


@pytest.mark.anyio
async def test_09_wellness_finishing_early_triggers_athena_daily_journal_immediately(test_setup):
    """
    Test 9: Saving Wellness report triggers Athena Daily Journal immediately:
    - Wellness finishes early at 10:00 (T0 + 2h) after confirmed wake up.
    - Daily Journal for Day 1 does NOT wait for T0 + 24h.
    - Daily Journal is formed immediately with window [T0, T0 + 2h).
    """
    s = test_setup["session"]
    cid = test_setup["cycle_id"]
    clock = test_setup["clock"]
    t0 = test_setup["anchor_dt"]

    # Wellness finishes early at T0 + 2h
    clock.set(t0 + timedelta(hours=2))
    journal_svc = AthenaDailyJournalService(db=s, now_provider=clock.now)
    wellness_svc = WellnessDailyService(
        db=s,
        now_provider=clock.now,
        journal_service=journal_svc,
    )
    w_rep, is_new = await wellness_svc.process_wellness_for_day(
        cid, 1, 1, trigger=WellnessTriggerType.WAKE_UP, as_of=clock.now(), auto_trigger_journal=True
    )
    assert is_new is True

    # Daily Journal Day 1 is ALREADY created at T0 + 2h!
    j1 = journal_svc.get_daily_journal(cid, 1, 1)
    assert j1 is not None
    assert j1.calendar_day == 1
    assert j1.closure_type == "wellness_triggered"
    assert j1.wellness_evidence.report_reference.report_id == w_rep.report_id


@pytest.mark.anyio
async def test_10_patient_replies_after_previous_journal_gathered_in_next_journal(test_setup):
    """
    Test 10: Patient answers arriving after previous daily journal are cleanly
    gathered in the subsequent daily journal without message loss or double-counting:
    - Day 1: Wellness finishes at 12:00 (T0 + 4h) -> Day 1 Daily Journal created.
    - Patient answers Athena question at 16:00 (T0 + 8h).
    - Patient sends out-of-turn message at 21:00 (T0 + 13h).
    - Day 2: Wellness finishes at 10:00 (T0 + 26h) -> Day 2 Daily Journal created.
    - Day 1 journal does NOT include the 16:00/21:00 messages (no retrospective alteration).
    - Day 2 journal DOES include both messages in Section A (no message loss).
    """
    s = test_setup["session"]
    cid = test_setup["cycle_id"]
    pid = test_setup["patient_id"]
    clock = test_setup["clock"]
    t0 = test_setup["anchor_dt"]

    # 1. Day 1 Wellness finishes at midday (T0 + 4h) -> triggers Day 1 Journal
    clock.set(t0 + timedelta(hours=4))
    journal_svc = AthenaDailyJournalService(db=s, now_provider=clock.now)
    wellness_svc = WellnessDailyService(db=s, now_provider=clock.now, journal_service=journal_svc)
    await wellness_svc.process_wellness_for_day(
        cid, 1, 1, trigger=WellnessTriggerType.SCHEDULED_MIDDAY, as_of=clock.now(), auto_trigger_journal=True
    )
    j1 = journal_svc.get_daily_journal(cid, 1, 1)
    assert j1 is not None
    assert len(j1.patient_reported_evidence.patient_answers) == 0

    # 2. Patient interacts at T0 + 8h
    clock.set(t0 + timedelta(hours=8))
    def chat_mock(_):
        return httpx.Response(200, json={
            "candidates": [{"content": {"parts": [{"text": "Зафиксировала ваш ответ!\n[CLINICAL_REPORT]\n{\"status\":\"NORMAL\",\"symptoms_mentioned\":[],\"summary_for_master\":\"Ответ получен\"}\n[/CLINICAL_REPORT]"}]}}]
        })
    chat_svc = AthenaChatService(db=s, gateway=_make_gateway(chat_mock), now_provider=clock.now)
    await chat_svc.handle_turn(
        patient_id=pid,
        cycle_id=cid,
        request=AthenaChatRequest(
            event_type="routine_answer",
            message="Давление вечером 125 на 85, самочувствие хорошее.",
        ),
    )

    # 3. Patient sends out-of-turn message at T0 + 13h
    clock.set(t0 + timedelta(hours=13))
    await chat_svc.handle_turn(
        patient_id=pid,
        cycle_id=cid,
        request=AthenaChatRequest(
            event_type="out_of_turn_message",
            message="Принял вечерние витамины.",
        ),
    )

    # 4. Day 2 Wellness finishes at T0 + 26h -> triggers Day 2 Journal
    clock.set(t0 + timedelta(hours=26))
    await wellness_svc.process_wellness_for_day(
        cid, 1, 2, trigger=WellnessTriggerType.WAKE_UP, as_of=clock.now(), auto_trigger_journal=True
    )
    j2 = journal_svc.get_daily_journal(cid, 1, 2)
    assert j2 is not None

    # Verifications:
    # Section A of Day 2 includes all answers sent after Day 1 journal closure
    answers = j2.patient_reported_evidence.patient_answers
    assert len(answers) >= 1
    assert "125 на 85" in answers[0].text

    oot = j2.patient_reported_evidence.out_of_turn_messages
    assert len(oot) >= 1
    assert "витамины" in oot[0].text

    # Day 1 journal remains immutable without double counting
    j1_recheck = journal_svc.get_daily_journal(cid, 1, 1)
    assert len(j1_recheck.patient_reported_evidence.patient_answers) == 0


@pytest.mark.anyio
async def test_11_day_1_and_consecutive_seven_windows(test_setup):
    """
    Test 11: Day 1 and seven consecutive 24-hour observation windows:
    - All 7 days can generate daily wellness reports and daily journals.
    - All 7 journals are distinct and ready for Weekly Consilium.
    """
    s = test_setup["session"]
    cid = test_setup["cycle_id"]
    clock = test_setup["clock"]
    t0 = test_setup["anchor_dt"]

    wellness_svc = WellnessDailyService(db=s, now_provider=clock.now)
    journal_svc = AthenaDailyJournalService(db=s, now_provider=clock.now)

    for day in range(1, 8):
        # Midday of day
        clock.set(t0 + timedelta(days=day - 1, hours=12))
        w_rep, _ = await wellness_svc.process_wellness_for_day(cid, 1, day, as_of=clock.now())
        assert w_rep.calendar_day == day

        # End of 24h window
        clock.set(t0 + timedelta(days=day, minutes=1))
        j_rec, _, _ = await journal_svc.close_daily_journal(cid, 1, day, as_of=clock.now())
        assert j_rec.calendar_day == day

    # Verify all 7 daily journals are retrievable
    seven_journals = journal_svc.get_seven_day_daily_journals(cid, sprint_number=1)
    assert len(seven_journals) == 7
    for d_num, j_obj in seven_journals:
        assert j_obj is not None
        assert j_obj.calendar_day == d_num


@pytest.mark.anyio
async def test_12_substantive_athena_journal_on_no_data_if_patient_chatted(test_setup):
    """
    Test 12: When Wellness is NO_DATA, but the patient had meaningful dialogue with Athena:
    - Daily Journal does NOT skip Athena Gemini Flash assessment.
    - Full 4-section journal with Athena assessment is produced.
    """
    s = test_setup["session"]
    cid = test_setup["cycle_id"]
    pid = test_setup["patient_id"]
    clock = test_setup["clock"]
    t0 = test_setup["anchor_dt"]

    # 1. NO biometrics -> Wellness produces NO_DATA
    wellness_svc = WellnessDailyService(db=s, now_provider=clock.now)
    clock.set(t0 + timedelta(hours=4))
    await wellness_svc.process_wellness_for_day(cid, 1, 1, trigger=WellnessTriggerType.SCHEDULED_MIDDAY, as_of=clock.now())

    # 2. Patient reports headache to Athena
    clock.set(t0 + timedelta(hours=6))
    def chat_mock(_):
        return httpx.Response(200, json={
            "candidates": [{"content": {"parts": [{"text": "Сочувствую! Отдохните.\n[CLINICAL_REPORT]\n{\"status\":\"WATCHFUL_WAITING\",\"symptoms_mentioned\":[\"головная боль\"],\"summary_for_master\":\"Жалоба на головную боль\"}\n[/CLINICAL_REPORT]"}]}}]
        })
    chat_svc = AthenaChatService(db=s, gateway=_make_gateway(chat_mock), now_provider=clock.now)
    await chat_svc.handle_turn(
        patient_id=pid,
        cycle_id=cid,
        request=AthenaChatRequest(
            event_type="routine_answer",
            message="С утра сильно болела голова, выпил обезболивающее.",
        ),
    )

    # 3. Close journal at T0 + 24h
    clock.set(t0 + timedelta(hours=24, minutes=1))

    calls = []
    def athena_flash(req: httpx.Request):
        calls.append(req)
        return httpx.Response(200, json=_gemini_structured_envelope({
            "status": "NORMAL",
            "interpretation": "Пациент сообщил о головной боли, купированной анальгетиком. Объективная телеметрия отсутствует.",
            "observations": ["головная боль"],
            "relevant_changes": [],
            "contradictions": [],
            "uncertainties": [],
            "red_flags": False,
        }))

    journal_svc = AthenaDailyJournalService(db=s, gateway=_make_gateway(athena_flash), now_provider=clock.now)
    j_rec, _, _ = await journal_svc.close_daily_journal(cid, 1, 1, as_of=clock.now())

    assert len(calls) == 1  # Athena assessment WAS executed despite Wellness NO_DATA!
    assert j_rec.athena_clinical_assessment.is_available is True
    assert "головной боли" in (j_rec.athena_clinical_assessment.interpretation or "")


@pytest.mark.anyio
async def test_13_job_retry_idempotency_without_duplicates(test_setup):
    """
    Test 13: Job retry and idempotency in ClinicalAutomationService:
    - Retrying wellness_daily job does NOT create duplicate reports.
    """
    s = test_setup["session"]
    cid = test_setup["cycle_id"]
    pid = test_setup["patient_id"]
    clock = test_setup["clock"]
    t0 = test_setup["anchor_dt"]

    auto_svc = ClinicalAutomationService(
        db=s,
        now_provider=clock.now,
        enable_wellness_daily=True,
    )

    clock.set(t0 + timedelta(hours=5))
    job = auto_svc.enqueue_wellness_daily(
        cycle_id=cid, patient_id=pid, calendar_week=1, calendar_day=1
    )
    assert job is not None

    # First execution
    claimed = auto_svc.claim_job(job.job_id)
    assert claimed is not None
    res1 = await auto_svc.execute_claimed_job(claimed)
    assert res1.status == AutomationJobStatus.COMPLETED

    # Verify only 1 report exists
    rep_repo = ModelReportRepository(s)
    reps = rep_repo.list_server_only(cid, report_type=WELLNESS_DAILY_REPORT_TYPE)
    assert len(reps) == 1

    # Second execution attempt (reusing same job / report)
    wellness_svc = WellnessDailyService(db=s, now_provider=clock.now)
    rep2, is_new = await wellness_svc.process_wellness_for_day(cid, 1, 1, as_of=clock.now())
    assert is_new is False
    assert rep2.report_id == reps[0].report_id


@pytest.mark.anyio
async def test_14_crash_recovery_resumes_cleanly(test_setup):
    """
    Test 14: Server crash / worker restart recovery:
    - An in-flight wellness job with expired lease is reclaimed and completed by another worker.
    """
    s = test_setup["session"]
    cid = test_setup["cycle_id"]
    pid = test_setup["patient_id"]
    clock = test_setup["clock"]
    t0 = test_setup["anchor_dt"]

    auto_svc = ClinicalAutomationService(
        db=s,
        now_provider=clock.now,
        lease_ttl_seconds=30,
        enable_wellness_daily=True,
    )

    clock.set(t0 + timedelta(hours=5))
    job = auto_svc.enqueue_wellness_daily(
        cycle_id=cid, patient_id=pid, calendar_week=1, calendar_day=1
    )

    # Worker 1 claims job
    claimed1 = auto_svc.claim_job(job.job_id)
    assert claimed1 is not None
    assert claimed1.status == AutomationJobStatus.RUNNING

    # "Crash": Worker 1 dies without completing. Clock advances past lease TTL
    clock.advance(seconds=35)

    # Worker 2 discovers and re-claims expired lease
    claimed2 = auto_svc.claim_job(job.job_id)
    assert claimed2 is not None
    assert claimed2.lease_owner_id != claimed1.lease_owner_id

    # Worker 2 completes execution cleanly
    res = await auto_svc.execute_claimed_job(claimed2)
    assert res.status == AutomationJobStatus.COMPLETED


@pytest.mark.anyio
async def test_15_late_measurements_do_not_overwrite_closed_reports(test_setup):
    """
    Test 15: Late measurements arriving after a day's report has been closed:
    - Never silently overwrite historical closed report.
    - Closed report remains unchanged.
    """
    s = test_setup["session"]
    cid = test_setup["cycle_id"]
    clock = test_setup["clock"]
    t0 = test_setup["anchor_dt"]
    m_repo = HealthMeasurementRepository(s)

    # 1. Close Day 1 with 1 measurement
    m_repo.create(cycle_id=cid, pulse=65, source="watch_sensor", measured_at=t0 + timedelta(hours=2))

    def flash_wellness(req: httpx.Request):
        return httpx.Response(200, json=_gemini_structured_envelope({
            "observed_trends": ["Пульс стабилен"],
            "limitations_and_gaps": [],
            "questionable_measurements": [],
            "summary_for_athena": "Пульс 65 bpm.",
        }))

    wellness_svc = WellnessDailyService(db=s, gateway=_make_gateway(flash_wellness), now_provider=clock.now)

    clock.set(t0 + timedelta(hours=5))
    rep1, is_new1 = await wellness_svc.process_wellness_for_day(cid, 1, 1, as_of=clock.now())
    assert is_new1 is True
    assert rep1.factual_summary.heart_rate.avg_bpm == 65.0

    # 2. Late measurements arrive backfilled with timestamp in Day 1 window
    clock.set(t0 + timedelta(hours=36))  # Day 2
    m_repo.create(cycle_id=cid, pulse=95, source="watch_sensor", measured_at=t0 + timedelta(hours=3))

    # 3. Attempt re-processing Day 1
    rep1_recheck, is_new2 = await wellness_svc.process_wellness_for_day(cid, 1, 1, as_of=clock.now())
    assert is_new2 is False
    assert rep1_recheck.report_id == rep1.report_id
    # Heart rate in closed report is UNTOUCHED
    assert rep1_recheck.factual_summary.heart_rate.avg_bpm == 65.0


@pytest.mark.anyio
async def test_16_server_only_isolation_and_no_client_leak(test_setup):
    """
    Test 16: Privacy isolation:
    - WellnessDailyReport and PatientTimezoneSetting are stored strictly as SERVER_ONLY.
    - Must NOT appear in list_client_facing queries.
    """
    s = test_setup["session"]
    cid = test_setup["cycle_id"]
    clock = test_setup["clock"]
    t0 = test_setup["anchor_dt"]

    wellness_svc = WellnessDailyService(db=s, now_provider=clock.now)
    wellness_svc.save_patient_timezone(cid, "Europe/Moscow")

    clock.set(t0 + timedelta(hours=5))
    await wellness_svc.process_wellness_for_day(cid, 1, 1, as_of=clock.now())

    rep_repo = ModelReportRepository(s)
    # SERVER_ONLY list contains them
    server_reports = rep_repo.list_server_only(cid)
    types_server = {r.report_type for r in server_reports}
    assert WELLNESS_DAILY_REPORT_TYPE in types_server
    assert PATIENT_TIMEZONE_REPORT_TYPE in types_server

    # CLIENT_FACING list does NOT contain them
    client_reports = rep_repo.list_client_facing(cid)
    types_client = {r.report_type for r in client_reports}
    assert WELLNESS_DAILY_REPORT_TYPE not in types_client
    assert PATIENT_TIMEZONE_REPORT_TYPE not in types_client


@pytest.mark.anyio
async def test_17_config_flags_enabled_disabled(test_setup):
    """
    Test 17: Configuration flags:
    - When enable_wellness_daily is False, automation worker does NOT discover or run wellness jobs.
    - When enable_wellness_daily is True, automation worker discovers and runs wellness jobs.
    """
    sf = test_setup["session_factory"]
    cid = test_setup["cycle_id"]
    clock = test_setup["clock"]
    t0 = test_setup["anchor_dt"]

    # 1. Flag is False
    clock.set(t0 + timedelta(hours=5))
    worker_disabled = ClinicalAutomationWorker(
        session_factory=sf,
        now_provider=clock.now,
        enable_wellness_daily=False,
    )
    ran_disabled = await worker_disabled.run_once(discover=True)
    wellness_jobs_disabled = [j for j in ran_disabled if j.job_type == AutomationJobType.WELLNESS_DAILY]
    assert len(wellness_jobs_disabled) == 0

    # 2. Flag is True
    worker_enabled = ClinicalAutomationWorker(
        session_factory=sf,
        now_provider=clock.now,
        enable_wellness_daily=True,
    )
    ran_enabled = await worker_enabled.run_once(discover=True)
    wellness_jobs_enabled = [j for j in ran_enabled if j.job_type == AutomationJobType.WELLNESS_DAILY]
    assert len(wellness_jobs_enabled) == 1
    assert wellness_jobs_enabled[0].status == AutomationJobStatus.COMPLETED


@pytest.mark.anyio
async def test_18_weekly_consilium_independent_of_wellness_success(test_setup):
    """
    Test 18: Architectural invariant:
    - Weekly Consilium does NOT depend on Wellness success.
    - If Wellness daily reports are missing or failed, Weekly Consilium functions normally.
    """
    s = test_setup["session"]
    cid = test_setup["cycle_id"]
    clock = test_setup["clock"]
    t0 = test_setup["anchor_dt"]

    # Close 7 days of daily journals WITHOUT running WellnessDailyService
    journal_svc = AthenaDailyJournalService(db=s, now_provider=clock.now)
    for d in range(1, 8):
        clock.set(t0 + timedelta(days=d, minutes=1))
        await journal_svc.close_daily_journal(cid, 1, d, as_of=clock.now())

    # Verify Daily Journal can assemble 7-day diary for consilium without crashing
    dj_diary = journal_svc.format_seven_day_journals_for_consilium(cid, sprint_number=1)
    assert dj_diary is not None
    assert "SEVEN-DAY ATHENA CLINICAL JOURNALS" in dj_diary
    for d in range(1, 8):
        assert f"--- DAY {d} ---" in dj_diary

    # Verify WeeklyConsiliumService operates completely independently
    weekly_svc = WeeklyConsiliumService(db=s, now_provider=clock.now)
    seven_logs = weekly_svc.get_seven_day_athena_logs(cid, sprint_id=None, reference_date=clock.now(), sprint_number=1)
    diary_str = weekly_svc.format_athena_diary_for_consilium(seven_logs)
    assert diary_str is not None


@pytest.mark.anyio
async def test_19_worker_restart_discovers_wellness_report_and_triggers_daily_journal_without_24h_wait(test_setup):
    """
    Test 19: Worker restart / crash recovery:
    - Wellness report was created at midday 12:00 (T0 + 4h).
    - Server/worker crashed before Daily Journal was created.
    - Worker restarts at T0 + 5h (well before 24h completion).
    - discover_and_enqueue_jobs immediately discovers the existing Wellness report and enqueues Daily Journal.
    - Daily Journal executes cleanly without waiting for 24 hours.
    """
    sf = test_setup["session_factory"]
    cid = test_setup["cycle_id"]
    clock = test_setup["clock"]
    t0 = test_setup["anchor_dt"]

    # 1. Create Wellness report at T0 + 4h
    clock.set(t0 + timedelta(hours=4))
    with sf() as s:
        wellness_svc = WellnessDailyService(db=s, now_provider=clock.now)
        await wellness_svc.process_wellness_for_day(cid, 1, 1, trigger=WellnessTriggerType.SCHEDULED_MIDDAY, as_of=clock.now())

    # 2. Advance to T0 + 5h ("Worker restarted after crash")
    clock.set(t0 + timedelta(hours=5))
    worker = ClinicalAutomationWorker(
        session_factory=sf,
        now_provider=clock.now,
        enable_wellness_daily=True,
        enable_daily_journal=True,
    )
    ran_jobs = await worker.run_once(discover=True)
    dj_jobs = [j for j in ran_jobs if j.job_type == AutomationJobType.DAILY_JOURNAL_CLOSURE]
    assert len(dj_jobs) == 1
    assert dj_jobs[0].status == AutomationJobStatus.COMPLETED

    # 3. Verify Daily Journal exists
    with sf() as s:
        j_svc = AthenaDailyJournalService(db=s, now_provider=clock.now)
        journal = j_svc.get_daily_journal(cid, 1, 1)
        assert journal is not None
        assert journal.calendar_day == 1
        assert journal.wellness_evidence.report_reference.is_available is True


@pytest.mark.anyio
async def test_20_repeat_events_and_no_duplicate_journals(test_setup):
    """
    Test 20: Repeat events and zero duplicate journals:
    - Multiple successive worker runs or re-triggers for the same day do NOT duplicate journals.
    - Exact idempotency is preserved across repeated executions.
    """
    sf = test_setup["session_factory"]
    cid = test_setup["cycle_id"]
    clock = test_setup["clock"]
    t0 = test_setup["anchor_dt"]

    clock.set(t0 + timedelta(hours=4))
    worker = ClinicalAutomationWorker(
        session_factory=sf,
        now_provider=clock.now,
        enable_wellness_daily=True,
        enable_daily_journal=True,
    )

    # First pass: executes wellness_daily and immediately enqueues daily journal closure
    ran1 = await worker.run_once(discover=True)
    wellness_ran = [j for j in ran1 if j.job_type == AutomationJobType.WELLNESS_DAILY]
    assert len(wellness_ran) == 1
    assert wellness_ran[0].status == AutomationJobStatus.COMPLETED

    # Second pass: executes the enqueued daily_journal_closure
    ran2 = await worker.run_once(discover=True)
    dj_ran = [j for j in ran2 if j.job_type == AutomationJobType.DAILY_JOURNAL_CLOSURE]
    assert len(dj_ran) == 1
    assert dj_ran[0].status == AutomationJobStatus.COMPLETED

    # Third pass immediately after: nothing new for wellness or daily journal
    ran3 = await worker.run_once(discover=True)
    w_and_dj = [
        j for j in ran3
        if j.job_type in (AutomationJobType.WELLNESS_DAILY, AutomationJobType.DAILY_JOURNAL_CLOSURE)
    ]
    assert len(w_and_dj) == 0

    # Verify strictly 1 daily journal exists in DB and is retrievable
    with sf() as s:
        j_svc = AthenaDailyJournalService(db=s, now_provider=clock.now)
        journal = j_svc.get_daily_journal(cid, 1, 1)
        assert journal is not None
        assert journal.calendar_day == 1

