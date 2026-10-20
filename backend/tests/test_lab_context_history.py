"""
PROJECT AEGIS — C-1 / C-2 TEST SUITE
Verification of Laboratory Context & Chronological Observations History in Consiliums

Tests:
1. Multi-document OCR intake: Primary Consilium receives markers from multiple preparatory documents (C-1).
2. Multi-date measurements: Multiple dated studies of the same marker are all preserved in laboratory_history (C-2).
3. Cascade dissemination: All 8 Flash-Lite specialists receive the identical full laboratory_history in input_package.
4. Deep cascade prompt assertions: Detailed assertions on prompts for 8 Flash-Lite, Deputy Flash, and Chief Pro.
5. PII sanitization: Strips patient_name, patient_id, dob, doctor, hospital, raw_image_url from shared_labs_table and history.
6. OCR fallback non-speculative event_type: event_type is not guessed, is_synthetic_fallback=True, ledger_version=0.
7. Weekly Consilium evaluated vs unreviewed labs: Clearly separates evaluated shared_labs_shelf from unreviewed_pending_labs.
8. Error corrections vs dated studies: Inactive/superseded observations remain in history with is_active=False.
9. Fallback / missing records: Empty database cycle returns empty context gracefully without error or commit/rollback.
10. Security / tamper-proofing: Tampered agent_role on preparatory OCR report raises ConsiliumContextMissingError.
"""

import asyncio
from datetime import datetime, timezone
import json
from typing import Any, Dict, List, Optional
import pytest
from sqlalchemy.orm import Session

from app.models.model_report import ModelReport
from app.repositories.cycle_repository import MedicalCycleRepository
from app.repositories.report_repository import ModelReportRepository
from app.schemas.privacy import PrivacyLevel
from app.schemas.consilium import (
    SYSTEM_KEYS,
    AegisAgentRole,
    AnamnesisModelOutput,
    LabEventType,
    LabMarkerMutationItem,
    LabReferenceRange,
    SystemAgentOutput,
    FlashMasterOutput,
    ProMasterOutput,
)
from app.services.gemini_gateway import GeminiGateway, GeminiModelRole
from app.services.lab_context_service import (
    LABORATORY_LEDGER_REPORT_TYPE,
    PREPARATORY_OCR_REPORT_TYPE,
    assemble_consilium_laboratory_context,
)
from app.services.lab_revision_consilium_service import LabRevisionConsiliumService
from app.services.primary_consilium_service import (
    PREPARATORY_ANAMNESIS_REPORT_TYPE,
    PREPARATORY_ECG_REPORT_TYPE,
    ConsiliumContextMissingError,
    PrimaryConsiliumService,
)
from app.services.weekly_consilium_service import WeeklyConsiliumService


def _sample_anamnesis_output() -> Dict[str, Any]:
    return {
        "agent_role": AegisAgentRole.ANAMNESIS_M1.value,
        "user_view": {
            "summary": "Primary anamnesis processed across 8 systems in blind mode."
        },
        "system_logic": {
            key: {
                "data": f"{key.upper()} anamnesis facts: mild fatigue and sedentary work",
                "internal_comment": f"M1 blind comment for {key}",
            }
            for key in SYSTEM_KEYS
        },
        "training_sandbox": {
            "raw_diagnostic_hypothesis": "Autonomic fatigue with sedentary cardiovascular deconditioning",
            "data_gap_analysis": "Need lipid profile, ferritin, TSH, and resting ECG",
            "error_probability": "Low (15%)",
        },
    }


def _build_primary_mock_gateway(recorded_calls: List[Dict[str, Any]]) -> GeminiGateway:
    class _MockGateway(GeminiGateway):
        async def generate_structured(
            self,
            prompt: str,
            response_schema: Any,
            model: GeminiModelRole,
            temperature: float = 0.2,
            max_output_tokens: Optional[int] = None,
        ) -> Any:
            recorded_calls.append({
                "prompt": prompt,
                "schema": response_schema,
                "model": model,
            })
            if response_schema is SystemAgentOutput:
                return SystemAgentOutput.model_validate({
                    "system_view": {"isolated_labs": {}},
                    "system_logic": {"system_general_shelf": f"Organ conclusion for {model.value}"},
                    "training_sandbox": {"system_training_shelf": "Recommend monitoring"},
                })
            elif response_schema is FlashMasterOutput:
                return FlashMasterOutput.model_validate({
                    "flash_general_shelf": "Flash synthesis conclusion: Hepatic transaminase trajectory indicates steatohepatitis.",
                    "flash_training_shelf": "Flash training recommendations: Ultrasound and lipid subfractions required.",
                    "user_system_reports": {k: f"Draft user report for {k}" for k in SYSTEM_KEYS},
                })
            elif response_schema is ProMasterOutput:
                return ProMasterOutput.model_validate({
                    "pro_general_shelf": "Chief Physician comprehensive clinical strategy",
                    "pro_training_shelf": "Chief Physician longitudinal training trajectory",
                    "user_overall_report": {
                        "title": "Comprehensive Health Assessment",
                        "summary": "Clinical overview",
                        "highlights": ["Routine aerobic conditioning"],
                    },
                    "vitals_thresholds": {
                        "min_pessimum_bpm": 48,
                        "max_pessimum_bpm": 118,
                        "max_spike_delta_bpm": 28,
                        "clinical_rationale": "Resting baseline normal",
                    },
                    "flash_quality_review": {
                        "approved_for_publication": True,
                        "rejection_reason": None,
                        "corrected_user_system_reports": {},
                    },
                })
            raise ValueError(f"Unexpected schema: {response_schema}")

    return _MockGateway()


def _seed_base_anamnesis_and_ecg(db: Session, cycle_id: str):
    report_repo = ModelReportRepository(db)
    report_repo.create_next_version(
        cycle_id=cycle_id,
        model="gemini-3.6-flash",
        report_type=PREPARATORY_ANAMNESIS_REPORT_TYPE,
        privacy_level=PrivacyLevel.SERVER_ONLY,
        content=_sample_anamnesis_output(),
    )
    report_repo.create_next_version(
        cycle_id=cycle_id,
        model="gemini-3.6-flash",
        report_type=PREPARATORY_ECG_REPORT_TYPE,
        privacy_level=PrivacyLevel.SERVER_ONLY,
        content={
            "agent_role": AegisAgentRole.VISION_ECG.value,
            "ecg_summary": "Sinus rhythm, HR 72 bpm, normal axis",
        },
    )


