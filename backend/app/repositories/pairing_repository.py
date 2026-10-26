from datetime import datetime, timezone, timedelta
import secrets
import time
from typing import Callable, Optional, Tuple
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session
from sqlalchemy import func, or_, select, update
from app.models.device import Device
from app.models.pairing_code import PairingCode
from app.models.patient import Patient
from app.repositories.device_repository import DeviceRepository


class PairingCodeCollisionError(ValueError):
    """
    Raised when a pairing code cannot be issued because its HMAC digest collides
    with an already active pairing code in the database. Subclasses ValueError.
    """


class PairingRepository:
    """
    Repository for single-use 6-digit pairing codes and atomic device pairing.

    SECURITY ARCHITECTURE NOTE (Stage 5.3A / 5.3B Separation of Concerns):
    - This repository enforces cryptographic HMAC-SHA256 hashing (`AEGIS_PAIRING_SECRET`),
      global active code uniqueness (`active_code_hash` UNIQUE index), TTL expiration,
      per-code `max_attempts` eligibility checks, and atomic single-use consumption.
    - Unknown code-only requests do NOT mutate or increment `attempts_count` on other
      patients' active pairing codes, preventing cross-patient lockout / DoS.
    - IMPORTANT: A public pairing HTTP endpoint MUST NOT be exposed until Stage 5.3B
      implements API/middleware-level rate limiting (per-IP / per-client throttling and
      lockout) to prevent online brute-force enumeration of the 6-digit code space.
    """

    def __init__(self, db: Session, secret: Optional[str] = None):
        self.db = db
        if secret is not None:
            PairingCode.resolve_secret(secret)
        self._secret = secret

    def _resolve_secret(self, secret: Optional[str] = None) -> str:
        """
        Resolves and validates the server-side pairing secret (fail-closed).
        Raises ValueError if missing or empty.
        """
        if secret is not None:
            return PairingCode.resolve_secret(secret)
        return PairingCode.resolve_secret(self._secret)

    def create_code(
        self,
        patient_id: str,
        ttl_minutes: int = 10,
        max_attempts: int = 5,
        secret: Optional[str] = None,
        raw_code_override: Optional[str] = None,
        max_collision_retries: int = 10,
        max_global_active_codes: int = 100,
        code_generator: Optional[Callable[[], str]] = None,
    ) -> Tuple[str, PairingCode]:
        """
        Issues a new single-use 6-digit pairing code for patient device enrollment.
        Returns a tuple of (plaintext_code, PairingCode_entity).
        - Preserves leading zeros (always 6 numeric digits, e.g. '004219').
        - Plaintext code is NEVER stored in the database or logged.
        - Requires a non-empty server-side pairing secret (fails closed with ValueError).
        - Enforces global uniqueness across all active pairing codes via `active_code_hash` UNIQUE index:
          automatically retries with a fresh code on random collision, or raises PairingCodeCollisionError
          when an explicit override collides or retries are exhausted.
        - Supersedes any previously unconsumed active codes for the same patient.
        """
        if not isinstance(patient_id, str) or not patient_id.strip():
            raise ValueError("patient_id must not be empty.")
        if ttl_minutes <= 0:
            raise ValueError("ttl_minutes must be positive.")
        if max_attempts <= 0:
            raise ValueError("max_attempts must be positive.")
        if max_collision_retries < 0:
            raise ValueError("max_collision_retries must be non-negative.")
        if max_global_active_codes <= 0:
            raise ValueError("max_global_active_codes must be positive.")

        resolved_secret = self._resolve_secret(secret)
        clean_patient_id = patient_id.strip()

        if raw_code_override is not None:
            PairingCode.normalize_code(raw_code_override, strict_six_digits=True)

        effective_collision_retries = 0 if raw_code_override is not None else max_collision_retries
        max_lock_retries = 5

        for collision_attempt in range(effective_collision_retries + 1):
            if raw_code_override is not None:
                raw_code = PairingCode.normalize_code(raw_code_override, strict_six_digits=True)
            elif code_generator is not None:
                raw_code = PairingCode.normalize_code(code_generator(), strict_six_digits=True)
            else:
                raw_code = f"{secrets.randbelow(1_000_000):06d}"

            code_hash = PairingCode.hash_code(raw_code, secret=resolved_secret)

            for lock_attempt in range(max_lock_retries + 1):
                now = datetime.now(timezone.utc)
                expires_at = now + timedelta(minutes=ttl_minutes)

                try:
                    # 1. Release active_code_hash slot on any expired, consumed, or rate-limited codes
                    self.db.execute(
                        update(PairingCode)
                        .where(
                            PairingCode.active_code_hash.is_not(None),
                            or_(
                                PairingCode.consumed_at.is_not(None),
                                PairingCode.expires_at <= now,
                                PairingCode.attempts_count >= PairingCode.max_attempts,
                            ),
                        )
                        .values(active_code_hash=None)
                        .execution_options(synchronize_session=False)
                    )

                    # 2. Supersede any prior unconsumed active codes for this patient
                    self.db.execute(
                        update(PairingCode)
                        .where(
                            PairingCode.patient_id == clean_patient_id,
                            PairingCode.active_code_hash.is_not(None),
                        )
                        .values(
                            expires_at=now,
                            active_code_hash=None,
                        )
                        .execution_options(synchronize_session=False)
                    )

                    # 3. Enforce cap on global concurrent active pairing procedures
                    active_count = self.db.execute(
                        select(func.count(PairingCode.code_id)).where(
                            PairingCode.active_code_hash.is_not(None),
                            PairingCode.consumed_at.is_(None),
                            PairingCode.expires_at > now,
                            PairingCode.attempts_count < PairingCode.max_attempts,
                        )
                    ).scalar_one()
                    if active_count >= max_global_active_codes:
                        self.db.rollback()
                        raise RuntimeError(
                            f"Maximum number of concurrent active pairing procedures ({max_global_active_codes}) reached."
                        )

                    # 4. Insert new pairing code with unique active_code_hash
                    code_entity = PairingCode(
                        patient_id=clean_patient_id,
                        code_hash=code_hash,
                        active_code_hash=code_hash,
                        expires_at=expires_at,
                        attempts_count=0,
                        max_attempts=max_attempts,
                        created_at=now,
                    )
                    self.db.add(code_entity)
                    self.db.commit()
                    self.db.refresh(code_entity)
                    return raw_code, code_entity

                except IntegrityError as exc:
                    self.db.rollback()
                    err_msg = str(exc).lower()
                    if "foreign key" in err_msg:
                        raise
                    if collision_attempt < effective_collision_retries:
                        break
                    raise PairingCodeCollisionError(
                        "Active pairing code collision detected: another active pairing procedure "
                        "already holds this code digest."
                    ) from exc
                except OperationalError as exc:
                    self.db.rollback()
                    err_msg = str(exc).lower()
                    if lock_attempt < max_lock_retries and ("locked" in err_msg or "busy" in err_msg):
                        time.sleep(0.015 * (lock_attempt + 1))
                        continue
                    raise
                except Exception:
                    self.db.rollback()
                    raise

        raise PairingCodeCollisionError(
            "Exhausted collision retries while generating a unique active pairing code."
        )

    def verify_and_consume(
        self,
        raw_code: str,
        secret: Optional[str] = None,
        *,
        patient_id: Optional[str] = None,
        commit: bool = True,
    ) -> Tuple[bool, str, Optional[PairingCode]]:
        """
        Code-only atomic verification and single-use consumption of a pairing code.
        - Resolves `patient_id` directly from the unique active `code_hash` without requiring
          the caller to know `patient_id` in advance (`patient_id` filter is optional).
        - Uses a single conditional SQL UPDATE inside a transaction to guarantee at-most-once
          consumption under concurrent requests across SQLite and PostgreSQL.
        - Never modifies or increments `attempts_count` on other patients' active codes when
          an unknown/invalid code is submitted, preventing cross-patient lockout.
        - When `commit=False`, neither commits nor rolls back the caller's outer transaction
          on verification failure or success (only flushes on success), preserving outer
          transactional semantics.
        """
        clean_patient_id: Optional[str] = None
        if patient_id is not None:
            if not isinstance(patient_id, str) or not patient_id.strip():
                raise ValueError("patient_id must not be empty when provided.")
            clean_patient_id = patient_id.strip()

        resolved_secret = self._resolve_secret(secret)
        target_hash = PairingCode.hash_code(raw_code, secret=resolved_secret)

        max_lock_retries = 5

        for attempt in range(max_lock_retries + 1):
            now = datetime.now(timezone.utc)

            consume_conditions = [
                PairingCode.active_code_hash == target_hash,
                PairingCode.code_hash == target_hash,
                PairingCode.consumed_at.is_(None),
                PairingCode.expires_at > now,
                PairingCode.attempts_count < PairingCode.max_attempts,
            ]
            if clean_patient_id is not None:
                consume_conditions.append(PairingCode.patient_id == clean_patient_id)

            consume_stmt = (
                update(PairingCode)
                .where(*consume_conditions)
                .values(
                    consumed_at=now,
                    active_code_hash=None,
                )
                .execution_options(synchronize_session=False)
            )

            try:
                res = self.db.execute(consume_stmt)
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
                    if commit:
                        self.db.commit()
                    else:
                        self.db.flush()
                    consumed = self.db.execute(
                        select(PairingCode)
                        .where(
                            PairingCode.code_hash == target_hash,
                            PairingCode.consumed_at == now,
                        )
                        .order_by(PairingCode.created_at.desc(), PairingCode.code_id.desc())
                        .limit(1)
                        .execution_options(populate_existing=True)
                    ).scalar_one_or_none()
                    return True, "Code successfully verified and consumed", consumed
                except Exception:
                    if commit:
                        self.db.rollback()
                    raise

            # Verification did not match any active eligible code.
            # Do NOT modify any other patient's active codes, and if commit=False,
            # do NOT commit or rollback the caller's outer transaction.
            return False, "Invalid, expired, or rate-limited pairing code", None

        return False, "Invalid, expired, or rate-limited pairing code", None

    def pair_device(
        self,
        raw_code: str,
        device_name: str,
        platform: str = "android",
        device_id: Optional[str] = None,
        secret: Optional[str] = None,
        *,
        commit: bool = True,
    ) -> Tuple[bool, str, Optional[Device], Optional[PairingCode]]:
        """
        Atomically verifies and consumes a single-use pairing code, resolves its `patient_id`,
        and registers the device within a SINGLE database transaction.
        Guarantees that if device registration fails for any reason (validation error,
        device ownership conflict, or database error), the transaction is rolled back
        and the pairing code remains unconsumed.
        """
        ok, msg, consumed = self.verify_and_consume(
            raw_code=raw_code,
            secret=secret,
            commit=False,
        )
        if not ok or consumed is None:
            return False, msg, None, None

        try:
            patient = self.db.execute(
                select(Patient)
                .where(Patient.patient_id == consumed.patient_id)
                .execution_options(populate_existing=True)
            ).scalar_one_or_none()
            if patient is None or patient.status != "active":
                raise ValueError("Cannot pair device: patient is not active.")

            device_repo = DeviceRepository(self.db)
            device = device_repo.register_device(
                patient_id=consumed.patient_id,
                device_name=device_name,
                platform=platform,
                device_id=device_id,
                commit=False,
            )
            if commit:
                self.db.commit()
                self.db.refresh(device)
                self.db.refresh(consumed)
            else:
                self.db.flush()
            return True, "Device paired and pairing code consumed atomically", device, consumed
        except Exception:
            if commit:
                self.db.rollback()
            raise
