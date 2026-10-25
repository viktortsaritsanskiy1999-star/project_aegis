from datetime import datetime, timezone
from typing import List, Optional, TYPE_CHECKING
from sqlalchemy import String, DateTime, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.models.base import Base

if TYPE_CHECKING:
    from app.models.patient import Patient
    from app.models.weekly_sprint import WeeklySprint
    from app.models.workout_session import WorkoutSession
    from app.models.health_measurement import HealthMeasurement
    from app.models.model_report import ModelReport


class MedicalCycle(Base):
    """
    Macro-cycle of clinical observation in Project Aegis.
    Contains multiple WeeklySprints. Completing day 7 of a sprint
    does NOT close the MedicalCycle. Closing the cycle requires an explicit
    cycle closure action.
    """
    __tablename__ = "medical_cycles"

    cycle_id: Mapped[str] = mapped_column(String(64), primary_key=True, index=True)
    patient_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("patients.patient_id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    status: Mapped[str] = mapped_column(String(32), default="active", index=True)  # active, completed, archived
    started_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    # Relationships
    patient: Mapped[Optional["Patient"]] = relationship(
        "Patient",
        back_populates="cycles",
    )
    sprints: Mapped[List["WeeklySprint"]] = relationship(
        "WeeklySprint",
        back_populates="cycle",
        cascade="all, delete-orphan",
    )
    workouts: Mapped[List["WorkoutSession"]] = relationship(
        "WorkoutSession",
        back_populates="cycle",
        cascade="all, delete-orphan",
    )
    measurements: Mapped[List["HealthMeasurement"]] = relationship(
        "HealthMeasurement",
        back_populates="cycle",
    )
    reports: Mapped[List["ModelReport"]] = relationship(
        "ModelReport",
        back_populates="cycle",
        cascade="all, delete-orphan",
    )
