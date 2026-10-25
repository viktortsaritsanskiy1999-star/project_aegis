from datetime import datetime, timezone
import time
from typing import List, Optional
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session
from sqlalchemy import select, update
from app.models.medical_cycle import MedicalCycle


class CycleOwnershipConflictError(ValueError):
    """
    Raised when attempting to assign or reassign a MedicalCycle that is already
    owned by a different patient. Subclasses ValueError.
    """


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

    def get_active_for_patient(self, patient_id: str) -> Optional[MedicalCycle]:
        """Resolves the current active clinical cycle owned by the specified patient."""
        return self.db.execute(
            select(MedicalCycle)
            .where(MedicalCycle.patient_id == patient_id, MedicalCycle.status == "active")
            .order_by(MedicalCycle.started_at.desc())
        ).scalars().first()

    def list_by_patient(self, patient_id: str, limit: int = 100, offset: int = 0) -> List[MedicalCycle]:
        """Lists all medical cycles belonging to the specified patient."""
        return list(self.db.execute(
            select(MedicalCycle)
            .where(MedicalCycle.patient_id == patient_id)
            .order_by(MedicalCycle.started_at.desc())
            .offset(offset)
            .limit(limit)
        ).scalars().all())

    def create(
        self,
        cycle_id: str,
        status: str = "active",
        started_at: Optional[datetime] = None,
        patient_id: Optional[str] = None,
    ) -> MedicalCycle:
        existing = self.get(cycle_id)
        if existing:
            if (
                patient_id is not None
                and existing.patient_id is not None
                and existing.patient_id != patient_id
            ):
                raise CycleOwnershipConflictError(
                    f"Cycle ownership conflict: medical cycle '{cycle_id}' already belongs "
                    f"to a different patient and cannot be claimed by '{patient_id}'."
                )
            return existing

        now = started_at or datetime.now(timezone.utc)
        cycle = MedicalCycle(
            cycle_id=cycle_id,
            patient_id=patient_id,
            status=status,
            started_at=now,
            created_at=now,
        )
        try:
            self.db.add(cycle)
            self.db.commit()
            self.db.refresh(cycle)
            return cycle
        except Exception:
            self.db.rollback()
            raise

    def assign_patient(self, cycle_id: str, patient_id: str) -> Optional[MedicalCycle]:
        """
        Controlled atomic linking procedure for assigning a previously unassigned (legacy)
        medical cycle to a verified patient without altering historical measurements or reports.
        - Permitted ONLY when cycle.patient_id IS NULL.
        - Re-assigning to the same patient_id is safe and idempotent.
        - Attempting to reassign a cycle owned by Patient A to Patient B raises CycleOwnershipConflictError.
        - Protected against concurrent cross-patient assignment races via conditional SQL UPDATE.
        """
        if not isinstance(cycle_id, str) or not cycle_id.strip():
            raise ValueError("cycle_id must not be empty.")
        if not isinstance(patient_id, str) or not patient_id.strip():
            raise ValueError("patient_id must not be empty.")

        clean_cycle_id = cycle_id.strip()
        clean_patient_id = patient_id.strip()
        max_lock_retries = 5

        for attempt in range(max_lock_retries + 1):
            now = datetime.now(timezone.utc)
            stmt = (
                update(MedicalCycle)
                .where(
                    MedicalCycle.cycle_id == clean_cycle_id,
                    MedicalCycle.patient_id.is_(None),
                )
                .values(
                    patient_id=clean_patient_id,
                    updated_at=now,
                )
                .execution_options(synchronize_session=False)
            )

            try:
                res = self.db.execute(stmt)
                if res.rowcount == 1:
                    self.db.commit()
                    self.db.expire_all()
                    return self.get(clean_cycle_id)

                self.db.rollback()
                self.db.expire_all()
                existing = self.get(clean_cycle_id)
                if existing is None:
                    return None
                if existing.patient_id == clean_patient_id:
                    return existing
                if existing.patient_id is not None and existing.patient_id != clean_patient_id:
                    raise CycleOwnershipConflictError(
                        f"Cycle ownership conflict: medical cycle '{clean_cycle_id}' is already "
                        f"assigned to patient '{existing.patient_id}' and cannot be reassigned "
                        f"to patient '{clean_patient_id}'."
                    )
            except CycleOwnershipConflictError:
                self.db.rollback()
                raise
            except OperationalError as exc:
                self.db.rollback()
                err_msg = str(exc).lower()
                if attempt < max_lock_retries and ("locked" in err_msg or "busy" in err_msg):
                    time.sleep(0.015 * (attempt + 1))
                    continue
                raise
            except Exception:
                self.db.rollback()
                raise

        return self.get(clean_cycle_id)

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
