"""
PROJECT AEGIS — ANAMNESIS CARDS V2: STAGE 4 BACKEND TEST SUITE
Covers Cards 12 (Chronic Conditions & Medical History), 13 (Recent Illnesses & Health Events),
14 (Infectious Disease History), cross-field consistency rules, onco_status preservation,
non-overlapping recent timeframes, and backward compatibility.
"""
import pytest
from pydantic import ValidationError

from app.schemas.consilium import AnamnesisQuestionnaireAnswers


def _base_valid_answers(**kwargs):
    data = {
        "age_confirmation": "Confirmed",
        "gender": "Female",
        "age": 35,
        "height": 168,
        "weight": 62,
        "nicotine_status": "Non-user",
        "alcohol_status": "Non-drinker",
        "spec_factors": "None",
        "sleep_quality": "Normal",
        "diet_types": "Balanced",
        "diet_habits": "Regular",
        "gi_signals": "None",
        "work_type": "Mostly sitting",
        "chronic_diseases": "None",
        "recent_illness": "Healthy",
        "critical_infections": "No diagnosed chronic infections",
        "heredity_risks": "None",
        "medications": "None",
    }
    data.update(kwargs)
    return data


# ============================================================================
# 1. CARD 12: CHRONIC CONDITIONS & MEDICAL HISTORY TESTS
# ============================================================================

def test_chronic_multiple_diagnoses_with_complaints_valid():
    ans = AnamnesisQuestionnaireAnswers(**_base_valid_answers(
        chronic_diseases="Heart or blood pressure conditions (Arterial hypertension), Joint, spine or musculoskeletal conditions (Lumbar disc herniation)",
        chronic_categories=[
            "Heart or blood pressure conditions",
            "Joint, spine or musculoskeletal conditions",
        ],
        chronic_condition_details={
            "Heart or blood pressure conditions": {
                "diagnosis": "Arterial hypertension",
                "diagnosed_when": "In 2021",
                "current_status": "Controlled or stable",
                "currently_bothers": "No, it doesn't currently bother me",
            },
            "Joint, spine or musculoskeletal conditions": {
                "diagnosis": "Lumbar disc herniation",
                "current_status": "Ongoing / Still present",
                "currently_bothers": "Yes, I occasionally experience symptoms",
                "current_symptoms_text": "Lower back pain after sitting for a long time",
            },
        },
        user_text_chronic_diseases="Past appendectomy 10 years ago",
    ))
    assert len(ans.chronic_categories) == 2
    assert ans.chronic_condition_details["Joint, spine or musculoskeletal conditions"]["current_symptoms_text"] == "Lower back pain after sitting for a long time"
    assert ans.user_text_chronic_diseases == "Past appendectomy 10 years ago"


def test_chronic_cancer_preserves_onco_status():
    ans = AnamnesisQuestionnaireAnswers(**_base_valid_answers(
        chronic_diseases="Cancer or oncological history (Breast cancer)",
        onco_status="Remission",
        chronic_categories=["Cancer or oncological history"],
        chronic_condition_details={
            "Cancer or oncological history": {
                "diagnosis": "Breast cancer",
                "diagnosed_when": "2019",
                "onco_status": "Remission",
                "current_status": "In remission (if applicable)",
                "currently_bothers": "No, it doesn't currently bother me",
            }
        },
    ))
    assert ans.onco_status == "Remission"
    assert ans.chronic_condition_details["Cancer or oncological history"]["onco_status"] == "Remission"


def test_chronic_cancer_requires_consistent_onco_status():
    with pytest.raises(ValidationError) as exc:
        AnamnesisQuestionnaireAnswers(**_base_valid_answers(
            chronic_diseases="Cancer or oncological history (Lymphoma)",
            onco_status=None,
            chronic_categories=["Cancer or oncological history"],
            chronic_condition_details={
                "Cancer or oncological history": {
                    "diagnosis": "Lymphoma",
                    "current_status": "Ongoing / Still present",
                    "currently_bothers": "Not sure",
                }
            },
        ))
    assert "onco_status" in str(exc.value)


