import io
import json
import math
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
import pytest
from sqlalchemy.orm import Session

from app.models.model_report import ModelReport
from app.repositories.cycle_repository import MedicalCycleRepository
from app.repositories.measurement_repository import HealthMeasurementRepository
from app.repositories.report_repository import ModelReportRepository
from app.schemas.consilium import (
    SYSTEM_KEYS,
    AegisAgentRole,
    AnamnesisModelOutput,
    FlashMasterOutput,
    ProMasterOutput,
    LabRevisionFlashMasterOutput,
)
from app.schemas.privacy import PrivacyLevel
from app.services.ecg_context_service import (
    EcgConsiliumContext,
    assemble_consilium_ecg_context,
    sanitize_pii_text,
    sanitize_pii_value,
    is_pii_key,
)
from app.services.gemini_gateway import GeminiGateway, GeminiModelRole
from app.services.primary_consilium_service import (
    PREPARATORY_ANAMNESIS_REPORT_TYPE,
    PREPARATORY_OCR_REPORT_TYPE,
    PREPARATORY_ECG_REPORT_TYPE,
    ConsiliumContextMissingError,
    PrimaryConsiliumService,
)
from app.services.weekly_consilium_service import WeeklyConsiliumService
from app.services.lab_revision_consilium_service import LabRevisionConsiliumService
from tests.test_primary_consilium import (
    _sample_anamnesis_output,
    _sample_flash_output,
    _sample_pro_output,
    _build_consilium_mock_gateway,
    _gemini_envelope,
)
from tests.test_weekly_consilium import (
    _sample_weekly_flash_output,
    _sample_weekly_pro_output,
)
from tests.test_lab_revision_consilium import (
    _build_lab_revision_mock_gateway,
)


def _seed_basic_cycle(db_session: Session, cycle_id: str) -> None:
    cycle_repo = MedicalCycleRepository(db_session)
    if not cycle_repo.get(cycle_id):
        cycle_repo.create(cycle_id=cycle_id, status="active")

    measurement_repo = HealthMeasurementRepository(db_session)
    measurement_repo.create(
        cycle_id=cycle_id,
        measured_at=datetime(2026, 10, 21, 10, 0, tzinfo=timezone.utc),
        source="watch_sensor",
        pulse=70,
        systolic=120,
        diastolic=80,
        spo2=99,
    )

    primary_service = PrimaryConsiliumService(db=db_session)
    primary_service.save_preparatory_anamnesis_result(
        cycle_id=cycle_id,
        anamnesis_output=_sample_anamnesis_output(),
    )
    primary_service.save_preparatory_ocr_labs(
        cycle_id=cycle_id,
        shared_labs_table={
            "hemoglobin": "145 g/L",
            "glucose": "5.0 mmol/L",
        },
        created_at=datetime(2026, 10, 21, 10, 0, tzinfo=timezone.utc),
    )


# ==============================================================================
# 1. UNCONDUCTED / MISSING ECG FALLBACK
# ==============================================================================

def test_ecg_context_unconducted_fallback(db_session: Session):
    cycle_id = "test-c3-unconducted-empty"
    _seed_basic_cycle(db_session, cycle_id)

    # No preparatory ECG report exists
    ctx = assemble_consilium_ecg_context(db_session, cycle_id)
    assert ctx.is_conducted is False
    assert ctx.ecg_summary == "Not conducted"
    assert ctx.recording_type is None
    assert ctx.heart_rate_bpm is None
    assert ctx.intervals["pr_interval_ms"] is None
    assert ctx.limitations == []
    assert ctx.uncertainties == []
    assert ctx.raw_unresolved_metrics == {}

    d = ctx.to_dict()
    assert d["is_conducted"] is False
    assert d["ecg_summary"] == "Not conducted"


@pytest.mark.parametrize("fallback_phrase", ["Not conducted", "Not performed", "--", "None", "  "])
def test_ecg_context_unconducted_explicit_phrases(db_session: Session, fallback_phrase: str):
    cycle_id = f"test-c3-unconducted-{hash(fallback_phrase)}"
    _seed_basic_cycle(db_session, cycle_id)

    report_repo = ModelReportRepository(db_session)
    report_repo.create_next_version(
        cycle_id=cycle_id,
        model="gemini-3.6-flash",
        report_type=PREPARATORY_ECG_REPORT_TYPE,
        privacy_level=PrivacyLevel.SERVER_ONLY,
        content={
            "agent_role": AegisAgentRole.VISION_ECG.value,
            "ecg_summary": fallback_phrase,
        },
    )

    ctx = assemble_consilium_ecg_context(db_session, cycle_id)
    assert ctx.is_conducted is False
    assert ctx.ecg_summary == "Not conducted"
    assert ctx.recording_type is None
    assert ctx.heart_rate_bpm is None


# ==============================================================================
# 2. LEGACY REPORT FALLBACK (ONLY ecg_summary) & PROVENANCE LIMITATION
# ==============================================================================

def test_ecg_context_legacy_report_fallback(db_session: Session):
    cycle_id = "test-c3-legacy-ecg"
    _seed_basic_cycle(db_session, cycle_id)

    summary_text = "Sinus rhythm, HR 68 bpm, normal electrical axis, no acute ST-T deviations."
    report_repo = ModelReportRepository(db_session)
    report_repo.create_next_version(
        cycle_id=cycle_id,
        model="gemini-3.6-flash",
        report_type=PREPARATORY_ECG_REPORT_TYPE,
        privacy_level=PrivacyLevel.SERVER_ONLY,
        content={
            "agent_role": AegisAgentRole.VISION_ECG.value,
            "ecg_summary": summary_text,
        },
    )

    ctx = assemble_consilium_ecg_context(db_session, cycle_id)
    assert ctx.is_conducted is True
    assert ctx.ecg_summary == summary_text
    assert ctx.recording_type is None
    assert ctx.heart_rate_bpm is None
    assert ctx.intervals["pr_interval_ms"] is None
    assert ctx.raw_unresolved_metrics == {}
    # Provenance notice and legacy format limitation documented without fake verification
    assert any("provenance" in lim.lower() for lim in ctx.limitations)
    assert any("legacy" in lim.lower() for lim in ctx.limitations)


# ==============================================================================
# 3. PII SAFETY: OVERT IDENTIFIERS & NESTED STRUCTURES
# ==============================================================================

