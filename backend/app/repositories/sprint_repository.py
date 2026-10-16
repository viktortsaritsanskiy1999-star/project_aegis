from datetime import datetime, timezone
from typing import List, Optional
from sqlalchemy.orm import Session
from sqlalchemy import select
from app.models.weekly_sprint import WeeklySprint


class WeeklySprintRepository:
    def __init__(self, db: Session):
        self.db = db

    def get(self, sprint_id: str) -> Optional[WeeklySprint]:
        return self.db.execute(
            select(WeeklySprint).where(WeeklySprint.sprint_id == sprint_id)
        ).scalar_one_or_none()

    def get_active_for_cycle(self, cycle_id: str) -> Optional[WeeklySprint]:
        return self.db.execute(
            select(WeeklySprint)
            .where(WeeklySprint.cycle_id == cycle_id, WeeklySprint.status == "active")
            .order_by(WeeklySprint.sprint_number.desc())
        ).scalars().first()

    def create(
        self,
        sprint_id: str,
        cycle_id: str,
        sprint_number: int = 1,
        status: str = "active",
        started_at: Optional[datetime] = None,
    ) -> WeeklySprint:
        existing = self.get(sprint_id)
        if existing:
            return existing

        now = started_at or datetime.now(timezone.utc)
        sprint = WeeklySprint(
            sprint_id=sprint_id,
            cycle_id=cycle_id,
            sprint_number=sprint_number,
            status=status,
            started_at=now,
            created_at=now,
        )
        self.db.add(sprint)
        self.db.commit()
        self.db.refresh(sprint)
        return sprint

    def complete_sprint(
        self,
        sprint_id: str,
        completed_at: Optional[datetime] = None,
    ) -> Optional[WeeklySprint]:
        sprint = self.get(sprint_id)
        if not sprint:
            return None

        sprint.status = "completed"
        sprint.completed_at = completed_at or datetime.now(timezone.utc)
        self.db.commit()
        self.db.refresh(sprint)
        return sprint

    def list_by_cycle(self, cycle_id: str) -> List[WeeklySprint]:
        return list(self.db.execute(
            select(WeeklySprint)
            .where(WeeklySprint.cycle_id == cycle_id)
            .order_by(WeeklySprint.sprint_number.asc())
        ).scalars().all())
