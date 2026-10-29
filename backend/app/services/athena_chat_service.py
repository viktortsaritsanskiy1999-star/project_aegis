from datetime import datetime, timezone
import json
import logging
import re
from typing import Any, Dict, List, Optional, Tuple
import uuid
from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.models.model_report import ModelReport
from app.repositories.cycle_repository import MedicalCycleRepository
from app.repositories.report_repository import ModelReportRepository
from app.schemas.chat import (
    AthenaChatRequest,
    AthenaChatResponse,
    AthenaChatMessageItem,
    AthenaChatHistoryResponse,
    AthenaVitalsAlertContext,
)
from app.schemas.consilium import (
    AegisAgentRole,
    AthenaClinicalLogEntry,
)
from app.schemas.privacy import PrivacyLevel
from app.services.consilium_prompts import build_athena_system_prompt
from app.services.gemini_gateway import GeminiGateway, GeminiModelRole
from app.services.primary_consilium_service import (
    ConsiliumContextMissingError,
    PREPARATORY_ANAMNESIS_REPORT_TYPE,
    RAW_USER_ANAMNESIS_REPORT_TYPE,
)
from app.services.weekly_consilium_service import (
    WeeklyConsiliumService,
    ATHENA_CHAT_HISTORY_REPORT_TYPE,
)

logger = logging.getLogger("aegis.athena_chat")

ATHENA_CHAT_TURN_REPORT_TYPE = "athena_chat_turn"

# Regex for stripping [CLINICAL_REPORT] JSON block
CLINICAL_REPORT_REGEX = re.compile(r"\[CLINICAL_REPORT\]([\s\S]*?)\[/CLINICAL_REPORT\]", re.IGNORECASE)
EMERGENCY_TRIGGER_REGEX = re.compile(r"\[EMERGENCY_TRIGGER\]", re.IGNORECASE)

# Keywords indicating acute emergency triggers in user messages
ACUTE_EMERGENCY_KEYWORDS = (
    "боль в груди",
    "давит в груди",
    "жжение в груди",
    "задыхаюсь",
    "не могу дышать",
    "нехватка воздуха",
    "потерял сознание",
    "падаю в обморок",
    "сильное кровотечение",
    "вызовите скорую",
    "нужна скорая",
    "chest pain",
    "cannot breathe",
    "loss of consciousness",
    "fainting",
    "call ambulance",
)


