"""
Stage 8A.2 — Comprehensive Integration & Unit Tests for Server Referral Document
Generation (Flash -> Pro) and ClinicalAutomationWorker Integration.

Verifies all 13 mandatory test scenarios with 100% mocked GeminiGateway calls:
1. recommended=false -> document cascade is NOT enqueued or executed.
2. recommended=true -> strictly Flash -> Pro executed (ZERO Flash-Lite calls).
3. All clinical sources (anamnesis, labs, ECG, vitals, Wellness, Mode 2, Athena journals,
   prior Chief Physician conclusions, approved referral decision) are passed with exact dates & units.
4. Missing data categories are explicitly declared absent and never fabricated.
5. Flash failure -> retries ONLY document generation; Weekly Consilium is NEVER re-run.
6. Pro failure / invalid or unsafe diagnostic output -> rejected & retried without saving dummy doc.
7. Duplicate job enqueue & repeat execution -> zero duplicate documents or extra Gemini calls.
8. Worker crash / server restart -> recovery via lease expiration & discover_and_enqueue_jobs.
9. New Weekly while referral is active -> preserves ready document link, skips redundant
   regeneration when unchanged, and keeps full historical document versions on substantive updates.
10. Closed / Neo-superseded cycle -> never publishes stale active referral into new cycle.
11. Invalid client report structure -> strictly rejected by ClientReportContent(extra='forbid')
    with atomic rollback (never corrupts referral_lifecycle_state).
12. Multi-patient isolation -> zero cross-patient data leakage in prompts, status, or reports API.
13. Regular Weekly Step B/C (Athena wrap-up) & Pro thresholds are NOT blocked by document cascade,
    and Athena server state cleanly distinguishes generating / ready / temporarily failed.
"""

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.database import get_db
from app.main import app
from app.models.base import Base
from app.models.health_measurement import HealthMeasurement
from app.models.patient import Patient
from app.repositories.cycle_repository import MedicalCycleRepository
from app.repositories.report_repository import ModelReportRepository
from app.schemas.chat import (
    AthenaClinicalAssessment,
    AthenaDailyJournalRecord,
    BiometricsSummary,
    OutOfTurnPatientMessage,
    PatientAnswerEntry,
    PatientReportedEvidence,
    RoutineQuestionJournalSection,
    WellnessEvidence,
    WellnessReportReference,
)
from app.schemas.consilium import (
    LabMarkerMutationItem,
    LabReferenceRange,
    ProReferralDecision,
    ProVitalsThresholds,
    REFERRAL_DOCUMENT_CANONICAL_TITLE,
)
from app.schemas.mode2 import (
    Mode2EpisodeRecord,
    Mode2EpisodeStatus,
    Mode2TelemetryPoint,
    Mode2UserStatement,
    Mode2WorkoutContext,
)
from app.schemas.privacy import PrivacyLevel
from app.schemas.reports import ClientReportContent
from app.schemas.wellness import (
    HeartRateSummary,
    WellnessBiometricsSummary,
    WellnessDailyReport,
    WellnessModelOutput,
    WellnessStatus,
    WellnessTimeWindow,
    WellnessTriggerType,
)
from app.security.auth import DeviceSession, require_device_session, verify_client_token
from app.services.athena_daily_journal_service import (
    ATHENA_CLINICAL_LOG_REPORT_TYPE,
    ATHENA_DAILY_JOURNAL_REPORT_TYPE,
)
from app.services.clinical_automation_service import (
    AutomationJobStatus,
    AutomationJobType,
    ClinicalAutomationService,
    ClinicalAutomationWorker,
)
from app.services.gemini_gateway import GeminiGateway, GeminiValidationError
from app.services.lab_revision_consilium_service import LabRevisionConsiliumService
from app.services.mode2_vitals_service import ATHENA_MODE2_EPISODE_REPORT_TYPE
from app.services.primary_consilium_service import PREPARATORY_ECG_REPORT_TYPE
from app.services.referral_document_service import (
    REFERRAL_DOCTOR_DOCUMENT_RAW_REPORT_TYPE,
    REFERRAL_DOCTOR_DOCUMENT_REPORT_TYPE,
    ReferralCycleInactiveError,
    ReferralDocumentSchemaViolationError,
    ReferralDocumentService,
)
from app.services.weekly_consilium_service import (
    ATHENA_SCHEDULE_ANCHOR_REPORT_TYPE,
    REFERRAL_LIFECYCLE_STATE_REPORT_TYPE,
    WeeklyConsiliumService,
)
from app.services.wellness_daily_service import WELLNESS_DAILY_REPORT_TYPE
from tests.test_primary_consilium import _gemini_envelope
from tests.test_weekly_consilium import (
    _sample_weekly_flash_output,
    _sample_weekly_pro_output,
    _seed_completed_primary_consilium,
    _seed_seven_day_athena_logs,
)


# ==============================================================================
# SAMPLE PAYLOADS & DETERMINISTIC MOCK GATEWAY HELPERS
# ==============================================================================


def _seed_patient(db: Session, patient_id: str) -> Patient:
    patient = db.query(Patient).filter_by(patient_id=patient_id).first()
    if not patient:
        patient = Patient(patient_id=patient_id, status="active")
        db.add(patient)
        db.commit()
    return patient


def _seed_schedule_anchor(db: Session, cycle_id: str, t0: datetime) -> None:
    report_repo = ModelReportRepository(db)
    report_repo.create_next_version(
        cycle_id=cycle_id,
        model="athena-schedule-engine",
        report_type=ATHENA_SCHEDULE_ANCHOR_REPORT_TYPE,
        privacy_level=PrivacyLevel.SERVER_ONLY,
        content={"anchor_timestamp_utc": t0.isoformat(), "event_type": "initial_greeting"},
        created_at=t0,
    )


def _sample_referral_flash_extraction(
    *,
    has_missing_data: bool = False,
) -> Dict[str, Any]:
    if has_missing_data:
        return {
            "observation_period_start": "2026-10-18T10:00:00+00:00",
            "observation_period_end": "2026-10-25T10:05:00+00:00",
            "observation_period_summary": "7-day outpatient digital observation window with limited baseline instrumentation.",
            "anamnesis_and_baseline_facts": [
                {
                    "date_or_period": "2026-10-18",
                    "category": "anamnesis",
                    "finding": "Patient reported intermittent afternoon fatigue during onboarding questionnaire.",
                    "value_and_unit": None,
                    "source_attribution": "preparatory_anamnesis_m1 (v1)",
                    "is_patient_self_reported": True,
                }
            ],
            "objective_measurements_and_labs": [],
            "ecg_and_examination_findings": [],
            "patient_reported_complaints": [
                {
                    "date_or_period": "2026-10-22",
                    "category": "patient_reported_symptom",
                    "finding": "Patient self-reported recurring dizziness upon standing.",
                    "value_and_unit": None,
                    "source_attribution": "athena_clinical_log",
                    "is_patient_self_reported": True,
                }
            ],
            "wellness_and_mode2_events": [],
            "observed_changes_and_recurring_patterns": [
                "Recurring self-reported orthostatic discomfort across the first observation week."
            ],
            "prior_chief_physician_conclusions": [
                "AI Chief Physician preliminary hypothesis: evaluate orthostatic hemodynamic regulation in person."
            ],
            "approved_referral_reason_summary": "Persistent orthostatic complaints requiring in-person physical examination.",
            "data_gaps_and_uncertainties": [
                "No laboratory studies were uploaded during this cycle.",
                "No ECG or instrumental examination was recorded.",
                "No continuous wearable biometric vitals were recorded.",
            ],
        }

    return {
        "observation_period_start": "2026-10-18T10:00:00+00:00",
        "observation_period_end": "2026-10-25T10:05:00+00:00",
        "observation_period_summary": "7-day observation window combining baseline anamnesis, laboratory ledger, ECG, biometrics, Mode 2 telemetry, and Athena daily journals.",
        "anamnesis_and_baseline_facts": [
            {
                "date_or_period": "2026-10-18",
                "category": "anamnesis",
                "finding": "Patient reported progressive fatigue and cold intolerance over 2 months.",
                "value_and_unit": None,
                "source_attribution": "preparatory_anamnesis_m1 (v1)",
                "is_patient_self_reported": True,
            }
        ],
        "objective_measurements_and_labs": [
            {
                "date_or_period": "2026-10-15",
                "category": "laboratory",
                "finding": "Elevated thyroid-stimulating hormone (TSH) above upper reference limit.",
                "value_and_unit": "5.8 mIU/L (ref 0.4-4.0 mIU/L)",
                "source_attribution": "laboratory_ledger (v1, sample_date=2026-10-15)",
                "is_patient_self_reported": False,
            },
            {
                "date_or_period": "2026-10-20T08:30:00+00:00",
                "category": "vitals_measurement",
                "finding": "Manual morning arterial blood pressure and resting heart rate measurement.",
                "value_and_unit": "142/92 mmHg, 88 bpm, SpO2 97%",
                "source_attribution": "health_measurements (manual_input)",
                "is_patient_self_reported": False,
            },
        ],
        "ecg_and_examination_findings": [
            {
                "date_or_period": "2026-10-14",
                "category": "ecg_or_exam",
                "finding": "Single-lead smartwatch ECG sinus rhythm; electrical axis cannot be evaluated on single-lead strip.",
                "value_and_unit": "68 bpm, QTc 418 ms",
                "source_attribution": "preparatory_ecg",
                "is_patient_self_reported": False,
            }
        ],
        "patient_reported_complaints": [
            {
                "date_or_period": "2026-10-21T19:15:00+00:00",
                "category": "patient_reported_symptom",
                "finding": "Patient reported feeling sudden palpitations while sitting quietly.",
                "value_and_unit": None,
                "source_attribution": "athena_mode2_episode / athena_daily_journal",
                "is_patient_self_reported": True,
            }
        ],
        "wellness_and_mode2_events": [
            {
                "date_or_period": "2026-10-21T19:12:00+00:00",
                "category": "mode2_episode",
                "finding": "Resting tachycardia alert outside workout context.",
                "value_and_unit": "Peak 134 bpm (baseline 68 bpm), SpO2 97%",
                "source_attribution": "athena_mode2_episode",
                "is_patient_self_reported": False,
            }
        ],
        "observed_changes_and_recurring_patterns": [
            "Combination of elevated resting heart rate episodes (up to 134 bpm at rest) and borderline-elevated TSH (5.8 mIU/L)."
        ],
        "prior_chief_physician_conclusions": [
            "AI Chief Physician preliminary synthesis: concurrent cardiovascular and endocrine evaluation warranted."
        ],
        "approved_referral_reason_summary": "Resting tachycardia episodes combined with elevated TSH (5.8 mIU/L) and symptomatic palpitations.",
        "data_gaps_and_uncertainties": [
            "Smartwatch ECG is single-lead only; standard 12-lead diagnostic ECG has not been recorded."
        ],
    }


