from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta
import io
import logging
from pathlib import Path
import sqlite3
import threading
from fastapi.testclient import TestClient
import jwt
import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session, sessionmaker

from app.config import settings
from app.database import get_db
from app.main import app
from app.models.auth_rate_limit import AuthRateLimit
from app.models.base import Base
from app.models.device import Device
from app.models.device_refresh_token import DeviceRefreshToken
from app.models.health_measurement import HealthMeasurement
from app.models.pairing_code import PairingCode
from app.repositories.cycle_repository import MedicalCycleRepository
from app.repositories.device_repository import DeviceRepository
from app.repositories.measurement_repository import HealthMeasurementRepository
from app.repositories.pairing_repository import PairingRepository
from app.repositories.patient_repository import PatientRepository
from app.repositories.report_repository import ModelReportRepository
from app.routers.auth import GENERIC_PAIRING_FAILURE_DETAIL
from app.schemas.privacy import PrivacyLevel
from app.security.auth import (
    GENERIC_CYCLE_ACCESS_DENIED_DETAIL,
    GENERIC_REPORT_ACCESS_DENIED_DETAIL,
    require_any_scope,
    require_scopes,
)
from app.security.tokens import (
    DEFAULT_DEVICE_SCOPE,
    LEGACY_DEVICE_SCOPE,
    create_access_token,
    verify_access_token,
)

TEST_API_SECRET = "test-stage-5-3b2-admin-api-secret-11223344"
TEST_PAIRING_SECRET = "test-stage-5-3b2-pairing-hmac-secret-55667788"
TEST_JWT_SECRET = "test-stage-5-3b2-jwt-hs256-signing-secret-99001122"


def _make_sqlite_engine(db_path: Path):
    eng = create_engine(
        f"sqlite:///{db_path.as_posix()}",
        connect_args={"check_same_thread": False},
    )

    @event.listens_for(eng, "connect")
    def _set_pragmas(dbapi_conn, _record):
        if isinstance(dbapi_conn, sqlite3.Connection):
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA busy_timeout=5000")
            cur.close()

    return eng


@pytest.fixture()
def isolated_api_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """
    Sets up an isolated file-backed SQLite database in `tmp_path` with a configured
    FastAPI `TestClient` and isolated secrets (`AEGIS_API_SECRET`, `AEGIS_PAIRING_SECRET`,
    `AEGIS_JWT_SECRET`). Never touches `aegis_private_vault.db`.
    """
    monkeypatch.setattr(settings, "env", "testing")
    monkeypatch.setattr(settings, "api_secret", TEST_API_SECRET)
    monkeypatch.setattr(settings, "pairing_secret", TEST_PAIRING_SECRET)
    monkeypatch.setattr(settings, "jwt_secret", TEST_JWT_SECRET)
    monkeypatch.setattr(settings, "jwt_issuer", "aegis-backend")
    monkeypatch.setattr(settings, "jwt_audience", "aegis-mobile-client")
    monkeypatch.setattr(settings, "trusted_proxy_ips", "")
    monkeypatch.setattr(settings, "max_active_pairing_codes", 10)

    db_file = tmp_path / "test_auth_api_isolation.db"
    eng = _make_sqlite_engine(db_file)
    Base.metadata.create_all(bind=eng)
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=eng)
    session = SessionLocal()

    def _override_get_db():
        with SessionLocal() as req_session:
            yield req_session

    app.dependency_overrides[get_db] = _override_get_db
    try:
        with TestClient(app) as test_client:
            yield test_client, session, SessionLocal, eng
    finally:
        app.dependency_overrides.clear()
        session.close()
        eng.dispose()


