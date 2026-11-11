from datetime import datetime, timezone, timedelta
import json
import time
from typing import Any, Dict, List, Optional
import uuid

import jwt
import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import settings
from app.database import get_db
from app.main import app
from app.models.base import Base
from app.models.beta_whitelist import BetaAccessWhitelist
from app.models.device import Device
from app.models.device_refresh_token import DeviceRefreshToken
from app.models.patient import Patient
from app.models.patient_google_identity import PatientGoogleIdentity
from app.routers.admin_audit import _ephemeral_admin_sessions
from app.schemas.auth import GoogleAuthRequest
from app.security.rate_limiter import PersistentRateLimiter
from app.services.auth_service import AuthService, ClosedBetaAccessDeniedError
from app.services.google_auth_service import (
    GoogleAuthConfigError,
    GoogleAuthService,
    GoogleTokenVerificationError,
    VerifiedGoogleClaims,
    override_google_auth_service,
)


@pytest.fixture
def isolated_db_session():
    """Provides a pristine, isolated in-memory SQLite database session for Google auth tests."""
    old_jwt = settings.jwt_secret
    settings.jwt_secret = "test_super_secure_jwt_secret_with_32_characters_minimum"

    test_engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=test_engine)
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=test_engine)
    db = TestingSessionLocal()
    try:
        # Pre-initialize rate limiter table
        limiter = PersistentRateLimiter(db)
        limiter.verify_ready()
        yield db
    finally:
        db.close()
        test_engine.dispose()
        settings.jwt_secret = old_jwt


@pytest.fixture
def mock_google_claims_factory():
    """Creates a mock verifier that decodes a JWT or returns predictable mock claims."""
    def _create_mock_verifier(mock_claims: Optional[Dict[str, Any]] = None):
        def _verifier(token_str: str, request_obj: Any, audience: Any) -> Dict[str, Any]:
            if token_str in ("INVALID_SIGNATURE_TOKEN", "invalid.signature.token"):
                raise ValueError("Signature verification failed.")
            if token_str in ("EXPIRED_GOOGLE_TOKEN", "expired.google.token"):
                raise ValueError("Token is expired.")
            if mock_claims is not None:
                return dict(mock_claims)
            
            # Default payload if valid JWT format
            if token_str.count(".") == 2:
                try:
                    return jwt.decode(token_str, options={"verify_signature": False})
                except Exception as e:
                    raise ValueError(f"Malformed token: {e}")
            raise ValueError("Token format invalid.")
        return _verifier
    return _create_mock_verifier


@pytest.fixture(autouse=True)
def cleanup_overrides():
    """Resets GoogleAuthService and dependency overrides after every test."""
    yield
    override_google_auth_service(None)
    app.dependency_overrides.clear()