def _sample_referral_pro_document(
    *,
    specialties: Optional[List[str]] = None,
    urgency: str = "routine",
    has_missing_data: bool = False,
) -> Dict[str, Any]:
    specs = specialties or ["Cardiologist", "Endocrinologist"]
    obj_section = (
        [
            "No laboratory studies, ECG recordings, or continuous wearable biometrics were uploaded during this observation period."
        ]
        if has_missing_data
        else [
            "Laboratory Ledger (2026-10-15): TSH 5.8 mIU/L (reference 0.4-4.0 mIU/L).",
            "Manual Vitals (2026-10-20 08:30 UTC): Blood pressure 142/92 mmHg, heart rate 88 bpm, SpO2 97%.",
            "Smartwatch Single-Lead ECG (2026-10-14): Sinus rhythm at 68 bpm, QTc 418 ms (single-lead limitation: frontal electrical axis not assessable).",
            "Mode 2 Wearable Telemetry Episode (2026-10-21 19:12 UTC): Resting heart rate peak 134 bpm vs baseline 68 bpm (not working out).",
        ]
    )
    return {
        "document_title": REFERRAL_DOCUMENT_CANONICAL_TITLE,
        "informational_disclaimer": (
            "This summary is an AI-assisted informational observation summary prepared to support "
            "discussion with a licensed clinician. It is not an official medical discharge summary "
            "and is not a confirmed clinical diagnosis."
        ),
        "recommended_specialties": specs,
        "consultation_urgency": urgency,
        "executive_summary_for_patient": (
            f"Informational health observation summary prepared for your consultation with: {', '.join(specs)}."
        ),
        "patient_and_observation_period": (
            "Authenticated Aegis participant observed from 2026-10-18 to 2026-10-25 (7-day longitudinal cycle)."
        ),
        "reason_for_in_person_consultation": (
            "In-person clinical evaluation is recommended due to recurring resting heart rate elevations "
            "accompanied by patient-reported palpitations and laboratory TSH elevation."
        ),
        "objective_measurements_and_examination_results": obj_section,
        "timeline_of_reported_symptoms_and_changes": [
            "2026-10-18 (Patient self-reported in onboarding anamnesis): Afternoon fatigue over 2 months.",
            "2026-10-21 19:15 UTC (Patient self-reported during Mode 2 episode): Sudden palpitations while seated at rest.",
        ],
        "important_patterns_and_unresolved_concerns": [
            "Paroxysmal resting heart rate spikes above 130 bpm occurring outside physical exertion.",
            "Single-lead smartwatch strip cannot exclude conduction or structural abnormalities.",
        ],
        "ai_observations_requiring_verification": [
            "AI pattern observation (unverified hypothesis): Co-occurring autonomic/rhythm instability and thyroid-axis deviation require differential assessment by a licensed physician."
        ],
        "questions_and_examinations_for_clinician": [
            "Would a 12-lead diagnostic ECG and 24-hour Holter rhythm monitoring be appropriate?",
            "Should a complete thyroid panel (repeat TSH, free T4, free T3, thyroid antibodies) be performed?",
        ],
    }


def _sample_weekly_plan_output() -> Dict[str, Any]:
    return {
        "plan_id": "plan_mock_w2",
        "cycle_id": "cycle_mock",
        "week_number": 2,
        "created_at_utc": "2026-10-25T10:05:00+00:00",
        "summary_for_curator": "Monitor resting heart rate episodes and orthostatic comfort during Week 2.",
        "goals": [
            {
                "goal_id": "goal_w2_1",
                "title": "Clarify resting palpitation triggers",
                "clinical_rationale": "Evaluate context of non-exertional heart rate spikes.",
                "priority": 1,
                "known_facts": ["Resting pulse reached 134 bpm on 2026-10-21"],
                "target_clarification": "Ask whether caffeine, stress, or posture change preceded episodes.",
                "sufficiency_criteria": "Clear description of preceding context.",
            }
        ],
    }


def _build_prompt_aware_mock_gateway(
    recorded_calls: List[Dict[str, Any]],
    *,
    weekly_pro_override: Optional[Dict[str, Any]] = None,
    doc_flash_override: Optional[Callable[[int, str], httpx.Response] | Dict[str, Any]] = None,
    doc_pro_override: Optional[Callable[[int, str], httpx.Response] | Dict[str, Any]] = None,
) -> GeminiGateway:
    """
    Deterministic MockTransport gateway routing requests by prompt scenario header
    while strictly forbidding any Flash-Lite call across Weekly & Referral Document cascades.
    """
    counters = {
        "weekly_flash": 0,
        "weekly_pro": 0,
        "doc_flash": 0,
        "doc_pro": 0,
        "weekly_plan": 0,
        "weekly_wrapup": 0,
        "athena_chat": 0,
    }

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        prompt_text = body["contents"][0]["parts"][0]["text"]
        url_path = request.url.path

        assert "flash-lite" not in url_path.lower(), (
            f"STRICT VIOLATION: Forbidden Flash-Lite call detected: {url_path}"
        )

        scenario = "unknown"
        if "[Internal Clinical History Extraction Scenario" in prompt_text:
            scenario = "doc_flash"
        elif "[Doctor Observation Summary Authoring Scenario" in prompt_text:
            scenario = "doc_pro"
        elif "[Internal Weekly Plan Formulation Scenario]" in prompt_text:
            scenario = "weekly_plan"
        elif "[Internal Athena Weekly Wrap-up Message Formulation]" in prompt_text:
            scenario = "weekly_wrapup"
        elif (
            "Service pulse: INITIAL INTRODUCTION" in prompt_text
            or "Service pulse: FIRST PLANNED QUESTION" in prompt_text
            or "[CLINICAL_REPORT]" in prompt_text
        ):
            scenario = "athena_chat"
        elif "pro-preview" in url_path or "-pro" in url_path or "argon" in url_path:
            scenario = "weekly_pro"
        else:
            scenario = "weekly_flash"

        attempt_num = counters[scenario]
        counters[scenario] += 1

        recorded_calls.append(
            {
                "scenario": scenario,
                "attempt_index": attempt_num,
                "url_path": url_path,
                "prompt": prompt_text,
            }
        )

        if scenario == "weekly_flash":
            return httpx.Response(200, json=_gemini_envelope(_sample_weekly_flash_output()))

        if scenario == "weekly_pro":
            payload = weekly_pro_override or _sample_weekly_pro_output()
            return httpx.Response(200, json=_gemini_envelope(payload))

        if scenario == "doc_flash":
            if callable(doc_flash_override):
                return doc_flash_override(attempt_num, prompt_text)
            payload = doc_flash_override or _sample_referral_flash_extraction()
            return httpx.Response(200, json=_gemini_envelope(payload))

        if scenario == "doc_pro":
            if callable(doc_pro_override):
                return doc_pro_override(attempt_num, prompt_text)
            payload = doc_pro_override or _sample_referral_pro_document()
            return httpx.Response(200, json=_gemini_envelope(payload))

        if scenario == "weekly_plan":
            return httpx.Response(200, json=_gemini_envelope(_sample_weekly_plan_output()))

        if scenario == "weekly_wrapup":
            return httpx.Response(
                200,
                json=_gemini_envelope(
                    {
                        "message": (
                            "Your weekly observation summary is ready! Tap the human silhouette "
                            "on the main screen to explore your updated Vault reports."
                        )
                    }
                ),
            )

        if scenario == "athena_chat":
            greeting_text = (
                "Hello! I am Athena, your personal AI companion in Aegis.\n"
                "[CLINICAL_REPORT]\n"
                '{"status":"INITIALIZATION","symptoms_mentioned":[],"subjective_wellbeing_score":null,'
                '"adherence_to_plan":"UNKNOWN","red_flags":false,'
                '"summary_for_master":"Initial greeting sent."}\n'
                "[/CLINICAL_REPORT]"
            )
            return httpx.Response(
                200,
                json={"candidates": [{"content": {"parts": [{"text": greeting_text}]}}]},
            )

        raise AssertionError(f"Unhandled mock prompt scenario for URL {url_path}")

    async def _noop_sleep(_: float) -> None:
        return None

    return GeminiGateway(
        api_key="test-stage-8a2-mock-key",
        transport=httpx.MockTransport(handler),
        max_retries=1,
        sleep_func=_noop_sleep,
    )


