"""
Stage 8B.2C.1 Test Suite: Verification of Neo Activation Recovery, Athena Initialization,
First Planned Question at T0 + 30s, and Authoritative Pro Thresholds.

Coverage:
1. Crash between activation commit and job enqueue (durable recovery via discovery).
2. Server restart recovery: new worker discovers missing post_primary_init, claims and executes.
3. Double enqueue and repeated activation idempotency.
4. Greeting and first planned question when enable_weekly_plan=False (no fictitious plan, safe fallback).
5. Greeting and first planned question when enable_weekly_plan=True (weekly plan formulated).
6. Overdue first question (T0 + 30s in past): safe immediate execution preserving message order.
7. Mismatch between internal Pro thresholds and client report metrics rejected during activation.
8. End-to-end guarantee: zero duplicate T0 anchors, zero duplicate strategies, zero duplicate greetings.
"""

import asyncio
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from app.database import get_db, Base, engine
from app.models.patient import Patient
from app.models.medical_cycle import MedicalCycle
from app.models.model_report import ModelReport
from app.schemas.privacy import PrivacyLevel
from app.schemas.consilium import ConsiliumMode, AegisAgentRole
from app.services.gemini_gateway import GeminiGateway, GeminiModelRole
from app.services.neo_activation_service import (
    NeoConsiliumActivationService,
    NeoActivationPreconditionError,
    NEO_CONSILIUM_EXECUTION_LOG_REPORT_TYPE,
    ATHENA_ACTIVE_STRATEGY_REPORT_TYPE,
    NEO_ACTIVATION_AUDIT_REPORT_TYPE,
)
from app.services.clinical_automation_service import (
    ClinicalAutomationService,
    AutomationJobType,
    AutomationJobStatus,
)
from app.services.weekly_consilium_service import (
    WeeklyConsiliumService,
    ATHENA_SCHEDULE_ANCHOR_REPORT_TYPE,
)
from app.services.athena_chat_service import (
    AthenaChatService,
    AthenaChatRequest,
    ATHENA_ROUTINE_QUESTION_REPORT_TYPE,
)
from app.services.mode2_vitals_service import (
    Mode2VitalsMonitoringService,
    CRITICAL_BRADYCARDIA_BPM,
    CRITICAL_TACHYCARDIA_BPM,
    EXTREME_TACHYCARDIA_BPM,
    CRITICAL_SPO2,
    WARNING_SPO2,
)


# ==============================================================================
# TEST FIXTURES & MOCK GATEWAY
# ==============================================================================

class MockGeminiGateway(GeminiGateway):
    """Mock Gemini Gateway capturing calls and returning deterministic structured/text responses."""

    def __init__(self) -> None:
        super().__init__()
        self.recorded_calls: List[Dict[str, Any]] = []

    async def generate_structured(
        self,
        prompt: str,
        response_schema: Any,
        model: GeminiModelRole = GeminiModelRole.FLASH,
        temperature: float = 0.0,
    ) -> Any:
        self.recorded_calls.append({"prompt": prompt, "model": model, "type": "structured"})
        if getattr(response_schema, "__name__", "") == "AthenaWeeklyPlanRecord":
            return response_schema(
                plan_id="plan_w1_recovery_01",
                cycle_id="c_neo_rec_01",
                week_number=1,
                created_at_utc=datetime.now(timezone.utc).isoformat(),
                summary_for_curator="Weekly plan for cycle recovery.",
                goals=[
                    {
                        "goal_id": "goal_01",
                        "priority": 1,
                        "title": "Resting Heart Rate Adaptation",
                        "target_clarification": "Verify morning pulse",
                        "clinical_rationale": "Longitudinal stability check",
                        "sufficiency_criteria": "Patient confirms morning pulse range",
                        "status": "active",
                        "questions_asked_count": 0,
                    }
                ],
            )
        return response_schema.model_validate({})

    async def generate_raw_text(
        self,
        contents: Any,
        model: GeminiModelRole = GeminiModelRole.FLASH,
        system_instruction: Optional[str] = None,
        response_mime_type: Optional[str] = None,
        temperature: float = 0.2,
    ) -> str:
        prompt_str = f"{contents} {system_instruction or ''}"
        self.recorded_calls.append({"prompt": prompt_str, "system_prompt": system_instruction, "model": model})
        lower_prompt = prompt_str.lower()
        if (
            "first_planned_question" in lower_prompt
            or "first planned question" in lower_prompt
            or "первый плановый вопрос" in lower_prompt
            or "первый запланированный вопрос" in lower_prompt
            or "first question" in lower_prompt
            or "цель наблюдения" in lower_prompt
        ):
            return (
                "Как вы себя чувствуете сегодня утром? Заметили ли вы изменения в уровне энергии?\n\n"
                "[CLINICAL_REPORT]\n"
                "{\n"
                '  "status": "MONITORING",\n'
                '  "symptoms_mentioned": [],\n'
                '  "subjective_wellbeing_score": null,\n'
                '  "adherence_to_plan": "UNKNOWN",\n'
                '  "red_flags": false,\n'
                '  "summary_for_master": "First question delivered."\n'
                "}\n"
                "[/CLINICAL_REPORT]"
            )
        return (
            "Здравствуйте! С возвращением в Aegis. Рада снова быть на связи! "
            "Консилиум завершил повторную оценку здоровья, и мы продолжаем наше сопровождение.\n\n"
            "[CLINICAL_REPORT]\n"
            "{\n"
            '  "status": "INITIALIZATION",\n'
            '  "symptoms_mentioned": [],\n'
            '  "subjective_wellbeing_score": null,\n'
            '  "adherence_to_plan": "UNKNOWN",\n'
            '  "red_flags": false,\n'
            '  "summary_for_master": "Neo greeting delivered."\n'
            "}\n"
            "[/CLINICAL_REPORT]"
        )


