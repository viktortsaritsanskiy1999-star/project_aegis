from datetime import date, datetime, timezone
from pathlib import Path
import sqlite3
import threading
from typing import Any, Dict, List

from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

import app.models  # noqa: F401
from app.database import (
    Base,
    assert_no_duplicate_report_versions,
    find_duplicate_report_versions,
    get_db,
)
from app.main import app
from app.models.medical_cycle import MedicalCycle
from app.models.model_report import ModelReport
from app.repositories.report_repository import (
    ModelReportRepository,
    ReportVersionConflictError,
)
from app.schemas.consilium import LabEventType, LabMarkerMutationItem
from app.schemas.privacy import PrivacyLevel
from app.security.auth import verify_client_token
from app.services.lab_revision_consilium_service import (
    LABORATORY_LEDGER_REPORT_TYPE,
    LabRevisionConsiliumService,
)


BACKEND_DIR = Path(__file__).resolve().parents[1]


def _make_alembic_config(sqlite_url: str) -> Config:
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    cfg.set_main_option("sqlalchemy.url", sqlite_url)
    return cfg


def _build_file_engine(sqlite_url: str, busy_timeout_ms: int = 5000):
    engine = create_engine(
        sqlite_url,
        connect_args={"check_same_thread": False, "timeout": busy_timeout_ms / 1000.0},
        pool_pre_ping=True,
    )

    @event.listens_for(engine, "connect")
    def _set_custom_busy_timeout(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)};")
        cursor.close()

    return engine


def _create_file_session_factory(db_path: Path, busy_timeout_ms: int = 5000):
    sqlite_url = f"sqlite:///{db_path}"
    engine = _build_file_engine(sqlite_url, busy_timeout_ms=busy_timeout_ms)
    Base.metadata.create_all(bind=engine)
    session_factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    return engine, session_factory


def _seed_cycle(session: Session, cycle_id: str) -> MedicalCycle:
    now = datetime.now(timezone.utc)
    cycle = MedicalCycle(
        cycle_id=cycle_id,
        status="active",
        started_at=now,
        created_at=now,
        updated_at=now,
    )
    session.add(cycle)
    session.commit()
    return cycle


def test_migration_0002_clean_and_populated_temp_sqlite_db(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """
    Verifies migration 0002_model_reports_version_unique:
    1. Upgrades cleanly from 0001_initial_schema on a populated temporary SQLite DB
       containing a long UUID cycle_id and existing version history.
    2. Preserves all pre-existing historical rows without modification.
    3. Enforces UNIQUE(cycle_id, report_type, privacy_level, version) at the SQLite schema level.
    """
    db_path = tmp_path / "migration_valid.db"
    sqlite_url = f"sqlite:///{db_path}"
    monkeypatch.setenv("DATABASE_URL", sqlite_url)
    cfg = _make_alembic_config(sqlite_url)

    command.upgrade(cfg, "0001_initial_schema")

    uuid_cycle_id = "cycle_550e8400-e29b-41d4-a716-446655440000"
    now_str = datetime.now(timezone.utc).isoformat()
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("PRAGMA foreign_keys=ON;")
        conn.execute(
            "INSERT INTO medical_cycles (cycle_id, status, started_at, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (uuid_cycle_id, "active", now_str, now_str, now_str),
        )
        conn.execute(
            "INSERT INTO model_reports "
            "(report_id, cycle_id, sprint_id, model, report_type, privacy_level, version, content, created_at) "
            "VALUES (?, ?, NULL, ?, ?, ?, ?, ?, ?)",
            (
                "rep_hist_v1",
                uuid_cycle_id,
                "gemini-2.5-pro",
                "chief_physician_internal",
                "server_only",
                1,
                '{"note": "historical_v1"}',
                now_str,
            ),
        )
        conn.execute(
            "INSERT INTO model_reports "
            "(report_id, cycle_id, sprint_id, model, report_type, privacy_level, version, content, created_at) "
            "VALUES (?, ?, NULL, ?, ?, ?, ?, ?, ?)",
            (
                "rep_hist_v2",
                uuid_cycle_id,
                "gemini-2.5-pro",
                "chief_physician_internal",
                "server_only",
                2,
                '{"note": "historical_v2"}',
                now_str,
            ),
        )
        conn.commit()
    finally:
        conn.close()

    engine = _build_file_engine(sqlite_url)
    try:
        assert find_duplicate_report_versions(engine) == []
        assert_no_duplicate_report_versions(engine)
    finally:
        engine.dispose()

    command.upgrade(cfg, "head")

    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute(
            "SELECT report_id, version, content FROM model_reports "
            "WHERE cycle_id = ? ORDER BY version ASC",
            (uuid_cycle_id,),
        ).fetchall()
        assert len(rows) == 2
        assert rows[0][0] == "rep_hist_v1" and rows[0][1] == 1
        assert rows[1][0] == "rep_hist_v2" and rows[1][1] == 2

        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO model_reports "
                "(report_id, cycle_id, sprint_id, model, report_type, privacy_level, version, content, created_at) "
                "VALUES (?, ?, NULL, ?, ?, ?, ?, ?, ?)",
                (
                    "rep_duplicate_v2_different_pk",
                    uuid_cycle_id,
                    "gemini-2.5-pro",
                    "chief_physician_internal",
                    "server_only",
                    2,
                    '{"note": "duplicate_attempt"}',
                    now_str,
                ),
            )
    finally:
        conn.close()


