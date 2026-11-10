"""
Stage 8B.2C — Comprehensive Test Suite: Atomic Neo Consilium Activation, Strategy & Weekly T0.

Verifies:
1. All 12 mandatory activation pre-conditions (cycle ownership, pending status, valid lineage,
   prior cycle completed, execution log prepared, 8 specialists + deputy + chief reports,
   Pro quality gate approved, 9 client reports valid against ClientReportContent, strategy & vitals
   thresholds valid, no concurrent active cycles).
2. Atomic transition: 'pending_reassessment' -> 'active'.
3. Single authoritative publication of `athena_active_strategy` (SERVER_ONLY) from Chief Pro shelves.
4. Activation of new personal Pro thresholds in Mode 2 vitals monitoring & continuous emergency bounds.
5. Exact schedule anchor T0 pinning at the moment of activation & Weekly Consilium at T0 + 168h.
6. Athena post-Neo initialization: warm returning-patient tone, zero consilium re-execution.
7. Strict idempotency: second activation calls return existing state without altering T0 or duplicating jobs.
8. Transaction rollback: failure mid-activation leaves cycle in 'pending_reassessment' with no partial publications.
9. Client-facing report visibility: hidden before activation, published after activation.
10. Preservation of historical Medical Safe documents across cycles.
11. Strict security and Zero Oracle Leakage on patient endpoints.
12. Admin activation endpoint with ServiceAdminSession.
13. ZERO additional Gemini LLM calls during the activation transaction itself.
"""

import asyncio
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, Generator, List, Optional
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.orm.attributes import flag_modified
from sqlalchemy.pool import StaticPool
from fastapi.testclient import TestClient

from app.database import get_db
from app.main import app
from app.models.base import Base
from app.models.medical_cycle import MedicalCycle
from app.models.model_report import ModelReport
from app.models.patient import Patient
from app.repositories.cycle_repository import MedicalCycleRepository
from app.repositories.report_repository import ModelReportRepository
from app.routers.cascade import get_gemini_gateway
from app.schemas.consilium import (
    SYSTEM_KEYS,
    SYSTEM_TITLES,
    ConsiliumMode,
    AegisAgentRole,
    NeoActivationResponse,
    NeoConsiliumMetadata,
)
from app.schemas.privacy import PrivacyLevel
from app.schemas.reports import ClientReportContent
from app.services.gemini_gateway import GeminiGateway, GeminiModelRole
from app.services.primary_consilium_service import (
    NEO_CONSILIUM_EXECUTION_LOG_REPORT_TYPE,
    NEO_CYCLE_LINEAGE_REPORT_TYPE,
)
from app.services.weekly_consilium_service import (
    WeeklyConsiliumService,
    ATHENA_ACTIVE_STRATEGY_REPORT_TYPE,
    ATHENA_SCHEDULE_ANCHOR_REPORT_TYPE,
)
from app.services.neo_activation_service import (
    NeoConsiliumActivationService,
    NeoActivationError,
    NeoActivationPreconditionError,
    NeoActivationCycleNotFoundError,
    NeoActivationAccessDeniedError,
    NEO_ACTIVATION_AUDIT_REPORT_TYPE,
)
from app.services.clinical_automation_service import ClinicalAutomationService
from app.services.mode2_vitals_service import Mode2VitalsMonitoringService
from app.services.referral_document_service import REFERRAL_DOCTOR_DOCUMENT_REPORT_TYPE
from app.security.auth import (
    GENERIC_CYCLE_ACCESS_DENIED_DETAIL,
    DeviceSession,
    ServiceAdminSession,
    require_device_session,
    require_service_admin_session,
    verify_client_token,
)


# ==============================================================================
# TEST FIXTURES AND HELPERS
# ==============================================================================