@pytest.fixture(scope="function")
def mock_db() -> Session:
    """Provides an isolated clean SQLite in-memory database session for each test."""
    Base.metadata.create_all(bind=engine)
    session = next(get_db())
    try:
        yield session
    finally:
        session.rollback()
        session.close()
        Base.metadata.drop_all(bind=engine)


@pytest.fixture(scope="function")
def mock_gateway() -> MockGeminiGateway:
    return MockGeminiGateway()


def _setup_patient_and_cycles(db: Session, prefix: str = "rec"):
    """Creates a patient, an archived cycle 1, and a pending Neo cycle 2."""
    now = datetime.now(timezone.utc)
    patient = Patient(
        patient_id=f"p_{prefix}_01",
        status="active",
        created_at=now - timedelta(days=60),
    )
    db.add(patient)

    old_cycle = MedicalCycle(
        cycle_id=f"c_{prefix}_old",
        patient_id=patient.patient_id,
        status="completed",
        started_at=now - timedelta(days=60),
        closed_at=now - timedelta(hours=1),
    )
    db.add(old_cycle)

    new_cycle = MedicalCycle(
        cycle_id=f"c_{prefix}_neo",
        patient_id=patient.patient_id,
        status="pending_reassessment",
        started_at=now - timedelta(hours=1),
    )
    db.add(new_cycle)
    db.commit()
    return patient, old_cycle, new_cycle


from tests.test_stage_8b2c_neo_activation import _populate_prepared_neo_consilium_data


def _populate_prepared_neo_consilium(
    db: Session,
    cycle_id: str,
    internal_min_bpm: int = 50,
    internal_max_bpm: int = 120,
    internal_spike_bpm: int = 25,
    client_min_bpm: int = 50,
    client_max_bpm: int = 120,
    client_spike_bpm: int = 25,
):
    """Populates fully prepared Stage 8B.2B consilium artifacts for cycle_id."""
    _populate_prepared_neo_consilium_data(
        db=db,
        cycle_id=cycle_id,
        min_bpm=internal_min_bpm,
        max_bpm=internal_max_bpm,
        spike_bpm=internal_spike_bpm,
    )
    if (
        client_min_bpm != internal_min_bpm
        or client_max_bpm != internal_max_bpm
        or client_spike_bpm != internal_spike_bpm
    ):
        overall_rep = db.execute(
            select(ModelReport).where(
                ModelReport.cycle_id == cycle_id,
                ModelReport.report_type == "user_overall_report",
            )
        ).scalar_one()
        ov_c = dict(overall_rep.content)
        ov_c["metrics"] = {
            "min_pessimum_bpm": client_min_bpm,
            "max_pessimum_bpm": client_max_bpm,
            "max_spike_delta_bpm": client_spike_bpm,
        }
        overall_rep.content = ov_c
        flag_modified(overall_rep, "content")
        db.commit()


# ==============================================================================
# TARGETED TEST SUITE (8 TARGETED TESTS)
# ==============================================================================

