from datetime import datetime, timezone
from typing import Callable, Dict, List, Literal, Optional
from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    Header,
    HTTPException,
    Request,
    Response,
    UploadFile,
    status,
)
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.medical_cycle import MedicalCycle
from app.repositories.cycle_repository import MedicalCycleRepository
from app.repositories.report_repository import ModelReportRepository, ReportVersionConflictError
from app.schemas.lab_scheduling import (
    LabChangeType,
    LabDocumentRecord,
    LabDocumentStatus,
    LabManualEditRequest,
    LabManualEditResponse,
    LabRevisionPendingTask,
    LabSchedulingStatusResponse,
)
from app.services.lab_scheduling_service import LabSchedulingService
from app.schemas.consilium import (
    AnamnesisQuestionnaireAnswers,
    AnamnesisIntakeClientResponse,
    ClientVitalsThresholds,
    EcgVisionIntakeClientResponse,
    LabLedgerStateResponse,
    LabVisionIntakeClientResponse,
    PrimaryConsiliumClientResponse,
    WeeklyConsiliumClientResponse,
    SYSTEM_KEYS,
)
from app.schemas.reports import ClientModelReportResponse
from app.security.auth import (
    DeviceSession,
    require_device_session,
    require_scopes,
)
from app.security.tokens import (
    SCOPE_CASCADE_INTAKE,
    SCOPE_CASCADE_RUN,
    SCOPE_REPORTS_READ,
)
from app.services.gemini_gateway import GeminiGateway, GeminiGatewayError
from app.services.primary_consilium_service import (
    PrimaryConsiliumService,
    ConsiliumError,
    ConsiliumCycleNotFoundError,
    ConsiliumContextMissingError,
    ConsiliumQualityRejectionError,
    ConsiliumAnamnesisLockedError,
    ConsiliumConcurrencyConflictError,
    PREPARATORY_ANAMNESIS_REPORT_TYPE,
    PREPARATORY_ECG_REPORT_TYPE,
)
from app.services.lab_revision_consilium_service import (
    LabRevisionConsiliumService,
    LABORATORY_LEDGER_REPORT_TYPE,
)
from app.services.weekly_consilium_service import (
    WeeklyConsiliumService,
    WEEKLY_CONSILIUM_EXECUTION_LOG_REPORT_TYPE,
)
from app.services.lab_vision_ocr_service import (
    LabVisionOcrService,
    LabVisionValidationError,
)
from app.services.ecg_vision_service import (
    EcgVisionService,
    EcgVisionValidationError,
)
from app.services.clinical_automation_service import ClinicalAutomationService
from app.services.athena_chat_service import AthenaChatService

router = APIRouter(prefix="/api/v1/client", tags=["Client Medical Operations"])


# ==============================================================================
# DEPENDENCIES (OVERRIDEABLE IN TESTS)
# ==============================================================================

def get_gemini_gateway() -> GeminiGateway:
    """Dependency provider for GeminiGateway (overrideable in tests via dependency_overrides)."""
    return GeminiGateway()


def get_server_now_provider() -> Callable[[], datetime]:
    """Dependency provider for authoritative server UTC clock (overrideable in tests)."""
    return lambda: datetime.now(timezone.utc)


def resolve_active_cycle_for_patient(
    db: Session = Depends(get_db),
    session: DeviceSession = Depends(require_device_session),
) -> MedicalCycle:
    """
    Enforces active clinical cycle ownership for authenticated mobile client:
    - Resolves active cycle strictly for `session.patient_id`.
    - Never accepts `cycle_id` or `patient_id` parameter from client request body or query string.
    - Raises 404 Not Found if no active cycle exists for this patient.
    """
    repo = MedicalCycleRepository(db)
    active_cycle = repo.get_active_for_patient(session.patient_id)
    if active_cycle is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No active medical cycle found for authenticated patient '{session.patient_id}'.",
        )
    return active_cycle


# ==============================================================================
# SCHEMAS FOR CLIENT CIRCUIT
# ==============================================================================

class ClientAnamnesisIntakeRequest(BaseModel):
    """
    Client request payload for submitting raw questionnaire answers.
    `cycle_id` is optional and ignored if provided: the server resolves or
    provisions the active cycle for `session.patient_id` with Zero Oracle Leakage.
    """
    model_config = ConfigDict(extra="forbid")
    answers: AnamnesisQuestionnaireAnswers
    cycle_id: Optional[str] = Field(default=None, description="Optional; server authoritative session takes precedence")


