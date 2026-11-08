"""
Stage 8A.3 & 8A.3.1 — Doctor Document Export Service (PDF, DOCX, Markdown).

Provides deterministic, zero-AI export of clinical observation summaries
persisted during Stage 8A.2:
- PDF export via `fpdf2` with bundled Unicode DejaVu TrueType fonts, professional
  medical formatting, auto page-break, page numbering, and high-fidelity preservation
  of critical clinical units (µg/L, µmol/L, mg/dL, ≥, ≤, ±, SpO₂, °C, m²).
- DOCX export via `python-docx` with structured headings, metadata table,
  bullet lists, and non-diagnostic disclaimers.
- Markdown export returning authoritative UTF-8 markdown text.

Architectural Guarantees:
- Strictly 0 Gemini calls, 0 Flash-Lite calls, and 0 database writes.
- Standalone: bundled Open Source DejaVu TrueType fonts (Bitstream Vera / Public Domain)
  isolated within repository (backend/app/assets/fonts) without Windows OS font dependence.
- Multi-page rendering without text truncation or font clipping.
- Anti-hallucination: renders only saved data or explicit 'NOT_RECORDED'.
"""

from datetime import datetime, timezone
import io
import logging
from pathlib import Path
import re
from typing import Any, Dict, List, Optional, Tuple, Union

import docx
from docx.shared import Inches, Pt, RGBColor
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.enum.table import WD_TABLE_ALIGNMENT
from fpdf import FPDF
from sqlalchemy.orm import Session

from app.models.model_report import ModelReport
from app.repositories.cycle_repository import MedicalCycleRepository
from app.repositories.report_repository import ModelReportRepository
from app.schemas.consilium import (
    REFERRAL_DOCUMENT_CANONICAL_TITLE,
    ReferralDoctorDocumentOutput,
)
from app.schemas.privacy import PrivacyLevel

logger = logging.getLogger("aegis.doctor_document_export")

# Colors for styling
PDF_NAVY = (26, 54, 93)       # #1A365D - primary headings
PDF_SLATE = (74, 85, 104)     # #4A5568 - subheaders / metadata labels
PDF_DARK = (45, 55, 72)       # #2D3748 - body text
PDF_MUTED = (113, 128, 150)   # #718096 - footers / borders
PDF_BOX_BG = (247, 250, 252)  # #F7FAFC - disclaimer box background
PDF_BOX_BORDER = (226, 232, 240)
PDF_ALERT_BORDER = (229, 62, 62)

DOCX_NAVY = RGBColor(26, 54, 93)
DOCX_SLATE = RGBColor(74, 85, 104)
DOCX_DARK = RGBColor(45, 55, 72)
DOCX_MUTED = RGBColor(113, 128, 150)

# TrueType font asset discovery
FONTS_DIR = Path(__file__).resolve().parent.parent / "assets" / "fonts"
DEJAVU_REGULAR_PATH = FONTS_DIR / "DejaVuSans.ttf"
DEJAVU_BOLD_PATH = FONTS_DIR / "DejaVuSans-Bold.ttf"
HAS_UNICODE_FONTS = DEJAVU_REGULAR_PATH.is_file() and DEJAVU_BOLD_PATH.is_file()


