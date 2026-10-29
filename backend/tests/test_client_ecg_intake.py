import asyncio
import hashlib
import io
import json
import uuid
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, Generator, List, Optional
import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import settings
from app.database import get_db
from app.main import app
from app.models.base import Base
from app.models.device import Device
from app.models.device_refresh_token import DeviceRefreshToken
from app.models.patient import Patient
from app.repositories.cycle_repository import MedicalCycleRepository
from app.repositories.report_repository import ModelReportRepository
from app.routers.client_cascade import get_gemini_gateway, get_server_now_provider
from app.schemas.consilium import (
    EcgRecordingQuality,
    EcgRecordingType,
    EcgVisionIntakeClientResponse,
)
from app.security.tokens import (
    SCOPE_CASCADE_INTAKE,
    SCOPE_CASCADE_RUN,
    SCOPE_CYCLE_READ,
    SCOPE_REPORTS_READ,
    SCOPE_VITALS_READ,
    SCOPE_VITALS_WRITE,
    create_access_token,
)
from app.services.gemini_gateway import GeminiGateway, GeminiGatewayError, GeminiModelRole
from app.services.primary_consilium_service import PREPARATORY_ECG_REPORT_TYPE
from tests.test_primary_consilium import _gemini_envelope


# --- Test Helpers & Magic Byte Generators ---

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


def _sample_smartwatch_ecg_output(
    heart_rate: int = 72,
    quality: str = EcgRecordingQuality.GOOD.value,
) -> Dict[str, Any]:
    return {
        "recording_type": EcgRecordingType.SINGLE_LEAD_SMARTWATCH.value,
        "recording_quality": quality,
        "lead_name": "Lead I (Galaxy Watch)",
        "heart_rate_bpm": heart_rate,
        "rhythm_type": "sinus",
        "rhythm_regularity": "regular",
        "pr_interval_ms": 156.0,
        "qrs_duration_ms": 86.0,
        "qt_interval_ms": 382.0,
        "qtc_interval_ms": 418.0,
        "electrical_axis": None,
        "st_segment_findings": "isoelectric in Lead I",
        "t_wave_findings": "upright normal morphology in Lead I",
        "limitations": [
            "Single-lead smartwatch recording: cannot detect transmural ischemia.",
            "Cannot substitute for comprehensive 12-lead diagnostic ECG.",
        ],
        "uncertainties": ["Minor baseline drift."],
        "ecg_summary": (
            f"Smartwatch Lead I recording: Regular sinus rhythm, ventricular rate {heart_rate} bpm. "
            "Normal QRS width (86 ms), PR interval (156 ms), QTc (418 ms)."
        ),
        "potential_urgent_warning": None,
    }


def _sample_unreadable_ecg_output() -> Dict[str, Any]:
    return {
        "recording_type": EcgRecordingType.UNKNOWN.value,
        "recording_quality": EcgRecordingQuality.UNREADABLE.value,
        "lead_name": None,
        "heart_rate_bpm": None,
        "rhythm_type": None,
        "rhythm_regularity": None,
        "pr_interval_ms": None,
        "qrs_duration_ms": None,
        "qt_interval_ms": None,
        "qtc_interval_ms": None,
        "electrical_axis": None,
        "st_segment_findings": None,
        "t_wave_findings": None,
        "limitations": ["Severe movement artifact obscuring all diagnostic waveforms."],
        "uncertainties": ["Unable to determine rhythm or ventricular rate."],
        "ecg_summary": "Unreadable recording due to extensive motion artifact; repeat recommended.",
        "potential_urgent_warning": None,
    }


TEST_JWT_SECRET = "test_jwt_signing_secret_at_least_16_chars_long!"
TEST_PAIRING_SECRET = "test_pairing_secret_deterministic_12345!"
TEST_API_SECRET = "test_device_secret_token_12345"

_CURRENT_DB_SESSION: Optional[Session] = None
_TestingSessionLocal: Optional[sessionmaker] = None


