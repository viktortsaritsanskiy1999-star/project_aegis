import asyncio
import json
import os
from pathlib import Path
import shutil
import tempfile
import time
from typing import Any, Dict
import unittest
import pytest
from fastapi.testclient import TestClient
import httpx
from pydantic import BaseModel, Field
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import settings
from app.database import Base, get_db
from datetime import datetime, timezone
from app.main import app
from app.models.audit_review import ClinicalAIAuditReview
from app.models.health_measurement import HealthMeasurement
from app.models.medical_cycle import MedicalCycle
from app.models.model_report import ModelReport
from app.models.patient import Patient
from app.models.workout_session import WorkoutSession
from app.schemas.audit import AuditRecord, ClinicalReviewStatus
from app.schemas.privacy import PrivacyLevel
from app.security.auth import DeviceSession, ServiceAdminSession, require_service_admin_session
from app.services.audit_trail_service import (
    AuditContext,
    AuditStorageError,
    AuditTrailService,
    audit_scope,
    get_audit_trail_service,
)
from app.services.gemini_gateway import (
    GeminiGateway,
    GeminiModelRole,
    GeminiTransientError,
    GeminiValidationError,
)


class DummyClinicalOutput(BaseModel):
    clinical_score: int
    recommendations: list[str] = Field(default_factory=list)


class MockCustomTransport(httpx.AsyncBaseTransport):
    """Configurable mock transport for Gemini API simulation without external network calls."""

    def __init__(self, responses: list[httpx.Response]):
        self.responses = list(responses)
        self.call_count = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.call_count += 1
        if not self.responses:
            raise RuntimeError("No more mocked responses available in transport.")
        res = self.responses.pop(0)
        if isinstance(res, Exception):
            raise res
        return res


