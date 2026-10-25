from datetime import datetime, timezone, timedelta
import gc
import hashlib
from pathlib import Path
import tempfile
import threading
from unittest.mock import patch
import uuid
import pytest
from sqlalchemy import create_engine, event, inspect, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker
from alembic.config import Config
from alembic import command

from app.config import settings
from app.database import Base
from app.models.patient import Patient
from app.models.device import Device
from app.models.pairing_code import PairingCode
from app.models.device_refresh_token import DeviceRefreshToken
from app.models.medical_cycle import MedicalCycle
from app.models.health_measurement import HealthMeasurement
from app.models.model_report import ModelReport
from app.repositories.patient_repository import PatientRepository
from app.repositories.device_repository import DeviceRepository
from app.repositories.pairing_repository import (
    PairingRepository,
    PairingCodeCollisionError,
)
from app.repositories.token_repository import DeviceTokenRepository
from app.repositories.cycle_repository import (
    MedicalCycleRepository,
    CycleOwnershipConflictError,
)


def _create_isolated_file_session_factory(db_path: Path):
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False, "timeout": 5.0},
        pool_pre_ping=True,
    )

    @event.listens_for(engine, "connect")
    def _configure_sqlite(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON;")
        cursor.execute("PRAGMA busy_timeout=5000;")
        cursor.close()

    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    return engine, factory


def test_01_resolve_patient_id_by_code_only(db_session: Session):
    """
    Test 1: Successful resolution of patient_id using ONLY the 6-digit pairing code
    (without passing patient_id to verify_and_consume).
    """
    patient = PatientRepository(db_session).create()
    secret = "code_only_resolution_secret_key"
    pairing_repo = PairingRepository(db_session, secret=secret)

    raw_code, issued_entity = pairing_repo.create_code(patient_id=patient.patient_id)
    assert len(raw_code) == 6
    assert issued_entity.patient_id == patient.patient_id
    assert issued_entity.active_code_hash == issued_entity.code_hash

    # Verify and consume using ONLY raw_code (no patient_id passed)
    ok, msg, consumed = pairing_repo.verify_and_consume(raw_code)
    assert ok is True
    assert consumed is not None
    assert consumed.code_id == issued_entity.code_id
    assert consumed.patient_id == patient.patient_id
    assert consumed.is_consumed is True
    assert consumed.active_code_hash is None


def test_02_pairing_code_preserves_leading_zeros(db_session: Session):
    """
    Test 2: Leading zeros (e.g. '000000', '004219', '010203') are strictly preserved
    during generation, normalization, hashing, and verification.
    """
    patient = PatientRepository(db_session).create()
    secret = "leading_zeros_hmac_secret"
    pairing_repo = PairingRepository(db_session, secret=secret)

    with patch("app.repositories.pairing_repository.secrets.randbelow", return_value=4219):
        raw_code, entity = pairing_repo.create_code(patient_id=patient.patient_id)

    assert raw_code == "004219"
    assert len(raw_code) == 6
    assert PairingCode.normalize_code("004219", strict_six_digits=True) == "004219"
    assert PairingCode.normalize_code("000-000", strict_six_digits=True) == "000000"

    # Truncated code without leading zeros ('4219') must not match '004219'
    ok_short, _, _ = pairing_repo.verify_and_consume("4219")
    assert ok_short is False
    db_session.refresh(entity)
    assert entity.attempts_count == 0
    assert entity.is_consumed is False

    with pytest.raises(ValueError, match="6-digit numeric string"):
        PairingCode.normalize_code("4219", strict_six_digits=True)

    # Exact code with leading zeros succeeds
    ok_exact, _, consumed = pairing_repo.verify_and_consume("004219")
    assert ok_exact is True
    assert consumed is not None
    assert consumed.patient_id == patient.patient_id


def test_03_two_concurrently_active_patients_with_different_codes(db_session: Session):
    """
    Test 3: Two active patients simultaneously hold distinct active pairing codes.
    Consuming Patient A's code resolves Patient A without affecting Patient B's active code,
    and consuming Patient B's code resolves Patient B.
    """
    p_repo = PatientRepository(db_session)
    p_a = p_repo.create()
    p_b = p_repo.create()

    secret = "multi_patient_active_secret"
    pairing_repo = PairingRepository(db_session, secret=secret)

    code_a, ent_a = pairing_repo.create_code(
        patient_id=p_a.patient_id,
        raw_code_override="011222",
    )
    code_b, ent_b = pairing_repo.create_code(
        patient_id=p_b.patient_id,
        raw_code_override="033444",
    )

    assert code_a == "011222"
    assert code_b == "033444"

    # Consume Patient B's code first (code-only)
    ok_b, _, consumed_b = pairing_repo.verify_and_consume("033444")
    assert ok_b is True
    assert consumed_b is not None
    assert consumed_b.patient_id == p_b.patient_id

    # Patient A's code must still be active and untouched (attempts_count == 0)
    db_session.refresh(ent_a)
    assert ent_a.is_consumed is False
    assert ent_a.attempts_count == 0
    assert ent_a.active_code_hash == ent_a.code_hash

    # Consume Patient A's code (code-only)
    ok_a, _, consumed_a = pairing_repo.verify_and_consume("011222")
    assert ok_a is True
    assert consumed_a is not None
    assert consumed_a.patient_id == p_a.patient_id


def test_04_pairing_code_collision_on_generation_retries_and_rejects_duplicate(db_session: Session):
    """
    Test 4: Collision handling during pairing code generation:
    - If random generator produces a code already active for Patient A, create_code()
      automatically detects the collision and retries to issue a fresh unique code for Patient B.
    - If an explicit override (or exhausted retry sequence) collides with an active code,
      create_code() raises PairingCodeCollisionError.
    - Once Patient A's code is consumed, its active_code_hash slot is freed for future reuse.
    """
    p_repo = PatientRepository(db_session)
    p_a = p_repo.create()
    p_b = p_repo.create()
    p_c = p_repo.create()

    secret = "collision_test_secret_key"
    pairing_repo = PairingRepository(db_session, secret=secret)

    # Patient A holds active code '123456'
    code_a, _ = pairing_repo.create_code(
        patient_id=p_a.patient_id,
        raw_code_override="123456",
    )
    assert code_a == "123456"

    # 1. Generator for Patient B first yields colliding '123456', then unique '654321'
    seq = iter(["123456", "654321"])
    code_b, ent_b = pairing_repo.create_code(
        patient_id=p_b.patient_id,
        code_generator=lambda: next(seq),
    )
    assert code_b == "654321"
    assert ent_b.patient_id == p_b.patient_id

    # 2. Explicit duplicate override '123456' for Patient C while '123456' is active for Patient A -> fails
    with pytest.raises(PairingCodeCollisionError):
        pairing_repo.create_code(
            patient_id=p_c.patient_id,
            raw_code_override="123456",
        )

    # 3. Exhausted retries when generator only yields colliding '123456' -> fails
    with pytest.raises(PairingCodeCollisionError):
        pairing_repo.create_code(
            patient_id=p_c.patient_id,
            max_collision_retries=2,
            code_generator=lambda: "123456",
        )

    # 4. After Patient A consumes '123456', its active slot is released and Patient C can be issued '123456'
    ok_a, _, _ = pairing_repo.verify_and_consume("123456")
    assert ok_a is True

    code_c, ent_c = pairing_repo.create_code(
        patient_id=p_c.patient_id,
        raw_code_override="123456",
    )
    assert code_c == "123456"
    assert ent_c.patient_id == p_c.patient_id


def test_05_concurrent_code_generation_with_collision_attempt():
    """
    Test 5: Concurrent pairing code generation with collision attempt across two threads:
    - Subcase A: Both threads force the same code '555666' -> exactly 1 succeeds and 1 raises PairingCodeCollisionError.
    - Subcase B: Both threads use a generator whose first choice collides ('888999') and second choice is distinct ->
      both threads succeed with distinct active codes.
    """
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
        db_path = Path(tmpdir) / "concurrent_gen_collision.db"
        engine, SessionFactory = _create_isolated_file_session_factory(db_path)
        secret = "concurrent_gen_hmac_secret"

        try:
            seed = SessionFactory()
            p_repo = PatientRepository(seed)
            p1_id = p_repo.create().patient_id
            p2_id = p_repo.create().patient_id
            p3_id = p_repo.create().patient_id
            p4_id = p_repo.create().patient_id
            seed.close()

            # Subcase A: Forced collision on '555666'
            barrier_a = threading.Barrier(2)
            successes_a = []
            collisions_a = []
            unexpected_a = []
            lock = threading.Lock()

            def _forced_worker(pid: str):
                sess = SessionFactory()
                try:
                    repo = PairingRepository(sess, secret=secret)
                    barrier_a.wait(timeout=5.0)
                    code, ent = repo.create_code(patient_id=pid, raw_code_override="555666")
                    with lock:
                        successes_a.append((pid, code, ent.code_id))
                except PairingCodeCollisionError as exc:
                    with lock:
                        collisions_a.append(str(exc))
                except Exception as exc:
                    with lock:
                        unexpected_a.append(exc)
                finally:
                    sess.close()

            t1 = threading.Thread(target=_forced_worker, args=(p1_id,))
            t2 = threading.Thread(target=_forced_worker, args=(p2_id,))
            t1.start()
            t2.start()
            t1.join(timeout=10.0)
            t2.join(timeout=10.0)

            assert not unexpected_a, f"Unexpected errors in Subcase A: {unexpected_a}"
            assert len(successes_a) == 1
            assert len(collisions_a) == 1

            # Subcase B: Automatic retry on concurrent first-choice collision ('888999')
            barrier_b = threading.Barrier(2)
            successes_b = []
            unexpected_b = []

            def _retry_worker(pid: str, fallback_code: str):
                sess = SessionFactory()
                seq = iter(["888999", fallback_code])
                try:
                    repo = PairingRepository(sess, secret=secret)
                    barrier_b.wait(timeout=5.0)
                    code, ent = repo.create_code(
                        patient_id=pid,
                        code_generator=lambda: next(seq),
                    )
                    with lock:
                        successes_b.append((pid, code, ent.code_id))
                except Exception as exc:
                    with lock:
                        unexpected_b.append(exc)
                finally:
                    sess.close()

            t3 = threading.Thread(target=_retry_worker, args=(p3_id, "888001"))
            t4 = threading.Thread(target=_retry_worker, args=(p4_id, "888002"))
            t3.start()
            t4.start()
            t3.join(timeout=10.0)
            t4.join(timeout=10.0)

            assert not unexpected_b, f"Unexpected errors in Subcase B: {unexpected_b}"
            assert len(successes_b) == 2
            issued_codes_b = {item[1] for item in successes_b}
            assert "888999" in issued_codes_b
            assert len(issued_codes_b) == 2
        finally:
            engine.dispose()
            gc.collect()


def test_06_double_use_of_single_code_rejected(db_session: Session):
    """
    Test 6: A pairing code can be used at most once; a second verification attempt
    with the same code is strictly rejected.
    """
    patient = PatientRepository(db_session).create()
    secret = "single_use_hmac_secret"
    pairing_repo = PairingRepository(db_session, secret=secret)

    raw_code, _ = pairing_repo.create_code(
        patient_id=patient.patient_id,
        raw_code_override="123456",
    )

    ok_first, _, consumed_first = pairing_repo.verify_and_consume(raw_code)
    assert ok_first is True
    assert consumed_first is not None
    assert consumed_first.patient_id == patient.patient_id
    assert consumed_first.is_consumed is True

    ok_second, msg_second, consumed_second = pairing_repo.verify_and_consume(raw_code)
    assert ok_second is False
    assert consumed_second is None
    assert "Invalid, expired, or rate-limited" in msg_second


def test_07_concurrent_code_consumption_allows_at_most_one_winner():
    """
    Test 7: Concurrent code-only consumption:
    Two parallel threads attempting to consume the same valid pairing code simultaneously
    result in exactly ONE success and ONE rejection.
    """
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
        db_path = Path(tmpdir) / "concurrent_pairing.db"
        engine, SessionFactory = _create_isolated_file_session_factory(db_path)
        secret = "concurrent_pairing_hmac_secret"

        try:
            seed_session = SessionFactory()
            patient = PatientRepository(seed_session).create()
            patient_id = patient.patient_id
            raw_code, code_entity = PairingRepository(seed_session, secret=secret).create_code(
                patient_id=patient_id,
                raw_code_override="654321",
            )
            code_id = code_entity.code_id
            seed_session.close()

            barrier = threading.Barrier(2)
            results = []
            errors = []
            lock = threading.Lock()

            def _worker():
                worker_session = SessionFactory()
                try:
                    repo = PairingRepository(worker_session, secret=secret)
                    barrier.wait(timeout=5.0)
                    ok, msg, consumed = repo.verify_and_consume(raw_code)
                    with lock:
                        results.append(
                            (
                                ok,
                                msg,
                                consumed.code_id if consumed else None,
                                consumed.patient_id if consumed else None,
                            )
                        )
                except Exception as exc:
                    with lock:
                        errors.append(exc)
                finally:
                    worker_session.close()

            t1 = threading.Thread(target=_worker)
            t2 = threading.Thread(target=_worker)
            t1.start()
            t2.start()
            t1.join(timeout=10.0)
            t2.join(timeout=10.0)

            assert not errors, f"Unexpected worker errors during concurrent consume: {errors}"
            assert len(results) == 2
            successes = [r for r in results if r[0] is True]
            failures = [r for r in results if r[0] is False]
            assert len(successes) == 1, f"Expected exactly 1 consumption success, got: {results}"
            assert len(failures) == 1
            assert successes[0][2] == code_id
            assert successes[0][3] == patient_id

            check_session = SessionFactory()
            persisted = check_session.get(PairingCode, code_id)
            assert persisted is not None
            assert persisted.is_consumed is True
            assert persisted.active_code_hash is None
            check_session.close()
        finally:
            engine.dispose()
            gc.collect()


def test_08_expired_and_rate_limited_locked_codes_rejected(db_session: Session):
    """
    Test 8: Expired codes and rate-limited (attempts_count >= max_attempts) codes are rejected
    with uniform error messages that do not leak patient existence.
    """
    p_repo = PatientRepository(db_session)
    p1 = p_repo.create()
    p2 = p_repo.create()
    secret = "rate_limit_hmac_secret_key"
    pairing_repo = PairingRepository(db_session, secret=secret)

    # 1. Code that has reached max_attempts (locked) must be rejected even with exact valid code
    locked_code = PairingCode(
        patient_id=p1.patient_id,
        code_hash=PairingCode.hash_code("246810", secret=secret),
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
        attempts_count=3,
        max_attempts=3,
    )
    db_session.add(locked_code)
    db_session.commit()
    assert locked_code.is_rate_limited is True
    assert locked_code.is_valid_for_use is False

    ok_locked, msg_locked, _ = pairing_repo.verify_and_consume("246810")
    assert ok_locked is False
    assert msg_locked == "Invalid, expired, or rate-limited pairing code"
    db_session.refresh(locked_code)
    assert locked_code.is_consumed is False

    # 2. TTL Expiration test: expired code cannot be verified or consumed
    expired_code = PairingCode(
        patient_id=p2.patient_id,
        code_hash=PairingCode.hash_code("888888", secret=secret),
        expires_at=datetime.now(timezone.utc) - timedelta(minutes=5),
        attempts_count=0,
        max_attempts=5,
    )
    db_session.add(expired_code)
    db_session.commit()
    assert expired_code.is_expired is True
    assert expired_code.is_valid_for_use is False

    ok_exp, msg_exp, _ = pairing_repo.verify_and_consume("888888")
    assert ok_exp is False
    assert msg_exp == "Invalid, expired, or rate-limited pairing code"
    db_session.refresh(expired_code)
    assert expired_code.is_consumed is False


def test_09_transaction_error_during_consumption_rolls_back_cleanly(db_session: Session):
    """
    Test 9: If a transaction failure occurs during pairing code consumption,
    the transaction is rolled back, the code remains unconsumed and active,
    and can be consumed once the database recovers.
    """
    patient = PatientRepository(db_session).create()
    secret = "tx_rollback_hmac_secret"
    pairing_repo = PairingRepository(db_session, secret=secret)

    raw_code, entity = pairing_repo.create_code(
        patient_id=patient.patient_id,
        raw_code_override="777888",
    )

    with patch.object(db_session, "commit", side_effect=RuntimeError("Simulated transaction commit failure")):
        with pytest.raises(RuntimeError, match="Simulated transaction commit failure"):
            pairing_repo.verify_and_consume(raw_code)

    # Code must remain unconsumed and active after rollback
    db_session.expire_all()
    reloaded = db_session.get(PairingCode, entity.code_id)
    assert reloaded is not None
    assert reloaded.is_consumed is False
    assert reloaded.consumed_at is None
    assert reloaded.active_code_hash == reloaded.code_hash

    # Subsequent consumption in a healthy transaction succeeds
    ok, _, consumed = pairing_repo.verify_and_consume(raw_code)
    assert ok is True
    assert consumed is not None
    assert consumed.is_consumed is True
    assert consumed.patient_id == patient.patient_id


def test_10_device_registration_failure_after_code_verification_rolls_back_code_consumption(db_session: Session):
    """
    Test 10: Atomic pairing transaction (pair_device):
    If device registration fails after pairing code verification (e.g., due to a device_id
    conflict with another patient, invalid device parameters, or a DB error), the entire
    transaction is rolled back so the pairing code is NOT consumed and remains usable.
    """
    p_repo = PatientRepository(db_session)
    device_repo = DeviceRepository(db_session)
    p1 = p_repo.create()
    p2 = p_repo.create()

    # Register a device owned by p2
    device_repo.register_device(
        patient_id=p2.patient_id,
        device_name="Patient 2 Watch",
        device_id="dev_owned_by_p2",
    )

    secret = "atomic_pair_device_secret"
    pairing_repo = PairingRepository(db_session, secret=secret)
    raw_code, code_entity = pairing_repo.create_code(
        patient_id=p1.patient_id,
        raw_code_override="909090",
    )

    # 1. Attempt to pair p1's code using p2's existing device_id -> device registration fails!
    with pytest.raises(ValueError, match="already registered to a different patient"):
        pairing_repo.pair_device(
            raw_code=raw_code,
            device_name="Conflicting Device",
            device_id="dev_owned_by_p2",
        )

    # Pairing code for p1 MUST still be unconsumed and active!
    db_session.expire_all()
    reloaded_code = db_session.get(PairingCode, code_entity.code_id)
    assert reloaded_code is not None
    assert reloaded_code.is_consumed is False
    assert reloaded_code.consumed_at is None
    assert reloaded_code.active_code_hash == reloaded_code.code_hash

    # 2. Simulate a database commit failure during pair_device
    with patch.object(db_session, "commit", side_effect=RuntimeError("Simulated DB error during device insert")):
        with pytest.raises(RuntimeError, match="Simulated DB error during device insert"):
            pairing_repo.pair_device(
                raw_code=raw_code,
                device_name="Pixel 9 Pro",
                device_id="dev_p1_pixel",
            )

    db_session.expire_all()
    assert device_repo.get("dev_p1_pixel") is None
    reloaded_code = db_session.get(PairingCode, code_entity.code_id)
    assert reloaded_code is not None
    assert reloaded_code.is_consumed is False
    assert reloaded_code.active_code_hash == reloaded_code.code_hash

    # 3. Retry pair_device with valid parameters -> succeeds atomically!
    ok, _, paired_device, consumed_code = pairing_repo.pair_device(
        raw_code=raw_code,
        device_name="Pixel 9 Pro",
        device_id="dev_p1_pixel",
    )
    assert ok is True
    assert paired_device is not None
    assert paired_device.device_id == "dev_p1_pixel"
    assert paired_device.patient_id == p1.patient_id
    assert consumed_code is not None
    assert consumed_code.is_consumed is True
    assert consumed_code.active_code_hash is None


def test_11_no_plaintext_pairing_codes_in_db_logs_or_exceptions(db_session: Session):
    """
    Test 11: Verifies that plaintext pairing codes are never stored in any column
    of pairing_codes, never match unsalted SHA-256, and never appear in exception messages.
    """
    patient = PatientRepository(db_session).create()
    server_secret = "server_hmac_secret_alpha_99"
    pairing_repo = PairingRepository(db_session, secret=server_secret)

    raw_code, entity = pairing_repo.create_code(
        patient_id=patient.patient_id,
        raw_code_override="314159",
    )

    assert raw_code == "314159"
    assert "314159" not in entity.code_hash
    assert len(entity.code_hash) == 64
    assert entity.code_hash == PairingCode.hash_code("314159", secret=server_secret)
    assert entity.code_hash != hashlib.sha256(b"314159").hexdigest()
    assert entity.code_hash != hashlib.sha256(b":314159").hexdigest()

    raw_row = db_session.execute(
        text(
            "SELECT code_id, patient_id, code_hash, active_code_hash "
            "FROM pairing_codes WHERE code_id = :cid"
        ),
        {"cid": entity.code_id},
    ).fetchone()
    assert raw_row is not None
    for col_val in raw_row:
        assert "314159" not in str(col_val)
        assert server_secret not in str(col_val)


def test_12_missing_and_empty_pairing_secret_fails_closed(db_session: Session, monkeypatch):
    """
    Test 12: Verifies fail-closed behavior when pairing secret is missing, empty, or wrong,
    and confirms AEGIS_API_SECRET is never automatically used as a fallback.
    """
    patient = PatientRepository(db_session).create()
    pairing_repo = PairingRepository(db_session)

    monkeypatch.setattr(settings, "api_secret", "configured_global_api_secret_value")
    monkeypatch.setattr(settings, "pairing_secret", "")

    with pytest.raises(ValueError, match="Pairing code secret is required"):
        PairingCode.hash_code("123456")

    with pytest.raises(ValueError, match="Pairing code secret is required"):
        pairing_repo.create_code(patient_id=patient.patient_id)

    with pytest.raises(ValueError, match="Pairing code secret is required"):
        pairing_repo.verify_and_consume("123456")

    monkeypatch.setattr(settings, "pairing_secret", "valid_env_pairing_secret_key")

    for invalid_secret in ["", "   ", "\t\n"]:
        with pytest.raises(ValueError, match="Pairing code secret is required"):
            PairingCode.hash_code("123456", secret=invalid_secret)

        with pytest.raises(ValueError, match="Pairing code secret is required"):
            PairingRepository(db_session, secret=invalid_secret)

        with pytest.raises(ValueError, match="Pairing code secret is required"):
            pairing_repo.create_code(patient_id=patient.patient_id, secret=invalid_secret)

        with pytest.raises(ValueError, match="Pairing code secret is required"):
            pairing_repo.verify_and_consume("123456", secret=invalid_secret)

    # Wrong secret must fail verification without mutating active code state
    _, entity = pairing_repo.create_code(
        patient_id=patient.patient_id,
        raw_code_override="123456",
        secret="correct_secret_key_01",
    )
    ok_wrong, _, _ = pairing_repo.verify_and_consume("123456", secret="wrong_secret_key_02")
    assert ok_wrong is False
    db_session.refresh(entity)
    assert entity.is_consumed is False
    assert entity.attempts_count == 0
    assert entity.active_code_hash == entity.code_hash


def test_13_migration_0003_preserves_existing_records_and_medical_history():
    """
    Test 13: Comprehensive migration test on an isolated temporary SQLite database:
    1. Upgrades 0001 -> 0002.
    2. Seeds existing legacy medical history (medical_cycles, health_measurements, model_reports).
    3. Upgrades to 0003 (head).
    4. Confirms tables (patients, devices, pairing_codes, device_refresh_tokens) and
       unique index ix_pairing_codes_active_code_hash exist.
    5. Confirms legacy cycle has patient_id IS NULL and all historical measurements
       and model_reports are 100% preserved.
    6. Tests downgrade back to 0002 without losing medical history.
    7. Tests re-upgrade to 0003 (idempotent re-run).
    """
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
        test_db_path = Path(tmpdir) / "test_migration_0003.db"
        test_db_url = f"sqlite:///{test_db_path}"

        alembic_ini_path = Path(__file__).resolve().parent.parent / "alembic.ini"
        alembic_cfg = Config(str(alembic_ini_path))
        alembic_cfg.set_main_option("script_location", str(alembic_ini_path.parent / "alembic"))
        alembic_cfg.set_main_option("sqlalchemy.url", test_db_url)

        # 1. Upgrade to 0002
        command.upgrade(alembic_cfg, "0002_model_reports_version_unique")

        # Seed legacy cycle, measurement, and model report
        engine = create_engine(test_db_url)
        SessionLocal = sessionmaker(bind=engine)
        sess = SessionLocal()
        sess.execute(text(
            "INSERT INTO medical_cycles (cycle_id, status, started_at, created_at, updated_at) "
            "VALUES ('legacy_cycle_seeded', 'active', '2026-10-23 10:00:00', '2026-10-23 10:00:00', '2026-10-23 10:00:00')"
        ))
        sess.execute(text(
            "INSERT INTO health_measurements (cycle_id, pulse, source, is_simulated, is_historical, measured_at, ingested_at, phase, note) "
            "VALUES ('legacy_cycle_seeded', 72, 'watch_sensor', 0, 0, '2026-10-23 10:05:00', '2026-10-23 10:05:01', 'standby', '')"
        ))
        sess.execute(text(
            "INSERT INTO model_reports (report_id, cycle_id, model, report_type, privacy_level, version, content, created_at) "
            "VALUES ('rep_hist_01', 'legacy_cycle_seeded', 'gemini-3.1-pro-preview', 'primary_consilium', 'client_facing', 1, '{\"summary\": \"historical\"}', '2026-10-23 10:10:00')"
        ))
        sess.commit()
        sess.close()
        engine.dispose()
        gc.collect()

        # 2. Upgrade to 0003 (head)
        command.upgrade(alembic_cfg, "0003_identity_and_device_pairing")

        # 3. Verify new tables, columns, unique index, and preserved medical history
        engine = create_engine(test_db_url)
        SessionLocal = sessionmaker(bind=engine)
        try:
            inspector = inspect(engine)
            tables = set(inspector.get_table_names())
            assert {"patients", "devices", "pairing_codes", "device_refresh_tokens"}.issubset(tables)

            cycle_cols = [c["name"] for c in inspector.get_columns("medical_cycles")]
            assert "patient_id" in cycle_cols

            pairing_cols = [c["name"] for c in inspector.get_columns("pairing_codes")]
            assert "active_code_hash" in pairing_cols
            pairing_indexes = {idx["name"]: idx for idx in inspector.get_indexes("pairing_codes")}
            assert "ix_pairing_codes_active_code_hash" in pairing_indexes
            assert bool(pairing_indexes["ix_pairing_codes_active_code_hash"]["unique"]) is True

            sess = SessionLocal()
            cycle_row = sess.execute(
                text("SELECT cycle_id, patient_id, status FROM medical_cycles WHERE cycle_id = 'legacy_cycle_seeded'")
            ).fetchone()
            assert cycle_row == ("legacy_cycle_seeded", None, "active")

            meas_row = sess.execute(
                text("SELECT cycle_id, pulse, source FROM health_measurements WHERE cycle_id = 'legacy_cycle_seeded'")
            ).fetchone()
            assert meas_row == ("legacy_cycle_seeded", 72, "watch_sensor")

            rep_row = sess.execute(
                text("SELECT report_id, cycle_id, version FROM model_reports WHERE report_id = 'rep_hist_01'")
            ).fetchone()
            assert rep_row == ("rep_hist_01", "legacy_cycle_seeded", 1)
            sess.close()
        finally:
            engine.dispose()
            gc.collect()

        # 4. Test Downgrade to 0002 and verify medical history is still intact
        command.downgrade(alembic_cfg, "0002_model_reports_version_unique")

        engine = create_engine(test_db_url)
        SessionLocal = sessionmaker(bind=engine)
        try:
            inspector = inspect(engine)
            tables_after_down = set(inspector.get_table_names())
            assert "patients" not in tables_after_down
            assert "devices" not in tables_after_down
            cycle_cols_down = [c["name"] for c in inspector.get_columns("medical_cycles")]
            assert "patient_id" not in cycle_cols_down

            sess = SessionLocal()
            assert sess.execute(text("SELECT COUNT(*) FROM medical_cycles WHERE cycle_id = 'legacy_cycle_seeded'")).scalar() == 1
            assert sess.execute(text("SELECT COUNT(*) FROM health_measurements WHERE cycle_id = 'legacy_cycle_seeded'")).scalar() == 1
            assert sess.execute(text("SELECT COUNT(*) FROM model_reports WHERE report_id = 'rep_hist_01'")).scalar() == 1
            sess.close()
        finally:
            engine.dispose()
            gc.collect()

        # 5. Re-upgrade to 0003 (head)
        command.upgrade(alembic_cfg, "head")

        engine = create_engine(test_db_url)
        SessionLocal = sessionmaker(bind=engine)
        try:
            inspector = inspect(engine)
            tables_reup = set(inspector.get_table_names())
            assert {"patients", "devices", "pairing_codes", "device_refresh_tokens"}.issubset(tables_reup)
            sess = SessionLocal()
            assert sess.execute(text("SELECT COUNT(*) FROM medical_cycles WHERE cycle_id = 'legacy_cycle_seeded'")).scalar() == 1
            assert sess.execute(text("SELECT COUNT(*) FROM health_measurements WHERE cycle_id = 'legacy_cycle_seeded'")).scalar() == 1
            assert sess.execute(text("SELECT COUNT(*) FROM model_reports WHERE report_id = 'rep_hist_01'")).scalar() == 1
            sess.close()
        finally:
            engine.dispose()
            gc.collect()


def test_14_medical_cycle_ownership_protection_and_concurrency_no_regressions(db_session: Session):
    """
    Test 14: Verifies MedicalCycle ownership assignment, idempotency, reassignment rejection,
    preservation of measurements/reports, and concurrent assignment race protection.
    """
    patient_repo = PatientRepository(db_session)
    cycle_repo = MedicalCycleRepository(db_session)

    p1 = patient_repo.create()
    p2 = patient_repo.create()

    legacy_cycle = cycle_repo.create(cycle_id="cycle_legacy_unassigned", status="active")
    assert legacy_cycle.patient_id is None

    measurement = HealthMeasurement(
        cycle_id=legacy_cycle.cycle_id,
        pulse=68,
        source="watch_sensor",
        measured_at=datetime.now(timezone.utc),
    )
    report = ModelReport(
        report_id="rep_legacy_01",
        cycle_id=legacy_cycle.cycle_id,
        model="gemini-3.1-pro-preview",
        report_type="primary_consilium",
        privacy_level="client_facing",
        version=1,
        content={"summary": "Preserved clinical report"},
    )
    db_session.add_all([measurement, report])
    db_session.commit()
    meas_id = measurement.id

    # Assign legacy cycle to p1
    linked = cycle_repo.assign_patient("cycle_legacy_unassigned", p1.patient_id)
    assert linked is not None
    assert linked.patient_id == p1.patient_id

    # Idempotent re-assignment to p1
    linked_again = cycle_repo.assign_patient("cycle_legacy_unassigned", p1.patient_id)
    assert linked_again is not None
    assert linked_again.patient_id == p1.patient_id

    # Reassignment to p2 must fail with CycleOwnershipConflictError
    with pytest.raises(CycleOwnershipConflictError):
        cycle_repo.assign_patient("cycle_legacy_unassigned", p2.patient_id)

    db_session.expire_all()
    assert cycle_repo.get("cycle_legacy_unassigned").patient_id == p1.patient_id
    assert db_session.get(HealthMeasurement, meas_id).pulse == 68
    assert db_session.get(ModelReport, "rep_legacy_01").content == {"summary": "Preserved clinical report"}

    # Concurrent assignment race on isolated file DB
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
        db_path = Path(tmpdir) / "concurrent_cycle_assign.db"
        engine, SessionFactory = _create_isolated_file_session_factory(db_path)
        try:
            seed = SessionFactory()
            sp_repo = PatientRepository(seed)
            sc_repo = MedicalCycleRepository(seed)
            sp1_id = sp_repo.create().patient_id
            sp2_id = sp_repo.create().patient_id
            sc_repo.create(cycle_id="legacy_race_cycle", status="active")
            seed.close()

            barrier = threading.Barrier(2)
            successes = []
            conflicts = []
            lock = threading.Lock()

            def _assign_worker(target_pid: str):
                ws = SessionFactory()
                try:
                    repo = MedicalCycleRepository(ws)
                    barrier.wait(timeout=5.0)
                    assigned = repo.assign_patient("legacy_race_cycle", target_pid)
                    with lock:
                        successes.append(assigned.patient_id if assigned else None)
                except CycleOwnershipConflictError as exc:
                    with lock:
                        conflicts.append(str(exc))
                finally:
                    ws.close()

            t1 = threading.Thread(target=_assign_worker, args=(sp1_id,))
            t2 = threading.Thread(target=_assign_worker, args=(sp2_id,))
            t1.start()
            t2.start()
            t1.join(timeout=10.0)
            t2.join(timeout=10.0)

            assert len(successes) == 1
            assert len(conflicts) == 1
        finally:
            engine.dispose()
            gc.collect()


def test_15_patient_device_and_refresh_token_rotation_and_reuse_detection(db_session: Session):
    """
    Test 15: Verifies Patient attributes, Device FK & revocation cascade to active refresh tokens,
    and DeviceRefreshToken rotation with automatic family-wide reuse detection.
    """
    patient_repo = PatientRepository(db_session)
    device_repo = DeviceRepository(db_session)
    token_repo = DeviceTokenRepository(db_session)

    patient = patient_repo.create()
    device = device_repo.register_device(patient_id=patient.patient_id, device_name="Galaxy Watch 6")

    raw_tok1, tok1 = token_repo.issue_refresh_token(
        device_id=device.device_id,
        raw_token_override="raw_secret_refresh_token_v1",
    )
    assert raw_tok1 not in tok1.token_hash
    assert tok1.is_active is True
    family_id = tok1.family_id

    # Rotate v1 -> v2
    ok, _, result = token_repo.rotate_refresh_token(
        device_id=device.device_id,
        old_raw_token="raw_secret_refresh_token_v1",
        new_raw_token_override="raw_secret_refresh_token_v2",
    )
    assert ok is True
    _, tok2 = result
    assert tok2.family_id == family_id
    assert tok2.is_active is True

    db_session.refresh(tok1)
    assert tok1.is_revoked is True
    assert tok1.replaced_by_token_id == tok2.token_id

    # Reuse of revoked tok1 revokes entire family (including tok2)
    ok_reuse, msg_reuse, _ = token_repo.rotate_refresh_token(
        device_id=device.device_id,
        old_raw_token="raw_secret_refresh_token_v1",
    )
    assert ok_reuse is False
    assert "reuse detected" in msg_reuse.lower()
    db_session.refresh(tok2)
    assert tok2.is_revoked is True

    # Issue a new token family and verify device revocation revokes active tokens
    raw_tok3, tok3 = token_repo.issue_refresh_token(device_id=device.device_id)
    assert tok3.is_active is True
    device_repo.revoke_device(device.device_id)
    db_session.refresh(tok3)
    assert tok3.is_revoked is True
    ok_rev_dev, _, _ = token_repo.rotate_refresh_token(device_id=device.device_id, old_raw_token=raw_tok3)
    assert ok_rev_dev is False


def test_16_twenty_active_codes_isolated_from_wrong_attempts(db_session: Session):
    """
    Test 16 (Stage 5.3A v4):
    - Creates 20 active pairing codes for 20 different patients (max_attempts=5).
    - Submits 5 (and 10) wrong/unknown pairing code attempts.
    - Verifies that NONE of the 20 active pairing procedures are locked, blocked,
      or have their state (attempts_count, active_code_hash, consumed_at, expires_at) modified.
    - Verifies that patients can still successfully consume their own pairing codes afterward.
    """
    p_repo = PatientRepository(db_session)
    secret = "twenty_patients_isolation_secret"
    pairing_repo = PairingRepository(db_session, secret=secret)

    issued = []
    for idx in range(20):
        patient = p_repo.create()
        code_str = f"{100000 + idx:06d}"
        raw_code, entity = pairing_repo.create_code(
            patient_id=patient.patient_id,
            max_attempts=5,
            raw_code_override=code_str,
        )
        issued.append((patient.patient_id, raw_code, entity.code_id, entity.expires_at))

    # Submit 10 wrong/unknown code attempts (>= max_attempts=5)
    for wrong_idx in range(10):
        wrong_code = f"{900000 + wrong_idx:06d}"
        ok, msg, consumed = pairing_repo.verify_and_consume(wrong_code)
        assert ok is False
        assert consumed is None
        assert msg == "Invalid, expired, or rate-limited pairing code"

    # Verify that ALL 20 active pairing codes remain completely untouched and unlocked
    db_session.expire_all()
    for pid, raw_code, code_id, orig_expires_at in issued:
        ent = db_session.get(PairingCode, code_id)
        assert ent is not None
        assert ent.patient_id == pid
        assert ent.attempts_count == 0
        assert ent.is_rate_limited is False
        assert ent.is_consumed is False
        assert ent.is_valid_for_use is True
        assert ent.active_code_hash == ent.code_hash
        assert ent.expires_at == orig_expires_at

    # Verify that every one of the 20 patients can still consume their valid code
    for pid, raw_code, code_id, _ in issued:
        ok, _, consumed = pairing_repo.verify_and_consume(raw_code)
        assert ok is True
        assert consumed is not None
        assert consumed.code_id == code_id
        assert consumed.patient_id == pid
        assert consumed.is_consumed is True


def test_17_verify_and_consume_commit_false_does_not_commit_outer_transaction(db_session: Session):
    """
    Test 17 (Stage 5.3A v4):
    Verifies transactional semantics of verify_and_consume(..., commit=False) and
    pair_device(..., commit=False):
    A verification failure when commit=False MUST NOT unexpectedly commit or rollback
    the caller's outer transaction.
    """
    p_repo = PatientRepository(db_session)
    existing_patient = p_repo.create()
    secret = "outer_tx_semantics_secret"
    pairing_repo = PairingRepository(db_session, secret=secret)

    raw_code, active_entity = pairing_repo.create_code(
        patient_id=existing_patient.patient_id,
        raw_code_override="445566",
    )

    # Start an uncommitted outer unit of work in db_session
    outer_patient = Patient(patient_id="outer_uncommitted_patient_001", status="active")
    db_session.add(outer_patient)
    db_session.flush()

    # 1. Failed verify_and_consume with commit=False must NOT commit outer_patient
    ok, _, consumed = pairing_repo.verify_and_consume("999999", commit=False)
    assert ok is False
    assert consumed is None

    # 2. Failed pair_device with commit=False must NOT commit outer_patient
    ok_pair, _, dev, _ = pairing_repo.pair_device(
        raw_code="999999",
        device_name="Test Device",
        commit=False,
    )
    assert ok_pair is False
    assert dev is None

    # Rollback the outer transaction and verify outer_patient was NOT committed
    db_session.rollback()
    db_session.expire_all()
    assert db_session.get(Patient, "outer_uncommitted_patient_001") is None

    # Active code for existing_patient is still intact and valid
    reloaded = db_session.get(PairingCode, active_entity.code_id)
    assert reloaded is not None
    assert reloaded.is_consumed is False
    assert reloaded.attempts_count == 0
    assert reloaded.active_code_hash == reloaded.code_hash

    # 3. Successful verify_and_consume with commit=False flushes within outer transaction
    # and can still be rolled back by the outer transaction if needed
    outer_patient_2 = Patient(patient_id="outer_uncommitted_patient_002", status="active")
    db_session.add(outer_patient_2)
    db_session.flush()

    ok_valid, _, consumed_valid = pairing_repo.verify_and_consume(raw_code, commit=False)
    assert ok_valid is True
    assert consumed_valid is not None

    # Caller decides to rollback outer transaction -> both outer_patient_2 and consumption rollback!
    db_session.rollback()
    db_session.expire_all()
    assert db_session.get(Patient, "outer_uncommitted_patient_002") is None
    reloaded_after_rb = db_session.get(PairingCode, active_entity.code_id)
    assert reloaded_after_rb is not None
    assert reloaded_after_rb.is_consumed is False
    assert reloaded_after_rb.active_code_hash == reloaded_after_rb.code_hash
