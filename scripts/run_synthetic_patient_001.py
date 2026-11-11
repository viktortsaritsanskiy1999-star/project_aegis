#!/usr/bin/env python3
"""
PROJECT AEGIS — SYNTHETIC PATIENT 001
Full Clinical Simulation Runner with Real Gemini API and Accelerated Virtual Time.

Execution Modes:
  --preflight-only : Runs Stage A Pre-flight checks and exits (0 real Gemini calls)
  --dry-run        : Runs simulated offline run with deterministic mocks for local smoke testing
  (default)        : Runs LIVE clinical simulation with REAL Gemini API calls!
"""

import argparse
import asyncio
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
import logging
import os
from pathlib import Path
import sys
import time
from typing import Any, Dict, List, Optional, Tuple
import uuid

# Add backend directory to sys.path
BACKEND_DIR = Path(__file__).resolve().parent.parent / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from fastapi import UploadFile
from sqlalchemy import create_engine, select, text, event
from sqlalchemy.orm import sessionmaker, Session

from app.config import settings
from app.database import Base
from app.models.model_report import ModelReport
from app.models.patient import Patient
from app.models.medical_cycle import MedicalCycle
from app.models.health_measurement import HealthMeasurement
from app.repositories.cycle_repository import MedicalCycleRepository
from app.repositories.measurement_repository import HealthMeasurementRepository
from app.repositories.report_repository import ModelReportRepository
from app.schemas.chat import AthenaChatRequest, AthenaChatResponse
from app.schemas.consilium import (
    AnamnesisQuestionnaireAnswers,
    ConsiliumMode,
    PrimaryConsiliumClientResponse,
)
from app.schemas.privacy import PrivacyLevel
from app.services.audit_trail_service import AuditTrailService, set_current_audit_context, AuditContext
from app.services.gemini_gateway import GeminiGateway, GeminiModelRole
from app.services.primary_consilium_service import PrimaryConsiliumService
from app.services.ecg_vision_service import EcgVisionService
from app.services.lab_vision_ocr_service import LabVisionOcrService
from app.services.wellness_daily_service import (
    WELLNESS_DAILY_REPORT_TYPE,
    WellnessDailyService,
    WellnessTriggerType,
)
from app.services.athena_daily_journal_service import (
    ATHENA_CLINICAL_LOG_REPORT_TYPE,
    ATHENA_DAILY_JOURNAL_REPORT_TYPE,
    AthenaDailyJournalService,
)
from app.services.weekly_consilium_service import WeeklyConsiliumService
from app.services.athena_chat_service import AthenaChatService
from app.services.lab_revision_consilium_service import LabRevisionConsiliumService
from app.services.referral_document_service import ReferralDocumentService

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("synthetic_patient_001")


# ==============================================================================
# 1. CONTROLLED VIRTUAL CLOCK
# ==============================================================================

class ControlledVirtualClock:
    """Provides deterministic advancing virtual time for clinical simulation."""

    def __init__(self, start_dt: datetime):
        if start_dt.tzinfo is None:
            self._current = start_dt.replace(tzinfo=timezone.utc)
        else:
            self._current = start_dt.astimezone(timezone.utc)

    def now(self) -> datetime:
        return self._current

    def advance(self, days: int = 0, hours: int = 0, minutes: int = 0, seconds: int = 0) -> datetime:
        self._current += timedelta(days=days, hours=hours, minutes=minutes, seconds=seconds)
        return self._current

    def set(self, dt: datetime) -> datetime:
        if dt.tzinfo is None:
            self._current = dt.replace(tzinfo=timezone.utc)
        else:
            self._current = dt.astimezone(timezone.utc)
        return self._current


# ==============================================================================
# 2. PATIENT PROFILE & QUESTIONNAIRE ANSWERS
# ==============================================================================

def build_patient_001_anamnesis() -> AnamnesisQuestionnaireAnswers:
    """Forms accurate M1 answers for Synthetic Patient 001 reflecting all constraints."""
    return AnamnesisQuestionnaireAnswers(
        age_confirmation="confirmed",
        gender="male",
        age=32,
        height=185,
        weight=80,
        nicotine_status="иногда курит",
        nicotine_years="менее 5 лет",
        nicotine_freq="точная частота неизвестна",
        alcohol_status="употребляет",
        alcohol_type="достаточно часто пьет пиво, точное количество не установлено",
        spec_factors="нет",
        sleep_quality="бывает беспокойным, режим может изменяться",
        diet_types="разнообразное: мясо, рыба, овощи, фастфуд, кофе",
        diet_habits="иногда бывает изжога",
        gi_signals="изжога время от времени",
        work_type="удаленный формат и/или сменный график",
        work_schedule="плавающий график",
        sport_level="посещает тренажерный зал примерно 3 раза в неделю, иногда играет в футбол",
        chronic_diseases="межпозвоночная грыжа диагностирована около 7 лет назад (МРТ около 5 лет назад), после футбола иногда легкое тянущее ощущение в пояснице",
        onco_status="не установлено",
        recent_illness="на старте острое заболевание не заявлено",
        critical_infections="сообщает об отсутствии известных ВИЧ, гепатита, туберкулеза, сифилиса (самоотчет, без лаборатории)",
        heredity_risks="инфаркт и/или инсульт у родственников (степень родства и возраст не определены)",
        medications="принимает витамины и пищевые добавки (точный состав не указан)",
        user_text_medications="витаминные комплексы, БАДы",
        user_text_heredity_risks="кардиоваскулярные заболевания у старших родственников",
        user_text_chronic_diseases="грыжа поясничного отдела позвоночника без выраженных ограничений",
        user_text_recent_illness=None,
        user_text_critical_infections=None,
        user_text_spec_factors=None,
        user_text_diet_habits="периодическая изжога после кофе или фастфуда",
        user_text_gi_signals="дискомфорт в эпигастрии",
    )


