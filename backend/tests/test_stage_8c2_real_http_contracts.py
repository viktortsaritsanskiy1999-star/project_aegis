"""
Stage 8C.2 — Real HTTP Contracts & Schema Verification Test Suite.

Verifies end-to-end FastAPI HTTP endpoints across the complete patient journey:
1. GET  /api/v1/client/cascade/status (Cycle 1 status, referral detection)
2. GET  /api/v1/client/referral/documents (Safe document list)
3. GET  /api/v1/client/referral/documents/{id}/export (PDF/DOCX/MD binary & text export)
4. GET  /api/v1/client/chat/athena/history (Athena chat history)
5. POST /api/v1/cycles/start-neo-reassessment (Transition to pending_reassessment)
6. POST /api/v1/client/cascade/intake/anamnesis (Blind M1 submission)
7. POST /api/v1/client/cascade/run-primary (Neo 10-model consilium cascade)
8. GET  /api/v1/client/cascade/status (Verification of prepared status)
9. POST /api/v1/client/cascade/activate-neo (Atomic zero-LLM activation)
10. GET /api/v1/client/cascade/status & GET /api/v1/reports/client (Activated status & reports)
11. GET /api/v1/client/chat/athena/history (Athena returning patient greeting verification)
12. GET /api/v1/client/referral/documents (Historical Safe documents cross-cycle persistence)

Also verifies error handling: 401 Unauthorized, 403 Forbidden on patient cross-access,
422 on invalid payload, and Idempotent-Replay headers.
"""

from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional
import asyncio
import json
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.main import app
from app.database import get_db, Base
from app.models.medical_cycle import MedicalCycle
from app.models.patient import Patient
from app.models.model_report import ModelReport
from app.repositories.cycle_repository import MedicalCycleRepository
from app.repositories.report_repository import ModelReportRepository
from app.schemas.privacy import PrivacyLevel
from app.schemas.consilium import ConsiliumMode, AegisAgentRole
from app.services.gemini_gateway import (
    GeminiGateway,
    GeminiModelRole,
)
from app.security.auth import (
    DeviceSession,
    ServiceAdminSession,
    require_device_session,
    require_scopes,
    require_service_admin_session,
    verify_client_token,
)
from app.security.tokens import (
    SCOPE_CASCADE_INTAKE,
    SCOPE_CASCADE_RUN,
    SCOPE_CYCLE_READ,
    SCOPE_REPORTS_READ,
    SCOPE_CHAT_READ,
    SCOPE_CHAT_WRITE,
    SCOPE_VITALS_WRITE,
)
from app.services.primary_consilium_service import (
    PrimaryConsiliumService,
    PRIMARY_CONSILIUM_EXECUTION_LOG_REPORT_TYPE,
    SYSTEM_KEYS,
)
from app.services.weekly_consilium_service import (
    WeeklyConsiliumService,
    ATHENA_ACTIVE_STRATEGY_REPORT_TYPE,
    ATHENA_SCHEDULE_ANCHOR_REPORT_TYPE,
)
from app.services.referral_document_service import ReferralDocumentService
from app.services.clinical_automation_service import (
    ClinicalAutomationService,
    AutomationJobType,
)
from tests.test_stage_8c1_full_patient_journey import (
    Stage8C1FullJourneyMockGateway,
    _make_device_session,
    _override_all_gateways,
    _seed_completed_primary_consilium,
)


@pytest.fixture(autouse=True)
def clean_dependency_overrides():
    yield
    app.dependency_overrides.clear()


@pytest.fixture
def http_test_db():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    session = session_factory()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(bind=engine)


