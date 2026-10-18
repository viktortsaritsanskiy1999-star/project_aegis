from datetime import datetime, timezone
import json
from typing import Any, Dict, List
import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.repositories.cycle_repository import MedicalCycleRepository
from app.repositories.measurement_repository import HealthMeasurementRepository
from app.repositories.report_repository import ModelReportRepository
from app.routers.cascade import get_gemini_gateway
from app.schemas.consilium import (
    SYSTEM_KEYS,
    AegisAgentRole,
    AnamnesisModelOutput,
)
from app.services.consilium_prompts import (
    build_anamnesis_prompt,
    sanitize_blind_anamnesis_context,
)
from app.services.gemini_gateway import (
    GeminiGateway,
    GeminiValidationError,
)
from app.services.primary_consilium_service import (
    PrimaryConsiliumService,
    ConsiliumContextMissingError,
    ConsiliumQualityRejectionError,
    PREPARATORY_ANAMNESIS_REPORT_TYPE,
    PREPARATORY_OCR_REPORT_TYPE,
    PREPARATORY_ECG_REPORT_TYPE,
)


def _gemini_envelope(payload: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "candidates": [
            {
                "content": {
                    "parts": [{"text": json.dumps(payload, ensure_ascii=False)}]
                }
            }
        ]
    }


def _sample_anamnesis_output() -> Dict[str, Any]:
    return {
        "user_view": {
            "summary": "Primary anamnesis processed across 8 systems in blind mode."
        },
        "system_logic": {
            key: {
                "data": f"{key.upper()} anamnesis facts: mild fatigue and sedentary work",
                "internal_comment": f"M1 blind comment for {key}",
            }
            for key in SYSTEM_KEYS
        },
        "training_sandbox": {
            "raw_diagnostic_hypothesis": "Autonomic fatigue with sedentary cardiovascular deconditioning",
            "data_gap_analysis": "Need lipid profile, ferritin, TSH, and resting ECG",
            "error_probability": "Low (15%)",
        },
    }


def _sample_system_agent_output(system_key: str) -> Dict[str, Any]:
    return {
        "system_view": {
            "isolated_labs": {f"{system_key}_marker": "within reference"}
        },
        "system_logic": {
            "system_general_shelf": f"Academic pathophysiological conclusion for {system_key}."
        },
        "training_sandbox": {
            "system_training_shelf": f"Deficit log and required markers for {system_key}."
        },
    }


def _sample_flash_output(cvs_draft_text: str | None = None) -> Dict[str, Any]:
    user_reports = {
        key: (
            f"Calm wellness summary for {key}. No unverified diagnoses; "
            f"additional instrumental markers for {key} are recommended."
        )
        for key in SYSTEM_KEYS
    }
    if cvs_draft_text is not None:
        user_reports["cvs"] = cvs_draft_text

    return {
        "flash_general_shelf": "INTERNAL FLASH GENERAL SHELF: intersystem synthesis for cascade.",
        "flash_training_shelf": "INTERNAL FLASH TRAINING SANDBOX: nosologies, deficits, audit of M1 and 8 agents.",
        "user_system_reports": user_reports,
    }


def _sample_pro_output(
    approved: bool = True,
    rejection_reason: str | None = None,
    corrected_reports: Dict[str, str] | None = None,
) -> Dict[str, Any]:
    return {
        "vitals_thresholds": {
            "min_pessimum_bpm": 48,
            "max_pessimum_bpm": 118,
            "max_spike_delta_bpm": 28,
            "clinical_rationale": "SECRET_PRO_CLINICAL_RATIONALE: beta-adrenergic sensitivity and baseline resting HR 62.",
        },
        "pro_general_shelf": "SECRET_ATHENA_STRATEGY: Ask max 3 questions/day, verify orthostatic tolerance.",
        "pro_training_shelf": "SECRET_PRO_SUPREME_SANDBOX: Final nosologies and supreme audit of M1, 8 Flash-Lite, and Deputy Flash.",
        "user_overall_report": {
            "title": "Aegis Personal Wellness Roadmap",
            "summary": "Overall functional state is stable with mild autonomic fatigue. Further routine blood panel is advised due to current data gaps.",
            "highlights": [
                "Maintain regular aerobic pacing within comfortable heart rate zones",
                "Complete baseline lipid and iron panel when convenient",
            ],
        },
        "flash_quality_review": {
            "approved_for_publication": approved,
            "rejection_reason": rejection_reason,
            "corrected_user_system_reports": corrected_reports or {},
        },
    }


