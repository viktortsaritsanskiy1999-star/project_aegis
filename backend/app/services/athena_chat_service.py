from datetime import datetime, timezone
import asyncio
import threading
import time
import json
import logging
import re
from typing import Any, Callable, Dict, List, Optional, Tuple
import uuid
import weakref
from fastapi import HTTPException, status
from sqlalchemy import text as sql_text
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from app.models.model_report import ModelReport
from app.repositories.cycle_repository import MedicalCycleRepository
from app.repositories.report_repository import (
    ModelReportRepository,
    ReportVersionConflictError,
)
from app.schemas.chat import (
    AthenaChatRequest,
    AthenaChatResponse,
    AthenaChatMessageItem,
    AthenaChatHistoryResponse,
    AthenaVitalsAlertContext,
)
from app.schemas.consilium import (
    AegisAgentRole,
    AthenaClinicalLogEntry,
)
from app.schemas.privacy import PrivacyLevel
from app.services.consilium_prompts import build_athena_system_prompt
from app.services.gemini_gateway import GeminiGateway, GeminiModelRole
from app.services.primary_consilium_service import (
    ConsiliumContextMissingError,
    PREPARATORY_ANAMNESIS_REPORT_TYPE,
    RAW_USER_ANAMNESIS_REPORT_TYPE,
)
from app.services.weekly_consilium_service import (
    WeeklyConsiliumService,
    ATHENA_CHAT_HISTORY_REPORT_TYPE,
)

logger = logging.getLogger("aegis.athena_chat")

ATHENA_CHAT_TURN_REPORT_TYPE = "athena_chat_turn"
ATHENA_ROUTINE_QUESTION_REPORT_TYPE = "athena_routine_question"
POST_PRIMARY_INIT_IDEMPOTENCY_PREFIX = "athena_init_greeting_"
DEFAULT_MAX_ROUTINE_REMINDERS = 2
DEFAULT_REMINDER_COOLDOWN_SECONDS = 0

# Regex for stripping [CLINICAL_REPORT] JSON block
CLINICAL_REPORT_REGEX = re.compile(r"\[CLINICAL_REPORT\]([\s\S]*?)\[/CLINICAL_REPORT\]", re.IGNORECASE)
EMERGENCY_TRIGGER_REGEX = re.compile(r"\[EMERGENCY_TRIGGER\]", re.IGNORECASE)

# Keywords indicating acute emergency triggers in user messages
ACUTE_EMERGENCY_KEYWORDS = (
    "боль в груди",
    "давит в груди",
    "жжение в груди",
    "задыхаюсь",
    "не могу дышать",
    "нехватка воздуха",
    "потерял сознание",
    "падаю в обморок",
    "сильное кровотечение",
    "вызовите скорую",
    "нужна скорая",
    "chest pain",
    "cannot breathe",
    "loss of consciousness",
    "fainting",
    "call ambulance",
)


