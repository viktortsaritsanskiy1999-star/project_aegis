import asyncio
import json
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
from app.models.model_report import ModelReport
from app.repositories.cycle_repository import MedicalCycleRepository
from app.repositories.pairing_repository import PairingRepository
from app.repositories.patient_repository import PatientRepository
from app.repositories.report_repository import ModelReportRepository
from app.routers.client_chat import get_gemini_gateway
from app.schemas.consilium import AegisAgentRole
from app.schemas.privacy import PrivacyLevel
from app.security.tokens import create_access_token, verify_access_token
from app.services.athena_chat_service import AthenaChatService
from app.services.gemini_gateway import GeminiGateway
from app.services.primary_consilium_service import (
    PREPARATORY_ANAMNESIS_REPORT_TYPE,
    RAW_USER_ANAMNESIS_REPORT_TYPE,
)

TEST_JWT_SECRET = "test_jwt_signing_secret_at_least_16_chars_long!"
TEST_PAIRING_SECRET = "test_pairing_secret_deterministic_12345!"
TEST_API_SECRET = "test_device_secret_token_12345"


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


def _sample_routine_athena_reply() -> str:
    return (
        "Привет! Я рада, что мы на связи. Твои показатели записаны, продолжаем следовать плану.\n"
        "До завтра!\n\n"
        "[CLINICAL_REPORT]\n"
        "{\n"
        '  "status": "NORMAL",\n'
        '  "symptoms_mentioned": [],\n'
        '  "subjective_wellbeing_score": 8,\n'
        '  "adherence_to_plan": "HIGH",\n'
        '  "red_flags": false,\n'
        '  "summary_for_master": "Patient adheres well to routine habits."\n'
        "}\n"
        "[/CLINICAL_REPORT]"
    )


@pytest.fixture
def athena_test_env() -> Generator[tuple[TestClient, Session, List[Dict[str, Any]]], None, None]:
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
        system_instruction = ""
        if "systemInstruction" in body:
            parts = body["systemInstruction"].get("parts", [])
            if parts:
                system_instruction = parts[0].get("text", "")
        recorded_ai_calls.append({
            "body": body,
            "contents": contents,
            "system_instruction": system_instruction,
        })
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


def _pair_and_get_token(
    client: TestClient,
    session: Session,
    patient_id: str,
    device_id: str,
    raw_code: str = "123456",
) -> str:
    PatientRepository(session).create(patient_id=patient_id, status="active")
    PairingRepository(session).create_code(patient_id=patient_id, raw_code_override=raw_code)
    session.commit()

    resp = client.post(
        "/api/v1/auth/pair",
        json={
            "pairing_code": raw_code,
            "device_name": "Test Device",
            "platform": "ios",
            "device_id": device_id,
        },
    )
    assert resp.status_code == 200
    token = resp.json()["access_token"]
    fam_id = verify_access_token(token).family_id
    return create_access_token(
        patient_id=patient_id,
        device_id=device_id,
        family_id=fam_id,
        scope="chat:read chat:write cycle:read vitals:read",
    ).access_token


# ==============================================================================
# 1. ATHENA CONTEXT INCLUDES AUTHENTICATED PATIENT MEDICAL DATA
# ==============================================================================