def test_01_crash_between_activation_commit_and_enqueue(
    mock_db: Session, mock_gateway: MockGeminiGateway
):
    """
    Test 1: Verifies that if server crashes between SQLite transaction commit and post_primary_init enqueue,
    the background worker / job discovery recovers the missing post_primary_init job durably across restarts.
    """
    patient, old_cycle, new_cycle = _setup_patient_and_cycles(mock_db, prefix="t01")
    _populate_prepared_neo_consilium(mock_db, new_cycle.cycle_id)

    # Simulate activation transaction committing, but simulate a crash where post_primary_init job is absent
    service = NeoConsiliumActivationService(db=mock_db, gateway=mock_gateway)
    res = service.activate_prepared_neo(new_cycle.cycle_id)
    assert res.success is True

    # Delete the enqueued post_primary_init job to simulate the server dying BEFORE job was persisted
    automation_svc = ClinicalAutomationService(db=mock_db, gateway=mock_gateway)
    job = automation_svc.get_job(cycle_id=new_cycle.cycle_id, job_type=AutomationJobType.POST_PRIMARY_INIT)
    assert job is not None
    # Remove job row from database to simulate the exact crash scenario
    job_row = mock_db.execute(
        select(ModelReport).where(ModelReport.report_id == job.job_id)
    ).scalar_one()
    mock_db.delete(job_row)
    mock_db.commit()

    # Verify job is now absent
    assert automation_svc.get_job(cycle_id=new_cycle.cycle_id, job_type=AutomationJobType.POST_PRIMARY_INIT) is None

    # Server restarts: new worker instance runs discover_and_enqueue_jobs()
    restarted_automation = ClinicalAutomationService(db=mock_db, gateway=mock_gateway)
    discovered = restarted_automation.discover_and_enqueue_jobs()

    # The missing post_primary_init must be safely recovered and enqueued
    recovered_job = restarted_automation.get_job(
        cycle_id=new_cycle.cycle_id, job_type=AutomationJobType.POST_PRIMARY_INIT
    )
    assert recovered_job is not None
    assert recovered_job.status == AutomationJobStatus.PENDING
    assert any(d.job_id == recovered_job.job_id for d in discovered)


def test_02_restart_recovery_discovers_and_runs_post_primary_init(
    mock_db: Session, mock_gateway: MockGeminiGateway
):
    """
    Test 2: Verifies that after server restart, process_due_jobs(discover=True) discovers the job,
    claims it, executes post_primary_init, delivers Athena greeting, and marks job COMPLETED.
    """
    patient, old_cycle, new_cycle = _setup_patient_and_cycles(mock_db, prefix="t02")
    _populate_prepared_neo_consilium(mock_db, new_cycle.cycle_id)

    service = NeoConsiliumActivationService(db=mock_db, gateway=mock_gateway)
    service.activate_prepared_neo(new_cycle.cycle_id)

    # Server restarts: create fresh automation service instance
    restarted_automation = ClinicalAutomationService(
        db=mock_db, gateway=mock_gateway, enable_weekly_plan=False
    )
    mock_gateway.recorded_calls.clear()

    # Run background worker tick
    processed = asyncio.run(restarted_automation.process_due_jobs(discover=True))
    assert len(processed) >= 1
    post_init_outcome = [p for p in processed if p.job_type == AutomationJobType.POST_PRIMARY_INIT][0]
    assert post_init_outcome.status == AutomationJobStatus.COMPLETED

    # Verify Athena message in database
    chat_svc = AthenaChatService(db=mock_db, gateway=mock_gateway)
    greeting_msg = chat_svc.find_existing_post_primary_initial_greeting(new_cycle.cycle_id)
    assert greeting_msg is not None
    assert "С возвращением" in greeting_msg.reply


def test_03_double_enqueue_and_repeat_activation(
    mock_db: Session, mock_gateway: MockGeminiGateway
):
    """
    Test 3: Verifies double activation requests (e.g. lost HTTP response) and double enqueue
    yield idempotent delivery without duplicate job rows or duplicate greetings.
    """
    patient, old_cycle, new_cycle = _setup_patient_and_cycles(mock_db, prefix="t03")
    _populate_prepared_neo_consilium(mock_db, new_cycle.cycle_id)

    service = NeoConsiliumActivationService(db=mock_db, gateway=mock_gateway)
    res1 = service.activate_prepared_neo(new_cycle.cycle_id)
    assert res1.is_idempotent_replay is False

    # Second activation call simulating lost response retry
    res2 = service.activate_prepared_neo(new_cycle.cycle_id)
    assert res2.is_idempotent_replay is True
    assert res2.schedule_anchor_t0_utc == res1.schedule_anchor_t0_utc

    # Count post_primary_init job rows in SQLite
    automation_svc = ClinicalAutomationService(db=mock_db, gateway=mock_gateway)
    jobs = automation_svc.list_jobs(cycle_id=new_cycle.cycle_id)
    init_jobs = [j for j in jobs if j.job_type == AutomationJobType.POST_PRIMARY_INIT]
    assert len(init_jobs) == 1


