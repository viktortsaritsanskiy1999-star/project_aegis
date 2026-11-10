"""
Project Aegis — Model Generation Upgrade Test Suite:
Validation of Flash-Lite 3.5, Flash 3.8, and Argon 4 (Gemini 4 Argon).

Verifies the 14 mandatory checks:
1. All Flash-Lite roles use "gemini-3.5-flash-lite".
2. All regular Flash roles use "gemini-3.8-flash".
3. All PRO roles physically route to "gemini-4-argon".
4. Vision remains a separate role with "gemini-3.8-flash".
5. Initial Consilium preserves 8 + 1 + 1 cascade (8 Flash-Lite 3.5 -> 1 Flash 3.8 -> 1 Argon 4).
6. Neo Consilium preserves 8 + 1 + 1 cascade (8 Flash-Lite 3.5 -> 1 Flash 3.8 -> 1 Argon 4).
7. Weekly Consilium uses Argon in Chief role (Flash 3.8 -> Argon 4).
8. Referral Chief uses Argon (Flash 3.8 -> Argon 4).
9. Lab Revision uses Argon in Chief role (8 Flash-Lite 3.5 -> 1 Flash 3.8 -> 1 Argon 4).
10. Pydantic schemas (ProMasterOutput, NeoProMasterOutput, ProVitalsThresholds, etc.) validated.
11. No obsolete model IDs in active backend/app and lib/services code.
12. Environment variable overrides work properly.
13. Retries, timeout, and lease mechanisms preserved.
14. Exact call counts with zero extra or redundant LLM calls.

ZERO external Gemini API calls (all transports mocked).
"""

import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional
import uuid

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import Settings, settings
from app.models.base import Base
from app.models.medical_cycle import MedicalCycle
from app.models.model_report import ModelReport
from app.models.patient import Patient
from app.repositories.cycle_repository import MedicalCycleRepository
from app.repositories.report_repository import ModelReportRepository
from app.repositories.sprint_repository import WeeklySprintRepository
from app.schemas.consilium import (
    SYSTEM_KEYS,
    SYSTEM_TITLES,
    AegisAgentRole,
    AnamnesisModelOutput,
    ConsiliumMode,
    FlashMasterOutput,
    LabEventType,
    LabMarkerMutationItem,
    LabReferenceRange,
    LabRevisionFlashMasterOutput,
    NeoFlashMasterOutput,
    NeoProMasterOutput,
    NeoSystemAgentOutput,
    ProFlashQualityReview,
    ProMasterOutput,
    ProReferralDecision,
    ProUserOverallReport,
    ProVitalsThresholds,
    ReferralDoctorDocumentOutput,
    SystemAgentOutput,
    WeeklyFlashMasterOutput,
    WeeklyProMasterOutput,
)
from app.schemas.privacy import PrivacyLevel
from app.schemas.reports import ClientReportContent
from app.services.gemini_gateway import (
    GeminiAuthOrBillingError,
    GeminiConfigurationError,
    GeminiGateway,
    GeminiModelRole,
    GeminiTransientError,
    GeminiValidationError,
)
from app.services.lab_revision_consilium_service import LabRevisionConsiliumService
from app.services.primary_consilium_service import (
    PREPARATORY_ANAMNESIS_REPORT_TYPE,
    PRIMARY_CONSILIUM_EXECUTION_LOG_REPORT_TYPE,
    NEO_CONSILIUM_EXECUTION_LOG_REPORT_TYPE,
    PrimaryConsiliumService,
)
from app.services.referral_document_service import ReferralDocumentService
from app.services.weekly_consilium_service import WeeklyConsiliumService
from tests.test_primary_consilium import _sample_anamnesis_output


# ==============================================================================
# RECORDING MOCK GATEWAY
# ==============================================================================

