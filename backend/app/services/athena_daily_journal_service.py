import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.models.health_measurement import HealthMeasurement
from app.models.model_report import ModelReport
from app.repositories.cycle_repository import MedicalCycleRepository
from app.repositories.measurement_repository import HealthMeasurementRepository
from app.repositories.report_repository import (
    ModelReportRepository,
    ReportVersionConflictError,
)
from app.repositories.sprint_repository import WeeklySprintRepository
from app.schemas.chat import (
    AthenaClinicalAssessment,
    AthenaDailyAssessmentModelOutput,
    AthenaDailyJournalRecord,
    BiometricMeasurementPoint,
    BiometricsSummary,
    ExceptionalEventEntry,
    OutOfTurnPatientMessage,
    PatientAnswerEntry,
    PatientReportedEvidence,
    RoutineQuestionJournalSection,
    WellnessEvidence,
    WellnessReportReference,
)
from app.schemas.consilium import (
    AthenaClinicalLogEntry,
)
from app.schemas.privacy import PrivacyLevel
from app.services.athena_chat_service import (
    ATHENA_CHAT_HISTORY_REPORT_TYPE,
    ATHENA_CHAT_TURN_REPORT_TYPE,
    ATHENA_ROUTINE_QUESTION_REPORT_TYPE,
    AthenaChatService,
)
from app.services.consilium_prompts import (
    build_athena_daily_assessment_prompt,
    build_athena_system_prompt,
)
from app.services.gemini_gateway import GeminiGateway, GeminiModelRole
from app.services.weekly_consilium_service import WeeklyConsiliumService

logger = logging.getLogger("aegis.athena_daily_journal")

ATHENA_DAILY_JOURNAL_REPORT_TYPE: str = "athena_daily_journal"
ATHENA_DAILY_JOURNAL_MODEL: str = "aegis-athena-daily-journal"
ATHENA_CLINICAL_LOG_REPORT_TYPE: str = "athena_clinical_log"

# Contradiction keywords
_POOR_SLEEP_KEYWORDS = {
    "didn't sleep",
    "bad sleep",
    "sleepless",
    "insomnia",
    "poor sleep",
    "woke up tired",
    "trouble sleeping",
    "couldn't sleep",
    "не спал",
    "плохо спал",
    "бессонница",
    "не мог уснуть",
    "разбит",
    "не выспался",
}

_FEELING_FINE_KEYWORDS = {
    "feeling fine",
    "feeling great",
    "felt fine",
    "felt great",
    "no palpitations",
    "no symptoms",
    "all good",
    "perfect",
    "нет жалоб",
    "чувствую себя отлично",
    "чувствую себя хорошо",
    "всё в порядке",
    "хорошо",
    "никаких жалоб",
}

_PALPITATIONS_KEYWORDS = {
    "palpitations",
    "heart pounding",
    "racing heart",
    "fluttering heart",
    "тахикардия",
    "колотилось сердце",
    "сердцебиение",
    "сильно бьется сердце",
}


