import asyncio
import io
import pytest
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock
from sqlalchemy.orm import Session
import pypdf

from app.config import settings
from app.schemas.consilium import (
    VisionOcrPageExtraction,
    VisionOcrMarkerItem,
    VisionOcrReconciliationOutput,
    VisionOcrStudyLinkage,
    VisionOcrDocumentMetadata,
    LabReferenceRange,
    EcgRecordingType,
)
from app.services.gemini_gateway import (
    GeminiGateway,
    GeminiModelRole,
    GeminiGatewayError,
)
from app.services.pdf_extraction_service import (
    PreparedDocumentPage,
    PdfExtractionService,
    LabVisionValidationError,
)
from app.services.lab_vision_ocr_service import LabVisionOcrService
from app.services.consilium_prompts import (
    build_lab_vision_ocr_page_prompt,
    build_lab_vision_ocr_reconciliation_prompt,
    build_vision_ecg_prompt,
)


class MockCascadeGateway:
    """Mock GeminiGateway supporting both Flash-Lite (Stage 1) and Flash (Stage 2 Reconciler)."""

    def __init__(
        self,
        reconciliation_error: bool = False,
        reconciliation_drop_marker_keys: Optional[List[str]] = None,
        reconciliation_subvert_marker_keys: Optional[Dict[str, str]] = None,
    ):
        self.recorded_calls: List[Dict[str, Any]] = []
        self.reconciliation_error = reconciliation_error
        self.reconciliation_drop_marker_keys = reconciliation_drop_marker_keys or []
        self.reconciliation_subvert_marker_keys = reconciliation_subvert_marker_keys or {}

    def resolve_model_name(self, role: Any) -> str:
        if role == GeminiModelRole.FLASH_LITE:
            return "gemini-3.5-flash-lite"
        if role == GeminiModelRole.FLASH:
            return "gemini-3.8-flash"
        if role == GeminiModelRole.VISION:
            return "gemini-3.8-flash"
        return str(role)

    async def generate_structured(
        self,
        prompt: str,
        response_schema: Any,
        model: Any = GeminiModelRole.FLASH_LITE,
        inline_images: Optional[List[Dict[str, str]]] = None,
        max_output_tokens: int = 16384,
        **kwargs,
    ) -> Any:
        call_record = {
            "model": model,
            "prompt": prompt,
            "inline_images": inline_images,
            "response_schema": response_schema.__name__ if hasattr(response_schema, "__name__") else str(response_schema),
        }
        self.recorded_calls.append(call_record)

        if model == GeminiModelRole.FLASH_LITE:
            # Stage 1: Page-level extraction
            # Detect page number from prompt
            page_num = 1
            for part in prompt.split():
                if part.isdigit():
                    page_num = int(part)
                    break

            markers = [
                VisionOcrMarkerItem(
                    row_id=f"p{page_num}_r1",
                    page_number=page_num,
                    source_filename=f"doc_page_{page_num}.png",
                    original_name=f"Маркер {page_num}",
                    marker_key=f"marker_{page_num}",
                    raw_value="142",
                    numeric_value=142.0,
                    unit="г/л",
                    reference_range=LabReferenceRange(low=120.0, high=160.0, text="120 - 160"),
                    sample_date="2026-10-15",
                    category="blood_count",
                )
            ]

            return VisionOcrPageExtraction(
                page_number=page_num,
                source_filename=f"doc_page_{page_num}.png",
                laboratory_name="Invitro",
                page_sample_date="2026-10-15",
                order_id="INV-12345",
                order_id_type="order_number",
                printed_page_num=page_num,
                table_state="START" if page_num == 1 else "CONTINUATION",
                section_name="Общий анализ крови",
                markers=markers,
            )

        elif model == GeminiModelRole.FLASH:
            # Stage 2: Single document-level reconciliation
            if self.reconciliation_error:
                raise GeminiGatewayError("Simulated Gemini Flash 500 server error during reconciliation")

            # Parse input prompt to discover stage 1 markers
            reconciled_markers = []
            # Gather calls made to Flash-Lite so far
            flash_lite_calls = [c for c in self.recorded_calls if c["model"] == GeminiModelRole.FLASH_LITE]
            for idx, _ in enumerate(flash_lite_calls, start=1):
                m_key = f"marker_{idx}"
                if m_key in self.reconciliation_drop_marker_keys:
                    # Simulate Flash dropping this marker
                    continue

                raw_val = "142"
                num_val = 142.0
                if m_key in self.reconciliation_subvert_marker_keys:
                    # Simulate Flash altering value
                    raw_val = self.reconciliation_subvert_marker_keys[m_key]
                    try:
                        num_val = float(raw_val)
                    except ValueError:
                        num_val = None

                reconciled_markers.append(
                    VisionOcrMarkerItem(
                        row_id=f"rec_r{idx}",
                        source_row_ids=[f"p{idx}_r1"],
                        page_number=idx,
                        source_filename=f"doc_page_{idx}.png",
                        original_name=f"Маркер {idx}",
                        marker_key=m_key,
                        raw_value=raw_val,
                        numeric_value=num_val,
                        unit="г/л",
                        reference_range=LabReferenceRange(low=120.0, high=160.0, text="120 - 160"),
                        sample_date="2026-10-15",
                        category="blood_count",
                    )
                )

            return VisionOcrReconciliationOutput(
                total_pages_reconciled=len(flash_lite_calls),
                document_metadata=VisionOcrDocumentMetadata(
                    sample_date="2026-10-15",
                    laboratory_name="Invitro",
                    order_id="INV-12345",
                    page_count=len(flash_lite_calls),
                ),
                reconciled_markers=reconciled_markers,
                study_linkages=[
                    VisionOcrStudyLinkage(
                        study_id="INV-12345",
                        laboratory_name="Invitro",
                        sample_date="2026-10-15",
                        page_numbers=list(range(1, len(flash_lite_calls) + 1)),
                        confidence="high",
                    )
                ],
                reconciliation_notes="Reconciled document across all pages",
            )

        raise ValueError(f"Unexpected model: {model}")


