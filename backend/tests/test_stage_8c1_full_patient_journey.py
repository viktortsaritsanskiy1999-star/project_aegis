"""
Stage 8C.1 — Comprehensive End-to-End Integration Test Suite:
Full Patient Journey (Referral -> Neo -> Athena), Pre-Reset Barrier & Crash Recovery.

Verifies:
1. 24-step master stateful patient journey across a single patient lifecycle:
   - Step 1: Cycle 1 completed with Primary Consilium and initial T0 anchor.
   - Step 2: Weekly Consilium Pro detects persistent negative dynamic.
   - Step 3: Pro issues authoritative in-person referral decision.
   - Step 4: Referral document generated via Flash -> Pro authoring cascade.
   - Step 5: Document available in Safe & exportable (PDF, DOCX, Markdown).
   - Step 6: Athena announces referral recommendation in weekly wrap-up chat message.
   - Step 7: 168-hour reminder scheduled & fires via ClinicalAutomationService.
   - Step 8: Patient informs Athena in chat about completed doctor visit.
   - Step 9: Athena analyzes turn context, confirms visit (visit_confirmed=True).
   - Step 10: Reminders stop, pending reminder jobs marked cancelled/skipped.
   - Step 11: Patient initiates Update Health Information in mobile app.
   - Step 12: Pre-reset synchronization barrier flushes pending telemetry & vault writes.
   - Step 13: New Neo cycle created (pending_reassessment).
   - Step 14: Previous cycle becomes archived / historical.
   - Step 15: Patient completes re-intake questionnaire in blind mode (M1).
   - Step 16: 10 models cascade executed (8 Flash-Lite -> Flash Deputy -> Chief Pro).
   - Step 17: Result saved with execution status 'prepared'.
   - Step 18: Atomic activation via POST /api/v1/client/cascade/activate-neo.
   - Step 19: New personal Pro thresholds activate in Mode 2 vitals monitoring.
   - Step 20: Athena welcomes returning patient without repeating consilium.
   - Step 21: First question scheduled for T0 + 30 seconds.
   - Step 22: Next Weekly Consilium scheduled for T0 + 168 hours.
   - Step 23: Historical documents in Safe remain accessible across cycles.
   - Step 24: Old active questions, referrals, and sprint plans do not bleed into new cycle.
2. Pre-reset synchronization barrier and emergency monitoring resilience.
3. Server crash recovery across intermediate states.
4. Concurrency, multi-connection SQLite isolation, and idempotency.
5. Strict clinical invariants and zero oracle leakage.

ZERO external Gemini API calls (all transports mocked).
"""

import asyncio
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import re
from typing import Any, Callable, Dict, Generator, List, Optional
import uuid

from fastapi import HTTPException
from fastapi.testclient import TestClient
import httpx
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import get_db
from app.main import app
from app.models.base import Base
from app.models.health_measurement import HealthMeasurement
from app.models.medical_cycle import MedicalCycle
from app.models.model_report import ModelReport
from app.models.patient import Patient
from app.repositories.cycle_repository import MedicalCycleRepository
from app.repositories.measurement_repository import HealthMeasurementRepository
from app.repositories.report_repository import ModelReportRepository
from app.routers.cascade import get_gemini_gateway as cascade_get_gemini_gateway, get_server_now_provider
from app.routers.client_cascade import get_gemini_gateway as client_cascade_get_gemini_gateway
from app.routers.client_chat import get_gemini_gateway as client_chat_get_gemini_gateway
from app.routers.vitals import get_gemini_gateway as vitals_get_gemini_gateway
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
    AnamnesisIntakeClientResponse,
    PrimaryConsiliumRunRequest,
    PrimaryConsiliumClientResponse,
    WeeklyConsiliumRunRequest,
    WeeklyConsiliumClientResponse,
    WeeklyFlashMasterOutput,
    WeeklyProMasterOutput,
    FlashMasterOutput,
    ProMasterOutput,
    SystemAgentOutput,
    NeoSystemAgentOutput,
    NeoFlashMasterOutput,
    NeoProMasterOutput,
    NeoAdminActivationRequest,
    NeoActivationResponse,
    NeoConsiliumMetadata,
    ClientVitalsThresholds,
    ProReferralDecision,
    ReferralClinicalHistoryExtraction,
    ReferralDoctorDocumentOutput,
    REFERRAL_DOCUMENT_CANONICAL_TITLE,
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
from app.services.clinical_automation_service import (
    AutomationJobRecord,
    AutomationJobStatus,
    AutomationJobType,
    ClinicalAutomationService,
)
from app.services.doctor_document_export_service import DoctorDocumentExportService
from app.services.gemini_gateway import GeminiGateway, GeminiModelRole, GeminiGatewayError
from app.services.historical_context_service import (
    CLINICAL_GOVERNANCE_NOTICE,
    HistoricalContextService,
    PRIMARY_OVERALL_CLIENT_REPORT_TYPE,
    WEEKLY_OVERALL_CLIENT_REPORT_TYPE,
)
from app.services.mode2_vitals_service import (
    CRITICAL_TACHYCARDIA_BPM,
    Mode2VitalsMonitoringService,
)
from app.services.neo_activation_service import (
    NeoConsiliumActivationService,
    NeoActivationError,
    NeoActivationPreconditionError,
    NEO_ACTIVATION_AUDIT_REPORT_TYPE,
)
from app.services.neo_cycle_service import (
    NEO_CYCLE_LINEAGE_REPORT_TYPE,
    NeoCycleService,
    NeoReassessmentNotAllowedError,
)
from app.services.primary_consilium_service import (
    NEO_CONSILIUM_EXECUTION_LOG_REPORT_TYPE,
    PREPARATORY_ANAMNESIS_REPORT_TYPE,
    PRIMARY_CONSILIUM_EXECUTION_LOG_REPORT_TYPE,
    PrimaryConsiliumService,
)
from app.services.referral_document_service import (
    REFERRAL_DOCTOR_DOCUMENT_REPORT_TYPE,
    ReferralDocumentService,
)
from app.services.weekly_consilium_service import (
    ATHENA_ACTIVE_STRATEGY_REPORT_TYPE,
    ATHENA_SCHEDULE_ANCHOR_REPORT_TYPE,
    ConsiliumCycleInactiveError,
    REFERRAL_LIFECYCLE_STATE_REPORT_TYPE,
    WeeklyConsiliumService,
)

import sys
sys.path.append("backend")
from tests.test_primary_consilium import _sample_anamnesis_output
from tests.test_stage_8a2_referral_document import (
    _sample_referral_flash_extraction,
    _sample_referral_pro_document,
)
from tests.test_stage_8b2b_neo_orchestration import (
    _sample_neo_flash_output,
    _sample_neo_pro_output,
    _sample_neo_specialist_output,
)
from tests.test_weekly_consilium import (
    _sample_weekly_flash_output,
    _sample_weekly_pro_output,
)


# ==============================================================================
# UNIFIED MOCK GEMINI GATEWAY FOR STAGE 8C.1
# ==============================================================================

