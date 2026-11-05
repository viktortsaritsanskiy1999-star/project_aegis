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
from app.models import MedicalCycle, HealthMeasurement
from app.models.model_report import ModelReport
from app.repositories.cycle_repository import MedicalCycleRepository
from app.repositories.measurement_repository import HealthMeasurementRepository
from app.repositories.report_repository import ModelReportRepository
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
from app.services.athena_chat_service import AthenaChatService
from app.services.gemini_gateway import GeminiGateway, GeminiGatewayError, GeminiModelRole
from app.services.mode2_vitals_service import (
    ATHENA_MODE2_EPISODE_REPORT_TYPE,
    Mode2VitalsMonitoringService,
)
from app.services.weekly_consilium_service import WeeklyConsiliumService
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


def _standard_athena_chat_handler(req: httpx.Request):
    reply_text = (
        "Пожалуйста, присядьте, обеспечьте доступ свежего воздуха. Вы тренируетесь или отдыхаете?\n\n"
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
def sqlite_db_session():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    session_factory = sessionmaker(bind=engine)
    session = session_factory()
    try:
        yield session, session_factory
    finally:
        session.close()


def _setup_patient_and_cycle(
    db: Session,
    patient_id: str = "pat_wellness_mode2",
    cycle_id: str = "cycle_wellness_mode2",
    t0: Optional[datetime] = None,
) -> datetime:
    from app.repositories.patient_repository import PatientRepository
    patient_repo = PatientRepository(db)
    if not patient_repo.get(patient_id):
        patient_repo.create(patient_id=patient_id, status="active")

    t0_dt = t0 or datetime(2026, 11, 2, 8, 0, 0, tzinfo=timezone.utc)
    cycle_repo = MedicalCycleRepository(db)
    cycle = cycle_repo.create(
        cycle_id=cycle_id,
        patient_id=patient_id,
        status="active",
    )

    weekly_svc = WeeklyConsiliumService(db=db, gateway=_make_gemini_gateway(_standard_athena_chat_handler))
    weekly_svc.record_schedule_anchor(
        cycle_id=cycle_id,
        anchor_dt=t0_dt,
        commit=True,
    )
    return t0_dt


# ==============================================================================
# TEST 1: HR SPIKE WITHOUT WORKOUT -> NOT_EXERCISING
# ==============================================================================
@pytest.mark.anyio
async def test_01_hr_spike_without_workout_context_not_exercising(sqlite_db_session):
    s, _ = sqlite_db_session
    t0 = _setup_patient_and_cycle(s)
    clock = MockClock(t0 + timedelta(hours=2))

    gw = _make_gemini_gateway(_standard_athena_chat_handler)
    mode2_svc = Mode2VitalsMonitoringService(db=s, gateway=gw, now_provider=clock.now)

    # Ingest baseline points then spike to 142 bpm outside of workout
    for p in [70, 72, 71]:
        await mode2_svc.process_measurement(
            cycle_id="cycle_wellness_mode2",
            pulse=p,
            measured_at=clock.now(),
        )
        clock.advance(minutes=1)

    ep = await mode2_svc.process_measurement(
        cycle_id="cycle_wellness_mode2",
        pulse=142,
        measured_at=clock.now(),
    )
    assert ep is not None

    # User replies: "Нет, я сижу на работе за компьютером" (is_workout=False)
    clock.advance(minutes=1)
    await mode2_svc.handle_user_statement(
        cycle_id="cycle_wellness_mode2",
        user_text="Нет, я сижу на работе за компьютером",
        is_workout=False,
        as_of=clock.now(),
    )

    # Run Wellness Daily Service for Day 1
    def flash_wellness_handler(req: httpx.Request):
        body = req.read().decode("utf-8")
        assert "not_working_out" in body
        assert "сижу на работе" in body
        return httpx.Response(200, json=_gemini_structured_envelope({
            "observed_trends": ["Скачок пульса до 142 уд/мин в покое без физической нагрузки"],
            "limitations_and_gaps": [],
            "questionable_measurements": [],
            "episode_evaluations": [
                {
                    "episode_id": ep.episode_id,
                    "workout_context": "not_working_out",
                    "analysis": "Эпизод тахикардии в покое без тренировки; рекомендовано наблюдение."
                }
            ],
            "summary_for_athena": "Зафиксирован скачок пульса в покое до 142 уд/мин (контекст: не тренируется).",
        }))

    wellness_gw = _make_gemini_gateway(flash_wellness_handler)
    wellness_svc = WellnessDailyService(
        db=s, gateway=wellness_gw, now_provider=clock.now, mode2_service=mode2_svc
    )

    report, is_new = await wellness_svc.process_wellness_for_day(
        cycle_id="cycle_wellness_mode2",
        calendar_week=1,
        calendar_day=1,
        as_of=clock.now(),
    )

    assert is_new is True
    assert report.status == WellnessStatus.READY
    assert len(report.factual_summary.mode2_episodes) == 1
    ep_sum = report.factual_summary.mode2_episodes[0]
    assert ep_sum.user_facts.workout_context == "not_working_out"
    assert "Нет, я сижу на работе за компьютером" in ep_sum.user_facts.user_statements
    assert ep_sum.objective_metrics.trigger_pulse == 142
    assert ep_sum.objective_metrics.max_pulse == 142


# ==============================================================================
# TEST 2: CONFIRMED WORKOUT -> EXERCISING
# ==============================================================================
@pytest.mark.anyio
async def test_02_confirmed_workout_context_exercising(sqlite_db_session):
    s, _ = sqlite_db_session
    t0 = _setup_patient_and_cycle(s)
    clock = MockClock(t0 + timedelta(hours=3))

    gw = _make_gemini_gateway(_standard_athena_chat_handler)
    mode2_svc = Mode2VitalsMonitoringService(db=s, gateway=gw, now_provider=clock.now)

    for p in [68, 70, 69]:
        await mode2_svc.process_measurement(
            cycle_id="cycle_wellness_mode2", pulse=p, measured_at=clock.now()
        )
        clock.advance(minutes=1)

    ep = await mode2_svc.process_measurement(
        cycle_id="cycle_wellness_mode2", pulse=150, measured_at=clock.now(), phase="workout"
    )

    # User confirms workout
    clock.advance(minutes=1)
    await mode2_svc.handle_user_statement(
        cycle_id="cycle_wellness_mode2",
        user_text="Да, бегу на дорожке в спортзале",
        is_workout=True,
        as_of=clock.now(),
    )

    def flash_wellness_handler(req: httpx.Request):
        body = req.read().decode("utf-8")
        assert "confirmed_workout" in body
        assert "бегу на дорожке" in body
        return httpx.Response(200, json=_gemini_structured_envelope({
            "observed_trends": ["Адекватный прирост ЧСС до 150 уд/мин во время тренировки"],
            "limitations_and_gaps": [],
            "questionable_measurements": [],
            "episode_evaluations": [
                {
                    "episode_id": ep.episode_id,
                    "workout_context": "confirmed_workout",
                    "analysis": "Физиологический подъем ЧСС на фоне бега в спортзале."
                }
            ],
            "summary_for_athena": "Тренировка подтверждена, пик ЧСС 150 уд/мин.",
        }))

    wellness_svc = WellnessDailyService(
        db=s, gateway=_make_gemini_gateway(flash_wellness_handler), now_provider=clock.now, mode2_service=mode2_svc
    )
    report, _ = await wellness_svc.process_wellness_for_day(
        cycle_id="cycle_wellness_mode2", calendar_week=1, calendar_day=1, as_of=clock.now()
    )

    assert report.status == WellnessStatus.READY
    ep_sum = report.factual_summary.mode2_episodes[0]
    assert ep_sum.user_facts.workout_context == "confirmed_workout"
    assert "Да, бегу на дорожке в спортзале" in ep_sum.user_facts.user_statements


# ==============================================================================
# TEST 3: NO ANSWER -> UNKNOWN, WITHOUT FABRICATED WORKOUT
# ==============================================================================
@pytest.mark.anyio
async def test_03_no_answer_context_unknown_no_fabricated_workout(sqlite_db_session):
    s, _ = sqlite_db_session
    t0 = _setup_patient_and_cycle(s)
    clock = MockClock(t0 + timedelta(hours=4))

    gw = _make_gemini_gateway(_standard_athena_chat_handler)
    mode2_svc = Mode2VitalsMonitoringService(db=s, gateway=gw, now_provider=clock.now)

    for p in [65, 66, 65]:
        await mode2_svc.process_measurement(
            cycle_id="cycle_wellness_mode2", pulse=p, measured_at=clock.now()
        )
        clock.advance(minutes=1)

    ep = await mode2_svc.process_measurement(
        cycle_id="cycle_wellness_mode2", pulse=145, measured_at=clock.now()
    )
    assert ep.workout_context == Mode2WorkoutContext.UNKNOWN

    # No user statement arrives
    clock.advance(hours=2)

    def flash_wellness_handler(req: httpx.Request):
        body = req.read().decode("utf-8")
        assert "unknown" in body
        return httpx.Response(200, json=_gemini_structured_envelope({
            "observed_trends": ["Скачок ЧСС до 145 уд/мин без подтвержденного контекста"],
            "limitations_and_gaps": ["Контекст физической нагрузки неизвестен: пользователь не ответил на уточнение"],
            "questionable_measurements": [],
            "episode_evaluations": [
                {
                    "episode_id": ep.episode_id,
                    "workout_context": "unknown",
                    "analysis": "Контекст неизвестен, подтверждение тренировки отсутствует."
                }
            ],
            "summary_for_athena": "Скачок ЧСС до 145 уд/мин, статус нагрузки unknown.",
        }))

    wellness_svc = WellnessDailyService(
        db=s, gateway=_make_gemini_gateway(flash_wellness_handler), now_provider=clock.now, mode2_service=mode2_svc
    )
    report, _ = await wellness_svc.process_wellness_for_day(
        cycle_id="cycle_wellness_mode2", calendar_week=1, calendar_day=1, as_of=clock.now()
    )

    ep_sum = report.factual_summary.mode2_episodes[0]
    assert ep_sum.user_facts.workout_context == "unknown"
    assert len(ep_sum.user_facts.user_statements) == 0


# ==============================================================================
# TEST 4: FIRST EPISODE CLOSES AND SECOND SPIKE -> TWO INDEPENDENT EPISODE_IDS
# ==============================================================================
@pytest.mark.anyio
async def test_04_first_episode_closure_and_second_spike_creates_two_independent_episode_ids(sqlite_db_session):
    s, _ = sqlite_db_session
    t0 = _setup_patient_and_cycle(s)
    clock = MockClock(t0 + timedelta(hours=1))

    gw = _make_gemini_gateway(_standard_athena_chat_handler)
    mode2_svc = Mode2VitalsMonitoringService(db=s, gateway=gw, now_provider=clock.now)

    # Baseline
    for p in [70, 70, 70]:
        await mode2_svc.process_measurement("cycle_wellness_mode2", p, measured_at=clock.now())
        clock.advance(minutes=1)

    # Spike 1
    ep1 = await mode2_svc.process_measurement("cycle_wellness_mode2", 144, measured_at=clock.now())
    clock.advance(minutes=2)
    # Send 2-minute summary trigger
    await mode2_svc.process_measurement("cycle_wellness_mode2", 140, measured_at=clock.now())
    # Normalize episode 1: 2 consecutive normal readings
    clock.advance(minutes=2)
    await mode2_svc.process_measurement("cycle_wellness_mode2", 72, measured_at=clock.now())
    clock.advance(minutes=2)
    closed_ep1 = await mode2_svc.process_measurement("cycle_wellness_mode2", 70, measured_at=clock.now())
    assert closed_ep1.status == Mode2EpisodeStatus.CLOSED
    assert closed_ep1.closed_at_utc is not None

    # Time passes to evening on Day 1
    clock.set(t0 + timedelta(hours=9))

    # Spike 2
    ep2 = await mode2_svc.process_measurement("cycle_wellness_mode2", 155, measured_at=clock.now())
    assert ep2.status == Mode2EpisodeStatus.ACTIVE
    assert ep2.episode_id != ep1.episode_id

    # Check Wellness summary
    episodes_for_day = mode2_svc.get_wellness_episodes_for_window(
        "cycle_wellness_mode2", t0, t0 + timedelta(days=1)
    )
    assert len(episodes_for_day) == 2
    assert episodes_for_day[0].episode_id == ep1.episode_id
    assert episodes_for_day[1].episode_id == ep2.episode_id
    assert episodes_for_day[0].model_context.is_normalized is True
    assert episodes_for_day[1].model_context.is_normalized is False


# ==============================================================================
# TEST 5: SEPARATE HR DYNAMICS FOR TWO EPISODES
# ==============================================================================
@pytest.mark.anyio
async def test_05_separate_hr_dynamics_for_two_episodes(sqlite_db_session):
    s, _ = sqlite_db_session
    t0 = _setup_patient_and_cycle(s)
    clock = MockClock(t0 + timedelta(hours=1))

    gw = _make_gemini_gateway(_standard_athena_chat_handler)
    mode2_svc = Mode2VitalsMonitoringService(db=s, gateway=gw, now_provider=clock.now)

    # Episode 1: baseline 65, peak 142
    for p in [65, 65, 65]:
        await mode2_svc.process_measurement("cycle_wellness_mode2", p, measured_at=clock.now())
        clock.advance(minutes=1)
    ep1 = await mode2_svc.process_measurement("cycle_wellness_mode2", 142, measured_at=clock.now())
    clock.advance(minutes=2)
    await mode2_svc.process_measurement("cycle_wellness_mode2", 145, measured_at=clock.now())
    clock.advance(minutes=2)
    # Close ep 1
    await mode2_svc.process_measurement("cycle_wellness_mode2", 66, measured_at=clock.now())
    clock.advance(minutes=1)
    await mode2_svc.process_measurement("cycle_wellness_mode2", 65, measured_at=clock.now())

    # Episode 2: evening, peak 165
    clock.set(t0 + timedelta(hours=11))
    for p in [72, 72, 72]:
        await mode2_svc.process_measurement("cycle_wellness_mode2", p, measured_at=clock.now())
        clock.advance(minutes=1)
    ep2 = await mode2_svc.process_measurement("cycle_wellness_mode2", 165, measured_at=clock.now())

    episodes = mode2_svc.get_wellness_episodes_for_window(
        "cycle_wellness_mode2", t0, t0 + timedelta(days=1)
    )
    assert len(episodes) == 2
    sum1, sum2 = episodes[0], episodes[1]

    # Verify separate HR dynamics
    assert sum1.objective_metrics.trigger_pulse == 142
    assert sum1.objective_metrics.max_pulse == 145
    assert sum2.objective_metrics.trigger_pulse == 165
    assert sum2.objective_metrics.max_pulse == 165
    assert sum1.objective_metrics.max_pulse != sum2.objective_metrics.max_pulse


# ==============================================================================
# TEST 6: WORKOUT AND RECOVERY ACTUAL MONITORING INTERVALS
# ==============================================================================
@pytest.mark.anyio
async def test_06_workout_and_recovery_actual_monitoring_intervals(sqlite_db_session):
    s, _ = sqlite_db_session
    t0 = _setup_patient_and_cycle(s)
    clock = MockClock(t0 + timedelta(hours=2))

    gw = _make_gemini_gateway(_standard_athena_chat_handler)
    mode2_svc = Mode2VitalsMonitoringService(db=s, gateway=gw, now_provider=clock.now)

    for p in [70, 70, 70]:
        await mode2_svc.process_measurement("cycle_wellness_mode2", p, measured_at=clock.now())
        clock.advance(minutes=1)

    # Start workout
    await mode2_svc.process_measurement(
        "cycle_wellness_mode2", 145, measured_at=clock.now(), phase="workout"
    )
    await mode2_svc.handle_user_statement(
        "cycle_wellness_mode2", "Тренируюсь", is_workout=True, as_of=clock.now()
    )

    # 1-min sampling during workout
    for p in [148, 150, 152]:
        clock.advance(minutes=1)
        await mode2_svc.process_measurement(
            "cycle_wellness_mode2", p, measured_at=clock.now(), phase="workout"
        )

    # 2-min sampling during early recovery (recovery_0_10)
    for p in [130, 125, 120]:
        clock.advance(minutes=2)
        await mode2_svc.process_measurement(
            "cycle_wellness_mode2", p, measured_at=clock.now(), phase="recovery_0_10"
        )

    # 5-min sampling during mid recovery (recovery_10_50)
    for p in [118, 116, 75, 70]:
        clock.advance(minutes=5)
        await mode2_svc.process_measurement(
            "cycle_wellness_mode2", p, measured_at=clock.now(), phase="recovery_10_50"
        )

    episodes = mode2_svc.get_wellness_episodes_for_window(
        "cycle_wellness_mode2", t0, t0 + timedelta(days=1)
    )
    assert len(episodes) == 1
    sum_ep = episodes[0]
    assert sum_ep.objective_metrics.duration_minutes >= 20.0
    assert sum_ep.objective_metrics.telemetry_samples_count >= 8
    # No gaps flagged because intervals matched schedule
    assert len(sum_ep.objective_metrics.telemetry_gaps) == 0


# ==============================================================================
# TEST 7: SPO2 PRESERVED WHEN AVAILABLE
# ==============================================================================
@pytest.mark.anyio
async def test_07_spo2_preserved_when_available(sqlite_db_session):
    s, _ = sqlite_db_session
    t0 = _setup_patient_and_cycle(s)
    clock = MockClock(t0 + timedelta(hours=2))

    gw = _make_gemini_gateway(_standard_athena_chat_handler)
    mode2_svc = Mode2VitalsMonitoringService(db=s, gateway=gw, now_provider=clock.now)

    for p in [68, 70, 69]:
        await mode2_svc.process_measurement("cycle_wellness_mode2", p, spo2=98, measured_at=clock.now())
        clock.advance(minutes=1)

    await mode2_svc.process_measurement("cycle_wellness_mode2", 145, spo2=97, measured_at=clock.now())
    clock.advance(minutes=2)
    await mode2_svc.process_measurement("cycle_wellness_mode2", 142, spo2=96, measured_at=clock.now())

    episodes = mode2_svc.get_wellness_episodes_for_window(
        "cycle_wellness_mode2", t0, t0 + timedelta(days=1)
    )
    sum_ep = episodes[0]
    assert sum_ep.objective_metrics.spo2_min == 96
    assert sum_ep.objective_metrics.spo2_max == 97
    assert len(sum_ep.objective_metrics.spo2_history) >= 2


# ==============================================================================
# TEST 8: ABSENCE OF SPO2 WITHOUT FICTITIOUS VALUES
# ==============================================================================
@pytest.mark.anyio
async def test_08_absence_of_spo2_no_fictitious_values(sqlite_db_session):
    s, _ = sqlite_db_session
    t0 = _setup_patient_and_cycle(s)
    clock = MockClock(t0 + timedelta(hours=2))

    gw = _make_gemini_gateway(_standard_athena_chat_handler)
    mode2_svc = Mode2VitalsMonitoringService(db=s, gateway=gw, now_provider=clock.now)

    for p in [68, 70, 69]:
        await mode2_svc.process_measurement("cycle_wellness_mode2", p, spo2=None, measured_at=clock.now())
        clock.advance(minutes=1)

    await mode2_svc.process_measurement("cycle_wellness_mode2", 145, spo2=None, measured_at=clock.now())

    episodes = mode2_svc.get_wellness_episodes_for_window(
        "cycle_wellness_mode2", t0, t0 + timedelta(days=1)
    )
    sum_ep = episodes[0]
    assert sum_ep.objective_metrics.spo2_min is None
    assert sum_ep.objective_metrics.spo2_max is None
    assert sum_ep.objective_metrics.spo2_history == []


# ==============================================================================
# TEST 9: MULTIPLE EPISODES REFLECTED IN WELLNESS DAILY REPORT
# ==============================================================================
@pytest.mark.anyio
async def test_09_multiple_episodes_reflected_in_wellness_daily_report(sqlite_db_session):
    s, _ = sqlite_db_session
    t0 = _setup_patient_and_cycle(s)
    clock = MockClock(t0 + timedelta(hours=1))

    gw = _make_gemini_gateway(_standard_athena_chat_handler)
    mode2_svc = Mode2VitalsMonitoringService(db=s, gateway=gw, now_provider=clock.now)

    for p in [65, 65, 65]:
        await mode2_svc.process_measurement("cycle_wellness_mode2", p, measured_at=clock.now())
        clock.advance(minutes=1)

    # Ep 1: Morning resting spike
    ep1 = await mode2_svc.process_measurement("cycle_wellness_mode2", 142, measured_at=clock.now())
    await mode2_svc.handle_user_statement("cycle_wellness_mode2", "Не тренируюсь", is_workout=False, as_of=clock.now())
    clock.advance(minutes=3)
    await mode2_svc.process_measurement("cycle_wellness_mode2", 68, measured_at=clock.now())
    clock.advance(minutes=1)
    await mode2_svc.process_measurement("cycle_wellness_mode2", 66, measured_at=clock.now())

    # Ep 2: Evening exercise spike
    clock.set(t0 + timedelta(hours=10))
    ep2 = await mode2_svc.process_measurement("cycle_wellness_mode2", 155, measured_at=clock.now(), phase="workout")
    await mode2_svc.handle_user_statement("cycle_wellness_mode2", "Тренировка кроссфит", is_workout=True, as_of=clock.now())
    clock.advance(minutes=30)

    def flash_wellness_handler(req: httpx.Request):
        body = req.read().decode("utf-8")
        assert ep1.episode_id in body
        assert ep2.episode_id in body
        return httpx.Response(200, json=_gemini_structured_envelope({
            "observed_trends": [
                "Утренний скачок пульса до 142 уд/мин в покое (нормализовался)",
                "Вечерний тренировочный подъем пульса до 155 уд/мин"
            ],
            "limitations_and_gaps": [],
            "questionable_measurements": [],
            "episode_evaluations": [
                {
                    "episode_id": ep1.episode_id,
                    "workout_context": "not_working_out",
                    "analysis": "Утренний эпизод вне нагрузки с быстрой нормализацией."
                },
                {
                    "episode_id": ep2.episode_id,
                    "workout_context": "confirmed_workout",
                    "analysis": "Вечерняя тренировка кроссфит с адекватным кардио-откликом."
                }
            ],
            "summary_for_athena": "За день 2 независимых эпизода: утренний скачок в покое (142 уд/мин) и вечерняя тренировка (155 уд/мин).",
        }))

    wellness_svc = WellnessDailyService(
        db=s, gateway=_make_gemini_gateway(flash_wellness_handler), now_provider=clock.now, mode2_service=mode2_svc
    )

    report, _ = await wellness_svc.process_wellness_for_day(
        cycle_id="cycle_wellness_mode2", calendar_week=1, calendar_day=1, as_of=clock.now()
    )

    assert report.status == WellnessStatus.READY
    assert len(report.factual_summary.mode2_episodes) == 2
    assert len(report.interpretation.episode_evaluations) == 2
    assert report.interpretation.episode_evaluations[0].workout_context == "not_working_out"
    assert report.interpretation.episode_evaluations[1].workout_context == "confirmed_workout"


# ==============================================================================
# TEST 10: NO DUPLICATION ON REPEATED TELEMETRY IMPORT
# ==============================================================================
@pytest.mark.anyio
async def test_10_no_duplication_on_repeated_telemetry_import(sqlite_db_session):
    s, _ = sqlite_db_session
    t0 = _setup_patient_and_cycle(s)
    clock = MockClock(t0 + timedelta(hours=2))

    meas_repo = HealthMeasurementRepository(s)
    create_dto = HealthMeasurementCreate(
        idempotency_key="idempotent_vitals_key_101",
        cycle_id="cycle_wellness_mode2",
        pulse=145,
        source="watch_sensor",
        measured_at=clock.now(),
    )

    rec1, is_new1 = meas_repo.ingest_single(create_dto, cycle_id="cycle_wellness_mode2")
    assert is_new1 is True

    # Re-ingest exact same measurement
    rec2, is_new2 = meas_repo.ingest_single(create_dto, cycle_id="cycle_wellness_mode2")
    assert is_new2 is False
    assert rec1.id == rec2.id


# ==============================================================================
# TEST 11: HISTORY RESTORED AFTER SERVER RESTART
# ==============================================================================
@pytest.mark.anyio
async def test_11_history_restored_after_server_restart(sqlite_db_session):
    s, session_factory = sqlite_db_session
    t0 = _setup_patient_and_cycle(s)
    clock = MockClock(t0 + timedelta(hours=2))

    gw = _make_gemini_gateway(_standard_athena_chat_handler)
    mode2_svc1 = Mode2VitalsMonitoringService(db=s, gateway=gw, now_provider=clock.now)

    for p in [70, 70, 70]:
        await mode2_svc1.process_measurement("cycle_wellness_mode2", p, measured_at=clock.now())
        clock.advance(minutes=1)

    ep = await mode2_svc1.process_measurement("cycle_wellness_mode2", 146, measured_at=clock.now())
    await mode2_svc1.handle_user_statement(
        "cycle_wellness_mode2", "Чувствую слабость", is_workout=False, as_of=clock.now()
    )

    # Simulate server reboot: create brand new DB session and fresh service instances
    new_db_session = session_factory()
    try:
        mode2_svc2 = Mode2VitalsMonitoringService(db=new_db_session, gateway=gw, now_provider=clock.now)
        restored_episodes = mode2_svc2.get_wellness_episodes_for_window(
            "cycle_wellness_mode2", t0, t0 + timedelta(days=1)
        )
        assert len(restored_episodes) == 1
        res = restored_episodes[0]
        assert res.episode_id == ep.episode_id
        assert res.objective_metrics.trigger_pulse == 146
        assert res.user_facts.workout_context == "not_working_out"
        assert "Чувствую слабость" in res.user_facts.user_statements
        assert "сильная слабость" in res.user_facts.confirmed_symptoms or len(res.user_facts.user_statements) > 0
    finally:
        new_db_session.close()


# ==============================================================================
# TEST 12: ZERO NEW GEMINI CALLS ON MINOR FLUCTUATIONS
# ==============================================================================
@pytest.mark.anyio
async def test_12_zero_new_gemini_calls_on_minor_fluctuations(sqlite_db_session):
    s, _ = sqlite_db_session
    t0 = _setup_patient_and_cycle(s)
    clock = MockClock(t0 + timedelta(hours=2))

    gemini_calls = []

    def tracking_handler(req: httpx.Request):
        gemini_calls.append(req)
        return _standard_athena_chat_handler(req)

    gw = _make_gemini_gateway(tracking_handler)
    mode2_svc = Mode2VitalsMonitoringService(db=s, gateway=gw, now_provider=clock.now)

    # Baseline
    for p in [70, 70, 70]:
        await mode2_svc.process_measurement("cycle_wellness_mode2", p, measured_at=clock.now())
        clock.advance(minutes=1)
    assert len(gemini_calls) == 0

    # Trigger spike -> 1 Athena call
    await mode2_svc.process_measurement("cycle_wellness_mode2", 145, measured_at=clock.now())
    assert len(gemini_calls) == 1

    # Minor fluctuations (< 15 bpm delta from trigger)
    for p in [146, 144, 147, 145]:
        clock.advance(seconds=15)
        await mode2_svc.process_measurement("cycle_wellness_mode2", p, measured_at=clock.now())

    # Still exactly 1 call! Minor fluctuations are logged silently without calling Gemini
    assert len(gemini_calls) == 1


# ==============================================================================
# TEST 13: PRESERVATION OF SERVER_ONLY PRIVACY
# ==============================================================================
@pytest.mark.anyio
async def test_13_server_only_privacy_preserved(sqlite_db_session):
    s, _ = sqlite_db_session
    t0 = _setup_patient_and_cycle(s)
    clock = MockClock(t0 + timedelta(hours=2))

    gw = _make_gemini_gateway(_standard_athena_chat_handler)
    mode2_svc = Mode2VitalsMonitoringService(db=s, gateway=gw, now_provider=clock.now)

    for p in [70, 70, 70]:
        await mode2_svc.process_measurement("cycle_wellness_mode2", p, measured_at=clock.now())
        clock.advance(minutes=1)

    await mode2_svc.process_measurement("cycle_wellness_mode2", 144, measured_at=clock.now())

    def flash_wellness_handler(req: httpx.Request):
        return httpx.Response(200, json=_gemini_structured_envelope({
            "observed_trends": ["Подъем пульса 144"],
            "limitations_and_gaps": [],
            "questionable_measurements": [],
            "episode_evaluations": [],
            "summary_for_athena": "Эпизод 144 уд/мин",
        }))

    wellness_svc = WellnessDailyService(
        db=s, gateway=_make_gemini_gateway(flash_wellness_handler), now_provider=clock.now, mode2_service=mode2_svc
    )
    await wellness_svc.process_wellness_for_day("cycle_wellness_mode2", 1, 1, as_of=clock.now())

    report_repo = ModelReportRepository(s)
    # Check client-facing reports: should NOT contain internal Mode 2 or Wellness daily reports
    client_mode2 = report_repo.list_client_facing(
        cycle_id="cycle_wellness_mode2", report_type=ATHENA_MODE2_EPISODE_REPORT_TYPE
    )
    assert len(client_mode2) == 0

    client_wellness = report_repo.list_client_facing(
        cycle_id="cycle_wellness_mode2", report_type=WELLNESS_DAILY_REPORT_TYPE
    )
    assert len(client_wellness) == 0

    # Check server-only reports: both are safely stored
    server_mode2 = report_repo.list_server_only(
        cycle_id="cycle_wellness_mode2", report_type=ATHENA_MODE2_EPISODE_REPORT_TYPE
    )
    assert len(server_mode2) > 0
    assert server_mode2[0].privacy_level == PrivacyLevel.SERVER_ONLY

    server_wellness = report_repo.list_server_only(
        cycle_id="cycle_wellness_mode2", report_type=WELLNESS_DAILY_REPORT_TYPE
    )
    assert len(server_wellness) > 0
    assert server_wellness[0].privacy_level == PrivacyLevel.SERVER_ONLY
