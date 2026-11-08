"""
Stage 8A.4 — Comprehensive Integration & Unit Tests for Athena Referral Follow-up,
168-Hour Server-Scheduled Reminders, Contextual Doctor Visit Confirmation,
Continuous Clinical Observation, Safe Examination Guidance & `Update Health Information`.

All Gemini calls are 100% mocked via `httpx.MockTransport` — ZERO real network or Gemini API calls.
"""

from datetime import datetime, timedelta, timezone
import inspect
import json
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.models.base import Base
from app.models.health_measurement import HealthMeasurement
from app.models.patient import Patient
from app.repositories.cycle_repository import MedicalCycleRepository
from app.repositories.report_repository import ModelReportRepository
from app.schemas.chat import AthenaChatRequest, AthenaVitalsAlertContext
from app.schemas.consilium import (
    AthenaClinicalLogEntry,
    ProReferralDecision,
    REFERRAL_DOCUMENT_CANONICAL_TITLE,
)
from app.schemas.privacy import PrivacyLevel
from app.services import athena_chat_service as athena_chat_module
from app.services import weekly_consilium_service as weekly_consilium_module
from app.services.athena_chat_service import AthenaChatService
from app.services.athena_daily_journal_service import (
    ATHENA_DAILY_JOURNAL_REPORT_TYPE,
    AthenaDailyJournalService,
)
from app.services.clinical_automation_service import (
    AutomationJobStatus,
    AutomationJobType,
    ClinicalAutomationService,
)
from app.services.doctor_document_export_service import DoctorDocumentExportService
from app.services.gemini_gateway import GeminiGateway
from app.services.mode2_vitals_service import Mode2VitalsMonitoringService
from app.services.referral_document_service import (
    REFERRAL_DOCTOR_DOCUMENT_REPORT_TYPE,
    ReferralDocumentService,
)
from app.services.weekly_consilium_service import (
    ATHENA_SCHEDULE_ANCHOR_REPORT_TYPE,
    REFERRAL_LIFECYCLE_STATE_REPORT_TYPE,
    WeeklyConsiliumService,
)
from tests.test_primary_consilium import _gemini_envelope
from tests.test_stage_8a2_referral_document import (
    _sample_referral_flash_extraction,
    _sample_referral_pro_document,
)
from tests.test_weekly_consilium import (
    _sample_weekly_flash_output,
    _sample_weekly_pro_output,
    _seed_completed_primary_consilium,
    _seed_seven_day_athena_logs,
)


# ==============================================================================
# FIXTURES & MOCK HELPERS
# ==============================================================================


@pytest.fixture()
def test_db(tmp_path: Path):
    db_file = tmp_path / "test_stage_8a4.sqlite3"
    engine = create_engine(
        f"sqlite:///{db_file}",
        connect_args={"check_same_thread": False},
    )
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)
    session = TestingSessionLocal()
    try:
        yield session, TestingSessionLocal
    finally:
        session.close()
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


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


def _sample_pro_referral_doc() -> Dict[str, Any]:
    return _sample_referral_pro_document(
        specialties=["cardiologist", "endocrinologist"],
        urgency="priority",
    )


def _extract_request_payload(request: httpx.Request) -> Dict[str, Any]:
    return json.loads(request.content.decode("utf-8"))


def _extract_full_prompt_text(body: Dict[str, Any]) -> str:
    parts_text: List[str] = []
    sys_inst = body.get("systemInstruction") or {}
    for p in sys_inst.get("parts") or []:
        if isinstance(p, dict) and p.get("text"):
            parts_text.append(str(p["text"]))
    for c in body.get("contents") or []:
        for p in c.get("parts") or []:
            if isinstance(p, dict) and p.get("text"):
                parts_text.append(str(p["text"]))
    return "\n".join(parts_text)


