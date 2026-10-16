import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from app.config import settings

logger = logging.getLogger("aegis.backup")


def get_sqlite_path(database_url: str) -> Optional[Path]:
    """Extracts the local file path from a SQLAlchemy SQLite connection URL."""
    if not database_url.startswith("sqlite:"):
        return None
    # Strip sqlite:/// prefix
    clean_url = database_url.replace("sqlite:///", "")
    if clean_url == ":memory:":
        return None
    return Path(clean_url).resolve()


def create_sqlite_backup(
    source_db_path: Optional[Path | str] = None,
    backup_dir: Optional[Path | str] = None,
) -> Path:
    """
    Creates an atomic online backup of the SQLite database.
    Does NOT log any patient health measurements or personal data.
    """
    if source_db_path is None:
        extracted = get_sqlite_path(settings.database_url)
        if not extracted:
            raise ValueError(f"Cannot perform SQLite backup on non-file database URL: {settings.database_url}")
        src_path = extracted
    else:
        src_path = Path(source_db_path).resolve()

    if not src_path.exists():
        raise FileNotFoundError(f"Source database file not found: {src_path}")

    # Default backup directory
    target_dir = Path(backup_dir) if backup_dir else src_path.parent / "backups"
    target_dir.mkdir(parents=True, exist_ok=True)

    timestamp_str = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    backup_file = target_dir / f"aegis_vault_backup_{timestamp_str}.db"

    # SQLite native online backup API
    src_conn = sqlite3.connect(str(src_path))
    dst_conn = sqlite3.connect(str(backup_file))
    try:
        with dst_conn:
            src_conn.backup(dst_conn, pages=100)
        logger.info(f"SQLite backup successfully created at: {backup_file.name}")
    finally:
        src_conn.close()
        dst_conn.close()

    return backup_file


def restore_sqlite_backup(
    backup_file: Path | str,
    target_db_path: Optional[Path | str] = None,
) -> None:
    """
    Restores target SQLite database from a valid backup file.
    Does NOT log any sensitive health records.
    """
    bk_path = Path(backup_file).resolve()
    if not bk_path.exists():
        raise FileNotFoundError(f"Backup file not found: {bk_path}")

    if target_db_path is None:
        extracted = get_sqlite_path(settings.database_url)
        if not extracted:
            raise ValueError("Target database path must be specified for non-file database URL.")
        dst_path = extracted
    else:
        dst_path = Path(target_db_path).resolve()

    dst_path.parent.mkdir(parents=True, exist_ok=True)

    src_conn = sqlite3.connect(str(bk_path))
    dst_conn = sqlite3.connect(str(dst_path))
    try:
        with dst_conn:
            src_conn.backup(dst_conn, pages=100)
        logger.info(f"Database successfully restored from: {bk_path.name}")
    finally:
        src_conn.close()
        dst_conn.close()