class ClientReferralStatus(BaseModel):
    """Client-safe view of active referral status without internal clinical reasoning."""
    model_config = ConfigDict(extra="forbid")

    has_active_referral: bool = False
    status: Literal["none", "generating_document", "document_ready", "visit_confirmed"] = "none"
    document_generation_state: Literal["none", "generating", "ready", "temporarily_failed"] = "none"
    urgency: Optional[str] = None
    target_specialties: List[str] = Field(default_factory=list)
    clinical_reason_summary: Optional[str] = None
    document_report_id: Optional[str] = None
    created_at_utc: Optional[str] = None


class ClientCascadeStatusResponse(BaseModel):
    """Status summary of client clinical pipeline readiness for authenticated patient."""
    model_config = ConfigDict(extra="forbid")
    patient_id: str
    device_id: str
    has_active_cycle: bool
    active_cycle_id: Optional[str] = None
    has_preparatory_anamnesis: bool = False
    has_preparatory_labs: bool = False
    has_preparatory_ecg: bool = False
    is_primary_consilium_completed: bool = False
    has_unreviewed_lab_changes: bool = False
    current_weekly_sprint_id: Optional[str] = None
    weekly_sprint_number: Optional[int] = None
    is_weekly_consilium_completed: bool = False
    can_start_next_sprint: bool = False
    next_sprint_id: Optional[str] = None
    current_calendar_week: Optional[int] = None
    current_calendar_day: Optional[int] = None
    has_unanswered_routine_question: bool = False
    active_routine_question_id: Optional[str] = None
    active_routine_question_text: Optional[str] = None
    routine_question_reminders_count: int = 0
    can_send_routine_reminder: bool = False
    has_active_referral: bool = False
    referral_status: ClientReferralStatus = Field(default_factory=ClientReferralStatus)


class ClientWeeklyConsiliumRunRequest(BaseModel):
    """
    Client request payload for triggering or recovering Weekly Consilium.
    `cycle_id` is optional and ignored: server authoritative session takes precedence.
    """
    model_config = ConfigDict(extra="forbid")
    sprint_id: Optional[str] = Field(default=None, description="Optional sprint_id")
    report_date: Optional[str] = Field(default=None, description="Optional report_date (YYYY-MM-DD)")
    cycle_id: Optional[str] = Field(default=None, description="Optional; server authoritative session takes precedence")


# ==============================================================================
# RESULT RECOVERY HELPER (IDEMPOTENCY)
# ==============================================================================

def _recover_primary_consilium_client_response(
    db: Session,
    cycle_id: str,
) -> Optional[PrimaryConsiliumClientResponse]:
    """
    Recovers existing client-facing Primary Consilium reports directly from storage.
    Guarantees zero duplicate Gemini API calls on client retries or reconnections.
    """
    report_repo = ModelReportRepository(db)
    overall_reports = report_repo.list_client_facing(
        cycle_id=cycle_id,
        report_type="user_overall_report",
    )
    if not overall_reports:
        return None

    latest_overall = overall_reports[0]
    overall_validated = ClientModelReportResponse.model_validate(latest_overall)
    content = latest_overall.content or {}
    report_date = str(content.get("report_date") or "")
    metrics = content.get("metrics") or {}

    system_reports: Dict[str, ClientModelReportResponse] = {}
    for sys_key in SYSTEM_KEYS:
        sys_reps = report_repo.list_client_facing(
            cycle_id=cycle_id,
            report_type=f"system_user_report_{sys_key}",
        )
        if sys_reps:
            system_reports[sys_key] = ClientModelReportResponse.model_validate(sys_reps[0])

    return PrimaryConsiliumClientResponse(
        cycle_id=cycle_id,
        report_date=report_date or datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        overall_report=overall_validated,
        system_reports=system_reports,
        vitals_thresholds=ClientVitalsThresholds(
            min_pessimum_bpm=int(metrics.get("min_pessimum_bpm", 45)),
            max_pessimum_bpm=int(metrics.get("max_pessimum_bpm", 115)),
            max_spike_delta_bpm=int(metrics.get("max_spike_delta_bpm", 25)),
        ),
    )


# ==============================================================================
# ROUTE ENDPOINTS
# ==============================================================================