def _seed_preparatory_cycle_context(db_session, cycle_id: str = "cycle_consilium_01") -> None:
    cycle_repo = MedicalCycleRepository(db_session)
    cycle_repo.create(cycle_id=cycle_id, status="active")

    measurement_repo = HealthMeasurementRepository(db_session)
    measurement_repo.create(
        cycle_id=cycle_id,
        measured_at=datetime(2026, 10, 18, 9, 0, tzinfo=timezone.utc),
        source="watch_sensor",
        pulse=64,
        systolic=122,
        diastolic=78,
        spo2=98,
    )

    service = PrimaryConsiliumService(db=db_session)
    service.save_preparatory_anamnesis_result(
        cycle_id=cycle_id,
        anamnesis_output=_sample_anamnesis_output(),
    )
    service.save_preparatory_ocr_labs(
        cycle_id=cycle_id,
        shared_labs_table={
            "hemoglobin": "144 g/L",
            "ferritin": "42 ng/mL",
            "creatinine": "82 umol/L",
            "glucose": "5.1 mmol/L",
        },
    )
    service.save_preparatory_ecg_summary(
        cycle_id=cycle_id,
        ecg_summary="Sinus rhythm, HR 64 bpm, normal electrical axis, no ST-T shifts.",
    )


def _build_consilium_mock_gateway(
    recorded_calls: List[Dict[str, Any]],
    flash_override: Dict[str, Any] | None = None,
    pro_override: Dict[str, Any] | None = None,
    fail_on_call_index: int | None = None,
) -> GeminiGateway:
    """
    Creates a mocked GeminiGateway that records every request URL and prompt body,
    verifies no real network calls are made, and returns deterministic structured responses
    for the 8 Flash-Lite specialists -> Deputy Flash -> Chief Physician Pro.
    """
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

        if fail_on_call_index is not None and idx == fail_on_call_index:
            return httpx.Response(
                400,
                json={"error": {"message": f"Injected failure at call index {idx}"}},
            )

        # Calls 0..7 -> 8 Flash-Lite organ specialists
        if idx < 8:
            expected_system_key = SYSTEM_KEYS[idx]
            return httpx.Response(
                200,
                json=_gemini_envelope(_sample_system_agent_output(expected_system_key)),
            )

        # Call 8 -> Deputy Chief Physician (Gemini Flash)
        if idx == 8:
            payload = flash_override or _sample_flash_output()
            return httpx.Response(200, json=_gemini_envelope(payload))

        # Call 9 -> Chief Physician (Gemini Pro)
        if idx == 9:
            payload = pro_override or _sample_pro_output()
            return httpx.Response(200, json=_gemini_envelope(payload))

        raise AssertionError(f"Unexpected extra Gemini call at index {idx}: {url_path}")

    return GeminiGateway(
        api_key="test-consilium-key",
        transport=httpx.MockTransport(handler),
        sleep_func=lambda _: None,
    )


