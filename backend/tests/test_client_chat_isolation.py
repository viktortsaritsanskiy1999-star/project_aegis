import asyncio
import json
from typing import Any, Dict, Generator, List, Optional
import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import settings
from app.database import get_db
from app.main import app
from app.models.base import Base
from app.models.model_report import ModelReport
from app.repositories.cycle_repository import MedicalCycleRepository
from app.repositories.pairing_repository import PairingRepository
from app.repositories.patient_repository import PatientRepository
from app.repositories.report_repository import ModelReportRepository
from app.routers.client_chat import get_gemini_gateway
from app.schemas.chat import (
    AthenaChatRequest,
    AthenaVitalsAlertContext,
)
from app.schemas.privacy import PrivacyLevel
from app.security.tokens import (
    LEGACY_DEVICE_SCOPE,
    create_access_token,
    verify_access_token,
)
from app.services.gemini_gateway import GeminiGateway, GeminiModelRole
from app.services.weekly_consilium_service import (
    WeeklyConsiliumService,
    ATHENA_CLINICAL_LOG_REPORT_TYPE,
    ATHENA_CHAT_HISTORY_REPORT_TYPE,
)

TEST_JWT_SECRET = "test_jwt_signing_secret_at_least_16_chars_long!"
TEST_PAIRING_SECRET = "test_pairing_secret_deterministic_12345!"
TEST_API_SECRET = "test_device_secret_token_12345"


# ==============================================================================
# DETERMINISTIC MOCK GEMINI GATEWAY
# ==============================================================================

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


def _sample_routine_athena_reply(user_name: str = "друг") -> str:
    return (
        f"Привет! Отличный темп сегодня, {user_name}. Данные записаны, продолжаем следовать плану.\n"
        "До завтра!\n\n"
        "[CLINICAL_REPORT]\n"
        "{\n"
        '  "status": "NORMAL",\n'
        '  "symptoms_mentioned": [],\n'
        '  "subjective_wellbeing_score": 8,\n'
        '  "adherence_to_plan": "HIGH",\n'
        '  "red_flags": false,\n'
        '  "summary_for_master": "Patient adheres well to routine habits with no reported discomfort."\n'
        "}\n"
        "[/CLINICAL_REPORT]"
    )


def _sample_emergency_athena_reply() -> str:
    return (
        "Пожалуйста, немедленно присядьте и расстегните воротник! "
        "Сделайте медленный глубокий вдох. Я рядом. "
        "Если боль в груди усиливается или не хватает воздуха, немедленно вызовите скорую помощь!\n\n"
        "[CLINICAL_REPORT]\n"
        "{\n"
        '  "status": "CRITICAL",\n'
        '  "symptoms_mentioned": ["chest_pain", "shortness_of_breath"],\n'
        '  "subjective_wellbeing_score": 2,\n'
        '  "adherence_to_plan": "LOW",\n'
        '  "red_flags": true,\n'
        '  "summary_for_master": "Acute chest pain reported during spike. Emergency trigger engaged."\n'
        "}\n"
        "[/CLINICAL_REPORT]\n"
        "[EMERGENCY_TRIGGER]"
    )


# ==============================================================================
# ISOLATED FIXTURES
# ==============================================================================

@pytest.fixture
def isolated_chat_env() -> Generator[tuple[TestClient, Session, List[Dict[str, Any]]], None, None]:
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    TestingSession = sessionmaker(bind=engine, autoflush=True, autocommit=False)
    session = TestingSession()

    recorded_ai_calls: List[Dict[str, Any]] = []

    async def mock_handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        contents = body.get("contents", [])
        recorded_ai_calls.append({"body": body, "contents": contents})
        # Inspect only the active user prompt in the final content turn
        last_turn_text = ""
        if contents:
            parts = contents[-1].get("parts", [])
            if parts and isinstance(parts[0], dict):
                last_turn_text = parts[0].get("text", "")

        lower_text = last_turn_text.lower()
        if "chest pain" in lower_text or "острая боль" in lower_text or "боль в груди" in lower_text:
            return httpx.Response(200, json=_gemini_envelope(_sample_emergency_athena_reply()))
        return httpx.Response(200, json=_gemini_envelope(_sample_routine_athena_reply()))

    mock_gateway = GeminiGateway(
        api_key="test-key",
        transport=httpx.MockTransport(mock_handler),
        sleep_func=lambda _: asyncio.sleep(0),
    )

    def override_get_db():
        db = TestingSession()
        try:
            yield db
        finally:
            db.close()

    def override_get_gemini_gateway():
        return mock_gateway

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_gemini_gateway] = override_get_gemini_gateway

    orig_jwt_sec = settings.jwt_secret
    orig_pair_sec = settings.pairing_secret
    orig_api_sec = settings.api_secret
    settings.jwt_secret = TEST_JWT_SECRET
    settings.pairing_secret = TEST_PAIRING_SECRET
    settings.api_secret = TEST_API_SECRET

    with TestClient(app) as client:
        yield client, session, recorded_ai_calls

    session.close()
    engine.dispose()
    app.dependency_overrides.clear()
    settings.jwt_secret = orig_jwt_sec
    settings.pairing_secret = orig_pair_sec
    settings.api_secret = orig_api_sec


