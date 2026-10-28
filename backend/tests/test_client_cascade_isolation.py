import asyncio
import json
import os
from typing import Any, Dict, Generator, List, Optional
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
from app.models.model_report import ModelReport
from app.schemas.privacy import PrivacyLevel
from app.repositories.cycle_repository import MedicalCycleRepository
from app.repositories.pairing_repository import PairingRepository
from app.repositories.patient_repository import PatientRepository
from app.repositories.report_repository import ModelReportRepository
from app.routers.client_cascade import get_gemini_gateway
from app.security.tokens import (
    LEGACY_DEVICE_SCOPE,
    create_access_token,
    verify_access_token,
)
from app.services.gemini_gateway import GeminiGateway
from app.services.lab_revision_consilium_service import LabRevisionConsiliumService
from app.services.primary_consilium_service import (
    PREPARATORY_ANAMNESIS_REPORT_TYPE,
    RAW_USER_ANAMNESIS_REPORT_TYPE,
)
from tests.test_primary_consilium import (
    _gemini_envelope,
    _sample_anamnesis_output,
)


TEST_JWT_SECRET = "test_jwt_signing_secret_at_least_16_chars_long!"
TEST_PAIRING_SECRET = "test_pairing_secret_deterministic_12345!"
TEST_API_SECRET = "test_device_secret_token_12345"


# ==============================================================================
# DETERMINISTIC MOCK GEMINI GATEWAY FOR ANAMNESIS M1
# ==============================================================================

def _build_m1_mock_gateway(recorded_calls: List[Dict[str, Any]]) -> GeminiGateway:
    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        prompt_text = body["contents"][0]["parts"][0]["text"]
        recorded_calls.append({"url_path": request.url.path, "prompt": prompt_text})
        return httpx.Response(200, json=_gemini_envelope(_sample_anamnesis_output()))

    return GeminiGateway(
        api_key="test-key",
        transport=httpx.MockTransport(handler),
        sleep_func=lambda _: asyncio.sleep(0),
    )


def _sample_intake_answers(**overrides) -> Dict[str, Any]:
    base = {
        "age_confirmation": "Confirmed",
        "gender": "Male",
        "age": 34,
        "height": 182,
        "weight": 78,
        "nicotine_status": "Non-user",
        "nicotine_years": None,
        "nicotine_freq": None,
        "alcohol_status": "Rarely",
        "alcohol_type": "Wine",
        "spec_factors": "spec_none",
        "sleep_quality": "Great",
        "diet_types": "diet_meat, diet_veg",
        "diet_habits": "hab_balance",
        "gi_signals": "gi_ok",
        "work_type": "Sedentary",
        "work_schedule": "5/2",
        "sport_level": "Light",
        "chronic_diseases": "chr_none",
        "onco_status": None,
        "recent_illness": "ill_ok",
        "critical_infections": "safe_clear",
        "heredity_risks": "her_none",
        "medications": "med_none",
        "user_text_medications": None,
        "user_text_heredity_risks": None,
        "user_text_chronic_diseases": None,
        "user_text_recent_illness": None,
        "user_text_critical_infections": None,
        "user_text_spec_factors": None,
        "user_text_diet_habits": None,
        "user_text_gi_signals": None,
    }
    base.update(overrides)
    return base


# ==============================================================================
# ISOLATED FIXTURES
# ==============================================================================

@pytest.fixture(scope="function")
def isolated_client_env(monkeypatch, tmp_path) -> Generator[Any, None, None]:
    monkeypatch.setattr(settings, "api_secret", TEST_API_SECRET)
    monkeypatch.setattr(settings, "pairing_secret", TEST_PAIRING_SECRET)
    monkeypatch.setattr(settings, "jwt_secret", TEST_JWT_SECRET)
    monkeypatch.setattr(settings, "jwt_issuer", "aegis-backend")
    monkeypatch.setattr(settings, "jwt_audience", "aegis-mobile-client")

    db_file = tmp_path / "test_client_cascade_isolation.db"
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
    mock_gw = _build_m1_mock_gateway(recorded_ai_calls)
    app.dependency_overrides[get_gemini_gateway] = lambda: mock_gw

    try:
        with TestClient(app) as test_client:
            yield test_client, session, recorded_ai_calls
    finally:
        app.dependency_overrides.clear()
        session.close()
        eng.dispose()


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
            "device_name": "Test Client Device",
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
# TESTS: 401 UNAUTHORIZED / 403 FORBIDDEN / SCOPES BOUNDARY
# ==============================================================================