def test_ecg_context_full_structured_and_pii_sanitization(db_session: Session):
    cycle_id = "test-c3-full-structured-pii"
    _seed_basic_cycle(db_session, cycle_id)

    report_repo = ModelReportRepository(db_session)
    report_repo.create_next_version(
        cycle_id=cycle_id,
        model="gemini-3.6-flash",
        report_type=PREPARATORY_ECG_REPORT_TYPE,
        privacy_level=PrivacyLevel.SERVER_ONLY,
        content={
            "agent_role": AegisAgentRole.VISION_ECG.value,
            "ecg_summary": "12-lead ECG: Normal sinus rhythm, HR 74 bpm, normal intervals for Patient: John Doe (email: john@example.com).",
            "recording_type": "standard_12_lead",
            "recording_quality": "good",
            "lead_name": "Standard 12 leads (I, II, III, aVR, aVL, aVF, V1-V6)",
            "record_date": "2026-10-21",
            "heart_rate_bpm": 74,
            "rhythm_type": "sinus_rhythm",
            "rhythm_regularity": "regular",
            "intervals": {
                "pr_interval_ms": 156.0,
                "qrs_duration_ms": 86.0,
                "qt_interval_ms": 390.0,
                "qtc_interval_ms": 420.0,
            },
            "electrical_axis": "normal (~45 deg)",
            "st_segment_findings": "isoelectric; reviewed by Dr. House",
            "t_wave_findings": "concordant positive",
            "limitations": [
                "Minor baseline drift in lead III",
                "Patient phone: +1-555-0199 noted in paper footer",
            ],
            "uncertainties": [
                "Exact PR onset in aVR slightly obscured",
                "Patient: Jane Doe DOB: 1980-05-15 handwriting on strip",
            ],
            "potential_urgent_warning": "Urgent review requested by Dr. House at Hospital: Princeton Plainsboro",
            "raw_unresolved_metrics": {
                "qtc_interval_ms": {
                    "raw_value": "520 ms",
                    "reason": "Patient contact ivan@test.com reported abnormal reading at Clinic: MedCity",
                },
                "rejected_hr_peak": 280,
                "patient_full_name": "Ivan Testov",
                "doctor_private_cell": "+7 (999) 123-45-67",
                "hospital_address": "Main Street 10",
                "clinic_name": "General Clinic",
            },
            # Leaked top-level PII to test sanitization
            "patient_name": "Ivan Testov",
            "patient_dob": "1980-05-15",
            "doctor_name": "Dr. House",
            "hospital": "Princeton Plainsboro",
            "image_hash": "a1b2c3d4e5f6",
            "ingested_at": "2026-10-21T10:00:00Z",
        },
    )

    ctx = assemble_consilium_ecg_context(db_session, cycle_id)
    assert ctx.is_conducted is True
    assert ctx.recording_type == "standard_12_lead"
    assert ctx.recording_quality == "good"
    assert ctx.lead_name == "Standard 12 leads (I, II, III, aVR, aVL, aVF, V1-V6)"
    assert ctx.record_date == "2026-10-21"
    assert ctx.heart_rate_bpm == 74
    assert ctx.rhythm_type == "sinus_rhythm"
    assert ctx.rhythm_regularity == "regular"
    assert ctx.intervals["pr_interval_ms"] == 156.0
    assert ctx.intervals["qrs_duration_ms"] == 86.0
    assert ctx.intervals["qt_interval_ms"] == 390.0
    assert ctx.intervals["qtc_interval_ms"] == 420.0
    assert ctx.electrical_axis == "normal (~45 deg)"

    # Verify clinical information is preserved in text while PII is redacted:
    assert "Normal sinus rhythm, HR 74 bpm" in ctx.ecg_summary
    assert "John Doe" not in ctx.ecg_summary
    assert "john@example.com" not in ctx.ecg_summary
    assert "[REDACTED_NAME]" in ctx.ecg_summary
    assert "[REDACTED_EMAIL]" in ctx.ecg_summary

    assert "isoelectric" in ctx.st_segment_findings
    assert "Dr. House" not in ctx.st_segment_findings
    assert "[REDACTED_NAME]" in ctx.st_segment_findings

    assert any("Minor baseline drift" in lim for lim in ctx.limitations)
    assert not any("+1-555-0199" in lim for lim in ctx.limitations)
    assert any("[REDACTED_PHONE]" in lim for lim in ctx.limitations)

    assert any("Exact PR onset" in unc for unc in ctx.uncertainties)
    assert not any("Jane Doe" in unc for unc in ctx.uncertainties)
    assert any("[REDACTED_NAME]" in unc for unc in ctx.uncertainties)
    assert any("[REDACTED_DOB]" in unc for unc in ctx.uncertainties)

    assert "Urgent review requested" in ctx.potential_urgent_warning
    assert "Dr. House" not in ctx.potential_urgent_warning
    assert "Princeton Plainsboro" not in ctx.potential_urgent_warning
    assert "[REDACTED_NAME]" in ctx.potential_urgent_warning
    assert "[REDACTED_ORGANIZATION]" in ctx.potential_urgent_warning

    # Verify PII keys not in to_dict()
    ctx_dict = ctx.to_dict()
    forbidden_keys = {
        "patient_name",
        "patient_dob",
        "doctor_name",
        "hospital",
        "image_hash",
        "ingested_at",
    }
    assert forbidden_keys.isdisjoint(ctx_dict.keys())

    # Verify non-allowlist keys are purged from raw_unresolved_metrics
    assert "rejected_hr_peak" not in ctx.raw_unresolved_metrics
    assert "patient_full_name" not in ctx.raw_unresolved_metrics
    assert "doctor_private_cell" not in ctx.raw_unresolved_metrics
    assert "hospital_address" not in ctx.raw_unresolved_metrics
    assert "clinic_name" not in ctx.raw_unresolved_metrics

    # Allowlisted metric is preserved and its reason is sanitized against PII
    qtc_unresolved = ctx.raw_unresolved_metrics.get("qtc_interval_ms")
    assert qtc_unresolved is not None
    assert qtc_unresolved["raw_value"] == "520 ms"
    assert qtc_unresolved["reason"] == "Metric value cannot be verified from ECG recording."
    assert "ivan@test.com" not in qtc_unresolved["reason"]
    assert "MedCity" not in qtc_unresolved["reason"]


def test_pii_sanitization_helper_preserves_clinical_numbers():
    clinical_text = "Date: 2026-10-21, BP: 120/80 mmHg, HR: 72 bpm, PR: 160.0 ms, QRS: 88 ms, 12-lead ECG."
    sanitized = sanitize_pii_text(clinical_text)
    assert sanitized == clinical_text


# ==============================================================================
# 4. REPORT PROVENANCE & VALIDATION
# ==============================================================================

def test_ecg_context_missing_agent_role_is_rejected(db_session: Session):
    cycle_id = "test-c3-missing-agent-role"
    _seed_basic_cycle(db_session, cycle_id)

    report_repo = ModelReportRepository(db_session)
    report_repo.create_next_version(
        cycle_id=cycle_id,
        model="gemini-3.6-flash",
        report_type=PREPARATORY_ECG_REPORT_TYPE,
        privacy_level=PrivacyLevel.SERVER_ONLY,
        content={
            # Missing agent_role cannot prove verification
            "ecg_summary": "Sinus rhythm",
        },
    )

    with pytest.raises(ConsiliumContextMissingError, match="Invalid or untrusted preparatory Vision ECG report"):
        assemble_consilium_ecg_context(db_session, cycle_id)


def test_ecg_context_invalid_agent_role_is_rejected(db_session: Session):
    cycle_id = "test-c3-invalid-agent-role"
    _seed_basic_cycle(db_session, cycle_id)

    report_repo = ModelReportRepository(db_session)
    report_repo.create_next_version(
        cycle_id=cycle_id,
        model="gemini-3.6-flash",
        report_type=PREPARATORY_ECG_REPORT_TYPE,
        privacy_level=PrivacyLevel.SERVER_ONLY,
        content={
            "agent_role": "athena_curator",
            "ecg_summary": "Sinus rhythm",
        },
    )

    with pytest.raises(ConsiliumContextMissingError, match="Invalid or untrusted preparatory Vision ECG report"):
        assemble_consilium_ecg_context(db_session, cycle_id)


def test_ecg_context_malformed_non_dict_content_is_rejected(db_session: Session):
    cycle_id = "test-c3-malformed-content"
    _seed_basic_cycle(db_session, cycle_id)

    report_repo = ModelReportRepository(db_session)
    report = report_repo.create_next_version(
        cycle_id=cycle_id,
        model="gemini-3.6-flash",
        report_type=PREPARATORY_ECG_REPORT_TYPE,
        privacy_level=PrivacyLevel.SERVER_ONLY,
        content={},
    )
    # Simulate DB corruption where content is empty or non-dict
    report.content = None
    db_session.commit()

    with pytest.raises(ConsiliumContextMissingError, match="malformed or empty"):
        assemble_consilium_ecg_context(db_session, cycle_id)