@router.post(
    "/cascade/intake/anamnesis",
    response_model=AnamnesisIntakeClientResponse,
    summary="Submit raw anamnesis answers from mobile device (Patient-Isolated, Auto-Cycle)",
    description=(
        "Authorized mobile endpoint receiving 17-card questionnaire answers. "
        "Strictly requires `DeviceSession` with `cascade:intake` scope. "
        "If authenticated patient has no active cycle, atomically provisions Cycle 1. "
        "Executes server Anamnesis M1 in blind mode, saves server_only reports, "
        "and returns metadata confirmation without leaking private clinical reasoning."
    ),
)
async def submit_client_anamnesis_intake(
    payload: ClientAnamnesisIntakeRequest,
    db: Session = Depends(get_db),
    session: DeviceSession = Depends(require_device_session),
    _scope: DeviceSession = Depends(require_scopes(SCOPE_CASCADE_INTAKE)),
    gateway: GeminiGateway = Depends(get_gemini_gateway),
    now_provider: Callable[[], datetime] = Depends(get_server_now_provider),
) -> AnamnesisIntakeClientResponse:
    cycle_repo = MedicalCycleRepository(db)

    # 1. Atomically resolve or provision active cycle for authenticated patient
    cycle, _created = cycle_repo.get_or_create_active_for_patient(session.patient_id)

    # 2. Invoke PrimaryConsiliumService with content-hash idempotency
    service = PrimaryConsiliumService(db=db, gateway=gateway)
    try:
        return await service.process_anamnesis_intake(
            cycle_id=cycle.cycle_id,
            answers=payload.answers,
        )
    except ConsiliumCycleNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    except (
        ConsiliumAnamnesisLockedError,
        ConsiliumConcurrencyConflictError,
        ReportVersionConflictError,
    ) as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc
    except GeminiGatewayError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="AI Gateway is temporarily unavailable. Please retry later.",
        ) from exc
    except ConsiliumError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc


@router.get(
    "/cascade/status",
    response_model=ClientCascadeStatusResponse,
    summary="Get clinical readiness status for authenticated patient (Patient-Isolated)",
)
def get_client_cascade_status(
    db: Session = Depends(get_db),
    session: DeviceSession = Depends(require_device_session),
    _scope: DeviceSession = Depends(require_scopes("cycle:read")),
    gateway: GeminiGateway = Depends(get_gemini_gateway),
    now_provider: Callable[[], datetime] = Depends(get_server_now_provider),
) -> ClientCascadeStatusResponse:
    cycle_repo = MedicalCycleRepository(db)
    active_cycle = cycle_repo.get_active_for_patient(session.patient_id)

    if active_cycle is None:
        return ClientCascadeStatusResponse(
            patient_id=session.patient_id,
            device_id=session.device_id,
            has_active_cycle=False,
            current_weekly_sprint_id=None,
            weekly_sprint_number=None,
            is_weekly_consilium_completed=False,
            can_start_next_sprint=False,
            next_sprint_id=None,
            current_calendar_week=None,
            current_calendar_day=None,
            has_active_referral=False,
            referral_status=ClientReferralStatus(
                has_active_referral=False,
                status="none",
            ),
        )

    cycle_id = active_cycle.cycle_id
    report_repo = ModelReportRepository(db)
    primary_service = PrimaryConsiliumService(db=db, gateway=gateway, now_provider=now_provider)
    lab_service = LabRevisionConsiliumService(db=db, gateway=gateway)
    weekly_service = WeeklyConsiliumService(db=db, gateway=gateway, now_provider=now_provider)

    has_m1 = len(report_repo.list_server_only(cycle_id=cycle_id, report_type=PREPARATORY_ANAMNESIS_REPORT_TYPE)) > 0
    has_ecg = len(report_repo.list_server_only(cycle_id=cycle_id, report_type=PREPARATORY_ECG_REPORT_TYPE)) > 0
    has_labs = len(report_repo.list_server_only(cycle_id=cycle_id, report_type=LABORATORY_LEDGER_REPORT_TYPE)) > 0
    is_completed = primary_service.is_primary_consilium_completed(cycle_id)

    ledger_state = lab_service.get_lab_ledger_state(cycle_id)
    has_unreviewed = ledger_state.has_unreviewed_lab_changes

    weekly_state = weekly_service.get_weekly_sprint_lifecycle_state(cycle_id)

    ref_athena_state = weekly_service.get_referral_state_for_athena(cycle_id)
    if ref_athena_state.get("lifecycle_status") != "none":
        has_active_ref = bool(ref_athena_state.get("has_active_referral", False))
        client_referral = ClientReferralStatus(
            has_active_referral=has_active_ref,
            status=ref_athena_state.get("lifecycle_status", "none"),
            document_generation_state=ref_athena_state.get("document_generation_state", "none"),
            urgency=ref_athena_state.get("urgency"),
            target_specialties=ref_athena_state.get("target_specialties") or [],
            clinical_reason_summary=ref_athena_state.get("clinical_reason_for_referral"),
            document_report_id=ref_athena_state.get("document_report_id"),
            created_at_utc=ref_athena_state.get("created_at_utc"),
        )
    else:
        has_active_ref = False
        client_referral = ClientReferralStatus(
            has_active_referral=False,
            status="none",
            document_generation_state="none",
        )

    athena_service = AthenaChatService(db=db, gateway=gateway, now_provider=now_provider)
    active_q = athena_service.get_active_routine_question(cycle_id)
    has_unanswered_q = active_q is not None and active_q.get("status") == "unanswered"
    active_q_id = active_q.get("question_id") if has_unanswered_q else None
    active_q_text = active_q.get("question_text") if has_unanswered_q else None
    reminders_cnt = int(active_q.get("reminders_count", 0)) if has_unanswered_q else 0
    max_reminders = int(active_q.get("max_reminders", 2)) if has_unanswered_q else 2
    can_remind = has_unanswered_q and (reminders_cnt < max_reminders)

    return ClientCascadeStatusResponse(
        patient_id=session.patient_id,
        device_id=session.device_id,
        has_active_cycle=True,
        active_cycle_id=cycle_id,
        has_preparatory_anamnesis=has_m1,
        has_preparatory_labs=has_labs,
        has_preparatory_ecg=has_ecg,
        is_primary_consilium_completed=is_completed,
        has_unreviewed_lab_changes=has_unreviewed,
        current_weekly_sprint_id=weekly_state.current_weekly_sprint_id,
        weekly_sprint_number=weekly_state.weekly_sprint_number,
        is_weekly_consilium_completed=weekly_state.is_weekly_consilium_completed,
        can_start_next_sprint=weekly_state.can_start_next_sprint,
        next_sprint_id=weekly_state.next_sprint_id,
        current_calendar_week=weekly_state.current_calendar_week,
        current_calendar_day=weekly_state.current_calendar_day,
        has_unanswered_routine_question=has_unanswered_q,
        active_routine_question_id=active_q_id,
        active_routine_question_text=active_q_text,
        routine_question_reminders_count=reminders_cnt,
        can_send_routine_reminder=can_remind,
        has_active_referral=has_active_ref,
        referral_status=client_referral,
    )


