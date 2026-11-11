"""
Stage 8A.2 — Server Referral Document Generation Service (Flash -> Pro).

Executes a dedicated two-step analytical cascade via GeminiGateway after Weekly
Chief Physician (Pro) issues `referral_decision.recommended = True`:
1. Gemini Flash — Clinical History Extractor:
   Compiles a source-attributed, chronological clinical inventory from a bounded,
   server-prepared clinical snapshot without inventing dates, metrics, units,
   diagnoses, or missing examinations.
2. Gemini Pro — Doctor Observation Document Author:
   Synthesizes the structured English observation summary
   'AEGIS — HEALTH OBSERVATION SUMMARY FOR YOUR DOCTOR' across 7 mandatory sections,
   strictly separating objective measurements, patient-reported complaints, and
   AI hypotheses requiring verification by a licensed clinician.

Key Architectural & Safety Invariants:
- Strictly 2 model calls (Flash -> Pro); ZERO calls to the 8 Flash-Lite models.
- Never re-runs Weekly Consilium on document failure.
- Atomic persistence: SERVER_ONLY raw structured document, CLIENT_FACING validated
  ClientReportContent(extra='forbid') document, and SERVER_ONLY referral_lifecycle_state
  transition to 'document_ready' commit together in a single SQLite transaction.
- Never marks 'document_ready' unless the verified CLIENT_FACING document exists in DB.
- Preserves prior document versions and history across subsequent Weekly sprints.
- Refuses to publish an active referral document if the cycle is no longer active
  (e.g., closed due to Neo reassessment).
"""

from datetime import datetime, timezone
import logging
from typing import Any, Callable, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, ValidationError
from sqlalchemy.orm import Session

from app.models.model_report import ModelReport
from app.repositories.cycle_repository import MedicalCycleRepository
from app.repositories.measurement_repository import HealthMeasurementRepository
from app.repositories.report_repository import ModelReportRepository
from app.repositories.sprint_repository import WeeklySprintRepository
from app.schemas.consilium import (
    REFERRAL_DOCUMENT_CANONICAL_TITLE,
    ReferralClinicalHistoryExtraction,
    ReferralDoctorDocumentOutput,
)
from app.schemas.privacy import PrivacyLevel
from app.schemas.reports import ClientDatedSystemEntry, ClientReportContent
from app.services.athena_daily_journal_service import (
    ATHENA_CLINICAL_LOG_REPORT_TYPE,
    ATHENA_DAILY_JOURNAL_REPORT_TYPE,
)
from app.services.consilium_prompts import (
    build_referral_flash_extractor_prompt,
    build_referral_pro_document_prompt,
)
from app.services.ecg_context_service import (
    assemble_consilium_ecg_context,
    sanitize_pii_text,
)
from app.services.gemini_gateway import GeminiGateway, GeminiModelRole
from app.services.audit_trail_service import audit_scope
from app.services.lab_context_service import assemble_consilium_laboratory_context
from app.services.mode2_vitals_service import ATHENA_MODE2_EPISODE_REPORT_TYPE
from app.services.primary_consilium_service import (
    ConsiliumContextMissingError,
    ConsiliumCycleNotFoundError,
    ConsiliumError,
    PREPARATORY_ANAMNESIS_REPORT_TYPE,
)
from app.services.weekly_consilium_service import (
    REFERRAL_LIFECYCLE_STATE_REPORT_TYPE,
    WEEKLY_CHIEF_INTERNAL_REPORT_TYPE,
    WeeklyConsiliumService,
)
from app.services.wellness_daily_service import WELLNESS_DAILY_REPORT_TYPE

logger = logging.getLogger("aegis.referral_document")

REFERRAL_DOCTOR_DOCUMENT_REPORT_TYPE: str = "referral_doctor_document"
REFERRAL_DOCTOR_DOCUMENT_RAW_REPORT_TYPE: str = "referral_doctor_document_raw"

# Bounded context limits to prevent prompt window bloat over 120+ day cycles
MAX_SNAPSHOT_LAB_HISTORY: int = 60
MAX_SNAPSHOT_VITALS: int = 60
MAX_SNAPSHOT_WELLNESS_DAYS: int = 30
MAX_SNAPSHOT_MODE2_EPISODES: int = 25
MAX_SNAPSHOT_ATHENA_JOURNALS: int = 28
MAX_SNAPSHOT_ATHENA_LOGS: int = 40
MAX_SNAPSHOT_PRIOR_CONSILIUMS: int = 8


class ReferralDocumentError(ConsiliumError):
    """Base exception for Stage 8A.2 Referral Document generation."""


class ReferralCycleInactiveError(ReferralDocumentError):
    """Raised when attempting to publish an active referral document for a closed/Neo cycle."""


class ReferralContextMissingError(ReferralDocumentError):
    """Raised when no active referral lifecycle state exists for the cycle."""


class ReferralDocumentSchemaViolationError(ReferralDocumentError):
    """Raised when client-facing document payload violates strict ClientReportContent(extra='forbid')."""


class ReferralDocumentGenerationResult(BaseModel):
    """Structured outcome of ReferralDocumentService.generate_referral_document."""
    model_config = ConfigDict(extra="forbid")

    cycle_id: str
    patient_id: str
    sprint_id: str
    sprint_number: int
    referral_id: str
    document_report_id: str
    raw_document_report_id: Optional[str] = None
    document_version: int
    status: str
    created_at_utc: str
    reused_existing: bool = False


