from datetime import datetime, timezone
from typing import Callable, List, Optional
from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile, status
from sqlalchemy.orm import Session

from app.database import get_db
from app.schemas.consilium import (
    AnamnesisIntakeRequest,
    AnamnesisIntakeClientResponse,
    PrimaryConsiliumRunRequest,
    PrimaryConsiliumClientResponse,
    WeeklyConsiliumRunRequest,
    WeeklyConsiliumClientResponse,
    LabLedgerUpsertRequest,
    LabLedgerStateResponse,
    LabRevisionCooldownStatus,
    LabRevisionConsiliumRunRequest,
    LabRevisionConsiliumClientResponse,
    LabVisionIntakeClientResponse,
)
from app.security.auth import verify_client_token
from app.services.gemini_gateway import GeminiGateway, GeminiGatewayError
from app.services.primary_consilium_service import (
    PrimaryConsiliumService,
    ConsiliumError,
    ConsiliumCycleNotFoundError,
    ConsiliumContextMissingError,
    ConsiliumQualityRejectionError,
    ConsiliumAnamnesisLockedError,
    ConsiliumConcurrencyConflictError,
)
from app.services.weekly_consilium_service import WeeklyConsiliumService
from app.services.lab_revision_consilium_service import (
    LabRevisionConsiliumService,
    ConsiliumCooldownError,
)
from app.services.lab_vision_ocr_service import (
    LabVisionOcrService,
    LabVisionValidationError,
)

router = APIRouter(prefix="/api/v1/cascade", tags=["Medical Consilium Cascade"])


def get_gemini_gateway() -> GeminiGateway:
    """Dependency provider for GeminiGateway (overrideable in tests)."""
    return GeminiGateway()


def get_server_now_provider() -> Callable[[], datetime]:
    """Dependency provider for authoritative server UTC clock (overrideable in tests)."""
    return lambda: datetime.now(timezone.utc)


@router.post(
    "/intake/anamnesis",
    response_model=AnamnesisIntakeClientResponse,
    summary="Submit raw questionnaire answers and run server-side Anamnesis M1 in strict blind mode",
    description=(
        "Authorized endpoint receiving raw answers from Flutter anamnesis cards. "
        "Strictly forbids AI conclusions, organ shelves, or laboratory data from the client. "
        "Reconstructs context on the server, runs Gemini Flash (M1) in blind mode, and atomically "
        "persists raw answers and verified M1 output under server_only privacy level. "
        "Returns only client-facing summary and metadata."
    ),
)
async def submit_anamnesis_intake_endpoint(
    payload: AnamnesisIntakeRequest,
    db: Session = Depends(get_db),
    gateway: GeminiGateway = Depends(get_gemini_gateway),
    _token: str = Depends(verify_client_token),
) -> AnamnesisIntakeClientResponse:
    service = PrimaryConsiliumService(db=db, gateway=gateway)
    try:
        return await service.process_anamnesis_intake(
            cycle_id=payload.cycle_id,
            answers=payload.answers,
        )
    except ConsiliumCycleNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        )
    except (ConsiliumAnamnesisLockedError, ConsiliumConcurrencyConflictError) as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        )
    except GeminiGatewayError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Gemini Anamnesis M1 execution failed: {exc}",
        )
    except ConsiliumError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        )


@router.post(
    "/run-primary",
    response_model=PrimaryConsiliumClientResponse,
    summary="Execute Primary Medical Consilium (8 Flash-Lite -> Deputy Flash -> Chief Physician Pro)",
    description=(
        "Executes the server-side Primary Consilium on already-saved preparatory records "
        "(Anamnesis M1, Vision OCR labs, Vision ECG, Vitals) for the specified cycle_id. "
        "Does not re-run Anamnesis or Vision internally. Returns only verified client-facing reports."
    ),
)
async def run_primary_consilium_endpoint(
    payload: PrimaryConsiliumRunRequest,
    db: Session = Depends(get_db),
    gateway: GeminiGateway = Depends(get_gemini_gateway),
    _token: str = Depends(verify_client_token),
) -> PrimaryConsiliumClientResponse:
    service = PrimaryConsiliumService(db=db, gateway=gateway)
    try:
        return await service.run_primary_consilium(
            cycle_id=payload.cycle_id,
            report_date=payload.report_date,
        )
    except ConsiliumCycleNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        )
    except ConsiliumContextMissingError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        )
    except ConsiliumQualityRejectionError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        )
    except GeminiGatewayError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Gemini cascade execution failed: {exc}",
        )


@router.post(
    "/run-weekly",
    response_model=WeeklyConsiliumClientResponse,
    summary="Execute Weekly Medical Consilium (Deputy Flash -> Chief Physician Pro)",
    description=(
        "Executes the server-side 7-day Weekly Consilium strictly via Gemini Flash -> Gemini Pro "
        "without calling the 8 Flash-Lite organ specialists, Vision, or Anamnesis. "
        "Updates the server-side active Athena strategy and returns only verified client-facing reports."
    ),
)
async def run_weekly_consilium_endpoint(
    payload: WeeklyConsiliumRunRequest,
    db: Session = Depends(get_db),
    gateway: GeminiGateway = Depends(get_gemini_gateway),
    _token: str = Depends(verify_client_token),
) -> WeeklyConsiliumClientResponse:
    service = WeeklyConsiliumService(db=db, gateway=gateway)
    try:
        return await service.run_weekly_consilium(
            cycle_id=payload.cycle_id,
            sprint_id=payload.sprint_id,
            report_date=payload.report_date,
        )
    except ConsiliumCycleNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        )
    except ConsiliumContextMissingError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        )
    except ConsiliumQualityRejectionError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        )
    except ConsiliumError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        )
    except GeminiGatewayError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Gemini weekly cascade execution failed: {exc}",
        )