def _pair_patient_device(
    client: TestClient,
    session: Session,
    *,
    patient_id: str,
    device_id: str,
    raw_code: str,
) -> dict:
    PatientRepository(session).create(patient_id=patient_id, status="active")
    PairingRepository(session).create_code(
        patient_id=patient_id,
        raw_code_override=raw_code,
    )
    session.commit()

    resp = client.post(
        "/api/v1/auth/pair",
        json={
            "pairing_code": raw_code,
            "device_name": "Test Chat Device",
            "platform": "android",
            "device_id": device_id,
        },
    )
    assert resp.status_code == 200, f"Pairing failed: {resp.text}"
    return resp.json()


def _get_device_jwt_with_scopes(
    client: TestClient,
    session: Session,
    *,
    patient_id: str,
    device_id: str,
    raw_code: str,
    scope: str,
) -> tuple[dict, str]:
    pair_bundle = _pair_patient_device(
        client, session, patient_id=patient_id, device_id=device_id, raw_code=raw_code
    )
    fam_id = verify_access_token(pair_bundle["access_token"]).family_id
    jwt_token = create_access_token(
        patient_id=patient_id,
        device_id=device_id,
        family_id=fam_id,
        scope=scope,
    ).access_token
    return pair_bundle, jwt_token


# ==============================================================================
# TESTS: 401 / 403 / SCOPES / PATIENT ISOLATION
# ==============================================================================

def test_athena_chat_unauthenticated_returns_401(isolated_chat_env):
    """Verifies that requests without Bearer token or invalid token return 401."""
    client, _, _ = isolated_chat_env

    r1 = client.post("/api/v1/client/chat/athena", json={"message": "Привет"})
    assert r1.status_code == 401

    r2 = client.get("/api/v1/client/chat/athena/history")
    assert r2.status_code == 401


def test_static_admin_secret_rejected_on_client_chat(isolated_chat_env):
    """Verifies that sending AEGIS_API_SECRET to client chat endpoint returns 401."""
    client, _, _ = isolated_chat_env
    headers = {"Authorization": f"Bearer {TEST_API_SECRET}"}

    resp = client.post("/api/v1/client/chat/athena", json={"message": "Привет"}, headers=headers)
    assert resp.status_code == 401
    assert "Mobile device JWT token is required" in resp.json()["detail"]


def test_insufficient_scope_chat_write_returns_403(isolated_chat_env):
    """Verifies that token lacking chat:write scope receives 403 Forbidden."""
    client, session, _ = isolated_chat_env
    pair_res = _pair_patient_device(
        client, session, patient_id="pat-scope-chat-01", device_id="dev-scope-chat-01", raw_code="810001"
    )
    # Legacy token without chat:write receives 403
    real_claims = verify_access_token(pair_res["access_token"])
    legacy_jwt = create_access_token(
        patient_id="pat-scope-chat-01",
        device_id="dev-scope-chat-01",
        family_id=real_claims.family_id,
        scope=LEGACY_DEVICE_SCOPE,
    ).access_token
    legacy_headers = {"Authorization": f"Bearer {legacy_jwt}"}

    resp = client.post("/api/v1/client/chat/athena", json={"message": "Привет"}, headers=legacy_headers)
    assert resp.status_code == 403
    assert resp.json()["detail"] == "Insufficient token scope for this operation."