def test_client_circuit_unauthenticated_returns_401(isolated_client_env):
    """
    Verifies that requests to `/api/v1/client/cascade/*` without Authorization header
    or with invalid/expired tokens strictly return 401 Unauthorized.
    """
    client, _, _ = isolated_client_env

    # 1. No auth header
    r_no_auth = client.post(
        "/api/v1/client/cascade/intake/anamnesis",
        json={"answers": _sample_intake_answers()},
    )
    assert r_no_auth.status_code == 401

    # 2. Malformed token
    r_bad_auth = client.post(
        "/api/v1/client/cascade/intake/anamnesis",
        json={"answers": _sample_intake_answers()},
        headers={"Authorization": "Bearer not-a-valid-token"},
    )
    assert r_bad_auth.status_code == 401


def test_static_admin_secret_rejected_on_client_circuit(isolated_client_env):
    """
    Verifies that sending the static `AEGIS_API_SECRET` to `/api/v1/client/*`
    is strictly rejected with 401 Unauthorized (static secrets prohibited on mobile circuit).
    """
    client, _, _ = isolated_client_env
    headers = {"Authorization": f"Bearer {TEST_API_SECRET}"}

    resp = client.post(
        "/api/v1/client/cascade/intake/anamnesis",
        json={"answers": _sample_intake_answers()},
        headers=headers,
    )
    assert resp.status_code == 401
    assert "Mobile device JWT token is required" in resp.json()["detail"]


def test_device_jwt_rejected_on_admin_cascade_circuit(isolated_client_env):
    """
    Verifies that a mobile `DeviceSession` JWT attempting to invoke administrative
    cascade endpoints (`/api/v1/cascade/*` or `/api/v1/cycle/start`) is rejected with 403 Forbidden.
    """
    client, session, _ = isolated_client_env
    _, client_jwt = _get_device_jwt_with_scopes(
        client,
        session,
        patient_id="pat-admin-test-01",
        device_id="dev-admin-test-01",
        raw_code="740001",
        scope="vitals:read vitals:write cycle:read cascade:intake cascade:run admin",
    )
    headers = {"Authorization": f"Bearer {client_jwt}"}

    # Admin cascade endpoint:
    r_admin_cascade = client.post(
        "/api/v1/cascade/intake/anamnesis",
        json={"cycle_id": "any-cycle", "answers": _sample_intake_answers()},
        headers=headers,
    )
    assert r_admin_cascade.status_code == 403
    assert "Operation requires service administrator privileges" in r_admin_cascade.json()["detail"]

    # Admin cycle start endpoint:
    r_admin_cycle = client.post(
        "/api/v1/cycle/start",
        json={"cycle_id": "test-admin-cycle", "patient_id": "pat-admin-test-01"},
        headers=headers,
    )
    assert r_admin_cycle.status_code == 403


def test_missing_cascade_intake_scope_returns_403(isolated_client_env):
    """
    Verifies that a mobile device token lacking `cascade:intake`
    (e.g., legacy Stage 5.3 scope "vitals:read vitals:write cycle:read")
    receives 403 Forbidden when attempting to call `/api/v1/client/cascade/intake/anamnesis`,
    while a standard paired device token succeeds.
    """
    client, session, _ = isolated_client_env
    pair_res = _pair_patient_device(
        client, session, patient_id="pat-scope-01", device_id="dev-scope-01", raw_code="740002"
    )
    # 1. Legacy token without cascade:intake -> 403
    real_claims = verify_access_token(pair_res["access_token"])
    legacy_jwt = create_access_token(
        patient_id="pat-scope-01",
        device_id="dev-scope-01",
        family_id=real_claims.family_id,
        scope=LEGACY_DEVICE_SCOPE,
    ).access_token
    legacy_headers = {"Authorization": f"Bearer {legacy_jwt}"}

    resp = client.post(
        "/api/v1/client/cascade/intake/anamnesis",
        json={"answers": _sample_intake_answers()},
        headers=legacy_headers,
    )
    assert resp.status_code == 403
    assert resp.json()["detail"] == "Insufficient token scope for this operation."

    # 2. Standard pairing token (possesses all standard scopes including cascade:intake) -> 200 OK
    default_headers = {"Authorization": f"Bearer {pair_res['access_token']}"}
    resp_std = client.post(
        "/api/v1/client/cascade/intake/anamnesis",
        json={"answers": _sample_intake_answers()},
        headers=default_headers,
    )
    assert resp_std.status_code == 200


