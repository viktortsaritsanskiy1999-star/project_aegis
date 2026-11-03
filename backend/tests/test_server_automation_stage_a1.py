import asyncio
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from typing import Any, Callable, Dict, List
import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from app.config import settings
from app.database import Base, get_db
from app.main import app
from app.repositories.cycle_repository import MedicalCycleRepository
from app.repositories.pairing_repository import PairingRepository
from app.repositories.patient_repository import PatientRepository
from app.repositories.report_repository import ModelReportRepository
from app.routers.client_cascade import (
    get_gemini_gateway as client_get_gemini_gateway,
    get_server_now_provider as client_get_server_now_provider,
)
from app.routers.client_chat import (
    get_gemini_gateway as chat_get_gemini_gateway,
    get_server_now_provider as chat_get_server_now_provider,
)
from app.security.tokens import create_access_token, verify_access_token
from app.services.athena_chat_service import (
    ATHENA_CHAT_TURN_REPORT_TYPE,
    POST_PRIMARY_INIT_IDEMPOTENCY_PREFIX,
)
from app.services.clinical_automation_service import (
    AutomationJobStatus,
    AutomationJobType,
    ClinicalAutomationService,
    ClinicalAutomationWorker,
)
from app.services.gemini_gateway import GeminiGateway
from app.services.primary_consilium_service import (
    ConsiliumContextMissingError,
    PrimaryConsiliumService,
)
from app.services.weekly_consilium_service import (
    ATHENA_CLINICAL_LOG_REPORT_TYPE,
    ATHENA_SCHEDULE_ANCHOR_REPORT_TYPE,
    WeeklyConsiliumService,
)
from tests.test_primary_consilium import (
    _build_consilium_mock_gateway,
    _gemini_envelope,
    _sample_anamnesis_output,
)


TEST_JWT_SECRET = "test_jwt_stage_a1_secret_1234567890!"
TEST_PAIRING_SECRET = "test_pairing_stage_a1_secret_12345!"
TEST_API_SECRET = "test_api_stage_a1_secret_1234567890"


class ControlledClock:
    def __init__(self, start_dt: datetime):
        self.current = start_dt

    def now(self) -> datetime:
        return self.current

    def advance(self, **kwargs: Any) -> datetime:
        self.current = self.current + timedelta(**kwargs)
        return self.current


def _create_file_db(db_path: Path, busy_timeout_ms: int = 5000):
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False, "timeout": busy_timeout_ms / 1000.0},
        pool_pre_ping=True,
    )

    @event.listens_for(engine, "connect")
    def _set_pragmas(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)};")
        cursor.close()

    Base.metadata.create_all(bind=engine)
    SessionFactory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    return engine, SessionFactory


