from typing import Any, Callable, Coroutine, Optional
from fastapi import APIRouter, Body, Depends, HTTPException, Request, Response, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.device_refresh_token import DeviceRefreshToken
from app.repositories.device_repository import DeviceRepository
from app.schemas.auth import (
    DevicePairRequest,
    DevicePairResponse,
    DeviceRevokeRequest,
    DeviceRevokeResponse,
    TokenRefreshRequest,
    TokenRefreshResponse,
)
from app.security.auth import require_device_session, security_scheme
from app.security.rate_limiter import (
    RateLimitExceededError,
    RateLimiterUnavailableError,
    resolve_client_ip,
)
from app.security.tokens import TokenConfigurationError
from app.services.auth_service import AuthService

NO_STORE_HEADERS = {
    "Cache-Control": "no-store",
    "Pragma": "no-cache",
}
GENERIC_PAIRING_FAILURE_DETAIL = "Invalid or unacceptable pairing credentials."

SENSITIVE_FIELD_NAMES = frozenset(
    {"pairing_code", "refresh_token", "access_token", "password", "secret"}
)


def _sanitize_validation_payload(val: Any) -> Any:
    """Recursively strips sensitive keys from nested error input dictionaries."""
    if isinstance(val, dict):
        return {
            k: _sanitize_validation_payload(v)
            for k, v in val.items()
            if str(k).lower() not in SENSITIVE_FIELD_NAMES
        }
    if isinstance(val, list):
        return [_sanitize_validation_payload(x) for x in val]
    return val


def _sanitize_validation_errors(raw_errors: list[dict]) -> list[dict]:
    """
    Sanitizes validation error dictionaries from `RequestValidationError`:
    - Completely removes `input` when the error's `loc` references sensitive credentials.
    - Strips sensitive credential keys from composite body `input` and `ctx` dictionaries.
    """
    sanitized = []
    for err in raw_errors:
        clean = dict(err)
        loc_parts = {str(p).lower() for p in clean.get("loc", ())}
        if loc_parts.intersection(SENSITIVE_FIELD_NAMES):
            clean.pop("input", None)
        elif "input" in clean:
            clean["input"] = _sanitize_validation_payload(clean["input"])
        if "ctx" in clean and isinstance(clean["ctx"], dict):
            clean["ctx"] = _sanitize_validation_payload(clean["ctx"])
        sanitized.append(clean)
    return sanitized


class AuthAPIRoute(APIRoute):
    """
    Router-scoped APIRoute ensuring that:
    1. All responses under `/api/v1/auth/*` (including HTTP errors and validation failures)
       strictly contain `Cache-Control: no-store` and `Pragma: no-cache`.
    2. RequestValidationError payloads on auth endpoints never echo raw sensitive inputs
       (such as invalid pairing codes, refresh tokens, or passwords) in `input`, `detail`, or `ctx`.
    3. Scoped strictly to the auth router without modifying global app exception handlers.
    """
    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        original_handler = super().get_route_handler()

        async def auth_route_handler(request: Request) -> Response:
            try:
                response = await original_handler(request)
            except RequestValidationError as exc:
                clean_errors = _sanitize_validation_errors(jsonable_encoder(exc.errors()))
                return JSONResponse(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    content={"detail": clean_errors},
                    headers=dict(NO_STORE_HEADERS),
                )
            except HTTPException as exc:
                headers = dict(NO_STORE_HEADERS)
                if exc.headers:
                    headers.update(exc.headers)
                return JSONResponse(
                    status_code=exc.status_code,
                    content={"detail": exc.detail},
                    headers=headers,
                )

            for k, v in NO_STORE_HEADERS.items():
                response.headers[k] = v
            return response

        return auth_route_handler


router = APIRouter(
    prefix="/api/v1/auth",
    tags=["Device Authentication"],
    route_class=AuthAPIRoute,
)


