import asyncio
from datetime import datetime, timezone, timedelta
import json
from typing import Any, Callable, Dict, Generator, List
import concurrent.futures
import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import settings
from app.database import get_db
from app.main import app
from app.models.base import Base
from app.models.medical_cycle import MedicalCycle
from app.repositories.cycle_repository import MedicalCycleRepository
from app.repositories.pairing_repository import PairingRepository
from app.repositories.patient_repository import PatientRepository
from app.repositories.report_repository import ModelReportRepository
from app.repositories.sprint_repository import WeeklySprintRepository
from app.routers.client_cascade import (
    get_gemini_gateway as client_get_gemini_gateway,
    get_server_now_provider as client_get_server_now_provider,
)
from app.routers.client_chat import (
    get_gemini_gateway as chat_get_gemini_gateway,
    get_server_now_provider as chat_get_server_now_provider,
)
from app.routers.cascade import get_gemini_gateway as cascade_get_gemini_gateway
from app.schemas.consilium import SYSTEM_KEYS
from app.schemas.privacy import PrivacyLevel
from app.security.tokens import create_access_token, verify_access_token
from app.services.gemini_gateway import GeminiGateway
from app.services.primary_consilium_service import PrimaryConsiliumService
from app.services.weekly_consilium_service import (
    WeeklyConsiliumService,
    ATHENA_SCHEDULE_ANCHOR_REPORT_TYPE,
    ATHENA_CLINICAL_LOG_REPORT_TYPE,
)
from app.services.athena_chat_service import AthenaChatService
from app.schemas.chat import AthenaChatRequest
from tests.test_primary_consilium import (
    _gemini_envelope,
    _sample_anamnesis_output,
    _seed_preparatory_cycle_context,
    _build_consilium_mock_gateway,
)
from tests.test_weekly_consilium import (
    _sample_weekly_flash_output,
    _sample_weekly_pro_output,
)


TEST_JWT_SECRET = "test_jwt_scheduling_secret_1234567890!"
TEST_PAIRING_SECRET = "test_pairing_secret_scheduling_12345!"
TEST_API_SECRET = "test_device_secret_scheduling_12345"