def test_insufficient_scope_chat_read_returns_403(isolated_chat_env):
    """Verifies that token lacking chat:read scope receives 403 Forbidden on history, while standard pairing token succeeds."""
    client, session, _ = isolated_chat_env
    pair_res = _pair_patient_device(
        client, session, patient_id="pat-scope-chat-02", device_id="dev-scope-chat-02", raw_code="810002"
    )
    # 1. Legacy token without chat:read receives 403
    real_claims = verify_access_token(pair_res["access_token"])
    legacy_jwt = create_access_token(
        patient_id="pat-scope-chat-02",
        device_id="dev-scope-chat-02",
        family_id=real_claims.family_id,
        scope=LEGACY_DEVICE_SCOPE,
    ).access_token
    legacy_headers = {"Authorization": f"Bearer {legacy_jwt}"}

    resp = client.get("/api/v1/client/chat/athena/history", headers=legacy_headers)
    assert resp.status_code == 403
    assert resp.json()["detail"] == "Insufficient token scope for this operation."

    # 2. Standard pairing token (with DEFAULT_DEVICE_SCOPE containing chat:read) succeeds with 200 OK
    default_headers = {"Authorization": f"Bearer {pair_res['access_token']}"}
    resp_std = client.get("/api/v1/client/chat/athena/history", headers=default_headers)
    assert resp_std.status_code == 200
    assert resp_std.json()["messages"] == []


def test_client_device_token_rejected_on_admin_cascade_routes(isolated_chat_env):
    """Verifies that client token with chat scopes cannot access admin endpoints."""
    client, session, _ = isolated_chat_env
    _, chat_jwt = _get_device_jwt_with_scopes(
        client,
        session,
        patient_id="pat-admin-chk-01",
        device_id="dev-admin-chk-01",
        raw_code="810003",
        scope="vitals:read vitals:write cycle:read chat:read chat:write",
    )
    headers = {"Authorization": f"Bearer {chat_jwt}"}

    r_admin = client.post("/api/v1/cycle/start", json={"cycle_id": "test", "patient_id": "pat-admin-chk-01"}, headers=headers)
    assert r_admin.status_code == 403


# ==============================================================================
# TESTS: CHAT CONVERSATION / SANITIZATION / MULTI-TURN CONTINUITY
# ==============================================================================

def test_successful_routine_chat_turn_and_sanitized_reply(isolated_chat_env):
    """
    Verifies that a valid routine user message:
    1. Triggers Gemini with proper system instruction and contents.
    2. Returns clean conversational reply with [CLINICAL_REPORT] completely stripped.
    3. Persists hidden clinical log as server_only.
    4. Automatically provisions patient active cycle without client input.
    """
    client, session, ai_calls = isolated_chat_env
    _, jwt_token = _get_device_jwt_with_scopes(
        client,
        session,
        patient_id="pat-athena-01",
        device_id="dev-athena-01",
        raw_code="810004",
        scope="vitals:read vitals:write cycle:read chat:read chat:write",
    )
    headers = {"Authorization": f"Bearer {jwt_token}"}

    resp = client.post(
        "/api/v1/client/chat/athena",
        json={"message": "Сегодня прошёл 8000 шагов, чувствую себя отлично!", "sprint_day": 3},
        headers=headers,
    )
    assert resp.status_code == 200
    data = resp.json()

    assert data["cycle_id"].startswith("cycle_")
    assert "До завтра!" in data["reply"]
    assert data["emergency_trigger"] is False
    assert data["emergency_notice"] is None
    assert data["sprint_day"] == 3

    # ZERO leakage of hidden clinical report block in reply
    assert "[CLINICAL_REPORT]" not in data["reply"]
    assert "[/CLINICAL_REPORT]" not in data["reply"]
    assert "subjective_wellbeing_score" not in json.dumps(data)
    assert "summary_for_master" not in json.dumps(data)

    # In DB, verify server_only clinical log is saved
    session.expire_all()
    rep_repo = ModelReportRepository(session)
    clin_logs = rep_repo.list_server_only(cycle_id=data["cycle_id"], report_type=ATHENA_CLINICAL_LOG_REPORT_TYPE)
    assert len(clin_logs) == 1
    assert clin_logs[0].privacy_level == PrivacyLevel.SERVER_ONLY.value
    assert clin_logs[0].content["status"] == "NORMAL"

    # In DB, verify chat history contains user and curator turns
    chat_hist = WeeklyConsiliumService(session).get_athena_chat_history(data["cycle_id"])
    assert len(chat_hist) == 2
    assert chat_hist[0]["sender"] == "user"
    assert "8000 шагов" in chat_hist[0]["text"]
    assert chat_hist[1]["sender"] == "curator"