# ==============================================================================
# 5. NUMERIC SAFETY (C-10 STANDARDS COMPLIANCE)
# ==============================================================================

def test_ecg_context_numeric_safety_fractional_heart_rate(db_session: Session):
    cycle_id = "test-c3-num-fractional-hr"
    _seed_basic_cycle(db_session, cycle_id)

    report_repo = ModelReportRepository(db_session)
    report_repo.create_next_version(
        cycle_id=cycle_id,
        model="gemini-3.6-flash",
        report_type=PREPARATORY_ECG_REPORT_TYPE,
        privacy_level=PrivacyLevel.SERVER_ONLY,
        content={
            "agent_role": AegisAgentRole.VISION_ECG.value,
            "recording_type": "standard_12_lead",
            "heart_rate_bpm": 72.4,
            "ecg_summary": "Sinus rhythm, HR 72.4 bpm",
        },
    )

    ctx = assemble_consilium_ecg_context(db_session, cycle_id)
    assert ctx.is_conducted is True
    # Rounded to integer 72
    assert ctx.heart_rate_bpm == 72
    # Original value and conversion note preserved without loss
    assert "heart_rate_bpm" in ctx.raw_unresolved_metrics
    conv = ctx.raw_unresolved_metrics["heart_rate_bpm"]
    assert conv["raw_value"] == 72.4
    assert "rounded to nearest integer" in conv["conversion"]


def test_ecg_context_numeric_safety_nan_and_infinity_handling(db_session: Session):
    cycle_id = "test-c3-num-nan-inf"
    _seed_basic_cycle(db_session, cycle_id)

    report_repo = ModelReportRepository(db_session)
    report_repo.create_next_version(
        cycle_id=cycle_id,
        model="gemini-3.6-flash",
        report_type=PREPARATORY_ECG_REPORT_TYPE,
        privacy_level=PrivacyLevel.SERVER_ONLY,
        content={
            "agent_role": AegisAgentRole.VISION_ECG.value,
            "recording_type": "standard_12_lead",
            "heart_rate_bpm": float("nan"),
            "intervals": {
                "pr_interval_ms": float("inf"),
                "qrs_duration_ms": "-Infinity",
            },
            "ecg_summary": "Corrupted numeric record",
        },
    )

    ctx = assemble_consilium_ecg_context(db_session, cycle_id)
    # Questionable values rejected to None without throwing or crashing
    assert ctx.heart_rate_bpm is None
    assert ctx.intervals["pr_interval_ms"] is None
    assert ctx.intervals["qrs_duration_ms"] is None

    # Preserved in raw_unresolved_metrics with explanation
    assert "heart_rate_bpm" in ctx.raw_unresolved_metrics
    assert "NaN or Infinite" in ctx.raw_unresolved_metrics["heart_rate_bpm"]["reason"]
    assert "pr_interval_ms" in ctx.raw_unresolved_metrics
    assert "NaN or Infinite" in ctx.raw_unresolved_metrics["pr_interval_ms"]["reason"]
    assert "qrs_duration_ms" in ctx.raw_unresolved_metrics


def test_ecg_context_numeric_safety_strings_with_units_and_conversions(db_session: Session):
    cycle_id = "test-c3-num-units-conv"
    _seed_basic_cycle(db_session, cycle_id)

    report_repo = ModelReportRepository(db_session)
    report_repo.create_next_version(
        cycle_id=cycle_id,
        model="gemini-3.6-flash",
        report_type=PREPARATORY_ECG_REPORT_TYPE,
        privacy_level=PrivacyLevel.SERVER_ONLY,
        content={
            "agent_role": AegisAgentRole.VISION_ECG.value,
            "recording_type": "standard_12_lead",
            "heart_rate_bpm": "78 bpm",
            "intervals": {
                "pr_interval_ms": "0.16 s",  # seconds converted to ms
                "qrs_duration_ms": "84 ms",
                "qt_interval_ms": "400",
                "qtc_interval_ms": "425 ms",
            },
            "ecg_summary": "Normal rhythm",
        },
    )

    ctx = assemble_consilium_ecg_context(db_session, cycle_id)
    assert ctx.heart_rate_bpm == 78
    assert ctx.intervals["pr_interval_ms"] == 160.0
    assert ctx.intervals["qrs_duration_ms"] == 84.0
    assert ctx.intervals["qt_interval_ms"] == 400.0
    assert ctx.intervals["qtc_interval_ms"] == 425.0

    # Verify seconds conversion metadata was preserved
    assert "pr_interval_ms" in ctx.raw_unresolved_metrics
    assert "Converted from seconds" in ctx.raw_unresolved_metrics["pr_interval_ms"]["conversion"]


def test_ecg_context_numeric_safety_mismatched_units_rejected(db_session: Session):
    cycle_id = "test-c3-num-mismatched-units"
    _seed_basic_cycle(db_session, cycle_id)

    report_repo = ModelReportRepository(db_session)
    report_repo.create_next_version(
        cycle_id=cycle_id,
        model="gemini-3.6-flash",
        report_type=PREPARATORY_ECG_REPORT_TYPE,
        privacy_level=PrivacyLevel.SERVER_ONLY,
        content={
            "agent_role": AegisAgentRole.VISION_ECG.value,
            "recording_type": "standard_12_lead",
            "heart_rate_bpm": "72 ms",  # time unit for frequency
            "intervals": {
                "pr_interval_ms": "160 bpm",  # frequency unit for interval
            },
            "ecg_summary": "Confused units",
        },
    )

    ctx = assemble_consilium_ecg_context(db_session, cycle_id)
    assert ctx.heart_rate_bpm is None
    assert ctx.intervals["pr_interval_ms"] is None

    assert "heart_rate_bpm" in ctx.raw_unresolved_metrics
    assert "Mismatched unit" in ctx.raw_unresolved_metrics["heart_rate_bpm"]["reason"]
    assert "pr_interval_ms" in ctx.raw_unresolved_metrics
    assert "Mismatched unit" in ctx.raw_unresolved_metrics["pr_interval_ms"]["reason"]


def test_ecg_context_numeric_safety_negative_and_out_of_range_legacy_numbers(db_session: Session):
    cycle_id = "test-c3-num-neg-out-of-range"
    _seed_basic_cycle(db_session, cycle_id)

    report_repo = ModelReportRepository(db_session)
    report_repo.create_next_version(
        cycle_id=cycle_id,
        model="gemini-3.6-flash",
        report_type=PREPARATORY_ECG_REPORT_TYPE,
        privacy_level=PrivacyLevel.SERVER_ONLY,
        content={
            "agent_role": AegisAgentRole.VISION_ECG.value,
            "recording_type": "standard_12_lead",
            "heart_rate_bpm": -60,  # negative HR
            "intervals": {
                "pr_interval_ms": -20.0,  # negative interval
                "qrs_duration_ms": 500.0,  # out of 30-400 ms range
                "qt_interval_ms": 100.0,   # below 150 ms range
            },
            "ecg_summary": "Negative numbers",
        },
    )

    ctx = assemble_consilium_ecg_context(db_session, cycle_id)
    assert ctx.heart_rate_bpm is None
    assert ctx.intervals["pr_interval_ms"] is None
    assert ctx.intervals["qrs_duration_ms"] is None
    assert ctx.intervals["qt_interval_ms"] is None

    # Preserved in raw_unresolved_metrics with explanation without inventing diagnoses
    assert ctx.raw_unresolved_metrics["heart_rate_bpm"]["raw_value"] == -60
    assert "outside standard computational range" in ctx.raw_unresolved_metrics["heart_rate_bpm"]["reason"]
    assert ctx.raw_unresolved_metrics["pr_interval_ms"]["raw_value"] == -20.0
    assert ctx.raw_unresolved_metrics["qrs_duration_ms"]["raw_value"] == 500.0


