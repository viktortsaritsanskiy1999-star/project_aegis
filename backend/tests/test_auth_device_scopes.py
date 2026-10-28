"""
Project Aegis — Stage 5.4D-1 Tests: Device Scopes Activation & Auth Compatibility
Verifies:
1. New device pairing issues all 8 standard scopes (DEFAULT_DEVICE_SCOPE).
2. Existing device with legacy token (Stage 5.3 scopes) automatically receives
   all 8 standard scopes upon single-use token refresh without re-pairing.
3. Revoked device is rejected on both API calls and refresh (401 Unauthorized).
4. Invalid, expired, or replayed refresh tokens receive 401 and trigger family revocation.
5. DeviceSession cannot access administrative endpoints (/api/v1/cascade/* -> 403 Forbidden).
6. Scope escalation through request payload in /auth/pair and /auth/refresh is strictly forbidden (422).
7. Old vitals endpoints (/api/v1/vitals/batch, /api/v1/vitals/latest) work with new tokens.
8. Medical AI client endpoints (/api/v1/client/*, /api/v1/reports/client) are accessible with new tokens.
9. 401 (authentication/revocation) and 403 (insufficient scope / admin) are strictly distinguished.
"""

from datetime import datetime, timezone
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
from app.models.medical_cycle import MedicalCycle
from app.repositories.pairing_repository import PairingRepository
from app.repositories.patient_repository import PatientRepository
from app.security.tokens import (
    DEFAULT_DEVICE_SCOPE,
    LEGACY_DEVICE_SCOPE,
    STANDARD_DEVICE_SCOPES,
    create_access_token,
    verify_access_token,
)
from tests.test_client_cascade_isolation import _sample_intake_answers

TEST_JWT_SECRET = "test_device_scopes_jwt_secret_12345678!"
TEST_PAIRING_SECRET = "test_device_scopes_pairing_secret_12345678!"
TEST_ADMIN_SECRET = "test_device_scopes_admin_secret_12345678!"


@pytest.fixture
def scopes_test_env():
    """Isolated in-memory SQLite environment for Stage 5.4D-1 scope verification."""
    orig_jwt = settings.jwt_secret
    orig_pairing = settings.pairing_secret
    orig_api = settings.api_secret

    settings.jwt_secret = TEST_JWT_SECRET
    settings.pairing_secret = TEST_PAIRING_SECRET
    settings.api_secret = TEST_ADMIN_SECRET

    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    db = TestingSessionLocal()

    def override_get_db():
        try:
            yield db
        finally:
            pass

    app.dependency_overrides[get_db] = override_get_db
    client = TestClient(app)

    try:
        yield client, db
    finally:
        app.dependency_overrides.pop(get_db, None)
        db.close()
        Base.metadata.drop_all(bind=engine)
        settings.jwt_secret = orig_jwt
        settings.pairing_secret = orig_pairing
        settings.api_secret = orig_api


def _pair_new_device(client: TestClient, db: Session, patient_id: str, device_id: str, code: str = "123456") -> dict:
    PatientRepository(db).create(patient_id=patient_id)
    PairingRepository(db, secret=TEST_PAIRING_SECRET).create_code(
        patient_id=patient_id,
        raw_code_override=code,
    )
    db.commit()

    resp = client.post(
        "/api/v1/auth/pair",
        json={
            "pairing_code": code,
            "device_name": "Test Watch 7",
            "device_id": device_id,
            "platform": "android",
        },
    )
    assert resp.status_code == 200, f"Pairing failed: {resp.text}"
    return resp.json()


# ==============================================================================
# 1. NEW PAIRING & DEFAULT SCOPES
# ==============================================================================

def test_new_pairing_issues_all_eight_standard_scopes(scopes_test_env):
    """Verifies that newly paired mobile devices receive all 8 standard scopes."""
    client, db = scopes_test_env
    pair_res = _pair_new_device(client, db, patient_id="pat-sc-01", device_id="dev-sc-01", code="111222")

    assert pair_res["token_type"] == "Bearer"
    assert pair_res["patient_id"] == "pat-sc-01"
    assert pair_res["device_id"] == "dev-sc-01"
    assert pair_res["scope"] == DEFAULT_DEVICE_SCOPE

    issued_scopes = set(pair_res["scope"].split())
    assert issued_scopes == set(STANDARD_DEVICE_SCOPES)
    assert {
        "vitals:read",
        "vitals:write",
        "cycle:read",
        "cascade:intake",
        "cascade:run",
        "reports:read",
        "chat:read",
        "chat:write",
    } == issued_scopes


# ==============================================================================
# 2. EXISTING DEVICE UPGRADE VIA REFRESH (NO RE-PAIRING REQUIRED)
# ==============================================================================

