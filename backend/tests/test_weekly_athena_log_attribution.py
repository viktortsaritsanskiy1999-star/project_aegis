from datetime import datetime, timezone, timedelta
import json
from typing import Any, Dict, List
import httpx
import pytest

from app.models.medical_cycle import MedicalCycle
from app.repositories.cycle_repository import MedicalCycleRepository
from app.repositories.patient_repository import PatientRepository
from app.repositories.report_repository import ModelReportRepository
from app.repositories.sprint_repository import WeeklySprintRepository
from app.schemas.chat import AthenaChatRequest
from app.schemas.consilium import SYSTEM_KEYS
from app.services.gemini_gateway import GeminiGateway
from app.services.primary_consilium_service import PrimaryConsiliumService
from app.services.weekly_consilium_service import (
    WeeklyConsiliumService,
    ATHENA_SCHEDULE_ANCHOR_REPORT_TYPE,
    ATHENA_CLINICAL_LOG_REPORT_TYPE,
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


def _build_attribution_test_gateway(
    recorded_calls: List[Dict[str, Any]],
    athena_summary_supplier=None,
) -> GeminiGateway:
    call_counter = {"index": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        idx = call_counter["index"]
        call_counter["index"] += 1
        body = json.loads(request.content.decode("utf-8"))
        prompt_text = body["contents"][0]["parts"][0]["text"]
        url_path = request.url.path

        recorded_calls.append({
            "index": idx,
            "url_path": url_path,
            "prompt": prompt_text,
        })

        # 1. Weekly Consilium Pro Chief turn
        if "you are the chief physician" in prompt_text.lower() or "pro" in url_path.lower() or "argon" in url_path.lower():
            return httpx.Response(200, json=_gemini_envelope(_sample_weekly_pro_output()))

        # 2. Weekly Consilium Flash Deputy turn
        if "you are the head of department" in prompt_text.lower():
            return httpx.Response(200, json=_gemini_envelope(_sample_weekly_flash_output()))

        # 3. Athena chat turns
        summary = (
            athena_summary_supplier(prompt_text)
            if athena_summary_supplier
            else "Routine Athena clinical check-in recorded."
        )
        chat_reply = (
            "Здравствуйте! Зафиксировала ваше самочувствие.\n\n"
            "[CLINICAL_REPORT]\n"
            "{\n"
            '  "status": "NORMAL",\n'
            '  "symptoms_mentioned": ["tracked_symptom"],\n'
            '  "subjective_wellbeing_score": 8,\n'
            '  "adherence_to_plan": "HIGH",\n'
            '  "red_flags": false,\n'
            f'  "summary_for_master": "{summary}"\n'
            "}\n"
            "[/CLINICAL_REPORT]"
        )
        return httpx.Response(200, json=_text_envelope(chat_reply))

    return GeminiGateway(
        api_key="test-attribution-gateway-key",
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
async def test_argon_bug01_athena_logs_reach_flash_and_pro_week1_and_week2(db_session):
    """
    Proves that actual persisted Athena clinical logs reach both Flash Deputy and Pro Chief
    for Week 1 and Week 2, correctly bound to the 168-hour fixed grid schedule.
    """
    cycle_id = "cycle_attr_01"
    patient_id = "patient_attr_01"
    t0 = datetime(2026, 10, 18, 9, 0, 0, tzinfo=timezone.utc)
    clock = ControlledTime(t0)

    await _setup_cycle_with_primary(db_session, cycle_id=cycle_id, patient_id=patient_id)

    recorded_calls: List[Dict[str, Any]] = []
    dynamic_summary = {"text": "W1_GREETING_OBSERVATION"}

    gateway = _build_attribution_test_gateway(
        recorded_calls,
        athena_summary_supplier=lambda p: dynamic_summary["text"],
    )

    chat_service = AthenaChatService(
        db=db_session,
        gateway=gateway,
        now_provider=clock.now,
    )
    weekly_service = WeeklyConsiliumService(
        db=db_session,
        gateway=gateway,
        now_provider=clock.now,
    )

    # 1. Day 1 (T0): Initial greeting -> creates athena_schedule_anchor and first clinical log
    greet_resp = await chat_service.handle_turn(
        patient_id=patient_id,
        cycle_id=cycle_id,
        request=AthenaChatRequest(
            event_type="initial_greeting",
            message="",
            sprint_day=1,
            idempotency_key="greet_key_01",
        ),
    )
    assert greet_resp.reply is not None

    anchor_ts = weekly_service.find_schedule_anchor_timestamp(cycle_id)
    assert anchor_ts is not None
    assert anchor_ts == t0

    # 2. Day 3 (T0 + 48h): Daily pulse turn with notable observation
    clock.advance(hours=48)
    dynamic_summary["text"] = "W1_DAY3_HEADACHE_OBSERVATION"

    pulse_resp = await chat_service.handle_turn(
        patient_id=patient_id,
        cycle_id=cycle_id,
        request=AthenaChatRequest(
            event_type="daily_pulse",
            message="I had a slight headache in the afternoon.",
            sprint_day=3,
            idempotency_key="pulse_w1_d3",
        ),
    )
    assert pulse_resp.reply is not None

    # Verify logs exist in DB with sprint_id sprint_cycle_attr_01_w1
    expected_w1_sprint_id = f"sprint_{cycle_id}_w1"
    w1_logs = weekly_service.get_seven_day_athena_logs(cycle_id=cycle_id, sprint_id=expected_w1_sprint_id)
    assert len(w1_logs) == 2
    summaries = [l.summary_for_master for l in w1_logs]
    assert "W1_GREETING_OBSERVATION" in summaries
    assert "W1_DAY3_HEADACHE_OBSERVATION" in summaries

    # 3. Advance clock to T0 + 168h (Week 1 boundary elapsed)
    clock.advance(hours=120)  # Total 48 + 120 = 168h

    lifecycle = weekly_service.get_weekly_sprint_lifecycle_state(cycle_id)
    assert lifecycle.can_start_next_sprint is True
    assert lifecycle.next_sprint_id == expected_w1_sprint_id

    # 4. Run Week 1 Weekly Consilium
    consilium_calls_w1: List[Dict[str, Any]] = []
    weekly_gateway_w1 = _build_attribution_test_gateway(consilium_calls_w1)
    weekly_service_w1 = WeeklyConsiliumService(
        db=db_session,
        gateway=weekly_gateway_w1,
        now_provider=clock.now,
    )
    res_w1 = await weekly_service_w1.run_weekly_consilium(
        cycle_id=cycle_id,
        sprint_id=expected_w1_sprint_id,
    )
    assert res_w1.sprint_id == expected_w1_sprint_id
    assert res_w1.sprint_number == 1

    # Verify both Flash Deputy (call 0) and Pro Chief (call 1) received Week 1 Athena logs
    assert len(consilium_calls_w1) == 2
    flash_prompt_w1 = consilium_calls_w1[0]["prompt"]
    pro_prompt_w1 = consilium_calls_w1[1]["prompt"]

    assert "W1_DAY3_HEADACHE_OBSERVATION" in flash_prompt_w1
    assert "W1_DAY3_HEADACHE_OBSERVATION" in pro_prompt_w1
    assert "W1_GREETING_OBSERVATION" in flash_prompt_w1
    assert "W1_GREETING_OBSERVATION" in pro_prompt_w1

    # 5. Week 2: Advance to T0 + 172h (Week 2, Day 8)
    clock.advance(hours=4)  # 168 + 4 = 172h
    dynamic_summary["text"] = "W2_DAY8_RECOVERY_OBSERVATION"

    w2_pulse_resp = await chat_service.handle_turn(
        patient_id=patient_id,
        cycle_id=cycle_id,
        request=AthenaChatRequest(
            event_type="daily_pulse",
            message="Headache resolved, feeling energetic.",
            sprint_day=1,
            idempotency_key="pulse_w2_d1",
        ),
    )
    assert w2_pulse_resp.reply is not None

    expected_w2_sprint_id = f"sprint_{cycle_id}_w2"
    w2_logs = weekly_service.get_seven_day_athena_logs(cycle_id=cycle_id, sprint_id=expected_w2_sprint_id)
    assert len(w2_logs) == 1
    assert w2_logs[0].summary_for_master == "W2_DAY8_RECOVERY_OBSERVATION"

    # 6. Advance clock to T0 + 336h (Week 2 boundary elapsed)
    clock.advance(hours=164)  # 172 + 164 = 336h

    consilium_calls_w2: List[Dict[str, Any]] = []
    weekly_gateway_w2 = _build_attribution_test_gateway(consilium_calls_w2)
    weekly_service_w2 = WeeklyConsiliumService(
        db=db_session,
        gateway=weekly_gateway_w2,
        now_provider=clock.now,
    )
    res_w2 = await weekly_service_w2.run_weekly_consilium(
        cycle_id=cycle_id,
        sprint_id=expected_w2_sprint_id,
    )
    assert res_w2.sprint_id == expected_w2_sprint_id
    assert res_w2.sprint_number == 2

    # Verify both Flash Deputy and Pro Chief received Week 2 logs, and NEVER Week 1 logs
    assert len(consilium_calls_w2) == 2
    flash_prompt_w2 = consilium_calls_w2[0]["prompt"]
    pro_prompt_w2 = consilium_calls_w2[1]["prompt"]

    assert "W2_DAY8_RECOVERY_OBSERVATION" in flash_prompt_w2
    assert "W2_DAY8_RECOVERY_OBSERVATION" in pro_prompt_w2
    # STRICTLY NO MIXING of adjacent weeks:
    assert "W1_DAY3_HEADACHE_OBSERVATION" not in flash_prompt_w2
    assert "W1_DAY3_HEADACHE_OBSERVATION" not in pro_prompt_w2
    assert "W1_GREETING_OBSERVATION" not in flash_prompt_w2
    assert "W1_GREETING_OBSERVATION" not in pro_prompt_w2


@pytest.mark.anyio
async def test_argon_bug01_late_weekly_consilium_does_not_mix_adjacent_weeks(db_session):
    """
    Proves that late execution of Weekly Consilium (e.g. Week 1 executed at T0 + 200h)
    strictly respects the fixed 168-hour boundary and never absorbs logs from Week 2.
    """
    cycle_id = "cycle_attr_late"
    patient_id = "patient_attr_late"
    t0 = datetime(2026, 10, 18, 8, 0, 0, tzinfo=timezone.utc)
    clock = ControlledTime(t0)

    await _setup_cycle_with_primary(db_session, cycle_id=cycle_id, patient_id=patient_id)
    weekly_service = WeeklyConsiliumService(db=db_session, now_provider=clock.now)

    # Establish anchor
    weekly_service.record_schedule_anchor(cycle_id=cycle_id, anchor_dt=t0)

    # Week 1 log at T0 + 50h
    weekly_service.save_athena_clinical_log(
        cycle_id=cycle_id,
        log_entry={
            "status": "NORMAL",
            "symptoms_mentioned": ["on_time"],
            "subjective_wellbeing_score": 9,
            "adherence_to_plan": "HIGH",
            "red_flags": False,
            "summary_for_master": "W1_ON_TIME_OBSERVATION",
            "timestamp": (t0 + timedelta(hours=50)).isoformat(),
        },
    )

    # Week 2 log at T0 + 175h (chronologically in Week 2 window [T0 + 168h, T0 + 336h))
    weekly_service.save_athena_clinical_log(
        cycle_id=cycle_id,
        log_entry={
            "status": "NORMAL",
            "symptoms_mentioned": ["early_week2"],
            "subjective_wellbeing_score": 8,
            "adherence_to_plan": "HIGH",
            "red_flags": False,
            "summary_for_master": "W2_CHRONOLOGICAL_OBSERVATION",
            "timestamp": (t0 + timedelta(hours=175)).isoformat(),
        },
    )

    # Late execution: Run Week 1 Consilium at T0 + 200h!
    clock.advance(hours=200)

    consilium_calls_w1: List[Dict[str, Any]] = []
    gw_w1 = _build_attribution_test_gateway(consilium_calls_w1)
    svc_w1 = WeeklyConsiliumService(db=db_session, gateway=gw_w1, now_provider=clock.now)
    await svc_w1.run_weekly_consilium(
        cycle_id=cycle_id,
        sprint_id=f"sprint_{cycle_id}_w1",
    )

    assert len(consilium_calls_w1) == 2
    for c in consilium_calls_w1:
        assert "W1_ON_TIME_OBSERVATION" in c["prompt"]
        assert "W2_CHRONOLOGICAL_OBSERVATION" not in c["prompt"]

    # Now run Week 2 Consilium at T0 + 340h
    clock.advance(hours=140)  # Total 340h
    consilium_calls_w2: List[Dict[str, Any]] = []
    gw_w2 = _build_attribution_test_gateway(consilium_calls_w2)
    svc_w2 = WeeklyConsiliumService(db=db_session, gateway=gw_w2, now_provider=clock.now)
    await svc_w2.run_weekly_consilium(
        cycle_id=cycle_id,
        sprint_id=f"sprint_{cycle_id}_w2",
    )

    assert len(consilium_calls_w2) == 2
    for c in consilium_calls_w2:
        assert "W2_CHRONOLOGICAL_OBSERVATION" in c["prompt"]
        assert "W1_ON_TIME_OBSERVATION" not in c["prompt"]


@pytest.mark.anyio
async def test_argon_bug01_no_responses_during_week_does_not_substitute_data(db_session):
    """
    Proves that when no Athena responses exist for a given sprint, Weekly Consilium passes
    'Athena\'s hidden clinical logs are missing.' and NEVER substitutes data from another period.
    """
    cycle_id = "cycle_attr_empty"
    patient_id = "patient_attr_empty"
    t0 = datetime(2026, 10, 18, 10, 0, 0, tzinfo=timezone.utc)
    clock = ControlledTime(t0)

    await _setup_cycle_with_primary(db_session, cycle_id=cycle_id, patient_id=patient_id)
    weekly_service = WeeklyConsiliumService(db=db_session, now_provider=clock.now)
    weekly_service.record_schedule_anchor(cycle_id=cycle_id, anchor_dt=t0)

    # Week 1 has 1 log
    weekly_service.save_athena_clinical_log(
        cycle_id=cycle_id,
        log_entry={
            "status": "NORMAL",
            "symptoms_mentioned": [],
            "subjective_wellbeing_score": 8,
            "adherence_to_plan": "HIGH",
            "red_flags": False,
            "summary_for_master": "W1_ONLY_LOG_PRESENT",
            "timestamp": (t0 + timedelta(hours=24)).isoformat(),
        },
    )

    # Run Week 1
    clock.advance(hours=168)
    calls_w1: List[Dict[str, Any]] = []
    gw_w1 = _build_attribution_test_gateway(calls_w1)
    svc_w1 = WeeklyConsiliumService(db=db_session, gateway=gw_w1, now_provider=clock.now)
    await svc_w1.run_weekly_consilium(cycle_id=cycle_id, sprint_id=f"sprint_{cycle_id}_w1")

    # In Week 2: patient does NOT respond at all (ZERO logs generated between 168h and 336h)
    clock.advance(hours=168)  # Now at 336h
    calls_w2: List[Dict[str, Any]] = []
    gw_w2 = _build_attribution_test_gateway(calls_w2)
    svc_w2 = WeeklyConsiliumService(db=db_session, gateway=gw_w2, now_provider=clock.now)
    await svc_w2.run_weekly_consilium(cycle_id=cycle_id, sprint_id=f"sprint_{cycle_id}_w2")

    assert len(calls_w2) == 2
    for c in calls_w2:
        assert "Athena's hidden clinical logs are missing." in c["prompt"]
        assert "W1_ONLY_LOG_PRESENT" not in c["prompt"]


@pytest.mark.anyio
async def test_argon_bug01_retry_idempotency_preserves_exact_reports(db_session):
    """
    Proves that client retries or repeated executions return the recovered response
    idempotently without calling Gemini again.
    """
    cycle_id = "cycle_attr_retry"
    patient_id = "patient_attr_retry"
    t0 = datetime(2026, 10, 18, 12, 0, 0, tzinfo=timezone.utc)
    clock = ControlledTime(t0)

    await _setup_cycle_with_primary(db_session, cycle_id=cycle_id, patient_id=patient_id)
    weekly_service = WeeklyConsiliumService(db=db_session, now_provider=clock.now)
    weekly_service.record_schedule_anchor(cycle_id=cycle_id, anchor_dt=t0)

    weekly_service.save_athena_clinical_log(
        cycle_id=cycle_id,
        log_entry={
            "status": "NORMAL",
            "symptoms_mentioned": [],
            "subjective_wellbeing_score": 8,
            "adherence_to_plan": "HIGH",
            "red_flags": False,
            "summary_for_master": "IDEMPOTENT_LOG_W1",
            "timestamp": (t0 + timedelta(hours=20)).isoformat(),
        },
    )

    clock.advance(hours=168)
    calls_1: List[Dict[str, Any]] = []
    gw_1 = _build_attribution_test_gateway(calls_1)
    svc_1 = WeeklyConsiliumService(db=db_session, gateway=gw_1, now_provider=clock.now)

    res_first = await svc_1.run_weekly_consilium(cycle_id=cycle_id, sprint_id=f"sprint_{cycle_id}_w1")
    assert len(calls_1) == 2

    # Retry call with identical sprint_id
    calls_2: List[Dict[str, Any]] = []
    gw_2 = _build_attribution_test_gateway(calls_2)
    svc_2 = WeeklyConsiliumService(db=db_session, gateway=gw_2, now_provider=clock.now)

    res_retry = await svc_2.run_weekly_consilium(cycle_id=cycle_id, sprint_id=f"sprint_{cycle_id}_w1")
    # Idempotent recovery -> ZERO new calls
    assert len(calls_2) == 0
    assert res_retry.sprint_id == res_first.sprint_id
    assert res_retry.overall_report.report_id == res_first.overall_report.report_id
    assert res_retry.report_date == res_first.report_date


@pytest.mark.anyio
async def test_argon_bug01_patient_and_cycle_isolation(db_session):
    """
    Proves that Athena clinical logs from one patient/cycle NEVER leak into another
    patient/cycle's Weekly Consilium prompts.
    """
    t0 = datetime(2026, 10, 18, 14, 0, 0, tzinfo=timezone.utc)
    clock = ControlledTime(t0)

    # Setup Patient A
    cycle_a = "cycle_attr_patient_a"
    patient_a = "patient_attr_a"
    await _setup_cycle_with_primary(db_session, cycle_id=cycle_a, patient_id=patient_a)

    # Setup Patient B
    cycle_b = "cycle_attr_patient_b"
    patient_b = "patient_attr_b"
    await _setup_cycle_with_primary(db_session, cycle_id=cycle_b, patient_id=patient_b)

    svc = WeeklyConsiliumService(db=db_session, now_provider=clock.now)
    svc.record_schedule_anchor(cycle_id=cycle_a, anchor_dt=t0)
    svc.record_schedule_anchor(cycle_id=cycle_b, anchor_dt=t0)

    # Patient A log
    svc.save_athena_clinical_log(
        cycle_id=cycle_a,
        log_entry={
            "status": "NORMAL",
            "symptoms_mentioned": ["patient_a_symptom"],
            "subjective_wellbeing_score": 8,
            "adherence_to_plan": "HIGH",
            "red_flags": False,
            "summary_for_master": "SECRET_OBSERVATION_PATIENT_A",
            "timestamp": (t0 + timedelta(hours=30)).isoformat(),
        },
    )

    # Patient B log
    svc.save_athena_clinical_log(
        cycle_id=cycle_b,
        log_entry={
            "status": "NORMAL",
            "symptoms_mentioned": ["patient_b_symptom"],
            "subjective_wellbeing_score": 8,
            "adherence_to_plan": "HIGH",
            "red_flags": False,
            "summary_for_master": "SECRET_OBSERVATION_PATIENT_B",
            "timestamp": (t0 + timedelta(hours=30)).isoformat(),
        },
    )

    clock.advance(hours=168)

    # Run for Patient A
    calls_a: List[Dict[str, Any]] = []
    gw_a = _build_attribution_test_gateway(calls_a)
    svc_a = WeeklyConsiliumService(db=db_session, gateway=gw_a, now_provider=clock.now)
    await svc_a.run_weekly_consilium(cycle_id=cycle_a, sprint_id=f"sprint_{cycle_a}_w1")

    assert len(calls_a) == 2
    for c in calls_a:
        assert "SECRET_OBSERVATION_PATIENT_A" in c["prompt"]
        assert "SECRET_OBSERVATION_PATIENT_B" not in c["prompt"]

    # Run for Patient B
    calls_b: List[Dict[str, Any]] = []
    gw_b = _build_attribution_test_gateway(calls_b)
    svc_b = WeeklyConsiliumService(db=db_session, gateway=gw_b, now_provider=clock.now)
    await svc_b.run_weekly_consilium(cycle_id=cycle_b, sprint_id=f"sprint_{cycle_b}_w1")

    assert len(calls_b) == 2
    for c in calls_b:
        assert "SECRET_OBSERVATION_PATIENT_B" in c["prompt"]
        assert "SECRET_OBSERVATION_PATIENT_A" not in c["prompt"]
