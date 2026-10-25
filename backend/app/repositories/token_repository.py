from datetime import datetime, timezone, timedelta
import secrets
from typing import Optional, Tuple, List
from sqlalchemy.orm import Session
from sqlalchemy import select, update
from app.models.device import Device
from app.models.device_refresh_token import DeviceRefreshToken


class DeviceTokenRepository:
    def __init__(self, db: Session):
        self.db = db

    def issue_refresh_token(
        self,
        device_id: str,
        ttl_days: int = 30,
        family_id: Optional[str] = None,
        raw_token_override: Optional[str] = None,
    ) -> Tuple[str, DeviceRefreshToken]:
        """
        Issues a new cryptographically random refresh token for an active device.
        Stores only the SHA-256 hash in the database.
        Returns (raw_token, DeviceRefreshToken_entity).
        """
        if not isinstance(device_id, str) or not device_id.strip():
            raise ValueError("device_id must not be empty.")
        if ttl_days <= 0:
            raise ValueError("ttl_days must be positive.")

        clean_device_id = device_id.strip()
        device = self.db.execute(
            select(Device).where(Device.device_id == clean_device_id)
        ).scalar_one_or_none()
        if device is None or not device.is_active or device.revoked_at is not None:
            raise ValueError(
                f"Cannot issue refresh token: device '{clean_device_id}' is not found or is revoked."
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
            self.db.commit()
            self.db.refresh(token_entity)
            return raw_token.strip(), token_entity
        except Exception:
            self.db.rollback()
            raise

    def rotate_refresh_token(
        self,
        device_id: str,
        old_raw_token: str,
        ttl_days: int = 30,
        new_raw_token_override: Optional[str] = None,
    ) -> Tuple[bool, str, Optional[Tuple[str, DeviceRefreshToken]]]:
        """
        Rotates an existing refresh token with atomic state transition and reuse detection:
        - Verifies device is still active and unrevoked.
        - If old token is valid and unrevoked: atomically revokes old token and issues new token in same family.
        - If old token was ALREADY revoked (reuse detected!): revokes the ENTIRE token family
          immediately to protect against compromised/intercepted credentials.
        Returns (success: bool, message: str, new_token_pair: Optional[Tuple[str, DeviceRefreshToken]]).
        """
        if not isinstance(device_id, str) or not device_id.strip():
            raise ValueError("device_id must not be empty.")
        if ttl_days <= 0:
            raise ValueError("ttl_days must be positive.")

        clean_device_id = device_id.strip()
        old_hash = DeviceRefreshToken.hash_token(old_raw_token)
        now = datetime.now(timezone.utc)

        device = self.db.execute(
            select(Device).where(Device.device_id == clean_device_id)
        ).scalar_one_or_none()
        if device is None or not device.is_active or device.revoked_at is not None:
            return False, "Device is revoked or inactive", None

        token = self.db.execute(
            select(DeviceRefreshToken)
            .where(
                DeviceRefreshToken.device_id == clean_device_id,
                DeviceRefreshToken.token_hash == old_hash,
            )
        ).scalar_one_or_none()

        if not token:
            return False, "Refresh token not found", None

        # REUSE DETECTION: Token was already revoked!
        if token.is_revoked:
            self.revoke_family(token.family_id)
            return False, "Revoked refresh token reuse detected! All lineage tokens revoked.", None

        if token.is_expired:
            return False, "Refresh token has expired", None

        new_raw = new_raw_token_override if new_raw_token_override is not None else secrets.token_urlsafe(32)
        new_hash = DeviceRefreshToken.hash_token(new_raw)
        new_expires_at = now + timedelta(days=ttl_days)

        try:
            new_token = DeviceRefreshToken(
                device_id=clean_device_id,
                token_hash=new_hash,
                family_id=token.family_id,
                created_at=now,
                expires_at=new_expires_at,
            )
            self.db.add(new_token)
            self.db.flush()

            # Atomically mark old token as rotated and revoked ONLY if still unrevoked
            revoke_stmt = (
                update(DeviceRefreshToken)
                .where(
                    DeviceRefreshToken.token_id == token.token_id,
                    DeviceRefreshToken.revoked_at.is_(None),
                )
                .values(
                    revoked_at=now,
                    replaced_by_token_id=new_token.token_id,
                )
                .execution_options(synchronize_session=False)
            )
            res = self.db.execute(revoke_stmt)
            if res.rowcount != 1:
                self.db.rollback()
                self.revoke_family(token.family_id)
                return False, "Revoked refresh token reuse detected! All lineage tokens revoked.", None

            self.db.commit()
            self.db.expire_all()
            self.db.refresh(new_token)
            return True, "Token rotated successfully", (new_raw.strip(), new_token)
        except Exception:
            self.db.rollback()
            raise

    def revoke_token(self, token_id: str) -> Optional[DeviceRefreshToken]:
        token = self.db.execute(
            select(DeviceRefreshToken).where(DeviceRefreshToken.token_id == token_id)
        ).scalar_one_or_none()
        if not token:
            return None
        try:
            token.revoked_at = datetime.now(timezone.utc)
            self.db.commit()
            self.db.refresh(token)
            return token
        except Exception:
            self.db.rollback()
            raise

    def revoke_family(self, family_id: str) -> int:
        """Revokes all tokens belonging to the specified family lineage."""
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
            self.db.commit()
            self.db.expire_all()
            return res.rowcount
        except Exception:
            self.db.rollback()
            raise

    def revoke_all_for_device(self, device_id: str) -> int:
        """Revokes all active refresh tokens for a specific device."""
        now = datetime.now(timezone.utc)
        stmt = (
            update(DeviceRefreshToken)
            .where(
                DeviceRefreshToken.device_id == device_id,
                DeviceRefreshToken.revoked_at.is_(None),
            )
            .values(revoked_at=now)
            .execution_options(synchronize_session=False)
        )
        try:
            res = self.db.execute(stmt)
            self.db.commit()
            self.db.expire_all()
            return res.rowcount
        except Exception:
            self.db.rollback()
            raise

    def list_by_device(self, device_id: str) -> List[DeviceRefreshToken]:
        return list(self.db.execute(
            select(DeviceRefreshToken)
            .where(DeviceRefreshToken.device_id == device_id)
            .order_by(DeviceRefreshToken.created_at.desc())
        ).scalars().all())
