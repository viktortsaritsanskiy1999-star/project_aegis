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
from app.security.tokens import create_access_token, verify_access_token
from app.services.gemini_gateway import GeminiGateway
from app.services.primary_consilium_service import PrimaryConsiliumService
from app.services.weekly_consilium_service import (
    WeeklyConsiliumService,
    ATHENA_SCHEDULE_ANCHOR_REPORT_TYPE,
)
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


TEST_JWT_SECRET = "test_jwt_bug06_secret_1234567890!"
TEST_PAIRING_SECRET = "test_pairing_secret_bug06_12345!"
TEST_API_SECRET = "test_device_secret_bug06_12345"


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

        if "pro" in url_path.lower() or "argon" in url_path.lower():
            return httpx.Response(200, json=_gemini_envelope(_sample_weekly_pro_output()))
        if "service pulse:" in prompt_text.lower() or "first introduction" in prompt_text.lower() or "just checking in" in prompt_text.lower():
            return httpx.Response(200, json=_gemini_envelope(
                "Здравствуйте! Как ваше самочувствие?\n\n"
                "[CLINICAL_REPORT]\n"
                '{\n'
                '  "status": "NORMAL",\n'
                '  "symptoms_mentioned": [],\n'
                '  "subjective_wellbeing_score": 8,\n'
                '  "adherence_to_plan": "HIGH",\n'
                '  "red_flags": false,\n'
                '  "summary_for_master": "Athena daily pulse check-in."\n'
                '}\n'
                '[/CLINICAL_REPORT]'
            ))
        return httpx.Response(200, json=_gemini_envelope(_sample_weekly_flash_output()))

    return GeminiGateway(
        api_key="test-bug06-gateway-key",
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
def bug06_test_env(monkeypatch, tmp_path) -> Generator[Any, None, None]:
    monkeypatch.setattr(settings, "api_secret", TEST_API_SECRET)
    monkeypatch.setattr(settings, "pairing_secret", TEST_PAIRING_SECRET)
    monkeypatch.setattr(settings, "jwt_secret", TEST_JWT_SECRET)
    monkeypatch.setattr(settings, "jwt_issuer", "aegis-backend")
    monkeypatch.setattr(settings, "jwt_audience", "aegis-mobile-client")
    monkeypatch.setattr(settings, "gemini_api_key", "test-gemini-key")

    db_file = tmp_path / "test_bug06_boundary.db"
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
# TARGETED TESTS FOR ARGON BUG-06: WEEKLY SPRINT BOUNDARY ENFORCEMENT
# ==============================================================================

@pytest.mark.anyio
async def test_precreated_active_week_1_before_168h_cannot_run(bug06_test_env):
    """
    ARGON BUG-06 Requirement:
    Pre-created active Week 1 record in DB must NEVER bypass the fixed 168-hour scheduling boundary.
    - Status reports can_start_next_sprint=False, next_sprint_id=None.
    - POST /run-weekly with pre-created sprint_id returns 409 Conflict.
    - POST /run-weekly without sprint_id returns 409 Conflict.
    - Zero AI calls made.
    """
    client, session, recorded_calls, mock_gw, time_ctrl = bug06_test_env
    patient_id = "pat_bug06_w1"
    token = _pair_and_get_token(client, session, patient_id, "dev_bug06_w1")
    headers = {"Authorization": f"Bearer {token}"}

    cycle, _ = MedicalCycleRepository(session).get_or_create_active_for_patient(patient_id)
    await _seed_primary(session, cycle.cycle_id, mock_gw, time_ctrl.now)

    # Pre-create active sprint record in DB for Week 1 before chat turns
    sprint_repo = WeeklySprintRepository(session)
    precreated_w1_id = f"sprint_precreated_{cycle.cycle_id}_w1"
    sprint_repo.create(
        sprint_id=precreated_w1_id,
        cycle_id=cycle.cycle_id,
        sprint_number=1,
        status="active",
        started_at=time_ctrl.now(),
    )

    # Establish schedule anchor via daily pulse
    client.post(
        "/api/v1/client/chat/athena",
        json={"event_type": "daily_pulse", "message": "Pulse 1", "sprint_day": 1},
        headers=headers,
    )

    ai_calls_before = len(recorded_calls)

    # Advance only 24 hours (24h < 168h)
    time_ctrl.advance(hours=24)

    # 1. GET /cascade/status must report can_start_next_sprint=False, next_sprint_id=None
    st = client.get("/api/v1/client/cascade/status", headers=headers).json()
    assert st["can_start_next_sprint"] is False
    assert st["next_sprint_id"] is None
    assert st["current_weekly_sprint_id"] == precreated_w1_id
    assert st["is_weekly_consilium_completed"] is False

    # 2. POST /run-weekly with precreated sprint_id must be rejected with 409
    resp_with_id = client.post(
        "/api/v1/client/cascade/run-weekly",
        json={"sprint_id": precreated_w1_id},
        headers=headers,
    )
    assert resp_with_id.status_code == 409
    assert "scheduling boundary has not elapsed" in resp_with_id.text.lower()

    # 3. POST /run-weekly without sprint_id must also be rejected with 409
    resp_no_id = client.post(
        "/api/v1/client/cascade/run-weekly",
        headers=headers,
    )
    assert resp_no_id.status_code == 409
    assert "not eligible to start" in resp_no_id.text.lower()

    # Verify zero AI calls occurred
    assert len(recorded_calls) == ai_calls_before


@pytest.mark.anyio
async def test_precreated_active_week_2_before_336h_cannot_run(bug06_test_env):
    """
    ARGON BUG-06 Requirement:
    Pre-created active Week 2 record in DB must NEVER bypass the fixed 336-hour scheduling boundary.
    - Status reports can_start_next_sprint=False, next_sprint_id=None, is_weekly_consilium_completed=True.
    - POST /run-weekly with pre-created Week 2 sprint_id returns 409 Conflict.
    - Zero AI calls made.
    """
    client, session, recorded_calls, mock_gw, time_ctrl = bug06_test_env
    patient_id = "pat_bug06_w2_early"
    token = _pair_and_get_token(client, session, patient_id, "dev_bug06_w2_early")
    headers = {"Authorization": f"Bearer {token}"}

    cycle, _ = MedicalCycleRepository(session).get_or_create_active_for_patient(patient_id)
    await _seed_primary(session, cycle.cycle_id, mock_gw, time_ctrl.now)

    # Establish schedule anchor T0
    client.post(
        "/api/v1/client/chat/athena",
        json={"event_type": "daily_pulse", "message": "Pulse 1", "sprint_day": 1},
        headers=headers,
    )

    # Advance 168 hours and complete Week 1
    time_ctrl.advance(hours=168)
    w1_run = client.post("/api/v1/client/cascade/run-weekly", headers=headers)
    assert w1_run.status_code == 200
    w1_id = w1_run.json()["sprint_id"]

    # Pre-create an active sprint record in DB for Week 2
    sprint_repo = WeeklySprintRepository(session)
    precreated_w2_id = f"sprint_precreated_{cycle.cycle_id}_w2"
    sprint_repo.create(
        sprint_id=precreated_w2_id,
        cycle_id=cycle.cycle_id,
        sprint_number=2,
        status="active",
        started_at=time_ctrl.now(),
    )

    ai_calls_after_w1 = len(recorded_calls)

    # Advance to 200 hours from T0 (200h < 336h boundary)
    time_ctrl.advance(hours=32)

    # 1. GET /cascade/status must report active Week 2 sprint, but NOT eligible before 336h
    st = client.get("/api/v1/client/cascade/status", headers=headers).json()
    assert st["current_weekly_sprint_id"] == precreated_w2_id
    assert st["weekly_sprint_number"] == 2
    assert st["is_weekly_consilium_completed"] is False
    assert st["can_start_next_sprint"] is False
    assert st["next_sprint_id"] is None

    # 2. POST /run-weekly with precreated Week 2 sprint_id must return 409
    resp_w2 = client.post(
        "/api/v1/client/cascade/run-weekly",
        json={"sprint_id": precreated_w2_id},
        headers=headers,
    )
    assert resp_w2.status_code == 409
    assert "scheduling boundary has not elapsed" in resp_w2.text.lower()

    # Zero AI calls made during rejection
    assert len(recorded_calls) == ai_calls_after_w1


@pytest.mark.anyio
async def test_active_week_2_becomes_eligible_at_336h(bug06_test_env):
    """
    ARGON BUG-06 Requirement:
    Active Week 2 must become eligible once 336 hours have elapsed from schedule anchor T0:
    - Status reports can_start_next_sprint=True, next_sprint_id=precreated_w2_id.
    - Never permanently blocked solely because an active record already exists.
    - POST /run-weekly with precreated_w2_id succeeds with 200 OK.
    """
    client, session, recorded_calls, mock_gw, time_ctrl = bug06_test_env
    patient_id = "pat_bug06_w2_eligible"
    token = _pair_and_get_token(client, session, patient_id, "dev_bug06_w2_eligible")
    headers = {"Authorization": f"Bearer {token}"}

    cycle, _ = MedicalCycleRepository(session).get_or_create_active_for_patient(patient_id)
    await _seed_primary(session, cycle.cycle_id, mock_gw, time_ctrl.now)

    client.post(
        "/api/v1/client/chat/athena",
        json={"event_type": "daily_pulse", "message": "Pulse 1", "sprint_day": 1},
        headers=headers,
    )

    # Advance 168h -> run Week 1
    time_ctrl.advance(hours=168)
    w1_run = client.post("/api/v1/client/cascade/run-weekly", headers=headers)
    assert w1_run.status_code == 200

    # Pre-create active sprint record for Week 2
    sprint_repo = WeeklySprintRepository(session)
    precreated_w2_id = f"sprint_precreated_{cycle.cycle_id}_w2"
    sprint_repo.create(
        sprint_id=precreated_w2_id,
        cycle_id=cycle.cycle_id,
        sprint_number=2,
        status="active",
        started_at=time_ctrl.now(),
    )

    # Advance to exactly 336 hours from T0 (168 + 168)
    time_ctrl.advance(hours=168)

    # Status must report eligible with exact pre-created sprint id
    st = client.get("/api/v1/client/cascade/status", headers=headers).json()
    assert st["can_start_next_sprint"] is True
    assert st["next_sprint_id"] == precreated_w2_id

    # Run Week 2 with pre-created sprint_id -> succeeds
    w2_run = client.post(
        "/api/v1/client/cascade/run-weekly",
        json={"sprint_id": precreated_w2_id},
        headers=headers,
    )
    assert w2_run.status_code == 200, w2_run.text
    assert w2_run.json()["sprint_id"] == precreated_w2_id
    assert w2_run.json()["sprint_number"] == 2


@pytest.mark.anyio
async def test_no_sprint_id_route_cannot_bypass_schedule(bug06_test_env):
    """
    ARGON BUG-06 Requirement:
    Omitting sprint_id in POST /run-weekly must never bypass the scheduling boundary:
    - Before 168h: returns 409 Conflict (even if active record exists).
    - At 168h: runs Week 1.
    - Between 168h and 336h: replays Week 1 (idempotent replay, zero AI calls), never runs Week 2 prematurely.
    """
    client, session, recorded_calls, mock_gw, time_ctrl = bug06_test_env
    patient_id = "pat_bug06_no_id"
    token = _pair_and_get_token(client, session, patient_id, "dev_bug06_no_id")
    headers = {"Authorization": f"Bearer {token}"}

    cycle, _ = MedicalCycleRepository(session).get_or_create_active_for_patient(patient_id)
    await _seed_primary(session, cycle.cycle_id, mock_gw, time_ctrl.now)

    client.post(
        "/api/v1/client/chat/athena",
        json={"event_type": "daily_pulse", "message": "Pulse 1", "sprint_day": 1},
        headers=headers,
    )

    # Pre-create active record for Week 1
    sprint_repo = WeeklySprintRepository(session)
    sprint_repo.create(
        sprint_id=f"sprint_{cycle.cycle_id}_w1",
        cycle_id=cycle.cycle_id,
        sprint_number=1,
        status="active",
        started_at=time_ctrl.now(),
    )

    # Advance only 50 hours (< 168h)
    time_ctrl.advance(hours=50)

    # Calling without sprint_id must fail closed with 409
    r_early = client.post("/api/v1/client/cascade/run-weekly", headers=headers)
    assert r_early.status_code == 409
    assert "not eligible to start" in r_early.text.lower()

    # Advance to 168h -> calling without sprint_id runs Week 1
    time_ctrl.advance(hours=118)
    r_w1 = client.post("/api/v1/client/cascade/run-weekly", headers=headers)
    assert r_w1.status_code == 200
    assert r_w1.json()["sprint_id"] == f"sprint_{cycle.cycle_id}_w1"

    ai_calls_w1 = len(recorded_calls)

    # Pre-create active record for Week 2
    sprint_repo.create(
        sprint_id=f"sprint_{cycle.cycle_id}_w2",
        cycle_id=cycle.cycle_id,
        sprint_number=2,
        status="active",
        started_at=time_ctrl.now(),
    )

    # Advance to 200 hours from T0 (< 336h)
    time_ctrl.advance(hours=32)

    # Calling without sprint_id before 336h must fail closed with 409 (cannot run Week 2 early)
    r_blocked = client.post("/api/v1/client/cascade/run-weekly", headers=headers)
    assert r_blocked.status_code == 409
    assert "not eligible to start" in r_blocked.text.lower()
    assert len(recorded_calls) == ai_calls_w1  # Zero additional AI calls!

    # Advance to 336 hours from T0 -> calling without sprint_id runs Week 2
    time_ctrl.advance(hours=136)
    r_w2 = client.post("/api/v1/client/cascade/run-weekly", headers=headers)
    assert r_w2.status_code == 200
    assert r_w2.json()["sprint_id"] == f"sprint_{cycle.cycle_id}_w2"


@pytest.mark.anyio
async def test_completed_period_replay_still_works(bug06_test_env):
    """
    ARGON BUG-06 Requirement:
    Idempotent replay of previously completed periods must continue to work without Gemini calls:
    - Explicit replay by sprint_id returns 200 with X-Aegis-Idempotent-Replay: true.
    - Replay with no sprint_id returns 200 with X-Aegis-Idempotent-Replay: true.
    """
    client, session, recorded_calls, mock_gw, time_ctrl = bug06_test_env
    patient_id = "pat_bug06_replay"
    token = _pair_and_get_token(client, session, patient_id, "dev_bug06_replay")
    headers = {"Authorization": f"Bearer {token}"}

    cycle, _ = MedicalCycleRepository(session).get_or_create_active_for_patient(patient_id)
    await _seed_primary(session, cycle.cycle_id, mock_gw, time_ctrl.now)

    client.post(
        "/api/v1/client/chat/athena",
        json={"event_type": "daily_pulse", "message": "Pulse 1", "sprint_day": 1},
        headers=headers,
    )

    time_ctrl.advance(hours=168)
    w1_run = client.post("/api/v1/client/cascade/run-weekly", headers=headers)
    assert w1_run.status_code == 200
    w1_id = w1_run.json()["sprint_id"]

    calls_after_w1 = len(recorded_calls)

    # Replay by explicit sprint_id
    r_explicit = client.post(
        "/api/v1/client/cascade/run-weekly",
        json={"sprint_id": w1_id},
        headers=headers,
    )
    assert r_explicit.status_code == 200
    assert r_explicit.headers.get("X-Aegis-Idempotent-Replay") == "true"
    assert len(recorded_calls) == calls_after_w1

    # Replay without sprint_id
    r_implicit = client.post(
        "/api/v1/client/cascade/run-weekly",
        headers=headers,
    )
    assert r_implicit.status_code == 200
    assert r_implicit.headers.get("X-Aegis-Idempotent-Replay") == "true"
    assert len(recorded_calls) == calls_after_w1


@pytest.mark.anyio
async def test_concurrent_requests_cannot_bypass_boundary(bug06_test_env):
    """
    ARGON BUG-06 Requirement:
    Concurrent requests before the boundary must all be rejected with 409 Conflict.
    No race condition can slip past the 168-hour / 336-hour schedule check.
    """
    client, session, recorded_calls, mock_gw, time_ctrl = bug06_test_env
    patient_id = "pat_bug06_concurrency"
    token = _pair_and_get_token(client, session, patient_id, "dev_bug06_concurrency")
    headers = {"Authorization": f"Bearer {token}"}

    cycle, _ = MedicalCycleRepository(session).get_or_create_active_for_patient(patient_id)
    await _seed_primary(session, cycle.cycle_id, mock_gw, time_ctrl.now)

    client.post(
        "/api/v1/client/chat/athena",
        json={"event_type": "daily_pulse", "message": "Pulse 1", "sprint_day": 1},
        headers=headers,
    )

    # Pre-create active sprint
    sprint_repo = WeeklySprintRepository(session)
    active_id = f"sprint_active_{cycle.cycle_id}_w1"
    sprint_repo.create(
        sprint_id=active_id,
        cycle_id=cycle.cycle_id,
        sprint_number=1,
        status="active",
        started_at=time_ctrl.now(),
    )

    # Time is only 48 hours (< 168h)
    time_ctrl.advance(hours=48)

    calls_before = len(recorded_calls)

    # Fire concurrent requests before boundary
    def send_req(payload):
        return client.post(
            "/api/v1/client/cascade/run-weekly",
            json=payload,
            headers=headers,
        )

    payloads = [
        {"sprint_id": active_id},
        {},
        {"sprint_id": f"sprint_{cycle.cycle_id}_w1"},
        {"sprint_id": active_id},
    ]

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        futures = [executor.submit(send_req, p) for p in payloads]
        results = [f.result() for f in futures]

    for r in results:
        assert r.status_code == 409

    # Zero AI calls made
    assert len(recorded_calls) == calls_before
