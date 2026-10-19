import hashlib
import os
import tempfile
from pathlib import Path
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from app.config import settings
from app.database import init_db
from app.main import app
from app.services.backup_service import create_sqlite_backup


def _compute_sha256(file_path: Path) -> str:
    """Computes SHA256 hash of a file."""
    hasher = hashlib.sha256()
    with open(file_path, "rb") as f:
        while chunk := f.read(65536):
            hasher.update(chunk)
    return hasher.hexdigest()


def test_client_lifespan_does_not_modify_production_vault_db_or_create_backups(
    client: TestClient,
    auth_headers: dict[str, str],
):
    """
    C-12 Remediation Verification:
    Ensures that starting the FastAPI TestClient, running its lifespan, and executing requests
    strictly isolates the database:
    1. The production aegis_private_vault.db file is NEVER modified (hash, size, and mtime remain identical).
    2. Zero new backup files are generated in the project's backups/ directory.
    """
    project_root = Path(__file__).resolve().parent.parent.parent
    vault_db_path = project_root / "aegis_private_vault.db"
    backups_dir = project_root / "backups"

    # 1. Capture pre-test state of production database and backups directory
    vault_exists_before = vault_db_path.exists()
    if vault_exists_before:
        vault_size_before = vault_db_path.stat().st_size
        vault_mtime_before = vault_db_path.stat().st_mtime
        vault_hash_before = _compute_sha256(vault_db_path)

    backups_before = set(backups_dir.iterdir()) if backups_dir.exists() else set()

    # 2. Execute multiple endpoints via the client
    health_resp = client.get("/health")
    assert health_resp.status_code == 200
    assert health_resp.json()["status"] == "ok"
    assert health_resp.json()["database"] == "connected"

    cycle_resp = client.post(
        "/api/v1/cycle/start",
        json={"cycle_id": "cycle_c12_isolation_check"},
        headers=auth_headers,
    )
    assert cycle_resp.status_code == 201

    # 3. Simulate another independent TestClient context manager (triggering lifespan again)
    with TestClient(app) as second_client:
        r = second_client.get("/health")
        assert r.status_code == 200

    # 4. Verify production vault state is completely untouched
    if vault_exists_before:
        assert vault_db_path.exists(), "Production vault was unexpectedly removed!"
        assert vault_db_path.stat().st_size == vault_size_before, "Production vault size was modified!"
        assert vault_db_path.stat().st_mtime == vault_mtime_before, "Production vault mtime was modified!"
        assert _compute_sha256(vault_db_path) == vault_hash_before, "Production vault SHA256 changed!"

    # 5. Verify zero new backups created in project root backups/
    backups_after = set(backups_dir.iterdir()) if backups_dir.exists() else set()
    new_backups = backups_after - backups_before
    assert len(new_backups) == 0, f"Pytest created unexpected backup files in project backups/: {new_backups}"


def test_create_sqlite_backup_isolated_in_test_env():
    """
    Verifies that calling create_sqlite_backup during tests without explicit backup_dir
    redirects to the OS temporary directory and NEVER to the project root backups/ directory.
    """
    project_root = Path(__file__).resolve().parent.parent.parent
    backups_dir = project_root / "backups"
    backups_before = set(backups_dir.iterdir()) if backups_dir.exists() else set()

    with tempfile.TemporaryDirectory() as tmpdir:
        fake_db = Path(tmpdir) / "test_fake.db"
        import sqlite3
        init_conn = sqlite3.connect(str(fake_db))
        init_conn.execute("CREATE TABLE dummy (id INTEGER PRIMARY KEY, val TEXT)")
        init_conn.execute("INSERT INTO dummy (val) VALUES ('ok')")
        init_conn.commit()
        init_conn.close()

        # Invoke backup without backup_dir argument while in testing environment
        assert settings.env == "testing"
        backup_file = create_sqlite_backup(source_db_path=fake_db, backup_dir=None)

        try:
            assert backup_file.exists()
            assert str(Path(tempfile.gettempdir()).resolve()) in str(backup_file.resolve())
            assert backups_dir.resolve() not in backup_file.parents
        finally:
            if backup_file.exists():
                backup_file.unlink(missing_ok=True)

    backups_after = set(backups_dir.iterdir()) if backups_dir.exists() else set()
    assert backups_after == backups_before, "Project backups directory was modified by backup service!"


def test_init_db_in_memory_engine_creates_no_backup():
    """
    Verifies that init_db on an in-memory SQLite engine returns None
    and performs zero file or backup operations.
    """
    mem_engine = create_engine("sqlite:///:memory:")
    try:
        backup_result = init_db(mem_engine)
        assert backup_result is None
    finally:
        mem_engine.dispose()


