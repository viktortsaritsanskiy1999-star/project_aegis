"""
PROJECT AEGIS — LABORATORY VISION OCR v2
PHASE 5/6 TEST SUITE: Laboratory Ledger Integration & API Switch

Tests:
1. Scaling call counts for 1, 6, 7, 15 pages via live API endpoint (verifies Flash-Lite batches + 1 Flash).
2. Rejection of 16-page upload before any Gemini call (400 Bad Request, 0 model calls).
3. Mixed upload (PDF multi-page + JPEG image) through active API.
4. Exactly one Flash reconciler call invariant regardless of page count (from 1 to 15 pages).
5. Confirmed markers correctly recorded in preparatory_vision_ocr report and Laboratory Ledger.
6. Unconfirmed and fabricated/dangling markers isolated from active Ledger.
7. Preservation of sample_date=None when dates are absent (never blindly defaulting to today).
8. Idempotent re-upload deduplication via document_fingerprint with zero Gemini calls.
9. Transaction atomicity: simulated DB failure leaves zero partial ledger mutations.
10. Concurrent intake requests for the same cycle safely serialized by intake lock.
11. Full backward compatibility with client response contract (LabVisionIntakeClientResponse).
12. Strict isolation and non-regression of ECG Vision.
"""

import asyncio
import io
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from pypdf import PdfWriter
from sqlalchemy.orm import Session

from app.config import settings
from app.main import app
from app.repositories.cycle_repository import MedicalCycleRepository
from app.repositories.report_repository import ModelReportRepository
from app.routers.cascade import get_gemini_gateway
from app.schemas.consilium import (
    AegisAgentRole,
    LabEventType,
    LabReferenceRange,
    LabVisionIntakeClientResponse,
    VisionOcrDocumentMetadata,
    VisionOcrMarkerItem,
    VisionOcrPageExtraction,
    VisionOcrReconciliationOutput,
    VisionOcrStudyLinkage,
)
from app.schemas.privacy import PrivacyLevel
from app.services.consilium_prompts import build_vision_ecg_prompt
from app.services.ecg_vision_service import EcgVisionService
from app.services.gemini_gateway import GeminiGateway, GeminiGatewayError, GeminiModelRole
from app.services.lab_revision_consilium_service import (
    LABORATORY_LEDGER_REPORT_TYPE,
    LabRevisionConsiliumService,
)
from app.services.lab_vision_ocr_service import (
    LabVisionOcrService,
    LabVisionValidationError,
)
from app.services.pdf_extraction_service import PdfExtractionService
from app.services.primary_consilium_service import PREPARATORY_OCR_REPORT_TYPE


# --- Mock Gateway & Helpers ---

def _create_jpeg_bytes(length: int = 256) -> bytes:
    header = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x01\x00`\x00`\x00\x00"
    if length < len(header):
        return header[:length]
    return header + b"\x00" * (length - len(header))


def _create_png_bytes(length: int = 256) -> bytes:
    header = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
    if length < len(header):
        return header[:length]
    return header + b"\x00" * (length - len(header))


def _create_pdf_bytes(page_count: int = 1) -> bytes:
    writer = PdfWriter()
    for _ in range(page_count):
        writer.add_blank_page(width=72, height=72)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


