import asyncio
from datetime import datetime, timedelta, timezone
from enum import Enum
import hashlib
import logging
import re
from typing import Any, Awaitable, Callable, Dict, List, Optional, Set, Tuple
import uuid

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text as sql_text, update
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from app.models.medical_cycle import MedicalCycle
from app.models.model_report import ModelReport
from app.repositories.cycle_repository import MedicalCycleRepository
from app.repositories.report_repository import (
    ModelReportRepository,
    ReportVersionConflictError,
)
from app.schemas.chat import AthenaChatRequest
from app.schemas.privacy import PrivacyLevel
from app.schemas.wellness import WellnessTriggerType
from app.services.athena_chat_service import (
    AthenaChatService,
    POST_PRIMARY_INIT_IDEMPOTENCY_PREFIX,
)
from app.services.athena_daily_journal_service import AthenaDailyJournalService
from app.services.gemini_gateway import GeminiGateway, GeminiGatewayError
from app.services.primary_consilium_service import (
    PrimaryConsiliumService,
    ConsiliumError,
    ConsiliumCycleNotFoundError,
    ConsiliumContextMissingError,
    ConsiliumConcurrencyConflictError,
)
from app.services.weekly_consilium_service import WeeklyConsiliumService
from app.services.wellness_daily_service import WellnessDailyService

logger = logging.getLogger("aegis.clinical_automation")

AUTOMATION_SCHEDULER_MODEL: str = "aegis-automation-scheduler"
AUTOMATION_JOB_REPORT_PREFIX: str = "auto_job_"

DEFAULT_JOB_LEASE_TTL_SECONDS: int = 120
DEFAULT_JOB_HEARTBEAT_SECONDS: float = 15.0
DEFAULT_JOB_MAX_ATTEMPTS: int = 5
DEFAULT_BASE_BACKOFF_SECONDS: int = 30
DEFAULT_MAX_BACKOFF_SECONDS: int = 3600
DEFAULT_WORKER_POLL_INTERVAL_SECONDS: float = 30.0


class AutomationJobType(str, Enum):
    """
    Stage A1/A2 durable automation job types.
    NOTE:
    - First Primary Consilium is strictly MANUAL (triggered by the patient via Flutter)
      and is NEVER scheduled as an automatic intake job.
    - Athena does NOT generate 24-hour timer questions without patient replies.
    - `post_primary_init`: post-Primary Athena initial greeting & T0 anchor.
    - `first_planned_question`: Stage 1 first planned question at T0 + 30s linked to weekly plan.
    - `daily_journal_closure`: evidence-preserving daily journal closure + internal Gemini Flash assessment (Stage A2.2).
    """
    POST_PRIMARY_INIT = "post_primary_init"
    FIRST_PLANNED_QUESTION = "first_planned_question"
    DAILY_JOURNAL_CLOSURE = "daily_journal_closure"
    WELLNESS_DAILY = "wellness_daily"
    WEEKLY_CONSILIUM_A = "weekly_consilium_step_a"
    WEEKLY_CONSILIUM_B = "weekly_consilium_step_b"
    WEEKLY_CONSILIUM_C = "weekly_consilium_step_c"
    LAB_REVISION = "lab_revision"


class AutomationJobStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    RETRYABLE_FAILURE = "retryable_failure"
    TERMINAL_FAILURE = "terminal_failure"


class AutomationJobRecord(BaseModel):
    """Durable SQLite automation job state persisted in `model_reports` (`server_only`)."""
    model_config = ConfigDict(extra="forbid")

    job_id: str
    report_type: str
    patient_id: str
    cycle_id: str
    job_type: AutomationJobType
    idempotency_key: str
    status: AutomationJobStatus
    attempts: int = Field(default=0, ge=0)
    max_attempts: int = Field(default=DEFAULT_JOB_MAX_ATTEMPTS, ge=1)
    next_attempt_at_utc: Optional[str] = None
    lease_owner_id: Optional[str] = None
    lease_expires_at_utc: Optional[str] = None
    renewed_at_utc: Optional[str] = None
    renewal_count: int = Field(default=0, ge=0)
    last_error: Optional[Dict[str, Any]] = None
    result_metadata: Optional[Dict[str, Any]] = None
    created_at_utc: str
    updated_at_utc: str
    version: int = Field(default=1, ge=1)


