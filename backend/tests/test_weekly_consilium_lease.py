import asyncio
from datetime import datetime, timedelta, timezone
import json
from typing import Any, Dict, Generator, List, Optional
import httpx
import pytest
from sqlalchemy import create_engine, delete, update
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool
from fastapi.testclient import TestClient

from app.config import settings
from app.database import get_db
from app.main import app
from app.models.base import Base
from app.models.medical_cycle import MedicalCycle
from app.models.model_report import ModelReport
from app.models.patient import Patient
from app.models.weekly_sprint import WeeklySprint
from app.repositories.cycle_repository import MedicalCycleRepository
from app.repositories.measurement_repository import HealthMeasurementRepository
from app.repositories.pairing_repository import PairingRepository
from app.repositories.patient_repository import PatientRepository
from app.repositories.report_repository import ModelReportRepository
from app.repositories.sprint_repository import WeeklySprintRepository
from app.schemas.consilium import (
    SYSTEM_KEYS,
    WeeklyConsiliumClientResponse,
)
from app.schemas.privacy import PrivacyLevel
from app.security.tokens import (
    SCOPE_CASCADE_RUN,
    SCOPE_REPORTS_READ,
)
from app.services.gemini_gateway import (
    GeminiGateway,
    GeminiGatewayError,
)
from app.routers.client_cascade import get_gemini_gateway
from app.services.primary_consilium_service import (
    PrimaryConsiliumService,
    ConsiliumConcurrencyConflictError,
    ConsiliumContextMissingError,
    ConsiliumQualityRejectionError,
    PRIMARY_CONSILIUM_IN_FLIGHT_LOCK_REPORT_TYPE,
)
from app.services.weekly_consilium_service import (
    WeeklyConsiliumService,
    ConsiliumSprintConflictError,
    WEEKLY_CONSILIUM_LOCK_TTL_SECONDS,
    WEEKLY_CONSILIUM_IN_FLIGHT_LOCK_REPORT_TYPE,
    WEEKLY_CONSILIUM_EXECUTION_LOG_REPORT_TYPE,
    WEEKLY_OVERALL_CLIENT_REPORT_TYPE,
    WEEKLY_DEPUTY_INTERNAL_REPORT_TYPE,
    WEEKLY_CHIEF_INTERNAL_REPORT_TYPE,
    ATHENA_ACTIVE_STRATEGY_REPORT_TYPE,
    CYCLE_CONSILIUM_COORDINATION_LOCK_REPORT_TYPE,
)
from tests.test_primary_consilium import (
    _seed_preparatory_cycle_context,
    _build_consilium_mock_gateway,
)
from tests.test_weekly_consilium import (
    _sample_weekly_flash_output,
    _sample_weekly_pro_output,
    _build_weekly_mock_gateway,
    _seed_completed_primary_consilium,
    _seed_seven_day_athena_logs,
)
from tests.test_client_cascade_isolation import (
    TEST_API_SECRET,
    TEST_PAIRING_SECRET,
    TEST_JWT_SECRET,
    _get_device_jwt_with_scopes,
)


@pytest.fixture
def sqlite_db_session() -> Generator[Session, None, None]:
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    SessionMaker = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    session = SessionMaker()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture
def client_env(monkeypatch, tmp_path) -> Generator[Any, None, None]:
    monkeypatch.setattr(settings, "api_secret", TEST_API_SECRET)
    monkeypatch.setattr(settings, "pairing_secret", TEST_PAIRING_SECRET)
    monkeypatch.setattr(settings, "jwt_secret", TEST_JWT_SECRET)
    monkeypatch.setattr(settings, "jwt_issuer", "aegis-backend")
    monkeypatch.setattr(settings, "jwt_audience", "aegis-mobile-client")

    db_file = tmp_path / "test_weekly_consilium_lease.db"
    eng = create_engine(
        f"sqlite:///{db_file}",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=eng)
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=eng)
    session = SessionLocal()

    def _override_get_db():
        with SessionLocal() as req_session:
            yield req_session

    app.dependency_overrides[get_db] = _override_get_db

    try:
        with TestClient(app) as test_client:
            yield test_client, session
    finally:
        app.dependency_overrides.clear()
        session.close()
        eng.dispose()


# ==============================================================================
# TEST 1: Successful Weekly Execution — Lease Lifecycle & Atomic Cleanup
# ==============================================================================

@pytest.mark.anyio
async def test_weekly_consilium_lease_lifecycle_and_success(sqlite_db_session: Session):
    """
    Verifies that during a successful Weekly Consilium run:
    1. Lease is acquired and heartbeat-managed;
    2. Strictly 2 Gemini calls are made: Flash -> Pro;
    3. Fencing verifies and removes lease uncommitted before persistence;
    4. All reports (weekly_deputy_internal, weekly_chief_physician_internal,
       athena_active_strategy, 8 system updates, weekly_user_overall_report,
       and weekly_consilium_execution_log) are atomically committed;
    5. In-flight lock row is completely deleted;
    6. Sprint is marked 'completed' while cycle remains 'active'.
    """
    cycle_id = "cycle_weekly_lifecycle_01"
    sprint_id = "sprint_w1_life"
    await _seed_completed_primary_consilium(sqlite_db_session, cycle_id=cycle_id, primary_date="2026-10-18")
    _seed_seven_day_athena_logs(sqlite_db_session, cycle_id=cycle_id, sprint_id=sprint_id, base_date="2026-10-18")

    weekly_calls: List[Dict[str, Any]] = []
    gateway = _build_weekly_mock_gateway(weekly_calls)
    service = WeeklyConsiliumService(
        db=sqlite_db_session,
        gateway=gateway,
        lease_ttl_seconds=30,
        lease_heartbeat_interval_seconds=0.05,
    )

    result = await service.run_weekly_consilium(
        cycle_id=cycle_id,
        sprint_id=sprint_id,
        report_date="2026-10-25",
        idempotency_key="idemp-wk-lifecycle-01",
    )

    assert isinstance(result, WeeklyConsiliumClientResponse)
    assert result.cycle_id == cycle_id
    assert result.sprint_id == sprint_id
    assert result.sprint_number == 1
    assert result.report_date == "2026-10-25"
    assert len(result.system_reports) == 8
    assert len(weekly_calls) == 2

    report_repo = ModelReportRepository(sqlite_db_session)
    sprint_repo = WeeklySprintRepository(sqlite_db_session)

    # In-flight lock is completely deleted
    lock_row = sqlite_db_session.query(ModelReport).filter(
        ModelReport.report_id == f"lock_weekly_{cycle_id}"
    ).first()
    assert lock_row is None

    # Execution log exists with valid lease metadata
    logs = report_repo.list_server_only(cycle_id=cycle_id, report_type=WEEKLY_CONSILIUM_EXECUTION_LOG_REPORT_TYPE)
    assert len(logs) == 1
    log_content = logs[0].content or {}
    assert log_content.get("idempotency_key") == "idemp-wk-lifecycle-01"
    assert "lease_weekly_" in log_content.get("lease_owner_id", "")

    # Sprint is completed and cycle remains active
    sprint = sprint_repo.get(sprint_id)
    assert sprint is not None
    assert sprint.status == "completed"
    cycle = MedicalCycleRepository(sqlite_db_session).get(cycle_id)
    assert cycle.status == "active"