@pytest.mark.anyio
async def test_blind_anamnesis_strips_all_lab_fields_and_tags_role(db_session):
    """
    Verifies that Anamnesis (M1) operates strictly in blind mode without access to
    any laboratory data and saves its output as server_only with role 'anamnesis_m1'.
    """
    cycle_repo = MedicalCycleRepository(db_session)
    cycle_repo.create(cycle_id="cycle_blind_m1", status="active")

    raw_context_with_labs = {
        "system_folders": {
            "cvs": {"complaints": "occasional palpitations", "labs": {"troponin": "negative"}},
            "gi_tract": {"complaints": "mild heartburn", "isolated_labs": {"alt": 22}},
        },
        "core_data_log": {
            "age": 34,
            "gender": "male",
            "shared_labs_shelf": {"hemoglobin": "150 g/L", "ferritin": "15 ng/mL"},
            "lab_panel_secret": "SECRET_LAB_VALUE_999",
        },
        "biochemistry": {"ast": 19},
    }

    sanitized = sanitize_blind_anamnesis_context(raw_context_with_labs)
    sanitized_str = json.dumps(sanitized)
    assert "SECRET_LAB_VALUE_999" not in sanitized_str
    assert "shared_labs_shelf" not in sanitized_str
    assert "troponin" not in sanitized_str
    assert "ferritin" not in sanitized_str
    assert "occasional palpitations" in sanitized_str

    prompt = build_anamnesis_prompt(raw_context_with_labs)
    assert "SECRET_LAB_VALUE_999" not in prompt
    assert "shared_labs_shelf" not in prompt

    captured_prompts: List[str] = []

    async def m1_handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        captured_prompts.append(body["contents"][0]["parts"][0]["text"])
        return httpx.Response(200, json=_gemini_envelope(_sample_anamnesis_output()))

    gateway = GeminiGateway(
        api_key="test-m1-key",
        transport=httpx.MockTransport(m1_handler),
    )
    service = PrimaryConsiliumService(db=db_session, gateway=gateway)
    saved_m1 = await service.run_preparatory_blind_anamnesis(
        cycle_id="cycle_blind_m1",
        raw_anamnesis_context=raw_context_with_labs,
    )

    assert len(captured_prompts) == 1
    assert "SECRET_LAB_VALUE_999" not in captured_prompts[0]
    assert saved_m1.privacy_level == "server_only"
    assert saved_m1.report_type == PREPARATORY_ANAMNESIS_REPORT_TYPE
    assert saved_m1.content["agent_role"] == AegisAgentRole.ANAMNESIS_M1.value
    assert AnamnesisModelOutput.model_validate(
        {k: v for k, v in saved_m1.content.items() if k != "agent_role"}
    )