def test_ecg_context_numeric_safety_single_lead_strip_nullifies_legacy_axis(db_session: Session):
    cycle_id = "test-c3-single-lead-legacy-axis"
    _seed_basic_cycle(db_session, cycle_id)

    report_repo = ModelReportRepository(db_session)
    report_repo.create_next_version(
        cycle_id=cycle_id,
        model="gemini-3.6-flash",
        report_type=PREPARATORY_ECG_REPORT_TYPE,
        privacy_level=PrivacyLevel.SERVER_ONLY,
        content={
            "agent_role": AegisAgentRole.VISION_ECG.value,
            "recording_type": "single_lead_strip",
            "lead_name": "Lead I",
            "electrical_axis": "left axis deviation (-30 deg)",  # invalid for single lead
            "ecg_summary": "Single lead strip",
        },
    )

    ctx = assemble_consilium_ecg_context(db_session, cycle_id)
    # Enforces C-10: single lead cannot determine electrical axis
    assert ctx.electrical_axis is None
    assert "electrical_axis" in ctx.raw_unresolved_metrics
    assert ctx.raw_unresolved_metrics["electrical_axis"]["raw_value"] == "left axis deviation (-30 deg)"
    assert "Single-lead" in ctx.raw_unresolved_metrics["electrical_axis"]["reason"]


def test_ecg_context_preserves_c10_resolutions(db_session: Session):
    cycle_id = "test-c3-c10-safety-preservation"
    _seed_basic_cycle(db_session, cycle_id)

    report_repo = ModelReportRepository(db_session)
    report_repo.create_next_version(
        cycle_id=cycle_id,
        model="gemini-3.6-flash",
        report_type=PREPARATORY_ECG_REPORT_TYPE,
        privacy_level=PrivacyLevel.SERVER_ONLY,
        content={
            "agent_role": AegisAgentRole.VISION_ECG.value,
            "ecg_summary": "Single-lead smartwatch recording (Lead I): HR 82 bpm, sinus rhythm.",
            "recording_type": "single_lead_smartwatch",
            "recording_quality": "adequate",
            "lead_name": "Lead I",
            "heart_rate_bpm": 82,
            "rhythm_type": "sinus_rhythm",
            "rhythm_regularity": "regular",
            "intervals": {
                "pr_interval_ms": None,
                "qrs_duration_ms": None,
                "qt_interval_ms": None,
                "qtc_interval_ms": None,
            },
            "electrical_axis": None,
            "st_segment_findings": None,
            "t_wave_findings": "positive",
            "limitations": [
                "Single-lead recording: frontal plane electrical axis cannot be calculated from a single lead."
            ],
            "uncertainties": [],
            "potential_urgent_warning": None,
            "raw_unresolved_metrics": {
                "heart_rate_bpm": 350,
            },
        },
    )

    ctx = assemble_consilium_ecg_context(db_session, cycle_id)
    assert ctx.is_conducted is True
    assert ctx.recording_type == "single_lead_smartwatch"
    assert ctx.lead_name == "Lead I"
    assert ctx.electrical_axis is None
    assert any("Single-lead recording" in lim for lim in ctx.limitations)
    assert ctx.raw_unresolved_metrics.get("heart_rate_bpm") == 350


# ==============================================================================
# 6. DELIVERY TO ALL MODELS IN PRIMARY CONSILIUM
# ==============================================================================

@pytest.mark.anyio
async def test_ecg_context_delivery_primary_consilium(db_session: Session):
    cycle_id = "test-c3-primary-consilium-delivery"
    _seed_basic_cycle(db_session, cycle_id)

    report_repo = ModelReportRepository(db_session)
    report_repo.create_next_version(
        cycle_id=cycle_id,
        model="gemini-3.6-flash",
        report_type=PREPARATORY_ECG_REPORT_TYPE,
        privacy_level=PrivacyLevel.SERVER_ONLY,
        content={
            "agent_role": AegisAgentRole.VISION_ECG.value,
            "ecg_summary": "12-lead ECG: Sinus bradycardia, HR 54 bpm, normal axis.",
            "recording_type": "standard_12_lead",
            "recording_quality": "optimal",
            "lead_name": "12 Leads Standard",
            "heart_rate_bpm": 54,
            "rhythm_type": "sinus_bradycardia",
            "rhythm_regularity": "regular",
            "intervals": {
                "pr_interval_ms": 178.0,
                "qrs_duration_ms": 84.0,
                "qt_interval_ms": 420.0,
                "qtc_interval_ms": 398.0,
            },
            "electrical_axis": "normal (~30 deg)",
            "limitations": ["Patient movement during V2"],
            "uncertainties": [],
            "potential_urgent_warning": None,
            "raw_unresolved_metrics": {},
        },
    )

    recorded_calls: List[Dict[str, Any]] = []
    gateway = _build_consilium_mock_gateway(recorded_calls)
    service = PrimaryConsiliumService(db=db_session, gateway=gateway)

    resp = await service.run_primary_consilium(cycle_id=cycle_id, report_date="2026-10-21")
    assert resp.cycle_id == cycle_id

    # Verify 10 calls: 8 Flash-Lite -> 1 Deputy Flash -> 1 Chief Pro
    assert len(recorded_calls) == 10

    # 1. Verify ALL 8 Flash-Lite organ specialists received ecg_context
    for i in range(8):
        prompt_i = recorded_calls[i]["prompt"]
        system_key = SYSTEM_KEYS[i]
        assert system_key in prompt_i

        # Check prompt contains ecg_context with structured metrics
        assert '"ecg_context"' in prompt_i
        assert '"sinus_bradycardia"' in prompt_i
        assert '"standard_12_lead"' in prompt_i
        assert '54' in prompt_i

    # 2. Verify Deputy Flash received ecg_context
    flash_prompt = recorded_calls[8]["prompt"]
    assert '"ecg_context"' in flash_prompt
    assert '"sinus_bradycardia"' in flash_prompt
    assert '"standard_12_lead"' in flash_prompt

    # 3. Verify Chief Pro received ecg_context
    pro_prompt = recorded_calls[9]["prompt"]
    assert '"ecg_context"' in pro_prompt
    assert '"sinus_bradycardia"' in pro_prompt
    assert '"standard_12_lead"' in pro_prompt


# ==============================================================================
# 7. DELIVERY TO ALL MODELS IN WEEKLY CONSILIUM
# ==============================================================================