# ==============================================================================
# TEST 2: Data Insufficiency — Rejection when Primary Consilium is Missing
# ==============================================================================

@pytest.mark.anyio
async def test_weekly_consilium_insufficient_data(sqlite_db_session: Session):
    """
    Verifies that attempting to run Weekly Consilium without completed Primary Consilium
    is safely rejected with ConsiliumContextMissingError:
    - Zero Gemini calls made;
    - Zero reports created;
    - Sprint remains uncompleted;
    - Lease is never held.
    """
    cycle_id = "cycle_insufficient_data_01"
    cycle_repo = MedicalCycleRepository(sqlite_db_session)
    patient_repo = PatientRepository(sqlite_db_session)
    patient_repo.create(patient_id="pat_insufficient_01", status="active")
    cycle_repo.create(cycle_id=cycle_id, patient_id="pat_insufficient_01", status="active")

    weekly_calls: List[Dict[str, Any]] = []
    gateway = _build_weekly_mock_gateway(weekly_calls)
    service = WeeklyConsiliumService(db=sqlite_db_session, gateway=gateway)

    with pytest.raises(ConsiliumContextMissingError, match="Primary Consilium"):
        await service.run_weekly_consilium(cycle_id=cycle_id, report_date="2026-10-25")

    assert len(weekly_calls) == 0
    report_repo = ModelReportRepository(sqlite_db_session)
    assert report_repo.list_client_facing(cycle_id=cycle_id) == []
    assert report_repo.list_server_only(cycle_id=cycle_id) == []


# ==============================================================================
# TEST 3: Concurrent Requests & Cross-Consilium Mutual Exclusion
# ==============================================================================

@pytest.mark.anyio
async def test_weekly_consilium_concurrent_requests_and_cross_consilium_conflict(
    sqlite_db_session: Session,
):
    """
    Verifies mutual exclusion:
    1. Weekly vs Weekly: When Worker A holds the weekly lease, Worker B is rejected with 409 Conflict;
    2. Primary vs Weekly: When Primary lease is active, Weekly Consilium cannot run;
    3. Weekly vs Primary: When Weekly lease is active, Primary Consilium cannot run.
    """
    cycle_id = "cycle_concurrent_weekly_01"
    sprint_id = "sprint_w1_conc"
    await _seed_completed_primary_consilium(sqlite_db_session, cycle_id=cycle_id, primary_date="2026-10-18")
    _seed_seven_day_athena_logs(sqlite_db_session, cycle_id=cycle_id, sprint_id=sprint_id, base_date="2026-10-18")

    service_a = WeeklyConsiliumService(db=sqlite_db_session)
    service_b = WeeklyConsiliumService(db=sqlite_db_session)

    # 1. Acquire weekly lease manually as Worker A
    lease_a = service_a._acquire_cross_process_lease(cycle_id)
    assert "lease_weekly_" in lease_a

    # Worker B attempts to run Weekly -> rejected with ConsiliumConcurrencyConflictError
    with pytest.raises(ConsiliumConcurrencyConflictError, match="Weekly Consilium.*currently in progress"):
        await service_b.run_weekly_consilium(cycle_id=cycle_id, sprint_id=sprint_id)

    # Worker C attempts to run Primary Consilium -> rejected because Weekly is in progress
    primary_service = PrimaryConsiliumService(db=sqlite_db_session)
    with pytest.raises(ConsiliumConcurrencyConflictError, match="Weekly Consilium.*currently in progress"):
        primary_service._acquire_cross_process_lease(cycle_id)

    # Release weekly lease
    service_a._release_cross_process_lease_safe(cycle_id, lease_a)

    # 2. Acquire primary lease manually
    lease_prim = primary_service._acquire_cross_process_lease(cycle_id)
    assert "lease_primary_" in lease_prim

    # Worker B attempts to run Weekly -> rejected because Primary is in progress
    with pytest.raises(ConsiliumConcurrencyConflictError, match="Primary Consilium.*currently in progress"):
        await service_b.run_weekly_consilium(cycle_id=cycle_id, sprint_id=sprint_id)

    primary_service._release_cross_process_lease_safe(cycle_id, lease_prim)


# ==============================================================================
# TEST 4: Lease Expiration and Automatic Stale Lock Recovery
# ==============================================================================

