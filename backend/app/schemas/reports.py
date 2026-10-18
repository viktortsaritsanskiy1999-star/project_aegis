from datetime import datetime
from typing import Any, Dict, List, Union
from pydantic import BaseModel, Field, ConfigDict, field_validator, model_validator
from app.schemas.privacy import PrivacyLevel

# Strictly primitive scalar types allowed inside client-facing metrics
ClientMetricScalar = Union[str, int, float, bool, None]


def _validate_scalar_metrics(v: Any) -> Dict[str, ClientMetricScalar]:
    if not isinstance(v, dict):
        raise ValueError("metrics must be a dictionary of scalar values.")
    for k, val in v.items():
        if isinstance(val, (dict, list, tuple, set)):
            raise ValueError(
                f"Nested non-scalar structure is forbidden in client metrics key '{k}'."
            )
    return v


class ClientDatedSystemEntry(BaseModel):
    """
    Strict allowlist entry for dated user-facing organ system reports (Human Silhouette).
    Forbids any extra or unapproved fields.
    """
    model_config = ConfigDict(extra="forbid")

    date: str = Field(..., min_length=1, max_length=64)
    summary: str = Field(..., min_length=1)
    has_changes: bool = Field(default=True)


class ClientUserWorkoutReport(BaseModel):
    """
    Strict allowlist structure for user-facing Workout Intelligence reports.
    Forbids internal_analysis, athena_handoff, training_shelf, and any unapproved keys.
    """
    model_config = ConfigDict(extra="forbid")

    title: str = Field(default="Workout Summary", min_length=1, max_length=200)
    summary: str = Field(..., min_length=1)
    report_date: str | None = Field(default=None, max_length=64)
    highlights: List[str] = Field(default_factory=list)
    metrics: Dict[str, ClientMetricScalar] = Field(default_factory=dict)

    @field_validator("metrics", mode="before")
    @classmethod
    def ensure_scalar_metrics_only(cls, v: Any) -> Dict[str, ClientMetricScalar]:
        return _validate_scalar_metrics(v)


class ClientReportContent(BaseModel):
    """
    Strict allowlist structure for user-facing client reports.
    Supports both flat user reports and nested {"user_workout_report": {...}} payloads.
    Only explicitly declared user-friendly fields are permitted (extra='forbid').
    Arbitrary dictionaries, nested clinical structures, and internal shelves are rejected.
    """
    model_config = ConfigDict(extra="forbid")

    title: str = Field(default="Aegis Wellness Summary", min_length=1, max_length=200)
    summary: str = Field(..., min_length=1)
    report_date: str | None = Field(default=None, max_length=64)
    system_key: str | None = Field(default=None, max_length=64)
    has_changes: bool | None = Field(default=None)
    highlights: List[str] = Field(default_factory=list)
    metrics: Dict[str, ClientMetricScalar] = Field(default_factory=dict)
    entries: List[ClientDatedSystemEntry] = Field(default_factory=list)
    user_workout_report: ClientUserWorkoutReport | None = Field(default=None)

    @model_validator(mode="before")
    @classmethod
    def normalize_workout_envelope(cls, data: Any) -> Any:
        """
        Explicitly normalizes {"user_workout_report": {...}} payloads so top-level
        summary and title are populated when omitted, while preserving user_workout_report
        and enforcing strict allowlist validation on both levels.
        """
        if not isinstance(data, dict) or "user_workout_report" not in data:
            return data

        uwr = data.get("user_workout_report")
        if not isinstance(uwr, dict):
            raise ValueError("user_workout_report must be a structured object.")

        normalized = dict(data)
        if "summary" not in normalized and "summary" in uwr:
            normalized["summary"] = uwr["summary"]
        if "title" not in normalized:
            uwr_title = uwr.get("title")
            normalized["title"] = (
                uwr_title if isinstance(uwr_title, str) and uwr_title.strip() else "Workout Summary"
            )
        return normalized

    @field_validator("metrics", mode="before")
    @classmethod
    def ensure_scalar_metrics_only(cls, v: Any) -> Dict[str, ClientMetricScalar]:
        return _validate_scalar_metrics(v)


class ClientModelReportResponse(BaseModel):
    """
    Safe client-facing response schema for model reports.
    Strictly forbids server_only reports and enforces the ClientReportContent allowlist.
    """
    model_config = ConfigDict(from_attributes=True, extra="forbid")

    report_id: str
    cycle_id: str
    sprint_id: str | None = None
    session_id: str | None = None
    model: str
    report_type: str
    privacy_level: PrivacyLevel
    version: int
    content: ClientReportContent
    created_at: datetime

    @field_validator("privacy_level", mode="before")
    @classmethod
    def validate_privacy(cls, v: Any) -> PrivacyLevel:
        if isinstance(v, str):
            return PrivacyLevel.from_str(v)
        return v

    @model_validator(mode="after")
    def ensure_client_facing_only(self) -> "ClientModelReportResponse":
        if self.privacy_level != PrivacyLevel.CLIENT_FACING:
            raise ValueError(
                f"Security Violation: Report '{self.report_id}' has privacy_level='{self.privacy_level.value}' "
                "and cannot be serialized for client delivery."
            )
        return self


class ModelReportCreate(BaseModel):
    """Internal server schema for creating model reports."""
    model_config = ConfigDict(extra="forbid")

    report_id: str = Field(..., min_length=3, max_length=64)
    cycle_id: str = Field(..., min_length=3, max_length=64)
    sprint_id: str | None = None
    session_id: str | None = None
    model: str = Field(..., min_length=1, max_length=64)
    report_type: str = Field(..., min_length=1, max_length=64)
    privacy_level: PrivacyLevel
    version: int = Field(default=1, ge=1)
    content: Dict[str, Any]

    @field_validator("privacy_level", mode="before")
    @classmethod
    def validate_privacy(cls, v: Any) -> PrivacyLevel:
        if isinstance(v, str):
            return PrivacyLevel.from_str(v)
        return v

    @model_validator(mode="after")
    def validate_client_content_allowlist(self) -> "ModelReportCreate":
        if self.privacy_level == PrivacyLevel.CLIENT_FACING:
            ClientReportContent.model_validate(self.content)
        return self