class AthenaDailyJournalService:
    """
    Evidence-Preserving Athena Daily Journal Service (Stage A2.2).

    Persists one internal, structured clinical journal for each completed observation day.
    These journals are SERVER_ONLY and invisible to the patient.
    Their purpose is to supply Weekly Consilium with seven days of original clinical
    evidence AND Athena's separately identified assessment.

    Invariants:
    1. Athena and Wellness remain independent sources.
    2. Patient statements are preserved verbatim; never rewritten or normalized.
    3. Contradictions between patient statements and wellness/wearable findings are preserved.
    4. Four clearly separated sections:
       A. patient_reported_evidence
       B. wellness_evidence
       C. exceptional_events
       D. athena_clinical_assessment
    5. Daily journal creation NEVER generates routine questions or alters their status.
    6. Uses confirmed Athena schedule anchor T0 and fixed non-overlapping 24h daily windows.
    7. Reconstructs missed days from persisted historical evidence only.
    8. At the end of a completed day with substantive evidence, runs one internal non-chat
       Gemini Flash clinical assessment scenario while preserving Layer 1 evidence independently.
    """

    def __init__(
        self,
        db: Session,
        now_provider: Optional[Callable[[], datetime]] = None,
        gateway: Optional[GeminiGateway] = None,
    ):
        self.db = db
        self.now_provider = now_provider or (lambda: datetime.now(timezone.utc))
        self.gateway = gateway
        self.cycle_repo = MedicalCycleRepository(db)
        self.sprint_repo = WeeklySprintRepository(db)
        self.report_repo = ModelReportRepository(db)
        self.measurement_repo = HealthMeasurementRepository(db)
        self.weekly_service = WeeklyConsiliumService(
            db=db, now_provider=self.now_provider
        )

    def _now_utc(self) -> datetime:
        dt = self.now_provider()
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)

    @staticmethod
    def _parse_iso_utc(raw: Optional[str | datetime]) -> Optional[datetime]:
        if raw is None:
            return None
        if isinstance(raw, datetime):
            if raw.tzinfo is None:
                return raw.replace(tzinfo=timezone.utc)
            return raw.astimezone(timezone.utc)
        if not isinstance(raw, str) or not raw.strip():
            return None
        text = raw.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(text)
            if dt.tzinfo is None:
                return dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except Exception:
            return None

    # ==========================================================================
    # 1. OBSERVATION WINDOW CALCULATION
    # ==========================================================================

    def resolve_daily_window(
        self,
        cycle_id: str,
        calendar_week: int,
        calendar_day: int,
    ) -> Tuple[datetime, datetime]:
        """
        Calculates non-overlapping observation window [window_start, window_end) for
        (calendar_week, calendar_day) derived from confirmed schedule anchor T0.
        Day 1 of Week 1 includes a 10-minute backward buffer to capture the T0 initial
        greeting turn itself.
        """
        anchor_dt = self.weekly_service.find_schedule_anchor_timestamp(cycle_id)
        if anchor_dt is None:
            raise ValueError(
                f"Cannot resolve daily window for cycle '{cycle_id}': schedule anchor T0 not established."
            )
        if calendar_week < 1:
            raise ValueError(f"calendar_week must be >= 1, got {calendar_week}")
        if calendar_day < 1 or calendar_day > 7:
            raise ValueError(f"calendar_day must be between 1 and 7, got {calendar_day}")

        offset_days = (calendar_week - 1) * 7 + (calendar_day - 1)
        start_dt = anchor_dt + timedelta(days=offset_days)
        end_dt = anchor_dt + timedelta(days=offset_days + 1)

        # 10-minute backward buffer for Week 1 Day 1 to capture the anchor turn
        if calendar_week == 1 and calendar_day == 1:
            start_dt = anchor_dt - timedelta(minutes=10)

        return start_dt, end_dt

    def get_completed_daily_windows(
        self,
        cycle_id: str,
        as_of: Optional[datetime] = None,
    ) -> List[Tuple[int, int, datetime, datetime]]:
        """
        Returns all completed daily observation windows (week, day, start_dt, end_dt)
        where `end_dt <= as_of`.
        """
        anchor_dt = self.weekly_service.find_schedule_anchor_timestamp(cycle_id)
        if anchor_dt is None:
            return []

        effective_now = as_of or self._now_utc()
        if effective_now.tzinfo is None:
            effective_now = effective_now.replace(tzinfo=timezone.utc)
        else:
            effective_now = effective_now.astimezone(timezone.utc)

        if effective_now <= anchor_dt:
            return []

        hours_elapsed = (effective_now - anchor_dt).total_seconds() / 3600.0
        completed_days_count = int(hours_elapsed // 24)

        windows: List[Tuple[int, int, datetime, datetime]] = []
        for i in range(completed_days_count):
            w = (i // 7) + 1
            d = (i % 7) + 1
            start_dt, end_dt = self.resolve_daily_window(cycle_id, w, d)
            if end_dt <= effective_now:
                windows.append((w, d, start_dt, end_dt))
        return windows

    # ==========================================================================
    # 2. EVIDENCE GATHERING & SECTION BUILDERS
    # ==========================================================================

    def _gather_patient_reported_evidence(
        self,
        cycle_id: str,
        calendar_day: int,
        window_start: datetime,
        window_end: datetime,
    ) -> PatientReportedEvidence:
        turn_reports = self.report_repo.list_server_only(
            cycle_id=cycle_id,
            report_type=ATHENA_CHAT_TURN_REPORT_TYPE,
        )

        patient_answers: List[PatientAnswerEntry] = []
        out_of_turn_messages: List[OutOfTurnPatientMessage] = []

        # Filter turns falling into the observation window
        turns_in_window: List[Dict[str, Any]] = []
        for r in turn_reports:
            c = r.content or {}
            ts_dt = self._parse_iso_utc(c.get("created_at")) or self._parse_iso_utc(r.created_at)
            if ts_dt is None or ts_dt < window_start or ts_dt >= window_end:
                continue
            turns_in_window.append(c)

        # Sort chronologically
        turns_in_window.sort(
            key=lambda item: self._parse_iso_utc(item.get("created_at"))
            or datetime.min.replace(tzinfo=timezone.utc)
        )

        # Also inspect sanitized chat history for verbatim user messages
        chat_history = self.weekly_service.get_athena_chat_history(cycle_id)
        genuine_user_msgs: List[Dict[str, Any]] = []
        for msg in chat_history:
            if not isinstance(msg, dict) or msg.get("sender") != "user":
                continue
            ts_dt = self._parse_iso_utc(msg.get("timestamp"))
            if ts_dt is None or not (window_start <= ts_dt < window_end):
                continue
            raw_text = str(msg.get("text") or "").strip()
            if not raw_text:
                continue
            # Filter out synthetic system prompts/triggers
            if raw_text.startswith((
                "[Initialization]:",
                "[Daily Pulse",
                "[Reminder]:",
                "[Vitals Alert]:",
                "[Wearable Event]:",
            )):
                continue

            is_oot = False
            clean_text = raw_text
            if clean_text.startswith("[Out-of-turn]:"):
                is_oot = True
                clean_text = clean_text[len("[Out-of-turn]:"):].strip()

            genuine_user_msgs.append({
                "raw_text": raw_text,
                "text": clean_text,
                "timestamp": str(msg.get("timestamp") or ts_dt.isoformat()),
                "is_oot": is_oot,
                "used": False,
            })

        # Match turns and user messages
        for turn in turns_in_window:
            ev_type = str(turn.get("event_type") or "user_message")
            msg_id = str(turn.get("message_id") or "")
            ts_str = str(turn.get("created_at") or "")

            # If this turn was an out-of-turn message, store separately
            if ev_type == "out_of_turn_message":
                matching_msg = next(
                    (m for m in genuine_user_msgs if not m["used"] and m["is_oot"]),
                    None,
                )
                if matching_msg is None:
                    matching_msg = next(
                        (m for m in genuine_user_msgs if not m["used"]),
                        None,
                    )
                raw_text = matching_msg["text"] if matching_msg else f"[Spontaneous note at {ts_str}]"
                if matching_msg:
                    matching_msg["used"] = True
                out_of_turn_messages.append(
                    OutOfTurnPatientMessage(
                        text=raw_text,
                        timestamp_utc=ts_str,
                        source_event_id=msg_id,
                        event_type=ev_type,
                    )
                )
            elif ev_type in ("routine_answer", "user_message") or (
                ev_type == "daily_pulse" and any(not m["used"] for m in genuine_user_msgs)
            ):
                matching_msg = next(
                    (m for m in genuine_user_msgs if not m["used"] and not m["is_oot"]),
                    None,
                )
                if matching_msg is None:
                    matching_msg = next(
                        (m for m in genuine_user_msgs if not m["used"]),
                        None,
                    )
                if matching_msg is not None:
                    matching_msg["used"] = True
                    patient_answers.append(
                        PatientAnswerEntry(
                            text=matching_msg["text"],
                            timestamp_utc=str(matching_msg.get("timestamp") or ts_str),
                            source_event_id=msg_id,
                            event_type=ev_type,
                        )
                    )

        # Remaining genuine user messages in window not linked to a specific turn
        for u_msg in genuine_user_msgs:
            if u_msg["used"]:
                continue
            u_msg["used"] = True
            if u_msg["is_oot"]:
                out_of_turn_messages.append(
                    OutOfTurnPatientMessage(
                        text=u_msg["text"],
                        timestamp_utc=u_msg["timestamp"],
                        source_event_id=None,
                        event_type="out_of_turn_message",
                    )
                )
            else:
                patient_answers.append(
                    PatientAnswerEntry(
                        text=u_msg["text"],
                        timestamp_utc=u_msg["timestamp"],
                        source_event_id=None,
                        event_type="user_message",
                    )
                )

        # Determine routine question state for this day
        rq_reports = self.report_repo.list_server_only(
            cycle_id=cycle_id,
            report_type=ATHENA_ROUTINE_QUESTION_REPORT_TYPE,
        )
        active_q_content: Optional[Dict[str, Any]] = None
        for rq in rq_reports:
            c = rq.content or {}
            asked_dt = self._parse_iso_utc(c.get("asked_at_utc")) or self._parse_iso_utc(rq.created_at)
            if asked_dt is not None and asked_dt < window_end:
                active_q_content = c
                break

        routine_section = RoutineQuestionJournalSection()
        if active_q_content is not None:
            routine_section.question_id = active_q_content.get("question_id")
            routine_section.question_text = active_q_content.get("question_text")
            ans_dt = self._parse_iso_utc(active_q_content.get("answered_at_utc"))
            status = str(active_q_content.get("status") or "")

            if status == "answered" and ans_dt is not None and window_start <= ans_dt < window_end:
                routine_section.was_answered = True
                routine_section.answered_at_utc = active_q_content.get("answered_at_utc")
                routine_section.unanswered_declaration = None
            elif status == "answered" and ans_dt is not None and ans_dt < window_start:
                routine_section.was_answered = True
                routine_section.answered_at_utc = active_q_content.get("answered_at_utc")
                routine_section.unanswered_declaration = None
            elif status == "answered" and ans_dt is None:
                routine_section.was_answered = True
                routine_section.answered_at_utc = patient_answers[0].timestamp_utc if patient_answers else None
                routine_section.unanswered_declaration = None
            else:
                routine_section.was_answered = False
                routine_section.answered_at_utc = None
                routine_section.unanswered_declaration = (
                    f"Routine question '{routine_section.question_id}' was asked but remained unanswered during Day {calendar_day}."
                )
        else:
            routine_section.was_answered = False
            routine_section.unanswered_declaration = (
                f"No routine question was active or pending during Day {calendar_day}."
            )

        has_interaction = bool(patient_answers or out_of_turn_messages)
        return PatientReportedEvidence(
            routine_question=routine_section,
            patient_answers=patient_answers,
            out_of_turn_messages=out_of_turn_messages,
            has_patient_interaction=has_interaction,
        )

    def _gather_wellness_evidence(
        self,
        cycle_id: str,
        calendar_day: int,
        window_start: datetime,
        window_end: datetime,
    ) -> WellnessEvidence:
        missing_declarations: List[str] = []

        # 1. Independent Wellness Report Discovery
        # Query model_reports within the observation window for reports with "wellness" in report_type
        all_reports = self.report_repo.list_server_only(cycle_id=cycle_id)
        wellness_report_match: Optional[ModelReport] = None
        for r in all_reports:
            r_type = (r.report_type or "").lower()
            if "wellness" in r_type and r_type not in (
                ATHENA_DAILY_JOURNAL_REPORT_TYPE,
            ):
                created_dt = self._parse_iso_utc(r.created_at)
                if created_dt is not None and window_start <= created_dt < window_end:
                    wellness_report_match = r
                    break

        if wellness_report_match is not None:
            c = wellness_report_match.content or {}
            ref = WellnessReportReference(
                report_id=wellness_report_match.report_id,
                report_type=wellness_report_match.report_type,
                version=wellness_report_match.version,
                timestamp_utc=(wellness_report_match.created_at or window_start).isoformat(),
                is_available=True,
                missing_reason=None,
            )
            assessment = str(
                c.get("assessment")
                or c.get("summary")
                or c.get("wellness_assessment")
                or ""
            ).strip() or None
        else:
            ref = WellnessReportReference(
                report_id=None,
                report_type=None,
                version=None,
                timestamp_utc=None,
                is_available=False,
                missing_reason=f"No independent wellness report persisted for Day {calendar_day}.",
            )
            assessment = None
            missing_declarations.append(
                f"Independent wellness report is unavailable for Day {calendar_day}."
            )

        # 2. Genuine HealthMeasurements Discovery
        norm_start = self.measurement_repo._normalize_dt(window_start)
        norm_end = self.measurement_repo._normalize_dt(window_end)

        all_measurements = self.measurement_repo.list_by_cycle(
            cycle_id=cycle_id,
            is_historical=False,
            is_simulated=False,
            limit=2000,
        )

        records_in_window: List[HealthMeasurement] = []
        for m in all_measurements:
            m_dt = self.measurement_repo._normalize_dt(m.measured_at)
            if norm_start <= m_dt < norm_end:
                records_in_window.append(m)

        measurement_points: List[BiometricMeasurementPoint] = []
        pulse_values: List[int] = []
        sys_values: List[int] = []
        dia_values: List[int] = []
        spo2_values: List[int] = []
        sleep_records: List[Dict[str, Any]] = []
        step_records: List[Dict[str, Any]] = []

        for m in records_in_window:
            pt = BiometricMeasurementPoint(
                timestamp_utc=m.measured_at.isoformat(),
                source=m.source,
                pulse=m.pulse,
                systolic=m.systolic,
                diastolic=m.diastolic,
                spo2=m.spo2,
                raw_metadata=m.raw_metadata,
            )
            measurement_points.append(pt)

            if m.pulse is not None:
                pulse_values.append(m.pulse)
            if m.systolic is not None:
                sys_values.append(m.systolic)
            if m.diastolic is not None:
                dia_values.append(m.diastolic)
            if m.spo2 is not None:
                spo2_values.append(m.spo2)

            meta = m.raw_metadata or {}
            if "sleep_hours" in meta or "sleep" in meta:
                sleep_records.append(meta)
            if "steps" in meta or "step_count" in meta:
                step_records.append(meta)

        # Biometric summaries
        heart_rate_summary = (
            {
                "min_bpm": min(pulse_values),
                "avg_bpm": round(sum(pulse_values) / len(pulse_values), 1),
                "max_bpm": max(pulse_values),
                "sample_count": len(pulse_values),
            }
            if pulse_values
            else None
        )

        blood_pressure_summary = (
            {
                "min_systolic": min(sys_values),
                "avg_systolic": round(sum(sys_values) / len(sys_values), 1),
                "max_systolic": max(sys_values),
                "min_diastolic": min(dia_values) if dia_values else None,
                "avg_diastolic": round(sum(dia_values) / len(dia_values), 1) if dia_values else None,
                "max_diastolic": max(dia_values) if dia_values else None,
                "sample_count": len(sys_values),
            }
            if sys_values
            else None
        )

        spo2_summary = (
            {
                "min_spo2": min(spo2_values),
                "avg_spo2": round(sum(spo2_values) / len(spo2_values), 1),
                "max_spo2": max(spo2_values),
                "sample_count": len(spo2_values),
            }
            if spo2_values
            else None
        )

        sleep_summary = (
            sleep_records[-1]
            if sleep_records
            else {"status": "missing", "detail": f"No sleep sensor data recorded for Day {calendar_day}"}
        )
        if not sleep_records:
            missing_declarations.append(
                f"No confirmed sleep duration or quality recorded for Day {calendar_day}."
            )

        steps_summary = (
            step_records[-1]
            if step_records
            else {"status": "missing", "detail": f"No step count recorded for Day {calendar_day}"}
        )
        if not step_records:
            missing_declarations.append(
                f"No step count telemetry recorded for Day {calendar_day}."
            )

        if not measurement_points:
            missing_declarations.append(
                f"No wearable or manual biometric measurements recorded for Day {calendar_day}."
            )

        biometrics = BiometricsSummary(
            heart_rate_summary=heart_rate_summary,
            blood_pressure_summary=blood_pressure_summary,
            spo2_summary=spo2_summary,
            sleep_summary=sleep_summary,
            steps_summary=steps_summary,
            raw_records_count=len(measurement_points),
            records=measurement_points,
        )

        return WellnessEvidence(
            report_reference=ref,
            wellness_assessment=assessment,
            biometrics=biometrics,
            missing_data_declarations=missing_declarations,
        )

    def _gather_exceptional_events(
        self,
        cycle_id: str,
        window_start: datetime,
        window_end: datetime,
    ) -> List[ExceptionalEventEntry]:
        events: List[ExceptionalEventEntry] = []

        # 1. From Athena chat turns (wearable_event, vitals_alert, emergency triggers)
        turn_reports = self.report_repo.list_server_only(
            cycle_id=cycle_id,
            report_type=ATHENA_CHAT_TURN_REPORT_TYPE,
        )
        for r in turn_reports:
            c = r.content or {}
            ts_dt = self._parse_iso_utc(c.get("created_at")) or self._parse_iso_utc(r.created_at)
            if ts_dt is None or ts_dt < window_start or ts_dt >= window_end:
                continue

            ev_type = str(c.get("event_type") or "")
            msg_id = str(c.get("message_id") or r.report_id)
            ts_str = (ts_dt or window_start).isoformat()
            is_emerg = bool(c.get("emergency_trigger", False))

            if ev_type in ("vitals_alert", "wearable_event"):
                desc = str(c.get("emergency_notice") or c.get("reply") or f"Biometric alert ({ev_type})")
                events.append(
                    ExceptionalEventEntry(
                        event_type=ev_type,
                        event_id=msg_id,
                        timestamp_utc=ts_str,
                        source="wearable_sensor",
                        description=desc,
                        clinical_event_identity=f"{ev_type}_{msg_id}",
                        safety_tier="Mode 2 Intervention" if is_emerg else "Wearable Telemetry Alert",
                    )
                )
            elif is_emerg:
                desc = str(c.get("emergency_notice") or "Pre-emergency threshold triggered")
                events.append(
                    ExceptionalEventEntry(
                        event_type="emergency_trigger",
                        event_id=msg_id,
                        timestamp_utc=ts_str,
                        source="safety_guard",
                        description=desc,
                        clinical_event_identity=f"emerg_{msg_id}",
                        safety_tier="Mode 2 Intervention",
                    )
                )

        # 2. From clinical logs with red flags or critical status
        clinical_logs = self.report_repo.list_server_only(
            cycle_id=cycle_id,
            report_type=ATHENA_CLINICAL_LOG_REPORT_TYPE,
        )
        for r in clinical_logs:
            c = r.content or {}
            ts_dt = self._parse_iso_utc(c.get("timestamp")) or self._parse_iso_utc(r.created_at)
            if ts_dt is None or ts_dt < window_start or ts_dt >= window_end:
                continue
            red_flags = bool(c.get("red_flags", False))
            status_val = str(c.get("status") or "").lower()
            if red_flags or status_val == "critical":
                summary = str(c.get("summary_for_master") or f"Clinical status: {status_val}")
                events.append(
                    ExceptionalEventEntry(
                        event_type="clinical_red_flag",
                        event_id=r.report_id,
                        timestamp_utc=(ts_dt or window_start).isoformat(),
                        source="curator_clinical_log",
                        description=summary,
                        clinical_event_identity=f"red_flag_{r.report_id}",
                        safety_tier="Pre-emergency Alert",
                    )
                )

        events.sort(key=lambda item: item.timestamp_utc)
        return events

    def _detect_deterministic_contradictions(
        self,
        patient_evidence: PatientReportedEvidence,
        wellness_evidence: WellnessEvidence,
        exceptional_events: List[ExceptionalEventEntry],
    ) -> List[str]:
        contradictions: List[str] = []

        patient_text_combined = " ".join(
            [a.text.lower() for a in patient_evidence.patient_answers]
            + [m.text.lower() for m in patient_evidence.out_of_turn_messages]
        )

        # Case 1: Sleep contradiction
        has_poor_sleep_claim = any(kw in patient_text_combined for kw in _POOR_SLEEP_KEYWORDS)
        sleep_meta = wellness_evidence.biometrics.sleep_summary or {}
        sleep_hrs = sleep_meta.get("sleep_hours") or sleep_meta.get("duration_hours")
        if has_poor_sleep_claim and sleep_hrs is not None:
            try:
                hrs_val = float(sleep_hrs)
                if hrs_val >= 7.0:
                    contradictions.append(
                        f"Contradiction: Patient reported poor or inadequate sleep, but wearable/wellness data recorded {hrs_val} hours of sleep."
                    )
            except (ValueError, TypeError):
                pass

        # Case 2: Palpitations vs resting pulse
        has_palpitations_claim = any(kw in patient_text_combined for kw in _PALPITATIONS_KEYWORDS)
        hr_sum = wellness_evidence.biometrics.heart_rate_summary
        if has_palpitations_claim and hr_sum is not None:
            max_pulse = hr_sum.get("max_bpm", 0)
            avg_pulse = hr_sum.get("avg_bpm", 0)
            if max_pulse <= 75 and avg_pulse <= 70:
                contradictions.append(
                    f"Contradiction: Patient reported palpitations/tachycardia, but biometric monitoring recorded normal resting pulse (avg {avg_pulse} bpm, max {max_pulse} bpm)."
                )

        # Case 3: Feeling fine vs critical spike
        has_feeling_fine_claim = any(kw in patient_text_combined for kw in _FEELING_FINE_KEYWORDS)
        has_critical_spike = (
            hr_sum is not None and hr_sum.get("max_bpm", 0) >= 130
        ) or any(e.event_type in ("vitals_alert", "wearable_alert", "wearable_event") for e in exceptional_events)

        if has_feeling_fine_claim and has_critical_spike:
            contradictions.append(
                "Contradiction: Patient reported absence of symptoms / feeling well, but biometric alerts recorded tachycardia or critical vitals spike."
            )

        return contradictions

    def _get_clinical_logs_in_window(
        self,
        cycle_id: str,
        window_start: datetime,
        window_end: datetime,
    ) -> List[Tuple[str, Dict[str, Any]]]:
        clinical_logs = self.report_repo.list_server_only(
            cycle_id=cycle_id,
            report_type=ATHENA_CLINICAL_LOG_REPORT_TYPE,
        )
        logs_in_window: List[Tuple[str, Dict[str, Any]]] = []
        for r in clinical_logs:
            c = r.content or {}
            ts_dt = self._parse_iso_utc(c.get("timestamp")) or self._parse_iso_utc(r.created_at)
            if ts_dt is not None and window_start <= ts_dt < window_end:
                logs_in_window.append((r.report_id, c))
        return logs_in_window

    def _has_non_greeting_clinical_log_in_window(
        self,
        cycle_id: str,
        window_start: datetime,
        window_end: datetime,
    ) -> bool:
        logs = self._get_clinical_logs_in_window(cycle_id, window_start, window_end)
        for _, c in logs:
            if c.get("event_type") not in ("initial_greeting", "daily_pulse"):
                return True
        return False

    def has_substantive_daily_evidence(
        self,
        journal: AthenaDailyJournalRecord,
    ) -> bool:
        """
        Determines whether a completed observation day contains substantive clinical data
        requiring Athena's internal daily Gemini Flash assessment:
        - patient answers or out-of-turn messages;
        - independent Wellness report;
        - raw HealthMeasurement biometrics;
        - exceptional wearable/vitals/red-flag events;
        - or non-greeting Athena clinical logs in the window.
        """
        if journal.patient_reported_evidence.has_patient_interaction:
            return True
        if journal.wellness_evidence.report_reference.is_available:
            return True
        if journal.wellness_evidence.biometrics.raw_records_count > 0:
            return True
        if len(journal.exceptional_events) > 0:
            return True

        window_start = self._parse_iso_utc(journal.window_start_utc)
        window_end = self._parse_iso_utc(journal.window_end_utc)
        if window_start is not None and window_end is not None:
            if self._has_non_greeting_clinical_log_in_window(
                journal.cycle_id, window_start, window_end
            ):
                return True

        return False

    def _collect_source_references(
        self,
        cycle_id: str,
        journal: AthenaDailyJournalRecord,
    ) -> List[str]:
        refs: List[str] = []
        rq = journal.patient_reported_evidence.routine_question
        if rq.question_id:
            refs.append(f"routine_question:{rq.question_id}")
        for ans in journal.patient_reported_evidence.patient_answers:
            if ans.source_event_id:
                refs.append(f"patient_answer:{ans.source_event_id}")
            else:
                refs.append(f"patient_answer_ts:{ans.timestamp_utc}")
        for oot in journal.patient_reported_evidence.out_of_turn_messages:
            if oot.source_event_id:
                refs.append(f"out_of_turn:{oot.source_event_id}")
            else:
                refs.append(f"out_of_turn_ts:{oot.timestamp_utc}")
        if (
            journal.wellness_evidence.report_reference.is_available
            and journal.wellness_evidence.report_reference.report_id
        ):
            refs.append(
                f"wellness_report:{journal.wellness_evidence.report_reference.report_id}:v{journal.wellness_evidence.report_reference.version}"
            )
        for pt in journal.wellness_evidence.biometrics.records:
            refs.append(f"biometric:{pt.source}:{pt.timestamp_utc}")
        for ee in journal.exceptional_events:
            refs.append(f"exceptional_event:{ee.event_id}")

        window_start = self._parse_iso_utc(journal.window_start_utc)
        window_end = self._parse_iso_utc(journal.window_end_utc)
        if window_start is not None and window_end is not None:
            for rep_id, _ in self._get_clinical_logs_in_window(
                cycle_id, window_start, window_end
            ):
                refs.append(f"clinical_log:{rep_id}")

        return refs

    def _build_athena_clinical_assessment(
        self,
        cycle_id: str,
        calendar_day: int,
        window_start: datetime,
        window_end: datetime,
        patient_evidence: PatientReportedEvidence,
        wellness_evidence: WellnessEvidence,
        exceptional_events: List[ExceptionalEventEntry],
    ) -> AthenaClinicalAssessment:
        logs_with_ids = self._get_clinical_logs_in_window(
            cycle_id, window_start, window_end
        )
        logs_in_window = [c for _, c in logs_with_ids]

        if not logs_in_window:
            return AthenaClinicalAssessment(
                is_available=False,
                label="NO_ATHENA_ASSESSMENT_AVAILABLE",
                status=None,
                interpretation=None,
                observations=[],
                relevant_changes=[],
                contradictions=[],
                uncertainties=[
                    f"No patient interaction or curator evaluation occurred during Day {calendar_day} observation window."
                ],
                red_flags=False,
                source_references=[],
                provenance_model=None,
                absence_reason=f"No new Athena clinical model evaluation occurred for Day {calendar_day}.",
            )

        # Consolidate clinical logs in window
        latest_log = logs_in_window[-1]
        all_symptoms: List[str] = []
        any_red_flags = False
        for log in logs_in_window:
            if log.get("red_flags"):
                any_red_flags = True
            for s in log.get("symptoms_mentioned") or []:
                if s not in all_symptoms:
                    all_symptoms.append(s)

        raw_status = str(latest_log.get("status") or "NORMAL").upper()
        if raw_status not in ("NORMAL", "ALERT", "CRITICAL"):
            raw_status = "CRITICAL" if any_red_flags else "NORMAL"

        interpretation = str(latest_log.get("summary_for_master") or "").strip()
        contradictions = self._detect_deterministic_contradictions(
            patient_evidence=patient_evidence,
            wellness_evidence=wellness_evidence,
            exceptional_events=exceptional_events,
        )

        uncertainties: List[str] = []
        if wellness_evidence.missing_data_declarations:
            uncertainties.extend(wellness_evidence.missing_data_declarations)
        if not patient_evidence.has_patient_interaction:
            uncertainties.append(
                f"Subjective symptoms unconfirmed due to absence of patient response on Day {calendar_day}."
            )

        return AthenaClinicalAssessment(
            is_available=True,
            label="ATHENA_GENERATED_INTERPRETATION_NOT_RAW_EVIDENCE",
            status=raw_status,
            interpretation=interpretation or "Curator recorded stable status.",
            observations=all_symptoms,
            relevant_changes=[],
            contradictions=contradictions,
            uncertainties=uncertainties,
            red_flags=any_red_flags,
            source_references=[rep_id for rep_id, _ in logs_with_ids],
            provenance_model="athena_clinical_log_aggregation",
            absence_reason=None,
        )

    async def generate_daily_gemini_assessment(
        self,
        cycle_id: str,
        journal_record: AthenaDailyJournalRecord,
    ) -> AthenaClinicalAssessment:
        """
        Executes the INTERNAL, NON-CHAT daily clinical assessment scenario for Athena
        (Gemini Flash) using the persisted Layer 1 evidence of `journal_record`.
        Never creates a patient-facing chat turn, never generates a routine question,
        and never mutates Layer 1 source evidence.
        """
        if self.gateway is None:
            raise RuntimeError(
                "GeminiGateway is required to generate Athena daily Gemini assessment."
            )

        # Load approved Athena system prompt with current Chief Physician strategy
        athena_chat = AthenaChatService(
            db=self.db,
            gateway=self.gateway,
            now_provider=self.now_provider,
        )
        system_prompt, _, _ = athena_chat._resolve_strategy_context(cycle_id)

        window_start = self._parse_iso_utc(journal_record.window_start_utc)
        window_end = self._parse_iso_utc(journal_record.window_end_utc)
        logs_in_window: List[Dict[str, Any]] = []
        if window_start is not None and window_end is not None:
            logs_in_window = [
                {"report_id": rep_id, **content}
                for rep_id, content in self._get_clinical_logs_in_window(
                    cycle_id, window_start, window_end
                )
            ]

        daily_evidence_payload: Dict[str, Any] = {
            "cycle_id": journal_record.cycle_id,
            "calendar_week": journal_record.calendar_week,
            "calendar_day": journal_record.calendar_day,
            "window_start_utc": journal_record.window_start_utc,
            "window_end_utc": journal_record.window_end_utc,
            "patient_reported_evidence": journal_record.patient_reported_evidence.model_dump(
                mode="json"
            ),
            "wellness_evidence": journal_record.wellness_evidence.model_dump(
                mode="json"
            ),
            "exceptional_events": [
                e.model_dump(mode="json") for e in journal_record.exceptional_events
            ],
            "existing_athena_clinical_logs": logs_in_window,
            "missing_data_declarations": list(
                journal_record.wellness_evidence.missing_data_declarations
            ),
        }

        prompt = build_athena_daily_assessment_prompt(daily_evidence_payload)

        model_out: AthenaDailyAssessmentModelOutput = (
            await self.gateway.generate_structured(
                prompt=prompt,
                response_schema=AthenaDailyAssessmentModelOutput,
                model=GeminiModelRole.FLASH,
                temperature=0.2,
                system_instruction=system_prompt,
            )
        )

        # Merge deterministic contradiction guards with Gemini's detected contradictions
        det_contradictions = self._detect_deterministic_contradictions(
            patient_evidence=journal_record.patient_reported_evidence,
            wellness_evidence=journal_record.wellness_evidence,
            exceptional_events=journal_record.exceptional_events,
        )
        merged_contradictions: List[str] = list(model_out.contradictions or [])
        for dc in det_contradictions:
            if dc not in merged_contradictions:
                merged_contradictions.append(dc)

        merged_uncertainties: List[str] = list(model_out.uncertainties or [])
        for decl in journal_record.wellness_evidence.missing_data_declarations:
            if decl not in merged_uncertainties:
                merged_uncertainties.append(decl)

        source_refs = self._collect_source_references(cycle_id, journal_record)

        return AthenaClinicalAssessment(
            is_available=True,
            label="ATHENA_GENERATED_INTERPRETATION_NOT_RAW_EVIDENCE",
            status=model_out.status,
            interpretation=model_out.interpretation,
            observations=list(model_out.observations or []),
            relevant_changes=list(model_out.relevant_changes or []),
            contradictions=merged_contradictions,
            uncertainties=merged_uncertainties,
            red_flags=bool(model_out.red_flags),
            source_references=source_refs,
            provenance_model=GeminiModelRole.FLASH.value,
            absence_reason=None,
        )

    # ==========================================================================
    # 3. DAILY JOURNAL BUILD, CLOSURE & PERSISTENCE
    # ==========================================================================

    def build_daily_journal(
        self,
        cycle_id: str,
        calendar_week: int,
        calendar_day: int,
        as_of: Optional[datetime] = None,
    ) -> AthenaDailyJournalRecord:
        """
        Builds the 4-section structured daily journal for one completed observation day.
        Deterministic aggregation only; ZERO real Gemini calls.
        """
        cycle = self.cycle_repo.get(cycle_id)
        if cycle is None:
            raise ValueError(f"Medical cycle '{cycle_id}' not found.")
        if not cycle.patient_id:
            raise ValueError(f"Medical cycle '{cycle_id}' has no associated patient_id.")

        window_start, window_end = self.resolve_daily_window(
            cycle_id, calendar_week, calendar_day
        )
        effective_now = as_of or self._now_utc()
        closure_ts = effective_now.isoformat()

        # Section A: Patient-Reported Evidence
        patient_evidence = self._gather_patient_reported_evidence(
            cycle_id=cycle_id,
            calendar_day=calendar_day,
            window_start=window_start,
            window_end=window_end,
        )

        # Section B: Wellness Evidence
        wellness_evidence = self._gather_wellness_evidence(
            cycle_id=cycle_id,
            calendar_day=calendar_day,
            window_start=window_start,
            window_end=window_end,
        )

        # Section C: Exceptional Events
        exceptional_events = self._gather_exceptional_events(
            cycle_id=cycle_id,
            window_start=window_start,
            window_end=window_end,
        )

        # Section D: Athena Clinical Assessment
        athena_assessment = self._build_athena_clinical_assessment(
            cycle_id=cycle_id,
            calendar_day=calendar_day,
            window_start=window_start,
            window_end=window_end,
            patient_evidence=patient_evidence,
            wellness_evidence=wellness_evidence,
            exceptional_events=exceptional_events,
        )

        journal_id = f"journal_{cycle_id}_w{calendar_week}_d{calendar_day}"
        return AthenaDailyJournalRecord(
            journal_id=journal_id,
            cycle_id=cycle_id,
            patient_id=cycle.patient_id,
            calendar_week=calendar_week,
            calendar_day=calendar_day,
            window_start_utc=window_start.isoformat(),
            window_end_utc=window_end.isoformat(),
            closure_timestamp_utc=closure_ts,
            closure_type="deterministic_utc_fallback",
            patient_reported_evidence=patient_evidence,
            wellness_evidence=wellness_evidence,
            exceptional_events=exceptional_events,
            athena_clinical_assessment=athena_assessment,
        )

    async def close_daily_journal(
        self,
        cycle_id: str,
        calendar_week: int,
        calendar_day: int,
        as_of: Optional[datetime] = None,
    ) -> Tuple[AthenaDailyJournalRecord, ModelReport, bool]:
        """
        End-of-day closure orchestration for a completed observation day:
        1. Persists Layer 1 source evidence independently of AI assessment.
        2. If substantive daily evidence exists, runs one internal non-chat Gemini Flash
           clinical assessment and updates ONLY Section D (athena_clinical_assessment).
        3. If no substantive evidence exists, persists a missing-data journal with
           ZERO Gemini calls.
        4. If Gemini fails transiently, preserves Layer 1 evidence in SQLite, records
           explicit assessment-unavailable status, and re-raises for durable worker retry.
        """
        report_id = f"rep_journal_{cycle_id}_w{calendar_week}_d{calendar_day}"[:64]
        existing_row = (
            self.db.query(ModelReport)
            .filter(ModelReport.report_id == report_id)
            .populate_existing()
            .first()
        )
        existing_journal = self.get_daily_journal(cycle_id, calendar_week, calendar_day)

        if existing_journal is not None and existing_row is not None:
            # Already completed Gemini Flash assessment -> idempotent reuse
            if (
                existing_journal.athena_clinical_assessment.is_available
                and existing_journal.athena_clinical_assessment.provenance_model
                == GeminiModelRole.FLASH.value
            ):
                return existing_journal, existing_row, True

            # Empty day already persisted -> idempotent reuse with 0 Gemini calls
            if not self.has_substantive_daily_evidence(existing_journal):
                return existing_journal, existing_row, True

            journal = existing_journal
            saved_row = existing_row
        else:
            journal = self.build_daily_journal(
                cycle_id=cycle_id,
                calendar_week=calendar_week,
                calendar_day=calendar_day,
                as_of=as_of,
            )
            substantive = self.has_substantive_daily_evidence(journal)
            if not substantive:
                journal.athena_clinical_assessment = AthenaClinicalAssessment(
                    is_available=False,
                    label="NO_ATHENA_ASSESSMENT_AVAILABLE",
                    status=None,
                    interpretation=None,
                    observations=[],
                    relevant_changes=[],
                    contradictions=[],
                    uncertainties=list(
                        journal.wellness_evidence.missing_data_declarations
                    )
                    + [
                        f"No substantive patient, biometric, or wellness evidence recorded during Day {calendar_day}."
                    ],
                    red_flags=False,
                    source_references=[],
                    provenance_model=None,
                    absence_reason=f"No substantive clinical data available on Day {calendar_day}; AI assessment skipped.",
                )
                saved_row = self.save_daily_journal(cycle_id, journal, commit=True)
                return journal, saved_row, False

            # Substantive evidence exists: persist Layer 1 evidence FIRST before calling Gemini
            journal.athena_clinical_assessment = AthenaClinicalAssessment(
                is_available=False,
                label="NO_ATHENA_ASSESSMENT_AVAILABLE",
                status=None,
                interpretation=None,
                observations=[],
                relevant_changes=[],
                contradictions=self._detect_deterministic_contradictions(
                    patient_evidence=journal.patient_reported_evidence,
                    wellness_evidence=journal.wellness_evidence,
                    exceptional_events=journal.exceptional_events,
                ),
                uncertainties=list(journal.wellness_evidence.missing_data_declarations),
                red_flags=False,
                source_references=self._collect_source_references(cycle_id, journal),
                provenance_model=None,
                absence_reason="Athena daily Gemini clinical assessment pending execution.",
            )
            saved_row = self.save_daily_journal(cycle_id, journal, commit=True)

        # Execute internal Gemini Flash daily clinical assessment
        try:
            gemini_assessment = await self.generate_daily_gemini_assessment(
                cycle_id=cycle_id,
                journal_record=journal,
            )
            journal.athena_clinical_assessment = gemini_assessment
            saved_row = self.save_daily_journal(
                cycle_id,
                journal,
                commit=True,
                update_assessment_only=True,
            )
            return journal, saved_row, False
        except Exception as exc:
            journal.athena_clinical_assessment = AthenaClinicalAssessment(
                is_available=False,
                label="NO_ATHENA_ASSESSMENT_AVAILABLE",
                status=None,
                interpretation=None,
                observations=[],
                relevant_changes=[],
                contradictions=self._detect_deterministic_contradictions(
                    patient_evidence=journal.patient_reported_evidence,
                    wellness_evidence=journal.wellness_evidence,
                    exceptional_events=journal.exceptional_events,
                ),
                uncertainties=list(journal.wellness_evidence.missing_data_declarations),
                red_flags=False,
                source_references=self._collect_source_references(cycle_id, journal),
                provenance_model=None,
                absence_reason=f"Athena daily Gemini clinical assessment unavailable due to AI error: {str(exc)[:200]}",
            )
            self.save_daily_journal(
                cycle_id,
                journal,
                commit=True,
                update_assessment_only=True,
            )
            raise

    def save_daily_journal(
        self,
        cycle_id: str,
        journal_record: AthenaDailyJournalRecord,
        commit: bool = True,
        update_assessment_only: bool = False,
    ) -> ModelReport:
        """
        Persists daily journal as a SERVER_ONLY report in SQLite.
        When `update_assessment_only=True`, updates ONLY `athena_clinical_assessment`
        on the existing row while keeping Layer 1 evidence strictly immutable.
        """
        cal_week = journal_record.calendar_week
        cal_day = journal_record.calendar_day
        report_id = f"rep_journal_{cycle_id}_w{cal_week}_d{cal_day}"[:64]
        sprint_id = f"sprint_{cycle_id}_w{cal_week}"[:64]

        now_dt = self._now_utc()
        content_dict = journal_record.model_dump(mode="json")

        existing_row = (
            self.db.query(ModelReport)
            .filter(ModelReport.report_id == report_id)
            .populate_existing()
            .first()
        )
        if existing_row is not None:
            if update_assessment_only:
                persisted_content = dict(existing_row.content or {})
                persisted_content["athena_clinical_assessment"] = content_dict[
                    "athena_clinical_assessment"
                ]
                existing_row.content = persisted_content
                if commit:
                    self.db.commit()
                    self.db.refresh(existing_row)
            return existing_row

        # Ensure sprint row exists for foreign key constraint
        existing_sprint = self.sprint_repo.get(sprint_id)
        if existing_sprint is None:
            self.sprint_repo.create(
                sprint_id=sprint_id,
                cycle_id=cycle_id,
                sprint_number=cal_week,
                status="active",
                commit=commit,
            )

        day_report_type = f"{ATHENA_DAILY_JOURNAL_REPORT_TYPE}_w{cal_week}_d{cal_day}"
        row = ModelReport(
            report_id=report_id,
            cycle_id=cycle_id,
            sprint_id=sprint_id,
            session_id=None,
            model=ATHENA_DAILY_JOURNAL_MODEL,
            report_type=day_report_type,
            privacy_level=PrivacyLevel.SERVER_ONLY.value,
            version=1,
            content=content_dict,
            created_at=now_dt,
        )
        try:
            self.db.add(row)
            if commit:
                self.db.commit()
                self.db.refresh(row)
            return row
        except Exception:
            if commit:
                self.db.rollback()
            fresh = (
                self.db.query(ModelReport)
                .filter(ModelReport.report_id == report_id)
                .populate_existing()
                .first()
            )
            if fresh is not None:
                return fresh
            raise

    def get_daily_journal(
        self,
        cycle_id: str,
        calendar_week: int,
        calendar_day: int,
    ) -> Optional[AthenaDailyJournalRecord]:
        """
        Fetches persisted daily journal for (calendar_week, calendar_day) from SQLite.
        """
        report_id = f"rep_journal_{cycle_id}_w{calendar_week}_d{calendar_day}"[:64]
        row = (
            self.db.query(ModelReport)
            .filter(ModelReport.report_id == report_id)
            .populate_existing()
            .first()
        )
        if row is None or not row.content:
            return None
        try:
            return AthenaDailyJournalRecord.model_validate(row.content)
        except ValidationError:
            return None

    def get_seven_day_daily_journals(
        self,
        cycle_id: str,
        sprint_number: int,
    ) -> List[Tuple[int, Optional[AthenaDailyJournalRecord]]]:
        """
        Returns list of 7 tuples: (day_number 1..7, journal_record or None)
        for the specified sprint_number.
        """
        result: List[Tuple[int, Optional[AthenaDailyJournalRecord]]] = []
        for day in range(1, 8):
            journal = self.get_daily_journal(cycle_id, sprint_number, day)
            result.append((day, journal))
        return result

    def list_daily_journals(
        self,
        cycle_id: str,
    ) -> List[AthenaDailyJournalRecord]:
        """
        Returns all persisted daily journals for the medical cycle, ordered by creation time.
        """
        rows = (
            self.db.query(ModelReport)
            .filter(
                ModelReport.cycle_id == cycle_id,
                ModelReport.report_type.like(f"{ATHENA_DAILY_JOURNAL_REPORT_TYPE}%"),
                ModelReport.privacy_level == PrivacyLevel.SERVER_ONLY.value,
            )
            .order_by(ModelReport.created_at.asc())
            .all()
        )
        records: List[AthenaDailyJournalRecord] = []
        for r in rows:
            if r.content:
                try:
                    records.append(AthenaDailyJournalRecord.model_validate(r.content))
                except Exception:
                    pass
        return records

    # ==========================================================================
    # 4. WEEKLY CONSILIUM TWO-LAYER FORMATTER
    # ==========================================================================

    def format_seven_day_journals_for_consilium(
        self,
        cycle_id: str,
        sprint_number: int,
    ) -> Optional[str]:
        """
        Formats the 7-day diary for Weekly Consilium with both Layer 1 and Layer 2:
        LAYER 1: Original structured evidence (verbatim statements, wellness findings,
                 exceptional events, missing declarations).
        LAYER 2: Athena's separately labeled clinical assessment.

        Returns None if NO daily journals exist for sprint_number, allowing the caller
        to fall back to legacy format for backwards compatibility with pre-A2.2 tests.
        """
        journals_list = self.get_seven_day_daily_journals(cycle_id, sprint_number)
        has_any_journal = any(j is not None for _, j in journals_list)
        if not has_any_journal:
            return None

        lines: List[str] = [
            f"=== SEVEN-DAY ATHENA CLINICAL JOURNALS (WEEK {sprint_number}, DAYS 1-7) ===",
            "NOTE FOR DEPUTY & CHIEF PHYSICIAN: Evidence (Layer 1) and Athena Interpretation (Layer 2) are separated.",
            "Do not turn Athena clinical hypotheses into raw physical facts.",
            "--------------------------------------------------",
        ]

        for day_num, journal in journals_list:
            lines.append(f"--- DAY {day_num} ---")
            if journal is None:
                lines.append(f"[MISSING DAY DECLARATION]: Daily journal not recorded for Day {day_num} (observation incomplete or missing).")
                lines.append("--------------------------------------------------")
                continue

            # Layer 1: Original Evidence
            lines.append("[LAYER 1: ORIGINAL EVIDENCE]")

            # A. Patient Statements
            pe = journal.patient_reported_evidence
            if pe.patient_answers:
                lines.append("  • Patient answers (verbatim):")
                for ans in pe.patient_answers:
                    lines.append(f"    - \"{ans.text}\" (timestamp: {ans.timestamp_utc}, event: {ans.event_type})")
            else:
                lines.append("  • Patient answers: None recorded during this day.")

            rq = pe.routine_question
            if rq.question_id:
                status_str = f"Answered at {rq.answered_at_utc}" if rq.was_answered else f"Unanswered ({rq.unanswered_declaration})"
                lines.append(f"  • Routine question [{rq.question_id}]: {status_str}")

            if pe.out_of_turn_messages:
                lines.append("  • Out-of-turn patient messages (verbatim, separate from routine):")
                for oot in pe.out_of_turn_messages:
                    lines.append(f"    - \"{oot.text}\" (timestamp: {oot.timestamp_utc})")

            # B. Wellness & Measurements
            we = journal.wellness_evidence
            lines.append("  • Wellness & Biometric Evidence:")
            if we.report_reference.is_available:
                lines.append(f"    - Independent report reference: {we.report_reference.report_id} (type: {we.report_reference.report_type}, v{we.report_reference.version})")
                if we.wellness_assessment:
                    lines.append(f"    - Wellness assessment: {we.wellness_assessment}")
            else:
                lines.append(f"    - Independent report: Unavailable ({we.report_reference.missing_reason})")

            bio = we.biometrics
            if bio.heart_rate_summary:
                hr = bio.heart_rate_summary
                lines.append(f"    - Heart rate: min {hr.get('min_bpm')}, avg {hr.get('avg_bpm')}, max {hr.get('max_bpm')} bpm (samples: {hr.get('sample_count')})")
            if bio.blood_pressure_summary:
                bp = bio.blood_pressure_summary
                lines.append(f"    - Blood pressure: {bp.get('avg_systolic')}/{bp.get('avg_diastolic')} mmHg (samples: {bp.get('sample_count')})")
            if bio.spo2_summary:
                spo2 = bio.spo2_summary
                lines.append(f"    - SpO2: avg {spo2.get('avg_spo2')}% (min {spo2.get('min_spo2')}%)")

            if we.missing_data_declarations:
                lines.append("    - Missing data declarations:")
                for decl in we.missing_data_declarations:
                    lines.append(f"      * {decl}")

            # C. Exceptional Events
            if journal.exceptional_events:
                lines.append("  • Exceptional clinical events:")
                for ee in journal.exceptional_events:
                    lines.append(f"    - [{ee.event_type} | {ee.safety_tier or 'Standard'}] {ee.description} (source: {ee.source}, timestamp: {ee.timestamp_utc})")
            else:
                lines.append("  • Exceptional clinical events: None recorded.")

            # Layer 2: Athena Clinical Assessment
            lines.append("")
            lines.append("[LAYER 2: ATHENA CLINICAL ASSESSMENT]")
            aa = journal.athena_clinical_assessment
            lines.append(f"  • Label: {aa.label}")
            if aa.is_available:
                if aa.status:
                    lines.append(f"  • Status: {aa.status} (Red flags: {aa.red_flags})")
                lines.append(f"  • Interpretation: {aa.interpretation}")
                if aa.observations:
                    lines.append(f"  • Symptoms / observations: {', '.join(aa.observations)}")
                if aa.relevant_changes:
                    lines.append(f"  • Relevant changes: {', '.join(aa.relevant_changes)}")
                if aa.contradictions:
                    lines.append("  • Detected contradictions with physical evidence:")
                    for c_text in aa.contradictions:
                        lines.append(f"    * {c_text}")
                if aa.uncertainties:
                    lines.append(f"  • Stated uncertainties: {'; '.join(aa.uncertainties)}")
                if aa.source_references:
                    lines.append(f"  • Source references: {', '.join(aa.source_references)}")
            else:
                lines.append("  • Status: ASSESSMENT_UNAVAILABLE")
                lines.append(f"  • Assessment: None ({aa.absence_reason})")

            lines.append("--------------------------------------------------")

        return "\n".join(lines)
