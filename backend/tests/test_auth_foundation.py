import base64
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta
import io
import json
from pathlib import Path
import sqlite3
import threading
import uuid
import jwt
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, event, func, inspect, select
from sqlalchemy.orm import sessionmaker, Session

from app.cli.pairing import run_cli
from app.config import settings
from app.models.auth_rate_limit import AuthRateLimit
from app.models.base import Base
from app.models.device import Device
from app.models.device_refresh_token import DeviceRefreshToken
from app.models.health_measurement import HealthMeasurement
from app.models.medical_cycle import MedicalCycle
from app.models.model_report import ModelReport
from app.models.pairing_code import PairingCode
from app.models.patient import Patient
from app.repositories.device_repository import DeviceRepository
from app.repositories.pairing_repository import PairingRepository
from app.repositories.patient_repository import PatientRepository
from app.repositories.token_repository import DeviceTokenRepository
from app.security.rate_limiter import (
    PersistentRateLimiter,
    RateLimitConfig,
    RateLimitExceededError,
    RateLimiterUnavailableError,
    resolve_client_ip,
)
from app.security.tokens import (
    TokenConfigurationError,
    TokenExpiredError,
    TokenValidationError,
    create_access_token,
    resolve_jwt_secret,
    verify_access_token,
)
from app.services.auth_service import AuthService

TEST_PAIRING_SECRET = "test-stage-5-3b1-pairing-hmac-secret-998877"
TEST_JWT_SECRET = "test-stage-5-3b1-jwt-signing-secret-key-665544"


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


@pytest.fixture
def isolated_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "pairing_secret", TEST_PAIRING_SECRET)
    monkeypatch.setattr(settings, "jwt_secret", TEST_JWT_SECRET)
    monkeypatch.setattr(settings, "jwt_issuer", "aegis-backend")
    monkeypatch.setattr(settings, "jwt_audience", "aegis-mobile-client")
    monkeypatch.setattr(settings, "trusted_proxy_ips", "")

    db_file = tmp_path / "test_auth_foundation.db"
    eng = _make_sqlite_engine(db_file)
    Base.metadata.create_all(bind=eng)
    SessionFactory = sessionmaker(autocommit=False, autoflush=False, bind=eng)
    session = SessionFactory()
    try:
        yield session, SessionFactory, eng, db_file
    finally:
        session.close()
        eng.dispose()


def test_01_jwt_valid_and_invalid_signature(monkeypatch: pytest.MonkeyPatch):
    """Verifies valid HS256 JWT issuance/verification and rejection of forged signatures."""
    monkeypatch.setattr(settings, "jwt_secret", TEST_JWT_SECRET)
    monkeypatch.setattr(settings, "pairing_secret", TEST_PAIRING_SECRET)

    issued = create_access_token(
        patient_id="patient-uuid-001",
        device_id="device-uuid-001",
        family_id="family-uuid-001",
        scope="vitals:read vitals:write cycle:read",
        ttl_minutes=30,
    )
    assert issued.expires_in == 1800
    assert issued.access_token.count(".") == 2

    claims = verify_access_token(issued.access_token)
    assert claims.patient_id == "patient-uuid-001"
    assert claims.device_id == "device-uuid-001"
    assert claims.family_id == "family-uuid-001"
    assert claims.iss == "aegis-backend"
    assert claims.aud == "aegis-mobile-client"
    assert claims.has_scope("vitals:write")
    assert claims.has_scope("cycle:read")
    assert not claims.has_scope("admin:all")

    # Wrong verification secret
    with pytest.raises(TokenValidationError):
        verify_access_token(
            issued.access_token,
            secret="wrong-jwt-signing-secret-key-0000000000",
        )

    # Tampered payload without re-signing
    parts = issued.access_token.split(".")
    payload_data = json.loads(base64.urlsafe_b64decode(parts[1] + "==="))
    payload_data["patient_id"] = "attacker-patient-id"
    tampered_payload = base64.urlsafe_b64encode(
        json.dumps(payload_data).encode("utf-8")
    ).decode("ascii").rstrip("=")
    forged_token = f"{parts[0]}.{tampered_payload}.{parts[2]}"

    with pytest.raises(TokenValidationError):
        verify_access_token(forged_token)


def test_02_jwt_alg_confusion_and_invalid_iss_aud(monkeypatch: pytest.MonkeyPatch):
    """Defeats alg='none', non-HS256 algorithms, invalid typ, and mismatched iss/aud."""
    monkeypatch.setattr(settings, "jwt_secret", TEST_JWT_SECRET)

    now_ts = int(datetime.now(timezone.utc).timestamp())
    valid_payload = {
        "iss": "aegis-backend",
        "aud": "aegis-mobile-client",
        "sub": "p-1",
        "patient_id": "p-1",
        "device_id": "d-1",
        "jti": "jti-1",
        "family_id": "fam-1",
        "scope": "vitals:read",
        "iat": now_ts,
        "exp": now_ts + 1800,
    }

    # 1. alg="none" unsigned token
    none_header = base64.urlsafe_b64encode(
        json.dumps({"alg": "none", "typ": "JWT"}).encode("utf-8")
    ).decode("ascii").rstrip("=")
    none_body = base64.urlsafe_b64encode(
        json.dumps(valid_payload).encode("utf-8")
    ).decode("ascii").rstrip("=")
    none_token = f"{none_header}.{none_body}."
    with pytest.raises(TokenValidationError, match="algorithm"):
        verify_access_token(none_token)

    # 2. alg="HS384" signed with same secret (must be rejected by fixed HS256 allowlist)
    hs384_token = jwt.encode(
        payload=valid_payload,
        key=TEST_JWT_SECRET,
        algorithm="HS384",
        headers={"alg": "HS384", "typ": "JWT"},
    )
    with pytest.raises(TokenValidationError, match="algorithm"):
        verify_access_token(hs384_token)

    # 3. Wrong header typ
    bad_typ_token = jwt.encode(
        payload=valid_payload,
        key=TEST_JWT_SECRET,
        algorithm="HS256",
        headers={"alg": "HS256", "typ": "invalid"},
    )
    with pytest.raises(TokenValidationError, match="type"):
        verify_access_token(bad_typ_token)

    # 4. Wrong issuer
    bad_iss = create_access_token(
        patient_id="p-1",
        device_id="d-1",
        family_id="fam-1",
        issuer="evil-issuer",
    )
    with pytest.raises(TokenValidationError):
        verify_access_token(bad_iss.access_token)

    # 5. Wrong audience
    bad_aud = create_access_token(
        patient_id="p-1",
        device_id="d-1",
        family_id="fam-1",
        audience="wrong-audience",
    )
    with pytest.raises(TokenValidationError):
        verify_access_token(bad_aud.access_token)


def test_03_jwt_expired_rejected(monkeypatch: pytest.MonkeyPatch):
    """Rejects expired access tokens with TokenExpiredError."""
    monkeypatch.setattr(settings, "jwt_secret", TEST_JWT_SECRET)
    past = datetime.now(timezone.utc) - timedelta(minutes=45)
    issued = create_access_token(
        patient_id="p-exp",
        device_id="d-exp",
        family_id="fam-exp",
        ttl_minutes=30,
        now=past,
    )
    with pytest.raises(TokenExpiredError):
        verify_access_token(issued.access_token)


