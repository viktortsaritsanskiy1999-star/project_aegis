"""
PROJECT AEGIS — ANAMNESIS CARDS V2: STAGE 3 BACKEND TEST SUITE
Covers Cards 9 (Digestion & GI Health), 10 (Work & Daily Routine), 11 (Physical Activity),
cross-field clinical consistency rules, matryoshka GI refinements, and backward compatibility.
"""
import pytest
from pydantic import ValidationError

from app.schemas.consilium import AnamnesisQuestionnaireAnswers


def _base_valid_answers(**kwargs):
    data = {
        "age_confirmation": "Confirmed",
        "gender": "Male",
        "age": 30,
        "height": 175,
        "weight": 70,
        "nicotine_status": "Non-user",
        "alcohol_status": "Non-drinker",
        "spec_factors": "None",
        "sleep_quality": "Normal",
        "diet_types": "Balanced",
        "diet_habits": "Regular",
        "gi_signals": "None",
        "work_type": "Sedentary",
        "chronic_diseases": "None",
        "recent_illness": "None",
        "critical_infections": "None",
        "heredity_risks": "None",
        "medications": "None",
    }
    data.update(kwargs)
    return data


# ============================================================================
# 1. CARD 9: DIGESTION & GASTROINTESTINAL HEALTH TESTS
# ============================================================================

def test_gi_symptoms_valid_with_details():
    ans = AnamnesisQuestionnaireAnswers(**_base_valid_answers(
        gi_signals="Heartburn or acid reflux, Bloating or abdominal fullness",
        gi_symptoms=["Heartburn or acid reflux", "Bloating or abdominal fullness"],
        gi_symptom_details={
            "Heartburn or acid reflux": {
                "frequency": "Several times a week",
                "duration": "1–6 months",
            },
            "Bloating or abdominal fullness": {
                "frequency": "Occasional (1–2 times a month)",
                "duration": "Less than 1 month",
            },
        },
        user_text_gi_signals="Worse after eating spicy foods",
    ))
    assert ans.gi_symptoms == ["Heartburn or acid reflux", "Bloating or abdominal fullness"]
    assert ans.gi_symptom_details["Heartburn or acid reflux"]["frequency"] == "Several times a week"
    assert ans.user_text_gi_signals == "Worse after eating spicy foods"


def test_gi_symptoms_none_of_above_valid():
    ans = AnamnesisQuestionnaireAnswers(**_base_valid_answers(
        gi_signals="None of the above",
        gi_symptoms=["None of the above"],
        gi_symptom_details=None,
    ))
    assert ans.gi_symptoms == ["None of the above"]
    assert ans.gi_symptom_details is None


def test_gi_symptoms_none_of_above_mutual_exclusion():
    with pytest.raises(ValidationError) as exc:
        AnamnesisQuestionnaireAnswers(**_base_valid_answers(
            gi_symptoms=["Heartburn or acid reflux", "None of the above"],
        ))
    assert "None of the above" in str(exc.value)


def test_gi_symptoms_none_of_above_forbids_details():
    with pytest.raises(ValidationError) as exc:
        AnamnesisQuestionnaireAnswers(**_base_valid_answers(
            gi_symptoms=["None of the above"],
            gi_symptom_details={
                "Heartburn or acid reflux": {
                    "frequency": "Daily",
                    "duration": "More than 6 months",
                }
            },
        ))
    assert "уточнения симптомов ЖКТ не указываются" in str(exc.value)


def test_gi_symptoms_missing_frequency_or_duration_rejected():
    # Missing duration
    with pytest.raises(ValidationError) as exc:
        AnamnesisQuestionnaireAnswers(**_base_valid_answers(
            gi_symptoms=["Stomach or abdominal pain"],
            gi_symptom_details={
                "Stomach or abdominal pain": {
                    "frequency": "Daily",
                }
            },
        ))
    assert "не указана давность (duration)" in str(exc.value)

    # Missing entire symptom in details
    with pytest.raises(ValidationError) as exc:
        AnamnesisQuestionnaireAnswers(**_base_valid_answers(
            gi_symptoms=["Stomach or abdominal pain"],
            gi_symptom_details={},
        ))
    assert "необходимо указать частоту и давность" in str(exc.value)


def test_gi_symptoms_extraneous_details_rejected():
    with pytest.raises(ValidationError) as exc:
        AnamnesisQuestionnaireAnswers(**_base_valid_answers(
            gi_symptoms=["Nausea"],
            gi_symptom_details={
                "Nausea": {
                    "frequency": "Occasional (1–2 times a month)",
                    "duration": "Less than 1 month",
                },
                "Constipation": {
                    "frequency": "Daily",
                    "duration": "More than 6 months",
                },
            },
        ))
    assert "Уточнение указано для невыбранного симптома ЖКТ" in str(exc.value)


