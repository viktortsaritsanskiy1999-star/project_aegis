from datetime import datetime, timezone
from typing import List, Optional
from pydantic import BaseModel, ConfigDict, Field, field_validator


class AthenaVitalsAlertContext(BaseModel):
    """Vitals spike context for Athena curator Mode 2 intervention."""
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    current_pulse: int = Field(..., ge=30, le=250, description="Current measured peak pulse in bpm")
    baseline_pulse: int = Field(..., ge=30, le=200, description="Patient resting baseline pulse in bpm")
    measured_at: Optional[str] = Field(default=None, max_length=64)
    notes: Optional[str] = Field(default=None, max_length=500)


class AthenaChatRequest(BaseModel):
    """
    Client request payload for Athena conversation turn.
    Strictly forbids client override of patient_id or cycle_id.
    """
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    message: Optional[str] = Field(default=None, max_length=4000)
    event_type: str = Field(default="user_message", max_length=64)
    vitals_alert: Optional[AthenaVitalsAlertContext] = None
    sprint_day: Optional[int] = Field(default=None, ge=1, le=7)
    idempotency_key: Optional[str] = Field(default=None, max_length=64)

    @field_validator("event_type", mode="after")
    @classmethod
    def validate_event_type(cls, v: str) -> str:
        clean = v.strip().lower()
        allowed = {"user_message", "vitals_alert", "daily_pulse", "initial_greeting"}
        if clean not in allowed:
            raise ValueError(f"event_type must be one of: {', '.join(sorted(allowed))}")
        return clean


class AthenaChatResponse(BaseModel):
    """
    Client-facing sanitized response from Athena curator.
    Strictly contains zero internal prompts, zero raw clinical scoring,
    and zero server_only report contents.
    """
    model_config = ConfigDict(extra="forbid")

    reply: str
    emergency_trigger: bool = False
    emergency_notice: Optional[str] = None
    cycle_id: str
    sprint_day: Optional[int] = None
    message_id: str
    created_at: str


class AthenaChatMessageItem(BaseModel):
    """Single sanitized chat history turn (sender and clean text only)."""
    model_config = ConfigDict(extra="forbid")

    sender: str  # user or curator
    text: str
    timestamp: str


class AthenaChatHistoryResponse(BaseModel):
    """Sanitized conversation history for patient's active cycle."""
    model_config = ConfigDict(extra="forbid")

    cycle_id: str
    messages: List[AthenaChatMessageItem]
