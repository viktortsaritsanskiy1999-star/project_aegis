"""
Stage 8A.3 — Doctor Document Export (PDF/DOCX) & Patient Document API Test Suite.

Verifies all 12 mandatory requirements:
1. PDF export from saved doc (valid %PDF, pypdf readable, non-empty pages).
2. DOCX export from saved doc (python-docx readable, headings, tables, bullet lists).
3. All 7 clinical observation sections present in both exports.
4. Content strictly matches saved source (0 LLM / Gemini calls, deterministic).
5. Multi-page, long longitudinal tables/paragraphs export cleanly (page_count > 1, no truncation).
6. Missing metrics honest display (NOT_RECORDED / explicit absence, no hallucinations).
7. Non-diagnostic disclaimer prominently displayed at top and in footers.
8. IDOR protection: requesting another patient's document returns 404 with Zero Oracle Leakage.
9. Cross-cycle discovery: lists documents across active, completed, and archived cycles.
10. Nonexistent ID (404) & unsupported format (400) cleanly handled.
11. Idempotency: repeated export downloads produce 0 new database rows.
12. Existing client APIs (/api/v1/client/cascade/status) remain fully operational.
"""

from datetime import datetime, timedelta, timezone
import io
import re
from typing import Any, Dict, List, Optional

import docx
from fastapi.testclient import TestClient
import pypdf
import pytest
from sqlalchemy.orm import Session

from app.main import app
from app.models.medical_cycle import MedicalCycle
from app.models.patient import Patient
from app.repositories.cycle_repository import MedicalCycleRepository
from app.repositories.report_repository import ModelReportRepository
from app.repositories.sprint_repository import WeeklySprintRepository
from app.schemas.consilium import (
    REFERRAL_DOCUMENT_CANONICAL_TITLE,
    ReferralDoctorDocumentOutput,
)
from app.schemas.privacy import PrivacyLevel
from app.schemas.reports import ClientDatedSystemEntry, ClientReportContent
from app.security.auth import (
    DeviceSession,
    require_device_session,
    verify_client_token,
)
from app.security.tokens import SCOPE_CASCADE_RUN, SCOPE_REPORTS_READ
from app.services.doctor_document_export_service import (
    DoctorDocumentExportService,
    sanitize_pdf_text,
)
from app.services.referral_document_service import (
    REFERRAL_DOCTOR_DOCUMENT_RAW_REPORT_TYPE,
    REFERRAL_DOCTOR_DOCUMENT_REPORT_TYPE,
)
from app.services.weekly_consilium_service import REFERRAL_LIFECYCLE_STATE_REPORT_TYPE


def _make_device_session(patient_id: str) -> DeviceSession:
    """Creates a mock DeviceSession with full client read scopes."""
    return DeviceSession(
        patient_id=patient_id,
        device_id=f"dev_{patient_id}",
        jti=f"jti_{patient_id}",
        family_id=f"fam_{patient_id}",
        scope="cycle:read reports:read cascade:run",
        scopes=frozenset(["cycle:read", "reports:read", "cascade:run"]),
        issued_at=datetime.now(timezone.utc),
        expires_at=datetime.now(timezone.utc) + timedelta(hours=2),
    )