def test_migration_0002_stops_safely_on_artificial_duplicates_without_data_loss(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """
    Verifies that if historical duplicate (cycle_id, report_type, privacy_level, version)
    rows exist prior to migration 0002:
    1. `find_duplicate_report_versions` detects them in read-only mode.
    2. `alembic upgrade head` aborts with a clear diagnostic RuntimeError listing the duplicates.
    3. Neither duplicate row is deleted, merged, or modified.
    """
    db_path = tmp_path / "migration_with_duplicates.db"
    sqlite_url = f"sqlite:///{db_path}"
    monkeypatch.setenv("DATABASE_URL", sqlite_url)
    cfg = _make_alembic_config(sqlite_url)

    command.upgrade(cfg, "0001_initial_schema")

    uuid_cycle_id = "cycle_9b1deb4d-3b7d-4bad-9bdd-2b0d7b3dcb6d"
    now_str = datetime.now(timezone.utc).isoformat()
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "INSERT INTO medical_cycles (cycle_id, status, started_at, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (uuid_cycle_id, "active", now_str, now_str, now_str),
        )
        conn.execute(
            "INSERT INTO model_reports "
            "(report_id, cycle_id, sprint_id, model, report_type, privacy_level, version, content, created_at) "
            "VALUES (?, ?, NULL, ?, ?, ?, ?, ?, ?)",
            (
                "rep_uuid_dup_first",
                uuid_cycle_id,
                "aegis-lab-ledger",
                "laboratory_ledger",
                "server_only",
                1,
                '{"marker": "ferritin_first"}',
                now_str,
            ),
        )
        conn.execute(
            "INSERT INTO model_reports "
            "(report_id, cycle_id, sprint_id, model, report_type, privacy_level, version, content, created_at) "
            "VALUES (?, ?, NULL, ?, ?, ?, ?, ?, ?)",
            (
                "rep_uuid_dup_second",
                uuid_cycle_id,
                "aegis-lab-ledger",
                "laboratory_ledger",
                "server_only",
                1,
                '{"marker": "vitamin_d_second"}',
                now_str,
            ),
        )
        conn.commit()
    finally:
        conn.close()

    engine = _build_file_engine(sqlite_url)
    try:
        duplicates = find_duplicate_report_versions(engine)
        assert len(duplicates) == 1
        assert duplicates[0]["cycle_id"] == uuid_cycle_id
        assert duplicates[0]["report_type"] == "laboratory_ledger"
        assert duplicates[0]["privacy_level"] == "server_only"
        assert duplicates[0]["version"] == 1
        assert duplicates[0]["duplicate_count"] == 2
        assert set(duplicates[0]["report_ids"]) == {
            "rep_uuid_dup_first",
            "rep_uuid_dup_second",
        }
    finally:
        engine.dispose()

    with pytest.raises(RuntimeError) as exc_info:
        command.upgrade(cfg, "head")

    err_msg = str(exc_info.value)
    assert uuid_cycle_id in err_msg
    assert "laboratory_ledger" in err_msg
    assert "rep_uuid_dup_first" in err_msg
    assert "rep_uuid_dup_second" in err_msg

    # Verify both historical rows remain completely untouched
    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute(
            "SELECT report_id, content FROM model_reports WHERE cycle_id = ? ORDER BY report_id ASC",
            (uuid_cycle_id,),
        ).fetchall()
        assert rows == [
            ("rep_uuid_dup_first", '{"marker": "ferritin_first"}'),
            ("rep_uuid_dup_second", '{"marker": "vitamin_d_second"}'),
        ]
    finally:
        conn.close()


