from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy.orm import Session
from app.database import get_db
from app.repositories.report_repository import ModelReportRepository
from app.schemas.reports import ClientModelReportResponse
from app.security.auth import verify_client_token

router = APIRouter(prefix="/api/v1/reports", tags=["Model Reports"])


@router.get(
    "/client",
    response_model=List[ClientModelReportResponse],
    summary="Get client-facing model reports for a cycle (Protected)",
    description="Returns strictly client_facing reports. Server-only private clinical reasoning is excluded.",
)
def get_client_reports(
    cycle_id: str = Query(..., description="Target medical cycle identifier"),
    report_type: Optional[str] = Query(None, description="Optional report type filter"),
    db: Session = Depends(get_db),
    _token: str = Depends(verify_client_token),
) -> List[ClientModelReportResponse]:
    repo = ModelReportRepository(db)
    reports = repo.list_client_facing(cycle_id=cycle_id, report_type=report_type)
    return [ClientModelReportResponse.model_validate(r) for r in reports]


@router.get(
    "/client/{report_id}",
    response_model=ClientModelReportResponse,
    summary="Get single client-facing report by ID (Protected)",
    description="Strictly rejects any attempt to fetch server_only private clinical reports with 403 Forbidden.",
)
def get_single_client_report(
    report_id: str,
    db: Session = Depends(get_db),
    _token: str = Depends(verify_client_token),
) -> ClientModelReportResponse:
    repo = ModelReportRepository(db)
    try:
        report = repo.get_client_facing(report_id)
    except PermissionError as e:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=str(e),
        )

    if not report:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Report '{report_id}' not found.",
        )

    return ClientModelReportResponse.model_validate(report)
