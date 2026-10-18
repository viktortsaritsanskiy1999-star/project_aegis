import tempfile
from pathlib import Path
from unittest.mock import patch
import pytest
from alembic.config import Config
from alembic import command
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from app.database import init_db
from app.models import MedicalCycle, WeeklySprint


def test_alembic_migrations_apply_cleanly_on_fresh_database():
    """
    Verifies that Alembic versioned migrations can be executed from scratch
    on a completely fresh database without any collisions or errors.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        test_db_path = Path(tmpdir) / "fresh_migration_test.db"
        test_db_url = f"sqlite:///{test_db_path}"

        alembic_ini_path = Path(__file__).resolve().parent.parent / "alembic.ini"
        assert alembic_ini_path.exists(), "alembic.ini not found"

        alembic_cfg = Config(str(alembic_ini_path))
        alembic_cfg.set_main_option("script_location", str(alembic_ini_path.parent / "alembic"))
        alembic_cfg.set_main_option("sqlalchemy.url", test_db_url)

        # 1. Run migrations upgrade to head
        command.upgrade(alembic_cfg, "head")

        # 2. Inspect created tables
        engine = create_engine(test_db_url)
        try:
            inspector = inspect(engine)
            table_names = inspector.get_table_names()
        finally:
            engine.dispose()

        expected_tables = {
            "medical_cycles",
            "weekly_sprints",
            "workout_sessions",
            "health_measurements",
            "model_reports",
            "alembic_version",
        }

        assert expected_tables.issubset(set(table_names)), (
            f"Missing tables in migrated database. Found: {table_names}"
        )


def test_sqlite_pragma_foreign_keys_is_enforced():
    """
    Verifies that PRAGMA foreign_keys=ON is automatically enabled on SQLite connections
    and rejects orphan child rows with IntegrityError.
    """
    engine = create_engine("sqlite:///:memory:")
    init_db(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    try:
        fk_enabled = session.execute(text("PRAGMA foreign_keys")).scalar()
        assert fk_enabled == 1

        # Attempting to insert a WeeklySprint referencing a non-existent MedicalCycle must fail
        orphan_sprint = WeeklySprint(
            sprint_id="sprint_orphan_01",
            cycle_id="non_existent_cycle_999",
            sprint_number=1,
            status="active",
        )
        session.add(orphan_sprint)
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()
    finally:
        session.close()
        engine.dispose()


def test_init_db_creates_backup_on_existing_db_and_fails_fast_on_alembic_error():
    """
    Verifies that:
    1. First initialization on a fresh file DB runs Alembic cleanly (no backup needed yet).
    2. Subsequent init_db on a populated file DB creates a pre-migration backup before running Alembic.
    3. If Alembic command.upgrade fails, init_db raises RuntimeError (no fallback to create_all)
       and the pre-migration backup with existing user data remains intact.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        db_file = tmp_path / "existing_aegis.db"
        backup_dir = tmp_path / "backups"
        db_url = f"sqlite:///{db_file}"

        engine = create_engine(db_url)
        try:
            # 1. Fresh DB initialization
            first_backup = init_db(engine, backup_dir=backup_dir)
            assert first_backup is None
            assert db_file.exists() and db_file.stat().st_size > 0

            # Seed a MedicalCycle record
            Session = sessionmaker(bind=engine)
            s = Session()
            s.add(MedicalCycle(cycle_id="cycle_preserved_before_fail", status="active"))
            s.commit()
            s.close()
        finally:
            engine.dispose()

        # 2. Simulate an Alembic upgrade failure on the existing populated database
        engine2 = create_engine(db_url)
        try:
            with patch("alembic.command.upgrade", side_effect=Exception("Simulated migration conflict")):
                with pytest.raises(RuntimeError) as excinfo:
                    init_db(engine2, backup_dir=backup_dir)
                assert "Database migration failed via Alembic" in str(excinfo.value)
        finally:
            engine2.dispose()

        # 3. Verify that the pre-migration backup was created and contains the seeded cycle
        backups = list(backup_dir.glob("aegis_vault_backup_*.db"))
        assert len(backups) == 1
        backup_engine = create_engine(f"sqlite:///{backups[0]}")
        try:
            BackupSession = sessionmaker(bind=backup_engine)
            bs = BackupSession()
            recovered = bs.query(MedicalCycle).filter_by(cycle_id="cycle_preserved_before_fail").one_or_none()
            assert recovered is not None
            assert recovered.status == "active"
            bs.close()
        finally:
            backup_engine.dispose()