def test_04_jwt_missing_or_colliding_secret_fails_closed(monkeypatch: pytest.MonkeyPatch):
    """Fails closed when AEGIS_JWT_SECRET is empty, short, or collides with API/Pairing secrets."""
    monkeypatch.setattr(settings, "api_secret", "shared-api-secret-value-123456")
    monkeypatch.setattr(settings, "pairing_secret", "shared-pairing-secret-value-123456")

    # Empty secret
    monkeypatch.setattr(settings, "jwt_secret", "   ")
    with pytest.raises(TokenConfigurationError):
        resolve_jwt_secret()

    # Too short secret (< 16 chars)
    with pytest.raises(TokenConfigurationError):
        resolve_jwt_secret("short-secret")

    # Collides with AEGIS_API_SECRET
    with pytest.raises(TokenConfigurationError, match="AEGIS_API_SECRET"):
        resolve_jwt_secret("shared-api-secret-value-123456")

    # Collides with AEGIS_PAIRING_SECRET
    with pytest.raises(TokenConfigurationError, match="AEGIS_PAIRING_SECRET"):
        resolve_jwt_secret("shared-pairing-secret-value-123456")


def test_05_jwt_malformed_and_invalid_claims(monkeypatch: pytest.MonkeyPatch):
    """Rejects JWTs with missing claims, sub != patient_id, boolean timestamps, or empty scopes."""
    monkeypatch.setattr(settings, "jwt_secret", TEST_JWT_SECRET)
    now_ts = int(datetime.now(timezone.utc).timestamp())

    base_claims = {
        "iss": "aegis-backend",
        "aud": "aegis-mobile-client",
        "sub": "patient-1",
        "patient_id": "patient-1",
        "device_id": "device-1",
        "jti": "jti-1",
        "family_id": "fam-1",
        "scope": "vitals:read",
        "iat": now_ts,
        "exp": now_ts + 1800,
    }

    # sub != patient_id
    mismatched_sub = dict(base_claims, sub="patient-2")
    tok1 = jwt.encode(mismatched_sub, TEST_JWT_SECRET, algorithm="HS256")
    with pytest.raises(TokenValidationError, match="sub"):
        verify_access_token(tok1)

    # Missing family_id
    missing_fam = dict(base_claims)
    del missing_fam["family_id"]
    tok2 = jwt.encode(missing_fam, TEST_JWT_SECRET, algorithm="HS256")
    with pytest.raises(TokenValidationError):
        verify_access_token(tok2)

    # Empty device_id
    empty_dev = dict(base_claims, device_id="   ")
    tok3 = jwt.encode(empty_dev, TEST_JWT_SECRET, algorithm="HS256")
    with pytest.raises(TokenValidationError, match="device_id"):
        verify_access_token(tok3)

    # Future iat (> now + 60s)
    future_iat = dict(base_claims, iat=now_ts + 600, exp=now_ts + 2400)
    tok4 = jwt.encode(future_iat, TEST_JWT_SECRET, algorithm="HS256")
    with pytest.raises(TokenValidationError, match="future"):
        verify_access_token(tok4)


def test_06_atomic_pairing_device_refresh_and_jwt_success_and_repairing(isolated_db):
    """
    Verifies atomic pairing operation (PairingCode consumption + Device registration +
    RefreshToken persistence + JWT issuance) and safe recovery on re-pairing.
    """
    session, _, _, _ = isolated_db
    patient = PatientRepository(session).create(patient_id="patient-atomic-01")
    pairing_repo = PairingRepository(session)
    raw_code_1, _ = pairing_repo.create_code(
        patient_id=patient.patient_id,
        raw_code_override="048219",
    )

    auth_service = AuthService(session)
    ok, msg, bundle_1 = auth_service.pair_device_and_issue_tokens(
        raw_code=raw_code_1,
        device_name="Galaxy Watch Companion",
        platform="android",
        device_id="device-atomic-01",
    )
    assert ok is True, msg
    assert bundle_1 is not None
    assert bundle_1.patient_id == patient.patient_id
    assert bundle_1.device_id == "device-atomic-01"
    assert bundle_1.pairing_code.is_consumed is True
    assert bundle_1.pairing_code.active_code_hash is None

    verified_claims = verify_access_token(bundle_1.access_token)
    assert verified_claims.patient_id == patient.patient_id
    assert verified_claims.device_id == "device-atomic-01"
    assert verified_claims.family_id == bundle_1.refresh_token_entity.family_id

    # Simulate uncertain outcome recovery: re-pairing same device with a new code
    # revokes old refresh token atomically and issues a single new active refresh token.
    raw_code_2, _ = pairing_repo.create_code(
        patient_id=patient.patient_id,
        raw_code_override="091523",
    )
    ok2, _, bundle_2 = auth_service.pair_device_and_issue_tokens(
        raw_code=raw_code_2,
        device_name="Galaxy Watch Companion Updated",
        platform="android",
        device_id="device-atomic-01",
    )
    assert ok2 is True
    assert bundle_2 is not None

    all_tokens = DeviceTokenRepository(session).list_by_device("device-atomic-01")
    assert len(all_tokens) == 2
    active_tokens = [t for t in all_tokens if not t.is_revoked]
    assert len(active_tokens) == 1
    assert active_tokens[0].token_id == bundle_2.refresh_token_entity.token_id


def test_07_atomic_pairing_rollback_on_jwt_or_device_error_preserves_pairing_code(isolated_db):
    """
    Verifies that if JWT signing fails or device ownership conflicts during pairing,
    the entire transaction rolls back:
    - PairingCode does NOT burn (consumed_at IS NULL, active_code_hash intact);
    - No Device or DeviceRefreshToken is left behind;
    - The same code can subsequently succeed once the error is resolved.
    """
    session, _, _, _ = isolated_db
    patient_a = PatientRepository(session).create(patient_id="patient-rb-A")
    patient_b = PatientRepository(session).create(patient_id="patient-rb-B")

    # Pre-register device-owned-by-B
    DeviceRepository(session).register_device(
        patient_id=patient_b.patient_id,
        device_name="Patient B Phone",
        device_id="device-owned-by-B",
    )

    pairing_repo = PairingRepository(session)
    raw_code, code_entity = pairing_repo.create_code(
        patient_id=patient_a.patient_id,
        raw_code_override="005511",
    )

    auth_service = AuthService(session)

    # Failure Case 1: JWT creation throws an exception after code verify + device + refresh flush
    def _failing_jwt_factory(**_kwargs):
        raise RuntimeError("Simulated HSM / JWT signing failure")

    with pytest.raises(RuntimeError, match="Simulated HSM"):
        auth_service.pair_device_and_issue_tokens(
            raw_code=raw_code,
            device_name="Patient A Phone",
            device_id="device-rb-A",
            access_token_factory=_failing_jwt_factory,
        )

    session.expire_all()
    code_after_jwt_fail = session.get(PairingCode, code_entity.code_id)
    assert code_after_jwt_fail is not None
    assert code_after_jwt_fail.consumed_at is None
    assert code_after_jwt_fail.active_code_hash == code_after_jwt_fail.code_hash
    assert DeviceRepository(session).get("device-rb-A") is None
    assert len(DeviceTokenRepository(session).list_by_device("device-rb-A")) == 0

    # Failure Case 2: Device ownership conflict (trying to claim device-owned-by-B)
    with pytest.raises(ValueError, match="already registered to a different patient"):
        auth_service.pair_device_and_issue_tokens(
            raw_code=raw_code,
            device_name="Hijack Attempt",
            device_id="device-owned-by-B",
        )

    session.expire_all()
    code_after_dev_fail = session.get(PairingCode, code_entity.code_id)
    assert code_after_dev_fail.consumed_at is None
    assert code_after_dev_fail.active_code_hash == code_after_dev_fail.code_hash

    # Now retry with the exact same unburned code and valid parameters -> succeeds!
    ok, _, bundle = auth_service.pair_device_and_issue_tokens(
        raw_code=raw_code,
        device_name="Patient A Phone",
        device_id="device-rb-A",
    )
    assert ok is True
    assert bundle is not None
    assert bundle.device.device_id == "device-rb-A"