@pytest.mark.anyio
async def test_weekly_consilium_lease_expiration_and_recovery(sqlite_db_session: Session):
    """
    Verifies that if a crashed worker left an expired lease (> TTL without renewal),
    a subsequent worker automatically detects it, deletes the stale lock row,
    acquires a fresh lease, and successfully executes the Weekly Consilium.
    """
    cycle_id = "cycle_lease_expire_01"
    sprint_id = "sprint_w1_exp"
    await _seed_completed_primary_consilium(sqlite_db_session, cycle_id=cycle_id, primary_date="2026-10-18")
    _seed_seven_day_athena_logs(sqlite_db_session, cycle_id=cycle_id, sprint_id=sprint_id, base_date="2026-10-18")

    # Seed an expired lock row from a crashed worker (expired 5 minutes ago)
    now_dt = datetime.now(timezone.utc)
    stale_expires_dt = now_dt - timedelta(minutes=5)
    stale_acquired_dt = now_dt - timedelta(minutes=10)
    stale_lock = ModelReport(
        report_id=f"lock_weekly_{cycle_id}",
        cycle_id=cycle_id,
        sprint_id=None,
        session_id=None,
        model="aegis-concurrency-guard",
        report_type=WEEKLY_CONSILIUM_IN_FLIGHT_LOCK_REPORT_TYPE,
        privacy_level=PrivacyLevel.SERVER_ONLY.value,
        version=1,
        content={
            "lease_owner_id": "lease_weekly_crashed_worker_dead",
            "expires_at_utc": stale_expires_dt.isoformat(),
            "acquired_at_utc": stale_acquired_dt.isoformat(),
            "renewed_at_utc": stale_acquired_dt.isoformat(),
            "renewal_count": 0,
        },
        created_at=stale_acquired_dt,
    )
    sqlite_db_session.add(stale_lock)
    sqlite_db_session.commit()

    # Worker B arrives to run Weekly Consilium
    calls: List[Dict[str, Any]] = []
    gateway = _build_weekly_mock_gateway(calls)
    service = WeeklyConsiliumService(db=sqlite_db_session, gateway=gateway)

    result = await service.run_weekly_consilium(
        cycle_id=cycle_id,
        sprint_id=sprint_id,
        report_date="2026-10-25",
    )

    assert result.cycle_id == cycle_id
    assert len(calls) == 2
    # Verify stale lock was superseded and deleted
    lock_row = sqlite_db_session.query(ModelReport).filter(
        ModelReport.report_id == f"lock_weekly_{cycle_id}"
    ).first()
    assert lock_row is None


# ==============================================================================
# TEST 5: Stale-Worker Fencing — Lost Lease Blocks Publication
# ==============================================================================

@pytest.mark.anyio
async def test_weekly_consilium_stale_worker_fencing(sqlite_db_session: Session):
    """
    Verifies that if Worker A's lease expires and Worker B acquires the lease,
    when Worker A attempts uncommitted verification and deletion of the lease,
    the fencing check fails (0 rows deleted matching owner_A), rolling back
    the entire transaction and preventing stale publication.
    """
    cycle_id = "cycle_fencing_weekly_01"
    PatientRepository(sqlite_db_session).create(patient_id="pat_fencing_01", status="active")
    MedicalCycleRepository(sqlite_db_session).create(cycle_id=cycle_id, patient_id="pat_fencing_01", status="active")
    service = WeeklyConsiliumService(db=sqlite_db_session)

    # Worker A acquires lease
    lease_a = service._acquire_cross_process_lease(cycle_id)

    # Worker B steals/supersedes the lease (simulating lease expiration and re-acquisition)
    lock_row = service._fetch_fresh_lock_row(cycle_id)
    assert lock_row is not None
    updated_content = dict(lock_row.content or {})
    updated_content["lease_owner_id"] = "lease_weekly_usurper_worker_b"
    sqlite_db_session.execute(
        update(ModelReport)
        .where(ModelReport.report_id.in_([f"lock_consilium_coord_{cycle_id}", f"lock_weekly_{cycle_id}"]))
        .values(content=updated_content)
    )
    sqlite_db_session.commit()

    # Worker A attempts fencing deletion
    with pytest.raises(ConsiliumConcurrencyConflictError, match="lost or superseded"):
        service._verify_and_delete_cross_process_lease_uncommitted(cycle_id, lease_a)

    # Worker B's lock remains safely untouched
    lock_after = service._fetch_fresh_lock_row(cycle_id)
    assert lock_after is not None
    assert (lock_after.content or {}).get("lease_owner_id") == "lease_weekly_usurper_worker_b"


# ==============================================================================
# TEST 6: Idempotent Retry — Zero Duplicate AI Generation
# ==============================================================================

@pytest.mark.anyio
async def test_weekly_consilium_retry_zero_ai_duplicate(sqlite_db_session: Session):
    """
    Verifies that retrying Weekly Consilium with an existing Idempotency-Key
    or for an already-completed sprint recovers the existing response immediately
    with strictly ZERO duplicate Gemini calls.
    """
    cycle_id = "cycle_weekly_retry_01"
    sprint_id = "sprint_w1_retry"
    await _seed_completed_primary_consilium(sqlite_db_session, cycle_id=cycle_id, primary_date="2026-10-18")
    _seed_seven_day_athena_logs(sqlite_db_session, cycle_id=cycle_id, sprint_id=sprint_id, base_date="2026-10-18")

    calls: List[Dict[str, Any]] = []
    gateway = _build_weekly_mock_gateway(calls)
    service = WeeklyConsiliumService(db=sqlite_db_session, gateway=gateway)

    # First run: executes 2 Gemini calls
    res1 = await service.run_weekly_consilium(
        cycle_id=cycle_id,
        sprint_id=sprint_id,
        report_date="2026-10-25",
        idempotency_key="idemp-key-weekly-001",
    )
    assert len(calls) == 2
    assert res1.overall_report.report_type == WEEKLY_OVERALL_CLIENT_REPORT_TYPE

    # Retry 1: Same idempotency key -> recovered from execution log, zero extra AI calls
    res2 = await service.run_weekly_consilium(
        cycle_id=cycle_id,
        sprint_id=sprint_id,
        report_date="2026-10-25",
        idempotency_key="idemp-key-weekly-001",
    )
    assert len(calls) == 2  # Still 2!
    assert res2.overall_report.report_id == res1.overall_report.report_id
    assert res2.sprint_id == sprint_id

    # Retry 2: Sprint is already completed -> recovered, zero extra AI calls
    res3 = await service.run_weekly_consilium(
        cycle_id=cycle_id,
        sprint_id=sprint_id,
        report_date="2026-10-25",
    )
    assert len(calls) == 2  # Still 2!
    assert res3.overall_report.report_id == res1.overall_report.report_id


# ==============================================================================
# TEST 7: Patient Isolation via Client API
# ==============================================================================

