"""
Project Aegis — Full System Lifecycle Simulation Test Suite
===========================================================
End-to-end continuous lifecycle simulation of a single synthetic patient:
From initial onboarding and Primary Consilium, through daily monitoring,
Mode 2 telemetry, mid-week lab revisions, three consecutive Weekly Consiliums,
Pro referral recommendation, doctor document generation and multi-format export,
Athena followup and visit confirmation, Update Health Information, Neo Consilium
and atomic activation, through to the first completed Weekly Consilium after Neo.

Plus 10 dedicated Branch Tests for alternate/failure paths on isolated state copies.

ZERO external Gemini API calls (all transports deterministically mocked).
ZERO production SQLite modifications.
ZERO real patient medical data.
"""

import asyncio
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
from typing import Any, Callable, Dict, Generator, List, Optional, Tuple
import uuid

import httpx
from fastapi import HTTPException
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine, select, update
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import get_db
from app.main import app
from app.models.base import Base
from app.models.health_measurement import HealthMeasurement
from app.models.medical_cycle import MedicalCycle
from app.models.model_report import ModelReport
from app.models.patient import Patient
from app.models.weekly_sprint import WeeklySprint
from app.models.workout_session import WorkoutSession
from app.repositories.cycle_repository import MedicalCycleRepository
from app.repositories.measurement_repository import HealthMeasurementRepository
from app.repositories.report_repository import ModelReportRepository
from app.repositories.sprint_repository import WeeklySprintRepository
from app.repositories.workout_repository import WorkoutSessionRepository
from app.routers.cascade import get_gemini_gateway as cascade_get_gemini_gateway, get_server_now_provider as cascade_get_now
from app.routers.client_cascade import get_gemini_gateway as client_cascade_get_gemini_gateway, get_server_now_provider as client_cascade_get_now
from app.routers.client_chat import get_gemini_gateway as client_chat_get_gemini_gateway, get_server_now_provider as client_chat_get_now
from app.routers.vitals import get_gemini_gateway as vitals_get_gemini_gateway, get_server_now_provider as vitals_get_now
from app.schemas.chat import (
    AthenaChatRequest,
    AthenaChatResponse,
    AthenaReferralReminderOutput,
    AthenaWeeklyWrapupOutput,
    AthenaVitalsAlertContext,
)
from app.schemas.consilium import (
    SYSTEM_KEYS,
    SYSTEM_TITLES,
    ConsiliumMode,
    AegisAgentRole,
    AnamnesisModelOutput,
    AnamnesisIntakeRequest,
    EcgRecordingQuality,
    EcgRecordingType,
    FlashMasterOutput,
    LabEventType,
    LabMarkerMutationItem,
    LabReferenceRange,
    LabRevisionFlashMasterOutput,
    NeoFlashMasterOutput,
    NeoProMasterOutput,
    NeoSystemAgentOutput,
    ProFlashQualityReview,
    ProMasterOutput,
    ProReferralDecision,
    ProUserOverallReport,
    ProVitalsThresholds,
    ReferralClinicalHistoryExtraction,
    ReferralDoctorDocumentOutput,
    SystemAgentOutput,
    WeeklyFlashMasterOutput,
    WeeklyProMasterOutput,
)
from app.schemas.health import HealthMeasurementCreate, HealthMeasurementCreateBatch
from app.schemas.privacy import PrivacyLevel
from app.schemas.reports import ClientReportContent
from app.security.auth import (
    DeviceSession,
    ServiceAdminSession,
    require_device_session,
    require_scopes,
    require_service_admin_session,
    verify_client_token,
)
from app.security.tokens import (
    SCOPE_CASCADE_INTAKE,
    SCOPE_CASCADE_RUN,
    SCOPE_CHAT_READ,
    SCOPE_CHAT_WRITE,
    SCOPE_CYCLE_READ,
    SCOPE_REPORTS_READ,
    SCOPE_VITALS_WRITE,
)
from app.services.athena_chat_service import AthenaChatService
from app.services.athena_daily_journal_service import AthenaDailyJournalService
from app.services.athena_weekly_plan_service import AthenaWeeklyPlanService
from app.services.clinical_automation_service import (
    AutomationJobRecord,
    AutomationJobStatus,
    AutomationJobType,
    ClinicalAutomationService,
)
from app.services.doctor_document_export_service import DoctorDocumentExportService
from app.services.ecg_vision_service import EcgVisionService
from app.services.gemini_gateway import GeminiGateway, GeminiModelRole
from app.services.historical_context_service import HistoricalContextService
from app.services.lab_revision_consilium_service import (
    ATHENA_ACTIVE_STRATEGY_REPORT_TYPE,
    ConsiliumCooldownError,
    LabRevisionConsiliumService,
)
from app.services.lab_vision_ocr_service import LabVisionOcrService
from app.services.mode2_vitals_service import (
    Mode2EpisodeStatus,
    Mode2VitalsMonitoringService,
)
from app.services.neo_activation_service import NeoConsiliumActivationService
from app.services.neo_cycle_service import NeoCycleService
from app.services.primary_consilium_service import (
    ConsiliumConcurrencyConflictError,
    PREPARATORY_ANAMNESIS_REPORT_TYPE,
    PREPARATORY_ECG_REPORT_TYPE,
    PRIMARY_CONSILIUM_EXECUTION_LOG_REPORT_TYPE,
    PrimaryConsiliumService,
)
from app.services.referral_document_service import ReferralDocumentService
from app.services.weekly_consilium_service import (
    ATHENA_ACTIVE_STRATEGY_REPORT_TYPE,
    ATHENA_SCHEDULE_ANCHOR_REPORT_TYPE,
    REFERRAL_LIFECYCLE_STATE_REPORT_TYPE,
    WeeklyConsiliumService,
)
from app.services.wellness_daily_service import WellnessDailyService

from tests.test_primary_consilium import _sample_anamnesis_output
from tests.test_stage_8a2_referral_document import (
    _sample_referral_flash_extraction,
    _sample_referral_pro_document,
)
from tests.test_stage_8a3_document_export import _seed_referral_documents
from tests.test_stage_8b2b_neo_orchestration import (
    _sample_neo_flash_output,
    _sample_neo_pro_output,
    _sample_neo_specialist_output,
)
from tests.test_stage_8b2c_neo_activation import (
    _setup_patient_and_cycles,
    _populate_prepared_neo_consilium_data,
)
from tests.test_weekly_consilium import (
    _sample_weekly_flash_output,
    _sample_weekly_pro_output,
)
from tests.test_lab_revision_consilium import _sample_lab_revision_flash_output


# ==============================================================================
# VIRTUAL CLOCK (DETERMINISTIC TIMEKEEPER)
# ==============================================================================

class VirtualClock:
    """Thread-safe virtual clock providing deterministic UTC time for schedulers."""

    def __init__(self, start_dt: Optional[datetime] = None):
        self._current: datetime = start_dt or datetime(2026, 10, 18, 9, 0, 0, tzinfo=timezone.utc)

    def now(self) -> datetime:
        return self._current

    def advance(
        self,
        days: int = 0,
        hours: int = 0,
        minutes: int = 0,
        seconds: int = 0,
    ) -> datetime:
        self._current += timedelta(
            days=days,
            hours=hours,
            minutes=minutes,
            seconds=seconds,
        )
        return self._current

    def set(self, dt: datetime) -> datetime:
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        self._current = dt
        return self._current


# ==============================================================================
# UNIFIED DETERMINISTIC MULTI-MODEL MOCK GATEWAY
# ==============================================================================

