from datetime import datetime, timezone
from typing import TYPE_CHECKING
from sqlalchemy import String, Integer, DateTime, ForeignKey, JSON, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.models.base import Base

if TYPE_CHECKING:
    from app.models.medical_cycle import MedicalCycle
    from app.models.weekly_sprint import WeeklySprint
    from app.models.workout_session import WorkoutSession


class ModelReport(Base):
    """
    AI model analytical report with strict privacy-level partitioning:
    - server_only:
        * internal_analysis (Workout Intelligence clinical biomechanics)
        * system_training_shelf (8 organ models deficiency/pathophysiology logs)
        * pro_training_shelf (Chief Physician clinical synthesis & root cause analysis)
        * athena_handoff (Directives for Athena)
    - client_facing:
        * system_general_shelf (Human Atlas organ summary for user)
        * pro_general_shelf (Chief Physician user-facing wellness roadmap)
        * user_workout_report (User workout summary)
    """
    __tablename__ = "model_reports"
    __table_args__ = (
        UniqueConstraint(
            "cycle_id",
            "report_type",
            "privacy_level",
            "version",
            name="uq_model_reports_cycle_type_privacy_version",
        ),
    )

    report_id: Mapped[str] = mapped_column(String(64), primary_key=True, index=True)
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
    session_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("workout_sessions.session_id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    model: Mapped[str] = mapped_column(String(64))  # gemini-3.1-pro, gemini-3.6-flash, etc.
    report_type: Mapped[str] = mapped_column(String(64), index=True)  # system_shelf, master_cascade, workout_intelligence
    privacy_level: Mapped[str] = mapped_column(String(32), index=True)  # server_only vs client_facing
    version: Mapped[int] = mapped_column(Integer, default=1)

    content: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))

    # Relationships
    cycle: Mapped["MedicalCycle"] = relationship("MedicalCycle", back_populates="reports")
    sprint: Mapped["WeeklySprint | None"] = relationship("WeeklySprint", back_populates="reports")
    session: Mapped["WorkoutSession | None"] = relationship("WorkoutSession", back_populates="reports")