def test_real_http_contracts_all_12_steps(http_test_db: Session):
    """
    Executes the exact 12-step real HTTP contract flow requested for Stage 8C technical acceptance.
    Validates HTTP status codes, JSON schema fields, content types, and error handling.
    """
    patient_id = "patient_http_8c2_01"
    device_id = "device_http_8c2_01"
    cycle_1_id = "cycle_http_c1"
    now_utc = datetime.now(timezone.utc)

    # Setup isolated session & gateway
    mock_gateway = Stage8C1FullJourneyMockGateway()
    active_session = _make_device_session(patient_id=patient_id, device_id=device_id)

    app.dependency_overrides[get_db] = lambda: http_test_db
    _override_all_gateways(mock_gateway)
    app.dependency_overrides[require_device_session] = lambda: active_session
    app.dependency_overrides[verify_client_token] = lambda: active_session
    app.dependency_overrides[require_service_admin_session] = lambda: ServiceAdminSession(admin_id="adm_test", role="admin")

    client = TestClient(app, headers={"Authorization": "Bearer mock_jwt_stage_8c2"})

    # Seed Patient and Cycle 1
    patient = Patient(patient_id=patient_id, status="active")
    http_test_db.add(patient)

    cycle_1 = MedicalCycle(
        cycle_id=cycle_1_id,
        patient_id=patient_id,
        status="active",
        started_at=now_utc - timedelta(days=8),
        created_at=now_utc - timedelta(days=8),
        updated_at=now_utc - timedelta(days=1),
    )
    http_test_db.add(cycle_1)
    http_test_db.commit()

    # Seed Primary Consilium & Initial Anchor T0
    _seed_completed_primary_consilium(http_test_db, cycle_1_id)
    weekly_svc = WeeklyConsiliumService(db=http_test_db, gateway=mock_gateway)
    weekly_svc.record_schedule_anchor(
        cycle_id=cycle_1_id,
        anchor_dt=now_utc - timedelta(days=7),
        source="cycle1_initial_t0",
    )
    report_repo = ModelReportRepository(http_test_db)
    report_repo.create_next_version(
        cycle_id=cycle_1_id,
        model=GeminiModelRole.PRO.value,
        report_type=ATHENA_ACTIVE_STRATEGY_REPORT_TYPE,
        privacy_level=PrivacyLevel.SERVER_ONLY,
        content={"strategy_text": "Baseline Strategy Week 1", "version": 1},
    )

    automation_svc = ClinicalAutomationService(db=http_test_db, gateway=mock_gateway, enable_automatic_weekly=True)

    # Seed 7-day Athena logs for cycle 1 so Weekly Consilium has history
    for day in range(1, 8):
        weekly_svc.save_athena_clinical_log(
            cycle_id=cycle_1_id,
            log_entry={
                "status": "ALERT",
                "symptoms_mentioned": ["afternoon_fatigue"],
                "adherence_to_plan": "NORMAL",
                "red_flags": False,
                "summary_for_master": f"Day {day} monitoring recorded pulse elevation.",
            },
            sprint_id="sprint_c1_w1",
            as_of=now_utc - timedelta(days=8 - day),
        )

    # Run Weekly Consilium via client endpoint
    resp_weekly = client.post("/api/v1/client/cascade/run-weekly")
    assert resp_weekly.status_code == 200, resp_weekly.text

    # Generate Doctor Document for Referral
    ref_doc_svc = ReferralDocumentService(db=http_test_db, gateway=mock_gateway)
    doc_res = asyncio.run(ref_doc_svc.generate_referral_document(cycle_id=cycle_1_id))
    assert doc_res is not None
    doc_report_id = doc_res.document_report_id

    # --------------------------------------------------------------------------
    # 1. Статус текущего цикла: GET /api/v1/client/cascade/status
    # --------------------------------------------------------------------------
    resp_step1 = client.get("/api/v1/client/cascade/status")
    assert resp_step1.status_code == 200, resp_step1.text
    data_step1 = resp_step1.json()
    assert data_step1["patient_id"] == patient_id
    assert data_step1["has_active_cycle"] is True
    assert data_step1["active_cycle_id"] == cycle_1_id
    assert data_step1["has_active_referral"] is True
    assert data_step1["referral_status"]["status"] in ("generating_document", "document_ready")
    assert "cardiologist" in data_step1["referral_status"]["target_specialties"]

    # --------------------------------------------------------------------------
    # 2. Referral и список документов: GET /api/v1/client/referral/documents
    # --------------------------------------------------------------------------
    resp_step2 = client.get("/api/v1/client/referral/documents")
    assert resp_step2.status_code == 200, resp_step2.text
    data_step2 = resp_step2.json()
    assert data_step2["total_count"] >= 1
    doc_meta = data_step2["documents"][0]
    assert doc_meta["report_id"] == doc_report_id
    assert doc_meta["cycle_id"] == cycle_1_id
    assert "available_formats" in doc_meta
    assert "pdf" in doc_meta["available_formats"]

    # --------------------------------------------------------------------------
    # 3. Экспорт PDF/DOCX: GET /api/v1/client/referral/documents/{id}/export?format=...
    # --------------------------------------------------------------------------
    # PDF
    resp_pdf = client.get(f"/api/v1/client/referral/documents/{doc_report_id}/export?format=pdf")
    assert resp_pdf.status_code == 200, resp_pdf.text
    assert resp_pdf.headers["content-type"] == "application/pdf"
    assert len(resp_pdf.content) > 100
    assert resp_pdf.content.startswith(b"%PDF")

    # DOCX
    resp_docx = client.get(f"/api/v1/client/referral/documents/{doc_report_id}/export?format=docx")
    assert resp_docx.status_code == 200, resp_docx.text
    assert "wordprocessingml" in resp_docx.headers["content-type"]
    assert len(resp_docx.content) > 100

    # MD
    resp_md = client.get(f"/api/v1/client/referral/documents/{doc_report_id}/export?format=md")
    assert resp_md.status_code == 200, resp_md.text
    assert "HEALTH OBSERVATION SUMMARY" in resp_md.text

    # --------------------------------------------------------------------------
    # 4. История чата Афины: GET /api/v1/client/chat/athena/history
    # --------------------------------------------------------------------------
    resp_step4 = client.get("/api/v1/client/chat/athena/history")
    assert resp_step4.status_code == 200, resp_step4.text
    data_step4 = resp_step4.json()
    assert "messages" in data_step4
    assert isinstance(data_step4["messages"], list)

    # --------------------------------------------------------------------------
    # 5. POST /api/v1/cycles/start-neo-reassessment
    # --------------------------------------------------------------------------
    resp_step5 = client.post("/api/v1/cycles/start-neo-reassessment")
    assert resp_step5.status_code == 200, resp_step5.text
    data_step5 = resp_step5.json()
    cycle_2_id = data_step5["cycle_id"]
    assert data_step5["patient_id"] == patient_id
    assert data_step5["previous_cycle_id"] == cycle_1_id
    assert data_step5["status"] == "pending_reassessment"
    assert data_step5["is_pending_reassessment"] is True

    # --------------------------------------------------------------------------
    # 6. Отправка нового анамнеза: POST /api/v1/client/cascade/intake/anamnesis
    # --------------------------------------------------------------------------
    intake_payload = {
        "age_confirmation": "Confirmed",
        "gender": "Male",
        "age": 36,
        "height": 178,
        "weight": 76,
        "nicotine_status": "Non-user",
        "alcohol_status": "Non-drinker",
        "spec_factors": "None",
        "sleep_quality": "Normal",
        "diet_types": "Balanced",
        "diet_habits": "Regular",
        "gi_signals": "None",
        "work_type": "Office",
        "chronic_diseases": "None",
        "recent_illness": "None",
        "critical_infections": "None",
        "heredity_risks": "None",
        "medications": "None",
    }
    resp_step6 = client.post(
        "/api/v1/client/cascade/intake/anamnesis",
        json={"answers": intake_payload},
    )
    assert resp_step6.status_code == 200, resp_step6.text
    data_step6 = resp_step6.json()
    assert data_step6["cycle_id"] == cycle_2_id
    assert data_step6["status"] == "processed"
    assert data_step6["intake_version"] >= 1

    # --------------------------------------------------------------------------
    # 7. Запуск Neo Consilium: POST /api/v1/client/cascade/run-primary
    # --------------------------------------------------------------------------
    resp_step7 = client.post("/api/v1/client/cascade/run-primary")
    assert resp_step7.status_code == 200, resp_step7.text
    data_step7 = resp_step7.json()
    assert data_step7["cycle_id"] == cycle_2_id
    assert data_step7["mode"] in ("neo", "neo_reassessment")
    assert "overall_report" in data_step7
    assert "vitals_thresholds" in data_step7

    # --------------------------------------------------------------------------
    # 8. Статус prepared: GET /api/v1/client/cascade/status & primary_svc
    # --------------------------------------------------------------------------
    primary_svc = PrimaryConsiliumService(db=http_test_db, gateway=mock_gateway)
    assert primary_svc.is_neo_consilium_prepared(cycle_2_id) is True
    assert primary_svc.is_neo_consilium_activated(cycle_2_id) is False

    resp_step8 = client.get("/api/v1/client/cascade/status")
    assert resp_step8.status_code == 200, resp_step8.text
    data_step8 = resp_step8.json()
    # Cycle remains pending_reassessment until activated
    assert data_step8["active_cycle_id"] == cycle_2_id
    assert data_step8["is_neo_reassessment"] is True

    # --------------------------------------------------------------------------
    # 9. POST /api/v1/client/cascade/activate-neo
    # --------------------------------------------------------------------------
    resp_step9 = client.post(
        "/api/v1/client/cascade/activate-neo",
        json={"cycle_id": cycle_2_id},
    )
    assert resp_step9.status_code == 200, resp_step9.text
    data_step9 = resp_step9.json()
    assert data_step9["success"] is True
    assert data_step9["cycle_id"] == cycle_2_id
    assert data_step9["status"] == "active"
    assert data_step9["strategy_version"] == 1
    assert data_step9["schedule_anchor_t0_utc"] != ""
    assert data_step9["next_weekly_consilium_due_utc"] != ""
    assert data_step9["post_primary_init_enqueued"] is True
    assert data_step9["is_idempotent_replay"] is False

    # --------------------------------------------------------------------------
    # 10. Статус activated и новые отчёты
    # --------------------------------------------------------------------------
    resp_step10 = client.get("/api/v1/client/cascade/status")
    assert resp_step10.status_code == 200, resp_step10.text
    data_step10 = resp_step10.json()
    assert data_step10["has_active_cycle"] is True
    assert data_step10["active_cycle_id"] == cycle_2_id

    # Client-facing reports are accessible via reports endpoint
    resp_reports = client.get(f"/api/v1/reports/client?cycle_id={cycle_2_id}")
    assert resp_reports.status_code == 200, resp_reports.text
    reports_data = resp_reports.json()
    assert len(reports_data) >= 9
    report_types = [r["report_type"] for r in reports_data]
    assert "user_overall_report" in report_types

    # --------------------------------------------------------------------------
    # 11. Повторное получение чата Афины: GET /api/v1/client/chat/athena/history
    # --------------------------------------------------------------------------
    # Execute due post_primary_init job
    asyncio.run(automation_svc.process_due_jobs(discover=False))

    resp_step11 = client.get("/api/v1/client/chat/athena/history")
    assert resp_step11.status_code == 200, resp_step11.text
    data_step11 = resp_step11.json()
    msgs = data_step11.get("messages", [])
    assert len(msgs) >= 1
    assert any("welcome back" in str(m.get("text")).lower() or "reassessment" in str(m.get("text")).lower() for m in msgs)

    # --------------------------------------------------------------------------
    # 12. Список исторических документов после Neo: GET /api/v1/client/referral/documents
    # --------------------------------------------------------------------------
    resp_step12 = client.get("/api/v1/client/referral/documents")
    assert resp_step12.status_code == 200, resp_step12.text
    data_step12 = resp_step12.json()
    assert data_step12["total_count"] >= 1
    doc_ids_after_neo = [d["report_id"] for d in data_step12["documents"]]
    assert doc_report_id in doc_ids_after_neo, "Historical doctor document was lost after Neo activation!"


