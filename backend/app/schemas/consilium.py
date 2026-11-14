from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import math
import re
from enum import Enum
from typing import Any, Dict, List, Literal, Optional, Tuple, Union
from pydantic import BaseModel, Field, ConfigDict, field_validator, model_validator
from app.schemas.reports import ClientModelReportResponse

SYSTEM_KEYS: tuple[str, ...] = (
    "cvs",
    "gi_tract",
    "respiratory",
    "nervous",
    "musculoskeletal",
    "kidneys",
    "immune",
    "endocrine",
)

SYSTEM_TITLES: Dict[str, str] = {
    "cvs": "Cardiovascular",
    "gi_tract": "GI Tract",
    "respiratory": "Respiratory",
    "nervous": "Nervous",
    "musculoskeletal": "Musculoskeletal",
    "kidneys": "Renal",
    "immune": "Immune and Blood",
    "endocrine": "Endocrine",
}


class ConsiliumMode(str, Enum):
    """
    Execution mode for the Primary Consilium orchestrator.
    - INITIAL: Baseline evaluation for a brand-new patient with no prior clinical history in Aegis.
    - NEO_REASSESSMENT: Comprehensive re-evaluation of a returning patient with historical context
      from completed prior cycles, evaluating longitudinal delta while maintaining clean-slate objectivity.
    """
    INITIAL = "initial"
    NEO_REASSESSMENT = "neo_reassessment"


class AegisAgentRole(str, Enum):
    """
    Distinct logical AI agent roles in Project Aegis.
    Separates the logical role of an agent from the physical Gemini model name
    (since multiple independent modules share 'gemini-3.8-flash').
    """
    # Preparatory modules (Gemini Flash)
    ANAMNESIS_M1 = "anamnesis_m1"
    VISION_OCR = "vision_ocr"
    VISION_ECG = "vision_ecg"

    # 8 Primary Organ Specialists (Gemini Flash-Lite)
    SYSTEM_CVS = "system_specialist_cvs"
    SYSTEM_GI_TRACT = "system_specialist_gi_tract"
    SYSTEM_RESPIRATORY = "system_specialist_respiratory"
    SYSTEM_NERVOUS = "system_specialist_nervous"
    SYSTEM_MUSCULOSKELETAL = "system_specialist_musculoskeletal"
    SYSTEM_KIDNEYS = "system_specialist_kidneys"
    SYSTEM_IMMUNE = "system_specialist_immune"
    SYSTEM_ENDOCRINE = "system_specialist_endocrine"

    # Master Consilium Board
    DEPUTY_CHIEF_PHYSICIAN = "deputy_chief_physician"  # Gemini Flash
    CHIEF_PHYSICIAN = "chief_physician"                # Gemini Pro

    # Standalone downstream modules (Gemini Flash - future stages)
    ATHENA_CURATOR = "athena_curator"
    WORKOUT_INTELLIGENCE = "workout_intelligence"

    @classmethod
    def for_system_key(cls, system_key: str) -> "AegisAgentRole":
        mapping = {
            "cvs": cls.SYSTEM_CVS,
            "gi_tract": cls.SYSTEM_GI_TRACT,
            "respiratory": cls.SYSTEM_RESPIRATORY,
            "nervous": cls.SYSTEM_NERVOUS,
            "musculoskeletal": cls.SYSTEM_MUSCULOSKELETAL,
            "kidneys": cls.SYSTEM_KIDNEYS,
            "immune": cls.SYSTEM_IMMUNE,
            "endocrine": cls.SYSTEM_ENDOCRINE,
        }
        if system_key not in mapping:
            raise ValueError(f"Unknown system_key '{system_key}' for AegisAgentRole.")
        return mapping[system_key]


# =====================================================================
# 1. ANAMNESIS (M1) SCHEMAS (BLIND MODE - PREPARATORY STAGE, SERVER ONLY)
# =====================================================================