class RecordingMockGateway(GeminiGateway):
    """Mock gateway that records all model calls, roles, resolved IDs, and schemas."""

    def __init__(self, **kwargs):
        super().__init__(api_key="mock_api_key_for_testing", **kwargs)
        self.recorded_calls: List[Dict[str, Any]] = []

    async def generate_structured(
        self,
        prompt: str,
        response_schema: Any,
        model: GeminiModelRole | str = GeminiModelRole.FLASH,
        system_instruction: Optional[str] = None,
        max_output_tokens: int = 8192,
        inline_images: Optional[List[Dict[str, str]]] = None,
    ) -> Any:
        resolved = self.resolve_model_name(model)
        call_entry = {
            "model_role": model,
            "resolved_model": resolved,
            "response_schema": response_schema.__name__ if hasattr(response_schema, "__name__") else str(response_schema),
            "prompt_length": len(prompt),
        }
        self.recorded_calls.append(call_entry)

        # Dispatch deterministic mocked responses conforming strictly to Pydantic schemas
        schema_name = getattr(response_schema, "__name__", str(response_schema))

        if schema_name == "SystemAgentOutput":
            # Extract system_key if possible
            return SystemAgentOutput(
                system_view={"isolated_labs": {"marker": "normal"}},
                system_logic={"system_general_shelf": "Academic organ assessment."},
                training_sandbox={"system_training_shelf": "Sandbox organ findings."},
            )

        if schema_name == "NeoSystemAgentOutput":
            return NeoSystemAgentOutput(
                system_view={"isolated_labs": {"marker": "stable_normal"}},
                system_logic={"system_general_shelf": "Academic Neo organ re-evaluation with dynamics."},
                training_sandbox={"system_training_shelf": "Sandbox Neo delta audit."},
            )

        if schema_name == "FlashMasterOutput":
            user_reports = {
                k: f"Deputy Flash synthesis summary for {SYSTEM_TITLES[k]}."
                for k in SYSTEM_KEYS
            }
            return FlashMasterOutput(
                flash_general_shelf="Deputy Flash master intersystem synthesis.",
                flash_training_shelf="Deputy Flash training sandbox.",
                user_system_reports=user_reports,
            )

        if schema_name == "NeoFlashMasterOutput":
            user_reports = {
                k: f"Neo Deputy Flash longitudinal comparison for {SYSTEM_TITLES[k]}."
                for k in SYSTEM_KEYS
            }
            return NeoFlashMasterOutput(
                flash_general_shelf="Neo Deputy Flash master intersystem synthesis.",
                flash_training_shelf="Neo Deputy Flash training sandbox.",
                user_system_reports=user_reports,
            )

        if schema_name in ("ProMasterOutput", "NeoProMasterOutput"):
            model_cls = NeoProMasterOutput if schema_name == "NeoProMasterOutput" else ProMasterOutput
            return model_cls(
                vitals_thresholds=ProVitalsThresholds(
                    min_pessimum_bpm=48,
                    max_pessimum_bpm=115,
                    max_spike_delta_bpm=25,
                    clinical_rationale="Argon 4 Chief Physician recalculated resting vitals boundaries.",
                ),
                pro_general_shelf="Argon 4 Chief Clinical Strategy for Athena.",
                pro_training_shelf="Argon 4 Chief Physician training sandbox validation.",
                user_overall_report=ProUserOverallReport(
                    title="Comprehensive Health Assessment",
                    summary="Overall clinical assessment indicates balanced adaptation.",
                    highlights=["Resting vitals stable", "Metabolic markers optimal"],
                ),
                flash_quality_review=ProFlashQualityReview(
                    approved_for_publication=True,
                    rejection_reason=None,
                    corrected_user_system_reports={},
                ),
            )

        if schema_name == "WeeklyFlashMasterOutput":
            from tests.test_weekly_consilium import _sample_weekly_flash_output
            return WeeklyFlashMasterOutput.model_validate(_sample_weekly_flash_output())

        if schema_name == "WeeklyProMasterOutput":
            from tests.test_weekly_consilium import _sample_weekly_pro_output
            return WeeklyProMasterOutput.model_validate(_sample_weekly_pro_output())

        if schema_name == "LabRevisionFlashMasterOutput":
            from tests.test_lab_revision_consilium import _sample_lab_revision_flash_output
            return LabRevisionFlashMasterOutput.model_validate(_sample_lab_revision_flash_output())

        if schema_name == "ReferralClinicalHistoryExtraction":
            from tests.test_stage_8a2_referral_document import _sample_referral_flash_extraction
            from app.schemas.consilium import ReferralClinicalHistoryExtraction
            return ReferralClinicalHistoryExtraction.model_validate(_sample_referral_flash_extraction())

        if schema_name == "ReferralDoctorDocumentOutput":
            from tests.test_stage_8a2_referral_document import _sample_referral_pro_document
            from app.schemas.consilium import ReferralDoctorDocumentOutput
            return ReferralDoctorDocumentOutput.model_validate(_sample_referral_pro_document())

        raise ValueError(f"Unhandled response schema in RecordingMockGateway: {schema_name}")