class Stage8A4MockHarness:
    """
    Deterministic mock Gemini transport recording every request payload, model,
    system instruction, and conversation history so tests can verify single-call
    contextual reasoning, prompt accuracy, and failure handling with zero real API calls.
    """

    def __init__(
        self,
        *,
        referral_recommended: bool = True,
        fail_chat_turn: bool = False,
        fail_reminder_calls: int = 0,
        fail_doc_generation: bool = False,
        on_doc_pro_inflight: Optional[Callable[[], None]] = None,
        custom_chat_responder: Optional[Callable[[Dict[str, Any], str, str], str]] = None,
    ):
        self.referral_recommended = referral_recommended
        self.fail_chat_turn = fail_chat_turn
        self.fail_reminder_calls = fail_reminder_calls
        self.fail_doc_generation = fail_doc_generation
        self.on_doc_pro_inflight = on_doc_pro_inflight
        self.custom_chat_responder = custom_chat_responder

        self.calls: List[Dict[str, Any]] = []
        self.weekly_wrapup_prompts: List[str] = []
        self.reminder_prompts: List[str] = []
        self.chat_turn_calls: List[Dict[str, Any]] = []
        self._reminder_attempt_counter = 0

    def build_gateway(self) -> GeminiGateway:
        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            body = _extract_request_payload(request)
            full_text = _extract_full_prompt_text(body)
            sys_text = "\n".join(
                str(p.get("text", ""))
                for p in (body.get("systemInstruction") or {}).get("parts", [])
            )
            contents = body.get("contents") or []
            last_user_turn = ""
            if contents:
                last_user_turn = "\n".join(
                    str(p.get("text", "")) for p in (contents[-1].get("parts") or [])
                )

            self.calls.append(
                {
                    "url": url,
                    "body": body,
                    "full_text": full_text,
                    "system_instruction": sys_text,
                    "contents": contents,
                    "last_user_turn": last_user_turn,
                }
            )

            url_path = request.url.path
            gen_cfg = body.get("generationConfig") or {}
            resp_mime = str(gen_cfg.get("responseMimeType") or "")

            # 1. Athena Interactive Conversational Turn (`generate_raw_text`, text/plain)
            if resp_mime == "text/plain":
                self.chat_turn_calls.append(
                    {
                        "system_instruction": sys_text,
                        "contents": contents,
                        "last_user_turn": last_user_turn,
                    }
                )
                if self.fail_chat_turn:
                    return httpx.Response(
                        502, json={"error": {"message": "Simulated Athena chat outage"}}
                    )

                if self.custom_chat_responder is not None:
                    raw_reply = self.custom_chat_responder(body, sys_text, last_user_turn)
                else:
                    raw_reply = self._default_contextual_athena_responder(
                        contents, sys_text, last_user_turn
                    )
                return httpx.Response(
                    200,
                    json={"candidates": [{"content": {"parts": [{"text": raw_reply}]}}]},
                )

            # 2. Referral Document Cascade (Flash -> Pro)
            if "[Internal Clinical History Extraction Scenario" in full_text:
                if self.fail_doc_generation:
                    return httpx.Response(
                        503, json={"error": {"message": "Simulated Flash doc timeout"}}
                    )
                return httpx.Response(
                    200, json=_gemini_envelope(_sample_referral_flash_extraction())
                )

            if "[Doctor Observation Summary Authoring Scenario" in full_text:
                if self.fail_doc_generation:
                    return httpx.Response(
                        503, json={"error": {"message": "Simulated Pro doc timeout"}}
                    )
                if self.on_doc_pro_inflight is not None:
                    self.on_doc_pro_inflight()
                return httpx.Response(200, json=_gemini_envelope(_sample_pro_referral_doc()))

            # 3. Athena Weekly Wrap-up (Step C)
            if "[Internal Athena Weekly Wrap-up Message Formulation]:" in full_text:
                self.weekly_wrapup_prompts.append(full_text)
                if "DOCUMENT STATUS: READY IN SAFE" in full_text:
                    doc_sentence = (
                        "Your Health Observation Summary is ready right now in the Safe so you can review it "
                        "or show it to your doctor during the appointment."
                    )
                elif "DOCUMENT STATUS: TEMPORARILY DELAYED (NOT READY)" in full_text:
                    doc_sentence = (
                        "Your Health Observation Summary is experiencing a brief preparation delay and is "
                        "not ready in the Safe yet—we are retrying automatically in the background."
                    )
                elif "DOCUMENT STATUS: CURRENTLY BEING PREPARED (NOT READY YET)" in full_text:
                    doc_sentence = (
                        "Your Health Observation Summary for the doctor is currently being prepared in the "
                        "background and will appear in the Safe once ready."
                    )
                else:
                    doc_sentence = "Your detailed weekly reports are available in the Safe."

                if "ACTIVE IN-PERSON MEDICAL CONSULTATION RECOMMENDATION" in full_text:
                    msg = (
                        "Hello! Week 1 of your health accompaniment is complete. Over the past week, we noticed "
                        "recurring resting heart rate elevations alongside your elevated TSH level and afternoon "
                        "fatigue. Because these patterns deserve a closer look in person, we gently recommend "
                        "scheduling a consultation with a cardiologist and an endocrinologist. "
                        f"{doc_sentence} After you visit the doctor and receive any new lab results or clinical "
                        "conclusions, you can use Update Health Information in the app whenever you are ready. "
                        "Tap the human silhouette to open the Safe and view your updated weekly reports."
                    )
                else:
                    msg = (
                        "Hello! Your weekly summary is ready. Key observations for the week have been saved—"
                        "tap the human silhouette to view your detailed reports in the Safe."
                    )
                return httpx.Response(200, json=_gemini_envelope({"message": msg}))

            # 4. Athena 168-Hour Referral Follow-up Reminder
            if "[Internal Athena Referral Follow-up Reminder Formulation]:" in full_text:
                self._reminder_attempt_counter += 1
                if self._reminder_attempt_counter <= self.fail_reminder_calls:
                    return httpx.Response(
                        503, json={"error": {"message": "Simulated Gemini reminder outage"}}
                    )
                self.reminder_prompts.append(full_text)
                if "DOCUMENT STATUS: READY IN SAFE" in full_text:
                    doc_note = "Your Health Observation Summary is ready in the Safe whenever you need it for the visit."
                elif "DOCUMENT STATUS: TEMPORARILY DELAYED (NOT READY)" in full_text:
                    doc_note = "Your Health Observation Summary is still delayed and not ready yet, as preparation is being retried."
                else:
                    doc_note = "Your Health Observation Summary is still being prepared and will appear in the Safe once ready."

                rem_msg = (
                    "Just checking in gently—have you had a chance to see the cardiologist or endocrinologist yet? "
                    f"{doc_note} Simply let me know here in chat if you have already completed the visit, and remember "
                    "you can use Update Health Information once you have new test results or doctor conclusions."
                )
                return httpx.Response(200, json=_gemini_envelope({"message": rem_msg}))

            # 5. Athena Daily Journal Closure (Stage A2.2)
            if (
                "ATHENA_GENERATED_INTERPRETATION_NOT_RAW_EVIDENCE" in full_text
                or "AthenaDailyAssessmentModelOutput" in full_text
                or "DAILY CLINICAL ASSESSMENT" in full_text.upper()
            ):
                return httpx.Response(
                    200,
                    json=_gemini_envelope(
                        {
                            "status": "ALERT",
                            "interpretation": (
                                "Patient reported clinical dynamics during ongoing monitoring; subjective symptoms "
                                "and referral follow-up status documented accurately."
                            ),
                            "observations": ["Afternoon palpitations recurred", "New headache reported"],
                            "relevant_changes": ["Recurrence of palpitations post-visit"],
                            "contradictions": [],
                            "uncertainties": [],
                            "red_flags": False,
                        }
                    ),
                )

            # 6. Weekly Consilium Pro (identified by pro model URL)
            if "pro-preview" in url_path or "-pro" in url_path:
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
                            "24-hour Holter ECG monitoring",
                            "Free T4 and thyroid antibody panel",
                        ],
                    }
                else:
                    pro_out["referral_decision"] = {
                        "recommended": False,
                        "urgency": "none",
                        "target_specialties": [],
                        "clinical_reason_for_referral": "",
                        "observed_syndromes_or_trends": [],
                        "recommended_workup_for_doctor_discussion": [],
                    }
                return httpx.Response(200, json=_gemini_envelope(pro_out))

            # 7. Default structured Flash call -> Weekly Consilium Flash
            return httpx.Response(200, json=_gemini_envelope(_sample_weekly_flash_output()))

        async def _noop_sleep(_delay: float) -> None:
            return None

        return GeminiGateway(
            api_key="test-stage-8a4-key",
            transport=httpx.MockTransport(handler),
            max_retries=1,
            sleep_func=_noop_sleep,
        )

    @staticmethod
    def _default_contextual_athena_responder(
        contents: List[Dict[str, Any]],
        sys_text: str,
        last_user_turn: str,
    ) -> str:
        """
        Simulates Gemini Flash evaluating natural dialogue in full context (active referral
        state + preceding curator question in `contents` and turn addendum) and emitting
        structured `[CLINICAL_REPORT]` JSON in the single turn call.
        """
        has_active_referral = "=== ACTIVE IN-PERSON MEDICAL REFERRAL CONTEXT ===" in sys_text

        # Extract the user's actual utterance from the turn wrapper
        import re as _re

        m = _re.search(r'(?:replied|inquiry / note|replied to routine question):\s*"([^"]+)"', last_user_turn)
        if m:
            utterance = m.group(1).strip()
        else:
            utterance = last_user_turn.strip()
        low = utterance.lower()

        # Inspect preceding model message in `contents` to verify contextual dialogue continuity
        preceding_model_text = ""
        for item in reversed(contents[:-1]):
            if item.get("role") == "model":
                preceding_model_text = "\n".join(
                    str(p.get("text", "")) for p in (item.get("parts") or [])
                )
                break

        asked_about_doctor = any(
            phrase in preceding_model_text.lower()
            for phrase in ("did you see the doctor", "see the cardiologist", "had a chance to see")
        )

        visit_event = "NONE"
        visit_note: Optional[str] = None
        symptoms: List[str] = []
        status_str = "NORMAL"
        red_flags = False
        reply_prose = "Thank you for sharing how you are feeling today. I have noted your update and we will continue observing calmly."

        if "chest pain" in low or "cannot breathe" in low or "heart rate peak 158" in last_user_turn.lower():
            status_str = "CRITICAL"
            red_flags = True
            symptoms = ["acute_chest_pain_or_critical_tachycardia"]
            reply_prose = (
                "Please sit down immediately, loosen any tight clothing, and call emergency services (112) "
                "right away because these symptoms require urgent medical care."
            )
            report = {
                "status": status_str,
                "symptoms_mentioned": symptoms,
                "subjective_wellbeing_score": 2,
                "adherence_to_plan": "HIGH",
                "red_flags": red_flags,
                "summary_for_master": "Acute emergency symptoms/vitals detected.",
                "referral_visit_event": "NONE",
            }
            return f"{reply_prose}\n\n[CLINICAL_REPORT]\n{json.dumps(report)}\n[/CLINICAL_REPORT]\n[EMERGENCY_TRIGGER]"

        if has_active_referral:
            # Short contextual replies that ONLY make sense when Athena's preceding message asked about the visit
            if low in ("i already did", "yes, on tuesday", "yes, yesterday"):
                assert asked_about_doctor, (
                    "Preceding curator question about the doctor visit was missing from Gemini contents!"
                )
                assert "Your immediately preceding message to the user was:" in last_user_turn, (
                    "Turn referral addendum did not include Athena's immediately preceding message!"
                )
                visit_event = "COMPLETED_VISIT"
                visit_note = f"Contextual reply '{utterance}' following Athena's doctor-visit question."
                reply_prose = (
                    "Thank you for letting me know you completed your appointment! I will stop sending reminders "
                    "about scheduling this visit. Whenever your new test results or doctor's conclusions are ready, "
                    "you can use Update Health Information in the app, and meanwhile we will continue regular daily observation."
                )
            elif low in ("not yet",):
                assert asked_about_doctor
                visit_event = "NOT_YET_OR_NEGATED"
                reply_prose = "No problem at all—take your time arranging the appointment whenever convenient."
            elif low in ("i only booked it", "i booked it for friday", "i booked an appointment."):
                visit_event = "FUTURE_OR_BOOKED"
                reply_prose = "Got it, glad you have the appointment scheduled! Let me know after the visit takes place."
            elif low in (
                "i visited the doctor yesterday.",
                "i had my appointment with the cardiologist.",
                "the doctor examined me.",
                "i have already seen the specialist.",
                "i went, but it wasn't helpful",
                "had my checkup at the clinic this morning—she listened to my heart and ordered labs.",
            ):
                visit_event = "COMPLETED_VISIT"
                visit_note = f"Patient reported completed in-person visit: {utterance}"
                reply_prose = (
                    "Thank you for updating me about your visit with the doctor. I have noted your self-report "
                    "so I will not send further reminders for this referral, while continuing our regular health observation. "
                    "Once you receive any new lab results or conclusions, feel free to run Update Health Information."
                )
            elif low in (
                "i will visit a doctor tomorrow.",
                "i'm thinking about seeing a doctor.",
            ):
                visit_event = "FUTURE_OR_BOOKED"
                reply_prose = "That sounds like a good plan—let me know how it goes once you have had the visit."
            elif low in (
                "i haven't visited the doctor yet.",
                "should i visit the doctor?",
                "my friend visited a doctor.",
                "the doctor called me.",
                "i wish i had gone to the doctor.",
            ):
                visit_event = "NOT_YET_OR_NEGATED"
                reply_prose = "Understood—an in-person examination is still recommended when you are able to schedule it."
            elif "clinic earlier" in low or "sort of dealt with the cardiology thing" in low:
                visit_event = "UNCLEAR_NEEDS_CLARIFICATION"
                reply_prose = (
                    "Just so I keep accurate notes—did you already have the in-person examination with the doctor, "
                    "or are you still in the process of arranging it?"
                )

        if "holter" in low or "how does the examination work" in low:
            reply_prose = (
                "Doctors typically order 24-hour Holter ECG monitoring to record your heart rhythm continuously "
                "during everyday activities and sleep. Small adhesive electrodes are placed on the chest and connected "
                "to a lightweight portable recorder; you may notice mild skin irritation or slight inconvenience "
                "from the adhesive pads, and showering is usually restricted while wearing it. Good questions to ask "
                "your doctor include whether to keep a symptom diary and how to prepare your skin beforehand."
            )

        if "worsened" in low or "palpitations came back" in low or "new headache" in low:
            status_str = "ALERT"
            symptoms = ["palpitations", "headache"]
            reply_prose = (
                "Thank you for telling me right away about these symptoms. Please rest comfortably and keep me posted "
                "if anything intensifies—I have logged this change for ongoing observation."
            )

        report_payload: Dict[str, Any] = {
            "status": status_str,
            "symptoms_mentioned": symptoms,
            "subjective_wellbeing_score": 7 if status_str == "NORMAL" else 5,
            "adherence_to_plan": "HIGH",
            "red_flags": red_flags,
            "summary_for_master": f"Turn processed ({utterance[:80]})",
            "referral_visit_event": visit_event,
            "referral_visit_context_note": visit_note,
        }
        return f"{reply_prose}\n\n[CLINICAL_REPORT]\n{json.dumps(report_payload)}\n[/CLINICAL_REPORT]"