def _create_pages(count: int) -> List[PreparedDocumentPage]:
    return [
        PreparedDocumentPage(
            page_id=f"page_{i}",
            source_filename=f"doc_page_{i}.png",
            file_index=0,
            page_index_in_file=i,
            global_page_number=i,
            mime_type="image/png",
            sha256_hash=f"hash_{i:064d}",
            raw_bytes=b"\x89PNG\r\n\x1a\n" + b"\x00" * 32,
        )
        for i in range(1, count + 1)
    ]


# --- Scenario 1: 1 page -> 1 Flash-Lite + 1 Flash (2 calls) ---

@pytest.mark.anyio
async def test_one_page_calls():
    mock_gateway = MockCascadeGateway()
    mock_db = MagicMock(spec=Session)
    service = LabVisionOcrService(db=mock_db, gateway=mock_gateway)

    pages = _create_pages(1)
    res = await service.process_document_v2(pages)

    flash_lite_calls = [c for c in mock_gateway.recorded_calls if c["model"] == GeminiModelRole.FLASH_LITE]
    flash_calls = [c for c in mock_gateway.recorded_calls if c["model"] == GeminiModelRole.FLASH]

    assert len(flash_lite_calls) == 1
    assert len(flash_calls) == 1
    assert len(mock_gateway.recorded_calls) == 2
    assert res.total_pages_reconciled == 1
    assert len(res.reconciled_markers) == 1


# --- Scenario 2: 6 pages -> 6 Flash-Lite + 1 Flash (7 calls) ---