class TestGoogleTokenVerification:
    """Tests for cryptographic and semantic Google ID Token verification logic."""

    def test_verify_token_success(self, mock_google_claims_factory):
        mock_claims = {
            "iss": "https://accounts.google.com",
            "sub": "google_sub_1092837465",
            "aud": "aegis-mobile-client-id.apps.googleusercontent.com",
            "email": "test.patient@gmail.com",
            "email_verified": True,
            "exp": int(time.time()) + 3600,
            "iat": int(time.time()),
        }
        verifier = mock_google_claims_factory(mock_claims)
        service = GoogleAuthService(
            allowed_client_ids=["aegis-mobile-client-id.apps.googleusercontent.com"],
            custom_verifier=verifier,
        )

        claims = service.verify_token("header.payload.signature")
        assert claims.sub == "google_sub_1092837465"
        assert claims.iss == "https://accounts.google.com"
        assert claims.email == "test.patient@gmail.com"
        assert claims.email_verified is True
        assert claims.aud == "aegis-mobile-client-id.apps.googleusercontent.com"

    def test_verify_token_empty_or_blank_rejected(self):
        service = GoogleAuthService(allowed_client_ids=["test-client"])
        with pytest.raises(GoogleTokenVerificationError, match="missing or empty"):
            service.verify_token("")

        with pytest.raises(GoogleTokenVerificationError, match="missing or empty"):
            service.verify_token("   ")

    def test_verify_token_invalid_signature_rejected(self, mock_google_claims_factory):
        verifier = mock_google_claims_factory()
        service = GoogleAuthService(
            allowed_client_ids=["test-client"],
            custom_verifier=verifier,
        )
        with pytest.raises(GoogleTokenVerificationError, match="Signature verification failed"):
            service.verify_token("INVALID_SIGNATURE_TOKEN")

    def test_verify_token_invalid_issuer_rejected(self, mock_google_claims_factory):
        mock_claims = {
            "iss": "https://attacker.evil.com",
            "sub": "123456",
            "aud": "test-client",
        }
        verifier = mock_google_claims_factory(mock_claims)
        service = GoogleAuthService(
            allowed_client_ids=["test-client"],
            custom_verifier=verifier,
        )
        with pytest.raises(GoogleTokenVerificationError, match="Invalid token issuer"):
            service.verify_token("header.payload.signature")

    def test_verify_token_audience_mismatch_rejected(self, mock_google_claims_factory):
        mock_claims = {
            "iss": "https://accounts.google.com",
            "sub": "123456",
            "aud": "different-app-client-id.apps.googleusercontent.com",
        }
        verifier = mock_google_claims_factory(mock_claims)
        service = GoogleAuthService(
            allowed_client_ids=["authorized-aegis-client-id"],
            custom_verifier=verifier,
        )
        with pytest.raises(GoogleTokenVerificationError, match="does not match authorized client IDs"):
            service.verify_token("header.payload.signature")

    def test_verify_token_missing_sub_rejected(self, mock_google_claims_factory):
        mock_claims = {
            "iss": "https://accounts.google.com",
            "sub": "",
            "aud": "test-client",
        }
        verifier = mock_google_claims_factory(mock_claims)
        service = GoogleAuthService(
            allowed_client_ids=["test-client"],
            custom_verifier=verifier,
        )
        with pytest.raises(GoogleTokenVerificationError, match="mandatory 'sub'"):
            service.verify_token("header.payload.signature")