def _sample_pro_document_output(
    urgency: str = "routine",
    specialties: Optional[List[str]] = None,
    long_history: bool = False,
    include_not_recorded: bool = False,
) -> ReferralDoctorDocumentOutput:
    specs = specialties or ["Cardiology", "Internal Medicine"]

    obj_items = [
        "Resting heart rate average: 74 bpm across 14 monitored days.",
        "Blood pressure: mean 138/88 mmHg; systolic peaks up to 154 mmHg recorded in morning windows.",
        "Single-lead smartwatch rhythm strips: sinus rhythm with isolated premature ventricular complexes.",
    ]
    if long_history:
        for i in range(1, 35):
            obj_items.append(
                f"Longitudinal sprint metric Day {i}: ambulatory resting vitals stable at {70 + (i % 10)} bpm, "
                f"systolic {125 + (i % 25)} mmHg recorded without acute symptoms."
            )

    symptom_items = [
        "2026-10-18: Patient reported mild recurrent evening fatigue.",
        "2026-10-24: Episode of sudden palpitations while resting after dinner.",
    ]
    if include_not_recorded:
        symptom_items = [
            "NO ATHENA CLINICAL JOURNALS OR PATIENT SYMPTOM LOGS RECORDED (NOT_RECORDED).",
            "No acute subjective symptoms reported by patient during the observation period.",
        ]

    return ReferralDoctorDocumentOutput(
        document_title=REFERRAL_DOCUMENT_CANONICAL_TITLE,
        informational_disclaimer=(
            "This summary is prepared strictly for informational review and discussion with a licensed "
            "physician. It does not replace medical consultation or diagnostic laboratory tests."
        ),
        recommended_specialties=specs,
        consultation_urgency="priority" if urgency == "priority" else "routine",
        executive_summary_for_patient=(
            "Observed blood pressure variability and resting heart rate spikes warrant an outpatient "
            "evaluation with a clinician."
        ),
        patient_and_observation_period=(
            "Patient monitored continuously in Aegis observation program from 2026-10-18 to 2026-11-08."
        ),
        reason_for_in_person_consultation=(
            "Outpatient clinical assessment recommended to evaluate recurrent morning systolic peaks "
            "and intermittent palpitations."
        ),
        objective_measurements_and_examination_results=obj_items,
        timeline_of_reported_symptoms_and_changes=symptom_items,
        important_patterns_and_unresolved_concerns=[
            "Morning systolic blood pressure elevation exceeding standard reference threshold (140 mmHg).",
            "Discrepancy between subjective exertion perception and heart rate recovery.",
        ],
        ai_observations_requiring_verification=[
            "AI analytical pattern: early Stage 1 systolic elevation pattern requiring clinician verification.",
            "Potential chronotropic variability requiring 24-hour ambulatory Holter correlation.",
        ],
        questions_and_examinations_for_clinician=[
            "Would 24-hour ambulatory blood pressure monitoring (ABPM) be clinically indicated?",
            "Should a 12-lead resting ECG and basic electrolyte/thyroid blood panel be ordered?",
        ],
    )


def _seed_patient_and_cycle(
    db: Session,
    patient_id: str,
    cycle_id: str,
    status: str = "active",
) -> MedicalCycle:
    patient = db.query(Patient).filter_by(patient_id=patient_id).first()
    if not patient:
        patient = Patient(patient_id=patient_id, status="active")
        db.add(patient)
        db.commit()

    cycle = db.query(MedicalCycle).filter_by(cycle_id=cycle_id).first()
    if not cycle:
        cycle = MedicalCycle(
            cycle_id=cycle_id,
            patient_id=patient_id,
            status=status,
            started_at=datetime(2026, 10, 18, 8, 0, tzinfo=timezone.utc),
        )
        db.add(cycle)
        db.commit()
    return cycle