def test_deterministic_report_id_for_long_uuid_cycle_id():
    uuid_cycle_id = "cycle_550e8400-e29b-41d4-a716-446655440000"
    id_a = ModelReportRepository.build_deterministic_report_id(
        cycle_id=uuid_cycle_id,
        report_type="deputy_chief_physician_internal",
        privacy_level=PrivacyLevel.SERVER_ONLY,
        version=3,
    )
    id_b = ModelReportRepository.build_deterministic_report_id(
        cycle_id=uuid_cycle_id,
        report_type="deputy_chief_physician_internal",
        privacy_level=PrivacyLevel.SERVER_ONLY,
        version=3,
    )
    id_client = ModelReportRepository.build_deterministic_report_id(
        cycle_id=uuid_cycle_id,
        report_type="deputy_chief_physician_internal",
        privacy_level=PrivacyLevel.CLIENT_FACING,
        version=3,
    )
    assert id_a == id_b
    assert len(id_a) <= 64
    assert id_a.endswith("_v3")
    assert id_a != id_client


def test_concurrent_same_version_creation_with_uuid_cycle_id_and_different_payloads(
    tmp_path: Path,
):
    """
    Two concurrent threads on separate SQLite sessions attempt to create the exact same
    target version (v2) for a long UUID cycle_id with different payloads while v1 already exists:
    - Exactly 1 succeeds and 1 fails with ReportVersionConflictError.
    - No duplicate (cycle_id, report_type, privacy_level, version) rows are created.
    - The winning v2 report is not overwritten, the losing session rolls back cleanly,
      and pre-existing v1 history is preserved.
    """
    db_path = tmp_path / "concurrent_same_version.db"
    engine, SessionFactory = _create_file_session_factory(db_path)
    uuid_cycle_id = "cycle_f47ac10b-58cc-4372-a567-0e02b2c3d479"

    try:
        with SessionFactory() as setup_session:
            _seed_cycle(setup_session, uuid_cycle_id)
            repo = ModelReportRepository(setup_session)
            v1 = repo.create_next_version(
                cycle_id=uuid_cycle_id,
                model="gemini-2.5-pro",
                report_type="chief_physician_internal",
                privacy_level=PrivacyLevel.SERVER_ONLY,
                content={"marker": "baseline_v1_preserved"},
            )
            assert v1.version == 1

        barrier = threading.Barrier(2)
        results: List[Dict[str, Any]] = []
        results_lock = threading.Lock()

        def _worker(worker_name: str, payload: Dict[str, Any]):
            session = SessionFactory()
            try:
                repo = ModelReportRepository(session)
                # Both sessions read latest version (v1) before the barrier
                latest = repo.get_latest_version_number(
                    cycle_id=uuid_cycle_id,
                    report_type="chief_physician_internal",
                    privacy_level=PrivacyLevel.SERVER_ONLY,
                )
                assert latest == 1
                barrier.wait(timeout=5.0)

                created = repo.create_next_version(
                    cycle_id=uuid_cycle_id,
                    model="gemini-2.5-pro",
                    report_type="chief_physician_internal",
                    privacy_level=PrivacyLevel.SERVER_ONLY,
                    content=payload,
                    expected_version=2,
                    max_retries=0,
                )
                with results_lock:
                    results.append(
                        {
                            "worker": worker_name,
                            "status": "ok",
                            "version": created.version,
                            "payload": payload,
                        }
                    )
            except ReportVersionConflictError as exc:
                # Verify session was cleanly rolled back and remains usable
                existing_count = (
                    session.query(ModelReport)
                    .filter(ModelReport.cycle_id == uuid_cycle_id)
                    .count()
                )
                with results_lock:
                    results.append(
                        {
                            "worker": worker_name,
                            "status": "conflict",
                            "error": str(exc),
                            "session_usable_count": existing_count,
                        }
                    )
            finally:
                session.close()

        t1 = threading.Thread(
            target=_worker,
            args=("worker_alpha", {"author": "alpha", "note": "payload_alpha"}),
        )
        t2 = threading.Thread(
            target=_worker,
            args=("worker_beta", {"author": "beta", "note": "payload_beta"}),
        )
        t1.start()
        t2.start()
        t1.join(timeout=10.0)
        t2.join(timeout=10.0)

        assert len(results) == 2
        successes = [r for r in results if r["status"] == "ok"]
        conflicts = [r for r in results if r["status"] == "conflict"]
        assert len(successes) == 1
        assert len(conflicts) == 1
        assert conflicts[0]["session_usable_count"] >= 1

        with SessionFactory() as verify_session:
            assert find_duplicate_report_versions(verify_session) == []
            versions = ModelReportRepository(verify_session).list_versions(
                cycle_id=uuid_cycle_id,
                report_type="chief_physician_internal",
                privacy_level=PrivacyLevel.SERVER_ONLY,
            )
            assert [v.version for v in versions] == [1, 2]
            assert versions[0].content == {"marker": "baseline_v1_preserved"}
            assert versions[1].content == successes[0]["payload"]
    finally:
        engine.dispose()