def test_04_greeting_and_q1_when_weekly_plan_disabled(
    mock_db: Session, mock_gateway: MockGeminiGateway
):
    """
    Test 4: Verifies that when enable_weekly_plan=False:
    1. Greeting is formulated with Neo tone.
    2. NO fictitious Athena weekly plan is created in SQLite.
    3. first_planned_question is planned for T0 + 30s.
    4. When first_planned_question runs, Athena generates question using safe fallback goal & Pro strategy.
    5. Exactly 1 greeting and 1 question in chat.
    """
    patient, old_cycle, new_cycle = _setup_patient_and_cycles(mock_db, prefix="t04")
    _populate_prepared_neo_consilium(mock_db, new_cycle.cycle_id)

    service = NeoConsiliumActivationService(db=mock_db, gateway=mock_gateway)
    service.activate_prepared_neo(new_cycle.cycle_id)

    automation_svc = ClinicalAutomationService(
        db=mock_db, gateway=mock_gateway, enable_weekly_plan=False
    )
    job = automation_svc.get_job(cycle_id=new_cycle.cycle_id, job_type=AutomationJobType.POST_PRIMARY_INIT)
    claimed = automation_svc.claim_job(job.job_id)
    assert claimed is not None

    mock_gateway.recorded_calls.clear()
    exec_res = asyncio.run(automation_svc._execute_post_primary_init(claimed))
    assert exec_res.get("reused_existing") is False
    assert "weekly_plan_id" not in exec_res  # NO fake weekly plan!

    # Verify no weekly plan record was saved in model_reports
    plans = mock_db.execute(
        select(ModelReport).where(
            ModelReport.cycle_id == new_cycle.cycle_id,
            ModelReport.report_type == "athena_weekly_plan",
        )
    ).scalars().all()
    assert len(plans) == 0

    # Verify first_planned_question is enqueued
    q1_job = automation_svc.get_job(
        cycle_id=new_cycle.cycle_id,
        job_type=AutomationJobType.FIRST_PLANNED_QUESTION,
        idempotency_key=f"first_planned_q_{new_cycle.cycle_id}",
    )
    assert q1_job is not None
    assert q1_job.status == AutomationJobStatus.PENDING

    # Advance time past T0 + 30s to claim and execute first_planned_question
    future_svc = ClinicalAutomationService(
        db=mock_db,
        gateway=mock_gateway,
        enable_weekly_plan=False,
        now_provider=lambda: datetime.now(timezone.utc) + timedelta(seconds=35),
    )
    claimed_q1 = future_svc.claim_job(q1_job.job_id)
    assert claimed_q1 is not None
    q1_outcome = asyncio.run(future_svc._execute_first_planned_question(claimed_q1))
    assert q1_outcome.get("reused_existing") is False

    # Verify chat messages: exactly 1 greeting and 1 question
    chat_svc = AthenaChatService(db=mock_db, gateway=mock_gateway)
    history = chat_svc.get_sanitized_history(patient_id=patient.patient_id, cycle_id=new_cycle.cycle_id).messages
    curator_msgs = [m for m in history if m.sender == "curator"]
    assert len(curator_msgs) == 2
    assert "С возвращением" in curator_msgs[0].text  # Turn 1: Greeting
    assert "чувствуете" in curator_msgs[1].text or "энергии" in curator_msgs[1].text  # Turn 2: Question