async def _setup_cycle_with_weekly_referral(
    db: Session,
    gateway: GeminiGateway,
    *,
    patient_id: str = "pt_stage_8a4",
    t0: datetime = datetime(2026, 10, 18, 10, 0, 0, tzinfo=timezone.utc),
    generate_ready_document: bool = True,
    now_holder: Optional[List[datetime]] = None,
) -> Dict[str, Any]:
    """
    Seeds a completed Primary Consilium + 7 days of Athena logs, executes Week 1
    Weekly Consilium at `t_ref = t0 + 7 days` issuing a Pro Referral Decision, and
    optionally runs `ReferralDocumentService.generate_referral_document` so the
    referral document is ready in Safe.
    """
    _seed_patient(db, patient_id)
    cycle_repo = MedicalCycleRepository(db)
    cycle, _ = cycle_repo.get_or_create_active_for_patient(patient_id)
    cycle_id = cycle.cycle_id

    await _seed_completed_primary_consilium(db, cycle_id=cycle_id)
    _seed_schedule_anchor(db, cycle_id, t0)
    _seed_seven_day_athena_logs(db, cycle_id=cycle_id, sprint_id=f"sprint_{cycle_id}_w1"[:64])

    t_ref = t0 + timedelta(days=7)
    if now_holder is not None:
        now_holder[0] = t_ref
    clock = (lambda: now_holder[0]) if now_holder is not None else (lambda: t_ref)

    weekly_svc = WeeklyConsiliumService(db=db, gateway=gateway, now_provider=clock)
    await weekly_svc.run_weekly_consilium(
        cycle_id=cycle_id,
        sprint_id=f"sprint_{cycle_id}_w1"[:64],
        report_date=t_ref.strftime("%Y-%m-%d"),
    )

    doc_result = None
    if generate_ready_document:
        doc_svc = ReferralDocumentService(db=db, gateway=gateway, now_provider=clock)
        doc_result = await doc_svc.generate_referral_document(
            cycle_id=cycle_id,
            patient_id=patient_id,
        )

    ref_state = weekly_svc.get_referral_lifecycle_state(cycle_id)
    return {
        "patient_id": patient_id,
        "cycle_id": cycle_id,
        "t0": t0,
        "t_ref": t_ref,
        "referral_state": ref_state,
        "doc_result": doc_result,
    }


# ==============================================================================
# TESTS
# ==============================================================================


@pytest.mark.anyio
async def test_01_referral_in_weekly_summary_plain_english_and_no_raw_json(test_db):
    """
    Scenario 1: Weekly Pro recommends referral -> Step C (`send_weekly_wrapup_message`)
    explains the observed changes, reason for consultation, recommended specialists,
    document availability in Safe, and `Update Health Information` in calm plain English,
    without repeating raw Pro JSON or internal labels.
    """
    db, _ = test_db
    t0 = datetime(2026, 10, 18, 10, 0, 0, tzinfo=timezone.utc)
    now_box = [t0]
    harness = Stage8A4MockHarness(referral_recommended=True)
    gateway = harness.build_gateway()

    ctx = await _setup_cycle_with_weekly_referral(
        db, gateway, t0=t0, generate_ready_document=True, now_holder=now_box
    )
    cycle_id = ctx["cycle_id"]
    orig_created_at = ctx["referral_state"]["created_at_utc"]

    athena_svc = AthenaChatService(db=db, gateway=gateway, now_provider=lambda: now_box[0])
    resp = await athena_svc.send_weekly_wrapup_message(cycle_id=cycle_id, sprint_number=1)

    assert len(harness.weekly_wrapup_prompts) == 1
    wrapup_prompt = harness.weekly_wrapup_prompts[0]
    assert "ACTIVE IN-PERSON MEDICAL CONSULTATION RECOMMENDATION" in wrapup_prompt
    assert "cardiologist, endocrinologist" in wrapup_prompt
    assert "DOCUMENT STATUS: READY IN SAFE" in wrapup_prompt
    assert "Update Health Information" in wrapup_prompt

    # User-facing chat message must be calm plain English with zero raw JSON or internal labels
    reply = resp.reply
    assert "cardiologist" in reply.lower()
    assert "endocrinologist" in reply.lower()
    assert "Safe" in reply
    assert "Update Health Information" in reply
    for forbidden in ("referral_decision", "clinical_reason_for_referral", "SERVER_ONLY", "{", "}"):
        assert forbidden not in reply

    # Lifecycle state records initial wrap-up notification without altering created_at_utc
    weekly_svc = WeeklyConsiliumService(db=db, gateway=gateway, now_provider=lambda: now_box[0])
    updated_ref = weekly_svc.get_referral_lifecycle_state(cycle_id)
    assert updated_ref["created_at_utc"] == orig_created_at
    assert updated_ref["initial_wrapup_notified_at_utc"] == now_box[0].isoformat()
    assert updated_ref["last_weekly_wrapup_sprint_number"] == 1


@pytest.mark.anyio
async def test_02_athena_distinguishes_ready_generating_and_failed_document_without_blocking(test_db):
    """
    Scenario 2: Athena accurately distinguishes `document_ready`,
    `referral_accepted_generating_document`, and `generation_temporarily_failed`
    in both Weekly Summary and 168-hour reminders, never claiming an ungenerated or
    failed document is ready, and never blocking Weekly Step C. Also verifies the
    race guard if document generation completes after `visit_confirmed`.
    """
    db, _ = test_db
    t0 = datetime(2026, 10, 18, 10, 0, 0, tzinfo=timezone.utc)
    now_box = [t0]
    harness = Stage8A4MockHarness(referral_recommended=True)
    gateway = harness.build_gateway()

    # Start with referral created, document NOT generated yet
    ctx = await _setup_cycle_with_weekly_referral(
        db, gateway, patient_id="pt_doc_states", t0=t0, generate_ready_document=False, now_holder=now_box
    )
    cycle_id = ctx["cycle_id"]
    weekly_svc = WeeklyConsiliumService(db=db, gateway=gateway, now_provider=lambda: now_box[0])
    athena_svc = AthenaChatService(db=db, gateway=gateway, now_provider=lambda: now_box[0])

    # State A: generating_document (pending)
    state_a = weekly_svc.get_referral_state_for_athena(cycle_id)
    assert state_a["athena_document_state"] == "referral_accepted_generating_document"

    resp_generating = await athena_svc.send_weekly_wrapup_message(
        cycle_id=cycle_id, sprint_number=1, idempotency_key="wrapup_gen_state"
    )
    assert "DOCUMENT STATUS: CURRENTLY BEING PREPARED (NOT READY YET)" in harness.weekly_wrapup_prompts[-1]
    assert "being prepared" in resp_generating.reply.lower()
    assert "ready right now" not in resp_generating.reply.lower()

    # State B: generation_temporarily_failed -> 168h reminder must NOT claim document is ready
    weekly_svc.record_referral_document_generation_status(
        cycle_id, generation_state="retryable_failure", error_type="GeminiGatewayError"
    )
    state_b = weekly_svc.get_referral_state_for_athena(cycle_id)
    assert state_b["athena_document_state"] == "generation_temporarily_failed"

    now_box[0] = ctx["t_ref"] + timedelta(hours=168)
    rem_failed_doc = await athena_svc.send_referral_followup_reminder(
        cycle_id=cycle_id,
        referral_id=state_b["referral_id"],
        target_interval=1,
    )
    assert rem_failed_doc is not None
    assert "DOCUMENT STATUS: TEMPORARILY DELAYED (NOT READY)" in harness.reminder_prompts[-1]
    assert "not ready" in rem_failed_doc.reply.lower()

    # State C: Mid-flight concurrency race — background document generation starts while active,
    # and user confirms visit (`visit_confirmed`) while Pro is authoring the document!
    # The commit guard links the document into Safe WITHOUT reverting status='visit_confirmed'.
    now_box[0] = ctx["t_ref"] + timedelta(hours=170)

    def _confirm_visit_during_inflight_doc() -> None:
        weekly_svc.confirm_referral_visit(
            cycle_id=cycle_id,
            turn_id="turn_inflight_confirm",
            confirmed_at_utc=now_box[0].isoformat(),
            commit=True,
        )

    harness.on_doc_pro_inflight = _confirm_visit_during_inflight_doc
    doc_svc = ReferralDocumentService(db=db, gateway=gateway, now_provider=lambda: now_box[0])
    doc_res = await doc_svc.generate_referral_document(cycle_id=cycle_id, patient_id="pt_doc_states")
    harness.on_doc_pro_inflight = None

    assert doc_res.status == "visit_confirmed"
    final_ref = weekly_svc.get_referral_lifecycle_state(cycle_id)
    assert final_ref["status"] == "visit_confirmed"
    assert final_ref["document_report_id"] == doc_res.document_report_id
    assert doc_res.document_report_id in final_ref["document_history_report_ids"]


@pytest.mark.anyio
async def test_03_reminder_not_sent_before_168_hours(test_db):
    """
    Scenario 3: At t = 0h, 24h, 120h, and 167h 59m 59s after referral creation,
    ZERO follow-up reminders are sent, even if `process_due_jobs()` runs repeatedly
    or a reminder job is manually enqueued prematurely.
    """
    db, _ = test_db
    t0 = datetime(2026, 10, 18, 10, 0, 0, tzinfo=timezone.utc)
    now_box = [t0]
    harness = Stage8A4MockHarness(referral_recommended=True)
    gateway = harness.build_gateway()

    ctx = await _setup_cycle_with_weekly_referral(
        db, gateway, patient_id="pt_before_168h", t0=t0, generate_ready_document=True, now_holder=now_box
    )
    cycle_id = ctx["cycle_id"]
    t_ref = ctx["t_ref"]
    ref_id = ctx["referral_state"]["referral_id"]

    # Complete initial Step C announcement at t_ref
    athena_svc = AthenaChatService(db=db, gateway=gateway, now_provider=lambda: now_box[0])
    await athena_svc.send_weekly_wrapup_message(cycle_id=cycle_id, sprint_number=1)

    auto_svc = ClinicalAutomationService(
        db=db, gateway=gateway, now_provider=lambda: now_box[0], enable_daily_journal=False, enable_wellness_daily=False
    )
    weekly_svc = WeeklyConsiliumService(db=db, gateway=gateway, now_provider=lambda: now_box[0])

    for delta in (
        timedelta(hours=0),
        timedelta(hours=24),
        timedelta(hours=120),
        timedelta(hours=167, minutes=59, seconds=59),
    ):
        now_box[0] = t_ref + delta
        sched = weekly_svc.get_referral_followup_reminder_schedule(cycle_id, as_of=now_box[0])
        assert sched["eligible"] is True
        assert sched["is_due"] is False
        await auto_svc.process_due_jobs(discover=True, discover_daily_journals=False)
        assert len(harness.reminder_prompts) == 0

    # Even if a job was prematurely marked available now at 167h59m, execution guard skips with 0 LLM calls
    premature_job = auto_svc.enqueue_referral_followup_reminder(
        cycle_id=cycle_id,
        patient_id="pt_before_168h",
        referral_id=ref_id,
        target_interval=1,
        available_at_utc=now_box[0],
    )
    assert premature_job is not None
    claimed = auto_svc.claim_job(premature_job.job_id)
    completed = await auto_svc.execute_claimed_job(claimed)
    assert completed.status == AutomationJobStatus.COMPLETED
    assert completed.result_metadata["skipped"] is True
    assert completed.result_metadata["reason"] == "not_due_before_168h"
    assert len(harness.reminder_prompts) == 0
    assert weekly_svc.get_referral_lifecycle_state(cycle_id)["reminder_count"] == 0