class ClinicalAutomationService:
    """
    Durable SQLite-backed automation coordinator (Stage A1/A2).
    Invariants:
    1. First Primary Consilium is NEVER auto-started after intake; it remains strictly manual.
    2. Athena NEVER generates unconditional 24-hour daily questions without a patient reply.
    3. Post-Primary initialization (`post_primary_init`) runs automatically only after a
       completed manual Primary Consilium, establishing at most one initial greeting and one T0.
    4. If the mobile client already created a valid post-Primary initial greeting / T0 anchor,
       the automation job reuses the existing event with zero extra Gemini calls.
    5. Multi-process safe via SQLite atomic claims, lease ownership fencing, separate-session
       heartbeat renewal, and bounded exponential backoff retries.
    """

    def __init__(
        self,
        db: Session,
        gateway: Optional[GeminiGateway] = None,
        now_provider: Optional[Callable[[], datetime]] = None,
        lease_ttl_seconds: int = DEFAULT_JOB_LEASE_TTL_SECONDS,
        lease_heartbeat_interval_seconds: float = DEFAULT_JOB_HEARTBEAT_SECONDS,
        base_backoff_seconds: int = DEFAULT_BASE_BACKOFF_SECONDS,
        max_backoff_seconds: int = DEFAULT_MAX_BACKOFF_SECONDS,
        enable_daily_journal: bool = False,
        enable_weekly_plan: bool = False,
        enable_wellness_daily: bool = False,
        enable_automatic_weekly: bool = False,
        enable_lab_revision: bool = False,
    ):
        self.db = db
        self.gateway = gateway or GeminiGateway()
        self.now_provider = now_provider or (lambda: datetime.now(timezone.utc))
        self.lease_ttl_seconds = max(1, int(lease_ttl_seconds))
        self.lease_heartbeat_interval_seconds = max(0.001, float(lease_heartbeat_interval_seconds))
        self.base_backoff_seconds = max(1, int(base_backoff_seconds))
        self.max_backoff_seconds = max(self.base_backoff_seconds, int(max_backoff_seconds))
        self.enable_daily_journal = enable_daily_journal
        self.enable_weekly_plan = enable_weekly_plan
        self.enable_wellness_daily = enable_wellness_daily
        self.enable_automatic_weekly = enable_automatic_weekly
        self.enable_lab_revision = enable_lab_revision

        self.cycle_repo = MedicalCycleRepository(db)
        self.report_repo = ModelReportRepository(db)
        self.primary_service = PrimaryConsiliumService(
            db=db, gateway=self.gateway, now_provider=self.now_provider
        )
        self.weekly_service = WeeklyConsiliumService(
            db=db, gateway=self.gateway, now_provider=self.now_provider
        )
        self.athena_service = AthenaChatService(
            db=db, gateway=self.gateway, now_provider=self.now_provider
        )
        self.journal_service = AthenaDailyJournalService(
            db=db, now_provider=self.now_provider, gateway=self.gateway
        )
        self.wellness_service = WellnessDailyService(
            db=db, gateway=self.gateway, now_provider=self.now_provider
        )
        from app.services.lab_scheduling_service import LabSchedulingService
        from app.services.lab_revision_consilium_service import LabRevisionConsiliumService

        self.lab_scheduling_service = LabSchedulingService(
            db=db, gateway=self.gateway, now_provider=self.now_provider
        )
        self.lab_revision_service = LabRevisionConsiliumService(
            db=db, gateway=self.gateway, now_provider=self.now_provider
        )

    def _now_utc(self) -> datetime:
        dt = self.now_provider()
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)

    @staticmethod
    def _parse_iso_utc(raw_value: Optional[str]) -> Optional[datetime]:
        if not raw_value or not isinstance(raw_value, str):
            return None
        try:
            dt = datetime.fromisoformat(raw_value)
            if dt.tzinfo is None:
                return dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except Exception:
            return None

    @staticmethod
    def compute_job_identifiers(
        cycle_id: str,
        job_type: AutomationJobType | str,
        idempotency_key: str,
    ) -> Tuple[str, str]:
        clean_cycle = cycle_id.strip()
        clean_type = job_type.value if isinstance(job_type, AutomationJobType) else str(job_type).strip()
        clean_idem = idempotency_key.strip()
        digest = hashlib.sha256(
            f"{clean_cycle}|{clean_type}|{clean_idem}".encode("utf-8")
        ).hexdigest()
        job_id = f"autojob_{digest[:48]}"[:64]
        report_type = f"{AUTOMATION_JOB_REPORT_PREFIX}{clean_type}_{digest[:16]}"[:64]
        return job_id, report_type

    @staticmethod
    def default_idempotency_key(cycle_id: str, job_type: AutomationJobType | str) -> str:
        clean_cycle = cycle_id.strip()
        resolved_type = (
            job_type
            if isinstance(job_type, AutomationJobType)
            else AutomationJobType(str(job_type).strip())
        )
        if resolved_type == AutomationJobType.POST_PRIMARY_INIT:
            return f"{POST_PRIMARY_INIT_IDEMPOTENCY_PREFIX}{clean_cycle}"
        if resolved_type == AutomationJobType.DAILY_JOURNAL_CLOSURE:
            return f"athena_daily_journal_{clean_cycle}_w1_d1"
        if resolved_type == AutomationJobType.WELLNESS_DAILY:
            return f"wellness_daily_{clean_cycle}_w1_d1"
        if resolved_type == AutomationJobType.WEEKLY_CONSILIUM_A:
            return f"weekly_consilium_step_a_{clean_cycle}_w1"
        if resolved_type == AutomationJobType.WEEKLY_CONSILIUM_B:
            return f"weekly_consilium_step_b_{clean_cycle}_w1"
        if resolved_type == AutomationJobType.WEEKLY_CONSILIUM_C:
            return f"weekly_consilium_step_c_{clean_cycle}_w1"
        if resolved_type == AutomationJobType.LAB_REVISION:
            return f"lab_revision_{clean_cycle}"
        return f"auto_{resolved_type.value}_{clean_cycle}"

    @staticmethod
    def _row_to_job_record(row: ModelReport) -> AutomationJobRecord:
        content = dict(row.content or {})
        content["job_id"] = str(content.get("job_id") or row.report_id)
        content["report_type"] = str(content.get("report_type") or row.report_type)
        content["cycle_id"] = str(content.get("cycle_id") or row.cycle_id)
        content["version"] = int(row.version or 1)
        content.pop("agent_role", None)
        return AutomationJobRecord.model_validate(content)

    # ==========================================================================
    # JOB ENQUEUE, DISCOVERY & QUERY
    # ==========================================================================

    def enqueue_or_get_job(
        self,
        *,
        patient_id: str,
        cycle_id: str,
        job_type: AutomationJobType | str = AutomationJobType.POST_PRIMARY_INIT,
        idempotency_key: Optional[str] = None,
        max_attempts: int = DEFAULT_JOB_MAX_ATTEMPTS,
        next_attempt_at_utc: Optional[str] = None,
    ) -> AutomationJobRecord:
        clean_patient = (patient_id or "").strip()
        clean_cycle = (cycle_id or "").strip()
        if not clean_patient:
            raise ValueError("patient_id must be non-empty for automation jobs.")
        if not clean_cycle:
            raise ValueError("cycle_id must be non-empty for automation jobs.")

        try:
            resolved_type = (
                job_type
                if isinstance(job_type, AutomationJobType)
                else AutomationJobType(str(job_type).strip())
            )
        except ValueError as exc:
            raise ValueError(
                f"Unsupported automation job_type '{job_type}'. "
                "First Primary Consilium is manual; only supported automation jobs are allowed."
            ) from exc

        cycle = self.cycle_repo.get(clean_cycle)
        if cycle is None:
            raise ConsiliumCycleNotFoundError(f"Medical cycle '{clean_cycle}' not found.")
        if cycle.patient_id and cycle.patient_id != clean_patient:
            raise ConsiliumError(
                f"Cycle '{clean_cycle}' belongs to patient '{cycle.patient_id}', not '{clean_patient}'."
            )

        if resolved_type == AutomationJobType.POST_PRIMARY_INIT:
            if not self.primary_service.is_primary_consilium_completed(clean_cycle):
                raise ConsiliumContextMissingError(
                    f"Cannot enqueue post-Primary initialization for cycle '{clean_cycle}': "
                    "manual Primary Consilium has not been completed yet."
                )
        elif resolved_type == AutomationJobType.FIRST_PLANNED_QUESTION:
            if not self.primary_service.is_primary_consilium_completed(clean_cycle):
                raise ConsiliumContextMissingError(
                    f"Cannot enqueue first planned question for cycle '{clean_cycle}': "
                    "manual Primary Consilium has not been completed yet."
                )
            if self.weekly_service.find_schedule_anchor_timestamp(clean_cycle) is None:
                raise ConsiliumContextMissingError(
                    f"Cannot enqueue first planned question for cycle '{clean_cycle}': "
                    "schedule anchor T0 has not been established yet."
                )
        elif resolved_type == AutomationJobType.DAILY_JOURNAL_CLOSURE:
            if not self.primary_service.is_primary_consilium_completed(clean_cycle):
                raise ConsiliumContextMissingError(
                    f"Cannot enqueue daily journal closure for cycle '{clean_cycle}': "
                    "manual Primary Consilium has not been completed yet."
                )
            if self.weekly_service.find_schedule_anchor_timestamp(clean_cycle) is None:
                raise ConsiliumContextMissingError(
                    f"Cannot enqueue daily journal closure for cycle '{clean_cycle}': "
                    "schedule anchor T0 has not been established yet."
                )
        elif resolved_type == AutomationJobType.WELLNESS_DAILY:
            if not self.primary_service.is_primary_consilium_completed(clean_cycle):
                raise ConsiliumContextMissingError(
                    f"Cannot enqueue daily wellness processing for cycle '{clean_cycle}': "
                    "manual Primary Consilium has not been completed yet."
                )
            if self.weekly_service.find_schedule_anchor_timestamp(clean_cycle) is None:
                raise ConsiliumContextMissingError(
                    f"Cannot enqueue daily wellness processing for cycle '{clean_cycle}': "
                    "schedule anchor T0 has not been established yet."
                )
        elif resolved_type in (
            AutomationJobType.WEEKLY_CONSILIUM_A,
            AutomationJobType.WEEKLY_CONSILIUM_B,
            AutomationJobType.WEEKLY_CONSILIUM_C,
        ):
            if not self.primary_service.is_primary_consilium_completed(clean_cycle):
                raise ConsiliumContextMissingError(
                    f"Cannot enqueue {resolved_type.value} for cycle '{clean_cycle}': "
                    "manual Primary Consilium has not been completed yet."
                )
            if self.weekly_service.find_schedule_anchor_timestamp(clean_cycle) is None:
                raise ConsiliumContextMissingError(
                    f"Cannot enqueue {resolved_type.value} for cycle '{clean_cycle}': "
                    "schedule anchor T0 has not been established yet."
                )

        clean_idem = (
            idempotency_key.strip()
            if (idempotency_key and idempotency_key.strip())
            else self.default_idempotency_key(clean_cycle, resolved_type)
        )
        job_id, report_type = self.compute_job_identifiers(
            clean_cycle, resolved_type, clean_idem
        )

        existing_row = (
            self.db.query(ModelReport)
            .filter(ModelReport.report_id == job_id)
            .populate_existing()
            .first()
        )
        if existing_row is not None:
            return self._row_to_job_record(existing_row)

        now_dt = self._now_utc()
        now_iso = now_dt.isoformat()
        record = AutomationJobRecord(
            job_id=job_id,
            report_type=report_type,
            patient_id=clean_patient,
            cycle_id=clean_cycle,
            job_type=resolved_type,
            idempotency_key=clean_idem,
            status=AutomationJobStatus.PENDING,
            attempts=0,
            max_attempts=max(1, int(max_attempts)),
            next_attempt_at_utc=next_attempt_at_utc or now_iso,
            lease_owner_id=None,
            lease_expires_at_utc=None,
            renewed_at_utc=None,
            renewal_count=0,
            last_error=None,
            result_metadata=None,
            created_at_utc=now_iso,
            updated_at_utc=now_iso,
            version=1,
        )

        content_dict = record.model_dump(mode="json")
        content_dict.pop("version", None)

        try:
            row = ModelReport(
                report_id=job_id,
                cycle_id=clean_cycle,
                sprint_id=None,
                session_id=None,
                model=AUTOMATION_SCHEDULER_MODEL,
                report_type=report_type,
                privacy_level=PrivacyLevel.SERVER_ONLY.value,
                version=1,
                content=content_dict,
                created_at=now_dt,
            )
            self.db.add(row)
            self.db.commit()
            self.db.refresh(row)
            return self._row_to_job_record(row)
        except (IntegrityError, OperationalError):
            self.db.rollback()
            concurrent_row = (
                self.db.query(ModelReport)
                .filter(ModelReport.report_id == job_id)
                .populate_existing()
                .first()
            )
            if concurrent_row is not None:
                return self._row_to_job_record(concurrent_row)
            raise

    def enqueue_post_primary_init(
        self,
        *,
        cycle_id: str,
        patient_id: Optional[str] = None,
        max_attempts: int = DEFAULT_JOB_MAX_ATTEMPTS,
    ) -> Optional[AutomationJobRecord]:
        """
        Safely enqueues the post-Primary initialization job (`post_primary_init`)
        ONLY if the manual Primary Consilium is already completed for `cycle_id`
        and the cycle is associated with a valid `patient_id`.
        """
        clean_cycle = (cycle_id or "").strip()
        if not clean_cycle:
            return None
        cycle = self.cycle_repo.get(clean_cycle)
        if cycle is None:
            return None
        effective_patient_id = (patient_id or cycle.patient_id or "").strip()
        if not effective_patient_id:
            return None
        if not self.primary_service.is_primary_consilium_completed(clean_cycle):
            return None
        return self.enqueue_or_get_job(
            patient_id=effective_patient_id,
            cycle_id=clean_cycle,
            job_type=AutomationJobType.POST_PRIMARY_INIT,
            idempotency_key=self.default_idempotency_key(
                clean_cycle, AutomationJobType.POST_PRIMARY_INIT
            ),
            max_attempts=max_attempts,
        )

    def enqueue_first_planned_question(
        self,
        *,
        cycle_id: str,
        patient_id: Optional[str] = None,
        available_at_utc: datetime,
        max_attempts: int = DEFAULT_JOB_MAX_ATTEMPTS,
    ) -> Optional[AutomationJobRecord]:
        """
        Enqueues the first planned question (first_planned_question) at T0 + 30 seconds.
        Idempotent: at most one first question job per cycle.
        """
        clean_cycle = (cycle_id or "").strip()
        if not clean_cycle:
            return None
        cycle = self.cycle_repo.get(clean_cycle)
        if cycle is None:
            return None
        effective_patient_id = (patient_id or cycle.patient_id or "").strip()
        if not effective_patient_id:
            return None
        if not self.primary_service.is_primary_consilium_completed(clean_cycle):
            return None
        if self.weekly_service.find_schedule_anchor_timestamp(clean_cycle) is None:
            return None

        idempotency_key = f"first_planned_q_{clean_cycle}"
        return self.enqueue_or_get_job(
            patient_id=effective_patient_id,
            cycle_id=clean_cycle,
            job_type=AutomationJobType.FIRST_PLANNED_QUESTION,
            idempotency_key=idempotency_key,
            max_attempts=max_attempts,
            next_attempt_at_utc=available_at_utc.isoformat(),
        )

    def enqueue_daily_journal_closure(
        self,
        *,
        cycle_id: str,
        patient_id: Optional[str] = None,
        calendar_week: int,
        calendar_day: int,
        max_attempts: int = DEFAULT_JOB_MAX_ATTEMPTS,
    ) -> Optional[AutomationJobRecord]:
        """
        Enqueues daily journal closure for a completed observation day.
        Requires manual Primary Consilium completed and anchor T0 established.
        """
        clean_cycle = (cycle_id or "").strip()
        if not clean_cycle:
            return None
        cycle = self.cycle_repo.get(clean_cycle)
        if cycle is None:
            return None
        effective_patient_id = (patient_id or cycle.patient_id or "").strip()
        if not effective_patient_id:
            return None
        if not self.primary_service.is_primary_consilium_completed(clean_cycle):
            return None
        if self.weekly_service.find_schedule_anchor_timestamp(clean_cycle) is None:
            return None

        idempotency_key = f"athena_daily_journal_{clean_cycle}_w{calendar_week}_d{calendar_day}"
        return self.enqueue_or_get_job(
            patient_id=effective_patient_id,
            cycle_id=clean_cycle,
            job_type=AutomationJobType.DAILY_JOURNAL_CLOSURE,
            idempotency_key=idempotency_key,
            max_attempts=max_attempts,
        )

    def enqueue_wellness_daily(
        self,
        *,
        cycle_id: str,
        patient_id: Optional[str] = None,
        calendar_week: int,
        calendar_day: int,
        trigger: Optional[WellnessTriggerType] = None,
        next_attempt_at_utc: Optional[str] = None,
        max_attempts: int = DEFAULT_JOB_MAX_ATTEMPTS,
    ) -> Optional[AutomationJobRecord]:
        """
        Enqueues daily Wellness processing for an observation day.
        Requires manual Primary Consilium completed and anchor T0 established.
        """
        clean_cycle = (cycle_id or "").strip()
        if not clean_cycle:
            return None
        cycle = self.cycle_repo.get(clean_cycle)
        if cycle is None:
            return None
        effective_patient_id = (patient_id or cycle.patient_id or "").strip()
        if not effective_patient_id:
            return None
        if not self.primary_service.is_primary_consilium_completed(clean_cycle):
            return None
        if self.weekly_service.find_schedule_anchor_timestamp(clean_cycle) is None:
            return None

        idempotency_key = f"wellness_daily_{clean_cycle}_w{calendar_week}_d{calendar_day}"
        return self.enqueue_or_get_job(
            patient_id=effective_patient_id,
            cycle_id=clean_cycle,
            job_type=AutomationJobType.WELLNESS_DAILY,
            idempotency_key=idempotency_key,
            max_attempts=max_attempts,
            next_attempt_at_utc=next_attempt_at_utc,
        )

    def enqueue_weekly_consilium_a(
        self,
        *,
        cycle_id: str,
        patient_id: Optional[str] = None,
        sprint_number: int = 1,
        available_at_utc: Optional[datetime] = None,
        max_attempts: int = DEFAULT_JOB_MAX_ATTEMPTS,
    ) -> Optional[AutomationJobRecord]:
        """
        Enqueues Stage 4 Step A: Automatic Weekly Consilium execution.
        Targeted to become claimable strictly at T0 + 168 * sprint_number hours.
        """
        clean_cycle = (cycle_id or "").strip()
        if not clean_cycle:
            return None
        cycle = self.cycle_repo.get(clean_cycle)
        if cycle is None:
            return None
        effective_patient_id = (patient_id or cycle.patient_id or "").strip()
        if not effective_patient_id:
            return None
        if not self.primary_service.is_primary_consilium_completed(clean_cycle):
            return None
        anchor_dt = self.weekly_service.find_schedule_anchor_timestamp(clean_cycle)
        if anchor_dt is None:
            return None

        eff_due_at = available_at_utc or (anchor_dt + timedelta(hours=168 * sprint_number))
        idempotency_key = f"weekly_consilium_step_a_{clean_cycle}_w{sprint_number}"
        return self.enqueue_or_get_job(
            patient_id=effective_patient_id,
            cycle_id=clean_cycle,
            job_type=AutomationJobType.WEEKLY_CONSILIUM_A,
            idempotency_key=idempotency_key,
            max_attempts=max_attempts,
            next_attempt_at_utc=eff_due_at.isoformat(),
        )

    def enqueue_weekly_consilium_b(
        self,
        *,
        cycle_id: str,
        patient_id: Optional[str] = None,
        sprint_number: int = 1,
        available_at_utc: Optional[datetime] = None,
        max_attempts: int = DEFAULT_JOB_MAX_ATTEMPTS,
    ) -> Optional[AutomationJobRecord]:
        """
        Enqueues Stage 4 Step B: Athena's autonomous weekly goal plan generation for week N+1.
        Triggered immediately upon completion of Step A.
        """
        clean_cycle = (cycle_id or "").strip()
        if not clean_cycle:
            return None
        cycle = self.cycle_repo.get(clean_cycle)
        if cycle is None:
            return None
        effective_patient_id = (patient_id or cycle.patient_id or "").strip()
        if not effective_patient_id:
            return None
        if not self.primary_service.is_primary_consilium_completed(clean_cycle):
            return None
        if self.weekly_service.find_schedule_anchor_timestamp(clean_cycle) is None:
            return None

        eff_available = (available_at_utc or self._now_utc()).isoformat()
        idempotency_key = f"weekly_consilium_step_b_{clean_cycle}_w{sprint_number}"
        return self.enqueue_or_get_job(
            patient_id=effective_patient_id,
            cycle_id=clean_cycle,
            job_type=AutomationJobType.WEEKLY_CONSILIUM_B,
            idempotency_key=idempotency_key,
            max_attempts=max_attempts,
            next_attempt_at_utc=eff_available,
        )

    def enqueue_weekly_consilium_c(
        self,
        *,
        cycle_id: str,
        patient_id: Optional[str] = None,
        sprint_number: int = 1,
        available_at_utc: Optional[datetime] = None,
        max_attempts: int = DEFAULT_JOB_MAX_ATTEMPTS,
    ) -> Optional[AutomationJobRecord]:
        """
        Enqueues Stage 4 Step C: Athena's separate weekly wrap-up chat message to the patient.
        Triggered immediately upon completion of Step B.
        """
        clean_cycle = (cycle_id or "").strip()
        if not clean_cycle:
            return None
        cycle = self.cycle_repo.get(clean_cycle)
        if cycle is None:
            return None
        effective_patient_id = (patient_id or cycle.patient_id or "").strip()
        if not effective_patient_id:
            return None
        if not self.primary_service.is_primary_consilium_completed(clean_cycle):
            return None
        if self.weekly_service.find_schedule_anchor_timestamp(clean_cycle) is None:
            return None

        eff_available = (available_at_utc or self._now_utc()).isoformat()
        idempotency_key = f"weekly_consilium_step_c_{clean_cycle}_w{sprint_number}"
        return self.enqueue_or_get_job(
            patient_id=effective_patient_id,
            cycle_id=clean_cycle,
            job_type=AutomationJobType.WEEKLY_CONSILIUM_C,
            idempotency_key=idempotency_key,
            max_attempts=max_attempts,
            next_attempt_at_utc=eff_available,
        )

    def enqueue_lab_revision(
        self,
        *,
        cycle_id: str,
        patient_id: Optional[str] = None,
        target_ledger_version: int = 1,
        available_at_utc: Optional[datetime] = None,
        max_attempts: int = DEFAULT_JOB_MAX_ATTEMPTS,
    ) -> Optional[AutomationJobRecord]:
        """
        Enqueues Stage 6B: background Ten-Model Lab Revision Consilium.
        Triggered when a pending task passes debounce, cooldown, and OCR checks.
        """
        clean_cycle = (cycle_id or "").strip()
        if not clean_cycle:
            return None
        cycle = self.cycle_repo.get(clean_cycle)
        if cycle is None:
            return None
        effective_patient_id = (patient_id or cycle.patient_id or "").strip()
        if not effective_patient_id:
            return None

        eff_available = (available_at_utc or self._now_utc()).isoformat()
        idempotency_key = f"lab_revision_{clean_cycle}_v{target_ledger_version}"
        return self.enqueue_or_get_job(
            patient_id=effective_patient_id,
            cycle_id=clean_cycle,
            job_type=AutomationJobType.LAB_REVISION,
            idempotency_key=idempotency_key,
            max_attempts=max_attempts,
            next_attempt_at_utc=eff_available,
        )

    def get_job_by_id(self, job_id: str) -> Optional[AutomationJobRecord]:
        row = (
            self.db.query(ModelReport)
            .filter(ModelReport.report_id == job_id.strip())
            .populate_existing()
            .first()
        )
        if row is None:
            return None
        return self._row_to_job_record(row)

    def get_job(
        self,
        cycle_id: str,
        job_type: AutomationJobType | str = AutomationJobType.POST_PRIMARY_INIT,
        idempotency_key: Optional[str] = None,
    ) -> Optional[AutomationJobRecord]:
        clean_cycle = cycle_id.strip()
        resolved_type = (
            job_type
            if isinstance(job_type, AutomationJobType)
            else AutomationJobType(str(job_type).strip())
        )
        clean_idem = (
            idempotency_key.strip()
            if (idempotency_key and idempotency_key.strip())
            else self.default_idempotency_key(clean_cycle, resolved_type)
        )
        job_id, _ = self.compute_job_identifiers(clean_cycle, resolved_type, clean_idem)
        return self.get_job_by_id(job_id)

    def list_jobs(
        self,
        *,
        cycle_id: Optional[str] = None,
        patient_id: Optional[str] = None,
        status: Optional[AutomationJobStatus | str] = None,
    ) -> List[AutomationJobRecord]:
        query = (
            self.db.query(ModelReport)
            .filter(ModelReport.model == AUTOMATION_SCHEDULER_MODEL)
            .filter(ModelReport.report_type.like(f"{AUTOMATION_JOB_REPORT_PREFIX}%"))
        )
        if cycle_id is not None and cycle_id.strip():
            query = query.filter(ModelReport.cycle_id == cycle_id.strip())
        rows = query.populate_existing().all()

        target_status = (
            status.value
            if isinstance(status, AutomationJobStatus)
            else (str(status).strip() if status is not None else None)
        )
        clean_patient = patient_id.strip() if (patient_id and patient_id.strip()) else None

        records: List[AutomationJobRecord] = []
        for row in rows:
            try:
                rec = self._row_to_job_record(row)
            except Exception:
                continue
            if clean_patient and rec.patient_id != clean_patient:
                continue
            if target_status and rec.status.value != target_status:
                continue
            records.append(rec)

        records.sort(key=lambda r: (r.created_at_utc, r.job_id))
        return records

    def discover_and_enqueue_jobs(
        self, *, discover_daily_journals: Optional[bool] = None
    ) -> List[AutomationJobRecord]:
        """
        Durable job discovery across server restarts:
        Scans active medical cycles associated with a patient.
        - NEVER enqueues or runs first Primary Consilium (which is strictly manual).
        - If manual Primary Consilium has completed for an active cycle, ensures the
          `post_primary_init` job is durably registered in SQLite.
        - If daily journals are enabled, discovers completed daily observation windows
          and enqueues `daily_journal_closure` jobs.
        """
        should_discover_dj = (
            discover_daily_journals
            if discover_daily_journals is not None
            else self.enable_daily_journal
        )
        active_cycles = (
            self.db.query(MedicalCycle)
            .filter(MedicalCycle.status == "active")
            .filter(MedicalCycle.patient_id.isnot(None))
            .populate_existing()
            .all()
        )
        discovered: List[AutomationJobRecord] = []
        for cycle in active_cycles:
            if not cycle.patient_id or not cycle.patient_id.strip():
                continue
            if not self.primary_service.is_primary_consilium_completed(cycle.cycle_id):
                # First Primary Consilium is strictly manual — never auto-enqueue after intake
                continue
            job = self.enqueue_post_primary_init(
                cycle_id=cycle.cycle_id,
                patient_id=cycle.patient_id,
            )
            if job is not None:
                discovered.append(job)

            # Discover first_planned_question if weekly plan enabled and anchor T0 established
            if self.enable_weekly_plan:
                anchor_dt = self.weekly_service.find_schedule_anchor_timestamp(cycle.cycle_id)
                if anchor_dt is not None:
                    has_unanswered = self.athena_service.has_unanswered_routine_question(cycle.cycle_id)
                    q1_key = f"first_planned_q_{cycle.cycle_id}"
                    existing_q1_job = self.get_job(
                        cycle_id=cycle.cycle_id,
                        job_type=AutomationJobType.FIRST_PLANNED_QUESTION,
                        idempotency_key=q1_key,
                    )
                    if not has_unanswered and existing_q1_job is None:
                        q1_job = self.enqueue_first_planned_question(
                            cycle_id=cycle.cycle_id,
                            patient_id=cycle.patient_id,
                            available_at_utc=anchor_dt + timedelta(seconds=30),
                        )
                        if q1_job is not None:
                            discovered.append(q1_job)

            # Discover completed observation days requiring daily journal closure
            if should_discover_dj:
                anchor_dt = self.weekly_service.find_schedule_anchor_timestamp(cycle.cycle_id)
                if anchor_dt is not None:
                    candidate_pairs: Set[Tuple[int, int]] = set()

                    # 1. Nominal completed 24h windows
                    completed_windows = self.journal_service.get_completed_daily_windows(
                        cycle.cycle_id, as_of=self._now_utc()
                    )
                    for w, d, _, _ in completed_windows:
                        candidate_pairs.add((w, d))

                    # 2. Days with existing Wellness reports (READY or NO_DATA)
                    if self.wellness_service is not None:
                        wellness_reports = self.wellness_service.list_wellness_reports(cycle.cycle_id)
                        for w_rep in wellness_reports:
                            candidate_pairs.add((w_rep.calendar_week, w_rep.calendar_day))

                    for w, d in sorted(candidate_pairs):
                        existing_journal = self.journal_service.get_daily_journal(
                            cycle.cycle_id, w, d
                        )
                        needs_closure = existing_journal is None or (
                            not existing_journal.athena_clinical_assessment.is_available
                            and self.journal_service.has_substantive_daily_evidence(
                                existing_journal
                            )
                        )
                        if needs_closure:
                            dj_job = self.enqueue_daily_journal_closure(
                                cycle_id=cycle.cycle_id,
                                patient_id=cycle.patient_id,
                                calendar_week=w,
                                calendar_day=d,
                            )
                            if dj_job is not None:
                                discovered.append(dj_job)

            # Discover pending daily wellness analysis if enabled
            if self.enable_wellness_daily:
                anchor_dt = self.weekly_service.find_schedule_anchor_timestamp(cycle.cycle_id)
                if anchor_dt is not None:
                    pending_wellness = self.wellness_service.get_pending_wellness_windows(
                        cycle.cycle_id, as_of=self._now_utc()
                    )
                    for w, d, trig in pending_wellness:
                        existing_wellness = self.wellness_service.get_daily_wellness_report(
                            cycle.cycle_id, w, d
                        )
                        if existing_wellness is None:
                            w_job = self.enqueue_wellness_daily(
                                cycle_id=cycle.cycle_id,
                                patient_id=cycle.patient_id,
                                calendar_week=w,
                                calendar_day=d,
                                trigger=trig,
                            )
                            if w_job is not None:
                                discovered.append(w_job)
            # Discover automatic weekly consilium (Steps A, B, C) if enabled
            if self.enable_automatic_weekly:
                anchor_dt = self.weekly_service.find_schedule_anchor_timestamp(cycle.cycle_id)
                if anchor_dt is not None:
                    n = 1
                    while True:
                        due_at = anchor_dt + timedelta(hours=168 * n)
                        step_a_key = f"weekly_consilium_step_a_{cycle.cycle_id}_w{n}"
                        step_a_job = self.get_job(cycle.cycle_id, AutomationJobType.WEEKLY_CONSILIUM_A, step_a_key)

                        is_step_a_done = (
                            (step_a_job is not None and step_a_job.status == AutomationJobStatus.COMPLETED)
                            or self.weekly_service.recover_completed_response(cycle.cycle_id, sprint_id=f"sprint_{cycle.cycle_id}_w{n}") is not None
                        )

                        if not is_step_a_done:
                            if step_a_job is None:
                                job = self.enqueue_weekly_consilium_a(
                                    cycle_id=cycle.cycle_id,
                                    patient_id=cycle.patient_id,
                                    sprint_number=n,
                                    available_at_utc=due_at,
                                )
                                if job is not None:
                                    discovered.append(job)
                            break
                        else:
                            step_b_key = f"weekly_consilium_step_b_{cycle.cycle_id}_w{n}"
                            step_b_job = self.get_job(cycle.cycle_id, AutomationJobType.WEEKLY_CONSILIUM_B, step_b_key)
                            from app.services.athena_weekly_plan_service import AthenaWeeklyPlanService
                            plan_svc = AthenaWeeklyPlanService(db=self.db, gateway=self.gateway, now_provider=self.now_provider)
                            has_next_plan = plan_svc.get_active_weekly_plan(cycle.cycle_id, week_number=n + 1) is not None
                            is_step_b_done = (
                                (step_b_job is not None and step_b_job.status == AutomationJobStatus.COMPLETED)
                                or has_next_plan
                            )

                            if not is_step_b_done:
                                if step_b_job is None:
                                    job = self.enqueue_weekly_consilium_b(
                                        cycle_id=cycle.cycle_id,
                                        patient_id=cycle.patient_id,
                                        sprint_number=n,
                                    )
                                    if job is not None:
                                        discovered.append(job)
                                break
                            else:
                                step_c_key = f"weekly_consilium_step_c_{cycle.cycle_id}_w{n}"
                                step_c_job = self.get_job(cycle.cycle_id, AutomationJobType.WEEKLY_CONSILIUM_C, step_c_key)
                                has_wrapup = self.athena_service._find_idempotent_turn(
                                    cycle.cycle_id, f"athena_weekly_wrapup_{cycle.cycle_id}_w{n}"
                                ) is not None
                                is_step_c_done = (
                                    (step_c_job is not None and step_c_job.status == AutomationJobStatus.COMPLETED)
                                    or has_wrapup
                                )

                                if not is_step_c_done:
                                    if step_c_job is None:
                                        job = self.enqueue_weekly_consilium_c(
                                            cycle_id=cycle.cycle_id,
                                            patient_id=cycle.patient_id,
                                            sprint_number=n,
                                        )
                                        if job is not None:
                                            discovered.append(job)
                                    break
                                else:
                                    n += 1
                                    if n > 52:
                                        break
        # Discover background Lab Revision tasks if enabled
        if self.enable_lab_revision:
            ready_tasks = self.lab_scheduling_service.get_ready_tasks_for_execution()
            for t in ready_tasks:
                job = self.enqueue_lab_revision(
                    cycle_id=t.cycle_id,
                    patient_id=t.patient_id,
                    target_ledger_version=t.target_ledger_version,
                )
                if job is not None:
                    discovered.append(job)
        return discovered

    # ==========================================================================
    # MULTI-PROCESS ATOMIC CLAIM, LEASE HEARTBEAT & RETRY STATE MACHINE
    # ==========================================================================

    def is_job_claimable(
        self,
        record: AutomationJobRecord,
        now_dt: Optional[datetime] = None,
    ) -> bool:
        effective_now = now_dt or self._now_utc()
        if record.status in (
            AutomationJobStatus.COMPLETED,
            AutomationJobStatus.TERMINAL_FAILURE,
        ):
            return False

        if record.status == AutomationJobStatus.PENDING:
            next_dt = self._parse_iso_utc(record.next_attempt_at_utc)
            return next_dt is None or next_dt <= effective_now

        if record.status == AutomationJobStatus.RETRYABLE_FAILURE:
            if record.attempts >= record.max_attempts:
                return False
            next_dt = self._parse_iso_utc(record.next_attempt_at_utc)
            return next_dt is None or next_dt <= effective_now

        if record.status == AutomationJobStatus.RUNNING:
            # Recoverable only if the previous worker's lease has expired
            expires_dt = self._parse_iso_utc(record.lease_expires_at_utc)
            return expires_dt is not None and expires_dt <= effective_now

        return False

    def claim_job(self, job_id: str) -> Optional[AutomationJobRecord]:
        """
        Atomically claims a due or lease-expired job in SQLite using `BEGIN IMMEDIATE`
        and version-fenced conditional UPDATE. Guarantees at most one worker claims
        a logical job attempt across threads and processes.
        """
        clean_id = job_id.strip()
        now_dt = self._now_utc()
        now_iso = now_dt.isoformat()

        try:
            if not self.db.new and not self.db.dirty and not self.db.deleted:
                self.db.rollback()
            bind = self.db.get_bind()
            if bind is not None and bind.dialect.name == "sqlite":
                self.db.execute(sql_text("BEGIN IMMEDIATE"))

            row = (
                self.db.query(ModelReport)
                .filter(ModelReport.report_id == clean_id)
                .populate_existing()
                .first()
            )
            if row is None:
                self.db.rollback()
                return None

            record = self._row_to_job_record(row)
            if not self.is_job_claimable(record, now_dt):
                self.db.rollback()
                return None

            # If a running job's lease expired after exhausting max_attempts, mark terminal_failure
            if (
                record.status == AutomationJobStatus.RUNNING
                and record.attempts >= record.max_attempts
            ):
                term_content = dict(row.content or {})
                term_content["status"] = AutomationJobStatus.TERMINAL_FAILURE.value
                term_content["lease_owner_id"] = None
                term_content["lease_expires_at_utc"] = None
                term_content["next_attempt_at_utc"] = None
                term_content["updated_at_utc"] = now_iso
                term_content["last_error"] = {
                    "error_type": "LeaseExpiredMaxAttemptsExceeded",
                    "message": (
                        f"Job lease expired on attempt {record.attempts}/{record.max_attempts}; "
                        "transitioning to terminal_failure."
                    ),
                    "failed_at_utc": now_iso,
                    "attempt": record.attempts,
                    "retryable": False,
                }
                stmt = (
                    update(ModelReport)
                    .where(ModelReport.report_id == clean_id)
                    .where(ModelReport.version == row.version)
                    .values(content=term_content, version=ModelReport.version + 1)
                )
                self.db.execute(stmt)
                self.db.commit()
                return None

            lease_owner_id = f"auto_lease_{uuid.uuid4().hex}"
            expires_dt = now_dt + timedelta(seconds=self.lease_ttl_seconds)
            new_attempts = record.attempts + 1

            updated_content = dict(row.content or {})
            updated_content["status"] = AutomationJobStatus.RUNNING.value
            updated_content["attempts"] = new_attempts
            updated_content["lease_owner_id"] = lease_owner_id
            updated_content["lease_expires_at_utc"] = expires_dt.isoformat()
            updated_content["renewed_at_utc"] = now_iso
            updated_content["updated_at_utc"] = now_iso

            stmt = (
                update(ModelReport)
                .where(ModelReport.report_id == clean_id)
                .where(ModelReport.version == row.version)
                .values(content=updated_content, version=ModelReport.version + 1)
            )
            res = self.db.execute(stmt)
            if res.rowcount != 1:
                self.db.rollback()
                return None

            self.db.commit()
            fresh_row = (
                self.db.query(ModelReport)
                .filter(ModelReport.report_id == clean_id)
                .populate_existing()
                .first()
            )
            return self._row_to_job_record(fresh_row) if fresh_row is not None else None
        except (IntegrityError, OperationalError):
            self.db.rollback()
            return None

    def renew_job_lease(
        self,
        job_id: str,
        lease_owner_id: str,
        db: Optional[Session] = None,
    ) -> AutomationJobRecord:
        """
        Verifies ownership of `lease_owner_id` and extends `lease_expires_at_utc`.
        Safe to invoke from a separate SQLAlchemy session in the background heartbeat.
        Raises `ConsiliumConcurrencyConflictError` if lease ownership was lost.
        """
        target_db = db or self.db
        clean_id = job_id.strip()
        row = (
            target_db.query(ModelReport)
            .filter(ModelReport.report_id == clean_id)
            .populate_existing()
            .first()
        )
        content = dict(row.content or {}) if row is not None else {}
        if (
            row is None
            or content.get("status") != AutomationJobStatus.RUNNING.value
            or content.get("lease_owner_id") != lease_owner_id
        ):
            raise ConsiliumConcurrencyConflictError(
                f"Automation job lease '{lease_owner_id}' for job '{clean_id}' "
                "is no longer owned by this worker; aborting execution."
            )

        now_dt = self._now_utc()
        now_iso = now_dt.isoformat()
        new_expires_dt = now_dt + timedelta(seconds=self.lease_ttl_seconds)

        updated_content = dict(content)
        updated_content["renewed_at_utc"] = now_iso
        updated_content["lease_expires_at_utc"] = new_expires_dt.isoformat()
        updated_content["renewal_count"] = int(updated_content.get("renewal_count", 0)) + 1
        updated_content["updated_at_utc"] = now_iso

        stmt = (
            update(ModelReport)
            .where(ModelReport.report_id == clean_id)
            .where(ModelReport.version == row.version)
            .where(ModelReport.content["lease_owner_id"].as_string() == lease_owner_id)
            .values(content=updated_content, version=ModelReport.version + 1)
        )
        res = target_db.execute(stmt)
        if res.rowcount != 1:
            target_db.rollback()
            raise ConsiliumConcurrencyConflictError(
                f"Automation job lease '{lease_owner_id}' for job '{clean_id}' "
                "was lost during renewal; aborting execution."
            )
        target_db.commit()

        refreshed = (
            target_db.query(ModelReport)
            .filter(ModelReport.report_id == clean_id)
            .populate_existing()
            .first()
        )
        return self._row_to_job_record(refreshed)

    def complete_job(
        self,
        job_id: str,
        lease_owner_id: str,
        result_metadata: Optional[Dict[str, Any]] = None,
    ) -> AutomationJobRecord:
        """
        Transitions a claimed running job to `completed` with owner fencing.
        """
        clean_id = job_id.strip()
        row = (
            self.db.query(ModelReport)
            .filter(ModelReport.report_id == clean_id)
            .populate_existing()
            .first()
        )
        content = dict(row.content or {}) if row is not None else {}
        if (
            row is None
            or content.get("status") != AutomationJobStatus.RUNNING.value
            or content.get("lease_owner_id") != lease_owner_id
        ):
            raise ConsiliumConcurrencyConflictError(
                f"Cannot complete job '{clean_id}': lease '{lease_owner_id}' is no longer active."
            )

        now_iso = self._now_utc().isoformat()
        updated_content = dict(content)
        updated_content["status"] = AutomationJobStatus.COMPLETED.value
        updated_content["lease_owner_id"] = None
        updated_content["lease_expires_at_utc"] = None
        updated_content["next_attempt_at_utc"] = None
        updated_content["result_metadata"] = dict(result_metadata or {})
        updated_content["updated_at_utc"] = now_iso

        stmt = (
            update(ModelReport)
            .where(ModelReport.report_id == clean_id)
            .where(ModelReport.version == row.version)
            .where(ModelReport.content["lease_owner_id"].as_string() == lease_owner_id)
            .values(content=updated_content, version=ModelReport.version + 1)
        )
        res = self.db.execute(stmt)
        if res.rowcount != 1:
            self.db.rollback()
            raise ConsiliumConcurrencyConflictError(
                f"Cannot complete job '{clean_id}': lease '{lease_owner_id}' was superseded."
            )
        self.db.commit()

        fresh = (
            self.db.query(ModelReport)
            .filter(ModelReport.report_id == clean_id)
            .populate_existing()
            .first()
        )
        return self._row_to_job_record(fresh)

    def fail_job(
        self,
        job_id: str,
        lease_owner_id: str,
        error: BaseException,
        *,
        retryable: bool = True,
    ) -> Optional[AutomationJobRecord]:
        """
        Records a job failure with bounded exponential backoff (`retryable_failure`)
        or transitions to `terminal_failure` when `attempts >= max_attempts` or
        `retryable` is False. Never overwrites a job if `lease_owner_id` was already
        superseded by another worker.
        """
        clean_id = job_id.strip()
        try:
            self.db.rollback()
        except Exception:
            pass

        row = (
            self.db.query(ModelReport)
            .filter(ModelReport.report_id == clean_id)
            .populate_existing()
            .first()
        )
        if row is None:
            return None
        content = dict(row.content or {})
        if content.get("lease_owner_id") != lease_owner_id:
            return self._row_to_job_record(row)

        record = self._row_to_job_record(row)
        now_dt = self._now_utc()
        now_iso = now_dt.isoformat()

        can_retry = bool(retryable and record.attempts < record.max_attempts)
        if can_retry:
            target_status = AutomationJobStatus.RETRYABLE_FAILURE
            delay_seconds = min(
                self.max_backoff_seconds,
                self.base_backoff_seconds * (2 ** max(0, record.attempts - 1)),
            )
            next_attempt_iso: Optional[str] = (
                now_dt + timedelta(seconds=delay_seconds)
            ).isoformat()
        else:
            target_status = AutomationJobStatus.TERMINAL_FAILURE
            next_attempt_iso = None

        updated_content = dict(content)
        updated_content["status"] = target_status.value
        updated_content["lease_owner_id"] = None
        updated_content["lease_expires_at_utc"] = None
        updated_content["next_attempt_at_utc"] = next_attempt_iso
        updated_content["updated_at_utc"] = now_iso
        updated_content["last_error"] = {
            "error_type": type(error).__name__,
            "message": str(error),
            "failed_at_utc": now_iso,
            "attempt": record.attempts,
            "retryable": can_retry,
        }

        stmt = (
            update(ModelReport)
            .where(ModelReport.report_id == clean_id)
            .where(ModelReport.version == row.version)
            .where(ModelReport.content["lease_owner_id"].as_string() == lease_owner_id)
            .values(content=updated_content, version=ModelReport.version + 1)
        )
        res = self.db.execute(stmt)
        if res.rowcount != 1:
            self.db.rollback()
            fresh = (
                self.db.query(ModelReport)
                .filter(ModelReport.report_id == clean_id)
                .populate_existing()
                .first()
            )
            return self._row_to_job_record(fresh) if fresh is not None else None

        self.db.commit()
        fresh = (
            self.db.query(ModelReport)
            .filter(ModelReport.report_id == clean_id)
            .populate_existing()
            .first()
        )
        return self._row_to_job_record(fresh) if fresh is not None else None

    async def _execute_with_job_lease_renewal(
        self,
        job_id: str,
        lease_owner_id: str,
        coro_factory: Callable[[], Awaitable[Any]],
    ) -> Any:
        """
        Executes `coro_factory` while periodically renewing `lease_owner_id` in an
        isolated SQLAlchemy session (`hb_session`), never sharing `self.db` across tasks.
        """
        self.renew_job_lease(job_id, lease_owner_id)
        stop_event = asyncio.Event()
        heartbeat_error: List[Exception] = []
        engine = self.db.get_bind()

        call_task = asyncio.create_task(coro_factory())

        async def _heartbeat() -> None:
            while not stop_event.is_set():
                try:
                    await asyncio.wait_for(
                        stop_event.wait(),
                        timeout=self.lease_heartbeat_interval_seconds,
                    )
                    break
                except asyncio.TimeoutError:
                    if not stop_event.is_set():
                        try:
                            with Session(bind=engine, autocommit=False, autoflush=False) as hb_session:
                                self.renew_job_lease(
                                    job_id, lease_owner_id, db=hb_session
                                )
                        except Exception as exc:
                            heartbeat_error.append(exc)
                            call_task.cancel()
                            break

        hb_task = asyncio.create_task(_heartbeat())
        try:
            try:
                result = await call_task
            except asyncio.CancelledError:
                if heartbeat_error:
                    raise heartbeat_error[0]
                raise
        finally:
            stop_event.set()
            await hb_task

        if heartbeat_error:
            raise heartbeat_error[0]

        self.renew_job_lease(job_id, lease_owner_id)
        return result

    # ==========================================================================
    # STAGE A1/A2 WORKFLOW EXECUTION
    # ==========================================================================

    @staticmethod
    def _is_retryable_exception(exc: BaseException) -> bool:
        if isinstance(
            exc,
            (
                GeminiGatewayError,
                ConsiliumConcurrencyConflictError,
                ReportVersionConflictError,
                OperationalError,
                TimeoutError,
                asyncio.TimeoutError,
            ),
        ):
            return True
        if isinstance(exc, HTTPException):
            return exc.status_code in (408, 409, 429, 500, 502, 503, 504)
        return False

    async def _execute_post_primary_init(
        self, job: AutomationJobRecord
    ) -> Dict[str, Any]:
        """
        Executes Stage A1 post-Primary initialization (`post_primary_init`):
        1. Verifies patient/cycle ownership and that manual Primary Consilium is completed.
        2. Yields if a cycle consilium coordination lock (`lock_consilium_coord_{cycle_id}`) is active.
        3. Generates or retrieves Athena's 7-day Weekly Plan (Stage 1).
        4. If a valid post-Primary initial greeting / T0 schedule anchor already exists
           (e.g., created by the Flutter app), reuses it without calling Gemini.
        5. Otherwise invokes `AthenaChatService.handle_turn` with `event_type="initial_greeting"`
           and the job's stable cycle-scoped idempotency key (`athena_init_greeting_{cycle_id}`).
        6. Enqueues the first planned question (`first_planned_question`) with next_attempt_at_utc = T0 + 30s.
        """
        cycle = self.cycle_repo.get(job.cycle_id)
        if cycle is None or cycle.patient_id != job.patient_id:
            raise ConsiliumCycleNotFoundError(
                f"Active cycle '{job.cycle_id}' not found for patient '{job.patient_id}'."
            )

        if not self.primary_service.is_primary_consilium_completed(job.cycle_id):
            raise ConsiliumContextMissingError(
                f"Cannot execute post-Primary initialization for cycle '{job.cycle_id}': "
                "manual Primary Consilium is not completed."
            )

        active_consilium_lock = self.weekly_service._get_active_in_flight_lock(job.cycle_id)
        if active_consilium_lock is not None:
            lock_content = active_consilium_lock.content or {}
            raise ConsiliumConcurrencyConflictError(
                f"Consilium lease active for cycle '{job.cycle_id}' "
                f"(until {lock_content.get('expires_at_utc')}); deferring post-Primary initialization."
            )

        # 1. If weekly plan is enabled, generate and save Athena's 7-day Weekly Plan (Stage 1)
        plan_id = None
        q1_available_at = None
        if self.enable_weekly_plan:
            from app.services.athena_weekly_plan_service import AthenaWeeklyPlanService
            plan_service = AthenaWeeklyPlanService(
                db=self.db, gateway=self.gateway, now_provider=self.now_provider
            )
            plan = await plan_service.create_or_get_weekly_plan(job.cycle_id, week_number=1)
            plan_id = plan.plan_id

        # 2. Execute or reuse initial greeting at T0
        existing_greeting = self.athena_service.find_existing_post_primary_initial_greeting(
            job.cycle_id
        )
        if existing_greeting is not None:
            anchor_dt = self.weekly_service.find_schedule_anchor_timestamp(job.cycle_id)
            reused = True
            msg_id = existing_greeting.message_id
            effective_anchor = anchor_dt or self._now_utc()
        else:
            response = await self.athena_service.handle_turn(
                patient_id=job.patient_id,
                cycle_id=job.cycle_id,
                request=AthenaChatRequest(
                    message="Start accompaniment",
                    event_type="initial_greeting",
                    idempotency_key=job.idempotency_key,
                ),
            )
            anchor_dt = self.weekly_service.find_schedule_anchor_timestamp(job.cycle_id)
            reused = False
            msg_id = response.message_id
            effective_anchor = anchor_dt or self._now_utc()

        # 3. Schedule first planned question at T0 + 30 seconds if weekly plan enabled
        if self.enable_weekly_plan:
            q1_available_at = effective_anchor + timedelta(seconds=30)
            self.enqueue_first_planned_question(
                cycle_id=job.cycle_id,
                patient_id=job.patient_id,
                available_at_utc=q1_available_at,
            )

        res_meta: Dict[str, Any] = {
            "reused_existing": reused,
            "message_id": msg_id,
            "anchor_timestamp_utc": effective_anchor.isoformat(),
        }
        if plan_id is not None:
            res_meta["weekly_plan_id"] = plan_id
        if q1_available_at is not None:
            res_meta["first_question_available_at_utc"] = q1_available_at.isoformat()
        return res_meta

    async def _execute_first_planned_question(
        self, job: AutomationJobRecord
    ) -> Dict[str, Any]:
        """
        Executes Stage 1 first planned question (`first_planned_question`):
        1. Verifies patient/cycle ownership and coordination lock.
        2. Reuses existing unanswered routine question if already formulated.
        3. Formulates the first targeted question based on Goal 1 from Athena's Weekly Plan.
        4. Registers the question as unanswered routine question in `model_reports`.
        """
        cycle = self.cycle_repo.get(job.cycle_id)
        if cycle is None or cycle.patient_id != job.patient_id:
            raise ConsiliumCycleNotFoundError(
                f"Active cycle '{job.cycle_id}' not found for patient '{job.patient_id}'."
            )

        active_consilium_lock = self.weekly_service._get_active_in_flight_lock(job.cycle_id)
        if active_consilium_lock is not None:
            lock_content = active_consilium_lock.content or {}
            raise ConsiliumConcurrencyConflictError(
                f"Consilium lease active for cycle '{job.cycle_id}' "
                f"(until {lock_content.get('expires_at_utc')}); deferring first planned question."
            )

        active_q = self.athena_service.get_active_routine_question(job.cycle_id)
        if active_q is not None and active_q.get("status") == "unanswered":
            return {
                "reused_existing": True,
                "question_id": active_q.get("question_id"),
                "question_text": active_q.get("question_text"),
            }

        response = await self.athena_service.handle_turn(
            patient_id=job.patient_id,
            cycle_id=job.cycle_id,
            request=AthenaChatRequest(
                message="Ask first planned question",
                event_type="first_planned_question",
                idempotency_key=job.idempotency_key,
            ),
        )
        return {
            "reused_existing": False,
            "message_id": response.message_id,
            "question_id": response.question_id,
            "routine_question_status": response.routine_question_status,
        }

    async def _execute_daily_journal_closure(
        self, job: AutomationJobRecord
    ) -> Dict[str, Any]:
        """
        Executes Stage A2.2 daily journal closure (`daily_journal_closure`):
        1. Verifies patient/cycle ownership, Primary completion, and anchor T0 existence.
        2. Parses calendar_week and calendar_day from idempotency_key.
        3. Persists Layer 1 evidence independently of AI assessment.
        4. When substantive evidence exists, executes one internal non-chat Gemini Flash
           clinical assessment scenario and attaches it as Section D.
        5. When no substantive evidence exists, persists a missing-data journal with
           ZERO Gemini calls.
        """
        cycle = self.cycle_repo.get(job.cycle_id)
        if cycle is None or cycle.patient_id != job.patient_id:
            raise ConsiliumCycleNotFoundError(
                f"Active cycle '{job.cycle_id}' not found for patient '{job.patient_id}'."
            )

        if not self.primary_service.is_primary_consilium_completed(job.cycle_id):
            raise ConsiliumContextMissingError(
                f"Cannot execute daily journal closure for cycle '{job.cycle_id}': "
                "manual Primary Consilium is not completed."
            )

        if self.weekly_service.find_schedule_anchor_timestamp(job.cycle_id) is None:
            raise ConsiliumContextMissingError(
                f"Cannot execute daily journal closure for cycle '{job.cycle_id}': "
                "schedule anchor T0 has not been established yet."
            )

        m = re.search(r'_w(\d+)_d(\d+)$', job.idempotency_key)
        if not m:
            raise ValueError(
                f"Invalid daily journal idempotency key format '{job.idempotency_key}'."
            )
        cal_week = int(m.group(1))
        cal_day = int(m.group(2))

        journal_rec, saved_row, reused = await self.journal_service.close_daily_journal(
            cycle_id=job.cycle_id,
            calendar_week=cal_week,
            calendar_day=cal_day,
            as_of=self._now_utc(),
        )
        return {
            "reused_existing": reused,
            "journal_id": journal_rec.journal_id,
            "calendar_week": cal_week,
            "calendar_day": cal_day,
            "report_id": saved_row.report_id,
            "assessment_available": journal_rec.athena_clinical_assessment.is_available,
        }

    async def _execute_wellness_daily(
        self, job: AutomationJobRecord
    ) -> Dict[str, Any]:
        """
        Executes Stage 3 daily Wellness processing (`wellness_daily`):
        1. Verifies patient/cycle ownership, Primary completion, and anchor T0 existence.
        2. Parses calendar_week and calendar_day from idempotency_key.
        3. Invokes WellnessDailyService.process_wellness_for_day.
        """
        cycle = self.cycle_repo.get(job.cycle_id)
        if cycle is None or cycle.patient_id != job.patient_id:
            raise ConsiliumCycleNotFoundError(
                f"Active cycle '{job.cycle_id}' not found for patient '{job.patient_id}'."
            )

        if not self.primary_service.is_primary_consilium_completed(job.cycle_id):
            raise ConsiliumContextMissingError(
                f"Cannot execute daily wellness processing for cycle '{job.cycle_id}': "
                "manual Primary Consilium is not completed."
            )

        if self.weekly_service.find_schedule_anchor_timestamp(job.cycle_id) is None:
            raise ConsiliumContextMissingError(
                f"Cannot execute daily wellness processing for cycle '{job.cycle_id}': "
                "schedule anchor T0 has not been established yet."
            )

        m = re.search(r'_w(\d+)_d(\d+)$', job.idempotency_key)
        if not m:
            raise ValueError(
                f"Invalid wellness daily idempotency key format '{job.idempotency_key}'."
            )
        cal_week = int(m.group(1))
        cal_day = int(m.group(2))

        report, is_new = await self.wellness_service.process_wellness_for_day(
            cycle_id=job.cycle_id,
            calendar_week=cal_week,
            calendar_day=cal_day,
            as_of=self._now_utc(),
        )

        # Immediate trigger of Athena Daily Journal upon saving Wellness report
        if self.enable_daily_journal:
            self.enqueue_daily_journal_closure(
                cycle_id=job.cycle_id,
                patient_id=job.patient_id,
                calendar_week=cal_week,
                calendar_day=cal_day,
            )

        return {
            "reused_existing": not is_new,
            "report_id": report.report_id,
            "status": report.status.value,
            "calendar_week": cal_week,
            "calendar_day": cal_day,
            "has_interpretation": report.interpretation is not None,
        }

    async def _execute_weekly_consilium_a(
        self, job: AutomationJobRecord
    ) -> Dict[str, Any]:
        """
        Executes Stage 4 Step A: Automatic Weekly Consilium:
        1. Validates cycle, patient ownership, Primary Consilium completion, and anchor T0.
        2. Enforces fixed schedule: refuses execution if as_of < T0 + 168 * sprint_number.
        3. Executes WeeklyConsiliumService.run_weekly_consilium (Flash -> Pro cascade).
        4. Validates structured responses and publishes 8 system reports + overall user report.
        5. Automatically enqueues Step B upon successful completion.
        """
        cycle = self.cycle_repo.get(job.cycle_id)
        if cycle is None or cycle.patient_id != job.patient_id:
            raise ConsiliumCycleNotFoundError(
                f"Active cycle '{job.cycle_id}' not found for patient '{job.patient_id}'."
            )
        if not self.primary_service.is_primary_consilium_completed(job.cycle_id):
            raise ConsiliumContextMissingError(
                f"Cannot execute weekly consilium for cycle '{job.cycle_id}': "
                "manual Primary Consilium is not completed."
            )
        anchor_dt = self.weekly_service.find_schedule_anchor_timestamp(job.cycle_id)
        if anchor_dt is None:
            raise ConsiliumContextMissingError(
                f"Cannot execute weekly consilium for cycle '{job.cycle_id}': "
                "schedule anchor T0 has not been established yet."
            )

        m = re.search(r'_w(\d+)$', job.idempotency_key)
        week_num = int(m.group(1)) if m else 1
        due_at = anchor_dt + timedelta(hours=168 * week_num)
        if self._now_utc() < due_at:
            raise ConsiliumError(
                f"Cannot execute weekly consilium for week {week_num} before scheduled time {due_at.isoformat()}."
            )

        sprint_id = f"sprint_{job.cycle_id}_w{week_num}"
        core_idem = f"weekly_consilium_core_{job.cycle_id}_w{week_num}"
        client_resp = await self.weekly_service.run_weekly_consilium(
            cycle_id=job.cycle_id,
            sprint_id=sprint_id,
            idempotency_key=core_idem,
        )

        # Enqueue Step B immediately
        self.enqueue_weekly_consilium_b(
            cycle_id=job.cycle_id,
            patient_id=job.patient_id,
            sprint_number=week_num,
        )

        return {
            "sprint_id": client_resp.sprint_id,
            "sprint_number": client_resp.sprint_number,
            "overall_report_id": client_resp.overall_report.report_id,
            "overall_report_version": client_resp.overall_report.version,
            "system_reports_count": len(client_resp.system_reports),
            "completed_at_utc": self._now_utc().isoformat(),
        }

    async def _execute_weekly_consilium_b(
        self, job: AutomationJobRecord
    ) -> Dict[str, Any]:
        """
        Executes Stage 4 Step B: Athena's Weekly Goal Plan Formulation:
        1. Verifies Step A completion.
        2. Retrieves updated Pro strategy from Step A.
        3. Invokes AthenaWeeklyPlanService.create_or_get_weekly_plan for week N+1.
        4. Saves plan under SERVER_ONLY, linked to sprint N+1 and Pro strategy version.
        5. Does NOT alter global T0.
        6. Automatically enqueues Step C upon successful completion.
        """
        cycle = self.cycle_repo.get(job.cycle_id)
        if cycle is None or cycle.patient_id != job.patient_id:
            raise ConsiliumCycleNotFoundError(
                f"Active cycle '{job.cycle_id}' not found for patient '{job.patient_id}'."
            )

        m = re.search(r'_w(\d+)$', job.idempotency_key)
        week_num = int(m.group(1)) if m else 1

        sprint_id = f"sprint_{job.cycle_id}_w{week_num}"
        completed_a = self.weekly_service.recover_completed_response(job.cycle_id, sprint_id=sprint_id)
        if completed_a is None:
            step_a_job = self.get_job(
                job.cycle_id,
                AutomationJobType.WEEKLY_CONSILIUM_A,
                f"weekly_consilium_step_a_{job.cycle_id}_w{week_num}",
            )
            if step_a_job is None or step_a_job.status != AutomationJobStatus.COMPLETED:
                raise ConsiliumContextMissingError(
                    f"Cannot execute Step B: Weekly Consilium Step A not completed for week {week_num}."
                )

        from app.services.athena_weekly_plan_service import AthenaWeeklyPlanService
        plan_svc = AthenaWeeklyPlanService(
            db=self.db, gateway=self.gateway, now_provider=self.now_provider
        )
        next_week = week_num + 1
        plan = await plan_svc.create_or_get_weekly_plan(
            cycle_id=job.cycle_id,
            week_number=next_week,
        )

        # Enqueue Step C immediately
        self.enqueue_weekly_consilium_c(
            cycle_id=job.cycle_id,
            patient_id=job.patient_id,
            sprint_number=week_num,
        )

        return {
            "next_week_plan_id": plan.plan_id,
            "week_number": next_week,
            "source_strategy_report_id": plan.source_strategy_report_id,
            "source_strategy_version": plan.source_strategy_version,
            "goals_count": len(plan.goals),
        }

    async def _execute_weekly_consilium_c(
        self, job: AutomationJobRecord
    ) -> Dict[str, Any]:
        """
        Executes Stage 4 Step C: Athena's Weekly Summary Chat Message:
        1. Verifies Step A and Step B completion.
        2. Generates friendly wrap-up message based strictly on published client overall report.
        3. Invites user to view human silhouette / medical vault.
        4. Does NOT ask routine questions or decrement question limits.
        5. Does NOT create daily journals or alter T0.
        """
        cycle = self.cycle_repo.get(job.cycle_id)
        if cycle is None or cycle.patient_id != job.patient_id:
            raise ConsiliumCycleNotFoundError(
                f"Active cycle '{job.cycle_id}' not found for patient '{job.patient_id}'."
            )

        m = re.search(r'_w(\d+)$', job.idempotency_key)
        week_num = int(m.group(1)) if m else 1

        sprint_id = f"sprint_{job.cycle_id}_w{week_num}"
        completed_a = self.weekly_service.recover_completed_response(job.cycle_id, sprint_id=sprint_id)
        if completed_a is None:
            raise ConsiliumContextMissingError(
                f"Cannot execute Step C: Weekly Consilium Step A not completed for week {week_num}."
            )

        chat_resp = await self.athena_service.send_weekly_wrapup_message(
            cycle_id=job.cycle_id,
            sprint_number=week_num,
            idempotency_key=f"athena_weekly_wrapup_{job.cycle_id}_w{week_num}",
        )

        return {
            "message_id": chat_resp.message_id,
            "sprint_number": week_num,
            "reply": chat_resp.reply,
            "created_at_utc": chat_resp.created_at,
        }

    async def _execute_lab_revision(
        self, job: AutomationJobRecord
    ) -> Dict[str, Any]:
        """
        Executes Stage 6B: background Ten-Model Lab Revision Consilium:
        8 Flash-Lite specialists -> Gemini Flash deputy -> Gemini Pro chief.
        1. Atomically captures the pending task and ledger version.
        2. Executes the 10-model analytical cascade via LabRevisionConsiliumService.
        3. Calls LabSchedulingService.complete_revision_task to establish the 72-hour cooldown.
        4. If newer data arrived during run, task remains pending for the next revision.
        """
        cycle = self.cycle_repo.get(job.cycle_id)
        if cycle is None or cycle.patient_id != job.patient_id:
            raise ConsiliumCycleNotFoundError(
                f"Active cycle '{job.cycle_id}' not found for patient '{job.patient_id}'."
            )

        pending_task = self.lab_scheduling_service.get_pending_task(job.cycle_id)
        target_version = pending_task.target_ledger_version if pending_task else None
        source_doc_ids = pending_task.pending_document_ids if pending_task else None

        rev_record = await self.lab_revision_service.execute_lab_revision_consilium(
            cycle_id=job.cycle_id,
            target_ledger_version=target_version,
            source_document_ids=source_doc_ids,
        )

        # Mark revision task completed in LabSchedulingService and initiate 72h cooldown
        self.lab_scheduling_service.complete_revision_task(
            cycle_id=job.cycle_id,
            evaluated_ledger_version=rev_record.evaluated_ledger_version,
        )

        return {
            "report_id": rev_record.report_id,
            "evaluated_ledger_version": rev_record.evaluated_ledger_version,
            "baseline_ledger_version": rev_record.baseline_ledger_version,
            "status": "completed",
            "completed_at_utc": rev_record.completed_at_utc,
            "summary": rev_record.summary,
        }

    async def execute_claimed_job(self, claimed: AutomationJobRecord) -> AutomationJobRecord:
        if not claimed.lease_owner_id:
            raise ConsiliumConcurrencyConflictError(
                f"Job '{claimed.job_id}' has no active lease_owner_id."
            )
        lease_owner_id = claimed.lease_owner_id

        try:
            if claimed.job_type == AutomationJobType.POST_PRIMARY_INIT:
                result_meta = await self._execute_with_job_lease_renewal(
                    claimed.job_id,
                    lease_owner_id,
                    lambda: self._execute_post_primary_init(claimed),
                )
            elif claimed.job_type == AutomationJobType.FIRST_PLANNED_QUESTION:
                result_meta = await self._execute_with_job_lease_renewal(
                    claimed.job_id,
                    lease_owner_id,
                    lambda: self._execute_first_planned_question(claimed),
                )
            elif claimed.job_type == AutomationJobType.DAILY_JOURNAL_CLOSURE:
                result_meta = await self._execute_with_job_lease_renewal(
                    claimed.job_id,
                    lease_owner_id,
                    lambda: self._execute_daily_journal_closure(claimed),
                )
            elif claimed.job_type == AutomationJobType.WELLNESS_DAILY:
                result_meta = await self._execute_with_job_lease_renewal(
                    claimed.job_id,
                    lease_owner_id,
                    lambda: self._execute_wellness_daily(claimed),
                )
            elif claimed.job_type == AutomationJobType.WEEKLY_CONSILIUM_A:
                result_meta = await self._execute_with_job_lease_renewal(
                    claimed.job_id,
                    lease_owner_id,
                    lambda: self._execute_weekly_consilium_a(claimed),
                )
            elif claimed.job_type == AutomationJobType.WEEKLY_CONSILIUM_B:
                result_meta = await self._execute_with_job_lease_renewal(
                    claimed.job_id,
                    lease_owner_id,
                    lambda: self._execute_weekly_consilium_b(claimed),
                )
            elif claimed.job_type == AutomationJobType.WEEKLY_CONSILIUM_C:
                result_meta = await self._execute_with_job_lease_renewal(
                    claimed.job_id,
                    lease_owner_id,
                    lambda: self._execute_weekly_consilium_c(claimed),
                )
            elif claimed.job_type == AutomationJobType.LAB_REVISION:
                result_meta = await self._execute_with_job_lease_renewal(
                    claimed.job_id,
                    lease_owner_id,
                    lambda: self._execute_lab_revision(claimed),
                )
            else:
                raise ValueError(f"Unsupported automation job_type '{claimed.job_type}'.")

            return self.complete_job(
                claimed.job_id,
                lease_owner_id,
                result_metadata=result_meta,
            )
        except Exception as exc:
            retryable = self._is_retryable_exception(exc)
            failed = self.fail_job(
                claimed.job_id,
                lease_owner_id,
                exc,
                retryable=retryable,
            )
            if failed is not None:
                return failed
            raise

    async def process_due_jobs(
        self,
        *,
        discover: bool = True,
        discover_daily_journals: Optional[bool] = None,
        limit: int = 20,
    ) -> List[AutomationJobRecord]:
        """
        Discovers completed manual Primary Consiliums needing post-Primary initialization,
        claims due jobs atomically, and executes them.
        """
        if discover:
            self.discover_and_enqueue_jobs(discover_daily_journals=discover_daily_journals)

        now_dt = self._now_utc()
        candidates = [
            job for job in self.list_jobs() if self.is_job_claimable(job, now_dt)
        ]

        processed: List[AutomationJobRecord] = []
        for candidate in candidates[: max(1, int(limit))]:
            claimed = self.claim_job(candidate.job_id)
            if claimed is None:
                continue
            outcome = await self.execute_claimed_job(claimed)
            processed.append(outcome)
        return processed