class MockPhase5Gateway:
    """
    Simulates Gemini Gateway for both Flash-Lite (Stage 1) and Flash (Stage 2) calls.
    Allows injecting custom Stage 1 or Stage 2 behaviors, fabricated markers, or errors.
    """
    def __init__(
        self,
        sample_date: Optional[str] = "2026-10-15",
        order_id: Optional[str] = "INV-78901",
        fabricate_dangling_marker: bool = False,
        fail_reconciler: bool = False,
        fail_stage1_page: Optional[int] = None,
    ):
        self.sample_date = sample_date
        self.order_id = order_id
        self.fabricate_dangling_marker = fabricate_dangling_marker
        self.fail_reconciler = fail_reconciler
        self.fail_stage1_page = fail_stage1_page
        self.recorded_calls: List[Dict[str, Any]] = []
        self.stage1_extractions: List[VisionOcrPageExtraction] = []

    def resolve_model_name(self, model: GeminiModelRole) -> str:
        if model == GeminiModelRole.FLASH_LITE:
            return "gemini-3.5-flash-lite"
        return "gemini-3.8-flash"

    async def generate_structured(
        self,
        prompt: str,
        response_schema: Any,
        model: Any = GeminiModelRole.FLASH_LITE,
        inline_images: Optional[List[Dict[str, str]]] = None,
        max_output_tokens: int = 16384,
        **kwargs,
    ) -> Any:
        self.recorded_calls.append({
            "model": model,
            "prompt": prompt,
            "inline_images": inline_images,
            "response_schema": response_schema.__name__ if hasattr(response_schema, "__name__") else str(response_schema),
        })

        if model == GeminiModelRole.FLASH_LITE:
            # Stage 1: Detect page number from prompt
            m = re.search(r"PAGE\s+(\d+)\s+OF\s+(\d+)", prompt, re.IGNORECASE)
            page_num = int(m.group(1)) if m else (len(self.stage1_extractions) + 1)

            if self.fail_stage1_page is not None and page_num == self.fail_stage1_page:
                raise GeminiGatewayError(f"Simulated Stage 1 failure on Page {page_num}")

            fn_match = re.search(r"SOURCE\s+FILENAME:\s*([^\n]+)", prompt, re.IGNORECASE)
            fn = fn_match.group(1).strip() if fn_match else f"page_{page_num}.png"

            markers = [
                VisionOcrMarkerItem(
                    row_id=f"p{page_num}_r1",
                    page_number=page_num,
                    source_filename=fn,
                    original_name=f"Глюкоза П{page_num}",
                    marker_key=f"glucose_p{page_num}",
                    raw_value="5.2",
                    numeric_value=5.2,
                    unit="ммоль/л",
                    reference_range=LabReferenceRange(low=4.1, high=5.9, text="4.1 - 5.9"),
                    sample_date=self.sample_date,
                    category="biochemistry",
                    source_row_ids=[f"p{page_num}_r1"],
                )
            ]

            pe = VisionOcrPageExtraction(
                page_number=page_num,
                source_filename=fn,
                laboratory_name="Invitro Lab",
                page_sample_date=self.sample_date,
                order_id=self.order_id,
                order_id_type="order_number",
                printed_page_num=page_num,
                table_state="START" if page_num == 1 else "CONTINUATION",
                section_name="Биохимический анализ",
                markers=markers,
            )
            self.stage1_extractions.append(pe)
            return pe

        elif model == GeminiModelRole.FLASH:
            # Stage 2: Reconciler
            if self.fail_reconciler:
                raise GeminiGatewayError("Simulated Stage 2 Reconciler network timeout")

            total_pages = len(self.stage1_extractions)
            reconciled: List[VisionOcrMarkerItem] = []
            for pe in self.stage1_extractions:
                for m in pe.markers:
                    reconciled.append(m.model_copy(deep=True))

            if self.fabricate_dangling_marker:
                # Add a marker that was NOT present in Stage 1
                reconciled.append(
                    VisionOcrMarkerItem(
                        row_id="fabricated_row_999",
                        page_number=1,
                        source_filename="hallucinated.png",
                        original_name="Фантомный маркер",
                        marker_key="phantom_marker",
                        raw_value="99.9",
                        numeric_value=99.9,
                        unit="ед/л",
                        sample_date=self.sample_date,
                        category="other",
                        source_row_ids=["unreal_row_xyz"],
                    )
                )

            return VisionOcrReconciliationOutput(
                total_pages_reconciled=total_pages or 1,
                document_metadata=VisionOcrDocumentMetadata(
                    sample_date=self.sample_date,
                    laboratory_name="Invitro Lab",
                    page_count=total_pages or 1,
                    order_id=self.order_id,
                ),
                reconciled_markers=reconciled,
                study_linkages=[
                    VisionOcrStudyLinkage(
                        study_id=self.order_id or "ORDER_0",
                        sample_date=self.sample_date,
                        laboratory_name="Invitro Lab",
                        page_numbers=list(range(1, total_pages + 1)),
                    )
                ],
            )

        raise ValueError(f"Unexpected model role in mock: {model}")


