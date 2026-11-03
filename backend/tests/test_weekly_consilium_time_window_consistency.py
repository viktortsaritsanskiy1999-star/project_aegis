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
from app.schemas.consilium import (
    SYSTEM_KEYS,
    AthenaClinicalLogEntry,
)
from app.schemas.privacy import PrivacyLevel
from app.services.gemini_gateway import GeminiGateway
from app.services.primary_consilium_service import PrimaryConsiliumService
from app.services.weekly_consilium_service import (
    WeeklyConsiliumService,
    ATHENA_SCHEDULE_ANCHOR_REPORT_TYPE,
    ATHENA_CLINICAL_LOG_REPORT_TYPE,
    ATHENA_ACTIVE_STRATEGY_REPORT_TYPE,
    WEEKLY_OVERALL_CLIENT_REPORT_TYPE,
    WEEKLY_CONSILIUM_EXECUTION_LOG_REPORT_TYPE,
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
        url_path = request.url.path

        recorded_calls.append({
            "index": idx,
            "url_path": url_path,
            "prompt": prompt_text,
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
            else "Routine clinical observation."
        )
        chat_reply = (
            "Здравствуйте! Показатели зафиксированы.\n\n"
            "[CLINICAL_REPORT]\n"
            "{\n"
            '  "status": "NORMAL",\n'
            '  "symptoms_mentioned": ["tracked_item"],\n'
            '  "subjective_wellbeing_score": 8,\n'
            '  "adherence_to_plan": "HIGH",\n'
            '  "red_flags": false,\n'
            f'  "summary_for_master": "{summary}"\n'
            "}\n"
            "[/CLINICAL_REPORT]"
        )
        return httpx.Response(200, json=_text_envelope(chat_reply))

    return GeminiGateway(
        api_key="test-bug07-gateway-key",
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
async def test_utc_clock_injection(db_session):
    """
    1. Proves that WeeklyConsiliumService uses the injected now_provider:
       - Default resolved_date when report_date is None uses injected clock.
       - Fallback _resolve_seven_day_window uses injected clock.
       - Cross-process lease and execution logs record timestamps matching injected clock.
    """
    cycle_id = "cycle_bug07_clock"
    patient_id = "patient_bug07_clock"
    injected_dt = datetime(2027, 5, 20, 14, 30, 0, tzinfo=timezone.utc)
    clock = ControlledTime(injected_dt)

    await _setup_cycle_with_primary(db_session, cycle_id=cycle_id, patient_id=patient_id)

    recorded_calls: List[Dict[str, Any]] = []
    gateway = _build_test_gateway(recorded_calls)

    service = WeeklyConsiliumService(
        db=db_session,
        gateway=gateway,
        now_provider=clock.now,
    )

    # 1. Verify _now_utc returns injected time
    assert service._now_utc() == injected_dt

    # 2. Verify _resolve_seven_day_window with reference_date=None falls back to injected time
    w_start, w_end = service._resolve_seven_day_window(None)
    assert w_end == injected_dt
    assert w_start == datetime(2027, 5, 13, 0, 0, 0, tzinfo=timezone.utc)

    # 3. Seed anchor in the past to allow Weekly Consilium execution
    anchor_time = injected_dt - timedelta(hours=168)
    service.record_schedule_anchor(cycle_id=cycle_id, anchor_dt=anchor_time)

    # 4. Run weekly consilium with report_date=None: resolved_date must match injected clock ("2027-05-20")
    weekly_res = await service.run_weekly_consilium(
        cycle_id=cycle_id,
        sprint_id=f"sprint_{cycle_id}_w1",
        report_date=None,
    )
    assert weekly_res.report_date == "2027-05-20"


@pytest.mark.anyio
async def test_exact_period_boundary(db_session):
    """
    2. Proves exact period boundary behavior on the half-open interval [T0, T0 + 168h):
       - Log at T0 - 10m is included in Week 1.
       - Log at T0 + 167h 59m 59s is included in Week 1.
       - Log at T0 + 168h 00m 00s is excluded from Week 1 and included in Week 2.
       - Log at T0 + 335h 59m 59s is included in Week 2.
       - Log at T0 + 336h 00m 00s is excluded from Week 2.
    """
    cycle_id = "cycle_bug07_boundary"
    patient_id = "patient_bug07_boundary"
    t0 = datetime(2026, 10, 18, 9, 0, 0, tzinfo=timezone.utc)
    clock = ControlledTime(t0)

    await _setup_cycle_with_primary(db_session, cycle_id=cycle_id, patient_id=patient_id)

    recorded_calls: List[Dict[str, Any]] = []
    gateway = _build_test_gateway(recorded_calls)
    service = WeeklyConsiliumService(db=db_session, gateway=gateway, now_provider=clock.now)

    # Set schedule anchor T0
    service.record_schedule_anchor(cycle_id=cycle_id, anchor_dt=t0)

    # Helper to save a log at an explicit ISO timestamp
    def save_log_at(iso_ts: str, summary: str, sprint_id: str):
        entry = AthenaClinicalLogEntry(
            status="NORMAL",
            symptoms_mentioned=[],
            subjective_wellbeing_score=8,
            adherence_to_plan="HIGH",
            red_flags=False,
            summary_for_master=summary,
            timestamp=iso_ts,
        )
        service.save_athena_clinical_log(
            cycle_id=cycle_id,
            log_entry=entry,
            sprint_id=sprint_id,
        )

    # 1. Log at T0 - 5 min (within grace window [T0 - 10m, T0 + 168h))
    save_log_at((t0 - timedelta(minutes=5)).isoformat(), "LOG_BEFORE_T0_GRACE", f"sprint_{cycle_id}_w1")

    # 2. Log at T0 + 167h 59m 59s (just before Week 1 boundary)
    save_log_at((t0 + timedelta(hours=167, minutes=59, seconds=59)).isoformat(), "LOG_W1_LAST_SECOND", f"sprint_{cycle_id}_w1")

    # 3. Log at exactly T0 + 168h 00m 00s (boundary -> Week 2 start)
    save_log_at((t0 + timedelta(hours=168)).isoformat(), "LOG_W2_EXACT_BOUNDARY", f"sprint_{cycle_id}_w2")

    # 4. Log at T0 + 335h 59m 59s (just before Week 2 boundary)
    save_log_at((t0 + timedelta(hours=335, minutes=59, seconds=59)).isoformat(), "LOG_W2_LAST_SECOND", f"sprint_{cycle_id}_w2")

    # 5. Log at exactly T0 + 336h 00m 00s (Week 3 start)
    save_log_at((t0 + timedelta(hours=336)).isoformat(), "LOG_W3_BOUNDARY", f"sprint_{cycle_id}_w3")

    # Query Week 1 logs:
    w1_logs = service.get_seven_day_athena_logs(cycle_id=cycle_id, sprint_number=1)
    w1_summaries = [l.summary_for_master for l in w1_logs]
    assert "LOG_BEFORE_T0_GRACE" in w1_summaries
    assert "LOG_W1_LAST_SECOND" in w1_summaries
    assert "LOG_W2_EXACT_BOUNDARY" not in w1_summaries
    assert "LOG_W2_LAST_SECOND" not in w1_summaries
    assert "LOG_W3_BOUNDARY" not in w1_summaries

    # Query Week 2 logs:
    w2_logs = service.get_seven_day_athena_logs(cycle_id=cycle_id, sprint_number=2)
    w2_summaries = [l.summary_for_master for l in w2_logs]
    assert "LOG_W2_EXACT_BOUNDARY" in w2_summaries
    assert "LOG_W2_LAST_SECOND" in w2_summaries
    assert "LOG_BEFORE_T0_GRACE" not in w2_summaries
    assert "LOG_W1_LAST_SECOND" not in w2_summaries
    assert "LOG_W3_BOUNDARY" not in w2_summaries


@pytest.mark.anyio
async def test_delayed_execution_excludes_future_weeks(db_session):
    """
    3. Proves that when Week 1 Weekly Consilium is executed with delay (e.g. at Day 10 / T0 + 240h),
       its observation window is strictly [T0 - 10m, T0 + 168h) and excludes all Week 2 observations.
       The execution log documents the distinction between execution date and observation period.
    """
    cycle_id = "cycle_bug07_delayed"
    patient_id = "patient_bug07_delayed"
    t0 = datetime(2026, 10, 18, 9, 0, 0, tzinfo=timezone.utc)
    clock = ControlledTime(t0)

    await _setup_cycle_with_primary(db_session, cycle_id=cycle_id, patient_id=patient_id)

    recorded_calls: List[Dict[str, Any]] = []
    gateway = _build_test_gateway(recorded_calls)
    service = WeeklyConsiliumService(db=db_session, gateway=gateway, now_provider=clock.now)

    # Establish anchor T0
    service.record_schedule_anchor(cycle_id=cycle_id, anchor_dt=t0)

    # Week 1 observation
    entry_w1 = AthenaClinicalLogEntry(
        status="NORMAL",
        symptoms_mentioned=[],
        subjective_wellbeing_score=9,
        adherence_to_plan="HIGH",
        red_flags=False,
        summary_for_master="W1_WEEKDAY_OBSERVATION",
        timestamp=(t0 + timedelta(hours=48)).isoformat(),
    )
    service.save_athena_clinical_log(cycle_id=cycle_id, log_entry=entry_w1, sprint_id=f"sprint_{cycle_id}_w1")

    # Day 8 observation (Week 2 Day 1: T0 + 172h)
    entry_w2_d1 = AthenaClinicalLogEntry(
        status="NORMAL",
        symptoms_mentioned=["fatigue"],
        subjective_wellbeing_score=7,
        adherence_to_plan="MEDIUM",
        red_flags=False,
        summary_for_master="W2_DAY8_OBSERVATION_MUST_NOT_LEAK",
        timestamp=(t0 + timedelta(hours=172)).isoformat(),
    )
    service.save_athena_clinical_log(cycle_id=cycle_id, log_entry=entry_w2_d1, sprint_id=f"sprint_{cycle_id}_w2")

    # Day 10 observation (Week 2 Day 3: T0 + 220h)
    entry_w2_d3 = AthenaClinicalLogEntry(
        status="NORMAL",
        symptoms_mentioned=[],
        subjective_wellbeing_score=8,
        adherence_to_plan="HIGH",
        red_flags=False,
        summary_for_master="W2_DAY10_OBSERVATION_MUST_NOT_LEAK",
        timestamp=(t0 + timedelta(hours=220)).isoformat(),
    )
    service.save_athena_clinical_log(cycle_id=cycle_id, log_entry=entry_w2_d3, sprint_id=f"sprint_{cycle_id}_w2")

    # Advance clock to Day 10 (T0 + 240h): late execution of Week 1 Consilium!
    clock.advance(hours=240)
    assert clock.now() == t0 + timedelta(hours=240)

    # Execute delayed Week 1 Weekly Consilium
    res = await service.run_weekly_consilium(
        cycle_id=cycle_id,
        sprint_id=f"sprint_{cycle_id}_w1",
        report_date=None,  # default via clock
    )
    assert res.sprint_number == 1

    # Verify Gemini prompts sent to Flash Deputy and Pro Chief
    flash_call = next(c for c in recorded_calls if "head of department" in c["prompt"].lower())
    pro_call = next(c for c in recorded_calls if "chief physician" in c["prompt"].lower())

    assert "W1_WEEKDAY_OBSERVATION" in flash_call["prompt"]
    assert "W2_DAY8_OBSERVATION_MUST_NOT_LEAK" not in flash_call["prompt"]
    assert "W2_DAY10_OBSERVATION_MUST_NOT_LEAK" not in flash_call["prompt"]

    assert "W1_WEEKDAY_OBSERVATION" in pro_call["prompt"]
    assert "W2_DAY8_OBSERVATION_MUST_NOT_LEAK" not in pro_call["prompt"]
    assert "W2_DAY10_OBSERVATION_MUST_NOT_LEAK" not in pro_call["prompt"]

    # Verify execution log captures both execution time and fixed observation period
    exec_logs = service.report_repo.list_server_only(
        cycle_id=cycle_id,
        report_type=WEEKLY_CONSILIUM_EXECUTION_LOG_REPORT_TYPE,
    )
    assert len(exec_logs) >= 1
    log_content = exec_logs[0].content

    # Execution date reflects actual moment of execution (T0 + 240h)
    assert log_content["execution_date_utc"] == (t0 + timedelta(hours=240)).isoformat()
    # Observation period reflects the fixed 168-hour window of Week 1
    assert log_content["observation_period_start_utc"] == (t0 - timedelta(minutes=10)).isoformat()
    assert log_content["observation_period_end_utc"] == (t0 + timedelta(hours=168)).isoformat()


@pytest.mark.anyio
async def test_week1_and_week2_isolation(db_session):
    """
    4. Proves full multi-week isolation: Week 1 and Week 2 Consiliums executed sequentially
       each read exclusively their own fixed 168-hour periods.
    """
    cycle_id = "cycle_bug07_iso"
    patient_id = "patient_bug07_iso"
    t0 = datetime(2026, 10, 18, 9, 0, 0, tzinfo=timezone.utc)
    clock = ControlledTime(t0)

    await _setup_cycle_with_primary(db_session, cycle_id=cycle_id, patient_id=patient_id)

    recorded_calls: List[Dict[str, Any]] = []
    gateway = _build_test_gateway(recorded_calls)
    service = WeeklyConsiliumService(db=db_session, gateway=gateway, now_provider=clock.now)

    service.record_schedule_anchor(cycle_id=cycle_id, anchor_dt=t0)

    # Week 1 observations
    for d in [1, 3, 5, 7]:
        service.save_athena_clinical_log(
            cycle_id=cycle_id,
            log_entry=AthenaClinicalLogEntry(
                status="NORMAL",
                symptoms_mentioned=[],
                subjective_wellbeing_score=8,
                adherence_to_plan="HIGH",
                red_flags=False,
                summary_for_master=f"OBS_WEEK_1_DAY_{d}",
                timestamp=(t0 + timedelta(days=d-1, hours=2)).isoformat(),
            ),
            sprint_id=f"sprint_{cycle_id}_w1",
        )

    # Week 2 observations
    for d in [8, 10, 12, 14]:
        service.save_athena_clinical_log(
            cycle_id=cycle_id,
            log_entry=AthenaClinicalLogEntry(
                status="NORMAL",
                symptoms_mentioned=[],
                subjective_wellbeing_score=8,
                adherence_to_plan="HIGH",
                red_flags=False,
                summary_for_master=f"OBS_WEEK_2_DAY_{d}",
                timestamp=(t0 + timedelta(days=d-1, hours=2)).isoformat(),
            ),
            sprint_id=f"sprint_{cycle_id}_w2",
        )

    # 1. Run Week 1 Consilium at T0 + 168h
    clock.advance(hours=168)
    res_w1 = await service.run_weekly_consilium(
        cycle_id=cycle_id,
        sprint_id=f"sprint_{cycle_id}_w1",
    )
    assert res_w1.sprint_number == 1

    # 2. Run Week 2 Consilium at T0 + 336h
    clock.advance(hours=168)
    res_w2 = await service.run_weekly_consilium(
        cycle_id=cycle_id,
        sprint_id=f"sprint_{cycle_id}_w2",
    )
    assert res_w2.sprint_number == 2

    # Check Deputy and Chief prompts for both weeks
    w1_flash = recorded_calls[0]["prompt"]
    w2_flash = recorded_calls[2]["prompt"]

    for d in [1, 3, 5, 7]:
        assert f"OBS_WEEK_1_DAY_{d}" in w1_flash
        assert f"OBS_WEEK_1_DAY_{d}" not in w2_flash

    for d in [8, 10, 12, 14]:
        assert f"OBS_WEEK_2_DAY_{d}" not in w1_flash
        assert f"OBS_WEEK_2_DAY_{d}" in w2_flash


@pytest.mark.anyio
async def test_no_regression_in_weekly_consilium_reports(db_session):
    """
    5. Proves no regression in Weekly reports:
       - 8 client-facing system reports and 1 overall report are generated and persisted.
       - ModelReport schema contracts and validation remain intact.
       - Replay recovers completed reports without re-running Gemini.
    """
    cycle_id = "cycle_bug07_reg"
    patient_id = "patient_bug07_reg"
    t0 = datetime(2026, 10, 18, 9, 0, 0, tzinfo=timezone.utc)
    clock = ControlledTime(t0)

    await _setup_cycle_with_primary(db_session, cycle_id=cycle_id, patient_id=patient_id)

    recorded_calls: List[Dict[str, Any]] = []
    gateway = _build_test_gateway(recorded_calls)
    service = WeeklyConsiliumService(db=db_session, gateway=gateway, now_provider=clock.now)

    service.record_schedule_anchor(cycle_id=cycle_id, anchor_dt=t0)
    clock.advance(hours=168)

    res = await service.run_weekly_consilium(
        cycle_id=cycle_id,
        sprint_id=f"sprint_{cycle_id}_w1",
        report_date="2026-10-25",
        idempotency_key="idemp_bug07_reg_01",
    )

    assert res.cycle_id == cycle_id
    assert res.sprint_id == f"sprint_{cycle_id}_w1"
    assert res.sprint_number == 1
    assert res.report_date == "2026-10-25"
    assert res.overall_report.content.title == "Aegis Week 1 Wellness Summary"
    assert len(res.system_reports) == len(SYSTEM_KEYS)
    for k in SYSTEM_KEYS:
        assert k in res.system_reports
        assert res.system_reports[k].content.system_key == k

    # Replay check: second execution with same idempotency key recovers response with zero Gemini calls
    initial_calls = len(recorded_calls)
    replayed = await service.run_weekly_consilium(
        cycle_id=cycle_id,
        sprint_id=f"sprint_{cycle_id}_w1",
        idempotency_key="idemp_bug07_reg_01",
    )
    assert len(recorded_calls) == initial_calls
    assert replayed.sprint_id == res.sprint_id
    assert replayed.overall_report.report_id == res.overall_report.report_id
