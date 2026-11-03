import asyncio
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import threading
from typing import Any, Dict, List, Optional
import httpx
import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from app.database import Base
from app.models.medical_cycle import MedicalCycle
from app.models.model_report import ModelReport
from app.models.patient import Patient
from app.repositories.report_repository import (
    ModelReportRepository,
    ReportVersionConflictError,
)
from app.schemas.chat import AthenaChatRequest
from app.schemas.consilium import AegisAgentRole
from app.schemas.privacy import PrivacyLevel
from app.services.athena_chat_service import (
    AthenaChatService,
    ATHENA_CHAT_TURN_REPORT_TYPE,
)
from app.services.gemini_gateway import GeminiGateway
from app.services.weekly_consilium_service import (
    WeeklyConsiliumService,
    ATHENA_CHAT_HISTORY_REPORT_TYPE,
    ATHENA_CLINICAL_LOG_REPORT_TYPE,
    ATHENA_SCHEDULE_ANCHOR_REPORT_TYPE,
)


def _gemini_envelope(text: str) -> Dict[str, Any]:
    return {
        "candidates": [
            {
                "content": {
                    "parts": [{"text": text}],
                    "role": "model",
                },
                "finishReason": "STOP",
            }
        ]
    }


def _athena_reply_with_clinical_report(reply_text: str, summary: str = "Routine turn observation.") -> str:
    return (
        f"{reply_text}\n\n"
        "[CLINICAL_REPORT]\n"
        "{\n"
        '  "status": "NORMAL",\n'
        '  "symptoms_mentioned": [],\n'
        '  "subjective_wellbeing_score": 8,\n'
        '  "adherence_to_plan": "HIGH",\n'
        '  "red_flags": false,\n'
        f'  "summary_for_master": "{summary}"\n'
        "}\n"
        "[/CLINICAL_REPORT]"
    )


def _create_file_session_factory(db_path: Path, busy_timeout_ms: int = 5000):
    sqlite_url = f"sqlite:///{db_path}"
    engine = create_engine(
        sqlite_url,
        connect_args={"check_same_thread": False, "timeout": busy_timeout_ms / 1000.0},
        pool_pre_ping=True,
    )

    @event.listens_for(engine, "connect")
    def _set_pragmas(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)};")
        cursor.close()

    Base.metadata.create_all(bind=engine)
    session_factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    return engine, session_factory


def _seed_patient_and_cycle(
    session: Session,
    *,
    patient_id: str,
    cycle_id: str,
    with_completed_primary: bool = True,
    now_dt: Optional[datetime] = None,
) -> None:
    effective_now = now_dt or datetime(2026, 11, 3, 10, 0, 0, tzinfo=timezone.utc)
    if session.get(Patient, patient_id) is None:
        session.add(
            Patient(
                patient_id=patient_id,
                status="active",
                created_at=effective_now,
            )
        )
    cycle = MedicalCycle(
        cycle_id=cycle_id,
        patient_id=patient_id,
        status="active",
        started_at=effective_now,
        created_at=effective_now,
        updated_at=effective_now,
    )
    session.add(cycle)
    session.flush()

    if with_completed_primary:
        repo = ModelReportRepository(session)
        repo.create_next_version(
            cycle_id=cycle_id,
            model="gemini-3.1-pro-preview",
            report_type="chief_physician_internal",
            privacy_level=PrivacyLevel.SERVER_ONLY,
            content={
                "agent_role": AegisAgentRole.CHIEF_PHYSICIAN.value,
                "pro_general_shelf": "Focus on sleep hygiene, hydration, and daily pulse monitoring.",
                "pro_training_shelf": "Mild autonomic lability under stress.",
            },
            commit=False,
        )
        repo.create_next_version(
            cycle_id=cycle_id,
            model="gemini-3-flash-preview",
            report_type="deputy_chief_physician_internal",
            privacy_level=PrivacyLevel.SERVER_ONLY,
            content={
                "agent_role": AegisAgentRole.DEPUTY_CHIEF_PHYSICIAN.value,
                "flash_general_shelf": "Stable baseline vitals.",
            },
            commit=False,
        )
        repo.create_next_version(
            cycle_id=cycle_id,
            model="gemini-3.1-pro-preview",
            report_type="user_overall_report",
            privacy_level=PrivacyLevel.CLIENT_FACING,
            content={
                "title": "Primary Overall Report",
                "summary": "Initial clinical synthesis completed.",
            },
            commit=False,
        )

    session.commit()