@pytest.mark.anyio
async def test_weekly_consilium_patient_isolation(client_env):
    """
    Verifies that client endpoint POST /api/v1/client/cascade/run-weekly:
    1. Resolves cycle strictly from authenticated DeviceSession;
    2. Completely ignores any client-supplied cycle_id parameter;
    3. Patient A cannot execute or access Weekly Consilium on Patient B's cycle.
    """
    client, session = client_env

    # Setup Patient A and Patient B
    p_a_id = "pat_weekly_iso_a"
    p_b_id = "pat_weekly_iso_b"
    token_bundle_a, token_a = _get_device_jwt_with_scopes(
        client, session, patient_id=p_a_id, device_id="dev_a_1", raw_code="111111", scope=f"{SCOPE_CASCADE_RUN} {SCOPE_REPORTS_READ}"
    )

    cycle_repo = MedicalCycleRepository(session)
    cycle_a, _ = cycle_repo.get_or_create_active_for_patient(p_a_id)
    PatientRepository(session).create(patient_id=p_b_id, status="active")
    cycle_b, _ = cycle_repo.get_or_create_active_for_patient(p_b_id)

    # Seed Patient A's primary consilium & diary
    await _seed_completed_primary_consilium(session, cycle_id=cycle_a.cycle_id, primary_date="2026-10-18")
    _seed_seven_day_athena_logs(session, cycle_id=cycle_a.cycle_id, sprint_id="sprint_a_w1")

    # Patient B has NO primary consilium
    calls: List[Dict[str, Any]] = []
    mock_gw = _build_weekly_mock_gateway(calls)
    app.dependency_overrides[get_gemini_gateway] = lambda: mock_gw

    headers_a = {
        "Authorization": f"Bearer {token_a}",
        "X-Device-Id": "dev_a_1",
    }

    # Patient A posts request attempting to inject Patient B's cycle_id
    resp = client.post(
        "/api/v1/client/cascade/run-weekly",
        json={
            "cycle_id": cycle_b.cycle_id,  # Attempted injection
            "sprint_id": "sprint_a_w1",
            "report_date": "2026-10-25",
        },
        headers=headers_a,
    )
    assert resp.status_code == 200
    body = resp.json()
    # Authoritative session took precedence: Patient A's cycle executed!
    assert body["cycle_id"] == cycle_a.cycle_id
    assert body["cycle_id"] != cycle_b.cycle_id

    # Verify Patient B's cycle was NEVER modified
    report_repo = ModelReportRepository(session)
    assert report_repo.list_client_facing(cycle_id=cycle_b.cycle_id) == []


# ==============================================================================
# TEST 8: Preservation of Previous Reports & Atlas History Accumulation
# ==============================================================================

@pytest.mark.anyio
async def test_weekly_consilium_preserves_previous_reports(sqlite_db_session: Session):
    """
    Verifies that running Weekly Consilium:
    1. Preserves Primary Consilium's 'user_overall_report' completely;
    2. Writes a separate 'weekly_user_overall_report';
    3. Increments the version of 8 system_user_report_<key> and appends
       a new dated entry to 'entries' without erasing the initial Primary entry.
    """
    cycle_id = "cycle_preserve_reports_01"
    sprint_id = "sprint_w1_pres"
    primary_date = "2026-10-18"
    weekly_date = "2026-10-25"

    await _seed_completed_primary_consilium(sqlite_db_session, cycle_id=cycle_id, primary_date=primary_date)
    _seed_seven_day_athena_logs(sqlite_db_session, cycle_id=cycle_id, sprint_id=sprint_id, base_date=primary_date)

    report_repo = ModelReportRepository(sqlite_db_session)

    # Primary user_overall_report exists
    primary_overalls = report_repo.list_client_facing(cycle_id=cycle_id, report_type="user_overall_report")
    assert len(primary_overalls) == 1
    assert primary_overalls[0].content["report_date"] == primary_date

    # Run Weekly Consilium
    calls: List[Dict[str, Any]] = []
    gateway = _build_weekly_mock_gateway(calls)
    service = WeeklyConsiliumService(db=sqlite_db_session, gateway=gateway)

    result = await service.run_weekly_consilium(
        cycle_id=cycle_id,
        sprint_id=sprint_id,
        report_date=weekly_date,
    )

    # 1. Primary user_overall_report is still intact!
    primary_after = report_repo.list_client_facing(cycle_id=cycle_id, report_type="user_overall_report")
    assert len(primary_after) == 1
    assert primary_after[0].content["report_date"] == primary_date

    # 2. Weekly overall report exists separately
    weekly_overalls = report_repo.list_client_facing(cycle_id=cycle_id, report_type=WEEKLY_OVERALL_CLIENT_REPORT_TYPE)
    assert len(weekly_overalls) == 1
    assert weekly_overalls[0].content["report_date"] == weekly_date

    # 3. All 8 system reports now have version 2 and 2 entries: [primary, weekly]
    for sys_key in SYSTEM_KEYS:
        sys_rep = result.system_reports[sys_key]
        assert sys_rep.version == 2
        entries = sys_rep.content.entries or []
        assert len(entries) == 2
        assert entries[0].date == primary_date
        assert entries[1].date == weekly_date


# ==============================================================================
# TEST 9: Rollback & Safe Release on Quality Rejection or Gemini Failure
# ==============================================================================

