from datetime import datetime, timezone
import hashlib
import uuid
from typing import TYPE_CHECKING
from sqlalchemy import String, DateTime, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.models.base import Base

if TYPE_CHECKING:
    from app.models.device import Device


class DeviceRefreshToken(Base):
    """
    Cryptographically hashed long-lived refresh token associated with a specific device.
    Only SHA-256 token hashes are stored on the server.
    Implements token family lineage tracking for automatic refresh token reuse detection:
    if a revoked token is presented for refresh, the entire family is revoked immediately.
    """
    __tablename__ = "device_refresh_tokens"

    token_id: Mapped[str] = mapped_column(
        String(64),
        primary_key=True,
        index=True,
        default=lambda: str(uuid.uuid4()),
    )
    device_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("devices.device_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    token_hash: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        unique=True,
        index=True,
    )
    family_id: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        index=True,
        default=lambda: str(uuid.uuid4()),
    )
    replaced_by_token_id: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
    )
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime,
        nullable=True,
    )

    # Relationships
    device: Mapped["Device"] = relationship(
        "Device",
        back_populates="refresh_tokens",
    )

    @staticmethod
    def hash_token(raw_token: str) -> str:
        """Computes standardized SHA-256 hash of refresh token secret."""
        if not isinstance(raw_token, str) or not raw_token.strip():
            raise ValueError("Refresh token secret must not be empty.")
        return hashlib.sha256(raw_token.strip().encode("utf-8")).hexdigest()

    @property
    def is_revoked(self) -> bool:
        return self.revoked_at is not None

    @property
    def is_expired(self) -> bool:
        now = datetime.now(timezone.utc)
        exp = self.expires_at
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        return now >= exp

    @property
    def is_active(self) -> bool:
        return not self.is_revoked and not self.is_expired