# ==============================================================================
# 1. CROSS-PROCESS CONCURRENCY: NO LOST UPDATES IN CHAT HISTORY
# ==============================================================================


def test_cross_process_save_athena_chat_message_prevents_lost_updates_on_version_conflict(
    tmp_path: Path,
):
    """
    Verifies that when Worker B commits a chat history version right after Worker A reads
    existing_messages (simulating a cross-process race), Worker A detects the version conflict,
    rolls back, re-reads Worker B's committed messages, and appends its message on top rather
    than overwriting Worker B's message with a stale precomputed messages array.
    """
    db_path = tmp_path / "chat_lost_update_race.db"
    engine, SessionFactory = _create_file_session_factory(db_path)
    cycle_id = "cycle_bug08_race_01"

    with SessionFactory() as seed_db:
        _seed_patient_and_cycle(seed_db, patient_id="pat_race_01", cycle_id=cycle_id)

    try:
        with SessionFactory() as session_a, SessionFactory() as session_b:
            service_a = WeeklyConsiliumService(db=session_a)
            service_b = WeeklyConsiliumService(db=session_b)

            orig_get_with_ver = service_a._get_athena_chat_history_with_version
            interleaved_once = {"done": False}

            def _interleaving_get_with_ver(cid: str):
                msgs, ver = orig_get_with_ver(cid)
                if not interleaved_once["done"]:
                    interleaved_once["done"] = True
                    # Worker B commits its message AFTER Worker A already read (msgs=[], ver=0)
                    service_b.save_athena_chat_message(
                        cycle_id=cid,
                        sender="user",
                        text="Message from Worker B (committed first)",
                        timestamp="2026-11-03T10:01:00+00:00",
                    )
                return msgs, ver

            service_a._get_athena_chat_history_with_version = _interleaving_get_with_ver

            rep_a = service_a.save_athena_chat_message(
                cycle_id=cycle_id,
                sender="curator",
                text="Message from Worker A (retried on conflict)",
                timestamp="2026-11-03T10:01:05+00:00",
            )
            assert rep_a.version == 2

        with SessionFactory() as verify_db:
            history = WeeklyConsiliumService(db=verify_db).get_athena_chat_history(cycle_id)
            assert len(history) == 2
            texts = [m["text"] for m in history]
            assert texts == [
                "Message from Worker B (committed first)",
                "Message from Worker A (retried on conflict)",
            ]
    finally:
        engine.dispose()


def test_two_concurrent_workers_append_distinct_turns_without_message_loss(tmp_path: Path):
    """
    Verifies that two concurrent workers (separate threads, separate SQLAlchemy sessions,
    and isolated in-process lock registries simulating distinct processes) executing
    AthenaChatService.handle_turn for the same cycle preserve all user and curator messages
    without lost updates.
    """
    db_path = tmp_path / "concurrent_workers_turns.db"
    engine, SessionFactory = _create_file_session_factory(db_path)
    cycle_id = "cycle_bug08_concurrent_turns"
    patient_id = "pat_bug08_concurrent"

    with SessionFactory() as seed_db:
        _seed_patient_and_cycle(seed_db, patient_id=patient_id, cycle_id=cycle_id)

    barrier = threading.Barrier(2, timeout=5.0)

    def _make_gateway(worker_label: str) -> GeminiGateway:
        async def _handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json=_gemini_envelope(
                    _athena_reply_with_clinical_report(
                        f"Athena response for {worker_label}",
                        summary=f"Clinical log for {worker_label}",
                    )
                ),
            )

        return GeminiGateway(
            api_key="test-key",
            transport=httpx.MockTransport(_handler),
            sleep_func=lambda _: asyncio.sleep(0),
        )

    results: Dict[str, Any] = {}
    errors: List[BaseException] = []

    def _run_worker(worker_label: str, user_msg: str, idem_key: str) -> None:
        try:
            with SessionFactory() as worker_session:
                # Separate lock_registry={} ensures workers do NOT share in-process asyncio.Lock
                service = AthenaChatService(
                    db=worker_session,
                    gateway=_make_gateway(worker_label),
                    lock_registry={},
                )
                barrier.wait()
                resp = asyncio.run(
                    service.handle_turn(
                        patient_id=patient_id,
                        cycle_id=cycle_id,
                        request=AthenaChatRequest(
                            message=user_msg,
                            event_type="user_message",
                            idempotency_key=idem_key,
                        ),
                    )
                )
                results[worker_label] = resp
        except BaseException as exc:
            errors.append(exc)

    try:
        t1 = threading.Thread(
            target=_run_worker,
            args=("Worker1", "User question from Worker 1", "idem_w1"),
        )
        t2 = threading.Thread(
            target=_run_worker,
            args=("Worker2", "User question from Worker 2", "idem_w2"),
        )
        t1.start()
        t2.start()
        t1.join(timeout=10.0)
        t2.join(timeout=10.0)

        assert not errors, f"Concurrent workers raised unexpected errors: {errors}"
        assert "Worker1" in results and "Worker2" in results

        with SessionFactory() as verify_db:
            weekly = WeeklyConsiliumService(db=verify_db)
            history = weekly.get_athena_chat_history(cycle_id)
            assert len(history) == 4
            texts = [m["text"] for m in history]
            assert "User question from Worker 1" in texts
            assert "Athena response for Worker1" in texts
            assert "User question from Worker 2" in texts
            assert "Athena response for Worker2" in texts

            rep_repo = ModelReportRepository(verify_db)
            logs = rep_repo.list_server_only(cycle_id, ATHENA_CLINICAL_LOG_REPORT_TYPE)
            assert len(logs) == 2

            turns = rep_repo.list_server_only(cycle_id, ATHENA_CHAT_TURN_REPORT_TYPE)
            assert len(turns) == 2
    finally:
        engine.dispose()