@router.post(
    "/cascade/intake/labs",
    response_model=LabVisionIntakeClientResponse,
    summary="Upload laboratory document images and run Vision OCR for authenticated patient (Patient-Isolated, Auto-Cycle)",
    description=(
        "Patient-scoped endpoint receiving 1 to 15 laboratory document images (JPEG, PNG, WEBP, HEIC/HEIF). "
        "Strictly requires authenticated `DeviceSession` with `cascade:intake` scope. "
        "Atomically resolves or provisions the active clinical cycle for the authenticated patient without "
        "allowing arbitrary client-supplied cycle IDs. Validates real image magic bytes and resource limits, "
        "extracts laboratory markers via server-side Gemini Vision OCR, and atomically synchronizes observations "
        "into the versioned laboratory ledger."
    ),
)
@router.post(
    "/labs/upload",
    response_model=LabVisionIntakeClientResponse,
    include_in_schema=False,
)
async def submit_client_lab_document_intake(
    files: Optional[List[UploadFile]] = File(None),
    file: Optional[UploadFile] = File(None),
    is_correction: bool = Form(default=False),
    sample_date: Optional[str] = Form(default=None),
    db: Session = Depends(get_db),
    session: DeviceSession = Depends(require_device_session),
    _scope: DeviceSession = Depends(require_scopes(SCOPE_CASCADE_INTAKE)),
    gateway: GeminiGateway = Depends(get_gemini_gateway),
    now_provider: Callable[[], datetime] = Depends(get_server_now_provider),
) -> LabVisionIntakeClientResponse:
    upload_files: List[UploadFile] = []
    if files:
        upload_files.extend(files)
    elif file:
        upload_files.append(file)

    if not upload_files:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="At least one laboratory report image page must be uploaded.",
        )

    # 1. Atomically resolve or provision active cycle for authenticated patient
    cycle_repo = MedicalCycleRepository(db)
    cycle, _created = cycle_repo.get_or_create_active_for_patient(session.patient_id)

    # 2. Save original documents and check file duplicates via LabSchedulingService
    scheduling_service = LabSchedulingService(db=db, gateway=gateway, now_provider=now_provider)
    saved_records: List[LabDocumentRecord] = []
    for f in upload_files:
        content = await f.read()
        await f.seek(0)
        rec = scheduling_service.save_uploaded_document(
            cycle_id=cycle.cycle_id,
            patient_id=session.patient_id,
            filename=f.filename or "uploaded_report",
            content_bytes=content,
        )
        saved_records.append(rec)

    prev_state = scheduling_service.lab_revision_service.get_lab_ledger_state(cycle.cycle_id)
    prev_version = prev_state.ledger_version

    # 3. Process lab images via LabVisionOcrService with atomic ledger integration
    service = LabVisionOcrService(
        db=db,
        gateway=gateway,
        now_provider=now_provider,
    )
    try:
        response = await service.process_lab_images(
            cycle_id=cycle.cycle_id,
            files=upload_files,
            is_correction=is_correction,
            sample_date_override=sample_date,
        )
        # Check if meaningful changes occurred
        new_state = scheduling_service.lab_revision_service.get_lab_ledger_state(cycle.cycle_id)
        is_meaningful = new_state.ledger_version > prev_version
        scheduling_service.update_or_create_pending_task(
            cycle_id=cycle.cycle_id,
            patient_id=session.patient_id,
            is_meaningful_change=is_meaningful,
        )
        for rec in saved_records:
            if not rec.is_duplicate:
                rec.status = LabDocumentStatus.PROCESSED
                rec.processed_at_utc = now_provider().isoformat()
                scheduling_service.update_document_record(rec)
        return response
    except Exception as exc:
        for rec in saved_records:
            if not rec.is_duplicate:
                rec.status = LabDocumentStatus.ERROR
                rec.error_message = str(exc)
                scheduling_service.update_document_record(rec)
        if isinstance(exc, ConsiliumCycleNotFoundError):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        if isinstance(exc, (ConsiliumConcurrencyConflictError, ReportVersionConflictError)):
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        if isinstance(exc, LabVisionValidationError):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
        if isinstance(exc, GeminiGatewayError):
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Gemini Vision OCR extraction failed. Please retry later.") from exc
        if isinstance(exc, ConsiliumError):
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
        raise


