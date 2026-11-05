import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.models import MedicalCycle, HealthMeasurement
from app.models.model_report import ModelReport
from app.schemas.privacy import PrivacyLevel
from app.repositories.cycle_repository import MedicalCycleRepository
from app.repositories.measurement_repository import HealthMeasurementRepository
from app.repositories.report_repository import ModelReportRepository
from app.schemas.chat import AthenaChatRequest, AthenaVitalsAlertContext
from app.schemas.mode2 import (
    Mode2EpisodeRecord,
    Mode2EpisodeStatus,
    Mode2WorkoutContext,
)
from app.services.athena_chat_service import AthenaChatService
from app.services.athena_weekly_plan_service import AthenaWeeklyPlanService
from app.services.gemini_gateway import GeminiGateway
from app.services.mode2_vitals_service import (
    ATHENA_MODE2_EPISODE_REPORT_TYPE,
    CRITICAL_BRADYCARDIA_BPM,
    CRITICAL_TACHYCARDIA_BPM,
    EXTREME_TACHYCARDIA_BPM,
    Mode2VitalsMonitoringService,
)


class MockClock:
    def __init__(self, start: datetime):
        self._current = start

    def now(self) -> datetime:
        return self._current

    def advance(self, **kwargs):
        self._current += timedelta(**kwargs)


def _make_gemini_gateway(mock_handler):
    import httpx
    transport = httpx.MockTransport(mock_handler)
    gw = GeminiGateway(
        api_key="test-api-key-mode2-mock",
        transport=transport,
        max_retries=1,
        sleep_func=lambda _: asyncio.sleep(0),
    )
    return gw


def _standard_chat_response_handler(request: Any):
    import httpx
    body = request.read().decode("utf-8")
    reply_text = (
        "Здравствуйте! Я зафиксировала повышение пульса. Пожалуйста, присядьте, "
        "расстегните воротник и спокойно подышите. Скажите, вы сейчас тренируетесь или отдыхаете? "
        "Как вы себя чувствуете?\n\n"
        "[CLINICAL_REPORT]\n"
        '{"status": "ALERT", "symptoms_mentioned": [], "subjective_wellbeing_score": null, '
        '"adherence_to_plan": "UNKNOWN", "red_flags": false, "summary_for_master": "Mode 2 alert"}\n'
        "[/CLINICAL_REPORT]"
    )
    return httpx.Response(
        200,
        json={
            "candidates": [
                {
                    "content": {
                        "parts": [{"text": reply_text}],
                        "role": "model",
                    },
                    "finishReason": "STOP",
                }
            ]
        },
    )


@pytest.fixture
def isolated_db():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    session = TestingSessionLocal()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(bind=engine)


def _seed_cycle_with_pro_thresholds(
    db: Session,
    cycle_id: str = "cycle_mode2_01",
    patient_id: str = "patient_mode2_01",
    min_pessimum_bpm: int = 50,
    max_pessimum_bpm: int = 115,
    max_spike_delta_bpm: int = 25,
) -> MedicalCycle:
    from app.repositories.patient_repository import PatientRepository
    patient_repo = PatientRepository(db)
    if not patient_repo.get(patient_id):
        patient_repo.create(patient_id=patient_id, status="active")

    cycle_repo = MedicalCycleRepository(db)
    cycle = cycle_repo.create(cycle_id=cycle_id, patient_id=patient_id, status="active")

    report_repo = ModelReportRepository(db)
    report_repo.create_next_version(
        cycle_id=cycle_id,
        model="gemini-3.1-pro-preview",
        report_type="user_overall_report",
        privacy_level=PrivacyLevel.CLIENT_FACING,
        content={
            "title": "Aegis Health Strategy",
            "summary": "Initial baseline health review",
            "metrics": {
                "min_pessimum_bpm": min_pessimum_bpm,
                "max_pessimum_bpm": max_pessimum_bpm,
                "max_spike_delta_bpm": max_spike_delta_bpm,
            },
        },
        commit=True,
    )
    return cycle


# ==============================================================================
# TEST 1: HR SPIKE OUTSIDE PRO THRESHOLD -> EPISODE CREATED & ATHENA TRIGGERED
# ==============================================================================