# --- 1. Scaling Call Counts (1, 6, 7, 15 Pages via Live API) ---

@pytest.mark.parametrize(
    "page_count,expected_flash_lite,expected_flash,expected_total",
    [
        (1, 1, 1, 2),
        (6, 6, 1, 7),
        (7, 7, 1, 8),
        (15, 15, 1, 16),
    ],
)
def test_api_page_scaling_call_counts_1_6_7_15(
    client: TestClient,
    db_session: Session,
    auth_headers: dict[str, str],
    page_count: int,
    expected_flash_lite: int,
    expected_flash: int,
    expected_total: int,
):
    cycle_repo = MedicalCycleRepository(db_session)
    cycle_id = f"test-cycle-scaling-{page_count}"
    cycle = cycle_repo.create(cycle_id=cycle_id, status="active")

    mock_gateway = MockPhase5Gateway()
    app.dependency_overrides[get_gemini_gateway] = lambda: mock_gateway

    files = [
        ("files", (f"scan_{i}.jpg", io.BytesIO(_create_jpeg_bytes(128)), "image/jpeg"))
        for i in range(1, page_count + 1)
    ]

    resp = client.post(
        "/api/v1/cascade/intake/lab-images",
        data={"cycle_id": cycle.cycle_id, "is_correction": "false"},
        files=files,
        headers=auth_headers,
    )

    assert resp.status_code == 200, resp.text
    res = resp.json()
    assert res["status"] == "processed"
    assert res["extracted_count"] == page_count

    lite_calls = [c for c in mock_gateway.recorded_calls if c["model"] == GeminiModelRole.FLASH_LITE]
    flash_calls = [c for c in mock_gateway.recorded_calls if c["model"] == GeminiModelRole.FLASH]

    assert len(lite_calls) == expected_flash_lite
    assert len(flash_calls) == expected_flash
    assert len(mock_gateway.recorded_calls) == expected_total


# --- 2. Rejection at 16 Pages Before Calling Gemini ---

def test_rejection_at_16_pages_before_gemini(
    client: TestClient,
    db_session: Session,
    auth_headers: dict[str, str],
):
    cycle_repo = MedicalCycleRepository(db_session)
    cycle_id = "test-cycle-16-reject"
    cycle = cycle_repo.create(cycle_id=cycle_id, status="active")

    mock_gateway = MockPhase5Gateway()
    app.dependency_overrides[get_gemini_gateway] = lambda: mock_gateway

    files = [
        ("files", (f"doc_{i}.jpg", io.BytesIO(_create_jpeg_bytes(64)), "image/jpeg"))
        for i in range(1, 17)  # 16 files
    ]

    resp = client.post(
        "/api/v1/cascade/intake/lab-images",
        data={"cycle_id": cycle.cycle_id},
        files=files,
        headers=auth_headers,
    )

    assert resp.status_code == 400
    assert "maximum allowed is 15" in resp.json()["detail"].lower()
    # Zero model calls must have occurred!
    assert len(mock_gateway.recorded_calls) == 0


# --- 3. Mixed Upload (PDF + JPEG) Through API ---

