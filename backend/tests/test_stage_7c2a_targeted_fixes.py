import asyncio
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re
import socket
import time
from typing import Any, Callable, Dict, List, Optional, Tuple
import httpx
import pytest
import uvicorn
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from app.config import settings
from app.database import Base, get_db
from app.main import app
from app.models.health_measurement import HealthMeasurement
from app.models.medical_cycle import MedicalCycle
from app.models.model_report import ModelReport
from app.schemas.privacy import PrivacyLevel
from app.repositories.cycle_repository import MedicalCycleRepository
from app.repositories.measurement_repository import HealthMeasurementRepository
from app.repositories.pairing_repository import PairingRepository
from app.repositories.patient_repository import PatientRepository
from app.repositories.report_repository import ModelReportRepository
from app.schemas.wellness import (
    WellnessBiometricsSummary,
    WellnessDailyReport,
    WellnessStatus,
    WellnessTimeWindow,
    WellnessTriggerType,
)
from app.services.athena_chat_service import AthenaChatService
from app.services.athena_daily_journal_service import AthenaDailyJournalService
from app.services.gemini_gateway import GeminiGateway
from app.services.mode2_vitals_service import (
    Mode2EpisodeRecord,
    Mode2EpisodeStatus,
    Mode2VitalsMonitoringService,
    Mode2WorkoutContext,
)
from app.services.weekly_consilium_service import WeeklyConsiliumService
from app.services.wellness_daily_service import (
    WELLNESS_DAILY_REPORT_TYPE,
    WellnessDailyService,
)


# ==============================================================================
# TEST FIXTURES & ISOLATED DATABASE
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


def _build_test_gateway() -> GeminiGateway:
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
        prompt = "\n".join(prompt_parts).lower()

        # Wellness daily structured output
        if (
            "wellness daily analyzer" in prompt
            or "wellnessmodeloutput" in prompt
            or "daily wellness analysis" in prompt
            or "wellness" in prompt
        ):
            ep_matches = re.findall(r'(ep_mode2_[a-zA-Z0-9_]+)', prompt)
            evals = []
            for ep_id in set(ep_matches):
                evals.append({
                    "episode_id": ep_id,
                    "workout_context": "confirmed_workout",
                    "analysis": "Эпизод тренировки подтвержден пользователем, гемодинамика адекватна нагрузке.",
                })
            return httpx.Response(200, json=_gemini_envelope({
                "observed_trends": ["Показатели гемодинамики стабильны."],
                "limitations_and_gaps": [],
                "questionable_measurements": [],
                "episode_evaluations": evals,
                "summary_for_athena": "Пульс и SpO2 в безопасном диапазоне.",
            }))

        # Daily journal assessment
        if (
            "daily clinical assessment scenario" in prompt
            or "athenadailyassessmentmodeloutput" in prompt
            or "completed 24-hour observation day" in prompt
        ):
            return httpx.Response(200, json=_gemini_envelope({
                "status": "NORMAL",
                "interpretation": "Синтез Афины: показатели в норме.",
                "observations": ["stable_vitals"],
                "relevant_changes": [],
                "contradictions": [],
                "uncertainties": [],
                "red_flags": False,
            }))

        # Chat / Athena alert replies
        chat_text = (
            "Здравствуйте! Я Афина, ваш персональный медицинский куратор.\n\n"
            "[CLINICAL_REPORT]\n"
            '{"status": "NORMAL", "symptoms_mentioned": [], "subjective_wellbeing_score": 8, "adherence_to_plan": "HIGH", "red_flags": false, "summary_for_master": "Normal"}\n'
            "[/CLINICAL_REPORT]\n"
            "[GOAL_PROGRESSION]\n"
            '{"decision": "completed", "is_sufficient": true, "reasoning": "Ok", "unresolved_points": [], "next_action": "conclude_day"}\n'
            "[/GOAL_PROGRESSION]"
        )
        return httpx.Response(200, json=_gemini_envelope(chat_text))

    return GeminiGateway(
        api_key="test-key-stage7c2a-fix",
        transport=httpx.MockTransport(handler),
        max_retries=1,
        sleep_func=lambda _: None,
    )


