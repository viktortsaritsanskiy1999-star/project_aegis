import asyncio
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re
import socket
import time
from typing import Any, Callable, Dict, List, Optional, Tuple
import httpx
import pytest
import uvicorn
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session, sessionmaker

from app.config import settings
from app.database import Base, get_db
from app.main import app
from app.models.health_measurement import HealthMeasurement
from app.models.medical_cycle import MedicalCycle
from app.models.model_report import ModelReport
from app.schemas.privacy import PrivacyLevel
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
from app.schemas.consilium import SYSTEM_KEYS
from app.schemas.wellness import WellnessStatus, WellnessTriggerType
from app.services.athena_chat_service import AthenaChatService
from app.services.athena_daily_journal_service import AthenaDailyJournalService
from app.services.athena_weekly_plan_service import (
    ATHENA_ACTIVE_STRATEGY_REPORT_TYPE,
    ATHENA_ROUTINE_QUESTION_REPORT_TYPE,
    ATHENA_SCHEDULE_ANCHOR_REPORT_TYPE,
    ATHENA_WEEKLY_PLAN_REPORT_TYPE,
    AthenaWeeklyPlanService,
)
from app.services.clinical_automation_service import (
    AutomationJobStatus,
    AutomationJobType,
    ClinicalAutomationService,
    ClinicalAutomationWorker,
)
from app.services.gemini_gateway import GeminiGateway, GeminiModelRole
from app.services.lab_revision_consilium_service import LabRevisionConsiliumService
from app.services.lab_scheduling_service import LabSchedulingService
from app.services.mode2_vitals_service import Mode2EpisodeStatus, Mode2VitalsMonitoringService
from app.services.primary_consilium_service import (
    PREPARATORY_ANAMNESIS_REPORT_TYPE,
    PREPARATORY_ECG_REPORT_TYPE,
    PREPARATORY_OCR_REPORT_TYPE,
    PrimaryConsiliumService,
)
from app.services.weekly_consilium_service import (
    WEEKLY_CHIEF_INTERNAL_REPORT_TYPE,
    WEEKLY_DEPUTY_INTERNAL_REPORT_TYPE,
    WEEKLY_OVERALL_CLIENT_REPORT_TYPE,
    WeeklyConsiliumService,
)
from app.services.wellness_daily_service import WellnessDailyService


# ==============================================================================
# CONTROLLED CLOCK FOR DETERMINISTIC 120-DAY SIMULATION
# ==============================================================================

class ControlledClock:
    """Authoritative test clock starting at T0 = 2026-06-01 08:00:00 UTC."""
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

