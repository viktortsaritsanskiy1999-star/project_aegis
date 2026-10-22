from datetime import datetime, timedelta, timezone
import json
from typing import Any, Dict, List, Optional
from sqlalchemy.orm import Session

from app.models.model_report import ModelReport
from app.repositories.cycle_repository import MedicalCycleRepository
from app.repositories.measurement_repository import HealthMeasurementRepository
from app.repositories.report_repository import ModelReportRepository
from app.repositories.sprint_repository import WeeklySprintRepository
from app.schemas.consilium import (
    SYSTEM_KEYS,
    SYSTEM_TITLES,
    AegisAgentRole,
    WeeklyFlashMasterOutput,
    WeeklyProMasterOutput,
    AthenaClinicalLogEntry,
    ActiveAthenaStrategyContext,
    AthenaSessionState,
    WeeklyConsiliumClientResponse,
    validate_no_guaranteed_unchanged_claim,
)
from app.schemas.privacy import PrivacyLevel
from app.schemas.reports import ClientModelReportResponse
from app.services.consilium_prompts import (
    build_weekly_flash_prompt,
    build_weekly_pro_prompt,
    build_athena_system_prompt,
)
from app.services.gemini_gateway import GeminiGateway, GeminiModelRole
from app.services.primary_consilium_service import (
    ConsiliumError,
    ConsiliumCycleNotFoundError,
    ConsiliumContextMissingError,
    ConsiliumQualityRejectionError,
    PREPARATORY_ANAMNESIS_REPORT_TYPE,
    PREPARATORY_OCR_REPORT_TYPE,
    PREPARATORY_ECG_REPORT_TYPE,
)


ATHENA_CLINICAL_LOG_REPORT_TYPE = "athena_clinical_log"
ATHENA_CHAT_HISTORY_REPORT_TYPE = "athena_chat_history"
ATHENA_ACTIVE_STRATEGY_REPORT_TYPE = "athena_active_strategy"
WEEKLY_DEPUTY_INTERNAL_REPORT_TYPE = "weekly_deputy_internal"
WEEKLY_CHIEF_INTERNAL_REPORT_TYPE = "weekly_chief_physician_internal"
WEEKLY_OVERALL_CLIENT_REPORT_TYPE = "weekly_user_overall_report"


