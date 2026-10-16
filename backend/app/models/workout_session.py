from datetime import datetime, timezone
from typing import List, TYPE_CHECKING
from sqlalchemy import String, Boolean, DateTime, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.models.base import Base

if TYPE_CHECKING:
    from app.models.medical_cycle import MedicalCycle
    from app.models.weekly_sprint import WeeklySprint
    from app.models.health_measurement import HealthMeasurement
    from app.models.model_report import ModelReport


class WorkoutSession(Base):
    """
    Workout session recorded during an active MedicalCycle and WeeklySprint.
    Separates user button completion time from physiological recovery time.
    """
    __tablename__ = "workout_sessions"

    session_id: Mapped[str] = mapped_column(String(64), primary_key=True, index=True)
    cycle_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("medical_cycles.cycle_id", ondelete="CASCADE"),
        index=True,
    )
    sprint_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("weekly_sprints.sprint_id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    anomaly_detected_at: Mapped[datetime] = mapped_column(DateTime)
    activity_confirmed_at: Mapped[datetime] = mapped_column(DateTime)
    workout_completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    physiological_end_time: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    status: Mapped[str] = mapped_column(String(32), default="active", index=True)  # active, recovering, completed
    is_data_collection_finished: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))

    # Relationships
    cycle: Mapped["MedicalCycle"] = relationship("MedicalCycle", back_populates="workouts")
    sprint: Mapped["WeeklySprint | None"] = relationship("WeeklySprint", back_populates="workouts")
    measurements: Mapped[List["HealthMeasurement"]] = relationship(
        "HealthMeasurement",
        back_populates="session",
    )
    reports: Mapped[List["ModelReport"]] = relationship(
        "ModelReport",
        back_populates="session",
    )
