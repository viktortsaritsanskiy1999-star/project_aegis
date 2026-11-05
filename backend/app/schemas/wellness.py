from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, ConfigDict, Field


class WellnessStatus(str, Enum):
    """Execution status of the daily Wellness analysis."""
    READY = "ready"
    NO_DATA = "no_data"


class WellnessTriggerType(str, Enum):
    """Trigger source that initiated daily Wellness analysis."""
    WAKE_UP = "wake_up"
    SCHEDULED_MIDDAY = "scheduled_midday"
    MANUAL = "manual"


class PatientTimezoneSetting(BaseModel):
    """Explicitly configured patient timezone setting."""
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    cycle_id: str
    patient_id: str
    timezone: str = "UTC"
    updated_at_utc: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


class WellnessTimeWindow(BaseModel):
    """Temporal boundaries of the analyzed wellness period."""
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    start_utc: str
    end_utc: str
    user_timezone: str = "UTC"
    calendar_week: int = Field(default=1, ge=1)
    calendar_day: int = Field(default=1, ge=1, le=7)


class HeartRateSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")
    min_bpm: int
    avg_bpm: float
    max_bpm: int
    sample_count: int


class BloodPressureSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")
    min_systolic: int
    avg_systolic: float
    max_systolic: int
    min_diastolic: int
    avg_diastolic: float
    max_diastolic: int
    sample_count: int


class SpO2Summary(BaseModel):
    model_config = ConfigDict(extra="forbid")
    min_spo2: int
    avg_spo2: float
    max_spo2: int
    sample_count: int


class SleepPhasesSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")
    deep_hours: Optional[float] = None
    rem_hours: Optional[float] = None
    light_hours: Optional[float] = None
    awake_hours: Optional[float] = None


class SleepSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sleep_hours: float
    sleep_start_utc: Optional[str] = None
    sleep_end_utc: Optional[str] = None
    phases: Optional[SleepPhasesSummary] = None
    quality: Optional[str] = None
    is_confirmed_wake_up: bool = False


class ActivitySummary(BaseModel):
    model_config = ConfigDict(extra="forbid")
    step_count: Optional[int] = None
    activity_minutes: Optional[int] = None


class WellnessBiometricsSummary(BaseModel):
    """Factual summary of raw physical measurements during the window."""
    model_config = ConfigDict(extra="forbid")

    heart_rate: Optional[HeartRateSummary] = None
    blood_pressure: Optional[BloodPressureSummary] = None
    spo2: Optional[SpO2Summary] = None
    sleep: Optional[SleepSummary] = None
    activity: Optional[ActivitySummary] = None
    sources_present: List[str] = Field(default_factory=list)
    has_manual_input: bool = False
    has_sensor_data: bool = False
    total_measurements: int = 0
    questionable_datapoints_count: int = 0


class WellnessModelOutput(BaseModel):
    """Structured Gemini Flash clinical interpretation output."""
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    observed_trends: List[str] = Field(default_factory=list)
    limitations_and_gaps: List[str] = Field(default_factory=list)
    questionable_measurements: List[str] = Field(default_factory=list)
    summary_for_athena: str = Field(
        ...,
        description="Concise factual & clinical summary for ingest into Section B of Athena Daily Journal",
    )


class WellnessDailyReport(BaseModel):
    """
    Durable, versioned daily Wellness report persisted as SERVER_ONLY in model_reports.
    Strictly separates verified measured facts from Gemini Flash model interpretations.
    """
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    report_id: str
    cycle_id: str
    patient_id: str
    calendar_week: int = Field(default=1, ge=1)
    calendar_day: int = Field(default=1, ge=1, le=7)
    status: WellnessStatus
    trigger: WellnessTriggerType
    window: WellnessTimeWindow
    factual_summary: WellnessBiometricsSummary
    interpretation: Optional[WellnessModelOutput] = None
    summary: Optional[str] = None  # Compatibility helper for AthenaDailyJournal discovery
    assessment: Optional[str] = None  # Compatibility helper for AthenaDailyJournal discovery
    created_at_utc: str
