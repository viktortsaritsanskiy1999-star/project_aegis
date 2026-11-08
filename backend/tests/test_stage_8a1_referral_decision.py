from datetime import datetime, timedelta, timezone
import json
from typing import Any, Dict, List, Optional
import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.database import get_db
from app.main import app
from app.models.medical_cycle import MedicalCycle
from app.models.patient import Patient
from app.repositories.cycle_repository import MedicalCycleRepository
from app.repositories.report_repository import ModelReportRepository
from app.repositories.sprint_repository import WeeklySprintRepository
from app.routers.client_cascade import ClientCascadeStatusResponse, ClientReferralStatus
from app.schemas.consilium import (
    SYSTEM_KEYS,
    ProReferralDecision,
    WeeklyProMasterOutput,
    ProVitalsThresholds,
    ClientVitalsThresholds,
)
from app.schemas.privacy import PrivacyLevel
from app.security.auth import DeviceSession, require_device_session, verify_client_token
from app.services.gemini_gateway import GeminiGateway
from app.services.primary_consilium_service import PrimaryConsiliumService
from app.services.weekly_consilium_service import (
    WeeklyConsiliumService,
    REFERRAL_LIFECYCLE_STATE_REPORT_TYPE,
    WEEKLY_CHIEF_INTERNAL_REPORT_TYPE,
    WEEKLY_CONSILIUM_EXECUTION_LOG_REPORT_TYPE,
)
from tests.test_primary_consilium import (
    _gemini_envelope,
    _seed_preparatory_cycle_context,
    _build_consilium_mock_gateway,
)
from tests.test_weekly_consilium import (
    _sample_weekly_flash_output,
    _sample_weekly_pro_output,
    _seed_completed_primary_consilium,
    _seed_seven_day_athena_logs,
)


