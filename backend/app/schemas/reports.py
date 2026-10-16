from datetime import datetime
from typing import Any, Dict, List
from pydantic import BaseModel, Field, ConfigDict, field_validator, model_validator
from app.schemas.privacy import PrivacyLevel


class ClientReportContent(BaseModel):
    """
    Validated structure for user-facing client reports.
    Rejects raw arbitrary nested clinical structures.
    """
    title: str = Field(..., min_length=1, max_length=200)
    summary: str = Field(..., min_length=1)
    highlights: List[str] = Field(default_factory=list)
    metrics: Dict[str, Any] = Field(default_factory=dict)


class ClientModelReportResponse(BaseModel):
    """
    Safe client-facing response schema for model reports.
    Strictly forbids and rejects server_only reports at the schema level.
    """
    report_id: str
    cycle_id: str
    sprint_id: str | None = None
    session_id: str | None = None
    model: str
    report_type: str
    privacy_level: PrivacyLevel
    version: int
    content: ClientReportContent | Dict[str, Any]
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

    model_config = ConfigDict(from_attributes=True)


class ModelReportCreate(BaseModel):
    """Internal server schema for creating model reports."""
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