@pytest.mark.anyio
async def test_six_pages_calls():
    mock_gateway = MockCascadeGateway()
    mock_db = MagicMock(spec=Session)
    service = LabVisionOcrService(db=mock_db, gateway=mock_gateway)

    pages = _create_pages(6)
    res = await service.process_document_v2(pages)

    flash_lite_calls = [c for c in mock_gateway.recorded_calls if c["model"] == GeminiModelRole.FLASH_LITE]
    flash_calls = [c for c in mock_gateway.recorded_calls if c["model"] == GeminiModelRole.FLASH]

    assert len(flash_lite_calls) == 6
    assert len(flash_calls) == 1
    assert len(mock_gateway.recorded_calls) == 7
    assert res.total_pages_reconciled == 6
    assert len(res.reconciled_markers) == 6


# --- Scenario 3: 7 pages -> 7 Flash-Lite (6 + 1) + 1 Flash (8 calls) ---

@pytest.mark.anyio
async def test_seven_pages_calls():
    mock_gateway = MockCascadeGateway()
    mock_db = MagicMock(spec=Session)
    service = LabVisionOcrService(db=mock_db, gateway=mock_gateway)

    pages = _create_pages(7)
    res = await service.process_document_v2(pages)

    flash_lite_calls = [c for c in mock_gateway.recorded_calls if c["model"] == GeminiModelRole.FLASH_LITE]
    flash_calls = [c for c in mock_gateway.recorded_calls if c["model"] == GeminiModelRole.FLASH]

    assert len(flash_lite_calls) == 7
    assert len(flash_calls) == 1
    assert len(mock_gateway.recorded_calls) == 8
    assert res.total_pages_reconciled == 7
    assert len(res.reconciled_markers) == 7


# --- Scenario 4: 15 pages -> 15 Flash-Lite (6 + 6 + 3) + 1 Flash (16 calls) ---

@pytest.mark.anyio
async def test_fifteen_pages_calls():
    mock_gateway = MockCascadeGateway()
    mock_db = MagicMock(spec=Session)
    service = LabVisionOcrService(db=mock_db, gateway=mock_gateway)

    pages = _create_pages(15)
    res = await service.process_document_v2(pages)

    flash_lite_calls = [c for c in mock_gateway.recorded_calls if c["model"] == GeminiModelRole.FLASH_LITE]
    flash_calls = [c for c in mock_gateway.recorded_calls if c["model"] == GeminiModelRole.FLASH]

    assert len(flash_lite_calls) == 15
    assert len(flash_calls) == 1
    assert len(mock_gateway.recorded_calls) == 16
    assert res.total_pages_reconciled == 15
    assert len(res.reconciled_markers) == 15


# --- Scenario 5: 16 pages -> Rejected at intake ---

@pytest.mark.anyio
async def test_sixteen_pages_rejected():
    mock_gateway = MockCascadeGateway()
    mock_db = MagicMock(spec=Session)
    service = LabVisionOcrService(db=mock_db, gateway=mock_gateway)

    pages = _create_pages(16)
    with pytest.raises(LabVisionValidationError) as exc:
        await service.process_document_v2(pages)

    assert "exceeds maximum allowed intake limit of 15 pages" in str(exc.value)
    assert len(mock_gateway.recorded_calls) == 0


# --- Scenario 6: Pages without dates and order numbers ---

@pytest.mark.anyio
async def test_pages_without_dates_and_order_id():
    class NoDateGateway(MockCascadeGateway):
        async def generate_structured(self, prompt: str, response_schema: Any, model: Any = GeminiModelRole.FLASH_LITE, **kwargs):
            if model == GeminiModelRole.FLASH_LITE:
                return VisionOcrPageExtraction(
                    page_number=1,
                    source_filename="undated.png",
                    page_sample_date=None,
                    page_issue_date=None,
                    raw_date_str=None,
                    order_id=None,
                    markers=[
                        VisionOcrMarkerItem(
                            row_id="p1_r1",
                            original_name="Гемоглобин",
                            marker_key="hemoglobin",
                            raw_value="140",
                            numeric_value=140.0,
                            unit="г/л",
                            sample_date=None,
                        )
                    ],
                )
            elif model == GeminiModelRole.FLASH:
                return VisionOcrReconciliationOutput(
                    total_pages_reconciled=1,
                    document_metadata=VisionOcrDocumentMetadata(
                        sample_date=None,
                        page_count=1,
                        notes="Date unestablished from laboratory blank",
                    ),
                    reconciled_markers=[
                        VisionOcrMarkerItem(
                            row_id="rec_r1",
                            source_row_ids=["p1_r1"],
                            original_name="Гемоглобин",
                            marker_key="hemoglobin",
                            raw_value="140",
                            numeric_value=140.0,
                            unit="г/л",
                            sample_date=None,
                        )
                    ],
                )

    mock_gateway = NoDateGateway()
    mock_db = MagicMock(spec=Session)
    service = LabVisionOcrService(db=mock_db, gateway=mock_gateway)

    pages = _create_pages(1)
    res = await service.process_document_v2(pages)

    assert res.document_metadata.sample_date is None
    assert res.reconciled_markers[0].sample_date is None
    # Indicators without sample_date require user verification
    assert res.reconciled_markers[0].requires_user_verification is True


