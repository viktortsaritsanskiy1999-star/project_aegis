import os
import sys

# Strictly isolate test environment before importing application modules
os.environ["AEGIS_ENV"] = "testing"
os.environ["AEGIS_DATABASE_URL"] = "sqlite:///:memory:"

import pytest
from typing import Generator
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session
from sqlalchemy.pool import StaticPool

# Add backend directory to sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.config import settings
from app.database import get_db
from app.models.base import Base
from app.main import app

# In-memory SQLite for isolated test runs
TEST_DATABASE_URL = "sqlite:///:memory:"
TEST_API_SECRET = "test_device_secret_token_12345"

test_engine = create_engine(
    TEST_DATABASE_URL,
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)

# Bind test engine to app.state so FastAPI lifespan never touches production database
app.state.engine = test_engine

TestingSessionLocal = sessionmaker(
    autocommit=False,
    autoflush=False,
    bind=test_engine,
)


@pytest.fixture(scope="function", autouse=True)
def configure_test_settings():
    """Sets deterministic test settings for isolated execution and strictly restores state."""
    original_secret = settings.api_secret
    original_env = settings.env
    original_db_url = settings.database_url
    original_backup_dir = settings.backup_dir
    original_state_engine = getattr(app.state, "engine", None)
    original_state_backup_dir = getattr(app.state, "backup_dir", None)
    original_env_vars = {
        k: os.environ.get(k)
        for k in ("AEGIS_ENV", "AEGIS_DATABASE_URL", "AEGIS_BACKUP_DIR", "AEGIS_API_SECRET")
    }

    settings.api_secret = TEST_API_SECRET
    settings.env = "testing"
    settings.database_url = TEST_DATABASE_URL
    app.state.engine = test_engine

    try:
        yield
    finally:
        settings.api_secret = original_secret
        settings.env = original_env
        settings.database_url = original_db_url
        settings.backup_dir = original_backup_dir
        app.state.engine = original_state_engine
        if original_state_backup_dir is not None:
            app.state.backup_dir = original_state_backup_dir
        elif hasattr(app.state, "backup_dir"):
            delattr(app.state, "backup_dir")

        for k, v in original_env_vars.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


@pytest.fixture(scope="function")
def auth_headers() -> dict[str, str]:
    """Provides valid Bearer authentication headers for test client."""
    return {"Authorization": f"Bearer {TEST_API_SECRET}"}


@pytest.fixture(scope="function")
def db_session() -> Generator[Session, None, None]:
    """Provides a clean in-memory database session for each test function."""
    Base.metadata.create_all(bind=test_engine)
    session = TestingSessionLocal()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(bind=test_engine)


@pytest.fixture(scope="function")
def client(db_session: Session) -> Generator[TestClient, None, None]:
    """TestClient fixture overriding get_db dependency to use test database session."""
    def override_get_db():
        try:
            yield db_session
        finally:
            pass

    app.dependency_overrides[get_db] = override_get_db
    app.state.engine = test_engine
    try:
        with TestClient(app) as test_client:
            yield test_client
    finally:
        app.dependency_overrides.clear()