# ==============================================================================
# 2. ATOMIC TURN PERSISTENCE: SIMULATED FAILURE ROLLS BACK ENTIRE TURN
# ==============================================================================


@pytest.mark.parametrize(
    "fail_target",
    [
        "clinical_log",
        "chat_user_message",
        "chat_curator_message",
        "schedule_anchor",
        "idempotency_turn",
        "db_commit",
    ],
)
@pytest.mark.anyio
async def test_simulated_failure_during_persistence_rolls_back_entire_turn(
    tmp_path: Path,
    fail_target: str,
):
    """
    Verifies that a failure at ANY point during Athena turn persistence (clinical log,
    user message, Athena response, schedule anchor T0, idempotency record, or final commit)
    rolls back the entire turn atomically, leaving zero partial artifacts in SQLite.
    """
    db_path = tmp_path / f"atomic_rollback_{fail_target}.db"
    engine, SessionFactory = _create_file_session_factory(db_path)
    cycle_id = f"cycle_atomic_{fail_target}"
    patient_id = f"pat_atomic_{fail_target}"

    with SessionFactory() as seed_db:
        _seed_patient_and_cycle(seed_db, patient_id=patient_id, cycle_id=cycle_id)

    async def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=_gemini_envelope(
                _athena_reply_with_clinical_report(
                    "Привет! Как ваше самочувствие в первый день?",
                    summary="First post-primary greeting.",
                )
            ),
        )

    gateway = GeminiGateway(
        api_key="test-key",
        transport=httpx.MockTransport(_handler),
        sleep_func=lambda _: asyncio.sleep(0),
    )

    try:
        with SessionFactory() as turn_session:
            service = AthenaChatService(db=turn_session, gateway=gateway)
            orig_create_next = service.report_repo.create_next_version
            chat_append_count = {"count": 0}

            def _hooked_create_next(*args, **kwargs):
                rep_type = kwargs.get("report_type")
                if fail_target == "clinical_log" and rep_type == ATHENA_CLINICAL_LOG_REPORT_TYPE:
                    raise RuntimeError("Simulated DB crash at clinical_log")
                if rep_type == ATHENA_CHAT_HISTORY_REPORT_TYPE:
                    chat_append_count["count"] += 1
                    if fail_target == "chat_user_message" and chat_append_count["count"] == 1:
                        # Flush user message first, then fail immediately
                        orig_create_next(*args, **kwargs)
                        raise RuntimeError("Simulated DB crash right after flushing user message")
                    if fail_target == "chat_curator_message" and chat_append_count["count"] == 2:
                        orig_create_next(*args, **kwargs)
                        raise RuntimeError("Simulated DB crash right after flushing curator message")
                if fail_target == "schedule_anchor" and rep_type == ATHENA_SCHEDULE_ANCHOR_REPORT_TYPE:
                    orig_create_next(*args, **kwargs)
                    raise RuntimeError("Simulated DB crash right after flushing schedule anchor")
                if fail_target == "idempotency_turn" and rep_type == ATHENA_CHAT_TURN_REPORT_TYPE:
                    orig_create_next(*args, **kwargs)
                    raise RuntimeError("Simulated DB crash right after flushing idempotency turn")
                return orig_create_next(*args, **kwargs)

            service.report_repo.create_next_version = _hooked_create_next
            service.weekly_service.report_repo.create_next_version = _hooked_create_next

            if fail_target == "db_commit":
                orig_commit = turn_session.commit

                def _failing_commit():
                    raise RuntimeError("Simulated crash during final transaction commit")

                turn_session.commit = _failing_commit

            with pytest.raises(RuntimeError, match="Simulated"):
                await service.handle_turn(
                    patient_id=patient_id,
                    cycle_id=cycle_id,
                    request=AthenaChatRequest(
                        message="Старт сопровождения",
                        event_type="initial_greeting",
                        idempotency_key=f"idem_atomic_{fail_target}",
                    ),
                )

        # Verify in a brand-new Session that ZERO turn artifacts were persisted
        with SessionFactory() as verify_db:
            rep_repo = ModelReportRepository(verify_db)
            weekly = WeeklyConsiliumService(db=verify_db)

            assert weekly.get_athena_chat_history(cycle_id) == []
            assert rep_repo.list_server_only(cycle_id, ATHENA_CHAT_HISTORY_REPORT_TYPE) == []
            assert rep_repo.list_server_only(cycle_id, ATHENA_CLINICAL_LOG_REPORT_TYPE) == []
            assert rep_repo.list_server_only(cycle_id, ATHENA_SCHEDULE_ANCHOR_REPORT_TYPE) == []
            assert rep_repo.list_server_only(cycle_id, ATHENA_CHAT_TURN_REPORT_TYPE) == []
            assert weekly.find_schedule_anchor_timestamp(cycle_id) is None

        # Verify that a subsequent clean retry with the same idempotency_key succeeds completely
        with SessionFactory() as retry_session:
            clean_service = AthenaChatService(db=retry_session, gateway=gateway)
            ok_resp = await clean_service.handle_turn(
                patient_id=patient_id,
                cycle_id=cycle_id,
                request=AthenaChatRequest(
                    message="Старт сопровождения",
                    event_type="initial_greeting",
                    idempotency_key=f"idem_atomic_{fail_target}",
                ),
            )
            assert ok_resp.reply == "Привет! Как ваше самочувствие в первый день?"

        with SessionFactory() as verify_after_retry:
            rep_repo = ModelReportRepository(verify_after_retry)
            weekly = WeeklyConsiliumService(db=verify_after_retry)
            assert len(weekly.get_athena_chat_history(cycle_id)) == 2
            assert len(rep_repo.list_server_only(cycle_id, ATHENA_CLINICAL_LOG_REPORT_TYPE)) == 1
            assert len(rep_repo.list_server_only(cycle_id, ATHENA_SCHEDULE_ANCHOR_REPORT_TYPE)) == 1
            assert len(rep_repo.list_server_only(cycle_id, ATHENA_CHAT_TURN_REPORT_TYPE)) == 1
    finally:
        engine.dispose()


