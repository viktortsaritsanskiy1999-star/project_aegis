from datetime import datetime, timedelta, timezone
import json
import logging
from pathlib import Path
import re
import secrets
from typing import Any, Dict, List, Optional, Tuple, Union
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select, func
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models.audit_review import ClinicalAIAuditReview
from app.models.health_measurement import HealthMeasurement
from app.models.medical_cycle import MedicalCycle
from app.models.model_report import ModelReport
from app.models.patient import Patient
from app.models.workout_session import WorkoutSession
from app.schemas.audit import (
    AdminLoginRequest,
    AdminLoginResponse,
    AuditRecord,
    AuditRecordSummary,
    CallInspectionDetailResponse,
    ClinicalReviewResponse,
    ClinicalReviewStatus,
    ClinicalReviewUpdateRequest,
    OrganStateSummary,
    PatientPreviewResponse,
    PersistedReportDetail,
    UnifiedTimelineEvent,
    UnifiedTimelineResponse,
)
from app.schemas.privacy import PrivacyLevel
from app.security.auth import (
    AuthSession,
    DeviceSession,
    ServiceAdminSession,
    require_service_admin_session,
    security_scheme,
)
from app.services.audit_trail_service import (
    AuditTrailService,
    get_audit_trail_service,
)

logger = logging.getLogger("aegis.admin_audit")

# Ephemeral in-memory admin sessions for secure browser access (no permanent master secret in JS)
_ephemeral_admin_sessions: Dict[str, datetime] = {}


def clean_expired_sessions() -> None:
    now = datetime.now(timezone.utc)
    expired = [k for k, exp in _ephemeral_admin_sessions.items() if exp <= now]
    for k in expired:
        _ephemeral_admin_sessions.pop(k, None)


def is_valid_ephemeral_session(token: str) -> bool:
    clean_expired_sessions()
    exp = _ephemeral_admin_sessions.get(token)
    return exp is not None and exp > datetime.now(timezone.utc)


def verify_localhost_client(request: Request) -> None:
    """Restricts administrative audit endpoints strictly to localhost (127.0.0.1 / ::1)."""
    client_host = request.client.host if request.client else "unknown"
    if client_host not in ("127.0.0.1", "::1", "localhost", "testclient"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Forbidden: Clinical AI Audit console is strictly bound to localhost (127.0.0.1). Request from '{client_host}' rejected.",
        )


router = APIRouter(tags=["Clinical AI Audit Trail"], dependencies=[Depends(verify_localhost_client)])


def require_admin_audit_session(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security_scheme),
    db: Session = Depends(get_db),
) -> ServiceAdminSession:
    """
    Administrative authorization dependency:
    1. Verifies localhost binding.
    2. Validates either:
       - an ephemeral admin session token (issued via /api/v1/admin/audit/login), OR
       - the master AEGIS_API_SECRET.
    3. Rejects mobile DeviceSession JWTs with 403 Forbidden.
    """
    verify_localhost_client(request)

    if credentials is None or not credentials.credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing administrative authorization header. Please log in.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    raw_token = credentials.credentials.strip()
    if not raw_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing administrative authorization token.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Strictly reject mobile JWT tokens
    if raw_token.count(".") == 2:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Mobile device session tokens are strictly forbidden from accessing the Clinical AI Audit Trail.",
        )

    # Check ephemeral session store
    clean_expired_sessions()
    if raw_token.startswith("adm_sess_"):
        exp = _ephemeral_admin_sessions.get(raw_token)
        if exp is not None:
            if exp > datetime.now(timezone.utc):
                return ServiceAdminSession()
            else:
                _ephemeral_admin_sessions.pop(raw_token, None)
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Ephemeral administrative session has expired. Please log in again.",
                    headers={"WWW-Authenticate": "Bearer"},
                )

    # Check master server API secret
    configured_secret = (settings.api_secret or "").strip()
    if not configured_secret:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Server API secret is unconfigured.",
        )

    if secrets.compare_digest(raw_token, configured_secret):
        return ServiceAdminSession()

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid administrative credentials.",
        headers={"WWW-Authenticate": "Bearer"},
    )


# ==============================================================================
# AUTHENTICATION & LOGIN
# ==============================================================================

@router.post(
    "/api/v1/admin/audit/login",
    response_model=AdminLoginResponse,
    summary="Exchange administrative secret for an ephemeral session token (Localhost Only)",
)
def admin_audit_login(
    payload: AdminLoginRequest,
    request: Request,
) -> AdminLoginResponse:
    """
    Exchanges AEGIS_API_SECRET for an ephemeral session token with a 2-hour TTL.
    Prevents leaking the master secret to browser JavaScript sessionStorage or script variables.
    """
    verify_localhost_client(request)
    configured_secret = (settings.api_secret or "").strip()
    if not configured_secret or not secrets.compare_digest(payload.secret.strip(), configured_secret):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid administrative secret.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token = f"adm_sess_{secrets.token_urlsafe(32)}"
    _ephemeral_admin_sessions[token] = datetime.now(timezone.utc) + timedelta(hours=2)
    return AdminLoginResponse(
        access_token=token,
        token_type="bearer",
        expires_in=7200,
    )


# ==============================================================================
# CORE AUDIT TRAIL API
# ==============================================================================

@router.get(
    "/api/v1/admin/audit/patients",
    response_model=List[str],
    summary="List patients with clinical cycles or audit records (Server Admin Only)",
)
def list_audit_patients(
    request: Request,
    db: Session = Depends(get_db),
    admin_session: ServiceAdminSession = Depends(require_service_admin_session),
    audit_service: AuditTrailService = Depends(get_audit_trail_service),
) -> List[str]:
    """Lists distinct patients from both the primary SQLite database and filesystem audit storage."""
    db_patients = set(db.scalars(select(Patient.patient_id)).all())
    cycle_patients = set(
        db.scalars(select(MedicalCycle.patient_id).where(MedicalCycle.patient_id.isnot(None))).all()
    )
    fs_patients = set(audit_service.list_patients())
    return sorted([p for p in (db_patients | cycle_patients | fs_patients) if p])


@router.get(
    "/api/v1/admin/audit/patients/{patient_id}/cycles",
    response_model=List[Dict[str, Any]],
    summary="List cycles for a patient including historical cycles (Server Admin Only)",
)
def list_patient_audit_cycles(
    patient_id: str,
    request: Request,
    db: Session = Depends(get_db),
    admin_session: ServiceAdminSession = Depends(require_service_admin_session),
    audit_service: AuditTrailService = Depends(get_audit_trail_service),
) -> List[Dict[str, Any]]:
    """Returns all cycles for a patient, preserving historical cycles across Neo reassessments."""
    clean_patient_id = audit_service.validate_identifier(patient_id, "patient_id")

    db_cycles = db.scalars(
        select(MedicalCycle)
        .where(MedicalCycle.patient_id == clean_patient_id)
        .order_by(MedicalCycle.created_at.asc())
    ).all()

    cycle_map: Dict[str, Dict[str, Any]] = {}
    for c in db_cycles:
        cycle_map[c.cycle_id] = {
            "cycle_id": c.cycle_id,
            "patient_id": c.patient_id,
            "status": c.status,
            "started_at": c.started_at.isoformat() if c.started_at else None,
            "closed_at": c.closed_at.isoformat() if c.closed_at else None,
            "source": "sqlite",
        }

    fs_cycles = audit_service.list_cycles(clean_patient_id)
    for cyc in fs_cycles:
        if cyc not in cycle_map:
            cycle_map[cyc] = {
                "cycle_id": cyc,
                "patient_id": clean_patient_id,
                "status": "historical",
                "started_at": None,
                "closed_at": None,
                "source": "filesystem",
            }

    return list(cycle_map.values())


@router.get(
    "/api/v1/admin/audit/cycles/{cycle_id}/stages",
    response_model=List[Dict[str, Any]],
    summary="List stages within a cycle (Server Admin Only)",
)
def list_cycle_stages(
    cycle_id: str,
    request: Request,
    db: Session = Depends(get_db),
    admin_session: ServiceAdminSession = Depends(require_service_admin_session),
    audit_service: AuditTrailService = Depends(get_audit_trail_service),
) -> List[Dict[str, Any]]:
    clean_cycle_id = audit_service.validate_identifier(cycle_id, "cycle_id")
    calls = audit_service.list_calls(cycle_id=clean_cycle_id)

    stage_counts: Dict[str, int] = {}
    for c in calls:
        stage_counts[c.stage] = stage_counts.get(c.stage, 0) + 1

    return [{"stage": stage, "call_count": count} for stage, count in stage_counts.items()]


@router.get(
    "/api/v1/admin/audit/cycles/{cycle_id}/calls",
    response_model=List[AuditRecordSummary],
    summary="List model calls for a cycle (Server Admin Only)",
)
def list_cycle_calls(
    cycle_id: str,
    request: Request,
    stage: Optional[str] = Query(None, description="Optional filter by stage"),
    sprint_id: Optional[str] = Query(None, description="Optional filter by sprint ID"),
    db: Session = Depends(get_db),
    admin_session: ServiceAdminSession = Depends(require_service_admin_session),
    audit_service: AuditTrailService = Depends(get_audit_trail_service),
) -> List[AuditRecordSummary]:
    clean_cycle_id = audit_service.validate_identifier(cycle_id, "cycle_id")
    summaries = audit_service.list_calls(cycle_id=clean_cycle_id, stage=stage, sprint_id=sprint_id)

    call_ids = [s.call_id for s in summaries]
    if call_ids:
        reviews = db.scalars(
            select(ClinicalAIAuditReview).where(ClinicalAIAuditReview.call_id.in_(call_ids))
        ).all()
        review_map = {r.call_id: r.status for r in reviews}
        for s in summaries:
            if s.call_id in review_map:
                s.review_status = review_map[s.call_id]

    return summaries