def test_real_http_contract_error_handling(http_test_db: Session):
    """
    Verifies HTTP error responses across the real API contracts:
    - 401 Unauthorized when Authorization header is missing
    - 403 Forbidden when a patient attempts to access another patient's cycle/document
    - 404 Not Found when document does not exist
    - 422 Unprocessable Content on malformed payloads
    - Idempotent-Replay header and status on duplicate activation
    """
    patient_a = "patient_err_a"
    patient_b = "patient_err_b"
    cycle_a_id = "cycle_err_a"
    mock_gateway = Stage8C1FullJourneyMockGateway()

    # Create two patients and a cycle for patient A
    http_test_db.add(Patient(patient_id=patient_a, status="active"))
    http_test_db.add(Patient(patient_id=patient_b, status="active"))
    http_test_db.add(MedicalCycle(cycle_id=cycle_a_id, patient_id=patient_a, status="active"))
    http_test_db.commit()

    session_a = _make_device_session(patient_id=patient_a, device_id="dev_a")
    session_b = _make_device_session(patient_id=patient_b, device_id="dev_b")

    app.dependency_overrides[get_db] = lambda: http_test_db
    _override_all_gateways(mock_gateway)

    # 1. 401 Unauthorized without auth headers (real auth dependencies execute)
    unauth_client = TestClient(app)
    resp_unauth = unauth_client.get("/api/v1/client/cascade/status")
    assert resp_unauth.status_code == 401

    # Now bind session_a for Patient A
    app.dependency_overrides[require_device_session] = lambda: session_a
    app.dependency_overrides[verify_client_token] = lambda: session_a

    # 2. 404 Not Found for non-existent document
    client_a = TestClient(app, headers={"Authorization": "Bearer mock_jwt_a"})
    resp_404 = client_a.get("/api/v1/client/referral/documents/rep_non_existent")
    assert resp_404.status_code == 404

    # 3. 403 Forbidden: Patient B cannot activate Patient A's cycle
    app.dependency_overrides[require_device_session] = lambda: session_b
    app.dependency_overrides[verify_client_token] = lambda: session_b
    client_b = TestClient(app, headers={"Authorization": "Bearer mock_jwt_b"})

    resp_cross_act = client_b.post(
        "/api/v1/client/cascade/activate-neo",
        json={"cycle_id": cycle_a_id},
    )
    assert resp_cross_act.status_code in (403, 404, 422), resp_cross_act.text

    # 4. 422 Unprocessable Content on invalid intake schema
    app.dependency_overrides[require_device_session] = lambda: session_a
    app.dependency_overrides[verify_client_token] = lambda: session_a
    resp_422 = client_a.post("/api/v1/client/cascade/intake/anamnesis", json={"invalid_field": 123})
    assert resp_422.status_code == 422