def test_08_refresh_token_rotation_and_commit_false_semantics(isolated_db):
    """Verifies atomic refresh token rotation and that commit=False never commits outer transactions."""
    session, _, _, _ = isolated_db
    patient = PatientRepository(session).create(patient_id="patient-rot-01")
    pairing_repo = PairingRepository(session)
    raw_code, _ = pairing_repo.create_code(patient_id=patient.patient_id)

    auth_service = AuthService(session)
    ok, _, bundle = auth_service.pair_device_and_issue_tokens(
        raw_code=raw_code,
        device_name="Phone Rot",
        device_id="device-rot-01",
    )
    assert ok is True

    # Test rollback when JWT signing fails during refresh_device_tokens
    def _failing_jwt(**_kwargs):
        raise RuntimeError("Refresh JWT signing failed")

    with pytest.raises(RuntimeError, match="Refresh JWT signing failed"):
        auth_service.refresh_device_tokens(
            device_id="device-rot-01",
            old_raw_refresh_token=bundle.refresh_token,
            access_token_factory=_failing_jwt,
        )

    # Original refresh token must STILL be active and unrevoked after rollback!
    session.expire_all()
    orig_token = session.get(DeviceRefreshToken, bundle.refresh_token_entity.token_id)
    assert orig_token.is_revoked is False

    # Now perform valid refresh rotation
    ok_ref, msg_ref, refreshed = auth_service.refresh_device_tokens(
        device_id="device-rot-01",
        old_raw_refresh_token=bundle.refresh_token,
    )
    assert ok_ref is True, msg_ref
    assert refreshed is not None
    assert refreshed.refresh_token != bundle.refresh_token
    assert refreshed.refresh_token_entity.family_id == bundle.refresh_token_entity.family_id

    session.expire_all()
    orig_token_after = session.get(DeviceRefreshToken, bundle.refresh_token_entity.token_id)
    assert orig_token_after.is_revoked is True
    assert orig_token_after.replaced_by_token_id == refreshed.refresh_token_entity.token_id


def test_09_refresh_token_reuse_detection_revokes_entire_family(isolated_db):
    """Presenting an already-rotated refresh token immediately revokes the entire token family."""
    session, _, _, _ = isolated_db
    patient = PatientRepository(session).create(patient_id="patient-reuse-01")
    raw_code, _ = PairingRepository(session).create_code(patient_id=patient.patient_id)

    auth_service = AuthService(session)
    _, _, paired = auth_service.pair_device_and_issue_tokens(
        raw_code=raw_code,
        device_name="Phone Reuse",
        device_id="device-reuse-01",
    )
    token_1 = paired.refresh_token

    # Legitimate rotation 1 -> 2
    ok, _, rot_2 = auth_service.refresh_device_tokens(
        device_id="device-reuse-01",
        old_raw_refresh_token=token_1,
    )
    assert ok is True
    token_2 = rot_2.refresh_token

    # Replay of already-rotated token_1 -> triggers reuse detection!
    ok_replay, msg_replay, _ = auth_service.refresh_device_tokens(
        device_id="device-reuse-01",
        old_raw_refresh_token=token_1,
    )
    assert ok_replay is False
    assert "reuse detected" in msg_replay.lower()

    # Now token_2 is ALSO revoked!
    ok_t2, _, _ = auth_service.refresh_device_tokens(
        device_id="device-reuse-01",
        old_raw_refresh_token=token_2,
    )
    assert ok_t2 is False

    active_remaining = session.execute(
        select(func.count(DeviceRefreshToken.token_id)).where(
            DeviceRefreshToken.device_id == "device-reuse-01",
            DeviceRefreshToken.revoked_at.is_(None),
        )
    ).scalar_one()
    assert active_remaining == 0


def test_10_concurrent_refresh_and_device_revoke_race_leaves_zero_active_tokens(isolated_db):
    """
    Simulates concurrent execution of:
    Thread A: rotate_refresh_token()
    Thread B: DeviceRepository.revoke_device()
    Across multiple iterations, guarantees that after both operations finish,
    the revoked device has ZERO active refresh tokens.
    """
    session, SessionFactory, _, _ = isolated_db
    patient = PatientRepository(session).create(patient_id="patient-race-01")

    for i in range(10):
        dev_id = f"device-race-{i:02d}"
        raw_code, _ = PairingRepository(session).create_code(patient_id=patient.patient_id)
        _, _, paired = AuthService(session).pair_device_and_issue_tokens(
            raw_code=raw_code,
            device_name=f"Race Phone {i}",
            device_id=dev_id,
        )
        initial_refresh = paired.refresh_token

        barrier = threading.Barrier(2)

        def _worker_refresh(target_dev_id: str, raw_tok: str):
            s = SessionFactory()
            try:
                barrier.wait(timeout=5)
                DeviceTokenRepository(s).rotate_refresh_token(
                    device_id=target_dev_id,
                    old_raw_token=raw_tok,
                )
            finally:
                s.close()

        def _worker_revoke(target_dev_id: str):
            s = SessionFactory()
            try:
                barrier.wait(timeout=5)
                DeviceRepository(s).revoke_device(target_dev_id)
            finally:
                s.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            f1 = pool.submit(_worker_refresh, dev_id, initial_refresh)
            f2 = pool.submit(_worker_revoke, dev_id)
            f1.result(timeout=10)
            f2.result(timeout=10)

        session.expire_all()
        dev_after = DeviceRepository(session).get(dev_id)
        assert dev_after is not None
        assert dev_after.is_active is False
        assert dev_after.revoked_at is not None

        active_tokens = session.execute(
            select(func.count(DeviceRefreshToken.token_id)).where(
                DeviceRefreshToken.device_id == dev_id,
                DeviceRefreshToken.revoked_at.is_(None),
            )
        ).scalar_one()
        assert active_tokens == 0, f"Iteration {i}: found {active_tokens} active tokens on revoked device!"