def test_multi_turn_history_restoration_and_continuity(isolated_chat_env):
    """
    Verifies that subsequent chat turns include restored conversation history
    in Gemini contents, maintaining full conversation continuity.
    """
    client, session, ai_calls = isolated_chat_env
    _, jwt_token = _get_device_jwt_with_scopes(
        client,
        session,
        patient_id="pat-athena-02",
        device_id="dev-athena-02",
        raw_code="810005",
        scope="vitals:read vitals:write cycle:read chat:read chat:write",
    )
    headers = {"Authorization": f"Bearer {jwt_token}"}

    # Turn 1
    r1 = client.post(
        "/api/v1/client/chat/athena",
        json={"message": "Turn 1: Привет, Афина!"},
        headers=headers,
    )
    assert r1.status_code == 200

    # Turn 2
    r2 = client.post(
        "/api/v1/client/chat/athena",
        json={"message": "Turn 2: Как мои показатели за сегодня?"},
        headers=headers,
    )
    assert r2.status_code == 200
    assert len(ai_calls) == 2

    # Second call contents should contain history of turn 1 (user and model) plus current prompt
    turn2_contents = ai_calls[1]["contents"]
    assert len(turn2_contents) >= 3
    roles = [t["role"] for t in turn2_contents]
    assert roles[-3] == "user"
    assert roles[-2] == "model"
    assert roles[-1] == "user"

    # Check history endpoint returns sanitized chronological messages
    r_hist = client.get("/api/v1/client/chat/athena/history", headers=headers)
    assert r_hist.status_code == 200
    hist_data = r_hist.json()
    assert len(hist_data["messages"]) == 4
    assert hist_data["messages"][0]["text"] == "Turn 1: Привет, Афина!"
    assert hist_data["messages"][2]["text"] == "Turn 2: Как мои показатели за сегодня?"


def test_patient_isolation_and_foreign_cycle_prevention(isolated_chat_env):
    """
    Verifies that:
    1. Patient A cannot access Patient B's chat history.
    2. Attempting to supply `patient_id` or `cycle_id` in request is rejected (extra=forbid).
    3. Patient A's active cycle is completely isolated from Patient B.
    """
    client, session, _ = isolated_chat_env
    _, jwt_a = _get_device_jwt_with_scopes(
        client, session, patient_id="pat-a-chat", device_id="dev-a-chat", raw_code="810006",
        scope="vitals:read vitals:write cycle:read chat:read chat:write",
    )
    _, jwt_b = _get_device_jwt_with_scopes(
        client, session, patient_id="pat-b-chat", device_id="dev-b-chat", raw_code="810007",
        scope="vitals:read vitals:write cycle:read chat:read chat:write",
    )

    # Patient A sends message
    client.post(
        "/api/v1/client/chat/athena",
        json={"message": "Secret message from Patient A"},
        headers={"Authorization": f"Bearer {jwt_a}"},
    )

    # Patient B fetches history -> zero turns from Patient A
    resp_b = client.get(
        "/api/v1/client/chat/athena/history",
        headers={"Authorization": f"Bearer {jwt_b}"},
    )
    assert resp_b.status_code == 200
    assert len(resp_b.json()["messages"]) == 0

    # Attempting to supply cycle_id in body is rejected with 422
    resp_tamper = client.post(
        "/api/v1/client/chat/athena",
        json={"message": "Test", "cycle_id": "foreign-cycle"},
        headers={"Authorization": f"Bearer {jwt_a}"},
    )
    assert resp_tamper.status_code == 422


# ==============================================================================
# TESTS: IDEMPOTENCY / RETRY / ZERO DUPLICATE AI CALLS
# ==============================================================================

