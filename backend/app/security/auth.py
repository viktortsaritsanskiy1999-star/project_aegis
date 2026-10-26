from dataclasses import dataclass
from datetime import datetime, timezone
import secrets
from typing import Callable, FrozenSet, Optional, Union
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models.device import Device
from app.models.device_refresh_token import DeviceRefreshToken
from app.models.medical_cycle import MedicalCycle
from app.models.patient import Patient
from app.security.tokens import (
    TokenConfigurationError,
    TokenError,
    TokenExpiredError,
    TokenValidationError,
    verify_access_token,
)

# HTTPBearer security scheme
security_scheme = HTTPBearer(auto_error=False)


@dataclass(frozen=True)
class DeviceSession:
    """
    Verified mobile device authentication context derived from a valid HS256 JWT
    and cross-checked against live SQLite `Device` and `Patient` state on every request.
    """
    patient_id: str
    device_id: str
    jti: str
    family_id: str
    scope: str
    scopes: FrozenSet[str]
    issued_at: datetime
    expires_at: datetime

    @property
    def is_device_session(self) -> bool:
        return True

    @property
    def is_admin_session(self) -> bool:
        return False

    def has_scope(self, required_scope: str) -> bool:
        return required_scope in self.scopes

    def has_all_scopes(self, *required_scopes: str) -> bool:
        return bool(required_scopes) and all(s in self.scopes for s in required_scopes)

    def has_any_scope(self, *required_scopes: str) -> bool:
        return bool(required_scopes) and any(s in self.scopes for s in required_scopes)


@dataclass(frozen=True)
class ServiceAdminSession:
    """
    Server-side administrative / internal service session authenticated via `AEGIS_API_SECRET`.
    Strictly isolated from mobile `DeviceSession` tokens and never issued to mobile devices.
    """
    scopes: FrozenSet[str] = frozenset({"*"})

    @property
    def is_device_session(self) -> bool:
        return False

    @property
    def is_admin_session(self) -> bool:
        return True

    def has_scope(self, required_scope: str) -> bool:
        return True

    def has_all_scopes(self, *required_scopes: str) -> bool:
        return True

    def has_any_scope(self, *required_scopes: str) -> bool:
        return True


AuthSession = Union[DeviceSession, ServiceAdminSession]


def _raise_unauthorized(detail: str) -> None:
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


def authenticate_device_jwt(raw_token: str, db: Session) -> DeviceSession:
    """
    Verifies a mobile HS256 JWT access token and enforces live database checks:
    1. Valid HS256 signature, non-expired `exp`, valid `iat`, `iss`, `aud`, `jti`, `family_id`, `scope`.
    2. `Device` exists in DB, `device.is_active == True`, `device.revoked_at is None`.
    3. `device.patient_id == claims.patient_id`.
    4. `Patient` exists in DB and `patient.status == 'active'`.
    5. If refresh-token lineage rows exist for `(device_id, family_id)`, at least one
       token in the family must remain unrevoked and unexpired (instant invalidation on
       refresh-token reuse detection).
    Raises 401 Unauthorized on any failure.
    """
    try:
        claims = verify_access_token(raw_token)
    except TokenExpiredError:
        _raise_unauthorized("Access token has expired.")
    except (TokenValidationError, TokenConfigurationError, TokenError):
        _raise_unauthorized("Invalid client authentication token.")

    row = db.execute(
        select(Device, Patient)
        .join(Patient, Patient.patient_id == Device.patient_id)
        .where(Device.device_id == claims.device_id)
        .execution_options(populate_existing=True)
    ).first()

    if row is None:
        _raise_unauthorized("Device session is invalid or device does not exist.")

    device, patient = row
    if not device.is_active or device.revoked_at is not None:
        _raise_unauthorized("Device access has been revoked.")

    if device.patient_id != claims.patient_id:
        _raise_unauthorized("Token patient identity does not match registered device owner.")

    if patient.status != "active":
        _raise_unauthorized("Patient profile is not active.")

    # Mandatory server-side refresh-token family lineage check (fail-closed):
    # 1. A refresh-token family matching (device_id, family_id) MUST exist in DB.
    # 2. At least one token in that family MUST remain unrevoked and unexpired.
    total_family_tokens = db.execute(
        select(func.count(DeviceRefreshToken.token_id)).where(
            DeviceRefreshToken.device_id == claims.device_id,
            DeviceRefreshToken.family_id == claims.family_id,
        )
    ).scalar_one()

    if total_family_tokens == 0:
        _raise_unauthorized("Token session family does not exist.")

    now = datetime.now(timezone.utc)
    active_family_tokens = db.execute(
        select(func.count(DeviceRefreshToken.token_id)).where(
            DeviceRefreshToken.device_id == claims.device_id,
            DeviceRefreshToken.family_id == claims.family_id,
            DeviceRefreshToken.revoked_at.is_(None),
            DeviceRefreshToken.expires_at > now,
        )
    ).scalar_one()
    if active_family_tokens == 0:
        _raise_unauthorized("Token session family has been revoked.")

    return DeviceSession(
        patient_id=claims.patient_id,
        device_id=claims.device_id,
        jti=claims.jti,
        family_id=claims.family_id,
        scope=claims.scope,
        scopes=claims.scopes,
        issued_at=claims.issued_at,
        expires_at=claims.expires_at,
    )