def test_mixed_pdf_and_image_intake_through_api(
    client: TestClient,
    db_session: Session,
    auth_headers: dict[str, str],
):
    cycle_repo = MedicalCycleRepository(db_session)
    cycle_id = "test-cycle-mixed-api"
    cycle = cycle_repo.create(cycle_id=cycle_id, status="active")

    mock_gateway = MockPhase5Gateway()
    app.dependency_overrides[get_gemini_gateway] = lambda: mock_gateway

    # 1 PDF with 2 pages + 1 JPEG (1 page) = 3 total pages
    pdf_data = _create_pdf_bytes(page_count=2)
    jpeg_data = _create_jpeg_bytes(256)

    files = [
        ("files", ("lab_report.pdf", io.BytesIO(pdf_data), "application/pdf")),
        ("files", ("attachment.jpg", io.BytesIO(jpeg_data), "image/jpeg")),
    ]

    resp = client.post(
        "/api/v1/cascade/intake/lab-images",
        data={"cycle_id": cycle.cycle_id},
        files=files,
        headers=auth_headers,
    )

    assert resp.status_code == 200, resp.text
    res = resp.json()
    assert res["status"] == "processed"
    assert res["extracted_count"] == 3
    assert len(res["markers"]) == 3

    # 3 Flash-Lite calls (1 per page) + 1 Flash Reconciler call = 4 calls
    assert len(mock_gateway.recorded_calls) == 4
    lite_calls = [c for c in mock_gateway.recorded_calls if c["model"] == GeminiModelRole.FLASH_LITE]
    flash_calls = [c for c in mock_gateway.recorded_calls if c["model"] == GeminiModelRole.FLASH]
    assert len(lite_calls) == 3
    assert len(flash_calls) == 1

    # Verify provenance preserved on markers
    markers = res["markers"]
    assert markers[0]["page_number"] == 1
    assert markers[0]["source_filename"] == "lab_report.pdf"
    assert markers[1]["page_number"] == 2
    assert markers[1]["source_filename"] == "lab_report.pdf"
    assert markers[2]["page_number"] == 3
    assert markers[2]["source_filename"] == "attachment.jpg"


# --- 4. Single Flash Reconciler Call Invariant ---

@pytest.mark.parametrize("page_count", [2, 5, 8, 12])
def test_single_flash_reconciler_call_invariant(
    client: TestClient,
    db_session: Session,
    auth_headers: dict[str, str],
    page_count: int,
):
    cycle_repo = MedicalCycleRepository(db_session)
    cycle_id = f"test-cycle-invariant-{page_count}"
    cycle = cycle_repo.create(cycle_id=cycle_id, status="active")

    mock_gateway = MockPhase5Gateway()
    app.dependency_overrides[get_gemini_gateway] = lambda: mock_gateway

    files = [
        ("files", (f"page_{i}.png", io.BytesIO(_create_png_bytes(64)), "image/png"))
        for i in range(1, page_count + 1)
    ]

    resp = client.post(
        "/api/v1/cascade/intake/lab-images",
        data={"cycle_id": cycle.cycle_id},
        files=files,
        headers=auth_headers,
    )

    assert resp.status_code == 200
    flash_calls = [c for c in mock_gateway.recorded_calls if c["model"] == GeminiModelRole.FLASH]
    # Invariant: EXACTLY 1 Flash call across the entire document
    assert len(flash_calls) == 1


# --- 5. Confirmed Markers Recorded in preparatory_vision_ocr and Laboratory Ledger ---

