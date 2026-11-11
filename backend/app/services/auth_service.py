from dataclasses import dataclass
from datetime import datetime, timezone
import logging
import time
from typing import Callable, List, Optional, Tuple
import uuid
from sqlalchemy import select, or_
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session
from app.config import settings
from app.models.beta_whitelist import BetaAccessWhitelist
from app.models.device import Device
from app.models.device_refresh_token import DeviceRefreshToken
from app.models.pairing_code import PairingCode
from app.models.patient import Patient
from app.models.patient_google_identity import PatientGoogleIdentity
from app.repositories.device_repository import DeviceRepository
from app.repositories.pairing_repository import PairingRepository
from app.repositories.token_repository import DeviceTokenRepository
from app.security.rate_limiter import (
    PersistentRateLimiter,
    RateLimitAttemptTicket,
    RateLimiterUnavailableError,
)
from app.security.tokens import (
    DEFAULT_DEVICE_SCOPE,
    IssuedAccessToken,
    create_access_token,
    resolve_jwt_secret,
)
from app.services.google_auth_service import (
    GoogleAuthConfigError,
    GoogleAuthService,
    GoogleTokenVerificationError,
    VerifiedGoogleClaims,
    get_google_auth_service,
    is_patient_beta_revoked,
)

logger = logging.getLogger(__name__)

_DEFAULT_RATE_LIMITER = object()


class ClosedBetaAccessDeniedError(Exception):
    """Raised when a user is not permitted into the closed beta."""


class GoogleAccountSuspendedError(Exception):
    """Raised when the associated Aegis patient profile is inactive or archived."""


@dataclass(frozen=True)
class PairingSessionBundle:
    """
    Complete atomic result of pairing a device:
    contains the paired Device entity, consumed PairingCode entity (or None for Google Auth),
    issued short-lived JWT access token, and issued long-lived refresh token.
    """
    device: Device
    access_token: str
    access_token_claims_jti: str
    access_token_expires_in: int
    access_token_expires_at: datetime
    refresh_token: str
    refresh_token_entity: DeviceRefreshToken
    patient_id: str
    device_id: str
    scope: str
    pairing_code: Optional[PairingCode] = None
    post_commit_warnings: Tuple[str, ...] = ()


@dataclass(frozen=True)
class RefreshedSessionBundle:
    """Result of an atomic refresh token rotation and new JWT issuance."""
    access_token: str
    access_token_claims_jti: str
    access_token_expires_in: int
    access_token_expires_at: datetime
    refresh_token: str
    refresh_token_entity: DeviceRefreshToken
    patient_id: str
    device_id: str
    scope: str
    post_commit_warnings: Tuple[str, ...] = ()


