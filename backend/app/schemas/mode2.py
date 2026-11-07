from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, ConfigDict, Field


class Mode2WorkoutContext(str, Enum):
    """Context of physical exertion during Mode 2 pulse episode."""
    UNKNOWN = "unknown"
    CONFIRMED_WORKOUT = "confirmed_workout"
    NOT_WORKING_OUT = "not_working_out"


class Mode2EpisodeStatus(str, Enum):
    """Lifecycle status of a Mode 2 pulse episode."""
    ACTIVE = "active"
    CLOSED = "closed"


class Mode2TelemetryPoint(BaseModel):
    """Single telemetry measurement point recorded during an active Mode 2 episode."""
    model_config = ConfigDict(extra="forbid")

    timestamp_utc: str
    pulse: int = Field(..., ge=20, le=300)
    spo2: Optional[int] = Field(default=None, ge=50, le=100)
    systolic: Optional[int] = Field(default=None, ge=40, le=300)
    diastolic: Optional[int] = Field(default=None, ge=20, le=200)
    source: str = "watch_sensor"
    phase: Optional[str] = None
    is_simulated: bool = False
    note: Optional[str] = None


class Mode2UserStatement(BaseModel):
    """User statement recorded verbatim with timestamp during an active Mode 2 episode."""
    model_config = ConfigDict(extra="forbid")

    text: str = Field(..., min_length=1)
    timestamp_utc: str
    is_workout_confirmation: Optional[bool] = None
    symptoms: Optional[str] = None


class Mode2AthenaEvent(BaseModel):
    """Athena event or interaction recorded during an active Mode 2 episode."""
    model_config = ConfigDict(extra="forbid")

    event_type: str = Field(..., description="initial_alert, two_minute_summary, significant_change, sustained_normalization, user_statement_reply")
    timestamp_utc: str
    prompt_summary: str
    athena_reply: str
    emergency_trigger: bool = False


class Mode2EpisodeRecord(BaseModel):
    """
    Durable server-side Mode 2 pulse episode record.
    Stored under report_type='athena_mode2_episode', SERVER_ONLY in model_reports.
    Guarantees full history preservation, resilience across server restarts,
    and strict cycle isolation.
    """
    model_config = ConfigDict(extra="forbid")

    episode_id: str = Field(..., description="Unique episode identifier, e.g. 'ep_mode2_{cycle_id}_{uuid}'")
    cycle_id: str
    patient_id: str
    session_id: Optional[str] = Field(default=None, description="Linked workout session identifier")
    status: Mode2EpisodeStatus = Mode2EpisodeStatus.ACTIVE
    started_at_utc: str
    closed_at_utc: Optional[str] = None

    # Baseline & Triggering Measurement
    baseline_pulse: int = Field(..., ge=20, le=250)
    trigger_pulse: int = Field(..., ge=20, le=300)
    trigger_spo2: Optional[int] = Field(default=None, ge=50, le=100)
    trigger_source: str = "watch_sensor"
    trigger_reason: str = Field(..., min_length=1)

    # Aggregated Vitals Metrics
    min_pulse: int = Field(..., ge=20, le=300)
    max_pulse: int = Field(..., ge=20, le=300)
    latest_pulse: int = Field(..., ge=20, le=300)
    latest_spo2: Optional[int] = Field(default=None, ge=50, le=100)

    # Active thresholds applied
    thresholds_applied: Dict[str, Any] = Field(default_factory=dict)

    # Workout context & verbatim statements
    workout_context: Mode2WorkoutContext = Mode2WorkoutContext.UNKNOWN
    user_statements: List[Mode2UserStatement] = Field(default_factory=list)

    # Full Telemetry History & Athena Interactions
    telemetry_samples: List[Mode2TelemetryPoint] = Field(default_factory=list)
    athena_events: List[Mode2AthenaEvent] = Field(default_factory=list)

    # Telemetry Filtering & Normalization State Tracking
    last_notified_at_utc: Optional[str] = None
    last_notified_pulse: Optional[int] = None
    last_notified_spo2: Optional[int] = None
    initial_two_minute_update_sent: bool = False
    consecutive_normal_readings: int = 0


class Mode2WorkoutResponseRequest(BaseModel):
    """Client request to clarify physical activity / workout during Mode 2."""
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    cycle_id: str = Field(..., min_length=3, max_length=64)
    is_workout: bool
    user_message: Optional[str] = Field(default=None, max_length=2000)
    symptoms: Optional[str] = Field(default=None, max_length=2000)


class Mode2WorkoutFinishRequest(BaseModel):
    """Client request to finish workout and transition to recovery."""
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    cycle_id: Optional[str] = Field(default=None, min_length=3, max_length=64)
    session_id: Optional[str] = Field(default=None, max_length=64)


class Mode2EpisodeSummaryResponse(BaseModel):
    """Sanitized client-facing summary of a Mode 2 episode."""
    model_config = ConfigDict(extra="forbid")

    episode_id: str
    cycle_id: str
    session_id: Optional[str] = None
    status: str
    started_at_utc: str
    closed_at_utc: Optional[str] = None
    baseline_pulse: int
    trigger_pulse: int
    min_pulse: int
    max_pulse: int
    latest_pulse: int
    latest_spo2: Optional[int] = None
    workout_context: str
    samples_count: int
    events_count: int
    user_statements_count: int
    last_athena_reply: Optional[str] = None