@pytest.mark.anyio
async def test_weekly_consilium_no_partial_publication_on_failure(sqlite_db_session: Session):
    """
    Verifies that if Chief Physician Pro rejects publication or Gemini fails:
    1. Transaction rolls back completely;
    2. Zero client-facing weekly reports are published;
    3. Zero server-only weekly reports are published;
    4. Sprint remains 'active' (not completed);
    5. Execution lease is safely released.
    """
    cycle_id = "cycle_weekly_rollback_test"
    sprint_id = "sprint_w1_fail"
    await _seed_completed_primary_consilium(sqlite_db_session, cycle_id=cycle_id, primary_date="2026-10-18")
    _seed_seven_day_athena_logs(sqlite_db_session, cycle_id=cycle_id, sprint_id=sprint_id, base_date="2026-10-18")

    sprint_repo = WeeklySprintRepository(sqlite_db_session)
    sprint_repo.create(
        sprint_id=sprint_id,
        cycle_id=cycle_id,
        sprint_number=1,
        status="active",
    )

    # Injected Pro rejection
    calls: List[Dict[str, Any]] = []
    gateway = _build_weekly_mock_gateway(
        calls,
        pro_override=_sample_weekly_pro_output(
            approved=False,
            rejection_reason="Contradictory weekly findings; unsafe for user publication.",
        ),
    )
    service = WeeklyConsiliumService(db=sqlite_db_session, gateway=gateway)

    with pytest.raises(ConsiliumQualityRejectionError, match="Contradictory weekly findings"):
        await service.run_weekly_consilium(
            cycle_id=cycle_id,
            sprint_id=sprint_id,
            report_date="2026-10-25",
        )

    report_repo = ModelReportRepository(sqlite_db_session)
    # Zero weekly client reports exist
    assert report_repo.list_client_facing(cycle_id=cycle_id, report_type=WEEKLY_OVERALL_CLIENT_REPORT_TYPE) == []

    # System reports remain at version 1 (from Primary)
    for sys_key in SYSTEM_KEYS:
        versions = report_repo.list_versions(cycle_id=cycle_id, report_type=f"system_user_report_{sys_key}")
        assert len(versions) == 1
        assert versions[0].version == 1

    # Sprint is STILL active
    sprint = sprint_repo.get(sprint_id)
    assert sprint is not None
    assert sprint.status == "active"

    # Execution lease is deleted / safely released
    lock_row = sqlite_db_session.query(ModelReport).filter(
        ModelReport.report_id == f"lock_weekly_{cycle_id}"
    ).first()
    assert lock_row is None


# ==============================================================================
# TEST 10: Simultaneous Primary & Weekly Atomic Exclusion (Single Winner)
# ==============================================================================

@pytest.mark.anyio
async def test_simultaneous_primary_and_weekly_atomic_exclusion(sqlite_db_session: Session):
    """
    Verifies that simultaneous attempts to start Primary and Weekly Consilium
    for the exact same cycle_id result in strictly ONE winner, while the loser
    receives 409 Conflict (ConsiliumConcurrencyConflictError) without creating
    any partial or orphaned database records.
    Guaranteed by SQLite PRIMARY KEY uniqueness on `lock_consilium_coord_{cycle_id}`.
    """
    cycle_id = "cycle_atomic_simultaneous_01"
    patient_id = "pat_atomic_simultaneous_01"
    PatientRepository(sqlite_db_session).create(patient_id=patient_id, status="active")
    MedicalCycleRepository(sqlite_db_session).create(cycle_id=cycle_id, patient_id=patient_id, status="active")

    primary_service = PrimaryConsiliumService(db=sqlite_db_session)
    weekly_service = WeeklyConsiliumService(db=sqlite_db_session)

    # Worker A (Primary) acquires the atomic coordination lease
    lease_primary = primary_service._acquire_cross_process_lease(cycle_id)
    assert "lease_primary_" in lease_primary

    # Worker B (Weekly) simultaneously attempts to acquire lease for the same cycle
    # SQLite PRIMARY KEY on `lock_consilium_coord_{cycle_id}` triggers immediate conflict
    with pytest.raises(ConsiliumConcurrencyConflictError):
        weekly_service._acquire_cross_process_lease(cycle_id)

    # Verify no partial or orphaned weekly lock row was left in database
    orphaned_weekly_lock = sqlite_db_session.query(ModelReport).filter(
        ModelReport.report_id == f"lock_weekly_{cycle_id}"
    ).first()
    assert orphaned_weekly_lock is None

    # Primary safely releases both coordination and primary lock rows
    primary_service._release_cross_process_lease_safe(cycle_id, lease_primary)

    coord_row = sqlite_db_session.query(ModelReport).filter(
        ModelReport.report_id == f"lock_consilium_coord_{cycle_id}"
    ).first()
    assert coord_row is None


# ==============================================================================
# TEST 11: Independent Execution for Different Cycles
# ==============================================================================

@pytest.mark.anyio
async def test_two_different_cycles_independent(sqlite_db_session: Session):
    """
    Verifies that two different medical cycles (e.g. Cycle A Primary and Cycle B Weekly)
    can execute and acquire cross-process execution leases concurrently without
    any cross-cycle interference.
    """
    cycle_a = "cycle_indep_01"
    cycle_b = "cycle_indep_02"
    patient_repo = PatientRepository(sqlite_db_session)
    cycle_repo = MedicalCycleRepository(sqlite_db_session)

    patient_repo.create(patient_id="pat_indep_01", status="active")
    patient_repo.create(patient_id="pat_indep_02", status="active")
    cycle_repo.create(cycle_id=cycle_a, patient_id="pat_indep_01", status="active")
    cycle_repo.create(cycle_id=cycle_b, patient_id="pat_indep_02", status="active")

    primary_service_a = PrimaryConsiliumService(db=sqlite_db_session)
    weekly_service_b = WeeklyConsiliumService(db=sqlite_db_session)

    # Both acquire leases concurrently on distinct cycles
    lease_a = primary_service_a._acquire_cross_process_lease(cycle_a)
    lease_b = weekly_service_b._acquire_cross_process_lease(cycle_b)

    assert "lease_primary_" in lease_a
    assert "lease_weekly_" in lease_b

    # Both leases exist independently in database
    lock_a = sqlite_db_session.query(ModelReport).filter(
        ModelReport.report_id == f"lock_consilium_coord_{cycle_a}"
    ).first()
    lock_b = sqlite_db_session.query(ModelReport).filter(
        ModelReport.report_id == f"lock_consilium_coord_{cycle_b}"
    ).first()
    assert lock_a is not None
    assert lock_b is not None
    assert (lock_a.content or {}).get("consilium_type") == "primary"
    assert (lock_b.content or {}).get("consilium_type") == "weekly"

    # Safely release both
    primary_service_a._release_cross_process_lease_safe(cycle_a, lease_a)
    weekly_service_b._release_cross_process_lease_safe(cycle_b, lease_b)


# ==============================================================================
# TEST 12: Retry Without Idempotency-Key Does Not Advance Sprint
# ==============================================================================