@pytest.mark.anyio
async def test_ecg_context_delivery_weekly_consilium(db_session: Session):
    cycle_id = "test-c3-weekly-consilium-delivery"
    _seed_basic_cycle(db_session, cycle_id)

    report_repo = ModelReportRepository(db_session)
    report_repo.create_next_version(
        cycle_id=cycle_id,
        model="gemini-3.6-flash",
        report_type=PREPARATORY_ECG_REPORT_TYPE,
        privacy_level=PrivacyLevel.SERVER_ONLY,
        content={
            "agent_role": AegisAgentRole.VISION_ECG.value,
            "ecg_summary": "12-lead ECG: Normal sinus rhythm, HR 72 bpm.",
            "recording_type": "standard_12_lead",
            "recording_quality": "good",
            "lead_name": "Standard 12 leads",
            "heart_rate_bpm": 72,
            "rhythm_type": "sinus_rhythm",
            "rhythm_regularity": "regular",
            "intervals": {
                "pr_interval_ms": 160.0,
                "qrs_duration_ms": 82.0,
                "qt_interval_ms": 380.0,
                "qtc_interval_ms": 415.0,
            },
            "electrical_axis": "normal (~45 deg)",
            "limitations": [],
            "uncertainties": [],
            "potential_urgent_warning": None,
            "raw_unresolved_metrics": {},
        },
    )

    # First run primary consilium so baseline outputs exist for weekly
    recorded_primary: List[Dict[str, Any]] = []
    primary_gateway = _build_consilium_mock_gateway(recorded_primary)
    primary_service = PrimaryConsiliumService(db=db_session, gateway=primary_gateway)
    await primary_service.run_primary_consilium(cycle_id=cycle_id)

    # Now run weekly consilium and record prompts
    weekly_calls: List[Dict[str, Any]] = []

    class MockWeeklyGateway(GeminiGateway):
        async def generate_structured(self, prompt: str, response_schema: Any, model: GeminiModelRole = GeminiModelRole.FLASH) -> Any:
            weekly_calls.append({"model": model, "prompt": prompt})
            if model == GeminiModelRole.FLASH:
                from app.schemas.consilium import WeeklyFlashMasterOutput
                return WeeklyFlashMasterOutput.model_validate(_sample_weekly_flash_output())
            else:
                from app.schemas.consilium import WeeklyProMasterOutput
                return WeeklyProMasterOutput.model_validate(_sample_weekly_pro_output())

    weekly_service = WeeklyConsiliumService(db=db_session, gateway=MockWeeklyGateway())
    res = await weekly_service.run_weekly_consilium(cycle_id=cycle_id)
    assert res.cycle_id == cycle_id

    # Verify exactly 2 calls: Deputy Flash -> Chief Pro
    assert len(weekly_calls) == 2

    # Deputy Flash prompt has structured ecg_context
    weekly_flash_prompt = weekly_calls[0]["prompt"]
    assert '"ecg_context"' in weekly_flash_prompt
    assert '"sinus_rhythm"' in weekly_flash_prompt
    assert '72' in weekly_flash_prompt

    # Chief Pro prompt has structured ecg_context
    weekly_pro_prompt = weekly_calls[1]["prompt"]
    assert '"ecg_context"' in weekly_pro_prompt
    assert '"sinus_rhythm"' in weekly_pro_prompt
    assert '72' in weekly_pro_prompt


# ==============================================================================
# 8. DELIVERY TO ALL MODELS IN LAB REVISION CONSILIUM
# ==============================================================================

@pytest.mark.anyio
async def test_ecg_context_delivery_lab_revision_consilium(db_session: Session):
    cycle_id = "test-c3-lab-revision-delivery"
    _seed_basic_cycle(db_session, cycle_id)

    report_repo = ModelReportRepository(db_session)
    report_repo.create_next_version(
        cycle_id=cycle_id,
        model="gemini-3.6-flash",
        report_type=PREPARATORY_ECG_REPORT_TYPE,
        privacy_level=PrivacyLevel.SERVER_ONLY,
        content={
            "agent_role": AegisAgentRole.VISION_ECG.value,
            "ecg_summary": "12-lead ECG: Sinus tachycardia, HR 105 bpm, normal intervals.",
            "recording_type": "standard_12_lead",
            "recording_quality": "good",
            "lead_name": "Standard 12 leads",
            "heart_rate_bpm": 105,
            "rhythm_type": "sinus_tachycardia",
            "rhythm_regularity": "regular",
            "intervals": {
                "pr_interval_ms": 140.0,
                "qrs_duration_ms": 80.0,
                "qt_interval_ms": 340.0,
                "qtc_interval_ms": 448.0,
            },
            "electrical_axis": "normal (~60 deg)",
            "limitations": [],
            "uncertainties": [],
            "potential_urgent_warning": None,
            "raw_unresolved_metrics": {},
        },
    )

    # First run primary consilium so baseline conclusions exist
    recorded_primary: List[Dict[str, Any]] = []
    primary_gateway = _build_consilium_mock_gateway(recorded_primary)
    primary_service = PrimaryConsiliumService(db=db_session, gateway=primary_gateway)
    await primary_service.run_primary_consilium(cycle_id=cycle_id)

    # Update lab ledger to trigger lab revision
    lab_service = LabRevisionConsiliumService(db=db_session)
    lab_service.upsert_laboratory_markers(
        cycle_id=cycle_id,
        mutations=[
            {
                "marker_key": "ferritin",
                "value": "55 ng/mL",
                "display_name": "Ferritin",
                "sample_date": "2026-10-21",
            }
        ],
    )

    lab_calls: List[Dict[str, Any]] = []
    lab_gateway = _build_lab_revision_mock_gateway(lab_calls)
    lab_revision_service = LabRevisionConsiliumService(db=db_session, gateway=lab_gateway)
    res = await lab_revision_service.run_lab_revision_consilium(cycle_id=cycle_id)
    assert res.cycle_id == cycle_id

    # Verify 10 calls: 8 Flash-Lite -> 1 Deputy Flash -> 1 Chief Pro
    assert len(lab_calls) == 10

    # 1. Verify 8 Flash-Lite specialists received ecg_context
    for i in range(8):
        organ_prompt = lab_calls[i]["prompt"]
        assert '"ecg_context"' in organ_prompt
        assert '"sinus_tachycardia"' in organ_prompt
        assert '105' in organ_prompt

    # 2. Verify Deputy Flash received ecg_context
    flash_prompt = lab_calls[8]["prompt"]
    assert '"ecg_context"' in flash_prompt
    assert '"sinus_tachycardia"' in flash_prompt

    # 3. Verify Chief Pro received ecg_context
    pro_prompt = lab_calls[9]["prompt"]
    assert '"ecg_context"' in pro_prompt
    assert '"sinus_tachycardia"' in pro_prompt


# ==============================================================================
# 9. REGRESSION TESTS: C-3 FINAL PII DEFENSE-IN-DEPTH & NUMBER SAFETY
# ==============================================================================

def test_ecg_context_pii_in_invalid_heart_rate(db_session: Session):
    cycle_id = "test-c3-pii-invalid-hr"
    _seed_basic_cycle(db_session, cycle_id)

    raw_hr_pii = "140 bpm (Patient: John Doe, phone: +7-999-123-45-67, email: test@example.com)"
    report_repo = ModelReportRepository(db_session)
    report_repo.create_next_version(
        cycle_id=cycle_id,
        model="gemini-3.6-flash",
        report_type=PREPARATORY_ECG_REPORT_TYPE,
        privacy_level=PrivacyLevel.SERVER_ONLY,
        content={
            "agent_role": AegisAgentRole.VISION_ECG.value,
            "ecg_summary": "ECG with PII in HR",
            "heart_rate_bpm": raw_hr_pii,
            "recording_type": "standard_12_lead",
        },
    )

    ctx = assemble_consilium_ecg_context(db_session, cycle_id)
    assert ctx.is_conducted is True
    assert ctx.heart_rate_bpm is None

    # raw_unresolved contains sanitized raw_value (None for free text) and deterministic reason
    assert "heart_rate_bpm" in ctx.raw_unresolved_metrics
    hr_entry = ctx.raw_unresolved_metrics["heart_rate_bpm"]
    assert hr_entry["raw_value"] is None
    assert hr_entry["reason"] == "Non-numeric, NaN or Infinite metric format."

    # Uncertainties note is sanitized
    assert any("Heart rate reading cannot be verified" in unc for unc in ctx.uncertainties)
    assert not any("John Doe" in unc for unc in ctx.uncertainties)
    assert not any("+7-999-123-45-67" in unc for unc in ctx.uncertainties)
    assert not any("test@example.com" in unc for unc in ctx.uncertainties)

    # Entire to_dict() contract has ZERO leaked identifiers
    dumped = json.dumps(ctx.to_dict())
    assert "John Doe" not in dumped
    assert "+7-999-123-45-67" not in dumped
    assert "test@example.com" not in dumped