# ==============================================================================
# 3 & 4 & 5. IDEMPOTENCY, POST-COMMIT TIMEOUT RETRY, AND T0 ANCHOR STABILITY
# ==============================================================================


@pytest.mark.anyio
async def test_idempotent_retry_and_post_commit_timeout_preserve_single_turn_and_anchor_t0(
    tmp_path: Path,
):
    """
    Verifies:
    - Same-key retry returns the original saved response without new Gemini calls,
      duplicate chat messages, new clinical logs, or resetting the Athena schedule anchor T0.
    - Client timeout after server commit followed by retry at a later timestamp does not
      duplicate the turn or shift T0.
    - Subsequent daily_pulse and user_message turns keep T0 immutable at version 1.
    """
    db_path = tmp_path / "idempotency_and_t0_anchor.db"
    engine, SessionFactory = _create_file_session_factory(db_path)
    cycle_id = "cycle_bug08_idem_t0"
    patient_id = "pat_bug08_idem_t0"

    t0 = datetime(2026, 11, 3, 9, 0, 0, tzinfo=timezone.utc)
    current_clock = {"now": t0}

    with SessionFactory() as seed_db:
        _seed_patient_and_cycle(
            seed_db, patient_id=patient_id, cycle_id=cycle_id, now_dt=t0
        )

    gemini_calls: List[int] = []

    async def _handler(request: httpx.Request) -> httpx.Response:
        gemini_calls.append(1)
        call_num = len(gemini_calls)
        return httpx.Response(
            200,
            json=_gemini_envelope(
                _athena_reply_with_clinical_report(
                    f"Ответ Афины #{call_num}",
                    summary=f"Observation #{call_num}",
                )
            ),
        )

    gateway = GeminiGateway(
        api_key="test-key",
        transport=httpx.MockTransport(_handler),
        sleep_func=lambda _: asyncio.sleep(0),
    )

    try:
        # 1. First initial_greeting turn commits on server, establishing T0
        with SessionFactory() as session_1:
            service_1 = AthenaChatService(
                db=session_1,
                gateway=gateway,
                now_provider=lambda: current_clock["now"],
            )
            resp_1 = await service_1.handle_turn(
                patient_id=patient_id,
                cycle_id=cycle_id,
                request=AthenaChatRequest(
                    message="Первый запуск",
                    event_type="initial_greeting",
                    idempotency_key="athena_init_greeting_key_1",
                ),
            )
            assert resp_1.reply == "Ответ Афины #1"
            assert len(gemini_calls) == 1

        # 2. Simulate client network timeout after server commit: advance clock by 2 hours
        #    and retry with the exact same idempotency_key on a new session
        current_clock["now"] = t0 + timedelta(hours=2)
        with SessionFactory() as session_retry:
            service_retry = AthenaChatService(
                db=session_retry,
                gateway=gateway,
                now_provider=lambda: current_clock["now"],
            )
            resp_retry = await service_retry.handle_turn(
                patient_id=patient_id,
                cycle_id=cycle_id,
                request=AthenaChatRequest(
                    message="Первый запуск",
                    event_type="initial_greeting",
                    idempotency_key="athena_init_greeting_key_1",
                ),
            )
            # Exact stored response returned with ZERO new Gemini calls
            assert len(gemini_calls) == 1
            assert resp_retry.message_id == resp_1.message_id
            assert resp_retry.reply == resp_1.reply
            assert resp_retry.created_at == resp_1.created_at

        # 3. Advance clock to Day 2 (+25h) and run a new Daily Pulse turn
        current_clock["now"] = t0 + timedelta(hours=25)
        with SessionFactory() as session_d2:
            service_d2 = AthenaChatService(
                db=session_d2,
                gateway=gateway,
                now_provider=lambda: current_clock["now"],
            )
            resp_d2 = await service_d2.handle_turn(
                patient_id=patient_id,
                cycle_id=cycle_id,
                request=AthenaChatRequest(
                    message="Ежедневный опрос",
                    event_type="daily_pulse",
                    sprint_number=1,
                    sprint_day=2,
                    idempotency_key="athena_daily_pulse_cycle_bug08_idem_t0_w1_d2",
                ),
            )
            assert resp_d2.reply == "Ответ Афины #2"
            assert len(gemini_calls) == 2

        # Verify database state: 4 chat messages (2 turns), 2 clinical logs, and T0 anchor strictly at t0 (version 1)
        with SessionFactory() as verify_db:
            weekly = WeeklyConsiliumService(
                db=verify_db, now_provider=lambda: current_clock["now"]
            )
            rep_repo = ModelReportRepository(verify_db)

            chat_history = weekly.get_athena_chat_history(cycle_id)
            assert len(chat_history) == 4

            logs = rep_repo.list_server_only(cycle_id, ATHENA_CLINICAL_LOG_REPORT_TYPE)
            assert len(logs) == 2

            anchors = rep_repo.list_server_only(cycle_id, ATHENA_SCHEDULE_ANCHOR_REPORT_TYPE)
            assert len(anchors) == 1
            assert anchors[0].version == 1
            assert weekly.find_schedule_anchor_timestamp(cycle_id) == t0
    finally:
        engine.dispose()