@pytest.mark.anyio
async def test_weekly_retry_without_idempotency_key_does_not_create_next_sprint(
    sqlite_db_session: Session,
):
    """
    Verifies that calling Weekly Consilium without an idempotency key and without sprint_id
    after Week 1 is already completed:
    1. Recovers Week 1 results directly from database without duplicate Gemini calls;
    2. Does NOT advance sprint_number from 1 to 2;
    3. Total sprint count for the cycle remains strictly 1.
    """
    cycle_id = "cycle_retry_no_key_01"
    sprint_id = "sprint_w1_no_key"
    await _seed_completed_primary_consilium(sqlite_db_session, cycle_id=cycle_id, primary_date="2026-10-18")
    _seed_seven_day_athena_logs(sqlite_db_session, cycle_id=cycle_id, sprint_id=sprint_id, base_date="2026-10-18")

    calls: List[Dict[str, Any]] = []
    gateway = _build_weekly_mock_gateway(calls)
    service = WeeklyConsiliumService(db=sqlite_db_session, gateway=gateway)
    sprint_repo = WeeklySprintRepository(sqlite_db_session)

    # Initial run for Week 1
    res1 = await service.run_weekly_consilium(
        cycle_id=cycle_id,
        sprint_id=sprint_id,
        report_date="2026-10-25",
    )
    assert len(calls) == 2
    assert res1.sprint_number == 1
    assert len(sprint_repo.list_by_cycle(cycle_id)) == 1

    # Retry with NO sprint_id and NO idempotency_key
    res2 = await service.run_weekly_consilium(
        cycle_id=cycle_id,
        sprint_id=None,
        report_date="2026-10-25",
        idempotency_key=None,
    )

    # Strictly ZERO additional Gemini calls
    assert len(calls) == 2
    # Recovers existing Week 1 report
    assert res2.sprint_number == 1
    assert res2.sprint_id == sprint_id
    assert res2.overall_report.report_id == res1.overall_report.report_id
    # Total sprints in database is STILL exactly 1!
    assert len(sprint_repo.list_by_cycle(cycle_id)) == 1


# ==============================================================================
# TEST 13: Explicitly New Weekly Period Advances Sprint
# ==============================================================================

@pytest.mark.anyio
async def test_weekly_explicitly_new_weekly_period(sqlite_db_session: Session):
    """
    Verifies that providing an explicit new sprint_id (e.g. 'sprint_w2') unambiguously
    starts a new weekly period:
    1. Executes Gemini Flash -> Pro for Week 2;
    2. Creates and completes sprint 2;
    3. Increments report versions to v3 (v1=primary, v2=week1, v3=week2);
    4. Sprints count becomes 2.
    """
    cycle_id = "cycle_explicit_w2_01"
    sprint_1 = "sprint_w1_exp"
    sprint_2 = "sprint_w2_exp"
    await _seed_completed_primary_consilium(sqlite_db_session, cycle_id=cycle_id, primary_date="2026-10-18")
    _seed_seven_day_athena_logs(sqlite_db_session, cycle_id=cycle_id, sprint_id=sprint_1, base_date="2026-10-18")

    calls: List[Dict[str, Any]] = []
    gateway = _build_weekly_mock_gateway(calls)
    service = WeeklyConsiliumService(db=sqlite_db_session, gateway=gateway)
    sprint_repo = WeeklySprintRepository(sqlite_db_session)

    # Week 1
    res1 = await service.run_weekly_consilium(
        cycle_id=cycle_id,
        sprint_id=sprint_1,
        report_date="2026-10-25",
    )
    assert len(calls) == 2
    assert res1.sprint_number == 1

    # Seed Week 2 athena logs
    _seed_seven_day_athena_logs(sqlite_db_session, cycle_id=cycle_id, sprint_id=sprint_2, base_date="2026-10-25")

    # Explicit Week 2 execution with fresh gateway for the second consilium run
    gateway_w2 = _build_weekly_mock_gateway(calls)
    service_w2 = WeeklyConsiliumService(db=sqlite_db_session, gateway=gateway_w2)
    res2 = await service_w2.run_weekly_consilium(
        cycle_id=cycle_id,
        sprint_id=sprint_2,
        report_date="2026-11-01",
    )
    assert len(calls) == 4
    assert res2.sprint_number == 2
    assert res2.sprint_id == sprint_2

    # Database now contains 2 completed sprints
    sprints = sprint_repo.list_by_cycle(cycle_id)
    assert len(sprints) == 2
    assert sprints[0].sprint_id == sprint_1
    assert sprints[1].sprint_id == sprint_2
    assert all(s.status == "completed" for s in sprints)


# ==============================================================================
# TEST 14: Foreign Sprint-ID Rejected Immediately (409 Conflict)
# ==============================================================================

@pytest.mark.anyio
async def test_weekly_foreign_sprint_id_rejected_before_reports(client_env):
    """
    Verifies that attempting to run or recover Weekly Consilium with a sprint_id
    belonging to a different cycle_id:
    1. Immediately raises 409 Conflict (ConsiliumSprintConflictError);
    2. Does so before reading or returning any reports;
    3. Tested on both service level and client API endpoint.
    """
    client, session = client_env

    pat_a = "pat_foreign_sprint_a"
    pat_b = "pat_foreign_sprint_b"
    token_bundle_a, token_a = _get_device_jwt_with_scopes(
        client, session, patient_id=pat_a, device_id="dev_fa_1", raw_code="111111", scope=f"{SCOPE_CASCADE_RUN} {SCOPE_REPORTS_READ}"
    )

    cycle_repo = MedicalCycleRepository(session)
    sprint_repo = WeeklySprintRepository(session)

    cycle_a, _ = cycle_repo.get_or_create_active_for_patient(pat_a)
    PatientRepository(session).create(patient_id=pat_b, status="active")
    cycle_b, _ = cycle_repo.get_or_create_active_for_patient(pat_b)

    # Patient B has a sprint
    sprint_b_id = "sprint_b_w1"
    sprint_repo.create(
        sprint_id=sprint_b_id,
        cycle_id=cycle_b.cycle_id,
        sprint_number=1,
        status="completed",
    )

    # Seed Patient A's primary consilium
    await _seed_completed_primary_consilium(session, cycle_id=cycle_a.cycle_id, primary_date="2026-10-18")
    _seed_seven_day_athena_logs(session, cycle_id=cycle_a.cycle_id, sprint_id="sprint_a_w1")

    service = WeeklyConsiliumService(db=session)

    # 1. Service recovery check with foreign sprint -> rejected with ConsiliumSprintConflictError
    with pytest.raises(ConsiliumSprintConflictError, match="belongs to cycle"):
        service.recover_completed_response(cycle_id=cycle_a.cycle_id, sprint_id=sprint_b_id)

    # 2. Service run check with foreign sprint -> rejected with ConsiliumSprintConflictError
    with pytest.raises(ConsiliumSprintConflictError, match="belongs to cycle"):
        await service.run_weekly_consilium(cycle_id=cycle_a.cycle_id, sprint_id=sprint_b_id)

    # 3. Client API POST /api/v1/client/cascade/run-weekly with foreign sprint -> returns 409 Conflict
    headers = {
        "Authorization": f"Bearer {token_a}",
        "X-Device-Id": "dev_fa_1",
    }
    resp = client.post(
        "/api/v1/client/cascade/run-weekly",
        json={"sprint_id": sprint_b_id},
        headers=headers,
    )
    assert resp.status_code == 409
    assert "different clinical cycle" in resp.json()["detail"] or "belongs to cycle" in resp.json()["detail"]