def test_ecg_context_pii_in_invalid_intervals(db_session: Session):
    cycle_id = "test-c3-pii-invalid-intervals"
    _seed_basic_cycle(db_session, cycle_id)

    report_repo = ModelReportRepository(db_session)
    report_repo.create_next_version(
        cycle_id=cycle_id,
        model="gemini-3.6-flash",
        report_type=PREPARATORY_ECG_REPORT_TYPE,
        privacy_level=PrivacyLevel.SERVER_ONLY,
        content={
            "agent_role": AegisAgentRole.VISION_ECG.value,
            "ecg_summary": "ECG with PII in intervals",
            "recording_type": "standard_12_lead",
            "intervals": {
                "pr_interval_ms": "160 ms / Doctor: Dr. House / contact: +1-555-0199",
                "qrs_duration_ms": "90 ms (Patient: Jane Smith)",
                "qt_interval_ms": "400 ms (email: clinic_staff@hospital.org)",
                "qtc_interval_ms": "440 ms (MRN: 987654)",
            },
        },
    )

    ctx = assemble_consilium_ecg_context(db_session, cycle_id)
    assert ctx.is_conducted is True
    # Values rejected from discrete numeric fields to protect precision
    for k in ("pr_interval_ms", "qrs_duration_ms", "qt_interval_ms", "qtc_interval_ms"):
        assert ctx.intervals[k] is None

    # Preserved safely in raw_unresolved_metrics with PII redacted
    for k in ("pr_interval_ms", "qrs_duration_ms", "qt_interval_ms", "qtc_interval_ms"):
        assert k in ctx.raw_unresolved_metrics

    dumped = json.dumps(ctx.to_dict())
    assert "Dr. House" not in dumped
    assert "+1-555-0199" not in dumped
    assert "Jane Smith" not in dumped
    assert "clinic_staff@hospital.org" not in dumped
    assert "987654" not in dumped


def test_ecg_context_record_date_validation_and_pii(db_session: Session):
    # 1. Valid ISO date
    cycle_id_valid = "test-c3-date-valid-iso"
    _seed_basic_cycle(db_session, cycle_id_valid)

    report_repo = ModelReportRepository(db_session)
    report_repo.create_next_version(
        cycle_id=cycle_id_valid,
        model="gemini-3.6-flash",
        report_type=PREPARATORY_ECG_REPORT_TYPE,
        privacy_level=PrivacyLevel.SERVER_ONLY,
        content={
            "agent_role": AegisAgentRole.VISION_ECG.value,
            "ecg_summary": "Normal record date",
            "record_date": "2026-10-21",
            "recording_type": "standard_12_lead",
        },
    )
    ctx_valid = assemble_consilium_ecg_context(db_session, cycle_id_valid)
    assert ctx_valid.record_date == "2026-10-21"
    assert not any("record_date" in u.lower() for u in ctx_valid.uncertainties)

    # 2. Valid European date normalized
    cycle_id_eu = "test-c3-date-valid-eu"
    _seed_basic_cycle(db_session, cycle_id_eu)
    report_repo.create_next_version(
        cycle_id=cycle_id_eu,
        model="gemini-3.6-flash",
        report_type=PREPARATORY_ECG_REPORT_TYPE,
        privacy_level=PrivacyLevel.SERVER_ONLY,
        content={
            "agent_role": AegisAgentRole.VISION_ECG.value,
            "ecg_summary": "EU record date",
            "record_date": "21.10.2026",
            "recording_type": "standard_12_lead",
        },
    )
    ctx_eu = assemble_consilium_ecg_context(db_session, cycle_id_eu)
    assert ctx_eu.record_date == "2026-10-21"

    # 3. Invalid record date containing PII and arbitrary free text
    cycle_id_invalid = "test-c3-date-invalid-pii"
    _seed_basic_cycle(db_session, cycle_id_invalid)
    report_repo.create_next_version(
        cycle_id=cycle_id_invalid,
        model="gemini-3.6-flash",
        report_type=PREPARATORY_ECG_REPORT_TYPE,
        privacy_level=PrivacyLevel.SERVER_ONLY,
        content={
            "agent_role": AegisAgentRole.VISION_ECG.value,
            "ecg_summary": "Invalid record date with PII",
            "record_date": "2024-05-12 / Patient: Alice Smith / Tel: +1234567890",
            "recording_type": "standard_12_lead",
        },
    )
    ctx_invalid = assemble_consilium_ecg_context(db_session, cycle_id_invalid)
    # record_date is not forwarded as free text
    assert ctx_invalid.record_date is None

    # Technical uncertainty recorded without leaking Alice Smith or phone
    assert any("record_date format is invalid" in u for u in ctx_invalid.uncertainties)
    assert not any("Alice Smith" in u for u in ctx_invalid.uncertainties)
    assert not any("+1234567890" in u for u in ctx_invalid.uncertainties)

    # Raw unresolved stores neutralized raw_value=None and neutral reason
    assert "record_date" in ctx_invalid.raw_unresolved_metrics
    raw_date_entry = ctx_invalid.raw_unresolved_metrics["record_date"]
    assert raw_date_entry["raw_value"] is None
    assert "Invalid or unverified study date format" in raw_date_entry["reason"]

    dumped = json.dumps(ctx_invalid.to_dict())
    assert "Alice Smith" not in dumped
    assert "+1234567890" not in dumped