@pytest.mark.anyio
async def test_04_after_168_hours_one_reminder_and_idempotent_across_reruns_and_parallel_calls(test_db):
    """
    Scenario 4: At t = 168h, exactly ONE follow-up reminder is sent (`reminder_count == 1`,
    `last_reminder_at_utc` set). Repeated `process_due_jobs()` runs and duplicate/parallel
    `send_referral_followup_reminder` calls within the same 168h interval send ZERO
    duplicates. Advancing another 168h (t = 336h) sends Reminder #2 (`reminder_count == 2`).
    """
    db, _ = test_db
    t0 = datetime(2026, 10, 18, 10, 0, 0, tzinfo=timezone.utc)
    now_box = [t0]
    harness = Stage8A4MockHarness(referral_recommended=True)
    gateway = harness.build_gateway()

    ctx = await _setup_cycle_with_weekly_referral(
        db, gateway, patient_id="pt_168h_exact", t0=t0, generate_ready_document=True, now_holder=now_box
    )
    cycle_id = ctx["cycle_id"]
    t_ref = ctx["t_ref"]
    ref_id = ctx["referral_state"]["referral_id"]

    athena_svc = AthenaChatService(db=db, gateway=gateway, now_provider=lambda: now_box[0])
    await athena_svc.send_weekly_wrapup_message(cycle_id=cycle_id, sprint_number=1)

    auto_svc = ClinicalAutomationService(
        db=db, gateway=gateway, now_provider=lambda: now_box[0], enable_daily_journal=False, enable_wellness_daily=False
    )
    weekly_svc = WeeklyConsiliumService(db=db, gateway=gateway, now_provider=lambda: now_box[0])

    # Advance to exactly t_ref + 168 hours
    now_box[0] = t_ref + timedelta(hours=168)
    # Only run referral reminder jobs (avoid running Week 2 consilium by not having 7 days of W2 journals)
    discovered = auto_svc.discover_and_enqueue_jobs(discover_daily_journals=False)
    rem_jobs = [j for j in discovered if j.job_type == AutomationJobType.REFERRAL_FOLLOWUP_REMINDER]
    assert len(rem_jobs) == 1

    claimed = auto_svc.claim_job(rem_jobs[0].job_id)
    done = await auto_svc.execute_claimed_job(claimed)
    assert done.status == AutomationJobStatus.COMPLETED
    assert done.result_metadata["skipped"] is False
    assert len(harness.reminder_prompts) == 1

    state_after_1 = weekly_svc.get_referral_lifecycle_state(cycle_id)
    assert state_after_1["reminder_count"] == 1
    assert state_after_1["covered_up_to_interval"] == 1
    assert state_after_1["last_reminder_at_utc"] == now_box[0].isoformat()

    # Re-running discovery & direct reminder calls at t=168h, t=200h, t=335h must produce 0 extra reminders
    for check_dt in (
        t_ref + timedelta(hours=168, minutes=5),
        t_ref + timedelta(hours=200),
        t_ref + timedelta(hours=335, minutes=59),
    ):
        now_box[0] = check_dt
        more = [
            j
            for j in auto_svc.discover_and_enqueue_jobs(discover_daily_journals=False)
            if j.job_type == AutomationJobType.REFERRAL_FOLLOWUP_REMINDER
        ]
        assert more == []
        dup_resp = await athena_svc.send_referral_followup_reminder(
            cycle_id=cycle_id,
            referral_id=ref_id,
            target_interval=1,
            idempotency_key="different_idem_key_same_interval_1",
        )
        assert dup_resp is None
    assert len(harness.reminder_prompts) == 1

    # Advance to t_ref + 336h (Interval 2) -> Reminder #2 fires once
    now_box[0] = t_ref + timedelta(hours=336)
    rem_jobs_2 = [
        j
        for j in auto_svc.discover_and_enqueue_jobs(discover_daily_journals=False)
        if j.job_type == AutomationJobType.REFERRAL_FOLLOWUP_REMINDER
    ]
    assert len(rem_jobs_2) == 1
    claimed_2 = auto_svc.claim_job(rem_jobs_2[0].job_id)
    done_2 = await auto_svc.execute_claimed_job(claimed_2)
    assert done_2.status == AutomationJobStatus.COMPLETED
    assert done_2.result_metadata["skipped"] is False
    assert len(harness.reminder_prompts) == 2
    assert "Reminder #2" in harness.reminder_prompts[-1]
    assert weekly_svc.get_referral_lifecycle_state(cycle_id)["reminder_count"] == 2


@pytest.mark.anyio
async def test_05_anti_duplication_weekly_summary_and_168h_reminder_plus_no_timer_reset(test_db):
    """
    Scenario 5:
    - Subsequent Weekly Consilium NEVER resets `created_at_utc` / 168-hour countdown.
    - When Weekly Summary (Step C) runs at t = 168h, it delivers the referral follow-up
      and marks Interval 1 covered so no standalone 168h reminder duplicates it.
    - Conversely, if a standalone 168h reminder already fired right before Weekly Summary,
      Weekly Summary omits the duplicate referral block.
    """
    db, _ = test_db
    t0 = datetime(2026, 10, 18, 10, 0, 0, tzinfo=timezone.utc)
    now_box = [t0]
    harness = Stage8A4MockHarness(referral_recommended=True)
    gateway = harness.build_gateway()

    # Subcase A & B: Weekly Summary runs at Week 2 (t_ref + 168h)
    ctx = await _setup_cycle_with_weekly_referral(
        db, gateway, patient_id="pt_antidup_a", t0=t0, generate_ready_document=True, now_holder=now_box
    )
    cycle_id = ctx["cycle_id"]
    t_ref = ctx["t_ref"]
    orig_created_at = ctx["referral_state"]["created_at_utc"]

    athena_svc = AthenaChatService(db=db, gateway=gateway, now_provider=lambda: now_box[0])
    weekly_svc = WeeklyConsiliumService(db=db, gateway=gateway, now_provider=lambda: now_box[0])
    auto_svc = ClinicalAutomationService(
        db=db, gateway=gateway, now_provider=lambda: now_box[0], enable_daily_journal=False, enable_wellness_daily=False
    )

    # Week 1 wrap-up at t_ref
    await athena_svc.send_weekly_wrapup_message(cycle_id=cycle_id, sprint_number=1)

    # At t_ref + 120h, run another Weekly Consilium -> created_at_utc MUST NOT reset!
    now_box[0] = t_ref + timedelta(hours=120)
    _seed_seven_day_athena_logs(db, cycle_id=cycle_id, sprint_id=f"sprint_{cycle_id}_w2"[:64])
    await weekly_svc.run_weekly_consilium(
        cycle_id=cycle_id,
        sprint_id=f"sprint_{cycle_id}_w2"[:64],
        report_date=now_box[0].strftime("%Y-%m-%d"),
    )
    mid_state = weekly_svc.get_referral_lifecycle_state(cycle_id)
    assert mid_state["created_at_utc"] == orig_created_at

    # At t_ref + 168h, enqueue a due Weekly Step C job: `discover_and_enqueue_jobs` yields to it!
    now_box[0] = t_ref + timedelta(hours=168)
    step_c_job = auto_svc.enqueue_or_get_job(
        patient_id="pt_antidup_a",
        cycle_id=cycle_id,
        job_type=AutomationJobType.WEEKLY_CONSILIUM_C,
        idempotency_key=f"weekly_c_{cycle_id}_w2",
        next_attempt_at_utc=now_box[0].isoformat(),
    )
    assert step_c_job is not None
    yielded = [
        j
        for j in auto_svc.discover_and_enqueue_jobs(discover_daily_journals=False)
        if j.job_type == AutomationJobType.REFERRAL_FOLLOWUP_REMINDER
    ]
    assert yielded == [], "Discovery must yield when Weekly Consilium Step C is due!"

    # Execute Step C -> delivers referral follow-up for Interval 1
    claimed_c = auto_svc.claim_job(step_c_job.job_id)
    await auto_svc.execute_claimed_job(claimed_c)
    assert "ACTIVE IN-PERSON MEDICAL CONSULTATION RECOMMENDATION" in harness.weekly_wrapup_prompts[-1]

    state_after_w2_wrapup = weekly_svc.get_referral_lifecycle_state(cycle_id)
    assert state_after_w2_wrapup["covered_up_to_interval"] == 1
    assert state_after_w2_wrapup["created_at_utc"] == orig_created_at

    # Now `discover_and_enqueue_jobs` at t_ref + 168h..335h must NOT send a duplicate standalone reminder!
    after_c_rem = [
        j
        for j in auto_svc.discover_and_enqueue_jobs(discover_daily_journals=False)
        if j.job_type == AutomationJobType.REFERRAL_FOLLOWUP_REMINDER
    ]
    assert after_c_rem == []
    assert len(harness.reminder_prompts) == 0

    # Subcase C: Standalone 168h reminder runs first at t=168h, then Weekly Summary runs at t=172h
    now_box[0] = t0
    ctx_b = await _setup_cycle_with_weekly_referral(
        db, gateway, patient_id="pt_antidup_b", t0=t0, generate_ready_document=True, now_holder=now_box
    )
    cycle_b = ctx_b["cycle_id"]
    ref_b = ctx_b["referral_state"]["referral_id"]
    await athena_svc.send_weekly_wrapup_message(cycle_id=cycle_b, sprint_number=1)

    now_box[0] = ctx_b["t_ref"] + timedelta(hours=168)
    rem_resp = await athena_svc.send_referral_followup_reminder(
        cycle_id=cycle_b, referral_id=ref_b, target_interval=1
    )
    assert rem_resp is not None

    now_box[0] = ctx_b["t_ref"] + timedelta(hours=172)
    await athena_svc.send_weekly_wrapup_message(cycle_id=cycle_b, sprint_number=2)
    assert "ACTIVE IN-PERSON MEDICAL CONSULTATION RECOMMENDATION" not in harness.weekly_wrapup_prompts[-1]