# --- Scenario 7: Distinct studies with identical marker keys ---

def test_distinct_studies_with_same_marker_key():
    s1_page1 = VisionOcrPageExtraction(
        page_number=1,
        source_filename="study1.png",
        page_sample_date="2026-10-15",
        order_id="ORD-001",
        markers=[
            VisionOcrMarkerItem(
                row_id="p1_r1",
                page_number=1,
                original_name="Глюкоза (натощак)",
                marker_key="glucose",
                raw_value="4.8",
                numeric_value=4.8,
                unit="ммоль/л",
                sample_date="2026-10-15",
            )
        ],
    )
    s1_page2 = VisionOcrPageExtraction(
        page_number=2,
        source_filename="study2.png",
        page_sample_date="2026-10-16",
        order_id="ORD-002",
        markers=[
            VisionOcrMarkerItem(
                row_id="p2_r1",
                page_number=2,
                original_name="Глюкоза (через 2 часа)",
                marker_key="glucose",
                raw_value="7.2",
                numeric_value=7.2,
                unit="ммоль/л",
                sample_date="2026-10-16",
            )
        ],
    )

    flash_raw = VisionOcrReconciliationOutput(
        total_pages_reconciled=2,
        document_metadata=VisionOcrDocumentMetadata(page_count=2),
        reconciled_markers=[
            VisionOcrMarkerItem(
                row_id="rec_1",
                source_row_ids=["p1_r1"],
                original_name="Глюкоза (натощак)",
                marker_key="glucose",
                raw_value="4.8",
                numeric_value=4.8,
                unit="ммоль/л",
                sample_date="2026-10-15",
            ),
            VisionOcrMarkerItem(
                row_id="rec_2",
                source_row_ids=["p2_r1"],
                original_name="Глюкоза (через 2 часа)",
                marker_key="glucose",
                raw_value="7.2",
                numeric_value=7.2,
                unit="ммоль/л",
                sample_date="2026-10-16",
            ),
        ],
        study_linkages=[
            VisionOcrStudyLinkage(study_id="ORD-001", page_numbers=[1], sample_date="2026-10-15"),
            VisionOcrStudyLinkage(study_id="ORD-002", page_numbers=[2], sample_date="2026-10-16"),
        ],
    )

    service = LabVisionOcrService(db=MagicMock(spec=Session), gateway=MagicMock())
    validated = service.validate_and_recover_reconciliation([s1_page1, s1_page2], flash_raw)

    assert len(validated.reconciled_markers) == 2
    assert validated.reconciled_markers[0].source_row_ids == ["p1_r1"]
    assert validated.reconciled_markers[1].source_row_ids == ["p2_r1"]
    assert validated.reconciled_markers[0].sample_date == "2026-10-15"
    assert validated.reconciled_markers[1].sample_date == "2026-10-16"
    assert len(validated.study_linkages) == 2


# --- Scenario 8: Split CBC table stitched across pages ---