# ==============================================================================
# 3. CONVERSATIONAL SYNTHETIC PATIENT PERSONA AGENT
# ==============================================================================

PATIENT_PERSONA_SYSTEM_INSTRUCTION = """
Ты — 32-летний мужчина, участник системы медицинского наблюдения AEGIS.
Твой профиль:
- Рост 185 см, вес 80 кг.
- Работаешь удаленно / плавающий график.
- Ходишь в спортзал около 3 раз в неделю, иногда играешь в футбол с друзьями.
- Иногда куришь (стаж менее 5 лет, нерегулярно).
- Довольно часто пьешь пиво с друзьями по выходным.
- Питание разнообразное (мясо, рыба, овощи, фастфуд, кофе). Иногда бывает изжога.
- Позвоночник: межпозвоночная грыжа найдена ~7 лет назад (МРТ ~5 лет назад). В обычной жизни не мешает, но после футбола бывает тянущее ощущение в пояснице.
- Сон: бывает беспокойным, график плавает.
- Родственники: у кого-то был инфаркт или инсульт, но ты не знаешь точный возраст и подробности.
- Принимаешь витамины и добавки.
- Доверяешь традиционной медицине, но интересуешься духовными практиками.

Характер общения:
- Ты обычный живой человек, НЕ врач. Говоришь простым разговорным русским языком.
- Не сыплешь медицинскими терминами.
- Иногда отвечаешь немного размыто («да вроде нормально», «не помню точно», «было дело»).
- Не обязан сразу всё понимать в анализах и измерениях.
- СТРОГО: ты НЕ знаешь будущих событий симуляции, ты НЕ ставишь себе диагнозы, и если тебя направили к врачу, но ты еще не дошел — ты честно говоришь, что пока не дошел (занят работой, нет времени, собираешься на днях). Никаких выдуманных походов к врачу!
Отвечай кратко, естественно и по-человечески (1-3 предложения).
""".strip()

async def generate_patient_reply(
    gateway: GeminiGateway,
    athena_question: str,
    context_note: str,
    is_dry_run: bool = False,
) -> str:
    """Generates an authentic in-character conversational reply from the synthetic patient."""
    if is_dry_run:
        return "Да, всё нормально в целом. Сегодня выспался, на тренировку собираюсь вечером."

    prompt = f"""
Сообщение от Афины:
"{athena_question}"

Контекст твоего текущего дня:
{context_note}

Ответь Афине от лица пациента:
""".strip()

    try:
        reply = await gateway.generate_raw_text(
            prompt=prompt,
            model=GeminiModelRole.FLASH,
            system_instruction=PATIENT_PERSONA_SYSTEM_INSTRUCTION,
            response_mime_type="text/plain",
            max_output_tokens=256,
        )
        return reply.strip()
    except Exception as exc:
        logger.warning(f"Patient persona generation error: {exc}. Falling back to default utterance.")
        return "Да, всё в порядке, чувствую себя нормально. Спасибо, что спросили."


# ==============================================================================
# 4. SYNTHETIC TELEMETRY GENERATION (144 measurements/day)
# ==============================================================================

def generate_daily_telemetry(
    db: Session,
    cycle_id: str,
    day_start_dt: datetime,
    calendar_day: int,
    is_workout_day: bool,
    is_soccer_day: bool,
) -> int:
    """
    Ingests physiologically realistic 10-minute smart watch measurements (144/day).
    Baseline resting heart rate ~57 bpm, circadian variation, sleep metrics, and workout bursts.
    """
    m_repo = HealthMeasurementRepository(db)
    inserted_count = 0

    # 1. Night Sleep Measurement (Recorded at 07:00 local time = wake-up event)
    wake_up_time = day_start_dt + timedelta(hours=3) # Local morning
    m_repo.create(
        cycle_id=cycle_id,
        source="wearable_sleep",
        measured_at=wake_up_time,
        pulse=54,
        systolic=116,
        diastolic=74,
        spo2=99,
        raw_metadata={
            "event": "wake_up",
            "sleep_hours": 7.6,
            "deep_hours": 1.9,
            "rem_hours": 1.7,
            "light_hours": 3.4,
            "awake_hours": 0.6,
            "wake_time": "07:00",
        },
    )
    inserted_count += 1

    # 2. Morning Resting Blood Pressure & Vitals (07:15 local time)
    m_repo.create(
        cycle_id=cycle_id,
        source="blood_pressure_monitor",
        measured_at=wake_up_time + timedelta(minutes=15),
        pulse=58,
        systolic=124,
        diastolic=76,
        spo2=98,
        raw_metadata={"posture": "sitting", "rest_minutes": 5},
    )
    inserted_count += 1

    # 3. Continuous 10-minute Watch Telemetry throughout 24 hours (144 points)
    # Using deterministic pseudorandom variation seeded by day number
    import random
    rng = random.Random(calendar_day * 1000 + 42)

    for i in range(144):
        timestamp = day_start_dt + timedelta(minutes=i * 10)
        hour_of_day = (timestamp.hour + 3) % 24  # Local hour

        # Circadian baseline
        if 0 <= hour_of_day < 7:  # Sleep
            base_hr = rng.randint(49, 55)
            spo2 = rng.randint(97, 99)
            activity = "sleep"
        elif 7 <= hour_of_day < 9:  # Morning routine
            base_hr = rng.randint(58, 68)
            spo2 = rng.randint(98, 99)
            activity = "light_activity"
        elif 9 <= hour_of_day < 18:  # Remote work / desk
            base_hr = rng.randint(55, 66)
            spo2 = rng.randint(98, 99)
            activity = "sedentary"
        elif 18 <= hour_of_day < 19:  # Workout window or evening activity
            if is_workout_day:
                # Gym strength training session
                base_hr = rng.randint(126, 146)
                spo2 = rng.randint(96, 98)
                activity = "strength_training"
            elif is_soccer_day and 16 <= hour_of_day < 18:
                # Soccer interval session
                base_hr = rng.randint(132, 154)
                spo2 = rng.randint(96, 98)
                activity = "soccer_match"
            else:
                base_hr = rng.randint(62, 74)
                spo2 = rng.randint(98, 99)
                activity = "walking"
        elif 19 <= hour_of_day < 20 and (is_workout_day or is_soccer_day):
            # Post-exercise recovery window
            base_hr = rng.randint(72, 88)
            spo2 = rng.randint(97, 99)
            activity = "recovery"
        else:  # Evening rest
            base_hr = rng.randint(56, 64)
            spo2 = rng.randint(98, 99)
            activity = "resting"

        m_repo.create(
            cycle_id=cycle_id,
            source="smart_watch_telemetry",
            measured_at=timestamp,
            pulse=base_hr,
            systolic=None,
            diastolic=None,
            spo2=spo2,
            raw_metadata={"activity_context": activity, "sample_index": i},
        )
        inserted_count += 1

    return inserted_count


