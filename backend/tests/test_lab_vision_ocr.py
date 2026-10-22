import asyncio
import base64
from datetime import datetime, timezone
import io
import json
from typing import Any, Dict, List, Optional
import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.main import app
from app.repositories.cycle_repository import MedicalCycleRepository
from app.repositories.report_repository import ModelReportRepository
from app.routers.cascade import get_gemini_gateway
from app.schemas.consilium import (
    AegisAgentRole,
    LabEventType,
    VisionOcrMarkerItem,
    VisionOcrDocumentMetadata,
    VisionOcrModelOutput,
    LabVisionIntakeClientResponse,
)
from app.schemas.privacy import PrivacyLevel
from app.services.gemini_gateway import GeminiGateway, GeminiGatewayError, GeminiModelRole
from app.services.lab_revision_consilium_service import (
    LabRevisionConsiliumService,
    LABORATORY_LEDGER_REPORT_TYPE,
)
from app.services.lab_vision_ocr_service import (
    LabVisionOcrService,
    LabVisionValidationError,
    detect_image_mime_type_from_bytes,
)
from app.services.primary_consilium_service import (
    PREPARATORY_OCR_REPORT_TYPE,
)
from tests.test_primary_consilium import (
    _gemini_envelope,
    _sample_anamnesis_output,
)
from tests.test_anamnesis_intake import _sample_intake_answers


# --- Test Helpers & Valid Image Generators ---

def _create_dummy_jpeg_bytes(length: int = 64) -> bytes:
    """Returns valid JPEG magic bytes padded to length."""
    header = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x01\x00`\x00`\x00\x00"
    if length < len(header):
        return header[:length]
    return header + b"\x00" * (length - len(header))


def _create_dummy_png_bytes(length: int = 64) -> bytes:
    """Returns valid PNG magic bytes padded to length."""
    header = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
    if length < len(header):
        return header[:length]
    return header + b"\x00" * (length - len(header))


def _create_dummy_webp_bytes(length: int = 64) -> bytes:
    """Returns valid WEBP magic bytes padded to length."""
    header = b"RIFF\x30\x00\x00\x00WEBPVP8 "
    if length < len(header):
        return header[:length]
    return header + b"\x00" * (length - len(header))


def _create_dummy_heic_bytes(length: int = 64) -> bytes:
    """Returns valid HEIC magic bytes padded to length."""
    header = b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00mif1heic"
    if length < len(header):
        return header[:length]
    return header + b"\x00" * (length - len(header))