def _build_mock_gateway(recorded_calls: List[Dict[str, Any]]) -> GeminiGateway:
    call_counter = {"index": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        idx = call_counter["index"]
        call_counter["index"] += 1
        body = json.loads(request.content.decode("utf-8"))
        prompt_text = body["contents"][0]["parts"][0]["text"]
        url_path = request.url.path

        recorded_calls.append({
            "index": idx,
            "url_path": url_path,
            "prompt": prompt_text,
        })

        if "pro" in url_path.lower():
            return httpx.Response(200, json=_gemini_envelope(_sample_weekly_pro_output()))
        # Handle athena chat mock: return warm response with valid hidden [CLINICAL_REPORT]
        if "service pulse:" in prompt_text.lower() or "first introduction" in prompt_text.lower() or "just checking in" in prompt_text.lower():
            return httpx.Response(200, json=_gemini_envelope(
                "Здравствуйте! Как ваше самочувствие сегодня? Примите утренние препараты.\n\n"
                "[CLINICAL_REPORT]\n"
                '{\n'
                '  "status": "NORMAL",\n'
                '  "symptoms_mentioned": [],\n'
                '  "subjective_wellbeing_score": 8,\n'
                '  "adherence_to_plan": "HIGH",\n'
                '  "red_flags": false,\n'
                '  "summary_for_master": "Athena daily check-in dispatched with routine question."\n'
                '}\n'
                '[/CLINICAL_REPORT]'
            ))
        return httpx.Response(200, json=_gemini_envelope(_sample_weekly_flash_output()))

    return GeminiGateway(
        api_key="test-scheduling-gateway-key",
        transport=httpx.MockTransport(handler),
        sleep_func=lambda _: None,
    )


class ControlledTimeFixture:
    def __init__(self, start_dt: datetime):
        self.current_time = start_dt

    def now(self) -> datetime:
        return self.current_time

    def advance(self, **kwargs):
        self.current_time = self.current_time + timedelta(**kwargs)


@pytest.fixture(scope="function")
def sched_test_env(monkeypatch, tmp_path) -> Generator[Any, None, None]:
    monkeypatch.setattr(settings, "api_secret", TEST_API_SECRET)
    monkeypatch.setattr(settings, "pairing_secret", TEST_PAIRING_SECRET)
    monkeypatch.setattr(settings, "jwt_secret", TEST_JWT_SECRET)
    monkeypatch.setattr(settings, "jwt_issuer", "aegis-backend")
    monkeypatch.setattr(settings, "jwt_audience", "aegis-mobile-client")
    monkeypatch.setattr(settings, "gemini_api_key", "test-gemini-key")

    db_file = tmp_path / "test_weekly_sprint_scheduling.db"
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
    mock_gw = _build_mock_gateway(recorded_calls)
    app.dependency_overrides[client_get_gemini_gateway] = lambda: mock_gw
    app.dependency_overrides[cascade_get_gemini_gateway] = lambda: mock_gw
    app.dependency_overrides[chat_get_gemini_gateway] = lambda: mock_gw

    base_time = datetime(2026, 10, 1, 8, 0, 0, tzinfo=timezone.utc)
    time_control = ControlledTimeFixture(base_time)
    app.dependency_overrides[client_get_server_now_provider] = lambda: time_control.now
    app.dependency_overrides[chat_get_server_now_provider] = lambda: time_control.now

    try:
        with TestClient(app) as client:
            yield client, session, recorded_calls, mock_gw, time_control
    finally:
        app.dependency_overrides.clear()
        session.close()
        eng.dispose()


def _pair_and_get_token(client: TestClient, session: Session, patient_id: str, device_id: str) -> str:
    PatientRepository(session).create(patient_id=patient_id, status="active")
    PairingRepository(session).create_code(patient_id=patient_id, raw_code_override="123456")
    session.commit()

    resp = client.post(
        "/api/v1/auth/pair",
        json={
            "pairing_code": "123456",
            "device_id": device_id,
            "platform": "windows",
            "device_name": "Test Device",
        },
    )
    assert resp.status_code == 200, resp.text
    token_bundle = resp.json()
    fam_id = verify_access_token(token_bundle["access_token"]).family_id
    jwt_token = create_access_token(
        patient_id=patient_id,
        device_id=device_id,
        family_id=fam_id,
        scope="cycle:read cascade:run chat:write chat:read",
    ).access_token
    return jwt_token


async def _seed_primary(session: Session, cycle_id: str, gw: GeminiGateway, now_provider: Callable[[], datetime]):
    _seed_preparatory_cycle_context(session, cycle_id=cycle_id, seed_ledger=True)
    p_calls: List[Dict[str, Any]] = []
    p_gw = _build_consilium_mock_gateway(p_calls)
    p_service = PrimaryConsiliumService(db=session, gateway=p_gw, now_provider=now_provider)
    await p_service.run_primary_consilium(cycle_id=cycle_id, report_date="2026-10-01")


# ==============================================================================
# TARGETED CHECK 1: REAL FIRST ATHENA DAILY QUESTION (DAILY_PULSE)
# ==============================================================================

@pytest.mark.anyio
async def test_anchor_real_first_daily_question_establishes_schedule(sched_test_env):
    """
    Verifies that the reference point (T_0) is fixed only after the first confirmed
    meaningful daily question of Athena (event_type == 'daily_pulse') after Primary Consilium.
    - Anchor is saved once with UTC timestamp T_0.
    - Status before 168h: can_start_next_sprint=False.
    - Status at 168h: can_start_next_sprint=True, next_sprint_id='sprint_{cycle}_w1'.
    """
    client, session, _, mock_gw, time_ctrl = sched_test_env
    patient_id = "pat_real_pulse"
    token = _pair_and_get_token(client, session, patient_id, "dev_real_pulse")
    headers = {"Authorization": f"Bearer {token}"}

    cycle, _ = MedicalCycleRepository(session).get_or_create_active_for_patient(patient_id)
    await _seed_primary(session, cycle.cycle_id, mock_gw, time_ctrl.now)

    weekly_service = WeeklyConsiliumService(db=session, gateway=mock_gw, now_provider=time_ctrl.now)
    assert weekly_service.find_schedule_anchor_timestamp(cycle.cycle_id) is None

    # Athena sends the first real daily question via active path
    pulse_resp = client.post(
        "/api/v1/client/chat/athena",
        json={
            "event_type": "daily_pulse",
            "message": "Daily pulse check-in",
            "sprint_day": 1,
            "idempotency_key": "pulse_turn_01",
        },
        headers=headers,
    )
    assert pulse_resp.status_code == 200, pulse_resp.text
    pulse_data = pulse_resp.json()
    assert pulse_data["reply"] is not None

    # Confirmed anchor is established at time_ctrl.now()
    anchor_dt = weekly_service.find_schedule_anchor_timestamp(cycle.cycle_id)
    assert anchor_dt is not None
    assert anchor_dt == time_ctrl.now()

    # Before 168 hours: cannot start Week 1
    st1 = client.get("/api/v1/client/cascade/status", headers=headers).json()
    assert st1["can_start_next_sprint"] is False
    assert st1["next_sprint_id"] is None

    # Advance exactly 168 hours: Week 1 eligible
    time_ctrl.advance(hours=168)
    st2 = client.get("/api/v1/client/cascade/status", headers=headers).json()
    assert st2["can_start_next_sprint"] is True
    assert st2["next_sprint_id"] == f"sprint_{cycle.cycle_id}_w1"


# ==============================================================================
# TARGETED CHECK 2A: INITIAL GREETING AFTER PRIMARY CREATES ANCHOR
# ==============================================================================

@pytest.mark.anyio
async def test_anchor_created_on_initial_greeting_after_primary(sched_test_env):
    """
    initial_greeting after Primary Consilium is completed establishes the
    authoritative schedule anchor T_0 at time of first curator response.
    """
    client, session, _, mock_gw, time_ctrl = sched_test_env
    patient_id = "pat_greeting_after_primary"
    token = _pair_and_get_token(client, session, patient_id, "dev_greeting_after")
    headers = {"Authorization": f"Bearer {token}"}

    cycle, _ = MedicalCycleRepository(session).get_or_create_active_for_patient(patient_id)
    await _seed_primary(session, cycle.cycle_id, mock_gw, time_ctrl.now)

    weekly_service = WeeklyConsiliumService(db=session, gateway=mock_gw, now_provider=time_ctrl.now)
    assert weekly_service.find_schedule_anchor_timestamp(cycle.cycle_id) is None

    r = client.post(
        "/api/v1/client/chat/athena",
        json={"event_type": "initial_greeting", "idempotency_key": "greeting_post_pri_01"},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    anchor_dt = weekly_service.find_schedule_anchor_timestamp(cycle.cycle_id)
    assert anchor_dt is not None
    assert anchor_dt == time_ctrl.now()

    # Before 168h, cannot start Week 1
    st1 = client.get("/api/v1/client/cascade/status", headers=headers).json()
    assert st1["can_start_next_sprint"] is False
    assert st1["next_sprint_id"] is None

    # Advance 168h -> Week 1 becomes eligible
    time_ctrl.advance(hours=168)
    st2 = client.get("/api/v1/client/cascade/status", headers=headers).json()
    assert st2["can_start_next_sprint"] is True
    assert st2["next_sprint_id"] == f"sprint_{cycle.cycle_id}_w1"


# ==============================================================================
# TARGETED CHECK 2B: INITIAL GREETING BEFORE PRIMARY NEVER CREATES ANCHOR
# ==============================================================================

@pytest.mark.anyio
async def test_anchor_not_created_on_initial_greeting_before_primary(sched_test_env):
    """
    initial_greeting before Primary Consilium must NOT create an anchor.
    """
    client, session, _, mock_gw, time_ctrl = sched_test_env
    patient_id = "pat_greeting_before_primary"
    token = _pair_and_get_token(client, session, patient_id, "dev_greeting_before")
    headers = {"Authorization": f"Bearer {token}"}

    cycle, _ = MedicalCycleRepository(session).get_or_create_active_for_patient(patient_id)
    # Note: Primary Consilium is NOT completed for this cycle

    weekly_service = WeeklyConsiliumService(db=session, gateway=mock_gw, now_provider=time_ctrl.now)
    assert weekly_service.find_schedule_anchor_timestamp(cycle.cycle_id) is None

    r = client.post(
        "/api/v1/client/chat/athena",
        json={"event_type": "initial_greeting", "idempotency_key": "greeting_pre_pri_01"},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    assert weekly_service.find_schedule_anchor_timestamp(cycle.cycle_id) is None

    st = client.get("/api/v1/client/cascade/status", headers=headers).json()
    assert st["can_start_next_sprint"] is False
    assert st["next_sprint_id"] is None


# ==============================================================================
# TARGETED CHECK 3: TIMEOUT REPLAY AND REPEAT NEVER ALTER T0
# ==============================================================================

@pytest.mark.anyio
async def test_anchor_timeout_replay_and_repeat_never_alter_t0(sched_test_env):
    """
    Replay after timeout / network error and repeated greetings or pulses
    must NEVER alter or shift the original T_0 anchor timestamp.
    """
    client, session, _, mock_gw, time_ctrl = sched_test_env
    patient_id = "pat_idem_t0_stable"
    token = _pair_and_get_token(client, session, patient_id, "dev_idem_t0")
    headers = {"Authorization": f"Bearer {token}"}

    cycle, _ = MedicalCycleRepository(session).get_or_create_active_for_patient(patient_id)
    await _seed_primary(session, cycle.cycle_id, mock_gw, time_ctrl.now)

    t0_expected = time_ctrl.now()

    # 1. First initial_greeting turn establishes T_0
    r1 = client.post(
        "/api/v1/client/chat/athena",
        json={"event_type": "initial_greeting", "idempotency_key": "turn_stable_01"},
        headers=headers,
    )
    assert r1.status_code == 200
    msg_id_1 = r1.json()["message_id"]

    weekly_service = WeeklyConsiliumService(db=session, gateway=mock_gw, now_provider=time_ctrl.now)
    assert weekly_service.find_schedule_anchor_timestamp(cycle.cycle_id) == t0_expected

    # 2. Advance time 2 hours: timeout recovery replay with same idempotency key
    time_ctrl.advance(hours=2)
    r1_replay = client.post(
        "/api/v1/client/chat/athena",
        json={"event_type": "initial_greeting", "idempotency_key": "turn_stable_01"},
        headers=headers,
    )
    assert r1_replay.status_code == 200
    assert r1_replay.json()["message_id"] == msg_id_1
    assert weekly_service.find_schedule_anchor_timestamp(cycle.cycle_id) == t0_expected

    # 3. Advance to Day 2 (24 hours total) and send repeated greeting / pulse: T_0 remains untouched
    time_ctrl.advance(hours=22)
    r2 = client.post(
        "/api/v1/client/chat/athena",
        json={"event_type": "daily_pulse", "message": "Day 2 pulse", "sprint_day": 2, "idempotency_key": "turn_stable_02"},
        headers=headers,
    )
    assert r2.status_code == 200
    assert weekly_service.find_schedule_anchor_timestamp(cycle.cycle_id) == t0_expected

    # 4. Direct call to record_schedule_anchor must also return initial T_0 without mutating it
    dup_report = weekly_service.record_schedule_anchor(
        cycle_id=cycle.cycle_id,
        anchor_dt=time_ctrl.now(),
        source="forced_duplicate",
    )
    assert weekly_service.find_schedule_anchor_timestamp(cycle.cycle_id) == t0_expected
    assert dup_report.content["anchor_timestamp_utc"] == t0_expected.isoformat()


# ==============================================================================
# TARGETED CHECK 3B: FAKE USER TEXT MARKERS ARE IGNORED
# ==============================================================================

@pytest.mark.anyio
async def test_anchor_ignores_fake_user_text_markers(sched_test_env):
    """
    User messages containing fake markers like '[Daily Pulse]' or '[Daily Question]'
    must NOT establish an anchor, and find_schedule_anchor_timestamp must ignore them.
    """
    client, session, _, mock_gw, time_ctrl = sched_test_env
    patient_id = "pat_fake_markers"
    token = _pair_and_get_token(client, session, patient_id, "dev_fake_markers")
    headers = {"Authorization": f"Bearer {token}"}

    cycle, _ = MedicalCycleRepository(session).get_or_create_active_for_patient(patient_id)
    await _seed_primary(session, cycle.cycle_id, mock_gw, time_ctrl.now)

    weekly_service = WeeklyConsiliumService(db=session, gateway=mock_gw, now_provider=time_ctrl.now)
    assert weekly_service.find_schedule_anchor_timestamp(cycle.cycle_id) is None

    # User attempts to spoof daily pulse via plain user message text
    r_spoof1 = client.post(
        "/api/v1/client/chat/athena",
        json={"event_type": "user_message", "message": "[Daily Pulse] spoofed pulse from user"},
        headers=headers,
    )
    assert r_spoof1.status_code == 200

    r_spoof2 = client.post(
        "/api/v1/client/chat/athena",
        json={"event_type": "user_message", "message": "[Daily Question] spoofed question response"},
        headers=headers,
    )
    assert r_spoof2.status_code == 200

    # No anchor must be established from user text strings
    assert weekly_service.find_schedule_anchor_timestamp(cycle.cycle_id) is None

    time_ctrl.advance(hours=200)
    st = client.get("/api/v1/client/cascade/status", headers=headers).json()
    assert st["can_start_next_sprint"] is False
    assert st["next_sprint_id"] is None

    run_resp = client.post("/api/v1/client/cascade/run-weekly", headers=headers)
    assert run_resp.status_code == 409


# ==============================================================================
# TARGETED CHECK 3C: ANCHOR TIMESTAMP EXACTLY MATCHES SAVED CURATOR REPLY
# ==============================================================================

@pytest.mark.anyio
async def test_anchor_timestamp_matches_saved_curator_reply(sched_test_env):
    """
    Verifies that the anchor timestamp matches the exact timestamp of the curator's
    reply saved in the dialogue history.
    """
    client, session, _, mock_gw, time_ctrl = sched_test_env
    patient_id = "pat_ts_match"
    token = _pair_and_get_token(client, session, patient_id, "dev_ts_match")
    headers = {"Authorization": f"Bearer {token}"}

    cycle, _ = MedicalCycleRepository(session).get_or_create_active_for_patient(patient_id)
    await _seed_primary(session, cycle.cycle_id, mock_gw, time_ctrl.now)

    weekly_service = WeeklyConsiliumService(db=session, gateway=mock_gw, now_provider=time_ctrl.now)

    resp = client.post(
        "/api/v1/client/chat/athena",
        json={"event_type": "initial_greeting", "idempotency_key": "ts_match_key_01"},
        headers=headers,
    )
    assert resp.status_code == 200

    anchor_dt = weekly_service.find_schedule_anchor_timestamp(cycle.cycle_id)
    assert anchor_dt is not None

    # Inspect chat history
    history = weekly_service.get_athena_chat_history(cycle.cycle_id)
    curator_turns = [m for m in history if m.get("sender") == "curator"]
    assert len(curator_turns) >= 1
    assert curator_turns[-1]["timestamp"] == anchor_dt.isoformat()


# ==============================================================================
# TARGETED CHECK 3D: CONCURRENT INITIAL REQUESTS NEVER CREATE TWO ANCHORS
# ==============================================================================

@pytest.mark.anyio
async def test_concurrent_initial_requests_never_create_duplicate_anchors(sched_test_env):
    """
    Two concurrent initial Athena chat requests for the same medical cycle
    must be protected by concurrency lock and produce exactly ONE anchor report.
    """
    client, session, _, mock_gw, time_ctrl = sched_test_env
    patient_id = "pat_concurrent_anchors"
    token = _pair_and_get_token(client, session, patient_id, "dev_concurrent_anchors")
    headers = {"Authorization": f"Bearer {token}"}

    cycle, _ = MedicalCycleRepository(session).get_or_create_active_for_patient(patient_id)
    await _seed_primary(session, cycle.cycle_id, mock_gw, time_ctrl.now)

    def _send_initial(idx: int):
        return client.post(
            "/api/v1/client/chat/athena",
            json={"event_type": "initial_greeting", "idempotency_key": f"concurrent_greet_{idx}"},
            headers=headers,
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        f1 = executor.submit(_send_initial, 1)
        f2 = executor.submit(_send_initial, 2)
        r1 = f1.result()
        r2 = f2.result()

    assert r1.status_code == 200
    assert r2.status_code == 200

    # Query all server-only anchor reports in the database
    report_repo = ModelReportRepository(session)
    anchors = report_repo.list_server_only(
        cycle_id=cycle.cycle_id,
        report_type=ATHENA_SCHEDULE_ANCHOR_REPORT_TYPE,
    )
    assert len(anchors) == 1, f"Expected exactly 1 anchor report, found {len(anchors)}"



# ==============================================================================
# TARGETED CHECK 4: ABSENCE OF QUESTIONS REFUSES TO GUESS
# ==============================================================================

@pytest.mark.anyio
async def test_anchor_absence_of_daily_questions_refuses_to_guess(sched_test_env):
    """
    If Athena has not dispatched any daily pulse question (e.g. only normal user messages):
    - Server refuses to guess start date.
    - can_start_next_sprint=False, next_sprint_id=None.
    - POST /run-weekly returns 409 Conflict.
    """
    client, session, _, mock_gw, time_ctrl = sched_test_env
    patient_id = "pat_no_questions"
    token = _pair_and_get_token(client, session, patient_id, "dev_no_questions")
    headers = {"Authorization": f"Bearer {token}"}

    cycle, _ = MedicalCycleRepository(session).get_or_create_active_for_patient(patient_id)
    await _seed_primary(session, cycle.cycle_id, mock_gw, time_ctrl.now)

    # User sends a normal message, but no daily pulse was triggered
    r_msg = client.post(
        "/api/v1/client/chat/athena",
        json={"event_type": "user_message", "message": "Hello Athena, just checking in!"},
        headers=headers,
    )
    assert r_msg.status_code == 200

    weekly_service = WeeklyConsiliumService(db=session, gateway=mock_gw, now_provider=time_ctrl.now)
    assert weekly_service.find_schedule_anchor_timestamp(cycle.cycle_id) is None

    # Advance past 168 hours from creation: still refuses to guess start date
    time_ctrl.advance(hours=200)

    st = client.get("/api/v1/client/cascade/status", headers=headers).json()
    assert st["can_start_next_sprint"] is False
    assert st["next_sprint_id"] is None

    run_resp = client.post("/api/v1/client/cascade/run-weekly", headers=headers)
    assert run_resp.status_code == 409
    assert "not eligible to start" in run_resp.text.lower()


# ==============================================================================
# TARGETED CHECK 5: TRANSITION WEEK 1 -> WEEK 2 WITH EXPLICIT NEXT_SPRINT_ID
# ==============================================================================

@pytest.mark.anyio
async def test_weekly_post_transition_to_week_2_with_explicit_next_sprint_id(sched_test_env):
    """
    Verifies Weekly POST lifecycle rules:
    - Calling run-weekly with no sprint_id after Week 1 completed performs safe replay (X-Aegis-Idempotent-Replay).
    - Transition to Week 2 requires server-authorized next_sprint_id and arrival of 336h boundary.
    - Unauthorized or premature future sprints are strictly rejected with 409 Conflict.
    """
    client, session, recorded_calls, mock_gw, time_ctrl = sched_test_env
    patient_id = "pat_transition_w2"
    token = _pair_and_get_token(client, session, patient_id, "dev_transition_w2")
    headers = {"Authorization": f"Bearer {token}"}

    cycle, _ = MedicalCycleRepository(session).get_or_create_active_for_patient(patient_id)
    await _seed_primary(session, cycle.cycle_id, mock_gw, time_ctrl.now)

    # Establish anchor via daily pulse
    client.post(
        "/api/v1/client/chat/athena",
        json={"event_type": "daily_pulse", "message": "Pulse 1", "sprint_day": 1},
        headers=headers,
    )

    # Advance 168 hours -> run Week 1
    time_ctrl.advance(hours=168)
    w1_resp = client.post("/api/v1/client/cascade/run-weekly", headers=headers)
    assert w1_resp.status_code == 200
    assert w1_resp.json()["sprint_id"] == f"sprint_{cycle.cycle_id}_w1"

    ai_calls_after_w1 = len(recorded_calls)

    # 1. Calling POST /run-weekly without sprint_id = safe replay of Week 1
    replay_resp = client.post("/api/v1/client/cascade/run-weekly", headers=headers)
    assert replay_resp.status_code == 200
    assert replay_resp.headers.get("X-Aegis-Idempotent-Replay") == "true"
    assert len(recorded_calls) == ai_calls_after_w1  # Zero additional AI calls

    # 2. Calling with premature next_sprint_id before 336 hours = 409 Conflict
    w2_id = f"sprint_{cycle.cycle_id}_w2"
    premature_resp = client.post(
        "/api/v1/client/cascade/run-weekly",
        json={"sprint_id": w2_id},
        headers=headers,
    )
    assert premature_resp.status_code == 409
    assert "not authorized" in premature_resp.text.lower()

    # 3. Calling with arbitrary unauthorized future sprint = 409 Conflict
    unauth_resp = client.post(
        "/api/v1/client/cascade/run-weekly",
        json={"sprint_id": f"sprint_{cycle.cycle_id}_w99"},
        headers=headers,
    )
    assert unauth_resp.status_code == 409

    # 4. Advance to 336 hours (T_0 + 2 * 168h): Week 2 boundary arrives
    time_ctrl.advance(hours=168)

    st_eligible = client.get("/api/v1/client/cascade/status", headers=headers).json()
    assert st_eligible["can_start_next_sprint"] is True
    assert st_eligible["next_sprint_id"] == w2_id

    # 5. Client launches Week 2 using the server-authorized next_sprint_id
    w2_run = client.post(
        "/api/v1/client/cascade/run-weekly",
        json={"sprint_id": w2_id},
        headers=headers,
    )
    assert w2_run.status_code == 200, w2_run.text
    assert w2_run.json()["sprint_id"] == w2_id

    # Status confirms Week 2 is now completed
    st_after_w2 = client.get("/api/v1/client/cascade/status", headers=headers).json()
    assert st_after_w2["is_weekly_consilium_completed"] is True
    assert st_after_w2["weekly_sprint_number"] == 2
    assert st_after_w2["current_weekly_sprint_id"] == w2_id


# ==============================================================================
# ADDITIONAL SCHEDULING INVARIANTS (BOUNDARY, LATE RUN, REPLAY, CONCURRENCY)
# ==============================================================================

@pytest.mark.anyio
async def test_scheduling_168h_boundary_and_early_request_rejection(sched_test_env):
    """
    Tests exact 168-hour boundary:
    - At T_0 + 167h 59m: can_start_next_sprint is False, POST returns 409 Conflict.
    - At T_0 + 168h 00m: can_start_next_sprint is True, POST succeeds (200 OK).
    """
    client, session, _, mock_gw, time_ctrl = sched_test_env
    patient_id = "pat_boundary"
    token = _pair_and_get_token(client, session, patient_id, "dev_boundary")
    headers = {"Authorization": f"Bearer {token}"}

    cycle, _ = MedicalCycleRepository(session).get_or_create_active_for_patient(patient_id)
    await _seed_primary(session, cycle.cycle_id, mock_gw, time_ctrl.now)

    weekly_service = WeeklyConsiliumService(db=session, gateway=mock_gw, now_provider=time_ctrl.now)
    weekly_service.record_schedule_anchor(
        cycle_id=cycle.cycle_id,
        anchor_dt=time_ctrl.now(),
        source="first_daily_question",
    )

    # 1 minute before boundary
    time_ctrl.advance(hours=167, minutes=59)
    st = client.get("/api/v1/client/cascade/status", headers=headers).json()
    assert st["can_start_next_sprint"] is False

    early_resp = client.post("/api/v1/client/cascade/run-weekly", headers=headers)
    assert early_resp.status_code == 409

    # Advance 1 minute to reach 168h 00m
    time_ctrl.advance(minutes=1)
    st_ready = client.get("/api/v1/client/cascade/status", headers=headers).json()
    assert st_ready["can_start_next_sprint"] is True

    ready_resp = client.post("/api/v1/client/cascade/run-weekly", headers=headers)
    assert ready_resp.status_code == 200


@pytest.mark.anyio
async def test_scheduling_late_run_does_not_shift_grid(sched_test_env):
    """
    Executing Week 1 late on Day 9 (T_0 + 216 hours) does NOT shift the Week 2 boundary (Day 14, T_0 + 336h).
    """
    client, session, _, mock_gw, time_ctrl = sched_test_env
    patient_id = "pat_late"
    token = _pair_and_get_token(client, session, patient_id, "dev_late")
    headers = {"Authorization": f"Bearer {token}"}

    cycle, _ = MedicalCycleRepository(session).get_or_create_active_for_patient(patient_id)
    await _seed_primary(session, cycle.cycle_id, mock_gw, time_ctrl.now)

    weekly_service = WeeklyConsiliumService(db=session, gateway=mock_gw, now_provider=time_ctrl.now)
    weekly_service.record_schedule_anchor(cycle.cycle_id, anchor_dt=time_ctrl.now())

    # Advance to Day 9 (216 hours) -> late run of Week 1
    time_ctrl.advance(hours=216)
    r_w1 = client.post("/api/v1/client/cascade/run-weekly", headers=headers)
    assert r_w1.status_code == 200

    # Advance to Day 13 (T_0 + 312 hours) -> still not eligible
    time_ctrl.advance(hours=96)
    st_day13 = client.get("/api/v1/client/cascade/status", headers=headers).json()
    assert st_day13["can_start_next_sprint"] is False

    # Advance to Day 14 (T_0 + 336 hours) -> Week 2 now eligible
    time_ctrl.advance(hours=24)
    st_day14 = client.get("/api/v1/client/cascade/status", headers=headers).json()
    assert st_day14["can_start_next_sprint"] is True
    assert st_day14["next_sprint_id"] == f"sprint_{cycle.cycle_id}_w2"


@pytest.mark.anyio
async def test_scheduling_absence_of_daily_answers_does_not_block_weekly(sched_test_env):
    """
    Athena daily pulse questions were asked, but patient gave zero answers:
    Consilium assembles successfully upon 168 hours.
    """
    client, session, _, mock_gw, time_ctrl = sched_test_env
    patient_id = "pat_no_answers"
    token = _pair_and_get_token(client, session, patient_id, "dev_no_answers")
    headers = {"Authorization": f"Bearer {token}"}

    cycle, _ = MedicalCycleRepository(session).get_or_create_active_for_patient(patient_id)
    await _seed_primary(session, cycle.cycle_id, mock_gw, time_ctrl.now)

    weekly_service = WeeklyConsiliumService(db=session, gateway=mock_gw, now_provider=time_ctrl.now)
    weekly_service.record_schedule_anchor(cycle.cycle_id, anchor_dt=time_ctrl.now())

    time_ctrl.advance(hours=168)
    resp = client.post("/api/v1/client/cascade/run-weekly", headers=headers)
    assert resp.status_code == 200
    assert len(resp.json()["system_reports"]) == 8


@pytest.mark.anyio
async def test_scheduling_patient_and_cycle_switch_isolation(sched_test_env):
    """
    Tests complete schedule grid isolation across different patients and cycles.
    """
    client, session, _, mock_gw, time_ctrl = sched_test_env

    # Patient A
    tok_a = _pair_and_get_token(client, session, "pat_iso_A", "dev_iso_A")
    hdr_a = {"Authorization": f"Bearer {tok_a}"}
    cycle_a, _ = MedicalCycleRepository(session).get_or_create_active_for_patient("pat_iso_A")
    await _seed_primary(session, cycle_a.cycle_id, mock_gw, time_ctrl.now)
    ws_a = WeeklyConsiliumService(db=session, gateway=mock_gw, now_provider=time_ctrl.now)
    ws_a.record_schedule_anchor(cycle_a.cycle_id, anchor_dt=time_ctrl.now())

    time_ctrl.advance(hours=168)
    r_a = client.post("/api/v1/client/cascade/run-weekly", headers=hdr_a)
    assert r_a.status_code == 200
    w1_a_id = r_a.json()["sprint_id"]

    # Patient B
    tok_b = _pair_and_get_token(client, session, "pat_iso_B", "dev_iso_B")
    hdr_b = {"Authorization": f"Bearer {tok_b}"}
    cycle_b, _ = MedicalCycleRepository(session).get_or_create_active_for_patient("pat_iso_B")
    await _seed_primary(session, cycle_b.cycle_id, mock_gw, time_ctrl.now)

    st_b = client.get("/api/v1/client/cascade/status", headers=hdr_b).json()
    assert st_b["can_start_next_sprint"] is False

    cross_resp = client.post(
        "/api/v1/client/cascade/run-weekly",
        json={"sprint_id": w1_a_id},
        headers=hdr_b,
    )
    assert cross_resp.status_code == 409


@pytest.mark.anyio
async def test_scheduling_concurrent_requests_mutual_exclusion(sched_test_env):
    """
    Concurrent requests for the same weekly sprint are serialized or rejected via lock.
    """
    client, session, _, mock_gw, time_ctrl = sched_test_env
    patient_id = "pat_concurrent"
    token = _pair_and_get_token(client, session, patient_id, "dev_concurrent")
    headers = {"Authorization": f"Bearer {token}"}

    cycle, _ = MedicalCycleRepository(session).get_or_create_active_for_patient(patient_id)
    await _seed_primary(session, cycle.cycle_id, mock_gw, time_ctrl.now)
    ws = WeeklyConsiliumService(db=session, gateway=mock_gw, now_provider=time_ctrl.now)
    ws.record_schedule_anchor(cycle.cycle_id, anchor_dt=time_ctrl.now())

    time_ctrl.advance(hours=168)

    def _call():
        return client.post("/api/v1/client/cascade/run-weekly", headers=headers)

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        f1 = executor.submit(_call)
        f2 = executor.submit(_call)
        res1 = f1.result()
        res2 = f2.result()

    statuses = [res1.status_code, res2.status_code]
    assert 200 in statuses
    for s in statuses:
        assert s in (200, 409)