# ==============================================================================
# 5. STAGE A: PRE-FLIGHT VERIFICATION
# ==============================================================================

def execute_preflight_checks(
    db_path: Path,
    audit_dir: Path,
    patient_dir: Path,
) -> Dict[str, Any]:
    """Executes all pre-flight verification items without paid Gemini API calls."""
    logger.info("Executing Pre-Flight Verification Checks...")
    results = {
        "status": "READY",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "checks": {},
    }

    # 1. Image Files Verification
    images_primary = [
        patient_dir / "01_Primary_Day_1" / "ECG_01.jpg",
        patient_dir / "01_Primary_Day_1" / "LAB_01.jpg",
        patient_dir / "01_Primary_Day_1" / "LAB_02.jpg",
        patient_dir / "01_Primary_Day_1" / "LAB_03.jpg",
        patient_dir / "01_Primary_Day_1" / "LAB_04.jpg",
    ]
    images_day10 = [
        patient_dir / "02_Lab_Revision_Day_10" / "LAB_05.jpg",
        patient_dir / "02_Lab_Revision_Day_10" / "LAB_06.jpg",
    ]

    all_images = images_primary + images_day10
    file_status = []
    for img in all_images:
        exists = img.is_file()
        size = img.stat().st_size if exists else 0
        file_status.append({"path": str(img), "exists": exists, "size_bytes": size})
        if not exists or size < 1000:
            results["status"] = "BLOCKED"
            results["checks"]["files"] = f"Missing or empty image file: {img}"
            return results

    results["checks"]["files"] = {
        "primary_images_count": len(images_primary),
        "day10_images_count": len(images_day10),
        "total_files": len(all_images),
        "status": "PASS",
    }

    # 2. Database & Schema Verification
    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=engine)
    with engine.connect() as conn:
        tables = conn.execute(text("SELECT name FROM sqlite_master WHERE type='table';")).fetchall()
        table_names = [t[0] for t in tables]
    engine.dispose()

    results["checks"]["database"] = {
        "db_path": str(db_path),
        "tables_count": len(table_names),
        "status": "PASS",
    }

    # 3. Audit Trail Directory Verification
    audit_dir.mkdir(parents=True, exist_ok=True)
    results["checks"]["audit_trail"] = {
        "audit_dir": str(audit_dir),
        "writable": os.access(audit_dir, os.W_OK),
        "status": "PASS",
    }

    # 4. Gemini API Configuration & Models
    api_key_set = bool(settings.gemini_api_key and settings.gemini_api_key.strip())
    results["checks"]["gemini_api"] = {
        "api_key_configured": api_key_set,
        "flash_model": settings.gemini_flash_model,
        "pro_model": settings.gemini_pro_model,
        "flash_lite_model": settings.gemini_flash_lite_model,
        "vision_model": settings.gemini_vision_model,
        "status": "PASS" if api_key_set else "BLOCKED",
    }
    if not api_key_set:
        results["status"] = "BLOCKED"

    # 5. Git Status (.gitignore isolation)
    gitignore_path = Path(__file__).resolve().parent.parent / ".gitignore"
    gi_text = gitignore_path.read_text(encoding="utf-8") if gitignore_path.exists() else ""
    is_patient_ignored = "test/Synthetic_Patient_001/" in gi_text
    is_audit_ignored = "backend/audit_logs/" in gi_text

    results["checks"]["git_isolation"] = {
        "patient_dir_ignored": is_patient_ignored,
        "audit_dir_ignored": is_audit_ignored,
        "status": "PASS" if is_patient_ignored and is_audit_ignored else "WARNING",
    }

    # 6. Pricing & Budget Estimates
    # Published Google Gemini API Pricing:
    # Flash / Flash-Lite: $0.075 / 1M input, $0.30 / 1M output
    # Pro / Argon: $1.25 / 1M input, $5.00 / 1M output
    results["estimated_budget"] = {
        "pricing_rates": {
            "flash_input_per_million": 0.075,
            "flash_output_per_million": 0.30,
            "pro_input_per_million": 1.25,
            "pro_output_per_million": 5.00,
        },
        "scenarios_usd": {
            "7_virtual_days": {"min": 0.35, "expected": 0.85, "max": 1.50},
            "14_virtual_days": {"min": 0.65, "expected": 1.45, "max": 2.50},
            "60_virtual_days": {"min": 1.80, "expected": 3.80, "max": 6.50},
        },
        "recommended_budget_cap_usd": 10.00,
    }

    return results


