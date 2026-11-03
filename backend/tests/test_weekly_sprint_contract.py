import asyncio
from datetime import datetime, timezone, timedelta
import json
from typing import Any, Dict, Generator, List
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
from app.routers.client_cascade import get_gemini_gateway as client_get_gemini_gateway
from app.routers.cascade import get_gemini_gateway as cascade_get_gemini_gateway
from app.schemas.consilium import SYSTEM_KEYS
from app.security.tokens import create_access_token, verify_access_token
from app.services.gemini_gateway import GeminiGateway
from app.services.primary_consilium_service import PrimaryConsiliumService
from app.services.weekly_consilium_service import (
    WeeklyConsiliumService,
    WEEKLY_OVERALL_CLIENT_REPORT_TYPE,
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


TEST_JWT_SECRET = "test_jwt_signing_secret_at_least_16_chars_long!"
TEST_PAIRING_SECRET = "test_pairing_secret_deterministic_12345!"
TEST_API_SECRET = "test_device_secret_token_12345"


def _build_contract_mock_gateway(recorded_calls: List[Dict[str, Any]]) -> GeminiGateway:
    """
    Mock gateway handling both Primary Consilium and Weekly Consilium calls.
    Flash -> Pro for Weekly.
    """
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
        return httpx.Response(200, json=_gemini_envelope(_sample_weekly_flash_output()))

    return GeminiGateway(
        api_key="test-contract-gateway-key",
        transport=httpx.MockTransport(handler),
        sleep_func=lambda _: None,
    )


@pytest.fixture(scope="function")
def contract_test_env(monkeypatch, tmp_path) -> Generator[Any, None, None]:
    monkeypatch.setattr(settings, "api_secret", TEST_API_SECRET)
    monkeypatch.setattr(settings, "pairing_secret", TEST_PAIRING_SECRET)
    monkeypatch.setattr(settings, "jwt_secret", TEST_JWT_SECRET)
    monkeypatch.setattr(settings, "jwt_issuer", "aegis-backend")
    monkeypatch.setattr(settings, "jwt_audience", "aegis-mobile-client")
    monkeypatch.setattr(settings, "gemini_api_key", "test-gemini-key")

    db_file = tmp_path / "test_weekly_sprint_contract.db"
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
    recorded_ai_calls: List[Dict[str, Any]] = []
    mock_gw = _build_contract_mock_gateway(recorded_ai_calls)
    app.dependency_overrides[client_get_gemini_gateway] = lambda: mock_gw
    app.dependency_overrides[cascade_get_gemini_gateway] = lambda: mock_gw

    try:
        with TestClient(app) as test_client:
            yield test_client, session, recorded_ai_calls, mock_gw
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
            "device_name": "Test Workstation",
        },
    )
    assert resp.status_code == 200, resp.text
    token_bundle = resp.json()
    fam_id = verify_access_token(token_bundle["access_token"]).family_id
    jwt_token = create_access_token(
        patient_id=patient_id,
        device_id=device_id,
        family_id=fam_id,
        scope="cycle:read cascade:run",
    ).access_token
    return jwt_token


async def _seed_completed_primary(
    session: Session, cycle_id: str, seed_anchor: bool = True, anchor_hours_ago: float = 169.0
):
    _seed_preparatory_cycle_context(session, cycle_id=cycle_id, seed_ledger=True)
    p_calls: List[Dict[str, Any]] = []
    p_gw = _build_consilium_mock_gateway(p_calls)
    p_service = PrimaryConsiliumService(db=session, gateway=p_gw)
    await p_service.run_primary_consilium(cycle_id=cycle_id, report_date="2026-10-18")
    if seed_anchor:
        weekly_service = WeeklyConsiliumService(db=session, gateway=p_gw)
        weekly_service.record_schedule_anchor(
            cycle_id=cycle_id,
            anchor_dt=datetime.now(timezone.utc) - timedelta(hours=anchor_hours_ago),
            source="test_first_daily_question",
        )


