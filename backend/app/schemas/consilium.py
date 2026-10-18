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
