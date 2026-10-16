import logging
from pathlib import Path
from typing import Generator
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session
from app.config import settings
from app.models.base import Base

logger = logging.getLogger("aegis.database")

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


def init_db(db_engine=engine) -> None:
    """
    Initializes database schema cleanly without conflicts:
    - If in-memory, uses Base.metadata.create_all for fast isolated test execution.
    - If file-based SQLite database, runs Alembic upgrade to head, stamping
      the alembic_version table so manual 'alembic upgrade head' commands never collide.
    """
    url_str = str(db_engine.url)
    if ":memory:" in url_str:
        Base.metadata.create_all(bind=db_engine)
        return

    # Locate alembic.ini relative to app package
    alembic_ini_path = Path(__file__).resolve().parent.parent / "alembic.ini"
    if alembic_ini_path.exists():
        try:
            from alembic.config import Config
            from alembic import command
            alembic_cfg = Config(str(alembic_ini_path))
            alembic_cfg.set_main_option("script_location", str(alembic_ini_path.parent / "alembic"))
            alembic_cfg.set_main_option("sqlalchemy.url", url_str)
            command.upgrade(alembic_cfg, "head")
            logger.info("Database schema synchronized via Alembic migrations.")
            return
        except Exception as e:
            logger.warning(f"Alembic auto-upgrade encountered exception ({e}), falling back to create_all.")

    Base.metadata.create_all(bind=db_engine)
