import re
from enum import Enum
from typing import Any, Dict, List, Optional
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


class AegisAgentRole(str, Enum):
    """
    Distinct logical AI agent roles in Project Aegis.
    Separates the logical role of an agent from the physical Gemini model name
    (since multiple independent modules share 'gemini-3.6-flash').
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

    # Card 2: Dimensions
    height: int = Field(..., ge=50, le=260)
    weight: int = Field(..., ge=30, le=350)

    # Card 3: Nicotine
    nicotine_status: str = Field(..., min_length=1, max_length=128)
    nicotine_years: Optional[str] = Field(default=None, max_length=128)
    nicotine_freq: Optional[str] = Field(default=None, max_length=128)

    # Card 4: Alcohol
    alcohol_status: str = Field(..., min_length=1, max_length=128)
    alcohol_type: Optional[str] = Field(default=None, max_length=128)

    # Card 5: Specific Factors
    spec_factors: str = Field(..., min_length=1, max_length=512)

    # Card 6: Sleep
    sleep_quality: str = Field(..., min_length=1, max_length=128)

    # Card 7: Diet Types
    diet_types: str = Field(..., min_length=1, max_length=512)

    # Card 8: Dietary Habits
    diet_habits: str = Field(..., min_length=1, max_length=512)

    # Card 9: GI Signals
    gi_signals: str = Field(..., min_length=1, max_length=512)

    # Card 10 & 10.1: Work Mode & Sport
    work_type: str = Field(..., min_length=1, max_length=128)
    work_schedule: Optional[str] = Field(default=None, max_length=128)
    sport_level: Optional[str] = Field(default=None, max_length=128)

    # Card 11: Chronic Conditions
    chronic_diseases: str = Field(..., min_length=1, max_length=1024)
    onco_status: Optional[str] = Field(default=None, max_length=128)

    # Card 12: Recent Illness
    recent_illness: str = Field(..., min_length=1, max_length=512)

    # Card 13: Screening & Safe Space
    critical_infections: str = Field(..., min_length=1, max_length=512)

    # Card 14: Heredity
    heredity_risks: str = Field(..., min_length=1, max_length=1024)

    # Card 15: Medications & Supplements
    medications: str = Field(..., min_length=1, max_length=1024)

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


class WeeklyProMasterOutput(BaseModel):
    """
    Structured output contract for Weekly Chief Physician (Pro):
    - updated_weekly_strategy (server_only practical 7-day strategy & priorities for Athena;
      if no change is required, explicitly confirms continuation of the previous strategy)
    - strategy_status ('updated' or 'continued')
    - weekly_overall_report (client_facing weekly overall summary for the Client Vault)
    - flash_quality_review (quality control & deterministic corrections for Flash's 8 weekly system updates)
    """
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    updated_weekly_strategy: str = Field(..., min_length=1)
    strategy_status: str = Field(default="updated", min_length=1)
    weekly_overall_report: ProUserOverallReport
    flash_quality_review: ProFlashQualityReview = Field(default_factory=ProFlashQualityReview)

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
    timestamp: Optional[str] = Field(default=None, max_length=64)
    sprint_day: Optional[int] = Field(default=None, ge=1, le=7)


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
    recorded_at: str = Field(..., min_length=1, max_length=64)


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
            try:
                return float(cleaned)
            except ValueError:
                return None
        return None

    @model_validator(mode="after")
    def validate_and_compute_verification_status(self) -> "VisionOcrMarkerItem":
        self.evaluate_verification_requirement()
        return self

    def evaluate_verification_requirement(self) -> bool:
        """
        Determines whether this marker requires manual user review before entering
        authoritative medical consilium ledgers:
        - is_uncertain is True (smudged, blurry, doubtful)
        - numeric_value is None and raw_value is not an established qualitative marker
        - quantitative number without an explicit unit of measurement
        - missing specimen collection date
        """
        if self.is_uncertain:
            self.requires_user_verification = True
            return True

        if self.numeric_value is None:
            cleaned_raw = self.raw_value.strip().lower()
            valid_qualitative = {
                "отрицательно", "положительно", "не обнаружено", "обнаружено",
                "norm", "normal", "negative", "positive", "норма", "не выявлено", "выявлено",
            }
            if cleaned_raw not in valid_qualitative:
                self.requires_user_verification = True
                if not self.uncertainty_note:
                    self.uncertainty_note = "Non-standard text value requiring client verification"
                return True
        else:
            if not self.unit or not self.unit.strip():
                self.requires_user_verification = True
                if not self.uncertainty_note:
                    self.uncertainty_note = "Missing unit of measurement for numeric value"
                return True

        if not self.sample_date or not self.sample_date.strip():
            self.requires_user_verification = True
            if not self.uncertainty_note:
                self.uncertainty_note = "Missing specimen collection date"
            return True

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
    page_count: int = Field(default=1, ge=1)
    notes: Optional[str] = Field(default=None, max_length=512)


class VisionOcrModelOutput(BaseModel):
    """
    Structured Pydantic output schema produced by Gemini Vision OCR agent.
    Captures complete laboratory indicators without hallucinations or omissions.
    """
    model_config = ConfigDict(extra="forbid")

    document_metadata: VisionOcrDocumentMetadata = Field(default_factory=VisionOcrDocumentMetadata)
    markers: List[VisionOcrMarkerItem] = Field(default_factory=list)


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
