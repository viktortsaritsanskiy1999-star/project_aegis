import asyncio
import base64
from datetime import datetime, timezone
import hashlib
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
    LabMarkerMutationItem,
    LabObservationEntry,
    VisionOcrMarkerItem,
    VisionOcrDocumentMetadata,
    VisionOcrModelOutput,
    LabVisionIntakeClientResponse,
    normalize_lab_date,
    normalize_marker_slug,
    is_dimensionless_marker,
    to_decimal,
    are_lab_values_equivalent,
)
from app.schemas.privacy import PrivacyLevel
from app.services.gemini_gateway import GeminiGateway, GeminiModelRole
from app.services.lab_revision_consilium_service import (
    LabRevisionConsiliumService,
    LABORATORY_LEDGER_REPORT_TYPE,
)
from app.services.lab_vision_ocr_service import (
    LabVisionOcrService,
)
from app.services.primary_consilium_service import (
    PREPARATORY_OCR_REPORT_TYPE,
)
from tests.test_primary_consilium import _gemini_envelope
from tests.test_lab_vision_ocr import (
    _create_dummy_jpeg_bytes,
    _create_dummy_png_bytes,
)


def _build_mock_gateway(
    recorded_calls: List[Dict[str, Any]],
    responses: Optional[List[Dict[str, Any]]] = None,
    default_response: Optional[Dict[str, Any]] = None,
) -> GeminiGateway:
    resp_queue = list(responses) if responses else []

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        recorded_calls.append({
            "url": str(request.url),
            "body": body,
        })
        if resp_queue:
            payload = resp_queue.pop(0)
        else:
            payload = default_response or {}

        return httpx.Response(
            status_code=200,
            json=_gemini_envelope(payload),
        )

    transport = httpx.MockTransport(handler)
    return GeminiGateway(api_key="test-c7-key", transport=transport)


# ==============================================================================
# 1. DATE NORMALIZATION AND BOUNDARY VERIFICATION
# ==============================================================================

def test_date_normalization_valid_and_invalid_formats():
    """Validates calendar correctness, leap years, and diverse date formats."""
    # Strict ISO
    norm, ok = normalize_lab_date("2026-10-05")
    assert ok and norm == "2026-10-05"

    # European / Russian dot format
    norm, ok = normalize_lab_date("05.10.2026")
    assert ok and norm == "2026-10-05"

    # European / Russian slash format
    norm, ok = normalize_lab_date("05/10/2026")
    assert ok and norm == "2026-10-05"

    # European / Russian dash format
    norm, ok = normalize_lab_date("05-10-2026")
    assert ok and norm == "2026-10-05"

    # Slash / Dot YYYY/MM/DD
    norm, ok = normalize_lab_date("2026/10/05")
    assert ok and norm == "2026-10-05"
    norm, ok = normalize_lab_date("2026.10.05")
    assert ok and norm == "2026-10-05"

    # ISO datetime with timestamp and timezone
    norm, ok = normalize_lab_date("2026-10-05T14:30:00Z")
    assert ok and norm == "2026-10-05"
    norm, ok = normalize_lab_date("2026-10-05 09:15:00+03:00")
    assert ok and norm == "2026-10-05"

    # Valid Leap Year
    norm, ok = normalize_lab_date("2024-02-29")
    assert ok and norm == "2024-02-29"

    # Non-Leap Year (must be rejected)
    norm, ok = normalize_lab_date("2025-02-29")
    assert not ok and norm is None

    # Invalid calendar dates
    norm, ok = normalize_lab_date("2026-02-30")
    assert not ok and norm is None
    norm, ok = normalize_lab_date("2026-04-31")
    assert not ok and norm is None
    norm, ok = normalize_lab_date("2026-13-01")
    assert not ok and norm is None
    norm, ok = normalize_lab_date("32.01.2026")
    assert not ok and norm is None

    # Garbage / Free text with dates embedded
    norm, ok = normalize_lab_date("2026-10-21 Patient: John Doe")
    assert not ok and norm is None

    # Empty / None handling
    norm, ok = normalize_lab_date(None)
    assert ok and norm is None
    norm, ok = normalize_lab_date("")
    assert ok and norm is None
    norm, ok = normalize_lab_date("   ")
    assert ok and norm is None


# ==============================================================================
# 2. RE-UPLOAD INVARIANCE (A -> A) SKIPS GEMINI VISION
# ==============================================================================

def test_reupload_invariance_a_to_a_skips_gemini(
    client: TestClient, db_session: Session, auth_headers: dict[str, str]
):
    cycle_repo = MedicalCycleRepository(db_session)
    cycle = cycle_repo.create(cycle_id="c7-cycle-a-to-a", status="active")

    recorded_calls: List[Dict[str, Any]] = []
    ocr_payload_a = {
        "document_metadata": {
            "sample_date": "2026-10-15",
            "issue_date": "2026-10-16",
            "laboratory_name": "Helix Diagnostics",
            "page_count": 1,
            "order_id": "ORD-A-001",
        },
        "markers": [
            {
                "original_name": "Глюкоза",
                "marker_key": "glucose",
                "raw_value": "5.2",
                "numeric_value": 5.2,
                "unit": "ммоль/л",
                "reference_range": {"low": 4.1, "high": 5.9, "text": "4.1 - 5.9"},
                "sample_date": "2026-10-15",
                "category": "endocrinology",
                "is_uncertain": False,
                "is_critical_flag": False,
            },
            {
                "original_name": "Холестерин общий",
                "marker_key": "cholesterol_total",
                "raw_value": "4.8",
                "numeric_value": 4.8,
                "unit": "ммоль/л",
                "reference_range": {"low": 3.0, "high": 5.2, "text": "3.0 - 5.2"},
                "sample_date": "2026-10-15",
                "category": "biochemistry",
                "is_uncertain": False,
                "is_critical_flag": False,
            },
        ],
    }

    mock_gateway = _build_mock_gateway(recorded_calls, default_response=ocr_payload_a)
    app.dependency_overrides[get_gemini_gateway] = lambda: mock_gateway

    jpeg_a = _create_dummy_jpeg_bytes(512)

    # 1. Initial upload of Document A
    files_1 = [("files", ("report_a.jpg", io.BytesIO(jpeg_a), "image/jpeg"))]
    resp_1 = client.post(
        "/api/v1/cascade/intake/lab-images",
        data={"cycle_id": cycle.cycle_id, "is_correction": "false"},
        files=files_1,
        headers=auth_headers,
    )
    assert resp_1.status_code == 200, resp_1.text
    data_1 = resp_1.json()
    assert data_1["ledger_version"] == 1
    assert data_1["extracted_count"] == 2
    assert len(recorded_calls) == 1

    # Verify ledger state in DB
    lab_service = LabRevisionConsiliumService(db=db_session)
    state_1 = lab_service.get_lab_ledger_state(cycle.cycle_id)
    assert state_1.ledger_version == 1
    assert len(state_1.observations_history) == 2

    # 2. Re-upload identical file Document A
    files_2 = [("files", ("report_a_again.jpg", io.BytesIO(jpeg_a), "image/jpeg"))]
    resp_2 = client.post(
        "/api/v1/cascade/intake/lab-images",
        data={"cycle_id": cycle.cycle_id, "is_correction": "false"},
        files=files_2,
        headers=auth_headers,
    )
    assert resp_2.status_code == 200, resp_2.text
    data_2 = resp_2.json()

    # Gemini Vision must NOT have been called a second time
    assert len(recorded_calls) == 1
    # Ledger version unchanged, no duplicates in history
    assert data_2["ledger_version"] == 1
    state_2 = lab_service.get_lab_ledger_state(cycle.cycle_id)
    assert state_2.ledger_version == 1
    assert len(state_2.observations_history) == 2


# ==============================================================================
# 3. RE-UPLOAD INVARIANCE (A -> B -> A)
# ==============================================================================