@router.get(
    "/api/v1/admin/audit/calls/{call_id}",
    response_model=CallInspectionDetailResponse,
    summary="Inspect complete call details, verbatim text, SQLite report, and review (Server Admin Only)",
)
def get_call_inspection_detail(
    call_id: str,
    request: Request,
    db: Session = Depends(get_db),
    admin_session: ServiceAdminSession = Depends(require_service_admin_session),
    audit_service: AuditTrailService = Depends(get_audit_trail_service),
) -> CallInspectionDetailResponse:
    clean_call_id = audit_service.validate_identifier(call_id, "call_id")
    call_record = audit_service.get_call(clean_call_id)
    if call_record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Audit record not found for call_id '{call_id}'.",
        )

    persisted_report: Optional[PersistedReportDetail] = None
    if call_record.report_id:
        report = db.scalar(
            select(ModelReport).where(ModelReport.report_id == call_record.report_id)
        )
        if report:
            persisted_report = PersistedReportDetail.model_validate(report)
    elif call_record.cycle_id:
        query = select(ModelReport).where(ModelReport.cycle_id == call_record.cycle_id)
        if call_record.sprint_id:
            query = query.where(ModelReport.sprint_id == call_record.sprint_id)
        report = db.scalars(query).first()
        if report:
            persisted_report = PersistedReportDetail.model_validate(report)

    review_record = db.scalar(
        select(ClinicalAIAuditReview).where(ClinicalAIAuditReview.call_id == clean_call_id)
    )
    review_response = (
        ClinicalReviewResponse.model_validate(review_record)
        if review_record is not None
        else None
    )

    return CallInspectionDetailResponse(
        call=call_record,
        persisted_report=persisted_report,
        review=review_response,
    )


@router.post(
    "/api/v1/admin/audit/calls/{call_id}/review",
    response_model=ClinicalReviewResponse,
    summary="Submit human clinical quality review for a model call (Server Admin Only)",
)
def submit_call_review(
    call_id: str,
    payload: ClinicalReviewUpdateRequest,
    request: Request,
    db: Session = Depends(get_db),
    admin_session: ServiceAdminSession = Depends(require_service_admin_session),
    audit_service: AuditTrailService = Depends(get_audit_trail_service),
) -> ClinicalReviewResponse:
    clean_call_id = audit_service.validate_identifier(call_id, "call_id")
    call_record = audit_service.get_call(clean_call_id)
    if call_record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Audit record not found for call_id '{call_id}'.",
        )

    if payload.status == ClinicalReviewStatus.REVIEWED_PASS:
        if not payload.reviewer_id or not payload.reviewer_id.strip():
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Medical quality PASS requires confirmation from a qualified reviewer (reviewer_id cannot be empty).",
            )

    review = db.scalar(
        select(ClinicalAIAuditReview).where(ClinicalAIAuditReview.call_id == clean_call_id)
    )

    now_utc = datetime.now(timezone.utc)
    if review is None:
        review = ClinicalAIAuditReview(
            review_id=f"rev_{uuid.uuid4().hex[:16]}",
            call_id=clean_call_id,
            patient_id=call_record.patient_id,
            cycle_id=call_record.cycle_id,
            sprint_id=call_record.sprint_id,
            stage=call_record.stage,
            status=payload.status.value,
            reviewer_id=payload.reviewer_id.strip() if payload.reviewer_id else None,
            reviewed_at=now_utc if payload.status != ClinicalReviewStatus.NOT_REVIEWED else None,
            notes=payload.notes,
            facts_accurate=payload.facts_accurate,
            units_correct=payload.units_correct,
            unjustified_conclusions_found=payload.unjustified_conclusions_found,
            cross_agent_consistent=payload.cross_agent_consistent,
            red_flags_identified=payload.red_flags_identified,
            strategy_aligns_with_inputs=payload.strategy_aligns_with_inputs,
            created_at=now_utc,
            updated_at=now_utc,
        )
        db.add(review)
    else:
        review.status = payload.status.value
        review.reviewer_id = payload.reviewer_id.strip() if payload.reviewer_id else review.reviewer_id
        review.reviewed_at = now_utc if payload.status != ClinicalReviewStatus.NOT_REVIEWED else review.reviewed_at
        review.notes = payload.notes
        if payload.facts_accurate is not None:
            review.facts_accurate = payload.facts_accurate
        if payload.units_correct is not None:
            review.units_correct = payload.units_correct
        if payload.unjustified_conclusions_found is not None:
            review.unjustified_conclusions_found = payload.unjustified_conclusions_found
        if payload.cross_agent_consistent is not None:
            review.cross_agent_consistent = payload.cross_agent_consistent
        if payload.red_flags_identified is not None:
            review.red_flags_identified = payload.red_flags_identified
        if payload.strategy_aligns_with_inputs is not None:
            review.strategy_aligns_with_inputs = payload.strategy_aligns_with_inputs
        review.updated_at = now_utc

    db.commit()
    db.refresh(review)
    return ClinicalReviewResponse.model_validate(review)


# ==============================================================================
# PATIENT PREVIEW — EXACT FLUTTER CLIENT-FACING REPORT
# ==============================================================================

_ORGAN_SYSTEMS = [
    ("cardiovascular", "Cardiovascular System"),
    ("respiratory", "Respiratory System"),
    ("nervous", "Nervous System"),
    ("musculoskeletal", "Musculoskeletal System"),
    ("gastrointestinal", "Gastrointestinal System"),
    ("immune", "Immune System"),
    ("endocrine", "Endocrine System"),
    ("integumentary", "Integumentary System"),
]


@router.get(
    "/api/v1/admin/audit/patients/{patient_id}/preview/{cycle_id}",
    response_model=PatientPreviewResponse,
    summary="Exact user-facing report preview matching Flutter app (Server Admin Only)",
)
def get_patient_preview(
    patient_id: str,
    cycle_id: str,
    request: Request,
    stage: Optional[str] = Query(None, description="Optional stage filter"),
    db: Session = Depends(get_db),
    admin_session: ServiceAdminSession = Depends(require_service_admin_session),
    audit_service: AuditTrailService = Depends(get_audit_trail_service),
) -> PatientPreviewResponse:
    """
    Assembles the exact client-facing presentation data delivered to the Flutter mobile app:
    - 8 Organ systems with status, highlights, metrics, and glow colors
    - Overall clinical strategy conclusion
    - Doctor referral banner & Safe documents
    - 4-Stage trace: Gemini Verbatim -> SERVER_ONLY -> CLIENT_FACING -> Flutter View
    """
    clean_pat = audit_service.validate_identifier(patient_id, "patient_id")
    clean_cyc = audit_service.validate_identifier(cycle_id, "cycle_id")

    # 1. Fetch cycle
    cycle = db.scalar(
        select(MedicalCycle).where(
            MedicalCycle.cycle_id == clean_cyc,
            MedicalCycle.patient_id == clean_pat,
        )
    )

    # 2. Fetch all reports for this cycle
    reports = db.scalars(
        select(ModelReport).where(ModelReport.cycle_id == clean_cyc)
    ).all()

    client_reports = [r for r in reports if r.privacy_level == PrivacyLevel.CLIENT_FACING.value]
    server_reports = [r for r in reports if r.privacy_level == PrivacyLevel.SERVER_ONLY.value]

    # Map reports by type and system
    client_by_type = {r.report_type: r for r in client_reports}

    # 3. Overall report
    overall_rep = None
    for cand in (
        "user_overall_report",
        "primary_consilium_overall",
        "weekly_consilium_overall",
        "neo_overall",
        "lab_revision_overall",
    ):
        if cand in client_by_type:
            overall_rep = client_by_type[cand]
            break
    if not overall_rep and client_reports:
        # Fallback to the latest version client report
        overall_rep = max(client_reports, key=lambda r: (r.version, r.created_at))

    overall_dict = None
    if overall_rep:
        overall_dict = {
            "report_id": overall_rep.report_id,
            "cycle_id": overall_rep.cycle_id,
            "report_type": overall_rep.report_type,
            "version": overall_rep.version,
            "model": overall_rep.model,
            "content": overall_rep.content,
            "created_at": overall_rep.created_at.isoformat(),
        }

    # 4. Map 8 Organ States
    organ_states: List[OrganStateSummary] = []
    system_reports_dict: Dict[str, Dict[str, Any]] = {}

    for sys_key, sys_title in _ORGAN_SYSTEMS:
        # Look for system report
        sys_rep = None
        for r in client_reports:
            rtype = r.report_type.lower()
            rcontent = r.content or {}
            if (
                rtype == f"{sys_key}_user"
                or rtype == f"user_system_{sys_key}"
                or f"_{sys_key}" in rtype
                or rcontent.get("system_key") == sys_key
            ):
                sys_rep = r
                break

        if sys_rep:
            cnt = sys_rep.content or {}
            title = cnt.get("title") or sys_title
            summary = cnt.get("summary") or ""
            highlights = cnt.get("highlights") or []
            metrics = cnt.get("metrics") or {}
            def_score = metrics.get("deficiency_score")
            if def_score is None:
                def_score = 1 if "attention" in summary.lower() else (2 if "critical" in summary.lower() or "spike" in summary.lower() else 0)

            if def_score >= 2 or "critical" in summary.lower() or "spike" in summary.lower():
                status_val = "critical"
                color_val = "#EF4444"  # Red
            elif def_score == 1 or "attention" in summary.lower() or "alert" in summary.lower():
                status_val = "attention"
                color_val = "#F59E0B"  # Amber
            else:
                status_val = "nominal"
                color_val = "#10B981"  # Green

            organ_state = OrganStateSummary(
                system_key=sys_key,
                title=title,
                status=status_val,
                color=color_val,
                deficiency_score=int(def_score),
                summary=summary,
                highlights=highlights,
                metrics=metrics,
                report_id=sys_rep.report_id,
                version=sys_rep.version,
            )
            organ_states.append(organ_state)
            system_reports_dict[sys_key] = cnt
        else:
            organ_states.append(
                OrganStateSummary(
                    system_key=sys_key,
                    title=sys_title,
                    status="nominal",
                    color="#38BDF8",  # Cyan neon
                    deficiency_score=0,
                    summary="No active observations reported for this organ system in this stage.",
                    highlights=[],
                    metrics={},
                    report_id=None,
                    version=1,
                )
            )

    # 5. Doctor Referral Banner & Documents
    referral_status = None
    referral_docs = []

    # Check for referral decision or doctor document reports
    for r in reports:
        if r.report_type in ("referral_doctor_document", "client_doctor_document"):
            cnt = r.content or {}
            metrics = cnt.get("metrics") or {}
            referral_docs.append({
                "report_id": r.report_id,
                "cycle_id": r.cycle_id,
                "document_type": "referral_doctor_document",
                "specialist_type": metrics.get("specialist_type") or "Cardiologist",
                "urgency": metrics.get("urgency") or "routine",
                "report_date": cnt.get("report_date") or r.created_at.strftime("%Y-%m-%d"),
                "summary": cnt.get("summary") or "Health Observation Summary for Your Doctor",
                "version": r.version,
            })
            if not referral_status:
                referral_status = {
                    "has_active_referral": True,
                    "specialist_type": metrics.get("specialist_type") or "Cardiologist",
                    "urgency": metrics.get("urgency") or "routine",
                    "reason": cnt.get("summary") or "Doctor referral recommended based on multidisciplinary analysis.",
                    "status": "document_ready",
                    "report_id": r.report_id,
                }
        elif r.report_type == "referral_decision":
            cnt = r.content or {}
            if cnt.get("recommended"):
                if not referral_status:
                    referral_status = {
                        "has_active_referral": True,
                        "specialist_type": cnt.get("specialist_type") or "Specialist",
                        "urgency": cnt.get("urgency") or "routine",
                        "reason": cnt.get("reason") or "Referral recommended.",
                        "status": "decision_recorded",
                        "report_id": r.report_id,
                    }

    # 6. Cascade status representation
    cascade_status = {
        "active_cycle_id": clean_cyc,
        "has_active_cycle": cycle is not None and cycle.status == "active",
        "cycle_status": cycle.status if cycle else "unknown",
        "has_preparatory_anamnesis": any(r.report_type.startswith("preparatory_anamnesis") for r in server_reports),
        "is_primary_consilium_completed": any(r.report_type in ("user_overall_report", "primary_consilium_overall") for r in client_reports),
        "has_active_referral": referral_status is not None,
        "referral_status": referral_status,
        "is_neo_reassessment": bool(cycle and getattr(cycle, "is_neo_reassessment", False)),
    }

    # 7. 4-Stage Trace Data Comparison
    trace_data = {}
    calls = audit_service.list_calls(patient_id=clean_pat, cycle_id=clean_cyc)
    sample_call = None
    if calls:
        # Prioritize master/chief or latest
        sample_call = next((c for c in reversed(calls) if "pro" in c.model_role or "master" in c.stage), calls[-1])
        full_call = audit_service.get_call(sample_call.call_id)
        if full_call:
            trace_data["gemini_raw_verbatim"] = full_call.raw_response_text
            trace_data["gemini_model_name"] = full_call.model_name
            trace_data["gemini_latency_ms"] = full_call.latency_ms
            trace_data["call_id"] = full_call.call_id

    # Server only representation
    if server_reports:
        latest_server = server_reports[-1]
        trace_data["server_only_report"] = {
            "report_id": latest_server.report_id,
            "report_type": latest_server.report_type,
            "content": latest_server.content,
        }

    # Client facing representation
    if overall_rep:
        trace_data["client_facing_report"] = {
            "report_id": overall_rep.report_id,
            "report_type": overall_rep.report_type,
            "content": overall_rep.content,
        }
        trace_data["flutter_view_data"] = {
            "rendered_title": overall_rep.content.get("title"),
            "rendered_summary": overall_rep.content.get("summary"),
            "rendered_highlights": overall_rep.content.get("highlights", []),
            "rendered_metrics": overall_rep.content.get("metrics", {}),
            "rendered_date": overall_rep.content.get("report_date"),
        }

    return PatientPreviewResponse(
        patient_id=clean_pat,
        cycle_id=clean_cyc,
        active_stage=stage or "consilium_view",
        cascade_status=cascade_status,
        overall_report=overall_dict,
        system_reports=system_reports_dict,
        referral_status=referral_status,
        referral_documents=referral_docs,
        organ_states=organ_states,
        trace_comparison=trace_data,
    )