class TestGoogleAuthServiceLinkageAndBeta:
    """Tests for AuthService Google authentication, closed beta access, and patient identity linkage."""

    def test_new_user_blocked_when_closed_beta_and_not_whitelisted(
        self, isolated_db_session, mock_google_claims_factory
    ):
        mock_claims = {
            "iss": "https://accounts.google.com",
            "sub": "google_sub_unauthorized_99",
            "aud": "test-client",
            "email": "outsider@example.com",
            "email_verified": True,
        }
        verifier = mock_google_claims_factory(mock_claims)
        service = GoogleAuthService(allowed_client_ids=["test-client"], custom_verifier=verifier)

        auth_svc = AuthService(isolated_db_session)
        with pytest.raises(ClosedBetaAccessDeniedError, match="closed beta"):
            auth_svc.authenticate_google_device_and_issue_tokens(
                id_token="header.payload.sig",
                device_name="Pixel 8",
                platform="android",
                google_auth_service=service,
            )

        # Confirm 0 patients created in DB
        assert isolated_db_session.scalar(select(Patient)) is None

    def test_new_user_allowed_when_sub_whitelisted(
        self, isolated_db_session, mock_google_claims_factory
    ):
        target_sub = "google_sub_beta_pioneer_01"
        # Pre-seed beta whitelist entry
        entry = BetaAccessWhitelist(
            id=f"bw_{uuid.uuid4().hex[:14]}",
            identifier_type="google_sub",
            identifier_value=target_sub,
            is_active=True,
            granted_by="admin",
            notes="Closed Beta Cohort 1",
            created_at=datetime.now(timezone.utc),
        )
        isolated_db_session.add(entry)
        isolated_db_session.commit()

        mock_claims = {
            "iss": "https://accounts.google.com",
            "sub": target_sub,
            "aud": "test-client",
            "email": "pioneer@example.com",
            "email_verified": True,
        }
        verifier = mock_google_claims_factory(mock_claims)
        service = GoogleAuthService(allowed_client_ids=["test-client"], custom_verifier=verifier)

        auth_svc = AuthService(isolated_db_session)
        ok, msg, bundle = auth_svc.authenticate_google_device_and_issue_tokens(
            id_token="header.payload.sig",
            device_name="Pixel 8 Pro",
            platform="android",
            device_id="device_pioneer_01",
            google_auth_service=service,
        )

        assert ok is True
        assert bundle is not None
        assert bundle.patient_id.startswith("pat_g_")
        assert bundle.device_id == "device_pioneer_01"
        assert bundle.access_token is not None
        assert bundle.refresh_token is not None

        # Verify DB identity record
        identity = isolated_db_session.scalar(
            select(PatientGoogleIdentity).where(PatientGoogleIdentity.google_sub == target_sub)
        )
        assert identity is not None
        assert identity.patient_id == bundle.patient_id
        assert identity.email == "pioneer@example.com"
        assert identity.email_verified is True

    def test_new_user_allowed_when_email_whitelisted(
        self, isolated_db_session, mock_google_claims_factory
    ):
        target_email = "doctor.invited@example.com"
        entry = BetaAccessWhitelist(
            id=f"bw_{uuid.uuid4().hex[:14]}",
            identifier_type="email",
            identifier_value=target_email.lower(),
            is_active=True,
            granted_by="admin",
            notes="Doctor invitation",
            created_at=datetime.now(timezone.utc),
        )
        isolated_db_session.add(entry)
        isolated_db_session.commit()

        mock_claims = {
            "iss": "https://accounts.google.com",
            "sub": "google_sub_doctor_42",
            "aud": "test-client",
            "email": "Doctor.Invited@Example.COM",  # mixed case should match
            "email_verified": True,
        }
        verifier = mock_google_claims_factory(mock_claims)
        service = GoogleAuthService(allowed_client_ids=["test-client"], custom_verifier=verifier)

        auth_svc = AuthService(isolated_db_session)
        ok, msg, bundle = auth_svc.authenticate_google_device_and_issue_tokens(
            id_token="header.payload.sig",
            device_name="Samsung S24",
            platform="android",
            google_auth_service=service,
        )
        assert ok is True
        assert bundle is not None
        assert bundle.patient_id.startswith("pat_g_")

    def test_returning_user_returns_same_patient_id(
        self, isolated_db_session, mock_google_claims_factory
    ):
        target_sub = "google_sub_returning_user"
        isolated_db_session.add(
            BetaAccessWhitelist(
                id=f"bw_{uuid.uuid4().hex[:14]}",
                identifier_type="google_sub",
                identifier_value=target_sub,
                is_active=True,
                granted_by="admin",
            )
        )
        isolated_db_session.commit()

        mock_claims = {
            "iss": "https://accounts.google.com",
            "sub": target_sub,
            "aud": "test-client",
            "email": "user@example.com",
            "email_verified": True,
        }
        verifier = mock_google_claims_factory(mock_claims)
        service = GoogleAuthService(allowed_client_ids=["test-client"], custom_verifier=verifier)
        auth_svc = AuthService(isolated_db_session)

        # First login: device 1 (phone)
        ok1, _, bundle1 = auth_svc.authenticate_google_device_and_issue_tokens(
            id_token="header.payload.sig",
            device_name="Phone 1",
            device_id="dev_phone_1",
            google_auth_service=service,
        )
        first_patient_id = bundle1.patient_id

        # Second login: device 2 (new tablet)
        ok2, _, bundle2 = auth_svc.authenticate_google_device_and_issue_tokens(
            id_token="header.payload.sig",
            device_name="Tablet 2",
            device_id="dev_tablet_2",
            google_auth_service=service,
        )
        second_patient_id = bundle2.patient_id

        # Identity Guarantee: Exact same patient_id, zero duplicate profiles!
        assert first_patient_id == second_patient_id
        # Confirm total patient count in DB is exactly 1
        patients = isolated_db_session.scalars(select(Patient)).all()
        assert len(patients) == 1

        # Confirm 2 devices registered to the same patient
        devices = isolated_db_session.scalars(
            select(Device).where(Device.patient_id == first_patient_id)
        ).all()
        assert len(devices) == 2

    def test_revoked_whitelist_blocks_even_existing_users(
        self, isolated_db_session, mock_google_claims_factory
    ):
        target_sub = "google_sub_to_revoke"
        bw_entry = BetaAccessWhitelist(
            id=f"bw_{uuid.uuid4().hex[:14]}",
            identifier_type="google_sub",
            identifier_value=target_sub,
            is_active=True,
            granted_by="admin",
        )
        isolated_db_session.add(bw_entry)
        isolated_db_session.commit()

        mock_claims = {
            "iss": "https://accounts.google.com",
            "sub": target_sub,
            "aud": "test-client",
            "email": "revoked@example.com",
            "email_verified": True,
        }
        verifier = mock_google_claims_factory(mock_claims)
        service = GoogleAuthService(allowed_client_ids=["test-client"], custom_verifier=verifier)
        auth_svc = AuthService(isolated_db_session)

        # Initial login succeeds
        ok, _, bundle = auth_svc.authenticate_google_device_and_issue_tokens(
            id_token="token", device_name="Device", google_auth_service=service
        )
        assert ok is True

        # Administrator revokes beta access
        bw_entry.is_active = False
        bw_entry.revoked_at = datetime.now(timezone.utc)
        isolated_db_session.commit()

        # Subsequent login attempt is strictly rejected
        with pytest.raises(ClosedBetaAccessDeniedError, match="revoked"):
            auth_svc.authenticate_google_device_and_issue_tokens(
                id_token="token", device_name="Device", google_auth_service=service
            )

    def test_no_cross_merging_with_code_paired_patient_by_email(
        self, isolated_db_session, mock_google_claims_factory
    ):
        """
        Critical Privacy Invariant:
        If a patient was previously paired via a 6-digit code, a Google account
        sharing the same email address must NEVER merge or claim that patient's history.
        """
        # Pre-seed an existing patient paired via 6-digit code
        existing_patient = Patient(patient_id="pat_original_code_paired", status="active")
        isolated_db_session.add(existing_patient)
        isolated_db_session.commit()

        # Whitelist the email for beta
        shared_email = "shared.email@example.com"
        isolated_db_session.add(
            BetaAccessWhitelist(
                id=f"bw_{uuid.uuid4().hex[:14]}",
                identifier_type="email",
                identifier_value=shared_email,
                is_active=True,
                granted_by="admin",
            )
        )
        isolated_db_session.commit()

        mock_claims = {
            "iss": "https://accounts.google.com",
            "sub": "google_sub_different_identity",
            "aud": "test-client",
            "email": shared_email,
            "email_verified": True,
        }
        verifier = mock_google_claims_factory(mock_claims)
        service = GoogleAuthService(allowed_client_ids=["test-client"], custom_verifier=verifier)
        auth_svc = AuthService(isolated_db_session)

        ok, _, bundle = auth_svc.authenticate_google_device_and_issue_tokens(
            id_token="token", device_name="Device", google_auth_service=service
        )
        assert ok is True
        # Must be a newly provisioned patient, NOT the original patient!
        assert bundle.patient_id != "pat_original_code_paired"
        assert bundle.patient_id.startswith("pat_g_")