def test_ecg_context_nested_ocr_strings_with_pii(db_session: Session):
    cycle_id = "test-c3-nested-ocr-pii"
    _seed_basic_cycle(db_session, cycle_id)

    report_repo = ModelReportRepository(db_session)
    report_repo.create_next_version(
        cycle_id=cycle_id,
        model="gemini-3.6-flash",
        report_type=PREPARATORY_ECG_REPORT_TYPE,
        privacy_level=PrivacyLevel.SERVER_ONLY,
        content={
            "agent_role": AegisAgentRole.VISION_ECG.value,
            "ecg_summary": "ECG with nested OCR data",
            "recording_type": "standard_12_lead",
            "raw_unresolved_metrics": {
                "heart_rate_bpm": {
                    "raw_value": 240,
                    "reason": "Doctor reported resting tachycardia in patient notes",
                },
                "ocr_fragments": [
                    {"box_id": 1, "ocr_text": "Patient: Ivan Petrov, email: ivan@clinic.ru, phone: +7 (916) 123-45-67"},
                    {"box_id": 2, "ocr_text": "Dr. Watson at Hospital: St. Jude"},
                ],
                "patient_fio": "Ivan Petrov",
                "doctor_cell": "+7-999-000-11-22",
                "clinic_name": "St. Jude Hospital",
                "snils": "123-456-789 00",
            },
        },
    )

    ctx = assemble_consilium_ecg_context(db_session, cycle_id)
    raw_unres = ctx.raw_unresolved_metrics

    # Top-level PII keys and arbitrary OCR text blocks are purged by allowlist
    assert "patient_fio" not in raw_unres
    assert "doctor_cell" not in raw_unres
    assert "clinic_name" not in raw_unres
    assert "snils" not in raw_unres
    assert "ocr_fragments" not in raw_unres

    # Allowlisted metrics are preserved with safe values and deterministic template reasons
    assert "heart_rate_bpm" in raw_unres
    assert raw_unres["heart_rate_bpm"]["raw_value"] == 240
    assert raw_unres["heart_rate_bpm"]["reason"] == "Metric value cannot be verified from ECG recording."

    # Safe refusal for unhandled / unsafe data structures
    class _CustomUnsafeData:
        pass
    assert sanitize_pii_value(_CustomUnsafeData()) == "[REDACTED_UNSUPPORTED_DATA]"

    dumped = json.dumps(ctx.to_dict())
    assert "Ivan Petrov" not in dumped
    assert "ivan@clinic.ru" not in dumped
    assert "+7 (916) 123-45-67" not in dumped
    assert "Dr. Watson" not in dumped
    assert "St. Jude" not in dumped
    assert "+7-999-000-11-22" not in dumped
    assert "Dr. Watson" not in dumped
    assert "St. Jude" not in dumped
    assert "+7-999-000-11-22" not in dumped


def test_ecg_context_no_raw_identifiers_in_to_dict_contract(db_session: Session):
    cycle_id = "test-c3-contract-zero-pii"
    _seed_basic_cycle(db_session, cycle_id)

    report_repo = ModelReportRepository(db_session)
    report_repo.create_next_version(
        cycle_id=cycle_id,
        model="gemini-3.6-flash",
        report_type=PREPARATORY_ECG_REPORT_TYPE,
        privacy_level=PrivacyLevel.SERVER_ONLY,
        content={
            "agent_role": AegisAgentRole.VISION_ECG.value,
            "ecg_summary": "Patient: Bob Johnson DOB: 1975-04-12 Sinus rhythm 88 bpm.",
            "recording_type": "standard_12_lead / Clinic: City Central",
            "recording_quality": "good / Operator: Alice Cooper",
            "lead_name": "Lead I / reviewed by Dr. Gregory House",
            "record_date": "2026-10-21 / Patient: Bob Johnson",
            "heart_rate_bpm": "88 bpm (Patient: Bob Johnson, tel: +1-555-4321)",
            "rhythm_type": "sinus / Nurse: Nancy Drew",
            "rhythm_regularity": "regular / Hospital: St. Mary",
            "intervals": {"pr_interval_ms": "160 ms / email: doc@test.org"},
            "electrical_axis": "normal / Dr. Who",
            "st_segment_findings": "isoelectric / Technician: Tom Sawyer",
            "t_wave_findings": "positive / Dr. Watson",
            "limitations": ["Patient phone: +1-800-555-0199", "SNILS: 111-222-333 44"],
            "uncertainties": ["Doctor email: doc@clinic.com", "Passport: 4000 123456"],
            "potential_urgent_warning": "Urgent notice by Dr. House for Patient: Bob Johnson",
            "raw_unresolved_metrics": {
                "patient_address": "123 Main Street",
                "ocr_lines": ["Patient: Bob Johnson", "Email: bob@test.com", "Phone: +1-555-4321"],
            },
        },
    )

    ctx = assemble_consilium_ecg_context(db_session, cycle_id)
    dumped = json.dumps(ctx.to_dict())

    # Verify zero raw personal identifiers anywhere in the serialized context:
    forbidden_strings = [
        "Bob Johnson",
        "Alice Cooper",
        "Gregory House",
        "Nancy Drew",
        "Tom Sawyer",
        "Dr. Who",
        "Dr. Watson",
        "+1-555-4321",
        "+1-800-555-0199",
        "doc@test.org",
        "doc@clinic.com",
        "bob@test.com",
        "111-222-333 44",
        "4000 123456",
        "123 Main Street",
    ]
    for s in forbidden_strings:
        assert s not in dumped, f"Overt personal identifier '{s}' was found in ecg_context.to_dict()!"


def test_ecg_context_unresolved_metrics_strict_allowlist_enforcement(db_session: Session):
    """
    Verifies that raw_unresolved_metrics enforces a strict allowlist boundary:
    - Only allowed ECG metrics (heart_rate_bpm, intervals, electrical_axis, record_date)
    - Only allowed subfields (raw_value, reason, conversion)
    - Purges all arbitrary OCR dumps, text boxes, and non-metric keys.
    """
    cycle_id = "test-c3-allowlist-boundary"
    _seed_basic_cycle(db_session, cycle_id)

    report_repo = ModelReportRepository(db_session)
    report_repo.create_next_version(
        cycle_id=cycle_id,
        model="gemini-3.6-flash",
        report_type=PREPARATORY_ECG_REPORT_TYPE,
        privacy_level=PrivacyLevel.SERVER_ONLY,
        content={
            "agent_role": AegisAgentRole.VISION_ECG.value,
            "ecg_summary": "ECG with allowlist test data",
            "recording_type": "standard_12_lead",
            "raw_unresolved_metrics": {
                # 1. Allowed metrics with allowed fields
                "heart_rate_bpm": {
                    "raw_value": 310,
                    "reason": "Extreme tachycardia reading",
                    "forbidden_extra_field": "leak_test",
                },
                "pr_interval_ms": {
                    "raw_value": "0.22 s",
                    "reason": "Prolonged PR",
                    "conversion": "Converted from seconds (0.22 s) to milliseconds (220.0 ms).",
                },
                "qrs_duration_ms": {"raw_value": 140.0, "reason": "Wide QRS"},
                "qt_interval_ms": {"raw_value": 460.0, "reason": "Borderline QT"},
                "qtc_interval_ms": {"raw_value": 510.0, "reason": "Prolonged QTc"},
                "electrical_axis": {"raw_value": "left axis (-45 deg)", "reason": "Fascicular block"},
                "record_date": {"raw_value": None, "reason": "Invalid date format"},
                # 2. Disallowed arbitrary keys
                "ocr_boxes": [{"x": 10, "y": 20, "text": "Unlabeled Name"}],
                "ocr_raw_text": "Unstructured OCR text fragment with random identifiers",
                "patient_history": "Previous MI in 2020",
                "doctor_private_cell": "+7 (999) 000-00-00",
            },
        },
    )

    ctx = assemble_consilium_ecg_context(db_session, cycle_id)
    raw = ctx.raw_unresolved_metrics

    # Allowed keys are present
    allowed_keys = {
        "heart_rate_bpm",
        "pr_interval_ms",
        "qrs_duration_ms",
        "qt_interval_ms",
        "qtc_interval_ms",
        "electrical_axis",
        "record_date",
    }
    assert allowed_keys.issubset(raw.keys())

    # Disallowed keys are purged
    disallowed_keys = {"ocr_boxes", "ocr_raw_text", "patient_history", "doctor_private_cell"}
    assert disallowed_keys.isdisjoint(raw.keys())

    # Subfields within allowed metrics are strictly filtered
    hr_entry = raw["heart_rate_bpm"]
    assert "raw_value" in hr_entry
    assert "reason" in hr_entry
    assert "forbidden_extra_field" not in hr_entry


