import math
import pytest
from pydantic import ValidationError

from app.schemas.consilium import AnamnesisQuestionnaireAnswers
from app.services.consilium_prompts import build_anamnesis_m1_context_from_answers


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
# 1. UNIFIED LIMITS & INPUT SANITIZATION TESTS (AGE, HEIGHT, WEIGHT)
# ============================================================================

def test_age_boundaries_and_sanitization():
    # 18 is minimum valid
    ans_18 = AnamnesisQuestionnaireAnswers(**_base_valid_answers(age=18))
    assert ans_18.age == 18

    # 120 is maximum valid
    ans_120 = AnamnesisQuestionnaireAnswers(**_base_valid_answers(age=120, age_confirmed_unusual=True))
    assert ans_120.age == 120
    assert ans_120.age_confirmed_unusual is True

    # Under 18 rejected
    with pytest.raises(ValidationError) as exc:
        AnamnesisQuestionnaireAnswers(**_base_valid_answers(age=17))
    assert "age" in str(exc.value).lower()

    # Over 120 rejected
    with pytest.raises(ValidationError) as exc:
        AnamnesisQuestionnaireAnswers(**_base_valid_answers(age=121))
    assert "age" in str(exc.value).lower()

    # Comma / dot integer strings converted properly
    ans_str = AnamnesisQuestionnaireAnswers(**_base_valid_answers(age="45"))
    assert ans_str.age == 45
    ans_str_float = AnamnesisQuestionnaireAnswers(**_base_valid_answers(age="45.0"))
    assert ans_str_float.age == 45

    # Non-integer float rejected
    with pytest.raises(ValidationError):
        AnamnesisQuestionnaireAnswers(**_base_valid_answers(age="45,5"))

    # NaN / Inf / letters rejected
    with pytest.raises(ValidationError):
        AnamnesisQuestionnaireAnswers(**_base_valid_answers(age="NaN"))
    with pytest.raises(ValidationError):
        AnamnesisQuestionnaireAnswers(**_base_valid_answers(age=float("nan")))
    with pytest.raises(ValidationError):
        AnamnesisQuestionnaireAnswers(**_base_valid_answers(age="abc"))


def test_height_boundaries_and_sanitization():
    # 100 to 220 usual
    ans_norm = AnamnesisQuestionnaireAnswers(**_base_valid_answers(height=180))
    assert ans_norm.height == 180

    # 50 unusual low
    ans_50 = AnamnesisQuestionnaireAnswers(**_base_valid_answers(height=50, height_confirmed_unusual=True))
    assert ans_50.height == 50

    # 260 unusual high
    ans_260 = AnamnesisQuestionnaireAnswers(**_base_valid_answers(height=260, height_confirmed_unusual=True))
    assert ans_260.height == 260

    # Under 50 rejected
    with pytest.raises(ValidationError):
        AnamnesisQuestionnaireAnswers(**_base_valid_answers(height=49))

    # Over 260 rejected
    with pytest.raises(ValidationError):
        AnamnesisQuestionnaireAnswers(**_base_valid_answers(height=261))

    # Comma decimal string parsed
    ans_comma = AnamnesisQuestionnaireAnswers(**_base_valid_answers(height="172,5"))
    assert ans_comma.height == 172.5

    # Dot decimal normalized
    ans_dot = AnamnesisQuestionnaireAnswers(**_base_valid_answers(height="180.0"))
    assert ans_dot.height == 180

    # NaN / Inf rejected
    with pytest.raises(ValidationError):
        AnamnesisQuestionnaireAnswers(**_base_valid_answers(height="inf"))
    with pytest.raises(ValidationError):
        AnamnesisQuestionnaireAnswers(**_base_valid_answers(height=float("inf")))


def test_weight_boundaries_and_sanitization():
    # 30 to 250 usual
    ans_norm = AnamnesisQuestionnaireAnswers(**_base_valid_answers(weight=75))
    assert ans_norm.weight == 75

    # 20 unusual low
    ans_20 = AnamnesisQuestionnaireAnswers(**_base_valid_answers(weight=20, weight_confirmed_unusual=True))
    assert ans_20.weight == 20

    # 500 unusual high
    ans_500 = AnamnesisQuestionnaireAnswers(**_base_valid_answers(weight=500, weight_confirmed_unusual=True))
    assert ans_500.weight == 500

    # Under 20 rejected
    with pytest.raises(ValidationError):
        AnamnesisQuestionnaireAnswers(**_base_valid_answers(weight=19))

    # Over 500 rejected
    with pytest.raises(ValidationError):
        AnamnesisQuestionnaireAnswers(**_base_valid_answers(weight=501))

    # Comma decimal string parsed
    ans_comma = AnamnesisQuestionnaireAnswers(**_base_valid_answers(weight="68,4"))
    assert ans_comma.weight == 68.4

    # NaN / Inf rejected
    with pytest.raises(ValidationError):
        AnamnesisQuestionnaireAnswers(**_base_valid_answers(weight="NaN"))


# ============================================================================
# 2. CARD 1: PREGNANCY & BREASTFEEDING TESTS
# ============================================================================

