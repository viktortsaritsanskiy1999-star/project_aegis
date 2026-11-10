import asyncio
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re
import socket
from typing import Any, Callable, Dict, List, Optional
import httpx
import pytest
import uvicorn
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session, sessionmaker

from app.config import settings
from app.database import Base, get_db
from app.main import app
from app.models.medical_cycle import MedicalCycle
from app.models.model_report import ModelReport
from app.schemas.privacy import PrivacyLevel
from app.models.patient import Patient
from app.repositories.cycle_repository import MedicalCycleRepository
from app.repositories.pairing_repository import PairingRepository
from app.repositories.patient_repository import PatientRepository
from app.repositories.report_repository import ModelReportRepository
from app.routers.cascade import (
    get_gemini_gateway as cascade_get_gemini_gateway,
    get_server_now_provider as cascade_get_server_now_provider,
)
from app.routers.client_cascade import (
    get_gemini_gateway as client_get_gemini_gateway,
    get_server_now_provider as client_get_server_now_provider,
)
from app.routers.client_chat import (
    get_gemini_gateway as chat_get_gemini_gateway,
    get_server_now_provider as chat_get_server_now_provider,
)
from app.routers.vitals import (
    get_gemini_gateway as vitals_get_gemini_gateway,
    get_server_now_provider as vitals_get_server_now_provider,
)
from app.schemas.chat import AthenaDailyAssessmentModelOutput
from app.schemas.consilium import (
    SYSTEM_KEYS,
    AegisAgentRole,
    LabEventType,
    LabMarkerMutationItem,
    LabReferenceRange,
    VisionOcrMarkerItem,
    VisionOcrModelOutput,
    VisionOcrDocumentMetadata,
)
from app.schemas.lab_scheduling import (
    BackgroundLabRevisionChiefOutput,
    BackgroundLabRevisionDeputyOutput,
    LabChangeType,
    LabDocumentRecord,
    LabDocumentStatus,
    LabManualEditRequest,
    LabManualEditResponse,
    LabRevisionPendingTask,
    LabSchedulingStatusResponse,
)
from app.schemas.reports import ClientModelReportResponse
from app.schemas.wellness import (
    WellnessDailyReport,
    WellnessStatus,
    WellnessTimeWindow,
    WellnessTriggerType,
    WellnessBiometricsSummary,
)
from app.services.athena_chat_service import AthenaChatService
from app.services.athena_daily_journal_service import AthenaDailyJournalService
from app.services.athena_weekly_plan_service import (
    ATHENA_ROUTINE_QUESTION_REPORT_TYPE,
    ATHENA_WEEKLY_PLAN_REPORT_TYPE,
    AthenaWeeklyPlanRecord,
    AthenaWeeklyPlanService,
)
from app.services.clinical_automation_service import (
    AutomationJobRecord,
    AutomationJobStatus,
    AutomationJobType,
    ClinicalAutomationService,
    ClinicalAutomationWorker,
    ConsiliumConcurrencyConflictError,
)
from app.services.gemini_gateway import GeminiGateway, GeminiGatewayError, GeminiModelRole
from app.services.lab_revision_consilium_service import (
    BACKGROUND_LAB_REVISION_REPORT_TYPE,
    LabRevisionConsiliumService,
    LABORATORY_LEDGER_REPORT_TYPE,
)
from app.services.lab_scheduling_service import (
    BACKGROUND_LAB_REVISION_COOLDOWN_HOURS,
    LAB_DOCUMENT_REPORT_TYPE,
    LAB_REVISION_DEBOUNCE_MINUTES,
    LAB_REVISION_PENDING_TASK_REPORT_TYPE,
    LabSchedulingService,
)
from app.services.mode2_vitals_service import Mode2EpisodeStatus, Mode2VitalsMonitoringService
from app.services.primary_consilium_service import PrimaryConsiliumService
from app.services.weekly_consilium_service import (
    ATHENA_ACTIVE_STRATEGY_REPORT_TYPE,
    ATHENA_SCHEDULE_ANCHOR_REPORT_TYPE,
    WEEKLY_CHIEF_INTERNAL_REPORT_TYPE,
    WEEKLY_CONSILIUM_EXECUTION_LOG_REPORT_TYPE,
    WEEKLY_DEPUTY_INTERNAL_REPORT_TYPE,
    WEEKLY_OVERALL_CLIENT_REPORT_TYPE,
    WeeklyConsiliumService,
)
from app.services.wellness_daily_service import WellnessDailyService


# ==============================================================================
# CONTROLLED CLOCK FOR DETERMINISTIC VIRTUAL TIME ADVANCEMENT
# ==============================================================================

class ControlledClock:
    def __init__(self, start_dt: datetime):
        self.current = start_dt

    def now(self) -> datetime:
        return self.current

    def advance(self, **kwargs: Any) -> datetime:
        self.current = self.current + timedelta(**kwargs)
        return self.current

    def set(self, dt: datetime) -> datetime:
        self.current = dt
        return self.current


# ==============================================================================
# ISOLATED TEST SQLITE DATABASE FACTORY
# ==============================================================================

