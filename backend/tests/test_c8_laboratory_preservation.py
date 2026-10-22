"""
PROJECT AEGIS — C-8 LABORATORY RESULT PRESERVATION TEST SUITE

Validates:
1. Dimensionless markers (INR, pH, specific gravity, ratios, indices) accepted without unit.
2. Numeric results with missing unit are preserved with numeric_value, unit=None, and explicit uncertainty.
3. Qualitative and semi-quantitative values (Positive, Negative, Trace, +, ++, +++, ++++, reactive, non-reactive, titers) preserved in original form.
4. No unit, date, or reference range hallucination.
5. Distinction between recognition ambiguity and missing unit uncertainty.
6. Propagation of uncertainty through mutations, ledger, context, and downstream consilium services.
7. Diff engine handling of qualitative, dimensionless, and missing-unit values.
8. Backward compatibility with older ledgers lacking uncertainty fields.
"""

import asyncio
from datetime import datetime, timezone
import io
from typing import Any, Dict, List
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.medical_cycle import MedicalCycle
from app.repositories.cycle_repository import MedicalCycleRepository
from app.repositories.report_repository import ModelReportRepository
from app.schemas.consilium import (
    AegisAgentRole,
    LabEventType,
    LabMarkerMutationItem,
    LabObservationEntry,
    LabReferenceRange,
    VisionOcrMarkerItem,
    is_dimensionless_marker,
)
from app.schemas.privacy import PrivacyLevel
from app.services.gemini_gateway import GeminiGateway, GeminiModelRole
from app.services.lab_context_service import (
    assemble_consilium_laboratory_context,
    sanitize_uncertainty_note,
    SAFE_SHARED_MARKER_FIELDS,
    SAFE_OBSERVATION_FIELDS,
)
from app.services.lab_revision_consilium_service import (
    LabRevisionConsiliumService,
    LABORATORY_LEDGER_REPORT_TYPE,
)
from app.services.lab_vision_ocr_service import (
    LabVisionOcrService,
    PREPARATORY_OCR_REPORT_TYPE,
)
from app.main import app
from app.routers.cascade import get_gemini_gateway


def _create_dummy_jpeg() -> bytes:
    # Standard JPEG magic bytes FF D8 FF E0
    return b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00" + b"\x00" * 200


def _build_mock_gateway(ocr_output: Dict[str, Any]) -> GeminiGateway:
    gateway = GeminiGateway()

    async def mock_generate_structured(prompt: str, response_schema: Any, model: GeminiModelRole, **kwargs):
        if hasattr(response_schema, "model_validate"):
            return response_schema.model_validate(ocr_output)
        return ocr_output

    gateway.generate_structured = mock_generate_structured  # type: ignore[assignment]
    return gateway


def test_is_dimensionless_marker_helper():
    """Rule 1: verifies dimensionless classification."""
    assert is_dimensionless_marker("inr") is True
    assert is_dimensionless_marker("ph") is True
    assert is_dimensionless_marker("urine_ph") is True
    assert is_dimensionless_marker("blood_ph") is True
    assert is_dimensionless_marker("specific_gravity") is True
    assert is_dimensionless_marker("ast_alt_ratio") is True
    assert is_dimensionless_marker("bun_creatinine_ratio") is True
    assert is_dimensionless_marker("atherogenic_index") is True
    assert is_dimensionless_marker("custom_ratio") is True
    assert is_dimensionless_marker("risk_index") is True

    # Regular quantitative markers must not be dimensionless
    assert is_dimensionless_marker("glucose") is False
    assert is_dimensionless_marker("potassium") is False
    assert is_dimensionless_marker("hemoglobin") is False
    assert is_dimensionless_marker("creatinine") is False


def test_dimensionless_markers_ingested_without_unit_or_uncertainty():
    """Rule 1: INR and pH have unit=None, are confirmed, and do NOT carry missing-unit uncertainty."""
    inr_marker = VisionOcrMarkerItem(
        original_name="МНО (INR)",
        marker_key="inr",
        raw_value="1.05",
        numeric_value=1.05,
        unit=None,
        sample_date="2026-10-15",
    )
    assert inr_marker.requires_user_verification is False
    assert inr_marker.is_uncertain is False
    assert inr_marker.uncertainty_note is None

    ph_marker = VisionOcrMarkerItem(
        original_name="pH мочи",
        marker_key="urine_ph",
        raw_value="6.5",
        numeric_value=6.5,
        unit=None,
        sample_date="2026-10-15",
    )
    assert ph_marker.requires_user_verification is False
    assert ph_marker.is_uncertain is False


def test_numeric_marker_missing_unit_preserved_with_uncertainty():
    """Rule 2 & 5 & 7: numbers without unit are preserved with unit=None and uncertainty, not rejected."""
    potassium_marker = VisionOcrMarkerItem(
        original_name="Калий сыворотки",
        marker_key="potassium",
        raw_value="4.2",
        numeric_value=4.2,
        unit=None,
        sample_date="2026-10-15",
    )
    # Does not require user confirmation before saving (Rule 7)
    assert potassium_marker.requires_user_verification is False
    # Preserves numeric value and unit=None without guessing units (Rule 2 & 4)
    assert potassium_marker.numeric_value == 4.2
    assert potassium_marker.unit is None
    # Explicitly records missing-unit uncertainty (Rule 2 & 5)
    assert potassium_marker.is_uncertain is True
    assert "Missing unit of measurement" in (potassium_marker.uncertainty_note or "")


def test_qualitative_and_semi_quantitative_spectrum_preserved():
    """Rule 3: qualitative strings, plus signs, titers, and borderline findings preserved."""
    test_cases = [
        ("Trace", "urine_protein", "Trace", False, None),
        ("следы", "urine_protein", "следы", False, None),
        ("++", "urine_glucose", "++", False, None),
        ("++++", "ketones", "++++", False, None),
        ("1:160", "ana_titer", "1:160", False, None),
        ("reactive", "vdrl", "reactive", False, None),
        ("non-reactive", "hiv_ab", "non-reactive", False, None),
        ("положительно", "crp_qual", "положительно", False, None),
        ("отрицательно", "hbsag", "отрицательно", False, None),
        ("слабоположительно", "rf", "слабоположительно", True, "Borderline qualitative"),
        ("сомнительно", "antibody", "сомнительно", True, "Borderline qualitative"),
    ]

    for raw, key, exp_val, exp_unc, unc_fragment in test_cases:
        item = VisionOcrMarkerItem(
            original_name=f"Test {key}",
            marker_key=key,
            raw_value=raw,
            numeric_value=None,
            unit=None,
            sample_date="2026-10-15",
        )
        assert item.requires_user_verification is False, f"Failed for {raw}"
        assert item.raw_value == exp_val
        assert item.is_uncertain is exp_unc, f"Uncertainty mismatch for {raw}"
        if unc_fragment:
            assert unc_fragment in (item.uncertainty_note or ""), f"Missing note for {raw}"


def test_distinguish_recognition_ambiguity_from_missing_unit():
    """Rule 5: physical blur on document requires verification; clean number without unit does not."""
    # 1. Smudged/blurred digit from OCR
    blurry_marker = VisionOcrMarkerItem(
        original_name="Ферритин",
        marker_key="ferritin",
        raw_value="~2?",
        numeric_value=None,
        unit="мкг/л",
        sample_date="2026-10-15",
        is_uncertain=True,
        uncertainty_note="Размыто пятном на бланке",
    )
    assert blurry_marker.requires_user_verification is True

    # 2. Crisp, clear digits but missing unit on form
    clean_no_unit = VisionOcrMarkerItem(
        original_name="Глюкоза",
        marker_key="glucose",
        raw_value="5.5",
        numeric_value=5.5,
        unit=None,
        sample_date="2026-10-15",
        is_uncertain=False,
    )
    assert clean_no_unit.requires_user_verification is False
    assert clean_no_unit.is_uncertain is True
    assert "Missing unit of measurement" in (clean_no_unit.uncertainty_note or "")