@pytest.mark.anyio
async def test_06_contextual_completed_visit_full_and_short_dialogue_replies_single_llm_call(test_db):
    """
    Scenario 6 + User Clarification:
    - Verifies ZERO regex/keyword visit-matching lists exist in production services.
    - Verifies full natural statements AND short contextual replies after Athena asked
      `"Did you see the doctor?"` (`"I already did"`, `"Yes, on Tuesday"`, `"Yes, yesterday"`,
      `"I went, but it wasn't helpful"`, plus novel phrasings) confirm the visit in a
      SINGLE Gemini Flash call per turn.
    - Verifies consecutive curator messages (e.g. routine reply + 168h reminder / question)
      are preserved in `_build_gemini_contents`.
    - Verifies `visit_confirmed` state, timestamp, turn ID, pending reminder cancellation,
      Safe document history retention, and NO automatic Neo launch.
    """
    # 1. Production source inspection: NO hardcoded visit phrase regexes or keyword lists
    chat_src = inspect.getsource(athena_chat_module)
    weekly_src = inspect.getsource(weekly_consilium_module)
    for forbidden_snippet in (
        "i visited the doctor",
        "i already did",
        "the doctor examined me",
        "had my appointment",
    ):
        assert forbidden_snippet not in chat_src.lower()
        assert forbidden_snippet not in weekly_src.lower()

    db, _ = test_db
    t0 = datetime(2026, 10, 18, 10, 0, 0, tzinfo=timezone.utc)
    now_box = [t0]
    harness = Stage8A4MockHarness(referral_recommended=True)
    gateway = harness.build_gateway()

    completed_utterances = [
        ("I visited the doctor yesterday.", False),
        ("I had my appointment with the cardiologist.", False),
        ("The doctor examined me.", False),
        ("I have already seen the specialist.", False),
        ("I went, but it wasn't helpful", False),
        ("Had my checkup at the clinic this morning—she listened to my heart and ordered labs.", False),
        ("I already did", True),
        ("Yes, on Tuesday", True),
        ("Yes, yesterday", True),
    ]

    for idx, (utterance, needs_preceding_question) in enumerate(completed_utterances):
        pid = f"pt_visit_ok_{idx}"
        now_box[0] = t0
        ctx = await _setup_cycle_with_weekly_referral(
            db, gateway, patient_id=pid, t0=t0, generate_ready_document=True, now_holder=now_box
        )
        cycle_id = ctx["cycle_id"]
        ref_id = ctx["referral_state"]["referral_id"]
        doc_id = ctx["referral_state"]["document_report_id"]

        weekly_svc = WeeklyConsiliumService(db=db, gateway=gateway, now_provider=lambda: now_box[0])
        athena_svc = AthenaChatService(db=db, gateway=gateway, now_provider=lambda: now_box[0])
        auto_svc = ClinicalAutomationService(
            db=db, gateway=gateway, now_provider=lambda: now_box[0], enable_daily_journal=False, enable_wellness_daily=False
        )

        # Enqueue a pending 168h reminder to verify it gets cancelled when the visit is confirmed
        pending_rem = auto_svc.enqueue_referral_followup_reminder(
            cycle_id=cycle_id,
            patient_id=pid,
            referral_id=ref_id,
            target_interval=1,
            available_at_utc=ctx["t_ref"] + timedelta(hours=168),
        )
        assert pending_rem is not None

        if needs_preceding_question:
            # Seed TWO consecutive curator messages to prove `_build_gemini_contents` merges them
            # instead of dropping Athena's second message ("Did you see the doctor?")!
            weekly_svc.save_athena_chat_message(
                cycle_id=cycle_id,
                sender="user",
                text="Good morning, I slept well.",
                timestamp=(ctx["t_ref"] + timedelta(hours=1)).isoformat(),
            )
            weekly_svc.save_athena_chat_message(
                cycle_id=cycle_id,
                sender="curator",
                text="Glad you rested well! I have logged your morning check-in.",
                timestamp=(ctx["t_ref"] + timedelta(hours=1, minutes=1)).isoformat(),
            )
            weekly_svc.save_athena_chat_message(
                cycle_id=cycle_id,
                sender="curator",
                text="By the way, did you see the doctor yet regarding the cardiology referral?",
                timestamp=(ctx["t_ref"] + timedelta(hours=2)).isoformat(),
            )

        now_box[0] = ctx["t_ref"] + timedelta(hours=5)
        calls_before = len(harness.calls)
        turn_resp = await athena_svc.handle_turn(
            patient_id=pid,
            cycle_id=cycle_id,
            request=AthenaChatRequest(event_type="user_message", message=utterance),
        )
        calls_after = len(harness.calls)

        # Strictly ONE Gemini Flash call for the entire conversational turn + visit recognition!
        assert calls_after - calls_before == 1

        ref_after = weekly_svc.get_referral_lifecycle_state(cycle_id)
        assert ref_after["status"] == "visit_confirmed", f"Failed for utterance: {utterance}"
        assert ref_after["visit_confirmed_at_utc"] == now_box[0].isoformat()
        assert ref_after["visit_confirmation_turn_id"] == turn_resp.message_id
        assert ref_after["visit_confirmation_source"] == "patient_self_report_structured_event"

        # Pending reminder job was cancelled atomically
        refreshed_job = auto_svc.get_job_by_id(pending_rem.job_id)
        assert refreshed_job.status == AutomationJobStatus.COMPLETED
        assert refreshed_job.result_metadata["skipped"] is True
        assert refreshed_job.result_metadata["reason"] == "visit_confirmed"

        # Advancing 168h and 336h sends ZERO referral follow-up reminders
        for future_hours in (168, 336):
            now_box[0] = ctx["t_ref"] + timedelta(hours=future_hours)
            due_rem_jobs = [
                j
                for j in auto_svc.discover_and_enqueue_jobs(discover_daily_journals=False)
                if j.job_type == AutomationJobType.REFERRAL_FOLLOWUP_REMINDER and j.cycle_id == cycle_id
            ]
            assert due_rem_jobs == []

        # Safe document remains accessible in patient document history and exportable!
        client_docs = ModelReportRepository(db).list_client_facing(
            cycle_id=cycle_id,
            report_type=REFERRAL_DOCTOR_DOCUMENT_REPORT_TYPE,
        )
        assert any(d.report_id == doc_id for d in client_docs)
        export_svc = DoctorDocumentExportService(db=db)
        pro_doc_parsed, doc_meta = export_svc.extract_document_data(client_docs[0])
        assert doc_meta["report_id"] == doc_id
        assert pro_doc_parsed.document_title == REFERRAL_DOCUMENT_CANONICAL_TITLE

        # Cycle remains active — Neo was NOT automatically started
        cycle_row = MedicalCycleRepository(db).get(cycle_id)
        assert cycle_row.status == "active"
        all_cycles = MedicalCycleRepository(db).list_by_patient(pid)
        assert len(all_cycles) == 1


@pytest.mark.anyio
async def test_07_non_completed_future_negation_question_third_party_and_short_contextual_non_visits(test_db):
    """
    Scenario 7 + User Clarification:
    Future plans, bookings, negations, questions, third-party visits, phone calls,
    counterfactuals, AND short contextual non-visit replies (`"Not yet"`,
    `"I only booked it"`, `"I booked it for Friday"`) MUST NOT confirm the visit
    or stop scheduled 168-hour reminders.
    """
    db, _ = test_db
    t0 = datetime(2026, 10, 18, 10, 0, 0, tzinfo=timezone.utc)
    now_box = [t0]
    harness = Stage8A4MockHarness(referral_recommended=True)
    gateway = harness.build_gateway()

    ctx = await _setup_cycle_with_weekly_referral(
        db, gateway, patient_id="pt_non_visits", t0=t0, generate_ready_document=True, now_holder=now_box
    )
    cycle_id = ctx["cycle_id"]
    t_ref = ctx["t_ref"]

    weekly_svc = WeeklyConsiliumService(db=db, gateway=gateway, now_provider=lambda: now_box[0])
    athena_svc = AthenaChatService(db=db, gateway=gateway, now_provider=lambda: now_box[0])
    await athena_svc.send_weekly_wrapup_message(cycle_id=cycle_id, sprint_number=1)

    non_completed_messages = [
        ("I will visit a doctor tomorrow.", False),
        ("I booked an appointment.", False),
        ("I booked it for Friday", False),
        ("I'm thinking about seeing a doctor.", False),
        ("I haven't visited the doctor yet.", False),
        ("Should I visit the doctor?", False),
        ("My friend visited a doctor.", False),
        ("The doctor called me.", False),
        ("I wish I had gone to the doctor.", False),
        ("Not yet", True),
        ("I only booked it", True),
    ]

    for step_idx, (msg, ask_first) in enumerate(non_completed_messages, start=1):
        now_box[0] = t_ref + timedelta(hours=step_idx)
        if ask_first:
            weekly_svc.save_athena_chat_message(
                cycle_id=cycle_id,
                sender="curator",
                text="Did you see the doctor yet?",
                timestamp=(now_box[0] - timedelta(minutes=2)).isoformat(),
            )
        await athena_svc.handle_turn(
            patient_id="pt_non_visits",
            cycle_id=cycle_id,
            request=AthenaChatRequest(event_type="user_message", message=msg),
        )
        state = weekly_svc.get_referral_lifecycle_state(cycle_id)
        assert state["status"] == "document_ready", f"Unexpectedly confirmed visit for: {msg}"
        assert state["visit_confirmed_at_utc"] is None

    # At t_ref + 168h, reminder is STILL scheduled and sent!
    now_box[0] = t_ref + timedelta(hours=168)
    sched = weekly_svc.get_referral_followup_reminder_schedule(cycle_id, as_of=now_box[0])
    assert sched["eligible"] is True
    assert sched["is_due"] is True


