from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import FrozenSet, Optional, Tuple
import uuid
import jwt
from app.config import settings

ALLOWED_JWT_ALGORITHMS: Tuple[str, ...] = ("HS256",)
MIN_JWT_SECRET_LENGTH: int = 16

# Standard mobile device scopes
SCOPE_VITALS_READ: str = "vitals:read"
SCOPE_VITALS_WRITE: str = "vitals:write"
SCOPE_CYCLE_READ: str = "cycle:read"

# Stage 5.4 Unified AI granular scopes
SCOPE_CASCADE_INTAKE: str = "cascade:intake"
SCOPE_CASCADE_RUN: str = "cascade:run"
SCOPE_REPORTS_READ: str = "reports:read"
SCOPE_CHAT_READ: str = "chat:read"
SCOPE_CHAT_WRITE: str = "chat:write"

# Standard permissions issued to paired mobile devices
STANDARD_DEVICE_SCOPES: Tuple[str, ...] = (
    SCOPE_VITALS_READ,
    SCOPE_VITALS_WRITE,
    SCOPE_CYCLE_READ,
    SCOPE_CASCADE_INTAKE,
    SCOPE_CASCADE_RUN,
    SCOPE_REPORTS_READ,
    SCOPE_CHAT_READ,
    SCOPE_CHAT_WRITE,
)
DEFAULT_DEVICE_SCOPE: str = " ".join(STANDARD_DEVICE_SCOPES)

# Legacy Stage 5.3 baseline scope (for backwards-compatibility testing)
LEGACY_STAGE_53_DEVICE_SCOPES: Tuple[str, ...] = (
    SCOPE_VITALS_READ,
    SCOPE_VITALS_WRITE,
    SCOPE_CYCLE_READ,
)
LEGACY_DEVICE_SCOPE: str = " ".join(LEGACY_STAGE_53_DEVICE_SCOPES)


class TokenError(ValueError):
    """Base exception for all JWT access token errors."""


class TokenConfigurationError(TokenError):
    """Raised when server JWT secret or configuration is missing or insecure (fail-closed)."""


class TokenValidationError(TokenError):
    """Raised when a JWT has an invalid signature, header, algorithm, issuer, audience, or claims."""


class TokenExpiredError(TokenValidationError):
    """Raised when a JWT access token has expired."""


@dataclass(frozen=True)
class AccessTokenClaims:
    """Verified, immutable claims extracted from a valid device HS256 JWT access token."""
    patient_id: str
    device_id: str
    jti: str
    family_id: str
    scope: str
    scopes: FrozenSet[str]
    iss: str
    aud: str
    iat: int
    exp: int

    @property
    def issued_at(self) -> datetime:
        return datetime.fromtimestamp(self.iat, tz=timezone.utc)

    @property
    def expires_at(self) -> datetime:
        return datetime.fromtimestamp(self.exp, tz=timezone.utc)

    def has_scope(self, required_scope: str) -> bool:
        return required_scope in self.scopes


@dataclass(frozen=True)
class IssuedAccessToken:
    """Result of issuing a new short-lived device access token."""
    access_token: str
    claims: AccessTokenClaims
    expires_in: int


def resolve_jwt_secret(secret: Optional[str] = None) -> str:
    """
    Resolves and validates the server-side JWT signing secret (fail-closed).
    - Rejects missing, None, empty/whitespace, or weak secrets (< 16 chars).
    - Strictly prohibits reusing AEGIS_API_SECRET or AEGIS_PAIRING_SECRET.
    - Never logs or leaks secret values in exception messages.
    """
    effective = settings.jwt_secret if secret is None else secret
    if not isinstance(effective, str) or not effective.strip():
        raise TokenConfigurationError(
            "JWT signing secret is required and must not be empty. "
            "Configure a non-empty AEGIS_JWT_SECRET outside the database."
        )

    clean = effective.strip()
    if len(clean) < MIN_JWT_SECRET_LENGTH:
        raise TokenConfigurationError(
            f"JWT signing secret must be at least {MIN_JWT_SECRET_LENGTH} characters long."
        )

    api_sec = settings.api_secret.strip() if isinstance(settings.api_secret, str) else ""
    if api_sec and clean == api_sec:
        raise TokenConfigurationError(
            "JWT signing secret (AEGIS_JWT_SECRET) must be distinct from AEGIS_API_SECRET."
        )

    pair_sec = settings.pairing_secret.strip() if isinstance(settings.pairing_secret, str) else ""
    if pair_sec and clean == pair_sec:
        raise TokenConfigurationError(
            "JWT signing secret (AEGIS_JWT_SECRET) must be distinct from AEGIS_PAIRING_SECRET."
        )

    return clean


