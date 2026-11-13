import json
import pytest
from pydantic import ValidationError

from app.config import Settings
from app.schemas.consilium import (
    LabReferenceRange,
    VisionOcrMarkerItem,
    VisionOcrDocumentMetadata,
    VisionOcrModelOutput,
    LabVisionIntakeClientResponse,
    VisionOcrPageExtraction,
    VisionOcrPriorBatchContext,
    VisionOcrBatchReconciliationOutput,
    EcgRecordingType,
)
from app.services.consilium_prompts import (
    build_lab_vision_ocr_prompt,
    build_lab_vision_ocr_page_prompt,
    build_lab_vision_ocr_batch_prompt,
    build_vision_ecg_prompt,
)


def test_config_lab_ocr_defaults_and_validation():
    """Verifies that Lab OCR v2 settings are properly configured with default 6."""
    settings = Settings()
    # Batch size and concurrency defaults to 6
    assert settings.lab_ocr_max_batch_size == 6
    assert settings.lab_ocr_concurrency == 6
    # Global gemini_vision_model must remain gemini-3.8-flash for ECG
    assert settings.gemini_vision_model == "gemini-3.8-flash"
    assert settings.gemini_flash_lite_model == "gemini-3.5-flash-lite"
    assert settings.gemini_flash_model == "gemini-3.8-flash"

    # Validation limits
    with pytest.raises(ValidationError):
        Settings(AEGIS_LAB_OCR_MAX_BATCH_SIZE=0)
    with pytest.raises(ValidationError):
        Settings(AEGIS_LAB_OCR_CONCURRENCY=0)


def test_vision_ocr_page_extraction_schema():
    """Tests Stage 1 VisionOcrPageExtraction schema validation and auto-normalization."""
    payload = {
        "page_number": 1,
        "source_filename": "invitro_report_p1.jpg",
        "source_file_id": "file_uuid_001",
        "page_sample_date": "15.10.2026",  # DD.MM.YYYY normalization
        "page_issue_date": "16.10.2026",
        "laboratory_name": "Invitro",
        "order_id": "INV-98214",
        "has_unreadable_regions": "false",  # strict bool parsing
        "unreadable_regions_note": None,
        "markers": [
            {
                "original_name": "Гемоглобин",
                "marker_key": "hemoglobin",
                "raw_value": "145",
                "numeric_value": 145.0,
                "unit": "г/л",
                "reference_range": {"low": 120.0, "high": 160.0, "text": "120 - 160"},
                "sample_date": "15.10.2026",
                "category": "blood_count",
            }
        ],
    }

    extraction = VisionOcrPageExtraction.model_validate(payload)
    assert extraction.page_number == 1
    assert extraction.source_filename == "invitro_report_p1.jpg"
    assert extraction.page_sample_date == "2026-10-15"  # Normalized
    assert extraction.page_issue_date == "2026-10-16"   # Normalized
    assert extraction.laboratory_name == "Invitro"
    assert extraction.has_unreadable_regions is False
    assert len(extraction.markers) == 1

    marker = extraction.markers[0]
    assert marker.marker_key == "hemoglobin"
    assert marker.sample_date == "2026-10-15"
    assert marker.row_id == "p1_r1"  # Auto-assigned row_id
    assert marker.page_number == 1
    assert marker.source_filename == "invitro_report_p1.jpg"


def test_vision_ocr_page_extraction_indicators_alias_and_extra_forbid():
    """Tests normalization of 'indicators' alias and extra fields rejection."""
    payload = {
        "page_number": 2,
        "source_filename": "page2.png",
        "indicators": [
            {
                "original_name": "АЛТ",
                "marker_key": "alt",
                "raw_value": "24",
                "numeric_value": 24.0,
                "unit": "Ед/л",
            }
        ],
    }
    extraction = VisionOcrPageExtraction.model_validate(payload)
    assert len(extraction.markers) == 1
    assert extraction.markers[0].marker_key == "alt"
    assert extraction.markers[0].row_id == "p2_r1"

    # Extra fields rejected by extra="forbid"
    with pytest.raises(ValidationError):
        VisionOcrPageExtraction.model_validate({**payload, "unauthorized_extra_field": "bad"})


def test_vision_ocr_prior_batch_context_schema():
    """Tests VisionOcrPriorBatchContext schema and date normalization."""
    ctx = VisionOcrPriorBatchContext(
        laboratory_name="Gemotest",
        order_id="GEMO-1122",
        default_sample_date="20.10.2026",
        already_seen_marker_keys=["hemoglobin", "wbc", "platelets"],
        already_seen_row_ids=["p1_r1", "p1_r2", "p1_r3"],
        table_continuation_hints="Biochemistry table continues on next page",
        last_processed_page=6,
    )
    assert ctx.laboratory_name == "Gemotest"
    assert ctx.default_sample_date == "2026-10-20"
    assert len(ctx.already_seen_marker_keys) == 3
    assert len(ctx.already_seen_row_ids) == 3
    assert ctx.last_processed_page == 6