@pytest.mark.anyio
async def test_08_ambiguous_message_clarifies_and_prose_without_structured_event_rejected(test_db):
    """
    Scenario 8:
    - Ambiguous statement (`UNCLEAR_NEEDS_CLARIFICATION`) prompts a brief clarifying
      question without marking `visit_confirmed`.
    - Free-form model prose claiming a visit occurred WITHOUT structured
      `"referral_visit_event": "COMPLETED_VISIT"` in `[CLINICAL_REPORT]` (or with
      malformed `[CLINICAL_REPORT]` JSON) MUST NOT confirm the visit!
    """
    db, _ = test_db
    t0 = datetime(2026, 10, 18, 10, 0, 0, tzinfo=timezone.utc)
    now_box = [t0]
    harness = Stage8A4MockHarness(referral_recommended=True)
    gateway = harness.build_gateway()

    ctx = await _setup_cycle_with_weekly_referral(
        db, gateway, patient_id="pt_ambiguous", t0=t0, generate_ready_document=True, now_holder=now_box
    )
    cycle_id = ctx["cycle_id"]
    weekly_svc = WeeklyConsiliumService(db=db, gateway=gateway, now_provider=lambda: now_box[0])
    athena_svc = AthenaChatService(db=db, gateway=gateway, now_provider=lambda: now_box[0])

    # 1. Ambiguous message -> clarifies without closing
    now_box[0] = ctx["t_ref"] + timedelta(hours=3)
    amb_resp = await athena_svc.handle_turn(
        patient_id="pt_ambiguous",
        cycle_id=cycle_id,
        request=AthenaChatRequest(
            event_type="user_message",
            message="I sort of dealt with the cardiology thing at the clinic earlier.",
        ),
    )
    assert "?" in amb_resp.reply
    assert weekly_svc.get_referral_lifecycle_state(cycle_id)["status"] == "document_ready"

    # 2. Free-form prose acknowledging visit WITHOUT structured COMPLETED_VISIT -> MUST NOT confirm
    def _prose_only_responder(_body: Dict[str, Any], _sys: str, _turn: str) -> str:
        return (
            "Wonderful, I see that you visited the cardiologist yesterday! Glad the appointment is done.\n\n"
            "[CLINICAL_REPORT]\n"
            '{"status": "NORMAL", "symptoms_mentioned": [], "subjective_wellbeing_score": 8, '
            '"adherence_to_plan": "HIGH", "red_flags": false, "summary_for_master": "Routine turn"}\n'
            "[/CLINICAL_REPORT]"
        )

    harness.custom_chat_responder = _prose_only_responder
    now_box[0] = ctx["t_ref"] + timedelta(hours=4)
    await athena_svc.handle_turn(
        patient_id="pt_ambiguous",
        cycle_id=cycle_id,
        request=AthenaChatRequest(event_type="user_message", message="I visited the doctor yesterday."),
    )
    assert weekly_svc.get_referral_lifecycle_state(cycle_id)["status"] == "document_ready"

    # 3. Malformed [CLINICAL_REPORT] JSON even with prose -> MUST NOT confirm
    def _broken_json_responder(_body: Dict[str, Any], _sys: str, _turn: str) -> str:
        return (
            "Got it, visit completed!\n\n"
            "[CLINICAL_REPORT]\n{broken_json_here: COMPLETED_VISIT\n[/CLINICAL_REPORT]"
        )

    harness.custom_chat_responder = _broken_json_responder
    now_box[0] = ctx["t_ref"] + timedelta(hours=5)
    await athena_svc.handle_turn(
        patient_id="pt_ambiguous",
        cycle_id=cycle_id,
        request=AthenaChatRequest(event_type="user_message", message="I visited the doctor yesterday."),
    )
    assert weekly_svc.get_referral_lifecycle_state(cycle_id)["status"] == "document_ready"


@pytest.mark.anyio
async def test_09_gemini_failure_safety_on_chat_turn_and_reminder_delivery(test_db):
    """
    Scenario 9:
    - Gemini failure during `handle_turn` raises HTTP 502 and never sets `visit_confirmed`.
    - Gemini failure during 168-hour `REFERRAL_FOLLOWUP_REMINDER` does NOT mark the
      reminder sent (`reminder_count == 0`), transitions the job to `RETRYABLE_FAILURE`,
      and succeeds cleanly on the subsequent retry after backoff (`reminder_count == 1`).
    """
    db, _ = test_db
    t0 = datetime(2026, 10, 18, 10, 0, 0, tzinfo=timezone.utc)
    now_box = [t0]
    harness = Stage8A4MockHarness(referral_recommended=True, fail_reminder_calls=1)
    gateway = harness.build_gateway()

    ctx = await _setup_cycle_with_weekly_referral(
        db, gateway, patient_id="pt_fail_safety", t0=t0, generate_ready_document=True, now_holder=now_box
    )
    cycle_id = ctx["cycle_id"]
    t_ref = ctx["t_ref"]

    weekly_svc = WeeklyConsiliumService(db=db, gateway=gateway, now_provider=lambda: now_box[0])
    athena_svc = AthenaChatService(db=db, gateway=gateway, now_provider=lambda: now_box[0])
    await athena_svc.send_weekly_wrapup_message(cycle_id=cycle_id, sprint_number=1)

    # 1. Chat turn Gemini failure -> HTTP 502, referral remains active
    harness.fail_chat_turn = True
    now_box[0] = t_ref + timedelta(hours=6)
    with pytest.raises(Exception) as exc_info:
        await athena_svc.handle_turn(
            patient_id="pt_fail_safety",
            cycle_id=cycle_id,
            request=AthenaChatRequest(event_type="user_message", message="I visited the doctor yesterday."),
        )
    assert getattr(exc_info.value, "status_code", None) == 502
    assert weekly_svc.get_referral_lifecycle_state(cycle_id)["status"] == "document_ready"
    harness.fail_chat_turn = False

    # 2. Reminder failure on Attempt 1 -> RETRYABLE_FAILURE, reminder_count == 0
    auto_svc = ClinicalAutomationService(
        db=db,
        gateway=gateway,
        now_provider=lambda: now_box[0],
        enable_daily_journal=False, enable_wellness_daily=False,
        base_backoff_seconds=60,
    )
    now_box[0] = t_ref + timedelta(hours=168)
    rem_jobs = [
        j
        for j in auto_svc.discover_and_enqueue_jobs(discover_daily_journals=False)
        if j.job_type == AutomationJobType.REFERRAL_FOLLOWUP_REMINDER
    ]
    assert len(rem_jobs) == 1
    claimed_1 = auto_svc.claim_job(rem_jobs[0].job_id)
    failed_1 = await auto_svc.execute_claimed_job(claimed_1)
    assert failed_1.status == AutomationJobStatus.RETRYABLE_FAILURE
    assert weekly_svc.get_referral_lifecycle_state(cycle_id)["reminder_count"] == 0
    assert weekly_svc.get_referral_lifecycle_state(cycle_id)["last_reminder_at_utc"] is None

    # Attempt 2 after 60s backoff -> succeeds cleanly once!
    now_box[0] = t_ref + timedelta(hours=168, seconds=65)
    claimed_2 = auto_svc.claim_job(rem_jobs[0].job_id)
    assert claimed_2 is not None
    succeeded_2 = await auto_svc.execute_claimed_job(claimed_2)
    assert succeeded_2.status == AutomationJobStatus.COMPLETED
    assert succeeded_2.result_metadata["skipped"] is False
    assert weekly_svc.get_referral_lifecycle_state(cycle_id)["reminder_count"] == 1


@pytest.mark.anyio
async def test_10_continuous_observation_post_visit_exam_explanation_and_update_health_info(test_db):
    """
    Scenario 10:
    - Both during an active referral and AFTER `visit_confirmed`, normal Athena dialogue
      continues naturally without turning every turn into doctor reminders.
    - Athena's system prompt enforces safe examination explanations (never promising
      painlessness, never diagnosing) and accurate `Update Health Information` rules.
    - `AthenaDailyJournalService` continues closing daily clinical journals with symptom
      dynamics (worsening, stabilization, new complaints).
    """
    db, _ = test_db
    t0 = datetime(2026, 10, 18, 10, 0, 0, tzinfo=timezone.utc)
    now_box = [t0]
    harness = Stage8A4MockHarness(referral_recommended=True)
    gateway = harness.build_gateway()

    ctx = await _setup_cycle_with_weekly_referral(
        db, gateway, patient_id="pt_continuous", t0=t0, generate_ready_document=True, now_holder=now_box
    )
    cycle_id = ctx["cycle_id"]
    t_ref = ctx["t_ref"]

    athena_svc = AthenaChatService(db=db, gateway=gateway, now_provider=lambda: now_box[0])

    # Patient asks how Holter ECG examination works while referral is active
    now_box[0] = t_ref + timedelta(hours=2)
    exam_resp = await athena_svc.handle_turn(
        patient_id="pt_continuous",
        cycle_id=cycle_id,
        request=AthenaChatRequest(
            event_type="out_of_turn_message",
            message="How does the 24-hour Holter examination work and is it uncomfortable?",
        ),
    )
    sys_prompt_active = harness.chat_turn_calls[-1]["system_instruction"]
    assert "NEVER promise that any examination is completely painless" in sys_prompt_active
    assert "NEVER present your explanation or any result interpretation as a confirmed clinical diagnosis" in sys_prompt_active
    assert "Visiting a doctor does NOT automatically trigger a full health recalculation" in sys_prompt_active
    assert "Receiving new test results does NOT trigger `Update Health Information` automatically" in sys_prompt_active
    assert "painless" not in exam_resp.reply.lower()

    # Patient confirms doctor visit
    now_box[0] = t_ref + timedelta(hours=4)
    await athena_svc.handle_turn(
        patient_id="pt_continuous",
        cycle_id=cycle_id,
        request=AthenaChatRequest(event_type="user_message", message="I visited the doctor yesterday."),
    )

    # Post-visit: patient reports new symptom / worsening -> normal clinical observation continues!
    now_box[0] = t_ref + timedelta(hours=8)
    post_visit_resp = await athena_svc.handle_turn(
        patient_id="pt_continuous",
        cycle_id=cycle_id,
        request=AthenaChatRequest(
            event_type="out_of_turn_message",
            message="My afternoon palpitations came back and I have a new headache today.",
        ),
    )
    sys_prompt_post_visit = harness.chat_turn_calls[-1]["system_instruction"]
    assert "=== REFERRAL LIFECYCLE CONTEXT (VISIT SELF-REPORTED AS COMPLETED) ===" in sys_prompt_post_visit
    assert "NOT independent medical verification and does NOT mean the underlying health concern is resolved" in sys_prompt_post_visit
    assert "logged this change" in post_visit_resp.reply.lower()

    # Daily Journal Closure continues uninterrupted on Week 2 Day 1
    now_box[0] = t_ref + timedelta(hours=25)
    journal_svc = AthenaDailyJournalService(db=db, gateway=gateway, now_provider=lambda: now_box[0])
    journal = await journal_svc.close_daily_journal(
        cycle_id=cycle_id,
        calendar_week=2,
        calendar_day=1,
    )
    assert journal is not None
    saved_journals = ModelReportRepository(db).list_server_only(
        cycle_id=cycle_id, report_type=f"{ATHENA_DAILY_JOURNAL_REPORT_TYPE}_w2_d1"
    )
    assert len(saved_journals) >= 1


