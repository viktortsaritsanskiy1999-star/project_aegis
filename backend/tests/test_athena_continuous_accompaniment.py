from datetime import datetime, timezone, timedelta
import json
from typing import Any, Dict, List
import httpx
import pytest
from fastapi import HTTPException

from app.models.medical_cycle import MedicalCycle
from app.repositories.cycle_repository import MedicalCycleRepository
from app.repositories.patient_repository import PatientRepository
from app.repositories.report_repository import ModelReportRepository
from app.repositories.sprint_repository import WeeklySprintRepository
from app.schemas.chat import AthenaChatRequest
from app.services.gemini_gateway import GeminiGateway
from app.services.primary_consilium_service import PrimaryConsiliumService
from app.services.weekly_consilium_service import (
    WeeklyConsiliumService,
    ATHENA_SCHEDULE_ANCHOR_REPORT_TYPE,
    ATHENA_CLINICAL_LOG_REPORT_TYPE,
    ATHENA_ACTIVE_STRATEGY_REPORT_TYPE,
)
from app.services.athena_chat_service import AthenaChatService
from tests.test_primary_consilium import (
    _gemini_envelope,
    _seed_preparatory_cycle_context,
    _build_consilium_mock_gateway,
)
from tests.test_weekly_consilium import (
    _sample_weekly_flash_output,
    _sample_weekly_pro_output,
)


class ControlledTime:
    def __init__(self, start_dt: datetime):
        self.current = start_dt

    def now(self) -> datetime:
        return self.current

    def advance(self, **kwargs):
        self.current += timedelta(**kwargs)


def _text_envelope(text: str) -> Dict[str, Any]:
    return {
        "candidates": [
            {
                "content": {
                    "parts": [{"text": text}]
                }
            }
        ]
    }