class AthenaChatService:
    """
    Server-side AI Gateway service managing conversational interactions with Athena (CuratorAgent).
    Invariants:
    1. Server strictly resolves `patient_id` from verified DeviceSession and active MedicalCycle.
       Client cannot forge or override patient or cycle identifiers.
    2. Re-uses the clinical rules, legal restrictions, and prompt templates of CuratorAgent.
    3. Strictly partitions dialogue history from hidden [CLINICAL_REPORT] entries.
    4. Never echoes internal system prompts, clinical scores, or server_only reports to the client.
    5. Evaluates EMERGENCY_TRIGGER as a structured signal, with fail-safe protocol independent
       of Gemini API uptime.
    6. Guarantees idempotent message submission with zero duplicate AI calls or messages on retry.
    """

    def __init__(self, db: Session, gateway: Optional[GeminiGateway] = None):
        self.db = db
        self.gateway = gateway or GeminiGateway()
        self.cycle_repo = MedicalCycleRepository(db)
        self.report_repo = ModelReportRepository(db)
        self.weekly_service = WeeklyConsiliumService(db, gateway=self.gateway)

    async def handle_turn(
        self,
        *,
        patient_id: str,
        request: AthenaChatRequest,
        cycle_id: Optional[str] = None,
    ) -> AthenaChatResponse:
        clean_patient_id = patient_id.strip()

        # 1. Resolve patient's active medical cycle atomically
        if cycle_id is not None and cycle_id.strip():
            clean_cycle_id = cycle_id.strip()
            cycle = self.cycle_repo.get(clean_cycle_id)
            if cycle is None or cycle.patient_id != clean_patient_id:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Medical cycle '{clean_cycle_id}' not found for active patient.",
                )
        else:
            cycle, _ = self.cycle_repo.get_or_create_active_for_patient(clean_patient_id)
        cycle_id = cycle.cycle_id

        # 2. Idempotency Check: if client submitted an idempotency_key, check for existing turn
        if request.idempotency_key and request.idempotency_key.strip():
            clean_idem = request.idempotency_key.strip()
            existing_turn = self._find_idempotent_turn(cycle_id, clean_idem)
            if existing_turn is not None:
                return existing_turn

        # 3. Rule-based pre-emergency evaluation (vital signs thresholds & acute keywords)
        pre_emergency, pre_emergency_notice = self._evaluate_pre_emergency(request)

        # 4. Strategy & System Instruction Resolution
        system_prompt, chat_history, sprint_id = self._resolve_strategy_context(cycle_id)

        # 5. Formulate Prompt & Clean Record Text based on event_type
        prompt_text, user_record_text = self._build_turn_prompt_and_record(request)

        # 6. Build Multi-Turn Conversation Contents for Gemini
        contents = self._build_gemini_contents(chat_history, prompt_text)

        # 7. Execute AI Generation with Emergency Fail-Safe Fallback
        raw_response, used_emergency_fallback = await self._execute_generation_with_fallback(
            contents=contents,
            system_instruction=system_prompt,
            pre_emergency=pre_emergency,
        )

        # 8. Post-Processing: Extract Structured Signals & Sanitize Reply
        clean_reply, emergency_triggered, emergency_notice = self._process_and_persist_reports(
            cycle_id=cycle_id,
            sprint_id=sprint_id,
            raw_response=raw_response,
            pre_emergency=pre_emergency,
            pre_emergency_notice=pre_emergency_notice,
            used_emergency_fallback=used_emergency_fallback,
        )

        # 9. Persist Dialogue Turn to Server Chat History
        self.weekly_service.save_athena_chat_message(
            cycle_id=cycle_id,
            sender="user",
            text=user_record_text,
            sprint_id=sprint_id,
        )
        self.weekly_service.save_athena_chat_message(
            cycle_id=cycle_id,
            sender="curator",
            text=clean_reply,
            sprint_id=sprint_id,
        )

        # 10. Persist Idempotency Turn Record
        message_id = f"msg_{uuid.uuid4().hex[:16]}"
        now_iso = datetime.now(timezone.utc).isoformat()
        if request.idempotency_key and request.idempotency_key.strip():
            self._save_idempotency_turn(
                cycle_id=cycle_id,
                sprint_id=sprint_id,
                idempotency_key=request.idempotency_key.strip(),
                message_id=message_id,
                reply=clean_reply,
                emergency_trigger=emergency_triggered,
                emergency_notice=emergency_notice,
                created_at=now_iso,
                sprint_day=request.sprint_day,
            )

        return AthenaChatResponse(
            reply=clean_reply,
            emergency_trigger=emergency_triggered,
            emergency_notice=emergency_notice,
            cycle_id=cycle_id,
            sprint_day=request.sprint_day,
            message_id=message_id,
            created_at=now_iso,
        )

    def get_sanitized_history(
        self,
        *,
        patient_id: str,
        cycle_id: Optional[str] = None,
    ) -> AthenaChatHistoryResponse:
        """
        Retrieves sanitized chat history for the patient's active cycle.
        Contains strictly user and curator messages, never exposing internal clinical logs.
        """
        clean_patient_id = patient_id.strip()
        if cycle_id is not None and cycle_id.strip():
            clean_cycle_id = cycle_id.strip()
            cycle = self.cycle_repo.get(clean_cycle_id)
            if cycle is None or cycle.patient_id != clean_patient_id:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Medical cycle '{clean_cycle_id}' not found for active patient.",
                )
        else:
            cycle, _ = self.cycle_repo.get_or_create_active_for_patient(clean_patient_id)
        raw_messages = self.weekly_service.get_athena_chat_history(cycle.cycle_id)

        sanitized_items = [
            AthenaChatMessageItem(
                sender=str(m.get("sender", "curator")),
                text=str(m.get("text", "")),
                timestamp=str(m.get("timestamp", "")),
            )
            for m in raw_messages
            if isinstance(m, dict) and str(m.get("text", "")).strip()
        ]
        return AthenaChatHistoryResponse(
            cycle_id=cycle.cycle_id,
            messages=sanitized_items,
        )

    # ==========================================================================
    # INTERNAL HELPERS
    # ==========================================================================

    def _find_idempotent_turn(self, cycle_id: str, idempotency_key: str) -> Optional[AthenaChatResponse]:
        reports = self.report_repo.list_server_only(
            cycle_id=cycle_id,
            report_type=ATHENA_CHAT_TURN_REPORT_TYPE,
        )
        for r in reports:
            content = r.content or {}
            if content.get("idempotency_key") == idempotency_key:
                return AthenaChatResponse(
                    reply=str(content.get("reply", "")),
                    emergency_trigger=bool(content.get("emergency_trigger", False)),
                    emergency_notice=content.get("emergency_notice"),
                    cycle_id=cycle_id,
                    sprint_day=content.get("sprint_day"),
                    message_id=str(content.get("message_id", r.report_id)),
                    created_at=str(content.get("created_at", r.created_at.isoformat())),
                )
        return None

    def _save_idempotency_turn(
        self,
        *,
        cycle_id: str,
        sprint_id: Optional[str],
        idempotency_key: str,
        message_id: str,
        reply: str,
        emergency_trigger: bool,
        emergency_notice: Optional[str],
        created_at: str,
        sprint_day: Optional[int],
    ) -> ModelReport:
        return self.report_repo.create_next_version(
            cycle_id=cycle_id,
            sprint_id=sprint_id,
            model=self.gateway.flash_model,
            report_type=ATHENA_CHAT_TURN_REPORT_TYPE,
            privacy_level=PrivacyLevel.SERVER_ONLY,
            content={
                "agent_role": AegisAgentRole.ATHENA_CURATOR.value,
                "idempotency_key": idempotency_key,
                "message_id": message_id,
                "reply": reply,
                "emergency_trigger": emergency_trigger,
                "emergency_notice": emergency_notice,
                "created_at": created_at,
                "sprint_day": sprint_day,
            },
        )

    @staticmethod
    def _evaluate_pre_emergency(request: AthenaChatRequest) -> Tuple[bool, Optional[str]]:
        """
        Deterministic, rule-based emergency detection before calling Gemini:
        - Critical pulse spike (>= 150 bpm or <= 40 bpm at rest)
        - Explicit acute medical symptoms in message text
        """
        if request.event_type == "vitals_alert" and request.vitals_alert is not None:
            va = request.vitals_alert
            # Heart rate spike >= 150 bpm or severe bradycardia <= 40 bpm
            if va.current_pulse >= 150 or va.current_pulse <= 40:
                return True, f"CRITICAL_VITALS_ALERT: Heart rate {va.current_pulse} bpm exceeds critical safety limits."
            if va.notes:
                lower_notes = va.notes.lower()
                for kw in ACUTE_EMERGENCY_KEYWORDS:
                    if kw in lower_notes:
                        return True, f"CRITICAL_SYMPTOM_ALERT: Detected acute indicator '{kw}' in vitals alert context."

        if request.message:
            lower_msg = request.message.lower()
            for kw in ACUTE_EMERGENCY_KEYWORDS:
                if kw in lower_msg:
                    return True, f"ACUTE_SYMPTOM_ALERT: Detected critical emergency symptom phrase '{kw}'."

        return False, None

    def build_patient_medical_context(self, cycle_id: str) -> str:
        """
        Assembles the authoritative medical context of the active patient cycle:
        1. Anamnesis (M1 validated output or raw questionnaire answers; explicitly noted if missing).
        2. Laboratory findings (active markers from authoritative ledger or OCR fallback; explicitly noted if absent).
        3. ECG examination (structured findings and metrics; explicitly noted if not conducted).
        4. Primary Consilium published client-facing conclusions (overall report + 8 system summaries; explicitly noted if pending).
        Strictly scopes all queries to `cycle_id` to prevent cross-cycle data mixing.
        """
        # 1. Anamnesis
        anamnesis_lines: List[str] = []
        m1_reports = self.report_repo.list_server_only(
            cycle_id=cycle_id,
            report_type=PREPARATORY_ANAMNESIS_REPORT_TYPE,
        )
        if m1_reports:
            m1_content = m1_reports[0].content or {}
            user_view = m1_content.get("user_view") or {}
            summary = user_view.get("summary")
            if summary:
                anamnesis_lines.append(f"- Anamnesis summary: {summary.strip()}")
            system_logic = m1_content.get("system_logic") or {}
            facts: List[str] = []
            for sys_k in sorted(system_logic.keys()):
                organ_val = system_logic[sys_k]
                if isinstance(organ_val, dict):
                    data_str = organ_val.get("data")
                    if data_str and str(data_str).strip():
                        facts.append(f"{sys_k}: {data_str.strip()}")
            if facts:
                anamnesis_lines.append(f"- Clinical facts: {'; '.join(facts)}")
        else:
            raw_reports = self.report_repo.list_server_only(
                cycle_id=cycle_id,
                report_type=RAW_USER_ANAMNESIS_REPORT_TYPE,
            )
            if raw_reports:
                raw_answers = (raw_reports[0].content or {}).get("answers") or {}
                raw_summary_parts: List[str] = []
                if raw_answers.get("age"):
                    raw_summary_parts.append(f"Age: {raw_answers.get('age')}")
                if raw_answers.get("gender"):
                    raw_summary_parts.append(f"Gender: {raw_answers.get('gender')}")
                if raw_answers.get("height") and raw_answers.get("weight"):
                    raw_summary_parts.append(
                        f"Height/Weight: {raw_answers.get('height')} cm / {raw_answers.get('weight')} kg"
                    )
                if raw_answers.get("chronic_diseases") and str(raw_answers.get("chronic_diseases")).lower() != "none":
                    raw_summary_parts.append(f"Chronic diseases: {raw_answers.get('chronic_diseases')}")
                if raw_answers.get("medications") and str(raw_answers.get("medications")).lower() != "none":
                    raw_summary_parts.append(f"Medications: {raw_answers.get('medications')}")
                if raw_answers.get("sleep_quality"):
                    raw_summary_parts.append(f"Sleep quality: {raw_answers.get('sleep_quality')}")
                if raw_answers.get("diet_types"):
                    raw_summary_parts.append(f"Diet: {raw_answers.get('diet_types')}")
                if raw_summary_parts:
                    anamnesis_lines.append(f"- Baseline intake answers: {'; '.join(raw_summary_parts)}")
                else:
                    anamnesis_lines.append("- Baseline intake answers recorded.")
            else:
                anamnesis_lines.append("- Status: Not completed (questionnaire pending).")

        # 2. Laboratory Studies
        from app.services.lab_context_service import assemble_consilium_laboratory_context
        lab_ctx = assemble_consilium_laboratory_context(self.db, cycle_id)
        lab_lines: List[str] = []
        if lab_ctx.shared_labs_table:
            for marker_key in sorted(lab_ctx.shared_labs_table.keys()):
                marker = lab_ctx.shared_labs_table[marker_key]
                if isinstance(marker, dict):
                    disp = marker.get("display_name") or marker_key
                    val = marker.get("value")
                    unit = marker.get("unit") or ""
                    ref = marker.get("reference_range")
                    ref_str = ""
                    if isinstance(ref, dict):
                        ref_text = ref.get("text") or (
                            f"{ref.get('low')}-{ref.get('high')}"
                            if ref.get("low") is not None or ref.get("high") is not None
                            else ""
                        )
                        if ref_text:
                            ref_str = f" [Ref: {ref_text}]"
                    date_str = f" ({marker.get('sample_date')})" if marker.get("sample_date") else ""
                    lab_lines.append(f"- {disp}: {val} {unit}{ref_str}{date_str}".strip())
                else:
                    lab_lines.append(f"- {marker_key}: {marker}")
        else:
            lab_lines.append("- Status: No laboratory studies recorded for this cycle.")

        # 3. ECG Examination
        from app.services.ecg_context_service import assemble_consilium_ecg_context
        ecg_ctx = assemble_consilium_ecg_context(self.db, cycle_id)
        ecg_lines: List[str] = []
        if ecg_ctx.is_conducted:
            ecg_lines.append(f"- Summary: {ecg_ctx.ecg_summary}")
            if ecg_ctx.heart_rate_bpm:
                ecg_lines.append(f"- Heart rate: {ecg_ctx.heart_rate_bpm} bpm")
            if ecg_ctx.rhythm_type:
                reg_str = f" ({ecg_ctx.rhythm_regularity})" if ecg_ctx.rhythm_regularity else ""
                ecg_lines.append(f"- Rhythm: {ecg_ctx.rhythm_type}{reg_str}")
            if ecg_ctx.record_date:
                ecg_lines.append(f"- Recording date: {ecg_ctx.record_date}")
        else:
            ecg_lines.append("- Status: Not conducted (no ECG recorded for this cycle).")

        # 4. Primary Consilium Published Conclusions (Client-Facing)
        consilium_lines: List[str] = []
        overall_reports = self.report_repo.list_client_facing(
            cycle_id=cycle_id,
            report_type="user_overall_report",
        )
        if overall_reports:
            overall_content = overall_reports[0].content or {}
            title = overall_content.get("title", "Primary Health Consilium")
            summary = overall_content.get("summary", "")
            highlights = overall_content.get("highlights") or []
            consilium_lines.append(f"- Overall conclusion ({title}): {summary}")
            if highlights:
                consilium_lines.append(f"- Key highlights: {'; '.join(str(h) for h in highlights)}")
            # Personalized vitals thresholds from published overall report
            metrics = overall_content.get("metrics")
            if isinstance(metrics, dict) and metrics:
                metric_parts: List[str] = []
                min_hr = metrics.get("min_pessimum_bpm")
                max_hr = metrics.get("max_pessimum_bpm")
                spike_delta = metrics.get("max_spike_delta_bpm")

                if min_hr is not None:
                    metric_parts.append(f"min {min_hr} bpm")
                if max_hr is not None:
                    metric_parts.append(f"max {max_hr} bpm")
                if spike_delta is not None:
                    metric_parts.append(f"max spike delta {spike_delta} bpm")

                if metric_parts:
                    consilium_lines.append(f"- Monitored thresholds: {', '.join(metric_parts)}.")
                else:
                    consilium_lines.append("- Monitored thresholds: Individual vitals thresholds unavailable.")
            else:
                consilium_lines.append("- Monitored thresholds: Individual vitals thresholds unavailable.")

            client_reports = self.report_repo.list_client_facing(cycle_id=cycle_id)
            for r in client_reports:
                if r.report_type.startswith("system_user_report_"):
                    c = r.content or {}
                    sys_title = c.get("title") or r.report_type
                    sys_summary = c.get("summary") or ""
                    if sys_summary:
                        consilium_lines.append(f"- {sys_title}: {sys_summary}")
        else:
            consilium_lines.append("- Status: Primary Consilium has not been executed yet for this cycle.")

        sections = [
            "=== AUTHORITATIVE PATIENT MEDICAL CONTEXT (ACTIVE CYCLE) ===",
            "1. ANAMNESIS:",
            "\n".join(anamnesis_lines),
            "",
            "2. LABORATORY STUDIES:",
            "\n".join(lab_lines),
            "",
            "3. ELECTROCARDIOGRAM (ECG):",
            "\n".join(ecg_lines),
            "",
            "4. PRIMARY CONSILIUM CONCLUSIONS (PUBLISHED):",
            "\n".join(consilium_lines),
        ]
        return "\n".join(sections)

    def _resolve_strategy_context(
        self, cycle_id: str
    ) -> Tuple[str, List[Dict[str, str]], Optional[str]]:
        medical_context = self.build_patient_medical_context(cycle_id)

        try:
            ctx = self.weekly_service.get_active_athena_context(cycle_id)
            full_curator_context = "\n\n".join(
                [
                    medical_context,
                    "=== GUIDELINES AND CLINICAL ROUTE FROM CHIEF PHYSICIAN (PRO) ===",
                    ctx.active_strategy_text,
                    "=== DEEP CLINICAL ANALYSIS AND SYNDROMES (PRO TRAINING) ===",
                    ctx.clinical_background_text,
                    "=== PRIMARY SYNTHESIS (FLASH) ===",
                    ctx.deputy_synthesis_text,
                    ctx.athena_logs_summary,
                ]
            )
            system_prompt = build_athena_system_prompt(full_curator_context)
            return system_prompt, ctx.chat_history, ctx.sprint_id
        except ConsiliumContextMissingError:
            # Fallback for newly paired patient prior to Primary Consilium execution
            onboarding_route = (
                "=== GUIDELINES AND CLINICAL ROUTE ===\n"
                "Patient is in initial onboarding (Primary Consilium not yet executed). "
                "Welcome the user warmly to Aegis Health, encourage them to complete the initial anamnesis questionnaire, "
                "upload laboratory studies or ECG if available, track daily vitals, "
                "and provide empathetic wellness companionship based on the available data."
            )
            full_curator_context = "\n\n".join(
                [
                    medical_context,
                    onboarding_route,
                ]
            )
            system_prompt = build_athena_system_prompt(full_curator_context)
            chat_history = self.weekly_service.get_athena_chat_history(cycle_id)
            return system_prompt, chat_history, None

    @staticmethod
    def _build_turn_prompt_and_record(request: AthenaChatRequest) -> Tuple[str, str]:
        event_type = request.event_type

        if event_type == "vitals_alert":
            if request.vitals_alert is None:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="vitals_alert context must be provided when event_type is 'vitals_alert'.",
                )
            va = request.vitals_alert
            prompt = f"""Service pulse: Heart rate spike detected (peak up to {va.current_pulse} bpm with a baseline of {va.baseline_pulse} bpm at rest).{f' Additional notes: {va.notes}' if va.notes else ''}

YOUR INSTRUCTION (MODE 2: ATTENTION AND SUPPORT):
1. THE "GOODBYE UNTIL TOMORROW" RULE IS STRICTLY CANCELED!
2. Speak warmly and calmly. Ask what happened, ask them to sit down, unbutton their collar, and breathe.
3. Filter out "red flags" (chest pain, shortness of breath, lack of air, darkening in the eyes).
4. Be sure to formulate [CLINICAL_REPORT] with "ALERT" (or "CRITICAL") status, specifying "red_flags" and a summary for the Chief Physician.
5. If the condition is critical, add to the very end of the response (after [CLINICAL_REPORT]): [EMERGENCY_TRIGGER].
"""
            user_record = f"[Vitals Alert]: Heart rate peak {va.current_pulse} bpm (baseline {va.baseline_pulse} bpm)"
            return prompt, user_record

        if event_type == "daily_pulse":
            s_day = request.sprint_day or 1
            prompt = f"""Service pulse: A NEW DAY HAS COME (ROUTINE MODE). Current sprint day: {s_day} of 7.
Study the history of past answers and the current step of the Pro route.
1. Gently greet the user.
2. Formulate 1 NEXT question along the route with 2-3 response options.
3. Generate the current [CLINICAL_REPORT].
"""
            user_record = f"[Daily Pulse]: Day {s_day} check-in"
            return prompt, user_record

        if event_type == "initial_greeting":
            prompt = """Service pulse: FIRST INTRODUCTION.
Briefly and sincerely introduce yourself (Athena), say that you will help follow the route.
Formulate the first targeted guiding question on the first step of the Pro route with 2-3 everyday response options.
Don't forget to generate the initial [CLINICAL_REPORT].
"""
            user_record = "[Initialization]: First introduction"
            return prompt, user_record

        # Default: user_message
        msg = (request.message or "").strip()
        if not msg:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="message must be provided and non-empty when event_type is 'user_message'.",
            )
        s_day = request.sprint_day or 1
        is_sprint_end = (s_day == 7)

        teaser_phrase = (
            '- SINCE TODAY IS DAY 7 OF THE SPRINT: At the very end of the response on a new line, '
            'add a natural teaser phrase: "By the way, wait a minute, let me show you how you spent this week!"\n'
            if is_sprint_end else ''
        )

        prompt = f"""User replied: "{msg}"

YOUR INSTRUCTION:
1. If the response is ordinary and routine:
   - Briefly comment on the response with empathy.
   - Confirm data recording.
   - Say goodbye until tomorrow.
   {teaser_phrase}   - Fill in [CLINICAL_REPORT] with "NORMAL" or "ALERT" status (depending on plan adherence).

2. If the user reported POOR WELL-BEING (pain, weakness, nausea, anxiety):
   - DO NOT SAY GOODBYE! Activate Mode 2 (Force Majeure).
   - Ask about symptoms, reassure, help ground.
   - Fill in [CLINICAL_REPORT] with "ALERT" or "CRITICAL" status and detail "symptoms_mentioned" and "red_flags".
   - In case of a real life-threatening situation, add strictly at the very end on a new line: [EMERGENCY_TRIGGER].
"""
        return prompt, msg

    @staticmethod
    def _build_gemini_contents(
        chat_history: List[Dict[str, str]],
        current_prompt: str,
    ) -> List[Dict[str, Any]]:
        contents: List[Dict[str, Any]] = []

        if chat_history:
            last_role: Optional[str] = None
            for item in chat_history:
                sender = str(item.get("sender", "curator")).lower()
                text = str(item.get("text", "")).strip()
                if not text:
                    continue
                role = "user" if sender == "user" else "model"
                if role != last_role:
                    contents.append({"role": role, "parts": [{"text": text}]})
                    last_role = role

        # Ensure first turn is from user if history starts with model
        if contents and contents[0]["role"] == "model":
            contents.insert(0, {"role": "user", "parts": [{"text": "Athena chat initialization."}]})

        # Append current user prompt as final turn
        contents.append({"role": "user", "parts": [{"text": current_prompt}]})
        return contents

    async def _execute_generation_with_fallback(
        self,
        *,
        contents: List[Dict[str, Any]],
        system_instruction: str,
        pre_emergency: bool,
    ) -> Tuple[str, bool]:
        """
        Executes Gemini call. If Gemini is unavailable / fails AND pre_emergency is True,
        falls back to deterministic emergency safety advice instead of failing with 502.
        """
        try:
            raw_text = await self.gateway.generate_raw_text(
                contents=contents,
                model=GeminiModelRole.FLASH,
                system_instruction=system_instruction,
                response_mime_type="text/plain",
            )
            return raw_text, False
        except Exception as exc:
            if pre_emergency:
                logger.error(
                    f"Gemini API failure during acute emergency: {exc}. "
                    "Engaging deterministic fail-safe emergency response protocol."
                )
                emergency_fallback = (
                    "Система Aegis зафиксировала тревожные показатели самочувствия и пульса. "
                    "Пожалуйста, немедленно присядьте, расстегните воротник и обеспечьте приток свежего воздуха. "
                    "Если вы ощущаете боль за грудиной, одышку, нехватку воздуха или потемнение в глазах — "
                    "незамедлительно вызовите скорую помощь (112).\n\n"
                    "[CLINICAL_REPORT]\n"
                    "{\n"
                    '  "status": "CRITICAL",\n'
                    '  "symptoms_mentioned": ["critical_vitals_or_acute_symptoms"],\n'
                    '  "subjective_wellbeing_score": null,\n'
                    '  "adherence_to_plan": "UNKNOWN",\n'
                    '  "red_flags": true,\n'
                    '  "summary_for_master": "Emergency fail-safe activated: acute vitals/symptoms with AI gateway offline"\n'
                    "}\n"
                    "[/CLINICAL_REPORT]\n"
                    "[EMERGENCY_TRIGGER]"
                )
                return emergency_fallback, True

            logger.error(f"Gemini API error during Athena turn: {exc}")
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Athena AI service temporarily unavailable: {exc}",
            ) from exc

    def _process_and_persist_reports(
        self,
        *,
        cycle_id: str,
        sprint_id: Optional[str],
        raw_response: str,
        pre_emergency: bool,
        pre_emergency_notice: Optional[str],
        used_emergency_fallback: bool,
    ) -> Tuple[str, bool, Optional[str]]:
        working_text = raw_response

        # 1. Detect and strip [EMERGENCY_TRIGGER]
        emergency_trigger_found = bool(EMERGENCY_TRIGGER_REGEX.search(working_text))
        working_text = EMERGENCY_TRIGGER_REGEX.sub("", working_text).strip()

        # 2. Extract and parse [CLINICAL_REPORT] JSON
        clinical_match = CLINICAL_REPORT_REGEX.search(working_text)
        is_critical_report = False

        if clinical_match:
            raw_json = clinical_match.group(1).strip()
            # Clean possible markdown fence
            raw_json = re.sub(r"^```json\s*", "", raw_json, flags=re.IGNORECASE)
            raw_json = re.sub(r"^```\s*", "", raw_json)
            raw_json = re.sub(r"\s*```$", "", raw_json)

            try:
                parsed_dict = json.loads(raw_json)
                log_entry = AthenaClinicalLogEntry.model_validate(parsed_dict)
                if log_entry.status.upper() == "CRITICAL" or log_entry.red_flags:
                    is_critical_report = True

                self.weekly_service.save_athena_clinical_log(
                    cycle_id=cycle_id,
                    log_entry=log_entry,
                    sprint_id=sprint_id,
                )
            except Exception as parse_err:
                logger.warning(f"Failed to parse Athena hidden [CLINICAL_REPORT]: {parse_err}")
                # Save fallback clinical report
                fallback_entry = AthenaClinicalLogEntry(
                    status="ALERT" if not pre_emergency else "CRITICAL",
                    symptoms_mentioned=["parsing_fallback"],
                    red_flags=pre_emergency,
                    summary_for_master="Athena turn recorded (clinical report parsing fallback)",
                )
                self.weekly_service.save_athena_clinical_log(
                    cycle_id=cycle_id,
                    log_entry=fallback_entry,
                    sprint_id=sprint_id,
                )

            # Completely strip [CLINICAL_REPORT] from client text
            working_text = CLINICAL_REPORT_REGEX.sub("", working_text).strip()

        # Extra defense-in-depth: strip unclosed clinical report blocks, emergency trigger tags, or internal headers
        working_text = re.sub(r"\[CLINICAL_REPORT\][\s\S]*$", "", working_text, flags=re.IGNORECASE).strip()
        working_text = re.sub(r"\[/?CLINICAL_REPORT\]", "", working_text, flags=re.IGNORECASE).strip()
        working_text = EMERGENCY_TRIGGER_REGEX.sub("", working_text).strip()
        working_text = re.sub(
            r"=== (?:AUTHORITATIVE PATIENT MEDICAL CONTEXT|GUIDELINES AND CLINICAL ROUTE|DEEP CLINICAL ANALYSIS|PRIMARY SYNTHESIS|INSTRUCTIONS AND ROUTE)[\s\S]*?===\n?",
            "",
            working_text,
            flags=re.IGNORECASE,
        ).strip()

        clean_reply = working_text.strip()

        # Final structured emergency determination
        final_emergency = (
            emergency_trigger_found
            or is_critical_report
            or pre_emergency
            or used_emergency_fallback
        )

        final_notice: Optional[str] = None
        if final_emergency:
            final_notice = (
                pre_emergency_notice
                or ("EMERGENCY_TRIGGER: Critical clinical signal detected by Athena." if emergency_trigger_found
                    else "CRITICAL_STATUS: Clinical indicators require immediate medical attention.")
            )

        return clean_reply, final_emergency, final_notice