# ==============================================================================
# UNIFIED PATIENT TIMELINE API
# ==============================================================================

@router.get(
    "/api/v1/admin/audit/patients/{patient_id}/timeline",
    response_model=UnifiedTimelineResponse,
    summary="Unified multi-process patient event timeline across virtual and real time (Server Admin Only)",
)
def get_patient_timeline(
    patient_id: str,
    request: Request,
    db: Session = Depends(get_db),
    admin_session: ServiceAdminSession = Depends(require_service_admin_session),
    audit_service: AuditTrailService = Depends(get_audit_trail_service),
) -> UnifiedTimelineResponse:
    """
    Chronologically orders all patient events across autonomous background processes:
    - Vitals telemetry & Mode 2 transitions
    - Pulse-monitored workouts
    - Athena curator interactions
    - Wellness daily assessments
    - Consiliums (Primary, Weekly, Lab Revision, Neo)
    - Original Gemini LLM calls, latencies, retries, and errors
    - Medical quality reviews
    """
    clean_pat = audit_service.validate_identifier(patient_id, "patient_id")

    # 1. Resolve all cycles for patient
    db_cycles = db.scalars(
        select(MedicalCycle).where(MedicalCycle.patient_id == clean_pat)
    ).all()
    cycle_ids = [c.cycle_id for c in db_cycles]
    fs_cycles = audit_service.list_cycles(clean_pat)
    all_cycles = sorted(list(set(cycle_ids + fs_cycles)))

    events: List[UnifiedTimelineEvent] = []
    anomaly_counts = {"delayed": 0, "duplicate": 0, "error": 0}

    # 2. Ingest Health Measurements (Telemetry)
    if cycle_ids:
        measurements = db.scalars(
            select(HealthMeasurement)
            .where(HealthMeasurement.cycle_id.in_(cycle_ids))
            .order_by(HealthMeasurement.measured_at.asc())
        ).all()
        for m in measurements:
            is_warning = bool(m.pulse and (m.pulse > 105 or m.pulse < 50))
            events.append(
                UnifiedTimelineEvent(
                    event_id=f"meas_{m.id}",
                    category="TELEMETRY",
                    title=f"Telemetry Point ({m.source})",
                    description=f"Pulse: {m.pulse or 'N/A'} bpm, BP: {m.systolic or 'N/A'}/{m.diastolic or 'N/A'} mmHg, SpO2: {m.spo2 or 'N/A'}%, Phase: {m.phase}",
                    virtual_time=m.measured_at.isoformat() if m.measured_at else None,
                    execution_time_utc=m.ingested_at.isoformat() if m.ingested_at else datetime.now(timezone.utc).isoformat(),
                    status="WARNING" if is_warning else "SUCCESS",
                    details={"pulse": m.pulse, "bp": f"{m.systolic}/{m.diastolic}", "phase": m.phase},
                )
            )

    # 3. Ingest Workout Sessions
    if cycle_ids:
        workouts = db.scalars(
            select(WorkoutSession)
            .where(WorkoutSession.cycle_id.in_(cycle_ids))
            .order_by(WorkoutSession.created_at.asc())
        ).all()
        for w in workouts:
            events.append(
                UnifiedTimelineEvent(
                    event_id=f"workout_{w.session_id}",
                    category="WORKOUT",
                    title=f"Workout Session ({w.status.upper()})",
                    description=f"Session {w.session_id}: Anomaly detected at {w.anomaly_detected_at.strftime('%H:%M:%S') if w.anomaly_detected_at else 'N/A'}",
                    virtual_time=w.anomaly_detected_at.isoformat() if w.anomaly_detected_at else None,
                    execution_time_utc=w.created_at.isoformat() if w.created_at else datetime.now(timezone.utc).isoformat(),
                    status="SUCCESS",
                    details={"session_id": w.session_id, "status": w.status},
                )
            )

    # 4. Ingest Model Reports
    if cycle_ids:
        reports = db.scalars(
            select(ModelReport)
            .where(ModelReport.cycle_id.in_(cycle_ids))
            .order_by(ModelReport.created_at.asc())
        ).all()
        for r in reports:
            rtype = r.report_type.lower()
            cat = "REPORT_PERSISTED"
            if "primary_consilium" in rtype or "primary" in rtype:
                cat = "PRIMARY_CONSILIUM"
            elif "weekly" in rtype:
                cat = "WEEKLY_CONSILIUM"
            elif "lab_revision" in rtype:
                cat = "LAB_REVISION"
            elif "neo" in rtype:
                cat = "NEO_REASSESSMENT"
            elif "referral" in rtype:
                cat = "EMERGENCY" if "urgent" in str(r.content).lower() else "REFERRAL"
            elif "athena" in rtype:
                cat = "ATHENA_CHAT"
            elif "wellness" in rtype:
                cat = "WELLNESS"
            elif "ocr" in rtype or "vision" in rtype or "ecg" in rtype:
                cat = "VISION_OCR"

            cnt = r.content or {}
            events.append(
                UnifiedTimelineEvent(
                    event_id=f"rep_{r.report_id}",
                    category=cat,
                    title=f"{cat}: {r.report_type} (v{r.version}, {r.privacy_level})",
                    description=(cnt.get("summary") or cnt.get("title") or f"Model: {r.model}")[:140],
                    virtual_time=str(cnt.get("report_date")) if cnt.get("report_date") else None,
                    execution_time_utc=r.created_at.isoformat() if r.created_at else datetime.now(timezone.utc).isoformat(),
                    status="SUCCESS",
                    report_id=r.report_id,
                    details={"model": r.model, "privacy_level": r.privacy_level, "version": r.version},
                )
            )

    # 5. Ingest Audit Calls from Filesystem
    calls = audit_service.list_calls(patient_id=clean_pat)
    seen_calls: Dict[str, datetime] = {}
    for c in calls:
        anomalies = []
        is_error = c.status not in ("SUCCESS", "UNKNOWN")
        is_delayed = c.latency_ms > 30000.0

        if is_delayed:
            anomalies.append("delayed")
            anomaly_counts["delayed"] += 1
        if is_error:
            anomalies.append("inference_error")
            anomaly_counts["error"] += 1

        # Duplicate check: same stage and sprint within 30 seconds
        dt_call = None
        try:
            dt_call = datetime.fromisoformat(c.created_at_utc.replace("Z", "+00:00"))
        except Exception:
            pass

        key = f"{c.stage}_{c.sprint_id or ''}"
        if key in seen_calls and dt_call:
            diff = abs((dt_call - seen_calls[key]).total_seconds())
            if diff < 30.0:
                anomalies.append("duplicate")
                anomaly_counts["duplicate"] += 1
        if dt_call:
            seen_calls[key] = dt_call

        status_val = "ERROR" if is_error else ("WARNING" if is_delayed else "SUCCESS")

        events.append(
            UnifiedTimelineEvent(
                event_id=f"call_{c.call_id}",
                category="AI_INFERENCE",
                title=f"AI Inference: {c.model_role} ({c.model_name})",
                description=f"Stage: {c.stage}, Latency: {c.latency_ms:.1f}ms, Status: {c.status}",
                virtual_time=None,
                execution_time_utc=c.created_at_utc,
                status=status_val,
                call_id=c.call_id,
                report_id=c.report_id,
                anomalies=anomalies,
                details={
                    "latency_ms": c.latency_ms,
                    "model_name": c.model_name,
                    "stage": c.stage,
                    "review_status": c.review_status,
                },
            )
        )

    # 6. Ingest Clinical AI Reviews
    reviews = db.scalars(
        select(ClinicalAIAuditReview).where(ClinicalAIAuditReview.patient_id == clean_pat)
    ).all()
    for rev in reviews:
        events.append(
            UnifiedTimelineEvent(
                event_id=f"rev_{rev.review_id}",
                category="CLINICAL_REVIEW",
                title=f"Clinical Audit Review: {rev.status}",
                description=f"Reviewer: {rev.reviewer_id or 'Unassigned'}. Notes: {rev.notes or 'None'}",
                virtual_time=None,
                execution_time_utc=rev.updated_at.isoformat() if rev.updated_at else datetime.now(timezone.utc).isoformat(),
                status="SUCCESS" if rev.status == ClinicalReviewStatus.REVIEWED_PASS.value else "WARNING",
                call_id=rev.call_id,
                details={"reviewer_id": rev.reviewer_id, "notes": rev.notes},
            )
        )

    # 7. Chronological Sort (by virtual_time if present, else execution_time_utc)
    def _sort_key(ev: UnifiedTimelineEvent):
        t_str = ev.virtual_time or ev.execution_time_utc
        return t_str

    events.sort(key=_sort_key)

    return UnifiedTimelineResponse(
        patient_id=clean_pat,
        total_events=len(events),
        events=events,
        anomaly_counts=anomaly_counts,
    )