class WeeklyConsiliumService:
    """
    Server-side orchestrator for the Weekly Medical Consilium (Stage 3.3):
    Strictly executes: Gemini Flash (Deputy Chief Physician) -> Gemini Pro (Chief Physician).
    - Never calls the 8 Flash-Lite organ specialists, Vision OCR/ECG, or Anamnesis M1.
    - Reads previous internal conclusions, active strategy, 7-day Athena diary, and new vitals.
    - Generates 8 dated user-facing Atlas system updates (with friendly messages when no new info is present)
      and a separate weekly overall report for the Client Vault.
    - Persists the updated active Athena strategy linked to cycle_id, sprint_id, and version,
      ensuring subsequent Athena interactions always refresh their system instruction without
      losing chat history.
    """

    def __init__(self, db: Session, gateway: Optional[GeminiGateway] = None):
        self.db = db
        self.gateway = gateway or GeminiGateway()
        self.cycle_repo = MedicalCycleRepository(db)
        self.sprint_repo = WeeklySprintRepository(db)
        self.report_repo = ModelReportRepository(db)
        self.measurement_repo = HealthMeasurementRepository(db)

    # ==========================================================================
    # 1. ATHENA 7-DAY CLINICAL DIARY & CHAT HISTORY (SERVER-ONLY)
    # ==========================================================================

    def _resolve_or_create_sprint_id(
        self,
        cycle_id: str,
        sprint_id: Optional[str] = None,
    ) -> Optional[str]:
        if sprint_id is not None:
            existing_sprint = self.sprint_repo.get(sprint_id)
            if existing_sprint is None:
                next_num = len(self.sprint_repo.list_by_cycle(cycle_id)) + 1
                self.sprint_repo.create(
                    sprint_id=sprint_id,
                    cycle_id=cycle_id,
                    sprint_number=next_num,
                    status="active",
                )
            elif existing_sprint.cycle_id != cycle_id:
                raise ConsiliumError(
                    f"Sprint '{sprint_id}' belongs to cycle '{existing_sprint.cycle_id}', not '{cycle_id}'."
                )
            return sprint_id

        active_sprint = self.sprint_repo.get_active_for_cycle(cycle_id)
        return active_sprint.sprint_id if active_sprint is not None else None

    def save_athena_clinical_log(
        self,
        cycle_id: str,
        log_entry: AthenaClinicalLogEntry | Dict[str, Any],
        sprint_id: Optional[str] = None,
    ) -> ModelReport:
        """
        Stores a hidden [CLINICAL_REPORT] entry from Athena as a server_only report
        tagged with logical role AegisAgentRole.ATHENA_CURATOR and linked to cycle_id and sprint_id.
        """
        if not self.cycle_repo.get(cycle_id):
            raise ConsiliumCycleNotFoundError(f"Medical cycle '{cycle_id}' not found.")

        validated = (
            log_entry
            if isinstance(log_entry, AthenaClinicalLogEntry)
            else AthenaClinicalLogEntry.model_validate(log_entry)
        )

        resolved_sprint_id = self._resolve_or_create_sprint_id(cycle_id, sprint_id)

        payload = validated.model_dump()
        if not payload.get("timestamp"):
            payload["timestamp"] = datetime.now(timezone.utc).isoformat()
        payload["agent_role"] = AegisAgentRole.ATHENA_CURATOR.value
        payload["sprint_id"] = resolved_sprint_id

        return self.report_repo.create_next_version(
            cycle_id=cycle_id,
            sprint_id=resolved_sprint_id,
            model=self.gateway.flash_model,
            report_type=ATHENA_CLINICAL_LOG_REPORT_TYPE,
            privacy_level=PrivacyLevel.SERVER_ONLY,
            content=payload,
        )

    @staticmethod
    def _parse_iso_datetime_utc(raw_value: Optional[str | datetime]) -> Optional[datetime]:
        if raw_value is None:
            return None
        if isinstance(raw_value, datetime):
            if raw_value.tzinfo is None:
                return raw_value.replace(tzinfo=timezone.utc)
            return raw_value.astimezone(timezone.utc)
        if not isinstance(raw_value, str):
            return None
        text = raw_value.strip()
        if not text:
            return None
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            if len(text) == 10 and text[4] == "-" and text[7] == "-":
                parsed_date = datetime.strptime(text, "%Y-%m-%d").date()
                return datetime(
                    parsed_date.year,
                    parsed_date.month,
                    parsed_date.day,
                    0,
                    0,
                    0,
                    tzinfo=timezone.utc,
                )
            dt = datetime.fromisoformat(text)
            if dt.tzinfo is None:
                return dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except ValueError:
            return None

    @classmethod
    def _resolve_seven_day_window(
        cls, reference_date: Optional[str | datetime] = None
    ) -> tuple[datetime, datetime]:
        if isinstance(reference_date, str) and len(reference_date.strip()) == 10:
            parsed_ref = cls._parse_iso_datetime_utc(reference_date.strip())
            if parsed_ref is not None:
                window_end = parsed_ref.replace(
                    hour=23, minute=59, second=59, microsecond=999999
                )
                start_date = (parsed_ref - timedelta(days=7)).date()
                window_start = datetime(
                    start_date.year,
                    start_date.month,
                    start_date.day,
                    0,
                    0,
                    0,
                    tzinfo=timezone.utc,
                )
                return window_start, window_end

        ref_dt = cls._parse_iso_datetime_utc(reference_date) or datetime.now(timezone.utc)
        window_end = ref_dt
        start_date = (ref_dt - timedelta(days=7)).date()
        window_start = datetime(
            start_date.year,
            start_date.month,
            start_date.day,
            0,
            0,
            0,
            tzinfo=timezone.utc,
        )
        return window_start, window_end

    def get_seven_day_athena_logs(
        self,
        cycle_id: str,
        sprint_id: Optional[str] = None,
        reference_date: Optional[str | datetime] = None,
    ) -> List[AthenaClinicalLogEntry]:
        """
        Returns Athena's 7-day clinical diary entries for the given cycle and sprint
        in chronological order (up to the 7 daily logs of the current 7-day window):
        - Strictly filters by cycle_id and sprint_id (when provided), never mixing previous weeks.
        - Strictly excludes logs with future timestamps (log_dt > window_end).
        - Strictly excludes old logs outside the 7-day window (log_dt < window_start).
        """
        window_start, window_end = self._resolve_seven_day_window(reference_date)
        versions = self.report_repo.list_versions(
            cycle_id=cycle_id,
            report_type=ATHENA_CLINICAL_LOG_REPORT_TYPE,
            privacy_level=PrivacyLevel.SERVER_ONLY,
        )
        filtered: List[AthenaClinicalLogEntry] = []
        for r in versions:
            if sprint_id is not None and r.sprint_id != sprint_id:
                continue
            content = dict(r.content or {})
            content.pop("agent_role", None)
            content.pop("sprint_id", None)
            entry = AthenaClinicalLogEntry.model_validate(content)

            log_dt = self._parse_iso_datetime_utc(entry.timestamp) or self._parse_iso_datetime_utc(
                r.created_at
            )
            if log_dt is None:
                continue
            # Strictly exclude future dates and stale logs outside the 7-day period
            if log_dt > window_end or log_dt < window_start:
                continue

            filtered.append(entry)

        # Return the 7-day slice (latest 7 logs in chronological order within the window)
        return filtered[-7:]

    @staticmethod
    def format_athena_diary_for_consilium(logs: List[AthenaClinicalLogEntry]) -> str:
        """
        Formats Athena's 7-day clinical diary using the exact format from
        VaultService.getAthenaLogsSummaryForPro() in lib/services/vault_service.dart.
        """
        if not logs:
            return "Athena's hidden clinical logs are missing."

        lines = ["=== CURATOR CLINICAL OBSERVATION DIARY (ATHENA LOGS) ==="]
        for log in logs:
            symptoms_str = ", ".join(log.symptoms_mentioned) if log.symptoms_mentioned else "None"
            lines.append(f"⏱ Timestamp: {log.timestamp or 'Unknown'}")
            lines.append(f"📊 Status: {log.status}")
            lines.append(f"🚩 Symptoms: {symptoms_str}")
            lines.append(f"⚠️ Red Flags: {str(log.red_flags).lower()}")
            lines.append(f"📝 Summary for Chief Physician: {log.summary_for_master}")
            lines.append("--------------------------------------------------")
        return "\n".join(lines)

    def save_athena_chat_message(
        self,
        cycle_id: str,
        sender: str,
        text: str,
        sprint_id: Optional[str] = None,
    ) -> ModelReport:
        """
        Appends a chat turn ('user' or 'curator') to the server-side chat history for cycle_id
        so that strategy updates across weekly sprints never lose conversation history.
        """
        if not self.cycle_repo.get(cycle_id):
            raise ConsiliumCycleNotFoundError(f"Medical cycle '{cycle_id}' not found.")

        sender_norm = sender.strip().lower()
        if sender_norm not in ("user", "curator", "model"):
            raise ValueError("sender must be 'user', 'curator', or 'model'.")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("Chat message text cannot be empty.")

        resolved_sprint_id = self._resolve_or_create_sprint_id(cycle_id, sprint_id)
        existing_messages = self.get_athena_chat_history(cycle_id)
        updated_messages = list(existing_messages)
        updated_messages.append(
            {
                "sender": "user" if sender_norm == "user" else "curator",
                "text": text.strip(),
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        )

        return self.report_repo.create_next_version(
            cycle_id=cycle_id,
            sprint_id=resolved_sprint_id,
            model=self.gateway.flash_model,
            report_type=ATHENA_CHAT_HISTORY_REPORT_TYPE,
            privacy_level=PrivacyLevel.SERVER_ONLY,
            content={
                "agent_role": AegisAgentRole.ATHENA_CURATOR.value,
                "messages": updated_messages,
            },
        )

    def get_athena_chat_history(self, cycle_id: str) -> List[Dict[str, str]]:
        reports = self.report_repo.list_server_only(
            cycle_id=cycle_id,
            report_type=ATHENA_CHAT_HISTORY_REPORT_TYPE,
        )
        if not reports:
            return []
        latest = reports[0].content or {}
        messages = latest.get("messages")
        if not isinstance(messages, list):
            return []
        return [
            {
                "sender": str(m.get("sender", "curator")),
                "text": str(m.get("text", "")),
                "timestamp": str(m.get("timestamp", "")),
            }
            for m in messages
            if isinstance(m, dict) and str(m.get("text", "")).strip()
        ]

    # ==========================================================================
    # 2. SERVER-SIDE ACTIVE STRATEGY & SESSION REFRESH FOR ATHENA
    # ==========================================================================

    def get_active_athena_context(self, cycle_id: str) -> ActiveAthenaStrategyContext:
        """
        Returns the authoritative, latest active strategy and system prompt for Athena.
        - Checks versioned 'athena_active_strategy' reports first (created by Weekly Consilium).
        - Falls back to the Primary Consilium 'chief_physician_internal' report (v1) if no weekly
          sprint has completed yet.
        - Always embeds the latest strategy into Athena's system prompt while preserving chat_history.
        """
        if not self.cycle_repo.get(cycle_id):
            raise ConsiliumCycleNotFoundError(f"Medical cycle '{cycle_id}' not found.")

        strategy_reports = self.report_repo.list_server_only(
            cycle_id=cycle_id,
            report_type=ATHENA_ACTIVE_STRATEGY_REPORT_TYPE,
        )

        if strategy_reports:
            latest = strategy_reports[0]
            c = latest.content or {}
            active_strategy_text = str(c.get("active_strategy_text", "")).strip()
            clinical_background_text = str(
                c.get("clinical_background_text", "Deep correction of the Chief Physician's strategy based on weekly dynamics")
            ).strip()
            deputy_synthesis_text = str(c.get("deputy_synthesis_text", "")).strip()
            source_consilium = str(c.get("source_consilium", "weekly_consilium"))
            strategy_status = str(c.get("strategy_status", "updated"))
            strategy_version = int(c.get("strategy_version", latest.version))
            sprint_id = latest.sprint_id
        else:
            # Fallback to Primary Consilium (Stage 3.2) baseline strategy (v1)
            pro_primary = self.report_repo.list_server_only(
                cycle_id=cycle_id,
                report_type="chief_physician_internal",
            )
            flash_primary = self.report_repo.list_server_only(
                cycle_id=cycle_id,
                report_type="deputy_chief_physician_internal",
            )
            if not pro_primary or not flash_primary:
                raise ConsiliumContextMissingError(
                    f"No active strategy found for cycle '{cycle_id}': Primary Consilium has not been executed yet."
                )
            pro_c = pro_primary[0].content or {}
            flash_c = flash_primary[0].content or {}
            active_strategy_text = str(pro_c.get("pro_general_shelf", "")).strip()
            clinical_background_text = str(pro_c.get("pro_training_shelf", "")).strip()
            deputy_synthesis_text = str(flash_c.get("flash_general_shelf", "")).strip()
            source_consilium = "primary_consilium"
            strategy_status = "initial"
            strategy_version = 1
            sprint_id = pro_primary[0].sprint_id

        logs = self.get_seven_day_athena_logs(cycle_id=cycle_id, sprint_id=sprint_id)
        athena_logs_summary = self.format_athena_diary_for_consilium(logs)

        full_curator_context = "\n".join(
            [
                "=== GUIDELINES AND CLINICAL ROUTE FROM CHIEF PHYSICIAN (PRO) ===",
                active_strategy_text,
                "",
                "=== DEEP CLINICAL ANALYSIS AND SYNDROMES (PRO TRAINING) ===",
                clinical_background_text,
                "",
                "=== PRIMARY SYNTHESIS (FLASH) ===",
                deputy_synthesis_text,
                athena_logs_summary,
            ]
        )

        system_prompt = build_athena_system_prompt(full_curator_context)
        chat_history = self.get_athena_chat_history(cycle_id)

        return ActiveAthenaStrategyContext(
            cycle_id=cycle_id,
            sprint_id=sprint_id,
            strategy_version=strategy_version,
            source_consilium=source_consilium,
            strategy_status=strategy_status,
            active_strategy_text=active_strategy_text,
            clinical_background_text=clinical_background_text,
            deputy_synthesis_text=deputy_synthesis_text,
            athena_logs_summary=athena_logs_summary,
            daily_question_limit=3,
            system_prompt=system_prompt,
            chat_history=chat_history,
        )

    def sync_athena_session(
        self,
        cycle_id: str,
        cached_session: Optional[AthenaSessionState] = None,
    ) -> AthenaSessionState:
        """
        Ensures that any subsequent Athena turn uses the latest server-side strategy.
        Eliminates the CuratorAgent.initializeSession() stale-instruction defect by
        automatically forcing a system_prompt refresh whenever `strategy_version` changes
        (e.g. v1 -> v2 after Weekly Consilium), while preserving 100% of `chat_history`.
        """
        latest_ctx = self.get_active_athena_context(cycle_id)

        needs_reinit = (
            cached_session is None
            or cached_session.cycle_id != cycle_id
            or cached_session.applied_strategy_version != latest_ctx.strategy_version
            or cached_session.system_prompt != latest_ctx.system_prompt
        )

        # Merge/preserve any in-memory chat history with server-persisted chat history
        preserved_history = list(latest_ctx.chat_history)
        if cached_session is not None and len(cached_session.chat_history) > len(preserved_history):
            preserved_history = list(cached_session.chat_history)

        return AthenaSessionState(
            cycle_id=cycle_id,
            sprint_id=latest_ctx.sprint_id,
            applied_strategy_version=latest_ctx.strategy_version,
            system_prompt=latest_ctx.system_prompt,
            chat_history=preserved_history,
            was_reinitialized=needs_reinit,
        )

    # ==========================================================================
    # 3. INTERNAL CONTEXT LOADERS FOR WEEKLY CONSILIUM
    # ==========================================================================

    def _load_previous_consilium_context(self, cycle_id: str) -> Dict[str, Any]:
        """
        Loads the baseline Primary Consilium outputs and any prior Weekly Consilium
        outputs from server_only storage without re-running M1, Vision, or 8 Flash-Lite.
        """
        pro_primary_list = self.report_repo.list_server_only(
            cycle_id=cycle_id,
            report_type="chief_physician_internal",
        )
        flash_primary_list = self.report_repo.list_server_only(
            cycle_id=cycle_id,
            report_type="deputy_chief_physician_internal",
        )
        if not pro_primary_list or not flash_primary_list:
            raise ConsiliumContextMissingError(
                f"Cannot run Weekly Consilium for cycle '{cycle_id}': Primary Consilium "
                "('deputy_chief_physician_internal' and 'chief_physician_internal') has not been completed yet."
            )

        pro_latest = pro_primary_list[0].content or {}
        flash_latest = flash_primary_list[0].content or {}
        pro_initial = pro_primary_list[-1].content or {}
        flash_initial = flash_primary_list[-1].content or {}

        # Check if prior weekly sprints or active strategy versions exist
        strategy_reports = self.report_repo.list_server_only(
            cycle_id=cycle_id,
            report_type=ATHENA_ACTIVE_STRATEGY_REPORT_TYPE,
        )
        prior_weekly_flash = self.report_repo.list_server_only(
            cycle_id=cycle_id,
            report_type=WEEKLY_DEPUTY_INTERNAL_REPORT_TYPE,
        )
        prior_weekly_pro = self.report_repo.list_server_only(
            cycle_id=cycle_id,
            report_type=WEEKLY_CHIEF_INTERNAL_REPORT_TYPE,
        )

        if strategy_reports:
            latest_strat_c = strategy_reports[0].content or {}
            latest_pro_strategy = str(
                latest_strat_c.get("active_strategy_text")
                or pro_latest.get("pro_general_shelf", "")
            )
            latest_flash_general = str(
                latest_strat_c.get("deputy_synthesis_text")
                or flash_latest.get("flash_general_shelf", "")
            )
        else:
            latest_flash_general = (
                str(prior_weekly_flash[0].content.get("weekly_deputy_analysis", ""))
                if prior_weekly_flash
                else str(flash_latest.get("flash_general_shelf", ""))
            )
            latest_pro_strategy = (
                str(prior_weekly_pro[0].content.get("updated_weekly_strategy", ""))
                if prior_weekly_pro
                else str(pro_latest.get("pro_general_shelf", ""))
            )
        latest_flash_training = str(flash_latest.get("flash_training_shelf", ""))
        latest_pro_training = str(pro_latest.get("pro_training_shelf", ""))

        # Load stored 8 organ specialist conclusions from latest full consilium (Primary or Lab-Revision; read-only)
        organ_summaries: List[str] = []
        for system_key in SYSTEM_KEYS:
            role_val = AegisAgentRole.for_system_key(system_key).value
            sys_reports = self.report_repo.list_server_only(
                cycle_id=cycle_id,
                report_type=role_val,
            )
            if sys_reports:
                sys_c = sys_reports[0].content or {}
                gen_shelf = (sys_c.get("system_logic") or {}).get("system_general_shelf", "No data")
                train_shelf = (sys_c.get("training_sandbox") or {}).get("system_training_shelf", "No data")
                organ_summaries.append(
                    f"--- BLOCK: {system_key.upper()} ---\n"
                    f"Main conclusion: {gen_shelf}\n"
                    f"Training log: {train_shelf}\n"
                )

        # Load latest vitals measurements (strictly excluding historical data from live stream)
        measurements = self.measurement_repo.list_by_cycle(cycle_id=cycle_id, is_historical=False, is_simulated=False, limit=100)
        systolic = next((m.systolic for m in reversed(measurements) if m.systolic is not None), None)
        diastolic = next((m.diastolic for m in reversed(measurements) if m.diastolic is not None), None)
        pulse = next((m.pulse for m in reversed(measurements) if m.pulse is not None), None)
        spo2 = next((m.spo2 for m in reversed(measurements) if m.spo2 is not None), None)

        from app.services.ecg_context_service import assemble_consilium_ecg_context

        ecg_ctx = assemble_consilium_ecg_context(self.db, cycle_id)
        ecg_summary = ecg_ctx.ecg_summary

        anamnesis_reports = self.report_repo.list_server_only(
            cycle_id=cycle_id,
            report_type=PREPARATORY_ANAMNESIS_REPORT_TYPE,
        )
        anamnesis_summary = (
            (anamnesis_reports[0].content or {}).get("user_view", {})
            if anamnesis_reports
            else {}
        )

        # Determine the latest laboratory state and distinguish data evaluated by the
        # last Full Consilium (Primary or Lab-Revision) from new unreviewed lab changes.
        from app.services.lab_context_service import assemble_consilium_laboratory_context

        latest_lab_ctx = assemble_consilium_laboratory_context(self.db, cycle_id)

        from app.services.lab_context_service import get_last_evaluated_ledger_version

        last_evaluated_ver = get_last_evaluated_ledger_version(self.db, cycle_id)

        unreviewed_pending_labs: Dict[str, Any] = {}
        pending_removed_marker_keys: List[str] = []

        if latest_lab_ctx.ledger_version > last_evaluated_ver and last_evaluated_ver > 0:
            from app.services.lab_context_service import LaboratoryLedgerVersionNotFoundError

            eval_lab_ctx = None
            try:
                eval_lab_ctx = assemble_consilium_laboratory_context(
                    self.db, cycle_id, ledger_version=last_evaluated_ver
                )
            except LaboratoryLedgerVersionNotFoundError:
                eval_lab_ctx = None

            if eval_lab_ctx is not None:
                shared_labs_shelf = eval_lab_ctx.shared_labs_table

                # 1. Detect removed or invalidated markers (previously active in evaluated shelf, now missing)
                pending_removed_marker_keys = [
                    k for k in shared_labs_shelf if k not in latest_lab_ctx.shared_labs_table
                ]

                # 2. Collect signatures of all observations evaluated by previous consilium
                evaluated_obs_keys = {
                    obs.get("observation_id")
                    or (
                        obs.get("marker_key"),
                        str(obs.get("value")),
                        str(obs.get("sample_date")),
                        str(obs.get("recorded_at")),
                    )
                    for obs in eval_lab_ctx.laboratory_history
                }

                # 3. Detect new observations in history (new dated studies, corrections, etc.)
                new_observations = [
                    obs
                    for obs in latest_lab_ctx.laboratory_history
                    if (
                        obs.get("observation_id")
                        or (
                            obs.get("marker_key"),
                            str(obs.get("value")),
                            str(obs.get("sample_date")),
                            str(obs.get("recorded_at")),
                        )
                    )
                    not in evaluated_obs_keys
                ]
                new_obs_marker_keys = {
                    obs.get("marker_key") for obs in new_observations if obs.get("marker_key")
                }

                # 4. Form unreviewed_pending_labs:
                # - newly added active markers
                # - mutated active markers (value, unit, date, ref range, etc.)
                # - active markers that received new dated observations in history (even if active value is identical)
                for k, marker_val in latest_lab_ctx.shared_labs_table.items():
                    if (
                        k not in shared_labs_shelf
                        or shared_labs_shelf[k] != marker_val
                        or k in new_obs_marker_keys
                    ):
                        unreviewed_pending_labs[k] = marker_val

                # Since ledger_version > last_evaluated_ver, new lab data was committed to ledger
                has_unreviewed_lab_changes = True
            else:
                # Safe indeterminate fallback if requested evaluated ledger version is missing from database
                shared_labs_shelf = {}
                unreviewed_pending_labs = dict(latest_lab_ctx.shared_labs_table)
                pending_removed_marker_keys = []
                has_unreviewed_lab_changes = bool(
                    latest_lab_ctx.shared_labs_table or latest_lab_ctx.laboratory_history
                )
        elif last_evaluated_ver == 0 and latest_lab_ctx.ledger_version > 0:
            # Audit Remediation C-4:
            # Ledger exists, but has NEVER been evaluated by a full consilium (or Primary ran on fallback).
            # Safe indeterminate state: do not invent evaluated markers.
            shared_labs_shelf = {}
            unreviewed_pending_labs = dict(latest_lab_ctx.shared_labs_table)
            pending_removed_marker_keys = []
            has_unreviewed_lab_changes = bool(latest_lab_ctx.shared_labs_table or latest_lab_ctx.laboratory_history)
        else:
            shared_labs_shelf = latest_lab_ctx.shared_labs_table
            has_unreviewed_lab_changes = False

        # Full chronological observations history is always provided so no dated studies are lost
        laboratory_history = latest_lab_ctx.laboratory_history

        initial_context_lines = [
            "=== ANAMNESIS AND INPUT DATA ===",
            json.dumps(
                {
                    "cycle_id": cycle_id,
                    "anamnesis_user_view": anamnesis_summary,
                    "shared_labs_shelf": shared_labs_shelf,
                    "evaluated_ledger_version": last_evaluated_ver,
                    "latest_ledger_version": latest_lab_ctx.ledger_version,
                    "has_unreviewed_lab_changes": has_unreviewed_lab_changes,
                    "unreviewed_pending_labs": unreviewed_pending_labs,
                    "pending_removed_marker_keys": pending_removed_marker_keys,
                    "laboratory_history": laboratory_history,
                    "ecg_context": ecg_ctx.to_dict(),
                },
                ensure_ascii=False,
            ),
            "",
            "=== VITAL MARKERS (SMART WATCH / LIVE) ===",
            f"Blood pressure: {systolic if systolic is not None else '--'}/{diastolic if diastolic is not None else '--'} mmHg",
            f"Heart rate (Pulse): {pulse if pulse is not None else '--'} bpm",
            f"Oxygen saturation (SpO2): {spo2 if spo2 is not None else '--'}%",
            f"ECG status: {ecg_summary}",
            "",
            "=== CONCLUSIONS OF 8 ORGAN SYSTEM AGENTS ===",
            "\n".join(organ_summaries),
            "=== SYNTHESIS (GEMINI FLASH) ===",
            "Main conclusion:",
            latest_flash_general,
            "",
            "Training output:",
            latest_flash_training,
            "",
            "=== ACTIVE CHIEF PHYSICIAN STRATEGY (GEMINI PRO) ===",
            latest_pro_strategy,
            "",
            "=== CHIEF PHYSICIAN CLINICAL TRAINING SANDBOX ===",
            latest_pro_training,
        ]

        return {
            "initial_context": "\n".join(initial_context_lines),
            "latest_pro_strategy": latest_pro_strategy,
            "latest_pro_training": latest_pro_training,
            "primary_flash_general": str(flash_initial.get("flash_general_shelf", "")),
            "primary_pro_general": str(pro_initial.get("pro_general_shelf", "")),
            "primary_pro_training": str(pro_initial.get("pro_training_shelf", "")),
            "shared_labs_shelf": shared_labs_shelf,
            "unreviewed_pending_labs": unreviewed_pending_labs,
            "pending_removed_marker_keys": pending_removed_marker_keys,
            "has_unreviewed_lab_changes": has_unreviewed_lab_changes,
            "laboratory_history": laboratory_history,
            "evaluated_ledger_version": last_evaluated_ver,
            "latest_ledger_version": latest_lab_ctx.ledger_version,
        }

    # ==========================================================================
    # 4. WEEKLY CONSILIUM EXECUTION (STRICTLY FLASH -> PRO)
    # ==========================================================================

    async def run_weekly_consilium(
        self,
        cycle_id: str,
        sprint_id: Optional[str] = None,
        report_date: Optional[str] = None,
    ) -> WeeklyConsiliumClientResponse:
        """
        Executes the server-side Weekly Consilium for `cycle_id`:
        1. Strictly calls ONLY Gemini Flash (Deputy Chief Physician) -> Gemini Pro (Chief Physician).
           Never calls the 8 Flash-Lite models, Anamnesis M1, or Vision OCR/ECG.
        2. Passes previous conclusions, active strategy, and the 7-day Athena diary to Flash and Pro.
        3. Validates and quality-checks Flash's 8 dated system updates and Pro's weekly overall report.
        4. Persists all server_only and client_facing reports, active Athena strategy version,
           and sprint completion inside a single atomic database transaction without closing the cycle.
        """
        cycle = self.cycle_repo.get(cycle_id)
        if not cycle:
            raise ConsiliumCycleNotFoundError(f"Medical cycle '{cycle_id}' not found.")
        if cycle.status != "active":
            raise ConsiliumError(
                f"Cannot run Weekly Consilium on non-active cycle '{cycle_id}' (status='{cycle.status}')."
            )

        resolved_date = (
            report_date.strip()
            if isinstance(report_date, str) and report_date.strip()
            else datetime.now(timezone.utc).strftime("%Y-%m-%d")
        )

        # Resolve target sprint metadata before DB writes
        existing_sprints = self.sprint_repo.list_by_cycle(cycle_id)
        if sprint_id is not None:
            target_sprint = self.sprint_repo.get(sprint_id)
            if target_sprint is not None and target_sprint.cycle_id != cycle_id:
                raise ConsiliumError(
                    f"Sprint '{sprint_id}' belongs to cycle '{target_sprint.cycle_id}', not '{cycle_id}'."
                )
            resolved_sprint_id = sprint_id
            resolved_sprint_number = (
                target_sprint.sprint_number
                if target_sprint is not None
                else len(existing_sprints) + 1
            )
        else:
            active_sprint = self.sprint_repo.get_active_for_cycle(cycle_id)
            if active_sprint is not None:
                resolved_sprint_id = active_sprint.sprint_id
                resolved_sprint_number = active_sprint.sprint_number
            else:
                resolved_sprint_number = len(existing_sprints) + 1
                resolved_sprint_id = f"sprint_{cycle_id}_w{resolved_sprint_number}"[:64]

        # 1. Load previous conclusions, active strategy, and 7-day Athena diary
        prev_ctx = self._load_previous_consilium_context(cycle_id)
        seven_day_logs = self.get_seven_day_athena_logs(
            cycle_id=cycle_id,
            sprint_id=resolved_sprint_id,
            reference_date=resolved_date,
        )
        athena_diary_str = self.format_athena_diary_for_consilium(seven_day_logs)

        # 2. STAGE 1: Execute Weekly Deputy Chief Physician (Gemini Flash)
        flash_prompt = build_weekly_flash_prompt(
            initial_context=prev_ctx["initial_context"],
            athena_diary=athena_diary_str,
        )
        flash_output: WeeklyFlashMasterOutput = await self.gateway.generate_structured(
            prompt=flash_prompt,
            response_schema=WeeklyFlashMasterOutput,
            model=GeminiModelRole.FLASH,
        )

        # 3. STAGE 2: Execute Weekly Chief Physician (Gemini Pro)
        previous_strategy_context = (
            f"Current Active Strategy:\n{prev_ctx['latest_pro_strategy']}\n\n"
            f"Clinical Background:\n{prev_ctx['latest_pro_training']}\n\n"
            f"Baseline & Current Input Context:\n{prev_ctx['initial_context']}"
        )
        draft_weekly_reports_dict = flash_output.user_system_reports.to_dict()
        pro_prompt = build_weekly_pro_prompt(
            flash_analysis=flash_output.weekly_deputy_analysis,
            athena_diary=athena_diary_str,
            previous_strategy_context=previous_strategy_context,
            draft_weekly_system_reports_json=json.dumps(
                draft_weekly_reports_dict, ensure_ascii=False
            ),
        )
        pro_output: WeeklyProMasterOutput = await self.gateway.generate_structured(
            prompt=pro_prompt,
            response_schema=WeeklyProMasterOutput,
            model=GeminiModelRole.PRO,
        )

        # 4. Chief Physician Quality Gate (BEFORE any database writes)
        if not pro_output.flash_quality_review.approved_for_publication:
            reason = (
                pro_output.flash_quality_review.rejection_reason
                or "Chief Physician (Pro) rejected publication of weekly user-facing reports."
            )
            raise ConsiliumQualityRejectionError(reason)

        # Apply Pro's deterministic corrections over Flash's 8 weekly system updates
        final_system_updates: Dict[str, Dict[str, Any]] = {
            k: dict(v) for k, v in draft_weekly_reports_dict.items()
        }
        for corrected_key, corrected_text in (
            pro_output.flash_quality_review.corrected_user_system_reports.items()
        ):
            if corrected_key not in SYSTEM_KEYS:
                raise ConsiliumQualityRejectionError(
                    f"Invalid system key '{corrected_key}' in Pro weekly quality review."
                )
            validated_text = validate_no_guaranteed_unchanged_claim(
                corrected_text,
                f"Corrected weekly summary for '{corrected_key}'",
            )
            final_system_updates[corrected_key]["summary"] = validated_text

        # Ensure all 8 systems are present and non-empty
        if set(final_system_updates.keys()) != set(SYSTEM_KEYS) or any(
            not isinstance(final_system_updates.get(k, {}).get("summary"), str)
            or not final_system_updates[k]["summary"].strip()
            for k in SYSTEM_KEYS
        ):
            raise ConsiliumQualityRejectionError(
                "Incomplete 8-system weekly updates after quality review; aborting publication."
            )

        # 5. Atomic single-transaction persistence
        try:
            # Ensure sprint row exists and mark it completed without closing the parent cycle
            if self.sprint_repo.get(resolved_sprint_id) is None:
                self.sprint_repo.create(
                    sprint_id=resolved_sprint_id,
                    cycle_id=cycle_id,
                    sprint_number=resolved_sprint_number,
                    status="active",
                    commit=False,
                )
            self.sprint_repo.complete_sprint(
                sprint_id=resolved_sprint_id,
                commit=False,
            )

            # Seed v1 of athena_active_strategy from Primary Consilium if not yet seeded
            if (
                self.report_repo.get_latest_version_number(
                    cycle_id=cycle_id,
                    report_type=ATHENA_ACTIVE_STRATEGY_REPORT_TYPE,
                    privacy_level=PrivacyLevel.SERVER_ONLY,
                )
                == 0
            ):
                self.report_repo.create_next_version(
                    cycle_id=cycle_id,
                    sprint_id=None,
                    model=self.gateway.pro_model,
                    report_type=ATHENA_ACTIVE_STRATEGY_REPORT_TYPE,
                    privacy_level=PrivacyLevel.SERVER_ONLY,
                    content={
                        "agent_role": AegisAgentRole.CHIEF_PHYSICIAN.value,
                        "source_consilium": "primary_consilium",
                        "strategy_status": "initial",
                        "strategy_version": 1,
                        "active_strategy_text": prev_ctx["primary_pro_general"],
                        "clinical_background_text": prev_ctx["primary_pro_training"],
                        "deputy_synthesis_text": prev_ctx["primary_flash_general"],
                        "daily_question_limit": 3,
                    },
                    commit=False,
                )

            # Save server_only Weekly Deputy Flash report
            self.report_repo.create_next_version(
                cycle_id=cycle_id,
                sprint_id=resolved_sprint_id,
                model=self.gateway.flash_model,
                report_type=WEEKLY_DEPUTY_INTERNAL_REPORT_TYPE,
                privacy_level=PrivacyLevel.SERVER_ONLY,
                content={
                    "agent_role": AegisAgentRole.DEPUTY_CHIEF_PHYSICIAN.value,
                    "sprint_id": resolved_sprint_id,
                    "sprint_number": resolved_sprint_number,
                    "weekly_deputy_analysis": flash_output.weekly_deputy_analysis,
                    "draft_user_system_reports": draft_weekly_reports_dict,
                },
                commit=False,
            )

            # Save server_only Weekly Chief Physician Pro report
            self.report_repo.create_next_version(
                cycle_id=cycle_id,
                sprint_id=resolved_sprint_id,
                model=self.gateway.pro_model,
                report_type=WEEKLY_CHIEF_INTERNAL_REPORT_TYPE,
                privacy_level=PrivacyLevel.SERVER_ONLY,
                content={
                    "agent_role": AegisAgentRole.CHIEF_PHYSICIAN.value,
                    "sprint_id": resolved_sprint_id,
                    "sprint_number": resolved_sprint_number,
                    "updated_weekly_strategy": pro_output.updated_weekly_strategy,
                    "strategy_status": pro_output.strategy_status,
                    "flash_quality_review": pro_output.flash_quality_review.model_dump(),
                },
                commit=False,
            )

            # Save new active Athena strategy version linked to cycle_id, sprint_id, and version
            next_strategy_ver = (
                self.report_repo.get_latest_version_number(
                    cycle_id=cycle_id,
                    report_type=ATHENA_ACTIVE_STRATEGY_REPORT_TYPE,
                    privacy_level=PrivacyLevel.SERVER_ONLY,
                )
                + 1
            )
            self.report_repo.create_next_version(
                cycle_id=cycle_id,
                sprint_id=resolved_sprint_id,
                model=self.gateway.pro_model,
                report_type=ATHENA_ACTIVE_STRATEGY_REPORT_TYPE,
                privacy_level=PrivacyLevel.SERVER_ONLY,
                content={
                    "agent_role": AegisAgentRole.CHIEF_PHYSICIAN.value,
                    "source_consilium": "weekly_consilium",
                    "strategy_status": pro_output.strategy_status,
                    "strategy_version": next_strategy_ver,
                    "sprint_id": resolved_sprint_id,
                    "sprint_number": resolved_sprint_number,
                    "active_strategy_text": pro_output.updated_weekly_strategy,
                    "clinical_background_text": (
                        "Deep correction of the Chief Physician's strategy based on weekly dynamics\n\n"
                        + prev_ctx["latest_pro_training"]
                    ),
                    "deputy_synthesis_text": flash_output.weekly_deputy_analysis,
                    "daily_question_limit": 3,
                },
                commit=False,
            )

            # Save 8 client_facing dated system reports for Atlas (preserving historical entries)
            client_system_reports: Dict[str, ClientModelReportResponse] = {}
            for system_key in SYSTEM_KEYS:
                sys_report_type = f"system_user_report_{system_key}"
                prior_versions = self.report_repo.list_versions(
                    cycle_id=cycle_id,
                    report_type=sys_report_type,
                    privacy_level=PrivacyLevel.CLIENT_FACING,
                )
                historical_entries: List[Dict[str, Any]] = []
                if prior_versions:
                    latest_prior_entries = (prior_versions[-1].content or {}).get("entries", [])
                    if isinstance(latest_prior_entries, list):
                        historical_entries = [dict(e) for e in latest_prior_entries if isinstance(e, dict)]

                verified_summary = final_system_updates[system_key]["summary"]
                has_changes = bool(final_system_updates[system_key]["has_changes"])
                new_dated_entry = {
                    "date": resolved_date,
                    "summary": verified_summary,
                    "has_changes": has_changes,
                }
                combined_entries = historical_entries + [new_dated_entry]

                saved_sys_report = self.report_repo.create_next_version(
                    cycle_id=cycle_id,
                    sprint_id=resolved_sprint_id,
                    model=self.gateway.flash_model,
                    report_type=sys_report_type,
                    privacy_level=PrivacyLevel.CLIENT_FACING,
                    content={
                        "title": f"{SYSTEM_TITLES[system_key]} Overview",
                        "summary": verified_summary,
                        "report_date": resolved_date,
                        "system_key": system_key,
                        "has_changes": has_changes,
                        "entries": combined_entries,
                    },
                    commit=False,
                )
                client_system_reports[system_key] = ClientModelReportResponse.model_validate(
                    saved_sys_report
                )

            # Save client_facing weekly overall report for the Client Vault
            saved_overall_report = self.report_repo.create_next_version(
                cycle_id=cycle_id,
                sprint_id=resolved_sprint_id,
                model=self.gateway.pro_model,
                report_type=WEEKLY_OVERALL_CLIENT_REPORT_TYPE,
                privacy_level=PrivacyLevel.CLIENT_FACING,
                content={
                    "title": pro_output.weekly_overall_report.title,
                    "summary": pro_output.weekly_overall_report.summary,
                    "report_date": resolved_date,
                    "highlights": list(pro_output.weekly_overall_report.highlights),
                    "metrics": {
                        "sprint_number": resolved_sprint_number,
                    },
                },
                commit=False,
            )
            validated_overall = ClientModelReportResponse.model_validate(saved_overall_report)

            self.db.commit()
        except Exception:
            self.db.rollback()
            raise

        return WeeklyConsiliumClientResponse(
            cycle_id=cycle_id,
            sprint_id=resolved_sprint_id,
            sprint_number=resolved_sprint_number,
            report_date=resolved_date,
            overall_report=validated_overall,
            system_reports=client_system_reports,
        )
