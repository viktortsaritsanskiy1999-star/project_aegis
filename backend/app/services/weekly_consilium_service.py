import asyncio
from datetime import datetime, timedelta, timezone
import json
import re
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple
import uuid
from sqlalchemy import delete, select, text as sql_text, update
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from app.models.model_report import ModelReport
from app.repositories.cycle_repository import MedicalCycleRepository
from app.repositories.measurement_repository import HealthMeasurementRepository
from app.repositories.report_repository import (
    ModelReportRepository,
    ReportVersionConflictError,
)
from app.repositories.sprint_repository import WeeklySprintRepository
from app.schemas.consilium import (
    SYSTEM_KEYS,
    SYSTEM_TITLES,
    AegisAgentRole,
    WeeklyFlashMasterOutput,
    WeeklyProMasterOutput,
    AthenaClinicalLogEntry,
    ActiveAthenaStrategyContext,
    AthenaSessionState,
    WeeklyConsiliumClientResponse,
    WeeklySprintLifecycleState,
    validate_no_guaranteed_unchanged_claim,
)
from app.schemas.privacy import PrivacyLevel
from app.schemas.reports import ClientModelReportResponse
from app.services.consilium_prompts import (
    build_weekly_flash_prompt,
    build_weekly_pro_prompt,
    build_athena_system_prompt,
)
from app.services.gemini_gateway import GeminiGateway, GeminiModelRole
from app.services.primary_consilium_service import (
    ConsiliumError,
    ConsiliumCycleNotFoundError,
    ConsiliumContextMissingError,
    ConsiliumQualityRejectionError,
    ConsiliumConcurrencyConflictError,
    PREPARATORY_ANAMNESIS_REPORT_TYPE,
    PREPARATORY_OCR_REPORT_TYPE,
    PREPARATORY_ECG_REPORT_TYPE,
)


ATHENA_CLINICAL_LOG_REPORT_TYPE = "athena_clinical_log"
ATHENA_CHAT_HISTORY_REPORT_TYPE = "athena_chat_history"
ATHENA_ACTIVE_STRATEGY_REPORT_TYPE = "athena_active_strategy"
WEEKLY_DEPUTY_INTERNAL_REPORT_TYPE = "weekly_deputy_internal"
WEEKLY_CHIEF_INTERNAL_REPORT_TYPE = "weekly_chief_physician_internal"
WEEKLY_OVERALL_CLIENT_REPORT_TYPE = "weekly_user_overall_report"

WEEKLY_CONSILIUM_LOCK_TTL_SECONDS: int = 120
WEEKLY_CONSILIUM_LOCK_HEARTBEAT_SECONDS: float = 15.0
WEEKLY_CONSILIUM_IN_FLIGHT_LOCK_REPORT_TYPE: str = "weekly_consilium_in_flight_lock"
WEEKLY_CONSILIUM_EXECUTION_LOG_REPORT_TYPE: str = "weekly_consilium_execution_log"
CYCLE_CONSILIUM_COORDINATION_LOCK_REPORT_TYPE: str = "cycle_consilium_coordination_lock"
ATHENA_SCHEDULE_ANCHOR_REPORT_TYPE: str = "athena_schedule_anchor"
WEEKLY_SPRINT_HOURS: int = 168


class ConsiliumSprintConflictError(ConsiliumConcurrencyConflictError):
    pass