def test_reupload_invariance_a_b_a_preserves_version_and_history(
    client: TestClient, db_session: Session, auth_headers: dict[str, str]
):
    cycle_repo = MedicalCycleRepository(db_session)
    cycle = cycle_repo.create(cycle_id="c7-cycle-a-b-a", status="active")

    recorded_calls: List[Dict[str, Any]] = []

    ocr_payload_a = {
        "document_metadata": {
            "sample_date": "2026-09-01",
            "laboratory_name": "Lab A",
            "page_count": 1,
            "order_id": "ORD-AAA",
        },
        "markers": [
            {
                "original_name": "Глюкоза",
                "marker_key": "glucose",
                "raw_value": "5.0",
                "numeric_value": 5.0,
                "unit": "ммоль/л",
                "sample_date": "2026-09-01",
                "category": "endocrinology",
                "is_uncertain": False,
                "is_critical_flag": False,
            },
            {
                "original_name": "АЛТ",
                "marker_key": "alt",
                "raw_value": "22.0",
                "numeric_value": 22.0,
                "unit": "Ед/л",
                "sample_date": "2026-09-01",
                "category": "biochemistry",
                "is_uncertain": False,
                "is_critical_flag": False,
            },
        ],
    }

    ocr_payload_b = {
        "document_metadata": {
            "sample_date": "2026-10-01",
            "laboratory_name": "Lab B",
            "page_count": 1,
            "order_id": "ORD-BBB",
        },
        "markers": [
            {
                "original_name": "Глюкоза",
                "marker_key": "glucose",
                "raw_value": "6.1",
                "numeric_value": 6.1,
                "unit": "ммоль/л",
                "sample_date": "2026-10-01",
                "category": "endocrinology",
                "is_uncertain": False,
                "is_critical_flag": False,
            },
            {
                "original_name": "Креатинин",
                "marker_key": "creatinine",
                "raw_value": "85.0",
                "numeric_value": 85.0,
                "unit": "мкмоль/л",
                "sample_date": "2026-10-01",
                "category": "nephrology",
                "is_uncertain": False,
                "is_critical_flag": False,
            },
        ],
    }

    mock_gateway = _build_mock_gateway(
        recorded_calls,
        responses=[ocr_payload_a, ocr_payload_b],
    )
    app.dependency_overrides[get_gemini_gateway] = lambda: mock_gateway

    bytes_a = _create_dummy_jpeg_bytes(500)
    bytes_b = _create_dummy_png_bytes(600)

    # 1. Upload A
    resp_a = client.post(
        "/api/v1/cascade/intake/lab-images",
        data={"cycle_id": cycle.cycle_id, "is_correction": "false"},
        files=[("files", ("doc_a.jpg", io.BytesIO(bytes_a), "image/jpeg"))],
        headers=auth_headers,
    )
    assert resp_a.status_code == 200
    assert resp_a.json()["ledger_version"] == 1
    assert len(recorded_calls) == 1

    # 2. Upload B
    resp_b = client.post(
        "/api/v1/cascade/intake/lab-images",
        data={"cycle_id": cycle.cycle_id, "is_correction": "false"},
        files=[("files", ("doc_b.png", io.BytesIO(bytes_b), "image/png"))],
        headers=auth_headers,
    )
    assert resp_b.status_code == 200
    assert resp_b.json()["ledger_version"] == 2
    assert len(recorded_calls) == 2

    lab_service = LabRevisionConsiliumService(db=db_session)
    state_after_b = lab_service.get_lab_ledger_state(cycle.cycle_id)
    assert state_after_b.ledger_version == 2
    assert len(state_after_b.observations_history) == 4

    # 3. Re-upload A
    resp_a2 = client.post(
        "/api/v1/cascade/intake/lab-images",
        data={"cycle_id": cycle.cycle_id, "is_correction": "false"},
        files=[("files", ("doc_a_again.jpg", io.BytesIO(bytes_a), "image/jpeg"))],
        headers=auth_headers,
    )
    assert resp_a2.status_code == 200
    data_a2 = resp_a2.json()

    # Gemini must NOT have been called for re-upload A
    assert len(recorded_calls) == 2
    # Must preserve current ledger version 2, NOT revert to 1, NOT bump to 3
    assert data_a2["ledger_version"] == 2

    # Observations history must remain intact without duplicates
    state_after_a2 = lab_service.get_lab_ledger_state(cycle.cycle_id)
    assert state_after_a2.ledger_version == 2
    assert len(state_after_a2.observations_history) == 4
    # Glucose active marker should still be from B (the more recent date 2026-10-01)
    assert state_after_a2.active_markers["glucose"].sample_date == "2026-10-01"
    assert state_after_a2.active_markers["glucose"].value == "6.1"


# ==============================================================================
# 4. MULTI-PAGE FINGERPRINT ORDER-INVARIANCE
# ==============================================================================

def test_multipage_fingerprint_order_invariance(
    client: TestClient, db_session: Session, auth_headers: dict[str, str]
):
    cycle_repo = MedicalCycleRepository(db_session)
    cycle = cycle_repo.create(cycle_id="c7-cycle-multipage", status="active")

    recorded_calls: List[Dict[str, Any]] = []
    ocr_payload = {
        "document_metadata": {
            "sample_date": "2026-10-10",
            "laboratory_name": "MultiPage Lab",
            "page_count": 2,
            "order_id": "ORD-MP",
        },
        "markers": [
            {
                "original_name": "Гемоглобин",
                "marker_key": "hemoglobin",
                "raw_value": "142.0",
                "numeric_value": 142.0,
                "unit": "г/л",
                "sample_date": "2026-10-10",
                "category": "hematology",
                "is_uncertain": False,
                "is_critical_flag": False,
            },
            {
                "original_name": "Лейкоциты",
                "marker_key": "wbc",
                "raw_value": "6.5",
                "numeric_value": 6.5,
                "unit": "10^9/л",
                "sample_date": "2026-10-10",
                "category": "hematology",
                "is_uncertain": False,
                "is_critical_flag": False,
            },
        ],
    }

    mock_gateway = _build_mock_gateway(recorded_calls, default_response=ocr_payload)
    app.dependency_overrides[get_gemini_gateway] = lambda: mock_gateway

    page1_bytes = _create_dummy_jpeg_bytes(400)
    page2_bytes = _create_dummy_png_bytes(500)

    # 1. Upload pages in order [page1, page2]
    files_order_1 = [
        ("files", ("page1.jpg", io.BytesIO(page1_bytes), "image/jpeg")),
        ("files", ("page2.png", io.BytesIO(page2_bytes), "image/png")),
    ]
    resp_1 = client.post(
        "/api/v1/cascade/intake/lab-images",
        data={"cycle_id": cycle.cycle_id, "is_correction": "false"},
        files=files_order_1,
        headers=auth_headers,
    )
    assert resp_1.status_code == 200
    assert resp_1.json()["ledger_version"] == 1
    assert len(recorded_calls) == 1

    # 2. Upload pages in reversed order [page2, page1]
    files_order_2 = [
        ("files", ("page2_rev.png", io.BytesIO(page2_bytes), "image/png")),
        ("files", ("page1_rev.jpg", io.BytesIO(page1_bytes), "image/jpeg")),
    ]
    resp_2 = client.post(
        "/api/v1/cascade/intake/lab-images",
        data={"cycle_id": cycle.cycle_id, "is_correction": "false"},
        files=files_order_2,
        headers=auth_headers,
    )
    assert resp_2.status_code == 200
    # Must skip Gemini Vision because sorted hashes produce identical document_fingerprint
    assert len(recorded_calls) == 1
    assert resp_2.json()["ledger_version"] == 1


# ==============================================================================
# 5. DIFFERENT PHOTO OF SAME MULTI-MARKER PANEL DEDUPLICATED
# ==============================================================================