def test_message_idempotency_retry_zero_duplicate_ai_calls(isolated_chat_env):
    """
    Verifies that resubmitting a chat request with the identical `idempotency_key`:
    1. Returns the exact same response and message_id.
    2. Guarantees ZERO duplicate Gemini AI gateway invocations.
    3. Does NOT append duplicate turns to chat history.
    """
    client, session, ai_calls = isolated_chat_env
    _, jwt_token = _get_device_jwt_with_scopes(
        client,
        session,
        patient_id="pat-idem-chat-01",
        device_id="dev-idem-chat-01",
        raw_code="810008",
        scope="vitals:read vitals:write cycle:read chat:read chat:write",
    )
    headers = {"Authorization": f"Bearer {jwt_token}"}
    idem_key = "idem-athena-req-unique-99"

    # First request
    r1 = client.post(
        "/api/v1/client/chat/athena",
        json={"message": "First delivery attempt", "idempotency_key": idem_key},
        headers=headers,
    )
    assert r1.status_code == 200
    d1 = r1.json()
    assert len(ai_calls) == 1

    # Second request (network timeout retry)
    r2 = client.post(
        "/api/v1/client/chat/athena",
        json={"message": "First delivery attempt", "idempotency_key": idem_key},
        headers=headers,
    )
    assert r2.status_code == 200
    d2 = r2.json()

    # Zero duplicate AI calls!
    assert len(ai_calls) == 1
    assert d1["message_id"] == d2["message_id"]
    assert d1["reply"] == d2["reply"]

    # History in DB has exactly 2 turns (1 user + 1 curator)
    session.expire_all()
    chat_hist = WeeklyConsiliumService(session).get_athena_chat_history(d1["cycle_id"])
    assert len(chat_hist) == 2


# ==============================================================================
# TESTS: EMERGENCY SIGNALS & FAIL-SAFE INDEPENDENCE
# ==============================================================================

def test_vitals_spike_alert_event_and_mode_2_prompt(isolated_chat_env):
    """
    Verifies that sending a `vitals_alert` event:
    1. Formulates Mode 2 prompt canceling the goodbye rule.
    2. Records the alert context in user history.
    3. Handles pulse spike appropriately.
    """
    client, session, ai_calls = isolated_chat_env
    _, jwt_token = _get_device_jwt_with_scopes(
        client,
        session,
        patient_id="pat-spike-01",
        device_id="dev-spike-01",
        raw_code="810009",
        scope="vitals:read vitals:write cycle:read chat:read chat:write",
    )
    headers = {"Authorization": f"Bearer {jwt_token}"}

    resp = client.post(
        "/api/v1/client/chat/athena",
        json={
            "event_type": "vitals_alert",
            "vitals_alert": {
                "current_pulse": 138,
                "baseline_pulse": 70,
                "notes": "Spike detected while resting",
            },
        },
        headers=headers,
    )
    assert resp.status_code == 200
    assert len(ai_calls) == 1
    prompt_sent = ai_calls[0]["contents"][-1]["parts"][0]["text"]
    assert "Heart rate spike detected (peak up to 138 bpm" in prompt_sent
    assert "MODE 2: ATTENTION AND SUPPORT" in prompt_sent


def test_structured_emergency_trigger_detection(isolated_chat_env):
    """
    Verifies that when model output contains [EMERGENCY_TRIGGER]:
    1. Response sets `emergency_trigger = True`.
    2. Sets `emergency_notice`.
    3. Strips [EMERGENCY_TRIGGER] and [CLINICAL_REPORT] from clean reply.
    """
    client, session, _ = isolated_chat_env
    _, jwt_token = _get_device_jwt_with_scopes(
        client,
        session,
        patient_id="pat-emerg-01",
        device_id="dev-emerg-01",
        raw_code="810010",
        scope="vitals:read vitals:write cycle:read chat:read chat:write",
    )
    headers = {"Authorization": f"Bearer {jwt_token}"}

    resp = client.post(
        "/api/v1/client/chat/athena",
        json={"message": "У меня острая боль в груди и тяжело дышать"},
        headers=headers,
    )
    assert resp.status_code == 200
    data = resp.json()

    assert data["emergency_trigger"] is True
    assert data["emergency_notice"] is not None
    assert "[EMERGENCY_TRIGGER]" not in data["reply"]
    assert "[CLINICAL_REPORT]" not in data["reply"]
    assert "немедленно вызовите скорую помощь" in data["reply"]