def test_pregnancy_trimester_and_weeks_consistency():
    # 1st trimester with week 12 (<= 13w6d) -> valid
    ans = AnamnesisQuestionnaireAnswers(**_base_valid_answers(
        gender="Female",
        pregnancy_status="pregnant",
        pregnancy_trimester="1st trimester",
        pregnancy_weeks=12,
        breastfeeding_status="No",
    ))
    assert ans.pregnancy_weeks == 12

    # 1st trimester with week 15 -> conflict rejected
    with pytest.raises(ValidationError) as exc:
        AnamnesisQuestionnaireAnswers(**_base_valid_answers(
            gender="Female",
            pregnancy_status="pregnant",
            pregnancy_trimester="1st trimester",
            pregnancy_weeks=15,
        ))
    assert "противоречит" in str(exc.value).lower() or "триместр" in str(exc.value).lower()

    # 2nd trimester with week 20 (14..27) -> valid
    ans2 = AnamnesisQuestionnaireAnswers(**_base_valid_answers(
        gender="Female",
        pregnancy_status="pregnant",
        pregnancy_trimester="2nd trimester",
        pregnancy_weeks=20,
    ))
    assert ans2.pregnancy_trimester == "2nd trimester"

    # 2nd trimester with week 8 -> conflict rejected
    with pytest.raises(ValidationError):
        AnamnesisQuestionnaireAnswers(**_base_valid_answers(
            gender="Female",
            pregnancy_status="pregnant",
            pregnancy_trimester="2nd trimester",
            pregnancy_weeks=8,
        ))

    # 3rd trimester with week 32 (>= 28) -> valid
    ans3 = AnamnesisQuestionnaireAnswers(**_base_valid_answers(
        gender="Female",
        pregnancy_status="pregnant",
        pregnancy_trimester="3rd trimester",
        pregnancy_weeks=32,
    ))
    assert ans3.pregnancy_weeks == 32

    # Week > 45 rejected
    with pytest.raises(ValidationError):
        AnamnesisQuestionnaireAnswers(**_base_valid_answers(
            gender="Female",
            pregnancy_status="pregnant",
            pregnancy_trimester="3rd trimester",
            pregnancy_weeks=46,
        ))

    # Independent breastfeeding
    ans_bf = AnamnesisQuestionnaireAnswers(**_base_valid_answers(
        gender="Female",
        pregnancy_status="no",
        breastfeeding_status="Yes",
    ))
    assert ans_bf.breastfeeding_status == "Yes"


# ============================================================================
# 3. CARD 2: WEIGHT DYNAMICS TESTS
# ============================================================================

def test_weight_dynamics_validation():
    # Weight gained with valid positive amount
    ans_gain = AnamnesisQuestionnaireAnswers(**_base_valid_answers(
        weight=80,
        weight_change_status="gained",
        weight_change_amount=4.5,
        weight_change_intentional="Yes",
    ))
    assert ans_gain.weight_change_amount == 4.5

    # Negative amount rejected
    with pytest.raises(ValidationError):
        AnamnesisQuestionnaireAnswers(**_base_valid_answers(
            weight=80,
            weight_change_status="gained",
            weight_change_amount=-3,
            weight_change_intentional="Yes",
        ))

    # Weight loss exceeding current weight rejected
    with pytest.raises(ValidationError) as exc:
        AnamnesisQuestionnaireAnswers(**_base_valid_answers(
            weight=70,
            weight_change_status="lost",
            weight_change_amount=75,
            weight_change_intentional="No",
        ))
    assert "снижения веса" in str(exc.value).lower() or "current weight" in str(exc.value).lower()

    # Stable weight must not have an amount
    with pytest.raises(ValidationError):
        AnamnesisQuestionnaireAnswers(**_base_valid_answers(
            weight_change_status="stable",
            weight_change_amount=5,
        ))


# ============================================================================
# 4. CARD 3: NICOTINE TESTS
# ============================================================================

def test_nicotine_products_tenure_and_mutual_exclusion():
    # Non-user cannot specify products
    with pytest.raises(ValidationError):
        AnamnesisQuestionnaireAnswers(**_base_valid_answers(
            nicotine_status="Non-user",
            nicotine_products=["cigarettes"],
        ))

    # User must specify tenure for each chosen product
    with pytest.raises(ValidationError) as exc:
        AnamnesisQuestionnaireAnswers(**_base_valid_answers(
            nicotine_status="Active",
            nicotine_products=["cigarettes", "vape"],
            nicotine_product_tenure={"cigarettes": "1-3 years"},
        ))
    assert "стаж" in str(exc.value).lower() or "tenure" in str(exc.value).lower()

    # Valid multi-product user
    ans = AnamnesisQuestionnaireAnswers(**_base_valid_answers(
        nicotine_status="Active",
        nicotine_products=["cigarettes", "vape"],
        nicotine_product_tenure={"cigarettes": "3-5 years", "vape": "<1 year"},
        nicotine_current_frequency="Daily",
        nicotine_quantities={"cigarettes_per_day": "10", "vape_nicotine_type": "Salt"},
    ))
    assert len(ans.nicotine_products) == 2
    assert ans.nicotine_product_tenure["cigarettes"] == "3-5 years"

    # Former user cannot specify current frequency
    with pytest.raises(ValidationError):
        AnamnesisQuestionnaireAnswers(**_base_valid_answers(
            nicotine_status="Quit",
            nicotine_products=["cigarettes"],
            nicotine_product_tenure={"cigarettes": "1-3 years"},
            nicotine_current_frequency="Daily",
            nicotine_quit_when="1-5 years ago",
        ))