def test_concurrent_same_key_retries_across_workers_commit_only_once(tmp_path: Path):
    """
    Verifies that if two cross-process workers simultaneously process a turn with the
    same idempotency_key (e.g., timeout + immediate retry while Worker 1 is still in Gemini),
    only one turn is persisted and both workers return the identical stored response without
    duplicating chat messages or clinical logs.
    """
    db_path = tmp_path / "concurrent_same_idem_key.db"
    engine, SessionFactory = _create_file_session_factory(db_path)
    cycle_id = "cycle_bug08_same_idem"
    patient_id = "pat_bug08_same_idem"

    with SessionFactory() as seed_db:
        _seed_patient_and_cycle(seed_db, patient_id=patient_id, cycle_id=cycle_id)

    barrier = threading.Barrier(2, timeout=5.0)

    async def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=_gemini_envelope(
                _athena_reply_with_clinical_report(
                    "Единый идемпотентный ответ Афины",
                    summary="Single idempotent log",
                )
            ),
        )

    responses: List[Any] = []
    errors: List[BaseException] = []

    def _worker() -> None:
        try:
            with SessionFactory() as s:
                gw = GeminiGateway(
                    api_key="test-key",
                    transport=httpx.MockTransport(_handler),
                    sleep_func=lambda _: asyncio.sleep(0),
                )
                svc = AthenaChatService(db=s, gateway=gw, lock_registry={})
                barrier.wait()
                res = asyncio.run(
                    svc.handle_turn(
                        patient_id=patient_id,
                        cycle_id=cycle_id,
                        request=AthenaChatRequest(
                            message="Привет, Афина!",
                            event_type="user_message",
                            idempotency_key="shared_idem_key_001",
                        ),
                    )
                )
                responses.append(res)
        except BaseException as exc:
            errors.append(exc)

    try:
        t1 = threading.Thread(target=_worker)
        t2 = threading.Thread(target=_worker)
        t1.start()
        t2.start()
        t1.join(timeout=10.0)
        t2.join(timeout=10.0)

        assert not errors, f"Unexpected errors: {errors}"
        assert len(responses) == 2
        assert responses[0].message_id == responses[1].message_id
        assert responses[0].reply == responses[1].reply

        with SessionFactory() as verify_db:
            weekly = WeeklyConsiliumService(db=verify_db)
            rep_repo = ModelReportRepository(verify_db)
            assert len(weekly.get_athena_chat_history(cycle_id)) == 2
            assert len(rep_repo.list_server_only(cycle_id, ATHENA_CLINICAL_LOG_REPORT_TYPE)) == 1
            assert len(rep_repo.list_server_only(cycle_id, ATHENA_CHAT_TURN_REPORT_TYPE)) == 1
    finally:
        engine.dispose()