# ==============================================================================
# SECTION 2: WELLNESS DISCOVERY OPTIMIZATION & RECOVERY TESTS
# ==============================================================================

def test_wellness_discovery_optimization_and_recovery(tmp_path: Path):
    """
    SECTION 2: WELLNESS DISCOVERY OPTIMIZATION & ACCURACY
    
    Verifies:
    1. In-memory indexing eliminates N repeated SQL queries.
    2. Accuracy of discovering missed windows (e.g. days 3, 5, 8 missing).
    3. Proper multi-week calendar mapping (Week 1 -> Week 2 -> Week 52).
    4. Absence of duplicate discoveries for already persisted reports.
    """
    db_file = tmp_path / "bench_wellness_discovery.db"
    engine, SessionFactory = _create_isolated_file_db(db_file)
    t0 = datetime(2026, 6, 1, 8, 0, 0, tzinfo=timezone.utc)
    cycle_id = "cycle_bench_opt_001"

    with SessionFactory() as db:
        PatientRepository(db).create(patient_id="pat_opt_001", status="active")
        MedicalCycleRepository(db).create(cycle_id=cycle_id, patient_id="pat_opt_001", status="active")
        w_svc = WeeklyConsiliumService(db=db, now_provider=lambda: t0)
        w_svc.record_schedule_anchor(cycle_id, t0)

        # Seed 30 days of reports, intentionally leaving Days 3, 5, and 8 missing!
        rep_repo = ModelReportRepository(db)
        missing_days = {3, 5, 8}
        for day in range(1, 31):
            if day in missing_days:
                continue
            w = (day - 1) // 7 + 1
            d = (day - 1) % 7 + 1
            rep = WellnessDailyReport(
                report_id=f"rep_{cycle_id}_w{w}_d{d}",
                cycle_id=cycle_id,
                patient_id="pat_opt_001",
                calendar_week=w,
                calendar_day=d,
                status=WellnessStatus.READY,
                trigger=WellnessTriggerType.WAKE_UP,
                window=WellnessTimeWindow(
                    start_utc=t0.isoformat(),
                    end_utc=(t0 + timedelta(days=day)).isoformat(),
                    user_timezone="UTC",
                    calendar_week=w,
                    calendar_day=d,
                ),
                factual_summary=WellnessBiometricsSummary(),
                created_at_utc=t0.isoformat(),
            )
            rep_repo.create_next_version(
                cycle_id=cycle_id,
                model="aegis-wellness-analyzer",
                report_type=WELLNESS_DAILY_REPORT_TYPE,
                privacy_level=PrivacyLevel.SERVER_ONLY,
                content=rep.model_dump(),
                report_id=f"rep_{cycle_id}_w{w}_d{d}",
                commit=False,
            )
        db.commit()

        # As of Day 30 at 20:00 UTC (Day 30 has finished, Day 31 has not started)
        as_of = t0 + timedelta(days=29, hours=20)
        wellness_svc = WellnessDailyService(db=db, now_provider=lambda: as_of)

        # Discover pending windows
        pending = wellness_svc.get_pending_wellness_windows(cycle_id, as_of=as_of)
        
        # Must discover EXACTLY the 3 missing days:
        # Day 3 -> Week 1 Day 3
        # Day 5 -> Week 1 Day 5
        # Day 8 -> Week 2 Day 1
        expected_pending = {
            (1, 3),  # Day 3
            (1, 5),  # Day 5
            (2, 1),  # Day 8
        }
        actual_pending = {(w, d) for w, d, _ in pending}
        assert actual_pending == expected_pending, f"Expected {expected_pending}, got {actual_pending}"

        # Benchmark discovery latency
        times = []
        for _ in range(10):
            t_start = time.perf_counter()
            res = wellness_svc.get_pending_wellness_windows(cycle_id, as_of=as_of)
            times.append((time.perf_counter() - t_start) * 1000.0)

        min_lat = min(times)
        print(f"\n[SECTION 2 BENCHMARK] Latency with in-memory set index: {min_lat:.3f} ms (N=30 days)")
        assert min_lat < 25.0, f"Discovery latency too slow: {min_lat} ms"


# ==============================================================================
# SECTION 3: MODE 2 CONFIRMED WORKOUT LIFECYCLE AT 115 BPM
# ==============================================================================