def test_11_suspended_or_archived_patient_blocks_refresh_and_revokes_tokens(isolated_db):
    """Verifies that suspending a patient revokes active refresh tokens and blocks refresh/issuance."""
    session, _, _, _ = isolated_db
    patient_repo = PatientRepository(session)
    patient = patient_repo.create(patient_id="patient-susp-01")
    raw_code, _ = PairingRepository(session).create_code(patient_id=patient.patient_id)

    auth_service = AuthService(session)
    _, _, bundle = auth_service.pair_device_and_issue_tokens(
        raw_code=raw_code,
        device_name="Phone Susp",
        device_id="device-susp-01",
    )

    # Suspend patient -> atomically revokes active refresh tokens of patient's devices
    patient_repo.update_status(patient.patient_id, "suspended")

    ok_ref, msg_ref, _ = auth_service.refresh_device_tokens(
        device_id="device-susp-01",
        old_raw_refresh_token=bundle.refresh_token,
    )
    assert ok_ref is False
    assert "patient is not active" in msg_ref.lower()

    with pytest.raises(ValueError, match="not active"):
        DeviceTokenRepository(session).issue_refresh_token(device_id="device-susp-01")


def test_12_concurrent_refresh_rotation_never_leaves_two_active_successors(isolated_db):
    """
    Verifies that when 5 parallel threads attempt to rotate the exact same refresh token:
    - At most 1 rotation can succeed before reuse detection triggers;
    - Once the losing threads trigger reuse detection on the consumed token, the family is revoked;
    - Under NO circumstances are two active replacement tokens ever left in the database.
    """
    session, SessionFactory, _, _ = isolated_db
    patient = PatientRepository(session).create(patient_id="patient-conc-rot")
    raw_code, _ = PairingRepository(session).create_code(patient_id=patient.patient_id)
    _, _, paired = AuthService(session).pair_device_and_issue_tokens(
        raw_code=raw_code,
        device_name="Concurrent Phone",
        device_id="device-conc-rot",
    )
    shared_token = paired.refresh_token

    barrier = threading.Barrier(5)

    def _attempt_rotate():
        s = SessionFactory()
        try:
            barrier.wait(timeout=5)
            ok, msg, _ = DeviceTokenRepository(s).rotate_refresh_token(
                device_id="device-conc-rot",
                old_raw_token=shared_token,
            )
            return ok, msg
        finally:
            s.close()

    with ThreadPoolExecutor(max_workers=5) as pool:
        results = [f.result(timeout=10) for f in [pool.submit(_attempt_rotate) for _ in range(5)]]

    success_count = sum(1 for ok, _ in results if ok)
    assert success_count == 1

    # Because the 4 losing threads presented the already-consumed token, reuse detection
    # immediately revoked the entire lineage so 0 active tokens remain.
    session.expire_all()
    active_count = session.execute(
        select(func.count(DeviceRefreshToken.token_id)).where(
            DeviceRefreshToken.device_id == "device-conc-rot",
            DeviceRefreshToken.revoked_at.is_(None),
        )
    ).scalar_one()
    assert active_count == 0


def test_13_persistent_rate_limiter_ip_device_lockout_and_restart_survival(isolated_db):
    """
    Verifies per-IP and per-device lockouts, Retry-After calculation, survival across
    limiter/session restarts, and expired bucket cleanup.
    """
    session, SessionFactory, eng, _ = isolated_db
    cfg = RateLimitConfig(
        ip_max_failures=3,
        ip_window_seconds=600,
        ip_lockout_seconds=900,
        device_max_failures=3,
        device_window_seconds=600,
        device_lockout_seconds=900,
        global_max_failures=50,
        stale_retention_seconds=3600,
    )
    now_dt = datetime(2026, 10, 25, 12, 0, 0, tzinfo=timezone.utc)
    limiter = PersistentRateLimiter(SessionFactory, config=cfg, now_provider=lambda: now_dt)

    # 3 failed attempts from IP 10.0.0.5
    for _ in range(3):
        limiter.acquire_attempt("10.0.0.5", "dev-1")

    # Simulate backend restart by instantiating a brand new PersistentRateLimiter on engine
    limiter_after_restart = PersistentRateLimiter(eng, config=cfg, now_provider=lambda: now_dt)
    with pytest.raises(RateLimitExceededError) as exc_info:
        limiter_after_restart.acquire_attempt("10.0.0.5", "dev-2")

    assert exc_info.value.scope == "ip"
    assert exc_info.value.retry_after == 900

    # Different IP but same locked device_id ("dev-1") is caught by auxiliary device bucket
    with pytest.raises(RateLimitExceededError) as dev_exc:
        limiter_after_restart.acquire_attempt("10.0.0.99", "dev-1")
    assert dev_exc.value.scope == "device"
    assert dev_exc.value.retry_after == 900

    # Advance clock past lockout (15m + 1s) -> allowed again!
    after_lockout = now_dt + timedelta(seconds=901)
    ticket = limiter_after_restart.acquire_attempt("10.0.0.5", "dev-1", now=after_lockout)
    assert ticket.client_ip == "10.0.0.5"

    # Advance clock past stale_retention_seconds and verify cleanup_expired
    far_future = after_lockout + timedelta(seconds=7200)
    deleted_count = limiter_after_restart.cleanup_expired(now=far_future)
    assert deleted_count >= 2


def test_14_concurrent_rate_limiter_requests_cannot_bypass_limits(isolated_db):
    """
    Verifies that 15 concurrent requests arriving simultaneously cannot bypass
    an ip_max_failures=5 threshold. Exactly 5 acquire attempt slots and 10 fail with 429.
    """
    _, SessionFactory, _, _ = isolated_db
    cfg = RateLimitConfig(ip_max_failures=5, global_max_failures=50)
    limiter = PersistentRateLimiter(SessionFactory, config=cfg)

    barrier = threading.Barrier(15)

    def _worker(idx: int):
        barrier.wait(timeout=5)
        try:
            limiter.acquire_attempt("192.168.10.25", f"dev-{idx}")
            return "allowed"
        except RateLimitExceededError:
            return "blocked"

    with ThreadPoolExecutor(max_workers=15) as pool:
        outcomes = [f.result(timeout=10) for f in [pool.submit(_worker, i) for i in range(15)]]

    assert outcomes.count("allowed") == 5
    assert outcomes.count("blocked") == 10