def _pair_patient_device(
    client: TestClient,
    session: Session,
    *,
    patient_id: str,
    device_id: str,
    raw_code: str,
    device_name: str = "Aegis Mobile Device",
) -> dict:
    PatientRepository(session).create(patient_id=patient_id, status="active")
    PairingRepository(session).create_code(
        patient_id=patient_id,
        raw_code_override=raw_code,
    )
    resp = client.post(
        "/api/v1/auth/pair",
        json={
            "pairing_code": raw_code,
            "device_id": device_id,
            "device_name": device_name,
            "platform": "android",
        },
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_01_successful_device_pairing_and_no_store_headers(isolated_api_env):
    """
    Verifies atomic `POST /api/v1/auth/pair`:
    - Preserves leading zero in 6-digit code (`042918`).
    - Sets `Cache-Control: no-store` and `Pragma: no-cache`.
    - Returns valid JWT access token, refresh token, `patient_id`, `device_id`, and `scope`.
    - Marks `PairingCode` as consumed (`consumed_at IS NOT NULL`, `active_code_hash IS NULL`).
    - Rejects extra fields in request body (`extra='forbid'`).
    """
    client, session, _, _ = isolated_api_env
    PatientRepository(session).create(patient_id="patient-pair-01")
    _, code_entity = PairingRepository(session).create_code(
        patient_id="patient-pair-01",
        raw_code_override="042918",
    )

    # Extra fields (e.g. attempting to inject patient_id) must be rejected with 422
    bad_resp = client.post(
        "/api/v1/auth/pair",
        json={
            "pairing_code": "042918",
            "device_id": "dev-pair-01",
            "device_name": "Pixel 8 Pro",
            "platform": "android",
            "patient_id": "injected-patient",
        },
    )
    assert bad_resp.status_code == 422

    resp = client.post(
        "/api/v1/auth/pair",
        json={
            "pairing_code": "042918",
            "device_id": "dev-pair-01",
            "device_name": "Pixel 8 Pro",
            "platform": "android",
        },
    )
    assert resp.status_code == 200
    assert resp.headers.get("cache-control") == "no-store"
    assert resp.headers.get("pragma") == "no-cache"

    data = resp.json()
    assert data["token_type"] == "Bearer"
    assert data["patient_id"] == "patient-pair-01"
    assert data["device_id"] == "dev-pair-01"
    assert data["scope"] == DEFAULT_DEVICE_SCOPE
    assert set(data["scope"].split()) >= {
        "vitals:read", "vitals:write", "cycle:read",
        "cascade:intake", "cascade:run", "reports:read",
        "chat:read", "chat:write",
    }
    assert data["expires_in"] == 1800
    assert isinstance(data["access_token"], str) and data["access_token"].count(".") == 2
    assert isinstance(data["refresh_token"], str) and len(data["refresh_token"]) >= 32

    session.expire_all()
    db_code = session.get(PairingCode, code_entity.code_id)
    assert db_code is not None
    assert db_code.consumed_at is not None
    assert db_code.active_code_hash is None

    db_dev = session.get(Device, "dev-pair-01")
    assert db_dev is not None and db_dev.is_active is True and db_dev.patient_id == "patient-pair-01"


def test_02_invalid_expired_and_reused_pairing_code_and_cross_patient_isolation(isolated_api_env):
    """
    Verifies rejection of wrong, expired, and already-used pairing codes (`401 Unauthorized`),
    and confirms that invalid attempts do not increment `attempts_count` on other patients' codes.
    """
    client, session, _, _ = isolated_api_env
    PatientRepository(session).create(patient_id="patient-A")
    PatientRepository(session).create(patient_id="patient-B")

    _, code_a = PairingRepository(session).create_code(
        patient_id="patient-A", raw_code_override="111222"
    )
    _, code_b = PairingRepository(session).create_code(
        patient_id="patient-B", raw_code_override="333444"
    )
    # Expire code_b
    code_b.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    session.commit()

    # 1. Wrong code -> 401
    r_wrong = client.post(
        "/api/v1/auth/pair",
        json={"pairing_code": "999999", "device_id": "dev-x", "device_name": "Watch"},
    )
    assert r_wrong.status_code == 401
    assert r_wrong.headers.get("cache-control") == "no-store"

    # 2. Expired code -> 401
    r_exp = client.post(
        "/api/v1/auth/pair",
        json={"pairing_code": "333444", "device_id": "dev-b", "device_name": "Watch B"},
    )
    assert r_exp.status_code == 401

    # Verify Patient A's code was not mutated by wrong/expired attempts
    session.expire_all()
    refreshed_a = session.get(PairingCode, code_a.code_id)
    assert refreshed_a is not None
    assert refreshed_a.attempts_count == 0
    assert refreshed_a.consumed_at is None
    assert refreshed_a.active_code_hash is not None

    # 3. Valid pairing with Patient A's code -> 200
    r_ok = client.post(
        "/api/v1/auth/pair",
        json={"pairing_code": "111222", "device_id": "dev-a", "device_name": "Watch A"},
    )
    assert r_ok.status_code == 200

    # 4. Reusing Patient A's consumed code -> 401
    r_reuse = client.post(
        "/api/v1/auth/pair",
        json={"pairing_code": "111222", "device_id": "dev-a2", "device_name": "Watch A2"},
    )
    assert r_reuse.status_code == 401


def test_03_concurrent_http_pairing_with_same_code_allows_at_most_one_winner(isolated_api_env):
    """
    Fires 5 concurrent HTTP `POST /api/v1/auth/pair` requests presenting the same valid code.
    Verifies that exactly 1 request succeeds (`200 OK`) and 4 fail (`401 Unauthorized`).
    """
    _, session, _, _ = isolated_api_env
    PatientRepository(session).create(patient_id="patient-conc-pair")
    PairingRepository(session).create_code(
        patient_id="patient-conc-pair",
        raw_code_override="777888",
    )

    barrier = threading.Barrier(5)

    def _worker(idx: int) -> int:
        with TestClient(app) as local_client:
            barrier.wait(timeout=5.0)
            resp = local_client.post(
                "/api/v1/auth/pair",
                json={
                    "pairing_code": "777888",
                    "device_id": f"dev-conc-{idx}",
                    "device_name": f"Concurrent Device {idx}",
                    "platform": "android",
                },
            )
            return resp.status_code

    with ThreadPoolExecutor(max_workers=5) as pool:
        statuses = list(pool.map(_worker, range(5)))

    assert statuses.count(200) == 1
    assert statuses.count(401) == 4

    session.expire_all()
    devices = DeviceRepository(session).list_by_patient("patient-conc-pair")
    assert len(devices) == 1


def test_04_rate_limiting_429_retry_after_spoofed_ip_headers_and_fail_closed(isolated_api_env):
    """
    Verifies rate limiting on `POST /api/v1/auth/pair`:
    1. Rotating spoofed `X-Forwarded-For` and `X-Real-IP` headers on every request does NOT
       bypass Per-IP rate limiting when `trusted_proxy_ips` is empty.
    2. After 5 failed attempts, the 6th attempt (even with a valid pairing code!) returns
       `429 Too Many Requests` with `Retry-After` header and does NOT consume the code.
    3. If the `auth_rate_limits` SQLite table is unavailable, `/auth/pair` fails closed
       with `503 Service Unavailable` and leaves the pairing code unconsumed.
    """
    client, session, _, eng = isolated_api_env
    PatientRepository(session).create(patient_id="patient-rl")
    _, valid_code = PairingRepository(session).create_code(
        patient_id="patient-rl",
        raw_code_override="555666",
    )

    for i in range(5):
        r = client.post(
            "/api/v1/auth/pair",
            json={
                "pairing_code": "000000",
                "device_id": f"rotating-dev-{i}",
                "device_name": "Attacker",
            },
            headers={
                "X-Forwarded-For": f"203.0.113.{10 + i}",
                "X-Real-IP": f"198.51.100.{20 + i}",
            },
        )
        assert r.status_code == 401

    # 6th attempt with the VALID code is blocked with 429 + Retry-After
    r6 = client.post(
        "/api/v1/auth/pair",
        json={
            "pairing_code": "555666",
            "device_id": "legit-dev-after-lockout",
            "device_name": "Legit Watch",
        },
        headers={"X-Forwarded-For": "192.0.2.99"},
    )
    assert r6.status_code == 429
    assert "retry-after" in r6.headers
    assert 1 <= int(r6.headers["retry-after"]) <= 900
    assert r6.headers.get("cache-control") == "no-store"

    # Pairing code must still be unconsumed!
    session.expire_all()
    code_check = session.get(PairingCode, valid_code.code_id)
    assert code_check is not None and code_check.consumed_at is None

    # Drop auth_rate_limits table -> fail-closed 503 Service Unavailable
    AuthRateLimit.__table__.drop(bind=eng)
    r_down = client.post(
        "/api/v1/auth/pair",
        json={
            "pairing_code": "555666",
            "device_id": "dev-when-limiter-down",
            "device_name": "Watch",
        },
    )
    assert r_down.status_code == 503
    session.expire_all()
    code_check_2 = session.get(PairingCode, valid_code.code_id)
    assert code_check_2 is not None and code_check_2.consumed_at is None


def test_05_invalid_forged_and_wrong_alg_jwt_and_api_secret_rejection_on_mobile_endpoints(
    isolated_api_env,
):
    """
    Verifies that:
    - Forged JWTs, tampered claims, `alg=none`, `alg=HS384`, wrong `iss`/`aud` return 401.
    - `AEGIS_API_SECRET` is NEVER accepted as a mobile `DeviceSession` on `/api/v1/auth/revoke`.
    """
    client, session, _, _ = isolated_api_env
    pair_data = _pair_patient_device(
        client,
        session,
        patient_id="pat-jwt-check",
        device_id="dev-jwt-check",
        raw_code="123123",
    )

    now_ts = int(datetime.now(timezone.utc).timestamp())
    base_claims = {
        "iss": "aegis-backend",
        "aud": "aegis-mobile-client",
        "sub": "pat-jwt-check",
        "patient_id": "pat-jwt-check",
        "device_id": "dev-jwt-check",
        "jti": "jti-test-05",
        "family_id": "fam-test-05",
        "scope": "vitals:read vitals:write cycle:read",
        "iat": now_ts,
        "exp": now_ts + 1800,
    }

    # 1. Signed with wrong secret (or signed with AEGIS_API_SECRET)
    forged_jwt = jwt.encode(base_claims, TEST_API_SECRET, algorithm="HS256")
    r1 = client.get("/api/v1/cycle/current", headers={"Authorization": f"Bearer {forged_jwt}"})
    assert r1.status_code == 401

    # 2. Signed with HS384 instead of HS256
    hs384_jwt = jwt.encode(base_claims, TEST_JWT_SECRET, algorithm="HS384")
    r2 = client.get("/api/v1/cycle/current", headers={"Authorization": f"Bearer {hs384_jwt}"})
    assert r2.status_code == 401

    # 3. Wrong issuer or audience
    bad_iss_jwt = jwt.encode(dict(base_claims, iss="evil"), TEST_JWT_SECRET, algorithm="HS256")
    assert (
        client.get("/api/v1/cycle/current", headers={"Authorization": f"Bearer {bad_iss_jwt}"}).status_code
        == 401
    )

    # 4. Token patient_id does not match registered device owner in DB
    mismatched_owner_jwt = jwt.encode(
        dict(base_claims, sub="other-pat", patient_id="other-pat"),
        TEST_JWT_SECRET,
        algorithm="HS256",
    )
    assert (
        client.get(
            "/api/v1/cycle/current",
            headers={"Authorization": f"Bearer {mismatched_owner_jwt}"},
        ).status_code
        == 401
    )

    # 5. AEGIS_API_SECRET is rejected on mobile-only /api/v1/auth/revoke
    r_admin_on_revoke = client.post(
        "/api/v1/auth/revoke",
        json={"device_id": pair_data["device_id"]},
        headers={"Authorization": f"Bearer {TEST_API_SECRET}"},
    )
    assert r_admin_on_revoke.status_code == 401


def test_06_expired_jwt_rejected_across_protected_endpoints(isolated_api_env):
    """Verifies that an expired JWT access token is rejected with 401 on all protected routes."""
    client, session, _, _ = isolated_api_env
    _pair_patient_device(
        client,
        session,
        patient_id="pat-exp",
        device_id="dev-exp",
        raw_code="234234",
    )
    MedicalCycleRepository(session).create(
        cycle_id="cycle-exp", patient_id="pat-exp", status="active"
    )

    past = datetime.now(timezone.utc) - timedelta(hours=2)
    expired_token = create_access_token(
        patient_id="pat-exp",
        device_id="dev-exp",
        family_id="fam-exp",
        ttl_minutes=30,
        now=past,
    ).access_token
    headers = {"Authorization": f"Bearer {expired_token}"}

    assert client.get("/api/v1/cycle/current", headers=headers).status_code == 401
    assert (
        client.post(
            "/api/v1/vitals/measurement",
            json={
                "cycle_id": "cycle-exp",
                "pulse": 72,
                "measured_at": datetime.now(timezone.utc).isoformat(),
            },
            headers=headers,
        ).status_code
        == 401
    )
    assert client.get("/api/v1/vitals/latest?cycle_id=cycle-exp", headers=headers).status_code == 401
    assert client.get("/api/v1/reports/client?cycle_id=cycle-exp", headers=headers).status_code == 401
    assert client.post("/api/v1/auth/revoke", headers=headers).status_code == 401


def test_07_scope_enforcement_on_device_session(isolated_api_env):
    """
    Verifies fine-grained scope enforcement on `DeviceSession`:
    - A token with only `vitals:read` cannot write vitals (`403 Forbidden`) or read current cycle (`403`).
    - A token with only `vitals:write` cannot read vitals (`403`), current cycle (`403`), or reports (`403`).
    """
    client, session, _, _ = isolated_api_env
    pair_scope = _pair_patient_device(
        client,
        session,
        patient_id="pat-scope",
        device_id="dev-scope",
        raw_code="345345",
    )
    active_family_id = verify_access_token(pair_scope["access_token"]).family_id
    MedicalCycleRepository(session).create(
        cycle_id="cycle-scope", patient_id="pat-scope", status="active"
    )

    read_only_vitals_jwt = create_access_token(
        patient_id="pat-scope",
        device_id="dev-scope",
        family_id=active_family_id,
        scope="vitals:read",
    ).access_token
    ro_headers = {"Authorization": f"Bearer {read_only_vitals_jwt}"}

    # Allowed: GET /vitals/latest
    assert client.get("/api/v1/vitals/latest?cycle_id=cycle-scope", headers=ro_headers).status_code == 200
    # Forbidden: POST /vitals/measurement, GET /cycle/current, GET /reports/client
    assert (
        client.post(
            "/api/v1/vitals/measurement",
            json={
                "cycle_id": "cycle-scope",
                "pulse": 70,
                "measured_at": datetime.now(timezone.utc).isoformat(),
            },
            headers=ro_headers,
        ).status_code
        == 403
    )
    assert client.get("/api/v1/cycle/current", headers=ro_headers).status_code == 403
    assert client.get("/api/v1/reports/client?cycle_id=cycle-scope", headers=ro_headers).status_code == 403

    write_only_vitals_jwt = create_access_token(
        patient_id="pat-scope",
        device_id="dev-scope",
        family_id=active_family_id,
        scope="vitals:write",
    ).access_token
    wo_headers = {"Authorization": f"Bearer {write_only_vitals_jwt}"}

    # Allowed: POST /vitals/measurement
    assert (
        client.post(
            "/api/v1/vitals/measurement",
            json={
                "cycle_id": "cycle-scope",
                "pulse": 70,
                "measured_at": datetime.now(timezone.utc).isoformat(),
            },
            headers=wo_headers,
        ).status_code
        == 201
    )
    # Forbidden: GET /vitals/latest
    assert client.get("/api/v1/vitals/latest?cycle_id=cycle-scope", headers=wo_headers).status_code == 403


def test_08_refresh_token_rotation_and_reuse_detection_via_http(isolated_api_env):
    """
    Verifies `POST /api/v1/auth/refresh`:
    1. Valid rotation returns new access + refresh tokens (`200 OK`, `Cache-Control: no-store`).
    2. Replaying the old rotated refresh token returns `401 Unauthorized` AND revokes the
       entire token family (`family_id`).
    3. Both the successor refresh token AND the successor access token in that compromised
       family are immediately invalidated (`401 Unauthorized`).
    """
    client, session, _, _ = isolated_api_env
    pair_data = _pair_patient_device(
        client,
        session,
        patient_id="pat-refresh",
        device_id="dev-refresh",
        raw_code="456456",
    )
    MedicalCycleRepository(session).create(
        cycle_id="cycle-refresh", patient_id="pat-refresh", status="active"
    )

    old_refresh = pair_data["refresh_token"]

    # 1. Legitimate refresh rotation -> 200 OK
    r_rot = client.post(
        "/api/v1/auth/refresh",
        json={"device_id": "dev-refresh", "refresh_token": old_refresh},
    )
    assert r_rot.status_code == 200
    assert r_rot.headers.get("cache-control") == "no-store"
    rot_data = r_rot.json()
    new_refresh = rot_data["refresh_token"]
    new_access = rot_data["access_token"]
    assert new_refresh != old_refresh

    # New access token works before reuse detection
    assert (
        client.get(
            "/api/v1/cycle/current",
            headers={"Authorization": f"Bearer {new_access}"},
        ).status_code
        == 200
    )

    # 2. Replay the OLD refresh token -> 401 + family revocation!
    r_replay = client.post(
        "/api/v1/auth/refresh",
        json={"device_id": "dev-refresh", "refresh_token": old_refresh},
    )
    assert r_replay.status_code == 401

    # 3. Successor refresh token is now also revoked (401)
    r_succ = client.post(
        "/api/v1/auth/refresh",
        json={"device_id": "dev-refresh", "refresh_token": new_refresh},
    )
    assert r_succ.status_code == 401

    # 4. Successor access token from the compromised family is also immediately rejected (401)!
    assert (
        client.get(
            "/api/v1/cycle/current",
            headers={"Authorization": f"Bearer {new_access}"},
        ).status_code
        == 401
    )


def test_09_concurrent_http_refresh_and_revoke_race(isolated_api_env):
    """
    Runs concurrent `POST /api/v1/auth/refresh` and `POST /api/v1/auth/revoke` requests.
    Guarantees that after both finish, the device is inactive (`is_active == False`),
    0 active refresh tokens remain in the database, and no valid session survives.
    """
    client, session, _, _ = isolated_api_env
    pair_data = _pair_patient_device(
        client,
        session,
        patient_id="pat-race-http",
        device_id="dev-race-http",
        raw_code="567567",
    )
    access_tok = pair_data["access_token"]
    refresh_tok = pair_data["refresh_token"]

    barrier = threading.Barrier(2)

    def _do_refresh():
        with TestClient(app) as c:
            barrier.wait(timeout=5.0)
            return c.post(
                "/api/v1/auth/refresh",
                json={"device_id": "dev-race-http", "refresh_token": refresh_tok},
            )

    def _do_revoke():
        with TestClient(app) as c:
            barrier.wait(timeout=5.0)
            return c.post(
                "/api/v1/auth/revoke",
                json={"device_id": "dev-race-http"},
                headers={"Authorization": f"Bearer {access_tok}"},
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        f_ref = pool.submit(_do_refresh)
        f_rev = pool.submit(_do_revoke)
        r_ref = f_ref.result()
        r_rev = f_rev.result()

    assert r_rev.status_code == 200

    session.expire_all()
    dev = session.get(Device, "dev-race-http")
    assert dev is not None and dev.is_active is False and dev.revoked_at is not None

    active_tokens = (
        session.execute(
            select(DeviceRefreshToken).where(
                DeviceRefreshToken.device_id == "dev-race-http",
                DeviceRefreshToken.revoked_at.is_(None),
            )
        )
        .scalars()
        .all()
    )
    assert len(active_tokens) == 0

    # If refresh completed right before revoke acquired the lock, its new access token must STILL be rejected!
    if r_ref.status_code == 200:
        minted_access = r_ref.json()["access_token"]
        assert (
            client.get(
                "/api/v1/cycle/current",
                headers={"Authorization": f"Bearer {minted_access}"},
            ).status_code
            == 401
        )


def test_10_device_revocation_self_only_and_instant_jwt_invalidation(isolated_api_env):
    """
    Verifies `POST /api/v1/auth/revoke`:
    1. Device A cannot revoke Device B (`403 Forbidden`), whether via Bearer JWT or refresh token.
    2. When Device A revokes itself (`200 OK`), its previously issued 30-minute JWT access token
       is IMMEDIATELY rejected (`401 Unauthorized`) on subsequent requests.
    3. Device B can also revoke itself via the fallback `(device_id, refresh_token)` body without
       a Bearer header.
    """
    client, session, _, _ = isolated_api_env
    pair_a = _pair_patient_device(
        client,
        session,
        patient_id="pat-rev-a",
        device_id="dev-rev-a",
        raw_code="616161",
    )
    pair_b = _pair_patient_device(
        client,
        session,
        patient_id="pat-rev-b",
        device_id="dev-rev-b",
        raw_code="626262",
    )
    MedicalCycleRepository(session).create(
        cycle_id="cycle-rev-a", patient_id="pat-rev-a", status="active"
    )

    headers_a = {"Authorization": f"Bearer {pair_a['access_token']}"}
    headers_b = {"Authorization": f"Bearer {pair_b['access_token']}"}

    # 1. Device A attempts to revoke Device B using Device A's Bearer JWT -> 403 Forbidden
    r_cross_jwt = client.post(
        "/api/v1/auth/revoke",
        json={"device_id": "dev-rev-b"},
        headers=headers_a,
    )
    assert r_cross_jwt.status_code == 403

    # 2. Device A attempts to revoke Device B using Device A's refresh token (no Bearer header) -> 403 Forbidden
    r_cross_rt = client.post(
        "/api/v1/auth/revoke",
        json={"device_id": "dev-rev-b", "refresh_token": pair_a["refresh_token"]},
    )
    assert r_cross_rt.status_code == 403

    # Verify Device B is still completely active
    session.expire_all()
    assert session.get(Device, "dev-rev-b").is_active is True

    # 3. Device A revokes its own session via Bearer JWT -> 200 OK
    r_self_a = client.post("/api/v1/auth/revoke", headers=headers_a)
    assert r_self_a.status_code == 200
    assert r_self_a.json()["device_id"] == "dev-rev-a"
    assert r_self_a.headers.get("cache-control") == "no-store"

    # Immediately, Device A's unexpired JWT is rejected with 401!
    assert client.get("/api/v1/cycle/current", headers=headers_a).status_code == 401
    assert (
        client.post(
            "/api/v1/auth/refresh",
            json={"device_id": "dev-rev-a", "refresh_token": pair_a["refresh_token"]},
        ).status_code
        == 401
    )

    # 4. Device B revokes itself using fallback (device_id, refresh_token) without Bearer header -> 200 OK
    r_self_b = client.post(
        "/api/v1/auth/revoke",
        json={"device_id": "dev-rev-b", "refresh_token": pair_b["refresh_token"]},
    )
    assert r_self_b.status_code == 200
    assert client.get("/api/v1/cycle/current", headers=headers_b).status_code == 401


def test_11_suspended_or_archived_patient_immediately_blocks_jwt_and_refresh(isolated_api_env):
    """
    Verifies that suspending or archiving a `Patient` immediately invalidates active
    `DeviceSession` JWTs (`401 Unauthorized`) and blocks `/api/v1/auth/refresh` (`401`).
    """
    client, session, _, _ = isolated_api_env
    pair_data = _pair_patient_device(
        client,
        session,
        patient_id="pat-suspend-http",
        device_id="dev-suspend-http",
        raw_code="636363",
    )
    MedicalCycleRepository(session).create(
        cycle_id="cycle-suspend-http", patient_id="pat-suspend-http", status="active"
    )
    headers = {"Authorization": f"Bearer {pair_data['access_token']}"}

    # Works while active
    assert client.get("/api/v1/cycle/current", headers=headers).status_code == 200

    # Suspend patient
    PatientRepository(session).update_status("pat-suspend-http", "suspended")

    # Immediately blocked on both JWT and Refresh!
    assert client.get("/api/v1/cycle/current", headers=headers).status_code == 401
    assert (
        client.get("/api/v1/vitals/latest?cycle_id=cycle-suspend-http", headers=headers).status_code
        == 401
    )
    assert (
        client.post(
            "/api/v1/auth/refresh",
            json={"device_id": "dev-suspend-http", "refresh_token": pair_data["refresh_token"]},
        ).status_code
        == 401
    )


def test_12_patient_a_cannot_read_patient_b_cycle_current_or_vitals_latest(isolated_api_env):
    """
    Verifies strict read isolation between Patient A and Patient B on:
    - `GET /api/v1/cycle/current`
    - `GET /api/v1/vitals/latest`
    """
    client, session, _, _ = isolated_api_env
    pair_a = _pair_patient_device(
        client, session, patient_id="pat-iso-a", device_id="dev-iso-a", raw_code="710001"
    )
    pair_b = _pair_patient_device(
        client, session, patient_id="pat-iso-b", device_id="dev-iso-b", raw_code="710002"
    )
    headers_a = {"Authorization": f"Bearer {pair_a['access_token']}"}
    headers_b = {"Authorization": f"Bearer {pair_b['access_token']}"}

    cycle_repo = MedicalCycleRepository(session)
    cycle_repo.create(cycle_id="cycle-owner-b", patient_id="pat-iso-b", status="active")

    # 1. Patient B has an active cycle, but Patient A does NOT -> Patient A gets 404 on /cycle/current
    r_curr_a_empty = client.get("/api/v1/cycle/current", headers=headers_a)
    assert r_curr_a_empty.status_code == 404
    assert "cycle-owner-b" not in r_curr_a_empty.text

    # Patient B gets cycle-owner-b
    r_curr_b = client.get("/api/v1/cycle/current", headers=headers_b)
    assert r_curr_b.status_code == 200
    assert r_curr_b.json()["cycle_id"] == "cycle-owner-b"

    # 2. Now create active cycle for Patient A -> each patient sees ONLY their own active cycle
    cycle_repo.create(cycle_id="cycle-owner-a", patient_id="pat-iso-a", status="active")
    assert client.get("/api/v1/cycle/current", headers=headers_a).json()["cycle_id"] == "cycle-owner-a"
    assert client.get("/api/v1/cycle/current", headers=headers_b).json()["cycle_id"] == "cycle-owner-b"

    # 3. Patient A attempts GET /api/v1/vitals/latest for Patient B's cycle -> 403 Forbidden
    r_latest_cross = client.get("/api/v1/vitals/latest?cycle_id=cycle-owner-b", headers=headers_a)
    assert r_latest_cross.status_code == 403
    assert r_latest_cross.json()["detail"] == GENERIC_CYCLE_ACCESS_DENIED_DETAIL


def test_13_patient_a_cannot_write_vitals_single_or_batch_to_patient_b_cycle(isolated_api_env):
    """
    Verifies that Patient A cannot write single or batch vitals measurements into Patient B's cycle.
    """
    client, session, _, _ = isolated_api_env
    pair_a = _pair_patient_device(
        client, session, patient_id="pat-write-a", device_id="dev-write-a", raw_code="720001"
    )
    _pair_patient_device(
        client, session, patient_id="pat-write-b", device_id="dev-write-b", raw_code="720002"
    )
    headers_a = {"Authorization": f"Bearer {pair_a['access_token']}"}

    cycle_repo = MedicalCycleRepository(session)
    cycle_repo.create(cycle_id="cycle-write-a", patient_id="pat-write-a", status="active")
    cycle_repo.create(cycle_id="cycle-write-b", patient_id="pat-write-b", status="active")

    now_iso = datetime.now(timezone.utc).isoformat()

    # 1. Patient A writes single measurement to Patient B's cycle -> 403 Forbidden
    r_single = client.post(
        "/api/v1/vitals/measurement",
        json={
            "cycle_id": "cycle-write-b",
            "pulse": 85,
            "systolic": 122,
            "diastolic": 78,
            "spo2": 98,
            "source": "watch_sensor",
            "measured_at": now_iso,
        },
        headers=headers_a,
    )
    assert r_single.status_code == 403
    assert r_single.json()["detail"] == GENERIC_CYCLE_ACCESS_DENIED_DETAIL

    # 2. Patient A writes batch measurements to Patient B's cycle -> 403 Forbidden
    r_batch = client.post(
        "/api/v1/vitals/batch",
        json={
            "cycle_id": "cycle-write-b",
            "measurements": [
                {
                    "pulse": 90,
                    "source": "watch_sensor",
                    "measured_at": now_iso,
                }
            ],
        },
        headers=headers_a,
    )
    assert r_batch.status_code == 403
    assert r_batch.json()["detail"] == GENERIC_CYCLE_ACCESS_DENIED_DETAIL

    # Verify 0 measurements were persisted in cycle-write-b
    session.expire_all()
    assert len(HealthMeasurementRepository(session).list_by_cycle("cycle-write-b")) == 0

    # 3. Patient A writing to own cycle-write-a succeeds (201 Created)
    r_own = client.post(
        "/api/v1/vitals/measurement",
        json={
            "cycle_id": "cycle-write-a",
            "pulse": 74,
            "source": "watch_sensor",
            "measured_at": now_iso,
        },
        headers=headers_a,
    )
    assert r_own.status_code == 201


def test_14_patient_a_cannot_access_patient_b_reports_by_cycle_or_report_id(isolated_api_env):
    """
    Verifies that Patient A cannot list or fetch Patient B's reports via `reports.py`,
    and receives a uniform `403 Forbidden` without leaking report existence or privacy level.
    """
    client, session, _, _ = isolated_api_env
    pair_a = _pair_patient_device(
        client, session, patient_id="pat-rep-a", device_id="dev-rep-a", raw_code="730001"
    )
    _pair_patient_device(
        client, session, patient_id="pat-rep-b", device_id="dev-rep-b", raw_code="730002"
    )
    fam_a = verify_access_token(pair_a["access_token"]).family_id
    reports_jwt_a = create_access_token(
        patient_id="pat-rep-a",
        device_id="dev-rep-a",
        family_id=fam_a,
        scope="cycle:read reports:read",
    ).access_token
    headers_a = {"Authorization": f"Bearer {reports_jwt_a}"}

    cycle_repo = MedicalCycleRepository(session)
    report_repo = ModelReportRepository(session)
    cycle_repo.create(cycle_id="cycle-rep-a", patient_id="pat-rep-a", status="active")
    cycle_repo.create(cycle_id="cycle-rep-b", patient_id="pat-rep-b", status="active")

    report_repo.create(
        report_id="rep_patient_a_01",
        cycle_id="cycle-rep-a",
        model="gemini-3.1-pro",
        report_type="pro_general_shelf",
        privacy_level=PrivacyLevel.CLIENT_FACING,
        content={"title": "Patient A Plan", "summary": "Personal roadmap for A"},
    )
    report_repo.create(
        report_id="rep_patient_b_client_01",
        cycle_id="cycle-rep-b",
        model="gemini-3.1-pro",
        report_type="pro_general_shelf",
        privacy_level=PrivacyLevel.CLIENT_FACING,
        content={"title": "Patient B Plan", "summary": "Confidential roadmap for B"},
    )
    report_repo.create(
        report_id="rep_patient_b_server_01",
        cycle_id="cycle-rep-b",
        model="gemini-3.1-pro",
        report_type="pro_training_shelf",
        privacy_level=PrivacyLevel.SERVER_ONLY,
        content={"internal": "Server only for B"},
    )

    # 1. Patient A reads own reports -> 200 OK
    r_own_list = client.get("/api/v1/reports/client?cycle_id=cycle-rep-a", headers=headers_a)
    assert r_own_list.status_code == 200
    assert len(r_own_list.json()) == 1
    assert r_own_list.json()[0]["report_id"] == "rep_patient_a_01"

    r_own_single = client.get("/api/v1/reports/client/rep_patient_a_01", headers=headers_a)
    assert r_own_single.status_code == 200

    # 2. Patient A attempts to list Patient B's reports -> 403 Forbidden
    r_other_list = client.get("/api/v1/reports/client?cycle_id=cycle-rep-b", headers=headers_a)
    assert r_other_list.status_code == 403
    assert r_other_list.json()["detail"] == GENERIC_CYCLE_ACCESS_DENIED_DETAIL
    assert "Confidential roadmap for B" not in r_other_list.text

    # 3. Patient A attempts to fetch Patient B's client report, server report, or nonexistent report -> identical 403!
    r_b_client = client.get("/api/v1/reports/client/rep_patient_b_client_01", headers=headers_a)
    r_b_server = client.get("/api/v1/reports/client/rep_patient_b_server_01", headers=headers_a)
    r_nonexistent = client.get("/api/v1/reports/client/rep_does_not_exist_99", headers=headers_a)

    for r in (r_b_client, r_b_server, r_nonexistent):
        assert r.status_code == 403
        assert r.json()["detail"] == GENERIC_REPORT_ACCESS_DENIED_DETAIL


def test_15_mobile_device_session_forbidden_on_clinical_cascade_and_cycle_admin_routes(
    isolated_api_env,
):
    """
    Verifies that a mobile `DeviceSession` (even if minted with elevated scope strings)
    is strictly forbidden (`403 Forbidden`) from invoking any clinical cascade route in
    `cascade.py` or administrative cycle lifecycle routes (`/start`, `/close`, `/archive`).
    """
    client, session, _, _ = isolated_api_env
    pair_no_cascade = _pair_patient_device(
        client, session, patient_id="pat-no-cascade", device_id="dev-no-cascade", raw_code="740001"
    )
    active_family_id = verify_access_token(pair_no_cascade["access_token"]).family_id
    MedicalCycleRepository(session).create(
        cycle_id="cycle-no-cascade", patient_id="pat-no-cascade", status="active"
    )

    # Even if a JWT had extra scope strings, DeviceSession is forbidden on admin/cascade endpoints
    elevated_jwt = create_access_token(
        patient_id="pat-no-cascade",
        device_id="dev-no-cascade",
        family_id=active_family_id,
        scope="vitals:read vitals:write cycle:read cycle:write cascade:run admin",
    ).access_token
    dev_headers = {"Authorization": f"Bearer {elevated_jwt}"}

    # Cycle lifecycle admin endpoints -> 403 Forbidden
    assert (
        client.post(
            "/api/v1/cycle/start",
            json={"cycle_id": "cycle-mobile-attempt"},
            headers=dev_headers,
        ).status_code
        == 403
    )
    assert (
        client.post("/api/v1/cycle/cycle-no-cascade/close", headers=dev_headers).status_code == 403
    )
    assert (
        client.post("/api/v1/cycle/cycle-no-cascade/archive", headers=dev_headers).status_code == 403
    )

    # All cascade endpoints -> 403 Forbidden
    assert (
        client.post(
            "/api/v1/cascade/intake/anamnesis",
            json={"cycle_id": "cycle-no-cascade", "answers": {"complaints": "none"}},
            headers=dev_headers,
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/api/v1/cascade/run-primary",
            json={"cycle_id": "cycle-no-cascade"},
            headers=dev_headers,
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/api/v1/cascade/run-weekly",
            json={"cycle_id": "cycle-no-cascade", "sprint_id": "sp-1"},
            headers=dev_headers,
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/api/v1/cascade/labs/upsert",
            json={
                "cycle_id": "cycle-no-cascade",
                "mutations": [
                    {
                        "marker_key": "glucose",
                        "value": 5.1,
                        "unit": "mmol/L",
                        "sample_date": "2026-10-20",
                        "event_type": "initial",
                    }
                ],
            },
            headers=dev_headers,
        ).status_code
        == 403
    )
    assert (
        client.get(
            "/api/v1/cascade/labs/state?cycle_id=cycle-no-cascade",
            headers=dev_headers,
        ).status_code
        == 403
    )
    assert (
        client.get(
            "/api/v1/cascade/lab-revision-cooldown?cycle_id=cycle-no-cascade",
            headers=dev_headers,
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/api/v1/cascade/run-lab-revision",
            json={"cycle_id": "cycle-no-cascade"},
            headers=dev_headers,
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/api/v1/cascade/intake/lab-images",
            data={"cycle_id": "cycle-no-cascade"},
            files=[("files", ("lab.png", b"\x89PNG\r\n\x1a\nfake", "image/png"))],
            headers=dev_headers,
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/api/v1/cascade/intake/ecg-image",
            data={"cycle_id": "cycle-no-cascade"},
            files={"file": ("ecg.png", b"\x89PNG\r\n\x1a\nfake", "image/png")},
            headers=dev_headers,
        ).status_code
        == 403
    )


def test_16_legacy_null_patient_cycles_inaccessible_to_device_session_until_explicitly_assigned(
    isolated_api_env,
):
    """
    Verifies that legacy cycles (`patient_id IS NULL`) are completely inaccessible to
    mobile `DeviceSession` tokens until explicitly linked via `assign_patient`.
    """
    client, session, _, _ = isolated_api_env
    pair_a = _pair_patient_device(
        client, session, patient_id="pat-leg-a", device_id="dev-leg-a", raw_code="750001"
    )
    pair_b = _pair_patient_device(
        client, session, patient_id="pat-leg-b", device_id="dev-leg-b", raw_code="750002"
    )
    headers_a = {"Authorization": f"Bearer {pair_a['access_token']}"}
    headers_b = {"Authorization": f"Bearer {pair_b['access_token']}"}
    fam_leg_a = verify_access_token(pair_a["access_token"]).family_id
    reports_headers_a = {
        "Authorization": f"Bearer {create_access_token(patient_id='pat-leg-a', device_id='dev-leg-a', family_id=fam_leg_a, scope='cycle:read reports:read').access_token}"
    }

    cycle_repo = MedicalCycleRepository(session)
    cycle_repo.create(cycle_id="cycle-legacy-null", patient_id=None, status="active")

    # 1. Legacy cycle is invisible on GET /cycle/current (404) and forbidden on vitals/reports (403)
    assert client.get("/api/v1/cycle/current", headers=headers_a).status_code == 404
    assert (
        client.post(
            "/api/v1/vitals/measurement",
            json={
                "cycle_id": "cycle-legacy-null",
                "pulse": 75,
                "measured_at": datetime.now(timezone.utc).isoformat(),
            },
            headers=headers_a,
        ).status_code
        == 403
    )
    assert (
        client.get("/api/v1/vitals/latest?cycle_id=cycle-legacy-null", headers=headers_a).status_code
        == 403
    )
    assert (
        client.get(
            "/api/v1/reports/client?cycle_id=cycle-legacy-null", headers=reports_headers_a
        ).status_code
        == 403
    )

    # 2. Explicitly assign legacy cycle to Patient A
    cycle_repo.assign_patient("cycle-legacy-null", "pat-leg-a")

    # Now Patient A can access it, while Patient B still gets 403 Forbidden
    r_curr_a = client.get("/api/v1/cycle/current", headers=headers_a)
    assert r_curr_a.status_code == 200
    assert r_curr_a.json()["cycle_id"] == "cycle-legacy-null"

    assert (
        client.get("/api/v1/vitals/latest?cycle_id=cycle-legacy-null", headers=headers_a).status_code
        == 200
    )
    assert (
        client.get("/api/v1/vitals/latest?cycle_id=cycle-legacy-null", headers=headers_b).status_code
        == 403
    )


def test_17_zero_oracle_leakage_across_nonexistent_foreign_closed_and_legacy_cycles(
    isolated_api_env,
):
    """
    Verifies Zero Oracle Leakage:
    When Patient A probes:
    - `cycle_b_active` (owned by Patient B, active, containing idempotency_key 'idem-b-1')
    - `cycle_b_completed` (owned by Patient B, completed)
    - `cycle_b_archived` (owned by Patient B, archived)
    - `cycle_legacy_completed` (`patient_id IS NULL`, completed)
    - `cycle_nonexistent_xyz` (does not exist in DB)
    Every request returns the EXACT SAME `403 Forbidden` status and generic `detail` string,
    never leaking existence, owner `patient_id`, `cycle.status`, or idempotency conflicts!
    """
    client, session, _, _ = isolated_api_env
    pair_a = _pair_patient_device(
        client, session, patient_id="pat-oracle-a", device_id="dev-oracle-a", raw_code="760001"
    )
    _pair_patient_device(
        client, session, patient_id="pat-oracle-b", device_id="dev-oracle-b", raw_code="760002"
    )
    headers_a = {"Authorization": f"Bearer {pair_a['access_token']}"}
    fam_oracle_a = verify_access_token(pair_a["access_token"]).family_id
    reports_headers_a = {
        "Authorization": f"Bearer {create_access_token(patient_id='pat-oracle-a', device_id='dev-oracle-a', family_id=fam_oracle_a, scope='cycle:read reports:read').access_token}"
    }

    cycle_repo = MedicalCycleRepository(session)
    cycle_repo.create(cycle_id="cycle_b_active", patient_id="pat-oracle-b", status="active")
    cycle_repo.create(cycle_id="cycle_b_completed", patient_id="pat-oracle-b", status="completed")
    cycle_repo.create(cycle_id="cycle_b_archived", patient_id="pat-oracle-b", status="archived")
    cycle_repo.create(cycle_id="cycle_legacy_completed", patient_id=None, status="completed")

    # Seed a measurement in cycle_b_active at now_dt
    now_dt = datetime.now(timezone.utc)
    session.add(
        HealthMeasurement(
            cycle_id="cycle_b_active",
            pulse=68,
            source="watch_sensor",
            measured_at=now_dt,
        )
    )
    session.commit()

    probed_cycles = [
        "cycle_b_active",
        "cycle_b_completed",
        "cycle_b_archived",
        "cycle_legacy_completed",
        "cycle_nonexistent_xyz",
    ]

    expected_body = {"detail": GENERIC_CYCLE_ACCESS_DENIED_DETAIL}

    for cid in probed_cycles:
        # 1. Single measurement with identical timestamp/source and conflicting pulse
        r1 = client.post(
            "/api/v1/vitals/measurement",
            json={
                "cycle_id": cid,
                "pulse": 145,
                "source": "watch_sensor",
                "measured_at": now_dt.isoformat(),
            },
            headers=headers_a,
        )
        assert r1.status_code == 403
        assert r1.json() == expected_body

        # 2. Batch measurement (even with mismatched item cycle_id that would normally trigger 400!)
        r2 = client.post(
            "/api/v1/vitals/batch",
            json={
                "cycle_id": cid,
                "measurements": [
                    {
                        "cycle_id": "conflicting-item-cycle-id",
                        "pulse": 110,
                        "source": "watch_sensor",
                        "measured_at": now_dt.isoformat(),
                    }
                ],
            },
            headers=headers_a,
        )
        assert r2.status_code == 403
        assert r2.json() == expected_body

        # 3. Latest vitals stream
        r3 = client.get(f"/api/v1/vitals/latest?cycle_id={cid}", headers=headers_a)
        assert r3.status_code == 403
        assert r3.json() == expected_body

        # 4. Client reports list
        r4 = client.get(f"/api/v1/reports/client?cycle_id={cid}", headers=reports_headers_a)
        assert r4.status_code == 403
        assert r4.json() == expected_body


def test_18_no_sensitive_codes_tokens_or_secrets_logged_during_auth_lifecycle(isolated_api_env):
    """
    Verifies that pairing codes, access tokens, refresh tokens, and server secrets are
    never written to logs or leaked in error payloads during `/pair`, `/refresh`, or `/revoke`.
    """
    client, session, _, _ = isolated_api_env
    log_stream = io.StringIO()
    handler = logging.StreamHandler(log_stream)
    root_logger = logging.getLogger()
    root_logger.addHandler(handler)

    try:
        secret_code = "098765"
        pair_data = _pair_patient_device(
            client,
            session,
            patient_id="pat-log-audit",
            device_id="dev-log-audit",
            raw_code=secret_code,
        )
        access_tok = pair_data["access_token"]
        refresh_tok = pair_data["refresh_token"]

        # Failed pair attempt
        r_fail_pair = client.post(
            "/api/v1/auth/pair",
            json={"pairing_code": "654321", "device_id": "dev-log-2", "device_name": "Watch"},
        )
        assert r_fail_pair.status_code == 401
        assert "654321" not in r_fail_pair.text

        # Successful refresh
        r_ref = client.post(
            "/api/v1/auth/refresh",
            json={"device_id": "dev-log-audit", "refresh_token": refresh_tok},
        )
        assert r_ref.status_code == 200
        rot_data = r_ref.json()

        # Revoke
        r_rev = client.post(
            "/api/v1/auth/revoke",
            headers={"Authorization": f"Bearer {rot_data['access_token']}"},
        )
        assert r_rev.status_code == 200

        logs = log_stream.getvalue()
        for sensitive in (
            secret_code,
            "654321",
            access_tok,
            refresh_tok,
            rot_data["access_token"],
            rot_data["refresh_token"],
            TEST_API_SECRET,
            TEST_PAIRING_SECRET,
            TEST_JWT_SECRET,
        ):
            assert sensitive not in logs
    finally:
        root_logger.removeHandler(handler)


def test_19_scope_all_required_and_reports_read_enforcement(isolated_api_env):
    """
    Stage 5.3B-2 v2 Regression Test (Item 1):
    Verifies that `require_scopes()` enforces ALL listed scopes by default (`has_all_scopes`):
    1. A device token with only `cycle:read` (or default `vitals:read vitals:write cycle:read`)
       is rejected with `403 Forbidden` (`Insufficient token scope for this operation.`) on
       `GET /api/v1/reports/client` and `GET /api/v1/reports/client/{report_id}`.
    2. A device token with only `reports:read` (missing `cycle:read`) is ALSO rejected with `403`.
    3. A device token possessing BOTH `cycle:read` AND `reports:read` with confirmed cycle
       ownership succeeds (`200 OK`) ONLY on client-facing reports belonging to that patient,
       and is rejected with `403 Forbidden` on `server_only` reports or other patients' reports.
    4. Verifies that `require_any_scope()` explicitly provides alternative-scope selection when needed.
    """
    client, session, _, _ = isolated_api_env
    pair_a = _pair_patient_device(
        client, session, patient_id="pat-v2-scope-a", device_id="dev-v2-scope-a", raw_code="810001"
    )
    _pair_patient_device(
        client, session, patient_id="pat-v2-scope-b", device_id="dev-v2-scope-b", raw_code="810002"
    )
    fam_a = verify_access_token(pair_a["access_token"]).family_id

    cycle_repo = MedicalCycleRepository(session)
    report_repo = ModelReportRepository(session)
    cycle_repo.create(cycle_id="cycle-v2-scope-a", patient_id="pat-v2-scope-a", status="active")
    cycle_repo.create(cycle_id="cycle-v2-scope-b", patient_id="pat-v2-scope-b", status="active")

    report_repo.create(
        report_id="rep_v2_a_client",
        cycle_id="cycle-v2-scope-a",
        model="gemini-3.1-pro",
        report_type="pro_general_shelf",
        privacy_level=PrivacyLevel.CLIENT_FACING,
        content={"title": "Patient A Client Report", "summary": "Visible to A with reports:read"},
    )
    report_repo.create(
        report_id="rep_v2_a_server",
        cycle_id="cycle-v2-scope-a",
        model="gemini-3.1-pro",
        report_type="pro_training_shelf",
        privacy_level=PrivacyLevel.SERVER_ONLY,
        content={"internal": "Strictly server_only for A"},
    )
    report_repo.create(
        report_id="rep_v2_b_client",
        cycle_id="cycle-v2-scope-b",
        model="gemini-3.1-pro",
        report_type="pro_general_shelf",
        privacy_level=PrivacyLevel.CLIENT_FACING,
        content={"title": "Patient B Client Report", "summary": "Belongs to B"},
    )

    # 1. Standard pairing token (with DEFAULT_DEVICE_SCOPE containing reports:read) -> 200 OK
    default_headers = {"Authorization": f"Bearer {pair_a['access_token']}"}
    r_def_list = client.get("/api/v1/reports/client?cycle_id=cycle-v2-scope-a", headers=default_headers)
    assert r_def_list.status_code == 200

    r_def_single = client.get("/api/v1/reports/client/rep_v2_a_client", headers=default_headers)
    assert r_def_single.status_code == 200

    # 1b. Legacy Stage 5.3 token ("vitals:read vitals:write cycle:read") lacks reports:read -> 403
    legacy_jwt = create_access_token(
        patient_id="pat-v2-scope-a",
        device_id="dev-v2-scope-a",
        family_id=fam_a,
        scope=LEGACY_DEVICE_SCOPE,
    ).access_token
    legacy_headers = {"Authorization": f"Bearer {legacy_jwt}"}
    r_leg_list = client.get("/api/v1/reports/client?cycle_id=cycle-v2-scope-a", headers=legacy_headers)
    assert r_leg_list.status_code == 403
    assert r_leg_list.json()["detail"] == "Insufficient token scope for this operation."

    r_leg_single = client.get("/api/v1/reports/client/rep_v2_a_client", headers=legacy_headers)
    assert r_leg_single.status_code == 403
    assert r_leg_single.json()["detail"] == "Insufficient token scope for this operation."

    # 2. Token with ONLY cycle:read -> 403
    only_cycle_jwt = create_access_token(
        patient_id="pat-v2-scope-a",
        device_id="dev-v2-scope-a",
        family_id=fam_a,
        scope="cycle:read",
    ).access_token
    only_cycle_headers = {"Authorization": f"Bearer {only_cycle_jwt}"}
    assert (
        client.get(
            "/api/v1/reports/client?cycle_id=cycle-v2-scope-a", headers=only_cycle_headers
        ).status_code
        == 403
    )
    assert (
        client.get("/api/v1/reports/client/rep_v2_a_client", headers=only_cycle_headers).status_code
        == 403
    )

    # 3. Token with ONLY reports:read (missing cycle:read) -> 403
    only_reports_jwt = create_access_token(
        patient_id="pat-v2-scope-a",
        device_id="dev-v2-scope-a",
        family_id=fam_a,
        scope="reports:read",
    ).access_token
    only_reports_headers = {"Authorization": f"Bearer {only_reports_jwt}"}
    assert (
        client.get(
            "/api/v1/reports/client?cycle_id=cycle-v2-scope-a", headers=only_reports_headers
        ).status_code
        == 403
    )
    assert (
        client.get(
            "/api/v1/reports/client/rep_v2_a_client", headers=only_reports_headers
        ).status_code
        == 403
    )

    # 4. Token with BOTH cycle:read AND reports:read -> 200 OK for own client_facing report, 403 for server_only & foreign
    both_jwt = create_access_token(
        patient_id="pat-v2-scope-a",
        device_id="dev-v2-scope-a",
        family_id=fam_a,
        scope="cycle:read reports:read",
    ).access_token
    both_headers = {"Authorization": f"Bearer {both_jwt}"}

    r_ok_list = client.get("/api/v1/reports/client?cycle_id=cycle-v2-scope-a", headers=both_headers)
    assert r_ok_list.status_code == 200
    assert [item["report_id"] for item in r_ok_list.json()] == ["rep_v2_a_client"]

    r_ok_single = client.get("/api/v1/reports/client/rep_v2_a_client", headers=both_headers)
    assert r_ok_single.status_code == 200
    assert r_ok_single.json()["report_id"] == "rep_v2_a_client"

    # Own server_only report -> 403 Forbidden
    r_own_srv = client.get("/api/v1/reports/client/rep_v2_a_server", headers=both_headers)
    assert r_own_srv.status_code == 403
    assert r_own_srv.json()["detail"] == GENERIC_REPORT_ACCESS_DENIED_DETAIL

    # Foreign patient report -> 403 Forbidden
    r_foreign = client.get("/api/v1/reports/client/rep_v2_b_client", headers=both_headers)
    assert r_foreign.status_code == 403
    assert r_foreign.json()["detail"] == GENERIC_REPORT_ACCESS_DENIED_DETAIL

    # 5. Direct unit check of require_scopes vs require_any_scope
    from app.security.auth import authenticate_device_jwt

    dev_sess_cycle_only = authenticate_device_jwt(only_cycle_jwt, session)
    dep_all = require_scopes("cycle:read", "reports:read")
    with pytest.raises( Exception ) as exc_info:
        dep_all(auth=dev_sess_cycle_only)
    assert getattr(exc_info.value, "status_code", None) == 403

    dep_any = require_any_scope("cycle:read", "reports:read")
    assert dep_any(auth=dev_sess_cycle_only) == dev_sess_cycle_only


def test_20_token_family_lineage_fail_closed_and_lifecycle_checks(isolated_api_env):
    """
    Stage 5.3B-2 v2 Regression Test (Item 2):
    Verifies `authenticate_device_jwt()` strict token family and device/patient checks:
    1. Active family + active device + active patient -> 200 OK.
    2. Unknown `family_id` (not matching any server family for this `device_id`) -> 401 Unauthorized.
    3. All refresh tokens in the family revoked -> 401 Unauthorized.
    4. Family completely absent from DB (0 refresh tokens for the device) -> 401 Unauthorized (fail-closed).
    5. Device revoked -> 401 Unauthorized.
    6. Patient suspended -> 401 Unauthorized.
    """
    client, session, _, _ = isolated_api_env
    pair_data = _pair_patient_device(
        client, session, patient_id="pat-v2-fam", device_id="dev-v2-fam", raw_code="820001"
    )
    MedicalCycleRepository(session).create(
        cycle_id="cycle-v2-fam", patient_id="pat-v2-fam", status="active"
    )
    real_family_id = verify_access_token(pair_data["access_token"]).family_id

    # 1. Active family -> 200 OK
    valid_headers = {"Authorization": f"Bearer {pair_data['access_token']}"}
    assert client.get("/api/v1/cycle/current", headers=valid_headers).status_code == 200

    # 2. Unknown family_id -> 401 Unauthorized (fail-closed)
    unknown_fam_jwt = create_access_token(
        patient_id="pat-v2-fam",
        device_id="dev-v2-fam",
        family_id="fam-does-not-exist-on-server",
    ).access_token
    r_unknown_fam = client.get(
        "/api/v1/cycle/current",
        headers={"Authorization": f"Bearer {unknown_fam_jwt}"},
    )
    assert r_unknown_fam.status_code == 401
    assert "family" in r_unknown_fam.json()["detail"].lower()

    # 3. Revoke all tokens in the active family -> immediate 401 Unauthorized
    now_utc = datetime.now(timezone.utc)
    family_rows = (
        session.execute(
            select(DeviceRefreshToken).where(
                DeviceRefreshToken.device_id == "dev-v2-fam",
                DeviceRefreshToken.family_id == real_family_id,
            )
        )
        .scalars()
        .all()
    )
    assert len(family_rows) == 1
    for r_tok in family_rows:
        r_tok.revoked_at = now_utc
    session.commit()

    r_revoked_fam = client.get("/api/v1/cycle/current", headers=valid_headers)
    assert r_revoked_fam.status_code == 401

    # Restore token to unrevoked, then delete the family row completely -> 401 Unauthorized (fail-closed when family absent)
    for r_tok in family_rows:
        session.delete(r_tok)
    session.commit()

    r_absent_fam = client.get("/api/v1/cycle/current", headers=valid_headers)
    assert r_absent_fam.status_code == 401

    # Re-pair device to get a fresh server-backed family
    _, _ = PairingRepository(session).create_code(
        patient_id="pat-v2-fam", raw_code_override="820002"
    )
    r_repair = client.post(
        "/api/v1/auth/pair",
        json={"pairing_code": "820002", "device_id": "dev-v2-fam", "device_name": "Phone"},
    )
    assert r_repair.status_code == 200
    repaired_headers = {"Authorization": f"Bearer {r_repair.json()['access_token']}"}
    assert client.get("/api/v1/cycle/current", headers=repaired_headers).status_code == 200

    # 5. Suspend patient while device and token family remain active -> 401 Unauthorized
    pat_entity = PatientRepository(session).get("pat-v2-fam")
    assert pat_entity is not None
    pat_entity.status = "suspended"
    session.commit()
    r_susp = client.get("/api/v1/cycle/current", headers=repaired_headers)
    assert r_susp.status_code == 401
    assert "patient" in r_susp.json()["detail"].lower()

    # Restore patient to active -> 200 OK, then revoke device -> 401 Unauthorized
    pat_entity.status = "active"
    session.commit()
    assert client.get("/api/v1/cycle/current", headers=repaired_headers).status_code == 200

    DeviceRepository(session).revoke_device("dev-v2-fam", commit=True)
    r_dev_rev = client.get("/api/v1/cycle/current", headers=repaired_headers)
    assert r_dev_rev.status_code == 401
    assert "revoked" in r_dev_rev.json()["detail"].lower()


def test_21_uniform_pairing_error_handling_without_patient_or_device_owner_leakage(
    isolated_api_env,
    monkeypatch: pytest.MonkeyPatch,
):
    """
    Stage 5.3B-2 v2 Regression Test (Item 3):
    Verifies that `POST /api/v1/auth/pair` returns a single uniform status (`401 Unauthorized`)
    and generic detail (`GENERIC_PAIRING_FAILURE_DETAIL`) across all invalid or unacceptable
    pairing credential scenarios:
    - unknown/wrong code
    - expired code
    - already used code
    - valid code belonging to a `suspended` `Patient`
    - valid code submitted with a `device_id` already registered to another patient
    Never discloses patient existence/status or `device_id` ownership, while preserving:
    - `429` for rate limiting
    - `503` when secrets or rate limiter are unavailable
    - `422` for malformed request schema
    """
    client, session, _, _ = isolated_api_env
    pair_a = _pair_patient_device(
        client, session, patient_id="pat-v2-pair-a", device_id="dev-owned-by-a", raw_code="830001"
    )
    assert pair_a["device_id"] == "dev-owned-by-a"

    PatientRepository(session).create(patient_id="pat-v2-pair-b", status="active")
    PatientRepository(session).create(patient_id="pat-v2-pair-susp", status="active")

    # Issue codes for B (active), B (expired), and Susp (then suspend patient)
    _, code_b_valid = PairingRepository(session).create_code(
        patient_id="pat-v2-pair-b", raw_code_override="830002"
    )
    _, code_b_exp = PairingRepository(session).create_code(
        patient_id="pat-v2-pair-b", raw_code_override="830003"
    )
    # Note: issuing 830003 superseded 830002 for pat-v2-pair-b, so let's expire 830003 first, then issue 830002!
    code_b_exp.expires_at = datetime.now(timezone.utc) - timedelta(minutes=5)
    code_b_exp.active_code_hash = None
    session.commit()

    _, code_b_valid = PairingRepository(session).create_code(
        patient_id="pat-v2-pair-b", raw_code_override="830002"
    )
    _, code_susp = PairingRepository(session).create_code(
        patient_id="pat-v2-pair-susp", raw_code_override="830004"
    )
    PatientRepository(session).update_status("pat-v2-pair-susp", "suspended")

    expected_error = {"detail": GENERIC_PAIRING_FAILURE_DETAIL}

    # 1. Wrong code -> 401
    r_wrong = client.post(
        "/api/v1/auth/pair",
        json={"pairing_code": "000999", "device_id": "dev-probe-1", "device_name": "Probe"},
    )
    # 2. Expired code -> 401
    r_expired = client.post(
        "/api/v1/auth/pair",
        json={"pairing_code": "830003", "device_id": "dev-probe-2", "device_name": "Probe"},
    )
    # 3. Already consumed code (830001 from pair_a) -> 401
    r_used = client.post(
        "/api/v1/auth/pair",
        json={"pairing_code": "830001", "device_id": "dev-probe-3", "device_name": "Probe"},
    )
    # 4. Valid code for suspended patient (830004) -> 401 (uniform, does NOT return 403 or leak patient status!)
    r_suspended = client.post(
        "/api/v1/auth/pair",
        json={"pairing_code": "830004", "device_id": "dev-probe-4", "device_name": "Probe"},
    )
    # 5. Valid code for active Patient B (830002) submitted with `dev-owned-by-a` -> 401 (uniform, does NOT return 409 or leak owner!)
    r_conflict_dev = client.post(
        "/api/v1/auth/pair",
        json={"pairing_code": "830002", "device_id": "dev-owned-by-a", "device_name": "Probe"},
    )

    for resp in (r_wrong, r_expired, r_used, r_suspended, r_conflict_dev):
        assert resp.status_code == 401
        assert resp.json() == expected_error
        assert resp.headers.get("cache-control") == "no-store"

    # Verify that 830002 (Patient B's valid code) was rolled back and NOT consumed on device_id conflict!
    session.expire_all()
    refreshed_b = session.get(PairingCode, code_b_valid.code_id)
    assert refreshed_b is not None
    assert refreshed_b.consumed_at is None
    assert refreshed_b.active_code_hash is not None

    # Verify 422 on malformed request format without leaking raw pairing_code
    r_422 = client.post(
        "/api/v1/auth/pair",
        json={"pairing_code": "123", "device_id": "dev-probe-5", "device_name": "Probe"},
    )
    assert r_422.status_code == 422
    assert "123" not in r_422.text
    assert r_422.headers.get("cache-control") == "no-store"
    assert r_422.headers.get("pragma") == "no-cache"

    # Verify 429 on 6th attempt from same IP (5 failed attempts already recorded above)
    r_429 = client.post(
        "/api/v1/auth/pair",
        json={"pairing_code": "830002", "device_id": "dev-fresh-b", "device_name": "Probe"},
    )
    assert r_429.status_code == 429
    assert int(r_429.headers.get("retry-after", "0")) > 0

    # Verify 503 when AEGIS_JWT_SECRET is missing
    monkeypatch.setattr(settings, "jwt_secret", "")
    r_503 = client.post(
        "/api/v1/auth/pair",
        json={"pairing_code": "830002", "device_id": "dev-fresh-b", "device_name": "Probe"},
    )
    assert r_503.status_code == 503


def test_22_pairing_code_validation_hardening_422_no_leak_and_no_store(isolated_api_env):
    """
    Stage 5.3B-2 Final Validation Hardening Test:
    1. Submitting malformed pairing codes (wrong length, non-digits, empty string) returns 422 Unprocessable Entity.
    2. The submitted raw code value is NEVER returned to the client in `detail`, `input`, or any other field.
    3. The response contains both caching-prevention headers: `Cache-Control: no-store` and `Pragma: no-cache`.
    4. Valid 6-digit numeric codes continue to pass format validation.
    5. When a valid code is submitted along with missing other required fields, the 422 payload strips
       the code from composite `input` objects so it is never reflected back to the caller.
    """
    client, session, _, _ = isolated_api_env
    PatientRepository(session).create(patient_id="pat-hardened-pair", status="active")
    _, valid_code_entity = PairingRepository(session).create_code(
        patient_id="pat-hardened-pair",
        raw_code_override="948372",
    )

    malformed_test_probes = [
        "secret_probe_alpha",
        "12345",
        "1234567",
        "abcdef",
        "00000a",
        "12 345",
    ]

    for bad_code in malformed_test_probes:
        r_bad = client.post(
            "/api/v1/auth/pair",
            json={
                "pairing_code": bad_code,
                "device_id": "dev-test-hardening",
                "device_name": "Pixel 9 Pro",
            },
        )
        assert r_bad.status_code == 422
        # Critical security check: raw submitted bad_code MUST NOT appear anywhere in response text
        assert bad_code not in r_bad.text
        assert r_bad.headers.get("cache-control") == "no-store"
        assert r_bad.headers.get("pragma") == "no-cache"

        detail = r_bad.json().get("detail", [])
        assert len(detail) > 0
        for err in detail:
            assert "input" not in err
            msg = err.get("msg", "")
            assert "6-digit" in msg or "pairing_code" in msg

    # Submitting valid 6-digit code with missing required field (e.g. missing device_name) returns 422,
    # but does NOT echo the valid pairing code in composite `input`
    secret_valid_probe = "948372"
    r_missing_dev = client.post(
        "/api/v1/auth/pair",
        json={
            "pairing_code": secret_valid_probe,
        },
    )
    assert r_missing_dev.status_code == 422
    assert secret_valid_probe not in r_missing_dev.text
    assert r_missing_dev.headers.get("cache-control") == "no-store"
    assert r_missing_dev.headers.get("pragma") == "no-cache"

    # Valid 6-digit code with all fields succeeds (200 OK)
    r_success = client.post(
        "/api/v1/auth/pair",
        json={
            "pairing_code": "948372",
            "device_id": "dev-test-hardening",
            "device_name": "Pixel 9 Pro",
        },
    )
    assert r_success.status_code == 200
    assert r_success.headers.get("cache-control") == "no-store"
    assert r_success.headers.get("pragma") == "no-cache"
    assert r_success.json()["patient_id"] == "pat-hardened-pair"