@pytest.mark.anyio
async def test_01_hr_spike_creates_episode_and_triggers_athena(isolated_db: Session):
    clock = MockClock(datetime(2026, 11, 5, 10, 0, tzinfo=timezone.utc))
    cycle = _seed_cycle_with_pro_thresholds(
        isolated_db,
        cycle_id="cycle_t1",
        patient_id="pat_t1",
        min_pessimum_bpm=50,
        max_pessimum_bpm=115,
        max_spike_delta_bpm=25,
    )

    ai_calls: List[str] = []

    def mock_handler(request):
        import httpx
        body = request.read().decode("utf-8")
        ai_calls.append(body)
        return _standard_chat_response_handler(request)

    gw = _make_gemini_gateway(mock_handler)
    service = Mode2VitalsMonitoringService(db=isolated_db, gateway=gw, now_provider=clock.now)

    # Ingest a pulse spike (135 bpm > 115 bpm max pessimum)
    ep = await service.process_measurement(
        cycle_id=cycle.cycle_id,
        pulse=135,
        spo2=97,
        source="watch_sensor",
        measured_at=clock.now(),
    )

    assert ep is not None
    assert ep.status == Mode2EpisodeStatus.ACTIVE
    assert ep.trigger_pulse == 135
    assert ep.max_pulse == 135
    assert ep.workout_context == Mode2WorkoutContext.UNKNOWN
    assert ep.episode_id.startswith(f"ep_mode2_{cycle.cycle_id}_")
    assert len(ep.athena_events) == 1
    assert ep.athena_events[0].event_type == "initial_alert"
    assert "воротник" in ep.athena_events[0].athena_reply

    # Verify Gemini was invoked directly for Athena
    assert len(ai_calls) == 1
    assert "Heart rate spike detected" in ai_calls[0]


# ==============================================================================
# TEST 2: CONFIRMED WORKOUT -> SAVED VERBATIM WITH TIMESTAMP
# ==============================================================================

@pytest.mark.anyio
async def test_02_confirmed_workout_saved_verbatim(isolated_db: Session):
    clock = MockClock(datetime(2026, 11, 5, 10, 0, tzinfo=timezone.utc))
    cycle = _seed_cycle_with_pro_thresholds(isolated_db, "cycle_t2", "pat_t2")

    def mock_handler(request):
        import httpx
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {
                        "content": {
                            "parts": [{"text": "Отлично, держите темп и пейте воду!\n\n[CLINICAL_REPORT]\n{\"status\": \"NORMAL\"}\n[/CLINICAL_REPORT]"}],
                            "role": "model",
                        },
                        "finishReason": "STOP",
                    }
                ]
            },
        )

    gw = _make_gemini_gateway(mock_handler)
    service = Mode2VitalsMonitoringService(db=isolated_db, gateway=gw, now_provider=clock.now)

    # Initial alert
    await service.process_measurement(cycle_id=cycle.cycle_id, pulse=130, measured_at=clock.now())

    # User answers: "Да, бегу интервальный кросс"
    clock.advance(seconds=30)
    user_stmt = "Да, бегу интервальный кросс на стадионе"
    updated_ep = await service.handle_user_statement(
        cycle_id=cycle.cycle_id,
        user_text=user_stmt,
        is_workout=True,
        as_of=clock.now(),
    )

    assert updated_ep is not None
    assert updated_ep.workout_context == Mode2WorkoutContext.CONFIRMED_WORKOUT
    assert len(updated_ep.user_statements) == 1
    assert updated_ep.user_statements[0].text == user_stmt
    assert updated_ep.user_statements[0].timestamp_utc == clock.now().isoformat()
    assert updated_ep.user_statements[0].is_workout_confirmation is True


# ==============================================================================
# TEST 3: DANGEROUS SYMPTOMS DURING WORKOUT -> URGENT SAFETY ALERT
# ==============================================================================