def _build_test_device_jwt(
    patient_id: str,
    device_id: str,
    scope: str = (
        f"{SCOPE_VITALS_READ} {SCOPE_VITALS_WRITE} {SCOPE_CYCLE_READ} "
        f"{SCOPE_CASCADE_INTAKE} {SCOPE_CASCADE_RUN} {SCOPE_REPORTS_READ}"
    ),
    session: Optional[Session] = None,
) -> str:
    s = session or _CURRENT_DB_SESSION
    family_id = f"fam_{uuid.uuid4().hex[:12]}"
    if s is not None:
        now = datetime.now(timezone.utc)
        patient = s.query(Patient).filter_by(patient_id=patient_id).first()
        if not patient:
            patient = Patient(patient_id=patient_id, status="active")
            s.add(patient)

        device = s.query(Device).filter_by(device_id=device_id).first()
        if not device:
            device = Device(
                device_id=device_id,
                patient_id=patient_id,
                device_name="Test Device",
                platform="android",
                is_active=True,
                paired_at=now,
            )
            s.add(device)

        refresh_token = DeviceRefreshToken(
            token_id=f"tok_{uuid.uuid4().hex[:12]}",
            device_id=device_id,
            token_hash=hashlib.sha256(uuid.uuid4().bytes).hexdigest(),
            family_id=family_id,
            created_at=now,
            expires_at=now + timedelta(days=30),
        )
        s.add(refresh_token)
        s.commit()

    return create_access_token(
        patient_id=patient_id,
        device_id=device_id,
        family_id=family_id,
        scope=scope,
        secret=settings.jwt_secret,
    ).access_token


# --- Pytest Fixtures ---

@pytest.fixture
def db_session() -> Generator[Session, None, None]:
    global _CURRENT_DB_SESSION, _TestingSessionLocal
    orig_jwt = settings.jwt_secret
    orig_pair = settings.pairing_secret
    orig_api = settings.api_secret
    orig_iss = settings.jwt_issuer
    orig_aud = settings.jwt_audience

    settings.jwt_secret = TEST_JWT_SECRET
    settings.pairing_secret = TEST_PAIRING_SECRET
    settings.api_secret = TEST_API_SECRET
    settings.jwt_issuer = "aegis-backend"
    settings.jwt_audience = "aegis-mobile-client"

    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    _TestingSessionLocal = sessionmaker(autocommit=False, autoflush=True, bind=engine)
    session = _TestingSessionLocal()
    _CURRENT_DB_SESSION = session
    try:
        yield session
    finally:
        _CURRENT_DB_SESSION = None
        _TestingSessionLocal = None
        session.close()
        Base.metadata.drop_all(bind=engine)
        engine.dispose()
        settings.jwt_secret = orig_jwt
        settings.pairing_secret = orig_pair
        settings.api_secret = orig_api
        settings.jwt_issuer = orig_iss
        settings.jwt_audience = orig_aud


@pytest.fixture
def client_with_mock_gateway(db_session: Session):
    def override_get_db():
        if _TestingSessionLocal is None:
            yield db_session
            return
        db = _TestingSessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db

    def _setup_client(
        ecg_output: Optional[Dict[str, Any]] = None,
        fail_with_status: Optional[int] = None,
        delay_seconds: float = 0.0,
    ):
        recorded_calls: List[Dict[str, Any]] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            recorded_calls.append({"url_path": request.url.path})
            if delay_seconds > 0:
                await asyncio.sleep(delay_seconds)
            if fail_with_status:
                return httpx.Response(fail_with_status, json={"error": "Simulated Gateway Failure"})
            output = ecg_output if ecg_output is not None else _sample_smartwatch_ecg_output()
            return httpx.Response(200, json=_gemini_envelope(output))

        mock_gateway = GeminiGateway(
            api_key="test-key",
            transport=httpx.MockTransport(handler),
            sleep_func=lambda _: asyncio.sleep(0),
        )
        app.dependency_overrides[get_gemini_gateway] = lambda: mock_gateway
        return TestClient(app), recorded_calls

    yield _setup_client

    app.dependency_overrides.pop(get_db, None)
    app.dependency_overrides.pop(get_gemini_gateway, None)


# ==============================================================================
# TARGETED TESTS FOR STAGE 5.4D-5A: CLIENT ECG INTAKE
# ==============================================================================

