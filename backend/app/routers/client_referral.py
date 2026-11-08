"""
Stage 8A.3 — Client Referral Documents Router.

Provides authenticated mobile endpoints for:
1. GET /api/v1/client/referral/documents
   Listing all doctor observation documents across all medical cycles of the patient.
2. GET /api/v1/client/referral/documents/{report_id}
   Retrieving a structured, client-safe detail view of an observation document
   for rendering in Flutter Safe.
3. GET /api/v1/client/referral/documents/{report_id}/export?format=pdf|docx|md
   Exporting the observation summary as a downloadable file (PDF, DOCX, Markdown).

Security & Invariants:
- Strict mobile device session authentication via `require_device_session`.
- Enforces `SCOPE_REPORTS_READ` token scope.
- Strict Anti-IDOR protection: cycle patient ownership is verified before any report
  detail or export is returned. Any mismatch or non-existent record returns 404 Not Found
  with zero oracle leakage (no 403 / existence disclosure).
- Deterministic: 0 LLM calls, 0 Flash-Lite calls, 0 database mutations on download.
"""

from datetime import datetime, timezone
import logging
import re
from typing import Any, Dict, List, Optional
from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Query,
    Response,
    status,
)
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.model_report import ModelReport
from app.repositories.cycle_repository import MedicalCycleRepository
from app.repositories.report_repository import ModelReportRepository
from app.schemas.consilium import REFERRAL_DOCUMENT_CANONICAL_TITLE
from app.schemas.privacy import PrivacyLevel
from app.schemas.referral_documents import (
    ClientReferralDocumentDetailResponse,
    ClientReferralDocumentItem,
    ClientReferralDocumentListResponse,
    ClientReferralDocumentSection,
)
from app.security.auth import (
    DeviceSession,
    require_device_session,
    require_scopes,
)
from app.security.tokens import SCOPE_REPORTS_READ
from app.services.doctor_document_export_service import DoctorDocumentExportService
from app.services.referral_document_service import (
    REFERRAL_DOCTOR_DOCUMENT_REPORT_TYPE,
    REFERRAL_DOCTOR_DOCUMENT_RAW_REPORT_TYPE,
)

logger = logging.getLogger("aegis.router.client_referral")

router = APIRouter(prefix="/api/v1/client/referral", tags=["Client Referral Documents"])

SUPPORTED_EXPORT_FORMATS = ("pdf", "docx", "md")

MEDIA_TYPES = {
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "md": "text/markdown; charset=utf-8",
}


def _verify_and_resolve_client_document(
    report_id: str,
    session: DeviceSession,
    db: Session,
) -> tuple[ModelReport, Any]:
    """
    Resolves client-facing referral document and verifies patient ownership with
    Zero Oracle Leakage. Returns (client_doc, cycle) if valid, otherwise raises 404.
    """
    clean_id = (report_id or "").strip()
    if not clean_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Referral document not found.",
        )

    report_repo = ModelReportRepository(db)
    client_doc = report_repo.get(clean_id)
    if (
        client_doc is None
        or client_doc.privacy_level != PrivacyLevel.CLIENT_FACING.value
        or client_doc.report_type != REFERRAL_DOCTOR_DOCUMENT_REPORT_TYPE
    ):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Referral document not found.",
        )

    cycle_repo = MedicalCycleRepository(db)
    cycle = cycle_repo.get(client_doc.cycle_id)
    if cycle is None or cycle.patient_id != session.patient_id:
        # Anti-IDOR: Raise 404 rather than 403 to prevent oracle leakage of report existence
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Referral document not found.",
        )

    return client_doc, cycle