def test_different_photo_same_multi_marker_panel_deduplicated(
    client: TestClient, db_session: Session, auth_headers: dict[str, str]
):
    cycle_repo = MedicalCycleRepository(db_session)
    cycle = cycle_repo.create(cycle_id="c7-cycle-panel-dedup", status="active")

    recorded_calls: List[Dict[str, Any]] = []

    # Panel with 3 markers
    panel_markers = [
        {
            "original_name": "АЛТ",
            "marker_key": "alt",
            "raw_value": "25.0",
            "numeric_value": 25.0,
            "unit": "Ед/л",
            "sample_date": "2026-10-01",
            "category": "biochemistry",
            "is_uncertain": False,
            "is_critical_flag": False,
        },
        {
            "original_name": "АСТ",
            "marker_key": "ast",
            "raw_value": "22.0",
            "numeric_value": 22.0,
            "unit": "Ед/л",
            "sample_date": "2026-10-01",
            "category": "biochemistry",
            "is_uncertain": False,
            "is_critical_flag": False,
        },
        {
            "original_name": "Билирубин общий",
            "marker_key": "bilirubin_total",
            "raw_value": "15.0",
            "numeric_value": 15.0,
            "unit": "мкмоль/л",
            "sample_date": "2026-10-01",
            "category": "biochemistry",
            "is_uncertain": False,
            "is_critical_flag": False,
        },
    ]

    ocr_payload_1 = {
        "document_metadata": {
            "sample_date": "2026-10-01",
            "laboratory_name": "Invitro",
            "page_count": 1,
        },
        "markers": panel_markers,
    }

    # Second photo extracts identical panel
    ocr_payload_2 = {
        "document_metadata": {
            "sample_date": "2026-10-01",
            "laboratory_name": "Invitro",
            "page_count": 1,
        },
        "markers": panel_markers,
    }

    mock_gateway = _build_mock_gateway(
        recorded_calls,
        responses=[ocr_payload_1, ocr_payload_2],
    )
    app.dependency_overrides[get_gemini_gateway] = lambda: mock_gateway

    photo_1 = _create_dummy_jpeg_bytes(300)
    photo_2 = _create_dummy_jpeg_bytes(350)  # different length => different fingerprint

    resp_1 = client.post(
        "/api/v1/cascade/intake/lab-images",
        data={"cycle_id": cycle.cycle_id, "is_correction": "false"},
        files=[("files", ("photo1.jpg", io.BytesIO(photo_1), "image/jpeg"))],
        headers=auth_headers,
    )
    assert resp_1.status_code == 200
    assert resp_1.json()["ledger_version"] == 1

    lab_service = LabRevisionConsiliumService(db=db_session)
    state_1 = lab_service.get_lab_ledger_state(cycle.cycle_id)
    assert len(state_1.observations_history) == 3

    # Upload photo 2 (different photo of the same panel)
    resp_2 = client.post(
        "/api/v1/cascade/intake/lab-images",
        data={"cycle_id": cycle.cycle_id, "is_correction": "false"},
        files=[("files", ("photo2.jpg", io.BytesIO(photo_2), "image/jpeg"))],
        headers=auth_headers,
    )
    assert resp_2.status_code == 200
    # Gemini was called for photo 2
    assert len(recorded_calls) == 2

    # But ledger must NOT have been mutated: version is still 1, history count is still 3
    assert resp_2.json()["ledger_version"] == 1
    state_2 = lab_service.get_lab_ledger_state(cycle.cycle_id)
    assert state_2.ledger_version == 1
    assert len(state_2.observations_history) == 3


# ==============================================================================
# 6. SAME VALUES WITH DIFFERENT DATES PRESERVED AS NEW STUDY
# ==============================================================================

def test_same_values_different_dates_preserved_as_new_study(
    client: TestClient, db_session: Session, auth_headers: dict[str, str]
):
    cycle_repo = MedicalCycleRepository(db_session)
    cycle = cycle_repo.create(cycle_id="c7-cycle-dates-diff", status="active")

    recorded_calls: List[Dict[str, Any]] = []

    ocr_payload_june = {
        "document_metadata": {
            "sample_date": "2026-06-01",
            "laboratory_name": "City Clinic",
            "page_count": 1,
        },
        "markers": [
            {
                "original_name": "Глюкоза",
                "marker_key": "glucose",
                "raw_value": "5.5",
                "numeric_value": 5.5,
                "unit": "ммоль/л",
                "sample_date": "2026-06-01",
                "category": "endocrinology",
                "is_uncertain": False,
                "is_critical_flag": False,
            },
        ],
    }

    ocr_payload_october = {
        "document_metadata": {
            "sample_date": "2026-10-01",
            "laboratory_name": "City Clinic",
            "page_count": 1,
        },
        "markers": [
            {
                "original_name": "Глюкоза",
                "marker_key": "glucose",
                "raw_value": "5.5",
                "numeric_value": 5.5,
                "unit": "ммоль/л",
                "sample_date": "2026-10-01",
                "category": "endocrinology",
                "is_uncertain": False,
                "is_critical_flag": False,
            },
        ],
    }

    mock_gateway = _build_mock_gateway(
        recorded_calls,
        responses=[ocr_payload_june, ocr_payload_october],
    )
    app.dependency_overrides[get_gemini_gateway] = lambda: mock_gateway

    photo_june = _create_dummy_jpeg_bytes(310)
    photo_oct = _create_dummy_jpeg_bytes(320)

    # Ingest June
    resp_1 = client.post(
        "/api/v1/cascade/intake/lab-images",
        data={"cycle_id": cycle.cycle_id, "is_correction": "false"},
        files=[("files", ("june.jpg", io.BytesIO(photo_june), "image/jpeg"))],
        headers=auth_headers,
    )
    assert resp_1.status_code == 200
    assert resp_1.json()["ledger_version"] == 1

    # Ingest October (same marker, same value, but distinct date)
    resp_2 = client.post(
        "/api/v1/cascade/intake/lab-images",
        data={"cycle_id": cycle.cycle_id, "is_correction": "false"},
        files=[("files", ("october.jpg", io.BytesIO(photo_oct), "image/jpeg"))],
        headers=auth_headers,
    )
    assert resp_2.status_code == 200
    # Must increment ledger version to 2
    assert resp_2.json()["ledger_version"] == 2

    lab_service = LabRevisionConsiliumService(db=db_session)
    state = lab_service.get_lab_ledger_state(cycle.cycle_id)
    assert state.ledger_version == 2
    # Both observations must be preserved in history (longitudinal tracking)
    assert len(state.observations_history) == 2
    dates = [obs.sample_date for obs in state.observations_history]
    assert "2026-06-01" in dates and "2026-10-01" in dates
    # Active marker must be the latest date
    assert state.active_markers["glucose"].sample_date == "2026-10-01"


# ==============================================================================
# 7. AMBIGUOUS SINGLE-MARKER DOCUMENT FROM DIFFERENT PHOTO NOT FALSELY MERGED
# ==============================================================================