def test_chronic_negative_and_unsure_mutual_exclusion():
    ans = AnamnesisQuestionnaireAnswers(**_base_valid_answers(
        chronic_diseases="Not sure / Haven't been evaluated",
        chronic_categories=["Not sure / Haven't been evaluated"],
        chronic_condition_details=None,
    ))
    assert ans.chronic_categories == ["Not sure / Haven't been evaluated"]

    with pytest.raises(ValidationError) as exc:
        AnamnesisQuestionnaireAnswers(**_base_valid_answers(
            chronic_categories=["No diagnosed chronic conditions", "Not sure / Haven't been evaluated"],
        ))
    assert "взаимоисключающие" in str(exc.value)

    with pytest.raises(ValidationError) as exc:
        AnamnesisQuestionnaireAnswers(**_base_valid_answers(
            chronic_categories=["No diagnosed chronic conditions", "Diabetes or blood sugar disorders"],
        ))
    assert "взаимоисключающие" in str(exc.value)


def test_chronic_incomplete_or_phantom_complaints_rejected():
    # Missing diagnosis
    with pytest.raises(ValidationError):
        AnamnesisQuestionnaireAnswers(**_base_valid_answers(
            chronic_categories=["Thyroid or hormonal conditions"],
            chronic_condition_details={
                "Thyroid or hormonal conditions": {
                    "diagnosis": "   ",
                    "current_status": "Controlled or stable",
                    "currently_bothers": "No, it doesn't currently bother me",
                }
            },
        ))

    # Phantom current_symptoms_text when not bothered
    with pytest.raises(ValidationError) as exc:
        AnamnesisQuestionnaireAnswers(**_base_valid_answers(
            chronic_categories=["Heart or blood pressure conditions"],
            chronic_condition_details={
                "Heart or blood pressure conditions": {
                    "diagnosis": "Arrhythmia",
                    "current_status": "Controlled or stable",
                    "currently_bothers": "No, it doesn't currently bother me",
                    "current_symptoms_text": "Phantom chest ache",
                }
            },
        ))
    assert "current_symptoms_text" in str(exc.value)

    # Extraneous detail for unselected category
    with pytest.raises(ValidationError) as exc:
        AnamnesisQuestionnaireAnswers(**_base_valid_answers(
            chronic_categories=["Neurological conditions"],
            chronic_condition_details={
                "Neurological conditions": {
                    "diagnosis": "Migraine",
                    "current_status": "Ongoing / Still present",
                    "currently_bothers": "Yes, I occasionally experience symptoms",
                },
                "Asthma or chronic respiratory conditions": {
                    "diagnosis": "Asthma",
                    "current_status": "Controlled or stable",
                    "currently_bothers": "No, it doesn't currently bother me",
                },
            },
        ))
    assert "невыбранной категории" in str(exc.value)


# ============================================================================
# 2. CARD 13: RECENT ILLNESSES & HEALTH EVENTS TESTS
# ============================================================================

def test_recent_events_multiple_valid():
    ans = AnamnesisQuestionnaireAnswers(**_base_valid_answers(
        recent_illness="Cold, flu, COVID-19 or another respiratory viral infection (1–3 months ago, Improving), Injury or significant physical trauma (Within the last month, Fully recovered)",
        recent_events=[
            "Cold, flu, COVID-19 or another respiratory viral infection",
            "Injury or significant physical trauma",
        ],
        recent_event_details={
            "Cold, flu, COVID-19 or another respiratory viral infection": {
                "timeframe": "1–3 months ago",
                "timeframe_code": "31_90_days",
                "recovery_status": "Improving",
                "details": "Fever for 3 days, mild cough lingers",
            },
            "Injury or significant physical trauma": {
                "timeframe": "Within the last month",
                "timeframe_code": "8_30_days",
                "recovery_status": "Fully recovered",
            },
        },
        user_text_recent_illness="Feeling mostly back to normal",
    ))
    assert len(ans.recent_events) == 2
    assert ans.recent_event_details["Injury or significant physical trauma"]["timeframe_code"] == "8_30_days"


def test_recent_events_covid_standalone_rejected():
    with pytest.raises(ValidationError) as exc:
        AnamnesisQuestionnaireAnswers(**_base_valid_answers(
            recent_events=["COVID-19"],
            recent_event_details={
                "COVID-19": {
                    "timeframe": "Within the last 7 days",
                    "timeframe_code": "0_7_days",
                    "recovery_status": "Improving",
                }
            },
        ))
    assert "COVID-19 не должен быть отдельным пунктом" in str(exc.value)