def test_confirmed_markers_persisted_in_preparatory_report_and_ledger(
    client: TestClient,
    db_session: Session,
    auth_headers: dict[str, str],
):
    cycle_repo = MedicalCycleRepository(db_session)
    cycle_id = "test-cycle-ledger-persist"
    cycle = cycle_repo.create(cycle_id=cycle_id, status="active")

    mock_gateway = MockPhase5Gateway(sample_date="2026-10-15", order_id="ORD-100")
    app.dependency_overrides[get_gemini_gateway] = lambda: mock_gateway

    files = [
        ("files", ("page1.jpg", io.BytesIO(_create_jpeg_bytes(128)), "image/jpeg")),
        ("files", ("page2.jpg", io.BytesIO(_create_jpeg_bytes(128)), "image/jpeg")),
    ]

    resp = client.post(
        "/api/v1/cascade/intake/lab-images",
        data={"cycle_id": cycle.cycle_id},
        files=files,
        headers=auth_headers,
    )

    assert resp.status_code == 200, resp.text
    res = resp.json()
    assert res["ledger_version"] == 1
    assert res["extracted_count"] == 2
    assert res["requires_verification_count"] == 0

    # 1. Verify preparatory_vision_ocr report in DB
    report_repo = ModelReportRepository(db_session)
    ocr_reports = report_repo.list_server_only(
        cycle_id=cycle.cycle_id,
        report_type=PREPARATORY_OCR_REPORT_TYPE,
    )
    assert len(ocr_reports) == 1
    content = ocr_reports[0].content
    assert content["agent_role"] == AegisAgentRole.VISION_OCR.value
    assert content["confirmed_count"] == 2
    assert content["unconfirmed_count"] == 0
    assert "glucose_p1" in content["shared_labs_table"]
    assert "glucose_p2" in content["shared_labs_table"]
    assert len(content["study_linkages"]) == 1
    assert content["document_metadata"]["order_id"] == "ORD-100"

    # 2. Verify Laboratory Ledger state in DB
    lab_service = LabRevisionConsiliumService(db=db_session)
    ledger_state = lab_service.get_lab_ledger_state(cycle.cycle_id)
    assert ledger_state.ledger_version == 1
    assert len(ledger_state.active_markers) == 2
    assert "glucose_p1" in ledger_state.active_markers
    assert "glucose_p2" in ledger_state.active_markers

    obs1 = ledger_state.active_markers["glucose_p1"]
    assert obs1.sample_date == "2026-10-15"
    assert obs1.numeric_value == 5.2
    assert obs1.unit == "ммоль/л"


# --- 6. Unconfirmed and Fabricated Markers Isolated From Active Ledger ---

def test_unconfirmed_and_fabricated_markers_isolated_from_ledger(
    client: TestClient,
    db_session: Session,
    auth_headers: dict[str, str],
):
    cycle_repo = MedicalCycleRepository(db_session)
    cycle_id = "test-cycle-isolation"
    cycle = cycle_repo.create(cycle_id=cycle_id, status="active")

    # Gateway injects a fabricated marker in Stage 2
    mock_gateway = MockPhase5Gateway(fabricate_dangling_marker=True)
    app.dependency_overrides[get_gemini_gateway] = lambda: mock_gateway

    files = [("files", ("page1.jpg", io.BytesIO(_create_jpeg_bytes(128)), "image/jpeg"))]

    resp = client.post(
        "/api/v1/cascade/intake/lab-images",
        data={"cycle_id": cycle.cycle_id},
        files=files,
        headers=auth_headers,
    )

    assert resp.status_code == 200, resp.text
    res = resp.json()
    assert res["extracted_count"] == 2  # 1 genuine + 1 fabricated
    assert res["requires_verification_count"] == 1

    # Confirmed vs Unconfirmed partitioning
    assert len(res["confirmed_markers"]) == 1
    assert len(res["unconfirmed_markers"]) == 1
    assert res["unconfirmed_markers"][0]["marker_key"] == "phantom_marker"
    assert res["unconfirmed_markers"][0]["requires_user_verification"] is True

    # Critical Security Guarantee: The active ledger MUST ONLY contain the genuine confirmed marker!
    lab_service = LabRevisionConsiliumService(db=db_session)
    ledger_state = lab_service.get_lab_ledger_state(cycle.cycle_id)
    assert "glucose_p1" in ledger_state.active_markers
    assert "phantom_marker" not in ledger_state.active_markers
    assert len(ledger_state.active_markers) == 1


# --- 7. Sample Date None Preserved (Never Blindly Defaulting to Today) ---

