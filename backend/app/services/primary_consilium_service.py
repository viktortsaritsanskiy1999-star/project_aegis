from datetime import datetime, timezone
import json
from typing import Any, Dict, Optional
from sqlalchemy.orm import Session

from app.models.model_report import ModelReport
from app.repositories.cycle_repository import MedicalCycleRepository
from app.repositories.measurement_repository import HealthMeasurementRepository
from app.repositories.report_repository import ModelReportRepository
from app.schemas.consilium import (
    SYSTEM_KEYS,
    SYSTEM_TITLES,
    AegisAgentRole,
    AnamnesisModelOutput,
    SystemAgentOutput,
    FlashMasterOutput,
    ProMasterOutput,
    ClientVitalsThresholds,
    PrimaryConsiliumClientResponse,
)
from app.schemas.privacy import PrivacyLevel
from app.schemas.reports import ClientModelReportResponse
from app.services.consilium_prompts import (
    build_anamnesis_prompt,
    build_system_agent_prompt,
    build_flash_master_prompt,
    build_pro_master_prompt,
)
from app.services.gemini_gateway import GeminiGateway, GeminiModelRole


PREPARATORY_ANAMNESIS_REPORT_TYPE = "preparatory_anamnesis_m1"
PREPARATORY_OCR_REPORT_TYPE = "preparatory_vision_ocr"
PREPARATORY_ECG_REPORT_TYPE = "preparatory_vision_ecg"


class ConsiliumError(Exception):
    """Base exception for server-side Primary Consilium orchestration."""


class ConsiliumCycleNotFoundError(ConsiliumError):
    """Raised when the requested medical cycle does not exist."""


class ConsiliumContextMissingError(ConsiliumError):
    """
    Raised when required preparatory context (such as Anamnesis M1) is missing
    from server storage. Primary Consilium never re-runs Anamnesis or Vision internally.
    """


class ConsiliumQualityRejectionError(ConsiliumError):
    """
    Raised when Chief Physician (Pro) rejects publication of user-facing reports
    during the mandatory quality control review.
    """


