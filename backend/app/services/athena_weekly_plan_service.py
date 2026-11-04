import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from sqlalchemy.orm import Session

from app.models.health_measurement import HealthMeasurement
from app.models.model_report import ModelReport
from app.repositories.report_repository import ModelReportRepository
from app.schemas.chat import (
    AthenaGoalItem,
    AthenaGoalProgressionDecision,
    AthenaGoalStatus,
    AthenaPlanExecutionLogEntry,
    AthenaWeeklyPlanRecord,
)
from app.schemas.privacy import PrivacyLevel
from app.services.consilium_prompts import build_athena_weekly_plan_prompt
from app.services.gemini_gateway import GeminiGateway, GeminiModelRole, GeminiValidationError

logger = logging.getLogger("aegis.athena_weekly_plan")

ATHENA_WEEKLY_PLAN_REPORT_TYPE: str = "athena_weekly_plan"
ATHENA_WEEKLY_PLAN_MODEL: str = "aegis-athena-weekly-plan"
ATHENA_ACTIVE_STRATEGY_REPORT_TYPE: str = "athena_active_strategy"
CHIEF_PHYSICIAN_INTERNAL_REPORT_TYPE: str = "chief_physician_internal"
PREPARATORY_ANAMNESIS_REPORT_TYPE: str = "preparatory_anamnesis_m1"
RAW_USER_ANAMNESIS_REPORT_TYPE: str = "raw_user_anamnesis"
ATHENA_PLAN_EXECUTION_LOG_REPORT_TYPE: str = "athena_plan_execution_log"
ATHENA_ROUTINE_QUESTION_REPORT_TYPE: str = "athena_routine_question"
ATHENA_SCHEDULE_ANCHOR_REPORT_TYPE: str = "athena_schedule_anchor"