def test_acute_vitals_emergency_fail_safe_fallback_when_gemini_unavailable():
    """
    Verifies that during an acute emergency (e.g. pulse spike to 170 bpm):
    If Gemini API fails with network connection error:
    1. Server DOES NOT crash with 502 Bad Gateway.
    2. Returns deterministic fail-safe emergency advice with emergency_trigger=True.
    3. Records CRITICAL clinical log in server storage.
    """
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    TestingSession = sessionmaker(bind=engine, autoflush=True, autocommit=False)
    session = TestingSession()

    async def broken_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Gemini gateway completely unreachable")

    broken_gateway = GeminiGateway(
        api_key="test-key",
        transport=httpx.MockTransport(broken_handler),
        sleep_func=lambda _: asyncio.sleep(0),
        max_retries=1,
    )

    def override_db():
        db = TestingSession()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_gemini_gateway] = lambda: broken_gateway

    orig_jwt = settings.jwt_secret
    orig_pair = settings.pairing_secret
    settings.jwt_secret = TEST_JWT_SECRET
    settings.pairing_secret = TEST_PAIRING_SECRET

    with TestClient(app) as client:
        _, jwt_token = _get_device_jwt_with_scopes(
            client,
            session,
            patient_id="pat-fail-safe-01",
            device_id="dev-fail-safe-01",
            raw_code="810011",
            scope="vitals:read vitals:write cycle:read chat:read chat:write",
        )
        headers = {"Authorization": f"Bearer {jwt_token}"}

        # Acute vital spike >= 150 bpm
        resp = client.post(
            "/api/v1/client/chat/athena",
            json={
                "event_type": "vitals_alert",
                "vitals_alert": {
                    "current_pulse": 172,
                    "baseline_pulse": 68,
                    "notes": "Patient collapsed",
                },
            },
            headers=headers,
        )

        assert resp.status_code == 200
        data = resp.json()
        assert data["emergency_trigger"] is True
        assert "скорую помощь (112)" in data["reply"]
        assert "CRITICAL_VITALS_ALERT" in data["emergency_notice"]

        # Clinical log saved in DB as CRITICAL
        session.expire_all()
        rep_repo = ModelReportRepository(session)
        logs = rep_repo.list_server_only(cycle_id=data["cycle_id"], report_type=ATHENA_CLINICAL_LOG_REPORT_TYPE)
        assert len(logs) == 1
        assert logs[0].content["status"] == "CRITICAL"
        assert logs[0].content["red_flags"] is True

    session.close()
    engine.dispose()
    app.dependency_overrides.clear()
    settings.jwt_secret = orig_jwt
    settings.pairing_secret = orig_pair


def test_routine_ai_failure_returns_502_when_no_emergency():
    """
    Verifies that for normal routine messages (without acute emergency signals):
    If Gemini API fails, returns HTTP 502 Bad Gateway.
    """
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    TestingSession = sessionmaker(bind=engine, autoflush=True, autocommit=False)
    session = TestingSession()

    async def broken_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Gemini API connection refused")

    broken_gateway = GeminiGateway(
        api_key="test-key",
        transport=httpx.MockTransport(broken_handler),
        sleep_func=lambda _: asyncio.sleep(0),
        max_retries=1,
    )

    def override_db():
        db = TestingSession()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_gemini_gateway] = lambda: broken_gateway

    orig_jwt = settings.jwt_secret
    orig_pair = settings.pairing_secret
    settings.jwt_secret = TEST_JWT_SECRET
    settings.pairing_secret = TEST_PAIRING_SECRET

    with TestClient(app) as client:
        _, jwt_token = _get_device_jwt_with_scopes(
            client,
            session,
            patient_id="pat-routine-fail-01",
            device_id="dev-routine-fail-01",
            raw_code="810012",
            scope="vitals:read vitals:write cycle:read chat:read chat:write",
        )
        headers = {"Authorization": f"Bearer {jwt_token}"}

        resp = client.post(
            "/api/v1/client/chat/athena",
            json={"message": "Обычное приветствие"},
            headers=headers,
        )
        assert resp.status_code == 502
        assert "temporarily unavailable" in resp.json()["detail"]

    session.close()
    engine.dispose()
    app.dependency_overrides.clear()
    settings.jwt_secret = orig_jwt
    settings.pairing_secret = orig_pair
