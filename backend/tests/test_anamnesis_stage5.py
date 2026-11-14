"""
PROJECT AEGIS — ANAMNESIS CARDS V2: STAGE 5 BACKEND TEST SUITE
Covers Cards 15 (Family History & Heredity), 16 (Medications, Vitamins & Supplements),
all 4 primary states, per-relative isolation, 'Other blood relatives' relationship_detail validation,
zero false-positive enforcement, API HTTP intake contract with mocked Gemini M1,
and full backward compatibility with Stages 1-4 payloads.
"""
from typing import Any, Dict, List
import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.main import app
from app.repositories.cycle_repository import MedicalCycleRepository
from app.routers.cascade import get_gemini_gateway
from app.schemas.consilium import AnamnesisQuestionnaireAnswers
from app.services.gemini_gateway import GeminiGateway
from tests.test_primary_consilium import _gemini_envelope, _sample_anamnesis_output


def _base_valid_answers(**kwargs) -> Dict[str, Any]:
    data: Dict[str, Any] = {
        "age_confirmation": "Confirmed",
        "gender": "Female",
        "age": 34,
        "height": 168,
        "weight": 61,
        "nicotine_status": "Non-user",
        "alcohol_status": "Non-drinker",
        "spec_factors": "None",
        "sleep_quality": "Normal",
        "diet_types": "Balanced",
        "diet_habits": "Regular",
        "gi_signals": "None",
        "work_type": "Mostly sitting",
        "chronic_diseases": "No diagnosed chronic conditions",
        "recent_illness": "None of the above",
        "critical_infections": "No diagnosed chronic infections",
        "heredity_risks": "No, as far as I know",
        "medications": "No",
    }
    data.update(kwargs)
    return data


# ============================================================================
# 1. CARD 15: FAMILY HISTORY & HEREDITY TESTS
# ============================================================================

def test_family_history_multiple_relatives_and_diseases_valid():
    ans = AnamnesisQuestionnaireAnswers(**_base_valid_answers(
        heredity_risks="Mother: Heart and vascular conditions, Diabetes mellitus; Father: Stroke; Other blood relatives (Aunt): Oncological / cancer conditions",
        family_history_status="Yes",
        family_history_relatives=["Mother", "Father", "Other blood relatives"],
        family_history_details={
            "Mother": {
                "conditions": ["Heart and vascular conditions", "Diabetes mellitus"],
                "details": "Type 2 diabetes, hypertension",
            },
            "Father": {
                "conditions": ["Stroke"],
            },
            "Other blood relatives": {
                "relationship_detail": "Aunt",
                "conditions": ["Oncological / cancer conditions"],
                "details": "Colon cancer",
            },
        },
    ))
    assert ans.family_history_status == "Yes"
    assert ans.family_history_relatives == ["Mother", "Father", "Other blood relatives"]
    assert ans.family_history_details["Mother"].conditions == ["Heart and vascular conditions", "Diabetes mellitus"]
    assert ans.family_history_details["Father"].conditions == ["Stroke"]
    assert ans.family_history_details["Father"].details is None
    assert ans.family_history_details["Other blood relatives"].relationship_detail == "Aunt"


@pytest.mark.parametrize(
    "status_val",
    [
        "No, as far as I know",
        "I don't know my family history",
        "Prefer not to answer",
    ],
)
def test_family_history_non_yes_states_distinct_and_valid(status_val: str):
    ans = AnamnesisQuestionnaireAnswers(**_base_valid_answers(
        heredity_risks=status_val,
        family_history_status=status_val,
    ))
    assert ans.family_history_status == status_val
    assert ans.family_history_relatives is None
    assert ans.family_history_details is None


def test_family_history_rejects_missing_other_relationship_and_invalid_combos():
    # 'Other blood relatives' without relationship_detail
    with pytest.raises(ValidationError):
        AnamnesisQuestionnaireAnswers(**_base_valid_answers(
            family_history_status="Yes",
            family_history_relatives=["Other blood relatives"],
            family_history_details={
                "Other blood relatives": {
                    "conditions": ["Kidney conditions"],
                },
            },
        ))

    # relationship_detail provided on non-Other relative
    with pytest.raises(ValidationError):
        AnamnesisQuestionnaireAnswers(**_base_valid_answers(
            family_history_status="Yes",
            family_history_relatives=["Mother"],
            family_history_details={
                "Mother": {
                    "relationship_detail": "Biological mother",
                    "conditions": ["Stroke"],
                },
            },
        ))

    # Unapproved extra fields (e.g. age_at_onset) must be rejected by extra='forbid'
    with pytest.raises(ValidationError):
        AnamnesisQuestionnaireAnswers(**_base_valid_answers(
            family_history_status="Yes",
            family_history_relatives=["Mother"],
            family_history_details={
                "Mother": {
                    "conditions": ["Stroke"],
                    "age_at_onset": 52,
                },
            },
        ))

    # False positive: relatives sent with 'No, as far as I know'
    with pytest.raises(ValidationError):
        AnamnesisQuestionnaireAnswers(**_base_valid_answers(
            family_history_status="No, as far as I know",
            family_history_relatives=["Mother"],
            family_history_details={
                "Mother": {"conditions": ["Stroke"]},
            },
        ))

    # Orphaned family_history_relatives without family_history_status
    with pytest.raises(ValidationError):
        AnamnesisQuestionnaireAnswers(**_base_valid_answers(
            family_history_status=None,
            family_history_relatives=["Father"],
        ))


# ============================================================================
# 2. CARD 16: MEDICATIONS, VITAMINS & SUPPLEMENTS TESTS
# ============================================================================