def test_single_marker_ambiguous_doc_from_different_photo_not_falsely_merged(
    client: TestClient, db_session: Session, auth_headers: dict[str, str]
):
    cycle_repo = MedicalCycleRepository(db_session)
    cycle = cycle_repo.create(cycle_id="c7-cycle-single-marker", status="active")

    recorded_calls: List[Dict[str, Any]] = []

    # Single marker without order_id (e.g. repeated blood glucose test on the same day)
    single_marker_payload = {
        "document_metadata": {
            "sample_date": "2026-10-15",
            "laboratory_name": "Express POC",
            "page_count": 1,
            "order_id": None,
        },
        "markers": [
            {
                "original_name": "Глюкоза экспресс",
                "marker_key": "glucose",
                "raw_value": "5.5",
                "numeric_value": 5.5,
                "unit": "ммоль/л",
                "sample_date": "2026-10-15",
                "category": "endocrinology",
                "is_uncertain": False,
                "is_critical_flag": False,
            },
        ],
    }

    mock_gateway = _build_mock_gateway(
        recorded_calls,
        responses=[single_marker_payload, single_marker_payload],
    )
    app.dependency_overrides[get_gemini_gateway] = lambda: mock_gateway

    photo_1 = _create_dummy_jpeg_bytes(250)
    photo_2 = _create_dummy_jpeg_bytes(275)

    # First photo upload
    resp_1 = client.post(
        "/api/v1/cascade/intake/lab-images",
        data={"cycle_id": cycle.cycle_id, "is_correction": "false"},
        files=[("files", ("poc1.jpg", io.BytesIO(photo_1), "image/jpeg"))],
        headers=auth_headers,
    )
    assert resp_1.status_code == 200
    assert resp_1.json()["ledger_version"] == 1

    # Second photo upload: different photo, single marker, no order_id.
    # Must NOT automatically merge (could be repeated test at different time).
    resp_2 = client.post(
        "/api/v1/cascade/intake/lab-images",
        data={"cycle_id": cycle.cycle_id, "is_correction": "false"},
        files=[("files", ("poc2.jpg", io.BytesIO(photo_2), "image/jpeg"))],
        headers=auth_headers,
    )
    assert resp_2.status_code == 200
    assert resp_2.json()["ledger_version"] == 2

    lab_service = LabRevisionConsiliumService(db=db_session)
    state = lab_service.get_lab_ledger_state(cycle.cycle_id)
    assert len(state.observations_history) == 2


# ==============================================================================
# 8. SINGLE-MARKER WITH MATCHING ORDER_ID DEDUPLICATED
# ==============================================================================

def test_single_marker_with_matching_order_id_deduplicated(
    client: TestClient, db_session: Session, auth_headers: dict[str, str]
):
    cycle_repo = MedicalCycleRepository(db_session)
    cycle = cycle_repo.create(cycle_id="c7-cycle-single-order-id", status="active")

    recorded_calls: List[Dict[str, Any]] = []

    single_marker_payload = {
        "document_metadata": {
            "sample_date": "2026-10-15",
            "laboratory_name": "Express POC",
            "page_count": 1,
            "order_id": "POC-ORDER-999",
        },
        "markers": [
            {
                "original_name": "Глюкоза экспресс",
                "marker_key": "glucose",
                "raw_value": "5.5",
                "numeric_value": 5.5,
                "unit": "ммоль/л",
                "sample_date": "2026-10-15",
                "category": "endocrinology",
                "is_uncertain": False,
                "is_critical_flag": False,
            },
        ],
    }

    mock_gateway = _build_mock_gateway(
        recorded_calls,
        responses=[single_marker_payload, single_marker_payload],
    )
    app.dependency_overrides[get_gemini_gateway] = lambda: mock_gateway

    photo_1 = _create_dummy_jpeg_bytes(260)
    photo_2 = _create_dummy_jpeg_bytes(290)

    # First photo upload
    resp_1 = client.post(
        "/api/v1/cascade/intake/lab-images",
        data={"cycle_id": cycle.cycle_id, "is_correction": "false"},
        files=[("files", ("poc1.jpg", io.BytesIO(photo_1), "image/jpeg"))],
        headers=auth_headers,
    )
    assert resp_1.status_code == 200
    assert resp_1.json()["ledger_version"] == 1

    # Second photo upload with same order_id: must safely deduplicate
    resp_2 = client.post(
        "/api/v1/cascade/intake/lab-images",
        data={"cycle_id": cycle.cycle_id, "is_correction": "false"},
        files=[("files", ("poc2.jpg", io.BytesIO(photo_2), "image/jpeg"))],
        headers=auth_headers,
    )
    assert resp_2.status_code == 200
    assert resp_2.json()["ledger_version"] == 1

    lab_service = LabRevisionConsiliumService(db=db_session)
    state = lab_service.get_lab_ledger_state(cycle.cycle_id)
    assert state.ledger_version == 1
    assert len(state.observations_history) == 1


# ==============================================================================
# 9. CONFLICTING ORDER_ID PREVENTS FALSE MERGE
# ==============================================================================

def test_conflicting_order_id_prevents_merge(
    client: TestClient, db_session: Session, auth_headers: dict[str, str]
):
    cycle_repo = MedicalCycleRepository(db_session)
    cycle = cycle_repo.create(cycle_id="c7-cycle-order-conflict", status="active")

    recorded_calls: List[Dict[str, Any]] = []

    panel_order_1 = {
        "document_metadata": {
            "sample_date": "2026-10-15",
            "laboratory_name": "Clinic",
            "page_count": 1,
            "order_id": "ORD-111",
        },
        "markers": [
            {
                "original_name": "АЛТ",
                "marker_key": "alt",
                "raw_value": "20.0",
                "numeric_value": 20.0,
                "unit": "Ед/л",
                "sample_date": "2026-10-15",
                "category": "biochemistry",
                "is_uncertain": False,
                "is_critical_flag": False,
            },
            {
                "original_name": "АСТ",
                "marker_key": "ast",
                "raw_value": "19.0",
                "numeric_value": 19.0,
                "unit": "Ед/л",
                "sample_date": "2026-10-15",
                "category": "biochemistry",
                "is_uncertain": False,
                "is_critical_flag": False,
            },
        ],
    }

    panel_order_2 = {
        "document_metadata": {
            "sample_date": "2026-10-15",
            "laboratory_name": "Clinic",
            "page_count": 1,
            "order_id": "ORD-222",  # Different order ID
        },
        "markers": [
            {
                "original_name": "АЛТ",
                "marker_key": "alt",
                "raw_value": "20.0",
                "numeric_value": 20.0,
                "unit": "Ед/л",
                "sample_date": "2026-10-15",
                "category": "biochemistry",
                "is_uncertain": False,
                "is_critical_flag": False,
            },
            {
                "original_name": "АСТ",
                "marker_key": "ast",
                "raw_value": "19.0",
                "numeric_value": 19.0,
                "unit": "Ед/л",
                "sample_date": "2026-10-15",
                "category": "biochemistry",
                "is_uncertain": False,
                "is_critical_flag": False,
            },
        ],
    }

    mock_gateway = _build_mock_gateway(
        recorded_calls,
        responses=[panel_order_1, panel_order_2],
    )
    app.dependency_overrides[get_gemini_gateway] = lambda: mock_gateway

    photo_1 = _create_dummy_jpeg_bytes(315)
    photo_2 = _create_dummy_jpeg_bytes(325)

    resp_1 = client.post(
        "/api/v1/cascade/intake/lab-images",
        data={"cycle_id": cycle.cycle_id, "is_correction": "false"},
        files=[("files", ("p1.jpg", io.BytesIO(photo_1), "image/jpeg"))],
        headers=auth_headers,
    )
    assert resp_1.status_code == 200
    assert resp_1.json()["ledger_version"] == 1

    resp_2 = client.post(
        "/api/v1/cascade/intake/lab-images",
        data={"cycle_id": cycle.cycle_id, "is_correction": "false"},
        files=[("files", ("p2.jpg", io.BytesIO(photo_2), "image/jpeg"))],
        headers=auth_headers,
    )
    assert resp_2.status_code == 200
    # Must NOT merge because order IDs explicitly conflict
    assert resp_2.json()["ledger_version"] == 2

    lab_service = LabRevisionConsiliumService(db=db_session)
    state = lab_service.get_lab_ledger_state(cycle.cycle_id)
    assert len(state.observations_history) == 4


# ==============================================================================
# 10. MISSING COLLECTION DATE PRESERVES NONE AND FLAGS UNCERTAINTY
# ==============================================================================