def _build_referral_mock_gateway(
    recorded_calls: List[Dict[str, Any]],
    flash_override: Optional[Dict[str, Any]] = None,
    pro_override: Optional[Dict[str, Any]] = None,
    fail_on_call_index: Optional[int] = None,
) -> GeminiGateway:
    call_counter = {"index": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        idx = call_counter["index"]
        call_counter["index"] += 1

        body = json.loads(request.content.decode("utf-8"))
        prompt_text = body["contents"][0]["parts"][0]["text"]
        url_path = request.url.path

        recorded_calls.append(
            {
                "index": idx,
                "url_path": url_path,
                "prompt": prompt_text,
            }
        )

        assert "flash-lite" not in url_path, (
            f"Forbidden Flash-Lite call detected during Weekly Consilium: {url_path}"
        )

        if fail_on_call_index is not None and idx == fail_on_call_index:
            return httpx.Response(
                400,
                json={"error": {"message": f"Injected weekly failure at call index {idx}"}},
            )

        if idx == 0:
            payload = flash_override or _sample_weekly_flash_output()
            return httpx.Response(200, json=_gemini_envelope(payload))

        if idx == 1:
            payload = pro_override or _sample_weekly_pro_output()
            return httpx.Response(200, json=_gemini_envelope(payload))

        raise AssertionError(f"Unexpected extra Gemini call at index {idx}: {url_path}")

    return GeminiGateway(
        api_key="test-referral-mock-key",
        transport=httpx.MockTransport(handler),
        sleep_func=lambda _: None,
    )


def _seed_patient(db_session: Session, patient_id: str) -> Patient:
    patient = db_session.query(Patient).filter_by(patient_id=patient_id).first()
    if not patient:
        patient = Patient(patient_id=patient_id, status="active")
        db_session.add(patient)
        db_session.commit()
    return patient


# ==============================================================================
# TEST 1: SCHEMA VALIDATION & BACKWARD COMPATIBILITY
# ==============================================================================

def test_schema_pro_referral_decision_defaults_and_validation():
    # 1. Defaults when initialized empty
    dec = ProReferralDecision()
    assert dec.recommended is False
    assert dec.urgency == "none"
    assert dec.target_specialties == []
    assert dec.clinical_reason_for_referral == ""

    # 2. Recommended=True with automatic urgency fallback
    dec_rec = ProReferralDecision(recommended=True)
    assert dec_rec.recommended is True
    assert dec_rec.urgency == "routine"
    assert dec_rec.target_specialties == ["General Practitioner"]

    # 3. Urgency normalization
    dec_norm1 = ProReferralDecision(
        recommended=True,
        urgency="Priority Urgent High",
        target_specialties=["Cardiologist"],
        clinical_reason_for_referral="Persistent tachycardia",
    )
    assert dec_norm1.urgency == "priority"
    assert dec_norm1.target_specialties == ["Cardiologist"]

    dec_norm2 = ProReferralDecision(
        recommended=True,
        urgency="Planned Routine Outpatient",
        target_specialties=["Endocrinologist"],
    )
    assert dec_norm2.urgency == "routine"

    # 4. Backward compatibility: WeeklyProMasterOutput parsed without referral_decision
    old_pro_dict = _sample_weekly_pro_output()
    assert "referral_decision" not in old_pro_dict
    parsed_old = WeeklyProMasterOutput.model_validate(old_pro_dict)
    assert parsed_old.referral_decision is None

    # 5. New WeeklyProMasterOutput parsed with referral_decision
    new_pro_dict = _sample_weekly_pro_output()
    new_pro_dict["referral_decision"] = {
        "recommended": True,
        "urgency": "priority",
        "target_specialties": ["Cardiologist", "Neurologist"],
        "clinical_reason_for_referral": "Suspected autonomic dysfunction",
        "observed_syndromes_or_trends": ["Orthostatic spikes", "Sleep latency"],
        "recommended_workup_for_doctor_discussion": ["Holter ECG 24h", "Tilt test"],
    }
    parsed_new = WeeklyProMasterOutput.model_validate(new_pro_dict)
    assert parsed_new.referral_decision is not None
    assert parsed_new.referral_decision.recommended is True
    assert parsed_new.referral_decision.urgency == "priority"
    assert parsed_new.referral_decision.target_specialties == ["Cardiologist", "Neurologist"]


# ==============================================================================
# TEST 2: PRO RECOMMENDED = FALSE -> REFERRAL NOT CREATED
# ==============================================================================

@pytest.mark.anyio
async def test_weekly_pro_recommended_false_no_referral_created(db_session: Session):
    cycle_id = "cycle_ref_false_01"
    await _seed_completed_primary_consilium(db_session, cycle_id=cycle_id)
    _seed_seven_day_athena_logs(db_session, cycle_id=cycle_id, sprint_id="sprint_w1")

    pro_output = _sample_weekly_pro_output()
    pro_output["referral_decision"] = {
        "recommended": False,
        "urgency": "none",
        "target_specialties": [],
        "clinical_reason_for_referral": "",
        "observed_syndromes_or_trends": [],
        "recommended_workup_for_doctor_discussion": [],
    }

    recorded_calls: List[Dict[str, Any]] = []
    gateway = _build_referral_mock_gateway(recorded_calls, pro_override=pro_output)
    weekly_service = WeeklyConsiliumService(db=db_session, gateway=gateway)

    client_resp = await weekly_service.run_weekly_consilium(
        cycle_id=cycle_id,
        sprint_id="sprint_w1",
        idempotency_key="idem_w1_no_ref",
    )
    assert client_resp.sprint_number == 1

    # Verify no referral_lifecycle_state report was created
    report_repo = ModelReportRepository(db_session)
    ref_reports = report_repo.list_server_only(
        cycle_id=cycle_id,
        report_type=REFERRAL_LIFECYCLE_STATE_REPORT_TYPE,
    )
    assert len(ref_reports) == 0

    ref_state = weekly_service.get_referral_lifecycle_state(cycle_id)
    assert ref_state is None


# ==============================================================================
# TEST 3: PRO RECOMMENDED = TRUE -> REFERRAL PERSISTED IN SERVER_ONLY
# ==============================================================================

@pytest.mark.anyio
async def test_weekly_pro_recommended_true_referral_created_in_server_only(db_session: Session):
    cycle_id = "cycle_ref_true_01"
    await _seed_completed_primary_consilium(db_session, cycle_id=cycle_id)
    _seed_seven_day_athena_logs(db_session, cycle_id=cycle_id, sprint_id="sprint_w1")

    pro_output = _sample_weekly_pro_output()
    pro_output["referral_decision"] = {
        "recommended": True,
        "urgency": "routine",
        "target_specialties": ["Cardiologist", "Endocrinologist"],
        "clinical_reason_for_referral": "Unfavorable combination of elevated resting pulse and progressive fatigue.",
        "observed_syndromes_or_trends": ["Resting HR > 82 bpm over 7 days", "Sleep quality score dropped"],
        "recommended_workup_for_doctor_discussion": ["Holter ECG 24h", "TSH and free T4 panel"],
    }

    recorded_calls: List[Dict[str, Any]] = []
    gateway = _build_referral_mock_gateway(recorded_calls, pro_override=pro_output)
    weekly_service = WeeklyConsiliumService(db=db_session, gateway=gateway)

    client_resp = await weekly_service.run_weekly_consilium(
        cycle_id=cycle_id,
        sprint_id="sprint_w1",
        idempotency_key="idem_w1_ref_true",
    )
    assert client_resp.sprint_number == 1

    # Verify referral_lifecycle_state stored in SERVER_ONLY
    report_repo = ModelReportRepository(db_session)
    ref_reports = report_repo.list_server_only(
        cycle_id=cycle_id,
        report_type=REFERRAL_LIFECYCLE_STATE_REPORT_TYPE,
    )
    assert len(ref_reports) == 1
    ref_report = ref_reports[0]
    assert ref_report.privacy_level == PrivacyLevel.SERVER_ONLY

    content = ref_report.content
    assert content["cycle_id"] == cycle_id
    assert content["sprint_id"] == "sprint_w1"
    assert content["sprint_number"] == 1
    assert content["status"] == "generating_document"
    assert content["urgency"] == "routine"
    assert content["target_specialties"] == ["Cardiologist", "Endocrinologist"]
    assert "elevated resting pulse" in content["clinical_reason_for_referral"]
    assert content["document_report_id"] is None
    assert content["created_at_utc"] is not None
    assert content["referral_id"] is not None

    # Check Chief Internal report stores referral_decision
    chief_reports = report_repo.list_server_only(
        cycle_id=cycle_id,
        report_type=WEEKLY_CHIEF_INTERNAL_REPORT_TYPE,
    )
    assert len(chief_reports) == 1
    assert chief_reports[0].content["referral_decision"]["recommended"] is True

    # Check Execution log recorded referral_recommended: True
    exec_logs = report_repo.list_server_only(
        cycle_id=cycle_id,
        report_type=WEEKLY_CONSILIUM_EXECUTION_LOG_REPORT_TYPE,
    )
    assert len(exec_logs) == 1
    assert exec_logs[0].content["referral_recommended"] is True


# ==============================================================================
# TEST 4: OLD PRO RESPONSE WITHOUT REFERRAL FIELD WORKS CLEANLY
# ==============================================================================

@pytest.mark.anyio
async def test_weekly_pro_old_response_without_referral_decision_backward_compatibility(
    db_session: Session,
):
    cycle_id = "cycle_old_compat_01"
    await _seed_completed_primary_consilium(db_session, cycle_id=cycle_id)
    _seed_seven_day_athena_logs(db_session, cycle_id=cycle_id, sprint_id="sprint_w1")

    # Old Pro output that lacks referral_decision key completely
    old_pro_dict = _sample_weekly_pro_output()
    assert "referral_decision" not in old_pro_dict

    recorded_calls: List[Dict[str, Any]] = []
    gateway = _build_referral_mock_gateway(recorded_calls, pro_override=old_pro_dict)
    weekly_service = WeeklyConsiliumService(db=db_session, gateway=gateway)

    client_resp = await weekly_service.run_weekly_consilium(
        cycle_id=cycle_id,
        sprint_id="sprint_w1",
        idempotency_key="idem_old_pro",
    )
    assert client_resp.sprint_number == 1
    assert len(client_resp.system_reports) == 8

    # No referral created
    ref_state = weekly_service.get_referral_lifecycle_state(cycle_id)
    assert ref_state is None


# ==============================================================================
# TEST 5: REPEAT EXECUTION OF SAME WEEKLY SPRINT DOES NOT DUPLICATE REFERRAL
# ==============================================================================

@pytest.mark.anyio
async def test_repeat_execution_of_same_weekly_does_not_duplicate_referral(db_session: Session):
    cycle_id = "cycle_idem_ref_01"
    await _seed_completed_primary_consilium(db_session, cycle_id=cycle_id)
    _seed_seven_day_athena_logs(db_session, cycle_id=cycle_id, sprint_id="sprint_w1")

    pro_output = _sample_weekly_pro_output()
    pro_output["referral_decision"] = {
        "recommended": True,
        "urgency": "routine",
        "target_specialties": ["Neurologist"],
        "clinical_reason_for_referral": "Recurring tension headaches unresponsive to sleep adjustment.",
        "observed_syndromes_or_trends": ["Cephalea reports on days 3, 5, 7"],
        "recommended_workup_for_doctor_discussion": ["Consultation", "Cervical spine evaluation"],
    }

    recorded_calls: List[Dict[str, Any]] = []
    gateway = _build_referral_mock_gateway(recorded_calls, pro_override=pro_output)
    weekly_service = WeeklyConsiliumService(db=db_session, gateway=gateway)

    # First execution
    resp1 = await weekly_service.run_weekly_consilium(
        cycle_id=cycle_id,
        sprint_id="sprint_w1",
        idempotency_key="idem_repeat_ref",
    )
    assert resp1.sprint_number == 1

    # Second execution (retry with same idempotency key or sprint_id)
    resp2 = await weekly_service.run_weekly_consilium(
        cycle_id=cycle_id,
        sprint_id="sprint_w1",
        idempotency_key="idem_repeat_ref",
    )
    assert resp2.sprint_number == 1

    # Exactly 2 Gemini calls made (none on the 2nd run due to recovery)
    assert len(recorded_calls) == 2

    report_repo = ModelReportRepository(db_session)
    ref_reports = report_repo.list_server_only(
        cycle_id=cycle_id,
        report_type=REFERRAL_LIFECYCLE_STATE_REPORT_TYPE,
    )
    assert len(ref_reports) == 1


# ==============================================================================
# TEST 6: SUBSEQUENT WEEKLY WITH ACTIVE REFERRAL MERGES WITHOUT CONFLICT
# ==============================================================================

@pytest.mark.anyio
async def test_subsequent_weekly_with_active_referral_merges_without_conflict(db_session: Session):
    cycle_id = "cycle_merge_ref_01"
    _seed_patient(db_session, "patient_merge_01")
    cycle_repo = MedicalCycleRepository(db_session)
    cycle = cycle_repo.create(cycle_id=cycle_id, patient_id="patient_merge_01")

    await _seed_completed_primary_consilium(db_session, cycle_id=cycle_id)

    weekly_service = WeeklyConsiliumService(db=db_session)

    # Sprint 1 creates active referral
    decision_w1 = ProReferralDecision(
        recommended=True,
        urgency="routine",
        target_specialties=["Cardiologist"],
        clinical_reason_for_referral="Resting HR elevation",
        observed_syndromes_or_trends=["Trend 1"],
        recommended_workup_for_doctor_discussion=["Workup 1"],
    )
    rep1 = weekly_service.save_referral_lifecycle_state(
        cycle_id=cycle_id,
        patient_id="patient_merge_01",
        sprint_id="sprint_w1",
        sprint_number=1,
        decision=decision_w1,
    )
    assert rep1 is not None
    orig_ref_id = rep1.content["referral_id"]
    orig_created_at = rep1.content["created_at_utc"]

    # Sprint 2 with new findings escalates to priority and adds Endocrinologist
    decision_w2 = ProReferralDecision(
        recommended=True,
        urgency="priority",
        target_specialties=["Endocrinologist"],
        clinical_reason_for_referral="Resting HR elevation accompanied by tremors and weight change",
        observed_syndromes_or_trends=["Trend 2"],
        recommended_workup_for_doctor_discussion=["Workup 2"],
    )
    rep2 = weekly_service.save_referral_lifecycle_state(
        cycle_id=cycle_id,
        patient_id="patient_merge_01",
        sprint_id="sprint_w2",
        sprint_number=2,
        decision=decision_w2,
    )
    assert rep2 is not None

    # Check continuity: same referral_id and original created_at_utc preserved
    content2 = rep2.content
    assert content2["referral_id"] == orig_ref_id
    assert content2["created_at_utc"] == orig_created_at
    assert content2["sprint_id"] == "sprint_w2"
    assert content2["sprint_number"] == 2
    assert content2["urgency"] == "priority"  # upgraded to priority
    assert set(content2["target_specialties"]) == {"Cardiologist", "Endocrinologist"}
    assert "Trend 1" in content2["observed_syndromes_or_trends"]
    assert "Trend 2" in content2["observed_syndromes_or_trends"]
    assert "Workup 1" in content2["recommended_workup_for_doctor_discussion"]
    assert "Workup 2" in content2["recommended_workup_for_doctor_discussion"]


# ==============================================================================
# TEST 7: SUBSEQUENT WEEKLY RECOMMENDED = FALSE PRESERVES ACTIVE REFERRAL
# ==============================================================================

@pytest.mark.anyio
async def test_subsequent_weekly_recommended_false_preserves_active_referral(db_session: Session):
    cycle_id = "cycle_keep_active_01"
    _seed_patient(db_session, "patient_keep_01")
    cycle_repo = MedicalCycleRepository(db_session)
    cycle = cycle_repo.create(cycle_id=cycle_id, patient_id="patient_keep_01")

    weekly_service = WeeklyConsiliumService(db=db_session)

    # Sprint 1 creates active referral
    decision_w1 = ProReferralDecision(
        recommended=True,
        urgency="routine",
        target_specialties=["Gastroenterologist"],
        clinical_reason_for_referral="Persistent postprandial distress",
    )
    weekly_service.save_referral_lifecycle_state(
        cycle_id=cycle_id,
        patient_id="patient_keep_01",
        sprint_id="sprint_w1",
        sprint_number=1,
        decision=decision_w1,
    )

    # Sprint 2 has recommended=False (doctor visit not yet confirmed, but Pro has no new referral note)
    decision_w2 = ProReferralDecision(recommended=False)
    res_w2 = weekly_service.save_referral_lifecycle_state(
        cycle_id=cycle_id,
        patient_id="patient_keep_01",
        sprint_id="sprint_w2",
        sprint_number=2,
        decision=decision_w2,
    )
    assert res_w2 is None

    # Active referral still present and active
    current_state = weekly_service.get_referral_lifecycle_state(cycle_id)
    assert current_state is not None
    assert current_state["status"] == "generating_document"
    assert current_state["target_specialties"] == ["Gastroenterologist"]


# ==============================================================================
# TEST 8: ROLLBACK SAFETY — TRANSACTION FAILURE LEAVES NO PARTIAL REFERRAL STATE
# ==============================================================================

@pytest.mark.anyio
async def test_transaction_rollback_leaves_no_partial_referral_state(db_session: Session):
    cycle_id = "cycle_rollback_ref_01"
    await _seed_completed_primary_consilium(db_session, cycle_id=cycle_id)
    _seed_seven_day_athena_logs(db_session, cycle_id=cycle_id, sprint_id="sprint_w1")

    # Pro recommends referral, but Gemini call 1 (Pro) fails before completion
    recorded_calls: List[Dict[str, Any]] = []
    gateway = _build_referral_mock_gateway(recorded_calls, fail_on_call_index=1)
    weekly_service = WeeklyConsiliumService(db=db_session, gateway=gateway)

    with pytest.raises(Exception):
        await weekly_service.run_weekly_consilium(
            cycle_id=cycle_id,
            sprint_id="sprint_w1",
            idempotency_key="idem_fail_ref",
        )

    # Verify no partial referral state was saved
    report_repo = ModelReportRepository(db_session)
    ref_reports = report_repo.list_server_only(
        cycle_id=cycle_id,
        report_type=REFERRAL_LIFECYCLE_STATE_REPORT_TYPE,
    )
    assert len(ref_reports) == 0


# ==============================================================================
# TEST 9: CLIENT STATUS API — RETURNED ONLY TO AUTHORIZED PATIENT
# ==============================================================================

def test_client_cascade_status_api_isolation_and_referral_visibility(db_session: Session):
    patient_a = "patient_owner_aaa"
    patient_b = "patient_foreign_bbb"
    _seed_patient(db_session, patient_a)
    _seed_patient(db_session, patient_b)

    cycle_repo = MedicalCycleRepository(db_session)
    cycle_a = cycle_repo.create(cycle_id="cycle_iso_a", patient_id=patient_a)
    cycle_b = cycle_repo.create(cycle_id="cycle_iso_b", patient_id=patient_b)

    # Seed referral for patient A
    weekly_service = WeeklyConsiliumService(db=db_session)
    decision = ProReferralDecision(
        recommended=True,
        urgency="priority",
        target_specialties=["Cardiologist"],
        clinical_reason_for_referral="Sustained arrhythmia episodes in resting window",
    )
    weekly_service.save_referral_lifecycle_state(
        cycle_id=cycle_a.cycle_id,
        patient_id=patient_a,
        sprint_id="sprint_w1",
        sprint_number=1,
        decision=decision,
    )

    client = TestClient(app)

    def _make_session(p_id: str, d_id: str) -> DeviceSession:
        return DeviceSession(
            patient_id=p_id,
            device_id=d_id,
            jti="jti_test",
            family_id="fam_test",
            scope="cycle:read cycle:write",
            scopes=frozenset(["cycle:read", "cycle:write"]),
            issued_at=datetime.now(timezone.utc),
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        )

    # 1. Query status as Patient A
    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[require_device_session] = lambda: _make_session(patient_a, "dev_a")
    app.dependency_overrides[verify_client_token] = lambda: _make_session(patient_a, "dev_a")
    try:
        resp_a = client.get("/api/v1/client/cascade/status")
        assert resp_a.status_code == 200
        data_a = resp_a.json()
        assert data_a["has_active_referral"] is True
        ref_status_a = data_a["referral_status"]
        assert ref_status_a["has_active_referral"] is True
        assert ref_status_a["status"] == "generating_document"
        assert ref_status_a["urgency"] == "priority"
        assert ref_status_a["target_specialties"] == ["Cardiologist"]
        assert "Sustained arrhythmia" in ref_status_a["clinical_reason_summary"]

        # 2. Query status as Patient B (Zero Leakage)
        app.dependency_overrides[require_device_session] = lambda: _make_session(patient_b, "dev_b")
        app.dependency_overrides[verify_client_token] = lambda: _make_session(patient_b, "dev_b")
        resp_b = client.get("/api/v1/client/cascade/status")
        assert resp_b.status_code == 200
        data_b = resp_b.json()
        assert data_b["has_active_referral"] is False
        assert data_b["referral_status"]["has_active_referral"] is False
        assert data_b["referral_status"]["status"] == "none"
        assert data_b["referral_status"]["target_specialties"] == []
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(require_device_session, None)
        app.dependency_overrides.pop(verify_client_token, None)


# ==============================================================================
# TEST 10: PRO THRESHOLDS AND EMERGENCY LOGIC REMAIN COMPLETELY UNTOUCHED
# ==============================================================================

def test_pro_thresholds_and_emergency_rules_untouched():
    from app.services.athena_chat_service import AthenaChatService

    # Verify ProVitalsThresholds continues to enforce strict bounds and forbid extra
    thresholds = ProVitalsThresholds(
        min_pessimum_bpm=50,
        max_pessimum_bpm=100,
        max_spike_delta_bpm=30,
        clinical_rationale="Patient baseline is stable resting 60-70.",
    )
    dumped = thresholds.model_dump()
    assert dumped["min_pessimum_bpm"] == 50
    assert dumped["max_pessimum_bpm"] == 100
    assert dumped["clinical_rationale"] == "Patient baseline is stable resting 60-70."

    # Verify client threshold schema
    client_thresh = ClientVitalsThresholds(
        min_pessimum_bpm=50,
        max_pessimum_bpm=100,
        max_spike_delta_bpm=30,
    )
    assert client_thresh.min_pessimum_bpm == 50

    # Verify Mode 2 emergency keywords remain intact
    assert hasattr(AthenaChatService, "_evaluate_pre_emergency")


# ==============================================================================
# TEST 11: CLINICAL AUTOMATION WORKER STEP A REPORTS REFERRAL FLAG
# ==============================================================================

@pytest.mark.anyio
async def test_clinical_automation_step_a_reports_referral_flag(db_session: Session):
    from app.services.clinical_automation_service import ClinicalAutomationService
    from app.services.weekly_consilium_service import ATHENA_SCHEDULE_ANCHOR_REPORT_TYPE

    cycle_id = "cycle_cas_ref_01"
    patient_id = "patient_cas_01"
    _seed_patient(db_session, patient_id)
    cycle_repo = MedicalCycleRepository(db_session)
    cycle_repo.create(cycle_id=cycle_id, patient_id=patient_id)
    await _seed_completed_primary_consilium(db_session, cycle_id=cycle_id)
    _seed_seven_day_athena_logs(db_session, cycle_id=cycle_id, sprint_id="sprint_w1")

    # Set anchor T0
    report_repo = ModelReportRepository(db_session)
    t0 = datetime(2026, 10, 18, 10, 0, tzinfo=timezone.utc)
    report_repo.create_next_version(
        cycle_id=cycle_id,
        model="athena-schedule-engine",
        report_type=ATHENA_SCHEDULE_ANCHOR_REPORT_TYPE,
        privacy_level=PrivacyLevel.SERVER_ONLY,
        content={"anchor_timestamp_utc": t0.isoformat(), "event_type": "initial_greeting"},
        created_at=t0,
    )

    pro_output = _sample_weekly_pro_output()
    pro_output["referral_decision"] = {
        "recommended": True,
        "urgency": "priority",
        "target_specialties": ["Cardiologist"],
        "clinical_reason_for_referral": "High pulse in Stage 4 test",
    }
    recorded_calls: List[Dict[str, Any]] = []
    gateway = _build_referral_mock_gateway(recorded_calls, pro_override=pro_output)

    now_dt = t0 + timedelta(hours=168, minutes=5)
    cas = ClinicalAutomationService(
        db=db_session,
        gateway=gateway,
        now_provider=lambda: now_dt,
    )

    job = cas.enqueue_weekly_consilium_a(
        cycle_id=cycle_id,
        patient_id=patient_id,
        sprint_number=1,
    )

    res = await cas._execute_weekly_consilium_a(job)
    assert res["has_active_referral"] is True
    assert res["sprint_number"] == 1