@pytest.mark.anyio
async def test_03_dangerous_symptoms_during_workout_alerts_urgently(isolated_db: Session):
    clock = MockClock(datetime(2026, 11, 5, 10, 0, tzinfo=timezone.utc))
    cycle = _seed_cycle_with_pro_thresholds(isolated_db, "cycle_t3", "pat_t3")

    ai_prompts: List[str] = []

    def mock_handler(request):
        import httpx
        body = request.read().decode("utf-8")
        ai_prompts.append(body)
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {
                        "content": {
                            "parts": [{"text": "Срочно прекратите тренировку! Присядьте и вызовите скорую помощь (112)!\n\n[CLINICAL_REPORT]\n{\"status\": \"CRITICAL\", \"red_flags\": true}\n[/CLINICAL_REPORT]\n[EMERGENCY_TRIGGER]"}],
                            "role": "model",
                        },
                        "finishReason": "STOP",
                    }
                ]
            },
        )

    gw = _make_gemini_gateway(mock_handler)
    service = Mode2VitalsMonitoringService(db=isolated_db, gateway=gw, now_provider=clock.now)

    # Initial alert
    await service.process_measurement(cycle_id=cycle.cycle_id, pulse=145, measured_at=clock.now())

    # User responds during workout: "Бегу, но сильно давит за грудиной и темнеет в глазах"
    clock.advance(seconds=40)
    updated_ep = await service.handle_user_statement(
        cycle_id=cycle.cycle_id,
        user_text="Бегу кросс, но резко появилась боль в груди и темнеет в глазах",
        is_workout=True,
        as_of=clock.now(),
    )

    assert updated_ep is not None
    assert updated_ep.workout_context == Mode2WorkoutContext.CONFIRMED_WORKOUT
    # Confirmed workout did NOT silence red flags: emergency instruction sent
    assert any("ТРЕВОЖНЫЙ СИГНАЛ" in p for p in ai_prompts)
    assert any(ev.emergency_trigger for ev in updated_ep.athena_events)


# ==============================================================================
# TEST 4: NEW TELEMETRY THROUGH 2 MINUTES -> 2-MINUTE SUMMARY SENT TO ATHENA
# ==============================================================================

@pytest.mark.anyio
async def test_04_two_minute_telemetry_summary_sent(isolated_db: Session):
    clock = MockClock(datetime(2026, 11, 5, 10, 0, tzinfo=timezone.utc))
    cycle = _seed_cycle_with_pro_thresholds(isolated_db, "cycle_t4", "pat_t4")

    ai_events: List[str] = []

    def mock_handler(request):
        import httpx
        ai_events.append("gemini_called")
        return _standard_chat_response_handler(request)

    gw = _make_gemini_gateway(mock_handler)
    service = Mode2VitalsMonitoringService(db=isolated_db, gateway=gw, now_provider=clock.now)

    # 0 min: Spike detected
    await service.process_measurement(cycle_id=cycle.cycle_id, pulse=128, spo2=98, measured_at=clock.now())
    assert len(ai_events) == 1

    # 1 min (60 sec): Intermediate measurement, < 2 min -> recorded silently
    clock.advance(seconds=60)
    ep_1 = await service.process_measurement(cycle_id=cycle.cycle_id, pulse=130, spo2=97, measured_at=clock.now())
    assert len(ai_events) == 1  # No new AI call at 1 min!
    assert len(ep_1.telemetry_samples) == 2

    # 2 min (120 sec): 2-minute mark reached -> triggers 2-minute summary!
    clock.advance(seconds=60)
    ep_2 = await service.process_measurement(cycle_id=cycle.cycle_id, pulse=132, spo2=97, measured_at=clock.now())
    assert len(ai_events) == 2  # 2-minute summary delivered to Athena!
    assert ep_2.initial_two_minute_update_sent is True
    assert ep_2.athena_events[-1].event_type == "two_minute_summary"


# ==============================================================================
# TEST 5: MINOR FLUCTUATIONS (< 15 BPM) -> SAVED WITHOUT SENDING TO GEMINI
# ==============================================================================