# ==============================================================================
# TEST 15: Week 2 Missing System Report Must Not Reuse Week 1 Report
# ==============================================================================

@pytest.mark.anyio
async def test_weekly_recovery_week2_missing_system_report_must_not_reuse_week1(
    sqlite_db_session: Session,
):
    """
    Verifies that if Week 2 is missing even ONE of the 8 system reports,
    recover_completed_response() safely refuses (returns None) instead of
    falling back to Week 1's report for that missing system.
    """
    cycle_id = "cycle_recovery_no_fallback_01"
    sprint_1 = "sprint_w1_complete"
    sprint_2 = "sprint_w2_incomplete"

    await _seed_completed_primary_consilium(sqlite_db_session, cycle_id=cycle_id, primary_date="2026-10-18")
    _seed_seven_day_athena_logs(sqlite_db_session, cycle_id=cycle_id, sprint_id=sprint_1, base_date="2026-10-18")

    # Run full Week 1 (all 8 system reports + overall report for sprint_1 exist)
    calls: List[Dict[str, Any]] = []
    gateway_w1 = _build_weekly_mock_gateway(calls)
    service = WeeklyConsiliumService(db=sqlite_db_session, gateway=gateway_w1)
    res1 = await service.run_weekly_consilium(
        cycle_id=cycle_id,
        sprint_id=sprint_1,
        report_date="2026-10-25",
    )
    assert len(res1.system_reports) == 8

    # Now create Week 2 with overall report and only 7 out of 8 system reports
    report_repo = ModelReportRepository(sqlite_db_session)
    sprint_repo = WeeklySprintRepository(sqlite_db_session)
    sprint_repo.create(
        sprint_id=sprint_2,
        cycle_id=cycle_id,
        sprint_number=2,
        status="completed",
    )
    report_repo.create_next_version(
        cycle_id=cycle_id,
        sprint_id=sprint_2,
        model="test-pro",
        report_type=WEEKLY_OVERALL_CLIENT_REPORT_TYPE,
        privacy_level=PrivacyLevel.CLIENT_FACING,
        content={
            "title": "Week 2 Incomplete Overall",
            "summary": "Week 2 summary",
            "report_date": "2026-11-01",
            "highlights": ["h1"],
            "metrics": {"sprint_number": 2},
        },
    )
    # Save only 7 system reports for sprint_2 (omit the last one: SYSTEM_KEYS[-1])
    for sys_key in SYSTEM_KEYS[:-1]:
        report_repo.create_next_version(
            cycle_id=cycle_id,
            sprint_id=sprint_2,
            model="test-flash",
            report_type=f"system_user_report_{sys_key}",
            privacy_level=PrivacyLevel.CLIENT_FACING,
            content={
                "title": f"{sys_key} Overview",
                "summary": f"{sys_key} week 2 update",
                "report_date": "2026-11-01",
                "system_key": sys_key,
                "has_changes": False,
                "entries": [{"date": "2026-11-01", "summary": f"{sys_key} week 2 update"}],
            },
        )

    # Attempting to recover Week 2 must NOT fall back to Week 1's missing system report!
    # It must return None (safe refusal due to incomplete set of 8 reports for sprint_2)
    recovered_w2 = service.recover_completed_response(cycle_id=cycle_id, sprint_id=sprint_2)
    assert recovered_w2 is None

    # Meanwhile, Week 1 remains fully recoverable with all 8 of its own reports
    recovered_w1 = service.recover_completed_response(cycle_id=cycle_id, sprint_id=sprint_1)
    assert recovered_w1 is not None
    assert recovered_w1.sprint_id == sprint_1
    assert len(recovered_w1.system_reports) == 8


# ==============================================================================
# TEST 16: Weekly Recovery Contains Reports from One Sprint Only
# ==============================================================================

@pytest.mark.anyio
async def test_weekly_recovery_contains_reports_from_one_sprint_only(
    sqlite_db_session: Session,
):
    """
    Verifies that when multiple completed weekly sprints exist in storage,
    recovering any sprint returns strictly reports belonging to that exact sprint_id,
    with zero contamination or mixing of reports across different sprints.
    """
    cycle_id = "cycle_recovery_single_sprint_01"
    sprint_1 = "sprint_iso_w1"
    sprint_2 = "sprint_iso_w2"

    await _seed_completed_primary_consilium(sqlite_db_session, cycle_id=cycle_id, primary_date="2026-10-18")
    _seed_seven_day_athena_logs(sqlite_db_session, cycle_id=cycle_id, sprint_id=sprint_1, base_date="2026-10-18")

    # Run Week 1
    calls_w1: List[Dict[str, Any]] = []
    gw1 = _build_weekly_mock_gateway(calls_w1)
    service1 = WeeklyConsiliumService(db=sqlite_db_session, gateway=gw1)
    await service1.run_weekly_consilium(
        cycle_id=cycle_id,
        sprint_id=sprint_1,
        report_date="2026-10-25",
    )

    # Run Week 2
    _seed_seven_day_athena_logs(sqlite_db_session, cycle_id=cycle_id, sprint_id=sprint_2, base_date="2026-10-25")
    calls_w2: List[Dict[str, Any]] = []
    gw2 = _build_weekly_mock_gateway(calls_w2)
    service2 = WeeklyConsiliumService(db=sqlite_db_session, gateway=gw2)
    await service2.run_weekly_consilium(
        cycle_id=cycle_id,
        sprint_id=sprint_2,
        report_date="2026-11-01",
    )

    # Recover Week 1: every system report must belong strictly to sprint_1
    rec1 = service1.recover_completed_response(cycle_id=cycle_id, sprint_id=sprint_1)
    assert rec1 is not None
    assert rec1.sprint_id == sprint_1
    for sys_key, sys_rep in rec1.system_reports.items():
        assert sys_rep.sprint_id == sprint_1

    # Recover Week 2: every system report must belong strictly to sprint_2
    rec2 = service2.recover_completed_response(cycle_id=cycle_id, sprint_id=sprint_2)
    assert rec2 is not None
    assert rec2.sprint_id == sprint_2
    for sys_key, sys_rep in rec2.system_reports.items():
        assert sys_rep.sprint_id == sprint_2


