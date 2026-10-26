from datetime import datetime, timezone
import time
import uuid
from typing import List, Optional
from sqlalchemy.exc import OperationalError
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

    def revoke_device(self, device_id: str, *, commit: bool = True) -> Optional[Device]:
        """
        Atomically revokes a device and all of its active refresh tokens.
        Uses a write-first SQL UPDATE to acquire an immediate write lock in SQLite,
        eliminating SHARED->RESERVED lock upgrade deadlocks and race windows against
        concurrent refresh token rotations.
        """
        if not isinstance(device_id, str) or not device_id.strip():
            return None
        clean_id = device_id.strip()
        max_lock_retries = 5

        for attempt in range(max_lock_retries + 1):
            now = datetime.now(timezone.utc)
            try:
                dev_res = self.db.execute(
                    update(Device)
                    .where(Device.device_id == clean_id)
                    .values(
                        is_active=False,
                        revoked_at=now,
                        updated_at=now,
                    )
                    .execution_options(synchronize_session=False)
                )
                if dev_res.rowcount == 0:
                    if commit:
                        self.db.rollback()
                    return None

                self.db.execute(
                    update(DeviceRefreshToken)
                    .where(
                        DeviceRefreshToken.device_id == clean_id,
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

    def update_last_seen(self, device_id: str, *, commit: bool = True) -> Optional[Device]:
        device = self.get(device_id)
        if not device:
            return None
        now = datetime.now(timezone.utc)
        try:
            device.last_seen_at = now
            device.updated_at = now
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