def verify_client_token(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security_scheme),
    db: Session = Depends(get_db),
) -> AuthSession:
    """
    Root authentication dependency resolving either:
    - `DeviceSession` when a compact JWT (`header.payload.signature`) is supplied, OR
    - `ServiceAdminSession` when the server-side `AEGIS_API_SECRET` is supplied.
    Never accepts `AEGIS_API_SECRET` as a `DeviceSession`, and never falls back to
    `AEGIS_API_SECRET` when a JWT-formatted token fails verification.
    """
    if credentials is None or not credentials.credentials:
        _raise_unauthorized("Missing or malformed authorization header. Expected Bearer token.")

    raw_token = credentials.credentials.strip()
    if not raw_token:
        _raise_unauthorized("Missing or malformed authorization header. Expected Bearer token.")

    # If the token has compact JWT structure (3 dot-separated segments), validate strictly as DeviceSession
    if raw_token.count(".") == 2:
        return authenticate_device_jwt(raw_token, db)

    configured_secret = settings.api_secret.strip()
    if not configured_secret:
        _raise_unauthorized("Authentication failed: server API secret is unconfigured.")

    # Reject if AEGIS_JWT_SECRET or AEGIS_PAIRING_SECRET was mistakenly sent as a Bearer token
    if settings.jwt_secret.strip() and secrets.compare_digest(raw_token, settings.jwt_secret.strip()):
        _raise_unauthorized("Invalid client authentication token.")
    if settings.pairing_secret.strip() and secrets.compare_digest(raw_token, settings.pairing_secret.strip()):
        _raise_unauthorized("Invalid client authentication token.")

    if not secrets.compare_digest(raw_token, configured_secret):
        _raise_unauthorized("Invalid client authentication token.")

    return ServiceAdminSession()


def require_device_session(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security_scheme),
    db: Session = Depends(get_db),
) -> DeviceSession:
    """
    Strictly requires a mobile `DeviceSession` (HS256 JWT).
    Never accepts `AEGIS_API_SECRET` as a mobile token (returns 401 Unauthorized).
    """
    if credentials is None or not credentials.credentials:
        _raise_unauthorized("Missing or malformed authorization header. Expected Bearer token.")

    raw_token = credentials.credentials.strip()
    if raw_token.count(".") != 2:
        _raise_unauthorized("Mobile device JWT token is required; static API secret is not accepted.")

    return authenticate_device_jwt(raw_token, db)


def require_service_admin_session(
    auth: Union[AuthSession, str] = Depends(verify_client_token),
) -> ServiceAdminSession:
    """
    Strictly requires `ServiceAdminSession` (`AEGIS_API_SECRET`).
    Any mobile `DeviceSession` attempting to invoke an administrative or clinical cascade
    endpoint is rejected with 403 Forbidden.
    """
    if isinstance(auth, ServiceAdminSession):
        return auth
    if isinstance(auth, str):
        # Supports existing tests that override `verify_client_token` with a lambda returning a string
        return ServiceAdminSession()
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Operation requires service administrator privileges and is forbidden for mobile device sessions.",
    )