# ==============================================================================
# 6 & 7. PATIENT/CYCLE ISOLATION AND SQLITE LOCKING RETRY RECOVERY
# ==============================================================================


@pytest.mark.anyio
async def test_patient_and_cycle_isolation_preserved_across_turns(tmp_path: Path):
    """
    Verifies that chat history, clinical logs, schedule anchors, and idempotency keys
    are strictly isolated by patient_id and cycle_id, and foreign cycle access is rejected.
    """
    db_path = tmp_path / "patient_cycle_isolation.db"
    engine, SessionFactory = _create_file_session_factory(db_path)

    with SessionFactory() as seed_db:
        _seed_patient_and_cycle(seed_db, patient_id="pat_A", cycle_id="cycle_A1")
        _seed_patient_and_cycle(seed_db, patient_id="pat_B", cycle_id="cycle_B1")

    async def _handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        last_text = body["contents"][-1]["parts"][0]["text"]
        tag = "PAT_A" if "Пациент А" in last_text else "PAT_B"
        return httpx.Response(
            200,
            json=_gemini_envelope(
                _athena_reply_with_clinical_report(f"Ответ для {tag}", summary=f"Log {tag}")
            ),
        )

    gateway = GeminiGateway(
        api_key="test-key",
        transport=httpx.MockTransport(_handler),
        sleep_func=lambda _: asyncio.sleep(0),
    )

    try:
        with SessionFactory() as session:
            service = AthenaChatService(db=session, gateway=gateway)

            # Same idempotency_key string used by two different patients/cycles must NOT collide
            resp_a = await service.handle_turn(
                patient_id="pat_A",
                cycle_id="cycle_A1",
                request=AthenaChatRequest(
                    message="Сообщение: Пациент А",
                    event_type="user_message",
                    idempotency_key="shared_key_per_cycle",
                ),
            )
            resp_b = await service.handle_turn(
                patient_id="pat_B",
                cycle_id="cycle_B1",
                request=AthenaChatRequest(
                    message="Сообщение: Пациент Б",
                    event_type="user_message",
                    idempotency_key="shared_key_per_cycle",
                ),
            )

            assert resp_a.reply == "Ответ для PAT_A"
            assert resp_b.reply == "Ответ для PAT_B"

            # Patient A attempting to access Patient B's cycle is rejected with 404
            from fastapi import HTTPException

            with pytest.raises(HTTPException) as exc_info:
                await service.handle_turn(
                    patient_id="pat_A",
                    cycle_id="cycle_B1",
                    request=AthenaChatRequest(message="Чужой цикл"),
                )
            assert exc_info.value.status_code == 404

            hist_a = service.get_sanitized_history(patient_id="pat_A", cycle_id="cycle_A1")
            hist_b = service.get_sanitized_history(patient_id="pat_B", cycle_id="cycle_B1")

            assert [m.text for m in hist_a.messages] == [
                "Сообщение: Пациент А",
                "Ответ для PAT_A",
            ]
            assert [m.text for m in hist_b.messages] == [
                "Сообщение: Пациент Б",
                "Ответ для PAT_B",
            ]
    finally:
        engine.dispose()