def test_vision_ocr_batch_reconciliation_output_schema():
    """Tests Stage 2 batch reconciliation output schema with provenance."""
    batch_data = {
        "batch_index": 1,
        "batch_page_count": 6,
        "processed_page_numbers": [1, 2, 3, 4, 5, 6],
        "document_metadata": {
            "sample_date": "15.10.2026",
            "laboratory_name": "Invitro",
            "page_count": 6,
            "order_id": "ORD-12345",
        },
        "reconciled_markers": [
            {
                "row_id": "rec_001",
                "source_row_ids": ["p1_r12", "p2_r1"],  # Split table row stitched across pages 1 and 2
                "original_name": "Билирубин общий",
                "marker_key": "bilirubin_total",
                "raw_value": "18.2",
                "numeric_value": 18.2,
                "unit": "мкмоль/л",
                "reference_range": {"low": 3.4, "high": 20.5, "text": "3.4 - 20.5"},
                "sample_date": "15.10.2026",
                "category": "biochemistry",
            }
        ],
        "cross_page_notes": "Stitched bilirubin total row across page 1 bottom and page 2 top",
    }

    out = VisionOcrBatchReconciliationOutput.model_validate(batch_data)
    assert out.batch_index == 1
    assert out.batch_page_count == 6
    assert out.processed_page_numbers == [1, 2, 3, 4, 5, 6]
    assert out.document_metadata.sample_date == "2026-10-15"
    assert len(out.reconciled_markers) == 1

    marker = out.reconciled_markers[0]
    assert marker.marker_key == "bilirubin_total"
    assert marker.source_row_ids == ["p1_r12", "p2_r1"]
    assert marker.requires_user_verification is False


def test_identity_preservation_for_identical_marker_keys():
    """
    CRITICAL MEDICAL TEST:
    Ensures that identical marker names or keys from different pages or time points
    (e.g., dynamic glucose tolerance or repeat panels) do NOT overwrite each other,
    and distinct row identity is strictly preserved.
    """
    page1_marker = VisionOcrMarkerItem(
        row_id="p1_r1",
        page_number=1,
        source_filename="doc_page1.png",
        original_name="Глюкоза (натощак)",
        marker_key="glucose",
        raw_value="4.8",
        numeric_value=4.8,
        unit="ммоль/л",
        reference_range=LabReferenceRange(low=4.1, high=5.9, text="4.1 - 5.9"),
        sample_date="2026-10-15",
    )

    page2_marker = VisionOcrMarkerItem(
        row_id="p2_r3",
        page_number=2,
        source_filename="doc_page2.png",
        original_name="Глюкоза (через 2 часа после нагрузки)",
        marker_key="glucose",
        raw_value="7.2",
        numeric_value=7.2,
        unit="ммоль/л",
        reference_range=LabReferenceRange(low=4.1, high=7.8, text="4.1 - 7.8"),
        sample_date="2026-10-15",
    )

    # Both share marker_key="glucose", but have distinct row_id and clinical identity
    assert page1_marker.marker_key == page2_marker.marker_key == "glucose"
    assert page1_marker.row_id != page2_marker.row_id
    assert page1_marker.page_number == 1
    assert page2_marker.page_number == 2
    assert page1_marker.raw_value == "4.8"
    assert page2_marker.raw_value == "7.2"

    # Reconciled output holds both distinct markers with their respective source provenance
    rec_marker_1 = page1_marker.model_copy()
    rec_marker_1.source_row_ids = ["p1_r1"]
    rec_marker_2 = page2_marker.model_copy()
    rec_marker_2.source_row_ids = ["p2_r3"]

    batch_out = VisionOcrBatchReconciliationOutput(
        batch_index=1,
        batch_page_count=2,
        processed_page_numbers=[1, 2],
        reconciled_markers=[rec_marker_1, rec_marker_2],
    )

    assert len(batch_out.reconciled_markers) == 2
    assert batch_out.reconciled_markers[0].source_row_ids == ["p1_r1"]
    assert batch_out.reconciled_markers[1].source_row_ids == ["p2_r3"]
    assert batch_out.reconciled_markers[0].numeric_value == 4.8
    assert batch_out.reconciled_markers[1].numeric_value == 7.2