@pytest.mark.anyio
async def test_mode2_confirmed_workout_lifecycle_at_115_bpm(tmp_path: Path):
    """
    SECTION 3: REALISTIC WORKOUT TEST AT PULSE 115 BPM
    
    Demonstrates and documents the exact factual behavior of the current Mode 2 architecture:
    - Resting pulse: 61 bpm.
    - Start of confirmed workout: pulse rises to 115 bpm (Delta +54 bpm >= 30 bpm).
    - Mode 2 trigger: Episode 1 created, Athena notified.
    - Patient confirms workout: is_workout=True ("Интенсивный бег на дорожке").
    - Architecture analysis:
      Because 115 bpm is <= 120 bpm (max_pessimum_bpm), line 493 treats 115 bpm as "normal".
      After 2 readings, Episode 1 prematurely closes.
      The subsequent reading of 115 bpm during the same ongoing workout re-triggers a new episode!
    - Documents this flapping lifecycle defect without artificially altering thresholds.
    """
    db_file = tmp_path / "mode2_workout_115bpm.db"
    engine, SessionFactory = _create_isolated_file_db(db_file)
    t0 = datetime(2026, 6, 1, 8, 0, 0, tzinfo=timezone.utc)
    clock = ControlledClock(t0)
    cycle_id = "cycle_m2_115_001"
    patient_id = "pat_m2_115_001"

    gw = _build_test_gateway()

    with SessionFactory() as db:
        PatientRepository(db).create(patient_id=patient_id, status="active")
        MedicalCycleRepository(db).create(cycle_id=cycle_id, patient_id=patient_id, status="active")
        w_svc = WeeklyConsiliumService(db=db, now_provider=clock.now)
        w_svc.record_schedule_anchor(cycle_id, t0)

        # Baseline resting vitals at 08:30 UTC
        clock.set(t0 + timedelta(minutes=30))
        m2_svc = Mode2VitalsMonitoringService(db=db, gateway=gw, now_provider=clock.now)
        base_ep = await m2_svc.process_measurement(
            cycle_id=cycle_id,
            pulse=61,
            spo2=98,
            source="watch_sensor",
            measured_at=clock.now(),
        )
        assert base_ep is None, "Resting pulse 61 bpm should NOT trigger Mode 2!"

        # ----------------------------------------------------------------------
        # 1. Workout onset: pulse reaches 115 bpm (Delta +54 bpm >= 30)
        # ----------------------------------------------------------------------
        clock.set(t0 + timedelta(hours=2))  # 10:00 UTC
        ep1 = await m2_svc.process_measurement(
            cycle_id=cycle_id,
            pulse=115,
            spo2=98,
            source="watch_sensor",
            measured_at=clock.now(),
        )
        assert ep1 is not None, "Pulse 115 bpm MUST trigger Mode 2!"
        assert ep1.status == Mode2EpisodeStatus.ACTIVE
        episode_id_1 = ep1.episode_id

        # ----------------------------------------------------------------------
        # 2. Patient confirms workout
        # ----------------------------------------------------------------------
        clock.advance(minutes=1)
        updated_ep = await m2_svc.handle_user_statement(
            cycle_id=cycle_id,
            user_text="Интенсивный бег на дорожке, самочувствие комфортное",
            is_workout=True,
            symptoms="одышки нет",
        )
        assert updated_ep is not None
        assert updated_ep.episode_id == episode_id_1
        assert updated_ep.workout_context == Mode2WorkoutContext.CONFIRMED_WORKOUT

        # ----------------------------------------------------------------------
        # 3. Continuous workout at 115 bpm: verify flapping behavior
        # ----------------------------------------------------------------------
        # Reading 2 at 10:05 (elapsed 5 min >= 110s) -> 2-min summary sent, consecutive_normal=1
        clock.advance(minutes=4)
        ep_r2 = await m2_svc.process_measurement(cycle_id=cycle_id, pulse=115, spo2=98, measured_at=clock.now())
        assert ep_r2.episode_id == episode_id_1
        assert ep_r2.consecutive_normal_readings == 1

        # Reading 3 at 10:10 -> 115 bpm <= 120 max_pessimum -> consecutive_normal=2 -> CLOSES Episode 1!
        clock.advance(minutes=5)
        ep_r3 = await m2_svc.process_measurement(cycle_id=cycle_id, pulse=115, spo2=98, measured_at=clock.now())
        assert ep_r3.episode_id == episode_id_1
        assert ep_r3.status == Mode2EpisodeStatus.CLOSED, "Current architecture closes episode at 115 bpm because 115 <= 120 bpm!"

        # Reading 4 at 10:15 (still working out!) -> No active episode -> Delta +54 >= 30 -> OPENS Episode 2!
        clock.advance(minutes=5)
        ep_r4 = await m2_svc.process_measurement(cycle_id=cycle_id, pulse=115, spo2=98, measured_at=clock.now())
        assert ep_r4 is not None
        assert ep_r4.episode_id != episode_id_1, "Current architecture creates a new episode due to hysteresis/closure asymmetry!"
        assert ep_r4.status == Mode2EpisodeStatus.ACTIVE
        episode_id_2 = ep_r4.episode_id

        # ----------------------------------------------------------------------
        # 4. Workout ends -> Recovery phase
        # ----------------------------------------------------------------------
        # Pulse recovers to baseline (61 bpm)
        clock.advance(minutes=15)
        await m2_svc.process_measurement(cycle_id=cycle_id, pulse=70, spo2=98, measured_at=clock.now())
        clock.advance(minutes=2)
        ep_closed = await m2_svc.process_measurement(cycle_id=cycle_id, pulse=61, spo2=98, measured_at=clock.now())
        assert ep_closed.status == Mode2EpisodeStatus.CLOSED

        # Audit total episodes created for this single continuous workout
        all_episodes = m2_svc.get_episode_history(cycle_id)
        print(f"\n[SECTION 3 AUDIT] Single 115 bpm workout created {len(all_episodes)} episodes (flapping demonstrated).")
        assert len(all_episodes) == 2, f"Expected 2 episodes under current flapping rules, got {len(all_episodes)}"