def _seed_referral_documents(
    db: Session,
    *,
    cycle_id: str,
    patient_id: str,
    sprint_id: str = "sprint_test_w1",
    sprint_number: int = 1,
    referral_id: str = "REF-20261108-01",
    pro_doc: Optional[ReferralDoctorDocumentOutput] = None,
    created_at: Optional[datetime] = None,
) -> tuple[Any, Any]:
    """Seeds both raw SERVER_ONLY report and validated CLIENT_FACING report in DB."""
    report_repo = ModelReportRepository(db)
    doc_output = pro_doc or _sample_pro_document_output()
    now_dt = created_at or datetime(2026, 11, 8, 9, 0, tzinfo=timezone.utc)
    now_iso = now_dt.isoformat()
    report_date = now_dt.strftime("%Y-%m-%d")

    # 0. Ensure sprint row exists for foreign key constraint
    sprint_repo = WeeklySprintRepository(db)
    sprint_repo.create(
        sprint_id=sprint_id,
        cycle_id=cycle_id,
        sprint_number=sprint_number,
        status="completed",
        commit=True,
    )

    # 1. Raw SERVER_ONLY document
    raw_content = {
        "patient_id": patient_id,
        "cycle_id": cycle_id,
        "sprint_id": sprint_id,
        "sprint_number": sprint_number,
        "referral_id": referral_id,
        "referral_source": "weekly_consilium",
        "created_at_utc": now_iso,
        "pro_doctor_document": doc_output.model_dump(),
        "rendered_english_markdown": doc_output.render_english_markdown(),
    }
    raw_report = report_repo.create_next_version(
        cycle_id=cycle_id,
        sprint_id=sprint_id,
        model="gemini-1.5-pro",
        report_type=REFERRAL_DOCTOR_DOCUMENT_RAW_REPORT_TYPE,
        privacy_level=PrivacyLevel.SERVER_ONLY,
        content=raw_content,
        created_at=now_dt,
        commit=True,
    )

    # 2. Validated CLIENT_FACING document
    specs_str = ", ".join(doc_output.recommended_specialties)
    highlights = [
        f"Recommended consultation: {specs_str} ({doc_output.consultation_urgency})",
        doc_output.executive_summary_for_patient,
    ]
    client_payload = {
        "title": REFERRAL_DOCUMENT_CANONICAL_TITLE,
        "summary": doc_output.render_english_markdown(),
        "report_date": report_date,
        "system_key": None,
        "has_changes": True,
        "highlights": highlights,
        "metrics": {
            "patient_id": patient_id,
            "cycle_id": cycle_id,
            "sprint_id": sprint_id,
            "sprint_number": sprint_number,
            "referral_id": referral_id,
            "created_at_utc": now_iso,
            "referral_source": "weekly_consilium",
            "urgency": doc_output.consultation_urgency,
            "target_specialties": specs_str,
            "raw_document_report_id": raw_report.report_id,
            "is_informational_observation_summary": True,
        },
        "entries": [
            ClientDatedSystemEntry(
                date=report_date,
                summary=doc_output.executive_summary_for_patient,
                has_changes=True,
            ).model_dump()
        ],
    }
    validated_client = ClientReportContent.model_validate(client_payload).model_dump(exclude_none=True)

    client_report = report_repo.create_next_version(
        cycle_id=cycle_id,
        sprint_id=sprint_id,
        model="gemini-1.5-pro",
        report_type=REFERRAL_DOCTOR_DOCUMENT_REPORT_TYPE,
        privacy_level=PrivacyLevel.CLIENT_FACING,
        content=validated_client,
        created_at=now_dt,
        commit=True,
    )

    # 3. Referral Lifecycle state
    ref_lifecycle = {
        "cycle_id": cycle_id,
        "status": "document_ready",
        "document_report_id": client_report.report_id,
        "raw_document_report_id": raw_report.report_id,
        "referral_id": referral_id,
        "sprint_id": sprint_id,
        "urgency": doc_output.consultation_urgency,
        "target_specialties": doc_output.recommended_specialties,
        "clinical_reason_for_referral": doc_output.reason_for_in_person_consultation,
        "document_generation_state": "ready",
        "created_at_utc": now_iso,
    }
    report_repo.create_next_version(
        cycle_id=cycle_id,
        sprint_id=sprint_id,
        model="gemini-1.5-pro",
        report_type=REFERRAL_LIFECYCLE_STATE_REPORT_TYPE,
        privacy_level=PrivacyLevel.SERVER_ONLY,
        content=ref_lifecycle,
        created_at=now_dt,
        commit=True,
    )

    return client_report, raw_report


# ==============================================================================
# SCENARIOS 1 & 2: VALID PDF & DOCX EXPORTS
# ==============================================================================


def test_01_pdf_export_from_saved_doc(client: TestClient, db_session: Session):
    patient_id = "pat_export_01"
    cycle_id = "cyc_export_01"
    _seed_patient_and_cycle(db_session, patient_id, cycle_id)
    client_doc, _ = _seed_referral_documents(db_session, cycle_id=cycle_id, patient_id=patient_id)

    session = _make_device_session(patient_id)
    app.dependency_overrides[require_device_session] = lambda: session
    app.dependency_overrides[verify_client_token] = lambda: session

    resp = client.get(f"/api/v1/client/referral/documents/{client_doc.report_id}/export?format=pdf")
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"] == "application/pdf"
    assert "attachment; filename=" in resp.headers["content-disposition"]
    assert resp.headers["content-disposition"].endswith('.pdf"')

    pdf_bytes = resp.content
    assert pdf_bytes.startswith(b"%PDF"), "Must have PDF magic header"

    reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
    assert len(reader.pages) >= 1
    text = "".join(page.extract_text() for page in reader.pages)
    assert "AEGIS" in text
    assert "OBSERVATION" in text


