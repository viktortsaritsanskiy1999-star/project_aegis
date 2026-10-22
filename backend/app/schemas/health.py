from datetime import datetime, timezone, timedelta
from enum import Enum
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field, field_validator, model_validator


class HealthResponse(BaseModel):
    """Health check payload showing server status and database connectivity."""
    status: str = Field(default="ok", description="Server operational status")
    app_name: str = Field(description="Name of the backend service")
    app_version: str = Field(description="Backend semantic version")
    environment: str = Field(description="Operating environment")
    database: str = Field(description="Database connectivity status")
    timestamp: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="Current UTC timestamp",
    )


class HealthDataSource(str, Enum):
    """Explicit validated sources for biometric health measurements."""
    WATCH_SENSOR = "watch_sensor"
    MANUAL_INPUT = "manual_input"
    TEST_CHIP = "test_chip"
    SIMULATED = "simulated"
    UNKNOWN = "unknown"


# Technical physiological bounds (equipment measurement admissibility limits; no clinical alert thresholds)
PULSE_MIN = 20
PULSE_MAX = 300
SYSTOLIC_MIN = 40
SYSTOLIC_MAX = 300
DIASTOLIC_MIN = 20
DIASTOLIC_MAX = 200
SPO2_MIN = 50
SPO2_MAX = 100


class HealthMeasurementCreate(BaseModel):
    """
    Schema for creating or submitting a single biometric health measurement.
    Enforces strict technical admissibility ranges and timestamp sanity without
    imposing artificial clinical diagnostic thresholds.
    """
    cycle_id: Optional[str] = Field(
        default=None,
        min_length=3,
        max_length=64,
        description="Target medical cycle identifier (required unless specified in batch envelope)",
    )
    measured_at: datetime = Field(
        ...,
        description="Authoritative measurement timestamp (UTC or timezone-aware)",
    )
    source: HealthDataSource = Field(
        default=HealthDataSource.WATCH_SENSOR,
        description="Origin of measurement: watch_sensor, manual_input, test_chip, simulated, unknown",
    )
    pulse: Optional[int] = Field(
        default=None,
        ge=PULSE_MIN,
        le=PULSE_MAX,
        description="Heart rate in beats per minute (technical equipment range 20-300)",
    )
    systolic: Optional[int] = Field(
        default=None,
        ge=SYSTOLIC_MIN,
        le=SYSTOLIC_MAX,
        description="Systolic blood pressure in mmHg (technical range 40-300)",
    )
    diastolic: Optional[int] = Field(
        default=None,
        ge=DIASTOLIC_MIN,
        le=DIASTOLIC_MAX,
        description="Diastolic blood pressure in mmHg (technical range 20-200)",
    )
    spo2: Optional[int] = Field(
        default=None,
        ge=SPO2_MIN,
        le=SPO2_MAX,
        description="Blood oxygen saturation percentage (technical range 50-100)",
    )
    session_id: Optional[str] = Field(
        default=None,
        max_length=64,
        description="Optional workout session identifier",
    )
    is_simulated: bool = Field(
        default=False,
        description="Flag indicating simulated or synthetic test data",
    )
    is_historical: Optional[bool] = Field(
        default=None,
        description="Flag indicating historical record; historical records are excluded from live vitals stream",
    )
    phase: str = Field(
        default="standby",
        max_length=64,
        description="Operational phase (standby, historical, workout, recovery, etc.)",
    )
    note: str = Field(
        default="",
        max_length=255,
        description="Optional note describing the measurement",
    )
    raw_metadata: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Raw device or client metadata (can contain idempotency_key)",
    )

    @field_validator("measured_at", mode="after")
    @classmethod
    def validate_timestamp_sanity(cls, dt: datetime) -> datetime:
        now_utc = datetime.now(timezone.utc)
        target_dt = dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)
        # Clock skew tolerance: allow up to 5 minutes into the future
        if target_dt > now_utc + timedelta(minutes=5):
            raise ValueError(
                f"measured_at timestamp '{dt.isoformat()}' cannot be in the future (server UTC: {now_utc.isoformat()})."
            )
        if target_dt.year < 1970 or target_dt.year > 2100:
            raise ValueError(f"measured_at year {target_dt.year} is outside acceptable range (1970-2100).")
        return target_dt

    @model_validator(mode="after")
    def validate_metrics_presence_and_consistency(self) -> "HealthMeasurementCreate":
        if (
            self.pulse is None
            and self.systolic is None
            and self.diastolic is None
            and self.spo2 is None
        ):
            raise ValueError("At least one biometric metric (pulse, systolic, diastolic, spo2) must be provided.")

        if self.systolic is not None and self.diastolic is not None:
            if self.systolic < self.diastolic:
                raise ValueError(
                    f"Systolic blood pressure ({self.systolic} mmHg) cannot be lower than diastolic ({self.diastolic} mmHg)."
                )
        return self


class HealthMeasurementCreateBatch(BaseModel):
    """
    Schema for batch ingestion of biometric measurements (e.g. smartwatch batch sync or historical import).
    """
    cycle_id: str = Field(
        ...,
        min_length=3,
        max_length=64,
        description="Authoritative medical cycle for the batch",
    )
    session_id: Optional[str] = Field(
        default=None,
        max_length=64,
        description="Default session_id for all measurements in batch (if omitted per-item)",
    )
    as_historical: Optional[bool] = Field(
        default=None,
        description="If True, overrides is_historical=True on all items in batch; if False, enforces is_historical=False",
    )
    measurements: List[HealthMeasurementCreate] = Field(
        ...,
        min_length=1,
        max_length=5000,
        description="List of measurements to ingest atomically",
    )


class HealthMeasurementResponse(BaseModel):
    """Client-facing response for an individual health measurement."""
    id: int
    cycle_id: str
    session_id: Optional[str] = None
    pulse: Optional[int] = None
    systolic: Optional[int] = None
    diastolic: Optional[int] = None
    spo2: Optional[int] = None
    source: str
    is_simulated: bool
    is_historical: bool
    measured_at: datetime
    ingested_at: datetime
    phase: str
    note: str
    status: str = Field(
        default="recorded",
        description="Processing status: 'recorded' (new) or 'deduplicated' (already present)",
    )


class HealthMeasurementBatchResponse(BaseModel):
    """Client-facing response for batch ingestion."""
    cycle_id: str
    total_received: int
    recorded_count: int
    deduplicated_count: int
    items: List[HealthMeasurementResponse]


class VitalsStreamResponse(BaseModel):
    """
    Client-facing representation of the authoritative latest vitals for a medical cycle.
    Provides backward-compatible payload with latest_measured_at and safe semantics of
    unverified/unknown live actuality until an explicit monitoring policy is established.
    """
    cycle_id: str
    blood_pressure: str = Field(default="--/-- mmHg")
    heart_rate: str = Field(default="-- bpm")
    spo2: str = Field(default="--%")
    latest_measured_at: Optional[datetime] = None
    source: Optional[str] = None
    is_live: Optional[bool] = Field(
        default=None,
        description="True if actively streaming live, False if no data, None/null if actuality is unknown without a monitoring policy",
    )
    recency_status: str = Field(
        default="unknown",
        description="Safe telemetry actuality status: 'no_data' or 'unknown'",
    )