class AthenaChatService:
    """
    Server-side AI Gateway service managing conversational interactions with Athena (CuratorAgent).
    Invariants:
    1. Server strictly resolves `patient_id` from verified DeviceSession and active MedicalCycle.
       Client cannot forge or override patient or cycle identifiers.
    2. Re-uses the clinical rules, legal restrictions, and prompt templates of CuratorAgent.
    3. Strictly partitions dialogue history from hidden [CLINICAL_REPORT] entries.
    4. Never echoes internal system prompts, clinical scores, or server_only reports to the client.
    5. Evaluates EMERGENCY_TRIGGER as a structured signal, with fail-safe protocol independent
       of Gemini API uptime.
    6. Guarantees idempotent message submission with zero duplicate AI calls or messages on retry.
    """
    _cycle_locks: Dict[str, asyncio.Lock] = {}
    _loop_cycle_locks: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, Dict[str, asyncio.Lock]]" = (
        weakref.WeakKeyDictionary()
    )
    _locks_guard: threading.Lock = threading.Lock()

    def _get_cycle_lock(self, cycle_id: str) -> asyncio.Lock:
        with self._locks_guard:
            if self._lock_registry is not None:
                if cycle_id not in self._lock_registry:
                    self._lock_registry[cycle_id] = asyncio.Lock()
                return self._lock_registry[cycle_id]
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                if cycle_id not in self._cycle_locks:
                    self._cycle_locks[cycle_id] = asyncio.Lock()
                return self._cycle_locks[cycle_id]
            loop_locks = self._loop_cycle_locks.get(loop)
            if loop_locks is None:
                loop_locks = {}
                self._loop_cycle_locks[loop] = loop_locks
            if cycle_id not in loop_locks:
                loop_locks[cycle_id] = asyncio.Lock()
            return loop_locks[cycle_id]

    def __init__(
        self,
        db: Session,
        gateway: Optional[GeminiGateway] = None,
        now_provider: Optional[Callable[[], datetime]] = None,
        lock_registry: Optional[Dict[str, asyncio.Lock]] = None,
        reminder_cooldown_seconds: int = DEFAULT_REMINDER_COOLDOWN_SECONDS,
    ):
        self.db = db
        self.gateway = gateway or GeminiGateway()
        self.now_provider = now_provider
        self._lock_registry = lock_registry
        self.reminder_cooldown_seconds = reminder_cooldown_seconds
        self.cycle_repo = MedicalCycleRepository(db)
        self.report_repo = ModelReportRepository(db)
        self.weekly_service = WeeklyConsiliumService(
            db, gateway=self.gateway, now_provider=now_provider
        )

    def get_latest_routine_question_record(self, cycle_id: str) -> Optional[Dict[str, Any]]:
        """Retrieves the latest version of the routine question record for the cycle."""
        reports = self.report_repo.list_server_only(
            cycle_id=cycle_id,
            report_type=ATHENA_ROUTINE_QUESTION_REPORT_TYPE,
        )
        if not reports:
            return None
        return reports[0].content or {}

    def get_active_routine_question(self, cycle_id: str) -> Optional[Dict[str, Any]]:
        """
        Returns the active routine question dict if there is currently an unanswered
        routine question awaiting patient response; otherwise None.
        """
        rec = self.get_latest_routine_question_record(cycle_id)
        if rec and rec.get("status") == "unanswered":
            return rec
        return None

    def has_unanswered_routine_question(self, cycle_id: str) -> bool:
        """Returns True if there is an outstanding unanswered routine question for cycle_id."""
        return self.get_active_routine_question(cycle_id) is not None

    async def handle_turn(
        self,
        *,
        patient_id: str,
        request: AthenaChatRequest,
        cycle_id: Optional[str] = None,
    ) -> AthenaChatResponse:
        clean_patient_id = patient_id.strip()

        # 1. Resolve patient's active medical cycle atomically
        if cycle_id is not None and cycle_id.strip():
            clean_cycle_id = cycle_id.strip()
            cycle = self.cycle_repo.get(clean_cycle_id)
            if cycle is None or cycle.patient_id != clean_patient_id:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Medical cycle '{clean_cycle_id}' not found for active patient.",
                )
        else:
            cycle, _ = self.cycle_repo.get_or_create_active_for_patient(clean_patient_id)
        resolved_cycle_id = cycle.cycle_id

        async with self._get_cycle_lock(resolved_cycle_id):
            # 2. Idempotency Check: if client submitted an idempotency_key, check for existing turn
            clean_idem: Optional[str] = (
                request.idempotency_key.strip()
                if (request.idempotency_key and request.idempotency_key.strip())
                else None
            )
            if clean_idem:
                existing_turn = self._find_idempotent_turn(resolved_cycle_id, clean_idem)
                if existing_turn is not None:
                    return existing_turn

            if request.event_type == "initial_greeting":
                existing_greeting = self.find_existing_post_primary_initial_greeting(resolved_cycle_id)
                if existing_greeting is not None:
                    return existing_greeting

            turn_dt = self.weekly_service._now_utc()
            turn_now_iso = turn_dt.isoformat()

            # Handle routine question reminder (Zero Gemini API calls, non-spam bounded)
            if request.event_type in ("routine_reminder", "reminder"):
                active_q = self.get_active_routine_question(resolved_cycle_id)
                if active_q is None:
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail="No unanswered routine question pending reminder.",
                    )
                reminders_cnt = int(active_q.get("reminders_count", 0))
                max_reminders = int(active_q.get("max_reminders", DEFAULT_MAX_ROUTINE_REMINDERS))
                if reminders_cnt >= max_reminders:
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail=f"Maximum routine question reminders ({max_reminders}) already reached.",
                    )
                if self.reminder_cooldown_seconds > 0 and active_q.get("last_reminded_at_utc"):
                    try:
                        last_dt = datetime.fromisoformat(str(active_q["last_reminded_at_utc"]))
                        if (turn_dt - last_dt).total_seconds() < self.reminder_cooldown_seconds:
                            raise HTTPException(
                                status_code=status.HTTP_409_CONFLICT,
                                detail="Routine reminder cooldown in effect. Please try again later.",
                            )
                    except (ValueError, TypeError):
                        pass

                q_text = str(active_q.get("question_text", "")).strip()
                preview = q_text
                if len(preview) > 120:
                    preview = preview[:117] + "..."
                reminder_reply = (
                    f"Напоминание от Афины: когда у вас появится свободная минута, пожалуйста, ответьте на предыдущий вопрос"
                    + (f": «{preview}»" if preview else "")
                    + ". Это важно для непрерывного медицинского сопровождения и корректировки программы."
                )

                message_id = f"msg_{uuid.uuid4().hex[:16]}"
                max_persist_retries = 8
                new_reminders_count = reminders_cnt + 1

                for persist_attempt in range(max_persist_retries + 1):
                    if persist_attempt > 0:
                        self.db.rollback()
                        self.db.expire_all()
                        time.sleep(0.015 * persist_attempt)
                        bind = self.db.get_bind()
                        if bind is not None and bind.dialect.name == "sqlite":
                            self.db.execute(sql_text("BEGIN IMMEDIATE"))
                    elif not self.db.new and not self.db.dirty and not self.db.deleted:
                        self.db.expire_all()

                    try:
                        if clean_idem:
                            existing_turn = self._find_idempotent_turn(resolved_cycle_id, clean_idem)
                            if existing_turn is not None:
                                self.db.rollback()
                                return existing_turn

                        active_sprint_id = self.weekly_service.resolve_active_sprint_id(
                            resolved_cycle_id,
                            as_of=turn_dt,
                            commit=False,
                        )

                        # Re-verify latest active routine question inside transaction attempt
                        curr_active_q = self.get_active_routine_question(resolved_cycle_id)
                        if curr_active_q is None:
                            self.db.rollback()
                            raise HTTPException(
                                status_code=status.HTTP_409_CONFLICT,
                                detail="No unanswered routine question pending reminder.",
                            )
                        curr_cnt = int(curr_active_q.get("reminders_count", 0))
                        if curr_cnt >= max_reminders:
                            self.db.rollback()
                            raise HTTPException(
                                status_code=status.HTTP_409_CONFLICT,
                                detail=f"Maximum routine question reminders ({max_reminders}) already reached.",
                            )

                        new_reminders_count = curr_cnt + 1
                        updated_q = dict(curr_active_q)
                        updated_q["reminders_count"] = new_reminders_count
                        updated_q["last_reminded_at_utc"] = turn_now_iso

                        self.report_repo.create_next_version(
                            cycle_id=resolved_cycle_id,
                            sprint_id=active_sprint_id,
                            model=self.gateway.flash_model,
                            report_type=ATHENA_ROUTINE_QUESTION_REPORT_TYPE,
                            privacy_level=PrivacyLevel.SERVER_ONLY,
                            content=updated_q,
                            commit=False,
                        )

                        self.weekly_service.save_athena_chat_message(
                            cycle_id=resolved_cycle_id,
                            sender="curator",
                            text=reminder_reply,
                            sprint_id=active_sprint_id,
                            timestamp=turn_now_iso,
                            commit=False,
                        )

                        base_turn_ver = self.report_repo.get_latest_version_number(
                            cycle_id=resolved_cycle_id,
                            report_type=ATHENA_CHAT_TURN_REPORT_TYPE,
                            privacy_level=PrivacyLevel.SERVER_ONLY,
                        )

                        if clean_idem:
                            self._save_idempotency_turn(
                                cycle_id=resolved_cycle_id,
                                sprint_id=active_sprint_id,
                                idempotency_key=clean_idem,
                                message_id=message_id,
                                reply=reminder_reply,
                                emergency_trigger=False,
                                emergency_notice=None,
                                created_at=turn_now_iso,
                                sprint_day=curr_active_q.get("sprint_day"),
                                sprint_number=curr_active_q.get("sprint_number"),
                                event_type=request.event_type,
                                question_id=curr_active_q.get("question_id"),
                                routine_question_status="unanswered",
                                reminders_count=new_reminders_count,
                                expected_version=base_turn_ver + 1,
                                commit=False,
                            )

                        self.db.commit()
                        break
                    except (ReportVersionConflictError, IntegrityError, OperationalError) as exc:
                        self.db.rollback()
                        if persist_attempt < max_persist_retries:
                            continue
                        raise ReportVersionConflictError(
                            f"Concurrent persistence conflict during Athena reminder for cycle '{resolved_cycle_id}': {exc}"
                        ) from exc
                    except BaseException:
                        self.db.rollback()
                        raise

                return AthenaChatResponse(
                    reply=reminder_reply,
                    emergency_trigger=False,
                    emergency_notice=None,
                    cycle_id=resolved_cycle_id,
                    sprint_day=active_q.get("sprint_day"),
                    sprint_number=active_q.get("sprint_number"),
                    message_id=message_id,
                    created_at=turn_now_iso,
                    question_id=active_q.get("question_id"),
                    routine_question_status="unanswered",
                    reminders_count=new_reminders_count,
                )

            # Enforce One Outstanding Routine Question:
            # Cannot trigger a new daily_pulse without a patient reply while previous routine question is unanswered
            if request.event_type == "daily_pulse":
                has_patient_reply = bool(request.message and request.message.strip())
                active_q = self.get_active_routine_question(resolved_cycle_id)
                if active_q is not None and active_q.get("status") == "unanswered":
                    if not has_patient_reply:
                        raise HTTPException(
                            status_code=status.HTTP_409_CONFLICT,
                            detail=(
                                f"Cannot generate new routine question: outstanding routine question "
                                f"'{active_q.get('question_id')}' for sprint {active_q.get('sprint_number')} "
                                f"day {active_q.get('sprint_day')} is unanswered."
                            ),
                        )

            # Server-authoritative calendar schedule resolution & validation
            auth_week, auth_day, _ = self.weekly_service.get_authoritative_calendar_schedule(
                resolved_cycle_id, as_of=turn_dt
            )

            effective_sprint_number = request.sprint_number
            effective_sprint_day = request.sprint_day

            if request.event_type == "daily_pulse":
                if auth_week is not None and auth_day is not None:
                    if request.sprint_number is not None and request.sprint_number > auth_week:
                        raise HTTPException(
                            status_code=status.HTTP_409_CONFLICT,
                            detail=(
                                f"Cannot execute Daily Pulse for sprint {request.sprint_number}: "
                                f"current authoritative calendar week is {auth_week}."
                            ),
                        )
                    if (
                        request.sprint_number is not None
                        and request.sprint_number == auth_week
                        and request.sprint_day is not None
                        and request.sprint_day > auth_day
                    ):
                        raise HTTPException(
                            status_code=status.HTTP_409_CONFLICT,
                            detail=(
                                f"Cannot execute Daily Pulse for day {request.sprint_day}: "
                                f"current authoritative calendar day is {auth_day}."
                            ),
                        )
                    if effective_sprint_number is None:
                        effective_sprint_number = auth_week
                    if effective_sprint_day is None:
                        effective_sprint_day = auth_day
                else:
                    if effective_sprint_number is None:
                        effective_sprint_number = 1
                    if effective_sprint_day is None:
                        effective_sprint_day = 1
            else:
                if effective_sprint_number is None and auth_week is not None:
                    effective_sprint_number = auth_week
                if effective_sprint_day is None and auth_day is not None:
                    effective_sprint_day = auth_day

            # 3. Rule-based pre-emergency evaluation (vital signs thresholds & acute keywords)
            pre_emergency, pre_emergency_notice = self._evaluate_pre_emergency(request)

            # 4. Strategy & System Instruction Resolution
            system_prompt, chat_history, _ = self._resolve_strategy_context(
                resolved_cycle_id, as_of=turn_dt
            )

            # 5. Formulate Prompt & Clean Record Text based on event_type
            prompt_text, user_record_text = self._build_turn_prompt_and_record(
                request,
                effective_sprint_day=effective_sprint_day,
            )

            # 6. Build Multi-Turn Conversation Contents for Gemini
            contents = self._build_gemini_contents(chat_history, prompt_text)

            # 7. Execute AI Generation with Emergency Fail-Safe Fallback
            raw_response, used_emergency_fallback = await self._execute_generation_with_fallback(
                contents=contents,
                system_instruction=system_prompt,
                pre_emergency=pre_emergency,
            )

            # 8-10. Atomic Turn Persistence with Cross-Process Optimistic Retry
            message_id = f"msg_{uuid.uuid4().hex[:16]}"
            max_persist_retries = 8
            clean_reply: str = ""
            emergency_triggered: bool = False
            emergency_notice: Optional[str] = None
            final_question_id: Optional[str] = None
            final_routine_status: Optional[str] = None
            final_reminders_count: Optional[int] = None

            for persist_attempt in range(max_persist_retries + 1):
                if persist_attempt > 0:
                    self.db.rollback()
                    self.db.expire_all()
                    time.sleep(0.015 * persist_attempt)
                    bind = self.db.get_bind()
                    if bind is not None and bind.dialect.name == "sqlite":
                        self.db.execute(sql_text("BEGIN IMMEDIATE"))
                elif not self.db.new and not self.db.dirty and not self.db.deleted:
                    self.db.expire_all()

                try:
                    base_turn_ver = self.report_repo.get_latest_version_number(
                        cycle_id=resolved_cycle_id,
                        report_type=ATHENA_CHAT_TURN_REPORT_TYPE,
                        privacy_level=PrivacyLevel.SERVER_ONLY,
                    )

                    # Re-check idempotency inside transaction attempt in case a concurrent
                    # worker with the same idempotency_key committed while Gemini was running.
                    if clean_idem:
                        existing_turn = self._find_idempotent_turn(resolved_cycle_id, clean_idem)
                        if existing_turn is not None:
                            self.db.rollback()
                            return existing_turn

                    if request.event_type == "initial_greeting":
                        existing_greeting = self.find_existing_post_primary_initial_greeting(resolved_cycle_id)
                        if existing_greeting is not None:
                            self.db.rollback()
                            return existing_greeting

                    active_sprint_id = self.weekly_service.resolve_active_sprint_id(
                        resolved_cycle_id,
                        as_of=turn_dt,
                        commit=False,
                    )

                    # 8. Post-Processing: Extract Structured Signals, Sanitize Reply & Flush Clinical Log
                    clean_reply, emergency_triggered, emergency_notice = self._process_and_persist_reports(
                        cycle_id=resolved_cycle_id,
                        sprint_id=active_sprint_id,
                        raw_response=raw_response,
                        pre_emergency=pre_emergency,
                        pre_emergency_notice=pre_emergency_notice,
                        used_emergency_fallback=used_emergency_fallback,
                        event_type=request.event_type,
                        as_of=turn_dt,
                        commit=False,
                    )

                    # 9. Flush Dialogue Turn to Server Chat History on top of latest messages
                    self.weekly_service.save_athena_chat_message(
                        cycle_id=resolved_cycle_id,
                        sender="user",
                        text=user_record_text,
                        sprint_id=active_sprint_id,
                        timestamp=turn_now_iso,
                        commit=False,
                    )
                    self.weekly_service.save_athena_chat_message(
                        cycle_id=resolved_cycle_id,
                        sender="curator",
                        text=clean_reply,
                        sprint_id=active_sprint_id,
                        timestamp=turn_now_iso,
                        commit=False,
                    )

                    # 10. Update / Record routine question state
                    turn_question_id: Optional[str] = None
                    turn_routine_status: Optional[str] = None
                    turn_reminders_count: Optional[int] = None

                    # 10. Update / Record routine question state
                    turn_question_id: Optional[str] = None
                    turn_routine_status: Optional[str] = None
                    turn_reminders_count: Optional[int] = None

                    has_patient_reply = bool(request.message and request.message.strip())

                    # If this turn contains a patient reply, mark previous outstanding question as answered
                    if request.event_type in ("routine_answer", "user_message") or (
                        request.event_type == "daily_pulse" and has_patient_reply
                    ):
                        prev_active_q = self.get_active_routine_question(resolved_cycle_id)
                        if prev_active_q is not None and prev_active_q.get("status") == "unanswered":
                            turn_question_id = prev_active_q.get("question_id")
                            turn_routine_status = "answered"
                            turn_reminders_count = prev_active_q.get("reminders_count", 0)
                            updated_q = dict(prev_active_q)
                            updated_q["status"] = "answered"
                            updated_q["answered_at_utc"] = turn_now_iso
                            updated_q["answer_text"] = user_record_text
                            updated_q["answer_message_id"] = message_id
                            self.report_repo.create_next_version(
                                cycle_id=resolved_cycle_id,
                                sprint_id=active_sprint_id,
                                model=self.gateway.flash_model,
                                report_type=ATHENA_ROUTINE_QUESTION_REPORT_TYPE,
                                privacy_level=PrivacyLevel.SERVER_ONLY,
                                content=updated_q,
                                commit=False,
                            )

                    # If this turn formulates a new routine question, record it as unanswered
                    if request.event_type in ("initial_greeting", "daily_pulse"):
                        turn_question_id = f"rq_{resolved_cycle_id}_s{effective_sprint_number or 1}_d{effective_sprint_day or 1}_{uuid.uuid4().hex[:8]}"
                        turn_routine_status = "unanswered"
                        turn_reminders_count = 0
                        new_routine_q = {
                            "question_id": turn_question_id,
                            "cycle_id": resolved_cycle_id,
                            "patient_id": clean_patient_id,
                            "sprint_number": effective_sprint_number or 1,
                            "sprint_day": effective_sprint_day or 1,
                            "status": "unanswered",
                            "question_text": clean_reply,
                            "message_id": message_id,
                            "asked_at_utc": turn_now_iso,
                            "reminders_count": 0,
                            "max_reminders": DEFAULT_MAX_ROUTINE_REMINDERS,
                            "last_reminded_at_utc": None,
                            "answered_at_utc": None,
                            "answer_text": None,
                            "answer_message_id": None,
                        }
                        self.report_repo.create_next_version(
                            cycle_id=resolved_cycle_id,
                            sprint_id=active_sprint_id,
                            model=self.gateway.flash_model,
                            report_type=ATHENA_ROUTINE_QUESTION_REPORT_TYPE,
                            privacy_level=PrivacyLevel.SERVER_ONLY,
                            content=new_routine_q,
                            commit=False,
                        )
                    elif request.event_type not in ("routine_answer", "user_message"):
                        # vitals_alert, wearable_event, out_of_turn_message
                        active_q = self.get_active_routine_question(resolved_cycle_id)
                        if active_q is not None and active_q.get("status") == "unanswered":
                            turn_question_id = active_q.get("question_id")
                            turn_routine_status = "unanswered"
                            turn_reminders_count = active_q.get("reminders_count", 0)

                    # Record schedule anchor if this turn is the first confirmed clinical message/question AFTER Primary Consilium
                    from app.services.primary_consilium_service import PrimaryConsiliumService

                    primary_service = PrimaryConsiliumService(
                        db=self.db, gateway=self.gateway, now_provider=self.now_provider
                    )
                    if (
                        request.event_type in ("initial_greeting", "daily_pulse")
                        and primary_service.is_primary_consilium_completed(resolved_cycle_id)
                    ):
                        if self.weekly_service.find_schedule_anchor_timestamp(resolved_cycle_id) is None:
                            self.weekly_service.record_schedule_anchor(
                                cycle_id=resolved_cycle_id,
                                anchor_dt=turn_dt,
                                source=f"{request.event_type}_turn",
                                sprint_id=active_sprint_id,
                                commit=False,
                            )

                    # 11. Flush Idempotency Turn Record
                    effective_idem = clean_idem
                    if (
                        effective_idem is None
                        and request.event_type == "initial_greeting"
                        and primary_service.is_primary_consilium_completed(resolved_cycle_id)
                    ):
                        effective_idem = f"{POST_PRIMARY_INIT_IDEMPOTENCY_PREFIX}{resolved_cycle_id}"

                    if effective_idem:
                        self._save_idempotency_turn(
                            cycle_id=resolved_cycle_id,
                            sprint_id=active_sprint_id,
                            idempotency_key=effective_idem,
                            message_id=message_id,
                            reply=clean_reply,
                            emergency_trigger=emergency_triggered,
                            emergency_notice=emergency_notice,
                            created_at=turn_now_iso,
                            sprint_day=effective_sprint_day,
                            sprint_number=effective_sprint_number,
                            event_type=request.event_type,
                            question_id=turn_question_id,
                            routine_question_status=turn_routine_status,
                            reminders_count=turn_reminders_count,
                            expected_version=base_turn_ver + 1,
                            commit=False,
                        )

                    final_question_id = turn_question_id
                    final_routine_status = turn_routine_status
                    final_reminders_count = turn_reminders_count

                    # Single atomic commit for all artifacts of the turn
                    self.db.commit()
                    break
                except (ReportVersionConflictError, IntegrityError, OperationalError) as exc:
                    self.db.rollback()
                    if persist_attempt < max_persist_retries:
                        continue
                    if isinstance(exc, ReportVersionConflictError):
                        raise
                    raise ReportVersionConflictError(
                        f"Concurrent persistence conflict during Athena turn for cycle '{resolved_cycle_id}': {exc}"
                    ) from exc
                except BaseException:
                    self.db.rollback()
                    raise

            return AthenaChatResponse(
                reply=clean_reply,
                emergency_trigger=emergency_triggered,
                emergency_notice=emergency_notice,
                cycle_id=resolved_cycle_id,
                sprint_day=effective_sprint_day,
                sprint_number=effective_sprint_number,
                message_id=message_id,
                created_at=turn_now_iso,
                question_id=final_question_id,
                routine_question_status=final_routine_status,
                reminders_count=final_reminders_count,
            )

    def get_sanitized_history(
        self,
        *,
        patient_id: str,
        cycle_id: Optional[str] = None,
    ) -> AthenaChatHistoryResponse:
        """
        Retrieves sanitized chat history for the patient's active cycle.
        Contains strictly user and curator messages, never exposing internal clinical logs.
        """
        clean_patient_id = patient_id.strip()
        if cycle_id is not None and cycle_id.strip():
            clean_cycle_id = cycle_id.strip()
            cycle = self.cycle_repo.get(clean_cycle_id)
            if cycle is None or cycle.patient_id != clean_patient_id:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Medical cycle '{clean_cycle_id}' not found for active patient.",
                )
        else:
            cycle, _ = self.cycle_repo.get_or_create_active_for_patient(clean_patient_id)
        raw_messages = self.weekly_service.get_athena_chat_history(cycle.cycle_id)

        sanitized_items = [
            AthenaChatMessageItem(
                sender=str(m.get("sender", "curator")),
                text=str(m.get("text", "")),
                timestamp=str(m.get("timestamp", "")),
            )
            for m in raw_messages
            if isinstance(m, dict) and str(m.get("text", "")).strip()
        ]
        return AthenaChatHistoryResponse(
            cycle_id=cycle.cycle_id,
            messages=sanitized_items,
        )

    # ==========================================================================
    # INTERNAL HELPERS
    # ==========================================================================

    def find_existing_post_primary_initial_greeting(
        self, cycle_id: str
    ) -> Optional[AthenaChatResponse]:
        """
        Checks if a valid post-Primary Consilium `initial_greeting` turn and schedule
        anchor T0 already exist for `cycle_id`. If so, returns the existing response
        so neither the automation worker nor client retries ever duplicate the initial
        greeting or shift T0.
        """
        from app.services.primary_consilium_service import PrimaryConsiliumService

        primary_service = PrimaryConsiliumService(
            db=self.db, gateway=self.gateway, now_provider=self.now_provider
        )
        if not primary_service.is_primary_consilium_completed(cycle_id):
            return None

        anchor_dt = self.weekly_service.find_schedule_anchor_timestamp(cycle_id)
        if anchor_dt is None:
            return None

        anchor_iso = anchor_dt.isoformat()
        turn_reports = self.report_repo.list_server_only(
            cycle_id=cycle_id,
            report_type=ATHENA_CHAT_TURN_REPORT_TYPE,
        )
        for r in reversed(turn_reports):
            content = r.content or {}
            ev_type = content.get("event_type")
            idem_key = str(content.get("idempotency_key") or "")
            created_at_str = str(content.get("created_at") or "")
            if (
                ev_type == "initial_greeting"
                or idem_key.startswith(POST_PRIMARY_INIT_IDEMPOTENCY_PREFIX)
                or created_at_str == anchor_iso
            ):
                return AthenaChatResponse(
                    reply=str(content.get("reply", "")),
                    emergency_trigger=bool(content.get("emergency_trigger", False)),
                    emergency_notice=content.get("emergency_notice"),
                    cycle_id=cycle_id,
                    sprint_day=content.get("sprint_day"),
                    sprint_number=content.get("sprint_number"),
                    message_id=str(content.get("message_id", r.report_id)),
                    created_at=created_at_str or r.created_at.isoformat(),
                    question_id=content.get("question_id"),
                    routine_question_status=content.get("routine_question_status", "unanswered"),
                    reminders_count=content.get("reminders_count", 0),
                )

        # Fallback if an initial_greeting after Primary was saved in chat history without a turn report
        history = self.weekly_service.get_athena_chat_history(cycle_id)
        for idx, msg in enumerate(history):
            if (
                isinstance(msg, dict)
                and msg.get("sender") == "user"
                and msg.get("text") == "[Initialization]: First introduction"
                and idx + 1 < len(history)
            ):
                reply_msg = history[idx + 1]
                if isinstance(reply_msg, dict) and reply_msg.get("sender") == "curator":
                    ts = str(reply_msg.get("timestamp") or anchor_iso)
                    return AthenaChatResponse(
                        reply=str(reply_msg.get("text", "")),
                        emergency_trigger=False,
                        emergency_notice=None,
                        cycle_id=cycle_id,
                        sprint_day=1,
                        sprint_number=1,
                        message_id=f"msg_reused_{cycle_id[:16]}",
                        created_at=ts,
                    )
        return None

    def _find_idempotent_turn(self, cycle_id: str, idempotency_key: str) -> Optional[AthenaChatResponse]:
        reports = self.report_repo.list_server_only(
            cycle_id=cycle_id,
            report_type=ATHENA_CHAT_TURN_REPORT_TYPE,
        )
        for r in reports:
            content = r.content or {}
            if content.get("idempotency_key") == idempotency_key:
                return AthenaChatResponse(
                    reply=str(content.get("reply", "")),
                    emergency_trigger=bool(content.get("emergency_trigger", False)),
                    emergency_notice=content.get("emergency_notice"),
                    cycle_id=cycle_id,
                    sprint_day=content.get("sprint_day"),
                    sprint_number=content.get("sprint_number"),
                    message_id=str(content.get("message_id", r.report_id)),
                    created_at=str(content.get("created_at", r.created_at.isoformat())),
                    question_id=content.get("question_id"),
                    routine_question_status=content.get("routine_question_status"),
                    reminders_count=content.get("reminders_count"),
                )
        return None

    def _save_idempotency_turn(
        self,
        *,
        cycle_id: str,
        sprint_id: Optional[str],
        idempotency_key: str,
        message_id: str,
        reply: str,
        emergency_trigger: bool,
        emergency_notice: Optional[str],
        created_at: str,
        sprint_day: Optional[int],
        sprint_number: Optional[int] = None,
        event_type: Optional[str] = None,
        question_id: Optional[str] = None,
        routine_question_status: Optional[str] = None,
        reminders_count: Optional[int] = None,
        expected_version: Optional[int] = None,
        commit: bool = True,
    ) -> ModelReport:
        resolved_expected_ver = expected_version
        if resolved_expected_ver is None and not commit:
            latest_ver = self.report_repo.get_latest_version_number(
                cycle_id=cycle_id,
                report_type=ATHENA_CHAT_TURN_REPORT_TYPE,
                privacy_level=PrivacyLevel.SERVER_ONLY,
            )
            resolved_expected_ver = latest_ver + 1

        return self.report_repo.create_next_version(
            cycle_id=cycle_id,
            sprint_id=sprint_id,
            model=self.gateway.flash_model,
            report_type=ATHENA_CHAT_TURN_REPORT_TYPE,
            privacy_level=PrivacyLevel.SERVER_ONLY,
            content={
                "agent_role": AegisAgentRole.ATHENA_CURATOR.value,
                "idempotency_key": idempotency_key,
                "event_type": event_type,
                "message_id": message_id,
                "reply": reply,
                "emergency_trigger": emergency_trigger,
                "emergency_notice": emergency_notice,
                "created_at": created_at,
                "sprint_day": sprint_day,
                "sprint_number": sprint_number,
                "question_id": question_id,
                "routine_question_status": routine_question_status,
                "reminders_count": reminders_count,
            },
            expected_version=resolved_expected_ver,
            commit=commit,
        )

    @staticmethod
    def _evaluate_pre_emergency(request: AthenaChatRequest) -> Tuple[bool, Optional[str]]:
        """
        Deterministic, rule-based emergency detection before calling Gemini:
        - Critical pulse spike (>= 150 bpm or <= 40 bpm at rest)
        - Explicit acute medical symptoms in message text
        """
        if request.event_type in ("vitals_alert", "wearable_event") and request.vitals_alert is not None:
            va = request.vitals_alert
            # Heart rate spike >= 150 bpm or severe bradycardia <= 40 bpm
            if va.current_pulse >= 150 or va.current_pulse <= 40:
                return True, f"CRITICAL_VITALS_ALERT: Heart rate {va.current_pulse} bpm exceeds critical safety limits."
            if va.notes:
                lower_notes = va.notes.lower()
                for kw in ACUTE_EMERGENCY_KEYWORDS:
                    if kw in lower_notes:
                        return True, f"CRITICAL_SYMPTOM_ALERT: Detected acute indicator '{kw}' in vitals alert context."

        if request.message:
            lower_msg = request.message.lower()
            for kw in ACUTE_EMERGENCY_KEYWORDS:
                if kw in lower_msg:
                    return True, f"ACUTE_SYMPTOM_ALERT: Detected critical emergency symptom phrase '{kw}'."

        return False, None

    def build_patient_medical_context(self, cycle_id: str) -> str:
        """
        Assembles the authoritative medical context of the active patient cycle:
        1. Anamnesis (M1 validated output or raw questionnaire answers; explicitly noted if missing).
        2. Laboratory findings (active markers from authoritative ledger or OCR fallback; explicitly noted if absent).
        3. ECG examination (structured findings and metrics; explicitly noted if not conducted).
        4. Primary Consilium published client-facing conclusions (overall report + 8 system summaries; explicitly noted if pending).
        Strictly scopes all queries to `cycle_id` to prevent cross-cycle data mixing.
        """
        # 1. Anamnesis
        anamnesis_lines: List[str] = []
        m1_reports = self.report_repo.list_server_only(
            cycle_id=cycle_id,
            report_type=PREPARATORY_ANAMNESIS_REPORT_TYPE,
        )
        if m1_reports:
            m1_content = m1_reports[0].content or {}
            user_view = m1_content.get("user_view") or {}
            summary = user_view.get("summary")
            if summary:
                anamnesis_lines.append(f"- Anamnesis summary: {summary.strip()}")
            system_logic = m1_content.get("system_logic") or {}
            facts: List[str] = []
            for sys_k in sorted(system_logic.keys()):
                organ_val = system_logic[sys_k]
                if isinstance(organ_val, dict):
                    data_str = organ_val.get("data")
                    if data_str and str(data_str).strip():
                        facts.append(f"{sys_k}: {data_str.strip()}")
            if facts:
                anamnesis_lines.append(f"- Clinical facts: {'; '.join(facts)}")
        else:
            raw_reports = self.report_repo.list_server_only(
                cycle_id=cycle_id,
                report_type=RAW_USER_ANAMNESIS_REPORT_TYPE,
            )
            if raw_reports:
                raw_answers = (raw_reports[0].content or {}).get("answers") or {}
                raw_summary_parts: List[str] = []
                if raw_answers.get("age"):
                    raw_summary_parts.append(f"Age: {raw_answers.get('age')}")
                if raw_answers.get("gender"):
                    raw_summary_parts.append(f"Gender: {raw_answers.get('gender')}")
                if raw_answers.get("height") and raw_answers.get("weight"):
                    raw_summary_parts.append(
                        f"Height/Weight: {raw_answers.get('height')} cm / {raw_answers.get('weight')} kg"
                    )
                if raw_answers.get("chronic_diseases") and str(raw_answers.get("chronic_diseases")).lower() != "none":
                    raw_summary_parts.append(f"Chronic diseases: {raw_answers.get('chronic_diseases')}")
                if raw_answers.get("medications") and str(raw_answers.get("medications")).lower() != "none":
                    raw_summary_parts.append(f"Medications: {raw_answers.get('medications')}")
                if raw_answers.get("sleep_quality"):
                    raw_summary_parts.append(f"Sleep quality: {raw_answers.get('sleep_quality')}")
                if raw_answers.get("diet_types"):
                    raw_summary_parts.append(f"Diet: {raw_answers.get('diet_types')}")
                if raw_summary_parts:
                    anamnesis_lines.append(f"- Baseline intake answers: {'; '.join(raw_summary_parts)}")
                else:
                    anamnesis_lines.append("- Baseline intake answers recorded.")
            else:
                anamnesis_lines.append("- Status: Not completed (questionnaire pending).")

        # 2. Laboratory Studies
        from app.services.lab_context_service import assemble_consilium_laboratory_context
        lab_ctx = assemble_consilium_laboratory_context(self.db, cycle_id)
        lab_lines: List[str] = []
        if lab_ctx.shared_labs_table:
            for marker_key in sorted(lab_ctx.shared_labs_table.keys()):
                marker = lab_ctx.shared_labs_table[marker_key]
                if isinstance(marker, dict):
                    disp = marker.get("display_name") or marker_key
                    val = marker.get("value")
                    unit = marker.get("unit") or ""
                    ref = marker.get("reference_range")
                    ref_str = ""
                    if isinstance(ref, dict):
                        ref_text = ref.get("text") or (
                            f"{ref.get('low')}-{ref.get('high')}"
                            if ref.get("low") is not None or ref.get("high") is not None
                            else ""
                        )
                        if ref_text:
                            ref_str = f" [Ref: {ref_text}]"
                    date_str = f" ({marker.get('sample_date')})" if marker.get("sample_date") else ""
                    lab_lines.append(f"- {disp}: {val} {unit}{ref_str}{date_str}".strip())
                else:
                    lab_lines.append(f"- {marker_key}: {marker}")
        else:
            lab_lines.append("- Status: No laboratory studies recorded for this cycle.")

        # 3. ECG Examination
        from app.services.ecg_context_service import assemble_consilium_ecg_context
        ecg_ctx = assemble_consilium_ecg_context(self.db, cycle_id)
        ecg_lines: List[str] = []
        if ecg_ctx.is_conducted:
            ecg_lines.append(f"- Summary: {ecg_ctx.ecg_summary}")
            if ecg_ctx.heart_rate_bpm:
                ecg_lines.append(f"- Heart rate: {ecg_ctx.heart_rate_bpm} bpm")
            if ecg_ctx.rhythm_type:
                reg_str = f" ({ecg_ctx.rhythm_regularity})" if ecg_ctx.rhythm_regularity else ""
                ecg_lines.append(f"- Rhythm: {ecg_ctx.rhythm_type}{reg_str}")
            if ecg_ctx.record_date:
                ecg_lines.append(f"- Recording date: {ecg_ctx.record_date}")
        else:
            ecg_lines.append("- Status: Not conducted (no ECG recorded for this cycle).")

        # 4. Primary Consilium Published Conclusions (Client-Facing)
        consilium_lines: List[str] = []
        overall_reports = self.report_repo.list_client_facing(
            cycle_id=cycle_id,
            report_type="user_overall_report",
        )
        if overall_reports:
            overall_content = overall_reports[0].content or {}
            title = overall_content.get("title", "Primary Health Consilium")
            summary = overall_content.get("summary", "")
            highlights = overall_content.get("highlights") or []
            consilium_lines.append(f"- Overall conclusion ({title}): {summary}")
            if highlights:
                consilium_lines.append(f"- Key highlights: {'; '.join(str(h) for h in highlights)}")
            # Personalized vitals thresholds from published overall report
            metrics = overall_content.get("metrics")
            if isinstance(metrics, dict) and metrics:
                metric_parts: List[str] = []
                min_hr = metrics.get("min_pessimum_bpm")
                max_hr = metrics.get("max_pessimum_bpm")
                spike_delta = metrics.get("max_spike_delta_bpm")

                if min_hr is not None:
                    metric_parts.append(f"min {min_hr} bpm")
                if max_hr is not None:
                    metric_parts.append(f"max {max_hr} bpm")
                if spike_delta is not None:
                    metric_parts.append(f"max spike delta {spike_delta} bpm")

                if metric_parts:
                    consilium_lines.append(f"- Monitored thresholds: {', '.join(metric_parts)}.")
                else:
                    consilium_lines.append("- Monitored thresholds: Individual vitals thresholds unavailable.")
            else:
                consilium_lines.append("- Monitored thresholds: Individual vitals thresholds unavailable.")

            client_reports = self.report_repo.list_client_facing(cycle_id=cycle_id)
            for r in client_reports:
                if r.report_type.startswith("system_user_report_"):
                    c = r.content or {}
                    sys_title = c.get("title") or r.report_type
                    sys_summary = c.get("summary") or ""
                    if sys_summary:
                        consilium_lines.append(f"- {sys_title}: {sys_summary}")
        else:
            consilium_lines.append("- Status: Primary Consilium has not been executed yet for this cycle.")

        sections = [
            "=== AUTHORITATIVE PATIENT MEDICAL CONTEXT (ACTIVE CYCLE) ===",
            "1. ANAMNESIS:",
            "\n".join(anamnesis_lines),
            "",
            "2. LABORATORY STUDIES:",
            "\n".join(lab_lines),
            "",
            "3. ELECTROCARDIOGRAM (ECG):",
            "\n".join(ecg_lines),
            "",
            "4. PRIMARY CONSILIUM CONCLUSIONS (PUBLISHED):",
            "\n".join(consilium_lines),
        ]
        return "\n".join(sections)

    def _resolve_strategy_context(
        self, cycle_id: str, as_of: Optional[datetime] = None
    ) -> Tuple[str, List[Dict[str, str]], Optional[str]]:
        medical_context = self.build_patient_medical_context(cycle_id)

        try:
            ctx = self.weekly_service.get_active_athena_context(cycle_id, as_of=as_of)
            full_curator_context = "\n\n".join(
                [
                    medical_context,
                    "=== GUIDELINES AND CLINICAL ROUTE FROM CHIEF PHYSICIAN (PRO) ===",
                    ctx.active_strategy_text,
                    "=== DEEP CLINICAL ANALYSIS AND SYNDROMES (PRO TRAINING) ===",
                    ctx.clinical_background_text,
                    "=== PRIMARY SYNTHESIS (FLASH) ===",
                    ctx.deputy_synthesis_text,
                    ctx.athena_logs_summary,
                ]
            )
            system_prompt = build_athena_system_prompt(full_curator_context)
            return system_prompt, ctx.chat_history, ctx.sprint_id
        except ConsiliumContextMissingError:
            # Fallback for newly paired patient prior to Primary Consilium execution
            onboarding_route = (
                "=== GUIDELINES AND CLINICAL ROUTE ===\n"
                "Patient is in initial onboarding (Primary Consilium not yet executed). "
                "Welcome the user warmly to Aegis Health, encourage them to complete the initial anamnesis questionnaire, "
                "upload laboratory studies or ECG if available, track daily vitals, "
                "and provide empathetic wellness companionship based on the available data."
            )
            full_curator_context = "\n\n".join(
                [
                    medical_context,
                    onboarding_route,
                ]
            )
            system_prompt = build_athena_system_prompt(full_curator_context)
            chat_history = self.weekly_service.get_athena_chat_history(cycle_id)
            return system_prompt, chat_history, None

    @staticmethod
    def _build_turn_prompt_and_record(
        request: AthenaChatRequest,
        effective_sprint_day: Optional[int] = None,
    ) -> Tuple[str, str]:
        event_type = request.event_type

        if event_type == "vitals_alert":
            if request.vitals_alert is None:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="vitals_alert context must be provided when event_type is 'vitals_alert'.",
                )
            va = request.vitals_alert
            prompt = f"""Service pulse: Heart rate spike detected (peak up to {va.current_pulse} bpm with a baseline of {va.baseline_pulse} bpm at rest).{f' Additional notes: {va.notes}' if va.notes else ''}

YOUR INSTRUCTION (MODE 2: ATTENTION AND SUPPORT):
1. THE "GOODBYE UNTIL TOMORROW" RULE IS STRICTLY CANCELED!
2. Speak warmly and calmly. Ask what happened, ask them to sit down, unbutton their collar, and breathe.
3. Filter out "red flags" (chest pain, shortness of breath, lack of air, darkening in the eyes).
4. Be sure to formulate [CLINICAL_REPORT] with "ALERT" (or "CRITICAL") status, specifying "red_flags" and a summary for the Chief Physician.
5. If the condition is critical, add to the very end of the response (after [CLINICAL_REPORT]): [EMERGENCY_TRIGGER].
"""
            user_record = f"[Vitals Alert]: Heart rate peak {va.current_pulse} bpm (baseline {va.baseline_pulse} bpm)"
            return prompt, user_record

        if event_type == "wearable_event":
            if request.vitals_alert is not None:
                va = request.vitals_alert
                prompt = f"""Service pulse: Wearable biometric alert (peak pulse {va.current_pulse} bpm, baseline {va.baseline_pulse} bpm).{f' Additional notes: {va.notes}' if va.notes else ''}

YOUR INSTRUCTION (MODE 2: WEARABLE ALERT EVALUATION):
1. THE "GOODBYE UNTIL TOMORROW" RULE IS STRICTLY CANCELED!
2. Speak warmly and calmly. Ask what happened, ask them to sit down, unbutton their collar, and breathe.
3. Filter out "red flags" (chest pain, shortness of breath, lack of air, darkening in the eyes).
4. Formulate [CLINICAL_REPORT] with "ALERT" (or "CRITICAL") status, specifying "red_flags" and a summary for the Chief Physician.
5. If the condition is critical, add strictly at the very end on a new line: [EMERGENCY_TRIGGER].
"""
                user_record = f"[Wearable Event]: Heart rate peak {va.current_pulse} bpm (baseline {va.baseline_pulse} bpm)"
                return prompt, user_record
            else:
                msg = (request.message or "Smartwatch biometric event").strip()
                prompt = f"""Service pulse: Wearable biometric event recorded: "{msg}"

YOUR INSTRUCTION (MODE 2: WEARABLE EVENT EVALUATION):
1. Speak calmly and empathetically.
2. Evaluate reported biometric indicators and ask about current symptoms.
3. Formulate [CLINICAL_REPORT] with "ALERT" or "NORMAL" status.
4. If acute danger or critical symptoms are reported, add strictly at the very end on a new line: [EMERGENCY_TRIGGER].
"""
                user_record = f"[Wearable Event]: {msg}"
                return prompt, user_record

        if event_type == "out_of_turn_message":
            msg = (request.message or "").strip()
            if not msg:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="message must be provided and non-empty when event_type is 'out_of_turn_message'.",
                )
            prompt = f"""User sent an out-of-turn inquiry / note: "{msg}"

YOUR INSTRUCTION:
1. Address the patient's specific question or statement directly, warmly, and empathetically.
2. Note that this is an out-of-turn inquiry, separate from routine daily check-in questions.
3. If the user reported POOR WELL-BEING (pain, weakness, nausea, anxiety):
   - DO NOT SAY GOODBYE! Activate Mode 2 (Force Majeure).
   - Ask about symptoms, reassure, help ground.
   - Fill in [CLINICAL_REPORT] with "ALERT" or "CRITICAL" status and detail "symptoms_mentioned" and "red_flags".
   - In case of a real life-threatening situation, add strictly at the very end on a new line: [EMERGENCY_TRIGGER].
4. Otherwise, fill in [CLINICAL_REPORT] with "NORMAL" status and answer the patient's inquiry supportively.
"""
            user_record = f"[Out-of-turn]: {msg}"
            return prompt, user_record

        if event_type == "routine_answer":
            msg = (request.message or "").strip()
            if not msg:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="message must be provided and non-empty when event_type is 'routine_answer'.",
                )
            s_day = effective_sprint_day or request.sprint_day or 1
            is_sprint_end = (s_day == 7)
            teaser_phrase = (
                '- SINCE TODAY IS DAY 7 OF THE SPRINT: At the very end of the response on a new line, '
                'add a natural teaser phrase: "By the way, wait a minute, let me show you how you spent this week!"\n'
                if is_sprint_end else ''
            )
            prompt = f"""User replied to routine question: "{msg}"

YOUR INSTRUCTION:
1. If the response is ordinary and routine:
   - Briefly comment on the response with empathy.
   - Confirm data recording.
   - Say goodbye until tomorrow.
   {teaser_phrase}   - Fill in [CLINICAL_REPORT] with "NORMAL" or "ALERT" status (depending on plan adherence).

2. If the user reported POOR WELL-BEING (pain, weakness, nausea, anxiety):
   - DO NOT SAY GOODBYE! Activate Mode 2 (Force Majeure).
   - Ask about symptoms, reassure, help ground.
   - Fill in [CLINICAL_REPORT] with "ALERT" or "CRITICAL" status and detail "symptoms_mentioned" and "red_flags".
   - In case of a real life-threatening situation, add strictly at the very end on a new line: [EMERGENCY_TRIGGER].
"""
            return prompt, msg

        if event_type == "daily_pulse":
            s_day = effective_sprint_day or request.sprint_day or 1
            prompt = f"""Service pulse: A NEW DAY HAS COME (ROUTINE MODE). Current sprint day: {s_day} of 7.
Study the history of past answers and the current step of the Pro route.
1. Gently greet the user.
2. Formulate 1 NEXT question along the route with 2-3 response options.
3. Generate the current [CLINICAL_REPORT].
"""
            user_record = f"[Daily Pulse]: Day {s_day} check-in"
            return prompt, user_record

        if event_type == "initial_greeting":
            prompt = """Service pulse: FIRST INTRODUCTION.
Briefly and sincerely introduce yourself (Athena), say that you will help follow the route.
Formulate the first targeted guiding question on the first step of the Pro route with 2-3 everyday response options.
Don't forget to generate the initial [CLINICAL_REPORT].
"""
            user_record = "[Initialization]: First introduction"
            return prompt, user_record

        # Default: user_message
        msg = (request.message or "").strip()
        if not msg:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="message must be provided and non-empty when event_type is 'user_message'.",
            )
        s_day = effective_sprint_day or request.sprint_day or 1
        is_sprint_end = (s_day == 7)

        teaser_phrase = (
            '- SINCE TODAY IS DAY 7 OF THE SPRINT: At the very end of the response on a new line, '
            'add a natural teaser phrase: "By the way, wait a minute, let me show you how you spent this week!"\n'
            if is_sprint_end else ''
        )

        prompt = f"""User replied: "{msg}"

YOUR INSTRUCTION:
1. If the response is ordinary and routine:
   - Briefly comment on the response with empathy.
   - Confirm data recording.
   - Say goodbye until tomorrow.
   {teaser_phrase}   - Fill in [CLINICAL_REPORT] with "NORMAL" or "ALERT" status (depending on plan adherence).

2. If the user reported POOR WELL-BEING (pain, weakness, nausea, anxiety):
   - DO NOT SAY GOODBYE! Activate Mode 2 (Force Majeure).
   - Ask about symptoms, reassure, help ground.
   - Fill in [CLINICAL_REPORT] with "ALERT" or "CRITICAL" status and detail "symptoms_mentioned" and "red_flags".
   - In case of a real life-threatening situation, add strictly at the very end on a new line: [EMERGENCY_TRIGGER].
"""
        return prompt, msg

    @staticmethod
    def _build_gemini_contents(
        chat_history: List[Dict[str, str]],
        current_prompt: str,
    ) -> List[Dict[str, Any]]:
        contents: List[Dict[str, Any]] = []

        if chat_history:
            last_role: Optional[str] = None
            for item in chat_history:
                sender = str(item.get("sender", "curator")).lower()
                text = str(item.get("text", "")).strip()
                if not text:
                    continue
                role = "user" if sender == "user" else "model"
                if role != last_role:
                    contents.append({"role": role, "parts": [{"text": text}]})
                    last_role = role

        # Ensure first turn is from user if history starts with model
        if contents and contents[0]["role"] == "model":
            contents.insert(0, {"role": "user", "parts": [{"text": "Athena chat initialization."}]})

        # Append current user prompt as final turn
        contents.append({"role": "user", "parts": [{"text": current_prompt}]})
        return contents

    async def _execute_generation_with_fallback(
        self,
        *,
        contents: List[Dict[str, Any]],
        system_instruction: str,
        pre_emergency: bool,
    ) -> Tuple[str, bool]:
        """
        Executes Gemini call. If Gemini is unavailable / fails AND pre_emergency is True,
        falls back to deterministic emergency safety advice instead of failing with 502.
        """
        try:
            raw_text = await self.gateway.generate_raw_text(
                contents=contents,
                model=GeminiModelRole.FLASH,
                system_instruction=system_instruction,
                response_mime_type="text/plain",
            )
            return raw_text, False
        except Exception as exc:
            if pre_emergency:
                logger.error(
                    f"Gemini API failure during acute emergency: {exc}. "
                    "Engaging deterministic fail-safe emergency response protocol."
                )
                emergency_fallback = (
                    "Система Aegis зафиксировала тревожные показатели самочувствия и пульса. "
                    "Пожалуйста, немедленно присядьте, расстегните воротник и обеспечьте приток свежего воздуха. "
                    "Если вы ощущаете боль за грудиной, одышку, нехватку воздуха или потемнение в глазах — "
                    "незамедлительно вызовите скорую помощь (112).\n\n"
                    "[CLINICAL_REPORT]\n"
                    "{\n"
                    '  "status": "CRITICAL",\n'
                    '  "symptoms_mentioned": ["critical_vitals_or_acute_symptoms"],\n'
                    '  "subjective_wellbeing_score": null,\n'
                    '  "adherence_to_plan": "UNKNOWN",\n'
                    '  "red_flags": true,\n'
                    '  "summary_for_master": "Emergency fail-safe activated: acute vitals/symptoms with AI gateway offline"\n'
                    "}\n"
                    "[/CLINICAL_REPORT]\n"
                    "[EMERGENCY_TRIGGER]"
                )
                return emergency_fallback, True

            logger.error(f"Gemini API error during Athena turn: {exc}")
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Athena AI service temporarily unavailable: {exc}",
            ) from exc

    def _process_and_persist_reports(
        self,
        *,
        cycle_id: str,
        sprint_id: Optional[str],
        raw_response: str,
        pre_emergency: bool,
        pre_emergency_notice: Optional[str],
        used_emergency_fallback: bool,
        event_type: Optional[str] = None,
        as_of: Optional[datetime] = None,
        commit: bool = True,
    ) -> Tuple[str, bool, Optional[str]]:
        working_text = raw_response

        # 1. Detect and strip [EMERGENCY_TRIGGER]
        emergency_trigger_found = bool(EMERGENCY_TRIGGER_REGEX.search(working_text))
        working_text = EMERGENCY_TRIGGER_REGEX.sub("", working_text).strip()

        # 2. Extract and parse [CLINICAL_REPORT] JSON
        clinical_match = CLINICAL_REPORT_REGEX.search(working_text)
        is_critical_report = False

        if clinical_match:
            raw_json = clinical_match.group(1).strip()
            # Clean possible markdown fence
            raw_json = re.sub(r"^```json\s*", "", raw_json, flags=re.IGNORECASE)
            raw_json = re.sub(r"^```\s*", "", raw_json)
            raw_json = re.sub(r"\s*```$", "", raw_json)

            try:
                parsed_dict = json.loads(raw_json)
                log_entry = AthenaClinicalLogEntry.model_validate(parsed_dict)
                if log_entry.status.upper() == "CRITICAL" or log_entry.red_flags:
                    is_critical_report = True
            except Exception as parse_err:
                logger.warning(f"Failed to parse Athena hidden [CLINICAL_REPORT]: {parse_err}")
                # Build fallback clinical report
                log_entry = AthenaClinicalLogEntry(
                    status="ALERT" if not pre_emergency else "CRITICAL",
                    symptoms_mentioned=["parsing_fallback"],
                    red_flags=pre_emergency,
                    summary_for_master="Athena turn recorded (clinical report parsing fallback)",
                )

            self.weekly_service.save_athena_clinical_log(
                cycle_id=cycle_id,
                log_entry=log_entry,
                sprint_id=sprint_id,
                event_type=event_type,
                as_of=as_of,
                commit=commit,
            )

            # Completely strip [CLINICAL_REPORT] from client text
            working_text = CLINICAL_REPORT_REGEX.sub("", working_text).strip()

        # Extra defense-in-depth: strip unclosed clinical report blocks, emergency trigger tags, or internal headers
        working_text = re.sub(r"\[CLINICAL_REPORT\][\s\S]*$", "", working_text, flags=re.IGNORECASE).strip()
        working_text = re.sub(r"\[/?CLINICAL_REPORT\]", "", working_text, flags=re.IGNORECASE).strip()
        working_text = EMERGENCY_TRIGGER_REGEX.sub("", working_text).strip()
        working_text = re.sub(
            r"=== (?:AUTHORITATIVE PATIENT MEDICAL CONTEXT|GUIDELINES AND CLINICAL ROUTE|DEEP CLINICAL ANALYSIS|PRIMARY SYNTHESIS|INSTRUCTIONS AND ROUTE)[\s\S]*?===\n?",
            "",
            working_text,
            flags=re.IGNORECASE,
        ).strip()

        clean_reply = working_text.strip()

        # Final structured emergency determination
        final_emergency = (
            emergency_trigger_found
            or is_critical_report
            or pre_emergency
            or used_emergency_fallback
        )

        final_notice: Optional[str] = None
        if final_emergency:
            final_notice = (
                pre_emergency_notice
                or ("EMERGENCY_TRIGGER: Critical clinical signal detected by Athena." if emergency_trigger_found
                    else "CRITICAL_STATUS: Clinical indicators require immediate medical attention.")
            )

        return clean_reply, final_emergency, final_notice