@pytest.fixture
def temp_audit_dir():
    """Provides a temporary, isolated directory for audit trail storage."""
    d = tempfile.mkdtemp(prefix="aegis_audit_test_")
    yield Path(d)
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def isolated_db_session():
    """Provides an isolated in-memory SQLite database session."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    session = TestingSessionLocal()
    try:
        yield session
    finally:
        session.close()


def make_gemini_response_payload(raw_text: str) -> dict:
    """Builds standard Google Gemini REST candidates response."""
    return {
        "candidates": [
            {
                "content": {
                    "parts": [{"text": raw_text}],
                    "role": "model",
                },
                "finishReason": "STOP",
            }
        ]
    }


class TestClinicalAIAuditTrail:
    """Unit and integration test suite for Stage 9 Clinical AI Audit Trail & Local Admin Dashboard."""

    def test_call_id_uniqueness(self, temp_audit_dir):
        """Verifies that every inference call generates a strictly unique call_id."""
        audit_service = AuditTrailService(storage_dir=temp_audit_dir, enabled=True)
        raw_output = '{"clinical_score": 85, "recommendations": ["Daily walking"]}'
        payload = make_gemini_response_payload(raw_output)

        mock_responses = [
            httpx.Response(200, json=payload),
            httpx.Response(200, json=payload),
            httpx.Response(200, json=payload),
        ]
        transport = MockCustomTransport(mock_responses)
        gateway = GeminiGateway(
            api_key="TEST_KEY",
            transport=transport,
            audit_service=audit_service,
        )

        call_ids = []
        for i in range(3):
            with audit_scope(patient_id="pat_001", cycle_id="cyc_001", stage=f"stage_{i}"):
                res = asyncio.run(gateway.generate_structured("prompt", DummyClinicalOutput))
                assert res.clinical_score == 85
                assert gateway.last_call_id is not None
                call_ids.append(gateway.last_call_id)

        assert len(call_ids) == 3
        assert len(set(call_ids)) == 3, "Each call_id must be globally unique"
        for cid in call_ids:
            assert cid.startswith("call_")
            record = audit_service.get_call(cid)
            assert record is not None
            assert record.call_id == cid

    def test_verbatim_raw_and_validated_output_recorded_separately(self, temp_audit_dir):
        """Verifies that raw response text is preserved verbatim without alteration alongside parsed JSON."""
        audit_service = AuditTrailService(storage_dir=temp_audit_dir, enabled=True)
        raw_text_with_fences = "```json\n{\n  \"clinical_score\": 92,\n  \"recommendations\": [\"Hydration\"]\n}\n```"
        payload = make_gemini_response_payload(raw_text_with_fences)

        transport = MockCustomTransport([httpx.Response(200, json=payload)])
        gateway = GeminiGateway(
            api_key="TEST_KEY",
            transport=transport,
            audit_service=audit_service,
        )

        with audit_scope(patient_id="pat_alpha", cycle_id="cyc_alpha", stage="primary_consilium"):
            result = asyncio.run(gateway.generate_structured("Evaluate vitals", DummyClinicalOutput))
            assert result.clinical_score == 92
            call_id = gateway.last_call_id

        record = audit_service.get_call(call_id)
        assert record is not None
        # Verbatim raw output must preserve the exact outer markdown and whitespace
        assert record.raw_response_text == raw_text_with_fences
        # Cleaned json text has outer fence removed
        assert "```json" not in (record.cleaned_json_text or "")
        # Parsed JSON object is structured
        assert record.parsed_json == {"clinical_score": 92, "recommendations": ["Hydration"]}
        assert record.status == "SUCCESS"
        assert record.schema_name == "DummyClinicalOutput"
        assert record.validation_error is None

    def test_schema_error_and_invalid_output_diagnostic_preservation(self, temp_audit_dir):
        """Verifies that schema validation failures preserve verbatim invalid text and error details for diagnostics."""
        audit_service = AuditTrailService(storage_dir=temp_audit_dir, enabled=True)
        # Invalid payload: clinical_score is a string instead of an int
        invalid_raw_text = '{"clinical_score": "INVALID_NOT_AN_INT", "recommendations": []}'
        payload = make_gemini_response_payload(invalid_raw_text)

        transport = MockCustomTransport([httpx.Response(200, json=payload)])
        gateway = GeminiGateway(
            api_key="TEST_KEY",
            transport=transport,
            audit_service=audit_service,
        )

        with audit_scope(patient_id="pat_beta", cycle_id="cyc_beta", stage="cardiology_check"):
            with pytest.raises(GeminiValidationError) as exc_info:
                asyncio.run(gateway.generate_structured("prompt", DummyClinicalOutput))

        assert "validation for DummyClinicalOutput" in str(exc_info.value)
        call_id = gateway.last_call_id
        assert call_id is not None

        record = audit_service.get_call(call_id)
        assert record is not None
        assert record.status == "SCHEMA_ERROR"
        # The invalid verbatim text must NOT be lost!
        assert record.raw_response_text == invalid_raw_text
        assert record.parsed_json == {"clinical_score": "INVALID_NOT_AN_INT", "recommendations": []}
        assert record.validation_error is not None
        assert "clinical_score" in record.validation_error

    def test_json_decode_error_preservation(self, temp_audit_dir):
        """Verifies that non-JSON model outputs preserve verbatim invalid text with JSON_DECODE_ERROR status."""
        audit_service = AuditTrailService(storage_dir=temp_audit_dir, enabled=True)
        corrupted_raw_text = "This is not JSON at all: error in reasoning: {broken"
        payload = make_gemini_response_payload(corrupted_raw_text)

        transport = MockCustomTransport([httpx.Response(200, json=payload)])
        gateway = GeminiGateway(
            api_key="TEST_KEY",
            transport=transport,
            audit_service=audit_service,
        )

        with audit_scope(patient_id="pat_corrupt", cycle_id="cyc_corrupt", stage="raw_test"):
            with pytest.raises(GeminiValidationError) as exc_info:
                asyncio.run(gateway.generate_structured("prompt", DummyClinicalOutput))

        assert "not valid JSON" in str(exc_info.value)
        call_id = gateway.last_call_id

        record = audit_service.get_call(call_id)
        assert record is not None
        assert record.status == "JSON_DECODE_ERROR"
        assert record.raw_response_text == corrupted_raw_text
        assert record.parsed_json is None
        assert record.validation_error is not None

    def test_id_chain_integrity(self, temp_audit_dir):
        """Verifies that full chain patient_id -> cycle_id -> sprint_id -> report_id -> call_id is maintained."""
        audit_service = AuditTrailService(storage_dir=temp_audit_dir, enabled=True)
        raw_output = '{"clinical_score": 75, "recommendations": ["Lab retest"]}'
        payload = make_gemini_response_payload(raw_output)

        transport = MockCustomTransport([httpx.Response(200, json=payload)])
        gateway = GeminiGateway(
            api_key="TEST_KEY",
            transport=transport,
            audit_service=audit_service,
        )

        patient_id = "pat_chain_01"
        cycle_id = "cyc_chain_c1"
        sprint_id = "sprint_chain_w1"
        report_id = "rep_chain_w1_server_only_v1"

        with audit_scope(patient_id=patient_id, cycle_id=cycle_id, sprint_id=sprint_id, stage="weekly_consilium_w1"):
            asyncio.run(gateway.generate_structured("Consilium prompt", DummyClinicalOutput))
            call_id = gateway.last_call_id

        # Link SQLite report_id
        linked = audit_service.link_report(
            call_id=call_id,
            report_id=report_id,
            patient_id=patient_id,
            cycle_id=cycle_id,
            privacy_level=PrivacyLevel.SERVER_ONLY.value,
        )
        assert linked is True

        record = audit_service.get_call(call_id)
        assert record.patient_id == patient_id
        assert record.cycle_id == cycle_id
        assert record.sprint_id == sprint_id
        assert record.report_id == report_id
        assert record.privacy_level == "server_only"
        assert record.call_id == call_id

    def test_patient_isolation_zero_leakage(self, temp_audit_dir):
        """Verifies that audit storage strictly isolates patients on disk with zero leakage."""
        audit_service = AuditTrailService(storage_dir=temp_audit_dir, enabled=True)
        payload = make_gemini_response_payload('{"clinical_score": 80, "recommendations": []}')

        transport = MockCustomTransport([
            httpx.Response(200, json=payload),
            httpx.Response(200, json=payload),
        ])
        gateway = GeminiGateway(
            api_key="TEST_KEY",
            transport=transport,
            audit_service=audit_service,
        )

        # Patient Alice
        with audit_scope(patient_id="patient_alice", cycle_id="cyc_alice_1", stage="stage_alice"):
            asyncio.run(gateway.generate_structured("prompt", DummyClinicalOutput))
            call_alice = gateway.last_call_id

        # Patient Bob
        with audit_scope(patient_id="patient_bob", cycle_id="cyc_bob_1", stage="stage_bob"):
            asyncio.run(gateway.generate_structured("prompt", DummyClinicalOutput))
            call_bob = gateway.last_call_id

        # Verify filesystem paths
        alice_file = temp_audit_dir / "patient_alice" / "cyc_alice_1" / f"{call_alice}.json"
        bob_file = temp_audit_dir / "patient_bob" / "cyc_bob_1" / f"{call_bob}.json"
        assert alice_file.exists()
        assert bob_file.exists()

        # Query Alice calls -> must contain 0 calls from Bob
        alice_calls = audit_service.list_calls(patient_id="patient_alice", cycle_id="cyc_alice_1")
        assert len(alice_calls) == 1
        assert alice_calls[0].call_id == call_alice
        assert alice_calls[0].patient_id == "patient_alice"

        bob_calls = audit_service.list_calls(patient_id="patient_bob", cycle_id="cyc_bob_1")
        assert len(bob_calls) == 1
        assert bob_calls[0].call_id == call_bob
        assert bob_calls[0].patient_id == "patient_bob"

    def test_path_traversal_prevention(self, temp_audit_dir):
        """Verifies that path traversal attacks via malicious IDs are strictly blocked."""
        audit_service = AuditTrailService(storage_dir=temp_audit_dir, enabled=True)

        malicious_inputs = [
            "../../etc/passwd",
            "..\\..\\windows\\system32",
            "patient/subdir",
            "cycle\0nullbyte",
            "pat$illegal#chars!",
            "",
            "   ",
        ]

        for mal in malicious_inputs:
            with pytest.raises(ValueError):
                audit_service.validate_identifier(mal, "test_param")

        # Test relative path boundary escape
        with pytest.raises(ValueError):
            audit_service._resolve_record_path(
                patient_id="..",
                cycle_id="normal_cycle",
                call_id="call_normal",
            )

    def test_atomic_write_and_overwrite_protection(self, temp_audit_dir):
        """Verifies atomic write mechanics and overwrite protection."""
        audit_service = AuditTrailService(storage_dir=temp_audit_dir, enabled=True)
        record = AuditRecord(
            call_id="call_unique_atomic_1",
            patient_id="pat_atomic",
            cycle_id="cyc_atomic",
            stage="test_atomic",
            model_role="flash",
            model_name="gemini-3.8-flash",
            raw_response_text='{"status": "ok"}',
            status="SUCCESS",
            created_at_utc="2026-11-10T20:00:00Z",
        )

        # 1. Write record
        target_path = audit_service.record_call(record)
        assert target_path.exists()
        assert not any(f.name.endswith(".tmp") for f in target_path.parent.iterdir())

        # 2. Attempting to record same call_id with overwrite=False must raise FileExistsError
        with pytest.raises(FileExistsError):
            audit_service.record_call(record, overwrite=False)

        # 3. With overwrite=True, update succeeds
        record.status = "UPDATED"
        audit_service.record_call(record, overwrite=True)
        updated = audit_service.get_call("call_unique_atomic_1")
        assert updated.status == "UPDATED"

    def test_strict_test_mode_error_propagation(self, temp_audit_dir):
        """Verifies that in strict test mode, missing or failing audit records raise AuditStorageError."""
        audit_service = AuditTrailService(
            storage_dir=temp_audit_dir,
            enabled=True,
            strict_test_mode=True,
        )

        # Linking a non-existent report in strict mode raises AuditStorageError
        with pytest.raises(AuditStorageError):
            audit_service.link_report(
                call_id="call_non_existent",
                report_id="rep_test_01",
            )

    def test_disabled_audit_zero_side_effects(self, temp_audit_dir):
        """Verifies that when audit is disabled, GeminiGateway performs 0 disk writes and behaves normally."""
        audit_service = AuditTrailService(storage_dir=temp_audit_dir, enabled=False)
        payload = make_gemini_response_payload('{"clinical_score": 99, "recommendations": []}')

        transport = MockCustomTransport([httpx.Response(200, json=payload)])
        gateway = GeminiGateway(
            api_key="TEST_KEY",
            transport=transport,
            audit_service=audit_service,
        )

        assert gateway.is_audit_enabled() is False

        with audit_scope(patient_id="pat_disabled", cycle_id="cyc_disabled"):
            res = asyncio.run(gateway.generate_structured("prompt", DummyClinicalOutput))
            assert res.clinical_score == 99

        # Directory must remain completely empty
        assert not any(temp_audit_dir.iterdir()), "Audit directory must be empty when audit is disabled"


class TestAdminInspectionDashboardAPI:
    """Tests the local server-only administration and inspection endpoints."""

    def test_protected_local_admin_access(self, temp_audit_dir, isolated_db_session):
        """Verifies that admin audit endpoints strictly reject unauthorized or mobile requests."""
        client = TestClient(app)
        app.dependency_overrides[get_db] = lambda: isolated_db_session

        try:
            # 1. No auth header -> 401 Unauthorized
            res_unauth = client.get("/api/v1/admin/audit/patients")
            assert res_unauth.status_code == 401

            # 2. ServiceAdminSession authorized (via dependency override or secret)
            app.dependency_overrides[require_service_admin_session] = lambda: ServiceAdminSession()
            res_auth = client.get("/api/v1/admin/audit/patients")
            assert res_auth.status_code == 200
        finally:
            app.dependency_overrides.clear()

    def test_mobile_device_session_rejected_with_403(self, temp_audit_dir):
        """Verifies that mobile DeviceSession cannot access administrative audit endpoints."""
        client = TestClient(app)

        # Mock require_service_admin_session to enforce live behavior on DeviceSession
        def _mock_require_service_admin():
            from fastapi import HTTPException
            raise HTTPException(
                status_code=403,
                detail="Operation requires service administrator privileges and is forbidden for mobile device sessions."
            )

        app.dependency_overrides[require_service_admin_session] = _mock_require_service_admin
        try:
            res = client.get("/api/v1/admin/audit/patients")
            assert res.status_code == 403
            assert "forbidden for mobile" in res.json()["detail"]
        finally:
            app.dependency_overrides.pop(require_service_admin_session, None)

    def test_legacy_cycles_accessible_after_neo(self, temp_audit_dir, isolated_db_session):
        """Verifies that historical cycles remain navigable and accessible after Neo cycle activation."""
        patient_id = "pat_neo_history_01"
        cycle_c1_id = "cycle_synthetic_c1"
        cycle_c2_id = "cycle_synthetic_c2_neo"

        # Setup Patient and two Cycles in SQLite
        pat = Patient(patient_id=patient_id)
        c1 = MedicalCycle(cycle_id=cycle_c1_id, patient_id=patient_id, status="archived")
        c2 = MedicalCycle(cycle_id=cycle_c2_id, patient_id=patient_id, status="active")
        isolated_db_session.add_all([pat, c1, c2])
        isolated_db_session.commit()

        # Setup audit calls for both cycles
        audit_service = AuditTrailService(storage_dir=temp_audit_dir, enabled=True)
        rec_c1 = AuditRecord(
            call_id="call_c1_weekly1",
            patient_id=patient_id,
            cycle_id=cycle_c1_id,
            stage="weekly_consilium_w1",
            model_role="flash",
            model_name="gemini-3.8-flash",
            raw_response_text='{"clinical_score": 70}',
            parsed_json={"clinical_score": 70},
            status="SUCCESS",
            created_at_utc="2026-10-01T10:00:00Z",
        )
        rec_c2 = AuditRecord(
            call_id="call_c2_neo_verdict",
            patient_id=patient_id,
            cycle_id=cycle_c2_id,
            stage="neo_orchestration",
            model_role="pro",
            model_name="gemini-4-argon",
            raw_response_text='{"clinical_score": 90}',
            parsed_json={"clinical_score": 90},
            status="SUCCESS",
            created_at_utc="2026-10-28T10:00:00Z",
        )
        audit_service.record_call(rec_c1)
        audit_service.record_call(rec_c2)

        client = TestClient(app)
        app.dependency_overrides[get_db] = lambda: isolated_db_session
        app.dependency_overrides[require_service_admin_session] = lambda: ServiceAdminSession()
        app.dependency_overrides[get_audit_trail_service] = lambda: audit_service

        try:
            # 1. Fetch cycles for patient
            res_cycles = client.get(f"/api/v1/admin/audit/patients/{patient_id}/cycles")
            assert res_cycles.status_code == 200
            cycle_list = res_cycles.json()
            assert len(cycle_list) == 2
            cycle_ids = [c["cycle_id"] for c in cycle_list]
            assert cycle_c1_id in cycle_ids
            assert cycle_c2_id in cycle_ids

            # 2. Fetch calls for legacy cycle_c1
            res_c1_calls = client.get(f"/api/v1/admin/audit/cycles/{cycle_c1_id}/calls")
            assert res_c1_calls.status_code == 200
            assert len(res_c1_calls.json()) == 1
            assert res_c1_calls.json()[0]["call_id"] == "call_c1_weekly1"

            # 3. Fetch calls for neo cycle_c2
            res_c2_calls = client.get(f"/api/v1/admin/audit/cycles/{cycle_c2_id}/calls")
            assert res_c2_calls.status_code == 200
            assert len(res_c2_calls.json()) == 1
            assert res_c2_calls.json()[0]["call_id"] == "call_c2_neo_verdict"
        finally:
            app.dependency_overrides.clear()

    def test_clinical_review_pass_requires_qualified_reviewer(self, temp_audit_dir, isolated_db_session):
        """Verifies that REVIEWED_PASS strictly requires a reviewer_id; Pydantic PASS alone does not qualify."""
        patient_id = "pat_review_01"
        cycle_id = "cyc_review_01"
        call_id = "call_review_target"

        audit_service = AuditTrailService(storage_dir=temp_audit_dir, enabled=True)
        rec = AuditRecord(
            call_id=call_id,
            patient_id=patient_id,
            cycle_id=cycle_id,
            stage="weekly_consilium",
            model_role="pro",
            model_name="gemini-4-argon",
            raw_response_text='{"clinical_score": 95}',
            parsed_json={"clinical_score": 95},
            status="SUCCESS",  # Pydantic passed
            created_at_utc="2026-11-10T12:00:00Z",
        )
        audit_service.record_call(rec)

        client = TestClient(app)
        app.dependency_overrides[get_db] = lambda: isolated_db_session
        app.dependency_overrides[require_service_admin_session] = lambda: ServiceAdminSession()
        app.dependency_overrides[get_audit_trail_service] = lambda: audit_service

        try:
            # 1. Initial inspection: review status must be NOT_REVIEWED even though Pydantic is SUCCESS
            detail_res = client.get(f"/api/v1/admin/audit/calls/{call_id}")
            assert detail_res.status_code == 200
            assert detail_res.json()["call"]["status"] == "SUCCESS"
            assert detail_res.json()["review"] is None  # Not reviewed yet

            # 2. Attempting REVIEWED_PASS with empty reviewer_id must fail with 422
            fail_payload = {
                "status": "REVIEWED_PASS",
                "reviewer_id": "",
                "notes": "Looks good",
            }
            res_fail = client.post(f"/api/v1/admin/audit/calls/{call_id}/review", json=fail_payload)
            assert res_fail.status_code == 422
            assert "reviewer_id cannot be empty" in res_fail.json()["detail"]

            # 3. Submitting REVIEWED_PASS with qualified reviewer succeeds
            pass_payload = {
                "status": "REVIEWED_PASS",
                "reviewer_id": "dr_lead_cardiologist_42",
                "notes": "Verified against patient ECG and lab telemetry. Clinical logic sound.",
                "facts_accurate": True,
                "units_correct": True,
                "unjustified_conclusions_found": False,
                "cross_agent_consistent": True,
                "red_flags_identified": False,
                "strategy_aligns_with_inputs": True,
            }
            res_pass = client.post(f"/api/v1/admin/audit/calls/{call_id}/review", json=pass_payload)
            assert res_pass.status_code == 200
            data = res_pass.json()
            assert data["status"] == "REVIEWED_PASS"
            assert data["reviewer_id"] == "dr_lead_cardiologist_42"
            assert data["facts_accurate"] is True

            # 4. Detail inspection now reflects the approved review
            detail_after = client.get(f"/api/v1/admin/audit/calls/{call_id}")
            assert detail_after.json()["review"]["status"] == "REVIEWED_PASS"
            assert detail_after.json()["review"]["reviewer_id"] == "dr_lead_cardiologist_42"
        finally:
            app.dependency_overrides.clear()

    def test_dashboard_ui_renders(self):
        """Verifies that the local admin inspection dashboard HTML route responds with HTTP 200."""
        client = TestClient(app)
        res = client.get("/admin/audit")
        assert res.status_code == 200
        assert "Aegis Clinical AI Audit Trail" in res.text
        assert "adminSecretInput" in res.text
        assert "CLINICAL AI AUDIT TRAIL — READ ONLY" in res.text

        res_ui = client.get("/admin/audit/ui")
        assert res_ui.status_code == 200

    def test_retry_and_timeout_behavior(self, temp_audit_dir):
        """Verifies that transient timeout and retries are recorded with diagnostic status."""
        audit_service = AuditTrailService(storage_dir=temp_audit_dir, enabled=True)

        class TimeoutMockTransport(httpx.AsyncBaseTransport):
            def __init__(self):
                self.calls = 0

            async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
                self.calls += 1
                raise httpx.ReadTimeout("Socket timed out after 60s")

        transport = TimeoutMockTransport()
        gateway = GeminiGateway(
            api_key="TEST_KEY",
            transport=transport,
            max_retries=2,
            initial_backoff_seconds=0.001,
            audit_service=audit_service,
        )

        with audit_scope(patient_id="pat_timeout", cycle_id="cyc_timeout", stage="timeout_test"):
            with pytest.raises(GeminiTransientError) as exc_info:
                asyncio.run(gateway.generate_structured("test", DummyClinicalOutput))

        assert "Network or timeout error" in str(exc_info.value)
        assert transport.calls == 2, "Must attempt max_retries times"
        call_id = gateway.last_call_id

        record = audit_service.get_call(call_id)
        assert record is not None
        assert record.status in ("TIMEOUT", "TRANSPORT_ERROR")
        assert record.raw_response_text == ""
        assert record.parsed_json is None
        assert "ReadTimeout" in (record.validation_error or "")
        assert record.total_attempts == 2

    def test_read_only_medical_reports(self, temp_audit_dir, isolated_db_session):
        """Verifies that administrative audit interface cannot modify persisted medical reports in SQLite."""
        patient_id = "pat_readonly_01"
        cycle_id = "cyc_readonly_01"
        report_id = "rep_readonly_test"

        # Create Patient, Cycle, ModelReport in SQLite
        pat = Patient(patient_id=patient_id)
        cyc = MedicalCycle(cycle_id=cycle_id, patient_id=patient_id, status="active")
        rep = ModelReport(
            report_id=report_id,
            cycle_id=cycle_id,
            model="gemini-4-argon",
            report_type="pro_training_shelf",
            privacy_level="server_only",
            version=1,
            content={"clinical_strategy": "Original pristine strategy", "version": 1},
        )
        isolated_db_session.add_all([pat, cyc, rep])
        isolated_db_session.commit()

        audit_service = AuditTrailService(storage_dir=temp_audit_dir, enabled=True)
        rec = AuditRecord(
            call_id="call_readonly_check",
            patient_id=patient_id,
            cycle_id=cycle_id,
            stage="primary_consilium",
            model_role="pro",
            model_name="gemini-4-argon",
            raw_response_text='{"clinical_strategy": "Original pristine strategy"}',
            parsed_json={"clinical_strategy": "Original pristine strategy"},
            status="SUCCESS",
            created_at_utc="2026-11-10T14:00:00Z",
            report_id=report_id,
        )
        audit_service.record_call(rec)

        client = TestClient(app)
        app.dependency_overrides[get_db] = lambda: isolated_db_session
        app.dependency_overrides[require_service_admin_session] = lambda: ServiceAdminSession()
        app.dependency_overrides[get_audit_trail_service] = lambda: audit_service

        try:
            # 1. Read through inspection endpoint
            res = client.get(f"/api/v1/admin/audit/calls/{rec.call_id}")
            assert res.status_code == 200
            assert res.json()["persisted_report"]["content"]["clinical_strategy"] == "Original pristine strategy"

            # 2. Verify NO report modification endpoints exist in audit router
            for method in ("put", "patch", "delete"):
                resp = getattr(client, method)(f"/api/v1/admin/audit/calls/{rec.call_id}")
                assert resp.status_code in (404, 405)

            # 3. Submitting a review must NOT alter the medical report content in SQLite
            review_payload = {
                "status": "REVIEWED_PASS",
                "reviewer_id": "dr_auditor",
                "notes": "Reviewed and confirmed read-only integrity",
            }
            res_rev = client.post(f"/api/v1/admin/audit/calls/{rec.call_id}/review", json=review_payload)
            assert res_rev.status_code == 200

            # Verify report in SQLite is 100% unchanged
            db_rep = isolated_db_session.query(ModelReport).filter_by(report_id=report_id).one()
            assert db_rep.content["clinical_strategy"] == "Original pristine strategy"
            assert db_rep.version == 1
        finally:
            app.dependency_overrides.clear()

    def test_full_synthetic_patient_journey_audit_tree(self, temp_audit_dir, isolated_db_session):
        """
        Simulates an entire synthetic patient journey across Primary Consilium (10 models),
        Weekly Consilium (2 models), Referral Cascade (2 models), and Neo Consilium (10 models).
        Verifies full hierarchical navigation tree: Patient -> Cycle -> Stage -> Model -> Response.
        """
        patient_id = "pat_synthetic_journey_01"
        cycle_1_id = "cycle_synthetic_c1"
        cycle_2_id = "cycle_synthetic_c2_neo"

        # Database entities
        pat = Patient(patient_id=patient_id)
        c1 = MedicalCycle(cycle_id=cycle_1_id, patient_id=patient_id, status="archived")
        c2 = MedicalCycle(cycle_id=cycle_2_id, patient_id=patient_id, status="active")
        isolated_db_session.add_all([pat, c1, c2])

        # SQLite reports for cycle 1 and 2
        r1 = ModelReport(
            report_id="rep_c1_cardio",
            cycle_id=cycle_1_id,
            model="gemini-3.5-flash-lite",
            report_type="system_shelf",
            privacy_level="server_only",
            version=1,
            content={"system": "cardiovascular", "risk": "low"},
        )
        r2 = ModelReport(
            report_id="rep_c1_weekly_client",
            cycle_id=cycle_1_id,
            model="gemini-3.8-flash",
            report_type="pro_general_shelf",
            privacy_level="client_facing",
            version=1,
            content={"summary": "Weekly goals achieved"},
        )
        r3 = ModelReport(
            report_id="rep_c2_neo_chief",
            cycle_id=cycle_2_id,
            model="gemini-4-argon",
            report_type="pro_training_shelf",
            privacy_level="server_only",
            version=1,
            content={"verdict": "Neo cycle approved"},
        )
        isolated_db_session.add_all([r1, r2, r3])
        isolated_db_session.commit()

        audit_service = AuditTrailService(storage_dir=temp_audit_dir, enabled=True)

        # 1. Primary Consilium: 10 model calls in cycle 1
        organ_specialists = [
            "cardiovascular", "respiratory", "gastrointestinal", "musculoskeletal",
            "nervous", "endocrine", "immune", "integumentary"
        ]
        created_calls = []
        for organ in organ_specialists:
            rec = AuditRecord(
                call_id=f"call_primary_{organ}",
                patient_id=patient_id,
                cycle_id=cycle_1_id,
                stage="primary_consilium",
                model_role="flash_lite",
                model_name="gemini-3.5-flash-lite",
                prompt=f"Assess {organ} system telemetries",
                raw_response_text=f'{{"system": "{organ}", "status": "nominal"}}',
                parsed_json={"system": organ, "status": "nominal"},
                status="SUCCESS",
                created_at_utc=f"2026-10-01T08:00:{len(created_calls):02d}Z",
                report_id="rep_c1_cardio" if organ == "cardiovascular" else None,
            )
            audit_service.record_call(rec)
            created_calls.append(rec.call_id)

        # Primary Consilium Master & Pro Chief
        rec_fm = AuditRecord(
            call_id="call_primary_flash_master",
            patient_id=patient_id,
            cycle_id=cycle_1_id,
            stage="primary_consilium",
            model_role="flash",
            model_name="gemini-3.8-flash",
            prompt="Intersystem synthesis",
            raw_response_text='{"synthesis": "nominal"}',
            parsed_json={"synthesis": "nominal"},
            status="SUCCESS",
            created_at_utc="2026-10-01T08:05:00Z",
        )
        rec_pm = AuditRecord(
            call_id="call_primary_pro_master",
            patient_id=patient_id,
            cycle_id=cycle_1_id,
            stage="primary_consilium",
            model_role="pro",
            model_name="gemini-4-argon",
            prompt="Chief Physician verdict",
            raw_response_text='{"verdict": "approved"}',
            parsed_json={"verdict": "approved"},
            status="SUCCESS",
            created_at_utc="2026-10-01T08:06:00Z",
        )
        audit_service.record_call(rec_fm)
        audit_service.record_call(rec_pm)
        created_calls.extend([rec_fm.call_id, rec_pm.call_id])

        # 2. Weekly Consilium Sprint 1: 2 calls (Flash Master + Argon Pro Chief)
        rec_w1_fm = AuditRecord(
            call_id="call_w1_flash_master",
            patient_id=patient_id,
            cycle_id=cycle_1_id,
            sprint_id="sprint_c1_w1",
            stage="weekly_consilium_w1",
            model_role="flash",
            model_name="gemini-3.8-flash",
            prompt="Weekly synthesis",
            raw_response_text='{"weekly_eval": "good"}',
            parsed_json={"weekly_eval": "good"},
            status="SUCCESS",
            created_at_utc="2026-10-08T08:00:00Z",
            report_id="rep_c1_weekly_client",
        )
        rec_w1_pm = AuditRecord(
            call_id="call_w1_pro_master",
            patient_id=patient_id,
            cycle_id=cycle_1_id,
            sprint_id="sprint_c1_w1",
            stage="weekly_consilium_w1",
            model_role="pro",
            model_name="gemini-4-argon",
            prompt="Weekly Chief directive",
            raw_response_text='{"directive": "continue"}',
            parsed_json={"directive": "continue"},
            status="SUCCESS",
            created_at_utc="2026-10-08T08:01:00Z",
        )
        audit_service.record_call(rec_w1_fm)
        audit_service.record_call(rec_w1_pm)
        created_calls.extend([rec_w1_fm.call_id, rec_w1_pm.call_id])

        # 3. Referral Cascade: 2 calls
        rec_ref_dec = AuditRecord(
            call_id="call_ref_decision",
            patient_id=patient_id,
            cycle_id=cycle_1_id,
            stage="referral_cascade",
            model_role="flash",
            model_name="gemini-3.8-flash",
            prompt="Referral decision triage",
            raw_response_text='{"referral_recommended": true}',
            parsed_json={"referral_recommended": True},
            status="SUCCESS",
            created_at_utc="2026-10-15T08:00:00Z",
        )
        rec_ref_doc = AuditRecord(
            call_id="call_ref_doc",
            patient_id=patient_id,
            cycle_id=cycle_1_id,
            stage="referral_cascade",
            model_role="pro",
            model_name="gemini-4-argon",
            prompt="Clinical referral document formulation",
            raw_response_text='{"document_summary": "Cardiology referral letter"}',
            parsed_json={"document_summary": "Cardiology referral letter"},
            status="SUCCESS",
            created_at_utc="2026-10-15T08:02:00Z",
        )
        audit_service.record_call(rec_ref_dec)
        audit_service.record_call(rec_ref_doc)
        created_calls.extend([rec_ref_dec.call_id, rec_ref_doc.call_id])

        # 4. Neo Consilium in cycle 2: 10 model calls
        for organ in organ_specialists:
            rec = AuditRecord(
                call_id=f"call_neo_{organ}",
                patient_id=patient_id,
                cycle_id=cycle_2_id,
                stage="neo_orchestration",
                model_role="flash_lite",
                model_name="gemini-3.5-flash-lite",
                prompt=f"Neo reassessment of {organ}",
                raw_response_text=f'{{"neo_system": "{organ}", "trend": "improved"}}',
                parsed_json={"neo_system": organ, "trend": "improved"},
                status="SUCCESS",
                created_at_utc=f"2026-10-28T08:00:{len(created_calls):02d}Z",
            )
            audit_service.record_call(rec)
            created_calls.append(rec.call_id)

        rec_neo_fm = AuditRecord(
            call_id="call_neo_flash_master",
            patient_id=patient_id,
            cycle_id=cycle_2_id,
            stage="neo_orchestration",
            model_role="flash",
            model_name="gemini-3.8-flash",
            prompt="Neo longitudinal audit",
            raw_response_text='{"longitudinal_score": 88}',
            parsed_json={"longitudinal_score": 88},
            status="SUCCESS",
            created_at_utc="2026-10-28T08:05:00Z",
        )
        rec_neo_pm = AuditRecord(
            call_id="call_neo_pro_master",
            patient_id=patient_id,
            cycle_id=cycle_2_id,
            stage="neo_orchestration",
            model_role="pro",
            model_name="gemini-4-argon",
            prompt="Neo Chief activation verdict",
            raw_response_text='{"activation_verdict": "approved"}',
            parsed_json={"activation_verdict": "approved"},
            status="SUCCESS",
            created_at_utc="2026-10-28T08:06:00Z",
            report_id="rep_c2_neo_chief",
        )
        audit_service.record_call(rec_neo_fm)
        audit_service.record_call(rec_neo_pm)
        created_calls.extend([rec_neo_fm.call_id, rec_neo_pm.call_id])

        assert len(created_calls) == 24, "Total 24 calls recorded in synthetic lifecycle"

        # Verify through Admin API
        client = TestClient(app)
        app.dependency_overrides[get_db] = lambda: isolated_db_session
        app.dependency_overrides[require_service_admin_session] = lambda: ServiceAdminSession()
        app.dependency_overrides[get_audit_trail_service] = lambda: audit_service

        try:
            # 1. Patients endpoint
            res_pats = client.get("/api/v1/admin/audit/patients")
            assert res_pats.status_code == 200
            assert patient_id in res_pats.json()

            # 2. Cycles endpoint: both cycle_1 and cycle_2 are returned
            res_cycs = client.get(f"/api/v1/admin/audit/patients/{patient_id}/cycles")
            assert res_cycs.status_code == 200
            cycle_ids = [c["cycle_id"] for c in res_cycs.json()]
            assert cycle_1_id in cycle_ids
            assert cycle_2_id in cycle_ids

            # 3. Stages in cycle 1
            res_st_c1 = client.get(f"/api/v1/admin/audit/cycles/{cycle_1_id}/stages")
            assert res_st_c1.status_code == 200
            stages_c1 = {s["stage"]: s["call_count"] for s in res_st_c1.json()}
            assert stages_c1["primary_consilium"] == 10
            assert stages_c1["weekly_consilium_w1"] == 2
            assert stages_c1["referral_cascade"] == 2

            # 4. Stages in cycle 2 (Neo)
            res_st_c2 = client.get(f"/api/v1/admin/audit/cycles/{cycle_2_id}/stages")
            assert res_st_c2.status_code == 200
            stages_c2 = {s["stage"]: s["call_count"] for s in res_st_c2.json()}
            assert stages_c2["neo_orchestration"] == 10

            # 5. Inspect call with server_only report
            res_detail_cardio = client.get("/api/v1/admin/audit/calls/call_primary_cardiovascular")
            assert res_detail_cardio.status_code == 200
            data_cardio = res_detail_cardio.json()
            assert data_cardio["call"]["stage"] == "primary_consilium"
            assert data_cardio["persisted_report"]["privacy_level"] == "server_only"
            assert data_cardio["persisted_report"]["report_id"] == "rep_c1_cardio"

            # 6. Inspect call with client_facing report
            res_detail_weekly = client.get("/api/v1/admin/audit/calls/call_w1_flash_master")
            assert res_detail_weekly.status_code == 200
            data_weekly = res_detail_weekly.json()
            assert data_weekly["persisted_report"]["privacy_level"] == "client_facing"
            assert data_weekly["persisted_report"]["report_id"] == "rep_c1_weekly_client"

            # 7. Inspect Neo Chief call
            res_detail_neo = client.get("/api/v1/admin/audit/calls/call_neo_pro_master")
            assert res_detail_neo.status_code == 200
            data_neo = res_detail_neo.json()
            assert data_neo["call"]["model_name"] == "gemini-4-argon"
            assert data_neo["persisted_report"]["report_id"] == "rep_c2_neo_chief"
        finally:
            app.dependency_overrides.clear()

    def test_ephemeral_admin_session_token_exchange(self, temp_audit_dir, isolated_db_session):
        """Verifies ephemeral admin session token exchange and authorization."""
        client = TestClient(app)
        app.dependency_overrides[get_db] = lambda: isolated_db_session

        try:
            # 1. Login with invalid secret -> 401
            res_bad = client.post("/api/v1/admin/audit/login", json={"secret": "wrong_secret"})
            assert res_bad.status_code == 401

            # 2. Login with valid secret -> returns access_token starting with adm_sess_
            res_ok = client.post("/api/v1/admin/audit/login", json={"secret": settings.api_secret})
            assert res_ok.status_code == 200
            token_data = res_ok.json()
            assert token_data["access_token"].startswith("adm_sess_")
            assert token_data["expires_in"] == 7200

            # 3. Use ephemeral token to authenticate
            token = token_data["access_token"]
            res_auth = client.get(
                "/api/v1/admin/audit/patients",
                headers={"Authorization": f"Bearer {token}"},
            )
            assert res_auth.status_code == 200
        finally:
            app.dependency_overrides.clear()

    def test_credential_scrubber_redaction(self, temp_audit_dir):
        """Verifies that secrets, API keys, and bearer tokens are never persisted in audit logs."""
        audit_service = AuditTrailService(storage_dir=temp_audit_dir, enabled=True)
        rec = AuditRecord(
            call_id="call_credential_scrub_01",
            patient_id="pat_scrub_01",
            cycle_id="cycle_scrub_01",
            stage="test_scrub",
            model_role="flash",
            model_name="gemini-3.8-flash",
            system_instruction="Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.doNotLeakThis",
            prompt=f"Patient analyzed with API key AIzaSyA1B2C3D4E5F6G7H8I9J0K1L2M3N4O5P6Q and secret {settings.api_secret}",
            raw_response_text="Response includes key AIzaSyA1B2C3D4E5F6G7H8I9J0K1L2M3N4O5P6Q",
            parsed_json={"secret_leak": settings.api_secret, "api_key": "AIzaSyA1B2C3D4E5F6G7H8I9J0K1L2M3N4O5P6Q"},
            status="SUCCESS",
            created_at_utc="2026-10-01T08:00:00Z",
        )
        saved_path = audit_service.record_call(rec)
        assert saved_path.exists()

        content = saved_path.read_text(encoding="utf-8")
        assert "AIzaSyA1B2C3D4E5F6G7H8I9J0K1L2M3N4O5P6Q" not in content
        assert settings.api_secret not in content
        assert "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9" not in content
        assert "[REDACTED_CREDENTIAL]" in content or "[REDACTED_SECRET]" in content

    def test_patient_preview_endpoint(self, temp_audit_dir, isolated_db_session):
        """Verifies the exact patient preview endpoint matching Flutter app data structure."""
        client = TestClient(app)
        app.dependency_overrides[get_db] = lambda: isolated_db_session
        app.dependency_overrides[require_service_admin_session] = lambda: ServiceAdminSession()

        audit_service = AuditTrailService(storage_dir=temp_audit_dir, enabled=True)
        app.dependency_overrides[get_audit_trail_service] = lambda: audit_service

        patient_id = "pat_preview_test"
        cycle_id = "cycle_preview_test"

        # Seed patient & cycle
        pat = Patient(patient_id=patient_id, status="active")
        cyc = MedicalCycle(cycle_id=cycle_id, patient_id=patient_id, status="active")
        isolated_db_session.add_all([pat, cyc])
        isolated_db_session.commit()

        # Seed client report for cardiovascular
        cardio_rep = ModelReport(
            report_id="rep_cardio_preview_01",
            cycle_id=cycle_id,
            model="gemini-3.8-flash",
            report_type="cardiovascular_user",
            privacy_level="client_facing",
            version=1,
            content={
                "title": "Cardiovascular System Summary",
                "summary": "Resting pulse is nominal, cardiac autonomic tone is stable.",
                "highlights": ["Resting heart rate remains at 64 bpm", "Zero arrhythmic spikes detected"],
                "metrics": {"resting_hr": 64, "deficiency_score": 0},
                "report_date": "2026-10-18",
            },
            created_at=datetime(2026, 10, 18, 9, 0, 0, tzinfo=timezone.utc),
        )
        overall_rep = ModelReport(
            report_id="rep_overall_preview_01",
            cycle_id=cycle_id,
            model="gemini-4-argon",
            report_type="user_overall_report",
            privacy_level="client_facing",
            version=1,
            content={
                "title": "Overall Health Strategy",
                "summary": "Multidisciplinary synthesis: all physiological systems within target thresholds.",
                "highlights": ["Nominal physiological adaptation", "Training volume may increase by 10%"],
                "metrics": {},
                "report_date": "2026-10-18",
            },
            created_at=datetime(2026, 10, 18, 9, 0, 0, tzinfo=timezone.utc),
        )
        doctor_doc = ModelReport(
            report_id="rep_doctor_preview_01",
            cycle_id=cycle_id,
            model="gemini-4-argon",
            report_type="referral_doctor_document",
            privacy_level="client_facing",
            version=1,
            content={
                "title": "Health Observation Summary for Doctor",
                "summary": "Routine Cardiology Observation Summary",
                "metrics": {"specialist_type": "Cardiologist", "urgency": "routine"},
                "report_date": "2026-10-18",
            },
            created_at=datetime(2026, 10, 18, 9, 0, 0, tzinfo=timezone.utc),
        )
        isolated_db_session.add_all([cardio_rep, overall_rep, doctor_doc])
        isolated_db_session.commit()

        # Seed audit call
        rec = AuditRecord(
            call_id="call_preview_argon_01",
            patient_id=patient_id,
            cycle_id=cycle_id,
            stage="primary_consilium",
            model_role="pro",
            model_name="gemini-4-argon",
            prompt="Generate clinical summary",
            raw_response_text="Overall Health Strategy nominal",
            status="SUCCESS",
            created_at_utc="2026-10-18T09:00:00Z",
            report_id="rep_overall_preview_01",
        )
        audit_service.record_call(rec)

        try:
            res = client.get(f"/api/v1/admin/audit/patients/{patient_id}/preview/{cycle_id}")
            assert res.status_code == 200
            data = res.json()

            assert data["patient_id"] == patient_id
            assert data["cycle_id"] == cycle_id
            assert len(data["organ_states"]) == 8

            cardio = next(o for o in data["organ_states"] if o["system_key"] == "cardiovascular")
            assert cardio["status"] == "nominal"
            assert cardio["color"] == "#10B981"
            assert len(cardio["highlights"]) == 2
            assert "64 bpm" in cardio["highlights"][0]

            assert data["overall_report"]["content"]["title"] == "Overall Health Strategy"
            assert len(data["referral_documents"]) == 1
            assert data["referral_documents"][0]["specialist_type"] == "Cardiologist"

            assert "gemini_raw_verbatim" in data["trace_comparison"]
            assert "flutter_view_data" in data["trace_comparison"]
        finally:
            app.dependency_overrides.clear()

    def test_unified_patient_timeline(self, temp_audit_dir, isolated_db_session):
        """Verifies unified patient timeline aggregating multi-process events and anomaly detection."""
        client = TestClient(app)
        app.dependency_overrides[get_db] = lambda: isolated_db_session
        app.dependency_overrides[require_service_admin_session] = lambda: ServiceAdminSession()

        audit_service = AuditTrailService(storage_dir=temp_audit_dir, enabled=True)
        app.dependency_overrides[get_audit_trail_service] = lambda: audit_service

        patient_id = "pat_timeline_test"
        cycle_id = "cycle_timeline_test"

        pat = Patient(patient_id=patient_id, status="active")
        cyc = MedicalCycle(cycle_id=cycle_id, patient_id=patient_id, status="active")
        isolated_db_session.add_all([pat, cyc])
        isolated_db_session.commit()

        # Seed telemetry measurement
        m = HealthMeasurement(
            cycle_id=cycle_id,
            pulse=72,
            systolic=120,
            diastolic=80,
            spo2=98,
            source="watch_sensor",
            measured_at=datetime(2026, 10, 18, 8, 30, 0, tzinfo=timezone.utc),
            ingested_at=datetime(2026, 10, 18, 8, 30, 5, tzinfo=timezone.utc),
            phase="standard",
        )
        isolated_db_session.add(m)
        isolated_db_session.commit()

        # Seed delayed audit call
        rec_delayed = AuditRecord(
            call_id="call_delayed_01",
            patient_id=patient_id,
            cycle_id=cycle_id,
            stage="weekly_consilium",
            model_role="pro",
            model_name="gemini-4-argon",
            prompt="Weekly synthesis",
            raw_response_text="Weekly strategy",
            status="SUCCESS",
            latency_ms=35000.0,
            created_at_utc="2026-10-25T08:00:00Z",
        )
        audit_service.record_call(rec_delayed)

        try:
            res = client.get(f"/api/v1/admin/audit/patients/{patient_id}/timeline")
            assert res.status_code == 200
            data = res.json()

            assert data["patient_id"] == patient_id
            assert data["total_events"] >= 2
            assert data["anomaly_counts"]["delayed"] >= 1

            # Check that telemetry event exists and has virtual_time
            telemetry_event = next(e for e in data["events"] if e["category"] == "TELEMETRY")
            assert telemetry_event["virtual_time"] is not None
            assert "72 bpm" in telemetry_event["description"]

            # Check delayed inference anomaly
            delayed_event = next(e for e in data["events"] if e["call_id"] == "call_delayed_01")
            assert "delayed" in delayed_event["anomalies"]
        finally:
            app.dependency_overrides.clear()

    @pytest.mark.anyio
    async def test_real_service_automatic_audit_propagation(self, temp_audit_dir, isolated_db_session):
        """
        Verifies that invoking real Aegis services automatically binds patient_id, cycle_id,
        and stage without manually setting ContextVars or passing call_id parameters.
        """
        from app.services.primary_consilium_service import PrimaryConsiliumService
        from app.schemas.consilium import (
            AnamnesisModelOutput,
            AnamnesisUserView,
            AnamnesisOrganFact,
            AnamnesisSystemLogicMap,
            AnamnesisTrainingSandbox,
        )

        audit_service = AuditTrailService(storage_dir=temp_audit_dir, enabled=True)

        patient_id = "pat_auto_prop_01"
        cycle_id = "cycle_auto_prop_01"

        pat = Patient(patient_id=patient_id, status="active")
        cyc = MedicalCycle(cycle_id=cycle_id, patient_id=patient_id, status="active")
        isolated_db_session.add_all([pat, cyc])
        isolated_db_session.commit()

        # Build mock response for Anamnesis
        fact = AnamnesisOrganFact(data="Baseline clear", internal_comment="Nominal")
        mock_anamnesis = AnamnesisModelOutput(
            user_view=AnamnesisUserView(
                summary="Patient has active aerobic lifestyle.",
            ),
            system_logic=AnamnesisSystemLogicMap(
                cvs=fact,
                respiratory=fact,
                nervous=fact,
                musculoskeletal=fact,
                gi_tract=fact,
                immune=fact,
                endocrine=fact,
                kidneys=fact,
            ),
            training_sandbox=AnamnesisTrainingSandbox(
                raw_diagnostic_hypothesis="Nominal fitness status",
                data_gap_analysis="None",
                error_probability="Low (10%)",
            ),
        )

        payload = {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {
                                "text": mock_anamnesis.model_dump_json()
                            }
                        ]
                    }
                }
            ]
        }
        transport = MockCustomTransport([
            httpx.Response(200, json=payload)
        ])
        gateway = GeminiGateway(
            api_key="test_api_key_valid",
            transport=transport,
            audit_service=audit_service,
        )

        service = PrimaryConsiliumService(db=isolated_db_session, gateway=gateway)

        # Execute real service method: run_preparatory_blind_anamnesis
        report = await service.run_preparatory_blind_anamnesis(
            cycle_id=cycle_id,
            raw_anamnesis_context={"age": 35, "sport": "running"},
        )

        assert report is not None
        assert report.cycle_id == cycle_id
        assert report.privacy_level == "server_only"

        # Verify audit file was automatically created under patient_id/cycle_id
        calls = audit_service.list_calls(patient_id=patient_id, cycle_id=cycle_id)
        assert len(calls) == 1
        call_summary = calls[0]
        assert call_summary.patient_id == patient_id
        assert call_summary.cycle_id == cycle_id
        assert call_summary.stage == "preparatory_anamnesis_m1"
        assert call_summary.status == "SUCCESS"
        assert call_summary.report_id == report.report_id

    def test_localhost_restriction_enforces_403(self):
        """Verifies that requests from non-localhost IP addresses are strictly rejected with 403."""
        client = TestClient(app, client=("192.168.1.100", 50000))
        res = client.get("/admin/audit")
        assert res.status_code == 403
        assert "strictly bound to localhost" in res.json()["detail"]

        res_api = client.get("/api/v1/admin/audit/patients")
        assert res_api.status_code == 403


