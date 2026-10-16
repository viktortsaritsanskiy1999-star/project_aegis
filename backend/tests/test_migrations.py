import tempfile
from pathlib import Path
from alembic.config import Config
from alembic import command
from sqlalchemy import create_engine, inspect


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
