from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from app.database import get_db
from app.repositories.cycle_repository import CycleOwnershipConflictError, MedicalCycleRepository
from app.schemas.cycle import MedicalCycleCreate, MedicalCycleResponse
from app.security.auth import (
    AuthSession,
    DeviceSession,
    ServiceAdminSession,
    require_scopes,
    require_service_admin_session,
)

router = APIRouter(prefix="/api/v1/cycle", tags=["Medical Cycles"])


@router.post(
    "/start",
    response_model=MedicalCycleResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Start a new medical cycle (Service Admin Only)",
)
def start_cycle(
    payload: MedicalCycleCreate,
    db: Session = Depends(get_db),
    _admin: ServiceAdminSession = Depends(require_service_admin_session),
) -> MedicalCycleResponse:
    repo = MedicalCycleRepository(db)
    try:
        cycle = repo.create(
            cycle_id=payload.cycle_id,
            status=payload.status,
            patient_id=payload.patient_id,
        )
    except CycleOwnershipConflictError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc
    return cycle


@router.get(
    "/current",
    response_model=MedicalCycleResponse,
    summary="Get current active medical cycle (Protected, Patient-Isolated)",
)
def get_current_cycle(
    db: Session = Depends(get_db),
    session: AuthSession = Depends(require_scopes("cycle:read")),
) -> MedicalCycleResponse:
    repo = MedicalCycleRepository(db)
    if isinstance(session, DeviceSession):
        active = repo.get_active_for_patient(session.patient_id)
    else:
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
    summary="Close a medical cycle (Service Admin Only)",
)
def close_cycle(
    cycle_id: str,
    db: Session = Depends(get_db),
    _admin: ServiceAdminSession = Depends(require_service_admin_session),
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
    summary="Soft-archive a medical cycle without deleting data (Service Admin Only)",
)
def archive_cycle(
    cycle_id: str,
    db: Session = Depends(get_db),
    _admin: ServiceAdminSession = Depends(require_service_admin_session),
) -> MedicalCycleResponse:
    repo = MedicalCycleRepository(db)
    cycle = repo.archive_cycle(cycle_id)
    if not cycle:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Medical cycle '{cycle_id}' not found.",
        )
    return cycle
