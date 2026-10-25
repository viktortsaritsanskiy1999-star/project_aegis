from datetime import datetime, timezone
import uuid
from typing import List, Optional
from sqlalchemy.orm import Session
from sqlalchemy import select, update
from app.models.device import Device
from app.models.device_refresh_token import DeviceRefreshToken


class DeviceRepository:
    def __init__(self, db: Session):
        self.db = db

    def get(self, device_id: str) -> Optional[Device]:
        return self.db.execute(
            select(Device).where(Device.device_id == device_id)
        ).scalar_one_or_none()

    def register_device(
        self,
        patient_id: str,
        device_name: str,
        platform: str = "android",
        device_id: Optional[str] = None,
        commit: bool = True,
    ) -> Device:
        if not isinstance(patient_id, str) or not patient_id.strip():
            raise ValueError("patient_id must not be empty.")
        if not isinstance(device_name, str) or not device_name.strip():
            raise ValueError("device_name must not be empty.")

        clean_patient_id = patient_id.strip()
        clean_id = (device_id.strip() if device_id and device_id.strip() else str(uuid.uuid4()))

        existing = self.get(clean_id)
        if existing:
            if existing.patient_id != clean_patient_id:
                raise ValueError(
                    f"Device '{clean_id}' is already registered to a different patient "
                    "and cannot be reassigned."
                )
            try:
                existing.device_name = device_name.strip()
                existing.platform = platform
                existing.is_active = True
                existing.revoked_at = None
                existing.updated_at = datetime.now(timezone.utc)
                if commit:
                    self.db.commit()
                    self.db.refresh(existing)
                else:
                    self.db.flush()
                return existing
            except Exception:
                if commit:
                    self.db.rollback()
                raise

        now = datetime.now(timezone.utc)
        device = Device(
            device_id=clean_id,
            patient_id=clean_patient_id,
            device_name=device_name.strip(),
            platform=platform,
            is_active=True,
            paired_at=now,
            created_at=now,
            updated_at=now,
        )
        try:
            self.db.add(device)
            if commit:
                self.db.commit()
                self.db.refresh(device)
            else:
                self.db.flush()
            return device
        except Exception:
            if commit:
                self.db.rollback()
            raise

    def list_by_patient(self, patient_id: str) -> List[Device]:
        return list(self.db.execute(
            select(Device)
            .where(Device.patient_id == patient_id)
            .order_by(Device.paired_at.desc())
        ).scalars().all())

    def revoke_device(self, device_id: str) -> Optional[Device]:
        device = self.get(device_id)
        if not device:
            return None
        now = datetime.now(timezone.utc)
        try:
            device.is_active = False
            device.revoked_at = now
            device.updated_at = now
            self.db.execute(
                update(DeviceRefreshToken)
                .where(
                    DeviceRefreshToken.device_id == device_id,
                    DeviceRefreshToken.revoked_at.is_(None),
                )
                .values(revoked_at=now)
                .execution_options(synchronize_session=False)
            )
            self.db.commit()
            self.db.refresh(device)
            return device
        except Exception:
            self.db.rollback()
            raise

    def update_last_seen(self, device_id: str) -> Optional[Device]:
        device = self.get(device_id)
        if not device:
            return None
        now = datetime.now(timezone.utc)
        try:
            device.last_seen_at = now
            device.updated_at = now
            self.db.commit()
            self.db.refresh(device)
            return device
        except Exception:
            self.db.rollback()
            raise