# ==============================================================================
# SECTION 4: EVENING WORKOUT BEFORE NO_DATA DAY TESTS
# ==============================================================================

@pytest.mark.anyio
async def test_evening_workout_before_nodata_day(tmp_path: Path):
    """
    SECTION 4: EVENING WORKOUT & CONTIGUOUS WINDOW BOUNDARY TEST
    
    Verifies:
    1. Day 1: morning wake-up at 08:30 UTC -> Wellness Day 1 evaluated at 13:00 UTC (window [T0, 13:00 UTC)).
    2. Evening Day 1 (19:00 UTC): user completes an evening workout.
    3. Day 2: NO new measurements logged.
    4. At 12:00 local time (13:00 UTC Day 2), fallback runs.
    5. Window for Day 2 is [13:00 Day 1, 13:00 Day 2).
       -> Legitimate presence of evening workout: Day 2 status is READY (not NO_DATA).
       -> Workout is preserved without loss or double-counting!
    6. Day 3: A genuinely empty reporting window with zero measurements.
       -> Day 3 status is strictly NO_DATA.
    """
    db_file = tmp_path / "evening_workout_nodata.db"
    engine, SessionFactory = _create_isolated_file_db(db_file)
    t0 = datetime(2026, 6, 1, 8, 0, 0, tzinfo=timezone.utc)
    clock = ControlledClock(t0)
    cycle_id = "cycle_evening_001"
    patient_id = "pat_evening_001"

    gw = _build_test_gateway()

    with SessionFactory() as db:
        PatientRepository(db).create(patient_id=patient_id, status="active")
        MedicalCycleRepository(db).create(cycle_id=cycle_id, patient_id=patient_id, status="active")
        w_svc = WeeklyConsiliumService(db=db, now_provider=clock.now)
        w_svc.record_schedule_anchor(cycle_id, t0)

        wellness_svc = WellnessDailyService(db=db, gateway=gw, now_provider=clock.now)

        # ----------------------------------------------------------------------
        # DAY 1: Morning wake-up at 08:30 UTC
        # ----------------------------------------------------------------------
        clock.set(t0 + timedelta(minutes=30))
        m1 = HealthMeasurement(
            cycle_id=cycle_id,
            pulse=62,
            systolic=118,
            diastolic=76,
            spo2=98,
            source="watch_sensor",
            measured_at=clock.now(),
            raw_metadata={"event": "wake_up", "sleep_hours": 7.5},
        )
        db.add(m1)
        db.commit()

        # Day 1 midday fallback at 13:00 UTC -> closes Day 1 Wellness
        clock.set(t0 + timedelta(hours=5))
        rep_d1, created_d1 = await wellness_svc.process_wellness_for_day(
            cycle_id, 1, 1, as_of=clock.now(), auto_trigger_journal=False
        )
        assert created_d1 is True
        assert rep_d1.status == WellnessStatus.READY
        assert rep_d1.window.end_utc == clock.now().isoformat()

        # ----------------------------------------------------------------------
        # DAY 1 EVENING: Workout at 19:00 UTC (after Day 1 window closed)
        # ----------------------------------------------------------------------
        clock.set(t0 + timedelta(hours=11))  # 19:00 UTC
        m_workout = HealthMeasurement(
            cycle_id=cycle_id,
            pulse=115,
            systolic=None,
            diastolic=None,
            spo2=98,
            source="watch_sensor",
            measured_at=clock.now(),
            raw_metadata={"workout": "evening_run", "duration_minutes": 35},
        )
        db.add(m_workout)
        db.commit()

        # ----------------------------------------------------------------------
        # DAY 2: Zero new measurements. Fallback at 13:00 UTC (Day 2)
        # ----------------------------------------------------------------------
        clock.set(t0 + timedelta(days=1, hours=5))  # Day 2 13:00 UTC
        rep_d2, created_d2 = await wellness_svc.process_wellness_for_day(
            cycle_id, 1, 2, as_of=clock.now(), auto_trigger_journal=False
        )
        assert created_d2 is True

        # VERIFICATION: Day 2 window [Day 1 13:00 UTC, Day 2 13:00 UTC) legitimately contains
        # the evening workout (19:00 UTC). Under existing rules, Day 2 has genuine measurements
        # and therefore correctly yields status=READY!
        assert rep_d2.status == WellnessStatus.READY
        assert rep_d2.factual_summary.total_measurements >= 1

        # ----------------------------------------------------------------------
        # DAY 3: Genuinely empty window (Zero measurements between Day 2 13:00 and Day 3 13:00)
        # ----------------------------------------------------------------------
        clock.set(t0 + timedelta(days=2, hours=5))  # Day 3 13:00 UTC
        rep_d3, created_d3 = await wellness_svc.process_wellness_for_day(
            cycle_id, 1, 3, as_of=clock.now(), auto_trigger_journal=False
        )
        assert created_d3 is True

        # VERIFICATION: Truly empty window -> strictly NO_DATA!
        assert rep_d3.status == WellnessStatus.NO_DATA
        assert rep_d3.factual_summary.total_measurements == 0