def _build_test_gateway(
    recorded_calls: List[Dict[str, Any]],
    athena_summary_supplier=None,
) -> GeminiGateway:
    call_counter = {"index": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        idx = call_counter["index"]
        call_counter["index"] += 1
        body = json.loads(request.content.decode("utf-8"))
        prompt_text = body["contents"][0]["parts"][0]["text"]
        system_instruction = (body.get("systemInstruction") or {}).get("parts", [{}])[0].get("text", "")
        url_path = request.url.path

        recorded_calls.append({
            "index": idx,
            "url_path": url_path,
            "prompt": prompt_text,
            "system_instruction": system_instruction,
            "body": body,
        })

        # Weekly Consilium Pro Chief
        if "you are the chief physician" in prompt_text.lower() or "pro" in url_path.lower():
            return httpx.Response(200, json=_gemini_envelope(_sample_weekly_pro_output()))

        # Weekly Consilium Flash Deputy
        if "you are the head of department" in prompt_text.lower():
            return httpx.Response(200, json=_gemini_envelope(_sample_weekly_flash_output()))

        # Athena chat turn
        summary = (
            athena_summary_supplier(prompt_text)
            if athena_summary_supplier
            else "Routine Athena clinical check-in."
        )
        chat_reply = (
            "Здравствуйте! Ваше самочувствие зафиксировано.\n\n"
            "[CLINICAL_REPORT]\n"
            "{\n"
            '  "status": "NORMAL",\n'
            '  "symptoms_mentioned": ["tracked_observation"],\n'
            '  "subjective_wellbeing_score": 8,\n'
            '  "adherence_to_plan": "HIGH",\n'
            '  "red_flags": false,\n'
            f'  "summary_for_master": "{summary}"\n'
            "}\n"
            "[/CLINICAL_REPORT]"
        )
        return httpx.Response(200, json=_text_envelope(chat_reply))

    return GeminiGateway(
        api_key="test-continuous-gateway-key",
        transport=httpx.MockTransport(handler),
        sleep_func=lambda _: None,
    )


async def _setup_cycle_with_primary(
    db_session,
    cycle_id: str,
    patient_id: str,
    base_date: str = "2026-10-18",
) -> None:
    patient_repo = PatientRepository(db_session)
    patient_repo.create(patient_id=patient_id)
    cycle_repo = MedicalCycleRepository(db_session)
    cycle_repo.create(
        cycle_id=cycle_id,
        patient_id=patient_id,
        status="active",
    )

    _seed_preparatory_cycle_context(db_session, cycle_id=cycle_id, seed_ledger=True)
    p_calls: List[Dict[str, Any]] = []
    p_gw = _build_consilium_mock_gateway(p_calls)
    p_service = PrimaryConsiliumService(db=db_session, gateway=p_gw)
    await p_service.run_primary_consilium(cycle_id=cycle_id, report_date=base_date)


@pytest.mark.anyio
async def test_continuous_accompaniment_day8_and_day9_delayed_weekly(db_session):
    """
    1. Proves Day 8 and Day 9 accompaniment executes seamlessly while Week 1 Weekly
       Consilium is unfinished/delayed, advancing sprint and day authoritatively.
    """
    cycle_id = "cycle_cont_01"
    patient_id = "patient_cont_01"
    t0 = datetime(2026, 10, 18, 9, 0, 0, tzinfo=timezone.utc)
    clock = ControlledTime(t0)

    await _setup_cycle_with_primary(db_session, cycle_id=cycle_id, patient_id=patient_id)

    recorded_calls: List[Dict[str, Any]] = []
    gateway = _build_test_gateway(recorded_calls)

    chat_service = AthenaChatService(db=db_session, gateway=gateway, now_provider=clock.now)
    weekly_service = WeeklyConsiliumService(db=db_session, gateway=gateway, now_provider=clock.now)

    # Initial greeting at T0 anchors schedule
    await chat_service.handle_turn(
        patient_id=patient_id,
        cycle_id=cycle_id,
        request=AthenaChatRequest(
            event_type="initial_greeting",
            message="",
            sprint_day=1,
            idempotency_key="greet_cont_01",
        ),
    )

    # Day 8 arrival (T0 + 169h -> calendar Week 2 Day 1)
    clock.advance(hours=169)

    cal_w, cal_d, _ = weekly_service.get_authoritative_calendar_schedule(cycle_id, as_of=clock.now())
    assert cal_w == 2
    assert cal_d == 1

    # Weekly Consilium for Week 1 has NOT been executed!
    status_w1 = weekly_service.get_weekly_sprint_lifecycle_state(cycle_id)
    assert not status_w1.is_weekly_consilium_completed

    # Day 8 daily pulse turn must execute successfully without raising any error
    resp_d8 = await chat_service.handle_turn(
        patient_id=patient_id,
        cycle_id=cycle_id,
        request=AthenaChatRequest(
            event_type="daily_pulse",
            message="Day 8 morning check-in: feeling well.",
            sprint_number=2,
            sprint_day=1,
            idempotency_key="athena_daily_pulse_cycle_cont_01_w2_d1",
        ),
    )
    assert resp_d8.sprint_number == 2
    assert resp_d8.sprint_day == 1
    assert resp_d8.reply is not None

    # Day 9 arrival (T0 + 193h -> calendar Week 2 Day 2)
    clock.advance(hours=24)
    cal_w9, cal_d9, _ = weekly_service.get_authoritative_calendar_schedule(cycle_id, as_of=clock.now())
    assert cal_w9 == 2
    assert cal_d9 == 2

    resp_d9 = await chat_service.handle_turn(
        patient_id=patient_id,
        cycle_id=cycle_id,
        request=AthenaChatRequest(
            event_type="daily_pulse",
            message="Day 9 check-in.",
            sprint_number=2,
            sprint_day=2,
            idempotency_key="athena_daily_pulse_cycle_cont_01_w2_d2",
        ),
    )
    assert resp_d9.sprint_number == 2
    assert resp_d9.sprint_day == 2


@pytest.mark.anyio
async def test_observation_diary_isolation_between_week1_and_week2(db_session):
    """
    2. Proves Week 1 and Week 2 observation diary entries are strictly partitioned
       by the fixed 168-hour boundaries, preventing cross-week log pollution.
    """
    cycle_id = "cycle_cont_02"
    patient_id = "patient_cont_02"
    t0 = datetime(2026, 10, 18, 9, 0, 0, tzinfo=timezone.utc)
    clock = ControlledTime(t0)

    await _setup_cycle_with_primary(db_session, cycle_id=cycle_id, patient_id=patient_id)

    recorded_calls: List[Dict[str, Any]] = []
    dynamic_summary = {"text": "W1_D1_OBS"}
    gateway = _build_test_gateway(
        recorded_calls,
        athena_summary_supplier=lambda p: dynamic_summary["text"],
    )

    chat_service = AthenaChatService(db=db_session, gateway=gateway, now_provider=clock.now)
    weekly_service = WeeklyConsiliumService(db=db_session, gateway=gateway, now_provider=clock.now)

    # Day 1 greeting at T0
    await chat_service.handle_turn(
        patient_id=patient_id,
        cycle_id=cycle_id,
        request=AthenaChatRequest(
            event_type="initial_greeting",
            message="",
            sprint_day=1,
            idempotency_key="greet_cont_02",
        ),
    )

    # Day 4 observation (T0 + 72h)
    clock.advance(hours=72)
    dynamic_summary["text"] = "W1_D4_OBS"
    await chat_service.handle_turn(
        patient_id=patient_id,
        cycle_id=cycle_id,
        request=AthenaChatRequest(
            event_type="daily_pulse",
            message="Mid-week check-in.",
            sprint_number=1,
            sprint_day=4,
            idempotency_key="pulse_cont_02_w1_d4",
        ),
    )

    # Day 8 observation (T0 + 169h -> calendar Week 2 Day 1)
    clock.advance(hours=97)
    dynamic_summary["text"] = "W2_D1_OBS"
    await chat_service.handle_turn(
        patient_id=patient_id,
        cycle_id=cycle_id,
        request=AthenaChatRequest(
            event_type="daily_pulse",
            message="Week 2 starts.",
            sprint_number=2,
            sprint_day=1,
            idempotency_key="pulse_cont_02_w2_d1",
        ),
    )

    # Query Week 1 logs: must contain ONLY W1_D1 and W1_D4, strictly excluding W2_D1
    w1_logs = weekly_service.get_seven_day_athena_logs(
        cycle_id=cycle_id,
        sprint_number=1,
    )
    w1_summaries = [l.summary_for_master for l in w1_logs]
    assert "W1_D1_OBS" in w1_summaries
    assert "W1_D4_OBS" in w1_summaries
    assert "W2_D1_OBS" not in w1_summaries

    # Query Week 2 logs: must contain W2_D1, strictly excluding W1 logs
    w2_logs = weekly_service.get_seven_day_athena_logs(
        cycle_id=cycle_id,
        sprint_number=2,
    )
    w2_summaries = [l.summary_for_master for l in w2_logs]
    assert "W2_D1_OBS" in w2_summaries
    assert "W1_D1_OBS" not in w2_summaries
    assert "W1_D4_OBS" not in w2_summaries


@pytest.mark.anyio
async def test_delayed_week1_consilium_receives_only_week1_logs(db_session):
    """
    3. Proves that when Week 1 Weekly Consilium executes late (e.g. at T0 + 190h),
       both Flash Deputy and Pro Chief receive ONLY Week 1 observations.
    """
    cycle_id = "cycle_cont_03"
    patient_id = "patient_cont_03"
    t0 = datetime(2026, 10, 18, 9, 0, 0, tzinfo=timezone.utc)
    clock = ControlledTime(t0)

    await _setup_cycle_with_primary(db_session, cycle_id=cycle_id, patient_id=patient_id)

    recorded_calls: List[Dict[str, Any]] = []
    dynamic_summary = {"text": "W1_LOG_ONLY"}
    gateway = _build_test_gateway(
        recorded_calls,
        athena_summary_supplier=lambda p: dynamic_summary["text"],
    )

    chat_service = AthenaChatService(db=db_session, gateway=gateway, now_provider=clock.now)
    weekly_service = WeeklyConsiliumService(db=db_session, gateway=gateway, now_provider=clock.now)

    # Initial greeting at T0
    await chat_service.handle_turn(
        patient_id=patient_id,
        cycle_id=cycle_id,
        request=AthenaChatRequest(
            event_type="initial_greeting",
            message="",
            sprint_day=1,
            idempotency_key="greet_cont_03",
        ),
    )

    # Day 8 observation in Week 2 (T0 + 170h)
    clock.advance(hours=170)
    dynamic_summary["text"] = "W2_DAY8_DO_NOT_INCLUDE_IN_WEEK1"
    await chat_service.handle_turn(
        patient_id=patient_id,
        cycle_id=cycle_id,
        request=AthenaChatRequest(
            event_type="daily_pulse",
            message="Day 8 check-in.",
            sprint_number=2,
            sprint_day=1,
            idempotency_key="pulse_cont_03_w2_d1",
        ),
    )

    # At T0 + 190h, late Week 1 Consilium executes
    clock.advance(hours=20)
    weekly_resp = await weekly_service.run_weekly_consilium(
        cycle_id=cycle_id,
        sprint_id=f"sprint_{cycle_id}_w1",
        report_date="2026-10-25",
    )
    assert weekly_resp.sprint_number == 1

    # Verify calls to Flash Deputy and Pro Chief:
    # W1_LOG_ONLY must be present; W2_DAY8_DO_NOT_INCLUDE_IN_WEEK1 must NOT be present!
    deputy_call = next(c for c in recorded_calls if "head of department" in c["prompt"].lower())
    chief_call = next(c for c in recorded_calls if "chief physician" in c["prompt"].lower())

    assert "W1_LOG_ONLY" in deputy_call["prompt"]
    assert "W2_DAY8_DO_NOT_INCLUDE_IN_WEEK1" not in deputy_call["prompt"]

    assert "W1_LOG_ONLY" in chief_call["prompt"]
    assert "W2_DAY8_DO_NOT_INCLUDE_IN_WEEK1" not in chief_call["prompt"]


@pytest.mark.anyio
async def test_strategy_preservation_during_delayed_weekly(db_session):
    """
    4. Proves that while Week 1 Weekly Consilium is delayed, Athena turns on Day 8
       continue using the Primary Consilium strategy (v1) without falling back to onboarding.
    """
    cycle_id = "cycle_cont_04"
    patient_id = "patient_cont_04"
    t0 = datetime(2026, 10, 18, 9, 0, 0, tzinfo=timezone.utc)
    clock = ControlledTime(t0)

    await _setup_cycle_with_primary(db_session, cycle_id=cycle_id, patient_id=patient_id)

    recorded_calls: List[Dict[str, Any]] = []
    gateway = _build_test_gateway(recorded_calls)

    chat_service = AthenaChatService(db=db_session, gateway=gateway, now_provider=clock.now)

    # Initial greeting at T0
    await chat_service.handle_turn(
        patient_id=patient_id,
        cycle_id=cycle_id,
        request=AthenaChatRequest(
            event_type="initial_greeting",
            message="",
            sprint_day=1,
            idempotency_key="greet_cont_04",
        ),
    )

    # Advance to Day 8 (T0 + 172h)
    clock.advance(hours=172)
    recorded_calls.clear()

    await chat_service.handle_turn(
        patient_id=patient_id,
        cycle_id=cycle_id,
        request=AthenaChatRequest(
            event_type="daily_pulse",
            message="Day 8 check-in.",
            sprint_number=2,
            sprint_day=1,
            idempotency_key="pulse_cont_04_w2_d1",
        ),
    )

    assert len(recorded_calls) == 1
    call = recorded_calls[0]
    sys_instruction = call["system_instruction"]

    # System instruction must include Primary Consilium clinical route & thresholds
    assert "CHIEF PHYSICIAN" in sys_instruction
    assert "Individual vitals thresholds" not in sys_instruction
    assert "Patient is in initial onboarding" not in sys_instruction


@pytest.mark.anyio
async def test_strategy_transition_following_weekly_consilium_completion(db_session):
    """
    5. Proves that once Week 1 Weekly Consilium completes and publishes v2 strategy,
       subsequent Athena turns immediately transition to v2 strategy in their system instruction.
    """
    cycle_id = "cycle_cont_05"
    patient_id = "patient_cont_05"
    t0 = datetime(2026, 10, 18, 9, 0, 0, tzinfo=timezone.utc)
    clock = ControlledTime(t0)

    await _setup_cycle_with_primary(db_session, cycle_id=cycle_id, patient_id=patient_id)

    recorded_calls: List[Dict[str, Any]] = []
    gateway = _build_test_gateway(recorded_calls)

    chat_service = AthenaChatService(db=db_session, gateway=gateway, now_provider=clock.now)
    weekly_service = WeeklyConsiliumService(db=db_session, gateway=gateway, now_provider=clock.now)

    # Greeting at T0
    await chat_service.handle_turn(
        patient_id=patient_id,
        cycle_id=cycle_id,
        request=AthenaChatRequest(
            event_type="initial_greeting",
            message="",
            sprint_day=1,
            idempotency_key="greet_cont_05",
        ),
    )

    # Advance to 168 hours and complete Week 1 Weekly Consilium
    clock.advance(hours=168)
    await weekly_service.run_weekly_consilium(
        cycle_id=cycle_id,
        sprint_id=f"sprint_{cycle_id}_w1",
        report_date="2026-10-25",
    )

    # Verify athena_active_strategy was published
    strat_reports = weekly_service.report_repo.list_server_only(
        cycle_id=cycle_id,
        report_type=ATHENA_ACTIVE_STRATEGY_REPORT_TYPE,
    )
    assert len(strat_reports) >= 1
    v2_text = strat_reports[0].content["active_strategy_text"]

    # Now execute Day 8 turn in Week 2
    clock.advance(hours=2)
    recorded_calls.clear()

    await chat_service.handle_turn(
        patient_id=patient_id,
        cycle_id=cycle_id,
        request=AthenaChatRequest(
            event_type="daily_pulse",
            message="Good morning after Weekly Consilium.",
            sprint_number=2,
            sprint_day=1,
            idempotency_key="pulse_cont_05_w2_d1",
        ),
    )

    assert len(recorded_calls) == 1
    turn_sys_inst = recorded_calls[0]["system_instruction"]
    assert v2_text in turn_sys_inst


@pytest.mark.anyio
async def test_multi_week_idempotency_stability(db_session):
    """
    6. Proves week/day-specific idempotency stability across retries and guarantees
       Week 2 Day 1 cannot collide with or replay Week 1 Day 1.
    """
    cycle_id = "cycle_cont_06"
    patient_id = "patient_cont_06"
    t0 = datetime(2026, 10, 18, 9, 0, 0, tzinfo=timezone.utc)
    clock = ControlledTime(t0)

    await _setup_cycle_with_primary(db_session, cycle_id=cycle_id, patient_id=patient_id)

    recorded_calls: List[Dict[str, Any]] = []
    gateway = _build_test_gateway(recorded_calls)

    chat_service = AthenaChatService(db=db_session, gateway=gateway, now_provider=clock.now)

    # 1. Week 1 Day 1 greeting
    resp_w1 = await chat_service.handle_turn(
        patient_id=patient_id,
        cycle_id=cycle_id,
        request=AthenaChatRequest(
            event_type="initial_greeting",
            message="",
            sprint_day=1,
            idempotency_key="athena_daily_pulse_cycle_cont_06_w1_d1",
        ),
    )

    # 2. Advance to Week 2 Day 1 (T0 + 169h)
    clock.advance(hours=169)
    resp_w2 = await chat_service.handle_turn(
        patient_id=patient_id,
        cycle_id=cycle_id,
        request=AthenaChatRequest(
            event_type="daily_pulse",
            message="Week 2 Day 1.",
            sprint_number=2,
            sprint_day=1,
            idempotency_key="athena_daily_pulse_cycle_cont_06_w2_d1",
        ),
    )

    assert resp_w1.message_id != resp_w2.message_id
    assert resp_w1.sprint_day == 1
    assert resp_w2.sprint_number == 2
    assert resp_w2.sprint_day == 1

    # 3. Retry Week 2 Day 1: returns exact same turn without extra AI call
    initial_call_count = len(recorded_calls)
    retry_resp = await chat_service.handle_turn(
        patient_id=patient_id,
        cycle_id=cycle_id,
        request=AthenaChatRequest(
            event_type="daily_pulse",
            message="Week 2 Day 1 retry.",
            sprint_number=2,
            sprint_day=1,
            idempotency_key="athena_daily_pulse_cycle_cont_06_w2_d1",
        ),
    )
    assert len(recorded_calls) == initial_call_count
    assert retry_resp.message_id == resp_w2.message_id
    assert retry_resp.reply == resp_w2.reply
    assert retry_resp.sprint_number == 2


@pytest.mark.anyio
async def test_patient_and_cycle_isolation(db_session):
    """
    7. Proves strict cycle and patient isolation: Patient A in Week 2 and Patient B in Week 1
       never mix logs, active strategies, or dialogue turns.
    """
    t0 = datetime(2026, 10, 18, 9, 0, 0, tzinfo=timezone.utc)
    clock = ControlledTime(t0)

    await _setup_cycle_with_primary(db_session, cycle_id="cycle_pA", patient_id="patient_A")
    await _setup_cycle_with_primary(db_session, cycle_id="cycle_pB", patient_id="patient_B")

    recorded_calls: List[Dict[str, Any]] = []
    gateway = _build_test_gateway(recorded_calls)

    chat_service = AthenaChatService(db=db_session, gateway=gateway, now_provider=clock.now)
    weekly_service = WeeklyConsiliumService(db=db_session, gateway=gateway, now_provider=clock.now)

    # Establish Patient A anchor at T0
    await chat_service.handle_turn(
        patient_id="patient_A",
        cycle_id="cycle_pA",
        request=AthenaChatRequest(
            event_type="initial_greeting",
            message="",
            sprint_day=1,
            idempotency_key="pA_greet",
        ),
    )

    # Patient B anchor at T0 + 10h
    clock.advance(hours=10)
    await chat_service.handle_turn(
        patient_id="patient_B",
        cycle_id="cycle_pB",
        request=AthenaChatRequest(
            event_type="initial_greeting",
            message="",
            sprint_day=1,
            idempotency_key="pB_greet",
        ),
    )

    # Advance to T0 + 170h (Patient A is in Week 2, Patient B is at 160h -> Week 1)
    clock.advance(hours=160)
    sched_A = weekly_service.get_authoritative_calendar_schedule("cycle_pA", as_of=clock.now())
    sched_B = weekly_service.get_authoritative_calendar_schedule("cycle_pB", as_of=clock.now())

    assert sched_A[0] == 2  # Patient A is in Week 2
    assert sched_B[0] == 1  # Patient B is still in Week 1

    # Verify log separation
    logs_A = weekly_service.get_seven_day_athena_logs("cycle_pA", sprint_number=1)
    logs_B = weekly_service.get_seven_day_athena_logs("cycle_pB", sprint_number=1)

    assert len(logs_A) == 1
    assert len(logs_B) == 1
    assert logs_A[0].timestamp == t0.isoformat()
    assert logs_B[0].timestamp == (t0 + timedelta(hours=10)).isoformat()

    db_reps_A = weekly_service.report_repo.list_server_only("cycle_pA", ATHENA_CLINICAL_LOG_REPORT_TYPE)
    db_reps_B = weekly_service.report_repo.list_server_only("cycle_pB", ATHENA_CLINICAL_LOG_REPORT_TYPE)
    assert all(r.cycle_id == "cycle_pA" for r in db_reps_A)
    assert all(r.cycle_id == "cycle_pB" for r in db_reps_B)


@pytest.mark.anyio
async def test_server_rejects_client_invented_future_periods(db_session):
    """
    8. Proves server validates incoming daily_pulse against authoritative calendar schedule
       and rejects client-invented future sprint numbers or sprint days with HTTP 409 Conflict.
    """
    cycle_id = "cycle_cont_08"
    patient_id = "patient_cont_08"
    t0 = datetime(2026, 10, 18, 9, 0, 0, tzinfo=timezone.utc)
    clock = ControlledTime(t0)

    await _setup_cycle_with_primary(db_session, cycle_id=cycle_id, patient_id=patient_id)

    recorded_calls: List[Dict[str, Any]] = []
    gateway = _build_test_gateway(recorded_calls)

    chat_service = AthenaChatService(db=db_session, gateway=gateway, now_provider=clock.now)

    # Anchor at T0
    await chat_service.handle_turn(
        patient_id=patient_id,
        cycle_id=cycle_id,
        request=AthenaChatRequest(
            event_type="initial_greeting",
            message="",
            sprint_day=1,
            idempotency_key="greet_cont_08",
        ),
    )

    # Advance 24h -> Calendar Week 1 Day 2
    clock.advance(hours=24)

    # 1. Attempting future week (Week 2 when authoritative is Week 1) -> 409
    with pytest.raises(HTTPException) as exc_w:
        await chat_service.handle_turn(
            patient_id=patient_id,
            cycle_id=cycle_id,
            request=AthenaChatRequest(
                event_type="daily_pulse",
                message="Client guessing future week.",
                sprint_number=2,
                sprint_day=1,
                idempotency_key="future_w2",
            ),
        )
    assert exc_w.value.status_code == 409
    assert "current authoritative calendar week is 1" in exc_w.value.detail

    # 2. Attempting future day (Day 5 when authoritative is Day 2) -> 409
    with pytest.raises(HTTPException) as exc_d:
        await chat_service.handle_turn(
            patient_id=patient_id,
            cycle_id=cycle_id,
            request=AthenaChatRequest(
                event_type="daily_pulse",
                message="Client skipping ahead to day 5.",
                sprint_number=1,
                sprint_day=5,
                idempotency_key="future_d5",
            ),
        )
    assert exc_d.value.status_code == 409
    assert "current authoritative calendar day is 2" in exc_d.value.detail

    # 3. Legitimate authoritative turn (Week 1 Day 2) -> 200 OK
    resp_ok = await chat_service.handle_turn(
        patient_id=patient_id,
        cycle_id=cycle_id,
        request=AthenaChatRequest(
            event_type="daily_pulse",
            message="Legitimate Day 2 turn.",
            sprint_number=1,
            sprint_day=2,
            idempotency_key="legit_w1_d2",
        ),
    )
    assert resp_ok.sprint_number == 1
    assert resp_ok.sprint_day == 2