def _build_athena_gateway(
    recorded_calls: List[Dict[str, Any]],
    *,
    fail_times: int = 0,
    delay_seconds: float = 0.0,
) -> GeminiGateway:
    state = {"calls": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        idx = state["calls"]
        state["calls"] += 1
        body = json.loads(request.content.decode("utf-8"))
        prompt_text = body["contents"][-1]["parts"][0]["text"]
        recorded_calls.append(
            {
                "index": idx,
                "url_path": request.url.path,
                "prompt": prompt_text,
            }
        )
        if delay_seconds > 0:
            await asyncio.sleep(delay_seconds)
        if idx < fail_times:
            return httpx.Response(
                503,
                json={"error": {"message": f"Simulated transient Gemini 503 failure #{idx + 1}"}},
            )
        reply = (
            "Здравствуйте! Я Афина, ваш медицинский куратор. Первичный консилиум завершён. "
            "Как вы себя чувствуете сегодня утром?\n\n"
            "[CLINICAL_REPORT]\n"
            "{\n"
            '  "status": "NORMAL",\n'
            '  "symptoms_mentioned": [],\n'
            '  "subjective_wellbeing_score": 8,\n'
            '  "adherence_to_plan": "HIGH",\n'
            '  "red_flags": false,\n'
            '  "summary_for_master": "Post-Primary initial greeting dispatched."\n'
            "}\n"
            "[/CLINICAL_REPORT]"
        )
        return httpx.Response(200, json=_gemini_envelope(reply))

    return GeminiGateway(
        api_key="test-stage-a1-key",
        transport=httpx.MockTransport(handler),
        max_retries=1,
        sleep_func=lambda _: None,
    )


def _seed_patient_with_preparatory_intake(
    session: Session,
    *,
    patient_id: str,
    cycle_id: str,
    seed_labs: bool = True,
    seed_ecg: bool = True,
) -> None:
    if PatientRepository(session).get(patient_id) is None:
        PatientRepository(session).create(patient_id=patient_id, status="active")
    cycle_repo = MedicalCycleRepository(session)
    cycle = cycle_repo.get(cycle_id)
    if cycle is None:
        cycle = cycle_repo.create(
            cycle_id=cycle_id,
            patient_id=patient_id,
            status="active",
        )
    else:
        cycle.patient_id = patient_id
        session.commit()

    service = PrimaryConsiliumService(db=session)
    service.save_preparatory_anamnesis_result(
        cycle_id=cycle_id,
        anamnesis_output=_sample_anamnesis_output(),
    )
    if seed_labs:
        service.save_preparatory_ocr_labs(
            cycle_id=cycle_id,
            shared_labs_table={
                "hemoglobin": "144 g/L",
                "ferritin": "42 ng/mL",
                "creatinine": "82 umol/L",
                "glucose": "5.1 mmol/L",
            },
            created_at=datetime(2026, 10, 18, 9, 0, tzinfo=timezone.utc),
        )
        from app.services.lab_revision_consilium_service import LabRevisionConsiliumService

        rev_service = LabRevisionConsiliumService(db=session)
        rev_service._ensure_baseline_ledger_seeded(cycle_id)
    if seed_ecg:
        service.save_preparatory_ecg_summary(
            cycle_id=cycle_id,
            ecg_summary="Sinus rhythm, HR 64 bpm, normal electrical axis, no ST-T shifts.",
        )
    session.commit()


async def _run_manual_primary(
    session: Session,
    *,
    cycle_id: str,
    now_provider: Callable[[], datetime],
) -> List[Dict[str, Any]]:
    primary_calls: List[Dict[str, Any]] = []
    primary_gw = _build_consilium_mock_gateway(primary_calls)
    primary_service = PrimaryConsiliumService(
        db=session,
        gateway=primary_gw,
        now_provider=now_provider,
    )
    await primary_service.run_primary_consilium(
        cycle_id=cycle_id,
        report_date=now_provider().strftime("%Y-%m-%d"),
    )
    return primary_calls


# ==============================================================================
# 1. FIRST PRIMARY REMAINS MANUAL — NEVER AUTO-RUN AFTER INTAKE
# ==============================================================================

@pytest.mark.anyio
async def test_first_primary_never_auto_runs_after_intake(tmp_path: Path):
    """
    Verifies Critical Rule 1:
    Even when Anamnesis M1, Labs, and ECG are all present in server storage,
    the automation service and worker NEVER auto-enqueue or auto-run the first
    Primary Consilium. First Primary Consilium remains strictly manual.
    """
    db_file = tmp_path / "stage_a1_manual_primary_only.db"
    engine, SessionFactory = _create_file_db(db_file)
    clock = ControlledClock(datetime(2026, 11, 3, 9, 0, 0, tzinfo=timezone.utc))

    try:
        with SessionFactory() as session:
            _seed_patient_with_preparatory_intake(
                session,
                patient_id="pat_intake_only",
                cycle_id="cycle_intake_only",
                seed_labs=True,
                seed_ecg=True,
            )

            athena_calls: List[Dict[str, Any]] = []
            gw = _build_athena_gateway(athena_calls)
            auto_service = ClinicalAutomationService(
                db=session, gateway=gw, now_provider=clock.now
            )

            # 1. Discovery finds 0 jobs because manual Primary has not been executed
            discovered = auto_service.discover_and_enqueue_jobs()
            assert discovered == []
            assert auto_service.list_jobs(cycle_id="cycle_intake_only") == []

            # 2. Attempting to enqueue post_primary_init before manual Primary is rejected
            with pytest.raises(ConsiliumContextMissingError, match="manual Primary Consilium"):
                auto_service.enqueue_or_get_job(
                    patient_id="pat_intake_only",
                    cycle_id="cycle_intake_only",
                    job_type=AutomationJobType.POST_PRIMARY_INIT,
                )

            # 3. Attempting to enqueue an automatic primary_consilium job type is rejected
            with pytest.raises(ValueError, match="First Primary Consilium is manual"):
                auto_service.enqueue_or_get_job(
                    patient_id="pat_intake_only",
                    cycle_id="cycle_intake_only",
                    job_type="primary_consilium",
                )

        # 4. Running worker ticks produces 0 jobs, 0 Gemini calls, and does not complete Primary
        athena_calls_worker: List[Dict[str, Any]] = []
        worker = ClinicalAutomationWorker(
            session_factory=SessionFactory,
            gateway_factory=lambda: _build_athena_gateway(athena_calls_worker),
            now_provider=clock.now,
        )
        processed = await worker.run_once()
        assert processed == []
        assert len(athena_calls_worker) == 0

        with SessionFactory() as verify_session:
            p_service = PrimaryConsiliumService(db=verify_session)
            w_service = WeeklyConsiliumService(db=verify_session, now_provider=clock.now)
            assert p_service.is_primary_consilium_completed("cycle_intake_only") is False
            assert w_service.find_schedule_anchor_timestamp("cycle_intake_only") is None
    finally:
        engine.dispose()


# ==============================================================================
# 2. MANUAL PRIMARY -> AUTOMATIC POST-PRIMARY INIT (ONE GREETING & ONE T0)
#    AND NO 24-HOUR AUTOMATIC QUESTION TIMERS WITHOUT PATIENT REPLY
# ==============================================================================

@pytest.mark.anyio
async def test_manual_primary_triggers_one_athena_greeting_and_no_24h_timer_questions(
    tmp_path: Path,
):
    """
    Verifies:
    - After manual Primary Consilium is completed, the worker automatically runs
      `post_primary_init` (without Flutter being open), creating exactly 1 initial
      greeting and 1 T0 anchor (`athena_schedule_anchor`).
    - Advancing time by 24h, 48h, 72h, and 168h without a patient reply NEVER
      generates automatic 24-hour Athena questions (Critical Rule 2).
    """
    db_file = tmp_path / "stage_a1_post_primary_init.db"
    engine, SessionFactory = _create_file_db(db_file)
    t0_dt = datetime(2026, 11, 3, 10, 0, 0, tzinfo=timezone.utc)
    clock = ControlledClock(t0_dt)

    try:
        with SessionFactory() as session:
            _seed_patient_with_preparatory_intake(
                session,
                patient_id="pat_manual_pri",
                cycle_id="cycle_manual_pri",
                seed_labs=False,
                seed_ecg=False,
            )
            primary_calls = await _run_manual_primary(
                session, cycle_id="cycle_manual_pri", now_provider=clock.now
            )
            assert len(primary_calls) == 10

        athena_calls: List[Dict[str, Any]] = []
        worker = ClinicalAutomationWorker(
            session_factory=SessionFactory,
            gateway_factory=lambda: _build_athena_gateway(athena_calls),
            now_provider=clock.now,
        )

        # First worker tick discovers completed manual Primary and executes post_primary_init
        processed = await worker.run_once()
        assert len(processed) == 1
        job = processed[0]
        assert job.status == AutomationJobStatus.COMPLETED
        assert job.job_type == AutomationJobType.POST_PRIMARY_INIT
        assert job.idempotency_key == f"{POST_PRIMARY_INIT_IDEMPOTENCY_PREFIX}cycle_manual_pri"
        assert job.attempts == 1
        assert job.result_metadata is not None
        assert job.result_metadata["reused_existing"] is False
        assert job.result_metadata["anchor_timestamp_utc"] == t0_dt.isoformat()
        assert len(athena_calls) == 1

        # Verify DB artifacts: 1 anchor T0, 1 clinical log, 2 chat messages (user + curator)
        with SessionFactory() as session:
            w_service = WeeklyConsiliumService(db=session, now_provider=clock.now)
            repo = ModelReportRepository(session)
            assert w_service.find_schedule_anchor_timestamp("cycle_manual_pri") == t0_dt

            anchors = repo.list_server_only(
                cycle_id="cycle_manual_pri",
                report_type=ATHENA_SCHEDULE_ANCHOR_REPORT_TYPE,
            )
            assert len(anchors) == 1

            logs = repo.list_server_only(
                cycle_id="cycle_manual_pri",
                report_type=ATHENA_CLINICAL_LOG_REPORT_TYPE,
            )
            assert len(logs) == 1

            history = w_service.get_athena_chat_history("cycle_manual_pri")
            assert len(history) == 2
            assert history[0]["sender"] == "user"
            assert history[1]["sender"] == "curator"

        # Critical Rule 2: Advance 24h, 48h, 72h, 168h without patient reply -> 0 new questions
        for hours in (24, 24, 24, 96):
            clock.advance(hours=hours)
            subsequent = await worker.run_once()
            assert subsequent == []
            assert len(athena_calls) == 1

        with SessionFactory() as session:
            w_service = WeeklyConsiliumService(db=session, now_provider=clock.now)
            assert w_service.find_schedule_anchor_timestamp("cycle_manual_pri") == t0_dt
            assert len(w_service.get_athena_chat_history("cycle_manual_pri")) == 2
    finally:
        engine.dispose()


# ==============================================================================
# 3. EXISTING GREETING REUSE (BOTH DIRECTIONS: CLIENT FIRST & WORKER FIRST)
# ==============================================================================

@pytest.mark.anyio
async def test_existing_greeting_is_not_duplicated_client_first_or_worker_first(
    monkeypatch, tmp_path: Path
):
    """
    Verifies:
    - Case A (Client First): If Flutter already called `/api/v1/client/chat/athena`
      (`initial_greeting`) after manual Primary Consilium, the automation worker
      reuses the existing greeting (`reused_existing=True`) with 0 extra Gemini calls.
    - Case B (Worker First): If the automation worker ran `post_primary_init` while
      Flutter was closed, and Flutter later calls `/api/v1/client/chat/athena`
      (`initial_greeting`) with its own client `idempotency_key`, the server returns
      the existing greeting with 0 extra Gemini calls and 0 duplicate messages.
    """
    monkeypatch.setattr(settings, "api_secret", TEST_API_SECRET)
    monkeypatch.setattr(settings, "pairing_secret", TEST_PAIRING_SECRET)
    monkeypatch.setattr(settings, "jwt_secret", TEST_JWT_SECRET)
    monkeypatch.setattr(settings, "jwt_issuer", "aegis-backend")
    monkeypatch.setattr(settings, "jwt_audience", "aegis-mobile-client")
    monkeypatch.setattr(settings, "gemini_api_key", "test-gemini-key")

    db_file = tmp_path / "stage_a1_greeting_reuse.db"
    engine, SessionFactory = _create_file_db(db_file)
    clock = ControlledClock(datetime(2026, 11, 3, 11, 0, 0, tzinfo=timezone.utc))

    def _override_get_db():
        with SessionFactory() as req_session:
            yield req_session

    athena_calls: List[Dict[str, Any]] = []
    mock_athena_gw = _build_athena_gateway(athena_calls)
    primary_calls: List[Dict[str, Any]] = []
    mock_primary_gw = _build_consilium_mock_gateway(primary_calls)

    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[client_get_gemini_gateway] = lambda: mock_primary_gw
    app.dependency_overrides[chat_get_gemini_gateway] = lambda: mock_athena_gw
    app.dependency_overrides[client_get_server_now_provider] = lambda: clock.now
    app.dependency_overrides[chat_get_server_now_provider] = lambda: clock.now

    try:
        with TestClient(app) as client:
            # --- CASE A: Client triggers initial_greeting before worker runs ---
            with SessionFactory() as session:
                _seed_patient_with_preparatory_intake(
                    session,
                    patient_id="pat_client_first",
                    cycle_id="cycle_client_first",
                )
                PairingRepository(session).create_code(
                    patient_id="pat_client_first", raw_code_override="111111"
                )
                session.commit()

            pair_a = client.post(
                "/api/v1/auth/pair",
                json={
                    "pairing_code": "111111",
                    "device_id": "dev_a",
                    "platform": "android",
                    "device_name": "Phone A",
                },
            )
            assert pair_a.status_code == 200
            fam_a = verify_access_token(pair_a.json()["access_token"]).family_id
            tok_a = create_access_token(
                patient_id="pat_client_first",
                device_id="dev_a",
                family_id=fam_a,
                scope="cycle:read cascade:run chat:write chat:read",
            ).access_token
            headers_a = {"Authorization": f"Bearer {tok_a}"}

            # Patient manually clicks Run Primary in Flutter
            pri_resp_a = client.post(
                "/api/v1/client/cascade/run-primary", headers=headers_a
            )
            assert pri_resp_a.status_code == 200
            assert len(primary_calls) == 10

            # Verify durable job was enqueued in pending state by run-primary endpoint
            with SessionFactory() as session:
                auto_s = ClinicalAutomationService(db=session, now_provider=clock.now)
                enqueued_a = auto_s.get_job("cycle_client_first")
                assert enqueued_a is not None
                assert enqueued_a.status == AutomationJobStatus.PENDING

            # Client immediately sends initial_greeting with its own idempotency_key
            greet_resp_a = client.post(
                "/api/v1/client/chat/athena",
                json={
                    "event_type": "initial_greeting",
                    "idempotency_key": "flutter_client_key_a",
                },
                headers=headers_a,
            )
            assert greet_resp_a.status_code == 200
            assert len(athena_calls) == 1
            client_msg_id = greet_resp_a.json()["message_id"]

            # Now automation worker runs: must reuse client's greeting with 0 extra Gemini calls
            worker = ClinicalAutomationWorker(
                session_factory=SessionFactory,
                gateway_factory=lambda: mock_athena_gw,
                now_provider=clock.now,
            )
            clock.advance(minutes=5)
            processed_a = await worker.run_once()
            assert len(processed_a) == 1
            assert processed_a[0].status == AutomationJobStatus.COMPLETED
            assert processed_a[0].result_metadata is not None
            assert processed_a[0].result_metadata["reused_existing"] is True
            assert processed_a[0].result_metadata["message_id"] == client_msg_id
            assert len(athena_calls) == 1  # Zero extra Gemini calls!

            # --- CASE B: Worker runs first while app is closed, then client calls initial_greeting ---
            with SessionFactory() as session:
                _seed_patient_with_preparatory_intake(
                    session,
                    patient_id="pat_worker_first",
                    cycle_id="cycle_worker_first",
                )
                PairingRepository(session).create_code(
                    patient_id="pat_worker_first", raw_code_override="222222"
                )
                session.commit()
                await _run_manual_primary(
                    session, cycle_id="cycle_worker_first", now_provider=clock.now
                )

            pair_b = client.post(
                "/api/v1/auth/pair",
                json={
                    "pairing_code": "222222",
                    "device_id": "dev_b",
                    "platform": "android",
                    "device_name": "Phone B",
                },
            )
            assert pair_b.status_code == 200
            fam_b = verify_access_token(pair_b.json()["access_token"]).family_id
            tok_b = create_access_token(
                patient_id="pat_worker_first",
                device_id="dev_b",
                family_id=fam_b,
                scope="cycle:read cascade:run chat:write chat:read",
            ).access_token
            headers_b = {"Authorization": f"Bearer {tok_b}"}

            # Worker runs first while Flutter is closed
            processed_b = await worker.run_once()
            assert len(processed_b) == 1
            assert processed_b[0].cycle_id == "cycle_worker_first"
            assert processed_b[0].status == AutomationJobStatus.COMPLETED
            assert processed_b[0].result_metadata["reused_existing"] is False
            assert len(athena_calls) == 2
            worker_msg_id = processed_b[0].result_metadata["message_id"]

            # Later, Flutter opens and sends initial_greeting with a different client idempotency_key
            clock.advance(hours=1)
            greet_resp_b = client.post(
                "/api/v1/client/chat/athena",
                json={
                    "event_type": "initial_greeting",
                    "idempotency_key": "flutter_late_client_key_b",
                },
                headers=headers_b,
            )
            assert greet_resp_b.status_code == 200
            assert greet_resp_b.json()["message_id"] == worker_msg_id
            assert len(athena_calls) == 2  # Zero extra Gemini calls!

            with SessionFactory() as session:
                w_service = WeeklyConsiliumService(db=session, now_provider=clock.now)
                repo = ModelReportRepository(session)
                assert len(w_service.get_athena_chat_history("cycle_worker_first")) == 2
                assert (
                    len(
                        repo.list_server_only(
                            cycle_id="cycle_worker_first",
                            report_type=ATHENA_SCHEDULE_ANCHOR_REPORT_TYPE,
                        )
                    )
                    == 1
                )
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


# ==============================================================================
# 4. CONCURRENT WORKERS — ATOMIC CLAIM & SINGLE EXECUTION
# ==============================================================================

@pytest.mark.anyio
async def test_concurrent_workers_atomic_claim_executes_job_once(tmp_path: Path):
    """
    Verifies multi-process / multi-worker safety:
    Two independent `ClinicalAutomationWorker` instances polling the same SQLite
    database concurrently claim and execute `post_primary_init` at most once.
    """
    db_file = tmp_path / "stage_a1_concurrent_workers.db"
    engine, SessionFactory = _create_file_db(db_file)
    clock = ControlledClock(datetime(2026, 11, 3, 12, 0, 0, tzinfo=timezone.utc))

    try:
        with SessionFactory() as session:
            _seed_patient_with_preparatory_intake(
                session,
                patient_id="pat_concurrent",
                cycle_id="cycle_concurrent",
            )
            await _run_manual_primary(
                session, cycle_id="cycle_concurrent", now_provider=clock.now
            )
            # Pre-discover so both workers race directly on claiming the pending job
            ClinicalAutomationService(
                db=session, now_provider=clock.now
            ).discover_and_enqueue_jobs()

        athena_calls: List[Dict[str, Any]] = []
        shared_gw = _build_athena_gateway(athena_calls, delay_seconds=0.03)

        worker_1 = ClinicalAutomationWorker(
            session_factory=SessionFactory,
            gateway_factory=lambda: shared_gw,
            now_provider=clock.now,
        )
        worker_2 = ClinicalAutomationWorker(
            session_factory=SessionFactory,
            gateway_factory=lambda: shared_gw,
            now_provider=clock.now,
        )

        res_1, res_2 = await asyncio.gather(
            worker_1.run_once(discover=False),
            worker_2.run_once(discover=False),
        )
        total_processed = res_1 + res_2
        assert len(total_processed) == 1
        assert total_processed[0].status == AutomationJobStatus.COMPLETED
        assert total_processed[0].attempts == 1
        assert len(athena_calls) == 1

        with SessionFactory() as session:
            repo = ModelReportRepository(session)
            w_service = WeeklyConsiliumService(db=session, now_provider=clock.now)
            assert (
                len(
                    repo.list_server_only(
                        cycle_id="cycle_concurrent",
                        report_type=ATHENA_SCHEDULE_ANCHOR_REPORT_TYPE,
                    )
                )
                == 1
            )
            assert len(w_service.get_athena_chat_history("cycle_concurrent")) == 2
    finally:
        engine.dispose()


# ==============================================================================
# 5. PROCESS CRASH / EXPIRED LEASE RECOVERY & HEARTBEAT RENEWAL
# ==============================================================================

@pytest.mark.anyio
async def test_expired_lease_crash_recovery_and_separate_session_heartbeat(tmp_path: Path):
    """
    Verifies:
    1. If a worker crashes while a job is in `running` state, a second worker
       does NOT steal the job before `lease_expires_at_utc`, but recovers and
       completes the job once the lease expires.
    2. While a job is executing, the background heartbeat renews the lease in
       an isolated SQLAlchemy session (`renewal_count > 0`).
    """
    db_file = tmp_path / "stage_a1_lease_recovery.db"
    engine, SessionFactory = _create_file_db(db_file)
    clock = ControlledClock(datetime(2026, 11, 3, 13, 0, 0, tzinfo=timezone.utc))

    try:
        with SessionFactory() as session:
            _seed_patient_with_preparatory_intake(
                session,
                patient_id="pat_crash_rec",
                cycle_id="cycle_crash_rec",
            )
            await _run_manual_primary(
                session, cycle_id="cycle_crash_rec", now_provider=clock.now
            )
            auto_service = ClinicalAutomationService(
                db=session, now_provider=clock.now, lease_ttl_seconds=60
            )
            job = auto_service.enqueue_post_primary_init(cycle_id="cycle_crash_rec")
            assert job is not None

            # Simulate Worker A claiming the job and then crashing mid-flight
            claimed = auto_service.claim_job(job.job_id)
            assert claimed is not None
            assert claimed.status == AutomationJobStatus.RUNNING
            assert claimed.attempts == 1
            crashed_owner = claimed.lease_owner_id
            assert crashed_owner is not None

        athena_calls: List[Dict[str, Any]] = []
        worker_b = ClinicalAutomationWorker(
            session_factory=SessionFactory,
            gateway_factory=lambda: _build_athena_gateway(
                athena_calls, delay_seconds=0.05
            ),
            now_provider=clock.now,
            lease_ttl_seconds=60,
            lease_heartbeat_interval_seconds=0.015,
        )

        # 1. Before lease expiry (e.g. +30s < 60s TTL), Worker B must NOT steal the running job
        clock.advance(seconds=30)
        early_tick = await worker_b.run_once()
        assert early_tick == []
        assert len(athena_calls) == 0

        # 2. After lease expiry (+31s -> 61s >= 60s TTL), Worker B recovers and completes the job
        clock.advance(seconds=31)
        recovered_tick = await worker_b.run_once()
        assert len(recovered_tick) == 1
        completed_job = recovered_tick[0]
        assert completed_job.status == AutomationJobStatus.COMPLETED
        assert completed_job.attempts == 2
        assert completed_job.renewal_count >= 2  # Heartbeat renewed lease during execution
        assert len(athena_calls) == 1
    finally:
        engine.dispose()


# ==============================================================================
# 6. FAILED GEMINI REQUEST & BOUNDED EXPONENTIAL BACKOFF RETRY
# ==============================================================================

@pytest.mark.anyio
async def test_failed_gemini_request_bounded_retry_and_terminal_failure(tmp_path: Path):
    """
    Verifies bounded retry state machine:
    - Transient Gemini 503 failure transitions job to `retryable_failure` with
      exponential backoff (`next_attempt_at_utc` in the future).
    - Polling before `next_attempt_at_utc` does not re-run the job.
    - Advancing past `next_attempt_at_utc` retries and transitions to `completed`
      when Gemini recovers, or `terminal_failure` when `max_attempts` is exhausted.
    """
    db_file = tmp_path / "stage_a1_retries.db"
    engine, SessionFactory = _create_file_db(db_file)
    clock = ControlledClock(datetime(2026, 11, 3, 14, 0, 0, tzinfo=timezone.utc))

    try:
        # --- Part A: Fails once, then succeeds after backoff ---
        with SessionFactory() as session:
            _seed_patient_with_preparatory_intake(
                session,
                patient_id="pat_retry_ok",
                cycle_id="cycle_retry_ok",
            )
            await _run_manual_primary(
                session, cycle_id="cycle_retry_ok", now_provider=clock.now
            )

        calls_a: List[Dict[str, Any]] = []
        gw_a = _build_athena_gateway(calls_a, fail_times=1)
        worker_a = ClinicalAutomationWorker(
            session_factory=SessionFactory,
            gateway_factory=lambda: gw_a,
            now_provider=clock.now,
            base_backoff_seconds=30,
        )

        # Attempt 1 fails with HTTP 502 (wrapping Gemini 503) -> retryable_failure
        tick_1 = await worker_a.run_once()
        assert len(tick_1) == 1
        assert tick_1[0].status == AutomationJobStatus.RETRYABLE_FAILURE
        assert tick_1[0].attempts == 1
        assert tick_1[0].last_error is not None
        assert tick_1[0].last_error["retryable"] is True
        assert tick_1[0].next_attempt_at_utc == (
            clock.now() + timedelta(seconds=30)
        ).isoformat()
        assert len(calls_a) == 1

        # Polling 10s later (before 30s backoff) skips the job
        clock.advance(seconds=10)
        tick_early = await worker_a.run_once()
        assert tick_early == []
        assert len(calls_a) == 1

        # Advance past 30s backoff -> Attempt 2 succeeds!
        clock.advance(seconds=20)
        tick_2 = await worker_a.run_once()
        assert len(tick_2) == 1
        assert tick_2[0].status == AutomationJobStatus.COMPLETED
        assert tick_2[0].attempts == 2
        assert len(calls_a) == 2

        # --- Part B: Exhausts max_attempts=2 -> transitions to terminal_failure ---
        with SessionFactory() as session:
            _seed_patient_with_preparatory_intake(
                session,
                patient_id="pat_retry_term",
                cycle_id="cycle_retry_term",
            )
            await _run_manual_primary(
                session, cycle_id="cycle_retry_term", now_provider=clock.now
            )
            ClinicalAutomationService(
                db=session, now_provider=clock.now, base_backoff_seconds=30
            ).enqueue_post_primary_init(cycle_id="cycle_retry_term", max_attempts=2)

        calls_b: List[Dict[str, Any]] = []
        gw_b = _build_athena_gateway(calls_b, fail_times=10)
        worker_b = ClinicalAutomationWorker(
            session_factory=SessionFactory,
            gateway_factory=lambda: gw_b,
            now_provider=clock.now,
            base_backoff_seconds=30,
        )

        # Attempt 1 -> retryable_failure
        b_tick_1 = await worker_b.run_once()
        assert len(b_tick_1) == 1
        assert b_tick_1[0].status == AutomationJobStatus.RETRYABLE_FAILURE
        assert b_tick_1[0].attempts == 1

        # Advance 30s -> Attempt 2 (max_attempts=2) -> terminal_failure
        clock.advance(seconds=30)
        b_tick_2 = await worker_b.run_once()
        assert len(b_tick_2) == 1
        assert b_tick_2[0].status == AutomationJobStatus.TERMINAL_FAILURE
        assert b_tick_2[0].attempts == 2
        assert b_tick_2[0].next_attempt_at_utc is None
        assert b_tick_2[0].last_error["retryable"] is False

        # Further ticks never retry a terminal_failure job
        clock.advance(hours=2)
        b_tick_3 = await worker_b.run_once()
        assert b_tick_3 == []
        assert len(calls_b) == 2

        with SessionFactory() as session:
            w_service = WeeklyConsiliumService(db=session, now_provider=clock.now)
            assert w_service.find_schedule_anchor_timestamp("cycle_retry_term") is None
            assert w_service.get_athena_chat_history("cycle_retry_term") == []
    finally:
        engine.dispose()


# ==============================================================================
# 7. PATIENT / CYCLE ISOLATION & COORDINATION LOCK SAFETY
# ==============================================================================

@pytest.mark.anyio
async def test_patient_cycle_isolation_and_consilium_lock_deferral(tmp_path: Path):
    """
    Verifies:
    - If Patient A has completed manual Primary Consilium and Patient B has only
      completed intake, only Patient A receives post-Primary initialization.
    - If an active cycle consilium coordination lock (`lock_consilium_coord_{cycle_id}`)
      is held, `post_primary_init` defers safely (`retryable_failure`) until the
      consilium lock is released.
    """
    db_file = tmp_path / "stage_a1_isolation.db"
    engine, SessionFactory = _create_file_db(db_file)
    clock = ControlledClock(datetime(2026, 11, 3, 15, 0, 0, tzinfo=timezone.utc))

    try:
        with SessionFactory() as session:
            # Patient A: manual Primary completed, but a consilium lease is temporarily active
            _seed_patient_with_preparatory_intake(
                session,
                patient_id="pat_iso_a",
                cycle_id="cycle_iso_a",
            )
            await _run_manual_primary(
                session, cycle_id="cycle_iso_a", now_provider=clock.now
            )
            w_service_a = WeeklyConsiliumService(db=session, now_provider=clock.now)
            lease_owner = w_service_a._acquire_cross_process_lease("cycle_iso_a")

            # Patient B: intake completed, manual Primary NOT run
            _seed_patient_with_preparatory_intake(
                session,
                patient_id="pat_iso_b",
                cycle_id="cycle_iso_b",
            )

        calls: List[Dict[str, Any]] = []
        worker = ClinicalAutomationWorker(
            session_factory=SessionFactory,
            gateway_factory=lambda: _build_athena_gateway(calls),
            now_provider=clock.now,
            base_backoff_seconds=15,
        )

        # Tick 1: Patient B is ignored (manual Primary not run); Patient A defers due to active consilium lock
        tick_1 = await worker.run_once()
        assert len(tick_1) == 1
        assert tick_1[0].cycle_id == "cycle_iso_a"
        assert tick_1[0].status == AutomationJobStatus.RETRYABLE_FAILURE
        assert "Consilium lease active" in tick_1[0].last_error["message"]
        assert len(calls) == 0

        # Release consilium lock on Patient A and advance past 15s backoff
        with SessionFactory() as session:
            WeeklyConsiliumService(
                db=session, now_provider=clock.now
            )._release_cross_process_lease_safe("cycle_iso_a", lease_owner)

        clock.advance(seconds=15)
        tick_2 = await worker.run_once()
        assert len(tick_2) == 1
        assert tick_2[0].cycle_id == "cycle_iso_a"
        assert tick_2[0].status == AutomationJobStatus.COMPLETED
        assert len(calls) == 1

        with SessionFactory() as session:
            w_service = WeeklyConsiliumService(db=session, now_provider=clock.now)
            p_service = PrimaryConsiliumService(db=session)
            assert w_service.find_schedule_anchor_timestamp("cycle_iso_a") == clock.now()
            assert w_service.find_schedule_anchor_timestamp("cycle_iso_b") is None
            assert p_service.is_primary_consilium_completed("cycle_iso_b") is False
    finally:
        engine.dispose()
