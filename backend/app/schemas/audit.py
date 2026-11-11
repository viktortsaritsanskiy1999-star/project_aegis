from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, ConfigDict, Field


class ClinicalReviewStatus(str, Enum):
    NOT_REVIEWED = "NOT_REVIEWED"
    REVIEWED_PASS = "REVIEWED_PASS"
    REVIEWED_FAIL = "REVIEWED_FAIL"
    NEEDS_CLINICAL_REVIEW = "NEEDS_CLINICAL_REVIEW"


class AuditRecord(BaseModel):
    """
    Immutable record of a single Gemini LLM call stored as an audit artifact.
    Preserves exact unparsed model output and structured result separately.
    """
    model_config = ConfigDict(extra="ignore")

    call_id: str = Field(..., description="Unique identifier of this model invocation")
    patient_id: str = Field(..., description="Target patient identifier")
    cycle_id: str = Field(..., description="Macro-cycle identifier")
    sprint_id: Optional[str] = Field(None, description="Weekly sprint identifier if applicable")
    stage: str = Field(..., description="Pipeline stage or consilium step name")

    model_role: str = Field(..., description="Model cascade role (flash_lite, flash, pro, vision)")
    model_name: str = Field(..., description="Exact resolved Gemini model identifier")

    system_instruction: Optional[str] = Field(None, description="Exact system prompt provided to the model")
    prompt: Optional[str] = Field(None, description="Exact user prompt or payload sent to the model")
    input_context_summary: Optional[Dict[str, Any]] = Field(None, description="Synthetic input parameters or context")

    raw_response_text: str = Field(..., description="Unaltered verbatim raw response text directly from model")
    cleaned_json_text: Optional[str] = Field(None, description="Raw text stripped only of outer markdown code fences")
    parsed_json: Optional[Dict[str, Any]] = Field(None, description="JSON dictionary successfully parsed from raw text")

    status: str = Field(..., description="Inference outcome status (SUCCESS, SCHEMA_ERROR, JSON_DECODE_ERROR, etc.)")
    schema_name: Optional[str] = Field(None, description="Expected Pydantic response schema name if structured")
    validation_error: Optional[str] = Field(None, description="Validation or decode error message if validation failed")

    latency_ms: float = Field(default=0.0, description="End-to-end call latency in milliseconds")
    created_at_utc: str = Field(..., description="ISO-8601 timestamp in UTC when inference finished")

    report_id: Optional[str] = Field(None, description="Linked ModelReport.report_id in SQLite database")
    privacy_level: Optional[str] = Field(None, description="Linked report privacy level (server_only or client_facing)")

    attempt_number: int = Field(default=1, description="Attempt number if retried")
    total_attempts: int = Field(default=1, description="Total attempts executed")
    error_diagnostic_log: Optional[str] = Field(None, description="Diagnostic notes or retry logs")


class AuditRecordSummary(BaseModel):
    """Lightweight summary of an audit record for navigation trees and lists."""
    model_config = ConfigDict(from_attributes=True)

    call_id: str
    patient_id: str
    cycle_id: str
    sprint_id: Optional[str] = None
    stage: str
    model_role: str
    model_name: str
    status: str
    schema_name: Optional[str] = None
    latency_ms: float
    created_at_utc: str
    report_id: Optional[str] = None
    review_status: str = ClinicalReviewStatus.NOT_REVIEWED.value


class ClinicalReviewUpdateRequest(BaseModel):
    """Payload submitted by a qualified server administrator to record human clinical evaluation."""
    status: ClinicalReviewStatus
    reviewer_id: Optional[str] = Field(None, description="Identifier of the qualified medical/technical reviewer")
    notes: Optional[str] = Field(None, description="Human auditor clinical notes and observations")

    facts_accurate: Optional[bool] = None
    units_correct: Optional[bool] = None
    unjustified_conclusions_found: Optional[bool] = None
    cross_agent_consistent: Optional[bool] = None
    red_flags_identified: Optional[bool] = None
    strategy_aligns_with_inputs: Optional[bool] = None


