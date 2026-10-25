from datetime import datetime, timezone
from typing import List, Optional, TYPE_CHECKING
from sqlalchemy import String, DateTime, Boolean, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.models.base import Base

if TYPE_CHECKING:
    from app.models.patient import Patient
    from app.models.device_refresh_token import DeviceRefreshToken


class Device(Base):
    """
    Registered client device for telemetry ingestion.
    Maps a physical sensor/companion app to an authorized patient.
    Note: device_id is an identifier, not a cryptographic proof of identity.
    Authenticity is verified via scoped cryptographically hashed device tokens.
    """
    __tablename__ = "devices"

    device_id: Mapped[str] = mapped_column(
        String(64),
        primary_key=True,
        index=True,
    )
    patient_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("patients.patient_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    device_name: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
    )
    platform: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="android",
    )  # android, wearos, ios, test
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        index=True,
    )
    paired_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime,
        nullable=True,
    )
    last_seen_at: Mapped[datetime | None] = mapped_column(
        DateTime,
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    # Relationships
    patient: Mapped["Patient"] = relationship(
        "Patient",
        back_populates="devices",
    )
    refresh_tokens: Mapped[List["DeviceRefreshToken"]] = relationship(
        "DeviceRefreshToken",
        back_populates="device",
        cascade="all, delete-orphan",
    )
