import logging
import sqlite3
from pathlib import Path
from typing import Generator, Optional
from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker, Session
from app.config import settings
from app.models.base import Base
from app.services.backup_service import create_sqlite_backup, get_sqlite_path

logger = logging.getLogger("aegis.database")


@event.listens_for(Engine, "connect")
def _enable_sqlite_foreign_keys(dbapi_connection, _connection_record) -> None:
    """Enforces referential integrity (PRAGMA foreign_keys=ON) on every SQLite connection."""
    if isinstance(dbapi_connection, sqlite3.Connection):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
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