class AnamnesisUserView(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    summary: str = Field(..., min_length=1)


class AnamnesisOrganFact(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    data: str = Field(..., min_length=1)
    internal_comment: str = Field(..., min_length=1)


class AnamnesisSystemLogicMap(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cvs: AnamnesisOrganFact
    gi_tract: AnamnesisOrganFact
    kidneys: AnamnesisOrganFact
    endocrine: AnamnesisOrganFact
    immune: AnamnesisOrganFact
    nervous: AnamnesisOrganFact
    respiratory: AnamnesisOrganFact
    musculoskeletal: AnamnesisOrganFact


class AnamnesisTrainingSandbox(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    raw_diagnostic_hypothesis: str = Field(..., min_length=1)
    data_gap_analysis: str = Field(..., min_length=1)
    error_probability: str = Field(..., min_length=1)


class AnamnesisModelOutput(BaseModel):
    """Structured output contract for preparatory M1 Anamnesis module (anamnesis_m1)."""
    model_config = ConfigDict(extra="forbid")

    user_view: AnamnesisUserView
    system_logic: AnamnesisSystemLogicMap
    training_sandbox: AnamnesisTrainingSandbox


class FamilyHistoryRelativeDetail(BaseModel):
    """Structured Stage 5 family history details for one blood relative."""
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    conditions: List[str] = Field(..., min_length=1)
    relationship_detail: Optional[str] = Field(default=None, max_length=256)
    details: Optional[str] = Field(default=None, max_length=1024)


class AnamnesisQuestionnaireAnswers(BaseModel):
    """
    Strict server-side validation schema for raw answers from the 17 Flutter anamnesis cards.
    Rejects client injection of AI hypotheses, internal comments, organ shelves, or laboratory data.
    """
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    # Card 0: Age Gate
    age_confirmation: str = Field(..., max_length=64)

    # Card 1: Biological Profile
    gender: str = Field(..., max_length=32)
    age: int = Field(..., ge=18, le=120)
    age_confirmed_unusual: Optional[bool] = Field(default=None)
    pregnancy_status: Optional[str] = Field(default=None, max_length=64)
    pregnancy_trimester: Optional[str] = Field(default=None, max_length=128)
    pregnancy_weeks: Optional[Union[int, float]] = Field(default=None, ge=1, le=45)
    breastfeeding_status: Optional[str] = Field(default=None, max_length=64)

    # Card 2: Dimensions
    height: Union[int, float] = Field(..., ge=50, le=260)
    weight: Union[int, float] = Field(..., ge=20, le=500)
    height_confirmed_unusual: Optional[bool] = Field(default=None)
    weight_confirmed_unusual: Optional[bool] = Field(default=None)
    weight_change_status: Optional[str] = Field(default=None, max_length=64)
    weight_change_amount: Optional[Union[int, float]] = Field(default=None, ge=0)
    weight_change_intentional: Optional[str] = Field(default=None, max_length=64)

    # Card 3: Nicotine
    nicotine_status: str = Field(..., min_length=1, max_length=128)
    nicotine_years: Optional[str] = Field(default=None, max_length=128)
    nicotine_freq: Optional[str] = Field(default=None, max_length=128)
    nicotine_products: Optional[List[str]] = Field(default=None)
    nicotine_product_tenure: Optional[Dict[str, str]] = Field(default=None)
    nicotine_current_frequency: Optional[str] = Field(default=None, max_length=128)
    nicotine_quantities: Optional[Dict[str, Any]] = Field(default=None)
    nicotine_quit_when: Optional[str] = Field(default=None, max_length=128)

    # Card 4: Alcohol
    alcohol_status: str = Field(..., min_length=1, max_length=128)
    alcohol_type: Optional[str] = Field(default=None, max_length=128)
    alcohol_types: Optional[List[str]] = Field(default=None)
    alcohol_volumes: Optional[Dict[str, str]] = Field(default=None)
    alcohol_comment: Optional[str] = Field(default=None, max_length=1024)
    user_text_alcohol: Optional[str] = Field(default=None, max_length=1024)

    # Card 5: Specific Factors / Дополнительные факторы здоровья
    spec_factors: str = Field(..., min_length=1, max_length=512)
    substance_types: Optional[List[str]] = Field(default=None)
    substance_last_use: Optional[str] = Field(default=None, max_length=128)
    substance_notes: Optional[str] = Field(default=None, max_length=1024)

    # Card 6: Sleep & Recovery (Stage 2)
    sleep_quality: str = Field(..., min_length=1, max_length=128)
    sleep_duration: Optional[str] = Field(default=None, max_length=128)
    sleep_symptoms: Optional[List[str]] = Field(default=None)
    sleep_schedule: Optional[str] = Field(default=None, max_length=128)
    sleep_schedule_notes: Optional[str] = Field(default=None, max_length=1024)
    user_text_sleep: Optional[str] = Field(default=None, max_length=1024)

    # Card 7: Diet & Eating Habits (Stage 2)
    diet_types: str = Field(..., min_length=1, max_length=512)
    diet_custom_details: Optional[str] = Field(default=None, max_length=512)
    diet_meals_per_day: Optional[str] = Field(default=None, max_length=128)
    diet_snacks_per_day: Optional[str] = Field(default=None, max_length=128)
    diet_staples: Optional[List[str]] = Field(default=None)
    diet_occasional_foods: Optional[List[str]] = Field(default=None)
    diet_food_frequencies: Optional[Dict[str, str]] = Field(default=None)
    diet_excluded_foods_text: Optional[str] = Field(default=None, max_length=1024)
    diet_day_description: Optional[str] = Field(default=None, max_length=1024)
    diet_habits: str = Field(..., min_length=1, max_length=512)

    # Card 8: Drinks & Hydration (Stage 2)
    drinks_total_volume: Optional[str] = Field(default=None, max_length=128)
    drinks_types: Optional[List[str]] = Field(default=None)
    drinks_coffee_types: Optional[List[str]] = Field(default=None)
    drinks_coffee_frequency: Optional[str] = Field(default=None, max_length=128)
    drinks_caffeine_frequency: Optional[str] = Field(default=None, max_length=128)
    drinks_day_description: Optional[str] = Field(default=None, max_length=1024)
    user_text_drinks: Optional[str] = Field(default=None, max_length=1024)

    # Card 9: GI Signals / Digestion & Gastrointestinal Health (Stage 3)
    gi_signals: str = Field(..., min_length=1, max_length=512)
    gi_symptoms: Optional[List[str]] = Field(default=None)
    gi_symptom_details: Optional[Dict[str, Dict[str, str]]] = Field(default=None)

    # Card 10: Work & Daily Routine (Stage 3)
    work_type: str = Field(..., min_length=1, max_length=128)
    work_occupation: Optional[str] = Field(default=None, max_length=256)
    work_schedule: Optional[str] = Field(default=None, max_length=128)
    work_stress: Optional[str] = Field(default=None, max_length=64)
    user_text_work: Optional[str] = Field(default=None, max_length=1024)

    # Card 11: Physical Activity (Stage 3)
    sport_level: Optional[str] = Field(default=None, max_length=128)
    activity_days_per_week: Optional[str] = Field(default=None, max_length=64)
    activity_duration_minutes: Optional[str] = Field(default=None, max_length=64)
    activity_types: Optional[List[str]] = Field(default=None)
    activity_other_details: Optional[str] = Field(default=None, max_length=256)
    user_text_sport: Optional[str] = Field(default=None, max_length=1024)

    # Card 12: Chronic Conditions & Medical History (Stage 4)
    chronic_diseases: str = Field(..., min_length=1, max_length=1024)
    onco_status: Optional[str] = Field(default=None, max_length=128)
    chronic_categories: Optional[List[str]] = Field(default=None)
    chronic_condition_details: Optional[Dict[str, Dict[str, str]]] = Field(default=None)

    # Card 13: Recent Illnesses & Health Events (Stage 4)
    recent_illness: str = Field(..., min_length=1, max_length=512)
    recent_events: Optional[List[str]] = Field(default=None)
    recent_event_details: Optional[Dict[str, Dict[str, str]]] = Field(default=None)

    # Card 14: Infectious Disease History (Stage 4)
    critical_infections: str = Field(..., min_length=1, max_length=512)
    infectious_history_status: Optional[str] = Field(default=None, max_length=128)
    infectious_diagnosed: Optional[List[str]] = Field(default=None)
    infectious_tested_negative: Optional[List[str]] = Field(default=None)
    infectious_last_tested: Optional[str] = Field(default=None, max_length=256)

    # Card 15: Heredity & Family History (Stage 5)
    heredity_risks: str = Field(..., min_length=1, max_length=1024)
    family_history_status: Optional[str] = Field(default=None, max_length=128)
    family_history_relatives: Optional[List[str]] = Field(default=None)
    family_history_details: Optional[Dict[str, FamilyHistoryRelativeDetail]] = Field(default=None)

    # Card 16: Medications, Vitamins & Supplements (Stage 5)
    medications: str = Field(..., min_length=1, max_length=1024)
    medications_status: Optional[str] = Field(default=None, max_length=128)
    medication_categories: Optional[List[str]] = Field(default=None)
    medication_details: Optional[Dict[str, Dict[str, str]]] = Field(default=None)

    # Optional custom user text inputs from Flutter cards
    user_text_medications: Optional[str] = Field(default=None, max_length=1024)
    user_text_heredity_risks: Optional[str] = Field(default=None, max_length=1024)
    user_text_chronic_diseases: Optional[str] = Field(default=None, max_length=1024)
    user_text_recent_illness: Optional[str] = Field(default=None, max_length=1024)
    user_text_critical_infections: Optional[str] = Field(default=None, max_length=1024)
    user_text_spec_factors: Optional[str] = Field(default=None, max_length=1024)
    user_text_diet_habits: Optional[str] = Field(default=None, max_length=1024)
    user_text_gi_signals: Optional[str] = Field(default=None, max_length=1024)

    @field_validator("age_confirmation")
    @classmethod
    def validate_age_confirmation(cls, v: str) -> str:
        s = v.strip().lower()
        if s not in ("confirmed", "true", "1", "yes", "подтверждено"):
            raise ValueError("Age confirmation must be 'Confirmed'.")
        return "Confirmed"

    @field_validator("gender")
    @classmethod
    def validate_gender(cls, v: str) -> str:
        s = v.strip().lower()
        if s in ("male", "m", "мужской", "м"):
            return "Male"
        if s in ("female", "f", "женский", "ж"):
            return "Female"
        raise ValueError("Gender must be 'Male' or 'Female'.")

    @field_validator("age", mode="before")
    @classmethod
    def validate_age(cls, v: Any) -> int:
        if isinstance(v, str):
            v_clean = v.strip()
            try:
                val_f = float(v_clean.replace(",", "."))
                if not val_f.is_integer():
                    raise ValueError(f"Возраст должен быть целым числом: {v}")
                val = int(val_f)
            except ValueError:
                raise ValueError(f"Недопустимое значение возраста: {v}")
        elif isinstance(v, (int, float)):
            if isinstance(v, float) and not v.is_integer():
                raise ValueError(f"Возраст должен быть целым числом: {v}")
            val = int(v)
        else:
            raise ValueError(f"Недопустимый тип значения возраста: {type(v)}")
        if math.isnan(val) or math.isinf(val):
            raise ValueError("Возраст не может быть NaN или Infinity")
        if val < 18 or val > 120:
            raise ValueError(f"Возраст {val} лет выходит за допустимые пределы (18–120 лет)")
        return val

    @field_validator("height", mode="before")
    @classmethod
    def validate_height(cls, v: Any) -> Union[int, float]:
        if isinstance(v, str):
            v_clean = v.strip().replace(",", ".")
            try:
                val = float(v_clean)
            except ValueError:
                raise ValueError(f"Недопустимое значение роста: {v}")
        elif isinstance(v, (int, float)):
            val = float(v)
        else:
            raise ValueError(f"Недопустимый тип значения роста: {type(v)}")
        if math.isnan(val) or math.isinf(val):
            raise ValueError("Значение роста не может быть NaN или Infinity")
        if val < 50.0 or val > 260.0:
            raise ValueError(f"Рост {val} см выходит за допустимые пределы (50–260 см)")
        return int(val) if val.is_integer() else val

    @field_validator("weight", mode="before")
    @classmethod
    def validate_weight(cls, v: Any) -> Union[int, float]:
        if isinstance(v, str):
            v_clean = v.strip().replace(",", ".")
            try:
                val = float(v_clean)
            except ValueError:
                raise ValueError(f"Недопустимое значение веса: {v}")
        elif isinstance(v, (int, float)):
            val = float(v)
        else:
            raise ValueError(f"Недопустимый тип значения веса: {type(v)}")
        if math.isnan(val) or math.isinf(val):
            raise ValueError("Значение веса не может быть NaN или Infinity")
        if val < 20.0 or val > 500.0:
            raise ValueError(f"Вес {val} кг выходит за допустимые пределы (20–500 кг)")
        return int(val) if val.is_integer() else val

    @field_validator("pregnancy_weeks", mode="before")
    @classmethod
    def validate_pregnancy_weeks(cls, v: Any) -> Optional[Union[int, float]]:
        if v is None or v == "":
            return None
        if isinstance(v, str):
            v_clean = v.strip().replace(",", ".")
            try:
                val = float(v_clean)
            except ValueError:
                raise ValueError(f"Недопустимое значение срока беременности: {v}")
        elif isinstance(v, (int, float)):
            val = float(v)
        else:
            raise ValueError(f"Недопустимый тип срока беременности: {type(v)}")
        if math.isnan(val) or math.isinf(val):
            raise ValueError("Срок беременности не может быть NaN или Infinity")
        if val < 1.0 or val > 45.0:
            raise ValueError(f"Срок беременности {val} нед. выходит за физиологический диапазон (1–45 недель)")
        return int(val) if val.is_integer() else val

    @field_validator("weight_change_amount", mode="before")
    @classmethod
    def validate_weight_change_amount(cls, v: Any) -> Optional[Union[int, float]]:
        if v is None or v == "":
            return None
        if isinstance(v, str):
            v_clean = v.strip().replace(",", ".")
            try:
                val = float(v_clean)
            except ValueError:
                raise ValueError(f"Недопустимое значение изменения веса: {v}")
        elif isinstance(v, (int, float)):
            val = float(v)
        else:
            raise ValueError(f"Недопустимый тип изменения веса: {type(v)}")
        if math.isnan(val) or math.isinf(val):
            raise ValueError("Величина изменения веса не может быть NaN или Infinity")
        if val < 0.0:
            raise ValueError("Величина изменения веса не может быть отрицательной (знак минус запрещен)")
        return int(val) if val.is_integer() else val

    @model_validator(mode="after")
    def validate_clinical_consistency(self) -> "AnamnesisQuestionnaireAnswers":
        # 1. Pregnancy consistency
        preg_status = (self.pregnancy_status or "").strip().lower()
        if preg_status in ("да", "yes", "pregnant", "возможно", "possible", "possible_unconfirmed", "possibly, but not confirmed"):
            if self.pregnancy_trimester and self.pregnancy_weeks is not None:
                tri = self.pregnancy_trimester.lower()
                weeks = float(self.pregnancy_weeks)
                if any(x in tri for x in ("первый", "1", "first")):
                    if weeks > 13.9:
                        raise ValueError(
                            f"Срок беременности {weeks} нед. противоречит выбранному первому триместру (до 13 недель 6 дней)."
                        )
                elif any(x in tri for x in ("второй", "2", "second")):
                    if weeks < 14.0 or weeks > 27.9:
                        raise ValueError(
                            f"Срок беременности {weeks} нед. противоречит выбранному второму триместру (14–27 недель 6 дней)."
                        )
                elif any(x in tri for x in ("третий", "3", "third")):
                    if weeks < 28.0:
                        raise ValueError(
                            f"Срок беременности {weeks} нед. противоречит выбранному третьему триместру (с 28-й недели)."
                        )
        elif preg_status in ("нет", "no", "предпочитаю не отвечать", "prefer_not_to_say", "prefer not to say", "prefer not to answer", "declined"):
            if self.pregnancy_trimester is not None or self.pregnancy_weeks is not None:
                raise ValueError(
                    "Срок и триместр беременности не могут быть указаны, если беременность отсутствует или не подтверждена."
                )

        # 2. Weight change consistency
        wc_status = (self.weight_change_status or "").strip().lower()
        if wc_status in ("вес примерно одинаковый", "stable", "не знаю / не отслеживаю", "unknown", ""):
            if self.weight_change_amount is not None or self.weight_change_intentional is not None:
                raise ValueError(
                    "Величина и намеренность изменения веса не могут быть указаны, если вес оставался стабильным или не отслеживался."
                )
        elif wc_status in ("набрал(а) вес", "gained", "потерял(а) вес", "lost"):
            if self.weight_change_amount is not None:
                if self.weight_change_amount <= 0:
                    raise ValueError("Величина изменения веса должна быть строго положительным числом (> 0).")
                if wc_status in ("потерял(а) вес", "lost") and float(self.weight_change_amount) >= float(self.weight):
                    raise ValueError(
                        f"Величина снижения веса ({self.weight_change_amount} кг) не может превышать или равняться текущему весу ({self.weight} кг)."
                    )

        # 3. Nicotine consistency & mutual exclusion
        nic_status = (self.nicotine_status or "").strip().lower()
        if nic_status in ("никогда", "never", "non-user", "не курю", "предпочитаю не отвечать", "prefer_not_to_say", "prefer not to say", "prefer not to answer"):
            if self.nicotine_products or self.nicotine_product_tenure or self.nicotine_current_frequency or self.nicotine_quantities:
                raise ValueError("Выбор 'Никогда' или 'Предпочитаю не отвечать' исключает указание табачных продуктов, стажа и частоты.")
        else:
            if self.nicotine_products:
                tenures = self.nicotine_product_tenure or {}
                for prod in self.nicotine_products:
                    if prod not in tenures or not tenures[prod]:
                        raise ValueError(f"Для продукта '{prod}' обязательно должен быть указан стаж употребления.")
            if nic_status in ("употреблял(а) раньше, сейчас нет", "former", "quit"):
                if self.nicotine_current_frequency is not None:
                    raise ValueError("Для бывшего употребления табака текущая частота не должна указываться.")

        # 4. Alcohol consistency & mutual exclusion
        alc_status = (self.alcohol_status or "").strip().lower()
        if alc_status in ("не употребляю", "none", "non-drinker", "non-user", "не пью", "0", "предпочитаю не отвечать", "prefer_not_to_say", "prefer not to say", "prefer not to answer"):
            if self.alcohol_types or self.alcohol_volumes:
                raise ValueError("Выбор 'Не употребляю' или 'Предпочитаю не отвечать' исключает выбор типов напитков и объемов.")

        # 5. Substances consistency & mutual exclusion
        if self.substance_types:
            has_negative = any(s.strip().lower() in ("не употребляю", "предпочитаю не отвечать", "none", "prefer_not_to_say", "prefer not to say", "prefer not to answer", "declined") for s in self.substance_types)
            if has_negative and len(self.substance_types) > 1:
                raise ValueError("Варианты 'Не употребляю' и 'Предпочитаю не отвечать' являются взаимоисключающими.")
            if has_negative and self.substance_last_use is not None:
                raise ValueError("Для статуса 'Не употребляю' / 'Предпочитаю не отвечать' время последнего употребления не указывается.")

        # 6. Sleep symptoms mutual exclusion
        if self.sleep_symptoms:
            has_none_sleep = any(s.strip().lower() in ("none of the above", "ничего из перечисленного", "none") for s in self.sleep_symptoms)
            if has_none_sleep and len(self.sleep_symptoms) > 1:
                raise ValueError("Вариант 'Ничего из перечисленного' исключает выбор других симптомов сна.")

        # 7. Diet type, staples, and details consistency
        dt_low = (self.diet_types or "").lower()
        if any(w in dt_low for w in ("other", "другое", "special diet")):
            if not self.diet_custom_details or not self.diet_custom_details.strip():
                raise ValueError("Для специального или другого типа питания необходимо указать подробности (diet_custom_details).")

        if self.diet_staples:
            staples_low = [s.lower() for s in self.diet_staples]
            has_meat = any("meat" in s or "мясо" in s or "птица" in s or "poultry" in s for s in staples_low)
            has_fish = any("fish" in s or "рыба" in s or "seafood" in s or "морепродукт" in s for s in staples_low)
            has_dairy = any("dairy" in s or "молоч" in s or "milk" in s for s in staples_low)
            has_eggs = any("egg" in s or "яйц" in s for s in staples_low)

            if "vegan" in dt_low or "веган" in dt_low:
                if has_meat or has_fish or has_dairy or has_eggs:
                    raise ValueError("Веганский тип питания несовместим с продуктами животного происхождения (мясо, рыба, молочные продукты, яйца).")
            elif "vegetarian" in dt_low or "вегетариан" in dt_low:
                if has_meat or has_fish:
                    raise ValueError("Вегетарианский тип питания несовместим с мясом, птицей или рыбой.")
            elif "pescatarian" in dt_low or "пескетариан" in dt_low:
                if has_meat:
                    raise ValueError("Пескетарианский тип питания несовместим с мясом или птицей.")

        # 8. Diet occasional foods and frequencies
        if self.diet_occasional_foods:
            has_none_occ = any(s.strip().lower() in ("none of the above", "ничего из перечисленного", "none") for s in self.diet_occasional_foods)
            if has_none_occ and len(self.diet_occasional_foods) > 1:
                raise ValueError("Вариант 'Ничего из перечисленного' исключает выбор отдельных периодических продуктов.")
            if has_none_occ and self.diet_food_frequencies:
                raise ValueError("Для варианта 'Ничего из перечисленного' частоты употребления продуктов не указываются.")
            if not has_none_occ:
                freqs = self.diet_food_frequencies or {}
                for prod in self.diet_occasional_foods:
                    if prod not in freqs or not freqs[prod]:
                        raise ValueError(f"Для периодического продукта '{prod}' обязательно должна быть указана частота употребления.")

        # 9. Drinks coffee and caffeine consistency
        if self.drinks_types:
            dt_types_low = [d.lower() for d in self.drinks_types]
            has_coffee = any("coffee and coffee beverages" in d or "кофе и кофейные напитки" in d or (d.startswith("coffee") and "decaf" not in d) for d in dt_types_low)
            if has_coffee:
                if not self.drinks_coffee_types:
                    raise ValueError("При выборе кофе необходимо указать хотя бы один вид кофейного напитка (drinks_coffee_types).")
                if not self.drinks_coffee_frequency:
                    raise ValueError("При выборе кофе необходимо указать частоту употребления (drinks_coffee_frequency).")
            else:
                if self.drinks_coffee_types or self.drinks_coffee_frequency:
                    raise ValueError("Виды и частота кофе не могут быть указаны, если кофе не выбран в списке напитков.")

        # 10. GI symptoms and details consistency (Stage 3)
        if self.gi_symptoms:
            has_none_gi = any(s.strip().lower() in ("none of the above", "ничего из перечисленного", "none", "no discomfort") for s in self.gi_symptoms)
            if has_none_gi and len(self.gi_symptoms) > 1:
                raise ValueError("Вариант 'None of the above' исключает выбор других симптомов ЖКТ.")
            if has_none_gi and self.gi_symptom_details:
                raise ValueError("Для варианта 'None of the above' уточнения симптомов ЖКТ не указываются.")
            if not has_none_gi:
                details = self.gi_symptom_details or {}
                for sym in self.gi_symptoms:
                    sym_data = details.get(sym)
                    if not sym_data or not isinstance(sym_data, dict):
                        raise ValueError(f"Для симптома ЖКТ '{sym}' необходимо указать частоту и давность.")
                    freq = sym_data.get("frequency")
                    dur = sym_data.get("duration")
                    if not freq or not str(freq).strip():
                        raise ValueError(f"Для симптома ЖКТ '{sym}' не указана частота (frequency).")
                    if not dur or not str(dur).strip():
                        raise ValueError(f"Для симптома ЖКТ '{sym}' не указана давность (duration).")
                for detail_sym in details:
                    if detail_sym not in self.gi_symptoms:
                        raise ValueError(f"Уточнение указано для невыбранного симптома ЖКТ: '{detail_sym}'.")

        # 11. Physical activity consistency (Stage 3)
        if self.sport_level:
            sl_low = self.sport_level.lower()
            is_almost_none = any(w in sl_low for w in ("almost no", "нет активности", "couch", "none"))
            if is_almost_none:
                if self.activity_days_per_week or self.activity_duration_minutes or self.activity_types or self.activity_other_details:
                    raise ValueError("Для уровня 'Almost no physical activity' количественные параметры тренировок не указываются.")
            else:
                if self.activity_types:
                    has_other = any(t.strip().lower() == "other" or "другое" in t.strip().lower() for t in self.activity_types)
                    if has_other and (not self.activity_other_details or not self.activity_other_details.strip()):
                        raise ValueError("При выборе активности 'Other' необходимо указать её название (activity_other_details).")
                    if not has_other and self.activity_other_details:
                        raise ValueError("Поле activity_other_details не может быть указано, если 'Other' не выбран.")

        # 12. Chronic conditions consistency (Stage 4)
        if self.chronic_categories is not None:
            if len(self.chronic_categories) == 0:
                raise ValueError("Список категорий хронических заболеваний (chronic_categories) не может быть пустым.")
            neg_cats = {"no diagnosed chronic conditions", "not sure / haven't been evaluated"}
            cats_low = [c.strip().lower() for c in self.chronic_categories]
            has_neg = any(c in neg_cats for c in cats_low)
            if has_neg:
                if len(self.chronic_categories) > 1:
                    raise ValueError("Варианты отсутствия диагнозов или неопределённости взаимоисключающие с диагнозами и друг с другом.")
                if self.chronic_condition_details:
                    raise ValueError("При выборе отсутствия хронических заболеваний детали диагнозов не указываются.")
                if self.onco_status:
                    raise ValueError("Онкологический статус не может быть указан при отсутствии хронических заболеваний.")
            else:
                details = self.chronic_condition_details or {}
                allowed_statuses = {
                    "Ongoing / Still present",
                    "Controlled or stable",
                    "In remission (if applicable)",
                    "Previously diagnosed, no current symptoms",
                    "Not sure",
                }
                allowed_bothers = {
                    "No, it doesn't currently bother me",
                    "Yes, I occasionally experience symptoms",
                    "Yes, I frequently experience symptoms",
                    "Not sure",
                }
                no_symptom_bothers = {"No, it doesn't currently bother me", "Not sure"}
                allowed_onco = {"Remission", "Newly diagnosed", "Active treatment", "Relapse"}

                for cat in self.chronic_categories:
                    cat_data = details.get(cat)
                    if not cat_data or not isinstance(cat_data, dict):
                        raise ValueError(f"Для категории '{cat}' необходимо указать детали диагноза (chronic_condition_details).")
                    diag = (cat_data.get("diagnosis") or "").strip()
                    if not diag:
                        raise ValueError(f"Для категории '{cat}' необходимо указать название диагноза (diagnosis).")
                    c_status = (cat_data.get("current_status") or "").strip()
                    if c_status not in allowed_statuses:
                        raise ValueError(f"Для категории '{cat}' указан некорректный текущий статус: '{c_status}'.")
                    bothers = (cat_data.get("currently_bothers") or "").strip()
                    if bothers not in allowed_bothers:
                        raise ValueError(f"Для категории '{cat}' необходимо указать, беспокоит ли состояние сейчас (currently_bothers).")
                    sym_text = (cat_data.get("current_symptoms_text") or "").strip()
                    if bothers in no_symptom_bothers and sym_text:
                        raise ValueError(f"Для категории '{cat}' при отсутствии текущих симптомов поле current_symptoms_text не должно заполняться.")
                    if cat == "Cancer or oncological history":
                        onco_detail = (cat_data.get("onco_status") or "").strip()
                        if onco_detail not in allowed_onco or self.onco_status not in allowed_onco or onco_detail != self.onco_status:
                            raise ValueError("Для категории онкологического анамнеза необходимо указать корректный и согласованный onco_status.")

                for detail_cat in details:
                    if detail_cat not in self.chronic_categories:
                        raise ValueError(f"Указаны детали для невыбранной категории хронических заболеваний: '{detail_cat}'.")

                if "Cancer or oncological history" not in self.chronic_categories and self.onco_status:
                    raise ValueError("Поле onco_status не может быть указано, если категория онкологического анамнеза не выбрана.")

        # 13. Recent illnesses & health events consistency (Stage 4)
        if self.recent_events is not None:
            if len(self.recent_events) == 0:
                raise ValueError("Список недавних событий (recent_events) не может быть пустым.")
            ev_low = [e.strip().lower() for e in self.recent_events]
            if any(e in ("covid-19", "covid", "ill_covid") for e in ev_low):
                raise ValueError("COVID-19 не должен быть отдельным пунктом; он объединяется с респираторными вирусными инфекциями.")
            has_none_ev = any(e in ("none of the above", "none") for e in ev_low)
            if has_none_ev:
                if len(self.recent_events) > 1:
                    raise ValueError("Вариант 'None of the above' взаимоисключает другие недавние события.")
                if self.recent_event_details:
                    raise ValueError("При выборе 'None of the above' детализация недавних событий не указывается.")
            else:
                ev_details = self.recent_event_details or {}
                allowed_tf_codes = {"0_7_days", "8_30_days", "31_90_days"}
                allowed_recovery = {"Fully recovered", "Improving", "Symptoms are still present"}
                for ev in self.recent_events:
                    ev_data = ev_details.get(ev)
                    if not ev_data or not isinstance(ev_data, dict):
                        raise ValueError(f"Для события '{ev}' необходимо указать давность и состояние восстановления.")
                    tf_code = (ev_data.get("timeframe_code") or "").strip()
                    if tf_code not in allowed_tf_codes:
                        raise ValueError(f"Для события '{ev}' указан некорректный непересекающийся интервал (timeframe_code): '{tf_code}'.")
                    rec_status = (ev_data.get("recovery_status") or "").strip()
                    if rec_status not in allowed_recovery:
                        raise ValueError(f"Для события '{ev}' указано некорректное состояние восстановления (recovery_status): '{rec_status}'.")
                for detail_ev in ev_details:
                    if detail_ev not in self.recent_events:
                        raise ValueError(f"Указаны детали для невыбранного недавнего события: '{detail_ev}'.")

        # 14. Infectious disease history consistency (Stage 4)
        allowed_inf_statuses = {
            "I have been diagnosed with one or more of these infections",
            "I have not been diagnosed with any of these infections",
            "I have had negative test results",
            "I don't know / I haven't been tested",
            "Prefer not to answer",
        }
        if self.infectious_history_status is not None:
            if self.infectious_history_status not in allowed_inf_statuses:
                raise ValueError(f"Недопустимый статус инфекционного анамнеза: '{self.infectious_history_status}'.")
            if self.infectious_history_status == "I have been diagnosed with one or more of these infections":
                if not self.infectious_diagnosed or len(self.infectious_diagnosed) == 0:
                    raise ValueError("При указании диагностированных инфекций необходимо выбрать хотя бы одну инфекцию (infectious_diagnosed).")
                if self.infectious_tested_negative or (self.infectious_last_tested and self.infectious_last_tested.strip()):
                    raise ValueError("Сведения об отрицательных тестах не должны указываться одновременно с веткой подтверждённых диагнозов.")
            elif self.infectious_history_status == "I have had negative test results":
                if self.infectious_diagnosed or (self.user_text_critical_infections and self.user_text_critical_infections.strip()):
                    raise ValueError("При выборе ветки отрицательных тестов диагностированные инфекции не указываются.")
            else:
                if (
                    self.infectious_diagnosed
                    or self.infectious_tested_negative
                    or (self.infectious_last_tested and self.infectious_last_tested.strip())
                    or (self.user_text_critical_infections and self.user_text_critical_infections.strip())
                ):
                    raise ValueError(f"Для статуса '{self.infectious_history_status}' детали диагностированных инфекций или тестов не указываются.")
        else:
            if self.infectious_diagnosed or self.infectious_tested_negative or self.infectious_last_tested:
                raise ValueError("Поля инфекционного анамнеза не могут передаваться без указания infectious_history_status.")

        # 15. Family history & heredity consistency (Stage 5)
        allowed_fh_statuses = {
            "Yes",
            "No, as far as I know",
            "I don't know my family history",
            "Prefer not to answer",
        }
        allowed_fh_relatives = {
            "Mother",
            "Father",
            "Brother",
            "Sister",
            "Maternal grandmother",
            "Maternal grandfather",
            "Paternal grandmother",
            "Paternal grandfather",
            "Children",
            "Other blood relatives",
        }
        allowed_fh_conditions = {
            "Heart and vascular conditions",
            "Stroke",
            "Diabetes mellitus",
            "Oncological / cancer conditions",
            "Neurological conditions",
            "Mental health conditions",
            "Genetic / hereditary conditions",
            "Kidney conditions",
            "Blood clotting disorders",
            "Other conditions",
        }
        if self.family_history_status is not None:
            if self.family_history_status not in allowed_fh_statuses:
                raise ValueError(f"Недопустимый статус семейного анамнеза: '{self.family_history_status}'.")
            if self.family_history_status == "Yes":
                if not self.family_history_relatives or len(self.family_history_relatives) == 0:
                    raise ValueError("При выборе 'Yes' в семейном анамнезе необходимо указать хотя бы одного родственника (family_history_relatives).")
                if len(set(self.family_history_relatives)) != len(self.family_history_relatives):
                    raise ValueError("Список родственников (family_history_relatives) не должен содержать дубликатов.")
                for rel in self.family_history_relatives:
                    if rel not in allowed_fh_relatives:
                        raise ValueError(f"Недопустимый родственник в family_history_relatives: '{rel}'.")
                fh_details = self.family_history_details or {}
                for rel in self.family_history_relatives:
                    rel_detail = fh_details.get(rel)
                    if rel_detail is None:
                        raise ValueError(f"Для родственника '{rel}' необходимо указать выбранные заболевания (family_history_details).")
                    if len(rel_detail.conditions) == 0:
                        raise ValueError(f"Для родственника '{rel}' необходимо выбрать хотя бы одно заболевание.")
                    if len(set(rel_detail.conditions)) != len(rel_detail.conditions):
                        raise ValueError(f"Список заболеваний для родственника '{rel}' содержит дубликаты.")
                    for cond in rel_detail.conditions:
                        if cond not in allowed_fh_conditions:
                            raise ValueError(f"Недопустимое заболевание '{cond}' для родственника '{rel}'.")
                    rel_spec = (rel_detail.relationship_detail or "").strip()
                    if rel == "Other blood relatives":
                        if not rel_spec:
                            raise ValueError("Для категории 'Other blood relatives' необходимо уточнить степень родства (relationship_detail).")
                    elif rel_spec:
                        raise ValueError(f"Поле relationship_detail допустимо только для 'Other blood relatives', а не для '{rel}'.")
                for detail_rel in fh_details:
                    if detail_rel not in self.family_history_relatives:
                        raise ValueError(f"Указаны детали для невыбранного родственника: '{detail_rel}'.")
            else:
                if self.family_history_relatives or self.family_history_details:
                    raise ValueError(f"Для статуса семейного анамнеза '{self.family_history_status}' детали родственников не указываются.")
        else:
            if self.family_history_relatives or self.family_history_details:
                raise ValueError("Поля семейного анамнеза не могут передаваться без указания family_history_status.")

        # 16. Medications, vitamins & supplements consistency (Stage 5)
        allowed_med_statuses = {"Yes", "No", "Not sure", "Prefer not to answer"}
        allowed_med_categories = {
            "Prescription medications",
            "Over-the-counter medications",
            "Vitamins",
            "Supplements and herbal products",
        }
        allowed_med_detail_keys = {"names", "course"}
        if self.medications_status is not None:
            if self.medications_status not in allowed_med_statuses:
                raise ValueError(f"Недопустимый статус приёма препаратов: '{self.medications_status}'.")
            if self.medications_status == "Yes":
                if not self.medication_categories or len(self.medication_categories) == 0:
                    raise ValueError("При выборе 'Yes' для лекарств необходимо выбрать хотя бы одну категорию (medication_categories).")
                if len(set(self.medication_categories)) != len(self.medication_categories):
                    raise ValueError("Список категорий препаратов (medication_categories) не должен содержать дубликатов.")
                for cat in self.medication_categories:
                    if cat not in allowed_med_categories:
                        raise ValueError(f"Недопустимая категория препаратов: '{cat}'.")
                med_details = self.medication_details or {}
                for cat in self.medication_categories:
                    cat_data = med_details.get(cat)
                    if not cat_data or not isinstance(cat_data, dict):
                        raise ValueError(f"Для категории '{cat}' необходимо указать сведения о препаратах (medication_details).")
                    unknown_keys = set(cat_data.keys()) - allowed_med_detail_keys
                    if unknown_keys:
                        raise ValueError(f"Для категории '{cat}' переданы недопустимые поля: {sorted(unknown_keys)}.")
                    names = (cat_data.get("names") or "").strip()
                    if not names:
                        raise ValueError(f"Для категории '{cat}' необходимо указать хотя бы одно название препарата или добавки (names).")
                for detail_cat in med_details:
                    if detail_cat not in self.medication_categories:
                        raise ValueError(f"Указаны детали для невыбранной категории препаратов: '{detail_cat}'.")
            else:
                if self.medication_categories or self.medication_details:
                    raise ValueError(f"Для статуса приёма препаратов '{self.medications_status}' категории и детали не указываются.")
        else:
            if self.medication_categories or self.medication_details:
                raise ValueError("Поля приёма препаратов не могут передаваться без указания medications_status.")

        return self


class AnamnesisIntakeRequest(BaseModel):
    """Client request payload to submit raw questionnaire answers for server-side Anamnesis M1 processing."""
    model_config = ConfigDict(extra="forbid")

    cycle_id: str = Field(..., min_length=3, max_length=64)
    answers: AnamnesisQuestionnaireAnswers


class AnamnesisIntakeClientResponse(BaseModel):
    """
    Client-facing response returned after server-side Anamnesis M1 intake.
    Returns only service-level confirmation metadata.
    Zero leakage of preparatory AI conclusions, user_view.summary, system_logic,
    or training_sandbox (all retained securely under server_only privacy level).
    """
    model_config = ConfigDict(extra="forbid")

    cycle_id: str
    status: str = Field(default="processed")
    intake_version: int = Field(..., ge=1)
    created_at_utc: str
    privacy_notice: str = Field(
        default="Anamnesis processed in strict blind mode; raw answers stored securely under server_only privacy level."
    )


# =====================================================================
# 2. 8 ORGAN-SPECIFIC AGENTS (FLASH-LITE) SCHEMAS (SERVER ONLY)
# =====================================================================

class SystemAgentView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    isolated_labs: Dict[str, Any] = Field(default_factory=dict)


class SystemAgentLogic(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    system_general_shelf: str = Field(..., min_length=1)


class SystemAgentSandbox(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    system_training_shelf: str = Field(..., min_length=1)


class SystemAgentOutput(BaseModel):
    """Structured output contract for each of the 8 Flash-Lite organ specialists."""
    model_config = ConfigDict(extra="forbid")

    system_view: SystemAgentView
    system_logic: SystemAgentLogic
    training_sandbox: SystemAgentSandbox


# =====================================================================
# 3. GEMINI FLASH (DEPUTY CHIEF PHYSICIAN) SCHEMAS
# =====================================================================

class UserSystemReportsMap(BaseModel):
    """
    8 separate user-facing conclusions generated by Deputy Chief Physician (Flash)
    for the Human Silhouette. Subject to mandatory verification and correction
    by Chief Physician (Pro) before publication.
    Strictly requires all 8 systems to be present and non-empty after whitespace stripping.
    """
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    cvs: str = Field(..., min_length=1)
    gi_tract: str = Field(..., min_length=1)
    respiratory: str = Field(..., min_length=1)
    nervous: str = Field(..., min_length=1)
    musculoskeletal: str = Field(..., min_length=1)
    kidneys: str = Field(..., min_length=1)
    immune: str = Field(..., min_length=1)
    endocrine: str = Field(..., min_length=1)

    @model_validator(mode="after")
    def verify_all_eight_systems_non_empty(self) -> "UserSystemReportsMap":
        for key in SYSTEM_KEYS:
            val = getattr(self, key, None)
            if not isinstance(val, str) or not val.strip():
                raise ValueError(
                    f"Incomplete user_system_reports: system '{key}' must be a non-empty string."
                )
        return self

    def to_dict(self) -> Dict[str, str]:
        return {key: getattr(self, key) for key in SYSTEM_KEYS}


class FlashMasterOutput(BaseModel):
    """
    Structured output contract for Deputy Chief Physician (deputy_chief_physician):
    - flash_general_shelf (server_only internal synthesis)
    - flash_training_shelf (server_only clinical hypotheses, deficits, audit of M1 + 8 models)
    - user_system_reports (8 draft user-facing organ reports to be verified by Pro)
    """
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    flash_general_shelf: str = Field(..., min_length=1)
    flash_training_shelf: str = Field(..., min_length=1)
    user_system_reports: UserSystemReportsMap


# =====================================================================
# 4. GEMINI PRO (CHIEF PHYSICIAN) SCHEMAS
# =====================================================================

class ProVitalsThresholds(BaseModel):
    """
    Full vitals threshold output from Chief Physician Pro.
    Note: clinical_rationale is server_only and never exposed to client_facing schemas.
    """
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    min_pessimum_bpm: int = Field(..., ge=25, le=200)
    max_pessimum_bpm: int = Field(..., ge=40, le=260)
    max_spike_delta_bpm: int = Field(..., ge=5, le=150)
    clinical_rationale: str = Field(..., min_length=1)

    @model_validator(mode="after")
    def validate_bpm_bounds(self) -> "ProVitalsThresholds":
        if self.min_pessimum_bpm >= self.max_pessimum_bpm:
            raise ValueError("min_pessimum_bpm must be strictly less than max_pessimum_bpm.")
        return self


class ProUserOverallReport(BaseModel):
    """
    Separate user-facing overall conclusion generated by Chief Physician (Pro) for the Client Vault.
    Strictly separated from Athena instructions (pro_general_shelf) and internal analytics.
    """
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    title: str = Field(default="Aegis Wellness Roadmap", min_length=1, max_length=200)
    summary: str = Field(..., min_length=1)
    highlights: List[str] = Field(default_factory=list)


class ProFlashQualityReview(BaseModel):
    """
    Chief Physician Pro quality control gate over Deputy Flash's 8 user_system_reports.
    - If approved_for_publication is False, publication of client_facing reports is aborted.
    - Any entries in corrected_user_system_reports deterministically override Flash's drafts
      before client_facing reports are saved.
    """
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    approved_for_publication: bool = Field(default=True)
    rejection_reason: Optional[str] = Field(default=None)
    corrected_user_system_reports: Dict[str, str] = Field(default_factory=dict)

    @field_validator("corrected_user_system_reports", mode="before")
    @classmethod
    def ensure_dict_and_non_empty_strings(cls, v: Any) -> Dict[str, str]:
        if not isinstance(v, dict):
            raise ValueError("corrected_user_system_reports must be a dictionary.")
        cleaned: Dict[str, str] = {}
        for k, text in v.items():
            if k not in SYSTEM_KEYS:
                raise ValueError(
                    f"Invalid system key '{k}' in corrected_user_system_reports. "
                    f"Allowed: {SYSTEM_KEYS}"
                )
            if not isinstance(text, str) or not text.strip():
                raise ValueError(f"Corrected report text for '{k}' must be a non-empty string.")
            cleaned[k] = text.strip()
        return cleaned


class ProMasterOutput(BaseModel):
    """
    Structured output contract for Chief Physician (chief_physician):
    - vitals_thresholds (numeric thresholds + server_only clinical_rationale)
    - pro_general_shelf (server_only clinical strategy and instructions for Athena)
    - pro_training_shelf (server_only supreme clinical synthesis & cascade audit)
    - user_overall_report (client_facing overall wellness report for Vault)
    - flash_quality_review (quality control & deterministic corrections for Flash's 8 user reports)
    """
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    vitals_thresholds: ProVitalsThresholds
    pro_general_shelf: str = Field(..., min_length=1)
    pro_training_shelf: str = Field(..., min_length=1)
    user_overall_report: ProUserOverallReport
    flash_quality_review: ProFlashQualityReview = Field(default_factory=ProFlashQualityReview)


# =====================================================================
# 4B. NEO CONSILIUM (REASSESSMENT) SCHEMAS
# =====================================================================

class NeoSystemAgentOutput(SystemAgentOutput):
    """
    Structured output contract for each of the 8 Flash-Lite organ specialists in Neo mode.
    Inherits 100% schema parity with SystemAgentOutput (system_view, system_logic, training_sandbox).
    """
    pass


class NeoFlashMasterOutput(FlashMasterOutput):
    """
    Structured output contract for Deputy Chief Physician (Gemini Flash) in Neo mode.
    Inherits 100% schema parity with FlashMasterOutput (flash_general_shelf, flash_training_shelf, user_system_reports).
    """
    pass


class NeoProMasterOutput(ProMasterOutput):
    """
    Structured output contract for Chief Physician (Gemini Pro) in Neo mode.
    Inherits 100% schema parity with ProMasterOutput (vitals_thresholds, pro_general_shelf, pro_training_shelf, user_overall_report, flash_quality_review).
    """
    pass


class NeoConsiliumMetadata(BaseModel):
    """
    Metadata capturing the Neo Consilium execution lineage and historical context boundaries.
    Stored under SERVER_ONLY privacy level in neo_consilium_execution_log.
    """
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    mode: ConsiliumMode = Field(default=ConsiliumMode.NEO_REASSESSMENT)
    new_cycle_id: str = Field(..., min_length=3, max_length=64)
    previous_cycle_id: Optional[str] = Field(default=None, max_length=64)
    prior_cycles_count: int = Field(default=0, ge=0)
    prior_cycle_ids: List[str] = Field(default_factory=list)
    historical_character_count: int = Field(default=0, ge=0)
    is_historical_condensed: bool = Field(default=False)
    abnormal_labs_count: int = Field(default=0, ge=0)
    referrals_count: int = Field(default=0, ge=0)


class NeoActivationRequest(BaseModel):
    """
    Client request payload for activating a prepared Neo Consilium cycle.
    `cycle_id` is optional and validated against session ownership with Zero Oracle Leakage.
    """
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    cycle_id: Optional[str] = Field(
        default=None,
        min_length=3,
        max_length=64,
        description="Optional target cycle_id. If omitted, resolved from authenticated patient's pending cycle.",
    )


class NeoAdminActivationRequest(BaseModel):
    """
    Admin request payload to activate a prepared Neo Consilium cycle.
    """
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    cycle_id: str = Field(..., min_length=3, max_length=64)


class NeoActivationResponse(BaseModel):
    """
    Authoritative response schema returned upon successful atomic Neo activation.
    Guarantees idempotency and reflects active cycle state, new strategy version,
    and anchor T0 without exposing internal private model reports.
    """
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    success: bool = True
    cycle_id: str = Field(..., min_length=3, max_length=64)
    status: str = Field(default="active", max_length=32)
    activated_at_utc: str = Field(..., max_length=64)
    strategy_version: int = Field(default=1, ge=1)
    schedule_anchor_t0_utc: str = Field(..., max_length=64)
    next_weekly_consilium_due_utc: str = Field(..., max_length=64)
    post_primary_init_enqueued: bool = True
    is_idempotent_replay: bool = False


# =====================================================================
# 5. API REQUEST / RESPONSE SCHEMAS FOR PRIMARY CONSILIUM
# =====================================================================

class PrimaryConsiliumRunRequest(BaseModel):
    """
    Request to trigger the server-side Primary Consilium (8 Flash-Lite -> Deputy Flash -> Pro)
    for an existing medical cycle.
    Reads all preparatory results (Anamnesis M1, Vision OCR labs, Vision ECG, Vitals)
    exclusively from stored server records for cycle_id without re-running Anamnesis or Vision.
    """
    model_config = ConfigDict(extra="forbid")

    cycle_id: str = Field(..., min_length=3, max_length=64)
    report_date: Optional[str] = Field(
        default=None,
        max_length=32,
        description="Optional ISO date string (YYYY-MM-DD) for user-facing dated entries.",
    )
    mode: Optional[ConsiliumMode] = Field(
        default=None,
        description="Optional consilium mode: 'initial' or 'neo_reassessment'. Auto-detected if omitted.",
    )


class ClientVitalsThresholds(BaseModel):
    """
    Client-safe numerical heart rate thresholds for VitalsMonitorService.
    Excludes server_only clinical_rationale.
    """
    model_config = ConfigDict(extra="forbid")

    min_pessimum_bpm: int
    max_pessimum_bpm: int
    max_spike_delta_bpm: int


class PrimaryConsiliumClientResponse(BaseModel):
    """
    Strict client-facing response returned after successful Primary Consilium completion.
    Contains ONLY verified user reports and numeric vitals thresholds.
    """
    model_config = ConfigDict(extra="forbid")

    cycle_id: str
    report_date: str
    overall_report: ClientModelReportResponse
    system_reports: Dict[str, ClientModelReportResponse]
    vitals_thresholds: ClientVitalsThresholds
    mode: ConsiliumMode = Field(default=ConsiliumMode.INITIAL)


# =====================================================================
# 6. STAGE 3.3 — WEEKLY CONSILIUM (FLASH -> PRO) & ATHENA STRATEGY SCHEMAS
# =====================================================================

_FORBIDDEN_GUARANTEE_SUBSTRINGS: tuple[str, ...] = (
    "guaranteed unchanged",
    "guaranteed not to have changed",
    "health is guaranteed",
    "гарантированно не изменил",
    "гарантированно без изменений",
)


def validate_no_guaranteed_unchanged_claim(text: str, field_label: str = "summary") -> str:
    cleaned = text.strip() if isinstance(text, str) else ""
    if not cleaned:
        raise ValueError(f"{field_label} must be a non-empty string.")
    lower = cleaned.lower()
    for pattern in _FORBIDDEN_GUARANTEE_SUBSTRINGS:
        if pattern in lower:
            raise ValueError(
                f"{field_label} must not claim that health is guaranteed unchanged ('{pattern}')."
            )
    return cleaned


class WeeklySystemUpdateItem(BaseModel):
    """
    Single dated user-facing update for one of the 8 Atlas systems generated during
    the Weekly Consilium.
    - When has_changes is False, summary must provide a friendly message about the absence
      of notable new observations over the 7-day sprint without claiming guaranteed health invariance.
    """
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    summary: str = Field(..., min_length=1)
    has_changes: bool = Field(default=True)

    @field_validator("summary", mode="after")
    @classmethod
    def check_no_false_guarantee(cls, v: str) -> str:
        return validate_no_guaranteed_unchanged_claim(v, "Weekly system summary")


class WeeklyUserSystemReportsMap(BaseModel):
    """
    8 separate dated user-facing system updates generated by Deputy Chief Physician (Flash)
    during the Weekly Consilium. All 8 systems are strictly required.
    """
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    cvs: WeeklySystemUpdateItem
    gi_tract: WeeklySystemUpdateItem
    respiratory: WeeklySystemUpdateItem
    nervous: WeeklySystemUpdateItem
    musculoskeletal: WeeklySystemUpdateItem
    kidneys: WeeklySystemUpdateItem
    immune: WeeklySystemUpdateItem
    endocrine: WeeklySystemUpdateItem

    @model_validator(mode="after")
    def verify_all_eight_systems_present(self) -> "WeeklyUserSystemReportsMap":
        for key in SYSTEM_KEYS:
            item = getattr(self, key, None)
            if not isinstance(item, WeeklySystemUpdateItem) or not item.summary.strip():
                raise ValueError(
                    f"Incomplete weekly user_system_reports: system '{key}' must be present and non-empty."
                )
        return self

    def to_dict(self) -> Dict[str, Dict[str, Any]]:
        return {key: getattr(self, key).model_dump() for key in SYSTEM_KEYS}


class WeeklyFlashMasterOutput(BaseModel):
    """
    Structured output contract for Weekly Deputy Chief Physician (Flash):
    - weekly_deputy_analysis (server_only internal 7-day dynamics, bottlenecks, proposal for Pro)
    - user_system_reports (8 dated user-facing organ updates for Atlas, verified by Pro before publication)
    """
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    weekly_deputy_analysis: str = Field(..., min_length=1)
    user_system_reports: WeeklyUserSystemReportsMap


class ProReferralDecision(BaseModel):
    """
    Structured referral decision produced exclusively by the Chief Physician (Pro)
    during Weekly Consilium when in-person medical evaluation is clinically indicated.
    """
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    recommended: bool = Field(default=False)
    urgency: Literal["none", "routine", "priority"] = Field(default="none")
    target_specialties: List[str] = Field(default_factory=list)
    clinical_reason_for_referral: str = Field(default="")
    observed_syndromes_or_trends: List[str] = Field(default_factory=list)
    recommended_workup_for_doctor_discussion: List[str] = Field(default_factory=list)

    @field_validator("urgency", mode="before")
    @classmethod
    def normalize_urgency(cls, v: Any) -> str:
        if not v:
            return "none"
        norm = str(v).strip().lower()
        if norm in ("none", "routine", "priority"):
            return norm
        if any(w in norm for w in ("prior", "urgent", "high")):
            return "priority"
        if any(w in norm for w in ("rout", "plan", "low", "med")):
            return "routine"
        return "none"

    @model_validator(mode="after")
    def validate_referral_consistency(self) -> "ProReferralDecision":
        if self.recommended:
            if self.urgency == "none":
                object.__setattr__(self, "urgency", "routine")
            if not self.target_specialties:
                object.__setattr__(self, "target_specialties", ["General Practitioner"])
        else:
            if self.urgency != "none":
                object.__setattr__(self, "urgency", "none")
        return self


class WeeklyProMasterOutput(BaseModel):
    """
    Structured output contract for Weekly Chief Physician (Pro):
    - updated_weekly_strategy (server_only practical 7-day strategy & priorities for Athena;
      if no change is required, explicitly confirms continuation of the previous strategy)
    - strategy_status ('updated' or 'continued')
    - weekly_overall_report (client_facing weekly overall summary for the Client Vault)
    - flash_quality_review (quality control & deterministic corrections for Flash's 8 weekly system updates)
    - referral_decision (optional in-person medical referral recommendation)
    """
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    updated_weekly_strategy: str = Field(..., min_length=1)
    strategy_status: str = Field(default="updated", min_length=1)
    weekly_overall_report: ProUserOverallReport
    flash_quality_review: ProFlashQualityReview = Field(default_factory=ProFlashQualityReview)
    referral_decision: Optional[ProReferralDecision] = Field(default=None)

    @field_validator("strategy_status", mode="after")
    @classmethod
    def validate_strategy_status(cls, v: str) -> str:
        normalized = v.strip().lower()
        if normalized not in ("updated", "continued"):
            raise ValueError("strategy_status must be either 'updated' or 'continued'.")
        return normalized


class AthenaClinicalLogEntry(BaseModel):
    """
    Hidden [CLINICAL_REPORT] entry recorded by Athena during a 7-day sprint.
    Stored as server_only and aggregated into Athena's 7-day clinical diary for the Weekly Consilium.
    """
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    status: str = Field(default="NORMAL", min_length=1, max_length=32)
    symptoms_mentioned: List[str] = Field(default_factory=list)
    subjective_wellbeing_score: Optional[int] = Field(default=None, ge=1, le=10)
    adherence_to_plan: str = Field(default="UNKNOWN", min_length=1, max_length=32)
    red_flags: bool = Field(default=False)
    summary_for_master: str = Field(..., min_length=1)
    referral_visit_event: Optional[
        Literal[
            "NONE",
            "COMPLETED_VISIT",
            "FUTURE_OR_BOOKED",
            "NOT_YET_OR_NEGATED",
            "UNCLEAR_NEEDS_CLARIFICATION",
        ]
    ] = Field(default=None)
    referral_visit_context_note: Optional[str] = Field(default=None, max_length=500)
    timestamp: Optional[str] = Field(default=None, max_length=64)
    sprint_day: Optional[int] = Field(default=None, ge=1, le=7)
    event_type: Optional[str] = Field(default=None, max_length=64)

    @field_validator("referral_visit_event", mode="before")
    @classmethod
    def normalize_referral_visit_event(cls, v: Any) -> Optional[str]:
        if v is None:
            return None
        s = str(v).strip().upper()
        if not s:
            return None
        allowed = {
            "NONE",
            "COMPLETED_VISIT",
            "FUTURE_OR_BOOKED",
            "NOT_YET_OR_NEGATED",
            "UNCLEAR_NEEDS_CLARIFICATION",
        }
        if s in allowed:
            return s
        if s in ("FUTURE", "BOOKED", "PLANNED", "FUTURE_INTENT"):
            return "FUTURE_OR_BOOKED"
        if s in ("NOT_YET", "NEGATED", "NEGATION", "NO"):
            return "NOT_YET_OR_NEGATED"
        if s in ("AMBIGUOUS", "UNCLEAR", "CLARIFY"):
            return "UNCLEAR_NEEDS_CLARIFICATION"
        return "UNCLEAR_NEEDS_CLARIFICATION"


class ActiveAthenaStrategyContext(BaseModel):
    """
    Server-side active strategy and context package for Athena (CuratorAgent).
    Linked to cycle_id, sprint_id, and sequential strategy_version so that subsequent
    Athena turns always receive the latest server strategy without losing chat history.
    """
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    cycle_id: str
    sprint_id: Optional[str] = None
    strategy_version: int = Field(..., ge=1)
    source_consilium: str = Field(..., min_length=1)
    strategy_status: str = Field(default="updated", min_length=1)
    active_strategy_text: str = Field(..., min_length=1)
    clinical_background_text: str = Field(..., min_length=1)
    deputy_synthesis_text: str = Field(..., min_length=1)
    athena_logs_summary: str = Field(..., min_length=1)
    daily_question_limit: int = Field(default=3, ge=1, le=5)
    system_prompt: str = Field(..., min_length=1)
    chat_history: List[Dict[str, str]] = Field(default_factory=list)


class AthenaSessionState(BaseModel):
    """
    Server-side session state for Athena that prevents the CuratorAgent.initializeSession()
    stale-instruction bug: whenever applied_strategy_version lags behind the latest
    ActiveAthenaStrategyContext.strategy_version, the session's system_prompt is refreshed
    while preserving 100% of chat_history.
    """
    model_config = ConfigDict(extra="forbid")

    cycle_id: str
    sprint_id: Optional[str] = None
    applied_strategy_version: int = Field(..., ge=1)
    system_prompt: str = Field(..., min_length=1)
    chat_history: List[Dict[str, str]] = Field(default_factory=list)
    was_reinitialized: bool = Field(default=False)


class WeeklyConsiliumRunRequest(BaseModel):
    """
    Request to trigger the server-side Weekly Consilium (Deputy Flash -> Chief Physician Pro)
    for an active medical cycle.
    Reads all context, previous conclusions, active strategy, and 7-day Athena diary
    exclusively from server storage. Never invokes the 8 Flash-Lite agents, Vision, or Anamnesis.
    """
    model_config = ConfigDict(extra="forbid")

    cycle_id: str = Field(..., min_length=3, max_length=64)
    sprint_id: Optional[str] = Field(
        default=None,
        min_length=3,
        max_length=64,
        description="Optional sprint_id to complete/analyze. Defaults to the active sprint for cycle_id.",
    )
    report_date: Optional[str] = Field(
        default=None,
        max_length=32,
        description="Optional ISO date string (YYYY-MM-DD) for the 8 dated Atlas system updates.",
    )


class WeeklyConsiliumClientResponse(BaseModel):
    """
    Strict client-facing response returned after successful Weekly Consilium completion.
    Contains ONLY verified user reports (weekly overall report for Vault + 8 dated system reports for Atlas).
    Excludes weekly_deputy_analysis, updated_weekly_strategy, and all internal analytics.
    """
    model_config = ConfigDict(extra="forbid")

    cycle_id: str
    sprint_id: str
    sprint_number: int
    report_date: str
    overall_report: ClientModelReportResponse
    system_reports: Dict[str, ClientModelReportResponse]


class WeeklySprintLifecycleState(BaseModel):
    """
    Authoritative server lifecycle state of weekly sprint progression for an active cycle.
    Enforces server-driven period eligibility, preventing client-side sprint creation
    or premature week progression.
    """
    model_config = ConfigDict(extra="forbid")

    current_weekly_sprint_id: Optional[str] = None
    weekly_sprint_number: Optional[int] = None
    is_weekly_consilium_completed: bool = False
    can_start_next_sprint: bool = False
    next_sprint_id: Optional[str] = None
    current_calendar_week: Optional[int] = None
    current_calendar_day: Optional[int] = None


# =====================================================================
# 7. STAGE 3.4 — LAB REVISION CONSILIUM & VERSIONED LAB LEDGER SCHEMAS
# =====================================================================

LAB_REVISION_COOLDOWN_HOURS: int = 168

MEDICAL_SAFETY_COOLDOWN_NOTICE: str = (
    "IMPORTANT MEDICAL NOTICE: AI re-evaluation cooldown applies only to automated "
    "consilium recalculation. If any laboratory values show potentially dangerous "
    "deviations or if you experience concerning symptoms, seek in-person medical "
    "attention immediately rather than waiting for the AI cooldown to expire."
)

_INSUFFICIENT_GROUNDS_SUBSTRINGS: tuple[str, ...] = (
    "sufficient grounds",
    "insufficient grounds",
    "do not provide grounds",
    "does not provide grounds",
    "no grounds to revise",
    "no basis to change",
    "no basis to revise",
    "insufficient new data",
    "не дают достаточных оснований",
    "недостаточно оснований",
    "не дают оснований",
    "недостаточно данных для пересмотра",
)


def validate_lab_revision_unchanged_explanation(
    text: str, has_changes: bool, field_label: str = "Lab revision system summary"
) -> str:
    cleaned = validate_no_guaranteed_unchanged_claim(text, field_label)
    if not has_changes:
        lower = cleaned.lower()
        if not any(phrase in lower for phrase in _INSUFFICIENT_GROUNDS_SUBSTRINGS):
            raise ValueError(
                f"{field_label} with has_changes=False must explicitly explain that "
                "available new data does not provide sufficient grounds to change the previous assessment."
            )
    return cleaned


class LabEventType(str, Enum):
    """
    Distinguishes between:
    - initial: baseline laboratory entry;
    - new_study: new laboratory test result from another sample date;
    - correction: correction of a previously entered erroneous value (without erasing the old record);
    - invalidated: marking a prior observation as erroneous/removed while keeping it in history.
    """
    INITIAL = "initial"
    NEW_STUDY = "new_study"
    CORRECTION = "correction"
    INVALIDATED = "invalidated"


class LabReferenceRange(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    low: Optional[float] = None
    high: Optional[float] = None
    text: Optional[str] = Field(default=None, max_length=128)


def _parse_bool_strict(val: Any) -> bool:
    """Safely coerces boolean inputs including string representations ('true', 'false', '0', '1')."""
    if isinstance(val, bool):
        return val
    if isinstance(val, (int, float)):
        return bool(val)
    if isinstance(val, str):
        cleaned = val.strip().lower()
        if cleaned in ("true", "1", "yes", "t"):
            return True
        if cleaned in ("false", "0", "no", "f", ""):
            return False
    return bool(val)


def normalize_lab_date(raw_val: Any) -> Tuple[Optional[str], bool]:
    """
    Normalizes laboratory date to canonical ISO format 'YYYY-MM-DD',
    strictly verifying calendar correctness (leap years, month bounds 1..12, day bounds 1..31).
    Accepts:
    - ISO date: 'YYYY-MM-DD'
    - ISO datetime: 'YYYY-MM-DDTHH:MM:SS...' or 'YYYY-MM-DD HH:MM:SS...'
    - European/Russian: 'DD.MM.YYYY', 'DD/MM/YYYY', 'DD-MM-YYYY'
    - Slash/Dot: 'YYYY/MM/DD', 'YYYY.MM.DD'
    - datetime.date / datetime.datetime objects
    Rejects:
    - Invalid calendar dates (e.g. 2026-02-30, 2026-13-45) -> returns (None, False)
    - Free text / unverified strings -> returns (None, False)
    - Empty / None -> returns (None, True)
    Returns: (normalized_date_str, is_valid)
    """
    if raw_val is None:
        return (None, True)
    if isinstance(raw_val, datetime):
        return (raw_val.strftime("%Y-%m-%d"), True)
    if isinstance(raw_val, date):
        return (raw_val.strftime("%Y-%m-%d"), True)
    if not isinstance(raw_val, str):
        return (None, False)
    val_str = raw_val.strip()
    if not val_str:
        return (None, True)

    # 1. Strict ISO date: YYYY-MM-DD
    m_iso = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", val_str)
    if m_iso:
        y, m, d = int(m_iso.group(1)), int(m_iso.group(2)), int(m_iso.group(3))
        try:
            parsed_d = date(y, m, d)
            if 1900 <= parsed_d.year <= 2100:
                return (parsed_d.isoformat(), True)
        except ValueError:
            return (None, False)

    # 2. Strict ISO datetime: YYYY-MM-DD[T ]HH:MM(:SS)?(Z|[+-]HH:MM)?$
    m_dt = re.match(
        r"^(\d{4})-(\d{2})-(\d{2})[T\s](\d{2}):(\d{2})(?::(\d{2})(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?$",
        val_str,
    )
    if m_dt:
        y, m, d = int(m_dt.group(1)), int(m_dt.group(2)), int(m_dt.group(3))
        hr, mn = int(m_dt.group(4)), int(m_dt.group(5))
        sec = int(m_dt.group(6)) if m_dt.group(6) else 0
        if 0 <= hr <= 23 and 0 <= mn <= 59 and 0 <= sec <= 59:
            try:
                parsed_d = date(y, m, d)
                if 1900 <= parsed_d.year <= 2100:
                    return (parsed_d.isoformat(), True)
            except ValueError:
                return (None, False)

    # 3. European/Russian: DD.MM.YYYY or DD/MM/YYYY or DD-MM-YYYY
    m_dmy = re.match(r"^(\d{2})[./-](\d{2})[./-](\d{4})$", val_str)
    if m_dmy:
        d, m, y = int(m_dmy.group(1)), int(m_dmy.group(2)), int(m_dmy.group(3))
        try:
            parsed_d = date(y, m, d)
            if 1900 <= parsed_d.year <= 2100:
                return (parsed_d.isoformat(), True)
        except ValueError:
            return (None, False)

    # 4. Slash / Dot: YYYY/MM/DD or YYYY.MM.DD
    m_ymd = re.match(r"^(\d{4})[./](\d{2})[./](\d{2})$", val_str)
    if m_ymd:
        y, m, d = int(m_ymd.group(1)), int(m_ymd.group(2)), int(m_ymd.group(3))
        try:
            parsed_d = date(y, m, d)
            if 1900 <= parsed_d.year <= 2100:
                return (parsed_d.isoformat(), True)
        except ValueError:
            return (None, False)

    return (None, False)


def normalize_marker_slug(raw_key: Any) -> str:
    """Normalizes marker key to lowercase snake_case alphanumeric string."""
    cleaned = re.sub(r"[^a-z0-9_]+", "_", str(raw_key or "").strip().lower()).strip("_")
    return cleaned or "marker"


class LabObservationEntry(BaseModel):
    """
    Immutable single observation record in the server-side laboratory ledger.
    Previous records are never overwritten in place; corrections create a new entry
    referencing `supersedes_observation_id` while the original entry stays in history
    with `is_active=False`.
    """
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    observation_id: str = Field(..., min_length=3, max_length=64)
    marker_key: str = Field(..., min_length=1, max_length=64)
    display_name: str = Field(..., min_length=1, max_length=128)
    value: Optional[float | int | str] = None
    numeric_value: Optional[float] = None
    unit: Optional[str] = Field(default=None, max_length=64)
    sample_date: Optional[str] = Field(default=None, max_length=32)
    reference_range: Optional[LabReferenceRange] = None
    event_type: LabEventType = Field(default=LabEventType.INITIAL)
    supersedes_observation_id: Optional[str] = Field(default=None, max_length=64)
    superseded_by_observation_id: Optional[str] = Field(default=None, max_length=64)
    correction_reason: Optional[str] = Field(default=None, max_length=256)
    is_active: bool = Field(default=True)
    is_critical_flag: bool = Field(default=False)
    is_uncertain: bool = Field(default=False)
    uncertainty_note: Optional[str] = Field(default=None, max_length=256)
    is_recognition_ambiguous: bool = Field(default=False)
    recorded_at: str = Field(..., min_length=1, max_length=64)
    study_id: Optional[str] = Field(default=None, max_length=128)
    is_user_corrected: bool = Field(default=False)

    @field_validator("marker_key", mode="before")
    @classmethod
    def parse_marker_key(cls, v: Any) -> str:
        return normalize_marker_slug(v)

    @field_validator("sample_date", mode="before")
    @classmethod
    def parse_sample_date(cls, v: Any) -> Optional[str]:
        if v is None:
            return None
        norm_date, is_valid = normalize_lab_date(v)
        return norm_date if is_valid else None

    @field_validator("is_uncertain", "is_recognition_ambiguous", "is_critical_flag", "is_active", "is_user_corrected", mode="before")
    @classmethod
    def parse_bool_fields(cls, v: Any) -> bool:
        return _parse_bool_strict(v)


class LabMarkerMutationItem(BaseModel):
    """
    Single laboratory mutation submitted by the user or preparatory pipeline.
    Supports adding a new marker, recording a new study with a different sample_date,
    correcting an erroneous old value, or invalidating an erroneous entry.
    """
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    marker_key: str = Field(..., min_length=1, max_length=64)
    display_name: Optional[str] = Field(default=None, max_length=128)
    event_type: LabEventType = Field(default=LabEventType.NEW_STUDY)
    value: Optional[float | int | str] = None
    unit: Optional[str] = Field(default=None, max_length=64)
    sample_date: Optional[str] = Field(default=None, max_length=32)
    reference_range: Optional[LabReferenceRange] = None
    supersedes_observation_id: Optional[str] = Field(default=None, max_length=64)
    correction_reason: Optional[str] = Field(default=None, max_length=256)
    is_critical_flag: bool = Field(default=False)
    is_uncertain: bool = Field(default=False)
    uncertainty_note: Optional[str] = Field(default=None, max_length=256)
    is_recognition_ambiguous: bool = Field(default=False)
    study_id: Optional[str] = Field(default=None, max_length=128)
    is_user_corrected: bool = Field(default=False)

    @field_validator("marker_key", mode="before")
    @classmethod
    def parse_marker_key(cls, v: Any) -> str:
        return normalize_marker_slug(v)

    @field_validator("sample_date", mode="before")
    @classmethod
    def parse_sample_date(cls, v: Any) -> Optional[str]:
        if v is None:
            return None
        norm_date, is_valid = normalize_lab_date(v)
        return norm_date if is_valid else None

    @field_validator("is_uncertain", "is_recognition_ambiguous", "is_critical_flag", "is_user_corrected", mode="before")
    @classmethod
    def parse_bool_fields(cls, v: Any) -> bool:
        return _parse_bool_strict(v)

    @model_validator(mode="after")
    def validate_mutation_fields(self) -> "LabMarkerMutationItem":
        if self.event_type != LabEventType.INVALIDATED:
            if self.value is None or (isinstance(self.value, str) and not self.value.strip()):
                raise ValueError(
                    f"Laboratory marker '{self.marker_key}' with event_type='{self.event_type.value}' "
                    "requires a non-empty value."
                )
        return self


class LabLedgerUpsertRequest(BaseModel):
    """
    Request to save new or edited laboratory markers in the current medical cycle.
    Saving laboratory data is always allowed immediately with zero cooldown.
    """
    model_config = ConfigDict(extra="forbid")

    cycle_id: str = Field(..., min_length=3, max_length=64)
    mutations: List[LabMarkerMutationItem] = Field(..., min_length=1)


class LabLedgerStateResponse(BaseModel):
    """
    Client-safe view of the versioned laboratory ledger and its AI evaluation status.
    Explicitly indicates when new/edited laboratory values have not yet been evaluated
    by a new Full Consilium version, so old AI conclusions are never mistaken for
    an analysis of the new unreviewed labs.
    """
    model_config = ConfigDict(extra="forbid")

    cycle_id: str
    ledger_version: int = Field(..., ge=0)
    last_evaluated_ledger_version: int = Field(..., ge=0)
    has_unreviewed_lab_changes: bool
    evaluation_status: str
    evaluation_notice: str
    has_critical_flags: bool = Field(default=False)
    safety_notice: str = Field(default=MEDICAL_SAFETY_COOLDOWN_NOTICE)
    active_markers: Dict[str, LabObservationEntry] = Field(default_factory=dict)
    observations_history: List[LabObservationEntry] = Field(default_factory=list)


class LabDiffItem(BaseModel):
    """
    Deterministic comparison entry for a single laboratory marker before running
    the Full Lab-Revision Consilium.
    Never labels a numeric shift as a clinical improvement or deterioration without context.
    """
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    marker_key: str
    display_name: str
    change_type: str
    previous_observation_id: Optional[str] = None
    new_observation_id: Optional[str] = None
    previous_value: Optional[float | int | str] = None
    new_value: Optional[float | int | str] = None
    previous_unit: Optional[str] = None
    new_unit: Optional[str] = None
    previous_sample_date: Optional[str] = None
    new_sample_date: Optional[str] = None
    previous_reference_range: Optional[LabReferenceRange] = None
    new_reference_range: Optional[LabReferenceRange] = None
    directly_comparable: bool = Field(default=True)
    comparability_note: str
    neutral_summary: str


class LaboratoryDiffReport(BaseModel):
    """
    Complete deterministic diff between the previously evaluated laboratory state
    and the current laboratory ledger. Stored as a server_only audit record.
    """
    model_config = ConfigDict(extra="forbid")

    cycle_id: str
    baseline_ledger_version: int
    target_ledger_version: int
    generated_at_utc: str
    added_markers: List[LabDiffItem] = Field(default_factory=list)
    modified_values: List[LabDiffItem] = Field(default_factory=list)
    corrected_error_values: List[LabDiffItem] = Field(default_factory=list)
    new_dated_studies: List[LabDiffItem] = Field(default_factory=list)
    unit_changed_markers: List[LabDiffItem] = Field(default_factory=list)
    removed_or_invalidated_markers: List[LabDiffItem] = Field(default_factory=list)
    unchanged_markers: List[LabDiffItem] = Field(default_factory=list)


class LabRevisionSystemUpdateItem(BaseModel):
    """
    Single dated user-facing update for one of the 8 Atlas systems generated during
    the Full Lab-Revision Consilium.
    - When has_changes is False, summary must explicitly explain that available new
      data does not provide sufficient grounds to change the previous assessment,
      and must never claim that health is guaranteed unchanged.
    """
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    summary: str = Field(..., min_length=1)
    has_changes: bool = Field(default=True)

    @model_validator(mode="after")
    def verify_summary_rules(self) -> "LabRevisionSystemUpdateItem":
        self.summary = validate_lab_revision_unchanged_explanation(
            self.summary,
            self.has_changes,
            "Lab revision system summary",
        )
        return self


class LabRevisionUserSystemReportsMap(BaseModel):
    """
    8 separate dated user-facing system conclusions generated by Deputy Chief Physician (Flash)
    during the Full Lab-Revision Consilium. All 8 systems are strictly required.
    """
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    cvs: LabRevisionSystemUpdateItem
    gi_tract: LabRevisionSystemUpdateItem
    respiratory: LabRevisionSystemUpdateItem
    nervous: LabRevisionSystemUpdateItem
    musculoskeletal: LabRevisionSystemUpdateItem
    kidneys: LabRevisionSystemUpdateItem
    immune: LabRevisionSystemUpdateItem
    endocrine: LabRevisionSystemUpdateItem

    @model_validator(mode="after")
    def verify_all_eight_systems_present(self) -> "LabRevisionUserSystemReportsMap":
        for key in SYSTEM_KEYS:
            item = getattr(self, key, None)
            if not isinstance(item, LabRevisionSystemUpdateItem) or not item.summary.strip():
                raise ValueError(
                    f"Incomplete lab revision user_system_reports: system '{key}' must be present and non-empty."
                )
        return self

    def to_dict(self) -> Dict[str, Dict[str, Any]]:
        return {key: getattr(self, key).model_dump() for key in SYSTEM_KEYS}


class LabRevisionFlashMasterOutput(BaseModel):
    """
    Structured output contract for Deputy Chief Physician (Flash) during Full Lab-Revision Consilium:
    - flash_general_shelf (server_only comparative intersystem synthesis)
    - flash_training_shelf (server_only clinical hypotheses, deficits, comparative audit of 8 models)
    - user_system_reports (8 dated user-facing organ reports to be verified by Pro)
    """
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    flash_general_shelf: str = Field(..., min_length=1)
    flash_training_shelf: str = Field(..., min_length=1)
    user_system_reports: LabRevisionUserSystemReportsMap


class LabRevisionCooldownStatus(BaseModel):
    """
    Authoritative server-time cooldown status for user-initiated Full Lab-Revision Consilium (168 hours).
    """
    model_config = ConfigDict(extra="forbid")

    cycle_id: str
    cooldown_hours: int = Field(default=LAB_REVISION_COOLDOWN_HOURS)
    is_allowed: bool
    remaining_seconds: int = Field(default=0, ge=0)
    remaining_hours: float = Field(default=0.0, ge=0.0)
    last_completed_at_utc: Optional[str] = None
    next_available_at_utc: Optional[str] = None
    has_unreviewed_lab_changes: bool = Field(default=False)
    evaluation_status: str = Field(default="evaluated")
    message: str
    safety_notice: str = Field(default=MEDICAL_SAFETY_COOLDOWN_NOTICE)


class LabRevisionConsiliumRunRequest(BaseModel):
    """
    Request to trigger the user-initiated Full Lab-Revision Consilium
    (8 Gemini Flash-Lite -> Gemini Flash -> Gemini Pro) in the current medical cycle.
    Subject to the server-side 168-hour cooldown between successful full revisions.
    """
    model_config = ConfigDict(extra="forbid")

    cycle_id: str = Field(..., min_length=3, max_length=64)
    report_date: Optional[str] = Field(
        default=None,
        max_length=32,
        description="Optional ISO date string (YYYY-MM-DD) for user-facing dated entries (never used for cooldown calculation).",
    )


class LabRevisionConsiliumClientResponse(BaseModel):
    """
    Strict client-facing response returned after successful Full Lab-Revision Consilium completion.
    Contains ONLY verified user reports, numeric vitals thresholds, and evaluation/cooldown status.
    """
    model_config = ConfigDict(extra="forbid")

    cycle_id: str
    report_date: str
    evaluated_ledger_version: int
    evaluation_status: str
    overall_report: ClientModelReportResponse
    system_reports: Dict[str, ClientModelReportResponse]
    vitals_thresholds: ClientVitalsThresholds
    cooldown: LabRevisionCooldownStatus


KNOWN_DIMENSIONLESS_MARKERS = {
    "inr",
    "ph",
    "urine_ph",
    "blood_ph",
    "specific_gravity",
    "urine_specific_gravity",
    "ast_alt_ratio",
    "de_ritis_ratio",
    "bun_creatinine_ratio",
    "albumin_globulin_ratio",
    "atherogenic_index",
    "neutrophil_lymphocyte_ratio",
    "thrombocyte_large_cell_ratio",
    "pt_ratio",
    "homa_ir",
    "caro_index",
}


def is_dimensionless_marker(marker_key: str) -> bool:
    """Checks whether a clinical laboratory marker is inherently dimensionless."""
    k = marker_key.strip().lower()
    if k in KNOWN_DIMENSIONLESS_MARKERS:
        return True
    if k.endswith("_ratio") or k.endswith("_index"):
        return True
    return False


def to_decimal(val: Any) -> Optional[Decimal]:
    """
    Safely converts a numeric value (int, float, or numeric string) to Decimal for exact comparison.
    Returns None if the value cannot be parsed as a decimal number.
    Handles comma decimal separators common in European/Russian laboratory reports.
    """
    if val is None or isinstance(val, bool):
        return None
    if isinstance(val, (int, float)):
        return Decimal(str(val))
    if isinstance(val, str):
        cleaned = val.strip().replace(",", ".")
        try:
            return Decimal(cleaned)
        except InvalidOperation:
            return None
    return None


def are_lab_values_equivalent(
    val1: Any,
    num_val1: Optional[float],
    unit1: Optional[str],
    val2: Any,
    num_val2: Optional[float],
    unit2: Optional[str],
) -> bool:
    """
    Evaluates whether two laboratory values are clinically and technically equivalent.
    - Units must match (whitespace-stripped case-insensitive match).
    - If units differ (e.g. mmol/L vs mg/dL, or one has unit and the other does not), returns False.
    - If numeric, exact Decimal comparison: 5.20 == 5.2, but 5.20001 != 5.2.
    - If non-numeric (qualitative/textual), case-insensitive stripped string match.
    """
    norm_u1 = unit1.strip().lower() if (unit1 and unit1.strip()) else None
    norm_u2 = unit2.strip().lower() if (unit2 and unit2.strip()) else None
    if norm_u1 != norm_u2:
        return False

    d1 = to_decimal(num_val1) if num_val1 is not None else to_decimal(val1)
    d2 = to_decimal(num_val2) if num_val2 is not None else to_decimal(val2)
    if d1 is not None and d2 is not None:
        return d1 == d2

    if (d1 is not None) != (d2 is not None):
        return False

    s1 = str(val1).strip().lower() if val1 is not None else ""
    s2 = str(val2).strip().lower() if val2 is not None else ""
    return s1 == s2


QUALITATIVE_PATTERNS = {
    "отрицательно", "положительно", "не обнаружено", "обнаружено",
    "norm", "normal", "negative", "positive", "норма", "не выявлено", "выявлено",
    "trace", "traces", "следы", "незначительно", "умеренно", "значительно",
    "reactive", "non-reactive", "nonreactive", "indeterminate", "equivocal", "borderline",
    "сомнительно", "слабоположительно", "резкоположительно",
    "слабо", "резко", "weakly", "detected", "not", "non", "светло", "соломенно",
    "+", "++", "+++", "++++", "1+", "2+", "3+", "4+",
    "clear", "straw", "yellow", "cloudy", "turbid",
    "прозрачная", "соломенно-желтый", "желтый", "мутная",
}

_TITRATION_RE = re.compile(r"^\s*1\s*:\s*\d+\s*$")
_PLUS_SIGNS_RE = re.compile(r"^\s*(\+{1,4}|[1-4]\+)\s*$")


class VisionOcrMarkerItem(BaseModel):
    """
    Single recognized laboratory marker from Vision OCR.
    Captures complete laboratory indicators without hallucinations or omissions.
    """
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    original_name: str = Field(..., min_length=1, max_length=128)
    marker_key: str = Field(..., min_length=1, max_length=64)
    raw_value: str = Field(..., min_length=1, max_length=64)
    numeric_value: Optional[float] = None
    unit: Optional[str] = Field(default=None, max_length=64)
    reference_range: Optional[LabReferenceRange] = None
    sample_date: Optional[str] = Field(default=None, max_length=32)
    category: Optional[str] = Field(default="other", max_length=64)
    is_uncertain: bool = Field(default=False)
    uncertainty_note: Optional[str] = Field(default=None, max_length=256)
    is_critical_flag: bool = Field(default=False)
    requires_user_verification: bool = Field(default=False)
    is_recognition_ambiguous: bool = Field(default=False)
    row_id: Optional[str] = Field(default=None, max_length=64, description="Unique row identifier within the source page or document")
    source_row_ids: List[str] = Field(default_factory=list, description="IDs of source page extraction rows reconciled into this marker")
    page_number: Optional[int] = Field(default=None, ge=1, description="Page number where this marker was detected")
    source_filename: Optional[str] = Field(default=None, max_length=256, description="Source filename where this marker was detected")

    @field_validator(
        "is_uncertain",
        "is_recognition_ambiguous",
        "is_critical_flag",
        "requires_user_verification",
        mode="before",
    )
    @classmethod
    def parse_bool_fields(cls, v: Any) -> bool:
        return _parse_bool_strict(v)

    @field_validator("marker_key", mode="before")
    @classmethod
    def parse_marker_key(cls, v: Any) -> str:
        return normalize_marker_slug(v)

    @field_validator("sample_date", mode="before")
    @classmethod
    def parse_sample_date(cls, v: Any) -> Optional[str]:
        if v is None:
            return None
        norm_date, is_valid = normalize_lab_date(v)
        return norm_date if is_valid else None

    @model_validator(mode="before")
    @classmethod
    def capture_initial_recognition_ambiguity(cls, data: Any) -> Any:
        if isinstance(data, dict):
            raw_val = str(data.get("raw_value") or "").strip()
            num_val = data.get("numeric_value")

            # Try parsing numeric_value from raw_value if not explicitly provided (e.g. '5.5*')
            if num_val is None and raw_val:
                cleaned_num = raw_val.strip().replace(",", ".").rstrip("* \t")
                try:
                    num_val = float(cleaned_num)
                    data["numeric_value"] = num_val
                except ValueError:
                    num_val = None

            # Check for explicit ambiguous characters in raw_value (? or ~ or ≈)
            # Note: single footnote '*' does NOT denote OCR digit ambiguity
            has_ambiguous_chars = bool(re.search(r"[?~≈]", raw_val))

            explicit_flag = None
            if "is_recognition_ambiguous" in data and data["is_recognition_ambiguous"] is not None:
                explicit_flag = _parse_bool_strict(data["is_recognition_ambiguous"])

            if explicit_flag is True:
                # 1. Explicit True always preserves blocking / ambiguity
                data["is_recognition_ambiguous"] = True
            elif has_ambiguous_chars:
                # 2. Objective unreadability ('~2.?', '5.?', '??') cannot be cancelled by explicit False or OCR guess
                data["is_recognition_ambiguous"] = True
            elif explicit_flag is False:
                # 3. Explicit False is respected when no ambiguous characters exist
                data["is_recognition_ambiguous"] = False
            elif num_val is not None:
                # 4. Legible number: regardless of unit presence or is_uncertain, the number itself
                # is NOT an OCR error (uncertainty may be due to missing unit, footnote, reference range, etc.)
                data["is_recognition_ambiguous"] = False
            else:
                # 5. Non-numeric string: check if recognized qualitative pattern
                cleaned_raw = raw_val.lower().rstrip("* \t")
                tokens = cleaned_raw.replace("-", " ").split()
                is_qual = (
                    cleaned_raw in QUALITATIVE_PATTERNS
                    or (bool(tokens) and all(t in QUALITATIVE_PATTERNS for t in tokens))
                    or bool(_TITRATION_RE.match(cleaned_raw))
                    or bool(_PLUS_SIGNS_RE.match(cleaned_raw))
                )
                if is_qual:
                    # Recognized qualitative finding (negative, trace, equivocal): clinical, NOT OCR error
                    data["is_recognition_ambiguous"] = False
                else:
                    # Non-numeric, unparseable, non-qualitative: truly ambiguous OCR recognition
                    is_unc = _parse_bool_strict(data.get("is_uncertain", False))
                    data["is_recognition_ambiguous"] = True if is_unc else False
        elif hasattr(data, "is_uncertain"):
            raw_val = str(getattr(data, "raw_value", "") or "").strip()
            num_val = getattr(data, "numeric_value", None)
            if num_val is None and raw_val:
                cleaned_num = raw_val.strip().replace(",", ".").rstrip("* \t")
                try:
                    num_val = float(cleaned_num)
                    setattr(data, "numeric_value", num_val)
                except Exception:
                    num_val = None

            has_ambiguous_chars = bool(re.search(r"[?~≈]", raw_val))

            explicit_flag = None
            if hasattr(data, "is_recognition_ambiguous") and getattr(data, "is_recognition_ambiguous") is not None:
                explicit_flag = _parse_bool_strict(getattr(data, "is_recognition_ambiguous"))

            if explicit_flag is True:
                setattr(data, "is_recognition_ambiguous", True)
            elif has_ambiguous_chars:
                setattr(data, "is_recognition_ambiguous", True)
            elif explicit_flag is False:
                setattr(data, "is_recognition_ambiguous", False)
            elif num_val is not None:
                setattr(data, "is_recognition_ambiguous", False)
            else:
                cleaned_raw = raw_val.lower().rstrip("* \t")
                tokens = cleaned_raw.replace("-", " ").split()
                is_qual = (
                    cleaned_raw in QUALITATIVE_PATTERNS
                    or (bool(tokens) and all(t in QUALITATIVE_PATTERNS for t in tokens))
                    or bool(_TITRATION_RE.match(cleaned_raw))
                    or bool(_PLUS_SIGNS_RE.match(cleaned_raw))
                )
                if is_qual:
                    setattr(data, "is_recognition_ambiguous", False)
                else:
                    is_unc = _parse_bool_strict(getattr(data, "is_uncertain", False))
                    setattr(data, "is_recognition_ambiguous", True if is_unc else False)
        return data

    @field_validator("reference_range", mode="before")
    @classmethod
    def parse_reference_range(cls, v: Any) -> Optional[LabReferenceRange | Dict[str, Any]]:
        if v is None:
            return None
        if isinstance(v, (LabReferenceRange, dict)):
            return v
        if isinstance(v, str):
            cleaned = v.strip()
            if not cleaned:
                return None
            m = re.match(r"^\s*([+-]?\d+(?:\.\d+)?)\s*[-–—]\s*([+-]?\d+(?:\.\d+)?)\s*$", cleaned)
            if m:
                return {"low": float(m.group(1)), "high": float(m.group(2)), "text": cleaned}
            m_less = re.match(r"^\s*<\s*([+-]?\d+(?:\.\d+)?)\s*$", cleaned)
            if m_less:
                return {"high": float(m_less.group(1)), "text": cleaned}
            m_greater = re.match(r"^\s*>\s*([+-]?\d+(?:\.\d+)?)\s*$", cleaned)
            if m_greater:
                return {"low": float(m_greater.group(1)), "text": cleaned}
            return {"text": cleaned}
        return None

    @field_validator("numeric_value", mode="before")
    @classmethod
    def parse_numeric_value(cls, v: Any) -> Optional[float]:
        if v is None:
            return None
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return float(v)
        if isinstance(v, str):
            cleaned = v.strip().replace(",", ".")
            cleaned_num = cleaned.rstrip("* \t")
            try:
                return float(cleaned_num)
            except ValueError:
                return None
        return None

    @model_validator(mode="after")
    def validate_and_compute_verification_status(self) -> "VisionOcrMarkerItem":
        self.evaluate_verification_requirement()
        return self

    def evaluate_verification_requirement(self) -> bool:
        """
        Determines verification status and clinical uncertainty for this recognized marker.
        Under C-8 rules:
        - Dimensionless indicators (INR, pH, specific gravity, ratios, indices) are accepted without unit.
        - Numeric results with missing unit are preserved: numeric_value, unit=None, is_uncertain=True,
          with explicit uncertainty note distinguishing missing unit from recognition blur (Rule 5).
        - Qualitative and semi-quantitative values (Positive, Negative, Trace, +, ++, +++, ++++,
          reactive, non-reactive, titers, etc.) are preserved in original raw form (Rule 3).
        - Physical OCR ambiguity (smudge, blur, doubtful print) sets is_recognition_ambiguous=True
          and requires_user_verification=True, isolating it from active ledger numbers into unconfirmed_markers.
        - Recognition ambiguity and missing unit do not cancel each other out.
        - Repeated validation of the same object preserves ambiguity without degradation.
        """
        # Objective unreadability in raw_value ([?~≈]) cannot be bypassed by explicit False or OCR guess
        # Note: single footnote '*' does NOT denote OCR digit ambiguity
        if bool(re.search(r"[?~≈]", str(self.raw_value or ""))):
            self.is_recognition_ambiguous = True

        # 1. Physical recognition ambiguity on document (Rule 5 & C-8 Review Fix 1)
        if self.is_recognition_ambiguous:
            self.is_uncertain = True
            self.requires_user_verification = True
            if not self.uncertainty_note:
                self.uncertainty_note = "Recognition ambiguity: unclear print or damaged document"

        # 2. Specimen collection date check
        if not self.sample_date or not self.sample_date.strip():
            self.requires_user_verification = True
            date_note = "Missing specimen collection date on laboratory report"
            if not self.uncertainty_note:
                self.uncertainty_note = date_note
            elif date_note not in self.uncertainty_note and "Missing specimen collection date" not in self.uncertainty_note:
                self.uncertainty_note = f"{self.uncertainty_note}; {date_note}"
        else:
            if self.uncertainty_note and "Missing specimen collection date" in self.uncertainty_note:
                parts = [p.strip() for p in self.uncertainty_note.split(";") if "Missing specimen collection date" not in p]
                self.uncertainty_note = "; ".join(parts) if parts else None

        # 3. Qualitative / semi-quantitative vs Numeric evaluation
        if self.numeric_value is None:
            if not self.is_recognition_ambiguous:
                cleaned_raw = self.raw_value.strip().lower()
                tokens = cleaned_raw.replace("-", " ").split()
                is_recognized_qualitative = (
                    cleaned_raw in QUALITATIVE_PATTERNS
                    or (bool(tokens) and all(t in QUALITATIVE_PATTERNS for t in tokens))
                    or bool(_TITRATION_RE.match(cleaned_raw))
                    or bool(_PLUS_SIGNS_RE.match(cleaned_raw))
                )
                if not is_recognized_qualitative:
                    self.requires_user_verification = True
                    custom_note = f"Non-standard text value requiring verification: '{self.raw_value}'"
                    if not self.uncertainty_note:
                        self.uncertainty_note = custom_note
                    elif custom_note not in self.uncertainty_note and "Non-standard text value" not in self.uncertainty_note:
                        self.uncertainty_note = f"{self.uncertainty_note}; {custom_note}"
                else:
                    if any(w in cleaned_raw for w in ("сомнительно", "indeterminate", "equivocal", "borderline", "слабоположительно")):
                        self.is_uncertain = True
                        borderline_note = f"Borderline qualitative finding: '{self.raw_value}'"
                        if not self.uncertainty_note:
                            self.uncertainty_note = borderline_note
                        elif borderline_note not in self.uncertainty_note and "Borderline qualitative" not in self.uncertainty_note:
                            self.uncertainty_note = f"{self.uncertainty_note}; {borderline_note}"
        else:
            has_unit = bool(self.unit and self.unit.strip())
            if not has_unit:
                if is_dimensionless_marker(self.marker_key):
                    # Dimensionless marker (INR, pH, specific gravity, etc.) - accepted without unit (Rule 1)
                    pass
                else:
                    # Regular numeric marker with missing unit of measurement on report (Rule 2)
                    self.is_uncertain = True
                    unit_note = "Missing unit of measurement for numeric value on laboratory report"
                    if not self.uncertainty_note:
                        self.uncertainty_note = unit_note
                    elif unit_note not in self.uncertainty_note and "Missing unit of measurement" not in self.uncertainty_note:
                        self.uncertainty_note = f"{self.uncertainty_note}; {unit_note}"

        # 4. Final verification requirement resolution
        if (
            self.is_recognition_ambiguous
            or (not self.sample_date or not self.sample_date.strip())
            or (self.uncertainty_note and any(sig in self.uncertainty_note for sig in ("Fabricated", "omitted", "Preserved Stage 1")))
        ):
            self.requires_user_verification = True
            return True

        if self.numeric_value is None:
            return self.requires_user_verification

        self.requires_user_verification = False
        return False


class VisionOcrDocumentMetadata(BaseModel):
    """
    Document-level metadata extracted from laboratory test report images.
    """
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    sample_date: Optional[str] = Field(default=None, max_length=32)
    issue_date: Optional[str] = Field(default=None, max_length=32)
    laboratory_name: Optional[str] = Field(default=None, max_length=128)
    order_id: Optional[str] = Field(default=None, max_length=128)
    document_fingerprint: Optional[str] = Field(default=None, max_length=128)
    page_count: int = Field(default=1, ge=1)
    notes: Optional[str] = Field(default=None, max_length=512)

    @field_validator("sample_date", "issue_date", mode="before")
    @classmethod
    def parse_meta_dates(cls, v: Any) -> Optional[str]:
        if v is None:
            return None
        norm_date, is_valid = normalize_lab_date(v)
        return norm_date if is_valid else None


class VisionOcrModelOutput(BaseModel):
    """
    Structured Pydantic output schema produced by Gemini Vision OCR agent.
    Captures complete laboratory indicators without hallucinations or omissions.
    """
    model_config = ConfigDict(extra="forbid")

    document_metadata: VisionOcrDocumentMetadata = Field(default_factory=VisionOcrDocumentMetadata)
    markers: List[VisionOcrMarkerItem] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _normalize_payload_structure(cls, data: Any) -> Any:
        if isinstance(data, dict):
            # If model returned "indicators" instead of "markers"
            if "markers" not in data and "indicators" in data:
                data["markers"] = data.pop("indicators")
            # If document_metadata is already an instantiated VisionOcrDocumentMetadata object, preserve it
            if "document_metadata" in data and isinstance(data.get("document_metadata"), VisionOcrDocumentMetadata):
                pass
            # If model returned metadata fields at root level
            elif "document_metadata" not in data or not isinstance(data.get("document_metadata"), dict):
                meta = {}
                for k in ("sample_date", "issue_date", "laboratory_name", "page_count", "notes"):
                    if k in data:
                        meta[k] = data.pop(k)
                data["document_metadata"] = meta
            elif isinstance(data.get("document_metadata"), dict):
                # Move any stray root-level metadata keys into document_metadata
                for k in ("sample_date", "issue_date", "laboratory_name", "page_count", "notes"):
                    if k in data:
                        data["document_metadata"].setdefault(k, data.pop(k))
        return data


class LabVisionIntakeClientResponse(BaseModel):
    """
    Safe client-facing confirmation of laboratory Vision OCR processing.
    Separates confirmed structured markers from results requiring user verification;
    never exposes raw Gemini JSON, internal AI comments, or unverified hypotheses.
    """
    model_config = ConfigDict(extra="forbid")

    cycle_id: str
    status: str = "processed"
    ledger_version: int = Field(..., ge=0)
    extracted_count: int = Field(..., ge=0)
    requires_verification_count: int = Field(default=0, ge=0)
    document_metadata: VisionOcrDocumentMetadata
    markers: List[VisionOcrMarkerItem]
    confirmed_markers: List[VisionOcrMarkerItem] = Field(default_factory=list)
    unconfirmed_markers: List[VisionOcrMarkerItem] = Field(default_factory=list)
    has_unreviewed_lab_changes: bool = True
    privacy_notice: str = (
        "Laboratory documents processed server-side. Raw image bytes and unverified "
        "OCR hypotheses are never logged or exposed to the client."
    )


class VisionOcrPageExtraction(BaseModel):
    """
    Structured extraction produced by Stage 1 (Gemini 3.5 Flash-Lite) for a SINGLE laboratory report page.
    Captures complete raw indicators, row-level provenance, and page-level metadata without omissions.
    Includes four structural cues: printed dates, order IDs & types, printed page numbers, and table state.
    """
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    page_number: int = Field(default=1, ge=1, description="1-based index of this page within the document")
    source_filename: str = Field(default="page_1", min_length=1, max_length=256, description="Original filename of this page or PDF")
    source_file_id: Optional[str] = Field(default=None, max_length=64, description="Optional unique identifier of the source file upload")
    page_sample_date: Optional[str] = Field(default=None, max_length=32, description="Specimen collection date printed on this page header")
    page_issue_date: Optional[str] = Field(default=None, max_length=32, description="Report issue/printing date printed on this page header")
    raw_date_str: Optional[str] = Field(default=None, max_length=128, description="Raw unparsed date string if date type or format is ambiguous")
    laboratory_name: Optional[str] = Field(default=None, max_length=128, description="Name of laboratory detected on this page")
    order_id: Optional[str] = Field(default=None, max_length=128, description="Order, accession, barcode, or card ID detected on this page")
    order_id_type: Optional[str] = Field(default=None, max_length=64, description="Type of ID: order_number, barcode, medical_card, accession, etc.")
    printed_page_num: Optional[int] = Field(default=None, ge=1, description="Page number physically printed on the laboratory form itself")
    total_printed_pages: Optional[int] = Field(default=None, ge=1, description="Total pages physically printed on form if available (e.g. 3 from 'Стр. 1 из 3')")
    table_state: Optional[str] = Field(default=None, max_length=32, description="START (new table/section), CONTINUATION (table split continued), or null")
    section_name: Optional[str] = Field(default=None, max_length=128, description="Printed laboratory section title, e.g. 'Общий анализ крови'")
    markers: List[VisionOcrMarkerItem] = Field(default_factory=list, description="All indicators detected on this page")
    has_unreadable_regions: bool = Field(default=False, description="True if any part of the page is illegible, torn, smudged, or cropped")
    unreadable_regions_note: Optional[str] = Field(default=None, max_length=256, description="Details regarding unreadable regions")

    @field_validator("has_unreadable_regions", mode="before")
    @classmethod
    def parse_has_unreadable_regions(cls, v: Any) -> bool:
        return _parse_bool_strict(v)

    @field_validator("page_sample_date", "page_issue_date", mode="before")
    @classmethod
    def parse_page_dates(cls, v: Any) -> Optional[str]:
        if v is None:
            return None
        norm_date, is_valid = normalize_lab_date(v)
        return norm_date if is_valid else None

    @field_validator("table_state", mode="before")
    @classmethod
    def parse_table_state(cls, v: Any) -> Optional[str]:
        if v is None:
            return None
        cleaned = str(v).strip().upper()
        if cleaned in ("START", "CONTINUATION"):
            return cleaned
        return None

    @model_validator(mode="before")
    @classmethod
    def _normalize_page_extraction_payload(cls, data: Any) -> Any:
        if isinstance(data, dict):
            data = dict(data)
            # If payload has document_metadata, map common fields to page-level cues and remove it
            if "document_metadata" in data:
                dm = data.pop("document_metadata")
                if isinstance(dm, dict):
                    if not data.get("page_sample_date") and dm.get("sample_date"):
                        data["page_sample_date"] = dm.get("sample_date")
                    if not data.get("page_issue_date") and dm.get("issue_date"):
                        data["page_issue_date"] = dm.get("issue_date")
                    if not data.get("laboratory_name") and dm.get("laboratory_name"):
                        data["laboratory_name"] = dm.get("laboratory_name")
                    if not data.get("order_id") and dm.get("order_id"):
                        data["order_id"] = dm.get("order_id")
            # If model returned "indicators" instead of "markers"
            if "markers" not in data and "indicators" in data:
                data["markers"] = data.pop("indicators")
            # Ensure markers list items inherit page_number and source_filename if missing
            page_num = data.get("page_number", 1)
            source_fn = data.get("source_filename", "page_1")
            markers = data.get("markers")
            if isinstance(markers, list):
                for idx, m in enumerate(markers, start=1):
                    if isinstance(m, dict):
                        if not m.get("row_id"):
                            m["row_id"] = f"p{page_num}_r{idx}"
                        if not m.get("page_number"):
                            m["page_number"] = page_num
                        if not m.get("source_filename"):
                            m["source_filename"] = source_fn
        return data


class VisionOcrPriorBatchContext(BaseModel):
    """
    Concise context passed from prior batches to subsequent batches in multi-batch chaining.
    Carries header metadata and seen marker keys without blowing up the prompt context window.
    """
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    laboratory_name: Optional[str] = Field(default=None, max_length=128)
    order_id: Optional[str] = Field(default=None, max_length=128)
    default_sample_date: Optional[str] = Field(default=None, max_length=32)
    already_seen_marker_keys: List[str] = Field(default_factory=list, description="Unique normalized marker slugs already extracted")
    already_seen_row_ids: List[str] = Field(default_factory=list, description="Unique row IDs already processed in earlier batches")
    table_continuation_hints: Optional[str] = Field(default=None, max_length=512, description="Hint regarding table split at page boundary")
    last_processed_page: Optional[int] = Field(default=None, ge=1)

    @field_validator("default_sample_date", mode="before")
    @classmethod
    def parse_default_sample_date(cls, v: Any) -> Optional[str]:
        if v is None:
            return None
        norm_date, is_valid = normalize_lab_date(v)
        return norm_date if is_valid else None


class VisionOcrBatchReconciliationOutput(BaseModel):
    """
    Structured reconciliation produced by Stage 2 (Gemini 3.8 Flash) for a batch of 1 to 6 pages.
    Reconciles multi-page table splits, harmonizes dates, detects duplicates, and preserves row provenance.
    """
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    batch_index: int = Field(..., ge=1, description="1-based index of this batch")
    batch_page_count: int = Field(..., ge=1, le=12, description="Number of pages in this batch (1 to 6, max 12)")
    processed_page_numbers: List[int] = Field(default_factory=list, description="List of page numbers processed in this batch")
    document_metadata: VisionOcrDocumentMetadata = Field(default_factory=VisionOcrDocumentMetadata)
    reconciled_markers: List[VisionOcrMarkerItem] = Field(default_factory=list, description="Reconciled laboratory indicators")
    cross_page_notes: Optional[str] = Field(default=None, max_length=512, description="Notes on table splits or cross-page alignment")
    next_batch_context: Optional[VisionOcrPriorBatchContext] = None

    @model_validator(mode="before")
    @classmethod
    def _normalize_batch_output_payload(cls, data: Any) -> Any:
        if isinstance(data, dict):
            # If model returned "markers" instead of "reconciled_markers"
            if "reconciled_markers" not in data and "markers" in data:
                data["reconciled_markers"] = data.pop("markers")
            elif "reconciled_markers" not in data and "indicators" in data:
                data["reconciled_markers"] = data.pop("indicators")
            # If document_metadata is already an instantiated VisionOcrDocumentMetadata object, preserve it
            if "document_metadata" in data and isinstance(data.get("document_metadata"), VisionOcrDocumentMetadata):
                pass
            # If model returned metadata fields at root level
            elif "document_metadata" not in data or not isinstance(data.get("document_metadata"), dict):
                meta = {}
                for k in ("sample_date", "issue_date", "laboratory_name", "page_count", "notes", "order_id", "page_sample_date", "page_issue_date"):
                    if k in data:
                        val = data.pop(k)
                        clean_k = k.replace("page_", "")
                        meta[clean_k] = val
                if meta:
                    data["document_metadata"] = meta
            elif isinstance(data.get("document_metadata"), dict):
                for k in ("sample_date", "issue_date", "laboratory_name", "page_count", "notes", "order_id", "page_sample_date", "page_issue_date"):
                    if k in data:
                        val = data.pop(k)
                        clean_k = k.replace("page_", "")
                        data["document_metadata"].setdefault(clean_k, val)
        return data


class VisionOcrStudyLinkage(BaseModel):
    """
    Provenance grouping identifying which pages belong to which specific laboratory study/order.
    """
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    study_id: Optional[str] = Field(default=None, max_length=128, description="Order, accession, or study ID")
    laboratory_name: Optional[str] = Field(default=None, max_length=128)
    sample_date: Optional[str] = Field(default=None, max_length=32)
    page_numbers: List[int] = Field(default_factory=list, description="Global page numbers belonging to this study")
    confidence: Optional[str] = Field(default="high", max_length=32, description="high, moderate, or doubtful")
    linkage_notes: Optional[str] = Field(default=None, max_length=512)

    @field_validator("sample_date", mode="before")
    @classmethod
    def parse_sample_date(cls, v: Any) -> Optional[str]:
        if v is None:
            return None
        norm_date, is_valid = normalize_lab_date(v)
        return norm_date if is_valid else None


class VisionOcrReconciliationOutput(BaseModel):
    """
    Structured output produced by the single Gemini 3.8 Flash Reconciler covering ALL document pages (1 to 15).
    Reconciles multi-page studies, detects split tables, harmonizes headers and dates, and tracks row provenance.
    """
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    total_pages_reconciled: int = Field(default=1, ge=1, le=15, description="Total number of pages analyzed")
    document_metadata: VisionOcrDocumentMetadata = Field(default_factory=VisionOcrDocumentMetadata)
    reconciled_markers: List[VisionOcrMarkerItem] = Field(default_factory=list, description="All reconciled laboratory indicators")
    study_linkages: List[VisionOcrStudyLinkage] = Field(default_factory=list, description="Page groupings by laboratory study/order")
    reconciliation_notes: Optional[str] = Field(default=None, max_length=1024, description="Details on table stitches, separate studies, or ambiguities")

    @model_validator(mode="before")
    @classmethod
    def _normalize_reconciliation_output_payload(cls, data: Any) -> Any:
        if isinstance(data, dict):
            data = dict(data)
            # Normalize total_pages_reconciled
            if "total_pages_reconciled" not in data:
                doc_meta = data.get("document_metadata")
                if isinstance(doc_meta, dict) and doc_meta.get("page_count"):
                    try:
                        data["total_pages_reconciled"] = min(15, max(1, int(doc_meta["page_count"])))
                    except (ValueError, TypeError):
                        data["total_pages_reconciled"] = 1
                elif "page_count" in data:
                    try:
                        data["total_pages_reconciled"] = min(15, max(1, int(data.pop("page_count"))))
                    except (ValueError, TypeError):
                        data["total_pages_reconciled"] = 1
                else:
                    data["total_pages_reconciled"] = 1
            # If model returned "markers" instead of "reconciled_markers"
            if "reconciled_markers" not in data and "markers" in data:
                data["reconciled_markers"] = data.pop("markers")
            elif "reconciled_markers" not in data and "indicators" in data:
                data["reconciled_markers"] = data.pop("indicators")
            # If document_metadata is already an instantiated VisionOcrDocumentMetadata object, preserve it
            if "document_metadata" in data and isinstance(data.get("document_metadata"), VisionOcrDocumentMetadata):
                pass
            # If model returned metadata fields at root level
            elif "document_metadata" not in data or not isinstance(data.get("document_metadata"), dict):
                meta = {}
                for k in ("sample_date", "issue_date", "laboratory_name", "page_count", "notes", "order_id", "page_sample_date", "page_issue_date"):
                    if k in data:
                        val = data.pop(k)
                        clean_k = k.replace("page_", "")
                        meta[clean_k] = val
                if meta:
                    data["document_metadata"] = meta
            elif isinstance(data.get("document_metadata"), dict):
                for k in ("sample_date", "issue_date", "laboratory_name", "page_count", "notes", "order_id", "page_sample_date", "page_issue_date"):
                    if k in data:
                        val = data.pop(k)
                        clean_k = k.replace("page_", "")
                        data["document_metadata"].setdefault(clean_k, val)
        return data


class EcgRecordingType(str, Enum):
    """Classification of the physical recording device / lead setup."""
    SINGLE_LEAD_SMARTWATCH = "single_lead_smartwatch"
    STANDARD_12_LEAD = "standard_12_lead"
    OTHER_MULTICHANNEL = "other_multichannel"
    UNKNOWN = "unknown"


class EcgRecordingQuality(str, Enum):
    """Technical quality of the electrocardiogram tracing."""
    GOOD = "good"
    MODERATE = "moderate"
    POOR = "poor"
    UNREADABLE = "unreadable"


def _parse_hr_metric(val: Any) -> Tuple[Optional[float], Optional[str], Optional[Dict[str, Any]]]:
    """
    Safely parses heart rate metric from int, float, or formatted strings (e.g. '72 bpm', '72.4').
    Validates units: accepts bpm or unitless; rejects mismatched time units (ms, s).
    Fractional heart rates are rounded to the nearest integer and documented in conversion metadata.
    Returns (parsed_float, error_reason, raw_info_metadata).
    """
    if val is None:
        return (None, None, None)
    if isinstance(val, bool):
        return (None, "Boolean value cannot be used as heart rate", None)
    if isinstance(val, (int, float)):
        if math.isnan(val) or math.isinf(val):
            return (None, "NaN or Infinite numerical value", None)
        if isinstance(val, float) and abs(val - round(val)) > 1e-4:
            rounded_val = int(round(val))
            raw_info = {
                "raw_value": val,
                "conversion": f"Fractional heart rate ({val}) rounded to nearest integer ({rounded_val} bpm).",
            }
            return (float(rounded_val), None, raw_info)
        return (float(val), None, None)
    if isinstance(val, str):
        val_clean = val.strip()
        if not val_clean:
            return (None, "Empty string value", None)
        if re.search(r"\b(ms|мс|sec|сек|s|с)\b", val_clean, re.IGNORECASE):
            return (None, f"Mismatched unit for heart rate: found time unit in '{val_clean}' (expected bpm).", None)
        m = re.match(r"^([+-]?[0-9]+(?:\.[0-9]+)?)\s*(?:bpm|уд/?мин|уд)?$", val_clean, re.IGNORECASE)
        if m:
            try:
                num = float(m.group(1))
                if math.isnan(num) or math.isinf(num):
                    return (None, "NaN or Infinite string value", None)
                if abs(num - round(num)) > 1e-4:
                    rounded_val = int(round(num))
                    raw_info = {
                        "raw_value": val,
                        "conversion": f"Fractional heart rate string ({val_clean!r}) rounded to nearest integer ({rounded_val} bpm).",
                    }
                    return (float(rounded_val), None, raw_info)
                return (num, None, None)
            except (ValueError, TypeError):
                pass
        return (None, f"Non-numeric or unrecognized heart rate string value '{val_clean}'", None)
    return (None, f"Unsupported value type '{type(val).__name__}'", None)


def _parse_interval_metric(val: Any) -> Tuple[Optional[float], Optional[str], Optional[Dict[str, Any]]]:
    """
    Safely parses ECG intervals (PR, QRS, QT, QTc) from int, float, or formatted strings.
    Validates units: accepts ms/мс, explicit seconds (s/sec/с/сек) converted to ms, or unitless.
    Rejects mismatched frequency units (bpm).
    Returns (parsed_float_in_ms, error_reason, raw_info_metadata).
    """
    if val is None:
        return (None, None, None)
    if isinstance(val, bool):
        return (None, "Boolean value cannot be used as interval metric", None)
    if isinstance(val, (int, float)):
        if math.isnan(val) or math.isinf(val):
            return (None, "NaN or Infinite numerical value", None)
        return (float(val), None, None)
    if isinstance(val, str):
        val_clean = val.strip()
        if not val_clean:
            return (None, "Empty string value", None)
        if re.search(r"\b(bpm|уд/?мин|уд)\b", val_clean, re.IGNORECASE):
            return (None, f"Mismatched unit for ECG interval: found frequency unit in '{val_clean}' (expected ms).", None)
        # Explicit seconds conversion to milliseconds
        m_sec = re.match(r"^([+-]?[0-9]+(?:\.[0-9]+)?)\s*(?:s|sec|сек|с)$", val_clean, re.IGNORECASE)
        if m_sec:
            try:
                sec_num = float(m_sec.group(1))
                if math.isnan(sec_num) or math.isinf(sec_num):
                    return (None, "NaN or Infinite string value", None)
                ms_num = round(sec_num * 1000.0, 1)
                raw_info = {
                    "raw_value": val,
                    "conversion": f"Converted from seconds ({val_clean}) to milliseconds ({ms_num} ms).",
                }
                return (ms_num, None, raw_info)
            except (ValueError, TypeError):
                pass
        m_ms = re.match(r"^([+-]?[0-9]+(?:\.[0-9]+)?)\s*(?:ms|мс)?$", val_clean, re.IGNORECASE)
        if m_ms:
            try:
                num = float(m_ms.group(1))
                if math.isnan(num) or math.isinf(num):
                    return (None, "NaN or Infinite string value", None)
                return (num, None, None)
            except (ValueError, TypeError):
                pass
        return (None, f"Non-numeric or unrecognized interval string value '{val_clean}'", None)
    return (None, f"Unsupported value type '{type(val).__name__}'", None)


def classify_lead_setup(
    recording_type: Optional[str],
    lead_name: Optional[str],
) -> Dict[str, Any]:
    """
    Deterministically evaluates lead setup configuration and detects conflicts
    between recording_type and lead_name without relying solely on device brand names.
    Distinguishes:
    - Study recording type (e.g. standard_12_lead vs single-lead)
    - Specific individual lead name (e.g. "Lead I", "Lead II", "V1")
    - Available lead configuration (full 12-lead vs single-channel vs contradictory)
    """
    rec_clean = (recording_type or "").lower().strip()
    lead_clean = (lead_name or "").lower().strip()

    is_12_lead_claimed = (
        rec_clean == EcgRecordingType.STANDARD_12_LEAD.value
        or "12-lead" in rec_clean
        or "12 lead" in rec_clean
        or "12lead" in rec_clean
        or "12-канальн" in rec_clean
    )

    single_descriptors = (
        "single_lead",
        "single-lead",
        "single lead",
        "1-lead",
        "1 lead",
        "1lead",
        "one-lead",
        "single_channel",
        "single channel",
        "одноканальн",
        "smartwatch",
        "watch",
    )

    is_single_lead_rec = (
        rec_clean in (
            EcgRecordingType.SINGLE_LEAD_SMARTWATCH.value,
            "single_lead",
            "single-lead",
            "single lead",
            "1-lead",
            "1 lead",
            "1lead",
            "single_channel",
            "single channel",
        )
        or any(d in rec_clean for d in single_descriptors)
    )

    has_single_lead_descriptor_in_lead = any(d in lead_clean for d in single_descriptors)

    # 1. Contradiction: claimed 12-lead study but explicit single-lead/smartwatch indicators present
    if is_12_lead_claimed and (is_single_lead_rec or has_single_lead_descriptor_in_lead):
        return {
            "category": "conflict",
            "can_compute_axis": False,
            "is_single_lead": False,
            "is_confirmed_12_lead": False,
            "reason": (
                f"Contradictory ECG lead metadata: recording_type claims 12-lead study, "
                f"but descriptors indicate single-lead configuration ('{lead_name}'). "
                "Frontal plane electrical axis cannot be calculated from contradictory metadata."
            ),
        }

    # 2. Confirmed 12-lead setup (Lead I is a valid lead in 12-lead study)
    if is_12_lead_claimed:
        return {
            "category": "confirmed_12_lead",
            "can_compute_axis": True,
            "is_single_lead": False,
            "is_confirmed_12_lead": True,
            "reason": None,
        }

    # 3. Confirmed single-lead setup
    multi_indicators = ("12-lead", "12 lead", "12lead", "multi", "precordial", ",", ";", "/", "v1-v6", "i-iii")
    has_multi = any(m in lead_clean for m in multi_indicators)
    is_isolated_lead = False
    if not has_multi and lead_clean:
        if re.search(r"\b(lead\s*(?:i{1,3}|[1-3]|avr|avl|avf|v[1-6])|v[1-6])\b", lead_clean):
            is_isolated_lead = True

    if is_single_lead_rec or has_single_lead_descriptor_in_lead or is_isolated_lead:
        return {
            "category": "single_lead",
            "can_compute_axis": False,
            "is_single_lead": True,
            "is_confirmed_12_lead": False,
            "reason": "Single-lead recording cannot determine electrical heart axis.",
        }

    # 4. Undetermined / unknown lead setup
    return {
        "category": "undetermined",
        "can_compute_axis": False,
        "is_single_lead": False,
        "is_confirmed_12_lead": False,
        "reason": (
            "Recording device lead configuration is undetermined; findings cannot be "
            "interpreted as a full 12-lead ECG or used to calculate frontal plane electrical axis."
        ),
    }


def _is_single_lead_setup(recording_type: Optional[str], lead_name: Optional[str]) -> bool:
    """Backward compatibility helper for single-lead classification."""
    return classify_lead_setup(recording_type, lead_name)["is_single_lead"]


class VisionEcgModelOutput(BaseModel):
    """
    Structured Pydantic model produced by Gemini Vision ECG (role='vision_ecg').
    Enforces strict medical safety rules:
    - Single-lead recordings (e.g. smartwatch Lead I) CANNOT determine electrical axis.
    - If recording quality is unreadable, speculative intervals/rates must be null.
    - No fabricated measurements or premature categorical clinical diagnoses.
    - Technical context preserves raw uncomputable metrics with uncertainty reasons.
    """
    model_config = ConfigDict(extra="forbid")

    recording_type: str = "unknown"
    recording_quality: str = "moderate"
    lead_name: Optional[str] = None
    heart_rate_bpm: Optional[int] = Field(default=None, ge=20, le=300)
    rhythm_type: Optional[str] = None
    rhythm_regularity: Optional[str] = None
    pr_interval_ms: Optional[float] = Field(default=None, ge=40.0, le=600.0)
    qrs_duration_ms: Optional[float] = Field(default=None, ge=30.0, le=400.0)
    qt_interval_ms: Optional[float] = Field(default=None, ge=150.0, le=800.0)
    qtc_interval_ms: Optional[float] = Field(default=None, ge=200.0, le=800.0)
    electrical_axis: Optional[str] = None
    st_segment_findings: Optional[str] = None
    t_wave_findings: Optional[str] = None
    limitations: List[str] = Field(default_factory=list)
    uncertainties: List[str] = Field(default_factory=list)
    raw_unresolved_metrics: Dict[str, Any] = Field(default_factory=dict)
    ecg_summary: str = Field(..., min_length=5)
    potential_urgent_warning: Optional[str] = None

    @model_validator(mode="before")
    @classmethod
    def sanitize_unusual_metrics(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data

        raw_unresolved = dict(data.get("raw_unresolved_metrics") or {})
        uncertainties = list(data.get("uncertainties") or [])
        urgent_warning = data.get("potential_urgent_warning")

        metrics_specs = [
            ("heart_rate_bpm", 20.0, 300.0, "bpm", True, _parse_hr_metric),
            ("pr_interval_ms", 40.0, 600.0, "ms", False, _parse_interval_metric),
            ("qrs_duration_ms", 30.0, 400.0, "ms", False, _parse_interval_metric),
            ("qt_interval_ms", 150.0, 800.0, "ms", False, _parse_interval_metric),
            ("qtc_interval_ms", 200.0, 800.0, "ms", False, _parse_interval_metric),
        ]

        for field_name, min_v, max_v, unit, is_int, parser in metrics_specs:
            if field_name not in data:
                continue
            orig_val = data[field_name]
            if orig_val is None:
                continue

            parsed_num, parse_err, raw_info = parser(orig_val)

            if parse_err is not None:
                if field_name not in raw_unresolved:
                    raw_unresolved[field_name] = {
                        "raw_value": orig_val,
                        "reason": parse_err,
                    }
                data[field_name] = None
                note = f"Metric '{field_name}' reading (raw: {orig_val!r}) cannot be verified: {parse_err}."
                if note not in uncertainties:
                    uncertainties.append(note)
                continue

            assert parsed_num is not None
            if parsed_num < min_v or parsed_num > max_v:
                if field_name not in raw_unresolved:
                    reason = (
                        f"Reading ({orig_val}) is outside standard computational range "
                        f"({min_v}-{max_v} {unit}); requires clinical verification."
                    )
                    raw_unresolved[field_name] = {
                        "raw_value": orig_val,
                        "reason": reason,
                    }
                data[field_name] = None
                note = (
                    f"Reading of '{field_name}' (raw: {orig_val}) is outside standard computational "
                    f"range ({min_v}-{max_v} {unit}); requires verification."
                )
                if note not in uncertainties:
                    uncertainties.append(note)


            else:
                if raw_info is not None and field_name not in raw_unresolved:
                    raw_unresolved[field_name] = raw_info
                data[field_name] = int(round(parsed_num)) if is_int else round(parsed_num, 1)

        data["raw_unresolved_metrics"] = raw_unresolved
        data["uncertainties"] = uncertainties
        if urgent_warning:
            data["potential_urgent_warning"] = urgent_warning

        return data

    @model_validator(mode="after")
    def enforce_clinical_safety_rules(self) -> "VisionEcgModelOutput":
        lead_classification = classify_lead_setup(self.recording_type, self.lead_name)

        if lead_classification["category"] == "conflict":
            # Metadata contradiction: preserve recording_type, nullify axis, record uncertainty
            self.electrical_axis = None
            unc = (
                lead_classification["reason"]
                or f"Contradictory ECG lead metadata between '{self.recording_type}' and '{self.lead_name}'; "
                "frontal plane electrical axis cannot be calculated."
            )
            if not any("contradict" in u.lower() or "conflict" in u.lower() for u in self.uncertainties):
                self.uncertainties.append(unc)
            axis_lim = "Electrical heart axis cannot be determined from contradictory or unverified lead configuration."
            if not any("axis" in lim.lower() for lim in self.limitations):
                self.limitations.append(axis_lim)

        elif lead_classification["is_single_lead"]:
            # Confirmed single-lead recordings cannot determine electrical heart axis
            self.electrical_axis = None
            axis_lim = "Electrical heart axis cannot be determined from single-lead recording."
            if not any("axis" in lim.lower() for lim in self.limitations):
                self.limitations.append(axis_lim)

            single_lead_lim = (
                "Single-lead recording cannot evaluate electrical axis, chamber hypertrophy, "
                "transmural ischemia, or localize myocardial infarctions, and cannot replace "
                "a standard diagnostic 12-lead ECG."
            )
            if not any("cannot replace" in lim.lower() for lim in self.limitations):
                self.limitations.append(single_lead_lim)

        elif not lead_classification["is_confirmed_12_lead"]:
            # Undetermined lead setup: preserve lead setup uncertainty
            self.electrical_axis = None
            unc = (
                "Recording device lead configuration is undetermined; findings cannot be "
                "interpreted as a full 12-lead ECG or used to calculate frontal plane electrical axis."
            )
            if not any("undetermined" in u.lower() for u in self.uncertainties):
                self.uncertainties.append(unc)

        # 2. If unreadable quality, strip speculative numeric measurements
        if (self.recording_quality or "").lower() in ("unreadable", EcgRecordingQuality.UNREADABLE.value):
            for field in ("heart_rate_bpm", "pr_interval_ms", "qrs_duration_ms", "qt_interval_ms", "qtc_interval_ms", "electrical_axis"):
                val = getattr(self, field, None)
                if val is not None:
                    if field not in self.raw_unresolved_metrics:
                        self.raw_unresolved_metrics[field] = {
                            "raw_value": val,
                            "reason": "Tracing quality is unreadable; numeric value cannot be verified.",
                        }
                    setattr(self, field, None)

            unreadable_note = "ECG tracing is unreadable or heavily corrupted by noise."
            if not any("unreadable" in u.lower() for u in self.uncertainties):
                self.uncertainties.append(unreadable_note)

        return self


class EcgVisionIntakeClientResponse(BaseModel):
    """
    Safe client-facing confirmation of electrocardiogram Vision processing.
    Never exposes raw Gemini JSON, internal agent reasoning, or raw tracing images.
    Provides cautionary emergency guidance if acute warnings are detected.
    """
    model_config = ConfigDict(extra="forbid")

    cycle_id: str
    status: str = "processed"
    intake_version: int = Field(..., ge=1)
    recording_type: str
    recording_quality: str
    created_at_utc: str
    primary_consilium_concluded: bool = False
    privacy_notice: str = (
        "Electrocardiogram securely accepted and analyzed on the server under server_only privacy level. "
        "Clinical interpretation, rhythm evaluation, and cardiac findings are synthesized exclusively "
        "during the medical consilium."
    )


# ==============================================================================
# STAGE 8A.2 — REFERRAL DOCUMENT GENERATION SCHEMAS (FLASH -> PRO)
# ==============================================================================

REFERRAL_DOCUMENT_CANONICAL_TITLE: str = "AEGIS — HEALTH OBSERVATION SUMMARY FOR YOUR DOCTOR"

_FORBIDDEN_DIAGNOSTIC_OVERREACH_PATTERNS = (
    re.compile(r"(?i)\bofficial\s+(?:medical|hospital|clinical)\s+discharge\s+summary\b"),
    re.compile(r"(?i)\bconfirmed\s+(?:medical|clinical|definitive)\s+diagnosis\b"),
    re.compile(r"(?i)\bdefinitive\s+diagnosis\s+(?:established|confirmed)\b"),
)


def _contains_unnegated_diagnostic_overreach(text: str) -> bool:
    """
    Returns True if text claims to be an official discharge summary or a confirmed
    clinical diagnosis without a negating qualifier in the surrounding clause.
    """
    if not text:
        return False
    for pattern in _FORBIDDEN_DIAGNOSTIC_OVERREACH_PATTERNS:
        for match in pattern.finditer(text):
            start = max(0, match.start() - 250)
            prefix = text[start:match.start()].lower()
            c_start = max(prefix.rfind("."), prefix.rfind(";"), prefix.rfind("\n"), 0)
            clause = prefix[c_start:] if c_start >= 0 else prefix
            if not any(
                neg in clause
                for neg in ("not ", "not an ", "not a ", "neither ", "never ", "no ", "does not ", "is not ", "nor ", "without ", "cannot ", "n't ")
            ):
                return True
    return False


class ReferralClinicalFactItem(BaseModel):
    """
    Single attributed clinical fact or patient-reported statement extracted by
    Gemini Flash Clinical History Extractor from server-authoritative records.
    """
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    date_or_period: str = Field(..., min_length=1, max_length=120)
    category: Literal[
        "anamnesis",
        "laboratory",
        "ecg_or_exam",
        "vitals_measurement",
        "wellness_summary",
        "mode2_episode",
        "patient_reported_symptom",
        "chief_physician_conclusion",
    ]
    finding: str = Field(..., min_length=1)
    value_and_unit: Optional[str] = Field(default=None)
    source_attribution: str = Field(..., min_length=1)
    is_patient_self_reported: bool = Field(default=False)


class ReferralClinicalHistoryExtraction(BaseModel):
    """
    Structured output of Step 1 (Gemini Flash — Clinical History Extractor) in the
    Stage 8A.2 Referral Document cascade.
    Strictly preserves dates, exact units, source attributions, and missing-data gaps.
    """
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    observation_period_start: str = Field(default="", max_length=64)
    observation_period_end: str = Field(default="", max_length=64)
    observation_period_summary: str = Field(default="")
    anamnesis_and_baseline_facts: List[ReferralClinicalFactItem] = Field(default_factory=list)
    objective_measurements_and_labs: List[ReferralClinicalFactItem] = Field(default_factory=list)
    ecg_and_examination_findings: List[ReferralClinicalFactItem] = Field(default_factory=list)
    patient_reported_complaints: List[ReferralClinicalFactItem] = Field(default_factory=list)
    wellness_and_mode2_events: List[ReferralClinicalFactItem] = Field(default_factory=list)
    observed_changes_and_recurring_patterns: List[str] = Field(default_factory=list)
    prior_chief_physician_conclusions: List[str] = Field(default_factory=list)
    approved_referral_reason_summary: str = Field(default="")
    data_gaps_and_uncertainties: List[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _normalize_extraction_payload(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data

        def _str_list(items: Any) -> List[str]:
            if not isinstance(items, list):
                return []
            res = []
            for it in items:
                if isinstance(it, str):
                    res.append(it)
                elif isinstance(it, dict):
                    parts = [f"{k}: {v}" for k, v in it.items() if v is not None]
                    res.append(", ".join(parts))
                else:
                    res.append(str(it))
            return res

        for list_field in (
            "observed_changes_and_recurring_patterns",
            "prior_chief_physician_conclusions",
            "data_gaps_and_uncertainties",
        ):
            if list_field in data:
                data[list_field] = _str_list(data[list_field])

        if not data.get("approved_referral_reason_summary"):
            ref_ctx = data.get("referral_context") or {}
            reason = (
                ref_ctx.get("clinical_reason")
                or ref_ctx.get("reason")
                or ref_ctx.get("approved_referral_reason_summary")
                or data.get("observation_period_summary")
                or "Clinical evaluation recommended by medical consilium."
            )
            data["approved_referral_reason_summary"] = str(reason)

        obs_p = data.get("observation_period") or {}
        if not data.get("observation_period_start") and obs_p.get("start_date_utc"):
            data["observation_period_start"] = str(obs_p["start_date_utc"])
        if not data.get("observation_period_end") and obs_p.get("end_date_utc"):
            data["observation_period_end"] = str(obs_p["end_date_utc"])
        if not data.get("observation_period_summary"):
            data["observation_period_summary"] = str(obs_p.get("summary") or "Observation period completed.")

        return data


class ReferralDoctorDocumentOutput(BaseModel):
    """
    Structured output of Step 2 (Gemini Pro — Doctor Observation Document Author)
    in the Stage 8A.2 Referral Document cascade.
    Produces an English informational health observation summary for discussion
    with a licensed clinician across 7 mandatory sections.
    """
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    document_title: str = Field(
        default=REFERRAL_DOCUMENT_CANONICAL_TITLE,
        min_length=1,
        max_length=200,
    )
    informational_disclaimer: str = Field(..., min_length=20)
    recommended_specialties: List[str] = Field(..., min_length=1)
    consultation_urgency: Literal["routine", "priority"]
    executive_summary_for_patient: str = Field(..., min_length=10)

    # Mandatory 7 clinical observation sections:
    patient_and_observation_period: str = Field(..., min_length=10)
    reason_for_in_person_consultation: str = Field(..., min_length=10)
    objective_measurements_and_examination_results: List[str] = Field(..., min_length=1)
    timeline_of_reported_symptoms_and_changes: List[str] = Field(..., min_length=1)
    important_patterns_and_unresolved_concerns: List[str] = Field(..., min_length=1)
    ai_observations_requiring_verification: List[str] = Field(..., min_length=1)
    questions_and_examinations_for_clinician: List[str] = Field(..., min_length=1)

    @model_validator(mode="before")
    @classmethod
    def _pre_normalize_doc_fields(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        d = dict(data)

        # 1. Provide defaults for missing fields if omitted by the LLM
        if not d.get("recommended_specialties"):
            d["recommended_specialties"] = ["Cardiologist / Lipidologist", "Pulmonologist / Internist (General Practitioner)"]
        elif isinstance(d["recommended_specialties"], str):
            d["recommended_specialties"] = [s.strip() for s in d["recommended_specialties"].split(",") if s.strip()]

        if not d.get("consultation_urgency"):
            d["consultation_urgency"] = "routine"

        if not d.get("executive_summary_for_patient"):
            reason = str(d.get("reason_for_in_person_consultation") or "")
            d["executive_summary_for_patient"] = reason[:500] if reason else "Planned consultation recommended based on multi-system observation."

        if not d.get("informational_disclaimer"):
            d["informational_disclaimer"] = (
                "This document is an informational health observation summary prepared by the Aegis Health Observation System "
                "solely to assist discussion between the patient and a licensed healthcare professional. It is not an official medical "
                "discharge summary, prescription, or confirmed diagnosis."
            )

        # 2. Normalize string inputs to list for the 5 list sections
        list_section_names = (
            "objective_measurements_and_examination_results",
            "timeline_of_reported_symptoms_and_changes",
            "important_patterns_and_unresolved_concerns",
            "ai_observations_requiring_verification",
            "questions_and_examinations_for_clinician",
        )
        for sec in list_section_names:
            val = d.get(sec)
            if isinstance(val, str):
                lines = [line.strip() for line in val.split("\n") if line.strip() and len(line.strip()) >= 3]
                d[sec] = lines if lines else [val.strip()]
            elif isinstance(val, list):
                cleaned = []
                for item in val:
                    if isinstance(item, str):
                        cleaned.append(item.strip())
                    elif isinstance(item, dict):
                        parts = [f"{k}: {v}" for k, v in item.items() if v is not None]
                        cleaned.append(", ".join(parts))
                    elif item is not None:
                        cleaned.append(str(item).strip())
                d[sec] = cleaned if cleaned else ["Standard clinical observation noted."]
            elif val is None:
                d[sec] = ["Standard clinical observation noted."]

        return d

    @field_validator("consultation_urgency", mode="before")
    @classmethod
    def _normalize_doc_urgency(cls, v: Any) -> str:
        norm = str(v or "").strip().lower()
        if norm in ("routine", "priority"):
            return norm
        if any(w in norm for w in ("prior", "urgent", "high")):
            return "priority"
        if any(w in norm for w in ("rout", "plan", "standard")):
            return "routine"
        return norm

    @model_validator(mode="after")
    def _validate_document_integrity_and_safety(self) -> "ReferralDoctorDocumentOutput":
        norm_title = re.sub(r"\s*[-–—]\s*", " — ", self.document_title.strip()).upper()
        if norm_title != REFERRAL_DOCUMENT_CANONICAL_TITLE:
            raise ValueError(
                f"Invalid document_title '{self.document_title}': must be '{REFERRAL_DOCUMENT_CANONICAL_TITLE}'."
            )
        object.__setattr__(self, "document_title", REFERRAL_DOCUMENT_CANONICAL_TITLE)

        clean_specs = [s.strip() for s in self.recommended_specialties if s and s.strip()]
        if not clean_specs:
            raise ValueError("recommended_specialties must contain at least one non-empty clinician specialty.")
        object.__setattr__(self, "recommended_specialties", clean_specs)

        list_section_names = (
            "objective_measurements_and_examination_results",
            "timeline_of_reported_symptoms_and_changes",
            "important_patterns_and_unresolved_concerns",
            "ai_observations_requiring_verification",
            "questions_and_examinations_for_clinician",
        )
        for sec_name in list_section_names:
            items = getattr(self, sec_name)
            cleaned = []
            for it in (items or []):
                if isinstance(it, str) and len(it.strip()) >= 3:
                    cleaned.append(it.strip())
                elif isinstance(it, dict):
                    parts = [f"{k}: {v}" for k, v in it.items() if v is not None]
                    cleaned.append(", ".join(parts))
                elif it is not None and len(str(it).strip()) >= 3:
                    cleaned.append(str(it).strip())
            if not cleaned:
                cleaned = ["Standard clinical observation noted."]
            object.__setattr__(self, sec_name, cleaned)

        all_text_blocks = [
            self.informational_disclaimer,
            self.executive_summary_for_patient,
            self.patient_and_observation_period,
            self.reason_for_in_person_consultation,
            *self.objective_measurements_and_examination_results,
            *self.timeline_of_reported_symptoms_and_changes,
            *self.important_patterns_and_unresolved_concerns,
            *self.ai_observations_requiring_verification,
            *self.questions_and_examinations_for_clinician,
        ]
        combined_text = "\n".join(all_text_blocks)
        if _contains_unnegated_diagnostic_overreach(combined_text):
            raise ValueError(
                "Referral document violates medical safety boundary: must not claim to be an "
                "official discharge summary or a confirmed medical diagnosis."
            )

        return self

    def render_english_markdown(self) -> str:
        """
        Renders the authoritative English structured document with all 7 mandatory
        sections and explicit non-diagnostic informational framing.
        """
        specs_str = ", ".join(self.recommended_specialties)
        urgency_label = (
            "Priority outpatient evaluation recommended"
            if self.consultation_urgency == "priority"
            else "Routine planned outpatient evaluation recommended"
        )

        def _bullets(items: List[str]) -> str:
            return "\n".join(f"- {item}" for item in items)

        return (
            f"# {REFERRAL_DOCUMENT_CANONICAL_TITLE}\n\n"
            f"> **Informational Observation Notice:** {self.informational_disclaimer}\n"
            f"> *(This document is an AI-generated health observation summary prepared solely to facilitate "
            f"discussion with a licensed healthcare professional. It is NOT an official medical discharge "
            f"summary and does NOT represent a confirmed clinical diagnosis.)*\n\n"
            f"- **Recommended Clinician Specialties:** {specs_str}\n"
            f"- **Recommended Timing:** {urgency_label} (`{self.consultation_urgency}`)\n\n"
            f"## 1. Patient and Observation Period\n"
            f"{self.patient_and_observation_period}\n\n"
            f"## 2. Reason for Recommending an In-Person Consultation\n"
            f"{self.reason_for_in_person_consultation}\n\n"
            f"## 3. Objective Health Measurements and Examination Results\n"
            f"{_bullets(self.objective_measurements_and_examination_results)}\n\n"
            f"## 4. Timeline of Reported Symptoms and Observed Changes\n"
            f"{_bullets(self.timeline_of_reported_symptoms_and_changes)}\n\n"
            f"## 5. Important Patterns and Unresolved Concerns\n"
            f"{_bullets(self.important_patterns_and_unresolved_concerns)}\n\n"
            f"## 6. AI-Generated Observations Requiring Professional Verification\n"
            f"{_bullets(self.ai_observations_requiring_verification)}\n\n"
            f"## 7. Questions and Possible Examinations to Discuss with a Licensed Clinician\n"
            f"{_bullets(self.questions_and_examinations_for_clinician)}\n"
        )