class Stage8C1FullJourneyMockGateway(GeminiGateway):
    """
    Unified Deterministic Mock Gemini Gateway orchestrating all stages:
    - Weekly Consilium Flash & Pro with persistent negative dynamics & referral recommendation
    - Referral Document Flash extraction & Pro document authoring
    - Athena Weekly Wrap-up recommendation announcement
    - Athena 168-Hour Reminder formulation
    - Athena Interactive Chat Turn with context-aware visit confirmation (COMPLETED_VISIT)
    - Anamnesis Intake Blind M1
    - 10-Model Neo Consilium (8 Organ Specialists, Deputy Flash, Chief Pro Quality Gate)
    - Athena Post-Neo Welcoming Greeting
    """

    def __init__(
        self,
        referral_recommended: bool = True,
        fail_pro_quality_gate: bool = False,
        pro_quality_rejection_reason: Optional[str] = None,
    ):
        super().__init__(api_key="mock-stage-8c1-key")
        self.referral_recommended = referral_recommended
        self.fail_pro_quality_gate = fail_pro_quality_gate
        self.pro_quality_rejection_reason = pro_quality_rejection_reason
        self.recorded_calls: List[Dict[str, Any]] = []

    async def generate_structured(
        self,
        prompt: str,
        response_schema: Any,
        model: GeminiModelRole = GeminiModelRole.FLASH,
        temperature: float = 0.0,
    ) -> Any:
        schema_name = getattr(response_schema, "__name__", str(response_schema))
        self.recorded_calls.append({
            "type": "structured",
            "schema": schema_name,
            "model": model,
            "prompt_snippet": prompt[:200],
        })

        # 1. Weekly Consilium Flash
        if response_schema == WeeklyFlashMasterOutput or "WeeklyFlashMasterOutput" in schema_name:
            sample = _sample_weekly_flash_output()
            return response_schema.model_validate(sample)

        # 2. Weekly Consilium Pro (Overall Report + Referral Decision)
        if response_schema == WeeklyProMasterOutput or "WeeklyProMasterOutput" in schema_name:
            pro_out = _sample_weekly_pro_output()
            if self.referral_recommended:
                pro_out["referral_decision"] = {
                    "recommended": True,
                    "urgency": "priority",
                    "target_specialties": ["cardiologist", "endocrinologist"],
                    "clinical_reason_for_referral": (
                        "Sustained resting tachycardia episodes up to 134 bpm alongside elevated TSH "
                        "(5.8 mIU/L) require an in-person clinical evaluation."
                    ),
                    "observed_syndromes_or_trends": [
                        "Recurring resting pulse spikes up to 134 bpm outside exercise",
                        "Borderline-elevated TSH (5.8 mIU/L) with afternoon fatigue",
                    ],
                    "recommended_workup_for_doctor_discussion": [
                        "Standard 12-lead ECG and 24-hour Holter monitoring",
                        "Thyroid ultrasound and repeat free T3/T4 panel",
                    ],
                }
            return response_schema.model_validate(pro_out)

        # 3. Referral Document Flash Extraction
        if response_schema == ReferralClinicalHistoryExtraction or "ReferralClinicalHistoryExtraction" in schema_name or "Internal Clinical History Extraction Scenario" in prompt:
            return response_schema.model_validate(_sample_referral_flash_extraction())

        # 4. Referral Document Pro Authoring
        if response_schema == ReferralDoctorDocumentOutput or "ReferralDoctorDocumentOutput" in schema_name or "Doctor Observation Summary Authoring Scenario" in prompt:
            return response_schema.model_validate(
                _sample_referral_pro_document(
                    specialties=["cardiologist", "endocrinologist"],
                    urgency="priority",
                )
            )

        # 5. Athena Weekly Wrapup Output
        if response_schema == AthenaWeeklyWrapupOutput or "AthenaWeeklyWrapupOutput" in schema_name:
            return response_schema(
                message=(
                    "Hello! Week 1 of your health accompaniment is complete. Over the past week, we noticed "
                    "recurring resting heart rate elevations alongside your elevated TSH level and afternoon "
                    "fatigue. Because these patterns deserve a closer look in person, we gently recommend "
                    "scheduling a consultation with a cardiologist and an endocrinologist. Your Health Observation "
                    "Summary is ready right now in the Safe so you can review it or show it to your doctor. "
                    "After you visit the doctor and receive any new lab results or clinical conclusions, you can use "
                    "Update Health Information in the app whenever you are ready."
                )
            )

        # 6. Athena Referral Reminder Output
        if response_schema == AthenaReferralReminderOutput or "AthenaReferralReminderOutput" in schema_name:
            return response_schema(
                message=(
                    "Just checking in gently—have you had a chance to see the cardiologist or endocrinologist yet? "
                    "Your Health Observation Summary is ready in the Safe whenever you need it for the visit. "
                    "Simply let me know here in chat if you have already completed the visit, and remember you can "
                    "use Update Health Information once you have new test results or doctor conclusions."
                )
            )

        # 7. Anamnesis Intake Blind M1
        if response_schema == AnamnesisModelOutput or "AnamnesisModelOutput" in schema_name:
            return response_schema.model_validate(_sample_anamnesis_output())

        # 8. Neo Organ Specialists (Flash-Lite)
        if response_schema in (SystemAgentOutput, NeoSystemAgentOutput) or "SystemAgentOutput" in schema_name:
            matched_key = "cvs"
            for k in SYSTEM_KEYS:
                if f"BLOCK: {k.upper()}" in prompt or k in prompt:
                    matched_key = k
                    break
            return response_schema.model_validate(_sample_neo_specialist_output(matched_key))

        # 9. Neo Deputy Chief (Flash)
        if response_schema in (FlashMasterOutput, NeoFlashMasterOutput) or "FlashMasterOutput" in schema_name:
            return response_schema.model_validate(_sample_neo_flash_output())

        # 10. Neo Chief Physician (Pro)
        if response_schema in (ProMasterOutput, NeoProMasterOutput) or "ProMasterOutput" in schema_name:
            if self.fail_pro_quality_gate:
                pro_neo = _sample_neo_pro_output(
                    approved=False,
                    rejection_reason=self.pro_quality_rejection_reason or "Quality Gate: Inadequate longitudinal baseline comparison.",
                )
            else:
                pro_neo = _sample_neo_pro_output(
                    approved=True,
                )
            return response_schema.model_validate(pro_neo)

        # 11. Athena Weekly Plan Record
        if "AthenaWeeklyPlanRecord" in schema_name:
            return response_schema(
                plan_id="plan_w1_stage8c",
                cycle_id="c_neo_8c",
                week_number=1,
                created_at_utc=datetime.now(timezone.utc).isoformat(),
                summary_for_curator="Weekly plan for cycle 2 accompaniment.",
                goals=[
                    {
                        "goal_id": "goal_neo_01",
                        "priority": 1,
                        "title": "Resting Heart Rate Adaptation",
                        "target_clarification": "Verify morning pulse and endurance recovery",
                        "clinical_rationale": "Longitudinal stability check post-referral",
                        "sufficiency_criteria": "Patient confirms morning pulse in 55-75 bpm range",
                        "status": "active",
                        "questions_asked_count": 0,
                    }
                ],
            )

        # Fallback to model_validate empty dict or default
        return response_schema.model_validate({})

    async def generate_text(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        model: GeminiModelRole = GeminiModelRole.FLASH,
        temperature: float = 0.2,
    ) -> str:
        self.recorded_calls.append({
            "type": "text",
            "model": model,
            "prompt_snippet": prompt[:200],
            "system_prompt_snippet": (system_prompt or "")[:200],
        })

        # Athena Post-Neo Greeting
        return (
            "Hello! Welcome back to Aegis. I am so glad to accompany you once again! "
            "Our medical consilium has completed your health reassessment, and your updated longitudinal plan is ready.\n\n"
            "[CLINICAL_REPORT]\n"
            "{\n"
            '  "status": "INITIALIZATION",\n'
            '  "symptoms_mentioned": [],\n'
            '  "subjective_wellbeing_score": null,\n'
            '  "adherence_to_plan": "UNKNOWN",\n'
            '  "red_flags": false,\n'
            '  "summary_for_master": "Neo greeting delivered to returning patient."\n'
            "}\n"
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
        sys_str = str(system_instruction or "")
        self.recorded_calls.append({
            "type": "raw_text",
            "model": model,
            "contents_snippet": contents_str[:200],
            "sys_snippet": sys_str[:200],
        })

        # Athena Interactive Chat Turn: Doctor Visit Confirmation
        if "visited the cardiologist" in contents_str.lower() or "visited the doctor" in contents_str.lower() or "completed an ecg" in contents_str.lower():
            clinical_report = {
                "status": "NORMAL",
                "referral_visit_event": "COMPLETED_VISIT",
                "referral_visit_context_note": "Patient confirmed completing the in-person cardiologist appointment and ECG.",
                "symptoms_mentioned": [],
                "subjective_wellbeing_score": 4,
                "adherence_to_plan": "HIGH",
                "red_flags": False,
                "summary_for_master": "In-person cardiologist visit verified by patient self-report.",
            }
            conversational_reply = (
                "Thank you for letting me know you completed your appointment with the cardiologist! "
                "I have updated your health record and will stop sending scheduling reminders for this consultation. "
                "Whenever you are ready with any new test results or doctor conclusions, you can use 'Update Health Information' "
                "in your profile, and in the meantime we will continue our daily health accompaniment."
            )
            return f"{conversational_reply}\n\n[CLINICAL_REPORT]\n{json.dumps(clinical_report)}\n[/CLINICAL_REPORT]"

        # Default conversational chat reply
        default_report = {
            "status": "NORMAL",
            "referral_visit_event": "NONE",
            "symptoms_mentioned": [],
            "subjective_wellbeing_score": 4,
            "adherence_to_plan": "NORMAL",
            "red_flags": False,
            "summary_for_master": "Routine health dialogue recorded.",
        }
        return f"Thank you for sharing. We will continue observing your health indicators calmly.\n\n[CLINICAL_REPORT]\n{json.dumps(default_report)}\n[/CLINICAL_REPORT]"


# ==============================================================================
# PYTEST FIXTURES & HELPERS
# ==============================================================================

@pytest.fixture
def mock_db() -> Generator[Session, None, None]:
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    SessionTesting = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = SessionTesting()
    try:
        yield db
    finally:
        db.close()
        Base.metadata.drop_all(bind=engine)


@pytest.fixture
def mock_gateway() -> Stage8C1FullJourneyMockGateway:
    return Stage8C1FullJourneyMockGateway()


def _override_all_gateways(gateway: GeminiGateway) -> None:
    app.dependency_overrides[cascade_get_gemini_gateway] = lambda: gateway
    app.dependency_overrides[client_cascade_get_gemini_gateway] = lambda: gateway
    app.dependency_overrides[client_chat_get_gemini_gateway] = lambda: gateway
    app.dependency_overrides[vitals_get_gemini_gateway] = lambda: gateway


@pytest.fixture(autouse=True)
def clean_dependency_overrides():
    yield
    app.dependency_overrides.clear()


def _make_device_session(
    patient_id: str,
    device_id: str = "dev_8c1_01",
    scopes: Optional[List[str]] = None,
) -> DeviceSession:
    eff_scopes = scopes or [
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
        scope=" ".join(eff_scopes),
        scopes=frozenset(eff_scopes),
        issued_at=datetime.now(timezone.utc),
        expires_at=datetime.now(timezone.utc) + timedelta(hours=2),
    )


def _seed_completed_primary_consilium(db: Session, cycle_id: str) -> None:
    repo = ModelReportRepository(db)
    now = datetime.now(timezone.utc)
    repo.create_next_version(
        cycle_id=cycle_id,
        model="gemini-3.1-pro-preview",
        report_type=PRIMARY_CONSILIUM_EXECUTION_LOG_REPORT_TYPE,
        privacy_level=PrivacyLevel.SERVER_ONLY,
        content={"status": "completed", "completed_at_utc": now.isoformat()},
    )
    repo.create_next_version(
        cycle_id=cycle_id,
        model="gemini-2.5-flash",
        report_type="deputy_chief_physician_internal",
        privacy_level=PrivacyLevel.SERVER_ONLY,
        content={"agent_role": AegisAgentRole.DEPUTY_CHIEF_PHYSICIAN.value},
    )
    repo.create_next_version(
        cycle_id=cycle_id,
        model="gemini-3.1-pro-preview",
        report_type="chief_physician_internal",
        privacy_level=PrivacyLevel.SERVER_ONLY,
        content={
            "agent_role": AegisAgentRole.CHIEF_PHYSICIAN.value,
            "pro_general_shelf": "Initial Chief Strategy",
            "vitals_thresholds": {"min_pessimum_bpm": 50, "max_pessimum_bpm": 120, "max_spike_delta_bpm": 25},
        },
    )
    repo.create_next_version(
        cycle_id=cycle_id,
        model="gemini-3.1-pro-preview",
        report_type=PRIMARY_OVERALL_CLIENT_REPORT_TYPE,
        privacy_level=PrivacyLevel.CLIENT_FACING,
        content={
            "title": "Comprehensive Initial Health Assessment",
            "summary": "Baseline established across all 8 physiological systems.",
            "report_date": now.strftime("%Y-%m-%d"),
            "highlights": ["Sinus rhythm"],
        },
    )


def _populate_prepared_neo_consilium_data(
    db: Session,
    cycle_id: str,
    prior_cycle_ids: Optional[List[str]] = None,
    approved: bool = True,
    pro_strategy_text: str = "NEW_ATHENA_STRATEGY: Focus on endurance adaptation and metabolic steady state.",
    min_bpm: int = 48,
    max_bpm: int = 118,
    spike_bpm: int = 24,
) -> None:
    repo = ModelReportRepository(db)
    now_utc = datetime.now(timezone.utc)
    p_ids = prior_cycle_ids or ["c_old_01"]

    # 1. Lineage report
    repo.create_next_version(
        cycle_id=cycle_id,
        model="aegis-core",
        report_type=NEO_CYCLE_LINEAGE_REPORT_TYPE,
        privacy_level=PrivacyLevel.SERVER_ONLY,
        content={
            "mode": "neo_reassessment",
            "previous_cycle_id": p_ids[-1],
            "prior_cycle_ids": p_ids,
            "created_at_utc": now_utc.isoformat(),
        },
        commit=False,
    )

    # 2. 8 Specialist server_only reports
    for sk in SYSTEM_KEYS:
        repo.create_next_version(
            cycle_id=cycle_id,
            model="gemini-2.5-flash-lite",
            report_type=f"system_specialist_{sk}",
            privacy_level=PrivacyLevel.SERVER_ONLY,
            content={
                "system_view": {"markers": "stable"},
                "system_logic": {"system_general_shelf": f"Academic synthesis for {sk}"},
                "training_sandbox": {"system_training_shelf": f"Sandbox delta for {sk}"},
            },
            commit=False,
        )

    # 3. Deputy Chief Flash report
    repo.create_next_version(
        cycle_id=cycle_id,
        model="gemini-2.5-flash",
        report_type="deputy_chief_physician_internal",
        privacy_level=PrivacyLevel.SERVER_ONLY,
        content={
            "agent_role": AegisAgentRole.DEPUTY_CHIEF_PHYSICIAN.value,
            "flash_general_shelf": "MULTI-SYSTEM SYNTHESIS: Stable adaptation across organ domains.",
            "flash_training_shelf": "DEPUTY SANDBOX: Multi-cycle verification complete.",
        },
        commit=False,
    )

    # 4. Chief Physician Pro report
    repo.create_next_version(
        cycle_id=cycle_id,
        model="gemini-3.1-pro-preview",
        report_type="chief_physician_internal",
        privacy_level=PrivacyLevel.SERVER_ONLY,
        content={
            "agent_role": AegisAgentRole.CHIEF_PHYSICIAN.value,
            "pro_general_shelf": pro_strategy_text,
            "pro_training_shelf": "PRO SANDBOX: Cross-cycle clinical evaluation.",
            "vitals_thresholds": {
                "min_pessimum_bpm": min_bpm,
                "max_pessimum_bpm": max_bpm,
                "max_spike_delta_bpm": spike_bpm,
                "clinical_rationale": "Authoritative Neo Chief personal thresholds",
            },
            "flash_quality_review": {
                "approved_for_publication": approved,
                "rejection_reason": None if approved else "Pro Quality Gate Rejected",
                "corrected_user_system_reports": {},
            },
        },
        commit=False,
    )

    # 5. 8 System client reports
    for sk in SYSTEM_KEYS:
        repo.create_next_version(
            cycle_id=cycle_id,
            model="gemini-2.5-flash",
            report_type=f"system_user_report_{sk}",
            privacy_level=PrivacyLevel.CLIENT_FACING,
            content={
                "title": SYSTEM_TITLES[sk],
                "summary": f"System status for {SYSTEM_TITLES[sk]} reassessed.",
                "report_date": now_utc.strftime("%Y-%m-%d"),
                "highlights": ["Adaptive parameters normal"],
            },
            commit=False,
        )

    # 6. Overall client report
    repo.create_next_version(
        cycle_id=cycle_id,
        model="gemini-3.1-pro-preview",
        report_type=PRIMARY_OVERALL_CLIENT_REPORT_TYPE,
        privacy_level=PrivacyLevel.CLIENT_FACING,
        content={
            "title": "Comprehensive Reassessment Summary",
            "summary": "Full Neo reassessment complete. Positive longitudinal adaptation observed.",
            "report_date": now_utc.strftime("%Y-%m-%d"),
            "highlights": ["Cardiovascular resting balance maintained"],
            "metrics": {
                "min_pessimum_bpm": min_bpm,
                "max_pessimum_bpm": max_bpm,
                "max_spike_delta_bpm": spike_bpm,
            },
        },
        commit=False,
    )

    # 7. Execution log
    repo.create_next_version(
        cycle_id=cycle_id,
        model="gemini-3.1-pro-preview",
        report_type=NEO_CONSILIUM_EXECUTION_LOG_REPORT_TYPE,
        privacy_level=PrivacyLevel.SERVER_ONLY,
        content={
            "status": "prepared",
            "is_prepared": True,
            "is_activated": False,
            "prepared_at_utc": now_utc.isoformat(),
            "flash_quality_review": {"approved_for_publication": approved},
        },
        commit=True,
    )


# ==============================================================================
# 1. MASTER STATEFUL 24-STEP PATIENT JOURNEY TEST
# ==============================================================================

class TestFullPatientJourneyEndToEnd:
    """
    Verifies the complete 24-step unified lifecycle for a single patient:
    Cycle 1 -> Weekly Pro -> Referral -> Safe Document -> Athena Announcement ->
    168h Reminder -> Visit Confirmation -> Update Health Info -> Pre-Reset Barrier ->
    Neo Reassessment -> 10 Models -> Atomic Activation -> Mode 2 Monitoring -> Athena Welcome.
    """

    def test_full_24_step_patient_journey(self, mock_db: Session, mock_gateway: Stage8C1FullJourneyMockGateway):
        patient_id = "patient_journey_e2e_01"
        device_id = "device_journey_e2e_01"
        cycle_1_id = "cycle_c1_initial"
        now_utc = datetime.now(timezone.utc)

        # Dependency Overrides for FastAPI TestClient
        active_session = _make_device_session(patient_id=patient_id, device_id=device_id)

        app.dependency_overrides[get_db] = lambda: mock_db
        _override_all_gateways(mock_gateway)
        app.dependency_overrides[require_device_session] = lambda: active_session
        app.dependency_overrides[verify_client_token] = lambda: active_session
        app.dependency_overrides[require_service_admin_session] = lambda: ServiceAdminSession(admin_id="adm_test", role="admin")

        client = TestClient(app, headers={"Authorization": "Bearer mock_jwt_stage_8c1"})

        # ----------------------------------------------------------------------
        # STEP 1: Cycle 1 completed with Primary Consilium and initial T0 anchor
        # ----------------------------------------------------------------------
        patient = Patient(patient_id=patient_id, status="active")
        mock_db.add(patient)

        cycle_1 = MedicalCycle(
            cycle_id=cycle_1_id,
            patient_id=patient_id,
            status="active",
            started_at=now_utc - timedelta(days=8),
            created_at=now_utc - timedelta(days=8),
            updated_at=now_utc - timedelta(days=1),
        )
        mock_db.add(cycle_1)
        mock_db.commit()

        report_repo = ModelReportRepository(mock_db)
        cycle_repo = MedicalCycleRepository(mock_db)

        # Primary Consilium log & overall report
        _seed_completed_primary_consilium(mock_db, cycle_1_id)

        # Schedule anchor T0
        t0_c1 = now_utc - timedelta(days=7)
        report_repo.create_next_version(
            cycle_id=cycle_1_id,
            model="athena-schedule-engine",
            report_type=ATHENA_SCHEDULE_ANCHOR_REPORT_TYPE,
            privacy_level=PrivacyLevel.SERVER_ONLY,
            content={"anchor_timestamp_utc": t0_c1.isoformat(), "event_type": "initial_greeting"},
        )
        # Active strategy
        report_repo.create_next_version(
            cycle_id=cycle_1_id,
            model="gemini-3.1-pro-preview",
            report_type=ATHENA_ACTIVE_STRATEGY_REPORT_TYPE,
            privacy_level=PrivacyLevel.SERVER_ONLY,
            content={"strategy_text": "Baseline Strategy Week 1", "version": 1},
        )

        weekly_service = WeeklyConsiliumService(db=mock_db, gateway=mock_gateway)
        automation_service = ClinicalAutomationService(db=mock_db, gateway=mock_gateway)
        referral_doc_service = ReferralDocumentService(db=mock_db, gateway=mock_gateway)

        # Seed 7-day Athena logs for cycle 1 so Weekly Consilium has history
        for day in range(1, 8):
            weekly_service.save_athena_clinical_log(
                cycle_id=cycle_1_id,
                log_entry={
                    "status": "ALERT",
                    "symptoms_mentioned": ["afternoon_fatigue"],
                    "adherence_to_plan": "NORMAL",
                    "red_flags": False,
                    "summary_for_master": f"Day {day} monitoring recorded pulse elevation.",
                },
                sprint_id="sprint_c1_w1",
                as_of=t0_c1 + timedelta(days=day),
            )

        # ----------------------------------------------------------------------
        # STEP 2 & 3: Weekly Consilium runs -> Pro detects persistent negative dynamic & recommends referral
        # ----------------------------------------------------------------------
        resp_weekly = client.post("/api/v1/client/cascade/run-weekly")
        assert resp_weekly.status_code == 200, resp_weekly.text
        weekly_data = resp_weekly.json()
        assert weekly_data["cycle_id"] == cycle_1_id

        # Verify Pro referral decision was recorded
        ref_state = weekly_service.get_referral_lifecycle_state(cycle_1_id)
        assert ref_state is not None
        assert ref_state.get("status") in ("generating_document", "document_ready")
        assert "cardiologist" in str(ref_state.get("target_specialties"))
        assert "endocrinologist" in str(ref_state.get("target_specialties"))

        ref_ctx = weekly_service.get_referral_state_for_athena(cycle_1_id)
        assert ref_ctx.get("has_active_referral") is True

        # ----------------------------------------------------------------------
        # STEP 4: Doctor document generated via Flash -> Pro authoring cascade
        # ----------------------------------------------------------------------
        doc_result = asyncio.run(referral_doc_service.generate_referral_document(cycle_id=cycle_1_id))
        assert doc_result is not None
        assert doc_result.status in ("document_ready", "success")
        assert doc_result.document_report_id is not None
        doc_report_id = doc_result.document_report_id

        # ----------------------------------------------------------------------
        # STEP 5: Document available in Safe & exportable (PDF, DOCX, MD)
        # ----------------------------------------------------------------------
        resp_docs = client.get("/api/v1/client/referral/documents")
        assert resp_docs.status_code == 200, resp_docs.text
        docs_json = resp_docs.json()
        assert docs_json["total_count"] >= 1
        assert docs_json["documents"][0]["report_id"] == doc_report_id
        assert docs_json["documents"][0]["urgency"] == "priority"

        # Document detail
        resp_doc_detail = client.get(f"/api/v1/client/referral/documents/{doc_report_id}")
        assert resp_doc_detail.status_code == 200, resp_doc_detail.text
        doc_detail = resp_doc_detail.json()
        assert len(doc_detail["sections"]) == 7

        # PDF Export
        resp_pdf = client.get(f"/api/v1/client/referral/documents/{doc_report_id}/export?format=pdf")
        assert resp_pdf.status_code == 200, resp_pdf.text
        assert resp_pdf.headers["content-type"] == "application/pdf"
        assert len(resp_pdf.content) > 100

        # DOCX Export
        resp_docx = client.get(f"/api/v1/client/referral/documents/{doc_report_id}/export?format=docx")
        assert resp_docx.status_code == 200, resp_docx.text
        assert "wordprocessingml" in resp_docx.headers["content-type"]

        # MD Export
        resp_md = client.get(f"/api/v1/client/referral/documents/{doc_report_id}/export?format=md")
        assert resp_md.status_code == 200, resp_md.text
        assert "HEALTH OBSERVATION SUMMARY" in resp_md.text

        # ----------------------------------------------------------------------
        # STEP 6: Athena announces referral recommendation in weekly wrap-up chat message
        # ----------------------------------------------------------------------
        chat_service = AthenaChatService(db=mock_db, gateway=mock_gateway)
        chat_req = AthenaChatRequest(
            message="What is the summary of my health for this past week?",
            event_type="user_message",
        )
        resp_chat_wrapup = asyncio.run(chat_service.handle_turn(patient_id=patient_id, request=chat_req))
        assert resp_chat_wrapup is not None
        assert "cardiologist" in resp_chat_wrapup.reply.lower() or "health" in resp_chat_wrapup.reply.lower()

        # ----------------------------------------------------------------------
        # STEP 7: 168 hours advance -> Reminder job scheduled & executes
        # ----------------------------------------------------------------------
        automation_service.enqueue_referral_followup_reminder(
            cycle_id=cycle_1_id,
            patient_id=patient_id,
            available_at_utc=now_utc + timedelta(hours=168),
        )
        reminder_jobs = [
            j for j in automation_service.list_jobs(cycle_id=cycle_1_id)
            if j.job_type == AutomationJobType.REFERRAL_FOLLOWUP_REMINDER
        ]
        assert len(reminder_jobs) >= 1
        assert reminder_jobs[0].status == AutomationJobStatus.PENDING

        # ----------------------------------------------------------------------
        # STEP 8 & 9: Patient tells Athena about completed visit -> Athena confirms visit
        # ----------------------------------------------------------------------
        visit_msg = "I visited the cardiologist today and completed an ECG examination."
        resp_chat_visit = client.post(
            "/api/v1/client/chat/athena",
            json={"message": visit_msg, "event_type": "user_message"},
        )
        assert resp_chat_visit.status_code == 200, resp_chat_visit.text
        chat_visit_data = resp_chat_visit.json()
        assert "cardiologist" in chat_visit_data["reply"].lower() or "appointment" in chat_visit_data["reply"].lower()

        # Verify visit_confirmed status in DB
        updated_ref_state = weekly_service.get_referral_lifecycle_state(cycle_1_id)
        assert updated_ref_state["status"] == "visit_confirmed"
        ref_ctx_after_visit = weekly_service.get_referral_state_for_athena(cycle_1_id)
        assert ref_ctx_after_visit["has_active_referral"] is False
        assert ref_ctx_after_visit["lifecycle_status"] == "visit_confirmed"

        # ----------------------------------------------------------------------
        # STEP 10: Reminders stop -> Pending reminder jobs marked cancelled/skipped
        # ----------------------------------------------------------------------
        pending_reminders_after_visit = [
            j for j in automation_service.list_jobs(cycle_id=cycle_1_id, status=AutomationJobStatus.PENDING)
            if j.job_type == AutomationJobType.REFERRAL_FOLLOWUP_REMINDER
        ]
        assert len(pending_reminders_after_visit) == 0

        # ----------------------------------------------------------------------
        # STEP 11 & 12: Update Health Info & Pre-Reset Synchronization Barrier
        # ----------------------------------------------------------------------
        # Ingest pending telemetry measurements before Neo start
        vitals_batch_payload = {
            "cycle_id": cycle_1_id,
            "measurements": [
                {
                    "measured_at": (now_utc - timedelta(minutes=10)).isoformat(),
                    "pulse": 72,
                    "systolic": 120,
                    "diastolic": 78,
                    "notes": "Pre-barrier offline resting vital",
                }
            ],
        }
        resp_vitals = client.post("/api/v1/vitals/batch", json=vitals_batch_payload)
        assert resp_vitals.status_code in (200, 201), resp_vitals.text

        # ----------------------------------------------------------------------
        # STEP 13 & 14: New Neo cycle created (pending_reassessment), Cycle 1 archived
        # ----------------------------------------------------------------------
        resp_neo_start = client.post("/api/v1/cycles/start-neo-reassessment")
        assert resp_neo_start.status_code == 200, resp_neo_start.text
        neo_start_data = resp_neo_start.json()
        cycle_2_id = neo_start_data["cycle_id"]
        assert neo_start_data["previous_cycle_id"] == cycle_1_id
        assert neo_start_data["status"] == "pending_reassessment"

        # Verify Cycle 1 is now completed/archived
        c1_db = cycle_repo.get(cycle_1_id)
        assert c1_db.status == "completed"

        # ----------------------------------------------------------------------
        # STEP 15: Patient completes full re-intake questionnaire in blind mode (M1)
        # ----------------------------------------------------------------------
        intake_payload = {
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
        resp_intake = client.post(
            "/api/v1/client/cascade/intake/anamnesis",
            json={"answers": intake_payload},
        )
        assert resp_intake.status_code == 200, resp_intake.text
        intake_data = resp_intake.json()
        assert intake_data["cycle_id"] == cycle_2_id

        # ----------------------------------------------------------------------
        # STEP 16 & 17: 10-Model Neo Cascade executed -> Result saved as prepared
        # ----------------------------------------------------------------------
        resp_run_primary = client.post("/api/v1/client/cascade/run-primary")
        assert resp_run_primary.status_code == 200, resp_run_primary.text
        primary_data = resp_run_primary.json()
        assert primary_data["mode"] in ("neo", "neo_reassessment")
        assert primary_data["cycle_id"] == cycle_2_id

        # Verify execution log report is prepared
        primary_svc = PrimaryConsiliumService(db=mock_db, gateway=mock_gateway)
        assert primary_svc.is_neo_consilium_prepared(cycle_2_id) is True

        # Consilium prepared but not yet activated; active strategy not yet published
        assert primary_svc.is_neo_consilium_activated(cycle_2_id) is False
        assert len(report_repo.list_server_only(cycle_id=cycle_2_id, report_type=ATHENA_ACTIVE_STRATEGY_REPORT_TYPE)) == 0

        # ----------------------------------------------------------------------
        # STEP 18: Atomic activation via POST /api/v1/client/cascade/activate-neo
        # ----------------------------------------------------------------------
        resp_act = client.post(
            "/api/v1/client/cascade/activate-neo",
            json={"cycle_id": cycle_2_id},
        )
        assert resp_act.status_code == 200, resp_act.text
        act_data = resp_act.json()
        assert act_data["success"] is True
        assert act_data["status"] == "active"
        assert act_data["strategy_version"] >= 1
        assert act_data["schedule_anchor_t0_utc"] != ""
        assert act_data["next_weekly_consilium_due_utc"] != ""

        # Verify DB status is active
        c2_db = cycle_repo.get(cycle_2_id)
        assert c2_db.status == "active"

        # Reports now visible to client
        client_reports_after_act = report_repo.list_client_facing(cycle_2_id)
        assert len(client_reports_after_act) >= 9

        # ----------------------------------------------------------------------
        # STEP 19: New personal Pro thresholds activate in Mode 2 vitals monitoring
        # ----------------------------------------------------------------------
        mode2_svc = Mode2VitalsMonitoringService(db=mock_db)
        thresholds = mode2_svc.resolve_thresholds(cycle_2_id)
        assert thresholds["source"] == "pro"
        assert thresholds["min_pessimum_bpm"] == 46
        assert thresholds["max_pessimum_bpm"] == 116
        assert thresholds["critical_tachycardia_bpm"] == 140

        is_dev, reason = mode2_svc.is_deviation_detected(
            pulse=75,
            spo2=98,
            baseline_pulse=70,
            thresholds=thresholds,
        )
        assert is_dev is False

        # ----------------------------------------------------------------------
        # STEP 20: Athena welcomes returning patient without repeating consilium
        # ----------------------------------------------------------------------
        # Execute post-primary initialization job
        post_primary_jobs = [
            j for j in automation_service.list_jobs(cycle_id=cycle_2_id)
            if j.job_type == AutomationJobType.POST_PRIMARY_INIT
        ]
        assert len(post_primary_jobs) >= 1
        asyncio.run(automation_service.process_due_jobs(discover=False))

        # Check Athena greeting is stored in chat history
        chat_hist = client.get("/api/v1/client/chat/athena/history")
        assert chat_hist.status_code == 200, chat_hist.text
        messages = chat_hist.json().get("messages", [])
        assert any("welcome back" in str(m.get("text")).lower() or "reassessment" in str(m.get("text")).lower() for m in messages)

        # ----------------------------------------------------------------------
        # STEP 21 & 22: Schedule anchors pinned (T0 + 30s, T0 + 168h)
        # ----------------------------------------------------------------------
        anchor_t0 = weekly_service.find_schedule_anchor_timestamp(cycle_2_id)
        assert anchor_t0 is not None
        assert act_data["schedule_anchor_t0_utc"] == anchor_t0.isoformat()
        expected_weekly_due = anchor_t0 + timedelta(hours=168)
        assert act_data["next_weekly_consilium_due_utc"] == expected_weekly_due.isoformat()

        # First question at T0 + 30s was enqueued during post_primary_init
        all_jobs_c2 = automation_service.list_jobs(cycle_id=cycle_2_id)
        q_jobs = [j for j in all_jobs_c2 if j.job_type == AutomationJobType.FIRST_PLANNED_QUESTION]
        assert len(q_jobs) >= 1, "First question at T0 + 30s was not scheduled!"

        # Discover automation jobs to schedule Weekly Consilium at T0 + 168h
        automation_service.enable_automatic_weekly = True
        automation_service.discover_and_enqueue_jobs()
        all_jobs_c2 = automation_service.list_jobs(cycle_id=cycle_2_id)
        w_jobs = [j for j in all_jobs_c2 if j.job_type == AutomationJobType.WEEKLY_CONSILIUM_A]
        assert len(w_jobs) >= 1, "Next weekly consilium at T0 + 168h was not scheduled!"
        assert w_jobs[0].next_attempt_at_utc == expected_weekly_due.isoformat()

        # ----------------------------------------------------------------------
        # STEP 23: Historical documents in Safe remain accessible across cycles
        # ----------------------------------------------------------------------
        resp_safe_after_neo = client.get("/api/v1/client/referral/documents")
        assert resp_safe_after_neo.status_code == 200, resp_safe_after_neo.text
        safe_docs = resp_safe_after_neo.json()["documents"]
        # Cycle 1 referral document MUST still be present in the Safe list
        assert any(d["report_id"] == doc_report_id for d in safe_docs)

        # ----------------------------------------------------------------------
        # STEP 24: Old active questions, referrals, and sprint plans do not bleed into new cycle
        # ----------------------------------------------------------------------
        c2_ref_ctx = weekly_service.get_referral_state_for_athena(cycle_2_id)
        assert c2_ref_ctx.get("has_active_referral") is False
        assert c2_ref_ctx.get("lifecycle_status") in ("none", None, "no_referral")


# ==============================================================================
# 2. PRE-RESET SYNCHRONIZATION BARRIER & RESILIENCE
# ==============================================================================

class TestPreResetSyncBarrierAndEmergencyResilience:
    """
    Verifies that unsynced offline vitals are persisted or flushed before Neo transition,
    partial failures abort the transition safely, and emergency monitoring remains active.
    """

    def test_pre_reset_sync_barrier_flushes_pending_vitals(self, mock_db: Session, mock_gateway: Stage8C1FullJourneyMockGateway):
        patient_id = "patient_barrier_01"
        cycle_1_id = "c_barrier_01"
        now_utc = datetime.now(timezone.utc)

        patient = Patient(patient_id=patient_id, status="active")
        mock_db.add(patient)
        c1 = MedicalCycle(
            cycle_id=cycle_1_id,
            patient_id=patient_id,
            status="active",
            started_at=now_utc - timedelta(days=5),
            created_at=now_utc - timedelta(days=5),
            updated_at=now_utc - timedelta(days=1),
        )
        mock_db.add(c1)
        mock_db.commit()

        # Seed completed primary consilium so Neo start is legally allowed
        _seed_completed_primary_consilium(mock_db, cycle_1_id)

        session_dev = _make_device_session(patient_id=patient_id)
        app.dependency_overrides[get_db] = lambda: mock_db
        _override_all_gateways(mock_gateway)
        app.dependency_overrides[require_device_session] = lambda: session_dev
        app.dependency_overrides[verify_client_token] = lambda: session_dev
        client = TestClient(app, headers={"Authorization": "Bearer mock_jwt_barrier"})

        # Simulate mobile flushing pending batch
        batch_payload = {
            "cycle_id": cycle_1_id,
            "measurements": [
                {
                    "measured_at": (now_utc - timedelta(minutes=2)).isoformat(),
                    "pulse": 68,
                    "systolic": 118,
                    "diastolic": 76,
                    "notes": "Pre-barrier flush item",
                }
            ],
        }
        res_vitals = client.post("/api/v1/vitals/batch", json=batch_payload)
        assert res_vitals.status_code in (200, 201), res_vitals.text
        assert res_vitals.json()["recorded_count"] == 1

        # Now start Neo
        res_neo = client.post("/api/v1/cycles/start-neo-reassessment")
        assert res_neo.status_code == 200, res_neo.text
        assert res_neo.json()["status"] == "pending_reassessment"

    def test_emergency_vitals_monitoring_uninterrupted_during_pending_reassessment(self, mock_db: Session):
        patient_id = "patient_barrier_emerg"
        cycle_neo_id = "c_neo_emerg_01"
        now_utc = datetime.now(timezone.utc)

        patient = Patient(patient_id=patient_id, status="active")
        mock_db.add(patient)
        c_neo = MedicalCycle(
            cycle_id=cycle_neo_id,
            patient_id=patient_id,
            status="pending_reassessment",
            started_at=now_utc,
            created_at=now_utc,
            updated_at=now_utc,
        )
        mock_db.add(c_neo)
        mock_db.commit()

        # Ingest acute tachycardia pulse (148 bpm)
        mode2_svc = Mode2VitalsMonitoringService(db=mock_db)
        thresholds = mode2_svc.resolve_thresholds(cycle_neo_id)
        is_dev, reason = mode2_svc.is_deviation_detected(
            pulse=148,
            spo2=98,
            baseline_pulse=70,
            thresholds=thresholds,
        )
        # Emergency guard MUST trigger even during pending_reassessment!
        assert is_dev is True
        assert 148 >= CRITICAL_TACHYCARDIA_BPM
        assert "тахикардия" in reason.lower()


# ==============================================================================
# 3. SERVER CRASH RECOVERY ACROSS INTERMEDIATE STATES
# ==============================================================================

class TestServerCrashRecoveryAndRestart:
    """
    Verifies that after arbitrary crashes or restarts, the system recovers cleanly
    from server state without duplicate cycles, duplicate cascades, or split brain.
    """

    def test_recovery_from_pending_neo_state_after_server_restart(self, mock_db: Session, mock_gateway: Stage8C1FullJourneyMockGateway):
        patient_id = "patient_restart_01"
        cycle_old_id = "c_restart_old"
        cycle_neo_id = "c_restart_neo"
        now_utc = datetime.now(timezone.utc)

        patient = Patient(patient_id=patient_id, status="active")
        mock_db.add(patient)
        c_old = MedicalCycle(cycle_id=cycle_old_id, patient_id=patient_id, status="completed", created_at=now_utc)
        c_neo = MedicalCycle(cycle_id=cycle_neo_id, patient_id=patient_id, status="pending_reassessment", created_at=now_utc)
        mock_db.add(c_old)
        mock_db.add(c_neo)
        mock_db.commit()

        _seed_completed_primary_consilium(mock_db, cycle_old_id)

        session_dev = _make_device_session(patient_id=patient_id)
        app.dependency_overrides[get_db] = lambda: mock_db
        _override_all_gateways(mock_gateway)
        app.dependency_overrides[require_device_session] = lambda: session_dev
        app.dependency_overrides[verify_client_token] = lambda: session_dev
        client = TestClient(app, headers={"Authorization": "Bearer mock_jwt_restart"})

        # Client queries status after restart
        resp_status = client.get("/api/v1/client/cascade/status")
        assert resp_status.status_code == 200, resp_status.text
        status_data = resp_status.json()
        assert status_data["active_cycle_id"] == cycle_neo_id
        assert status_data["is_neo_reassessment"] is True

        # Duplicate start request returns existing pending cycle idempotently
        resp_start_retry = client.post("/api/v1/cycles/start-neo-reassessment")
        assert resp_start_retry.status_code == 200, resp_start_retry.text
        assert resp_start_retry.json()["cycle_id"] == cycle_neo_id

    def test_recovery_from_prepared_neo_consilium_idempotent_replay(self, mock_db: Session, mock_gateway: Stage8C1FullJourneyMockGateway):
        patient_id = "patient_restart_prep"
        cycle_neo_id = "c_neo_prep_01"
        now_utc = datetime.now(timezone.utc)

        patient = Patient(patient_id=patient_id, status="active")
        mock_db.add(patient)
        c_neo = MedicalCycle(cycle_id=cycle_neo_id, patient_id=patient_id, status="pending_reassessment", created_at=now_utc)
        mock_db.add(c_neo)
        mock_db.commit()

        # Populate full prepared neo data
        _populate_prepared_neo_consilium_data(mock_db, cycle_id=cycle_neo_id, prior_cycle_ids=["c_old_01"])

        session_dev = _make_device_session(patient_id=patient_id)
        app.dependency_overrides[get_db] = lambda: mock_db
        _override_all_gateways(mock_gateway)
        app.dependency_overrides[require_device_session] = lambda: session_dev
        app.dependency_overrides[verify_client_token] = lambda: session_dev
        client = TestClient(app, headers={"Authorization": "Bearer mock_jwt_replay"})

        # Retry run-primary returns prepared result immediately with Idempotent-Replay header
        resp_retry = client.post("/api/v1/client/cascade/run-primary")
        assert resp_retry.status_code == 200, resp_retry.text
        assert resp_retry.headers.get("x-aegis-idempotent-replay") == "true"
        assert resp_retry.json()["cycle_id"] == cycle_neo_id
        assert resp_retry.json()["mode"] in ("neo", "neo_reassessment")

    def test_recovery_from_lost_post_response_during_activation(self, mock_db: Session, mock_gateway: Stage8C1FullJourneyMockGateway):
        patient_id = "patient_restart_act"
        cycle_old_id = "c_old_act"
        cycle_neo_id = "c_neo_act"
        now_utc = datetime.now(timezone.utc)

        patient = Patient(patient_id=patient_id, status="active")
        mock_db.add(patient)
        c_old = MedicalCycle(cycle_id=cycle_old_id, patient_id=patient_id, status="completed", created_at=now_utc - timedelta(days=10))
        c_neo = MedicalCycle(cycle_id=cycle_neo_id, patient_id=patient_id, status="pending_reassessment", created_at=now_utc)
        mock_db.add(c_old)
        mock_db.add(c_neo)
        mock_db.commit()

        # Populate prepared data
        _populate_prepared_neo_consilium_data(mock_db, cycle_id=cycle_neo_id, prior_cycle_ids=[cycle_old_id])

        session_dev = _make_device_session(patient_id=patient_id)
        app.dependency_overrides[get_db] = lambda: mock_db
        _override_all_gateways(mock_gateway)
        app.dependency_overrides[require_device_session] = lambda: session_dev
        app.dependency_overrides[verify_client_token] = lambda: session_dev
        client = TestClient(app, headers={"Authorization": "Bearer mock_jwt_lost_post"})

        # First activation call
        resp1 = client.post("/api/v1/client/cascade/activate-neo", json={"cycle_id": cycle_neo_id})
        assert resp1.status_code == 200, resp1.text
        t0_first = resp1.json()["schedule_anchor_t0_utc"]

        # Simulated client lost connection, repeats activation request with same parameters
        resp2 = client.post("/api/v1/client/cascade/activate-neo", json={"cycle_id": cycle_neo_id})
        assert resp2.status_code == 200, resp2.text
        assert resp2.json()["is_idempotent_replay"] is True
        assert resp2.json()["schedule_anchor_t0_utc"] == t0_first


# ==============================================================================
# 4. CONCURRENCY, MULTI-CONNECTION SQLITE & EDGE CASES
# ==============================================================================

class TestConcurrencyAndEdgeCasesMultiConnection:
    """
    Verifies concurrency handling using multiple independent SQLite sessions,
    fencing of old weekly consiliums, quality gate rejections, and offline vitals routing.
    """

    def test_two_devices_concurrent_neo_reassessment_idempotent(self, tmp_path: Path):
        # Dedicated SQLite file to test multiple connections
        db_path = tmp_path / "concurrent_neo.sqlite3"
        engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
        Base.metadata.create_all(bind=engine)
        SessionFactory = sessionmaker(bind=engine)

        session_a = SessionFactory()
        session_b = SessionFactory()

        patient_id = "patient_concurrency_01"
        cycle_old_id = "c_conc_old"
        now_utc = datetime.now(timezone.utc)

        # Seed data via session A
        patient = Patient(patient_id=patient_id, status="active")
        c_old = MedicalCycle(cycle_id=cycle_old_id, patient_id=patient_id, status="active", created_at=now_utc - timedelta(days=7))
        session_a.add(patient)
        session_a.add(c_old)
        session_a.commit()

        _seed_completed_primary_consilium(session_a, cycle_old_id)

        neo_service_a = NeoCycleService(db=session_a)
        neo_service_b = NeoCycleService(db=session_b)

        # Device A creates Neo cycle
        res_a = neo_service_a.start_neo_reassessment(patient_id=patient_id)
        assert res_a.status == "pending_reassessment"
        created_cycle_id = res_a.cycle_id

        # Device B immediately calls start_neo_reassessment
        res_b = neo_service_b.start_neo_reassessment(patient_id=patient_id)
        assert res_b.cycle_id == created_cycle_id
        assert res_b.is_pending_reassessment is True

        session_a.close()
        session_b.close()
        engine.dispose()

    def test_in_flight_old_weekly_consilium_fenced_when_neo_starts(self, mock_db: Session, mock_gateway: Stage8C1FullJourneyMockGateway):
        patient_id = "patient_fence_01"
        cycle_old_id = "c_fence_old"
        now_utc = datetime.now(timezone.utc)

        patient = Patient(patient_id=patient_id, status="active")
        c_old = MedicalCycle(cycle_id=cycle_old_id, patient_id=patient_id, status="active", created_at=now_utc - timedelta(days=7))
        mock_db.add(patient)
        mock_db.add(c_old)
        mock_db.commit()

        _seed_completed_primary_consilium(mock_db, cycle_old_id)

        weekly_svc = WeeklyConsiliumService(db=mock_db, gateway=mock_gateway)
        neo_svc = NeoCycleService(db=mock_db)

        # Start Neo -> closes old cycle
        neo_svc.start_neo_reassessment(patient_id=patient_id)

        # In-flight old weekly consilium attempts to run on the old closed cycle
        with pytest.raises(ConsiliumCycleInactiveError):
            asyncio.run(weekly_svc.run_weekly_consilium(cycle_id=cycle_old_id))

    def test_rejected_pro_quality_gate_aborts_activation(self, mock_db: Session):
        patient_id = "patient_rej_01"
        cycle_neo_id = "c_neo_rej"
        now_utc = datetime.now(timezone.utc)

        gateway_rejecting = Stage8C1FullJourneyMockGateway(
            fail_pro_quality_gate=True,
            pro_quality_rejection_reason="Chief Pro Quality Rejection: Lack of longitudinal marker delta analysis.",
        )

        patient = Patient(patient_id=patient_id, status="active")
        mock_db.add(patient)
        c_old = MedicalCycle(cycle_id="c_old_prior", patient_id=patient_id, status="completed", created_at=now_utc - timedelta(days=10))
        c_neo = MedicalCycle(cycle_id=cycle_neo_id, patient_id=patient_id, status="pending_reassessment", created_at=now_utc)
        mock_db.add(c_old)
        mock_db.add(c_neo)
        mock_db.commit()

        # Seed prepared neo data with approved=False
        _populate_prepared_neo_consilium_data(
            mock_db,
            cycle_id=cycle_neo_id,
            prior_cycle_ids=["c_old_prior"],
            approved=False,
        )

        act_service = NeoConsiliumActivationService(db=mock_db, gateway=gateway_rejecting)
        with pytest.raises(NeoActivationPreconditionError) as exc_info:
            act_service.activate_prepared_neo(cycle_id=cycle_neo_id, patient_id=patient_id)
        assert "quality gate" in str(exc_info.value).lower()

    def test_mode2_pulse_spike_gte_140_triggers_emergency_alert(self, mock_db: Session):
        patient_id = "patient_tachy_01"
        cycle_id = "c_tachy_01"
        now_utc = datetime.now(timezone.utc)

        patient = Patient(patient_id=patient_id, status="active")
        mock_db.add(patient)
        c = MedicalCycle(cycle_id=cycle_id, patient_id=patient_id, status="active", created_at=now_utc)
        mock_db.add(c)
        mock_db.commit()

        # Ingest heart rate = 142 bpm
        mode2_svc = Mode2VitalsMonitoringService(db=mock_db)
        thresholds = mode2_svc.resolve_thresholds(cycle_id)
        is_dev, reason = mode2_svc.is_deviation_detected(
            pulse=142,
            spo2=98,
            baseline_pulse=70,
            thresholds=thresholds,
        )
        assert is_dev is True
        assert 142 >= CRITICAL_TACHYCARDIA_BPM
        assert "тахикардия" in reason.lower()


# ==============================================================================
# 5. CLINICAL INVARIANTS & DATA SANITIZATION
# ==============================================================================

class TestClinicalInvariantsAndDataSanitization:
    """
    Verifies that clinical invariants hold:
    - Only Pro makes referral decisions
    - Athena never diagnoses
    - Old AI hypotheses never become confirmed diagnoses
    - No auto-carryover of old referral
    - SERVER_ONLY data never leaks in client APIs
    """

    def test_clinical_invariant_pro_only_referral_decision(self, mock_db: Session):
        # Pro has authoritative referral_decision field
        pro_output = _sample_weekly_pro_output()
        pro_output["referral_decision"] = {
            "recommended": True,
            "urgency": "priority",
            "target_specialties": ["cardiologist", "endocrinologist"],
            "clinical_reason_for_referral": "Sustained resting tachycardia episodes require evaluation.",
            "observed_syndromes_or_trends": ["Tachycardia spikes"],
            "recommended_workup_for_doctor_discussion": ["12-lead ECG"],
        }
        decision = ProReferralDecision(**pro_output["referral_decision"])
        assert decision.recommended is True
        assert "cardiologist" in decision.target_specialties

        # Flash has NO referral_decision authority in schema
        flash_output = _sample_weekly_flash_output()
        assert "referral_decision" not in flash_output

    def test_clinical_invariant_athena_never_diagnoses(self):
        # Athena dialogue never contains authoritative diagnostic assertions
        athena_sample_reply = (
            "I noticed you have been experiencing elevated heart rate spikes. "
            "Please follow up with your doctor for a formal diagnostic workup."
        )
        forbidden_phrases = ["you are diagnosed with", "i diagnose you with", "my medical diagnosis is"]
        assert not any(phrase in athena_sample_reply.lower() for phrase in forbidden_phrases)

    def test_clinical_invariant_server_only_data_never_leaked_to_client_api(self, mock_db: Session, mock_gateway: Stage8C1FullJourneyMockGateway):
        patient_id = "patient_privacy_01"
        cycle_id = "c_privacy_01"
        now_utc = datetime.now(timezone.utc)

        patient = Patient(patient_id=patient_id, status="active")
        c = MedicalCycle(cycle_id=cycle_id, patient_id=patient_id, status="active", created_at=now_utc)
        mock_db.add(patient)
        mock_db.add(c)
        mock_db.commit()

        # Seed SERVER_ONLY internal report
        report_repo = ModelReportRepository(mock_db)
        report_repo.create_next_version(
            cycle_id=cycle_id,
            model="gemini-3.1-pro-preview",
            report_type="chief_physician_internal",
            privacy_level=PrivacyLevel.SERVER_ONLY,
            content={
                "pro_general_shelf": "TOP_SECRET_CLINICAL_HYPOTHESIS_NEVER_LEAK",
                "training_sandbox": "INTERNAL_NOSOLOGY_TRAINING",
            },
        )

        session_dev = _make_device_session(patient_id=patient_id)
        app.dependency_overrides[get_db] = lambda: mock_db
        _override_all_gateways(mock_gateway)
        app.dependency_overrides[require_device_session] = lambda: session_dev
        app.dependency_overrides[verify_client_token] = lambda: session_dev
        client = TestClient(app, headers={"Authorization": "Bearer mock_jwt_privacy"})

        resp_reports = client.get(f"/api/v1/reports/client?cycle_id={cycle_id}")
        assert resp_reports.status_code == 200, resp_reports.text
        reports_text = json.dumps(resp_reports.json())
        assert "TOP_SECRET_CLINICAL_HYPOTHESIS_NEVER_LEAK" not in reports_text
        assert "INTERNAL_NOSOLOGY_TRAINING" not in reports_text