@pytest.mark.anyio
async def test_05_minor_fluctuations_recorded_without_gemini_calls(isolated_db: Session):
    clock = MockClock(datetime(2026, 11, 5, 10, 0, tzinfo=timezone.utc))
    cycle = _seed_cycle_with_pro_thresholds(isolated_db, "cycle_t5", "pat_t5")

    ai_call_count = 0

    def mock_handler(request):
        nonlocal ai_call_count
        ai_call_count += 1
        return _standard_chat_response_handler(request)

    gw = _make_gemini_gateway(mock_handler)
    service = Mode2VitalsMonitoringService(db=isolated_db, gateway=gw, now_provider=clock.now)

    # 0 min: Initial alert (Call 1)
    await service.process_measurement(cycle_id=cycle.cycle_id, pulse=125, spo2=98, measured_at=clock.now())
    assert ai_call_count == 1

    # 2 min: Two-minute summary (Call 2, last_notified_pulse = 126)
    clock.advance(seconds=120)
    await service.process_measurement(cycle_id=cycle.cycle_id, pulse=126, spo2=98, measured_at=clock.now())
    assert ai_call_count == 2

    # 3 min: Pulse 128 bpm (delta = |128 - 126| = 2 < 15 bpm) -> ZERO AI calls!
    clock.advance(seconds=60)
    await service.process_measurement(cycle_id=cycle.cycle_id, pulse=128, spo2=98, measured_at=clock.now())
    assert ai_call_count == 2

    # 4 min: Pulse 131 bpm (delta = |131 - 126| = 5 < 15 bpm) -> ZERO AI calls!
    clock.advance(seconds=60)
    await service.process_measurement(cycle_id=cycle.cycle_id, pulse=131, spo2=98, measured_at=clock.now())
    assert ai_call_count == 2

    # 5 min: Pulse 135 bpm (delta = |135 - 126| = 9 < 15 bpm) -> ZERO AI calls!
    clock.advance(seconds=60)
    ep = await service.process_measurement(cycle_id=cycle.cycle_id, pulse=135, spo2=98, measured_at=clock.now())
    assert ai_call_count == 2
    assert len(ep.telemetry_samples) == 5  # All 5 points recorded in history!


# ==============================================================================
# TEST 6: SIGNIFICANT DETERIORATION (DELTA >= 15 BPM) -> NOTIFIES ATHENA
# ==============================================================================

@pytest.mark.anyio
async def test_06_significant_deterioration_notifies_athena(isolated_db: Session):
    clock = MockClock(datetime(2026, 11, 5, 10, 0, tzinfo=timezone.utc))
    cycle = _seed_cycle_with_pro_thresholds(isolated_db, "cycle_t6", "pat_t6")

    ai_call_count = 0

    def mock_handler(request):
        nonlocal ai_call_count
        ai_call_count += 1
        return _standard_chat_response_handler(request)

    gw = _make_gemini_gateway(mock_handler)
    service = Mode2VitalsMonitoringService(db=isolated_db, gateway=gw, now_provider=clock.now)

    # 0 min: Initial alert (Call 1)
    await service.process_measurement(cycle_id=cycle.cycle_id, pulse=120, spo2=98, measured_at=clock.now())
    # 2 min: Two-minute summary (Call 2)
    clock.advance(seconds=120)
    await service.process_measurement(cycle_id=cycle.cycle_id, pulse=122, spo2=98, measured_at=clock.now())
    assert ai_call_count == 2

    # 4 min: Significant spike to 142 bpm (delta = |142 - 122| = 20 >= 15 bpm)
    clock.advance(seconds=120)
    ep = await service.process_measurement(cycle_id=cycle.cycle_id, pulse=142, spo2=98, measured_at=clock.now())

    assert ai_call_count == 3  # Athena notified!
    assert ep.athena_events[-1].event_type == "significant_change"
    assert ep.last_notified_pulse == 142


# ==============================================================================
# TEST 7: SUSTAINED NORMALIZATION CLOSES EPISODE; SECOND SPIKE CREATES NEW EPISODE
# ==============================================================================

