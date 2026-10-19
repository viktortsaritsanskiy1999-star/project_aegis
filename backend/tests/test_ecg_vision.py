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
    EcgRecordingQuality,
    EcgRecordingType,
    EcgVisionIntakeClientResponse,
    VisionEcgModelOutput,
)
from app.schemas.privacy import PrivacyLevel
from app.services.ecg_vision_service import (
    EcgVisionService,
    EcgVisionValidationError,
    MAX_ECG_IMAGE_BYTES,
)
from app.services.gemini_gateway import GeminiGateway, GeminiGatewayError, GeminiModelRole
from app.services.primary_consilium_service import (
    PREPARATORY_ECG_REPORT_TYPE,
    PREPARATORY_ANAMNESIS_REPORT_TYPE,
    PrimaryConsiliumService,
)
from tests.test_primary_consilium import (
    _gemini_envelope,
    _sample_anamnesis_output,
    _build_consilium_mock_gateway,
)


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


def _sample_smartwatch_ecg_output(
    heart_rate: int = 72,
    electrical_axis_attempt: Optional[str] = "normal",  # Should be forced to None by validator!
) -> Dict[str, Any]:
    return {
        "recording_type": EcgRecordingType.SINGLE_LEAD_SMARTWATCH.value,
        "recording_quality": EcgRecordingQuality.GOOD.value,
        "lead_name": "Lead I (Samsung Galaxy Watch)",
        "heart_rate_bpm": heart_rate,
        "rhythm_type": "sinus",
        "rhythm_regularity": "regular",
        "pr_interval_ms": 156.0,
        "qrs_duration_ms": 86.0,
        "qt_interval_ms": 382.0,
        "qtc_interval_ms": 418.0,
        "electrical_axis": electrical_axis_attempt,
        "st_segment_findings": "isoelectric in Lead I",
        "t_wave_findings": "upright normal morphology in Lead I",
        "limitations": [
            "Single-lead smartwatch recording: cannot detect transmural ischemia or localize infarcts.",
            "Cannot substitute for comprehensive 12-lead diagnostic ECG.",
        ],
        "uncertainties": ["Minor high-frequency skin contact artifact at initiation."],
        "ecg_summary": (
            "Smartwatch Lead I recording: Regular sinus rhythm, ventricular rate 72 bpm. "
            "Normal QRS width (86 ms), PR interval (156 ms), QTc (418 ms). "
            "Single-lead recording: electrical axis and multilead repolarization cannot be assessed."
        ),
        "potential_urgent_warning": None,
    }


def _sample_12_lead_ecg_output() -> Dict[str, Any]:
    return {
        "recording_type": EcgRecordingType.STANDARD_12_LEAD.value,
        "recording_quality": EcgRecordingQuality.GOOD.value,
        "lead_name": "Standard 12-lead (I, II, III, aVR, aVL, aVF, V1-V6)",
        "heart_rate_bpm": 68,
        "rhythm_type": "sinus",
        "rhythm_regularity": "regular",
        "pr_interval_ms": 164.0,
        "qrs_duration_ms": 90.0,
        "qt_interval_ms": 395.0,
        "qtc_interval_ms": 420.0,
        "electrical_axis": "normal (~45 degrees)",
        "st_segment_findings": "isoelectric across all limb and precordial leads",
        "t_wave_findings": "normal positive T waves in leads I, II, V3-V6",
        "limitations": [
            "Paper tracing digitised from photograph; exact millimetric calibration verified against 10mm/mV scale."
        ],
        "uncertainties": [],
        "ecg_summary": (
            "Standard 12-lead ECG: Normal sinus rhythm at 68 bpm. Normal frontal electrical axis. "
            "Intact AV conduction (PR 164 ms), narrow QRS (90 ms), normal repolarization (QTc 420 ms). "
            "No acute ST deviations or pathological Q waves detected."
        ),
        "potential_urgent_warning": None,
    }


def _build_ecg_mock_gateway(
    recorded_calls: List[Dict[str, Any]],
    ecg_override: Optional[Dict[str, Any]] = None,
    fail_with_status: Optional[int] = None,
) -> GeminiGateway:
    """Mock GeminiGateway for Vision ECG requests."""
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

        payload = ecg_override or _sample_smartwatch_ecg_output()
        return httpx.Response(
            status_code=200,
            json=_gemini_envelope(payload),
        )

    transport = httpx.MockTransport(handler)
    return GeminiGateway(api_key="test-ecg-key", transport=transport)


# ==============================================================================
# 1. Single-Lead Smartwatch ECG Extraction & Axis Restriction
# ==============================================================================