def get_auth_service(db: Session = Depends(get_db)) -> AuthService:
    """
    Provides an initialized `AuthService` with mandatory `PersistentRateLimiter`.
    Fails closed (503 Service Unavailable) if the SQLite rate-limiter storage is missing or broken.
    """
    try:
        return AuthService(db)
    except RateLimiterUnavailableError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Authentication rate limiter is unavailable (fail-closed).",
            headers=NO_STORE_HEADERS,
        ) from exc


@router.post(
    "/pair",
    response_model=DevicePairResponse,
    summary="Atomically pair a mobile device using a single-use 6-digit pairing code",
    description=(
        "Protected by persistent SQLite multi-bucket rate limiting (Per-IP, Per-Device, Global Budget). "
        "Atomically verifies and consumes the 6-digit pairing code, registers the device to the patient, "
        "revokes any stale device refresh tokens, persists a new refresh token, and issues a short-lived "
        "HS256 JWT access token."
    ),
)
def pair_device_endpoint(
    payload: DevicePairRequest,
    request: Request,
    response: Response,
    auth_service: AuthService = Depends(get_auth_service),
) -> DevicePairResponse:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"

    client_host = request.client.host if request.client is not None else None
    client_ip = resolve_client_ip(client_host, request.headers)

    try:
        ok, _msg, bundle = auth_service.pair_device_and_issue_tokens(
            raw_code=payload.pairing_code,
            device_name=payload.device_name,
            platform=payload.platform,
            device_id=payload.device_id,
            client_ip=client_ip,
        )
    except RateLimitExceededError as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many failed pairing attempts. Please retry later.",
            headers={
                "Retry-After": str(exc.retry_after),
                "Cache-Control": "no-store",
                "Pragma": "no-cache",
            },
        ) from exc
    except RateLimiterUnavailableError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Authentication rate limiter is unavailable (fail-closed).",
            headers=NO_STORE_HEADERS,
        ) from exc
    except TokenConfigurationError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Server authentication configuration is unavailable (fail-closed).",
            headers=NO_STORE_HEADERS,
        ) from exc
    except ValueError as exc:
        err_lower = str(exc).lower()
        if "pairing secret" in err_lower:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Server pairing configuration is unavailable (fail-closed).",
                headers=NO_STORE_HEADERS,
            ) from exc
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=GENERIC_PAIRING_FAILURE_DETAIL,
            headers={"WWW-Authenticate": "Bearer", **NO_STORE_HEADERS},
        ) from exc

    if not ok or bundle is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=GENERIC_PAIRING_FAILURE_DETAIL,
            headers={"WWW-Authenticate": "Bearer", **NO_STORE_HEADERS},
        )

    return DevicePairResponse(
        access_token=bundle.access_token,
        token_type="Bearer",
        expires_in=bundle.access_token_expires_in,
        expires_at=bundle.access_token_expires_at,
        refresh_token=bundle.refresh_token,
        refresh_expires_at=bundle.refresh_token_entity.expires_at,
        patient_id=bundle.patient_id,
        device_id=bundle.device_id,
        scope=bundle.scope,
    )


@router.post(
    "/refresh",
    response_model=TokenRefreshResponse,
    summary="Atomically rotate a device refresh token and issue a new JWT access token",
    description=(
        "Verifies refresh token authenticity, enforces active Device and Patient status, "
        "performs atomic single-use rotation, and triggers automatic family revocation on "
        "compromised token reuse."
    ),
)
def refresh_tokens_endpoint(
    payload: TokenRefreshRequest,
    response: Response,
    auth_service: AuthService = Depends(get_auth_service),
) -> TokenRefreshResponse:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"

    try:
        ok, _msg, bundle = auth_service.refresh_device_tokens(
            device_id=payload.device_id,
            old_raw_refresh_token=payload.refresh_token,
        )
    except TokenConfigurationError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Server authentication configuration is unavailable (fail-closed).",
            headers=NO_STORE_HEADERS,
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid, expired, or revoked refresh token.",
            headers={"WWW-Authenticate": "Bearer", **NO_STORE_HEADERS},
        ) from exc

    if not ok or bundle is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid, expired, or revoked refresh token.",
            headers={"WWW-Authenticate": "Bearer", **NO_STORE_HEADERS},
        )

    return TokenRefreshResponse(
        access_token=bundle.access_token,
        token_type="Bearer",
        expires_in=bundle.access_token_expires_in,
        expires_at=bundle.access_token_expires_at,
        refresh_token=bundle.refresh_token,
        refresh_expires_at=bundle.refresh_token_entity.expires_at,
        patient_id=bundle.patient_id,
        device_id=bundle.device_id,
        scope=bundle.scope,
    )