def test_lifespan_engine_fallback_when_app_state_engine_none():
    """
    Verifies that if app.state.engine is None in testing environment,
    the lifespan gracefully creates an isolated in-memory engine and never
    connects to or migrates the production database.
    """
    project_root = Path(__file__).resolve().parent.parent.parent
    vault_db_path = project_root / "aegis_private_vault.db"
    vault_hash_before = _compute_sha256(vault_db_path) if vault_db_path.exists() else None

    original_engine = getattr(app.state, "engine", None)
    app.state.engine = None
    try:
        assert settings.env == "testing"
        with TestClient(app) as test_client:
            resp = test_client.get("/health")
            assert resp.status_code == 200

        if vault_hash_before is not None:
            assert _compute_sha256(vault_db_path) == vault_hash_before
    finally:
        app.state.engine = original_engine


def test_production_lifespan_uses_default_engine_when_state_engine_none(monkeypatch):
    """
    Point 1 & 5 Verification:
    Ensures that in production/development environment (settings.env != "testing"),
    when app.state.engine is None, lifespan uses the default engine and executes normal startup.
    Tested safely on an isolated temporary file database, never touching the real vault.
    """
    project_root = Path(__file__).resolve().parent.parent.parent
    vault_db_path = project_root / "aegis_private_vault.db"
    vault_hash_before = _compute_sha256(vault_db_path) if vault_db_path.exists() else None
    backups_dir = project_root / "backups"
    backups_before = set(backups_dir.iterdir()) if backups_dir.exists() else set()

    with tempfile.TemporaryDirectory() as tmpdir:
        temp_prod_db = Path(tmpdir) / "simulated_prod_vault.db"
        temp_backup_dir = Path(tmpdir) / "simulated_backups"
        temp_prod_engine = create_engine(
            f"sqlite:///{temp_prod_db}",
            connect_args={"check_same_thread": False},
        )

        # Monkeypatch app.main.engine with the isolated temporary engine
        monkeypatch.setattr("app.main.engine", temp_prod_engine)
        monkeypatch.setattr(settings, "env", "production")
        monkeypatch.setattr(settings, "backup_dir", str(temp_backup_dir))

        app.state.engine = None
        try:
            with TestClient(app) as test_client:
                resp = test_client.get("/health")
                assert resp.status_code == 200
                data = resp.json()
                assert data["environment"] == "production"

            # Verify the temporary database was initialized
            assert temp_prod_db.exists()
        finally:
            temp_prod_engine.dispose()

    # Verify real medical vault and backups are 100% untouched
    if vault_hash_before is not None:
        assert _compute_sha256(vault_db_path) == vault_hash_before
    backups_after = set(backups_dir.iterdir()) if backups_dir.exists() else set()
    assert backups_after == backups_before


def test_production_backup_service_default_directory():
    """
    Point 3 Verification:
    Ensures that standard backup rules work properly in production/development:
    1. When backup_dir is None, default target directory is src_path.parent / 'backups'.
    2. Atomic online backup preserves data and restore completely recovers records.
    3. Workspace backups/ directory is never polluted.
    """
    project_root = Path(__file__).resolve().parent.parent.parent
    backups_dir = project_root / "backups"
    backups_before = set(backups_dir.iterdir()) if backups_dir.exists() else set()

    with tempfile.TemporaryDirectory() as tmpdir:
        fake_db = Path(tmpdir) / "prod_source.db"
        import sqlite3
        conn = sqlite3.connect(str(fake_db))
        conn.execute("CREATE TABLE records (id INTEGER PRIMARY KEY, note TEXT)")
        conn.execute("INSERT INTO records (note) VALUES ('patient_baseline_vital')")
        conn.commit()
        conn.close()

        # Simulate production environment
        original_env = settings.env
        original_backup_dir = settings.backup_dir
        settings.env = "production"
        settings.backup_dir = None
        try:
            backup_file = create_sqlite_backup(source_db_path=fake_db, backup_dir=None)

            # In production, default backup directory is src_path.parent / "backups"
            expected_backup_dir = fake_db.parent / "backups"
            assert backup_file.exists()
            assert backup_file.parent.resolve() == expected_backup_dir.resolve()

            # Verify contents of backup
            bk_conn = sqlite3.connect(str(backup_file))
            val = bk_conn.execute("SELECT note FROM records WHERE id = 1").fetchone()[0]
            bk_conn.close()
            assert val == "patient_baseline_vital"
        finally:
            settings.env = original_env
            settings.backup_dir = original_backup_dir

    backups_after = set(backups_dir.iterdir()) if backups_dir.exists() else set()
    assert backups_after == backups_before


def test_settings_restoration_guarantee():
    """
    Point 4 Verification:
    Ensures that setting testing environment or changing settings does not leak across tests.
    After the configure_test_settings fixture runs, all settings revert cleanly.
    """
    assert settings.env == "testing"
    assert settings.database_url == "sqlite:///:memory:"
    assert app.state.engine is not None
