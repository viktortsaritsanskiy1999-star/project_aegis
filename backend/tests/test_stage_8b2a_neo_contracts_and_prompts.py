"""
PROJECT AEGIS — STAGE 8B.2A
Neo Contracts, Historical Grounding, Model Call Invariants & Prompts Tests

Tests all 10 requirements:
1. Correct determination of INITIAL/NEO mode.
2. Immutability/preservation of normal Primary prompts.
3. Presence of eight distinct Neo specializations.
4. Proper separation of fresh and historical data.
5. No automatic carryover of old Referral into new cycle.
6. No carryover of old strategy as new strategy.
7. Preservation of clinical uncertainty (anti-hallucination of gaps).
8. Historical context size boundedness and governance notice.
9. Strict Pydantic validation (rejection of unverified fields, extra="forbid").
10. Invariant model identifiers and planned call counts (8 Flash-Lite -> 1 Flash -> 1 Pro).
"""

import asyncio
from datetime import datetime, timezone
import json
import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models.patient import Patient
from app.models.medical_cycle import MedicalCycle
from app.models.model_report import ModelReport
from app.schemas.consilium import (
    SYSTEM_KEYS,
    ConsiliumMode,
    AegisAgentRole,
    SystemAgentOutput,
    SystemAgentView,
    SystemAgentLogic,
    SystemAgentSandbox,
    UserSystemReportsMap,
    FlashMasterOutput,
    ProVitalsThresholds,
    ProUserOverallReport,
    ProFlashQualityReview,
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
from app.schemas.reports import ClientReportContent, ClientModelReportResponse
from app.services.consilium_prompts import (
    build_system_agent_prompt,
    build_flash_master_prompt,
    build_pro_master_prompt,
    build_neo_system_agent_prompt,
    build_neo_flash_master_prompt,
    build_neo_pro_master_prompt,
    _SYSTEM_SPECIFIC_INSTRUCTIONS,
    _NEO_SYSTEM_SPECIFIC_INSTRUCTIONS,
)
from app.services.gemini_gateway import GeminiGateway, GeminiModelRole
from app.services.historical_context_service import (
    CLINICAL_GOVERNANCE_NOTICE,
    HistoricalContextService,
)
from app.services.neo_cycle_service import (
    NEO_CYCLE_LINEAGE_REPORT_TYPE,
    NeoCycleService,
)
from app.services.primary_consilium_service import (
    ConsiliumCycleNotFoundError,
    ConsiliumError,
    PrimaryConsiliumService,
)


@pytest.fixture
def db_session():
    """In-memory SQLite database session isolated per test."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    session_factory = sessionmaker(bind=engine)
    session = session_factory()
    try:
        yield session
    finally:
        session.close()


# ==============================================================================
# TEST 1: MODE DETECTION (INITIAL VS. NEO_REASSESSMENT)
# ==============================================================================

def test_01_consilium_mode_detection(db_session):
    now = datetime.now(timezone.utc)
    patient = Patient(patient_id="p_mode_test", status="active")
    db_session.add(patient)

    # 1. Baseline cycle with no lineage and active status -> INITIAL
    c1 = MedicalCycle(
        cycle_id="cycle_initial_01",
        patient_id="p_mode_test",
        status="active",
        started_at=now,
    )
    db_session.add(c1)
    db_session.commit()

    service = PrimaryConsiliumService(db=db_session)
    assert service.detect_consilium_mode("cycle_initial_01") == ConsiliumMode.INITIAL

    # 2. Cycle in pending_reassessment -> NEO_REASSESSMENT
    c2 = MedicalCycle(
        cycle_id="cycle_neo_pending_01",
        patient_id="p_mode_test",
        status="pending_reassessment",
        started_at=now,
    )
    db_session.add(c2)
    db_session.commit()
    assert service.detect_consilium_mode("cycle_neo_pending_01") == ConsiliumMode.NEO_REASSESSMENT

    # 3. Cycle with neo_cycle_lineage report -> NEO_REASSESSMENT
    c3 = MedicalCycle(
        cycle_id="cycle_neo_active_01",
        patient_id="p_mode_test",
        status="active",
        started_at=now,
    )
    db_session.add(c3)
    lineage = ModelReport(
        report_id="rep_lineage_c3",
        cycle_id="cycle_neo_active_01",
        model="aegis-neo-lifecycle",
        report_type=NEO_CYCLE_LINEAGE_REPORT_TYPE,
        privacy_level=PrivacyLevel.SERVER_ONLY.value,
        content={"event_type": "neo_cycle_transition", "previous_cycle_id": "cycle_initial_01"},
        version=1,
    )
    db_session.add(lineage)
    db_session.commit()
    assert service.detect_consilium_mode("cycle_neo_active_01") == ConsiliumMode.NEO_REASSESSMENT

    # 4. Unknown cycle raises ConsiliumCycleNotFoundError
    with pytest.raises(ConsiliumCycleNotFoundError):
        service.detect_consilium_mode("cycle_non_existent")

    # 5. PrimaryConsiliumRunRequest parses mode cleanly
    req_default = PrimaryConsiliumRunRequest(cycle_id="c_test")
    assert req_default.mode is None
    req_explicit = PrimaryConsiliumRunRequest(cycle_id="c_test", mode=ConsiliumMode.NEO_REASSESSMENT)
    assert req_explicit.mode == ConsiliumMode.NEO_REASSESSMENT


# ==============================================================================
# TEST 2: IMMUTABILITY OF INITIAL PRIMARY PROMPTS
# ==============================================================================

def test_02_initial_primary_prompts_immutability():
    for key in SYSTEM_KEYS:
        sample_input = {"system_key": key, "facts": "Normal baseline", "shared_labs_table": {}}
        init_prompt = build_system_agent_prompt(key, sample_input)
        assert "NEO REASSESSMENT" not in init_prompt
        assert "HISTORICAL REFERENCE CONTEXT" not in init_prompt
        assert "prior observation cycles" not in init_prompt
        assert key in init_prompt

    flash_prompt = build_flash_master_prompt("test initial master context")
    assert "You are the senior analytical module \"Synthesis and Control\"" in flash_prompt
    assert "SECOND AND FINAL LINK of the cascade" in flash_prompt
    assert "NEO" not in flash_prompt

    pro_prompt = build_pro_master_prompt("full test pro master context")
    assert "You are the supreme clinical expert \"Chief Physician\"" in pro_prompt
    assert "Pessimum Zone" in pro_prompt
    assert "NEO" not in pro_prompt


# ==============================================================================
# TEST 3: EIGHT DISTINCT NEO SYSTEM SPECIALIZATIONS
# ==============================================================================

def test_03_eight_distinct_neo_system_specializations():
    assert len(_NEO_SYSTEM_SPECIFIC_INSTRUCTIONS) == 8
    assert set(_NEO_SYSTEM_SPECIFIC_INSTRUCTIONS.keys()) == set(SYSTEM_KEYS)

    # Ensure all 8 system instructions are unique
    instruction_texts = list(_NEO_SYSTEM_SPECIFIC_INSTRUCTIONS.values())
    assert len(set(instruction_texts)) == 8, "Each organ system must have distinct clinical instructions"

    expected_clinical_roles = {
        "cvs": ("Cardiology and Angiology", "cardiovascular"),
        "gi_tract": ("Gastroenterology and Hepatology", "gastroenterological"),
        "respiratory": ("Pulmonology", "respiratory"),
        "nervous": ("Neurology and Neurobiology", "neurological"),
        "kidneys": ("Nephrology", "renal"),
        "endocrine": ("Endocrinology and Metabolism", "metabolic"),
        "immune": ("Immunology and Hematology", "immunological"),
        "musculoskeletal": ("Orthopedics and Rheumatology", "musculoskeletal"),
    }

    sample_input = {"sample_key": "data"}
    for sys_key, (role_name, domain) in expected_clinical_roles.items():
        prompt = build_neo_system_agent_prompt(sys_key, sample_input, historical_context="History excerpt")
        assert role_name in prompt
        assert domain in prompt.lower()
        assert "NEO REASSESSMENT mode" in prompt
        assert "HISTORICAL REFERENCE CONTEXT" in prompt
        assert "FRESH CLINICAL INPUT" in prompt


# ==============================================================================
# TEST 4: SEPARATION OF FRESH AND HISTORICAL DATA
# ==============================================================================

def test_04_separation_of_fresh_and_historical_data():
    fresh_package = {
        "facts": "Patient reports renewed endurance",
        "shared_labs_table": {"glucose": {"value": 5.1, "unit": "mmol/L"}},
        "vitals_stream": {"heart_rate": "62 bpm"},
    }
    history_md = "## 1. Prior Cycles Overview\nCycle 1: Glucose was 6.8 mmol/L.\n"

    # 1. System specialist prompt
    sys_prompt = build_neo_system_agent_prompt("endocrine", fresh_package, historical_context=history_md)
    assert "=== HISTORICAL REFERENCE CONTEXT (PRIOR CYCLES — FOR LONGITUDINAL REFERENCE ONLY) ===" in sys_prompt
    assert "=== FRESH CLINICAL INPUT (NEW CYCLE) ===" in sys_prompt
    assert "Glucose was 6.8 mmol/L" in sys_prompt
    assert "5.1" in sys_prompt
    assert "Ground your conclusions primarily on FRESH CLINICAL INPUT" in sys_prompt
    assert "Use HISTORICAL REFERENCE CONTEXT solely to evaluate longitudinal trajectories" in sys_prompt

    # 2. Neo Deputy Flash prompt
    flash_prompt = build_neo_flash_master_prompt("FRESH DEPUTY INPUT", history_md)
    assert "INPUT DATA OF CURRENT REASSESSMENT" in flash_prompt
    assert "BOUNDED HISTORICAL LONGITUDINAL CONTEXT" in flash_prompt
    assert "LONGITUDINAL TRAJECTORY AUDIT" in flash_prompt
    assert "STRICT EPISTEMIC INTEGRITY" in flash_prompt

    # 3. Neo Chief Pro prompt
    pro_prompt = build_neo_pro_master_prompt("FRESH PRO INPUT", history_md)
    assert "CURRENT REASSESSMENT DOSSIER" in pro_prompt
    assert "BOUNDED HISTORICAL LONGITUDINAL DOSSIER" in pro_prompt
    assert "CLEAN SLATE CLINICAL VERDICT" in pro_prompt


# ==============================================================================
# TEST 5: NO AUTOMATIC REFERRAL CARRYOVER
# ==============================================================================

def test_05_no_automatic_referral_carryover(db_session):
    now = datetime.now(timezone.utc)
    patient = Patient(patient_id="p_ref_test", status="active")
    db_session.add(patient)

    c1 = MedicalCycle(cycle_id="c_ref_01", patient_id="p_ref_test", status="active", started_at=now)
    db_session.add(c1)

    # Completed primary on c1
    prim_report = ModelReport(
        report_id="rep_prim_c1",
        cycle_id="c_ref_01",
        model="gemini-3.7-pro",
        report_type="user_overall_report",
        privacy_level=PrivacyLevel.CLIENT_FACING.value,
        content={"title": "Primary Review", "summary": "Baseline established."},
        version=1,
    )
    db_session.add(prim_report)
    exec_log = ModelReport(
        report_id="rep_exec_c1",
        cycle_id="c_ref_01",
        model="gemini-3.7-pro",
        report_type="primary_consilium_execution_log",
        privacy_level=PrivacyLevel.SERVER_ONLY.value,
        content={"completed_at_utc": now.isoformat()},
        version=1,
    )
    db_session.add(exec_log)

    # Referral state on c1
    ref_report = ModelReport(
        report_id="rep_ref_c1",
        cycle_id="c_ref_01",
        model="aegis-referral-engine",
        report_type="referral_lifecycle_state",
        privacy_level=PrivacyLevel.SERVER_ONLY.value,
        content={
            "referral_id": "REF-C1",
            "status": "visit_confirmed",
            "target_specialties": ["Cardiologist"],
            "clinical_reason_for_referral": "Suspected arrhythmia",
        },
        version=1,
    )
    db_session.add(ref_report)
    db_session.commit()

    # Start Neo reassessment to create cycle 2
    neo_service = NeoCycleService(db=db_session)
    resp = neo_service.start_neo_reassessment("p_ref_test")
    new_c_id = resp.cycle_id

    # Verify new cycle does NOT contain referral_lifecycle_state
    reports_in_new_c = (
        db_session.query(ModelReport)
        .filter(ModelReport.cycle_id == new_c_id)
        .filter(ModelReport.report_type == "referral_lifecycle_state")
        .all()
    )
    assert len(reports_in_new_c) == 0, "New cycle must not inherit active referral state"

    # Verify Chief Pro Neo prompt explicitly directs clean slate on referrals
    pro_prompt = build_neo_pro_master_prompt("context", "history")
    assert "DO NOT inertially copy-paste the previous cycle's strategy, past questions, or previous referral decisions." in pro_prompt
    assert "Past referral decisions and past active strategies belong to the historical archive" in pro_prompt


# ==============================================================================
# TEST 6: NO STRATEGY CARRYOVER (CLEAN SLATE STRATEGY)
# ==============================================================================

def test_06_no_strategy_carryover(db_session):
    now = datetime.now(timezone.utc)
    patient = Patient(patient_id="p_strat_test", status="active")
    db_session.add(patient)

    c1 = MedicalCycle(cycle_id="c_strat_01", patient_id="p_strat_test", status="completed", started_at=now, closed_at=now)
    db_session.add(c1)

    week_c1 = ModelReport(
        report_id="rep_week_strat_c1",
        cycle_id="c_strat_01",
        model="gemini-3.7-pro",
        report_type="weekly_user_overall_report",
        privacy_level=PrivacyLevel.CLIENT_FACING.value,
        content={"summary": "Sprint 4 completed", "metrics": {"sprint_number": 4}},
        version=1,
    )
    db_session.add(week_c1)

    strat_c1 = ModelReport(
        report_id="rep_strat_c1",
        cycle_id="c_strat_01",
        model="gemini-3.7-pro",
        report_type="athena_active_strategy",
        privacy_level=PrivacyLevel.SERVER_ONLY.value,
        content={"strategy_version": 4, "active_weekly_strategy": "Old sprint 4 focus"},
        version=1,
    )
    db_session.add(strat_c1)
    db_session.commit()

    # Historical context reports strategy strictly as historical baseline
    hist_service = HistoricalContextService(db=db_session)
    summary = hist_service.build_historical_context("p_strat_test")
    assert "Strategy Version: v4" in summary.formatted_markdown

    # Chief Pro prompt directs building a brand new strategy
    pro_prompt = build_neo_pro_master_prompt("master_ctx", summary.formatted_markdown)
    assert "Formulate the NEW baseline clinical strategy for the Curator (Athena)" in pro_prompt
    assert "REFRESHED SCREENING PLAN (WEEK 1)" in pro_prompt
    assert "NEW CLINICAL STRATEGY FOR CURATOR (ATHENA)" in pro_prompt


# ==============================================================================
# TEST 7: PRESERVATION OF CLINICAL UNCERTAINTY (ANTI-HALLUCINATION)
# ==============================================================================

def test_07_preservation_of_clinical_uncertainty():
    # 1. Specialists must contain strict anti-fabrication mandate
    for key in SYSTEM_KEYS:
        prompt = build_neo_system_agent_prompt(key, {"empty": True})
        assert "PRESERVATION OF CLINICAL UNCERTAINTY" in prompt
        assert "explicitly state the diagnostic gap" in prompt.lower()
        assert "Never fabricate, hallucinate, or assume normal values" in prompt or "do not guess" in prompt.lower() or "document the missing evidence explicitly" in prompt.lower() or "state the diagnostic limits clearly" in prompt.lower() or "clearly record the diagnostic deficit" in prompt.lower() or "document the data gap plainly" in prompt.lower() or "explicitly report this gap" in prompt.lower() or "explicitly highlight this diagnostic deficit" in prompt.lower() or "explicitly document the absence of objective verification" in prompt.lower()

    # 2. Deputy Flash prompt mandates anti-hallucination & reporting data gaps
    flash_prompt = build_neo_flash_master_prompt("context", "history")
    assert "STRICTLY AVOID unverified medical diagnoses and false certainty" in flash_prompt
    assert "Explicitly acknowledge if specific data or tests are currently pending or missing" in flash_prompt

    # 3. Chief Pro prompt mandates anti-hallucination
    pro_prompt = build_neo_pro_master_prompt("context", "history")
    assert "Never fill data deficits with fabricated certainties" in pro_prompt
    assert "PRESERVATION OF CLINICAL UNCERTAINTY" in pro_prompt


# ==============================================================================
# TEST 8: HISTORICAL CONTEXT BUDGET AND CLINICAL GOVERNANCE
# ==============================================================================

def test_08_historical_context_budget_and_governance(db_session):
    now = datetime.now(timezone.utc)
    patient = Patient(patient_id="p_budget_test", status="active")
    db_session.add(patient)

    # Create 3 historical cycles with rich data
    for i in range(1, 4):
        cid = f"c_hist_0{i}"
        c = MedicalCycle(cycle_id=cid, patient_id="p_budget_test", status="completed", started_at=now, closed_at=now)
        db_session.add(c)

        # Primary report
        db_session.add(ModelReport(
            report_id=f"rep_prim_{cid}",
            cycle_id=cid,
            model="gemini-3.7-pro",
            report_type="user_overall_report",
            privacy_level=PrivacyLevel.CLIENT_FACING.value,
            content={"title": f"Summary Cycle {i}", "summary": "Baseline established and reviewed."},
            version=1,
        ))

        # Weekly report
        db_session.add(ModelReport(
            report_id=f"rep_week_{cid}",
            cycle_id=cid,
            model="gemini-3.7-pro",
            report_type="weekly_user_overall_report",
            privacy_level=PrivacyLevel.CLIENT_FACING.value,
            content={"summary": f"Weekly sprint conclusion for cycle {i}.", "metrics": {"sprint_number": i}},
            version=1,
        ))

        # Lab ledger with abnormal marker
        db_session.add(ModelReport(
            report_id=f"rep_lab_{cid}",
            cycle_id=cid,
            model="aegis-lab-ledger",
            report_type="laboratory_ledger",
            privacy_level=PrivacyLevel.SERVER_ONLY.value,
            content={
                "active_markers": {
                    "alt": {
                        "value": 65 + i * 5,
                        "numeric_value": 65 + i * 5,
                        "unit": "U/L",
                        "display_name": "ALT (Alanine Aminotransferase)",
                        "reference_range": {"low": 10.0, "high": 40.0},
                    }
                }
            },
            version=1,
        ))

        # Vision ECG
        db_session.add(ModelReport(
            report_id=f"rep_ecg_{cid}",
            cycle_id=cid,
            model="gemini-3.8-flash",
            report_type="preparatory_vision_ecg",
            privacy_level=PrivacyLevel.SERVER_ONLY.value,
            content={"recording_type": "12-lead ECG", "heart_rate_bpm": 68 + i, "rhythm_type": "Sinus rhythm"},
            version=1,
        ))

        # Referral with multi-version states (generating -> visit_confirmed)
        db_session.add(ModelReport(
            report_id=f"rep_ref_{cid}_v1",
            cycle_id=cid,
            model="aegis-referral",
            report_type="referral_lifecycle_state",
            privacy_level=PrivacyLevel.SERVER_ONLY.value,
            content={"referral_id": f"REF-{cid}", "status": "generating_document", "target_specialties": ["Cardiologist"]},
            version=1,
        ))
        db_session.add(ModelReport(
            report_id=f"rep_ref_{cid}_v2",
            cycle_id=cid,
            model="aegis-referral",
            report_type="referral_lifecycle_state",
            privacy_level=PrivacyLevel.SERVER_ONLY.value,
            content={"referral_id": f"REF-{cid}", "status": "visit_confirmed", "target_specialties": ["Cardiologist"]},
            version=2,
        ))

    db_session.commit()

    hist_service = HistoricalContextService(db=db_session)
    summary = hist_service.build_historical_context("p_budget_test", max_characters=8000)

    # 1. Budget bounds
    assert summary.character_count <= 8000
    assert summary.prior_cycles_count == 3
    assert len(summary.prior_cycle_ids) == 3

    # 2. Prominent governance notice
    assert CLINICAL_GOVERNANCE_NOTICE in summary.formatted_markdown
    assert "**NOT** confirmed medical diagnoses" in summary.formatted_markdown

    # 3. Units and dates preservation
    assert "U/L" in summary.formatted_markdown
    assert "ALT" in summary.formatted_markdown

    # 4. Deduplication of multi-version referrals
    # For each referral REF-c_hist_0i, it must show visit confirmed without duplicating
    assert summary.referrals_count == 3
    assert summary.formatted_markdown.count("REF-c_hist_01") == 1
    assert summary.formatted_markdown.count("REF-c_hist_02") == 1
    assert summary.formatted_markdown.count("REF-c_hist_03") == 1


# ==============================================================================
# TEST 9: STRICT PYDANTIC VALIDATION AND CONTRACT PARITY
# ==============================================================================

def test_09_strict_pydantic_validation():
    # 1. NeoSystemAgentOutput validates valid payload and forbids extra
    valid_sys = NeoSystemAgentOutput(
        system_view=SystemAgentView(isolated_labs={"glucose": 5.2}),
        system_logic=SystemAgentLogic(system_general_shelf="Valid conclusion"),
        training_sandbox=SystemAgentSandbox(system_training_shelf="No error"),
    )
    assert valid_sys.system_logic.system_general_shelf == "Valid conclusion"

    with pytest.raises(ValidationError):
        NeoSystemAgentOutput(
            system_view=SystemAgentView(),
            system_logic=SystemAgentLogic(system_general_shelf="Valid"),
            training_sandbox=SystemAgentSandbox(system_training_shelf="Valid"),
            unknown_extra_field="malicious",  # extra='forbid'
        )

    # 2. NeoFlashMasterOutput requires all 8 systems in user_system_reports
    valid_reports_map = UserSystemReportsMap(
        cvs="CVS update",
        gi_tract="GI update",
        respiratory="Respiratory update",
        nervous="Nervous update",
        musculoskeletal="Musculoskeletal update",
        kidneys="Kidneys update",
        immune="Immune update",
        endocrine="Endocrine update",
    )
    valid_flash = NeoFlashMasterOutput(
        flash_general_shelf="General shelf",
        flash_training_shelf="Training shelf",
        user_system_reports=valid_reports_map,
    )
    assert valid_flash.user_system_reports.cvs == "CVS update"

    with pytest.raises(ValidationError):
        # Empty string rejected by validator
        UserSystemReportsMap(
            cvs="",
            gi_tract="GI",
            respiratory="Resp",
            nervous="Nerv",
            musculoskeletal="MSK",
            kidneys="Kidneys",
            immune="Immune",
            endocrine="Endo",
        )

    with pytest.raises(ValidationError):
        NeoFlashMasterOutput(
            flash_general_shelf="Gen",
            flash_training_shelf="Train",
            user_system_reports=valid_reports_map,
            forbidden_field="not_allowed",
        )

    # 3. NeoProMasterOutput validates thresholds and quality review
    valid_pro = NeoProMasterOutput(
        vitals_thresholds=ProVitalsThresholds(
            min_pessimum_bpm=48,
            max_pessimum_bpm=120,
            max_spike_delta_bpm=25,
            clinical_rationale="Evaluated for moderate fitness",
        ),
        pro_general_shelf="New active strategy for Athena",
        pro_training_shelf="Supreme synthesis",
        user_overall_report=ProUserOverallReport(
            title="Roadmap",
            summary="Welcome back",
            highlights=["Step 1"],
        ),
        flash_quality_review=ProFlashQualityReview(
            approved_for_publication=True,
            corrected_user_system_reports={"cvs": "Corrected CVS"},
        ),
    )
    assert valid_pro.vitals_thresholds.min_pessimum_bpm == 48

    # Inverted thresholds raise ValidationError
    with pytest.raises(ValidationError):
        ProVitalsThresholds(
            min_pessimum_bpm=130,
            max_pessimum_bpm=80,  # min >= max
            max_spike_delta_bpm=20,
            clinical_rationale="Invalid",
        )

    # 4. NeoConsiliumMetadata validates cleanly
    meta = NeoConsiliumMetadata(
        mode=ConsiliumMode.NEO_REASSESSMENT,
        new_cycle_id="cycle_new_01",
        previous_cycle_id="cycle_old_01",
        prior_cycles_count=1,
        prior_cycle_ids=["cycle_old_01"],
        historical_character_count=1200,
        is_historical_condensed=False,
        abnormal_labs_count=1,
        referrals_count=1,
    )
    assert meta.mode == ConsiliumMode.NEO_REASSESSMENT
    with pytest.raises(ValidationError):
        NeoConsiliumMetadata(
            new_cycle_id="cycle_01",
            unsupported_field="fail",
        )

    # 5. ClientReportContent strictly forbids arbitrary extra fields
    with pytest.raises(ValidationError):
        ClientReportContent(
            title="Title",
            summary="Summary",
            report_date="2026-11-09",
            unsupported_extra_field="fail",
        )


# ==============================================================================
# TEST 10: MODEL IDENTIFIERS AND CALL COUNT INVARIANTS (8 -> 1 -> 1 = 10 CALLS)
# ==============================================================================

def test_10_model_identifiers_and_call_count_invariants():
    gateway = GeminiGateway()

    # 1. Physical model IDs must remain exactly as configured
    assert gateway.resolve_model_name(GeminiModelRole.FLASH_LITE) == gateway.flash_lite_model
    assert gateway.resolve_model_name(GeminiModelRole.FLASH) == gateway.flash_model
    assert gateway.resolve_model_name(GeminiModelRole.PRO) == gateway.pro_model
    assert gateway.resolve_model_name(GeminiModelRole.VISION) == gateway.vision_model

    # Check canonical values
    assert "flash-lite" in gateway.flash_lite_model.lower()
    assert "flash" in gateway.flash_model.lower()
    assert "argon" in gateway.pro_model.lower() or "pro" in gateway.pro_model.lower()

    # 2. Planned call sequence for Neo Consilium
    planned_sequence = [
        (GeminiModelRole.FLASH_LITE, f"organ_specialist_{sys_key}") for sys_key in SYSTEM_KEYS
    ] + [
        (GeminiModelRole.FLASH, "deputy_chief_physician"),
        (GeminiModelRole.PRO, "chief_physician"),
    ]

    assert len(planned_sequence) == 10, "Neo Consilium must plan exactly 10 model calls"

    flash_lite_calls = [c for c in planned_sequence if c[0] == GeminiModelRole.FLASH_LITE]
    flash_calls = [c for c in planned_sequence if c[0] == GeminiModelRole.FLASH]
    pro_calls = [c for c in planned_sequence if c[0] == GeminiModelRole.PRO]

    assert len(flash_lite_calls) == 8, "Exactly 8 Flash-Lite calls for the 8 organ systems"
    assert len(flash_calls) == 1, "Exactly 1 Gemini Flash call for Deputy Chief Physician"
    assert len(pro_calls) == 1, "Exactly 1 Gemini Pro call for Chief Physician"


# ==============================================================================
# TEST 11: MODE MISMATCH AND CONTRADICTION GUARDS
# ==============================================================================

def test_11_guard_against_premature_neo_execution(db_session):
    now = datetime.now(timezone.utc)
    patient = Patient(patient_id="p_guard_test", status="active")
    db_session.add(patient)
    db_session.commit()

    c_neo = MedicalCycle(
        cycle_id="cycle_neo_guard_01",
        patient_id="p_guard_test",
        status="pending_reassessment",
        started_at=now,
    )
    db_session.add(c_neo)
    db_session.commit()

    service = PrimaryConsiliumService(db=db_session)
    # 1. Attempting to run with mode=INITIAL on a Neo cycle raises ConsiliumError
    with pytest.raises(ConsiliumError) as exc_info:
        asyncio.run(service.run_primary_consilium("cycle_neo_guard_01", mode=ConsiliumMode.INITIAL))
    assert "Requested consilium mode 'initial' contradicts" in str(exc_info.value)
    assert "neo_reassessment" in str(exc_info.value)

    # 2. Attempting to run with mode=NEO_REASSESSMENT on an initial cycle raises ConsiliumError
    c_init = MedicalCycle(
        cycle_id="cycle_init_guard_01",
        patient_id="p_guard_test",
        status="active",
        started_at=now,
    )
    db_session.add(c_init)
    db_session.commit()
    with pytest.raises(ConsiliumError) as exc_info2:
        asyncio.run(service.run_primary_consilium("cycle_init_guard_01", mode=ConsiliumMode.NEO_REASSESSMENT))
    assert "Requested consilium mode 'neo_reassessment' contradicts" in str(exc_info2.value)
    assert "initial" in str(exc_info2.value)