@router.post(
    "/revoke",
    response_model=DeviceRevokeResponse,
    summary="Revoke the authenticated device's own session and all its refresh tokens",
    description=(
        "Allows a paired mobile device to revoke ONLY its own session (either via Bearer "
        "DeviceSession JWT or via its own `(device_id, refresh_token)` pair). "
        "Attempting to revoke another `device_id` is rejected with 403 Forbidden. "
        "Immediately invalidates the device's access and refresh tokens via server-side Device check."
    ),
)
def revoke_device_session_endpoint(
    response: Response,
    payload: Optional[DeviceRevokeRequest] = Body(default=None),
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security_scheme),
    db: Session = Depends(get_db),
) -> DeviceRevokeResponse:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"

    device_repo = DeviceRepository(db)

    if credentials is not None and credentials.credentials and credentials.credentials.strip():
        session = require_device_session(credentials=credentials, db=db)

        if payload is not None:
            if payload.device_id is not None and payload.device_id.strip() != session.device_id:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Device can only revoke its own session.",
                    headers=NO_STORE_HEADERS,
                )
            if payload.refresh_token is not None:
                supplied_hash = DeviceRefreshToken.hash_token(payload.refresh_token.strip())
                token_row = db.execute(
                    select(DeviceRefreshToken)
                    .where(DeviceRefreshToken.token_hash == supplied_hash)
                    .execution_options(populate_existing=True)
                ).scalar_one_or_none()
                if token_row is not None and token_row.device_id != session.device_id:
                    raise HTTPException(
                        status_code=status.HTTP_403_FORBIDDEN,
                        detail="Device can only revoke its own session.",
                        headers=NO_STORE_HEADERS,
                    )

        device_repo.revoke_device(session.device_id, commit=True)
        return DeviceRevokeResponse(
            status="revoked",
            device_id=session.device_id,
            revoked=True,
        )

    # Fallback path: no Bearer token supplied; require valid (device_id, refresh_token) for self-revocation
    if (
        payload is None
        or payload.device_id is None
        or not payload.device_id.strip()
        or payload.refresh_token is None
        or not payload.refresh_token.strip()
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or malformed authorization credentials.",
            headers={"WWW-Authenticate": "Bearer", **NO_STORE_HEADERS},
        )

    target_device_id = payload.device_id.strip()
    token_hash = DeviceRefreshToken.hash_token(payload.refresh_token.strip())
    token_row = db.execute(
        select(DeviceRefreshToken)
        .where(DeviceRefreshToken.token_hash == token_hash)
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()

    if token_row is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired refresh token.",
            headers={"WWW-Authenticate": "Bearer", **NO_STORE_HEADERS},
        )

    if token_row.device_id != target_device_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Device can only revoke its own session.",
            headers=NO_STORE_HEADERS,
        )

    if token_row.is_revoked or token_row.is_expired:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired refresh token.",
            headers={"WWW-Authenticate": "Bearer", **NO_STORE_HEADERS},
        )

    device_repo.revoke_device(target_device_id, commit=True)
    return DeviceRevokeResponse(
        status="revoked",
        device_id=target_device_id,
        revoked=True,
    )
