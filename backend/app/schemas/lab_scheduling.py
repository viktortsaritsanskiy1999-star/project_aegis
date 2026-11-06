from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, ConfigDict, Field

from app.schemas.consilium import LabMarkerMutationItem, LabObservationEntry, LaboratoryDiffReport


class LabDocumentStatus(str, Enum):
    """Lifecycle status of an uploaded laboratory document."""
    UPLOADED = "uploaded"
    PROCESSING = "processing"
    PROCESSED = "processed"
    ERROR = "error"
    DUPLICATE = "duplicate"


class LabChangeType(str, Enum):
    """Result of clinical change detection for laboratory updates."""
    NO_CHANGE = "no_change"
    MEANINGFUL_CHANGE = "meaningful_change"
    DUPLICATE_FILE = "duplicate_file"


class LabDocumentRecord(BaseModel):
    """
    Durable server-side record for an uploaded laboratory document page/file.
    Stored under report_type='lab_document_record', SERVER_ONLY in model_reports.
    Guarantees that original document data is permanently retained even if OCR fails.
    """
    model_config = ConfigDict(extra="forbid")

    document_id: str
    cycle_id: str
    patient_id: str
    filename: str
    mime_type: str
    file_sha256: str
    byte_size: int
    raw_data_base64: Optional[str] = None
    status: LabDocumentStatus = LabDocumentStatus.UPLOADED
    error_message: Optional[str] = None
    uploaded_at_utc: str
    processed_at_utc: Optional[str] = None
    extracted_markers_count: int = 0
    ocr_report_id: Optional[str] = None
    is_duplicate: bool = False
    duplicate_of_document_id: Optional[str] = None


class LabRevisionPendingTask(BaseModel):
    """
    Durable server-side queue task for background Lab Revision.
    Stored under report_type='lab_revision_pending_task', SERVER_ONLY in model_reports.
    Tracks 15-minute debounce, 72-hour cooldown, and accumulated unreviewed changes.
    """
    model_config = ConfigDict(extra="forbid")

    task_id: str
    cycle_id: str
    patient_id: str
    status: str = "pending"  # pending, ready, running, completed, cancelled
    accumulated_changes_count: int = 0
    first_meaningful_change_at_utc: Optional[str] = None
    last_meaningful_change_at_utc: Optional[str] = None
    debounce_until_utc: Optional[str] = None
    cooldown_until_utc: Optional[str] = None
    effective_ready_at_utc: Optional[str] = None
    has_pending_ocr: bool = False
    pending_document_ids: List[str] = Field(default_factory=list)
    target_ledger_version: int = 0
    change_summary: List[str] = Field(default_factory=list)
    created_at_utc: str
    updated_at_utc: str
    is_claimed: bool = False
    claimed_by: Optional[str] = None
    claimed_at_utc: Optional[str] = None
    last_attempted_at_utc: Optional[str] = None
    attempts_count: int = 0
    has_unresolved_ocr_errors: bool = False


class LabManualEditRequest(BaseModel):
    """Client request for manual laboratory marker editing or correction."""
    model_config = ConfigDict(extra="forbid")

    mutations: List[LabMarkerMutationItem] = Field(..., min_length=1)


class LabManualEditResponse(BaseModel):
    """Client response for manual laboratory marker editing."""
    model_config = ConfigDict(extra="forbid")

    cycle_id: str
    status: str
    change_type: LabChangeType
    ledger_version: int
    mutations_applied: int
    has_unreviewed_lab_changes: bool
    summary: str


class LabSchedulingStatusResponse(BaseModel):
    """Status of background Lab Revision scheduling for a cycle."""
    model_config = ConfigDict(extra="forbid")

    cycle_id: str
    has_pending_task: bool
    is_ready_for_revision: bool
    debounce_until_utc: Optional[str] = None
    cooldown_until_utc: Optional[str] = None
    effective_ready_at_utc: Optional[str] = None
    has_pending_ocr: bool = False
    accumulated_changes_count: int = 0
    unread_documents_count: int = 0
    evaluation_status: str
    readiness_reason: str


class BackgroundLabRevisionDeputyOutput(BaseModel):
    """
    Structured analytical output for Deputy Chief Physician (Gemini Flash)
    during background Lab Revision. Synthesizes findings across 8 organ systems.
    """
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    deputy_synthesis: str = Field(..., min_length=1, description="Intersystem synthesis comparing new vs previous data.")
    specialist_discrepancies: List[str] = Field(default_factory=list, description="Divergences or conflicting findings between specialists.")
    clinical_correlations: List[str] = Field(default_factory=list, description="Cross-system interactions and pathophysiological links.")
    data_limitations: List[str] = Field(default_factory=list, description="Data limitations, unmeasured indicators, or measurement gaps.")
    draft_hypotheses: List[str] = Field(default_factory=list, description="Preliminary clinical hypotheses for Chief Physician evaluation.")


class BackgroundLabRevisionChiefOutput(BaseModel):
    """
    Structured analytical output for Chief Physician (Gemini Pro)
    during background Lab Revision. Strictly analytical: does NOT issue a new
    patient strategy, change Athena's plan, or alter vitals thresholds.
    """
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    analytical_summary: str = Field(..., min_length=1, description="Supreme analytical evaluation of changes and trends.")
    observed_facts: List[str] = Field(default_factory=list, description="Verifiable facts and trends in markers over time.")
    clinical_hypotheses: List[str] = Field(default_factory=list, description="Working hypotheses to be tested; NOT established diagnoses.")
    uncertainties_and_data_deficits: List[str] = Field(default_factory=list, description="Areas where laboratory or instrumental data is incomplete.")
    questions_for_weekly_consilium: List[str] = Field(default_factory=list, description="Targeted clinical questions and focus areas for the next Weekly Consilium.")
    cascade_quality_audit: str = Field(..., min_length=1, description="Audit of the 8 specialists and Deputy Flash findings.")


class BackgroundLabRevisionReportRecord(BaseModel):
    """
    Durable server-side record for a completed or in-progress background Lab Revision.
    Stored under report_type='background_lab_revision_report', SERVER_ONLY in model_reports.
    Preserves versioned history and feeds into the next Weekly Consilium (Stage 6C).
    """
    model_config = ConfigDict(extra="forbid")

    report_id: str
    cycle_id: str
    patient_id: str
    evaluated_ledger_version: int
    baseline_ledger_version: int
    source_document_ids: List[str] = Field(default_factory=list)
    study_ids: List[str] = Field(default_factory=list)
    status: str = "completed"  # completed, in_progress, failed
    started_at_utc: str
    completed_at_utc: Optional[str] = None
    specialist_reports: Dict[str, Any] = Field(default_factory=dict)
    deputy_report: Dict[str, Any] = Field(default_factory=dict)
    chief_report: Dict[str, Any] = Field(default_factory=dict)
    summary: str = ""