def test_medications_multiple_categories_and_optional_course_valid():
    ans = AnamnesisQuestionnaireAnswers(**_base_valid_answers(
        medications="Prescription medications (Amoxicillin, tablets); Vitamins (Vitamin D, tablets); Supplements and herbal products (Ashwagandha, herbal extract)",
        medications_status="Yes",
        medication_categories=[
            "Prescription medications",
            "Over-the-counter medications",
            "Vitamins",
            "Supplements and herbal products",
        ],
        medication_details={
            "Prescription medications": {
                "names": "Amoxicillin, tablets; physician-prescribed injections",
                "course": "I have been taking a prescribed antibiotic for a week, started November 10",
            },
            "Over-the-counter medications": {
                "names": "Ibuprofen, tablets; nasal spray",
                "course": "I only take a painkiller for headaches",
            },
            "Vitamins": {
                "names": "Vitamin B6, intramuscular injections\nVitamin D, tablets",
            },
            "Supplements and herbal products": {
                "names": "Ashwagandha, herbal extract; other dietary supplements",
                "course": "I take a 1-month course followed by a 2-week break",
            },
        },
    ))
    assert ans.medications_status == "Yes"
    assert len(ans.medication_categories) == 4
    assert "course" not in ans.medication_details["Vitamins"]


@pytest.mark.parametrize("status_val", ["No", "Not sure", "Prefer not to answer"])
def test_medications_non_yes_states_distinct_and_valid(status_val: str):
    ans = AnamnesisQuestionnaireAnswers(**_base_valid_answers(
        medications=status_val,
        medications_status=status_val,
    ))
    assert ans.medications_status == status_val
    assert ans.medication_categories is None
    assert ans.medication_details is None


def test_medications_rejects_empty_names_phantom_and_unselected_categories():
    # Empty medication names
    with pytest.raises(ValidationError):
        AnamnesisQuestionnaireAnswers(**_base_valid_answers(
            medications_status="Yes",
            medication_categories=["Vitamins"],
            medication_details={
                "Vitamins": {"names": "   ", "course": "Every morning"},
            },
        ))

    # Unchecked category leaked into medication_details must be rejected
    with pytest.raises(ValidationError):
        AnamnesisQuestionnaireAnswers(**_base_valid_answers(
            medications_status="Yes",
            medication_categories=["Vitamins"],
            medication_details={
                "Vitamins": {"names": "Vitamin D, tablets"},
                "Prescription medications": {"names": "Amoxicillin, tablets"},
            },
        ))

    # False positive: medication_details present when status is 'No'
    with pytest.raises(ValidationError):
        AnamnesisQuestionnaireAnswers(**_base_valid_answers(
            medications_status="No",
            medication_categories=["Vitamins"],
            medication_details={"Vitamins": {"names": "Vitamin D"}},
        ))

    # Unknown detail keys rejected (no forced dosage/mg schema)
    with pytest.raises(ValidationError):
        AnamnesisQuestionnaireAnswers(**_base_valid_answers(
            medications_status="Yes",
            medication_categories=["Vitamins"],
            medication_details={
                "Vitamins": {"names": "Vitamin D", "dosage_mg": "5000"},
            },
        ))


# ============================================================================
# 3. BACKWARD COMPATIBILITY & MOCKED API ENDPOINT INTEGRATION
# ============================================================================

def test_backward_compatibility_cards_1_to_14_without_stage5_fields():
    ans = AnamnesisQuestionnaireAnswers(**_base_valid_answers(
        heredity_risks="her_heart, her_diab",
        medications="med_vit",
        user_text_heredity_risks="Father had hypertension",
        user_text_medications="Vitamin D3",
    ))
    assert ans.heredity_risks == "her_heart, her_diab"
    assert ans.medications == "med_vit"
    assert ans.family_history_status is None
    assert ans.medications_status is None


def test_stage5_cascade_intake_endpoint_mocked_no_real_gemini(
    client: TestClient,
    db_session,
    auth_headers,
):
    cycle_id = "cycle_stage5_intake_01"
    MedicalCycleRepository(db_session).create(cycle_id=cycle_id, status="active")

    async def _no_sleep(_: float) -> None:
        pass

    recorded: List[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        recorded.append(request.url.path)
        return httpx.Response(200, json=_gemini_envelope(_sample_anamnesis_output()))

    mock_gateway = GeminiGateway(
        api_key="test-key",
        transport=httpx.MockTransport(handler),
        sleep_func=_no_sleep,
    )
    app.dependency_overrides[get_gemini_gateway] = lambda: mock_gateway
    try:
        payload = _base_valid_answers(
            heredity_risks="Mother: Diabetes mellitus",
            family_history_status="Yes",
            family_history_relatives=["Mother"],
            family_history_details={
                "Mother": {
                    "conditions": ["Diabetes mellitus"],
                    "details": "Type 2 diabetes",
                },
            },
            medications="Over-the-counter medications (Ibuprofen, tablets)",
            medications_status="Yes",
            medication_categories=["Over-the-counter medications"],
            medication_details={
                "Over-the-counter medications": {
                    "names": "Ibuprofen, tablets",
                    "course": "I only take a painkiller for headaches",
                },
            },
        )
        resp = client.post(
            "/api/v1/cascade/intake/anamnesis",
            headers=auth_headers,
            json={"cycle_id": cycle_id, "answers": payload},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["cycle_id"] == cycle_id
        assert body["status"] == "processed"
        assert len(recorded) == 1
    finally:
        app.dependency_overrides.clear()