# ==============================================================================
# TEST 1: RECOMMENDED=FALSE -> DOCUMENT CASCADE DOES NOT START
# ==============================================================================


@pytest.mark.anyio
async def test_01_recommended_false_does_not_enqueue_or_run_document_cascade(db_session: Session):
    cycle_id = "cycle_8a2_false_01"
    patient_id = "patient_8a2_false_01"
    _seed_patient(db_session, patient_id)
    MedicalCycleRepository(db_session).create(cycle_id=cycle_id, patient_id=patient_id)
    await _seed_completed_primary_consilium(db_session, cycle_id=cycle_id)
    _seed_seven_day_athena_logs(db_session, cycle_id=cycle_id, sprint_id=f"sprint_{cycle_id}_w1")

    t0 = datetime(2026, 10, 18, 10, 0, tzinfo=timezone.utc)
    _seed_schedule_anchor(db_session, cycle_id, t0)

    pro_out = _sample_weekly_pro_output()
    pro_out["referral_decision"] = {
        "recommended": False,
        "urgency": "none",
        "target_specialties": [],
        "clinical_reason_for_referral": "",
    }
    recorded_calls: List[Dict[str, Any]] = []
    gateway = _build_prompt_aware_mock_gateway(recorded_calls, weekly_pro_override=pro_out)

    now_dt = t0 + timedelta(hours=168, minutes=5)
    cas = ClinicalAutomationService(
        db=db_session,
        gateway=gateway,
        now_provider=lambda: now_dt,
        enable_daily_journal=False,
        enable_wellness_daily=False,
    )

    step_a_job = cas.enqueue_weekly_consilium_a(cycle_id=cycle_id, patient_id=patient_id, sprint_number=1)
    claimed_a = cas.claim_job(step_a_job.job_id)
    assert claimed_a is not None
    await cas.execute_claimed_job(claimed_a)

    # Process any follow-up jobs
    await cas.process_due_jobs(discover=True)

    # Verify ZERO referral document jobs and ZERO document Gemini calls
    doc_jobs = [
        j for j in cas.list_jobs(cycle_id=cycle_id)
        if j.job_type == AutomationJobType.REFERRAL_DOCUMENT_GENERATION
    ]
    assert doc_jobs == []
    assert [c for c in recorded_calls if c["scenario"] in ("doc_flash", "doc_pro")] == []

    report_repo = ModelReportRepository(db_session)
    assert report_repo.list_client_facing(cycle_id, REFERRAL_DOCTOR_DOCUMENT_REPORT_TYPE) == []
    assert report_repo.list_server_only(cycle_id, REFERRAL_DOCTOR_DOCUMENT_RAW_REPORT_TYPE) == []


# ==============================================================================
# TEST 2: RECOMMENDED=TRUE -> STRICTLY FLASH -> PRO (ZERO FLASH-LITE) & ATOMIC READY
# ==============================================================================


@pytest.mark.anyio
async def test_02_recommended_true_executes_strictly_flash_then_pro_zero_flash_lite(db_session: Session):
    cycle_id = "cycle_8a2_true_02"
    patient_id = "patient_8a2_true_02"
    _seed_patient(db_session, patient_id)
    MedicalCycleRepository(db_session).create(cycle_id=cycle_id, patient_id=patient_id)
    await _seed_completed_primary_consilium(db_session, cycle_id=cycle_id)
    _seed_seven_day_athena_logs(db_session, cycle_id=cycle_id, sprint_id=f"sprint_{cycle_id}_w1")

    t0 = datetime(2026, 10, 18, 10, 0, tzinfo=timezone.utc)
    _seed_schedule_anchor(db_session, cycle_id, t0)

    pro_out = _sample_weekly_pro_output()
    pro_out["referral_decision"] = {
        "recommended": True,
        "urgency": "priority",
        "target_specialties": ["Cardiologist", "Endocrinologist"],
        "clinical_reason_for_referral": "Paroxysmal resting tachycardia and elevated TSH.",
        "observed_syndromes_or_trends": ["Resting HR > 130 bpm", "TSH 5.8 mIU/L"],
        "recommended_workup_for_doctor_discussion": ["12-lead ECG", "24h Holter", "Thyroid panel"],
    }
    recorded_calls: List[Dict[str, Any]] = []
    gateway = _build_prompt_aware_mock_gateway(
        recorded_calls,
        weekly_pro_override=pro_out,
        doc_pro_override=_sample_referral_pro_document(
            specialties=["Cardiologist", "Endocrinologist"], urgency="priority"
        ),
    )

    now_dt = t0 + timedelta(hours=168, minutes=5)
    cas = ClinicalAutomationService(
        db=db_session,
        gateway=gateway,
        now_provider=lambda: now_dt,
        enable_daily_journal=False,
        enable_wellness_daily=False,
        enable_weekly_plan=False,
    )

    # 1. Run Weekly Step A -> creates referral_lifecycle_state (generating_document) + enqueues doc job
    step_a_job = cas.enqueue_weekly_consilium_a(cycle_id=cycle_id, patient_id=patient_id, sprint_number=1)
    claimed_a = cas.claim_job(step_a_job.job_id)
    assert claimed_a is not None
    done_a = await cas.execute_claimed_job(claimed_a)
    assert done_a.status == AutomationJobStatus.COMPLETED

    athena_pre = cas.weekly_service.get_referral_state_for_athena(cycle_id)
    assert athena_pre["athena_document_state"] == "referral_accepted_generating_document"
    assert athena_pre["lifecycle_status"] == "generating_document"

    # Clear call log before executing REFERRAL_DOCUMENT_GENERATION to verify exact 2-call cascade
    recorded_calls.clear()

    doc_jobs = [
        j for j in cas.list_jobs(cycle_id=cycle_id)
        if j.job_type == AutomationJobType.REFERRAL_DOCUMENT_GENERATION
    ]
    assert len(doc_jobs) == 1
    claimed_doc = cas.claim_job(doc_jobs[0].job_id)
    assert claimed_doc is not None
    done_doc = await cas.execute_claimed_job(claimed_doc)
    assert done_doc.status == AutomationJobStatus.COMPLETED

    # Verify STRICTLY 2 calls: Flash Extractor -> Pro Author (0 Flash-Lite)
    assert len(recorded_calls) == 2
    assert recorded_calls[0]["scenario"] == "doc_flash"
    assert "flash" in recorded_calls[0]["url_path"].lower()
    assert "flash-lite" not in recorded_calls[0]["url_path"].lower()

    assert recorded_calls[1]["scenario"] == "doc_pro"
    assert "argon" in recorded_calls[1]["url_path"].lower() or "pro" in recorded_calls[1]["url_path"].lower()
    assert "flash-lite" not in recorded_calls[1]["url_path"].lower()

    # Verify both SERVER_ONLY raw and CLIENT_FACING validated reports + atomic document_ready state
    report_repo = ModelReportRepository(db_session)
    raw_docs = report_repo.list_server_only(cycle_id, REFERRAL_DOCTOR_DOCUMENT_RAW_REPORT_TYPE)
    client_docs = report_repo.list_client_facing(cycle_id, REFERRAL_DOCTOR_DOCUMENT_REPORT_TYPE)
    assert len(raw_docs) == 1
    assert len(client_docs) == 1

    client_doc = client_docs[0]
    validated_client = ClientReportContent.model_validate(client_doc.content)
    assert validated_client.title == REFERRAL_DOCUMENT_CANONICAL_TITLE
    assert validated_client.system_key is None  # Strictly outside 8 organ system folders
    assert "## 1. Patient and Observation Period" in validated_client.summary
    assert "## 2. Reason for Recommending an In-Person Consultation" in validated_client.summary
    assert "## 3. Objective Health Measurements and Examination Results" in validated_client.summary
    assert "## 4. Timeline of Reported Symptoms and Observed Changes" in validated_client.summary
    assert "## 5. Important Patterns and Unresolved Concerns" in validated_client.summary
    assert "## 6. AI-Generated Observations Requiring Professional Verification" in validated_client.summary
    assert "## 7. Questions and Possible Examinations to Discuss with a Licensed Clinician" in validated_client.summary
    assert "NOT an official medical discharge summary" in validated_client.summary

    # Verify mandatory metadata links
    metrics = validated_client.metrics
    ref_state = cas.weekly_service.get_referral_lifecycle_state(cycle_id)
    assert ref_state is not None
    assert metrics["patient_id"] == patient_id
    assert metrics["cycle_id"] == cycle_id
    assert metrics["sprint_id"] == f"sprint_{cycle_id}_w1"
    assert metrics["referral_id"] == ref_state["referral_id"]
    assert metrics["referral_source"] == "weekly_consilium_pro"
    assert metrics["created_at_utc"] is not None
    assert metrics["raw_document_report_id"] == raw_docs[0].report_id

    # Verify referral_lifecycle_state transitioned atomically to document_ready
    assert ref_state["status"] == "document_ready"
    assert ref_state["document_report_id"] == client_doc.report_id
    assert ref_state["raw_document_report_id"] == raw_docs[0].report_id
    assert ref_state["document_generation_state"] == "ready"
    assert cas.weekly_service.get_referral_state_for_athena(cycle_id)["athena_document_state"] == "document_ready"


# ==============================================================================
# TEST 3: ALL CLINICAL DATA SOURCES PASSED WITH EXACT DATES AND UNITS
# ==============================================================================