class WeeklyConsiliumService:
    """
    Server-side orchestrator for the Weekly Medical Consilium (Stage 3.3):
    Strictly executes: Gemini Flash (Deputy Chief Physician) -> Gemini Pro (Chief Physician).
    - Never calls the 8 Flash-Lite organ specialists, Vision OCR/ECG, or Anamnesis M1.
    - Reads previous internal conclusions, active strategy, 7-day Athena diary, and new vitals.
    - Generates 8 dated user-facing Atlas system updates (with friendly messages when no new info is present)
      and a separate weekly overall report for the Client Vault.
    - Persists the updated active Athena strategy linked to cycle_id, sprint_id, and version,
      ensuring subsequent Athena interactions always refresh their system instruction without
      losing chat history.
    - Protected by distributed SQLite execution lease, heartbeat renewal, and fencing.
    """

    _default_weekly_locks: Dict[str, asyncio.Lock] = {}

    def __init__(
        self,
        db: Session,
        gateway: Optional[GeminiGateway] = None,
        now_provider: Optional[Callable[[], datetime]] = None,
        lock_registry: Optional[Dict[str, asyncio.Lock]] = None,
        lease_ttl_seconds: int = WEEKLY_CONSILIUM_LOCK_TTL_SECONDS,
        lease_heartbeat_interval_seconds: float = WEEKLY_CONSILIUM_LOCK_HEARTBEAT_SECONDS,
    ):
        self.db = db
        self.gateway = gateway or GeminiGateway()
        self.now_provider = now_provider or (lambda: datetime.now(timezone.utc))
        self.cycle_repo = MedicalCycleRepository(db)
        self.sprint_repo = WeeklySprintRepository(db)
        self.report_repo = ModelReportRepository(db)
        self.measurement_repo = HealthMeasurementRepository(db)
        self._weekly_locks = lock_registry if lock_registry is not None else self._default_weekly_locks
        self.lease_ttl_seconds = max(1, int(lease_ttl_seconds))
        self.lease_heartbeat_interval_seconds = max(0.001, float(lease_heartbeat_interval_seconds))

    def _get_weekly_lock(self, cycle_id: str) -> asyncio.Lock:
        if cycle_id not in self._weekly_locks:
            self._weekly_locks[cycle_id] = asyncio.Lock()
        return self._weekly_locks[cycle_id]

    def _now_utc(self) -> datetime:
        dt = self.now_provider()
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)

    @staticmethod
    def _coordination_lock_id(cycle_id: str) -> str:
        return f"lock_consilium_coord_{cycle_id}"[:64]

    @staticmethod
    def _lock_report_id(cycle_id: str) -> str:
        return f"lock_weekly_{cycle_id}"[:64]

    def _fetch_fresh_lock_row(self, cycle_id: str) -> Optional[ModelReport]:
        lock_id = self._lock_report_id(cycle_id)
        return (
            self.db.query(ModelReport)
            .filter(ModelReport.report_id == lock_id)
            .populate_existing()
            .first()
        )

    def _get_active_in_flight_lock(self, cycle_id: str) -> Optional[ModelReport]:
        """
        Checks for an active cross-process execution lease row in `model_reports`.
        Checks both the cycle coordination lock and weekly-specific lock.
        If an expired lease row exists from a crashed or hung worker (> TTL without renewal),
        cleans it up atomically using a WHERE condition matching the exact stale owner and expiration,
        ensuring that a concurrent worker's active or renewed lease is NEVER deleted.
        """
        coord_id = self._coordination_lock_id(cycle_id)
        lock_id = self._lock_report_id(cycle_id)
        now_dt = self._now_utc()

        coord_row = (
            self.db.query(ModelReport)
            .filter(ModelReport.report_id == coord_id)
            .populate_existing()
            .first()
        )
        lock_row = (
            self.db.query(ModelReport)
            .filter(ModelReport.report_id == lock_id)
            .populate_existing()
            .first()
        )

        # Cleanup expired coordination lock if stale
        if coord_row is not None:
            coord_content = coord_row.content or {}
            stale_owner_id = coord_content.get("lease_owner_id")
            stale_expires_iso = coord_content.get("expires_at_utc")
            expires_dt = self._parse_iso_datetime_utc(stale_expires_iso)
            if expires_dt is not None and expires_dt <= now_dt and stale_owner_id and stale_expires_iso:
                all_cycle_lock_ids = [
                    coord_id,
                    lock_id,
                    f"lock_primary_{cycle_id}"[:64],
                    f"lock_lab_rev_{cycle_id}"[:64],
                ]
                stmt = (
                    delete(ModelReport)
                    .where(ModelReport.report_id.in_(all_cycle_lock_ids))
                    .where(ModelReport.content["lease_owner_id"].as_string() == stale_owner_id)
                    .where(ModelReport.content["expires_at_utc"].as_string() == stale_expires_iso)
                )
                self.db.execute(stmt)
                self.db.commit()
                coord_row = None

        # Cleanup expired specific lock if stale
        if lock_row is not None:
            lock_content = lock_row.content or {}
            stale_owner_id = lock_content.get("lease_owner_id")
            stale_expires_iso = lock_content.get("expires_at_utc")
            expires_dt = self._parse_iso_datetime_utc(stale_expires_iso)
            if expires_dt is not None and expires_dt <= now_dt and stale_owner_id and stale_expires_iso:
                stmt = (
                    delete(ModelReport)
                    .where(ModelReport.report_id == lock_id)
                    .where(ModelReport.content["lease_owner_id"].as_string() == stale_owner_id)
                    .where(ModelReport.content["expires_at_utc"].as_string() == stale_expires_iso)
                )
                self.db.execute(stmt)
                self.db.commit()
                lock_row = None

        # Re-verify active rows
        fresh_coord = (
            self.db.query(ModelReport)
            .filter(ModelReport.report_id == coord_id)
            .populate_existing()
            .first()
        )
        if fresh_coord is not None:
            exp = self._parse_iso_datetime_utc((fresh_coord.content or {}).get("expires_at_utc"))
            if exp is not None and exp > now_dt:
                return fresh_coord

        fresh_lock = (
            self.db.query(ModelReport)
            .filter(ModelReport.report_id == lock_id)
            .populate_existing()
            .first()
        )
        if fresh_lock is not None:
            exp = self._parse_iso_datetime_utc((fresh_lock.content or {}).get("expires_at_utc"))
            if exp is not None and exp > now_dt:
                return fresh_lock

        return None

    def _acquire_cross_process_lease(
        self, cycle_id: str, idempotency_key: Optional[str] = None
    ) -> str:
        """
        Acquires an atomic database-level execution lease backed by the SQLite PRIMARY KEY
        constraint on `model_reports.report_id` for both the master cycle coordination lock
        (`report_id = lock_consilium_coord_{cycle_id}`) and weekly-specific lock
        (`report_id = lock_weekly_{cycle_id}`).
        Guarantees zero cross-consilium race conditions with Primary Consilium.
        """
        active_lock = self._get_active_in_flight_lock(cycle_id)
        if active_lock is not None:
            lock_content = active_lock.content or {}
            consilium_type = lock_content.get("consilium_type", "weekly")
            if consilium_type == "primary":
                prefix = "Primary Consilium"
            elif consilium_type == "lab_revision":
                prefix = "Lab-Revision Consilium"
            else:
                prefix = "Weekly Consilium"
            raise ConsiliumConcurrencyConflictError(
                f"{prefix} for cycle '{cycle_id}' is currently in progress "
                f"(lease active until {lock_content.get('expires_at_utc')})."
            )

        now_dt = self._now_utc()
        expires_dt = now_dt + timedelta(seconds=self.lease_ttl_seconds)
        coord_id = self._coordination_lock_id(cycle_id)
        lock_id = self._lock_report_id(cycle_id)
        lease_owner_id = f"lease_weekly_{uuid.uuid4().hex}"

        try:
            coord_report = ModelReport(
                report_id=coord_id,
                cycle_id=cycle_id,
                sprint_id=None,
                session_id=None,
                model="aegis-concurrency-guard",
                report_type=CYCLE_CONSILIUM_COORDINATION_LOCK_REPORT_TYPE,
                privacy_level=PrivacyLevel.SERVER_ONLY.value,
                version=1,
                content={
                    "lease_owner_id": lease_owner_id,
                    "consilium_type": "weekly",
                    "idempotency_key": idempotency_key,
                    "acquired_at_utc": now_dt.isoformat(),
                    "renewed_at_utc": now_dt.isoformat(),
                    "expires_at_utc": expires_dt.isoformat(),
                    "renewal_count": 0,
                },
                created_at=now_dt,
            )
            lock_report = ModelReport(
                report_id=lock_id,
                cycle_id=cycle_id,
                sprint_id=None,
                session_id=None,
                model="aegis-concurrency-guard",
                report_type=WEEKLY_CONSILIUM_IN_FLIGHT_LOCK_REPORT_TYPE,
                privacy_level=PrivacyLevel.SERVER_ONLY.value,
                version=1,
                content={
                    "lease_owner_id": lease_owner_id,
                    "consilium_type": "weekly",
                    "idempotency_key": idempotency_key,
                    "acquired_at_utc": now_dt.isoformat(),
                    "renewed_at_utc": now_dt.isoformat(),
                    "expires_at_utc": expires_dt.isoformat(),
                    "renewal_count": 0,
                },
                created_at=now_dt,
            )
            self.db.add(coord_report)
            self.db.add(lock_report)
            self.db.commit()
            return lease_owner_id
        except (IntegrityError, OperationalError):
            self.db.rollback()
            raise ConsiliumConcurrencyConflictError(
                f"Weekly Consilium for cycle '{cycle_id}' is currently in progress."
            )

    def _renew_cross_process_lease(
        self, cycle_id: str, lease_owner_id: str, db: Optional[Session] = None
    ) -> None:
        """
        Verifies that `lease_owner_id` still owns the SQLite execution lease and extends
        `expires_at_utc` by `self.lease_ttl_seconds` using an atomic SQLite UPDATE.
        If the lease was lost or replaced by another owner, res.rowcount is 0,
        raising `ConsiliumConcurrencyConflictError` immediately (fencing guard).
        """
        target_db = db or self.db
        coord_id = self._coordination_lock_id(cycle_id)
        lock_id = self._lock_report_id(cycle_id)
        lock_row = (
            target_db.query(ModelReport)
            .filter(ModelReport.report_id == lock_id)
            .populate_existing()
            .first()
        )
        lock_content = dict(lock_row.content or {}) if lock_row is not None else {}
        if lock_row is None or lock_content.get("lease_owner_id") != lease_owner_id:
            raise ConsiliumConcurrencyConflictError(
                f"Execution lease '{lease_owner_id}' for cycle '{cycle_id}' is no longer "
                "owned by this worker; aborting stale consilium execution."
            )

        now_dt = self._now_utc()
        new_expires_dt = now_dt + timedelta(seconds=self.lease_ttl_seconds)
        updated_content = dict(lock_content)
        updated_content["renewed_at_utc"] = now_dt.isoformat()
        updated_content["expires_at_utc"] = new_expires_dt.isoformat()
        updated_content["renewal_count"] = int(updated_content.get("renewal_count", 0)) + 1

        stmt = (
            update(ModelReport)
            .where(ModelReport.report_id.in_([coord_id, lock_id]))
            .where(ModelReport.content["lease_owner_id"].as_string() == lease_owner_id)
            .values(content=updated_content, version=ModelReport.version + 1)
        )
        res = target_db.execute(stmt)
        if res.rowcount != 2:
            target_db.rollback()
            raise ConsiliumConcurrencyConflictError(
                f"Execution lease '{lease_owner_id}' for cycle '{cycle_id}' was partially or completely lost "
                f"(expected 2 lock rows renewed, got {res.rowcount}); aborting stale consilium execution."
            )
        target_db.commit()

    async def _execute_with_lease_renewal(
        self,
        cycle_id: str,
        lease_owner_id: str,
        coro_factory: Callable[[], Awaitable[Any]],
    ) -> Any:
        self._renew_cross_process_lease(cycle_id, lease_owner_id)
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
                                self._renew_cross_process_lease(
                                    cycle_id, lease_owner_id, db=hb_session
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

        self._renew_cross_process_lease(cycle_id, lease_owner_id)
        return result

    def _verify_and_delete_cross_process_lease_uncommitted(
        self, cycle_id: str, lease_owner_id: str
    ) -> None:
        """
        Verifies ownership and atomically deletes the SQLite execution lease (both coordination
        and weekly-specific lock rows) inside an UNCOMMITTED transaction before persisting reports.
        If another worker stole the lease (e.g. heartbeat stalled > TTL), rowcount is 0,
        which triggers a rollback and blocks publication of all client-facing reports.
        Never deletes a lease owned by another worker.
        """
        coord_id = self._coordination_lock_id(cycle_id)
        lock_id = self._lock_report_id(cycle_id)
        stmt = (
            delete(ModelReport)
            .where(ModelReport.report_id.in_([coord_id, lock_id]))
            .where(ModelReport.content["lease_owner_id"].as_string() == lease_owner_id)
        )
        res = self.db.execute(stmt)
        if res.rowcount < 2:
            raise ConsiliumConcurrencyConflictError(
                f"Cannot commit Weekly Consilium for cycle '{cycle_id}': "
                f"execution lease '{lease_owner_id}' was lost or superseded by another owner."
            )

    def _release_cross_process_lease_safe(self, cycle_id: str, lease_owner_id: str) -> None:
        """
        Safely deletes the SQLite execution lease on execution failure or abnormal termination.
        Uses an isolated session with an atomic DELETE matching lease_owner_id.
        """
        try:
            coord_id = self._coordination_lock_id(cycle_id)
            lock_id = self._lock_report_id(cycle_id)
            stmt = (
                delete(ModelReport)
                .where(ModelReport.report_id.in_([coord_id, lock_id]))
                .where(ModelReport.content["lease_owner_id"].as_string() == lease_owner_id)
            )
            with Session(bind=self.db.get_bind(), autocommit=False, autoflush=False) as safe_session:
                safe_session.execute(stmt)
                safe_session.commit()
        except Exception:
            pass

    def recover_completed_response(
        self, cycle_id: str, sprint_id: Optional[str] = None
    ) -> Optional[WeeklyConsiliumClientResponse]:
        """
        Recovers existing client-facing Weekly Consilium reports directly from storage.
        Guarantees zero duplicate Gemini API calls on client retries or reconnections.
        """
        if sprint_id is not None and sprint_id.strip():
            clean_sprint = sprint_id.strip()
            existing_sprint = self.sprint_repo.get(clean_sprint)
            if existing_sprint is not None and existing_sprint.cycle_id != cycle_id:
                raise ConsiliumSprintConflictError(
                    f"Sprint '{clean_sprint}' belongs to cycle '{existing_sprint.cycle_id}', not '{cycle_id}'."
                )

        overall_reports = self.report_repo.list_client_facing(
            cycle_id=cycle_id,
            report_type=WEEKLY_OVERALL_CLIENT_REPORT_TYPE,
        )
        if not overall_reports:
            return None

        target_report: Optional[ModelReport] = None
        if sprint_id is not None and sprint_id.strip():
            clean_sprint = sprint_id.strip()
            for r in overall_reports:
                if r.sprint_id == clean_sprint:
                    target_report = r
                    break
            if target_report is None:
                return None
        else:
            target_report = overall_reports[0]

        overall_validated = ClientModelReportResponse.model_validate(target_report)
        content = target_report.content or {}
        report_date = str(content.get("report_date") or self._now_utc().strftime("%Y-%m-%d"))
        metrics = content.get("metrics") or {}
        resolved_sprint_number = int(metrics.get("sprint_number", 1))
        resolved_sprint_id = target_report.sprint_id or f"sprint_{cycle_id}_w{resolved_sprint_number}"

        expected_sprint_id = target_report.sprint_id or resolved_sprint_id
        system_reports: Dict[str, ClientModelReportResponse] = {}
        for sys_key in SYSTEM_KEYS:
            sys_reps = self.report_repo.list_client_facing(
                cycle_id=cycle_id,
                report_type=f"system_user_report_{sys_key}",
            )
            matching_rep = None
            for s in sys_reps:
                if s.sprint_id == expected_sprint_id:
                    matching_rep = s
                    break
            if matching_rep is not None:
                system_reports[sys_key] = ClientModelReportResponse.model_validate(matching_rep)

        if len(system_reports) != len(SYSTEM_KEYS):
            return None

        return WeeklyConsiliumClientResponse(
            cycle_id=cycle_id,
            sprint_id=resolved_sprint_id,
            sprint_number=resolved_sprint_number,
            report_date=report_date,
            overall_report=overall_validated,
            system_reports=system_reports,
        )

    def record_schedule_anchor(
        self,
        cycle_id: str,
        anchor_dt: Optional[datetime] = None,
        source: str = "first_daily_question",
        sprint_id: Optional[str] = None,
        commit: bool = True,
    ) -> ModelReport:
        """
        Persists authoritative schedule anchor for cycle_id.
        Represents the first confirmed meaningful daily question/message of Athena after Primary Consilium.
        If an anchor report already exists for this cycle, returns the initial confirmed record T0 without creating a new version.
        """
        if not self.cycle_repo.get(cycle_id):
            raise ConsiliumCycleNotFoundError(f"Medical cycle '{cycle_id}' not found.")

        existing_anchors = self.report_repo.list_server_only(
            cycle_id=cycle_id,
            report_type=ATHENA_SCHEDULE_ANCHOR_REPORT_TYPE,
        )
        if existing_anchors:
            return min(
                existing_anchors,
                key=lambda r: (r.version, r.created_at or datetime.min.replace(tzinfo=timezone.utc)),
            )

        effective_dt = anchor_dt or self._now_utc()
        if effective_dt.tzinfo is None:
            effective_dt = effective_dt.replace(tzinfo=timezone.utc)
        else:
            effective_dt = effective_dt.astimezone(timezone.utc)

        try:
            return self.report_repo.create_next_version(
                cycle_id=cycle_id,
                sprint_id=sprint_id,
                model=self.gateway.flash_model,
                report_type=ATHENA_SCHEDULE_ANCHOR_REPORT_TYPE,
                privacy_level=PrivacyLevel.SERVER_ONLY,
                content={
                    "agent_role": AegisAgentRole.ATHENA_CURATOR.value,
                    "anchor_timestamp_utc": effective_dt.isoformat(),
                    "source": source,
                    "created_at_utc": self._now_utc().isoformat(),
                },
                expected_version=1,
                max_retries=0,
                commit=commit,
            )
        except (ReportVersionConflictError, IntegrityError, OperationalError):
            if not commit:
                raise
            self.db.rollback()
            self.db.expire_all()
            existing_anchors = self.report_repo.list_server_only(
                cycle_id=cycle_id,
                report_type=ATHENA_SCHEDULE_ANCHOR_REPORT_TYPE,
            )
            if existing_anchors:
                return min(
                    existing_anchors,
                    key=lambda r: (r.version, r.created_at or datetime.min.replace(tzinfo=timezone.utc)),
                )
            raise

    def find_schedule_anchor_timestamp(self, cycle_id: str) -> Optional[datetime]:
        """
        Locates the confirmed anchor UTC timestamp for the cycle's 168-hour weekly schedule grid.
        Defined as the first confirmed meaningful daily question/message of Athena after Primary Consilium.

        Rules:
        1. Exclusively uses 'athena_schedule_anchor' server-only report (single source of truth).
        2. Uses the initial confirmed record T0 (version 1 / earliest), never latest version.
        3. Never falls back on user text strings [Daily Pulse] / [Daily Question] or clinical logs.
        4. If none found, returns None (never guesses the start date).
        """
        anchor_reports = self.report_repo.list_server_only(
            cycle_id=cycle_id,
            report_type=ATHENA_SCHEDULE_ANCHOR_REPORT_TYPE,
        )
        if anchor_reports:
            sorted_anchors = sorted(
                anchor_reports,
                key=lambda r: (r.version, r.created_at or datetime.min.replace(tzinfo=timezone.utc)),
            )
            for r in sorted_anchors:
                content = r.content or {}
                raw_ts = content.get("anchor_timestamp_utc")
                dt = self._parse_iso_datetime_utc(raw_ts)
                if dt is not None:
                    return dt

        return None

    def get_authoritative_calendar_schedule(
        self, cycle_id: str, as_of: Optional[datetime] = None
    ) -> Tuple[Optional[int], Optional[int], Optional[datetime]]:
        """
        Calculates authoritative calendar schedule from persisted Athena schedule anchor T0:
        - current_calendar_week: N = max(1, int(hours_elapsed // 168) + 1)
        - current_calendar_day: 1..7 within week N: min(7, max(1, int((hours_elapsed % 168) // 24) + 1))
        - next_weekly_boundary_dt: anchor_dt + N * 168 hours
        If anchor_dt is not found (prior to first post-Primary question), returns (None, None, None).
        """
        anchor_dt = self.find_schedule_anchor_timestamp(cycle_id)
        if anchor_dt is None:
            return (None, None, None)

        effective_dt = as_of or self._now_utc()
        if effective_dt.tzinfo is None:
            effective_dt = effective_dt.replace(tzinfo=timezone.utc)
        else:
            effective_dt = effective_dt.astimezone(timezone.utc)

        if effective_dt < anchor_dt:
            return (1, 1, anchor_dt + timedelta(hours=WEEKLY_SPRINT_HOURS))

        hours_elapsed = (effective_dt - anchor_dt).total_seconds() / 3600.0
        week_n = max(1, int(hours_elapsed // WEEKLY_SPRINT_HOURS) + 1)
        hours_in_week = hours_elapsed % WEEKLY_SPRINT_HOURS
        day_n = min(7, max(1, int(hours_in_week // 24) + 1))
        next_boundary = anchor_dt + timedelta(hours=WEEKLY_SPRINT_HOURS * week_n)

        return (week_n, day_n, next_boundary)

    def get_weekly_sprint_lifecycle_state(
        self, cycle_id: str
    ) -> WeeklySprintLifecycleState:
        """
        Calculates authoritative server lifecycle state for weekly sprints in an active cycle
        based on fixed 168-hour periods anchored at Athena's first confirmed daily question.
        - Point of reference (anchor): first confirmed daily question/message of Athena after Primary Consilium.
        - Fixed 168-hour grid: period N boundary = anchor_dt + N * 168h.
        - Early requests before 168h boundary are rejected (can_start_next_sprint=False).
        - Late execution does NOT shift subsequent schedule boundaries.
        - Patient answer count does not determine or delay the schedule.
        - If anchor event does not exist, refuses to guess start date (can_start_next_sprint=False).
        """
        from app.services.primary_consilium_service import PrimaryConsiliumService

        primary_service = PrimaryConsiliumService(
            db=self.db, gateway=self.gateway, now_provider=self.now_provider
        )
        if not primary_service.is_primary_consilium_completed(cycle_id):
            return WeeklySprintLifecycleState(
                current_weekly_sprint_id=None,
                weekly_sprint_number=None,
                is_weekly_consilium_completed=False,
                can_start_next_sprint=False,
                next_sprint_id=None,
                current_calendar_week=None,
                current_calendar_day=None,
            )

        anchor_dt = self.find_schedule_anchor_timestamp(cycle_id)
        if anchor_dt is None:
            # Anchor not established yet -> schedule cannot start.
            # Do NOT guess start date.
            return WeeklySprintLifecycleState(
                current_weekly_sprint_id=None,
                weekly_sprint_number=None,
                is_weekly_consilium_completed=False,
                can_start_next_sprint=False,
                next_sprint_id=None,
                current_calendar_week=None,
                current_calendar_day=None,
            )

        now_dt = self._now_utc()
        cal_week, cal_day, _ = self.get_authoritative_calendar_schedule(cycle_id, as_of=now_dt)
        existing_sprints = self.sprint_repo.list_by_cycle(cycle_id)

        # Inspect completed sprints with valid recovered reports
        completed_numbers = set()
        for s in existing_sprints:
            if s.status == "completed":
                rec = self.recover_completed_response(cycle_id, sprint_id=s.sprint_id)
                if rec is not None:
                    completed_numbers.add(s.sprint_number)

        # Next sprint in sequence
        next_num = 1
        while next_num in completed_numbers:
            next_num += 1

        # Fixed 168-hour schedule from initial anchor_dt:
        # Period N boundary = anchor_dt + N * 168 hours
        target_boundary_dt = anchor_dt + timedelta(hours=WEEKLY_SPRINT_HOURS * next_num)
        has_boundary_elapsed = (now_dt >= target_boundary_dt)
        target_next_sprint_id = f"sprint_{cycle_id}_w{next_num}"[:64]

        active_sprint = self.sprint_repo.get_active_for_cycle(cycle_id)

        # Determine effective next sprint ID: if an active sprint for next_num exists, use it
        if active_sprint is not None and active_sprint.sprint_number == next_num:
            effective_next_sprint_id = active_sprint.sprint_id
        else:
            effective_next_sprint_id = target_next_sprint_id

        # Case 1: No completed sprints yet (Week 1)
        if not completed_numbers:
            return WeeklySprintLifecycleState(
                current_weekly_sprint_id=active_sprint.sprint_id if active_sprint else None,
                weekly_sprint_number=active_sprint.sprint_number if active_sprint else None,
                is_weekly_consilium_completed=False,
                can_start_next_sprint=has_boundary_elapsed,
                next_sprint_id=effective_next_sprint_id if has_boundary_elapsed else None,
                current_calendar_week=cal_week,
                current_calendar_day=cal_day,
            )

        # Case 2: One or more sprints have completed (Week 2+)
        if active_sprint is not None and active_sprint.sprint_number == next_num:
            return WeeklySprintLifecycleState(
                current_weekly_sprint_id=active_sprint.sprint_id,
                weekly_sprint_number=active_sprint.sprint_number,
                is_weekly_consilium_completed=False,
                can_start_next_sprint=has_boundary_elapsed,
                next_sprint_id=active_sprint.sprint_id if has_boundary_elapsed else None,
                current_calendar_week=cal_week,
                current_calendar_day=cal_day,
            )

        latest_completed_num = max(completed_numbers)
        latest_completed_id = f"sprint_{cycle_id}_w{latest_completed_num}"[:64]
        for s in existing_sprints:
            if s.sprint_number == latest_completed_num:
                latest_completed_id = s.sprint_id
                break

        return WeeklySprintLifecycleState(
            current_weekly_sprint_id=latest_completed_id,
            weekly_sprint_number=latest_completed_num,
            is_weekly_consilium_completed=True,
            can_start_next_sprint=has_boundary_elapsed,
            next_sprint_id=effective_next_sprint_id if has_boundary_elapsed else None,
            current_calendar_week=cal_week,
            current_calendar_day=cal_day,
        )

    # ==========================================================================
    # 1. ATHENA 7-DAY CLINICAL DIARY & CHAT HISTORY (SERVER-ONLY)
    # ==========================================================================

    def resolve_active_sprint_id(
        self,
        cycle_id: str,
        as_of: Optional[datetime] = None,
        commit: bool = True,
        create_if_missing: bool = True,
    ) -> str:
        """
        Calculates and returns the authoritative active weekly sprint ID for cycle_id.
        - Uses confirmed Athena schedule anchor (T0) if present:
          N = max(1, int((as_of - anchor_dt).total_seconds() / 3600.0 // 168) + 1)
          sprint_id = f"sprint_{cycle_id}_w{N}"[:64]
        - If anchor not present:
          Falls back to active sprint in sprint_repo or f"sprint_{cycle_id}_w1"[:64]
        - Guarantees sprint row exists in MedicalSprintRepository with status='active'
          when create_if_missing=True.
        """
        if not self.cycle_repo.get(cycle_id):
            raise ConsiliumCycleNotFoundError(f"Medical cycle '{cycle_id}' not found.")

        anchor_dt = self.find_schedule_anchor_timestamp(cycle_id)
        effective_dt = as_of or self._now_utc()
        if effective_dt.tzinfo is None:
            effective_dt = effective_dt.replace(tzinfo=timezone.utc)
        else:
            effective_dt = effective_dt.astimezone(timezone.utc)

        if anchor_dt is not None:
            if effective_dt < anchor_dt:
                sprint_number = 1
            else:
                hours_elapsed = (effective_dt - anchor_dt).total_seconds() / 3600.0
                sprint_number = max(1, int(hours_elapsed // WEEKLY_SPRINT_HOURS) + 1)
            target_sprint_id = f"sprint_{cycle_id}_w{sprint_number}"[:64]
        else:
            active_sprint = self.sprint_repo.get_active_for_cycle(cycle_id)
            if active_sprint is not None:
                return active_sprint.sprint_id
            sprint_number = 1
            target_sprint_id = f"sprint_{cycle_id}_w1"[:64]

        existing = self.sprint_repo.get(target_sprint_id)
        if existing is None:
            if create_if_missing:
                self.sprint_repo.create(
                    sprint_id=target_sprint_id,
                    cycle_id=cycle_id,
                    sprint_number=sprint_number,
                    status="active",
                    commit=commit,
                )
        elif existing.cycle_id != cycle_id:
            raise ConsiliumSprintConflictError(
                f"Sprint '{target_sprint_id}' belongs to cycle '{existing.cycle_id}', not '{cycle_id}'."
            )
        return target_sprint_id

    def _resolve_or_create_sprint_id(
        self,
        cycle_id: str,
        sprint_id: Optional[str] = None,
        as_of: Optional[datetime] = None,
        commit: bool = True,
    ) -> Optional[str]:
        if sprint_id is not None and sprint_id.strip():
            clean_sprint = sprint_id.strip()
            existing_sprint = self.sprint_repo.get(clean_sprint)
            if existing_sprint is None:
                next_num = len(self.sprint_repo.list_by_cycle(cycle_id)) + 1
                self.sprint_repo.create(
                    sprint_id=clean_sprint,
                    cycle_id=cycle_id,
                    sprint_number=next_num,
                    status="active",
                    commit=commit,
                )
            elif existing_sprint.cycle_id != cycle_id:
                raise ConsiliumSprintConflictError(
                    f"Sprint '{clean_sprint}' belongs to cycle '{existing_sprint.cycle_id}', not '{cycle_id}'."
                )
            return clean_sprint

        return self.resolve_active_sprint_id(cycle_id, as_of=as_of, commit=commit)

    def save_athena_clinical_log(
        self,
        cycle_id: str,
        log_entry: AthenaClinicalLogEntry | Dict[str, Any],
        sprint_id: Optional[str] = None,
        event_type: Optional[str] = None,
        as_of: Optional[datetime] = None,
        commit: bool = True,
    ) -> ModelReport:
        """
        Stores a hidden [CLINICAL_REPORT] entry from Athena as a server_only report
        tagged with logical role AegisAgentRole.ATHENA_CURATOR and linked to cycle_id and sprint_id.
        """
        if not self.cycle_repo.get(cycle_id):
            raise ConsiliumCycleNotFoundError(f"Medical cycle '{cycle_id}' not found.")

        validated = (
            log_entry
            if isinstance(log_entry, AthenaClinicalLogEntry)
            else AthenaClinicalLogEntry.model_validate(log_entry)
        )

        log_ts = self._parse_iso_datetime_utc(validated.timestamp) or as_of
        resolved_sprint_id = self._resolve_or_create_sprint_id(
            cycle_id, sprint_id, as_of=log_ts, commit=commit
        )

        payload = validated.model_dump()
        if not payload.get("timestamp"):
            payload["timestamp"] = (log_ts or self._now_utc()).isoformat()
        payload["agent_role"] = AegisAgentRole.ATHENA_CURATOR.value
        payload["sprint_id"] = resolved_sprint_id
        if event_type:
            payload["event_type"] = event_type

        expected_ver: Optional[int] = None
        if not commit:
            latest_ver = self.report_repo.get_latest_version_number(
                cycle_id=cycle_id,
                report_type=ATHENA_CLINICAL_LOG_REPORT_TYPE,
                privacy_level=PrivacyLevel.SERVER_ONLY,
            )
            expected_ver = latest_ver + 1

        return self.report_repo.create_next_version(
            cycle_id=cycle_id,
            sprint_id=resolved_sprint_id,
            model=self.gateway.flash_model,
            report_type=ATHENA_CLINICAL_LOG_REPORT_TYPE,
            privacy_level=PrivacyLevel.SERVER_ONLY,
            content=payload,
            expected_version=expected_ver,
            commit=commit,
        )

    @staticmethod
    def _parse_iso_datetime_utc(raw_value: Optional[str | datetime]) -> Optional[datetime]:
        if raw_value is None:
            return None
        if isinstance(raw_value, datetime):
            if raw_value.tzinfo is None:
                return raw_value.replace(tzinfo=timezone.utc)
            return raw_value.astimezone(timezone.utc)
        if not isinstance(raw_value, str):
            return None
        text = raw_value.strip()
        if not text:
            return None
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            if len(text) == 10 and text[4] == "-" and text[7] == "-":
                parsed_date = datetime.strptime(text, "%Y-%m-%d").date()
                return datetime(
                    parsed_date.year,
                    parsed_date.month,
                    parsed_date.day,
                    0,
                    0,
                    0,
                    tzinfo=timezone.utc,
                )
            dt = datetime.fromisoformat(text)
            if dt.tzinfo is None:
                return dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except ValueError:
            return None

    def _resolve_seven_day_window(
        self, reference_date: Optional[str | datetime] = None
    ) -> tuple[datetime, datetime]:
        if isinstance(reference_date, str) and len(reference_date.strip()) == 10:
            parsed_ref = self._parse_iso_datetime_utc(reference_date.strip())
            if parsed_ref is not None:
                window_end = parsed_ref.replace(
                    hour=23, minute=59, second=59, microsecond=999999
                )
                start_date = (parsed_ref - timedelta(days=7)).date()
                window_start = datetime(
                    start_date.year,
                    start_date.month,
                    start_date.day,
                    0,
                    0,
                    0,
                    tzinfo=timezone.utc,
                )
                return window_start, window_end

        ref_dt = self._parse_iso_datetime_utc(reference_date) or self._now_utc()
        window_end = ref_dt
        start_date = (ref_dt - timedelta(days=7)).date()
        window_start = datetime(
            start_date.year,
            start_date.month,
            start_date.day,
            0,
            0,
            0,
            tzinfo=timezone.utc,
        )
        return window_start, window_end

    def resolve_sprint_observation_window(
        self,
        cycle_id: str,
        sprint_number: Optional[int] = None,
        sprint_id: Optional[str] = None,
        reference_date: Optional[str | datetime] = None,
    ) -> Tuple[datetime, datetime]:
        """
        Calculates the authoritative observation time window [window_start, window_end) for a weekly sprint:
        - When athena_schedule_anchor (T0) exists:
            Sprint 1: [T0 - 10 min, T0 + 168 hours)
            Sprint N (N > 1): [T0 + 168 * (N - 1) hours, T0 + 168 * N hours)
          Guarantees fixed 168-hour observation periods derived from T0, completely decoupled from
          the execution timestamp (distinction between execution date and observation period).
        - When anchor does not exist: falls back to 7-day window ending at reference_date or self._now_utc().
        """
        anchor_dt = self.find_schedule_anchor_timestamp(cycle_id)
        if anchor_dt is not None:
            target_n: Optional[int] = sprint_number
            if target_n is None and sprint_id is not None:
                sprint_row = self.sprint_repo.get(sprint_id)
                if sprint_row is not None and sprint_row.sprint_number:
                    target_n = sprint_row.sprint_number
                else:
                    m = re.search(r'[_-]w(\d+)$', sprint_id)
                    if m:
                        target_n = int(m.group(1))

            if target_n is None:
                if reference_date is not None:
                    ref_dt = self._parse_iso_datetime_utc(reference_date)
                    if ref_dt is not None:
                        h = (ref_dt - anchor_dt).total_seconds() / 3600.0
                        target_n = max(1, int(h // WEEKLY_SPRINT_HOURS) + 1)
                if target_n is None:
                    now_dt = self._now_utc()
                    h = (now_dt - anchor_dt).total_seconds() / 3600.0
                    target_n = max(1, int(h // WEEKLY_SPRINT_HOURS) + 1)

            if target_n == 1:
                window_start = anchor_dt - timedelta(minutes=10)
                window_end = anchor_dt + timedelta(hours=WEEKLY_SPRINT_HOURS)
            else:
                window_start = anchor_dt + timedelta(hours=WEEKLY_SPRINT_HOURS * (target_n - 1))
                window_end = anchor_dt + timedelta(hours=WEEKLY_SPRINT_HOURS * target_n)

            return window_start, window_end

        return self._resolve_seven_day_window(reference_date)

    def get_seven_day_athena_logs(
        self,
        cycle_id: str,
        sprint_id: Optional[str] = None,
        reference_date: Optional[str | datetime] = None,
        sprint_number: Optional[int] = None,
    ) -> List[AthenaClinicalLogEntry]:
        """
        Returns Athena's 7-day clinical diary entries for the given cycle and sprint
        in chronological order (up to the 7 daily logs of the current 7-day window):
        - When athena_schedule_anchor exists, calculates exact fixed 168-hour window
          from anchor_dt (T0), ensuring late execution does not mix adjacent weeks.
        - Strictly filters by cycle_id, never mixing other patients or cycles.
        - Fallback to _resolve_seven_day_window if anchor_dt is not present.
        """
        anchor_dt = self.find_schedule_anchor_timestamp(cycle_id)
        versions = self.report_repo.list_versions(
            cycle_id=cycle_id,
            report_type=ATHENA_CLINICAL_LOG_REPORT_TYPE,
            privacy_level=PrivacyLevel.SERVER_ONLY,
        )

        if anchor_dt is not None:
            # 1. Authoritative 168-hour fixed grid schedule based on anchor_dt (T0)
            target_n: Optional[int] = sprint_number
            if target_n is None and sprint_id is not None:
                sprint_row = self.sprint_repo.get(sprint_id)
                if sprint_row is not None and sprint_row.sprint_number:
                    target_n = sprint_row.sprint_number
                else:
                    m = re.search(r'[_-]w(\d+)$', sprint_id)
                    if m:
                        target_n = int(m.group(1))

            if target_n is None:
                if reference_date is not None:
                    ref_dt = self._parse_iso_datetime_utc(reference_date)
                    if ref_dt is not None:
                        h = (ref_dt - anchor_dt).total_seconds() / 3600.0
                        target_n = max(1, int(h // WEEKLY_SPRINT_HOURS) + 1)
                if target_n is None:
                    now_dt = self._now_utc()
                    h = (now_dt - anchor_dt).total_seconds() / 3600.0
                    target_n = max(1, int(h // WEEKLY_SPRINT_HOURS) + 1)

            # Fixed period boundaries:
            # Sprint 1: [anchor_dt - 10 min, anchor_dt + 168 hours)
            # Sprint N (N > 1): [anchor_dt + 168 * (N - 1) hours, anchor_dt + 168 * N hours)
            if target_n == 1:
                window_start = anchor_dt - timedelta(minutes=10)
                window_end = anchor_dt + timedelta(hours=WEEKLY_SPRINT_HOURS)
            else:
                window_start = anchor_dt + timedelta(hours=WEEKLY_SPRINT_HOURS * (target_n - 1))
                window_end = anchor_dt + timedelta(hours=WEEKLY_SPRINT_HOURS * target_n)

            filtered: List[AthenaClinicalLogEntry] = []
            for r in versions:
                # Cycle isolation guard
                if r.cycle_id != cycle_id:
                    continue

                content = dict(r.content or {})
                content.pop("agent_role", None)
                content.pop("sprint_id", None)
                entry = AthenaClinicalLogEntry.model_validate(content)

                log_dt = self._parse_iso_datetime_utc(entry.timestamp) or self._parse_iso_datetime_utc(
                    r.created_at
                )
                if log_dt is None:
                    continue

                # Strict fixed time window check: prevents mixing adjacent weeks even on late execution
                if log_dt < window_start or log_dt >= window_end:
                    continue

                # Sprint identity check:
                # Disallow logs explicitly bound to a DIFFERENT cycle
                if r.sprint_id is not None:
                    sprint_meta = self.sprint_repo.get(r.sprint_id)
                    if sprint_meta is not None and sprint_meta.cycle_id != cycle_id:
                        continue

                # Acceptable sprint_id matching for target week target_n:
                # - None (unattributed Week 1 logs)
                # - canonical "sprint_{cycle_id}_w{target_n}"
                # - legacy strategy sprint attribution "sprint_{cycle_id}_w{target_n - 1}"
                # - sprint_id if explicitly specified
                expected_ids = {
                    None,
                    f"sprint_{cycle_id}_w{target_n}",
                    f"sprint_{cycle_id}_w{target_n - 1}",
                }
                if sprint_id is not None:
                    expected_ids.add(sprint_id)

                if r.sprint_id not in expected_ids:
                    continue

                filtered.append(entry)

            filtered.sort(
                key=lambda item: self._parse_iso_datetime_utc(item.timestamp)
                or datetime.min.replace(tzinfo=timezone.utc)
            )
            return filtered[-7:]

        else:
            # Fallback path when schedule anchor is not yet recorded (e.g. legacy tests)
            window_start, window_end = self._resolve_seven_day_window(reference_date)
            filtered = []
            for r in versions:
                if r.cycle_id != cycle_id:
                    continue
                if sprint_id is not None and r.sprint_id is not None and r.sprint_id != sprint_id:
                    continue
                content = dict(r.content or {})
                content.pop("agent_role", None)
                content.pop("sprint_id", None)
                entry = AthenaClinicalLogEntry.model_validate(content)

                log_dt = self._parse_iso_datetime_utc(entry.timestamp) or self._parse_iso_datetime_utc(
                    r.created_at
                )
                if log_dt is None:
                    continue
                if log_dt > window_end or log_dt < window_start:
                    continue

                filtered.append(entry)

            filtered.sort(
                key=lambda item: self._parse_iso_datetime_utc(item.timestamp)
                or datetime.min.replace(tzinfo=timezone.utc)
            )
            return filtered[-7:]

    @staticmethod
    def format_athena_diary_for_consilium(logs: List[AthenaClinicalLogEntry]) -> str:
        """
        Formats Athena's 7-day clinical diary using the exact format from
        VaultService.getAthenaLogsSummaryForPro() in lib/services/vault_service.dart.
        """
        if not logs:
            return "Athena's hidden clinical logs are missing."

        lines = ["=== CURATOR CLINICAL OBSERVATION DIARY (ATHENA LOGS) ==="]
        for log in logs:
            symptoms_str = ", ".join(log.symptoms_mentioned) if log.symptoms_mentioned else "None"
            lines.append(f"⏱ Timestamp: {log.timestamp or 'Unknown'}")
            lines.append(f"📊 Status: {log.status}")
            lines.append(f"🚩 Symptoms: {symptoms_str}")
            lines.append(f"⚠️ Red Flags: {str(log.red_flags).lower()}")
            lines.append(f"📝 Summary for Chief Physician: {log.summary_for_master}")
            lines.append("--------------------------------------------------")
        return "\n".join(lines)

    def _get_athena_chat_history_with_version(
        self, cycle_id: str
    ) -> Tuple[List[Dict[str, str]], int]:
        reports = self.report_repo.list_server_only(
            cycle_id=cycle_id,
            report_type=ATHENA_CHAT_HISTORY_REPORT_TYPE,
        )
        if not reports:
            return [], 0
        latest_report = reports[0]
        latest_version = int(latest_report.version or 0)
        latest = latest_report.content or {}
        messages = latest.get("messages")
        if not isinstance(messages, list):
            return [], latest_version
        sanitized = [
            {
                "sender": str(m.get("sender", "curator")),
                "text": str(m.get("text", "")),
                "timestamp": str(m.get("timestamp", "")),
            }
            for m in messages
            if isinstance(m, dict) and str(m.get("text", "")).strip()
        ]
        return sanitized, latest_version

    def save_athena_chat_message(
        self,
        cycle_id: str,
        sender: str,
        text: str,
        sprint_id: Optional[str] = None,
        timestamp: Optional[str] = None,
        commit: bool = True,
        max_retries: int = 8,
    ) -> ModelReport:
        """
        Appends a chat turn ('user' or 'curator') to the server-side chat history for cycle_id
        so that strategy updates across weekly sprints never lose conversation history.
        Prevents lost updates under cross-process concurrency by binding each append to
        expected_version = latest_version + 1 and re-reading the latest messages on retry.
        """
        if not self.cycle_repo.get(cycle_id):
            raise ConsiliumCycleNotFoundError(f"Medical cycle '{cycle_id}' not found.")

        sender_norm = sender.strip().lower()
        if sender_norm not in ("user", "curator", "model"):
            raise ValueError("sender must be 'user', 'curator', or 'model'.")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("Chat message text cannot be empty.")

        msg_ts = self._parse_iso_datetime_utc(timestamp)
        effective_timestamp = timestamp or self._now_utc().isoformat()
        clean_text = text.strip()
        effective_sender = "user" if sender_norm == "user" else "curator"

        effective_retries = max(0, int(max_retries)) if commit else 0

        for attempt in range(effective_retries + 1):
            if attempt > 0:
                self.db.rollback()
                self.db.expire_all()
                time.sleep(0.015 * attempt)
                bind = self.db.get_bind()
                if bind is not None and bind.dialect.name == "sqlite":
                    self.db.execute(sql_text("BEGIN IMMEDIATE"))
            elif commit and not self.db.new and not self.db.dirty and not self.db.deleted:
                self.db.expire_all()

            try:
                resolved_sprint_id = self._resolve_or_create_sprint_id(
                    cycle_id, sprint_id, as_of=msg_ts, commit=commit
                )
                existing_messages, latest_version = self._get_athena_chat_history_with_version(
                    cycle_id
                )
                expected_version = latest_version + 1
                updated_messages = list(existing_messages)
                updated_messages.append(
                    {
                        "sender": effective_sender,
                        "text": clean_text,
                        "timestamp": effective_timestamp,
                    }
                )

                return self.report_repo.create_next_version(
                    cycle_id=cycle_id,
                    sprint_id=resolved_sprint_id,
                    model=self.gateway.flash_model,
                    report_type=ATHENA_CHAT_HISTORY_REPORT_TYPE,
                    privacy_level=PrivacyLevel.SERVER_ONLY,
                    content={
                        "agent_role": AegisAgentRole.ATHENA_CURATOR.value,
                        "messages": updated_messages,
                    },
                    expected_version=expected_version,
                    max_retries=0,
                    commit=commit,
                )
            except (ReportVersionConflictError, IntegrityError, OperationalError) as exc:
                if commit:
                    self.db.rollback()
                if attempt < effective_retries:
                    continue
                if isinstance(exc, ReportVersionConflictError):
                    raise
                raise ReportVersionConflictError(
                    f"Concurrent modification conflict while appending Athena chat message for cycle '{cycle_id}': {exc}"
                ) from exc
            except BaseException:
                if commit:
                    self.db.rollback()
                raise

        raise ReportVersionConflictError(
            f"Exhausted retries ({effective_retries}) appending Athena chat message for cycle '{cycle_id}'."
        )

    def get_athena_chat_history(self, cycle_id: str) -> List[Dict[str, str]]:
        messages, _ = self._get_athena_chat_history_with_version(cycle_id)
        return messages

    # ==========================================================================
    # 2. SERVER-SIDE ACTIVE STRATEGY & SESSION REFRESH FOR ATHENA
    # ==========================================================================

    def get_active_athena_context(
        self, cycle_id: str, as_of: Optional[datetime] = None
    ) -> ActiveAthenaStrategyContext:
        """
        Returns the authoritative, latest active strategy and system prompt for Athena.
        - Checks versioned 'athena_active_strategy' reports first (created by Weekly Consilium).
        - Falls back to the Primary Consilium 'chief_physician_internal' report (v1) if no weekly
          sprint has completed yet.
        - Always embeds the latest strategy into Athena's system prompt while preserving chat_history.
        """
        if not self.cycle_repo.get(cycle_id):
            raise ConsiliumCycleNotFoundError(f"Medical cycle '{cycle_id}' not found.")

        strategy_reports = self.report_repo.list_server_only(
            cycle_id=cycle_id,
            report_type=ATHENA_ACTIVE_STRATEGY_REPORT_TYPE,
        )

        if strategy_reports:
            latest = strategy_reports[0]
            c = latest.content or {}
            active_strategy_text = str(c.get("active_strategy_text", "")).strip()
            clinical_background_text = str(
                c.get("clinical_background_text", "Deep correction of the Chief Physician's strategy based on weekly dynamics")
            ).strip()
            deputy_synthesis_text = str(c.get("deputy_synthesis_text", "")).strip()
            source_consilium = str(c.get("source_consilium", "weekly_consilium"))
            strategy_status = str(c.get("strategy_status", "updated"))
            strategy_version = int(c.get("strategy_version", latest.version))
            sprint_id = latest.sprint_id
        else:
            # Fallback to Primary Consilium (Stage 3.2) baseline strategy (v1)
            pro_primary = self.report_repo.list_server_only(
                cycle_id=cycle_id,
                report_type="chief_physician_internal",
            )
            flash_primary = self.report_repo.list_server_only(
                cycle_id=cycle_id,
                report_type="deputy_chief_physician_internal",
            )
            if not pro_primary or not flash_primary:
                raise ConsiliumContextMissingError(
                    f"No active strategy found for cycle '{cycle_id}': Primary Consilium has not been executed yet."
                )
            pro_c = pro_primary[0].content or {}
            flash_c = flash_primary[0].content or {}
            active_strategy_text = str(pro_c.get("pro_general_shelf", "")).strip()
            clinical_background_text = str(pro_c.get("pro_training_shelf", "")).strip()
            deputy_synthesis_text = str(flash_c.get("flash_general_shelf", "")).strip()
            source_consilium = "primary_consilium"
            strategy_status = "initial"
            strategy_version = 1
            sprint_id = pro_primary[0].sprint_id

        effective_dt = as_of or self._now_utc()
        if self.find_schedule_anchor_timestamp(cycle_id) is not None:
            active_sprint = self.resolve_active_sprint_id(
                cycle_id, as_of=effective_dt, create_if_missing=False
            )
            logs = self.get_seven_day_athena_logs(
                cycle_id=cycle_id, sprint_id=active_sprint, reference_date=effective_dt
            )
        else:
            logs = self.get_seven_day_athena_logs(
                cycle_id=cycle_id, sprint_id=sprint_id, reference_date=effective_dt
            )
        athena_logs_summary = self.format_athena_diary_for_consilium(logs)

        full_curator_context = "\n".join(
            [
                "=== GUIDELINES AND CLINICAL ROUTE FROM CHIEF PHYSICIAN (PRO) ===",
                active_strategy_text,
                "",
                "=== DEEP CLINICAL ANALYSIS AND SYNDROMES (PRO TRAINING) ===",
                clinical_background_text,
                "",
                "=== PRIMARY SYNTHESIS (FLASH) ===",
                deputy_synthesis_text,
                athena_logs_summary,
            ]
        )

        system_prompt = build_athena_system_prompt(full_curator_context)
        chat_history = self.get_athena_chat_history(cycle_id)

        return ActiveAthenaStrategyContext(
            cycle_id=cycle_id,
            sprint_id=sprint_id,
            strategy_version=strategy_version,
            source_consilium=source_consilium,
            strategy_status=strategy_status,
            active_strategy_text=active_strategy_text,
            clinical_background_text=clinical_background_text,
            deputy_synthesis_text=deputy_synthesis_text,
            athena_logs_summary=athena_logs_summary,
            daily_question_limit=3,
            system_prompt=system_prompt,
            chat_history=chat_history,
        )

    def sync_athena_session(
        self,
        cycle_id: str,
        cached_session: Optional[AthenaSessionState] = None,
    ) -> AthenaSessionState:
        """
        Ensures that any subsequent Athena turn uses the latest server-side strategy.
        Eliminates the CuratorAgent.initializeSession() stale-instruction defect by
        automatically forcing a system_prompt refresh whenever `strategy_version` changes
        (e.g. v1 -> v2 after Weekly Consilium), while preserving 100% of `chat_history`.
        """
        latest_ctx = self.get_active_athena_context(cycle_id)

        needs_reinit = (
            cached_session is None
            or cached_session.cycle_id != cycle_id
            or cached_session.applied_strategy_version != latest_ctx.strategy_version
            or cached_session.system_prompt != latest_ctx.system_prompt
        )

        # Merge/preserve any in-memory chat history with server-persisted chat history
        preserved_history = list(latest_ctx.chat_history)
        if cached_session is not None and len(cached_session.chat_history) > len(preserved_history):
            preserved_history = list(cached_session.chat_history)

        return AthenaSessionState(
            cycle_id=cycle_id,
            sprint_id=latest_ctx.sprint_id,
            applied_strategy_version=latest_ctx.strategy_version,
            system_prompt=latest_ctx.system_prompt,
            chat_history=preserved_history,
            was_reinitialized=needs_reinit,
        )

    # ==========================================================================
    # 3. INTERNAL CONTEXT LOADERS FOR WEEKLY CONSILIUM
    # ==========================================================================

    def _load_previous_consilium_context(self, cycle_id: str) -> Dict[str, Any]:
        """
        Loads the baseline Primary Consilium outputs and any prior Weekly Consilium
        outputs from server_only storage without re-running M1, Vision, or 8 Flash-Lite.
        """
        pro_primary_list = self.report_repo.list_server_only(
            cycle_id=cycle_id,
            report_type="chief_physician_internal",
        )
        flash_primary_list = self.report_repo.list_server_only(
            cycle_id=cycle_id,
            report_type="deputy_chief_physician_internal",
        )
        primary_overall_list = self.report_repo.list_client_facing(
            cycle_id=cycle_id,
            report_type="user_overall_report",
        )
        if not pro_primary_list or not flash_primary_list or not primary_overall_list:
            raise ConsiliumContextMissingError(
                f"Cannot run Weekly Consilium for cycle '{cycle_id}': Primary Consilium "
                "('user_overall_report', 'deputy_chief_physician_internal' and 'chief_physician_internal') "
                "has not been completed yet."
            )

        pro_latest = pro_primary_list[0].content or {}
        flash_latest = flash_primary_list[0].content or {}
        pro_initial = pro_primary_list[-1].content or {}
        flash_initial = flash_primary_list[-1].content or {}

        # Check if prior weekly sprints or active strategy versions exist
        strategy_reports = self.report_repo.list_server_only(
            cycle_id=cycle_id,
            report_type=ATHENA_ACTIVE_STRATEGY_REPORT_TYPE,
        )
        prior_weekly_flash = self.report_repo.list_server_only(
            cycle_id=cycle_id,
            report_type=WEEKLY_DEPUTY_INTERNAL_REPORT_TYPE,
        )
        prior_weekly_pro = self.report_repo.list_server_only(
            cycle_id=cycle_id,
            report_type=WEEKLY_CHIEF_INTERNAL_REPORT_TYPE,
        )

        if strategy_reports:
            latest_strat_c = strategy_reports[0].content or {}
            latest_pro_strategy = str(
                latest_strat_c.get("active_strategy_text")
                or pro_latest.get("pro_general_shelf", "")
            )
            latest_flash_general = str(
                latest_strat_c.get("deputy_synthesis_text")
                or flash_latest.get("flash_general_shelf", "")
            )
        else:
            latest_flash_general = (
                str(prior_weekly_flash[0].content.get("weekly_deputy_analysis", ""))
                if prior_weekly_flash
                else str(flash_latest.get("flash_general_shelf", ""))
            )
            latest_pro_strategy = (
                str(prior_weekly_pro[0].content.get("updated_weekly_strategy", ""))
                if prior_weekly_pro
                else str(pro_latest.get("pro_general_shelf", ""))
            )
        latest_flash_training = str(flash_latest.get("flash_training_shelf", ""))
        latest_pro_training = str(pro_latest.get("pro_training_shelf", ""))

        # Load stored 8 organ specialist conclusions from latest full consilium (Primary or Lab-Revision; read-only)
        organ_summaries: List[str] = []
        for system_key in SYSTEM_KEYS:
            role_val = AegisAgentRole.for_system_key(system_key).value
            sys_reports = self.report_repo.list_server_only(
                cycle_id=cycle_id,
                report_type=role_val,
            )
            if sys_reports:
                sys_c = sys_reports[0].content or {}
                gen_shelf = (sys_c.get("system_logic") or {}).get("system_general_shelf", "No data")
                train_shelf = (sys_c.get("training_sandbox") or {}).get("system_training_shelf", "No data")
                organ_summaries.append(
                    f"--- BLOCK: {system_key.upper()} ---\n"
                    f"Main conclusion: {gen_shelf}\n"
                    f"Training log: {train_shelf}\n"
                )

        # Load latest vitals measurements (strictly excluding historical data from live stream)
        measurements = self.measurement_repo.list_by_cycle(cycle_id=cycle_id, is_historical=False, is_simulated=False, limit=100)
        systolic = next((m.systolic for m in reversed(measurements) if m.systolic is not None), None)
        diastolic = next((m.diastolic for m in reversed(measurements) if m.diastolic is not None), None)
        pulse = next((m.pulse for m in reversed(measurements) if m.pulse is not None), None)
        spo2 = next((m.spo2 for m in reversed(measurements) if m.spo2 is not None), None)

        from app.services.ecg_context_service import assemble_consilium_ecg_context

        ecg_ctx = assemble_consilium_ecg_context(self.db, cycle_id)
        ecg_summary = ecg_ctx.ecg_summary

        anamnesis_reports = self.report_repo.list_server_only(
            cycle_id=cycle_id,
            report_type=PREPARATORY_ANAMNESIS_REPORT_TYPE,
        )
        anamnesis_summary = (
            (anamnesis_reports[0].content or {}).get("user_view", {})
            if anamnesis_reports
            else {}
        )

        # Determine the latest laboratory state and distinguish data evaluated by the
        # last Full Consilium (Primary or Lab-Revision) from new unreviewed lab changes.
        from app.services.lab_context_service import assemble_consilium_laboratory_context

        latest_lab_ctx = assemble_consilium_laboratory_context(self.db, cycle_id)

        from app.services.lab_context_service import get_last_evaluated_ledger_version

        last_evaluated_ver = get_last_evaluated_ledger_version(self.db, cycle_id)

        unreviewed_pending_labs: Dict[str, Any] = {}
        pending_removed_marker_keys: List[str] = []

        if latest_lab_ctx.ledger_version > last_evaluated_ver and last_evaluated_ver > 0:
            from app.services.lab_context_service import LaboratoryLedgerVersionNotFoundError

            eval_lab_ctx = None
            try:
                eval_lab_ctx = assemble_consilium_laboratory_context(
                    self.db, cycle_id, ledger_version=last_evaluated_ver
                )
            except LaboratoryLedgerVersionNotFoundError:
                eval_lab_ctx = None

            if eval_lab_ctx is not None:
                shared_labs_shelf = eval_lab_ctx.shared_labs_table

                # 1. Detect removed or invalidated markers (previously active in evaluated shelf, now missing)
                pending_removed_marker_keys = [
                    k for k in shared_labs_shelf if k not in latest_lab_ctx.shared_labs_table
                ]

                # 2. Collect signatures of all observations evaluated by previous consilium
                evaluated_obs_keys = {
                    obs.get("observation_id")
                    or (
                        obs.get("marker_key"),
                        str(obs.get("value")),
                        str(obs.get("sample_date")),
                        str(obs.get("recorded_at")),
                    )
                    for obs in eval_lab_ctx.laboratory_history
                }

                # 3. Detect new observations in history (new dated studies, corrections, etc.)
                new_observations = [
                    obs
                    for obs in latest_lab_ctx.laboratory_history
                    if (
                        obs.get("observation_id")
                        or (
                            obs.get("marker_key"),
                            str(obs.get("value")),
                            str(obs.get("sample_date")),
                            str(obs.get("recorded_at")),
                        )
                    )
                    not in evaluated_obs_keys
                ]
                new_obs_marker_keys = {
                    obs.get("marker_key") for obs in new_observations if obs.get("marker_key")
                }

                # 4. Form unreviewed_pending_labs:
                # - newly added active markers
                # - mutated active markers (value, unit, date, ref range, etc.)
                # - active markers that received new dated observations in history (even if active value is identical)
                for k, marker_val in latest_lab_ctx.shared_labs_table.items():
                    if (
                        k not in shared_labs_shelf
                        or shared_labs_shelf[k] != marker_val
                        or k in new_obs_marker_keys
                    ):
                        unreviewed_pending_labs[k] = marker_val

                # Since ledger_version > last_evaluated_ver, new lab data was committed to ledger
                has_unreviewed_lab_changes = True
            else:
                # Safe indeterminate fallback if requested evaluated ledger version is missing from database
                shared_labs_shelf = {}
                unreviewed_pending_labs = dict(latest_lab_ctx.shared_labs_table)
                pending_removed_marker_keys = []
                has_unreviewed_lab_changes = bool(
                    latest_lab_ctx.shared_labs_table or latest_lab_ctx.laboratory_history
                )
        elif last_evaluated_ver == 0 and latest_lab_ctx.ledger_version > 0:
            # Audit Remediation C-4:
            # Ledger exists, but has NEVER been evaluated by a full consilium (or Primary ran on fallback).
            # Safe indeterminate state: do not invent evaluated markers.
            shared_labs_shelf = {}
            unreviewed_pending_labs = dict(latest_lab_ctx.shared_labs_table)
            pending_removed_marker_keys = []
            has_unreviewed_lab_changes = bool(latest_lab_ctx.shared_labs_table or latest_lab_ctx.laboratory_history)
        else:
            shared_labs_shelf = latest_lab_ctx.shared_labs_table
            has_unreviewed_lab_changes = False

        # Full chronological observations history is always provided so no dated studies are lost
        laboratory_history = latest_lab_ctx.laboratory_history

        initial_context_lines = [
            "=== ANAMNESIS AND INPUT DATA ===",
            json.dumps(
                {
                    "cycle_id": cycle_id,
                    "anamnesis_user_view": anamnesis_summary,
                    "shared_labs_shelf": shared_labs_shelf,
                    "evaluated_ledger_version": last_evaluated_ver,
                    "latest_ledger_version": latest_lab_ctx.ledger_version,
                    "has_unreviewed_lab_changes": has_unreviewed_lab_changes,
                    "unreviewed_pending_labs": unreviewed_pending_labs,
                    "pending_removed_marker_keys": pending_removed_marker_keys,
                    "laboratory_history": laboratory_history,
                    "ecg_context": ecg_ctx.to_dict(),
                },
                ensure_ascii=False,
            ),
            "",
            "=== VITAL MARKERS (SMART WATCH / LIVE) ===",
            f"Blood pressure: {systolic if systolic is not None else '--'}/{diastolic if diastolic is not None else '--'} mmHg",
            f"Heart rate (Pulse): {pulse if pulse is not None else '--'} bpm",
            f"Oxygen saturation (SpO2): {spo2 if spo2 is not None else '--'}%",
            f"ECG status: {ecg_summary}",
            "",
            "=== CONCLUSIONS OF 8 ORGAN SYSTEM AGENTS ===",
            "\n".join(organ_summaries),
            "=== SYNTHESIS (GEMINI FLASH) ===",
            "Main conclusion:",
            latest_flash_general,
            "",
            "Training output:",
            latest_flash_training,
            "",
            "=== ACTIVE CHIEF PHYSICIAN STRATEGY (GEMINI PRO) ===",
            latest_pro_strategy,
            "",
            "=== CHIEF PHYSICIAN CLINICAL TRAINING SANDBOX ===",
            latest_pro_training,
        ]

        return {
            "initial_context": "\n".join(initial_context_lines),
            "latest_pro_strategy": latest_pro_strategy,
            "latest_pro_training": latest_pro_training,
            "primary_flash_general": str(flash_initial.get("flash_general_shelf", "")),
            "primary_pro_general": str(pro_initial.get("pro_general_shelf", "")),
            "primary_pro_training": str(pro_initial.get("pro_training_shelf", "")),
            "shared_labs_shelf": shared_labs_shelf,
            "unreviewed_pending_labs": unreviewed_pending_labs,
            "pending_removed_marker_keys": pending_removed_marker_keys,
            "has_unreviewed_lab_changes": has_unreviewed_lab_changes,
            "laboratory_history": laboratory_history,
            "evaluated_ledger_version": last_evaluated_ver,
            "latest_ledger_version": latest_lab_ctx.ledger_version,
        }

    # ==========================================================================
    # 4. WEEKLY CONSILIUM EXECUTION (STRICTLY FLASH -> PRO)
    # ==========================================================================

    async def run_weekly_consilium(
        self,
        cycle_id: str,
        sprint_id: Optional[str] = None,
        report_date: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> WeeklyConsiliumClientResponse:
        """
        Executes the server-side Weekly Consilium for `cycle_id`:
        1. Strictly calls ONLY Gemini Flash (Deputy Chief Physician) -> Gemini Pro (Chief Physician).
           Never calls the 8 Flash-Lite models, Anamnesis M1, or Vision OCR/ECG.
        2. Passes previous conclusions, active strategy, and the 7-day Athena diary to Flash and Pro.
        3. Validates and quality-checks Flash's 8 dated system updates and Pro's weekly overall report.
        4. Persists all server_only and client_facing reports, active Athena strategy version,
           execution log, and sprint completion inside a single atomic database transaction without closing the cycle.
        5. Protected by distributed cross-process SQLite lease, background heartbeat renewal, and fencing.
        6. Idempotent: retrying a completed operation or key immediately recovers results with zero AI calls.
        """
        cycle = self.cycle_repo.get(cycle_id)
        if not cycle:
            raise ConsiliumCycleNotFoundError(f"Medical cycle '{cycle_id}' not found.")
        if cycle.status != "active":
            raise ConsiliumError(
                f"Cannot run Weekly Consilium on non-active cycle '{cycle_id}' (status='{cycle.status}')."
            )

        # 1. Fast idempotency check (pre-lock)
        if idempotency_key and idempotency_key.strip():
            clean_idemp = idempotency_key.strip()
            existing_logs = self.report_repo.list_server_only(
                cycle_id=cycle_id,
                report_type=WEEKLY_CONSILIUM_EXECUTION_LOG_REPORT_TYPE,
            )
            for log in existing_logs:
                if (log.content or {}).get("idempotency_key") == clean_idemp:
                    recovered = self.recover_completed_response(
                        cycle_id, sprint_id=log.sprint_id
                    )
                    if recovered is not None:
                        return recovered

        # 2. Check if requested sprint_id is foreign or already completed,
        # or if retrying without sprint_id when the current week is already completed
        if sprint_id is not None and sprint_id.strip():
            existing_sprint = self.sprint_repo.get(sprint_id.strip())
            if existing_sprint is not None:
                if existing_sprint.cycle_id != cycle_id:
                    raise ConsiliumSprintConflictError(
                        f"Sprint '{sprint_id.strip()}' belongs to cycle '{existing_sprint.cycle_id}', not '{cycle_id}'."
                    )
                if existing_sprint.status == "completed":
                    recovered = self.recover_completed_response(
                        cycle_id, sprint_id=sprint_id.strip()
                    )
                    if recovered is not None:
                        return recovered
        else:
            active_sprint = self.sprint_repo.get_active_for_cycle(cycle_id)
            if active_sprint is None:
                existing_sprints = self.sprint_repo.list_by_cycle(cycle_id)
                if existing_sprints:
                    latest_sprint = existing_sprints[-1]
                    if latest_sprint.status == "completed":
                        recovered = self.recover_completed_response(
                            cycle_id, sprint_id=latest_sprint.sprint_id
                        )
                        if recovered is not None:
                            return recovered

        resolved_date = (
            report_date.strip()
            if isinstance(report_date, str) and report_date.strip()
            else self._now_utc().strftime("%Y-%m-%d")
        )

        async with self._get_weekly_lock(cycle_id):
            # 3. Double-checked locking idempotency
            if idempotency_key and idempotency_key.strip():
                clean_idemp = idempotency_key.strip()
                existing_logs = self.report_repo.list_server_only(
                    cycle_id=cycle_id,
                    report_type=WEEKLY_CONSILIUM_EXECUTION_LOG_REPORT_TYPE,
                )
                for log in existing_logs:
                    if (log.content or {}).get("idempotency_key") == clean_idemp:
                        recovered = self.recover_completed_response(
                            cycle_id, sprint_id=log.sprint_id
                        )
                        if recovered is not None:
                            return recovered

            if sprint_id is not None and sprint_id.strip():
                existing_sprint = self.sprint_repo.get(sprint_id.strip())
                if existing_sprint is not None:
                    if existing_sprint.cycle_id != cycle_id:
                        raise ConsiliumSprintConflictError(
                            f"Sprint '{sprint_id.strip()}' belongs to cycle '{existing_sprint.cycle_id}', not '{cycle_id}'."
                        )
                    if existing_sprint.status == "completed":
                        recovered = self.recover_completed_response(
                            cycle_id, sprint_id=sprint_id.strip()
                        )
                        if recovered is not None:
                            return recovered
            else:
                active_sprint = self.sprint_repo.get_active_for_cycle(cycle_id)
                if active_sprint is None:
                    existing_sprints = self.sprint_repo.list_by_cycle(cycle_id)
                    if existing_sprints:
                        latest_sprint = existing_sprints[-1]
                        if latest_sprint.status == "completed":
                            recovered = self.recover_completed_response(
                                cycle_id, sprint_id=latest_sprint.sprint_id
                            )
                            if recovered is not None:
                                return recovered

            # 4. Acquire cross-process SQLite lease with fencing token
            lease_owner_id = self._acquire_cross_process_lease(
                cycle_id, idempotency_key=idempotency_key
            )

            try:
                # Resolve target sprint metadata before DB writes
                existing_sprints = self.sprint_repo.list_by_cycle(cycle_id)
                if sprint_id is not None:
                    target_sprint = self.sprint_repo.get(sprint_id)
                    if target_sprint is not None and target_sprint.cycle_id != cycle_id:
                        raise ConsiliumSprintConflictError(
                            f"Sprint '{sprint_id}' belongs to cycle '{target_sprint.cycle_id}', not '{cycle_id}'."
                        )
                    resolved_sprint_id = sprint_id
                    if target_sprint is not None and target_sprint.sprint_number is not None:
                        resolved_sprint_number = target_sprint.sprint_number
                    else:
                        m = re.search(r'[_-]w(\d+)$', sprint_id)
                        if m:
                            resolved_sprint_number = int(m.group(1))
                        else:
                            resolved_sprint_number = len(existing_sprints) + 1
                else:
                    anchor_dt = self.find_schedule_anchor_timestamp(cycle_id)
                    if anchor_dt is not None:
                        completed_numbers = {
                            s.sprint_number for s in existing_sprints
                            if s.status == "completed" and self.recover_completed_response(cycle_id, sprint_id=s.sprint_id) is not None
                        }
                        next_num = 1
                        while next_num in completed_numbers:
                            next_num += 1
                        resolved_sprint_number = next_num
                        resolved_sprint_id = f"sprint_{cycle_id}_w{resolved_sprint_number}"[:64]
                    else:
                        active_sprint = self.sprint_repo.get_active_for_cycle(cycle_id)
                        if active_sprint is not None:
                            resolved_sprint_id = active_sprint.sprint_id
                            resolved_sprint_number = active_sprint.sprint_number
                        else:
                            resolved_sprint_number = len(existing_sprints) + 1
                            resolved_sprint_id = f"sprint_{cycle_id}_w{resolved_sprint_number}"[:64]

                # 5. Load previous conclusions, active strategy, and 7-day Athena diary
                prev_ctx = self._load_previous_consilium_context(cycle_id)
                window_start, window_end = self.resolve_sprint_observation_window(
                    cycle_id=cycle_id,
                    sprint_number=resolved_sprint_number,
                    sprint_id=resolved_sprint_id,
                    reference_date=resolved_date,
                )
                from app.services.athena_daily_journal_service import AthenaDailyJournalService
                journal_service = AthenaDailyJournalService(
                    db=self.db, now_provider=self.now_provider
                )
                journals_diary = journal_service.format_seven_day_journals_for_consilium(
                    cycle_id=cycle_id, sprint_number=resolved_sprint_number
                )
                if journals_diary:
                    athena_diary_str = journals_diary
                else:
                    seven_day_logs = self.get_seven_day_athena_logs(
                        cycle_id=cycle_id,
                        sprint_id=resolved_sprint_id,
                        reference_date=resolved_date,
                        sprint_number=resolved_sprint_number,
                    )
                    athena_diary_str = self.format_athena_diary_for_consilium(seven_day_logs)

                # 6. STAGE 1: Execute Weekly Deputy Chief Physician (Gemini Flash) with lease renewal
                flash_prompt = build_weekly_flash_prompt(
                    initial_context=prev_ctx["initial_context"],
                    athena_diary=athena_diary_str,
                )
                flash_output: WeeklyFlashMasterOutput = await self._execute_with_lease_renewal(
                    cycle_id,
                    lease_owner_id,
                    lambda: self.gateway.generate_structured(
                        prompt=flash_prompt,
                        response_schema=WeeklyFlashMasterOutput,
                        model=GeminiModelRole.FLASH,
                    ),
                )

                # 7. STAGE 2: Execute Weekly Chief Physician (Gemini Pro) with lease renewal
                previous_strategy_context = (
                    f"Current Active Strategy:\n{prev_ctx['latest_pro_strategy']}\n\n"
                    f"Clinical Background:\n{prev_ctx['latest_pro_training']}\n\n"
                    f"Baseline & Current Input Context:\n{prev_ctx['initial_context']}"
                )
                draft_weekly_reports_dict = flash_output.user_system_reports.to_dict()
                pro_prompt = build_weekly_pro_prompt(
                    flash_analysis=flash_output.weekly_deputy_analysis,
                    athena_diary=athena_diary_str,
                    previous_strategy_context=previous_strategy_context,
                    draft_weekly_system_reports_json=json.dumps(
                        draft_weekly_reports_dict, ensure_ascii=False
                    ),
                )
                pro_output: WeeklyProMasterOutput = await self._execute_with_lease_renewal(
                    cycle_id,
                    lease_owner_id,
                    lambda: self.gateway.generate_structured(
                        prompt=pro_prompt,
                        response_schema=WeeklyProMasterOutput,
                        model=GeminiModelRole.PRO,
                    ),
                )

                # 8. Chief Physician Quality Gate (BEFORE any database writes)
                if not pro_output.flash_quality_review.approved_for_publication:
                    reason = (
                        pro_output.flash_quality_review.rejection_reason
                        or "Chief Physician (Pro) rejected publication of weekly user-facing reports."
                    )
                    raise ConsiliumQualityRejectionError(reason)

                # Apply Pro's deterministic corrections over Flash's 8 weekly system updates
                final_system_updates: Dict[str, Dict[str, Any]] = {
                    k: dict(v) for k, v in draft_weekly_reports_dict.items()
                }
                for corrected_key, corrected_text in (
                    pro_output.flash_quality_review.corrected_user_system_reports.items()
                ):
                    if corrected_key not in SYSTEM_KEYS:
                        raise ConsiliumQualityRejectionError(
                            f"Invalid system key '{corrected_key}' in Pro weekly quality review."
                        )
                    validated_text = validate_no_guaranteed_unchanged_claim(
                        corrected_text,
                        f"Corrected weekly summary for '{corrected_key}'",
                    )
                    final_system_updates[corrected_key]["summary"] = validated_text

                # Ensure all 8 systems are present and non-empty
                if set(final_system_updates.keys()) != set(SYSTEM_KEYS) or any(
                    not isinstance(final_system_updates.get(k, {}).get("summary"), str)
                    or not final_system_updates[k]["summary"].strip()
                    for k in SYSTEM_KEYS
                ):
                    raise ConsiliumQualityRejectionError(
                        "Incomplete 8-system weekly updates after quality review; aborting publication."
                    )

                # 9. Atomic single-transaction persistence under fencing check
                try:
                    # Fencing check & atomic removal of execution lease in uncommitted transaction
                    self._verify_and_delete_cross_process_lease_uncommitted(
                        cycle_id, lease_owner_id
                    )

                    # Ensure sprint row exists and mark it completed without closing the parent cycle
                    if self.sprint_repo.get(resolved_sprint_id) is None:
                        self.sprint_repo.create(
                            sprint_id=resolved_sprint_id,
                            cycle_id=cycle_id,
                            sprint_number=resolved_sprint_number,
                            status="active",
                            commit=False,
                        )
                    self.sprint_repo.complete_sprint(
                        sprint_id=resolved_sprint_id,
                        commit=False,
                    )

                    # Seed v1 of athena_active_strategy from Primary Consilium if not yet seeded
                    if (
                        self.report_repo.get_latest_version_number(
                            cycle_id=cycle_id,
                            report_type=ATHENA_ACTIVE_STRATEGY_REPORT_TYPE,
                            privacy_level=PrivacyLevel.SERVER_ONLY,
                        )
                        == 0
                    ):
                        self.report_repo.create_next_version(
                            cycle_id=cycle_id,
                            sprint_id=None,
                            model=self.gateway.pro_model,
                            report_type=ATHENA_ACTIVE_STRATEGY_REPORT_TYPE,
                            privacy_level=PrivacyLevel.SERVER_ONLY,
                            content={
                                "agent_role": AegisAgentRole.CHIEF_PHYSICIAN.value,
                                "source_consilium": "primary_consilium",
                                "strategy_status": "initial",
                                "strategy_version": 1,
                                "active_strategy_text": prev_ctx["primary_pro_general"],
                                "clinical_background_text": prev_ctx["primary_pro_training"],
                                "deputy_synthesis_text": prev_ctx["primary_flash_general"],
                                "daily_question_limit": 3,
                            },
                            commit=False,
                        )

                    # Save server_only Weekly Deputy Flash report
                    self.report_repo.create_next_version(
                        cycle_id=cycle_id,
                        sprint_id=resolved_sprint_id,
                        model=self.gateway.flash_model,
                        report_type=WEEKLY_DEPUTY_INTERNAL_REPORT_TYPE,
                        privacy_level=PrivacyLevel.SERVER_ONLY,
                        content={
                            "agent_role": AegisAgentRole.DEPUTY_CHIEF_PHYSICIAN.value,
                            "sprint_id": resolved_sprint_id,
                            "sprint_number": resolved_sprint_number,
                            "weekly_deputy_analysis": flash_output.weekly_deputy_analysis,
                            "draft_user_system_reports": draft_weekly_reports_dict,
                        },
                        commit=False,
                    )

                    # Save server_only Weekly Chief Physician Pro report
                    self.report_repo.create_next_version(
                        cycle_id=cycle_id,
                        sprint_id=resolved_sprint_id,
                        model=self.gateway.pro_model,
                        report_type=WEEKLY_CHIEF_INTERNAL_REPORT_TYPE,
                        privacy_level=PrivacyLevel.SERVER_ONLY,
                        content={
                            "agent_role": AegisAgentRole.CHIEF_PHYSICIAN.value,
                            "sprint_id": resolved_sprint_id,
                            "sprint_number": resolved_sprint_number,
                            "updated_weekly_strategy": pro_output.updated_weekly_strategy,
                            "strategy_status": pro_output.strategy_status,
                            "flash_quality_review": pro_output.flash_quality_review.model_dump(),
                        },
                        commit=False,
                    )

                    # Save new active Athena strategy version linked to cycle_id, sprint_id, and version
                    next_strategy_ver = (
                        self.report_repo.get_latest_version_number(
                            cycle_id=cycle_id,
                            report_type=ATHENA_ACTIVE_STRATEGY_REPORT_TYPE,
                            privacy_level=PrivacyLevel.SERVER_ONLY,
                        )
                        + 1
                    )
                    self.report_repo.create_next_version(
                        cycle_id=cycle_id,
                        sprint_id=resolved_sprint_id,
                        model=self.gateway.pro_model,
                        report_type=ATHENA_ACTIVE_STRATEGY_REPORT_TYPE,
                        privacy_level=PrivacyLevel.SERVER_ONLY,
                        content={
                            "agent_role": AegisAgentRole.CHIEF_PHYSICIAN.value,
                            "source_consilium": "weekly_consilium",
                            "strategy_status": pro_output.strategy_status,
                            "strategy_version": next_strategy_ver,
                            "sprint_id": resolved_sprint_id,
                            "sprint_number": resolved_sprint_number,
                            "active_strategy_text": pro_output.updated_weekly_strategy,
                            "clinical_background_text": (
                                "Deep correction of the Chief Physician's strategy based on weekly dynamics\n\n"
                                + prev_ctx["latest_pro_training"]
                            ),
                            "deputy_synthesis_text": flash_output.weekly_deputy_analysis,
                            "daily_question_limit": 3,
                        },
                        commit=False,
                    )

                    # Save 8 client_facing dated system reports for Atlas (preserving historical entries)
                    client_system_reports: Dict[str, ClientModelReportResponse] = {}
                    for system_key in SYSTEM_KEYS:
                        sys_report_type = f"system_user_report_{system_key}"
                        prior_versions = self.report_repo.list_versions(
                            cycle_id=cycle_id,
                            report_type=sys_report_type,
                            privacy_level=PrivacyLevel.CLIENT_FACING,
                        )
                        historical_entries: List[Dict[str, Any]] = []
                        if prior_versions:
                            latest_prior_entries = (prior_versions[-1].content or {}).get("entries", [])
                            if isinstance(latest_prior_entries, list):
                                historical_entries = [dict(e) for e in latest_prior_entries if isinstance(e, dict)]

                        verified_summary = final_system_updates[system_key]["summary"]
                        has_changes = bool(final_system_updates[system_key]["has_changes"])
                        new_dated_entry = {
                            "date": resolved_date,
                            "summary": verified_summary,
                            "has_changes": has_changes,
                        }
                        combined_entries = historical_entries + [new_dated_entry]

                        saved_sys_report = self.report_repo.create_next_version(
                            cycle_id=cycle_id,
                            sprint_id=resolved_sprint_id,
                            model=self.gateway.flash_model,
                            report_type=sys_report_type,
                            privacy_level=PrivacyLevel.CLIENT_FACING,
                            content={
                                "title": f"{SYSTEM_TITLES[system_key]} Overview",
                                "summary": verified_summary,
                                "report_date": resolved_date,
                                "system_key": system_key,
                                "has_changes": has_changes,
                                "entries": combined_entries,
                            },
                            commit=False,
                        )
                        client_system_reports[system_key] = ClientModelReportResponse.model_validate(
                            saved_sys_report
                        )

                    # Save client_facing weekly overall report for the Client Vault
                    saved_overall_report = self.report_repo.create_next_version(
                        cycle_id=cycle_id,
                        sprint_id=resolved_sprint_id,
                        model=self.gateway.pro_model,
                        report_type=WEEKLY_OVERALL_CLIENT_REPORT_TYPE,
                        privacy_level=PrivacyLevel.CLIENT_FACING,
                        content={
                            "title": pro_output.weekly_overall_report.title,
                            "summary": pro_output.weekly_overall_report.summary,
                            "report_date": resolved_date,
                            "highlights": list(pro_output.weekly_overall_report.highlights),
                            "metrics": {
                                "sprint_number": resolved_sprint_number,
                            },
                        },
                        commit=False,
                    )
                    validated_overall = ClientModelReportResponse.model_validate(saved_overall_report)

                    # Save weekly consilium execution log for audit and idempotency
                    self.report_repo.create_next_version(
                        cycle_id=cycle_id,
                        sprint_id=resolved_sprint_id,
                        model="aegis-weekly-consilium-pipeline",
                        report_type=WEEKLY_CONSILIUM_EXECUTION_LOG_REPORT_TYPE,
                        privacy_level=PrivacyLevel.SERVER_ONLY,
                        content={
                            "lease_owner_id": lease_owner_id,
                            "idempotency_key": idempotency_key,
                            "sprint_id": resolved_sprint_id,
                            "sprint_number": resolved_sprint_number,
                            "report_date": resolved_date,
                            "execution_date_utc": self._now_utc().isoformat(),
                            "observation_period_start_utc": window_start.isoformat(),
                            "observation_period_end_utc": window_end.isoformat(),
                            "completed_at_utc": self._now_utc().isoformat(),
                            "system_count": len(final_system_updates),
                        },
                        commit=False,
                    )

                    self.db.commit()
                except Exception:
                    self.db.rollback()
                    raise

                return WeeklyConsiliumClientResponse(
                    cycle_id=cycle_id,
                    sprint_id=resolved_sprint_id,
                    sprint_number=resolved_sprint_number,
                    report_date=resolved_date,
                    overall_report=validated_overall,
                    system_reports=client_system_reports,
                )
            finally:
                self._release_cross_process_lease_safe(cycle_id, lease_owner_id)