# ==============================================================================
# TESTS: CYCLE PROVISIONING & CONTENT IDEMPOTENCY
# ==============================================================================

def test_auto_provision_cycle_on_first_anamnesis_intake(isolated_client_env):
    """
    Verifies that when a freshly paired patient submits their first anamnesis intake
    without an existing active cycle in the database:
    1. The backend automatically and atomically provisions Cycle 1 for this patient.
    2. Primary Consilium M1 executes in blind mode.
    3. The response contains 200 OK with `intake_version=1`.
    4. Exactly one active cycle exists in the database for this patient.
    """
    client, session, ai_calls = isolated_client_env
    _, jwt_token = _get_device_jwt_with_scopes(
        client,
        session,
        patient_id="pat-auto-cycle-01",
        device_id="dev-auto-cycle-01",
        raw_code="740003",
        scope="vitals:read vitals:write cycle:read cascade:intake",
    )
    headers = {"Authorization": f"Bearer {jwt_token}"}

    cycle_repo = MedicalCycleRepository(session)
    assert cycle_repo.get_active_for_patient("pat-auto-cycle-01") is None

    # Submit anamnesis without providing cycle_id
    resp = client.post(
        "/api/v1/client/cascade/intake/anamnesis",
        json={"answers": _sample_intake_answers()},
        headers=headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "processed"
    assert data["intake_version"] == 1
    assert data["cycle_id"].startswith("cycle_")
    assert len(ai_calls) == 1

    # Verify database state
    session.expire_all()
    active_cycle = cycle_repo.get_active_for_patient("pat-auto-cycle-01")
    assert active_cycle is not None
    assert active_cycle.cycle_id == data["cycle_id"]
    assert active_cycle.patient_id == "pat-auto-cycle-01"
    assert active_cycle.status == "active"


def test_idempotent_cycle_provisioning_and_content_deduplication(isolated_client_env):
    """
    Verifies that calling anamnesis intake again with identical answers:
    1. Reuses the exact same active cycle (does NOT create a second cycle).
    2. Detects identical raw answers and returns existing intake_version=1.
    3. Guarantees ZERO duplicate Gemini AI calls.
    """
    client, session, ai_calls = isolated_client_env
    _, jwt_token = _get_device_jwt_with_scopes(
        client,
        session,
        patient_id="pat-idem-01",
        device_id="dev-idem-01",
        raw_code="740004",
        scope="vitals:read vitals:write cycle:read cascade:intake",
    )
    headers = {"Authorization": f"Bearer {jwt_token}"}

    # First call: provisions cycle and invokes AI
    resp1 = client.post(
        "/api/v1/client/cascade/intake/anamnesis",
        json={"answers": _sample_intake_answers()},
        headers=headers,
    )
    assert resp1.status_code == 200
    cycle_id_1 = resp1.json()["cycle_id"]
    assert len(ai_calls) == 1

    # Second call (retry / identical payload):
    resp2 = client.post(
        "/api/v1/client/cascade/intake/anamnesis",
        json={"answers": _sample_intake_answers()},
        headers=headers,
    )
    assert resp2.status_code == 200
    assert resp2.json()["cycle_id"] == cycle_id_1
    assert resp2.json()["intake_version"] == 1
    # Gemini Flash was NOT called a second time!
    assert len(ai_calls) == 1

    # Verify DB still has exactly 1 cycle
    session.expire_all()
    cycles = MedicalCycleRepository(session).list_by_patient("pat-idem-01")
    assert len(cycles) == 1


# ==============================================================================
# TESTS: PATIENT CONTEXT ISOLATION & ZERO ORACLE LEAKAGE
# ==============================================================================

def test_cannot_substitute_foreign_patient_or_cycle(isolated_client_env):
    """
    Verifies that if Patient A attempts to supply a `cycle_id` belonging to Patient B,
    the server completely ignores the client-supplied `cycle_id` and operates strictly
    within Patient A's own verified active cycle.
    """
    client, session, _ = isolated_client_env
    # Provision Patient B with an active cycle
    cycle_repo = MedicalCycleRepository(session)
    PatientRepository(session).create(patient_id="pat-b-victim", status="active")
    cycle_b = cycle_repo.create(cycle_id="cycle-victim-b", patient_id="pat-b-victim", status="active")

    # Patient A submits anamnesis intake claiming Patient B's cycle_id in payload
    _, jwt_a = _get_device_jwt_with_scopes(
        client,
        session,
        patient_id="pat-a-attacker",
        device_id="dev-a-attacker",
        raw_code="740005",
        scope="vitals:read vitals:write cycle:read cascade:intake",
    )
    headers_a = {"Authorization": f"Bearer {jwt_a}"}

    resp = client.post(
        "/api/v1/client/cascade/intake/anamnesis",
        json={
            "answers": _sample_intake_answers(),
            "cycle_id": "cycle-victim-b",  # Attempted override
        },
        headers=headers_a,
    )
    assert resp.status_code == 200
    data = resp.json()
    # Cycle assigned to Patient A is NOT Patient B's cycle!
    assert data["cycle_id"] != cycle_b.cycle_id
    assert data["cycle_id"] != "cycle-victim-b"

    # Patient B's cycle remains completely untouched
    session.expire_all()
    report_repo = ModelReportRepository(session)
    assert len(report_repo.list_server_only(cycle_id=cycle_b.cycle_id)) == 0


def test_client_labs_state_isolation_between_patients(isolated_client_env):
    """
    Verifies that `GET /api/v1/client/labs/state` returns only the laboratory markers
    of the authenticated patient's active cycle, with zero cross-patient data leakage.
    """
    client, session, _ = isolated_client_env
    cycle_repo = MedicalCycleRepository(session)

    # 1. Setup Patient A with glucose marker in ledger
    _, jwt_a = _get_device_jwt_with_scopes(
        client, session, patient_id="pat-lab-a", device_id="dev-lab-a", raw_code="740006",
        scope="vitals:read vitals:write cycle:read reports:read",
    )
    cycle_a = cycle_repo.create(cycle_id="cycle-lab-a", patient_id="pat-lab-a", status="active")
    lab_service = LabRevisionConsiliumService(db=session)
    lab_service.upsert_laboratory_markers(
        cycle_id=cycle_a.cycle_id,
        mutations=[{"marker_key": "glucose", "value": 5.1, "unit": "mmol/l"}],
    )

    # 2. Setup Patient B with creatinine marker in ledger
    _, jwt_b = _get_device_jwt_with_scopes(
        client, session, patient_id="pat-lab-b", device_id="dev-lab-b", raw_code="740007",
        scope="vitals:read vitals:write cycle:read reports:read",
    )
    cycle_b = cycle_repo.create(cycle_id="cycle-lab-b", patient_id="pat-lab-b", status="active")
    lab_service.upsert_laboratory_markers(
        cycle_id=cycle_b.cycle_id,
        mutations=[{"marker_key": "creatinine", "value": 85.0, "unit": "umol/l"}],
    )

    # 3. Patient A requests labs state
    r_a = client.get("/api/v1/client/labs/state", headers={"Authorization": f"Bearer {jwt_a}"})
    assert r_a.status_code == 200
    data_a = r_a.json()
    assert data_a["cycle_id"] == cycle_a.cycle_id
    assert "glucose" in data_a["active_markers"]
    assert "creatinine" not in data_a["active_markers"]

    # 4. Patient B requests labs state
    r_b = client.get("/api/v1/client/labs/state", headers={"Authorization": f"Bearer {jwt_b}"})
    assert r_b.status_code == 200
    data_b = r_b.json()
    assert data_b["cycle_id"] == cycle_b.cycle_id
    assert "creatinine" in data_b["active_markers"]
    assert "glucose" not in data_b["active_markers"]


# ==============================================================================
# TESTS: PRIMARY CONSILIUM LOCKING & RESULT RECOVERY
# ==============================================================================

def test_primary_consilium_anamnesis_locked_returns_409(isolated_client_env):
    """
    Verifies that once Primary Consilium has been published for a cycle,
    subsequent attempts to modify or resubmit anamnesis intake return 409 Conflict.
    """
    client, session, _ = isolated_client_env
    _, jwt_token = _get_device_jwt_with_scopes(
        client,
        session,
        patient_id="pat-lock-01",
        device_id="dev-lock-01",
        raw_code="740008",
        scope="vitals:read vitals:write cycle:read cascade:intake",
    )
    headers = {"Authorization": f"Bearer {jwt_token}"}

    # 1. First intake succeeds
    r1 = client.post(
        "/api/v1/client/cascade/intake/anamnesis",
        json={"answers": _sample_intake_answers()},
        headers=headers,
    )
    assert r1.status_code == 200
    cycle_id = r1.json()["cycle_id"]

    # 2. Simulate Primary Consilium completion (publish client_facing user_overall_report)
    report_repo = ModelReportRepository(session)
    report_repo.create(
        report_id=f"rep_overall_{cycle_id}",
        cycle_id=cycle_id,
        model="gemini-3.1-pro",
        report_type="user_overall_report",
        privacy_level=PrivacyLevel.CLIENT_FACING,
        content={"title": "Primary Consilium Conclusion", "summary": "Full evaluation complete."},
    )
    report_repo.create(
        report_id=f"rep_quality_{cycle_id}",
        cycle_id=cycle_id,
        model="gemini-3.6-flash",
        report_type="flash_quality_review",
        privacy_level=PrivacyLevel.SERVER_ONLY,
        content={"approved_for_publication": True},
    )

    # 3. Submitting anamnesis now must return 409 Conflict
    r2 = client.post(
        "/api/v1/client/cascade/intake/anamnesis",
        json={"answers": _sample_intake_answers(age=35)},
        headers=headers,
    )
    assert r2.status_code == 409
    assert "Primary Consilium has already been executed" in r2.json()["detail"]


def test_concurrency_result_recovery_without_duplicate_ai(isolated_client_env):
    """
    Verifies that calling `POST /api/v1/client/cascade/run-primary` when Primary Consilium
    is ALREADY completed safely recovers and returns the existing completed report (`200 OK`)
    with `X-Aegis-Idempotent-Replay: true`, without repeating any Gemini calls.
    """
    client, session, ai_calls = isolated_client_env
    _, jwt_token = _get_device_jwt_with_scopes(
        client,
        session,
        patient_id="pat-recover-01",
        device_id="dev-recover-01",
        raw_code="740009",
        scope="vitals:read vitals:write cycle:read cascade:run",
    )
    headers = {"Authorization": f"Bearer {jwt_token}"}

    # Provision cycle and completed reports in DB
    cycle_repo = MedicalCycleRepository(session)
    cycle = cycle_repo.create(cycle_id="cycle-recover-01", patient_id="pat-recover-01", status="active")
    report_repo = ModelReportRepository(session)

    report_repo.create(
        report_id="rep_overall_rec_01",
        cycle_id=cycle.cycle_id,
        model="gemini-3.1-pro",
        report_type="user_overall_report",
        privacy_level=PrivacyLevel.CLIENT_FACING,
        content={
            "title": "Recovered Overall Plan",
            "summary": "Existing health roadmap",
            "report_date": "2026-10-27",
            "highlights": ["Strong CVS baseline"],
            "metrics": {"min_pessimum_bpm": 50, "max_pessimum_bpm": 120, "max_spike_delta_bpm": 20},
        },
    )
    report_repo.create(
        report_id="rep_quality_rec_01",
        cycle_id=cycle.cycle_id,
        model="gemini-3.6-flash",
        report_type="flash_quality_review",
        privacy_level=PrivacyLevel.SERVER_ONLY,
        content={"approved_for_publication": True},
    )

    # Call run-primary: should recover existing result with zero AI calls
    initial_ai_call_count = len(ai_calls)
    resp = client.post("/api/v1/client/cascade/run-primary", headers=headers)
    assert resp.status_code == 200
    assert resp.headers.get("x-aegis-idempotent-replay") == "true"

    data = resp.json()
    assert data["cycle_id"] == cycle.cycle_id
    assert data["overall_report"]["content"]["title"] == "Recovered Overall Plan"
    assert data["vitals_thresholds"]["min_pessimum_bpm"] == 50
    assert len(ai_calls) == initial_ai_call_count


def test_fresh_run_primary_blocked_pending_distributed_lease(isolated_client_env):
    """
    Verifies that calling `POST /api/v1/client/cascade/run-primary` when Primary Consilium
    is NOT yet completed returns 503 Service Unavailable with a structured blocker detail,
    enforcing Project Aegis Invariant: un-fenced multi-agent AI execution without atomic
    lease, renewal heartbeat, and write fencing is prohibited.
    """
    client, session, _ = isolated_client_env
    _, jwt_token = _get_device_jwt_with_scopes(
        client,
        session,
        patient_id="pat-fresh-block-01",
        device_id="dev-fresh-block-01",
        raw_code="740010",
        scope="vitals:read vitals:write cycle:read cascade:run",
    )
    headers = {"Authorization": f"Bearer {jwt_token}"}

    cycle_repo = MedicalCycleRepository(session)
    cycle_repo.create(cycle_id="cycle-fresh-01", patient_id="pat-fresh-block-01", status="active")

    resp = client.post("/api/v1/client/cascade/run-primary", headers=headers)
    assert resp.status_code == 503
    detail = resp.json()["detail"]
    assert detail["status"] == "BLOCKED_PENDING_DISTRIBUTED_LEASE"
    assert detail["blocker_code"] == "PRIMARY_CONSILIUM_LEASE_GUARD_REQUIRED"
    assert "atomic_lease_acquisition_with_lease_owner_id" in detail["missing_guarantees"]


# ==============================================================================
# TESTS: PRIVACY LEVEL ISOLATION & STATUS ENDPOINT
# ==============================================================================

def test_private_model_reports_never_leaked_in_client_response(isolated_client_env):
    """
    Verifies that after successful anamnesis intake:
    1. Server storage holds raw answers (`raw_user_anamnesis`) and verified M1
       conclusions (`preparatory_anamnesis_m1`) under `server_only` privacy level.
    2. The HTTP response to the mobile client contains ONLY metadata confirmation
       (cycle_id, status, version, timestamp, notice). Zero private clinical reasoning is exposed.
    """
    client, session, _ = isolated_client_env
    _, jwt_token = _get_device_jwt_with_scopes(
        client,
        session,
        patient_id="pat-privacy-01",
        device_id="dev-privacy-01",
        raw_code="740011",
        scope="vitals:read vitals:write cycle:read cascade:intake",
    )
    headers = {"Authorization": f"Bearer {jwt_token}"}

    resp = client.post(
        "/api/v1/client/cascade/intake/anamnesis",
        json={"answers": _sample_intake_answers(user_text_chronic_diseases="Confidential medical note")},
        headers=headers,
    )
    assert resp.status_code == 200
    data = resp.json()

    # Zero leakage in HTTP response
    assert "system_logic" not in data
    assert "training_sandbox" not in data
    assert "raw_user_anamnesis" not in data
    assert "Confidential medical note" not in json.dumps(data)

    # In DB, reports are strictly server_only
    session.expire_all()
    report_repo = ModelReportRepository(session)
    m1_reps = report_repo.list_server_only(cycle_id=data["cycle_id"], report_type=PREPARATORY_ANAMNESIS_REPORT_TYPE)
    assert len(m1_reps) == 1
    assert m1_reps[0].privacy_level == PrivacyLevel.SERVER_ONLY.value

    raw_reps = report_repo.list_server_only(cycle_id=data["cycle_id"], report_type=RAW_USER_ANAMNESIS_REPORT_TYPE)
    assert len(raw_reps) == 1
    assert raw_reps[0].privacy_level == PrivacyLevel.SERVER_ONLY.value


def test_cascade_status_endpoint(isolated_client_env):
    """
    Verifies `GET /api/v1/client/cascade/status` correctly reflects pipeline status:
    - Before intake: has_active_cycle=False (if no cycle)
    - After intake: has_active_cycle=True, has_preparatory_anamnesis=True, is_primary_consilium_completed=False
    """
    client, session, _ = isolated_client_env
    _, jwt_token = _get_device_jwt_with_scopes(
        client,
        session,
        patient_id="pat-status-01",
        device_id="dev-status-01",
        raw_code="740012",
        scope="vitals:read vitals:write cycle:read cascade:intake",
    )
    headers = {"Authorization": f"Bearer {jwt_token}"}

    # Before cycle exists
    r1 = client.get("/api/v1/client/cascade/status", headers=headers)
    assert r1.status_code == 200
    assert r1.json()["has_active_cycle"] is False

    # After intake
    client.post(
        "/api/v1/client/cascade/intake/anamnesis",
        json={"answers": _sample_intake_answers()},
        headers=headers,
    )

    r2 = client.get("/api/v1/client/cascade/status", headers=headers)
    assert r2.status_code == 200
    d2 = r2.json()
    assert d2["has_active_cycle"] is True
    assert d2["has_preparatory_anamnesis"] is True
    assert d2["is_primary_consilium_completed"] is False


# ==============================================================================
# TESTS: CONCURRENCY RACE PREVENTION & ATOMIC HARDENING
# ==============================================================================

def test_concurrent_cycle_provisioning_race_prevention(tmp_path):
    """
    Verifies that concurrent requests from multiple threads with independent DB sessions
    calling `MedicalCycleRepository.get_or_create_active_for_patient` for the same patient:
    1. Never create duplicate active cycles in SQLite (atomic BEGIN IMMEDIATE serialization).
    2. Exactly one thread creates the cycle (created=True); all others receive the existing cycle (created=False).
    3. All threads return the identical cycle_id.
    4. SQLite database contains exactly 1 active cycle row for the patient.
    """
    import concurrent.futures
    from sqlalchemy import select

    db_file = tmp_path / "test_cycle_concurrency.db"
    engine = create_engine(
        f"sqlite:///{db_file}",
        connect_args={"timeout": 30},
    )
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)

    patient_id = "pat-concurrent-race-01"
    init_session = SessionLocal()
    PatientRepository(init_session).create(patient_id=patient_id, status="active")
    init_session.commit()
    init_session.close()

    def worker(worker_id: int):
        s = SessionLocal()
        try:
            repo = MedicalCycleRepository(s)
            cycle, created = repo.get_or_create_active_for_patient(patient_id)
            return worker_id, cycle.cycle_id, created
        finally:
            s.close()

    thread_count = 10
    with concurrent.futures.ThreadPoolExecutor(max_workers=thread_count) as executor:
        futures = [executor.submit(worker, i) for i in range(thread_count)]
        results = [f.result() for f in futures]

    engine.dispose()

    verify_session = SessionLocal()
    try:
        active_cycles = verify_session.execute(
            select(MedicalCycle).where(
                MedicalCycle.patient_id == patient_id,
                MedicalCycle.status == "active",
            )
        ).scalars().all()

        assert len(results) == thread_count
        created_count = sum(1 for _, _, created in results if created)
        assert created_count == 1, f"Expected exactly 1 created cycle, got {created_count}"

        cycle_ids = {cycle_id for _, cycle_id, _ in results}
        assert len(cycle_ids) == 1, f"Expected all threads to receive same cycle_id, got {cycle_ids}"

        assert len(active_cycles) == 1, f"Expected exactly 1 active cycle in DB, got {len(active_cycles)}"
        assert active_cycles[0].cycle_id == list(cycle_ids)[0]
        assert active_cycles[0].patient_id == patient_id
    finally:
        verify_session.close()