def _validate_non_empty_str_claim(name: str, value: object) -> str:
    if not isinstance(value, str):
        raise TokenValidationError(f"Claim '{name}' must be a string.")
    clean = value.strip()
    if not clean or clean != value:
        raise TokenValidationError(
            f"Claim '{name}' must be a non-empty string without leading/trailing whitespace."
        )
    return clean


def _validate_int_timestamp_claim(name: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TokenValidationError(f"Claim '{name}' must be an integer timestamp.")
    if value <= 0:
        raise TokenValidationError(f"Claim '{name}' must be a positive integer timestamp.")
    return value


def create_access_token(
    *,
    patient_id: str,
    device_id: str,
    family_id: str,
    scope: str = DEFAULT_DEVICE_SCOPE,
    ttl_minutes: Optional[int] = None,
    jti: Optional[str] = None,
    issuer: Optional[str] = None,
    audience: Optional[str] = None,
    secret: Optional[str] = None,
    now: Optional[datetime] = None,
) -> IssuedAccessToken:
    """
    Issues a signed HS256 JWT access token for a paired patient device.
    - Enforces strict claim validation prior to signing.
    - Fails closed if AEGIS_JWT_SECRET is missing or collides with other secrets.
    - Never writes the JWT or secret to logs.
    """
    resolved_secret = resolve_jwt_secret(secret)

    clean_patient_id = _validate_non_empty_str_claim("patient_id", patient_id)
    clean_device_id = _validate_non_empty_str_claim("device_id", device_id)
    clean_family_id = _validate_non_empty_str_claim("family_id", family_id)
    clean_scope = _validate_non_empty_str_claim("scope", scope)
    scopes_set = frozenset(part for part in clean_scope.split(" ") if part)
    if not scopes_set:
        raise TokenValidationError("Claim 'scope' must contain at least one valid permission scope.")

    clean_jti = _validate_non_empty_str_claim("jti", jti if jti is not None else str(uuid.uuid4()))
    clean_iss = _validate_non_empty_str_claim(
        "iss",
        settings.jwt_issuer if issuer is None else issuer,
    )
    clean_aud = _validate_non_empty_str_claim(
        "aud",
        settings.jwt_audience if audience is None else audience,
    )

    effective_ttl = settings.access_token_ttl_minutes if ttl_minutes is None else ttl_minutes
    if isinstance(effective_ttl, bool) or not isinstance(effective_ttl, int) or effective_ttl <= 0:
        raise TokenValidationError("ttl_minutes must be a positive integer.")

    now_utc = now if now is not None else datetime.now(timezone.utc)
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)

    iat_ts = int(now_utc.timestamp())
    exp_dt = now_utc + timedelta(minutes=effective_ttl)
    exp_ts = int(exp_dt.timestamp())
    expires_in = exp_ts - iat_ts

    payload = {
        "iss": clean_iss,
        "aud": clean_aud,
        "sub": clean_patient_id,
        "patient_id": clean_patient_id,
        "device_id": clean_device_id,
        "jti": clean_jti,
        "family_id": clean_family_id,
        "scope": clean_scope,
        "iat": iat_ts,
        "exp": exp_ts,
    }

    encoded_jwt = jwt.encode(
        payload=payload,
        key=resolved_secret,
        algorithm="HS256",
        headers={"alg": "HS256", "typ": "JWT"},
    )

    claims = AccessTokenClaims(
        patient_id=clean_patient_id,
        device_id=clean_device_id,
        jti=clean_jti,
        family_id=clean_family_id,
        scope=clean_scope,
        scopes=scopes_set,
        iss=clean_iss,
        aud=clean_aud,
        iat=iat_ts,
        exp=exp_ts,
    )
    return IssuedAccessToken(
        access_token=encoded_jwt,
        claims=claims,
        expires_in=expires_in,
    )