def test_ecg_context_legacy_provenance_requires_agent_role(db_session: Session):
    """
    Verifies provenance rules for legacy records:
    - Reports created via save_preparatory_ecg_summary include agent_role='vision_ecg' and succeed.
    - Reports without agent_role are strictly rejected with ConsiliumContextMissingError.
    """
    # 1. Valid legacy summary created via service helper
    cycle_valid = "test-c3-legacy-helper-valid"
    _seed_basic_cycle(db_session, cycle_valid)
    service = PrimaryConsiliumService(db=db_session)
    service.save_preparatory_ecg_summary(
        cycle_id=cycle_valid,
        ecg_summary="Sinus bradycardia, HR 52 bpm, legacy record.",
    )

    ctx_valid = assemble_consilium_ecg_context(db_session, cycle_valid)
    assert ctx_valid.is_conducted is True
    assert "Sinus bradycardia" in ctx_valid.ecg_summary
    assert any("legacy" in lim.lower() for lim in ctx_valid.limitations)
    assert any("provenance" in lim.lower() for lim in ctx_valid.limitations)

    # 2. Invalid legacy report missing agent_role
    cycle_missing_role = "test-c3-legacy-missing-role"
    _seed_basic_cycle(db_session, cycle_missing_role)
    report_repo = ModelReportRepository(db_session)
    report_repo.create_next_version(
        cycle_id=cycle_missing_role,
        model="gemini-3.6-flash",
        report_type=PREPARATORY_ECG_REPORT_TYPE,
        privacy_level=PrivacyLevel.SERVER_ONLY,
        content={
            "ecg_summary": "Legacy report without agent role",
        },
    )

    with pytest.raises(ConsiliumContextMissingError, match="expected agent_role='vision_ecg'"):
        assemble_consilium_ecg_context(db_session, cycle_missing_role)


# ==============================================================================
# 10. REGRESSION TESTS: C-3 / C-10 V4 BOUNDARY FIXES
# ==============================================================================

def test_ecg_context_record_date_trailing_text_rejection(db_session: Session):
    """
    Verifies that ISO datetime with trailing text ('2026-10-21T12:30 Patient: John Doe')
    is strictly rejected:
    - record_date is None
    - raw_value is None
    - No PII is included in the consilium context or to_dict()
    """
    cycle_id = "test-c3-date-trailing-text"
    _seed_basic_cycle(db_session, cycle_id)

    report_repo = ModelReportRepository(db_session)
    report_repo.create_next_version(
        cycle_id=cycle_id,
        model="gemini-3.6-flash",
        report_type=PREPARATORY_ECG_REPORT_TYPE,
        privacy_level=PrivacyLevel.SERVER_ONLY,
        content={
            "agent_role": AegisAgentRole.VISION_ECG.value,
            "recording_type": "standard_12_lead",
            "record_date": "2026-10-21T12:30 Patient: John Doe",
            "ecg_summary": "ECG with trailing text in record_date",
        },
    )

    ctx = assemble_consilium_ecg_context(db_session, cycle_id)
    assert ctx.record_date is None

    # Technical uncertainty note is recorded
    assert any("record_date format is invalid" in u for u in ctx.uncertainties)
    assert not any("John Doe" in u for u in ctx.uncertainties)

    # raw_value is strictly None
    date_entry = ctx.raw_unresolved_metrics["record_date"]
    assert date_entry["raw_value"] is None
    assert date_entry["reason"] == "Invalid or unverified study date format; expected ISO date (YYYY-MM-DD)."

    # to_dict contains zero PII
    dumped = json.dumps(ctx.to_dict())
    assert "John Doe" not in dumped


def test_ecg_context_raw_metric_boundary_rejects_arbitrary_string_and_reason(db_session: Session):
    """
    Verifies that raw_value='John Doe apartment 42' and arbitrary reasons with unlabelled
    names are neutralized:
    - raw_value is rejected to None because it is not a valid numeric metric
    - reason is mapped to a fixed safe template (no free-text pass-through)
    """
    cycle_id = "test-c3-raw-metric-free-text-rejection"
    _seed_basic_cycle(db_session, cycle_id)

    report_repo = ModelReportRepository(db_session)
    report_repo.create_next_version(
        cycle_id=cycle_id,
        model="gemini-3.6-flash",
        report_type=PREPARATORY_ECG_REPORT_TYPE,
        privacy_level=PrivacyLevel.SERVER_ONLY,
        content={
            "agent_role": AegisAgentRole.VISION_ECG.value,
            "recording_type": "standard_12_lead",
            "ecg_summary": "ECG with free text in raw unresolved",
            "raw_unresolved_metrics": {
                "heart_rate_bpm": {
                    "raw_value": "John Doe apartment 42",
                    "reason": "Evaluated by Gregory House at clinic with unlabelled name",
                },
                "pr_interval_ms": {
                    "raw_value": "Jane Smith apartment 15",
                    "reason": "Review by Allison Cameron",
                },
            },
        },
    )

    ctx = assemble_consilium_ecg_context(db_session, cycle_id)
    hr_entry = ctx.raw_unresolved_metrics["heart_rate_bpm"]
    assert hr_entry["raw_value"] is None
    assert hr_entry["reason"] == "Metric value cannot be verified from ECG recording."

    pr_entry = ctx.raw_unresolved_metrics["pr_interval_ms"]
    assert pr_entry["raw_value"] is None
    assert pr_entry["reason"] == "Metric value cannot be verified from ECG recording."

    dumped = json.dumps(ctx.to_dict())
    assert "John Doe" not in dumped
    assert "apartment 42" not in dumped
    assert "Gregory House" not in dumped
    assert "Jane Smith" not in dumped
    assert "Allison Cameron" not in dumped


def test_ecg_context_unknown_lead_setup_nullifies_electrical_axis(db_session: Session):
    """
    Verifies that when lead configuration is unknown/undetermined, electrical_axis
    is NOT transmitted as a confirmed metric:
    - electrical_axis is None in context
    - raw_unresolved_metrics['electrical_axis']['raw_value'] == 'normal' (safe value preserved)
    - deterministic reason and clinical limitation recorded
    """
    cycle_id = "test-c3-unknown-lead-axis"
    _seed_basic_cycle(db_session, cycle_id)

    report_repo = ModelReportRepository(db_session)
    report_repo.create_next_version(
        cycle_id=cycle_id,
        model="gemini-3.6-flash",
        report_type=PREPARATORY_ECG_REPORT_TYPE,
        privacy_level=PrivacyLevel.SERVER_ONLY,
        content={
            "agent_role": AegisAgentRole.VISION_ECG.value,
            "recording_type": "unknown",
            "lead_name": None,
            "electrical_axis": "normal",
            "ecg_summary": "ECG with unknown recording type and normal axis",
        },
    )

    ctx = assemble_consilium_ecg_context(db_session, cycle_id)
    # Confirmed axis MUST be None because lead configuration is unknown/undetermined
    assert ctx.electrical_axis is None

    # Preserved in safe technical context
    assert "electrical_axis" in ctx.raw_unresolved_metrics
    axis_entry = ctx.raw_unresolved_metrics["electrical_axis"]
    assert axis_entry["raw_value"] == "normal"
    assert "undetermined" in axis_entry["reason"].lower()

    # Documented in limitations
    assert any("axis" in lim.lower() for lim in ctx.limitations)
    assert any("undetermined" in lim.lower() or "contradictory" in lim.lower() for lim in ctx.limitations)