@pytest.mark.anyio
async def test_primary_consilium_sequence_context_and_role_separation(db_session):
    """
    Verifies:
    1. Exact execution order: 8 Flash-Lite -> 1 Deputy Flash -> 1 Chief Physician Pro (10 calls total).
    2. Anamnesis (M1) and Vision OCR/ECG are NOT re-run inside run_primary_consilium.
    3. Preserved instructions & context passing between the 8 specialists, Deputy Flash, and Pro.
    4. Logical agent roles (AegisAgentRole) are explicitly stored on server_only reports.
    """
    cycle_id = "cycle_consilium_seq"
    _seed_preparatory_cycle_context(db_session, cycle_id=cycle_id)

    recorded_calls: List[Dict[str, Any]] = []
    gateway = _build_consilium_mock_gateway(recorded_calls)
    service = PrimaryConsiliumService(db=db_session, gateway=gateway)

    result = await service.run_primary_consilium(
        cycle_id=cycle_id,
        report_date="2026-10-18",
    )

    # 1. Exact 10 calls: 8 Flash-Lite -> 1 Flash -> 1 Pro (no M1 or Vision calls)
    assert len(recorded_calls) == 10
    for i, system_key in enumerate(SYSTEM_KEYS):
        assert gateway.flash_lite_model in recorded_calls[i]["url_path"]
        assert f'"system_key": "{system_key}"' in recorded_calls[i]["prompt"]
        assert f"M1 blind comment for {system_key}" in recorded_calls[i]["prompt"]
        assert "144 g/L" in recorded_calls[i]["prompt"]
        assert "122/78 mmHg" in recorded_calls[i]["prompt"]
        assert "Sinus rhythm, HR 64 bpm" in recorded_calls[i]["prompt"]

    # Call 8: Deputy Chief Physician (Flash) receives all 8 organ conclusions
    flash_call = recorded_calls[8]
    assert gateway.flash_model in flash_call["url_path"]
    assert 'senior analytical module "Synthesis and Control"' in flash_call["prompt"]
    for system_key in SYSTEM_KEYS:
        assert f"--- BLOCK: {system_key.upper()} ---" in flash_call["prompt"]
        assert f"Academic pathophysiological conclusion for {system_key}." in flash_call["prompt"]
        assert f"Deficit log and required markers for {system_key}." in flash_call["prompt"]

    # Call 9: Chief Physician (Pro) receives 8 organ conclusions + Flash shelves + Flash's 8 draft user reports
    pro_call = recorded_calls[9]
    assert gateway.pro_model in pro_call["url_path"]
    assert 'supreme clinical expert "Chief Physician"' in pro_call["prompt"]
    assert "INTERNAL FLASH GENERAL SHELF" in pro_call["prompt"]
    assert "INTERNAL FLASH TRAINING SANDBOX" in pro_call["prompt"]
    assert "DRAFT 8 USER SYSTEM REPORTS FROM DEPUTY FLASH" in pro_call["prompt"]

    # 2. Verify client response structure
    assert result.cycle_id == cycle_id
    assert result.report_date == "2026-10-18"
    assert set(result.system_reports.keys()) == set(SYSTEM_KEYS)
    assert result.overall_report.report_type == "user_overall_report"
    assert result.vitals_thresholds.min_pessimum_bpm == 48
    assert result.vitals_thresholds.max_pessimum_bpm == 118
    assert result.vitals_thresholds.max_spike_delta_bpm == 28

    # 3. Verify distinct logical agent_role metadata on server_only reports
    report_repo = ModelReportRepository(db_session)
    server_reports = report_repo.list_server_only(cycle_id=cycle_id)
    roles_in_db = {r.content.get("agent_role") for r in server_reports}
    expected_roles = {
        AegisAgentRole.ANAMNESIS_M1.value,
        AegisAgentRole.VISION_OCR.value,
        AegisAgentRole.VISION_ECG.value,
        AegisAgentRole.DEPUTY_CHIEF_PHYSICIAN.value,
        AegisAgentRole.CHIEF_PHYSICIAN.value,
        *(AegisAgentRole.for_system_key(k).value for k in SYSTEM_KEYS),
    }
    assert expected_roles.issubset(roles_in_db)


@pytest.mark.anyio
async def test_pro_quality_control_overwrites_flash_error_before_publication(db_session):
    """
    Verifies that if Deputy Flash makes an error or includes an unverified diagnosis
    in one of the 8 user_system_reports, Chief Physician Pro's correction in
    flash_quality_review.corrected_user_system_reports deterministically replaces it
    BEFORE any client_facing report is saved or returned.
    """
    cycle_id = "cycle_pro_correction"
    _seed_preparatory_cycle_context(db_session, cycle_id=cycle_id)

    raw_flash_mistake = (
        "UNVERIFIED_FLASH_DIAGNOSIS_ERROR: You definitely have severe myocarditis."
    )
    pro_verified_correction = (
        "Cardiovascular markers show normal resting rhythm (64 bpm). "
        "No acute signs detected; lipid profile is recommended to complete baseline assessment."
    )

    recorded_calls: List[Dict[str, Any]] = []
    gateway = _build_consilium_mock_gateway(
        recorded_calls=recorded_calls,
        flash_override=_sample_flash_output(cvs_draft_text=raw_flash_mistake),
        pro_override=_sample_pro_output(
            approved=True,
            corrected_reports={"cvs": pro_verified_correction},
        ),
    )

    service = PrimaryConsiliumService(db=db_session, gateway=gateway)
    response = await service.run_primary_consilium(
        cycle_id=cycle_id,
        report_date="2026-10-18",
    )

    # 1. Client-facing response must contain ONLY Pro's corrected text, never Flash's mistake
    cvs_client_report = response.system_reports["cvs"]
    assert cvs_client_report.content.summary == pro_verified_correction
    assert cvs_client_report.content.entries[0].summary == pro_verified_correction
    assert "UNVERIFIED_FLASH_DIAGNOSIS_ERROR" not in cvs_client_report.model_dump_json()

    # 2. Database client_facing reports must NOT contain Flash's mistake anywhere
    report_repo = ModelReportRepository(db_session)
    client_reports = report_repo.list_client_facing(cycle_id=cycle_id)
    assert len(client_reports) == 9  # 8 system reports + 1 overall report
    for cr in client_reports:
        serialized = json.dumps(cr.content)
        assert "UNVERIFIED_FLASH_DIAGNOSIS_ERROR" not in serialized

    # 3. Server-only Deputy Flash report retains the original draft for internal audit
    deputy_internal = report_repo.list_server_only(
        cycle_id=cycle_id,
        report_type="deputy_chief_physician_internal",
    )
    assert len(deputy_internal) == 1
    assert (
        deputy_internal[0].content["draft_user_system_reports"]["cvs"]
        == raw_flash_mistake
    )