@pytest.mark.anyio
async def test_11_emergency_guards_and_mode2_vitals_unaffected(test_db):
    """
    Scenario 11: Emergency symptom/vitals guards (including deterministic fail-safe
    when Gemini is offline) and `Mode2VitalsMonitoringService` personalized Pro
    thresholds work identically during an active referral and after `visit_confirmed`.
    """
    db, _ = test_db
    t0 = datetime(2026, 10, 18, 10, 0, 0, tzinfo=timezone.utc)
    now_box = [t0]
    harness = Stage8A4MockHarness(referral_recommended=True)
    gateway = harness.build_gateway()

    ctx = await _setup_cycle_with_weekly_referral(
        db, gateway, patient_id="pt_emergency", t0=t0, generate_ready_document=True, now_holder=now_box
    )
    cycle_id = ctx["cycle_id"]
    t_ref = ctx["t_ref"]

    athena_svc = AthenaChatService(db=db, gateway=gateway, now_provider=lambda: now_box[0])

    # 1. Acute chest pain with Gemini offline -> deterministic fail-safe triggers emergency!
    harness.fail_chat_turn = True
    now_box[0] = t_ref + timedelta(hours=1)
    em_resp = await athena_svc.handle_turn(
        patient_id="pt_emergency",
        cycle_id=cycle_id,
        request=AthenaChatRequest(
            event_type="user_message",
            message="I have severe chest pain right now!",
        ),
    )
    assert em_resp.emergency_trigger is True
    assert em_resp.emergency_notice is not None
    harness.fail_chat_turn = False

    # 2. Critical vitals alert (158 bpm) after visit_confirmed -> triggers emergency
    now_box[0] = t_ref + timedelta(hours=2)
    await athena_svc.handle_turn(
        patient_id="pt_emergency",
        cycle_id=cycle_id,
        request=AthenaChatRequest(event_type="user_message", message="I visited the doctor yesterday."),
    )

    now_box[0] = t_ref + timedelta(hours=3)
    vitals_resp = await athena_svc.handle_turn(
        patient_id="pt_emergency",
        cycle_id=cycle_id,
        request=AthenaChatRequest(
            event_type="vitals_alert",
            vitals_alert=AthenaVitalsAlertContext(current_pulse=158, baseline_pulse=68),
        ),
    )
    assert vitals_resp.emergency_trigger is True

    # 3. Mode2VitalsMonitoringService telemetry ingestion remains active
    mode2_svc = Mode2VitalsMonitoringService(db=db, gateway=gateway, now_provider=lambda: now_box[0])
    ep_res = await mode2_svc.process_measurement(
        cycle_id=cycle_id,
        pulse=148,
        spo2=97,
        measured_at=now_box[0] + timedelta(minutes=1),
    )
    assert ep_res is not None
    assert ep_res.trigger_pulse == 148


@pytest.mark.anyio
async def test_12_closed_cycle_new_cycle_isolation_and_worker_crash_restart_recovery(test_db):
    """
    Scenario 12:
    - Closing/completing a cycle stops reminder eligibility and causes any queued
      reminder job to skip with ZERO Gemini calls.
    - Opening a new active cycle for the same patient does NOT inherit the closed
      cycle's referral or schedule reminders.
    - A crashed worker whose `RUNNING` lease on `REFERRAL_FOLLOWUP_REMINDER` expired
      is cleanly recovered after server restart and sends the reminder once.
    """
    db, _ = test_db
    t0 = datetime(2026, 10, 18, 10, 0, 0, tzinfo=timezone.utc)
    now_box = [t0]
    harness = Stage8A4MockHarness(referral_recommended=True)
    gateway = harness.build_gateway()

    # Part A: Worker crash / expired lease recovery on active cycle
    ctx_crash = await _setup_cycle_with_weekly_referral(
        db, gateway, patient_id="pt_crash_recover", t0=t0, generate_ready_document=True, now_holder=now_box
    )
    cycle_crash = ctx_crash["cycle_id"]
    athena_svc = AthenaChatService(db=db, gateway=gateway, now_provider=lambda: now_box[0])
    await athena_svc.send_weekly_wrapup_message(cycle_id=cycle_crash, sprint_number=1)

    now_box[0] = ctx_crash["t_ref"] + timedelta(hours=168)
    worker_1 = ClinicalAutomationService(
        db=db,
        gateway=gateway,
        now_provider=lambda: now_box[0],
        enable_daily_journal=False, enable_wellness_daily=False,
        lease_ttl_seconds=60,
    )
    jobs = [
        j
        for j in worker_1.discover_and_enqueue_jobs(discover_daily_journals=False)
        if j.job_type == AutomationJobType.REFERRAL_FOLLOWUP_REMINDER
    ]
    assert len(jobs) == 1
    claimed_before_crash = worker_1.claim_job(jobs[0].job_id)
    assert claimed_before_crash.status == AutomationJobStatus.RUNNING

    # Simulate worker_1 crash & server restart after lease TTL expires (+120s)
    now_box[0] = ctx_crash["t_ref"] + timedelta(hours=168, seconds=120)
    worker_2 = ClinicalAutomationService(
        db=db,
        gateway=gateway,
        now_provider=lambda: now_box[0],
        enable_daily_journal=False, enable_wellness_daily=False,
        lease_ttl_seconds=60,
    )
    reclaimed = worker_2.claim_job(jobs[0].job_id)
    assert reclaimed is not None
    recovered_done = await worker_2.execute_claimed_job(reclaimed)
    assert recovered_done.status == AutomationJobStatus.COMPLETED
    assert recovered_done.result_metadata["skipped"] is False
    assert len(harness.reminder_prompts) == 1

    # Part B: Closed cycle & new cycle isolation
    now_box[0] = t0
    ctx_closed = await _setup_cycle_with_weekly_referral(
        db, gateway, patient_id="pt_cycle_boundary", t0=t0, generate_ready_document=True, now_holder=now_box
    )
    old_cycle_id = ctx_closed["cycle_id"]
    old_ref_id = ctx_closed["referral_state"]["referral_id"]

    # Queue a reminder before closing the cycle
    now_box[0] = ctx_closed["t_ref"] + timedelta(hours=168)
    queued_old = worker_2.enqueue_referral_followup_reminder(
        cycle_id=old_cycle_id,
        patient_id="pt_cycle_boundary",
        referral_id=old_ref_id,
        target_interval=1,
    )
    assert queued_old is not None

    # Close old cycle and open a brand-new cycle (future Neo boundary)
    cycle_repo = MedicalCycleRepository(db)
    old_row = cycle_repo.get(old_cycle_id)
    old_row.status = "completed"
    db.commit()

    new_cycle, created_new = cycle_repo.get_or_create_active_for_patient("pt_cycle_boundary")
    assert created_new is True
    assert new_cycle.cycle_id != old_cycle_id

    rem_prompts_before = len(harness.reminder_prompts)
    claimed_old = worker_2.claim_job(queued_old.job_id)
    skipped_old = await worker_2.execute_claimed_job(claimed_old)
    assert skipped_old.status == AutomationJobStatus.COMPLETED
    assert skipped_old.result_metadata["skipped"] is True
    assert skipped_old.result_metadata["reason"] == "cycle_not_active"
    assert len(harness.reminder_prompts) == rem_prompts_before

    # New cycle has NO inherited referral or reminder schedule
    weekly_svc = WeeklyConsiliumService(db=db, gateway=gateway, now_provider=lambda: now_box[0])
    new_ref_state = weekly_svc.get_referral_state_for_athena(new_cycle.cycle_id)
    assert new_ref_state["has_active_referral"] is False
    assert new_ref_state["lifecycle_status"] == "none"
    new_sched = weekly_svc.get_referral_followup_reminder_schedule(new_cycle.cycle_id, as_of=now_box[0])
    assert new_sched["eligible"] is False


