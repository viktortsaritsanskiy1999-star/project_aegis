from datetime import datetime, timezone
import uuid
from typing import List, TYPE_CHECKING
from sqlalchemy import String, DateTime
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.models.base import Base

if TYPE_CHECKING:
    from app.models.medical_cycle import MedicalCycle
    from app.models.device import Device
    from app.models.pairing_code import PairingCode


class Patient(Base):
    """
    Minimal clinical patient identity entity in Project Aegis.
    Represents an isolated subject of clinical observation.
    Strictly avoids storing PII (Full name, passport, phone) on the telemetry backend.
    """
    __tablename__ = "patients"

    patient_id: Mapped[str] = mapped_column(
        String(64),
        primary_key=True,
        index=True,
        default=lambda: str(uuid.uuid4()),
    )
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="active",
        index=True,
    )  # active, suspended, archived
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
    cycles: Mapped[List["MedicalCycle"]] = relationship(
        "MedicalCycle",
        back_populates="patient",
    )
    devices: Mapped[List["Device"]] = relationship(
        "Device",
        back_populates="patient",
        cascade="all, delete-orphan",
    )
    pairing_codes: Mapped[List["PairingCode"]] = relationship(
        "PairingCode",
        back_populates="patient",
        cascade="all, delete-orphan",
    )
