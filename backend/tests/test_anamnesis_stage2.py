"""
PROJECT AEGIS — ANAMNESIS CARDS V2: STAGE 2 BACKEND TEST SUITE
Covers Cards 6 (Sleep & Recovery), 7 (Diet & Eating Habits), 8 (Drinks & Hydration),
cross-field validations, clinical contradiction rules, matryoshka models, and backward compatibility.
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
# 1. CARD 6: SLEEP & RECOVERY TESTS
# ============================================================================

def test_sleep_fields_valid():
    ans = AnamnesisQuestionnaireAnswers(**_base_valid_answers(
        sleep_quality="Great",
        sleep_duration="7–8 hours",
        sleep_symptoms=["Difficulty falling asleep", "Daytime sleepiness / fatigue"],
        sleep_schedule="Regular schedule",
        sleep_schedule_notes="Sleep in cool room",
        user_text_sleep="Sleep in cool room",
    ))
    assert ans.sleep_quality == "Great"
    assert ans.sleep_duration == "7–8 hours"
    assert len(ans.sleep_symptoms) == 2
    assert ans.sleep_schedule == "Regular schedule"
    assert ans.sleep_schedule_notes == "Sleep in cool room"


def test_sleep_symptoms_none_of_above_mutual_exclusion():
    # Only "None of the above" is valid
    ans = AnamnesisQuestionnaireAnswers(**_base_valid_answers(
        sleep_symptoms=["None of the above"],
    ))
    assert ans.sleep_symptoms == ["None of the above"]

    # "None of the above" mixed with another symptom must be rejected
    with pytest.raises(ValidationError) as exc:
        AnamnesisQuestionnaireAnswers(**_base_valid_answers(
            sleep_symptoms=["Difficulty falling asleep", "None of the above"],
        ))
    assert "исключает" in str(exc.value) or "sleep_symptoms" in str(exc.value)


# ============================================================================
# 2. CARD 7: DIET & EATING HABITS TESTS
# ============================================================================

def test_diet_special_requires_details():
    # Special diet with details passes
    ans = AnamnesisQuestionnaireAnswers(**_base_valid_answers(
        diet_types="Special diet / Other",
        diet_custom_details="Strict low-FODMAP diet under supervision",
    ))
    assert ans.diet_custom_details == "Strict low-FODMAP diet under supervision"

    # Special diet without details rejected
    with pytest.raises(ValidationError) as exc:
        AnamnesisQuestionnaireAnswers(**_base_valid_answers(
            diet_types="Special diet / Other",
            diet_custom_details=None,
        ))
    assert "diet_custom_details" in str(exc.value)

    # Empty details rejected
    with pytest.raises(ValidationError) as exc:
        AnamnesisQuestionnaireAnswers(**_base_valid_answers(
            diet_types="Special diet / Other",
            diet_custom_details="   ",
        ))
    assert "diet_custom_details" in str(exc.value)


def test_diet_staples_contradictions():
    # Vegan incompatible with Meat, Fish, Dairy, Eggs
    for staple in ["Meat and poultry", "Fish and seafood", "Dairy products", "Eggs"]:
        with pytest.raises(ValidationError) as exc:
            AnamnesisQuestionnaireAnswers(**_base_valid_answers(
                diet_types="Vegan",
                diet_staples=["Vegetables and greens", staple],
            ))
        assert "несовместим" in str(exc.value) or "животного" in str(exc.value)

    # Vegan with plant staples passes
    ans_vegan = AnamnesisQuestionnaireAnswers(**_base_valid_answers(
        diet_types="Vegan",
        diet_staples=["Vegetables and greens", "Grains and cereals", "Legumes and nuts"],
    ))
    assert len(ans_vegan.diet_staples) == 3

    # Vegetarian incompatible with Meat or Fish
    for staple in ["Meat and poultry", "Fish and seafood"]:
        with pytest.raises(ValidationError) as exc:
            AnamnesisQuestionnaireAnswers(**_base_valid_answers(
                diet_types="Vegetarian",
                diet_staples=["Vegetables and greens", staple],
            ))
        assert "несовместим" in str(exc.value) or "мяс" in str(exc.value) or "рыб" in str(exc.value)

    # Vegetarian with Dairy and Eggs passes
    ans_veg = AnamnesisQuestionnaireAnswers(**_base_valid_answers(
        diet_types="Vegetarian",
        diet_staples=["Vegetables and greens", "Dairy products", "Eggs"],
    ))
    assert len(ans_veg.diet_staples) == 3

    # Pescatarian incompatible with Meat
    with pytest.raises(ValidationError) as exc:
        AnamnesisQuestionnaireAnswers(**_base_valid_answers(
            diet_types="Pescatarian",
            diet_staples=["Fish and seafood", "Meat and poultry"],
        ))
    assert "несовместим" in str(exc.value) or "мяс" in str(exc.value)

    # Pescatarian with Fish passes
    ans_pesc = AnamnesisQuestionnaireAnswers(**_base_valid_answers(
        diet_types="Pescatarian",
        diet_staples=["Fish and seafood", "Vegetables and greens"],
    ))
    assert len(ans_pesc.diet_staples) == 2


def test_diet_occasional_foods_matryoshka():
    # All selected products require an entry in diet_food_frequencies
    ans = AnamnesisQuestionnaireAnswers(**_base_valid_answers(
        diet_occasional_foods=["Sweets and desserts", "Fast food and fried foods"],
        diet_food_frequencies={
            "Sweets and desserts": "1–2 times a week",
            "Fast food and fried foods": "Rarely (< once a month)",
        },
    ))
    assert len(ans.diet_occasional_foods) == 2
    assert ans.diet_food_frequencies["Sweets and desserts"] == "1–2 times a week"

    # Missing frequency for a product must be rejected
    with pytest.raises(ValidationError) as exc:
        AnamnesisQuestionnaireAnswers(**_base_valid_answers(
            diet_occasional_foods=["Sweets and desserts", "Fast food and fried foods"],
            diet_food_frequencies={
                "Sweets and desserts": "1–2 times a week",
            },
        ))
    assert "Fast food and fried foods" in str(exc.value) or "частота" in str(exc.value)

    # "None of the above" mutual exclusion
    with pytest.raises(ValidationError) as exc:
        AnamnesisQuestionnaireAnswers(**_base_valid_answers(
            diet_occasional_foods=["None of the above", "Sweets and desserts"],
            diet_food_frequencies={"Sweets and desserts": "1–2 times a week"},
        ))
    assert "исключает" in str(exc.value)

    # "None of the above" cannot have frequencies
    with pytest.raises(ValidationError) as exc:
        AnamnesisQuestionnaireAnswers(**_base_valid_answers(
            diet_occasional_foods=["None of the above"],
            diet_food_frequencies={"None of the above": "Daily"},
        ))
    assert "не указываются" in str(exc.value)


# ============================================================================
# 3. CARD 8: DRINKS & HYDRATION TESTS
# ============================================================================

def test_drinks_coffee_consistency():
    # Coffee selected without types or frequency rejected
    with pytest.raises(ValidationError) as exc:
        AnamnesisQuestionnaireAnswers(**_base_valid_answers(
            drinks_types=["Coffee and coffee beverages", "Still water"],
            drinks_coffee_types=None,
            drinks_coffee_frequency="2–3 cups a day",
        ))
    assert "drinks_coffee_types" in str(exc.value)

    with pytest.raises(ValidationError) as exc:
        AnamnesisQuestionnaireAnswers(**_base_valid_answers(
            drinks_types=["Coffee and coffee beverages", "Still water"],
            drinks_coffee_types=["Espresso / Black coffee"],
            drinks_coffee_frequency=None,
        ))
    assert "drinks_coffee_frequency" in str(exc.value)

    # Coffee with types and frequency passes
    ans = AnamnesisQuestionnaireAnswers(**_base_valid_answers(
        drinks_total_volume="1.5–2 liters",
        drinks_types=["Coffee and coffee beverages", "Still water"],
        drinks_coffee_types=["Espresso / Black coffee", "Coffee with milk (Cappuccino, Latte)"],
        drinks_coffee_frequency="2–3 cups a day",
    ))
    assert ans.drinks_total_volume == "1.5–2 liters"
    assert len(ans.drinks_coffee_types) == 2

    # Coffee not in drinks_types, but coffee sub-fields present rejected
    with pytest.raises(ValidationError) as exc:
        AnamnesisQuestionnaireAnswers(**_base_valid_answers(
            drinks_types=["Still water", "Black / Green tea"],
            drinks_coffee_types=["Espresso / Black coffee"],
            drinks_coffee_frequency="1 cup a day",
        ))
    assert "не могут быть указаны" in str(exc.value)


def test_drinks_decaf_independence():
    # Decaf coffee is independent and does NOT require drinks_coffee_types
    ans = AnamnesisQuestionnaireAnswers(**_base_valid_answers(
        drinks_total_volume="1.5–2 liters",
        drinks_types=["Decaf coffee", "Still water"],
    ))
    assert "Decaf coffee" in ans.drinks_types
    assert ans.drinks_coffee_types is None


# ============================================================================
# 4. BACKWARD COMPATIBILITY TESTS
# ============================================================================

def test_legacy_answers_without_stage2_fields_validate_cleanly():
    legacy_data = _base_valid_answers(
        sleep_quality="Great",
        diet_types="Balanced, Mediterranean",
        diet_habits="Regular, 3 meals per day",
    )
    ans = AnamnesisQuestionnaireAnswers(**legacy_data)
    assert ans.sleep_quality == "Great"
    assert ans.diet_types == "Balanced, Mediterranean"
    assert ans.diet_habits == "Regular, 3 meals per day"
    assert ans.sleep_duration is None
    assert ans.diet_staples is None
    assert ans.drinks_total_volume is None
