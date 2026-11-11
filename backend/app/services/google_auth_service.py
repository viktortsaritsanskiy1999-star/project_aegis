from dataclasses import dataclass
from datetime import datetime, timezone
import logging
from typing import Any, Callable, Dict, List, Optional, Set

from google.auth.exceptions import GoogleAuthError as LibGoogleAuthError
from google.oauth2 import id_token as google_id_token
from google.auth.transport import requests as google_requests
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.config import settings
from app.models.beta_whitelist import BetaAccessWhitelist
from app.models.patient_google_identity import PatientGoogleIdentity

logger = logging.getLogger("aegis.google_auth")

ALLOWED_GOOGLE_ISSUERS: Set[str] = {
    "accounts.google.com",
    "https://accounts.google.com",
}


class GoogleAuthServiceError(Exception):
    """Base exception for Google Identity token verification failures."""


class GoogleAuthConfigError(GoogleAuthServiceError):
    """Raised when server Google OAuth configuration is missing or invalid."""


class GoogleTokenVerificationError(GoogleAuthServiceError):
    """Raised when an ID token fails cryptographic signature, audience, or validity checks."""


@dataclass(frozen=True)
class VerifiedGoogleClaims:
    """
    Validated, cryptographically verified claims extracted from a Google ID Token.
    
    Security Guarantee:
    Only `sub` is the immutable primary identifier for the Google Account.
    `email` is auxiliary metadata for beta whitelist invites and administrative logging;
    it must NEVER be used to link patients or as an identity primary key.
    """
    sub: str
    iss: str
    aud: str
    email: Optional[str]
    email_verified: bool
    exp: int
    iat: int
    raw_claims: Dict[str, Any]


class GoogleAuthService:
    """
    Service responsible for verifying Google ID Tokens presented by the mobile application.
    
    Invariants:
    1. Uses official Google Auth library (google.oauth2.id_token.verify_oauth2_token).
    2. Validates cryptographic signature against Google's public JWKS certificates.
    3. Validates `iss` strictly matches Google issuers (accounts.google.com).
    4. Validates `aud` matches configured authorized Aegis Google Client IDs.
    5. Validates `exp` and clock skew tolerances.
    6. Rejects any token lacking a non-empty `sub`.
    7. Supports verifier dependency injection for 100% deterministic, offline testing.
    """

    def __init__(
        self,
        allowed_client_ids: Optional[List[str]] = None,
        clock_skew_seconds: Optional[int] = None,
        custom_verifier: Optional[Callable[[str, Any, Optional[Any]], Dict[str, Any]]] = None,
    ) -> None:
        self._allowed_client_ids = (
            allowed_client_ids
            if allowed_client_ids is not None
            else settings.parsed_google_client_ids
        )
        self._clock_skew_seconds = (
            clock_skew_seconds
            if clock_skew_seconds is not None
            else settings.google_token_clock_skew_seconds
        )
        self._custom_verifier = custom_verifier
        self._http_request = google_requests.Request()

    @property
    def allowed_client_ids(self) -> List[str]:
        return list(self._allowed_client_ids)

    def verify_token(self, raw_id_token: str) -> VerifiedGoogleClaims:
        """
        Verifies the cryptographic signature, expiration, issuer, and audience of a Google ID Token.
        Returns a `VerifiedGoogleClaims` object upon success.
        Raises `GoogleTokenVerificationError` or `GoogleAuthConfigError` on failure.
        """
        clean_token = (raw_id_token or "").strip()
        if not clean_token:
            raise GoogleTokenVerificationError("Google ID Token is missing or empty.")

        # If configured client IDs are empty, fail-closed unless in testing mode with custom verifier
        if not self._allowed_client_ids and self._custom_verifier is None:
            raise GoogleAuthConfigError(
                "Google Sign-In is unavailable: AEGIS_GOOGLE_CLIENT_IDS is not configured."
            )

        try:
            if self._custom_verifier is not None:
                # Custom/mock verifier for isolated testing
                payload = self._custom_verifier(
                    clean_token,
                    self._http_request,
                    self._allowed_client_ids,
                )
            else:
                # Production path: official google-auth library
                # If multiple client IDs are configured, verify against audience container
                aud_param = (
                    self._allowed_client_ids[0]
                    if len(self._allowed_client_ids) == 1
                    else self._allowed_client_ids
                )
                payload = google_id_token.verify_oauth2_token(
                    clean_token,
                    self._http_request,
                    audience=aud_param,
                    clock_skew_in_seconds=self._clock_skew_seconds,
                )
        except (ValueError, LibGoogleAuthError) as exc:
            logger.warning(f"Google ID Token verification rejected: {exc}")
            raise GoogleTokenVerificationError(
                f"Google ID Token verification failed: {exc}"
            ) from exc
        except Exception as exc:
            logger.error(f"Unexpected error during Google ID Token verification: {exc}")
            raise GoogleTokenVerificationError(
                "Internal error during token verification."
            ) from exc

        if not isinstance(payload, dict):
            raise GoogleTokenVerificationError("Google ID Token payload is not a valid dictionary.")

        # 1. Verify issuer
        iss = str(payload.get("iss", "")).strip()
        if iss not in ALLOWED_GOOGLE_ISSUERS:
            raise GoogleTokenVerificationError(
                f"Invalid token issuer '{iss}'. Expected one of {ALLOWED_GOOGLE_ISSUERS}."
            )

        # 2. Verify audience matches allowed client IDs
        aud = str(payload.get("aud", "")).strip()
        if self._allowed_client_ids and aud not in self._allowed_client_ids:
            raise GoogleTokenVerificationError(
                f"Token audience '{aud}' does not match authorized client IDs."
            )

        # 3. Verify sub (Google User ID) is present and non-empty
        sub = str(payload.get("sub", "")).strip()
        if not sub:
            raise GoogleTokenVerificationError("Token is missing the mandatory 'sub' subject claim.")

        # 4. Extract email if present
        raw_email = payload.get("email")
        clean_email = str(raw_email).strip().lower() if raw_email else None
        email_verified = bool(payload.get("email_verified", False))

        # 5. Temporal claims
        exp = int(payload.get("exp", 0))
        iat = int(payload.get("iat", 0))

        return VerifiedGoogleClaims(
            sub=sub,
            iss=iss,
            aud=aud,
            email=clean_email,
            email_verified=email_verified,
            exp=exp,
            iat=iat,
            raw_claims=payload,
        )


