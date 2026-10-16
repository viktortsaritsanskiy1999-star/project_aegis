from datetime import datetime, timezone
from typing import List, Optional
from sqlalchemy.orm import Session
from sqlalchemy import select
from app.models.medical_cycle import MedicalCycle


class MedicalCycleRepository:
    def __init__(self, db: Session):
        self.db = db

    def get(self, cycle_id: str) -> Optional[MedicalCycle]:
        return self.db.execute(
            select(MedicalCycle).where(MedicalCycle.cycle_id == cycle_id)
        ).scalar_one_or_none()

    def get_active(self) -> Optional[MedicalCycle]:
        return self.db.execute(
            select(MedicalCycle).where(MedicalCycle.status == "active").order_by(MedicalCycle.started_at.desc())
        ).scalars().first()

    def create(
        self,
        cycle_id: str,
        status: str = "active",
        started_at: Optional[datetime] = None,
    ) -> MedicalCycle:
        existing = self.get(cycle_id)
        if existing:
            return existing

        now = started_at or datetime.now(timezone.utc)
        cycle = MedicalCycle(
            cycle_id=cycle_id,
            status=status,
            started_at=now,
            created_at=now,
        )
        self.db.add(cycle)
        self.db.commit()
        self.db.refresh(cycle)
        return cycle

    def update_status(
        self,
        cycle_id: str,
        status: str,
        closed_at: Optional[datetime] = None,
    ) -> Optional[MedicalCycle]:
        cycle = self.get(cycle_id)
        if not cycle:
            return None

        cycle.status = status
        if status in ("completed", "archived"):
            cycle.closed_at = closed_at or datetime.now(timezone.utc)
        elif status == "active":
            cycle.closed_at = None

        self.db.commit()
        self.db.refresh(cycle)
        return cycle

    def close_cycle(self, cycle_id: str) -> Optional[MedicalCycle]:
        return self.update_status(cycle_id, status="completed")

    def archive_cycle(self, cycle_id: str) -> Optional[MedicalCycle]:
        # Soft-archive: preserves all related sprints, workouts, reports, and measurements
        return self.update_status(cycle_id, status="archived")

    def list_cycles(self, limit: int = 100, offset: int = 0) -> List[MedicalCycle]:
        return list(self.db.execute(
            select(MedicalCycle).order_by(MedicalCycle.started_at.desc()).offset(offset).limit(limit)
        ).scalars().all())
