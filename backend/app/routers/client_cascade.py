from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional
from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    Header,
    HTTPException,
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
from app.schemas.consilium import (
    AnamnesisQuestionnaireAnswers,
    AnamnesisIntakeClientResponse,
    ClientVitalsThresholds,
    EcgVisionIntakeClientResponse,
    LabLedgerStateResponse,
    LabVisionIntakeClientResponse,
    PrimaryConsiliumClientResponse,
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
from app.services.lab_vision_ocr_service import (
    LabVisionOcrService,
    LabVisionValidationError,
)
from app.services.ecg_vision_service import (
    EcgVisionService,
    EcgVisionValidationError,
)

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
) -> ClientCascadeStatusResponse:
    cycle_repo = MedicalCycleRepository(db)
    active_cycle = cycle_repo.get_active_for_patient(session.patient_id)

    if active_cycle is None:
        return ClientCascadeStatusResponse(
            patient_id=session.patient_id,
            device_id=session.device_id,
            has_active_cycle=False,
        )

    cycle_id = active_cycle.cycle_id
    report_repo = ModelReportRepository(db)
    primary_service = PrimaryConsiliumService(db=db, gateway=gateway)
    lab_service = LabRevisionConsiliumService(db=db, gateway=gateway)

    has_m1 = len(report_repo.list_server_only(cycle_id=cycle_id, report_type=PREPARATORY_ANAMNESIS_REPORT_TYPE)) > 0
    has_ecg = len(report_repo.list_server_only(cycle_id=cycle_id, report_type=PREPARATORY_ECG_REPORT_TYPE)) > 0
    has_labs = len(report_repo.list_server_only(cycle_id=cycle_id, report_type=LABORATORY_LEDGER_REPORT_TYPE)) > 0
    is_completed = primary_service.is_primary_consilium_completed(cycle_id)

    ledger_state = lab_service.get_lab_ledger_state(cycle_id)
    has_unreviewed = ledger_state.has_unreviewed_lab_changes

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

    # 2. Process lab images via LabVisionOcrService with atomic ledger integration
    service = LabVisionOcrService(
        db=db,
        gateway=gateway,
        now_provider=now_provider,
    )
    try:
        return await service.process_lab_images(
            cycle_id=cycle.cycle_id,
            files=upload_files,
            is_correction=is_correction,
            sample_date_override=sample_date,
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
    except LabVisionValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    except GeminiGatewayError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Gemini Vision OCR extraction failed. Please retry later.",
        ) from exc
    except ConsiliumError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc


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

    # 1. Result Recovery Guard: If Primary Consilium is completed, recover existing reports immediately
    if service.is_primary_consilium_completed(cycle.cycle_id):
        recovered = service.recover_completed_response(cycle.cycle_id)
        if recovered is not None:
            response.headers["X-Aegis-Idempotent-Replay"] = "true"
            return recovered

    # 2. Protected Execution under distributed SQLite lease and fencing
    try:
        return await service.run_primary_consilium(
            cycle_id=cycle.cycle_id,
            idempotency_key=idempotency_key,
        )
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
    summary="Run Weekly Consilium for authenticated patient (Disabled pending Stage 5.4F)",
)
async def run_client_weekly_consilium(
    cycle: MedicalCycle = Depends(resolve_active_cycle_for_patient),
    _scope: DeviceSession = Depends(require_scopes(SCOPE_CASCADE_RUN)),
):
    raise HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail={
            "error": "Weekly Consilium execution via client API is disabled pending Stage 5.4F distributed lease & sprint protocol.",
            "status": "BLOCKED_PENDING_DISTRIBUTED_LEASE",
            "cycle_id": cycle.cycle_id,
            "blocker_code": "WEEKLY_CONSILIUM_LEASE_GUARD_REQUIRED",
            "action_required": "Scheduled for implementation in Stage 5.4F.",
        },
    )