def test_concurrent_create_next_version_auto_retry_preserves_all_different_payloads(
    tmp_path: Path,
):
    """
    Four concurrent threads on separate SQLite sessions simultaneously read v1 as the
    latest version (synchronized via a barrier inside `get_latest_version_number` on their
    first read) and call `create_next_version(..., commit=True)` with distinct payloads.
    Verifies bounded retry logic assigns versions v2, v3, v4, v5 with zero lost payloads
    and zero duplicate composite versions.
    """
    db_path = tmp_path / "concurrent_next_version_retry.db"
    engine, SessionFactory = _create_file_session_factory(db_path)
    uuid_cycle_id = "cycle_a1b2c3d4-e5f6-4a5b-8c9d-0e1f2a3b4c5d"

    try:
        with SessionFactory() as setup_session:
            _seed_cycle(setup_session, uuid_cycle_id)
            repo = ModelReportRepository(setup_session)
            v1 = repo.create_next_version(
                cycle_id=uuid_cycle_id,
                model="gemini-2.5-flash",
                report_type="weekly_deputy_internal",
                privacy_level=PrivacyLevel.SERVER_ONLY,
                content={"origin": "pre_existing_v1"},
            )
            assert v1.version == 1

        worker_count = 4
        barrier = threading.Barrier(worker_count)
        saved_versions: List[int] = []
        errors: List[Exception] = []
        lock = threading.Lock()

        def _worker(worker_idx: int):
            session = SessionFactory()
            try:
                repo = ModelReportRepository(session)
                orig_get_latest = repo.get_latest_version_number
                first_call = True

                def _synced_get_latest(*args, **kwargs) -> int:
                    nonlocal first_call
                    ver = orig_get_latest(*args, **kwargs)
                    if first_call:
                        first_call = False
                        barrier.wait(timeout=5.0)
                    return ver

                repo.get_latest_version_number = _synced_get_latest  # type: ignore[method-assign]

                report = repo.create_next_version(
                    cycle_id=uuid_cycle_id,
                    model="gemini-2.5-flash",
                    report_type="weekly_deputy_internal",
                    privacy_level=PrivacyLevel.SERVER_ONLY,
                    content={
                        "worker_idx": worker_idx,
                        "distinct_payload": f"clinical_synthesis_{worker_idx}",
                    },
                    max_retries=6,
                )
                with lock:
                    saved_versions.append(report.version)
            except Exception as exc:
                with lock:
                    errors.append(exc)
            finally:
                session.close()

        threads = [
            threading.Thread(target=_worker, args=(idx,))
            for idx in range(1, worker_count + 1)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15.0)

        assert errors == []
        assert sorted(saved_versions) == [2, 3, 4, 5]

        with SessionFactory() as verify_session:
            assert find_duplicate_report_versions(verify_session) == []
            versions = ModelReportRepository(verify_session).list_versions(
                cycle_id=uuid_cycle_id,
                report_type="weekly_deputy_internal",
                privacy_level=PrivacyLevel.SERVER_ONLY,
            )
            assert [v.version for v in versions] == [1, 2, 3, 4, 5]
            assert versions[0].content == {"origin": "pre_existing_v1"}
            recorded_payloads = {
                v.content["distinct_payload"] for v in versions[1:]
            }
            assert recorded_payloads == {
                "clinical_synthesis_1",
                "clinical_synthesis_2",
                "clinical_synthesis_3",
                "clinical_synthesis_4",
            }
    finally:
        engine.dispose()