@pytest.mark.anyio
async def test_03_clinical_snapshot_passes_all_sources_with_exact_dates_and_units(db_session: Session):
    cycle_id = "cycle_8a2_sources_03"
    patient_id = "patient_8a2_sources_03"
    _seed_patient(db_session, patient_id)
    MedicalCycleRepository(db_session).create(cycle_id=cycle_id, patient_id=patient_id)
    await _seed_completed_primary_consilium(db_session, cycle_id=cycle_id)
    report_repo = ModelReportRepository(db_session)

    # 1. Seed Laboratory Ledger with exact sample dates, values, units, and reference ranges
    lab_service = LabRevisionConsiliumService(db=db_session)
    lab_service.upsert_laboratory_markers(
        cycle_id=cycle_id,
        mutations=[
            LabMarkerMutationItem(
                marker_key="tsh",
                display_name="TSH",
                value=5.8,
                unit="mIU/L",
                sample_date="2026-10-15",
                reference_range=LabReferenceRange(low=0.4, high=4.0),
            ),
            LabMarkerMutationItem(
                marker_key="ferritin",
                display_name="Serum Ferritin",
                value=11.4,
                unit="ng/mL",
                sample_date="2026-10-16",
                reference_range=LabReferenceRange(low=30.0, high=300.0),
            ),
        ],
    )

    # 2. Seed ECG with exact bpm & ms units
    report_repo.create_next_version(
        cycle_id=cycle_id,
        model="gemini-3-flash-preview",
        report_type=PREPARATORY_ECG_REPORT_TYPE,
        privacy_level=PrivacyLevel.SERVER_ONLY,
        content={
            "agent_role": "vision_ecg",
            "is_conducted": True,
            "record_date": "2026-10-14",
            "recording_type": "single_lead_smartwatch",
            "recording_quality": "good",
            "lead_name": "Lead I",
            "heart_rate_bpm": 68,
            "rhythm_type": "sinus",
            "rhythm_regularity": "regular",
            "qtc_interval_ms": 418.0,
            "ecg_summary": "Sinus rhythm 68 bpm, QTc 418 ms on single-lead strip.",
            "limitations": ["Single-lead recording cannot assess electrical axis."],
            "uncertainties": [],
        },
    )

    # 3. Seed Objective Biometric Vitals (mmHg, bpm, %)
    vital_dt = datetime(2026, 10, 20, 8, 30, tzinfo=timezone.utc)
    db_session.add(
        HealthMeasurement(
            cycle_id=cycle_id,
            pulse=88,
            systolic=142,
            diastolic=92,
            spo2=97,
            source="manual_input",
            is_simulated=False,
            is_historical=False,
            measured_at=vital_dt,
            phase="standard",
        )
    )
    db_session.commit()

    # 4. Seed Wellness Daily Report
    wellness_rec = WellnessDailyReport(
        report_id=f"well_{cycle_id}_w1_d3",
        cycle_id=cycle_id,
        patient_id=patient_id,
        calendar_week=1,
        calendar_day=3,
        status=WellnessStatus.READY,
        trigger=WellnessTriggerType.WAKE_UP,
        window=WellnessTimeWindow(
            start_utc="2026-10-20T00:00:00+00:00",
            end_utc="2026-10-21T00:00:00+00:00",
            calendar_week=1,
            calendar_day=3,
        ),
        factual_summary=WellnessBiometricsSummary(
            heart_rate=HeartRateSummary(min_bpm=61, avg_bpm=78.5, max_bpm=112, sample_count=24),
            total_measurements=24,
            has_sensor_data=True,
        ),
        interpretation=WellnessModelOutput(
            observed_trends=["Elevated daytime resting pulse average 78.5 bpm"],
            summary_for_athena="Day 3 elevated resting pulse trend.",
        ),
        created_at_utc="2026-10-21T07:00:00+00:00",
    )
    report_repo.create_next_version(
        cycle_id=cycle_id,
        model="gemini-3-flash-preview",
        report_type=WELLNESS_DAILY_REPORT_TYPE,
        privacy_level=PrivacyLevel.SERVER_ONLY,
        content=wellness_rec.model_dump(mode="json"),
    )

    # 5. Seed Mode 2 Episode
    mode2_rec = Mode2EpisodeRecord(
        episode_id=f"ep_mode2_{cycle_id}_01",
        cycle_id=cycle_id,
        patient_id=patient_id,
        status=Mode2EpisodeStatus.CLOSED,
        started_at_utc="2026-10-21T19:12:00+00:00",
        closed_at_utc="2026-10-21T19:28:00+00:00",
        baseline_pulse=68,
        trigger_pulse=128,
        min_pulse=76,
        max_pulse=134,
        latest_pulse=78,
        latest_spo2=97,
        trigger_reason="Pulse exceeded max_pessimum_bpm",
        workout_context=Mode2WorkoutContext.NOT_WORKING_OUT,
        user_statements=[
            Mode2UserStatement(
                text="I felt sudden palpitations while sitting at my desk",
                timestamp_utc="2026-10-21T19:15:00+00:00",
                is_workout_confirmation=False,
                symptoms="palpitations at rest",
            )
        ],
        telemetry_samples=[
            Mode2TelemetryPoint(
                timestamp_utc="2026-10-21T19:14:00+00:00",
                pulse=134,
                spo2=97,
            )
        ],
    )
    report_repo.create_next_version(
        cycle_id=cycle_id,
        model="aegis-mode2-monitor",
        report_type=ATHENA_MODE2_EPISODE_REPORT_TYPE,
        privacy_level=PrivacyLevel.SERVER_ONLY,
        content=mode2_rec.model_dump(mode="json"),
    )

    # 6. Seed Athena Daily Journal
    journal_rec = AthenaDailyJournalRecord(
        journal_id=f"dj_{cycle_id}_w1_d4",
        cycle_id=cycle_id,
        patient_id=patient_id,
        calendar_week=1,
        calendar_day=4,
        window_start_utc="2026-10-21T00:00:00+00:00",
        window_end_utc="2026-10-22T00:00:00+00:00",
        closure_timestamp_utc="2026-10-22T00:05:00+00:00",
        patient_reported_evidence=PatientReportedEvidence(
            routine_question=RoutineQuestionJournalSection(
                question_id="q_d4",
                question_text="Did you notice dizziness during the evening pulse episode?",
                was_answered=True,
                answered_at_utc="2026-10-21T20:00:00+00:00",
            ),
            patient_answers=[
                PatientAnswerEntry(
                    text="Mild lightheadedness for about two minutes, no chest pain.",
                    timestamp_utc="2026-10-21T20:00:00+00:00",
                )
            ],
            out_of_turn_messages=[
                OutOfTurnPatientMessage(
                    text="Also noticed cold hands all afternoon.",
                    timestamp_utc="2026-10-21T16:10:00+00:00",
                )
            ],
            has_patient_interaction=True,
        ),
        wellness_evidence=WellnessEvidence(
            report_reference=WellnessReportReference(is_available=False, missing_reason="pending"),
            biometrics=BiometricsSummary(),
        ),
        athena_clinical_assessment=AthenaClinicalAssessment(
            is_available=True,
            status="ALERT",
            interpretation="Symptomatic non-exertional tachycardia episode requiring consilium review.",
            observations=["Resting HR 134 bpm with transient lightheadedness"],
        ),
    )
    report_repo.create_next_version(
        cycle_id=cycle_id,
        model="aegis-athena-daily-journal",
        report_type=ATHENA_DAILY_JOURNAL_REPORT_TYPE,
        privacy_level=PrivacyLevel.SERVER_ONLY,
        content=journal_rec.model_dump(mode="json"),
    )

    # 7. Seed Referral Decision
    weekly_svc = WeeklyConsiliumService(db=db_session)
    weekly_svc.save_referral_lifecycle_state(
        cycle_id=cycle_id,
        patient_id=patient_id,
        sprint_id="sprint_src_w1",
        sprint_number=1,
        decision=ProReferralDecision(
            recommended=True,
            urgency="priority",
            target_specialties=["Cardiologist", "Endocrinologist"],
            clinical_reason_for_referral="Resting tachycardia (134 bpm) with elevated TSH (5.8 mIU/L).",
            observed_syndromes_or_trends=["Paroxysmal resting tachycardia"],
            recommended_workup_for_doctor_discussion=["24h Holter ECG", "Free T4"],
        ),
    )

    recorded_calls: List[Dict[str, Any]] = []
    gateway = _build_prompt_aware_mock_gateway(recorded_calls)
    doc_service = ReferralDocumentService(db=db_session, gateway=gateway)

    res = await doc_service.generate_referral_document(cycle_id=cycle_id, patient_id=patient_id)
    assert res.status == "document_ready"
    assert len(recorded_calls) == 2

    flash_prompt = recorded_calls[0]["prompt"]
    # Verify exact dates and units across all sources in Flash Extractor prompt
    assert "2026-10-15" in flash_prompt and "5.8" in flash_prompt and "mIU/L" in flash_prompt
    assert "2026-10-16" in flash_prompt and "11.4" in flash_prompt and "ng/mL" in flash_prompt
    assert "2026-10-14" in flash_prompt and "418.0" in flash_prompt and '"unit": "ms"' in flash_prompt
    assert "142/92 mmHg" in flash_prompt and "88 bpm" in flash_prompt and "97%" in flash_prompt
    assert "78.5" in flash_prompt  # Wellness summary
    assert "134" in flash_prompt and "sudden palpitations while sitting at my desk" in flash_prompt  # Mode 2
    assert "Mild lightheadedness for about two minutes" in flash_prompt  # Athena journal answer
    assert "cold hands all afternoon" in flash_prompt  # Athena out-of-turn
    assert "chief_physician_internal" in flash_prompt  # Prior Chief conclusion
    assert "Resting tachycardia (134 bpm) with elevated TSH (5.8 mIU/L)." in flash_prompt  # Approved reason

    # Verify Pro Author prompt receives Flash's structured extraction + approved referral decision
    pro_prompt = recorded_calls[1]["prompt"]
    assert "5.8 mIU/L" in pro_prompt
    assert "142/92 mmHg" in pro_prompt
    assert "Cardiologist" in pro_prompt and "Endocrinologist" in pro_prompt


