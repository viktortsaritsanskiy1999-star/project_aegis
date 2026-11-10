import asyncio
from datetime import datetime, timezone
import json
from typing import Any, Dict, Generator, List, Optional
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool
from fastapi.testclient import TestClient

from app.config import settings
from app.database import get_db
from app.main import app
from app.models.base import Base
from app.models.medical_cycle import MedicalCycle
from app.models.model_report import ModelReport
from app.models.patient import Patient
from app.models.health_measurement import HealthMeasurement
from app.repositories.cycle_repository import MedicalCycleRepository
from app.repositories.measurement_repository import HealthMeasurementRepository
from app.repositories.report_repository import ModelReportRepository
from app.routers.cascade import get_gemini_gateway
from app.schemas.consilium import (
    SYSTEM_KEYS,
    SYSTEM_TITLES,
    ConsiliumMode,
    AegisAgentRole,
    AnamnesisModelOutput,
    SystemAgentOutput,
    FlashMasterOutput,
    ProMasterOutput,
    NeoSystemAgentOutput,
    NeoFlashMasterOutput,
    NeoProMasterOutput,
    NeoConsiliumMetadata,
    PrimaryConsiliumRunRequest,
    PrimaryConsiliumClientResponse,
    ClientVitalsThresholds,
)
from app.schemas.privacy import PrivacyLevel
from app.schemas.reports import ClientReportContent
from app.services.gemini_gateway import GeminiGateway, GeminiModelRole, GeminiGatewayError
from app.services.neo_cycle_service import NeoCycleService
from app.services.primary_consilium_service import (
    PrimaryConsiliumService,
    ConsiliumError,
    ConsiliumCycleNotFoundError,
    ConsiliumContextMissingError,
    ConsiliumQualityRejectionError,
    ConsiliumConcurrencyConflictError,
    PREPARATORY_ANAMNESIS_REPORT_TYPE,
    PRIMARY_CONSILIUM_EXECUTION_LOG_REPORT_TYPE,
    NEO_CONSILIUM_EXECUTION_LOG_REPORT_TYPE,
)
from app.services.weekly_consilium_service import WeeklyConsiliumService
from app.security.auth import DeviceSession, require_device_session, verify_client_token
from tests.test_primary_consilium import _sample_anamnesis_output


# ==============================================================================
# TEST FIXTURES AND MOCK DATA HELPERS
# ==============================================================================

def _sample_neo_specialist_output(system_key: str) -> Dict[str, Any]:
    return {
        "system_view": {
            "isolated_labs": {f"{system_key}_fresh_marker": "stable_normal"}
        },
        "system_logic": {
            "system_general_shelf": f"Academic pathophysiological re-evaluation for {system_key.upper()} with longitudinal dynamic comparison."
        },
        "training_sandbox": {
            "system_training_shelf": f"Deficit log and delta audit for {system_key.upper()} compared to previous cycle."
        },
    }


def _sample_neo_flash_output() -> Dict[str, Any]:
    user_reports = {
        key: (
            f"Neo reassessment summary for {SYSTEM_TITLES[key]}. Longitudinal dynamic analysis shows "
            f"stability relative to previous cycle observations; continuing routine monitoring."
        )
        for key in SYSTEM_KEYS
    }
    return {
        "flash_general_shelf": "INTERNAL DEPUTY FLASH NEO SYNTHESIS: multi-system dynamic audit shows improvement in sleep and stable cardiovascular function.",
        "flash_training_shelf": "INTERNAL DEPUTY FLASH TRAINING SANDBOX: nosology verification, deficit analysis across cycles.",
        "user_system_reports": user_reports,
    }