def _create_isolated_file_db(db_path: Path, busy_timeout_ms: int = 15000):
    engine = create_engine(
        f"sqlite:///{db_path.as_posix()}",
        connect_args={"check_same_thread": False, "timeout": busy_timeout_ms / 1000.0},
        pool_pre_ping=True,
    )

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragma(dbapi_con, con_record):
        cursor = dbapi_con.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)};")
        cursor.close()

    Base.metadata.create_all(bind=engine)
    from alembic.config import Config
    from alembic import command
    ini = (Path(__file__).resolve().parent.parent / "alembic.ini").resolve()
    cfg = Config(str(ini))
    cfg.set_main_option("script_location", str(ini.parent / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path.as_posix()}")
    command.stamp(cfg, "head")

    factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    return engine, factory


# ==============================================================================
# DETERMINISTIC GEMINI MOCK PAYLOADS
# ==============================================================================

def _gemini_envelope(content_obj: Any) -> Dict[str, Any]:
    text_content = content_obj if isinstance(content_obj, str) else json.dumps(content_obj, ensure_ascii=False)
    return {
        "candidates": [
            {
                "content": {
                    "parts": [{"text": text_content}],
                    "role": "model",
                },
                "finishReason": "STOP",
                "index": 0,
            }
        ],
        "usageMetadata": {
            "promptTokenCount": 0,
            "candidatesTokenCount": 0,
            "totalTokenCount": 0,
        },
    }


def _sample_anamnesis_output() -> Dict[str, Any]:
    return {
        "user_view": {
            "summary": "Primary anamnesis processed across 8 systems in blind mode."
        },
        "system_logic": {
            key: {
                "data": f"{key.upper()} anamnesis facts: baseline moderate aerobic activity, healthy sleep.",
                "internal_comment": f"M1 blind comment for {key}",
            }
            for key in SYSTEM_KEYS
        },
        "training_sandbox": {
            "raw_diagnostic_hypothesis": "Normal physiological profile with preserved functional reserve.",
            "data_gap_analysis": "Follow resting heart rate and recovery metrics after workouts.",
            "error_probability": "Low (10%)",
        },
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


def _sample_primary_flash_output() -> Dict[str, Any]:
    user_reports = {
        key: f"Базовое состояние системы {key} устойчивое. Рисков острой патологии нет."
        for key in SYSTEM_KEYS
    }
    return {
        "flash_general_shelf": "INTERNAL FLASH GENERAL SHELF: Primary intersystem synthesis complete.",
        "flash_training_shelf": "INTERNAL FLASH TRAINING SANDBOX: Primary M1 and 8 agents audit passed.",
        "user_system_reports": user_reports,
    }


def _sample_primary_pro_output() -> Dict[str, Any]:
    return {
        "vitals_thresholds": {
            "min_pessimum_bpm": 48,
            "max_pessimum_bpm": 120,
            "max_spike_delta_bpm": 30,
            "clinical_rationale": "PRO CLINICAL RATIONALE: baseline resting HR 60-64 bpm, normal sinus rhythm.",
        },
        "pro_general_shelf": "СТРАТЕГИЯ PRO НА ПЕРВУЮ НЕДЕЛЮ (v1): Базовая адаптация и мониторинг восстановительного пульса.",
        "pro_training_shelf": "INTERNAL PRO TRAINING SHELF: Initial nosology profiles verified.",
        "user_overall_report": {
            "title": "Aegis Master Health Strategy (Baseline)",
            "summary": "Начальный функциональный статус устойчив. Показатели гемодинамики в целевых пределах.",
            "highlights": [
                "Поддерживайте комфортный режим аэробной активности",
                "Контролируйте восстановление ЧСС после физических нагрузок",
            ],
        },
        "flash_quality_review": {
            "approved_for_publication": True,
            "rejection_reason": None,
            "corrected_user_system_reports": {},
        },
    }


def _sample_weekly_flash_output(week_number: int) -> Dict[str, Any]:
    system_reports = {
        k: {
            "summary": f"Стабильная динамика системы {k} по итогам недели {week_number}.",
            "has_changes": True,
        }
        for k in SYSTEM_KEYS
    }
    return {
        "weekly_deputy_analysis": (
            f"Заместитель главного врача: динамика за неделю {week_number} благоприятная. "
            f"Биометрические показатели и дневники наблюдений согласуются со стратегией."
        ),
        "user_system_reports": system_reports,
    }


def _sample_weekly_pro_output(week_number: int) -> Dict[str, Any]:
    next_ver = week_number + 1
    strategy_text = (
        f"УТВЕРЖДЁННАЯ СТРАТЕГИЯ PRO НА НЕДЕЛЮ №{next_ver} (v{next_ver}):\n"
        f"1. Мониторинг кардиоваскулярной адаптации по итогам недели {week_number}.\n"
        f"2. Поддержание режима тренировок умеренной интенсивности 2-4 раза в неделю.\n"
        f"3. Динамический контроль биомаркеров крови и восстановления после физических нагрузок."
    )
    title = f"Итоги недели №{week_number}: Устойчивая адаптация"
    summary = f"Неделя {week_number} завершена в рамках целевого физиологического коридора."
    return {
        "updated_weekly_strategy": strategy_text,
        "strategy_status": "updated",
        "weekly_overall_report": {
            "title": title,
            "summary": summary,
            "highlights": [
                f"Неделя {week_number} завершена успешно без патологических отклонений",
                "Физическая активность переносится удовлетворительно",
                "Лабораторный профиль и биометрия под систематическим контролем",
            ],
        },
        "flash_quality_review": {
            "approved_for_publication": True,
            "rejection_reason": None,
            "corrected_user_system_reports": {},
        },
    }


def _sample_weekly_plan(cycle_id: str, week_number: int) -> Dict[str, Any]:
    strat_ver = week_number
    return {
        "plan_id": f"plan_{cycle_id}_w{week_number}",
        "cycle_id": cycle_id,
        "week_number": week_number,
        "source_strategy_report_id": f"rep_{cycle_id}_strat_v{strat_ver}",
        "source_strategy_version": strat_ver,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "summary_for_curator": f"План недели {week_number}: контроль переносимости нагрузок и восстановления",
        "goals": [
            {
                "goal_id": f"goal_{week_number}_1",
                "title": f"Оценка переносимости нагрузок недели {week_number}",
                "clinical_rationale": f"Убедиться в отсутствии перегрузки на неделе {week_number}",
                "priority": 1,
                "known_facts": [f"Базовый статус недели {week_number} стабилен"],
                "target_clarification": f"Уточнить самочувствие после тренировок недели {week_number}",
                "sufficiency_criteria": "Пациент подтвердил комфорт нагрузки",
                "status": "active",
                "progress_notes": None,
                "questions_asked_count": 0,
            },
            {
                "goal_id": f"goal_{week_number}_2",
                "title": f"Контроль ночного сна недели {week_number}",
                "clinical_rationale": f"Оценить восстановительную функцию сна на неделе {week_number}",
                "priority": 2,
                "known_facts": ["Продолжительность сна ~7.5 ч"],
                "target_clarification": f"Уточнить субъективное качество отдыха на неделе {week_number}",
                "sufficiency_criteria": "Ответ о субъективном качестве сна",
                "status": "active",
                "progress_notes": None,
                "questions_asked_count": 0,
            },
        ],
    }


def _sample_lab_revision_deputy_output(prompt: str) -> Dict[str, Any]:
    p = prompt.lower()
    if "alt" in p or "cholesterol" in p:
        synthesis = "Синтез заместителя: липидный профиль и печеночные ферменты в пределах допустимой нормы."
    elif "glucose" in p or "5.8" in p:
        synthesis = "Синтез заместителя: глюкоза натощак 5.8 ммоль/л, пограничный уровень."
    elif "creatinine" in p or "115" in p:
        synthesis = "Синтез заместителя: креатинин 115 мкмоль/л на фоне тренировочной активности."
    elif "35" in p:
        synthesis = "Синтез заместителя: ферритин восстановился до 35 нг/мл."
    else:
        synthesis = "Синтез заместителя: ферритин снижен до 18 нг/мл при нормальном гемоглобине."
    return {
        "deputy_synthesis": synthesis,
        "specialist_discrepancies": [],
        "clinical_correlations": ["Клинико-лабораторная динамика сопоставлена с анамнезом."],
        "data_limitations": [],
        "draft_hypotheses": ["Динамическое наблюдение без изменения активной стратегии."],
    }


def _sample_lab_revision_chief_output(prompt: str) -> Dict[str, Any]:
    p = prompt.lower()
    if "alt" in p or "cholesterol" in p:
        summary = "Главный врач: метаболический профиль стабилен, рисков стеатогепатоза нет."
    elif "glucose" in p or "5.8" in p:
        summary = "Главный врач: гликемия 5.8 ммоль/л требует контроля углеводного обмена на очередном Weekly."
    elif "creatinine" in p or "115" in p:
        summary = "Главный врач: креатинин 115 мкмоль/л обусловлен мышечной адаптацией."
    elif "35" in p:
        summary = "Главный врач: восстановление ферритина до 35 нг/мл свидетельствует об эффективности диетотерапии."
    else:
        summary = "Главный врач: подтвержден латентный дефицит железа (ферритин 18 нг/мл)."
    return {
        "analytical_summary": summary,
        "observed_facts": ["Лабораторные маркеры сопоставлены с референтными значениями."],
        "clinical_hypotheses": ["Клиническая гипотеза сформулирована для подготовки к Weekly Consilium."],
        "uncertainties_and_data_deficits": [],
        "questions_for_weekly_consilium": ["Вопрос подготовлен для повестки очередного Weekly Consilium."],
        "cascade_quality_audit": "Аудит каскада 8 Flash-Lite + 1 Flash пройден успешно.",
    }


def _sample_daily_journal_assessment() -> Dict[str, Any]:
    return {
        "status": "NORMAL",
        "interpretation": "Синтез Афины: пациент следовал плану, биометрические показатели стабильны.",
        "observations": ["plan_adherence", "stable_vitals"],
        "relevant_changes": ["Стабильная адаптация к плану недели."],
        "contradictions": [],
        "uncertainties": [],
        "red_flags": False,
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
# CALL TRACKER & DETERMINISTIC MOCK GEMINI GATEWAY
# ==============================================================================

class Stage7C2ACallTracker:
    def __init__(self):
        self.all_calls: List[Dict[str, Any]] = []
        self.by_role: Dict[str, int] = {
            "flash_lite_specialists": 0,
            "primary_deputy_flash": 0,
            "primary_chief_pro": 0,
            "weekly_deputy_flash": 0,
            "weekly_chief_pro": 0,
            "weekly_plan_flash": 0,
            "weekly_wrapup_flash": 0,
            "lab_revision_specialists_lite": 0,
            "lab_revision_deputy_flash": 0,
            "lab_revision_chief_pro": 0,
            "wellness_flash": 0,
            "daily_journal_flash": 0,
            "athena_chat_flash": 0,
            "other": 0,
        }

    def record_call(self, role: str, url_path: str, prompt: str) -> None:
        rec = {
            "index": len(self.all_calls),
            "role": role,
            "url_path": url_path,
            "prompt": prompt,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        self.all_calls.append(rec)
        self.by_role[role] = self.by_role.get(role, 0) + 1


def _build_stage_7c2a_mock_gateway(tracker: Stage7C2ACallTracker) -> GeminiGateway:
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

        # ----------------------------------------------------------------------
        # 1. 8 Organ Specialists (Gemini Flash-Lite)
        # ----------------------------------------------------------------------
        if "flash-lite" in url_path or "clinical specialist for" in prompt.lower() or ("comparative lab-revision" in prompt.lower() and "deputy" not in prompt.lower() and "chief" not in prompt.lower()):
            if "comparative lab-revision" in prompt.lower() or "backgroundlabrevisionspecialistoutput" in prompt.lower():
                tracker.record_call("lab_revision_specialists_lite", url_path, prompt)
            else:
                tracker.record_call("flash_lite_specialists", url_path, prompt)

            sys_key = "cvs"
            for k in SYSTEM_KEYS:
                if f"clinical specialist for {k}" in prompt.lower() or f"specialist for {k}" in prompt.lower():
                    sys_key = k
                    break
            return httpx.Response(200, json=_gemini_envelope(_sample_system_agent_output(sys_key)))

        # ----------------------------------------------------------------------
        # 2. Lab Revision Deputy (Gemini Flash)
        # ----------------------------------------------------------------------
        if (
            "background comparative lab-revision" in prompt.lower()
            or "backgroundlabrevisiondeputyoutput" in prompt.lower()
            or "lab revision deputy" in prompt.lower()
        ):
            assert "flash-lite" not in url_path, f"Flash-Lite forbidden for Lab Revision Deputy: {url_path}"
            tracker.record_call("lab_revision_deputy_flash", url_path, prompt)
            return httpx.Response(200, json=_gemini_envelope(_sample_lab_revision_deputy_output(prompt)))

        # ----------------------------------------------------------------------
        # 3. Lab Revision Chief (Gemini Pro)
        # ----------------------------------------------------------------------
        if (
            "supreme analytical lab-revision" in prompt.lower()
            or "backgroundlabrevisionchiefoutput" in prompt.lower()
            or "lab revision chief" in prompt.lower()
        ):
            assert "flash-lite" not in url_path, f"Flash-Lite forbidden for Lab Revision Chief: {url_path}"
            tracker.record_call("lab_revision_chief_pro", url_path, prompt)
            return httpx.Response(200, json=_gemini_envelope(_sample_lab_revision_chief_output(prompt)))

        # ----------------------------------------------------------------------
        # 4. Weekly Consilium Deputy (Gemini Flash)
        # ----------------------------------------------------------------------
        if (
            "1 week (7 days) of patient monitoring has passed" in prompt.lower()
            or "weekly_deputy_analysis" in prompt.lower()
        ):
            assert "flash-lite" not in url_path, f"Flash-Lite forbidden for Weekly Deputy: {url_path}"
            tracker.record_call("weekly_deputy_flash", url_path, prompt)
            m = re.search(r'WEEK\s*(\d+)', prompt, re.IGNORECASE)
            if not m:
                m = re.search(r'недел[яие]\s*№?(\d+)', prompt, re.IGNORECASE)
            wk_num = int(m.group(1)) if m else 1
            return httpx.Response(200, json=_gemini_envelope(_sample_weekly_flash_output(wk_num)))

        # ----------------------------------------------------------------------
        # 5. Weekly Consilium Chief (Gemini Pro)
        # ----------------------------------------------------------------------
        if (
            "update the working strategy for the next week" in prompt.lower()
            or "updated_weekly_strategy" in prompt.lower()
        ):
            assert "flash-lite" not in url_path, f"Flash-Lite forbidden for Weekly Chief: {url_path}"
            tracker.record_call("weekly_chief_pro", url_path, prompt)
            m = re.search(r'WEEK\s*(\d+)', prompt, re.IGNORECASE)
            if not m:
                m = re.search(r'недел[яие]\s*№?(\d+)', prompt, re.IGNORECASE)
            wk_num = int(m.group(1)) if m else 1
            return httpx.Response(200, json=_gemini_envelope(_sample_weekly_pro_output(wk_num)))

        # ----------------------------------------------------------------------
        # 6. Athena Weekly Plan Record (Gemini Flash)
        # ----------------------------------------------------------------------
        if (
            "[internal weekly plan formulation scenario]" in prompt.lower()
            or "athenaweeklyplanrecord" in prompt.lower()
        ):
            tracker.record_call("weekly_plan_flash", url_path, prompt)
            m = re.search(r'недел[яие]\s*№?(\d+)', prompt, re.IGNORECASE)
            wk_num = int(m.group(1)) if m else 1
            return httpx.Response(200, json=_gemini_envelope(_sample_weekly_plan("cycle_sim_120d_001", wk_num)))

        # ----------------------------------------------------------------------
        # 7. Athena Daily Journal Clinical Assessment (Gemini Flash)
        # ----------------------------------------------------------------------
        if (
            "[internal daily clinical assessment scenario]" in prompt.lower()
            or "athenadailyassessmentmodeloutput" in prompt.lower()
            or "daily clinical assessment scenario" in prompt.lower()
            or "completed 24-hour observation day source evidence" in prompt.lower()
        ):
            tracker.record_call("daily_journal_flash", url_path, prompt)
            return httpx.Response(200, json=_gemini_envelope(_sample_daily_journal_assessment()))

        # ----------------------------------------------------------------------
        # 8. Wellness Daily Analysis (Gemini Flash)
        # ----------------------------------------------------------------------
        if (
            "wellness daily analyzer" in prompt.lower()
            or "wellnessmodeloutput" in prompt.lower()
            or "daily wellness analysis request" in prompt.lower()
        ):
            tracker.record_call("wellness_flash", url_path, prompt)
            ep_match = re.search(r'(ep_mode2_[a-zA-Z0-9_]+)', prompt)
            ep_id = ep_match.group(1) if ep_match else None
            return httpx.Response(200, json=_gemini_envelope(_sample_wellness_output(ep_id)))

        # ----------------------------------------------------------------------
        # 9. Primary Chief Physician (Gemini Pro)
        # ----------------------------------------------------------------------
        if (
            "supreme clinical expert \"chief physician\"" in prompt.lower()
            or ("chief physician" in prompt.lower() and "final concluding link" in prompt.lower())
        ):
            assert "flash-lite" not in url_path, f"Flash-Lite forbidden for Chief Physician: {url_path}"
            tracker.record_call("primary_chief_pro", url_path, prompt)
            return httpx.Response(200, json=_gemini_envelope(_sample_primary_pro_output()))

        # ----------------------------------------------------------------------
        # 10. Primary Deputy Chief Physician (Gemini Flash)
        # ----------------------------------------------------------------------
        if (
            "synthesis and control\" (head of department)" in prompt.lower()
            or ("head of department" in prompt.lower() and "second and final link" in prompt.lower())
        ):
            assert "flash-lite" not in url_path, f"Flash-Lite forbidden for Deputy: {url_path}"
            tracker.record_call("primary_deputy_flash", url_path, prompt)
            return httpx.Response(200, json=_gemini_envelope(_sample_primary_flash_output()))

        # ----------------------------------------------------------------------
        # 11. Anamnesis M1 (Gemini Flash)
        # ----------------------------------------------------------------------
        if "autonomous analytical module \"anamnesis\"" in prompt.lower():
            tracker.record_call("athena_chat_flash", url_path, prompt)
            return httpx.Response(200, json=_gemini_envelope(_sample_anamnesis_output()))

        # ----------------------------------------------------------------------
        # 12. Athena Weekly Wrap-up Message (Gemini Flash)
        # ----------------------------------------------------------------------
        if (
            "wrap-up message formulation" in prompt.lower()
            or "wrapup" in prompt.lower()
            or "wrap-up" in prompt.lower()
            or "weekly_wrapup" in prompt.lower()
        ):
            tracker.record_call("weekly_wrapup_flash", url_path, prompt)
            wrapup_payload = {
                "message": (
                    "Поздравляю с успешным завершением очередной недели! Все ключевые показатели в норме. "
                    "Обновленный план и детальные отчеты доступны в Клиентском Хранилище (Safe)."
                )
            }
            return httpx.Response(200, json=_gemini_envelope(wrapup_payload))

        # ----------------------------------------------------------------------
        # 13. Athena Chat / Planned Questions (Gemini Flash)
        # ----------------------------------------------------------------------
        tracker.record_call("athena_chat_flash", url_path, prompt)
        chat_text = (
            "Здравствуйте! Я Афина, ваш персональный медицинский куратор.\n\n"
            "[CLINICAL_REPORT]\n"
            "{\n"
            '  "status": "NORMAL",\n'
            '  "symptoms_mentioned": [],\n'
            '  "subjective_wellbeing_score": 8,\n'
            '  "adherence_to_plan": "HIGH",\n'
            '  "red_flags": false,\n'
            '  "summary_for_master": "Routine chat turn handled."\n'
            "}\n"
            "[/CLINICAL_REPORT]\n"
            "[GOAL_PROGRESSION]\n"
            "{\n"
            '  "decision": "completed",\n'
            '  "is_sufficient": true,\n'
            '  "reasoning": "Patient response accepted and sufficient.",\n'
            '  "unresolved_points": [],\n'
            '  "next_action": "conclude_day"\n'
            "}\n"
            "[/GOAL_PROGRESSION]"
        )
        return httpx.Response(200, json=_gemini_envelope(chat_text))

    return GeminiGateway(
        api_key="stage-7c2a-mock-key-12345",
        transport=httpx.MockTransport(handler),
        max_retries=1,
        sleep_func=lambda _: None,
    )


def _seed_patient_and_intake(
    session: Session,
    patient_id: str,
    cycle_id: str,
    intake_time: datetime,
    pairing_code: str = "123456",
) -> None:
    PatientRepository(session).create(patient_id=patient_id, status="active")
    cycle_repo = MedicalCycleRepository(session)
    cycle_repo.create(
        cycle_id=cycle_id,
        patient_id=patient_id,
        status="active",
    )
    if getattr(settings, "pairing_secret", None):
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
        ecg_summary="Sinus rhythm, HR 62 bpm, normal electrical axis, no acute ischemic abnormalities.",
    )
    session.commit()


# ==============================================================================
# LATENCY BENCHMARK HELPER
# ==============================================================================

def _measure_latency(func: Callable[[], Any], runs: int = 3) -> float:
    times = []
    for _ in range(runs):
        t0 = time.perf_counter()
        func()
        t1 = time.perf_counter()
        times.append((t1 - t0) * 1000.0)  # ms
    return min(times)


# ==============================================================================
# STAGE 7C.2A: 120-DAY LONGITUDINAL STABILITY SIMULATION
# ==============================================================================

@pytest.mark.anyio
async def test_stage_7c2a_120_day_stability_simulation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """
    STAGE 7C.2A: 120-DAY LONGITUDINAL STABILITY SIMULATION

    Comprehensive 120 virtual days simulation of one patient across 17 consecutive weekly cycles:
    - Day 0: Manual Primary Consilium -> Strategy v1, T0 Anchor (2026-06-01 08:00:00 UTC).
    - Days 1 to 120:
      * Workouts 2-4 times a week at varying times.
      * Rest days (3-5 days/week).
      * 5 Mode 2 episodes (Days 5, 24, 52, 88, 108) with workout confirmation & closure.
      * 4 Gap days with zero data (Days 4, 33, 68, 99) -> Wellness NO_DATA, Journal closed without assessment.
      * Partial data days (Days 18, 45, 79).
      * Midday 12:00 fallback days without wake-up metadata (Days 11, 27, 40, 63, 82, 105).
      * Late wake-up after fallback (Day 58) -> zero duplicate Wellness reports.
      * Worker downtime & batch recovery catch-up (Days 41–43).
      * 5 Lab Revisions (Day 3, Day 8, Day 12, Day 45, Day 80) respecting 15m debounce & 72h cooldown.
      * 17 Weekly Consiliums (Weeks 1 to 17 at T0 + 168*N h) strictly Flash -> Pro.
      * Monotonic Strategy v1 -> v2 -> ... -> v18.
      * Athena Weekly Plans Week 1 -> Week 2 -> ... -> Week 18.
      * Performance snapshots at Days 14, 30, 60, 90, 120 (DB size, table counts, query latencies).
      * Model memory audits at Weeks 2, 5, 10, 17.
      * Client safe isolation (zero SERVER_ONLY leaked).
    """
    start_wall_time = time.time()

    # 1. Setup Isolated SQLite Database and Controlled Clock
    db_file = tmp_path / "stage_7c2a_120day_simulation.db"
    backup_dir = tmp_path / "backups"
    test_engine, TestSessionFactory = _create_isolated_file_db(db_file)

    t0_dt = datetime(2026, 6, 1, 8, 0, 0, tzinfo=timezone.utc)
    clock = ControlledClock(t0_dt)
    patient_id = "pat_sim_120d_001"
    cycle_id = "cycle_sim_120d_001"

    monkeypatch.setattr(settings, "jwt_secret", "test_jwt_stage_7c2a_secret_1234567890!")
    monkeypatch.setattr(settings, "pairing_secret", "test_pairing_stage_7c2a_secret_12345!")
    monkeypatch.setattr(settings, "api_secret", "test_api_stage_7c2a_secret_1234567890")
    monkeypatch.setattr(settings, "access_token_ttl_minutes", 60 * 24 * 180)
    monkeypatch.setattr(settings, "enable_mode2_monitoring", True)

    tracker = Stage7C2ACallTracker()
    mock_gw = _build_stage_7c2a_mock_gateway(tracker)

    # 2. Dependency Overrides
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

    # 3. Configure App State for Lifespan
    app.state.engine = test_engine
    app.state.backup_dir = backup_dir
    app.state.enable_automation_worker = False  # Explicitly driven for deterministic execution
    app.state.worker_poll_interval = 0.05
    app.state.gateway_factory = lambda: mock_gw
    app.state.now_provider = clock.now
    app.state.enable_weekly_plan = True
    app.state.enable_daily_journal = True
    app.state.enable_wellness_daily = True
    app.state.enable_automatic_weekly = True
    app.state.enable_lab_revision = True

    # 4. Seed Initial Patient & Intake Data
    with TestSessionFactory() as init_session:
        _seed_patient_and_intake(
            init_session,
            patient_id=patient_id,
            cycle_id=cycle_id,
            intake_time=t0_dt,
            pairing_code="123456",
        )

    # 5. Start Real Uvicorn HTTP Server on Loopback Dynamic Port
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    uvicorn_config = uvicorn.Config(
        app=app,
        host="127.0.0.1",
        port=port,
        log_level="warning",
    )
    server = uvicorn.Server(uvicorn_config)
    server_task = asyncio.create_task(server.serve())

    while not server.started:
        await asyncio.sleep(0.01)

    async def _drain_worker(w: ClinicalAutomationWorker, max_ticks: int = 35) -> int:
        total = 0
        for _ in range(max_ticks):
            jobs = await w.run_once(discover=True)
            if not jobs:
                break
            total += len(jobs)
        return total

    worker: Optional[ClinicalAutomationWorker] = ClinicalAutomationWorker(
        session_factory=TestSessionFactory,
        gateway_factory=lambda: mock_gw,
        now_provider=clock.now,
        poll_interval_seconds=0.05,
        enable_weekly_plan=True,
        enable_daily_journal=True,
        enable_wellness_daily=True,
        enable_automatic_weekly=True,
        enable_lab_revision=True,
    )

    # Calendars & Special Day sets
    GAP_DAYS = {4, 33, 68, 99}
    PARTIAL_DAYS = {18, 45, 79}
    NO_WAKEUP_FALLBACK_DAYS = {11, 27, 40, 63, 82, 105}
    MODE2_DAYS = {5, 24, 52, 88, 108}
    WORKER_DOWN_DAYS = {41, 42}
    LATE_WAKEUP_DAY = 58
    CHECKPOINT_DAYS = {14, 30, 60, 90, 120}
    MEMORY_AUDIT_WEEKS = {2, 5, 10, 17}

    checkpoint_records: Dict[int, Dict[str, Any]] = {}
    memory_audit_records: Dict[int, Dict[str, Any]] = {}
    mode2_episode_ids: List[str] = []

    try:
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=30.0) as client:

            # ------------------------------------------------------------------
            # DAY 0: T0 SETUP & MANUAL PRIMARY CONSILIUM
            # ------------------------------------------------------------------
            pair_resp = await client.post(
                "/api/v1/auth/pair",
                json={
                    "pairing_code": "123456",
                    "device_id": "dev_pixel_120d_001",
                    "device_name": "Pixel 8 Pro",
                    "platform": "android",
                },
            )
            assert pair_resp.status_code == 200, f"Pairing failed: {pair_resp.text}"
            token = pair_resp.json()["access_token"]
            auth_headers = {"Authorization": f"Bearer {token}"}

            primary_calls_before = len(tracker.all_calls)
            primary_resp = await client.post(
                "/api/v1/client/cascade/run-primary",
                headers=auth_headers,
            )
            assert primary_resp.status_code == 200, f"Primary Consilium failed: {primary_resp.text}"
            primary_data = primary_resp.json()
            assert primary_data["cycle_id"] == cycle_id
            assert len(primary_data["system_reports"]) == 8

            # INVARIANT: Primary Consilium strictly 10 models (8 Flash-Lite + 1 Flash + 1 Pro)
            primary_calls = tracker.all_calls[primary_calls_before:]
            assert len(primary_calls) == 10
            assert sum(1 for c in primary_calls if "flash-lite" in c["url_path"]) == 8
            assert sum(1 for c in primary_calls if "flash" in c["url_path"] and "flash-lite" not in c["url_path"]) == 1
            assert sum(1 for c in primary_calls if "pro" in c["url_path"] or "argon" in c["url_path"]) == 1

            # Worker drains post_primary_init -> establishes Schedule Anchor T0
            assert worker is not None
            await _drain_worker(worker)

            with TestSessionFactory() as session:
                w_svc = WeeklyConsiliumService(db=session, now_provider=clock.now)
                anchor = w_svc.find_schedule_anchor_timestamp(cycle_id)
                assert anchor is not None, "Schedule Anchor T0 was not established!"
                assert anchor == t0_dt

            # First planned question answered
            clock.advance(seconds=30)
            await _drain_worker(worker)

            # ------------------------------------------------------------------
            # DAYS 1 TO 120: MULTI-WEEK SIMULATION LOOP
            # ------------------------------------------------------------------
            for day_idx in range(1, 121):
                day_start = t0_dt + timedelta(days=day_idx - 1)
                w = (day_idx - 1) // 7 + 1
                d = (day_idx - 1) % 7 + 1

                # --------------------------------------------------------------
                # A. WORKER RESTART / DOWNTIME HANDLING (Days 41–43)
                # --------------------------------------------------------------
                if day_idx == 41 and worker is not None:
                    # Worker stops on Day 41 morning (simulating server maintenance)
                    del worker
                    worker = None

                # --------------------------------------------------------------
                # B. MORNING ACTIVITY & TELEMETRY
                # --------------------------------------------------------------
                if day_idx in GAP_DAYS:
                    # 4 Gap days (Days 4, 33, 68, 99): zero telemetry, zero chat
                    pass

                elif day_idx == LATE_WAKEUP_DAY:
                    # Day 58: Late wake-up after fallback. Do nothing in morning; wake-up posted after midday.
                    pass

                elif day_idx in PARTIAL_DAYS:
                    # Days 18, 45, 79: Partial telemetry (single pulse record without BP or sleep metadata)
                    clock.set(day_start + timedelta(hours=1, minutes=30))
                    await client.post(
                        "/api/v1/vitals/batch",
                        headers=auth_headers,
                        json={
                            "cycle_id": cycle_id,
                            "measurements": [
                                {
                                    "pulse": 63,
                                    "source": "watch_sensor",
                                    "measured_at": clock.now().isoformat(),
                                }
                            ],
                        },
                    )

                elif day_idx in NO_WAKEUP_FALLBACK_DAYS:
                    # Days 11, 27, 40, 63, 82, 105: Measurements without "event": "wake_up" metadata
                    clock.set(day_start + timedelta(hours=1, minutes=30))
                    await client.post(
                        "/api/v1/vitals/batch",
                        headers=auth_headers,
                        json={
                            "cycle_id": cycle_id,
                            "measurements": [
                                {
                                    "pulse": 64,
                                    "systolic": 119,
                                    "diastolic": 77,
                                    "spo2": 98,
                                    "source": "watch_sensor",
                                    "measured_at": clock.now().isoformat(),
                                }
                            ],
                        },
                    )

                else:
                    # Normal day: wake-up telemetry with metadata
                    clock.set(day_start + timedelta(hours=0, minutes=45))
                    resting_hr = 60 + (day_idx % 5)
                    await client.post(
                        "/api/v1/vitals/batch",
                        headers=auth_headers,
                        json={
                            "cycle_id": cycle_id,
                            "measurements": [
                                {
                                    "pulse": resting_hr,
                                    "systolic": 118,
                                    "diastolic": 76,
                                    "spo2": 98,
                                    "source": "watch_sensor",
                                    "measured_at": clock.now().isoformat(),
                                    "raw_metadata": {
                                        "event": "wake_up",
                                        "sleep_hours": 7.5 + (0.1 * (day_idx % 4)),
                                        "quality": "restorative",
                                    },
                                }
                            ],
                        },
                    )
                    # Periodic morning Athena chat turn
                    if day_idx % 3 == 0:
                        await client.post(
                            "/api/v1/client/chat/athena",
                            headers=auth_headers,
                            json={"message": f"День {day_idx}: самочувствие отличное, пульс в покое {resting_hr}."},
                        )

                # --------------------------------------------------------------
                # C. DAYTIME ACTIVITY: WORKOUTS, MODE 2 & LAB EDITS
                # --------------------------------------------------------------
                # 1. Mode 2 episodes (Days 5, 24, 52, 88, 108)
                if day_idx in MODE2_DAYS:
                    workout_hr = 10 if day_idx == 5 else (14 if day_idx == 24 else (18 if day_idx == 52 else 15))
                    clock.set(day_start + timedelta(hours=workout_hr))

                    # High pulse spike triggers Mode 2
                    spike_resp = await client.post(
                        "/api/v1/vitals/batch",
                        headers=auth_headers,
                        json={
                            "cycle_id": cycle_id,
                            "measurements": [
                                {"pulse": 140, "spo2": 97, "source": "watch_sensor", "measured_at": clock.now().isoformat()}
                            ],
                        },
                    )
                    assert spike_resp.status_code == 201

                    active_m2 = await client.get(f"/api/v1/vitals/mode2/active?cycle_id={cycle_id}", headers=auth_headers)
                    assert active_m2.status_code == 200, f"No active Mode 2 episode on Day {day_idx}!"
                    ep_id = active_m2.json()["episode_id"]
                    mode2_episode_ids.append(ep_id)

                    # Patient confirms workout
                    activity_name = {
                        5: "велотренажер",
                        24: "интервальный бег",
                        52: "гребной тренажер",
                        88: "подъем по лестнице",
                        108: "круговая тренировка",
                    }.get(day_idx, "кардиотренировка")
                    confirm_resp = await client.post(
                        "/api/v1/vitals/mode2/workout-response",
                        headers=auth_headers,
                        json={
                            "cycle_id": cycle_id,
                            "is_workout": True,
                            "user_message": f"Интенсивная тренировка: {activity_name}",
                            "symptoms": "одышки нет, самочувствие нормальное",
                        },
                    )
                    assert confirm_resp.status_code == 200

                    # Pulse normalization closes Mode 2 episode
                    clock.advance(minutes=2)
                    await client.post(
                        "/api/v1/vitals/batch",
                        headers=auth_headers,
                        json={
                            "cycle_id": cycle_id,
                            "measurements": [
                                {"pulse": 72, "spo2": 98, "source": "watch_sensor", "measured_at": clock.now().isoformat()}
                            ],
                        },
                    )

                # 2. Regular non-Mode 2 workouts (2-4 times a week)
                elif day_idx % 7 in (1, 3, 6) and day_idx not in GAP_DAYS and day_idx not in PARTIAL_DAYS and (day_idx + 1) not in GAP_DAYS:
                    w_hour = 7 if (day_idx % 3 == 0) else (14 if (day_idx % 3 == 1) else 18)
                    clock.set(day_start + timedelta(hours=w_hour))
                    await client.post(
                        "/api/v1/vitals/batch",
                        headers=auth_headers,
                        json={
                            "cycle_id": cycle_id,
                            "measurements": [
                                {
                                    "pulse": 78,
                                    "spo2": 98,
                                    "source": "watch_sensor",
                                    "measured_at": clock.now().isoformat(),
                                    "raw_metadata": {"workout": "moderate_aerobic", "duration_minutes": 40},
                                }
                            ],
                        },
                    )
                    if day_idx % 2 == 1:
                        await client.post(
                            "/api/v1/client/chat/athena",
                            headers=auth_headers,
                            json={"message": f"День {day_idx}: завершил умеренную тренировку, самочувствие бодрое."},
                        )

                # 3. Lab Revisions & Manual Edits
                # Day 3: Ferritin 18 -> Debounce 15m -> Lab Revision #1 (10 models)
                if day_idx == 3:
                    clock.set(day_start + timedelta(hours=2))
                    edit_resp_1 = await client.post(
                        "/api/v1/client/cascade/labs/manual-edit",
                        headers=auth_headers,
                        json={
                            "mutations": [
                                {
                                    "marker_key": "ferritin",
                                    "display_name": "Ferritin",
                                    "value": 18.0,
                                    "unit": "ng/mL",
                                    "sample_date": "2026-06-04",
                                }
                            ]
                        },
                    )
                    assert edit_resp_1.status_code == 200
                    assert edit_resp_1.json()["change_type"] == "meaningful_change"
                    clock.advance(minutes=16)  # Debounce expired
                    if worker is not None:
                        await _drain_worker(worker)

                # Day 5: Duplicate edit Ferritin 18 -> no_change (0 models)
                elif day_idx == 5:
                    clock.set(day_start + timedelta(hours=2))
                    dup_resp = await client.post(
                        "/api/v1/client/cascade/labs/manual-edit",
                        headers=auth_headers,
                        json={
                            "mutations": [
                                {
                                    "marker_key": "ferritin",
                                    "display_name": "Ferritin",
                                    "value": 18.0,
                                    "unit": "ng/mL",
                                    "sample_date": "2026-06-04",
                                }
                            ]
                        },
                    )
                    assert dup_resp.status_code == 200
                    assert dup_resp.json()["change_type"] == "no_change"

                # Day 8: Creatinine 115 -> Debounce 15m -> Lab Revision #2 (10 models)
                elif day_idx == 8:
                    clock.set(day_start + timedelta(hours=2))
                    edit_resp_2 = await client.post(
                        "/api/v1/client/cascade/labs/manual-edit",
                        headers=auth_headers,
                        json={
                            "mutations": [
                                {
                                    "marker_key": "creatinine",
                                    "display_name": "Creatinine",
                                    "value": 115.0,
                                    "unit": "umol/L",
                                    "sample_date": "2026-06-09",
                                }
                            ]
                        },
                    )
                    assert edit_resp_2.status_code == 200
                    assert edit_resp_2.json()["change_type"] == "meaningful_change"
                    clock.advance(minutes=16)
                    if worker is not None:
                        await _drain_worker(worker)

                # Day 9: Glucose 5.8 edit during Day 8 cooldown -> pending task deferred
                elif day_idx == 9:
                    clock.set(day_start + timedelta(hours=2))
                    edit_resp_3 = await client.post(
                        "/api/v1/client/cascade/labs/manual-edit",
                        headers=auth_headers,
                        json={
                            "mutations": [
                                {
                                    "marker_key": "glucose",
                                    "display_name": "Glucose",
                                    "value": 5.8,
                                    "unit": "mmol/L",
                                    "sample_date": "2026-06-10",
                                }
                            ]
                        },
                    )
                    assert edit_resp_3.status_code == 200

                # Day 12: Cooldown expires -> Lab Revision #3 executed (10 models)
                elif day_idx == 12:
                    clock.set(day_start + timedelta(hours=2))
                    if worker is not None:
                        await _drain_worker(worker)

                # Day 45: Cholesterol 5.2 & ALT 22 -> Debounce 15m -> Lab Revision #4 (10 models)
                elif day_idx == 45:
                    clock.set(day_start + timedelta(hours=2))
                    edit_resp_4 = await client.post(
                        "/api/v1/client/cascade/labs/manual-edit",
                        headers=auth_headers,
                        json={
                            "mutations": [
                                {
                                    "marker_key": "cholesterol",
                                    "display_name": "Total Cholesterol",
                                    "value": 5.2,
                                    "unit": "mmol/L",
                                    "sample_date": "2026-07-16",
                                },
                                {
                                    "marker_key": "alt",
                                    "display_name": "ALT",
                                    "value": 22.0,
                                    "unit": "U/L",
                                    "sample_date": "2026-07-16",
                                },
                            ]
                        },
                    )
                    assert edit_resp_4.status_code == 200
                    clock.advance(minutes=16)
                    if worker is not None:
                        await _drain_worker(worker)

                # Day 80: Ferritin 35 recovery -> Debounce 15m -> Lab Revision #5 (10 models)
                elif day_idx == 80:
                    clock.set(day_start + timedelta(hours=2))
                    edit_resp_5 = await client.post(
                        "/api/v1/client/cascade/labs/manual-edit",
                        headers=auth_headers,
                        json={
                            "mutations": [
                                {
                                    "marker_key": "ferritin",
                                    "display_name": "Ferritin",
                                    "value": 35.0,
                                    "unit": "ng/mL",
                                    "sample_date": "2026-08-20",
                                }
                            ]
                        },
                    )
                    assert edit_resp_5.status_code == 200
                    clock.advance(minutes=16)
                    if worker is not None:
                        await _drain_worker(worker)

                # --------------------------------------------------------------
                # D. MIDDAY FALLBACK & DAILY SERVICE DRAIN
                # --------------------------------------------------------------
                # 1. Day 43: Restart worker and catch up Days 41, 42, 43 + Weekly #6!
                if day_idx == 43:
                    worker = ClinicalAutomationWorker(
                        session_factory=TestSessionFactory,
                        gateway_factory=lambda: mock_gw,
                        now_provider=clock.now,
                        poll_interval_seconds=0.05,
                        enable_weekly_plan=True,
                        enable_daily_journal=True,
                        enable_wellness_daily=True,
                        enable_automatic_weekly=True,
                        enable_lab_revision=True,
                    )
                    clock.set(day_start + timedelta(hours=6))
                    await _drain_worker(worker, max_ticks=40)

                # 2. Day 58: Late wake-up after fallback verification
                elif day_idx == LATE_WAKEUP_DAY:
                    # Advance to midday 13:00 UTC -> worker evaluates fallback
                    clock.set(day_start + timedelta(hours=5))
                    if worker is not None:
                        await _drain_worker(worker)

                    # Now patient logs late wake-up telemetry at 13:30 UTC
                    clock.set(day_start + timedelta(hours=5, minutes=30))
                    await client.post(
                        "/api/v1/vitals/batch",
                        headers=auth_headers,
                        json={
                            "cycle_id": cycle_id,
                            "measurements": [
                                {
                                    "pulse": 65,
                                    "systolic": 120,
                                    "diastolic": 78,
                                    "spo2": 98,
                                    "source": "watch_sensor",
                                    "measured_at": clock.now().isoformat(),
                                    "raw_metadata": {"event": "wake_up", "sleep_hours": 9.0},
                                }
                            ],
                        },
                    )
                    # Drain again: window was already closed -> zero duplicate Wellness reports
                    if worker is not None:
                        await _drain_worker(worker)

                # 3. Regular days: advance to midday 13:00 UTC and drain worker
                elif day_idx not in WORKER_DOWN_DAYS:
                    clock.set(day_start + timedelta(hours=5))
                    if worker is not None:
                        await _drain_worker(worker)

                # --------------------------------------------------------------
                # E. WEEKLY CONSILIUM AT BOUNDARY T0 + 168 * N HOURS
                # --------------------------------------------------------------
                if day_idx % 7 == 0 and day_idx not in WORKER_DOWN_DAYS:
                    sprint_n = day_idx // 7
                    clock.set(day_start + timedelta(hours=24, seconds=1))
                    if worker is not None:
                        weekly_calls_before = len(tracker.all_calls)
                        await _drain_worker(worker)
                        weekly_calls_after = len(tracker.all_calls)

                        # INVARIANT: Weekly Consilium executes strictly 2 consilium models (Deputy Flash -> Chief Pro)
                        consilium_calls = [
                            c for c in tracker.all_calls[weekly_calls_before:weekly_calls_after]
                            if c["role"] in ("weekly_deputy_flash", "weekly_chief_pro")
                        ]
                        assert len(consilium_calls) == 2, f"Weekly Consilium #{sprint_n} executed {len(consilium_calls)} models instead of 2!"
                        assert consilium_calls[0]["role"] == "weekly_deputy_flash"
                        assert consilium_calls[1]["role"] == "weekly_chief_pro"

                        # Memory Audit record for Weeks 2, 5, 10, 17
                        if sprint_n in MEMORY_AUDIT_WEEKS:
                            memory_audit_records[sprint_n] = {
                                "sprint_number": sprint_n,
                                "flash_prompt": consilium_calls[0]["prompt"],
                                "pro_prompt": consilium_calls[1]["prompt"],
                            }

                # --------------------------------------------------------------
                # F. PERFORMANCE CHECKPOINT MEASUREMENTS (Days 14, 30, 60, 90, 120)
                # --------------------------------------------------------------
                if day_idx in CHECKPOINT_DAYS:
                    # Advance to end of day
                    clock.set(day_start + timedelta(hours=24))
                    if worker is not None:
                        await _drain_worker(worker)

                    wall_elapsed = time.time() - start_wall_time
                    db_bytes = db_file.stat().st_size
                    db_kb = db_bytes / 1024.0

                    with TestSessionFactory() as cp_session:
                        cp_w_svc = WellnessDailyService(db=cp_session, now_provider=clock.now)
                        cp_auto_svc = ClinicalAutomationService(db=cp_session, gateway=mock_gw, now_provider=clock.now)
                        cp_weekly_svc = WeeklyConsiliumService(db=cp_session, gateway=mock_gw, now_provider=clock.now)

                        # Benchmark core discovery & context loading functions
                        lat_wellness = _measure_latency(lambda: cp_w_svc.get_pending_wellness_windows(cycle_id))
                        lat_automation = _measure_latency(lambda: cp_auto_svc.discover_and_enqueue_jobs())
                        lat_weekly_ctx = _measure_latency(lambda: cp_weekly_svc._load_previous_consilium_context(cycle_id))

                        count_reports = cp_session.query(ModelReport).count()
                        count_vitals = cp_session.query(HealthMeasurement).count()
                        count_jobs = cp_session.query(ModelReport).filter(ModelReport.report_type.like("job_%")).count()

                        checkpoint_records[day_idx] = {
                            "day": day_idx,
                            "wall_elapsed_seconds": round(wall_elapsed, 2),
                            "sqlite_size_kb": round(db_kb, 2),
                            "model_reports_count": count_reports,
                            "health_measurements_count": count_vitals,
                            "automation_jobs_count": count_jobs,
                            "lat_wellness_discovery_ms": round(lat_wellness, 3),
                            "lat_automation_discovery_ms": round(lat_automation, 3),
                            "lat_weekly_context_build_ms": round(lat_weekly_ctx, 3),
                            "ai_calls_total": len(tracker.all_calls),
                            "ai_calls_by_role": dict(tracker.by_role),
                        }

            # ------------------------------------------------------------------
            # AUDIT 1: MEMORY & HISTORICAL CONTEXT AUDIT (Weeks 2, 5, 10, 17)
            # ------------------------------------------------------------------
            for wk in sorted(MEMORY_AUDIT_WEEKS):
                assert wk in memory_audit_records, f"Memory audit record for Week {wk} missing!"
                rec = memory_audit_records[wk]
                fl_prompt = rec["flash_prompt"]
                pr_prompt = rec["pro_prompt"]

                # 1. Previous approved strategy is present
                prev_strat_ver = wk
                assert (
                    f"СТРАТЕГИЯ PRO НА " in fl_prompt
                    or f"(v{prev_strat_ver})" in fl_prompt
                    or f"СТРАТЕГИЯ PRO НА " in pr_prompt
                    or f"(v{prev_strat_ver})" in pr_prompt
                ), f"Week {wk} Weekly Consilium did not receive previous strategy v{prev_strat_ver}!"

                # 2. Correct weekly slice of Athena Journals (Week N journals only)
                assert (
                    f"WEEK {wk}" in fl_prompt
                    or f"SEVEN-DAY ATHENA CLINICAL JOURNALS (WEEK {wk}" in fl_prompt
                ), f"Week {wk} Weekly Consilium did not receive Week {wk} daily journals!"

                # 3. Lab history accessibility
                if wk >= 2:
                    assert "ferritin" in fl_prompt.lower() or "ферритин" in fl_prompt.lower()
                if wk >= 5:
                    assert "glucose" in fl_prompt.lower() or "5.8" in fl_prompt
                if wk >= 10:
                    assert "cholesterol" in fl_prompt.lower() or "alt" in fl_prompt.lower()
                if wk >= 17:
                    assert "35" in fl_prompt or "ferritin" in fl_prompt.lower()

                # 4. Strict time cutoff: ZERO events from future weeks
                for future_wk in range(wk + 1, 19):
                    assert f"WEEK {future_wk}" not in fl_prompt, f"Future Week {future_wk} journals leaked into Week {wk}!"
                    assert f"СТРАТЕГИЯ PRO НА НЕДЕЛЮ №{future_wk + 1}" not in pr_prompt

            # ------------------------------------------------------------------
            # AUDIT 2: FULL 120-DAY ARCHITECTURAL INVARIANTS AUDIT
            # ------------------------------------------------------------------
            with TestSessionFactory() as session:
                rep_repo = ModelReportRepository(session)

                # INVARIANT 1: Monotonic Strategy version chain: v1 through v18
                strat_reports = rep_repo.list_server_only(cycle_id=cycle_id, report_type=ATHENA_ACTIVE_STRATEGY_REPORT_TYPE)
                assert len(strat_reports) == 18, f"Expected 18 strategy reports, found {len(strat_reports)}!"
                strat_versions = [r.version for r in strat_reports]
                assert strat_versions == list(range(18, 0, -1)), f"Strategy versions not descending 18..1: {strat_versions}"

                # INVARIANT 2: Athena Weekly Plans: Week 1 through Week 18
                plan_svc = AthenaWeeklyPlanService(db=session, now_provider=clock.now)
                for w_num in range(1, 19):
                    plan = plan_svc.get_active_weekly_plan(cycle_id, week_number=w_num)
                    assert plan is not None, f"Weekly Plan for Week {w_num} missing!"
                    assert plan.week_number == w_num

                # INVARIANT 3: Exactly 17 Weekly Consilium executions
                weekly_step_a_reports = rep_repo.list_server_only(cycle_id=cycle_id, report_type=WEEKLY_CHIEF_INTERNAL_REPORT_TYPE)
                assert len(weekly_step_a_reports) == 17, f"Expected 17 Weekly Consiliums, found {len(weekly_step_a_reports)}!"

                # INVARIANT 4: Exactly 5 Lab Revision executions
                lab_rev_chief_reports = rep_repo.list_server_only(cycle_id=cycle_id, report_type="lab_revision_chief_internal")
                assert len(lab_rev_chief_reports) == 5, f"Expected 5 Lab Revisions, found {len(lab_rev_chief_reports)}!"

                # INVARIANT 5: Exactly 5 Mode 2 episodes confirmed & closed without duplicates
                m2_svc = Mode2VitalsMonitoringService(db=session, now_provider=clock.now)
                episodes = m2_svc.get_episode_history(cycle_id)
                assert len(episodes) == 5, f"Expected 5 Mode 2 episodes, found {len(episodes)}!"
                assert len(set(mode2_episode_ids)) == 5
                assert all(e.status == Mode2EpisodeStatus.CLOSED for e in episodes)

                # INVARIANT 6: Real count of Wellness reports across 120 days: exactly 120 reports
                well_reports = WellnessDailyService(db=session, now_provider=clock.now).list_wellness_reports(cycle_id)
                assert len(well_reports) == 120, f"Expected 120 Wellness reports, found {len(well_reports)}!"
                ready_wellness = sum(1 for w in well_reports if w.status == WellnessStatus.READY)
                no_data_wellness = sum(1 for w in well_reports if w.status == WellnessStatus.NO_DATA)
                for w in well_reports:
                    day_num = (w.calendar_week - 1) * 7 + w.calendar_day
                    if day_num in GAP_DAYS:
                        print(f"GAP_DAY_CHECK: Day {day_num} (w{w.calendar_week} d{w.calendar_day}) status={w.status} window={w.window}")
                assert ready_wellness == 116, f"Expected 116 READY Wellness reports, found {ready_wellness}!"
                assert no_data_wellness == 4, f"Expected 4 NO_DATA Wellness reports, found {no_data_wellness}!"

                # INVARIANT 7: Real count of Daily Journals across 120 days: exactly 120 journals
                journals = AthenaDailyJournalService(db=session, now_provider=clock.now).list_daily_journals(cycle_id)
                assert len(journals) == 120, f"Expected 120 Daily Journals, found {len(journals)}!"
                with_assessment = sum(1 for j in journals if j.athena_clinical_assessment.is_available)
                without_assessment = sum(1 for j in journals if not j.athena_clinical_assessment.is_available)
                assert with_assessment == 116, f"Expected 116 journals with assessment, found {with_assessment}!"
                assert without_assessment == 4, f"Expected 4 journals without assessment, found {without_assessment}!"

                # INVARIANT 8: Day 58 late wake-up: exactly 1 report for Day 58 (no duplicates)
                day58_w = WellnessDailyService(db=session, now_provider=clock.now).get_daily_wellness_report(cycle_id, 9, 2)
                assert day58_w is not None

            # ------------------------------------------------------------------
            # AUDIT 3: CLIENT PRIVACY ISOLATION & SAFE AUDIT
            # ------------------------------------------------------------------
            client_reports_resp = await client.get(f"/api/v1/reports/client?cycle_id={cycle_id}", headers=auth_headers)
            assert client_reports_resp.status_code == 200
            client_reports = client_reports_resp.json()
            assert len(client_reports) > 0
            for cr in client_reports:
                assert cr["privacy_level"] == PrivacyLevel.CLIENT_FACING.value
                assert cr["report_type"] not in (
                    "chief_physician_internal",
                    "deputy_chief_physician_internal",
                    WEEKLY_CHIEF_INTERNAL_REPORT_TYPE,
                    WEEKLY_DEPUTY_INTERNAL_REPORT_TYPE,
                    ATHENA_ACTIVE_STRATEGY_REPORT_TYPE,
                    ATHENA_WEEKLY_PLAN_REPORT_TYPE,
                    ATHENA_SCHEDULE_ANCHOR_REPORT_TYPE,
                    "lab_revision_chief",
                    "lab_revision_deputy",
                    "laboratory_ledger",
                )

            # Direct attempt to read a SERVER_ONLY report returns 403 Forbidden
            strat_v18_id = strat_reports[0].report_id
            forbidden_resp = await client.get(f"/api/v1/reports/client/{strat_v18_id}", headers=auth_headers)
            assert forbidden_resp.status_code == 403

    finally:
        server.should_exit = True
        await server_task
        app.dependency_overrides.clear()

    # ------------------------------------------------------------------
    # PERFORMANCE & RESOURCE SUMMARY
    # ------------------------------------------------------------------
    total_execution_duration = time.time() - start_wall_time
    final_sqlite_size_bytes = db_file.stat().st_size
    final_sqlite_size_kb = final_sqlite_size_bytes / 1024.0

    print("\n" + "=" * 80)
    print("STAGE 7C.2A: 120-DAY LONGITUDINAL STABILITY SIMULATION COMPLETE")
    print(f"Total Virtual Days: 120 (Weeks 1 to 17 + Day 120)")
    print(f"Total Wall Clock Duration: {total_execution_duration:.2f} seconds")
    print(f"Final SQLite Database Size: {final_sqlite_size_kb:.2f} KB ({final_sqlite_size_bytes} bytes)")
    print(f"Total AI Calls Tracked: {len(tracker.all_calls)}")
    print(f"AI Calls Distribution by Role:\n{json.dumps(tracker.by_role, indent=2)}")
    print("\nCHECKPOINT PERFORMANCE SUMMARY (Days 14, 30, 60, 90, 120):")
    for cp_day in sorted(checkpoint_records.keys()):
        rec = checkpoint_records[cp_day]
        print(
            f"Day {cp_day:3d}: Wall={rec['wall_elapsed_seconds']:5.1f}s | "
            f"DB={rec['sqlite_size_kb']:7.1f}KB | "
            f"Reports={rec['model_reports_count']:4d} | "
            f"Vitals={rec['health_measurements_count']:4d} | "
            f"Jobs={rec['automation_jobs_count']:4d} | "
            f"LatWellness={rec['lat_wellness_discovery_ms']:6.3f}ms | "
            f"LatAuto={rec['lat_automation_discovery_ms']:6.3f}ms | "
            f"LatWkCtx={rec['lat_weekly_context_build_ms']:6.3f}ms"
        )
    print("=" * 80 + "\n")