def test_concurrent_cycle_provisioning_different_patients(tmp_path):
    """
    Verifies that concurrent requests from multiple threads for DIFFERENT patients:
    1. Concurrently provision independent active cycles without deadlocks or lockouts.
    2. Maintain strict patient isolation (zero cross-patient contamination).
    3. Exactly 1 active cycle is created per distinct patient.
    """
    import concurrent.futures
    from sqlalchemy import select

    db_file = tmp_path / "test_cycle_multi_patients.db"
    engine = create_engine(
        f"sqlite:///{db_file}",
        connect_args={"timeout": 30},
    )
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)

    patients = [f"pat-multi-{i}" for i in range(4)]
    init_session = SessionLocal()
    patient_repo = PatientRepository(init_session)
    for p in patients:
        patient_repo.create(patient_id=p, status="active")
    init_session.commit()
    init_session.close()

    tasks = [(worker_id, patients[worker_id % len(patients)]) for worker_id in range(16)]

    def worker(task):
        worker_id, p_id = task
        s = SessionLocal()
        try:
            repo = MedicalCycleRepository(s)
            cycle, created = repo.get_or_create_active_for_patient(p_id)
            return worker_id, p_id, cycle.cycle_id, created
        finally:
            s.close()

    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as executor:
        results = list(executor.map(worker, tasks))

    engine.dispose()

    verify_session = SessionLocal()
    try:
        all_active = verify_session.execute(
            select(MedicalCycle).where(MedicalCycle.status == "active")
        ).scalars().all()

        assert len(all_active) == len(patients)

        for p_id in patients:
            p_cycles = [c for c in all_active if c.patient_id == p_id]
            assert len(p_cycles) == 1, f"Expected 1 cycle for {p_id}, got {len(p_cycles)}"

            p_results = [r for r in results if r[1] == p_id]
            assert len(p_results) == 4
            p_created = sum(1 for r in p_results if r[3])
            assert p_created == 1, f"Expected 1 created flag for {p_id}, got {p_created}"
            p_cycle_ids = {r[2] for r in p_results}
            assert len(p_cycle_ids) == 1
            assert list(p_cycle_ids)[0] == p_cycles[0].cycle_id
    finally:
        verify_session.close()


