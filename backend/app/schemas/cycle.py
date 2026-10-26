from datetime import datetime
from pydantic import BaseModel, Field, ConfigDict


class MedicalCycleCreate(BaseModel):
    cycle_id: str = Field(..., min_length=3, max_length=64, description="Unique medical cycle identifier")
    status: str = Field(default="active", pattern="^(active|completed|archived)$")
    patient_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=64,
        description="Optional patient identifier owning this medical cycle",
    )


class MedicalCycleStatusUpdate(BaseModel):
    status: str = Field(..., pattern="^(active|completed|archived)$")


class MedicalCycleResponse(BaseModel):
    cycle_id: str
    patient_id: str | None = None
    status: str
    started_at: datetime
    closed_at: datetime | None = None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)
