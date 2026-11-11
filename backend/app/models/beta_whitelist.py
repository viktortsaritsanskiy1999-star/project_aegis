from datetime import datetime, timezone
from typing import Optional
import uuid

from sqlalchemy import Boolean, DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class BetaAccessWhitelist(Base):
    """
    Administrative closed beta allowlist.
    Controls whether a Google account is permitted to register a new Aegis medical profile
    or access the system during the closed beta phase.
    
    Can whitelist by:
    - 'google_sub': Specific verified Google account sub (most secure).
    - 'email': Normalized email address for pre-registration beta invitations.
    """
    __tablename__ = "beta_access_whitelist"

    id: Mapped[str] = mapped_column(
        String(64),
        primary_key=True,
        index=True,
        default=lambda: str(uuid.uuid4()),
    )
    identifier_type: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
    )  # "google_sub" or "email"
    identifier_value: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        index=True,
    )  # exact google_sub or lowercased email
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        index=True,
    )
    granted_by: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        default="admin",
    )
    notes: Mapped[Optional[str]] = mapped_column(
        String(255),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    revoked_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime,
        nullable=True,
    )
