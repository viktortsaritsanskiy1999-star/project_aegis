"""
PROJECT AEGIS — STAGE 6C: LAB REVISION -> WEEKLY CONSILIUM INTEGRATION TEST SUITE

Comprehensive verification of:
1. Completed Lab Revision flows into Weekly Consilium context.
2. Main Flash Weekly receives parallel cascade conclusions (Deputy synthesis + organ summaries).
3. Main Pro Weekly receives Lab Revision analytical summary + own main strategy history.
4. Lab Revision does NOT change strategy outside Weekly (strict SERVER_ONLY, active strategy unchanged).
5. Weekly independently decides on strategy update (updated vs continued).
6. Athena forms new weekly plan ONLY after regular Weekly Consilium.
7. New labs during 72-hour cooldown are available to Weekly with status pending_revision_in_cooldown.
8. Old Lab Revision is not misrepresented as analysis of new results.
9. Absence of completed Lab Revision does not block Weekly Consilium.
10. Multiple Lab Revisions are chronologically ordered by time and version.
11. Lab data at weekly boundary strictly belongs to the correct sprint observation window.
12. Re-running Weekly does not duplicate reports (idempotency with zero AI calls).
13. Server restart preserves pending revisions and queue in SQLite.
14. Server configuration explicitly enables/disables background Lab Revision (AEGIS_ENABLE_LAB_REVISION).
15. Eight Flash-Lite models are NOT called inside Weekly Consilium (strictly Flash -> Pro, exactly 2 calls).
16. Internal Lab Revision conclusions remain strictly SERVER_ONLY.
17. Weekly user report does not leak internal prompts, shelves, or technical structures.
18. Full E2E lifecycle: Primary -> Lab Upload -> Debounce -> 10-model Lab Revision -> 7-Day Journals ->
    Weekly Consilium (Flash -> Pro) -> Athena Plan Week 2 -> Client Wrapup.
"""

import asyncio
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional
import uuid

import httpx
import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import settings
from app.database import Base
from app.models.model_report import ModelReport
from app.schemas.privacy import PrivacyLevel
from app.repositories.cycle_repository import MedicalCycleRepository
from app.repositories.report_repository import ModelReportRepository
from app.repositories.patient_repository import PatientRepository
from app.schemas.consilium import SYSTEM_KEYS, AegisAgentRole, LabMarkerMutationItem
from app.schemas.lab_scheduling import (
    LabChangeType,
    LabDocumentRecord,
    LabDocumentStatus,
    LabSchedulingStatusResponse,
)
from app.services.athena_weekly_plan_service import (
    ATHENA_WEEKLY_PLAN_REPORT_TYPE,
    AthenaWeeklyPlanRecord,
    AthenaWeeklyPlanService,
)
from app.services.clinical_automation_service import (
    AutomationJobStatus,
    AutomationJobType,
    ClinicalAutomationService,
    ClinicalAutomationWorker,
)
from app.services.gemini_gateway import GeminiGateway, GeminiModelRole
from app.services.lab_revision_consilium_service import (
    BACKGROUND_LAB_REVISION_REPORT_TYPE,
    LabRevisionConsiliumService,
)
from app.services.lab_context_service import LABORATORY_LEDGER_REPORT_TYPE
from app.services.lab_scheduling_service import (
    LAB_DOCUMENT_REPORT_TYPE,
    LAB_REVISION_PENDING_TASK_REPORT_TYPE,
    LabSchedulingService,
)
from app.services.primary_consilium_service import (
    PREPARATORY_ANAMNESIS_REPORT_TYPE,
    PrimaryConsiliumService,
)
from app.services.weekly_consilium_service import (
    ATHENA_ACTIVE_STRATEGY_REPORT_TYPE,
    ATHENA_SCHEDULE_ANCHOR_REPORT_TYPE,
    WEEKLY_CHIEF_INTERNAL_REPORT_TYPE,
    WEEKLY_CONSILIUM_EXECUTION_LOG_REPORT_TYPE,
    WEEKLY_DEPUTY_INTERNAL_REPORT_TYPE,
    WEEKLY_OVERALL_CLIENT_REPORT_TYPE,
    WeeklyConsiliumService,
)
from tests.test_primary_consilium import (
    _build_consilium_mock_gateway,
    _sample_anamnesis_output,
    _seed_preparatory_cycle_context,
)


# ==============================================================================
# CLOCK & MOCK FIXTURES
# ==============================================================================

class MockClock:
    def __init__(self, start: datetime):
        self._current = start

    def now(self) -> datetime:
        return self._current

    def advance(self, minutes: int = 0, hours: int = 0, seconds: int = 0, days: int = 0) -> datetime:
        self._current += timedelta(days=days, hours=hours, minutes=minutes, seconds=seconds)
        return self._current

    def set(self, dt: datetime) -> None:
        self._current = dt


def _gemini_envelope(payload: Any) -> Dict[str, Any]:
    text = json.dumps(payload, ensure_ascii=False) if not isinstance(payload, str) else payload
    return {
        "candidates": [
            {
                "content": {
                    "parts": [{"text": text}],
                    "role": "model",
                },
                "finishReason": "STOP",
                "index": 0,
            }
        ]
    }


def _sample_system_agent_output(system_key: str) -> Dict[str, Any]:
    return {
        "system_view": {
            "isolated_labs": {f"{system_key}_marker": "within reference"},
        },
        "system_logic": {
            "system_general_shelf": f"Analytical evaluation of {system_key}: markers remain clinically consistent.",
        },
        "training_sandbox": {
            "system_training_shelf": f"Training rationale for {system_key} based on updated laboratory diff.",
        },
    }


def _sample_lab_revision_deputy_output() -> Dict[str, Any]:
    return {
        "deputy_synthesis": "Lab Revision Deputy Synthesis: Fasting glucose 5.4 mmol/L and corrected ferritin align with metabolic compensation.",
        "specialist_discrepancies": ["No major intersystem conflicts observed between endocrine and hepatology specialists."],
        "clinical_correlations": ["Metabolic-cardiovascular correlation: glucose trend aligns with resting pulse."],
        "data_limitations": ["HbA1c pending in subsequent panels."],
        "draft_hypotheses": ["Mild transient postprandial glycemic shift."],
    }


def _sample_lab_revision_chief_output() -> Dict[str, Any]:
    return {
        "analytical_summary": "Supreme Analytical Lab Revision Verdict: Glucose 5.4 mmol/L confirmed within acceptable boundaries. Strategic metabolic focus recommended for upcoming Weekly Consilium.",
        "observed_facts": [
            "Venous glucose 5.4 mmol/L on 2026-11-06.",
            "Ferritin confirmed at 115 mcg/L.",
        ],
        "clinical_hypotheses": [
            "Isolated postprandial glucose fluctuation.",
        ],
        "uncertainties_and_data_deficits": [
            "Lack of serial fasting glucose measurements across 7 consecutive days.",
        ],
        "questions_for_weekly_consilium": [
            "Monitor patient's carbohydrate intake timing in evening hours.",
            "Track morning resting pulse dynamics.",
        ],
        "cascade_quality_audit": "10-model analytical cascade completed with high diagnostic fidelity.",
    }


def _sample_weekly_flash_output() -> Dict[str, Any]:
    system_reports = {
        k: {
            "summary": f"Стабильная положительная динамика системы {k}.",
            "has_changes": True,
        }
        for k in SYSTEM_KEYS
    }
    return {
        "weekly_deputy_analysis": "Заместитель главного врача (Weekly Flash): Семидневный мониторинг показывает стабильные показатели, данные параллельной ревизии анализов учтены.",
        "user_system_reports": system_reports,
    }