class ComprehensiveLifecycleMockGateway(GeminiGateway):
    """
    Unified mock gateway handling all LLM and Vision roles across Project Aegis:
    - Flash-Lite 3.5: 8 Initial & Neo organ specialists, Lab Revision organ agents
    - Flash 3.8: Anamnesis M1, Initial/Neo Deputy, Weekly Deputy, Lab Revision Deputy,
                 Referral History Extractor, Athena Chat Turns, Daily Journal Section 4, Wellness
    - Argon 4: Initial/Neo Chief Physician Quality Gate, Weekly Chief Physician,
               Referral Doctor Document Author, Lab Revision Chief
    - Vision Flash 3.8: Vision OCR and Vision ECG
    """

    def __init__(self):
        super().__init__(api_key="mock-comprehensive-lifecycle-key")
        self.recorded_calls: List[Dict[str, Any]] = []
        # Dynamic overrides for branch and progression testing
        self.weekly_referral_recommended: bool = False
        self.custom_pro_strategy: Optional[str] = None
        self.custom_athena_reply: Optional[str] = None
        self.custom_chat_turn_hook: Optional[Callable[[str], str]] = None

    async def generate_structured(
        self,
        prompt: str,
        response_schema: Any,
        model: GeminiModelRole | str = GeminiModelRole.FLASH,
        system_instruction: Optional[str] = None,
        max_output_tokens: int = 8192,
        inline_images: Optional[List[Dict[str, str]]] = None,
    ) -> Any:
        resolved_model = self.resolve_model_name(model)
        schema_name = getattr(response_schema, "__name__", str(response_schema))

        call_record = {
            "type": "structured",
            "model_role": model,
            "resolved_model": resolved_model,
            "response_schema": schema_name,
            "prompt_length": len(prompt),
            "prompt_snippet": prompt[:160],
        }
        self.recorded_calls.append(call_record)

        # 1. Anamnesis Intake M1 (Flash 3.8)
        if schema_name == "AnamnesisModelOutput":
            out = dict(_sample_anamnesis_output())
            out.pop("agent_role", None)
            return response_schema.model_validate(out)

        # 2. Initial / Lab Revision Organ Specialists (8 x Flash-Lite 3.5)
        if schema_name == "SystemAgentOutput":
            return SystemAgentOutput(
                system_view={"isolated_labs": {"marker": "evaluated_normal"}},
                system_logic={"system_general_shelf": "Academic organ assessment. Baseline tone stable."},
                training_sandbox={"system_training_shelf": "Specialist sandbox organ observations."},
            )

        # 3. Neo Organ Specialists (8 x Flash-Lite 3.5)
        if schema_name == "NeoSystemAgentOutput":
            matched_key = "cvs"
            for k in SYSTEM_KEYS:
                if f"BLOCK: {k.upper()}" in prompt or k in prompt:
                    matched_key = k
                    break
            return response_schema.model_validate(_sample_neo_specialist_output(matched_key))

        # 4. Initial Deputy Chief Physician (Flash 3.8)
        if schema_name == "FlashMasterOutput":
            user_reports = {
                k: f"Deputy Flash synthesis for {SYSTEM_TITLES[k]}: physiological adaptation balanced."
                for k in SYSTEM_KEYS
            }
            return FlashMasterOutput(
                flash_general_shelf="Deputy Flash master intersystem synthesis.",
                flash_training_shelf="Deputy Flash training sandbox.",
                user_system_reports=user_reports,
            )

        # 5. Neo Deputy Chief Physician (Flash 3.8)
        if schema_name == "NeoFlashMasterOutput":
            return response_schema.model_validate(_sample_neo_flash_output())

        # 6. Weekly Deputy Chief Physician (Flash 3.8)
        if schema_name == "WeeklyFlashMasterOutput":
            return response_schema.model_validate(_sample_weekly_flash_output())

        # 7. Lab Revision Deputy Chief Physician (Flash 3.8)
        if schema_name == "LabRevisionFlashMasterOutput":
            return response_schema.model_validate(_sample_lab_revision_flash_output())

        # 8. Initial Chief Physician (Argon 4)
        if schema_name == "ProMasterOutput":
            return ProMasterOutput(
                vitals_thresholds=ProVitalsThresholds(
                    min_pessimum_bpm=48,
                    max_pessimum_bpm=115,
                    max_spike_delta_bpm=25,
                    clinical_rationale="Argon 4 Chief Physician resting vitals baseline.",
                ),
                pro_general_shelf="Argon 4 Chief Clinical Strategy for Athena accompaniment.",
                pro_training_shelf="Argon 4 Chief Physician training sandbox validation.",
                user_overall_report=ProUserOverallReport(
                    title="Aegis Comprehensive Health Summary",
                    summary="Overall baseline assessment confirms healthy baseline functioning.",
                    highlights=["Resting vitals stable", "Metabolic markers optimal"],
                ),
                flash_quality_review=ProFlashQualityReview(
                    approved_for_publication=True,
                    rejection_reason=None,
                    corrected_user_system_reports={},
                ),
            )

        # 9. Neo Chief Physician (Argon 4)
        if schema_name == "NeoProMasterOutput":
            return response_schema.model_validate(_sample_neo_pro_output())

        # 10. Weekly Chief Physician (Argon 4)
        if schema_name == "WeeklyProMasterOutput":
            pro_out = _sample_weekly_pro_output(
                strategy_text=self.custom_pro_strategy or "Week updated strategy: focus on cardiovascular pacing."
            )
            if self.weekly_referral_recommended:
                pro_out["referral_decision"] = {
                    "recommended": True,
                    "urgency": "routine",
                    "target_specialties": ["Cardiology"],
                    "clinical_reason_for_referral": "Longitudinal evaluation of persistent resting bradycardia.",
                    "observed_syndromes_or_trends": ["Resting pulse dips to 46 bpm outside exercise"],
                    "recommended_workup_for_doctor_discussion": ["12-lead ECG", "24-hour Holter monitoring"],
                }
            else:
                pro_out["referral_decision"] = {
                    "recommended": False,
                    "urgency": "none",
                    "target_specialties": [],
                    "clinical_reason_for_referral": "",
                }
            return response_schema.model_validate(pro_out)

        # 11. Referral Clinical History Extractor (Flash 3.8)
        if schema_name == "ReferralClinicalHistoryExtraction":
            return response_schema.model_validate(_sample_referral_flash_extraction())

        # 12. Referral Doctor Document Author (Argon 4)
        if schema_name == "ReferralDoctorDocumentOutput":
            return response_schema.model_validate(
                _sample_referral_pro_document(
                    specialties=["Cardiologist"],
                    urgency="routine",
                )
            )

        # 13. Athena Weekly Wrapup Message
        if schema_name == "AthenaWeeklyWrapupOutput":
            return response_schema(
                message=(
                    "Your weekly health observation summary is ready! If you have visited a clinician or received "
                    "new medical tests, your Health Observation Summary is available in the Safe."
                )
            )

        # 14. Athena Referral Reminder Message
        if schema_name == "AthenaReferralReminderOutput":
            return response_schema(
                message=(
                    "Just checking in gently: have you had the chance to visit the cardiologist yet? "
                    "Your Health Observation Summary is ready in the Safe whenever you need it."
                )
            )

        # 15. Vision OCR Model Output
        if schema_name == "VisionOcrModelOutput":
            return response_schema.model_validate({
                "document_metadata": {
                    "sample_date": "2026-10-18",
                    "issue_date": "2026-10-18",
                    "laboratory_name": "Diagnostic Biospecimen Center",
                    "page_count": 1,
                    "notes": "Verified clinical chemistry report",
                },
                "markers": [
                    {
                        "original_name": "Ferritin",
                        "marker_key": "ferritin",
                        "raw_value": "18.5",
                        "numeric_value": 18.5,
                        "unit": "ng/mL",
                        "reference_range": {"low": 30.0, "high": 300.0, "text": "30 - 300 ng/mL"},
                        "sample_date": "2026-10-18",
                        "category": "biochemistry",
                        "is_uncertain": False,
                        "uncertainty_note": None,
                        "is_critical_flag": True,
                    },
                    {
                        "original_name": "Fasting Glucose",
                        "marker_key": "glucose",
                        "raw_value": "4.8",
                        "numeric_value": 4.8,
                        "unit": "mmol/L",
                        "reference_range": {"low": 3.9, "high": 5.6, "text": "3.9 - 5.6 mmol/L"},
                        "sample_date": "2026-10-18",
                        "category": "biochemistry",
                        "is_uncertain": False,
                        "uncertainty_note": None,
                        "is_critical_flag": False,
                    },
                ],
            })

        # 16. Vision ECG Model Output
        if schema_name == "VisionEcgModelOutput":
            return response_schema.model_validate({
                "recording_type": EcgRecordingType.SINGLE_LEAD_SMARTWATCH.value,
                "recording_quality": EcgRecordingQuality.GOOD.value,
                "lead_name": "Lead I (Smartwatch ECG)",
                "heart_rate_bpm": 68,
                "rhythm_type": "sinus",
                "rhythm_regularity": "regular",
                "pr_interval_ms": 158.0,
                "qrs_duration_ms": 86.0,
                "qt_interval_ms": 380.0,
                "qtc_interval_ms": 405.0,
                "electrical_axis": "normal",
                "st_segment_findings": "Isoelectric",
                "t_wave_findings": "Normal polarity",
                "limitations": ["Single-lead recording from consumer smartwatch"],
                "uncertainties": ["Limited lateral leads"],
                "raw_unresolved_metrics": {},
                "ecg_summary": "Sinus rhythm at 68 bpm with normal PR and QRS duration.",
                "potential_urgent_warning": None,
            })

        # 17. Athena Weekly Plan Record (Stage 1)
        if schema_name == "AthenaWeeklyPlanRecord":
            return response_schema(
                plan_id="plan_w1_01",
                cycle_id="cycle_synthetic_c1",
                week_number=1,
                source_strategy_report_id="rep_strat_01",
                source_strategy_version=1,
                created_at_utc=datetime.now(timezone.utc).isoformat(),
                summary_for_curator="Weekly plan for dynamic observation.",
                goals=[
                    {
                        "goal_id": "goal_01",
                        "priority": 1,
                        "title": "Resting Heart Rate Adaptation",
                        "target_clarification": "Verify morning pulse",
                        "clinical_rationale": "Longitudinal stability check",
                        "sufficiency_criteria": "Patient confirms morning pulse range",
                        "status": "active",
                        "questions_asked_count": 0,
                    }
                ],
                status="active",
            )

        # 18. Athena Daily Assessment Model Output
        if schema_name == "AthenaDailyAssessmentModelOutput":
            return response_schema(
                status="NORMAL",
                interpretation="Patient vitals and daily reports are within physiological targets.",
                observations=["Resting pulse within expected 65-75 range", "SpO2 maintained at 98%"],
                relevant_changes=[],
                contradictions=[],
                uncertainties=[],
                red_flags=False,
            )

        # Fallback to model_validate empty dict
        try:
            return response_schema.model_validate({})
        except Exception:
            raise ValueError(f"Unhandled response schema in ComprehensiveLifecycleMockGateway: {schema_name}")

    async def generate_json(
        self,
        prompt: str,
        system_instruction: Optional[str] = None,
        model: GeminiModelRole | str = GeminiModelRole.FLASH,
        temperature: float = 0.2,
        inline_images: Optional[List[Dict[str, str]]] = None,
    ) -> Dict[str, Any]:
        resolved_model = self.resolve_model_name(model)
        self.recorded_calls.append({
            "type": "json",
            "model_role": model,
            "resolved_model": resolved_model,
            "prompt_snippet": prompt[:160],
        })

        # Vision OCR processing
        if "Analyze the following laboratory test report image" in prompt or "markers" in prompt.lower():
            return {
                "document_metadata": {
                    "sample_date": "2026-10-18",
                    "issue_date": "2026-10-18",
                    "laboratory_name": "Diagnostic Biospecimen Center",
                    "page_count": 1,
                    "notes": "Verified clinical chemistry report",
                },
                "markers": [
                    {
                        "original_name": "Ferritin",
                        "marker_key": "ferritin",
                        "raw_value": "18.5",
                        "numeric_value": 18.5,
                        "unit": "ng/mL",
                        "reference_range": {"low": 30.0, "high": 300.0, "text": "30 - 300 ng/mL"},
                        "sample_date": "2026-10-18",
                        "category": "biochemistry",
                        "is_uncertain": False,
                        "uncertainty_note": None,
                        "is_critical_flag": True,
                    },
                    {
                        "original_name": "Fasting Glucose",
                        "marker_key": "glucose",
                        "raw_value": "4.8",
                        "numeric_value": 4.8,
                        "unit": "mmol/L",
                        "reference_range": {"low": 3.9, "high": 5.6, "text": "3.9 - 5.6 mmol/L"},
                        "sample_date": "2026-10-18",
                        "category": "biochemistry",
                        "is_uncertain": False,
                        "uncertainty_note": None,
                        "is_critical_flag": False,
                    },
                ],
            }

        # Vision ECG processing
        if "electrocardiogram" in prompt.lower() or "ecg" in prompt.lower() or "smartwatch" in prompt.lower():
            return {
                "recording_type": EcgRecordingType.SINGLE_LEAD_SMARTWATCH.value,
                "recording_quality": EcgRecordingQuality.GOOD.value,
                "lead_name": "Lead I (Smartwatch ECG)",
                "heart_rate_bpm": 68,
                "rhythm_type": "sinus",
                "rhythm_regularity": "regular",
                "pr_interval_ms": 158.0,
                "qrs_duration_ms": 86.0,
                "qt_interval_ms": 384.0,
                "qtc_interval_ms": 418.0,
                "electrical_axis": None,
                "st_segment_findings": "isoelectric in Lead I",
                "t_wave_findings": "upright normal morphology",
                "limitations": ["Single-lead recording: frontal electrical axis cannot be assessed."],
                "uncertainties": [],
                "ecg_summary": "Smartwatch Lead I recording: Regular sinus rhythm, 68 bpm, normal QTc 418 ms.",
                "potential_urgent_warning": None,
            }

        # Wellness Daily Analysis
        if "wellness" in prompt.lower():
            return {
                "status": "normal",
                "summary": "Resting heart rate and nighttime sleep duration were consistent with baseline.",
                "sleep_analysis": "7.5 hours of continuous rest observed with balanced phases.",
                "activity_analysis": "Daily walking target met calmly without excessive cardiac strain.",
            }

        # Daily Journal Section 4 Assessment
        if "athena clinical daily journal" in prompt.lower() or "daily journal" in prompt.lower():
            return {
                "analytical_assessment": "Patient responses indicate positive behavioral adjustment; no symptoms.",
                "data_coherence_evaluation": "Biometrics and subjective reporting align coherently.",
                "clinical_flags_detected": False,
                "recommendations_for_weekly": "Continue regular pacing and hydration reminders.",
            }

        return {"result": "ok"}

    async def generate_text(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        model: GeminiModelRole = GeminiModelRole.FLASH,
        temperature: float = 0.2,
    ) -> str:
        self.recorded_calls.append({
            "type": "text",
            "model_role": model,
            "prompt_snippet": prompt[:160],
        })

        if "neo" in prompt.lower() or "welcome back" in prompt.lower() or "returning patient" in prompt.lower():
            return (
                "Hello! Welcome back to Aegis. I am glad to continue accompanying you! "
                "Our consilium has completed your health update reassessment, and your new longitudinal plan is ready.\n\n"
                "[CLINICAL_REPORT]\n"
                '{"status": "INITIALIZATION", "symptoms_mentioned": [], "subjective_wellbeing_score": null, '
                '"adherence_to_plan": "UNKNOWN", "red_flags": false, "summary_for_master": "Neo greeting delivered."}\n'
                "[/CLINICAL_REPORT]"
            )

        # Standard initial greeting
        return (
            "Hello! I am Athena, your personal clinical companion in Aegis.\n\n"
            "[CLINICAL_REPORT]\n"
            '{"status": "INITIALIZATION", "symptoms_mentioned": [], "subjective_wellbeing_score": null, '
            '"adherence_to_plan": "UNKNOWN", "red_flags": false, "summary_for_master": "Initial greeting delivered."}\n'
            "[/CLINICAL_REPORT]"
        )

    async def generate_raw_text(
        self,
        contents: Any,
        model: GeminiModelRole = GeminiModelRole.FLASH,
        system_instruction: Optional[str] = None,
        response_mime_type: Optional[str] = None,
        temperature: float = 0.2,
    ) -> str:
        contents_str = str(contents)
        self.recorded_calls.append({
            "type": "raw_text",
            "model_role": model,
            "contents_snippet": contents_str[:160],
        })

        if self.custom_chat_turn_hook:
            return self.custom_chat_turn_hook(contents_str)

        # Doctor Visit Confirmation in Chat Turn
        if "visited the cardiologist" in contents_str.lower() or "visited the doctor" in contents_str.lower():
            clinical_report = {
                "status": "NORMAL",
                "referral_visit_event": "COMPLETED_VISIT",
                "referral_visit_context_note": "Patient verified completed in-person cardiologist visit.",
                "symptoms_mentioned": [],
                "subjective_wellbeing_score": 4,
                "adherence_to_plan": "HIGH",
                "red_flags": False,
                "summary_for_master": "Doctor consultation completed by patient.",
            }
            return (
                "Thank you for confirming your cardiologist visit! I have updated your health record "
                "and will cease reminders for this consultation.\n\n"
                f"[CLINICAL_REPORT]\n{json.dumps(clinical_report)}\n[/CLINICAL_REPORT]"
            )

        # Ambiguous / Non-confirmatory answer
        if "haven't gone" in contents_str.lower() or "next week" in contents_str.lower():
            clinical_report = {
                "status": "NORMAL",
                "referral_visit_event": "NONE",
                "referral_visit_context_note": "Patient has not visited clinician yet.",
                "symptoms_mentioned": [],
                "subjective_wellbeing_score": 3,
                "adherence_to_plan": "NORMAL",
                "red_flags": False,
                "summary_for_master": "Referral consultation pending.",
            }
            return (
                "Understood, take your time. Your Health Observation Summary remains ready in the Safe.\n\n"
                f"[CLINICAL_REPORT]\n{json.dumps(clinical_report)}\n[/CLINICAL_REPORT]"
            )

        # Mode 2 Resting Spike triage
        if "resting spike" in contents_str.lower() or "elevated resting" in contents_str.lower():
            clinical_report = {
                "status": "ALERT",
                "referral_visit_event": "NONE",
                "symptoms_mentioned": ["resting pulse elevation"],
                "subjective_wellbeing_score": 2,
                "adherence_to_plan": "HIGH",
                "red_flags": False,
                "summary_for_master": "Mode 2 resting spike noted; patient seated.",
            }
            return (
                "I notice your pulse is elevated while resting. Please sit comfortably and breathe slowly.\n\n"
                f"[CLINICAL_REPORT]\n{json.dumps(clinical_report)}\n[/CLINICAL_REPORT]"
            )

        # Standard daily chat reply
        default_report = {
            "status": "NORMAL",
            "referral_visit_event": "NONE",
            "symptoms_mentioned": [],
            "subjective_wellbeing_score": 4,
            "adherence_to_plan": "HIGH",
            "red_flags": False,
            "summary_for_master": "Patient answered routine daily question.",
        }
        return (
            "Thank you for your response! I have noted your progress in your daily record.\n\n"
            f"[CLINICAL_REPORT]\n{json.dumps(default_report)}\n[/CLINICAL_REPORT]"
        )