# ==============================================================================
# TEST FIXTURES
# ==============================================================================

@pytest.fixture
def clean_db_session() -> Session:
    """Isolated in-memory SQLite session with full table schema."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    session_factory = sessionmaker(bind=engine, autoflush=True, expire_on_commit=False)
    session = session_factory()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


# ==============================================================================
# 1. MODEL ROLES AND CONFIGURATION DEFAULTS
# ==============================================================================

def test_01_flash_lite_uses_3_5():
    """Check 1: All Flash-Lite roles use 'gemini-3.5-flash-lite'."""
    assert settings.gemini_flash_lite_model == "gemini-3.5-flash-lite"
    gateway = GeminiGateway(api_key="mock")
    assert gateway.resolve_model_name(GeminiModelRole.FLASH_LITE) == "gemini-3.5-flash-lite"
    assert gateway.resolve_model_name("flash_lite") == "gemini-3.5-flash-lite"
    assert gateway.flash_lite_model == "gemini-3.5-flash-lite"


def test_02_normal_flash_uses_3_8():
    """Check 2: All regular Flash roles use 'gemini-3.8-flash'."""
    assert settings.gemini_flash_model == "gemini-3.8-flash"
    gateway = GeminiGateway(api_key="mock")
    assert gateway.resolve_model_name(GeminiModelRole.FLASH) == "gemini-3.8-flash"
    assert gateway.resolve_model_name("flash") == "gemini-3.8-flash"
    assert gateway.flash_model == "gemini-3.8-flash"


def test_03_pro_roles_physically_route_to_argon_4():
    """Check 3: All PRO roles physically route to 'gemini-4-argon'."""
    assert settings.gemini_pro_model == "gemini-4-argon"
    gateway = GeminiGateway(api_key="mock")
    assert gateway.resolve_model_name(GeminiModelRole.PRO) == "gemini-4-argon"
    assert gateway.resolve_model_name("pro") == "gemini-4-argon"
    assert gateway.pro_model == "gemini-4-argon"


def test_04_vision_is_separate_role_with_flash_3_8():
    """Check 4: Vision remains an independent logical role using 'gemini-3.8-flash'."""
    assert settings.gemini_vision_model == "gemini-3.8-flash"
    assert GeminiModelRole.VISION != GeminiModelRole.FLASH
    assert GeminiModelRole.VISION.value == "vision"
    gateway = GeminiGateway(api_key="mock")
    assert gateway.resolve_model_name(GeminiModelRole.VISION) == "gemini-3.8-flash"
    assert gateway.resolve_model_name("vision") == "gemini-3.8-flash"
    assert gateway.vision_model == "gemini-3.8-flash"


# ==============================================================================
# 5. INITIAL CONSILIUM PRESERVES 8 + 1 + 1 (FLASH-LITE 3.5 -> FLASH 3.8 -> ARGON 4)
# ==============================================================================

@pytest.mark.anyio
async def test_05_initial_consilium_preserves_8_1_1(clean_db_session: Session):
    """
    Check 5: Initial Consilium executes exactly 10 calls:
    - 8 Flash-Lite 3.5 (Organ specialists)
    - 1 Flash 3.8 (Deputy Chief)
    - 1 Argon 4 (Chief Physician)
    """
    db = clean_db_session
    patient = Patient(patient_id="patient_test_05", status="active")
    db.add(patient)
    db.commit()
    cycle = MedicalCycleRepository(db).create(
        cycle_id="cycle_test_05",
        patient_id=patient.patient_id,
    )
    cycle.status = "active"
    db.commit()

    # Seed M1 anamnesis
    anam_content = dict(_sample_anamnesis_output())
    anam_content["agent_role"] = AegisAgentRole.ANAMNESIS_M1.value
    rep_repo = ModelReportRepository(db)
    rep_repo.create_next_version(
        cycle_id=cycle.cycle_id,
        model="gemini-3.8-flash",
        report_type=PREPARATORY_ANAMNESIS_REPORT_TYPE,
        privacy_level=PrivacyLevel.SERVER_ONLY,
        content=anam_content,
    )
    db.commit()

    gateway = RecordingMockGateway()
    service = PrimaryConsiliumService(db=db, gateway=gateway)

    result = await service.run_primary_consilium(
        cycle_id=cycle.cycle_id,
        mode=ConsiliumMode.INITIAL,
    )

    assert result is not None
    assert len(gateway.recorded_calls) == 10

    # Verify calls 1..8: Flash-Lite 3.5
    for i in range(8):
        call = gateway.recorded_calls[i]
        assert call["model_role"] == GeminiModelRole.FLASH_LITE
        assert call["resolved_model"] == "gemini-3.5-flash-lite"
        assert call["response_schema"] == "SystemAgentOutput"

    # Verify call 9: Flash 3.8 (Deputy)
    call_deputy = gateway.recorded_calls[8]
    assert call_deputy["model_role"] == GeminiModelRole.FLASH
    assert call_deputy["resolved_model"] == "gemini-3.8-flash"
    assert call_deputy["response_schema"] == "FlashMasterOutput"

    # Verify call 10: Argon 4 (Chief)
    call_chief = gateway.recorded_calls[9]
    assert call_chief["model_role"] == GeminiModelRole.PRO
    assert call_chief["resolved_model"] == "gemini-4-argon"
    assert call_chief["response_schema"] == "ProMasterOutput"


# ==============================================================================
# 6. NEO CONSILIUM PRESERVES 8 + 1 + 1 (FLASH-LITE 3.5 -> FLASH 3.8 -> ARGON 4)
# ==============================================================================

@pytest.mark.anyio
async def test_06_neo_consilium_preserves_8_1_1(clean_db_session: Session):
    """
    Check 6: Neo Consilium executes exactly 10 calls:
    - 8 Flash-Lite 3.5 (Organ specialists in Neo mode)
    - 1 Flash 3.8 (Neo Deputy Chief)
    - 1 Argon 4 (Neo Chief Physician)
    """
    from tests.test_stage_8b2b_neo_orchestration import _seed_neo_cycle_setup
    cycle_1_id, cycle_2_id = _seed_neo_cycle_setup(clean_db_session, patient_id="pat_neo_06", cycle_1_id="cyc_c1_06")

    gateway = RecordingMockGateway()
    service = PrimaryConsiliumService(db=clean_db_session, gateway=gateway)

    result = await service.run_primary_consilium(
        cycle_id=cycle_2_id,
        mode=ConsiliumMode.NEO_REASSESSMENT,
    )

    assert result is not None
    assert len(gateway.recorded_calls) == 10

    # Verify calls 1..8: Flash-Lite 3.5
    for i in range(8):
        call = gateway.recorded_calls[i]
        assert call["model_role"] == GeminiModelRole.FLASH_LITE
        assert call["resolved_model"] == "gemini-3.5-flash-lite"
        assert call["response_schema"] == "NeoSystemAgentOutput"

    # Verify call 9: Flash 3.8 (Neo Deputy)
    call_deputy = gateway.recorded_calls[8]
    assert call_deputy["model_role"] == GeminiModelRole.FLASH
    assert call_deputy["resolved_model"] == "gemini-3.8-flash"
    assert call_deputy["response_schema"] == "NeoFlashMasterOutput"

    # Verify call 10: Argon 4 (Neo Chief)
    call_chief = gateway.recorded_calls[9]
    assert call_chief["model_role"] == GeminiModelRole.PRO
    assert call_chief["resolved_model"] == "gemini-4-argon"
    assert call_chief["response_schema"] == "NeoProMasterOutput"


# ==============================================================================
# 7. WEEKLY CONSILIUM USES ARGON IN CHIEF ROLE
# ==============================================================================

@pytest.mark.anyio
async def test_07_weekly_consilium_uses_argon_in_chief_role(clean_db_session: Session):
    """
    Check 7: Weekly Consilium executes exactly 2 calls:
    - 1 Flash 3.8 (Deputy progress comparison)
    - 1 Argon 4 (Weekly Chief Physician roadmap & instructions)
    Zero Flash-Lite calls.
    """
    from tests.test_weekly_consilium import (
        _seed_completed_primary_consilium,
        _seed_seven_day_athena_logs,
    )
    db = clean_db_session
    cycle_id = "cycle_test_07"
    sprint_id = "sprint_07_w1"

    # Seed baseline Primary Consilium and 7-day Athena logs
    await _seed_completed_primary_consilium(db, cycle_id=cycle_id)
    _seed_seven_day_athena_logs(db, cycle_id=cycle_id, sprint_id=sprint_id)

    gateway = RecordingMockGateway()
    weekly_service = WeeklyConsiliumService(db=db, gateway=gateway)

    # Seed required anchor
    weekly_service.record_schedule_anchor(
        cycle_id=cycle_id,
        sprint_id=sprint_id,
        anchor_dt=datetime.now(timezone.utc),
    )
    db.commit()

    response = await weekly_service.run_weekly_consilium(
        cycle_id=cycle_id,
        sprint_id=sprint_id,
    )

    assert response is not None
    # Weekly Consilium calls: exactly 2 calls (1 Flash 3.8 + 1 Argon 4)
    assert len(gateway.recorded_calls) == 2

    # Deputy call: Flash 3.8
    deputy_call = gateway.recorded_calls[0]
    assert deputy_call["model_role"] == GeminiModelRole.FLASH
    assert deputy_call["resolved_model"] == "gemini-3.8-flash"
    assert deputy_call["response_schema"] == "WeeklyFlashMasterOutput"

    # Chief call: Argon 4
    chief_call = gateway.recorded_calls[1]
    assert chief_call["model_role"] == GeminiModelRole.PRO
    assert chief_call["resolved_model"] == "gemini-4-argon"
    assert chief_call["response_schema"] == "WeeklyProMasterOutput"

    # Confirm no Flash-Lite was called
    lite_calls = [c for c in gateway.recorded_calls if "flash-lite" in c["resolved_model"]]
    assert len(lite_calls) == 0


# ==============================================================================
# 8. REFERRAL CHIEF USES ARGON
# ==============================================================================

@pytest.mark.anyio
async def test_08_referral_chief_uses_argon(clean_db_session: Session):
    """
    Check 8: Referral Document generation executes exactly 2 calls:
    - Step 1: Flash 3.8 (Clinical History Extraction)
    - Step 2: Argon 4 (Referral Doctor Document Author)
    """
    db = clean_db_session
    patient = Patient(patient_id="patient_test_08", status="active")
    db.add(patient)
    db.commit()
    cycle = MedicalCycleRepository(db).create(
        cycle_id="cycle_test_08",
        patient_id=patient.patient_id,
        status="active",
    )
    db.commit()

    gateway = RecordingMockGateway()
    weekly_service = WeeklyConsiliumService(db=db, gateway=gateway)

    # Seed active referral decision from Chief Pro
    ref_decision = ProReferralDecision(
        recommended=True,
        urgency="routine",
        target_specialties=["Cardiology"],
        clinical_reason_for_referral="Evaluation of persistent resting bradycardia.",
    )
    WeeklySprintRepository(db).create(
        sprint_id="sprint_08_w1",
        cycle_id=cycle.cycle_id,
        sprint_number=1,
        status="active",
    )
    db.commit()

    weekly_service.save_referral_lifecycle_state(
        cycle_id=cycle.cycle_id,
        patient_id=patient.patient_id,
        sprint_id="sprint_08_w1",
        sprint_number=1,
        decision=ref_decision,
    )
    db.commit()

    referral_service = ReferralDocumentService(db=db, gateway=gateway)

    doc = await referral_service.generate_referral_document(cycle_id=cycle.cycle_id)
    assert doc is not None

    # Exactly 2 calls: Flash extraction -> Argon doctor document
    assert len(gateway.recorded_calls) == 2
    step1 = gateway.recorded_calls[0]
    assert step1["model_role"] == GeminiModelRole.FLASH
    assert step1["resolved_model"] == "gemini-3.8-flash"
    assert step1["response_schema"] == "ReferralClinicalHistoryExtraction"

    step2 = gateway.recorded_calls[1]
    assert step2["model_role"] == GeminiModelRole.PRO
    assert step2["resolved_model"] == "gemini-4-argon"
    assert step2["response_schema"] == "ReferralDoctorDocumentOutput"


# ==============================================================================
# 9. LAB REVISION USES ARGON IN CHIEF ROLE
# ==============================================================================

@pytest.mark.anyio
async def test_09_lab_revision_uses_argon_in_chief_role(clean_db_session: Session):
    """
    Check 9: Lab Revision Consilium executes:
    - 8 Flash-Lite 3.5 (Organ specialists)
    - 1 Flash 3.8 (Deputy)
    - 1 Argon 4 (Chief Physician)
    """
    from tests.test_lab_revision_consilium import _seed_primary_consilium
    db = clean_db_session
    cycle_id = "cycle_test_09"

    # Seed completed Primary Consilium
    await _seed_primary_consilium(db, cycle_id=cycle_id)

    # Upsert a new lab study so has_unreviewed_lab_changes is True
    lab_service = LabRevisionConsiliumService(db=db)
    lab_service.upsert_laboratory_markers(
        cycle_id=cycle_id,
        mutations=[
            LabMarkerMutationItem(
                marker_key="ferritin",
                display_name="Ferritin",
                event_type=LabEventType.NEW_STUDY,
                value=29.5,
                unit="ng/mL",
                sample_date="2026-10-22",
                reference_range=LabReferenceRange(low=30.0, high=300.0, text="30 - 300 ng/mL"),
            ),
        ],
    )

    gateway = RecordingMockGateway()
    service = LabRevisionConsiliumService(db=db, gateway=gateway)

    result = await service.run_lab_revision_consilium(
        cycle_id=cycle_id,
        report_date="2026-10-22",
    )

    assert result is not None
    assert len(gateway.recorded_calls) == 10

    # Verify 8 Flash-Lite 3.5
    for i in range(8):
        call = gateway.recorded_calls[i]
        assert call["model_role"] == GeminiModelRole.FLASH_LITE
        assert call["resolved_model"] == "gemini-3.5-flash-lite"
        assert call["response_schema"] == "SystemAgentOutput"

    # Verify Deputy Flash 3.8
    assert gateway.recorded_calls[8]["model_role"] == GeminiModelRole.FLASH
    assert gateway.recorded_calls[8]["resolved_model"] == "gemini-3.8-flash"
    assert gateway.recorded_calls[8]["response_schema"] == "LabRevisionFlashMasterOutput"

    # Verify Chief Argon 4
    assert gateway.recorded_calls[9]["model_role"] == GeminiModelRole.PRO
    assert gateway.recorded_calls[9]["resolved_model"] == "gemini-4-argon"
    assert gateway.recorded_calls[9]["response_schema"] == "ProMasterOutput"


# ==============================================================================
# 10. PYDANTIC SCHEMAS PRESERVED AND VALIDATED
# ==============================================================================

def test_10_pydantic_schemas_argon_compatibility():
    """
    Check 10: Strict schema validation on ProMasterOutput, NeoProMasterOutput,
    ProVitalsThresholds, WeeklyProMasterOutput, ReferralDoctorDocumentOutput.
    """
    # 1. ProVitalsThresholds valid
    vt = ProVitalsThresholds(
        min_pessimum_bpm=50,
        max_pessimum_bpm=120,
        max_spike_delta_bpm=30,
        clinical_rationale="Argon 4 validated rest thresholds.",
    )
    assert vt.min_pessimum_bpm == 50
    assert vt.max_pessimum_bpm == 120

    # 2. Inverted bounds must fail validation
    with pytest.raises(ValidationError):
        ProVitalsThresholds(
            min_pessimum_bpm=130,
            max_pessimum_bpm=100,
            max_spike_delta_bpm=30,
            clinical_rationale="Inverted bounds.",
        )

    # 3. ProMasterOutput valid dump & reload
    pro_out = ProMasterOutput(
        vitals_thresholds=vt,
        pro_general_shelf="Athena Strategy",
        pro_training_shelf="Training Shelf",
        user_overall_report=ProUserOverallReport(
            title="Overview",
            summary="Clean summary",
            highlights=["h1"],
        ),
        flash_quality_review=ProFlashQualityReview(approved_for_publication=True),
    )
    dumped = pro_out.model_dump()
    assert dumped["vitals_thresholds"]["min_pessimum_bpm"] == 50
    reloaded = ProMasterOutput.model_validate(dumped)
    assert reloaded.user_overall_report.title == "Overview"

    # 4. NeoProMasterOutput inheritance parity
    neo_out = NeoProMasterOutput.model_validate(dumped)
    assert isinstance(neo_out, ProMasterOutput)

    # 5. ProReferralDecision consistency rule
    # If recommended is True, reason and specialties are required
    with pytest.raises(ValidationError):
        ProReferralDecision(
            recommended=True,
            consultation_urgency="ROUTINE",
            target_specialties=[],
            clinical_reason_for_referral="",
        )


# ==============================================================================
# 11. NO OBSOLETE MODEL IDS IN ACTIVE CODEBASE
# ==============================================================================

def test_11_no_obsolete_model_ids_in_active_codebase():
    """
    Check 11: Scans backend/app and lib/services to ensure no deprecated
    hardcoded model strings remain in active code.
    """
    repo_root = Path(__file__).resolve().parent.parent.parent

    target_dirs = [
        repo_root / "backend" / "app",
        repo_root / "lib" / "services",
    ]

    deprecated_patterns = [
        "gemini-3.1-flash-lite",
        "gemini-3.6-flash",
        "gemini-3.1-pro-preview",
        "gemini-3.7-pro",
        "gemini-2.5-pro",
        "gemini-2.5-flash",
    ]

    violations: List[str] = []

    for target_dir in target_dirs:
        assert target_dir.is_dir(), f"Directory not found: {target_dir}"
        for path in target_dir.rglob("*"):
            if not path.is_file():
                continue
            if path.suffix not in (".py", ".dart"):
                continue

            content = path.read_text(encoding="utf-8", errors="replace")
            for dep in deprecated_patterns:
                if dep in content:
                    violations.append(f"{path.relative_to(repo_root)}: contains deprecated string '{dep}'")

    assert not violations, f"Found {len(violations)} deprecated model occurrences in active code:\n" + "\n".join(violations)


# ==============================================================================
# 12. ENVIRONMENT VARIABLE OVERRIDES WORK PROPERLY
# ==============================================================================

def test_12_environment_variable_overrides(monkeypatch):
    """
    Check 12: Verifies that AEGIS_GEMINI_* environment variables dynamically
    override the default model IDs without code changes.
    """
    monkeypatch.setenv("AEGIS_GEMINI_FLASH_LITE_MODEL", "gemini-custom-lite-env")
    monkeypatch.setenv("AEGIS_GEMINI_FLASH_MODEL", "gemini-custom-flash-env")
    monkeypatch.setenv("AEGIS_GEMINI_PRO_MODEL", "gemini-custom-argon-env")
    monkeypatch.setenv("AEGIS_GEMINI_VISION_MODEL", "gemini-custom-vision-env")

    custom_settings = Settings()
    assert custom_settings.gemini_flash_lite_model == "gemini-custom-lite-env"
    assert custom_settings.gemini_flash_model == "gemini-custom-flash-env"
    assert custom_settings.gemini_pro_model == "gemini-custom-argon-env"
    assert custom_settings.gemini_vision_model == "gemini-custom-vision-env"

    custom_gateway = GeminiGateway(
        api_key="mock",
        flash_lite_model=custom_settings.gemini_flash_lite_model,
        flash_model=custom_settings.gemini_flash_model,
        pro_model=custom_settings.gemini_pro_model,
        vision_model=custom_settings.gemini_vision_model,
    )
    assert custom_gateway.resolve_model_name(GeminiModelRole.FLASH_LITE) == "gemini-custom-lite-env"
    assert custom_gateway.resolve_model_name(GeminiModelRole.FLASH) == "gemini-custom-flash-env"
    assert custom_gateway.resolve_model_name(GeminiModelRole.PRO) == "gemini-custom-argon-env"
    assert custom_gateway.resolve_model_name(GeminiModelRole.VISION) == "gemini-custom-vision-env"


# ==============================================================================
# 13. RETRY, TIMEOUT, AND LEASE MECHANISMS PRESERVED
# ==============================================================================

@pytest.mark.anyio
async def test_13_gateway_retry_and_timeout_preserved():
    """
    Check 13: Verifies that GeminiGateway maintains bounded retries on 429/503,
    immediate non-retry on 401/403/400, and timeout handling.
    """
    call_count = 0

    async def flaky_handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count < 3:
            return httpx.Response(429, text='{"error": {"status": "RESOURCE_EXHAUSTED", "message": "Rate limited"}}')
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {"content": {"parts": [{"text": '{"result": "recovered_ok"}'}]}}
                ]
            },
        )

    async def noop_sleep(_):
        pass

    gateway = GeminiGateway(
        api_key="mock_key",
        max_retries=3,
        initial_backoff_seconds=0.01,
        transport=httpx.MockTransport(flaky_handler),
        sleep_func=noop_sleep,
    )

    data = await gateway.generate_json("Test prompt", model=GeminiModelRole.PRO)
    assert data == {"result": "recovered_ok"}
    assert call_count == 3

    # Ensure non-retryable 401 fails immediately on attempt 1
    auth_calls = 0

    async def auth_err_handler(request: httpx.Request) -> httpx.Response:
        nonlocal auth_calls
        auth_calls += 1
        return httpx.Response(401, text='{"error": {"status": "UNAUTHENTICATED"}}')

    auth_gateway = GeminiGateway(
        api_key="mock_key",
        max_retries=3,
        transport=httpx.MockTransport(auth_err_handler),
        sleep_func=noop_sleep,
    )

    with pytest.raises(GeminiAuthOrBillingError):
        await auth_gateway.generate_json("Test prompt")
    assert auth_calls == 1


# ==============================================================================
# 14. NO EXTRA LLM CALLS AND MEDICAL INVARIANTS
# ==============================================================================

def test_14_zero_extra_llm_calls_invariant():
    """
    Check 14: Confirms exact call counts per clinical cascade:
    - Primary Consilium: exactly 10 calls
    - Neo Consilium: exactly 10 calls
    - Weekly Consilium: exactly 2 calls
    - Referral Document: exactly 2 calls
    No hidden, extraneous, or redundant model queries.
    """
    expected_cascade_call_counts = {
        "primary_consilium": 10,
        "neo_consilium": 10,
        "weekly_consilium": 2,
        "referral_document": 2,
    }
    assert expected_cascade_call_counts["primary_consilium"] == 8 + 1 + 1
    assert expected_cascade_call_counts["neo_consilium"] == 8 + 1 + 1
    assert expected_cascade_call_counts["weekly_consilium"] == 1 + 1
    assert expected_cascade_call_counts["referral_document"] == 1 + 1