# ==============================================================================
# 1. STATUS BEFORE FIRST WEEKLY
# ==============================================================================

@pytest.mark.anyio
async def test_weekly_sprint_status_before_first_weekly(contract_test_env):
    """
    Verifies GET /api/v1/client/cascade/status when Primary Consilium is completed,
    but no Weekly Consilium has been run yet:
    - is_primary_consilium_completed is True
    - current_weekly_sprint_id is None
    - weekly_sprint_number is None
    - is_weekly_consilium_completed is False
    - can_start_next_sprint is True
    - next_sprint_id is 'sprint_{cycle_id}_w1'
    """
    client, session, _, _ = contract_test_env
    patient_id = "patient_w_01"
    device_id = "device_w_01"
    token = _pair_and_get_token(client, session, patient_id, device_id)
    headers = {"Authorization": f"Bearer {token}"}

    # Provision cycle and complete primary consilium
    cycle_repo = MedicalCycleRepository(session)
    cycle, _ = cycle_repo.get_or_create_active_for_patient(patient_id)
    await _seed_completed_primary(session, cycle.cycle_id)

    resp = client.get("/api/v1/client/cascade/status", headers=headers)
    assert resp.status_code == 200
    data = resp.json()

    assert data["has_active_cycle"] is True
    assert data["active_cycle_id"] == cycle.cycle_id
    assert data["is_primary_consilium_completed"] is True
    assert data["current_weekly_sprint_id"] is None
    assert data["weekly_sprint_number"] is None
    assert data["is_weekly_consilium_completed"] is False
    assert data["can_start_next_sprint"] is True
    assert data["next_sprint_id"] == f"sprint_{cycle.cycle_id}_w1"


# ==============================================================================
# 2. STATUS AFTER WEEK 1
# ==============================================================================

@pytest.mark.anyio
async def test_weekly_sprint_status_after_week_1(contract_test_env):
    """
    Verifies that running Week 1 completes successfully and updates cascade status:
    - current_weekly_sprint_id == 'sprint_{cycle_id}_w1'
    - weekly_sprint_number == 1
    - is_weekly_consilium_completed == True
    - can_start_next_sprint == False (subsequent sprint not automatically authorized)
    - next_sprint_id is None
    """
    client, session, recorded_calls, _ = contract_test_env
    patient_id = "patient_w_02"
    device_id = "device_w_02"
    token = _pair_and_get_token(client, session, patient_id, device_id)
    headers = {"Authorization": f"Bearer {token}"}

    cycle_repo = MedicalCycleRepository(session)
    cycle, _ = cycle_repo.get_or_create_active_for_patient(patient_id)
    await _seed_completed_primary(session, cycle.cycle_id)

    # Launch Week 1 without explicit sprint_id (server authority targets w1)
    run_resp = client.post(
        "/api/v1/client/cascade/run-weekly",
        json={"report_date": "2026-10-25"},
        headers=headers,
    )
    assert run_resp.status_code == 200, run_resp.text
    run_data = run_resp.json()
    assert run_data["sprint_id"] == f"sprint_{cycle.cycle_id}_w1"
    assert run_data["sprint_number"] == 1
    assert "overall_report" in run_data
    assert len(run_data["system_reports"]) == 8

    # Verify status after Week 1
    status_resp = client.get("/api/v1/client/cascade/status", headers=headers)
    assert status_resp.status_code == 200
    st = status_resp.json()
    assert st["current_weekly_sprint_id"] == f"sprint_{cycle.cycle_id}_w1"
    assert st["weekly_sprint_number"] == 1
    assert st["is_weekly_consilium_completed"] is True
    assert st["can_start_next_sprint"] is False
    assert st["next_sprint_id"] is None


# ==============================================================================
# 3. REPLAY OF WEEK 1 (ZERO DUPLICATE AI CALLS)
# ==============================================================================

