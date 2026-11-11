from datetime import datetime, timezone
from typing import Optional, TYPE_CHECKING
import uuid

from sqlalchemy import Boolean, DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base

if TYPE_CHECKING:
    from app.models.patient import Patient


class PatientGoogleIdentity(Base):
    """
    Mapping between an externally verified Google Identity (iss + sub) and an internal Aegis patient_id.
    
    Security & Privacy Invariants:
    - google_sub is the ONLY immutable identifier used to anchor identity.
    - google_sub has a UNIQUE constraint: one Google account maps to exactly ONE Aegis patient.
    - patient_id has a UNIQUE constraint: one Aegis patient has at most ONE Google account.
    - email is stored solely for administrative audit and beta invitations, NEVER as an identity anchor.
    - Accounts are NEVER merged based on matching email addresses.
    """
    __tablename__ = "patient_google_identities"

    id: Mapped[str] = mapped_column(
        String(64),
        primary_key=True,
        index=True,
        default=lambda: str(uuid.uuid4()),
    )
    google_sub: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        unique=True,
        index=True,
    )
    google_issuer: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        default="https://accounts.google.com",
    )
    patient_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("patients.patient_id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    email: Mapped[Optional[str]] = mapped_column(
        String(255),
        nullable=True,
        index=True,
    )
    email_verified: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    last_authenticated_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    # Relationship to Patient
    patient: Mapped["Patient"] = relationship(
        "Patient",
        back_populates="google_identity",
    )