@pytest.mark.anyio
async def test_no_client_reports_published_on_pro_rejection_or_cascade_failure(db_session):
    """
    Verifies atomic publication guarantees:
    - If Chief Physician Pro sets approved_for_publication=False, zero client_facing reports are saved.
    - If any model in the cascade fails, zero client_facing reports are saved.
    - If preparatory Anamnesis M1 is missing, ConsiliumContextMissingError is raised with zero Gemini calls.
    """
    # Case A: Missing preparatory Anamnesis M1 -> fails immediately without calling Gemini
    cycle_repo = MedicalCycleRepository(db_session)
    cycle_repo.create(cycle_id="cycle_missing_m1", status="active")
    calls_missing: List[Dict[str, Any]] = []
    gateway_missing = _build_consilium_mock_gateway(calls_missing)
    service_missing = PrimaryConsiliumService(db=db_session, gateway=gateway_missing)

    with pytest.raises(ConsiliumContextMissingError):
        await service_missing.run_primary_consilium(cycle_id="cycle_missing_m1")
    assert len(calls_missing) == 0

    # Case B: Pro rejects publication (approved_for_publication=False)
    cycle_rejected = "cycle_pro_rejected"
    _seed_preparatory_cycle_context(db_session, cycle_id=cycle_rejected)
    calls_rejected: List[Dict[str, Any]] = []
    gateway_rejected = _build_consilium_mock_gateway(
        recorded_calls=calls_rejected,
        pro_override=_sample_pro_output(
            approved=False,
            rejection_reason="Critical contradiction between ECG and lab inputs; unsafe to publish.",
        ),
    )
    service_rejected = PrimaryConsiliumService(db=db_session, gateway=gateway_rejected)

    with pytest.raises(ConsiliumQualityRejectionError, match="Critical contradiction"):
        await service_rejected.run_primary_consilium(cycle_id=cycle_rejected)

    report_repo = ModelReportRepository(db_session)
    assert report_repo.list_client_facing(cycle_id=cycle_rejected) == []

    # Case C: Mid-cascade failure (e.g. Deputy Flash fails at call index 8)
    cycle_failed = "cycle_mid_failure"
    _seed_preparatory_cycle_context(db_session, cycle_id=cycle_failed)
    calls_failed: List[Dict[str, Any]] = []
    gateway_failed = _build_consilium_mock_gateway(
        recorded_calls=calls_failed,
        fail_on_call_index=8,
    )
    service_failed = PrimaryConsiliumService(db=db_session, gateway=gateway_failed)

    with pytest.raises(GeminiValidationError):
        await service_failed.run_primary_consilium(cycle_id=cycle_failed)

    assert report_repo.list_client_facing(cycle_id=cycle_failed) == []


