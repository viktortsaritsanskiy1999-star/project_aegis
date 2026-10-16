from datetime import datetime, timezone
from typing import List, TYPE_CHECKING
from sqlalchemy import String, Integer, DateTime, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.models.base import Base

if TYPE_CHECKING:
    from app.models.medical_cycle import MedicalCycle
    from app.models.workout_session import WorkoutSession
    from app.models.model_report import ModelReport


class WeeklySprint(Base):
    """
    7-day Athena sprint running inside an active MedicalCycle.
    Finishing day 7 increments sprint_number and initiates weekly cascade analysis,
    while the enclosing MedicalCycle remains active.
    """
    __tablename__ = "weekly_sprints"

    sprint_id: Mapped[str] = mapped_column(String(64), primary_key=True, index=True)
    cycle_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("medical_cycles.cycle_id", ondelete="CASCADE"),
        index=True,
    )
    sprint_number: Mapped[int] = mapped_column(Integer, default=1)  # 1, 2, 3...
    status: Mapped[str] = mapped_column(String(32), default="active", index=True)  # active, completed
    started_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))

    # Relationships
    cycle: Mapped["MedicalCycle"] = relationship("MedicalCycle", back_populates="sprints")
    workouts: Mapped[List["WorkoutSession"]] = relationship("WorkoutSession", back_populates="sprint")
    reports: Mapped[List["ModelReport"]] = relationship("ModelReport", back_populates="sprint")
