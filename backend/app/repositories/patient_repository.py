from datetime import datetime, timezone
import uuid
from typing import List, Optional
from sqlalchemy.orm import Session
from sqlalchemy import select
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
    ) -> Patient:
        clean_id = patient_id or str(uuid.uuid4())
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
        self.db.add(patient)
        self.db.commit()
        self.db.refresh(patient)
        return patient

    def list_all(self, limit: int = 100, offset: int = 0) -> List[Patient]:
        return list(self.db.execute(
            select(Patient).order_by(Patient.created_at.desc()).offset(offset).limit(limit)
        ).scalars().all())

    def update_status(self, patient_id: str, status: str) -> Optional[Patient]:
        patient = self.get(patient_id)
        if not patient:
            return None
        patient.status = status
        patient.updated_at = datetime.now(timezone.utc)
        self.db.commit()
        self.db.refresh(patient)
        return patient