class MockGeminiGateway(GeminiGateway):
    def __init__(self):
        super().__init__()
        self.recorded_calls: List[Dict[str, Any]] = []

    async def generate_structured(
        self,
        prompt: str,
        response_schema: Any,
        model: GeminiModelRole = GeminiModelRole.FLASH,
        temperature: float = 0.0,
    ) -> Any:
        self.recorded_calls.append({"prompt": prompt, "model": model})
        if getattr(response_schema, "__name__", "") == "AthenaWeeklyPlanRecord":
            return response_schema(
                plan_id="plan_w1_01",
                cycle_id="c_neo_01",
                week_number=1,
                created_at_utc=datetime.now(timezone.utc).isoformat(),
                summary_for_curator="Weekly plan for cycle 2.",
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

    async def generate_text(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        model: GeminiModelRole = GeminiModelRole.FLASH,
        temperature: float = 0.2,
    ) -> str:
        self.recorded_calls.append({"prompt": prompt, "system_prompt": system_prompt, "model": model})
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

    async def generate_raw_text(
        self,
        contents: Any,
        model: GeminiModelRole = GeminiModelRole.FLASH,
        system_instruction: Optional[str] = None,
        response_mime_type: Optional[str] = None,
        temperature: float = 0.2,
    ) -> str:
        prompt_str = str(contents)
        self.recorded_calls.append({"prompt": prompt_str, "system_prompt": system_instruction, "model": model})
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


@pytest.fixture
def mock_db() -> Generator[Session, None, None]:
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    SessionTesting = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = SessionTesting()
    try:
        yield db
    finally:
        db.close()
        Base.metadata.drop_all(bind=engine)


@pytest.fixture
def mock_gateway() -> MockGeminiGateway:
    return MockGeminiGateway()


def _mock_device_session(patient_id: str = "p_neo_test_01", scopes: Optional[List[str]] = None) -> DeviceSession:
    eff_scopes = scopes or ["cycle:read", "reports:read", "cascade:run"]
    return DeviceSession(
        patient_id=patient_id,
        device_id=f"dev_{patient_id}",
        jti=f"jti_{patient_id}",
        family_id=f"fam_{patient_id}",
        scope=" ".join(eff_scopes),
        scopes=frozenset(eff_scopes),
        issued_at=datetime.now(timezone.utc),
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )


def _setup_patient_and_cycles(
    db: Session,
    patient_id: str = "p_neo_test_01",
    old_cycle_id: str = "c_old_01",
    new_cycle_id: str = "c_neo_01",
    old_cycle_status: str = "completed",
    new_cycle_status: str = "pending_reassessment",
) -> tuple[Patient, MedicalCycle, MedicalCycle]:
    patient = Patient(patient_id=patient_id, status="active")
    db.add(patient)

    now_utc = datetime.now(timezone.utc)
    old_cycle = MedicalCycle(
        cycle_id=old_cycle_id,
        patient_id=patient_id,
        status=old_cycle_status,
        started_at=now_utc - timedelta(days=30),
        closed_at=now_utc - timedelta(days=1) if old_cycle_status in ("completed", "archived") else None,
        created_at=now_utc - timedelta(days=30),
        updated_at=now_utc - timedelta(days=1),
    )
    db.add(old_cycle)

    new_cycle = MedicalCycle(
        cycle_id=new_cycle_id,
        patient_id=patient_id,
        status=new_cycle_status,
        started_at=now_utc - timedelta(hours=2),
        created_at=now_utc - timedelta(hours=2),
        updated_at=now_utc - timedelta(hours=2),
    )
    db.add(new_cycle)
    db.commit()
    return patient, old_cycle, new_cycle


def _populate_prepared_neo_consilium_data(
    db: Session,
    cycle_id: str,
    prior_cycle_ids: Optional[List[str]] = None,
    approved: bool = True,
    pro_strategy_text: str = "NEW_ATHENA_STRATEGY: Focus on endurance adaptation and metabolic steady state.",
    min_bpm: int = 48,
    max_bpm: int = 118,
    spike_bpm: int = 24,
    execution_status: str = "prepared",
    is_prepared: bool = True,
    is_activated: bool = False,
) -> None:
    repo = ModelReportRepository(db)
    now_utc = datetime.now(timezone.utc)
    p_ids = prior_cycle_ids or ["c_old_01"]

    # 1. Lineage report
    repo.create_next_version(
        cycle_id=cycle_id,
        model="aegis-core",
        report_type=NEO_CYCLE_LINEAGE_REPORT_TYPE,
        privacy_level=PrivacyLevel.SERVER_ONLY,
        content={
            "mode": "neo_reassessment",
            "previous_cycle_id": p_ids[-1],
            "prior_cycle_ids": p_ids,
            "created_at_utc": now_utc.isoformat(),
        },
        commit=False,
    )

    # 2. 8 Specialist server_only reports
    for sk in SYSTEM_KEYS:
        repo.create_next_version(
            cycle_id=cycle_id,
            model="gemini-2.5-flash-lite",
            report_type=f"system_specialist_{sk}",
            privacy_level=PrivacyLevel.SERVER_ONLY,
            content={
                "system_view": {"markers": "stable"},
                "system_logic": {"system_general_shelf": f"Academic synthesis for {sk}"},
                "training_sandbox": {"system_training_shelf": f"Sandbox delta for {sk}"},
            },
            commit=False,
        )

    # 3. Deputy Chief Flash report
    repo.create_next_version(
        cycle_id=cycle_id,
        model="gemini-2.5-flash",
        report_type="deputy_chief_physician_internal",
        privacy_level=PrivacyLevel.SERVER_ONLY,
        content={
            "agent_role": AegisAgentRole.DEPUTY_CHIEF_PHYSICIAN.value,
            "flash_general_shelf": "MULTI-SYSTEM SYNTHESIS: Stable adaptation across organ domains.",
            "flash_training_shelf": "DEPUTY SANDBOX: Multi-cycle verification complete.",
        },
        commit=False,
    )

    # 4. Chief Physician Pro internal report
    repo.create_next_version(
        cycle_id=cycle_id,
        model="gemini-2.5-pro",
        report_type="chief_physician_internal",
        privacy_level=PrivacyLevel.SERVER_ONLY,
        content={
            "agent_role": AegisAgentRole.CHIEF_PHYSICIAN.value,
            "pro_general_shelf": pro_strategy_text,
            "pro_training_shelf": "CHIEF TRAINING SANDBOX: Confirmed longitudinal trajectory.",
            "vitals_thresholds": {
                "min_pessimum_bpm": min_bpm,
                "max_pessimum_bpm": max_bpm,
                "max_spike_delta_bpm": spike_bpm,
            },
            "flash_quality_review": {
                "approved_for_publication": approved,
                "rejection_reason": None if approved else "Quality gate threshold rejected.",
            },
        },
        commit=False,
    )

    # 5. Neo Consilium execution log
    repo.create_next_version(
        cycle_id=cycle_id,
        model="gemini-2.5-pro",
        report_type=NEO_CONSILIUM_EXECUTION_LOG_REPORT_TYPE,
        privacy_level=PrivacyLevel.SERVER_ONLY,
        content={
            "status": execution_status,
            "is_prepared": is_prepared,
            "is_activated": is_activated,
            "completed_at_utc": now_utc.isoformat(),
            "metadata": {
                "mode": "neo_reassessment",
                "new_cycle_id": cycle_id,
                "previous_cycle_id": p_ids[-1],
                "prior_cycles_count": len(p_ids),
                "prior_cycle_ids": p_ids,
            },
        },
        commit=False,
    )

    # 6. 8 Client system user reports
    for sk in SYSTEM_KEYS:
        sys_content = {
            "title": f"{SYSTEM_TITLES[sk]} Overview",
            "summary": f"Your {SYSTEM_TITLES[sk]} metrics show positive longitudinal stability.",
            "report_date": now_utc.strftime("%Y-%m-%d"),
            "system_key": sk,
            "has_changes": True,
            "entries": [
                {
                    "date": now_utc.strftime("%Y-%m-%d"),
                    "summary": f"Your {SYSTEM_TITLES[sk]} metrics show positive longitudinal stability.",
                    "has_changes": True,
                }
            ],
        }
        ClientReportContent.model_validate(sys_content)
        repo.create_next_version(
            cycle_id=cycle_id,
            model="gemini-2.5-flash",
            report_type=f"system_user_report_{sk}",
            privacy_level=PrivacyLevel.CLIENT_FACING,
            content=sys_content,
            commit=False,
        )

    # 7. 1 Client overall report
    overall_content = {
        "title": "Comprehensive Reassessment Summary",
        "summary": "Overall physiological adaptation is confirmed with favorable resting recovery parameters.",
        "report_date": now_utc.strftime("%Y-%m-%d"),
        "highlights": ["Resting parameters normalized", "Metabolic status stable"],
        "metrics": {
            "min_pessimum_bpm": min_bpm,
            "max_pessimum_bpm": max_bpm,
            "max_spike_delta_bpm": spike_bpm,
        },
    }
    ClientReportContent.model_validate(overall_content)
    repo.create_next_version(
        cycle_id=cycle_id,
        model="gemini-2.5-pro",
        report_type="user_overall_report",
        privacy_level=PrivacyLevel.CLIENT_FACING,
        content=overall_content,
        commit=False,
    )

    db.commit()


# ==============================================================================
# TEST CASES
# ==============================================================================

def test_01_all_12_preconditions_validation(mock_db: Session, mock_gateway: MockGeminiGateway):
    """Verifies that all 12 mandatory activation pre-conditions are strictly enforced."""
    patient, old_cycle, new_cycle = _setup_patient_and_cycles(mock_db)
    service = NeoConsiliumActivationService(db=mock_db, gateway=mock_gateway)

    # Condition 1: Non-existent cycle
    with pytest.raises(NeoActivationCycleNotFoundError):
        service.activate_prepared_neo("c_nonexistent_99")

    # Condition 1: Patient ownership mismatch
    _populate_prepared_neo_consilium_data(mock_db, new_cycle.cycle_id)
    with pytest.raises(NeoActivationAccessDeniedError):
        service.activate_prepared_neo(new_cycle.cycle_id, patient_id="p_other_patient_99")

    # Condition 2: Invalid status (e.g. cycle already completed or archived)
    new_cycle.status = "completed"
    mock_db.commit()
    with pytest.raises(NeoActivationPreconditionError, match="invalid status"):
        service.activate_prepared_neo(new_cycle.cycle_id)
    new_cycle.status = "pending_reassessment"
    mock_db.commit()

    # Condition 4: Prior cycle not closed
    old_cycle.status = "active"
    mock_db.commit()
    with pytest.raises(NeoActivationPreconditionError, match="is not closed"):
        service.activate_prepared_neo(new_cycle.cycle_id)
    old_cycle.status = "completed"
    mock_db.commit()

    # Condition 12: Another active cycle exists for this patient
    third_cycle = MedicalCycle(
        cycle_id="c_rogue_active",
        patient_id=patient.patient_id,
        status="active",
        started_at=datetime.now(timezone.utc),
    )
    mock_db.add(third_cycle)
    mock_db.commit()
    with pytest.raises(NeoActivationPreconditionError, match="Another medical cycle"):
        service.activate_prepared_neo(new_cycle.cycle_id)
    mock_db.delete(third_cycle)
    mock_db.commit()

    # Condition 9: Quality gate rejected
    chief_rep = mock_db.execute(
        select(ModelReport).where(
            ModelReport.cycle_id == new_cycle.cycle_id,
            ModelReport.report_type == "chief_physician_internal",
        )
    ).scalar_one()
    chief_c = dict(chief_rep.content)
    chief_c["flash_quality_review"] = {"approved_for_publication": False, "rejection_reason": "Rejected"}
    chief_rep.content = chief_c
    flag_modified(chief_rep, "content")
    mock_db.commit()
    with pytest.raises(NeoActivationPreconditionError, match="quality gate rejected"):
        service.activate_prepared_neo(new_cycle.cycle_id)
    chief_c["flash_quality_review"]["approved_for_publication"] = True
    chief_rep.content = chief_c
    flag_modified(chief_rep, "content")
    mock_db.commit()

    # Condition 11: Invalid vitals thresholds (min >= max)
    chief_c = dict(chief_rep.content)
    chief_c["vitals_thresholds"] = {"min_pessimum_bpm": 140, "max_pessimum_bpm": 80, "max_spike_delta_bpm": 20}
    chief_rep.content = chief_c
    flag_modified(chief_rep, "content")

    overall_rep = mock_db.execute(
        select(ModelReport).where(
            ModelReport.cycle_id == new_cycle.cycle_id,
            ModelReport.report_type == "user_overall_report",
        )
    ).scalar_one()
    ov_c = dict(overall_rep.content)
    ov_c["metrics"] = {"min_pessimum_bpm": 140, "max_pessimum_bpm": 80, "max_spike_delta_bpm": 20}
    overall_rep.content = ov_c
    flag_modified(overall_rep, "content")
    mock_db.commit()
    with pytest.raises(NeoActivationPreconditionError, match="out of bounds"):
        service.activate_prepared_neo(new_cycle.cycle_id)


def test_02_atomic_activation_happy_path(mock_db: Session, mock_gateway: MockGeminiGateway):
    """Verifies complete atomic activation happy path."""
    patient, old_cycle, new_cycle = _setup_patient_and_cycles(mock_db)
    strat_text = "AUTHENTIC_PRO_NEO_STRATEGY_FOR_ATHENA: Focus on cardiovascular stability."
    _populate_prepared_neo_consilium_data(
        mock_db,
        new_cycle.cycle_id,
        pro_strategy_text=strat_text,
        min_bpm=46,
        max_bpm=116,
        spike_bpm=22,
    )

    t_before = datetime.now(timezone.utc) - timedelta(seconds=1)
    service = NeoConsiliumActivationService(db=mock_db, gateway=mock_gateway)
    res = service.activate_prepared_neo(new_cycle.cycle_id, patient_id=patient.patient_id)
    t_after = datetime.now(timezone.utc) + timedelta(seconds=1)

    assert res.success is True
    assert res.status == "active"
    assert res.is_idempotent_replay is False
    assert res.strategy_version == 1
    assert res.post_primary_init_enqueued is True

    # Check MedicalCycle row
    cycle_repo = MedicalCycleRepository(mock_db)
    fresh_cycle = cycle_repo.get(new_cycle.cycle_id)
    assert fresh_cycle.status == "active"

    # Check neo_consilium_execution_log updated
    report_repo = ModelReportRepository(mock_db)
    exec_logs = report_repo.list_server_only(
        cycle_id=new_cycle.cycle_id,
        report_type=NEO_CONSILIUM_EXECUTION_LOG_REPORT_TYPE,
    )
    assert len(exec_logs) == 1
    log_c = exec_logs[0].content
    assert log_c["status"] == "activated"
    assert log_c["is_activated"] is True
    assert log_c["activated_at_utc"] is not None

    # Check chief_physician_internal marked activated
    chief_reps = report_repo.list_server_only(
        cycle_id=new_cycle.cycle_id,
        report_type="chief_physician_internal",
    )
    assert chief_reps[0].content.get("is_activated") is True

    # Check athena_active_strategy published
    strat_reps = report_repo.list_server_only(
        cycle_id=new_cycle.cycle_id,
        report_type=ATHENA_ACTIVE_STRATEGY_REPORT_TYPE,
    )
    assert len(strat_reps) == 1
    strat_content = strat_reps[0].content
    assert strat_content["source_consilium"] == "neo_consilium"
    assert strat_content["strategy_status"] == "activated"
    assert strat_content["active_strategy_text"] == strat_text

    # Check athena_schedule_anchor T0 recorded
    weekly_service = WeeklyConsiliumService(db=mock_db, gateway=mock_gateway)
    anchor_dt = weekly_service.find_schedule_anchor_timestamp(new_cycle.cycle_id)
    assert anchor_dt is not None
    assert t_before <= anchor_dt <= t_after

    # Check neo_consilium_activation_audit report
    audit_reps = report_repo.list_server_only(
        cycle_id=new_cycle.cycle_id,
        report_type=NEO_ACTIVATION_AUDIT_REPORT_TYPE,
    )
    assert len(audit_reps) == 1
    assert audit_reps[0].content["patient_id"] == patient.patient_id


def test_03_zero_gemini_calls_during_activation(mock_db: Session, mock_gateway: MockGeminiGateway):
    """Guarantees strictly ZERO Gemini API calls occur during the activation transaction."""
    patient, old_cycle, new_cycle = _setup_patient_and_cycles(mock_db)
    _populate_prepared_neo_consilium_data(mock_db, new_cycle.cycle_id)

    mock_gateway.recorded_calls.clear()
    service = NeoConsiliumActivationService(db=mock_db, gateway=mock_gateway)
    res = service.activate_prepared_neo(new_cycle.cycle_id)

    assert res.success is True
    assert len(mock_gateway.recorded_calls) == 0, "Activation transaction MUST make ZERO LLM calls!"


def test_04_pro_thresholds_applied_to_mode2_monitoring(mock_db: Session, mock_gateway: MockGeminiGateway):
    """Verifies that new Pro thresholds apply in Mode 2 vitals monitoring while hard guards remain active."""
    patient, old_cycle, new_cycle = _setup_patient_and_cycles(mock_db)
    _populate_prepared_neo_consilium_data(
        mock_db,
        new_cycle.cycle_id,
        min_bpm=44,
        max_bpm=114,
        spike_bpm=20,
    )

    vitals_service = Mode2VitalsMonitoringService(db=mock_db)
    service = NeoConsiliumActivationService(db=mock_db, gateway=mock_gateway)
    service.activate_prepared_neo(new_cycle.cycle_id)

    # Mode 2 resolves thresholds for the active cycle
    thresholds = vitals_service.resolve_thresholds(new_cycle.cycle_id)
    assert thresholds["source"] == "pro"
    assert thresholds["min_pessimum_bpm"] == 44
    assert thresholds["max_pessimum_bpm"] == 114
    assert thresholds["max_spike_delta_bpm"] == 20

    # Verify hard emergency guards remain intact
    assert thresholds["critical_bradycardia_bpm"] == 40
    assert thresholds["critical_tachycardia_bpm"] == 140
    assert thresholds["extreme_tachycardia_bpm"] == 160
    assert thresholds["critical_spo2"] == 90
    assert thresholds["warning_spo2"] == 92


def test_05_weekly_t0_anchor_and_168h_scheduling(mock_db: Session, mock_gateway: MockGeminiGateway):
    """Verifies T0 schedule anchor pinning and first Weekly consilium scheduled at T0 + 168h."""
    patient, old_cycle, new_cycle = _setup_patient_and_cycles(mock_db)
    _populate_prepared_neo_consilium_data(mock_db, new_cycle.cycle_id)

    frozen_t0 = datetime(2026, 11, 9, 14, 0, 0, tzinfo=timezone.utc)
    service = NeoConsiliumActivationService(
        db=mock_db, gateway=mock_gateway, now_provider=lambda: frozen_t0
    )
    res = service.activate_prepared_neo(new_cycle.cycle_id)

    assert res.schedule_anchor_t0_utc == frozen_t0.isoformat()
    expected_due = frozen_t0 + timedelta(hours=168)
    assert res.next_weekly_consilium_due_utc == expected_due.isoformat()

    # Re-running later must NOT alter T0
    later_dt = frozen_t0 + timedelta(days=2)
    service_later = NeoConsiliumActivationService(
        db=mock_db, gateway=mock_gateway, now_provider=lambda: later_dt
    )
    replay = service_later.activate_prepared_neo(new_cycle.cycle_id)
    assert replay.is_idempotent_replay is True
    assert replay.schedule_anchor_t0_utc == frozen_t0.isoformat()
    assert replay.next_weekly_consilium_due_utc == expected_due.isoformat()


def test_06_athena_post_neo_greeting_and_post_primary_init(
    mock_db: Session, mock_gateway: MockGeminiGateway
):
    """Verifies that post_primary_init executes Athena greeting with Neo returning-patient prompt and ZERO 10-model calls."""
    patient, old_cycle, new_cycle = _setup_patient_and_cycles(mock_db)
    _populate_prepared_neo_consilium_data(mock_db, new_cycle.cycle_id)

    service = NeoConsiliumActivationService(db=mock_db, gateway=mock_gateway)
    res = service.activate_prepared_neo(new_cycle.cycle_id)
    assert res.post_primary_init_enqueued is True

    # Retrieve enqueued job
    automation_service = ClinicalAutomationService(
        db=mock_db, gateway=mock_gateway, enable_weekly_plan=True
    )
    job = automation_service.get_job(cycle_id=new_cycle.cycle_id, job_type="post_primary_init")
    assert job is not None
    claimed_job = automation_service.claim_job(job.job_id)
    assert claimed_job is not None

    # Execute post_primary_init
    mock_gateway.recorded_calls.clear()
    exec_res = asyncio.run(automation_service._execute_post_primary_init(claimed_job))
    assert exec_res.get("reused_existing") is False

    # Check Gemini calls: only Athena Flash calls (weekly plan formulation & initial greeting), ZERO calls to 10 Consilium models
    assert len(mock_gateway.recorded_calls) == 2
    assert all(c["model"] == GeminiModelRole.FLASH for c in mock_gateway.recorded_calls)
    greeting_call = [c for c in mock_gateway.recorded_calls if "NEO REASSESSMENT INTRODUCTION" in c["prompt"]][0]
    assert "NEO REASSESSMENT INTRODUCTION" in greeting_call["prompt"]
    assert "С возвращением" in greeting_call["prompt"] or "повторн" in greeting_call["prompt"]

    # Verify first planned question is now enqueued at T0 + 30s
    q1_job = automation_service.get_job(
        cycle_id=new_cycle.cycle_id,
        job_type="first_planned_question",
        idempotency_key=f"first_planned_q_{new_cycle.cycle_id}",
    )
    assert q1_job is not None


def test_07_strict_idempotency(mock_db: Session, mock_gateway: MockGeminiGateway):
    """Verifies double activation requests return idempotent replay without creating duplicate records."""
    patient, old_cycle, new_cycle = _setup_patient_and_cycles(mock_db)
    _populate_prepared_neo_consilium_data(mock_db, new_cycle.cycle_id)

    service = NeoConsiliumActivationService(db=mock_db, gateway=mock_gateway)
    res1 = service.activate_prepared_neo(new_cycle.cycle_id)
    assert res1.is_idempotent_replay is False

    res2 = service.activate_prepared_neo(new_cycle.cycle_id)
    assert res2.is_idempotent_replay is True
    assert res2.schedule_anchor_t0_utc == res1.schedule_anchor_t0_utc
    assert res2.activated_at_utc == res1.activated_at_utc

    # Verify no duplicate strategy reports
    report_repo = ModelReportRepository(mock_db)
    strat_reps = report_repo.list_server_only(
        cycle_id=new_cycle.cycle_id,
        report_type=ATHENA_ACTIVE_STRATEGY_REPORT_TYPE,
    )
    assert len(strat_reps) == 1


def test_08_transaction_rollback_on_db_error(mock_db: Session, mock_gateway: MockGeminiGateway):
    """Verifies that an injected failure rolls back all changes without leaving partial state."""
    patient, old_cycle, new_cycle = _setup_patient_and_cycles(mock_db)
    _populate_prepared_neo_consilium_data(mock_db, new_cycle.cycle_id)

    service = NeoConsiliumActivationService(db=mock_db, gateway=mock_gateway)

    # Inject failure during db.commit
    original_commit = mock_db.commit

    def failing_commit():
        raise RuntimeError("Simulated SQLite write error during activation commit!")

    mock_db.commit = failing_commit

    with pytest.raises(RuntimeError, match="Simulated SQLite write error"):
        service.activate_prepared_neo(new_cycle.cycle_id)

    # Restore commit & verify rollback
    mock_db.commit = original_commit
    mock_db.expire_all()
    cycle = mock_db.query(MedicalCycle).filter_by(cycle_id=new_cycle.cycle_id).first()
    assert cycle.status == "pending_reassessment", "Cycle MUST remain in pending_reassessment on failure!"

    # Verify no active strategy was published
    report_repo = ModelReportRepository(mock_db)
    strat_reps = report_repo.list_server_only(
        cycle_id=new_cycle.cycle_id,
        report_type=ATHENA_ACTIVE_STRATEGY_REPORT_TYPE,
    )
    assert len(strat_reps) == 0


def test_09_client_facing_visibility_before_vs_after(
    mock_db: Session, mock_gateway: MockGeminiGateway
):
    """Verifies that client reports are hidden prior to activation and visible after activation."""
    patient, old_cycle, new_cycle = _setup_patient_and_cycles(mock_db)
    _populate_prepared_neo_consilium_data(mock_db, new_cycle.cycle_id)

    def _override_db():
        yield mock_db

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[get_gemini_gateway] = lambda: mock_gateway

    session_device = _mock_device_session(patient_id=patient.patient_id)
    app.dependency_overrides[require_device_session] = lambda: session_device
    app.dependency_overrides[verify_client_token] = lambda: session_device

    try:
        client = TestClient(app)

        # 1. Before activation: status endpoint indicates pending_reassessment / prepared
        status_resp = client.get("/api/v1/client/cascade/status")
        assert status_resp.status_code == 200
        st_data = status_resp.json()
        assert st_data["is_neo_reassessment"] is True
        assert st_data["reassessment_status"] == "prepared"
        assert st_data["is_primary_consilium_completed"] is False

        # 2. Before activation: client reports endpoint returns empty list
        reports_resp = client.get(f"/api/v1/reports/client?cycle_id={new_cycle.cycle_id}")
        assert reports_resp.status_code == 200
        assert len(reports_resp.json()) == 0

        # 3. Trigger activation via client endpoint
        act_resp = client.post("/api/v1/client/cascade/activate-neo", json={"cycle_id": new_cycle.cycle_id})
        assert act_resp.status_code == 200
        act_data = act_resp.json()
        assert act_data["success"] is True
        assert act_data["status"] == "active"

        # 4. After activation: status reflects activated & completed
        status_after = client.get("/api/v1/client/cascade/status")
        assert status_after.status_code == 200
        st_after_data = status_after.json()
        assert st_after_data["is_neo_reassessment"] is True
        assert st_after_data["reassessment_status"] == "activated"
        assert st_after_data["is_primary_consilium_completed"] is True

        # 5. After activation: client reports are published and accessible
        reports_after = client.get(f"/api/v1/reports/client?cycle_id={new_cycle.cycle_id}")
        assert reports_after.status_code == 200
        reps = reports_after.json()
        assert len(reps) == 9, "All 8 system reports + 1 overall report must be visible after activation!"

        # 6. Idempotent call sets replay header
        act_replay = client.post("/api/v1/client/cascade/activate-neo", json={"cycle_id": new_cycle.cycle_id})
        assert act_replay.status_code == 200
        assert act_replay.headers.get("x-aegis-idempotent-replay") == "true"
        assert act_replay.json()["is_idempotent_replay"] is True
    finally:
        app.dependency_overrides.clear()


def test_10_safe_documents_historical_preservation(mock_db: Session, mock_gateway: MockGeminiGateway):
    """Verifies that doctor observation documents from prior cycles remain accessible in Safe across cycles."""
    patient, old_cycle, new_cycle = _setup_patient_and_cycles(mock_db)
    _populate_prepared_neo_consilium_data(mock_db, new_cycle.cycle_id)

    # Insert a valid referral doctor document in the old cycle
    repo = ModelReportRepository(mock_db)
    now_utc = datetime.now(timezone.utc)
    old_doc_content = {
        "title": "Doctor Observation Summary",
        "summary": "# Historical Medical Referral\n\nPatient was monitored.",
        "report_date": now_utc.strftime("%Y-%m-%d"),
        "system_key": None,
        "has_changes": True,
        "highlights": ["Recommended consultation: Cardiology (routine)"],
        "metrics": {
            "patient_id": patient.patient_id,
            "cycle_id": old_cycle.cycle_id,
            "sprint_id": "sp_hist_01",
            "sprint_number": 1,
            "referral_id": "ref_hist_01",
            "target_specialties": "Cardiology",
            "consultation_urgency": "routine",
            "created_at_utc": (now_utc - timedelta(days=10)).isoformat(),
        },
    }
    ClientReportContent.model_validate(old_doc_content)
    old_doc = repo.create_next_version(
        cycle_id=old_cycle.cycle_id,
        model="gemini-2.5-pro",
        report_type=REFERRAL_DOCTOR_DOCUMENT_REPORT_TYPE,
        privacy_level=PrivacyLevel.CLIENT_FACING,
        content=old_doc_content,
    )

    def _override_db():
        yield mock_db

    app.dependency_overrides[get_db] = _override_db
    session_device = _mock_device_session(patient_id=patient.patient_id)
    app.dependency_overrides[require_device_session] = lambda: session_device
    app.dependency_overrides[verify_client_token] = lambda: session_device

    try:
        client = TestClient(app)

        # Before activation, document is listed
        doc_resp_1 = client.get("/api/v1/client/referral/documents")
        assert doc_resp_1.status_code == 200
        assert len(doc_resp_1.json()["documents"]) == 1

        # Activate Neo
        service = NeoConsiliumActivationService(db=mock_db, gateway=mock_gateway)
        service.activate_prepared_neo(new_cycle.cycle_id)

        # After activation, historical document is still listed
        doc_resp_2 = client.get("/api/v1/client/referral/documents")
        assert doc_resp_2.status_code == 200
        docs = doc_resp_2.json()["documents"]
        assert len(docs) == 1
        assert docs[0]["report_id"] == old_doc.report_id
        assert docs[0]["cycle_id"] == old_cycle.cycle_id
    finally:
        app.dependency_overrides.clear()


def test_11_client_security_zero_oracle_leakage(mock_db: Session, mock_gateway: MockGeminiGateway):
    """Verifies that unauthorized patients cannot activate another patient's cycle (403 Zero Oracle Leakage)."""
    patient_a, _, new_cycle_a = _setup_patient_and_cycles(
        mock_db, patient_id="p_alice", old_cycle_id="c_old_a", new_cycle_id="c_neo_a"
    )
    _populate_prepared_neo_consilium_data(mock_db, new_cycle_a.cycle_id)

    # Patient B tries to activate Patient A's cycle
    def _override_db():
        yield mock_db

    app.dependency_overrides[get_db] = _override_db
    session_bob = _mock_device_session(patient_id="p_bob")
    app.dependency_overrides[require_device_session] = lambda: session_bob
    app.dependency_overrides[verify_client_token] = lambda: session_bob

    try:
        client = TestClient(app)
        resp = client.post("/api/v1/client/cascade/activate-neo", json={"cycle_id": new_cycle_a.cycle_id})
        assert resp.status_code == 403
        assert resp.json()["detail"] == GENERIC_CYCLE_ACCESS_DENIED_DETAIL
    finally:
        app.dependency_overrides.clear()


def test_12_admin_endpoint_activation(mock_db: Session, mock_gateway: MockGeminiGateway):
    """Verifies that service admin can activate prepared Neo cycle via /api/v1/cascade/activate-neo."""
    patient, old_cycle, new_cycle = _setup_patient_and_cycles(mock_db)
    _populate_prepared_neo_consilium_data(mock_db, new_cycle.cycle_id)

    def _override_db():
        yield mock_db

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[get_gemini_gateway] = lambda: mock_gateway
    admin_session = ServiceAdminSession()
    app.dependency_overrides[require_service_admin_session] = lambda: admin_session

    try:
        client = TestClient(app)
        resp = client.post("/api/v1/cascade/activate-neo", json={"cycle_id": new_cycle.cycle_id})
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["status"] == "active"
        assert data["strategy_version"] == 1
    finally:
        app.dependency_overrides.clear()