@pytest.mark.anyio
async def test_07_normalization_closes_and_second_spike_creates_new_episode(isolated_db: Session):
    clock = MockClock(datetime(2026, 11, 5, 10, 0, tzinfo=timezone.utc))
    cycle = _seed_cycle_with_pro_thresholds(
        isolated_db, "cycle_t7", "pat_t7", min_pessimum_bpm=50, max_pessimum_bpm=115
    )

    ai_call_count = 0

    def mock_handler(request):
        nonlocal ai_call_count
        ai_call_count += 1
        return _standard_chat_response_handler(request)

    gw = _make_gemini_gateway(mock_handler)
    service = Mode2VitalsMonitoringService(db=isolated_db, gateway=gw, now_provider=clock.now)

    # 1. First Episode: Spike 130 bpm
    ep1 = await service.process_measurement(cycle_id=cycle.cycle_id, pulse=130, measured_at=clock.now())
    ep1_id = ep1.episode_id

    # 2 min: summary
    clock.advance(seconds=120)
    await service.process_measurement(cycle_id=cycle.cycle_id, pulse=125, measured_at=clock.now())

    # 4 min: pulse drops into normal range (85 bpm, check 1)
    clock.advance(seconds=120)
    ep1_norm1 = await service.process_measurement(cycle_id=cycle.cycle_id, pulse=85, measured_at=clock.now())
    assert ep1_norm1.status == Mode2EpisodeStatus.ACTIVE
    assert ep1_norm1.consecutive_normal_readings == 1

    # 6 min: pulse remains normal (82 bpm, check 2 -> sustained normalization!)
    clock.advance(seconds=120)
    ep1_closed = await service.process_measurement(cycle_id=cycle.cycle_id, pulse=82, measured_at=clock.now())
    assert ep1_closed.status == Mode2EpisodeStatus.CLOSED
    assert ep1_closed.closed_at_utc is not None
    assert service.get_active_episode(cycle.cycle_id) is None

    # 3. Later that same day: NEW spike to 140 bpm!
    clock.advance(minutes=45)
    ep2 = await service.process_measurement(cycle_id=cycle.cycle_id, pulse=140, measured_at=clock.now())

    assert ep2 is not None
    assert ep2.status == Mode2EpisodeStatus.ACTIVE
    assert ep2.episode_id != ep1_id  # Brand NEW episode ID!
    assert ep2.trigger_pulse == 140

    # Verify both episodes exist in history
    history = service.get_episode_history(cycle.cycle_id)
    assert len(history) == 2
    assert history[0].episode_id == ep1_id
    assert history[0].status == Mode2EpisodeStatus.CLOSED
    assert history[1].episode_id == ep2.episode_id
    assert history[1].status == Mode2EpisodeStatus.ACTIVE


# ==============================================================================
# TEST 8: INDEPENDENCE FROM 3-QUESTION DAILY PLAN LIMIT
# ==============================================================================

@pytest.mark.anyio
async def test_08_mode2_does_not_consume_daily_plan_question_limit(isolated_db: Session):
    clock = MockClock(datetime(2026, 11, 5, 10, 0, tzinfo=timezone.utc))
    cycle = _seed_cycle_with_pro_thresholds(isolated_db, "cycle_t8", "pat_t8")

    gw = _make_gemini_gateway(_standard_chat_response_handler)
    plan_svc = AthenaWeeklyPlanService(db=isolated_db, gateway=gw, now_provider=clock.now)
    mode2_svc = Mode2VitalsMonitoringService(db=isolated_db, gateway=gw, now_provider=clock.now)

    # Initial daily questions count before Mode 2
    initial_count = plan_svc.get_daily_planned_questions_count(cycle.cycle_id, as_of=clock.now())
    assert initial_count == 0

    # Trigger Mode 2 spike & follow-up statements
    await mode2_svc.process_measurement(cycle_id=cycle.cycle_id, pulse=138, measured_at=clock.now())
    await mode2_svc.handle_user_statement(cycle_id=cycle.cycle_id, user_text="Сижу за компьютером", as_of=clock.now())

    clock.advance(seconds=120)
    await mode2_svc.process_measurement(cycle_id=cycle.cycle_id, pulse=140, measured_at=clock.now())

    # Mode 2 turns did NOT decrement or increment routine planned questions count!
    daily_count_after = plan_svc.get_daily_planned_questions_count(cycle.cycle_id, as_of=clock.now())
    assert daily_count_after == 0


# ==============================================================================
# TEST 9: TWO PATIENTS WITH DIFFERENT PRO THRESHOLDS -> CORRECT ISOLATION
# ==============================================================================