def test_split_cbc_table_stitched():
    # Page 1: CBC header + WBC, RBC
    p1 = VisionOcrPageExtraction(
        page_number=1,
        source_filename="cbc_p1.png",
        table_state="START",
        section_name="Общий анализ крови",
        markers=[
            VisionOcrMarkerItem(row_id="p1_r1", original_name="Лейкоциты", marker_key="wbc", raw_value="6.5", numeric_value=6.5, unit="10^9/л"),
            VisionOcrMarkerItem(row_id="p1_r2", original_name="Эритроциты", marker_key="rbc", raw_value="4.7", numeric_value=4.7, unit="10^12/л"),
        ],
    )
    # Page 2: Continuation with Hemoglobin, Platelets
    p2 = VisionOcrPageExtraction(
        page_number=2,
        source_filename="cbc_p2.png",
        table_state="CONTINUATION",
        section_name="Общий анализ крови",
        markers=[
            VisionOcrMarkerItem(row_id="p2_r1", original_name="Гемоглобин", marker_key="hemoglobin", raw_value="145", numeric_value=145.0, unit="г/л"),
            VisionOcrMarkerItem(row_id="p2_r2", original_name="Тромбоциты", marker_key="platelets", raw_value="240", numeric_value=240.0, unit="10^9/л"),
        ],
    )

    flash_raw = VisionOcrReconciliationOutput(
        total_pages_reconciled=2,
        reconciled_markers=[
            VisionOcrMarkerItem(row_id="rec_wbc", source_row_ids=["p1_r1"], original_name="Лейкоциты", marker_key="wbc", raw_value="6.5", numeric_value=6.5, unit="10^9/л"),
            VisionOcrMarkerItem(row_id="rec_rbc", source_row_ids=["p1_r2"], original_name="Эритроциты", marker_key="rbc", raw_value="4.7", numeric_value=4.7, unit="10^12/л"),
            VisionOcrMarkerItem(row_id="rec_hgb", source_row_ids=["p2_r1"], original_name="Гемоглобин", marker_key="hemoglobin", raw_value="145", numeric_value=145.0, unit="г/л"),
            VisionOcrMarkerItem(row_id="rec_plt", source_row_ids=["p2_r2"], original_name="Тромбоциты", marker_key="platelets", raw_value="240", numeric_value=240.0, unit="10^9/л"),
        ],
        reconciliation_notes="Stitched split CBC table between page 1 and page 2",
    )

    service = LabVisionOcrService(db=MagicMock(spec=Session), gateway=MagicMock())
    validated = service.validate_and_recover_reconciliation([p1, p2], flash_raw)

    assert len(validated.reconciled_markers) == 4
    all_source_ids = [m.source_row_ids[0] for m in validated.reconciled_markers]
    assert all_source_ids == ["p1_r1", "p1_r2", "p2_r1", "p2_r2"]


# --- Scenario 9: Absence of printed page number ---

def test_missing_printed_page_num():
    p = VisionOcrPageExtraction(
        page_number=1,
        source_filename="no_page_num.png",
        printed_page_num=None,
        total_printed_pages=None,
        markers=[
            VisionOcrMarkerItem(row_id="p1_r1", original_name="АЛТ", marker_key="alt", raw_value="24", numeric_value=24.0, unit="Ед/л")
        ],
    )
    assert p.printed_page_num is None
    assert p.page_number == 1  # Authoritative backend global page number


# --- Scenario 10: Python blocks Flash from dropping or modifying markers ---

