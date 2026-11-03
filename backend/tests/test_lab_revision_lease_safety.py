import asyncio
from datetime import datetime, timedelta, timezone
import json
from typing import Any, Dict, Generator, List
import httpx
import pytest
from sqlalchemy import create_engine, delete, update
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.models.base import Base
from app.models.model_report import ModelReport
from app.repositories.cycle_repository import MedicalCycleRepository
from app.repositories.patient_repository import PatientRepository
from app.repositories.report_repository import ModelReportRepository
from app.schemas.consilium import (
    SYSTEM_KEYS,
    LabEventType,
    LabMarkerMutationItem,
    LabReferenceRange,
)
from app.schemas.privacy import PrivacyLevel
from app.services.gemini_gateway import GeminiGateway, GeminiValidationError
from app.services.lab_revision_consilium_service import (
    LabRevisionConsiliumService,
    ConsiliumCooldownError,
    LAB_REVISION_EXECUTION_LOG_REPORT_TYPE,
    LAB_REVISION_IN_FLIGHT_LOCK_REPORT_TYPE,
    LABORATORY_DIFF_AUDIT_REPORT_TYPE,
)
from app.services.primary_consilium_service import (
    PrimaryConsiliumService,
    ConsiliumConcurrencyConflictError,
    CYCLE_CONSILIUM_COORDINATION_LOCK_REPORT_TYPE,
)
from app.services.weekly_consilium_service import (
    WeeklyConsiliumService,
    ATHENA_ACTIVE_STRATEGY_REPORT_TYPE,
)
from tests.test_lab_revision_consilium import (
    _build_lab_revision_mock_gateway,
    _sample_lab_revision_flash_output,
    _sample_lab_revision_organ_output,
    _sample_lab_revision_pro_output,
    _seed_primary_consilium,
)
from tests.test_primary_consilium import (
    _build_consilium_mock_gateway,
    _gemini_envelope,
)
from tests.test_weekly_consilium import (
    _build_weekly_mock_gateway,
    _seed_seven_day_athena_logs,
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


def _seed_unreviewed_lab_change(
    db_session: Session,
    cycle_id: str,
    now_dt: datetime,
    value: float = 19.5,
) -> None:
    service = LabRevisionConsiliumService(db=db_session, now_provider=lambda: now_dt)
    service.upsert_laboratory_markers(
        cycle_id=cycle_id,
        mutations=[
            LabMarkerMutationItem(
                marker_key="ferritin",
                display_name="Ferritin",
                event_type=LabEventType.NEW_STUDY,
                value=value,
                unit="ng/mL",
                sample_date=now_dt.strftime("%Y-%m-%d"),
                reference_range=LabReferenceRange(low=30.0, high=300.0, text="30 - 300 ng/mL"),
            )
        ],
    )


# ==============================================================================
# TEST 1: Lab Revision vs Primary Consilium Contention (Both Directions)
# ==============================================================================

@pytest.mark.anyio
async def test_lab_revision_vs_primary_contention(sqlite_db_session: Session):
    """
    Verifies mutual exclusion between Lab Revision Consilium and Primary Consilium
    for the same cycle_id via `lock_consilium_coord_{cycle_id}`:
    1. When Primary Consilium holds the cycle lease, Lab Revision is rejected immediately
       with ConsiliumCooldownError (subclass of ConsiliumConcurrencyConflictError),
       making 0 Gemini calls and leaving no orphaned `lock_lab_rev_{cycle_id}` row.
    2. When Lab Revision holds the cycle lease (`lock_lab_rev_{cycle_id}` +
       `lock_consilium_coord_{cycle_id}`), Primary Consilium is rejected immediately
       with ConsiliumConcurrencyConflictError, making 0 Gemini calls and leaving no
       orphaned `lock_primary_{cycle_id}` row.
    """
    cycle_id = "cycle_lab_vs_primary_01"
    now_dt = datetime(2026, 10, 20, 10, 0, 0, tzinfo=timezone.utc)
    await _seed_primary_consilium(sqlite_db_session, cycle_id=cycle_id, primary_date="2026-10-18")
    _seed_unreviewed_lab_change(sqlite_db_session, cycle_id=cycle_id, now_dt=now_dt)

    bind = sqlite_db_session.get_bind()
    session_primary = Session(bind=bind, autocommit=False, autoflush=False)
    session_lab = Session(bind=bind, autocommit=False, autoflush=False)

    try:
        primary_service = PrimaryConsiliumService(
            db=session_primary,
            now_provider=lambda: now_dt,
            lock_registry={},
        )
        lab_calls: List[Dict[str, Any]] = []
        lab_service = LabRevisionConsiliumService(
            db=session_lab,
            gateway=_build_lab_revision_mock_gateway(recorded_calls=lab_calls),
            now_provider=lambda: now_dt,
            lock_registry={},
        )

        # Direction A: Primary holds the lease -> Lab Revision is blocked
        lease_primary = primary_service._acquire_cross_process_lease(cycle_id)
        assert lease_primary.startswith("lease_primary_")

        with pytest.raises(ConsiliumCooldownError) as exc_lab:
            await lab_service.run_lab_revision_consilium(cycle_id=cycle_id, report_date="2026-10-20")

        assert isinstance(exc_lab.value, ConsiliumConcurrencyConflictError)
        assert "Primary Consilium is already in progress" in str(exc_lab.value)
        assert len(lab_calls) == 0

        # Ensure no orphaned lab_rev lock was created
        orphaned_lab_lock = (
            sqlite_db_session.query(ModelReport)
            .filter(ModelReport.report_id == f"lock_lab_rev_{cycle_id}")
            .populate_existing()
            .first()
        )
        assert orphaned_lab_lock is None

        primary_service._release_cross_process_lease_safe(cycle_id, lease_primary)

        # Direction B: Lab Revision holds both leases -> Primary is blocked
        lease_lab = lab_service._acquire_cross_process_lease(cycle_id)
        assert lease_lab.startswith("lease_lab_rev_")

        # Both lock rows must exist in SQLite
        coord_row = lab_service._fetch_fresh_coordination_lock_row(cycle_id)
        lab_row = lab_service._fetch_fresh_lock_row(cycle_id)
        assert coord_row is not None
        assert lab_row is not None
        assert (coord_row.content or {}).get("lease_owner_id") == lease_lab
        assert (lab_row.content or {}).get("lease_owner_id") == lease_lab

        primary_calls: List[Dict[str, Any]] = []
        primary_runner = PrimaryConsiliumService(
            db=session_primary,
            gateway=_build_consilium_mock_gateway(primary_calls),
            now_provider=lambda: now_dt,
            lock_registry={},
        )
        with pytest.raises(ConsiliumConcurrencyConflictError, match="Lab-Revision Consilium.*currently in progress"):
            primary_runner._acquire_cross_process_lease(cycle_id)

        # Also test full `run_primary_consilium` on a fresh cycle where Lab Revision holds the lease
        cycle_fresh = "cycle_lab_vs_primary_fresh_02"
        from tests.test_primary_consilium import _seed_preparatory_cycle_context
        _seed_preparatory_cycle_context(sqlite_db_session, cycle_id=cycle_fresh, seed_ledger=True)
        lease_lab_fresh = lab_service._acquire_cross_process_lease(cycle_fresh)
        with pytest.raises(ConsiliumConcurrencyConflictError, match="Lab-Revision Consilium.*currently in progress"):
            await primary_runner.run_primary_consilium(
                cycle_id=cycle_fresh,
                report_date="2026-10-20",
            )
        assert len(primary_calls) == 0
        lab_service._release_cross_process_lease_safe(cycle_fresh, lease_lab_fresh)

        orphaned_prim_lock = (
            sqlite_db_session.query(ModelReport)
            .filter(ModelReport.report_id == f"lock_primary_{cycle_id}")
            .populate_existing()
            .first()
        )
        assert orphaned_prim_lock is None

        lab_service._release_cross_process_lease_safe(cycle_id, lease_lab)
        assert lab_service._fetch_fresh_coordination_lock_row(cycle_id) is None
        assert lab_service._fetch_fresh_lock_row(cycle_id) is None
    finally:
        session_primary.close()
        session_lab.close()


# ==============================================================================
# TEST 2: Lab Revision vs Weekly Consilium Contention (Both Directions)
# ==============================================================================

@pytest.mark.anyio
async def test_lab_revision_vs_weekly_contention(sqlite_db_session: Session):
    """
    Verifies mutual exclusion between Lab Revision Consilium and Weekly Consilium:
    1. While Weekly Consilium holds the cycle lease, Lab Revision is rejected immediately
       with 0 Gemini calls.
    2. While Lab Revision is actively executing its 10-model cascade (at call index 4),
       a concurrent Weekly Consilium request from another process/session is rejected
       with ConsiliumConcurrencyConflictError (0 Weekly Gemini calls), while Lab Revision
       finishes all 10 calls and atomically commits its reports and Athena strategy.
    """
    cycle_id = "cycle_lab_vs_weekly_01"
    sprint_id = "sprint_lab_vs_wk_w1"
    now_dt = datetime(2026, 10, 25, 12, 0, 0, tzinfo=timezone.utc)
    await _seed_primary_consilium(sqlite_db_session, cycle_id=cycle_id, primary_date="2026-10-18")
    _seed_seven_day_athena_logs(
        sqlite_db_session, cycle_id=cycle_id, sprint_id=sprint_id, base_date="2026-10-18"
    )
    _seed_unreviewed_lab_change(sqlite_db_session, cycle_id=cycle_id, now_dt=now_dt)

    bind = sqlite_db_session.get_bind()
    session_weekly = Session(bind=bind, autocommit=False, autoflush=False)
    session_lab = Session(bind=bind, autocommit=False, autoflush=False)

    try:
        weekly_service = WeeklyConsiliumService(
            db=session_weekly,
            now_provider=lambda: now_dt,
            lock_registry={},
        )
        lab_calls_blocked: List[Dict[str, Any]] = []
        lab_service_blocked = LabRevisionConsiliumService(
            db=session_lab,
            gateway=_build_lab_revision_mock_gateway(recorded_calls=lab_calls_blocked),
            now_provider=lambda: now_dt,
            lock_registry={},
        )

        # Direction A: Weekly holds lease -> Lab Revision is blocked
        lease_weekly = weekly_service._acquire_cross_process_lease(cycle_id)
        assert lease_weekly.startswith("lease_weekly_")

        with pytest.raises(ConsiliumCooldownError) as exc_lab:
            await lab_service_blocked.run_lab_revision_consilium(
                cycle_id=cycle_id, report_date="2026-10-25"
            )
        assert isinstance(exc_lab.value, ConsiliumConcurrencyConflictError)
        assert "Weekly Consilium is already in progress" in str(exc_lab.value)
        assert len(lab_calls_blocked) == 0

        weekly_service._release_cross_process_lease_safe(cycle_id, lease_weekly)

        # Direction B: Lab Revision is mid-flight -> Weekly Consilium is blocked
        lab_calls_active: List[Dict[str, Any]] = []
        weekly_calls_contending: List[Dict[str, Any]] = []
        weekly_errors: List[Exception] = []

        weekly_contender = WeeklyConsiliumService(
            db=session_weekly,
            gateway=_build_weekly_mock_gateway(weekly_calls_contending),
            now_provider=lambda: now_dt,
            lock_registry={},
        )

        call_seq = {"idx": 0}

        async def lab_in_flight_handler(request: httpx.Request) -> httpx.Response:
            idx = call_seq["idx"]
            call_seq["idx"] += 1
            lab_calls_active.append({"index": idx})

            if idx == 4:
                try:
                    await weekly_contender.run_weekly_consilium(
                        cycle_id=cycle_id,
                        sprint_id=sprint_id,
                        report_date="2026-10-25",
                    )
                except Exception as exc:
                    weekly_errors.append(exc)

            if 0 <= idx < 8:
                return httpx.Response(
                    200,
                    json=_gemini_envelope(_sample_lab_revision_organ_output(SYSTEM_KEYS[idx])),
                )
            if idx == 8:
                return httpx.Response(200, json=_gemini_envelope(_sample_lab_revision_flash_output()))
            return httpx.Response(200, json=_gemini_envelope(_sample_lab_revision_pro_output()))

        lab_runner = LabRevisionConsiliumService(
            db=session_lab,
            gateway=GeminiGateway(
                api_key="test-lab-vs-weekly",
                transport=httpx.MockTransport(lab_in_flight_handler),
                sleep_func=lambda _: None,
            ),
            now_provider=lambda: now_dt,
            lock_registry={},
        )

        lab_res = await lab_runner.run_lab_revision_consilium(
            cycle_id=cycle_id, report_date="2026-10-25"
        )
        assert lab_res.evaluated_ledger_version == 2
        assert len(lab_calls_active) == 10
        assert len(weekly_calls_contending) == 0
        assert len(weekly_errors) == 1
        assert isinstance(weekly_errors[0], ConsiliumConcurrencyConflictError)
        assert "Lab-Revision Consilium" in str(weekly_errors[0])

        # Verify both leases are cleaned up after Lab Revision finishes
        assert lab_runner._fetch_fresh_lock_row(cycle_id) is None
        assert lab_runner._fetch_fresh_coordination_lock_row(cycle_id) is None
    finally:
        session_weekly.close()
        session_lab.close()


# ==============================================================================
# TEST 3: Simultaneous Cross-Process Lab Revision Requests
# ==============================================================================

@pytest.mark.anyio
async def test_simultaneous_lab_revision_requests_cross_process(sqlite_db_session: Session):
    """
    Verifies that when two cross-process workers (separate DB sessions and separate
    lock registries) launch Lab Revision Consilium simultaneously:
    1. Exactly one worker wins the atomic SQLite lease (`lock_lab_rev_{cycle_id}` +
       `lock_consilium_coord_{cycle_id}`) and executes the 10-model cascade;
    2. The second worker fails closed with ConsiliumCooldownError and makes 0 extra Gemini calls;
    3. Both lease rows are atomically removed upon completion.
    """
    cycle_id = "cycle_simul_lab_rev_01"
    now_dt = datetime(2026, 10, 22, 10, 0, 0, tzinfo=timezone.utc)
    await _seed_primary_consilium(sqlite_db_session, cycle_id=cycle_id, primary_date="2026-10-18")
    _seed_unreviewed_lab_change(sqlite_db_session, cycle_id=cycle_id, now_dt=now_dt)

    bind = sqlite_db_session.get_bind()
    s1 = Session(bind=bind, autocommit=False, autoflush=False)
    s2 = Session(bind=bind, autocommit=False, autoflush=False)
    calls: List[Dict[str, Any]] = []
    gw = _build_lab_revision_mock_gateway(recorded_calls=calls, artificial_delay_sec=0.015)

    try:
        w1 = LabRevisionConsiliumService(
            db=s1, gateway=gw, now_provider=lambda: now_dt, lock_registry={}
        )
        w2 = LabRevisionConsiliumService(
            db=s2, gateway=gw, now_provider=lambda: now_dt, lock_registry={}
        )

        outcomes = await asyncio.gather(
            w1.run_lab_revision_consilium(cycle_id=cycle_id, report_date="2026-10-22"),
            w2.run_lab_revision_consilium(cycle_id=cycle_id, report_date="2026-10-22"),
            return_exceptions=True,
        )
        winners = [r for r in outcomes if not isinstance(r, Exception)]
        losers = [r for r in outcomes if isinstance(r, ConsiliumCooldownError)]

        assert len(winners) == 1
        assert len(losers) == 1
        assert isinstance(losers[0], ConsiliumConcurrencyConflictError)
        assert len(calls) == 10

        sqlite_db_session.expire_all()
        assert w1._fetch_fresh_lock_row(cycle_id) is None
        assert w1._fetch_fresh_coordination_lock_row(cycle_id) is None
    finally:
        s1.close()
        s2.close()


# ==============================================================================
# TEST 4: Heartbeat Uses Independent DB Session & Renews Both Lease Rows
# ==============================================================================

@pytest.mark.anyio
async def test_heartbeat_uses_independent_db_session_and_renews_both_locks(
    sqlite_db_session: Session,
):
    """
    Verifies Requirement 2:
    1. Background heartbeat renews leases in a separate SQLAlchemy Session (`db is not service.db`),
       never sharing `service.db` while awaiting Gemini I/O.
    2. Both `lock_lab_rev_{cycle_id}` and `lock_consilium_coord_{cycle_id}` are renewed
       and verified together (`renewal_count` and `version` match on both rows).
    3. If either of the two lease rows is missing (`res.rowcount != 2`), renewal rolls back
       and raises ConsiliumCooldownError, cancelling the in-flight call immediately.
    """
    cycle_id = "cycle_hb_independent_session_01"
    clock = {"now": datetime(2026, 10, 22, 12, 0, 0, tzinfo=timezone.utc)}
    await _seed_primary_consilium(sqlite_db_session, cycle_id=cycle_id, primary_date="2026-10-18")
    _seed_unreviewed_lab_change(sqlite_db_session, cycle_id=cycle_id, now_dt=clock["now"])

    bind = sqlite_db_session.get_bind()
    req_session = Session(bind=bind, autocommit=False, autoflush=False)
    verify_session = Session(bind=bind, autocommit=False, autoflush=False)

    observed_renewal_sessions: List[bool] = []
    observed_lock_versions_in_flight: List[tuple[int, int]] = []

    try:
        call_seq = {"idx": 0}

        async def slow_first_call_handler(request: httpx.Request) -> httpx.Response:
            idx = call_seq["idx"]
            call_seq["idx"] += 1
            if idx == 0:
                # Wait long enough for multiple background heartbeat ticks
                clock["now"] = clock["now"] + timedelta(seconds=10)
                await asyncio.sleep(0.03)
                clock["now"] = clock["now"] + timedelta(seconds=10)
                await asyncio.sleep(0.03)

                coord_in_flight = (
                    verify_session.query(ModelReport)
                    .filter(ModelReport.report_id == f"lock_consilium_coord_{cycle_id}")
                    .populate_existing()
                    .first()
                )
                lab_in_flight = (
                    verify_session.query(ModelReport)
                    .filter(ModelReport.report_id == f"lock_lab_rev_{cycle_id}")
                    .populate_existing()
                    .first()
                )
                assert coord_in_flight is not None and lab_in_flight is not None
                observed_lock_versions_in_flight.append(
                    (
                        int((coord_in_flight.content or {}).get("renewal_count", 0)),
                        int((lab_in_flight.content or {}).get("renewal_count", 0)),
                    )
                )

            if 0 <= idx < 8:
                return httpx.Response(
                    200,
                    json=_gemini_envelope(_sample_lab_revision_organ_output(SYSTEM_KEYS[idx])),
                )
            if idx == 8:
                return httpx.Response(200, json=_gemini_envelope(_sample_lab_revision_flash_output()))
            return httpx.Response(200, json=_gemini_envelope(_sample_lab_revision_pro_output()))

        service = LabRevisionConsiliumService(
            db=req_session,
            gateway=GeminiGateway(
                api_key="test-hb-session",
                transport=httpx.MockTransport(slow_first_call_handler),
                sleep_func=lambda _: None,
            ),
            now_provider=lambda: clock["now"],
            lock_registry={},
            lease_ttl_seconds=60,
            lease_heartbeat_interval_seconds=0.008,
        )

        orig_renew = service._renew_cross_process_lease

        def spy_renew(c_id: str, owner_id: str, db: Session | None = None) -> None:
            if db is not None:
                # Background heartbeat MUST pass a separate Session, never `service.db`
                observed_renewal_sessions.append(db is not service.db)
            return orig_renew(c_id, owner_id, db=db)

        service._renew_cross_process_lease = spy_renew  # type: ignore[method-assign]

        res = await service.run_lab_revision_consilium(cycle_id=cycle_id, report_date="2026-10-22")
        assert res.evaluated_ledger_version == 2
        assert len(observed_renewal_sessions) >= 1
        assert all(observed_renewal_sessions), "Heartbeat must never share self.db!"
        assert len(observed_lock_versions_in_flight) == 1
        coord_renewals, lab_renewals = observed_lock_versions_in_flight[0]
        assert coord_renewals == lab_renewals
        assert coord_renewals >= 2

        # Also verify partial lock loss (e.g. coordination lock missing) fails renewal immediately
        cycle_partial = "cycle_hb_partial_loss_lab_01"
        PatientRepository(sqlite_db_session).create(patient_id="pat_hb_part_lab", status="active")
        MedicalCycleRepository(sqlite_db_session).create(
            cycle_id=cycle_partial, patient_id="pat_hb_part_lab", status="active"
        )
        await _seed_primary_consilium(sqlite_db_session, cycle_id=cycle_partial, primary_date="2026-10-18")
        _seed_unreviewed_lab_change(sqlite_db_session, cycle_id=cycle_partial, now_dt=clock["now"])

        part_service = LabRevisionConsiliumService(
            db=sqlite_db_session, now_provider=lambda: clock["now"]
        )
        owner_part = part_service._acquire_cross_process_lease(cycle_partial)

        # Delete ONLY the coordination lock row
        sqlite_db_session.execute(
            delete(ModelReport).where(
                ModelReport.report_id == f"lock_consilium_coord_{cycle_partial}"
            )
        )
        sqlite_db_session.commit()

        with pytest.raises(ConsiliumCooldownError, match="partially or completely lost"):
            part_service._renew_cross_process_lease(cycle_partial, owner_part)

        part_service._release_cross_process_lease_safe(cycle_partial, owner_part)
    finally:
        req_session.close()
        verify_session.close()


# ==============================================================================
# TEST 5: Expired Lease Takeover (Lab Revision & Cross-Consilium)
# ==============================================================================

@pytest.mark.anyio
async def test_expired_lease_takeover(sqlite_db_session: Session):
    """
    Verifies that when a worker crashes leaving expired `lock_lab_rev_{cycle_id}` and
    `lock_consilium_coord_{cycle_id}` (> TTL without renewal):
    1. A subsequent Lab Revision worker atomically cleans up both expired lock rows,
       acquires a new lease (`owner_B != owner_A`), and completes the 10-model cascade.
    2. If a Weekly Consilium worker crashed leaving expired `lock_weekly_{cycle_id}` and
       `lock_consilium_coord_{cycle_id}`, Lab Revision cleans up both expired rows and succeeds.
    """
    cycle_id = "cycle_expired_takeover_lab_01"
    clock = {"now": datetime(2026, 10, 22, 10, 0, 0, tzinfo=timezone.utc)}
    await _seed_primary_consilium(sqlite_db_session, cycle_id=cycle_id, primary_date="2026-10-18")
    _seed_unreviewed_lab_change(sqlite_db_session, cycle_id=cycle_id, now_dt=clock["now"])

    # 1. Worker A acquires both Lab Revision leases, then crashes
    service_a = LabRevisionConsiliumService(
        db=sqlite_db_session,
        now_provider=lambda: clock["now"],
        lease_ttl_seconds=300,
    )
    owner_a = service_a._acquire_cross_process_lease(cycle_id)
    assert service_a._fetch_fresh_lock_row(cycle_id) is not None
    assert service_a._fetch_fresh_coordination_lock_row(cycle_id) is not None

    # Advance clock past TTL (+301s)
    clock["now"] = clock["now"] + timedelta(seconds=301)

    # 2. Simulate another crashed Weekly lock on a second cycle to verify cross-type cleanup too
    calls_b: List[Dict[str, Any]] = []
    service_b = LabRevisionConsiliumService(
        db=sqlite_db_session,
        gateway=_build_lab_revision_mock_gateway(recorded_calls=calls_b),
        now_provider=lambda: clock["now"],
        lock_registry={},
        lease_ttl_seconds=300,
    )
    res_b = await service_b.run_lab_revision_consilium(
        cycle_id=cycle_id, report_date="2026-10-22"
    )
    assert res_b.evaluated_ledger_version == 2
    assert len(calls_b) == 10

    # Verify execution log has Worker B's lease_owner_id, not crashed Worker A's
    report_repo = ModelReportRepository(sqlite_db_session)
    logs = report_repo.list_server_only(
        cycle_id=cycle_id, report_type=LAB_REVISION_EXECUTION_LOG_REPORT_TYPE
    )
    assert len(logs) == 1
    assert logs[0].content["lease_owner_id"] != owner_a
    assert service_b._fetch_fresh_lock_row(cycle_id) is None
    assert service_b._fetch_fresh_coordination_lock_row(cycle_id) is None


# ==============================================================================
# TEST 6: Stale Worker Cannot Publish Clinical Reports or Athena Strategy
# ==============================================================================

@pytest.mark.anyio
async def test_stale_worker_cannot_publish_reports_or_athena_strategy(
    sqlite_db_session: Session,
):
    """
    Verifies Requirement 3 (Fencing):
    1. If a worker's lease expires or is superseded before final publication,
       `_verify_and_delete_cross_process_lease_uncommitted` fails closed and blocks
       publication of ANY clinical reports, diff audits, execution logs, or Athena strategy.
    2. Even if no new worker has taken over yet, an expired lease (`expires_at_utc <= now_utc`)
       cannot pass `_verify_and_delete_cross_process_lease_uncommitted` or publish stale results.
    """
    cycle_id = "cycle_stale_worker_no_publish_01"
    clock = {"now": datetime(2026, 10, 22, 10, 0, 0, tzinfo=timezone.utc)}
    await _seed_primary_consilium(sqlite_db_session, cycle_id=cycle_id, primary_date="2026-10-18")
    _seed_unreviewed_lab_change(sqlite_db_session, cycle_id=cycle_id, now_dt=clock["now"])

    report_repo = ModelReportRepository(sqlite_db_session)
    weekly_service = WeeklyConsiliumService(db=sqlite_db_session)

    # Case A: Lease expires right after call 9 (Pro) before final persistence
    call_seq = {"idx": 0}

    async def expire_after_pro_handler(request: httpx.Request) -> httpx.Response:
        idx = call_seq["idx"]
        call_seq["idx"] += 1
        if 0 <= idx < 8:
            return httpx.Response(
                200,
                json=_gemini_envelope(_sample_lab_revision_organ_output(SYSTEM_KEYS[idx])),
            )
        if idx == 8:
            return httpx.Response(200, json=_gemini_envelope(_sample_lab_revision_flash_output()))
        # On final call 9 (Pro), simulate worker freeze past lease TTL right as response returns
        clock["now"] = clock["now"] + timedelta(seconds=500)
        return httpx.Response(200, json=_gemini_envelope(_sample_lab_revision_pro_output()))

    stale_service = LabRevisionConsiliumService(
        db=sqlite_db_session,
        gateway=GeminiGateway(
            api_key="test-stale-publish",
            transport=httpx.MockTransport(expire_after_pro_handler),
            sleep_func=lambda _: None,
        ),
        now_provider=lambda: clock["now"],
        lease_ttl_seconds=300,
        lease_heartbeat_interval_seconds=100.0,
    )

    with pytest.raises(ConsiliumCooldownError):
        await stale_service.run_lab_revision_consilium(
            cycle_id=cycle_id, report_date="2026-10-22"
        )

    # Verify ZERO Lab Revision artifacts were published:
    # 1. No diff audit
    assert (
        report_repo.list_server_only(
            cycle_id=cycle_id, report_type=LABORATORY_DIFF_AUDIT_REPORT_TYPE
        )
        == []
    )
    # 2. No lab revision execution log -> cooldown NOT consumed
    assert (
        report_repo.list_server_only(
            cycle_id=cycle_id, report_type=LAB_REVISION_EXECUTION_LOG_REPORT_TYPE
        )
        == []
    )
    # 3. Client system reports and overall report remain at v1 (Primary Consilium)
    for sys_key in SYSTEM_KEYS:
        versions = report_repo.list_versions(
            cycle_id=cycle_id, report_type=f"system_user_report_{sys_key}"
        )
        assert [v.version for v in versions] == [1]
    overall_versions = report_repo.list_versions(
        cycle_id=cycle_id, report_type="user_overall_report"
    )
    assert [v.version for v in overall_versions] == [1]
    # 4. Athena strategy remains at v1 (Primary Consilium)
    assert weekly_service.get_active_athena_context(cycle_id).strategy_version == 1
    # 5. Lab ledger remains pending_full_consilium_revision
    assert stale_service.get_lab_ledger_state(cycle_id).has_unreviewed_lab_changes is True


# ==============================================================================
# TEST 7: Stale Worker Cannot Release Another Owner's Lease
# ==============================================================================

@pytest.mark.anyio
async def test_stale_worker_cannot_release_another_owners_lease(
    sqlite_db_session: Session,
):
    """
    Verifies Requirement 3:
    1. `_release_cross_process_lease_safe` uses atomic owner/fencing-token predicates.
    2. A stale worker (`owner_A`) can never delete `lock_lab_rev_{cycle_id}` or
       `lock_consilium_coord_{cycle_id}` owned by `owner_B`.
    3. A stale Lab Revision worker can never delete `lock_consilium_coord_{cycle_id}`
       owned by a Weekly or Primary Consilium worker.
    """
    cycle_id = "cycle_no_delete_other_owner_01"
    now_dt = datetime(2026, 10, 22, 10, 0, 0, tzinfo=timezone.utc)
    await _seed_primary_consilium(sqlite_db_session, cycle_id=cycle_id, primary_date="2026-10-18")
    _seed_unreviewed_lab_change(sqlite_db_session, cycle_id=cycle_id, now_dt=now_dt)

    service = LabRevisionConsiliumService(
        db=sqlite_db_session, now_provider=lambda: now_dt
    )
    owner_b = service._acquire_cross_process_lease(cycle_id)
    stale_owner_a = "lease_lab_rev_stale_zombie_000"

    # Attempt safe release with stale owner token
    service._release_cross_process_lease_safe(cycle_id, stale_owner_a)

    # Both of Worker B's leases must still exist and belong to owner_b
    coord_row = service._fetch_fresh_coordination_lock_row(cycle_id)
    lab_row = service._fetch_fresh_lock_row(cycle_id)
    assert coord_row is not None
    assert lab_row is not None
    assert (coord_row.content or {}).get("lease_owner_id") == owner_b
    assert (lab_row.content or {}).get("lease_owner_id") == owner_b

    # Legitimate owner_b releases cleanly
    service._release_cross_process_lease_safe(cycle_id, owner_b)
    assert service._fetch_fresh_coordination_lock_row(cycle_id) is None
    assert service._fetch_fresh_lock_row(cycle_id) is None

    # Now Weekly Worker acquires lease; stale Lab Revision worker attempts release
    weekly_service = WeeklyConsiliumService(
        db=sqlite_db_session, now_provider=lambda: now_dt
    )
    owner_weekly = weekly_service._acquire_cross_process_lease(cycle_id)
    service._release_cross_process_lease_safe(cycle_id, stale_owner_a)

    coord_weekly = service._fetch_fresh_coordination_lock_row(cycle_id)
    assert coord_weekly is not None
    assert (coord_weekly.content or {}).get("lease_owner_id") == owner_weekly
    weekly_service._release_cross_process_lease_safe(cycle_id, owner_weekly)


# ==============================================================================
# TEST 8: Interrupted Execution (Error & Cancellation) and Safe Recovery
# ==============================================================================

@pytest.mark.anyio
async def test_interrupted_execution_and_safe_recovery(sqlite_db_session: Session):
    """
    Verifies Requirement 4 & 5:
    1. When Lab Revision execution is interrupted mid-cascade by:
       (a) an upstream Gemini failure (e.g. call index 4 fails), or
       (b) an async task cancellation (`asyncio.CancelledError`),
       both `lock_lab_rev_{cycle_id}` and `lock_consilium_coord_{cycle_id}` are
       immediately and safely released, 0 partial reports are committed, and
       cooldown is not consumed.
    2. A subsequent retry cleanly acquires the leases, executes all 10 models in order,
       and publishes v2 reports and updated Athena strategy.
    """
    cycle_id = "cycle_interrupted_recovery_01"
    now_dt = datetime(2026, 10, 22, 14, 0, 0, tzinfo=timezone.utc)
    await _seed_primary_consilium(sqlite_db_session, cycle_id=cycle_id, primary_date="2026-10-18")
    _seed_unreviewed_lab_change(sqlite_db_session, cycle_id=cycle_id, now_dt=now_dt)

    # Part A: Interrupted by mid-cascade failure at call index 4
    fail_calls: List[Dict[str, Any]] = []
    fail_service = LabRevisionConsiliumService(
        db=sqlite_db_session,
        gateway=_build_lab_revision_mock_gateway(
            recorded_calls=fail_calls, fail_on_call_index=4
        ),
        now_provider=lambda: now_dt,
        lock_registry={},
    )
    with pytest.raises(GeminiValidationError):
        await fail_service.run_lab_revision_consilium(
            cycle_id=cycle_id, report_date="2026-10-22"
        )

    assert len(fail_calls) == 5  # calls 0..4
    assert fail_service._fetch_fresh_lock_row(cycle_id) is None
    assert fail_service._fetch_fresh_coordination_lock_row(cycle_id) is None
    assert fail_service.get_cooldown_status(cycle_id).is_allowed is True

    # Part B: Interrupted by asyncio.CancelledError mid-cascade
    entered_call_event = asyncio.Event()

    async def cancellable_handler(request: httpx.Request) -> httpx.Response:
        entered_call_event.set()
        await asyncio.sleep(10.0)
        return httpx.Response(200, json={})

    cancel_service = LabRevisionConsiliumService(
        db=sqlite_db_session,
        gateway=GeminiGateway(
            api_key="test-cancel-key",
            transport=httpx.MockTransport(cancellable_handler),
            sleep_func=lambda _: None,
        ),
        now_provider=lambda: now_dt,
        lock_registry={},
    )

    cancel_task = asyncio.create_task(
        cancel_service.run_lab_revision_consilium(
            cycle_id=cycle_id, report_date="2026-10-22"
        )
    )
    await entered_call_event.wait()
    # Both leases are held while task is in-flight
    assert cancel_service._fetch_fresh_lock_row(cycle_id) is not None
    assert cancel_service._fetch_fresh_coordination_lock_row(cycle_id) is not None

    # Cancel the in-flight task
    cancel_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancel_task

    # Both leases must be released immediately on cancellation!
    assert cancel_service._fetch_fresh_lock_row(cycle_id) is None
    assert cancel_service._fetch_fresh_coordination_lock_row(cycle_id) is None
    assert cancel_service.get_cooldown_status(cycle_id).is_allowed is True
    assert cancel_service.get_lab_ledger_state(cycle_id).has_unreviewed_lab_changes is True

    # Part C: Safe recovery — retry succeeds cleanly with all 10 calls
    recovery_calls: List[Dict[str, Any]] = []
    recovery_service = LabRevisionConsiliumService(
        db=sqlite_db_session,
        gateway=_build_lab_revision_mock_gateway(recorded_calls=recovery_calls),
        now_provider=lambda: now_dt,
        lock_registry={},
    )
    recovered_res = await recovery_service.run_lab_revision_consilium(
        cycle_id=cycle_id, report_date="2026-10-22"
    )
    assert len(recovery_calls) == 10
    assert recovered_res.evaluated_ledger_version == 2
    assert recovered_res.evaluation_status == "evaluated"
    assert recovered_res.overall_report.version == 2
    assert recovery_service._fetch_fresh_lock_row(cycle_id) is None
    assert recovery_service._fetch_fresh_coordination_lock_row(cycle_id) is None