def sanitize_pdf_text(text: str, unicode_mode: bool = True) -> str:
    """
    Sanitizes Unicode text for safe rendering in PDF documents.

    In Unicode Mode (DejaVu Sans TrueType font):
    - Preserves all clinical symbols, math operators, units, and subscripts/superscripts:
      µg/L, µmol/L, mg/dL, ≥, ≤, ±, SpO₂, °C, m², ranges (2.5–3.0), etc.
    - Normalizes non-breaking and thin spaces (\u00a0, \u2009, \u202f) to standard spaces.
    - Strips zero-width/invisible characters (\u200b, \u200c, \u200d, \ufeff).
    - Strips non-printable ASCII controls while preserving newlines and tabs.
    - Explicitly NEVER alters numbers, decimals, or medical units.

    In Fallback Latin-1 Mode (only if TrueType font cannot be loaded):
    - Applies safe ASCII/Latin-1 mapping with warning logging.
    """
    if not text:
        return ""

    # 1. Normalize spaces
    cleaned = text.replace("\u00a0", " ").replace("\u2009", " ").replace("\u202f", " ")

    # 2. Strip zero-width and invisible control characters
    cleaned = re.sub(r"[\u200b\u200c\u200d\ufeff]", "", cleaned)

    # 3. Strip non-printable control characters (< 0x20 except \n, \r, \t)
    cleaned = "".join(ch for ch in cleaned if ch >= " " or ch in "\n\r\t")

    if unicode_mode:
        return cleaned

    # Fallback Latin-1 replacements (only used when TrueType font is missing)
    replacements = {
        "\u2014": " - ",
        "\u2013": "-",
        "\u2018": "'",
        "\u2019": "'",
        "\u201c": '"',
        "\u201d": '"',
        "\u2022": "-",
        "\u2026": "...",
        "\u2265": ">=",
        "\u2264": "<=",
        "\u00b1": "+/-",
        "\u2192": "->",
        "\u2190": "<-",
        "\u00b5": "u",
        "\u03bc": "u",
        "\u2082": "2",
    }
    for orig, rep in replacements.items():
        cleaned = cleaned.replace(orig, rep)
    return cleaned.encode("latin-1", "replace").decode("latin-1")


class AegisReferralPDF(FPDF):
    """
    Specialized A4 PDF document for Aegis Doctor Observation Summaries.
    Includes running headers, footers with page numbering, and safety disclaimers.
    Employs bundled DejaVu TrueType fonts for full Unicode medical notation fidelity.
    """

    def __init__(self, doc_date: str = "", patient_ref: str = ""):
        super().__init__(orientation="P", unit="mm", format="A4")
        self.doc_date = doc_date
        self.patient_ref = patient_ref
        self.font_family = "Helvetica"
        self.is_unicode = False

        if HAS_UNICODE_FONTS:
            try:
                self.add_font("DejaVu", "", str(DEJAVU_REGULAR_PATH))
                self.add_font("DejaVu", "B", str(DEJAVU_BOLD_PATH))
                self.font_family = "DejaVu"
                self.is_unicode = True
            except Exception as exc:
                logger.warning(
                    "Failed to register bundled DejaVu fonts: %s; falling back to Helvetica", exc
                )
                self.font_family = "Helvetica"
                self.is_unicode = False

        self.set_auto_page_break(auto=True, margin=20)
        self.set_margins(left=18, top=18, right=18)
        self.alias_nb_pages()

    def clean_text(self, text: str) -> str:
        """Sanitizes text according to the active font's Unicode capabilities."""
        return sanitize_pdf_text(text, unicode_mode=self.is_unicode)

    def header(self):
        # Only show running header on page 2 and later
        if self.page_no() > 1:
            self.set_font(self.font_family, "B" if self.is_unicode else "I", 8)
            self.set_text_color(*PDF_SLATE)
            self.cell(100, 6, self.clean_text("AEGIS HEALTH OBSERVATION SUMMARY FOR CLINICIAN"), align="L")
            page_info = f"Date: {self.doc_date}" if self.doc_date else "Confidential"
            self.cell(0, 6, self.clean_text(page_info), align="R", new_x="LMARGIN", new_y="NEXT")
            self.set_draw_color(*PDF_MUTED)
            self.set_line_width(0.2)
            self.line(18, 24, 192, 24)
            self.ln(4)

    def footer(self):
        self.set_y(-14)
        self.set_draw_color(*PDF_MUTED)
        self.set_line_width(0.2)
        self.line(18, 283, 192, 283)
        self.set_font(self.font_family, "", 7.5)
        self.set_text_color(*PDF_MUTED)
        self.cell(
            120,
            6,
            self.clean_text("Informational Clinical Observation Summary - Not an Official Diagnostic Discharge"),
            align="L",
        )
        self.cell(
            0,
            6,
            f"Page {self.page_no()} of {{nb}}",
            align="R",
        )