def test_api_run_primary_and_client_reports_strict_privacy_and_versioning(
    client: TestClient,
    db_session,
    auth_headers: dict[str, str],
):
    """
    End-to-end FastAPI test for POST /api/v1/cascade/run-primary and GET /api/v1/reports/client:
    - Verifies authentication protection.
    - Verifies that internal shelves (flash_general_shelf, flash_training_shelf,
      pro_general_shelf / Athena strategy, pro_training_shelf, clinical_rationale)
      NEVER leak through POST /api/v1/cascade/run-primary or GET /api/v1/reports/client.
    - Verifies that running the cascade a second time increments version v1 -> v2
      without overwriting v1.
    """
    cycle_id = "cycle_api_e2e"
    _seed_preparatory_cycle_context(db_session, cycle_id=cycle_id)

    recorded_calls: List[Dict[str, Any]] = []
    app.dependency_overrides[get_gemini_gateway] = lambda: _build_consilium_mock_gateway(
        recorded_calls
    )

    try:
        headers = auth_headers

        # 1. Unauthorized call is rejected
        unauth_resp = client.post(
            "/api/v1/cascade/run-primary",
            json={"cycle_id": cycle_id, "report_date": "2026-10-18"},
        )
        assert unauth_resp.status_code == 401

        # 2. First authorized run -> creates v1 reports
        resp_v1 = client.post(
            "/api/v1/cascade/run-primary",
            json={"cycle_id": cycle_id, "report_date": "2026-10-18"},
            headers=headers,
        )
        assert resp_v1.status_code == 200
        body_v1 = resp_v1.json()
        raw_v1_str = json.dumps(body_v1)

        assert body_v1["cycle_id"] == cycle_id
        assert body_v1["overall_report"]["version"] == 1
        assert len(body_v1["system_reports"]) == 8

        # Ensure zero leakage of server_only shelves or Athena instructions
        forbidden_markers = [
            "INTERNAL FLASH GENERAL SHELF",
            "INTERNAL FLASH TRAINING SANDBOX",
            "SECRET_ATHENA_STRATEGY",
            "SECRET_PRO_SUPREME_SANDBOX",
            "SECRET_PRO_CLINICAL_RATIONALE",
            "flash_general_shelf",
            "flash_training_shelf",
            "pro_general_shelf",
            "pro_training_shelf",
            "system_general_shelf",
            "system_training_shelf",
            "clinical_rationale",
            "flash_quality_review",
        ]
        for marker in forbidden_markers:
            assert marker not in raw_v1_str

        # 3. Second authorized run -> creates v2 reports without overwriting v1
        resp_v2 = client.post(
            "/api/v1/cascade/run-primary",
            json={"cycle_id": cycle_id, "report_date": "2026-10-19"},
            headers=headers,
        )
        assert resp_v2.status_code == 200
        body_v2 = resp_v2.json()
        assert body_v2["overall_report"]["version"] == 2

        # 4. Fetch via GET /api/v1/reports/client and verify both v1 and v2 exist and are sterile
        get_resp = client.get(
            f"/api/v1/reports/client?cycle_id={cycle_id}",
            headers=headers,
        )
        assert get_resp.status_code == 200
        client_list = get_resp.json()
        # 9 client reports per run * 2 runs = 18 versioned client_facing reports
        assert len(client_list) == 18

        raw_get_str = json.dumps(client_list)
        for marker in forbidden_markers:
            assert marker not in raw_get_str

        report_repo = ModelReportRepository(db_session)
        overall_versions = report_repo.list_versions(
            cycle_id=cycle_id,
            report_type="user_overall_report",
        )
        assert [r.version for r in overall_versions] == [1, 2]
        assert overall_versions[0].content["report_date"] == "2026-10-18"
        assert overall_versions[1].content["report_date"] == "2026-10-19"
    finally:
        app.dependency_overrides.pop(get_gemini_gateway, None)