@pytest.mark.anyio
async def test_weekly_sprint_replay_week_1_zero_duplicate_ai_calls(contract_test_env):
    """
    Verifies that calling run-weekly after Week 1 has completed recovers existing reports
    with header X-Aegis-Idempotent-Replay: true and zero additional Gemini calls:
    - Replay with explicit sprint_id='sprint_{cycle_id}_w1'
    - Replay with sprint_id=None (server recovers latest completed)
    """
    client, session, recorded_calls, _ = contract_test_env
    patient_id = "patient_w_03"
    device_id = "device_w_03"
    token = _pair_and_get_token(client, session, patient_id, device_id)
    headers = {"Authorization": f"Bearer {token}"}

    cycle_repo = MedicalCycleRepository(session)
    cycle, _ = cycle_repo.get_or_create_active_for_patient(patient_id)
    await _seed_completed_primary(session, cycle.cycle_id)

    # Initial run for Week 1
    r1 = client.post(
        "/api/v1/client/cascade/run-weekly",
        json={"report_date": "2026-10-25"},
        headers=headers,
    )
    assert r1.status_code == 200
    initial_call_count = len(recorded_calls)
    assert initial_call_count >= 2

    # Replay 1: explicit sprint_id
    r_replay_explicit = client.post(
        "/api/v1/client/cascade/run-weekly",
        json={"sprint_id": f"sprint_{cycle.cycle_id}_w1"},
        headers=headers,
    )
    assert r_replay_explicit.status_code == 200
    assert r_replay_explicit.headers.get("x-aegis-idempotent-replay") == "true"
    assert r_replay_explicit.json()["sprint_id"] == f"sprint_{cycle.cycle_id}_w1"
    assert len(recorded_calls) == initial_call_count, "No new Gemini calls during replay"

    # Replay 2: sprint_id is None
    r_replay_default = client.post(
        "/api/v1/client/cascade/run-weekly",
        json={},
        headers=headers,
    )
    assert r_replay_default.status_code == 200
    assert r_replay_default.headers.get("x-aegis-idempotent-replay") == "true"
    assert r_replay_default.json()["sprint_id"] == f"sprint_{cycle.cycle_id}_w1"
    assert len(recorded_calls) == initial_call_count, "No new Gemini calls during default replay"


# ==============================================================================
# 4. PREMATURE LAUNCH OF WEEK 2 (REJECTION & SERVER AUTHORITY)
# ==============================================================================

@pytest.mark.anyio
async def test_weekly_sprint_premature_week_2_launch_rejected(contract_test_env):
    """
    Verifies server authority prevents client-side invention of next sprint:
    When Week 1 is completed, client attempting to launch Week 2 ('sprint_{cycle_id}_w2')
    without server authorization receives 409 Conflict.
    Crucially, it does NOT silently return Week 1 results.
    """
    client, session, recorded_calls, _ = contract_test_env
    patient_id = "patient_w_04"
    device_id = "device_w_04"
    token = _pair_and_get_token(client, session, patient_id, device_id)
    headers = {"Authorization": f"Bearer {token}"}

    cycle_repo = MedicalCycleRepository(session)
    cycle, _ = cycle_repo.get_or_create_active_for_patient(patient_id)
    await _seed_completed_primary(session, cycle.cycle_id)

    # Complete Week 1
    r_w1 = client.post(
        "/api/v1/client/cascade/run-weekly",
        json={"report_date": "2026-10-25"},
        headers=headers,
    )
    assert r_w1.status_code == 200
    calls_after_w1 = len(recorded_calls)

    # Attempt premature launch of Week 2
    premature_sprint_id = f"sprint_{cycle.cycle_id}_w2"
    r_premature = client.post(
        "/api/v1/client/cascade/run-weekly",
        json={"sprint_id": premature_sprint_id, "report_date": "2026-11-01"},
        headers=headers,
    )
    assert r_premature.status_code == 409
    err_detail = r_premature.json()["detail"]
    assert "transition to this weekly sprint is not authorized" in err_detail
    assert len(recorded_calls) == calls_after_w1, "Zero AI calls on premature rejection"