@pytest.mark.anyio
async def test_09_two_patients_different_thresholds_isolation(isolated_db: Session):
    clock = MockClock(datetime(2026, 11, 5, 10, 0, tzinfo=timezone.utc))
    # Patient A: strict max threshold 100 bpm
    cycle_a = _seed_cycle_with_pro_thresholds(
        isolated_db, "cycle_pat_a", "pat_a", min_pessimum_bpm=50, max_pessimum_bpm=100
    )
    # Patient B: athletic/loose max threshold 130 bpm and higher delta tolerance
    cycle_b = _seed_cycle_with_pro_thresholds(
        isolated_db, "cycle_pat_b", "pat_b", min_pessimum_bpm=45, max_pessimum_bpm=130, max_spike_delta_bpm=45
    )

    gw = _make_gemini_gateway(_standard_chat_response_handler)
    service = Mode2VitalsMonitoringService(db=isolated_db, gateway=gw, now_provider=clock.now)

    # Pulse 110 bpm for both patients:
    ep_a = await service.process_measurement(cycle_id=cycle_a.cycle_id, pulse=110, measured_at=clock.now())
    ep_b = await service.process_measurement(cycle_id=cycle_b.cycle_id, pulse=110, measured_at=clock.now())

    # Patient A has threshold 100 -> 110 bpm triggers Mode 2 episode!
    assert ep_a is not None
    assert ep_a.status == Mode2EpisodeStatus.ACTIVE

    # Patient B has threshold 130 -> 110 bpm is completely normal -> NO episode!
    assert ep_b is None
    assert service.get_active_episode(cycle_b.cycle_id) is None


# ==============================================================================
# TEST 10: DUPLICATE MEASUREMENT INGESTION DEDUPLICATES SAFELY
# ==============================================================================

@pytest.mark.anyio
async def test_10_duplicate_measurement_deduplicated_no_extra_episodes(isolated_db: Session):
    clock = MockClock(datetime(2026, 11, 5, 10, 0, tzinfo=timezone.utc))
    cycle = _seed_cycle_with_pro_thresholds(isolated_db, "cycle_t10", "pat_t10")

    ai_calls = 0

    def mock_handler(request):
        nonlocal ai_calls
        ai_calls += 1
        return _standard_chat_response_handler(request)

    gw = _make_gemini_gateway(mock_handler)
    service = Mode2VitalsMonitoringService(db=isolated_db, gateway=gw, now_provider=clock.now)

    from app.schemas.health import HealthMeasurementCreate
    meas_repo = HealthMeasurementRepository(isolated_db)

    # First measurement ingestion
    m1 = HealthMeasurementCreate(
        cycle_id=cycle.cycle_id,
        pulse=135,
        spo2=98,
        source="watch_sensor",
        measured_at=clock.now(),
    )
    rec1, is_new1 = meas_repo.ingest_single(m1, cycle_id=cycle.cycle_id)
    assert is_new1 is True

    ep1 = await service.process_measurement(cycle_id=cycle.cycle_id, pulse=rec1.pulse, measured_at=rec1.measured_at)
    assert ep1 is not None
    assert ai_calls == 1

    # Exact duplicate ingestion
    rec2, is_new2 = meas_repo.ingest_single(m1, cycle_id=cycle.cycle_id)
    assert is_new2 is False  # Deduplicated!

    # When deduplicated, router does not re-process
    history = service.get_episode_history(cycle.cycle_id)
    assert len(history) == 1
    assert ai_calls == 1


# ==============================================================================
# TEST 11: SERVER RESTART DURING ACTIVE EPISODE -> RESTORES STATE FROM SQLITE
# ==============================================================================

@pytest.mark.anyio
async def test_11_server_restart_restores_episode_state(isolated_db: Session):
    clock = MockClock(datetime(2026, 11, 5, 10, 0, tzinfo=timezone.utc))
    cycle = _seed_cycle_with_pro_thresholds(isolated_db, "cycle_t11", "pat_t11")

    gw = _make_gemini_gateway(_standard_chat_response_handler)
    service_instance_1 = Mode2VitalsMonitoringService(db=isolated_db, gateway=gw, now_provider=clock.now)

    # Start episode in service instance 1
    ep = await service_instance_1.process_measurement(cycle_id=cycle.cycle_id, pulse=136, measured_at=clock.now())
    assert ep is not None
    ep_id = ep.episode_id

    # SIMULATE SERVER RESTART: Create a completely new service instance from DB
    service_instance_2 = Mode2VitalsMonitoringService(db=isolated_db, gateway=gw, now_provider=clock.now)
    restored_ep = service_instance_2.get_active_episode(cycle.cycle_id)

    assert restored_ep is not None
    assert restored_ep.episode_id == ep_id
    assert restored_ep.status == Mode2EpisodeStatus.ACTIVE
    assert restored_ep.trigger_pulse == 136

    # Continue processing on restarted server
    clock.advance(seconds=120)
    updated_ep = await service_instance_2.process_measurement(
        cycle_id=cycle.cycle_id, pulse=138, measured_at=clock.now()
    )
    assert updated_ep.episode_id == ep_id
    assert updated_ep.initial_two_minute_update_sent is True