class AthenaWeeklyPlanService:
    """
    Server-side service managing Athena's autonomous 7-day observation plan (Stage 1).
    Invariants:
    1. Plan consists of priority observation goals based on clinical necessity,
       not a rigid set of 7 daily questions.
    2. Stored strictly under server_only privacy level in model_reports; never exposed to user.
    3. Fully idempotent: once a plan for (cycle_id, week_number) is generated, subsequent calls
       reuse the persisted plan with zero additional Gemini calls.
    4. Upon validation failure from Gemini, rejects partial/corrupted plans and raises an error
       so the caller or automation worker can retry safely.
    """

    def __init__(
        self,
        db: Session,
        gateway: Optional[GeminiGateway] = None,
        now_provider: Optional[Callable[[], datetime]] = None,
    ):
        self.db = db
        self.gateway = gateway or GeminiGateway()
        self.now_provider = now_provider or (lambda: datetime.now(timezone.utc))
        self.report_repo = ModelReportRepository(db)

    def _now_utc(self) -> datetime:
        dt = self.now_provider()
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)

    def get_active_weekly_plan(
        self, cycle_id: str, week_number: Optional[int] = None
    ) -> Optional[AthenaWeeklyPlanRecord]:
        """Returns the latest persisted weekly plan record for cycle_id."""
        clean_cycle = cycle_id.strip()
        reports = self.report_repo.list_server_only(
            cycle_id=clean_cycle,
            report_type=ATHENA_WEEKLY_PLAN_REPORT_TYPE,
        )
        if not reports:
            return None

        # Filter by week_number if provided, otherwise pick latest
        for r in reports:
            content = r.content or {}
            if week_number is not None and content.get("week_number") != week_number:
                continue
            try:
                return AthenaWeeklyPlanRecord.model_validate(content)
            except Exception as exc:
                logger.warning(f"Corrupted athena_weekly_plan content in report {r.report_id}: {exc}")
                continue
        return None

    def get_current_active_goal(
        self, cycle_id: str, week_number: Optional[int] = None
    ) -> Optional[AthenaGoalItem]:
        """
        Retrieves the highest priority active observation goal from the current weekly plan.
        Prioritizes lowest numerical priority rank (1 is highest priority).
        """
        plan = self.get_active_weekly_plan(cycle_id, week_number=week_number)
        if not plan or not plan.goals:
            return None

        active_goals = [
            g for g in plan.goals if g.status == AthenaGoalStatus.ACTIVE
        ]
        if not active_goals:
            # Fallback to any goal if all completed
            return plan.goals[0] if plan.goals else None

        active_goals.sort(key=lambda g: g.priority)
        return active_goals[0]

    def _gather_pro_strategy(self, cycle_id: str) -> tuple[str, Optional[str], Optional[int]]:
        """Resolves Chief Physician Pro strategy text and source report metadata."""
        reports = self.report_repo.list_server_only(cycle_id=cycle_id)
        # 1. Try athena_active_strategy
        for r in reports:
            if r.report_type == ATHENA_ACTIVE_STRATEGY_REPORT_TYPE:
                content = r.content or {}
                text = content.get("active_strategy_text") or content.get("strategy_text")
                if text and str(text).strip():
                    return str(text).strip(), r.report_id, r.version

        # 2. Try chief_physician_internal
        for r in reports:
            if r.report_type == CHIEF_PHYSICIAN_INTERNAL_REPORT_TYPE:
                content = r.content or {}
                shelf = content.get("pro_general_shelf")
                if shelf and str(shelf).strip():
                    return str(shelf).strip(), r.report_id, r.version

        # Fallback default strategy
        default_strat = (
            "Установить первичное доверительное наблюдение, уточнить базовое самочувствие, "
            "режим сна и реакцию на умеренную активность без постановки диагнозов."
        )
        return default_strat, None, None

    def _gather_known_facts(self, cycle_id: str) -> List[str]:
        """Extracts established facts from preparatory anamnesis to prevent repeat questions."""
        facts: List[str] = []
        reports = self.report_repo.list_server_only(cycle_id=cycle_id)

        # Check preparatory_anamnesis_m1
        for r in reports:
            if r.report_type == PREPARATORY_ANAMNESIS_REPORT_TYPE:
                content = r.content or {}
                extracted = content.get("extracted_facts") or []
                if isinstance(extracted, list):
                    facts.extend(str(f).strip() for f in extracted if str(f).strip())
                summary = content.get("summary")
                if summary:
                    facts.append(f"Анамнез: {summary}")
                lifestyle = content.get("lifestyle") or {}
                if isinstance(lifestyle, dict):
                    for k, v in lifestyle.items():
                        if v:
                            facts.append(f"{k}: {v}")
                allergies = content.get("allergies")
                if allergies:
                    facts.append(f"Аллергии: {allergies}")
                complaints = content.get("complaints")
                if isinstance(complaints, list):
                    facts.extend(f"Жалоба: {c}" for c in complaints if c)

        # Check raw_user_anamnesis
        for r in reports:
            if r.report_type == RAW_USER_ANAMNESIS_REPORT_TYPE:
                content = r.content or {}
                answers = content.get("answers") or {}
                if answers.get("age"):
                    facts.append(f"Возраст: {answers.get('age')}")
                if answers.get("gender"):
                    facts.append(f"Пол: {answers.get('gender')}")
                if answers.get("chronic_diseases") and str(answers.get("chronic_diseases")).lower() != "none":
                    facts.append(f"Указанные хронические особенности: {answers.get('chronic_diseases')}")
                if answers.get("medications") and str(answers.get("medications")).lower() != "none":
                    facts.append(f"Принимаемые препараты: {answers.get('medications')}")

        return list(dict.fromkeys(facts))  # Unique preserved order

    def _gather_objective_measurements(self, cycle_id: str) -> str:
        """Summarizes known baseline measurements."""
        measurements = (
            self.db.query(HealthMeasurement)
            .filter(HealthMeasurement.cycle_id == cycle_id)
            .all()
        )
        if not measurements:
            return "Объективные измерения пока отсутствуют."

        pulses = [m.pulse for m in measurements if m.pulse is not None]
        bps = [f"{m.systolic}/{m.diastolic}" for m in measurements if m.systolic and m.diastolic]
        parts: List[str] = []
        if pulses:
            parts.append(f"Зафиксированный пульс: min {min(pulses)}, max {max(pulses)} bpm")
        if bps:
            parts.append(f"Последние замеры АД: {', '.join(bps[-3:])}")
        return "; ".join(parts) if parts else "Базовые измерения в норме."

    async def create_or_get_weekly_plan(
        self, cycle_id: str, week_number: int = 1
    ) -> AthenaWeeklyPlanRecord:
        """
        Creates or retrieves Athena's weekly plan for (cycle_id, week_number).
        If already persisted, reuses it without calling Gemini.
        If generating, invokes Gemini Flash with structured validation.
        """
        clean_cycle = cycle_id.strip()
        existing = self.get_active_weekly_plan(clean_cycle, week_number=week_number)
        if existing is not None:
            return existing

        strategy_text, rep_id, rep_ver = self._gather_pro_strategy(clean_cycle)
        known_facts = self._gather_known_facts(clean_cycle)
        measurements_summary = self._gather_objective_measurements(clean_cycle)
        identified_gaps: List[str] = []
        if not measurements_summary or "отсутствуют" in measurements_summary:
            identified_gaps.append("Нет исходных данных пульса в покое и динамики АД")

        prompt = build_athena_weekly_plan_prompt(
            cycle_id=clean_cycle,
            week_number=week_number,
            pro_strategy_text=strategy_text,
            known_anamnesis_facts=known_facts,
            objective_measurements_summary=measurements_summary,
            identified_gaps=identified_gaps,
        )

        plan = await self.gateway.generate_structured(
            prompt=prompt,
            response_schema=AthenaWeeklyPlanRecord,
            model=GeminiModelRole.FLASH,
        )

        # Enforce cycle and metadata consistency
        now_iso = self._now_utc().isoformat()
        normalized_plan = AthenaWeeklyPlanRecord(
            plan_id=plan.plan_id or f"plan_{clean_cycle}_w{week_number}_{uuid.uuid4().hex[:8]}",
            cycle_id=clean_cycle,
            week_number=week_number,
            source_strategy_report_id=rep_id,
            source_strategy_version=rep_ver,
            created_at_utc=now_iso,
            summary_for_curator=plan.summary_for_curator,
            goals=plan.goals,
            status="active",
        )

        if not normalized_plan.goals:
            raise GeminiValidationError("Athena weekly plan must contain at least 1 goal.")

        # Persist safely under SERVER_ONLY
        self.report_repo.create_next_version(
            cycle_id=clean_cycle,
            model=self.gateway.flash_model,
            report_type=ATHENA_WEEKLY_PLAN_REPORT_TYPE,
            privacy_level=PrivacyLevel.SERVER_ONLY,
            content=normalized_plan.model_dump(),
            commit=True,
        )

        return normalized_plan

    def record_goal_question_asked(
        self, cycle_id: str, goal_id: str, commit: bool = True
    ) -> Optional[AthenaGoalItem]:
        """Increments question counter for the specified goal in the active plan."""
        clean_cycle = cycle_id.strip()
        reports = self.report_repo.list_server_only(
            cycle_id=clean_cycle,
            report_type=ATHENA_WEEKLY_PLAN_REPORT_TYPE,
        )
        if not reports:
            return None

        latest_rep = reports[0]
        content = dict(latest_rep.content or {})
        try:
            plan = AthenaWeeklyPlanRecord.model_validate(content)
        except Exception:
            return None

        matched_goal: Optional[AthenaGoalItem] = None
        updated_goals: List[AthenaGoalItem] = []
        for g in plan.goals:
            if g.goal_id == goal_id:
                updated_g = g.model_copy(update={"questions_asked_count": g.questions_asked_count + 1})
                matched_goal = updated_g
                updated_goals.append(updated_g)
            else:
                updated_goals.append(g)

        if matched_goal is None:
            return None

        updated_plan = plan.model_copy(update={"goals": updated_goals})
        self.report_repo.create_next_version(
            cycle_id=clean_cycle,
            model=self.gateway.flash_model,
            report_type=ATHENA_WEEKLY_PLAN_REPORT_TYPE,
            privacy_level=PrivacyLevel.SERVER_ONLY,
            content=updated_plan.model_dump(),
            commit=commit,
        )
        return matched_goal

    def update_goal_status(
        self,
        cycle_id: str,
        goal_id: str,
        status: AthenaGoalStatus,
        progress_notes: Optional[str] = None,
        commit: bool = True,
    ) -> Optional[AthenaGoalItem]:
        """Updates status and progress notes for a goal."""
        clean_cycle = cycle_id.strip()
        reports = self.report_repo.list_server_only(
            cycle_id=clean_cycle,
            report_type=ATHENA_WEEKLY_PLAN_REPORT_TYPE,
        )
        if not reports:
            return None

        latest_rep = reports[0]
        content = dict(latest_rep.content or {})
        try:
            plan = AthenaWeeklyPlanRecord.model_validate(content)
        except Exception:
            return None

        matched_goal: Optional[AthenaGoalItem] = None
        updated_goals: List[AthenaGoalItem] = []
        for g in plan.goals:
            if g.goal_id == goal_id:
                updates: Dict[str, Any] = {"status": status}
                if progress_notes is not None:
                    updates["progress_notes"] = progress_notes
                updated_g = g.model_copy(update=updates)
                matched_goal = updated_g
                updated_goals.append(updated_g)
            else:
                updated_goals.append(g)

        if matched_goal is None:
            return None

        updated_plan = plan.model_copy(update={"goals": updated_goals})
        self.report_repo.create_next_version(
            cycle_id=clean_cycle,
            model=self.gateway.flash_model,
            report_type=ATHENA_WEEKLY_PLAN_REPORT_TYPE,
            privacy_level=PrivacyLevel.SERVER_ONLY,
            content=updated_plan.model_dump(),
            commit=commit,
        )
        return matched_goal

    def get_next_active_goal(
        self,
        cycle_id: str,
        current_goal_id: Optional[str] = None,
        week_number: Optional[int] = None,
    ) -> Optional[AthenaGoalItem]:
        """
        Retrieves the next active observation goal (status == ACTIVE)
        excluding current_goal_id, prioritized by lowest numerical rank.
        """
        plan = self.get_active_weekly_plan(cycle_id, week_number=week_number)
        if not plan or not plan.goals:
            return None

        active_goals = [
            g for g in plan.goals
            if g.status == AthenaGoalStatus.ACTIVE and (current_goal_id is None or g.goal_id != current_goal_id)
        ]
        if not active_goals:
            return None

        active_goals.sort(key=lambda g: g.priority)
        return active_goals[0]

    def are_all_goals_completed(
        self, cycle_id: str, week_number: Optional[int] = None
    ) -> bool:
        """
        Returns True if all goals in the active weekly plan are either COMPLETED or UNRESOLVED
        (i.e. no ACTIVE or DEFERRED goals remaining).
        """
        plan = self.get_active_weekly_plan(cycle_id, week_number=week_number)
        if not plan or not plan.goals:
            return False

        return all(
            g.status in (AthenaGoalStatus.COMPLETED, AthenaGoalStatus.UNRESOLVED)
            for g in plan.goals
        )

    def resolve_daily_window(
        self, cycle_id: str, as_of: Optional[datetime] = None
    ) -> Tuple[datetime, datetime, int, int]:
        """
        Resolves the 24-hour observation window [window_start, window_end)
        relative to schedule anchor T0: [T0 + k*24h, T0 + (k+1)*24h).
        Returns (window_start, window_end, sprint_number, sprint_day).
        """
        clean_cycle = cycle_id.strip()
        ref_dt = as_of or self._now_utc()
        if ref_dt.tzinfo is None:
            ref_dt = ref_dt.replace(tzinfo=timezone.utc)
        else:
            ref_dt = ref_dt.astimezone(timezone.utc)

        anchor_reports = self.report_repo.list_server_only(
            cycle_id=clean_cycle,
            report_type=ATHENA_SCHEDULE_ANCHOR_REPORT_TYPE,
        )
        anchor_dt: Optional[datetime] = None
        if anchor_reports:
            c = anchor_reports[0].content or {}
            raw_anchor = c.get("anchor_timestamp_utc") or c.get("anchor_dt")
            if raw_anchor:
                try:
                    anchor_dt = datetime.fromisoformat(str(raw_anchor).replace("Z", "+00:00"))
                    if anchor_dt.tzinfo is None:
                        anchor_dt = anchor_dt.replace(tzinfo=timezone.utc)
                except Exception:
                    anchor_dt = None

        if anchor_dt is not None:
            elapsed_seconds = (ref_dt - anchor_dt).total_seconds()
            if elapsed_seconds >= 0:
                k = int(elapsed_seconds // 86400)
            else:
                k = -1
            window_start = anchor_dt + timedelta(days=k)
            window_end = anchor_dt + timedelta(days=k + 1)
            sprint_number = max(1, (k // 7) + 1) if k >= 0 else 1
            sprint_day = max(1, (k % 7) + 1) if k >= 0 else 1
        else:
            window_start = ref_dt.replace(hour=0, minute=0, second=0, microsecond=0)
            window_end = window_start + timedelta(days=1)
            sprint_number = 1
            sprint_day = 1

        return window_start, window_end, sprint_number, sprint_day

    def get_daily_planned_questions_count(
        self, cycle_id: str, as_of: Optional[datetime] = None
    ) -> int:
        """
        Authoritatively counts the number of distinct planned questions/clarifications
        asked within the active 24-hour window [T0 + k*24h, T0 + (k+1)*24h).
        """
        clean_cycle = cycle_id.strip()
        window_start, window_end, _, _ = self.resolve_daily_window(clean_cycle, as_of=as_of)

        reports = self.report_repo.list_server_only(
            cycle_id=clean_cycle,
            report_type=ATHENA_ROUTINE_QUESTION_REPORT_TYPE,
        )
        if not reports:
            return 0

        seen_qids = set()
        for r in reports:
            c = r.content or {}
            qid = c.get("question_id")
            if not qid:
                continue
            asked_at_str = c.get("asked_at_utc")
            dt: Optional[datetime] = None
            if asked_at_str:
                try:
                    dt = datetime.fromisoformat(str(asked_at_str).replace("Z", "+00:00"))
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                except Exception:
                    dt = None
            if dt is None and r.created_at:
                dt = r.created_at if r.created_at.tzinfo else r.created_at.replace(tzinfo=timezone.utc)

            if dt is not None and (window_start <= dt < window_end):
                seen_qids.add(qid)

        return len(seen_qids)

    def get_remaining_daily_budget(
        self, cycle_id: str, as_of: Optional[datetime] = None, max_daily: int = 3
    ) -> int:
        asked = self.get_daily_planned_questions_count(cycle_id, as_of=as_of)
        return max(0, max_daily - asked)

    def record_plan_execution_log(
        self,
        *,
        cycle_id: str,
        goal_id: str,
        goal_title: str,
        goal_status: AthenaGoalStatus,
        questions_asked_count: int,
        transition_reason: str,
        unresolved_points: Optional[List[str]] = None,
        next_action: str,
        sprint_number: int = 1,
        sprint_day: int = 1,
        as_of: Optional[datetime] = None,
        commit: bool = True,
    ) -> AthenaPlanExecutionLogEntry:
        """
        Records a technical execution log entry for Athena's weekly plan progression.
        Persisted under report_type='athena_plan_execution_log', SERVER_ONLY.
        Strictly isolated from Weekly Consilium.
        """
        clean_cycle = cycle_id.strip()
        now_dt = as_of or self._now_utc()
        now_iso = now_dt.isoformat()
        log_id = f"log_exec_{clean_cycle}_s{sprint_number}_d{sprint_day}_{uuid.uuid4().hex[:8]}"

        entry = AthenaPlanExecutionLogEntry(
            log_id=log_id,
            cycle_id=clean_cycle,
            sprint_number=sprint_number,
            sprint_day=sprint_day,
            goal_id=goal_id,
            goal_title=goal_title,
            goal_status=goal_status,
            questions_asked_count=questions_asked_count,
            transition_reason=transition_reason,
            unresolved_points=list(unresolved_points or []),
            next_action=next_action,
            timestamp_utc=now_iso,
        )

        self.report_repo.create_next_version(
            cycle_id=clean_cycle,
            model=self.gateway.flash_model,
            report_type=ATHENA_PLAN_EXECUTION_LOG_REPORT_TYPE,
            privacy_level=PrivacyLevel.SERVER_ONLY,
            content=entry.model_dump(),
            commit=commit,
        )
        return entry

    def list_plan_execution_logs(self, cycle_id: str) -> List[AthenaPlanExecutionLogEntry]:
        """Lists technical execution log entries for cycle_id in chronological order."""
        clean_cycle = cycle_id.strip()
        reports = self.report_repo.list_server_only(
            cycle_id=clean_cycle,
            report_type=ATHENA_PLAN_EXECUTION_LOG_REPORT_TYPE,
        )
        entries: List[AthenaPlanExecutionLogEntry] = []
        for r in reversed(reports):
            try:
                entries.append(AthenaPlanExecutionLogEntry.model_validate(r.content or {}))
            except Exception:
                continue
        return entries

    def mark_goal_completed(
        self, cycle_id: str, goal_id: str, notes: Optional[str] = None, commit: bool = True
    ) -> Optional[AthenaGoalItem]:
        """Marks goal as COMPLETED."""
        return self.update_goal_status(
            cycle_id=cycle_id,
            goal_id=goal_id,
            status=AthenaGoalStatus.COMPLETED,
            progress_notes=notes,
            commit=commit,
        )

    def defer_goal(
        self, cycle_id: str, goal_id: str, reason: Optional[str] = None, commit: bool = True
    ) -> Optional[AthenaGoalItem]:
        """Postpones goal to next day (DEFERRED)."""
        return self.update_goal_status(
            cycle_id=cycle_id,
            goal_id=goal_id,
            status=AthenaGoalStatus.DEFERRED,
            progress_notes=reason,
            commit=commit,
        )

    def mark_goal_unresolved(
        self, cycle_id: str, goal_id: str, reason: Optional[str] = None, commit: bool = True
    ) -> Optional[AthenaGoalItem]:
        """Marks goal as UNRESOLVED when further questioning is clinically inappropriate."""
        return self.update_goal_status(
            cycle_id=cycle_id,
            goal_id=goal_id,
            status=AthenaGoalStatus.UNRESOLVED,
            progress_notes=reason,
            commit=commit,
        )

    def add_substantive_goal(
        self,
        cycle_id: str,
        goal_item: AthenaGoalItem,
        rationale: str,
        commit: bool = True,
    ) -> Optional[AthenaGoalItem]:
        """
        Appends a new substantive goal to the active weekly plan only if
        all prior goals are completed/unresolved AND a non-empty clinical rationale is provided.
        """
        clean_cycle = cycle_id.strip()
        if not rationale or not rationale.strip():
            raise ValueError("Cannot add substantive goal without a non-empty clinical rationale.")

        plan = self.get_active_weekly_plan(clean_cycle)
        if not plan:
            raise ValueError(f"No active weekly plan found for cycle '{clean_cycle}'.")

        if not self.are_all_goals_completed(clean_cycle):
            raise ValueError("Cannot add new substantive goal while prior active/deferred goals remain in progress.")

        updated_goals = list(plan.goals) + [goal_item]
        updated_plan = plan.model_copy(update={"goals": updated_goals})

        self.report_repo.create_next_version(
            cycle_id=clean_cycle,
            model=self.gateway.flash_model,
            report_type=ATHENA_WEEKLY_PLAN_REPORT_TYPE,
            privacy_level=PrivacyLevel.SERVER_ONLY,
            content=updated_plan.model_dump(),
            commit=commit,
        )
        return goal_item