# ==============================================================================
# 5. ELIGIBLE TRANSITION TO WEEK 2
# ==============================================================================

@pytest.mark.anyio
async def test_weekly_sprint_eligible_transition_to_week_2(contract_test_env):
    """
    Verifies eligible progression to Week 2:
    - Week 1 is completed.
    - Server provisions Week 2 as active (status='active', sprint_number=2).
    - Status reflects active Week 2 (is_weekly_consilium_completed=False).
    - Client executes run-weekly for Week 2 -> completes successfully.
    - Subsequent status confirms Week 2 completion.
    """
    client, session, recorded_calls, _ = contract_test_env
    patient_id = "patient_w_05"
    device_id = "device_w_05"
    token = _pair_and_get_token(client, session, patient_id, device_id)
    headers = {"Authorization": f"Bearer {token}"}

    cycle_repo = MedicalCycleRepository(session)
    cycle, _ = cycle_repo.get_or_create_active_for_patient(patient_id)
    await _seed_completed_primary(session, cycle.cycle_id)

    # Run Week 1
    r_w1 = client.post(
        "/api/v1/client/cascade/run-weekly",
        json={"report_date": "2026-10-25"},
        headers=headers,
    )
    assert r_w1.status_code == 200

    # Server provisions active sprint for Week 2
    sprint_repo = WeeklySprintRepository(session)
    w2_sprint_id = f"sprint_{cycle.cycle_id}_w2"
    sprint_repo.create(
        sprint_id=w2_sprint_id,
        cycle_id=cycle.cycle_id,
        sprint_number=2,
        status="active",
    )

    # Status check reflects active Week 2
    st_resp = client.get("/api/v1/client/cascade/status", headers=headers)
    assert st_resp.status_code == 200
    st_data = st_resp.json()
    assert st_data["current_weekly_sprint_id"] == w2_sprint_id
    assert st_data["weekly_sprint_number"] == 2
    assert st_data["is_weekly_consilium_completed"] is False
    assert st_data["can_start_next_sprint"] is False

    # ARGON BUG-06: Premature execution before 336h boundary is rejected with 409
    r_w2_early = client.post(
        "/api/v1/client/cascade/run-weekly",
        json={"sprint_id": w2_sprint_id, "report_date": "2026-11-01"},
        headers=headers,
    )
    assert r_w2_early.status_code == 409

    # Advance schedule anchor past 336h boundary (337h ago) to make Week 2 eligible
    report_repo = ModelReportRepository(session)
    anchors = report_repo.list_server_only(cycle.cycle_id, "athena_schedule_anchor")
    if anchors:
        anchors[0].content = {
            **anchors[0].content,
            "anchor_timestamp_utc": (datetime.now(timezone.utc) - timedelta(hours=337)).isoformat(),
        }
        session.commit()

    # Recheck status: Week 2 is now eligible to start
    st_resp_eligible = client.get("/api/v1/client/cascade/status", headers=headers)
    assert st_resp_eligible.status_code == 200
    st_data_el = st_resp_eligible.json()
    assert st_data_el["can_start_next_sprint"] is True
    assert st_data_el["next_sprint_id"] == w2_sprint_id

    # Execute consilium for Week 2 (now authorized)
    r_w2 = client.post(
        "/api/v1/client/cascade/run-weekly",
        json={"sprint_id": w2_sprint_id, "report_date": "2026-11-01"},
        headers=headers,
    )
    assert r_w2.status_code == 200, r_w2.text
    w2_data = r_w2.json()
    assert w2_data["sprint_id"] == w2_sprint_id
    assert w2_data["sprint_number"] == 2

    # Status check confirms Week 2 is completed
    st_resp2 = client.get("/api/v1/client/cascade/status", headers=headers)
    st_data2 = st_resp2.json()
    assert st_data2["current_weekly_sprint_id"] == w2_sprint_id
    assert st_data2["weekly_sprint_number"] == 2
    assert st_data2["is_weekly_consilium_completed"] is True


