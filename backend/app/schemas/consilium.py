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