def test_commit_false_does_not_auto_commit_and_rolls_back_entire_unit_of_work(
    tmp_path: Path,
):
    """
    Verifies that when `commit=False` is used inside a multi-report transaction:
    - A version conflict raises `ReportVersionConflictError` without silently retrying or
      committing earlier flushed reports in the same transaction.
    - Rolling back the session leaves only pre-existing committed history in SQLite.
    """
    db_path = tmp_path / "commit_false_rollback.db"
    engine, SessionFactory = _create_file_session_factory(db_path)
    uuid_cycle_id = "cycle_123e4567-e89b-12d3-a456-426614174000"

    try:
        with SessionFactory() as setup_session:
            _seed_cycle(setup_session, uuid_cycle_id)
            repo = ModelReportRepository(setup_session)
            repo.create_next_version(
                cycle_id=uuid_cycle_id,
                model="gemini-2.5-pro",
                report_type="chief_physician_internal",
                privacy_level=PrivacyLevel.SERVER_ONLY,
                content={"step": "committed_v1"},
            )

        session_a = SessionFactory()
        session_b = SessionFactory()
        try:
            repo_a = ModelReportRepository(session_a)
            repo_b = ModelReportRepository(session_b)

            # Session A reads latest version (v1) before starting its multi-report flush
            expected_next = (
                repo_a.get_latest_version_number(
                    cycle_id=uuid_cycle_id,
                    report_type="chief_physician_internal",
                    privacy_level=PrivacyLevel.SERVER_ONLY,
                )
                + 1
            )
            assert expected_next == 2

            # Session B concurrently commits chief_physician_internal v2
            repo_b.create_next_version(
                cycle_id=uuid_cycle_id,
                model="gemini-2.5-pro",
                report_type="chief_physician_internal",
                privacy_level=PrivacyLevel.SERVER_ONLY,
                content={"step": "session_b_committed_v2"},
                commit=True,
            )

            # Session A starts a multi-report unit of work (commit=False)
            repo_a.create_next_version(
                cycle_id=uuid_cycle_id,
                model="gemini-2.5-flash",
                report_type="deputy_chief_physician_internal",
                privacy_level=PrivacyLevel.SERVER_ONLY,
                content={"step": "uncommitted_deputy_v1"},
                commit=False,
            )

            # Now Session A tries to flush conflicting chief_physician_internal v2 with commit=False
            with pytest.raises(ReportVersionConflictError):
                repo_a.create_next_version(
                    cycle_id=uuid_cycle_id,
                    model="gemini-2.5-pro",
                    report_type="chief_physician_internal",
                    privacy_level=PrivacyLevel.SERVER_ONLY,
                    content={"step": "session_a_conflicting_v2"},
                    expected_version=expected_next,
                    commit=False,
                )
            session_a.rollback()
        finally:
            session_a.close()
            session_b.close()

        with SessionFactory() as verify_session:
            repo_v = ModelReportRepository(verify_session)
            # The uncommitted deputy report from Session A must NOT exist
            assert (
                repo_v.list_versions(
                    cycle_id=uuid_cycle_id,
                    report_type="deputy_chief_physician_internal",
                    privacy_level=PrivacyLevel.SERVER_ONLY,
                )
                == []
            )
            chief_versions = repo_v.list_versions(
                cycle_id=uuid_cycle_id,
                report_type="chief_physician_internal",
                privacy_level=PrivacyLevel.SERVER_ONLY,
            )
            assert [v.version for v in chief_versions] == [1, 2]
            assert chief_versions[0].content == {"step": "committed_v1"}
            assert chief_versions[1].content == {"step": "session_b_committed_v2"}
    finally:
        engine.dispose()