@router.get(
    "/documents",
    response_model=ClientReferralDocumentListResponse,
    summary="List all doctor observation documents for the authenticated patient across all cycles",
)
def list_client_referral_documents(
    session: DeviceSession = Depends(require_device_session),
    _: Any = Depends(require_scopes(SCOPE_REPORTS_READ)),
    db: Session = Depends(get_db),
) -> ClientReferralDocumentListResponse:
    """
    Returns all doctor observation summaries generated for this patient across
    both active and closed/completed medical cycles.
    """
    cycle_repo = MedicalCycleRepository(db)
    report_repo = ModelReportRepository(db)

    # 1. Retrieve all cycles belonging to authenticated patient
    cycles = cycle_repo.list_by_patient(session.patient_id)
    cycle_map = {c.cycle_id: c for c in cycles}

    document_items: List[ClientReferralDocumentItem] = []

    # 2. Collect referral documents from each cycle
    for cycle in cycles:
        cycle_docs = report_repo.list_client_facing(
            cycle_id=cycle.cycle_id,
            report_type=REFERRAL_DOCTOR_DOCUMENT_REPORT_TYPE,
        )
        for doc in cycle_docs:
            content = doc.content or {}
            metrics = content.get("metrics") or {}

            # Format specialties as list
            raw_specs = metrics.get("target_specialties") or []
            if isinstance(raw_specs, str):
                specs = [s.strip() for s in raw_specs.split(",") if s.strip()]
            elif isinstance(raw_specs, list):
                specs = [str(s).strip() for s in raw_specs if str(s).strip()]
            else:
                specs = []

            # Executive summary for listing
            exec_summary = ""
            entries = content.get("entries") or []
            if entries and isinstance(entries, list) and entries[0].get("summary"):
                exec_summary = str(entries[0]["summary"])
            elif content.get("highlights"):
                exec_summary = str(content["highlights"][0])
            else:
                exec_summary = "Observation summary for doctor consultation."

            created_iso = (
                metrics.get("created_at_utc")
                or doc.created_at.strftime("%Y-%m-%dT%H:%M:%SZ")
            )
            report_date = content.get("report_date") or doc.created_at.strftime("%Y-%m-%d")

            item = ClientReferralDocumentItem(
                report_id=doc.report_id,
                cycle_id=doc.cycle_id,
                sprint_id=doc.sprint_id or metrics.get("sprint_id"),
                referral_id=metrics.get("referral_id", "REF-UNKNOWN"),
                created_at_utc=created_iso,
                report_date=report_date,
                title=content.get("title") or REFERRAL_DOCUMENT_CANONICAL_TITLE,
                available_formats=list(SUPPORTED_EXPORT_FORMATS),
                summary=exec_summary,
                urgency=str(metrics.get("urgency", "routine")),
                target_specialties=specs,
                cycle_status=cycle.status,
                document_version=doc.version,
            )
            document_items.append(item)

    # Sort documents globally by creation timestamp descending
    document_items.sort(key=lambda d: d.created_at_utc, reverse=True)

    return ClientReferralDocumentListResponse(
        patient_id=session.patient_id,
        total_count=len(document_items),
        documents=document_items,
    )