@pytest.mark.anyio
async def test_sqlite_transient_operational_error_retries_and_commits_atomically(
    tmp_path: Path,
):
    """
    Verifies that transient SQLite OperationalError ('database is locked') during
    Athena turn persistence triggers rollback + bounded retry and commits the complete
    turn atomically without partial artifacts or duplicate Gemini calls.
    """
    db_path = tmp_path / "sqlite_lock_retry.db"
    engine, SessionFactory = _create_file_session_factory(db_path)
    cycle_id = "cycle_bug08_lock_retry"
    patient_id = "pat_bug08_lock_retry"

    with SessionFactory() as seed_db:
        _seed_patient_and_cycle(seed_db, patient_id=patient_id, cycle_id=cycle_id)

    gemini_calls = {"count": 0}

    async def _handler(request: httpx.Request) -> httpx.Response:
        gemini_calls["count"] += 1
        return httpx.Response(
            200,
            json=_gemini_envelope(
                _athena_reply_with_clinical_report(
                    "Ответ после восстановления блокировки SQLite",
                    summary="Recovered after transient lock",
                )
            ),
        )

    gateway = GeminiGateway(
        api_key="test-key",
        transport=httpx.MockTransport(_handler),
        sleep_func=lambda _: asyncio.sleep(0),
    )

    try:
        with SessionFactory() as session:
            service = AthenaChatService(db=session, gateway=gateway)
            orig_create = service.report_repo.create

            flaky_attempts = {"count": 0}

            def _flaky_create(*args, **kwargs):
                if kwargs.get("report_type") == ATHENA_CHAT_HISTORY_REPORT_TYPE:
                    flaky_attempts["count"] += 1
                    if flaky_attempts["count"] <= 2:
                        raise OperationalError(
                            "INSERT INTO model_reports ...",
                            {},
                            Exception("database is locked"),
                        )
                return orig_create(*args, **kwargs)

            service.report_repo.create = _flaky_create
            service.weekly_service.report_repo.create = _flaky_create

            resp = await service.handle_turn(
                patient_id=patient_id,
                cycle_id=cycle_id,
                request=AthenaChatRequest(
                    message="Проверка устойчивости к SQLite busy",
                    event_type="user_message",
                    idempotency_key="idem_sqlite_busy_01",
                ),
            )

            assert resp.reply == "Ответ после восстановления блокировки SQLite"
            # Gemini was called only ONCE despite 2 transient SQLite lock retries during persistence
            assert gemini_calls["count"] == 1
            assert flaky_attempts["count"] >= 3

        with SessionFactory() as verify_db:
            weekly = WeeklyConsiliumService(db=verify_db)
            rep_repo = ModelReportRepository(verify_db)
            history = weekly.get_athena_chat_history(cycle_id)
            assert len(history) == 2
            assert history[0]["text"] == "Проверка устойчивости к SQLite busy"
            assert history[1]["text"] == "Ответ после восстановления блокировки SQLite"
            assert len(rep_repo.list_server_only(cycle_id, ATHENA_CLINICAL_LOG_REPORT_TYPE)) == 1
            assert len(rep_repo.list_server_only(cycle_id, ATHENA_CHAT_TURN_REPORT_TYPE)) == 1
    finally:
        engine.dispose()