def test_python_blocks_dropping_and_subversion():
    p1 = VisionOcrPageExtraction(
        page_number=1,
        source_filename="test.png",
        markers=[
            VisionOcrMarkerItem(row_id="p1_r1", original_name="Гемоглобин", marker_key="hemoglobin", raw_value="142", numeric_value=142.0, unit="г/л"),
            VisionOcrMarkerItem(row_id="p1_r2", original_name="Редкий маркер", marker_key="rare_marker", raw_value="0.05", numeric_value=0.05, unit="мг/л"),
        ],
    )

    # Flash dropped "rare_marker" AND altered "142" to "14.2"
    flash_raw = VisionOcrReconciliationOutput(
        total_pages_reconciled=1,
        reconciled_markers=[
            VisionOcrMarkerItem(
                row_id="rec_1",
                source_row_ids=["p1_r1"],
                original_name="Гемоглобин",
                marker_key="hemoglobin",
                raw_value="14.2",  # Subversion! Flash changed 142 to 14.2
                numeric_value=14.2,
                unit="г/л",
            )
            # "rare_marker" dropped by Flash!
        ],
    )

    service = LabVisionOcrService(db=MagicMock(spec=Session), gateway=MagicMock())
    validated = service.validate_and_recover_reconciliation([p1], flash_raw)

    # 1. Total markers restored to 2
    assert len(validated.reconciled_markers) == 2

    # 2. Subverted value was REVERTED back to Stage 1 raw value "142"
    hgb = validated.reconciled_markers[0]
    assert hgb.raw_value == "142"
    assert hgb.numeric_value == 142.0
    assert hgb.requires_user_verification is True
    assert "Preserved Stage 1 raw='142'" in hgb.uncertainty_note

    # 3. Dropped marker was RESTORED
    rare = validated.reconciled_markers[1]
    assert rare.marker_key == "rare_marker"
    assert rare.raw_value == "0.05"
    assert rare.requires_user_verification is True
    assert "Restored by Python safety check: omitted during Flash reconciliation" in rare.uncertainty_note


# --- Scenario 11: All Stage 1 row_id preserved in accounted rows ---

def test_all_row_ids_preserved_in_provenance():
    p1 = VisionOcrPageExtraction(
        page_number=1,
        source_filename="p1.png",
        markers=[
            VisionOcrMarkerItem(row_id="p1_r1", original_name="Тест 1", marker_key="test1", raw_value="10"),
            VisionOcrMarkerItem(row_id="p1_r2", original_name="Тест 2", marker_key="test2", raw_value="20"),
        ],
    )
    flash_raw = VisionOcrReconciliationOutput(
        total_pages_reconciled=1,
        reconciled_markers=[
            VisionOcrMarkerItem(row_id="rec_1", source_row_ids=["p1_r1"], original_name="Тест 1", marker_key="test1", raw_value="10"),
            VisionOcrMarkerItem(row_id="rec_2", source_row_ids=["p1_r2"], original_name="Тест 2", marker_key="test2", raw_value="20"),
        ],
    )
    service = LabVisionOcrService(db=MagicMock(spec=Session), gateway=MagicMock())
    validated = service.validate_and_recover_reconciliation([p1], flash_raw)

    accounted_source_ids = {rid for m in validated.reconciled_markers for rid in m.source_row_ids}
    assert accounted_source_ids == {"p1_r1", "p1_r2"}


# --- Scenario 12: Ledger untouched on Reconciler failure ---

@pytest.mark.anyio
async def test_ledger_untouched_on_reconciler_failure():
    mock_gateway = MockCascadeGateway(reconciliation_error=True)
    mock_db = MagicMock(spec=Session)
    service = LabVisionOcrService(db=mock_db, gateway=mock_gateway)

    pages = _create_pages(2)
    with pytest.raises(LabVisionValidationError) as exc:
        await service.process_document_v2(pages)

    assert "Flash reconciliation failed" in str(exc.value)
    # Database transaction was NOT committed
    mock_db.commit.assert_not_called()


# --- Scenario 13: Strict ECG Vision Isolation ---

def test_ecg_vision_isolation():
    ecg_prompt = build_vision_ecg_prompt()
    assert "Electrocardiogram Vision Digitization & Recognition Engine" in ecg_prompt
    assert EcgRecordingType.SINGLE_LEAD_SMARTWATCH == "single_lead_smartwatch"
    assert settings.gemini_vision_model == "gemini-3.8-flash"
    assert settings.gemini_flash_lite_model == "gemini-3.5-flash-lite"
    assert settings.gemini_flash_model == "gemini-3.8-flash"