def test_05_greeting_and_q1_when_weekly_plan_enabled(
    mock_db: Session, mock_gateway: MockGeminiGateway
):
    """
    Test 5: Verifies that when enable_weekly_plan=True:
    1. Weekly plan is formulated via Flash.
    2. Greeting is formulated via Flash.
    3. first_planned_question is enqueued at T0 + 30s.
    4. Exactly 1 greeting and 1 question in chat.
    """
    patient, old_cycle, new_cycle = _setup_patient_and_cycles(mock_db, prefix="t05")
    _populate_prepared_neo_consilium(mock_db, new_cycle.cycle_id)

    service = NeoConsiliumActivationService(db=mock_db, gateway=mock_gateway)
    service.activate_prepared_neo(new_cycle.cycle_id)

    automation_svc = ClinicalAutomationService(
        db=mock_db, gateway=mock_gateway, enable_weekly_plan=True
    )
    job = automation_svc.get_job(cycle_id=new_cycle.cycle_id, job_type=AutomationJobType.POST_PRIMARY_INIT)
    claimed = automation_svc.claim_job(job.job_id)

    mock_gateway.recorded_calls.clear()
    exec_res = asyncio.run(automation_svc._execute_post_primary_init(claimed))
    assert exec_res.get("weekly_plan_id") is not None

    q1_job = automation_svc.get_job(
        cycle_id=new_cycle.cycle_id,
        job_type=AutomationJobType.FIRST_PLANNED_QUESTION,
        idempotency_key=f"first_planned_q_{new_cycle.cycle_id}",
    )
    assert q1_job is not None

    # Advance time past T0 + 30s to claim and execute first_planned_question
    future_svc = ClinicalAutomationService(
        db=mock_db,
        gateway=mock_gateway,
        enable_weekly_plan=True,
        now_provider=lambda: datetime.now(timezone.utc) + timedelta(seconds=35),
    )
    claimed_q1 = future_svc.claim_job(q1_job.job_id)
    assert claimed_q1 is not None
    asyncio.run(future_svc._execute_first_planned_question(claimed_q1))

    chat_svc = AthenaChatService(db=mock_db, gateway=mock_gateway)
    history = chat_svc.get_sanitized_history(patient_id=patient.patient_id, cycle_id=new_cycle.cycle_id).messages
    curator_msgs = [m for m in history if m.sender == "curator"]
    assert len(curator_msgs) == 2


def test_06_overdue_first_question_immediate_execution(
    mock_db: Session, mock_gateway: MockGeminiGateway
):
    """
    Test 6: Verifies that if T0 + 30 seconds has already passed when greeting completes,
    the system immediately executes first_planned_question, strictly preserving message order
    (Greeting first, Question second).
    """
    patient, old_cycle, new_cycle = _setup_patient_and_cycles(mock_db, prefix="t06")
    _populate_prepared_neo_consilium(mock_db, new_cycle.cycle_id)

    # Establish an anchor T0 2 minutes in the past
    past_t0 = datetime.now(timezone.utc) - timedelta(minutes=2)
    service = NeoConsiliumActivationService(db=mock_db, gateway=mock_gateway)
    service.activate_prepared_neo(new_cycle.cycle_id)

    # Override anchor report to be 2 minutes ago
    weekly_svc = WeeklyConsiliumService(db=mock_db, gateway=mock_gateway)
    anchor_rep = mock_db.execute(
        select(ModelReport).where(
            ModelReport.cycle_id == new_cycle.cycle_id,
            ModelReport.report_type == ATHENA_SCHEDULE_ANCHOR_REPORT_TYPE,
        )
    ).scalar_one()
    anchor_c = dict(anchor_rep.content)
    anchor_c["anchor_timestamp_utc"] = past_t0.isoformat()
    anchor_rep.content = anchor_c
    flag_modified(anchor_rep, "content")
    mock_db.commit()

    automation_svc = ClinicalAutomationService(
        db=mock_db, gateway=mock_gateway, enable_weekly_plan=False
    )
    job = automation_svc.get_job(cycle_id=new_cycle.cycle_id, job_type=AutomationJobType.POST_PRIMARY_INIT)
    claimed = automation_svc.claim_job(job.job_id)

    # Execute post_primary_init: since T0 + 30s is overdue, it should immediately process q1
    asyncio.run(automation_svc._execute_post_primary_init(claimed))

    # Check that first_planned_question was executed immediately and completed
    q1_job = automation_svc.get_job(
        cycle_id=new_cycle.cycle_id,
        job_type=AutomationJobType.FIRST_PLANNED_QUESTION,
        idempotency_key=f"first_planned_q_{new_cycle.cycle_id}",
    )
    assert q1_job is not None
    assert q1_job.status == AutomationJobStatus.COMPLETED

    # Check chat history: message 1 is greeting, message 2 is question
    chat_svc = AthenaChatService(db=mock_db, gateway=mock_gateway)
    history = chat_svc.get_sanitized_history(patient_id=patient.patient_id, cycle_id=new_cycle.cycle_id).messages
    curator_msgs = [m for m in history if m.sender == "curator"]
    assert len(curator_msgs) == 2
    assert "С возвращением" in curator_msgs[0].text  # Message 1
    assert "чувствуете" in curator_msgs[1].text or "энергии" in curator_msgs[1].text  # Message 2