def test_concurrent_laboratory_ledger_upserts_zero_lost_updates_across_sessions(
    tmp_path: Path,
):
    """
    Verifies that concurrent `upsert_laboratory_markers` calls on a UUID cycle_id across
    independent SQLite sessions (even with isolated `ledger_lock_registry` instances to
    simulate cross-process contention where all workers simultaneously read v1 first)
    re-read and re-merge state on conflict so zero laboratory markers are lost.
    """
    db_path = tmp_path / "concurrent_lab_ledger.db"
    engine, SessionFactory = _create_file_session_factory(db_path)
    uuid_cycle_id = "cycle_7c9e6679-7425-40de-944b-e07fc1f90ae7"

    try:
        with SessionFactory() as setup_session:
            _seed_cycle(setup_session, uuid_cycle_id)
            seed_service = LabRevisionConsiliumService(db=setup_session)
            v1_state = seed_service.upsert_laboratory_markers(
                cycle_id=uuid_cycle_id,
                mutations=[
                    LabMarkerMutationItem(
                        marker_key="ferritin",
                        display_name="Ferritin",
                        value=45.0,
                        unit="ng/mL",
                        sample_date="2026-10-01",
                        event_type=LabEventType.INITIAL,
                    )
                ],
            )
            assert v1_state.ledger_version == 1

        worker_mutations = [
            (
                "vitamin_d",
                LabMarkerMutationItem(
                    marker_key="vitamin_d",
                    display_name="25-OH Vitamin D",
                    value=32.5,
                    unit="ng/mL",
                    sample_date="2026-10-02",
                    event_type=LabEventType.INITIAL,
                ),
            ),
            (
                "tsh",
                LabMarkerMutationItem(
                    marker_key="tsh",
                    display_name="TSH",
                    value=1.85,
                    unit="mIU/L",
                    sample_date="2026-10-02",
                    event_type=LabEventType.INITIAL,
                ),
            ),
            (
                "hemoglobin",
                LabMarkerMutationItem(
                    marker_key="hemoglobin",
                    display_name="Hemoglobin",
                    value=148.0,
                    unit="g/L",
                    sample_date="2026-10-02",
                    event_type=LabEventType.INITIAL,
                ),
            ),
        ]

        barrier = threading.Barrier(len(worker_mutations))
        errors: List[Exception] = []
        returned_versions: List[int] = []
        lock = threading.Lock()

        def _upsert_worker(marker_name: str, item: LabMarkerMutationItem):
            session = SessionFactory()
            try:
                # Pass an isolated lock registry per worker to force true DB-level
                # optimistic read-modify-write contention on v2
                service = LabRevisionConsiliumService(
                    db=session,
                    ledger_lock_registry={},
                )
                orig_get_state = service.get_lab_ledger_state
                first_read = True

                def _synced_get_state(cid: str):
                    nonlocal first_read
                    st = orig_get_state(cid)
                    if first_read:
                        first_read = False
                        barrier.wait(timeout=5.0)
                    return st

                service.get_lab_ledger_state = _synced_get_state  # type: ignore[method-assign]

                res = service.upsert_laboratory_markers(
                    cycle_id=uuid_cycle_id,
                    mutations=[item],
                    max_retries=6,
                )
                with lock:
                    returned_versions.append(res.ledger_version)
            except Exception as exc:
                with lock:
                    errors.append(exc)
            finally:
                session.close()

        threads = [
            threading.Thread(target=_upsert_worker, args=(m_key, m_item))
            for m_key, m_item in worker_mutations
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15.0)

        assert errors == []

        with SessionFactory() as verify_session:
            assert find_duplicate_report_versions(verify_session) == []
            repo = ModelReportRepository(verify_session)
            ledger_versions = repo.list_versions(
                cycle_id=uuid_cycle_id,
                report_type=LABORATORY_LEDGER_REPORT_TYPE,
                privacy_level=PrivacyLevel.SERVER_ONLY,
            )
            assert [v.version for v in ledger_versions] == [1, 2, 3, 4]

            final_state = LabRevisionConsiliumService(
                db=verify_session
            ).get_lab_ledger_state(uuid_cycle_id)
            assert final_state.ledger_version == 4
            assert set(final_state.active_markers.keys()) == {
                "ferritin",
                "vitamin_d",
                "tsh",
                "hemoglobin",
            }
            assert len(final_state.observations_history) == 4
            assert final_state.active_markers["ferritin"].numeric_value == 45.0
            assert final_state.active_markers["vitamin_d"].numeric_value == 32.5
            assert final_state.active_markers["tsh"].numeric_value == 1.85
            assert final_state.active_markers["hemoglobin"].numeric_value == 148.0
    finally:
        engine.dispose()