def test_missing_collection_date_preserves_none_and_flags_uncertainty(
    client: TestClient, db_session: Session, auth_headers: dict[str, str]
):
    cycle_repo = MedicalCycleRepository(db_session)
    cycle = cycle_repo.create(cycle_id="c7-cycle-missing-date", status="active")

    recorded_calls: List[Dict[str, Any]] = []

    ocr_payload_missing_date = {
        "document_metadata": {
            "sample_date": None,  # Missing collection date
            "issue_date": "2026-10-18",
            "laboratory_name": "Undated Report",
            "page_count": 1,
        },
        "markers": [
            {
                "original_name": "Холестерин ЛПВП",
                "marker_key": "hdl",
                "raw_value": "1.4",
                "numeric_value": 1.4,
                "unit": "ммоль/л",
                "sample_date": None,
                "category": "lipidology",
                "is_uncertain": False,
                "is_critical_flag": False,
            },
        ],
    }

    mock_gateway = _build_mock_gateway(
        recorded_calls,
        default_response=ocr_payload_missing_date,
    )
    app.dependency_overrides[get_gemini_gateway] = lambda: mock_gateway

    photo = _create_dummy_jpeg_bytes(400)

    resp = client.post(
        "/api/v1/cascade/intake/lab-images",
        data={"cycle_id": cycle.cycle_id, "is_correction": "false"},
        files=[("files", ("undated.jpg", io.BytesIO(photo), "image/jpeg"))],
        headers=auth_headers,
    )
    assert resp.status_code == 200
    data = resp.json()

    # The marker without a date must require verification
    assert data["requires_verification_count"] == 1
    marker = data["markers"][0]
    assert marker["sample_date"] is None
    assert marker["requires_user_verification"] is True
    assert "Missing specimen collection date" in marker["uncertainty_note"]

    # Since all markers required verification, none were auto-ingested into ledger
    assert data["ledger_version"] == 0

    # Test baseline seeding with missing date does not substitute today
    report_repo = ModelReportRepository(db_session)
    report_repo.create_next_version(
        cycle_id=cycle.cycle_id,
        model="aegis-vision-ocr",
        report_type=PREPARATORY_OCR_REPORT_TYPE,
        privacy_level=PrivacyLevel.SERVER_ONLY,
        content={
            "confirmed_observations": [
                {
                    "marker_key": "ldl",
                    "value": "2.8",
                    "unit": "ммоль/л",
                    "sample_date": None,  # strictly None
                }
            ]
        },
    )

    lab_service = LabRevisionConsiliumService(db=db_session)
    seeded_state = lab_service.get_lab_ledger_state(cycle.cycle_id)
    assert seeded_state.ledger_version == 1
    ldl_obs = seeded_state.active_markers["ldl"]
    assert ldl_obs.sample_date is None
    assert ldl_obs.is_uncertain is True
    assert "Missing specimen collection date" in (ldl_obs.uncertainty_note or "")


# ==============================================================================
# 11. OCR CORRECTION VS NEW STUDY
# ==============================================================================

def test_ocr_correction_supersedes_prior_observation(
    client: TestClient, db_session: Session, auth_headers: dict[str, str]
):
    cycle_repo = MedicalCycleRepository(db_session)
    cycle = cycle_repo.create(cycle_id="c7-cycle-correction", status="active")

    recorded_calls: List[Dict[str, Any]] = []

    # 1. First upload: OCR misread ALT as 245.0
    misread_payload = {
        "document_metadata": {
            "sample_date": "2026-10-10",
            "laboratory_name": "Clinic",
            "page_count": 1,
        },
        "markers": [
            {
                "original_name": "АЛТ",
                "marker_key": "alt",
                "raw_value": "245.0",
                "numeric_value": 245.0,
                "unit": "Ед/л",
                "sample_date": "2026-10-10",
                "category": "biochemistry",
                "is_uncertain": False,
                "is_critical_flag": False,
            },
        ],
    }

    # 2. Corrected upload: ALT is 24.5
    corrected_payload = {
        "document_metadata": {
            "sample_date": "2026-10-10",
            "laboratory_name": "Clinic",
            "page_count": 1,
        },
        "markers": [
            {
                "original_name": "АЛТ",
                "marker_key": "alt",
                "raw_value": "24.5",
                "numeric_value": 24.5,
                "unit": "Ед/л",
                "sample_date": "2026-10-10",
                "category": "biochemistry",
                "is_uncertain": False,
                "is_critical_flag": False,
            },
        ],
    }

    mock_gateway = _build_mock_gateway(
        recorded_calls,
        responses=[misread_payload, corrected_payload],
    )
    app.dependency_overrides[get_gemini_gateway] = lambda: mock_gateway

    photo_1 = _create_dummy_jpeg_bytes(280)
    photo_2 = _create_dummy_jpeg_bytes(295)

    # Initial intake
    resp_1 = client.post(
        "/api/v1/cascade/intake/lab-images",
        data={"cycle_id": cycle.cycle_id, "is_correction": "false"},
        files=[("files", ("misread.jpg", io.BytesIO(photo_1), "image/jpeg"))],
        headers=auth_headers,
    )
    assert resp_1.status_code == 200
    assert resp_1.json()["ledger_version"] == 1

    lab_service = LabRevisionConsiliumService(db=db_session)
    state_1 = lab_service.get_lab_ledger_state(cycle.cycle_id)
    obs_1_id = state_1.active_markers["alt"].observation_id
    assert state_1.active_markers["alt"].value == "245.0"

    # Correction intake with is_correction=true
    resp_2 = client.post(
        "/api/v1/cascade/intake/lab-images",
        data={"cycle_id": cycle.cycle_id, "is_correction": "true"},
        files=[("files", ("corrected.jpg", io.BytesIO(photo_2), "image/jpeg"))],
        headers=auth_headers,
    )
    assert resp_2.status_code == 200
    assert resp_2.json()["ledger_version"] == 2

    state_2 = lab_service.get_lab_ledger_state(cycle.cycle_id)
    assert state_2.ledger_version == 2
    assert len(state_2.observations_history) == 2

    # Prior observation must be superseded and deactivated
    obs_prior = next(o for o in state_2.observations_history if o.observation_id == obs_1_id)
    assert obs_prior.is_active is False
    assert obs_prior.superseded_by_observation_id is not None

    # New active observation must be CORRECTION
    obs_corr = state_2.active_markers["alt"]
    assert obs_corr.value == "24.5"
    assert obs_corr.event_type == LabEventType.CORRECTION
    assert obs_corr.supersedes_observation_id == obs_1_id
    assert obs_corr.is_active is True


# ==============================================================================
# 12. COMPATIBILITY WITH C-8 MARKER PRESERVATION RULES
# ==============================================================================