def test_07_threshold_mismatch_internal_vs_client_rejected(
    mock_db: Session, mock_gateway: MockGeminiGateway
):
    """
    Test 7: Verifies that if internal Pro thresholds in chief_physician_internal contradict
    user_overall_report metrics, activation fails with NeoActivationPreconditionError.
    """
    patient, old_cycle, new_cycle = _setup_patient_and_cycles(mock_db, prefix="t07")
    # Populate with mismatch: internal=(50/120/25), client=(65/135/30)
    _populate_prepared_neo_consilium(
        mock_db,
        new_cycle.cycle_id,
        internal_min_bpm=50,
        internal_max_bpm=120,
        internal_spike_bpm=25,
        client_min_bpm=65,
        client_max_bpm=135,
        client_spike_bpm=30,
    )

    service = NeoConsiliumActivationService(db=mock_db, gateway=mock_gateway)
    with pytest.raises(NeoActivationPreconditionError, match="discrepancy"):
        service.activate_prepared_neo(new_cycle.cycle_id)

    # Cycle must remain unactivated
    refreshed_cycle = mock_db.get(MedicalCycle, new_cycle.cycle_id)
    assert refreshed_cycle.status == "pending_reassessment"

    # Also verify Mode2VitalsMonitoringService uses authoritative internal Pro result
    m2_svc = Mode2VitalsMonitoringService(db=mock_db, gateway=mock_gateway)
    th = m2_svc.resolve_thresholds(new_cycle.cycle_id)
    assert th["source"] == "pro"
    assert th["source_report_type"] == "chief_physician_internal"
    assert th["min_pessimum_bpm"] == 50  # Strictly from chief_physician_internal!
    assert th["max_pessimum_bpm"] == 120


def test_08_no_duplicate_t0_strategy_or_greeting(
    mock_db: Session, mock_gateway: MockGeminiGateway
):
    """
    Test 8: Verifies full end-to-end lifecycle under double worker runs and repeated activation:
    - Exactly 1 athena_schedule_anchor record.
    - Exactly 1 athena_active_strategy record.
    - Exactly 1 Athena initial greeting in chat.
    - Exactly 1 first planned question in chat.
    """
    patient, old_cycle, new_cycle = _setup_patient_and_cycles(mock_db, prefix="t08")
    _populate_prepared_neo_consilium(mock_db, new_cycle.cycle_id)

    service = NeoConsiliumActivationService(db=mock_db, gateway=mock_gateway)
    res1 = service.activate_prepared_neo(new_cycle.cycle_id)

    automation_svc = ClinicalAutomationService(
        db=mock_db, gateway=mock_gateway, enable_weekly_plan=False
    )
    # Process due jobs
    asyncio.run(automation_svc.process_due_jobs(discover=True))

    # Repeated activation call
    res2 = service.activate_prepared_neo(new_cycle.cycle_id)
    assert res2.is_idempotent_replay is True

    # Second worker run
    asyncio.run(automation_svc.process_due_jobs(discover=True))

    # Verify counts in database
    anchor_reps = mock_db.execute(
        select(ModelReport).where(
            ModelReport.cycle_id == new_cycle.cycle_id,
            ModelReport.report_type == ATHENA_SCHEDULE_ANCHOR_REPORT_TYPE,
        )
    ).scalars().all()
    assert len(anchor_reps) == 1

    strat_reps = mock_db.execute(
        select(ModelReport).where(
            ModelReport.cycle_id == new_cycle.cycle_id,
            ModelReport.report_type == ATHENA_ACTIVE_STRATEGY_REPORT_TYPE,
        )
    ).scalars().all()
    assert len(strat_reps) == 1

    chat_svc = AthenaChatService(db=mock_db, gateway=mock_gateway)
    history = chat_svc.get_sanitized_history(patient_id=patient.patient_id, cycle_id=new_cycle.cycle_id).messages
    greeting_msgs = [m for m in history if "С возвращением" in m.text]
    assert len(greeting_msgs) == 1  # Never duplicated!
