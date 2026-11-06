import asyncio
from datetime import datetime, timedelta, timezone
import io
import json
from typing import Any, Callable, Dict, List, Optional
import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.models.base import Base
from app.models.medical_cycle import MedicalCycle
from app.models.model_report import ModelReport
from app.repositories.cycle_repository import MedicalCycleRepository
from app.repositories.patient_repository import PatientRepository
from app.repositories.report_repository import ModelReportRepository
from app.repositories.measurement_repository import HealthMeasurementRepository
from app.schemas.consilium import (
    SYSTEM_KEYS,
    AegisAgentRole,
    AnamnesisModelOutput,
    LabEventType,
    LabMarkerMutationItem,
    SystemAgentOutput,
    SystemAgentView,
    SystemAgentLogic,
    SystemAgentSandbox,
)
from app.schemas.lab_scheduling import (
    BackgroundLabRevisionChiefOutput,
    BackgroundLabRevisionDeputyOutput,
    BackgroundLabRevisionReportRecord,
    LabChangeType,
    LabDocumentRecord,
    LabDocumentStatus,
    LabRevisionPendingTask,
)
from app.schemas.privacy import PrivacyLevel
from app.services.clinical_automation_service import (
    AutomationJobRecord,
    AutomationJobStatus,
    AutomationJobType,
    ClinicalAutomationService,
)
from app.services.gemini_gateway import GeminiGateway, GeminiGatewayError, GeminiModelRole
from app.services.lab_revision_consilium_service import (
    BACKGROUND_LAB_REVISION_REPORT_TYPE,
    LAB_REVISION_EXECUTION_LOG_REPORT_TYPE,
    LABORATORY_DIFF_AUDIT_REPORT_TYPE,
    LABORATORY_LEDGER_REPORT_TYPE,
    LabRevisionConsiliumService,
)
from app.services.lab_scheduling_service import (
    BACKGROUND_LAB_REVISION_COOLDOWN_HOURS,
    LAB_REVISION_DEBOUNCE_MINUTES,
    LabSchedulingService,
)
from app.services.primary_consilium_service import (
    PREPARATORY_ANAMNESIS_REPORT_TYPE,
    PrimaryConsiliumService,
)
from app.services.weekly_consilium_service import (
    ATHENA_ACTIVE_STRATEGY_REPORT_TYPE,
    WeeklyConsiliumService,
)
from tests.test_primary_consilium import (
    _sample_anamnesis_output,
    _seed_preparatory_cycle_context,
    _build_consilium_mock_gateway,
)


class MockClock:
    def __init__(self, start: datetime):
        self._current = start

    def now(self) -> datetime:
        return self._current

    def advance(self, minutes: int = 0, hours: int = 0, seconds: int = 0) -> datetime:
        self._current += timedelta(minutes=minutes, hours=hours, seconds=seconds)
        return self._current

    def set(self, dt: datetime) -> None:
        self._current = dt


def _gemini_envelope(payload: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "candidates": [
            {
                "content": {
                    "parts": [{"text": json.dumps(payload)}],
                    "role": "model",
                },
                "finishReason": "STOP",
            }
        ]
    }


def _sample_system_agent_output(system_key: str) -> Dict[str, Any]:
    return {
        "system_view": {
            "isolated_labs": {f"{system_key}_marker": "within reference"},
        },
        "system_logic": {
            "system_general_shelf": f"Analytical evaluation of {system_key}: markers remain clinically consistent.",
        },
        "training_sandbox": {
            "system_training_shelf": f"Training rationale for {system_key} based on updated laboratory diff.",
        },
    }


def _sample_deputy_output() -> Dict[str, Any]:
    return {
        "deputy_synthesis": "Comparative intersystem synthesis: new glucose and ferritin markers show mild metabolic shift while renal parameters remain stable.",
        "specialist_discrepancies": ["No major discrepancies observed between endocrinologist and nephrologist."],
        "clinical_correlations": ["Metabolic-cardiovascular interaction: mildly elevated glucose aligns with resting heart rate trend."],
        "data_limitations": ["HbA1c and lipid panel not yet available in current ledger."],
        "draft_hypotheses": ["Early insulin resistance hypothesis to be evaluated in next Weekly sprint."],
    }


def _sample_chief_output() -> Dict[str, Any]:
    return {
        "analytical_summary": "Chief Physician Supreme Analytical Verdict: Confirmed ferritin error correction and new glucose study. Patient remains hemodynamically compensated.",
        "observed_facts": [
            "Ferritin corrected from typo 450 to 115 mcg/L.",
            "Venous blood glucose observed at 5.4 mmol/L on 2026-11-06.",
        ],
        "clinical_hypotheses": [
            "Transient postprandial glucose elevation vs early metabolic syndrome.",
        ],
        "uncertainties_and_data_deficits": [
            "Lack of serial HbA1c to confirm glucose trajectory.",
        ],
        "questions_for_weekly_consilium": [
            "Evaluate fasting glucose trend across the full 7-day sprint window.",
            "Assess whether nutrition timing recommendations need reinforcement.",
        ],
        "cascade_quality_audit": "8 Flash-Lite specialists correctly distinguished error correction from new dated studies. Deputy Flash synthesis approved.",
    }