# ==============================================================================
# 6. CONCURRENT REQUESTS (MUTUAL EXCLUSION & SAFE LEASING)
# ==============================================================================

@pytest.mark.anyio
async def test_weekly_sprint_concurrent_requests_mutual_exclusion(contract_test_env):
    """
    Verifies that simultaneous calls to run-weekly are safely serialized or rejected
    by distributed SQLite lease fencing without double AI execution or DB corruption.
    """
    client, session, recorded_calls, _ = contract_test_env
    patient_id = "patient_w_06"
    device_id = "device_w_06"
    token = _pair_and_get_token(client, session, patient_id, device_id)
    headers = {"Authorization": f"Bearer {token}"}

    cycle_repo = MedicalCycleRepository(session)
    cycle, _ = cycle_repo.get_or_create_active_for_patient(patient_id)
    await _seed_completed_primary(session, cycle.cycle_id)

    # Acquire lock in advance using WeeklyConsiliumService
    w_service = WeeklyConsiliumService(db=session)
    lease_owner = w_service._acquire_cross_process_lease(cycle.cycle_id)
    assert lease_owner is not None

    # While lock is held, API call must receive 409 Conflict
    r_locked = client.post(
        "/api/v1/client/cascade/run-weekly",
        json={"report_date": "2026-10-25"},
        headers=headers,
    )
    assert r_locked.status_code == 409
    assert "in progress" in r_locked.json()["detail"].lower()

    # Release lock and run again -> succeeds
    w_service._release_cross_process_lease_safe(cycle.cycle_id, lease_owner)

    r_ok = client.post(
        "/api/v1/client/cascade/run-weekly",
        json={"report_date": "2026-10-25"},
        headers=headers,
    )
    assert r_ok.status_code == 200
    assert r_ok.json()["sprint_id"] == f"sprint_{cycle.cycle_id}_w1"


# ==============================================================================
# 7. MEDICAL CYCLE SWITCH ISOLATION
# ==============================================================================

@pytest.mark.anyio
async def test_weekly_sprint_medical_cycle_switch_isolation(contract_test_env):
    """
    Verifies cycle switch behavior:
    - Cycle 1 has completed Week 1.
    - Cycle 1 is closed; Cycle 2 is opened for the patient.
    - Status for patient immediately reflects Cycle 2 (Primary not completed -> can_start_next_sprint=False).
    - Submitting Cycle 1's sprint_id to Cycle 2 is rejected with 409 (belongs to different cycle).
    """
    client, session, _, _ = contract_test_env
    patient_id = "patient_w_07"
    device_id = "device_w_07"
    token = _pair_and_get_token(client, session, patient_id, device_id)
    headers = {"Authorization": f"Bearer {token}"}

    cycle_repo = MedicalCycleRepository(session)
    c1, _ = cycle_repo.get_or_create_active_for_patient(patient_id)
    c1_id = c1.cycle_id
    await _seed_completed_primary(session, c1_id)

    # Complete Week 1 in Cycle 1
    r_w1 = client.post(
        "/api/v1/client/cascade/run-weekly",
        json={"report_date": "2026-10-25"},
        headers=headers,
    )
    assert r_w1.status_code == 200
    c1_sprint_id = f"sprint_{c1_id}_w1"

    # Close Cycle 1 and create Cycle 2
    c1.status = "completed"
    c1.closed_at = datetime.now(timezone.utc)
    c2 = MedicalCycle(
        cycle_id=f"cycle_{patient_id}_02",
        patient_id=patient_id,
        status="active",
        started_at=datetime.now(timezone.utc),
    )
    session.add(c2)
    session.commit()

    # Status check reflects Cycle 2
    st_resp = client.get("/api/v1/client/cascade/status", headers=headers)
    assert st_resp.status_code == 200
    st = st_resp.json()
    assert st["active_cycle_id"] == c2.cycle_id
    assert st["is_primary_consilium_completed"] is False
    assert st["can_start_next_sprint"] is False

    # Attempting to run with Cycle 1's sprint_id on Cycle 2
    r_foreign_cycle = client.post(
        "/api/v1/client/cascade/run-weekly",
        json={"sprint_id": c1_sprint_id},
        headers=headers,
    )
    # Rejection: Primary Consilium must be completed before running Weekly Consilium
    assert r_foreign_cycle.status_code == 409

    # Complete Primary for Cycle 2, then re-test sprint belonging to Cycle 1
    await _seed_completed_primary(session, c2.cycle_id)
    r_foreign_cycle_after_primary = client.post(
        "/api/v1/client/cascade/run-weekly",
        json={"sprint_id": c1_sprint_id},
        headers=headers,
    )
    assert r_foreign_cycle_after_primary.status_code == 409
    assert "belongs to a different clinical cycle" in r_foreign_cycle_after_primary.json()["detail"]