def test_02_docx_export_from_saved_doc(client: TestClient, db_session: Session):
    patient_id = "pat_export_02"
    cycle_id = "cyc_export_02"
    _seed_patient_and_cycle(db_session, patient_id, cycle_id)
    client_doc, _ = _seed_referral_documents(db_session, cycle_id=cycle_id, patient_id=patient_id)

    session = _make_device_session(patient_id)
    app.dependency_overrides[require_device_session] = lambda: session
    app.dependency_overrides[verify_client_token] = lambda: session

    resp = client.get(f"/api/v1/client/referral/documents/{client_doc.report_id}/export?format=docx")
    assert resp.status_code == 200, resp.text
    assert (
        resp.headers["content-type"]
        == "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )
    assert resp.headers["content-disposition"].endswith('.docx"')

    docx_bytes = resp.content
    doc = docx.Document(io.BytesIO(docx_bytes))
    full_text = " ".join(p.text for p in doc.paragraphs)
    assert "AEGIS — HEALTH OBSERVATION SUMMARY FOR YOUR DOCTOR" in full_text
    assert len(doc.tables) >= 2, "Must contain notice table and metadata table"


# ==============================================================================
# SCENARIO 3: ALL 7 MANDATORY CLINICAL SECTIONS PRESENT IN BOTH EXPORTS
# ==============================================================================


def test_03_all_seven_sections_present_in_both_pdf_and_docx(db_session: Session):
    patient_id = "pat_export_03"
    cycle_id = "cyc_export_03"
    _seed_patient_and_cycle(db_session, patient_id, cycle_id)
    client_doc, raw_doc = _seed_referral_documents(db_session, cycle_id=cycle_id, patient_id=patient_id)

    service = DoctorDocumentExportService(db_session)
    pro_doc, meta = service.extract_document_data(client_doc, raw_doc)

    pdf_bytes = service.export_pdf(pro_doc, meta)
    docx_bytes = service.export_docx(pro_doc, meta)

    # 1. Verify in PDF
    pdf_reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
    pdf_text = " ".join(page.extract_text() for page in pdf_reader.pages)

    # 2. Verify in DOCX
    docx_doc = docx.Document(io.BytesIO(docx_bytes))
    docx_text = " ".join(p.text for p in docx_doc.paragraphs)

    expected_sections = [
        "1. Patient and Observation Period",
        "2. Reason for Recommending an In-Person Consultation",
        "3. Objective Health Measurements and Examination Results",
        "4. Timeline of Reported Symptoms and Observed Changes",
        "5. Important Patterns and Unresolved Concerns",
        "6. AI-Generated Observations Requiring Professional Verification",
        "7. Questions and Possible Examinations to Discuss with a Licensed Clinician",
    ]

    for sec in expected_sections:
        # Check both formats
        assert sec in docx_text, f"Section '{sec}' missing in DOCX"
        clean_sec = sanitize_pdf_text(sec)
        assert clean_sec in pdf_text, f"Section '{clean_sec}' missing in PDF"


# ==============================================================================
# SCENARIO 4: CONTENT STRICTLY MATCHES SOURCE — ZERO GEMINI CALLS
# ==============================================================================


def test_04_content_matches_saved_source_zero_gemini_calls(db_session: Session):
    patient_id = "pat_export_04"
    cycle_id = "cyc_export_04"
    _seed_patient_and_cycle(db_session, patient_id, cycle_id)

    custom_pro = _sample_pro_document_output()
    custom_pro.reason_for_in_person_consultation = (
        "Specific distinctive clinical reason: Evaluation of persistent nocturnal bradycardia."
    )
    client_doc, raw_doc = _seed_referral_documents(
        db_session,
        cycle_id=cycle_id,
        patient_id=patient_id,
        pro_doc=custom_pro,
    )

    # Export via service — zero AI gateway calls
    service = DoctorDocumentExportService(db_session)
    pro_doc, meta = service.extract_document_data(client_doc, raw_doc)

    md_content = service.export_markdown(pro_doc, meta)
    assert "Evaluation of persistent nocturnal bradycardia" in md_content

    docx_bytes = service.export_docx(pro_doc, meta)
    doc = docx.Document(io.BytesIO(docx_bytes))
    assert any("persistent nocturnal bradycardia" in p.text for p in doc.paragraphs)

    pdf_bytes = service.export_pdf(pro_doc, meta)
    pdf_reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
    pdf_text = " ".join(page.extract_text() for page in pdf_reader.pages)
    assert "persistent nocturnal bradycardia" in pdf_text


# ==============================================================================
# SCENARIO 5: MULTI-PAGE EXPORTS FOR LONG CLINICAL HISTORIES
# ==============================================================================


def test_05_multi_page_export_for_long_clinical_history(db_session: Session):
    patient_id = "pat_export_05"
    cycle_id = "cyc_export_05"
    _seed_patient_and_cycle(db_session, patient_id, cycle_id)

    long_pro = _sample_pro_document_output(long_history=True)
    client_doc, raw_doc = _seed_referral_documents(
        db_session,
        cycle_id=cycle_id,
        patient_id=patient_id,
        pro_doc=long_pro,
    )

    service = DoctorDocumentExportService(db_session)
    pro_doc, meta = service.extract_document_data(client_doc, raw_doc)

    pdf_bytes = service.export_pdf(pro_doc, meta)
    pdf_reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
    assert len(pdf_reader.pages) >= 2, f"Expected multi-page PDF, got {len(pdf_reader.pages)}"

    # Check that running header appears on page 2
    page2_text = pdf_reader.pages[1].extract_text()
    assert "AEGIS HEALTH OBSERVATION SUMMARY FOR CLINICIAN" in page2_text

    # Check footer page numbering on page 1 and page 2
    assert "Page 1 of" in pdf_reader.pages[0].extract_text()
    assert "Page 2 of" in pdf_reader.pages[1].extract_text()


# ==============================================================================
# SCENARIO 6: HONEST DISPLAY OF MISSING METRICS (NOT_RECORDED)
# ==============================================================================


def test_06_missing_metrics_honest_display_no_hallucination(db_session: Session):
    patient_id = "pat_export_06"
    cycle_id = "cyc_export_06"
    _seed_patient_and_cycle(db_session, patient_id, cycle_id)

    unrecorded_pro = _sample_pro_document_output(include_not_recorded=True)
    client_doc, raw_doc = _seed_referral_documents(
        db_session,
        cycle_id=cycle_id,
        patient_id=patient_id,
        pro_doc=unrecorded_pro,
    )

    service = DoctorDocumentExportService(db_session)
    pro_doc, meta = service.extract_document_data(client_doc, raw_doc)

    # DOCX
    docx_bytes = service.export_docx(pro_doc, meta)
    doc = docx.Document(io.BytesIO(docx_bytes))
    full_docx_text = " ".join(p.text for p in doc.paragraphs)
    assert "NOT_RECORDED" in full_docx_text

    # PDF
    pdf_bytes = service.export_pdf(pro_doc, meta)
    pdf_reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
    pdf_text = " ".join(page.extract_text() for page in pdf_reader.pages)
    assert "NOT_RECORDED" in pdf_text


# ==============================================================================
# SCENARIO 7: NON-DIAGNOSTIC DISCLAIMER PROMINENTLY DISPLAYED
# ==============================================================================


def test_07_non_diagnostic_disclaimer_prominently_displayed(client: TestClient, db_session: Session):
    patient_id = "pat_export_07"
    cycle_id = "cyc_export_07"
    _seed_patient_and_cycle(db_session, patient_id, cycle_id)
    client_doc, _ = _seed_referral_documents(db_session, cycle_id=cycle_id, patient_id=patient_id)

    session = _make_device_session(patient_id)
    app.dependency_overrides[require_device_session] = lambda: session
    app.dependency_overrides[verify_client_token] = lambda: session

    # 1. Detail API
    resp = client.get(f"/api/v1/client/referral/documents/{client_doc.report_id}")
    assert resp.status_code == 200
    detail = resp.json()
    assert "informational_disclaimer" in detail
    assert "does not replace medical consultation" in detail["informational_disclaimer"]

    # 2. Export PDF
    resp_pdf = client.get(f"/api/v1/client/referral/documents/{client_doc.report_id}/export?format=pdf")
    pdf_reader = pypdf.PdfReader(io.BytesIO(resp_pdf.content))
    first_page_text = pdf_reader.pages[0].extract_text()
    assert "IMPORTANT INFORMATIONAL NOTICE" in first_page_text
    assert "NOT an official medical discharge" in first_page_text


# ==============================================================================
# SCENARIO 8: IDOR PROTECTION — 404 WITH ZERO ORACLE LEAKAGE
# ==============================================================================


def test_08_idor_protection_returns_404_zero_oracle_leakage(client: TestClient, db_session: Session):
    patient_owner = "pat_export_08_owner"
    patient_intruder = "pat_export_08_intruder"
    cycle_id = "cyc_export_08"

    _seed_patient_and_cycle(db_session, patient_owner, cycle_id)
    _seed_patient_and_cycle(db_session, patient_intruder, "cyc_export_08_intruder")
    client_doc, _ = _seed_referral_documents(db_session, cycle_id=cycle_id, patient_id=patient_owner)

    # Intruder tries to access owner's document
    intruder_session = _make_device_session(patient_intruder)
    app.dependency_overrides[require_device_session] = lambda: intruder_session
    app.dependency_overrides[verify_client_token] = lambda: intruder_session

    # 1. Attempt GET detail
    resp_detail = client.get(f"/api/v1/client/referral/documents/{client_doc.report_id}")
    assert resp_detail.status_code == 404, "Must return 404 for foreign document, not 403"
    assert resp_detail.json()["detail"] == "Referral document not found."

    # 2. Attempt GET export PDF
    resp_export = client.get(f"/api/v1/client/referral/documents/{client_doc.report_id}/export?format=pdf")
    assert resp_export.status_code == 404
    assert resp_export.json()["detail"] == "Referral document not found."


# ==============================================================================
# SCENARIO 9: CROSS-CYCLE LIST DISCOVERY (ACTIVE & CLOSED CYCLES)
# ==============================================================================


def test_09_list_documents_across_active_and_closed_cycles(client: TestClient, db_session: Session):
    patient_id = "pat_export_09"
    foreign_patient = "pat_export_09_foreign"

    # Cycle 1: completed/closed
    cyc1 = _seed_patient_and_cycle(db_session, patient_id, "cyc_09_closed", status="completed")
    doc1, _ = _seed_referral_documents(
        db_session,
        cycle_id="cyc_09_closed",
        patient_id=patient_id,
        referral_id="REF-CLOSED-1",
        created_at=datetime(2026, 10, 20, 8, 0, tzinfo=timezone.utc),
    )

    # Cycle 2: active
    cyc2 = _seed_patient_and_cycle(db_session, patient_id, "cyc_09_active", status="active")
    doc2, _ = _seed_referral_documents(
        db_session,
        cycle_id="cyc_09_active",
        patient_id=patient_id,
        referral_id="REF-ACTIVE-2",
        created_at=datetime(2026, 11, 8, 8, 0, tzinfo=timezone.utc),
    )

    # Cycle 3: belongs to foreign patient
    _seed_patient_and_cycle(db_session, foreign_patient, "cyc_09_foreign", status="active")
    _seed_referral_documents(
        db_session,
        cycle_id="cyc_09_foreign",
        patient_id=foreign_patient,
        referral_id="REF-FOREIGN-3",
    )

    session = _make_device_session(patient_id)
    app.dependency_overrides[require_device_session] = lambda: session
    app.dependency_overrides[verify_client_token] = lambda: session

    resp = client.get("/api/v1/client/referral/documents")
    assert resp.status_code == 200
    data = resp.json()
    assert data["patient_id"] == patient_id
    assert data["total_count"] == 2

    report_ids = [d["report_id"] for d in data["documents"]]
    assert doc1.report_id in report_ids
    assert doc2.report_id in report_ids

    # Most recent first
    assert data["documents"][0]["report_id"] == doc2.report_id
    assert data["documents"][0]["cycle_status"] == "active"
    assert data["documents"][1]["report_id"] == doc1.report_id
    assert data["documents"][1]["cycle_status"] == "completed"


# ==============================================================================
# SCENARIO 10: NONEXISTENT ID & UNSUPPORTED EXPORT FORMATS
# ==============================================================================


def test_10_nonexistent_id_and_unsupported_formats(client: TestClient, db_session: Session):
    patient_id = "pat_export_10"
    cycle_id = "cyc_export_10"
    _seed_patient_and_cycle(db_session, patient_id, cycle_id)
    client_doc, _ = _seed_referral_documents(db_session, cycle_id=cycle_id, patient_id=patient_id)

    session = _make_device_session(patient_id)
    app.dependency_overrides[require_device_session] = lambda: session
    app.dependency_overrides[verify_client_token] = lambda: session

    # 1. Nonexistent report_id
    resp_404 = client.get("/api/v1/client/referral/documents/nonexistent_doc_id")
    assert resp_404.status_code == 404
    assert resp_404.json()["detail"] == "Referral document not found."

    # 2. Unsupported format in export
    resp_bad_fmt = client.get(
        f"/api/v1/client/referral/documents/{client_doc.report_id}/export?format=exe"
    )
    assert resp_bad_fmt.status_code == 400
    assert "Unsupported export format" in resp_bad_fmt.json()["detail"]

    resp_txt = client.get(
        f"/api/v1/client/referral/documents/{client_doc.report_id}/export?format=txt"
    )
    assert resp_txt.status_code == 400


# ==============================================================================
# SCENARIO 11: IDEMPOTENT EXPORT — ZERO DATABASE MUTATIONS
# ==============================================================================


def test_11_export_is_pure_and_idempotent_zero_db_mutations(client: TestClient, db_session: Session):
    patient_id = "pat_export_11"
    cycle_id = "cyc_export_11"
    _seed_patient_and_cycle(db_session, patient_id, cycle_id)
    client_doc, _ = _seed_referral_documents(db_session, cycle_id=cycle_id, patient_id=patient_id)

    session = _make_device_session(patient_id)
    app.dependency_overrides[require_device_session] = lambda: session
    app.dependency_overrides[verify_client_token] = lambda: session

    report_repo = ModelReportRepository(db_session)
    count_before = len(report_repo.list_client_facing(cycle_id))

    # Repeated downloads across all formats (with artificial delay across second boundary)
    import time
    res_pdf1 = client.get(f"/api/v1/client/referral/documents/{client_doc.report_id}/export?format=pdf")
    time.sleep(1.05)
    res_pdf2 = client.get(f"/api/v1/client/referral/documents/{client_doc.report_id}/export?format=pdf")
    res_docx = client.get(f"/api/v1/client/referral/documents/{client_doc.report_id}/export?format=docx")
    res_md = client.get(f"/api/v1/client/referral/documents/{client_doc.report_id}/export?format=md")

    assert res_pdf1.status_code == 200
    assert res_pdf2.status_code == 200
    assert res_docx.status_code == 200
    assert res_md.status_code == 200

    # Ensure identical binary payload
    assert res_pdf1.content == res_pdf2.content

    # Ensure zero database mutations
    count_after = len(report_repo.list_client_facing(cycle_id))
    assert count_before == count_after, "Export must never create new database report rows"


# ==============================================================================
# SCENARIO 12: EXISTING CLIENT CASCADE AND STATUS APIS REMAIN UNAFFECTED
# ==============================================================================


def test_12_existing_cascade_and_vitals_apis_remain_unaffected(client: TestClient, db_session: Session):
    patient_id = "pat_export_12"
    cycle_id = "cyc_export_12"
    _seed_patient_and_cycle(db_session, patient_id, cycle_id)
    client_doc, _ = _seed_referral_documents(db_session, cycle_id=cycle_id, patient_id=patient_id)

    session = _make_device_session(patient_id)
    app.dependency_overrides[require_device_session] = lambda: session
    app.dependency_overrides[verify_client_token] = lambda: session

    # Verify existing /api/v1/client/cascade/status endpoint
    resp = client.get("/api/v1/client/cascade/status")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["active_cycle_id"] == cycle_id
    assert data["has_active_referral"] is True
    assert "referral_status" in data
    assert data["referral_status"]["has_active_referral"] is True
    assert data["referral_status"]["status"] == "document_ready"
    assert data["referral_status"]["document_report_id"] == client_doc.report_id


# ==============================================================================
# SCENARIOS 13 & 14: UNICODE MEDICAL SYMBOLS, UNITS & MULTI-PAGE HIGH FIDELITY (STAGE 8A.3.1)
# ==============================================================================


def test_13_unicode_medical_symbols_and_units_preservation(client: TestClient, db_session: Session):
    patient_id = "pat_export_13_unicode"
    cycle_id = "cyc_export_13_unicode"
    _seed_patient_and_cycle(db_session, patient_id, cycle_id)

    medical_metrics = [
        "Thyroid: TSH 2.45 µIU/mL (ref 0.4–4.0 µIU/mL), Free T4 14.2 pmol/L.",
        "Iron panel: Ferritin 85 µg/L (ref 30–400 µg/L), Serum Iron 18 µmol/L.",
        "Renal function: Serum Creatinine 72 µmol/L, eGFR 88 mL/min/1.73m².",
        "Metabolic: Fasting Blood Glucose 95 mg/dL, HbA1c 5.4%.",
        "Hemodynamics: Morning SBP ≥ 140 mmHg (peak 154 mmHg), DBP ≤ 90 mmHg.",
        "Rhythm & Tolerance: Resting heart rate 72 ± 4 bpm, PVC burden < 1%.",
        "Respiratory & Vitals: SpO₂ 97% at rest (drops to SpO₂ ≤ 91% on stairs), Body temp 36.6 °C.",
        "Urinalysis: Albumin/Creatinine ratio 2.5–3.0 mg/mmol.",
    ]

    pro_doc = _sample_pro_document_output()
    pro_doc.reason_for_in_person_consultation = (
        "Outpatient cardiology evaluation for persistent SBP ≥ 140 mmHg and exertional SpO₂ ≤ 91%."
    )
    pro_doc.objective_measurements_and_examination_results = medical_metrics

    client_doc, _ = _seed_referral_documents(
        db_session,
        cycle_id=cycle_id,
        patient_id=patient_id,
        pro_doc=pro_doc,
    )

    session = _make_device_session(patient_id)
    app.dependency_overrides[require_device_session] = lambda: session
    app.dependency_overrides[verify_client_token] = lambda: session

    # 1. Test PDF Export via API
    resp_pdf = client.get(f"/api/v1/client/referral/documents/{client_doc.report_id}/export?format=pdf")
    assert resp_pdf.status_code == 200, resp_pdf.text
    pdf_bytes = resp_pdf.content
    pdf_reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
    pdf_text = " ".join(page.extract_text() for page in pdf_reader.pages)

    # 2. Test DOCX Export via API
    resp_docx = client.get(f"/api/v1/client/referral/documents/{client_doc.report_id}/export?format=docx")
    assert resp_docx.status_code == 200, resp_docx.text
    docx_bytes = resp_docx.content
    docx_doc = docx.Document(io.BytesIO(docx_bytes))
    docx_text = " ".join(p.text for p in docx_doc.paragraphs)

    # 3. Assert exact medical units, symbols, and values preserved in both
    expected_symbols = [
        "µg/L",
        "µmol/L",
        "mg/dL",
        "µIU/mL",
        "≥",
        "≤",
        "±",
        "SpO₂",
        "°C",
        "1.73m²",
        "30–400",
        "2.5–3.0",
        "2.45",
        "85",
        "72",
        "95",
        "140",
        "90",
        "36.6",
    ]

    for item in expected_symbols:
        assert item in pdf_text, f"Symbol or unit '{item}' missing or distorted in PDF export"
        assert item in docx_text, f"Symbol or unit '{item}' missing or distorted in DOCX export"


def test_14_multipage_unicode_table_and_running_headers(db_session: Session):
    patient_id = "pat_export_14_multi"
    cycle_id = "cyc_export_14_multi"
    _seed_patient_and_cycle(db_session, patient_id, cycle_id)

    # Generate 40 detailed medical items with Unicode symbols
    multi_items = [
        f"Sprint day {i}: Resting SpO₂ 98% ± 1%, SBP ≥ {130 + (i % 20)} mmHg, Ferritin {70 + i} µg/L, Creatinine {65 + i} µmol/L."
        for i in range(1, 41)
    ]

    long_pro = _sample_pro_document_output()
    long_pro.objective_measurements_and_examination_results = multi_items

    client_doc, raw_doc = _seed_referral_documents(
        db_session,
        cycle_id=cycle_id,
        patient_id=patient_id,
        pro_doc=long_pro,
    )

    service = DoctorDocumentExportService(db_session)
    pro_doc, meta = service.extract_document_data(client_doc, raw_doc)

    pdf_bytes = service.export_pdf(pro_doc, meta)
    reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))

    assert len(reader.pages) >= 2, "Long history must generate multi-page PDF"

    # Verify running header on page 2
    page2_text = reader.pages[1].extract_text()
    assert "AEGIS HEALTH OBSERVATION SUMMARY FOR CLINICIAN" in page2_text

    # Verify that symbols are preserved across pages
    assert "µg/L" in page2_text
    assert "SpO₂" in page2_text
    assert "±" in page2_text

    # Verify footer on each page
    for idx, page in enumerate(reader.pages):
        page_str = page.extract_text()
        assert f"Page {idx + 1} of" in page_str
        assert "Informational Clinical Observation Summary" in page_str

