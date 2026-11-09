from datetime import datetime, timezone
import time
from typing import Callable, Optional
import uuid

from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.models.medical_cycle import MedicalCycle
from app.models.patient import Patient
from app.repositories.cycle_repository import MedicalCycleRepository
from app.repositories.report_repository import ModelReportRepository
from app.schemas.cycle import StartNeoReassessmentResponse
from app.schemas.privacy import PrivacyLevel
from app.services.clinical_automation_service import ClinicalAutomationService
from app.services.gemini_gateway import GeminiGateway
from app.services.primary_consilium_service import PrimaryConsiliumService

NEO_CYCLE_LINEAGE_REPORT_TYPE = "neo_cycle_lineage"
NEO_LIFECYCLE_MODEL = "aegis-neo-lifecycle"


class NeoReassessmentNotAllowedError(ValueError):
    """
    Raised when client attempts to start Neo reassessment without completing
    the existing cycle's Primary Consilium or without an active cycle to transition.
    Subclasses ValueError.
    """


class NeoCycleService:
    """
    Server-side orchestrator for safe Neo cycle transition and historical preservation (Stage 8B.1).
    Invariants:
    1. Zero Patient History Deletion: old cycles, reports, labs, ECGs, and vitals remain immutable.
    2. Atomic DB Transition: closing old cycle, cancelling pending jobs, invalidating referral
       reminders, provisioning new cycle ('pending_reassessment'), and storing lineage record
       occur inside a single database transaction.
    3. Idempotent: repeated or reconnected calls return existing pending cycle without duplicating.
    4. Guardrails: Neo transition is forbidden unless Primary Consilium was completed on the current cycle.
    5. Fencing: in-flight executions on the old cycle are fenced and prevented from committing stale state.
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
        self.cycle_repo = MedicalCycleRepository(db)
        self.report_repo = ModelReportRepository(db)
        self.primary_service = PrimaryConsiliumService(
            db=db, gateway=self.gateway, now_provider=self.now_provider
        )
        self.automation_service = ClinicalAutomationService(
            db=db, gateway=self.gateway, now_provider=self.now_provider
        )

    def _now_utc(self) -> datetime:
        dt = self.now_provider()
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)

    def start_neo_reassessment(
        self,
        patient_id: str,
        cycle_id_factory: Optional[Callable[[], str]] = None,
    ) -> StartNeoReassessmentResponse:
        """
        Atomically transitions the authenticated patient to a new Neo reassessment cycle.
        """
        if not isinstance(patient_id, str) or not patient_id.strip():
            raise NeoReassessmentNotAllowedError("Patient ID must not be empty.")

        clean_patient_id = patient_id.strip()

        # 1. Validate patient existence
        patient = self.db.query(Patient).filter_by(patient_id=clean_patient_id).first()
        if patient is None:
            raise NeoReassessmentNotAllowedError(
                f"Patient '{clean_patient_id}' not found."
            )

        # 2. Idempotency Check: if patient already has a pending_reassessment cycle, return it cleanly
        existing_pending = self.cycle_repo.get_pending_reassessment_for_patient(clean_patient_id)
        if existing_pending is not None:
            lineage_reports = self.report_repo.list_server_only(
                cycle_id=existing_pending.cycle_id,
                report_type=NEO_CYCLE_LINEAGE_REPORT_TYPE,
            )
            prev_id = ""
            if lineage_reports and lineage_reports[0].content:
                prev_id = str(lineage_reports[0].content.get("previous_cycle_id") or "")
            if not prev_id:
                completed = [
                    c for c in self.cycle_repo.list_by_patient(clean_patient_id)
                    if c.status == "completed" and c.cycle_id != existing_pending.cycle_id
                ]
                prev_id = completed[0].cycle_id if completed else ""

            created_iso = (
                existing_pending.created_at.replace(tzinfo=timezone.utc).isoformat()
                if existing_pending.created_at.tzinfo is None
                else existing_pending.created_at.isoformat()
            )
            return StartNeoReassessmentResponse(
                patient_id=clean_patient_id,
                cycle_id=existing_pending.cycle_id,
                previous_cycle_id=prev_id,
                status=existing_pending.status,
                reassessment_status="pending_reassessment",
                is_pending_reassessment=True,
                created_at_utc=created_iso,
                message="Existing pending Neo reassessment cycle recovered idempotently.",
            )

        # 3. Find current active cycle
        active_cycle = self.cycle_repo.get_active_for_patient(
            clean_patient_id, include_pending_reassessment=False
        )
        if active_cycle is None:
            raise NeoReassessmentNotAllowedError(
                f"No active medical cycle found to transition from for patient '{clean_patient_id}'."
            )

        # 4. Enforce completed Primary Consilium guardrail
        if not self.primary_service.is_primary_consilium_completed(active_cycle.cycle_id):
            raise NeoReassessmentNotAllowedError(
                f"Cannot start Neo reassessment for cycle '{active_cycle.cycle_id}': "
                "Primary Consilium must be completed before initiating a new reassessment cycle."
            )

        # 5. Atomic write serialization loop (SQLite BEGIN IMMEDIATE with progressive backoff)
        max_lock_retries = 15
        for attempt in range(max_lock_retries + 1):
            try:
                bind = self.db.get_bind()
                is_sqlite = (bind.dialect.name == "sqlite") if bind is not None else False
                if is_sqlite:
                    if self.db.in_transaction() and not self.db.dirty and not self.db.new:
                        self.db.rollback()
                    self.db.execute(text("BEGIN IMMEDIATE"))

                # Expire session cache under write lock so queries re-read fresh committed data
                self.db.expire_all()

                # Re-check under write lock in case of concurrent execution
                recheck_pending = self.cycle_repo.get_pending_reassessment_for_patient(clean_patient_id)
                if recheck_pending is not None:
                    self.db.commit()
                    return self.start_neo_reassessment(clean_patient_id, cycle_id_factory)

                recheck_active = self.cycle_repo.get_active_for_patient(
                    clean_patient_id, include_pending_reassessment=False
                )
                if recheck_active is None:
                    self.db.rollback()
                    raise NeoReassessmentNotAllowedError(
                        f"Active cycle vanished during transition for patient '{clean_patient_id}'."
                    )

                now = self._now_utc()
                now_iso = now.isoformat()
                old_cycle_id = recheck_active.cycle_id

                # A. Close old cycle
                recheck_active.status = "completed"
                recheck_active.closed_at = now
                recheck_active.updated_at = now

                # B. Cancel all pending and retryable jobs on old cycle
                self.automation_service.cancel_all_pending_jobs_for_cycle(
                    cycle_id=old_cycle_id,
                    reason="neo_cycle_transition",
                    commit=False,
                )

                # C. Invalidate 168h referral reminders on old cycle
                self.automation_service.cancel_pending_referral_reminders(
                    cycle_id=old_cycle_id,
                    reason="neo_cycle_transition",
                    commit=False,
                )

                # D. Provision new cycle in pending_reassessment status
                if cycle_id_factory is not None:
                    new_cycle_id = cycle_id_factory()
                else:
                    new_cycle_id = f"cycle_{uuid.uuid4().hex[:16]}"

                new_cycle = MedicalCycle(
                    cycle_id=new_cycle_id,
                    patient_id=clean_patient_id,
                    status="pending_reassessment",
                    started_at=now,
                    created_at=now,
                    updated_at=now,
                )
                self.db.add(new_cycle)

                # E. Persist neo_cycle_lineage report (SERVER_ONLY)
                lineage_content = {
                    "event_type": "neo_cycle_transition",
                    "patient_id": clean_patient_id,
                    "new_cycle_id": new_cycle_id,
                    "previous_cycle_id": old_cycle_id,
                    "transitioned_at_utc": now_iso,
                    "reason": "patient_initiated_neo_reassessment",
                    "previous_cycle_status": "completed",
                    "new_cycle_status": "pending_reassessment",
                }
                self.report_repo.create_next_version(
                    cycle_id=new_cycle_id,
                    model=NEO_LIFECYCLE_MODEL,
                    report_type=NEO_CYCLE_LINEAGE_REPORT_TYPE,
                    privacy_level=PrivacyLevel.SERVER_ONLY,
                    content=lineage_content,
                    created_at=now,
                    commit=False,
                )

                # Commit entire transition atomically
                self.db.commit()
                self.db.refresh(new_cycle)

                return StartNeoReassessmentResponse(
                    patient_id=clean_patient_id,
                    cycle_id=new_cycle_id,
                    previous_cycle_id=old_cycle_id,
                    status="pending_reassessment",
                    reassessment_status="pending_reassessment",
                    is_pending_reassessment=True,
                    created_at_utc=now_iso,
                    message="Neo reassessment cycle successfully provisioned and linked to previous cycle.",
                )
            except OperationalError as exc:
                self.db.rollback()
                err_msg = str(exc).lower()
                if attempt < max_lock_retries and (
                    "cannot start a transaction within a transaction" in err_msg
                    or "locked" in err_msg
                    or "busy" in err_msg
                ):
                    time.sleep(0.02 * (attempt + 1))
                    continue
                raise
            except Exception:
                self.db.rollback()
                # If another concurrent request completed in parallel, recover cleanly
                recheck_pending = self.cycle_repo.get_pending_reassessment_for_patient(clean_patient_id)
                if recheck_pending is not None:
                    return self.start_neo_reassessment(clean_patient_id, cycle_id_factory)
                raise

        raise RuntimeError(
            f"Failed to start Neo reassessment for patient '{clean_patient_id}' after retries."
        )