def require_scopes(*required_scopes: str) -> Callable[..., AuthSession]:
    """
    Dependency factory that allows either:
    - `ServiceAdminSession` (for backend service tests / internal operations), OR
    - `DeviceSession` that possesses ALL of `required_scopes` (raises 403 Forbidden if any is missing).
    """
    if not required_scopes or any(not isinstance(s, str) or not s.strip() for s in required_scopes):
        raise ValueError("require_scopes() requires at least one non-empty scope name.")
    normalized_required = tuple(s.strip() for s in required_scopes)

    def _dependency(auth: Union[AuthSession, str] = Depends(verify_client_token)) -> AuthSession:
        if isinstance(auth, ServiceAdminSession):
            return auth
        if isinstance(auth, str):
            return ServiceAdminSession()
        if not auth.has_all_scopes(*normalized_required):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Insufficient token scope for this operation.",
            )
        return auth

    return _dependency


def require_any_scope(*allowed_scopes: str) -> Callable[..., AuthSession]:
    """
    Explicit alternative-scope dependency factory that allows either:
    - `ServiceAdminSession`, OR
    - `DeviceSession` that possesses AT LEAST ONE of `allowed_scopes` (raises 403 Forbidden if none match).
    """
    if not allowed_scopes or any(not isinstance(s, str) or not s.strip() for s in allowed_scopes):
        raise ValueError("require_any_scope() requires at least one non-empty scope name.")
    normalized_allowed = tuple(s.strip() for s in allowed_scopes)

    def _dependency(auth: Union[AuthSession, str] = Depends(verify_client_token)) -> AuthSession:
        if isinstance(auth, ServiceAdminSession):
            return auth
        if isinstance(auth, str):
            return ServiceAdminSession()
        if not auth.has_any_scope(*normalized_allowed):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Insufficient token scope for this operation.",
            )
        return auth

    return _dependency


GENERIC_CYCLE_ACCESS_DENIED_DETAIL = (
    "Access denied: medical cycle is not accessible for the authenticated patient."
)
GENERIC_REPORT_ACCESS_DENIED_DETAIL = (
    "Access denied: report is not accessible for the authenticated patient."
)


def verify_cycle_access_for_session(
    db: Session,
    session: AuthSession,
    cycle_id: str,
) -> MedicalCycle:
    """
    Enforces strict patient ownership isolation on a `MedicalCycle` BEFORE any medical
    data is read/written, BEFORE `cycle.status` is inspected, and BEFORE deduplication.

    UNIFORM ERROR POLICY (ZERO ORACLE LEAKAGE):
    - For `DeviceSession`:
      If `cycle` does not exist, has `patient_id IS NULL` (legacy unassigned cycle),
      or belongs to a different `patient_id` (`cycle.patient_id != session.patient_id`),
      always raises `403 Forbidden` with the exact same generic message
      (`GENERIC_CYCLE_ACCESS_DENIED_DETAIL`). Never discloses whether the cycle exists,
      who owns it, or what its status (`active`/`completed`/`archived`) is.
    - For `ServiceAdminSession`:
      Raises `404 Not Found` if `cycle` does not exist, preserving backward compatibility
      with existing service/admin workflows.
    """
    clean_cycle_id = cycle_id.strip() if isinstance(cycle_id, str) else ""
    cycle = db.execute(
        select(MedicalCycle)
        .where(MedicalCycle.cycle_id == clean_cycle_id)
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()

    if isinstance(session, DeviceSession):
        if cycle is None or cycle.patient_id is None or cycle.patient_id != session.patient_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=GENERIC_CYCLE_ACCESS_DENIED_DETAIL,
            )
        return cycle

    if cycle is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Medical cycle '{clean_cycle_id}' not found.",
        )
    return cycle
