from datetime import datetime, timezone
import hashlib
import hmac
import uuid
from typing import Any, Optional, TYPE_CHECKING
from sqlalchemy import String, DateTime, Integer, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.config import settings
from app.models.base import Base

if TYPE_CHECKING:
    from app.models.patient import Patient

PAIRING_HMAC_DOMAIN = "aegis.pairing.v1"


class PairingCode(Base):
    """
    Short-lived, single-use pairing code for secure code-only device pairing.
    Plaintext 6-digit codes are NEVER stored in the database or written to logs.
    Only HMAC-SHA256 digests keyed with a mandatory server-side secret stored
    outside the database (AEGIS_PAIRING_SECRET) are persisted.
    - `code_hash`: immutable audit hash of the issued code.
    - `active_code_hash`: nullable UNIQUE column populated only while the code is active;
      cleared to NULL upon consumption, expiration, supersession, or rate-limit lockout.
      Guarantees global uniqueness across all active pairing procedures so `patient_id`
      can be resolved deterministically from the 6-digit code alone.
    """
    __tablename__ = "pairing_codes"

    code_id: Mapped[str] = mapped_column(
        String(64),
        primary_key=True,
        index=True,
        default=lambda: str(uuid.uuid4()),
    )
    patient_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("patients.patient_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    code_hash: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        index=True,
    )
    active_code_hash: Mapped[str | None] = mapped_column(
        String(128),
        nullable=True,
        unique=True,
        index=True,
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
    )
    consumed_at: Mapped[datetime | None] = mapped_column(
        DateTime,
        nullable=True,
    )
    attempts_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
    )
    max_attempts: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=5,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    # Relationships
    patient: Mapped["Patient"] = relationship(
        "Patient",
        back_populates="pairing_codes",
    )

    def __init__(self, **kwargs: Any) -> None:
        if "active_code_hash" not in kwargs:
            if kwargs.get("consumed_at") is None and "code_hash" in kwargs:
                kwargs["active_code_hash"] = kwargs["code_hash"]
        super().__init__(**kwargs)

    @staticmethod
    def resolve_secret(secret: Optional[str] = None) -> str:
        """
        Resolves and validates the server-side pairing secret.
        Fails closed (raises ValueError) if the secret is missing, None, or empty/whitespace.
        Never falls back to AEGIS_API_SECRET.
        """
        effective = settings.pairing_secret if secret is None else secret
        if not isinstance(effective, str) or not effective.strip():
            raise ValueError(
                "Pairing code secret is required and must not be empty. "
                "Configure a non-empty AEGIS_PAIRING_SECRET outside the database."
            )
        return effective.strip()

    @staticmethod
    def normalize_code(raw_code: str, strict_six_digits: bool = False) -> str:
        """Normalizes and validates a pairing code (preserving leading zeros) without logging it."""
        if not isinstance(raw_code, str):
            raise ValueError("Pairing code must be a string.")
        clean = raw_code.strip().replace("-", "").replace(" ", "")
        if not clean:
            raise ValueError("Pairing code must not be empty.")
        if strict_six_digits and (len(clean) != 6 or not clean.isdigit()):
            raise ValueError("Pairing code must be a 6-digit numeric string (preserving leading zeros).")
        return clean

    @staticmethod
    def hash_code(raw_code: str, secret: Optional[str] = None) -> str:
        """
        Computes standardized HMAC-SHA256 digest of a 6-digit pairing code using
        a mandatory server secret stored outside the database.
        Fails closed (raises ValueError) if secret is missing or empty.
        """
        resolved_secret = PairingCode.resolve_secret(secret)
        clean_code = PairingCode.normalize_code(raw_code)
        key_bytes = resolved_secret.encode("utf-8")
        msg_bytes = f"{PAIRING_HMAC_DOMAIN}:{clean_code}".encode("utf-8")
        return hmac.new(key_bytes, msg_bytes, hashlib.sha256).hexdigest()

    @property
    def is_consumed(self) -> bool:
        return self.consumed_at is not None

    @property
    def is_expired(self) -> bool:
        now = datetime.now(timezone.utc)
        exp = self.expires_at
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        return now >= exp

    @property
    def is_rate_limited(self) -> bool:
        return self.attempts_count >= self.max_attempts

    @property
    def is_valid_for_use(self) -> bool:
        return not self.is_consumed and not self.is_expired and not self.is_rate_limited
