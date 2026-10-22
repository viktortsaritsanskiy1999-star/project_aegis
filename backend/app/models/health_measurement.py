from datetime import datetime, timezone
from typing import TYPE_CHECKING
from sqlalchemy import String, Integer, Boolean, DateTime, ForeignKey, JSON
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.models.base import Base

if TYPE_CHECKING:
    from app.models.medical_cycle import MedicalCycle
    from app.models.workout_session import WorkoutSession


class HealthMeasurement(Base):
    """
    Biometric measurement point with explicit source and timestamp separation.
    - measured_at: original physical sensor measurement timestamp
    - ingested_at: server receipt timestamp
    - source: watch_sensor, manual_input, test_chip, simulated, unknown
    """
    __tablename__ = "health_measurements"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("workout_sessions.session_id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    cycle_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("medical_cycles.cycle_id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    pulse: Mapped[int | None] = mapped_column(Integer, nullable=True)
    systolic: Mapped[int | None] = mapped_column(Integer, nullable=True)
    diastolic: Mapped[int | None] = mapped_column(Integer, nullable=True)
    spo2: Mapped[int | None] = mapped_column(Integer, nullable=True)

    source: Mapped[str] = mapped_column(String(32), index=True)  # watch_sensor, manual_input, test_chip, simulated, unknown
    is_simulated: Mapped[bool] = mapped_column(Boolean, default=False)
    is_historical: Mapped[bool] = mapped_column(Boolean, default=False)

    measured_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    ingested_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))

    phase: Mapped[str] = mapped_column(String(64), default="standby")  # workout, recovery_0_10, recovery_10_50, standard, historical, standby
    note: Mapped[str] = mapped_column(String(255), default="")
    raw_metadata: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    # Relationships
    session: Mapped["WorkoutSession | None"] = relationship("WorkoutSession", back_populates="measurements")
    cycle: Mapped["MedicalCycle | None"] = relationship("MedicalCycle", back_populates="measurements")

    @property
    def is_medically_admissible(self) -> bool:
        """
        True only if the measurement is genuine, live biometric data admissible for clinical evaluation.
        Synthetic test data (test_chip, simulated, is_simulated=True) and historical retrospective imports
        (is_historical=True) are strictly inadmissible for real-time consilium inference.
        """
        if self.is_simulated or self.source in ("test_chip", "simulated"):
            return False
        if self.is_historical:
            return False
        return True