# ==============================================================================
# TESTS
# ==============================================================================

@pytest.mark.anyio
async def test_c1_multi_document_ocr_intake_combined_in_primary(db_session: Session):
    """
    C-1 Verification:
    Two separate laboratory documents uploaded in the same cycle must BOTH be loaded
    into Primary Consilium rather than only the latest single preparatory_vision_ocr.
    """
    cycle_id = "cycle_c1_multi_doc"
    cycle_repo = MedicalCycleRepository(db_session)
    cycle_repo.create(cycle_id=cycle_id, status="active")
    _seed_base_anamnesis_and_ecg(db_session, cycle_id)

    report_repo = ModelReportRepository(db_session)
    # Document 1 (Biochemistry: ALT, AST)
    report_repo.create_next_version(
        cycle_id=cycle_id,
        model="gemini-3.8-flash",
        report_type=PREPARATORY_OCR_REPORT_TYPE,
        privacy_level=PrivacyLevel.SERVER_ONLY,
        content={
            "agent_role": AegisAgentRole.VISION_OCR.value,
            "confirmed_observations": [
                {
                    "marker_key": "alt",
                    "display_name": "ALT",
                    "value": "24 U/L",
                    "numeric_value": 24.0,
                    "unit": "U/L",
                    "sample_date": "2026-10-10",
                },
                {
                    "marker_key": "ast",
                    "display_name": "AST",
                    "value": "26 U/L",
                    "numeric_value": 26.0,
                    "unit": "U/L",
                    "sample_date": "2026-10-10",
                },
            ],
            "shared_labs_table": {
                "alt": {"value": "24 U/L", "numeric_value": 24.0, "unit": "U/L", "sample_date": "2026-10-10"},
                "ast": {"value": "26 U/L", "numeric_value": 26.0, "unit": "U/L", "sample_date": "2026-10-10"},
            },
        },
    )
    # Document 2 (Endocrinology: Glucose, TSH)
    report_repo.create_next_version(
        cycle_id=cycle_id,
        model="gemini-3.8-flash",
        report_type=PREPARATORY_OCR_REPORT_TYPE,
        privacy_level=PrivacyLevel.SERVER_ONLY,
        content={
            "agent_role": AegisAgentRole.VISION_OCR.value,
            "confirmed_observations": [
                {
                    "marker_key": "glucose",
                    "display_name": "Glucose",
                    "value": "4.8 mmol/L",
                    "numeric_value": 4.8,
                    "unit": "mmol/L",
                    "sample_date": "2026-10-12",
                },
                {
                    "marker_key": "tsh",
                    "display_name": "TSH",
                    "value": "2.1 mIU/L",
                    "numeric_value": 2.1,
                    "unit": "mIU/L",
                    "sample_date": "2026-10-12",
                },
            ],
            "shared_labs_table": {
                "glucose": {"value": "4.8 mmol/L", "numeric_value": 4.8, "unit": "mmol/L", "sample_date": "2026-10-12"},
                "tsh": {"value": "2.1 mIU/L", "numeric_value": 2.1, "unit": "mIU/L", "sample_date": "2026-10-12"},
            },
        },
    )

    calls: List[Dict[str, Any]] = []
    gateway = _build_primary_mock_gateway(calls)
    service = PrimaryConsiliumService(db=db_session, gateway=gateway)
    await service.run_primary_consilium(cycle_id=cycle_id)

    # 8 organ specialists + 1 flash + 1 pro = 10 calls
    assert len(calls) == 10

    # Verify specialists received markers from BOTH documents in their input_package
    first_specialist_prompt = calls[0]["prompt"]
    assert "alt" in first_specialist_prompt
    assert "ast" in first_specialist_prompt
    assert "glucose" in first_specialist_prompt
    assert "tsh" in first_specialist_prompt

    # Verify Deputy Flash received all markers
    flash_prompt = calls[8]["prompt"]
    assert "alt" in flash_prompt
    assert "glucose" in flash_prompt

    # Verify Chief Pro received all markers
    pro_prompt = calls[9]["prompt"]
    assert "alt" in pro_prompt
    assert "glucose" in pro_prompt


@pytest.mark.anyio
async def test_c2_multiple_dated_measurements_of_same_marker_preserved(db_session: Session):
    """
    C-2 Verification:
    Three separate measurements of 'glucose' over different sample dates (2026-01-10, 2026-04-10, 2026-10-10)
    must ALL be preserved in laboratory_history rather than overwritten by marker_key in shared_labs_table.
    """
    cycle_id = "cycle_c2_multi_date"
    cycle_repo = MedicalCycleRepository(db_session)
    cycle_repo.create(cycle_id=cycle_id, status="active")
    _seed_base_anamnesis_and_ecg(db_session, cycle_id)

    rev_service = LabRevisionConsiliumService(db=db_session)
    rev_service.upsert_laboratory_markers(
        cycle_id=cycle_id,
        mutations=[
            LabMarkerMutationItem(
                marker_key="glucose",
                display_name="Fasting Glucose",
                value="6.2 mmol/L",
                unit="mmol/L",
                sample_date="2026-01-10",
                event_type=LabEventType.INITIAL,
            ),
            LabMarkerMutationItem(
                marker_key="glucose",
                display_name="Fasting Glucose",
                value="5.8 mmol/L",
                unit="mmol/L",
                sample_date="2026-04-10",
                event_type=LabEventType.NEW_STUDY,
            ),
            LabMarkerMutationItem(
                marker_key="glucose",
                display_name="Fasting Glucose",
                value="5.1 mmol/L",
                unit="mmol/L",
                sample_date="2026-10-10",
                event_type=LabEventType.NEW_STUDY,
            ),
        ],
    )

    ctx = assemble_consilium_laboratory_context(db_session, cycle_id)
    assert ctx.ledger_version == 1

    # shared_labs_table contains the active (latest) marker
    assert "glucose" in ctx.shared_labs_table
    assert ctx.shared_labs_table["glucose"]["value"] == "5.1 mmol/L"

    # laboratory_history preserves ALL 3 dated measurements in chronological order
    glucose_history = [obs for obs in ctx.laboratory_history if obs.get("marker_key") == "glucose"]
    assert len(glucose_history) == 3
    dates = [obs.get("sample_date") for obs in glucose_history]
    values = [obs.get("value") for obs in glucose_history]
    assert dates == ["2026-01-10", "2026-04-10", "2026-10-10"]
    assert values == ["6.2 mmol/L", "5.8 mmol/L", "5.1 mmol/L"]