def _sample_vision_ocr_output(
    sample_date: str = "2026-10-15",
    lab_name: str = "Invitro Diagnostics",
    page_count: int = 1,
    markers: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    if markers is None:
        markers = [
            {
                "original_name": "Аланинаминотрансфераза (АЛТ)",
                "marker_key": "alt",
                "raw_value": "24.5",
                "numeric_value": 24.5,
                "unit": "Ед/л",
                "reference_range": {"low": 0.0, "high": 41.0, "text": "0 - 41"},
                "sample_date": sample_date,
                "category": "biochemistry",
                "is_uncertain": False,
                "uncertainty_note": None,
                "is_critical_flag": False,
            },
            {
                "original_name": "Аспартатаминотрансфераза (АСТ)",
                "marker_key": "ast",
                "raw_value": "21.0",
                "numeric_value": 21.0,
                "unit": "Ед/л",
                "reference_range": {"low": 0.0, "high": 37.0, "text": "0 - 37"},
                "sample_date": sample_date,
                "category": "biochemistry",
                "is_uncertain": False,
                "uncertainty_note": None,
                "is_critical_flag": False,
            },
            {
                "original_name": "Билирубин общий",
                "marker_key": "bilirubin_total",
                "raw_value": "14.2",
                "numeric_value": 14.2,
                "unit": "мкмоль/л",
                "reference_range": {"low": 3.4, "high": 20.5, "text": "3.4 - 20.5"},
                "sample_date": sample_date,
                "category": "biochemistry",
                "is_uncertain": False,
                "uncertainty_note": None,
                "is_critical_flag": False,
            },
            {
                "original_name": "Ферритин",
                "marker_key": "ferritin",
                "raw_value": "115.0",
                "numeric_value": 115.0,
                "unit": "мкг/л",
                "reference_range": {"low": 30.0, "high": 400.0, "text": "30 - 400"},
                "sample_date": sample_date,
                "category": "hematology",
                "is_uncertain": False,
                "uncertainty_note": None,
                "is_critical_flag": False,
            },
            {
                "original_name": "Глюкоза (венозная кровь)",
                "marker_key": "glucose",
                "raw_value": "4.9",
                "numeric_value": 4.9,
                "unit": "ммоль/л",
                "reference_range": {"low": 4.1, "high": 5.9, "text": "4.1 - 5.9"},
                "sample_date": sample_date,
                "category": "endocrinology",
                "is_uncertain": False,
                "uncertainty_note": None,
                "is_critical_flag": False,
            },
        ]

    return {
        "document_metadata": {
            "sample_date": sample_date,
            "issue_date": "2026-10-16",
            "laboratory_name": lab_name,
            "page_count": page_count,
            "notes": "Quality scan, legible print",
        },
        "markers": markers,
    }


def _build_vision_mock_gateway(
    recorded_calls: List[Dict[str, Any]],
    ocr_override: Optional[Dict[str, Any]] = None,
    fail_with_status: Optional[int] = None,
) -> GeminiGateway:
    """Mock GeminiGateway for Vision OCR requests."""
    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        recorded_calls.append({
            "url": str(request.url),
            "body": body,
        })

        if fail_with_status:
            return httpx.Response(
                status_code=fail_with_status,
                json={"error": {"status": "UNAVAILABLE", "message": "Vision backend temporarily unavailable"}},
            )

        payload = ocr_override or _sample_vision_ocr_output()
        return httpx.Response(
            status_code=200,
            json=_gemini_envelope(payload),
        )

    transport = httpx.MockTransport(handler)
    return GeminiGateway(api_key="test-ocr-key", transport=transport)


# --- 1. Single Page Lab Vision OCR Extraction ---

def test_single_page_lab_vision_ocr_extraction(client: TestClient, db_session: Session, auth_headers: dict[str, str]):
    cycle_repo = MedicalCycleRepository(db_session)
    cycle_id = "test-cycle-vision-01"
    cycle = cycle_repo.create(cycle_id=cycle_id, status="active")

    recorded_calls: List[Dict[str, Any]] = []
    mock_gateway = _build_vision_mock_gateway(recorded_calls)
    app.dependency_overrides[get_gemini_gateway] = lambda: mock_gateway

    jpeg_data = _create_dummy_jpeg_bytes(1024)
    files = [("files", ("lab_report.jpg", io.BytesIO(jpeg_data), "image/jpeg"))]
    data = {
        "cycle_id": cycle.cycle_id,
        "is_correction": "false",
    }

    resp = client.post(
        "/api/v1/cascade/intake/lab-images",
        data=data,
        files=files,
        headers=auth_headers,
    )

    assert resp.status_code == 200, resp.text
    res = resp.json()

    assert res["cycle_id"] == cycle.cycle_id
    assert res["status"] == "processed"
    assert res["ledger_version"] == 1
    assert res["extracted_count"] == 5
    assert res["has_unreviewed_lab_changes"] is True
    assert res["document_metadata"]["laboratory_name"] == "Invitro Diagnostics"
    assert len(res["markers"]) == 5

    # Verify client response privacy: NO raw images, NO system logic
    assert "inlineData" not in str(res)
    assert "training_sandbox" not in res

    # Verify recorded call to Gemini has inlineData with image/jpeg
    assert len(recorded_calls) == 1
    req_parts = recorded_calls[0]["body"]["contents"][0]["parts"]
    assert any("inlineData" in p and p["inlineData"]["mimeType"] == "image/jpeg" for p in req_parts)

    # Verify database: preparatory_vision_ocr saved with SERVER_ONLY
    report_repo = ModelReportRepository(db_session)
    prep_reports = report_repo.list_versions(
        cycle_id=cycle.cycle_id,
        report_type=PREPARATORY_OCR_REPORT_TYPE,
        privacy_level=PrivacyLevel.SERVER_ONLY,
    )
    assert len(prep_reports) == 1
    assert prep_reports[0].content["agent_role"] == AegisAgentRole.VISION_OCR.value
    assert prep_reports[0].content["extracted_count"] == 5

    # Verify laboratory_ledger in database
    ledger_reports = report_repo.list_versions(
        cycle_id=cycle.cycle_id,
        report_type=LABORATORY_LEDGER_REPORT_TYPE,
        privacy_level=PrivacyLevel.SERVER_ONLY,
    )
    assert len(ledger_reports) == 1
    ledger_content = ledger_reports[0].content
    assert ledger_content["ledger_version"] == 1
    assert "alt" in ledger_content["active_markers"]
    assert ledger_content["active_markers"]["alt"]["event_type"] == LabEventType.INITIAL.value


# --- 2. Multi-page Lab Vision OCR Correlation ---

def test_multipage_lab_vision_ocr_correlation(client: TestClient, db_session: Session, auth_headers: dict[str, str]):
    cycle_repo = MedicalCycleRepository(db_session)
    cycle_id = "test-cycle-vision-01"
    cycle = cycle_repo.create(cycle_id=cycle_id, status="active")

    recorded_calls: List[Dict[str, Any]] = []
    # Multi-page output returning 3 markers across 3 pages
    multipage_output = _sample_vision_ocr_output(
        page_count=3,
        markers=[
            {
                "original_name": "Гемоглобин",
                "marker_key": "hemoglobin",
                "raw_value": "154.0",
                "numeric_value": 154.0,
                "unit": "г/л",
                "reference_range": {"low": 130.0, "high": 170.0, "text": "130 - 170"},
                "sample_date": "2026-10-15",
                "category": "hematology",
                "is_uncertain": False,
                "uncertainty_note": None,
                "is_critical_flag": False,
            },
            {
                "original_name": "Эритроциты",
                "marker_key": "rbc",
                "raw_value": "5.1",
                "numeric_value": 5.1,
                "unit": "10^12/л",
                "reference_range": {"low": 4.0, "high": 5.5, "text": "4.0 - 5.5"},
                "sample_date": "2026-10-15",
                "category": "hematology",
                "is_uncertain": False,
                "uncertainty_note": None,
                "is_critical_flag": False,
            },
            {
                "original_name": "Лейкоциты",
                "marker_key": "wbc",
                "raw_value": "6.8",
                "numeric_value": 6.8,
                "unit": "10^9/л",
                "reference_range": {"low": 4.0, "high": 9.0, "text": "4.0 - 9.0"},
                "sample_date": "2026-10-15",
                "category": "hematology",
                "is_uncertain": False,
                "uncertainty_note": None,
                "is_critical_flag": False,
            },
        ],
    )
    mock_gateway = _build_vision_mock_gateway(recorded_calls, ocr_override=multipage_output)
    app.dependency_overrides[get_gemini_gateway] = lambda: mock_gateway

    files = [
        ("files", ("page1.png", io.BytesIO(_create_dummy_png_bytes(500)), "image/png")),
        ("files", ("page2.png", io.BytesIO(_create_dummy_png_bytes(600)), "image/png")),
        ("files", ("page3.png", io.BytesIO(_create_dummy_png_bytes(700)), "image/png")),
    ]
    data = {
        "cycle_id": cycle.cycle_id,
        "is_correction": "false",
    }

    resp = client.post(
        "/api/v1/cascade/intake/lab-images",
        data=data,
        files=files,
        headers=auth_headers,
    )

    assert resp.status_code == 200, resp.text
    res = resp.json()
    assert res["extracted_count"] == 3
    assert res["document_metadata"]["page_count"] == 3

    # Verify gateway call received all 3 pages
    assert len(recorded_calls) == 1
    parts = recorded_calls[0]["body"]["contents"][0]["parts"]
    image_parts = [p for p in parts if "inlineData" in p]
    assert len(image_parts) == 3
    for part in image_parts:
        assert part["inlineData"]["mimeType"] == "image/png"

    # Verify prompt instructed 3 pages
    text_part = next(p["text"] for p in parts if "text" in p)
    assert "analyzing 3 image page(s)" in text_part.lower()


# --- 3. Multiple Dates in Single Upload Creates Distinct Study Dates ---

def test_multiple_dates_in_single_upload_creates_distinct_study_dates(client: TestClient, db_session: Session, auth_headers: dict[str, str]):
    cycle_repo = MedicalCycleRepository(db_session)
    cycle_id = "test-cycle-vision-01"
    cycle = cycle_repo.create(cycle_id=cycle_id, status="active")

    recorded_calls: List[Dict[str, Any]] = []

    # Step 1: September study upload
    september_output = _sample_vision_ocr_output(
        sample_date="2026-09-10",
        markers=[
            {
                "original_name": "Креатинин (сентябрь)",
                "marker_key": "creatinine",
                "raw_value": "88.0",
                "numeric_value": 88.0,
                "unit": "мкмоль/л",
                "reference_range": {"low": 62.0, "high": 106.0, "text": "62 - 106"},
                "sample_date": "2026-09-10",
                "category": "biochemistry",
                "is_uncertain": False,
                "uncertainty_note": None,
                "is_critical_flag": False,
            },
        ],
    )
    mock_gateway = _build_vision_mock_gateway(recorded_calls, ocr_override=september_output)
    app.dependency_overrides[get_gemini_gateway] = lambda: mock_gateway

    resp1 = client.post(
        "/api/v1/cascade/intake/lab-images",
        data={"cycle_id": cycle.cycle_id},
        files=[("files", ("sept.webp", io.BytesIO(_create_dummy_webp_bytes(512)), "image/webp"))],
        headers=auth_headers,
    )
    assert resp1.status_code == 200, resp1.text
    assert resp1.json()["ledger_version"] == 1

    # Step 2: October study upload (new study with different sample_date)
    october_output = _sample_vision_ocr_output(
        sample_date="2026-10-15",
        markers=[
            {
                "original_name": "Креатинин (октябрь)",
                "marker_key": "creatinine",
                "raw_value": "82.0",
                "numeric_value": 82.0,
                "unit": "мкмоль/л",
                "reference_range": {"low": 62.0, "high": 106.0, "text": "62 - 106"},
                "sample_date": "2026-10-15",
                "category": "biochemistry",
                "is_uncertain": False,
                "uncertainty_note": None,
                "is_critical_flag": False,
            },
        ],
    )
    mock_gateway_oct = _build_vision_mock_gateway(recorded_calls, ocr_override=october_output)
    app.dependency_overrides[get_gemini_gateway] = lambda: mock_gateway_oct

    resp2 = client.post(
        "/api/v1/cascade/intake/lab-images",
        data={"cycle_id": cycle.cycle_id},
        files=[("files", ("oct.webp", io.BytesIO(_create_dummy_webp_bytes(600)), "image/webp"))],
        headers=auth_headers,
    )
    assert resp2.status_code == 200, resp2.text
    res2 = resp2.json()
    assert res2["ledger_version"] == 2

    # In ledger, the latest active creatinine is 82.0 with sample_date 2026-10-15
    ledger_service = LabRevisionConsiliumService(db=db_session, gateway=mock_gateway_oct)
    ledger_state = ledger_service.get_lab_ledger_state(cycle.cycle_id)
    assert "creatinine" in ledger_state.active_markers
    active_cr = ledger_state.active_markers["creatinine"]
    assert active_cr.value == "82.0"
    assert active_cr.sample_date == "2026-10-15"
    assert active_cr.event_type == LabEventType.NEW_STUDY.value

    # The observations history in ledger retains both observations
    assert len(ledger_state.observations_history) == 2


# --- 4. Diverse Units, Textual Reference Ranges, and Uncertainty ---

def test_diverse_units_and_textual_reference_ranges_and_uncertainty(client: TestClient, db_session: Session, auth_headers: dict[str, str]):
    cycle_repo = MedicalCycleRepository(db_session)
    cycle_id = "test-cycle-vision-01"
    cycle = cycle_repo.create(cycle_id=cycle_id, status="active")

    recorded_calls: List[Dict[str, Any]] = []
    diverse_markers_output = _sample_vision_ocr_output(
        markers=[
            {
                "original_name": "HBsAg (гепатит B)",
                "marker_key": "hbsag",
                "raw_value": "отрицательно",
                "numeric_value": None,
                "unit": None,
                "reference_range": {"text": "отрицательно"},
                "sample_date": "2026-10-15",
                "category": "infectious",
                "is_uncertain": False,
                "uncertainty_note": None,
                "is_critical_flag": False,
            },
            {
                "original_name": "Витамин D (25-OH)",
                "marker_key": "vitamin_d",
                "raw_value": "28.4",
                "numeric_value": 28.4,
                "unit": "нг/мл",
                "reference_range": {"low": 30.0, "high": 100.0, "text": ">= 30"},
                "sample_date": "2026-10-15",
                "category": "endocrinology",
                "is_uncertain": False,
                "uncertainty_note": None,
                "is_critical_flag": False,
            },
            {
                "original_name": "СОЭ (по Вестергрену)",
                "marker_key": "esr",
                "raw_value": "~1?",
                "numeric_value": None,
                "unit": "мм/ч",
                "reference_range": {"low": 2.0, "high": 15.0, "text": "2 - 15"},
                "sample_date": "2026-10-15",
                "category": "hematology",
                "is_uncertain": True,
                "uncertainty_note": "Размытая цифра на сгибе листа",
                "is_critical_flag": False,
            },
            {
                "original_name": "Тропонин I",
                "marker_key": "troponin_i",
                "raw_value": "0.01",
                "numeric_value": 0.01,
                "unit": "мкг/л",
                "reference_range": {"high": 0.04, "text": "< 0.04"},
                "sample_date": "2026-10-15",
                "category": "cardiology",
                "is_uncertain": False,
                "uncertainty_note": None,
                "is_critical_flag": False,
            },
        ]
    )
    mock_gateway = _build_vision_mock_gateway(recorded_calls, ocr_override=diverse_markers_output)
    app.dependency_overrides[get_gemini_gateway] = lambda: mock_gateway

    files = [("files", ("scan.heic", io.BytesIO(_create_dummy_heic_bytes(512)), "image/heic"))]
    resp = client.post(
        "/api/v1/cascade/intake/lab-images",
        data={"cycle_id": cycle.cycle_id},
        files=files,
        headers=auth_headers,
    )

    assert resp.status_code == 200, resp.text
    res = resp.json()
    assert res["extracted_count"] == 4

    # Verify markers
    markers_by_key = {m["marker_key"]: m for m in res["markers"]}

    # 1. Non-numeric marker
    hbsag = markers_by_key["hbsag"]
    assert hbsag["raw_value"] == "отрицательно"
    assert hbsag["numeric_value"] is None

    # 2. Text reference range with greater than
    vit_d = markers_by_key["vitamin_d"]
    assert vit_d["numeric_value"] == 28.4
    assert vit_d["unit"] == "нг/мл"

    # 3. Uncertain/blurred marker
    esr = markers_by_key["esr"]
    assert esr["is_uncertain"] is True
    assert esr["uncertainty_note"] == "Размытая цифра на сгибе листа"

    # 4. Reference range with less than
    trop = markers_by_key["troponin_i"]
    assert trop["reference_range"]["high"] == 0.04


# --- 5. File Validation: Magic Bytes and Invalid Formats ---

def test_file_validation_magic_bytes_and_invalid_formats(client: TestClient, db_session: Session, auth_headers: dict[str, str]):
    cycle_repo = MedicalCycleRepository(db_session)
    cycle_id = "test-cycle-vision-01"
    cycle = cycle_repo.create(cycle_id=cycle_id, status="active")

    mock_gateway = _build_vision_mock_gateway([])
    app.dependency_overrides[get_gemini_gateway] = lambda: mock_gateway

    # Case A: Plain text disguised as .jpg
    fake_jpg = b"This is plain text and definitely not an image file"
    resp = client.post(
        "/api/v1/cascade/intake/lab-images",
        data={"cycle_id": cycle.cycle_id},
        files=[("files", ("report.jpg", io.BytesIO(fake_jpg), "image/jpeg"))],
        headers=auth_headers,
    )
    assert resp.status_code == 400
    assert "invalid image content" in resp.json()["detail"].lower()

    # Case B: Windows executable PE header disguised as .png
    fake_png_exe = b"MZ\x90\x00\x03\x00\x00\x00\x04\x00\x00\x00\xff\xff\x00\x00"
    resp2 = client.post(
        "/api/v1/cascade/intake/lab-images",
        data={"cycle_id": cycle.cycle_id},
        files=[("files", ("trojan.png", io.BytesIO(fake_png_exe), "image/png"))],
        headers=auth_headers,
    )
    assert resp2.status_code == 400
    assert "invalid image content" in resp2.json()["detail"].lower()

    # Case C: PDF document (only direct images accepted by Vision intake)
    fake_pdf = b"%PDF-1.4\n%..."
    resp3 = client.post(
        "/api/v1/cascade/intake/lab-images",
        data={"cycle_id": cycle.cycle_id},
        files=[("files", ("doc.pdf", io.BytesIO(fake_pdf), "application/pdf"))],
        headers=auth_headers,
    )
    assert resp3.status_code == 400


# --- 6. File Validation: Page Count and Size Limits ---

def test_file_validation_page_count_and_size_limits(client: TestClient, db_session: Session, auth_headers: dict[str, str]):
    cycle_repo = MedicalCycleRepository(db_session)
    cycle_id = "test-cycle-vision-01"
    cycle = cycle_repo.create(cycle_id=cycle_id, status="active")

    mock_gateway = _build_vision_mock_gateway([])
    app.dependency_overrides[get_gemini_gateway] = lambda: mock_gateway

    # Case A: Too many files (>15 files: 16 files)
    sixteen_files = [
        ("files", (f"page_{i}.jpg", io.BytesIO(_create_dummy_jpeg_bytes(100)), "image/jpeg"))
        for i in range(16)
    ]
    resp_too_many = client.post(
        "/api/v1/cascade/intake/lab-images",
        data={"cycle_id": cycle.cycle_id},
        files=sixteen_files,
        headers=auth_headers,
    )
    assert resp_too_many.status_code == 400
    assert "maximum allowed is 15" in resp_too_many.json()["detail"].lower()

    # Case B: Single file exceeds 10MB
    large_bytes = _create_dummy_jpeg_bytes(10 * 1024 * 1024 + 1024)  # 10MB + 1KB
    resp_large_file = client.post(
        "/api/v1/cascade/intake/lab-images",
        data={"cycle_id": cycle.cycle_id},
        files=[("files", ("huge.jpg", io.BytesIO(large_bytes), "image/jpeg"))],
        headers=auth_headers,
    )
    assert resp_large_file.status_code == 400
    assert "10 mb" in resp_large_file.json()["detail"].lower()


# --- 7. Correction Flag and Sample Date Override ---

def test_correction_flag_and_sample_date_override(client: TestClient, db_session: Session, auth_headers: dict[str, str]):
    cycle_repo = MedicalCycleRepository(db_session)
    cycle_id = "test-cycle-vision-01"
    cycle = cycle_repo.create(cycle_id=cycle_id, status="active")

    recorded_calls: List[Dict[str, Any]] = []

    # Step 1: Initial upload with Ferritin = 12.0
    initial_output = _sample_vision_ocr_output(
        sample_date="2026-10-10",
        markers=[
            {
                "original_name": "Ферритин сыворотки",
                "marker_key": "ferritin",
                "raw_value": "12.0",
                "numeric_value": 12.0,
                "unit": "мкг/л",
                "reference_range": {"low": 30.0, "high": 400.0, "text": "30 - 400"},
                "sample_date": "2026-10-10",
                "category": "hematology",
                "is_uncertain": False,
                "uncertainty_note": None,
                "is_critical_flag": True,
            }
        ],
    )
    mock_gateway = _build_vision_mock_gateway(recorded_calls, ocr_override=initial_output)
    app.dependency_overrides[get_gemini_gateway] = lambda: mock_gateway

    resp1 = client.post(
        "/api/v1/cascade/intake/lab-images",
        data={"cycle_id": cycle.cycle_id, "is_correction": "false"},
        files=[("files", ("report1.jpg", io.BytesIO(_create_dummy_jpeg_bytes(512)), "image/jpeg"))],
        headers=auth_headers,
    )
    assert resp1.status_code == 200
    assert resp1.json()["ledger_version"] == 1

    ledger_service = LabRevisionConsiliumService(db=db_session, gateway=mock_gateway)
    state1 = ledger_service.get_lab_ledger_state(cycle.cycle_id)
    obs1_id = state1.active_markers["ferritin"].observation_id

    # Step 2: Client uploads corrected scan with is_correction=true and sample_date override
    corrected_output = _sample_vision_ocr_output(
        sample_date="2026-10-10",
        markers=[
            {
                "original_name": "Ферритин сыворотки (исправлено)",
                "marker_key": "ferritin",
                "raw_value": "120.0",
                "numeric_value": 120.0,
                "unit": "мкг/л",
                "reference_range": {"low": 30.0, "high": 400.0, "text": "30 - 400"},
                "sample_date": "2026-10-10",
                "category": "hematology",
                "is_uncertain": False,
                "uncertainty_note": None,
                "is_critical_flag": False,
            }
        ],
    )
    mock_gateway_corr = _build_vision_mock_gateway(recorded_calls, ocr_override=corrected_output)
    app.dependency_overrides[get_gemini_gateway] = lambda: mock_gateway_corr

    resp2 = client.post(
        "/api/v1/cascade/intake/lab-images",
        data={
            "cycle_id": cycle.cycle_id,
            "is_correction": "true",
            "sample_date": "2026-10-18",
        },
        files=[("files", ("corrected.jpg", io.BytesIO(_create_dummy_jpeg_bytes(512)), "image/jpeg"))],
        headers=auth_headers,
    )
    assert resp2.status_code == 200
    res2 = resp2.json()
    assert res2["ledger_version"] == 2
    assert res2["document_metadata"]["sample_date"] == "2026-10-18"

    # Check ledger: ferritin was updated with CORRECTION and supersedes previous observation
    state2 = ledger_service.get_lab_ledger_state(cycle.cycle_id)
    active_ferritin = state2.active_markers["ferritin"]
    assert active_ferritin.value == "120.0"
    assert active_ferritin.event_type == LabEventType.CORRECTION.value
    assert active_ferritin.supersedes_observation_id == obs1_id
    assert active_ferritin.sample_date == "2026-10-18"


# --- 8. Vision OCR Does Not Consume 168h Cooldown ---

def test_vision_ocr_does_not_consume_168h_cooldown(client: TestClient, db_session: Session, auth_headers: dict[str, str]):
    cycle_repo = MedicalCycleRepository(db_session)
    cycle_id = "test-cycle-vision-01"
    cycle = cycle_repo.create(cycle_id=cycle_id, status="active")

    mock_gateway = _build_vision_mock_gateway([])
    app.dependency_overrides[get_gemini_gateway] = lambda: mock_gateway

    # Upload lab image
    resp = client.post(
        "/api/v1/cascade/intake/lab-images",
        data={"cycle_id": cycle.cycle_id},
        files=[("files", ("labs.jpg", io.BytesIO(_create_dummy_jpeg_bytes(512)), "image/jpeg"))],
        headers=auth_headers,
    )
    assert resp.status_code == 200

    # Query cooldown endpoint
    cooldown_resp = client.get(
        f"/api/v1/cascade/lab-revision-cooldown?cycle_id={cycle.cycle_id}",
        headers=auth_headers,
    )
    assert cooldown_resp.status_code == 200
    cd = cooldown_resp.json()
    assert cd["is_allowed"] is True
    assert cd["remaining_seconds"] == 0
    assert cd["has_unreviewed_lab_changes"] is True


# --- 9. Cycle Not Found and Unauthorized ---

def test_cycle_not_found_and_unauthorized(client: TestClient, db_session: Session, auth_headers: dict[str, str]):
    mock_gateway = _build_vision_mock_gateway([])
    app.dependency_overrides[get_gemini_gateway] = lambda: mock_gateway

    # Case A: Missing Authorization token
    resp_unauth = client.post(
        "/api/v1/cascade/intake/lab-images",
        data={"cycle_id": "nonexistent-cycle"},
        files=[("files", ("test.jpg", io.BytesIO(_create_dummy_jpeg_bytes(512)), "image/jpeg"))],
    )
    assert resp_unauth.status_code == 401

    # Case B: Non-existent cycle
    resp_404 = client.post(
        "/api/v1/cascade/intake/lab-images",
        data={"cycle_id": "nonexistent-cycle"},
        files=[("files", ("test.jpg", io.BytesIO(_create_dummy_jpeg_bytes(512)), "image/jpeg"))],
        headers=auth_headers,
    )
    assert resp_404.status_code == 404
    assert "not found" in resp_404.json()["detail"].lower()


# --- 10. Gemini Gateway Failure Returns 502 and Transaction Rolls Back ---

def test_gemini_gateway_failure_returns_502(client: TestClient, db_session: Session, auth_headers: dict[str, str]):
    cycle_repo = MedicalCycleRepository(db_session)
    cycle_id = "test-cycle-vision-01"
    cycle = cycle_repo.create(cycle_id=cycle_id, status="active")

    mock_gateway = _build_vision_mock_gateway([], fail_with_status=503)
    app.dependency_overrides[get_gemini_gateway] = lambda: mock_gateway

    resp = client.post(
        "/api/v1/cascade/intake/lab-images",
        data={"cycle_id": cycle.cycle_id},
        files=[("files", ("test.jpg", io.BytesIO(_create_dummy_jpeg_bytes(512)), "image/jpeg"))],
        headers=auth_headers,
    )
    assert resp.status_code == 502
    assert "Gemini Vision OCR extraction failed" in resp.json()["detail"]

    # Verify no preparatory reports or ledger created
    report_repo = ModelReportRepository(db_session)
    assert len(report_repo.list_versions(cycle.cycle_id, PREPARATORY_OCR_REPORT_TYPE)) == 0
    assert len(report_repo.list_versions(cycle.cycle_id, LABORATORY_LEDGER_REPORT_TYPE)) == 0


# --- 11. Anamnesis M1 Remains Strictly Blind to Vision OCR & Ledger ---

def test_anamnesis_m1_remains_strictly_blind_to_vision_ocr(client: TestClient, db_session: Session, auth_headers: dict[str, str]):
    """
    Architectural guarantee:
    Anamnesis M1 must execute in pure blind mode without receiving any lab data,
    even if preparatory_vision_ocr and laboratory_ledger already exist in the cycle.
    """
    cycle_repo = MedicalCycleRepository(db_session)
    cycle_id = "test-cycle-vision-01"
    cycle = cycle_repo.create(cycle_id=cycle_id, status="active")

    recorded_calls: List[Dict[str, Any]] = []

    # Step 1: Upload lab images
    vision_gateway = _build_vision_mock_gateway(recorded_calls)
    app.dependency_overrides[get_gemini_gateway] = lambda: vision_gateway

    resp_ocr = client.post(
        "/api/v1/cascade/intake/lab-images",
        data={"cycle_id": cycle.cycle_id},
        files=[("files", ("labs.jpg", io.BytesIO(_create_dummy_jpeg_bytes(512)), "image/jpeg"))],
        headers=auth_headers,
    )
    assert resp_ocr.status_code == 200
    assert resp_ocr.json()["extracted_count"] == 5

    # Step 2: Now submit Anamnesis M1 intake for the same cycle
    m1_recorded_calls: List[Dict[str, Any]] = []

    async def m1_handler(request: httpx.Request) -> httpx.Response:
        m1_recorded_calls.append({
            "url": str(request.url),
            "body": json.loads(request.content.decode("utf-8")),
        })
        return httpx.Response(
            status_code=200,
            json=_gemini_envelope(_sample_anamnesis_output()),
        )

    m1_gateway = GeminiGateway(api_key="m1-test-key", transport=httpx.MockTransport(m1_handler))
    app.dependency_overrides[get_gemini_gateway] = lambda: m1_gateway

    intake_payload = {
        "cycle_id": cycle.cycle_id,
        "answers": _sample_intake_answers(),
    }
    resp_m1 = client.post(
        "/api/v1/cascade/intake/anamnesis",
        json=intake_payload,
        headers=auth_headers,
    )
    assert resp_m1.status_code == 200

    # Step 3: Inspect the prompt sent to Gemini for Anamnesis M1
    assert len(m1_recorded_calls) == 1
    m1_request_content = m1_recorded_calls[0]["body"]["contents"][0]["parts"][0]["text"]

    forbidden_lab_tokens = [
        "Аланинаминотрансфераза",
        "bilirubin_total",
        "ferritin",
        "glucose",
        "24.5",
        "115.0",
        "laboratory_ledger",
        "shared_labs_table",
    ]
    for token in forbidden_lab_tokens:
        assert token not in m1_request_content, (
            f"Blind isolation violation: M1 prompt contains laboratory token '{token}'"
        )


# --- 12. Uncertain Recognition Isolated From Consilium Ledger ---

def test_uncertain_recognition_isolation_from_consilium_ledger(client: TestClient, db_session: Session, auth_headers: dict[str, str]):
    """
    Critical requirement 1:
    Uncertain, unconfirmed, or non-numeric results requiring user verification
    must NOT be ingested into active_markers of laboratory_ledger as authoritative numbers.
    They must be safely isolated in unconfirmed_markers for user review.
    """
    cycle_repo = MedicalCycleRepository(db_session)
    cycle_id = "test-cycle-uncertain-01"
    cycle = cycle_repo.create(cycle_id=cycle_id, status="active")

    recorded_calls: List[Dict[str, Any]] = []
    mixed_output = {
        "document_metadata": {
            "sample_date": "2026-10-15",
            "issue_date": "2026-10-16",
            "laboratory_name": "Diagnostic Center",
            "page_count": 1,
            "notes": "Partially damaged paper document",
        },
        "markers": [
            # 1. Confirmed numeric marker
            {
                "original_name": "Гемоглобин",
                "marker_key": "hemoglobin",
                "raw_value": "145.0",
                "numeric_value": 145.0,
                "unit": "г/л",
                "reference_range": {"low": 130.0, "high": 170.0, "text": "130 - 170"},
                "sample_date": "2026-10-15",
                "category": "hematology",
                "is_uncertain": False,
                "uncertainty_note": None,
                "is_critical_flag": False,
            },
            # 2. Confirmed qualitative marker
            {
                "original_name": "Антиген HBsAg",
                "marker_key": "hbsag",
                "raw_value": "отрицательно",
                "numeric_value": None,
                "unit": None,
                "reference_range": {"text": "отрицательно"},
                "sample_date": "2026-10-15",
                "category": "infectious",
                "is_uncertain": False,
                "uncertainty_note": None,
                "is_critical_flag": False,
            },
            # 3. Unconfirmed: is_uncertain is True (blurry print)
            {
                "original_name": "Ферритин",
                "marker_key": "ferritin",
                "raw_value": "~2?",
                "numeric_value": None,
                "unit": "мкг/л",
                "reference_range": {"low": 30.0, "high": 400.0, "text": "30 - 400"},
                "sample_date": "2026-10-15",
                "category": "hematology",
                "is_uncertain": True,
                "uncertainty_note": "Цифра размыта пятном на бланке",
                "is_critical_flag": False,
            },
            # 4. Unconfirmed: numeric value without unit of measurement
            {
                "original_name": "Калий",
                "marker_key": "potassium",
                "raw_value": "4.2",
                "numeric_value": 4.2,
                "unit": None,  # Missing unit!
                "reference_range": None,
                "sample_date": "2026-10-15",
                "category": "electrolytes",
                "is_uncertain": False,
                "uncertainty_note": None,
                "is_critical_flag": False,
            },
            # 5. Unconfirmed: non-standard non-numeric text
            {
                "original_name": "Белок в моче",
                "marker_key": "urine_protein",
                "raw_value": "следы сомнительно",
                "numeric_value": None,
                "unit": None,
                "reference_range": None,
                "sample_date": "2026-10-15",
                "category": "urinalysis",
                "is_uncertain": False,
                "uncertainty_note": None,
                "is_critical_flag": False,
            },
            # 6. Unconfirmed: missing specimen date
            {
                "original_name": "Магний",
                "marker_key": "magnesium",
                "raw_value": "0.85",
                "numeric_value": 0.85,
                "unit": "ммоль/л",
                "reference_range": {"low": 0.66, "high": 1.07, "text": "0.66 - 1.07"},
                "sample_date": None,  # No date!
                "category": "biochemistry",
                "is_uncertain": False,
                "uncertainty_note": None,
                "is_critical_flag": False,
            },
        ],
    }

    mock_gateway = _build_vision_mock_gateway(recorded_calls, ocr_override=mixed_output)
    app.dependency_overrides[get_gemini_gateway] = lambda: mock_gateway

    resp = client.post(
        "/api/v1/cascade/intake/lab-images",
        data={"cycle_id": cycle.cycle_id},
        files=[("files", ("mixed.jpg", io.BytesIO(_create_dummy_jpeg_bytes(512)), "image/jpeg"))],
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text
    res = resp.json()

    assert res["extracted_count"] == 6
    assert res["requires_verification_count"] == 1  # only ferritin has OCR recognition ambiguity (smudge/blur)
    # Notice: magnesium inherited doc_metadata.sample_date 2026-10-15.
    # Under C-8, potassium (missing unit) and urine_protein (qualitative) are preserved in confirmed markers!
    assert len(res["confirmed_markers"]) == 5  # hemoglobin, hbsag, magnesium, potassium, urine_protein
    assert len(res["unconfirmed_markers"]) == 1  # ferritin (is_uncertain from OCR blur)

    # Check that confirmed markers entered the authoritative laboratory ledger:
    ledger_service = LabRevisionConsiliumService(db=db_session, gateway=mock_gateway)
    ledger_state = ledger_service.get_lab_ledger_state(cycle.cycle_id)

    assert "hemoglobin" in ledger_state.active_markers
    assert "hbsag" in ledger_state.active_markers
    assert "magnesium" in ledger_state.active_markers

    # C-8 preservation: potassium (missing unit) and urine_protein (qualitative) are preserved in ledger!
    assert "potassium" in ledger_state.active_markers
    pot_obs = ledger_state.active_markers["potassium"]
    assert pot_obs.numeric_value == 4.2
    assert pot_obs.unit is None
    assert pot_obs.is_uncertain is True
    assert "Missing unit of measurement" in (pot_obs.uncertainty_note or "")

    assert "urine_protein" in ledger_state.active_markers
    prot_obs = ledger_state.active_markers["urine_protein"]
    assert prot_obs.value == "следы сомнительно"
    assert prot_obs.is_uncertain is True

    # CRITICAL: only truly unconfirmed recognition-ambiguity markers (ferritin ~2?) are NOT in active_markers of the ledger!
    assert "ferritin" not in ledger_state.active_markers

    # But they ARE preserved in preparatory_vision_ocr on server for client review
    prep_reports = ModelReportRepository(db_session).list_versions(
        cycle_id=cycle.cycle_id,
        report_type=PREPARATORY_OCR_REPORT_TYPE,
        privacy_level=PrivacyLevel.SERVER_ONLY,
    )
    assert len(prep_reports) == 1
    content = prep_reports[0].content
    assert content["confirmed_count"] == 5
    assert content["unconfirmed_count"] == 1
    unconfirmed_keys = [u["marker_key"] for u in content["unconfirmed_markers"]]
    assert "ferritin" in unconfirmed_keys
    assert "potassium" not in unconfirmed_keys
    assert "urine_protein" not in unconfirmed_keys


# --- 13. Multiple Studies and Dates Without Date Leakage ---

def test_multiple_studies_and_dates_in_single_request_without_date_leakage(client: TestClient, db_session: Session, auth_headers: dict[str, str]):
    """
    Critical requirement 2:
    Document upload with multiple studies and different dates:
    - Dates from study 1 are not assigned to study 2 or a dateless marker.
    - Dateless document/marker preserves uncertainty without inventing dates.
    - Repeated markers across different dates are processed in chronological order.
    """
    cycle_repo = MedicalCycleRepository(db_session)
    cycle_id = "test-cycle-multi-dates-01"
    cycle = cycle_repo.create(cycle_id=cycle_id, status="active")

    recorded_calls: List[Dict[str, Any]] = []
    multi_study_output = {
        "document_metadata": {
            # Document metadata has NO sample date (e.g. mixed multi-page bundle)
            "sample_date": None,
            "issue_date": "2026-10-18",
            "laboratory_name": "Multiple Labs Bundle",
            "page_count": 3,
            "notes": "Combined pages from different dates",
        },
        "markers": [
            # Study A: September 1, 2026 from Lab A
            {
                "original_name": "АЛТ (Сентябрь)",
                "marker_key": "alt",
                "raw_value": "45.0",
                "numeric_value": 45.0,
                "unit": "Ед/л",
                "reference_range": {"low": 0.0, "high": 41.0, "text": "0 - 41"},
                "sample_date": "2026-09-01",
                "category": "biochemistry",
                "is_uncertain": False,
                "uncertainty_note": None,
                "is_critical_flag": True,
            },
            # Study B: October 1, 2026 from Lab B (chronologically later)
            {
                "original_name": "АЛТ (Октябрь)",
                "marker_key": "alt",
                "raw_value": "22.0",
                "numeric_value": 22.0,
                "unit": "Ед/л",
                "reference_range": {"low": 0.0, "high": 41.0, "text": "0 - 41"},
                "sample_date": "2026-10-01",
                "category": "biochemistry",
                "is_uncertain": False,
                "uncertainty_note": None,
                "is_critical_flag": False,
            },
            # Study C: Document without established date
            {
                "original_name": "Холестерин общий",
                "marker_key": "cholesterol_total",
                "raw_value": "5.2",
                "numeric_value": 5.2,
                "unit": "ммоль/л",
                "reference_range": {"high": 5.0, "text": "< 5.0"},
                "sample_date": None,  # No date on this page or on document!
                "category": "biochemistry",
                "is_uncertain": False,
                "uncertainty_note": None,
                "is_critical_flag": False,
            },
        ],
    }

    mock_gateway = _build_vision_mock_gateway(recorded_calls, ocr_override=multi_study_output)
    app.dependency_overrides[get_gemini_gateway] = lambda: mock_gateway

    resp = client.post(
        "/api/v1/cascade/intake/lab-images",
        data={"cycle_id": cycle.cycle_id},
        files=[("files", ("bundle.jpg", io.BytesIO(_create_dummy_jpeg_bytes(512)), "image/jpeg"))],
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text
    res = resp.json()

    # The dateless marker requires user verification
    cholesterol = next(m for m in res["markers"] if m["marker_key"] == "cholesterol_total")
    assert cholesterol["sample_date"] is None
    assert cholesterol["requires_user_verification"] is True
    assert "Missing specimen collection date" in cholesterol["uncertainty_note"]

    # In ledger:
    ledger_service = LabRevisionConsiliumService(db=db_session, gateway=mock_gateway)
    ledger_state = ledger_service.get_lab_ledger_state(cycle.cycle_id)

    # Dateless marker is NOT in active ledger
    assert "cholesterol_total" not in ledger_state.active_markers

    # The two ALT studies were processed in chronological order:
    # Latest active ALT is October (22.0, 2026-10-01)
    assert "alt" in ledger_state.active_markers
    active_alt = ledger_state.active_markers["alt"]
    assert active_alt.value == "22.0"
    assert active_alt.sample_date == "2026-10-01"

    # Both observations are preserved in ledger history without date contamination
    alt_history = [h for h in ledger_state.observations_history if h.marker_key == "alt"]
    assert len(alt_history) == 2
    dates_in_history = {h.sample_date for h in alt_history}
    assert dates_in_history == {"2026-09-01", "2026-10-01"}


# --- 14. Selective Correction Targeting Preserves History ---

def test_selective_correction_targeting_preserves_new_study_history(client: TestClient, db_session: Session, auth_headers: dict[str, str]):
    """
    Critical requirement 3:
    When is_correction=True is set:
    - Only markers targeting the same date or explicit correction supersede previous observation.
    - Markers with a new sample date are treated as NEW_STUDY, NOT a destructive correction.
    - Completely new markers are treated as NEW_STUDY without creating invalid supersedes links.
    """
    cycle_repo = MedicalCycleRepository(db_session)
    cycle_id = "test-cycle-corr-targeting-01"
    cycle = cycle_repo.create(cycle_id=cycle_id, status="active")

    recorded_calls: List[Dict[str, Any]] = []

    # Step 1: Initial upload with Ferritin and Glucose from 2026-09-01
    initial_output = {
        "document_metadata": {
            "sample_date": "2026-09-01",
            "issue_date": "2026-09-02",
            "laboratory_name": "Baseline Labs",
            "page_count": 1,
            "notes": None,
        },
        "markers": [
            {
                "original_name": "Ферритин",
                "marker_key": "ferritin",
                "raw_value": "12.0",
                "numeric_value": 12.0,
                "unit": "мкг/л",
                "reference_range": {"low": 30.0, "high": 400.0, "text": "30 - 400"},
                "sample_date": "2026-09-01",
                "category": "hematology",
                "is_uncertain": False,
                "uncertainty_note": None,
                "is_critical_flag": True,
            },
            {
                "original_name": "Глюкоза",
                "marker_key": "glucose",
                "raw_value": "5.1",
                "numeric_value": 5.1,
                "unit": "ммоль/л",
                "reference_range": {"low": 4.1, "high": 5.9, "text": "4.1 - 5.9"},
                "sample_date": "2026-09-01",
                "category": "endocrinology",
                "is_uncertain": False,
                "uncertainty_note": None,
                "is_critical_flag": False,
            },
        ],
    }
    mock_gw1 = _build_vision_mock_gateway(recorded_calls, ocr_override=initial_output)
    app.dependency_overrides[get_gemini_gateway] = lambda: mock_gw1

    resp1 = client.post(
        "/api/v1/cascade/intake/lab-images",
        data={"cycle_id": cycle.cycle_id},
        files=[("files", ("baseline.jpg", io.BytesIO(_create_dummy_jpeg_bytes(512)), "image/jpeg"))],
        headers=auth_headers,
    )
    assert resp1.status_code == 200
    assert resp1.json()["ledger_version"] == 1

    ledger_service = LabRevisionConsiliumService(db=db_session, gateway=mock_gw1)
    state1 = ledger_service.get_lab_ledger_state(cycle.cycle_id)
    prev_ferritin_id = state1.active_markers["ferritin"].observation_id
    prev_glucose_id = state1.active_markers["glucose"].observation_id

    # Step 2: Upload batch with is_correction=True containing:
    # a) Ferritin from same date 2026-09-01 -> legitimate correction!
    # b) Glucose with a NEW date 2026-10-15 -> dynamic new study, NOT an error invalidation!
    # c) Potassium (new marker) -> new observation, NOT a correction of non-existent observation!
    second_batch = {
        "document_metadata": {
            "sample_date": None,
            "issue_date": "2026-10-16",
            "laboratory_name": "Followup Labs",
            "page_count": 1,
            "notes": None,
        },
        "markers": [
            {
                "original_name": "Ферритин (исправленный бланк)",
                "marker_key": "ferritin",
                "raw_value": "120.0",
                "numeric_value": 120.0,
                "unit": "мкг/л",
                "reference_range": {"low": 30.0, "high": 400.0, "text": "30 - 400"},
                "sample_date": "2026-09-01",  # Same date as before
                "category": "hematology",
                "is_uncertain": False,
                "uncertainty_note": None,
                "is_critical_flag": False,
            },
            {
                "original_name": "Глюкоза (повторный контроль)",
                "marker_key": "glucose",
                "raw_value": "4.8",
                "numeric_value": 4.8,
                "unit": "ммоль/л",
                "reference_range": {"low": 4.1, "high": 5.9, "text": "4.1 - 5.9"},
                "sample_date": "2026-10-15",  # NEW date!
                "category": "endocrinology",
                "is_uncertain": False,
                "uncertainty_note": None,
                "is_critical_flag": False,
            },
            {
                "original_name": "Калий",
                "marker_key": "potassium",
                "raw_value": "4.4",
                "numeric_value": 4.4,
                "unit": "ммоль/л",
                "reference_range": {"low": 3.5, "high": 5.1, "text": "3.5 - 5.1"},
                "sample_date": "2026-10-15",  # New marker
                "category": "electrolytes",
                "is_uncertain": False,
                "uncertainty_note": None,
                "is_critical_flag": False,
            },
        ],
    }

    mock_gw2 = _build_vision_mock_gateway(recorded_calls, ocr_override=second_batch)
    app.dependency_overrides[get_gemini_gateway] = lambda: mock_gw2

    resp2 = client.post(
        "/api/v1/cascade/intake/lab-images",
        data={"cycle_id": cycle.cycle_id, "is_correction": "true"},
        files=[("files", ("batch2.jpg", io.BytesIO(_create_dummy_jpeg_bytes(512)), "image/jpeg"))],
        headers=auth_headers,
    )
    assert resp2.status_code == 200
    res2 = resp2.json()
    assert res2["ledger_version"] == 2

    state2 = ledger_service.get_lab_ledger_state(cycle.cycle_id)

    # a) Ferritin WAS superseded via CORRECTION
    active_ferritin = state2.active_markers["ferritin"]
    assert active_ferritin.value == "120.0"
    assert active_ferritin.event_type == LabEventType.CORRECTION.value
    assert active_ferritin.supersedes_observation_id == prev_ferritin_id

    # b) Glucose with NEW date was treated as NEW_STUDY, preserving September study!
    active_glucose = state2.active_markers["glucose"]
    assert active_glucose.value == "4.8"
    assert active_glucose.event_type == LabEventType.NEW_STUDY.value
    assert active_glucose.supersedes_observation_id is None

    # The September glucose is NOT marked as invalid! Both remain in history:
    glucose_history = [h for h in state2.observations_history if h.marker_key == "glucose"]
    assert len(glucose_history) == 2
    assert any(h.value == "5.1" and h.sample_date == "2026-09-01" for h in glucose_history)

    # c) Potassium was created as NEW_STUDY, supersedes nothing
    active_k = state2.active_markers["potassium"]
    assert active_k.event_type == LabEventType.NEW_STUDY.value
    assert active_k.supersedes_observation_id is None


# --- 15. Atomicity, Idempotent Duplicate Upload, and Primary Consilium Lock ---

def test_atomicity_idempotent_duplicate_upload_and_primary_consilium_lock(client: TestClient, db_session: Session, auth_headers: dict[str, str]):
    """
    Critical requirement 4:
    - Re-submitting identical document/markers is idempotent and does not create duplicate ledger versions.
    - Transaction rollback ensures no partial state if ledger fails.
    - Primary Consilium cannot be re-run on new lab data once completed (locked).
    """
    cycle_repo = MedicalCycleRepository(db_session)
    cycle_id = "test-cycle-atomicity-01"
    cycle = cycle_repo.create(cycle_id=cycle_id, status="active")

    recorded_calls: List[Dict[str, Any]] = []
    sample_output = _sample_vision_ocr_output(sample_date="2026-10-10")
    mock_gateway = _build_vision_mock_gateway(recorded_calls, ocr_override=sample_output)
    app.dependency_overrides[get_gemini_gateway] = lambda: mock_gateway

    # 1. Initial upload
    resp1 = client.post(
        "/api/v1/cascade/intake/lab-images",
        data={"cycle_id": cycle.cycle_id},
        files=[("files", ("scan.jpg", io.BytesIO(_create_dummy_jpeg_bytes(512)), "image/jpeg"))],
        headers=auth_headers,
    )
    assert resp1.status_code == 200
    assert resp1.json()["ledger_version"] == 1

    report_repo = ModelReportRepository(db_session)
    assert len(report_repo.list_versions(cycle.cycle_id, PREPARATORY_OCR_REPORT_TYPE)) == 1

    # 2. Idempotent re-upload of identical document
    resp2 = client.post(
        "/api/v1/cascade/intake/lab-images",
        data={"cycle_id": cycle.cycle_id},
        files=[("files", ("scan.jpg", io.BytesIO(_create_dummy_jpeg_bytes(512)), "image/jpeg"))],
        headers=auth_headers,
    )
    assert resp2.status_code == 200
    assert resp2.json()["ledger_version"] == 1  # Version did NOT bump!
    # No duplicate reports created:
    assert len(report_repo.list_versions(cycle.cycle_id, PREPARATORY_OCR_REPORT_TYPE)) == 1

    # 3. Primary Consilium lock on new laboratory changes:
    # Simulate completed Primary Consilium by creating chief_physician_internal report
    report_repo.create_next_version(
        cycle_id=cycle.cycle_id,
        model="gemini-3.1-pro",
        report_type="chief_physician_internal",
        privacy_level=PrivacyLevel.SERVER_ONLY,
        content={"status": "completed"},
    )

    # Subsequent new laboratory intake arrives for this cycle
    new_study_output = _sample_vision_ocr_output(sample_date="2026-10-25")
    new_study_output["markers"].append(
        {
            "marker_key": "crp",
            "original_name": "C-Reactive Protein",
            "raw_value": "1.2",
            "numeric_value": 1.2,
            "unit": "mg/L",
            "sample_date": "2026-10-25",
            "reference_range": None,
            "is_critical_flag": False,
            "category": "immunology",
            "is_uncertain": False,
            "uncertainty_note": None,
        }
    )
    mock_gateway_new = _build_vision_mock_gateway([], ocr_override=new_study_output)
    app.dependency_overrides[get_gemini_gateway] = lambda: mock_gateway_new

    resp3 = client.post(
        "/api/v1/cascade/intake/lab-images",
        data={"cycle_id": cycle.cycle_id},
        files=[("files", ("new_scan.jpg", io.BytesIO(_create_dummy_jpeg_bytes(600)), "image/jpeg"))],
        headers=auth_headers,
    )
    assert resp3.status_code == 200
    assert resp3.json()["ledger_version"] == 2
    assert resp3.json()["has_unreviewed_lab_changes"] is True

    # Attempting to re-run Primary Consilium to evaluate new lab markers must be rejected
    from app.services.primary_consilium_service import PrimaryConsiliumService, ConsiliumError

    primary_service = PrimaryConsiliumService(db=db_session, gateway=mock_gateway)
    with pytest.raises(ConsiliumError) as exc_info:
        asyncio.run(primary_service.run_primary_consilium(cycle.cycle_id))
    assert "already been completed" in str(exc_info.value).lower()
    assert "full lab-revision consilium" in str(exc_info.value).lower()