def _sample_neo_pro_output(
    approved: bool = True,
    rejection_reason: Optional[str] = None,
    corrected_reports: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    return {
        "vitals_thresholds": {
            "min_pessimum_bpm": 46,
            "max_pessimum_bpm": 116,
            "max_spike_delta_bpm": 26,
            "clinical_rationale": "NEO_CHIEF_PRO_RATIONALE: Rest recalculation based on improved resting heart rate dynamics.",
        },
        "pro_general_shelf": "SECRET_NEO_ATHENA_STRATEGY: Fresh clinical roadmap for cycle 2. Focus on endurance adaptation.",
        "pro_training_shelf": "SECRET_NEO_PRO_SANDBOX: Clean slate audit. Validated 8 organ specialists and Deputy Flash conclusions.",
        "user_overall_report": {
            "title": "Comprehensive Reassessment Summary",
            "summary": "Your overall health status shows positive adaptation compared to your prior assessment period. Aerobic markers and recovery metrics remain well-balanced.",
            "highlights": [
                "Cardiovascular resting parameters show calm adaptation",
                "Metabolic panel remains stable; schedule next routine follow-up as advised",
            ],
        },
        "flash_quality_review": {
            "approved_for_publication": approved,
            "rejection_reason": rejection_reason,
            "corrected_user_system_reports": corrected_reports or {},
        },
    }


class MockGeminiGateway(GeminiGateway):
    def __init__(
        self,
        recorded_calls: Optional[List[Dict[str, Any]]] = None,
        fail_system_key: Optional[str] = None,
        fail_deputy: bool = False,
        fail_chief: bool = False,
        pro_quality_approved: bool = True,
        pro_quality_reason: Optional[str] = None,
        pro_corrected_reports: Optional[Dict[str, str]] = None,
    ):
        super().__init__()
        self.recorded_calls = recorded_calls if recorded_calls is not None else []
        self.fail_system_key = fail_system_key
        self.fail_deputy = fail_deputy
        self.fail_chief = fail_chief
        self.pro_quality_approved = pro_quality_approved
        self.pro_quality_reason = pro_quality_reason
        self.pro_corrected_reports = pro_corrected_reports

    async def generate_structured(
        self,
        prompt: str,
        response_schema: Any,
        model: GeminiModelRole = GeminiModelRole.FLASH,
        temperature: float = 0.0,
    ) -> Any:
        call_info = {
            "model_role": model,
            "response_schema": response_schema.__name__ if hasattr(response_schema, "__name__") else str(response_schema),
            "prompt": prompt,
        }
        self.recorded_calls.append(call_info)

        # Injected failures
        spec_markers = {
            "cvs": "Cardiology",
            "gi_tract": "Gastroenterology",
            "respiratory": "Pulmonology",
            "nervous": "Neurology",
            "kidneys": "Nephrology",
            "endocrine": "Endocrinology",
            "immune": "Immunology",
            "musculoskeletal": "Orthopedics",
        }

        if self.fail_system_key:
            marker = spec_markers.get(self.fail_system_key, self.fail_system_key.upper())
            if marker in prompt or f"BLOCK: {self.fail_system_key.upper()}" in prompt:
                raise GeminiGatewayError(f"Simulated failure in organ specialist: {self.fail_system_key}")

        if self.fail_deputy and (response_schema in (FlashMasterOutput, NeoFlashMasterOutput)):
            raise GeminiGatewayError("Simulated failure in Deputy Chief Physician (Flash)")

        if self.fail_chief and (response_schema in (ProMasterOutput, NeoProMasterOutput)):
            raise GeminiGatewayError("Simulated failure in Chief Physician (Pro)")

        if response_schema in (SystemAgentOutput, NeoSystemAgentOutput):
            sys_key = "cvs"
            for k, marker in spec_markers.items():
                if marker in prompt or f"BLOCK: {k.upper()}" in prompt:
                    sys_key = k
                    break
            data = _sample_neo_specialist_output(sys_key)
            return response_schema.model_validate(data)

        if response_schema in (FlashMasterOutput, NeoFlashMasterOutput):
            data = _sample_neo_flash_output()
            return response_schema.model_validate(data)

        if response_schema in (ProMasterOutput, NeoProMasterOutput):
            data = _sample_neo_pro_output(
                approved=self.pro_quality_approved,
                rejection_reason=self.pro_quality_reason,
                corrected_reports=self.pro_corrected_reports,
            )
            return response_schema.model_validate(data)

        if response_schema == AnamnesisModelOutput:
            data = _sample_anamnesis_output()
            return response_schema.model_validate(data)

        raise ValueError(f"Unhandled response_schema: {response_schema}")


@pytest.fixture
def sqlite_db_session() -> Generator[Session, None, None]:
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    SessionMaker = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    session = SessionMaker()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _seed_neo_cycle_setup(
    db: Session,
    patient_id: str = "p_neo_orch_01",
    cycle_1_id: str = "cyc_01_old",
) -> tuple[str, str]:
    """
    Sets up a complete two-cycle environment:
    - Cycle 1: completed with baseline anamnesis, overall report, weekly report, and an active referral.
    - Transitions patient to Neo Reassessment via NeoCycleService.
    - Cycle 2: status 'pending_reassessment', has neo_cycle_lineage.
    - Seeds fresh preparatory anamnesis (M1) and vitals into Cycle 2.
    Returns (cycle_1_id, cycle_2_id).
    """
    now = datetime(2026, 10, 1, 10, 0, tzinfo=timezone.utc)
    patient = Patient(patient_id=patient_id, status="active")
    db.add(patient)

    c1 = MedicalCycle(
        cycle_id=cycle_1_id,
        patient_id=patient_id,
        status="active",
        started_at=now,
        created_at=now,
    )
    db.add(c1)
    db.commit()

    report_repo = ModelReportRepository(db)
    # Seed Cycle 1 Primary Consilium
    report_repo.create_next_version(
        cycle_id=cycle_1_id,
        model="gemini-3.7-pro",
        report_type="user_overall_report",
        privacy_level=PrivacyLevel.CLIENT_FACING,
        content={
            "title": "Initial Health Assessment Cycle 1",
            "summary": "Baseline across 8 organ systems.",
            "report_date": "2026-10-01",
            "metrics": {"min_pessimum_bpm": 48, "max_pessimum_bpm": 118},
        },
        created_at=now,
    )
    report_repo.create_next_version(
        cycle_id=cycle_1_id,
        model="gemini-3.7-pro",
        report_type="chief_physician_internal",
        privacy_level=PrivacyLevel.SERVER_ONLY,
        content={
            "agent_role": AegisAgentRole.CHIEF_PHYSICIAN.value,
            "pro_general_shelf": "Cycle 1 initial strategy",
            "pro_training_shelf": "Cycle 1 sandbox",
            "vitals_thresholds": {"min_pessimum_bpm": 48, "max_pessimum_bpm": 118, "max_spike_delta_bpm": 28},
            "flash_quality_review": {"approved_for_publication": True, "corrected_user_system_reports": {}},
        },
        created_at=now,
    )
    # Seed Cycle 1 Weekly Report
    report_repo.create_next_version(
        cycle_id=cycle_1_id,
        model="gemini-3.7-pro",
        report_type="weekly_user_overall_report",
        privacy_level=PrivacyLevel.CLIENT_FACING,
        content={
            "summary": "Sprint 4 completed: cardiovascular pacing steady.",
            "metrics": {"sprint_number": 4},
        },
        created_at=now,
    )
    # Seed Cycle 1 Referral Decision
    report_repo.create_next_version(
        cycle_id=cycle_1_id,
        model="gemini-2.5-flash",
        report_type="referral_lifecycle_state",
        privacy_level=PrivacyLevel.SERVER_ONLY,
        content={
            "has_active_referral": True,
            "lifecycle_status": "document_ready",
            "document_generation_state": "ready",
            "urgency": "routine",
            "target_specialties": ["Cardiology"],
            "clinical_reason_for_referral": "Historical cycle 1 arrhythmia check",
            "document_report_id": "rep_doc_cyc1_ref",
        },
        created_at=now,
    )
    db.commit()

    # Start Neo reassessment -> creates Cycle 2
    neo_service = NeoCycleService(db=db)
    resp = neo_service.start_neo_reassessment(patient_id=patient_id)
    cycle_2_id = resp.cycle_id

    # Seed fresh preparatory anamnesis for Cycle 2
    c2_now = datetime(2026, 11, 1, 9, 0, tzinfo=timezone.utc)
    raw_m1_data = _sample_anamnesis_output()
    raw_m1_data["agent_role"] = AegisAgentRole.ANAMNESIS_M1.value
    report_repo.create_next_version(
        cycle_id=cycle_2_id,
        model="gemini-2.5-flash",
        report_type=PREPARATORY_ANAMNESIS_REPORT_TYPE,
        privacy_level=PrivacyLevel.SERVER_ONLY,
        content=raw_m1_data,
        created_at=c2_now,
    )

    # Seed fresh vitals for Cycle 2
    meas_repo = HealthMeasurementRepository(db)
    meas_repo.create(
        cycle_id=cycle_2_id,
        source="smartwatch",
        systolic=122,
        diastolic=78,
        pulse=64,
        spo2=99,
        measured_at=c2_now,
    )
    db.commit()

    return cycle_1_id, cycle_2_id


def _make_device_session(patient_id: str, device_id: str = "dev_neo_01") -> DeviceSession:
    return DeviceSession(
        patient_id=patient_id,
        device_id=device_id,
        jti="jti_neo_test_01",
        family_id="fam_01",
        scopes=["cascade:run", "cycle:read", "cascade:intake", "vitals:write"],
        issued_at=datetime.now(timezone.utc),
        expires_at=datetime(2030, 1, 1, tzinfo=timezone.utc),
    )


# ==============================================================================
# TEST 1: SEQUENTIAL EXECUTION OF 10 MODELS (8 FLASH-LITE -> 1 FLASH -> 1 PRO)
# ==============================================================================

@pytest.mark.anyio
async def test_01_neo_consilium_sequence_ten_models(sqlite_db_session: Session):
    """Verifies that Neo Consilium executes exactly 10 model calls in 8 -> 1 -> 1 order."""
    _, cycle_2_id = _seed_neo_cycle_setup(sqlite_db_session)
    recorded_calls: List[Dict[str, Any]] = []
    gateway = MockGeminiGateway(recorded_calls=recorded_calls)
    service = PrimaryConsiliumService(db=sqlite_db_session, gateway=gateway)

    result = await service.run_primary_consilium(cycle_id=cycle_2_id)

    assert result.mode == ConsiliumMode.NEO_REASSESSMENT
    assert len(recorded_calls) == 10, "Neo Consilium must execute exactly 10 successful model calls"

    # First 8 calls must be Flash-Lite
    for i in range(8):
        assert recorded_calls[i]["model_role"] == GeminiModelRole.FLASH_LITE
        assert recorded_calls[i]["response_schema"] == "NeoSystemAgentOutput"

    # 9th call must be Deputy Flash
    assert recorded_calls[8]["model_role"] == GeminiModelRole.FLASH
    assert recorded_calls[8]["response_schema"] == "NeoFlashMasterOutput"

    # 10th call must be Chief Pro
    assert recorded_calls[9]["model_role"] == GeminiModelRole.PRO
    assert recorded_calls[9]["response_schema"] == "NeoProMasterOutput"


# ==============================================================================
# TEST 2: GEMINI MODEL ROLES MAPPING AND UNCHANGED MODEL IDS
# ==============================================================================

@pytest.mark.anyio
async def test_02_real_gemini_model_roles_and_ids(sqlite_db_session: Session):
    """Verifies GeminiModelRole mapping and guarantees model IDs and settings are untouched."""
    _, cycle_2_id = _seed_neo_cycle_setup(sqlite_db_session)
    recorded_calls: List[Dict[str, Any]] = []
    gateway = MockGeminiGateway(recorded_calls=recorded_calls)
    service = PrimaryConsiliumService(db=sqlite_db_session, gateway=gateway)

    await service.run_primary_consilium(cycle_id=cycle_2_id)

    # Invariants on Gateway model configurations
    assert "flash-lite" in gateway.flash_lite_model.lower()
    assert "flash" in gateway.flash_model.lower()
    assert "argon" in gateway.pro_model.lower() or "pro" in gateway.pro_model.lower()

    flash_lite_calls = [c for c in recorded_calls if c["model_role"] == GeminiModelRole.FLASH_LITE]
    flash_calls = [c for c in recorded_calls if c["model_role"] == GeminiModelRole.FLASH]
    pro_calls = [c for c in recorded_calls if c["model_role"] == GeminiModelRole.PRO]

    assert len(flash_lite_calls) == 8
    assert len(flash_calls) == 1
    assert len(pro_calls) == 1


# ==============================================================================
# TEST 3: EIGHT DISTINCT NEO SYSTEM PROMPTS
# ==============================================================================

@pytest.mark.anyio
async def test_03_eight_distinct_neo_system_prompts(sqlite_db_session: Session):
    """Verifies that each of the 8 specialists receives its specific system key and specialization instructions."""
    _, cycle_2_id = _seed_neo_cycle_setup(sqlite_db_session)
    recorded_calls: List[Dict[str, Any]] = []
    gateway = MockGeminiGateway(recorded_calls=recorded_calls)
    service = PrimaryConsiliumService(db=sqlite_db_session, gateway=gateway)

    await service.run_primary_consilium(cycle_id=cycle_2_id)

    specialist_prompts = [c["prompt"] for c in recorded_calls[:8]]
    specializations_found = set()

    spec_markers = {
        "cvs": "Cardiology",
        "gi_tract": "Gastroenterology",
        "respiratory": "Pulmonology",
        "nervous": "Neurology",
        "kidneys": "Nephrology",
        "endocrine": "Endocrinology",
        "immune": "Immunology",
        "musculoskeletal": "Orthopedics",
    }

    for p in specialist_prompts:
        assert "NEO REASSESSMENT mode" in p
        for key, marker in spec_markers.items():
            if marker in p:
                specializations_found.add(key)

    assert specializations_found == set(SYSTEM_KEYS), "All 8 unique organ specializations must be represented"


# ==============================================================================
# TEST 4: FRESH VS LONGITUDINAL HISTORICAL CONTEXT SEPARATION
# ==============================================================================

@pytest.mark.anyio
async def test_04_fresh_vs_historical_context_separation(sqlite_db_session: Session):
    """Verifies clean separation: historical context from Cycle 1, fresh inputs from Cycle 2."""
    cycle_1_id, cycle_2_id = _seed_neo_cycle_setup(sqlite_db_session)
    recorded_calls: List[Dict[str, Any]] = []
    gateway = MockGeminiGateway(recorded_calls=recorded_calls)
    service = PrimaryConsiliumService(db=sqlite_db_session, gateway=gateway)

    await service.run_primary_consilium(cycle_id=cycle_2_id)

    # Check Deputy and Pro prompts
    deputy_prompt = recorded_calls[8]["prompt"]
    pro_prompt = recorded_calls[9]["prompt"]

    # Historical context includes Cycle 1 in both
    assert cycle_1_id in deputy_prompt
    assert cycle_1_id in pro_prompt

    # Both contain governance disclaimer
    assert "**NOT** confirmed medical diagnoses" in deputy_prompt
    assert "**NOT** confirmed medical diagnoses" in pro_prompt

    # Pro contains explicit clean slate mandate
    assert "DO NOT inertially copy-paste" in pro_prompt


# ==============================================================================
# TEST 5: STRICT PYDANTIC VALIDATION OF NEO OUTPUTS
# ==============================================================================

@pytest.mark.anyio
async def test_05_strict_pydantic_validation_of_neo_outputs(sqlite_db_session: Session):
    """Verifies that all Neo outputs strictly conform to their Pydantic contracts."""
    _, cycle_2_id = _seed_neo_cycle_setup(sqlite_db_session)
    recorded_calls: List[Dict[str, Any]] = []
    gateway = MockGeminiGateway(recorded_calls=recorded_calls)
    service = PrimaryConsiliumService(db=sqlite_db_session, gateway=gateway)

    result = await service.run_primary_consilium(cycle_id=cycle_2_id)

    # Response is typed
    assert isinstance(result, PrimaryConsiliumClientResponse)
    assert result.mode == ConsiliumMode.NEO_REASSESSMENT
    assert len(result.system_reports) == 8

    # All client reports strictly validate against ClientReportContent(extra="forbid")
    for key, sys_rep in result.system_reports.items():
        assert ClientReportContent.model_validate(sys_rep.content)

    assert ClientReportContent.model_validate(result.overall_report.content)


# ==============================================================================
# TEST 6: CORRECT REPORTS PERSISTENCE UNDER NEW CYCLE ID
# ==============================================================================

@pytest.mark.anyio
async def test_06_reports_persistence_under_new_cycle_id(sqlite_db_session: Session):
    """Verifies all prepared internal and client-facing reports are scoped under new cycle_id."""
    cycle_1_id, cycle_2_id = _seed_neo_cycle_setup(sqlite_db_session)
    recorded_calls: List[Dict[str, Any]] = []
    gateway = MockGeminiGateway(recorded_calls=recorded_calls)
    service = PrimaryConsiliumService(db=sqlite_db_session, gateway=gateway)

    await service.run_primary_consilium(cycle_id=cycle_2_id)

    report_repo = ModelReportRepository(sqlite_db_session)
    c2_server_reports = report_repo.list_server_only(cycle_id=cycle_2_id)
    c2_client_reports = report_repo.list_client_facing(cycle_id=cycle_2_id)

    # 8 specialists + 1 deputy + 1 chief + 1 neo_exec_log + 1 anamnesis_m1 + 1 lineage
    # All server reports must have cycle_id == cycle_2_id
    for r in c2_server_reports:
        assert r.cycle_id == cycle_2_id

    # 8 client systems + 1 user_overall_report = 9 client reports
    assert len(c2_client_reports) == 9
    for r in c2_client_reports:
        assert r.cycle_id == cycle_2_id

    # Execution log report verification
    exec_logs = report_repo.list_server_only(cycle_id=cycle_2_id, report_type=NEO_CONSILIUM_EXECUTION_LOG_REPORT_TYPE)
    assert len(exec_logs) == 1
    log = exec_logs[0].content or {}
    assert log.get("status") == "prepared"
    assert log.get("is_prepared") is True
    assert log.get("is_activated") is False
    assert "metadata" in log
    assert log["metadata"]["new_cycle_id"] == cycle_2_id


# ==============================================================================
# TEST 7: COMPLETE ISOLATION OF SERVER_ONLY FROM CLIENT_FACING
# ==============================================================================

@pytest.mark.anyio
async def test_07_complete_isolation_server_only_from_client_facing(sqlite_db_session: Session):
    """Verifies internal shelves and training sandboxes are never leaked to client."""
    _, cycle_2_id = _seed_neo_cycle_setup(sqlite_db_session)
    recorded_calls: List[Dict[str, Any]] = []
    gateway = MockGeminiGateway(recorded_calls=recorded_calls)
    service = PrimaryConsiliumService(db=sqlite_db_session, gateway=gateway)

    result = await service.run_primary_consilium(cycle_id=cycle_2_id)

    # Check overall report
    overall_json = json.dumps(result.overall_report.model_dump(mode="json"))
    assert "SECRET_NEO_ATHENA_STRATEGY" not in overall_json
    assert "SECRET_NEO_PRO_SANDBOX" not in overall_json
    assert "NEO_CHIEF_PRO_RATIONALE" not in overall_json
    assert "INTERNAL DEPUTY FLASH" not in overall_json

    # Check all system reports
    for key, rep in result.system_reports.items():
        rep_json = json.dumps(rep.model_dump(mode="json"))
        assert "SECRET_NEO" not in rep_json
        assert "system_general_shelf" not in rep_json
        assert "system_training_shelf" not in rep_json


# ==============================================================================
# TEST 8: NO CARRYOVER OF OLD REFERRAL
# ==============================================================================

@pytest.mark.anyio
async def test_08_no_carryover_of_old_referral(sqlite_db_session: Session):
    """Verifies that Cycle 1's active referral is completely isolated and not inherited by Cycle 2."""
    cycle_1_id, cycle_2_id = _seed_neo_cycle_setup(sqlite_db_session)
    recorded_calls: List[Dict[str, Any]] = []
    gateway = MockGeminiGateway(recorded_calls=recorded_calls)
    service = PrimaryConsiliumService(db=sqlite_db_session, gateway=gateway)

    await service.run_primary_consilium(cycle_id=cycle_2_id)

    weekly_service = WeeklyConsiliumService(db=sqlite_db_session, gateway=gateway)
    ref_state_c2 = weekly_service.get_referral_state_for_athena(cycle_2_id)

    assert ref_state_c2.get("has_active_referral") is False
    assert ref_state_c2.get("lifecycle_status") == "none"

    report_repo = ModelReportRepository(sqlite_db_session)
    c2_ref_reps = report_repo.list_server_only(cycle_id=cycle_2_id, report_type="referral_lifecycle_state")
    assert len(c2_ref_reps) == 0, "No referral report should exist in the new reassessment cycle"


# ==============================================================================
# TEST 9: DISALLOWING INITIAL ON NEO CYCLE AND VICE-VERSA
# ==============================================================================

@pytest.mark.anyio
async def test_09_disallowing_initial_on_neo(sqlite_db_session: Session):
    """Verifies strict server authority on ConsiliumMode with explicit rejection on mismatch."""
    _, cycle_2_id = _seed_neo_cycle_setup(sqlite_db_session)
    gateway = MockGeminiGateway()
    service = PrimaryConsiliumService(db=sqlite_db_session, gateway=gateway)

    # 1. Attempting to request INITIAL on a Neo cycle is explicitly rejected
    with pytest.raises(ConsiliumError) as exc_info:
        await service.run_primary_consilium(cycle_id=cycle_2_id, mode=ConsiliumMode.INITIAL)
    assert "contradicts server-determined mode 'neo_reassessment'" in str(exc_info.value)

    # 2. Attempting to request NEO_REASSESSMENT on a baseline initial cycle is explicitly rejected
    pat_fresh = Patient(patient_id="p_fresh_01", status="active")
    sqlite_db_session.add(pat_fresh)
    c_init = MedicalCycle(cycle_id="cyc_fresh_init_01", patient_id="p_fresh_01", status="active")
    sqlite_db_session.add(c_init)
    sqlite_db_session.commit()

    with pytest.raises(ConsiliumError) as exc_info2:
        await service.run_primary_consilium(cycle_id="cyc_fresh_init_01", mode=ConsiliumMode.NEO_REASSESSMENT)
    assert "contradicts server-determined mode 'initial'" in str(exc_info2.value)


# ==============================================================================
# TEST 10: STAGE 8B.2B BOUNDARY — NO ACTIVE STRATEGY, NO WEEKLY T0
# ==============================================================================

@pytest.mark.anyio
async def test_10_boundary_no_active_strategy_and_no_weekly_t0(sqlite_db_session: Session):
    """
    Verifies the critical Stage 8B.2B boundary:
    - cycle.status remains 'pending_reassessment'
    - is_primary_consilium_completed remains False
    - is_neo_consilium_prepared becomes True
    - athena_active_strategy is NOT published
    """
    _, cycle_2_id = _seed_neo_cycle_setup(sqlite_db_session)
    gateway = MockGeminiGateway()
    service = PrimaryConsiliumService(db=sqlite_db_session, gateway=gateway)

    await service.run_primary_consilium(cycle_id=cycle_2_id)

    cycle_repo = MedicalCycleRepository(sqlite_db_session)
    cycle = cycle_repo.get(cycle_2_id)
    assert cycle.status == "pending_reassessment", "Cycle must NOT be activated in Stage 8B.2B"

    assert service.is_primary_consilium_completed(cycle_2_id) is False, (
        "is_primary_consilium_completed must remain False while pending reassessment"
    )
    assert service.is_neo_consilium_prepared(cycle_2_id) is True, (
        "is_neo_consilium_prepared must become True after calculation"
    )

    report_repo = ModelReportRepository(sqlite_db_session)
    strat_reports = report_repo.list_server_only(cycle_id=cycle_2_id, report_type="athena_active_strategy")
    assert len(strat_reports) == 0, "Athena active strategy must NOT be published in 8B.2B"


# ==============================================================================
# TEST 11: ERROR HANDLING DURING ORCHESTRATION (FLASH-LITE, FLASH, PRO, QUALITY GATE)
# ==============================================================================

@pytest.mark.anyio
async def test_11_error_handling_and_atomic_rollback(sqlite_db_session: Session):
    """Verifies that failures in Flash-Lite, Flash, Pro, or Quality Gate trigger rollback and release lease."""
    _, cycle_2_id = _seed_neo_cycle_setup(sqlite_db_session)
    report_repo = ModelReportRepository(sqlite_db_session)

    # 11a: Flash-Lite specialist fails
    gw_fail_lite = MockGeminiGateway(fail_system_key="gi_tract")
    svc1 = PrimaryConsiliumService(db=sqlite_db_session, gateway=gw_fail_lite)
    with pytest.raises(GeminiGatewayError):
        await svc1.run_primary_consilium(cycle_id=cycle_2_id)

    assert svc1.is_neo_consilium_prepared(cycle_2_id) is False
    assert len(report_repo.list_client_facing(cycle_id=cycle_2_id)) == 0

    # 11b: Deputy Flash fails
    gw_fail_flash = MockGeminiGateway(fail_deputy=True)
    svc2 = PrimaryConsiliumService(db=sqlite_db_session, gateway=gw_fail_flash)
    with pytest.raises(GeminiGatewayError):
        await svc2.run_primary_consilium(cycle_id=cycle_2_id)

    assert svc2.is_neo_consilium_prepared(cycle_2_id) is False
    assert len(report_repo.list_client_facing(cycle_id=cycle_2_id)) == 0

    # 11c: Chief Pro fails
    gw_fail_pro = MockGeminiGateway(fail_chief=True)
    svc3 = PrimaryConsiliumService(db=sqlite_db_session, gateway=gw_fail_pro)
    with pytest.raises(GeminiGatewayError):
        await svc3.run_primary_consilium(cycle_id=cycle_2_id)

    assert svc3.is_neo_consilium_prepared(cycle_2_id) is False
    assert len(report_repo.list_client_facing(cycle_id=cycle_2_id)) == 0

    # 11d: Pro Quality Rejection
    gw_reject = MockGeminiGateway(pro_quality_approved=False, pro_quality_reason="Clinical inconsistency detected")
    svc4 = PrimaryConsiliumService(db=sqlite_db_session, gateway=gw_reject)
    with pytest.raises(ConsiliumQualityRejectionError) as exc_info:
        await svc4.run_primary_consilium(cycle_id=cycle_2_id)

    assert "Clinical inconsistency detected" in str(exc_info.value)
    assert svc4.is_neo_consilium_prepared(cycle_2_id) is False
    assert len(report_repo.list_client_facing(cycle_id=cycle_2_id)) == 0


# ==============================================================================
# TEST 12: IDEMPOTENCY REPLAY AND RECOVERY (ZERO DUPLICATE AI CALLS)
# ==============================================================================

@pytest.mark.anyio
async def test_12_idempotency_recovery_zero_duplicate_ai(sqlite_db_session: Session):
    """Verifies that re-running Neo Consilium on an already prepared cycle recovers without extra AI calls."""
    _, cycle_2_id = _seed_neo_cycle_setup(sqlite_db_session)
    recorded_calls: List[Dict[str, Any]] = []
    gateway = MockGeminiGateway(recorded_calls=recorded_calls)
    service = PrimaryConsiliumService(db=sqlite_db_session, gateway=gateway)

    # First run -> 10 calls
    res1 = await service.run_primary_consilium(cycle_id=cycle_2_id)
    assert len(recorded_calls) == 10

    # Second run without allow_version_bump -> recovers with 0 extra calls
    res2 = await service.run_primary_consilium(cycle_id=cycle_2_id, allow_version_bump=False)
    assert len(recorded_calls) == 10, "Subsequent calls must recover from storage with 0 new AI calls"
    assert res2.mode == ConsiliumMode.NEO_REASSESSMENT
    assert res2.overall_report.content.title == res1.overall_report.content.title


# ==============================================================================
# TEST 13: DISTRIBUTED LEASE PROTECTION AND FENCING
# ==============================================================================

@pytest.mark.anyio
async def test_13_distributed_lease_protection_and_fencing(sqlite_db_session: Session):
    """Verifies that cross-process lease prevents concurrent executions of Neo Consilium."""
    _, cycle_2_id = _seed_neo_cycle_setup(sqlite_db_session)
    bind = sqlite_db_session.get_bind()

    s1 = Session(bind=bind, autocommit=False, autoflush=False)
    s2 = Session(bind=bind, autocommit=False, autoflush=False)

    gw = MockGeminiGateway()
    svc1 = PrimaryConsiliumService(db=s1, gateway=gw, lock_registry={})
    svc2 = PrimaryConsiliumService(db=s2, gateway=gw, lock_registry={})

    # Worker 1 acquires lease
    lease_owner = svc1._acquire_cross_process_lease(cycle_2_id)
    assert lease_owner.startswith("lease_primary_")

    # Worker 2 attempts to run Neo Consilium concurrently on cycle_2
    with pytest.raises(ConsiliumConcurrencyConflictError):
        await svc2.run_primary_consilium(cycle_id=cycle_2_id)

    # Worker 1 cleans up lease
    svc1._release_cross_process_lease_safe(cycle_2_id, lease_owner)


# ==============================================================================
# TEST 14: EMERGENCY MONITORING (MODE 2) ACTIVE DURING PENDING NEO
# ==============================================================================

def test_14_emergency_monitoring_mode2_active_during_pending_neo(sqlite_db_session: Session):
    """Verifies that Mode 2 vitals ingestion and safety monitoring work normally during pending Neo."""
    patient_id = "p_mode2_test"
    _, cycle_2_id = _seed_neo_cycle_setup(sqlite_db_session, patient_id=patient_id)

    meas_repo = HealthMeasurementRepository(sqlite_db_session)
    now_dt = datetime.now(timezone.utc)

    # Ingest vital measurement for the pending cycle
    meas = meas_repo.create(
        cycle_id=cycle_2_id,
        source="smartwatch",
        systolic=120,
        diastolic=80,
        pulse=70,
        spo2=98,
        measured_at=now_dt,
    )
    assert meas.id is not None

    measurements = meas_repo.list_by_cycle(cycle_id=cycle_2_id)
    assert len(measurements) >= 1
    assert any(m.pulse == 70 for m in measurements)


# ==============================================================================
# TEST 15: BASELINE INITIAL PRIMARY IS COMPLETELY PRESERVED
# ==============================================================================

@pytest.mark.anyio
async def test_15_baseline_initial_primary_completely_preserved(sqlite_db_session: Session):
    """Verifies that Initial Primary Consilium runs completely unchanged for brand new cycles."""
    now = datetime.now(timezone.utc)
    patient = Patient(patient_id="p_initial_bench_01", status="active")
    sqlite_db_session.add(patient)

    c_init = MedicalCycle(
        cycle_id="cyc_init_bench_01",
        patient_id="p_initial_bench_01",
        status="active",
        started_at=now,
    )
    sqlite_db_session.add(c_init)
    sqlite_db_session.commit()

    report_repo = ModelReportRepository(sqlite_db_session)
    raw_m1 = _sample_anamnesis_output()
    raw_m1["agent_role"] = AegisAgentRole.ANAMNESIS_M1.value
    report_repo.create_next_version(
        cycle_id="cyc_init_bench_01",
        model="gemini-2.5-flash",
        report_type=PREPARATORY_ANAMNESIS_REPORT_TYPE,
        privacy_level=PrivacyLevel.SERVER_ONLY,
        content=raw_m1,
    )

    recorded_calls: List[Dict[str, Any]] = []
    gateway = MockGeminiGateway(recorded_calls=recorded_calls)
    service = PrimaryConsiliumService(db=sqlite_db_session, gateway=gateway)

    result = await service.run_primary_consilium(cycle_id="cyc_init_bench_01")

    assert result.mode == ConsiliumMode.INITIAL
    assert len(recorded_calls) == 10
    assert service.is_primary_consilium_completed("cyc_init_bench_01") is True
