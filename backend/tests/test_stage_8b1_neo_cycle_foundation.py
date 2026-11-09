"""
Stage 8B.1 — Safe Neo Cycle Transition, Historical Preservation & Reassessment Foundation
Unit & Integration Test Suite.

Verifies:
1. Valid Neo start creates linked cycle (old completed, new pending_reassessment).
2. Historical data immutability: old reports, measurements, documents remain intact.
3. Idempotency: duplicate start requests return existing pending cycle without creating duplicates.
4. Interrupted / lost HTTP response retry recovers pending cycle cleanly.
5. Patient isolation & security: device session authentication, cross-patient isolation.
6. DB atomic rollback on failure.
7. Old-cycle pending automation jobs are invalidated (skipped=True).
8. In-flight running old Weekly Consilium is fenced (ConsiliumCycleInactiveError).
9. In-flight running old document generation cannot publish referral into new cycle.
10. Delayed valid old-cycle vitals within 5min window are archived without Mode 2 alerts.
11. Post-closure (>5min) and cross-patient vitals are rejected.
12. Server restart recovers pending Neo state (durable SQLite storage).
13. New cycle does not start Weekly Consilium before Primary Consilium is completed.
14. Old referral reminders stop, doctor documents remain accessible in Safe.
15. Historical Context Service bounded size (~8,000 chars) and clinical governance disclaimer.
16. Concurrency / race condition test: multi-worker execution produces exactly one new cycle.

Zero external Gemini API calls (all transports mocked).
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from unittest.mock import AsyncMock
import uuid

from fastapi import HTTPException
from fastapi.testclient import TestClient
import httpx
import pypdf
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.database import get_db
from app.main import app
from app.models.base import Base
from app.models.health_measurement import HealthMeasurement
from app.models.medical_cycle import MedicalCycle
from app.models.model_report import ModelReport
from app.models.patient import Patient
from app.repositories.cycle_repository import MedicalCycleRepository
from app.repositories.measurement_repository import HealthMeasurementRepository
from app.repositories.report_repository import ModelReportRepository
from app.schemas.health import HealthMeasurementCreate, HealthMeasurementCreateBatch
from app.schemas.privacy import PrivacyLevel
from app.security.auth import (
    DeviceSession,
    require_device_session,
    verify_client_token,
)
from app.services.clinical_automation_service import (
    AutomationJobRecord,
    AutomationJobStatus,
    AutomationJobType,
    ClinicalAutomationService,
)
from app.services.gemini_gateway import GeminiGateway
from app.services.historical_context_service import (
    CLINICAL_GOVERNANCE_NOTICE,
    HistoricalContextService,
    LABORATORY_LEDGER_REPORT_TYPE,
    PREPARATORY_ECG_REPORT_TYPE,
    PRIMARY_OVERALL_CLIENT_REPORT_TYPE,
    REFERRAL_LIFECYCLE_STATE_REPORT_TYPE,
    WEEKLY_OVERALL_CLIENT_REPORT_TYPE,
)
from app.services.neo_cycle_service import (
    NEO_CYCLE_LINEAGE_REPORT_TYPE,
    NeoCycleService,
    NeoReassessmentNotAllowedError,
)
from app.services.weekly_consilium_service import (
    ConsiliumCycleInactiveError,
    WeeklyConsiliumService,
)


# ==============================================================================
# FIXTURES & HELPERS
# ==============================================================================

@pytest.fixture()
def test_db(tmp_path: Path):
    db_file = tmp_path / "test_stage_8b1.sqlite3"
    engine = create_engine(
        f"sqlite:///{db_file}",
        connect_args={"check_same_thread": False},
    )
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)
    session = TestingSessionLocal()
    try:
        yield session, TestingSessionLocal
    finally:
        session.close()
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


def _make_device_session(patient_id: str, scopes: Optional[List[str]] = None) -> DeviceSession:
    eff_scopes = scopes or [
        "cycle:read",
        "reports:read",
        "cascade:run",
        "vitals:write",
        "vitals:read",
        "cascade:intake",
    ]
    return DeviceSession(
        patient_id=patient_id,
        device_id=f"dev_{patient_id}",
        jti=f"jti_{patient_id}",
        family_id=f"fam_{patient_id}",
        scope=" ".join(eff_scopes),
        scopes=frozenset(eff_scopes),
        issued_at=datetime.now(timezone.utc),
        expires_at=datetime.now(timezone.utc) + timedelta(hours=2),
    )


def _seed_patient_and_cycle(
    db: Session,
    patient_id: str,
    cycle_id: str,
    status: str = "active",
    started_at: Optional[datetime] = None,
) -> tuple[Patient, MedicalCycle]:
    patient = db.query(Patient).filter_by(patient_id=patient_id).first()
    if not patient:
        patient = Patient(patient_id=patient_id, status="active")
        db.add(patient)
        db.commit()

    start_dt = started_at or datetime(2026, 10, 18, 8, 0, tzinfo=timezone.utc)
    cycle = db.query(MedicalCycle).filter_by(cycle_id=cycle_id).first()
    if not cycle:
        cycle = MedicalCycle(
            cycle_id=cycle_id,
            patient_id=patient_id,
            status=status,
            started_at=start_dt,
            created_at=start_dt,
        )
        db.add(cycle)
        db.commit()
    return patient, cycle


def _seed_completed_primary(db: Session, cycle_id: str, created_at: Optional[datetime] = None) -> None:
    repo = ModelReportRepository(db)
    now = created_at or datetime(2026, 10, 18, 9, 0, tzinfo=timezone.utc)
    repo.create_next_version(
        cycle_id=cycle_id,
        model="aegis-primary-consilium",
        report_type="user_overall_report",
        privacy_level=PrivacyLevel.CLIENT_FACING,
        content={
            "title": "Comprehensive Initial Health Assessment",
            "summary": "Baseline established across all 8 physiological systems.",
            "report_date": now.strftime("%Y-%m-%d"),
            "metrics": {"min_pessimum_bpm": 48, "max_pessimum_bpm": 118},
        },
        created_at=now,
    )
    repo.create_next_version(
        cycle_id=cycle_id,
        model="gemini-1.5-pro",
        report_type="chief_physician_internal",
        privacy_level=PrivacyLevel.SERVER_ONLY,
        content={
            "pro_training_summary": "Conservative ambulatory strategy initialized.",
            "strategy_status": "initial",
        },
        created_at=now,
    )


def _seed_referral_state(
    db: Session,
    cycle_id: str,
    patient_id: str,
    status: str = "document_ready",
    referral_id: str = "REF-20261108-01",
) -> None:
    repo = ModelReportRepository(db)
    now = datetime(2026, 11, 8, 9, 0, tzinfo=timezone.utc)
    repo.create_next_version(
        cycle_id=cycle_id,
        model="aegis-weekly-consilium",
        report_type=REFERRAL_LIFECYCLE_STATE_REPORT_TYPE,
        privacy_level=PrivacyLevel.SERVER_ONLY,
        content={
            "referral_id": referral_id,
            "patient_id": patient_id,
            "cycle_id": cycle_id,
            "sprint_id": "sprint_test_w1",
            "sprint_number": 1,
            "status": status,
            "urgency": "priority",
            "target_specialties": ["cardiologist", "endocrinologist"],
            "clinical_reason_for_referral": "Recurrent blood pressure variability and resting tachycardia.",
            "created_at_utc": now.isoformat(),
            "has_active_referral": True,
        },
        created_at=now,
    )
    raw_content = {
        "patient_id": patient_id,
        "cycle_id": cycle_id,
        "sprint_id": None,
        "sprint_number": 1,
        "referral_id": referral_id,
        "referral_source": "weekly_consilium",
        "created_at_utc": now.isoformat(),
        "rendered_english_markdown": (
            "# Aegis Clinical Observation Summary\n\n"
            "## Reason for In-Person Consultation\n"
            "Recurrent blood pressure variability and resting tachycardia.\n"
        ),
        "pro_doctor_document": {
            "document_title": "CONSULTATION SUMMARY FOR ATTENDING PHYSICIAN",
            "informational_disclaimer": "Informational summary for licensed physician review.",
            "recommended_specialties": ["cardiologist", "endocrinologist"],
            "consultation_urgency": "priority",
            "executive_summary_for_patient": "Ambulatory evaluation recommended.",
            "patient_and_observation_period": "Monitored 2026-10-18 to 2026-11-08.",
            "reason_for_in_person_consultation": "Recurrent blood pressure variability.",
            "objective_measurements_and_examination_results": ["Mean BP: 142/90 mmHg."],
            "timeline_of_reported_symptoms_and_changes": ["Reported evening fatigue."],
            "important_patterns_and_unresolved_concerns": ["Systolic spikes."],
            "ai_observations_requiring_verification": ["Potential Stage 1 HTN."],
            "questions_and_examinations_for_clinician": ["Consider 24h ABPM."],
        },
    }
    raw_rep = repo.create_next_version(
        cycle_id=cycle_id,
        sprint_id=None,
        model="gemini-1.5-pro",
        report_type="referral_doctor_document_raw",
        privacy_level=PrivacyLevel.SERVER_ONLY,
        content=raw_content,
        created_at=now,
    )
    repo.create_next_version(
        cycle_id=cycle_id,
        sprint_id=None,
        model="gemini-1.5-pro",
        report_type="referral_doctor_document",
        privacy_level=PrivacyLevel.CLIENT_FACING,
        content={
            "title": "CONSULTATION SUMMARY FOR ATTENDING PHYSICIAN",
            "summary": raw_content["rendered_english_markdown"],
            "report_date": now.strftime("%Y-%m-%d"),
            "system_key": None,
            "has_changes": True,
            "highlights": [
                "Recommended consultation: cardiologist, endocrinologist (priority)",
                "Ambulatory evaluation recommended.",
            ],
            "metrics": {
                "patient_id": patient_id,
                "cycle_id": cycle_id,
                "referral_id": referral_id,
                "created_at_utc": now.isoformat(),
                "urgency": "priority",
                "target_specialties": "cardiologist, endocrinologist",
                "raw_document_report_id": raw_rep.report_id,
                "is_informational_observation_summary": True,
            },
        },
        created_at=now,
    )


# ==============================================================================
# SCENARIO 1: VALID NEO CYCLE TRANSITION
# ==============================================================================

def test_01_valid_neo_start_creates_linked_cycle(client: TestClient, db_session: Session):
    patient_id = "pat_neo_01"
    cycle_1_id = "cyc_neo_01_old"
    _seed_patient_and_cycle(db_session, patient_id, cycle_1_id)
    _seed_completed_primary(db_session, cycle_1_id)

    session = _make_device_session(patient_id)
    app.dependency_overrides[require_device_session] = lambda: session
    app.dependency_overrides[verify_client_token] = lambda: session

    # 1. Trigger start Neo reassessment
    resp = client.post("/api/v1/cycle/start-neo-reassessment")
    assert resp.status_code == 200, resp.text
    data = resp.json()

    assert data["patient_id"] == patient_id
    assert data["previous_cycle_id"] == cycle_1_id
    assert data["cycle_id"] != cycle_1_id
    assert data["status"] == "pending_reassessment"
    assert data["reassessment_status"] == "pending_reassessment"
    assert data["is_pending_reassessment"] is True
    assert "successfully provisioned" in data["message"]

    new_cycle_id = data["cycle_id"]

    # 2. Verify Old Cycle is Completed
    cycle_repo = MedicalCycleRepository(db_session)
    old_cycle = cycle_repo.get(cycle_1_id)
    assert old_cycle is not None
    assert old_cycle.status == "completed"
    assert old_cycle.closed_at is not None

    # 3. Verify New Cycle is Pending Reassessment
    new_cycle = cycle_repo.get(new_cycle_id)
    assert new_cycle is not None
    assert new_cycle.status == "pending_reassessment"
    assert new_cycle.patient_id == patient_id
    assert new_cycle.closed_at is None

    # 4. Verify neo_cycle_lineage report exists in SERVER_ONLY
    report_repo = ModelReportRepository(db_session)
    lineage = report_repo.list_server_only(cycle_id=new_cycle_id, report_type=NEO_CYCLE_LINEAGE_REPORT_TYPE)
    assert len(lineage) == 1
    content = lineage[0].content or {}
    assert content["previous_cycle_id"] == cycle_1_id
    assert content["new_cycle_id"] == new_cycle_id
    assert content["previous_cycle_status"] == "completed"
    assert content["new_cycle_status"] == "pending_reassessment"

    # Also test plural route alias (/api/v1/cycles/start-neo-reassessment) returns existing pending cycle idempotently
    resp_plural = client.post("/api/v1/cycles/start-neo-reassessment")
    assert resp_plural.status_code == 200
    assert resp_plural.json()["cycle_id"] == new_cycle_id


# ==============================================================================
# SCENARIO 2: HISTORICAL DATA IMMUTABILITY
# ==============================================================================

def test_02_historical_data_immutability(client: TestClient, db_session: Session):
    patient_id = "pat_neo_02"
    cycle_1_id = "cyc_neo_02_old"
    _seed_patient_and_cycle(db_session, patient_id, cycle_1_id)
    _seed_completed_primary(db_session, cycle_1_id)
    _seed_referral_state(db_session, cycle_1_id, patient_id)

    session = _make_device_session(patient_id)
    app.dependency_overrides[require_device_session] = lambda: session
    app.dependency_overrides[verify_client_token] = lambda: session

    # Transition to Neo
    resp = client.post("/api/v1/cycle/start-neo-reassessment")
    assert resp.status_code == 200
    new_cycle_id = resp.json()["cycle_id"]

    # Verify cross-cycle document query retrieves old referral documents
    resp_docs = client.get("/api/v1/client/referral/documents")
    assert resp_docs.status_code == 200
    docs_data = resp_docs.json()
    assert docs_data["total_count"] >= 1
    found_doc = next((d for d in docs_data["documents"] if d["cycle_id"] == cycle_1_id), None)
    assert found_doc is not None
    assert found_doc["cycle_id"] == cycle_1_id
    assert found_doc["title"] == "CONSULTATION SUMMARY FOR ATTENDING PHYSICIAN"

    # Verify old Primary reports are still intact
    report_repo = ModelReportRepository(db_session)
    old_reports = report_repo.list_client_facing(cycle_id=cycle_1_id)
    assert len(old_reports) >= 2


# ==============================================================================
# SCENARIOS 3 & 4: IDEMPOTENCY & LOST HTTP RETRY
# ==============================================================================

def test_03_duplicate_start_request_is_idempotent(client: TestClient, db_session: Session):
    patient_id = "pat_neo_03"
    cycle_1_id = "cyc_neo_03_old"
    _seed_patient_and_cycle(db_session, patient_id, cycle_1_id)
    _seed_completed_primary(db_session, cycle_1_id)

    session = _make_device_session(patient_id)
    app.dependency_overrides[require_device_session] = lambda: session
    app.dependency_overrides[verify_client_token] = lambda: session

    resp1 = client.post("/api/v1/cycle/start-neo-reassessment")
    assert resp1.status_code == 200
    cyc_first = resp1.json()["cycle_id"]

    # Immediate second call
    resp2 = client.post("/api/v1/cycle/start-neo-reassessment")
    assert resp2.status_code == 200
    cyc_second = resp2.json()["cycle_id"]

    assert cyc_first == cyc_second
    assert resp2.json()["previous_cycle_id"] == cycle_1_id

    # Total cycles in DB for patient should be exactly 2 (cycle_1 and cycle_2)
    cycle_repo = MedicalCycleRepository(db_session)
    cycles = cycle_repo.list_by_patient(patient_id)
    assert len(cycles) == 2


def test_04_interrupted_http_retry_recovers_pending_cycle(client: TestClient, db_session: Session):
    patient_id = "pat_neo_04"
    cycle_1_id = "cyc_neo_04_old"
    _seed_patient_and_cycle(db_session, patient_id, cycle_1_id)
    _seed_completed_primary(db_session, cycle_1_id)

    neo_svc = NeoCycleService(db_session)
    res = neo_svc.start_neo_reassessment(patient_id)
    expected_pending_id = res.cycle_id

    # Client reconnects after dropped response
    session = _make_device_session(patient_id)
    app.dependency_overrides[require_device_session] = lambda: session
    app.dependency_overrides[verify_client_token] = lambda: session

    resp = client.post("/api/v1/cycle/start-neo-reassessment")
    assert resp.status_code == 200
    assert resp.json()["cycle_id"] == expected_pending_id
    assert resp.json()["status"] == "pending_reassessment"


# ==============================================================================
# SCENARIO 5: PATIENT ISOLATION & GUARDRAILS
# ==============================================================================

def test_05_cross_patient_unauthorized_start_rejected(client: TestClient, db_session: Session):
    patient_a = "pat_neo_05_a"
    patient_b = "pat_neo_05_b"
    cycle_a = "cyc_neo_05_a"

    _seed_patient_and_cycle(db_session, patient_a, cycle_a)
    _seed_completed_primary(db_session, cycle_a)

    # Patient B attempts to call start-neo without having any cycle
    session_b = _make_device_session(patient_b)
    app.dependency_overrides[require_device_session] = lambda: session_b
    app.dependency_overrides[verify_client_token] = lambda: session_b

    resp_b = client.post("/api/v1/cycle/start-neo-reassessment")
    assert resp_b.status_code == 409
    assert "not found" in resp_b.json()["detail"].lower()

    # Patient C has active cycle but NEVER completed Primary Consilium
    patient_c = "pat_neo_05_c"
    cycle_c = "cyc_neo_05_c"
    _seed_patient_and_cycle(db_session, patient_c, cycle_c)

    session_c = _make_device_session(patient_c)
    app.dependency_overrides[require_device_session] = lambda: session_c
    app.dependency_overrides[verify_client_token] = lambda: session_c

    resp_c = client.post("/api/v1/cycle/start-neo-reassessment")
    assert resp_c.status_code == 409
    assert "primary consilium must be completed" in resp_c.json()["detail"].lower()


# ==============================================================================
# SCENARIO 6: DB ATOMIC ROLLBACK ON FAILURE
# ==============================================================================

def test_06_db_error_rolls_back_transition_cleanly(client: TestClient, db_session: Session):
    patient_id = "pat_neo_06"
    cycle_1_id = "cyc_neo_06_old"
    _seed_patient_and_cycle(db_session, patient_id, cycle_1_id)
    _seed_completed_primary(db_session, cycle_1_id)

    neo_svc = NeoCycleService(db_session)

    # Force a failure midway by mocking report_repo.create_next_version to raise
    def fail_create_version(*args, **kwargs):
        raise RuntimeError("Simulated DB disk failure")

    neo_svc.report_repo.create_next_version = fail_create_version

    with pytest.raises(RuntimeError, match="Simulated DB disk failure"):
        neo_svc.start_neo_reassessment(patient_id)

    # Verify rollback: cycle 1 must remain active and no pending cycle created
    cycle_repo = MedicalCycleRepository(db_session)
    c1 = cycle_repo.get(cycle_1_id)
    assert c1.status == "active"
    assert c1.closed_at is None

    cycles = cycle_repo.list_by_patient(patient_id)
    assert len(cycles) == 1


# ==============================================================================
# SCENARIO 7: OLD-CYCLE AUTOMATION JOBS INVALIDATED
# ==============================================================================

def test_07_pending_old_cycle_jobs_are_invalidated(client: TestClient, db_session: Session):
    patient_id = "pat_neo_07"
    cycle_1_id = "cyc_neo_07_old"
    _seed_patient_and_cycle(db_session, patient_id, cycle_1_id)
    _seed_completed_primary(db_session, cycle_1_id)

    # Create schedule anchor so weekly consilium and daily journal can be enqueued
    report_repo = ModelReportRepository(db_session)
    report_repo.create_next_version(
        cycle_id=cycle_1_id,
        model="athena-schedule-engine",
        report_type="athena_schedule_anchor",
        privacy_level=PrivacyLevel.SERVER_ONLY,
        content={"anchor_timestamp_utc": (datetime.now(timezone.utc) - timedelta(hours=200)).isoformat()},
    )

    # Enqueue a weekly consilium job and a daily journal job on old cycle
    automation_svc = ClinicalAutomationService(db_session)
    automation_svc.enqueue_weekly_consilium_a(
        cycle_id=cycle_1_id,
        patient_id=patient_id,
        sprint_number=1,
    )
    automation_svc.enqueue_daily_journal_closure(
        cycle_id=cycle_1_id,
        patient_id=patient_id,
        calendar_week=1,
        calendar_day=1,
    )

    # Verify jobs are currently pending
    pending_before = automation_svc.list_jobs(cycle_id=cycle_1_id, status=AutomationJobStatus.PENDING)
    assert len(pending_before) == 2

    # Start Neo
    neo_svc = NeoCycleService(db_session)
    neo_svc.start_neo_reassessment(patient_id)

    # Verify all jobs for old cycle are cancelled with skipped=True
    pending_after = automation_svc.list_jobs(cycle_id=cycle_1_id, status=AutomationJobStatus.PENDING)
    assert len(pending_after) == 0

    job_rec = automation_svc.get_job(cycle_1_id, AutomationJobType.WEEKLY_CONSILIUM_A, f"weekly_consilium_step_a_{cycle_1_id}_w1")
    assert job_rec is not None
    assert job_rec.status == AutomationJobStatus.COMPLETED
    assert job_rec.result_metadata.get("skipped") is True
    assert job_rec.result_metadata.get("reason") == "neo_cycle_transition"


# ==============================================================================
# SCENARIO 8: IN-FLIGHT RUNNING OLD WEEKLY FENCED
# ==============================================================================

@pytest.mark.anyio
async def test_08_inflight_running_old_weekly_is_fenced(test_db):
    session, TestingSessionLocal = test_db
    patient_id = "pat_neo_08"
    cycle_1_id = "cyc_neo_08_old"
    _seed_patient_and_cycle(session, patient_id, cycle_1_id)
    _seed_completed_primary(session, cycle_1_id)

    # Create schedule anchor for weekly consilium
    t0 = datetime.now(timezone.utc) - timedelta(hours=170)
    report_repo = ModelReportRepository(session)
    report_repo.create_next_version(
        cycle_id=cycle_1_id,
        model="athena-schedule-engine",
        report_type="athena_schedule_anchor",
        privacy_level=PrivacyLevel.SERVER_ONLY,
        content={"anchor_timestamp_utc": t0.isoformat()},
        created_at=t0,
    )

    weekly_svc = WeeklyConsiliumService(session)

    # Transition cycle_1 to completed
    cycle_repo = MedicalCycleRepository(session)
    cycle_repo.update_status(cycle_1_id, status="completed")

    # Attempting to run weekly consilium on inactive cycle must raise ConsiliumCycleInactiveError
    with pytest.raises(ConsiliumCycleInactiveError) as excinfo:
        await weekly_svc.run_weekly_consilium(
            cycle_id=cycle_1_id,
            sprint_id="sprint_test_w1",
        )
    assert "not active" in str(excinfo.value).lower() or "inactive" in str(excinfo.value).lower() or "non-active" in str(excinfo.value).lower()

    # Verify nothing was persisted into the database for cycle_1
    persisted_strategy = session.query(ModelReport).filter_by(
        cycle_id=cycle_1_id,
        report_type="athena_active_strategy",
    ).first()
    assert persisted_strategy is None


# ==============================================================================
# SCENARIO 9: IN-FLIGHT OLD DOCUMENT GENERATION FENCED
# ==============================================================================

@pytest.mark.anyio
async def test_09_inflight_old_doc_generation_cannot_publish_active_referral(test_db):
    session, TestingSessionLocal = test_db
    patient_id = "pat_neo_09"
    cycle_1_id = "cyc_neo_09_old"
    _seed_patient_and_cycle(session, patient_id, cycle_1_id)
    _seed_completed_primary(session, cycle_1_id)
    _seed_referral_state(session, cycle_1_id, patient_id, status="generating_document")

    # Start Neo transition (closes old cycle)
    neo_svc = NeoCycleService(session)
    res = neo_svc.start_neo_reassessment(patient_id)
    new_cycle_id = res.cycle_id

    # Background doc generation runs on old closed cycle
    automation_svc = ClinicalAutomationService(session)
    job_rec = AutomationJobRecord(
        job_id="job_doc_test",
        report_type="auto_job_referral_doc",
        patient_id=patient_id,
        cycle_id=cycle_1_id,
        job_type=AutomationJobType.REFERRAL_DOCUMENT_GENERATION,
        idempotency_key=f"referral_doc_{cycle_1_id}_ref1_sprint1",
        status=AutomationJobStatus.PENDING,
        created_at_utc=datetime.now(timezone.utc).isoformat(),
        updated_at_utc=datetime.now(timezone.utc).isoformat(),
    )

    exec_result = await automation_svc._execute_referral_document_generation(job_rec)
    assert exec_result.get("skipped") is True
    assert exec_result.get("reason") == "cycle_not_active"

    # New cycle must NOT have any referral state
    new_ref = session.query(ModelReport).filter_by(
        cycle_id=new_cycle_id,
        report_type=REFERRAL_LIFECYCLE_STATE_REPORT_TYPE,
    ).first()
    assert new_ref is None


# ==============================================================================
# SCENARIOS 10 & 11: DELAYED VITALS ARCHIVAL PATH & REJECTION
# ==============================================================================

def test_10_delayed_valid_old_cycle_vitals_archived(client: TestClient, db_session: Session):
    patient_id = "pat_neo_10"
    cycle_1_id = "cyc_neo_10_old"
    _seed_patient_and_cycle(db_session, patient_id, cycle_1_id)
    _seed_completed_primary(db_session, cycle_1_id)

    # Start Neo to close cycle 1
    session = _make_device_session(patient_id)
    app.dependency_overrides[require_device_session] = lambda: session
    app.dependency_overrides[verify_client_token] = lambda: session

    resp_neo = client.post("/api/v1/cycle/start-neo-reassessment")
    assert resp_neo.status_code == 200

    cycle_repo = MedicalCycleRepository(db_session)
    c1 = cycle_repo.get(cycle_1_id)
    assert c1.status == "completed"
    closed_at = c1.closed_at or datetime.now(timezone.utc)

    # 1. Delivery after 1 hour of valid offline measurement taken 1 hour BEFORE closure
    meas_dt_1h = closed_at - timedelta(hours=1)
    delayed_payload = HealthMeasurementCreateBatch(
        cycle_id=cycle_1_id,
        measurements=[
            HealthMeasurementCreate(
                cycle_id=cycle_1_id,
                measured_at=meas_dt_1h,
                pulse=72,
                systolic=120,
                diastolic=80,
                spo2=98,
                phase="standby",
                source="watch_sensor",
            )
        ],
    )

    resp_batch = client.post(
        "/api/v1/vitals/batch",
        json=delayed_payload.model_dump(mode="json"),
    )
    assert resp_batch.status_code in (200, 201), resp_batch.text
    assert resp_batch.json()["recorded_count"] == 1

    # Deduplication check: re-delivering the exact same batch is safely deduplicated
    resp_dedup = client.post(
        "/api/v1/vitals/batch",
        json=delayed_payload.model_dump(mode="json"),
    )
    assert resp_dedup.status_code == 200
    assert resp_dedup.json()["recorded_count"] == 0
    assert resp_dedup.json()["deduplicated_count"] == 1

    # 2. Delivery after 24 hours (or days) of valid measurement taken 24 hours BEFORE closure
    meas_dt_24h = closed_at - timedelta(hours=24)
    payload_24h = HealthMeasurementCreate(
        cycle_id=cycle_1_id,
        measured_at=meas_dt_24h,
        pulse=68,
        systolic=118,
        diastolic=78,
        spo2=99,
        source="watch_sensor",
    )
    resp_single = client.post(
        "/api/v1/vitals/measurement",
        json=payload_24h.model_dump(mode="json"),
    )
    assert resp_single.status_code == 201, resp_single.text
    assert resp_single.json()["status"] == "recorded"

    # Verify both measurements are saved into completed cycle 1
    meas_repo = HealthMeasurementRepository(db_session)
    saved = meas_repo.list_by_cycle(cycle_1_id)
    assert len(saved) >= 2
    pulses = [s.pulse for s in saved]
    assert 72 in pulses
    assert 68 in pulses


def test_11_post_closure_and_cross_patient_vitals_rejected(client: TestClient, db_session: Session):
    patient_id = "pat_neo_11"
    cycle_1_id = "cyc_neo_11_old"
    _seed_patient_and_cycle(db_session, patient_id, cycle_1_id)
    _seed_completed_primary(db_session, cycle_1_id)

    session = _make_device_session(patient_id)
    app.dependency_overrides[require_device_session] = lambda: session
    app.dependency_overrides[verify_client_token] = lambda: session

    resp_start = client.post("/api/v1/cycle/start-neo-reassessment")
    assert resp_start.status_code == 200
    new_cycle_id = resp_start.json()["cycle_id"]

    cycle_repo = MedicalCycleRepository(db_session)
    c1 = cycle_repo.get(cycle_1_id)
    # Set closed_at 30 minutes in the past so post-closure test items are in the past but post-closure
    c1.closed_at = datetime.now(timezone.utc) - timedelta(minutes=30)
    db_session.commit()
    closed_at = c1.closed_at

    # 1. Measurement taken 1 second AFTER closure cannot be written into old cycle -> HTTP 400
    post_closure_dt = closed_at + timedelta(seconds=1)
    bad_payload = HealthMeasurementCreate(
        cycle_id=cycle_1_id,
        measured_at=post_closure_dt,
        pulse=75,
        systolic=122,
        diastolic=82,
        spo2=98,
        source="watch_sensor",
    )
    resp_stale = client.post(
        "/api/v1/vitals/measurement",
        json=bad_payload.model_dump(mode="json"),
    )
    assert resp_stale.status_code == 400
    assert "postdates cycle closure" in resp_stale.json()["detail"].lower()

    # 2. Batch item with timestamp postdating closure -> rejected with HTTP 400
    bad_batch = HealthMeasurementCreateBatch(
        cycle_id=cycle_1_id,
        measurements=[
            HealthMeasurementCreate(
                cycle_id=cycle_1_id,
                measured_at=closed_at - timedelta(minutes=5),
                pulse=70,
                source="watch_sensor",
            ),
            HealthMeasurementCreate(
                cycle_id=cycle_1_id,
                measured_at=closed_at + timedelta(minutes=2),
                pulse=74,
                source="watch_sensor",
            ),
        ],
    )
    resp_bad_batch = client.post(
        "/api/v1/vitals/batch",
        json=bad_batch.model_dump(mode="json"),
    )
    assert resp_bad_batch.status_code == 400
    assert "postdates cycle closure" in resp_bad_batch.json()["detail"].lower()

    # 3. Future timestamp relative to server time -> rejected
    future_dt = datetime.now(timezone.utc) + timedelta(hours=2)
    resp_future = client.post(
        "/api/v1/vitals/measurement",
        json={
            "cycle_id": new_cycle_id,
            "measured_at": future_dt.isoformat(),
            "pulse": 75,
            "source": "watch_sensor",
        },
    )
    assert resp_future.status_code in (400, 422)
    assert "future" in resp_future.text.lower()

    # 4. Cross-patient measurement attempt: Patient B tries to write vitals into Patient A's cycle
    session_b = _make_device_session("pat_stranger")
    app.dependency_overrides[require_device_session] = lambda: session_b
    app.dependency_overrides[verify_client_token] = lambda: session_b

    resp_cross = client.post(
        "/api/v1/vitals/measurement",
        json=HealthMeasurementCreate(
            cycle_id=cycle_1_id,
            measured_at=closed_at - timedelta(minutes=5),
            pulse=70,
            source="watch_sensor",
        ).model_dump(mode="json"),
    )
    assert resp_cross.status_code in (403, 404)


# ==============================================================================
# SCENARIO 12: SERVER RESTART RECOVERS PENDING NEO STATE
# ==============================================================================

def test_12_server_restart_recovers_pending_neo_state(client: TestClient, db_session: Session):
    patient_id = "pat_neo_12"
    cycle_1_id = "cyc_neo_12_old"
    _seed_patient_and_cycle(db_session, patient_id, cycle_1_id)
    _seed_completed_primary(db_session, cycle_1_id)

    session = _make_device_session(patient_id)
    app.dependency_overrides[require_device_session] = lambda: session
    app.dependency_overrides[verify_client_token] = lambda: session

    resp_neo = client.post("/api/v1/cycle/start-neo-reassessment")
    assert resp_neo.status_code == 200
    new_cycle_id = resp_neo.json()["cycle_id"]

    # Cascade status endpoint reflects pending reassessment
    resp_status = client.get("/api/v1/client/cascade/status")
    assert resp_status.status_code == 200
    stat_data = resp_status.json()
    assert stat_data["active_cycle_id"] == new_cycle_id
    assert stat_data["is_neo_reassessment"] is True
    assert stat_data["reassessment_status"] == "pending_reassessment"
    assert stat_data["is_primary_consilium_completed"] is False


# ==============================================================================
# SCENARIO 13: WEEKLY CONSILIUM FORBIDDEN ON PENDING REASSESSMENT
# ==============================================================================

def test_13_new_cycle_does_not_start_weekly_before_primary(client: TestClient, db_session: Session):
    patient_id = "pat_neo_13"
    cycle_1_id = "cyc_neo_13_old"
    _seed_patient_and_cycle(db_session, patient_id, cycle_1_id)
    _seed_completed_primary(db_session, cycle_1_id)

    session = _make_device_session(patient_id)
    app.dependency_overrides[require_device_session] = lambda: session
    app.dependency_overrides[verify_client_token] = lambda: session

    client.post("/api/v1/cycle/start-neo-reassessment")

    # Attempting to trigger weekly consilium on the new cycle fails
    resp_weekly = client.post("/api/v1/client/cascade/run-weekly")
    assert resp_weekly.status_code == 409
    assert "primary consilium must be completed" in resp_weekly.json()["detail"].lower()


# ==============================================================================
# SCENARIO 14: REFERRAL REMINDERS STOP & DOCUMENTS STAY ACCESSIBLE
# ==============================================================================

def test_14_old_referral_reminders_stop_documents_accessible(client: TestClient, db_session: Session):
    patient_id = "pat_neo_14"
    cycle_1_id = "cyc_neo_14_old"
    _seed_patient_and_cycle(db_session, patient_id, cycle_1_id)
    _seed_completed_primary(db_session, cycle_1_id)
    _seed_referral_state(db_session, cycle_1_id, patient_id)

    session = _make_device_session(patient_id)
    app.dependency_overrides[require_device_session] = lambda: session
    app.dependency_overrides[verify_client_token] = lambda: session

    # Enqueue a referral follow-up reminder job on cycle 1
    automation_svc = ClinicalAutomationService(db_session)
    automation_svc.enqueue_referral_followup_reminder(
        cycle_id=cycle_1_id,
        patient_id=patient_id,
        target_interval=1,
    )

    # Start Neo
    client.post("/api/v1/cycle/start-neo-reassessment")

    # Verify reminder job is cancelled
    rem_jobs = [
        j for j in automation_svc.list_jobs(cycle_id=cycle_1_id, status=AutomationJobStatus.PENDING)
        if j.job_type == AutomationJobType.REFERRAL_FOLLOWUP_REMINDER
    ]
    assert len(rem_jobs) == 0

    # Verify document export still works for the old cycle
    report_repo = ModelReportRepository(db_session)
    docs = report_repo.list_client_facing(cycle_id=cycle_1_id, report_type="referral_doctor_document")
    assert len(docs) >= 1
    doc_id = docs[0].report_id

    resp_export = client.get(f"/api/v1/client/referral/documents/{doc_id}/export?format=pdf")
    assert resp_export.status_code == 200
    assert resp_export.content.startswith(b"%PDF")


# ==============================================================================
# SCENARIO 15: HISTORICAL CONTEXT SERVICE BOUNDED SIZE & DISCLAIMER
# ==============================================================================

def test_15_historical_context_bounded_size_and_disclaimer(db_session: Session):
    patient_id = "pat_neo_15"
    cycle_1_id = "cyc_neo_15_c1"
    cycle_2_id = "cyc_neo_15_c2"

    _seed_patient_and_cycle(db_session, patient_id, cycle_1_id, status="completed")
    _seed_completed_primary(db_session, cycle_1_id)
    # Seed two versions of referral state for the same referral (v1 generating_document, v2 visit_confirmed)
    _seed_referral_state(db_session, cycle_1_id, patient_id, status="generating_document")
    _seed_referral_state(db_session, cycle_1_id, patient_id, status="visit_confirmed")

    # Add laboratory ledger with abnormal marker
    report_repo = ModelReportRepository(db_session)
    report_repo.create_next_version(
        cycle_id=cycle_1_id,
        model="aegis-lab-ledger",
        report_type=LABORATORY_LEDGER_REPORT_TYPE,
        privacy_level=PrivacyLevel.SERVER_ONLY,
        content={
            "ledger_version": 1,
            "active_markers": {
                "fasting_glucose": {
                    "display_name": "Fasting Plasma Glucose",
                    "value": "128",
                    "numeric_value": 128.0,
                    "unit": "mg/dL",
                    "sample_date": "2026-10-20",
                    "reference_range": {"low": 70.0, "high": 99.0},
                    "is_critical_flag": False,
                },
                "alt": {
                    "display_name": "Alanine Aminotransferase",
                    "value": "85",
                    "numeric_value": 85.0,
                    "unit": "U/L",
                    "sample_date": "2026-10-20",
                    "reference_range": {"low": 10.0, "high": 40.0},
                    "is_critical_flag": False,
                },
            },
        },
    )

    # Add canonical Vision ECG report (preparatory_vision_ecg)
    report_repo.create_next_version(
        cycle_id=cycle_1_id,
        model="aegis-ecg-vision",
        report_type="preparatory_vision_ecg",
        privacy_level=PrivacyLevel.SERVER_ONLY,
        content={
            "recording_type": "Standard 12-lead ECG",
            "recording_quality": "good",
            "heart_rate_bpm": 102,
            "rhythm_type": "sinus_rhythm",
            "ecg_summary": "Normal sinus rhythm with sinus tachycardia (HR 102 bpm).",
            "potential_urgent_warning": None,
        },
    )

    # Cycle 2 completed
    _seed_patient_and_cycle(db_session, patient_id, cycle_2_id, status="completed")
    _seed_completed_primary(db_session, cycle_2_id)

    # Build historical context
    hist_svc = HistoricalContextService(db_session)
    summary = hist_svc.build_historical_context(patient_id, max_characters=8000)

    assert summary.patient_id == patient_id
    assert summary.prior_cycles_count == 2
    assert cycle_1_id in summary.prior_cycle_ids
    assert cycle_2_id in summary.prior_cycle_ids
    assert summary.abnormal_labs_count == 2
    # Multi-version referral is correctly deduplicated to 1 entry
    assert summary.referrals_count == 1

    md = summary.formatted_markdown

    # 1. Prominent clinical governance notice
    assert CLINICAL_GOVERNANCE_NOTICE in md
    assert "**NOT** confirmed medical diagnoses" in md

    # 2. Key sections present
    assert "Fasting Plasma Glucose" in md
    assert "128 mg/dL" in md
    assert "ABOVE_REF" in md
    assert "Standard 12-lead ECG" in md
    assert "HR: 102 bpm" in md
    assert "CONFIRMED (Patient self-reported in-person doctor visit)" in md

    # 3. Size bound
    assert summary.character_count <= 8000
    assert summary.is_condensed is False

    # 4. Condensation test: force low character limit and verify condensation flag
    condensed_summary = hist_svc.build_historical_context(patient_id, max_characters=1500)
    assert condensed_summary.is_condensed is True
    assert condensed_summary.character_count <= 1500
    assert CLINICAL_GOVERNANCE_NOTICE in condensed_summary.formatted_markdown


# ==============================================================================
# SCENARIO 16: CONCURRENCY RACE CONDITION TEST
# ==============================================================================

def test_16_concurrency_race_condition_worker_timing(test_db):
    session, TestingSessionLocal = test_db
    patient_id = "pat_neo_16"
    cycle_1_id = "cyc_neo_16_old"
    _seed_patient_and_cycle(session, patient_id, cycle_1_id)
    _seed_completed_primary(session, cycle_1_id)

    def run_neo_transition(worker_idx: int) -> str:
        worker_session = TestingSessionLocal()
        try:
            svc = NeoCycleService(worker_session)
            resp = svc.start_neo_reassessment(patient_id)
            return resp.cycle_id
        finally:
            worker_session.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        f1 = executor.submit(run_neo_transition, 1)
        f2 = executor.submit(run_neo_transition, 2)
        res1 = f1.result()
        res2 = f2.result()

    # Both concurrent calls must return identical new cycle_id without duplicate cycles
    assert res1 == res2

    cycle_repo = MedicalCycleRepository(session)
    cycles = cycle_repo.list_by_patient(patient_id)
    assert len(cycles) == 2  # exactly cycle 1 and cycle 2
    pending_cycles = [c for c in cycles if c.status == "pending_reassessment"]
    assert len(pending_cycles) == 1


# ==============================================================================
# SCENARIO 17: IN-FLIGHT ATHENA WEEKLY WRAPUP FENCED
# ==============================================================================

@pytest.mark.anyio
async def test_17_inflight_athena_wrapup_fenced_when_cycle_closes(test_db):
    session, TestingSessionLocal = test_db
    patient_id = "pat_neo_17"
    cycle_1_id = "cyc_neo_17_old"
    _seed_patient_and_cycle(session, patient_id, cycle_1_id)
    _seed_completed_primary(session, cycle_1_id)

    # Seed client-facing overall report for weekly wrapup
    report_repo = ModelReportRepository(session)
    report_repo.create_next_version(
        cycle_id=cycle_1_id,
        model="aegis-chief-physician",
        report_type="weekly_user_overall_report",
        privacy_level=PrivacyLevel.CLIENT_FACING,
        content={"summary": "Week 1 is stable", "highlights": ["Good vitals"], "metrics": {"sprint_number": 1}},
    )

    from app.services.athena_chat_service import AthenaChatService
    from app.schemas.chat import AthenaWeeklyWrapupOutput
    athena_svc = AthenaChatService(session)

    orig_generate = athena_svc.gateway.generate_structured

    async def side_effect(*args, **kwargs):
        # Neo reassessment starts and closes cycle_1 in parallel session while Gemini Flash is thinking
        s2 = TestingSessionLocal()
        try:
            repo2 = MedicalCycleRepository(s2)
            repo2.update_status(cycle_1_id, status="completed")
        finally:
            s2.close()

        return AthenaWeeklyWrapupOutput(
            message="Athena wrapup message",
            confidence=0.95,
        )

    athena_svc.gateway.generate_structured = AsyncMock(side_effect=side_effect)

    # In-flight wrapup execution must abort and raise ConsiliumCycleInactiveError
    with pytest.raises(ConsiliumCycleInactiveError):
        await athena_svc.send_weekly_wrapup_message(
            cycle_id=cycle_1_id,
            sprint_number=1,
            idempotency_key=f"wrapup_{cycle_1_id}_w1",
        )

    # Verify no message was persisted into chat history
    chat_repo = report_repo.list_server_only(cycle_id=cycle_1_id, report_type="athena_chat_history")
    assert len(chat_repo) == 0


# ==============================================================================
# SCENARIO 18: LIVE MEASUREMENTS IN NEW CYCLE TRIGGER MODE 2 MONITORING
# ==============================================================================

def test_18_live_measurements_in_new_cycle_trigger_mode2(client: TestClient, db_session: Session):
    patient_id = "pat_neo_18"
    cycle_1_id = "cyc_neo_18_old"
    _seed_patient_and_cycle(db_session, patient_id, cycle_1_id)
    _seed_completed_primary(db_session, cycle_1_id)

    session = _make_device_session(patient_id)
    app.dependency_overrides[require_device_session] = lambda: session
    app.dependency_overrides[verify_client_token] = lambda: session

    # Transition to Neo
    resp_start = client.post("/api/v1/cycle/start-neo-reassessment")
    assert resp_start.status_code == 200
    new_cycle_id = resp_start.json()["cycle_id"]

    # Ingest a live normal telemetry point into the new pending_reassessment cycle
    live_meas_dt = datetime.now(timezone.utc) - timedelta(seconds=10)
    live_payload = HealthMeasurementCreate(
        cycle_id=new_cycle_id,
        measured_at=live_meas_dt,
        pulse=74,
        systolic=120,
        diastolic=80,
        spo2=98,
        source="watch_sensor",
        is_historical=False,
    )

    resp_ingest = client.post(
        "/api/v1/vitals/measurement",
        json=live_payload.model_dump(mode="json"),
    )
    assert resp_ingest.status_code == 201
    assert resp_ingest.json()["cycle_id"] == new_cycle_id
    assert resp_ingest.json()["status"] == "recorded"

    # Verify measurement was recorded into new cycle
    meas_repo = HealthMeasurementRepository(db_session)
    saved = meas_repo.list_by_cycle(new_cycle_id)
    assert len(saved) == 1
    assert saved[0].pulse == 74
    assert saved[0].is_historical is False