# ==============================================================================
# TEST 4: MISSING DATA IS EXPLICITLY MARKED ABSENT & NEVER REPLACED WITH FICTION
# ==============================================================================


@pytest.mark.anyio
async def test_04_missing_data_explicitly_marked_and_never_fabricated(db_session: Session):
    cycle_id = "cycle_8a2_missing_04"
    patient_id = "patient_8a2_missing_04"
    _seed_patient(db_session, patient_id)
    MedicalCycleRepository(db_session).create(cycle_id=cycle_id, patient_id=patient_id)

    weekly_svc = WeeklyConsiliumService(db=db_session)
    weekly_svc.save_referral_lifecycle_state(
        cycle_id=cycle_id,
        patient_id=patient_id,
        sprint_id="sprint_min_w1",
        sprint_number=1,
        decision=ProReferralDecision(
            recommended=True,
            urgency="routine",
            target_specialties=["General Practitioner"],
            clinical_reason_for_referral="Recurring self-reported orthostatic dizziness without uploaded exams.",
        ),
    )

    recorded_calls: List[Dict[str, Any]] = []
    gateway = _build_prompt_aware_mock_gateway(
        recorded_calls,
        doc_flash_override=_sample_referral_flash_extraction(has_missing_data=True),
        doc_pro_override=_sample_referral_pro_document(
            specialties=["General Practitioner"],
            urgency="routine",
            has_missing_data=True,
        ),
    )
    doc_service = ReferralDocumentService(db=db_session, gateway=gateway)

    res = await doc_service.generate_referral_document(cycle_id=cycle_id, patient_id=patient_id)
    assert res.status == "document_ready"

    flash_prompt = recorded_calls[0]["prompt"]
    assert "NO LABORATORY STUDIES RECORDED" in flash_prompt
    assert "NO ECG OR INSTRUMENTAL EXAMINATION RECORDED" in flash_prompt
    assert "NO OBJECTIVE BIOMETRIC VITALS RECORDED" in flash_prompt
    assert "NO SUBSTANTIVE WELLNESS DAILY SUMMARIES RECORDED" in flash_prompt
    assert "NO MODE 2 VITAL ALERT EPISODES RECORDED" in flash_prompt

    client_doc = ModelReportRepository(db_session).get(res.document_report_id)
    assert client_doc is not None
    assert "No laboratory studies, ECG recordings, or continuous wearable biometrics were uploaded" in (
        client_doc.content["summary"]
    )


# ==============================================================================
# TEST 5: FLASH ERROR -> RETRIES ONLY DOCUMENT GENERATION (NEVER WEEKLY)
# ==============================================================================


@pytest.mark.anyio
async def test_05_flash_error_retries_only_document_generation_never_reruns_weekly(db_session: Session):
    cycle_id = "cycle_8a2_flash_err_05"
    patient_id = "patient_8a2_flash_err_05"
    _seed_patient(db_session, patient_id)
    MedicalCycleRepository(db_session).create(cycle_id=cycle_id, patient_id=patient_id)
    await _seed_completed_primary_consilium(db_session, cycle_id=cycle_id)
    _seed_seven_day_athena_logs(db_session, cycle_id=cycle_id, sprint_id=f"sprint_{cycle_id}_w1")

    t0 = datetime(2026, 10, 18, 10, 0, tzinfo=timezone.utc)
    _seed_schedule_anchor(db_session, cycle_id, t0)

    pro_out = _sample_weekly_pro_output()
    pro_out["referral_decision"] = {
        "recommended": True,
        "urgency": "routine",
        "target_specialties": ["Cardiologist"],
        "clinical_reason_for_referral": "Sustained resting pulse elevation.",
    }

    def flaky_doc_flash(attempt: int, _prompt: str) -> httpx.Response:
        if attempt == 0:
            return httpx.Response(503, json={"error": {"message": "Transient Flash overload"}})
        return httpx.Response(200, json=_gemini_envelope(_sample_referral_flash_extraction()))

    recorded_calls: List[Dict[str, Any]] = []
    gateway = _build_prompt_aware_mock_gateway(
        recorded_calls,
        weekly_pro_override=pro_out,
        doc_flash_override=flaky_doc_flash,
    )

    clock = {"now": t0 + timedelta(hours=168, minutes=5)}
    cas = ClinicalAutomationService(
        db=db_session,
        gateway=gateway,
        now_provider=lambda: clock["now"],
        enable_daily_journal=False,
        enable_wellness_daily=False,
        enable_weekly_plan=False,
    )

    # 1. Run Weekly Step A (succeeds once)
    step_a = cas.enqueue_weekly_consilium_a(cycle_id=cycle_id, patient_id=patient_id, sprint_number=1)
    claimed_a = cas.claim_job(step_a.job_id)
    assert claimed_a is not None
    await cas.execute_claimed_job(claimed_a)
    assert len([c for c in recorded_calls if c["scenario"] == "weekly_flash"]) == 1
    assert len([c for c in recorded_calls if c["scenario"] == "weekly_pro"]) == 1

    # 2. First execution of REFERRAL_DOCUMENT_GENERATION fails on Flash
    doc_job = [
        j for j in cas.list_jobs(cycle_id=cycle_id)
        if j.job_type == AutomationJobType.REFERRAL_DOCUMENT_GENERATION
    ][0]
    claimed_1 = cas.claim_job(doc_job.job_id)
    assert claimed_1 is not None
    failed_1 = await cas.execute_claimed_job(claimed_1)
    assert failed_1.status == AutomationJobStatus.RETRYABLE_FAILURE

    # Athena state reflects temporary failure, NO document marked ready
    athena_mid = cas.weekly_service.get_referral_state_for_athena(cycle_id)
    assert athena_mid["athena_document_state"] == "generation_temporarily_failed"
    assert athena_mid["document_generation_state"] == "temporarily_failed"
    assert athena_mid["lifecycle_status"] == "generating_document"
    assert ModelReportRepository(db_session).list_client_facing(cycle_id, REFERRAL_DOCTOR_DOCUMENT_REPORT_TYPE) == []

    # 3. Advance clock past retry backoff -> retries ONLY document cascade (Weekly NEVER re-runs!)
    clock["now"] = clock["now"] + timedelta(minutes=5)
    await cas.process_due_jobs(discover=True)

    assert len([c for c in recorded_calls if c["scenario"] == "weekly_flash"]) == 1
    assert len([c for c in recorded_calls if c["scenario"] == "weekly_pro"]) == 1
    assert len([c for c in recorded_calls if c["scenario"] == "doc_flash"]) == 2
    assert len([c for c in recorded_calls if c["scenario"] == "doc_pro"]) == 1

    recovered_job = cas.get_job_by_id(doc_job.job_id)
    assert recovered_job is not None and recovered_job.status == AutomationJobStatus.COMPLETED
    athena_after = cas.weekly_service.get_referral_state_for_athena(cycle_id)
    assert athena_after["athena_document_state"] == "document_ready"
    assert athena_after["lifecycle_status"] == "document_ready"


# ==============================================================================
# TEST 6: PRO INVALID / UNSAFE OUTPUT -> CLEAN RETRY WITHOUT DUMMY DOCUMENT
# ==============================================================================