@pytest.mark.anyio
async def test_13_full_15_step_e2e_referral_lifecycle_and_api_contracts(test_db):
    """
    Scenario 13 (Stage 8A Final Integration Review — 15-Step End-to-End Verification):
    1. Weekly Pro recommends an in-person doctor examination.
    2. Referral state is persisted in `model_reports` (`referral_lifecycle_state`).
    3. Flash -> Pro Referral Document cascade runs and saves `SERVER_ONLY` and `CLIENT_FACING` reports in `model_reports`.
    4. Athena informs the user in the Weekly Summary (`send_weekly_wrapup_message`) in calm plain English.
    5. Flutter fetches `GET /api/v1/client/cascade/status` -> `has_active_referral=True`, `status='document_ready'`, `document_generation_state='ready'`.
    6. Safe fetches `GET /api/v1/client/referral/documents` -> lists the document with `cycle_status='active'`.
    7. User opens `GET /api/v1/client/referral/documents/{report_id}` -> receives exact 7 clinical sections without `SERVER_ONLY` fields.
    8. User exports PDF (`?format=pdf`) and DOCX (`?format=docx`) -> valid `%PDF` and DOCX bytes returned with 0 LLM calls.
    9. After 168 hours (`t_ref + 168h`), Athena sends a single follow-up reminder (`reminder_count == 1`), with 0 duplicate messages.
    10. User replies contextually via `POST /api/v1/client/chat/athena` with `"I already did"` after Athena's reminder question.
    11. Referral transitions to `visit_confirmed` in a single Gemini Flash call.
    12. Subsequent 168-hour intervals (`t_ref + 336h`) send 0 reminders.
    13. Flutter fetches `GET /api/v1/client/cascade/status` -> `has_active_referral=False`, `status='visit_confirmed'` (active banner disappears).
    14. Safe fetches `GET /api/v1/client/referral/documents` and `/{report_id}` -> document remains in Safe history and exportable.
    15. Wellness, Mode 2, Athena Daily Journal, and Weekly Consilium continue working normally.
    """
    from fastapi.testclient import TestClient
    from app.database import get_db
    from app.main import app
    from app.routers.client_chat import get_gemini_gateway, get_server_now_provider
    from app.security.auth import DeviceSession, require_device_session, verify_client_token

    db, _ = test_db
    t0 = datetime(2026, 10, 18, 10, 0, 0, tzinfo=timezone.utc)
    now_box = [t0]
    harness = Stage8A4MockHarness(referral_recommended=True)
    gateway = harness.build_gateway()
    patient_id = "pt_stage8a_e2e_15steps"

    # Steps 1, 2, 3: Weekly Pro recommends referral -> Referral saved -> Flash->Pro document generated in `model_reports`
    ctx = await _setup_cycle_with_weekly_referral(
        db, gateway, patient_id=patient_id, t0=t0, generate_ready_document=True, now_holder=now_box
    )
    cycle_id = ctx["cycle_id"]
    t_ref = ctx["t_ref"]
    doc_id = ctx["referral_state"]["document_report_id"]
    assert doc_id is not None

    # Step 4: Athena Weekly Summary informs the user
    athena_svc = AthenaChatService(db=db, gateway=gateway, now_provider=lambda: now_box[0])
    wrapup_resp = await athena_svc.send_weekly_wrapup_message(cycle_id=cycle_id, sprint_number=1)
    assert "cardiologist" in wrapup_resp.reply.lower()
    assert "Safe" in wrapup_resp.reply

    # Mount FastAPI TestClient with authenticated DeviceSession
    session = DeviceSession(
        patient_id=patient_id,
        device_id=f"dev_{patient_id}",
        jti=f"jti_{patient_id}",
        family_id=f"fam_{patient_id}",
        scope="cycle:read reports:read cascade:run chat:read chat:write",
        scopes=frozenset(["cycle:read", "reports:read", "cascade:run", "chat:read", "chat:write"]),
        issued_at=t_ref,
        expires_at=t_ref + timedelta(days=30),
    )

    def _override_db():
        yield db

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[require_device_session] = lambda: session
    app.dependency_overrides[verify_client_token] = lambda: session
    app.dependency_overrides[get_gemini_gateway] = lambda: gateway
    app.dependency_overrides[get_server_now_provider] = lambda: (lambda: now_box[0])

    try:
        with TestClient(app) as client:
            # Step 5: Flutter receives updated status (`GET /api/v1/client/cascade/status`)
            status_resp = client.get("/api/v1/client/cascade/status")
            assert status_resp.status_code == 200
            status_json = status_resp.json()
            assert status_json["has_active_referral"] is True
            ref_status = status_json["referral_status"]
            assert ref_status["has_active_referral"] is True
            assert ref_status["status"] == "document_ready"
            assert ref_status["document_generation_state"] == "ready"
            assert ref_status["document_report_id"] == doc_id
            assert set(ref_status["target_specialties"]) == {"cardiologist", "endocrinologist"}

            # Step 6: Document appears in Safe (`GET /api/v1/client/referral/documents`)
            list_resp = client.get("/api/v1/client/referral/documents")
            assert list_resp.status_code == 200
            list_json = list_resp.json()
            assert list_json["total_count"] == 1
            doc_item = list_json["documents"][0]
            assert doc_item["report_id"] == doc_id
            assert doc_item["cycle_status"] == "active"
            assert doc_item["available_formats"] == ["pdf", "docx", "md"]

            # Step 7: User opens document (`GET /api/v1/client/referral/documents/{report_id}`)
            detail_resp = client.get(f"/api/v1/client/referral/documents/{doc_id}")
            assert detail_resp.status_code == 200
            detail_json = detail_resp.json()
            assert detail_json["report_id"] == doc_id
            assert detail_json["title"] == REFERRAL_DOCUMENT_CANONICAL_TITLE
            assert len(detail_json["sections"]) == 7
            expected_titles = [
                "Patient and Observation Period",
                "Reason for Recommending an In-Person Consultation",
                "Objective Health Measurements and Examination Results",
                "Timeline of Reported Symptoms and Observed Changes",
                "Important Patterns and Unresolved Concerns",
                "AI-Generated Observations Requiring Professional Verification",
                "Questions and Possible Examinations to Discuss with a Licensed Clinician",
            ]
            assert [s["section_title"] for s in detail_json["sections"]] == expected_titles
            for forbidden_key in ("pro_doctor_document", "flash_extraction", "raw_document_report_id"):
                assert forbidden_key not in detail_json

            # Step 8: User exports PDF and DOCX (`GET .../export?format=pdf|docx`) with 0 LLM calls
            llm_calls_before_export = len(harness.calls)
            pdf_resp = client.get(f"/api/v1/client/referral/documents/{doc_id}/export?format=pdf")
            assert pdf_resp.status_code == 200
            assert pdf_resp.content.startswith(b"%PDF")
            docx_resp = client.get(f"/api/v1/client/referral/documents/{doc_id}/export?format=docx")
            assert docx_resp.status_code == 200
            assert docx_resp.content.startswith(b"PK")
            assert len(harness.calls) == llm_calls_before_export

            # Step 9: After 168 hours, Athena sends a single follow-up reminder
            now_box[0] = t_ref + timedelta(hours=168)
            auto_svc = ClinicalAutomationService(
                db=db,
                gateway=gateway,
                now_provider=lambda: now_box[0],
                enable_daily_journal=False,
                enable_wellness_daily=False,
            )
            rem_jobs = [
                j
                for j in auto_svc.discover_and_enqueue_jobs(discover_daily_journals=False)
                if j.job_type == AutomationJobType.REFERRAL_FOLLOWUP_REMINDER and j.cycle_id == cycle_id
            ]
            assert len(rem_jobs) == 1
            claimed_rem = auto_svc.claim_job(rem_jobs[0].job_id)
            done_rem = await auto_svc.execute_claimed_job(claimed_rem)
            assert done_rem.status == AutomationJobStatus.COMPLETED
            assert done_rem.result_metadata["skipped"] is False
            assert len(harness.reminder_prompts) == 1

            # Step 10 & 11: User replies contextually ("I already did") via POST /api/v1/client/chat/athena -> visit_confirmed
            now_box[0] = t_ref + timedelta(hours=169)
            calls_before_turn = len(harness.calls)
            chat_resp = client.post(
                "/api/v1/client/chat/athena",
                json={"event_type": "user_message", "message": "I already did"},
            )
            assert chat_resp.status_code == 200
            assert len(harness.calls) - calls_before_turn == 1

            weekly_svc = WeeklyConsiliumService(db=db, gateway=gateway, now_provider=lambda: now_box[0])
            ref_after = weekly_svc.get_referral_lifecycle_state(cycle_id)
            assert ref_after["status"] == "visit_confirmed"

            # Step 12: Subsequent 168h intervals send 0 reminders
            now_box[0] = t_ref + timedelta(hours=336)
            later_rem_jobs = [
                j
                for j in auto_svc.discover_and_enqueue_jobs(discover_daily_journals=False)
                if j.job_type == AutomationJobType.REFERRAL_FOLLOWUP_REMINDER and j.cycle_id == cycle_id
            ]
            assert later_rem_jobs == []

            # Step 13: Flutter cascade status shows `has_active_referral=False` and `status='visit_confirmed'`
            status_after_resp = client.get("/api/v1/client/cascade/status")
            assert status_after_resp.status_code == 200
            status_after_json = status_after_resp.json()
            assert status_after_json["has_active_referral"] is False
            assert status_after_json["referral_status"]["has_active_referral"] is False
            assert status_after_json["referral_status"]["status"] == "visit_confirmed"
            assert status_after_json["referral_status"]["document_report_id"] == doc_id

            # Step 14: Document remains in Safe history and exportable
            list_after = client.get("/api/v1/client/referral/documents").json()
            assert list_after["total_count"] == 1
            assert list_after["documents"][0]["report_id"] == doc_id
            detail_after = client.get(f"/api/v1/client/referral/documents/{doc_id}")
            assert detail_after.status_code == 200
            pdf_after = client.get(f"/api/v1/client/referral/documents/{doc_id}/export?format=pdf")
            assert pdf_after.status_code == 200

            # Step 15: Mode 2, Athena Daily Journal, and Weekly Consilium continue working normally
            mode2_svc = Mode2VitalsMonitoringService(db=db, gateway=gateway, now_provider=lambda: now_box[0])
            ep = await mode2_svc.process_measurement(
                cycle_id=cycle_id,
                pulse=146,
                spo2=98,
                measured_at=now_box[0] + timedelta(minutes=5),
            )
            assert ep is not None

            journal_svc = AthenaDailyJournalService(db=db, gateway=gateway, now_provider=lambda: now_box[0])
            closed_j = await journal_svc.close_daily_journal(
                cycle_id=cycle_id,
                calendar_week=2,
                calendar_day=2,
            )
            assert closed_j is not None

            _seed_seven_day_athena_logs(db, cycle_id=cycle_id, sprint_id=f"sprint_{cycle_id}_w2"[:64])
            w2_res = await weekly_svc.run_weekly_consilium(
                cycle_id=cycle_id,
                sprint_id=f"sprint_{cycle_id}_w2"[:64],
                report_date=now_box[0].strftime("%Y-%m-%d"),
            )
            assert w2_res is not None
    finally:
        app.dependency_overrides.clear()