def test_15_spoofed_ip_headers_ignored_by_default():
    """
    Verifies that X-Forwarded-For and X-Real-IP headers are ignored by default,
    and only trusted when client_host matches AEGIS_TRUSTED_PROXIES.
    """
    spoofed_headers = {
        "X-Forwarded-For": f"203.0.113.{uuid.uuid4().int % 200}, 198.51.100.1",
        "X-Real-IP": "198.51.100.77",
    }
    # Default: trusted_proxies="" -> ignores headers and returns socket peer IP
    resolved = resolve_client_ip("10.10.1.50", spoofed_headers, trusted_proxies="")
    assert resolved == "10.10.1.50"

    # Untrusted peer sending XFF when trusted proxy is 127.0.0.1 -> still ignored!
    resolved_untrusted = resolve_client_ip("10.10.1.50", spoofed_headers, trusted_proxies="127.0.0.1")
    assert resolved_untrusted == "10.10.1.50"

    # Trusted proxy 127.0.0.1 forwarding request -> extracts rightmost non-trusted hop
    resolved_trusted = resolve_client_ip(
        "127.0.0.1",
        {"X-Forwarded-For": "203.0.113.5, 198.51.100.9, 127.0.0.1"},
        trusted_proxies="127.0.0.1/8",
    )
    assert resolved_trusted == "198.51.100.9"


def test_16_global_rate_limit_budget_and_non_reset_on_single_valid_code(isolated_db):
    """
    Verifies that:
    1. Repeated requests from an already-locked IP do NOT exhaust the global bucket.
    2. Distributed attacks from distinct IPs and devices hit the global failure cap.
    3. A single valid pairing code only refunds 1 slot on pair:global and NEVER resets
       prior accumulated global failures.
    """
    session, SessionFactory, _, _ = isolated_db
    cfg = RateLimitConfig(
        ip_max_failures=2,
        device_max_failures=2,
        global_max_failures=5,
        global_lockout_seconds=300,
    )
    limiter = PersistentRateLimiter(SessionFactory, config=cfg)

    # 1. Attacker IP 1.1.1.1 hits per-IP limit (2 failures -> global=2)
    limiter.acquire_attempt("1.1.1.1", "bot-dev-1")
    limiter.acquire_attempt("1.1.1.1", "bot-dev-2")

    # 10 more blocked attempts from banned IP 1.1.1.1 must NOT increment global bucket!
    for _ in range(10):
        with pytest.raises(RateLimitExceededError) as exc:
            limiter.acquire_attempt("1.1.1.1", "bot-dev-x")
        assert exc.value.scope == "ip"

    session.expire_all()
    global_row = session.get(AuthRateLimit, "pair:global")
    assert global_row.failures_count == 2

    # 2. Legitimate patient pairs successfully from IP 2.2.2.2 -> global increases by 1 then refunds 1 (stays 2!)
    patient = PatientRepository(session).create(patient_id="patient-glob-1")
    raw_code, _ = PairingRepository(session).create_code(patient_id=patient.patient_id)
    auth_service = AuthService(session, rate_limiter=limiter)
    ok, _, _ = auth_service.pair_device_and_issue_tokens(
        raw_code=raw_code,
        device_name="Legit Phone",
        device_id="legit-dev-1",
        client_ip="2.2.2.2",
    )
    assert ok is True

    session.expire_all()
    global_row_after_success = session.get(AuthRateLimit, "pair:global")
    assert global_row_after_success.failures_count == 2  # Prior failures NOT wiped!

    # 3. Distributed botnet sends 3 more wrong attempts from unique IPs -> hits global_max_failures=5
    limiter.acquire_attempt("3.3.3.1", "bot-3")
    limiter.acquire_attempt("3.3.3.2", "bot-4")
    limiter.acquire_attempt("3.3.3.3", "bot-5")

    with pytest.raises(RateLimitExceededError) as glob_exc:
        limiter.acquire_attempt("4.4.4.4", "bot-6")
    assert glob_exc.value.scope == "global"
    assert glob_exc.value.retry_after == 300

    # 4. Fail-closed verification if storage is corrupted/dropped
    AuthRateLimit.__table__.drop(bind=session.get_bind())
    with pytest.raises(RateLimiterUnavailableError):
        limiter.acquire_attempt("5.5.5.5", "dev-fail-closed")