def test_sample_date_and_uncertainty_handling():
    """Tests Russian date normalization, unreadable digits, and qualitative pattern detection."""
    # 1. Unclear print with ambiguous character triggers verification
    ambig_item = VisionOcrMarkerItem(
        original_name="Креатинин",
        marker_key="creatinine",
        raw_value="~8?",  # Smudge with '?'
        sample_date="15.10.2026",
    )
    assert ambig_item.is_recognition_ambiguous is True
    assert ambig_item.requires_user_verification is True
    assert ambig_item.sample_date == "2026-10-15"

    # 2. Qualitative marker without unit is valid without error
    qual_item = VisionOcrMarkerItem(
        original_name="RW (реакция Вассермана)",
        marker_key="syphilis_rw",
        raw_value="отрицательно",
        sample_date="15.10.2026",
    )
    assert qual_item.is_recognition_ambiguous is False
    assert qual_item.requires_user_verification is False
    assert qual_item.numeric_value is None

    # 3. Page with unreadable regions
    page_extr = VisionOcrPageExtraction(
        page_number=1,
        source_filename="torn_page.jpg",
        has_unreadable_regions=True,
        unreadable_regions_note="Bottom right corner torn off",
        markers=[qual_item],
    )
    assert page_extr.has_unreadable_regions is True
    assert page_extr.unreadable_regions_note == "Bottom right corner torn off"


def test_prompt_builders_content_and_prohibitions():
    """Verifies that both prompt builders enforce mandatory clinical directives and prohibitions."""
    # 1. Stage 1 Page Prompt
    p1 = build_lab_vision_ocr_page_prompt(page_number=3, total_pages=10, filename="report_p3.pdf")
    assert "Page 3 of 10" in p1
    assert 'source: "report_p3.pdf"' in p1
    # Prohibitions
    assert "NEVER hallucinate or guess missing values" in p1
    assert "NEVER alter printed laboratory references" in p1
    assert "NEVER discard qualitative findings" in p1
    assert "NEVER confuse specimen collection date" in p1
    assert "NEVER make clinical diagnoses" in p1
    assert "row_id" in p1
    assert "VisionOcrPageExtraction" in p1

    # 2. Stage 2 Batch Prompt
    prior_ctx = VisionOcrPriorBatchContext(
        laboratory_name="Helix",
        order_id="HLX-777",
        default_sample_date="2026-10-15",
        already_seen_marker_keys=["hemoglobin", "wbc"],
        already_seen_row_ids=["p1_r1", "p1_r2"],
    )
    p2 = build_lab_vision_ocr_batch_prompt(
        batch_index=2,
        total_batches=4,
        page_numbers=[7, 8, 9, 10, 11, 12],
        prior_context=prior_ctx,
    )
    assert "reconciling Batch 2 of 4" in p2
    assert "Pages: [7, 8, 9, 10, 11, 12]" in p2
    assert "PRIOR BATCH CONTEXT" in p2
    assert "Helix" in p2
    assert "HLX-777" in p2
    # Directives
    assert "ZERO SILENT DROPPING" in p2
    assert "source_row_ids" in p2
    assert "NEVER silently alter or modify raw values" in p2
    assert "NEVER formulate clinical diagnoses" in p2
    assert "Identical indicator names or marker_keys DO NOT automatically imply duplicates" in p2
    assert "VisionOcrBatchReconciliationOutput" in p2


def test_backward_compatibility_existing_schemas():
    """
    CRITICAL REGRESSION TEST:
    Verifies that legacy v1 OCR output instantiations without row_id or source_row_ids
    continue to validate perfectly with zero breaking changes.
    """
    legacy_marker = VisionOcrMarkerItem(
        original_name="Аланинаминотрансфераза (АЛТ)",
        marker_key="alt",
        raw_value="24.5",
        numeric_value=24.5,
        unit="Ед/л",
        reference_range=LabReferenceRange(low=0.0, high=41.0, text="0 - 41"),
        sample_date="2026-10-15",
        category="biochemistry",
    )
    assert legacy_marker.row_id is None
    assert legacy_marker.source_row_ids == []
    assert legacy_marker.page_number is None
    assert legacy_marker.source_filename is None

    legacy_output = VisionOcrModelOutput(
        document_metadata=VisionOcrDocumentMetadata(
            sample_date="2026-10-15",
            laboratory_name="Invitro",
            page_count=1,
        ),
        markers=[legacy_marker],
    )
    assert len(legacy_output.markers) == 1
    assert legacy_output.document_metadata.sample_date == "2026-10-15"

    client_resp = LabVisionIntakeClientResponse(
        cycle_id="cycle_test_001",
        status="processed",
        ledger_version=1,
        extracted_count=1,
        document_metadata=legacy_output.document_metadata,
        markers=legacy_output.markers,
        confirmed_markers=[legacy_marker],
        unconfirmed_markers=[],
        has_unreviewed_lab_changes=True,
    )
    assert client_resp.extracted_count == 1
    assert client_resp.ledger_version == 1


def test_ecg_components_strictly_untouched():
    """Verifies that ECG prompts, roles, and schemas remain completely untouched."""
    ecg_prompt = build_vision_ecg_prompt()
    assert "Electrocardiogram Vision Digitization & Recognition Engine" in ecg_prompt
    assert "single_lead_smartwatch" in ecg_prompt
    assert "standard_12_lead" in ecg_prompt

    assert EcgRecordingType.SINGLE_LEAD_SMARTWATCH == "single_lead_smartwatch"
    assert EcgRecordingType.STANDARD_12_LEAD == "standard_12_lead"