def test_01_client_ecg_intake_successful_upload(client_with_mock_gateway, db_session: Session):
    """1. Successful ECG upload: auto-provisions cycle, runs Vision ECG, returns 200 and updates status."""
    client, recorded_calls = client_with_mock_gateway()
    token = _build_test_device_jwt(patient_id="patient_ecg_1", device_id="dev_ecg_1")

    jpeg_bytes = _create_dummy_jpeg_bytes(1024)
    files = {"file": ("ecg_strip.jpg", io.BytesIO(jpeg_bytes), "image/jpeg")}

    resp = client.post(
        "/api/v1/client/cascade/intake/ecg",
        headers={"Authorization": f"Bearer {token}"},
        files=files,
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()

    assert data["status"] == "processed"
    assert data["intake_version"] == 1
    assert data["recording_type"] == "single_lead_smartwatch"
    assert data["recording_quality"] == "good"
    assert data["primary_consilium_concluded"] is False
    assert "privacy_notice" in data
    assert len(recorded_calls) == 1

    # Verify cycle auto-provisioned
    cycle_repo = MedicalCycleRepository(db_session)
    cycle = cycle_repo.get_active_for_patient("patient_ecg_1")
    assert cycle is not None
    assert cycle.cycle_id == data["cycle_id"]

    # Verify DB report
    report_repo = ModelReportRepository(db_session)
    reports = report_repo.list_server_only(cycle_id=cycle.cycle_id, report_type=PREPARATORY_ECG_REPORT_TYPE)
    assert len(reports) == 1
    assert reports[0].version == 1
    assert reports[0].content["heart_rate_bpm"] == 72

    # Verify cascade readiness status
    status_resp = client.get(
        "/api/v1/client/cascade/status",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert status_resp.status_code == 200
    status_data = status_resp.json()
    assert status_data["has_active_cycle"] is True
    assert status_data["has_preparatory_ecg"] is True

    # Verify queryable latest ECG endpoint
    latest_resp = client.get(
        "/api/v1/client/cascade/ecg/latest",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert latest_resp.status_code == 200
    latest_data = latest_resp.json()
    assert latest_data["intake_version"] == 1
    assert latest_data["recording_type"] == "single_lead_smartwatch"


def test_02_client_ecg_intake_unauthorized_and_forbidden(client_with_mock_gateway):
    """2. Auth checks: missing (401), invalid (401), and insufficient scope (403)."""
    client, recorded_calls = client_with_mock_gateway()
    jpeg_bytes = _create_dummy_jpeg_bytes(512)

    # Missing auth header
    resp_no_auth = client.post(
        "/api/v1/client/cascade/intake/ecg",
        files={"file": ("ecg.jpg", io.BytesIO(jpeg_bytes), "image/jpeg")},
    )
    assert resp_no_auth.status_code == 401

    # Invalid token
    resp_invalid = client.post(
        "/api/v1/client/cascade/intake/ecg",
        headers={"Authorization": "Bearer totally_invalid_jwt_token_payload"},
        files={"file": ("ecg.jpg", io.BytesIO(jpeg_bytes), "image/jpeg")},
    )
    assert resp_invalid.status_code == 401

    # Token with narrow scopes lacking cascade:intake
    narrow_token = _build_test_device_jwt(
        patient_id="patient_narrow",
        device_id="dev_narrow",
        scope=f"{SCOPE_VITALS_READ} {SCOPE_REPORTS_READ}",
    )
    resp_forbidden = client.post(
        "/api/v1/client/cascade/intake/ecg",
        headers={"Authorization": f"Bearer {narrow_token}"},
        files={"file": ("ecg.jpg", io.BytesIO(jpeg_bytes), "image/jpeg")},
    )
    assert resp_forbidden.status_code == 403
    assert "Insufficient token scope" in resp_forbidden.text

    # Zero Gemini calls dispatched
    assert len(recorded_calls) == 0


def test_03_client_ecg_intake_patient_isolation(client_with_mock_gateway, db_session: Session):
    """3. Patient isolation: Patient A and Patient B data isolated; arbitrary client cycle/patient IDs rejected."""
    client, recorded_calls = client_with_mock_gateway()
    token_a = _build_test_device_jwt(patient_id="patient_iso_A", device_id="dev_A")
    token_b = _build_test_device_jwt(patient_id="patient_iso_B", device_id="dev_B")

    # Patient A uploads ECG, attempting to inject patient_id="patient_iso_B"
    jpeg_bytes = _create_dummy_jpeg_bytes(1024)
    resp_a = client.post(
        "/api/v1/client/cascade/intake/ecg",
        headers={"Authorization": f"Bearer {token_a}"},
        files={"file": ("ecg_a.jpg", io.BytesIO(jpeg_bytes), "image/jpeg")},
        data={"patient_id": "patient_iso_B", "cycle_id": "cycle_fake_123"},
    )
    assert resp_a.status_code == 200
    data_a = resp_a.json()

    # Verify bound strictly to Patient A
    cycle_repo = MedicalCycleRepository(db_session)
    cycle_a = cycle_repo.get_active_for_patient("patient_iso_A")
    assert cycle_a is not None
    assert data_a["cycle_id"] == cycle_a.cycle_id
    assert data_a["cycle_id"] != "cycle_fake_123"

    # Patient B uploads separate ECG
    png_bytes = _create_dummy_png_bytes(1024)
    resp_b = client.post(
        "/api/v1/client/cascade/intake/ecg",
        headers={"Authorization": f"Bearer {token_b}"},
        files={"file": ("ecg_b.png", io.BytesIO(png_bytes), "image/png")},
    )
    assert resp_b.status_code == 200
    data_b = resp_b.json()

    cycle_b = cycle_repo.get_active_for_patient("patient_iso_B")
    assert cycle_b is not None
    assert data_b["cycle_id"] == cycle_b.cycle_id
    assert cycle_a.cycle_id != cycle_b.cycle_id

    # Cross-query isolation: Patient B's latest ECG is distinct
    latest_b = client.get(
        "/api/v1/client/cascade/ecg/latest",
        headers={"Authorization": f"Bearer {token_b}"},
    )
    assert latest_b.json()["cycle_id"] == cycle_b.cycle_id


def test_04_client_ecg_intake_invalid_format_and_size_limit(client_with_mock_gateway):
    """4. File validation: reject >10MB, plain text, fake extensions, empty files with 400 Bad Request."""
    client, recorded_calls = client_with_mock_gateway()
    token = _build_test_device_jwt(patient_id="patient_val", device_id="dev_val")

    # a) File exceeds 10MB limit (10MB + 1 byte)
    too_large = b"\xff\xd8\xff\xe0" + b"\x00" * (10 * 1024 * 1024 + 1)
    resp_large = client.post(
        "/api/v1/client/cascade/intake/ecg",
        headers={"Authorization": f"Bearer {token}"},
        files={"file": ("huge_ecg.jpg", io.BytesIO(too_large), "image/jpeg")},
    )
    assert resp_large.status_code == 400
    assert "exceeds maximum allowed size" in resp_large.text

    # b) Disguised text file
    fake_png = b"This is plain text pretending to be an ECG image."
    resp_txt = client.post(
        "/api/v1/client/cascade/intake/ecg",
        headers={"Authorization": f"Bearer {token}"},
        files={"file": ("fake_ecg.png", io.BytesIO(fake_png), "image/png")},
    )
    assert resp_txt.status_code == 400
    assert "not a supported valid image format" in resp_txt.text

    # c) Disguised PDF file
    fake_pdf = b"%PDF-1.4\n1 0 obj\n<<>>\nendobj"
    resp_pdf = client.post(
        "/api/v1/client/cascade/intake/ecg",
        headers={"Authorization": f"Bearer {token}"},
        files={"file": ("ecg.jpg", io.BytesIO(fake_pdf), "image/jpeg")},
    )
    assert resp_pdf.status_code == 400
    assert "not a supported valid image format" in resp_pdf.text

    # d) Empty file (0 bytes)
    resp_empty = client.post(
        "/api/v1/client/cascade/intake/ecg",
        headers={"Authorization": f"Bearer {token}"},
        files={"file": ("empty.jpg", io.BytesIO(b""), "image/jpeg")},
    )
    assert resp_empty.status_code == 400
    assert "is empty" in resp_empty.text

    # e) Missing file parameter
    resp_nofile = client.post(
        "/api/v1/client/cascade/intake/ecg",
        headers={"Authorization": f"Bearer {token}"},
        data={"record_date": "2026-10-29"},
    )
    assert resp_nofile.status_code == 400
    assert "An electrocardiogram image file must be uploaded" in resp_nofile.text

    # Zero Gemini calls
    assert len(recorded_calls) == 0


def test_05_client_ecg_intake_duplicate_upload_deduplication(client_with_mock_gateway, db_session: Session):
    """5. Deduplication: re-upload of identical ECG image returns 200 without second Gemini call or duplicate report."""
    client, recorded_calls = client_with_mock_gateway()
    token = _build_test_device_jwt(patient_id="patient_dedup", device_id="dev_dedup")

    jpeg_bytes = _create_dummy_jpeg_bytes(1024)

    # First upload
    resp1 = client.post(
        "/api/v1/client/cascade/intake/ecg",
        headers={"Authorization": f"Bearer {token}"},
        files={"file": ("ecg_strip.jpg", io.BytesIO(jpeg_bytes), "image/jpeg")},
    )
    assert resp1.status_code == 200
    data1 = resp1.json()
    assert data1["intake_version"] == 1
    assert len(recorded_calls) == 1

    # Second upload with identical content
    resp2 = client.post(
        "/api/v1/client/cascade/intake/ecg",
        headers={"Authorization": f"Bearer {token}"},
        files={"file": ("ecg_strip_duplicate.jpg", io.BytesIO(jpeg_bytes), "image/jpeg")},
    )
    assert resp2.status_code == 200
    data2 = resp2.json()
    assert data2["intake_version"] == 1
    assert data2["cycle_id"] == data1["cycle_id"]

    # Invariant: Zero additional Gemini calls made (total remains 1)
    assert len(recorded_calls) == 1

    # Invariant: DB contains exactly 1 report version
    report_repo = ModelReportRepository(db_session)
    reports = report_repo.list_server_only(cycle_id=data1["cycle_id"], report_type=PREPARATORY_ECG_REPORT_TYPE)
    assert len(reports) == 1


def test_06_client_ecg_intake_preserves_previous_ecgs(client_with_mock_gateway, db_session: Session):
    """6. History preservation: uploading new distinct ECG creates Version 2 without deleting Version 1."""
    client, recorded_calls = client_with_mock_gateway()
    token = _build_test_device_jwt(patient_id="patient_hist", device_id="dev_hist")

    # Upload Study 1 (Smartwatch strip)
    ecg1_bytes = _create_dummy_jpeg_bytes(1024)
    resp1 = client.post(
        "/api/v1/client/cascade/intake/ecg",
        headers={"Authorization": f"Bearer {token}"},
        files={"file": ("ecg_study_1.jpg", io.BytesIO(ecg1_bytes), "image/jpeg")},
        data={"record_date": "2026-10-01"},
    )
    assert resp1.status_code == 200
    assert resp1.json()["intake_version"] == 1

    # Upload Study 2 (Distinct PNG recording)
    ecg2_bytes = _create_dummy_png_bytes(2048)
    resp2 = client.post(
        "/api/v1/client/cascade/intake/ecg",
        headers={"Authorization": f"Bearer {token}"},
        files={"file": ("ecg_study_2.png", io.BytesIO(ecg2_bytes), "image/png")},
        data={"record_date": "2026-10-29"},
    )
    assert resp2.status_code == 200
    assert resp2.json()["intake_version"] == 2

    # Invariant: Both versions exist in DB with their distinct contents
    report_repo = ModelReportRepository(db_session)
    cycle_id = resp1.json()["cycle_id"]
    reports = report_repo.list_server_only(cycle_id=cycle_id, report_type=PREPARATORY_ECG_REPORT_TYPE)
    assert len(reports) == 2
    versions = [r.version for r in reports]
    assert 1 in versions and 2 in versions

    # Latest endpoint returns Version 2
    latest = client.get(
        "/api/v1/client/cascade/ecg/latest",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert latest.json()["intake_version"] == 2

    # Re-uploading Study 1 matches Version 1 across history without creating Version 3
    resp1_retry = client.post(
        "/api/v1/client/cascade/intake/ecg",
        headers={"Authorization": f"Bearer {token}"},
        files={"file": ("ecg_study_1.jpg", io.BytesIO(ecg1_bytes), "image/jpeg")},
    )
    assert resp1_retry.status_code == 200
    assert resp1_retry.json()["intake_version"] == 1

    # Still exactly 2 versions in DB
    reports_after = report_repo.list_server_only(cycle_id=cycle_id, report_type=PREPARATORY_ECG_REPORT_TYPE)
    assert len(reports_after) == 2


def test_07_client_ecg_intake_uncertain_or_unreadable_interpretation(client_with_mock_gateway, db_session: Session):
    """7. Medical safety: unreadable ECG preserves limitations and uncertainties without fabricating diagnoses."""
    unreadable_out = _sample_unreadable_ecg_output()
    client, recorded_calls = client_with_mock_gateway(ecg_output=unreadable_out)
    token = _build_test_device_jwt(patient_id="patient_unreadable", device_id="dev_unreadable")

    jpeg_bytes = _create_dummy_jpeg_bytes(1024)
    resp = client.post(
        "/api/v1/client/cascade/intake/ecg",
        headers={"Authorization": f"Bearer {token}"},
        files={"file": ("noisy_ecg.jpg", io.BytesIO(jpeg_bytes), "image/jpeg")},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "processed"
    assert data["recording_quality"] == "unreadable"

    # Verify stored report in DB
    report_repo = ModelReportRepository(db_session)
    reports = report_repo.list_server_only(cycle_id=data["cycle_id"], report_type=PREPARATORY_ECG_REPORT_TYPE)
    assert len(reports) == 1
    content = reports[0].content
    assert content["heart_rate_bpm"] is None
    assert content["rhythm_type"] is None
    assert "Severe movement artifact" in content["limitations"][0]


def test_08_client_ecg_intake_gemini_error_preserves_history(client_with_mock_gateway, db_session: Session):
    """8. Gateway error resilience: Gemini error (502) on second upload leaves prior ECG intact."""
    # First upload succeeds
    client1, _ = client_with_mock_gateway()
    token = _build_test_device_jwt(patient_id="patient_fail_test", device_id="dev_fail")

    ecg1_bytes = _create_dummy_jpeg_bytes(1024)
    resp1 = client1.post(
        "/api/v1/client/cascade/intake/ecg",
        headers={"Authorization": f"Bearer {token}"},
        files={"file": ("ecg_v1.jpg", io.BytesIO(ecg1_bytes), "image/jpeg")},
    )
    assert resp1.status_code == 200
    cycle_id = resp1.json()["cycle_id"]

    # Second upload fails with 502 Bad Gateway
    client2, _ = client_with_mock_gateway(fail_with_status=503)
    ecg2_bytes = _create_dummy_png_bytes(2048)
    resp2 = client2.post(
        "/api/v1/client/cascade/intake/ecg",
        headers={"Authorization": f"Bearer {token}"},
        files={"file": ("ecg_v2.png", io.BytesIO(ecg2_bytes), "image/png")},
    )
    assert resp2.status_code == 502
    assert "Gemini Vision ECG extraction failed" in resp2.text

    # Invariant: Version 1 remains intact in DB
    report_repo = ModelReportRepository(db_session)
    reports = report_repo.list_server_only(cycle_id=cycle_id, report_type=PREPARATORY_ECG_REPORT_TYPE)
    assert len(reports) == 1
    assert reports[0].version == 1


def test_09_client_ecg_intake_alias_and_files_field(client_with_mock_gateway):
    """9. Compatibility: route alias /ecg/upload with 'files' field name and /ecg/latest."""
    client, recorded_calls = client_with_mock_gateway()
    token = _build_test_device_jwt(patient_id="patient_alias", device_id="dev_alias")

    jpeg_bytes = _create_dummy_jpeg_bytes(1024)

    # Post to alias using field name "files"
    resp = client.post(
        "/api/v1/client/ecg/upload",
        headers={"Authorization": f"Bearer {token}"},
        files=[("files", ("ecg_alias.jpg", io.BytesIO(jpeg_bytes), "image/jpeg"))],
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "processed"
    assert resp.json()["intake_version"] == 1

    # Query alias /ecg/latest
    latest_resp = client.get(
        "/api/v1/client/ecg/latest",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert latest_resp.status_code == 200
    assert latest_resp.json()["intake_version"] == 1


@pytest.mark.anyio
async def test_10_client_ecg_intake_concurrent_duplicate_upload(client_with_mock_gateway):
    """10. Concurrency: concurrent identical uploads serialize cleanly, invoke Gemini once, and return 200."""
    client, recorded_calls = client_with_mock_gateway(delay_seconds=0.05)
    token = _build_test_device_jwt(patient_id="patient_concurrent", device_id="dev_concurrent")
    token_other = _build_test_device_jwt(patient_id="patient_other_ecg", device_id="dev_other_ecg")

    jpeg_bytes = _create_dummy_jpeg_bytes(1024)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as ac:
        req1 = ac.post(
            "/api/v1/client/cascade/intake/ecg",
            headers={"Authorization": f"Bearer {token}"},
            files={"file": ("concurrent_strip_1.jpg", io.BytesIO(jpeg_bytes), "image/jpeg")},
        )
        req2 = ac.post(
            "/api/v1/client/cascade/intake/ecg",
            headers={"Authorization": f"Bearer {token}"},
            files={"file": ("concurrent_strip_2.jpg", io.BytesIO(jpeg_bytes), "image/jpeg")},
        )
        req_other = ac.post(
            "/api/v1/client/cascade/intake/ecg",
            headers={"Authorization": f"Bearer {token_other}"},
            files={"file": ("other_strip.jpg", io.BytesIO(jpeg_bytes), "image/jpeg")},
        )

        resp1, resp2, resp_other = await asyncio.gather(req1, req2, req_other)

    assert resp1.status_code == 200, resp1.text
    assert resp2.status_code == 200, resp2.text
    assert resp_other.status_code == 200, resp_other.text

    d1 = resp1.json()
    d2 = resp2.json()
    d_other = resp_other.json()

    # Patient concurrent invariants:
    # 1. Both concurrent requests resolve to the same cycle
    assert d1["cycle_id"] == d2["cycle_id"]
    # 2. Both report intake_version = 1 (no duplicate increment)
    assert d1["intake_version"] == 1
    assert d2["intake_version"] == 1
    assert d1["status"] == "processed"
    assert d2["status"] == "processed"

    # Patient other invariants:
    # 3. Patient other cycle is completely isolated from patient concurrent
    assert d_other["cycle_id"] != d1["cycle_id"]
    assert d_other["intake_version"] == 1

    # 4. Total Gemini calls: exactly 1 for patient concurrent + 1 for patient other = 2 total
    # (patient concurrent's duplicate upload was safely deduped without invoking Gemini a second time)
    assert len(recorded_calls) == 2


def test_11_client_ecg_intake_zero_secrets_leakage(client_with_mock_gateway):
    """11. Zero secrets: no Gemini API keys, internal prompts, or private model roles in client response."""
    client, _ = client_with_mock_gateway()
    token = _build_test_device_jwt(patient_id="patient_secrets_check", device_id="dev_sec")

    jpeg_bytes = _create_dummy_jpeg_bytes(1024)
    resp = client.post(
        "/api/v1/client/cascade/intake/ecg",
        headers={"Authorization": f"Bearer {token}"},
        files={"file": ("ecg.jpg", io.BytesIO(jpeg_bytes), "image/jpeg")},
    )
    assert resp.status_code == 200

    resp_str = json.dumps(resp.json())
    headers_str = str(resp.headers)

    # Invariant: No API keys, prompts, or sensitive internals leaked
    assert "test-key" not in resp_str and "test-key" not in headers_str
    assert "AIzaSy" not in resp_str
    assert "build_vision_ecg_prompt" not in resp_str
    assert "generativelanguage.googleapis.com" not in resp_str


def test_12_client_ecg_post_and_get_contract_alignment_and_uncertainty_preservation(client_with_mock_gateway, db_session: Session):
    """12. Contract alignment & safety: POST and GET return identical client-facing projection,
    preserve uncertainty flags on GET, make zero Gemini calls on GET, leak no server_only fields,
    and enforce patient isolation."""
    # Step 1: Upload a standard ECG (good quality)
    client, recorded_calls = client_with_mock_gateway()
    token_a = _build_test_device_jwt(patient_id="patient_contract_a", device_id="dev_contract_a")

    jpeg_bytes = _create_dummy_jpeg_bytes(1024)
    post_resp = client.post(
        "/api/v1/client/cascade/intake/ecg",
        headers={"Authorization": f"Bearer {token_a}"},
        files={"file": ("ecg_strip.jpg", io.BytesIO(jpeg_bytes), "image/jpeg")},
    )
    assert post_resp.status_code == 200, post_resp.text
    post_data = post_resp.json()
    assert len(recorded_calls) == 1

    # Step 2: Query GET /cascade/ecg/latest
    get_resp = client.get(
        "/api/v1/client/cascade/ecg/latest",
        headers={"Authorization": f"Bearer {token_a}"},
    )
    assert get_resp.status_code == 200, get_resp.text
    get_data = get_resp.json()

    # Invariant: GET makes ZERO Gemini calls
    assert len(recorded_calls) == 1

    # Invariant: Contract alignment - POST and GET return identical schema keys and values
    expected_keys = {
        "cycle_id",
        "status",
        "intake_version",
        "recording_type",
        "recording_quality",
        "created_at_utc",
        "primary_consilium_concluded",
        "privacy_notice",
    }
    assert set(post_data.keys()) == expected_keys
    assert set(get_data.keys()) == expected_keys
    assert post_data["cycle_id"] == get_data["cycle_id"]
    assert post_data["status"] == get_data["status"] == "processed"
    assert post_data["intake_version"] == get_data["intake_version"] == 1
    assert post_data["recording_type"] == get_data["recording_type"] == "single_lead_smartwatch"
    assert post_data["recording_quality"] == get_data["recording_quality"] == "good"
    assert post_data["created_at_utc"] == get_data["created_at_utc"]
    assert post_data["primary_consilium_concluded"] == get_data["primary_consilium_concluded"] is False
    assert post_data["privacy_notice"] == get_data["privacy_notice"]

    # Invariant: Zero leakage of server_only intermediate metrics
    server_only_fields = [
        "heart_rate_bpm",
        "rhythm_regularity",
        "rhythm_type",
        "intervals",
        "pr_interval_ms",
        "qrs_duration_ms",
        "qt_interval_ms",
        "qtc_interval_ms",
        "electrical_axis",
        "st_segment_findings",
        "t_wave_findings",
        "limitations",
        "uncertainties",
        "raw_unresolved_metrics",
        "potential_urgent_warning",
        "ecg_summary",
        "agent_role",
    ]
    for field in server_only_fields:
        assert field not in post_data, f"server_only field '{field}' leaked into POST response"
        assert field not in get_data, f"server_only field '{field}' leaked into GET response"

    # Step 3: Test uncertainty preservation for unreadable/poor quality ECG
    unreadable_out = _sample_unreadable_ecg_output()
    client_unrec, rec_calls_unrec = client_with_mock_gateway(ecg_output=unreadable_out)
    token_noisy = _build_test_device_jwt(patient_id="patient_noisy_contract", device_id="dev_noisy")

    post_noisy_resp = client_unrec.post(
        "/api/v1/client/cascade/intake/ecg",
        headers={"Authorization": f"Bearer {token_noisy}"},
        files={"file": ("noisy.jpg", io.BytesIO(jpeg_bytes), "image/jpeg")},
    )
    assert post_noisy_resp.status_code == 200
    assert post_noisy_resp.json()["recording_quality"] == "unreadable"

    # Query GET for the unreadable ECG: quality uncertainty is reliably preserved
    get_noisy_resp = client_unrec.get(
        "/api/v1/client/cascade/ecg/latest",
        headers={"Authorization": f"Bearer {token_noisy}"},
    )
    assert get_noisy_resp.status_code == 200
    assert get_noisy_resp.json()["recording_quality"] == "unreadable"
    assert get_noisy_resp.json()["status"] == "processed"
    assert get_noisy_resp.json()["intake_version"] == 1
    assert len(rec_calls_unrec) == 1  # zero calls during GET

    # Step 4: Patient isolation: Patient B cannot view Patient A's ECG or cycle
    token_b = _build_test_device_jwt(patient_id="patient_contract_b", device_id="dev_contract_b")
    get_resp_b = client.get(
        "/api/v1/client/cascade/ecg/latest",
        headers={"Authorization": f"Bearer {token_b}"},
    )
    assert get_resp_b.status_code == 404
    assert "No active medical cycle found" in get_resp_b.text

    # Provision an active cycle for Patient B without any ECG
    cycle_repo = MedicalCycleRepository(db_session)
    cycle_b = cycle_repo.create(cycle_id="cycle_b_empty", status="active", patient_id="patient_contract_b")
    get_resp_b2 = client.get(
        "/api/v1/client/cascade/ecg/latest",
        headers={"Authorization": f"Bearer {token_b}"},
    )
    assert get_resp_b2.status_code == 404
    assert f"No electrocardiogram report found for active cycle '{cycle_b.cycle_id}'" in get_resp_b2.text