def test_single_lead_smartwatch_ecg_extraction(client: TestClient, db_session: Session, auth_headers: dict[str, str]):
    cycle_repo = MedicalCycleRepository(db_session)
    cycle_id = "test-cycle-ecg-single-01"
    cycle = cycle_repo.create(cycle_id=cycle_id, status="active")

    recorded_calls: List[Dict[str, Any]] = []
    # Note: raw mock tries to output electrical_axis="normal", validator MUST nullify it for single lead!
    sample_output = _sample_smartwatch_ecg_output(heart_rate=76, electrical_axis_attempt="normal")
    mock_gateway = _build_ecg_mock_gateway(recorded_calls, ecg_override=sample_output)
    app.dependency_overrides[get_gemini_gateway] = lambda: mock_gateway

    jpeg_data = _create_dummy_jpeg_bytes(512)
    files = {"file": ("smartwatch_lead1.jpg", io.BytesIO(jpeg_data), "image/jpeg")}
    data = {"cycle_id": cycle.cycle_id}

    resp = client.post("/api/v1/cascade/intake/ecg-image", data=data, files=files, headers=auth_headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["cycle_id"] == cycle_id
    assert body["status"] == "processed"
    assert body["intake_version"] == 1
    assert body["recording_type"] == "single_lead_smartwatch"
    assert body["recording_quality"] == "good"
    assert body["primary_consilium_concluded"] is False
    assert "privacy_notice" in body

    # Strictly verify NO intermediate medical inferences/metrics are in client response
    for forbidden in ["heart_rate_bpm", "rhythm_regularity", "has_measurable_intervals", "has_urgent_warning", "urgent_warning", "limitations"]:
        assert forbidden not in body, f"{forbidden} leaked into client response!"

    # Verify DB record: all clinical metrics preserved in server_only storage
    report_repo = ModelReportRepository(db_session)
    reports = report_repo.list_server_only(cycle_id, PREPARATORY_ECG_REPORT_TYPE)
    assert len(reports) == 1
    content = reports[0].content
    assert content["agent_role"] == AegisAgentRole.VISION_ECG.value
    assert content["heart_rate_bpm"] == 76
    assert content["rhythm_regularity"] == "regular"
    assert content["electrical_axis"] is None  # FORBIDDEN ON SINGLE LEAD!
    assert any("single-lead" in lim.lower() for lim in content["limitations"])
    assert any("axis" in lim.lower() for lim in content["limitations"])
    assert "Single-lead recording: electrical axis and multilead repolarization cannot be assessed." in content["ecg_summary"]


# ==============================================================================
# 2. Standard 12-Lead Multichannel ECG Extraction
# ==============================================================================

def test_standard_12_lead_multichannel_ecg_extraction(client: TestClient, db_session: Session, auth_headers: dict[str, str]):
    cycle_repo = MedicalCycleRepository(db_session)
    cycle_id = "test-cycle-ecg-12lead-01"
    cycle = cycle_repo.create(cycle_id=cycle_id, status="active")

    recorded_calls: List[Dict[str, Any]] = []
    sample_output = _sample_12_lead_ecg_output()
    mock_gateway = _build_ecg_mock_gateway(recorded_calls, ecg_override=sample_output)
    app.dependency_overrides[get_gemini_gateway] = lambda: mock_gateway

    png_data = _create_dummy_png_bytes(1024)
    files = {"file": ("ecg_12lead.png", io.BytesIO(png_data), "image/png")}
    data = {"cycle_id": cycle.cycle_id, "record_date": "2026-10-18"}

    resp = client.post("/api/v1/cascade/intake/ecg-image", data=data, files=files, headers=auth_headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["recording_type"] == "standard_12_lead"
    assert body["recording_quality"] == "good"
    assert body["intake_version"] == 1
    for forbidden in ["heart_rate_bpm", "rhythm_regularity", "has_measurable_intervals", "has_urgent_warning", "urgent_warning", "limitations"]:
        assert forbidden not in body

    # 12-lead allows electrical axis measurement in internal server_only report
    report_repo = ModelReportRepository(db_session)
    reports = report_repo.list_server_only(cycle_id, PREPARATORY_ECG_REPORT_TYPE)
    assert len(reports) == 1
    content = reports[0].content
    assert content["heart_rate_bpm"] == 68
    assert content["electrical_axis"] == "normal (~45 degrees)"
    assert content["intervals"]["pr_interval_ms"] == 164.0
    assert content["intervals"]["qrs_duration_ms"] == 90.0
    assert content["intervals"]["qtc_interval_ms"] == 420.0
    assert content["record_date"] == "2026-10-18"


# ==============================================================================
# 3. Missing or Unmeasurable Intervals (e.g. Atrial Fibrillation)
# ==============================================================================

def test_missing_or_unmeasurable_intervals(client: TestClient, db_session: Session, auth_headers: dict[str, str]):
    cycle_repo = MedicalCycleRepository(db_session)
    cycle_id = "test-cycle-ecg-afib-01"
    cycle = cycle_repo.create(cycle_id=cycle_id, status="active")

    afib_output = {
        "recording_type": "single_lead_smartwatch",
        "recording_quality": "moderate",
        "lead_name": "Lead I",
        "heart_rate_bpm": 112,
        "rhythm_type": "atrial_fibrillation",
        "rhythm_regularity": "irregular",
        "pr_interval_ms": None,  # P wave absent in AF!
        "qrs_duration_ms": 88.0,
        "qt_interval_ms": None,  # Variable RR makes fixed QT impossible
        "qtc_interval_ms": None,
        "electrical_axis": None,
        "st_segment_findings": "mild non-specific depression in Lead I",
        "t_wave_findings": "low amplitude T wave",
        "limitations": [
            "Atrial fibrillation with irregular RR intervals precludes fixed PR/QT calculation.",
            "Single-lead smartwatch strip cannot assess chamber dimensions or full repolarization."
        ],
        "uncertainties": ["Fine f-waves present on baseline."],
        "ecg_summary": (
            "Irregularly irregular rhythm with absent P waves and fibrillatory baseline, "
            "consistent with Atrial Fibrillation with rapid ventricular response (112 bpm). "
            "Narrow QRS (88 ms). PR interval not measurable."
        ),
        "potential_urgent_warning": "Suspected atrial fibrillation with rapid ventricular response (> 100 bpm).",
    }

    recorded_calls: List[Dict[str, Any]] = []
    mock_gateway = _build_ecg_mock_gateway(recorded_calls, ecg_override=afib_output)
    app.dependency_overrides[get_gemini_gateway] = lambda: mock_gateway

    jpeg_data = _create_dummy_jpeg_bytes(512)
    files = {"file": ("afib_strip.jpg", io.BytesIO(jpeg_data), "image/jpeg")}

    resp = client.post("/api/v1/cascade/intake/ecg-image", data={"cycle_id": cycle.cycle_id}, files=files, headers=auth_headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["status"] == "processed"
    assert body["intake_version"] == 1
    assert body["recording_type"] == "single_lead_smartwatch"
    assert body["recording_quality"] == "moderate"
    for forbidden in ["heart_rate_bpm", "rhythm_regularity", "has_measurable_intervals", "has_urgent_warning", "urgent_warning", "limitations"]:
        assert forbidden not in body

    # In DB: all clinical details, irregular rhythm, and warnings are stored for consilium
    report_repo = ModelReportRepository(db_session)
    report = report_repo.list_server_only(cycle_id, PREPARATORY_ECG_REPORT_TYPE)[0]
    assert report.content["rhythm_regularity"] == "irregular"
    assert report.content["heart_rate_bpm"] == 112
    assert report.content["potential_urgent_warning"] is not None
    assert "atrial fibrillation" in report.content["potential_urgent_warning"].lower()
    assert report.content["intervals"]["pr_interval_ms"] is None
    assert report.content["intervals"]["qtc_interval_ms"] is None


# ==============================================================================
# 4. Low Image Quality and Unreadable Tracing
# ==============================================================================

def test_low_image_quality_and_unreadable_ecg(client: TestClient, db_session: Session, auth_headers: dict[str, str]):
    cycle_repo = MedicalCycleRepository(db_session)
    cycle_id = "test-cycle-ecg-unreadable-01"
    cycle = cycle_repo.create(cycle_id=cycle_id, status="active")

    # The model raw output attempts to hallucinate intervals on an unreadable tracing
    unreadable_raw = {
        "recording_type": "unknown",
        "recording_quality": "unreadable",
        "lead_name": None,
        "heart_rate_bpm": 80,  # Hallucinated!
        "rhythm_type": "unclear",
        "rhythm_regularity": "unknown",
        "pr_interval_ms": 160.0,  # Hallucinated!
        "qrs_duration_ms": 90.0,   # Hallucinated!
        "qt_interval_ms": None,
        "qtc_interval_ms": None,
        "electrical_axis": "normal",  # Hallucinated!
        "st_segment_findings": "unclear",
        "t_wave_findings": "unclear",
        "limitations": ["Severe motion and line detachment."],
        "uncertainties": [],
        "ecg_summary": "ECG tracing is corrupted by severe motion artifacts and noise. Reliable metric extraction is not possible.",
        "potential_urgent_warning": None,
    }

    recorded_calls: List[Dict[str, Any]] = []
    mock_gateway = _build_ecg_mock_gateway(recorded_calls, ecg_override=unreadable_raw)
    app.dependency_overrides[get_gemini_gateway] = lambda: mock_gateway

    jpeg_data = _create_dummy_jpeg_bytes(512)
    files = {"file": ("blurred_ecg.jpg", io.BytesIO(jpeg_data), "image/jpeg")}

    resp = client.post("/api/v1/cascade/intake/ecg-image", data={"cycle_id": cycle.cycle_id}, files=files, headers=auth_headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["recording_quality"] == "unreadable"
    assert body["status"] == "processed"
    assert body["intake_version"] == 1
    for forbidden in ["heart_rate_bpm", "rhythm_regularity", "has_measurable_intervals", "has_urgent_warning", "urgent_warning", "limitations"]:
        assert forbidden not in body

    # Clinical safety model validator MUST have stripped hallucinated intervals in server_only storage
    report_repo = ModelReportRepository(db_session)
    report = report_repo.list_server_only(cycle_id, PREPARATORY_ECG_REPORT_TYPE)[0]
    assert report.content["heart_rate_bpm"] is None
    assert report.content["intervals"]["pr_interval_ms"] is None
    assert report.content["intervals"]["qrs_duration_ms"] is None
    assert report.content["electrical_axis"] is None
    assert any("unreadable" in u.lower() for u in report.content["uncertainties"])


# ==============================================================================
# 5. Invalid File Format, Magic Bytes Tampering & Size Limits
# ==============================================================================

def test_invalid_file_format_and_tampered_magic_bytes_and_size_limits(client: TestClient, db_session: Session, auth_headers: dict[str, str]):
    cycle_repo = MedicalCycleRepository(db_session)
    cycle_id = "test-cycle-ecg-tamper-01"
    cycle = cycle_repo.create(cycle_id=cycle_id, status="active")

    # Case A: Plain text disguised as .jpg
    fake_jpg = io.BytesIO(b"Plain text content disguised as a JPEG file.")
    resp1 = client.post(
        "/api/v1/cascade/intake/ecg-image",
        data={"cycle_id": cycle.cycle_id},
        files={"file": ("fake.jpg", fake_jpg, "image/jpeg")},
        headers=auth_headers,
    )
    assert resp1.status_code == 400
    assert "not a supported valid image format" in resp1.json()["detail"].lower()

    # Case B: Empty file (0 bytes)
    empty_file = io.BytesIO(b"")
    resp2 = client.post(
        "/api/v1/cascade/intake/ecg-image",
        data={"cycle_id": cycle.cycle_id},
        files={"file": ("empty.png", empty_file, "image/png")},
        headers=auth_headers,
    )
    assert resp2.status_code == 400
    assert "empty" in resp2.json()["detail"].lower()

    # Case C: Exceeding 10 MB limit
    oversized = io.BytesIO(_create_dummy_jpeg_bytes(10 * 1024 * 1024 + 1024))
    resp3 = client.post(
        "/api/v1/cascade/intake/ecg-image",
        data={"cycle_id": cycle.cycle_id},
        files={"file": ("huge.jpg", oversized, "image/jpeg")},
        headers=auth_headers,
    )
    assert resp3.status_code == 400
    assert "exceeds maximum allowed size" in resp3.json()["detail"].lower()

    # Case D: Valid WEBP and HEIC formats are accepted
    recorded_calls: List[Dict[str, Any]] = []
    mock_gateway = _build_ecg_mock_gateway(recorded_calls)
    app.dependency_overrides[get_gemini_gateway] = lambda: mock_gateway

    webp_file = io.BytesIO(_create_dummy_webp_bytes(256))
    resp_webp = client.post(
        "/api/v1/cascade/intake/ecg-image",
        data={"cycle_id": cycle.cycle_id},
        files={"file": ("ecg.webp", webp_file, "image/webp")},
        headers=auth_headers,
    )
    assert resp_webp.status_code == 200

    heic_file = io.BytesIO(_create_dummy_heic_bytes(256))
    resp_heic = client.post(
        "/api/v1/cascade/intake/ecg-image",
        data={"cycle_id": cycle.cycle_id},
        files={"file": ("ecg.heic", heic_file, "image/heic")},
        headers=auth_headers,
    )
    assert resp_heic.status_code == 200


# ==============================================================================
# 6. Unauthorized Request Rejected
# ==============================================================================

def test_unauthorized_ecg_request_rejected(client: TestClient, db_session: Session):
    cycle_repo = MedicalCycleRepository(db_session)
    cycle = cycle_repo.create(cycle_id="cycle-unauth-ecg", status="active")

    jpeg_data = _create_dummy_jpeg_bytes(128)
    files = {"file": ("scan.jpg", io.BytesIO(jpeg_data), "image/jpeg")}

    # Missing Authorization header
    resp = client.post("/api/v1/cascade/intake/ecg-image", data={"cycle_id": cycle.cycle_id}, files=files)
    assert resp.status_code == 401

    # Invalid token
    resp_bad = client.post(
        "/api/v1/cascade/intake/ecg-image",
        data={"cycle_id": cycle.cycle_id},
        files=files,
        headers={"Authorization": "Bearer invalid_secret_token"},
    )
    assert resp_bad.status_code == 401


# ==============================================================================
# 7. Unknown Medical Cycle Returns 404
# ==============================================================================

def test_unknown_medical_cycle_returns_404(client: TestClient, auth_headers: dict[str, str]):
    jpeg_data = _create_dummy_jpeg_bytes(128)
    files = {"file": ("scan.jpg", io.BytesIO(jpeg_data), "image/jpeg")}

    resp = client.post(
        "/api/v1/cascade/intake/ecg-image",
        data={"cycle_id": "non-existent-cycle-999"},
        files=files,
        headers=auth_headers,
    )
    assert resp.status_code == 404
    assert "not found" in resp.json()["detail"].lower()


# ==============================================================================
# 8. Gemini Gateway Failure Returns 502 and Rolls Back
# ==============================================================================

def test_gemini_gateway_failure_returns_502_and_rolls_back(client: TestClient, db_session: Session, auth_headers: dict[str, str]):
    cycle_repo = MedicalCycleRepository(db_session)
    cycle_id = "test-cycle-ecg-fail-01"
    cycle = cycle_repo.create(cycle_id=cycle_id, status="active")

    recorded_calls: List[Dict[str, Any]] = []
    mock_gateway = _build_ecg_mock_gateway(recorded_calls, fail_with_status=503)
    app.dependency_overrides[get_gemini_gateway] = lambda: mock_gateway

    jpeg_data = _create_dummy_jpeg_bytes(512)
    files = {"file": ("ecg.jpg", io.BytesIO(jpeg_data), "image/jpeg")}

    resp = client.post("/api/v1/cascade/intake/ecg-image", data={"cycle_id": cycle.cycle_id}, files=files, headers=auth_headers)
    assert resp.status_code == 502
    assert "extraction failed" in resp.json()["detail"].lower()

    # Zero reports created in database due to rollback
    report_repo = ModelReportRepository(db_session)
    assert report_repo.list_server_only(cycle_id, PREPARATORY_ECG_REPORT_TYPE) == []


# ==============================================================================
# 9. Idempotent Duplicate ECG Upload (Same Image)
# ==============================================================================

def test_idempotent_duplicate_ecg_upload_same_image(client: TestClient, db_session: Session, auth_headers: dict[str, str]):
    cycle_repo = MedicalCycleRepository(db_session)
    cycle_id = "test-cycle-ecg-idempotent-01"
    cycle = cycle_repo.create(cycle_id=cycle_id, status="active")

    recorded_calls: List[Dict[str, Any]] = []
    sample_output = _sample_smartwatch_ecg_output(heart_rate=70)
    mock_gateway = _build_ecg_mock_gateway(recorded_calls, ecg_override=sample_output)
    app.dependency_overrides[get_gemini_gateway] = lambda: mock_gateway

    jpeg_bytes = _create_dummy_jpeg_bytes(512)

    # 1. First upload
    resp1 = client.post(
        "/api/v1/cascade/intake/ecg-image",
        data={"cycle_id": cycle.cycle_id},
        files={"file": ("ecg.jpg", io.BytesIO(jpeg_bytes), "image/jpeg")},
        headers=auth_headers,
    )
    assert resp1.status_code == 200
    assert resp1.json()["intake_version"] == 1
    assert len(recorded_calls) == 1

    report_repo = ModelReportRepository(db_session)
    assert len(report_repo.list_versions(cycle_id, PREPARATORY_ECG_REPORT_TYPE)) == 1

    # 2. Second upload with identical bytes
    resp2 = client.post(
        "/api/v1/cascade/intake/ecg-image",
        data={"cycle_id": cycle.cycle_id},
        files={"file": ("ecg_copy.jpg", io.BytesIO(jpeg_bytes), "image/jpeg")},
        headers=auth_headers,
    )
    assert resp2.status_code == 200
    assert resp2.json()["intake_version"] == 1  # Version did NOT increment!
    # Gateway was NOT invoked again!
    assert len(recorded_calls) == 1
    # No duplicate DB row created:
    assert len(report_repo.list_versions(cycle_id, PREPARATORY_ECG_REPORT_TYPE)) == 1


# ==============================================================================
# 10. New ECG Record Preserves History as Version 2
# ==============================================================================

def test_new_ecg_record_preserves_history_as_version_2(client: TestClient, db_session: Session, auth_headers: dict[str, str]):
    cycle_repo = MedicalCycleRepository(db_session)
    cycle_id = "test-cycle-ecg-multi-01"
    cycle = cycle_repo.create(cycle_id=cycle_id, status="active")

    recorded_calls: List[Dict[str, Any]] = []
    output_1 = _sample_smartwatch_ecg_output(heart_rate=72)
    output_2 = _sample_smartwatch_ecg_output(heart_rate=84)
    output_2["ecg_summary"] = "Follow-up recording: Sinus tachycardia at 84 bpm post mild exertion."

    mock_gateway_1 = _build_ecg_mock_gateway(recorded_calls, ecg_override=output_1)
    app.dependency_overrides[get_gemini_gateway] = lambda: mock_gateway_1

    # Upload 1: image A
    jpeg_bytes_a = _create_dummy_jpeg_bytes(512)
    resp1 = client.post(
        "/api/v1/cascade/intake/ecg-image",
        data={"cycle_id": cycle.cycle_id, "record_date": "2026-10-10"},
        files={"file": ("ecg_a.jpg", io.BytesIO(jpeg_bytes_a), "image/jpeg")},
        headers=auth_headers,
    )
    assert resp1.status_code == 200
    assert resp1.json()["intake_version"] == 1
    assert "heart_rate_bpm" not in resp1.json()

    # Upload 2: image B (different bytes, different date)
    jpeg_bytes_b = _create_dummy_jpeg_bytes(600)  # Different size -> different hash
    mock_gateway_2 = _build_ecg_mock_gateway(recorded_calls, ecg_override=output_2)
    app.dependency_overrides[get_gemini_gateway] = lambda: mock_gateway_2

    resp2 = client.post(
        "/api/v1/cascade/intake/ecg-image",
        data={"cycle_id": cycle.cycle_id, "record_date": "2026-10-15"},
        files={"file": ("ecg_b.jpg", io.BytesIO(jpeg_bytes_b), "image/jpeg")},
        headers=auth_headers,
    )
    assert resp2.status_code == 200
    assert resp2.json()["intake_version"] == 2
    assert "heart_rate_bpm" not in resp2.json()

    # Both versions are preserved in DB history
    report_repo = ModelReportRepository(db_session)
    versions = report_repo.list_server_only(cycle_id, PREPARATORY_ECG_REPORT_TYPE)
    assert len(versions) == 2
    # Latest version (v2) has HR 84
    assert versions[0].version == 2
    assert versions[0].content["heart_rate_bpm"] == 84
    assert versions[0].content["record_date"] == "2026-10-15"
    # Prior version (v1) has HR 72
    assert versions[1].version == 1
    assert versions[1].content["heart_rate_bpm"] == 72
    assert versions[1].content["record_date"] == "2026-10-10"


# ==============================================================================
# 11. ECG Report Server-Only Privacy Isolation
# ==============================================================================

def test_ecg_report_server_only_privacy_isolation(client: TestClient, db_session: Session, auth_headers: dict[str, str]):
    cycle_repo = MedicalCycleRepository(db_session)
    cycle_id = "test-cycle-ecg-privacy-01"
    cycle = cycle_repo.create(cycle_id=cycle_id, status="active")

    recorded_calls: List[Dict[str, Any]] = []
    mock_gateway = _build_ecg_mock_gateway(recorded_calls)
    app.dependency_overrides[get_gemini_gateway] = lambda: mock_gateway

    jpeg_bytes = _create_dummy_jpeg_bytes(512)
    resp = client.post(
        "/api/v1/cascade/intake/ecg-image",
        data={"cycle_id": cycle.cycle_id},
        files={"file": ("ecg.jpg", io.BytesIO(jpeg_bytes), "image/jpeg")},
        headers=auth_headers,
    )
    assert resp.status_code == 200

    report_repo = ModelReportRepository(db_session)
    # Excluded from client-facing reports table
    assert report_repo.list_client_facing(cycle_id) == []

    # Excluded from GET /api/v1/reports/client
    reports_resp = client.get(f"/api/v1/reports/client?cycle_id={cycle_id}", headers=auth_headers)
    assert reports_resp.status_code == 200
    client_reports = reports_resp.json()
    assert not any(r["report_type"] == PREPARATORY_ECG_REPORT_TYPE for r in client_reports)


# ==============================================================================
# 12. Delivery of Result to Cardiovascular Specialist, Flash & Pro
# ==============================================================================

@pytest.mark.anyio
async def test_ecg_summary_delivery_to_cardiovascular_specialist_flash_and_pro(db_session: Session):
    cycle_repo = MedicalCycleRepository(db_session)
    cycle_id = "test-cycle-ecg-consilium-pipeline"
    cycle = cycle_repo.create(cycle_id=cycle_id, status="active")

    report_repo = ModelReportRepository(db_session)

    # 1. Seed preparatory anamnesis (M1)
    m1_content = _sample_anamnesis_output()
    m1_content["agent_role"] = AegisAgentRole.ANAMNESIS_M1.value
    report_repo.create_next_version(
        cycle_id=cycle_id,
        model="gemini-3.6-flash",
        report_type=PREPARATORY_ANAMNESIS_REPORT_TYPE,
        privacy_level=PrivacyLevel.SERVER_ONLY,
        content=m1_content,
    )

    # 2. Process ECG via EcgVisionService
    sample_ecg = _sample_smartwatch_ecg_output(heart_rate=78)
    ecg_calls: List[Dict[str, Any]] = []
    ecg_gateway = _build_ecg_mock_gateway(ecg_calls, ecg_override=sample_ecg)
    ecg_service = EcgVisionService(db=db_session, gateway=ecg_gateway)

    fake_file = type("FakeUploadFile", (), {
        "filename": "smartwatch_strip.jpg",
        "read": lambda self: asyncio.sleep(0, result=_create_dummy_jpeg_bytes(512)),
    })()
    await ecg_service.process_ecg_image(cycle_id=cycle_id, file=fake_file)  # type: ignore

    # Verify preparatory_vision_ecg was created
    ecg_reports = report_repo.list_server_only(cycle_id, PREPARATORY_ECG_REPORT_TYPE)
    assert len(ecg_reports) == 1
    assert "Smartwatch Lead I recording" in ecg_reports[0].content["ecg_summary"]

    # 3. Run Primary Consilium and record all model invocations
    consilium_calls: List[Dict[str, Any]] = []
    consilium_gateway = _build_consilium_mock_gateway(consilium_calls)
    primary_service = PrimaryConsiliumService(db=db_session, gateway=consilium_gateway)

    primary_resp = await primary_service.run_primary_consilium(cycle_id=cycle_id)
    assert primary_resp.cycle_id == cycle_id

    # 4. Verify CVS Organ Specialist (Flash-Lite) received the ECG summary
    assert len(consilium_calls) == 10
    cvs_prompt = consilium_calls[0]["prompt"]
    assert "Smartwatch Lead I recording" in cvs_prompt

    # 5. Verify Deputy Flash received vitals_stream with ecg_summary
    flash_prompt = consilium_calls[8]["prompt"]
    assert "Smartwatch Lead I recording" in flash_prompt


# ==============================================================================
# 13. Multimodal GeminiGateway Inline Images Payload
# ==============================================================================

def test_multimodal_gemini_gateway_inline_images_payload(client: TestClient, db_session: Session, auth_headers: dict[str, str]):
    cycle_repo = MedicalCycleRepository(db_session)
    cycle_id = "test-cycle-ecg-gateway-payload"
    cycle = cycle_repo.create(cycle_id=cycle_id, status="active")

    recorded_calls: List[Dict[str, Any]] = []
    mock_gateway = _build_ecg_mock_gateway(recorded_calls)
    app.dependency_overrides[get_gemini_gateway] = lambda: mock_gateway

    raw_bytes = _create_dummy_png_bytes(512)
    files = {"file": ("ecg.png", io.BytesIO(raw_bytes), "image/png")}

    resp = client.post("/api/v1/cascade/intake/ecg-image", data={"cycle_id": cycle.cycle_id}, files=files, headers=auth_headers)
    assert resp.status_code == 200

    assert len(recorded_calls) == 1
    req_body = recorded_calls[0]["body"]
    contents = req_body.get("contents", [])
    assert len(contents) >= 1
    parts = contents[0].get("parts", [])

    # Text prompt part
    text_part = next((p for p in parts if "text" in p), None)
    assert text_part is not None
    assert "Electrocardiogram Vision Digitization & Recognition Engine" in text_part["text"]

    # Inline image part
    image_part = next((p for p in parts if "inlineData" in p or "inline_data" in p), None)
    assert image_part is not None
    inline_obj = image_part.get("inlineData") or image_part.get("inline_data")
    assert inline_obj["mimeType"] == "image/png" or inline_obj.get("mime_type") == "image/png"
    encoded_data = inline_obj.get("data")
    assert base64.b64decode(encoded_data) == raw_bytes


# ==============================================================================
# 14. Medical Safety: Urgent Warning & Forbidding Fabricated Measurements
# ==============================================================================

def test_medical_safety_urgent_warning_and_forbidding_fabricated_measurements(client: TestClient, db_session: Session, auth_headers: dict[str, str]):
    cycle_repo = MedicalCycleRepository(db_session)
    cycle_id = "test-cycle-ecg-safety-01"
    cycle = cycle_repo.create(cycle_id=cycle_id, status="active")

    acute_ecg_output = {
        "recording_type": "standard_12_lead",
        "recording_quality": "good",
        "lead_name": "12-lead standard",
        "heart_rate_bpm": 178,
        "rhythm_type": "ventricular_tachycardia",
        "rhythm_regularity": "regular",
        "pr_interval_ms": None,
        "qrs_duration_ms": 152.0,  # Wide QRS!
        "qt_interval_ms": None,
        "qtc_interval_ms": None,
        "electrical_axis": "extreme_axis_deviation",
        "st_segment_findings": "concordant ST elevation > 2mm in V1-V3",
        "t_wave_findings": "monomorphic wide complexes with secondary ST-T discordance",
        "limitations": [
            "Vision analysis cannot substitute for hospital monitoring or defibrillator readiness."
        ],
        "uncertainties": [],
        "ecg_summary": (
            "Regular monomorphic wide-complex tachycardia at 178 bpm with AV dissociation features, "
            "highly suggestive of Ventricular Tachycardia. Acute urgent condition."
        ),
        "potential_urgent_warning": "Sustained wide-complex tachycardia at 178 bpm; immediate emergency medical evaluation required.",
    }

    recorded_calls: List[Dict[str, Any]] = []
    mock_gateway = _build_ecg_mock_gateway(recorded_calls, ecg_override=acute_ecg_output)
    app.dependency_overrides[get_gemini_gateway] = lambda: mock_gateway

    jpeg_data = _create_dummy_jpeg_bytes(512)
    files = {"file": ("acute_vt.jpg", io.BytesIO(jpeg_data), "image/jpeg")}

    resp = client.post("/api/v1/cascade/intake/ecg-image", data={"cycle_id": cycle.cycle_id}, files=files, headers=auth_headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()

    # Safe client response: zero diagnostic warnings, heart rate, or AI CoT exposed
    assert body["status"] == "processed"
    assert body["intake_version"] == 1
    for forbidden in ["has_urgent_warning", "urgent_warning", "heart_rate_bpm", "limitations", "chain_of_thought", "thought"]:
        assert forbidden not in body, f"{forbidden} leaked into client response!"

    # Internal server_only report stores urgent warning for consilium evaluation
    report_repo = ModelReportRepository(db_session)
    report = report_repo.list_server_only(cycle_id, PREPARATORY_ECG_REPORT_TYPE)[0]
    assert report.content["potential_urgent_warning"] is not None
    assert "wide-complex tachycardia" in report.content["potential_urgent_warning"].lower()
    assert report.content["heart_rate_bpm"] == 178


# ==============================================================================
# 15. Database Failure Rollback Mid-Save (Zero Partial Records)
# ==============================================================================

@pytest.mark.anyio
async def test_database_failure_rollback_mid_save(db_session: Session, monkeypatch: pytest.MonkeyPatch):
    cycle_repo = MedicalCycleRepository(db_session)
    cycle_id = "test-cycle-ecg-dberr-01"
    cycle_repo.create(cycle_id=cycle_id, status="active")

    recorded_calls: List[Dict[str, Any]] = []
    mock_gateway = _build_ecg_mock_gateway(recorded_calls)
    service = EcgVisionService(db=db_session, gateway=mock_gateway)

    rollback_called = False
    original_rollback = db_session.rollback

    def mock_rollback():
        nonlocal rollback_called
        rollback_called = True
        return original_rollback()

    def mock_commit_failure():
        raise RuntimeError("Simulated database write error during ECG intake commit")

    monkeypatch.setattr(db_session, "commit", mock_commit_failure)
    monkeypatch.setattr(db_session, "rollback", mock_rollback)

    fake_file = type("FakeUploadFile", (), {
        "filename": "ecg.jpg",
        "read": lambda self: asyncio.sleep(0, result=_create_dummy_jpeg_bytes(512)),
    })()

    with pytest.raises(RuntimeError, match="Simulated database write error"):
        await service.process_ecg_image(cycle_id=cycle_id, file=fake_file)  # type: ignore

    # Ensure transaction was rolled back and 0 reports exist
    assert rollback_called is True
    report_repo = ModelReportRepository(db_session)
    reports = report_repo.list_server_only(cycle_id, PREPARATORY_ECG_REPORT_TYPE)
    assert len(reports) == 0


# ==============================================================================
# 16. New ECG Upload Leaves Published Pro Consilium Reports Untouched
# ==============================================================================

@pytest.mark.anyio
async def test_new_ecg_leaves_published_pro_consilium_reports_untouched(client: TestClient, db_session: Session, auth_headers: dict[str, str]):
    cycle_repo = MedicalCycleRepository(db_session)
    cycle_id = "test-cycle-ecg-published-consilium"
    cycle = cycle_repo.create(cycle_id=cycle_id, status="active")

    report_repo = ModelReportRepository(db_session)

    # 1. Seed preparatory anamnesis (M1)
    m1_content = _sample_anamnesis_output()
    m1_content["agent_role"] = AegisAgentRole.ANAMNESIS_M1.value
    report_repo.create_next_version(
        cycle_id=cycle_id,
        model="gemini-3.6-flash",
        report_type=PREPARATORY_ANAMNESIS_REPORT_TYPE,
        privacy_level=PrivacyLevel.SERVER_ONLY,
        content=m1_content,
    )

    # 2. Upload initial ECG (v1)
    ecg_calls_1: List[Dict[str, Any]] = []
    output_1 = _sample_smartwatch_ecg_output(heart_rate=70)
    mock_gateway_1 = _build_ecg_mock_gateway(ecg_calls_1, ecg_override=output_1)
    app.dependency_overrides[get_gemini_gateway] = lambda: mock_gateway_1

    jpeg_bytes_1 = _create_dummy_jpeg_bytes(512)
    resp1 = client.post(
        "/api/v1/cascade/intake/ecg-image",
        data={"cycle_id": cycle.cycle_id, "record_date": "2026-10-01"},
        files={"file": ("ecg_1.jpg", io.BytesIO(jpeg_bytes_1), "image/jpeg")},
        headers=auth_headers,
    )
    assert resp1.status_code == 200
    assert resp1.json()["intake_version"] == 1
    assert resp1.json()["primary_consilium_concluded"] is False

    # 3. Conclude Primary Consilium -> publishes Pro reports (user_overall_report, chief_physician_internal)
    consilium_calls: List[Dict[str, Any]] = []
    consilium_gateway = _build_consilium_mock_gateway(consilium_calls)
    primary_service = PrimaryConsiliumService(db=db_session, gateway=consilium_gateway)
    primary_resp = await primary_service.run_primary_consilium(cycle_id=cycle_id)
    assert primary_resp.cycle_id == cycle_id

    # Verify published Pro reports exist
    user_reports_before = report_repo.list_client_facing(cycle_id)
    assert len(user_reports_before) >= 1
    user_overall_before = next(r for r in user_reports_before if r.report_type == "user_overall_report")
    chief_internal_before = next(
        r for r in report_repo.list_server_only(cycle_id) if r.report_type == "chief_physician_internal"
    )

    user_overall_content_before = dict(user_overall_before.content)
    user_overall_version_before = user_overall_before.version
    chief_internal_content_before = dict(chief_internal_before.content)
    chief_internal_version_before = chief_internal_before.version

    # 4. Upload a second ECG (v2) for a later date
    ecg_calls_2: List[Dict[str, Any]] = []
    output_2 = _sample_smartwatch_ecg_output(heart_rate=82)
    mock_gateway_2 = _build_ecg_mock_gateway(ecg_calls_2, ecg_override=output_2)
    app.dependency_overrides[get_gemini_gateway] = lambda: mock_gateway_2

    jpeg_bytes_2 = _create_dummy_jpeg_bytes(650)
    resp2 = client.post(
        "/api/v1/cascade/intake/ecg-image",
        data={"cycle_id": cycle.cycle_id, "record_date": "2026-10-18"},
        files={"file": ("ecg_2.jpg", io.BytesIO(jpeg_bytes_2), "image/jpeg")},
        headers=auth_headers,
    )
    assert resp2.status_code == 200
    body2 = resp2.json()
    assert body2["intake_version"] == 2
    assert body2["primary_consilium_concluded"] is True

    # 5. Crucially: Existing published Pro reports are NEVER modified or overwritten
    user_overall_after = next(
        r for r in report_repo.list_client_facing(cycle_id) if r.report_type == "user_overall_report"
    )
    chief_internal_after = next(
        r for r in report_repo.list_server_only(cycle_id) if r.report_type == "chief_physician_internal"
    )

    assert user_overall_after.version == user_overall_version_before
    assert user_overall_after.content == user_overall_content_before
    assert chief_internal_after.version == chief_internal_version_before
    assert chief_internal_after.content == chief_internal_content_before

    # Verify both ECG preparatory versions are saved in history
    ecg_versions = report_repo.list_server_only(cycle_id, PREPARATORY_ECG_REPORT_TYPE)
    assert len(ecg_versions) == 2
    assert ecg_versions[0].version == 2
    assert ecg_versions[1].version == 1