# Singleton factory with dependency injection support
_google_auth_service: Optional[GoogleAuthService] = None


def get_google_auth_service() -> GoogleAuthService:
    global _google_auth_service
    if _google_auth_service is None:
        _google_auth_service = GoogleAuthService()
    return _google_auth_service


def override_google_auth_service(service: Optional[GoogleAuthService]) -> None:
    """Used in testing to override or reset the GoogleAuthService singleton."""
    global _google_auth_service
    _google_auth_service = service


def is_patient_beta_revoked(db: Session, patient_id: str) -> bool:
    """
    Checks whether a patient registered via Google has had their closed beta access revoked,
    or is disallowed under closed beta policies.

    Invariants:
    1. Returns False for non-Google patients (e.g., paired via 6-digit clinical code).
    2. Returns True if any matching BetaAccessWhitelist entry is inactive or revoked.
    3. In closed beta mode (beta_registration_open == False), returns True if no active
       non-revoked whitelist entry exists for this Google account.
    4. Evaluates against live DB state without caching.
    """
    google_id = db.scalar(
        select(PatientGoogleIdentity)
        .where(PatientGoogleIdentity.patient_id == patient_id)
        .execution_options(populate_existing=True)
    )
    if google_id is None:
        return False

    ident_values = [google_id.google_sub]
    if google_id.email:
        ident_values.append(google_id.email.lower().strip())

    # 1. Check for explicit revocation (active = False OR revoked_at is not None)
    revoked = db.scalar(
        select(BetaAccessWhitelist.id).where(
            BetaAccessWhitelist.identifier_value.in_(ident_values),
            or_(
                BetaAccessWhitelist.is_active.is_(False),
                BetaAccessWhitelist.revoked_at.isnot(None),
            ),
        )
    )
    if revoked is not None:
        return True

    # 2. In closed beta mode, verify that an active whitelist grant exists
    if not settings.beta_registration_open:
        allowed = db.scalar(
            select(BetaAccessWhitelist.id).where(
                BetaAccessWhitelist.identifier_value.in_(ident_values),
                BetaAccessWhitelist.is_active.is_(True),
                BetaAccessWhitelist.revoked_at.is_(None),
            )
        )
        if allowed is None:
            return True

    return False
