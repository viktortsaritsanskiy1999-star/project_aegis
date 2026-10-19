import logging
import sqlite3
from pathlib import Path
from typing import Any, Dict, Generator, List, Optional, Union
from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.orm import sessionmaker, Session
from app.config import settings
from app.models.base import Base
from app.services.backup_service import create_sqlite_backup, get_sqlite_path

logger = logging.getLogger("aegis.database")


@event.listens_for(Engine, "connect")
def _enable_sqlite_foreign_keys(dbapi_connection, _connection_record) -> None:
    """Enforces referential integrity (PRAGMA foreign_keys=ON) and default busy_timeout on every SQLite connection."""
    if isinstance(dbapi_connection, sqlite3.Connection):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()


# SQLite optimization: allow multithreaded queries in FastAPI
connect_args = {}
if settings.database_url.startswith("sqlite"):
    connect_args["check_same_thread"] = False

engine = create_engine(
    settings.database_url,
    connect_args=connect_args,
    pool_pre_ping=True,
)

SessionLocal = sessionmaker(
    autocommit=False,
    autoflush=False,
    bind=engine,
)


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency to provide a database session per request."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db(
    db_engine: Optional[Engine] = None,
    backup_dir: Optional[Path | str] = None,
    alembic_ini_path: Optional[Path | str] = None,
) -> Optional[Path]:
    """
    Initializes database schema safely:
    - If in-memory SQLite, uses Base.metadata.create_all for fast isolated test execution.
    - If file-based SQLite database already exists and is non-empty, creates an atomic
      pre-migration backup before running Alembic.
    - Runs Alembic upgrade to head and fails fast (raises RuntimeError) if migration fails,
      never masking migration errors with create_all().
    """
    target_engine = db_engine if db_engine is not None else engine
    url_str = str(target_engine.url)
    if ":memory:" in url_str or target_engine.url.database in (None, ":memory:"):
        Base.metadata.create_all(bind=target_engine)
        return None

    if target_engine.url.database:
        db_path: Optional[Path] = Path(target_engine.url.database).resolve()
    else:
        db_path = get_sqlite_path(url_str)

    backup_file: Optional[Path] = None
    if db_path is not None and db_path.exists() and db_path.stat().st_size > 0:
        backup_file = create_sqlite_backup(
            source_db_path=db_path,
            backup_dir=backup_dir,
        )
        logger.info(f"Pre-migration backup created: {backup_file.name}")

    resolved_ini = (
        Path(alembic_ini_path).resolve()
        if alembic_ini_path is not None
        else Path(__file__).resolve().parent.parent / "alembic.ini"
    )
    if not resolved_ini.exists():
        raise RuntimeError(f"Alembic configuration not found at: {resolved_ini}")

    alembic_url = (
        f"sqlite:///{db_path.as_posix()}"
        if db_path is not None and url_str.startswith("sqlite")
        else url_str
    ).replace("%", "%%")

    try:
        from alembic.config import Config
        from alembic import command

        alembic_cfg = Config(str(resolved_ini))
        alembic_cfg.set_main_option("script_location", str(resolved_ini.parent / "alembic"))
        alembic_cfg.set_main_option("sqlalchemy.url", alembic_url)
        command.upgrade(alembic_cfg, "head")
        logger.info("Database schema synchronized via Alembic migrations.")
        return backup_file
    except Exception as e:
        logger.error(f"Alembic migration failed ({e}). Aborting startup to prevent schema corruption.")
        raise RuntimeError(f"Database migration failed via Alembic: {e}") from e


def _query_duplicate_report_versions(
    conn_or_session: Union[Connection, Session],
) -> List[Dict[str, Any]]:
    bind = conn_or_session.get_bind() if isinstance(conn_or_session, Session) else conn_or_session
    inspector = inspect(bind)
    if "model_reports" not in inspector.get_table_names():
        return []

    dup_rows = conn_or_session.execute(
        text(
            """
            SELECT cycle_id, report_type, privacy_level, version, COUNT(*) AS duplicate_count
            FROM model_reports
            GROUP BY cycle_id, report_type, privacy_level, version
            HAVING COUNT(*) > 1
            ORDER BY cycle_id ASC, report_type ASC, privacy_level ASC, version ASC
            """
        )
    ).fetchall()

    results: List[Dict[str, Any]] = []
    for row in dup_rows:
        id_rows = conn_or_session.execute(
            text(
                """
                SELECT report_id
                FROM model_reports
                WHERE cycle_id = :cycle_id
                  AND report_type = :report_type
                  AND privacy_level = :privacy_level
                  AND version = :version
                ORDER BY created_at ASC, report_id ASC
                """
            ),
            {
                "cycle_id": row[0],
                "report_type": row[1],
                "privacy_level": row[2],
                "version": row[3],
            },
        ).fetchall()
        results.append(
            {
                "cycle_id": str(row[0]),
                "report_type": str(row[1]),
                "privacy_level": str(row[2]),
                "version": int(row[3]),
                "duplicate_count": int(row[4]),
                "report_ids": [str(r[0]) for r in id_rows],
            }
        )
    return results


def find_duplicate_report_versions(
    bind_or_session: Union[Engine, Connection, Session],
) -> List[Dict[str, Any]]:
    """
    Executes a strictly read-only inspection on `model_reports` to detect any
    duplicate (cycle_id, report_type, privacy_level, version) combinations.
    Never modifies, merges, or deletes any records.
    """
    if isinstance(bind_or_session, Engine):
        with bind_or_session.connect() as conn:
            return _query_duplicate_report_versions(conn)
    return _query_duplicate_report_versions(bind_or_session)


def assert_no_duplicate_report_versions(
    bind_or_session: Union[Engine, Connection, Session],
) -> None:
    """
    Verifies via read-only query that no duplicate (cycle_id, report_type, privacy_level, version)
    combinations exist in `model_reports`. Raises RuntimeError with diagnostic metadata if found.
    """
    duplicates = find_duplicate_report_versions(bind_or_session)
    if duplicates:
        summary = "; ".join(
            f"(cycle_id='{d['cycle_id']}', report_type='{d['report_type']}', "
            f"privacy_level='{d['privacy_level']}', version={d['version']}, "
            f"count={d['duplicate_count']}, report_ids={d['report_ids']})"
            for d in duplicates
        )
        raise RuntimeError(
            "Historical data integrity check failed: duplicate "
            "(cycle_id, report_type, privacy_level, version) combinations detected in "
            f"model_reports ({len(duplicates)} duplicate group(s)): {summary}. "
            "Automatic deletion or merging of clinical reports is forbidden. "
            "Manual data inspection is required before applying the UNIQUE constraint."
        )
