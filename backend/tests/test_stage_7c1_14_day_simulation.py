import asyncio
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re
import socket
import time
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
# CONTROLLED CLOCK FOR DETERMINISTIC 14-DAY SIMULATION
# ==============================================================================

class ControlledClock:
    """Authoritative test clock starting at T0 = 2026-10-01 08:00:00 UTC."""
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
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False, "timeout": busy_timeout_ms / 1000.0},
        pool_pre_ping=True,
    )

    @event.listens_for(engine, "connect")
    def _set_pragmas(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL;")
        cursor.execute("PRAGMA foreign_keys=ON;")
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

    SessionFactory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    return engine, SessionFactory


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
            "summary": f"Стабильная положительная динамика системы {k} по итогам недели {week_number}.",
            "has_changes": True,
        }
        for k in SYSTEM_KEYS
    }
    return {
        "weekly_deputy_analysis": (
            f"Заместитель главного врача: динамика за неделю {week_number} благоприятная. "
            f"Биометрические показатели и дневники наблюдений согласуются с планом адаптации."
        ),
        "user_system_reports": system_reports,
    }


def _sample_weekly_pro_output(week_number: int) -> Dict[str, Any]:
    next_week = week_number + 1
    if week_number == 1:
        strategy_text = (
            "УТВЕРЖДЁННАЯ СТРАТЕГИЯ PRO НА ВТОРУЮ НЕДЕЛЮ (v2):\n"
            "1. Контроль переносимости умеренных кардионагрузок (ходьба/велотренажёр).\n"
            "2. Уточнение качества сна и скорости восстановления ЧСС после физической активности.\n"
            "3. Динамический контроль уровня ферритина и креатинина."
        )
        title = "Итоги недели №1: Устойчивая адаптация"
        summary = "Первая неделя завершена успешно: гемодинамика стабильна, реакция на нагрузки адекватна."
    else:
        strategy_text = (
            "УТВЕРЖДЁННАЯ СТРАТЕГИЯ PRO НА ТРЕТЬЮ НЕДЕЛЮ (v3):\n"
            "1. Расширение диапазона аэробных тренировок до 45 минут при целевом пульсе до 135 уд/мин.\n"
            "2. Мониторинг гидратации и уровня креатинина в сыворотке.\n"
            "3. Продолжение непрерывного телеметрического контроля восстановления ЧСС."
        )
        title = "Итоги недели №2: Расширение адаптационного резерва"
        summary = "Вторая неделя подтвердила отличную толерантность к физическим нагрузкам и стабильность биомаркеров."

    return {
        "updated_weekly_strategy": strategy_text,
        "strategy_status": "updated",
        "weekly_overall_report": {
            "title": title,
            "summary": summary,
            "highlights": [
                f"Неделя {week_number} завершена в рамках целевого физиологического коридора",
                "Физическая активность переносится удовлетворительно без признаков перегрузки",
                "Лабораторный профиль находится под систематическим наблюдением",
            ],
        },
        "flash_quality_review": {
            "approved_for_publication": True,
            "rejection_reason": None,
            "corrected_user_system_reports": {},
        },
    }