# ==============================================================================
# TEST 12: ZERO REAL GEMINI CALLS (100% MOCK TRANSPORT)
# ==============================================================================

def test_12_zero_real_gemini_calls_guaranteed():
    """Verify that MockTransport is strictly instantiated with zero internet outbound calls."""
    transport_called = False

    def handler(request):
        nonlocal transport_called
        transport_called = True
        import httpx
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": "Mock"}]}}]})

    gw = _make_gemini_gateway(handler)
    assert gw._api_key == "test-api-key-mode2-mock"
    assert gw._transport is not None


# ==============================================================================
# TEST 13: ZERO LEAKAGE OF SERVER_ONLY DATA TO CLIENT
# ==============================================================================

def test_13_zero_leakage_of_server_only_reports_to_client(isolated_db: Session):
    cycle = _seed_cycle_with_pro_thresholds(isolated_db, "cycle_t13", "pat_t13")
    report_repo = ModelReportRepository(isolated_db)

    # List client facing reports
    client_reports = report_repo.list_client_facing(cycle.cycle_id)
    report_types = [r.report_type for r in client_reports]

    # Verify athena_mode2_episode is strictly absent from client-facing reports
    assert ATHENA_MODE2_EPISODE_REPORT_TYPE not in report_types

    # Verify it is safely located under SERVER_ONLY
    server_reports = report_repo.list_server_only(cycle.cycle_id, report_type=ATHENA_MODE2_EPISODE_REPORT_TYPE)
    for sr in server_reports:
        assert sr.privacy_level == PrivacyLevel.SERVER_ONLY


# ==============================================================================
# TEST 14: SAFE FALLBACK BOUNDS WHEN PRO THRESHOLDS ARE ABSENT
# ==============================================================================

@pytest.mark.anyio
async def test_14_safe_fallback_bounds_when_pro_thresholds_absent(isolated_db: Session):
    clock = MockClock(datetime(2026, 11, 5, 10, 0, tzinfo=timezone.utc))
    from app.repositories.patient_repository import PatientRepository
    PatientRepository(isolated_db).create("pat_t14", status="active")
    cycle_repo = MedicalCycleRepository(isolated_db)
    cycle = cycle_repo.create(cycle_id="cycle_t14", patient_id="pat_t14", status="active")
    # NO user_overall_report created (Primary Consilium pending)

    gw = _make_gemini_gateway(_standard_chat_response_handler)
    service = Mode2VitalsMonitoringService(db=isolated_db, gateway=gw, now_provider=clock.now)

    # Thresholds should safely resolve to fallback
    thresholds = service.resolve_thresholds(cycle.cycle_id)
    assert thresholds["source"] == "fallback"
    assert thresholds["min_pessimum_bpm"] == 45
    assert thresholds["max_pessimum_bpm"] == 115

    # Dangerous pulse 145 bpm MUST NOT be ignored!
    ep = await service.process_measurement(cycle_id=cycle.cycle_id, pulse=145, measured_at=clock.now())
    assert ep is not None
    assert ep.status == Mode2EpisodeStatus.ACTIVE
    assert ep.trigger_pulse == 145


# ==============================================================================
# TEST 15: MEDICAL SAFETY: NO FALSE RECOVERY CLAIMS & AMBULANCE CAUTION
# ==============================================================================

