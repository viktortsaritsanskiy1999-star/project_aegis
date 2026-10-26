from datetime import datetime, timezone
import time
import uuid
from typing import List, Optional
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session
from sqlalchemy import select, update
from app.models.device import Device
from app.models.device_refresh_token import DeviceRefreshToken
from app.models.patient import Patient


class PatientRepository:
    def __init__(self, db: Session):
        self.db = db

    def get(self, patient_id: str) -> Optional[Patient]:
        return self.db.execute(
            select(Patient).where(Patient.patient_id == patient_id)
        ).scalar_one_or_none()

    def create(
        self,
        patient_id: Optional[str] = None,
        status: str = "active",
        *,
        commit: bool = True,
    ) -> Patient:
        clean_id = patient_id.strip() if patient_id and patient_id.strip() else str(uuid.uuid4())
        existing = self.get(clean_id)
        if existing:
            return existing

        now = datetime.now(timezone.utc)
        patient = Patient(
            patient_id=clean_id,
            status=status,
            created_at=now,
            updated_at=now,
        )
        try:
            self.db.add(patient)
            if commit:
                self.db.commit()
                self.db.refresh(patient)
            else:
                self.db.flush()
            return patient
        except Exception:
            if commit:
                self.db.rollback()
            raise

    def list_all(self, limit: int = 100, offset: int = 0) -> List[Patient]:
        return list(self.db.execute(
            select(Patient).order_by(Patient.created_at.desc()).offset(offset).limit(limit)
        ).scalars().all())

    def update_status(
        self,
        patient_id: str,
        status: str,
        *,
        commit: bool = True,
    ) -> Optional[Patient]:
        """
        Atomically updates patient status. When a patient is transitioned to a non-active
        state (e.g. 'suspended' or 'archived'), all active refresh tokens across all
        devices belonging to this patient are revoked in the same transaction.
        """
        if not isinstance(patient_id, str) or not patient_id.strip():
            return None
        clean_id = patient_id.strip()
        max_lock_retries = 5

        for attempt in range(max_lock_retries + 1):
            now = datetime.now(timezone.utc)
            try:
                res = self.db.execute(
                    update(Patient)
                    .where(Patient.patient_id == clean_id)
                    .values(status=status, updated_at=now)
                    .execution_options(synchronize_session=False)
                )
                if res.rowcount == 0:
                    if commit:
                        self.db.rollback()
                    return None

                if status != "active":
                    patient_devices_subq = (
                        select(Device.device_id).where(Device.patient_id == clean_id)
                    )
                    self.db.execute(
                        update(DeviceRefreshToken)
                        .where(
                            DeviceRefreshToken.device_id.in_(patient_devices_subq),
                            DeviceRefreshToken.revoked_at.is_(None),
                        )
                        .values(revoked_at=now)
                        .execution_options(synchronize_session=False)
                    )

                if commit:
                    self.db.commit()
                    self.db.expire_all()
                else:
                    self.db.flush()
                return self.get(clean_id)
            except OperationalError as exc:
                if commit:
                    self.db.rollback()
                err_msg = str(exc).lower()
                if commit and attempt < max_lock_retries and ("locked" in err_msg or "busy" in err_msg):
                    time.sleep(0.015 * (attempt + 1))
                    continue
                raise
            except Exception:
                if commit:
                    self.db.rollback()
                raise

        return self.get(clean_id)