def _build_stage_6b_mock_gateway(
    recorded_calls: List[Dict[str, Any]],
    fail_on_call_index: Optional[int] = None,
    artificial_delay_sec: float = 0.0,
) -> GeminiGateway:
    """
    Mock GeminiGateway for Stage 6B Ten-Model Lab Revision.
    Correctly responds according to requested agent prompt:
    - 8 specialists: SystemAgentOutput
    - Deputy Chief: BackgroundLabRevisionDeputyOutput
    - Chief Pro: BackgroundLabRevisionChiefOutput
    """
    call_counter = {"index": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        if artificial_delay_sec > 0:
            await asyncio.sleep(artificial_delay_sec)

        idx = call_counter["index"]
        call_counter["index"] += 1

        body = json.loads(request.content.decode("utf-8"))
        prompt_text = ""
        for part in body.get("contents", [{}])[0].get("parts", []):
            prompt_text += part.get("text", "")
        url_path = str(request.url)

        recorded_calls.append(
            {
                "index": idx,
                "url": url_path,
                "prompt": prompt_text,
            }
        )

        if fail_on_call_index is not None and idx >= fail_on_call_index:
            return httpx.Response(
                502,
                json={"error": {"message": f"Injected transient gateway error at call index {idx}"}},
            )

        if "BACKGROUND COMPARATIVE LAB-REVISION ANALYSIS" in prompt_text:
            return httpx.Response(200, json=_gemini_envelope(_sample_deputy_output()))

        if "SUPREME ANALYTICAL LAB-REVISION REPORT" in prompt_text:
            return httpx.Response(200, json=_gemini_envelope(_sample_chief_output()))

        if "Вы — Афина" in prompt_text or "Первичное приветствие" in prompt_text or "POST_PRIMARY" in prompt_text or "Athena" in prompt_text:
            return httpx.Response(
                200,
                json={
                    "candidates": [
                        {
                            "content": {
                                "parts": [
                                    {
                                        "text": "Здравствуйте! Я Афина, ваш куратор Aegis.\n[CLINICAL_REPORT]\n{\n  \"status\": \"INITIALIZATION\",\n  \"symptoms_mentioned\": [],\n  \"subjective_wellbeing_score\": null,\n  \"adherence_to_plan\": \"UNKNOWN\",\n  \"red_flags\": false,\n  \"summary_for_master\": \"Первичное приветствие куратора отправлено.\"\n}\n[/CLINICAL_REPORT]"
                                    }
                                ],
                                "role": "model",
                            },
                            "finishReason": "STOP",
                        }
                    ]
                },
            )

        # Organ specialist (Flash-Lite)
        for sk in SYSTEM_KEYS:
            if f"'{sk}'" in prompt_text or f"the '{sk}' system" in prompt_text:
                return httpx.Response(200, json=_gemini_envelope(_sample_system_agent_output(sk)))

        # Default fallback to specialist
        return httpx.Response(200, json=_gemini_envelope(_sample_system_agent_output("cvs")))

    return GeminiGateway(
        api_key="test-stage-6b-key",
        transport=httpx.MockTransport(handler),
        sleep_func=lambda _: asyncio.sleep(0),
    )


@pytest.fixture
def db_session() -> Session:
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    session = Session(bind=engine)
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
    p_repo = PatientRepository(session)
    if not p_repo.get(patient_id):
        p_repo.create(patient_id=patient_id, status="active")

    c_repo = MedicalCycleRepository(session)
    if not c_repo.get(cycle_id):
        c_repo.create(
            cycle_id=cycle_id,
            patient_id=patient_id,
            status="active",
            started_at=anchor_dt,
        )


async def _seed_active_cycle_with_baseline(
    session: Session,
    patient_id: str,
    cycle_id: str,
    anchor_dt: datetime,
) -> None:
    """Seeds patient, cycle, preparatory anamnesis, baseline labs, vitals, and Primary Consilium."""
    _seed_patient_and_cycle(session, patient_id, cycle_id, anchor_dt)
    _seed_preparatory_cycle_context(session, cycle_id=cycle_id, seed_ledger=True)

    primary_calls: List[Dict[str, Any]] = []
    primary_gateway = _build_consilium_mock_gateway(primary_calls)
    primary_service = PrimaryConsiliumService(
        db=session,
        gateway=primary_gateway,
        now_provider=lambda: anchor_dt,
    )
    await primary_service.run_primary_consilium(
        cycle_id=cycle_id,
        report_date=anchor_dt.strftime("%Y-%m-%d"),
    )

    # Mark post_primary_init job as completed so automation workers focus strictly on lab revision
    auto_svc = ClinicalAutomationService(
        db=session,
        gateway=primary_gateway,
        now_provider=lambda: anchor_dt,
    )
    init_job = auto_svc.enqueue_post_primary_init(cycle_id=cycle_id, patient_id=patient_id)
    if init_job is not None:
        row = session.query(ModelReport).filter(ModelReport.report_id == init_job.job_id).first()
        if row is not None:
            c = dict(row.content or {})
            c["status"] = AutomationJobStatus.COMPLETED.value
            row.content = c
            session.commit()


# ==============================================================================
# TESTS 1 - 20
# ==============================================================================

@pytest.mark.anyio
async def test_01_ready_task_auto_executed_by_worker(db_session: Session):
    """
    1. Ready task is discovered, claimed, and executed automatically by ClinicalAutomationService.
    Debounce (15m) and cooldown (0 initially) have passed.
    """
    t0 = datetime(2026, 11, 6, 8, 0, tzinfo=timezone.utc)
    clock = MockClock(t0)
    cycle_id = "cyc_6b_01"
    patient_id = "pat_6b_01"
    await _seed_active_cycle_with_baseline(db_session, patient_id, cycle_id, t0)

    recorded_calls: List[Dict[str, Any]] = []
    gw = _build_stage_6b_mock_gateway(recorded_calls)

    sched_svc = LabSchedulingService(db=db_session, gateway=gw, now_provider=clock.now)
    # Manual edit introducing meaningful change (4 -> 4.5)
    resp = sched_svc.process_manual_edit(
        cycle_id=cycle_id,
        patient_id=patient_id,
        mutations=[
            LabMarkerMutationItem(
                marker_key="glucose",
                display_name="Glucose",
                value=5.4,
                unit="mmol/L",
                sample_date="2026-11-06",
            )
        ],
    )
    assert resp.change_type == LabChangeType.MEANINGFUL_CHANGE
    assert not sched_svc.evaluate_task_readiness(cycle_id)[0]  # In debounce

    # Advance past 15-minute debounce
    clock.advance(minutes=16)
    is_ready, reason = sched_svc.evaluate_task_readiness(cycle_id)
    assert is_ready, reason

    auto_svc = ClinicalAutomationService(
        db=db_session,
        gateway=gw,
        now_provider=clock.now,
        enable_lab_revision=True,
    )

    processed = await auto_svc.process_due_jobs(discover=True)
    assert len(processed) >= 1
    lab_job = next(j for j in processed if j.job_type == AutomationJobType.LAB_REVISION)
    assert lab_job.status == AutomationJobStatus.COMPLETED

    # Check that LabRevisionPendingTask is now marked completed
    task = sched_svc.get_pending_task(cycle_id)
    assert task is not None
    assert task.status == "completed"

    # Exactly 10 model calls were executed (8 Flash-Lite + 1 Flash + 1 Pro)
    assert len(recorded_calls) == 10


@pytest.mark.anyio
async def test_02_task_not_executed_before_debounce_ends(db_session: Session):
    """
    2. Before debounce (15 minutes) elapses, task is not ready and worker does NOT execute it.
    """
    t0 = datetime(2026, 11, 6, 8, 0, tzinfo=timezone.utc)
    clock = MockClock(t0)
    cycle_id = "cyc_6b_02"
    patient_id = "pat_6b_02"
    await _seed_active_cycle_with_baseline(db_session, patient_id, cycle_id, t0)

    recorded_calls: List[Dict[str, Any]] = []
    gw = _build_stage_6b_mock_gateway(recorded_calls)

    sched_svc = LabSchedulingService(db=db_session, gateway=gw, now_provider=clock.now)
    sched_svc.process_manual_edit(
        cycle_id=cycle_id,
        patient_id=patient_id,
        mutations=[
            LabMarkerMutationItem(
                marker_key="alt",
                display_name="ALT",
                value=35.0,
                unit="U/L",
                sample_date="2026-11-06",
            )
        ],
    )

    # Advance only 10 minutes (5 minutes left in debounce)
    clock.advance(minutes=10)
    is_ready, reason = sched_svc.evaluate_task_readiness(cycle_id)
    assert not is_ready
    assert "Debounce period active" in reason

    auto_svc = ClinicalAutomationService(
        db=db_session,
        gateway=gw,
        now_provider=clock.now,
        enable_lab_revision=True,
    )
    processed = await auto_svc.process_due_jobs(discover=True)
    lab_jobs = [j for j in processed if j.job_type == AutomationJobType.LAB_REVISION]
    assert len(lab_jobs) == 0
    assert len(recorded_calls) == 0


@pytest.mark.anyio
async def test_03_cascade_not_executed_during_cooldown(db_session: Session):
    """
    3. During 72-hour cooldown after a successful revision, cascade does not launch,
    even though debounce has elapsed. At T0 + 72h1m, it launches.
    """
    t0 = datetime(2026, 11, 6, 8, 0, tzinfo=timezone.utc)
    clock = MockClock(t0)
    cycle_id = "cyc_6b_03"
    patient_id = "pat_6b_03"
    await _seed_active_cycle_with_baseline(db_session, patient_id, cycle_id, t0)

    recorded_calls: List[Dict[str, Any]] = []
    gw = _build_stage_6b_mock_gateway(recorded_calls)

    sched_svc = LabSchedulingService(db=db_session, gateway=gw, now_provider=clock.now)
    auto_svc = ClinicalAutomationService(
        db=db_session, gateway=gw, now_provider=clock.now, enable_lab_revision=True
    )

    # 1. First revision (glucose 5.4)
    sched_svc.process_manual_edit(
        cycle_id=cycle_id,
        patient_id=patient_id,
        mutations=[LabMarkerMutationItem(marker_key="glucose", display_name="Glucose", value=5.4, unit="mmol/L", sample_date="2026-11-06")],
    )
    clock.advance(minutes=16)
    await auto_svc.process_due_jobs(discover=True)
    assert len(recorded_calls) == 10

    # 2. Upload new lab 2 hours later (alt 40)
    clock.advance(hours=2)
    sched_svc.process_manual_edit(
        cycle_id=cycle_id,
        patient_id=patient_id,
        mutations=[LabMarkerMutationItem(marker_key="alt", display_name="ALT", value=40.0, unit="U/L", sample_date="2026-11-06")],
    )

    # Advance past 15m debounce (still within 72h cooldown)
    clock.advance(minutes=30)
    is_ready, reason = sched_svc.evaluate_task_readiness(cycle_id)
    assert not is_ready
    assert "72-hour cooldown period active" in reason

    # Worker attempts job discovery -> nothing executed
    processed = await auto_svc.process_due_jobs(discover=True)
    lab_jobs = [j for j in processed if j.job_type == AutomationJobType.LAB_REVISION]
    assert len(lab_jobs) == 0
    assert len(recorded_calls) == 10  # No extra calls

    # Advance past 72h cooldown (70 hours more)
    clock.advance(hours=70, minutes=1)
    is_ready_now, _ = sched_svc.evaluate_task_readiness(cycle_id)
    assert is_ready_now

    # Worker executes second revision
    processed_2 = await auto_svc.process_due_jobs(discover=True)
    assert any(j.job_type == AutomationJobType.LAB_REVISION and j.status == AutomationJobStatus.COMPLETED for j in processed_2)
    assert len(recorded_calls) == 20  # Another 10 calls


@pytest.mark.anyio
async def test_04_eight_specialist_calls_executed_with_full_context(db_session: Session):
    """
    4. Eight specialist calls (FLASH_LITE) are executed in order for all 8 systems,
    and each prompt contains full anamnesis, vitals, shared labs, and lab diff.
    """
    t0 = datetime(2026, 11, 6, 8, 0, tzinfo=timezone.utc)
    clock = MockClock(t0)
    cycle_id = "cyc_6b_04"
    patient_id = "pat_6b_04"
    await _seed_active_cycle_with_baseline(db_session, patient_id, cycle_id, t0)

    recorded_calls: List[Dict[str, Any]] = []
    gw = _build_stage_6b_mock_gateway(recorded_calls)

    sched_svc = LabSchedulingService(db=db_session, gateway=gw, now_provider=clock.now)
    sched_svc.process_manual_edit(
        cycle_id=cycle_id,
        patient_id=patient_id,
        mutations=[LabMarkerMutationItem(marker_key="creatinine", display_name="Creatinine", value=95.0, unit="umol/L", sample_date="2026-11-06")],
    )
    clock.advance(minutes=16)

    auto_svc = ClinicalAutomationService(db=db_session, gateway=gw, now_provider=clock.now, enable_lab_revision=True)
    await auto_svc.process_due_jobs(discover=True)

    assert len(recorded_calls) == 10
    specialist_calls = recorded_calls[:8]

    for idx, call in enumerate(specialist_calls):
        expected_sys = SYSTEM_KEYS[idx]
        prompt = call["prompt"]
        assert expected_sys in prompt
        # Full medical context checks
        assert "DETERMINISTIC LABORATORY DIFF" in prompt
        assert "creatinine" in prompt.lower()
        assert "YOUR OWN PREVIOUS CONCLUSION" in prompt


@pytest.mark.anyio
async def test_05_flash_deputy_receives_specialist_results_and_history(db_session: Session):
    """
    5. Flash Deputy call (#8) receives all 8 specialist outputs, prior deputy conclusions,
    and comparative synthesis context.
    """
    t0 = datetime(2026, 11, 6, 8, 0, tzinfo=timezone.utc)
    clock = MockClock(t0)
    cycle_id = "cyc_6b_05"
    patient_id = "pat_6b_05"
    await _seed_active_cycle_with_baseline(db_session, patient_id, cycle_id, t0)

    recorded_calls: List[Dict[str, Any]] = []
    gw = _build_stage_6b_mock_gateway(recorded_calls)

    sched_svc = LabSchedulingService(db=db_session, gateway=gw, now_provider=clock.now)
    sched_svc.process_manual_edit(
        cycle_id=cycle_id,
        patient_id=patient_id,
        mutations=[LabMarkerMutationItem(marker_key="glucose", display_name="Glucose", value=6.1, unit="mmol/L", sample_date="2026-11-06")],
    )
    clock.advance(minutes=16)

    auto_svc = ClinicalAutomationService(db=db_session, gateway=gw, now_provider=clock.now, enable_lab_revision=True)
    await auto_svc.process_due_jobs(discover=True)

    deputy_call = recorded_calls[8]
    prompt = deputy_call["prompt"]
    assert "Deputy Chief Physician" in prompt
    assert "8 SPECIALIST REPORTS (CURRENT EVALUATION)" in prompt
    for sk in SYSTEM_KEYS:
        assert f"SYSTEM: {sk.upper()}" in prompt
    assert "PREVIOUS DEPUTY & CHIEF CONCLUSIONS" in prompt


@pytest.mark.anyio
async def test_06_pro_chief_receives_data_for_analytical_verdict(db_session: Session):
    """
    6. Pro Chief call (#9) receives the 8 specialist findings + Flash deputy synthesis
    + baseline verifiable data + prior Pro conclusions.
    """
    t0 = datetime(2026, 11, 6, 8, 0, tzinfo=timezone.utc)
    clock = MockClock(t0)
    cycle_id = "cyc_6b_06"
    patient_id = "pat_6b_06"
    await _seed_active_cycle_with_baseline(db_session, patient_id, cycle_id, t0)

    recorded_calls: List[Dict[str, Any]] = []
    gw = _build_stage_6b_mock_gateway(recorded_calls)

    sched_svc = LabSchedulingService(db=db_session, gateway=gw, now_provider=clock.now)
    sched_svc.process_manual_edit(
        cycle_id=cycle_id,
        patient_id=patient_id,
        mutations=[LabMarkerMutationItem(marker_key="glucose", display_name="Glucose", value=6.2, unit="mmol/L", sample_date="2026-11-06")],
    )
    clock.advance(minutes=16)

    auto_svc = ClinicalAutomationService(db=db_session, gateway=gw, now_provider=clock.now, enable_lab_revision=True)
    await auto_svc.process_due_jobs(discover=True)
    pro_call = recorded_calls[9]
    prompt = pro_call["prompt"]
    assert "Chief Physician (Gemini Pro)" in prompt
    assert "SUPREME ANALYTICAL LAB-REVISION REPORT" in prompt
    assert "DEPUTY CHIEF PHYSICIAN SYNTHESIS" in prompt
    assert "Questions for Next Weekly Consilium" in prompt


@pytest.mark.anyio
async def test_07_successful_conclusion_saved_with_correct_version(db_session: Session):
    """
    7. Successful revision saves BackgroundLabRevisionReportRecord in ModelReportRepository
    with evaluated_ledger_version = N and status = 'completed'.
    """
    t0 = datetime(2026, 11, 6, 8, 0, tzinfo=timezone.utc)
    clock = MockClock(t0)
    cycle_id = "cyc_6b_07"
    patient_id = "pat_6b_07"
    await _seed_active_cycle_with_baseline(db_session, patient_id, cycle_id, t0)

    recorded_calls: List[Dict[str, Any]] = []
    gw = _build_stage_6b_mock_gateway(recorded_calls)

    sched_svc = LabSchedulingService(db=db_session, gateway=gw, now_provider=clock.now)
    sched_svc.process_manual_edit(
        cycle_id=cycle_id,
        patient_id=patient_id,
        mutations=[LabMarkerMutationItem(marker_key="bilirubin_total", display_name="Bilirubin Total", value=18.0, unit="umol/L", sample_date="2026-11-06")],
    )
    clock.advance(minutes=16)

    auto_svc = ClinicalAutomationService(db=db_session, gateway=gw, now_provider=clock.now, enable_lab_revision=True)
    await auto_svc.process_due_jobs(discover=True)

    report_repo = ModelReportRepository(db_session)
    rev_reports = report_repo.list_server_only(cycle_id=cycle_id, report_type=BACKGROUND_LAB_REVISION_REPORT_TYPE)
    assert len(rev_reports) == 1
    content = rev_reports[0].content or {}
    record = BackgroundLabRevisionReportRecord.model_validate(content)
    assert record.status == "completed"
    assert record.evaluated_ledger_version > 0
    assert record.summary != ""
    assert len(record.specialist_reports) == 8


@pytest.mark.anyio
async def test_08_72h_cooldown_begins_only_after_complete_success(db_session: Session):
    """
    8. 72-hour cooldown starts strictly upon full successful completion.
    """
    t0 = datetime(2026, 11, 6, 8, 0, tzinfo=timezone.utc)
    clock = MockClock(t0)
    cycle_id = "cyc_6b_08"
    patient_id = "pat_6b_08"
    await _seed_active_cycle_with_baseline(db_session, patient_id, cycle_id, t0)

    recorded_calls: List[Dict[str, Any]] = []
    gw = _build_stage_6b_mock_gateway(recorded_calls)

    sched_svc = LabSchedulingService(db=db_session, gateway=gw, now_provider=clock.now)
    assert sched_svc.get_last_successful_revision_timestamp(cycle_id) is None

    sched_svc.process_manual_edit(
        cycle_id=cycle_id,
        patient_id=patient_id,
        mutations=[LabMarkerMutationItem(marker_key="glucose", display_name="Glucose", value=5.5, unit="mmol/L", sample_date="2026-11-06")],
    )
    clock.advance(minutes=16)

    auto_svc = ClinicalAutomationService(db=db_session, gateway=gw, now_provider=clock.now, enable_lab_revision=True)
    await auto_svc.process_due_jobs(discover=True)

    last_rev_dt = sched_svc.get_last_successful_revision_timestamp(cycle_id)
    assert last_rev_dt is not None
    # Timestamp corresponds to completion moment
    diff = abs((last_rev_dt - clock.now()).total_seconds())
    assert diff < 5


@pytest.mark.anyio
async def test_09_single_model_failure_does_not_count_as_success_and_no_cooldown(db_session: Session):
    """
    9. Failure on any model (e.g. call #9 Pro fails) aborts the consilium, rolls back,
    does NOT save background_lab_revision_report, and does NOT start cooldown.
    """
    t0 = datetime(2026, 11, 6, 8, 0, tzinfo=timezone.utc)
    clock = MockClock(t0)
    cycle_id = "cyc_6b_09"
    patient_id = "pat_6b_09"
    await _seed_active_cycle_with_baseline(db_session, patient_id, cycle_id, t0)

    recorded_calls: List[Dict[str, Any]] = []
    # Fail on call index 9 (Gemini Pro)
    failing_gw = _build_stage_6b_mock_gateway(recorded_calls, fail_on_call_index=9)

    sched_svc = LabSchedulingService(db=db_session, gateway=failing_gw, now_provider=clock.now)
    sched_svc.process_manual_edit(
        cycle_id=cycle_id,
        patient_id=patient_id,
        mutations=[LabMarkerMutationItem(marker_key="glucose", display_name="Glucose", value=5.6, unit="mmol/L", sample_date="2026-11-06")],
    )
    clock.advance(minutes=16)

    auto_svc = ClinicalAutomationService(db=db_session, gateway=failing_gw, now_provider=clock.now, enable_lab_revision=True)
    processed = await auto_svc.process_due_jobs(discover=True)
    lab_job = next(j for j in processed if j.job_type == AutomationJobType.LAB_REVISION)
    assert lab_job.status in (AutomationJobStatus.RETRYABLE_FAILURE, AutomationJobStatus.TERMINAL_FAILURE)

    # Verify no report and no cooldown
    report_repo = ModelReportRepository(db_session)
    assert len(report_repo.list_server_only(cycle_id, BACKGROUND_LAB_REVISION_REPORT_TYPE)) == 0
    assert sched_svc.get_last_successful_revision_timestamp(cycle_id) is None


@pytest.mark.anyio
async def test_10_retry_after_failure_does_not_duplicate_final_report(db_session: Session):
    """
    10. Retry after failure successfully completes without creating duplicate final reports.
    """
    t0 = datetime(2026, 11, 6, 8, 0, tzinfo=timezone.utc)
    clock = MockClock(t0)
    cycle_id = "cyc_6b_10"
    patient_id = "pat_6b_10"
    await _seed_active_cycle_with_baseline(db_session, patient_id, cycle_id, t0)

    recorded_calls: List[Dict[str, Any]] = []
    gw_fail_once = _build_stage_6b_mock_gateway(recorded_calls, fail_on_call_index=8)

    sched_svc = LabSchedulingService(db=db_session, gateway=gw_fail_once, now_provider=clock.now)
    sched_svc.process_manual_edit(
        cycle_id=cycle_id,
        patient_id=patient_id,
        mutations=[LabMarkerMutationItem(marker_key="alt", display_name="ALT", value=42.0, unit="U/L", sample_date="2026-11-06")],
    )
    clock.advance(minutes=16)

    # First attempt fails at call 8
    auto_svc = ClinicalAutomationService(db=db_session, gateway=gw_fail_once, now_provider=clock.now, enable_lab_revision=True)
    await auto_svc.process_due_jobs(discover=True)

    # Second attempt with healthy gateway
    healthy_calls: List[Dict[str, Any]] = []
    healthy_gw = _build_stage_6b_mock_gateway(healthy_calls)
    auto_svc.gateway = healthy_gw
    auto_svc.lab_revision_service.gateway = healthy_gw

    clock.advance(minutes=2)  # backoff passed
    await auto_svc.process_due_jobs(discover=False)

    report_repo = ModelReportRepository(db_session)
    reports = report_repo.list_server_only(cycle_id, BACKGROUND_LAB_REVISION_REPORT_TYPE)
    assert len(reports) == 1
    assert reports[0].content.get("status") == "completed"


@pytest.mark.anyio
async def test_11_server_restart_preserves_task_and_queue(db_session: Session):
    """
    11. Durable state in SQLite survives server restart; newly created services pick up task.
    """
    t0 = datetime(2026, 11, 6, 8, 0, tzinfo=timezone.utc)
    clock = MockClock(t0)
    cycle_id = "cyc_6b_11"
    patient_id = "pat_6b_11"
    await _seed_active_cycle_with_baseline(db_session, patient_id, cycle_id, t0)

    gw = _build_stage_6b_mock_gateway([])
    sched_svc_1 = LabSchedulingService(db=db_session, gateway=gw, now_provider=clock.now)
    sched_svc_1.process_manual_edit(
        cycle_id=cycle_id,
        patient_id=patient_id,
        mutations=[LabMarkerMutationItem(marker_key="glucose", display_name="Glucose", value=5.7, unit="mmol/L", sample_date="2026-11-06")],
    )
    clock.advance(minutes=16)

    # Simulate restart by instantiating new service instances
    calls_after_restart: List[Dict[str, Any]] = []
    gw_restart = _build_stage_6b_mock_gateway(calls_after_restart)
    sched_svc_2 = LabSchedulingService(db=db_session, gateway=gw_restart, now_provider=clock.now)
    ready = sched_svc_2.get_ready_tasks_for_execution()
    assert len(ready) == 1
    assert ready[0].cycle_id == cycle_id

    auto_svc_2 = ClinicalAutomationService(db=db_session, gateway=gw_restart, now_provider=clock.now, enable_lab_revision=True)
    processed = await auto_svc_2.process_due_jobs(discover=True)
    assert len(processed) >= 1
    assert processed[0].status == AutomationJobStatus.COMPLETED


@pytest.mark.anyio
async def test_12_parallel_workers_do_not_run_two_consiliums_same_cycle(db_session: Session):
    """
    12. Two parallel workers attempting to process the same cycle are serialized.
    One claims the job; the other worker gets None / conflict.
    """
    t0 = datetime(2026, 11, 6, 8, 0, tzinfo=timezone.utc)
    clock = MockClock(t0)
    cycle_id = "cyc_6b_12"
    patient_id = "pat_6b_12"
    await _seed_active_cycle_with_baseline(db_session, patient_id, cycle_id, t0)

    calls: List[Dict[str, Any]] = []
    gw = _build_stage_6b_mock_gateway(calls, artificial_delay_sec=0.01)

    sched_svc = LabSchedulingService(db=db_session, gateway=gw, now_provider=clock.now)
    sched_svc.process_manual_edit(
        cycle_id=cycle_id,
        patient_id=patient_id,
        mutations=[LabMarkerMutationItem(marker_key="alt", display_name="ALT", value=45.0, unit="U/L", sample_date="2026-11-06")],
    )
    clock.advance(minutes=16)

    auto_svc_w1 = ClinicalAutomationService(db=db_session, gateway=gw, now_provider=clock.now, enable_lab_revision=True)
    auto_svc_w2 = ClinicalAutomationService(db=db_session, gateway=gw, now_provider=clock.now, enable_lab_revision=True)

    # Discover jobs on w1
    discovered = auto_svc_w1.discover_and_enqueue_jobs()
    lab_job = next(j for j in discovered if j.job_type == AutomationJobType.LAB_REVISION)
    job_id = lab_job.job_id

    # W1 claims the job
    claimed_w1 = auto_svc_w1.claim_job(job_id)
    assert claimed_w1 is not None

    # W2 attempts to claim same job -> returns None
    claimed_w2 = auto_svc_w2.claim_job(job_id)
    assert claimed_w2 is None

    # W1 finishes execution
    res = await auto_svc_w1.execute_claimed_job(claimed_w1)
    assert res.status == AutomationJobStatus.COMPLETED


@pytest.mark.anyio
async def test_13_new_data_version_during_run_not_lost_n_plus_1(db_session: Session):
    """
    13. While version N is being revised, user uploads version N+1.
    Version N finishes and evaluates N; version N+1 remains pending for next revision.
    """
    t0 = datetime(2026, 11, 6, 8, 0, tzinfo=timezone.utc)
    clock = MockClock(t0)
    cycle_id = "cyc_6b_13"
    patient_id = "pat_6b_13"
    await _seed_active_cycle_with_baseline(db_session, patient_id, cycle_id, t0)

    calls: List[Dict[str, Any]] = []
    gw = _build_stage_6b_mock_gateway(calls)

    sched_svc = LabSchedulingService(db=db_session, gateway=gw, now_provider=clock.now)
    # 1. Edit creates version N (v2 in ledger)
    sched_svc.process_manual_edit(
        cycle_id=cycle_id,
        patient_id=patient_id,
        mutations=[LabMarkerMutationItem(marker_key="glucose", display_name="Glucose", value=5.8, unit="mmol/L", sample_date="2026-11-06")],
    )
    v_n = sched_svc.lab_revision_service.get_lab_ledger_state(cycle_id).ledger_version
    clock.advance(minutes=16)

    # 2. While worker is processing v_n, user inputs new analysis v_n+1
    # Run consilium specifically for target version v_n
    rev_record = await sched_svc.lab_revision_service.execute_lab_revision_consilium(
        cycle_id=cycle_id,
        target_ledger_version=v_n,
    )
    assert rev_record.evaluated_ledger_version == v_n

    # User uploads version N+1 before complete_revision_task is called
    sched_svc.process_manual_edit(
        cycle_id=cycle_id,
        patient_id=patient_id,
        mutations=[LabMarkerMutationItem(marker_key="ferritin", display_name="Ferritin", value=95.0, unit="mcg/L", sample_date="2026-11-06")],
    )
    v_n_plus_1 = sched_svc.lab_revision_service.get_lab_ledger_state(cycle_id).ledger_version
    assert v_n_plus_1 > v_n

    # Complete revision for v_n
    task = sched_svc.complete_revision_task(cycle_id=cycle_id, evaluated_ledger_version=v_n)

    # Task is NOT completed, but pending for version N+1!
    assert task is not None
    assert task.status == "pending"
    assert task.target_ledger_version == v_n_plus_1


@pytest.mark.anyio
async def test_14_permanently_failed_ocr_does_not_block_other_valid_labs(db_session: Session):
    """
    14. An error document does not indefinitely block revision of other valid ledger markers.
    """
    t0 = datetime(2026, 11, 6, 8, 0, tzinfo=timezone.utc)
    clock = MockClock(t0)
    cycle_id = "cyc_6b_14"
    patient_id = "pat_6b_14"
    await _seed_active_cycle_with_baseline(db_session, patient_id, cycle_id, t0)

    calls: List[Dict[str, Any]] = []
    gw = _build_stage_6b_mock_gateway(calls)

    sched_svc = LabSchedulingService(db=db_session, gateway=gw, now_provider=clock.now)

    # Save a document that fails OCR
    doc = sched_svc.save_uploaded_document(
        cycle_id, patient_id, "broken.jpg", b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x01\x00`\x00`\x00\x00broken"
    )
    doc.status = LabDocumentStatus.ERROR
    doc.error_message = "Gemini OCR failed permanently"
    sched_svc.update_document_record(doc)

    # User adds a valid manual edit
    sched_svc.process_manual_edit(
        cycle_id=cycle_id,
        patient_id=patient_id,
        mutations=[LabMarkerMutationItem(marker_key="alt", display_name="ALT", value=33.0, unit="U/L", sample_date="2026-11-06")],
    )
    clock.advance(minutes=16)

    # Readiness allows revision of the valid data!
    is_ready, reason = sched_svc.evaluate_task_readiness(cycle_id)
    assert is_ready, reason

    auto_svc = ClinicalAutomationService(db=db_session, gateway=gw, now_provider=clock.now, enable_lab_revision=True)
    processed = await auto_svc.process_due_jobs(discover=True)
    assert any(j.job_type == AutomationJobType.LAB_REVISION and j.status == AutomationJobStatus.COMPLETED for j in processed)


@pytest.mark.anyio
async def test_15_user_isolation_between_patients(db_session: Session):
    """
    15. Patient 1 and Patient 2 tasks and data are completely isolated.
    """
    t0 = datetime(2026, 11, 6, 8, 0, tzinfo=timezone.utc)
    clock = MockClock(t0)

    # Cycle 1
    await _seed_active_cycle_with_baseline(db_session, "pat_iso_1", "cyc_iso_1", t0)
    # Cycle 2
    await _seed_active_cycle_with_baseline(db_session, "pat_iso_2", "cyc_iso_2", t0)

    calls: List[Dict[str, Any]] = []
    gw = _build_stage_6b_mock_gateway(calls)

    sched_svc = LabSchedulingService(db=db_session, gateway=gw, now_provider=clock.now)
    # Patient 1 gets changes
    sched_svc.process_manual_edit(
        cycle_id="cyc_iso_1",
        patient_id="pat_iso_1",
        mutations=[LabMarkerMutationItem(marker_key="glucose", display_name="Glucose", value=5.9, unit="mmol/L", sample_date="2026-11-06")],
    )
    clock.advance(minutes=16)

    auto_svc = ClinicalAutomationService(db=db_session, gateway=gw, now_provider=clock.now, enable_lab_revision=True)
    processed = await auto_svc.process_due_jobs(discover=True)

    # Only Cycle 1 executed
    assert len(processed) == 1
    assert processed[0].cycle_id == "cyc_iso_1"
    assert processed[0].patient_id == "pat_iso_1"
    assert sched_svc.get_pending_task("cyc_iso_2") is None


@pytest.mark.anyio
async def test_16_all_reports_are_server_only(db_session: Session):
    """
    16. All 10 internal model reports, diff audit, and background_lab_revision_report are SERVER_ONLY.
    """
    t0 = datetime(2026, 11, 6, 8, 0, tzinfo=timezone.utc)
    clock = MockClock(t0)
    cycle_id = "cyc_6b_16"
    patient_id = "pat_6b_16"
    await _seed_active_cycle_with_baseline(db_session, patient_id, cycle_id, t0)

    calls: List[Dict[str, Any]] = []
    gw = _build_stage_6b_mock_gateway(calls)

    sched_svc = LabSchedulingService(db=db_session, gateway=gw, now_provider=clock.now)
    sched_svc.process_manual_edit(
        cycle_id=cycle_id,
        patient_id=patient_id,
        mutations=[LabMarkerMutationItem(marker_key="glucose", display_name="Glucose", value=6.0, unit="mmol/L", sample_date="2026-11-06")],
    )
    clock.advance(minutes=16)

    auto_svc = ClinicalAutomationService(db=db_session, gateway=gw, now_provider=clock.now, enable_lab_revision=True)
    await auto_svc.process_due_jobs(discover=True)

    # Check client-facing reports: NO new client-facing reports created by Lab Revision!
    report_repo = ModelReportRepository(db_session)
    client_reports = report_repo.list_client_facing(cycle_id)
    # The client-facing reports present are only from Primary Consilium
    for cr in client_reports:
        c = cr.content or {}
        assert "background_lab_revision" not in str(c).lower()

    # All Lab Revision reports must be server_only
    server_reports = report_repo.list_server_only(cycle_id, BACKGROUND_LAB_REVISION_REPORT_TYPE)
    assert len(server_reports) == 1
    assert server_reports[0].privacy_level == PrivacyLevel.SERVER_ONLY.value


@pytest.mark.anyio
async def test_17_active_pro_strategy_and_athena_plan_not_changed(db_session: Session):
    """
    17. Critical invariant: Lab Revision does NOT alter active Pro strategy or Athena's weekly plan.
    """
    t0 = datetime(2026, 11, 6, 8, 0, tzinfo=timezone.utc)
    clock = MockClock(t0)
    cycle_id = "cyc_6b_17"
    patient_id = "pat_6b_17"
    await _seed_active_cycle_with_baseline(db_session, patient_id, cycle_id, t0)

    report_repo = ModelReportRepository(db_session)
    strat_before = report_repo.list_server_only(cycle_id, ATHENA_ACTIVE_STRATEGY_REPORT_TYPE)
    strat_ver_before = strat_before[0].version if strat_before else 0

    calls: List[Dict[str, Any]] = []
    gw = _build_stage_6b_mock_gateway(calls)

    sched_svc = LabSchedulingService(db=db_session, gateway=gw, now_provider=clock.now)
    sched_svc.process_manual_edit(
        cycle_id=cycle_id,
        patient_id=patient_id,
        mutations=[LabMarkerMutationItem(marker_key="glucose", display_name="Glucose", value=6.3, unit="mmol/L", sample_date="2026-11-06")],
    )
    clock.advance(minutes=16)

    auto_svc = ClinicalAutomationService(db=db_session, gateway=gw, now_provider=clock.now, enable_lab_revision=True)
    await auto_svc.process_due_jobs(discover=True)

    strat_after = report_repo.list_server_only(cycle_id, ATHENA_ACTIVE_STRATEGY_REPORT_TYPE)
    strat_ver_after = strat_after[0].version if strat_after else 0

    # Strategy version is UNCHANGED!
    assert strat_ver_after == strat_ver_before


@pytest.mark.anyio
async def test_18_weekly_consilium_not_started_prematurely(db_session: Session):
    """
    18. Lab Revision does not trigger Weekly Consilium prematurely or alter the 168-hour anchor.
    """
    t0 = datetime(2026, 11, 6, 8, 0, tzinfo=timezone.utc)
    clock = MockClock(t0)
    cycle_id = "cyc_6b_18"
    patient_id = "pat_6b_18"
    await _seed_active_cycle_with_baseline(db_session, patient_id, cycle_id, t0)

    weekly_svc = WeeklyConsiliumService(db=db_session, now_provider=clock.now)
    anchor_before = weekly_svc.find_schedule_anchor_timestamp(cycle_id)

    calls: List[Dict[str, Any]] = []
    gw = _build_stage_6b_mock_gateway(calls)

    sched_svc = LabSchedulingService(db=db_session, gateway=gw, now_provider=clock.now)
    sched_svc.process_manual_edit(
        cycle_id=cycle_id,
        patient_id=patient_id,
        mutations=[LabMarkerMutationItem(marker_key="glucose", display_name="Glucose", value=6.4, unit="mmol/L", sample_date="2026-11-06")],
    )
    clock.advance(minutes=16)

    auto_svc = ClinicalAutomationService(
        db=db_session, gateway=gw, now_provider=clock.now, enable_lab_revision=True, enable_automatic_weekly=True
    )
    await auto_svc.process_due_jobs(discover=True)

    anchor_after = weekly_svc.find_schedule_anchor_timestamp(cycle_id)
    assert anchor_after == anchor_before

    # No Weekly jobs enqueued or executed before 168h
    report_repo = ModelReportRepository(db_session)
    auto_jobs = report_repo.list_server_only(cycle_id)
    weekly_jobs = [r for r in auto_jobs if "weekly_consilium" in str(r.report_type).lower()]
    assert len(weekly_jobs) == 0


@pytest.mark.anyio
async def test_19_full_cascade_e2e_with_mocked_models(db_session: Session):
    """
    19. Full End-to-End lifecycle test:
    Manual edit -> Debounce active -> Debounce elapsed -> Worker discovery -> 10 Models -> Completed -> 72h Cooldown.
    """
    t0 = datetime(2026, 11, 6, 8, 0, tzinfo=timezone.utc)
    clock = MockClock(t0)
    cycle_id = "cyc_6b_19"
    patient_id = "pat_6b_19"
    await _seed_active_cycle_with_baseline(db_session, patient_id, cycle_id, t0)

    calls: List[Dict[str, Any]] = []
    gw = _build_stage_6b_mock_gateway(calls)

    sched_svc = LabSchedulingService(db=db_session, gateway=gw, now_provider=clock.now)
    auto_svc = ClinicalAutomationService(db=db_session, gateway=gw, now_provider=clock.now, enable_lab_revision=True)

    # 1. Edit
    sched_svc.process_manual_edit(
        cycle_id=cycle_id,
        patient_id=patient_id,
        mutations=[LabMarkerMutationItem(marker_key="ferritin", display_name="Ferritin", value=120.0, unit="mcg/L", sample_date="2026-11-06")],
    )

    # 2. Debounce check
    clock.advance(minutes=5)
    status_5m = sched_svc.get_scheduling_status(cycle_id)
    assert not status_5m.is_ready_for_revision

    # 3. Advance past 15m
    clock.advance(minutes=11)
    status_16m = sched_svc.get_scheduling_status(cycle_id)
    assert status_16m.is_ready_for_revision

    # 4. Worker executes
    res = await auto_svc.process_due_jobs(discover=True)
    assert len(res) == 1
    assert res[0].status == AutomationJobStatus.COMPLETED

    # 5. Check completed status and cooldown
    status_post = sched_svc.get_scheduling_status(cycle_id)
    assert not status_post.is_ready_for_revision
    assert not status_post.has_pending_task
    assert sched_svc.get_last_successful_revision_timestamp(cycle_id) is not None


@pytest.mark.anyio
async def test_20_medical_continuity_previous_conclusions_passed_to_each_tier(db_session: Session):
    """
    20. Medical continuity verification:
    - 8 specialists receive their own previous conclusions (or 'N/A' if absent).
    - Deputy receives previous deputy and chief conclusions.
    - Chief receives previous chief conclusion and active strategy.
    """
    t0 = datetime(2026, 11, 6, 8, 0, tzinfo=timezone.utc)
    clock = MockClock(t0)
    cycle_id = "cyc_6b_20"
    patient_id = "pat_6b_20"
    await _seed_active_cycle_with_baseline(db_session, patient_id, cycle_id, t0)

    calls: List[Dict[str, Any]] = []
    gw = _build_stage_6b_mock_gateway(calls)

    sched_svc = LabSchedulingService(db=db_session, gateway=gw, now_provider=clock.now)
    sched_svc.process_manual_edit(
        cycle_id=cycle_id,
        patient_id=patient_id,
        mutations=[LabMarkerMutationItem(marker_key="glucose", display_name="Glucose", value=5.9, unit="mmol/L", sample_date="2026-11-06")],
    )
    clock.advance(minutes=16)

    auto_svc = ClinicalAutomationService(db=db_session, gateway=gw, now_provider=clock.now, enable_lab_revision=True)
    await auto_svc.process_due_jobs(discover=True)

    # 1. Specialists continuity
    for idx in range(8):
        prompt = calls[idx]["prompt"]
        assert "YOUR OWN PREVIOUS CONCLUSION" in prompt
        assert "RELEVANT PREVIOUS CONCLUSIONS & AUDIT OF OTHER MODELS" in prompt

    # 2. Deputy continuity
    deputy_prompt = calls[8]["prompt"]
    assert "PREVIOUS DEPUTY & CHIEF CONCLUSIONS" in deputy_prompt

    # 3. Chief continuity
    chief_prompt = calls[9]["prompt"]
    assert "PREVIOUS DEPUTY & CHIEF CONCLUSIONS" in chief_prompt
    assert "active_pro_strategy" in chief_prompt.lower()
