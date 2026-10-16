from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from app.database import get_db
from app.repositories.cycle_repository import MedicalCycleRepository
from app.schemas.cycle import MedicalCycleCreate, MedicalCycleResponse, MedicalCycleStatusUpdate
from app.security.auth import verify_client_token

router = APIRouter(prefix="/api/v1/cycle", tags=["Medical Cycles"])


@router.post(
    "/start",
    response_model=MedicalCycleResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Start a new medical cycle (Protected)",
)
def start_cycle(
    payload: MedicalCycleCreate,
    db: Session = Depends(get_db),
    _token: str = Depends(verify_client_token),
) -> MedicalCycleResponse:
    repo = MedicalCycleRepository(db)
    cycle = repo.create(cycle_id=payload.cycle_id, status=payload.status)
    return cycle


@router.get(
    "/current",
    response_model=MedicalCycleResponse,
    summary="Get current active medical cycle (Protected)",
)
def get_current_cycle(
    db: Session = Depends(get_db),
    _token: str = Depends(verify_client_token),
) -> MedicalCycleResponse:
    repo = MedicalCycleRepository(db)
    active = repo.get_active()
    if not active:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No active medical cycle found.",
        )
    return active


@router.post(
    "/{cycle_id}/close",
    response_model=MedicalCycleResponse,
    summary="Close a medical cycle (Protected)",
)
def close_cycle(
    cycle_id: str,
    db: Session = Depends(get_db),
    _token: str = Depends(verify_client_token),
) -> MedicalCycleResponse:
    repo = MedicalCycleRepository(db)
    cycle = repo.close_cycle(cycle_id)
    if not cycle:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Medical cycle '{cycle_id}' not found.",
        )
    return cycle


@router.post(
    "/{cycle_id}/archive",
    response_model=MedicalCycleResponse,
    summary="Soft-archive a medical cycle without deleting data (Protected)",
)
def archive_cycle(
    cycle_id: str,
    db: Session = Depends(get_db),
    _token: str = Depends(verify_client_token),
) -> MedicalCycleResponse:
    repo = MedicalCycleRepository(db)
    cycle = repo.archive_cycle(cycle_id)
    if not cycle:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Medical cycle '{cycle_id}' not found.",
        )
    return cycle
