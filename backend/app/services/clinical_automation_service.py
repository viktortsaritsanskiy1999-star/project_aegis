import asyncio
from datetime import datetime, timedelta, timezone
from enum import Enum
import hashlib
import logging
import re
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple
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
    - `daily_journal_closure`: evidence-preserving daily journal closure + internal Gemini Flash assessment (Stage A2.2).
    """
    POST_PRIMARY_INIT = "post_primary_init"
    DAILY_JOURNAL_CLOSURE = "daily_journal_closure"


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
    ):
        self.db = db
        self.gateway = gateway or GeminiGateway()
        self.now_provider = now_provider or (lambda: datetime.now(timezone.utc))
        self.lease_ttl_seconds = max(1, int(lease_ttl_seconds))
        self.lease_heartbeat_interval_seconds = max(0.001, float(lease_heartbeat_interval_seconds))
        self.base_backoff_seconds = max(1, int(base_backoff_seconds))
        self.max_backoff_seconds = max(self.base_backoff_seconds, int(max_backoff_seconds))
        self.enable_daily_journal = enable_daily_journal

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
                "First Primary Consilium is manual; only 'post_primary_init' and 'daily_journal_closure' are allowed."
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
            next_attempt_at_utc=now_iso,
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

            # Discover completed observation days requiring daily journal closure
            if should_discover_dj:
                anchor_dt = self.weekly_service.find_schedule_anchor_timestamp(cycle.cycle_id)
                if anchor_dt is not None:
                    completed_windows = self.journal_service.get_completed_daily_windows(
                        cycle.cycle_id, as_of=self._now_utc()
                    )
                    for w, d, _, _ in completed_windows:
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
        3. If a valid post-Primary initial greeting / T0 schedule anchor already exists
           (e.g., created by the Flutter app), reuses it without calling Gemini.
        4. Otherwise invokes `AthenaChatService.handle_turn` with `event_type="initial_greeting"`
           and the job's stable cycle-scoped idempotency key (`athena_init_greeting_{cycle_id}`).
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

        existing_greeting = self.athena_service.find_existing_post_primary_initial_greeting(
            job.cycle_id
        )
        if existing_greeting is not None:
            anchor_dt = self.weekly_service.find_schedule_anchor_timestamp(job.cycle_id)
            return {
                "reused_existing": True,
                "message_id": existing_greeting.message_id,
                "anchor_timestamp_utc": (
                    anchor_dt.isoformat() if anchor_dt is not None else existing_greeting.created_at
                ),
            }

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
        return {
            "reused_existing": False,
            "message_id": response.message_id,
            "anchor_timestamp_utc": (
                anchor_dt.isoformat() if anchor_dt is not None else response.created_at
            ),
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
            elif claimed.job_type == AutomationJobType.DAILY_JOURNAL_CLOSURE:
                result_meta = await self._execute_with_job_lease_renewal(
                    claimed.job_id,
                    lease_owner_id,
                    lambda: self._execute_daily_journal_closure(claimed),
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
