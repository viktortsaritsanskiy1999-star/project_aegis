from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.database import get_db
from app.models.medical_cycle import MedicalCycle
from app.models.model_report import ModelReport
from app.repositories.report_repository import ModelReportRepository
from app.schemas.privacy import PrivacyLevel
from app.schemas.reports import ClientModelReportResponse
from app.security.auth import (
    GENERIC_REPORT_ACCESS_DENIED_DETAIL,
    AuthSession,
    DeviceSession,
    require_scopes,
    verify_cycle_access_for_session,
)

router = APIRouter(prefix="/api/v1/reports", tags=["Model Reports"])


@router.get(
    "/client",
    response_model=List[ClientModelReportResponse],
    summary="Get client-facing model reports for a cycle (Protected, Patient-Isolated)",
    description="Returns strictly client_facing reports for an authorized medical cycle. Server-only private clinical reasoning is excluded.",
)
def get_client_reports(
    cycle_id: str = Query(..., description="Target medical cycle identifier"),
    report_type: Optional[str] = Query(None, description="Optional report type filter"),
    db: Session = Depends(get_db),
    session: AuthSession = Depends(require_scopes("cycle:read", "reports:read")),
) -> List[ClientModelReportResponse]:
    # Enforce patient cycle ownership BEFORE reading any reports
    verify_cycle_access_for_session(db, session, cycle_id)

    repo = ModelReportRepository(db)
    reports = repo.list_client_facing(cycle_id=cycle_id, report_type=report_type)
    return [ClientModelReportResponse.model_validate(r) for r in reports]


@router.get(
    "/client/{report_id}",
    response_model=ClientModelReportResponse,
    summary="Get single client-facing report by ID (Protected, Patient-Isolated)",
    description="Strictly rejects any attempt to fetch server_only private clinical reports or another patient's reports with 403 Forbidden.",
)
def get_single_client_report(
    report_id: str,
    db: Session = Depends(get_db),
    session: AuthSession = Depends(require_scopes("cycle:read", "reports:read")),
) -> ClientModelReportResponse:
    repo = ModelReportRepository(db)

    if isinstance(session, DeviceSession):
        clean_report_id = report_id.strip() if isinstance(report_id, str) else ""
        # Enforce cycle ownership check BEFORE reading ModelReport content
        cycle = db.execute(
            select(MedicalCycle)
            .join(ModelReport, ModelReport.cycle_id == MedicalCycle.cycle_id)
            .where(ModelReport.report_id == clean_report_id)
            .execution_options(populate_existing=True)
        ).scalar_one_or_none()
        if (
            cycle is None
            or cycle.patient_id is None
            or cycle.patient_id != session.patient_id
        ):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=GENERIC_REPORT_ACCESS_DENIED_DETAIL,
            )
        report = repo.get(clean_report_id)
        if report is None or report.privacy_level != PrivacyLevel.CLIENT_FACING.value:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=GENERIC_REPORT_ACCESS_DENIED_DETAIL,
            )
        return ClientModelReportResponse.model_validate(report)

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