class ClinicalAutomationWorker:
    """
    Minimal background worker supporting periodic polling and immediate wake-up
    signals (`notify()`) without depending on the Flutter application being open.
    Each tick opens a dedicated SQLAlchemy session from `session_factory`.
    """

    def __init__(
        self,
        session_factory: Callable[[], Session],
        *,
        gateway_factory: Optional[Callable[[], GeminiGateway]] = None,
        now_provider: Optional[Callable[[], datetime]] = None,
        poll_interval_seconds: float = DEFAULT_WORKER_POLL_INTERVAL_SECONDS,
        lease_ttl_seconds: int = DEFAULT_JOB_LEASE_TTL_SECONDS,
        lease_heartbeat_interval_seconds: float = DEFAULT_JOB_HEARTBEAT_SECONDS,
        base_backoff_seconds: int = DEFAULT_BASE_BACKOFF_SECONDS,
        max_backoff_seconds: int = DEFAULT_MAX_BACKOFF_SECONDS,
        enable_daily_journal: bool = False,
        enable_weekly_plan: bool = False,
        enable_wellness_daily: bool = False,
        enable_automatic_weekly: bool = False,
        enable_lab_revision: bool = False,
    ):
        self.session_factory = session_factory
        self.gateway_factory = gateway_factory or GeminiGateway
        self.now_provider = now_provider or (lambda: datetime.now(timezone.utc))
        self.poll_interval_seconds = max(0.01, float(poll_interval_seconds))
        self.lease_ttl_seconds = lease_ttl_seconds
        self.lease_heartbeat_interval_seconds = lease_heartbeat_interval_seconds
        self.base_backoff_seconds = base_backoff_seconds
        self.max_backoff_seconds = max_backoff_seconds
        self.enable_daily_journal = enable_daily_journal
        self.enable_weekly_plan = enable_weekly_plan
        self.enable_wellness_daily = enable_wellness_daily
        self.enable_automatic_weekly = enable_automatic_weekly
        self.enable_lab_revision = enable_lab_revision

        self._wake_event: Optional[asyncio.Event] = None
        self._stop_event: Optional[asyncio.Event] = None
        self._task: Optional[asyncio.Task[None]] = None

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    def notify(self) -> None:
        """Signals the worker loop to wake up immediately and process due jobs."""
        if self._wake_event is not None:
            self._wake_event.set()

    async def run_once(
        self,
        *,
        discover: bool = True,
        discover_daily_journals: Optional[bool] = None,
        limit: int = 20,
    ) -> List[AutomationJobRecord]:
        """Executes a single deterministic polling and execution tick."""
        with self.session_factory() as db:
            service = ClinicalAutomationService(
                db=db,
                gateway=self.gateway_factory(),
                now_provider=self.now_provider,
                lease_ttl_seconds=self.lease_ttl_seconds,
                lease_heartbeat_interval_seconds=self.lease_heartbeat_interval_seconds,
                base_backoff_seconds=self.base_backoff_seconds,
                max_backoff_seconds=self.max_backoff_seconds,
                enable_daily_journal=self.enable_daily_journal,
                enable_weekly_plan=self.enable_weekly_plan,
                enable_wellness_daily=self.enable_wellness_daily,
                enable_automatic_weekly=self.enable_automatic_weekly,
                enable_lab_revision=self.enable_lab_revision,
            )
            return await service.process_due_jobs(
                discover=discover,
                discover_daily_journals=discover_daily_journals,
                limit=limit,
            )

    async def _run_loop(self) -> None:
        assert self._wake_event is not None
        assert self._stop_event is not None
        while not self._stop_event.is_set():
            self._wake_event.clear()
            try:
                await self.run_once(discover=True)
            except Exception as exc:
                logger.error(f"ClinicalAutomationWorker tick error: {exc}")

            if self._stop_event.is_set():
                break

            wake_task = asyncio.create_task(self._wake_event.wait())
            stop_task = asyncio.create_task(self._stop_event.wait())
            try:
                await asyncio.wait(
                    {wake_task, stop_task},
                    timeout=self.poll_interval_seconds,
                    return_when=asyncio.FIRST_COMPLETED,
                )
            finally:
                for t in (wake_task, stop_task):
                    if not t.done():
                        t.cancel()

    async def start(self) -> None:
        if self.is_running:
            return
        self._wake_event = asyncio.Event()
        self._stop_event = asyncio.Event()
        self._task = asyncio.create_task(self._run_loop())

    async def stop(self) -> None:
        if self._stop_event is not None:
            self._stop_event.set()
        if self._wake_event is not None:
            self._wake_event.set()
        if self._task is not None:
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