# ============================================================================
# 5. CARD 4: ALCOHOL TESTS
# ============================================================================

def test_alcohol_mutual_exclusion_and_volumes():
    # Non-drinker cannot specify types
    with pytest.raises(ValidationError):
        AnamnesisQuestionnaireAnswers(**_base_valid_answers(
            alcohol_status="Non-drinker",
            alcohol_types=["beer"],
        ))

    # Valid drinker with types and volumes
    ans = AnamnesisQuestionnaireAnswers(**_base_valid_answers(
        alcohol_status="Frequently",
        alcohol_types=["beer", "wine", "alco_energy"],
        alcohol_volumes={"beer": "1 л", "wine": "300 мл", "alco_energy": "250 мл"},
        alcohol_comment="In moderation on weekends",
    ))
    assert "alco_energy" in ans.alcohol_types
    assert ans.alcohol_volumes["beer"] == "1 л"


# ============================================================================
# 6. CARD 5: PSYCHOACTIVE SUBSTANCES TESTS
# ============================================================================

def test_substances_mutual_exclusion_and_last_use():
    # 'none' cannot combine with substances
    with pytest.raises(ValidationError):
        AnamnesisQuestionnaireAnswers(**_base_valid_answers(
            substance_types=["none", "cannabis"],
        ))

    # 'declined' cannot combine with substances
    with pytest.raises(ValidationError):
        AnamnesisQuestionnaireAnswers(**_base_valid_answers(
            substance_types=["declined", "stimulants"],
        ))

    # Valid substance profile
    ans = AnamnesisQuestionnaireAnswers(**_base_valid_answers(
        substance_types=["cannabis", "stimulants"],
        substance_last_use="<7 days",
        substance_notes="Cannabis for insomnia",
    ))
    assert "cannabis" in ans.substance_types
    assert ans.substance_last_use == "<7 days"


# ============================================================================
# 7. M1 CONTEXT EXTRACTION TESTS
# ============================================================================

def test_m1_context_extraction_full_stage1_flow():
    ans = AnamnesisQuestionnaireAnswers(**_base_valid_answers(
        gender="Female",
        pregnancy_status="pregnant",
        pregnancy_trimester="2nd trimester",
        pregnancy_weeks=18,
        breastfeeding_status="No",
        weight_change_status="gained",
        weight_change_amount=3.5,
        weight_change_intentional="Yes",
        nicotine_status="Active",
        nicotine_products=["cigarettes", "vape"],
        nicotine_product_tenure={"cigarettes": "5-10 years", "vape": "1-3 years"},
        nicotine_current_frequency="daily",
        alcohol_status="Frequently",
        alcohol_types=["beer", "alco_energy"],
        alcohol_volumes={"beer": "0.5 л", "alco_energy": "500 мл"},
        substance_types=["cannabis"],
        substance_last_use="<1 month",
        substance_notes="Mild use",
    ))

    m1_context = build_anamnesis_m1_context_from_answers(ans)
    assert "system_folders" in m1_context
    assert "core_data_log" in m1_context

    sys_folders = m1_context["system_folders"]
    # Pregnancy in endocrine, cvs, kidneys
    assert "pregnancy_status" in sys_folders["endocrine"]
    assert "pregnancy_status" in sys_folders["cvs"]
    assert "pregnancy_status" in sys_folders["kidneys"]

    # Weight change in endocrine & cvs
    assert "weight_dynamics" in sys_folders["endocrine"]
    assert "weight_gain_hemodynamic_strain" in sys_folders["cvs"]

    # Nicotine in respiratory & cvs
    assert "nicotine_products" in sys_folders["respiratory"]
    assert "vessel_nicotine_status" in sys_folders["cvs"]

    # Alco-energy cardiac risk in cvs
    assert "alco_energy_arrhythmic_risk" in sys_folders["cvs"]

    # Cannabis in nervous & cvs
    assert "substance_types" in sys_folders["nervous"]
    assert "cannabis_tachycardia_orthostatic_risk" in sys_folders["cvs"]

    # Core data log
    log = m1_context["core_data_log"]
    assert log["pregnancy_status"] == "pregnant"
    assert log["pregnancy_trimester"] == "2nd trimester"
    assert log["pregnancy_weeks"] == 18
    assert log["weight_change_status"] == "gained"
    assert log["nicotine_products"] == ["cigarettes", "vape"]
    assert log["alcohol_types"] == ["beer", "alco_energy"]
    assert log["substance_types"] == ["cannabis"]
