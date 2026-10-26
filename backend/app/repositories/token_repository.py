from datetime import datetime, timezone, timedelta
import secrets
import time
import uuid
from typing import Optional, Tuple, List
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session
from sqlalchemy import select, update
from app.models.device import Device
from app.models.device_refresh_token import DeviceRefreshToken
from app.models.patient import Patient


class DeviceTokenRepository:
    """
    Repository for managing device refresh tokens with SHA-256 hashing,
    external transaction support (`commit=False`), atomic token rotation,
    device/patient active state enforcement, and automatic family reuse detection.

    CONCURRENCY & RACE CONDITION GUARANTEES:
    1. Refresh vs. Revoke Race:
       `rotate_refresh_token` executes a write-first conditional SQL UPDATE (`revoke_stmt`)
       that atomically verifies `DeviceRefreshToken.revoked_at IS NULL`, `expires_at > now`,
       AND `EXISTS (SELECT 1 FROM devices JOIN patients ... WHERE devices.is_active = 1
       AND devices.revoked_at IS NULL AND patients.status = 'active')` BEFORE inserting
       the replacement token.
       - If `revoke_device` wins the write lock first, `revoke_stmt` matches 0 rows and no
         replacement token is ever inserted.
       - If `rotate_refresh_token` wins the write lock first, `revoke_device` blocks on the
         SQLite write lock until rotation commits, and then immediately revokes the newly
         minted replacement token as well.
       In all possible thread interleavings, a revoked device or suspended patient ends with
       ZERO active refresh tokens.
    2. Concurrent Refresh Requests & Reuse Detection:
       Because `revoke_stmt` atomically transitions `revoked_at` from `NULL` to `now`,
       at most one concurrent refresh request with the same raw token can achieve `rowcount == 1`.
       Any second request presenting the same (now-revoked) token triggers `revoke_family()`,
       immediately invalidating the entire token lineage (`family_id`).
       NOTE FOR FLUTTER CLIENT (Stage 5.3C):
       To prevent false-positive family revocation caused by concurrent client HTTP calls,
       `DeviceAuthService` on the mobile client MUST serialize token refreshes through a
       single-flight async mutex (`Completer<String?> _refreshInFlight`) so parallel 401
       responses await a single shared `/api/v1/auth/refresh` request.
    """

    def __init__(self, db: Session):
        self.db = db

    def issue_refresh_token(
        self,
        device_id: str,
        ttl_days: int = 30,
        family_id: Optional[str] = None,
        raw_token_override: Optional[str] = None,
        *,
        commit: bool = True,
    ) -> Tuple[str, DeviceRefreshToken]:
        """
        Issues a new cryptographically random refresh token for an active device of an active patient.
        Stores only the SHA-256 hash in the database.
        When `commit=False`, flushes to the caller's outer transaction without committing.
        Returns (raw_token, DeviceRefreshToken_entity).
        """
        if not isinstance(device_id, str) or not device_id.strip():
            raise ValueError("device_id must not be empty.")
        if ttl_days <= 0:
            raise ValueError("ttl_days must be positive.")

        clean_device_id = device_id.strip()
        row = self.db.execute(
            select(Device, Patient)
            .join(Patient, Patient.patient_id == Device.patient_id)
            .where(Device.device_id == clean_device_id)
            .execution_options(populate_existing=True)
        ).first()
        if row is None:
            raise ValueError(
                f"Cannot issue refresh token: device '{clean_device_id}' is not found."
            )
        device, patient = row
        if not device.is_active or device.revoked_at is not None:
            raise ValueError(
                f"Cannot issue refresh token: device '{clean_device_id}' is revoked or inactive."
            )
        if patient.status != "active":
            raise ValueError(
                f"Cannot issue refresh token: patient '{patient.patient_id}' is not active (status='{patient.status}')."
            )

        raw_token = raw_token_override if raw_token_override is not None else secrets.token_urlsafe(32)
        token_hash = DeviceRefreshToken.hash_token(raw_token)

        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(days=ttl_days)

        token_entity = DeviceRefreshToken(
            device_id=clean_device_id,
            token_hash=token_hash,
            family_id=(family_id.strip() if family_id and family_id.strip() else secrets.token_hex(16)),
            created_at=now,
            expires_at=expires_at,
        )
        try:
            self.db.add(token_entity)
            if commit:
                self.db.commit()
                self.db.refresh(token_entity)
            else:
                self.db.flush()
            return raw_token.strip(), token_entity
        except Exception:
            if commit:
                self.db.rollback()
            raise

    def rotate_refresh_token(
        self,
        device_id: str,
        old_raw_token: str,
        ttl_days: int = 30,
        new_raw_token_override: Optional[str] = None,
        *,
        commit: bool = True,
    ) -> Tuple[bool, str, Optional[Tuple[str, DeviceRefreshToken]]]:
        """
        Rotates an existing refresh token with write-first atomic state transition and reuse detection:
        - Atomically verifies device is active/unrevoked AND patient is active at the exact write instant.
        - If old token is valid and unrevoked: atomically revokes old token and inserts new token in same family.
        - If old token was ALREADY revoked while device and patient are active (reuse detected!):
          revokes the ENTIRE token family immediately to protect against compromised credentials.
        Returns (success: bool, message: str, new_token_pair: Optional[Tuple[str, DeviceRefreshToken]]).
        """
        if not isinstance(device_id, str) or not device_id.strip():
            raise ValueError("device_id must not be empty.")
        if ttl_days <= 0:
            raise ValueError("ttl_days must be positive.")

        clean_device_id = device_id.strip()
        old_hash = DeviceRefreshToken.hash_token(old_raw_token)

        new_raw = new_raw_token_override if new_raw_token_override is not None else secrets.token_urlsafe(32)
        new_hash = DeviceRefreshToken.hash_token(new_raw)
        new_token_id = str(uuid.uuid4())
        max_lock_retries = 5

        for attempt in range(max_lock_retries + 1):
            now = datetime.now(timezone.utc)
            new_expires_at = now + timedelta(days=ttl_days)

            active_owner_exists = (
                select(Device.device_id)
                .join(Patient, Patient.patient_id == Device.patient_id)
                .where(
                    Device.device_id == clean_device_id,
                    Device.is_active.is_(True),
                    Device.revoked_at.is_(None),
                    Patient.status == "active",
                )
                .exists()
            )

            # Write-first conditional UPDATE acquires RESERVED write lock in SQLite immediately,
            # avoiding SHARED->RESERVED upgrade deadlocks and serializing against revoke_device.
            revoke_stmt = (
                update(DeviceRefreshToken)
                .where(
                    DeviceRefreshToken.device_id == clean_device_id,
                    DeviceRefreshToken.token_hash == old_hash,
                    DeviceRefreshToken.revoked_at.is_(None),
                    DeviceRefreshToken.expires_at > now,
                    active_owner_exists,
                )
                .values(
                    revoked_at=now,
                    replaced_by_token_id=new_token_id,
                )
                .execution_options(synchronize_session=False)
            )

            try:
                res = self.db.execute(revoke_stmt)
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

            if res.rowcount == 1:
                try:
                    rotated_old = self.db.execute(
                        select(DeviceRefreshToken)
                        .where(
                            DeviceRefreshToken.device_id == clean_device_id,
                            DeviceRefreshToken.token_hash == old_hash,
                        )
                        .execution_options(populate_existing=True)
                    ).scalar_one()

                    new_token = DeviceRefreshToken(
                        token_id=new_token_id,
                        device_id=clean_device_id,
                        token_hash=new_hash,
                        family_id=rotated_old.family_id,
                        created_at=now,
                        expires_at=new_expires_at,
                    )
                    self.db.add(new_token)

                    if commit:
                        self.db.commit()
                        self.db.expire_all()
                        self.db.refresh(new_token)
                    else:
                        self.db.flush()
                    return True, "Token rotated successfully", (new_raw.strip(), new_token)
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

            # res.rowcount == 0: Release write transaction (if commit=True) before inspecting reason
            if commit:
                self.db.rollback()
                self.db.expire_all()

            device = self.db.execute(
                select(Device)
                .where(Device.device_id == clean_device_id)
                .execution_options(populate_existing=True)
            ).scalar_one_or_none()
            if device is None or not device.is_active or device.revoked_at is not None:
                return False, "Device is revoked or inactive", None

            patient = self.db.execute(
                select(Patient)
                .where(Patient.patient_id == device.patient_id)
                .execution_options(populate_existing=True)
            ).scalar_one_or_none()
            if patient is None or patient.status != "active":
                return False, "Patient is not active", None

            token = self.db.execute(
                select(DeviceRefreshToken)
                .where(
                    DeviceRefreshToken.device_id == clean_device_id,
                    DeviceRefreshToken.token_hash == old_hash,
                )
                .execution_options(populate_existing=True)
            ).scalar_one_or_none()

            if not token:
                return False, "Refresh token not found", None

            if token.is_revoked:
                self.revoke_family(token.family_id, commit=commit)
                return False, "Revoked refresh token reuse detected! All lineage tokens revoked.", None

            if token.is_expired:
                return False, "Refresh token has expired", None

            return False, "Device or patient is no longer active", None

        return False, "Could not rotate refresh token due to concurrent lock contention", None

    def revoke_token(self, token_id: str, *, commit: bool = True) -> Optional[DeviceRefreshToken]:
        token = self.db.execute(
            select(DeviceRefreshToken).where(DeviceRefreshToken.token_id == token_id)
        ).scalar_one_or_none()
        if not token:
            return None
        try:
            token.revoked_at = datetime.now(timezone.utc)
            if commit:
                self.db.commit()
                self.db.refresh(token)
            else:
                self.db.flush()
            return token
        except Exception:
            if commit:
                self.db.rollback()
            raise

    def revoke_family(self, family_id: str, *, commit: bool = True) -> int:
        """Revokes all tokens belonging to the specified family lineage."""
        max_lock_retries = 5
        for attempt in range(max_lock_retries + 1):
            now = datetime.now(timezone.utc)
            stmt = (
                update(DeviceRefreshToken)
                .where(
                    DeviceRefreshToken.family_id == family_id,
                    DeviceRefreshToken.revoked_at.is_(None),
                )
                .values(revoked_at=now)
                .execution_options(synchronize_session=False)
            )
            try:
                res = self.db.execute(stmt)
                if commit:
                    self.db.commit()
                    self.db.expire_all()
                else:
                    self.db.flush()
                return res.rowcount
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
        return 0

    def revoke_all_for_device(self, device_id: str, *, commit: bool = True) -> int:
        """
        Revokes all active refresh tokens for a specific device.
        When `commit=False`, flushes within the caller's transaction without committing.
        """
        if not isinstance(device_id, str) or not device_id.strip():
            raise ValueError("device_id must not be empty.")
        clean_id = device_id.strip()
        max_lock_retries = 5

        for attempt in range(max_lock_retries + 1):
            now = datetime.now(timezone.utc)
            stmt = (
                update(DeviceRefreshToken)
                .where(
                    DeviceRefreshToken.device_id == clean_id,
                    DeviceRefreshToken.revoked_at.is_(None),
                )
                .values(revoked_at=now)
                .execution_options(synchronize_session=False)
            )
            try:
                res = self.db.execute(stmt)
                if commit:
                    self.db.commit()
                    self.db.expire_all()
                else:
                    self.db.flush()
                return res.rowcount
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
        return 0

    def list_by_device(self, device_id: str) -> List[DeviceRefreshToken]:
        return list(self.db.execute(
            select(DeviceRefreshToken)
            .where(DeviceRefreshToken.device_id == device_id)
            .order_by(DeviceRefreshToken.created_at.desc())
        ).scalars().all())