@pytest.mark.anyio
async def test_db_failure_mid_persistence_rolls_back_all_client_reports(db_session):
    """
    Verifies atomic transaction rollback when a database error occurs mid-way
    through saving the consilium reports (e.g. after several server_only and
    client_facing reports have already been flushed in the session).
    Zero client_facing reports and zero new consilium server_only reports may remain.
    """
    cycle_id = "cycle_atomic_rollback"
    _seed_preparatory_cycle_context(db_session, cycle_id=cycle_id)

    recorded_calls: List[Dict[str, Any]] = []
    gateway = _build_consilium_mock_gateway(recorded_calls)
    service = PrimaryConsiliumService(db=db_session, gateway=gateway)

    original_create_next_version = service.report_repo.create_next_version
    client_write_counter = {"count": 0}

    def failing_mid_client_persistence(*args, **kwargs):
        if kwargs.get("privacy_level") == "client_facing" or (
            hasattr(kwargs.get("privacy_level"), "value")
            and kwargs["privacy_level"].value == "client_facing"
        ):
            client_write_counter["count"] += 1
            # Fail on the 5th client_facing report after 4 client reports + 10 server reports were flushed
            if client_write_counter["count"] == 5:
                raise RuntimeError("Simulated SQLite disk I/O failure mid-transaction")
        return original_create_next_version(*args, **kwargs)

    service.report_repo.create_next_version = failing_mid_client_persistence  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="Simulated SQLite disk I/O failure mid-transaction"):
        await service.run_primary_consilium(cycle_id=cycle_id, report_date="2026-10-18")

    # Verify rollback: zero client_facing reports exist, and only the 3 preparatory server_only reports exist
    report_repo = ModelReportRepository(db_session)
    assert report_repo.list_client_facing(cycle_id=cycle_id) == []
    remaining_server_reports = report_repo.list_server_only(cycle_id=cycle_id)
    assert {r.report_type for r in remaining_server_reports} == {
        PREPARATORY_ANAMNESIS_REPORT_TYPE,
        PREPARATORY_OCR_REPORT_TYPE,
        PREPARATORY_ECG_REPORT_TYPE,
    }