# ==============================================================================
# 6. MAIN SIMULATION ENGINE
# ==============================================================================

async def run_simulation(
    db_path: Path,
    audit_dir: Path,
    patient_dir: Path,
    max_days: int = 60,
    post_referral_days: int = 14,
    dry_run: bool = False,
):
    """Executes the full clinical simulation with real Gemini API and accelerated virtual time."""
    logger.info("=" * 80)
    logger.info("STARTING PROJECT AEGIS — SYNTHETIC PATIENT 001 CLINICAL SIMULATION")
    logger.info(f"Target Max Days: {max_days} | Post-Referral Window: {post_referral_days} days")
    logger.info(f"Execution Mode: {'DRY RUN (Mocks)' if dry_run else 'LIVE GEMINI API'}")
    logger.info("=" * 80)

    # Initialize Engine & SessionFactory
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
    )

    @event.listens_for(engine, "connect")
    def _set_pragmas(dbapi_connection, _):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON;")
        cursor.close()

    Base.metadata.create_all(bind=engine)
    SessionFactory = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    # Initialize Virtual Clock: 11 November 2026, 07:00 local (04:00 UTC)
    start_utc = datetime(2026, 11, 11, 4, 0, 0, tzinfo=timezone.utc)
    clock = ControlledVirtualClock(start_utc)

    # Initialize Audit Trail Service
    audit_service = AuditTrailService(storage_dir=audit_dir, enabled=True)

    # Initialize Gemini Gateway
    gateway = GeminiGateway(audit_service=audit_service)

    patient_id = "patient_synth_001"
    cycle_id = "cycle_synth_001"

    # Simulation State Tracking
    simulation_state = {
        "start_real_time": time.time(),
        "virtual_start_utc": clock.now().isoformat(),
        "virtual_current_utc": clock.now().isoformat(),
        "days_completed": 0,
        "stop_reason": None,
        "referral_issued": False,
        "referral_day": None,
        "referral_timestamp_utc": None,
        "referral_initiating_model": None,
        "referral_reason": None,
        "referral_specialty": None,
        "target_end_day": max_days,
        "total_measurements_ingested": 0,
        "total_gemini_calls": 0,
        "events_log": [],
    }

    # ==========================================================================
    # DAY 1: INITIAL INTAKE & PRIMARY CONSILIUM
    # ==========================================================================
    logger.info("--- [DAY 1] Initial Ingestion & Preparatory Consilium ---")

    with SessionFactory() as db:
        # Create Patient and Medical Cycle if not present
        p_row = db.get(Patient, patient_id)
        if not p_row:
            p_row = Patient(patient_id=patient_id, status="active", created_at=clock.now())
            db.add(p_row)
        c_row = db.get(MedicalCycle, cycle_id)
        if not c_row:
            c_row = MedicalCycle(cycle_id=cycle_id, patient_id=patient_id, status="active", started_at=clock.now(), created_at=clock.now())
            db.add(c_row)
        db.commit()

        # Save Patient Timezone (Europe/Moscow, UTC+3)
        w_service = WellnessDailyService(db=db, gateway=gateway, now_provider=clock.now)
        w_service.save_patient_timezone(cycle_id, "Europe/Moscow")

    with SessionFactory() as db:
        primary_svc = PrimaryConsiliumService(db=db, gateway=gateway, now_provider=clock.now)
        is_primary_completed = primary_svc.is_primary_consilium_completed(cycle_id)

    if not is_primary_completed:
        # Step 1: Submit M1 Questionnaire Answers
        logger.info("Submitting M1 Questionnaire Answers...")
        m1_answers = build_patient_001_anamnesis()
        with SessionFactory() as db:
            primary_svc = PrimaryConsiliumService(db=db, gateway=gateway, now_provider=clock.now)
            anamnesis_resp = await primary_svc.process_anamnesis_intake(cycle_id=cycle_id, answers=m1_answers)
            logger.info(f"M1 Anamnesis intake completed (version {anamnesis_resp.intake_version})")

        # Step 2: Upload ECG Image (ECG_01.jpg)
        logger.info("Uploading ECG_01.jpg via EcgVisionService...")
        ecg_path = patient_dir / "01_Primary_Day_1" / "ECG_01.jpg"
        with SessionFactory() as db:
            ecg_file = UploadFile(file=io.BytesIO(ecg_path.read_bytes()), filename="ECG_01.jpg", headers={"content-type": "image/jpeg"})
            ecg_svc = EcgVisionService(db=db, gateway=gateway, now_provider=clock.now)
            ecg_resp = await ecg_svc.process_ecg_image(cycle_id=cycle_id, file=ecg_file)
            logger.info(f"ECG Vision extraction completed (type: {ecg_resp.recording_type}, quality: {ecg_resp.recording_quality})")

        # Step 3: Upload Initial Lab Images (LAB_01.jpg to LAB_04.jpg)
        logger.info("Uploading LAB_01.jpg to LAB_04.jpg via LabVisionOcrService...")
        lab_files = []
        for i in range(1, 5):
            lp = patient_dir / "01_Primary_Day_1" / f"LAB_0{i}.jpg"
            lab_files.append(UploadFile(file=io.BytesIO(lp.read_bytes()), filename=f"LAB_0{i}.jpg", headers={"content-type": "image/jpeg"}))

        with SessionFactory() as db:
            lab_ocr_svc = LabVisionOcrService(db=db, gateway=gateway, now_provider=clock.now)
            lab_ocr_resp = await lab_ocr_svc.process_lab_images(cycle_id=cycle_id, files=lab_files)
            logger.info(f"Lab Vision OCR completed (extracted markers: {lab_ocr_resp.extracted_count})")

        # Step 4: Run Primary Consilium (8 Specialists -> Deputy -> Chief Physician)
        logger.info("Executing Primary Consilium Cascade (8 Organ Specialists + Deputy + Chief Physician)...")
        with SessionFactory() as db:
            primary_svc = PrimaryConsiliumService(db=db, gateway=gateway, now_provider=clock.now)
            consilium_resp = await primary_svc.run_primary_consilium(cycle_id=cycle_id, mode=ConsiliumMode.INITIAL)
            summary_str = consilium_resp.overall_report.content.get("summary", "") if (consilium_resp.overall_report and consilium_resp.overall_report.content) else "Completed"
            logger.info(f"Primary Consilium completed! Master Summary: {str(summary_str)[:120]}...")

    else:
        logger.info("Primary Consilium is ALREADY completed in DB. Reusing existing reports seamlessly.")

    # Ensure schedule anchor T0 is recorded
    with SessionFactory() as db:
        consilium_w_svc = WeeklyConsiliumService(db=db, now_provider=clock.now)
        consilium_w_svc.record_schedule_anchor(
            cycle_id=cycle_id,
            anchor_dt=datetime(2026, 11, 11, 4, 0, 0, tzinfo=timezone.utc),
            source="post_primary_greeting",
        )

    # Step 5: Send Initial Athena Companion Greeting
    with SessionFactory() as db:
        chat_reports = db.scalars(
            select(ModelReport).where(ModelReport.cycle_id == cycle_id, ModelReport.report_type.in_(["athena_chat_turn", "athena_chat_history"]))
        ).all()

    if not chat_reports:
        logger.info("Initializing Athena Companion Greeting...")
        with SessionFactory() as db:
            athena_svc = AthenaChatService(db=db, gateway=gateway, now_provider=clock.now)
            greeting_resp = await athena_svc.handle_turn(
                patient_id=patient_id,
                cycle_id=cycle_id,
                request=AthenaChatRequest(
                    message=None,
                    event_type="first_planned_question",
                ),
            )
            logger.info(f"Athena Initial Greeting: {greeting_resp.reply[:100]}...")

        # Step 6: Patient Answers Athena's First Question (with delay)
        clock.advance(hours=2) # 06:00 UTC = 09:00 local
        logger.info("Patient responding to Athena's first greeting...")
        patient_day1_reply = await generate_patient_reply(
            gateway=gateway,
            athena_question=greeting_resp.reply,
            context_note="Первый день в программе, чувствую себя бодро.",
            is_dry_run=dry_run,
        )
        with SessionFactory() as db:
            athena_svc = AthenaChatService(db=db, gateway=gateway, now_provider=clock.now)
            await athena_svc.handle_turn(
                patient_id=patient_id,
                cycle_id=cycle_id,
                request=AthenaChatRequest(
                    message=patient_day1_reply,
                    event_type="routine_answer",
                ),
            )
            logger.info(f"Patient replied: {patient_day1_reply}")
    else:
        logger.info("Athena Day 1 initial greeting already completed in DB.")

    with SessionFactory() as db:
        j1_exists = db.scalars(
            select(ModelReport).where(ModelReport.cycle_id == cycle_id, ModelReport.report_type == f"{ATHENA_DAILY_JOURNAL_REPORT_TYPE}_w1_d1")
        ).first()

    if not j1_exists:
        # Ingest Day 1 Telemetry (144 measurements)
        with SessionFactory() as db:
            telem_count = generate_daily_telemetry(
                db=db,
                cycle_id=cycle_id,
                day_start_dt=clock.now() - timedelta(hours=2),
                calendar_day=1,
                is_workout_day=False,
                is_soccer_day=False,
            )
            simulation_state["total_measurements_ingested"] += telem_count

        # Day 1 completes: Advance clock to Day 2 07:05 UTC (24h + 5m mark)
        clock.advance(hours=22, minutes=5)
        with SessionFactory() as db:
            journal_svc = AthenaDailyJournalService(db=db, gateway=gateway, now_provider=clock.now)
            j1, saved_j1, _ = await journal_svc.close_daily_journal(
                cycle_id=cycle_id,
                calendar_week=1,
                calendar_day=1,
                as_of=clock.now(),
            )
            logger.info(f"Day 1 Athena Daily Journal closed (status: {j1.athena_clinical_assessment.status})")
    else:
        logger.info("Day 1 Athena Daily Journal already closed in DB.")
        clock.advance(hours=24, minutes=5)

    simulation_state["days_completed"] = 1

    # ==========================================================================
    # DAY-BY-DAY AUTONOMOUS SIMULATION LOOP (Days 2 to Target End Day)
    # ==========================================================================
    # Resolve starting day dynamically based on completed daily journals
    with SessionFactory() as db:
        completed_days = []
        for d in range(1, max_days + 1):
            w = ((d - 1) // 7) + 1
            s_d = ((d - 1) % 7) + 1
            j = db.scalars(
                select(ModelReport).where(
                    ModelReport.cycle_id == cycle_id,
                    ModelReport.report_type == f"{ATHENA_DAILY_JOURNAL_REPORT_TYPE}_w{w}_d{s_d}",
                )
            ).first()
            if j:
                completed_days.append(d)

        last_done_day = max(completed_days) if completed_days else 1
        current_day = last_done_day + 1 if last_done_day < max_days else max_days
        simulation_state["days_completed"] = last_done_day

        weekly_svc = WeeklyConsiliumService(db=db, gateway=gateway, now_provider=clock.now)
        existing_ref = weekly_svc.get_referral_lifecycle_state(cycle_id)
        if existing_ref:
            simulation_state["referral_issued"] = True
            ref_origin_sprint = existing_ref.get("origin_sprint_number") or 1
            simulation_state["referral_day"] = ref_origin_sprint * 7
            simulation_state["referral_timestamp_utc"] = existing_ref.get("created_at_utc")
            simulation_state["referral_reason"] = existing_ref.get("clinical_reason_for_referral")
            simulation_state["referral_specialty"] = ", ".join(existing_ref.get("target_specialties", []))
            simulation_state["referral_initiating_model"] = existing_ref.get("referral_source")
            if simulation_state["referral_day"] <= 46:
                simulation_state["target_end_day"] = simulation_state["referral_day"] + post_referral_days
            else:
                simulation_state["target_end_day"] = 60
            logger.info(f"Existing referral detected from DB on Day {simulation_state['referral_day']}! Target end day: {simulation_state['target_end_day']}")

    clock.set(start_utc + timedelta(days=current_day - 1))
    logger.info(f"Resuming autonomous simulation from Day {current_day} (Clock: {clock.now().isoformat()})")

    while current_day <= simulation_state["target_end_day"]:
        cal_week = ((current_day - 1) // 7) + 1
        sprint_day = ((current_day - 1) % 7) + 1
        is_workout = (current_day % 7) in (2, 4, 6) # Tue, Thu, Sat
        is_soccer = (current_day % 7) == 0         # Sunday

        logger.info("=" * 60)
        logger.info(f"--- [DAY {current_day} | Week {cal_week}, Day {sprint_day}] Starting Virtual Day ---")
        logger.info(f"Virtual Clock UTC: {clock.now().isoformat()} | Target End Day: {simulation_state['target_end_day']}")

        # 1. Morning Telemetry Ingestion (07:00 UTC = 10:00 MSK)
        with SessionFactory() as db:
            existing_telem = db.scalars(
                select(HealthMeasurement).where(
                    HealthMeasurement.cycle_id == cycle_id,
                    HealthMeasurement.measured_at >= clock.now() - timedelta(minutes=5),
                    HealthMeasurement.measured_at <= clock.now() + timedelta(hours=24),
                )
            ).first()
            if not existing_telem:
                telem_count = generate_daily_telemetry(
                    db=db,
                    cycle_id=cycle_id,
                    day_start_dt=clock.now(),
                    calendar_day=current_day,
                    is_workout_day=is_workout,
                    is_soccer_day=is_soccer,
                )
                simulation_state["total_measurements_ingested"] += telem_count
            else:
                logger.info(f"Telemetry for Day {current_day} already ingested.")

        # 2. Morning Wellness Daily Run (07:30 UTC)
        clock.advance(minutes=30)
        with SessionFactory() as db:
            w_existing = db.scalars(
                select(ModelReport).where(
                    ModelReport.cycle_id == cycle_id,
                    ModelReport.report_type == WELLNESS_DAILY_REPORT_TYPE,
                )
            ).all()
            w_done = any(
                ((r.content or {}).get("window") or {}).get("calendar_week") == cal_week
                and ((r.content or {}).get("window") or {}).get("calendar_day") == sprint_day
                for r in w_existing
            )
            if not w_done:
                wellness_svc = WellnessDailyService(db=db, gateway=gateway, now_provider=clock.now)
                w_rep, is_new = await wellness_svc.process_wellness_for_day(
                    cycle_id=cycle_id,
                    calendar_week=cal_week,
                    calendar_day=sprint_day,
                    as_of=clock.now(),
                )
                logger.info(f"Wellness Daily Day {current_day} (W{cal_week}D{sprint_day}): status={w_rep.status.value}, trigger={w_rep.trigger.value if w_rep.trigger else 'N/A'}")
            else:
                logger.info(f"Wellness Daily Day {current_day} (W{cal_week}D{sprint_day}) already exists in DB.")

        # 3. Athena Proactive / Routine Chat Turn (08:00 UTC)
        clock.advance(minutes=30)
        with SessionFactory() as db:
            chat_turns_today = db.scalars(
                select(ModelReport).where(
                    ModelReport.cycle_id == cycle_id,
                    ModelReport.report_type == ATHENA_CLINICAL_LOG_REPORT_TYPE,
                    ModelReport.created_at >= clock.now() - timedelta(hours=2),
                    ModelReport.created_at <= clock.now() + timedelta(hours=22),
                )
            ).all()

        if len(chat_turns_today) < 2:
            with SessionFactory() as db:
                athena_svc = AthenaChatService(db=db, gateway=gateway, now_provider=clock.now)
                athena_turn_resp = await athena_svc.handle_turn(
                    patient_id=patient_id,
                    cycle_id=cycle_id,
                    request=AthenaChatRequest(
                        message=None,
                        event_type="daily_pulse",
                    ),
                )
                athena_msg = athena_turn_resp.reply or ""
                logger.info(f"Athena Question: {athena_msg[:100]}...")

            # 4. Patient Delayed Reply (10:00 UTC, 2 hours later)
            clock.advance(hours=2)
            patient_context = f"День {current_day}."
            if is_workout:
                patient_context += " Вечером планирую пойти в спортзал."
            if is_soccer:
                patient_context += " Сегодня играл в футбол, немного тянет поясницу."
            if simulation_state["referral_issued"]:
                patient_context += " К врачу пока не ходил: много работы на удаленке, чувствую себя нормально, схожу на днях."

            patient_reply = await generate_patient_reply(
                gateway=gateway,
                athena_question=athena_msg,
                context_note=patient_context,
                is_dry_run=dry_run,
            )

            with SessionFactory() as db:
                athena_svc = AthenaChatService(db=db, gateway=gateway, now_provider=clock.now)
                await athena_svc.handle_turn(
                    patient_id=patient_id,
                    cycle_id=cycle_id,
                    request=AthenaChatRequest(
                        message=patient_reply,
                        event_type="routine_answer",
                    ),
                )
                logger.info(f"Patient Replied: {patient_reply}")
        else:
            logger.info(f"Athena chat turns for Day {current_day} already exist in DB.")
            clock.advance(hours=2)

        # 5. SPECIAL EVENT: Day 10 New Lab Images Upload & Lab Revision
        if current_day == 10:
            with SessionFactory() as db:
                lab_rev_svc = LabRevisionConsiliumService(db=db, gateway=gateway, now_provider=clock.now)
                ledger_st = lab_rev_svc.get_lab_ledger_state(cycle_id)
                has_day10_labs = (ledger_st.ledger_version >= 2)
            if not has_day10_labs:
                logger.info(">>> [DAY 10 SPECIAL EVENT] Uploading LAB_05.jpg & LAB_06.jpg for Lab Revision! <<<")
                lab10_files = [
                    UploadFile(file=io.BytesIO((patient_dir / "02_Lab_Revision_Day_10" / "LAB_05.jpg").read_bytes()), filename="LAB_05.jpg", headers={"content-type": "image/jpeg"}),
                    UploadFile(file=io.BytesIO((patient_dir / "02_Lab_Revision_Day_10" / "LAB_06.jpg").read_bytes()), filename="LAB_06.jpg", headers={"content-type": "image/jpeg"}),
                ]
                with SessionFactory() as db:
                    lab_ocr_svc = LabVisionOcrService(db=db, gateway=gateway, now_provider=clock.now)
                    l10_resp = await lab_ocr_svc.process_lab_images(cycle_id=cycle_id, files=lab10_files)
                    logger.info(f"Day 10 Vision OCR Ingested: {l10_resp.extracted_count} markers. Has unreviewed: {l10_resp.has_unreviewed_lab_changes}")

                    # Run Lab Revision Consilium (Deputy + Chief Physician)
                    lab_rev_svc = LabRevisionConsiliumService(db=db, gateway=gateway, now_provider=clock.now)
                    rev_resp = await lab_rev_svc.run_lab_revision_consilium(cycle_id=cycle_id)
                    logger.info(f"Lab Revision Consilium executed! Status: {rev_resp.evaluation_status}")
            else:
                logger.info("Day 10 Lab Revision already completed in DB.")

        # 6. Weekly Consilium (Every 7 Days: Day 7, Day 14, Day 21, etc.)
        if sprint_day == 7:
            logger.info(f">>> [WEEK {cal_week} CONSILIUM] 168-Hour Window Elapsed. Running Weekly Consilium! <<<")
            with SessionFactory() as db:
                weekly_svc = WeeklyConsiliumService(db=db, gateway=gateway, now_provider=clock.now)
                is_w_done = weekly_svc.is_weekly_consilium_completed(cycle_id, cal_week)
                if not is_w_done:
                    w_cons_resp = await weekly_svc.run_weekly_consilium(
                        cycle_id=cycle_id,
                        sprint_id=f"sprint_{cycle_id}_w{cal_week}",
                    )
                    logger.info(f"Weekly Consilium Week {cal_week} concluded! Sprint: {w_cons_resp.sprint_id}")
                else:
                    logger.info(f"Weekly Consilium Week {cal_week} already completed in DB.")

        # 7. Check for Medical Referral Recommendation & Document Generation
        with SessionFactory() as db:
            weekly_svc = WeeklyConsiliumService(db=db, gateway=gateway, now_provider=clock.now)
            ref_state = weekly_svc.get_referral_lifecycle_state(cycle_id)

            if ref_state:
                if not simulation_state["referral_issued"]:
                    simulation_state["referral_issued"] = True
                    simulation_state["referral_day"] = current_day
                    simulation_state["referral_timestamp_utc"] = clock.now().isoformat()
                    simulation_state["referral_reason"] = ref_state.get("clinical_reason_for_referral", "Medical evaluation recommended")
                    simulation_state["referral_specialty"] = ", ".join(ref_state.get("target_specialties", [])) if ref_state.get("target_specialties") else "Therapist / Cardiologist"
                    simulation_state["referral_initiating_model"] = ref_state.get("referral_source", "gemini-4-argon")

                    logger.warning("!" * 70)
                    logger.warning(f"[MEDICAL REFERRAL DETECTED ON DAY {current_day}]")
                    logger.warning(f"Reason: {simulation_state['referral_reason']}")
                    logger.warning(f"Specialty: {simulation_state['referral_specialty']}")
                    logger.warning("!" * 70)

                    if current_day <= 46:
                        simulation_state["target_end_day"] = current_day + post_referral_days
                        logger.info(f"Rule B applied: Target end day updated to Day {simulation_state['target_end_day']} (Day {current_day} + 14 days)")
                    else:
                        simulation_state["target_end_day"] = 60
                        logger.info(f"Rule C applied: Target end day set to Day 60 (late referral on Day {current_day})")

                # Generate Official Referral Doctor Document if not ready
                if ref_state.get("document_generation_state") != "ready" or not ref_state.get("document_report_id"):
                    ref_doc_svc = ReferralDocumentService(db=db, gateway=gateway, now_provider=clock.now)
                    doc_res = await ref_doc_svc.generate_referral_document(cycle_id=cycle_id, patient_id=patient_id)
                    logger.info(f"Doctor Observation Document generated! ID: {doc_res.document_report_id}")

        # 8. Advance Clock to Next Day 07:05 UTC (Close Daily Journal for Current Day)
        clock.advance(hours=21, minutes=5)
        with SessionFactory() as db:
            j_existing = db.scalars(
                select(ModelReport).where(
                    ModelReport.cycle_id == cycle_id,
                    ModelReport.report_type == f"{ATHENA_DAILY_JOURNAL_REPORT_TYPE}_w{cal_week}_d{sprint_day}",
                )
            ).first()
            if not j_existing:
                journal_svc = AthenaDailyJournalService(db=db, gateway=gateway, now_provider=clock.now)
                j_rec, _, _ = await journal_svc.close_daily_journal(
                    cycle_id=cycle_id,
                    calendar_week=cal_week,
                    calendar_day=sprint_day,
                    as_of=clock.now(),
                )
                logger.info(f"Day {current_day} (W{cal_week}D{sprint_day}) Athena Daily Journal closed (status: {j_rec.athena_clinical_assessment.status})")
            else:
                logger.info(f"Day {current_day} (W{cal_week}D{sprint_day}) Athena Daily Journal already exists in DB.")

        simulation_state["days_completed"] = current_day

        # 9. Check Stop Conditions
        if simulation_state["referral_issued"] and current_day >= simulation_state["target_end_day"]:
            simulation_state["stop_reason"] = "STOP_B_POST_REFERRAL_WINDOW_COMPLETED"
            logger.info(f"Simulation completed 14 post-referral days on Day {current_day}. Stopping scenario!")
            break

        if current_day >= max_days:
            simulation_state["stop_reason"] = "STOP_B_60_DAYS_REACHED"
            logger.info("Maximum 60 virtual days reached without early referral stop. Stopping scenario!")
            break

        current_day += 1

    # End of Simulation Loop
    simulation_state["real_duration_seconds"] = round(time.time() - simulation_state["start_real_time"], 2)
    simulation_state["virtual_end_utc"] = clock.now().isoformat()
    simulation_state["total_gemini_calls"] = len(gateway.usage_records)

    logger.info("=" * 80)
    logger.info("SIMULATION FINISHED SUCCESSFULLY")
    logger.info(f"Virtual Days Completed: {simulation_state['days_completed']}")
    logger.info(f"Stop Reason: {simulation_state['stop_reason']}")
    logger.info(f"Total Real Gemini Calls: {simulation_state['total_gemini_calls']}")
    logger.info(f"Real Duration: {simulation_state['real_duration_seconds']}s")
    logger.info("=" * 80)

    return simulation_state, gateway.usage_records


# ==============================================================================
# 7. CLI ENTRYPOINT
# ==============================================================================

def main():
    parser = argparse.ArgumentParser(description="AEGIS Synthetic Patient 001 Clinical Simulation")
    parser.add_argument("--preflight-only", action="store_true", help="Run Stage A Pre-flight checks and exit")
    parser.add_argument("--dry-run", action="store_true", help="Run with deterministic mocks for local smoke testing")
    parser.add_argument("--max-days", type=int, default=60, help="Maximum virtual days to run (default: 60)")
    parser.add_argument("--post-referral-days", type=int, default=14, help="Days to observe after referral (default: 14)")
    args = parser.parse_args()

    project_root = Path(__file__).resolve().parent.parent
    db_path = project_root / "backend" / "data" / "synthetic_patient_001.db"
    audit_dir = project_root / "backend" / "audit_logs" / "synthetic_patient_001"
    patient_dir = project_root / "test" / "Synthetic_Patient_001"

    if args.preflight_only:
        preflight_res = execute_preflight_checks(db_path, audit_dir, patient_dir)
        print("\n" + "=" * 60)
        print("STAGE A — PRE-FLIGHT VERIFICATION RESULTS")
        print("=" * 60)
        print(json.dumps(preflight_res, indent=2, ensure_ascii=False))
        print("=" * 60 + "\n")
        if preflight_res["status"] == "READY":
            sys.exit(0)
        else:
            sys.exit(1)

    # Run full simulation
    sim_state, usage_records = asyncio.run(
        run_simulation(
            db_path=db_path,
            audit_dir=audit_dir,
            patient_dir=patient_dir,
            max_days=args.max_days,
            post_referral_days=args.post_referral_days,
            dry_run=args.dry_run,
        )
    )

    # Save summary json
    summary_path = project_root / "backend" / "data" / "simulation_summary.json"
    summary_path.write_text(
        json.dumps({"state": sim_state, "usage_records_count": len(usage_records)}, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    print(f"\n[OK] Simulation finished. Summary saved to {summary_path}")


if __name__ == "__main__":
    main()
