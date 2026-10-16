import os
import sys
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

TestingSessionLocal = sessionmaker(
    autocommit=False,
    autoflush=False,
    bind=test_engine,
)


@pytest.fixture(scope="function", autouse=True)
def configure_test_settings():
    """Sets a deterministic non-empty test secret for authentication tests."""
    original_secret = settings.api_secret
    settings.api_secret = TEST_API_SECRET
    yield
    settings.api_secret = original_secret


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
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()