def test_database_locked_rolls_back_cleanly_and_returns_conflict_without_corrupting_history(
    tmp_path: Path,
):
    """
    Simulates a real SQLite `database is locked` condition using an independent SQLite
    connection holding `BEGIN EXCLUSIVE` while a repository/service session (with a short
    busy_timeout) attempts to write a new version:
    - `create_next_version` rolls back cleanly and raises `ReportVersionConflictError`.
    - `/api/v1/cascade/labs/upsert` returns HTTP 409 Conflict (never 500 or false 200).
    - Once the exclusive lock is released, existing v1 history is intact and subsequent
      writes succeed normally as v2.
    """
    db_path = tmp_path / "database_locked.db"
    engine, SessionFactory = _create_file_session_factory(db_path, busy_timeout_ms=50)
    uuid_cycle_id = "cycle_e02fd0e4-00fd-090A-ca30-0d00a0038ba0"

    try:
        with SessionFactory() as setup_session:
            _seed_cycle(setup_session, uuid_cycle_id)
            seed_service = LabRevisionConsiliumService(db=setup_session)
            v1 = seed_service.upsert_laboratory_markers(
                cycle_id=uuid_cycle_id,
                mutations=[
                    {
                        "marker_key": "glucose",
                        "value": 5.1,
                        "unit": "mmol/L",
                        "sample_date": "2026-10-01",
                        "event_type": "initial",
                    }
                ],
            )
            assert v1.ledger_version == 1

        # Acquire a real cross-connection SQLite exclusive write lock
        locker_conn = sqlite3.connect(str(db_path), timeout=0.05)
        try:
            locker_conn.isolation_level = None
            locker_conn.execute("BEGIN EXCLUSIVE;")

            with SessionFactory() as locked_session:
                repo = ModelReportRepository(locked_session)
                with pytest.raises(ReportVersionConflictError) as exc_info:
                    repo.create_next_version(
                        cycle_id=uuid_cycle_id,
                        model="aegis-lab-ledger",
                        report_type=LABORATORY_LEDGER_REPORT_TYPE,
                        privacy_level=PrivacyLevel.SERVER_ONLY,
                        content={"ledger_version": 2, "locked_attempt": True},
                        max_retries=1,
                    )
                assert "locked" in str(exc_info.value).lower() or "conflict" in str(
                    exc_info.value
                ).lower()

            # Also verify HTTP 409 Conflict via FastAPI endpoint when DB is locked
            def _override_get_db():
                s = SessionFactory()
                try:
                    yield s
                finally:
                    s.close()

            app.dependency_overrides[get_db] = _override_get_db
            app.dependency_overrides[verify_client_token] = lambda: "test-token"
            try:
                with TestClient(app) as client:
                    resp = client.post(
                        "/api/v1/cascade/labs/upsert",
                        json={
                            "cycle_id": uuid_cycle_id,
                            "mutations": [
                                {
                                    "marker_key": "crp",
                                    "value": 1.2,
                                    "unit": "mg/L",
                                    "sample_date": "2026-10-02",
                                    "event_type": "initial",
                                }
                            ],
                        },
                    )
                    assert resp.status_code == 409
            finally:
                app.dependency_overrides.pop(get_db, None)
                app.dependency_overrides.pop(verify_client_token, None)
        finally:
            locker_conn.execute("ROLLBACK;")
            locker_conn.close()

        # After lock release, verify v1 is intact and a new upsert succeeds as v2
        with SessionFactory() as recovery_session:
            service = LabRevisionConsiliumService(db=recovery_session)
            recovered_state = service.upsert_laboratory_markers(
                cycle_id=uuid_cycle_id,
                mutations=[
                    {
                        "marker_key": "crp",
                        "value": 1.2,
                        "unit": "mg/L",
                        "sample_date": "2026-10-02",
                        "event_type": "initial",
                    }
                ],
            )
            assert recovered_state.ledger_version == 2
            assert set(recovered_state.active_markers.keys()) == {"glucose", "crp"}
    finally:
        engine.dispose()


