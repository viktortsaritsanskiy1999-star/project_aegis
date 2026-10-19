import asyncio
from datetime import datetime, timezone
import json
from typing import Any, Dict, List
import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from sqlalchemy.exc import OperationalError, IntegrityError

from app.main import app
from app.repositories.cycle_repository import MedicalCycleRepository
from app.repositories.measurement_repository import HealthMeasurementRepository
from app.repositories.report_repository import ModelReportRepository
from app.routers.cascade import get_gemini_gateway
from app.schemas.consilium import (
    SYSTEM_KEYS,
    AegisAgentRole,
    AnamnesisQuestionnaireAnswers,
)
from app.schemas.privacy import PrivacyLevel
from app.services.consilium_prompts import (
    build_anamnesis_m1_context_from_answers,
    build_anamnesis_prompt,
)
from app.services.gemini_gateway import GeminiGateway
from app.services.primary_consilium_service import (
    PrimaryConsiliumService,
    ConsiliumAnamnesisLockedError,
    ConsiliumConcurrencyConflictError,
    RAW_USER_ANAMNESIS_REPORT_TYPE,
    PREPARATORY_ANAMNESIS_REPORT_TYPE,
    PREPARATORY_OCR_REPORT_TYPE,
    PREPARATORY_ECG_REPORT_TYPE,
)
from tests.test_primary_consilium import (
    _gemini_envelope,
    _sample_anamnesis_output,
    _sample_system_agent_output,
    _sample_flash_output,
    _sample_pro_output,
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


async def _no_sleep(_: float) -> None:
    pass


def _build_m1_mock_gateway(
    recorded_calls: List[Dict[str, Any]],
    m1_override: Dict[str, Any] | None = None,
    fail: bool = False,
    artificial_delay_sec: float = 0.0,
) -> GeminiGateway:
    async def handler(request: httpx.Request) -> httpx.Response:
        if artificial_delay_sec > 0:
            await asyncio.sleep(artificial_delay_sec)
        body = json.loads(request.content.decode("utf-8"))
        prompt_text = body["contents"][0]["parts"][0]["text"]
        recorded_calls.append(
            {
                "url_path": request.url.path,
                "prompt": prompt_text,
            }
        )
        if fail:
            return httpx.Response(
                500,
                json={"error": {"message": "Simulated Gemini Flash M1 outage"}},
            )
        payload = m1_override or _sample_anamnesis_output()
        return httpx.Response(200, json=_gemini_envelope(payload))

    return GeminiGateway(
        api_key="test-key",
        transport=httpx.MockTransport(handler),
        sleep_func=_no_sleep,
    )


@pytest.fixture
def active_cycle(db_session) -> str:
    cycle_id = "cycle_anamnesis_test_01"
    cycle_repo = MedicalCycleRepository(db_session)
    cycle_repo.create(cycle_id=cycle_id, status="active")
    return cycle_id


# ==============================================================================
# 1. PRIVACY & METADATA-ONLY CLIENT RESPONSE TESTS
# ==============================================================================

def test_anamnesis_intake_success_zero_ai_leakage(client: TestClient, db_session, active_cycle, auth_headers):
    """
    Test 1: Full valid answers intake successfully invokes Gemini Flash (M1).
    Client response contains ONLY service metadata (cycle_id, status, version, timestamp, notice).
    STRICT PRIVACY: zero preparatory AI conclusions, NO summary, NO system_logic, NO training_sandbox.
    """
    recorded: List[Dict[str, Any]] = []
    mock_gateway = _build_m1_mock_gateway(recorded)
    app.dependency_overrides[get_gemini_gateway] = lambda: mock_gateway

    answers = _sample_intake_answers(
        nicotine_status="User",
        nicotine_years="1-5 years",
        nicotine_freq="Daily",
        chronic_diseases="chr_heart",
        user_text_chronic_diseases="Mild stage 1 hypertension",
    )

    response = client.post(
        "/api/v1/cascade/intake/anamnesis",
        headers=auth_headers,
        json={"cycle_id": active_cycle, "answers": answers},
    )

    assert response.status_code == 200, response.text
    data = response.json()
    assert data["cycle_id"] == active_cycle
    assert data["status"] == "processed"
    assert data["intake_version"] == 1
    assert "privacy_notice" in data
    assert "created_at_utc" in data

    # Strict privacy verification: client receives NO AI conclusions or summaries
    assert "summary" not in data
    assert "user_view" not in data
    assert "system_logic" not in data
    assert "training_sandbox" not in data
    assert "raw_diagnostic_hypothesis" not in data
    assert "internal_comment" not in data
    assert "system_folders" not in data


def test_anamnesis_intake_optional_fields_omitted_or_none(client: TestClient, db_session, active_cycle, auth_headers):
    """
    Test 2: Optional fields (nicotine details, custom user texts, onco status) can be omitted or None.
    """
    recorded: List[Dict[str, Any]] = []
    mock_gateway = _build_m1_mock_gateway(recorded)
    app.dependency_overrides[get_gemini_gateway] = lambda: mock_gateway

    answers = _sample_intake_answers()
    answers.pop("nicotine_years", None)
    answers.pop("nicotine_freq", None)
    answers.pop("alcohol_type", None)
    answers.pop("onco_status", None)
    answers.pop("user_text_medications", None)

    response = client.post(
        "/api/v1/cascade/intake/anamnesis",
        headers=auth_headers,
        json={"cycle_id": active_cycle, "answers": answers},
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "processed"


def test_anamnesis_intake_rejects_extra_unallowed_fields(client: TestClient, db_session, active_cycle, auth_headers):
    """
    Test 3: Reject unallowed arbitrary fields with 422 Unprocessable Entity (extra='forbid').
    """
    answers = _sample_intake_answers()
    answers["injected_client_field"] = "malicious_payload"

    response = client.post(
        "/api/v1/cascade/intake/anamnesis",
        headers=auth_headers,
        json={"cycle_id": active_cycle, "answers": answers},
    )
    assert response.status_code == 422


def test_anamnesis_intake_rejects_client_supplied_ai_or_lab_outputs(client: TestClient, db_session, active_cycle, auth_headers):
    """
    Test 4: Reject client attempts to inject AI conclusions or lab results.
    """
    answers1 = _sample_intake_answers()
    answers1["system_logic"] = {"cvs": {"data": "client injected", "internal_comment": "fake"}}
    r1 = client.post(
        "/api/v1/cascade/intake/anamnesis",
        headers=auth_headers,
        json={"cycle_id": active_cycle, "answers": answers1},
    )
    assert r1.status_code == 422

    answers2 = _sample_intake_answers()
    answers2["shared_labs"] = {"troponin": "positive"}
    r2 = client.post(
        "/api/v1/cascade/intake/anamnesis",
        headers=auth_headers,
        json={"cycle_id": active_cycle, "answers": answers2},
    )
    assert r2.status_code == 422


def test_blind_mode_guarantee_no_labs_in_m1_prompt(client: TestClient, db_session, active_cycle, auth_headers):
    """
    Test 5: Verify M1 prompt receives clean physiological and demographic context across 8 systems,
    and has zero lab markers or test references.
    """
    recorded: List[Dict[str, Any]] = []
    mock_gateway = _build_m1_mock_gateway(recorded)
    app.dependency_overrides[get_gemini_gateway] = lambda: mock_gateway

    answers = _sample_intake_answers(
        gender="Female",
        age=45,
        height=168,
        weight=62,
        diet_types="diet_vegan",
        diet_habits="hab_coffee, hab_salt",
    )

    response = client.post(
        "/api/v1/cascade/intake/anamnesis",
        headers=auth_headers,
        json={"cycle_id": active_cycle, "answers": answers},
    )
    assert response.status_code == 200

    assert len(recorded) == 1
    prompt = recorded[0]["prompt"]

    assert "Female" in prompt
    assert "168" in prompt
    assert "62" in prompt
    assert "system_folders" in prompt
    for key in SYSTEM_KEYS:
        assert f'"{key}"' in prompt

    prompt_lower = prompt.lower()
    for forbidden in ("shared_labs", "isolated_labs", "shared_labs_shelf", "blood_count", "biochemistry", "coagulation"):
        assert forbidden not in prompt_lower


def test_server_storage_atomic_dual_records_and_server_only_privacy(client: TestClient, db_session, active_cycle, auth_headers):
    """
    Test 6: Verify DB contains both raw_user_anamnesis and preparatory_anamnesis_m1 with server_only privacy level.
    Verify get_client_facing raises PermissionError on both.
    """
    recorded: List[Dict[str, Any]] = []
    mock_gateway = _build_m1_mock_gateway(recorded)
    app.dependency_overrides[get_gemini_gateway] = lambda: mock_gateway

    answers = _sample_intake_answers(
        sport_level="High",
        medications="med_stat",
        user_text_medications="Atorvastatin 10mg",
    )

    response = client.post(
        "/api/v1/cascade/intake/anamnesis",
        headers=auth_headers,
        json={"cycle_id": active_cycle, "answers": answers},
    )
    assert response.status_code == 200

    report_repo = ModelReportRepository(db_session)

    # 1. Check raw_user_anamnesis
    raw_reports = report_repo.list_server_only(cycle_id=active_cycle, report_type=RAW_USER_ANAMNESIS_REPORT_TYPE)
    assert len(raw_reports) == 1
    assert raw_reports[0].privacy_level == PrivacyLevel.SERVER_ONLY.value
    assert raw_reports[0].version == 1
    assert raw_reports[0].content["answers"]["sport_level"] == "High"
    assert raw_reports[0].content["answers"]["user_text_medications"] == "Atorvastatin 10mg"

    # 2. Check preparatory_anamnesis_m1
    m1_reports = report_repo.list_server_only(cycle_id=active_cycle, report_type=PREPARATORY_ANAMNESIS_REPORT_TYPE)
    assert len(m1_reports) == 1
    assert m1_reports[0].privacy_level == PrivacyLevel.SERVER_ONLY.value
    assert m1_reports[0].version == 1
    assert m1_reports[0].content["agent_role"] == AegisAgentRole.ANAMNESIS_M1.value
    assert "user_view" in m1_reports[0].content
    assert "system_logic" in m1_reports[0].content
    assert "training_sandbox" in m1_reports[0].content

    # 3. Privacy boundary check: get_client_facing must forbid returning server_only reports
    with pytest.raises(PermissionError):
        report_repo.get_client_facing(raw_reports[0].report_id)

    with pytest.raises(PermissionError):
        report_repo.get_client_facing(m1_reports[0].report_id)


def test_atomic_rollback_on_gemini_failure(client: TestClient, db_session, active_cycle, auth_headers):
    """
    Test 7: If Gemini fails, 502 Bad Gateway is returned and 0 records are persisted in the database.
    """
    recorded: List[Dict[str, Any]] = []
    mock_gateway = _build_m1_mock_gateway(recorded, fail=True)
    app.dependency_overrides[get_gemini_gateway] = lambda: mock_gateway

    answers = _sample_intake_answers()

    response = client.post(
        "/api/v1/cascade/intake/anamnesis",
        headers=auth_headers,
        json={"cycle_id": active_cycle, "answers": answers},
    )
    assert response.status_code == 502

    report_repo = ModelReportRepository(db_session)
    assert len(report_repo.list_server_only(cycle_id=active_cycle, report_type=RAW_USER_ANAMNESIS_REPORT_TYPE)) == 0
    assert len(report_repo.list_server_only(cycle_id=active_cycle, report_type=PREPARATORY_ANAMNESIS_REPORT_TYPE)) == 0


def test_cycle_not_found_returns_404(client: TestClient, db_session, auth_headers):
    """
    Test 8: Submitting answers for non-existent cycle returns 404 Not Found.
    """
    recorded: List[Dict[str, Any]] = []
    mock_gateway = _build_m1_mock_gateway(recorded)
    app.dependency_overrides[get_gemini_gateway] = lambda: mock_gateway

    answers = _sample_intake_answers()

    response = client.post(
        "/api/v1/cascade/intake/anamnesis",
        headers=auth_headers,
        json={"cycle_id": "non_existent_cycle_999", "answers": answers},
    )
    assert response.status_code == 404
    assert "not found" in response.json()["detail"].lower()


# ==============================================================================
# 2. RESUBMISSION & LOCKING SCENARIO TESTS (POINT 2)
# ==============================================================================

def test_anamnesis_intake_duplicate_submission_is_idempotent(client: TestClient, db_session, active_cycle, auth_headers):
    """
    Scenario 2.2: User accidentally sends identical intake request.
    Does NOT increment version and does NOT re-invoke Gemini Flash M1; returns existing v1 idempotently.
    """
    recorded: List[Dict[str, Any]] = []
    mock_gateway = _build_m1_mock_gateway(recorded)
    app.dependency_overrides[get_gemini_gateway] = lambda: mock_gateway

    answers = _sample_intake_answers(weight=78)

    # First submission -> version 1, calls Gemini
    r1 = client.post(
        "/api/v1/cascade/intake/anamnesis",
        headers=auth_headers,
        json={"cycle_id": active_cycle, "answers": answers},
    )
    assert r1.status_code == 200
    assert r1.json()["intake_version"] == 1
    assert len(recorded) == 1

    # Accidental duplicate submission with identical answers -> still version 1, NO extra Gemini call!
    r2 = client.post(
        "/api/v1/cascade/intake/anamnesis",
        headers=auth_headers,
        json={"cycle_id": active_cycle, "answers": answers},
    )
    assert r2.status_code == 200
    assert r2.json()["intake_version"] == 1
    assert len(recorded) == 1  # No extra Gemini call made!

    report_repo = ModelReportRepository(db_session)
    assert len(report_repo.list_server_only(cycle_id=active_cycle, report_type=RAW_USER_ANAMNESIS_REPORT_TYPE)) == 1


def test_anamnesis_intake_corrected_answers_before_primary_increments_version(client: TestClient, db_session, active_cycle, auth_headers):
    """
    Scenario 2.3: User submits corrected answers before primary consilium.
    Re-runs M1 and increments intake version (v1 -> v2).
    """
    recorded: List[Dict[str, Any]] = []
    mock_gateway = _build_m1_mock_gateway(recorded)
    app.dependency_overrides[get_gemini_gateway] = lambda: mock_gateway

    # v1
    r1 = client.post(
        "/api/v1/cascade/intake/anamnesis",
        headers=auth_headers,
        json={"cycle_id": active_cycle, "answers": _sample_intake_answers(weight=78)},
    )
    assert r1.status_code == 200
    assert r1.json()["intake_version"] == 1
    assert len(recorded) == 1

    # v2 (weight corrected to 76)
    r2 = client.post(
        "/api/v1/cascade/intake/anamnesis",
        headers=auth_headers,
        json={"cycle_id": active_cycle, "answers": _sample_intake_answers(weight=76)},
    )
    assert r2.status_code == 200
    assert r2.json()["intake_version"] == 2
    assert len(recorded) == 2

    report_repo = ModelReportRepository(db_session)
    raw_reports = report_repo.list_server_only(cycle_id=active_cycle, report_type=RAW_USER_ANAMNESIS_REPORT_TYPE)
    assert len(raw_reports) == 2
    assert raw_reports[0].version == 2
    assert raw_reports[0].content["answers"]["weight"] == 76


@pytest.mark.anyio
async def test_anamnesis_intake_locked_after_primary_consilium_returns_409(client: TestClient, db_session, active_cycle, auth_headers):
    """
    Scenario 2.4: User attempts to modify/resubmit questionnaire AFTER Primary Consilium has concluded.
    Strictly forbids altering M1; returns 409 Conflict (ConsiliumAnamnesisLockedError).
    """
    # 1. Run initial intake
    recorded: List[Dict[str, Any]] = []
    m1_gateway = _build_m1_mock_gateway(recorded)
    service = PrimaryConsiliumService(db=db_session, gateway=m1_gateway)
    answers = AnamnesisQuestionnaireAnswers.model_validate(_sample_intake_answers())
    await service.process_anamnesis_intake(cycle_id=active_cycle, answers=answers)

    # 2. Seed vitals + OCR + ECG and execute Primary Consilium
    measurement_repo = HealthMeasurementRepository(db_session)
    measurement_repo.create(
        cycle_id=active_cycle,
        measured_at=datetime(2026, 10, 18, 9, 0, tzinfo=timezone.utc),
        source="watch_sensor",
        pulse=64,
        systolic=120,
        diastolic=80,
        spo2=98,
    )
    service.save_preparatory_ocr_labs(
        cycle_id=active_cycle,
        shared_labs_table={"hemoglobin": "145 g/L"},
        created_at=datetime(2026, 10, 18, 9, 0, tzinfo=timezone.utc),
    )
    service.save_preparatory_ecg_summary(cycle_id=active_cycle, ecg_summary="Normal sinus.")

    call_counter = {"index": 0}
    async def consilium_handler(request: httpx.Request) -> httpx.Response:
        idx = call_counter["index"]
        call_counter["index"] += 1
        if idx < 8:
            return httpx.Response(200, json=_gemini_envelope(_sample_system_agent_output(SYSTEM_KEYS[idx])))
        if idx == 8:
            return httpx.Response(200, json=_gemini_envelope(_sample_flash_output()))
        if idx == 9:
            return httpx.Response(200, json=_gemini_envelope(_sample_pro_output()))
        raise AssertionError(f"Unexpected call index {idx}")

    consilium_gateway = GeminiGateway(
        api_key="test-key",
        transport=httpx.MockTransport(consilium_handler),
        sleep_func=_no_sleep,
    )
    primary_srv = PrimaryConsiliumService(db=db_session, gateway=consilium_gateway)
    await primary_srv.run_primary_consilium(cycle_id=active_cycle)
    assert primary_srv.is_primary_consilium_completed(active_cycle) is True

    # 3. Attempt to alter questionnaire after primary consilium completion -> 409 Conflict
    app.dependency_overrides[get_gemini_gateway] = lambda: m1_gateway
    response = client.post(
        "/api/v1/cascade/intake/anamnesis",
        headers=auth_headers,
        json={"cycle_id": active_cycle, "answers": _sample_intake_answers(weight=82)},
    )
    assert response.status_code == 409
    assert "cannot be modified" in response.json()["detail"].lower()
    assert "primary consilium" in response.json()["detail"].lower()


# ==============================================================================
# 3. VAULTSERVICE ACCURATE MAPPING AUDIT TESTS (POINT 3)
# ==============================================================================

def test_vault_service_comprehensive_system_mapping_accuracy():
    """
    Test 3: Comprehensive verification of build_anamnesis_m1_context_from_answers against VaultService.saveAnamnesis:
    - Biological profile (gender, height, weight, age)
    - Multiorgan distribution of lifestyle factors (nicotine, alcohol, spec factors, sleep, diet, work, sport)
    - Multiorgan distribution of chronic diseases, infections, heredity, and medications
    - Preservation and trigger activation from free text inputs (user_text_*)
    """
    answers = AnamnesisQuestionnaireAnswers(
        age_confirmation="Confirmed",
        gender="Female",
        age=42,
        height=165,
        weight=60,
        nicotine_status="Active",
        nicotine_years="5-10 years",
        nicotine_freq="Pack+",
        alcohol_status="Frequently",
        alcohol_type="Beer, Wine",
        spec_factors="spec_stim, spec_inhale",
        sleep_quality="Бессонница",
        diet_types="diet_meat, diet_junk, diet_fish, diet_milk",
        diet_habits="hab_sugar, hab_salt, hab_coffee, hab_night, hab_water",
        gi_signals="gi_bloat, gi_burn",
        work_type="Sedentary",
        work_schedule="Shift",
        sport_level="High",
        chronic_diseases="chr_heart, chr_diabetes, chr_asthma, chr_bones, chr_neuro",
        onco_status="Remission",
        recent_illness="ill_poison",
        critical_infections="safe_hep, safe_siph, safe_hiv, safe_tub",
        heredity_risks="her_heart, her_onco, her_diab, her_brain",
        medications="med_horm, med_stat, med_pain",
        user_text_medications="Metformin 500mg",
        user_text_chronic_diseases="Gastritis and lumbar osteochondrosis",
        user_text_diet_habits="Excess caffeine in evening",
        user_text_spec_factors="Pre-workout booster",
        user_text_gi_signals="Reflux after coffee",
        user_text_recent_illness="Food poisoning 2 weeks ago",
        user_text_critical_infections="Treated syphilis in 2018",
        user_text_heredity_risks="Father had myocardial infarction at 48",
    )

    context = build_anamnesis_m1_context_from_answers(answers)
    sf = context["system_folders"]
    core = context["core_data_log"]

    # 1. Biological profile checks
    assert sf["endocrine"]["user_gender"] == "Female"
    assert sf["kidneys"]["user_gender"] == "Female"
    assert sf["cvs"]["user_gender"] == "Female"
    assert sf["endocrine"]["physical_height"] == 165
    assert sf["endocrine"]["physical_weight"] == 60
    assert sf["cvs"]["load_height"] == 165
    assert sf["cvs"]["load_weight"] == 60
    assert sf["musculoskeletal"]["axial_load_height"] == 165
    assert sf["musculoskeletal"]["axial_load_weight"] == 60
    assert core["age"] == 42

    # 2. Nicotine checks (simultaneous respiratory, cvs, immune)
    assert sf["respiratory"]["smoking_nicotine_status"] == "Active"
    assert sf["cvs"]["vessel_nicotine_status"] == "Active"
    assert sf["immune"]["broncho_local_imm_disruption"] is True

    # 3. Alcohol checks (simultaneous gi_tract, nervous, kidneys, endocrine, cvs)
    assert sf["gi_tract"]["liver_tox_load"] == "Alcohol consumption recorded"
    assert sf["nervous"]["gaba_neuro_load"] == "Ethanol-induced GABA modulation"
    assert sf["kidneys"]["uric_acid_risk"] is True
    assert sf["endocrine"]["visceral_adipose_trigger"] is True
    assert sf["cvs"]["cardiomyopathy_risk"] is True

    # 4. Spec factors (simultaneous nervous, cvs, gi_tract, respiratory)
    assert "Stimulation" in sf["nervous"]["synaptic_overdrive"]
    assert "Vasoconstrictor" in sf["cvs"]["vasoconstriction_tachy_response"]
    assert "hyperacidity" in sf["gi_tract"]["mucosal_ischemia_hyperacidity"]
    assert "alveolar" in sf["respiratory"]["alveolar_stress"]

    # 5. Sleep checks
    assert "Circadian" in sf["endocrine"]["circadian_desynchronosis"]
    assert sf["nervous"]["glymphatic_clearance_disruption"] is True

    # 6. Diet checks (protease in gi, nitrogen in kidneys, trans fats in cvs & endocrine, omega3 in cvs)
    assert "protease" in sf["gi_tract"]["protease_load"].lower()
    assert "nitrogen" in sf["kidneys"]["nitrogen_waste_load"].lower()
    assert "glycemic" in sf["endocrine"]["insulin_spike_risk"].lower()
    assert "omega-3" in sf["cvs"]["omega3_cardioprotection"].lower()
    assert "calcium" in sf["musculoskeletal"]["minor_calcium_source"].lower()

    # 7. Diet habits (sugar on endocrine, salt on cvs/kidneys, coffee on nervous/cvs/gi, night on gi/endocrine, water on kidneys/cvs)
    assert "glycation" in sf["endocrine"]["carb_metabolism_glycation"]
    assert "volemic" in sf["cvs"]["volemic_hypertension_load"]
    assert "osmotic" in sf["kidneys"]["tubular_osmotic_load"]
    assert "adenosine" in sf["nervous"]["adenosine_receptor_blockade"]
    assert "catecholamine" in sf["cvs"]["arrhythmogenic_potential_bp_fluctuation"].lower()
    assert "secretion" in sf["gi_tract"]["gastric_acid_stimulation"]
    assert "reflux" in sf["gi_tract"]["nocturnal_reflux_risk"]
    assert "suppressing" in sf["endocrine"]["nocturnal_sth_blockade"].lower()
    assert "urolithiasis" in sf["kidneys"]["hypersthenuria_urolithiasis_risk"]
    assert "hemoconcentration" in sf["cvs"]["hemoconcentration_viscosity_load"].lower()

    # 8. Work & Sport
    assert "Biomechanics" in sf["musculoskeletal"]["biomechanics_work_type"]
    assert "Shift" in sf["musculoskeletal"]["biomechanics_work_schedule"]
    assert "High" in sf["musculoskeletal"]["biomechanics_sport_level"]
    assert "sympathicotonia" in sf["nervous"]["sympathicotonia_stress"]
    assert "cortisol" in sf["endocrine"]["adrenal_distress_cortisol"]

    # 9. Chronic diseases (diabetes on endocrine/cvs/kidneys, cvs pathology, asthma on respiratory/immune, bones, neuro, gi)
    assert "diabetes" in sf["endocrine"]["primary_defect_db"].lower()
    assert "atherosclerosis" in sf["cvs"]["macroangiopathy_accelerated_atherosclerosis"].lower()
    assert "hyperfiltration" in sf["kidneys"]["diabetic_nephropathy_hyperfiltration"].lower()
    assert "hyperreactivity" in sf["respiratory"]["bronchial_hyperreactivity"]
    assert "atopic" in sf["immune"]["th2_atopic_shift_ige"].lower()
    assert "degenerative" in sf["musculoskeletal"]["chronic_degradation_inflammation"]
    assert "cns/pns" in sf["nervous"]["chronic_cns_pns_pathology"].lower()
    assert "gastritis" in answers.user_text_chronic_diseases.lower()
    assert "chronic_gi_pathology" in sf["gi_tract"]

    # 10. Oncology
    assert "oncological" in sf["immune"]["antitumor_surveillance_deficit"].lower()

    # 11. Recent illness & poisoning (immune, respiratory, gi mucosal barrier, kidneys prerenal)
    assert "post-viral" in sf["immune"]["postviral_exhaustion_formula_shift"].lower()
    assert "residual" in sf["respiratory"]["residual_lung_markers"].lower()
    assert "mucosal" in sf["gi_tract"]["mucosal_barrier_destruction_malabsorption"].lower()
    assert sf["kidneys"]["prerenal_dehydration_risk"] is True

    # 12. Critical infections (hepatitis on gi/immune, HIV on immune, TB on respiratory/immune, syphilis on immune/nervous/cvs/kidneys)
    assert "fibrosis" in sf["gi_tract"]["hepatocyte_cytolysis_fibrosis"].lower()
    assert "t-cell" in sf["immune"]["cd4_destruction_immunodeficiency"].lower()
    assert "tuberculosis" in sf["respiratory"]["tbc_lung_lesions"].lower()
    assert sf["nervous"]["neurosyphilis_organic_risk"] is True
    assert sf["cvs"]["mesoaortitis_vascular_risk"] is True
    assert sf["kidneys"]["membranous_nephropathy_immune_complex"] is True

    # 13. Heredity (early CVD on cvs, onco on immune, diab on endocrine, alzheimer on nervous)
    assert "cvd" in sf["cvs"]["genetic_dyslipidemia_atherosclerosis"].lower()
    assert "oncological" in sf["immune"]["genetic_tumor_surveillance_weakness"].lower()
    assert "predisposition" in sf["endocrine"]["polygenic_diabetes_predisposition"]
    assert "neurodegeneration" in sf["nervous"]["tau_amyloidosis_genetic_risk"]

    # 14. Medications (hormones on endocrine/cvs, statins on cvs/gi, nsaids on gi/kidneys/cvs)
    assert "exogenous" in sf["endocrine"]["exogenous_hormonal_background"].lower()
    assert "thrombosis" in sf["cvs"]["estrogen_hypercoagulation_thrombosis_risk"].lower()
    assert "lipid" in sf["cvs"]["lipid_management_pharmacology"].lower()
    assert "transaminase" in sf["gi_tract"]["statin_liver_transaminase_control"].lower()
    assert "gastropathy" in sf["gi_tract"]["cox1_inhibition_gastropathy_erosion"].lower()
    assert "arteriole" in sf["kidneys"]["prostaglandin_block_afferent_arteriole_spasm"].lower()
    assert "prohypertensive" in sf["cvs"]["nsaid_hypertension_thrombotic_shift"].lower()

    # 15. Custom notes preservation
    assert "user_custom_notes" in core
    assert core["user_custom_notes"]["user_text_medications"] == "Metformin 500mg"


# ==============================================================================
# 4. CONCURRENCY & END-TO-END TESTS (POINT 4)
# ==============================================================================

@pytest.mark.anyio
async def test_end_to_end_compatibility_with_primary_consilium(db_session, active_cycle):
    """
    Test 10: After submitting anamnesis intake, PrimaryConsiliumService.run_primary_consilium
    successfully consumes the stored preparatory_anamnesis_m1 report and runs the full cascade.
    """
    # 1. Run intake via service
    recorded: List[Dict[str, Any]] = []
    m1_gateway = _build_m1_mock_gateway(recorded)
    service = PrimaryConsiliumService(db=db_session, gateway=m1_gateway)

    answers = AnamnesisQuestionnaireAnswers.model_validate(_sample_intake_answers())
    intake_resp = await service.process_anamnesis_intake(cycle_id=active_cycle, answers=answers)
    assert intake_resp.intake_version == 1

    # 2. Seed remaining preparatory items for primary consilium: vitals, vision ocr, vision ecg
    measurement_repo = HealthMeasurementRepository(db_session)
    measurement_repo.create(
        cycle_id=active_cycle,
        measured_at=datetime(2026, 10, 18, 9, 0, tzinfo=timezone.utc),
        source="watch_sensor",
        pulse=64,
        systolic=120,
        diastolic=80,
        spo2=98,
    )
    service.save_preparatory_ocr_labs(
        cycle_id=active_cycle,
        shared_labs_table={"hemoglobin": "145 g/L", "glucose": "5.0 mmol/L"},
        created_at=datetime(2026, 10, 18, 9, 0, tzinfo=timezone.utc),
    )
    service.save_preparatory_ecg_summary(
        cycle_id=active_cycle,
        ecg_summary="Sinus rhythm, normal axis, no pathology.",
    )

    # 3. Build mock gateway for the 10 Primary Consilium calls (8 Flash-Lite -> Flash -> Pro)
    call_counter = {"index": 0}

    async def consilium_handler(request: httpx.Request) -> httpx.Response:
        idx = call_counter["index"]
        call_counter["index"] += 1
        if idx < 8:
            return httpx.Response(200, json=_gemini_envelope(_sample_system_agent_output(SYSTEM_KEYS[idx])))
        if idx == 8:
            return httpx.Response(200, json=_gemini_envelope(_sample_flash_output()))
        if idx == 9:
            return httpx.Response(200, json=_gemini_envelope(_sample_pro_output()))
        raise AssertionError(f"Unexpected call index {idx}")

    consilium_gateway = GeminiGateway(
        api_key="test-consilium-key",
        transport=httpx.MockTransport(consilium_handler),
        sleep_func=_no_sleep,
    )

    # 4. Execute Primary Consilium — it will load the intake's preparatory_anamnesis_m1 report!
    consilium_service = PrimaryConsiliumService(db=db_session, gateway=consilium_gateway)
    consilium_result = await consilium_service.run_primary_consilium(cycle_id=active_cycle)

    assert consilium_result.cycle_id == active_cycle
    assert len(consilium_result.system_reports) == 8
    assert consilium_result.overall_report.content.title == "Aegis Personal Wellness Roadmap"
    assert consilium_result.vitals_thresholds.min_pessimum_bpm == 48


@pytest.mark.anyio
async def test_concurrent_identical_intake_across_independent_sessions(db_session, active_cycle):
    """
    Test 11: Two independent worker processes (simulated by separate SQLAlchemy Sessions
    and separate in-memory lock registries) concurrently submit identical answers.
    Verifies:
    - Both workers return success with intake_version=1.
    - Exactly ONE raw_user_anamnesis and ONE preparatory_anamnesis_m1 are committed to DB.
    - Zero incompatible versions, zero duplicate version numbers, zero partial records.
    """
    engine_bind = db_session.get_bind()
    worker_session_1 = Session(bind=engine_bind, autocommit=False, autoflush=False)
    worker_session_2 = Session(bind=engine_bind, autocommit=False, autoflush=False)

    recorded_calls: List[Dict[str, Any]] = []
    shared_gateway = _build_m1_mock_gateway(recorded_calls, artificial_delay_sec=0.02)

    try:
        worker_1 = PrimaryConsiliumService(
            db=worker_session_1,
            gateway=shared_gateway,
            lock_registry={},  # isolated process memory
        )
        worker_2 = PrimaryConsiliumService(
            db=worker_session_2,
            gateway=shared_gateway,
            lock_registry={},  # isolated process memory
        )

        answers = AnamnesisQuestionnaireAnswers.model_validate(_sample_intake_answers(gender="Female", weight=62))

        results = await asyncio.gather(
            worker_1.process_anamnesis_intake(cycle_id=active_cycle, answers=answers),
            worker_2.process_anamnesis_intake(cycle_id=active_cycle, answers=answers),
            return_exceptions=True,
        )
    finally:
        worker_session_1.close()
        worker_session_2.close()

    # Both workers must succeed with intake_version=1
    successes = [r for r in results if not isinstance(r, Exception)]
    assert len(successes) == 2, f"Expected 2 successful responses, got: {results!r}"
    assert successes[0].intake_version == 1
    assert successes[1].intake_version == 1

    # Check DB via main session: strictly 1 raw report and 1 preparatory report
    db_session.expire_all()
    report_repo = ModelReportRepository(db_session)
    raw_reports = report_repo.list_server_only(cycle_id=active_cycle, report_type=RAW_USER_ANAMNESIS_REPORT_TYPE)
    m1_reports = report_repo.list_server_only(cycle_id=active_cycle, report_type=PREPARATORY_ANAMNESIS_REPORT_TYPE)

    assert len(raw_reports) == 1
    assert len(m1_reports) == 1
    assert raw_reports[0].version == 1
    assert m1_reports[0].version == 1
    assert raw_reports[0].content["answers"]["weight"] == 62


@pytest.mark.anyio
async def test_concurrent_conflicting_intake_across_independent_sessions_rejects_collision_without_false_success(
    db_session, active_cycle
):
    """
    Test 12: Two independent worker processes concurrently submit DIFFERENT answers for the same cycle.
    Verifies:
    - One worker succeeds and commits v1.
    - The colliding worker encounters a concurrency conflict and receives ConsiliumConcurrencyConflictError.
    - ABSENCE OF FALSE SUCCESS: The colliding worker NEVER returns success if the saved data differs from its submission.
    - Database contains NO partial records (both raw and m1 exist for committed version).
    - Database contains NO incompatible or colliding version numbers.
    - Subsequent retry after collision cleanly commits v2.
    """
    engine_bind = db_session.get_bind()
    worker_session_1 = Session(bind=engine_bind, autocommit=False, autoflush=False)
    worker_session_2 = Session(bind=engine_bind, autocommit=False, autoflush=False)

    recorded_calls: List[Dict[str, Any]] = []
    shared_gateway = _build_m1_mock_gateway(recorded_calls, artificial_delay_sec=0.02)

    answers_a = AnamnesisQuestionnaireAnswers.model_validate(_sample_intake_answers(gender="Female", weight=60))
    answers_b = AnamnesisQuestionnaireAnswers.model_validate(_sample_intake_answers(gender="Male", weight=90))

    try:
        worker_1 = PrimaryConsiliumService(
            db=worker_session_1,
            gateway=shared_gateway,
            lock_registry={},  # isolated process memory
        )
        worker_2 = PrimaryConsiliumService(
            db=worker_session_2,
            gateway=shared_gateway,
            lock_registry={},  # isolated process memory
        )

        results = await asyncio.gather(
            worker_1.process_anamnesis_intake(cycle_id=active_cycle, answers=answers_a),
            worker_2.process_anamnesis_intake(cycle_id=active_cycle, answers=answers_b),
            return_exceptions=True,
        )
    finally:
        worker_session_1.close()
        worker_session_2.close()

    successes = [r for r in results if not isinstance(r, Exception)]
    conflicts = [r for r in results if isinstance(r, ConsiliumConcurrencyConflictError)]

    assert len(successes) == 1, f"Expected exactly 1 success, got: {results!r}"
    assert len(conflicts) == 1, f"Expected exactly 1 ConsiliumConcurrencyConflictError, got: {results!r}"
    assert successes[0].intake_version == 1

    # Verify database state
    db_session.expire_all()
    report_repo = ModelReportRepository(db_session)
    raw_reports = report_repo.list_server_only(cycle_id=active_cycle, report_type=RAW_USER_ANAMNESIS_REPORT_TYPE)
    m1_reports = report_repo.list_server_only(cycle_id=active_cycle, report_type=PREPARATORY_ANAMNESIS_REPORT_TYPE)

    # Strictly matched pairs: exactly 1 raw and 1 m1
    assert len(raw_reports) == 1
    assert len(m1_reports) == 1
    assert raw_reports[0].version == 1
    assert m1_reports[0].version == 1

    # Confirm the saved data matches the winning submission (answers_a)
    assert raw_reports[0].content["answers"]["weight"] == 60
    assert raw_reports[0].content["answers"]["gender"] == "Female"

    # Now verify sequential retry: worker 2 retries after collision resolution
    worker_session_retry = Session(bind=engine_bind, autocommit=False, autoflush=False)
    try:
        worker_retry = PrimaryConsiliumService(
            db=worker_session_retry,
            gateway=shared_gateway,
            lock_registry={},
        )
        retry_resp = await worker_retry.process_anamnesis_intake(cycle_id=active_cycle, answers=answers_b)
        assert retry_resp.intake_version == 2
    finally:
        worker_session_retry.close()

    # Now DB has strictly v1 (Answers A) and v2 (Answers B), each fully paired
    db_session.expire_all()
    raw_all = report_repo.list_server_only(cycle_id=active_cycle, report_type=RAW_USER_ANAMNESIS_REPORT_TYPE)
    m1_all = report_repo.list_server_only(cycle_id=active_cycle, report_type=PREPARATORY_ANAMNESIS_REPORT_TYPE)
    assert len(raw_all) == 2
    assert len(m1_all) == 2
    assert {r.version for r in raw_all} == {1, 2}
    assert {r.version for r in m1_all} == {1, 2}


@pytest.mark.anyio
async def test_sqlite_operational_database_locked_rollback_safety(db_session, active_cycle, monkeypatch):
    """
    Test 13: Direct verification of SQLite OperationalError ('database is locked') handling during commit:
    - Transaction failure triggers clean db.rollback().
    - Zero partial records are written to the database.
    - ConsiliumConcurrencyConflictError is raised.
    - No false success message is returned.
    """
    recorded_calls: List[Dict[str, Any]] = []
    gateway = _build_m1_mock_gateway(recorded_calls)
    service = PrimaryConsiliumService(db=db_session, gateway=gateway)

    answers = AnamnesisQuestionnaireAnswers.model_validate(_sample_intake_answers(gender="Female", weight=55))

    # Monkeypatch db.commit to simulate SQLite OperationalError: database is locked
    def mock_locked_commit():
        raise OperationalError("database is locked", {}, Exception("sqlite3.OperationalError: database is locked"))

    monkeypatch.setattr(db_session, "commit", mock_locked_commit)

    with pytest.raises(ConsiliumConcurrencyConflictError, match="database is locked"):
        await service.process_anamnesis_intake(cycle_id=active_cycle, answers=answers)

    # Restore commit and check that database has NO partial records
    monkeypatch.undo()
    db_session.rollback()
    report_repo = ModelReportRepository(db_session)
    raw_all = report_repo.list_server_only(cycle_id=active_cycle, report_type=RAW_USER_ANAMNESIS_REPORT_TYPE)
    m1_all = report_repo.list_server_only(cycle_id=active_cycle, report_type=PREPARATORY_ANAMNESIS_REPORT_TYPE)
    assert len(raw_all) == 0
    assert len(m1_all) == 0