def test_recent_events_none_mutual_exclusion_and_completeness():
    ans = AnamnesisQuestionnaireAnswers(**_base_valid_answers(
        recent_illness="Healthy (None of the above)",
        recent_events=["None of the above"],
        recent_event_details=None,
    ))
    assert ans.recent_events == ["None of the above"]

    with pytest.raises(ValidationError):
        AnamnesisQuestionnaireAnswers(**_base_valid_answers(
            recent_events=["None of the above", "Another infection"],
        ))

    # Invalid overlapping / unknown timeframe_code rejected
    with pytest.raises(ValidationError):
        AnamnesisQuestionnaireAnswers(**_base_valid_answers(
            recent_events=["Surgery or medical procedure"],
            recent_event_details={
                "Surgery or medical procedure": {
                    "timeframe": "Last month",
                    "timeframe_code": "invalid_code",
                    "recovery_status": "Fully recovered",
                }
            },
        ))


# ============================================================================
# 3. CARD 14: INFECTIOUS DISEASE HISTORY TESTS
# ============================================================================

def test_infectious_diagnosed_valid():
    ans = AnamnesisQuestionnaireAnswers(**_base_valid_answers(
        critical_infections="Hepatitis B, Tuberculosis (TBC)",
        infectious_history_status="I have been diagnosed with one or more of these infections",
        infectious_diagnosed=["Hepatitis B", "Tuberculosis"],
        user_text_critical_infections="Hepatitis B managed with annual follow-up",
    ))
    assert ans.infectious_diagnosed == ["Hepatitis B", "Tuberculosis"]


def test_infectious_negative_tests_valid_and_distinct():
    ans = AnamnesisQuestionnaireAnswers(**_base_valid_answers(
        critical_infections="Negative test results reported",
        infectious_history_status="I have had negative test results",
        infectious_tested_negative=["HIV", "Hepatitis C"],
        infectious_last_tested="About 6 months ago",
    ))
    assert ans.infectious_tested_negative == ["HIV", "Hepatitis C"]
    assert ans.infectious_last_tested == "About 6 months ago"


def test_infectious_prefer_not_to_answer_allowed():
    ans = AnamnesisQuestionnaireAnswers(**_base_valid_answers(
        critical_infections="User declined to answer",
        infectious_history_status="Prefer not to answer",
        infectious_diagnosed=None,
        infectious_tested_negative=None,
        infectious_last_tested=None,
    ))
    assert ans.infectious_history_status == "Prefer not to answer"
    assert ans.critical_infections == "User declined to answer"


def test_infectious_contradictory_and_phantom_fields_rejected():
    # Diagnosed branch without infections
    with pytest.raises(ValidationError):
        AnamnesisQuestionnaireAnswers(**_base_valid_answers(
            infectious_history_status="I have been diagnosed with one or more of these infections",
            infectious_diagnosed=[],
        ))

    # Negative branch with diagnosed infections
    with pytest.raises(ValidationError):
        AnamnesisQuestionnaireAnswers(**_base_valid_answers(
            infectious_history_status="I have had negative test results",
            infectious_diagnosed=["HIV"],
        ))

    # Prefer not to answer with subfields
    with pytest.raises(ValidationError):
        AnamnesisQuestionnaireAnswers(**_base_valid_answers(
            infectious_history_status="Prefer not to answer",
            infectious_tested_negative=["Syphilis"],
        ))

    # Orphaned infectious subfields without status
    with pytest.raises(ValidationError):
        AnamnesisQuestionnaireAnswers(**_base_valid_answers(
            infectious_history_status=None,
            infectious_diagnosed=["Hepatitis B"],
        ))


# ============================================================================
# 4. BACKWARD COMPATIBILITY (STAGE 1-3 & LEGACY PAYLOADS)
# ============================================================================

def test_legacy_anamnesis_without_stage4_fields_valid():
    ans = AnamnesisQuestionnaireAnswers(**_base_valid_answers(
        chronic_diseases="chr_onco_trigger, chr_heart",
        onco_status="Active treatment",
        recent_illness="ill_covid",
        critical_infections="No chronic infections (Negative)",
    ))
    assert ans.chronic_diseases == "chr_onco_trigger, chr_heart"
    assert ans.onco_status == "Active treatment"
    assert ans.recent_illness == "ill_covid"
    assert ans.chronic_categories is None
    assert ans.recent_events is None
    assert ans.infectious_history_status is None