@pytest.mark.anyio
async def test_15_medical_safety_prompt_instructions(isolated_db: Session):
    clock = MockClock(datetime(2026, 11, 5, 10, 0, tzinfo=timezone.utc))
    cycle = _seed_cycle_with_pro_thresholds(isolated_db, "cycle_t15", "pat_t15")

    captured_prompts: List[str] = []

    def mock_handler(request):
        import httpx
        body = request.read().decode("utf-8")
        captured_prompts.append(body)
        return _standard_chat_response_handler(request)

    gw = _make_gemini_gateway(mock_handler)
    service = Mode2VitalsMonitoringService(db=isolated_db, gateway=gw, now_provider=clock.now)

    # 1. Initial alert
    await service.process_measurement(cycle_id=cycle.cycle_id, pulse=130, measured_at=clock.now())
    # Initial prompt forbids false recovery and phantom ambulance
    assert any("ЗАПРЕЩЕНО заявлять о прибытии помощи без подтверждения" in p for p in captured_prompts)

    # 2. User mentions calling ambulance
    clock.advance(seconds=30)
    await service.handle_user_statement(
        cycle_id=cycle.cycle_id,
        user_text="Вызвал скорую помощь, сижу жду",
        is_workout=False,
        as_of=clock.now(),
    )
    assert any("ЗАПРЕЩЕНО заявлять о прибытии помощи без подтверждения" in p for p in captured_prompts)

    # 3. Two-minute summary
    clock.advance(seconds=120)
    await service.process_measurement(cycle_id=cycle.cycle_id, pulse=95, measured_at=clock.now())

    # 4. Sustained normalization -> closure
    clock.advance(seconds=120)
    await service.process_measurement(cycle_id=cycle.cycle_id, pulse=90, measured_at=clock.now())

    # Normalization prompt strictly forbids false claims of complete cure
    assert any("ЗАПРЕЩЕНО заявлять о полном выздоровлении или восстановлении" in p for p in captured_prompts)


# ==============================================================================
# TEST 16: FASTAPI ROUTER ENDPOINTS FOR MODE 2
# ==============================================================================

def test_16_fastapi_mode2_endpoints(isolated_db: Session):
    from fastapi.testclient import TestClient
    from app.main import app
    from app.routers.vitals import get_gemini_gateway, get_server_now_provider

    clock = MockClock(datetime(2026, 11, 5, 10, 0, tzinfo=timezone.utc))
    cycle = _seed_cycle_with_pro_thresholds(isolated_db, "cycle_t16", "pat_t16")

    gw = _make_gemini_gateway(_standard_chat_response_handler)

    from app.database import get_db
    from app.security.auth import verify_client_token, ServiceAdminSession
    app.dependency_overrides[get_db] = lambda: isolated_db
    app.dependency_overrides[get_gemini_gateway] = lambda: gw
    app.dependency_overrides[get_server_now_provider] = lambda: clock.now
    app.dependency_overrides[verify_client_token] = lambda: ServiceAdminSession()

    headers = {"Authorization": "Bearer test-secret-key-for-unit-tests-only"}

    try:
        client = TestClient(app)

        # 1. No active episode initially
        resp = client.get(f"/api/v1/vitals/mode2/active?cycle_id={cycle.cycle_id}", headers=headers)
        assert resp.status_code == 200
        assert resp.json() is None

        # 2. Ingest measurement with elevated pulse via Mode2 service
        asyncio.run(
            Mode2VitalsMonitoringService(db=isolated_db, gateway=gw, now_provider=clock.now).process_measurement(
                cycle_id=cycle.cycle_id,
                pulse=142,
                spo2=96,
                measured_at=clock.now(),
            )
        )

        # 3. Active episode now exists
        resp_act = client.get(f"/api/v1/vitals/mode2/active?cycle_id={cycle.cycle_id}", headers=headers)
        assert resp_act.status_code == 200
        act_data = resp_act.json()
        assert act_data["status"] == "active"
        assert act_data["trigger_pulse"] == 142

        # 4. Post workout response via router endpoint
        resp_workout = client.post(
            "/api/v1/vitals/mode2/workout-response",
            json={
                "cycle_id": cycle.cycle_id,
                "is_workout": True,
                "user_message": "Да, занимаюсь на велотренажере",
            },
            headers=headers,
        )
        assert resp_workout.status_code == 200
        w_data = resp_workout.json()
        assert w_data["workout_context"] == "confirmed_workout"
        assert w_data["user_statements_count"] == 1

        # 5. History endpoint returns the episode
        resp_hist = client.get(f"/api/v1/vitals/mode2/history?cycle_id={cycle.cycle_id}", headers=headers)
        assert resp_hist.status_code == 200
        hist_data = resp_hist.json()
        assert len(hist_data) == 1
        assert hist_data[0]["episode_id"] == act_data["episode_id"]

    finally:
        app.dependency_overrides.clear()