class ReferralDocumentService:
    """
    Dedicated server-side service executing the Flash -> Pro Referral Document cascade
    and atomically persisting both SERVER_ONLY and CLIENT_FACING versions.
    """

    def __init__(
        self,
        db: Session,
        gateway: Optional[GeminiGateway] = None,
        now_provider: Optional[Callable[[], datetime]] = None,
    ):
        self.db = db
        self.gateway = gateway or GeminiGateway()
        self.now_provider = now_provider or (lambda: datetime.now(timezone.utc))

        self.cycle_repo = MedicalCycleRepository(db)
        self.report_repo = ModelReportRepository(db)
        self.measurement_repo = HealthMeasurementRepository(db)
        self.sprint_repo = WeeklySprintRepository(db)
        self.weekly_service = WeeklyConsiliumService(
            db=db,
            gateway=self.gateway,
            now_provider=self.now_provider,
        )

    def _now_utc(self) -> datetime:
        dt = self.now_provider()
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)

    @staticmethod
    def _format_dt_iso(dt: Optional[datetime]) -> str:
        if dt is None:
            return "Date not recorded"
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc).isoformat()
        return dt.astimezone(timezone.utc).isoformat()

    def _find_existing_document_for_sprint(
        self,
        cycle_id: str,
        referral_id: str,
        target_sprint_id: str,
    ) -> Optional[ModelReport]:
        """
        Checks whether a verified CLIENT_FACING referral_doctor_document already exists
        for (cycle_id, referral_id, target_sprint_id) to guarantee strict idempotency.
        """
        client_docs = self.report_repo.list_client_facing(
            cycle_id=cycle_id,
            report_type=REFERRAL_DOCTOR_DOCUMENT_REPORT_TYPE,
        )
        for doc in client_docs:
            if doc.cycle_id != cycle_id:
                continue
            metrics = (doc.content or {}).get("metrics") or {}
            if (
                str(metrics.get("referral_id") or "") == referral_id
                and str(metrics.get("sprint_id") or doc.sprint_id or "") == target_sprint_id
            ):
                return doc
        return None

    def build_client_document_content(
        self,
        *,
        pro_doc: ReferralDoctorDocumentOutput,
        patient_id: str,
        cycle_id: str,
        sprint_id: str,
        sprint_number: int,
        referral_id: str,
        referral_source: str,
        created_at_utc: str,
        report_date: str,
        raw_document_report_id: Optional[str] = None,
        extra_client_overrides: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Builds and strictly validates the CLIENT_FACING payload against
        ClientReportContent(extra='forbid'). Any schema violation raises
        ReferralDocumentSchemaViolationError instead of bypassing validation.
        """
        rendered_markdown = pro_doc.render_english_markdown()
        specs_csv = ", ".join(pro_doc.recommended_specialties)

        highlights: List[str] = [
            f"Recommended consultation: {specs_csv} ({pro_doc.consultation_urgency})",
            pro_doc.executive_summary_for_patient,
        ]
        for item in pro_doc.questions_and_examinations_for_clinician[:3]:
            if item and item not in highlights:
                highlights.append(item)

        payload: Dict[str, Any] = {
            "title": REFERRAL_DOCUMENT_CANONICAL_TITLE,
            "summary": rendered_markdown,
            "report_date": report_date,
            "system_key": None,
            "has_changes": True,
            "highlights": highlights,
            "metrics": {
                "patient_id": patient_id,
                "cycle_id": cycle_id,
                "sprint_id": sprint_id,
                "sprint_number": int(sprint_number),
                "referral_id": referral_id,
                "created_at_utc": created_at_utc,
                "referral_source": referral_source,
                "urgency": pro_doc.consultation_urgency,
                "target_specialties": specs_csv,
                "raw_document_report_id": raw_document_report_id,
                "is_informational_observation_summary": True,
            },
            "entries": [
                ClientDatedSystemEntry(
                    date=report_date,
                    summary=pro_doc.executive_summary_for_patient,
                    has_changes=True,
                ).model_dump()
            ],
        }
        if extra_client_overrides:
            payload.update(extra_client_overrides)

        try:
            validated = ClientReportContent.model_validate(payload)
        except ValidationError as exc:
            raise ReferralDocumentSchemaViolationError(
                f"Invalid client-facing referral_doctor_document structure rejected by "
                f"ClientReportContent(extra='forbid'): {exc}"
            ) from exc

        return validated.model_dump(exclude_none=True)

    def _assemble_bounded_clinical_snapshot(
        self,
        *,
        cycle_id: str,
        patient_id: str,
        cycle_created_at: Optional[datetime],
        referral_state: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        Assembles a bounded, strictly patient-isolated, server-authoritative clinical
        snapshot for Gemini Flash Clinical History Extractor.
        Explicitly declares absent data categories so missing data is never fabricated.
        """
        now_dt = self._now_utc()

        # 1. Initial Anamnesis (M1)
        anamnesis_rows = [
            r
            for r in self.report_repo.list_server_only(
                cycle_id=cycle_id,
                report_type=PREPARATORY_ANAMNESIS_REPORT_TYPE,
            )
            if r.cycle_id == cycle_id
        ]
        if anamnesis_rows:
            latest_m1 = max(anamnesis_rows, key=lambda r: int(r.version or 0))
            m1_content = latest_m1.content or {}
            user_view = m1_content.get("user_view") or {}
            internal_summary = m1_content.get("clinical_summary") or m1_content.get("anamnesis_summary")
            initial_anamnesis: Dict[str, Any] = {
                "available": True,
                "source_attribution": f"preparatory_anamnesis_m1 (v{latest_m1.version})",
                "recorded_at_utc": self._format_dt_iso(latest_m1.created_at),
                "is_patient_self_reported": True,
                "user_view_summary": user_view,
                "clinical_intake_summary": (
                    sanitize_pii_text(str(internal_summary))
                    if internal_summary
                    else None
                ),
            }
        else:
            initial_anamnesis = {
                "available": False,
                "status": "NOT_RECORDED",
                "note": "No initial anamnesis report recorded for this cycle. Do not invent baseline history.",
            }

        # 2. Laboratory Studies & Revisions (with exact sample dates, values, units, reference ranges)
        try:
            lab_ctx = assemble_consilium_laboratory_context(self.db, cycle_id)
            has_labs = bool(lab_ctx.shared_labs_table or lab_ctx.laboratory_history)
        except ConsiliumContextMissingError:
            lab_ctx = None
            has_labs = False

        if has_labs and lab_ctx is not None:
            bounded_lab_history = list(lab_ctx.laboratory_history[-MAX_SNAPSHOT_LAB_HISTORY:])
            laboratory_studies: Dict[str, Any] = {
                "available": True,
                "source_attribution": f"laboratory_ledger (v{lab_ctx.ledger_version})",
                "ledger_version": lab_ctx.ledger_version,
                "active_markers_with_units": lab_ctx.shared_labs_table,
                "chronological_observations": bounded_lab_history,
                "total_observations_count": len(lab_ctx.laboratory_history),
            }
        else:
            laboratory_studies = {
                "available": False,
                "status": "NOT_RECORDED",
                "note": "NO LABORATORY STUDIES RECORDED. Do not invent laboratory markers, dates, values, or units.",
            }

        # 3. ECG & Instrumental Examinations + Objective Biometric Measurements
        try:
            ecg_ctx = assemble_consilium_ecg_context(self.db, cycle_id)
            has_ecg = bool(ecg_ctx.is_conducted)
        except ConsiliumContextMissingError:
            ecg_ctx = None
            has_ecg = False

        if has_ecg and ecg_ctx is not None:
            ecg_block: Dict[str, Any] = {
                "available": True,
                "source_attribution": "preparatory_ecg",
                "record_date": ecg_ctx.record_date or "Date not recorded",
                "recording_type": ecg_ctx.recording_type,
                "recording_quality": ecg_ctx.recording_quality,
                "lead_name": ecg_ctx.lead_name,
                "heart_rate": (
                    {"value": ecg_ctx.heart_rate_bpm, "unit": "bpm"}
                    if ecg_ctx.heart_rate_bpm is not None
                    else None
                ),
                "rhythm_type": ecg_ctx.rhythm_type,
                "rhythm_regularity": ecg_ctx.rhythm_regularity,
                "intervals_ms": {
                    k: {"value": v, "unit": "ms"}
                    for k, v in (ecg_ctx.intervals or {}).items()
                    if v is not None
                },
                "electrical_axis": ecg_ctx.electrical_axis,
                "st_segment_findings": ecg_ctx.st_segment_findings,
                "t_wave_findings": ecg_ctx.t_wave_findings,
                "ecg_summary": ecg_ctx.ecg_summary,
                "limitations": ecg_ctx.limitations,
                "uncertainties": ecg_ctx.uncertainties,
                "potential_urgent_warning": ecg_ctx.potential_urgent_warning,
            }
        else:
            ecg_block = {
                "available": False,
                "status": "NOT_RECORDED",
                "note": "NO ECG OR INSTRUMENTAL EXAMINATION RECORDED. Do not invent ECG findings or intervals.",
            }

        raw_measurements = self.measurement_repo.list_by_cycle(
            cycle_id=cycle_id,
            is_historical=False,
            is_simulated=False,
            limit=MAX_SNAPSHOT_VITALS,
        )
        admissible_vitals: List[Dict[str, Any]] = []
        for m in raw_measurements:
            if m.cycle_id != cycle_id or not m.is_medically_admissible:
                continue
            entry: Dict[str, Any] = {
                "measured_at_utc": self._format_dt_iso(m.measured_at),
                "source": m.source,
                "phase": m.phase,
            }
            if m.systolic is not None and m.diastolic is not None:
                entry["blood_pressure"] = {
                    "systolic": m.systolic,
                    "diastolic": m.diastolic,
                    "unit": "mmHg",
                    "formatted": f"{m.systolic}/{m.diastolic} mmHg",
                }
            if m.pulse is not None:
                entry["heart_rate"] = {
                    "value": m.pulse,
                    "unit": "bpm",
                    "formatted": f"{m.pulse} bpm",
                }
            if m.spo2 is not None:
                entry["spo2"] = {
                    "value": m.spo2,
                    "unit": "%",
                    "formatted": f"{m.spo2}%",
                }
            if m.note:
                entry["patient_note"] = sanitize_pii_text(m.note)
            admissible_vitals.append(entry)

        objective_vitals_block: Dict[str, Any] = (
            {
                "available": True,
                "source_attribution": "health_measurements",
                "count": len(admissible_vitals),
                "measurements": admissible_vitals,
            }
            if admissible_vitals
            else {
                "available": False,
                "status": "NOT_RECORDED",
                "note": "NO OBJECTIVE BIOMETRIC VITALS RECORDED. Do not invent blood pressure, pulse, or SpO2 values.",
            }
        )

        # 4. Notable Wellness Daily Changes
        wellness_rows = [
            r
            for r in self.report_repo.list_server_only(
                cycle_id=cycle_id,
                report_type=WELLNESS_DAILY_REPORT_TYPE,
            )
            if r.cycle_id == cycle_id
        ]
        notable_wellness: List[Dict[str, Any]] = []
        no_data_wellness_days = 0
        for wr in wellness_rows[:MAX_SNAPSHOT_WELLNESS_DAYS]:
            wc = wr.content or {}
            w_status = str(wc.get("status") or "").lower()
            if w_status == "no_data":
                no_data_wellness_days += 1
                continue
            win = wc.get("window") or wc.get("time_window") or {}
            notable_wellness.append(
                {
                    "report_id": wr.report_id,
                    "calendar_week": wc.get("calendar_week"),
                    "calendar_day": wc.get("calendar_day"),
                    "window_start_utc": win.get("start_utc") or win.get("window_start_utc"),
                    "window_end_utc": win.get("end_utc") or win.get("window_end_utc"),
                    "status": wc.get("status"),
                    "factual_biometrics_summary": wc.get("factual_summary") or wc.get("biometrics_summary"),
                    "ai_interpretation": wc.get("interpretation"),
                }
            )
        wellness_block: Dict[str, Any] = (
            {
                "available": True,
                "source_attribution": "wellness_daily_report",
                "substantive_days_count": len(notable_wellness),
                "no_data_days_count": no_data_wellness_days,
                "daily_summaries": notable_wellness,
            }
            if notable_wellness
            else {
                "available": False,
                "status": "NOT_RECORDED",
                "no_data_days_count": no_data_wellness_days,
                "note": "NO SUBSTANTIVE WELLNESS DAILY SUMMARIES RECORDED.",
            }
        )

        # 5. Mode 2 Vital Alert Episodes
        mode2_rows = [
            r
            for r in self.report_repo.list_server_only(
                cycle_id=cycle_id,
                report_type=ATHENA_MODE2_EPISODE_REPORT_TYPE,
            )
            if r.cycle_id == cycle_id
        ]
        mode2_items: List[Dict[str, Any]] = []
        for mr in mode2_rows[:MAX_SNAPSHOT_MODE2_EPISODES]:
            mc = mr.content or {}
            user_stmts = [
                {
                    "timestamp_utc": st.get("timestamp_utc"),
                    "text": sanitize_pii_text(str(st.get("text") or "")),
                    "symptoms": (
                        sanitize_pii_text(str(st.get("symptoms")))
                        if st.get("symptoms")
                        else None
                    ),
                    "is_patient_self_reported": True,
                }
                for st in (mc.get("user_statements") or [])
                if isinstance(st, dict) and st.get("text")
            ]
            raw_samples = mc.get("telemetry_samples") or mc.get("telemetry_points") or []
            telemetry_pts = [
                {
                    "timestamp_utc": tp.get("timestamp_utc") or tp.get("measured_at_utc"),
                    "pulse_bpm": tp.get("pulse"),
                    "spo2_percent": tp.get("spo2"),
                    "systolic_mmhg": tp.get("systolic"),
                    "diastolic_mmhg": tp.get("diastolic"),
                }
                for tp in raw_samples[:10]
                if isinstance(tp, dict)
            ]
            mode2_items.append(
                {
                    "episode_id": mc.get("episode_id") or mr.report_id,
                    "status": mc.get("status"),
                    "workout_context": mc.get("workout_context"),
                    "trigger_reason": mc.get("trigger_reason"),
                    "started_at_utc": (
                        mc.get("started_at_utc")
                        or mc.get("opened_at_utc")
                        or self._format_dt_iso(mr.created_at)
                    ),
                    "closed_at_utc": mc.get("closed_at_utc"),
                    "baseline_pulse_bpm": mc.get("baseline_pulse"),
                    "trigger_pulse_bpm": mc.get("trigger_pulse"),
                    "min_pulse_bpm": mc.get("min_pulse"),
                    "max_pulse_bpm": mc.get("max_pulse"),
                    "latest_spo2_percent": mc.get("latest_spo2"),
                    "telemetry_samples": telemetry_pts,
                    "patient_statements": user_stmts,
                }
            )
        mode2_block: Dict[str, Any] = (
            {
                "available": True,
                "source_attribution": "athena_mode2_episode",
                "episodes_count": len(mode2_items),
                "episodes": mode2_items,
            }
            if mode2_items
            else {
                "available": False,
                "status": "NOT_RECORDED",
                "note": "NO MODE 2 VITAL ALERT EPISODES RECORDED.",
            }
        )

        # 6. Athena Clinical Journals & Clinical Logs
        journal_rows = [
            r
            for r in self.report_repo.list_server_only(
                cycle_id=cycle_id,
                report_type=ATHENA_DAILY_JOURNAL_REPORT_TYPE,
            )
            if r.cycle_id == cycle_id
        ]
        journal_items: List[Dict[str, Any]] = []
        for jr in journal_rows[:MAX_SNAPSHOT_ATHENA_JOURNALS]:
            jc = jr.content or {}
            pat_ev = jc.get("patient_reported_evidence") or {}
            routine_q = pat_ev.get("routine_question") or {}
            raw_answers = pat_ev.get("patient_answers") or pat_ev.get("routine_answers") or []
            answers = [
                {
                    "timestamp_utc": ans.get("timestamp_utc") or ans.get("answered_at_utc"),
                    "question_text": ans.get("question_text") or routine_q.get("question_text"),
                    "patient_answer": sanitize_pii_text(
                        str(ans.get("text") or ans.get("answer_text") or "")
                    ),
                    "is_patient_self_reported": True,
                }
                for ans in raw_answers
                if isinstance(ans, dict) and (ans.get("text") or ans.get("answer_text"))
            ]
            out_of_turn = [
                {
                    "timestamp_utc": msg.get("timestamp_utc") or msg.get("sent_at_utc"),
                    "patient_message": sanitize_pii_text(
                        str(msg.get("text") or msg.get("message_text") or "")
                    ),
                    "is_patient_self_reported": True,
                }
                for msg in (pat_ev.get("out_of_turn_messages") or [])
                if isinstance(msg, dict) and (msg.get("text") or msg.get("message_text"))
            ]
            ai_assess = jc.get("athena_clinical_assessment") or {}
            exc_events = jc.get("exceptional_events") or []
            if not answers and not out_of_turn and not exc_events and not ai_assess.get("is_available"):
                continue
            journal_items.append(
                {
                    "journal_id": jc.get("journal_id") or jr.report_id,
                    "calendar_week": jc.get("calendar_week"),
                    "calendar_day": jc.get("calendar_day"),
                    "window_start_utc": jc.get("window_start_utc"),
                    "window_end_utc": jc.get("window_end_utc"),
                    "patient_routine_answers": answers,
                    "patient_out_of_turn_messages": out_of_turn,
                    "exceptional_events": exc_events,
                    "ai_curator_assessment": (
                        {
                            "status": ai_assess.get("status"),
                            "interpretation": ai_assess.get("interpretation"),
                            "observations": ai_assess.get("observations") or [],
                            "contradictions": ai_assess.get("contradictions") or [],
                            "uncertainties": ai_assess.get("uncertainties") or [],
                            "note": "AI Curator internal synthesis (not an objective measurement).",
                        }
                        if ai_assess.get("is_available")
                        else None
                    ),
                }
            )

        clinical_log_rows = [
            r
            for r in self.report_repo.list_server_only(
                cycle_id=cycle_id,
                report_type=ATHENA_CLINICAL_LOG_REPORT_TYPE,
            )
            if r.cycle_id == cycle_id
        ]
        clinical_log_items: List[Dict[str, Any]] = []
        for lr in clinical_log_rows[:MAX_SNAPSHOT_ATHENA_LOGS]:
            lc = lr.content or {}
            symptoms_list = [
                sanitize_pii_text(str(s))
                for s in (lc.get("symptoms_mentioned") or [])
                if s
            ]
            clinical_log_items.append(
                {
                    "recorded_at_utc": (
                        lc.get("timestamp")
                        or lc.get("timestamp_utc")
                        or self._format_dt_iso(lr.created_at)
                    ),
                    "sprint_id": lr.sprint_id,
                    "sprint_day": lc.get("sprint_day"),
                    "status": lc.get("status"),
                    "event_type": lc.get("event_type"),
                    "symptoms_mentioned_by_patient": symptoms_list,
                    "subjective_wellbeing_score": lc.get("subjective_wellbeing_score"),
                    "red_flags": bool(lc.get("red_flags", False)),
                    "summary_for_master": sanitize_pii_text(
                        str(lc.get("summary_for_master") or lc.get("summary") or "")
                    ),
                    "is_patient_self_reported": bool(symptoms_list),
                }
            )

        athena_journals_block: Dict[str, Any] = (
            {
                "available": True,
                "source_attribution": "athena_daily_journal / athena_clinical_log",
                "daily_journals": journal_items,
                "clinical_logs": clinical_log_items,
            }
            if (journal_items or clinical_log_items)
            else {
                "available": False,
                "status": "NOT_RECORDED",
                "note": "NO ATHENA CLINICAL JOURNALS OR PATIENT SYMPTOM LOGS RECORDED.",
            }
        )

        # 7. Prior Chief Physician Conclusions (Primary, Lab-Revision, Weekly Pro)
        primary_pro_rows = [
            r
            for r in self.report_repo.list_server_only(
                cycle_id=cycle_id,
                report_type="chief_physician_internal",
            )
            if r.cycle_id == cycle_id
        ]
        weekly_pro_rows = [
            r
            for r in self.report_repo.list_server_only(
                cycle_id=cycle_id,
                report_type=WEEKLY_CHIEF_INTERNAL_REPORT_TYPE,
            )
            if r.cycle_id == cycle_id
        ]
        prior_conclusions: List[Dict[str, Any]] = []
        for pr in reversed(primary_pro_rows[:MAX_SNAPSHOT_PRIOR_CONSILIUMS]):
            pc = pr.content or {}
            prior_conclusions.append(
                {
                    "source": f"chief_physician_internal (v{pr.version})",
                    "recorded_at_utc": self._format_dt_iso(pr.created_at),
                    "ai_hypothesis_summary": pc.get("pro_general_shelf"),
                    "note": "AI Chief Physician analytical hypothesis — requires clinician verification.",
                }
            )
        for wpr in reversed(weekly_pro_rows[:MAX_SNAPSHOT_PRIOR_CONSILIUMS]):
            wpc = wpr.content or {}
            prior_conclusions.append(
                {
                    "source": f"weekly_chief_physician_internal (sprint={wpc.get('sprint_id')}, v{wpr.version})",
                    "recorded_at_utc": self._format_dt_iso(wpr.created_at),
                    "sprint_number": wpc.get("sprint_number"),
                    "ai_weekly_strategy_synthesis": wpc.get("updated_weekly_strategy"),
                    "note": "AI Weekly Chief Physician analytical synthesis — requires clinician verification.",
                }
            )

        # 8. Observation Period Bounds & Approved Referral Decision
        anchor_dt = self.weekly_service.find_schedule_anchor_timestamp(cycle_id)
        period_start_dt = anchor_dt or cycle_created_at or now_dt

        return {
            "cycle_context": {
                "cycle_id": cycle_id,
                "patient_reference": f"Authenticated Aegis Patient (Cycle {cycle_id})",
                "observation_period_start_utc": self._format_dt_iso(period_start_dt),
                "observation_period_end_utc": self._format_dt_iso(now_dt),
            },
            "approved_referral_decision": {
                "referral_id": referral_state.get("referral_id"),
                "sprint_id": referral_state.get("sprint_id"),
                "sprint_number": referral_state.get("sprint_number"),
                "decision_date_utc": referral_state.get("decision_date_utc"),
                "urgency": referral_state.get("urgency"),
                "target_specialties": referral_state.get("target_specialties") or [],
                "clinical_reason_for_referral": referral_state.get("clinical_reason_for_referral"),
                "observed_syndromes_or_trends": referral_state.get("observed_syndromes_or_trends") or [],
                "recommended_workup_for_doctor_discussion": (
                    referral_state.get("recommended_workup_for_doctor_discussion") or []
                ),
            },
            "initial_anamnesis": initial_anamnesis,
            "laboratory_studies": laboratory_studies,
            "ecg_examination": ecg_block,
            "objective_biometric_vitals": objective_vitals_block,
            "wellness_daily_changes": wellness_block,
            "mode2_vital_alert_episodes": mode2_block,
            "athena_clinical_journals": athena_journals_block,
            "prior_chief_physician_conclusions": prior_conclusions,
        }

    async def generate_referral_document(
        self,
        *,
        cycle_id: str,
        patient_id: Optional[str] = None,
        sprint_id: Optional[str] = None,
        extra_client_overrides: Optional[Dict[str, Any]] = None,
    ) -> ReferralDocumentGenerationResult:
        """
        Executes the Stage 8A.2 Flash -> Pro referral document generation cascade:
        1. Verifies cycle existence, patient isolation, and active cycle status.
        2. Verifies active referral_lifecycle_state and performs idempotent replay if
           the target sprint's document is already persisted and verified.
        3. Runs Step 1: Gemini Flash (Clinical History Extractor).
        4. Runs Step 2: Gemini Pro (Doctor Observation Document Author).
        5. Atomically persists SERVER_ONLY raw document, CLIENT_FACING validated document,
           and updates referral_lifecycle_state to 'document_ready' in a single DB commit.
        """
        clean_cycle_id = (cycle_id or "").strip()
        cycle = self.cycle_repo.get(clean_cycle_id)
        if cycle is None:
            raise ConsiliumCycleNotFoundError(f"Medical cycle '{clean_cycle_id}' not found.")

        if patient_id is not None and cycle.patient_id != patient_id.strip():
            raise ConsiliumCycleNotFoundError(
                f"Medical cycle '{clean_cycle_id}' not found for patient '{patient_id}'."
            )

        if cycle.status != "active":
            raise ReferralCycleInactiveError(
                f"Cannot publish an active referral document for cycle '{clean_cycle_id}' "
                f"because cycle status is '{cycle.status}' (not 'active')."
            )

        effective_patient_id = cycle.patient_id or (patient_id or "").strip()
        ref_state = self.weekly_service.get_referral_lifecycle_state(clean_cycle_id)
        if not ref_state or ref_state.get("status") not in ("generating_document", "document_ready"):
            raise ReferralContextMissingError(
                f"No active referral lifecycle state found for cycle '{clean_cycle_id}'."
            )

        referral_id = str(ref_state.get("referral_id") or "")
        target_sprint_id = str(
            (sprint_id.strip() if sprint_id and sprint_id.strip() else None)
            or ref_state.get("pending_document_sprint_id")
            or ref_state.get("sprint_id")
            or ""
        )
        sprint_number = int(ref_state.get("sprint_number") or 1)
        referral_source = str(ref_state.get("referral_source") or "weekly_consilium_pro")

        # Idempotent recovery guard: if target sprint's document is already persisted & verified,
        # return immediately with ZERO Gemini calls and ZERO duplicate versions.
        if not extra_client_overrides:
            existing_doc = self._find_existing_document_for_sprint(
                cycle_id=clean_cycle_id,
                referral_id=referral_id,
                target_sprint_id=target_sprint_id,
            )
            if existing_doc is None:
                linked_doc_id = ref_state.get("document_report_id")
                generated_sprint = ref_state.get("generated_for_sprint_id")
                pending_sprint = ref_state.get("pending_document_sprint_id")
                if (
                    linked_doc_id
                    and (generated_sprint == target_sprint_id or (not pending_sprint and not sprint_id))
                ):
                    candidate = self.report_repo.get(str(linked_doc_id))
                    if (
                        candidate is not None
                        and candidate.cycle_id == clean_cycle_id
                        and candidate.privacy_level == PrivacyLevel.CLIENT_FACING.value
                    ):
                        existing_doc = candidate

            if existing_doc is not None:
                # Ensure referral_lifecycle_state is synchronized to 'document_ready' if needed
                if (
                    ref_state.get("status") != "document_ready"
                    or ref_state.get("document_report_id") != existing_doc.report_id
                    or ref_state.get("pending_document_sprint_id") == target_sprint_id
                ):
                    self._sync_lifecycle_to_existing_document(
                        cycle_id=clean_cycle_id,
                        ref_state=ref_state,
                        client_doc=existing_doc,
                        target_sprint_id=target_sprint_id,
                    )
                doc_metrics = (existing_doc.content or {}).get("metrics") or {}
                return ReferralDocumentGenerationResult(
                    cycle_id=clean_cycle_id,
                    patient_id=effective_patient_id,
                    sprint_id=target_sprint_id,
                    sprint_number=sprint_number,
                    referral_id=referral_id,
                    document_report_id=existing_doc.report_id,
                    raw_document_report_id=doc_metrics.get("raw_document_report_id"),
                    document_version=existing_doc.version,
                    status="document_ready",
                    created_at_utc=str(
                        doc_metrics.get("created_at_utc") or self._format_dt_iso(existing_doc.created_at)
                    ),
                    reused_existing=True,
                )

        # Build bounded, server-prepared clinical snapshot strictly isolated to cycle_id
        snapshot = self._assemble_bounded_clinical_snapshot(
            cycle_id=clean_cycle_id,
            patient_id=effective_patient_id,
            cycle_created_at=cycle.created_at,
            referral_state=ref_state,
        )

        with audit_scope(
            patient_id=effective_patient_id,
            cycle_id=clean_cycle_id,
            sprint_id=target_sprint_id,
            stage="referral_document",
        ):
            # STEP 1: Gemini Flash — Clinical History Extractor (NEVER Flash-Lite)
            flash_prompt = build_referral_flash_extractor_prompt(snapshot)
            flash_extraction: ReferralClinicalHistoryExtraction = await self.gateway.generate_structured(
                prompt=flash_prompt,
                response_schema=ReferralClinicalHistoryExtraction,
                model=GeminiModelRole.FLASH,
            )

            # STEP 2: Gemini Pro — Doctor Observation Document Author (NEVER Flash-Lite)
            pro_prompt = build_referral_pro_document_prompt(
                flash_extraction=flash_extraction.model_dump(),
                referral_decision=snapshot["approved_referral_decision"],
                cycle_metadata=snapshot["cycle_context"],
            )
            pro_document: ReferralDoctorDocumentOutput = await self.gateway.generate_structured(
                prompt=pro_prompt,
                response_schema=ReferralDoctorDocumentOutput,
                model=GeminiModelRole.PRO,
            )

        now_dt = self._now_utc()
        now_iso = now_dt.isoformat()
        report_date = now_dt.strftime("%Y-%m-%d")

        # Atomic Persistence: SERVER_ONLY raw + CLIENT_FACING validated + SERVER_ONLY lifecycle state
        try:
            # Ensure sprint row exists for foreign key integrity
            if target_sprint_id and self.sprint_repo.get(target_sprint_id) is None:
                self.sprint_repo.create(
                    sprint_id=target_sprint_id,
                    cycle_id=clean_cycle_id,
                    sprint_number=sprint_number,
                    status="completed",
                    commit=False,
                )

            # Fence check against concurrent Neo cycle closure during Gemini HTTP calls
            fresh_cycle = self.cycle_repo.get_fresh(clean_cycle_id)
            if fresh_cycle is None or fresh_cycle.status != "active":
                self.db.rollback()
                raise ReferralCycleInactiveError(
                    f"Cycle '{clean_cycle_id}' became inactive during referral document generation; "
                    "aborting publication to prevent stale active referral state."
                )

            raw_content: Dict[str, Any] = {
                "patient_id": effective_patient_id,
                "cycle_id": clean_cycle_id,
                "sprint_id": target_sprint_id,
                "sprint_number": sprint_number,
                "referral_id": referral_id,
                "referral_source": referral_source,
                "created_at_utc": now_iso,
                "approved_referral_decision": snapshot["approved_referral_decision"],
                "flash_clinical_history_extraction": flash_extraction.model_dump(),
                "pro_doctor_document": pro_document.model_dump(),
                "rendered_english_markdown": pro_document.render_english_markdown(),
            }
            raw_report = self.report_repo.create_next_version(
                cycle_id=clean_cycle_id,
                sprint_id=target_sprint_id or None,
                model=self.gateway.pro_model,
                report_type=REFERRAL_DOCTOR_DOCUMENT_RAW_REPORT_TYPE,
                privacy_level=PrivacyLevel.SERVER_ONLY,
                content=raw_content,
                created_at=now_dt,
                commit=False,
            )

            client_content = self.build_client_document_content(
                pro_doc=pro_document,
                patient_id=effective_patient_id,
                cycle_id=clean_cycle_id,
                sprint_id=target_sprint_id,
                sprint_number=sprint_number,
                referral_id=referral_id,
                referral_source=referral_source,
                created_at_utc=now_iso,
                report_date=report_date,
                raw_document_report_id=raw_report.report_id,
                extra_client_overrides=extra_client_overrides,
            )
            client_report = self.report_repo.create_next_version(
                cycle_id=clean_cycle_id,
                sprint_id=target_sprint_id or None,
                model=self.gateway.pro_model,
                report_type=REFERRAL_DOCTOR_DOCUMENT_REPORT_TYPE,
                privacy_level=PrivacyLevel.CLIENT_FACING,
                content=client_content,
                created_at=now_dt,
                commit=False,
            )

            # Re-read latest referral_lifecycle_state inside transaction and transition to document_ready
            latest_ref_state = self.weekly_service.get_referral_lifecycle_state(clean_cycle_id) or dict(ref_state)
            history_ids: List[str] = list(latest_ref_state.get("document_history_report_ids") or [])
            prev_doc_id = latest_ref_state.get("document_report_id")
            if prev_doc_id and prev_doc_id not in history_ids:
                history_ids.append(str(prev_doc_id))
            if client_report.report_id not in history_ids:
                history_ids.append(client_report.report_id)

            latest_pending = latest_ref_state.get("pending_document_sprint_id")
            cleared_pending = None if (latest_pending in (None, target_sprint_id)) else latest_pending

            updated_lifecycle: Dict[str, Any] = dict(latest_ref_state)
            next_status = (
                "visit_confirmed"
                if latest_ref_state.get("status") == "visit_confirmed"
                else "document_ready"
            )
            updated_lifecycle.update(
                {
                    "status": next_status,
                    "document_report_id": client_report.report_id,
                    "raw_document_report_id": raw_report.report_id,
                    "document_ready_at_utc": now_iso,
                    "generated_for_sprint_id": target_sprint_id,
                    "pending_document_sprint_id": cleared_pending,
                    "document_history_report_ids": history_ids,
                    "document_generation_state": "ready" if cleared_pending is None else "pending",
                    "last_generation_error_type": None,
                    "last_generation_failed_at_utc": None,
                    "updated_at_utc": now_iso,
                }
            )
            self.report_repo.create_next_version(
                cycle_id=clean_cycle_id,
                sprint_id=target_sprint_id or None,
                model=self.gateway.pro_model,
                report_type=REFERRAL_LIFECYCLE_STATE_REPORT_TYPE,
                privacy_level=PrivacyLevel.SERVER_ONLY,
                content=updated_lifecycle,
                created_at=now_dt,
                commit=False,
            )

            fresh_cycle = self.cycle_repo.get_fresh(clean_cycle_id)
            if fresh_cycle is None or fresh_cycle.status != "active":
                self.db.rollback()
                raise ReferralCycleInactiveError(
                    f"Cycle '{clean_cycle_id}' became inactive during referral document generation; "
                    "aborting publication to prevent stale active referral state."
                )

            self.db.commit()
        except Exception:
            self.db.rollback()
            raise

        return ReferralDocumentGenerationResult(
            cycle_id=clean_cycle_id,
            patient_id=effective_patient_id,
            sprint_id=target_sprint_id,
            sprint_number=sprint_number,
            referral_id=referral_id,
            document_report_id=client_report.report_id,
            raw_document_report_id=raw_report.report_id,
            document_version=client_report.version,
            status=next_status,
            created_at_utc=now_iso,
            reused_existing=False,
        )

    def _sync_lifecycle_to_existing_document(
        self,
        *,
        cycle_id: str,
        ref_state: Dict[str, Any],
        client_doc: ModelReport,
        target_sprint_id: str,
    ) -> None:
        """
        Idempotently heals referral_lifecycle_state if a matching verified client_doc
        already exists in storage.
        """
        now_iso = self._now_utc().isoformat()
        doc_metrics = (client_doc.content or {}).get("metrics") or {}
        history_ids: List[str] = list(ref_state.get("document_history_report_ids") or [])
        if client_doc.report_id not in history_ids:
            history_ids.append(client_doc.report_id)

        latest_state = self.weekly_service.get_referral_lifecycle_state(cycle_id) or dict(ref_state)
        updated = dict(latest_state)
        next_status = (
            "visit_confirmed"
            if latest_state.get("status") == "visit_confirmed"
            else "document_ready"
        )
        updated.update(
            {
                "status": next_status,
                "document_report_id": client_doc.report_id,
                "raw_document_report_id": doc_metrics.get("raw_document_report_id") or ref_state.get("raw_document_report_id"),
                "document_ready_at_utc": ref_state.get("document_ready_at_utc") or now_iso,
                "generated_for_sprint_id": target_sprint_id,
                "pending_document_sprint_id": None,
                "document_history_report_ids": history_ids,
                "document_generation_state": "ready",
                "last_generation_error_type": None,
                "last_generation_failed_at_utc": None,
                "updated_at_utc": now_iso,
            }
        )
        self.report_repo.create_next_version(
            cycle_id=cycle_id,
            sprint_id=target_sprint_id or client_doc.sprint_id,
            model=self.gateway.pro_model,
            report_type=REFERRAL_LIFECYCLE_STATE_REPORT_TYPE,
            privacy_level=PrivacyLevel.SERVER_ONLY,
            content=updated,
            commit=True,
        )