class TestGoogleAuthHTTPAPI:
    """HTTP endpoint integration tests for POST /api/v1/auth/google and admin whitelist endpoints."""

    def test_google_auth_endpoint_success(self, isolated_db_session, mock_google_claims_factory):
        app.dependency_overrides[get_db] = lambda: isolated_db_session

        target_sub = "google_sub_http_user"
        isolated_db_session.add(
            BetaAccessWhitelist(
                id="bw_1",
                identifier_type="google_sub",
                identifier_value=target_sub,
                is_active=True,
                granted_by="admin",
            )
        )
        isolated_db_session.commit()

        mock_claims = {
            "iss": "https://accounts.google.com",
            "sub": target_sub,
            "aud": "test-client-id",
            "email": "http.user@gmail.com",
            "email_verified": True,
        }
        service = GoogleAuthService(
            allowed_client_ids=["test-client-id"],
            custom_verifier=mock_google_claims_factory(mock_claims),
        )
        override_google_auth_service(service)

        client = TestClient(app)
        res = client.post(
            "/api/v1/auth/google",
            json={
                "id_token": "valid.mock.token",
                "device_name": "Pixel 8 Pro",
                "device_id": "dev_http_01",
                "platform": "android",
            },
        )
        assert res.status_code == 200
        assert res.headers["cache-control"] == "no-store"
        assert res.headers["pragma"] == "no-cache"
        data = res.json()
        assert data["token_type"] == "Bearer"
        assert data["patient_id"].startswith("pat_g_")
        assert data["device_id"] == "dev_http_01"
        assert "access_token" in data
        assert "refresh_token" in data

    def test_google_auth_endpoint_closed_beta_rejected_with_403(
        self, isolated_db_session, mock_google_claims_factory
    ):
        app.dependency_overrides[get_db] = lambda: isolated_db_session

        mock_claims = {
            "iss": "https://accounts.google.com",
            "sub": "sub_not_in_beta",
            "aud": "test-client-id",
            "email": "random.user@gmail.com",
            "email_verified": True,
        }
        service = GoogleAuthService(
            allowed_client_ids=["test-client-id"],
            custom_verifier=mock_google_claims_factory(mock_claims),
        )
        override_google_auth_service(service)

        client = TestClient(app)
        res = client.post(
            "/api/v1/auth/google",
            json={
                "id_token": "valid.mock.token",
                "device_name": "Pixel 8",
            },
        )
        assert res.status_code == 403
        assert "closed beta" in res.json()["detail"].lower()
        assert res.headers["cache-control"] == "no-store"

    def test_google_auth_endpoint_invalid_token_401(
        self, isolated_db_session, mock_google_claims_factory
    ):
        app.dependency_overrides[get_db] = lambda: isolated_db_session
        service = GoogleAuthService(
            allowed_client_ids=["test-client-id"],
            custom_verifier=mock_google_claims_factory(),
        )
        override_google_auth_service(service)

        client = TestClient(app)
        res = client.post(
            "/api/v1/auth/google",
            json={
                "id_token": "invalid.signature.token",
                "device_name": "Pixel 8",
            },
        )
        assert res.status_code == 401
        assert "verification failed" in res.json()["detail"].lower()

    def test_google_auth_token_grants_valid_device_session_access(
        self, isolated_db_session, mock_google_claims_factory
    ):
        app.dependency_overrides[get_db] = lambda: isolated_db_session

        target_sub = "google_sub_bearer_test"
        isolated_db_session.add(
            BetaAccessWhitelist(
                id="bw_bearer",
                identifier_type="google_sub",
                identifier_value=target_sub,
                is_active=True,
                granted_by="admin",
            )
        )
        isolated_db_session.commit()

        mock_claims = {
            "iss": "https://accounts.google.com",
            "sub": target_sub,
            "aud": "test-client-id",
            "email": "bearer.user@gmail.com",
            "email_verified": True,
        }
        service = GoogleAuthService(
            allowed_client_ids=["test-client-id"],
            custom_verifier=mock_google_claims_factory(mock_claims),
        )
        override_google_auth_service(service)

        client = TestClient(app)
        auth_res = client.post(
            "/api/v1/auth/google",
            json={
                "id_token": "valid.mock.token",
                "device_name": "Pixel 8",
                "device_id": "dev_bearer_test",
            },
        )
        assert auth_res.status_code == 200
        auth_data = auth_res.json()
        access_tok = auth_data["access_token"]
        refresh_tok = auth_data["refresh_token"]

        # Call protected endpoint /api/v1/cycle/current using the issued access token
        headers = {"Authorization": f"Bearer {access_tok}"}
        cycle_res = client.get("/api/v1/cycle/current", headers=headers)
        # Should authenticate successfully (200 with active cycle or 404 No Active Cycle, but NEVER 401)
        assert cycle_res.status_code in (200, 404)
        assert cycle_res.status_code != 401

        # Test token refresh works seamlessly with the issued refresh token
        ref_res = client.post(
            "/api/v1/auth/refresh",
            json={
                "device_id": "dev_bearer_test",
                "refresh_token": refresh_tok,
            },
        )
        assert ref_res.status_code == 200
        assert "access_token" in ref_res.json()
        assert ref_res.headers["cache-control"] == "no-store"

    def test_admin_beta_whitelist_crud_endpoints(self, isolated_db_session):
        app.dependency_overrides[get_db] = lambda: isolated_db_session

        # Setup local admin session
        admin_token = f"adm_sess_test_admin_{uuid.uuid4().hex}"
        from datetime import timedelta
        _ephemeral_admin_sessions[admin_token] = datetime.now(timezone.utc) + timedelta(hours=1)
        headers = {"Authorization": f"Bearer {admin_token}"}

        client = TestClient(app)

        # 1. Create whitelist entry for an email
        res_create = client.post(
            "/api/v1/admin/beta/whitelist",
            headers=headers,
            json={
                "identifier_type": "email",
                "identifier_value": "Vip.Tester@Hospital.org",
                "notes": "Lead Cardiologist Beta Tester",
            },
        )
        assert res_create.status_code == 201
        entry_data = res_create.json()
        assert entry_data["identifier_value"] == "vip.tester@hospital.org"  # normalized to lowercase
        assert entry_data["is_active"] is True
        entry_id = entry_data["id"]

        # 2. List whitelist entries
        res_list = client.get("/api/v1/admin/beta/whitelist", headers=headers)
        assert res_list.status_code == 200
        items = res_list.json()
        assert len(items) == 1
        assert items[0]["id"] == entry_id

        # 3. Revoke whitelist entry
        res_revoke = client.delete(f"/api/v1/admin/beta/whitelist/{entry_id}", headers=headers)
        assert res_revoke.status_code == 200
        assert res_revoke.json()["is_active"] is False
        assert res_revoke.json()["revoked_at"] is not None

        # 4. Verify listed entry is now inactive
        res_list_after = client.get("/api/v1/admin/beta/whitelist", headers=headers)
        assert res_list_after.json()[0]["is_active"] is False

    def test_revoking_beta_access_blocks_existing_jwt_and_refresh_token(
        self, isolated_db_session, mock_google_claims_factory
    ):
        """
        Verifies:
        1. User authorizes via Google in closed beta, gets JWT + Refresh Token.
        2. Protected request succeeds with the JWT.
        3. Admin revokes the user's beta access whitelist entry.
        4. Subsequent request with the still-valid JWT is immediately blocked with 403 Forbidden.
        5. Token refresh attempt with the Refresh Token is immediately blocked with 403 Forbidden.
        6. Subsequent Google Sign-In attempt is blocked with 403 Forbidden.
        7. Non-Google patient (paired via 6-digit code) remains completely unaffected.
        """
        app.dependency_overrides[get_db] = lambda: isolated_db_session

        target_sub = "sub_user_to_be_revoked_99"
        target_email = "revoked.user@example.com"
        entry_id = "bw_test_to_revoke"

        # 1. Whitelist user
        isolated_db_session.add(
            BetaAccessWhitelist(
                id=entry_id,
                identifier_type="google_sub",
                identifier_value=target_sub,
                is_active=True,
                granted_by="admin",
                created_at=datetime.now(timezone.utc),
            )
        )
        isolated_db_session.commit()

        # 2. Setup mock Google service and login
        mock_claims = {
            "iss": "https://accounts.google.com",
            "sub": target_sub,
            "aud": "test-client-id",
            "email": target_email,
            "email_verified": True,
        }
        service = GoogleAuthService(
            allowed_client_ids=["test-client-id"],
            custom_verifier=mock_google_claims_factory(mock_claims),
        )
        override_google_auth_service(service)

        client = TestClient(app)
        login_res = client.post(
            "/api/v1/auth/google",
            json={
                "id_token": "valid.mock.token",
                "device_name": "Revocable Phone",
                "device_id": "dev_revocable_1",
                "platform": "android",
            },
        )
        assert login_res.status_code == 200
        auth_data = login_res.json()
        access_tok = auth_data["access_token"]
        refresh_tok = auth_data["refresh_token"]
        patient_id = auth_data["patient_id"]

        # Unified TTL check: access token expires_in matches settings.access_token_ttl_minutes * 60
        assert auth_data["expires_in"] == settings.access_token_ttl_minutes * 60

        # Create active medical cycle for this patient so /cycle/current returns 200
        from app.models.medical_cycle import MedicalCycle
        isolated_db_session.add(
            MedicalCycle(
                cycle_id="cycle_rev_test",
                patient_id=patient_id,
                status="active",
                created_at=datetime.now(timezone.utc),
            )
        )
        isolated_db_session.commit()

        # 3. Verify active access token works
        cycle_res = client.get(
            "/api/v1/cycle/current",
            headers={"Authorization": f"Bearer {access_tok}"},
        )
        assert cycle_res.status_code == 200
        assert cycle_res.json()["cycle_id"] == "cycle_rev_test"

        # 4. Admin revokes the user's beta whitelist entry
        entry = isolated_db_session.scalar(
            select(BetaAccessWhitelist).where(BetaAccessWhitelist.id == entry_id)
        )
        entry.is_active = False
        entry.revoked_at = datetime.now(timezone.utc)
        isolated_db_session.commit()

        # 5. Subsequent request with the original valid JWT must be blocked with 403 Forbidden!
        blocked_cycle_res = client.get(
            "/api/v1/cycle/current",
            headers={"Authorization": f"Bearer {access_tok}"},
        )
        assert blocked_cycle_res.status_code == 403
        assert "closed beta access has been revoked" in blocked_cycle_res.json()["detail"].lower()

        # 6. Attempting to refresh tokens with the refresh token must be blocked with 403 Forbidden!
        blocked_refresh_res = client.post(
            "/api/v1/auth/refresh",
            json={
                "device_id": "dev_revocable_1",
                "refresh_token": refresh_tok,
            },
        )
        assert blocked_refresh_res.status_code == 403
        assert "closed beta access has been revoked" in blocked_refresh_res.json()["detail"].lower()

        # 7. Subsequent Google Sign-In attempt must be blocked with 403 Forbidden!
        blocked_login_res = client.post(
            "/api/v1/auth/google",
            json={
                "id_token": "valid.mock.token",
                "device_name": "Revocable Phone",
                "device_id": "dev_revocable_1",
                "platform": "android",
            },
        )
        assert blocked_login_res.status_code == 403

        # 8. Non-Google patient (paired via 6-digit code) remains 100% active and untouched
        from app.models.device import Device
        from app.models.device_refresh_token import DeviceRefreshToken
        from app.security.tokens import create_access_token

        code_pat = Patient(patient_id="pat_code_independent", status="active")
        code_dev = Device(
            device_id="dev_code_independent",
            patient_id="pat_code_independent",
            device_name="Code Phone",
            platform="android",
            is_active=True,
        )
        code_rf = DeviceRefreshToken(
            token_id="rt_code_indep",
            device_id="dev_code_independent",
            token_hash=DeviceRefreshToken.hash_token("raw_code_rt"),
            family_id="fam_code_indep",
            expires_at=datetime.now(timezone.utc) + timedelta(days=30),
        )
        isolated_db_session.add_all([code_pat, code_dev, code_rf])
        isolated_db_session.commit()

        code_jwt = create_access_token(
            patient_id="pat_code_independent",
            device_id="dev_code_independent",
            family_id="fam_code_indep",
        ).access_token

        code_cycle_res = client.get(
            "/api/v1/cycle/current",
            headers={"Authorization": f"Bearer {code_jwt}"},
        )
        # Should NOT be 403 (returns 404 since no cycle was created for pat_code_independent)
        assert code_cycle_res.status_code == 404

    def test_jwt_ttl_unified_between_pairing_code_and_google_auth(
        self, isolated_db_session, mock_google_claims_factory
    ):
        """
        Verifies that both 6-digit pairing code and Google auth produce identical
        access token TTL governed by settings.access_token_ttl_minutes.
        """
        from datetime import timedelta
        from app.models.pairing_code import PairingCode

        app.dependency_overrides[get_db] = lambda: isolated_db_session

        # 1. 6-digit pairing code issuance
        code_patient = Patient(patient_id="pat_ttl_code", status="active")
        pairing_code = PairingCode(
            code_id="pc_ttl_test",
            patient_id="pat_ttl_code",
            code_hash=PairingCode.hash_code("777888", secret="testsalt12345678"),
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
        )
        # 2. Google whitelist
        g_sub = "sub_ttl_test"
        g_wl = BetaAccessWhitelist(
            id="bw_ttl_test",
            identifier_type="google_sub",
            identifier_value=g_sub,
            is_active=True,
            granted_by="admin",
        )
        isolated_db_session.add_all([code_patient, pairing_code, g_wl])
        isolated_db_session.commit()

        auth_service = AuthService(isolated_db_session, pairing_secret="testsalt12345678")

        # Code pairing
        ok_code, _, bundle_code = auth_service.pair_device_and_issue_tokens(
            raw_code="777888",
            device_name="Code Device",
            device_id="dev_code_ttl",
            client_ip="127.0.0.1",
        )
        assert ok_code is True
        assert bundle_code.access_token_expires_in == settings.access_token_ttl_minutes * 60

        # Google auth
        mock_claims = {
            "iss": "https://accounts.google.com",
            "sub": g_sub,
            "aud": "test-client-id",
        }
        g_service = GoogleAuthService(
            allowed_client_ids=["test-client-id"],
            custom_verifier=mock_google_claims_factory(mock_claims),
        )
        ok_g, _, bundle_g = auth_service.authenticate_google_device_and_issue_tokens(
            id_token="valid.mock.token",
            device_name="Google Device",
            device_id="dev_g_ttl",
            client_ip="127.0.0.1",
            google_auth_service=g_service,
        )
        assert ok_g is True
        assert bundle_g.access_token_expires_in == settings.access_token_ttl_minutes * 60

        # Both match exactly
        assert bundle_code.access_token_expires_in == bundle_g.access_token_expires_in == 1800
