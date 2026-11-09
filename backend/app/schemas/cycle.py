from datetime import datetime
from pydantic import BaseModel, Field, ConfigDict


class MedicalCycleCreate(BaseModel):
    cycle_id: str = Field(..., min_length=3, max_length=64, description="Unique medical cycle identifier")
    status: str = Field(default="active", pattern="^(active|completed|archived|pending_reassessment)$")
    patient_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=64,
        description="Optional patient identifier owning this medical cycle",
    )


class MedicalCycleStatusUpdate(BaseModel):
    status: str = Field(..., pattern="^(active|completed|archived|pending_reassessment)$")


class MedicalCycleResponse(BaseModel):
    cycle_id: str
    patient_id: str | None = None
    status: str
    started_at: datetime
    closed_at: datetime | None = None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class StartNeoReassessmentResponse(BaseModel):
    """
    Response schema for client-initiated Neo cycle transition.
    Patient identity is authoritative from session; client never provides patient_id.
    """
    patient_id: str = Field(..., description="Authoritative patient identifier")
    cycle_id: str = Field(..., description="Newly provisioned reassessment cycle identifier")
    previous_cycle_id: str = Field(..., description="Previous cycle identifier transitioned to completed")
    status: str = Field(default="pending_reassessment", description="Current status of the new cycle")
    reassessment_status: str = Field(default="pending_reassessment", description="Sub-status for reassessment intake")
    is_pending_reassessment: bool = Field(default=True, description="Flag indicating cycle is in pending reassessment")
    created_at_utc: str = Field(..., description="Authoritative ISO UTC timestamp of transition")
    message: str = Field(default="Neo reassessment cycle successfully provisioned and linked to previous cycle.")

    model_config = ConfigDict(from_attributes=True)