@pytest.mark.anyio
async def test_c1_c2_cascade_dissemination_deep_prompt_assertions(db_session: Session):
    """
    Point 4 Structural Cascade Verification:
    Asserts exact prompt structure for all 8 Flash-Lite specialists, Deputy Flash, and Chief Pro.
    With 3 dated observations of 'alt' (45.0, 85.0, 120.0 across 3 dates):
    - All 8 specialists see exact values, dates, observation counts, and M1 anamnesis comments in input_package.
    - Deputy Flash sees exact values, dates, active shelf, and 8 specialist conclusions.
    - Chief Pro sees full context + Flash synthesis + 8 draft user system reports.
    """
    cycle_id = "cycle_cascade_deep_assertions"
    cycle_repo = MedicalCycleRepository(db_session)
    cycle_repo.create(cycle_id=cycle_id, status="active")
    _seed_base_anamnesis_and_ecg(db_session, cycle_id)

    rev_service = LabRevisionConsiliumService(db=db_session)
    rev_service.upsert_laboratory_markers(
        cycle_id=cycle_id,
        mutations=[
            LabMarkerMutationItem(
                marker_key="alt",
                display_name="ALT",
                value="45.0 U/L",
                unit="U/L",
                sample_date="2026-03-01",
                event_type=LabEventType.INITIAL,
            ),
            LabMarkerMutationItem(
                marker_key="alt",
                display_name="ALT",
                value="85.0 U/L",
                unit="U/L",
                sample_date="2026-04-01",
                event_type=LabEventType.NEW_STUDY,
            ),
            LabMarkerMutationItem(
                marker_key="alt",
                display_name="ALT",
                value="120.0 U/L",
                unit="U/L",
                sample_date="2026-05-01",
                event_type=LabEventType.NEW_STUDY,
            ),
        ],
    )

    calls: List[Dict[str, Any]] = []
    gateway = _build_primary_mock_gateway(calls)
    service = PrimaryConsiliumService(db=db_session, gateway=gateway)
    await service.run_primary_consilium(cycle_id=cycle_id)

    assert len(calls) == 10

    # 1. Inspect all 8 Flash-Lite specialists (indices 0..7)
    for idx, system_key in enumerate(SYSTEM_KEYS):
        prompt = calls[idx]["prompt"]
        # Extract and parse input_package
        json_marker = "INPUT DATA PACKAGE FOR ANALYSIS"
        assert json_marker in prompt
        raw_json = prompt.split(json_marker)[1].split("):\n")[1].split(
            "\n\nYour job is to generate a strictly valid JSON"
        )[0].strip()
        pkg = json.loads(raw_json)

        assert pkg["system_key"] == system_key
        # M1 first model commentary present
        assert f"M1 blind comment for {system_key}" in pkg["first_model_short_summary"]

        # Shared active labs table contains latest measurement
        assert "alt" in pkg["shared_labs_table"]
        assert pkg["shared_labs_table"]["alt"]["value"] == "120.0 U/L"

        # Complete laboratory_history has exactly 3 dated observations
        history = pkg["laboratory_history"]
        alt_obs = [o for o in history if o.get("marker_key") == "alt"]
        assert len(alt_obs) == 3

        # Exact values and dates present
        assert alt_obs[0]["value"] == "45.0 U/L"
        assert alt_obs[0]["sample_date"] == "2026-03-01"
        assert alt_obs[1]["value"] == "85.0 U/L"
        assert alt_obs[1]["sample_date"] == "2026-04-01"
        assert alt_obs[2]["value"] == "120.0 U/L"
        assert alt_obs[2]["sample_date"] == "2026-05-01"

    # 2. Inspect Deputy Flash (index 8)
    flash_prompt = calls[8]["prompt"]
    assert "INPUT DATA (Anamnesis, Laboratory, Conclusions of 8 system agents):" in flash_prompt
    # All 8 organ system conclusions present
    for key in SYSTEM_KEYS:
        assert f"--- BLOCK: {key.upper()} ---" in flash_prompt
        assert f"Main conclusion: Organ conclusion for {GeminiModelRole.FLASH_LITE.value}" in flash_prompt

    # Multi-date ALT trajectory present in Flash prompt
    assert "45.0 U/L" in flash_prompt
    assert "85.0 U/L" in flash_prompt
    assert "120.0 U/L" in flash_prompt
    assert "2026-03-01" in flash_prompt
    assert "2026-04-01" in flash_prompt
    assert "2026-05-01" in flash_prompt

    # 3. Inspect Chief Pro (index 9)
    pro_prompt = calls[9]["prompt"]
    assert "FULL PATIENT DOSSIER AND BOARD PROGRESS:" in pro_prompt
    # Flash synthesis present
    assert "=== SYNTHESIS (GEMINI FLASH) ===" in pro_prompt
    assert "Hepatic transaminase trajectory indicates steatohepatitis" in pro_prompt
    # Draft 8 user system reports from Flash present
    assert "=== DRAFT 8 USER SYSTEM REPORTS FROM DEPUTY FLASH (FOR PRO QUALITY REVIEW) ===" in pro_prompt
    for key in SYSTEM_KEYS:
        assert f"Draft user report for {key}" in pro_prompt

    # All 3 dates and values also embedded in Pro's dossier
    assert "45.0 U/L" in pro_prompt
    assert "85.0 U/L" in pro_prompt
    assert "120.0 U/L" in pro_prompt