# ==============================================================================
# TEST 17: Heartbeat With One Missing Lock Rejects Execution & Rolls Back
# ==============================================================================

@pytest.mark.anyio
async def test_heartbeat_with_one_missing_lock_rejects_execution(sqlite_db_session: Session):
    """
    Verifies that _renew_cross_process_lease() enforces res.rowcount == 2.
    If one of the two lock rows (e.g. lock_consilium_coord_{cycle_id}) was deleted
    or usurped, the update affects only 1 row (!= 2).
    The service must immediately roll back the transaction and raise
    ConsiliumConcurrencyConflictError, stopping execution.
    """
    cycle_id = "cycle_heartbeat_partial_loss_01"
    PatientRepository(sqlite_db_session).create(patient_id="pat_hb_01", status="active")
    MedicalCycleRepository(sqlite_db_session).create(cycle_id=cycle_id, patient_id="pat_hb_01", status="active")

    weekly_service = WeeklyConsiliumService(db=sqlite_db_session)
    lease_owner = weekly_service._acquire_cross_process_lease(cycle_id)

    # Simulate that one of the two lock records (the coordination lock) was deleted
    coord_id = f"lock_consilium_coord_{cycle_id}"[:64]
    sqlite_db_session.execute(
        delete(ModelReport).where(ModelReport.report_id == coord_id)
    )
    sqlite_db_session.commit()

    # Now attempt heartbeat renewal: only 1 row remains, so res.rowcount == 1 (!= 2)
    with pytest.raises(ConsiliumConcurrencyConflictError, match="partially or completely lost"):
        weekly_service._renew_cross_process_lease(cycle_id, lease_owner)

    # Primary consilium service must enforce the exact same rule
    cycle_id_p = "cycle_heartbeat_primary_partial_01"
    MedicalCycleRepository(sqlite_db_session).create(cycle_id=cycle_id_p, patient_id="pat_hb_01", status="active")
    primary_service = PrimaryConsiliumService(db=sqlite_db_session)
    lease_p = primary_service._acquire_cross_process_lease(cycle_id_p)

    coord_id_p = f"lock_consilium_coord_{cycle_id_p}"[:64]
    sqlite_db_session.execute(
        delete(ModelReport).where(ModelReport.report_id == coord_id_p)
    )
    sqlite_db_session.commit()

    with pytest.raises(ConsiliumConcurrencyConflictError, match="partially or completely lost"):
        primary_service._renew_cross_process_lease(cycle_id_p, lease_p)


# ==============================================================================
# TEST 18: Stale Worker Cannot Publish After Partial Lease Loss
# ==============================================================================

@pytest.mark.anyio
async def test_stale_worker_cannot_publish_after_partial_lease_loss(sqlite_db_session: Session):
    """
    Verifies that during atomic uncommitted verification (_verify_and_delete_cross_process_lease_uncommitted),
    if Worker A has lost even one of its two lock rows (e.g. rowcount == 1 < 2),
    it raises ConsiliumConcurrencyConflictError, aborting publication and triggering a rollback.
    """
    cycle_id = "cycle_fencing_partial_loss_01"
    PatientRepository(sqlite_db_session).create(patient_id="pat_fencing_partial_01", status="active")
    MedicalCycleRepository(sqlite_db_session).create(cycle_id=cycle_id, patient_id="pat_fencing_partial_01", status="active")

    service = WeeklyConsiliumService(db=sqlite_db_session)
    lease_owner = service._acquire_cross_process_lease(cycle_id)

    # Simulate partial loss: coordination lock is stolen by Worker B
    coord_id = f"lock_consilium_coord_{cycle_id}"[:64]
    sqlite_db_session.execute(
        update(ModelReport)
        .where(ModelReport.report_id == coord_id)
        .values(content={"lease_owner_id": "lease_weekly_usurper_b"})
    )
    sqlite_db_session.commit()

    # Worker A attempts uncommitted fencing check: only 1 row deleted matching lease_owner
    with pytest.raises(ConsiliumConcurrencyConflictError, match="lost or superseded"):
        service._verify_and_delete_cross_process_lease_uncommitted(cycle_id, lease_owner)

    # Same rule for PrimaryConsiliumService
    cycle_id_p = "cycle_fencing_primary_partial_01"
    MedicalCycleRepository(sqlite_db_session).create(cycle_id=cycle_id_p, patient_id="pat_fencing_partial_01", status="active")
    prim_service = PrimaryConsiliumService(db=sqlite_db_session)
    lease_prim = prim_service._acquire_cross_process_lease(cycle_id_p)

    coord_id_p = f"lock_consilium_coord_{cycle_id_p}"[:64]
    sqlite_db_session.execute(
        update(ModelReport)
        .where(ModelReport.report_id == coord_id_p)
        .values(content={"lease_owner_id": "lease_primary_usurper_b"})
    )
    sqlite_db_session.commit()

    with pytest.raises(ConsiliumConcurrencyConflictError, match="lost or superseded"):
        prim_service._verify_and_delete_cross_process_lease_uncommitted(cycle_id_p, lease_prim)