# ==============================================================================
# LOCAL WEB INSPECTION DASHBOARD (HTML / JS / CSS)
# ==============================================================================

_DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Aegis Clinical AI Audit Trail & Patient Preview</title>
  <style>
    :root {
      --bg-primary: #0a0e17;
      --bg-secondary: #0f172a;
      --bg-tertiary: #1e293b;
      --bg-card: #0d1117;
      --text-main: #f8fafc;
      --text-muted: #94a3b8;
      --accent: #38bdf8;
      --accent-glow: rgba(56, 189, 248, 0.3);
      --success: #10b981;
      --danger: #ef4444;
      --warning: #f59e0b;
      --border: #334155;
      --font-mono: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      background-color: var(--bg-primary);
      color: var(--text-main);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
      height: 100vh;
      display: flex;
      flex-direction: column;
      overflow: hidden;
    }
    header {
      background-color: var(--bg-secondary);
      border-bottom: 1px solid var(--border);
      padding: 10px 20px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 15px;
      flex-shrink: 0;
    }
    .brand { display: flex; align-items: center; gap: 10px; }
    .brand h1 { font-size: 1.05rem; font-weight: 700; letter-spacing: 0.5px; }
    .badge-ip {
      background: rgba(16, 185, 129, 0.15);
      border: 1px solid #10b981;
      color: #34d399;
      font-size: 0.7rem;
      padding: 2px 6px;
      border-radius: 4px;
      font-weight: 600;
    }
    .nav-tabs {
      display: flex;
      gap: 6px;
      background: var(--bg-primary);
      padding: 4px;
      border-radius: 8px;
      border: 1px solid var(--border);
    }
    .nav-tab {
      background: transparent;
      border: none;
      color: var(--text-muted);
      padding: 6px 14px;
      border-radius: 6px;
      font-size: 0.8rem;
      font-weight: 600;
      cursor: pointer;
      display: flex;
      align-items: center;
      gap: 6px;
      transition: all 0.15s;
    }
    .nav-tab:hover { color: #fff; }
    .nav-tab.active {
      background: var(--bg-tertiary);
      color: var(--accent);
      box-shadow: 0 0 10px var(--accent-glow);
    }
    .auth-bar { display: flex; align-items: center; gap: 10px; }
    .auth-status {
      font-size: 0.75rem;
      padding: 4px 8px;
      border-radius: 4px;
      font-weight: 600;
    }
    .auth-status.locked { background: rgba(239, 68, 68, 0.2); color: #f87171; border: 1px solid #ef4444; }
    .auth-status.unlocked { background: rgba(16, 185, 129, 0.2); color: #34d399; border: 1px solid #10b981; }

    .btn {
      background-color: var(--accent);
      color: #0f172a;
      border: none;
      padding: 6px 12px;
      border-radius: 4px;
      font-weight: 600;
      font-size: 0.8rem;
      cursor: pointer;
      transition: all 0.15s;
    }
    .btn:hover { background-color: #0284c7; color: #fff; }
    .btn-secondary { background: var(--bg-tertiary); color: #fff; border: 1px solid var(--border); }
    .btn-secondary:hover { background: #475569; }

    /* Selectors bar */
    .selectors-bar {
      background: var(--bg-card);
      border-bottom: 1px solid var(--border);
      padding: 8px 20px;
      display: flex;
      align-items: center;
      gap: 16px;
      flex-shrink: 0;
    }
    .select-group { display: flex; align-items: center; gap: 8px; font-size: 0.8rem; }
    .select-group label { color: var(--text-muted); font-weight: 600; }
    select {
      background: var(--bg-tertiary);
      border: 1px solid var(--border);
      color: #fff;
      padding: 5px 10px;
      border-radius: 4px;
      font-size: 0.8rem;
      outline: none;
    }

    /* Main view container */
    .view-container { flex: 1; overflow: hidden; position: relative; }
    .view-pane {
      display: none;
      width: 100%;
      height: 100%;
      overflow: hidden;
    }
    .view-pane.active { display: flex; }

    /* VIEW 1: Deep Audit Trail */
    .audit-grid {
      display: grid;
      grid-template-columns: 280px 1fr 320px;
      width: 100%;
      height: 100%;
      overflow: hidden;
    }
    .col-calls {
      background: var(--bg-card);
      border-right: 1px solid var(--border);
      overflow-y: auto;
      display: flex;
      flex-direction: column;
    }
    .calls-header {
      padding: 10px 14px;
      border-bottom: 1px solid var(--border);
      font-size: 0.75rem;
      font-weight: 700;
      color: var(--text-muted);
      text-transform: uppercase;
    }
    .call-item {
      padding: 10px 14px;
      border-bottom: 1px solid rgba(255, 255, 255, 0.05);
      cursor: pointer;
      transition: background 0.15s;
    }
    .call-item:hover { background: var(--bg-tertiary); }
    .call-item.active { background: rgba(56, 189, 248, 0.15); border-left: 3px solid var(--accent); }
    .call-item .role { font-weight: 600; font-size: 0.85rem; color: #fff; }
    .call-item .meta { font-size: 0.75rem; color: var(--text-muted); margin-top: 3px; display: flex; justify-content: space-between; }

    .col-inspector {
      display: flex;
      flex-direction: column;
      overflow: hidden;
      background: var(--bg-primary);
    }
    .tabs-header {
      background: var(--bg-secondary);
      border-bottom: 1px solid var(--border);
      display: flex;
      gap: 2px;
      padding: 6px 12px 0 12px;
    }
    .tab-btn {
      background: transparent;
      border: 1px solid transparent;
      border-bottom: none;
      color: var(--text-muted);
      padding: 6px 12px;
      border-radius: 4px 4px 0 0;
      font-size: 0.78rem;
      font-weight: 600;
      cursor: pointer;
    }
    .tab-btn.active {
      background: var(--bg-primary);
      border-color: var(--border);
      color: var(--accent);
    }
    .tab-body {
      flex: 1;
      overflow: auto;
      padding: 16px;
      font-family: var(--font-mono);
      font-size: 0.8rem;
      white-space: pre-wrap;
      word-break: break-all;
      color: #e2e8f0;
      background: #060911;
    }

    .col-review {
      background: var(--bg-card);
      border-left: 1px solid var(--border);
      padding: 16px;
      overflow-y: auto;
      display: flex;
      flex-direction: column;
      gap: 14px;
    }
    .review-title { font-size: 0.85rem; font-weight: 700; color: var(--accent); text-transform: uppercase; }

    /* VIEW 2: Patient Preview */
    .preview-container {
      display: flex;
      width: 100%;
      height: 100%;
      overflow: hidden;
      background: #000;
    }
    .chassis-wrapper {
      flex: 0 0 420px;
      display: flex;
      justify-content: center;
      align-items: center;
      padding: 15px;
      background: #030712;
      border-right: 1px solid var(--border);
    }
    .phone-chassis {
      width: 360px;
      height: 680px;
      background: #000000;
      border-radius: 40px;
      border: 10px solid #1f2937;
      box-shadow: 0 0 30px rgba(0, 0, 0, 0.8), 0 0 15px var(--accent-glow);
      position: relative;
      display: flex;
      flex-direction: column;
      overflow: hidden;
    }
    .phone-notch {
      width: 110px;
      height: 20px;
      background: #1f2937;
      border-radius: 0 0 14px 14px;
      margin: 0 auto;
      z-index: 50;
    }
    .phone-screen {
      flex: 1;
      display: flex;
      flex-direction: column;
      position: relative;
      overflow: hidden;
      padding: 8px 12px;
      color: #fff;
    }
    .phone-top-bar {
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 8px;
      z-index: 20;
    }
    .pill {
      background: rgba(13, 17, 23, 0.9);
      border: 1px solid rgba(0, 255, 255, 0.6);
      box-shadow: 0 0 8px rgba(0, 255, 255, 0.2);
      border-radius: 12px;
      padding: 4px 10px;
      font-size: 0.7rem;
      font-weight: 700;
      display: flex;
      align-items: center;
      gap: 6px;
    }
    .dot-pulse {
      width: 6px;
      height: 6px;
      border-radius: 50%;
      background: #10b981;
      box-shadow: 0 0 6px #10b981;
    }
    .status-badge-primary {
      align-self: center;
      background: rgba(16, 185, 129, 0.15);
      border: 1px solid #10b981;
      color: #34d399;
      font-size: 0.68rem;
      font-weight: 700;
      padding: 3px 10px;
      border-radius: 10px;
      margin-bottom: 8px;
      text-transform: uppercase;
      z-index: 20;
    }
    .referral-banner-card {
      background: rgba(245, 158, 11, 0.12);
      border: 1px solid #f59e0b;
      border-radius: 8px;
      padding: 6px 10px;
      margin-bottom: 8px;
      font-size: 0.7rem;
      z-index: 20;
    }
    .referral-banner-card .title { font-weight: bold; color: #fbbf24; }
    .referral-banner-card .desc { color: #fde68a; margin-top: 2px; }

    /* Center silhouette & organ columns */
    .body-canvas-wrapper {
      flex: 1;
      position: relative;
      display: flex;
      justify-content: space-between;
    }
    .organ-col {
      width: 100px;
      display: flex;
      flex-direction: column;
      justify-content: space-around;
      z-index: 10;
    }
    .organ-card {
      background: rgba(13, 17, 23, 0.85);
      border-radius: 8px;
      padding: 6px 8px;
      border: 1px solid #334155;
      cursor: pointer;
      transition: all 0.2s;
    }
    .organ-card:hover { transform: scale(1.05); }
    .organ-card.active { border-color: var(--accent); box-shadow: 0 0 10px var(--accent-glow); }
    .organ-card .name { font-size: 0.65rem; font-weight: 700; color: #fff; }
    .organ-card .tag { font-size: 0.6rem; margin-top: 2px; font-weight: 600; text-transform: uppercase; }

    .silhouette-svg-container {
      position: absolute;
      top: 0; left: 0; right: 0; bottom: 0;
      display: flex;
      justify-content: center;
      align-items: center;
      pointer-events: none;
    }
    .silhouette-svg {
      height: 90%;
      fill: none;
      stroke: rgba(56, 189, 248, 0.35);
      stroke-width: 1.5;
      filter: drop-shadow(0 0 6px rgba(56, 189, 248, 0.3));
    }
    .organ-node {
      pointer-events: auto;
      cursor: pointer;
      transition: all 0.2s;
    }
    .organ-node:hover { filter: drop-shadow(0 0 8px #fff); }

    /* Bottom organ sheet in phone */
    .phone-sheet {
      position: absolute;
      bottom: 0; left: 0; right: 0;
      background: rgba(13, 17, 23, 0.95);
      border-top: 2px solid var(--accent);
      border-radius: 16px 16px 0 0;
      padding: 12px;
      transform: translateY(100%);
      transition: transform 0.25s ease-out;
      z-index: 30;
      max-height: 55%;
      overflow-y: auto;
      font-size: 0.72rem;
    }
    .phone-sheet.open { transform: translateY(0); }
    .phone-sheet h4 { font-size: 0.8rem; color: var(--accent); margin-bottom: 6px; }
    .phone-sheet .bullet { color: var(--accent); font-weight: bold; margin-right: 4px; }
    .phone-sheet .metric-row { display: flex; justify-content: space-between; margin-top: 2px; color: #94a3b8; font-family: var(--font-mono); }

    /* Right side of preview: 4-Way Trace & Reports */
    .preview-trace-side {
      flex: 1;
      display: flex;
      flex-direction: column;
      overflow: hidden;
      background: var(--bg-primary);
    }

    /* VIEW 3: Unified Timeline */
    .timeline-container {
      display: flex;
      flex-direction: column;
      width: 100%;
      height: 100%;
      overflow: hidden;
    }
    .timeline-controls {
      background: var(--bg-secondary);
      border-bottom: 1px solid var(--border);
      padding: 8px 20px;
      display: flex;
      align-items: center;
      gap: 16px;
      font-size: 0.8rem;
    }
    .timeline-scroll {
      flex: 1;
      overflow-y: auto;
      padding: 20px 40px;
    }
    .timeline-card {
      position: relative;
      margin-left: 20px;
      padding: 10px 14px;
      background: var(--bg-card);
      border: 1px solid var(--border);
      border-radius: 8px;
      margin-bottom: 12px;
    }
    .timeline-card::before {
      content: "";
      position: absolute;
      left: -27px;
      top: 14px;
      width: 12px;
      height: 12px;
      border-radius: 50%;
      background: var(--accent);
      box-shadow: 0 0 6px var(--accent);
    }
    .timeline-card.delayed::before { background: var(--warning); box-shadow: 0 0 6px var(--warning); }
    .timeline-card.error::before { background: var(--danger); box-shadow: 0 0 6px var(--danger); }
    .timeline-card .time-row {
      display: flex;
      justify-content: space-between;
      font-size: 0.72rem;
      color: var(--text-muted);
      margin-bottom: 4px;
      font-family: var(--font-mono);
    }
    .timeline-card .title { font-size: 0.85rem; font-weight: 700; color: #fff; }
    .timeline-card .desc { font-size: 0.75rem; color: #cbd5e1; margin-top: 4px; }

    /* Modal */
    .modal-overlay {
      position: fixed;
      top: 0; left: 0; right: 0; bottom: 0;
      background: rgba(0, 0, 0, 0.7);
      display: none;
      justify-content: center;
      align-items: center;
      z-index: 1000;
    }
    .modal-overlay.open { display: flex; }
    .modal-card {
      background: var(--bg-secondary);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 20px;
      width: 360px;
    }
    .modal-card h3 { font-size: 1rem; margin-bottom: 12px; color: #fff; }
    .modal-card input {
      width: 100%;
      background: var(--bg-tertiary);
      border: 1px solid var(--border);
      color: #fff;
      padding: 8px;
      border-radius: 4px;
      margin-bottom: 14px;
    }
  </style>
</head>
<body>

  <!-- Top Header -->
  <header>
    <div class="brand">
      <span style="font-size: 1.3rem;">🛡️</span>
      <h1>Aegis Clinical AI Audit Trail & Patient Preview</h1>
      <span class="badge-ip">127.0.0.1 LOCALHOST</span>
      <span class="badge-ip" style="color: #38bdf8; border-color: #38bdf8;">CLINICAL AI AUDIT TRAIL — READ ONLY</span>
    </div>

    <!-- Navigation View Switcher -->
    <div class="nav-tabs">
      <button class="nav-tab active" id="tab-btn-inferences" onclick="switchView('inferences')">
        <span>🔍</span> Model Inferences
      </button>
      <button class="nav-tab" id="tab-btn-preview" onclick="switchView('preview')">
        <span>📱</span> Patient Preview (Flutter)
      </button>
      <button class="nav-tab" id="tab-btn-timeline" onclick="switchView('timeline')">
        <span>⏱️</span> Unified Timeline
      </button>
    </div>

    <!-- Auth Controls -->
    <div class="auth-bar">
      <span class="auth-status locked" id="authStatusBadge">LOCKED</span>
      <button class="btn" id="loginBtn" onclick="openLoginModal()">Authenticate</button>
    </div>
  </header>

  <!-- Global Selectors Bar -->
  <div class="selectors-bar">
    <div class="select-group">
      <label for="patientSelect">1. Patient:</label>
      <select id="patientSelect" onchange="onPatientSelected()">
        <option value="">Loading patients...</option>
      </select>
    </div>
    <div class="select-group">
      <label for="cycleSelect">2. Cycle:</label>
      <select id="cycleSelect" onchange="onCycleSelected()">
        <option value="">Select patient first</option>
      </select>
    </div>
    <div class="select-group" id="stageFilterGroup">
      <label for="stageSelect">3. Stage / Weekly:</label>
      <select id="stageSelect" onchange="onStageSelected()">
        <option value="">All Stages</option>
      </select>
    </div>
    <div style="flex: 1;"></div>
    <button class="btn btn-secondary" onclick="reloadActiveView()">↻ Refresh</button>
  </div>

  <!-- Main View Container -->
  <div class="view-container">

    <!-- VIEW 1: MODEL INFERENCES AUDIT TRAIL -->
    <div class="view-pane active" id="pane-inferences">
      <div class="audit-grid">
        <!-- Calls List -->
        <div class="col-calls">
          <div class="calls-header">Calls (<span id="callsCount">0</span>)</div>
          <div id="callsList" style="flex: 1; overflow-y: auto;">
            <div style="padding: 20px; color: var(--text-muted); font-size: 0.8rem; text-align: center;">
              Select a cycle to view model calls.
            </div>
          </div>
        </div>

        <!-- Call Inspector Tabbed View -->
        <div class="col-inspector">
          <div class="tabs-header">
            <button class="tab-btn active" onclick="switchInspectorTab('prompt')">1. Prompt & System</button>
            <button class="tab-btn" onclick="switchInspectorTab('verbatim')">2. Verbatim Output</button>
            <button class="tab-btn" onclick="switchInspectorTab('json')">3. Cleaned JSON</button>
            <button class="tab-btn" onclick="switchInspectorTab('validation')">4. Schema & Errors</button>
            <button class="tab-btn" onclick="switchInspectorTab('sqlite')">5. SQLite Report</button>
            <button class="tab-btn" onclick="switchInspectorTab('metadata')">6. IDs & Timing</button>
          </div>
          <div class="tab-body" id="inspectorBody">
            Select an invocation from the left to inspect raw inputs and verbatim responses.
          </div>
        </div>

        <!-- Clinical Quality Review Panel -->
        <div class="col-review">
          <div class="review-title">Medical Quality Review</div>
          <div style="font-size: 0.75rem; color: var(--text-muted);">
            Human clinical auditor decision for selected inference.
          </div>
          <div style="display: flex; flex-direction: column; gap: 8px;">
            <label style="font-size: 0.75rem; color: var(--text-muted);">Decision Status:</label>
            <select id="reviewStatus">
              <option value="NOT_REVIEWED">NOT_REVIEWED</option>
              <option value="REVIEWED_PASS">REVIEWED_PASS</option>
              <option value="REVIEWED_FAIL">REVIEWED_FAIL</option>
              <option value="NEEDS_CLINICAL_REVIEW">NEEDS_CLINICAL_REVIEW</option>
            </select>

            <label style="font-size: 0.75rem; color: var(--text-muted); margin-top: 6px;">Reviewer ID (Required for PASS):</label>
            <input type="text" id="reviewerId" style="background: var(--bg-tertiary); border: 1px solid var(--border); color: #fff; padding: 6px; border-radius: 4px; font-size: 0.8rem;" placeholder="e.g. dr_smith_md">

            <label style="font-size: 0.75rem; color: var(--text-muted); margin-top: 6px;">Clinical Notes:</label>
            <textarea id="reviewNotes" rows="4" style="background: var(--bg-tertiary); border: 1px solid var(--border); color: #fff; padding: 6px; border-radius: 4px; font-size: 0.8rem; font-family: inherit; resize: vertical;" placeholder="Document clinical evaluation..."></textarea>

            <button class="btn" style="margin-top: 10px;" onclick="saveClinicalReview()">Save Clinical Review</button>
          </div>
        </div>
      </div>
    </div>

    <!-- VIEW 2: PATIENT PREVIEW (EXACT FLUTTER MOBILE VIEW) -->
    <div class="view-pane" id="pane-preview">
      <div class="preview-container">
        <!-- Smartphone Chassis -->
        <div class="chassis-wrapper">
          <div class="phone-chassis">
            <div class="phone-notch"></div>
            <div class="phone-screen">
              <!-- Top Bar -->
              <div class="phone-top-bar">
                <div class="pill">
                  <span>⌚</span>
                  <span>Smart Watch</span>
                  <div class="dot-pulse"></div>
                </div>
                <div class="pill" style="font-family: var(--font-mono);">
                  <span>🕒</span>
                  <span id="phoneVirtualTime">08:00</span>
                </div>
              </div>

              <!-- Consilium Status Pill -->
              <div class="status-badge-primary" id="phoneConsiliumStatus">
                PRIMARY CONSILIUM :: COMPLETED (v1)
              </div>

              <!-- Referral Banner (If Active) -->
              <div class="referral-banner-card" id="phoneReferralBanner" style="display: none;">
                <div class="title">⚠️ Doctor Referral Recommended</div>
                <div class="desc" id="phoneReferralText">Consultation recommended.</div>
              </div>

              <!-- Interactive Silhouette & Organ Columns -->
              <div class="body-canvas-wrapper">
                <!-- Left 4 Organs -->
                <div class="organ-col" id="leftOrganCol"></div>

                <!-- Center Anatomical Silhouette SVG -->
                <div class="silhouette-svg-container">
                  <svg class="silhouette-svg" viewBox="0 0 200 400">
                    <!-- Head -->
                    <circle cx="100" cy="40" r="22" />
                    <!-- Neck -->
                    <line x1="100" y1="62" x2="100" y2="75" />
                    <!-- Torso -->
                    <path d="M 70 75 Q 100 70 130 75 L 125 180 Q 100 185 75 180 Z" />
                    <!-- Left Arm -->
                    <path d="M 70 80 Q 50 130 45 190" />
                    <!-- Right Arm -->
                    <path d="M 130 80 Q 150 130 155 190" />
                    <!-- Left Leg -->
                    <path d="M 85 180 Q 80 270 75 360" />
                    <!-- Right Leg -->
                    <path d="M 115 180 Q 120 270 125 360" />

                    <!-- Organ Anchor Glowing Dots -->
                    <circle class="organ-node" id="node-nervous" cx="100" cy="40" r="7" fill="#10b981" onclick="openOrganModal('nervous')" />
                    <circle class="organ-node" id="node-respiratory" cx="100" cy="100" r="7" fill="#10b981" onclick="openOrganModal('respiratory')" />
                    <circle class="organ-node" id="node-cardiovascular" cx="108" cy="115" r="7" fill="#10b981" onclick="openOrganModal('cardiovascular')" />
                    <circle class="organ-node" id="node-gastrointestinal" cx="100" cy="145" r="7" fill="#10b981" onclick="openOrganModal('gastrointestinal')" />
                    <circle class="organ-node" id="node-endocrine" cx="95" cy="85" r="6" fill="#10b981" onclick="openOrganModal('endocrine')" />
                    <circle class="organ-node" id="node-immune" cx="115" cy="95" r="6" fill="#10b981" onclick="openOrganModal('immune')" />
                    <circle class="organ-node" id="node-musculoskeletal" cx="80" cy="220" r="6" fill="#10b981" onclick="openOrganModal('musculoskeletal')" />
                    <circle class="organ-node" id="node-integumentary" cx="50" cy="150" r="6" fill="#10b981" onclick="openOrganModal('integumentary')" />
                  </svg>
                </div>

                <!-- Right 4 Organs -->
                <div class="organ-col" id="rightOrganCol"></div>
              </div>

              <!-- Sliding Organ Detail Sheet in Phone -->
              <div class="phone-sheet" id="phoneOrganSheet">
                <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 6px;">
                  <h4 id="sheetTitle">Organ System</h4>
                  <button style="background: none; border: none; color: #fff; cursor: pointer; font-size: 0.9rem;" onclick="closeOrganModal()">✕</button>
                </div>
                <div style="margin-bottom: 6px; color: #e2e8f0;" id="sheetSummary"></div>
                <div style="font-weight: 700; color: var(--accent); margin-top: 6px; font-size: 0.7rem;">CLINICAL HIGHLIGHTS:</div>
                <div id="sheetHighlights" style="margin-top: 4px;"></div>
                <div style="font-weight: 700; color: #a7f3d0; margin-top: 8px; font-size: 0.7rem;">METRICS:</div>
                <div id="sheetMetrics" style="margin-top: 4px;"></div>
              </div>
            </div>
          </div>
        </div>

        <!-- 4-Stage Trace Pipeline & Raw Data Side -->
        <div class="preview-trace-side">
          <div class="tabs-header">
            <button class="tab-btn active" onclick="switchTraceTab('comparison')">4-Stage Medical Trace</button>
            <button class="tab-btn" onclick="switchTraceTab('client_json')">CLIENT_FACING JSON</button>
            <button class="tab-btn" onclick="switchTraceTab('documents')">Safe Doctor Documents (<span id="safeDocsCount">0</span>)</button>
          </div>
          <div class="tab-body" id="traceBody" style="padding: 16px;">
            Select a cycle to compare Gemini Original Response -> SERVER_ONLY -> CLIENT_FACING -> Flutter View.
          </div>
        </div>
      </div>
    </div>

    <!-- VIEW 3: UNIFIED PATIENT TIMELINE -->
    <div class="view-pane" id="pane-timeline">
      <div class="timeline-container">
        <div class="timeline-controls">
          <span style="font-weight: 700;">Filter Category:</span>
          <select id="timelineCategoryFilter" onchange="renderTimeline()">
            <option value="ALL">ALL CATEGORIES</option>
            <option value="TELEMETRY">Telemetry</option>
            <option value="WORKOUT">Workouts</option>
            <option value="PRIMARY_CONSILIUM">Primary Consilium</option>
            <option value="WEEKLY_CONSILIUM">Weekly Consilium</option>
            <option value="LAB_REVISION">Lab Revision</option>
            <option value="NEO_REASSESSMENT">Neo Consilium</option>
            <option value="ATHENA_CHAT">Athena</option>
            <option value="WELLNESS">Wellness</option>
            <option value="AI_INFERENCE">AI Inferences</option>
            <option value="CLINICAL_REVIEW">Reviews</option>
          </select>

          <span style="font-weight: 700; margin-left: 15px;">Anomaly Filter:</span>
          <select id="timelineAnomalyFilter" onchange="renderTimeline()">
            <option value="ALL">All Events</option>
            <option value="ANOMALIES_ONLY">Anomalies Only (Delays / Duplicates / Errors)</option>
          </select>

          <span style="margin-left: auto; color: var(--text-muted); font-size: 0.75rem;">
            Total Events: <b id="timelineTotalCount" style="color: #fff;">0</b> |
            Delays: <b id="timelineDelaysCount" style="color: var(--warning);">0</b> |
            Duplicates: <b id="timelineDuplicatesCount" style="color: var(--warning);">0</b> |
            Errors: <b id="timelineErrorsCount" style="color: var(--danger);">0</b>
          </span>
        </div>

        <div class="timeline-scroll" id="timelineList">
          <div style="color: var(--text-muted); text-align: center; margin-top: 40px;">Select a patient to view the unified virtual-time chronological history.</div>
        </div>
      </div>
    </div>

  </div>

  <!-- Ephemeral Login Modal -->
  <div class="modal-overlay" id="loginModal">
    <div class="modal-card">
      <h3>Administrative Login</h3>
      <p style="font-size: 0.75rem; color: var(--text-muted); margin-bottom: 12px;">
        Enter your AEGIS_API_SECRET. An ephemeral session token will be issued for this browser tab.
      </p>
      <input type="password" id="adminSecretInput" placeholder="Enter AEGIS_API_SECRET" onkeydown="if(event.key==='Enter') executeLogin()">
      <div style="display: flex; justify-content: flex-end; gap: 8px;">
        <button class="btn btn-secondary" onclick="closeLoginModal()">Cancel</button>
        <button class="btn" onclick="executeLogin()">Log In</button>
      </div>
    </div>
  </div>

  <script>
    // State management
    let state = {
      activeView: 'inferences',
      activeInspectorTab: 'prompt',
      activeTraceTab: 'comparison',
      token: sessionStorage.getItem('aegis_admin_token') || '',
      patientId: '',
      cycleId: '',
      stage: '',
      callId: '',
      calls: [],
      currentCallDetail: null,
      previewData: null,
      timelineEvents: [],
      anomalyCounts: {}
    };

    function escapeHtml(str) {
      if (!str) return '';
      return String(str)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
    }

    async function apiRequest(url, options = {}) {
      options.headers = options.headers || {};
      if (state.token) {
        options.headers['Authorization'] = 'Bearer ' + state.token;
      }
      const res = await fetch(url, options);
      if (res.status === 401 || res.status === 403) {
        updateAuthBadge(false);
      }
      return res;
    }

    function updateAuthBadge(isAuthed) {
      const badge = document.getElementById('authStatusBadge');
      const btn = document.getElementById('loginBtn');
      if (isAuthed) {
        badge.className = 'auth-status unlocked';
        badge.textContent = 'UNLOCKED';
        btn.textContent = 'Log Out';
      } else {
        badge.className = 'auth-status locked';
        badge.textContent = 'LOCKED';
        btn.textContent = 'Authenticate';
      }
    }

    function openLoginModal() {
      if (state.token) {
        // Logout
        state.token = '';
        sessionStorage.removeItem('aegis_admin_token');
        updateAuthBadge(false);
        return;
      }
      document.getElementById('loginModal').classList.add('open');
      document.getElementById('adminSecretInput').focus();
    }

    function closeLoginModal() {
      document.getElementById('loginModal').classList.remove('open');
      document.getElementById('adminSecretInput').value = '';
    }

    async function executeLogin() {
      const secret = document.getElementById('adminSecretInput').value.trim();
      if (!secret) return;

      try {
        const res = await fetch('/api/v1/admin/audit/login', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ secret: secret })
        });
        if (!res.ok) {
          alert('Login failed: invalid secret or access forbidden.');
          return;
        }
        const data = await res.json();
        state.token = data.access_token;
        sessionStorage.setItem('aegis_admin_token', state.token);
        updateAuthBadge(true);
        closeLoginModal();
        loadPatients();
      } catch (err) {
        alert('Network error during login: ' + err);
      }
    }

    function switchView(viewName) {
      state.activeView = viewName;
      document.querySelectorAll('.nav-tab').forEach(t => t.classList.remove('active'));
      document.querySelectorAll('.view-pane').forEach(p => p.classList.remove('active'));

      document.getElementById('tab-btn-' + viewName).classList.add('active');
      document.getElementById('pane-' + viewName).classList.add('active');

      if (viewName === 'preview') loadPreview();
      else if (viewName === 'timeline') loadTimeline();
      else if (viewName === 'inferences') loadCalls();
    }

    async function loadPatients() {
      const res = await apiRequest('/api/v1/admin/audit/patients');
      if (!res.ok) return;
      updateAuthBadge(true);
      const patients = await res.json();
      const sel = document.getElementById('patientSelect');
      sel.innerHTML = '<option value="">-- Select Patient --</option>';
      patients.forEach(p => {
        sel.innerHTML += `<option value="${escapeHtml(p)}">${escapeHtml(p)}</option>`;
      });
      if (patients.length > 0) {
        sel.value = patients[0];
        onPatientSelected();
      }
    }

    async function onPatientSelected() {
      state.patientId = document.getElementById('patientSelect').value;
      if (!state.patientId) return;

      const res = await apiRequest(`/api/v1/admin/audit/patients/${encodeURIComponent(state.patientId)}/cycles`);
      if (!res.ok) return;
      const cycles = await res.json();
      const sel = document.getElementById('cycleSelect');
      sel.innerHTML = '<option value="">-- Select Cycle --</option>';
      cycles.forEach(c => {
        sel.innerHTML += `<option value="${escapeHtml(c.cycle_id)}">${escapeHtml(c.cycle_id)} (${escapeHtml(c.status)})</option>`;
      });
      if (cycles.length > 0) {
        sel.value = cycles[0].cycle_id;
        onCycleSelected();
      }
    }

    async function onCycleSelected() {
      state.cycleId = document.getElementById('cycleSelect').value;
      if (!state.cycleId) return;

      // Load stages
      const res = await apiRequest(`/api/v1/admin/audit/cycles/${encodeURIComponent(state.cycleId)}/stages`);
      if (res.ok) {
        const stages = await res.json();
        const sel = document.getElementById('stageSelect');
        sel.innerHTML = '<option value="">All Stages</option>';
        stages.forEach(s => {
          sel.innerHTML += `<option value="${escapeHtml(s.stage)}">${escapeHtml(s.stage)} (${s.call_count})</option>`;
        });
      }

      reloadActiveView();
    }

    function onStageSelected() {
      state.stage = document.getElementById('stageSelect').value;
      reloadActiveView();
    }

    function reloadActiveView() {
      if (state.activeView === 'inferences') loadCalls();
      else if (state.activeView === 'preview') loadPreview();
      else if (state.activeView === 'timeline') loadTimeline();
    }

    // --- INFERENCES VIEW ---
    async function loadCalls() {
      if (!state.cycleId) return;
      let url = `/api/v1/admin/audit/cycles/${encodeURIComponent(state.cycleId)}/calls`;
      if (state.stage) url += `?stage=${encodeURIComponent(state.stage)}`;

      const res = await apiRequest(url);
      if (!res.ok) return;
      state.calls = await res.json();
      document.getElementById('callsCount').textContent = state.calls.length;

      const list = document.getElementById('callsList');
      list.innerHTML = '';
      state.calls.forEach(c => {
        const div = document.createElement('div');
        div.className = 'call-item' + (c.call_id === state.callId ? ' active' : '');
        div.onclick = () => selectCall(c.call_id);
        div.innerHTML = `
          <div class="role">${escapeHtml(c.model_role)} (${escapeHtml(c.model_name)})</div>
          <div class="meta">
            <span>${escapeHtml(c.stage)}</span>
            <span style="color: ${c.status === 'SUCCESS' ? 'var(--success)' : 'var(--danger)'};">${escapeHtml(c.status)}</span>
          </div>
        `;
        list.appendChild(div);
      });

      if (state.calls.length > 0 && !state.callId) {
        selectCall(state.calls[0].call_id);
      }
    }

    async function selectCall(callId) {
      state.callId = callId;
      document.querySelectorAll('.call-item').forEach(el => el.classList.remove('active'));
      const res = await apiRequest(`/api/v1/admin/audit/calls/${encodeURIComponent(callId)}`);
      if (!res.ok) return;
      state.currentCallDetail = await res.json();
      renderInspectorTab();

      // Update review form
      const rev = state.currentCallDetail.review;
      document.getElementById('reviewStatus').value = rev ? rev.status : 'NOT_REVIEWED';
      document.getElementById('reviewerId').value = rev && rev.reviewer_id ? rev.reviewer_id : '';
      document.getElementById('reviewNotes').value = rev && rev.notes ? rev.notes : '';
    }

    function switchInspectorTab(tabName) {
      state.activeInspectorTab = tabName;
      document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
      event.target.classList.add('active');
      renderInspectorTab();
    }

    function renderInspectorTab() {
      const box = document.getElementById('inspectorBody');
      const det = state.currentCallDetail;
      if (!det) {
        box.textContent = 'No call selected.';
        return;
      }

      const c = det.call;
      if (state.activeInspectorTab === 'prompt') {
        box.textContent = `=== SYSTEM INSTRUCTION ===\n${c.system_instruction || '(none)'}\n\n=== USER PROMPT ===\n${c.prompt || '(none)'}`;
      } else if (state.activeInspectorTab === 'verbatim') {
        box.textContent = c.raw_response_text || '(empty raw output)';
      } else if (state.activeInspectorTab === 'json') {
        box.textContent = c.cleaned_json_text || JSON.stringify(c.parsed_json, null, 2);
      } else if (state.activeInspectorTab === 'validation') {
        box.textContent = `Status: ${c.status}\nSchema: ${c.schema_name || 'N/A'}\nValidation Error: ${c.validation_error || 'None (VALID SCHEMA)'}\nDiagnostic Log: ${c.error_diagnostic_log || 'None'}`;
      } else if (state.activeInspectorTab === 'sqlite') {
        box.textContent = det.persisted_report ? JSON.stringify(det.persisted_report, null, 2) : 'No SQLite report linked to this call.';
      } else if (state.activeInspectorTab === 'metadata') {
        box.textContent = JSON.stringify({
          call_id: c.call_id,
          patient_id: c.patient_id,
          cycle_id: c.cycle_id,
          sprint_id: c.sprint_id,
          stage: c.stage,
          latency_ms: c.latency_ms,
          created_at_utc: c.created_at_utc,
          attempt: `${c.attempt_number} of ${c.total_attempts}`,
          report_id: c.report_id
        }, null, 2);
      }
    }

    async function saveClinicalReview() {
      if (!state.callId) return;
      const statusVal = document.getElementById('reviewStatus').value;
      const reviewer = document.getElementById('reviewerId').value.trim();
      const notes = document.getElementById('reviewNotes').value.trim();

      if (statusVal === 'REVIEWED_PASS' && !reviewer) {
        alert('Reviewer ID is mandatory for status REVIEWED_PASS.');
        return;
      }

      const payload = {
        status: statusVal,
        reviewer_id: reviewer || null,
        notes: notes || null
      };

      const res = await apiRequest(`/api/v1/admin/audit/calls/${encodeURIComponent(state.callId)}/review`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
      });

      if (!res.ok) {
        const err = await res.json();
        alert('Failed to save review: ' + (err.detail || 'error'));
        return;
      }

      alert('Clinical review saved successfully.');
      loadCalls();
    }

    // --- PATIENT PREVIEW ---
    async function loadPreview() {
      if (!state.patientId || !state.cycleId) return;
      const res = await apiRequest(`/api/v1/admin/audit/patients/${encodeURIComponent(state.patientId)}/preview/${encodeURIComponent(state.cycleId)}`);
      if (!res.ok) return;
      state.previewData = await res.json();

      renderPhonePreview();
      renderTraceSide();
    }

    function renderPhonePreview() {
      const data = state.previewData;
      if (!data) return;

      // Status pill
      const statusPill = document.getElementById('phoneConsiliumStatus');
      if (data.cascade_status.is_primary_consilium_completed) {
        statusPill.textContent = 'PRIMARY CONSILIUM :: COMPLETED (v1)';
        statusPill.style.color = '#34d399';
        statusPill.style.borderColor = '#10b981';
      } else {
        statusPill.textContent = 'CONSILIUM :: PREPARING';
        statusPill.style.color = '#38bdf8';
        statusPill.style.borderColor = '#38bdf8';
      }

      // Referral banner
      const refBanner = document.getElementById('phoneReferralBanner');
      if (data.referral_status && data.referral_status.has_active_referral) {
        refBanner.style.display = 'block';
        document.getElementById('phoneReferralText').textContent = data.referral_status.reason || 'Referral recommended.';
      } else {
        refBanner.style.display = 'none';
      }

      // Organ Columns
      const leftCol = document.getElementById('leftOrganCol');
      const rightCol = document.getElementById('rightOrganCol');
      leftCol.innerHTML = '';
      rightCol.innerHTML = '';

      const left4 = data.organ_states.slice(0, 4);
      const right4 = data.organ_states.slice(4, 8);

      function createCard(org) {
        const d = document.createElement('div');
        d.className = 'organ-card';
        d.style.borderColor = org.color;
        d.onclick = () => openOrganModal(org.system_key);
        d.innerHTML = `
          <div class="name">${escapeHtml(org.title.replace(' System', ''))}</div>
          <div class="tag" style="color: ${org.color};">${escapeHtml(org.status)}</div>
        `;
        return d;
      }

      left4.forEach(org => leftCol.appendChild(createCard(org)));
      right4.forEach(org => rightCol.appendChild(createCard(org)));

      // Update Silhouette node colors
      data.organ_states.forEach(org => {
        const node = document.getElementById('node-' + org.system_key);
        if (node) {
          node.setAttribute('fill', org.color);
        }
      });

      // Update Safe docs count
      document.getElementById('safeDocsCount').textContent = data.referral_documents.length;
    }

    function openOrganModal(sysKey) {
      if (!state.previewData) return;
      const org = state.previewData.organ_states.find(o => o.system_key === sysKey);
      if (!org) return;

      document.getElementById('sheetTitle').textContent = org.title.toUpperCase();
      document.getElementById('sheetTitle').style.color = org.color;
      document.getElementById('sheetSummary').textContent = org.summary;

      const hlBox = document.getElementById('sheetHighlights');
      hlBox.innerHTML = '';
      if (org.highlights && org.highlights.length > 0) {
        org.highlights.forEach(h => {
          hlBox.innerHTML += `<div style="margin-top: 2px;"><span class="bullet">•</span>${escapeHtml(h)}</div>`;
        });
      } else {
        hlBox.innerHTML = '<div style="color: #64748b;">No explicit bullet highlights.</div>';
      }

      const mBox = document.getElementById('sheetMetrics');
      mBox.innerHTML = '';
      if (org.metrics && Object.keys(org.metrics).length > 0) {
        for (const [k, v] of Object.entries(org.metrics)) {
          mBox.innerHTML += `<div class="metric-row"><span>&gt; ${escapeHtml(k.toUpperCase())}:</span><span style="color: ${org.color};">${escapeHtml(JSON.stringify(v))}</span></div>`;
        }
      } else {
        mBox.innerHTML = '<div style="color: #64748b;">No specific metrics recorded.</div>';
      }

      document.getElementById('phoneOrganSheet').classList.add('open');
    }

    function closeOrganModal() {
      document.getElementById('phoneOrganSheet').classList.remove('open');
    }

    function switchTraceTab(tabName) {
      state.activeTraceTab = tabName;
      document.querySelectorAll('#pane-preview .tab-btn').forEach(b => b.classList.remove('active'));
      event.target.classList.add('active');
      renderTraceSide();
    }

    function renderTraceSide() {
      const box = document.getElementById('traceBody');
      const data = state.previewData;
      if (!data) return;

      if (state.activeTraceTab === 'comparison') {
        const trace = data.trace_comparison || {};
        box.innerHTML = `
          <div style="font-size: 0.9rem; font-weight: 700; color: var(--accent); margin-bottom: 12px;">
            4-Stage Clinical Transformation Pipeline (Trace Integrity)
          </div>
          <div style="display: flex; flex-direction: column; gap: 14px;">
            <div style="background: var(--bg-card); border: 1px solid var(--border); border-radius: 6px; padding: 10px;">
              <div style="font-weight: 700; color: #f87171; margin-bottom: 4px;">STAGE 1: Gemini Original Verbatim Stream</div>
              <div style="font-size: 0.75rem; color: #94a3b8; font-family: var(--font-mono); max-height: 120px; overflow-y: auto;">${escapeHtml(trace.gemini_raw_verbatim || '(No verbatim audit call recorded)')}</div>
            </div>

            <div style="background: var(--bg-card); border: 1px solid var(--border); border-radius: 6px; padding: 10px;">
              <div style="font-weight: 700; color: #fbbf24; margin-bottom: 4px;">STAGE 2: Persisted SERVER_ONLY Synthesis</div>
              <div style="font-size: 0.75rem; color: #94a3b8; font-family: var(--font-mono); max-height: 120px; overflow-y: auto;">${escapeHtml(JSON.stringify(trace.server_only_report || {}, null, 2))}</div>
            </div>

            <div style="background: var(--bg-card); border: 1px solid var(--border); border-radius: 6px; padding: 10px;">
              <div style="font-weight: 700; color: #34d399; margin-bottom: 4px;">STAGE 3: Validated CLIENT_FACING Report</div>
              <div style="font-size: 0.75rem; color: #94a3b8; font-family: var(--font-mono); max-height: 120px; overflow-y: auto;">${escapeHtml(JSON.stringify(trace.client_facing_report || {}, null, 2))}</div>
            </div>

            <div style="background: var(--bg-card); border: 1px solid var(--border); border-radius: 6px; padding: 10px;">
              <div style="font-weight: 700; color: #38bdf8; margin-bottom: 4px;">STAGE 4: Flutter Patient Mobile Display</div>
              <div style="font-size: 0.75rem; color: #94a3b8; font-family: var(--font-mono); max-height: 120px; overflow-y: auto;">${escapeHtml(JSON.stringify(trace.flutter_view_data || {}, null, 2))}</div>
            </div>
          </div>
        `;
      } else if (state.activeTraceTab === 'client_json') {
        box.textContent = JSON.stringify(data.overall_report || data.system_reports, null, 2);
      } else if (state.activeTraceTab === 'documents') {
        box.textContent = JSON.stringify(data.referral_documents, null, 2);
      }
    }

    // --- UNIFIED TIMELINE ---
    async function loadTimeline() {
      if (!state.patientId) return;
      const res = await apiRequest(`/api/v1/admin/audit/patients/${encodeURIComponent(state.patientId)}/timeline`);
      if (!res.ok) return;
      const data = await res.json();
      state.timelineEvents = data.events;
      state.anomalyCounts = data.anomaly_counts;

      document.getElementById('timelineTotalCount').textContent = data.total_events;
      document.getElementById('timelineDelaysCount').textContent = data.anomaly_counts.delayed || 0;
      document.getElementById('timelineDuplicatesCount').textContent = data.anomaly_counts.duplicate || 0;
      document.getElementById('timelineErrorsCount').textContent = data.anomaly_counts.error || 0;

      renderTimeline();
    }

    function renderTimeline() {
      const catFilter = document.getElementById('timelineCategoryFilter').value;
      const anomFilter = document.getElementById('timelineAnomalyFilter').value;

      const list = document.getElementById('timelineList');
      list.innerHTML = '';

      const filtered = state.timelineEvents.filter(ev => {
        if (catFilter !== 'ALL' && ev.category !== catFilter) return false;
        if (anomFilter === 'ANOMALIES_ONLY' && (!ev.anomalies || ev.anomalies.length === 0)) return false;
        return true;
      });

      if (filtered.length === 0) {
        list.innerHTML = '<div style="color: var(--text-muted); text-align: center; margin-top: 40px;">No events match the selected filters.</div>';
        return;
      }

      filtered.forEach(ev => {
        const div = document.createElement('div');
        let cardClass = 'timeline-card';
        if (ev.status === 'ERROR' || ev.anomalies.includes('error')) cardClass += ' error';
        else if (ev.status === 'WARNING' || ev.anomalies.includes('delayed') || ev.anomalies.includes('duplicate')) cardClass += ' delayed';

        div.className = cardClass;
        div.innerHTML = `
          <div class="time-row">
            <span><b>Virtual Time:</b> ${escapeHtml(ev.virtual_time || 'N/A')}</span>
            <span><b>Wall Clock UTC:</b> ${escapeHtml(ev.execution_time_utc)}</span>
          </div>
          <div style="display: flex; justify-content: space-between; align-items: center;">
            <div class="title">${escapeHtml(ev.title)}</div>
            <span style="font-size: 0.7rem; font-weight: 700; color: ${ev.status === 'SUCCESS' ? 'var(--success)' : (ev.status === 'WARNING' ? 'var(--warning)' : 'var(--danger)')};">[ ${escapeHtml(ev.status)} ]</span>
          </div>
          <div class="desc">${escapeHtml(ev.description)}</div>
          ${ev.anomalies && ev.anomalies.length > 0 ? `<div style="margin-top: 6px; font-size: 0.7rem; color: var(--warning); font-weight: 700;">Flags: ${escapeHtml(ev.anomalies.join(', '))}</div>` : ''}
        `;
        list.appendChild(div);
      });
    }

    // Auto-initialize on load
    window.addEventListener('DOMContentLoaded', () => {
      if (state.token) {
        updateAuthBadge(true);
        loadPatients();
      } else {
        updateAuthBadge(false);
      }
    });
  </script>
</body>
</html>
"""


@router.get(
    "/admin/audit",
    response_class=HTMLResponse,
    summary="Clinical AI Audit & Patient Preview Console (Localhost Only)",
)
@router.get(
    "/admin/audit/ui",
    response_class=HTMLResponse,
    include_in_schema=False,
)
def get_audit_dashboard(request: Request) -> HTMLResponse:
    """Renders the single-page HTML administrative inspection and patient preview dashboard."""
    verify_localhost_client(request)
    return HTMLResponse(content=_DASHBOARD_HTML, status_code=200)