class PrimaryConsiliumService:
    """
    Server-side orchestrator for the Primary Medical Consilium:
    8 Gemini Flash-Lite (Organ Specialists) -> Gemini Flash (Deputy Chief Physician) -> Gemini Pro (Chief Physician).

    Strictly separates logical agent roles (AegisAgentRole) from physical Gemini models
    and reads preparatory results (Anamnesis M1, Vision OCR, Vision ECG, Vitals)
    from server storage without re-running Anamnesis or Vision inside the consilium.
    """

    def __init__(self, db: Session, gateway: Optional[GeminiGateway] = None):
        self.db = db
        self.gateway = gateway or GeminiGateway()
        self.cycle_repo = MedicalCycleRepository(db)
        self.report_repo = ModelReportRepository(db)
        self.measurement_repo = HealthMeasurementRepository(db)

    # ==========================================================================
    # PREPARATORY CONTEXT HELPERS (STANDALONE — NEVER CALLED INSIDE CONSILIUM)
    # ==========================================================================

    async def run_preparatory_blind_anamnesis(
        self,
        cycle_id: str,
        raw_anamnesis_context: Dict[str, Any],
    ) -> ModelReport:
        """
        Standalone preparatory step for Anamnesis (M1, role='anamnesis_m1').
        Strips all laboratory fields to enforce strict blind mode, invokes Gemini Flash
        as the Anamnesis M1 agent, and stores the verified result as a server_only report.
        NOTE: Never called inside run_primary_consilium().
        """
        if not self.cycle_repo.get(cycle_id):
            raise ConsiliumCycleNotFoundError(f"Medical cycle '{cycle_id}' not found.")

        prompt = build_anamnesis_prompt(raw_anamnesis_context)
        parsed: AnamnesisModelOutput = await self.gateway.generate_structured(
            prompt=prompt,
            response_schema=AnamnesisModelOutput,
            model=GeminiModelRole.FLASH,
        )
        return self.save_preparatory_anamnesis_result(
            cycle_id=cycle_id,
            anamnesis_output=parsed,
        )

    def save_preparatory_anamnesis_result(
        self,
        cycle_id: str,
        anamnesis_output: AnamnesisModelOutput | Dict[str, Any],
    ) -> ModelReport:
        """
        Stores an already-verified Anamnesis (M1) result as a server_only report
        tagged with logical role AegisAgentRole.ANAMNESIS_M1.
        """
        if not self.cycle_repo.get(cycle_id):
            raise ConsiliumCycleNotFoundError(f"Medical cycle '{cycle_id}' not found.")

        validated = (
            anamnesis_output
            if isinstance(anamnesis_output, AnamnesisModelOutput)
            else AnamnesisModelOutput.model_validate(anamnesis_output)
        )
        payload = validated.model_dump()
        payload["agent_role"] = AegisAgentRole.ANAMNESIS_M1.value

        return self.report_repo.create_next_version(
            cycle_id=cycle_id,
            model=self.gateway.flash_model,
            report_type=PREPARATORY_ANAMNESIS_REPORT_TYPE,
            privacy_level=PrivacyLevel.SERVER_ONLY,
            content=payload,
        )

    def save_preparatory_ocr_labs(
        self,
        cycle_id: str,
        shared_labs_table: Dict[str, Any],
    ) -> ModelReport:
        """
        Stores verified Vision OCR extracted laboratory table as a server_only report
        tagged with logical role AegisAgentRole.VISION_OCR.
        """
        if not self.cycle_repo.get(cycle_id):
            raise ConsiliumCycleNotFoundError(f"Medical cycle '{cycle_id}' not found.")

        return self.report_repo.create_next_version(
            cycle_id=cycle_id,
            model=self.gateway.flash_model,
            report_type=PREPARATORY_OCR_REPORT_TYPE,
            privacy_level=PrivacyLevel.SERVER_ONLY,
            content={
                "agent_role": AegisAgentRole.VISION_OCR.value,
                "shared_labs_table": dict(shared_labs_table),
            },
        )

    def save_preparatory_ecg_summary(
        self,
        cycle_id: str,
        ecg_summary: str,
    ) -> ModelReport:
        """
        Stores verified Vision ECG parameter summary as a server_only report
        tagged with logical role AegisAgentRole.VISION_ECG.
        """
        if not self.cycle_repo.get(cycle_id):
            raise ConsiliumCycleNotFoundError(f"Medical cycle '{cycle_id}' not found.")

        return self.report_repo.create_next_version(
            cycle_id=cycle_id,
            model=self.gateway.flash_model,
            report_type=PREPARATORY_ECG_REPORT_TYPE,
            privacy_level=PrivacyLevel.SERVER_ONLY,
            content={
                "agent_role": AegisAgentRole.VISION_ECG.value,
                "ecg_summary": str(ecg_summary).strip() or "Not conducted",
            },
        )

    # ==========================================================================
    # INTERNAL CONTEXT LOADERS FROM SERVER STORAGE
    # ==========================================================================

    def _load_preparatory_anamnesis(self, cycle_id: str) -> AnamnesisModelOutput:
        reports = self.report_repo.list_server_only(
            cycle_id=cycle_id,
            report_type=PREPARATORY_ANAMNESIS_REPORT_TYPE,
        )
        if not reports:
            raise ConsiliumContextMissingError(
                f"Cannot run Primary Consilium for cycle '{cycle_id}': verified Anamnesis (M1) "
                f"report ('{PREPARATORY_ANAMNESIS_REPORT_TYPE}') is missing in server storage."
            )
        latest_content = dict(reports[0].content or {})
        stored_role = latest_content.pop("agent_role", None)
        if stored_role != AegisAgentRole.ANAMNESIS_M1.value:
            raise ConsiliumContextMissingError(
                f"Invalid agent_role '{stored_role}' on preparatory Anamnesis M1 report; "
                f"expected '{AegisAgentRole.ANAMNESIS_M1.value}'."
            )
        return AnamnesisModelOutput.model_validate(latest_content)

    def _load_preparatory_labs(self, cycle_id: str) -> Dict[str, Any]:
        reports = self.report_repo.list_server_only(
            cycle_id=cycle_id,
            report_type=PREPARATORY_OCR_REPORT_TYPE,
        )
        if not reports:
            return {}
        content = reports[0].content or {}
        if content.get("agent_role") != AegisAgentRole.VISION_OCR.value:
            raise ConsiliumContextMissingError(
                f"Invalid agent_role '{content.get('agent_role')}' on preparatory Vision OCR report; "
                f"expected '{AegisAgentRole.VISION_OCR.value}'."
            )
        labs = content.get("shared_labs_table")
        return dict(labs) if isinstance(labs, dict) else {}

    def _load_preparatory_ecg(self, cycle_id: str) -> str:
        reports = self.report_repo.list_server_only(
            cycle_id=cycle_id,
            report_type=PREPARATORY_ECG_REPORT_TYPE,
        )
        if not reports:
            return "Not conducted"
        content = reports[0].content or {}
        if content.get("agent_role") != AegisAgentRole.VISION_ECG.value:
            raise ConsiliumContextMissingError(
                f"Invalid agent_role '{content.get('agent_role')}' on preparatory Vision ECG report; "
                f"expected '{AegisAgentRole.VISION_ECG.value}'."
            )
        ecg = content.get("ecg_summary")
        return str(ecg).strip() if isinstance(ecg, str) and ecg.strip() else "Not conducted"

    def _load_latest_vitals_stream(self, cycle_id: str, ecg_summary: str) -> Dict[str, str]:
        measurements = self.measurement_repo.list_by_cycle(cycle_id=cycle_id)
        systolic: Optional[int] = None
        diastolic: Optional[int] = None
        pulse: Optional[int] = None
        spo2: Optional[int] = None

        for m in reversed(measurements):
            if systolic is None and m.systolic is not None:
                systolic = m.systolic
            if diastolic is None and m.diastolic is not None:
                diastolic = m.diastolic
            if pulse is None and m.pulse is not None:
                pulse = m.pulse
            if spo2 is None and m.spo2 is not None:
                spo2 = m.spo2
            if all(v is not None for v in (systolic, diastolic, pulse, spo2)):
                break

        return {
            "blood_pressure": f"{systolic if systolic is not None else '--'}/{diastolic if diastolic is not None else '--'} mmHg",
            "heart_rate": f"{pulse if pulse is not None else '--'} bpm",
            "spo2": f"{spo2 if spo2 is not None else '--'}%",
            "ecg_summary": ecg_summary or "Not conducted",
        }

    @staticmethod
    def _assemble_context_for_flash(
        anamnesis: AnamnesisModelOutput,
        shared_labs_table: Dict[str, Any],
        vitals_stream: Dict[str, str],
        system_outputs: Dict[str, SystemAgentOutput],
    ) -> str:
        """
        Mirrors VaultService.getAggregatedContextForMaster() from Flutter.
        """
        lines = [
            "=== ANAMNESIS AND INPUT DATA ===",
            json.dumps(
                {
                    "user_view": anamnesis.user_view.model_dump(),
                    "training_sandbox": anamnesis.training_sandbox.model_dump(),
                    "shared_labs_shelf": shared_labs_table,
                },
                ensure_ascii=False,
            ),
            "",
            "=== VITAL MARKERS (SMART WATCH / LIVE) ===",
            f"Blood pressure: {vitals_stream['blood_pressure']}",
            f"Heart rate (Pulse): {vitals_stream['heart_rate']}",
            f"Oxygen saturation (SpO2): {vitals_stream['spo2']}",
            f"ECG status: {vitals_stream['ecg_summary']}",
            "",
            "=== CONCLUSIONS OF 8 ORGAN SYSTEM AGENTS ===",
        ]
        for key in SYSTEM_KEYS:
            sys_out = system_outputs[key]
            lines.append(f"--- BLOCK: {key.upper()} ---")
            lines.append(f"Main conclusion: {sys_out.system_logic.system_general_shelf}")
            lines.append(f"Training log: {sys_out.training_sandbox.system_training_shelf}")
            lines.append("")
        return "\n".join(lines)

    @staticmethod
    def _assemble_context_for_pro(
        initial_master_context: str,
        flash_output: FlashMasterOutput,
    ) -> str:
        """
        Mirrors VaultService.getAggregatedContextForMasterWithFlash() and includes
        Flash's 8 draft user_system_reports so Chief Physician (Pro) can audit and correct them.
        """
        lines = [
            initial_master_context,
            "=== SYNTHESIS (GEMINI FLASH) ===",
            "Main conclusion:",
            flash_output.flash_general_shelf,
            "",
            "Training output:",
            flash_output.flash_training_shelf,
            "",
            "=== DRAFT 8 USER SYSTEM REPORTS FROM DEPUTY FLASH (FOR PRO QUALITY REVIEW) ===",
            json.dumps(flash_output.user_system_reports.to_dict(), ensure_ascii=False),
            "",
        ]
        return "\n".join(lines)

    # ==========================================================================
    # PRIMARY CONSILIUM ORCHESTRATION (8 FLASH-LITE -> DEPUTY FLASH -> PRO)
    # ==========================================================================

    async def run_primary_consilium(
        self,
        cycle_id: str,
        report_date: Optional[str] = None,
    ) -> PrimaryConsiliumClientResponse:
        """
        Executes the server-side Primary Consilium for `cycle_id`:
        1. Reads saved preparatory context (Anamnesis M1, Vision OCR labs, Vision ECG, Vitals).
           Never invokes Anamnesis M1 or Vision internally.
        2. Sequentially runs the 8 Flash-Lite organ specialists.
        3. Runs Deputy Chief Physician (Gemini Flash) for intersystem synthesis, audit,
           and 8 draft user_system_reports.
        4. Runs Chief Physician (Gemini Pro) for final clinical strategy (Athena handoff),
           personal heart rate thresholds, user_overall_report, and quality control of Flash's
           8 user_system_reports.
        5. Enforces Pro's quality gate: if approved_for_publication is False, aborts before
           any client_facing report is created. Otherwise applies Pro's corrections over Flash's
           draft user_system_reports and persists versioned server_only + client_facing reports
           inside a single atomic database transaction.
        """
        cycle = self.cycle_repo.get(cycle_id)
        if not cycle:
            raise ConsiliumCycleNotFoundError(f"Medical cycle '{cycle_id}' not found.")

        resolved_date = (
            report_date.strip()
            if isinstance(report_date, str) and report_date.strip()
            else datetime.now(timezone.utc).strftime("%Y-%m-%d")
        )

        # 1. Load already-saved preparatory results from DB (never re-run M1 or Vision here)
        anamnesis_m1 = self._load_preparatory_anamnesis(cycle_id)
        shared_labs_table = self._load_preparatory_labs(cycle_id)
        ecg_summary = self._load_preparatory_ecg(cycle_id)
        vitals_stream = self._load_latest_vitals_stream(cycle_id, ecg_summary)

        # 2. Execute 8 Organ Specialists (Gemini Flash-Lite)
        system_outputs: Dict[str, SystemAgentOutput] = {}
        for system_key in SYSTEM_KEYS:
            organ_fact = getattr(anamnesis_m1.system_logic, system_key)
            input_package = {
                "system_key": system_key,
                "facts": organ_fact.data,
                "first_model_short_summary": organ_fact.internal_comment,
                "shared_labs_table": shared_labs_table,
                "vitals_stream": vitals_stream,
            }
            system_prompt = build_system_agent_prompt(system_key, input_package)
            organ_result: SystemAgentOutput = await self.gateway.generate_structured(
                prompt=system_prompt,
                response_schema=SystemAgentOutput,
                model=GeminiModelRole.FLASH_LITE,
            )
            system_outputs[system_key] = organ_result

        # 3. Execute Deputy Chief Physician (Gemini Flash)
        initial_master_context = self._assemble_context_for_flash(
            anamnesis=anamnesis_m1,
            shared_labs_table=shared_labs_table,
            vitals_stream=vitals_stream,
            system_outputs=system_outputs,
        )
        flash_prompt = build_flash_master_prompt(initial_master_context)
        flash_output: FlashMasterOutput = await self.gateway.generate_structured(
            prompt=flash_prompt,
            response_schema=FlashMasterOutput,
            model=GeminiModelRole.FLASH,
        )

        # 4. Execute Chief Physician (Gemini Pro)
        full_master_context = self._assemble_context_for_pro(
            initial_master_context=initial_master_context,
            flash_output=flash_output,
        )
        pro_prompt = build_pro_master_prompt(full_master_context)
        pro_output: ProMasterOutput = await self.gateway.generate_structured(
            prompt=pro_prompt,
            response_schema=ProMasterOutput,
            model=GeminiModelRole.PRO,
        )

        # 5. Chief Physician Quality Gate (BEFORE any client_facing report is created)
        if not pro_output.flash_quality_review.approved_for_publication:
            reason = (
                pro_output.flash_quality_review.rejection_reason
                or "Chief Physician (Pro) rejected publication of user-facing reports."
            )
            raise ConsiliumQualityRejectionError(reason)

        # Deterministically apply Pro's corrections over Flash's 8 draft user system reports
        final_user_system_texts: Dict[str, str] = flash_output.user_system_reports.to_dict()
        for corrected_key, corrected_text in (
            pro_output.flash_quality_review.corrected_user_system_reports.items()
        ):
            if corrected_key not in SYSTEM_KEYS:
                raise ConsiliumQualityRejectionError(
                    f"Invalid system key '{corrected_key}' in Pro quality review."
                )
            if not isinstance(corrected_text, str) or not corrected_text.strip():
                raise ConsiliumQualityRejectionError(
                    f"Empty corrected report text for system '{corrected_key}'."
                )
            final_user_system_texts[corrected_key] = corrected_text.strip()

        # Verify completeness of all 8 user system reports before staging DB writes
        if set(final_user_system_texts.keys()) != set(SYSTEM_KEYS) or any(
            not isinstance(final_user_system_texts.get(k), str)
            or not final_user_system_texts[k].strip()
            for k in SYSTEM_KEYS
        ):
            raise ConsiliumQualityRejectionError(
                "Incomplete 8-system user reports after quality review; aborting publication."
            )

        # 6. Atomic single-transaction persistence of all 10 server_only + 9 client_facing reports
        try:
            for system_key in SYSTEM_KEYS:
                organ_out = system_outputs[system_key]
                agent_role = AegisAgentRole.for_system_key(system_key)
                self.report_repo.create_next_version(
                    cycle_id=cycle_id,
                    model=self.gateway.flash_lite_model,
                    report_type=agent_role.value,
                    privacy_level=PrivacyLevel.SERVER_ONLY,
                    content={
                        "agent_role": agent_role.value,
                        "system_key": system_key,
                        "system_view": organ_out.system_view.model_dump(),
                        "system_logic": organ_out.system_logic.model_dump(),
                        "training_sandbox": organ_out.training_sandbox.model_dump(),
                    },
                    commit=False,
                )

            self.report_repo.create_next_version(
                cycle_id=cycle_id,
                model=self.gateway.flash_model,
                report_type="deputy_chief_physician_internal",
                privacy_level=PrivacyLevel.SERVER_ONLY,
                content={
                    "agent_role": AegisAgentRole.DEPUTY_CHIEF_PHYSICIAN.value,
                    "flash_general_shelf": flash_output.flash_general_shelf,
                    "flash_training_shelf": flash_output.flash_training_shelf,
                    "draft_user_system_reports": flash_output.user_system_reports.to_dict(),
                },
                commit=False,
            )

            self.report_repo.create_next_version(
                cycle_id=cycle_id,
                model=self.gateway.pro_model,
                report_type="chief_physician_internal",
                privacy_level=PrivacyLevel.SERVER_ONLY,
                content={
                    "agent_role": AegisAgentRole.CHIEF_PHYSICIAN.value,
                    "pro_general_shelf": pro_output.pro_general_shelf,
                    "pro_training_shelf": pro_output.pro_training_shelf,
                    "vitals_thresholds": pro_output.vitals_thresholds.model_dump(),
                    "flash_quality_review": pro_output.flash_quality_review.model_dump(),
                },
                commit=False,
            )

            client_system_reports: Dict[str, ClientModelReportResponse] = {}
            for system_key in SYSTEM_KEYS:
                verified_summary = final_user_system_texts[system_key]
                system_title = f"{SYSTEM_TITLES[system_key]} Overview"
                saved_sys_report = self.report_repo.create_next_version(
                    cycle_id=cycle_id,
                    model=self.gateway.flash_model,
                    report_type=f"system_user_report_{system_key}",
                    privacy_level=PrivacyLevel.CLIENT_FACING,
                    content={
                        "title": system_title,
                        "summary": verified_summary,
                        "report_date": resolved_date,
                        "system_key": system_key,
                        "has_changes": True,
                        "entries": [
                            {
                                "date": resolved_date,
                                "summary": verified_summary,
                                "has_changes": True,
                            }
                        ],
                    },
                    commit=False,
                )
                client_system_reports[system_key] = ClientModelReportResponse.model_validate(
                    saved_sys_report
                )

            saved_overall_report = self.report_repo.create_next_version(
                cycle_id=cycle_id,
                model=self.gateway.pro_model,
                report_type="user_overall_report",
                privacy_level=PrivacyLevel.CLIENT_FACING,
                content={
                    "title": pro_output.user_overall_report.title,
                    "summary": pro_output.user_overall_report.summary,
                    "report_date": resolved_date,
                    "highlights": list(pro_output.user_overall_report.highlights),
                    "metrics": {
                        "min_pessimum_bpm": pro_output.vitals_thresholds.min_pessimum_bpm,
                        "max_pessimum_bpm": pro_output.vitals_thresholds.max_pessimum_bpm,
                        "max_spike_delta_bpm": pro_output.vitals_thresholds.max_spike_delta_bpm,
                    },
                },
                commit=False,
            )
            validated_overall = ClientModelReportResponse.model_validate(saved_overall_report)

            self.db.commit()
        except Exception:
            self.db.rollback()
            raise

        return PrimaryConsiliumClientResponse(
            cycle_id=cycle_id,
            report_date=resolved_date,
            overall_report=validated_overall,
            system_reports=client_system_reports,
            vitals_thresholds=ClientVitalsThresholds(
                min_pessimum_bpm=pro_output.vitals_thresholds.min_pessimum_bpm,
                max_pessimum_bpm=pro_output.vitals_thresholds.max_pessimum_bpm,
                max_spike_delta_bpm=pro_output.vitals_thresholds.max_spike_delta_bpm,
            ),
        )