def test_pii_sanitization_strips_hostile_identifiers(db_session: Session):
    """
    Point 3 Sanitization Verification:
    Ensures unexpected PII and non-clinical metadata (patient_name, patient_id, dob, doctor,
    hospital, raw_image_url, debug fields) are strictly stripped from BOTH shared_labs_table
    and laboratory_history, while preserving clinical fields and reference ranges.
    """
    cycle_id = "cycle_pii_sanitization"
    cycle_repo = MedicalCycleRepository(db_session)
    cycle_repo.create(cycle_id=cycle_id, status="active")

    report_repo = ModelReportRepository(db_session)
    # Seed ledger with injected hostile PII in active_markers and observations_history
    report_repo.create_next_version(
        cycle_id=cycle_id,
        model="gemini-3.8-flash",
        report_type=LABORATORY_LEDGER_REPORT_TYPE,
        privacy_level=PrivacyLevel.SERVER_ONLY,
        content={
            "ledger_version": 1,
            "active_markers": {
                "alt": {
                    "marker_key": "alt",
                    "display_name": "ALT",
                    "value": "35 U/L",
                    "numeric_value": 35.0,
                    "unit": "U/L",
                    "sample_date": "2026-10-15",
                    "reference_range": {
                        "low": 0.0,
                        "high": 40.0,
                        "text": "0-40",
                        "doctor_notes": "Dr. Smith noted slight elevation",
                        "clinic_id": "CLINIC-777",
                    },
                    "patient_name": "John Doe",
                    "patient_id": "PID-9999",
                    "dob": "1975-05-12",
                    "doctor": "Dr. Smith",
                    "hospital": "City Central Hospital",
                    "raw_image_url": "https://vault.internal/scans/p9999.jpg",
                    "debug_metadata": {"worker_id": "w-42"},
                }
            },
            "observations_history": [
                {
                    "observation_id": "obs_hostile_1",
                    "marker_key": "alt",
                    "display_name": "ALT",
                    "value": "35 U/L",
                    "numeric_value": 35.0,
                    "unit": "U/L",
                    "sample_date": "2026-10-15",
                    "reference_range": {
                        "low": 0.0,
                        "high": 40.0,
                        "text": "0-40",
                        "leak_token": "TOKEN_SECRET",
                    },
                    "patient_name": "John Doe",
                    "patient_id": "PID-9999",
                    "dob": "1975-05-12",
                    "doctor": "Dr. Smith",
                    "hospital": "City Central Hospital",
                    "raw_image_url": "https://vault.internal/scans/p9999.jpg",
                    "debug_metadata": {"worker_id": "w-42"},
                    "is_active": True,
                }
            ],
        },
    )

    ctx = assemble_consilium_laboratory_context(db_session, cycle_id)
    assert ctx.ledger_version == 1

    forbidden_pii_strings = [
        "John Doe",
        "PID-9999",
        "1975-05-12",
        "Dr. Smith",
        "City Central Hospital",
        "https://vault.internal/scans/p9999.jpg",
        "w-42",
        "TOKEN_SECRET",
        "CLINIC-777",
    ]

    # Check shared_labs_table
    shared_alt = ctx.shared_labs_table["alt"]
    assert shared_alt["value"] == "35 U/L"
    assert shared_alt["numeric_value"] == 35.0
    assert shared_alt["reference_range"] == {"low": 0.0, "high": 40.0, "text": "0-40"}
    for pii in forbidden_pii_strings:
        assert pii not in json.dumps(shared_alt)

    # Check laboratory_history
    history_alt = ctx.laboratory_history[0]
    assert history_alt["value"] == "35 U/L"
    assert history_alt["reference_range"] == {"low": 0.0, "high": 40.0, "text": "0-40"}
    for pii in forbidden_pii_strings:
        assert pii not in json.dumps(history_alt)


def test_ocr_fallback_does_not_speculate_event_type_and_flags_synthetic(db_session: Session):
    """
    Point 2 Non-speculative Fallback Verification:
    When synthesized from OCR reports without an authoritative ledger:
    - is_synthetic_fallback is True, ledger_version is 0, source is 'preparatory_vision_ocr_fallback'.
    - event_type is NOT guessed as 'new_study' or 'initial' when omitted in the OCR source.
    - If event_type is explicitly provided, it is preserved without modification.
    """
    cycle_id = "cycle_ocr_fallback_nospec"
    cycle_repo = MedicalCycleRepository(db_session)
    cycle_repo.create(cycle_id=cycle_id, status="active")

    report_repo = ModelReportRepository(db_session)
    # OCR 1: No event_type provided
    report_repo.create_next_version(
        cycle_id=cycle_id,
        model="gemini-3.8-flash",
        report_type=PREPARATORY_OCR_REPORT_TYPE,
        privacy_level=PrivacyLevel.SERVER_ONLY,
        content={
            "agent_role": AegisAgentRole.VISION_OCR.value,
            "confirmed_observations": [
                {
                    "marker_key": "creatinine",
                    "display_name": "Creatinine",
                    "value": "88 umol/L",
                    "sample_date": "2026-09-01",
                    # event_type omitted
                }
            ],
        },
    )
    # OCR 2: Same marker on a new date, but event_type still omitted in OCR
    report_repo.create_next_version(
        cycle_id=cycle_id,
        model="gemini-3.8-flash",
        report_type=PREPARATORY_OCR_REPORT_TYPE,
        privacy_level=PrivacyLevel.SERVER_ONLY,
        content={
            "agent_role": AegisAgentRole.VISION_OCR.value,
            "confirmed_observations": [
                {
                    "marker_key": "creatinine",
                    "display_name": "Creatinine",
                    "value": "92 umol/L",
                    "sample_date": "2026-10-01",
                    # event_type omitted
                }
            ],
        },
    )

    ctx = assemble_consilium_laboratory_context(db_session, cycle_id)
    assert ctx.is_synthetic_fallback is True
    assert ctx.ledger_version == 0
    assert ctx.source == "preparatory_vision_ocr_fallback"

    assert len(ctx.laboratory_history) == 2
    # Verify neither observation has speculative event_type guessed
    for obs in ctx.laboratory_history:
        assert obs.get("event_type") is None, (
            f"event_type must NOT be guessed as 'new_study' or 'initial' in fallback; got {obs.get('event_type')}"
        )