@pytest.mark.anyio
async def test_incomplete_eight_systems_or_invalid_pro_corrections_are_rejected(db_session):
    """
    Verifies that incomplete or whitespace-only 8-system outputs from Flash or
    invalid/empty system corrections from Pro are strictly rejected and never published.
    """
    report_repo = ModelReportRepository(db_session)

    # Case 1: Flash omits one of the 8 required systems ('endocrine')
    cycle_missing_sys = "cycle_incomplete_flash_key"
    _seed_preparatory_cycle_context(db_session, cycle_id=cycle_missing_sys)
    incomplete_flash = _sample_flash_output()
    del incomplete_flash["user_system_reports"]["endocrine"]

    calls_1: List[Dict[str, Any]] = []
    gateway_1 = _build_consilium_mock_gateway(calls_1, flash_override=incomplete_flash)
    service_1 = PrimaryConsiliumService(db=db_session, gateway=gateway_1)

    with pytest.raises(GeminiValidationError):
        await service_1.run_primary_consilium(cycle_id=cycle_missing_sys)
    assert report_repo.list_client_facing(cycle_id=cycle_missing_sys) == []

    # Case 2: Flash returns whitespace-only string for one of the 8 systems ('kidneys')
    cycle_blank_sys = "cycle_blank_flash_key"
    _seed_preparatory_cycle_context(db_session, cycle_id=cycle_blank_sys)
    blank_flash = _sample_flash_output()
    blank_flash["user_system_reports"]["kidneys"] = "    \n\t  "

    calls_2: List[Dict[str, Any]] = []
    gateway_2 = _build_consilium_mock_gateway(calls_2, flash_override=blank_flash)
    service_2 = PrimaryConsiliumService(db=db_session, gateway=gateway_2)

    with pytest.raises(GeminiValidationError):
        await service_2.run_primary_consilium(cycle_id=cycle_blank_sys)
    assert report_repo.list_client_facing(cycle_id=cycle_blank_sys) == []

    # Case 3: Pro provides an unknown system key in corrected_user_system_reports
    cycle_bad_pro_key = "cycle_bad_pro_key"
    _seed_preparatory_cycle_context(db_session, cycle_id=cycle_bad_pro_key)
    bad_pro_key = _sample_pro_output(
        approved=True,
        corrected_reports={"unknown_organ": "Some text"},
    )

    calls_3: List[Dict[str, Any]] = []
    gateway_3 = _build_consilium_mock_gateway(calls_3, pro_override=bad_pro_key)
    service_3 = PrimaryConsiliumService(db=db_session, gateway=gateway_3)

    with pytest.raises(GeminiValidationError):
        await service_3.run_primary_consilium(cycle_id=cycle_bad_pro_key)
    assert report_repo.list_client_facing(cycle_id=cycle_bad_pro_key) == []

    # Case 4: Pro provides whitespace-only text in corrected_user_system_reports
    cycle_blank_pro = "cycle_blank_pro_val"
    _seed_preparatory_cycle_context(db_session, cycle_id=cycle_blank_pro)
    blank_pro_val = _sample_pro_output(
        approved=True,
        corrected_reports={"cvs": "   "},
    )

    calls_4: List[Dict[str, Any]] = []
    gateway_4 = _build_consilium_mock_gateway(calls_4, pro_override=blank_pro_val)
    service_4 = PrimaryConsiliumService(db=db_session, gateway=gateway_4)

    with pytest.raises(GeminiValidationError):
        await service_4.run_primary_consilium(cycle_id=cycle_blank_pro)
    assert report_repo.list_client_facing(cycle_id=cycle_blank_pro) == []


@pytest.mark.anyio
async def test_preparatory_role_mismatch_and_client_payload_injection_rejected(
    client: TestClient,
    db_session,
    auth_headers: dict[str, str],
):
    """
    Verifies:
    1. POST /api/v1/cascade/run-primary rejects any client attempt to inject arbitrary
       M1, OCR, or ECG payloads in the request body (extra='forbid' -> 422).
    2. PrimaryConsiliumService rejects preparatory records if their stored agent_role
       does not match the expected preparatory role ('anamnesis_m1', 'vision_ocr', 'vision_ecg').
    """
    cycle_id = "cycle_tamper_check"
    cycle_repo = MedicalCycleRepository(db_session)
    cycle_repo.create(cycle_id=cycle_id, status="active")

    # 1. Client attempts to pass arbitrary M1/labs payload into POST /api/v1/cascade/run-primary
    injected_resp = client.post(
        "/api/v1/cascade/run-primary",
        json={
            "cycle_id": cycle_id,
            "anamnesis_m1": _sample_anamnesis_output(),
            "shared_labs_table": {"hemoglobin": "999"},
        },
        headers=auth_headers,
    )
    assert injected_resp.status_code == 422

    # 2. Tampered agent_role in DB record is rejected by _load_preparatory_anamnesis
    report_repo = ModelReportRepository(db_session)
    tampered_payload = _sample_anamnesis_output()
    tampered_payload["agent_role"] = AegisAgentRole.ATHENA_CURATOR.value
    report_repo.create_next_version(
        cycle_id=cycle_id,
        model="gemini-3.6-flash",
        report_type=PREPARATORY_ANAMNESIS_REPORT_TYPE,
        privacy_level="server_only",
        content=tampered_payload,
    )

    calls: List[Dict[str, Any]] = []
    gateway = _build_consilium_mock_gateway(calls)
    service = PrimaryConsiliumService(db=db_session, gateway=gateway)

    with pytest.raises(ConsiliumContextMissingError, match="Invalid agent_role"):
        await service.run_primary_consilium(cycle_id=cycle_id)
    assert len(calls) == 0