def test_compatibility_with_c8_marker_rules(
    client: TestClient, db_session: Session, auth_headers: dict[str, str]
):
    cycle_repo = MedicalCycleRepository(db_session)
    cycle = cycle_repo.create(cycle_id="c7-cycle-c8-compat", status="active")

    recorded_calls: List[Dict[str, Any]] = []

    c8_payload = {
        "document_metadata": {
            "sample_date": "2026-10-20",
            "laboratory_name": "Specialized Lab",
            "page_count": 1,
        },
        "markers": [
            # 1. Qualitative positive
            {
                "original_name": "Anti-HCV",
                "marker_key": "anti_hcv",
                "raw_value": "Negative",
                "numeric_value": None,
                "unit": None,
                "sample_date": "2026-10-20",
                "category": "infectious",
                "is_uncertain": False,
                "is_critical_flag": False,
            },
            # 2. Dimensionless INR
            {
                "original_name": "МНО / INR",
                "marker_key": "inr",
                "raw_value": "1.15",
                "numeric_value": 1.15,
                "unit": None,
                "sample_date": "2026-10-20",
                "category": "coagulation",
                "is_uncertain": False,
                "is_critical_flag": False,
            },
            # 3. Numeric with missing unit
            {
                "original_name": "Калий",
                "marker_key": "potassium",
                "raw_value": "4.2",
                "numeric_value": 4.2,
                "unit": None,
                "sample_date": "2026-10-20",
                "category": "electrolytes",
                "is_uncertain": True,
                "uncertainty_note": "Missing unit of measurement",
                "is_critical_flag": False,
            },
        ],
    }

    mock_gateway = _build_mock_gateway(
        recorded_calls,
        default_response=c8_payload,
    )
    app.dependency_overrides[get_gemini_gateway] = lambda: mock_gateway

    photo = _create_dummy_jpeg_bytes(450)

    resp = client.post(
        "/api/v1/cascade/intake/lab-images",
        data={"cycle_id": cycle.cycle_id, "is_correction": "false"},
        files=[("files", ("c8_panel.jpg", io.BytesIO(photo), "image/jpeg"))],
        headers=auth_headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["ledger_version"] == 1
    assert data["extracted_count"] == 3
    # All 3 are confirmed valid (Rule 1, Rule 2, Rule 3 under C-8)
    assert data["requires_verification_count"] == 0

    lab_service = LabRevisionConsiliumService(db=db_session)
    state = lab_service.get_lab_ledger_state(cycle.cycle_id)
    assert state.ledger_version == 1

    # Check anti_hcv
    hcv = state.active_markers["anti_hcv"]
    assert hcv.value == "Negative"
    assert hcv.numeric_value is None

    # Check INR
    inr = state.active_markers["inr"]
    assert inr.value == "1.15"
    assert inr.numeric_value == 1.15
    assert inr.unit is None

    # Check potassium
    k = state.active_markers["potassium"]
    assert k.value == "4.2"
    assert k.numeric_value == 4.2
    assert k.is_uncertain is True
    assert "Missing unit of measurement" in (k.uncertainty_note or "")


# ==============================================================================
# 13. REGRESSION TESTS: STUDY-LEVEL DEDUPLICATION & UNIT INHERITANCE FORBIDDEN
# ==============================================================================

def test_two_distinct_panels_on_same_date_with_partially_matching_values_preserved(db_session: Session):
    """
    Test Task 1: Deduplication operates on WHOLE studies, not marker-by-marker.
    Panel A on 2026-05-10: glucose=5.2 mmol/L, potassium=4.1 mmol/L.
    Panel B on 2026-05-10: glucose=5.2 mmol/L, potassium=4.6 mmol/L.
    Glucose in Panel B must NOT be dropped as a duplicate. Both glucose and potassium
    from Panel B must be preserved in history and active markers.
    """
    cycle_repo = MedicalCycleRepository(db_session)
    cycle = cycle_repo.create(cycle_id="c7-test-partial-match-panels", status="active")
    service = LabRevisionConsiliumService(db=db_session)

    # Initial study (Panel A)
    mutations_a = [
        LabMarkerMutationItem(
            marker_key="glucose",
            display_name="Glucose",
            event_type=LabEventType.INITIAL,
            value="5.2",
            unit="mmol/L",
            sample_date="2026-05-10",
        ),
        LabMarkerMutationItem(
            marker_key="potassium",
            display_name="Potassium",
            event_type=LabEventType.INITIAL,
            value="4.1",
            unit="mmol/L",
            sample_date="2026-05-10",
        ),
    ]
    state_a = service.upsert_laboratory_markers(cycle.cycle_id, mutations_a)
    assert state_a.ledger_version == 1
    assert len(state_a.observations_history) == 2
    assert state_a.active_markers["glucose"].value == "5.2"
    assert state_a.active_markers["potassium"].value == "4.1"

    # Subsequent study (Panel B on same date, glucose matches but potassium differs)
    mutations_b = [
        LabMarkerMutationItem(
            marker_key="glucose",
            display_name="Glucose",
            event_type=LabEventType.NEW_STUDY,
            value="5.2",
            unit="mmol/L",
            sample_date="2026-05-10",
        ),
        LabMarkerMutationItem(
            marker_key="potassium",
            display_name="Potassium",
            event_type=LabEventType.NEW_STUDY,
            value="4.6",
            unit="mmol/L",
            sample_date="2026-05-10",
        ),
    ]
    state_b = service.upsert_laboratory_markers(cycle.cycle_id, mutations_b)
    # Must bump ledger version because it's a distinct study
    assert state_b.ledger_version == 2
    # History must have 4 observations: 2 from Panel A and 2 from Panel B
    assert len(state_b.observations_history) == 4
    # Both markers in Panel B are recorded
    b_glucose_obs = [o for o in state_b.observations_history if o.marker_key == "glucose"]
    assert len(b_glucose_obs) == 2
    assert state_b.active_markers["potassium"].value == "4.6"
    assert state_b.active_markers["glucose"].value == "5.2"


def test_full_duplicate_multi_marker_panel_deduplicated_without_ledger_bump(db_session: Session):
    """
    Test Task 1: If ALL markers in the panel match an existing active study,
    the panel is skipped as a full duplicate and does not create duplicate entries or bump version.
    """
    cycle_repo = MedicalCycleRepository(db_session)
    cycle = cycle_repo.create(cycle_id="c7-test-full-dup-panel", status="active")
    service = LabRevisionConsiliumService(db=db_session)

    mutations_a = [
        LabMarkerMutationItem(
            marker_key="glucose",
            display_name="Glucose",
            event_type=LabEventType.INITIAL,
            value="5.2",
            unit="mmol/L",
            sample_date="2026-05-10",
        ),
        LabMarkerMutationItem(
            marker_key="potassium",
            display_name="Potassium",
            event_type=LabEventType.INITIAL,
            value="4.1",
            unit="mmol/L",
            sample_date="2026-05-10",
        ),
    ]
    state_a = service.upsert_laboratory_markers(cycle.cycle_id, mutations_a)
    assert state_a.ledger_version == 1
    assert len(state_a.observations_history) == 2

    # Duplicate study (same markers, same values, same units, same date)
    mutations_dup = [
        LabMarkerMutationItem(
            marker_key="glucose",
            display_name="Glucose",
            event_type=LabEventType.NEW_STUDY,
            value="5.2",
            unit="mmol/L",
            sample_date="2026-05-10",
        ),
        LabMarkerMutationItem(
            marker_key="potassium",
            display_name="Potassium",
            event_type=LabEventType.NEW_STUDY,
            value="4.1",
            unit="mmol/L",
            sample_date="2026-05-10",
        ),
    ]
    state_dup = service.upsert_laboratory_markers(cycle.cycle_id, mutations_dup)
    assert state_dup.ledger_version == 1
    assert len(state_dup.observations_history) == 2


def test_unit_inheritance_strictly_forbidden_for_new_study(db_session: Session):
    """
    Test Task 2: Strictly forbid inheriting units from prior_active for INITIAL / NEW_STUDY.
    If unit is None on a new study for a dimensional marker (glucose), unit remains None,
    is_uncertain becomes True, and uncertainty_note flags missing unit.
    """
    cycle_repo = MedicalCycleRepository(db_session)
    cycle = cycle_repo.create(cycle_id="c7-test-unit-inheritance-forbidden", status="active")
    service = LabRevisionConsiliumService(db=db_session)

    # Initial study with mmol/L
    mutations_v1 = [
        LabMarkerMutationItem(
            marker_key="glucose",
            display_name="Glucose",
            event_type=LabEventType.INITIAL,
            value="5.2",
            unit="mmol/L",
            sample_date="2026-05-10",
        )
    ]
    state_v1 = service.upsert_laboratory_markers(cycle.cycle_id, mutations_v1)
    assert state_v1.ledger_version == 1
    assert state_v1.active_markers["glucose"].unit == "mmol/L"
    assert state_v1.active_markers["glucose"].is_uncertain is False

    # New study with unit=None (e.g. from a foreign lab where unit was omitted)
    mutations_v2 = [
        LabMarkerMutationItem(
            marker_key="glucose",
            display_name="Glucose",
            event_type=LabEventType.NEW_STUDY,
            value="5.5",
            unit=None,
            sample_date="2026-06-01",
        )
    ]
    state_v2 = service.upsert_laboratory_markers(cycle.cycle_id, mutations_v2)
    assert state_v2.ledger_version == 2
    new_glucose = state_v2.active_markers["glucose"]
    # MUST NOT inherit "mmol/L" from prior_active
    assert new_glucose.unit is None
    assert new_glucose.value == "5.5"
    assert new_glucose.numeric_value == 5.5
    assert new_glucose.is_uncertain is True
    assert "Missing unit of measurement for numeric value on laboratory report" in (new_glucose.uncertainty_note or "")


def test_explicit_correction_omitted_unit_preserves_previous_unit(db_session: Session):
    """
    Test Task 3: When field 'unit' is omitted from the CORRECTION input,
    the previous observation's unit is preserved.
    """
    cycle_repo = MedicalCycleRepository(db_session)
    cycle = cycle_repo.create(cycle_id="c7-test-correction-omitted-unit", status="active")
    service = LabRevisionConsiliumService(db=db_session)

    mutations_v1 = [
        LabMarkerMutationItem(
            marker_key="glucose",
            display_name="Glucose",
            event_type=LabEventType.INITIAL,
            value="5.2",
            unit="mmol/L",
            sample_date="2026-05-10",
        )
    ]
    state_v1 = service.upsert_laboratory_markers(cycle.cycle_id, mutations_v1)
    prev_obs_id = state_v1.active_markers["glucose"].observation_id

    # Typo correction: value is corrected from 5.2 to 5.4, field 'unit' is omitted
    mutations_corr = [
        LabMarkerMutationItem(
            marker_key="glucose",
            display_name="Glucose",
            event_type=LabEventType.CORRECTION,
            value="5.4",
            sample_date="2026-05-10",
            supersedes_observation_id=prev_obs_id,
            correction_reason="Typo correction of OCR digit",
        )
    ]
    assert "unit" not in mutations_corr[0].model_fields_set

    state_v2 = service.upsert_laboratory_markers(cycle.cycle_id, mutations_corr)
    assert state_v2.ledger_version == 2
    corr_glucose = state_v2.active_markers["glucose"]
    assert corr_glucose.value == "5.4"
    assert corr_glucose.numeric_value == 5.4
    # Preserves mmol/L from previous observation
    assert corr_glucose.unit == "mmol/L"
    assert corr_glucose.is_uncertain is False
    assert corr_glucose.supersedes_observation_id == prev_obs_id


def test_explicit_correction_null_unit_resets_unit_and_flags_uncertainty(db_session: Session):
    """
    Test Task 3: When field 'unit' is explicitly passed as null (unit=None) in a CORRECTION,
    unit is reset to None. For dimensional numeric markers, is_uncertain is set to True.
    """
    cycle_repo = MedicalCycleRepository(db_session)
    cycle = cycle_repo.create(cycle_id="c7-test-correction-null-unit", status="active")
    service = LabRevisionConsiliumService(db=db_session)

    mutations_v1 = [
        LabMarkerMutationItem(
            marker_key="glucose",
            display_name="Glucose",
            event_type=LabEventType.INITIAL,
            value="5.2",
            unit="mmol/L",
            sample_date="2026-05-10",
        )
    ]
    state_v1 = service.upsert_laboratory_markers(cycle.cycle_id, mutations_v1)
    prev_obs_id = state_v1.active_markers["glucose"].observation_id

    # User explicitly passes unit=None to reset/wipe the unit
    mutations_corr = [
        LabMarkerMutationItem(
            marker_key="glucose",
            display_name="Glucose",
            event_type=LabEventType.CORRECTION,
            value="5.4",
            unit=None,
            sample_date="2026-05-10",
            supersedes_observation_id=prev_obs_id,
            correction_reason="Explicit unit wipe",
        )
    ]
    assert "unit" in mutations_corr[0].model_fields_set
    assert mutations_corr[0].unit is None

    state_v2 = service.upsert_laboratory_markers(cycle.cycle_id, mutations_corr)
    assert state_v2.ledger_version == 2
    corr_glucose = state_v2.active_markers["glucose"]
    assert corr_glucose.value == "5.4"
    assert corr_glucose.numeric_value == 5.4
    # Unit reset to None
    assert corr_glucose.unit is None
    # Flagged as uncertain because unit is missing on a dimensional numeric marker
    assert corr_glucose.is_uncertain is True
    assert "Missing unit of measurement for numeric value on laboratory report" in (corr_glucose.uncertainty_note or "")


def test_explicit_correction_dimensionless_null_unit_stays_certain(db_session: Session):
    """
    Test Task 3: For inherently dimensionless markers (e.g. INR), explicit null unit is valid
    and does not trigger missing unit uncertainty.
    """
    cycle_repo = MedicalCycleRepository(db_session)
    cycle = cycle_repo.create(cycle_id="c7-test-correction-inr-null-unit", status="active")
    service = LabRevisionConsiliumService(db=db_session)

    mutations_v1 = [
        LabMarkerMutationItem(
            marker_key="inr",
            display_name="INR",
            event_type=LabEventType.INITIAL,
            value="1.15",
            unit=None,
            sample_date="2026-05-10",
        )
    ]
    state_v1 = service.upsert_laboratory_markers(cycle.cycle_id, mutations_v1)
    prev_obs_id = state_v1.active_markers["inr"].observation_id

    mutations_corr = [
        LabMarkerMutationItem(
            marker_key="inr",
            display_name="INR",
            event_type=LabEventType.CORRECTION,
            value="1.20",
            unit=None,
            sample_date="2026-05-10",
            supersedes_observation_id=prev_obs_id,
            correction_reason="Corrected INR decimal",
        )
    ]
    state_v2 = service.upsert_laboratory_markers(cycle.cycle_id, mutations_corr)
    assert state_v2.ledger_version == 2
    corr_inr = state_v2.active_markers["inr"]
    assert corr_inr.value == "1.20"
    assert corr_inr.unit is None
    assert corr_inr.is_uncertain is False


def test_explicit_correction_string_unit_updates_unit(db_session: Session):
    """
    Test Task 3: When unit is explicitly passed as a string, it replaces the previous unit.
    """
    cycle_repo = MedicalCycleRepository(db_session)
    cycle = cycle_repo.create(cycle_id="c7-test-correction-string-unit", status="active")
    service = LabRevisionConsiliumService(db=db_session)

    mutations_v1 = [
        LabMarkerMutationItem(
            marker_key="glucose",
            display_name="Glucose",
            event_type=LabEventType.INITIAL,
            value="5.2",
            unit="mmol/L",
            sample_date="2026-05-10",
        )
    ]
    state_v1 = service.upsert_laboratory_markers(cycle.cycle_id, mutations_v1)
    prev_obs_id = state_v1.active_markers["glucose"].observation_id

    mutations_corr = [
        LabMarkerMutationItem(
            marker_key="glucose",
            display_name="Glucose",
            event_type=LabEventType.CORRECTION,
            value="94.0",
            unit="mg/dL",
            sample_date="2026-05-10",
            supersedes_observation_id=prev_obs_id,
            correction_reason="Unit converted from lab report",
        )
    ]
    state_v2 = service.upsert_laboratory_markers(cycle.cycle_id, mutations_corr)
    assert state_v2.ledger_version == 2
    corr_glucose = state_v2.active_markers["glucose"]
    assert corr_glucose.value == "94.0"
    assert corr_glucose.unit == "mg/dL"
    assert corr_glucose.is_uncertain is False


def test_study_c_not_deduplicated_against_combination_of_study_a_and_study_b(db_session: Session):
    """
    Test Task 1 (Whole-Study Identity):
    Study A: glucose=5.2 (date 2026-05-10)
    Study B: potassium=4.6 (date 2026-05-10)
    New Study C: glucose=5.2, potassium=4.6 (date 2026-05-10)

    Study C must NOT be considered a duplicate of the combination of Study A + Study B!
    Both markers in Study C are saved, ledger version becomes 3, and all observations are preserved.
    """
    cycle_repo = MedicalCycleRepository(db_session)
    cycle = cycle_repo.create(cycle_id="c7-test-study-c-not-dup-of-a-plus-b", status="active")
    service = LabRevisionConsiliumService(db=db_session)

    # 1. Study A
    mut_a = [
        LabMarkerMutationItem(
            marker_key="glucose",
            display_name="Glucose",
            event_type=LabEventType.INITIAL,
            value="5.2",
            unit="mmol/L",
            sample_date="2026-05-10",
        )
    ]
    state_1 = service.upsert_laboratory_markers(cycle.cycle_id, mut_a)
    assert state_1.ledger_version == 1
    assert len(state_1.observations_history) == 1

    # 2. Study B
    mut_b = [
        LabMarkerMutationItem(
            marker_key="potassium",
            display_name="Potassium",
            event_type=LabEventType.NEW_STUDY,
            value="4.6",
            unit="mmol/L",
            sample_date="2026-05-10",
        )
    ]
    state_2 = service.upsert_laboratory_markers(cycle.cycle_id, mut_b)
    assert state_2.ledger_version == 2
    assert len(state_2.observations_history) == 2

    # 3. New Study C: contains both glucose=5.2 and potassium=4.6 on the same date
    mut_c = [
        LabMarkerMutationItem(
            marker_key="glucose",
            display_name="Glucose",
            event_type=LabEventType.NEW_STUDY,
            value="5.2",
            unit="mmol/L",
            sample_date="2026-05-10",
        ),
        LabMarkerMutationItem(
            marker_key="potassium",
            display_name="Potassium",
            event_type=LabEventType.NEW_STUDY,
            value="4.6",
            unit="mmol/L",
            sample_date="2026-05-10",
        ),
    ]
    state_3 = service.upsert_laboratory_markers(cycle.cycle_id, mut_c)
    # MUST NOT be skipped as duplicate of A + B!
    assert state_3.ledger_version == 3
    # History must contain all 4 observations: 1 from A, 1 from B, 2 from C
    assert len(state_3.observations_history) == 4

    # 4. Repeating Study C MUST be deduplicated against Study C as a concrete registered study
    state_3_repeat = service.upsert_laboratory_markers(cycle.cycle_id, mut_c)
    assert state_3_repeat.ledger_version == 3
    assert len(state_3_repeat.observations_history) == 4


def test_decimal_exact_numeric_comparison_equivalence_and_differences(db_session: Session):
    """
    Test Task 2:
    - 5.20 and 5.2 are numerically equivalent via Decimal -> deduplicated.
    - 5.20001 and 5.2 differ -> distinct study, not deduplicated.
    - 5.2 mmol/L and 5.2 mg/dL differ in unit -> distinct study, not deduplicated.
    """
    # Direct helper verification
    assert are_lab_values_equivalent("5.20", 5.2, "mmol/L", "5.2", 5.2, "mmol/L") is True
    assert are_lab_values_equivalent("5,20", 5.2, "mmol/L", "5.2", 5.2, "mmol/L") is True
    assert are_lab_values_equivalent("5.20001", 5.20001, "mmol/L", "5.2", 5.2, "mmol/L") is False
    assert are_lab_values_equivalent("5.2", 5.2, "mmol/L", "5.2", 5.2, "mg/dL") is False
    assert are_lab_values_equivalent("Negative", None, None, "negative", None, None) is True
    assert are_lab_values_equivalent("Negative", None, None, "Positive", None, None) is False

    # Ledger integration verification
    cycle_repo = MedicalCycleRepository(db_session)
    cycle = cycle_repo.create(cycle_id="c7-test-decimal-comparison", status="active")
    service = LabRevisionConsiliumService(db=db_session)

    # Study 1 with 5.20 and 4.10
    mut_1 = [
        LabMarkerMutationItem(
            marker_key="glucose",
            display_name="Glucose",
            event_type=LabEventType.INITIAL,
            value="5.20",
            unit="mmol/L",
            sample_date="2026-05-10",
        ),
        LabMarkerMutationItem(
            marker_key="potassium",
            display_name="Potassium",
            event_type=LabEventType.INITIAL,
            value="4.10",
            unit="mmol/L",
            sample_date="2026-05-10",
        ),
    ]
    state_1 = service.upsert_laboratory_markers(cycle.cycle_id, mut_1)
    assert state_1.ledger_version == 1

    # Study 2 with equivalent 5.2 and 4.1 (must deduplicate)
    mut_2 = [
        LabMarkerMutationItem(
            marker_key="glucose",
            display_name="Glucose",
            event_type=LabEventType.NEW_STUDY,
            value="5.2",
            unit="mmol/L",
            sample_date="2026-05-10",
        ),
        LabMarkerMutationItem(
            marker_key="potassium",
            display_name="Potassium",
            event_type=LabEventType.NEW_STUDY,
            value="4.1",
            unit="mmol/L",
            sample_date="2026-05-10",
        ),
    ]
    state_2 = service.upsert_laboratory_markers(cycle.cycle_id, mut_2)
    assert state_2.ledger_version == 1
    assert len(state_2.observations_history) == 2

    # Study 3 with 5.20001 (differs from 5.2) -> must NOT deduplicate
    mut_3 = [
        LabMarkerMutationItem(
            marker_key="glucose",
            display_name="Glucose",
            event_type=LabEventType.NEW_STUDY,
            value="5.20001",
            unit="mmol/L",
            sample_date="2026-05-10",
        ),
        LabMarkerMutationItem(
            marker_key="potassium",
            display_name="Potassium",
            event_type=LabEventType.NEW_STUDY,
            value="4.1",
            unit="mmol/L",
            sample_date="2026-05-10",
        ),
    ]
    state_3 = service.upsert_laboratory_markers(cycle.cycle_id, mut_3)
    assert state_3.ledger_version == 2
    assert len(state_3.observations_history) == 4


def test_mutation_serialization_deserialization_preserves_omitted_vs_explicit_null():
    """
    Test Task 3: Verifies that model_dump(exclude_unset=True) and model_validate
    preserve the distinction between omitted unit and explicit null unit across
    network/serialization boundaries.
    """
    # 1. Omitted unit
    m_omitted = LabMarkerMutationItem(
        marker_key="glucose",
        value="5.4",
        event_type=LabEventType.CORRECTION,
    )
    assert "unit" not in m_omitted.model_fields_set
    d_omitted = m_omitted.model_dump(exclude_unset=True)
    assert "unit" not in d_omitted
    re_omitted = LabMarkerMutationItem.model_validate(d_omitted)
    assert "unit" not in re_omitted.model_fields_set

    # 2. Explicit null unit
    m_null = LabMarkerMutationItem(
        marker_key="glucose",
        value="5.4",
        unit=None,
        event_type=LabEventType.CORRECTION,
    )
    assert "unit" in m_null.model_fields_set
    assert m_null.unit is None
    d_null = m_null.model_dump(exclude_unset=True)
    assert "unit" in d_null
    assert d_null["unit"] is None
    re_null = LabMarkerMutationItem.model_validate(d_null)
    assert "unit" in re_null.model_fields_set
    assert re_null.unit is None

    # 3. Explicit string unit
    m_str = LabMarkerMutationItem(
        marker_key="glucose",
        value="5.4",
        unit="mmol/L",
        event_type=LabEventType.CORRECTION,
    )
    assert "unit" in m_str.model_fields_set
    d_str = m_str.model_dump(exclude_unset=True)
    assert d_str["unit"] == "mmol/L"
    re_str = LabMarkerMutationItem.model_validate(d_str)
    assert "unit" in re_str.model_fields_set
    assert re_str.unit == "mmol/L"