def test_athena_context_includes_authenticated_patient_medical_data(athena_test_env):
    """
    Verifies that Athena's system instruction contains the active patient's:
    - Anamnesis summary and clinical facts
    - Available laboratory findings (active markers, values, units, reference ranges)
    - Available ECG findings (summary, rhythm, heart rate)
    - Published client-facing Primary Consilium conclusions (title, summary, highlights, vitals limits)
    - Published system conclusions
    - Active Chief Physician strategy
    """
    client, session, recorded_ai = athena_test_env
    patient_id = "pat_med_ctx_01"
    device_id = "dev_med_ctx_01"
    token = _pair_and_get_token(client, session, patient_id=patient_id, device_id=device_id)

    cycle_repo = MedicalCycleRepository(session)
    cycle, _ = cycle_repo.get_or_create_active_for_patient(patient_id)
    cycle_id = cycle.cycle_id

    report_repo = ModelReportRepository(session)

    # 1. Seed Anamnesis M1
    report_repo.create_next_version(
        cycle_id=cycle_id,
        model="gemini-2.5-flash",
        report_type=PREPARATORY_ANAMNESIS_REPORT_TYPE,
        privacy_level=PrivacyLevel.SERVER_ONLY,
        content={
            "agent_role": AegisAgentRole.ANAMNESIS_M1.value,
            "user_view": {"summary": "35-year-old male with mild exercise-induced asthma."},
            "system_logic": {
                "cvs": {"data": "Normal resting heart rate 68 bpm, BP 120/80.", "internal_comment": "No CVD."},
            },
            "training_sandbox": {"notes": "M1 validated."},
        },
    )

    # 2. Seed Laboratory Ledger
    report_repo.create_next_version(
        cycle_id=cycle_id,
        model="gemini-2.5-flash",
        report_type="laboratory_ledger",
        privacy_level=PrivacyLevel.SERVER_ONLY,
        content={
            "ledger_version": 1,
            "shared_labs_table": {
                "glucose": {
                    "display_name": "Blood Glucose",
                    "value": "5.4",
                    "numeric_value": 5.4,
                    "unit": "mmol/L",
                    "reference_range": {"low": 3.9, "high": 6.1, "text": "3.9 - 6.1"},
                    "sample_date": "2026-10-25",
                },
                "total_cholesterol": {
                    "display_name": "Total Cholesterol",
                    "value": "4.6",
                    "numeric_value": 4.6,
                    "unit": "mmol/L",
                    "reference_range": {"low": 3.0, "high": 5.2, "text": "3.0 - 5.2"},
                    "sample_date": "2026-10-25",
                },
            },
        },
    )

    # 3. Seed ECG
    report_repo.create_next_version(
        cycle_id=cycle_id,
        model="gemini-2.5-flash",
        report_type="preparatory_vision_ecg",
        privacy_level=PrivacyLevel.SERVER_ONLY,
        content={
            "agent_role": AegisAgentRole.VISION_ECG.value,
            "is_conducted": True,
            "ecg_summary": "Sinus rhythm, 72 bpm, normal electrical axis.",
            "heart_rate_bpm": 72,
            "rhythm_type": "Sinus",
            "rhythm_regularity": "Regular",
            "record_date": "2026-10-26",
        },
    )

    # 4. Seed Primary Consilium (client-facing + internal strategy)
    report_repo.create_next_version(
        cycle_id=cycle_id,
        model="gemini-2.5-pro",
        report_type="user_overall_report",
        privacy_level=PrivacyLevel.CLIENT_FACING,
        content={
            "title": "Primary Consilium Health Roadmap",
            "summary": "Overall good functional reserve. Maintain regular aerobic activity.",
            "highlights": ["Hydration > 2L daily", "Target resting HR under 75 bpm"],
            "metrics": {
                "min_pessimum_bpm": 48,
                "max_pessimum_bpm": 115,
                "max_spike_delta_bpm": 30,
            },
        },
    )
    report_repo.create_next_version(
        cycle_id=cycle_id,
        model="gemini-2.5-flash",
        report_type="system_user_report_cvs",
        privacy_level=PrivacyLevel.CLIENT_FACING,
        content={
            "title": "Cardiovascular Overview",
            "summary": "Heart metrics show robust adaptation.",
            "system_key": "cvs",
        },
    )
    report_repo.create_next_version(
        cycle_id=cycle_id,
        model="gemini-2.5-pro",
        report_type="chief_physician_internal",
        privacy_level=PrivacyLevel.SERVER_ONLY,
        content={
            "agent_role": AegisAgentRole.CHIEF_PHYSICIAN.value,
            "pro_general_shelf": "ATHENA_STRATEGY_MAINTAIN_ZONE_2_CARDIO",
            "pro_training_shelf": "INTERNAL_CLINICAL_BACKGROUND",
            "vitals_thresholds": {
                "min_pessimum_bpm": 48,
                "max_pessimum_bpm": 115,
                "max_spike_delta_bpm": 30,
            },
        },
    )
    report_repo.create_next_version(
        cycle_id=cycle_id,
        model="gemini-2.5-flash",
        report_type="deputy_chief_physician_internal",
        privacy_level=PrivacyLevel.SERVER_ONLY,
        content={
            "agent_role": AegisAgentRole.DEPUTY_CHIEF_PHYSICIAN.value,
            "flash_general_shelf": "DEPUTY_PRIMARY_SYNTHESIS",
            "flash_training_shelf": "INTERNAL_FLASH_DIAGNOSTICS",
        },
    )
    session.commit()

    # Send Athena turn
    resp = client.post(
        "/api/v1/client/chat/athena",
        json={"message": "Как мои дела сегодня?"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["cycle_id"] == cycle_id
    assert "Привет!" in data["reply"]

    # Verify Gemini system instruction captured all medical context
    assert len(recorded_ai) == 1
    sys_instruction = recorded_ai[0]["system_instruction"]

    # Anamnesis
    assert "35-year-old male with mild exercise-induced asthma" in sys_instruction
    assert "Normal resting heart rate 68 bpm" in sys_instruction

    # Laboratory
    assert "Blood Glucose: 5.4 mmol/L" in sys_instruction
    assert "Total Cholesterol: 4.6 mmol/L" in sys_instruction

    # ECG
    assert "Sinus rhythm, 72 bpm" in sys_instruction
    assert "Heart rate: 72 bpm" in sys_instruction

    # Published Primary Consilium
    assert "Primary Consilium Health Roadmap" in sys_instruction
    assert "Overall good functional reserve" in sys_instruction
    assert "Cardiovascular Overview" in sys_instruction
    assert "Hydration > 2L daily" in sys_instruction

    # Chief Physician Route
    assert "ATHENA_STRATEGY_MAINTAIN_ZONE_2_CARDIO" in sys_instruction


# ==============================================================================
# 2. ZERO CYCLE MIXING: NEW CYCLE DOES NOT INHERIT STALE DATA
# ==============================================================================

def test_athena_zero_cycle_mixing_when_new_cycle_started(athena_test_env):
    """
    Verifies that when a patient completes Cycle 1 and transitions to Cycle 2,
    Athena operates strictly in Cycle 2 without inheriting or mixing Cycle 1's
    conclusions or lab findings.
    """
    client, session, recorded_ai = athena_test_env
    patient_id = "pat_cycle_mix_01"
    device_id = "dev_cycle_mix_01"
    token = _pair_and_get_token(client, session, patient_id=patient_id, device_id=device_id)

    cycle_repo = MedicalCycleRepository(session)
    report_repo = ModelReportRepository(session)

    # Provision Cycle 1
    cycle_1, _ = cycle_repo.get_or_create_active_for_patient(patient_id)
    cycle_1_id = cycle_1.cycle_id

    # Seed Cycle 1 with distinct reports
    report_repo.create_next_version(
        cycle_id=cycle_1_id,
        model="gemini-2.5-pro",
        report_type="user_overall_report",
        privacy_level=PrivacyLevel.CLIENT_FACING,
        content={
            "title": "CYCLE_1_EXCLUSIVE_CONCLUSION",
            "summary": "Cycle 1 complete review.",
        },
    )
    report_repo.create_next_version(
        cycle_id=cycle_1_id,
        model="gemini-2.5-flash",
        report_type="laboratory_ledger",
        privacy_level=PrivacyLevel.SERVER_ONLY,
        content={
            "ledger_version": 1,
            "shared_labs_table": {
                "glucose": {"display_name": "CYCLE_1_GLUCOSE_99", "value": "9.9"},
            },
        },
    )

    # Mark Cycle 1 as completed and start active Cycle 2
    cycle_1.status = "completed"
    session.commit()

    cycle_2, created = cycle_repo.get_or_create_active_for_patient(patient_id)
    assert created is True
    cycle_2_id = cycle_2.cycle_id

    # Chat turn in active Cycle 2
    resp = client.post(
        "/api/v1/client/chat/athena",
        json={"message": "Начинаем новый цикл!"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    assert resp.json()["cycle_id"] == cycle_2_id

    sys_instruction = recorded_ai[0]["system_instruction"]

    # Verify Cycle 1 exclusive strings are 100% ABSENT in Cycle 2 context
    assert "CYCLE_1_EXCLUSIVE_CONCLUSION" not in sys_instruction
    assert "CYCLE_1_GLUCOSE_99" not in sys_instruction

    # Verify Cycle 2 accurately reports absence of studies and pending consilium
    assert "Status: No laboratory studies recorded for this cycle." in sys_instruction
    assert "Status: Primary Consilium has not been executed yet for this cycle." in sys_instruction
    assert "Patient is in initial onboarding (Primary Consilium not yet executed)." in sys_instruction


# ==============================================================================
# 3. PATIENT ISOLATION: ZERO CROSS-PATIENT LEAKAGE
# ==============================================================================

def test_athena_patient_isolation_zero_cross_patient_leakage(athena_test_env):
    """
    Verifies that Athena strictly segregates patient data:
    Patient B's chat context NEVER sees Patient A's confidential anamnesis or labs.
    """
    client, session, recorded_ai = athena_test_env
    token_a = _pair_and_get_token(client, session, "patient_A", "device_A", "111111")
    token_b = _pair_and_get_token(client, session, "patient_B", "device_B", "222222")

    cycle_repo = MedicalCycleRepository(session)
    report_repo = ModelReportRepository(session)

    cycle_a, _ = cycle_repo.get_or_create_active_for_patient("patient_A")
    cycle_b, _ = cycle_repo.get_or_create_active_for_patient("patient_B")

    # Seed Patient A confidential anamnesis and lab
    report_repo.create_next_version(
        cycle_id=cycle_a.cycle_id,
        model="gemini-2.5-flash",
        report_type=RAW_USER_ANAMNESIS_REPORT_TYPE,
        privacy_level=PrivacyLevel.SERVER_ONLY,
        content={
            "answers": {
                "age": 62,
                "chronic_diseases": "PATIENT_A_SECRET_CARDIAC_SYMPTOM",
            }
        },
    )
    report_repo.create_next_version(
        cycle_id=cycle_a.cycle_id,
        model="gemini-2.5-flash",
        report_type="laboratory_ledger",
        privacy_level=PrivacyLevel.SERVER_ONLY,
        content={
            "ledger_version": 1,
            "shared_labs_table": {
                "potassium": {"display_name": "PATIENT_A_SECRET_POTASSIUM", "value": "3.1"},
            },
        },
    )

    # Seed Patient B distinct anamnesis
    report_repo.create_next_version(
        cycle_id=cycle_b.cycle_id,
        model="gemini-2.5-flash",
        report_type=RAW_USER_ANAMNESIS_REPORT_TYPE,
        privacy_level=PrivacyLevel.SERVER_ONLY,
        content={
            "answers": {
                "age": 28,
                "chronic_diseases": "PATIENT_B_HEALTHY_STATUS",
            }
        },
    )
    session.commit()

    # Patient B executes chat turn
    resp_b = client.post(
        "/api/v1/client/chat/athena",
        json={"message": "Добрый день, Афина!"},
        headers={"Authorization": f"Bearer {token_b}"},
    )
    assert resp_b.status_code == 200
    assert resp_b.json()["cycle_id"] == cycle_b.cycle_id

    sys_b = recorded_ai[0]["system_instruction"]

    # Verify Patient B's data is present
    assert "PATIENT_B_HEALTHY_STATUS" in sys_b
    assert "Age: 28" in sys_b

    # Verify ZERO leakage of Patient A's confidential medical data
    assert "PATIENT_A_SECRET_CARDIAC_SYMPTOM" not in sys_b
    assert "PATIENT_A_SECRET_POTASSIUM" not in sys_b
    assert "62" not in sys_b


# ==============================================================================
# 4. COMPLETED VS INCOMPLETE PRIMARY CONSILIUM HANDLING
# ==============================================================================

def test_athena_distinguishes_completed_vs_incomplete_primary_consilium(athena_test_env):
    """
    Verifies that Athena explicitly distinguishes between an incomplete Primary
    Consilium (onboarding route + pending status) and a completed one (full
    published conclusions + Chief Physician strategy).
    """
    client, session, recorded_ai = athena_test_env
    patient_id = "pat_consilium_status"
    device_id = "dev_consilium_status"
    token = _pair_and_get_token(client, session, patient_id=patient_id, device_id=device_id)

    cycle_repo = MedicalCycleRepository(session)
    report_repo = ModelReportRepository(session)
    cycle, _ = cycle_repo.get_or_create_active_for_patient(patient_id)
    cycle_id = cycle.cycle_id

    # Step 1: Incomplete Consilium
    report_repo.create_next_version(
        cycle_id=cycle_id,
        model="gemini-2.5-flash",
        report_type=RAW_USER_ANAMNESIS_REPORT_TYPE,
        privacy_level=PrivacyLevel.SERVER_ONLY,
        content={"answers": {"age": 40, "gender": "Female"}},
    )
    session.commit()

    resp_incomplete = client.post(
        "/api/v1/client/chat/athena",
        json={"message": "Я заполнила анкету."},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp_incomplete.status_code == 200
    sys_inc = recorded_ai[0]["system_instruction"]
    assert "Status: Primary Consilium has not been executed yet for this cycle." in sys_inc
    assert "Patient is in initial onboarding (Primary Consilium not yet executed)." in sys_inc

    # Step 2: Complete Primary Consilium
    report_repo.create_next_version(
        cycle_id=cycle_id,
        model="gemini-2.5-pro",
        report_type="user_overall_report",
        privacy_level=PrivacyLevel.CLIENT_FACING,
        content={
            "title": "Comprehensive Health Plan",
            "summary": "Cardiovascular stability confirmed.",
            "highlights": ["Daily walking", "Sleep at 23:00"],
            "metrics": {"min_pessimum_bpm": 50, "max_pessimum_bpm": 120, "max_spike_delta_bpm": 25},
        },
    )
    report_repo.create_next_version(
        cycle_id=cycle_id,
        model="gemini-2.5-pro",
        report_type="chief_physician_internal",
        privacy_level=PrivacyLevel.SERVER_ONLY,
        content={
            "agent_role": AegisAgentRole.CHIEF_PHYSICIAN.value,
            "pro_general_shelf": "CHIEF_STRATEGY_ACTIVE_ROUTE",
            "pro_training_shelf": "TRAINING_BACKGROUND",
            "vitals_thresholds": {"min_pessimum_bpm": 50, "max_pessimum_bpm": 120, "max_spike_delta_bpm": 25},
        },
    )
    report_repo.create_next_version(
        cycle_id=cycle_id,
        model="gemini-2.5-flash",
        report_type="deputy_chief_physician_internal",
        privacy_level=PrivacyLevel.SERVER_ONLY,
        content={
            "agent_role": AegisAgentRole.DEPUTY_CHIEF_PHYSICIAN.value,
            "flash_general_shelf": "FLASH_SYNTHESIS",
            "flash_training_shelf": "FLASH_DIAGNOSTICS",
        },
    )
    session.commit()

    resp_complete = client.post(
        "/api/v1/client/chat/athena",
        json={"message": "Консилиум завершился, что дальше?"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp_complete.status_code == 200
    sys_comp = recorded_ai[1]["system_instruction"]
    assert "Overall conclusion (Comprehensive Health Plan): Cardiovascular stability confirmed." in sys_comp
    assert "CHIEF_STRATEGY_ACTIVE_ROUTE" in sys_comp
    assert "Status: Primary Consilium has not been executed yet" not in sys_comp


# ==============================================================================
# 5. ABSENCE OF STUDIES: EXPLICIT DISTINCTION FROM CONDUCTED STUDIES
# ==============================================================================

def test_athena_handles_absence_of_all_studies_gracefully(athena_test_env):
    """
    Verifies that when a patient has not uploaded laboratory tests or recorded an ECG,
    Athena's context explicitly notes their absence rather than failing or inventing data.
    """
    client, session, recorded_ai = athena_test_env
    patient_id = "pat_no_studies"
    device_id = "dev_no_studies"
    token = _pair_and_get_token(client, session, patient_id=patient_id, device_id=device_id)

    cycle_repo = MedicalCycleRepository(session)
    cycle, _ = cycle_repo.get_or_create_active_for_patient(patient_id)

    resp = client.post(
        "/api/v1/client/chat/athena",
        json={"message": "Я ещё не сдавал анализы и не делал ЭКГ."},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    sys = recorded_ai[0]["system_instruction"]

    assert "Status: Not completed (questionnaire pending)." in sys
    assert "Status: No laboratory studies recorded for this cycle." in sys
    assert "Status: Not conducted (no ECG recorded for this cycle)." in sys
    assert "Status: Primary Consilium has not been executed yet for this cycle." in sys


# ==============================================================================
# 6. DEFENSE AGAINST SERVER_ONLY DISCLOSURE IN CLIENT REPLY
# ==============================================================================

def test_athena_defense_against_server_only_disclosure_in_client_reply(athena_test_env):
    """
    Verifies that even if the AI model output includes [CLINICAL_REPORT],
    server_only hypotheses, internal tags, or raw headers, the client response
    is 100% sanitized and contains only clean conversational text.
    """
    client, session, _ = athena_test_env
    patient_id = "pat_sanitization"
    device_id = "dev_sanitization"
    token = _pair_and_get_token(client, session, patient_id=patient_id, device_id=device_id)

    # Injected raw model response containing sensitive internal scoring and headers
    rogue_model_output = (
        "=== AUTHORITATIVE PATIENT MEDICAL CONTEXT (ACTIVE CYCLE) ===\n"
        "Привет! Я внимательно изучила твои показатели. Давай сосредоточимся на прогулках сегодня.\n\n"
        "[CLINICAL_REPORT]\n"
        "{\n"
        '  "status": "ALERT",\n'
        '  "symptoms_mentioned": ["suspected_myocardial_hypoxia"],\n'
        '  "subjective_wellbeing_score": 4,\n'
        '  "adherence_to_plan": "MEDIUM",\n'
        '  "red_flags": false,\n'
        '  "summary_for_master": "CONFIDENTIAL_SERVER_ONLY_SCORE_888"\n'
        "}\n"
        "[/CLINICAL_REPORT]"
    )

    mock_gateway = GeminiGateway(
        api_key="test-key",
        transport=httpx.MockTransport(
            lambda req: httpx.Response(200, json=_gemini_envelope(rogue_model_output))
        ),
        sleep_func=lambda _: asyncio.sleep(0),
    )
    app.dependency_overrides[get_gemini_gateway] = lambda: mock_gateway

    resp = client.post(
        "/api/v1/client/chat/athena",
        json={"message": "Как моё здоровье?"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    reply = resp.json()["reply"]

    # Verify clean conversational text is present
    assert "Привет! Я внимательно изучила твои показатели. Давай сосредоточимся на прогулках сегодня." in reply

    # Verify zero leakage of server_only hypotheses, scores, or prompt tags
    assert "[CLINICAL_REPORT]" not in reply
    assert "[/CLINICAL_REPORT]" not in reply
    assert "suspected_myocardial_hypoxia" not in reply
    assert "CONFIDENTIAL_SERVER_ONLY_SCORE_888" not in reply
    assert "summary_for_master" not in reply
    assert "=== AUTHORITATIVE PATIENT MEDICAL CONTEXT" not in reply

    # Verify hidden clinical log is safely persisted in ModelReportRepository as server_only
    cycle_repo = MedicalCycleRepository(session)
    cycle = cycle_repo.get_active_for_patient(patient_id)
    report_repo = ModelReportRepository(session)
    clinical_logs = report_repo.list_server_only(cycle_id=cycle.cycle_id, report_type="athena_clinical_log")
    assert len(clinical_logs) >= 1
    latest_log = clinical_logs[0].content
    assert latest_log["summary_for_master"] == "CONFIDENTIAL_SERVER_ONLY_SCORE_888"


# ==============================================================================
# 7. IDEMPOTENT RETRY: ZERO RE-GENERATION
# ==============================================================================

def test_athena_idempotent_retry_prevents_duplicate_generation(athena_test_env):
    """
    Verifies that repeating a turn submission with the same idempotency_key
    returns the cached turn without invoking Gemini a second time, and without
    duplicating dialogue history turns.
    """
    client, session, recorded_ai = athena_test_env
    patient_id = "pat_idempotency_retry"
    device_id = "dev_idempotency_retry"
    token = _pair_and_get_token(client, session, patient_id=patient_id, device_id=device_id)

    idempotency_key = "idem_key_proof_001"

    # Call 1: fresh submission
    resp1 = client.post(
        "/api/v1/client/chat/athena",
        json={
            "message": "Первая отправка сообщения",
            "idempotency_key": idempotency_key,
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp1.status_code == 200
    assert len(recorded_ai) == 1
    reply1 = resp1.json()["reply"]
    msg_id1 = resp1.json()["message_id"]

    # Call 2: retry with identical idempotency_key
    resp2 = client.post(
        "/api/v1/client/chat/athena",
        json={
            "message": "Первая отправка сообщения",
            "idempotency_key": idempotency_key,
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp2.status_code == 200
    # ZERO additional Gemini calls made
    assert len(recorded_ai) == 1
    assert resp2.json()["reply"] == reply1
    assert resp2.json()["message_id"] == msg_id1

    # Verify history contains exactly 2 messages (1 user, 1 curator), not 4
    hist_resp = client.get(
        "/api/v1/client/chat/athena/history",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert hist_resp.status_code == 200
    messages = hist_resp.json()["messages"]
    assert len(messages) == 2
    assert messages[0]["sender"] == "user"
    assert messages[0]["text"] == "Первая отправка сообщения"
    assert messages[1]["sender"] == "curator"


# ==============================================================================
# 8. TARGETED TEST: METRICS ABSENT
# ==============================================================================

def test_athena_context_when_metrics_absent(athena_test_env):
    """
    Verifies that when published overall report has no 'metrics' (or empty dict),
    Athena's context explicitly notes that individual thresholds are unavailable,
    without substituting default values 45, 110, 30.
    """
    client, session, recorded_ai = athena_test_env
    patient_id = "pat_no_metrics"
    device_id = "dev_no_metrics"
    token = _pair_and_get_token(client, session, patient_id=patient_id, device_id=device_id)

    cycle_repo = MedicalCycleRepository(session)
    cycle, _ = cycle_repo.get_or_create_active_for_patient(patient_id)
    report_repo = ModelReportRepository(session)

    # Published overall report WITHOUT metrics key
    report_repo.create_next_version(
        cycle_id=cycle.cycle_id,
        model="gemini-2.5-pro",
        report_type="user_overall_report",
        privacy_level=PrivacyLevel.CLIENT_FACING,
        content={
            "title": "Roadmap Without Metrics",
            "summary": "Baseline roadmap established.",
            # "metrics" is explicitly omitted
        },
    )
    report_repo.create_next_version(
        cycle_id=cycle.cycle_id,
        model="gemini-2.5-pro",
        report_type="chief_physician_internal",
        privacy_level=PrivacyLevel.SERVER_ONLY,
        content={
            "pro_general_shelf": "ROUTINE_STRATEGY",
            "pro_training_shelf": "INTERNAL_CLINICAL_BACKGROUND",
        },
    )
    report_repo.create_next_version(
        cycle_id=cycle.cycle_id,
        model="gemini-2.5-flash",
        report_type="deputy_chief_physician_internal",
        privacy_level=PrivacyLevel.SERVER_ONLY,
        content={"flash_general_shelf": "FLASH_ROUTINE"},
    )
    session.commit()

    resp = client.post(
        "/api/v1/client/chat/athena",
        json={"message": "Каковы мои пороги пульса?"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    sys_instruction = recorded_ai[0]["system_instruction"]

    assert "- Monitored thresholds: Individual vitals thresholds unavailable." in sys_instruction
    assert "45" not in sys_instruction
    assert "110" not in sys_instruction
    assert "30" not in sys_instruction


# ==============================================================================
# 9. TARGETED TEST: METRICS PRESENT FULL
# ==============================================================================

def test_athena_context_when_metrics_present_full(athena_test_env):
    """
    Verifies that when published overall report has a full metrics set,
    Athena's context transmits the exact personalized values (e.g. 52, 118, 28)
    without alteration.
    """
    client, session, recorded_ai = athena_test_env
    patient_id = "pat_full_metrics"
    device_id = "dev_full_metrics"
    token = _pair_and_get_token(client, session, patient_id=patient_id, device_id=device_id)

    cycle_repo = MedicalCycleRepository(session)
    cycle, _ = cycle_repo.get_or_create_active_for_patient(patient_id)
    report_repo = ModelReportRepository(session)

    report_repo.create_next_version(
        cycle_id=cycle.cycle_id,
        model="gemini-2.5-pro",
        report_type="user_overall_report",
        privacy_level=PrivacyLevel.CLIENT_FACING,
        content={
            "title": "Personalized Roadmap",
            "summary": "Tuned parameters.",
            "metrics": {
                "min_pessimum_bpm": 52,
                "max_pessimum_bpm": 118,
                "max_spike_delta_bpm": 28,
            },
        },
    )
    report_repo.create_next_version(
        cycle_id=cycle.cycle_id,
        model="gemini-2.5-pro",
        report_type="chief_physician_internal",
        privacy_level=PrivacyLevel.SERVER_ONLY,
        content={
            "pro_general_shelf": "TUNED_STRATEGY",
            "pro_training_shelf": "INTERNAL_CLINICAL_BACKGROUND",
        },
    )
    report_repo.create_next_version(
        cycle_id=cycle.cycle_id,
        model="gemini-2.5-flash",
        report_type="deputy_chief_physician_internal",
        privacy_level=PrivacyLevel.SERVER_ONLY,
        content={"flash_general_shelf": "FLASH_TUNED"},
    )
    session.commit()

    resp = client.post(
        "/api/v1/client/chat/athena",
        json={"message": "Проверь мои пороги."},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    sys_instruction = recorded_ai[0]["system_instruction"]

    assert "- Monitored thresholds: min 52 bpm, max 118 bpm, max spike delta 28 bpm." in sys_instruction
    assert "45" not in sys_instruction
    assert "110" not in sys_instruction


# ==============================================================================
# 10. TARGETED TEST: INCOMPLETE METRICS SET
# ==============================================================================

def test_athena_context_when_metrics_incomplete(athena_test_env):
    """
    Verifies that when metrics contains only a partial subset (e.g. only min_pessimum_bpm),
    Athena's context transmits ONLY the present metric and does NOT invent or substitute
    defaults for the missing ones.
    """
    client, session, recorded_ai = athena_test_env
    patient_id = "pat_partial_metrics"
    device_id = "dev_partial_metrics"
    token = _pair_and_get_token(client, session, patient_id=patient_id, device_id=device_id)

    cycle_repo = MedicalCycleRepository(session)
    cycle, _ = cycle_repo.get_or_create_active_for_patient(patient_id)
    report_repo = ModelReportRepository(session)

    report_repo.create_next_version(
        cycle_id=cycle.cycle_id,
        model="gemini-2.5-pro",
        report_type="user_overall_report",
        privacy_level=PrivacyLevel.CLIENT_FACING,
        content={
            "title": "Partial Metrics Roadmap",
            "summary": "Resting pulse only.",
            "metrics": {
                "min_pessimum_bpm": 55,
                "max_pessimum_bpm": None,
                "max_spike_delta_bpm": None,
            },
        },
    )
    report_repo.create_next_version(
        cycle_id=cycle.cycle_id,
        model="gemini-2.5-pro",
        report_type="chief_physician_internal",
        privacy_level=PrivacyLevel.SERVER_ONLY,
        content={
            "pro_general_shelf": "PARTIAL_STRATEGY",
            "pro_training_shelf": "INTERNAL_CLINICAL_BACKGROUND",
        },
    )
    report_repo.create_next_version(
        cycle_id=cycle.cycle_id,
        model="gemini-2.5-flash",
        report_type="deputy_chief_physician_internal",
        privacy_level=PrivacyLevel.SERVER_ONLY,
        content={"flash_general_shelf": "FLASH_PARTIAL"},
    )
    session.commit()

    resp = client.post(
        "/api/v1/client/chat/athena",
        json={"message": "Какой у меня минимальный пульс?"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    sys_instruction = recorded_ai[0]["system_instruction"]

    assert "- Monitored thresholds: min 55 bpm." in sys_instruction
    assert "max 110 bpm" not in sys_instruction
    assert "max spike delta 30 bpm" not in sys_instruction


# ==============================================================================
# 11. TARGETED TEST: MODEL REPLY WITH INTERNAL DATA WITHOUT SERVICE HEADERS
# ==============================================================================

def test_athena_model_reply_with_internal_data_without_service_headers(athena_test_env):
    """
    Verifies sanitization of model response when it contains internal clinical data
    and structured [CLINICAL_REPORT] WITHOUT any service section headers like '=== ... ==='.
    Guarantees that:
    1. Conversational text is preserved.
    2. Internal diagnostic hypothesis and syndrome tags are completely purged from client reply.
    3. The clinical report is recorded in DB strictly as server_only.
    """
    client, session, _ = athena_test_env
    patient_id = "pat_no_headers_leak"
    device_id = "dev_no_headers_leak"
    token = _pair_and_get_token(client, session, patient_id=patient_id, device_id=device_id)

    # Raw model output without any section headers (no === banners)
    raw_model_reply = (
        "Привет! Я ознакомилась с твоим отчетом. Чувствуй себя спокойно и пей воду.\n\n"
        "[CLINICAL_REPORT]\n"
        "{\n"
        '  "status": "ALERT",\n'
        '  "symptoms_mentioned": ["paroxysmal_tachycardia", "hidden_ischemia"],\n'
        '  "subjective_wellbeing_score": 3,\n'
        '  "adherence_to_plan": "LOW",\n'
        '  "red_flags": true,\n'
        '  "summary_for_master": "SECRET_DIAGNOSTIC_EVALUATION_SYNDROME_42"\n'
        "}\n"
        "[/CLINICAL_REPORT]"
    )

    mock_gateway = GeminiGateway(
        api_key="test-key",
        transport=httpx.MockTransport(
            lambda req: httpx.Response(200, json=_gemini_envelope(raw_model_reply))
        ),
        sleep_func=lambda _: asyncio.sleep(0),
    )
    app.dependency_overrides[get_gemini_gateway] = lambda: mock_gateway

    resp = client.post(
        "/api/v1/client/chat/athena",
        json={"message": "Как мои результаты?"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    reply = resp.json()["reply"]

    # Verify conversational text preserved
    assert "Привет! Я ознакомилась с твоим отчетом. Чувствуй себя спокойно и пей воду." in reply

    # Verify real internal clinical data is strictly purged from client reply
    assert "[CLINICAL_REPORT]" not in reply
    assert "[/CLINICAL_REPORT]" not in reply
    assert "paroxysmal_tachycardia" not in reply
    assert "hidden_ischemia" not in reply
    assert "SECRET_DIAGNOSTIC_EVALUATION_SYNDROME_42" not in reply
    assert "summary_for_master" not in reply

    # Verify the clinical report was safely persisted as server_only
    cycle_repo = MedicalCycleRepository(session)
    cycle = cycle_repo.get_active_for_patient(patient_id)
    report_repo = ModelReportRepository(session)
    clinical_logs = report_repo.list_server_only(cycle_id=cycle.cycle_id, report_type="athena_clinical_log")
    assert len(clinical_logs) >= 1
    assert clinical_logs[0].content["summary_for_master"] == "SECRET_DIAGNOSTIC_EVALUATION_SYNDROME_42"