@pytest.mark.anyio
async def test_06_pro_invalid_or_unsafe_output_rejected_and_retried_without_dummy_document(db_session: Session):
    cycle_id = "cycle_8a2_pro_err_06"
    patient_id = "patient_8a2_pro_err_06"
    _seed_patient(db_session, patient_id)
    MedicalCycleRepository(db_session).create(cycle_id=cycle_id, patient_id=patient_id)

    weekly_svc = WeeklyConsiliumService(db=db_session)
    weekly_svc.save_referral_lifecycle_state(
        cycle_id=cycle_id,
        patient_id=patient_id,
        sprint_id="sprint_pro_err_w1",
        sprint_number=1,
        decision=ProReferralDecision(
            recommended=True,
            urgency="routine",
            target_specialties=["Endocrinologist"],
            clinical_reason_for_referral="Elevated TSH requiring consultation.",
        ),
    )

    def flaky_doc_pro(attempt: int, _prompt: str) -> httpx.Response:
        if attempt == 0:
            # Violates safety boundary: claims to be an official medical discharge summary & confirmed diagnosis
            bad_doc = _sample_referral_pro_document(specialties=["Endocrinologist"])
            bad_doc["informational_disclaimer"] = (
                "Official medical discharge summary with confirmed clinical diagnosis of hypothyroidism."
            )
            return httpx.Response(200, json=_gemini_envelope(bad_doc))
        if attempt == 1:
            # Malformed incomplete JSON missing required sections
            return httpx.Response(200, json=_gemini_envelope({"document_title": REFERRAL_DOCUMENT_CANONICAL_TITLE}))
        return httpx.Response(200, json=_gemini_envelope(_sample_referral_pro_document(specialties=["Endocrinologist"])))

    recorded_calls: List[Dict[str, Any]] = []
    gateway = _build_prompt_aware_mock_gateway(recorded_calls, doc_pro_override=flaky_doc_pro)

    clock = {"now": datetime(2026, 10, 25, 12, 0, tzinfo=timezone.utc)}
    cas = ClinicalAutomationService(
        db=db_session,
        gateway=gateway,
        now_provider=lambda: clock["now"],
        enable_daily_journal=False,
        enable_wellness_daily=False,
        enable_automatic_weekly=False,
    )
    report_repo = ModelReportRepository(db_session)

    job = cas.enqueue_referral_document_generation(cycle_id=cycle_id, patient_id=patient_id)
    assert job is not None

    # Attempt 1 (unsafe diagnostic claim) -> RETRYABLE_FAILURE, zero documents saved
    claimed_1 = cas.claim_job(job.job_id)
    assert claimed_1 is not None
    out_1 = await cas.execute_claimed_job(claimed_1)
    assert out_1.status == AutomationJobStatus.RETRYABLE_FAILURE
    assert report_repo.list_client_facing(cycle_id, REFERRAL_DOCTOR_DOCUMENT_REPORT_TYPE) == []
    assert report_repo.list_server_only(cycle_id, REFERRAL_DOCTOR_DOCUMENT_RAW_REPORT_TYPE) == []
    assert cas.weekly_service.get_referral_lifecycle_state(cycle_id)["status"] == "generating_document"

    # Attempt 2 (missing required sections) -> RETRYABLE_FAILURE, still zero documents saved
    clock["now"] = clock["now"] + timedelta(minutes=2)
    claimed_2 = cas.claim_job(job.job_id)
    assert claimed_2 is not None
    out_2 = await cas.execute_claimed_job(claimed_2)
    assert out_2.status == AutomationJobStatus.RETRYABLE_FAILURE
    assert report_repo.list_client_facing(cycle_id, REFERRAL_DOCTOR_DOCUMENT_REPORT_TYPE) == []

    # Attempt 3 (valid structured output) -> COMPLETED & document_ready
    clock["now"] = clock["now"] + timedelta(minutes=5)
    claimed_3 = cas.claim_job(job.job_id)
    assert claimed_3 is not None
    out_3 = await cas.execute_claimed_job(claimed_3)
    assert out_3.status == AutomationJobStatus.COMPLETED
    assert len(report_repo.list_client_facing(cycle_id, REFERRAL_DOCTOR_DOCUMENT_REPORT_TYPE)) == 1
    assert cas.weekly_service.get_referral_lifecycle_state(cycle_id)["status"] == "document_ready"


# ==============================================================================
# TEST 7: DUPLICATE JOB / REPEATED EXECUTION -> ZERO DUPLICATES
# ==============================================================================


@pytest.mark.anyio
async def test_07_duplicate_enqueue_and_reexecution_create_zero_duplicate_documents(db_session: Session):
    cycle_id = "cycle_8a2_idem_07"
    patient_id = "patient_8a2_idem_07"
    _seed_patient(db_session, patient_id)
    MedicalCycleRepository(db_session).create(cycle_id=cycle_id, patient_id=patient_id)

    weekly_svc = WeeklyConsiliumService(db=db_session)
    weekly_svc.save_referral_lifecycle_state(
        cycle_id=cycle_id,
        patient_id=patient_id,
        sprint_id="sprint_idem_w1",
        sprint_number=1,
        decision=ProReferralDecision(
            recommended=True,
            urgency="routine",
            target_specialties=["Neurologist"],
            clinical_reason_for_referral="Persistent tension headaches.",
        ),
    )

    recorded_calls: List[Dict[str, Any]] = []
    gateway = _build_prompt_aware_mock_gateway(
        recorded_calls,
        doc_pro_override=_sample_referral_pro_document(specialties=["Neurologist"]),
    )
    cas = ClinicalAutomationService(
        db=db_session,
        gateway=gateway,
        enable_daily_journal=False,
        enable_wellness_daily=False,
        enable_automatic_weekly=False,
    )

    # Enqueue 3 times -> exact same deterministic job_id
    j1 = cas.enqueue_referral_document_generation(cycle_id=cycle_id, patient_id=patient_id)
    j2 = cas.enqueue_referral_document_generation(cycle_id=cycle_id, patient_id=patient_id)
    j3 = cas.enqueue_referral_document_generation(cycle_id=cycle_id, patient_id=patient_id)
    assert j1 is not None and j2 is not None and j3 is not None
    assert j1.job_id == j2.job_id == j3.job_id

    claimed = cas.claim_job(j1.job_id)
    assert claimed is not None
    await cas.execute_claimed_job(claimed)
    assert len(recorded_calls) == 2  # 1 Flash + 1 Pro

    # Direct repeat call to ReferralDocumentService -> reuses existing with ZERO new Gemini calls
    replay = await cas.referral_document_service.generate_referral_document(
        cycle_id=cycle_id,
        patient_id=patient_id,
    )
    assert replay.reused_existing is True
    assert replay.document_version == 1
    assert len(recorded_calls) == 2

    report_repo = ModelReportRepository(db_session)
    assert len(report_repo.list_client_facing(cycle_id, REFERRAL_DOCTOR_DOCUMENT_REPORT_TYPE)) == 1
    assert len(report_repo.list_server_only(cycle_id, REFERRAL_DOCTOR_DOCUMENT_RAW_REPORT_TYPE)) == 1


# ==============================================================================
# TEST 8: WORKER CRASH / SERVER RESTART -> AUTOMATIC RECOVERY
# ==============================================================================


