from datetime import datetime, timezone
from typing import Callable, Dict, Optional
from fastapi import APIRouter, Depends, Header, HTTPException, Response, status
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
    LabLedgerStateResponse,
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
    ConsiliumAnamnesisLockedError,
    ConsiliumConcurrencyConflictError,
    PREPARATORY_ANAMNESIS_REPORT_TYPE,
    PREPARATORY_ECG_REPORT_TYPE,
)
from app.services.lab_revision_consilium_service import (
    LabRevisionConsiliumService,
    LABORATORY_LEDGER_REPORT_TYPE,
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
    "/cascade/run-primary",
    response_model=PrimaryConsiliumClientResponse,
    summary="Run or recover Primary Consilium for authenticated patient (Patient-Isolated)",
    description=(
        "If Primary Consilium was already concluded, safely recovers completed "
        "client-facing reports without repeating Gemini calls (`X-Aegis-Idempotent-Replay: true`). "
        "Fresh un-fenced execution is disabled with 503 pending Stage 5.4E distributed lease protocol."
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
    service = PrimaryConsiliumService(db=db, gateway=gateway)

    # 1. Result Recovery Guard: If Primary Consilium is completed, recover existing reports immediately
    if service.is_primary_consilium_completed(cycle.cycle_id):
        recovered = _recover_primary_consilium_client_response(db, cycle.cycle_id)
        if recovered is not None:
            response.headers["X-Aegis-Idempotent-Replay"] = "true"
            return recovered

    # 2. Concurrency & Idempotency Blocker Guard (Per Project Aegis Invariant):
    # Un-fenced concurrent execution of expensive multi-agent cascades without
    # atomic lease acquisition, heartbeat renewal, and write fencing is prohibited.
    raise HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail={
            "error": "Primary Consilium fresh execution via client API is temporarily inactive pending Stage 5.4E distributed lease & fencing protocol.",
            "status": "BLOCKED_PENDING_DISTRIBUTED_LEASE",
            "cycle_id": cycle.cycle_id,
            "blocker_code": "PRIMARY_CONSILIUM_LEASE_GUARD_REQUIRED",
            "missing_guarantees": [
                "atomic_lease_acquisition_with_lease_owner_id",
                "background_heartbeat_lease_renewal",
                "fencing_write_protection_against_stale_owners",
                "persistent_idempotency_table_for_client_retry",
            ],
            "action_required": "Scheduled for implementation in Stage 5.4E with distributed lease protocol.",
        },
    )


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