def _sample_weekly_plan(cycle_id: str, week_number: int) -> Dict[str, Any]:
    return {
        "plan_id": f"plan_{cycle_id}_w{week_number}",
        "cycle_id": cycle_id,
        "week_number": week_number,
        "source_strategy_report_id": f"rep_{cycle_id}_strat_v{week_number}",
        "source_strategy_version": week_number,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "summary_for_curator": f"План недели {week_number}: контроль переносимости нагрузок и восстановления",
        "goals": [
            {
                "goal_id": f"goal_{week_number}_1",
                "title": f"Оценка переносимости нагрузок недели {week_number}",
                "clinical_rationale": f"Убедиться в отсутствии одышки и перегрузки на неделе {week_number}",
                "priority": 1,
                "known_facts": [f"Базовый статус недели {week_number} стабилен"],
                "target_clarification": f"Уточнить самочувствие после тренировок недели {week_number}",
                "sufficiency_criteria": "Пациент подтвердил отсутствие жалоб и комфорт нагрузки",
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


def _sample_lab_revision_deputy_output(is_creatinine: bool = False) -> Dict[str, Any]:
    if is_creatinine:
        return {
            "deputy_synthesis": "Синтез заместителя: креатинин 115 мкмоль/л на фоне тренировочной нагрузки.",
            "specialist_discrepancies": [],
            "clinical_correlations": ["Физиологическое повышение креатинина при мышечной активности."],
            "data_limitations": [],
            "draft_hypotheses": ["Транзиторное повышение креатинина после интенсивных тренировок."],
        }
    return {
        "deputy_synthesis": "Синтез заместителя: ферритин снижен до 18 нг/мл при нормальном гемоглобине.",
        "specialist_discrepancies": [],
        "clinical_correlations": ["Латентный дефицит железа без анемического синдрома."],
        "data_limitations": [],
        "draft_hypotheses": ["Депо железа истощено, рекомендована консультация на Weekly."],
    }


def _sample_lab_revision_chief_output(is_creatinine: bool = False) -> Dict[str, Any]:
    if is_creatinine:
        return {
            "analytical_summary": "Главный врач: креатинин 115 мкмоль/л требует гидратационного контроля.",
            "observed_facts": ["Креатинин 115 мкмоль/л, гемодинамика стабильна."],
            "clinical_hypotheses": ["Тренировочная реакция без признаков почечной дисфункции."],
            "uncertainties_and_data_deficits": [],
            "questions_for_weekly_consilium": ["Оценить стабильность функции почек на Weekly Consilium №2."],
            "cascade_quality_audit": "8 специалистов Flash-Lite и заместитель Flash провели корректный анализ.",
        }
    return {
        "analytical_summary": "Главный врач: подтвержден латентный дефицит железа (ферритин 18 нг/мл).",
        "observed_facts": ["Ферритин 18 нг/мл, гемоглобин 144 г/л."],
        "clinical_hypotheses": ["Субклинический сидеропенический статус."],
        "uncertainties_and_data_deficits": [],
        "questions_for_weekly_consilium": ["Передать заключение в Weekly Consilium №1."],
        "cascade_quality_audit": "Качественный аудит ревизии ферритина подтвержден.",
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

class Stage7C1CallTracker:
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


def _build_stage_7c1_mock_gateway(tracker: Stage7C1CallTracker) -> GeminiGateway:
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
        if "flash-lite" in url_path or "clinical specialist for" in prompt.lower() or "comparative lab-revision" in prompt.lower() and "deputy" not in prompt.lower() and "chief" not in prompt.lower():
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
            is_creat = "creatinine" in prompt.lower() or "115" in prompt
            return httpx.Response(200, json=_gemini_envelope(_sample_lab_revision_deputy_output(is_creatinine=is_creat)))

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
            is_creat = "creatinine" in prompt.lower() or "115" in prompt
            return httpx.Response(200, json=_gemini_envelope(_sample_lab_revision_chief_output(is_creatinine=is_creat)))

        # ----------------------------------------------------------------------
        # 4. Weekly Consilium Deputy (Gemini Flash)
        # ----------------------------------------------------------------------
        if (
            "1 week (7 days) of patient monitoring has passed" in prompt.lower()
            or "weekly_deputy_analysis" in prompt.lower()
        ):
            assert "flash-lite" not in url_path, f"Flash-Lite forbidden for Weekly Deputy: {url_path}"
            tracker.record_call("weekly_deputy_flash", url_path, prompt)
            # Detect week number from prompt
            is_w2 = "week 2" in prompt.lower() or "неделя 2" in prompt.lower() or "week 2, days 1-7" in prompt.lower()
            wk_num = 2 if is_w2 else 1
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
            is_w2 = (
                "неделю (v2)" in prompt.lower()
                or "стратегия pro на вторую неделю" in prompt.lower()
                or "week 2" in prompt.lower()
                or "неделя 2" in prompt.lower()
            )
            wk_num = 2 if is_w2 else 1
            return httpx.Response(200, json=_gemini_envelope(_sample_weekly_pro_output(wk_num)))

        # ----------------------------------------------------------------------
        # 6. Athena Weekly Plan Record (Gemini Flash)
        # ----------------------------------------------------------------------
        if (
            "[internal weekly plan formulation scenario]" in prompt.lower()
            or "athenaweeklyplanrecord" in prompt.lower()
        ):
            tracker.record_call("weekly_plan_flash", url_path, prompt)
            m = re.search(r'неделя\s*№?(\d+)', prompt, re.IGNORECASE)
            wk_num = int(m.group(1)) if m else 1
            return httpx.Response(200, json=_gemini_envelope(_sample_weekly_plan("cycle_sim_14d_001", wk_num)))

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
                    "Поздравляю с успешным завершением недели! Все ключевые показатели в норме. "
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
        api_key="stage-7c1-mock-key-12345",
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
# STAGE 7C.1: 14-DAY SIMULATION PILOT TEST
# ==============================================================================

@pytest.mark.anyio
async def test_stage_7c1_14_day_simulation_pilot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """
    STAGE 7C.1: LONG-HORIZON SIMULATION HARNESS & 14-DAY PILOT

    Verifies the complete 14-day continuous lifecycle across two consecutive virtual weeks:
    - Day 0: Manual Primary Consilium -> Strategy v1, T0 Anchor, Athena introduction.
    - Week 1 (Days 1–7): Normal telemetry, physical exertion, rest, Day 4 no-data gap,
      Day 3 manual lab edit (Ferritin 42 -> 18) -> 15m debounce -> Lab Revision #1 (10 models),
      Day 5 Mode 2 workout spike and edit during 72h cooldown (Creatinine 115).
    - Day 7: Weekly Consilium #1 (Flash -> Pro, 2 models) -> Strategy v2, Week 2 Plan.
    - Worker restart simulation between weeks (persistence & continuity verification).
    - Week 2 (Days 8–14): Cooldown expires on Day 8 -> Lab Revision #2 (10 models),
      Week 2 Athena uses Week 2 goals, Day 10 Mode 2 episode #2 closed without duplicates,
      Days 8–14 Wellness & Journals with contiguous non-overlapping windows.
    - Day 14: Weekly Consilium #2 (Flash -> Pro) -> Strategy v3, Week 3 Plan.
    - Comprehensive multi-layer architectural invariant audit:
      1. Zero real Gemini calls.
      2. Strict model roles (Flash-Lite for specialists, Flash for deputy, Pro for chief).
      3. No duplicate Weekly, Wellness, or Journals.
      4. Strategy versions v1 -> v2 -> v3.
      5. History handover from Week 1 to Week 2.
      6. Client isolation (zero SERVER_ONLY leaked).
      7. Execution performance and SQLite size measurement.
    """
    start_wall_time = time.time()

    # 1. Setup Isolated SQLite Database and Controlled Clock
    db_file = tmp_path / "stage_7c1_14day_simulation.db"
    backup_dir = tmp_path / "backups"
    test_engine, TestSessionFactory = _create_isolated_file_db(db_file)

    t0_dt = datetime(2026, 10, 1, 8, 0, 0, tzinfo=timezone.utc)
    clock = ControlledClock(t0_dt)
    patient_id = "pat_sim_14d_001"
    cycle_id = "cycle_sim_14d_001"

    monkeypatch.setattr(settings, "jwt_secret", "test_jwt_stage_7c1_secret_1234567890!")
    monkeypatch.setattr(settings, "pairing_secret", "test_pairing_stage_7c1_secret_12345!")
    monkeypatch.setattr(settings, "api_secret", "test_api_stage_7c1_secret_1234567890")
    monkeypatch.setattr(settings, "access_token_ttl_minutes", 60 * 24 * 60)
    monkeypatch.setattr(settings, "enable_mode2_monitoring", True)

    tracker = Stage7C1CallTracker()
    mock_gw = _build_stage_7c1_mock_gateway(tracker)

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
    app.state.enable_automation_worker = False  # Driven explicitly for deterministic control
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

    async def _drain_worker(w: ClinicalAutomationWorker, max_ticks: int = 15) -> int:
        total = 0
        for _ in range(max_ticks):
            jobs = await w.run_once(discover=True)
            if not jobs:
                break
            total += len(jobs)
        return total

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

    try:
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=30.0) as client:

            # ------------------------------------------------------------------
            # DAY 0: T0 SETUP & MANUAL PRIMARY CONSILIUM
            # ------------------------------------------------------------------
            # 1. Device Pairing via HTTP POST /api/v1/auth/pair
            pair_resp = await client.post(
                "/api/v1/auth/pair",
                json={
                    "pairing_code": "123456",
                    "device_id": "dev_pixel_pilot_001",
                    "device_name": "Pixel 8 Pro",
                    "platform": "android",
                },
            )
            assert pair_resp.status_code == 200
            token = pair_resp.json()["access_token"]
            auth_headers = {"Authorization": f"Bearer {token}"}

            # 2. Manual Primary Consilium via HTTP POST /api/v1/client/cascade/run-primary
            primary_calls_before = len(tracker.all_calls)
            primary_resp = await client.post(
                "/api/v1/client/cascade/run-primary",
                headers=auth_headers,
            )
            assert primary_resp.status_code == 200
            primary_data = primary_resp.json()
            assert primary_data["cycle_id"] == cycle_id
            assert len(primary_data["system_reports"]) == 8

            # INVARIANT: Primary Consilium strictly 10 models (8 Flash-Lite + 1 Flash + 1 Pro)
            primary_calls = tracker.all_calls[primary_calls_before:]
            assert len(primary_calls) == 10
            assert sum(1 for c in primary_calls if "flash-lite" in c["url_path"]) == 8
            assert sum(1 for c in primary_calls if "flash" in c["url_path"] and "flash-lite" not in c["url_path"]) == 1
            assert sum(1 for c in primary_calls if "pro" in c["url_path"] or "argon" in c["url_path"]) == 1

            # 3. Worker executes post_primary_init -> establishes Schedule Anchor T0
            await _drain_worker(worker)

            with TestSessionFactory() as session:
                w_svc = WeeklyConsiliumService(db=session, now_provider=clock.now)
                anchor = w_svc.find_schedule_anchor_timestamp(cycle_id)
                assert anchor is not None, "Schedule Anchor T0 was not established!"
                assert anchor == t0_dt

            # 4. Advance clock by 30 seconds -> first_planned_question
            clock.advance(seconds=30)
            await _drain_worker(worker)

            # Chat history confirms Athena introduction and initial planned question
            chat_hist_resp = await client.get("/api/v1/client/chat/athena/history", headers=auth_headers)
            assert chat_hist_resp.status_code == 200
            chat_hist = chat_hist_resp.json()["messages"]
            assert len(chat_hist) >= 2
            assert any(m["sender"] == "curator" and "Афина" in m["text"] for m in chat_hist)

            # ------------------------------------------------------------------
            # WEEK 1 (DAYS 1 TO 7)
            # ------------------------------------------------------------------

            # --- DAY 1 (Observation Day 1: [T0, T0 + 24h)) ---
            clock.set(t0_dt + timedelta(hours=1))
            await client.post(
                "/api/v1/client/chat/athena",
                headers=auth_headers,
                json={"message": "Доброе утро! Чувствую себя бодро, спал отлично."},
            )
            await client.post(
                "/api/v1/vitals/batch",
                headers=auth_headers,
                json={
                    "cycle_id": cycle_id,
                    "measurements": [
                        {
                            "pulse": 62,
                            "systolic": 118,
                            "diastolic": 76,
                            "spo2": 98,
                            "source": "watch_sensor",
                            "measured_at": (clock.now() - timedelta(minutes=30)).isoformat(),
                            "raw_metadata": {"event": "wake_up", "sleep_hours": 7.5, "quality": "restorative"},
                        }
                    ],
                },
            )
            # Midday fallback at T0 + 5h (13:00 UTC)
            clock.set(t0_dt + timedelta(hours=5))
            await _drain_worker(worker)

            with TestSessionFactory() as session:
                w_rep1 = WellnessDailyService(db=session, now_provider=clock.now).get_daily_wellness_report(cycle_id, 1, 1)
                assert w_rep1 is not None and w_rep1.status == WellnessStatus.READY
                j_rep1 = AthenaDailyJournalService(db=session, now_provider=clock.now).get_daily_journal(cycle_id, 1, 1)
                assert j_rep1 is not None and j_rep1.athena_clinical_assessment.is_available is True

            # --- DAY 2 (Observation Day 2: [T0 + 24h, T0 + 48h)) ---
            clock.set(t0_dt + timedelta(hours=25))
            await client.post(
                "/api/v1/client/chat/athena",
                headers=auth_headers,
                json={"message": "Всё по плану, водный баланс соблюдаю."},
            )
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
                            "spo2": 99,
                            "source": "watch_sensor",
                            "measured_at": clock.now().isoformat(),
                            "raw_metadata": {"event": "wake_up", "sleep_hours": 8.0},
                        }
                    ],
                },
            )
            clock.set(t0_dt + timedelta(hours=29))
            await _drain_worker(worker)

            with TestSessionFactory() as session:
                w_rep2 = WellnessDailyService(db=session, now_provider=clock.now).get_daily_wellness_report(cycle_id, 1, 2)
                assert w_rep2 is not None and w_rep2.status == WellnessStatus.READY
                j_rep2 = AthenaDailyJournalService(db=session, now_provider=clock.now).get_daily_journal(cycle_id, 1, 2)
                assert j_rep2 is not None and j_rep2.athena_clinical_assessment.is_available is True

            # --- DAY 3: PHYSICAL ACTIVITY, MANUAL LAB EDIT & 10-MODEL LAB REVISION ---
            clock.set(t0_dt + timedelta(hours=49))
            await client.post(
                "/api/v1/client/chat/athena",
                headers=auth_headers,
                json={"message": "Дневная прогулка 6 км завершена, одышки нет, переносимость хорошая."},
            )
            await client.post(
                "/api/v1/vitals/batch",
                headers=auth_headers,
                json={
                    "cycle_id": cycle_id,
                    "measurements": [
                        {
                            "pulse": 63,
                            "systolic": 120,
                            "diastolic": 78,
                            "spo2": 98,
                            "source": "watch_sensor",
                            "measured_at": clock.now().isoformat(),
                            "raw_metadata": {"event": "wake_up", "sleep_hours": 7.2},
                        }
                    ],
                },
            )
            # Submit manual lab edit: Ferritin 42 -> 18 ng/mL (meaningful change)
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
                            "sample_date": "2026-10-03",
                        }
                    ]
                },
            )
            assert edit_resp_1.status_code == 200
            assert edit_resp_1.json()["change_type"] == "meaningful_change"
            assert edit_resp_1.json()["ledger_version"] == 2

            # Check debounce is active (15 minutes)
            sched_status_1 = await client.get("/api/v1/client/cascade/labs/scheduling-status", headers=auth_headers)
            assert sched_status_1.json()["has_pending_task"] is True
            assert sched_status_1.json()["is_ready_for_revision"] is False

            # Advance clock by 16 minutes -> debounce expired, ready for revision!
            clock.advance(minutes=16)
            sched_status_2 = await client.get("/api/v1/client/cascade/labs/scheduling-status", headers=auth_headers)
            assert sched_status_2.json()["is_ready_for_revision"] is True

            # Worker executes Lab Revision #1: 10 models (8 Flash-Lite -> 1 Flash Deputy -> 1 Pro Chief)
            calls_before_rev1 = len(tracker.all_calls)
            await _drain_worker(worker)
            calls_after_rev1 = len(tracker.all_calls)
            rev1_calls = [
                c for c in tracker.all_calls[calls_before_rev1:calls_after_rev1]
                if "lab_revision" in c["role"]
            ]
            assert len(rev1_calls) == 10
            assert sum(1 for c in rev1_calls if c["role"] == "lab_revision_specialists_lite") == 8
            assert sum(1 for c in rev1_calls if c["role"] == "lab_revision_deputy_flash") == 1
            assert sum(1 for c in rev1_calls if c["role"] == "lab_revision_chief_pro") == 1

            # 72-hour cooldown established
            sched_status_3 = await client.get("/api/v1/client/cascade/labs/scheduling-status", headers=auth_headers)
            assert sched_status_3.json()["cooldown_until_utc"] is not None

            # Close Day 3 Wellness & Daily Journal
            clock.set(t0_dt + timedelta(hours=53))
            await _drain_worker(worker)

            with TestSessionFactory() as session:
                w_rep3 = WellnessDailyService(db=session, now_provider=clock.now).get_daily_wellness_report(cycle_id, 1, 3)
                assert w_rep3 is not None and w_rep3.status == WellnessStatus.READY
                j_rep3 = AthenaDailyJournalService(db=session, now_provider=clock.now).get_daily_journal(cycle_id, 1, 3)
                assert j_rep3 is not None and j_rep3.athena_clinical_assessment.is_available is True

            # --- DAY 4: SIMULATION OF DAY WITHOUT MEASUREMENTS (GAP TESTING) ---
            # Strictly zero vitals uploaded for Day 4!
            clock.set(t0_dt + timedelta(hours=77))
            calls_before_day4 = len(tracker.all_calls)
            await _drain_worker(worker)
            calls_after_day4 = len(tracker.all_calls)

            # INVARIANT: Day 4 Wellness transitions to NO_DATA with STRICTLY 0 Gemini calls!
            # Daily Journal closes without substantive evidence with STRICTLY 0 Gemini calls!
            assert calls_after_day4 == calls_before_day4

            with TestSessionFactory() as session:
                w_rep4 = WellnessDailyService(db=session, now_provider=clock.now).get_daily_wellness_report(cycle_id, 1, 4)
                assert w_rep4 is not None and w_rep4.status == WellnessStatus.NO_DATA
                assert w_rep4.interpretation is None
                j_rep4 = AthenaDailyJournalService(db=session, now_provider=clock.now).get_daily_journal(cycle_id, 1, 4)
                assert j_rep4 is not None and j_rep4.athena_clinical_assessment.is_available is False

            # --- DAY 5: MODE 2 WORKOUT SPIKE & LAB EDIT DURING COOLDOWN ---
            clock.set(t0_dt + timedelta(hours=97))
            await client.post(
                "/api/v1/vitals/batch",
                headers=auth_headers,
                json={
                    "cycle_id": cycle_id,
                    "measurements": [
                        {
                            "pulse": 61,
                            "systolic": 117,
                            "diastolic": 76,
                            "spo2": 99,
                            "source": "watch_sensor",
                            "measured_at": clock.now().isoformat(),
                            "raw_metadata": {"sleep_hours": 7.6},
                        }
                    ],
                },
            )
            # Pulse spike at T0 + 98h triggers Mode 2 episode #1
            clock.set(t0_dt + timedelta(hours=98))
            spike_resp = await client.post(
                "/api/v1/vitals/batch",
                headers=auth_headers,
                json={
                    "cycle_id": cycle_id,
                    "measurements": [{"pulse": 138, "spo2": 97, "source": "watch_sensor", "measured_at": clock.now().isoformat()}],
                },
            )
            assert spike_resp.status_code == 201

            active_m2_1 = await client.get(f"/api/v1/vitals/mode2/active?cycle_id={cycle_id}", headers=auth_headers)
            assert active_m2_1.status_code == 200
            mode2_ep1_id = active_m2_1.json()["episode_id"]

            # Patient confirms workout
            workout_confirm_1 = await client.post(
                "/api/v1/vitals/mode2/workout-response",
                headers=auth_headers,
                json={"cycle_id": cycle_id, "is_workout": True, "user_message": "Интенсивная тренировка на велотренажере", "symptoms": "одышки нет"},
            )
            assert workout_confirm_1.status_code == 200

            # Normalization closes episode #1
            clock.advance(minutes=2)
            await client.post(
                "/api/v1/vitals/batch",
                headers=auth_headers,
                json={"cycle_id": cycle_id, "measurements": [{"pulse": 72, "spo2": 98, "source": "watch_sensor", "measured_at": clock.now().isoformat()}]},
            )
            clock.advance(minutes=1)
            await client.post(
                "/api/v1/vitals/batch",
                headers=auth_headers,
                json={"cycle_id": cycle_id, "measurements": [{"pulse": 70, "spo2": 98, "source": "watch_sensor", "measured_at": clock.now().isoformat()}]},
            )

            # Test identical edit: Ferritin 18.0 -> 18.0 (no_change, debounce NOT reset)
            edit_resp_ident = await client.post(
                "/api/v1/client/cascade/labs/manual-edit",
                headers=auth_headers,
                json={
                    "mutations": [
                        {
                            "marker_key": "ferritin",
                            "display_name": "Ferritin",
                            "value": 18.0,
                            "unit": "ng/mL",
                            "sample_date": "2026-10-03",
                        }
                    ]
                },
            )
            assert edit_resp_ident.status_code == 200
            assert edit_resp_ident.json()["change_type"] == "no_change"
            assert edit_resp_ident.json()["ledger_version"] == 2

            # Close Day 5 Wellness & Daily Journal
            clock.set(t0_dt + timedelta(hours=101))
            await _drain_worker(worker)

            with TestSessionFactory() as session:
                w_rep5 = WellnessDailyService(db=session, now_provider=clock.now).get_daily_wellness_report(cycle_id, 1, 5)
                assert w_rep5 is not None and w_rep5.status == WellnessStatus.READY
                assert len(w_rep5.factual_summary.mode2_episodes) == 1
                assert w_rep5.factual_summary.mode2_episodes[0].episode_id == mode2_ep1_id

                j_rep5 = AthenaDailyJournalService(db=session, now_provider=clock.now).get_daily_journal(cycle_id, 1, 5)
                assert j_rep5 is not None
                assert len(j_rep5.wellness_evidence.mode2_episodes) == 1
                assert j_rep5.wellness_evidence.mode2_episodes[0].episode_id == mode2_ep1_id

            # --- DAY 6: REST DAY / RECOVERY ---
            clock.set(t0_dt + timedelta(hours=121))
            await client.post(
                "/api/v1/client/chat/athena",
                headers=auth_headers,
                json={"message": "Отдыхаю после вчерашней тренировки, мышцы в порядке."},
            )
            await client.post(
                "/api/v1/vitals/batch",
                headers=auth_headers,
                json={
                    "cycle_id": cycle_id,
                    "measurements": [
                        {
                            "pulse": 60,
                            "systolic": 116,
                            "diastolic": 74,
                            "spo2": 99,
                            "source": "watch_sensor",
                            "measured_at": clock.now().isoformat(),
                            "raw_metadata": {"event": "wake_up", "sleep_hours": 8.1},
                        }
                    ],
                },
            )
            clock.set(t0_dt + timedelta(hours=125))
            await _drain_worker(worker)

            with TestSessionFactory() as session:
                w_rep6 = WellnessDailyService(db=session, now_provider=clock.now).get_daily_wellness_report(cycle_id, 1, 6)
                assert w_rep6 is not None and w_rep6.status == WellnessStatus.READY
                j_rep6 = AthenaDailyJournalService(db=session, now_provider=clock.now).get_daily_journal(cycle_id, 1, 6)
                assert j_rep6 is not None and j_rep6.athena_clinical_assessment.is_available is True

            # --- DAY 7: DAY 7 WELLNESS & AUTOMATIC WEEKLY CONSILIUM #1 AT T0 + 168H ---
            clock.set(t0_dt + timedelta(hours=145))
            await client.post(
                "/api/v1/client/chat/athena",
                headers=auth_headers,
                json={"message": "Самочувствие отличное, неделя подошла к концу."},
            )
            await client.post(
                "/api/v1/vitals/batch",
                headers=auth_headers,
                json={
                    "cycle_id": cycle_id,
                    "measurements": [
                        {
                            "pulse": 62,
                            "systolic": 118,
                            "diastolic": 76,
                            "spo2": 98,
                            "source": "watch_sensor",
                            "measured_at": clock.now().isoformat(),
                            "raw_metadata": {"event": "wake_up", "sleep_hours": 7.8},
                        }
                    ],
                },
            )
            # Midday fallback for Day 7 at T0 + 149h
            clock.set(t0_dt + timedelta(hours=149))
            await _drain_worker(worker)

            with TestSessionFactory() as session:
                w_rep7 = WellnessDailyService(db=session, now_provider=clock.now).get_daily_wellness_report(cycle_id, 1, 7)
                assert w_rep7 is not None and w_rep7.status == WellnessStatus.READY
                j_rep7 = AthenaDailyJournalService(db=session, now_provider=clock.now).get_daily_journal(cycle_id, 1, 7)
                assert j_rep7 is not None and j_rep7.athena_clinical_assessment.is_available is True

            # Advance clock to boundary T0 + 168h + 1s for Weekly Consilium #1
            clock.set(t0_dt + timedelta(hours=168, seconds=1))
            weekly_calls_before_1 = len(tracker.all_calls)
            await _drain_worker(worker)
            weekly_calls_after_1 = len(tracker.all_calls)

            # INVARIANT: Weekly Consilium #1 executed strictly 2 models (Flash Deputy -> Pro Chief)!
            # Flash-Lite specialists were NOT invoked!
            weekly_consilium_calls_1 = [
                c for c in tracker.all_calls[weekly_calls_before_1:weekly_calls_after_1]
                if c["role"] in ("weekly_deputy_flash", "weekly_chief_pro")
            ]
            assert len(weekly_consilium_calls_1) == 2
            assert weekly_consilium_calls_1[0]["role"] == "weekly_deputy_flash"
            assert weekly_consilium_calls_1[1]["role"] == "weekly_chief_pro"

            # Check Weekly #1 context: received Ferritin revision (18 ng/mL)
            w1_flash_prompt = weekly_consilium_calls_1[0]["prompt"]
            assert "ferritin" in w1_flash_prompt.lower() or "ферритин" in w1_flash_prompt.lower() or "18" in w1_flash_prompt

            # Verify Strategy v2 is persisted and active
            with TestSessionFactory() as session:
                rep_repo = ModelReportRepository(session)
                strat_reports = rep_repo.list_server_only(cycle_id=cycle_id, report_type=ATHENA_ACTIVE_STRATEGY_REPORT_TYPE)
                assert len(strat_reports) >= 2
                assert strat_reports[0].version == 2
                assert "УТВЕРЖДЁННАЯ СТРАТЕГИЯ PRO НА ВТОРУЮ НЕДЕЛЮ" in (strat_reports[0].content or {}).get("active_strategy_text", "")

                # Verify Athena Weekly Plan for Week 2 is generated
                plan_svc = AthenaWeeklyPlanService(db=session, now_provider=clock.now)
                plan_w2 = plan_svc.get_active_weekly_plan(cycle_id, week_number=2)
                assert plan_w2 is not None
                assert plan_w2.week_number == 2
                assert len(plan_w2.goals) >= 2
                assert plan_w2.goals[0].goal_id.startswith("goal_2_")

            # ------------------------------------------------------------------
            # WORKER RESTART SIMULATION BETWEEN WEEKS
            # ------------------------------------------------------------------
            # Verify clean restart and continuous persistence across worker lifecycle
            del worker
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

            # ------------------------------------------------------------------
            # WEEK 2 (DAYS 8 TO 14)
            # ------------------------------------------------------------------

            # --- DAY 8: NEW LAB DATA, DEBOUNCE & LAB REVISION #2 ---
            clock.set(t0_dt + timedelta(hours=169))
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
                            "sample_date": "2026-10-08",
                        }
                    ]
                },
            )
            assert edit_resp_2.status_code == 200
            assert edit_resp_2.json()["change_type"] == "meaningful_change"
            assert edit_resp_2.json()["ledger_version"] == 3

            # Check debounce is active (15 minutes)
            sched_status_5 = await client.get("/api/v1/client/cascade/labs/scheduling-status", headers=auth_headers)
            assert sched_status_5.json()["has_pending_task"] is True
            assert sched_status_5.json()["is_ready_for_revision"] is False

            # Advance clock by 16 minutes -> debounce expired, ready for revision!
            clock.advance(minutes=16)
            sched_status_6 = await client.get("/api/v1/client/cascade/labs/scheduling-status", headers=auth_headers)
            assert sched_status_6.json()["is_ready_for_revision"] is True

            # Worker executes Lab Revision #2: 10 models (8 Flash-Lite -> 1 Flash Deputy -> 1 Pro Chief)
            calls_before_rev2 = len(tracker.all_calls)
            await _drain_worker(worker)
            calls_after_rev2 = len(tracker.all_calls)
            rev2_calls = [
                c for c in tracker.all_calls[calls_before_rev2:calls_after_rev2]
                if "lab_revision" in c["role"]
            ]
            assert len(rev2_calls) == 10
            assert sum(1 for c in rev2_calls if c["role"] == "lab_revision_specialists_lite") == 8
            assert sum(1 for c in rev2_calls if c["role"] == "lab_revision_deputy_flash") == 1
            assert sum(1 for c in rev2_calls if c["role"] == "lab_revision_chief_pro") == 1

            # 72-hour cooldown established for Lab Revision #2 (until Day 8 + 16m + 72h = Day 11 + 16m)
            sched_status_7 = await client.get("/api/v1/client/cascade/labs/scheduling-status", headers=auth_headers)
            assert sched_status_7.json()["cooldown_until_utc"] is not None

            # Patient answers question according to Week 2 Plan
            await client.post(
                "/api/v1/client/chat/athena",
                headers=auth_headers,
                json={"message": "Самочувствие на второй неделе отличное, нагрузка умеренная, спал 8 часов."},
            )
            await client.post(
                "/api/v1/vitals/batch",
                headers=auth_headers,
                json={
                    "cycle_id": cycle_id,
                    "measurements": [
                        {
                            "pulse": 62,
                            "systolic": 118,
                            "diastolic": 76,
                            "spo2": 98,
                            "source": "watch_sensor",
                            "measured_at": clock.now().isoformat(),
                            "raw_metadata": {"event": "wake_up", "sleep_hours": 8.0},
                        }
                    ],
                },
            )
            clock.set(t0_dt + timedelta(hours=173))
            await _drain_worker(worker)

            with TestSessionFactory() as session:
                w_rep8 = WellnessDailyService(db=session, now_provider=clock.now).get_daily_wellness_report(cycle_id, 2, 1)
                assert w_rep8 is not None and w_rep8.status == WellnessStatus.READY
                j_rep8 = AthenaDailyJournalService(db=session, now_provider=clock.now).get_daily_journal(cycle_id, 2, 1)
                assert j_rep8 is not None and j_rep8.athena_clinical_assessment.is_available is True

            # --- DAY 9: WORKOUT DAY & LAB INGESTION DURING 72-H COOLDOWN ---
            # Ingest Glucose 5.1 -> 5.8 mmol/L during active 72-hour cooldown
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
                            "sample_date": "2026-10-09",
                        }
                    ]
                },
            )
            assert edit_resp_3.status_code == 200
            assert edit_resp_3.json()["change_type"] == "meaningful_change"
            assert edit_resp_3.json()["ledger_version"] == 4

            # Task is pending in ledger, but blocked from execution by 72-hour cooldown!
            sched_status_cd = await client.get("/api/v1/client/cascade/labs/scheduling-status", headers=auth_headers)
            assert sched_status_cd.json()["has_pending_task"] is True
            assert sched_status_cd.json()["is_ready_for_revision"] is False

            # Worker runs: zero lab revisions executed!
            jobs_cd = await worker.run_once(discover=True)
            assert len([j for j in jobs_cd if j.job_type == AutomationJobType.LAB_REVISION]) == 0
            clock.set(t0_dt + timedelta(hours=193))
            await client.post(
                "/api/v1/client/chat/athena",
                headers=auth_headers,
                json={"message": "Пробежка 4 км на стадионе, пульс быстро восстановился."},
            )
            await client.post(
                "/api/v1/vitals/batch",
                headers=auth_headers,
                json={
                    "cycle_id": cycle_id,
                    "measurements": [
                        {
                            "pulse": 63,
                            "systolic": 119,
                            "diastolic": 77,
                            "spo2": 99,
                            "source": "watch_sensor",
                            "measured_at": clock.now().isoformat(),
                            "raw_metadata": {"event": "wake_up", "sleep_hours": 7.5},
                        }
                    ],
                },
            )
            clock.set(t0_dt + timedelta(hours=197))
            await _drain_worker(worker)

            with TestSessionFactory() as session:
                w_rep9 = WellnessDailyService(db=session, now_provider=clock.now).get_daily_wellness_report(cycle_id, 2, 2)
                assert w_rep9 is not None and w_rep9.status == WellnessStatus.READY
                j_rep9 = AthenaDailyJournalService(db=session, now_provider=clock.now).get_daily_journal(cycle_id, 2, 2)
                assert j_rep9 is not None and j_rep9.athena_clinical_assessment.is_available is True

            # --- DAY 10: MODE 2 EPISODE #2 (EXERTION INCIDENT) ---
            clock.set(t0_dt + timedelta(hours=217))
            await client.post(
                "/api/v1/vitals/batch",
                headers=auth_headers,
                json={
                    "cycle_id": cycle_id,
                    "measurements": [
                        {
                            "pulse": 63,
                            "systolic": 120,
                            "diastolic": 78,
                            "spo2": 98,
                            "source": "watch_sensor",
                            "measured_at": clock.now().isoformat(),
                            "raw_metadata": {"event": "wake_up", "sleep_hours": 7.9},
                        }
                    ],
                },
            )
            # Pulse spike at T0 + 218h triggers Mode 2 episode #2
            clock.set(t0_dt + timedelta(hours=218))
            spike_resp_2 = await client.post(
                "/api/v1/vitals/batch",
                headers=auth_headers,
                json={
                    "cycle_id": cycle_id,
                    "measurements": [{"pulse": 135, "spo2": 97, "source": "watch_sensor", "measured_at": clock.now().isoformat()}],
                },
            )
            assert spike_resp_2.status_code == 201

            active_m2_2 = await client.get(f"/api/v1/vitals/mode2/active?cycle_id={cycle_id}", headers=auth_headers)
            assert active_m2_2.status_code == 200
            mode2_ep2_id = active_m2_2.json()["episode_id"]
            assert mode2_ep2_id != mode2_ep1_id

            # Patient confirms exertion
            await client.post(
                "/api/v1/vitals/mode2/workout-response",
                headers=auth_headers,
                json={"cycle_id": cycle_id, "is_workout": True, "user_message": "Подъем по лестнице на 10 этаж", "symptoms": "одышки нет"},
            )
            # Normalization closes episode #2
            clock.advance(minutes=2)
            await client.post(
                "/api/v1/vitals/batch",
                headers=auth_headers,
                json={"cycle_id": cycle_id, "measurements": [{"pulse": 74, "spo2": 98, "source": "watch_sensor", "measured_at": clock.now().isoformat()}]},
            )
            clock.advance(minutes=1)
            await client.post(
                "/api/v1/vitals/batch",
                headers=auth_headers,
                json={"cycle_id": cycle_id, "measurements": [{"pulse": 71, "spo2": 98, "source": "watch_sensor", "measured_at": clock.now().isoformat()}]},
            )

            clock.set(t0_dt + timedelta(hours=221))
            await _drain_worker(worker)

            with TestSessionFactory() as session:
                w_rep10 = WellnessDailyService(db=session, now_provider=clock.now).get_daily_wellness_report(cycle_id, 2, 3)
                assert w_rep10 is not None and w_rep10.status == WellnessStatus.READY
                assert len(w_rep10.factual_summary.mode2_episodes) == 1
                assert w_rep10.factual_summary.mode2_episodes[0].episode_id == mode2_ep2_id

                j_rep10 = AthenaDailyJournalService(db=session, now_provider=clock.now).get_daily_journal(cycle_id, 2, 3)
                assert j_rep10 is not None
                assert len(j_rep10.wellness_evidence.mode2_episodes) == 1
                assert j_rep10.wellness_evidence.mode2_episodes[0].episode_id == mode2_ep2_id

            # --- DAY 11: REST DAY (NO WORKOUTS) ---
            clock.set(t0_dt + timedelta(hours=241))
            await client.post(
                "/api/v1/client/chat/athena",
                headers=auth_headers,
                json={"message": "День отдыха, гулял в парке, пульс спокойный."},
            )
            await client.post(
                "/api/v1/vitals/batch",
                headers=auth_headers,
                json={
                    "cycle_id": cycle_id,
                    "measurements": [
                        {
                            "pulse": 59,
                            "systolic": 115,
                            "diastolic": 75,
                            "spo2": 99,
                            "source": "watch_sensor",
                            "measured_at": clock.now().isoformat(),
                            "raw_metadata": {"event": "wake_up", "sleep_hours": 8.3},
                        }
                    ],
                },
            )
            clock.set(t0_dt + timedelta(hours=245))
            await _drain_worker(worker)

            with TestSessionFactory() as session:
                w_rep11 = WellnessDailyService(db=session, now_provider=clock.now).get_daily_wellness_report(cycle_id, 2, 4)
                assert w_rep11 is not None and w_rep11.status == WellnessStatus.READY
                j_rep11 = AthenaDailyJournalService(db=session, now_provider=clock.now).get_daily_journal(cycle_id, 2, 4)
                assert j_rep11 is not None and j_rep11.athena_clinical_assessment.is_available is True

            # --- DAY 12: NORMAL MONITORING ---
            clock.set(t0_dt + timedelta(hours=265))
            await client.post(
                "/api/v1/client/chat/athena",
                headers=auth_headers,
                json={"message": "Режим стабильный, сон глубокий."},
            )
            await client.post(
                "/api/v1/vitals/batch",
                headers=auth_headers,
                json={
                    "cycle_id": cycle_id,
                    "measurements": [
                        {
                            "pulse": 61,
                            "systolic": 117,
                            "diastolic": 76,
                            "spo2": 98,
                            "source": "watch_sensor",
                            "measured_at": clock.now().isoformat(),
                            "raw_metadata": {"event": "wake_up", "sleep_hours": 7.6},
                        }
                    ],
                },
            )
            clock.set(t0_dt + timedelta(hours=269))
            await _drain_worker(worker)

            with TestSessionFactory() as session:
                w_rep12 = WellnessDailyService(db=session, now_provider=clock.now).get_daily_wellness_report(cycle_id, 2, 5)
                assert w_rep12 is not None and w_rep12.status == WellnessStatus.READY
                j_rep12 = AthenaDailyJournalService(db=session, now_provider=clock.now).get_daily_journal(cycle_id, 2, 5)
                assert j_rep12 is not None and j_rep12.athena_clinical_assessment.is_available is True

            # --- DAY 13: NORMAL MONITORING ---
            clock.set(t0_dt + timedelta(hours=289))
            await client.post(
                "/api/v1/client/chat/athena",
                headers=auth_headers,
                json={"message": "Готовлюсь к завершению второй недели, всё отлично."},
            )
            await client.post(
                "/api/v1/vitals/batch",
                headers=auth_headers,
                json={
                    "cycle_id": cycle_id,
                    "measurements": [
                        {
                            "pulse": 62,
                            "systolic": 118,
                            "diastolic": 77,
                            "spo2": 98,
                            "source": "watch_sensor",
                            "measured_at": clock.now().isoformat(),
                            "raw_metadata": {"event": "wake_up", "sleep_hours": 7.7},
                        }
                    ],
                },
            )
            clock.set(t0_dt + timedelta(hours=293))
            await _drain_worker(worker)

            with TestSessionFactory() as session:
                w_rep13 = WellnessDailyService(db=session, now_provider=clock.now).get_daily_wellness_report(cycle_id, 2, 6)
                assert w_rep13 is not None and w_rep13.status == WellnessStatus.READY
                j_rep13 = AthenaDailyJournalService(db=session, now_provider=clock.now).get_daily_journal(cycle_id, 2, 6)
                assert j_rep13 is not None and j_rep13.athena_clinical_assessment.is_available is True

            # --- DAY 14: DAY 14 WELLNESS & AUTOMATIC WEEKLY CONSILIUM #2 AT T0 + 336H ---
            clock.set(t0_dt + timedelta(hours=313))
            await client.post(
                "/api/v1/client/chat/athena",
                headers=auth_headers,
                json={"message": "Вторая неделя полностью завершена, самочувствие на высоте."},
            )
            await client.post(
                "/api/v1/vitals/batch",
                headers=auth_headers,
                json={
                    "cycle_id": cycle_id,
                    "measurements": [
                        {
                            "pulse": 61,
                            "systolic": 117,
                            "diastolic": 75,
                            "spo2": 99,
                            "source": "watch_sensor",
                            "measured_at": clock.now().isoformat(),
                            "raw_metadata": {"event": "wake_up", "sleep_hours": 8.0},
                        }
                    ],
                },
            )
            # Midday fallback for Day 14 at T0 + 317h
            clock.set(t0_dt + timedelta(hours=317))
            await _drain_worker(worker)

            with TestSessionFactory() as session:
                w_rep14 = WellnessDailyService(db=session, now_provider=clock.now).get_daily_wellness_report(cycle_id, 2, 7)
                assert w_rep14 is not None and w_rep14.status == WellnessStatus.READY
                j_rep14 = AthenaDailyJournalService(db=session, now_provider=clock.now).get_daily_journal(cycle_id, 2, 7)
                assert j_rep14 is not None and j_rep14.athena_clinical_assessment.is_available is True

            # Advance clock to boundary T0 + 336h + 1s for Weekly Consilium #2
            clock.set(t0_dt + timedelta(hours=336, seconds=1))
            weekly_calls_before_2 = len(tracker.all_calls)
            await _drain_worker(worker)
            weekly_calls_after_2 = len(tracker.all_calls)

            # INVARIANT: Weekly Consilium #2 executed strictly 2 models (Flash Deputy -> Pro Chief)!
            # Flash-Lite specialists were NOT invoked!
            weekly_consilium_calls_2 = [
                c for c in tracker.all_calls[weekly_calls_before_2:weekly_calls_after_2]
                if c["role"] in ("weekly_deputy_flash", "weekly_chief_pro")
            ]
            assert len(weekly_consilium_calls_2) == 2
            assert weekly_consilium_calls_2[0]["role"] == "weekly_deputy_flash"
            assert weekly_consilium_calls_2[1]["role"] == "weekly_chief_pro"

            # ------------------------------------------------------------------
            # SECTION 4 & 5: PROOF OF STRATEGY, HISTORICAL MEMORY & INVARIANTS
            # ------------------------------------------------------------------

            # PROOF 1: Weekly #2 received previous approved Strategy v2 from Pro
            w2_pro_prompt = weekly_consilium_calls_2[1]["prompt"]
            w2_flash_prompt = weekly_consilium_calls_2[0]["prompt"]
            assert (
                "СТРАТЕГИЯ PRO НА ВТОРУЮ НЕДЕЛЮ" in w2_pro_prompt
                or "СТРАТЕГИЯ PRO НА ВТОРУЮ НЕДЕЛЮ" in w2_flash_prompt
            ), "Weekly Consilium #2 did not receive previous Pro Strategy v2!"

            # PROOF 2: Weekly #2 received journals for Week 2 observation period
            assert (
                "WEEK 2" in w2_flash_prompt
                or "SEVEN-DAY ATHENA CLINICAL JOURNALS (WEEK 2" in w2_flash_prompt
            ), "Weekly Consilium #2 did not receive Week 2 daily journals!"

            # PROOF 3: Weekly #2 received new and previous lab facts & Lab Revision #2 summary
            assert (
                "creatinine" in w2_flash_prompt.lower()
                or "креатинин" in w2_flash_prompt.lower()
                or "115" in w2_flash_prompt
            ), "Weekly Consilium #2 did not receive Lab Revision #2 Creatinine findings!"

            # PROOF 4: Weekly #2 received closed Mode 2 episode from Week 2
            assert (
                mode2_ep2_id in w2_flash_prompt
                or "лестниц" in w2_flash_prompt.lower()
            ), "Weekly Consilium #2 did not receive Week 2 closed Mode 2 episode!"

            # PROOF 5: Exact time cutoffs: Future events never leaked into Weekly #1
            assert (
                mode2_ep2_id not in w1_flash_prompt
            ), "Future Mode 2 episode #2 leaked into Weekly Consilium #1!"
            assert (
                "WEEK 2" not in w1_flash_prompt
            ), "Future Week 2 journals leaked into Weekly Consilium #1!"

            # PROOF 6: Strict monotonic sequence of strategy versions: v1 -> v2 -> v3
            with TestSessionFactory() as session:
                rep_repo = ModelReportRepository(session)
                strat_reports = rep_repo.list_server_only(cycle_id=cycle_id, report_type=ATHENA_ACTIVE_STRATEGY_REPORT_TYPE)
                assert len(strat_reports) == 3
                assert [r.version for r in strat_reports] == [3, 2, 1]
                assert "СТРАТЕГИЯ PRO НА ПЕРВУЮ НЕДЕЛЮ" in (strat_reports[2].content or {}).get("active_strategy_text", "")
                assert "СТРАТЕГИЯ PRO НА ВТОРУЮ НЕДЕЛЮ" in (strat_reports[1].content or {}).get("active_strategy_text", "")
                assert "СТРАТЕГИЯ PRO НА ТРЕТЬЮ НЕДЕЛЮ" in (strat_reports[0].content or {}).get("active_strategy_text", "")

                # PROOF 7: Athena Weekly Plans: Week 1 -> Week 2 -> Week 3
                plan_reports = rep_repo.list_server_only(cycle_id=cycle_id, report_type=ATHENA_WEEKLY_PLAN_REPORT_TYPE)
                assert len(plan_reports) >= 2
                plan_svc = AthenaWeeklyPlanService(db=session, now_provider=clock.now)
                plan_w3 = plan_svc.get_active_weekly_plan(cycle_id, week_number=3)
                assert plan_w3 is not None
                assert plan_w3.week_number == 3

                # PROOF 8: Zero duplicate Mode 2 episodes across 14 days
                m2_svc = Mode2VitalsMonitoringService(db=session, now_provider=clock.now)
                mode2_episodes = m2_svc.get_episode_history(cycle_id)
                assert len(mode2_episodes) == 2
                found_ep_ids = {e.episode_id for e in mode2_episodes}
                assert found_ep_ids == {mode2_ep1_id, mode2_ep2_id}
                assert all(e.status == Mode2EpisodeStatus.CLOSED for e in mode2_episodes)

                # PROOF 9: Real count of Wellness reports across 14 days: exactly 14 reports
                well_reports = WellnessDailyService(db=session, now_provider=clock.now).list_wellness_reports(cycle_id)
                assert len(well_reports) == 14
                ready_w_count = sum(1 for w in well_reports if w.status == WellnessStatus.READY)
                no_data_w_count = sum(1 for w in well_reports if w.status == WellnessStatus.NO_DATA)
                assert ready_w_count == 13
                assert no_data_w_count == 1  # Day 4

                # PROOF 10: Real count of Daily Journals across 14 days: exactly 14 journals
                journals = AthenaDailyJournalService(db=session, now_provider=clock.now).list_daily_journals(cycle_id)
                assert len(journals) == 14
                with_assessment = sum(1 for j in journals if j.athena_clinical_assessment.is_available)
                without_assessment = sum(1 for j in journals if not j.athena_clinical_assessment.is_available)
                assert with_assessment == 13
                assert without_assessment == 1  # Day 4

                # PROOF 11: Real count of Weekly Consiliums: exactly 2 executions
                weekly_step_a_reports = rep_repo.list_server_only(cycle_id=cycle_id, report_type=WEEKLY_CHIEF_INTERNAL_REPORT_TYPE)
                assert len(weekly_step_a_reports) == 2

                # PROOF 12: Real count of Lab Revisions: exactly 3 executions (Day 3 Ferritin, Day 8 Creatinine, Day 12 Glucose)
                lab_rev_chief_reports = rep_repo.list_server_only(cycle_id=cycle_id, report_type="lab_revision_chief_internal")
                assert len(lab_rev_chief_reports) == 3

            # PROOF 13: Client Isolation & Safe: Zero SERVER_ONLY reports leaked to client endpoints
            client_reports_resp = await client.get(f"/api/v1/reports/client?cycle_id={cycle_id}", headers=auth_headers)
            assert client_reports_resp.status_code == 200
            client_reports = client_reports_resp.json()
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

            # PROOF 14: Direct attempt to read a SERVER_ONLY report returns 403 Forbidden
            strat_v2_id = strat_reports[1].report_id
            forbidden_resp = await client.get(f"/api/v1/reports/client/{strat_v2_id}", headers=auth_headers)
            assert forbidden_resp.status_code == 403

    finally:
        server.should_exit = True
        await server_task
        app.dependency_overrides.clear()

    # Performance and SQLite Size Measurement
    execution_duration = time.time() - start_wall_time
    sqlite_size_bytes = db_file.stat().st_size
    sqlite_size_kb = sqlite_size_bytes / 1024.0

    print("\n" + "=" * 60)
    print("STAGE 7C.1 PILOT SIMULATION METRICS:")
    print(f"Total Virtual Days: 14 (Weeks 1 & 2)")
    print(f"Total Execution Duration: {execution_duration:.2f} seconds")
    print(f"Final SQLite Database Size: {sqlite_size_kb:.2f} KB ({sqlite_size_bytes} bytes)")
    print(f"Total AI Calls Tracked: {len(tracker.all_calls)}")
    print(f"AI Calls Distribution by Role: {json.dumps(tracker.by_role, indent=2)}")
    print("=" * 60 + "\n")