class AuthService:
    """
    Transactional service coordinating atomic device pairing, token rotation,
    and device revocation prior to exposing public HTTP endpoints in Stage 5.3B-2.

    MANDATORY RATE LIMITING (FAIL-CLOSED):
    - `AuthService` requires an operational `PersistentRateLimiter` instance.
    - Passing `rate_limiter=None` (or any non-`PersistentRateLimiter` object) or initializing
      `AuthService` when the `auth_rate_limits` SQLite storage is missing/unreachable
      immediately raises `RateLimiterUnavailableError` (fail-closed).
    - Every call to `pair_device_and_issue_tokens` unconditionally acquires a rate-limit
      attempt slot before verifying the pairing code.

    PRE-COMMIT VS. POST-COMMIT TRANSACTION BOUNDARY:
    1. Pre-Commit & Commit Phase:
       - Consumes the 6-digit `PairingCode` and registers/updates `Device` (`commit=False`).
       - Revokes any prior active `DeviceRefreshToken` records for this `device_id` (`commit=False`).
       - Issues a new `DeviceRefreshToken` (`commit=False`).
       - Signs the short-lived HS256 JWT `access_token` in memory.
       - Executes a single `self.db.commit()`.
       - If ANY step before or during `self.db.commit()` fails, `self.db.rollback()` restores
         the database completely: the pairing code remains unconsumed (`consumed_at IS NULL`,
         `active_code_hash` intact), the device is not left partially registered, and no
         orphan refresh tokens exist.
    2. Post-Commit Phase (after `self.db.commit()` succeeds):
       - Once `self.db.commit()` succeeds, the database state transition is irreversible and
         the in-memory `(access_token, refresh_token)` credentials are valid and live.
       - Subsequent best-effort operations (`self.db.refresh(...)` and
         `self._rate_limiter.record_success(ticket)`) execute outside the rollback handler:
         if either raises an exception, `self.db.rollback()` is NEVER called, the error is
         logged and recorded in `bundle.post_commit_warnings`, and the valid committed
         `PairingSessionBundle` is returned to the caller.
       - Note that if `record_success(ticket)` fails after commit, the reserved failure slot
         simply remains counted in `auth_rate_limits` (erring on the conservative side without
         weakening brute-force protection).

    UNCERTAIN OUTCOME RECOVERY (LOST SERVER RESPONSE AFTER COMMIT):
    If `self.db.commit()` succeeds on the backend but the network connection drops before
    the mobile client receives the `(access_token, refresh_token)` HTTP response:
    - The single-use 6-digit pairing code is now consumed in SQLite and CANNOT be replayed.
    - Recovery Procedure:
      1. The administrator issues a new 6-digit pairing code for the same `patient_id`
         via the local Admin CLI (`python -m app.cli.pairing issue-code --patient-id <ID>`).
      2. The mobile app submits the new code along with its same persistent `device_id`.
      3. `pair_device_and_issue_tokens` verifies that `device.patient_id == code.patient_id`,
         updates `device`, atomically revokes the orphaned refresh token from the lost
         response via `revoke_all_for_device(device_id, commit=False)`, and commits a
         fresh `(access_token, refresh_token)` pair.
      4. If the client does not yet know whether its request succeeded or failed due to a
         network timeout, the client MUST treat the pairing outcome as failed/unpaired and
         prompt for a fresh code rather than assuming partial authentication.
    """

    def __init__(
        self,
        db: Session,
        *,
        pairing_secret: Optional[str] = None,
        jwt_secret: Optional[str] = None,
        rate_limiter: object = _DEFAULT_RATE_LIMITER,
    ) -> None:
        if db is None:
            raise ValueError("db session must not be None.")
        self.db = db
        self._pairing_secret = pairing_secret
        self._jwt_secret = jwt_secret

        if rate_limiter is _DEFAULT_RATE_LIMITER:
            resolved_limiter = PersistentRateLimiter(db)
        elif isinstance(rate_limiter, PersistentRateLimiter):
            resolved_limiter = rate_limiter
        else:
            raise RateLimiterUnavailableError(
                "AuthService requires an active PersistentRateLimiter instance; "
                "disabling rate_limiter (rate_limiter=None) is forbidden (fail-closed)."
            )

        # Fail-closed readiness check at service initialization
        resolved_limiter.verify_ready()
        self._rate_limiter: PersistentRateLimiter = resolved_limiter

    @property
    def rate_limiter(self) -> PersistentRateLimiter:
        return self._rate_limiter

    def pair_device_and_issue_tokens(
        self,
        *,
        raw_code: str,
        device_name: str,
        platform: str = "android",
        device_id: Optional[str] = None,
        client_ip: Optional[str] = None,
        scope: str = DEFAULT_DEVICE_SCOPE,
        access_ttl_minutes: Optional[int] = None,
        refresh_ttl_days: Optional[int] = None,
        access_token_factory: Optional[Callable[..., IssuedAccessToken]] = None,
    ) -> Tuple[bool, str, Optional[PairingSessionBundle]]:
        """
        Atomically verifies a pairing code, registers the device, revokes any stale
        device tokens, persists a new refresh token, and signs a new JWT access token
        with a single final commit.
        """
        # 1. Fail-closed secret & limiter verification BEFORE touching DB state
        resolved_pairing_secret = PairingCode.resolve_secret(self._pairing_secret)
        resolved_jwt_secret = resolve_jwt_secret(self._jwt_secret)
        if not isinstance(self._rate_limiter, PersistentRateLimiter):
            raise RateLimiterUnavailableError(
                "PersistentRateLimiter is required for device pairing (fail-closed)."
            )

        # 2. Mandatory persistent rate-limit check & slot reservation in isolated transaction
        ticket: RateLimitAttemptTicket = self._rate_limiter.acquire_attempt(
            client_ip=client_ip or "unknown",
            device_id=device_id,
        )

        pairing_repo = PairingRepository(self.db, secret=resolved_pairing_secret)
        token_repo = DeviceTokenRepository(self.db)
        effective_refresh_ttl = (
            settings.refresh_token_ttl_days if refresh_ttl_days is None else refresh_ttl_days
        )
        token_creator = access_token_factory or create_access_token

        # --- PRE-COMMIT & COMMIT PHASE ---
        try:
            # 3. Verify and consume code + register device without committing
            ok, msg, device, consumed = pairing_repo.pair_device(
                raw_code=raw_code,
                device_name=device_name,
                platform=platform,
                device_id=device_id,
                secret=resolved_pairing_secret,
                commit=False,
            )
            if not ok or device is None or consumed is None:
                self.db.rollback()
                return False, msg, None

            committed_patient_id = str(device.patient_id)
            committed_device_id = str(device.device_id)

            # 4. Revoke any prior unrevoked refresh tokens for this device (safe re-pairing recovery)
            token_repo.revoke_all_for_device(committed_device_id, commit=False)

            # 5. Issue new refresh token without committing
            raw_refresh, refresh_entity = token_repo.issue_refresh_token(
                device_id=committed_device_id,
                ttl_days=effective_refresh_ttl,
                commit=False,
            )
            committed_family_id = str(refresh_entity.family_id)

            # 6. Issue signed HS256 JWT access token in memory
            issued_jwt = token_creator(
                patient_id=committed_patient_id,
                device_id=committed_device_id,
                family_id=committed_family_id,
                scope=scope,
                ttl_minutes=access_ttl_minutes,
                secret=resolved_jwt_secret,
            )
            if (
                issued_jwt is None
                or not isinstance(getattr(issued_jwt, "access_token", None), str)
                or not issued_jwt.access_token.strip()
            ):
                raise RuntimeError("Access token factory returned an invalid token bundle.")

            # 7. Single atomic commit for the entire pairing + token issuance workflow
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise

        # --- POST-COMMIT PHASE (Transaction is already committed; NEVER rollback!) ---
        post_commit_warnings: List[str] = []

        try:
            self.db.refresh(device)
            self.db.refresh(consumed)
            self.db.refresh(refresh_entity)
        except Exception as refresh_exc:
            warn_msg = f"Post-commit ORM refresh warning: {refresh_exc}"
            logger.warning(warn_msg)
            post_commit_warnings.append(warn_msg)

        try:
            self._rate_limiter.record_success(ticket)
        except Exception as limiter_exc:
            warn_msg = f"Post-commit rate limiter record_success warning: {limiter_exc}"
            logger.warning(warn_msg)
            post_commit_warnings.append(warn_msg)

        bundle = PairingSessionBundle(
            device=device,
            pairing_code=consumed,
            access_token=issued_jwt.access_token,
            access_token_claims_jti=issued_jwt.claims.jti,
            access_token_expires_in=issued_jwt.expires_in,
            access_token_expires_at=issued_jwt.claims.expires_at,
            refresh_token=raw_refresh,
            refresh_token_entity=refresh_entity,
            patient_id=committed_patient_id,
            device_id=committed_device_id,
            scope=issued_jwt.claims.scope,
            post_commit_warnings=tuple(post_commit_warnings),
        )
        return True, "Device paired and tokens issued atomically", bundle

    def refresh_device_tokens(
        self,
        *,
        device_id: str,
        old_raw_refresh_token: str,
        scope: str = DEFAULT_DEVICE_SCOPE,
        access_ttl_minutes: Optional[int] = None,
        refresh_ttl_days: Optional[int] = None,
        access_token_factory: Optional[Callable[..., IssuedAccessToken]] = None,
    ) -> Tuple[bool, str, Optional[RefreshedSessionBundle]]:
        """
        Atomically rotates a device refresh token and issues a new HS256 JWT access token.
        - Enforces active status of both Device and Patient with fresh DB reads.
        - Protects against refresh/revoke races and performs automatic reuse detection.
        - Rolls back rotation if JWT signing fails before commit.
        - Isolates post-commit ORM refresh from the committed transaction.
        """
        resolved_jwt_secret = resolve_jwt_secret(self._jwt_secret)
        token_repo = DeviceTokenRepository(self.db)
        effective_refresh_ttl = (
            settings.refresh_token_ttl_days if refresh_ttl_days is None else refresh_ttl_days
        )
        token_creator = access_token_factory or create_access_token
        clean_device_id = device_id.strip() if isinstance(device_id, str) else ""
        max_lock_retries = 5

        for attempt in range(max_lock_retries + 1):
            # --- PRE-COMMIT & COMMIT PHASE ---
            try:
                ok, msg, rotated_pair = token_repo.rotate_refresh_token(
                    device_id=clean_device_id,
                    old_raw_token=old_raw_refresh_token,
                    ttl_days=effective_refresh_ttl,
                    commit=False,
                )
                if not ok or rotated_pair is None:
                    # Commit any reuse-detection family revocation that occurred on failure
                    if "reuse detected" in msg.lower():
                        self.db.commit()
                    else:
                        self.db.rollback()
                    return False, msg, None

                new_raw_refresh, new_refresh_entity = rotated_pair
                committed_family_id = str(new_refresh_entity.family_id)

                row = self.db.execute(
                    select(Device, Patient)
                    .join(Patient, Patient.patient_id == Device.patient_id)
                    .where(Device.device_id == clean_device_id)
                    .execution_options(populate_existing=True)
                ).first()
                if row is None:
                    self.db.rollback()
                    return False, "Device is revoked or inactive", None
                device, patient = row
                if not device.is_active or device.revoked_at is not None:
                    self.db.rollback()
                    return False, "Device is revoked or inactive", None
                if patient.status != "active":
                    self.db.rollback()
                    return False, "Patient is not active", None

                if is_patient_beta_revoked(self.db, str(patient.patient_id)):
                    self.db.rollback()
                    raise ClosedBetaAccessDeniedError(
                        "Access denied. Project Aegis closed beta access has been revoked."
                    )

                committed_patient_id = str(device.patient_id)
                committed_device_id = str(device.device_id)

                issued_jwt = token_creator(
                    patient_id=committed_patient_id,
                    device_id=committed_device_id,
                    family_id=committed_family_id,
                    scope=scope,
                    ttl_minutes=access_ttl_minutes,
                    secret=resolved_jwt_secret,
                )
                if (
                    issued_jwt is None
                    or not isinstance(getattr(issued_jwt, "access_token", None), str)
                    or not issued_jwt.access_token.strip()
                ):
                    raise RuntimeError("Access token factory returned an invalid token bundle.")

                self.db.commit()
            except OperationalError as exc:
                self.db.rollback()
                err_msg = str(exc).lower()
                if attempt < max_lock_retries and ("locked" in err_msg or "busy" in err_msg):
                    time.sleep(0.015 * (attempt + 1))
                    continue
                raise
            except Exception:
                self.db.rollback()
                raise

            # --- POST-COMMIT PHASE (Transaction is already committed; NEVER rollback!) ---
            post_commit_warnings: List[str] = []
            try:
                self.db.refresh(new_refresh_entity)
            except Exception as refresh_exc:
                warn_msg = f"Post-commit ORM refresh warning: {refresh_exc}"
                logger.warning(warn_msg)
                post_commit_warnings.append(warn_msg)

            return (
                True,
                "Tokens rotated successfully",
                RefreshedSessionBundle(
                    access_token=issued_jwt.access_token,
                    access_token_claims_jti=issued_jwt.claims.jti,
                    access_token_expires_in=issued_jwt.expires_in,
                    access_token_expires_at=issued_jwt.claims.expires_at,
                    refresh_token=new_raw_refresh,
                    refresh_token_entity=new_refresh_entity,
                    patient_id=committed_patient_id,
                    device_id=committed_device_id,
                    scope=issued_jwt.claims.scope,
                    post_commit_warnings=tuple(post_commit_warnings),
                ),
            )

        return False, "Could not rotate refresh token due to concurrent lock contention", None

    def authenticate_google_device_and_issue_tokens(
        self,
        *,
        id_token: str,
        device_name: str,
        platform: str = "android",
        device_id: Optional[str] = None,
        client_ip: Optional[str] = None,
        scope: str = DEFAULT_DEVICE_SCOPE,
        access_ttl_minutes: Optional[int] = None,
        refresh_ttl_days: Optional[int] = None,
        access_token_factory: Optional[Callable[..., IssuedAccessToken]] = None,
        google_auth_service: Optional[GoogleAuthService] = None,
    ) -> Tuple[bool, str, Optional[PairingSessionBundle]]:
        """
        Authenticates a client device using an externally verified Google ID Token,
        anchoring the identity strictly to Google's verified `sub`.
        """
        # 1. Fail-closed secret & limiter verification
        resolved_jwt_secret = resolve_jwt_secret(self._jwt_secret)
        if not isinstance(self._rate_limiter, PersistentRateLimiter):
            raise RateLimiterUnavailableError(
                "PersistentRateLimiter is required for authentication (fail-closed)."
            )

        # 2. Rate-limit check & slot reservation
        ticket: RateLimitAttemptTicket = self._rate_limiter.acquire_attempt(
            client_ip=client_ip or "unknown",
            device_id=device_id,
        )

        # 3. Verify Google ID token cryptographically
        verifier = google_auth_service or get_google_auth_service()
        claims: VerifiedGoogleClaims = verifier.verify_token(id_token)

        device_repo = DeviceRepository(self.db)
        token_repo = DeviceTokenRepository(self.db)
        effective_refresh_ttl = (
            settings.refresh_token_ttl_days if refresh_ttl_days is None else refresh_ttl_days
        )
        token_creator = access_token_factory or create_access_token

        # --- PRE-COMMIT & COMMIT PHASE ---
        try:
            # 4. Check if Google Identity already exists
            identity = self.db.scalar(
                select(PatientGoogleIdentity)
                .where(PatientGoogleIdentity.google_sub == claims.sub)
            )

            # Check for explicit closed-beta revocation
            revocation_check = self.db.scalar(
                select(BetaAccessWhitelist).where(
                    BetaAccessWhitelist.identifier_type.in_(["google_sub", "email"]),
                    BetaAccessWhitelist.identifier_value.in_(
                        [claims.sub] + ([claims.email.lower()] if claims.email else [])
                    ),
                    or_(
                        BetaAccessWhitelist.is_active.is_(False),
                        BetaAccessWhitelist.revoked_at.isnot(None),
                    ),
                )
            )
            if revocation_check is not None:
                self.db.rollback()
                raise ClosedBetaAccessDeniedError(
                    "Closed beta access has been revoked for this Google account."
                )

            if identity is not None:
                # Existing Google-authenticated patient
                patient = self.db.scalar(
                    select(Patient).where(Patient.patient_id == identity.patient_id)
                )
                if patient is None or patient.status != "active":
                    self.db.rollback()
                    raise GoogleAccountSuspendedError(
                        f"Patient profile '{identity.patient_id}' is inactive or suspended."
                    )

                patient_id = identity.patient_id
                identity.last_authenticated_at = datetime.now(timezone.utc)
                if claims.email and identity.email != claims.email:
                    identity.email = claims.email
                identity.email_verified = claims.email_verified
                self.db.flush()
            else:
                # New user registration: check closed beta allowlist
                if not settings.beta_registration_open:
                    allowed = False
                    # Sub-based whitelist
                    sub_entry = self.db.scalar(
                        select(BetaAccessWhitelist).where(
                            BetaAccessWhitelist.identifier_type == "google_sub",
                            BetaAccessWhitelist.identifier_value == claims.sub,
                            BetaAccessWhitelist.is_active.is_(True),
                            BetaAccessWhitelist.revoked_at.is_(None),
                        )
                    )
                    if sub_entry is not None:
                        allowed = True
                    elif claims.email:
                        email_entry = self.db.scalar(
                            select(BetaAccessWhitelist).where(
                                BetaAccessWhitelist.identifier_type == "email",
                                BetaAccessWhitelist.identifier_value == claims.email.lower(),
                                BetaAccessWhitelist.is_active.is_(True),
                                BetaAccessWhitelist.revoked_at.is_(None),
                            )
                        )
                        if email_entry is not None:
                            allowed = True

                    if not allowed:
                        self.db.rollback()
                        raise ClosedBetaAccessDeniedError(
                            "Registration is restricted during closed beta. Your Google account is not on the approval list."
                        )

                # Provision new Aegis patient
                patient_id = f"pat_g_{uuid.uuid4().hex[:14]}"
                new_patient = Patient(patient_id=patient_id, status="active")
                self.db.add(new_patient)
                self.db.flush()

                new_identity = PatientGoogleIdentity(
                    id=f"gid_{uuid.uuid4().hex[:14]}",
                    google_sub=claims.sub,
                    google_issuer=claims.iss,
                    patient_id=patient_id,
                    email=claims.email,
                    email_verified=claims.email_verified,
                    created_at=datetime.now(timezone.utc),
                    last_authenticated_at=datetime.now(timezone.utc),
                )
                self.db.add(new_identity)
                self.db.flush()

            # 5. Register device & issue tokens atomically
            device = device_repo.register_device(
                patient_id=patient_id,
                device_name=device_name,
                platform=platform,
                device_id=device_id,
                commit=False,
            )
            committed_device_id = str(device.device_id)

            # 6. Revoke prior refresh tokens for this device
            token_repo.revoke_all_for_device(committed_device_id, commit=False)

            # 7. Issue new refresh token
            raw_refresh, refresh_entity = token_repo.issue_refresh_token(
                device_id=committed_device_id,
                ttl_days=effective_refresh_ttl,
                commit=False,
            )
            committed_family_id = str(refresh_entity.family_id)

            # 8. Issue signed HS256 JWT access token
            issued_jwt = token_creator(
                patient_id=patient_id,
                device_id=committed_device_id,
                family_id=committed_family_id,
                scope=scope,
                ttl_minutes=access_ttl_minutes,
                secret=resolved_jwt_secret,
            )
            if (
                issued_jwt is None
                or not isinstance(getattr(issued_jwt, "access_token", None), str)
                or not issued_jwt.access_token.strip()
            ):
                raise RuntimeError("Access token factory returned an invalid token bundle.")

            # 9. Commit transaction atomically
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise

        # --- POST-COMMIT PHASE (Transaction is already committed; NEVER rollback!) ---
        post_commit_warnings: List[str] = []

        try:
            self.db.refresh(device)
            self.db.refresh(refresh_entity)
        except Exception as refresh_exc:
            warn_msg = f"Post-commit ORM refresh warning: {refresh_exc}"
            logger.warning(warn_msg)
            post_commit_warnings.append(warn_msg)

        try:
            self._rate_limiter.record_success(ticket)
        except Exception as limiter_exc:
            warn_msg = f"Post-commit rate limiter record_success warning: {limiter_exc}"
            logger.warning(warn_msg)
            post_commit_warnings.append(warn_msg)

        bundle = PairingSessionBundle(
            device=device,
            access_token=issued_jwt.access_token,
            access_token_claims_jti=issued_jwt.claims.jti,
            access_token_expires_in=issued_jwt.expires_in,
            access_token_expires_at=issued_jwt.claims.expires_at,
            refresh_token=raw_refresh,
            refresh_token_entity=refresh_entity,
            patient_id=patient_id,
            device_id=committed_device_id,
            scope=issued_jwt.claims.scope,
            pairing_code=None,
            post_commit_warnings=tuple(post_commit_warnings),
        )
        return True, "Google authentication successful", bundle