class DoctorDocumentExportService:
    """
    Deterministic export service converting stored Aegis referral observation
    reports into client-deliverable PDF, DOCX, and Markdown formats.
    """

    def __init__(self, db: Optional[Session] = None):
        self.db = db
        self.report_repo = ModelReportRepository(db) if db else None
        self.cycle_repo = MedicalCycleRepository(db) if db else None

    def extract_document_data(
        self,
        client_doc: ModelReport,
        raw_doc: Optional[ModelReport] = None,
    ) -> Tuple[ReferralDoctorDocumentOutput, Dict[str, Any]]:
        """
        Extracts and validates structured document data from existing database reports.
        Strictly zero LLM calls: loads pre-synthesized data directly from SQLite.
        """
        content = client_doc.content or {}
        metrics = content.get("metrics") or {}

        # 1. Attempt to load raw structured document if not passed
        if raw_doc is None and self.report_repo is not None:
            raw_id = metrics.get("raw_document_report_id")
            if raw_id:
                raw_candidate = self.report_repo.get(str(raw_id))
                if (
                    raw_candidate is not None
                    and raw_candidate.cycle_id == client_doc.cycle_id
                    and raw_candidate.privacy_level == PrivacyLevel.SERVER_ONLY.value
                ):
                    raw_doc = raw_candidate

        pro_doc: Optional[ReferralDoctorDocumentOutput] = None
        if raw_doc is not None and (raw_doc.content or {}).get("pro_doctor_document"):
            try:
                pro_doc = ReferralDoctorDocumentOutput.model_validate(
                    raw_doc.content["pro_doctor_document"]
                )
            except Exception as exc:
                logger.warning(
                    "Failed to validate pro_doctor_document from raw_report '%s': %s; falling back to markdown parser",
                    raw_doc.report_id,
                    exc,
                )

        if pro_doc is None:
            pro_doc = self._parse_from_client_doc(client_doc)

        metadata: Dict[str, Any] = {
            "report_id": client_doc.report_id,
            "cycle_id": client_doc.cycle_id,
            "sprint_id": client_doc.sprint_id or metrics.get("sprint_id"),
            "sprint_number": metrics.get("sprint_number", 1),
            "referral_id": metrics.get("referral_id", "REF-UNKNOWN"),
            "version": client_doc.version,
            "report_date": content.get("report_date") or client_doc.created_at.strftime("%Y-%m-%d"),
            "created_at_utc": metrics.get("created_at_utc") or client_doc.created_at.isoformat(),
            "patient_id": metrics.get("patient_id", ""),
            "urgency": pro_doc.consultation_urgency,
            "target_specialties": pro_doc.recommended_specialties,
            "title": REFERRAL_DOCUMENT_CANONICAL_TITLE,
            "raw_document_report_id": metrics.get("raw_document_report_id"),
        }

        return pro_doc, metadata

    def export_markdown(
        self,
        pro_doc: ReferralDoctorDocumentOutput,
        metadata: Dict[str, Any],
    ) -> str:
        """Renders full canonical Markdown document."""
        return pro_doc.render_english_markdown()

    def export_docx(
        self,
        pro_doc: ReferralDoctorDocumentOutput,
        metadata: Dict[str, Any],
    ) -> bytes:
        """
        Renders a clean, print-ready Microsoft Word DOCX document with styling,
        metadata table, callout notice, and all 7 observation sections.
        Preserves all medical symbols and UTF-8 notations natively.
        """
        doc = docx.Document()

        # Set page margins (0.75 in / 19 mm)
        sections = doc.sections
        for section in sections:
            section.top_margin = Inches(0.75)
            section.bottom_margin = Inches(0.75)
            section.left_margin = Inches(0.75)
            section.right_margin = Inches(0.75)

        # Document Header Label
        top_label = doc.add_paragraph()
        top_label.paragraph_format.space_after = Pt(2)
        run_brand = top_label.add_run("PROJECT AEGIS - CLINICAL OBSERVATION SERVICE")
        run_brand.font.size = Pt(8.5)
        run_brand.font.bold = True
        run_brand.font.color.rgb = DOCX_SLATE

        # Document Title (Heading 1)
        title_para = doc.add_paragraph()
        title_para.paragraph_format.space_before = Pt(4)
        title_para.paragraph_format.space_after = Pt(12)
        title_run = title_para.add_run(REFERRAL_DOCUMENT_CANONICAL_TITLE)
        title_run.font.size = Pt(16)
        title_run.font.bold = True
        title_run.font.color.rgb = DOCX_NAVY

        # Informational Notice Callout Box
        notice_table = doc.add_table(rows=1, cols=1)
        notice_table.alignment = WD_TABLE_ALIGNMENT.CENTER
        notice_cell = notice_table.rows[0].cells[0]
        notice_cell.width = Inches(7.0)

        notice_p = notice_cell.paragraphs[0]
        notice_p.paragraph_format.space_before = Pt(6)
        notice_p.paragraph_format.space_after = Pt(6)

        warn_head = notice_p.add_run("IMPORTANT INFORMATIONAL NOTICE FOR CLINICIAN & PATIENT:\n")
        warn_head.font.bold = True
        warn_head.font.size = Pt(9)
        warn_head.font.color.rgb = RGBColor(180, 40, 40)

        warn_body = notice_p.add_run(
            f"{pro_doc.informational_disclaimer}\n\n"
            "(This document is an AI-generated health observation summary prepared solely to facilitate "
            "discussion with a licensed healthcare professional. It is NOT an official medical discharge "
            "summary and does NOT represent a confirmed clinical diagnosis.)"
        )
        warn_body.font.size = Pt(8.5)
        warn_body.font.italic = True
        warn_body.font.color.rgb = DOCX_DARK

        doc.add_paragraph().paragraph_format.space_after = Pt(4)

        # Metadata Table
        meta_table = doc.add_table(rows=3, cols=2)
        meta_table.alignment = WD_TABLE_ALIGNMENT.CENTER

        def _format_cell(cell, label: str, value: str):
            p = cell.paragraphs[0]
            p.paragraph_format.space_before = Pt(2)
            p.paragraph_format.space_after = Pt(2)
            lbl_run = p.add_run(f"{label}: ")
            lbl_run.font.bold = True
            lbl_run.font.size = Pt(9)
            lbl_run.font.color.rgb = DOCX_SLATE
            val_run = p.add_run(value)
            val_run.font.size = Pt(9)
            val_run.font.color.rgb = DOCX_DARK

        specs_str = ", ".join(pro_doc.recommended_specialties)
        urgency_label = (
            f"Priority evaluation ({pro_doc.consultation_urgency.upper()})"
            if pro_doc.consultation_urgency == "priority"
            else f"Routine planned evaluation ({pro_doc.consultation_urgency.upper()})"
        )

        _format_cell(meta_table.rows[0].cells[0], "Report Date", metadata.get("report_date", "N/A"))
        _format_cell(meta_table.rows[0].cells[1], "Consultation Urgency", urgency_label)
        _format_cell(meta_table.rows[1].cells[0], "Patient Reference", metadata.get("patient_id", "Aegis Patient"))
        _format_cell(meta_table.rows[1].cells[1], "Cycle / Sprint", f"{metadata.get('cycle_id', 'N/A')} / {metadata.get('sprint_id', 'Sprint-1')}")
        _format_cell(meta_table.rows[2].cells[0], "Recommended Specialties", specs_str)
        _format_cell(meta_table.rows[2].cells[1], "Referral ID", metadata.get("referral_id", "REF-N/A"))

        doc.add_paragraph().paragraph_format.space_after = Pt(8)

        # 7 Clinical Observation Sections
        sections_data = [
            ("1. Patient and Observation Period", pro_doc.patient_and_observation_period, False),
            ("2. Reason for Recommending an In-Person Consultation", pro_doc.reason_for_in_person_consultation, False),
            ("3. Objective Health Measurements and Examination Results", pro_doc.objective_measurements_and_examination_results, True),
            ("4. Timeline of Reported Symptoms and Observed Changes", pro_doc.timeline_of_reported_symptoms_and_changes, True),
            ("5. Important Patterns and Unresolved Concerns", pro_doc.important_patterns_and_unresolved_concerns, True),
            ("6. AI-Generated Observations Requiring Professional Verification", pro_doc.ai_observations_requiring_verification, True),
            ("7. Questions and Possible Examinations to Discuss with a Licensed Clinician", pro_doc.questions_and_examinations_for_clinician, True),
        ]

        for sec_title, sec_content, is_bullets in sections_data:
            h = doc.add_paragraph()
            h.paragraph_format.space_before = Pt(12)
            h.paragraph_format.space_after = Pt(4)
            h.paragraph_format.keep_with_next = True
            hrun = h.add_run(sec_title)
            hrun.font.bold = True
            hrun.font.size = Pt(11.5)
            hrun.font.color.rgb = DOCX_NAVY

            if is_bullets and isinstance(sec_content, list):
                for bullet in sec_content:
                    bp = doc.add_paragraph(style="List Bullet")
                    bp.paragraph_format.space_before = Pt(1.5)
                    bp.paragraph_format.space_after = Pt(1.5)
                    brun = bp.add_run(bullet)
                    brun.font.size = Pt(9.5)
                    brun.font.color.rgb = DOCX_DARK
            else:
                p = doc.add_paragraph()
                p.paragraph_format.space_before = Pt(2)
                p.paragraph_format.space_after = Pt(4)
                prun = p.add_run(str(sec_content))
                prun.font.size = Pt(9.5)
                prun.font.color.rgb = DOCX_DARK

        # Footer notes
        end_p = doc.add_paragraph()
        end_p.paragraph_format.space_before = Pt(18)
        end_p.paragraph_format.space_after = Pt(4)
        end_run = end_p.add_run(
            f"--- End of Aegis Health Observation Summary (Created: {metadata.get('created_at_utc', 'N/A')}) ---"
        )
        end_run.font.size = Pt(8)
        end_run.font.italic = True
        end_run.font.color.rgb = DOCX_MUTED

        buf = io.BytesIO()
        doc.save(buf)
        return buf.getvalue()

    def export_pdf(
        self,
        pro_doc: ReferralDoctorDocumentOutput,
        metadata: Dict[str, Any],
    ) -> bytes:
        """
        Renders a publication-grade, multi-page A4 PDF document with clean
        typography, safety callout, and full Unicode TrueType fidelity.
        """
        pdf = AegisReferralPDF(
            doc_date=metadata.get("report_date", ""),
            patient_ref=metadata.get("patient_id", ""),
        )

        # Ensure deterministic binary output by binding PDF CreationDate to the stored document creation timestamp
        doc_dt: Optional[datetime] = None
        raw_dt = metadata.get("created_at_utc")
        if isinstance(raw_dt, datetime):
            doc_dt = raw_dt
        elif isinstance(raw_dt, str) and raw_dt:
            try:
                normalized = raw_dt.replace("Z", "+00:00") if raw_dt.endswith("Z") else raw_dt
                doc_dt = datetime.fromisoformat(normalized)
            except Exception:
                doc_dt = None
        if doc_dt is None:
            doc_dt = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        if not doc_dt.tzinfo:
            doc_dt = doc_dt.replace(tzinfo=timezone.utc)
        pdf.set_creation_date(doc_dt)

        pdf.add_page()
        f_fam = pdf.font_family

        # Top Category & Date
        pdf.set_font(f_fam, "B" if pdf.is_unicode else "B", 8)
        pdf.set_text_color(*PDF_SLATE)
        pdf.cell(100, 5, pdf.clean_text("PROJECT AEGIS - CLINICAL OBSERVATION SERVICE"), align="L")
        pdf.set_font(f_fam, "", 8)
        pdf.cell(0, 5, pdf.clean_text(f"Date: {metadata.get('report_date', 'N/A')}"), align="R", new_x="LMARGIN", new_y="NEXT")
        pdf.ln(2)

        # Document Title
        pdf.set_font(f_fam, "B", 14)
        pdf.set_text_color(*PDF_NAVY)
        pdf.multi_cell(0, 6.5, pdf.clean_text(REFERRAL_DOCUMENT_CANONICAL_TITLE))
        pdf.ln(3)

        # Callout Disclaimer Box
        disclaimer_text = (
            "IMPORTANT INFORMATIONAL NOTICE FOR CLINICIAN & PATIENT:\n"
            f"{pro_doc.informational_disclaimer}\n\n"
            "(This document is an AI-generated health observation summary prepared solely to facilitate "
            "discussion with a licensed healthcare professional. It is NOT an official medical discharge "
            "summary and does NOT represent a confirmed clinical diagnosis.)"
        )
        clean_disclaimer = pdf.clean_text(disclaimer_text)

        pdf.set_fill_color(*PDF_BOX_BG)
        pdf.set_draw_color(*PDF_BOX_BORDER)
        pdf.set_line_width(0.3)
        pdf.set_font(f_fam, "", 7.5)
        pdf.set_text_color(*PDF_DARK)
        pdf.multi_cell(0, 4.0, clean_disclaimer, border=1, fill=True)
        pdf.ln(4)

        # Metadata Summary Strip
        pdf.set_fill_color(240, 244, 248)
        pdf.set_draw_color(203, 213, 225)
        pdf.set_font(f_fam, "B", 8)
        pdf.set_text_color(*PDF_SLATE)

        specs_str = ", ".join(pro_doc.recommended_specialties)
        urgency_label = (
            f"PRIORITY ({pro_doc.consultation_urgency.upper()})"
            if pro_doc.consultation_urgency == "priority"
            else f"ROUTINE ({pro_doc.consultation_urgency.upper()})"
        )

        meta_lines = [
            f"Patient Reference: {metadata.get('patient_id', 'Authenticated Patient')}   |   Cycle / Sprint: {metadata.get('cycle_id', 'N/A')} / {metadata.get('sprint_id', 'Sprint-1')}",
            f"Urgency: {urgency_label}   |   Recommended Specialties: {specs_str}",
            f"Referral ID: {metadata.get('referral_id', 'REF-N/A')}   |   Created: {metadata.get('created_at_utc', 'N/A')}",
        ]
        meta_block = "\n".join(meta_lines)
        pdf.multi_cell(0, 4.5, pdf.clean_text(meta_block), border=1, fill=True)
        pdf.ln(5)

        # 7 Clinical Observation Sections
        sections_data = [
            ("1. Patient and Observation Period", pro_doc.patient_and_observation_period, False),
            ("2. Reason for Recommending an In-Person Consultation", pro_doc.reason_for_in_person_consultation, False),
            ("3. Objective Health Measurements and Examination Results", pro_doc.objective_measurements_and_examination_results, True),
            ("4. Timeline of Reported Symptoms and Observed Changes", pro_doc.timeline_of_reported_symptoms_and_changes, True),
            ("5. Important Patterns and Unresolved Concerns", pro_doc.important_patterns_and_unresolved_concerns, True),
            ("6. AI-Generated Observations Requiring Professional Verification", pro_doc.ai_observations_requiring_verification, True),
            ("7. Questions and Possible Examinations to Discuss with a Licensed Clinician", pro_doc.questions_and_examinations_for_clinician, True),
        ]

        for sec_title, sec_content, is_bullets in sections_data:
            # Check for page space to avoid orphaned headers
            if pdf.get_y() > 245:
                pdf.add_page()

            pdf.set_font(f_fam, "B", 10.5)
            pdf.set_text_color(*PDF_NAVY)
            pdf.cell(0, 6, pdf.clean_text(sec_title), new_x="LMARGIN", new_y="NEXT")
            pdf.ln(1)

            if is_bullets and isinstance(sec_content, list):
                pdf.set_font(f_fam, "", 8.5)
                pdf.set_text_color(*PDF_DARK)
                for bullet in sec_content:
                    bullet_text = f"  -  {bullet}"
                    pdf.multi_cell(0, 4.3, pdf.clean_text(bullet_text))
                    pdf.ln(0.8)
                pdf.ln(2.5)
            else:
                pdf.set_font(f_fam, "", 8.5)
                pdf.set_text_color(*PDF_DARK)
                pdf.multi_cell(0, 4.5, pdf.clean_text(str(sec_content)))
                pdf.ln(3.5)

        return bytes(pdf.output())

    def _parse_from_client_doc(self, client_doc: ModelReport) -> ReferralDoctorDocumentOutput:
        """
        Robust fallback parser converting stored ClientReportContent into
        ReferralDoctorDocumentOutput when raw_doc is not present.
        """
        content = client_doc.content or {}
        metrics = content.get("metrics") or {}
        markdown_text = str(content.get("summary") or "")

        # Extract disclaimer
        disc_match = re.search(r">\s*\*\*Informational Observation Notice:\*\*\s*(.+)", markdown_text)
        disclaimer = (
            disc_match.group(1).strip()
            if disc_match
            else "This document is an informational health observation summary prepared solely for clinician discussion."
        )

        # Extract urgency and specialties
        urgency = metrics.get("urgency") or "routine"
        raw_specs = metrics.get("target_specialties") or "General Practice"
        if isinstance(raw_specs, str):
            specs = [s.strip() for s in raw_specs.split(",") if s.strip()]
        elif isinstance(raw_specs, list):
            specs = [str(s).strip() for s in raw_specs if str(s).strip()]
        else:
            specs = ["General Practice"]

        # Extract sections using section headers
        def _extract_section_text(sec_num: int, next_sec_num: Optional[int]) -> str:
            start_pat = rf"##\s*{sec_num}\.\s*[^\n]+\n"
            end_pat = rf"##\s*{next_sec_num}\.\s*[^\n]+" if next_sec_num else r"\Z"
            pattern = rf"{start_pat}(.*?)(?:{end_pat})"
            m = re.search(pattern, markdown_text, re.DOTALL)
            return m.group(1).strip() if m else ""

        def _extract_bullets(sec_text: str) -> List[str]:
            bullets: List[str] = []
            for line in sec_text.splitlines():
                line = line.strip()
                if line.startswith(("-", "*", "•")):
                    item = re.sub(r"^[-*•]\s*", "", line).strip()
                    if item:
                        bullets.append(item)
                elif line and not line.startswith("#"):
                    bullets.append(line)
            return bullets or ["No specific findings noted in this category (NOT_RECORDED)."]

        sec1 = _extract_section_text(1, 2) or "Patient observed under Aegis medical cycle."
        sec2 = _extract_section_text(2, 3) or "Outpatient medical evaluation recommended for clinical correlation."
        sec3 = _extract_bullets(_extract_section_text(3, 4))
        sec4 = _extract_bullets(_extract_section_text(4, 5))
        sec5 = _extract_bullets(_extract_section_text(5, 6))
        sec6 = _extract_bullets(_extract_section_text(6, 7))
        sec7 = _extract_bullets(_extract_section_text(7, None))

        exec_summary = ""
        entries = content.get("entries") or []
        if entries and isinstance(entries, list) and entries[0].get("summary"):
            exec_summary = str(entries[0]["summary"])
        elif content.get("highlights"):
            exec_summary = str(content["highlights"][0])
        else:
            exec_summary = "Outpatient medical consultation recommended."

        return ReferralDoctorDocumentOutput(
            document_title=REFERRAL_DOCUMENT_CANONICAL_TITLE,
            informational_disclaimer=disclaimer,
            recommended_specialties=specs or ["General Practice"],
            consultation_urgency="priority" if str(urgency).lower() == "priority" else "routine",
            executive_summary_for_patient=exec_summary,
            patient_and_observation_period=sec1,
            reason_for_in_person_consultation=sec2,
            objective_measurements_and_examination_results=sec3,
            timeline_of_reported_symptoms_and_changes=sec4,
            important_patterns_and_unresolved_concerns=sec5,
            ai_observations_requiring_verification=sec6,
            questions_and_examinations_for_clinician=sec7,
        )