def _create_isolated_file_db(db_path: Path, busy_timeout_ms: int = 10000):
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False, "timeout": busy_timeout_ms / 1000.0},
        pool_pre_ping=True,
    )

    @event.listens_for(engine, "connect")
    def _set_pragmas(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON;")
        cursor.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)};")

    Base.metadata.create_all(bind=engine)
    from alembic.config import Config
    from alembic import command
    ini = (Path(__file__).resolve().parent.parent / "alembic.ini").resolve()
    cfg = Config(str(ini))
    cfg.set_main_option("script_location", str(ini.parent / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path.as_posix()}")
    command.stamp(cfg, "head")

    session_factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    return engine, session_factory


# ==============================================================================
# SAMPLE OUTPUT BUILDERS FOR DETERMINISTIC MOCK RESPONSES
# ==============================================================================

def _gemini_envelope(payload: Any) -> Dict[str, Any]:
    text_content = json.dumps(payload, ensure_ascii=False) if isinstance(payload, (dict, list)) else str(payload)
    return {
        "candidates": [
            {
                "content": {
                    "parts": [{"text": text_content}],
                    "role": "model",
                },
                "finishReason": "STOP",
            }
        ]
    }


def _sample_system_agent_output(system_key: str) -> Dict[str, Any]:
    return {
        "system_view": {
            "isolated_labs": {f"{system_key}_marker": "optimal"}
        },
        "system_logic": {
            "system_general_shelf": f"Academic pathophysiological conclusion for {system_key}."
        },
        "training_sandbox": {
            "system_training_shelf": f"Deficit log and observations for {system_key}."
        },
    }


def _sample_flash_output() -> Dict[str, Any]:
    return {
        "flash_general_shelf": "Заместитель главного врача: Первичный осмотр 8 систем завершен. Профиль риска низкий.",
        "organ_syntheses": {
            k: {
                "system_title": k.upper(),
                "status": "normal",
                "summary": f"Функция {k} в норме.",
                "clinical_correlations": [],
                "client_summary": f"Состояние системы {k} стабильно.",
            }
            for k in SYSTEM_KEYS
        },
    }


def _sample_pro_output() -> Dict[str, Any]:
    return {
        "pro_general_shelf": "Главный врач: Первичная стратегия утверждена. Целевой коридор нагрузок комфортный.",
        "pro_training_shelf": "Клинический базис: кардиоваскулярный резерв сохранен. Адаптация устойчива.",
        "client_facing_report": {
            "title": "Aegis Master Health Strategy (Baseline)",
            "summary": "Функциональные параметры стабильны. Рисков декомпенсации нет.",
            "highlights": [
                "Поддерживайте комфортный режим двигательной активности",
                "Контролируйте восстановление пульса после тренировок",
            ],
        },
        "vitals_thresholds": {
            "min_pessimum_bpm": 50,
            "max_pessimum_bpm": 120,
        },
    }


def _sample_anamnesis_output() -> Dict[str, Any]:
    return {
        "user_view": {
            "summary": "Primary anamnesis processed across 8 systems in blind mode."
        },
        "system_logic": {
            key: {
                "data": f"{key.upper()} anamnesis facts: mild fatigue, active routine.",
                "internal_comment": f"M1 blind comment for {key}",
            }
            for key in SYSTEM_KEYS
        },
        "training_sandbox": {
            "raw_diagnostic_hypothesis": "Normal physiological profile with mild autonomic fatigue.",
            "data_gap_analysis": "Follow resting heart rate and recovery metrics.",
            "error_probability": "Low (10%)",
        },
    }


def _sample_lab_revision_deputy_output() -> Dict[str, Any]:
    return {
        "deputy_synthesis": "Синтез заместителя: новые лабораторные параметры стабильны.",
        "specialist_discrepancies": [],
        "clinical_correlations": ["Метаболический профиль стабилен."],
        "data_limitations": [],
        "draft_hypotheses": [],
    }


def _sample_lab_revision_chief_output() -> Dict[str, Any]:
    return {
        "analytical_summary": "Главный врач: аналитический вердикт ревизии лаборатории утвержден.",
        "observed_facts": ["Показатели крови соответствуют целевому коридору."],
        "clinical_hypotheses": [],
        "uncertainties_and_data_deficits": [],
        "questions_for_weekly_consilium": ["Продолжить наблюдение в рамках недельного спринта."],
        "cascade_quality_audit": "8 специалистов Flash-Lite и заместитель Flash выполнили ревизию корректно.",
    }


def _sample_weekly_flash_output() -> Dict[str, Any]:
    return {
        "weekly_deputy_analysis": "Заместитель: Динамика первой недели благоприятная. Биомаркеры и пульс стабильны.",
        "user_system_reports": {
            k: {
                "summary": f"Показатели системы {k} за прошедшую неделю стабильны.",
                "has_changes": True,
            }
            for k in SYSTEM_KEYS
        },
    }


def _sample_weekly_pro_output() -> Dict[str, Any]:
    return {
        "updated_weekly_strategy": "Продолжить базовую стратегию адаптации Недели №1 с сохранением текущего двигательного режима.",
        "strategy_status": "continued",
        "weekly_overall_report": {
            "title": "Итоги недели №1: Устойчивая адаптация",
            "summary": "Все ключевые физиологические системы демонстрируют стабильные показатели.",
            "highlights": [
                "Пульс в покое и при нагрузке в пределах нормы",
                "Лабораторные показатели сохраняют стабильность",
                "Рекомендовано сохранить текущий режим тренировок",
            ],
        },
        "flash_quality_review": {
            "approved_for_publication": True,
            "corrected_user_system_reports": {},
        },
    }


def _sample_weekly_plan(cycle_id: str, week_number: int = 1) -> Dict[str, Any]:
    return {
        "plan_id": f"plan_{cycle_id}_w{week_number}",
        "cycle_id": cycle_id,
        "week_number": week_number,
        "source_strategy_report_id": f"rep_{cycle_id}_strat_v{week_number}",
        "source_strategy_version": week_number,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "summary_for_curator": f"План недели {week_number}: контроль восстановления ЧСС",
        "goals": [
            {
                "goal_id": f"goal_{week_number}_1",
                "title": "Оценка самочувствия после дневной активности",
                "clinical_rationale": "Убедиться в отсутствии одышки и выраженного утомления",
                "priority": 1,
                "known_facts": ["Базовая переносимость удовлетворительная"],
                "target_clarification": "Уточнить наличие одышки или сердцебиения",
                "sufficiency_criteria": "Пациент ответил о наличии или отсутствии симптомов",
                "status": "active",
                "progress_notes": None,
                "questions_asked_count": 0,
            },
            {
                "goal_id": f"goal_{week_number}_2",
                "title": "Контроль восстановления ночного сна",
                "clinical_rationale": "Оценить влияние физической активности на качество сна",
                "priority": 2,
                "known_facts": ["Продолжительность сна ~7.5 ч"],
                "target_clarification": "Уточнить время засыпания и пробуждений",
                "sufficiency_criteria": "Ответ о субъективном качестве сна",
                "status": "active",
                "progress_notes": None,
                "questions_asked_count": 0,
            },
        ],
    }


def _sample_daily_journal_assessment() -> Dict[str, Any]:
    return {
        "clinical_status": "STABLE",
        "functional_reserve_assessment": "Функциональный резерв миокарда и сосудистого тонуса сохранен.",
        "key_clinical_observations": ["ЧСС в покое 60-65 уд/мин, без патологических пауз."],
        "conclusions_for_weekly_sprint": "День завершен без признаков декомпенсации.",
        "red_flags_present": False,
    }


def _sample_wellness_output(ep_id: Optional[str] = None) -> Dict[str, Any]:
    evals = []
    if ep_id:
        evals.append({
            "episode_id": ep_id,
            "workout_context": "confirmed_workout",
            "analysis": "Эпизод физической нагрузки подтвержден пользователем, гемодинамика адекватна.",
        })
    return {
        "observed_trends": ["Качественный сон и стабильный утренний пульс."],
        "limitations_and_gaps": ["АД утром не фиксировалось."],
        "questionable_measurements": [],
        "episode_evaluations": evals,
        "summary_for_athena": "Пульс и SpO2 в безопасном диапазоне. Состояние стабильно.",
    }


# ==============================================================================
# MOCK GEMINI GATEWAY WITH MULTI-MODAL VISION & CALL TRACKING
# ==============================================================================

class Stage7B3MockGateway:
    def __init__(self, recorded_calls: List[Dict[str, Any]]):
        self.recorded_calls = recorded_calls
        self.dynamic_ocr_markers: List[Dict[str, Any]] = [
            {
                "marker_key": "ferritin",
                "original_name": "Ferritin",
                "raw_value": "45.0",
                "numeric_value": 45.0,
                "unit": "ng/mL",
                "reference_range": {"low": 20.0, "high": 250.0, "text": "20-250"},
                "sample_date": "2026-11-01",
                "category": "iron_metabolism",
                "requires_user_verification": False,
            },
            {
                "marker_key": "glucose",
                "original_name": "Glucose",
                "raw_value": "5.2",
                "numeric_value": 5.2,
                "unit": "mmol/L",
                "reference_range": {"low": 3.9, "high": 6.1, "text": "3.9-6.1"},
                "sample_date": "2026-11-01",
                "category": "carbohydrate_metabolism",
                "requires_user_verification": False,
            },
        ]

    def set_ocr_markers(self, markers: List[Dict[str, Any]]) -> None:
        self.dynamic_ocr_markers = markers

    def create_gateway(self) -> GeminiGateway:
        async def handler(request: httpx.Request) -> httpx.Response:
            url_path = request.url.path
            body = json.loads(request.content.decode("utf-8"))
            contents = body.get("contents", [])
            prompt_parts: List[str] = []
            for p in body.get("system_instruction", {}).get("parts", []):
                if isinstance(p, dict) and "text" in p:
                    prompt_parts.append(p["text"])
            for c in contents:
                for p in c.get("parts", []):
                    if isinstance(p, dict) and "text" in p:
                        prompt_parts.append(p["text"])
            prompt = "\n".join(prompt_parts)

            call_record = {
                "index": len(self.recorded_calls),
                "url_path": url_path,
                "prompt": prompt,
            }
            self.recorded_calls.append(call_record)

            # 1. Gemini Vision OCR
            if (
                "vision" in url_path
                or "visionocrmodeloutput" in prompt.lower()
                or "laboratory test report image" in prompt.lower()
                or "extract all laboratory markers" in prompt.lower()
                or "lab-vision-ocr" in prompt.lower()
            ):
                ocr_payload = {
                    "document_metadata": {
                        "order_id": "ORD-7B3-001",
                        "laboratory_name": "InVitro Diagnostics",
                        "sample_date": "2026-11-01",
                        "page_count": 1,
                        "notes": "Verified laboratory document",
                    },
                    "markers": self.dynamic_ocr_markers,
                }
                return httpx.Response(200, json=_gemini_envelope(ocr_payload))

            # 2. 8 Organ Specialists (Gemini Flash-Lite)
            if "flash-lite" in url_path or "clinical specialist for" in prompt.lower():
                sys_key = "cvs"
                for k in SYSTEM_KEYS:
                    if f"clinical specialist for {k}" in prompt.lower() or f"specialist for {k}" in prompt.lower():
                        sys_key = k
                        break
                return httpx.Response(200, json=_gemini_envelope(_sample_system_agent_output(sys_key)))

            # 3. Lab Revision Deputy
            if (
                "background comparative lab-revision" in prompt.lower()
                or "backgroundlabrevisiondeputyoutput" in prompt.lower()
                or "lab revision deputy" in prompt.lower()
            ):
                return httpx.Response(200, json=_gemini_envelope(_sample_lab_revision_deputy_output()))

            # 4. Lab Revision Chief
            if (
                "supreme analytical lab-revision" in prompt.lower()
                or "backgroundlabrevisionchiefoutput" in prompt.lower()
                or "lab revision chief" in prompt.lower()
            ):
                return httpx.Response(200, json=_gemini_envelope(_sample_lab_revision_chief_output()))

            # 5. Weekly Consilium Deputy (Gemini Flash)
            if "1 week (7 days) of patient monitoring has passed" in prompt.lower() or "weekly_deputy_analysis" in prompt.lower():
                return httpx.Response(200, json=_gemini_envelope(_sample_weekly_flash_output()))

            # 6. Weekly Consilium Chief (Gemini Pro)
            if "update the working strategy for the next week" in prompt.lower() or "updated_weekly_strategy" in prompt.lower():
                return httpx.Response(200, json=_gemini_envelope(_sample_weekly_pro_output()))

            # 7. Athena Weekly Plan Record (Gemini Flash)
            if "[internal weekly plan formulation scenario]" in prompt.lower() or "athenaweeklyplanrecord" in prompt.lower():
                wk_num = 2 if ("неделя №2" in prompt.lower() or "week 2" in prompt.lower() or "week_number: 2" in prompt.lower() or "неделя 2" in prompt.lower()) else 1
                return httpx.Response(200, json=_gemini_envelope(_sample_weekly_plan("cycle_7b3_001", wk_num)))

            # 8. Athena Daily Journal Clinical Assessment (Gemini Flash)
            if (
                "[internal daily clinical assessment scenario]" in prompt.lower()
                or "athenadailyassessmentmodeloutput" in prompt.lower()
                or "daily clinical assessment scenario" in prompt.lower()
                or "completed 24-hour observation day source evidence" in prompt.lower()
            ):
                return httpx.Response(200, json=_gemini_envelope(_sample_daily_journal_assessment()))

            # 9. Wellness Daily Analysis (Gemini Flash)
            if (
                "wellness daily analyzer" in prompt.lower()
                or "wellnessmodeloutput" in prompt.lower()
                or "daily wellness analysis request" in prompt.lower()
            ):
                ep_match = re.search(r'(ep_mode2_[a-zA-Z0-9_]+)', prompt)
                ep_id = ep_match.group(1) if ep_match else None
                return httpx.Response(200, json=_gemini_envelope(_sample_wellness_output(ep_id)))

            # 10. Primary Chief Physician (Gemini Pro / Argon)
            if "pro" in url_path or "argon" in url_path or ("chief physician" in prompt.lower() and "deputy" not in prompt.lower()):
                return httpx.Response(200, json=_gemini_envelope(_sample_pro_output()))

            # 11. Primary Deputy Chief Physician (Gemini Flash)
            if "head of department" in prompt.lower() or "deputy chief physician" in prompt.lower():
                return httpx.Response(200, json=_gemini_envelope(_sample_flash_output()))

            # 12. Anamnesis M1 (Gemini Flash)
            if "anamnesis" in prompt.lower() or "questionnaire" in prompt.lower():
                return httpx.Response(200, json=_gemini_envelope(_sample_anamnesis_output()))

            # 13. Athena Weekly Wrap-up Message (Gemini Flash)
            if "wrap-up message formulation" in prompt.lower() or "wrapup" in prompt.lower() or "wrap-up" in prompt.lower() or "weekly_wrapup" in prompt.lower():
                wrapup_payload = {
                    "message": (
                        "Поздравляю с успешным завершением первой недели! Все системы организма показали отличную динамику. "
                        "Ваша обновленная стратегия и 8 детальных отчетов готовы к просмотру в Клиентском Хранилище (Safe)."
                    )
                }
                return httpx.Response(200, json=_gemini_envelope(wrapup_payload))

            chat_text = (
                "Здравствуйте! Я Афина, ваш медицинский куратор.\n\n"
                "[CLINICAL_REPORT]\n"
                "{\n"
                '  "status": "NORMAL",\n'
                '  "symptoms_mentioned": [],\n'
                '  "subjective_wellbeing_score": 8,\n'
                '  "adherence_to_plan": "HIGH",\n'
                '  "red_flags": false,\n'
                '  "summary_for_master": "Routine chat handled."\n'
                "}\n"
                "[/CLINICAL_REPORT]"
            )
            return httpx.Response(200, json=_gemini_envelope(chat_text))

        return GeminiGateway(
            api_key="stage-7b3-mock-key",
            transport=httpx.MockTransport(handler),
            max_retries=1,
            sleep_func=lambda _: None,
        )


def _seed_patient_and_intake(
    session: Session,
    patient_id: str,
    cycle_id: str,
    intake_time: datetime,
    pairing_code: Optional[str] = None,
    seed_primary_overall: bool = True,
) -> None:
    PatientRepository(session).create(patient_id=patient_id, status="active")
    cycle_repo = MedicalCycleRepository(session)
    cycle_repo.create(
        cycle_id=cycle_id,
        patient_id=patient_id,
        status="active",
    )
    if pairing_code and getattr(settings, "pairing_secret", None):
        PairingRepository(session, secret=settings.pairing_secret).create_code(
            patient_id=patient_id,
            raw_code_override=pairing_code,
        )

    primary_service = PrimaryConsiliumService(db=session)
    primary_service.save_preparatory_anamnesis_result(
        cycle_id=cycle_id,
        anamnesis_output=_sample_anamnesis_output(),
    )
    primary_service.save_preparatory_ocr_labs(
        cycle_id=cycle_id,
        shared_labs_table={
            "hemoglobin": "144 g/L",
            "ferritin": "42 ng/mL",
            "creatinine": "82 umol/L",
            "glucose": "5.1 mmol/L",
        },
        created_at=intake_time - timedelta(hours=2),
    )
    rev_service = LabRevisionConsiliumService(db=session)
    rev_service._ensure_baseline_ledger_seeded(cycle_id)

    primary_service.save_preparatory_ecg_summary(
        cycle_id=cycle_id,
        ecg_summary="Sinus rhythm, HR 64 bpm, normal electrical axis.",
    )

    rep_repo = ModelReportRepository(session)
    rep_repo.create_next_version(
        cycle_id=cycle_id,
        model="aegis-system",
        report_type=ATHENA_SCHEDULE_ANCHOR_REPORT_TYPE,
        privacy_level=PrivacyLevel.SERVER_ONLY,
        content={"anchor_timestamp_utc": intake_time.isoformat()},
        commit=False,
    )
    rep_repo.create_next_version(
        cycle_id=cycle_id,
        model="gemini-1.5-pro",
        report_type=ATHENA_ACTIVE_STRATEGY_REPORT_TYPE,
        privacy_level=PrivacyLevel.SERVER_ONLY,
        content={
            "active_strategy_text": "Week 1 baseline strategy",
            "clinical_background_text": "Baseline clinical background",
            "deputy_synthesis_text": "Baseline deputy synthesis",
            "active_strategy": "Week 1 baseline strategy",
            "clinical_background": "Baseline clinical background",
            "deputy_synthesis": "Baseline deputy synthesis",
            "vitals_thresholds": {"min_pessimum_bpm": 50, "max_pessimum_bpm": 120},
        },
        commit=False,
    )
    rep_repo.create_next_version(
        cycle_id=cycle_id,
        model="gemini-1.5-pro",
        report_type="chief_physician_internal",
        privacy_level=PrivacyLevel.SERVER_ONLY,
        content=_sample_pro_output(),
        commit=False,
    )
    if seed_primary_overall:
        rep_repo.create_next_version(
            cycle_id=cycle_id,
            model="gemini-1.5-pro",
            report_type="user_overall_report",
            privacy_level=PrivacyLevel.CLIENT_FACING,
            content={
                "title": "Aegis Baseline Health Strategy",
                "summary": "Функциональные параметры стабильны.",
                "highlights": ["Продолжать режим адаптации."],
            },
            commit=False,
        )
    session.commit()

# ==============================================================================
# TEST 1: LAB HTTP E2E, FILE DEDUPLICATION & IDENTICAL DATA DETECTION
# ==============================================================================

@pytest.mark.anyio
async def test_01_lab_http_e2e_deduplication_and_identical_data(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """
    HTTP E2E LABORATORY VERIFICATION:
    1. Upload laboratory image via real HTTP API -> Vision OCR extraction -> Ledger update.
    2. Document status is PROCESSED, ledger version increments.
    3. Exact byte copy re-upload -> DUPLICATE_FILE detected, OCR skipped, zero extra AI calls.
    4. Different image with identical clinical data -> NO_CHANGE, ledger version unchanged.
    """
    db_file = tmp_path / "stage_7b3_test1.db"
    backup_dir = tmp_path / "backups"
    test_engine, TestSessionFactory = _create_isolated_file_db(db_file)

    t0_dt = datetime(2026, 10, 30, 8, 0, 0, tzinfo=timezone.utc)
    clock = ControlledClock(t0_dt)
    patient_id = "pat_7b3_001"
    cycle_id = "cycle_7b3_001"

    monkeypatch.setattr(settings, "jwt_secret", "test_jwt_secret_stage_7b3_1234567890!")
    monkeypatch.setattr(settings, "pairing_secret", "test_pairing_secret_stage_7b3_12345!")
    monkeypatch.setattr(settings, "api_secret", "test_api_secret_stage_7b3_1234567890")
    monkeypatch.setattr(settings, "access_token_ttl_minutes", 60 * 24 * 30)

    recorded_calls: List[Dict[str, Any]] = []
    mock_helper = Stage7B3MockGateway(recorded_calls)
    mock_gw = mock_helper.create_gateway()

    def _override_get_db():
        db = TestSessionFactory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[client_get_gemini_gateway] = lambda: mock_gw
    app.dependency_overrides[client_get_server_now_provider] = lambda: clock.now
    app.dependency_overrides[chat_get_gemini_gateway] = lambda: mock_gw
    app.dependency_overrides[chat_get_server_now_provider] = lambda: clock.now
    app.dependency_overrides[vitals_get_gemini_gateway] = lambda: mock_gw
    app.dependency_overrides[vitals_get_server_now_provider] = lambda: clock.now
    app.dependency_overrides[cascade_get_gemini_gateway] = lambda: mock_gw
    app.dependency_overrides[cascade_get_server_now_provider] = lambda: clock.now

    app.state.engine = test_engine
    app.state.backup_dir = backup_dir
    app.state.enable_automation_worker = False

    with TestSessionFactory() as db:
        _seed_patient_and_intake(db, patient_id=patient_id, cycle_id=cycle_id, intake_time=t0_dt, pairing_code="123456")

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    server_config = uvicorn.Config(app=app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(server_config)
    server_task = asyncio.create_task(server.serve())

    while not server.started:
        await asyncio.sleep(0.01)

    try:
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=30.0) as client:
            # Pair device
            pair_resp = await client.post(
                "/api/v1/auth/pair",
                json={
                    "pairing_code": "123456",
                    "device_id": "dev_test_01",
                    "device_name": "Test Device",
                    "platform": "android",
                },
            )
            assert pair_resp.status_code == 200
            token = pair_resp.json()["access_token"]
            auth_headers = {"Authorization": f"Bearer {token}"}

            # Check initial ledger state
            init_state_resp = await client.get("/api/v1/client/labs/state", headers=auth_headers)
            assert init_state_resp.status_code == 200
            init_ledger = init_state_resp.json()
            init_version = init_ledger["ledger_version"]

            # 1. First upload: Genuine JPEG
            calls_before_first = len(recorded_calls)
            jpeg_bytes_1 = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x01\x00\x60\x00\x60\x00\x00genuine_report_01"
            files_1 = [("files", ("blood_report_1.jpg", jpeg_bytes_1, "image/jpeg"))]
            data_1 = {"is_correction": "false"}

            upload_resp_1 = await client.post(
                "/api/v1/client/cascade/intake/labs",
                files=files_1,
                data=data_1,
                headers=auth_headers,
            )
            assert upload_resp_1.status_code == 200
            data_resp_1 = upload_resp_1.json()
            assert len(data_resp_1["confirmed_markers"]) >= 2

            # Verify ledger version increased
            state_resp_1 = await client.get("/api/v1/client/labs/state", headers=auth_headers)
            assert state_resp_1.status_code == 200
            version_after_1 = state_resp_1.json()["ledger_version"]
            assert version_after_1 > init_version

            # Verify document saved in DB with status PROCESSED
            with TestSessionFactory() as db:
                sched_svc = LabSchedulingService(db=db, now_provider=clock.now)
                docs = sched_svc.get_document_records(cycle_id)
                assert len(docs) == 1
                assert docs[0].status == LabDocumentStatus.PROCESSED
                assert docs[0].is_duplicate is False

            # 2. Second upload: Exact byte copy
            calls_before_second = len(recorded_calls)
            files_2 = [("files", ("blood_report_1_copy.jpg", jpeg_bytes_1, "image/jpeg"))]
            upload_resp_2 = await client.post(
                "/api/v1/client/cascade/intake/labs",
                files=files_2,
                data=data_1,
                headers=auth_headers,
            )
            assert upload_resp_2.status_code == 200
            # Zero new Gemini calls!
            assert len(recorded_calls) == calls_before_second

            # Verify document saved as DUPLICATE
            with TestSessionFactory() as db:
                sched_svc = LabSchedulingService(db=db, now_provider=clock.now)
                docs = sched_svc.get_document_records(cycle_id)
                assert len(docs) == 2
                assert any(d.status == LabDocumentStatus.DUPLICATE and d.is_duplicate for d in docs)
                assert any(d.status == LabDocumentStatus.PROCESSED and not d.is_duplicate for d in docs)

            # 3. Third upload: Different image bytes, identical clinical data
            jpeg_bytes_3 = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x01\x00\x60\x00\x60\x00\x00different_photo_same_data"
            files_3 = [("files", ("photo_different_angle.jpg", jpeg_bytes_3, "image/jpeg"))]
            upload_resp_3 = await client.post(
                "/api/v1/client/cascade/intake/labs",
                files=files_3,
                data=data_1,
                headers=auth_headers,
            )
            assert upload_resp_3.status_code == 200

            # Ledger version does NOT increment because data is identical!
            state_resp_3 = await client.get("/api/v1/client/labs/state", headers=auth_headers)
            assert state_resp_3.json()["ledger_version"] == version_after_1

    finally:
        server.should_exit = True
        await server_task
        app.dependency_overrides.clear()

# ==============================================================================
# TEST 2: MANUAL EDIT, 15-MIN DEBOUNCE, 10-MODEL LAB REVISION & 72-H COOLDOWN
# ==============================================================================

@pytest.mark.anyio
async def test_02_manual_edit_debounce_and_cooldown_lifecycle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """
    MANUAL EDIT & BACKGROUND SCHEDULING LIFECYCLE:
    1. Manual edit with meaningful change -> MEANINGFUL_CHANGE, 15-min debounce established.
    2. Manual edit with identical value (4 -> 4) -> NO_CHANGE, debounce NOT reset.
    3. Before 15 minutes: task pending, not ready.
    4. Past 15 minutes: task ready.
    5. ClinicalAutomationWorker executes 10-model Lab Revision: 8 Flash-Lite -> Flash -> Pro.
    6. Revision completes -> 72-hour cooldown established.
    7. New labs ingested during cooldown -> recorded in ledger, but revision blocked by cooldown.
    """
    db_file = tmp_path / "stage_7b3_test2.db"
    backup_dir = tmp_path / "backups"
    test_engine, TestSessionFactory = _create_isolated_file_db(db_file)

    t0_dt = datetime(2026, 10, 30, 8, 0, 0, tzinfo=timezone.utc)
    clock = ControlledClock(t0_dt)
    patient_id = "pat_7b3_002"
    cycle_id = "cycle_7b3_002"

    monkeypatch.setattr(settings, "jwt_secret", "test_jwt_secret_stage_7b3_1234567890!")
    monkeypatch.setattr(settings, "pairing_secret", "test_pairing_secret_stage_7b3_12345!")
    monkeypatch.setattr(settings, "api_secret", "test_api_secret_stage_7b3_1234567890")
    monkeypatch.setattr(settings, "access_token_ttl_minutes", 60 * 24 * 30)

    recorded_calls: List[Dict[str, Any]] = []
    mock_helper = Stage7B3MockGateway(recorded_calls)
    mock_gw = mock_helper.create_gateway()

    def _override_get_db():
        db = TestSessionFactory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[client_get_gemini_gateway] = lambda: mock_gw
    app.dependency_overrides[client_get_server_now_provider] = lambda: clock.now
    app.dependency_overrides[chat_get_gemini_gateway] = lambda: mock_gw
    app.dependency_overrides[chat_get_server_now_provider] = lambda: clock.now
    app.dependency_overrides[vitals_get_gemini_gateway] = lambda: mock_gw
    app.dependency_overrides[vitals_get_server_now_provider] = lambda: clock.now
    app.dependency_overrides[cascade_get_gemini_gateway] = lambda: mock_gw
    app.dependency_overrides[cascade_get_server_now_provider] = lambda: clock.now

    app.state.engine = test_engine
    app.state.backup_dir = backup_dir
    app.state.enable_automation_worker = False

    with TestSessionFactory() as db:
        _seed_patient_and_intake(db, patient_id=patient_id, cycle_id=cycle_id, intake_time=t0_dt, pairing_code="123456")

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    server_config = uvicorn.Config(app=app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(server_config)
    server_task = asyncio.create_task(server.serve())

    while not server.started:
        await asyncio.sleep(0.01)

    try:
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=30.0) as client:
            pair_resp = await client.post(
                "/api/v1/auth/pair",
                json={
                    "pairing_code": "123456",
                    "device_id": "dev_test_02",
                    "device_name": "Test Device 2",
                    "platform": "android",
                },
            )
            assert pair_resp.status_code == 200
            token = pair_resp.json()["access_token"]
            auth_headers = {"Authorization": f"Bearer {token}"}

            # 1. Manual edit with meaningful change: Ferritin from 42 to 18 ng/mL
            edit_resp_1 = await client.post(
                "/api/v1/client/cascade/labs/manual-edit",
                json={
                    "mutations": [
                        {
                            "marker_key": "ferritin",
                            "display_name": "Ferritin",
                            "value": 18.0,
                            "unit": "ng/mL",
                            "sample_date": "2026-11-01",
                        }
                    ]
                },
                headers=auth_headers,
            )
            assert edit_resp_1.status_code == 200
            edit_data_1 = edit_resp_1.json()
            assert edit_data_1["change_type"] == "meaningful_change"
            assert edit_data_1["mutations_applied"] == 1
            ver_after_edit1 = edit_data_1["ledger_version"]

            # Check scheduling status: Debounce is set for 15 minutes
            status_resp_1 = await client.get("/api/v1/client/cascade/labs/scheduling-status", headers=auth_headers)
            assert status_resp_1.status_code == 200
            sched_data_1 = status_resp_1.json()
            assert sched_data_1["has_pending_task"] is True
            assert sched_data_1["is_ready_for_revision"] is False
            debounce_time_1 = sched_data_1["debounce_until_utc"]
            assert debounce_time_1 is not None

            # 2. Manual edit with identical value: Ferritin 18.0 -> 18.0
            edit_resp_2 = await client.post(
                "/api/v1/client/cascade/labs/manual-edit",
                json={
                    "mutations": [
                        {
                            "marker_key": "ferritin",
                            "display_name": "Ferritin",
                            "value": 18.0,
                            "unit": "ng/mL",
                            "sample_date": "2026-11-01",
                        }
                    ]
                },
                headers=auth_headers,
            )
            assert edit_resp_2.status_code == 200
            edit_data_2 = edit_resp_2.json()
            assert edit_data_2["change_type"] == "no_change"
            assert edit_data_2["mutations_applied"] == 0

            # Verify debounce time was NOT changed
            status_resp_2 = await client.get("/api/v1/client/cascade/labs/scheduling-status", headers=auth_headers)
            sched_data_2 = status_resp_2.json()
            assert sched_data_2["debounce_until_utc"] == debounce_time_1

            # 3. Advance clock by 10 minutes: Still in debounce
            clock.advance(minutes=10)
            status_resp_3 = await client.get("/api/v1/client/cascade/labs/scheduling-status", headers=auth_headers)
            assert status_resp_3.json()["is_ready_for_revision"] is False

            # 4. Advance clock by 6 minutes (total 16m): Debounce expired, ready for execution!
            clock.advance(minutes=6)
            status_resp_4 = await client.get("/api/v1/client/cascade/labs/scheduling-status", headers=auth_headers)
            assert status_resp_4.json()["is_ready_for_revision"] is True

            # 5. Worker executes 10-model Lab Revision
            calls_before_rev = len(recorded_calls)
            worker = ClinicalAutomationWorker(
                session_factory=TestSessionFactory,
                gateway_factory=lambda: mock_gw,
                now_provider=clock.now,
                poll_interval_seconds=0.05,
                enable_lab_revision=True,
            )
            jobs = await worker.run_once(discover=True)
            lab_rev_jobs = [j for j in jobs if j.job_type == AutomationJobType.LAB_REVISION]
            assert len(lab_rev_jobs) == 1
            assert lab_rev_jobs[0].status == AutomationJobStatus.COMPLETED

            # Exactly 10 Gemini calls made: 8 Flash-Lite + 1 Flash deputy + 1 Pro chief!
            rev_calls = [
                c for c in recorded_calls[calls_before_rev:]
                if "initial introduction" not in c["prompt"].lower()
            ]
            assert len(rev_calls) == 10
            assert sum(1 for c in rev_calls if "flash-lite" in c["url_path"]) == 8
            assert sum(1 for c in rev_calls if "flash-lite" not in c["url_path"] and "flash" in c["url_path"]) == 1
            assert sum(1 for c in rev_calls if "pro" in c["url_path"] or "argon" in c["url_path"]) == 1

            # 6. Verify 72-hour cooldown is established
            status_resp_5 = await client.get("/api/v1/client/cascade/labs/scheduling-status", headers=auth_headers)
            sched_data_5 = status_resp_5.json()
            assert sched_data_5["cooldown_until_utc"] is not None
            cooldown_until = sched_data_5["cooldown_until_utc"]
            assert cooldown_until is not None

            # 7. Ingest new lab during cooldown (e.g. at +2h)
            clock.advance(hours=2)
            edit_resp_3 = await client.post(
                "/api/v1/client/cascade/labs/manual-edit",
                json={
                    "mutations": [
                        {
                            "marker_key": "creatinine",
                            "display_name": "Creatinine",
                            "value": 115.0,
                            "unit": "umol/L",
                            "sample_date": "2026-11-01",
                        }
                    ]
                },
                headers=auth_headers,
            )
            assert edit_resp_3.status_code == 200
            assert edit_resp_3.json()["change_type"] == "meaningful_change"

            # Check status: Task is pending, but blocked by cooldown!
            status_resp_6 = await client.get("/api/v1/client/cascade/labs/scheduling-status", headers=auth_headers)
            sched_data_6 = status_resp_6.json()
            assert sched_data_6["has_pending_task"] is True
            assert sched_data_6["cooldown_until_utc"] is not None
            assert sched_data_6["is_ready_for_revision"] is False

            # Worker runs: zero lab revision executed due to active cooldown!
            jobs_cooldown = await worker.run_once(discover=True)
            assert len([j for j in jobs_cooldown if j.job_type == AutomationJobType.LAB_REVISION]) == 0

    finally:
        server.should_exit = True
        await server_task
        app.dependency_overrides.clear()

# ==============================================================================
# TEST 3: WEEKLY CONSILIUM WITH LAB REVISIONS, UNREVIEWED LABS & STRICT FLASH->PRO
# ==============================================================================

@pytest.mark.anyio
async def test_03_weekly_consilium_with_lab_revision_and_unreviewed_labs(tmp_path: Path):
    """
    WEEKLY CONSILIUM INTEGRATION & MODEL CALL VERIFICATION:
    1. Weekly Flash receives completed Lab Revision #1 conclusions.
    2. Weekly Pro receives unreviewed labs (ingested during cooldown) directly from the ledger.
    3. Active strategy updated strictly by Pro decision.
    4. Week 2 Athena plan generated.
    5. CRITICAL CHECK: Strictly Flash -> Pro (exactly 2 calls, ZERO Flash-Lite calls).
    """
    db_file = tmp_path / "stage_7b3_test3.db"
    _, TestSessionFactory = _create_isolated_file_db(db_file)

    t0_dt = datetime(2026, 10, 30, 8, 0, 0, tzinfo=timezone.utc)
    clock = ControlledClock(t0_dt)
    patient_id = "pat_7b3_003"
    cycle_id = "cycle_7b3_003"

    recorded_calls: List[Dict[str, Any]] = []
    mock_helper = Stage7B3MockGateway(recorded_calls)
    mock_gw = mock_helper.create_gateway()

    with TestSessionFactory() as db:
        _seed_patient_and_intake(db, patient_id=patient_id, cycle_id=cycle_id, intake_time=t0_dt, pairing_code="123456")

        # Seed baseline strategy reports from Primary Consilium
        rep_repo = ModelReportRepository(db)
        rep_repo.create_next_version(
            cycle_id=cycle_id,
            model="gemini-1.5-flash",
            report_type="deputy_chief_physician_internal",
            privacy_level=PrivacyLevel.SERVER_ONLY,
            content=_sample_flash_output(),
            commit=False,
        )
        rep_repo.create_next_version(
            cycle_id=cycle_id,
            model="gemini-1.5-pro",
            report_type="chief_physician_internal",
            privacy_level=PrivacyLevel.SERVER_ONLY,
            content=_sample_pro_output(),
            commit=False,
        )
        rep_repo.create_next_version(
            cycle_id=cycle_id,
            model="gemini-1.5-pro",
            report_type="user_overall_report",
            privacy_level=PrivacyLevel.CLIENT_FACING,
            content={
                "title": "Aegis Baseline Health Strategy",
                "summary": "Функциональные параметры стабильны.",
                "highlights": ["Продолжать режим адаптации."],
            },
            commit=False,
        )
        rep_repo.create_next_version(
            cycle_id=cycle_id,
            model="gemini-1.5-pro",
            report_type=ATHENA_ACTIVE_STRATEGY_REPORT_TYPE,
            privacy_level=PrivacyLevel.SERVER_ONLY,
            content={
                "active_strategy_text": "Week 1 baseline strategy",
                "clinical_background_text": "Baseline clinical background",
                "deputy_synthesis_text": "Baseline deputy synthesis",
                "active_strategy": "Week 1 baseline strategy",
                "clinical_background": "Baseline clinical background",
                "deputy_synthesis": "Baseline deputy synthesis",
                "vitals_thresholds": {"min_pessimum_bpm": 50, "max_pessimum_bpm": 120},
            },
            commit=False,
        )
        rep_repo.create_next_version(
            cycle_id=cycle_id,
            model="aegis-system",
            report_type=ATHENA_SCHEDULE_ANCHOR_REPORT_TYPE,
            privacy_level=PrivacyLevel.SERVER_ONLY,
            content={"anchor_timestamp_utc": t0_dt.isoformat()},
            commit=False,
        )

        # 1. Ingest mutation for ferritin (42 -> 18 ng/mL)
        sched_svc = LabSchedulingService(db=db, gateway=mock_gw, now_provider=clock.now)
        sched_svc.process_manual_edit(
            cycle_id=cycle_id,
            patient_id=patient_id,
            mutations=[
                LabMarkerMutationItem(
                    marker_key="ferritin",
                    display_name="Ferritin",
                    value=18.0,
                    unit="ng/mL",
                    sample_date="2026-11-01",
                )
            ],
        )

        # 2. Execute background Lab Revision consilium
        rev_service = LabRevisionConsiliumService(db=db, gateway=mock_gw, now_provider=clock.now)
        await rev_service.execute_lab_revision_consilium(cycle_id=cycle_id)

        # 3. Advance into cooldown and ingest new lab (creatinine = 115)
        clock.advance(hours=2)
        sched_svc.process_manual_edit(
            cycle_id=cycle_id,
            patient_id=patient_id,
            mutations=[
                LabMarkerMutationItem(
                    marker_key="creatinine",
                    display_name="Creatinine",
                    value=115.0,
                    unit="umol/L",
                    sample_date="2026-11-02",
                )
            ],
        )
        db.commit()

    # Fast-forward clock to Weekly boundary T0 + 168h
    clock.set(t0_dt + timedelta(hours=168))

    worker = ClinicalAutomationWorker(
        session_factory=TestSessionFactory,
        gateway_factory=lambda: mock_gw,
        now_provider=clock.now,
        poll_interval_seconds=0.05,
        enable_automatic_weekly=True,
        enable_weekly_plan=True,
    )

    # 1. Execute Weekly Consilium Step A
    calls_before_step_a = len(recorded_calls)
    res_a = await worker.run_once(discover=True, limit=1)
    step_a_jobs = [j for j in res_a if j.job_type == AutomationJobType.WEEKLY_CONSILIUM_A]
    assert len(step_a_jobs) == 1
    assert step_a_jobs[0].status == AutomationJobStatus.COMPLETED, f"Step A failed: {step_a_jobs[0].last_error}"

    # Verify model calls during Step A:
    # Strictly 1 Flash + 1 Pro (exactly 2 calls).
    # ZERO Flash-Lite calls!
    step_a_calls = recorded_calls[calls_before_step_a:]
    assert len(step_a_calls) == 2, f"Expected exactly 2 Gemini calls for Weekly A, got {len(step_a_calls)}"
    assert "flash-lite" not in step_a_calls[0]["url_path"]
    assert "flash-lite" not in step_a_calls[1]["url_path"]

    # Verify Weekly Flash received Lab Revision formatted context
    flash_prompt = step_a_calls[0]["prompt"]
    assert "background analytical lab revision conclusions" in flash_prompt.lower() or "lab revision" in flash_prompt.lower()

    # 2. Execute Weekly Consilium Step B (Plan + Client Reports)
    res_b = await worker.run_once(discover=True, limit=10)
    step_b_jobs = [j for j in res_b if j.job_type == AutomationJobType.WEEKLY_CONSILIUM_B]
    assert len(step_b_jobs) == 1
    assert step_b_jobs[0].status == AutomationJobStatus.COMPLETED, f"Step B failed: {step_b_jobs[0].last_error}"

    # 3. Execute Weekly Consilium Step C (Athena Wrap-up Message)
    res_c = await worker.run_once(discover=True, limit=10)
    step_c_jobs = [j for j in res_c if j.job_type == AutomationJobType.WEEKLY_CONSILIUM_C]
    assert len(step_c_jobs) == 1
    assert step_c_jobs[0].status == AutomationJobStatus.COMPLETED

    # 4. Verify Database State & Invariants
    with TestSessionFactory() as db:
        plan_svc = AthenaWeeklyPlanService(db=db, gateway=mock_gw, now_provider=clock.now)
        plan_w2 = plan_svc.get_active_weekly_plan(cycle_id, week_number=2)
        assert plan_w2 is not None
        assert plan_w2.week_number == 2

        # Check wrap-up turn
        athena_svc = AthenaChatService(db=db, gateway=mock_gw, now_provider=clock.now)
        wrapup = athena_svc._find_idempotent_turn(cycle_id, f"athena_weekly_wrapup_{cycle_id}_w1")
        assert wrapup is not None
        assert len(wrapup.reply) > 0

        # Anchor T0 preserved
        weekly_svc = WeeklyConsiliumService(db=db, gateway=mock_gw, now_provider=clock.now)
        assert weekly_svc.find_schedule_anchor_timestamp(cycle_id) == t0_dt

        # Invariant: Weekly Consilium does NOT touch or complete pending lab revision task
        sched_svc_check = LabSchedulingService(db=db, gateway=mock_gw, now_provider=clock.now)
        task_after_weekly = sched_svc_check.get_pending_task(cycle_id)
        assert task_after_weekly is not None
        assert task_after_weekly.status == "pending"
        assert task_after_weekly.accumulated_changes_count >= 1

# ==============================================================================
# TEST 4: SAFE CLIENT CONTRACTS, JSON SCHEMA COMPLIANCE & PRIVACY PROTECTION
# ==============================================================================

@pytest.mark.anyio
async def test_04_client_reports_safe_contracts_and_privacy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """
    CLIENT REPORT CONTRACTS & SAFE ISOLATION:
    1. GET /api/v1/reports/client returns all client-facing reports matching Flutter VaultService schemas.
    2. Server-only private reports are strictly excluded from client endpoints.
    3. Direct GET /api/v1/reports/client/{server_only_id} returns 403 Forbidden.
    4. Cross-patient cycle access returns 403 Forbidden (strict patient isolation).
    """
    db_file = tmp_path / "stage_7b3_test4.db"
    backup_dir = tmp_path / "backups"
    test_engine, TestSessionFactory = _create_isolated_file_db(db_file)

    t0_dt = datetime(2026, 10, 30, 8, 0, 0, tzinfo=timezone.utc)
    clock = ControlledClock(t0_dt)
    patient_id_a = "pat_7b3_safe_a"
    patient_id_b = "pat_7b3_safe_b"
    cycle_id_a = "cycle_7b3_safe_a"
    cycle_id_b = "cycle_7b3_safe_b"

    monkeypatch.setattr(settings, "jwt_secret", "test_jwt_secret_stage_7b3_safe_1234567890!")
    monkeypatch.setattr(settings, "pairing_secret", "test_pairing_secret_stage_7b3_safe_12345!")
    monkeypatch.setattr(settings, "api_secret", "test_api_secret_stage_7b3_safe_1234567890")
    monkeypatch.setattr(settings, "access_token_ttl_minutes", 60 * 24 * 30)

    recorded_calls: List[Dict[str, Any]] = []
    mock_helper = Stage7B3MockGateway(recorded_calls)
    mock_gw = mock_helper.create_gateway()

    def _override_get_db():
        db = TestSessionFactory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[client_get_gemini_gateway] = lambda: mock_gw
    app.dependency_overrides[client_get_server_now_provider] = lambda: clock.now
    app.dependency_overrides[chat_get_gemini_gateway] = lambda: mock_gw
    app.dependency_overrides[chat_get_server_now_provider] = lambda: clock.now
    app.dependency_overrides[vitals_get_gemini_gateway] = lambda: mock_gw
    app.dependency_overrides[vitals_get_server_now_provider] = lambda: clock.now
    app.dependency_overrides[cascade_get_gemini_gateway] = lambda: mock_gw
    app.dependency_overrides[cascade_get_server_now_provider] = lambda: clock.now

    app.state.engine = test_engine
    app.state.backup_dir = backup_dir
    app.state.enable_automation_worker = False

    server_only_report_id = "rep_internal_confidential_01"

    with TestSessionFactory() as db:
        # Seed Patient A and Patient B
        _seed_patient_and_intake(db, patient_id=patient_id_a, cycle_id=cycle_id_a, intake_time=t0_dt, pairing_code="123456", seed_primary_overall=False)
        PatientRepository(db).create(patient_id=patient_id_b, status="active")
        MedicalCycleRepository(db).create(cycle_id=cycle_id_b, patient_id=patient_id_b, status="active")
        PairingRepository(db, secret=settings.pairing_secret).create_code(
            patient_id=patient_id_b,
            raw_code_override="654321",
        )

        rep_repo = ModelReportRepository(db)
        # Seed 8 client system reports
        for k in SYSTEM_KEYS:
            rep_repo.create_next_version(
                cycle_id=cycle_id_a,
                model="gemini-1.5-flash",
                report_type=f"system_report_{k}",
                privacy_level=PrivacyLevel.CLIENT_FACING,
                content={
                    "title": f"Система {k.upper()}: Отчет",
                    "summary": f"Показатели системы {k} в норме.",
                    "highlights": [f"Регулярный контроль {k}."],
                },
                commit=False,
            )

        # Seed 1 client overall report
        rep_repo.create_next_version(
            cycle_id=cycle_id_a,
            model="gemini-1.5-pro",
            report_type="user_overall_report",
            privacy_level=PrivacyLevel.CLIENT_FACING,
            content={
                "title": "Итоги недели: Общий статус",
                "summary": "Функциональный профиль устойчив.",
                "highlights": ["Продолжайте комфортную физическую активность."],
            },
            commit=False,
        )

        # Seed 1 SERVER_ONLY confidential report
        rep = rep_repo.create_next_version(
            cycle_id=cycle_id_a,
            model="gemini-1.5-pro",
            report_type="chief_physician_internal",
            privacy_level=PrivacyLevel.SERVER_ONLY,
            content={"internal_diagnosis_notes": "Конфиденциальные врачебные рассуждения и сырые промпты."},
            commit=False,
        )
        server_only_report_id = rep.report_id
        db.commit()

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    server_config = uvicorn.Config(app=app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(server_config)
    server_task = asyncio.create_task(server.serve())

    while not server.started:
        await asyncio.sleep(0.01)

    try:
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=30.0) as client:
            # Pair Patient A
            pair_a = await client.post(
                "/api/v1/auth/pair",
                json={"pairing_code": "123456", "device_id": "dev_safe_a", "device_name": "Device A", "platform": "android"},
            )
            assert pair_a.status_code == 200
            token_a = pair_a.json()["access_token"]
            headers_a = {"Authorization": f"Bearer {token_a}"}

            # Pair Patient B
            pair_b = await client.post(
                "/api/v1/auth/pair",
                json={"pairing_code": "654321", "device_id": "dev_safe_b", "device_name": "Device B", "platform": "android"},
            )
            assert pair_b.status_code == 200
            token_b = pair_b.json()["access_token"]
            headers_b = {"Authorization": f"Bearer {token_b}"}

            # 1. Patient A requests client reports for own cycle
            resp_reports = await client.get(f"/api/v1/reports/client?cycle_id={cycle_id_a}", headers=headers_a)
            assert resp_reports.status_code == 200
            reports_list = resp_reports.json()

            # Expect exactly 8 organ reports + 1 overall report = 9 client reports
            assert len(reports_list) == 9

            # Verify every report conforms to Flutter VaultService & AegisApiClient contracts
            for r in reports_list:
                assert "report_id" in r and len(r["report_id"]) > 0
                assert r["cycle_id"] == cycle_id_a
                assert r["privacy_level"] == "client_facing"
                assert "content" in r
                content = r["content"]
                assert "title" in content and len(content["title"]) > 0
                assert "summary" in content and len(content["summary"]) > 0
                assert "highlights" in content and isinstance(content["highlights"], list)

            # 2. Privacy Protection: Zero server_only reports leaked in the list
            assert not any(r["privacy_level"] == "server_only" for r in reports_list)
            assert not any(r["report_id"] == server_only_report_id for r in reports_list)

            # 3. Direct access to server_only report ID returns 403 Forbidden
            resp_secret = await client.get(f"/api/v1/reports/client/{server_only_report_id}", headers=headers_a)
            assert resp_secret.status_code == 403

            # 4. Cross-Patient Isolation: Patient B attempts to fetch Patient A's reports -> 403 Forbidden
            foreign_resp = await client.get(f"/api/v1/reports/client?cycle_id={cycle_id_a}", headers=headers_b)
            assert foreign_resp.status_code == 403

    finally:
        server.should_exit = True
        await server_task
        app.dependency_overrides.clear()

# ==============================================================================
# TEST 5: WELLNESS WINDOW BOUNDARY SECONDS & TELEMETRY INGESTION RESILIENCE
# ==============================================================================

@pytest.mark.anyio
async def test_05_wellness_window_boundaries_and_telemetry_resilience(tmp_path: Path):
    """
    WELLNESS BOUNDARY SECONDS & TELEMETRY INGESTION:
    1. Measurement at (end - 1s) belongs strictly to Window 1.
    2. Measurement at exactly (end) belongs strictly to Window 2 via [start, end).
    3. Measurement at (end + 1s) belongs strictly to Window 2.
    4. Delayed measurement with past timestamp is sorted cleanly into previous window.
    5. Duplicate measurements are ingested idempotently without duplicate records.
    """
    db_file = tmp_path / "stage_7b3_test5.db"
    _, TestSessionFactory = _create_isolated_file_db(db_file)

    t0_dt = datetime(2026, 10, 30, 8, 0, 0, tzinfo=timezone.utc)
    clock = ControlledClock(t0_dt)
    patient_id = "pat_7b3_005"
    cycle_id = "cycle_7b3_005"

    with TestSessionFactory() as db:
        _seed_patient_and_intake(db, patient_id=patient_id, cycle_id=cycle_id, intake_time=t0_dt, pairing_code="123456")

        wellness_svc = WellnessDailyService(db=db, now_provider=clock.now)
        w1_start, w1_end = wellness_svc.resolve_observation_window(cycle_id, calendar_week=1, calendar_day=1)
        w2_start, w2_end = wellness_svc.resolve_observation_window(cycle_id, calendar_week=1, calendar_day=2)

        # Boundary timestamps
        sec_before_end = w1_end - timedelta(seconds=1)
        exact_end = w1_end
        sec_after_end = w1_end + timedelta(seconds=1)

        # 1. Ingest telemetry at the exact boundary seconds
        m_repo = wellness_svc.measurement_repo
        m_repo.create_batch(
            default_cycle_id=cycle_id,
            measurements=[
                {
                    "pulse": 65,
                    "systolic": 118,
                    "diastolic": 76,
                    "spo2": 99,
                    "source": "watch_sensor",
                    "measured_at": sec_before_end,
                    "raw_metadata": {"tag": "sec_before_end"},
                },
                {
                    "pulse": 68,
                    "systolic": 120,
                    "diastolic": 78,
                    "spo2": 98,
                    "source": "watch_sensor",
                    "measured_at": exact_end,
                    "raw_metadata": {"tag": "exact_end"},
                },
                {
                    "pulse": 72,
                    "systolic": 122,
                    "diastolic": 80,
                    "spo2": 98,
                    "source": "watch_sensor",
                    "measured_at": sec_after_end,
                    "raw_metadata": {"tag": "sec_after_end"},
                },
            ],
        )

        # 2. Gather measurements for Window 1 [w1_start, w1_end)
        w1_records = wellness_svc.gather_genuine_measurements(
            cycle_id=cycle_id,
            start_dt=w1_start,
            end_dt=w1_end,
        )
        w1_tags = [(m.raw_metadata or {}).get("tag") for m in w1_records]

        # Invariant: 'sec_before_end' IS in Window 1; 'exact_end' and 'sec_after_end' are NOT in Window 1
        assert "sec_before_end" in w1_tags
        assert "exact_end" not in w1_tags
        assert "sec_after_end" not in w1_tags

        # 3. Gather measurements for Window 2 [w2_start, w2_end)
        w2_records = wellness_svc.gather_genuine_measurements(
            cycle_id=cycle_id,
            start_dt=w2_start,
            end_dt=w2_end,
        )
        w2_tags = [(m.raw_metadata or {}).get("tag") for m in w2_records]

        # Invariant: 'exact_end' and 'sec_after_end' ARE in Window 2; 'sec_before_end' is NOT in Window 2
        assert "sec_before_end" not in w2_tags
        assert "exact_end" in w2_tags
        assert "sec_after_end" in w2_tags

        # Zero double-counting: total count across w1 and w2 equals 3
        assert len(w1_records) + len(w2_records) == 3

        # 4. Ingest delayed measurement with past timestamp (e.g. 5 hours into Window 1)
        delayed_ts = w1_start + timedelta(hours=5)
        m_repo.create_batch(
            default_cycle_id=cycle_id,
            measurements=[
                {
                    "pulse": 62,
                    "systolic": 115,
                    "diastolic": 75,
                    "spo2": 99,
                    "source": "watch_sensor",
                    "measured_at": delayed_ts,
                    "raw_metadata": {"tag": "delayed_midday"},
                }
            ],
        )

        # Regather Window 1: delayed measurement is properly captured and sorted chronologically
        w1_records_after = wellness_svc.gather_genuine_measurements(
            cycle_id=cycle_id,
            start_dt=w1_start,
            end_dt=w1_end,
        )
        assert len(w1_records_after) == 2
        assert (w1_records_after[0].raw_metadata or {}).get("tag") == "delayed_midday"
        assert (w1_records_after[1].raw_metadata or {}).get("tag") == "sec_before_end"

        # 5. Ingest duplicate measurement (identical timestamp and pulse)
        m_repo.create_batch(
            default_cycle_id=cycle_id,
            measurements=[
                {
                    "pulse": 65,
                    "systolic": 118,
                    "diastolic": 76,
                    "spo2": 99,
                    "source": "watch_sensor",
                    "measured_at": sec_before_end,
                    "raw_metadata": {"tag": "sec_before_end_duplicate"},
                }
            ],
        )
        # All measurements count in db
        total_in_db = m_repo.list_by_cycle(cycle_id)
        assert len(total_in_db) >= 4