@router.get(
    "/labs/state",
    response_model=LabLedgerStateResponse,
    summary="Get laboratory ledger history and state for authenticated patient (Patient-Isolated)",
    description=(
        "Returns full laboratory ledger for patient's active cycle: "
        "all markers with dates, units, reference intervals, version history, "
        "and unreviewed laboratory changes flag."
    ),
)
@router.get(
    "/cascade/labs/state",
    response_model=LabLedgerStateResponse,
    include_in_schema=False,
)
def get_client_lab_ledger_state(
    cycle: MedicalCycle = Depends(resolve_active_cycle_for_patient),
    db: Session = Depends(get_db),
    _scope: DeviceSession = Depends(require_scopes("cycle:read")),
    gateway: GeminiGateway = Depends(get_gemini_gateway),
    now_provider: Callable[[], datetime] = Depends(get_server_now_provider),
) -> LabLedgerStateResponse:
    lab_service = LabRevisionConsiliumService(db=db, gateway=gateway, now_provider=now_provider)
    return lab_service.get_lab_ledger_state(cycle.cycle_id)

@router.post(
    "/cascade/labs/manual-edit",
    response_model=LabManualEditResponse,
    summary="Submit manual edits or corrections to laboratory markers (with 15-minute debounce change detection)",
    description=(
        "Applies manual patient adjustments or error corrections to laboratory markers. "
        "Strictly verifies meaningful changes against current ledger baseline. "
        "Meaningful changes reset the 15-minute debounce countdown for background Lab Revision. "
        "Empty or identical values (e.g. 4 -> 4) result in NO_CHANGE without timer reset."
    ),
)
async def submit_client_lab_manual_edit(
    payload: LabManualEditRequest,
    cycle: MedicalCycle = Depends(resolve_active_cycle_for_patient),
    db: Session = Depends(get_db),
    session: DeviceSession = Depends(require_device_session),
    _scope: DeviceSession = Depends(require_scopes(SCOPE_CASCADE_INTAKE)),
    gateway: GeminiGateway = Depends(get_gemini_gateway),
    now_provider: Callable[[], datetime] = Depends(get_server_now_provider),
) -> LabManualEditResponse:
    scheduling_service = LabSchedulingService(db=db, gateway=gateway, now_provider=now_provider)
    try:
        return scheduling_service.process_manual_edit(
            cycle_id=cycle.cycle_id,
            patient_id=session.patient_id,
            mutations=payload.mutations,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    except (ConsiliumConcurrencyConflictError, ReportVersionConflictError) as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc


@router.get(
    "/cascade/labs/scheduling-status",
    response_model=LabSchedulingStatusResponse,
    summary="Get background Lab Revision scheduling status (debounce, cooldown, readiness)",
)
def get_client_lab_scheduling_status(
    cycle: MedicalCycle = Depends(resolve_active_cycle_for_patient),
    db: Session = Depends(get_db),
    _scope: DeviceSession = Depends(require_scopes("cycle:read")),
    gateway: GeminiGateway = Depends(get_gemini_gateway),
    now_provider: Callable[[], datetime] = Depends(get_server_now_provider),
) -> LabSchedulingStatusResponse:
    scheduling_service = LabSchedulingService(db=db, gateway=gateway, now_provider=now_provider)
    return scheduling_service.get_scheduling_status(cycle.cycle_id)



@router.post(
    "/cascade/intake/ecg",
    response_model=EcgVisionIntakeClientResponse,
    summary="Upload electrocardiogram image and run Vision ECG for authenticated patient (Patient-Isolated, Auto-Cycle)",
    description=(
        "Patient-scoped endpoint receiving an electrocardiogram tracing image (JPEG, PNG, WEBP, HEIC/HEIF). "
        "Strictly requires authenticated `DeviceSession` with `cascade:intake` scope. "
        "Atomically resolves or provisions the active clinical cycle for the authenticated patient without "
        "allowing arbitrary client-supplied cycle IDs or patient IDs. Validates real image magic bytes and 10MB limit, "
        "extracts ECG features via server-side Gemini Vision under server_only privacy level, "
        "and securely returns operational metadata without leaking internal clinical hypotheses."
    ),
)
@router.post(
    "/ecg/upload",
    response_model=EcgVisionIntakeClientResponse,
    include_in_schema=False,
)
async def submit_client_ecg_document_intake(
    file: Optional[UploadFile] = File(None),
    files: Optional[List[UploadFile]] = File(None),
    record_date: Optional[str] = Form(default=None),
    db: Session = Depends(get_db),
    session: DeviceSession = Depends(require_device_session),
    _scope: DeviceSession = Depends(require_scopes(SCOPE_CASCADE_INTAKE)),
    gateway: GeminiGateway = Depends(get_gemini_gateway),
    now_provider: Callable[[], datetime] = Depends(get_server_now_provider),
) -> EcgVisionIntakeClientResponse:
    target_file = file
    if target_file is None and files:
        target_file = files[0]

    if not target_file:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="An electrocardiogram image file must be uploaded.",
        )

    # 1. Atomically resolve or provision active cycle for authenticated patient
    cycle_repo = MedicalCycleRepository(db)
    cycle, _created = cycle_repo.get_or_create_active_for_patient(session.patient_id)

    # 2. Process ECG image via EcgVisionService with content-hash deduplication and versioning
    service = EcgVisionService(
        db=db,
        gateway=gateway,
        now_provider=now_provider,
    )
    try:
        return await service.process_ecg_image(
            cycle_id=cycle.cycle_id,
            file=target_file,
            record_date_override=record_date,
        )
    except ConsiliumCycleNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    except (ConsiliumConcurrencyConflictError, ReportVersionConflictError) as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc
    except EcgVisionValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    except GeminiGatewayError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Gemini Vision ECG extraction failed. Please retry later.",
        ) from exc
    except ConsiliumError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc


@router.get(
    "/cascade/ecg/latest",
    response_model=EcgVisionIntakeClientResponse,
    summary="Get latest electrocardiogram intake confirmation for authenticated patient (Patient-Isolated)",
    description=(
        "Returns the latest confirmed electrocardiogram intake metadata for the patient's active cycle. "
        "Strictly requires `cycle:read` scope and patient device session."
    ),
)
@router.get(
    "/ecg/latest",
    response_model=EcgVisionIntakeClientResponse,
    include_in_schema=False,
)
def get_client_latest_ecg(
    cycle: MedicalCycle = Depends(resolve_active_cycle_for_patient),
    db: Session = Depends(get_db),
    _scope: DeviceSession = Depends(require_scopes("cycle:read")),
    gateway: GeminiGateway = Depends(get_gemini_gateway),
) -> EcgVisionIntakeClientResponse:
    report_repo = ModelReportRepository(db)
    ecg_reports = report_repo.list_server_only(
        cycle_id=cycle.cycle_id,
        report_type=PREPARATORY_ECG_REPORT_TYPE,
    )
    if not ecg_reports:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No electrocardiogram report found for active cycle '{cycle.cycle_id}'.",
        )

    latest = ecg_reports[0]
    content = latest.content or {}
    primary_service = PrimaryConsiliumService(db=db, gateway=gateway)
    primary_concluded = primary_service.is_primary_consilium_completed(cycle.cycle_id)

    created_at_iso = (
        latest.created_at.replace(tzinfo=timezone.utc).isoformat()
        if latest.created_at and latest.created_at.tzinfo is None
        else (latest.created_at.astimezone(timezone.utc).isoformat() if latest.created_at else datetime.now(timezone.utc).isoformat())
    )

    return EcgVisionIntakeClientResponse(
        cycle_id=cycle.cycle_id,
        status="processed",
        intake_version=latest.version,
        recording_type=str(content.get("recording_type", "unknown")),
        recording_quality=str(content.get("recording_quality", "moderate")),
        created_at_utc=created_at_iso,
        primary_consilium_concluded=primary_concluded,
    )