def test_17_migration_0004_upgrade_and_downgrade_preserves_clinical_history(tmp_path: Path):
    """
    Tests Alembic upgrade to 0004_auth_rate_limits and downgrade to 0003 on an isolated SQLite DB,
    verifying zero loss of medical_cycles, health_measurements, and model_reports.
    """
    db_path = tmp_path / "migration_0004_test.db"
    alembic_ini = Path(__file__).resolve().parent.parent / "alembic.ini"
    cfg = Config(str(alembic_ini))
    cfg.set_main_option("script_location", str(alembic_ini.parent / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path.as_posix()}")

    # 1. Upgrade to 0003 and seed clinical data
    command.upgrade(cfg, "0003_identity_and_device_pairing")
    eng = _make_sqlite_engine(db_path)
    SessionLocal = sessionmaker(bind=eng)
    with SessionLocal() as s:
        pat = Patient(patient_id="pat-mig-0004", status="active")
        s.add(pat)
        s.flush()
        cyc = MedicalCycle(cycle_id="cycle-mig-0004", patient_id=pat.patient_id, status="active")
        s.add(cyc)
        s.flush()
        meas = HealthMeasurement(
            cycle_id=cyc.cycle_id,
            pulse=72,
            systolic=120,
            diastolic=80,
            spo2=98,
            source="watch_sensor",
            measured_at=datetime.now(timezone.utc),
        )
        rep = ModelReport(
            report_id="rep-mig-0004",
            cycle_id=cyc.cycle_id,
            model="gemini-3.1-pro-preview",
            report_type="primary_consilium",
            privacy_level="client_facing",
            version=1,
            content={"summary": "Clinical route preserved."},
        )
        s.add_all([meas, rep])
        s.commit()

    # 2. Upgrade to 0004 (head)
    command.upgrade(cfg, "0004_auth_rate_limits")
    inspector = inspect(eng)
    assert "auth_rate_limits" in inspector.get_table_names()

    with SessionLocal() as s:
        assert s.get(MedicalCycle, "cycle-mig-0004") is not None
        assert s.get(ModelReport, "rep-mig-0004") is not None
        assert s.execute(select(func.count(HealthMeasurement.id))).scalar_one() == 1

    # 3. Downgrade back to 0003 and verify clinical records remain intact
    command.downgrade(cfg, "0003_identity_and_device_pairing")
    inspector_after = inspect(eng)
    assert "auth_rate_limits" not in inspector_after.get_table_names()

    with SessionLocal() as s:
        assert s.get(MedicalCycle, "cycle-mig-0004") is not None
        assert s.get(ModelReport, "rep-mig-0004") is not None
        assert s.execute(select(func.count(HealthMeasurement.id))).scalar_one() == 1

    eng.dispose()


def test_18_admin_cli_operations_and_production_guard(isolated_db):
    """
    Tests all local Admin CLI commands (create-patient, issue-code, assign-cycle,
    list-devices, revoke-device) in isolated test mode and verifies the production
    mutation confirmation guard.
    """
    session, _, _, db_file = isolated_db
    db_url = f"sqlite:///{db_file.as_posix()}"

    # Create a legacy unassigned cycle
    legacy_cycle = MedicalCycle(cycle_id="legacy-cycle-100", patient_id=None, status="active")
    session.add(legacy_cycle)
    session.commit()

    # 1. Production guard blocks mutation when env != testing without --confirm-production
    err_buf = io.StringIO()
    rc_blocked = run_cli(
        ["--db-url", db_url, "--env", "production", "create-patient", "--patient-id", "pat-blocked"],
        stderr=err_buf,
    )
    assert rc_blocked == 2
    assert "confirm-production" in err_buf.getvalue()
    assert PatientRepository(session).get("pat-blocked") is None

    # 2. Create patient in testing env
    out_create = io.StringIO()
    rc_create = run_cli(
        ["--db-url", db_url, "--env", "testing", "create-patient", "--patient-id", "pat-cli-01"],
        stdout=out_create,
    )
    assert rc_create == 0
    created_data = json.loads(out_create.getvalue())
    assert created_data["patient_id"] == "pat-cli-01"

    # 3. Issue 6-digit pairing code via CLI
    out_code = io.StringIO()
    rc_code = run_cli(
        ["--db-url", db_url, "--env", "testing", "issue-code", "--patient-id", "pat-cli-01"],
        stdout=out_code,
    )
    assert rc_code == 0
    code_data = json.loads(out_code.getvalue())
    raw_code = code_data["pairing_code"]
    assert len(raw_code) == 6 and raw_code.isdigit()

    # Pair a device using the CLI-issued code
    session.expire_all()
    ok, _, bundle = AuthService(session).pair_device_and_issue_tokens(
        raw_code=raw_code,
        device_name="CLI Test Watch",
        device_id="dev-cli-01",
    )
    assert ok is True and bundle is not None

    # 4. Assign legacy cycle via CLI
    out_assign = io.StringIO()
    rc_assign = run_cli(
        [
            "--db-url",
            db_url,
            "--env",
            "testing",
            "assign-cycle",
            "--cycle-id",
            "legacy-cycle-100",
            "--patient-id",
            "pat-cli-01",
        ],
        stdout=out_assign,
    )
    assert rc_assign == 0
    assign_data = json.loads(out_assign.getvalue())
    assert assign_data["patient_id"] == "pat-cli-01"

    # 5. List devices via CLI (verify no secrets or token hashes in output!)
    out_list = io.StringIO()
    rc_list = run_cli(
        ["--db-url", db_url, "--env", "testing", "list-devices", "--patient-id", "pat-cli-01"],
        stdout=out_list,
    )
    assert rc_list == 0
    raw_list_output = out_list.getvalue()
    assert TEST_PAIRING_SECRET not in raw_list_output
    assert TEST_JWT_SECRET not in raw_list_output
    assert bundle.refresh_token not in raw_list_output
    assert bundle.refresh_token_entity.token_hash not in raw_list_output
    list_data = json.loads(raw_list_output)
    assert list_data["count"] == 1
    assert list_data["devices"][0]["is_active"] is True
    assert list_data["devices"][0]["active_refresh_tokens"] == 1

    # 6. Revoke device via CLI
    out_rev = io.StringIO()
    rc_rev = run_cli(
        ["--db-url", db_url, "--env", "testing", "revoke-device", "--device-id", "dev-cli-01"],
        stdout=out_rev,
    )
    assert rc_rev == 0
    rev_data = json.loads(out_rev.getvalue())
    assert rev_data["is_active"] is False

    # Verify active_refresh_tokens is now 0
    out_list_after = io.StringIO()
    run_cli(
        ["--db-url", db_url, "--env", "testing", "list-devices", "--patient-id", "pat-cli-01"],
        stdout=out_list_after,
    )
    assert json.loads(out_list_after.getvalue())["devices"][0]["active_refresh_tokens"] == 0


def test_19_limiter_valid_and_invalid_code_on_fifth_attempt_and_sixth_attempt_after_lockout(
    isolated_db,
):
    """
    Boundary tests for the 5-attempt threshold (Per-IP and Per-Device):
    1. Valid code on the 5th attempt (after 4 failed attempts) succeeds, consumes the code,
       and resets the caller's IP and Device buckets (`failures_count == 0`, `locked_until is None`).
    2. Invalid code on the 5th attempt (after 4 failed attempts) fails and locks both the IP
       and Device buckets (`failures_count == 5`, `locked_until == now + 900s`).
    3. 6th attempt immediately after lockout (even with a valid pairing code) raises
       RateLimitExceededError (`retry_after == 900`), leaves the valid pairing code unconsumed,
       and does not increment `pair:global`.
    """
    session, _, _, _ = isolated_db
    patient = PatientRepository(session).create(patient_id="pat-5th-boundary")
    pairing_repo = PairingRepository(session)
    auth_svc = AuthService(session)

    # --- Subcase 1: Valid code on the 5th attempt ---
    valid_code_1, _ = pairing_repo.create_code(
        patient_id=patient.patient_id, raw_code_override="515151"
    )
    ip_1 = "198.51.100.51"
    dev_1 = "dev-boundary-51"

    for _ in range(4):
        ok, _, bundle = auth_svc.pair_device_and_issue_tokens(
            raw_code="000000",
            device_name="Boundary Watch 1",
            device_id=dev_1,
            client_ip=ip_1,
        )
        assert ok is False and bundle is None

    # 5th attempt with the VALID code must succeed!
    ok5, _, bundle5 = auth_svc.pair_device_and_issue_tokens(
        raw_code=valid_code_1,
        device_name="Boundary Watch 1",
        device_id=dev_1,
        client_ip=ip_1,
    )
    assert ok5 is True and bundle5 is not None
    session.expire_all()
    ip_bucket_1 = session.get(AuthRateLimit, f"pair:ip:{ip_1}")
    dev_bucket_1 = session.get(AuthRateLimit, f"pair:device:{dev_1}")
    assert ip_bucket_1 is not None and ip_bucket_1.failures_count == 0 and ip_bucket_1.locked_until is None
    assert dev_bucket_1 is not None and dev_bucket_1.failures_count == 0 and dev_bucket_1.locked_until is None

    # --- Subcase 2: Invalid code on the 5th attempt ---
    valid_code_2, code_entity_2 = pairing_repo.create_code(
        patient_id=patient.patient_id, raw_code_override="525252"
    )
    ip_2 = "198.51.100.52"
    dev_2 = "dev-boundary-52"

    for attempt_idx in range(5):
        ok, _, bundle = auth_svc.pair_device_and_issue_tokens(
            raw_code="999999",
            device_name="Boundary Watch 2",
            device_id=dev_2,
            client_ip=ip_2,
        )
        assert ok is False and bundle is None
        session.expire_all()
        ip_b = session.get(AuthRateLimit, f"pair:ip:{ip_2}")
        assert ip_b is not None
        if attempt_idx < 4:
            assert ip_b.failures_count == attempt_idx + 1
            assert ip_b.locked_until is None
        else:
            # 5th failed attempt triggers 15-minute (900s) lockout
            assert ip_b.failures_count == 5
            assert ip_b.locked_until is not None

    dev_b = session.get(AuthRateLimit, f"pair:device:{dev_2}")
    assert dev_b is not None and dev_b.failures_count == 5 and dev_b.locked_until is not None
    global_before_6th = session.get(AuthRateLimit, "pair:global")
    assert global_before_6th is not None
    global_count_before_6th = global_before_6th.failures_count

    # --- Subcase 3: 6th attempt after lockout (even with valid code!) is rejected ---
    with pytest.raises(RateLimitExceededError) as exc_info:
        auth_svc.pair_device_and_issue_tokens(
            raw_code=valid_code_2,
            device_name="Boundary Watch 2",
            device_id=dev_2,
            client_ip=ip_2,
        )
    assert exc_info.value.scope == "ip"
    assert 1 <= exc_info.value.retry_after <= 900

    session.expire_all()
    refreshed_code_2 = session.get(PairingCode, code_entity_2.code_id)
    assert refreshed_code_2 is not None and refreshed_code_2.consumed_at is None
    global_after_6th = session.get(AuthRateLimit, "pair:global")
    assert global_after_6th is not None and global_after_6th.failures_count == global_count_before_6th


def test_20_concurrent_requests_at_global_rate_limit_boundary(isolated_db):
    """
    Tests concurrent pairing attempts at the boundary of `global_max_failures = 25`:
    - Pre-populates 20 global failed attempts across 5 distinct IPs (4 failures each).
    - Fires 12 concurrent requests from 12 new distinct IPs and 12 distinct device_ids.
    - Verifies that exactly 5 requests reach code verification (bringing `pair:global` to 25)
      and the remaining 7 concurrent requests are blocked with RateLimitExceededError(scope='global').
    """
    session, SessionLocal, _, _ = isolated_db
    patient = PatientRepository(session).create(patient_id="pat-global-conc")
    PairingRepository(session).create_code(patient_id=patient.patient_id, raw_code_override="606060")

    # Pre-populate 20 global failures (5 IPs x 4 failures each so none of those IPs are locked)
    init_svc = AuthService(session)
    for ip_idx in range(5):
        for _ in range(4):
            ok, _, _ = init_svc.pair_device_and_issue_tokens(
                raw_code="000000",
                device_name="Preload",
                device_id=f"preload-dev-{ip_idx}",
                client_ip=f"192.0.2.{10 + ip_idx}",
            )
            assert ok is False

    session.expire_all()
    g_bucket = session.get(AuthRateLimit, "pair:global")
    assert g_bucket is not None and g_bucket.failures_count == 20 and g_bucket.locked_until is None

    # Launch 12 concurrent requests from 12 fresh IPs and devices
    barrier = threading.Barrier(12)

    def _worker(idx: int) -> str:
        with SessionLocal() as s:
            svc = AuthService(s)
            barrier.wait(timeout=5.0)
            try:
                ok, _, _ = svc.pair_device_and_issue_tokens(
                    raw_code="111111",
                    device_name=f"Concurrent Watch {idx}",
                    device_id=f"conc-global-dev-{idx}",
                    client_ip=f"198.51.100.{100 + idx}",
                )
                return "verified_false" if not ok else "verified_true"
            except RateLimitExceededError as exc:
                return f"blocked:{exc.scope}"

    with ThreadPoolExecutor(max_workers=12) as pool:
        outcomes = list(pool.map(_worker, range(12)))

    assert outcomes.count("verified_false") == 5
    assert outcomes.count("blocked:global") == 7

    session.expire_all()
    g_bucket_after = session.get(AuthRateLimit, "pair:global")
    assert g_bucket_after is not None
    assert g_bucket_after.failures_count == 25
    assert g_bucket_after.locked_until is not None


def test_21_successful_pairing_preserves_other_clients_accumulated_failures(isolated_db):
    """
    Verifies that a successful pairing by Client C resets ONLY Client C's IP and Device
    buckets, refunds only Client C's single successful attempt slot on `pair:global`,
    and NEVER erases failures accumulated by Client A, Client B, or Client C's own prior
    failed attempts in `pair:global`.
    """
    session, _, _, _ = isolated_db
    patient = PatientRepository(session).create(patient_id="pat-isolation-success")
    raw_code, _ = PairingRepository(session).create_code(
        patient_id=patient.patient_id, raw_code_override="717171"
    )
    svc = AuthService(session)

    # Client A: 4 failed attempts
    for _ in range(4):
        svc.pair_device_and_issue_tokens(
            raw_code="000000",
            device_name="Client A",
            device_id="dev-client-A",
            client_ip="203.0.113.101",
        )

    # Client B: 3 failed attempts
    for _ in range(3):
        svc.pair_device_and_issue_tokens(
            raw_code="000000",
            device_name="Client B",
            device_id="dev-client-B",
            client_ip="203.0.113.102",
        )

    # Client C: 2 failed attempts followed by 1 successful attempt
    for _ in range(2):
        svc.pair_device_and_issue_tokens(
            raw_code="000000",
            device_name="Client C",
            device_id="dev-client-C",
            client_ip="203.0.113.103",
        )

    ok, _, bundle = svc.pair_device_and_issue_tokens(
        raw_code=raw_code,
        device_name="Client C",
        device_id="dev-client-C",
        client_ip="203.0.113.103",
    )
    assert ok is True and bundle is not None

    session.expire_all()
    # Client C reset
    assert session.get(AuthRateLimit, "pair:ip:203.0.113.103").failures_count == 0
    assert session.get(AuthRateLimit, "pair:device:dev-client-C").failures_count == 0
    # Client A & Client B untouched
    assert session.get(AuthRateLimit, "pair:ip:203.0.113.101").failures_count == 4
    assert session.get(AuthRateLimit, "pair:device:dev-client-A").failures_count == 4
    assert session.get(AuthRateLimit, "pair:ip:203.0.113.102").failures_count == 3
    assert session.get(AuthRateLimit, "pair:device:dev-client-B").failures_count == 3
    # Global bucket retains 4 (A) + 3 (B) + 2 (C failed) = 9 failures
    assert session.get(AuthRateLimit, "pair:global").failures_count == 9


def test_22_auth_service_requires_operational_rate_limiter_and_fails_closed_when_sqlite_unavailable(
    isolated_db, tmp_path: Path
):
    """
    Verifies mandatory rate-limiter enforcement (fail-closed):
    1. AuthService(db, rate_limiter=None) is forbidden and raises RateLimiterUnavailableError.
    2. AuthService(db, rate_limiter=<invalid>) raises RateLimiterUnavailableError.
    3. AuthService(db) when `auth_rate_limits` table is missing raises RateLimiterUnavailableError
       immediately upon instantiation.
    4. If the `auth_rate_limits` SQLite storage becomes unavailable after AuthService creation,
       `pair_device_and_issue_tokens()` fails closed with RateLimiterUnavailableError and leaves
       the pairing code unconsumed.
    """
    session, _, _, _ = isolated_db

    # 1. Explicit rate_limiter=None is forbidden
    with pytest.raises(RateLimiterUnavailableError, match="forbidden"):
        AuthService(session, rate_limiter=None)

    # 2. Invalid rate_limiter object is forbidden
    with pytest.raises(RateLimiterUnavailableError):
        AuthService(session, rate_limiter="invalid-limiter")

    # 3. Database missing `auth_rate_limits` table fails closed at AuthService construction
    no_limiter_db_path = tmp_path / "no_limiter_table.db"
    no_limiter_eng = _make_sqlite_engine(no_limiter_db_path)
    # Create all tables EXCEPT auth_rate_limits
    tables_without_limiter = [
        t for name, t in Base.metadata.tables.items() if name != "auth_rate_limits"
    ]
    Base.metadata.create_all(bind=no_limiter_eng, tables=tables_without_limiter)
    NoLimiterSession = sessionmaker(bind=no_limiter_eng)
    with NoLimiterSession() as s_no_lim:
        with pytest.raises(RateLimiterUnavailableError):
            AuthService(s_no_lim)
    no_limiter_eng.dispose()

    # 4. If `auth_rate_limits` table becomes unavailable after AuthService init, pairing is blocked
    patient = PatientRepository(session).create(patient_id="pat-limiter-down")
    raw_code, code_entity = PairingRepository(session).create_code(
        patient_id=patient.patient_id, raw_code_override="818181"
    )
    svc = AuthService(session)

    # Drop `auth_rate_limits` table to simulate SQLite limiter unavailability
    AuthRateLimit.__table__.drop(bind=session.get_bind())

    with pytest.raises(RateLimiterUnavailableError):
        svc.pair_device_and_issue_tokens(
            raw_code=raw_code,
            device_name="Blocked Watch",
            device_id="dev-blocked-limiter",
            client_ip="198.51.100.200",
        )

    session.expire_all()
    unchanged_code = session.get(PairingCode, code_entity.code_id)
    assert unchanged_code is not None and unchanged_code.consumed_at is None
    assert DeviceRepository(session).get("dev-blocked-limiter") is None


def test_23_post_commit_refresh_or_record_success_failure_preserves_committed_bundle(
    isolated_db, monkeypatch: pytest.MonkeyPatch
):
    """
    Verifies strict separation between pre-commit errors and post-commit best-effort hooks:
    1. If `db.refresh()` raises an exception AFTER `db.commit()` has succeeded in
       `pair_device_and_issue_tokens` or `refresh_device_tokens`, the service does NOT
       falsely report a rollback or discard the valid tokens; it returns `(True, ..., bundle)`
       with `post_commit_warnings` populated.
    2. If `rate_limiter.record_success()` raises an exception AFTER `db.commit()` has succeeded,
       `pair_device_and_issue_tokens` still returns `(True, ..., bundle)` with valid tokens
       matching the committed database state.
    """
    session, _, _, _ = isolated_db
    patient = PatientRepository(session).create(patient_id="pat-post-commit")
    pairing_repo = PairingRepository(session)

    # --- Case 1: Exception inside db.refresh() after commit in pair_device_and_issue_tokens ---
    raw_code_1, _ = pairing_repo.create_code(
        patient_id=patient.patient_id, raw_code_override="919191"
    )
    svc = AuthService(session)

    orig_refresh = session.refresh

    def _broken_refresh(_instance, *args, **kwargs):
        raise RuntimeError("Simulated post-commit ORM refresh failure")

    monkeypatch.setattr(session, "refresh", _broken_refresh)
    ok1, _, bundle1 = svc.pair_device_and_issue_tokens(
        raw_code=raw_code_1,
        device_name="PostCommit Watch 1",
        device_id="dev-post-commit-1",
        client_ip="198.51.100.210",
    )
    assert ok1 is True and bundle1 is not None
    assert len(bundle1.post_commit_warnings) == 1
    assert "Post-commit ORM refresh warning" in bundle1.post_commit_warnings[0]
    claims1 = verify_access_token(bundle1.access_token)
    assert claims1.patient_id == patient.patient_id
    assert claims1.device_id == "dev-post-commit-1"

    # Also verify refresh_device_tokens when db.refresh() raises after commit
    ok_rot, _, rot_bundle = svc.refresh_device_tokens(
        device_id="dev-post-commit-1",
        old_raw_refresh_token=bundle1.refresh_token,
    )
    assert ok_rot is True and rot_bundle is not None
    assert len(rot_bundle.post_commit_warnings) == 1
    assert "Post-commit ORM refresh warning" in rot_bundle.post_commit_warnings[0]
    monkeypatch.setattr(session, "refresh", orig_refresh)

    # --- Case 2: Exception inside rate_limiter.record_success() after commit ---
    raw_code_2, _ = pairing_repo.create_code(
        patient_id=patient.patient_id, raw_code_override="929292"
    )

    def _broken_record_success(_ticket, *args, **kwargs):
        raise RateLimiterUnavailableError("Simulated post-commit limiter write failure")

    monkeypatch.setattr(svc.rate_limiter, "record_success", _broken_record_success)
    ok2, _, bundle2 = svc.pair_device_and_issue_tokens(
        raw_code=raw_code_2,
        device_name="PostCommit Watch 2",
        device_id="dev-post-commit-2",
        client_ip="198.51.100.211",
    )
    assert ok2 is True and bundle2 is not None
    assert len(bundle2.post_commit_warnings) == 1
    assert "Post-commit rate limiter record_success warning" in bundle2.post_commit_warnings[0]
    claims2 = verify_access_token(bundle2.access_token)
    assert claims2.device_id == "dev-post-commit-2"


def test_24_revoked_device_never_receives_token_even_with_stale_orm_session_cache(isolated_db):
    """
    Verifies that even if a Session has already loaded a `Device` and `Patient` into its
    SQLAlchemy identity map while active, and another session subsequently revokes the
    device, neither `DeviceTokenRepository.issue_refresh_token` nor
    `AuthService.refresh_device_tokens` will issue a token to the revoked device.
    """
    session1, SessionLocal, _, _ = isolated_db
    patient = PatientRepository(session1).create(patient_id="pat-stale-cache")
    raw_code, _ = PairingRepository(session1).create_code(
        patient_id=patient.patient_id, raw_code_override="939393"
    )
    svc1 = AuthService(session1)
    ok, _, bundle = svc1.pair_device_and_issue_tokens(
        raw_code=raw_code,
        device_name="Cache Watch",
        device_id="dev-stale-cache",
        client_ip="198.51.100.220",
    )
    assert ok is True and bundle is not None

    # Warm session1's identity map with active Device and Patient
    cached_dev = session1.get(Device, "dev-stale-cache")
    assert cached_dev is not None and cached_dev.is_active is True

    # Revoke the device in a separate session (Session 2) without expiring session1
    with SessionLocal() as session2:
        DeviceRepository(session2).revoke_device("dev-stale-cache")

    # session1's in-memory object wasn't manually expired, yet populate_existing=True must catch revocation!
    with pytest.raises(ValueError, match="revoked or inactive"):
        DeviceTokenRepository(session1).issue_refresh_token(device_id="dev-stale-cache")

    ok_ref, msg_ref, ref_bundle = svc1.refresh_device_tokens(
        device_id="dev-stale-cache",
        old_raw_refresh_token=bundle.refresh_token,
    )
    assert ok_ref is False and ref_bundle is None
    assert "revoked or inactive" in msg_ref.lower()