@router.get(
    "/documents/{report_id}",
    response_model=ClientReferralDocumentDetailResponse,
    summary="Get structured doctor observation document details for mobile Safe UI",
)
def get_client_referral_document_detail(
    report_id: str,
    session: DeviceSession = Depends(require_device_session),
    _: Any = Depends(require_scopes(SCOPE_REPORTS_READ)),
    db: Session = Depends(get_db),
) -> ClientReferralDocumentDetailResponse:
    """
    Returns structured observation document content with 7 typed sections,
    informational disclaimer, and rendered markdown for Flutter Safe display.
    """
    client_doc, cycle = _verify_and_resolve_client_document(report_id, session, db)

    export_service = DoctorDocumentExportService(db)
    pro_doc, meta = export_service.extract_document_data(client_doc)

    sections = [
        ClientReferralDocumentSection(
            section_number=1,
            section_title="Patient and Observation Period",
            content=pro_doc.patient_and_observation_period,
            is_bullet_list=False,
        ),
        ClientReferralDocumentSection(
            section_number=2,
            section_title="Reason for Recommending an In-Person Consultation",
            content=pro_doc.reason_for_in_person_consultation,
            is_bullet_list=False,
        ),
        ClientReferralDocumentSection(
            section_number=3,
            section_title="Objective Health Measurements and Examination Results",
            content=pro_doc.objective_measurements_and_examination_results,
            is_bullet_list=True,
        ),
        ClientReferralDocumentSection(
            section_number=4,
            section_title="Timeline of Reported Symptoms and Observed Changes",
            content=pro_doc.timeline_of_reported_symptoms_and_changes,
            is_bullet_list=True,
        ),
        ClientReferralDocumentSection(
            section_number=5,
            section_title="Important Patterns and Unresolved Concerns",
            content=pro_doc.important_patterns_and_unresolved_concerns,
            is_bullet_list=True,
        ),
        ClientReferralDocumentSection(
            section_number=6,
            section_title="AI-Generated Observations Requiring Professional Verification",
            content=pro_doc.ai_observations_requiring_verification,
            is_bullet_list=True,
        ),
        ClientReferralDocumentSection(
            section_number=7,
            section_title="Questions and Possible Examinations to Discuss with a Licensed Clinician",
            content=pro_doc.questions_and_examinations_for_clinician,
            is_bullet_list=True,
        ),
    ]

    return ClientReferralDocumentDetailResponse(
        report_id=client_doc.report_id,
        cycle_id=client_doc.cycle_id,
        sprint_id=client_doc.sprint_id or meta.get("sprint_id"),
        referral_id=meta.get("referral_id", "REF-UNKNOWN"),
        created_at_utc=meta.get("created_at_utc", ""),
        report_date=meta.get("report_date", ""),
        title=meta.get("title", REFERRAL_DOCUMENT_CANONICAL_TITLE),
        urgency=pro_doc.consultation_urgency,
        target_specialties=pro_doc.recommended_specialties,
        informational_disclaimer=pro_doc.informational_disclaimer,
        executive_summary=pro_doc.executive_summary_for_patient,
        sections=sections,
        rendered_markdown=pro_doc.render_english_markdown(),
        available_formats=list(SUPPORTED_EXPORT_FORMATS),
        cycle_status=cycle.status,
        document_version=client_doc.version,
    )


@router.get(
    "/documents/{report_id}/export",
    summary="Download doctor observation document as PDF, DOCX, or Markdown",
)
def export_client_referral_document(
    report_id: str,
    format: str = Query(..., description="Export format: pdf, docx, or md"),
    session: DeviceSession = Depends(require_device_session),
    _: Any = Depends(require_scopes(SCOPE_REPORTS_READ)),
    db: Session = Depends(get_db),
) -> Response:
    """
    Streams the pre-generated document binary directly to client with
    Content-Disposition attachment headers.
    """
    clean_format = (format or "").strip().lower()
    if clean_format not in SUPPORTED_EXPORT_FORMATS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Unsupported export format '{format}'. "
                f"Supported formats are: {', '.join(SUPPORTED_EXPORT_FORMATS)}."
            ),
        )

    client_doc, cycle = _verify_and_resolve_client_document(report_id, session, db)

    export_service = DoctorDocumentExportService(db)
    pro_doc, meta = export_service.extract_document_data(client_doc)

    referral_id = meta.get("referral_id", "observation")
    safe_ref = re.sub(r"[^a-zA-Z0-9_\-]", "_", referral_id)[:16]
    report_date = meta.get("report_date", datetime.now(timezone.utc).strftime("%Y-%m-%d"))
    filename = f"aegis-referral-{safe_ref}-{report_date}.{clean_format}"

    if clean_format == "pdf":
        file_bytes = export_service.export_pdf(pro_doc, meta)
    elif clean_format == "docx":
        file_bytes = export_service.export_docx(pro_doc, meta)
    elif clean_format == "md":
        md_text = export_service.export_markdown(pro_doc, meta)
        file_bytes = md_text.encode("utf-8")
    else:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unsupported export format '{clean_format}'.",
        )

    media_type = MEDIA_TYPES.get(clean_format, "application/octet-stream")
    headers = {
        "Content-Disposition": f'attachment; filename="{filename}"',
        "Content-Length": str(len(file_bytes)),
        "X-Aegis-Report-Id": client_doc.report_id,
        "X-Aegis-Referral-Id": referral_id,
    }

    return Response(content=file_bytes, media_type=media_type, headers=headers)