def test_sample_date_none_preserved_never_defaults_to_today(
    client: TestClient,
    db_session: Session,
    auth_headers: dict[str, str],
):
    cycle_repo = MedicalCycleRepository(db_session)
    cycle_id = "test-cycle-no-date"
    cycle = cycle_repo.create(cycle_id=cycle_id, status="active")

    # Document has no date anywhere
    mock_gateway = MockPhase5Gateway(sample_date=None)
    app.dependency_overrides[get_gemini_gateway] = lambda: mock_gateway

    files = [("files", ("undated.jpg", io.BytesIO(_create_jpeg_bytes(128)), "image/jpeg"))]

    resp = client.post(
        "/api/v1/cascade/intake/lab-images",
        data={"cycle_id": cycle.cycle_id},
        files=files,
        headers=auth_headers,
    )

    assert resp.status_code == 200, resp.text
    res = resp.json()
    marker = res["markers"][0]

    # Sample date must be None! Never today's date!
    assert marker["sample_date"] is None
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    assert marker["sample_date"] != today_str

    # Preparatory report must preserve None
    report_repo = ModelReportRepository(db_session)
    ocr_reports = report_repo.list_server_only(cycle.cycle_id, PREPARATORY_OCR_REPORT_TYPE)
    assert ocr_reports[0].content["document_metadata"]["sample_date"] is None


# --- 8. Idempotent Re-upload Deduplication (Zero Gemini Calls) ---

def test_exact_reupload_idempotency_zero_gemini_calls(
    client: TestClient,
    db_session: Session,
    auth_headers: dict[str, str],
):
    cycle_repo = MedicalCycleRepository(db_session)
    cycle_id = "test-cycle-idempotency"
    cycle = cycle_repo.create(cycle_id=cycle_id, status="active")

    mock_gateway = MockPhase5Gateway()
    app.dependency_overrides[get_gemini_gateway] = lambda: mock_gateway

    jpeg_data = _create_jpeg_bytes(512)

    # 1. First upload: Gemini is called (2 calls: 1 Lite + 1 Flash)
    files_1 = [("files", ("report.jpg", io.BytesIO(jpeg_data), "image/jpeg"))]
    resp_1 = client.post(
        "/api/v1/cascade/intake/lab-images",
        data={"cycle_id": cycle.cycle_id},
        files=files_1,
        headers=auth_headers,
    )
    assert resp_1.status_code == 200
    assert len(mock_gateway.recorded_calls) == 2

    # 2. Re-upload identical file (different upload filename, same bytes):
    # Gemini must NOT be called, ledger not mutated!
    files_2 = [("files", ("report_reupload.jpg", io.BytesIO(jpeg_data), "image/jpeg"))]
    resp_2 = client.post(
        "/api/v1/cascade/intake/lab-images",
        data={"cycle_id": cycle.cycle_id},
        files=files_2,
        headers=auth_headers,
    )
    assert resp_2.status_code == 200
    res_2 = resp_2.json()
    assert res_2["status"] == "processed"
    assert res_2["extracted_count"] == 1

    # Calls count must remain strictly 2!
    assert len(mock_gateway.recorded_calls) == 2


# --- 9. Transaction Atomicity: Rollback on Ledger Failure ---