@pytest.mark.anyio
async def test_weekly_consilium_distinguishes_evaluated_from_unreviewed_pending_labs(db_session: Session):
    """
    Point 1 Weekly Consilium Context Verification:
    When new lab mutations (ledger v2) are added after Primary Consilium (which evaluated v1),
    Weekly Consilium context formulation:
    - Keeps shared_labs_shelf at the evaluated v1 state.
    - Sets has_unreviewed_lab_changes to True.
    - Places the new v2 markers into unreviewed_pending_labs.
    - Provides the complete laboratory_history containing both v1 and v2 measurements.
    """
    cycle_id = "cycle_weekly_evaluated_vs_pending"
    cycle_repo = MedicalCycleRepository(db_session)
    cycle_repo.create(cycle_id=cycle_id, status="active")
    _seed_base_anamnesis_and_ecg(db_session, cycle_id)

    rev_service = LabRevisionConsiliumService(db=db_session)
    # v1: Baseline evaluated by Primary Consilium
    rev_service.upsert_laboratory_markers(
        cycle_id=cycle_id,
        mutations=[
            LabMarkerMutationItem(
                marker_key="cholesterol",
                display_name="Total Cholesterol",
                value="5.2 mmol/L",
                sample_date="2026-09-01",
                event_type=LabEventType.INITIAL,
            ),
        ],
    )

    # Complete Primary Consilium (evaluates v1)
    p_calls: List[Dict[str, Any]] = []
    p_gateway = _build_primary_mock_gateway(p_calls)
    p_service = PrimaryConsiliumService(db=db_session, gateway=p_gateway)
    await p_service.run_primary_consilium(cycle_id=cycle_id)

    # Now add new unreviewed lab markers (ledger v2)
    rev_service.upsert_laboratory_markers(
        cycle_id=cycle_id,
        mutations=[
            LabMarkerMutationItem(
                marker_key="hdl",
                display_name="HDL Cholesterol",
                value="1.4 mmol/L",
                sample_date="2026-10-15",
                event_type=LabEventType.INITIAL,
            ),
            LabMarkerMutationItem(
                marker_key="cholesterol",
                display_name="Total Cholesterol",
                value="6.1 mmol/L",
                sample_date="2026-10-15",
                event_type=LabEventType.NEW_STUDY,
            ),
        ],
    )

    # Check Weekly Consilium context formulation
    weekly_service = WeeklyConsiliumService(db=db_session)
    prev_ctx = weekly_service._load_previous_consilium_context(cycle_id)
    initial_context_str = prev_ctx["initial_context"]

    json_text = initial_context_str.split("=== ANAMNESIS AND INPUT DATA ===\n")[1].split(
        "\n\n=== VITAL MARKERS"
    )[0]
    parsed_input = json.loads(json_text)

    assert parsed_input["evaluated_ledger_version"] == 1
    assert parsed_input["latest_ledger_version"] == 2
    assert parsed_input["has_unreviewed_lab_changes"] is True

    # shared_labs_shelf is strictly the evaluated baseline (v1)
    assert "cholesterol" in parsed_input["shared_labs_shelf"]
    assert parsed_input["shared_labs_shelf"]["cholesterol"]["value"] == "5.2 mmol/L"
    assert "hdl" not in parsed_input["shared_labs_shelf"]

    # unreviewed_pending_labs contains the new/modified v2 markers
    assert "hdl" in parsed_input["unreviewed_pending_labs"]
    assert parsed_input["unreviewed_pending_labs"]["hdl"]["value"] == "1.4 mmol/L"
    assert "cholesterol" in parsed_input["unreviewed_pending_labs"]
    assert parsed_input["unreviewed_pending_labs"]["cholesterol"]["value"] == "6.1 mmol/L"

    # pending_removed_marker_keys is empty since no markers were removed
    assert parsed_input.get("pending_removed_marker_keys") == []

    # Full laboratory_history has all 3 observations
    assert len(parsed_input["laboratory_history"]) == 3


@pytest.mark.anyio
async def test_c2_inactive_and_superseded_observations_flagged_properly(db_session: Session):
    """
    Verifies that when a marker error is corrected, the superseded observation remains
    in laboratory_history with is_active=False and supersedes pointers, allowing models
    to distinguish error corrections from valid dated studies without data loss.
    """
    cycle_id = "cycle_superseded_audit"
    cycle_repo = MedicalCycleRepository(db_session)
    cycle_repo.create(cycle_id=cycle_id, status="active")

    rev_service = LabRevisionConsiliumService(db=db_session)
    # v1: erroneous entry
    v1_state = rev_service.upsert_laboratory_markers(
        cycle_id=cycle_id,
        mutations=[
            LabMarkerMutationItem(
                marker_key="potassium",
                display_name="Potassium",
                value="7.8 mmol/L",
                sample_date="2026-10-18",
                event_type=LabEventType.INITIAL,
            ),
        ],
    )
    v1_obs_id = v1_state.active_markers["potassium"].observation_id

    # v2: correction with explicit reason
    rev_service.upsert_laboratory_markers(
        cycle_id=cycle_id,
        mutations=[
            LabMarkerMutationItem(
                marker_key="potassium",
                display_name="Potassium",
                value="4.5 mmol/L",
                sample_date="2026-10-18",
                event_type=LabEventType.CORRECTION,
                supersedes_observation_id=v1_obs_id,
                correction_reason="Hemolyzed sample artifact corrected via re-draw",
            ),
        ],
    )

    ctx = assemble_consilium_laboratory_context(db_session, cycle_id)
    assert ctx.ledger_version == 2

    # Active table reflects only the corrected value
    assert ctx.shared_labs_table["potassium"]["value"] == "4.5 mmol/L"

    # History preserves both, with clear audit tracking
    k_history = [obs for obs in ctx.laboratory_history if obs.get("marker_key") == "potassium"]
    assert len(k_history) == 2

    erroneous = next(obs for obs in k_history if obs.get("value") == "7.8 mmol/L")
    assert erroneous["is_active"] is False
    assert erroneous["observation_id"] == v1_obs_id

    corrected = next(obs for obs in k_history if obs.get("value") == "4.5 mmol/L")
    assert corrected["is_active"] is True
    assert corrected["supersedes_observation_id"] == v1_obs_id
    assert corrected["correction_reason"] == "Hemolyzed sample artifact corrected via re-draw"
    assert corrected["event_type"] == "correction"


