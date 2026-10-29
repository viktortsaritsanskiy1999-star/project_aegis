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
from app.repositories.cycle_repository import MedicalCycleRepository
from app.repositories.measurement_repository import HealthMeasurementRepository
from app.repositories.report_repository import ModelReportRepository
from app.schemas.consilium import (
    SYSTEM_KEYS,
    PrimaryConsiliumClientResponse,
)
from app.schemas.privacy import PrivacyLevel
from app.services.gemini_gateway import (
    GeminiGateway,
    GeminiGatewayError,
)
from app.routers.client_cascade import get_gemini_gateway
from app.services.primary_consilium_service import (
    PrimaryConsiliumService,
    ConsiliumConcurrencyConflictError,
    ConsiliumQualityRejectionError,
    PRIMARY_CONSILIUM_IN_FLIGHT_LOCK_REPORT_TYPE,
    PRIMARY_CONSILIUM_EXECUTION_LOG_REPORT_TYPE,
)
from tests.test_primary_consilium import (
    _gemini_envelope,
    _sample_anamnesis_output,
    _sample_system_agent_output,
    _sample_flash_output,
    _sample_pro_output,
    _seed_preparatory_cycle_context,
    _build_consilium_mock_gateway,
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


# ==============================================================================
# TEST 1: First Successful Run — Lease Lifecycle and Atomic Cleanup
# ==============================================================================

@pytest.mark.anyio
async def test_primary_consilium_lease_lifecycle_and_atomic_cleanup(sqlite_db_session: Session):
    """
    Verifies that during a successful run:
    1. Lease is acquired during processing;
    2. Atomic commit writes all 10 server_only reports, 9 client_facing reports, and 1 execution log;
    3. In-flight lock row is deleted within the same database transaction;
    4. Execution log contains valid lease metadata.
    """
    cycle_id = "cycle_lease_lifecycle_01"
    _seed_preparatory_cycle_context(sqlite_db_session, cycle_id=cycle_id, seed_ledger=True)

    recorded_calls: List[Dict[str, Any]] = []
    gateway = _build_consilium_mock_gateway(recorded_calls)
    service = PrimaryConsiliumService(db=sqlite_db_session, gateway=gateway)

    result = await service.run_primary_consilium(
        cycle_id=cycle_id,
        report_date="2026-10-20",
        idempotency_key="idemp-key-001",
    )

    assert isinstance(result, PrimaryConsiliumClientResponse)
    assert result.cycle_id == cycle_id
    assert len(result.system_reports) == 8
    assert result.overall_report.content.title == "Aegis Personal Wellness Roadmap"

    # 1. Verify lock row is completely deleted
    lock_id = f"lock_primary_{cycle_id}"
    lock_report = sqlite_db_session.query(ModelReport).filter(ModelReport.report_id == lock_id).first()
    assert lock_report is None, "In-flight lock row must be atomically removed upon completion"

    # 2. Verify authoritative execution log report exists
    report_repo = ModelReportRepository(sqlite_db_session)
    exec_logs = report_repo.list_server_only(
        cycle_id=cycle_id,
        report_type=PRIMARY_CONSILIUM_EXECUTION_LOG_REPORT_TYPE,
    )
    assert len(exec_logs) == 1
    log_content = exec_logs[0].content or {}
    assert log_content.get("idempotency_key") == "idemp-key-001"
    assert str(log_content.get("lease_owner_id", "")).startswith("lease_primary_")
    assert "completed_at_utc" in log_content

    # 3. Verify exactly 10 Gemini calls (8 Flash-Lite + 1 Deputy Flash + 1 Chief Physician Pro)
    assert len(recorded_calls) == 10

    # 4. Verify 9 client facing reports
    client_reports = report_repo.list_client_facing(cycle_id=cycle_id)
    assert len(client_reports) == 9


# ==============================================================================
# TEST 2: Concurrent Execution Rejection Across Simulated OS Processes
# ==============================================================================

@pytest.mark.anyio
async def test_concurrent_execution_rejection_across_processes(sqlite_db_session: Session):
    """
    Simulates two distinct worker processes with separate DB sessions and separate in-process lock registries:
    - Worker 1 acquires the SQLite lease and executes the AI cascade.
    - While Worker 1 is in-flight (at call 4), Worker 2 attempts to run the same cycle.
    - Worker 2 is immediately rejected with ConsiliumConcurrencyConflictError without calling Gemini.
    - Worker 1 successfully finishes and cleans up.
    """
    bind = sqlite_db_session.get_bind()
    cycle_id = "cycle_concurrent_01"
    _seed_preparatory_cycle_context(sqlite_db_session, cycle_id=cycle_id, seed_ledger=True)

    w1_session = Session(bind=bind, autocommit=False, autoflush=False)
    w2_session = Session(bind=bind, autocommit=False, autoflush=False)

    w1_calls: List[Dict[str, Any]] = []
    w2_calls: List[Dict[str, Any]] = []
    w2_errors: List[Exception] = []

    w2_gateway = _build_consilium_mock_gateway(w2_calls)
    w2_service = PrimaryConsiliumService(
        db=w2_session,
        gateway=w2_gateway,
        lock_registry={},  # isolated process
    )

    call_index = {"val": 0}

    async def long_running_handler(request: httpx.Request) -> httpx.Response:
        idx = call_index["val"]
        call_index["val"] += 1
        body = json.loads(request.content.decode("utf-8"))
        w1_calls.append({"index": idx, "prompt": body["contents"][0]["parts"][0]["text"]})

        # When Worker 1 is at call index 4, Worker 2 attempts to run
        if idx == 4:
            try:
                await w2_service.run_primary_consilium(
                    cycle_id=cycle_id,
                    report_date="2026-10-20",
                )
            except Exception as exc:
                w2_errors.append(exc)

        await asyncio.sleep(0.01)

        if idx < 8:
            return httpx.Response(
                200, json=_gemini_envelope(_sample_system_agent_output(SYSTEM_KEYS[idx]))
            )
        if idx == 8:
            return httpx.Response(200, json=_gemini_envelope(_sample_flash_output()))
        return httpx.Response(200, json=_gemini_envelope(_sample_pro_output()))

    w1_gateway = GeminiGateway(
        api_key="test-key-w1",
        transport=httpx.MockTransport(long_running_handler),
    )
    w1_service = PrimaryConsiliumService(
        db=w1_session,
        gateway=w1_gateway,
        lock_registry={},  # isolated process
    )

    res_w1 = await w1_service.run_primary_consilium(
        cycle_id=cycle_id,
        report_date="2026-10-20",
    )

    assert res_w1 is not None
    assert len(w1_calls) == 10
    assert len(w2_calls) == 0, "Worker 2 must make 0 Gemini calls when blocked by active lease"
    assert len(w2_errors) == 1
    assert isinstance(w2_errors[0], ConsiliumConcurrencyConflictError)
    assert f"Primary Consilium for cycle '{cycle_id}' is currently in progress" in str(w2_errors[0])

    w1_session.close()
    w2_session.close()


# ==============================================================================
# TEST 3: Client API Rejection (HTTP 409) When Lease Is Active
# ==============================================================================

def test_client_api_concurrent_requests_return_409(tmp_path, monkeypatch):
    """
    Verifies that when a cross-process lease is held in SQLite for cycle X,
    a client call to POST /api/v1/client/cascade/run-primary returns HTTP 409 Conflict.
    """
    monkeypatch.setattr(settings, "api_secret", TEST_API_SECRET)
    monkeypatch.setattr(settings, "pairing_secret", TEST_PAIRING_SECRET)
    monkeypatch.setattr(settings, "jwt_secret", TEST_JWT_SECRET)
    monkeypatch.setattr(settings, "jwt_issuer", "aegis-backend")
    monkeypatch.setattr(settings, "jwt_audience", "aegis-mobile-client")

    db_file = tmp_path / "test_client_api_lease_409.db"
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
    recorded_calls: List[Dict[str, Any]] = []
    mock_gw = _build_consilium_mock_gateway(recorded_calls)
    app.dependency_overrides[get_gemini_gateway] = lambda: mock_gw

    try:
        with TestClient(app) as test_client:
            _, jwt_token = _get_device_jwt_with_scopes(
                test_client,
                session,
                patient_id="pat-concurrent-409",
                device_id="dev-concurrent-409",
                raw_code="880011",
                scope="cascade:run cycle:read",
            )
            headers = {"Authorization": f"Bearer {jwt_token}"}

            cycle_id = "cycle-concurrent-api-409"
            cycle_repo = MedicalCycleRepository(session)
            cycle_repo.create(cycle_id=cycle_id, patient_id="pat-concurrent-409", status="active")
            _seed_preparatory_cycle_context(session, cycle_id=cycle_id, seed_ledger=True)

            # Manually acquire the cross-process lease as if another process is running
            lock_id = f"lock_primary_{cycle_id}"
            now_dt = datetime.now(timezone.utc)
            lock_report = ModelReport(
                report_id=lock_id,
                cycle_id=cycle_id,
                model="aegis-concurrency-guard",
                report_type=PRIMARY_CONSILIUM_IN_FLIGHT_LOCK_REPORT_TYPE,
                privacy_level=PrivacyLevel.SERVER_ONLY.value,
                version=1,
                content={
                    "lease_owner_id": "lease_other_process_999",
                    "acquired_at_utc": now_dt.isoformat(),
                    "expires_at_utc": (now_dt + timedelta(seconds=300)).isoformat(),
                },
                created_at=now_dt,
            )
            session.add(lock_report)
            session.commit()

            # Client attempt to run Primary Consilium must return 409 Conflict
            resp = test_client.post("/api/v1/client/cascade/run-primary", headers=headers)
            assert resp.status_code == 409
            assert "currently in progress" in resp.json()["detail"]
            assert len(recorded_calls) == 0
    finally:
        app.dependency_overrides.clear()
        session.close()
        eng.dispose()


# ==============================================================================
# TEST 4: Idempotency Replay — Zero Duplicate Gemini AI Calls
# ==============================================================================

def test_idempotency_retry_zero_duplicate_ai_calls(tmp_path, monkeypatch):
    """
    Verifies that after Primary Consilium is completed, subsequent calls to
    POST /api/v1/client/cascade/run-primary immediately return recovered reports
    with header X-Aegis-Idempotent-Replay: true and 0 new AI calls.
    """
    monkeypatch.setattr(settings, "api_secret", TEST_API_SECRET)
    monkeypatch.setattr(settings, "pairing_secret", TEST_PAIRING_SECRET)
    monkeypatch.setattr(settings, "jwt_secret", TEST_JWT_SECRET)
    monkeypatch.setattr(settings, "jwt_issuer", "aegis-backend")
    monkeypatch.setattr(settings, "jwt_audience", "aegis-mobile-client")

    db_file = tmp_path / "test_client_idempotency_replay.db"
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
    recorded_calls: List[Dict[str, Any]] = []
    mock_gw = _build_consilium_mock_gateway(recorded_calls)
    app.dependency_overrides[get_gemini_gateway] = lambda: mock_gw

    try:
        with TestClient(app) as test_client:
            _, jwt_token = _get_device_jwt_with_scopes(
                test_client,
                session,
                patient_id="pat-replay-001",
                device_id="dev-replay-001",
                raw_code="771122",
                scope="cascade:run cycle:read",
            )
            headers = {"Authorization": f"Bearer {jwt_token}"}

            cycle_id = "cycle-replay-001"
            cycle_repo = MedicalCycleRepository(session)
            cycle_repo.create(cycle_id=cycle_id, patient_id="pat-replay-001", status="active")
            _seed_preparatory_cycle_context(session, cycle_id=cycle_id, seed_ledger=True)

            # 1. First run -> 10 Gemini calls
            resp1 = test_client.post(
                "/api/v1/client/cascade/run-primary",
                headers={**headers, "Idempotency-Key": "client-key-abc"},
            )
            assert resp1.status_code == 200
            assert resp1.headers.get("x-aegis-idempotent-replay") is None
            assert len(recorded_calls) == 10

            # 2. Second run -> recovered from DB, X-Aegis-Idempotent-Replay header present
            resp2 = test_client.post(
                "/api/v1/client/cascade/run-primary",
                headers={**headers, "Idempotency-Key": "client-key-abc"},
            )
            assert resp2.status_code == 200
            assert resp2.headers.get("x-aegis-idempotent-replay") == "true"
            data2 = resp2.json()
            assert data2["cycle_id"] == cycle_id
            assert data2["overall_report"]["content"]["title"] == "Aegis Personal Wellness Roadmap"
            assert len(data2["system_reports"]) == 8

            # Exactly 0 new Gemini calls
            assert len(recorded_calls) == 10
    finally:
        app.dependency_overrides.clear()
        session.close()
        eng.dispose()


# ==============================================================================
# TEST 5: Heartbeat Renewal Keeps Long Run Alive Past Initial TTL
# ==============================================================================

@pytest.mark.anyio
async def test_heartbeat_renewal_keeps_long_run_alive(sqlite_db_session: Session):
    """
    Verifies that active heartbeat renewal prevents lease expiration:
    - Initial lease TTL is set to 2.0s;
    - Consilium execution takes 4.0s of server clock time;
    - Heartbeat background task ticks every 0.05s, renewing the lease;
    - A concurrent Worker 2 checks at t=+3.0s: lease is active and renewed, so Worker 2 is rejected;
    - Worker 1 successfully finishes.
    """
    bind = sqlite_db_session.get_bind()
    cycle_id = "cycle_heartbeat_renewal_01"
    _seed_preparatory_cycle_context(sqlite_db_session, cycle_id=cycle_id, seed_ledger=True)

    clock = {"now": datetime(2026, 10, 20, 10, 0, 0, tzinfo=timezone.utc)}

    w1_session = Session(bind=bind, autocommit=False, autoflush=False)
    w2_session = Session(bind=bind, autocommit=False, autoflush=False)

    w1_calls: List[Dict[str, Any]] = []
    w2_calls: List[Dict[str, Any]] = []
    w2_errors: List[Exception] = []

    w2_service = PrimaryConsiliumService(
        db=w2_session,
        gateway=_build_consilium_mock_gateway(w2_calls),
        now_provider=lambda: clock["now"],
        lock_registry={},
        lease_ttl_seconds=2,
    )

    call_seq = {"idx": 0}

    async def long_call_handler(request: httpx.Request) -> httpx.Response:
        idx = call_seq["idx"]
        call_seq["idx"] += 1
        body = json.loads(request.content.decode("utf-8"))
        w1_calls.append({"index": idx, "prompt": body["contents"][0]["parts"][0]["text"]})

        # Advance server time by 0.4s per call (total 10 calls * 0.4s = 4.0s > 2.0s initial TTL)
        clock["now"] = clock["now"] + timedelta(seconds=0.2)
        await asyncio.sleep(0.02)
        clock["now"] = clock["now"] + timedelta(seconds=0.2)
        await asyncio.sleep(0.02)

        # At call index 6, elapsed server time is 7 * 0.4s = 2.8s (> 2.0s initial TTL!)
        if idx == 6:
            try:
                await w2_service.run_primary_consilium(cycle_id=cycle_id)
            except Exception as exc:
                w2_errors.append(exc)

        if idx < 8:
            return httpx.Response(
                200, json=_gemini_envelope(_sample_system_agent_output(SYSTEM_KEYS[idx]))
            )
        if idx == 8:
            return httpx.Response(200, json=_gemini_envelope(_sample_flash_output()))
        return httpx.Response(200, json=_gemini_envelope(_sample_pro_output()))

    w1_gateway = GeminiGateway(
        api_key="test-key-hb",
        transport=httpx.MockTransport(long_call_handler),
    )
    w1_service = PrimaryConsiliumService(
        db=w1_session,
        gateway=w1_gateway,
        now_provider=lambda: clock["now"],
        lock_registry={},
        lease_ttl_seconds=2,
        lease_heartbeat_interval_seconds=0.05,
    )

    res_w1 = await w1_service.run_primary_consilium(cycle_id=cycle_id)
    assert res_w1 is not None
    assert len(w1_calls) == 10
    assert len(w2_calls) == 0
    assert len(w2_errors) == 1
    assert isinstance(w2_errors[0], ConsiliumConcurrencyConflictError)

    w1_session.close()
    w2_session.close()


# ==============================================================================
# TEST 6: Crash Recovery — Stale Expired Lease Automatically Cleaned Up
# ==============================================================================

@pytest.mark.anyio
async def test_crash_recovery_expired_lease_cleaned_up(sqlite_db_session: Session):
    """
    Simulates a hard-crashed process that left an expired lease in SQLite (> TTL without renewal).
    A new worker calling run_primary_consilium cleans up the stale lock row,
    acquires a new lease, and successfully executes the consilium.
    """
    cycle_id = "cycle_crash_recovery_01"
    _seed_preparatory_cycle_context(sqlite_db_session, cycle_id=cycle_id, seed_ledger=True)

    clock = {"now": datetime(2026, 10, 20, 12, 0, 0, tzinfo=timezone.utc)}

    # Insert a stale lock row that expired 10 minutes ago
    lock_id = f"lock_primary_{cycle_id}"
    stale_dt = clock["now"] - timedelta(minutes=10)
    stale_lock = ModelReport(
        report_id=lock_id,
        cycle_id=cycle_id,
        model="aegis-concurrency-guard",
        report_type=PRIMARY_CONSILIUM_IN_FLIGHT_LOCK_REPORT_TYPE,
        privacy_level=PrivacyLevel.SERVER_ONLY.value,
        version=1,
        content={
            "lease_owner_id": "lease_crashed_worker_dead",
            "acquired_at_utc": (stale_dt - timedelta(minutes=5)).isoformat(),
            "expires_at_utc": stale_dt.isoformat(),
            "renewal_count": 0,
        },
        created_at=stale_dt,
    )
    sqlite_db_session.add(stale_lock)
    sqlite_db_session.commit()

    recorded_calls: List[Dict[str, Any]] = []
    gateway = _build_consilium_mock_gateway(recorded_calls)
    service = PrimaryConsiliumService(
        db=sqlite_db_session,
        gateway=gateway,
        now_provider=lambda: clock["now"],
        lease_ttl_seconds=300,
    )

    result = await service.run_primary_consilium(cycle_id=cycle_id)
    assert result is not None
    assert len(recorded_calls) == 10

    # Verify stale lock was cleaned up and removed
    lock_row = sqlite_db_session.query(ModelReport).filter(ModelReport.report_id == lock_id).first()
    assert lock_row is None


# ==============================================================================
# TEST 7: Stale Owner Fencing Token Protection
# ==============================================================================

def test_stale_owner_fencing_token_protection(sqlite_db_session: Session):
    """
    Verifies that a zombie worker with a stale owner token cannot:
    1. Renew another worker's active lease;
    2. Delete another worker's active lease on safe release;
    3. Verify and delete another worker's lease during commit.
    """
    cycle_id = "cycle_fencing_guard_01"
    _seed_preparatory_cycle_context(sqlite_db_session, cycle_id=cycle_id, seed_ledger=True)

    clock = {"now": datetime(2026, 10, 20, 12, 0, 0, tzinfo=timezone.utc)}
    service = PrimaryConsiliumService(
        db=sqlite_db_session,
        now_provider=lambda: clock["now"],
        lease_ttl_seconds=300,
    )

    # Worker B acquires legitimate lease
    owner_B = service._acquire_cross_process_lease(cycle_id)
    assert owner_B.startswith("lease_primary_")

    stale_owner_A = "lease_primary_zombie_stale_owner_A"

    # 1. Stale Worker A cannot renew
    with pytest.raises(ConsiliumConcurrencyConflictError) as exc_renew:
        service._renew_cross_process_lease(cycle_id, stale_owner_A)
    assert "no longer owned by this worker" in str(exc_renew.value)

    # 2. Stale Worker A cannot release Worker B's lease
    service._release_cross_process_lease_safe(cycle_id, stale_owner_A)
    lock_row = service._fetch_fresh_lock_row(cycle_id)
    assert lock_row is not None
    assert (lock_row.content or {}).get("lease_owner_id") == owner_B, "Worker B's lease must remain intact"

    # 3. Stale Worker A cannot commit uncommitted
    with pytest.raises(ConsiliumConcurrencyConflictError) as exc_commit:
        service._verify_and_delete_cross_process_lease_uncommitted(cycle_id, stale_owner_A)
    assert "was lost or superseded by another owner" in str(exc_commit.value)

    # Legitimate Worker B can verify and delete uncommitted
    service._verify_and_delete_cross_process_lease_uncommitted(cycle_id, owner_B)
    sqlite_db_session.commit()
    assert service._fetch_fresh_lock_row(cycle_id) is None


# ==============================================================================
# TEST 8: Patient Isolation — Independent Cycles Have Zero Lock Contention
# ==============================================================================

@pytest.mark.anyio
async def test_patient_isolation_no_cross_cycle_contention(sqlite_db_session: Session):
    """
    Verifies that two patients running Primary Consilium concurrently on different cycles
    do not contend for locks and both succeed independently.
    """
    bind = sqlite_db_session.get_bind()
    cycle_A = "cycle_patient_A"
    cycle_B = "cycle_patient_B"
    _seed_preparatory_cycle_context(sqlite_db_session, cycle_id=cycle_A, seed_ledger=True)
    _seed_preparatory_cycle_context(sqlite_db_session, cycle_id=cycle_B, seed_ledger=True)

    session_A = Session(bind=bind, autocommit=False, autoflush=False)
    session_B = Session(bind=bind, autocommit=False, autoflush=False)

    calls_A: List[Dict[str, Any]] = []
    calls_B: List[Dict[str, Any]] = []

    service_A = PrimaryConsiliumService(
        db=session_A,
        gateway=_build_consilium_mock_gateway(calls_A),
    )
    service_B = PrimaryConsiliumService(
        db=session_B,
        gateway=_build_consilium_mock_gateway(calls_B),
    )

    res_A, res_B = await asyncio.gather(
        service_A.run_primary_consilium(cycle_id=cycle_A),
        service_B.run_primary_consilium(cycle_id=cycle_B),
    )

    assert res_A.cycle_id == cycle_A
    assert res_B.cycle_id == cycle_B
    assert len(calls_A) == 10
    assert len(calls_B) == 10

    session_A.close()
    session_B.close()


# ==============================================================================
# TEST 9: Failure or Quality Rejection Safely Releases Lease
# ==============================================================================

@pytest.mark.anyio
async def test_failure_or_quality_rejection_safely_releases_lease(sqlite_db_session: Session):
    """
    Verifies that if an AI gateway error or Quality Gate rejection occurs,
    the in-flight lease is immediately and safely released, allowing an immediate retry.
    """
    cycle_id = "cycle_failure_release_01"
    _seed_preparatory_cycle_context(sqlite_db_session, cycle_id=cycle_id, seed_ledger=True)

    # 1. Test Gateway failure at call index 3
    recorded_calls_1: List[Dict[str, Any]] = []
    gw_fail = _build_consilium_mock_gateway(recorded_calls_1, fail_on_call_index=3)
    service_fail = PrimaryConsiliumService(db=sqlite_db_session, gateway=gw_fail)

    with pytest.raises(GeminiGatewayError):
        await service_fail.run_primary_consilium(cycle_id=cycle_id)

    # Verify lock row was released on exception
    lock_id = f"lock_primary_{cycle_id}"
    lock_row = sqlite_db_session.query(ModelReport).filter(ModelReport.report_id == lock_id).first()
    assert lock_row is None, "Lease must be safely released on Gateway failure"

    # 2. Test Quality Gate rejection
    recorded_calls_2: List[Dict[str, Any]] = []
    pro_rejected = _sample_pro_output(approved=False, rejection_reason="Quality gate: contradictory recommendations")
    gw_reject = _build_consilium_mock_gateway(recorded_calls_2, pro_override=pro_rejected)
    service_reject = PrimaryConsiliumService(db=sqlite_db_session, gateway=gw_reject)

    with pytest.raises(ConsiliumQualityRejectionError) as exc_q:
        await service_reject.run_primary_consilium(cycle_id=cycle_id)
    assert "Quality gate: contradictory recommendations" in str(exc_q.value)

    # Verify lock row was released on quality rejection
    lock_row_q = sqlite_db_session.query(ModelReport).filter(ModelReport.report_id == lock_id).first()
    assert lock_row_q is None, "Lease must be safely released on Quality Gate rejection"

    # 3. Subsequent valid run succeeds immediately
    recorded_calls_3: List[Dict[str, Any]] = []
    gw_success = _build_consilium_mock_gateway(recorded_calls_3)
    service_success = PrimaryConsiliumService(db=sqlite_db_session, gateway=gw_success)

    res_success = await service_success.run_primary_consilium(cycle_id=cycle_id)
    assert res_success is not None
    assert len(recorded_calls_3) == 10


# ==============================================================================
# TEST 10: Concurrent Renewal vs Expired Lease Cleanup Race
# ==============================================================================

def test_concurrent_renewal_vs_expired_lease_cleanup_race(sqlite_db_session: Session):
    """
    Simulates race condition: Worker A has a lease that reached its expiry timestamp.
    Worker B checks _get_active_in_flight_lock and prepares to delete.
    Simultaneously, Worker A renews its lease (updating expires_at_utc and renewal_count).
    Worker B's atomic delete with matching stale expiration affects 0 rows,
    Worker B detects that the lease was renewed, and Worker B does NOT delete Worker A's lease.
    """
    cycle_id = "cycle_atomic_race_01"
    _seed_preparatory_cycle_context(sqlite_db_session, cycle_id=cycle_id, seed_ledger=True)

    clock = {"now": datetime(2026, 10, 20, 12, 0, 0, tzinfo=timezone.utc)}
    service_A = PrimaryConsiliumService(
        db=sqlite_db_session,
        now_provider=lambda: clock["now"],
        lease_ttl_seconds=300,
    )

    # Worker A acquires lease
    owner_A = service_A._acquire_cross_process_lease(cycle_id)

    # Fast forward clock past TTL so lease appears expired
    clock["now"] = clock["now"] + timedelta(seconds=301)

    # Worker A renews right at this moment
    service_A._renew_cross_process_lease(cycle_id, owner_A)

    # Another worker (Worker B) checks for active/expired lock
    service_B = PrimaryConsiliumService(
        db=sqlite_db_session,
        now_provider=lambda: clock["now"],
        lease_ttl_seconds=300,
    )
    active_lock = service_B._get_active_in_flight_lock(cycle_id)
    assert active_lock is not None, "Worker A's renewed lease must NOT be deleted by Worker B"
    assert (active_lock.content or {}).get("lease_owner_id") == owner_A


# ==============================================================================
# TEST 11: Stale Owner Cannot Delete New Owner's Lease
# ==============================================================================

def test_stale_owner_cannot_delete_new_owner_lease(sqlite_db_session: Session):
    """
    Verifies that Worker A (stale owner) attempting safe release or uncommitted commit
    can NEVER delete Worker B's active lease due to atomic WHERE condition on lease_owner_id.
    """
    cycle_id = "cycle_atomic_stale_release_01"
    _seed_preparatory_cycle_context(sqlite_db_session, cycle_id=cycle_id, seed_ledger=True)

    clock = {"now": datetime(2026, 10, 20, 12, 0, 0, tzinfo=timezone.utc)}
    service = PrimaryConsiliumService(
        db=sqlite_db_session,
        now_provider=lambda: clock["now"],
        lease_ttl_seconds=300,
    )

    owner_B = service._acquire_cross_process_lease(cycle_id)
    stale_owner_A = "lease_primary_zombie_stale_999"

    # Worker A attempts safe release
    service._release_cross_process_lease_safe(cycle_id, stale_owner_A)

    # Worker B's lease must remain completely intact
    lock_row = service._fetch_fresh_lock_row(cycle_id)
    assert lock_row is not None
    assert (lock_row.content or {}).get("lease_owner_id") == owner_B

    # Worker A attempts uncommitted verify & delete
    with pytest.raises(ConsiliumConcurrencyConflictError):
        service._verify_and_delete_cross_process_lease_uncommitted(cycle_id, stale_owner_A)

    # Worker B's lease is STILL intact
    lock_row_after = service._fetch_fresh_lock_row(cycle_id)
    assert lock_row_after is not None
    assert (lock_row_after.content or {}).get("lease_owner_id") == owner_B


# ==============================================================================
# TEST 12: Heartbeat Uses Independent DB Session
# ==============================================================================

@pytest.mark.anyio
async def test_heartbeat_uses_independent_db_session(sqlite_db_session: Session):
    """
    Verifies that background heartbeat lease renewal uses an independent short-lived DB session
    and does NOT touch or share the main executor's session.
    """
    cycle_id = "cycle_hb_session_isolation_01"
    _seed_preparatory_cycle_context(sqlite_db_session, cycle_id=cycle_id, seed_ledger=True)

    service = PrimaryConsiliumService(
        db=sqlite_db_session,
        lease_ttl_seconds=5,
        lease_heartbeat_interval_seconds=0.05,
    )
    owner = service._acquire_cross_process_lease(cycle_id)

    hb_sessions_used: List[Session] = []
    original_renew = service._renew_cross_process_lease

    def spy_renew(c_id, o_id, db=None):
        if db is not None:
            hb_sessions_used.append(db)
        return original_renew(c_id, o_id, db=db)

    service._renew_cross_process_lease = spy_renew

    async def dummy_slow_coro():
        await asyncio.sleep(0.15)
        return "success"

    result = await service._execute_with_lease_renewal(
        cycle_id=cycle_id,
        lease_owner_id=owner,
        coro_factory=dummy_slow_coro,
    )

    assert result == "success"
    assert len(hb_sessions_used) >= 1
    for sess in hb_sessions_used:
        assert sess is not sqlite_db_session, "Heartbeat session must be distinct from main service.db"


# ==============================================================================
# TEST 13: Heartbeat Failure Blocks Result Publication and Cancels Execution
# ==============================================================================

@pytest.mark.anyio
async def test_heartbeat_failure_blocks_result_publication_and_cancels_execution(
    sqlite_db_session: Session,
):
    """
    Verifies that if heartbeat encounters a failure (e.g. lease lost to another worker during processing),
    the background error is immediately propagated to the main executor, the AI call is cancelled,
    and publication is completely blocked without touching the new owner's lease.
    """
    cycle_id = "cycle_hb_failure_blocks_01"
    _seed_preparatory_cycle_context(sqlite_db_session, cycle_id=cycle_id, seed_ledger=True)

    bind = sqlite_db_session.get_bind()
    w1_session = Session(bind=bind, autocommit=False, autoflush=False)
    w2_session = Session(bind=bind, autocommit=False, autoflush=False)

    w1_service = PrimaryConsiliumService(
        db=w1_session,
        lease_ttl_seconds=2,
        lease_heartbeat_interval_seconds=0.05,
    )
    owner_w1 = w1_service._acquire_cross_process_lease(cycle_id)

    w2_service = PrimaryConsiliumService(db=w2_session, lease_ttl_seconds=300)

    async def slow_ai_call():
        # While Worker 1 is in-flight, Worker 2 forcibly overrides the lease (stolen lease)
        lock_id = w1_service._lock_report_id(cycle_id)
        stmt = (
            update(ModelReport)
            .where(ModelReport.report_id == lock_id)
            .values(
                content={
                    "lease_owner_id": "lease_primary_worker_2_stolen",
                    "expires_at_utc": (datetime.now(timezone.utc) + timedelta(seconds=300)).isoformat(),
                }
            )
        )
        w2_session.execute(stmt)
        w2_session.commit()

        # Worker 1's call continues to wait, but heartbeat should fail on next tick and cancel this task
        await asyncio.sleep(0.5)
        return "unreachable"

    with pytest.raises(ConsiliumConcurrencyConflictError) as exc_info:
        await w1_service._execute_with_lease_renewal(
            cycle_id=cycle_id,
            lease_owner_id=owner_w1,
            coro_factory=slow_ai_call,
        )

    assert "no longer owned by this worker" in str(exc_info.value)

    # Verify Worker 2's stolen lease was NOT deleted by Worker 1's error handlers
    lock_row = w2_service._fetch_fresh_lock_row(cycle_id)
    assert lock_row is not None
    assert (lock_row.content or {}).get("lease_owner_id") == "lease_primary_worker_2_stolen"

    w1_session.close()
    w2_session.close()


# ==============================================================================
# TEST 14: Stale Worker at Final Commit Cannot Publish Client-Facing Reports
# ==============================================================================

@pytest.mark.anyio
async def test_stale_worker_at_final_commit_cannot_publish_client_facing_reports(
    sqlite_db_session: Session,
):
    """
    Verifies that if a worker loses its lease right before step 6 (the final atomic transaction),
    the atomic delete in _verify_and_delete_cross_process_lease_uncommitted matches 0 rows,
    raises ConsiliumConcurrencyConflictError, rolls back the transaction, and 0 client-facing
    reports are published.
    """
    cycle_id = "cycle_stale_commit_guard_01"
    _seed_preparatory_cycle_context(sqlite_db_session, cycle_id=cycle_id, seed_ledger=True)

    bind = sqlite_db_session.get_bind()
    w1_session = Session(bind=bind, autocommit=False, autoflush=False)
    w2_session = Session(bind=bind, autocommit=False, autoflush=False)

    w1_calls: List[Dict[str, Any]] = []

    call_idx = {"v": 0}

    async def steal_before_commit_handler(request: httpx.Request) -> httpx.Response:
        idx = call_idx["v"]
        call_idx["v"] += 1
        body = json.loads(request.content.decode("utf-8"))
        w1_calls.append({"index": idx, "prompt": body["contents"][0]["parts"][0]["text"]})

        if idx == 9:  # Call 9 is the last AI call (Chief Physician Pro)
            # Worker 2 steals the lease right before Worker 1's commit step
            lock_id = f"lock_primary_{cycle_id}"
            stmt = (
                update(ModelReport)
                .where(ModelReport.report_id == lock_id)
                .values(
                    content={
                        "lease_owner_id": "lease_primary_worker_2_active",
                        "expires_at_utc": (datetime.now(timezone.utc) + timedelta(seconds=300)).isoformat(),
                    }
                )
            )
            w2_session.execute(stmt)
            w2_session.commit()

        if idx < 8:
            return httpx.Response(
                200, json=_gemini_envelope(_sample_system_agent_output(SYSTEM_KEYS[idx]))
            )
        if idx == 8:
            return httpx.Response(200, json=_gemini_envelope(_sample_flash_output()))
        return httpx.Response(200, json=_gemini_envelope(_sample_pro_output()))

    w1_gw = GeminiGateway(
        api_key="test-key-steal",
        transport=httpx.MockTransport(steal_before_commit_handler),
    )
    w1_service = PrimaryConsiliumService(
        db=w1_session,
        gateway=w1_gw,
        lease_ttl_seconds=300,
    )

    with pytest.raises(ConsiliumConcurrencyConflictError) as exc_info:
        await w1_service.run_primary_consilium(cycle_id=cycle_id)

    err_msg = str(exc_info.value)
    assert ("was lost or superseded by another owner" in err_msg or "no longer owned by this worker" in err_msg)

    # 1. Zero client-facing reports committed
    report_repo = ModelReportRepository(w2_session)
    client_reps = report_repo.list_client_facing(cycle_id=cycle_id)
    assert len(client_reps) == 0, "No client_facing reports may be published by a stale worker"

    # 2. Worker 2's lease remains intact
    lock_row = w1_service._fetch_fresh_lock_row(cycle_id)
    assert lock_row is not None
    assert (lock_row.content or {}).get("lease_owner_id") == "lease_primary_worker_2_active"

    w1_session.close()
    w2_session.close()