def test_full_vision_ocr_intake_preserves_c8_markers_in_ledger(
    client: TestClient, db_session: Session, auth_headers: dict[str, str]
):
    """End-to-end intake: verifies dimensionless, missing-unit, and qualitative markers enter ledger."""
    cycle_repo = MedicalCycleRepository(db_session)
    cycle = cycle_repo.create(cycle_id="test-cycle-c8-intake-01", status="active")

    mixed_c8_output = {
        "document_metadata": {
            "sample_date": "2026-10-20",
            "issue_date": "2026-10-21",
            "laboratory_name": "Central Clinical Lab",
            "page_count": 1,
            "notes": "Official report with mixed indicator types",
        },
        "markers": [
            # 1. Dimensionless: INR
            {
                "original_name": "МНО (INR)",
                "marker_key": "inr",
                "raw_value": "1.08",
                "numeric_value": 1.08,
                "unit": None,
                "reference_range": {"low": 0.85, "high": 1.15, "text": "0.85 - 1.15"},
                "sample_date": "2026-10-20",
                "category": "coagulation",
                "is_uncertain": False,
                "uncertainty_note": None,
                "is_critical_flag": False,
            },
            # 2. Dimensionless: pH
            {
                "original_name": "pH крови",
                "marker_key": "blood_ph",
                "raw_value": "7.39",
                "numeric_value": 7.39,
                "unit": None,
                "reference_range": {"low": 7.35, "high": 7.45, "text": "7.35 - 7.45"},
                "sample_date": "2026-10-20",
                "category": "biochemistry",
                "is_uncertain": False,
                "uncertainty_note": None,
                "is_critical_flag": False,
            },
            # 3. Numeric missing unit: Potassium
            {
                "original_name": "Калий",
                "marker_key": "potassium",
                "raw_value": "4.3",
                "numeric_value": 4.3,
                "unit": None,
                "reference_range": {"low": 3.5, "high": 5.1, "text": "3.5 - 5.1"},
                "sample_date": "2026-10-20",
                "category": "electrolytes",
                "is_uncertain": False,
                "uncertainty_note": None,
                "is_critical_flag": False,
            },
            # 4. Qualitative semi-quantitative: Urinalysis Protein
            {
                "original_name": "Белок в моче",
                "marker_key": "urine_protein",
                "raw_value": "Trace",
                "numeric_value": None,
                "unit": None,
                "reference_range": {"text": "отрицательно"},
                "sample_date": "2026-10-20",
                "category": "urinalysis",
                "is_uncertain": False,
                "uncertainty_note": None,
                "is_critical_flag": False,
            },
            # 5. Qualitative titer: Antinuclear antibodies
            {
                "original_name": "АНФ (титр)",
                "marker_key": "ana_titer",
                "raw_value": "1:160",
                "numeric_value": None,
                "unit": None,
                "reference_range": {"text": "< 1:160"},
                "sample_date": "2026-10-20",
                "category": "immunology",
                "is_uncertain": False,
                "uncertainty_note": None,
                "is_critical_flag": False,
            },
        ],
    }

    mock_gateway = _build_mock_gateway(mixed_c8_output)
    app.dependency_overrides[get_gemini_gateway] = lambda: mock_gateway

    resp = client.post(
        "/api/v1/cascade/intake/lab-images",
        data={"cycle_id": cycle.cycle_id},
        files=[("files", ("scan.jpg", io.BytesIO(_create_dummy_jpeg()), "image/jpeg"))],
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text
    res = resp.json()

    assert res["extracted_count"] == 5
    assert res["requires_verification_count"] == 0
    assert len(res["confirmed_markers"]) == 5
    assert len(res["unconfirmed_markers"]) == 0

    # Verify that all 5 markers entered the authoritative ledger
    ledger_service = LabRevisionConsiliumService(db=db_session, gateway=mock_gateway)
    ledger_state = ledger_service.get_lab_ledger_state(cycle.cycle_id)

    assert "inr" in ledger_state.active_markers
    inr_obs = ledger_state.active_markers["inr"]
    assert inr_obs.numeric_value == 1.08
    assert inr_obs.unit is None
    assert inr_obs.is_uncertain is False

    assert "blood_ph" in ledger_state.active_markers
    ph_obs = ledger_state.active_markers["blood_ph"]
    assert ph_obs.numeric_value == 7.39
    assert ph_obs.unit is None
    assert ph_obs.is_uncertain is False

    assert "potassium" in ledger_state.active_markers
    k_obs = ledger_state.active_markers["potassium"]
    assert k_obs.numeric_value == 4.3
    assert k_obs.unit is None
    assert k_obs.is_uncertain is True
    assert "Missing unit of measurement" in (k_obs.uncertainty_note or "")

    assert "urine_protein" in ledger_state.active_markers
    prot_obs = ledger_state.active_markers["urine_protein"]
    assert prot_obs.value == "Trace"
    assert prot_obs.numeric_value is None

    assert "ana_titer" in ledger_state.active_markers
    ana_obs = ledger_state.active_markers["ana_titer"]
    assert ana_obs.value == "1:160"

    # Context Service assembly preserves all fields for downstream consiliums
    lab_ctx = assemble_consilium_laboratory_context(db_session, cycle.cycle_id)
    assert "inr" in lab_ctx.shared_labs_table
    assert "potassium" in lab_ctx.shared_labs_table
    assert lab_ctx.shared_labs_table["potassium"]["is_uncertain"] is True
    assert "Missing unit of measurement" in lab_ctx.shared_labs_table["potassium"]["uncertainty_note"]


def test_diff_engine_handles_dimensionless_qualitative_and_missing_unit(db_session: Session):
    """Rule 7 in diff engine: verified changes in INR, qualitative text, and missing unit."""
    cycle_repo = MedicalCycleRepository(db_session)
    cycle = cycle_repo.create(cycle_id="test-cycle-diff-c8", status="active")

    service = LabRevisionConsiliumService(db=db_session)

    # Version 1 mutations:
    # 1. INR = 1.05 (dimensionless)
    # 2. Urine Protein = Negative (qualitative)
    # 3. Potassium = 4.2 (missing unit)
    mutations_v1 = [
        LabMarkerMutationItem(
            marker_key="inr",
            display_name="INR",
            value="1.05",
            unit=None,
            sample_date="2026-10-01",
        ),
        LabMarkerMutationItem(
            marker_key="urine_protein",
            display_name="Protein Urine",
            value="Negative",
            unit=None,
            sample_date="2026-10-01",
        ),
        LabMarkerMutationItem(
            marker_key="potassium",
            display_name="Potassium",
            value="4.2",
            unit=None,
            is_uncertain=True,
            uncertainty_note="Missing unit of measurement on report",
            sample_date="2026-10-01",
        ),
    ]
    service.upsert_laboratory_markers(cycle.cycle_id, mutations_v1)

    # Version 2 mutations:
    # 1. INR = 1.25 (value_updated, both unit=None)
    # 2. Urine Protein = Trace (qualitative value_updated, both unit=None)
    # 3. Potassium = 4.4 with unit="mmol/L" (unit_changed from None to mmol/L)
    mutations_v2 = [
        LabMarkerMutationItem(
            marker_key="inr",
            display_name="INR",
            event_type=LabEventType.NEW_STUDY,
            value="1.25",
            unit=None,
            sample_date="2026-10-15",
        ),
        LabMarkerMutationItem(
            marker_key="urine_protein",
            display_name="Protein Urine",
            event_type=LabEventType.NEW_STUDY,
            value="Trace",
            unit=None,
            sample_date="2026-10-15",
        ),
        LabMarkerMutationItem(
            marker_key="potassium",
            display_name="Potassium",
            event_type=LabEventType.NEW_STUDY,
            value="4.4",
            unit="mmol/L",
            sample_date="2026-10-15",
        ),
    ]
    service.upsert_laboratory_markers(cycle.cycle_id, mutations_v2)

    diff_report = service.compute_laboratory_diff(
        cycle_id=cycle.cycle_id,
        baseline_version=1,
        target_version=2,
    )

    diff_map = {item.marker_key: item for item in diff_report.modified_values}

    # 1. INR: no unit change, value updated, directly comparable
    inr_diff = diff_map["inr"]
    assert inr_diff.previous_value == "1.05"
    assert inr_diff.new_value == "1.25"
    assert inr_diff.previous_unit is None
    assert inr_diff.new_unit is None
    assert inr_diff.directly_comparable is True

    # 2. Protein Urine: qualitative transition Negative -> Trace
    prot_diff = diff_map["urine_protein"]
    assert prot_diff.previous_value == "Negative"
    assert prot_diff.new_value == "Trace"
    assert prot_diff.directly_comparable is True

    # 3. Potassium: unit changed from None to mmol/L -> flagged as unit_changed!
    pot_diff = diff_map["potassium"]
    assert pot_diff.previous_unit is None
    assert pot_diff.new_unit == "mmol/L"
    assert pot_diff.directly_comparable is False
    assert any(m.marker_key == "potassium" for m in diff_report.unit_changed_markers)


def test_backward_compatibility_old_ledger_without_uncertainty_fields():
    """Validates that older ledger JSON structures without is_uncertain validate cleanly."""
    legacy_obs_data = {
        "observation_id": "obs_glucose_v1_1",
        "marker_key": "glucose",
        "display_name": "Glucose",
        "value": "5.4",
        "numeric_value": 5.4,
        "unit": "mmol/L",
        "sample_date": "2026-09-01",
        "reference_range": {"low": 3.9, "high": 6.1, "text": "3.9 - 6.1"},
        "event_type": "initial",
        "supersedes_observation_id": None,
        "superseded_by_observation_id": None,
        "correction_reason": None,
        "is_active": True,
        "is_critical_flag": False,
        "recorded_at": "2026-09-01T10:00:00Z",
    }
    # Must validate cleanly without ValidationError
    entry = LabObservationEntry.model_validate(legacy_obs_data)
    assert entry.is_uncertain is False
    assert entry.uncertainty_note is None

    legacy_mutation_data = {
        "marker_key": "alt",
        "display_name": "ALT",
        "event_type": "new_study",
        "value": 25.0,
        "unit": "U/L",
        "sample_date": "2026-09-01",
    }
    mut = LabMarkerMutationItem.model_validate(legacy_mutation_data)
    assert mut.is_uncertain is False
    assert mut.uncertainty_note is None


def test_primary_consilium_receives_c8_context(db_session: Session):
    """Verifies Primary Consilium context pipeline receives and preserves C-8 indicators."""
    from app.services.primary_consilium_service import PrimaryConsiliumService

    cycle_repo = MedicalCycleRepository(db_session)
    cycle = cycle_repo.create(cycle_id="test-cycle-primary-c8", status="active")

    service = LabRevisionConsiliumService(db=db_session)
    mutations = [
        LabMarkerMutationItem(
            marker_key="inr",
            display_name="INR",
            value="1.12",
            unit=None,
            sample_date="2026-10-10",
        ),
        LabMarkerMutationItem(
            marker_key="blood_ph",
            display_name="Blood pH",
            value="7.41",
            unit=None,
            sample_date="2026-10-10",
        ),
        LabMarkerMutationItem(
            marker_key="potassium",
            display_name="Potassium",
            value="4.6",
            unit=None,
            is_uncertain=True,
            uncertainty_note="Missing unit of measurement on report",
            sample_date="2026-10-10",
        ),
        LabMarkerMutationItem(
            marker_key="urine_protein",
            display_name="Urine Protein",
            value="Trace",
            unit=None,
            sample_date="2026-10-10",
        ),
    ]
    service.upsert_laboratory_markers(cycle.cycle_id, mutations)

    primary_service = PrimaryConsiliumService(db=db_session)
    loaded_labs = primary_service._load_preparatory_labs(cycle.cycle_id)

    assert "inr" in loaded_labs
    assert loaded_labs["inr"]["value"] == "1.12"
    assert loaded_labs["inr"]["numeric_value"] == 1.12
    assert loaded_labs["inr"]["unit"] is None
    assert loaded_labs["inr"]["is_uncertain"] is False

    assert "blood_ph" in loaded_labs
    assert loaded_labs["blood_ph"]["numeric_value"] == 7.41
    assert loaded_labs["blood_ph"]["unit"] is None

    assert "potassium" in loaded_labs
    assert loaded_labs["potassium"]["numeric_value"] == 4.6
    assert loaded_labs["potassium"]["unit"] is None
    assert loaded_labs["potassium"]["is_uncertain"] is True
    assert "Missing unit of measurement" in loaded_labs["potassium"]["uncertainty_note"]

    assert "urine_protein" in loaded_labs
    assert loaded_labs["urine_protein"]["value"] == "Trace"
    assert loaded_labs["urine_protein"]["numeric_value"] is None


def test_weekly_consilium_unreviewed_pending_labs_with_c8_markers(db_session: Session):
    """Verifies Weekly Consilium isolates unevaluated C-8 markers in unreviewed_pending_labs."""
    from app.services.weekly_consilium_service import WeeklyConsiliumService

    cycle_repo = MedicalCycleRepository(db_session)
    cycle = cycle_repo.create(cycle_id="test-cycle-weekly-c8", status="active")

    service = LabRevisionConsiliumService(db=db_session)
    mutations_v1 = [
        LabMarkerMutationItem(
            marker_key="inr",
            display_name="INR",
            value="1.05",
            unit=None,
            sample_date="2026-10-01",
        ),
    ]
    service.upsert_laboratory_markers(cycle.cycle_id, mutations_v1)

    # Simulate Primary Consilium evaluated ledger v1
    report_repo = ModelReportRepository(db_session)
    report_repo.create_next_version(
        cycle_id=cycle.cycle_id,
        model="gemini-3.1-pro",
        report_type="chief_physician_internal",
        privacy_level=PrivacyLevel.SERVER_ONLY,
        content={
            "evaluated_ledger_version": 1,
            "flash_quality_review": {"approved_for_publication": True},
        },
    )

    # Now add unreviewed v2 mutations with new C-8 markers
    mutations_v2 = [
        LabMarkerMutationItem(
            marker_key="urine_protein",
            display_name="Urine Protein",
            value="++",
            unit=None,
            sample_date="2026-10-15",
        ),
        LabMarkerMutationItem(
            marker_key="glucose",
            display_name="Glucose",
            value="6.2",
            unit=None,
            is_uncertain=True,
            uncertainty_note="Missing unit of measurement on report",
            sample_date="2026-10-15",
        ),
    ]
    service.upsert_laboratory_markers(cycle.cycle_id, mutations_v2)

    # Check context resolution via Weekly Consilium logic
    from app.services.lab_context_service import (
        assemble_consilium_laboratory_context,
        get_last_evaluated_ledger_version,
    )

    latest_lab_ctx = assemble_consilium_laboratory_context(db_session, cycle.cycle_id)
    last_evaluated_ver = get_last_evaluated_ledger_version(db_session, cycle.cycle_id)

    assert last_evaluated_ver == 1
    assert latest_lab_ctx.ledger_version == 2

    # Evaluated context v1
    eval_lab_ctx = assemble_consilium_laboratory_context(db_session, cycle.cycle_id, ledger_version=1)
    shared_labs_shelf = eval_lab_ctx.shared_labs_table

    assert "inr" in shared_labs_shelf
    assert "urine_protein" not in shared_labs_shelf
    assert "glucose" not in shared_labs_shelf

    # Unreviewed pending labs for v2
    unreviewed_pending_labs: Dict[str, Any] = {}
    for k, marker_val in latest_lab_ctx.shared_labs_table.items():
        if k not in shared_labs_shelf or shared_labs_shelf[k] != marker_val:
            unreviewed_pending_labs[k] = marker_val

    assert "urine_protein" in unreviewed_pending_labs
    assert unreviewed_pending_labs["urine_protein"]["value"] == "++"
    assert "glucose" in unreviewed_pending_labs
    assert unreviewed_pending_labs["glucose"]["numeric_value"] == 6.2
    assert unreviewed_pending_labs["glucose"]["is_uncertain"] is True
    assert "Missing unit of measurement" in unreviewed_pending_labs["glucose"]["uncertainty_note"]


# ==============================================================================
# C-8 REVIEW FIXES REGRESSION TESTS
# ==============================================================================


def test_regression_ambiguous_digit_with_unknown_unit(
    client: TestClient, db_session: Session, auth_headers: dict[str, str]
):
    """
    Regression Test 1: Ambiguous digit with unknown unit.
    - Physical OCR ambiguity (~4.?) cannot be confirmed into authoritative ledger numbers.
    - Both is_recognition_ambiguous=True and requires_user_verification=True are set.
    - Remains available for correction in unconfirmed_markers without ledger pollution.
    """
    ambiguous_item = VisionOcrMarkerItem(
        original_name="Кальций общий",
        marker_key="calcium",
        raw_value="~2.?",
        numeric_value=None,
        unit=None,
        sample_date="2026-10-21",
        is_uncertain=True,
        uncertainty_note="Неразборчивая цифра на сгибе",
    )
    assert ambiguous_item.is_recognition_ambiguous is True
    assert ambiguous_item.is_uncertain is True
    assert ambiguous_item.requires_user_verification is True
    assert ambiguous_item.numeric_value is None

    # End-to-end intake verification
    cycle_repo = MedicalCycleRepository(db_session)
    cycle = cycle_repo.create(cycle_id="test-cycle-amb-digit-01", status="active")

    ocr_payload = {
        "document_metadata": {
            "sample_date": "2026-10-21",
            "page_count": 1,
        },
        "markers": [
            {
                "original_name": "Кальций общий",
                "marker_key": "calcium",
                "raw_value": "~2.?",
                "numeric_value": None,
                "unit": None,
                "sample_date": "2026-10-21",
                "is_uncertain": True,
                "uncertainty_note": "Неразборчивая цифра на сгибе",
            }
        ],
    }
    mock_gateway = _build_mock_gateway(ocr_payload)
    app.dependency_overrides[get_gemini_gateway] = lambda: mock_gateway

    resp = client.post(
        "/api/v1/cascade/intake/lab-images",
        data={"cycle_id": cycle.cycle_id},
        files=[("files", ("scan.jpg", io.BytesIO(_create_dummy_jpeg()), "image/jpeg"))],
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text
    res = resp.json()

    assert res["extracted_count"] == 1
    assert res["requires_verification_count"] == 1
    assert len(res["confirmed_markers"]) == 0
    assert len(res["unconfirmed_markers"]) == 1

    unconfirmed_item = res["unconfirmed_markers"][0]
    assert unconfirmed_item["marker_key"] == "calcium"
    assert unconfirmed_item["is_recognition_ambiguous"] is True
    assert unconfirmed_item["requires_user_verification"] is True

    # Ledger state MUST NOT contain unconfirmed ambiguous marker in active ledger numbers
    ledger_service = LabRevisionConsiliumService(db=db_session, gateway=mock_gateway)
    ledger_state = ledger_service.get_lab_ledger_state(cycle.cycle_id)
    assert "calcium" not in ledger_state.active_markers


def test_regression_correct_number_with_unknown_unit(
    client: TestClient, db_session: Session, auth_headers: dict[str, str]
):
    """
    Regression Test 2: Correct legible number with unknown unit.
    - Preserved as numeric_value, unit=None, is_uncertain=True, is_recognition_ambiguous=False.
    - Confirmed directly into active ledger without blocking on manual verification.
    """
    clean_item = VisionOcrMarkerItem(
        original_name="Кальций общий",
        marker_key="calcium",
        raw_value="2.35",
        numeric_value=2.35,
        unit=None,
        sample_date="2026-10-21",
        is_uncertain=False,
    )
    assert clean_item.is_recognition_ambiguous is False
    assert clean_item.is_uncertain is True
    assert clean_item.requires_user_verification is False
    assert clean_item.numeric_value == 2.35
    assert clean_item.unit is None
    assert "Missing unit of measurement" in (clean_item.uncertainty_note or "")

    # End-to-end intake verification
    cycle_repo = MedicalCycleRepository(db_session)
    cycle = cycle_repo.create(cycle_id="test-cycle-clean-no-unit-01", status="active")

    ocr_payload = {
        "document_metadata": {
            "sample_date": "2026-10-21",
            "page_count": 1,
        },
        "markers": [
            {
                "original_name": "Кальций общий",
                "marker_key": "calcium",
                "raw_value": "2.35",
                "numeric_value": 2.35,
                "unit": None,
                "sample_date": "2026-10-21",
                "is_uncertain": False,
            }
        ],
    }
    mock_gateway = _build_mock_gateway(ocr_payload)
    app.dependency_overrides[get_gemini_gateway] = lambda: mock_gateway

    resp = client.post(
        "/api/v1/cascade/intake/lab-images",
        data={"cycle_id": cycle.cycle_id},
        files=[("files", ("scan.jpg", io.BytesIO(_create_dummy_jpeg()), "image/jpeg"))],
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text
    res = resp.json()

    assert res["extracted_count"] == 1
    assert res["requires_verification_count"] == 0
    assert len(res["confirmed_markers"]) == 1
    assert len(res["unconfirmed_markers"]) == 0

    # Confirmed marker MUST enter authoritative ledger active markers
    ledger_service = LabRevisionConsiliumService(db=db_session, gateway=mock_gateway)
    ledger_state = ledger_service.get_lab_ledger_state(cycle.cycle_id)
    assert "calcium" in ledger_state.active_markers
    obs = ledger_state.active_markers["calcium"]
    assert obs.numeric_value == 2.35
    assert obs.unit is None
    assert obs.is_uncertain is True
    assert obs.is_recognition_ambiguous is False


def test_regression_repeated_validation_preserves_recognition_ambiguity():
    """
    Regression Test 3: Repeated validation idempotence.
    - Ambiguous OCR markers retain is_recognition_ambiguous=True across multiple evaluations.
    - Clean missing-unit markers retain is_recognition_ambiguous=False across multiple evaluations.
    - Missing unit and recognition ambiguity never cancel or overwrite each other.
    """
    # 1. Ambiguous marker evaluated repeatedly
    ambiguous_marker = VisionOcrMarkerItem(
        original_name="Ферритин",
        marker_key="ferritin",
        raw_value="~15?",
        numeric_value=None,
        unit=None,  # unknown unit AND ambiguous
        sample_date="2026-10-21",
        is_uncertain=True,
        uncertainty_note="Размыто пятном",
    )
    for _ in range(5):
        ambiguous_marker.evaluate_verification_requirement()
        assert ambiguous_marker.is_recognition_ambiguous is True
        assert ambiguous_marker.requires_user_verification is True
        assert ambiguous_marker.is_uncertain is True

    # 2. Clean missing unit marker evaluated repeatedly
    clean_no_unit = VisionOcrMarkerItem(
        original_name="Глюкоза",
        marker_key="glucose",
        raw_value="5.6",
        numeric_value=5.6,
        unit=None,
        sample_date="2026-10-21",
        is_uncertain=False,
    )
    for _ in range(5):
        clean_no_unit.evaluate_verification_requirement()
        assert clean_no_unit.is_recognition_ambiguous is False
        assert clean_no_unit.requires_user_verification is False
        assert clean_no_unit.is_uncertain is True

    # 3. Model re-validation cycle from dict
    dumped_amb = ambiguous_marker.model_dump()
    re_validated_amb = VisionOcrMarkerItem.model_validate(dumped_amb)
    assert re_validated_amb.is_recognition_ambiguous is True
    assert re_validated_amb.requires_user_verification is True

    dumped_clean = clean_no_unit.model_dump()
    re_validated_clean = VisionOcrMarkerItem.model_validate(dumped_clean)
    assert re_validated_clean.is_recognition_ambiguous is False
    assert re_validated_clean.requires_user_verification is False


def test_regression_pii_redaction_and_masking_in_uncertainty_note(
    client: TestClient, db_session: Session, auth_headers: dict[str, str]
):
    """
    Regression Test 4: Strict PII elimination in uncertainty_note.
    - Zero arbitrary free OCR text is passed to consilium context.
    - Strictly maps to predetermined safe clinical technical descriptions.
    - Original raw OCR reports in SERVER_ONLY preserve complete data for audit.
    """
    # 1. Direct unit sanitization tests
    raw_pii_note = "Blurry near Patient: John Doe, phone: +7 (999) 123-45-67, email: test@clinic.ru, passport: 4510 123456, dob: 1982-04-15"
    sanitized = sanitize_uncertainty_note(raw_pii_note)
    assert sanitized == "Recognition ambiguity: unclear print or damaged document"
    assert "John Doe" not in sanitized
    assert "123-45-67" not in sanitized
    assert "test@clinic.ru" not in sanitized
    assert "4510 123456" not in sanitized

    # 2. Standard cause normalization without PII
    norm_note = sanitize_uncertainty_note("Размыто пятном на бланке анализа")
    assert norm_note == "Recognition ambiguity: unclear print or damaged document"

    missing_unit_note = sanitize_uncertainty_note("Отсутствует единица измерения в бланке")
    assert missing_unit_note == "Missing unit of measurement for numeric value on laboratory report"

    # 3. Full pipeline context sanitization: Vision OCR -> Mutation -> Ledger -> Lab Context
    cycle_repo = MedicalCycleRepository(db_session)
    cycle = cycle_repo.create(cycle_id="test-cycle-pii-scrub-01", status="active")

    ocr_payload = {
        "document_metadata": {
            "sample_date": "2026-10-21",
            "laboratory_name": "City Clinical Lab",
            "page_count": 1,
        },
        "markers": [
            {
                "original_name": "АЛТ",
                "marker_key": "alt",
                "raw_value": "32.0",
                "numeric_value": 32.0,
                "unit": None,
                "sample_date": "2026-10-21",
                "is_uncertain": True,
                "uncertainty_note": "Missing unit; Patient Ivan Ivanov phone +79161234567 blur",
            }
        ],
    }
    mock_gateway = _build_mock_gateway(ocr_payload)
    app.dependency_overrides[get_gemini_gateway] = lambda: mock_gateway

    resp = client.post(
        "/api/v1/cascade/intake/lab-images",
        data={"cycle_id": cycle.cycle_id},
        files=[("files", ("scan.jpg", io.BytesIO(_create_dummy_jpeg()), "image/jpeg"))],
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text

    # Verify SERVER_ONLY preparatory OCR preserves raw note for audit
    prep_reports = ModelReportRepository(db_session).list_versions(
        cycle_id=cycle.cycle_id,
        report_type=PREPARATORY_OCR_REPORT_TYPE,
        privacy_level=PrivacyLevel.SERVER_ONLY,
    )
    assert len(prep_reports) == 1
    raw_saved_note = prep_reports[0].content["confirmed_observations"][0]["uncertainty_note"]
    assert "Ivan Ivanov" in raw_saved_note  # Intact in SERVER_ONLY audit log

    # Verify Lab Context assembled for consilium models GUARANTEED scrubbed of PII and arbitrary text
    lab_context = assemble_consilium_laboratory_context(db_session, cycle.cycle_id)
    alt_context_marker = lab_context.shared_labs_table["alt"]
    context_note = alt_context_marker["uncertainty_note"]

    assert "Ivan Ivanov" not in context_note
    assert "+79161234567" not in context_note
    assert "Missing unit of measurement for numeric value on laboratory report" in context_note
    assert "Recognition ambiguity: unclear print or damaged document" in context_note


def test_regression_missing_unit_with_russian_note(
    client: TestClient, db_session: Session, auth_headers: dict[str, str]
):
    """
    Regression Test: Missing unit with Russian note.
    - Decoupled from English string checks in uncertainty_note.
    - Verified as clean number without unit (numeric_value=4.2, unit=None, is_uncertain=True).
    - is_recognition_ambiguous is False, requires_user_verification is False.
    - In consilium context, uncertainty_note is mapped to standard technical formulation.
    """
    item = VisionOcrMarkerItem(
        original_name="Калий сыворотки",
        marker_key="potassium",
        raw_value="4.2",
        numeric_value=4.2,
        unit=None,
        sample_date="2026-10-21",
        is_uncertain=True,
        uncertainty_note="Отсутствует единица измерения в бланке лаборатории",
    )
    assert item.is_recognition_ambiguous is False
    assert item.requires_user_verification is False
    assert item.is_uncertain is True
    assert item.numeric_value == 4.2
    assert item.unit is None

    cycle_repo = MedicalCycleRepository(db_session)
    cycle = cycle_repo.create(cycle_id="test-cycle-ru-no-unit-01", status="active")

    ocr_payload = {
        "document_metadata": {"sample_date": "2026-10-21", "page_count": 1},
        "markers": [item.model_dump()],
    }
    mock_gateway = _build_mock_gateway(ocr_payload)
    app.dependency_overrides[get_gemini_gateway] = lambda: mock_gateway

    resp = client.post(
        "/api/v1/cascade/intake/lab-images",
        data={"cycle_id": cycle.cycle_id},
        files=[("files", ("scan.jpg", io.BytesIO(_create_dummy_jpeg()), "image/jpeg"))],
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text
    res = resp.json()

    assert res["requires_verification_count"] == 0
    assert len(res["confirmed_markers"]) == 1

    lab_context = assemble_consilium_laboratory_context(db_session, cycle.cycle_id)
    k_ctx = lab_context.shared_labs_table["potassium"]
    assert k_ctx["numeric_value"] == 4.2
    assert k_ctx["unit"] is None
    assert k_ctx["is_uncertain"] is True
    assert k_ctx["is_recognition_ambiguous"] is False
    assert k_ctx["uncertainty_note"] == "Missing unit of measurement for numeric value on laboratory report"


def test_regression_indeterminate_qualitative_result(
    client: TestClient, db_session: Session, auth_headers: dict[str, str]
):
    """
    Regression Test: Indeterminate qualitative result.
    - Clinical uncertainty (e.g. 'сомнительно') is distinguished from OCR recognition error.
    - is_recognition_ambiguous is False, requires_user_verification is False.
    - Confirmed into active ledger as a valid clinical qualitative observation.
    - In consilium context, mapped to 'Borderline or indeterminate qualitative finding'.
    """
    qual_item = VisionOcrMarkerItem(
        original_name="Антитела к вирусу",
        marker_key="antibody",
        raw_value="сомнительно",
        numeric_value=None,
        unit=None,
        sample_date="2026-10-21",
        is_uncertain=True,
        uncertainty_note="Сомнительный результат теста",
    )
    assert qual_item.is_recognition_ambiguous is False
    assert qual_item.requires_user_verification is False
    assert qual_item.is_uncertain is True
    assert qual_item.raw_value == "сомнительно"

    cycle_repo = MedicalCycleRepository(db_session)
    cycle = cycle_repo.create(cycle_id="test-cycle-qual-indet-01", status="active")

    ocr_payload = {
        "document_metadata": {"sample_date": "2026-10-21", "page_count": 1},
        "markers": [qual_item.model_dump()],
    }
    mock_gateway = _build_mock_gateway(ocr_payload)
    app.dependency_overrides[get_gemini_gateway] = lambda: mock_gateway

    resp = client.post(
        "/api/v1/cascade/intake/lab-images",
        data={"cycle_id": cycle.cycle_id},
        files=[("files", ("scan.jpg", io.BytesIO(_create_dummy_jpeg()), "image/jpeg"))],
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text
    res = resp.json()

    assert res["requires_verification_count"] == 0
    assert len(res["confirmed_markers"]) == 1

    ledger_service = LabRevisionConsiliumService(db=db_session, gateway=mock_gateway)
    ledger_state = ledger_service.get_lab_ledger_state(cycle.cycle_id)
    assert "antibody" in ledger_state.active_markers
    obs = ledger_state.active_markers["antibody"]
    assert obs.value == "сомнительно"
    assert obs.numeric_value is None
    assert obs.is_uncertain is True
    assert obs.is_recognition_ambiguous is False

    lab_context = assemble_consilium_laboratory_context(db_session, cycle.cycle_id)
    ab_ctx = lab_context.shared_labs_table["antibody"]
    assert ab_ctx["uncertainty_note"] == "Borderline or indeterminate qualitative finding"


def test_regression_string_boolean_flag_values():
    """
    Regression Test: String values of boolean flags ('true', 'false', '0', '1').
    - Verifies that string 'false' is strictly evaluated to False, NOT True via bool('false').
    - Works across VisionOcrMarkerItem, LabMarkerMutationItem, and LabObservationEntry.
    """
    # 1. VisionOcrMarkerItem
    m_false = VisionOcrMarkerItem(
        original_name="Калий",
        marker_key="potassium",
        raw_value="4.2",
        numeric_value=4.2,
        sample_date="2026-10-21",
        is_recognition_ambiguous="false",  # type: ignore[arg-type]
        is_uncertain="false",  # type: ignore[arg-type]
    )
    assert m_false.is_recognition_ambiguous is False
    assert m_false.is_uncertain is True  # evaluate_verification_requirement detects missing unit

    m_true = VisionOcrMarkerItem(
        original_name="Калий",
        marker_key="potassium",
        raw_value="4.2",
        numeric_value=4.2,
        sample_date="2026-10-21",
        is_recognition_ambiguous="true",  # type: ignore[arg-type]
        is_uncertain="true",  # type: ignore[arg-type]
    )
    assert m_true.is_recognition_ambiguous is True
    assert m_true.is_uncertain is True
    assert m_true.requires_user_verification is True

    # 2. LabMarkerMutationItem
    mut_false = LabMarkerMutationItem(
        marker_key="alt",
        display_name="ALT",
        value=25.0,
        is_recognition_ambiguous="false",  # type: ignore[arg-type]
        is_uncertain="false",  # type: ignore[arg-type]
    )
    assert mut_false.is_recognition_ambiguous is False
    assert mut_false.is_uncertain is False

    mut_true = LabMarkerMutationItem(
        marker_key="alt",
        display_name="ALT",
        value=25.0,
        is_recognition_ambiguous="true",  # type: ignore[arg-type]
        is_uncertain="true",  # type: ignore[arg-type]
    )
    assert mut_true.is_recognition_ambiguous is True
    assert mut_true.is_uncertain is True

    # 3. LabObservationEntry
    obs_false = LabObservationEntry(
        observation_id="obs_test_1",
        marker_key="alt",
        display_name="ALT",
        value="25.0",
        recorded_at="2026-10-21T12:00:00Z",
        is_recognition_ambiguous="false",  # type: ignore[arg-type]
        is_uncertain="false",  # type: ignore[arg-type]
    )
    assert obs_false.is_recognition_ambiguous is False
    assert obs_false.is_uncertain is False


def test_regression_unmarked_name_and_address_in_uncertainty_note():
    """
    Regression Test: Unmarked Russian and English names, addresses, and identifiers in uncertainty_note.
    - Verifies zero leakage of unmarked PII (names, street addresses, apartment numbers, IDs).
    - Maps strictly to safe fixed technical clinical descriptions.
    """
    # 1. Russian unlabelled name and street address with recognition blur keyword
    note_ru = "Неразборчиво пятно Сидоров Петр Сергеевич Санкт-Петербург Невский 10 кв 23"
    sanitized_ru = sanitize_uncertainty_note(note_ru)
    assert sanitized_ru == "Recognition ambiguity: unclear print or damaged document"
    assert "Сидоров" not in sanitized_ru
    assert "Петр" not in sanitized_ru
    assert "Невский" not in sanitized_ru
    assert "10" not in sanitized_ru
    assert "23" not in sanitized_ru

    # 2. Russian unlabelled name and address without any standard keywords
    note_ru_arbitrary = "Иванов Иван Москва Ленина 5"
    sanitized_arb = sanitize_uncertainty_note(note_ru_arbitrary)
    assert sanitized_arb == "Non-standard text value requiring verification"
    assert "Иванов" not in sanitized_arb
    assert "Москва" not in sanitized_arb
    assert "Ленина" not in sanitized_arb

    # 3. English unlabelled name and street address
    note_en = "Blur near John Doe 123 Main Street Suite 400 Boston MA 02101"
    sanitized_en = sanitize_uncertainty_note(note_en)
    assert sanitized_en == "Recognition ambiguity: unclear print or damaged document"
    assert "John Doe" not in sanitized_en
    assert "Main Street" not in sanitized_en
    assert "Boston" not in sanitized_en


def test_regression_uncertainty_propagation_across_primary_weekly_revision(db_session: Session):
    """
    Regression Test 5: Uncertainty propagation across consilium modules.
    - Primary Consilium receives is_uncertain, uncertainty_note, and is_recognition_ambiguous.
    - Weekly Consilium isolates unreviewed pending labs with intact uncertainty fields.
    - Lab Revision Consilium preserves uncertainty fields across ledger upserts and diffs.
    """
    from app.services.primary_consilium_service import PrimaryConsiliumService
    from app.services.weekly_consilium_service import WeeklyConsiliumService

    cycle_repo = MedicalCycleRepository(db_session)
    cycle = cycle_repo.create(cycle_id="test-cycle-propagation-01", status="active")

    service = LabRevisionConsiliumService(db=db_session)
    mutations_v1 = [
        LabMarkerMutationItem(
            marker_key="inr",
            display_name="INR",
            value="1.15",
            unit=None,
            sample_date="2026-10-10",
        ),
        LabMarkerMutationItem(
            marker_key="potassium",
            display_name="Potassium",
            value="4.7",
            unit=None,
            sample_date="2026-10-10",
            is_uncertain=True,
            uncertainty_note="Missing unit of measurement for numeric value on laboratory report",
            is_recognition_ambiguous=False,
        ),
    ]
    service.upsert_laboratory_markers(cycle.cycle_id, mutations_v1)

    # 1. Primary Consilium context pipeline
    primary_service = PrimaryConsiliumService(db=db_session)
    loaded_labs = primary_service._load_preparatory_labs(cycle.cycle_id)

    assert "potassium" in loaded_labs
    k_marker = loaded_labs["potassium"]
    assert k_marker["numeric_value"] == 4.7
    assert k_marker["unit"] is None
    assert k_marker["is_uncertain"] is True
    assert k_marker["is_recognition_ambiguous"] is False
    assert "Missing unit of measurement" in k_marker["uncertainty_note"]

    # 2. Simulate Primary Consilium evaluated ledger v1
    report_repo = ModelReportRepository(db_session)
    report_repo.create_next_version(
        cycle_id=cycle.cycle_id,
        model="gemini-3.1-pro",
        report_type="chief_physician_internal",
        privacy_level=PrivacyLevel.SERVER_ONLY,
        content={
            "evaluated_ledger_version": 1,
            "flash_quality_review": {"approved_for_publication": True},
        },
    )

    # 3. Add v2 mutations with new uncertain marker
    mutations_v2 = [
        LabMarkerMutationItem(
            marker_key="glucose",
            display_name="Glucose",
            value="5.9",
            unit=None,
            sample_date="2026-10-18",
            is_uncertain=True,
            uncertainty_note="Missing unit of measurement for numeric value on laboratory report",
            is_recognition_ambiguous=False,
        )
    ]
    service.upsert_laboratory_markers(cycle.cycle_id, mutations_v2)

    # 4. Weekly Consilium context pipeline
    latest_ctx = assemble_consilium_laboratory_context(db_session, cycle.cycle_id)
    eval_ctx = assemble_consilium_laboratory_context(db_session, cycle.cycle_id, ledger_version=1)

    assert "glucose" in latest_ctx.shared_labs_table
    assert "glucose" not in eval_ctx.shared_labs_table

    gl_marker = latest_ctx.shared_labs_table["glucose"]
    assert gl_marker["numeric_value"] == 5.9
    assert gl_marker["is_uncertain"] is True
    assert gl_marker["is_recognition_ambiguous"] is False
    assert "Missing unit of measurement" in gl_marker["uncertainty_note"]

    # 5. Lab Revision Consilium diff engine
    diff_report = service.compute_laboratory_diff(
        cycle_id=cycle.cycle_id,
        baseline_version=1,
        target_version=2,
    )
    assert len(diff_report.added_markers) == 1
    new_diff_marker = diff_report.added_markers[0]
    assert new_diff_marker.marker_key == "glucose"
    assert new_diff_marker.new_value == "5.9"
    assert new_diff_marker.new_unit is None


# ==============================================================================
# C-8 V3 FINAL CLASSIFICATION FIX REGRESSION TESTS
# ==============================================================================


def test_regression_numeric_with_unit_and_uncertainty_not_ocr_error(
    client: TestClient, db_session: Session, auth_headers: dict[str, str]
):
    """
    Review v3 Fix: numeric=5.5, unit=mmol/L, is_uncertain=True, without explicit recognition ambiguity.
    - Uncertainty regarding clinical status, reference range, or method must NOT be conflated with OCR error.
    - is_recognition_ambiguous is strictly False.
    - requires_user_verification is False.
    - Result is confirmed into active ledger observations.
    """
    marker = VisionOcrMarkerItem(
        original_name="Глюкоза плазмы",
        marker_key="glucose",
        raw_value="5.5",
        numeric_value=5.5,
        unit="mmol/L",
        sample_date="2026-10-21",
        is_uncertain=True,
        uncertainty_note="Пограничный референсный интервал лаборатории",
    )
    assert marker.is_recognition_ambiguous is False
    assert marker.requires_user_verification is False
    assert marker.is_uncertain is True
    assert marker.numeric_value == 5.5
    assert marker.unit == "mmol/L"

    # End-to-end intake verification
    cycle_repo = MedicalCycleRepository(db_session)
    cycle = cycle_repo.create(cycle_id="test-cycle-v3-clean-unit-01", status="active")

    ocr_payload = {
        "document_metadata": {"sample_date": "2026-10-21", "page_count": 1},
        "markers": [marker.model_dump()],
    }
    mock_gateway = _build_mock_gateway(ocr_payload)
    app.dependency_overrides[get_gemini_gateway] = lambda: mock_gateway

    resp = client.post(
        "/api/v1/cascade/intake/lab-images",
        data={"cycle_id": cycle.cycle_id},
        files=[("files", ("scan.jpg", io.BytesIO(_create_dummy_jpeg()), "image/jpeg"))],
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text
    res = resp.json()

    assert res["requires_verification_count"] == 0
    assert len(res["confirmed_markers"]) == 1
    assert len(res["unconfirmed_markers"]) == 0

    ledger_service = LabRevisionConsiliumService(db=db_session, gateway=mock_gateway)
    ledger_state = ledger_service.get_lab_ledger_state(cycle.cycle_id)
    assert "glucose" in ledger_state.active_markers
    obs = ledger_state.active_markers["glucose"]
    assert obs.numeric_value == 5.5
    assert obs.unit == "mmol/L"
    assert obs.is_uncertain is True
    assert obs.is_recognition_ambiguous is False


def test_regression_numeric_with_footnote_asterisk(
    client: TestClient, db_session: Session, auth_headers: dict[str, str]
):
    """
    Review v3 Fix: numeric=5.5, raw_value="5.5*", footnote reference.
    - Single footnote '*' on laboratory report blank must NOT invalidate legible number.
    - Successfully parsed to numeric_value=5.5.
    - is_recognition_ambiguous is strictly False.
    - Confirmed into active ledger.
    """
    marker = VisionOcrMarkerItem(
        original_name="Глюкоза плазмы",
        marker_key="glucose",
        raw_value="5.5*",
        numeric_value=5.5,
        unit="mmol/L",
        sample_date="2026-10-21",
        is_uncertain=True,
        uncertainty_note="* Исследование выполнено натощак",
    )
    assert marker.is_recognition_ambiguous is False
    assert marker.requires_user_verification is False
    assert marker.numeric_value == 5.5

    # Also test auto-parsing numeric_value from raw_value="5.5*" when numeric_value is None
    marker_auto = VisionOcrMarkerItem(
        original_name="Глюкоза плазмы",
        marker_key="glucose",
        raw_value="5.5*",
        numeric_value=None,
        unit="mmol/L",
        sample_date="2026-10-21",
    )
    assert marker_auto.numeric_value == 5.5
    assert marker_auto.is_recognition_ambiguous is False
    assert marker_auto.requires_user_verification is False

    cycle_repo = MedicalCycleRepository(db_session)
    cycle = cycle_repo.create(cycle_id="test-cycle-v3-footnote-01", status="active")

    ocr_payload = {
        "document_metadata": {"sample_date": "2026-10-21", "page_count": 1},
        "markers": [marker.model_dump()],
    }
    mock_gateway = _build_mock_gateway(ocr_payload)
    app.dependency_overrides[get_gemini_gateway] = lambda: mock_gateway

    resp = client.post(
        "/api/v1/cascade/intake/lab-images",
        data={"cycle_id": cycle.cycle_id},
        files=[("files", ("scan.jpg", io.BytesIO(_create_dummy_jpeg()), "image/jpeg"))],
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text
    res = resp.json()

    assert res["requires_verification_count"] == 0
    assert len(res["confirmed_markers"]) == 1

    ledger_service = LabRevisionConsiliumService(db=db_session, gateway=mock_gateway)
    ledger_state = ledger_service.get_lab_ledger_state(cycle.cycle_id)
    assert "glucose" in ledger_state.active_markers
    obs = ledger_state.active_markers["glucose"]
    assert obs.numeric_value == 5.5
    assert obs.is_recognition_ambiguous is False


def test_regression_raw_value_tilde_question_mark_true_ambiguity(
    client: TestClient, db_session: Session, auth_headers: dict[str, str]
):
    """
    Review v3 Fix: raw_value="~2.?" with true OCR recognition ambiguity.
    - Explicit ambiguous characters ('~', '?') denote illegible / doubtful OCR digits.
    - is_recognition_ambiguous is strictly True.
    - requires_user_verification is strictly True.
    - Isolated from active ledger numbers into unconfirmed_markers.
    - Even if numeric_value was guessed by an OCR model, it remains unconfirmed.
    """
    # 1. Without numeric value
    amb_item_1 = VisionOcrMarkerItem(
        original_name="Ферритин",
        marker_key="ferritin",
        raw_value="~2.?",
        numeric_value=None,
        unit="мкг/л",
        sample_date="2026-10-21",
        is_uncertain=True,
        uncertainty_note="Размыта цифра",
    )
    assert amb_item_1.is_recognition_ambiguous is True
    assert amb_item_1.requires_user_verification is True

    # 2. Even if numeric_value was guessed as 2.0
    amb_item_2 = VisionOcrMarkerItem(
        original_name="Ферритин",
        marker_key="ferritin",
        raw_value="~2.?",
        numeric_value=2.0,
        unit="мкг/л",
        sample_date="2026-10-21",
        is_uncertain=True,
    )
    assert amb_item_2.is_recognition_ambiguous is True
    assert amb_item_2.requires_user_verification is True

    cycle_repo = MedicalCycleRepository(db_session)
    cycle = cycle_repo.create(cycle_id="test-cycle-v3-true-amb-01", status="active")

    ocr_payload = {
        "document_metadata": {"sample_date": "2026-10-21", "page_count": 1},
        "markers": [amb_item_1.model_dump()],
    }
    mock_gateway = _build_mock_gateway(ocr_payload)
    app.dependency_overrides[get_gemini_gateway] = lambda: mock_gateway

    resp = client.post(
        "/api/v1/cascade/intake/lab-images",
        data={"cycle_id": cycle.cycle_id},
        files=[("files", ("scan.jpg", io.BytesIO(_create_dummy_jpeg()), "image/jpeg"))],
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text
    res = resp.json()

    assert res["requires_verification_count"] == 1
    assert len(res["confirmed_markers"]) == 0
    assert len(res["unconfirmed_markers"]) == 1

    ledger_service = LabRevisionConsiliumService(db=db_session, gateway=mock_gateway)
    ledger_state = ledger_service.get_lab_ledger_state(cycle.cycle_id)
    assert "ferritin" not in ledger_state.active_markers


def test_regression_explicit_is_recognition_ambiguous_priority():
    """
    Review v3 Fix: Explicit is_recognition_ambiguous has absolute priority over heuristics.
    - If True, always requires user verification.
    - If False, never classified as OCR recognition error even with is_uncertain=True.
    - String boolean values ('true', 'false') are safely parsed.
    """
    # Explicit False overrides any other uncertainty
    explicit_false = VisionOcrMarkerItem(
        original_name="Тест",
        marker_key="test_marker",
        raw_value="5.5",
        numeric_value=5.5,
        unit="mmol/L",
        sample_date="2026-10-21",
        is_uncertain=True,
        is_recognition_ambiguous=False,
    )
    assert explicit_false.is_recognition_ambiguous is False
    assert explicit_false.requires_user_verification is False

    # Explicit True forces user verification
    explicit_true = VisionOcrMarkerItem(
        original_name="Тест",
        marker_key="test_marker",
        raw_value="5.5",
        numeric_value=5.5,
        unit="mmol/L",
        sample_date="2026-10-21",
        is_uncertain=False,
        is_recognition_ambiguous=True,
    )
    assert explicit_true.is_recognition_ambiguous is True
    assert explicit_true.requires_user_verification is True

    # String representations
    str_false = VisionOcrMarkerItem(
        original_name="Тест",
        marker_key="test_marker",
        raw_value="5.5",
        numeric_value=5.5,
        unit="mmol/L",
        sample_date="2026-10-21",
        is_recognition_ambiguous="false",  # type: ignore[arg-type]
    )
    assert str_false.is_recognition_ambiguous is False
    assert str_false.requires_user_verification is False

    str_true = VisionOcrMarkerItem(
        original_name="Тест",
        marker_key="test_marker",
        raw_value="5.5",
        numeric_value=5.5,
        unit="mmol/L",
        sample_date="2026-10-21",
        is_recognition_ambiguous="true",  # type: ignore[arg-type]
    )
    assert str_true.is_recognition_ambiguous is True
    assert str_true.requires_user_verification is True


def test_regression_repeated_pydantic_validation_v3():
    """
    Review v3 Fix: Repeated Pydantic serialization and validation cycle.
    - Clean number with unit, footnote marker, and ambiguous marker
      strictly preserve their exact states across repeated serialization cycles.
    """
    items = [
        # 1. Clean number with unit and uncertainty
        VisionOcrMarkerItem(
            original_name="Глюкоза",
            marker_key="glucose",
            raw_value="5.5",
            numeric_value=5.5,
            unit="mmol/L",
            sample_date="2026-10-21",
            is_uncertain=True,
        ),
        # 2. Footnote marker
        VisionOcrMarkerItem(
            original_name="Глюкоза",
            marker_key="glucose",
            raw_value="5.5*",
            numeric_value=5.5,
            unit="mmol/L",
            sample_date="2026-10-21",
        ),
        # 3. Truly ambiguous marker
        VisionOcrMarkerItem(
            original_name="Ферритин",
            marker_key="ferritin",
            raw_value="~2.?",
            numeric_value=None,
            unit="мкг/л",
            sample_date="2026-10-21",
            is_uncertain=True,
        ),
    ]

    for item in items:
        expected_ambiguous = item.is_recognition_ambiguous
        expected_req_ver = item.requires_user_verification
        expected_unc = item.is_uncertain

        current = item
        for _ in range(5):
            dumped = current.model_dump()
            revalidated = VisionOcrMarkerItem.model_validate(dumped)
            assert revalidated.is_recognition_ambiguous is expected_ambiguous
            assert revalidated.requires_user_verification is expected_req_ver
            assert revalidated.is_uncertain is expected_unc
            current = revalidated


def test_regression_c8_safety_invariant_contradictory_unreadable_with_explicit_false():
    """
    C-8 Final Safety Invariant:
    - Explicit is_recognition_ambiguous=False must NOT cancel objectively unreadable string ('~2.?', '5.?', '??').
    - Even if numeric_value was guessed by OCR model (e.g. 2.0 or 5.0).
    - String boolean values like 'false' or '0' must also NOT cancel unreadability.
    """
    # 1. '~2.?' with numeric_value=2.0 and explicit False
    item_tilde = VisionOcrMarkerItem(
        original_name="Ферритин",
        marker_key="ferritin",
        raw_value="~2.?",
        numeric_value=2.0,
        unit="мкг/л",
        sample_date="2026-10-21",
        is_recognition_ambiguous=False,
    )
    assert item_tilde.is_recognition_ambiguous is True
    assert item_tilde.requires_user_verification is True
    assert item_tilde.is_uncertain is True

    # 2. '5.?' with numeric_value=5.0 and explicit string 'false'
    item_qmark = VisionOcrMarkerItem(
        original_name="Глюкоза",
        marker_key="glucose",
        raw_value="5.?",
        numeric_value=5.0,
        unit="mmol/L",
        sample_date="2026-10-21",
        is_recognition_ambiguous="false",
    )
    assert item_qmark.is_recognition_ambiguous is True
    assert item_qmark.requires_user_verification is True
    assert item_qmark.is_uncertain is True

    # 3. '??' with numeric_value=None and explicit False
    item_pure_q = VisionOcrMarkerItem(
        original_name="Билирубин",
        marker_key="bilirubin",
        raw_value="??",
        numeric_value=None,
        unit="мкмоль/л",
        sample_date="2026-10-21",
        is_recognition_ambiguous=False,
    )
    assert item_pure_q.is_recognition_ambiguous is True
    assert item_pure_q.requires_user_verification is True
    assert item_pure_q.is_uncertain is True


def test_regression_c8_safety_invariant_contradictory_clean_number_with_explicit_true():
    """
    C-8 Final Safety Invariant:
    - Explicit is_recognition_ambiguous=True ALWAYS preserves blocking/lock.
    - Even if raw_value is a clean legible number with unit and no uncertainty.
    """
    item_true = VisionOcrMarkerItem(
        original_name="Глюкоза",
        marker_key="glucose",
        raw_value="5.5",
        numeric_value=5.5,
        unit="mmol/L",
        sample_date="2026-10-21",
        is_uncertain=False,
        is_recognition_ambiguous=True,
    )
    assert item_true.is_recognition_ambiguous is True
    assert item_true.requires_user_verification is True
    assert item_true.is_uncertain is True

    # Also with string 'true'
    item_str_true = VisionOcrMarkerItem(
        original_name="Глюкоза",
        marker_key="glucose",
        raw_value="5.5",
        numeric_value=5.5,
        unit="mmol/L",
        sample_date="2026-10-21",
        is_uncertain=False,
        is_recognition_ambiguous="true",
    )
    assert item_str_true.is_recognition_ambiguous is True
    assert item_str_true.requires_user_verification is True
    assert item_str_true.is_uncertain is True


def test_regression_c8_safety_invariant_footnote_vs_unreadable_digits():
    """
    C-8 Final Safety Invariant:
    - Footnotes like '5.5*' are NOT confused with unreadable digits.
    - Autoparsed numeric_value cleanly strips '*' to 5.5.
    - Does NOT trigger recognition ambiguity or require user verification.
    """
    item_footnote = VisionOcrMarkerItem(
        original_name="Глюкоза",
        marker_key="glucose",
        raw_value="5.5*",
        unit="mmol/L",
        sample_date="2026-10-21",
        is_recognition_ambiguous=False,
    )
    assert item_footnote.numeric_value == 5.5
    assert item_footnote.is_recognition_ambiguous is False
    assert item_footnote.requires_user_verification is False


def test_regression_c8_safety_invariant_repeated_validation_contradictory_matrix():
    """
    C-8 Final Safety Invariant:
    - Repeated Pydantic serialization / deserialization (5 cycles) on contradictory inputs.
    - Confirms that all states remain strictly invariant across repeated passes.
    """
    items = [
        # Contradictory: unreadable raw with numeric guess and explicit False
        VisionOcrMarkerItem(
            original_name="Ферритин",
            marker_key="ferritin",
            raw_value="~2.?",
            numeric_value=2.0,
            unit="мкг/л",
            sample_date="2026-10-21",
            is_recognition_ambiguous=False,
        ),
        # Contradictory: partial unreadable raw with numeric guess and string 'false'
        VisionOcrMarkerItem(
            original_name="Глюкоза",
            marker_key="glucose",
            raw_value="5.?",
            numeric_value=5.0,
            unit="mmol/L",
            sample_date="2026-10-21",
            is_recognition_ambiguous="false",
        ),
        # Contradictory: pure unreadable question marks with explicit False
        VisionOcrMarkerItem(
            original_name="Билирубин",
            marker_key="bilirubin",
            raw_value="??",
            numeric_value=None,
            unit="мкмоль/л",
            sample_date="2026-10-21",
            is_recognition_ambiguous=False,
        ),
        # Contradictory: clean number with explicit True
        VisionOcrMarkerItem(
            original_name="Глюкоза",
            marker_key="glucose",
            raw_value="5.5",
            numeric_value=5.5,
            unit="mmol/L",
            sample_date="2026-10-21",
            is_uncertain=False,
            is_recognition_ambiguous=True,
        ),
        # Clean footnote marker with explicit False
        VisionOcrMarkerItem(
            original_name="Глюкоза",
            marker_key="glucose",
            raw_value="5.5*",
            numeric_value=5.5,
            unit="mmol/L",
            sample_date="2026-10-21",
            is_recognition_ambiguous=False,
        ),
        # Clean number with is_uncertain=True and known unit
        VisionOcrMarkerItem(
            original_name="Глюкоза",
            marker_key="glucose",
            raw_value="5.5",
            numeric_value=5.5,
            unit="mmol/L",
            sample_date="2026-10-21",
            is_uncertain=True,
            is_recognition_ambiguous=False,
        ),
    ]

    for item in items:
        expected_ambiguous = item.is_recognition_ambiguous
        expected_req_ver = item.requires_user_verification
        expected_unc = item.is_uncertain
        expected_num = item.numeric_value

        current = item
        for _ in range(5):
            dumped = current.model_dump()
            revalidated = VisionOcrMarkerItem.model_validate(dumped)
            assert revalidated.is_recognition_ambiguous is expected_ambiguous
            assert revalidated.requires_user_verification is expected_req_ver
            assert revalidated.is_uncertain is expected_unc
            assert revalidated.numeric_value == expected_num
            current = revalidated


def test_regression_c8_safety_invariant_end_to_end_contradictory_intake(
    client: TestClient, db_session: Session, auth_headers: dict[str, str]
):
    """
    C-8 Final Safety Invariant:
    - Intake endpoint strictly isolates contradictory item ('~2.?', numeric=2.0, is_recognition_ambiguous=False)
      into unconfirmed_markers.
    - Active ledger is NOT populated with the ambiguous guess.
    """
    cycle_repo = MedicalCycleRepository(db_session)
    cycle = cycle_repo.create(cycle_id="test-cycle-safety-invariant-01", status="active")

    contradictory_marker = VisionOcrMarkerItem(
        original_name="Ферритин",
        marker_key="ferritin",
        raw_value="~2.?",
        numeric_value=2.0,
        unit="мкг/л",
        sample_date="2026-10-21",
        is_recognition_ambiguous=False,
    )

    ocr_payload = {
        "document_metadata": {"sample_date": "2026-10-21", "page_count": 1},
        "markers": [contradictory_marker.model_dump()],
    }
    mock_gateway = _build_mock_gateway(ocr_payload)
    app.dependency_overrides[get_gemini_gateway] = lambda: mock_gateway

    resp = client.post(
        "/api/v1/cascade/intake/lab-images",
        data={"cycle_id": cycle.cycle_id},
        files=[("files", ("scan.jpg", io.BytesIO(_create_dummy_jpeg()), "image/jpeg"))],
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text
    res = resp.json()

    # Must be isolated into unconfirmed_markers, zero confirmed
    assert res["requires_verification_count"] == 1
    assert len(res["confirmed_markers"]) == 0
    assert len(res["unconfirmed_markers"]) == 1

    unconfirmed = res["unconfirmed_markers"][0]
    assert unconfirmed["marker_key"] == "ferritin"
    assert unconfirmed["raw_value"] == "~2.?"
    assert unconfirmed["numeric_value"] == 2.0
    assert unconfirmed["is_recognition_ambiguous"] is True
    assert unconfirmed["requires_user_verification"] is True

    # Active ledger in database must NOT contain the unconfirmed marker
    ledger_state = LabRevisionConsiliumService(db=db_session).get_lab_ledger_state(cycle.cycle_id)
    assert "ferritin" not in ledger_state.active_markers
    assert ledger_state.ledger_version == 0
