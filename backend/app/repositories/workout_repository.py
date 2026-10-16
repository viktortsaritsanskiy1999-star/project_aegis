from datetime import datetime, timezone
from typing import List, Optional
from sqlalchemy.orm import Session
from sqlalchemy import select
from app.models.workout_session import WorkoutSession


class WorkoutSessionRepository:
    def __init__(self, db: Session):
        self.db = db

    def get(self, session_id: str) -> Optional[WorkoutSession]:
        return self.db.execute(
            select(WorkoutSession).where(WorkoutSession.session_id == session_id)
        ).scalar_one_or_none()

    def create(
        self,
        session_id: str,
        cycle_id: str,
        anomaly_detected_at: datetime,
        activity_confirmed_at: datetime,
        sprint_id: Optional[str] = None,
        status: str = "active",
    ) -> WorkoutSession:
        existing = self.get(session_id)
        if existing:
            return existing

        session = WorkoutSession(
            session_id=session_id,
            cycle_id=cycle_id,
            sprint_id=sprint_id,
            anomaly_detected_at=anomaly_detected_at,
            activity_confirmed_at=activity_confirmed_at,
            status=status,
            created_at=datetime.now(timezone.utc),
        )
        self.db.add(session)
        self.db.commit()
        self.db.refresh(session)
        return session

    def complete_workout(
        self,
        session_id: str,
        workout_completed_at: Optional[datetime] = None,
        physiological_end_time: Optional[datetime] = None,
    ) -> Optional[WorkoutSession]:
        session = self.get(session_id)
        if not session:
            return None

        session.workout_completed_at = workout_completed_at or datetime.now(timezone.utc)
        if physiological_end_time:
            session.physiological_end_time = physiological_end_time
        session.status = "recovering"
        self.db.commit()
        self.db.refresh(session)
        return session

    def finish_data_collection(
        self,
        session_id: str,
    ) -> Optional[WorkoutSession]:
        session = self.get(session_id)
        if not session:
            return None

        session.is_data_collection_finished = True
        session.status = "completed"
        self.db.commit()
        self.db.refresh(session)
        return session

    def list_by_cycle(self, cycle_id: str) -> List[WorkoutSession]:
        return list(self.db.execute(
            select(WorkoutSession)
            .where(WorkoutSession.cycle_id == cycle_id)
            .order_by(WorkoutSession.created_at.desc())
        ).scalars().all())

    def list_by_sprint(self, sprint_id: str) -> List[WorkoutSession]:
        return list(self.db.execute(
            select(WorkoutSession)
            .where(WorkoutSession.sprint_id == sprint_id)
            .order_by(WorkoutSession.created_at.desc())
        ).scalars().all())