def verify_access_token(
    raw_token: str,
    *,
    secret: Optional[str] = None,
    expected_issuer: Optional[str] = None,
    expected_audience: Optional[str] = None,
) -> AccessTokenClaims:
    """
    Verifies a compact HS256 JWT access token and returns its validated claims.
    - Fails closed if AEGIS_JWT_SECRET is missing or invalid.
    - Strictly enforces header alg == 'HS256' and typ == 'JWT' (defeating alg confusion).
    - Verifies cryptographic signature, expiration (exp), issued-at (iat), issuer (iss), and audience (aud).
    - Validates exact types and semantic consistency of all required claims.
    """
    resolved_secret = resolve_jwt_secret(secret)

    if not isinstance(raw_token, str) or not raw_token.strip():
        raise TokenValidationError("Access token must be a non-empty string.")
    clean_token = raw_token.strip()
    if clean_token.count(".") != 2:
        raise TokenValidationError("Malformed JWT structure.")

    target_iss = _validate_non_empty_str_claim(
        "expected_issuer",
        settings.jwt_issuer if expected_issuer is None else expected_issuer,
    )
    target_aud = _validate_non_empty_str_claim(
        "expected_audience",
        settings.jwt_audience if expected_audience is None else expected_audience,
    )

    try:
        unverified_header = jwt.get_unverified_header(clean_token)
    except jwt.PyJWTError as exc:
        raise TokenValidationError("Malformed JWT header.") from exc

    header_alg = unverified_header.get("alg")
    header_typ = unverified_header.get("typ")
    if header_alg not in ALLOWED_JWT_ALGORITHMS:
        raise TokenValidationError("Unsupported or forbidden JWT signing algorithm.")
    if header_typ != "JWT":
        raise TokenValidationError("Invalid JWT header type; expected 'JWT'.")

    try:
        decoded = jwt.decode(
            jwt=clean_token,
            key=resolved_secret,
            algorithms=list(ALLOWED_JWT_ALGORITHMS),
            issuer=target_iss,
            audience=target_aud,
            options={
                "require": [
                    "iss",
                    "aud",
                    "sub",
                    "patient_id",
                    "device_id",
                    "jti",
                    "family_id",
                    "scope",
                    "iat",
                    "exp",
                ],
                "verify_signature": True,
                "verify_exp": True,
                "verify_iat": True,
                "verify_iss": True,
                "verify_aud": True,
            },
        )
    except jwt.ExpiredSignatureError as exc:
        raise TokenExpiredError("Access token has expired.") from exc
    except jwt.ImmatureSignatureError as exc:
        raise TokenValidationError("Claim 'iat' is in the future.") from exc
    except jwt.PyJWTError as exc:
        raise TokenValidationError("Access token verification failed.") from exc

    if not isinstance(decoded, dict):
        raise TokenValidationError("Decoded JWT payload must be a JSON object.")

    iss_val = _validate_non_empty_str_claim("iss", decoded.get("iss"))
    aud_val = _validate_non_empty_str_claim("aud", decoded.get("aud"))
    sub_val = _validate_non_empty_str_claim("sub", decoded.get("sub"))
    patient_id_val = _validate_non_empty_str_claim("patient_id", decoded.get("patient_id"))
    device_id_val = _validate_non_empty_str_claim("device_id", decoded.get("device_id"))
    jti_val = _validate_non_empty_str_claim("jti", decoded.get("jti"))
    family_id_val = _validate_non_empty_str_claim("family_id", decoded.get("family_id"))
    scope_val = _validate_non_empty_str_claim("scope", decoded.get("scope"))
    iat_val = _validate_int_timestamp_claim("iat", decoded.get("iat"))
    exp_val = _validate_int_timestamp_claim("exp", decoded.get("exp"))

    if sub_val != patient_id_val:
        raise TokenValidationError("Claim 'sub' must match 'patient_id'.")
    if iss_val != target_iss:
        raise TokenValidationError("Claim 'iss' mismatch.")
    if aud_val != target_aud:
        raise TokenValidationError("Claim 'aud' mismatch.")
    if exp_val <= iat_val:
        raise TokenValidationError("Claim 'exp' must be strictly greater than 'iat'.")

    now_ts = int(datetime.now(timezone.utc).timestamp())
    if iat_val > now_ts + 60:
        raise TokenValidationError("Claim 'iat' is in the future.")

    scopes_set = frozenset(part for part in scope_val.split(" ") if part)
    if not scopes_set:
        raise TokenValidationError("Claim 'scope' must contain at least one valid scope.")

    return AccessTokenClaims(
        patient_id=patient_id_val,
        device_id=device_id_val,
        jti=jti_val,
        family_id=family_id_val,
        scope=scope_val,
        scopes=scopes_set,
        iss=iss_val,
        aud=aud_val,
        iat=iat_val,
        exp=exp_val,
    )