@router.post(
    "/cascade/run-primary",
    response_model=PrimaryConsiliumClientResponse,
    summary="Run or recover Primary Consilium for authenticated patient (Patient-Isolated)",
    description=(
        "If Primary Consilium was already concluded, safely recovers completed "
        "client-facing reports without repeating Gemini calls (`X-Aegis-Idempotent-Replay: true`). "
        "Fresh executions are protected by cross-process SQLite lease, heartbeat renewal, and fencing."
    ),
)
async def run_client_primary_consilium(
    request: Request,
    response: Response,
    cycle: MedicalCycle = Depends(resolve_active_cycle_for_patient),
    db: Session = Depends(get_db),
    _scope: DeviceSession = Depends(require_scopes(SCOPE_CASCADE_RUN)),
    gateway: GeminiGateway = Depends(get_gemini_gateway),
    now_provider: Callable[[], datetime] = Depends(get_server_now_provider),
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
) -> PrimaryConsiliumClientResponse:
    service = PrimaryConsiliumService(
        db=db,
        gateway=gateway,
        now_provider=now_provider,
    )
    automation_service = ClinicalAutomationService(
        db=db,
        gateway=gateway,
        now_provider=now_provider,
    )

    def _enqueue_and_notify_post_primary() -> None:
        try:
            automation_service.enqueue_post_primary_init(
                cycle_id=cycle.cycle_id,
                patient_id=cycle.patient_id,
            )
            worker = getattr(request.app.state, "automation_worker", None)
            if worker is not None and hasattr(worker, "notify"):
                worker.notify()
        except Exception:
            pass

    # 1. Result Recovery Guard: If Primary Consilium is completed, recover existing reports immediately
    if service.is_primary_consilium_completed(cycle.cycle_id):
        recovered = service.recover_completed_response(cycle.cycle_id)
        if recovered is not None:
            _enqueue_and_notify_post_primary()
            response.headers["X-Aegis-Idempotent-Replay"] = "true"
            return recovered

    # 2. Protected Execution under distributed SQLite lease and fencing
    try:
        result = await service.run_primary_consilium(
            cycle_id=cycle.cycle_id,
            idempotency_key=idempotency_key,
        )
        _enqueue_and_notify_post_primary()
        return result
    except ConsiliumCycleNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    except ConsiliumContextMissingError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc
    except (
        ConsiliumConcurrencyConflictError,
        ReportVersionConflictError,
    ) as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc
    except ConsiliumQualityRejectionError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    except GeminiGatewayError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Gemini cascade execution failed: {exc}",
        ) from exc
    except ConsiliumError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc


@router.post(
    "/cascade/run-weekly",
    response_model=WeeklyConsiliumClientResponse,
    summary="Run or recover Weekly Consilium for authenticated patient (Patient-Isolated)",
    description=(
        "Executes or recovers the Weekly Consilium for the authenticated patient's active cycle. "
        "Strictly requires DeviceSession with cascade:run scope. "
        "Protected by distributed SQLite lease, background heartbeat renewal, and fencing. "
        "Idempotent: retries of a completed operation return results directly without re-running Gemini."
    ),
)
async def run_client_weekly_consilium(
    request: Request,
    response: Response,
    payload: Optional[ClientWeeklyConsiliumRunRequest] = None,
    cycle: MedicalCycle = Depends(resolve_active_cycle_for_patient),
    db: Session = Depends(get_db),
    _scope: DeviceSession = Depends(require_scopes(SCOPE_CASCADE_RUN)),
    gateway: GeminiGateway = Depends(get_gemini_gateway),
    now_provider: Callable[[], datetime] = Depends(get_server_now_provider),
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
) -> WeeklyConsiliumClientResponse:
    primary_service = PrimaryConsiliumService(
        db=db,
        gateway=gateway,
        now_provider=now_provider,
    )
    if not primary_service.is_primary_consilium_completed(cycle.cycle_id):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Cannot run Weekly Consilium for cycle '{cycle.cycle_id}': "
                "Primary Consilium must be completed before running Weekly Consilium."
            ),
        )

    service = WeeklyConsiliumService(
        db=db,
        gateway=gateway,
        now_provider=now_provider,
    )
    sprint_id = payload.sprint_id if payload else None
    report_date = payload.report_date if payload else None

    # Idempotent replay check before running
    if idempotency_key and idempotency_key.strip():
        existing_logs = service.report_repo.list_server_only(
            cycle_id=cycle.cycle_id,
            report_type=WEEKLY_CONSILIUM_EXECUTION_LOG_REPORT_TYPE,
        )
        for log in existing_logs:
            if (log.content or {}).get("idempotency_key") == idempotency_key.strip():
                recovered = service.recover_completed_response(
                    cycle.cycle_id, sprint_id=log.sprint_id
                )
                if recovered is not None:
                    response.headers["X-Aegis-Idempotent-Replay"] = "true"
                    return recovered

    # 2. Authoritative Period Resolution & Replay / Validation
    lifecycle = service.get_weekly_sprint_lifecycle_state(cycle.cycle_id)
    clean_sprint_id = sprint_id.strip() if sprint_id and sprint_id.strip() else None

    if clean_sprint_id:
        existing_sprint = service.sprint_repo.get(clean_sprint_id)
        if existing_sprint is not None:
            if existing_sprint.cycle_id != cycle.cycle_id:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"Sprint '{clean_sprint_id}' belongs to a different clinical cycle.",
                )
            if existing_sprint.status == "completed":
                recovered = service.recover_completed_response(
                    cycle.cycle_id, sprint_id=clean_sprint_id
                )
                if recovered is not None:
                    response.headers["X-Aegis-Idempotent-Replay"] = "true"
                    return recovered
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"Sprint '{clean_sprint_id}' is marked completed but client reports could not be recovered.",
                )
            # Revalidate server-side eligibility for unfinished or pre-created active sprints:
            # An active record in DB must NEVER bypass the fixed 168-hour scheduling boundary!
            if not lifecycle.can_start_next_sprint or clean_sprint_id != lifecycle.next_sprint_id:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=(
                        f"Cannot run weekly consilium for sprint '{clean_sprint_id}': "
                        f"scheduling boundary has not elapsed or sprint transition is not authorized."
                    ),
                )
            target_sprint_id = clean_sprint_id
        else:
            # Client requested a sprint that does not exist in DB yet.
            # Client cannot invent unauthorized sprints or force arrival of next week.
            if not lifecycle.can_start_next_sprint or clean_sprint_id != lifecycle.next_sprint_id:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=(
                        f"Cannot start sprint '{clean_sprint_id}': "
                        f"transition to this weekly sprint is not authorized by server authority."
                    ),
                )
            target_sprint_id = clean_sprint_id
    else:
        # Client did not specify sprint_id -> server resolves authoritative target
        if lifecycle.is_weekly_consilium_completed and lifecycle.current_weekly_sprint_id is not None:
            # Replay latest completed sprint without re-running Gemini
            recovered = service.recover_completed_response(
                cycle.cycle_id, sprint_id=lifecycle.current_weekly_sprint_id
            )
            if recovered is not None:
                response.headers["X-Aegis-Idempotent-Replay"] = "true"
                return recovered
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"Sprint '{lifecycle.current_weekly_sprint_id}' is completed "
                    "but client reports could not be recovered."
                ),
            )
        elif lifecycle.can_start_next_sprint and lifecycle.next_sprint_id is not None:
            target_sprint_id = lifecycle.next_sprint_id
        else:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Weekly Consilium is not eligible to start for this clinical cycle.",
            )

    try:
        weekly_result = await service.run_weekly_consilium(
            cycle_id=cycle.cycle_id,
            sprint_id=target_sprint_id,
            report_date=report_date,
            idempotency_key=idempotency_key,
        )
        if service.needs_referral_document_generation(cycle.cycle_id):
            try:
                automation_service = ClinicalAutomationService(
                    db=db,
                    gateway=gateway,
                    now_provider=now_provider,
                )
                automation_service.enqueue_referral_document_generation(
                    cycle_id=cycle.cycle_id,
                    patient_id=cycle.patient_id,
                )
                worker = getattr(request.app.state, "automation_worker", None)
                if worker is not None and hasattr(worker, "notify"):
                    worker.notify()
            except Exception:
                pass
        return weekly_result
    except ConsiliumCycleNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    except ConsiliumContextMissingError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc
    except (
        ConsiliumConcurrencyConflictError,
        ReportVersionConflictError,
    ) as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc
    except ConsiliumQualityRejectionError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    except GeminiGatewayError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Gemini Weekly cascade execution failed: {exc}",
        ) from exc
    except ConsiliumError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