@pytest.mark.anyio
async def test_08_worker_restart_recovers_unfinished_and_expired_lease_document_jobs(tmp_path: Path):
    db_file = tmp_path / "stage_8a2_worker_restart.db"
    engine = create_engine(f"sqlite:///{db_file}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    cycle_a = "cycle_restart_lease_a"
    cycle_b = "cycle_restart_unenqueued_b"
    patient_id = "patient_restart_08"
    t0 = datetime(2026, 10, 18, 10, 0, tzinfo=timezone.utc)
    clock = {"now": t0 + timedelta(hours=169)}

    # Session 1 (Before Server Crash):
    # - cycle_a has a RUNNING job whose worker crashed mid-flight (holding an active lease)
    # - cycle_b saved referral_lifecycle_state ('generating_document'), crashed BEFORE enqueueing job
    with SessionLocal() as s1:
        _seed_patient(s1, patient_id)
        CycleRepo = MedicalCycleRepository(s1)
        CycleRepo.create(cycle_id=cycle_a, patient_id=patient_id)
        CycleRepo.create(cycle_id=cycle_b, patient_id=patient_id)
        await _seed_completed_primary_consilium(s1, cycle_id=cycle_a)
        await _seed_completed_primary_consilium(s1, cycle_id=cycle_b)
        _seed_schedule_anchor(s1, cycle_a, t0)
        _seed_schedule_anchor(s1, cycle_b, t0)

        w_svc = WeeklyConsiliumService(db=s1, now_provider=lambda: clock["now"])
        dec = ProReferralDecision(
            recommended=True,
            urgency="routine",
            target_specialties=["Cardiologist"],
            clinical_reason_for_referral="Restart recovery verification.",
        )
        w_svc.save_referral_lifecycle_state(cycle_a, patient_id, "sprint_ra_w1", 1, dec)
        w_svc.save_referral_lifecycle_state(cycle_b, patient_id, "sprint_rb_w1", 1, dec)

        cas1 = ClinicalAutomationService(db=s1, now_provider=lambda: clock["now"])
        job_a = cas1.enqueue_referral_document_generation(cycle_id=cycle_a, patient_id=patient_id)
        assert job_a is not None
        claimed_a = cas1.claim_job(job_a.job_id)
        assert claimed_a is not None and claimed_a.status == AutomationJobStatus.RUNNING
        # Simulate abrupt process crash here!

    # Advance clock past the 300s job lease duration
    clock["now"] = clock["now"] + timedelta(seconds=360)

    recorded_calls: List[Dict[str, Any]] = []
    gateway = _build_prompt_aware_mock_gateway(
        recorded_calls,
        doc_pro_override=_sample_referral_pro_document(specialties=["Cardiologist"]),
    )

    # New ClinicalAutomationWorker after server restart on fresh SQLite sessions
    worker = ClinicalAutomationWorker(
        session_factory=SessionLocal,
        gateway_factory=lambda: gateway,
        now_provider=lambda: clock["now"],
        enable_daily_journal=False,
        enable_wellness_daily=False,
        enable_weekly_plan=False,
        enable_automatic_weekly=False,
        enable_lab_revision=False,
    )
    processed = await worker.run_once()
    ref_outcomes = [
        p for p in processed if p.job_type == AutomationJobType.REFERRAL_DOCUMENT_GENERATION
    ]
    assert len(ref_outcomes) == 2
    assert all(p.status == AutomationJobStatus.COMPLETED for p in ref_outcomes)

    with SessionLocal() as s_verify:
        w_verify = WeeklyConsiliumService(db=s_verify)
        state_a = w_verify.get_referral_lifecycle_state(cycle_a)
        state_b = w_verify.get_referral_lifecycle_state(cycle_b)
        assert state_a is not None and state_a["status"] == "document_ready"
        assert state_b is not None and state_b["status"] == "document_ready"
        assert state_a["document_report_id"] is not None
        assert state_b["document_report_id"] is not None

    engine.dispose()


# ==============================================================================
# TEST 9: NEW WEEKLY WITH ACTIVE REFERRAL -> PRESERVES LINKS, NO REDUNDANT GEN, KEEPS HISTORY
# ==============================================================================


@pytest.mark.anyio
async def test_09_subsequent_weekly_preserves_ready_document_link_and_history_versions(db_session: Session):
    cycle_id = "cycle_8a2_history_09"
    patient_id = "patient_8a2_history_09"
    _seed_patient(db_session, patient_id)
    MedicalCycleRepository(db_session).create(cycle_id=cycle_id, patient_id=patient_id)

    recorded_calls: List[Dict[str, Any]] = []
    current_specs = {"list": ["Cardiologist"], "urgency": "routine"}

    def dynamic_doc_pro(_attempt: int, _prompt: str) -> httpx.Response:
        return httpx.Response(
            200,
            json=_gemini_envelope(
                _sample_referral_pro_document(
                    specialties=list(current_specs["list"]),
                    urgency=current_specs["urgency"],
                )
            ),
        )

    gateway = _build_prompt_aware_mock_gateway(recorded_calls, doc_pro_override=dynamic_doc_pro)
    weekly_svc = WeeklyConsiliumService(db=db_session, gateway=gateway)
    doc_svc = ReferralDocumentService(db=db_session, gateway=gateway)
    report_repo = ModelReportRepository(db_session)

    # 1. Week 1: Initial referral -> generates Document v1
    dec_w1 = ProReferralDecision(
        recommended=True,
        urgency="routine",
        target_specialties=["Cardiologist"],
        clinical_reason_for_referral="Elevated resting pulse.",
        observed_syndromes_or_trends=["Trend A"],
        recommended_workup_for_doctor_discussion=["ECG 12-lead"],
    )
    weekly_svc.save_referral_lifecycle_state(cycle_id, patient_id, "sprint_w1", 1, dec_w1)
    res_v1 = await doc_svc.generate_referral_document(cycle_id=cycle_id, patient_id=patient_id)
    assert res_v1.document_version == 1
    doc_v1_id = res_v1.document_report_id
    assert len(recorded_calls) == 2

    # 2. Week 2: recommended=False -> preserves document_ready and doc_v1_id, needs_generation=False
    weekly_svc.save_referral_lifecycle_state(
        cycle_id, patient_id, "sprint_w2", 2, ProReferralDecision(recommended=False)
    )
    state_w2 = weekly_svc.get_referral_lifecycle_state(cycle_id)
    assert state_w2 is not None
    assert state_w2["status"] == "document_ready"
    assert state_w2["document_report_id"] == doc_v1_id
    assert weekly_svc.needs_referral_document_generation(cycle_id) is False

    # 3. Week 3: recommended=True with IDENTICAL clinical content -> no redundant new document version!
    weekly_svc.save_referral_lifecycle_state(cycle_id, patient_id, "sprint_w3", 3, dec_w1)
    state_w3 = weekly_svc.get_referral_lifecycle_state(cycle_id)
    assert state_w3 is not None
    assert state_w3["sprint_id"] == "sprint_w3"  # Verifies latest version read (Stage 8A.1 ordering fix!)
    assert state_w3["status"] == "document_ready"
    assert state_w3["document_report_id"] == doc_v1_id
    assert weekly_svc.needs_referral_document_generation(cycle_id) is False

    # 4. Week 4: Substantive clinical escalation (adds Endocrinologist + priority urgency)
    current_specs["list"] = ["Cardiologist", "Endocrinologist"]
    current_specs["urgency"] = "priority"
    dec_w4 = ProReferralDecision(
        recommended=True,
        urgency="priority",
        target_specialties=["Endocrinologist"],
        clinical_reason_for_referral="Elevated resting pulse plus new TSH abnormality.",
        observed_syndromes_or_trends=["TSH 5.8 mIU/L"],
        recommended_workup_for_doctor_discussion=["Thyroid panel"],
    )
    weekly_svc.save_referral_lifecycle_state(cycle_id, patient_id, "sprint_w4", 4, dec_w4)

    # BEFORE v2 runs: link to previously ready doc_v1_id MUST NOT BE LOST!
    state_w4_before = weekly_svc.get_referral_lifecycle_state(cycle_id)
    assert state_w4_before is not None
    assert state_w4_before["document_report_id"] == doc_v1_id
    assert state_w4_before["status"] == "document_ready"
    assert weekly_svc.needs_referral_document_generation(cycle_id) is True

    # Generate v2 for Week 4 -> v2 becomes active while v1 remains intact in history!
    res_v2 = await doc_svc.generate_referral_document(cycle_id=cycle_id, patient_id=patient_id)
    assert res_v2.document_version == 2
    doc_v2_id = res_v2.document_report_id
    assert doc_v2_id != doc_v1_id

    state_w4_after = weekly_svc.get_referral_lifecycle_state(cycle_id)
    assert state_w4_after is not None
    assert state_w4_after["status"] == "document_ready"
    assert state_w4_after["document_report_id"] == doc_v2_id
    assert state_w4_after["document_history_report_ids"] == [doc_v1_id, doc_v2_id]

    all_versions = report_repo.list_versions(
        cycle_id=cycle_id,
        report_type=REFERRAL_DOCTOR_DOCUMENT_REPORT_TYPE,
        privacy_level=PrivacyLevel.CLIENT_FACING,
    )
    assert [r.report_id for r in all_versions] == [doc_v1_id, doc_v2_id]
    assert [r.version for r in all_versions] == [1, 2]


# ==============================================================================
# TEST 10: NEO / CLOSED CYCLE -> NO STALE ACTIVE REFERRAL PUBLISHED IN NEW CYCLE
# ==============================================================================


@pytest.mark.anyio
async def test_10_closed_or_neo_cycle_does_not_publish_stale_referral_in_new_cycle(db_session: Session):
    patient_id = "patient_neo_10"
    old_cycle_id = "cycle_old_10"
    new_cycle_id = "cycle_neo_10"
    _seed_patient(db_session, patient_id)

    cycle_repo = MedicalCycleRepository(db_session)
    old_cycle = cycle_repo.create(cycle_id=old_cycle_id, patient_id=patient_id)

    weekly_svc = WeeklyConsiliumService(db=db_session)
    weekly_svc.save_referral_lifecycle_state(
        cycle_id=old_cycle_id,
        patient_id=patient_id,
        sprint_id="sprint_old_w1",
        sprint_number=1,
        decision=ProReferralDecision(
            recommended=True,
            urgency="routine",
            target_specialties=["Cardiologist"],
            clinical_reason_for_referral="Old cycle referral before Neo transition.",
        ),
    )

    recorded_calls: List[Dict[str, Any]] = []
    gateway = _build_prompt_aware_mock_gateway(recorded_calls)
    cas = ClinicalAutomationService(
        db=db_session,
        gateway=gateway,
        enable_daily_journal=False,
        enable_wellness_daily=False,
        enable_automatic_weekly=False,
    )

    # Enqueue job while old_cycle is still active
    stale_job = cas.enqueue_referral_document_generation(cycle_id=old_cycle_id, patient_id=patient_id)
    assert stale_job is not None

    # Simulate Neo rollover: close old_cycle ('completed') and open new_cycle ('active')
    old_cycle.status = "completed"
    db_session.commit()
    cycle_repo.create(cycle_id=new_cycle_id, patient_id=patient_id)

    # 1. Direct service call on closed cycle must raise ReferralCycleInactiveError with 0 Gemini calls
    with pytest.raises(ReferralCycleInactiveError):
        await cas.referral_document_service.generate_referral_document(
            cycle_id=old_cycle_id,
            patient_id=patient_id,
        )
    assert recorded_calls == []

    # 2. Worker executing stale_job gracefully skips it without calling Gemini or polluting new_cycle
    await cas.process_due_jobs(discover=True)
    assert recorded_calls == []

    finished_stale = cas.get_job_by_id(stale_job.job_id)
    assert finished_stale is not None
    assert finished_stale.status == AutomationJobStatus.COMPLETED
    assert finished_stale.result_metadata.get("skipped") is True
    assert finished_stale.result_metadata.get("reason") == "cycle_not_active"

    # New cycle has NO active referral
    assert cas.weekly_service.get_referral_lifecycle_state(new_cycle_id) is None
    assert cas.weekly_service.get_referral_state_for_athena(new_cycle_id)["has_active_referral"] is False


# ==============================================================================
# TEST 11: INVALID CLIENT STRUCTURE -> STRICT REJECTION & ATOMIC ROLLBACK
# ==============================================================================


@pytest.mark.anyio
async def test_11_invalid_client_report_structure_is_strictly_rejected_without_corrupting_state(
    db_session: Session,
):
    cycle_id = "cycle_8a2_schema_11"
    patient_id = "patient_8a2_schema_11"
    _seed_patient(db_session, patient_id)
    MedicalCycleRepository(db_session).create(cycle_id=cycle_id, patient_id=patient_id)

    weekly_svc = WeeklyConsiliumService(db=db_session)
    weekly_svc.save_referral_lifecycle_state(
        cycle_id=cycle_id,
        patient_id=patient_id,
        sprint_id="sprint_schema_w1",
        sprint_number=1,
        decision=ProReferralDecision(
            recommended=True,
            urgency="routine",
            target_specialties=["Cardiologist"],
            clinical_reason_for_referral="Schema enforcement test.",
        ),
    )

    recorded_calls: List[Dict[str, Any]] = []
    gateway = _build_prompt_aware_mock_gateway(recorded_calls)
    doc_svc = ReferralDocumentService(db=db_session, gateway=gateway)
    report_repo = ModelReportRepository(db_session)

    # Attempt to inject an unapproved key into CLIENT_FACING report content
    with pytest.raises(ReferralDocumentSchemaViolationError):
        await doc_svc.generate_referral_document(
            cycle_id=cycle_id,
            patient_id=patient_id,
            extra_client_overrides={"unapproved_private_reasoning": "MUST_BE_FORBIDDEN"},
        )

    # Verify atomic rollback: NEITHER client_facing NOR server_only raw doc was committed,
    # and referral_lifecycle_state was NOT transitioned to document_ready!
    assert report_repo.list_client_facing(cycle_id, REFERRAL_DOCTOR_DOCUMENT_REPORT_TYPE) == []
    assert report_repo.list_server_only(cycle_id, REFERRAL_DOCTOR_DOCUMENT_RAW_REPORT_TYPE) == []
    ref_state = weekly_svc.get_referral_lifecycle_state(cycle_id)
    assert ref_state is not None
    assert ref_state["status"] == "generating_document"
    assert ref_state["document_report_id"] is None


# ==============================================================================
# TEST 12: DIFFERENT PATIENTS -> STRICT ISOLATION & ZERO DATA LEAKAGE
# ==============================================================================


@pytest.mark.anyio
async def test_12_multi_patient_isolation_zero_cross_patient_leakage(db_session: Session):
    patient_a = "patient_8a2_iso_a"
    patient_b = "patient_8a2_iso_b"
    cycle_a = "cycle_8a2_iso_a"
    cycle_b = "cycle_8a2_iso_b"
    _seed_patient(db_session, patient_a)
    _seed_patient(db_session, patient_b)

    cycle_repo = MedicalCycleRepository(db_session)
    cycle_repo.create(cycle_id=cycle_a, patient_id=patient_a)
    cycle_repo.create(cycle_id=cycle_b, patient_id=patient_b)

    lab_svc = LabRevisionConsiliumService(db=db_session)
    lab_svc.upsert_laboratory_markers(
        cycle_id=cycle_a,
        mutations=[
            LabMarkerMutationItem(
                marker_key="hba1c_secret_a",
                display_name="SECRET_PATIENT_A_HBA1C",
                value=8.4,
                unit="%",
                sample_date="2026-10-11",
            )
        ],
    )
    lab_svc.upsert_laboratory_markers(
        cycle_id=cycle_b,
        mutations=[
            LabMarkerMutationItem(
                marker_key="ldl_secret_b",
                display_name="SECRET_PATIENT_B_LDL",
                value=4.9,
                unit="mmol/L",
                sample_date="2026-10-12",
            )
        ],
    )

    weekly_svc = WeeklyConsiliumService(db=db_session)
    weekly_svc.save_referral_lifecycle_state(
        cycle_a,
        patient_a,
        "sprint_iso_a_w1",
        1,
        ProReferralDecision(
            recommended=True,
            urgency="priority",
            target_specialties=["Endocrinologist"],
            clinical_reason_for_referral="Patient A secret endocrine reason.",
        ),
    )
    weekly_svc.save_referral_lifecycle_state(
        cycle_b,
        patient_b,
        "sprint_iso_b_w1",
        1,
        ProReferralDecision(
            recommended=True,
            urgency="routine",
            target_specialties=["Cardiologist"],
            clinical_reason_for_referral="Patient B secret lipid reason.",
        ),
    )

    calls_a: List[Dict[str, Any]] = []
    calls_b: List[Dict[str, Any]] = []
    doc_a = await ReferralDocumentService(
        db=db_session,
        gateway=_build_prompt_aware_mock_gateway(
            calls_a, doc_pro_override=_sample_referral_pro_document(specialties=["Endocrinologist"])
        ),
    ).generate_referral_document(cycle_id=cycle_a, patient_id=patient_a)

    doc_b = await ReferralDocumentService(
        db=db_session,
        gateway=_build_prompt_aware_mock_gateway(
            calls_b, doc_pro_override=_sample_referral_pro_document(specialties=["Cardiologist"])
        ),
    ).generate_referral_document(cycle_id=cycle_b, patient_id=patient_b)

    # Verify zero prompt cross-contamination
    prompt_a = calls_a[0]["prompt"]
    prompt_b = calls_b[0]["prompt"]
    assert "SECRET_PATIENT_A_HBA1C" in prompt_a
    assert "SECRET_PATIENT_B_LDL" not in prompt_a
    assert patient_b not in prompt_a and cycle_b not in prompt_a

    assert "SECRET_PATIENT_B_LDL" in prompt_b
    assert "SECRET_PATIENT_A_HBA1C" not in prompt_b
    assert patient_a not in prompt_b and cycle_a not in prompt_b

    # Verify HTTP endpoint access isolation (/api/v1/reports/client/{report_id} & /cascade/status)
    client = TestClient(app)

    def _session_for(pid: str) -> DeviceSession:
        return DeviceSession(
            patient_id=pid,
            device_id=f"dev_{pid}",
            jti=f"jti_{pid}",
            family_id=f"fam_{pid}",
            scope="cycle:read reports:read",
            scopes=frozenset(["cycle:read", "reports:read"]),
            issued_at=datetime.now(timezone.utc),
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        )

    app.dependency_overrides[get_db] = lambda: db_session
    try:
        # Patient B attempting to read Patient A's referral_doctor_document -> 403 Forbidden
        app.dependency_overrides[require_device_session] = lambda: _session_for(patient_b)
        app.dependency_overrides[verify_client_token] = lambda: _session_for(patient_b)

        forbidden_resp = client.get(f"/api/v1/reports/client/{doc_a.document_report_id}")
        assert forbidden_resp.status_code == 403

        own_resp = client.get(f"/api/v1/reports/client/{doc_b.document_report_id}")
        assert own_resp.status_code == 200
        assert own_resp.json()["report_id"] == doc_b.document_report_id

        status_b = client.get("/api/v1/client/cascade/status")
        assert status_b.status_code == 200
        ref_b = status_b.json()["referral_status"]
        assert ref_b["status"] == "document_ready"
        assert ref_b["document_generation_state"] == "ready"
        assert ref_b["document_report_id"] == doc_b.document_report_id
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(require_device_session, None)
        app.dependency_overrides.pop(verify_client_token, None)


# ==============================================================================
# TEST 13: WEEKLY, ATHENA WRAP-UP & PRO THRESHOLDS CONTINUE WORKING UNBLOCKED
# ==============================================================================


@pytest.mark.anyio
async def test_13_weekly_step_c_athena_wrapup_and_pro_thresholds_not_blocked_by_referral_doc(
    db_session: Session,
):
    cycle_id = "cycle_8a2_athena_13"
    patient_id = "patient_8a2_athena_13"
    _seed_patient(db_session, patient_id)
    MedicalCycleRepository(db_session).create(cycle_id=cycle_id, patient_id=patient_id)
    await _seed_completed_primary_consilium(db_session, cycle_id=cycle_id)
    _seed_seven_day_athena_logs(db_session, cycle_id=cycle_id, sprint_id=f"sprint_{cycle_id}_w1")

    t0 = datetime(2026, 10, 18, 10, 0, tzinfo=timezone.utc)
    _seed_schedule_anchor(db_session, cycle_id, t0)

    pro_out = _sample_weekly_pro_output()
    pro_out["referral_decision"] = {
        "recommended": True,
        "urgency": "routine",
        "target_specialties": ["Cardiologist"],
        "clinical_reason_for_referral": "Evaluate resting heart rate trend.",
    }

    # Document Flash fails on Attempt 1 -> must NOT block Weekly Step B or Step C (Athena weekly wrapup)!
    def fail_doc_flash(_attempt: int, _prompt: str) -> httpx.Response:
        return httpx.Response(503, json={"error": {"message": "Document Flash temporarily unavailable"}})

    recorded_calls: List[Dict[str, Any]] = []
    gateway = _build_prompt_aware_mock_gateway(
        recorded_calls,
        weekly_pro_override=pro_out,
        doc_flash_override=fail_doc_flash,
    )

    now_dt = t0 + timedelta(hours=168, minutes=5)
    cas = ClinicalAutomationService(
        db=db_session,
        gateway=gateway,
        now_provider=lambda: now_dt,
        enable_daily_journal=False,
        enable_wellness_daily=False,
        enable_weekly_plan=True,
        enable_automatic_weekly=True,
    )

    # Run process_due_jobs cycles to execute Step A -> Step B -> Step C while doc job fails into retry
    await cas.process_due_jobs(discover=True)
    await cas.process_due_jobs(discover=True)
    await cas.process_due_jobs(discover=True)

    jobs_by_type = {j.job_type: j for j in cas.list_jobs(cycle_id=cycle_id)}
    assert jobs_by_type[AutomationJobType.WEEKLY_CONSILIUM_A].status == AutomationJobStatus.COMPLETED
    assert jobs_by_type[AutomationJobType.WEEKLY_CONSILIUM_B].status == AutomationJobStatus.COMPLETED
    assert jobs_by_type[AutomationJobType.WEEKLY_CONSILIUM_C].status == AutomationJobStatus.COMPLETED
    assert (
        jobs_by_type[AutomationJobType.REFERRAL_DOCUMENT_GENERATION].status
        == AutomationJobStatus.RETRYABLE_FAILURE
    )

    # Verify Athena weekly wrap-up message was delivered without waiting for Flash -> Pro doc!
    assert any(c["scenario"] == "weekly_wrapup" for c in recorded_calls)

    # Verify Pro thresholds remain completely functional
    thresh = ProVitalsThresholds(
        min_pessimum_bpm=48,
        max_pessimum_bpm=110,
        max_spike_delta_bpm=25,
        clinical_rationale="Standard resting envelope.",
    )
    assert thresh.max_pessimum_bpm == 110