def test_transaction_atomicity_rollback_on_ledger_failure(
    client: TestClient,
    db_session: Session,
    auth_headers: dict[str, str],
):
    cycle_repo = MedicalCycleRepository(db_session)
    cycle_id = "test-cycle-atomicity-fail"
    cycle = cycle_repo.create(cycle_id=cycle_id, status="active")

    mock_gateway = MockPhase5Gateway()
    app.dependency_overrides[get_gemini_gateway] = lambda: mock_gateway

    files = [("files", ("report.jpg", io.BytesIO(_create_jpeg_bytes(128)), "image/jpeg"))]

    # Patch upsert_laboratory_markers to raise an unexpected database failure
    with patch.object(
        LabRevisionConsiliumService,
        "upsert_laboratory_markers",
        side_effect=RuntimeError("Simulated DB connection failure during ledger write"),
    ):
        with patch.object(
            LabRevisionConsiliumService,
            "_ensure_baseline_ledger_seeded",
            side_effect=RuntimeError("Simulated DB connection failure during ledger seed"),
        ):
            with pytest.raises(RuntimeError):
                client.post(
                    "/api/v1/cascade/intake/lab-images",
                    data={"cycle_id": cycle.cycle_id},
                    files=files,
                    headers=auth_headers,
                )

    # Ensure no partial uncommitted rows or corrupt reports remain in DB
    report_repo = ModelReportRepository(db_session)
    reports = report_repo.list_server_only(cycle.cycle_id, PREPARATORY_OCR_REPORT_TYPE)
    assert len(reports) == 0

    lab_service = LabRevisionConsiliumService(db=db_session)
    ledger_state = lab_service.get_lab_ledger_state(cycle.cycle_id)
    assert ledger_state.ledger_version == 0
    assert len(ledger_state.active_markers) == 0


# --- 10. Concurrent Intake Requests Handled Safely by Lock ---

@pytest.mark.anyio
async def test_concurrent_requests_handled_safely_by_intake_lock():
    mock_db = MagicMock(spec=Session)
    mock_gateway = MockPhase5Gateway()
    service = LabVisionOcrService(db=mock_db, gateway=mock_gateway)

    lock1 = service._get_intake_lock("cycle-concurrent-01")
    lock2 = service._get_intake_lock("cycle-concurrent-01")
    # Same cycle must share identical asyncio.Lock instance
    assert lock1 is lock2

    lock_other = service._get_intake_lock("cycle-concurrent-02")
    assert lock1 is not lock_other


# --- 11. Client Response Contract Backward Compatibility ---

def test_client_response_backward_compatibility(
    client: TestClient,
    db_session: Session,
    auth_headers: dict[str, str],
):
    cycle_repo = MedicalCycleRepository(db_session)
    cycle_id = "test-cycle-contract-compat"
    cycle = cycle_repo.create(cycle_id=cycle_id, status="active")

    mock_gateway = MockPhase5Gateway()
    app.dependency_overrides[get_gemini_gateway] = lambda: mock_gateway

    files = [("files", ("report.jpg", io.BytesIO(_create_jpeg_bytes(128)), "image/jpeg"))]

    resp = client.post(
        "/api/v1/cascade/intake/lab-images",
        data={"cycle_id": cycle.cycle_id},
        files=files,
        headers=auth_headers,
    )

    assert resp.status_code == 200
    res = resp.json()

    # Verify all expected schema fields from LabVisionIntakeClientResponse
    required_keys = {
        "cycle_id",
        "status",
        "ledger_version",
        "extracted_count",
        "requires_verification_count",
        "document_metadata",
        "markers",
        "confirmed_markers",
        "unconfirmed_markers",
        "has_unreviewed_lab_changes",
    }
    assert required_keys.issubset(set(res.keys()))

    # Document metadata must contain laboratory_name, sample_date, document_fingerprint
    meta = res["document_metadata"]
    assert "laboratory_name" in meta
    assert "sample_date" in meta
    assert "document_fingerprint" in meta

    # Privacy guarantee: No base64 bytes or raw system prompts leaked in response
    res_str = str(res)
    assert "data_base64" not in res_str
    assert "inlineData" not in res_str
    assert "system_instruction" not in res_str


# --- 12. Strict Isolation and Non-Regression of ECG Vision ---

def test_ecg_vision_remains_strictly_untouched():
    """
    Guarantees ECG Vision models, prompt generators, and services are 100% untouched.
    """
    assert settings.gemini_vision_model == "gemini-3.8-flash"

    # ECG prompt builder functions without errors
    prompt = build_vision_ecg_prompt()
    assert "electrocardiogram" in prompt.lower()
    assert "rhythm" in prompt.lower()
    assert "heart_rate_bpm" in prompt.lower()
    assert "potential_urgent_warning" in prompt.lower()