def _sample_weekly_pro_output(
    strategy_status: str = "updated",
    approved: bool = True,
    updated_strategy: Optional[str] = None,
) -> Dict[str, Any]:
    default_strategy = (
        "УТВЕРЖДЁННАЯ СТРАТЕГИЯ PRO НА СЛЕДУЮЩУЮ НЕДЕЛЮ:\n"
        "1. Контроль адаптации к вечерней нагрузке и углеводному профилю.\n"
        "2. Уточнение гидратации во второй половине дня.\n"
        "3. Оценка динамики восстановления ЧСС после ходьбы с учётом лабораторного статуса."
    )
    return {
        "updated_weekly_strategy": updated_strategy or default_strategy,
        "strategy_status": strategy_status,
        "weekly_overall_report": {
            "title": "Итоги недели 1: Стабильный прогресс",
            "summary": "Отличная неделя: пульс в покое стабилизировался, лабораторные показатели согласованы со стратегией.",
            "highlights": [
                "Пульс покоя снизился в среднем на 3 уд/мин",
                "Лабораторный статус стабилен, углеводный обмен компенсирован",
            ],
        },
        "flash_quality_review": {
            "approved_for_publication": approved,
            "rejection_reason": None,
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
        "summary_for_curator": f"План недели {week_number}: контроль восстановления ЧСС и питания",
        "goals": [
            {
                "goal_id": f"goal_{week_number}_1",
                "title": "Оценка самочувствия после вечерней ходьбы",
                "clinical_rationale": "Необходимо убедиться в отсутствии одышки и тахикардии",
                "priority": 1,
                "known_facts": ["Базовая переносимость удовлетворительная"],
                "target_clarification": "Уточнить наличие одышки или сердцебиения",
                "sufficiency_criteria": "Пациент ответил о наличии или отсутствии симптомов",
                "status": "active",
                "questions_asked_count": 0,
                "progress_notes": "Инициализация цели",
            }
        ],
        "status": "active",
    }


def _build_stage_6c_mock_gateway(
    recorded_calls: List[Dict[str, Any]],
    weekly_strategy_status: str = "updated",
    weekly_strategy_text: Optional[str] = None,
) -> GeminiGateway:
    """
    Mock GeminiGateway handling both Stage 6B (10-model Lab Revision)
    and Stage 6C (Weekly Consilium Flash -> Pro + Athena Weekly Plan).
    """
    call_counter = {"index": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        idx = call_counter["index"]
        call_counter["index"] += 1

        body = json.loads(request.content.decode("utf-8"))
        prompt_text = ""
        for part in body.get("contents", [{}])[0].get("parts", []):
            prompt_text += part.get("text", "")
        url_path = str(request.url)

        recorded_calls.append(
            {
                "index": idx,
                "url": url_path,
                "prompt": prompt_text,
                "model": url_path.split("/")[-1].split(":")[0],
            }
        )

        # 1. Parallel Lab Revision: Deputy Flash
        if "BACKGROUND COMPARATIVE LAB-REVISION ANALYSIS" in prompt_text:
            return httpx.Response(200, json=_gemini_envelope(_sample_lab_revision_deputy_output()))

        # 2. Parallel Lab Revision: Chief Pro
        if "SUPREME ANALYTICAL LAB-REVISION REPORT" in prompt_text:
            return httpx.Response(200, json=_gemini_envelope(_sample_lab_revision_chief_output()))

        # 3. Weekly Consilium: Deputy Flash
        if "You are the Head of Department (Aegis Clinical Analyst)" in prompt_text or "1 week (7 days) of patient monitoring has passed" in prompt_text:
            return httpx.Response(200, json=_gemini_envelope(_sample_weekly_flash_output()))

        # 4. Weekly Consilium: Chief Pro
        if "You are the Chief Physician of the Aegis expert system." in prompt_text or "update the working strategy for the NEXT WEEK" in prompt_text:
            return httpx.Response(
                200,
                json=_gemini_envelope(
                    _sample_weekly_pro_output(
                        strategy_status=weekly_strategy_status,
                        updated_strategy=weekly_strategy_text,
                    )
                ),
            )

        # 5. Athena Weekly Plan Generator
        if "[Internal Weekly Plan Formulation Scenario]" in prompt_text or "Athena Weekly Plan Generator" in prompt_text:
            return httpx.Response(200, json=_gemini_envelope(_sample_weekly_plan(cycle_id="test_cycle", week_number=2)))

        # 6. Athena Wrapup Structured Message
        if "[Internal Athena Weekly Wrap-up Message Formulation]" in prompt_text or "Weekly Wrap-up" in prompt_text:
            return httpx.Response(
                200,
                json=_gemini_envelope(
                    {
                        "message": "Здравствуйте! Завершилась первая неделя программы Aegis. Показатели стабильны, подробный отчёт опубликован в Vault."
                    }
                ),
            )

        # 7. Athena Post-Primary Init or Chat Plain Text
        if "Первичное приветствие" in prompt_text or "POST_PRIMARY" in prompt_text or "куратор Aegis" in prompt_text:
            return httpx.Response(
                200,
                json={
                    "candidates": [
                        {
                            "content": {
                                "parts": [
                                    {
                                        "text": "Здравствуйте! Я Афина, ваш персональный куратор Aegis."
                                    }
                                ],
                                "role": "model",
                            },
                            "finishReason": "STOP",
                        }
                    ]
                },
            )

        # 7. Organ specialist (Flash-Lite) for Lab Revision
        for sk in SYSTEM_KEYS:
            if f"'{sk}'" in prompt_text or f"the '{sk}' system" in prompt_text:
                return httpx.Response(200, json=_gemini_envelope(_sample_system_agent_output(sk)))

        # Fallback default
        return httpx.Response(200, json=_gemini_envelope(_sample_system_agent_output("cvs")))

    return GeminiGateway(
        api_key="test-stage-6c-key",
        transport=httpx.MockTransport(handler),
    )


# ==============================================================================
# SEEDING HELPERS
# ==============================================================================

def _seed_patient_and_cycle(
    session: Session,
    patient_id: str,
    cycle_id: str,
    anchor_dt: datetime,
) -> None:
    p_repo = PatientRepository(session)
    if not p_repo.get(patient_id):
        p_repo.create(patient_id=patient_id, status="active")

    c_repo = MedicalCycleRepository(session)
    if not c_repo.get(cycle_id):
        c_repo.create(
            cycle_id=cycle_id,
            patient_id=patient_id,
            status="active",
            started_at=anchor_dt,
        )


async def _seed_active_cycle_with_baseline(
    session: Session,
    patient_id: str,
    cycle_id: str,
    anchor_dt: datetime,
) -> None:
    """Seeds patient, cycle, preparatory context, Primary Consilium, and schedule anchor T0."""
    _seed_patient_and_cycle(session, patient_id, cycle_id, anchor_dt)
    _seed_preparatory_cycle_context(session, cycle_id=cycle_id, seed_ledger=True)

    primary_calls: List[Dict[str, Any]] = []
    primary_gateway = _build_consilium_mock_gateway(primary_calls)
    primary_service = PrimaryConsiliumService(
        db=session,
        gateway=primary_gateway,
        now_provider=lambda: anchor_dt,
    )
    await primary_service.run_primary_consilium(
        cycle_id=cycle_id,
        report_date=anchor_dt.strftime("%Y-%m-%d"),
    )

    # Establish authoritative schedule anchor T0
    weekly_service = WeeklyConsiliumService(
        db=session, gateway=primary_gateway, now_provider=lambda: anchor_dt
    )
    weekly_service.record_schedule_anchor(
        cycle_id=cycle_id,
        anchor_dt=anchor_dt,
        source="primary_consilium_init",
        commit=True,
    )

    # Mark post_primary_init job as completed
    auto_svc = ClinicalAutomationService(
        db=session,
        gateway=primary_gateway,
        now_provider=lambda: anchor_dt,
    )
    init_job = auto_svc.enqueue_post_primary_init(cycle_id=cycle_id, patient_id=patient_id)
    if init_job is not None:
        row = session.query(ModelReport).filter(ModelReport.report_id == init_job.job_id).first()
        if row is not None:
            c = dict(row.content or {})
            c["status"] = AutomationJobStatus.COMPLETED.value
            row.content = c
            session.commit()


def _seed_seven_day_athena_logs(
    session: Session,
    cycle_id: str,
    sprint_id: str,
    start_dt: datetime,
) -> None:
    weekly_service = WeeklyConsiliumService(db=session, now_provider=lambda: start_dt)
    for day in range(1, 8):
        log_dt = start_dt + timedelta(days=day)
        weekly_service.save_athena_clinical_log(
            cycle_id=cycle_id,
            sprint_id=sprint_id,
            log_entry={
                "status": "NORMAL",
                "symptoms_mentioned": [],
                "subjective_wellbeing_score": 8,
                "adherence_to_plan": "HIGH",
                "red_flags": False,
                "summary_for_master": f"Day {day} Athena observation: vitals stable, plan adhered to.",
                "timestamp": log_dt.strftime("%Y-%m-%dT20:00:00Z"),
            },
        )


# ==============================================================================
# TESTS (1 TO 18)
# ==============================================================================

@pytest.mark.anyio
async def test_01_completed_lab_revision_flows_into_weekly(db_session: Session):
    """
    1. Completed Lab Revision flows into Weekly:
    After background Lab Revision finishes, its conclusions are provided to Weekly Consilium context.
    """
    t0 = datetime(2026, 11, 1, 8, 0, tzinfo=timezone.utc)
    clock = MockClock(t0)
    cycle_id = "cyc_6c_01"
    patient_id = "pat_6c_01"
    await _seed_active_cycle_with_baseline(db_session, patient_id, cycle_id, t0)

    recorded_calls: List[Dict[str, Any]] = []
    gw = _build_stage_6c_mock_gateway(recorded_calls)

    # 1. Upload lab update at T0 + 20h
    clock.advance(hours=20)
    sched_svc = LabSchedulingService(db=db_session, gateway=gw, now_provider=clock.now)
    resp = sched_svc.process_manual_edit(
        cycle_id=cycle_id,
        patient_id=patient_id,
        mutations=[
            LabMarkerMutationItem(
                marker_key="glucose",
                display_name="Glucose",
                value=5.4,
                unit="mmol/L",
                sample_date="2026-11-01",
            )
        ],
    )
    assert resp.change_type == LabChangeType.MEANINGFUL_CHANGE

    # 2. Advance clock past 15 min debounce -> execute Lab Revision cascade
    clock.advance(minutes=16)
    auto_svc = ClinicalAutomationService(
        db=db_session, gateway=gw, now_provider=clock.now, enable_lab_revision=True
    )
    processed = await auto_svc.process_due_jobs(discover=True)
    assert any(j.job_type == AutomationJobType.LAB_REVISION and j.status == AutomationJobStatus.COMPLETED for j in processed)

    # Verify background Lab Revision report was stored
    report_repo = ModelReportRepository(db_session)
    rev_reports = report_repo.list_server_only(cycle_id, report_type=BACKGROUND_LAB_REVISION_REPORT_TYPE)
    assert len(rev_reports) == 1
    assert rev_reports[0].content["evaluated_ledger_version"] == 2

    # 3. Seed Athena logs and advance to Weekly boundary T0 + 168h
    clock.set(t0 + timedelta(hours=168))
    sprint_id = f"sprint_{cycle_id}_w1"
    _seed_seven_day_athena_logs(db_session, cycle_id=cycle_id, sprint_id=sprint_id, start_dt=t0)

    recorded_calls.clear()
    weekly_svc = WeeklyConsiliumService(db=db_session, gateway=gw, now_provider=clock.now)
    result = await weekly_svc.run_weekly_consilium(
        cycle_id=cycle_id,
        sprint_id=sprint_id,
        report_date="2026-11-08",
    )
    assert result is not None

    # Check prompts received by Weekly Flash and Weekly Pro
    flash_call = next(c for c in recorded_calls if "You are the Head of Department" in c["prompt"])
    pro_call = next(c for c in recorded_calls if "You are the Chief Physician of the Aegis expert system." in c["prompt"])

    assert "PARALLEL LAB REVISION INPUT & LABORATORY STATUS" in flash_call["prompt"]
    assert "PARALLEL LAB REVISION INPUT & LABORATORY STATUS" in pro_call["prompt"]
    assert "LAB REVISION UP TO DATE" in flash_call["prompt"]
    assert "Supreme Analytical Lab Revision Verdict" in pro_call["prompt"]

    # Verify execution log contains lab revision metadata
    exec_logs = report_repo.list_server_only(cycle_id, report_type=WEEKLY_CONSILIUM_EXECUTION_LOG_REPORT_TYPE)
    assert len(exec_logs) == 1
    log_content = exec_logs[0].content
    assert log_content["lab_ledger_version"] == 2
    assert log_content["evaluated_lab_revision_version"] == 2
    assert log_content["lab_revision_status"] == "up_to_date"


@pytest.mark.anyio
async def test_02_main_flash_weekly_receives_parallel_cascade_conclusions(db_session: Session):
    """
    2. Main Flash Weekly receives parallel cascade conclusions:
    Flash Deputy receives Deputy Flash synthesis from Lab Revision and structured data.
    """
    t0 = datetime(2026, 11, 1, 8, 0, tzinfo=timezone.utc)
    clock = MockClock(t0)
    cycle_id = "cyc_6c_02"
    patient_id = "pat_6c_02"
    await _seed_active_cycle_with_baseline(db_session, patient_id, cycle_id, t0)

    recorded_calls: List[Dict[str, Any]] = []
    gw = _build_stage_6c_mock_gateway(recorded_calls)

    # Trigger and complete lab revision
    sched_svc = LabSchedulingService(db=db_session, gateway=gw, now_provider=clock.now)
    sched_svc.process_manual_edit(
        cycle_id=cycle_id,
        patient_id=patient_id,
        mutations=[
            LabMarkerMutationItem(marker_key="alt", display_name="ALT", value=28.0, unit="U/L")
        ],
    )
    clock.advance(minutes=16)
    auto_svc = ClinicalAutomationService(db=db_session, gateway=gw, now_provider=clock.now, enable_lab_revision=True)
    await auto_svc.process_due_jobs(discover=True)

    # Advance to T0 + 168h
    clock.set(t0 + timedelta(hours=168))
    sprint_id = f"sprint_{cycle_id}_w1"
    _seed_seven_day_athena_logs(db_session, cycle_id=cycle_id, sprint_id=sprint_id, start_dt=t0)

    recorded_calls.clear()
    weekly_svc = WeeklyConsiliumService(db=db_session, gateway=gw, now_provider=clock.now)
    await weekly_svc.run_weekly_consilium(cycle_id=cycle_id, sprint_id=sprint_id)

    flash_call = next(c for c in recorded_calls if "You are the Head of Department" in c["prompt"])
    prompt = flash_call["prompt"]
    assert "Deputy Flash Synthesis: Lab Revision Deputy Synthesis" in prompt
    assert "No major intersystem conflicts observed" in prompt
    assert "CRITICAL CONSILIUM POLICY:" in prompt


@pytest.mark.anyio
async def test_03_main_pro_weekly_receives_lab_revision_and_own_main_history(db_session: Session):
    """
    3. Main Pro Weekly receives Lab Revision + own main history:
    Pro Chief receives Lab Revision Chief conclusion, questions for Weekly,
    and its own authoritative active strategy history from Primary Consilium.
    """
    t0 = datetime(2026, 11, 1, 8, 0, tzinfo=timezone.utc)
    clock = MockClock(t0)
    cycle_id = "cyc_6c_03"
    patient_id = "pat_6c_03"
    await _seed_active_cycle_with_baseline(db_session, patient_id, cycle_id, t0)

    recorded_calls: List[Dict[str, Any]] = []
    gw = _build_stage_6c_mock_gateway(recorded_calls)

    # Trigger and complete lab revision
    sched_svc = LabSchedulingService(db=db_session, gateway=gw, now_provider=clock.now)
    sched_svc.process_manual_edit(
        cycle_id=cycle_id,
        patient_id=patient_id,
        mutations=[
            LabMarkerMutationItem(marker_key="hba1c", display_name="HbA1c", value=5.6, unit="%")
        ],
    )
    clock.advance(minutes=16)
    auto_svc = ClinicalAutomationService(db=db_session, gateway=gw, now_provider=clock.now, enable_lab_revision=True)
    await auto_svc.process_due_jobs(discover=True)

    clock.set(t0 + timedelta(hours=168))
    sprint_id = f"sprint_{cycle_id}_w1"
    _seed_seven_day_athena_logs(db_session, cycle_id=cycle_id, sprint_id=sprint_id, start_dt=t0)

    recorded_calls.clear()
    weekly_svc = WeeklyConsiliumService(db=db_session, gateway=gw, now_provider=clock.now)
    await weekly_svc.run_weekly_consilium(cycle_id=cycle_id, sprint_id=sprint_id)

    pro_call = next(c for c in recorded_calls if "You are the Chief Physician of the Aegis expert system." in c["prompt"])
    prompt = pro_call["prompt"]

    # 1. Received Lab Revision Analytical Summary & Questions
    assert "Supreme Analytical Lab Revision Verdict" in prompt
    assert "Questions for Weekly Consilium:" in prompt

    # 2. Received own main active strategy
    assert "Current Active Strategy:" in prompt

    # 3. Received Athena 7-day diary
    assert "ATHENA'S DIARY:" in prompt


@pytest.mark.anyio
async def test_04_lab_revision_does_not_change_strategy_outside_weekly(db_session: Session):
    """
    4. Lab Revision does not change strategy outside Weekly:
    After background Lab Revision completes, active strategy remains strictly unchanged (v1),
    Athena continues working under the old plan, vitals thresholds are unchanged.
    """
    t0 = datetime(2026, 11, 1, 8, 0, tzinfo=timezone.utc)
    clock = MockClock(t0)
    cycle_id = "cyc_6c_04"
    patient_id = "pat_6c_04"
    await _seed_active_cycle_with_baseline(db_session, patient_id, cycle_id, t0)

    gw = _build_stage_6c_mock_gateway([])
    weekly_svc = WeeklyConsiliumService(db=db_session, gateway=gw, now_provider=clock.now)
    strat_before = weekly_svc.get_active_athena_context(cycle_id)
    assert strat_before.strategy_version == 1
    assert strat_before.source_consilium == "primary_consilium"

    sched_svc = LabSchedulingService(db=db_session, gateway=gw, now_provider=clock.now)
    sched_svc.process_manual_edit(
        cycle_id=cycle_id,
        patient_id=patient_id,
        mutations=[
            LabMarkerMutationItem(marker_key="creatinine", display_name="Creatinine", value=85.0, unit="umol/L")
        ],
    )
    clock.advance(minutes=16)

    # Run Lab Revision
    auto_svc = ClinicalAutomationService(db=db_session, gateway=gw, now_provider=clock.now, enable_lab_revision=True)
    await auto_svc.process_due_jobs(discover=True)

    # Check active strategy in DB: MUST REMAIN EXACTLY VERSION 1 from primary
    strat_after = weekly_svc.get_active_athena_context(cycle_id)
    assert strat_after.strategy_version == 1
    assert strat_after.source_consilium == "primary_consilium"
    assert strat_after.active_strategy_text == strat_before.active_strategy_text

    # Check that NO Athena active strategy report row or weekly plan row was created by Lab Revision
    report_repo = ModelReportRepository(db_session)
    strat_rows = report_repo.list_server_only(cycle_id, report_type=ATHENA_ACTIVE_STRATEGY_REPORT_TYPE)
    assert len(strat_rows) == 0
    plans = report_repo.list_server_only(cycle_id, report_type=ATHENA_WEEKLY_PLAN_REPORT_TYPE)
    assert len(plans) == 0


@pytest.mark.anyio
async def test_05_weekly_independently_decides_on_strategy_update(db_session: Session):
    """
    5. Weekly independently decides on strategy update:
    Weekly Pro may adopt recommendations or decide to confirm continuation ('continued').
    """
    t0 = datetime(2026, 11, 1, 8, 0, tzinfo=timezone.utc)
    clock = MockClock(t0)
    cycle_id = "cyc_6c_05"
    patient_id = "pat_6c_05"
    await _seed_active_cycle_with_baseline(db_session, patient_id, cycle_id, t0)

    # Test decision "continued"
    recorded_calls: List[Dict[str, Any]] = []
    gw = _build_stage_6c_mock_gateway(
        recorded_calls,
        weekly_strategy_status="continued",
        weekly_strategy_text="ПОДТВЕРЖДЕНИЕ СТРАТЕГИИ: Стратегия продолжается без изменений с акцентом на питьевой режим.",
    )

    clock.set(t0 + timedelta(hours=168))
    sprint_id = f"sprint_{cycle_id}_w1"
    _seed_seven_day_athena_logs(db_session, cycle_id=cycle_id, sprint_id=sprint_id, start_dt=t0)

    weekly_svc = WeeklyConsiliumService(db=db_session, gateway=gw, now_provider=clock.now)
    result = await weekly_svc.run_weekly_consilium(cycle_id=cycle_id, sprint_id=sprint_id)
    assert result is not None

    report_repo = ModelReportRepository(db_session)
    strat_reports = report_repo.list_server_only(cycle_id, report_type=ATHENA_ACTIVE_STRATEGY_REPORT_TYPE)
    assert len(strat_reports) == 2
    latest_strat = strat_reports[0]
    assert latest_strat.content["strategy_status"] == "continued"
    assert latest_strat.content["source_consilium"] == "weekly_consilium"
    assert "ПОДТВЕРЖДЕНИЕ СТРАТЕГИИ" in latest_strat.content["active_strategy_text"]


@pytest.mark.anyio
async def test_06_athena_forms_new_plan_only_after_regular_weekly(db_session: Session):
    """
    6. Athena forms new plan only after regular Weekly:
    Only after regular Weekly Consilium finishes and generates strategy v2,
    AthenaWeeklyPlanService creates a plan for the next sprint.
    """
    t0 = datetime(2026, 11, 1, 8, 0, tzinfo=timezone.utc)
    clock = MockClock(t0)
    cycle_id = "cyc_6c_06"
    patient_id = "pat_6c_06"
    await _seed_active_cycle_with_baseline(db_session, patient_id, cycle_id, t0)

    gw = _build_stage_6c_mock_gateway([])
    # 1. Lab revision completes
    sched_svc = LabSchedulingService(db=db_session, gateway=gw, now_provider=clock.now)
    sched_svc.process_manual_edit(
        cycle_id=cycle_id,
        patient_id=patient_id,
        mutations=[LabMarkerMutationItem(marker_key="cholesterol", display_name="Cholesterol", value=4.9, unit="mmol/L")],
    )
    clock.advance(minutes=16)
    auto_svc = ClinicalAutomationService(db=db_session, gateway=gw, now_provider=clock.now, enable_lab_revision=True)
    await auto_svc.process_due_jobs(discover=True)

    plan_svc = AthenaWeeklyPlanService(db=db_session, gateway=gw, now_provider=clock.now)
    current_plan = plan_svc.get_active_weekly_plan(cycle_id)
    assert current_plan is None  # Still no weekly plan for Week 2

    # 2. Run regular Weekly Consilium at T0 + 168h
    clock.set(t0 + timedelta(hours=168))
    sprint_id = f"sprint_{cycle_id}_w1"
    _seed_seven_day_athena_logs(db_session, cycle_id=cycle_id, sprint_id=sprint_id, start_dt=t0)

    weekly_svc = WeeklyConsiliumService(db=db_session, gateway=gw, now_provider=clock.now)
    await weekly_svc.run_weekly_consilium(cycle_id=cycle_id, sprint_id=sprint_id)

    # 3. Now generate weekly plan for Week 2
    new_plan = await plan_svc.create_or_get_weekly_plan(cycle_id=cycle_id, week_number=2)
    assert new_plan.week_number == 2
    assert new_plan.source_strategy_version == 2


@pytest.mark.anyio
async def test_07_new_labs_during_72h_cooldown_available_to_weekly(db_session: Session):
    """
    7. New labs during 72-hour cooldown are available to Weekly:
    Verified laboratory values from ledger are provided to Weekly,
    status pending_revision_in_cooldown is explicitly set, and cooldown is preserved.
    """
    t0 = datetime(2026, 11, 1, 8, 0, tzinfo=timezone.utc)
    clock = MockClock(t0)
    cycle_id = "cyc_6c_07"
    patient_id = "pat_6c_07"
    await _seed_active_cycle_with_baseline(db_session, patient_id, cycle_id, t0)

    recorded_calls: List[Dict[str, Any]] = []
    gw = _build_stage_6c_mock_gateway(recorded_calls)

    # 1. First lab revision completes at T0 + 120h (evaluates ledger v2)
    clock.advance(hours=120)
    sched_svc = LabSchedulingService(db=db_session, gateway=gw, now_provider=clock.now)
    sched_svc.process_manual_edit(
        cycle_id=cycle_id,
        patient_id=patient_id,
        mutations=[LabMarkerMutationItem(marker_key="glucose", display_name="Glucose", value=5.2, unit="mmol/L")],
    )
    clock.advance(minutes=16)
    auto_svc = ClinicalAutomationService(db=db_session, gateway=gw, now_provider=clock.now, enable_lab_revision=True)
    await auto_svc.process_due_jobs(discover=True)

    status_after_first = sched_svc.get_scheduling_status(cycle_id)
    assert status_after_first.cooldown_until_utc is not None

    # 2. Patient uploads NEW labs at T0 + 140h (during 72-hour cooldown which lasts until T0 + 192h) -> ledger v3
    clock.advance(hours=20)
    sched_svc.process_manual_edit(
        cycle_id=cycle_id,
        patient_id=patient_id,
        mutations=[LabMarkerMutationItem(marker_key="ferritin", display_name="Ferritin", value=120.0, unit="mcg/L")],
    )
    clock.advance(minutes=16)
    # Check that 10-model Lab Revision is NOT ready because cooldown is active
    ready, reason = sched_svc.evaluate_task_readiness(cycle_id)
    assert not ready
    assert "cooldown" in reason.lower()

    # 3. Advance to Weekly boundary T0 + 168h (still in 72h cooldown)
    clock.set(t0 + timedelta(hours=168))
    sprint_id = f"sprint_{cycle_id}_w1"
    _seed_seven_day_athena_logs(db_session, cycle_id=cycle_id, sprint_id=sprint_id, start_dt=t0)

    recorded_calls.clear()
    weekly_svc = WeeklyConsiliumService(db=db_session, gateway=gw, now_provider=clock.now)
    await weekly_svc.run_weekly_consilium(cycle_id=cycle_id, sprint_id=sprint_id)

    # Check prompts
    flash_call = next(c for c in recorded_calls if "You are the Head of Department" in c["prompt"])
    pro_call = next(c for c in recorded_calls if "You are the Chief Physician of the Aegis expert system." in c["prompt"])

    assert "NEW LABORATORY MARKERS PENDING REVISION IN 72-HOUR COOLDOWN" in flash_call["prompt"]
    assert "ferritin" in flash_call["prompt"]
    assert "120" in flash_call["prompt"]
    assert "evaluated only up to v2" in pro_call["prompt"]

    # Verify execution log
    report_repo = ModelReportRepository(db_session)
    exec_logs = report_repo.list_server_only(cycle_id, report_type=WEEKLY_CONSILIUM_EXECUTION_LOG_REPORT_TYPE)
    assert exec_logs[0].content["lab_revision_status"] == "pending_revision_in_cooldown"
    assert exec_logs[0].content["lab_ledger_version"] == 3
    assert exec_logs[0].content["evaluated_lab_revision_version"] == 2


@pytest.mark.anyio
async def test_08_old_lab_revision_not_misrepresented_as_analysis_of_new_results(db_session: Session):
    """
    8. Old Lab Revision is not misrepresented as analysis of new results:
    Weekly Consilium does not mistake prior conclusions for an analysis of the new markers.
    """
    t0 = datetime(2026, 11, 1, 8, 0, tzinfo=timezone.utc)
    clock = MockClock(t0)
    cycle_id = "cyc_6c_08"
    patient_id = "pat_6c_08"
    await _seed_active_cycle_with_baseline(db_session, patient_id, cycle_id, t0)

    recorded_calls: List[Dict[str, Any]] = []
    gw = _build_stage_6c_mock_gateway(recorded_calls)

    # First revision on v2 at T0 + 120h
    clock.advance(hours=120)
    sched_svc = LabSchedulingService(db=db_session, gateway=gw, now_provider=clock.now)
    sched_svc.process_manual_edit(
        cycle_id=cycle_id,
        patient_id=patient_id,
        mutations=[LabMarkerMutationItem(marker_key="crp", display_name="CRP", value=1.5, unit="mg/L")],
    )
    clock.advance(minutes=16)
    auto_svc = ClinicalAutomationService(db=db_session, gateway=gw, now_provider=clock.now, enable_lab_revision=True)
    await auto_svc.process_due_jobs(discover=True)

    # New marker arriving at T0 + 140h: potassium (during 72-hour cooldown)
    clock.advance(hours=20)
    sched_svc.process_manual_edit(
        cycle_id=cycle_id,
        patient_id=patient_id,
        mutations=[LabMarkerMutationItem(marker_key="potassium", display_name="Potassium", value=4.5, unit="mmol/L")],
    )
    clock.advance(minutes=16)

    clock.set(t0 + timedelta(hours=168))
    sprint_id = f"sprint_{cycle_id}_w1"
    _seed_seven_day_athena_logs(db_session, cycle_id=cycle_id, sprint_id=sprint_id, start_dt=t0)

    weekly_svc = WeeklyConsiliumService(db=db_session, gateway=gw, now_provider=clock.now)
    ctx = weekly_svc._load_lab_revision_context_for_weekly(cycle_id, as_of=clock.now())

    assert ctx["status_code"] == "pending_revision_in_cooldown"
    assert "evaluated only up to v2" in ctx["formatted_text"]
    assert "must NOT be cited as evaluating the new markers" in ctx["formatted_text"]
    assert any(m.get("marker_key") == "potassium" for m in ctx["unreviewed_markers"])


@pytest.mark.anyio
async def test_09_absence_of_completed_lab_revision_does_not_block_weekly(db_session: Session):
    """
    9. Absence of completed Lab Revision does not block Weekly:
    Weekly Consilium completes smoothly when no background revision has occurred.
    """
    t0 = datetime(2026, 11, 1, 8, 0, tzinfo=timezone.utc)
    clock = MockClock(t0)
    cycle_id = "cyc_6c_09"
    patient_id = "pat_6c_09"
    await _seed_active_cycle_with_baseline(db_session, patient_id, cycle_id, t0)

    clock.set(t0 + timedelta(hours=168))
    sprint_id = f"sprint_{cycle_id}_w1"
    _seed_seven_day_athena_logs(db_session, cycle_id=cycle_id, sprint_id=sprint_id, start_dt=t0)

    recorded_calls: List[Dict[str, Any]] = []
    gw = _build_stage_6c_mock_gateway(recorded_calls)
    weekly_svc = WeeklyConsiliumService(db=db_session, gateway=gw, now_provider=clock.now)

    result = await weekly_svc.run_weekly_consilium(cycle_id=cycle_id, sprint_id=sprint_id)
    assert result is not None
    assert result.overall_report is not None

    report_repo = ModelReportRepository(db_session)
    exec_logs = report_repo.list_server_only(cycle_id, report_type=WEEKLY_CONSILIUM_EXECUTION_LOG_REPORT_TYPE)
    assert len(exec_logs) == 1
    assert exec_logs[0].content["lab_revision_status"] in ("up_to_date", "no_prior_lab_revision")


@pytest.mark.anyio
async def test_10_multiple_lab_revisions_chronologically_ordered(db_session: Session):
    """
    10. Multiple Lab Revisions are chronologically ordered by time and version:
    When multiple revisions exist across time, they are ordered correctly.
    """
    t0 = datetime(2026, 11, 1, 8, 0, tzinfo=timezone.utc)
    clock = MockClock(t0)
    cycle_id = "cyc_6c_10"
    patient_id = "pat_6c_10"
    await _seed_active_cycle_with_baseline(db_session, patient_id, cycle_id, t0)

    gw = _build_stage_6c_mock_gateway([])
    sched_svc = LabSchedulingService(db=db_session, gateway=gw, now_provider=clock.now)

    # Revision 1
    sched_svc.process_manual_edit(
        cycle_id=cycle_id,
        patient_id=patient_id,
        mutations=[LabMarkerMutationItem(marker_key="ast", display_name="AST", value=22.0, unit="U/L")],
    )
    clock.advance(minutes=16)
    auto_svc = ClinicalAutomationService(db=db_session, gateway=gw, now_provider=clock.now, enable_lab_revision=True)
    await auto_svc.process_due_jobs(discover=True)

    # Revision 2 after cooldown (72h)
    clock.advance(hours=73)
    sched_svc.process_manual_edit(
        cycle_id=cycle_id,
        patient_id=patient_id,
        mutations=[LabMarkerMutationItem(marker_key="alt", display_name="ALT", value=24.0, unit="U/L")],
    )
    clock.advance(minutes=16)
    await auto_svc.process_due_jobs(discover=True)

    weekly_svc = WeeklyConsiliumService(db=db_session, gateway=gw, now_provider=clock.now)
    ctx = weekly_svc._load_lab_revision_context_for_weekly(cycle_id, as_of=clock.now())

    assert ctx["completed_revisions_count"] == 2
    assert ctx["latest_eval_ver"] == 3
    assert "PREVIOUS COMPLETED LAB REVISIONS HISTORY:" in ctx["formatted_text"]


@pytest.mark.anyio
async def test_11_lab_data_at_weekly_boundary_belongs_to_correct_window(db_session: Session):
    """
    11. Lab data at weekly boundary belongs to the correct window:
    Labs received after sprint boundary do NOT affect current sprint report.
    """
    t0 = datetime(2026, 11, 1, 8, 0, tzinfo=timezone.utc)
    clock = MockClock(t0)
    cycle_id = "cyc_6c_11"
    patient_id = "pat_6c_11"
    await _seed_active_cycle_with_baseline(db_session, patient_id, cycle_id, t0)

    gw = _build_stage_6c_mock_gateway([])
    sched_svc = LabSchedulingService(db=db_session, gateway=gw, now_provider=clock.now)

    # Lab 1 inside Sprint 1 (at T0 + 50h)
    clock.set(t0 + timedelta(hours=50))
    resp1 = sched_svc.process_manual_edit(
        cycle_id=cycle_id,
        patient_id=patient_id,
        mutations=[LabMarkerMutationItem(marker_key="glucose", display_name="Glucose", value=5.9, unit="mmol/L")],
    )
    assert resp1.change_type == LabChangeType.MEANINGFUL_CHANGE
    assert resp1.ledger_version == 2

    # Lab 2 after Sprint 1 boundary (at T0 + 175h)
    clock.set(t0 + timedelta(hours=175))
    resp2 = sched_svc.process_manual_edit(
        cycle_id=cycle_id,
        patient_id=patient_id,
        mutations=[LabMarkerMutationItem(marker_key="creatinine", display_name="Creatinine", value=95.0, unit="umol/L")],
    )
    assert resp2.change_type == LabChangeType.MEANINGFUL_CHANGE
    assert resp2.ledger_version == 3

    sprint_id = f"sprint_{cycle_id}_w1"
    _seed_seven_day_athena_logs(db_session, cycle_id=cycle_id, sprint_id=sprint_id, start_dt=t0)

    # Run Weekly Consilium for Sprint 1 (reference date at boundary T0 + 168h)
    boundary_dt = t0 + timedelta(hours=168)
    weekly_svc = WeeklyConsiliumService(db=db_session, gateway=gw, now_provider=lambda: boundary_dt)
    result = await weekly_svc.run_weekly_consilium(
        cycle_id=cycle_id,
        sprint_id=sprint_id,
        report_date=boundary_dt.strftime("%Y-%m-%d"),
    )
    assert result is not None

    report_repo = ModelReportRepository(db_session)
    exec_logs = report_repo.list_server_only(cycle_id, report_type=WEEKLY_CONSILIUM_EXECUTION_LOG_REPORT_TYPE)
    # Sprint 1 cutoff is at boundary_dt: ledger v2 (glucose) is included, but ledger v3 (creatinine at 175h) is excluded
    assert exec_logs[0].content["lab_ledger_version"] == 2


@pytest.mark.anyio
async def test_12_rerunning_weekly_does_not_duplicate_reports(db_session: Session):
    """
    12. Re-running Weekly does not duplicate reports:
    Idempotent recovery returns existing result with zero AI calls.
    """
    t0 = datetime(2026, 11, 1, 8, 0, tzinfo=timezone.utc)
    clock = MockClock(t0)
    cycle_id = "cyc_6c_12"
    patient_id = "pat_6c_12"
    await _seed_active_cycle_with_baseline(db_session, patient_id, cycle_id, t0)

    clock.set(t0 + timedelta(hours=168))
    sprint_id = f"sprint_{cycle_id}_w1"
    _seed_seven_day_athena_logs(db_session, cycle_id=cycle_id, sprint_id=sprint_id, start_dt=t0)

    recorded_calls: List[Dict[str, Any]] = []
    gw = _build_stage_6c_mock_gateway(recorded_calls)
    weekly_svc = WeeklyConsiliumService(db=db_session, gateway=gw, now_provider=clock.now)

    idemp_key = "idemp_weekly_sprint_1_test"
    res1 = await weekly_svc.run_weekly_consilium(
        cycle_id=cycle_id,
        sprint_id=sprint_id,
        idempotency_key=idemp_key,
    )
    assert len(recorded_calls) == 2  # Flash + Pro

    # Second run with same idempotency key
    recorded_calls.clear()
    res2 = await weekly_svc.run_weekly_consilium(
        cycle_id=cycle_id,
        sprint_id=sprint_id,
        idempotency_key=idemp_key,
    )
    assert len(recorded_calls) == 0  # Zero AI calls
    assert res1.overall_report.report_id == res2.overall_report.report_id


def test_13_server_restart_preserves_pending_revisions_and_queue(tmp_path: Path):
    """
    13. Server restart preserves pending revisions and queue:
    Durable persistence in SQLite survives process termination and restart.
    """
    db_file = tmp_path / "test_restart.db"
    engine = create_engine(f"sqlite:///{db_file}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    SessionMaker = sessionmaker(bind=engine)

    t0 = datetime(2026, 11, 1, 8, 0, tzinfo=timezone.utc)
    cycle_id = "cyc_6c_13"
    patient_id = "pat_6c_13"

    with SessionMaker() as session:
        _seed_patient_and_cycle(session, patient_id, cycle_id, t0)
        gw = _build_stage_6c_mock_gateway([])
        sched_svc = LabSchedulingService(db=session, gateway=gw, now_provider=lambda: t0)
        sched_svc.process_manual_edit(
            cycle_id=cycle_id,
            patient_id=patient_id,
            mutations=[LabMarkerMutationItem(marker_key="glucose", display_name="Glucose", value=5.5, unit="mmol/L")],
        )

    engine.dispose()

    # Simulate restart
    engine_reopened = create_engine(f"sqlite:///{db_file}", connect_args={"check_same_thread": False})
    SessionMakerReopened = sessionmaker(bind=engine_reopened)
    with SessionMakerReopened() as session_after:
        sched_svc_after = LabSchedulingService(db=session_after, gateway=gw, now_provider=lambda: t0)
        task = sched_svc_after.get_pending_task(cycle_id)
        assert task is not None
        assert task.status == "pending"
        assert task.target_ledger_version == 1
    engine_reopened.dispose()


@pytest.mark.anyio
async def test_14_server_configuration_explicitly_enables_lab_revision(db_session: Session):
    """
    14. Server configuration explicitly enables background Lab Revision:
    Verifies AEGIS_ENABLE_LAB_REVISION configuration in Settings, Worker, and Service.
    """
    assert hasattr(settings, "enable_lab_revision")

    t0 = datetime(2026, 11, 1, 8, 0, tzinfo=timezone.utc)
    cycle_id = "cyc_6c_14"
    patient_id = "pat_6c_14"
    await _seed_active_cycle_with_baseline(db_session, patient_id, cycle_id, t0)

    gw = _build_stage_6c_mock_gateway([])
    sched_svc = LabSchedulingService(db=db_session, gateway=gw, now_provider=lambda: t0)
    sched_svc.process_manual_edit(
        cycle_id=cycle_id,
        patient_id=patient_id,
        mutations=[LabMarkerMutationItem(marker_key="glucose", display_name="Glucose", value=5.8, unit="mmol/L")],
    )

    # 1. When enable_lab_revision=False -> worker does NOT discover/process lab revision
    auto_svc_disabled = ClinicalAutomationService(
        db=db_session, gateway=gw, now_provider=lambda: t0 + timedelta(minutes=20), enable_lab_revision=False
    )
    processed_disabled = await auto_svc_disabled.process_due_jobs(discover=True)
    assert not any(j.job_type == AutomationJobType.LAB_REVISION for j in processed_disabled)

    # 2. When enable_lab_revision=True -> worker discovers and executes
    auto_svc_enabled = ClinicalAutomationService(
        db=db_session, gateway=gw, now_provider=lambda: t0 + timedelta(minutes=20), enable_lab_revision=True
    )
    processed_enabled = await auto_svc_enabled.process_due_jobs(discover=True)
    assert any(j.job_type == AutomationJobType.LAB_REVISION and j.status == AutomationJobStatus.COMPLETED for j in processed_enabled)


@pytest.mark.anyio
async def test_15_eight_flash_lite_not_called_inside_weekly(db_session: Session):
    """
    15. Eight Flash-Lite are not called inside Weekly:
    Weekly Consilium executes strictly Gemini Flash (Deputy) -> Gemini Pro (Chief) (exactly 2 calls).
    """
    t0 = datetime(2026, 11, 1, 8, 0, tzinfo=timezone.utc)
    clock = MockClock(t0)
    cycle_id = "cyc_6c_15"
    patient_id = "pat_6c_15"
    await _seed_active_cycle_with_baseline(db_session, patient_id, cycle_id, t0)

    clock.set(t0 + timedelta(hours=168))
    sprint_id = f"sprint_{cycle_id}_w1"
    _seed_seven_day_athena_logs(db_session, cycle_id=cycle_id, sprint_id=sprint_id, start_dt=t0)

    recorded_calls: List[Dict[str, Any]] = []
    gw = _build_stage_6c_mock_gateway(recorded_calls)
    weekly_svc = WeeklyConsiliumService(db=db_session, gateway=gw, now_provider=clock.now)

    await weekly_svc.run_weekly_consilium(cycle_id=cycle_id, sprint_id=sprint_id)

    assert len(recorded_calls) == 2
    # Verify no Flash-Lite specialist prompts
    assert not any("the '" in c["prompt"] for c in recorded_calls)
    assert "You are the Head of Department" in recorded_calls[0]["prompt"]
    assert "You are the Chief Physician of the Aegis expert system." in recorded_calls[1]["prompt"]


@pytest.mark.anyio
async def test_16_internal_lab_revision_conclusions_remain_server_only(db_session: Session):
    """
    16. Internal Lab Revision conclusions remain SERVER_ONLY:
    background_lab_revision_report and lab_revision_execution_log are strictly SERVER_ONLY.
    """
    t0 = datetime(2026, 11, 1, 8, 0, tzinfo=timezone.utc)
    clock = MockClock(t0)
    cycle_id = "cyc_6c_16"
    patient_id = "pat_6c_16"
    await _seed_active_cycle_with_baseline(db_session, patient_id, cycle_id, t0)

    gw = _build_stage_6c_mock_gateway([])
    sched_svc = LabSchedulingService(db=db_session, gateway=gw, now_provider=clock.now)
    sched_svc.process_manual_edit(
        cycle_id=cycle_id,
        patient_id=patient_id,
        mutations=[LabMarkerMutationItem(marker_key="glucose", display_name="Glucose", value=5.4, unit="mmol/L")],
    )
    clock.advance(minutes=16)
    auto_svc = ClinicalAutomationService(db=db_session, gateway=gw, now_provider=clock.now, enable_lab_revision=True)
    await auto_svc.process_due_jobs(discover=True)

    report_repo = ModelReportRepository(db_session)
    client_reports = report_repo.list_client_facing(cycle_id)
    assert not any(r.report_type == BACKGROUND_LAB_REVISION_REPORT_TYPE for r in client_reports)
    assert not any(r.report_type == "lab_revision_execution_log" for r in client_reports)

    server_reports = report_repo.list_server_only(cycle_id, report_type=BACKGROUND_LAB_REVISION_REPORT_TYPE)
    assert len(server_reports) == 1
    assert server_reports[0].privacy_level == PrivacyLevel.SERVER_ONLY


@pytest.mark.anyio
async def test_17_weekly_user_report_does_not_leak_internal_materials(db_session: Session):
    """
    17. Weekly user report does not leak internal prompts or technical structures:
    user_overall_report and 8 system reports contain only client-safe friendly summaries.
    """
    t0 = datetime(2026, 11, 1, 8, 0, tzinfo=timezone.utc)
    clock = MockClock(t0)
    cycle_id = "cyc_6c_17"
    patient_id = "pat_6c_17"
    await _seed_active_cycle_with_baseline(db_session, patient_id, cycle_id, t0)

    clock.set(t0 + timedelta(hours=168))
    sprint_id = f"sprint_{cycle_id}_w1"
    _seed_seven_day_athena_logs(db_session, cycle_id=cycle_id, sprint_id=sprint_id, start_dt=t0)

    gw = _build_stage_6c_mock_gateway([])
    weekly_svc = WeeklyConsiliumService(db=db_session, gateway=gw, now_provider=clock.now)
    resp = await weekly_svc.run_weekly_consilium(cycle_id=cycle_id, sprint_id=sprint_id)

    # Inspect client overall report
    overall_dict = resp.overall_report.content.model_dump() if hasattr(resp.overall_report.content, "model_dump") else resp.overall_report.content
    overall_json = json.dumps(overall_dict, ensure_ascii=False)
    assert "shelf" not in overall_json.lower()
    assert "sandbox" not in overall_json.lower()
    assert "You are the Chief Physician" not in overall_json

    # Inspect client system reports
    for sk, sys_rep in resp.system_reports.items():
        sys_dict = sys_rep.content.model_dump() if hasattr(sys_rep.content, "model_dump") else sys_rep.content
        sys_json = json.dumps(sys_dict, ensure_ascii=False)
        assert "training_shelf" not in sys_json
        assert "system_general_shelf" not in sys_json


@pytest.mark.anyio
async def test_18_full_e2e_lifecycle(db_session: Session):
    """
    18. Full E2E Lifecycle:
    Primary Consilium -> Lab Upload -> Debounce -> 10-Model Lab Revision -> 7-Day Journals ->
    Weekly Consilium (Flash -> Pro) -> Athena Weekly Plan Week 2 -> Client Wrapup Message.
    """
    t0 = datetime(2026, 11, 1, 8, 0, tzinfo=timezone.utc)
    clock = MockClock(t0)
    cycle_id = "cyc_6c_18"
    patient_id = "pat_6c_18"
    await _seed_active_cycle_with_baseline(db_session, patient_id, cycle_id, t0)

    recorded_calls: List[Dict[str, Any]] = []
    gw = _build_stage_6c_mock_gateway(recorded_calls)

    # 1. Patient uploads new lab report at T0 + 24h
    clock.advance(hours=24)
    sched_svc = LabSchedulingService(db=db_session, gateway=gw, now_provider=clock.now)
    resp = sched_svc.process_manual_edit(
        cycle_id=cycle_id,
        patient_id=patient_id,
        mutations=[
            LabMarkerMutationItem(marker_key="glucose", display_name="Glucose", value=5.3, unit="mmol/L", sample_date="2026-11-02"),
            LabMarkerMutationItem(marker_key="uric_acid", display_name="Uric Acid", value=320.0, unit="umol/L", sample_date="2026-11-02"),
        ],
    )
    assert resp.change_type == LabChangeType.MEANINGFUL_CHANGE

    # 2. Advance 16 min -> automated 10-model Lab Revision
    clock.advance(minutes=16)
    auto_svc = ClinicalAutomationService(db=db_session, gateway=gw, now_provider=clock.now, enable_lab_revision=True)
    processed = await auto_svc.process_due_jobs(discover=True)
    assert any(j.job_type == AutomationJobType.LAB_REVISION and j.status == AutomationJobStatus.COMPLETED for j in processed)

    # 3. Collect Athena logs across 7 days
    sprint_id = f"sprint_{cycle_id}_w1"
    _seed_seven_day_athena_logs(db_session, cycle_id=cycle_id, sprint_id=sprint_id, start_dt=t0)

    # 4. Advance clock to T0 + 168h -> trigger automatic weekly sprint automation
    clock.set(t0 + timedelta(hours=168))
    auto_weekly = ClinicalAutomationService(
        db=db_session, gateway=gw, now_provider=clock.now, enable_lab_revision=True, enable_automatic_weekly=True
    )

    # Step A: Weekly Consilium
    p1 = await auto_weekly.process_due_jobs(discover=True)
    assert any(j.job_type == AutomationJobType.WEEKLY_CONSILIUM_A and j.status == AutomationJobStatus.COMPLETED for j in p1)

    # Step B: Athena Weekly Plan
    p2 = await auto_weekly.process_due_jobs(discover=True)
    assert any(j.job_type == AutomationJobType.WEEKLY_CONSILIUM_B and j.status == AutomationJobStatus.COMPLETED for j in p2)

    # Step C: Client Wrapup Message
    p3 = await auto_weekly.process_due_jobs(discover=True)
    assert any(j.job_type == AutomationJobType.WEEKLY_CONSILIUM_C and j.status == AutomationJobStatus.COMPLETED for j in p3)

    # 5. Verify Step B created Athena Weekly Plan for Week 2 linked to strategy v2
    report_repo = ModelReportRepository(db_session)
    plans = report_repo.list_server_only(cycle_id, report_type=ATHENA_WEEKLY_PLAN_REPORT_TYPE)
    assert len(plans) == 1
    assert plans[0].content["week_number"] == 2
    assert plans[0].content["source_strategy_version"] == 2

    # 6. Verify Step C created client wrap-up message
    from app.services.athena_chat_service import AthenaChatService
    chat_svc = AthenaChatService(db=db_session, gateway=gw, now_provider=clock.now)
    history = chat_svc.get_sanitized_history(patient_id=patient_id, cycle_id=cycle_id)
    assert any("Завершилась первая неделя" in (m.text or "") for m in history.messages)


@pytest.mark.anyio
async def test_19_lab_saved_before_cutoff_included_even_if_revision_completes_after(db_session: Session):
    """
    19. Weekly Cutoff Invariance (Audit Requirement 2B):
    - A lab saved to ledger before weekly cutoff (T0 + 160h <= T0 + 168h) MUST be included
      in Sprint 1 Weekly Consilium even if Lab Revision completes later (T0 + 170h).
    - Sprint 1 Weekly Consilium evaluates the new marker directly (in unreviewed_pending_labs).
    - The Lab Revision completed at T0 + 170h does NOT retroactively affect Sprint 1.
    - Sprint 2 Weekly Consilium (T0 + 336h) correctly includes the completed Lab Revision from T0 + 170h.
    """
    t0 = datetime(2026, 11, 1, 8, 0, tzinfo=timezone.utc)
    clock = MockClock(t0)
    cycle_id = "cyc_6c_19"
    patient_id = "pat_6c_19"
    await _seed_active_cycle_with_baseline(db_session, patient_id, cycle_id, t0)

    recorded_calls: List[Dict[str, Any]] = []
    gw = _build_stage_6c_mock_gateway(recorded_calls)

    # 1. Patient saves new lab at T0 + 160h (before Sprint 1 cutoff at T0 + 168h) -> creates ledger v2
    clock.advance(hours=160)
    sched_svc = LabSchedulingService(db=db_session, gateway=gw, now_provider=clock.now)
    resp = sched_svc.process_manual_edit(
        cycle_id=cycle_id,
        patient_id=patient_id,
        mutations=[
            LabMarkerMutationItem(
                marker_key="ferritin",
                display_name="Ferritin",
                value=450.0,
                unit="mcg/L",
                sample_date="2026-11-07",
            )
        ],
    )
    assert resp.change_type == LabChangeType.MEANINGFUL_CHANGE
    assert resp.ledger_version == 2

    # 2. Advance to T0 + 168h (Sprint 1 cutoff). Lab Revision has NOT run yet.
    clock.set(t0 + timedelta(hours=168))
    sprint_1_id = f"sprint_{cycle_id}_w1"
    _seed_seven_day_athena_logs(db_session, cycle_id=cycle_id, sprint_id=sprint_1_id, start_dt=t0)

    weekly_svc = WeeklyConsiliumService(db=db_session, gateway=gw, now_provider=clock.now)

    # Check Sprint 1 context as of T0 + 168h
    prev_ctx_w1 = weekly_svc._load_previous_consilium_context(cycle_id, as_of=t0 + timedelta(hours=168))
    assert prev_ctx_w1["latest_ledger_version"] == 2
    assert prev_ctx_w1["evaluated_ledger_version"] == 1
    assert prev_ctx_w1["has_unreviewed_lab_changes"] is True
    assert "ferritin" in prev_ctx_w1["unreviewed_pending_labs"]
    assert prev_ctx_w1["unreviewed_pending_labs"]["ferritin"]["value"] == 450.0
    # No Lab Revision has completed yet
    assert prev_ctx_w1["lab_revision_context"]["latest_report_id"] is None

    # Run Sprint 1 Weekly Consilium
    w1_result = await weekly_svc.run_weekly_consilium(cycle_id=cycle_id, sprint_id=sprint_1_id)
    assert w1_result is not None
    assert w1_result.sprint_number == 1

    report_repo = ModelReportRepository(db_session)
    exec_logs = report_repo.list_server_only(cycle_id, report_type=WEEKLY_CONSILIUM_EXECUTION_LOG_REPORT_TYPE)
    assert len(exec_logs) >= 1
    w1_exec = exec_logs[0].content or {}
    assert w1_exec.get("lab_ledger_version") == 2
    assert w1_exec.get("lab_revision_report_id") is None
    assert w1_exec.get("lab_revision_status") in ("pending_revision_in_cooldown", "pending_revision_debounce", "no_prior_lab_revision")

    # 3. Now at T0 + 170h (AFTER Sprint 1 cutoff), automated 10-model Lab Revision completes
    clock.set(t0 + timedelta(hours=170))
    auto_svc = ClinicalAutomationService(db=db_session, gateway=gw, now_provider=clock.now, enable_lab_revision=True)
    jobs = await auto_svc.process_due_jobs(discover=True)
    assert any(j.job_type == AutomationJobType.LAB_REVISION and j.status == AutomationJobStatus.COMPLETED for j in jobs)

    # Verify that querying context as of Sprint 1 cutoff (T0 + 168h) remains invariant!
    retro_ctx_w1 = weekly_svc._load_previous_consilium_context(cycle_id, as_of=t0 + timedelta(hours=168))
    assert retro_ctx_w1["evaluated_ledger_version"] == 1  # Revision at 170h must NOT retroactively affect cutoff 168h
    assert retro_ctx_w1["lab_revision_context"]["latest_report_id"] is None

    # 4. Now at T0 + 336h (Sprint 2 cutoff), Weekly Consilium for Week 2 runs
    clock.set(t0 + timedelta(hours=336))
    ctx_w2 = weekly_svc._load_previous_consilium_context(cycle_id, as_of=t0 + timedelta(hours=336))
    assert ctx_w2["latest_ledger_version"] == 2
    assert ctx_w2["evaluated_ledger_version"] == 2  # The 170h revision is now recognized
    assert ctx_w2["lab_revision_context"]["latest_report_id"] is not None
    assert ctx_w2["lab_revision_context"]["latest_eval_ver"] == 2

