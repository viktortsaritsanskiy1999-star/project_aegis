import tempfile
from pathlib import Path
from datetime import datetime, timezone
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app.models.base import Base
from app.models import MedicalCycle, HealthMeasurement
from app.services.backup_service import create_sqlite_backup, restore_sqlite_backup


def test_sqlite_backup_and_restore_preserves_database_content():
    """
    Verifies that:
    1. Online SQLite backup captures all tables and records.
    2. Restoring from the backup into a fresh target DB completely recovers the data.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        source_db_path = tmp_path / "origin.db"
        backup_dir = tmp_path / "backups"
        restored_db_path = tmp_path / "restored.db"

        # 1. Setup source DB and populate with data
        engine_src = create_engine(f"sqlite:///{source_db_path}")
        Base.metadata.create_all(bind=engine_src)
        SessionSrc = sessionmaker(bind=engine_src)
        s_src = SessionSrc()

        now = datetime.now(timezone.utc)
        cycle = MedicalCycle(cycle_id="cycle_backup_eval", status="active", started_at=now)
        s_src.add(cycle)

        meas = HealthMeasurement(
            cycle_id=cycle.cycle_id,
            pulse=78,
            source="watch_sensor",
            measured_at=now,
            ingested_at=now,
        )
        s_src.add(meas)
        s_src.commit()
        s_src.close()
        engine_src.dispose()

        # 2. Perform backup
        backup_file = create_sqlite_backup(source_db_path=source_db_path, backup_dir=backup_dir)
        assert backup_file.exists()
        assert backup_file.stat().st_size > 0

        # 3. Restore to a new file
        restore_sqlite_backup(backup_file=backup_file, target_db_path=restored_db_path)
        assert restored_db_path.exists()

        # 4. Connect to restored DB and verify contents
        engine_restored = create_engine(f"sqlite:///{restored_db_path}")
        SessionRestored = sessionmaker(bind=engine_restored)
        s_rest = SessionRestored()

        recovered_cycle = s_rest.query(MedicalCycle).filter_by(cycle_id="cycle_backup_eval").one()
        assert recovered_cycle.status == "active"

        recovered_meas = s_rest.query(HealthMeasurement).filter_by(cycle_id="cycle_backup_eval").one()
        assert recovered_meas.pulse == 78
        assert recovered_meas.source == "watch_sensor"

        s_rest.close()
        engine_restored.dispose()