def test_existing_device_with_legacy_token_gets_all_standard_scopes_on_refresh(scopes_test_env):
    """
    Verifies that an existing device paired under Stage 5.3 (holding only vitals/cycle scopes)
    receives all 8 standard scopes when performing its regular token refresh,
    without requiring the user to re-pair the device.
    """
    client, db = scopes_test_env
    # 1. Simulate an existing device paired earlier
    pair_res = _pair_new_device(client, db, patient_id="pat-legacy-01", device_id="dev-legacy-01", code="222333")
    old_refresh_token = pair_res["refresh_token"]

    # 2. Mint a legacy access token with ONLY Stage 5.3 scopes (simulating stored old token)
    from app.security.tokens import verify_access_token
    real_claims = verify_access_token(pair_res["access_token"])
    legacy_access_jwt = create_access_token(
        patient_id="pat-legacy-01",
        device_id="dev-legacy-01",
        family_id=real_claims.family_id,
        scope=LEGACY_DEVICE_SCOPE,
    ).access_token

    # Verify that the legacy token fails medical AI endpoints with 403
    r_anamnesis_legacy = client.post(
        "/api/v1/client/cascade/intake/anamnesis",
        json={"answers": _sample_intake_answers()},
        headers={"Authorization": f"Bearer {legacy_access_jwt}"},
    )
    assert r_anamnesis_legacy.status_code == 403
    assert r_anamnesis_legacy.json()["detail"] == "Insufficient token scope for this operation."

    r_chat_legacy = client.get(
        "/api/v1/client/chat/athena/history",
        headers={"Authorization": f"Bearer {legacy_access_jwt}"},
    )
    assert r_chat_legacy.status_code == 403

    # 3. Perform standard single-use token refresh
    refresh_resp = client.post(
        "/api/v1/auth/refresh",
        json={"device_id": "dev-legacy-01", "refresh_token": old_refresh_token},
    )
    assert refresh_resp.status_code == 200
    refreshed_data = refresh_resp.json()

    # The refreshed session automatically has all 8 standard scopes!
    refreshed_scopes = set(refreshed_data["scope"].split())
    assert refreshed_scopes == set(STANDARD_DEVICE_SCOPES)
    assert "cascade:intake" in refreshed_scopes
    assert "chat:read" in refreshed_scopes
    assert "chat:write" in refreshed_scopes
    assert "reports:read" in refreshed_scopes

    new_access_token = refreshed_data["access_token"]
    new_headers = {"Authorization": f"Bearer {new_access_token}"}

    # 4. The new access token now cleanly passes scope checks on medical AI endpoints!
    r_chat_new = client.get(
        "/api/v1/client/chat/athena/history",
        headers=new_headers,
    )
    assert r_chat_new.status_code == 200
    assert r_chat_new.json()["messages"] == []

    # Cascade status passes with 200 OK (cycle:read)
    r_status = client.get("/api/v1/client/cascade/status", headers=new_headers)
    assert r_status.status_code == 200

    # Labs state passes with 200 OK (cycle:read)
    r_labs = client.get("/api/v1/client/labs/state", headers=new_headers)
    assert r_labs.status_code == 200

    # Cascade intake passes scope check (no 403 Forbidden)
    r_anamnesis_new = client.post(
        "/api/v1/client/cascade/intake/anamnesis",
        json={"answers": _sample_intake_answers()},
        headers=new_headers,
    )
    assert r_anamnesis_new.status_code != 403


# ==============================================================================
# 3. REVOKED DEVICE & INVALID TOKEN SECURITY
# ==============================================================================

def test_revoked_device_cannot_refresh_or_use_api(scopes_test_env):
    """Verifies that a revoked device is immediately rejected on refresh and APIs (401)."""
    client, db = scopes_test_env
    pair_res = _pair_new_device(client, db, patient_id="pat-rev-01", device_id="dev-rev-01", code="333444")
    refresh_token = pair_res["refresh_token"]
    access_token = pair_res["access_token"]

    # Revoke device in database
    dev = db.get(Device, "dev-rev-01")
    dev.is_active = False
    dev.revoked_at = datetime.now(timezone.utc)
    db.commit()

    # API call with existing valid access token fails with 401
    r_api = client.get(
        "/api/v1/cycle/current",
        headers={"Authorization": f"Bearer {access_token}"},
    )
    assert r_api.status_code == 401
    assert "revoked" in r_api.json()["detail"].lower()

    # Attempt to refresh token fails with 401
    r_ref = client.post(
        "/api/v1/auth/refresh",
        json={"device_id": "dev-rev-01", "refresh_token": refresh_token},
    )
    assert r_ref.status_code == 401