def test_fallback_empty_database_returns_clean_zero_context(db_session: Session):
    """
    Verifies that calling assemble_consilium_laboratory_context on a cycle with zero
    laboratory records returns a clean empty context without raising exceptions.
    """
    ctx = assemble_consilium_laboratory_context(db_session, "nonexistent_cycle")
    assert ctx.ledger_version == 0
    assert ctx.shared_labs_table == {}
    assert ctx.laboratory_history == []
    assert ctx.is_synthetic_fallback is False
    assert ctx.source == "empty"


def test_tampered_agent_role_in_preparatory_ocr_is_rejected(db_session: Session):
    """
    Security check:
    If a preparatory OCR report contains a tampered agent_role, assemble_consilium_laboratory_context
    strictly raises ConsiliumContextMissingError.
    """
    cycle_id = "cycle_tamper_role"
    cycle_repo = MedicalCycleRepository(db_session)
    cycle_repo.create(cycle_id=cycle_id, status="active")

    report_repo = ModelReportRepository(db_session)
    report_repo.create_next_version(
        cycle_id=cycle_id,
        model="gemini-3.8-flash",
        report_type=PREPARATORY_OCR_REPORT_TYPE,
        privacy_level=PrivacyLevel.SERVER_ONLY,
        content={
            "agent_role": "tampered_role",
            "shared_labs_table": {"alt": "30"},
        },
    )

    with pytest.raises(ConsiliumContextMissingError, match="Invalid agent_role"):
        assemble_consilium_laboratory_context(db_session, cycle_id)


@pytest.mark.anyio
async def test_weekly_consilium_new_date_same_value_is_unreviewed(db_session: Session):
    """
    Verification:
    When ledger v1 has glucose = 5.1 (2026-01-10) and is evaluated by Primary,
    and ledger v2 adds a new dated study of glucose = 5.1 (2026-02-10) with identical active value,
    Weekly Consilium context formulation must:
    - Set has_unreviewed_lab_changes to True (ledger version changed, new observation present).
    - Include 'glucose' in unreviewed_pending_labs as an unreviewed change.
    - Preserve shared_labs_shelf at the evaluated v1 state (sample_date 2026-01-10).
    - Contain both dated observations in laboratory_history.
    """
    cycle_id = "cycle_weekly_new_date_same_val"
    cycle_repo = MedicalCycleRepository(db_session)
    cycle_repo.create(cycle_id=cycle_id, status="active")
    _seed_base_anamnesis_and_ecg(db_session, cycle_id)

    rev_service = LabRevisionConsiliumService(db=db_session)
    # v1: Baseline evaluated by Primary Consilium
    rev_service.upsert_laboratory_markers(
        cycle_id=cycle_id,
        mutations=[
            LabMarkerMutationItem(
                marker_key="glucose",
                display_name="Glucose",
                value="5.1 mmol/L",
                sample_date="2026-01-10",
                event_type=LabEventType.INITIAL,
            ),
        ],
    )

    p_calls: List[Dict[str, Any]] = []
    p_gateway = _build_primary_mock_gateway(p_calls)
    p_service = PrimaryConsiliumService(db=db_session, gateway=p_gateway)
    await p_service.run_primary_consilium(cycle_id=cycle_id)

    # v2: Same value (5.1 mmol/L) on a new date (2026-02-10)
    rev_service.upsert_laboratory_markers(
        cycle_id=cycle_id,
        mutations=[
            LabMarkerMutationItem(
                marker_key="glucose",
                display_name="Glucose",
                value="5.1 mmol/L",
                sample_date="2026-02-10",
                event_type=LabEventType.NEW_STUDY,
            ),
        ],
    )

    weekly_service = WeeklyConsiliumService(db=db_session)
    prev_ctx = weekly_service._load_previous_consilium_context(cycle_id)
    initial_context_str = prev_ctx["initial_context"]

    json_text = initial_context_str.split("=== ANAMNESIS AND INPUT DATA ===\n")[1].split(
        "\n\n=== VITAL MARKERS"
    )[0]
    parsed_input = json.loads(json_text)

    assert parsed_input["evaluated_ledger_version"] == 1
    assert parsed_input["latest_ledger_version"] == 2
    assert parsed_input["has_unreviewed_lab_changes"] is True

    # shared_labs_shelf preserves the evaluated v1 state
    assert "glucose" in parsed_input["shared_labs_shelf"]
    assert parsed_input["shared_labs_shelf"]["glucose"]["sample_date"] == "2026-01-10"

    # unreviewed_pending_labs contains glucose since a new observation was recorded
    assert "glucose" in parsed_input["unreviewed_pending_labs"]
    assert parsed_input["unreviewed_pending_labs"]["glucose"]["sample_date"] == "2026-02-10"
    assert parsed_input["unreviewed_pending_labs"]["glucose"]["value"] == "5.1 mmol/L"

    # Both observations are in laboratory_history
    glucose_obs = [obs for obs in parsed_input["laboratory_history"] if obs.get("marker_key") == "glucose"]
    assert len(glucose_obs) == 2
    assert [o.get("sample_date") for o in glucose_obs] == ["2026-01-10", "2026-02-10"]


