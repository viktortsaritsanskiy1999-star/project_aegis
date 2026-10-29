import asyncio
from datetime import datetime, timedelta, timezone
import json
from typing import Any, Awaitable, Callable, Dict, List, Optional
import uuid
from sqlalchemy import delete, update
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from app.models.model_report import ModelReport
from app.repositories.cycle_repository import MedicalCycleRepository
from app.repositories.measurement_repository import HealthMeasurementRepository
from app.repositories.report_repository import ModelReportRepository
from app.schemas.consilium import (
    SYSTEM_KEYS,
    SYSTEM_TITLES,
    AegisAgentRole,
    AnamnesisModelOutput,
    AnamnesisQuestionnaireAnswers,
    AnamnesisIntakeClientResponse,
    SystemAgentOutput,
    FlashMasterOutput,
    ProMasterOutput,
    ClientVitalsThresholds,
    PrimaryConsiliumClientResponse,
)
from app.schemas.privacy import PrivacyLevel
from app.schemas.reports import ClientModelReportResponse
from app.services.consilium_prompts import (
    build_anamnesis_prompt,
    build_anamnesis_m1_context_from_answers,
    build_system_agent_prompt,
    build_flash_master_prompt,
    build_pro_master_prompt,
)
from app.services.gemini_gateway import GeminiGateway, GeminiModelRole


RAW_USER_ANAMNESIS_REPORT_TYPE = "raw_user_anamnesis"
PREPARATORY_ANAMNESIS_REPORT_TYPE = "preparatory_anamnesis_m1"
PREPARATORY_OCR_REPORT_TYPE = "preparatory_vision_ocr"
PREPARATORY_ECG_REPORT_TYPE = "preparatory_vision_ecg"
PRIMARY_CONSILIUM_IN_FLIGHT_LOCK_REPORT_TYPE = "primary_consilium_in_flight_lock"
PRIMARY_CONSILIUM_EXECUTION_LOG_REPORT_TYPE = "primary_consilium_execution_log"
PRIMARY_CONSILIUM_LOCK_TTL_SECONDS = 300
PRIMARY_CONSILIUM_LOCK_HEARTBEAT_SECONDS = 2.0


class ConsiliumError(Exception):
    """Base exception for server-side Primary Consilium orchestration."""


class ConsiliumCycleNotFoundError(ConsiliumError):
    """Raised when the requested medical cycle does not exist."""


class ConsiliumContextMissingError(ConsiliumError):
    """
    Raised when required preparatory context (such as Anamnesis M1) is missing
    from server storage. Primary Consilium never re-runs Anamnesis or Vision internally.
    """


class ConsiliumQualityRejectionError(ConsiliumError):
    """
    Raised when Chief Physician (Pro) rejects publication of user-facing reports
    during the mandatory quality control review.
    """


class ConsiliumAnamnesisLockedError(ConsiliumError):
    """
    Raised when user attempts to modify or resubmit baseline preparatory Anamnesis
    after Primary Consilium has already been executed for the medical cycle.
    This protects the authoritative preparatory M1 snapshot of the current stage;
    versioned anamnesis tracking across long-term cycles will be designed separately.
    """


class ConsiliumConcurrencyConflictError(ConsiliumError):
    """
    Raised when concurrent worker processes attempt to commit conflicting intake
    versions or answers simultaneously.
    """