def test_migration_0002_on_fresh_empty_temp_sqlite_db(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """
    Verifies that running `alembic upgrade head` on a brand-new empty temporary SQLite
    database applies 0001 and 0002 cleanly and enforces the composite UNIQUE constraint.
    """
    db_path = tmp_path / "migration_fresh_empty.db"
    sqlite_url = f"sqlite:///{db_path}"
    monkeypatch.setenv("DATABASE_URL", sqlite_url)
    cfg = _make_alembic_config(sqlite_url)

    command.upgrade(cfg, "head")

    uuid_cycle_id = "cycle_11111111-2222-4333-8444-555555555555"
    now_str = datetime.now(timezone.utc).isoformat()
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("PRAGMA foreign_keys=ON;")
        conn.execute(
            "INSERT INTO medical_cycles (cycle_id, status, started_at, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (uuid_cycle_id, "active", now_str, now_str, now_str),
        )
        conn.execute(
            "INSERT INTO model_reports "
            "(report_id, cycle_id, sprint_id, model, report_type, privacy_level, version, content, created_at) "
            "VALUES (?, ?, NULL, ?, ?, ?, ?, ?, ?)",
            (
                "rep_fresh_1",
                uuid_cycle_id,
                "gemini-2.5-pro",
                "chief_physician_internal",
                "server_only",
                1,
                '{"v": 1}',
                now_str,
            ),
        )
        conn.commit()

        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO model_reports "
                "(report_id, cycle_id, sprint_id, model, report_type, privacy_level, version, content, created_at) "
                "VALUES (?, ?, NULL, ?, ?, ?, ?, ?, ?)",
                (
                    "rep_fresh_1_dup_pk",
                    uuid_cycle_id,
                    "gemini-2.5-pro",
                    "chief_physician_internal",
                    "server_only",
                    1,
                    '{"v": "dup"}',
                    now_str,
                ),
            )
    finally:
        conn.close()