@pytest.mark.anyio
async def test_weekly_consilium_removed_or_invalidated_marker_detected_in_pending_removed(db_session: Session):
    """
    Verification:
    When a marker present in evaluated v1 (e.g. potassium) is invalidated/removed in v2:
    - has_unreviewed_lab_changes is True.
    - 'potassium' appears in pending_removed_marker_keys.
    - 'potassium' does NOT appear in unreviewed_pending_labs (must not be represented as a new active lab).
    - 'potassium' remains in shared_labs_shelf (preserves evaluated baseline).
    - 'potassium' invalidation is recorded in laboratory_history with is_active=False.
    """
    cycle_id = "cycle_weekly_removed_marker"
    cycle_repo = MedicalCycleRepository(db_session)
    cycle_repo.create(cycle_id=cycle_id, status="active")
    _seed_base_anamnesis_and_ecg(db_session, cycle_id)

    rev_service = LabRevisionConsiliumService(db=db_session)
    # v1: Two active markers
    rev_service.upsert_laboratory_markers(
        cycle_id=cycle_id,
        mutations=[
            LabMarkerMutationItem(
                marker_key="potassium",
                display_name="Potassium",
                value="4.5 mmol/L",
                sample_date="2026-01-10",
                event_type=LabEventType.INITIAL,
            ),
            LabMarkerMutationItem(
                marker_key="sodium",
                display_name="Sodium",
                value="140 mmol/L",
                sample_date="2026-01-10",
                event_type=LabEventType.INITIAL,
            ),
        ],
    )

    p_calls: List[Dict[str, Any]] = []
    p_gateway = _build_primary_mock_gateway(p_calls)
    p_service = PrimaryConsiliumService(db=db_session, gateway=p_gateway)
    await p_service.run_primary_consilium(cycle_id=cycle_id)

    # v2: Invalidate potassium
    rev_service.upsert_laboratory_markers(
        cycle_id=cycle_id,
        mutations=[
            LabMarkerMutationItem(
                marker_key="potassium",
                display_name="Potassium",
                event_type=LabEventType.INVALIDATED,
                correction_reason="Laboratory reported contamination; specimen cancelled",
            ),
        ],
    )

    weekly_service = WeeklyConsiliumService(db=db_session)
    prev_ctx = weekly_service._load_previous_consilium_context(cycle_id)
    initial_context_str = prev_ctx["initial_context"]

    json_text = initial_context_str.split("=== ANAMNESIS AND INPUT DATA ===\n")[1].split(
        "\n\n=== VITAL MARKERS"
    )[0]
    parsed_input = json.loads(json_text)

    assert parsed_input["evaluated_ledger_version"] == 1
    assert parsed_input["latest_ledger_version"] == 2
    assert parsed_input["has_unreviewed_lab_changes"] is True

    # Evaluated shelf preserves previously evaluated potassium and sodium
    assert "potassium" in parsed_input["shared_labs_shelf"]
    assert parsed_input["shared_labs_shelf"]["potassium"]["value"] == "4.5 mmol/L"
    assert "sodium" in parsed_input["shared_labs_shelf"]

    # Potassium is detected in pending_removed_marker_keys
    assert "potassium" in parsed_input["pending_removed_marker_keys"]

    # Potassium is strictly NOT in unreviewed_pending_labs
    assert "potassium" not in parsed_input["unreviewed_pending_labs"]
    # Sodium was unchanged, so also not in unreviewed_pending_labs
    assert "sodium" not in parsed_input["unreviewed_pending_labs"]

    # Invalidation observation is in laboratory_history
    k_obs = [obs for obs in parsed_input["laboratory_history"] if obs.get("marker_key") == "potassium"]
    assert any(obs.get("is_active") is False for obs in k_obs)


@pytest.mark.anyio
async def test_weekly_consilium_no_false_pending_when_ledger_version_unchanged(db_session: Session):
    """
    Verification:
    When no new mutations or OCR uploads occurred since Primary Consilium (ledger version is unchanged):
    - has_unreviewed_lab_changes is False.
    - unreviewed_pending_labs is empty {}.
    - pending_removed_marker_keys is empty [].
    - shared_labs_shelf accurately reflects the active markers.
    """
    cycle_id = "cycle_weekly_no_false_pending"
    cycle_repo = MedicalCycleRepository(db_session)
    cycle_repo.create(cycle_id=cycle_id, status="active")
    _seed_base_anamnesis_and_ecg(db_session, cycle_id)

    rev_service = LabRevisionConsiliumService(db=db_session)
    rev_service.upsert_laboratory_markers(
        cycle_id=cycle_id,
        mutations=[
            LabMarkerMutationItem(
                marker_key="creatinine",
                display_name="Creatinine",
                value="80 umol/L",
                sample_date="2026-01-10",
                event_type=LabEventType.INITIAL,
            ),
        ],
    )

    p_calls: List[Dict[str, Any]] = []
    p_gateway = _build_primary_mock_gateway(p_calls)
    p_service = PrimaryConsiliumService(db=db_session, gateway=p_gateway)
    await p_service.run_primary_consilium(cycle_id=cycle_id)

    # No new ledger entries created
    weekly_service = WeeklyConsiliumService(db=db_session)
    prev_ctx = weekly_service._load_previous_consilium_context(cycle_id)
    initial_context_str = prev_ctx["initial_context"]

    json_text = initial_context_str.split("=== ANAMNESIS AND INPUT DATA ===\n")[1].split(
        "\n\n=== VITAL MARKERS"
    )[0]
    parsed_input = json.loads(json_text)

    assert parsed_input["evaluated_ledger_version"] == 1
    assert parsed_input["latest_ledger_version"] == 1
    assert parsed_input["has_unreviewed_lab_changes"] is False
    assert parsed_input["unreviewed_pending_labs"] == {}
    assert parsed_input["pending_removed_marker_keys"] == []
    assert "creatinine" in parsed_input["shared_labs_shelf"]