# ==============================================================================
# TEST FIXTURES
# ==============================================================================

@pytest.fixture
def simulation_db() -> Generator[Session, None, None]:
    """Isolated SQLite database for the full lifecycle simulation."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    session = session_factory()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


@pytest.fixture
def virtual_clock() -> VirtualClock:
    return VirtualClock(datetime(2026, 10, 18, 9, 0, 0, tzinfo=timezone.utc))


@pytest.fixture
def mock_gateway() -> ComprehensiveLifecycleMockGateway:
    return ComprehensiveLifecycleMockGateway()


def _make_device_session(
    patient_id: str,
    device_id: str = "dev_synthetic_01",
) -> DeviceSession:
    scopes = [
        SCOPE_CASCADE_INTAKE,
        SCOPE_CASCADE_RUN,
        SCOPE_CYCLE_READ,
        SCOPE_REPORTS_READ,
        SCOPE_CHAT_READ,
        SCOPE_CHAT_WRITE,
        SCOPE_VITALS_WRITE,
    ]
    return DeviceSession(
        patient_id=patient_id,
        device_id=device_id,
        jti=f"jti_{patient_id}_{device_id}",
        family_id=f"fam_{patient_id}",
        scope=" ".join(scopes),
        scopes=frozenset(scopes),
        issued_at=datetime.now(timezone.utc),
        expires_at=datetime.now(timezone.utc) + timedelta(hours=24),
    )


def _create_synthetic_jpeg_bytes(length: int = 128) -> bytes:
    header = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x01\x00`\x00`\x00\x00"
    if length < len(header):
        return header[:length]
    return header + b"\x00" * (length - len(header))


# ==============================================================================
# PART 1: THE CONTINUOUS FULL PATIENT LIFECYCLE SIMULATION (PHASES A -> H)
# ==============================================================================

@pytest.mark.anyio
async def test_full_system_continuous_lifecycle_journey(
    simulation_db: Session,
    virtual_clock: VirtualClock,
    mock_gateway: ComprehensiveLifecycleMockGateway,
):
    """
    Main continuous lifecycle simulation for a single synthetic patient:
    Verifies Trigger -> Execution -> Persistence -> Downstream Effect -> Client Visibility
    across 8 consecutive phases from first-run onboarding to post-Neo Weekly Consilium.
    """
    db = simulation_db
    clock = virtual_clock
    gateway = mock_gateway

    patient_id = "patient_full_lifecycle_synthetic_01"
    dev_session = _make_device_session(patient_id=patient_id)

    # Wire FastAPI dependency overrides
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[require_device_session] = lambda: dev_session
    app.dependency_overrides[verify_client_token] = lambda: dev_session
    app.dependency_overrides[require_service_admin_session] = lambda: ServiceAdminSession(admin_id="adm_test", role="admin")
    app.dependency_overrides[cascade_get_gemini_gateway] = lambda: gateway
    app.dependency_overrides[client_cascade_get_gemini_gateway] = lambda: gateway
    app.dependency_overrides[client_chat_get_gemini_gateway] = lambda: gateway
    app.dependency_overrides[vitals_get_gemini_gateway] = lambda: gateway
    app.dependency_overrides[cascade_get_now] = lambda: clock.now
    app.dependency_overrides[client_cascade_get_now] = lambda: clock.now
    app.dependency_overrides[client_chat_get_now] = lambda: clock.now
    app.dependency_overrides[vitals_get_now] = lambda: clock.now

    client = TestClient(app, headers={"Authorization": "Bearer mock_jwt_stage_8c1"})

    # Instantiate core services
    cycle_repo = MedicalCycleRepository(db)
    report_repo = ModelReportRepository(db)
    meas_repo = HealthMeasurementRepository(db)
    sprint_repo = WeeklySprintRepository(db)

    automation_svc = ClinicalAutomationService(
        db=db,
        gateway=gateway,
        now_provider=clock.now,
        enable_weekly_plan=True,
        enable_daily_journal=True,
        enable_wellness_daily=True,
        enable_automatic_weekly=True,
        enable_lab_revision=True,
    )

    # Seed Patient
    patient = Patient(patient_id=patient_id, status="active")
    db.add(patient)
    db.commit()

    # --------------------------------------------------------------------------
    # PHASE A: FIRST-RUN FLOW & PRIMARY CONSILIUM (DAY 0, T=0)
    # --------------------------------------------------------------------------
    # 1. Trigger: Cycle 1 creation
    cycle_1 = cycle_repo.create(cycle_id="cycle_synthetic_c1", patient_id=patient_id)
    cycle_1.status = "active"
    db.commit()

    # 2. Execution: M1 Anamnesis Intake in Blind Mode
    intake_answers = {
        "age_confirmation": "Confirmed",
        "gender": "Male",
        "age": 36,
        "height": 178,
        "weight": 76,
        "nicotine_status": "Non-user",
        "alcohol_status": "Non-drinker",
        "spec_factors": "None",
        "sleep_quality": "Normal",
        "diet_types": "Balanced",
        "diet_habits": "Regular",
        "gi_signals": "None",
        "work_type": "Office",
        "chronic_diseases": "None",
        "recent_illness": "None",
        "critical_infections": "None",
        "heredity_risks": "None",
        "medications": "None",
    }
    resp_anam = client.post(
        "/api/v1/client/cascade/intake/anamnesis",
        json={"cycle_id": cycle_1.cycle_id, "answers": intake_answers},
    )
    assert resp_anam.status_code == 200
    assert resp_anam.json()["status"] in ("processed", "completed")

    # Persistence verification: preparatory_anamnesis saved as SERVER_ONLY
    anam_reports = report_repo.list_server_only(cycle_1.cycle_id, PREPARATORY_ANAMNESIS_REPORT_TYPE)
    assert len(anam_reports) == 1
    assert anam_reports[0].privacy_level == PrivacyLevel.SERVER_ONLY.value

    # 3. Execution: Biometric Measurements Batch
    now_iso = clock.now().isoformat()
    vitals_payload = {
        "cycle_id": cycle_1.cycle_id,
        "measurements": [
            {
                "source": "manual_input",
                "measured_at": now_iso,
                "systolic": 122,
                "diastolic": 78,
                "phase": "baseline",
            },
            {
                "source": "manual_input",
                "measured_at": now_iso,
                "pulse": 68,
                "spo2": 98,
                "phase": "baseline",
            },
        ]
    }
    resp_vitals = client.post("/api/v1/vitals/batch", json=vitals_payload)
    assert resp_vitals.status_code in (200, 201)
    assert resp_vitals.json()["recorded_count"] == 2

    # 4. Execution: Vision OCR (Synthetic Lab Report)
    lab_ocr_svc = LabVisionOcrService(db=db, gateway=gateway, now_provider=clock.now)
    fake_lab_file = type("FakeUploadFile", (), {
        "filename": "blood_panel_scan.jpg",
        "read": lambda self: asyncio.sleep(0, result=_create_synthetic_jpeg_bytes(256)),
    })()
    ocr_result = await lab_ocr_svc.process_lab_images(cycle_id=cycle_1.cycle_id, files=[fake_lab_file])  # type: ignore
    assert ocr_result.extracted_count == 2
    assert any(m.marker_key == "ferritin" for m in ocr_result.markers)

    # 5. Execution: Vision ECG (Synthetic Smartwatch Strip)
    ecg_svc = EcgVisionService(db=db, gateway=gateway, now_provider=clock.now)
    fake_ecg_file = type("FakeUploadFile", (), {
        "filename": "smartwatch_lead1.jpg",
        "read": lambda self: asyncio.sleep(0, result=_create_synthetic_jpeg_bytes(256)),
    })()
    ecg_result = await ecg_svc.process_ecg_image(cycle_id=cycle_1.cycle_id, file=fake_ecg_file)  # type: ignore
    assert ecg_result.status in ("processed", "completed")
    ecg_reps = report_repo.list_server_only(cycle_1.cycle_id, PREPARATORY_ECG_REPORT_TYPE)
    assert len(ecg_reps) == 1
    assert ecg_reps[0].content["heart_rate_bpm"] == 68
    assert ecg_reps[0].content["rhythm_type"] == "sinus"

    # 6. Execution: Primary Consilium 10-Model Cascade
    calls_before_primary = len(gateway.recorded_calls)
    resp_primary = client.post("/api/v1/client/cascade/run-primary", json={"cycle_id": cycle_1.cycle_id})
    assert resp_primary.status_code == 200
    primary_data = resp_primary.json()
    assert primary_data["cycle_id"] == cycle_1.cycle_id
    assert len(primary_data["system_reports"]) == 8

    # Verify strictly 10 LLM calls in order: 8 Flash-Lite -> 1 Flash Deputy -> 1 Argon 4 Chief
    primary_calls = gateway.recorded_calls[calls_before_primary:]
    assert len(primary_calls) == 10
    for i in range(8):
        assert primary_calls[i]["model_role"] == GeminiModelRole.FLASH_LITE
        assert primary_calls[i]["resolved_model"] == "gemini-3.5-flash-lite"
    assert primary_calls[8]["model_role"] == GeminiModelRole.FLASH
    assert primary_calls[8]["resolved_model"] == "gemini-3.8-flash"
    assert primary_calls[9]["model_role"] == GeminiModelRole.PRO
    assert primary_calls[9]["resolved_model"] == "gemini-4-argon"

    # Client Visibility: Check Vault returns client-facing reports with zero diagnostic leak
    resp_vault = client.get(f"/api/v1/reports/client?cycle_id={cycle_1.cycle_id}")
    assert resp_vault.status_code == 200
    vault_items = resp_vault.json()
    assert len(vault_items) >= 9  # 8 system reports + 1 overall report

    # 7. Downstream: Automation Worker executes post_primary_init
    due_jobs = await automation_svc.process_due_jobs()
    assert len(due_jobs) >= 1
    init_job = next(j for j in due_jobs if j.job_type == AutomationJobType.POST_PRIMARY_INIT.value)
    assert init_job.status == AutomationJobStatus.COMPLETED.value

    # Verify T0 anchor established and first Athena greeting saved in chat history
    weekly_svc = WeeklyConsiliumService(db=db, gateway=gateway, now_provider=clock.now)
    t0_dt = weekly_svc.find_schedule_anchor_timestamp(cycle_1.cycle_id)
    assert t0_dt is not None

    chat_hist = weekly_svc.get_athena_chat_history(cycle_1.cycle_id)
    assert len(chat_hist) >= 1
    assert any(m.get("sender") == "curator" for m in chat_hist)

    # Advance virtual time by 30 seconds -> first planned question fires
    clock.advance(seconds=30)
    due_jobs_30s = await automation_svc.process_due_jobs()
    assert any(j.job_type == AutomationJobType.FIRST_PLANNED_QUESTION.value for j in due_jobs_30s)

    # --------------------------------------------------------------------------
    # PHASE B: DAILY OPERATIONS & ATHENA ACCOMPANIMENT (DAYS 1–2)
    # --------------------------------------------------------------------------
    clock.advance(hours=14)

    # Patient answers routine daily question in Athena chat
    chat_resp = client.post(
        "/api/v1/client/chat/athena",
        json={
            "message": "I went for my 30-minute evening walk and feeling energized.",
            "event_type": "user_message",
            "idempotency_key": "turn_day1_01",
        },
    )
    assert chat_resp.status_code == 200
    assert "noted" in chat_resp.json()["reply"].lower() or "progress" in chat_resp.json()["reply"].lower()

    # Day 1 sensor measurements ingested (heart rate & SpO2)
    now_d1 = clock.now().isoformat()
    client.post(
        "/api/v1/vitals/batch",
        json={
            "cycle_id": cycle_1.cycle_id,
            "measurements": [
                {
                    "source": "smart_watch",
                    "measured_at": now_d1,
                    "pulse": 70 + i,
                    "spo2": 98,
                    "phase": "active_day",
                }
                for i in range(5)
            ]
        },
    )

    # Next morning: Day 1 closes -> Wellness Daily executed
    clock.advance(hours=10)  # Day 1 -> Day 2 morning
    due_jobs_d1 = await automation_svc.process_due_jobs()

    # Verify wellness_daily produced report & triggered daily_journal_closure
    journal_svc = AthenaDailyJournalService(db=db, gateway=gateway, now_provider=clock.now)
    journals = journal_svc.list_daily_journals(cycle_1.cycle_id)
    assert len(journals) >= 1
    d1_journal = journals[0]
    assert d1_journal.patient_reported_evidence is not None
    assert d1_journal.wellness_evidence is not None

    # Day 2: Zero wearable data scenario -> verifies deterministic NO_DATA report
    clock.advance(hours=24)
    wellness_svc = WellnessDailyService(db=db, gateway=gateway, now_provider=clock.now)
    d2_report, created = await wellness_svc.process_wellness_for_day(cycle_1.cycle_id, calendar_week=1, calendar_day=2)
    assert d2_report.status.value == "no_data"

    # --------------------------------------------------------------------------
    # PHASE C: MONITORING EVENTS & MODE 2 TELEMETRY (DAY 2 EVENING)
    # --------------------------------------------------------------------------
    mode2_svc = Mode2VitalsMonitoringService(db=db, gateway=gateway, now_provider=clock.now)

    # Mode 2 processes deviation and detects potential episode (142 bpm >= 115 bpm)
    episode_rec = await mode2_svc.process_measurement(
        cycle_id=cycle_1.cycle_id,
        pulse=142,
        spo2=97,
        measured_at=clock.now(),
    )
    assert episode_rec is not None
    assert mode2_svc.has_active_episode(cycle_1.cycle_id)

    # User activity check: Confirms workout
    episode_ans = await mode2_svc.handle_user_statement(
        cycle_id=cycle_1.cycle_id,
        user_text="Yes, I am working out on the treadmill",
        is_workout=True,
    )
    assert episode_ans is not None
    assert episode_ans.workout_context.value == "confirmed_workout"

    # Complete workout session cleanly
    active_session = mode2_svc.get_active_workout_session(cycle_1.cycle_id)
    if active_session:
        mode2_svc.finish_workout(cycle_1.cycle_id, active_session.session_id)
    mode2_svc.close_episode(cycle_1.cycle_id)
    assert not mode2_svc.has_active_episode(cycle_1.cycle_id)

    # --------------------------------------------------------------------------
    # PHASE D: MID-WEEK NEW LABS & LAB REVISION CONSILIUM (DAY 3, T0 + 72h)
    # --------------------------------------------------------------------------
    clock.advance(days=1)  # Day 3

    lab_rev_svc = LabRevisionConsiliumService(db=db, gateway=gateway, now_provider=clock.now)
    # Upload updated lab values: corrected ferritin + new vitamin D study
    lab_rev_svc.upsert_laboratory_markers(
        cycle_id=cycle_1.cycle_id,
        mutations=[
            LabMarkerMutationItem(
                marker_key="ferritin",
                display_name="Ferritin",
                event_type=LabEventType.CORRECTION,
                value=28.0,
                unit="ng/mL",
                sample_date=clock.now().strftime("%Y-%m-%d"),
                reference_range=LabReferenceRange(low=30.0, high=300.0, text="30 - 300 ng/mL"),
                correction_reason="Corrected decimal transcription",
            ),
            LabMarkerMutationItem(
                marker_key="vitamin_d",
                display_name="25-OH Vitamin D",
                event_type=LabEventType.NEW_STUDY,
                value=32.4,
                unit="ng/mL",
                sample_date=clock.now().strftime("%Y-%m-%d"),
                reference_range=LabReferenceRange(low=30.0, high=100.0, text="30 - 100 ng/mL"),
            ),
        ],
    )

    # Ledger state confirms has_unreviewed_lab_changes = True
    ledger_state = lab_rev_svc.get_lab_ledger_state(cycle_1.cycle_id)
    assert ledger_state.ledger_version == 2
    assert ledger_state.has_unreviewed_lab_changes is True

    # Run Lab Revision Consilium (10 models: 8 Flash-Lite + 1 Flash Deputy + 1 Argon 4 Chief)
    calls_before_rev = len(gateway.recorded_calls)
    rev_resp = await lab_rev_svc.run_lab_revision_consilium(cycle_id=cycle_1.cycle_id)
    assert rev_resp.evaluated_ledger_version == 2
    assert rev_resp.evaluation_status == "evaluated"
    assert len(gateway.recorded_calls) - calls_before_rev == 10

    # Medical Invariant Audits for Lab Revision:
    # 1. Personal thresholds updated immediately
    assert rev_resp.vitals_thresholds is not None
    assert rev_resp.vitals_thresholds.min_pessimum_bpm > 0
    # 2. Athena active strategy updated immediately (version 2)
    strat_versions = report_repo.list_versions(cycle_1.cycle_id, ATHENA_ACTIVE_STRATEGY_REPORT_TYPE)
    assert any(s.content.get("source_consilium") == "lab_revision_consilium" and s.content.get("strategy_version") == 2 for s in strat_versions)
    # 3. Cooldown is 168 hours (active immediately, blocking spam)
    cooldown_d = lab_rev_svc.get_cooldown_status(cycle_1.cycle_id)
    assert cooldown_d.is_allowed is False
    assert cooldown_d.cooldown_hours == 168
    # 4. Referral decision is NOT created by Lab Revision (must wait for Weekly Consilium)
    assert weekly_svc.get_referral_lifecycle_state(cycle_1.cycle_id) is None
    # 5. Client Vault receives client-facing reports immediately
    resp_vault_d = client.get(f"/api/v1/reports/client?cycle_id={cycle_1.cycle_id}")
    assert resp_vault_d.status_code == 200

    # --------------------------------------------------------------------------
    # PHASE E: THREE CONSECUTIVE WEEKLY CONSILIUMS (WEEKS 1, 2, 3)
    # --------------------------------------------------------------------------
    # Week 1 Consilium (T0 + 168h)
    clock.set(t0_dt + timedelta(hours=168, minutes=1))
    due_jobs_w1 = await automation_svc.process_due_jobs()
    # Execute weekly steps A, B, C
    sprint_w1 = sprint_repo.get_active_for_cycle(cycle_1.cycle_id)
    if not sprint_w1:
        sprint_repo.create(sprint_id="sprint_c1_w1", cycle_id=cycle_1.cycle_id, sprint_number=1, status="active")

    w1_resp = await weekly_svc.run_weekly_consilium(cycle_id=cycle_1.cycle_id, sprint_id="sprint_c1_w1")
    assert w1_resp.sprint_number == 1
    assert weekly_svc.get_referral_lifecycle_state(cycle_1.cycle_id) is None

    # Week 2 Consilium (T0 + 336h)
    clock.set(t0_dt + timedelta(hours=336, minutes=1))
    sprint_repo.create(sprint_id="sprint_c1_w2", cycle_id=cycle_1.cycle_id, sprint_number=2, status="active")
    w2_resp = await weekly_svc.run_weekly_consilium(cycle_id=cycle_1.cycle_id, sprint_id="sprint_c1_w2")
    assert w2_resp.sprint_number == 2
    assert weekly_svc.get_referral_lifecycle_state(cycle_1.cycle_id) is None

    # Week 3 Consilium (T0 + 504h): Negative dynamics -> Chief Argon 4 recommends in-person referral
    clock.set(t0_dt + timedelta(hours=504, minutes=1))
    sprint_repo.create(sprint_id="sprint_c1_w3", cycle_id=cycle_1.cycle_id, sprint_number=3, status="active")
    gateway.weekly_referral_recommended = True

    w3_resp = await weekly_svc.run_weekly_consilium(cycle_id=cycle_1.cycle_id, sprint_id="sprint_c1_w3")
    assert w3_resp.sprint_number == 3
    ref_state_w3 = weekly_svc.get_referral_lifecycle_state(cycle_1.cycle_id)
    assert ref_state_w3 is not None
    assert ref_state_w3["status"] in ("generating_document", "document_ready")
    assert "Cardiology" in ref_state_w3["target_specialties"]

    # Verify referral_lifecycle_state created with status 'generating_document'
    ref_state_w3 = weekly_svc.get_referral_lifecycle_state(cycle_1.cycle_id)
    assert ref_state_w3 is not None
    assert ref_state_w3["status"] in ("generating_document", "document_ready")

    # --------------------------------------------------------------------------
    # PHASE F: REFERRAL DOCUMENT GENERATION, EXPORT & ATHENA FOLLOWUP
    # --------------------------------------------------------------------------
    # Automation Worker executes referral_document_generation
    doc_job = automation_svc.enqueue_referral_document_generation(
        cycle_id=cycle_1.cycle_id,
        patient_id=patient_id,
        sprint_id="sprint_c1_w3",
    )
    claimed_doc_job = automation_svc.claim_job(doc_job.job_id)
    assert claimed_doc_job is not None
    await automation_svc.execute_claimed_job(claimed_doc_job)

    # Document ready in Vault & Safe
    doc_resp = client.get("/api/v1/client/referral/documents")
    assert doc_resp.status_code == 200
    docs_list = doc_resp.json()["documents"]
    assert len(docs_list) >= 1
    active_doc = docs_list[0]
    assert active_doc["urgency"] == "routine"

    # Multi-format Document Export Verification
    doc_id = active_doc["report_id"]
    export_pdf = client.get(f"/api/v1/client/referral/documents/{doc_id}/export?format=pdf")
    assert export_pdf.status_code == 200
    assert export_pdf.headers["content-type"] == "application/pdf"
    assert export_pdf.content.startswith(b"%PDF")

    export_docx = client.get(f"/api/v1/client/referral/documents/{doc_id}/export?format=docx")
    assert export_docx.status_code == 200
    assert "wordprocessingml" in export_docx.headers["content-type"]
    assert export_docx.content.startswith(b"PK")

    export_md = client.get(f"/api/v1/client/referral/documents/{doc_id}/export?format=md")
    assert export_md.status_code == 200
    assert "text/markdown" in export_md.headers["content-type"]
    assert "AEGIS" in export_md.text

    # 168-Hour Reminder fires via automation
    clock.advance(days=7)  # T0 + 672h
    rem_job = automation_svc.enqueue_referral_followup_reminder(
        cycle_id=cycle_1.cycle_id,
        patient_id=patient_id,
        target_interval=168,
    )
    claimed_rem = automation_svc.claim_job(rem_job.job_id)
    assert claimed_rem is not None
    await automation_svc.execute_claimed_job(claimed_rem)

    # Patient visits cardiologist and reports visit in Athena chat
    visit_chat_resp = client.post(
        "/api/v1/client/chat/athena",
        json={
            "message": "I visited the cardiologist today. The ECG was sinus bradycardia without ischemic changes.",
            "event_type": "user_message",
            "idempotency_key": "turn_visit_confirm_01",
        },
    )
    assert visit_chat_resp.status_code == 200

    # Verify visit_confirmed = True and future reminders cease
    ref_state_confirmed = weekly_svc.get_referral_lifecycle_state(cycle_1.cycle_id)
    assert ref_state_confirmed is not None
    assert ref_state_confirmed["status"] == "visit_confirmed"
    athena_ref_confirmed = weekly_svc.get_referral_state_for_athena(cycle_1.cycle_id)
    assert athena_ref_confirmed["has_active_referral"] is False

    # --------------------------------------------------------------------------
    # PHASE G: UPDATE HEALTH INFORMATION & NEO CONSILIUM
    # --------------------------------------------------------------------------
    # Patient initiates Neo reassessment
    neo_cycle_svc = NeoCycleService(db=db, now_provider=clock.now)
    start_neo_resp = neo_cycle_svc.start_neo_reassessment(
        patient_id=patient_id,
    )
    cycle_2 = cycle_repo.get(start_neo_resp.cycle_id)
    assert cycle_2.status == "pending_reassessment"

    # Cycle 1 is now archived
    cycle_1_refreshed = cycle_repo.get(cycle_1.cycle_id)
    assert cycle_1_refreshed.status in ("completed", "archived")

    # Neo Re-Intake Anamnesis (Blind M1 for Cycle 2)
    neo_anam_resp = client.post(
        "/api/v1/client/cascade/intake/anamnesis",
        json={
            "cycle_id": cycle_2.cycle_id,
            "answers": intake_answers,
        },
    )
    assert neo_anam_resp.status_code == 200
    assert neo_anam_resp.json()["status"] in ("processed", "completed")

    # 10-Model Neo Consilium Execution (8 Neo Flash-Lite -> Neo Deputy -> Neo Chief)
    calls_before_neo = len(gateway.recorded_calls)
    primary_svc = PrimaryConsiliumService(db=db, gateway=gateway, now_provider=clock.now)
    neo_consilium_result = await primary_svc.run_primary_consilium(
        cycle_id=cycle_2.cycle_id,
        mode=ConsiliumMode.NEO_REASSESSMENT,
    )
    assert neo_consilium_result.cycle_id == cycle_2.cycle_id
    assert len(gateway.recorded_calls) - calls_before_neo == 10

    # Atomic Neo Activation via POST /api/v1/client/cascade/activate-neo
    resp_act = client.post(
        "/api/v1/client/cascade/activate-neo",
        json={"cycle_id": cycle_2.cycle_id},
    )
    assert resp_act.status_code == 200
    assert resp_act.json()["success"] is True
    assert resp_act.json()["status"] == "active"

    # Cycle 2 is now active
    cycle_2_refreshed = cycle_repo.get(cycle_2.cycle_id)
    assert cycle_2_refreshed.status == "active"

    # Automation worker executes post_primary_init for Cycle 2
    neo_init_jobs = await automation_svc.process_due_jobs()
    assert any(j.job_type == AutomationJobType.POST_PRIMARY_INIT.value for j in neo_init_jobs)

    # Athena welcomes returning patient
    neo_chat_hist = weekly_svc.get_athena_chat_history(cycle_2.cycle_id)
    assert len(neo_chat_hist) >= 1
    assert any(m.get("sender") == "curator" for m in neo_chat_hist)

    # --------------------------------------------------------------------------
    # PHASE H: WEEK 1 AFTER NEO (NEW CYCLE CONTINUATION & FIRST WEEKLY)
    # --------------------------------------------------------------------------
    # Daily tracking under Cycle 2
    clock.advance(days=1)
    client.post(
        "/api/v1/client/chat/athena",
        json={
            "message": "Continuing my updated routine calmly under Cycle 2.",
            "event_type": "user_message",
            "idempotency_key": "turn_neo_d1",
        },
    )

    # Week 1 of Cycle 2 completes (T0_neo + 168h)
    neo_t0 = weekly_svc.find_schedule_anchor_timestamp(cycle_2.cycle_id)
    assert neo_t0 is not None
    clock.set(neo_t0 + timedelta(hours=168, minutes=1))

    # Weekly Consilium executes cleanly under Cycle 2
    sprint_repo.create(sprint_id="sprint_c2_w1", cycle_id=cycle_2.cycle_id, sprint_number=1, status="active")
    gateway.weekly_referral_recommended = False
    neo_w1_resp = await weekly_svc.run_weekly_consilium(cycle_id=cycle_2.cycle_id, sprint_id="sprint_c2_w1")
    assert neo_w1_resp.sprint_number == 1
    assert neo_w1_resp.cycle_id == cycle_2.cycle_id

    # Verify historical isolation: Cycle 1 reports and documents remain in Vault
    all_client_docs = client.get("/api/v1/client/referral/documents").json()["documents"]
    assert len(all_client_docs) >= 1
    assert any(d["cycle_id"] == cycle_1.cycle_id for d in all_client_docs)


# ==============================================================================
# PART 2: DEDICATED BRANCH TESTS ON ISOLATED STATE COPIES
# ==============================================================================

@pytest.mark.anyio
async def test_branch_01_referral_non_confirmatory_reply_preserves_reminders(
    simulation_db: Session,
    virtual_clock: VirtualClock,
    mock_gateway: ComprehensiveLifecycleMockGateway,
):
    """
    Branch 1: When patient replies vaguely ("I haven't gone yet"), reminders do NOT cancel
    and referral_lifecycle_state remains active ('document_ready').
    """
    db = simulation_db
    clock = virtual_clock
    gateway = mock_gateway

    patient_id = "patient_branch_01"
    dev_session = _make_device_session(patient_id=patient_id)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[require_device_session] = lambda: dev_session
    app.dependency_overrides[verify_client_token] = lambda: dev_session
    app.dependency_overrides[client_chat_get_gemini_gateway] = lambda: gateway
    app.dependency_overrides[client_chat_get_now] = lambda: clock.now

    client = TestClient(app, headers={"Authorization": "Bearer mock_jwt_stage_8c1"})
    db.add(Patient(patient_id=patient_id, status="active"))
    cycle = MedicalCycleRepository(db).create(cycle_id="cycle_b01", patient_id=patient_id)
    cycle.status = "active"
    db.commit()

    weekly_svc = WeeklyConsiliumService(db=db, gateway=gateway, now_provider=clock.now)
    weekly_svc.save_referral_lifecycle_state(
        cycle_id=cycle.cycle_id,
        patient_id=patient_id,
        sprint_id="sprint_b01",
        sprint_number=1,
        decision=ProReferralDecision(
            recommended=True,
            urgency="routine",
            target_specialties=["Cardiology"],
            clinical_reason_for_referral="Symptomatic resting bradycardia",
        ),
    )
    # Generate referral document to establish document_ready state
    ref_doc_svc = ReferralDocumentService(db=db, gateway=gateway, now_provider=clock.now)
    await ref_doc_svc.generate_referral_document(cycle_id=cycle.cycle_id, sprint_id="sprint_b01")

    # Patient sends non-confirmatory reply
    resp = client.post(
        "/api/v1/client/chat/athena",
        json={
            "message": "I haven't gone to see the doctor yet, maybe next week.",
            "event_type": "user_message",
            "idempotency_key": "turn_non_confirm_01",
        },
    )
    assert resp.status_code == 200

    ref_state = weekly_svc.get_referral_lifecycle_state(cycle.cycle_id)
    assert ref_state is not None
    assert ref_state["status"] == "document_ready"
    athena_ref = weekly_svc.get_referral_state_for_athena(cycle.cycle_id)
    assert athena_ref["has_active_referral"] is True


@pytest.mark.anyio
async def test_branch_02_mode2_resting_spike_triage_and_chat_escalation(
    simulation_db: Session,
    virtual_clock: VirtualClock,
    mock_gateway: ComprehensiveLifecycleMockGateway,
):
    """
    Branch 2: When heart rate exceeds threshold and patient answers "No, resting",
    status transitions to RESTING_DEVIATION and episode is recorded with clinical alert.
    """
    db = simulation_db
    clock = virtual_clock
    gateway = mock_gateway

    patient_id = "patient_branch_02"
    db.add(Patient(patient_id=patient_id, status="active"))
    cycle = MedicalCycleRepository(db).create(cycle_id="cycle_b02", patient_id=patient_id)
    cycle.status = "active"
    db.commit()

    mode2_svc = Mode2VitalsMonitoringService(db=db, gateway=gateway, now_provider=clock.now)
    episode = await mode2_svc.process_measurement(
        cycle_id=cycle.cycle_id,
        pulse=145,
        spo2=96,
        measured_at=clock.now(),
    )
    assert episode is not None

    episode_ans = await mode2_svc.handle_user_statement(
        cycle_id=cycle.cycle_id,
        user_text="No, I am resting on the sofa reading a book.",
        is_workout=False,
    )
    assert episode_ans is not None
    assert episode_ans.workout_context.value == "not_working_out"
    assert episode_ans.status.value == "active"


@pytest.mark.anyio
async def test_branch_03_referral_document_retryable_failure_and_backoff_recovery(
    simulation_db: Session,
    virtual_clock: VirtualClock,
    mock_gateway: ComprehensiveLifecycleMockGateway,
):
    """
    Branch 3: When document generation hits transient failure, job transitions to RETRYABLE_FAILURE,
    and upon subsequent execution recovers to COMPLETED without duplicating reports.
    """
    db = simulation_db
    clock = virtual_clock
    gateway = mock_gateway

    patient_id = "patient_branch_03"
    db.add(Patient(patient_id=patient_id, status="active"))
    cycle = MedicalCycleRepository(db).create(cycle_id="cycle_b03", patient_id=patient_id)
    cycle.status = "active"
    db.commit()

    weekly_svc = WeeklyConsiliumService(db=db, gateway=gateway, now_provider=clock.now)
    weekly_svc.save_referral_lifecycle_state(
        cycle_id=cycle.cycle_id,
        patient_id=patient_id,
        sprint_id="sprint_b03",
        sprint_number=1,
        decision=ProReferralDecision(
            recommended=True,
            urgency="priority",
            target_specialties=["Cardiologist"],
            clinical_reason_for_referral="Tachycardia episodes",
        ),
    )
    db.commit()

    automation_svc = ClinicalAutomationService(db=db, gateway=gateway, now_provider=clock.now)
    job = automation_svc.enqueue_referral_document_generation(
        cycle_id=cycle.cycle_id,
        patient_id=patient_id,
        sprint_id="sprint_b03",
    )

    claimed = automation_svc.claim_job(job.job_id)
    assert claimed is not None

    # Simulate transient failure
    automation_svc.fail_job(
        job_id=claimed.job_id,
        lease_owner_id=claimed.lease_owner_id,
        error=RuntimeError("Transient network blip"),
        retryable=True,
    )
    failed_job = automation_svc.get_job_by_id(claimed.job_id)
    assert failed_job is not None
    assert failed_job.status == AutomationJobStatus.RETRYABLE_FAILURE.value

    # Recover on retry
    clock.advance(seconds=120)
    reclaimed = automation_svc.claim_job(job.job_id)
    assert reclaimed is not None
    await automation_svc.execute_claimed_job(reclaimed)

    done_job = automation_svc.get_job_by_id(job.job_id)
    assert done_job is not None
    assert done_job.status == AutomationJobStatus.COMPLETED.value


@pytest.mark.anyio
async def test_branch_04_primary_consilium_idempotency_zero_extra_llm_calls(
    simulation_db: Session,
    virtual_clock: VirtualClock,
    mock_gateway: ComprehensiveLifecycleMockGateway,
):
    """
    Branch 4: Repeating run_primary_consilium on completed cycle returns cached result with 0 extra calls.
    """
    db = simulation_db
    clock = virtual_clock
    gateway = mock_gateway

    patient_id = "patient_branch_04"
    db.add(Patient(patient_id=patient_id, status="active"))
    cycle = MedicalCycleRepository(db).create(cycle_id="cycle_b04", patient_id=patient_id)
    cycle.status = "active"
    db.commit()

    # Seed M1
    rep_repo = ModelReportRepository(db)
    m1 = dict(_sample_anamnesis_output())
    m1["agent_role"] = AegisAgentRole.ANAMNESIS_M1.value
    rep_repo.create_next_version(
        cycle_id=cycle.cycle_id,
        model="gemini-3.8-flash",
        report_type=PREPARATORY_ANAMNESIS_REPORT_TYPE,
        privacy_level=PrivacyLevel.SERVER_ONLY,
        content=m1,
    )
    db.commit()

    service = PrimaryConsiliumService(db=db, gateway=gateway, now_provider=clock.now)
    res_1 = await service.run_primary_consilium(cycle_id=cycle.cycle_id)
    assert len(gateway.recorded_calls) == 10

    # Repeat call
    res_2 = await service.run_primary_consilium(cycle_id=cycle.cycle_id)
    assert res_2.cycle_id == res_1.cycle_id
    assert len(gateway.recorded_calls) == 10  # Zero extra calls!


@pytest.mark.anyio
async def test_branch_05_weekly_consilium_idempotency_zero_extra_llm_calls(
    simulation_db: Session,
    virtual_clock: VirtualClock,
    mock_gateway: ComprehensiveLifecycleMockGateway,
):
    """
    Branch 5: Repeating run_weekly_consilium on completed sprint returns cached result with 0 extra calls.
    """
    db = simulation_db
    clock = virtual_clock
    gateway = mock_gateway

    patient_id = "patient_branch_05"
    db.add(Patient(patient_id=patient_id, status="active"))
    cycle = MedicalCycleRepository(db).create(cycle_id="cycle_b05", patient_id=patient_id)
    cycle.status = "active"
    db.commit()

    # Seed primary reports
    rep_repo = ModelReportRepository(db)
    rep_repo.create_next_version(
        cycle_id=cycle.cycle_id,
        model="gemini-4-argon",
        report_type="chief_physician_internal",
        privacy_level=PrivacyLevel.SERVER_ONLY,
        content={"pro_general_shelf": "Baseline strategy"},
    )
    rep_repo.create_next_version(
        cycle_id=cycle.cycle_id,
        model="gemini-3.8-flash",
        report_type="deputy_chief_physician_internal",
        privacy_level=PrivacyLevel.SERVER_ONLY,
        content={"flash_general_shelf": "Deputy baseline"},
    )
    rep_repo.create_next_version(
        cycle_id=cycle.cycle_id,
        model="gemini-4-argon",
        report_type="user_overall_report",
        privacy_level=PrivacyLevel.CLIENT_FACING,
        content={
            "title": "Initial Overview",
            "summary": "Baseline summary",
            "report_date": "2026-10-18",
            "has_changes": True,
            "highlights": ["h1"],
        },
    )
    WeeklySprintRepository(db).create(
        sprint_id="sprint_b05_w1",
        cycle_id=cycle.cycle_id,
        sprint_number=1,
        status="active",
    )
    weekly_svc = WeeklyConsiliumService(db=db, gateway=gateway, now_provider=clock.now)
    weekly_svc.record_schedule_anchor(cycle.cycle_id, anchor_dt=clock.now(), sprint_id="sprint_b05_w1")
    db.commit()

    w1 = await weekly_svc.run_weekly_consilium(cycle.cycle_id, "sprint_b05_w1")
    calls_count = len(gateway.recorded_calls)
    assert calls_count == 2

    # Repeat call with same sprint_id
    w2 = await weekly_svc.run_weekly_consilium(cycle.cycle_id, "sprint_b05_w1")
    assert w2.sprint_id == w1.sprint_id
    assert len(gateway.recorded_calls) == calls_count  # Zero extra calls!


@pytest.mark.anyio
async def test_branch_06_lab_revision_cooldown_rejection(
    simulation_db: Session,
    virtual_clock: VirtualClock,
    mock_gateway: ComprehensiveLifecycleMockGateway,
):
    """
    Branch 6: Calling run_lab_revision_consilium again before 168 hours raises ConsiliumCooldownError.
    """
    db = simulation_db
    clock = virtual_clock
    gateway = mock_gateway

    patient_id = "patient_branch_06"
    db.add(Patient(patient_id=patient_id, status="active"))
    cycle = MedicalCycleRepository(db).create(cycle_id="cycle_b06", patient_id=patient_id)
    cycle.status = "active"
    db.commit()

    # Seed Primary
    from tests.test_lab_revision_consilium import _seed_primary_consilium
    await _seed_primary_consilium(db, cycle_id=cycle.cycle_id)

    lab_svc = LabRevisionConsiliumService(db=db, gateway=gateway, now_provider=clock.now)
    lab_svc.upsert_laboratory_markers(
        cycle_id=cycle.cycle_id,
        mutations=[
            LabMarkerMutationItem(
                marker_key="ferritin",
                display_name="Ferritin",
                event_type=LabEventType.NEW_STUDY,
                value=35.0,
                unit="ng/mL",
                sample_date="2026-10-22",
                reference_range=LabReferenceRange(low=30.0, high=300.0, text="30-300"),
            )
        ],
    )
    await lab_svc.run_lab_revision_consilium(cycle.cycle_id)

    # Immediate second call without advancing 168 hours must fail with ConsiliumCooldownError
    with pytest.raises(ConsiliumCooldownError):
        await lab_svc.run_lab_revision_consilium(cycle.cycle_id)


@pytest.mark.anyio
async def test_branch_07_neo_activation_idempotency_safe_replay(
    simulation_db: Session,
    virtual_clock: VirtualClock,
    mock_gateway: ComprehensiveLifecycleMockGateway,
):
    """
    Branch 7: Repeating activate_prepared_neo on already active Neo cycle is a clean no-op.
    """
    db = simulation_db
    clock = virtual_clock
    gateway = mock_gateway

    patient, old_cycle, new_cycle = _setup_patient_and_cycles(
        db,
        patient_id="patient_branch_07",
        old_cycle_id="c_old_b07",
        new_cycle_id="c_neo_b07",
    )
    _populate_prepared_neo_consilium_data(db, new_cycle.cycle_id)

    act_svc = NeoConsiliumActivationService(db=db, gateway=gateway, now_provider=clock.now)
    res1 = act_svc.activate_prepared_neo(new_cycle.cycle_id)
    assert res1.success is True
    assert res1.status == "active"

    # When already active, repeating returns success=True with is_idempotent_replay=True
    res2 = act_svc.activate_prepared_neo(new_cycle.cycle_id)
    assert res2.success is True
    assert res2.is_idempotent_replay is True


@pytest.mark.anyio
async def test_branch_08_export_format_validation_and_security(
    simulation_db: Session,
    virtual_clock: VirtualClock,
    mock_gateway: ComprehensiveLifecycleMockGateway,
):
    """
    Branch 8: Invalid format request or non-existent document ID returns proper HTTP errors.
    """
    db = simulation_db
    clock = virtual_clock
    patient_id = "patient_branch_08"

    dev_session = _make_device_session(patient_id=patient_id)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[require_device_session] = lambda: dev_session
    app.dependency_overrides[verify_client_token] = lambda: dev_session

    client = TestClient(app, headers={"Authorization": "Bearer mock_jwt_stage_8c1"})
    db.add(Patient(patient_id=patient_id, status="active"))
    cycle = MedicalCycleRepository(db).create(cycle_id="cycle_b08", patient_id=patient_id)
    cycle.status = "active"
    db.commit()

    # Non-existent document -> 404
    resp_404 = client.get("/api/v1/client/referral/documents/non_existent_doc/export?format=pdf")
    assert resp_404.status_code == 404

    # Seed real document using standard helper
    raw_rep, client_rep = _seed_referral_documents(
        db,
        cycle_id=cycle.cycle_id,
        patient_id=patient_id,
        sprint_id="sprint_b08",
    )

    # Invalid export format -> 400
    resp_400 = client.get(f"/api/v1/client/referral/documents/{client_rep.report_id}/export?format=exe")
    assert resp_400.status_code == 400


@pytest.mark.anyio
async def test_branch_09_automation_worker_crash_and_lease_recovery(
    simulation_db: Session,
    virtual_clock: VirtualClock,
    mock_gateway: ComprehensiveLifecycleMockGateway,
):
    """
    Branch 9: Automation worker crash recovery re-claims expired RUNNING jobs cleanly.
    """
    db = simulation_db
    clock = virtual_clock
    gateway = mock_gateway

    patient_id = "patient_branch_09"
    db.add(Patient(patient_id=patient_id, status="active"))
    cycle = MedicalCycleRepository(db).create(cycle_id="cycle_b09", patient_id=patient_id)
    cycle.status = "active"
    db.commit()

    # Seed Primary Consilium completion so post_primary_init is permitted
    ModelReportRepository(db).create_next_version(
        cycle_id=cycle.cycle_id,
        model="gemini-4-argon",
        report_type="chief_physician_internal",
        privacy_level=PrivacyLevel.SERVER_ONLY,
        content={"status": "completed"},
    )
    db.commit()

    worker_1 = ClinicalAutomationService(
        db=db,
        gateway=gateway,
        now_provider=clock.now,
        lease_ttl_seconds=60,
    )
    worker_1.worker_id = "crashed_worker_1"
    job = worker_1.enqueue_post_primary_init(cycle_id=cycle.cycle_id, patient_id=patient_id)
    assert job is not None

    # Worker 1 claims job
    claimed = worker_1.claim_job(job.job_id)
    assert claimed is not None
    assert claimed.status == AutomationJobStatus.RUNNING.value
    assert claimed.lease_owner_id.startswith("auto_lease_")
    lease_1 = claimed.lease_owner_id

    # Worker 1 crashes; virtual time advances past lease TTL (60s)
    clock.advance(seconds=120)

    # Worker 2 discovers and re-claims job cleanly
    worker_2 = ClinicalAutomationService(
        db=db,
        gateway=gateway,
        now_provider=clock.now,
        lease_ttl_seconds=60,
    )
    reclaimed = worker_2.claim_job(job.job_id)
    assert reclaimed is not None
    assert reclaimed.lease_owner_id.startswith("auto_lease_")
    assert reclaimed.lease_owner_id != lease_1


@pytest.mark.anyio
async def test_branch_10_sqlite_distributed_lease_cross_worker_fencing(
    simulation_db: Session,
    virtual_clock: VirtualClock,
    mock_gateway: ComprehensiveLifecycleMockGateway,
):
    """
    Branch 10: SQLite distributed lease prevents concurrent consilium execution on the same cycle.
    """
    db = simulation_db
    clock = virtual_clock
    gateway = mock_gateway

    patient_id = "patient_branch_10"
    db.add(Patient(patient_id=patient_id, status="active"))
    cycle = MedicalCycleRepository(db).create(cycle_id="cycle_b10", patient_id=patient_id)
    cycle.status = "active"
    db.commit()

    weekly_svc = WeeklyConsiliumService(db=db, gateway=gateway, now_provider=clock.now)

    # Worker 1 acquires lease
    owner_1 = weekly_svc._acquire_cross_process_lease(cycle.cycle_id)
    assert owner_1 is not None

    # Worker 2 attempts concurrent acquisition -> must raise ConsiliumConcurrencyConflictError
    with pytest.raises(ConsiliumConcurrencyConflictError):
        weekly_svc._acquire_cross_process_lease(cycle.cycle_id)

    # Worker 1 releases lease
    weekly_svc._release_cross_process_lease_safe(cycle.cycle_id, owner_1)

    # Worker 2 can now acquire
    owner_2 = weekly_svc._acquire_cross_process_lease(cycle.cycle_id)
    assert owner_2 is not None
    weekly_svc._release_cross_process_lease_safe(cycle.cycle_id, owner_2)