def test_cycle_provisioning_rollback_on_failure(isolated_client_env):
    """
    Verifies that if an unexpected error occurs during cycle insertion:
    1. Transaction is cleanly rolled back with zero leftover dirty or orphan records.
    2. Subsequent invocation succeeds cleanly without being blocked by prior failed state.
    """
    _, session, _ = isolated_client_env
    PatientRepository(session).create(patient_id="pat-fail-test", status="active")
    session.commit()
    repo = MedicalCycleRepository(session)

    # 1. Provide a cycle_id_factory that raises an error after being called
    def failing_factory():
        raise RuntimeError("Simulated failure during cycle ID generation")

    with pytest.raises(RuntimeError, match="Simulated failure"):
        repo.get_or_create_active_for_patient("pat-fail-test", cycle_id_factory=failing_factory)

    # Verify zero cycles created
    session.expire_all()
    assert repo.get_active_for_patient("pat-fail-test") is None

    # 2. Subsequent normal call succeeds
    cycle, created = repo.get_or_create_active_for_patient("pat-fail-test")
    assert created is True
    assert cycle.patient_id == "pat-fail-test"
    assert cycle.status == "active"


def test_active_session_transaction_concurrency_race_prevention(tmp_path):
    """
    Verifies that when two independent SQLAlchemy sessions/connections already hold
    active transactions (e.g. from prior reads), `get_or_create_active_for_patient`:
    1. Does NOT fail with 'cannot start a transaction within a transaction' or proceed without lock.
    2. Enforces exclusive write serialization (BEGIN IMMEDIATE).
    3. Guarantees exactly one active cycle is created with zero duplicates.
    """
    import concurrent.futures
    from sqlalchemy import select

    db_file = tmp_path / "test_active_tx_race.db"
    engine = create_engine(
        f"sqlite:///{db_file}",
        connect_args={"timeout": 30},
    )
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)

    patient_id = "pat-active-tx-01"
    init_s = SessionLocal()
    PatientRepository(init_s).create(patient_id=patient_id, status="active")
    init_s.commit()
    init_s.close()

    def worker(worker_id: int):
        s = SessionLocal()
        try:
            # Explicitly execute a read to place session into an active transaction
            s.execute(select(MedicalCycle)).scalars().all()
            assert s.in_transaction() is True

            repo = MedicalCycleRepository(s)
            cycle, created = repo.get_or_create_active_for_patient(patient_id)
            return worker_id, cycle.cycle_id, created
        finally:
            s.close()

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        f1 = executor.submit(worker, 1)
        f2 = executor.submit(worker, 2)
        r1 = f1.result()
        r2 = f2.result()

    engine.dispose()

    assert r1[1] == r2[1], f"Expected same cycle_id, got {r1[1]} vs {r2[1]}"
    created_flags = [r1[2], r2[2]]
    assert created_flags.count(True) == 1
    assert created_flags.count(False) == 1

    verify_s = SessionLocal()
    try:
        active_cycles = verify_s.execute(
            select(MedicalCycle).where(
                MedicalCycle.patient_id == patient_id,
                MedicalCycle.status == "active",
            )
        ).scalars().all()
        assert len(active_cycles) == 1
        assert active_cycles[0].cycle_id == r1[1]
    finally:
        verify_s.close()