class ClinicalReviewResponse(BaseModel):
    """Response showing live human quality review status for a call."""
    model_config = ConfigDict(from_attributes=True)

    review_id: str
    call_id: str
    patient_id: str
    cycle_id: str
    sprint_id: Optional[str] = None
    stage: Optional[str] = None
    status: str
    reviewer_id: Optional[str] = None
    reviewed_at: Optional[datetime] = None
    notes: Optional[str] = None

    facts_accurate: Optional[bool] = None
    units_correct: Optional[bool] = None
    unjustified_conclusions_found: Optional[bool] = None
    cross_agent_consistent: Optional[bool] = None
    red_flags_identified: Optional[bool] = None
    strategy_aligns_with_inputs: Optional[bool] = None
    updated_at: Optional[datetime] = None


class PersistedReportDetail(BaseModel):
    """Direct medical report record fetched from SQLite primary database."""
    model_config = ConfigDict(from_attributes=True)

    report_id: str
    cycle_id: str
    sprint_id: Optional[str] = None
    session_id: Optional[str] = None
    model: str
    report_type: str
    privacy_level: str
    version: int
    content: Dict[str, Any]
    created_at: datetime


class CallInspectionDetailResponse(BaseModel):
    """
    Complete inspection view returned to the local administrative dashboard.
    Combines audit record from storage, persisted SQLite medical report, and human review.
    """
    call: AuditRecord
    persisted_report: Optional[PersistedReportDetail] = None
    review: Optional[ClinicalReviewResponse] = None


class AdminLoginRequest(BaseModel):
    """Request payload for exchanging administrative secret for an ephemeral session token."""
    secret: str = Field(..., description="Server administrative secret (AEGIS_API_SECRET)")


class AdminLoginResponse(BaseModel):
    """Ephemeral session token issued to browser admin console."""
    access_token: str
    token_type: str = "bearer"
    expires_in: int = 7200


class OrganStateSummary(BaseModel):
    """Client-facing state of a single human organ system."""
    system_key: str
    title: str
    status: str = "nominal"  # nominal, attention, critical
    color: str = "#10B981"  # hex color matching Flutter palette
    deficiency_score: int = 0
    summary: str = ""
    highlights: List[str] = Field(default_factory=list)
    metrics: Dict[str, Any] = Field(default_factory=dict)
    report_id: Optional[str] = None
    version: int = 1


class PatientPreviewResponse(BaseModel):
    """
    Exact patient view reproduction dataset matching Flutter mobile app contracts.
    """
    patient_id: str
    cycle_id: str
    active_stage: str
    cascade_status: Dict[str, Any]
    overall_report: Optional[Dict[str, Any]] = None
    system_reports: Dict[str, Dict[str, Any]] = Field(default_factory=dict)
    referral_status: Optional[Dict[str, Any]] = None
    referral_documents: List[Dict[str, Any]] = Field(default_factory=list)
    organ_states: List[OrganStateSummary] = Field(default_factory=list)
    trace_comparison: Dict[str, Any] = Field(default_factory=dict)


class UnifiedTimelineEvent(BaseModel):
    """A single normalized chronological patient event across autonomous processes."""
    event_id: str
    category: str  # TELEMETRY, WORKOUT, EMERGENCY, WELLNESS, ATHENA_CHAT, VISION_OCR, LAB_REVISION, PRIMARY_CONSILIUM, WEEKLY_CONSILIUM, NEO_REASSESSMENT, AI_INFERENCE, CLINICAL_REVIEW
    title: str
    description: str
    virtual_time: Optional[str] = None
    execution_time_utc: str
    status: str = "SUCCESS"  # SUCCESS, WARNING, ERROR, PENDING
    call_id: Optional[str] = None
    report_id: Optional[str] = None
    anomalies: List[str] = Field(default_factory=list)
    details: Dict[str, Any] = Field(default_factory=dict)


class UnifiedTimelineResponse(BaseModel):
    """Chronologically ordered ledger of all synthetic patient events."""
    patient_id: str
    total_events: int
    events: List[UnifiedTimelineEvent]
    anomaly_counts: Dict[str, int] = Field(default_factory=dict)