@router.post(
    "/labs/upsert",
    response_model=LabLedgerStateResponse,
    summary="Add, update, or correct laboratory markers in the current medical cycle (no cooldown)",
    description=(
        "Saves new laboratory markers, new dated studies, or error corrections as a new "
        "immutable ledger version without erasing historical observations. "
        "Saving laboratory data is always allowed immediately with zero cooldown."
    ),
)
def upsert_laboratory_markers_endpoint(
    payload: LabLedgerUpsertRequest,
    db: Session = Depends(get_db),
    now_provider: Callable[[], datetime] = Depends(get_server_now_provider),
    _token: str = Depends(verify_client_token),
) -> LabLedgerStateResponse:
    service = LabRevisionConsiliumService(db=db, now_provider=now_provider)
    try:
        return service.upsert_laboratory_markers(
            cycle_id=payload.cycle_id,
            mutations=payload.mutations,
        )
    except ConsiliumCycleNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        )


@router.get(
    "/labs/state",
    response_model=LabLedgerStateResponse,
    summary="Get versioned laboratory ledger state and AI evaluation status",
)
def get_laboratory_ledger_state_endpoint(
    cycle_id: str = Query(..., min_length=3, max_length=64),
    db: Session = Depends(get_db),
    now_provider: Callable[[], datetime] = Depends(get_server_now_provider),
    _token: str = Depends(verify_client_token),
) -> LabLedgerStateResponse:
    service = LabRevisionConsiliumService(db=db, now_provider=now_provider)
    try:
        return service.get_lab_ledger_state(cycle_id=cycle_id)
    except ConsiliumCycleNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        )


@router.get(
    "/lab-revision-cooldown",
    response_model=LabRevisionCooldownStatus,
    summary="Check the 168-hour cooldown status for Full Lab-Revision Consilium",
)
def get_lab_revision_cooldown_endpoint(
    cycle_id: str = Query(..., min_length=3, max_length=64),
    db: Session = Depends(get_db),
    now_provider: Callable[[], datetime] = Depends(get_server_now_provider),
    _token: str = Depends(verify_client_token),
) -> LabRevisionCooldownStatus:
    service = LabRevisionConsiliumService(db=db, now_provider=now_provider)
    try:
        return service.get_cooldown_status(cycle_id=cycle_id)
    except ConsiliumCycleNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        )


@router.post(
    "/run-lab-revision",
    response_model=LabRevisionConsiliumClientResponse,
    summary="Execute Full Lab-Revision Consilium (8 Flash-Lite -> Deputy Flash -> Chief Physician Pro)",
    description=(
        "Executes the comparative Full Lab-Revision Consilium after laboratory data changes. "
        "Enforces a strict 168-hour server-time cooldown between successful user-initiated "
        "full revisions (independent of the 7-day Weekly Consilium). Returns 429 with remaining "
        "wait time and medical safety notice if blocked by cooldown without calling Gemini."
    ),
)
async def run_lab_revision_consilium_endpoint(
    payload: LabRevisionConsiliumRunRequest,
    db: Session = Depends(get_db),
    gateway: GeminiGateway = Depends(get_gemini_gateway),
    now_provider: Callable[[], datetime] = Depends(get_server_now_provider),
    _token: str = Depends(verify_client_token),
) -> LabRevisionConsiliumClientResponse:
    service = LabRevisionConsiliumService(
        db=db,
        gateway=gateway,
        now_provider=now_provider,
    )
    try:
        return await service.run_lab_revision_consilium(
            cycle_id=payload.cycle_id,
            report_date=payload.report_date,
        )
    except ConsiliumCooldownError as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=exc.cooldown_status.model_dump(),
        )
    except ConsiliumCycleNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        )
    except ConsiliumContextMissingError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        )
    except ConsiliumQualityRejectionError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        )
    except ConsiliumError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        )
    except GeminiGatewayError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Gemini lab-revision cascade execution failed: {exc}",
        )


@router.post(
    "/intake/lab-images",
    response_model=LabVisionIntakeClientResponse,
    summary="Upload multimodal laboratory report images and run server-side Vision OCR extraction",
    description=(
        "Authorized endpoint receiving 1 to 15 laboratory document images (JPEG, PNG, WEBP, HEIC/HEIF). "
        "Validates magic bytes, enforces a 10MB per-image and 30MB total size limit, invokes Gemini Vision "
        "to exhaustively extract laboratory markers, and synchronizes observations with the laboratory ledger "
        "without triggering or consuming the 168-hour revision cooldown."
    ),
)
async def submit_lab_images_intake_endpoint(
    cycle_id: str = Form(...),
    files: List[UploadFile] = File(...),
    is_correction: bool = Form(default=False),
    sample_date: Optional[str] = Form(default=None),
    db: Session = Depends(get_db),
    gateway: GeminiGateway = Depends(get_gemini_gateway),
    now_provider: Callable[[], datetime] = Depends(get_server_now_provider),
    _token: str = Depends(verify_client_token),
) -> LabVisionIntakeClientResponse:
    service = LabVisionOcrService(
        db=db,
        gateway=gateway,
        now_provider=now_provider,
    )
    try:
        return await service.process_lab_images(
            cycle_id=cycle_id,
            files=files,
            is_correction=is_correction,
            sample_date_override=sample_date,
        )
    except ConsiliumCycleNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        )
    except LabVisionValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        )
    except ConsiliumError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        )
    except GeminiGatewayError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Gemini Vision OCR extraction failed: {exc}",
        )