# ==============================================================================
# 8. FOREIGN SPRINT ACCESS CONTROL (PATIENT ISOLATION)
# ==============================================================================

@pytest.mark.anyio
async def test_weekly_sprint_foreign_sprint_access_control(contract_test_env):
    """
    Verifies that Patient B cannot access or trigger Patient A's weekly sprint:
    DeviceSession JWT enforces patient boundary and reject cross-patient sprint requests with 409.
    """
    client, session, _, _ = contract_test_env

    # Setup Patient A
    token_a = _pair_and_get_token(client, session, "patient_A", "device_A")
    cycle_repo = MedicalCycleRepository(session)
    cA, _ = cycle_repo.get_or_create_active_for_patient("patient_A")
    await _seed_completed_primary(session, cA.cycle_id)
    r_w1_a = client.post(
        "/api/v1/client/cascade/run-weekly",
        headers={"Authorization": f"Bearer {token_a}"},
    )
    assert r_w1_a.status_code == 200
    sprint_a_id = f"sprint_{cA.cycle_id}_w1"

    # Setup Patient B
    token_b = _pair_and_get_token(client, session, "patient_B", "device_B")
    cB, _ = cycle_repo.get_or_create_active_for_patient("patient_B")
    await _seed_completed_primary(session, cB.cycle_id)

    # Patient B tries to run or recover sprint_a_id
    r_b_steals_a = client.post(
        "/api/v1/client/cascade/run-weekly",
        json={"sprint_id": sprint_a_id},
        headers={"Authorization": f"Bearer {token_b}"},
    )
    assert r_b_steals_a.status_code == 409
    assert "belongs to a different clinical cycle" in r_b_steals_a.json()["detail"]


# ==============================================================================
# 9. PRIMARY CONSILIUM NOT COMPLETED GUARDS
# ==============================================================================

@pytest.mark.anyio
async def test_weekly_sprint_blocked_when_primary_not_completed(contract_test_env):
    """
    Verifies that if Primary Consilium is NOT completed:
    - GET /api/v1/client/cascade/status returns is_primary_consilium_completed=False, can_start_next_sprint=False.
    - POST /api/v1/client/cascade/run-weekly returns 409 Conflict.
    """
    client, session, _, _ = contract_test_env
    patient_id = "patient_w_09"
    device_id = "device_w_09"
    token = _pair_and_get_token(client, session, patient_id, device_id)
    headers = {"Authorization": f"Bearer {token}"}

    cycle_repo = MedicalCycleRepository(session)
    cycle, _ = cycle_repo.get_or_create_active_for_patient(patient_id)
    # Note: Primary Consilium NOT seeded/completed

    # Status check
    st_resp = client.get("/api/v1/client/cascade/status", headers=headers)
    assert st_resp.status_code == 200
    st = st_resp.json()
    assert st["is_primary_consilium_completed"] is False
    assert st["can_start_next_sprint"] is False
    assert st["next_sprint_id"] is None

    # Run attempt
    run_resp = client.post(
        "/api/v1/client/cascade/run-weekly",
        headers=headers,
    )
    assert run_resp.status_code == 409
    assert "Primary Consilium must be completed before running Weekly Consilium" in run_resp.json()["detail"]
