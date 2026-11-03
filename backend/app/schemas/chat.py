from datetime import datetime, timezone
from typing import Any, Dict, List, Literal, Optional
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
    sprint_number: Optional[int] = Field(default=None, ge=1)
    idempotency_key: Optional[str] = Field(default=None, max_length=128)

    @field_validator("event_type", mode="after")
    @classmethod
    def validate_event_type(cls, v: str) -> str:
        clean = v.strip().lower()
        allowed = {
            "user_message",
            "vitals_alert",
            "daily_pulse",
            "initial_greeting",
            "routine_answer",
            "out_of_turn_message",
            "wearable_event",
            "routine_reminder",
            "reminder",
        }
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
    sprint_number: Optional[int] = None
    message_id: str
    created_at: str
    question_id: Optional[str] = None
    routine_question_status: Optional[str] = None
    reminders_count: Optional[int] = None


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


# ==============================================================================
# STAGE A2.2: EVIDENCE-PRESERVING ATHENA DAILY JOURNAL SCHEMAS
# ==============================================================================

class PatientAnswerEntry(BaseModel):
    """Original patient answer preserved verbatim."""
    model_config = ConfigDict(extra="forbid")

    text: str = Field(description="Original patient answer preserved verbatim")
    timestamp_utc: str
    source_event_id: Optional[str] = None
    event_type: str = "routine_answer"


class RoutineQuestionJournalSection(BaseModel):
    """Section tracking whether routine question was scheduled, answered, or missing."""
    model_config = ConfigDict(extra="forbid")

    question_id: Optional[str] = None
    question_text: Optional[str] = None
    was_answered: bool = False
    answered_at_utc: Optional[str] = None
    unanswered_declaration: Optional[str] = None


class OutOfTurnPatientMessage(BaseModel):
    """Original out-of-turn patient message kept strictly separate from routine answers."""
    model_config = ConfigDict(extra="forbid")

    text: str = Field(description="Original out-of-turn message preserved verbatim")
    timestamp_utc: str
    source_event_id: Optional[str] = None
    event_type: str = "out_of_turn_message"


class PatientReportedEvidence(BaseModel):
    """Section A: Patient-reported evidence layer."""
    model_config = ConfigDict(extra="forbid")

    routine_question: RoutineQuestionJournalSection
    patient_answers: List[PatientAnswerEntry] = Field(default_factory=list)
    out_of_turn_messages: List[OutOfTurnPatientMessage] = Field(default_factory=list)
    has_patient_interaction: bool = False


class WellnessReportReference(BaseModel):
    """Reference to independently persisted server Wellness report."""
    model_config = ConfigDict(extra="forbid")

    report_id: Optional[str] = None
    report_type: Optional[str] = None
    version: Optional[int] = None
    timestamp_utc: Optional[str] = None
    is_available: bool = False
    missing_reason: Optional[str] = None


class BiometricMeasurementPoint(BaseModel):
    """Single genuine measurement point from HealthMeasurement."""
    model_config = ConfigDict(extra="forbid")

    timestamp_utc: str
    source: str
    pulse: Optional[int] = None
    systolic: Optional[int] = None
    diastolic: Optional[int] = None
    spo2: Optional[int] = None
    raw_metadata: Optional[Dict[str, Any]] = None


class BiometricsSummary(BaseModel):
    """Summary of biometric telemetry during the observation window."""
    model_config = ConfigDict(extra="forbid")

    heart_rate_summary: Optional[Dict[str, Any]] = None
    blood_pressure_summary: Optional[Dict[str, Any]] = None
    spo2_summary: Optional[Dict[str, Any]] = None
    sleep_summary: Optional[Dict[str, Any]] = None
    steps_summary: Optional[Dict[str, Any]] = None
    raw_records_count: int = 0
    records: List[BiometricMeasurementPoint] = Field(default_factory=list)


class WellnessEvidence(BaseModel):
    """Section B: Wellness evidence layer."""
    model_config = ConfigDict(extra="forbid")

    report_reference: WellnessReportReference
    wellness_assessment: Optional[str] = None
    biometrics: BiometricsSummary
    missing_data_declarations: List[str] = Field(default_factory=list)


class ExceptionalEventEntry(BaseModel):
    """Section C: Exceptional clinical event (spontaneous concerns, wearable spikes, alerts)."""
    model_config = ConfigDict(extra="forbid")

    event_type: str  # "wearable_alert", "vitals_alert", "spontaneous_concern", "emergency_trigger", "pre_emergency"
    event_id: str
    timestamp_utc: str
    source: str  # e.g. "wearable_sensor", "patient_message", "safety_guard"
    description: str
    clinical_event_identity: Optional[str] = None
    safety_tier: Optional[str] = None  # e.g. "Mode 2", "Pre-emergency"


class AthenaDailyAssessmentModelOutput(BaseModel):
    """
    Structured output produced by Athena (Gemini Flash) during the internal,
    non-chat daily clinical assessment scenario.
    All generated fields represent ATHENA_GENERATED_INTERPRETATION_NOT_RAW_EVIDENCE.
    """
    model_config = ConfigDict(extra="forbid")

    status: Literal["NORMAL", "ALERT", "CRITICAL"] = "NORMAL"
    interpretation: str = Field(..., min_length=1)
    observations: List[str] = Field(default_factory=list)
    relevant_changes: List[str] = Field(default_factory=list)
    contradictions: List[str] = Field(default_factory=list)
    uncertainties: List[str] = Field(default_factory=list)
    red_flags: bool = False


class AthenaClinicalAssessment(BaseModel):
    """Section D: Athena clinical assessment layer (strictly marked as AI interpretation)."""
    model_config = ConfigDict(extra="forbid")

    is_available: bool = False
    label: str = "ATHENA_GENERATED_INTERPRETATION_NOT_RAW_EVIDENCE"
    status: Optional[Literal["NORMAL", "ALERT", "CRITICAL"]] = None
    red_flags: Optional[bool] = None
    interpretation: Optional[str] = None
    observations: List[str] = Field(default_factory=list)
    relevant_changes: List[str] = Field(default_factory=list)
    contradictions: List[str] = Field(default_factory=list)
    uncertainties: List[str] = Field(default_factory=list)
    source_references: List[str] = Field(default_factory=list)
    provenance_model: Optional[str] = None
    absence_reason: Optional[str] = None


class AthenaDailyJournalRecord(BaseModel):
    """
    Internal, structured Athena clinical journal for one completed observation day (Stage A2.2).
    SERVER_ONLY and invisible to the patient.
    Supplies Weekly Consilium with 7 days of original evidence AND Athena's separate assessment.
    """
    model_config = ConfigDict(extra="forbid")

    journal_id: str
    cycle_id: str
    patient_id: str
    calendar_week: int = Field(ge=1)
    calendar_day: int = Field(ge=1, le=7)
    window_start_utc: str
    window_end_utc: str
    closure_timestamp_utc: str
    closure_type: str = "deterministic_utc_fallback"

    patient_reported_evidence: PatientReportedEvidence
    wellness_evidence: WellnessEvidence
    exceptional_events: List[ExceptionalEventEntry] = Field(default_factory=list)
    athena_clinical_assessment: AthenaClinicalAssessment