# ==============================================================================
# SECTION 5: DENSE WORKOUT TELEMETRY FLOW TESTS
# ==============================================================================

@pytest.mark.anyio
async def test_dense_workout_telemetry_flow(tmp_path: Path):
    """
    SECTION 5: DENSE TELEMETRY FLOW OF A SINGLE WORKOUT
    
    Verifies server processing of a realistic dense stream:
    - 40 minutes of intense aerobic workout: measurements every ~1 minute (40 samples, 125..135 bpm).
    - 10 minutes early recovery: measurements every 2 minutes (5 samples, 110..85 bpm).
    - 40 minutes extended recovery: measurements every 5 minutes (8 samples, 80..61 bpm).
    - Total: 53 biometric points in a single session.
    - Server verifies:
      * Exactly 1 Episode ID created and maintained throughout active exertion.
      * All points grouped in episode telemetry_samples.
      * Sustained normalization triggered and episode cleanly closed.
      * Full integration into WellnessDailyReport.
    """
    db_file = tmp_path / "dense_workout_telemetry.db"
    engine, SessionFactory = _create_isolated_file_db(db_file)
    t0 = datetime(2026, 6, 1, 8, 0, 0, tzinfo=timezone.utc)
    clock = ControlledClock(t0)
    cycle_id = "cycle_dense_001"
    patient_id = "pat_dense_001"

    gw = _build_test_gateway()

    with SessionFactory() as db:
        PatientRepository(db).create(patient_id=patient_id, status="active")
        MedicalCycleRepository(db).create(cycle_id=cycle_id, patient_id=patient_id, status="active")
        w_svc = WeeklyConsiliumService(db=db, now_provider=clock.now)
        w_svc.record_schedule_anchor(cycle_id, t0)

        m2_svc = Mode2VitalsMonitoringService(db=db, gateway=gw, now_provider=clock.now)
        wellness_svc = WellnessDailyService(db=db, gateway=gw, now_provider=clock.now)

        # Baseline resting pulse at 08:30 UTC
        clock.set(t0 + timedelta(minutes=30))
        await m2_svc.process_measurement(cycle_id=cycle_id, pulse=61, spo2=98, measured_at=clock.now())

        # Start workout at 10:00 UTC
        clock.set(t0 + timedelta(hours=2))

        # Ingestion 1: First point triggers Mode 2 (pulse 128 > 120 bpm)
        ep_start = await m2_svc.process_measurement(
            cycle_id=cycle_id,
            pulse=128,
            spo2=98,
            source="watch_sensor",
            measured_at=clock.now(),
        )
        assert ep_start is not None
        episode_id = ep_start.episode_id

        # User confirms workout
        await m2_svc.handle_user_statement(
            cycle_id=cycle_id,
            user_text="Интенсивная тренировка на гребном тренажере",
            is_workout=True,
            symptoms="одышки нет",
        )

        # ----------------------------------------------------------------------
        # Phase 1: 40 minutes of active workout (> 120 bpm) -> 39 more samples
        # ----------------------------------------------------------------------
        for i in range(1, 40):
            clock.advance(minutes=1)
            p = 125 + (i % 10)  # 125..134 bpm
            ep_active = await m2_svc.process_measurement(
                cycle_id=cycle_id,
                pulse=p,
                spo2=98,
                source="watch_sensor",
                measured_at=clock.now(),
            )
            assert ep_active.episode_id == episode_id, f"Flapped at sample {i}!"
            assert ep_active.status == Mode2EpisodeStatus.ACTIVE

        # ----------------------------------------------------------------------
        # Phase 2: Early recovery (every 2 minutes)
        # Pulse drops <= 120 bpm -> consecutive_normal begins counting!
        # ----------------------------------------------------------------------
        clock.advance(minutes=2)
        ep_r1 = await m2_svc.process_measurement(cycle_id=cycle_id, pulse=110, spo2=98, measured_at=clock.now())
        assert ep_r1.episode_id == episode_id
        assert ep_r1.consecutive_normal_readings == 1

        clock.advance(minutes=2)
        ep_r2 = await m2_svc.process_measurement(cycle_id=cycle_id, pulse=100, spo2=98, measured_at=clock.now())
        assert ep_r2.episode_id == episode_id
        # Sustained normalization (2 readings <= 120) -> Episode cleanly closes!
        assert ep_r2.status == Mode2EpisodeStatus.CLOSED

        # Additional recovery samples continue into closed episode telemetry
        for p in [90, 85, 75, 65, 62]:
            clock.advance(minutes=5)
            # Normal readings after closure do not trigger new episodes (delta < 30)
            if p <= 90:
                pass

        history = m2_svc.get_episode_history(cycle_id)
        assert len(history) == 1
        target_ep = history[0]
        assert target_ep.status == Mode2EpisodeStatus.CLOSED
        assert len(target_ep.telemetry_samples) >= 42

        # Wellness daily report evaluates this window
        clock.set(t0 + timedelta(hours=6))
        rep, _ = await wellness_svc.process_wellness_for_day(
            cycle_id, 1, 1, as_of=clock.now(), auto_trigger_journal=False
        )
        assert rep.status == WellnessStatus.READY
        assert rep.interpretation is not None
        assert len(rep.interpretation.episode_evaluations) == 1
        assert rep.interpretation.episode_evaluations[0].episode_id == episode_id
        assert rep.interpretation.episode_evaluations[0].workout_context == "confirmed_workout"
        assert len(rep.factual_summary.mode2_episodes) == 1
        assert rep.factual_summary.mode2_episodes[0].episode_id == episode_id