# ============================================================================
# 2. CARD 10: WORK & DAILY ROUTINE TESTS
# ============================================================================

def test_work_fields_valid():
    ans = AnamnesisQuestionnaireAnswers(**_base_valid_answers(
        work_type="Mostly sitting",
        work_occupation="Software Engineer",
        work_schedule="Regular daytime schedule",
        work_stress="Moderate",
        user_text_work="Ergonomic desk, stands up every hour",
    ))
    assert ans.work_type == "Mostly sitting"
    assert ans.work_occupation == "Software Engineer"
    assert ans.work_schedule == "Regular daytime schedule"
    assert ans.work_stress == "Moderate"
    assert ans.user_text_work == "Ergonomic desk, stands up every hour"


def test_work_occupation_optional():
    ans = AnamnesisQuestionnaireAnswers(**_base_valid_answers(
        work_type="Standing or walking",
        work_occupation=None,
        work_schedule="Shift work",
        work_stress="High",
    ))
    assert ans.work_occupation is None
    assert ans.work_type == "Standing or walking"
    assert ans.work_stress == "High"


# ============================================================================
# 3. CARD 11: PHYSICAL ACTIVITY TESTS
# ============================================================================

def test_sport_moderate_activity_valid():
    ans = AnamnesisQuestionnaireAnswers(**_base_valid_answers(
        sport_level="Moderate — regular movement or exercise",
        activity_days_per_week="3–4 days",
        activity_duration_minutes="30–60 minutes",
        activity_types=["Running / Jogging", "Gym / Strength training"],
        activity_other_details=None,
        user_text_sport="Focus on cardio and mobility",
    ))
    assert ans.sport_level == "Moderate — regular movement or exercise"
    assert ans.activity_days_per_week == "3–4 days"
    assert ans.activity_duration_minutes == "30–60 minutes"
    assert len(ans.activity_types) == 2
    assert ans.user_text_sport == "Focus on cardio and mobility"


def test_sport_almost_no_activity_forbids_quantitative_parameters():
    with pytest.raises(ValidationError) as exc:
        AnamnesisQuestionnaireAnswers(**_base_valid_answers(
            sport_level="Almost no physical activity",
            activity_days_per_week="1–2 days",
        ))
    assert "количественные параметры тренировок не указываются" in str(exc.value)

    with pytest.raises(ValidationError) as exc:
        AnamnesisQuestionnaireAnswers(**_base_valid_answers(
            sport_level="Almost no physical activity",
            activity_types=["Walking"],
        ))
    assert "количественные параметры тренировок не указываются" in str(exc.value)


def test_sport_almost_no_activity_preserves_user_text():
    # User text is explicitly ALLOWED when Almost no physical activity is selected (e.g. medical reason)
    ans = AnamnesisQuestionnaireAnswers(**_base_valid_answers(
        sport_level="Almost no physical activity",
        activity_days_per_week=None,
        activity_duration_minutes=None,
        activity_types=None,
        activity_other_details=None,
        user_text_sport="Recovering from knee surgery, currently bed rest",
    ))
    assert ans.sport_level == "Almost no physical activity"
    assert ans.activity_days_per_week is None
    assert ans.user_text_sport == "Recovering from knee surgery, currently bed rest"


def test_sport_other_activity_requires_specification():
    # "Other" activity requires details
    with pytest.raises(ValidationError) as exc:
        AnamnesisQuestionnaireAnswers(**_base_valid_answers(
            sport_level="High — intense exercise or training",
            activity_days_per_week="5+ days",
            activity_duration_minutes="Over 60 minutes",
            activity_types=["Other"],
            activity_other_details=None,
        ))
    assert "activity_other_details" in str(exc.value)

    # Valid with details
    ans = AnamnesisQuestionnaireAnswers(**_base_valid_answers(
        sport_level="High — intense exercise or training",
        activity_days_per_week="5+ days",
        activity_duration_minutes="Over 60 minutes",
        activity_types=["Other", "Swimming"],
        activity_other_details="Rock climbing and bouldering",
    ))
    assert ans.activity_other_details == "Rock climbing and bouldering"


# ============================================================================
# 4. BACKWARD COMPATIBILITY
# ============================================================================

def test_legacy_anamnesis_without_stage3_fields_valid():
    ans = AnamnesisQuestionnaireAnswers(**_base_valid_answers(
        gi_signals="Occasional heartburn",
        work_type="Office desk",
        sport_level="Moderate",
    ))
    assert ans.gi_signals == "Occasional heartburn"
    assert ans.work_type == "Office desk"
    assert ans.sport_level == "Moderate"
    assert ans.gi_symptoms is None
    assert ans.gi_symptom_details is None
    assert ans.work_occupation is None
    assert ans.activity_types is None