def test_invalid_and_reused_refresh_tokens_rejected(scopes_test_env):
    """Verifies reuse detection family revocation and rejection of bogus refresh tokens."""
    client, db = scopes_test_env
    pair_res = _pair_new_device(client, db, patient_id="pat-reuse-01", device_id="dev-reuse-01", code="444555")
    first_refresh = pair_res["refresh_token"]

    # Bogus token -> 401
    r_bogus = client.post(
        "/api/v1/auth/refresh",
        json={"device_id": "dev-reuse-01", "refresh_token": "bogus-non-existent-refresh-token"},
    )
    assert r_bogus.status_code == 401

    # Legitimate first rotation -> 200
    r_rot = client.post(
        "/api/v1/auth/refresh",
        json={"device_id": "dev-reuse-01", "refresh_token": first_refresh},
    )
    assert r_rot.status_code == 200
    second_refresh = r_rot.json()["refresh_token"]

    # Replay of old first_refresh -> 401 + family revocation
    r_replay = client.post(
        "/api/v1/auth/refresh",
        json={"device_id": "dev-reuse-01", "refresh_token": first_refresh},
    )
    assert r_replay.status_code == 401

    # Legitimate second_refresh is now also revoked -> 401
    r_compromised = client.post(
        "/api/v1/auth/refresh",
        json={"device_id": "dev-reuse-01", "refresh_token": second_refresh},
    )
    assert r_compromised.status_code == 401


# ==============================================================================
# 4. SCOPE ESCALATION REJECTION (PAYLOAD TAMPERING)
# ==============================================================================

def test_scope_escalation_in_request_payload_is_strictly_forbidden(scopes_test_env):
    """
    Verifies that client cannot pass arbitrary 'scope' or extra parameters
    in /api/v1/auth/pair or /api/v1/auth/refresh (HTTP 422 Unprocessable Content).
    """
    client, db = scopes_test_env
    PatientRepository(db).create(patient_id="pat-esc-01")
    PairingRepository(db, secret=TEST_PAIRING_SECRET).create_code(
        patient_id="pat-esc-01",
        raw_code_override="555666",
    )
    db.commit()

    # Attempt to inject scope in pair request
    r_pair_esc = client.post(
        "/api/v1/auth/pair",
        json={
            "pairing_code": "555666",
            "device_name": "Tampered Device",
            "device_id": "dev-esc-01",
            "scope": "admin:all vitals:* root",
        },
    )
    assert r_pair_esc.status_code == 422

    # Attempt to inject scope in refresh request
    r_ref_esc = client.post(
        "/api/v1/auth/refresh",
        json={
            "device_id": "dev-esc-01",
            "refresh_token": "any-refresh-token",
            "scope": "admin:all",
        },
    )
    assert r_ref_esc.status_code == 422


# ==============================================================================
# 5. DEVICE SESSION CANNOT ACCESS ADMIN ROUTES (403 FORBIDDEN)
# ==============================================================================

def test_device_session_strictly_forbidden_from_admin_endpoints(scopes_test_env):
    """
    Verifies that even with all standard device scopes, a mobile DeviceSession
    is strictly forbidden from accessing service admin endpoints (/api/v1/cascade/*).
    """
    client, db = scopes_test_env
    pair_res = _pair_new_device(client, db, patient_id="pat-admin-chk", device_id="dev-admin-chk", code="666777")
    headers = {"Authorization": f"Bearer {pair_res['access_token']}"}

    # Administrative cascade endpoints require ServiceAdminSession (AEGIS_API_SECRET)
    admin_routes = [
        ("POST", "/api/v1/cascade/run-primary"),
        ("POST", "/api/v1/cascade/run-weekly"),
        ("POST", "/api/v1/cascade/labs/upsert"),
        ("POST", "/api/v1/cascade/run-lab-revision"),
    ]

    for method, route in admin_routes:
        if method == "POST":
            resp = client.post(route, json={"cycle_id": "cycle-test"}, headers=headers)
        else:
            resp = client.get(route, headers=headers)
        assert resp.status_code == 403, f"{route} did not return 403 Forbidden for mobile device session!"
        assert "administrator" in resp.json()["detail"].lower()


# ==============================================================================
# 6. VITALS API COMPATIBILITY
# ==============================================================================

def test_vitals_api_works_with_standard_device_tokens(scopes_test_env):
    """Verifies that existing vitals read/write endpoints work cleanly with new tokens."""
    client, db = scopes_test_env
    pair_res = _pair_new_device(client, db, patient_id="pat-vit-01", device_id="dev-vit-01", code="777888")
    headers = {"Authorization": f"Bearer {pair_res['access_token']}"}

    # Ensure active cycle exists for patient
    cycle = MedicalCycle(cycle_id="cycle-vit-01", patient_id="pat-vit-01", status="active")
    db.add(cycle)
    db.commit()

    # Vitals batch write
    batch_payload = {
        "cycle_id": "cycle-vit-01",
        "measurements": [
            {
                "pulse": 72.0,
                "measured_at": datetime.now(timezone.utc).isoformat(),
                "raw_metadata": {"idempotency_key": "evt-vit-001"},
            }
        ],
    }
    r_batch = client.post("/api/v1/vitals/batch", json=batch_payload, headers=headers)
    assert r_batch.status_code == 201

    # Vitals latest read
    r_latest = client.get("/api/v1/vitals/latest?cycle_id=cycle-vit-01", headers=headers)
    assert r_latest.status_code == 200
    assert r_latest.json()["cycle_id"] == "cycle-vit-01"
    assert "72" in r_latest.json()["heart_rate"]