class PrimaryConsiliumService:
    """
    Server-side orchestrator for the Primary Medical Consilium:
    8 Gemini Flash-Lite (Organ Specialists) -> Gemini Flash (Deputy Chief Physician) -> Gemini Pro (Chief Physician).

    Strictly separates logical agent roles (AegisAgentRole) from physical Gemini models
    and reads preparatory results (Anamnesis M1, Vision OCR, Vision ECG, Vitals)
    from server storage without re-running Anamnesis or Vision inside the consilium.
    """

    _default_intake_locks: Dict[str, asyncio.Lock] = {}

    def __init__(
        self,
        db: Session,
        gateway: Optional[GeminiGateway] = None,
        now_provider: Optional[Callable[[], datetime]] = None,
        lock_registry: Optional[Dict[str, asyncio.Lock]] = None,
        lease_ttl_seconds: int = PRIMARY_CONSILIUM_LOCK_TTL_SECONDS,
        lease_heartbeat_interval_seconds: float = PRIMARY_CONSILIUM_LOCK_HEARTBEAT_SECONDS,
    ):
        self.db = db
        self.gateway = gateway or GeminiGateway()
        self.now_provider = now_provider or (lambda: datetime.now(timezone.utc))
        self.cycle_repo = MedicalCycleRepository(db)
        self.report_repo = ModelReportRepository(db)
        self.measurement_repo = HealthMeasurementRepository(db)
        self._intake_locks = lock_registry if lock_registry is not None else self._default_intake_locks
        self.lease_ttl_seconds = max(1, int(lease_ttl_seconds))
        self.lease_heartbeat_interval_seconds = max(0.001, float(lease_heartbeat_interval_seconds))

    def _get_cycle_lock(self, cycle_id: str) -> asyncio.Lock:
        if cycle_id not in self._intake_locks:
            self._intake_locks[cycle_id] = asyncio.Lock()
        return self._intake_locks[cycle_id]

    def _get_intake_lock(self, cycle_id: str) -> asyncio.Lock:
        return self._get_cycle_lock(cycle_id)

    def _now_utc(self) -> datetime:
        dt = self.now_provider()
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)

    @staticmethod
    def _parse_iso_utc(iso_str: Optional[str]) -> Optional[datetime]:
        if not iso_str or not isinstance(iso_str, str):
            return None
        try:
            dt = datetime.fromisoformat(iso_str)
            if dt.tzinfo is None:
                return dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except (ValueError, TypeError):
            return None

    @staticmethod
    def _lock_report_id(cycle_id: str) -> str:
        return f"lock_primary_{cycle_id}"[:64]

    def _fetch_fresh_lock_row(self, cycle_id: str) -> Optional[ModelReport]:
        """
        Queries the cross-process lock row directly from SQLite using `.populate_existing()`
        so the current SQLAlchemy Session never relies on a stale cached identity-map copy.
        """
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
        If an expired lease row exists from a crashed or hung worker (> TTL without renewal),
        cleans it up atomically using a WHERE condition matching the exact stale owner and expiration,
        ensuring that a concurrent worker's active or renewed lease is NEVER deleted.
        """
        lock_row = self._fetch_fresh_lock_row(cycle_id)
        if lock_row is None:
            return None
        now_dt = self._now_utc()
        lock_content = lock_row.content or {}
        stale_owner_id = lock_content.get("lease_owner_id")
        stale_expires_iso = lock_content.get("expires_at_utc")
        expires_dt = self._parse_iso_utc(stale_expires_iso)

        if expires_dt is not None and expires_dt <= now_dt and stale_owner_id and stale_expires_iso:
            lock_id = self._lock_report_id(cycle_id)
            stmt = (
                delete(ModelReport)
                .where(ModelReport.report_id == lock_id)
                .where(ModelReport.content["lease_owner_id"].as_string() == stale_owner_id)
                .where(ModelReport.content["expires_at_utc"].as_string() == stale_expires_iso)
            )
            res = self.db.execute(stmt)
            self.db.commit()
            if res.rowcount > 0:
                return None
            # If rowcount == 0, the lease was already deleted, renewed, or superseded in a race
            fresh_row = self._fetch_fresh_lock_row(cycle_id)
            if fresh_row is None:
                return None
            fresh_expires = self._parse_iso_utc((fresh_row.content or {}).get("expires_at_utc"))
            if fresh_expires is not None and fresh_expires > now_dt:
                return fresh_row
            return None
        return lock_row

    def _acquire_cross_process_lease(
        self, cycle_id: str, idempotency_key: Optional[str] = None
    ) -> str:
        """
        Acquires an atomic database-level execution lease (`report_id = lock_primary_{cycle_id}`)
        backed by the SQLite PRIMARY KEY constraint on `model_reports.report_id` and tagged
        with a unique `lease_owner_id` fencing token.
        """
        active_lock = self._get_active_in_flight_lock(cycle_id)
        if active_lock is not None:
            lock_content = active_lock.content or {}
            raise ConsiliumConcurrencyConflictError(
                f"Primary Consilium for cycle '{cycle_id}' is currently in progress "
                f"(lease active until {lock_content.get('expires_at_utc')})."
            )

        now_dt = self._now_utc()
        expires_dt = now_dt + timedelta(seconds=self.lease_ttl_seconds)
        lock_id = self._lock_report_id(cycle_id)
        lease_owner_id = f"lease_primary_{uuid.uuid4().hex}"

        try:
            lock_report = ModelReport(
                report_id=lock_id,
                cycle_id=cycle_id,
                sprint_id=None,
                session_id=None,
                model="aegis-concurrency-guard",
                report_type=PRIMARY_CONSILIUM_IN_FLIGHT_LOCK_REPORT_TYPE,
                privacy_level=PrivacyLevel.SERVER_ONLY.value,
                version=1,
                content={
                    "lease_owner_id": lease_owner_id,
                    "idempotency_key": idempotency_key,
                    "acquired_at_utc": now_dt.isoformat(),
                    "renewed_at_utc": now_dt.isoformat(),
                    "expires_at_utc": expires_dt.isoformat(),
                    "renewal_count": 0,
                },
                created_at=now_dt,
            )
            self.db.add(lock_report)
            self.db.commit()
            return lease_owner_id
        except (IntegrityError, OperationalError):
            self.db.rollback()
            raise ConsiliumConcurrencyConflictError(
                f"Primary Consilium for cycle '{cycle_id}' is currently in progress."
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
            .where(ModelReport.report_id == lock_id)
            .where(ModelReport.content["lease_owner_id"].as_string() == lease_owner_id)
            .values(content=updated_content, version=ModelReport.version + 1)
        )
        res = target_db.execute(stmt)
        target_db.commit()
        if res.rowcount == 0:
            raise ConsiliumConcurrencyConflictError(
                f"Execution lease '{lease_owner_id}' for cycle '{cycle_id}' is no longer "
                "owned by this worker; aborting stale consilium execution."
            )

    async def _execute_with_lease_renewal(
        self,
        cycle_id: str,
        lease_owner_id: str,
        coro_factory: Callable[[], Awaitable[Any]],
    ) -> Any:
        """
        Executes an async Gemini gateway call while keeping the SQLite execution lease alive:
        - Renews & verifies `lease_owner_id` before the call;
        - Periodically renews `lease_owner_id` via a background heartbeat using an isolated DB session while awaiting I/O;
        - If heartbeat fails to renew (lease lost or DB error), the error is immediately observed
          by the main executor, cancelling long-running tasks and aborting publication;
        - Renews & verifies `lease_owner_id` immediately after the call returns.
        """
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
        Fencing check before final atomic persistence: verifies and deletes the SQLite lease
        in the uncommitted transaction using an atomic DELETE matching lease_owner_id.
        If rowcount is 0, the lease was lost or superseded; raises ConsiliumConcurrencyConflictError,
        which triggers a rollback and blocks publication of all client-facing reports.
        Never deletes a lease owned by another worker.
        """
        lock_id = self._lock_report_id(cycle_id)
        stmt = (
            delete(ModelReport)
            .where(ModelReport.report_id == lock_id)
            .where(ModelReport.content["lease_owner_id"].as_string() == lease_owner_id)
        )
        res = self.db.execute(stmt)
        if res.rowcount == 0:
            raise ConsiliumConcurrencyConflictError(
                f"Cannot commit Primary Consilium for cycle '{cycle_id}': "
                f"execution lease '{lease_owner_id}' was lost or superseded by another owner."
            )

    def _release_cross_process_lease_safe(self, cycle_id: str, lease_owner_id: str) -> None:
        """
        Safely releases the cross-process execution lease on error ONLY if the lock row
        is still owned by `lease_owner_id`. Never deletes a lock acquired by another worker.
        Uses an isolated session with an atomic DELETE matching lease_owner_id.
        """
        try:
            lock_id = self._lock_report_id(cycle_id)
            stmt = (
                delete(ModelReport)
                .where(ModelReport.report_id == lock_id)
                .where(ModelReport.content["lease_owner_id"].as_string() == lease_owner_id)
            )
            with Session(bind=self.db.get_bind(), autocommit=False, autoflush=False) as safe_session:
                safe_session.execute(stmt)
                safe_session.commit()
        except Exception:
            pass

    def recover_completed_response(
        self, cycle_id: str
    ) -> Optional[PrimaryConsiliumClientResponse]:
        """
        Recovers existing client-facing Primary Consilium reports directly from storage.
        Guarantees zero duplicate Gemini API calls on client retries or reconnections.
        """
        overall_reports = self.report_repo.list_client_facing(
            cycle_id=cycle_id,
            report_type="user_overall_report",
        )
        if not overall_reports:
            return None

        latest_overall = overall_reports[0]
        overall_validated = ClientModelReportResponse.model_validate(latest_overall)
        content = latest_overall.content or {}
        report_date = str(content.get("report_date") or "")
        metrics = content.get("metrics") or {}

        system_reports: Dict[str, ClientModelReportResponse] = {}
        for sys_key in SYSTEM_KEYS:
            sys_reps = self.report_repo.list_client_facing(
                cycle_id=cycle_id,
                report_type=f"system_user_report_{sys_key}",
            )
            if sys_reps:
                system_reports[sys_key] = ClientModelReportResponse.model_validate(sys_reps[0])

        return PrimaryConsiliumClientResponse(
            cycle_id=cycle_id,
            report_date=report_date or self._now_utc().strftime("%Y-%m-%d"),
            overall_report=overall_validated,
            system_reports=system_reports,
            vitals_thresholds=ClientVitalsThresholds(
                min_pessimum_bpm=int(metrics.get("min_pessimum_bpm", 45)),
                max_pessimum_bpm=int(metrics.get("max_pessimum_bpm", 115)),
                max_spike_delta_bpm=int(metrics.get("max_spike_delta_bpm", 25)),
            ),
        )

    def is_primary_consilium_completed(self, cycle_id: str) -> bool:
        """
        Returns True if Primary Consilium has already been successfully executed
        and published for this medical cycle.
        """
        client_facing = self.report_repo.get_latest_version_number(
            cycle_id=cycle_id,
            report_type="user_overall_report",
            privacy_level=PrivacyLevel.CLIENT_FACING,
        )
        if client_facing > 0:
            return True
        internal = self.report_repo.get_latest_version_number(
            cycle_id=cycle_id,
            report_type="chief_physician_internal",
            privacy_level=PrivacyLevel.SERVER_ONLY,
        )
        return internal > 0

    def get_latest_raw_anamnesis(self, cycle_id: str) -> Optional[ModelReport]:
        reports = self.report_repo.list_server_only(
            cycle_id=cycle_id,
            report_type=RAW_USER_ANAMNESIS_REPORT_TYPE,
        )
        return reports[0] if reports else None

    async def process_anamnesis_intake(
        self,
        cycle_id: str,
        answers: AnamnesisQuestionnaireAnswers,
    ) -> AnamnesisIntakeClientResponse:
        """
        Processes raw questionnaire answers from the client:
        1. Validates that the medical cycle exists.
        2. Strictly blocks modification if Primary Consilium has already concluded for this cycle.
        3. Idempotently returns existing version if identical answers are resubmitted.
        4. Reconstructs blind Anamnesis M1 context deterministically on the server.
        5. Invokes Gemini Flash (GeminiModelRole.FLASH) as the Anamnesis M1 agent.
        6. Atomically persists:
           - raw_user_anamnesis (server_only, raw answers)
           - preparatory_anamnesis_m1 (server_only, verified M1 output, agent_role=anamnesis_m1)
        7. Returns safe client response with only service-level confirmation metadata.
        """
        if not self.cycle_repo.get(cycle_id):
            raise ConsiliumCycleNotFoundError(f"Medical cycle '{cycle_id}' not found.")

        # 1. Reject any alteration of baseline preparatory anamnesis if Primary Consilium was already executed
        if self.is_primary_consilium_completed(cycle_id):
            raise ConsiliumAnamnesisLockedError(
                f"Anamnesis baseline snapshot cannot be modified: Primary Consilium has already "
                f"been executed and published for cycle '{cycle_id}'."
            )

        lock = self._get_intake_lock(cycle_id)
        async with lock:
            # Re-check completion under lock
            if self.is_primary_consilium_completed(cycle_id):
                raise ConsiliumAnamnesisLockedError(
                    f"Anamnesis baseline snapshot cannot be modified: Primary Consilium has already "
                    f"been executed and published for cycle '{cycle_id}'."
                )

            # 2. Check for duplicate/identical submission within this cycle (idempotency)
            latest_raw = self.get_latest_raw_anamnesis(cycle_id)
            if (
                latest_raw
                and isinstance(latest_raw.content, dict)
                and latest_raw.content.get("answers") == answers.model_dump()
            ):
                m1_reports = self.report_repo.list_server_only(
                    cycle_id=cycle_id,
                    report_type=PREPARATORY_ANAMNESIS_REPORT_TYPE,
                )
                if m1_reports:
                    return AnamnesisIntakeClientResponse(
                        cycle_id=cycle_id,
                        status="processed",
                        intake_version=m1_reports[0].version,
                        created_at_utc=(
                            m1_reports[0].created_at.isoformat()
                            if m1_reports[0].created_at
                            else datetime.now(timezone.utc).isoformat()
                        ),
                    )

            base_version = latest_raw.version if latest_raw is not None else 0
            target_version = base_version + 1

            # 3. Reconstruct blind M1 context deterministically on server from allowlisted answers
            blind_context = build_anamnesis_m1_context_from_answers(answers)
            prompt = build_anamnesis_prompt(blind_context)

            # 4. Invoke Gemini Flash (M1)
            parsed: AnamnesisModelOutput = await self.gateway.generate_structured(
                prompt=prompt,
                response_schema=AnamnesisModelOutput,
                model=GeminiModelRole.FLASH,
            )

            # 5. Atomic database transaction with cross-process concurrency recovery
            raw_version = target_version
            prefix_raw = f"rep_{cycle_id}_{RAW_USER_ANAMNESIS_REPORT_TYPE}_v{raw_version}"
            raw_id = prefix_raw if len(prefix_raw) <= 64 else f"rep_{raw_version}_{RAW_USER_ANAMNESIS_REPORT_TYPE}_{cycle_id}"[:64]
            prefix_m1 = f"rep_{cycle_id}_{PREPARATORY_ANAMNESIS_REPORT_TYPE}_v{raw_version}"
            m1_id = prefix_m1 if len(prefix_m1) <= 64 else f"rep_{raw_version}_{PREPARATORY_ANAMNESIS_REPORT_TYPE}_{cycle_id}"[:64]

            try:
                self.report_repo.create(
                    report_id=raw_id,
                    cycle_id=cycle_id,
                    model="client-anamnesis-intake",
                    report_type=RAW_USER_ANAMNESIS_REPORT_TYPE,
                    privacy_level=PrivacyLevel.SERVER_ONLY,
                    content={
                        "answers": answers.model_dump(),
                        "source": "client_intake_questionnaire",
                    },
                    version=raw_version,
                    commit=False,
                )

                m1_payload = parsed.model_dump()
                m1_payload["agent_role"] = AegisAgentRole.ANAMNESIS_M1.value
                m1_report = self.report_repo.create(
                    report_id=m1_id,
                    cycle_id=cycle_id,
                    model=self.gateway.flash_model,
                    report_type=PREPARATORY_ANAMNESIS_REPORT_TYPE,
                    privacy_level=PrivacyLevel.SERVER_ONLY,
                    content=m1_payload,
                    version=raw_version,
                    commit=False,
                )

                self.db.commit()
                self.db.refresh(m1_report)
            except Exception as exc:
                self.db.rollback()
                # Cross-process idempotency check: if another process committed identical answers concurrently
                concurrent_raw = self.get_latest_raw_anamnesis(cycle_id)
                if (
                    concurrent_raw
                    and isinstance(concurrent_raw.content, dict)
                    and concurrent_raw.content.get("answers") == answers.model_dump()
                ):
                    m1_reports = self.report_repo.list_server_only(
                        cycle_id=cycle_id,
                        report_type=PREPARATORY_ANAMNESIS_REPORT_TYPE,
                    )
                    if m1_reports:
                        return AnamnesisIntakeClientResponse(
                            cycle_id=cycle_id,
                            status="processed",
                            intake_version=m1_reports[0].version,
                            created_at_utc=(
                                m1_reports[0].created_at.isoformat()
                                if m1_reports[0].created_at
                                else datetime.now(timezone.utc).isoformat()
                            ),
                        )
                # If answers are different or transaction failed, NEVER return false success
                raise ConsiliumConcurrencyConflictError(
                    f"Concurrent intake transaction conflict for cycle '{cycle_id}': {exc}"
                ) from exc

            created_at_utc = (
                m1_report.created_at.isoformat()
                if m1_report.created_at
                else datetime.now(timezone.utc).isoformat()
            )

            return AnamnesisIntakeClientResponse(
                cycle_id=cycle_id,
                status="processed",
                intake_version=m1_report.version,
                created_at_utc=created_at_utc,
            )

    # ==========================================================================
    # PREPARATORY CONTEXT HELPERS (STANDALONE — NEVER CALLED INSIDE CONSILIUM)
    # ==========================================================================

    async def run_preparatory_blind_anamnesis(
        self,
        cycle_id: str,
        raw_anamnesis_context: Dict[str, Any],
    ) -> ModelReport:
        """
        Standalone preparatory step for Anamnesis (M1, role='anamnesis_m1').
        Strips all laboratory fields to enforce strict blind mode, invokes Gemini Flash
        as the Anamnesis M1 agent, and stores the verified result as a server_only report.
        NOTE: Never called inside run_primary_consilium().
        """
        if not self.cycle_repo.get(cycle_id):
            raise ConsiliumCycleNotFoundError(f"Medical cycle '{cycle_id}' not found.")

        prompt = build_anamnesis_prompt(raw_anamnesis_context)
        parsed: AnamnesisModelOutput = await self.gateway.generate_structured(
            prompt=prompt,
            response_schema=AnamnesisModelOutput,
            model=GeminiModelRole.FLASH,
        )
        return self.save_preparatory_anamnesis_result(
            cycle_id=cycle_id,
            anamnesis_output=parsed,
        )

    def save_preparatory_anamnesis_result(
        self,
        cycle_id: str,
        anamnesis_output: AnamnesisModelOutput | Dict[str, Any],
    ) -> ModelReport:
        """
        Stores an already-verified Anamnesis (M1) result as a server_only report
        tagged with logical role AegisAgentRole.ANAMNESIS_M1.
        """
        if not self.cycle_repo.get(cycle_id):
            raise ConsiliumCycleNotFoundError(f"Medical cycle '{cycle_id}' not found.")

        validated = (
            anamnesis_output
            if isinstance(anamnesis_output, AnamnesisModelOutput)
            else AnamnesisModelOutput.model_validate(anamnesis_output)
        )
        payload = validated.model_dump()
        payload["agent_role"] = AegisAgentRole.ANAMNESIS_M1.value

        return self.report_repo.create_next_version(
            cycle_id=cycle_id,
            model=self.gateway.flash_model,
            report_type=PREPARATORY_ANAMNESIS_REPORT_TYPE,
            privacy_level=PrivacyLevel.SERVER_ONLY,
            content=payload,
        )

    def save_preparatory_ocr_labs(
        self,
        cycle_id: str,
        shared_labs_table: Dict[str, Any],
        created_at: Optional[datetime] = None,
    ) -> ModelReport:
        """
        Stores verified Vision OCR extracted laboratory table as a server_only report
        tagged with logical role AegisAgentRole.VISION_OCR.
        """
        if not self.cycle_repo.get(cycle_id):
            raise ConsiliumCycleNotFoundError(f"Medical cycle '{cycle_id}' not found.")

        return self.report_repo.create_next_version(
            cycle_id=cycle_id,
            model=self.gateway.flash_model,
            report_type=PREPARATORY_OCR_REPORT_TYPE,
            privacy_level=PrivacyLevel.SERVER_ONLY,
            content={
                "agent_role": AegisAgentRole.VISION_OCR.value,
                "shared_labs_table": dict(shared_labs_table),
            },
            created_at=created_at,
        )

    def save_preparatory_ecg_summary(
        self,
        cycle_id: str,
        ecg_summary: str,
    ) -> ModelReport:
        """
        Stores verified Vision ECG parameter summary as a server_only report
        tagged with logical role AegisAgentRole.VISION_ECG.
        """
        if not self.cycle_repo.get(cycle_id):
            raise ConsiliumCycleNotFoundError(f"Medical cycle '{cycle_id}' not found.")

        return self.report_repo.create_next_version(
            cycle_id=cycle_id,
            model=self.gateway.flash_model,
            report_type=PREPARATORY_ECG_REPORT_TYPE,
            privacy_level=PrivacyLevel.SERVER_ONLY,
            content={
                "agent_role": AegisAgentRole.VISION_ECG.value,
                "ecg_summary": str(ecg_summary).strip() or "Not conducted",
            },
        )

    # ==========================================================================
    # INTERNAL CONTEXT LOADERS FROM SERVER STORAGE
    # ==========================================================================

    def _load_preparatory_anamnesis(self, cycle_id: str) -> AnamnesisModelOutput:
        reports = self.report_repo.list_server_only(
            cycle_id=cycle_id,
            report_type=PREPARATORY_ANAMNESIS_REPORT_TYPE,
        )
        if not reports:
            raise ConsiliumContextMissingError(
                f"Cannot run Primary Consilium for cycle '{cycle_id}': verified Anamnesis (M1) "
                f"report ('{PREPARATORY_ANAMNESIS_REPORT_TYPE}') is missing in server storage."
            )
        latest_content = dict(reports[0].content or {})
        stored_role = latest_content.pop("agent_role", None)
        if stored_role != AegisAgentRole.ANAMNESIS_M1.value:
            raise ConsiliumContextMissingError(
                f"Invalid agent_role '{stored_role}' on preparatory Anamnesis M1 report; "
                f"expected '{AegisAgentRole.ANAMNESIS_M1.value}'."
            )
        return AnamnesisModelOutput.model_validate(latest_content)

    def _load_preparatory_labs(self, cycle_id: str) -> Dict[str, Any]:
        from app.services.lab_context_service import assemble_consilium_laboratory_context

        lab_ctx = assemble_consilium_laboratory_context(self.db, cycle_id)
        return dict(lab_ctx.shared_labs_table)

    def _load_preparatory_ecg_context(self, cycle_id: str):
        from app.services.ecg_context_service import assemble_consilium_ecg_context

        return assemble_consilium_ecg_context(self.db, cycle_id)

    def _load_preparatory_ecg(self, cycle_id: str) -> str:
        return self._load_preparatory_ecg_context(cycle_id).ecg_summary

    def _load_latest_vitals_stream(
        self,
        cycle_id: str,
        ecg_summary: str,
        ecg_context: Optional[Any] = None,
    ) -> Dict[str, Any]:
        measurements = self.measurement_repo.list_by_cycle(cycle_id=cycle_id, is_historical=False, is_simulated=False, limit=50)
        systolic: Optional[int] = None
        diastolic: Optional[int] = None
        pulse: Optional[int] = None
        spo2: Optional[int] = None

        for m in reversed(measurements):
            if systolic is None and m.systolic is not None:
                systolic = m.systolic
            if diastolic is None and m.diastolic is not None:
                diastolic = m.diastolic
            if pulse is None and m.pulse is not None:
                pulse = m.pulse
            if spo2 is None and m.spo2 is not None:
                spo2 = m.spo2
            if all(v is not None for v in (systolic, diastolic, pulse, spo2)):
                break

        res: Dict[str, Any] = {
            "blood_pressure": f"{systolic if systolic is not None else '--'}/{diastolic if diastolic is not None else '--'} mmHg",
            "heart_rate": f"{pulse if pulse is not None else '--'} bpm",
            "spo2": f"{spo2 if spo2 is not None else '--'}%",
            "ecg_summary": ecg_summary or "Not conducted",
        }
        if ecg_context is not None:
            res["ecg_context"] = (
                ecg_context.to_dict() if hasattr(ecg_context, "to_dict") else dict(ecg_context)
            )
        return res

    @staticmethod
    def _assemble_context_for_flash(
        anamnesis: AnamnesisModelOutput,
        shared_labs_table: Dict[str, Any],
        vitals_stream: Dict[str, Any],
        system_outputs: Dict[str, SystemAgentOutput],
        laboratory_history: Optional[List[Dict[str, Any]]] = None,
        ecg_context: Optional[Any] = None,
    ) -> str:
        """
        Mirrors VaultService.getAggregatedContextForMaster() from Flutter, extended with
        full laboratory_history for complete multi-date trajectory audit and structured ecg_context.
        """
        payload: Dict[str, Any] = {
            "user_view": anamnesis.user_view.model_dump(),
            "training_sandbox": anamnesis.training_sandbox.model_dump(),
            "shared_labs_shelf": shared_labs_table,
        }
        if laboratory_history is not None:
            payload["laboratory_history"] = laboratory_history
        if ecg_context is not None:
            payload["ecg_context"] = (
                ecg_context.to_dict() if hasattr(ecg_context, "to_dict") else dict(ecg_context)
            )

        lines = [
            "=== ANAMNESIS AND INPUT DATA ===",
            json.dumps(
                payload,
                ensure_ascii=False,
            ),
            "",
            "=== VITAL MARKERS (SMART WATCH / LIVE) ===",
            f"Blood pressure: {vitals_stream['blood_pressure']}",
            f"Heart rate (Pulse): {vitals_stream['heart_rate']}",
            f"Oxygen saturation (SpO2): {vitals_stream['spo2']}",
            f"ECG status: {vitals_stream['ecg_summary']}",
            "",
            "=== CONCLUSIONS OF 8 ORGAN SYSTEM AGENTS ===",
        ]
        for key in SYSTEM_KEYS:
            sys_out = system_outputs[key]
            lines.append(f"--- BLOCK: {key.upper()} ---")
            lines.append(f"Main conclusion: {sys_out.system_logic.system_general_shelf}")
            lines.append(f"Training log: {sys_out.training_sandbox.system_training_shelf}")
            lines.append("")
        return "\n".join(lines)

    @staticmethod
    def _assemble_context_for_pro(
        initial_master_context: str,
        flash_output: FlashMasterOutput,
    ) -> str:
        """
        Mirrors VaultService.getAggregatedContextForMasterWithFlash() and includes
        Flash's 8 draft user_system_reports so Chief Physician (Pro) can audit and correct them.
        """
        lines = [
            initial_master_context,
            "=== SYNTHESIS (GEMINI FLASH) ===",
            "Main conclusion:",
            flash_output.flash_general_shelf,
            "",
            "Training output:",
            flash_output.flash_training_shelf,
            "",
            "=== DRAFT 8 USER SYSTEM REPORTS FROM DEPUTY FLASH (FOR PRO QUALITY REVIEW) ===",
            json.dumps(flash_output.user_system_reports.to_dict(), ensure_ascii=False),
            "",
        ]
        return "\n".join(lines)

    # ==========================================================================
    # PRIMARY CONSILIUM ORCHESTRATION (8 FLASH-LITE -> DEPUTY FLASH -> PRO)
    # ==========================================================================

    async def run_primary_consilium(
        self,
        cycle_id: str,
        report_date: Optional[str] = None,
        idempotency_key: Optional[str] = None,
        allow_version_bump: bool = False,
    ) -> PrimaryConsiliumClientResponse:
        """
        Executes the server-side Primary Consilium for `cycle_id`:
        1. Reads saved preparatory context (Anamnesis M1, Vision OCR labs, Vision ECG, Vitals).
           Never invokes Anamnesis M1 or Vision internally.
        2. Acquires in-process `asyncio.Lock` and cross-process SQLite execution lease with owner fencing.
        3. Sequentially runs the 8 Flash-Lite organ specialists with background heartbeat lease renewal.
        4. Runs Deputy Chief Physician (Gemini Flash) with lease renewal for intersystem synthesis, audit,
           and 8 draft user_system_reports.
        5. Runs Chief Physician (Gemini Pro) with lease renewal for final clinical strategy (Athena handoff),
           personal heart rate thresholds, user_overall_report, and quality control of Flash's
           8 user_system_reports.
        6. Enforces Pro's quality gate: if approved_for_publication is False, aborts before
           any client_facing report is created. Otherwise applies Pro's corrections over Flash's
           draft user_system_reports.
        7. Atomically inside a single database transaction:
           - Verifies and deletes the cross-process execution lease;
           - Persists versioned server_only reports (8 organ + deputy + chief);
           - Persists server_only authoritative execution log;
           - Persists client_facing reports (8 systems + overall).
        """
        cycle = self.cycle_repo.get(cycle_id)
        if not cycle:
            raise ConsiliumCycleNotFoundError(f"Medical cycle '{cycle_id}' not found.")

        if self.is_primary_consilium_completed(cycle_id):
            from app.services.lab_revision_consilium_service import LabRevisionConsiliumService

            lab_service = LabRevisionConsiliumService(db=self.db, gateway=self.gateway)
            ledger_state = lab_service.get_lab_ledger_state(cycle_id)
            if ledger_state.has_unreviewed_lab_changes:
                raise ConsiliumError(
                    f"Primary Consilium has already been completed for cycle '{cycle_id}', and new laboratory "
                    f"markers (ledger v{ledger_state.ledger_version}) are pending review. "
                    "These markers cannot be evaluated via Primary Consilium; run Full Lab-Revision Consilium instead."
                )
            if not allow_version_bump:
                recovered = self.recover_completed_response(cycle_id)
                if recovered is not None:
                    return recovered

        resolved_date = (
            report_date.strip()
            if isinstance(report_date, str) and report_date.strip()
            else self._now_utc().strftime("%Y-%m-%d")
        )

        cycle_lock = self._get_cycle_lock(cycle_id)
        async with cycle_lock:
            # Re-check after acquiring in-process lock (handles intra-process concurrent callers)
            if self.is_primary_consilium_completed(cycle_id) and not allow_version_bump:
                recovered = self.recover_completed_response(cycle_id)
                if recovered is not None:
                    return recovered

            lease_owner_id = self._acquire_cross_process_lease(
                cycle_id=cycle_id,
                idempotency_key=idempotency_key,
            )

            try:
                # 1. Load already-saved preparatory results from DB (never re-run M1 or Vision here)
                from app.services.lab_context_service import assemble_consilium_laboratory_context
                from app.services.ecg_context_service import assemble_consilium_ecg_context

                anamnesis_m1 = self._load_preparatory_anamnesis(cycle_id)
                lab_context = assemble_consilium_laboratory_context(self.db, cycle_id)
                shared_labs_table = lab_context.shared_labs_table
                laboratory_history = lab_context.laboratory_history
                ecg_context = self._load_preparatory_ecg_context(cycle_id)
                ecg_summary = ecg_context.ecg_summary
                vitals_stream = self._load_latest_vitals_stream(
                    cycle_id, ecg_summary, ecg_context=ecg_context
                )
                ecg_context_dict = ecg_context.to_dict()

                # 2. Execute 8 Organ Specialists (Gemini Flash-Lite)
                system_outputs: Dict[str, SystemAgentOutput] = {}
                for system_key in SYSTEM_KEYS:
                    organ_fact = getattr(anamnesis_m1.system_logic, system_key)
                    input_package = {
                        "system_key": system_key,
                        "facts": organ_fact.data,
                        "first_model_short_summary": organ_fact.internal_comment,
                        "shared_labs_table": shared_labs_table,
                        "laboratory_history": laboratory_history,
                        "vitals_stream": vitals_stream,
                        "ecg_context": ecg_context_dict,
                    }
                    system_prompt = build_system_agent_prompt(system_key, input_package)
                    organ_result: SystemAgentOutput = await self._execute_with_lease_renewal(
                        cycle_id,
                        lease_owner_id,
                        lambda p=system_prompt: self.gateway.generate_structured(
                            prompt=p,
                            response_schema=SystemAgentOutput,
                            model=GeminiModelRole.FLASH_LITE,
                        ),
                    )
                    system_outputs[system_key] = organ_result

                # 3. Execute Deputy Chief Physician (Gemini Flash)
                initial_master_context = self._assemble_context_for_flash(
                    anamnesis=anamnesis_m1,
                    shared_labs_table=shared_labs_table,
                    vitals_stream=vitals_stream,
                    system_outputs=system_outputs,
                    laboratory_history=laboratory_history,
                    ecg_context=ecg_context,
                )
                flash_prompt = build_flash_master_prompt(initial_master_context)
                flash_output: FlashMasterOutput = await self._execute_with_lease_renewal(
                    cycle_id,
                    lease_owner_id,
                    lambda: self.gateway.generate_structured(
                        prompt=flash_prompt,
                        response_schema=FlashMasterOutput,
                        model=GeminiModelRole.FLASH,
                    ),
                )

                # 4. Execute Chief Physician (Gemini Pro)
                full_master_context = self._assemble_context_for_pro(
                    initial_master_context=initial_master_context,
                    flash_output=flash_output,
                )
                pro_prompt = build_pro_master_prompt(full_master_context)
                pro_output: ProMasterOutput = await self._execute_with_lease_renewal(
                    cycle_id,
                    lease_owner_id,
                    lambda: self.gateway.generate_structured(
                        prompt=pro_prompt,
                        response_schema=ProMasterOutput,
                        model=GeminiModelRole.PRO,
                    ),
                )

                # 5. Chief Physician Quality Gate (BEFORE any client_facing report is created)
                if not pro_output.flash_quality_review.approved_for_publication:
                    reason = (
                        pro_output.flash_quality_review.rejection_reason
                        or "Chief Physician (Pro) rejected publication of user-facing reports."
                    )
                    raise ConsiliumQualityRejectionError(reason)

                # Deterministically apply Pro's corrections over Flash's 8 draft user system reports
                final_user_system_texts: Dict[str, str] = flash_output.user_system_reports.to_dict()
                for corrected_key, corrected_text in (
                    pro_output.flash_quality_review.corrected_user_system_reports.items()
                ):
                    if corrected_key not in SYSTEM_KEYS:
                        raise ConsiliumQualityRejectionError(
                            f"Invalid system key '{corrected_key}' in Pro quality review."
                        )
                    if not isinstance(corrected_text, str) or not corrected_text.strip():
                        raise ConsiliumQualityRejectionError(
                            f"Empty corrected report text for system '{corrected_key}'."
                        )
                    final_user_system_texts[corrected_key] = corrected_text.strip()

                # Verify completeness of all 8 user system reports before staging DB writes
                if set(final_user_system_texts.keys()) != set(SYSTEM_KEYS) or any(
                    not isinstance(final_user_system_texts.get(k), str)
                    or not final_user_system_texts[k].strip()
                    for k in SYSTEM_KEYS
                ):
                    raise ConsiliumQualityRejectionError(
                        "Incomplete 8-system user reports after quality review; aborting publication."
                    )

                # 6. Atomic single-transaction persistence of all reports and lease cleanup:
                # Fencing check & atomic removal of execution lease in uncommitted transaction
                self._verify_and_delete_cross_process_lease_uncommitted(cycle_id, lease_owner_id)

                for system_key in SYSTEM_KEYS:
                    organ_out = system_outputs[system_key]
                    agent_role = AegisAgentRole.for_system_key(system_key)
                    self.report_repo.create_next_version(
                        cycle_id=cycle_id,
                        model=self.gateway.flash_lite_model,
                        report_type=agent_role.value,
                        privacy_level=PrivacyLevel.SERVER_ONLY,
                        content={
                            "agent_role": agent_role.value,
                            "system_key": system_key,
                            "system_view": organ_out.system_view.model_dump(),
                            "system_logic": organ_out.system_logic.model_dump(),
                            "training_sandbox": organ_out.training_sandbox.model_dump(),
                        },
                        commit=False,
                    )

                self.report_repo.create_next_version(
                    cycle_id=cycle_id,
                    model=self.gateway.flash_model,
                    report_type="deputy_chief_physician_internal",
                    privacy_level=PrivacyLevel.SERVER_ONLY,
                    content={
                        "agent_role": AegisAgentRole.DEPUTY_CHIEF_PHYSICIAN.value,
                        "flash_general_shelf": flash_output.flash_general_shelf,
                        "flash_training_shelf": flash_output.flash_training_shelf,
                        "draft_user_system_reports": flash_output.user_system_reports.to_dict(),
                    },
                    commit=False,
                )

                evaluated_ledger_version = (
                    lab_context.ledger_version if not lab_context.is_synthetic_fallback else 0
                )

                self.report_repo.create_next_version(
                    cycle_id=cycle_id,
                    model=self.gateway.pro_model,
                    report_type="chief_physician_internal",
                    privacy_level=PrivacyLevel.SERVER_ONLY,
                    content={
                        "agent_role": AegisAgentRole.CHIEF_PHYSICIAN.value,
                        "pro_general_shelf": pro_output.pro_general_shelf,
                        "pro_training_shelf": pro_output.pro_training_shelf,
                        "vitals_thresholds": pro_output.vitals_thresholds.model_dump(),
                        "flash_quality_review": pro_output.flash_quality_review.model_dump(),
                        "evaluated_ledger_version": evaluated_ledger_version,
                        "is_synthetic_fallback": bool(lab_context.is_synthetic_fallback),
                    },
                    commit=False,
                )

                now_utc = self._now_utc()
                # Authoritative execution log report
                self.report_repo.create_next_version(
                    cycle_id=cycle_id,
                    model=self.gateway.pro_model,
                    report_type=PRIMARY_CONSILIUM_EXECUTION_LOG_REPORT_TYPE,
                    privacy_level=PrivacyLevel.SERVER_ONLY,
                    content={
                        "completed_at_utc": now_utc.isoformat(),
                        "lease_owner_id": lease_owner_id,
                        "idempotency_key": idempotency_key,
                        "evaluated_ledger_version": evaluated_ledger_version,
                        "report_date": resolved_date,
                    },
                    created_at=now_utc,
                    commit=False,
                )

                client_system_reports: Dict[str, ClientModelReportResponse] = {}
                for system_key in SYSTEM_KEYS:
                    verified_summary = final_user_system_texts[system_key]
                    system_title = f"{SYSTEM_TITLES[system_key]} Overview"
                    saved_sys_report = self.report_repo.create_next_version(
                        cycle_id=cycle_id,
                        model=self.gateway.flash_model,
                        report_type=f"system_user_report_{system_key}",
                        privacy_level=PrivacyLevel.CLIENT_FACING,
                        content={
                            "title": system_title,
                            "summary": verified_summary,
                            "report_date": resolved_date,
                            "system_key": system_key,
                            "has_changes": True,
                            "entries": [
                                {
                                    "date": resolved_date,
                                    "summary": verified_summary,
                                    "has_changes": True,
                                }
                            ],
                        },
                        commit=False,
                    )
                    client_system_reports[system_key] = ClientModelReportResponse.model_validate(
                        saved_sys_report
                    )

                saved_overall_report = self.report_repo.create_next_version(
                    cycle_id=cycle_id,
                    model=self.gateway.pro_model,
                    report_type="user_overall_report",
                    privacy_level=PrivacyLevel.CLIENT_FACING,
                    content={
                        "title": pro_output.user_overall_report.title,
                        "summary": pro_output.user_overall_report.summary,
                        "report_date": resolved_date,
                        "highlights": list(pro_output.user_overall_report.highlights),
                        "metrics": {
                            "min_pessimum_bpm": pro_output.vitals_thresholds.min_pessimum_bpm,
                            "max_pessimum_bpm": pro_output.vitals_thresholds.max_pessimum_bpm,
                            "max_spike_delta_bpm": pro_output.vitals_thresholds.max_spike_delta_bpm,
                        },
                    },
                    commit=False,
                )
                validated_overall = ClientModelReportResponse.model_validate(saved_overall_report)

                self.db.commit()
            except Exception:
                self.db.rollback()
                self._release_cross_process_lease_safe(cycle_id, lease_owner_id)
                raise

        return PrimaryConsiliumClientResponse(
            cycle_id=cycle_id,
            report_date=resolved_date,
            overall_report=validated_overall,
            system_reports=client_system_reports,
            vitals_thresholds=ClientVitalsThresholds(
                min_pessimum_bpm=pro_output.vitals_thresholds.min_pessimum_bpm,
                max_pessimum_bpm=pro_output.vitals_thresholds.max_pessimum_bpm,
                max_spike_delta_bpm=pro_output.vitals_thresholds.max_spike_delta_bpm,
            ),
        )
