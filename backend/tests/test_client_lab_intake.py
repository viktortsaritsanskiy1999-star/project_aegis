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
from app.repositories.patient_repository import PatientRepository
from app.routers.client_cascade import get_gemini_gateway, get_server_now_provider
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
                "requires_user_verification": False,
                "is_recognition_ambiguous": False,
            },
            {
                "original_name": "Аспартатаминотрансфераза (АСТ)",
                "marker_key": "ast",
                "raw_value": "19.8",
                "numeric_value": 19.8,
                "unit": "Ед/л",
                "reference_range": {"low": 0.0, "high": 37.0, "text": "0 - 37"},
                "sample_date": sample_date,
                "category": "biochemistry",
                "is_uncertain": False,
                "uncertainty_note": None,
                "is_critical_flag": False,
                "requires_user_verification": False,
                "is_recognition_ambiguous": False,
            },
            {
                "original_name": "Глюкоза",
                "marker_key": "glucose",
                "raw_value": "5.1",
                "numeric_value": 5.1,
                "unit": "ммоль/л",
                "reference_range": {"low": 4.1, "high": 6.1, "text": "4.1 - 6.1"},
                "sample_date": sample_date,
                "category": "biochemistry",
                "is_uncertain": False,
                "uncertainty_note": None,
                "is_critical_flag": False,
                "requires_user_verification": False,
                "is_recognition_ambiguous": False,
            },
        ]
    return {
        "document_metadata": {
            "laboratory_name": lab_name,
            "sample_date": sample_date,
            "issue_date": sample_date,
            "order_id": f"ORD_{sample_date}",
            "page_count": page_count,
            "notes": "Legible laboratory report",
        },
        "markers": markers,
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
        ocr_output: Optional[Dict[str, Any]] = None,
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
            output = ocr_output if ocr_output is not None else _sample_vision_ocr_output()
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
# TARGETED TESTS FOR STAGE 5.4D-4A: CLIENT LAB INTAKE
# ==============================================================================

def test_01_client_lab_intake_valid_document(client_with_mock_gateway, db_session: Session):
    """1. Valid document upload: auto-provisions cycle, runs Vision OCR, and records into ledger."""
    client, recorded_calls = client_with_mock_gateway()
    token = _build_test_device_jwt(patient_id="patient_alpha", device_id="dev_alpha_1")

    jpeg_bytes = _create_dummy_jpeg_bytes(1024)
    files = [("files", ("lab_test.jpg", io.BytesIO(jpeg_bytes), "image/jpeg"))]

    resp = client.post(
        "/api/v1/client/cascade/intake/labs",
        headers={"Authorization": f"Bearer {token}"},
        files=files,
    )

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["status"] == "processed"
    assert data["cycle_id"] is not None
    assert data["ledger_version"] >= 1
    assert data["extracted_count"] == 3
    assert len(data["confirmed_markers"]) == 3
    assert data["requires_verification_count"] == 0
    assert data["has_unreviewed_lab_changes"] is True
    assert len(recorded_calls) == 1

    # Verify queryable in ledger state
    state_resp = client.get(
        "/api/v1/client/labs/state",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert state_resp.status_code == 200
    state_data = state_resp.json()
    assert state_data["ledger_version"] >= 1
    assert "alt" in state_data["active_markers"]
    assert state_data["active_markers"]["alt"]["value"] == "24.5"
    assert "ast" in state_data["active_markers"]
    assert "glucose" in state_data["active_markers"]


def test_02_client_lab_intake_unauthorized(client_with_mock_gateway):
    """2. Authorization contract: missing token -> 401, invalid token -> 401, missing scope -> 403."""
    client, _ = client_with_mock_gateway()
    jpeg_bytes = _create_dummy_jpeg_bytes(512)

    # 1. Missing Authorization header
    resp_no_auth = client.post(
        "/api/v1/client/cascade/intake/labs",
        files=[("files", ("lab.jpg", io.BytesIO(jpeg_bytes), "image/jpeg"))],
    )
    assert resp_no_auth.status_code == 401

    # 2. Invalid / forged token
    resp_bad_auth = client.post(
        "/api/v1/client/cascade/intake/labs",
        headers={"Authorization": "Bearer totally.invalid.jwt"},
        files=[("files", ("lab.jpg", io.BytesIO(jpeg_bytes), "image/jpeg"))],
    )
    assert resp_bad_auth.status_code == 401

    # 3. Missing cascade:intake scope
    narrow_token = _build_test_device_jwt(
        patient_id="patient_narrow",
        device_id="dev_narrow",
        scope="vitals:read vitals:write cycle:read",
    )
    resp_no_scope = client.post(
        "/api/v1/client/cascade/intake/labs",
        headers={"Authorization": f"Bearer {narrow_token}"},
        files=[("files", ("lab.jpg", io.BytesIO(jpeg_bytes), "image/jpeg"))],
    )
    assert resp_no_scope.status_code == 403
    assert "Insufficient token scope" in resp_no_scope.text


def test_03_client_lab_intake_patient_isolation(client_with_mock_gateway, db_session: Session):
    """3. Patient isolation: Patient A cannot see or mutate Patient B's labs, and client cycle_id is ignored."""
    # Setup Patient A with ALT
    client_a, _ = client_with_mock_gateway(_sample_vision_ocr_output(
        sample_date="2026-10-10",
        markers=[{
            "original_name": "АЛТ",
            "marker_key": "alt",
            "raw_value": "30.0",
            "numeric_value": 30.0,
            "unit": "Ед/л",
            "reference_range": {"low": 0.0, "high": 40.0, "text": "0 - 40"},
            "sample_date": "2026-10-10",
            "category": "biochemistry",
            "is_uncertain": False,
            "uncertainty_note": None,
            "is_critical_flag": False,
            "requires_user_verification": False,
            "is_recognition_ambiguous": False,
        }],
    ))
    token_a = _build_test_device_jwt(patient_id="patient_A", device_id="dev_A")
    resp_a = client_a.post(
        "/api/v1/client/cascade/intake/labs",
        headers={"Authorization": f"Bearer {token_a}"},
        files=[("files", ("lab_a.jpg", io.BytesIO(_create_dummy_jpeg_bytes(512)), "image/jpeg"))],
    )
    assert resp_a.status_code == 200
    cycle_id_a = resp_a.json()["cycle_id"]

    # Setup Patient B with Bilirubin
    client_b, _ = client_with_mock_gateway(_sample_vision_ocr_output(
        sample_date="2026-10-12",
        markers=[{
            "original_name": "Билирубин",
            "marker_key": "bilirubin_total",
            "raw_value": "15.0",
            "numeric_value": 15.0,
            "unit": "мкмоль/л",
            "reference_range": {"low": 5.0, "high": 21.0, "text": "5 - 21"},
            "sample_date": "2026-10-12",
            "category": "biochemistry",
            "is_uncertain": False,
            "uncertainty_note": None,
            "is_critical_flag": False,
            "requires_user_verification": False,
            "is_recognition_ambiguous": False,
        }],
    ))
    token_b = _build_test_device_jwt(patient_id="patient_B", device_id="dev_B")

    # Patient B attempts to forge Patient A's cycle_id in the form: it MUST be ignored
    resp_b = client_b.post(
        "/api/v1/client/cascade/intake/labs",
        headers={"Authorization": f"Bearer {token_b}"},
        data={"cycle_id": cycle_id_a},  # Forged attempt
        files=[("files", ("lab_b.jpg", io.BytesIO(_create_dummy_png_bytes(512)), "image/png"))],
    )
    assert resp_b.status_code == 200
    cycle_id_b = resp_b.json()["cycle_id"]
    assert cycle_id_b != cycle_id_a

    # Patient A's ledger contains only ALT
    state_a = client_a.get("/api/v1/client/labs/state", headers={"Authorization": f"Bearer {token_a}"}).json()
    assert "alt" in state_a["active_markers"]
    assert "bilirubin_total" not in state_a["active_markers"]

    # Patient B's ledger contains only Bilirubin
    state_b = client_b.get("/api/v1/client/labs/state", headers={"Authorization": f"Bearer {token_b}"}).json()
    assert "bilirubin_total" in state_b["active_markers"]
    assert "alt" not in state_b["active_markers"]


def test_04_client_lab_intake_file_size_exceeded(client_with_mock_gateway):
    """4. File size limit: files exceeding 10 MB are rejected with 400 Bad Request."""
    client, recorded_calls = client_with_mock_gateway()
    token = _build_test_device_jwt(patient_id="patient_oversized", device_id="dev_over")

    # Create oversized JPEG (> 10 MB)
    oversized_bytes = _create_dummy_jpeg_bytes(10 * 1024 * 1024 + 1024)
    resp = client.post(
        "/api/v1/client/cascade/intake/labs",
        headers={"Authorization": f"Bearer {token}"},
        files=[("files", ("huge_lab.jpg", io.BytesIO(oversized_bytes), "image/jpeg"))],
    )

    assert resp.status_code == 400
    assert "10 MB" in resp.text
    assert len(recorded_calls) == 0  # Zero Gemini calls


def test_05_client_lab_intake_unsupported_format(client_with_mock_gateway):
    """5. Unsupported format: plain text, forged extension, and executable content rejected with 400."""
    client, recorded_calls = client_with_mock_gateway()
    token = _build_test_device_jwt(patient_id="patient_unsupported", device_id="dev_uns")

    # 1. Plain text file with .jpg extension
    fake_jpg = b"This is a text file claiming to be a lab scan."
    resp1 = client.post(
        "/api/v1/client/cascade/intake/labs",
        headers={"Authorization": f"Bearer {token}"},
        files=[("files", ("fake.jpg", io.BytesIO(fake_jpg), "image/jpeg"))],
    )
    assert resp1.status_code == 400
    assert "unsupported format" in resp1.text or "invalid image content" in resp1.text

    # 2. PDF file with .png extension (magic %PDF)
    fake_png = b"%PDF-1.4\nSome pdf content here..."
    resp2 = client.post(
        "/api/v1/client/cascade/intake/labs",
        headers={"Authorization": f"Bearer {token}"},
        files=[("files", ("fake.png", io.BytesIO(fake_png), "image/png"))],
    )
    assert resp2.status_code == 400
    assert "unsupported format" in resp2.text or "invalid image content" in resp2.text

    # 3. Empty file (0 bytes)
    resp3 = client.post(
        "/api/v1/client/cascade/intake/labs",
        headers={"Authorization": f"Bearer {token}"},
        files=[("files", ("empty.jpg", io.BytesIO(b""), "image/jpeg"))],
    )
    assert resp3.status_code == 400
    assert "empty" in resp3.text

    assert len(recorded_calls) == 0


def test_06_client_lab_intake_duplicate_upload(client_with_mock_gateway):
    """6. Idempotency & deduplication: exact re-upload returns 200 without second Gemini dispatch."""
    client, recorded_calls = client_with_mock_gateway()
    token = _build_test_device_jwt(patient_id="patient_dedup", device_id="dev_dedup")

    jpeg_bytes = _create_dummy_jpeg_bytes(1024)

    # First upload
    resp1 = client.post(
        "/api/v1/client/cascade/intake/labs",
        headers={"Authorization": f"Bearer {token}"},
        files=[("files", ("lab.jpg", io.BytesIO(jpeg_bytes), "image/jpeg"))],
    )
    assert resp1.status_code == 200
    assert len(recorded_calls) == 1
    ledger_v1 = resp1.json()["ledger_version"]

    # Second upload of the EXACT same document
    resp2 = client.post(
        "/api/v1/client/cascade/intake/labs",
        headers={"Authorization": f"Bearer {token}"},
        files=[("files", ("lab.jpg", io.BytesIO(jpeg_bytes), "image/jpeg"))],
    )
    assert resp2.status_code == 200
    # Gemini must NOT be called again
    assert len(recorded_calls) == 1
    # Ledger version remains identical (no spurious mutation)
    assert resp2.json()["ledger_version"] == ledger_v1


def test_07_client_lab_intake_preserves_lab_history(client_with_mock_gateway):
    """7. Versioned ledger preservation: subsequent study appends to history without wiping previous entries."""
    # Study 1: Cholesterol on 2026-08-01
    client, _ = client_with_mock_gateway(_sample_vision_ocr_output(
        sample_date="2026-08-01",
        markers=[{
            "original_name": "Холестерин общий",
            "marker_key": "cholesterol_total",
            "raw_value": "5.8",
            "numeric_value": 5.8,
            "unit": "ммоль/л",
            "reference_range": {"low": 3.0, "high": 5.2, "text": "3.0 - 5.2"},
            "sample_date": "2026-08-01",
            "category": "lipids",
            "is_uncertain": False,
            "uncertainty_note": None,
            "is_critical_flag": False,
            "requires_user_verification": False,
            "is_recognition_ambiguous": False,
        }],
    ))
    token = _build_test_device_jwt(patient_id="patient_hist", device_id="dev_hist")

    resp1 = client.post(
        "/api/v1/client/cascade/intake/labs",
        headers={"Authorization": f"Bearer {token}"},
        files=[("files", ("study1.jpg", io.BytesIO(_create_dummy_jpeg_bytes(100)), "image/jpeg"))],
    )
    assert resp1.status_code == 200
    assert resp1.json()["ledger_version"] == 1

    # Study 2: Ferritin on 2026-10-01 (different image bytes)
    client2, _ = client_with_mock_gateway(_sample_vision_ocr_output(
        sample_date="2026-10-01",
        markers=[{
            "original_name": "Ферритин",
            "marker_key": "ferritin",
            "raw_value": "45.0",
            "numeric_value": 45.0,
            "unit": "мкг/л",
            "reference_range": {"low": 20.0, "high": 250.0, "text": "20 - 250"},
            "sample_date": "2026-10-01",
            "category": "iron",
            "is_uncertain": False,
            "uncertainty_note": None,
            "is_critical_flag": False,
            "requires_user_verification": False,
            "is_recognition_ambiguous": False,
        }],
    ))

    resp2 = client2.post(
        "/api/v1/client/cascade/intake/labs",
        headers={"Authorization": f"Bearer {token}"},
        files=[("files", ("study2.png", io.BytesIO(_create_dummy_png_bytes(200)), "image/png"))],
    )
    assert resp2.status_code == 200
    assert resp2.json()["ledger_version"] == 2

    # Query ledger state
    state = client2.get(
        "/api/v1/client/labs/state",
        headers={"Authorization": f"Bearer {token}"},
    ).json()

    assert state["ledger_version"] == 2
    # Both markers are active
    assert "cholesterol_total" in state["active_markers"]
    assert "ferritin" in state["active_markers"]
    # History contains observations from BOTH studies
    assert len(state["observations_history"]) >= 2
    history_keys = {obs["marker_key"] for obs in state["observations_history"]}
    assert "cholesterol_total" in history_keys
    assert "ferritin" in history_keys


def test_08_client_lab_intake_preserves_dates_units_references(client_with_mock_gateway):
    """8. Accurate preservation: test dates, units, reference intervals, and source metadata preserved."""
    client, _ = client_with_mock_gateway(_sample_vision_ocr_output(
        sample_date="2026-09-12",
        lab_name="Helix Diagnostic Center",
        markers=[{
            "original_name": "С-реактивный белок (СРБ)",
            "marker_key": "crp",
            "raw_value": "1.2",
            "numeric_value": 1.2,
            "unit": "мг/л",
            "reference_range": {"low": 0.0, "high": 5.0, "text": "0 - 5"},
            "sample_date": "2026-09-12",
            "category": "inflammation",
            "is_uncertain": False,
            "uncertainty_note": None,
            "is_critical_flag": False,
            "requires_user_verification": False,
            "is_recognition_ambiguous": False,
        }],
    ))
    token = _build_test_device_jwt(patient_id="patient_meta", device_id="dev_meta")

    resp = client.post(
        "/api/v1/client/cascade/intake/labs",
        headers={"Authorization": f"Bearer {token}"},
        files=[("files", ("crp.jpg", io.BytesIO(_create_dummy_jpeg_bytes(512)), "image/jpeg"))],
    )
    assert resp.status_code == 200

    state = client.get(
        "/api/v1/client/labs/state",
        headers={"Authorization": f"Bearer {token}"},
    ).json()

    crp = state["active_markers"]["crp"]
    assert crp["sample_date"] == "2026-09-12"
    assert crp["unit"] == "мг/л"
    assert crp["numeric_value"] == 1.2
    assert crp["reference_range"]["low"] == 0.0
    assert crp["reference_range"]["high"] == 5.0
    assert crp["reference_range"]["text"] == "0 - 5"


def test_09_client_lab_intake_uncertain_or_incomplete_ocr(client_with_mock_gateway):
    """9. Low-confidence safety: uncertain markers are separated and NOT automatically confirmed in active ledger."""
    client, _ = client_with_mock_gateway(_sample_vision_ocr_output(
        sample_date="2026-10-15",
        markers=[
            {
                "original_name": "Глюкоза",
                "marker_key": "glucose",
                "raw_value": "5.4",
                "numeric_value": 5.4,
                "unit": "ммоль/л",
                "reference_range": {"low": 4.1, "high": 6.1, "text": "4.1 - 6.1"},
                "sample_date": "2026-10-15",
                "category": "biochemistry",
                "is_uncertain": False,
                "uncertainty_note": None,
                "is_critical_flag": False,
                "requires_user_verification": False,
                "is_recognition_ambiguous": False,
            },
            {
                "original_name": "Креатинин",
                "marker_key": "creatinine",
                "raw_value": "?88?",
                "numeric_value": None,
                "unit": "мкмоль/л",
                "reference_range": {"low": 62.0, "high": 106.0, "text": "62 - 106"},
                "sample_date": "2026-10-15",
                "category": "biochemistry",
                "is_uncertain": True,
                "uncertainty_note": "Smudged digit on paper printout",
                "is_critical_flag": False,
                "requires_user_verification": True,
                "is_recognition_ambiguous": True,
            },
        ],
    ))
    token = _build_test_device_jwt(patient_id="patient_unconfirmed", device_id="dev_unc")

    resp = client.post(
        "/api/v1/client/cascade/intake/labs",
        headers={"Authorization": f"Bearer {token}"},
        files=[("files", ("lab.jpg", io.BytesIO(_create_dummy_jpeg_bytes(512)), "image/jpeg"))],
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["extracted_count"] == 2
    assert data["requires_verification_count"] == 1
    assert len(data["confirmed_markers"]) == 1
    assert data["confirmed_markers"][0]["marker_key"] == "glucose"
    assert len(data["unconfirmed_markers"]) == 1
    assert data["unconfirmed_markers"][0]["marker_key"] == "creatinine"

    # Ledger state: ONLY glucose is active! Creatinine is NOT confirmed!
    state = client.get(
        "/api/v1/client/labs/state",
        headers={"Authorization": f"Bearer {token}"},
    ).json()
    assert "glucose" in state["active_markers"]
    assert "creatinine" not in state["active_markers"]


def test_10_client_lab_intake_failure_preserves_previous_records(client_with_mock_gateway):
    """10. Failure atomicity: failed second intake preserves previous ledger version and observations."""
    # Step 1: Successful initial intake
    client, _ = client_with_mock_gateway(_sample_vision_ocr_output(
        sample_date="2026-09-01",
        markers=[{
            "original_name": "АЛТ",
            "marker_key": "alt",
            "raw_value": "25.0",
            "numeric_value": 25.0,
            "unit": "Ед/л",
            "reference_range": {"low": 0.0, "high": 41.0, "text": "0 - 41"},
            "sample_date": "2026-09-01",
            "category": "biochemistry",
            "is_uncertain": False,
            "uncertainty_note": None,
            "is_critical_flag": False,
            "requires_user_verification": False,
            "is_recognition_ambiguous": False,
        }],
    ))
    token = _build_test_device_jwt(patient_id="patient_fail_safe", device_id="dev_fs")

    resp1 = client.post(
        "/api/v1/client/cascade/intake/labs",
        headers={"Authorization": f"Bearer {token}"},
        files=[("files", ("lab1.jpg", io.BytesIO(_create_dummy_jpeg_bytes(100)), "image/jpeg"))],
    )
    assert resp1.status_code == 200
    assert resp1.json()["ledger_version"] == 1

    # Step 2: Next intake fails due to Gemini 502/503 Gateway Error
    client_fail, _ = client_with_mock_gateway(fail_with_status=503)
    resp2 = client_fail.post(
        "/api/v1/client/cascade/intake/labs",
        headers={"Authorization": f"Bearer {token}"},
        files=[("files", ("lab2.png", io.BytesIO(_create_dummy_png_bytes(200)), "image/png"))],
    )
    assert resp2.status_code == 502

    # Step 3: Ledger state is completely intact at version 1
    state = client.get(
        "/api/v1/client/labs/state",
        headers={"Authorization": f"Bearer {token}"},
    ).json()
    assert state["ledger_version"] == 1
    assert "alt" in state["active_markers"]
    assert state["active_markers"]["alt"]["value"] == "25.0"


def test_11_client_lab_intake_alias_and_single_file_field(client_with_mock_gateway):
    """11. Route & field aliases: POST /api/v1/client/labs/upload with field 'file' works seamlessly."""
    client, recorded_calls = client_with_mock_gateway()
    token = _build_test_device_jwt(patient_id="patient_alias", device_id="dev_alias")

    jpeg_bytes = _create_dummy_jpeg_bytes(512)
    # Using alias route '/api/v1/client/labs/upload' and single-file field name 'file'
    resp = client.post(
        "/api/v1/client/labs/upload",
        headers={"Authorization": f"Bearer {token}"},
        files=[("file", ("single_page.jpg", io.BytesIO(jpeg_bytes), "image/jpeg"))],
    )

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["status"] == "processed"
    assert data["extracted_count"] == 3
    assert len(recorded_calls) == 1


@pytest.mark.anyio
async def test_12_client_lab_intake_concurrent_duplicate_upload(client_with_mock_gateway):
    """12. Concurrent duplicate upload safety:
    Two simultaneous requests from the same patient with identical document bytes.
    - Exactly ONE Gemini OCR call occurs (no double AI dispatch).
    - Exactly ONE version increment occurs in the ledger.
    - Both HTTP requests succeed cleanly (HTTP 200).
    - Cross-patient isolation: a third concurrent upload for another patient is isolated into its own cycle.
    """
    client, recorded_calls = client_with_mock_gateway(delay_seconds=0.05)
    token_conc = _build_test_device_jwt(patient_id="patient_conc", device_id="dev_conc")
    token_other = _build_test_device_jwt(patient_id="patient_other", device_id="dev_other")

    jpeg_bytes = _create_dummy_jpeg_bytes(1024)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as ac:
        req1 = ac.post(
            "/api/v1/client/cascade/intake/labs",
            headers={"Authorization": f"Bearer {token_conc}"},
            files=[("files", ("lab_1.jpg", io.BytesIO(jpeg_bytes), "image/jpeg"))],
        )
        req2 = ac.post(
            "/api/v1/client/cascade/intake/labs",
            headers={"Authorization": f"Bearer {token_conc}"},
            files=[("files", ("lab_2.jpg", io.BytesIO(jpeg_bytes), "image/jpeg"))],
        )
        # Simultaneously launch request for Patient Other with the same file
        req_other = ac.post(
            "/api/v1/client/cascade/intake/labs",
            headers={"Authorization": f"Bearer {token_other}"},
            files=[("files", ("lab_other.jpg", io.BytesIO(jpeg_bytes), "image/jpeg"))],
        )

        resp1, resp2, resp_other = await asyncio.gather(req1, req2, req_other)

    assert resp1.status_code == 200, resp1.text
    assert resp2.status_code == 200, resp2.text
    assert resp_other.status_code == 200, resp_other.text

    d1 = resp1.json()
    d2 = resp2.json()
    d_other = resp_other.json()

    # Patient conc invariants:
    # 1. Both concurrent requests resolve to the same cycle
    assert d1["cycle_id"] == d2["cycle_id"]
    # 2. Both report the same ledger version (v1, no double bump)
    assert d1["ledger_version"] == 1
    assert d2["ledger_version"] == 1
    assert d1["status"] == "processed"
    assert d2["status"] == "processed"

    # Patient other invariants:
    # 3. Patient other cycle is completely isolated from patient conc
    assert d_other["cycle_id"] != d1["cycle_id"]
    assert d_other["ledger_version"] == 1

    # 4. Total Gemini calls: exactly 1 for patient conc + 1 for patient other = 2 total
    # (patient conc's duplicate was deduped and did NOT call Gemini a second time)
    assert len(recorded_calls) == 2