@pytest.mark.anyio
async def test_weekly_consilium_preserves_evaluated_shelf_when_new_markers_added_and_modified(db_session: Session):
    """
    Verification:
    When ledger v1 has alt=30.0 and ast=25.0, and ledger v2 mutates alt=60.0 and adds ferritin=150.0:
    - shared_labs_shelf strictly preserves v1 values (alt=30.0, ast=25.0, no ferritin).
    - unreviewed_pending_labs contains mutated alt=60.0 and added ferritin=150.0.
    - ast is not in unreviewed_pending_labs (was not modified).
    - has_unreviewed_lab_changes is True.
    """
    cycle_id = "cycle_weekly_shelf_preservation"
    cycle_repo = MedicalCycleRepository(db_session)
    cycle_repo.create(cycle_id=cycle_id, status="active")
    _seed_base_anamnesis_and_ecg(db_session, cycle_id)

    rev_service = LabRevisionConsiliumService(db=db_session)
    rev_service.upsert_laboratory_markers(
        cycle_id=cycle_id,
        mutations=[
            LabMarkerMutationItem(
                marker_key="alt",
                display_name="ALT",
                value="30 U/L",
                sample_date="2026-01-10",
                event_type=LabEventType.INITIAL,
            ),
            LabMarkerMutationItem(
                marker_key="ast",
                display_name="AST",
                value="25 U/L",
                sample_date="2026-01-10",
                event_type=LabEventType.INITIAL,
            ),
        ],
    )

    p_calls: List[Dict[str, Any]] = []
    p_gateway = _build_primary_mock_gateway(p_calls)
    p_service = PrimaryConsiliumService(db=db_session, gateway=p_gateway)
    await p_service.run_primary_consilium(cycle_id=cycle_id)

    # v2: Mutate alt, add ferritin
    rev_service.upsert_laboratory_markers(
        cycle_id=cycle_id,
        mutations=[
            LabMarkerMutationItem(
                marker_key="alt",
                display_name="ALT",
                value="60 U/L",
                sample_date="2026-02-15",
                event_type=LabEventType.NEW_STUDY,
            ),
            LabMarkerMutationItem(
                marker_key="ferritin",
                display_name="Ferritin",
                value="150 ng/mL",
                sample_date="2026-02-15",
                event_type=LabEventType.INITIAL,
            ),
        ],
    )

    weekly_service = WeeklyConsiliumService(db=db_session)
    prev_ctx = weekly_service._load_previous_consilium_context(cycle_id)
    initial_context_str = prev_ctx["initial_context"]

    json_text = initial_context_str.split("=== ANAMNESIS AND INPUT DATA ===\n")[1].split(
        "\n\n=== VITAL MARKERS"
    )[0]
    parsed_input = json.loads(json_text)

    # Evaluated shelf preservation
    shelf = parsed_input["shared_labs_shelf"]
    assert shelf["alt"]["value"] == "30 U/L"
    assert shelf["ast"]["value"] == "25 U/L"
    assert "ferritin" not in shelf

    # Pending changes
    pending = parsed_input["unreviewed_pending_labs"]
    assert pending["alt"]["value"] == "60 U/L"
    assert pending["ferritin"]["value"] == "150 ng/mL"
    assert "ast" not in pending

    assert parsed_input["has_unreviewed_lab_changes"] is True
    assert parsed_input["pending_removed_marker_keys"] == []


@pytest.mark.anyio
async def test_weekly_consilium_new_observation_in_history_detected_even_if_active_marker_identical(db_session: Session):
    """
    Edge Case Verification:
    If a new observation is committed to observations_history in ledger v2,
    but the active_markers dictionary is byte-for-byte identical to evaluated v1:
    - has_unreviewed_lab_changes must be True.
    - unreviewed_pending_labs must include the marker because a new observation exists.
    - Full observations_history must contain both the evaluated and new observation.
    - shared_labs_shelf must preserve the evaluated v1 state.
    """
    cycle_id = "cycle_weekly_identical_active_new_obs"
    cycle_repo = MedicalCycleRepository(db_session)
    cycle_repo.create(cycle_id=cycle_id, status="active")
    _seed_base_anamnesis_and_ecg(db_session, cycle_id)

    report_repo = ModelReportRepository(db_session)
    # Seed ledger v1
    report_repo.create_next_version(
        cycle_id=cycle_id,
        model="gemini-3.8-flash",
        report_type=LABORATORY_LEDGER_REPORT_TYPE,
        privacy_level=PrivacyLevel.SERVER_ONLY,
        content={
            "ledger_version": 1,
            "active_markers": {
                "glucose": {
                    "marker_key": "glucose",
                    "display_name": "Glucose",
                    "value": "5.1 mmol/L",
                    "numeric_value": 5.1,
                    "unit": "mmol/L",
                    "sample_date": "2026-01-10",
                    "observation_id": "obs_glucose_v1",
                }
            },
            "observations_history": [
                {
                    "observation_id": "obs_glucose_v1",
                    "marker_key": "glucose",
                    "display_name": "Glucose",
                    "value": "5.1 mmol/L",
                    "numeric_value": 5.1,
                    "unit": "mmol/L",
                    "sample_date": "2026-01-10",
                    "is_active": True,
                }
            ],
        },
    )

    p_calls: List[Dict[str, Any]] = []
    p_gateway = _build_primary_mock_gateway(p_calls)
    p_service = PrimaryConsiliumService(db=db_session, gateway=p_gateway)
    await p_service.run_primary_consilium(cycle_id=cycle_id)

    # Seed ledger v2 with IDENTICAL active_markers, but an additional observation in observations_history
    report_repo.create_next_version(
        cycle_id=cycle_id,
        model="gemini-3.8-flash",
        report_type=LABORATORY_LEDGER_REPORT_TYPE,
        privacy_level=PrivacyLevel.SERVER_ONLY,
        content={
            "ledger_version": 2,
            "active_markers": {
                "glucose": {
                    "marker_key": "glucose",
                    "display_name": "Glucose",
                    "value": "5.1 mmol/L",
                    "numeric_value": 5.1,
                    "unit": "mmol/L",
                    "sample_date": "2026-01-10",
                    "observation_id": "obs_glucose_v1",
                }
            },
            "observations_history": [
                {
                    "observation_id": "obs_glucose_v1",
                    "marker_key": "glucose",
                    "display_name": "Glucose",
                    "value": "5.1 mmol/L",
                    "numeric_value": 5.1,
                    "unit": "mmol/L",
                    "sample_date": "2026-01-10",
                    "is_active": True,
                },
                {
                    "observation_id": "obs_glucose_v2_extra",
                    "marker_key": "glucose",
                    "display_name": "Glucose",
                    "value": "5.1 mmol/L",
                    "numeric_value": 5.1,
                    "unit": "mmol/L",
                    "sample_date": "2026-02-10",
                    "is_active": True,
                },
            ],
        },
    )

    weekly_service = WeeklyConsiliumService(db=db_session)
    prev_ctx = weekly_service._load_previous_consilium_context(cycle_id)
    initial_context_str = prev_ctx["initial_context"]

    json_text = initial_context_str.split("=== ANAMNESIS AND INPUT DATA ===\n")[1].split(
        "\n\n=== VITAL MARKERS"
    )[0]
    parsed_input = json.loads(json_text)

    assert parsed_input["evaluated_ledger_version"] == 1
    assert parsed_input["latest_ledger_version"] == 2
    assert parsed_input["has_unreviewed_lab_changes"] is True

    # unreviewed_pending_labs contains glucose because a new observation was added to history
    assert "glucose" in parsed_input["unreviewed_pending_labs"]
    # shared_labs_shelf preserves evaluated baseline
    assert "glucose" in parsed_input["shared_labs_shelf"]

    # History contains both observations
    assert len(parsed_input["laboratory_history"]) == 2
