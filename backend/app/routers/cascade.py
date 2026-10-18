from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.database import get_db
from app.schemas.consilium import (
    PrimaryConsiliumRunRequest,
    PrimaryConsiliumClientResponse,
    WeeklyConsiliumRunRequest,
    WeeklyConsiliumClientResponse,
)
from app.security.auth import verify_client_token
from app.services.gemini_gateway import GeminiGateway, GeminiGatewayError
from app.services.primary_consilium_service import (
    PrimaryConsiliumService,
    ConsiliumError,
    ConsiliumCycleNotFoundError,
    ConsiliumContextMissingError,
    ConsiliumQualityRejectionError,
)
from app.services.weekly_consilium_service import WeeklyConsiliumService

router = APIRouter(prefix="/api/v1/cascade", tags=["Medical Consilium Cascade"])


def get_gemini_gateway() -> GeminiGateway:
    """Dependency provider for GeminiGateway (overrideable in tests)."""
    return GeminiGateway()


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
