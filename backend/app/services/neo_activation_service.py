"""
Stage 8B.2C — Atomic Neo Consilium, Strategy & Weekly T0 Activation Service.

Implements the authoritative, zero-LLM server transaction for transitioning a prepared
Neo Consilium cycle into active service:
1. Validates all 12 mandatory activation pre-conditions before altering state.
2. Atomically transitions cycle status 'pending_reassessment' -> 'active'.
3. Finalizes `neo_consilium_execution_log` ('activated', 'is_activated': True).
4. Updates `chief_physician_internal` ('is_activated': True).
5. Publishes authoritative `athena_active_strategy` (SERVER_ONLY) from Chief Pro shelves.
6. Establishes immutable Weekly schedule anchor T0 at the exact moment of activation.
7. Durably persists `neo_consilium_activation_audit` report (SERVER_ONLY).
8. Idempotently enqueues post-Primary Athena accompaniment (`post_primary_init`).
9. Provides crash-proof idempotent replays and rollback guarantees.
"""

from datetime import datetime, timezone, timedelta
from typing import Any, Callable, Dict, List, Optional
import logging

from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified
from sqlalchemy import select

from app.models.medical_cycle import MedicalCycle
from app.models.model_report import ModelReport
from app.repositories.cycle_repository import MedicalCycleRepository
from app.repositories.report_repository import ModelReportRepository
from app.schemas.privacy import PrivacyLevel
from app.schemas.reports import ClientReportContent
from app.schemas.consilium import (
    AegisAgentRole,
    ConsiliumMode,
    NeoActivationResponse,
)
from app.services.gemini_gateway import GeminiGateway
from app.services.primary_consilium_service import (
    ConsiliumError,
    ConsiliumCycleNotFoundError,
    PrimaryConsiliumService,
    NEO_CONSILIUM_EXECUTION_LOG_REPORT_TYPE,
    NEO_CYCLE_LINEAGE_REPORT_TYPE,
    SYSTEM_KEYS,
)
from app.services.weekly_consilium_service import (
    WeeklyConsiliumService,
    ATHENA_ACTIVE_STRATEGY_REPORT_TYPE,
    ATHENA_SCHEDULE_ANCHOR_REPORT_TYPE,
)
from app.services.clinical_automation_service import ClinicalAutomationService

logger = logging.getLogger(__name__)

NEO_ACTIVATION_AUDIT_REPORT_TYPE = "neo_consilium_activation_audit"


class NeoActivationError(ConsiliumError):
    """Base error for Neo Consilium activation failures."""
    pass


class NeoActivationPreconditionError(NeoActivationError):
    """Raised when one of the 12 mandatory activation pre-conditions fails."""
    pass


class NeoActivationCycleNotFoundError(NeoActivationError, ConsiliumCycleNotFoundError):
    """Raised when the specified medical cycle does not exist."""
    pass


class NeoActivationAccessDeniedError(NeoActivationError):
    """Raised when an unauthorized patient attempts to activate a cycle."""
    pass


class NeoConsiliumActivationService:
    """
    Dedicated server service executing atomic activation of prepared Neo Consilium.
    Guarantees ZERO additional LLM calls during the activation transaction.
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
        self.weekly_service = WeeklyConsiliumService(
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

    def activate_prepared_neo(
        self,
        cycle_id: str,
        patient_id: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> NeoActivationResponse:
        """
        Atomically activates a prepared Neo Consilium for `cycle_id`.
        Validates all 12 pre-conditions, transitions the cycle to 'active',
        publishes the single authoritative Athena strategy, sets T0 anchor,
        and enqueues `post_primary_init` for Athena greeting.

        Guarantees:
        - ZERO Gemini LLM calls within the activation transaction.
        - Strict idempotency: repeat calls return existing activation state without
          altering T0 or duplicating jobs.
        - Full rollback on any database or validation failure.
        """
        clean_cycle_id = (cycle_id or "").strip()
        if not clean_cycle_id:
            raise NeoActivationCycleNotFoundError("Cycle identifier cannot be empty.")

        now_utc = self._now_utc()

        # Step 1: Pre-Condition Validation
        validation_ctx = self._validate_all_preconditions(clean_cycle_id, patient_id)

        # Step 2: Idempotent Replay Handling
        if validation_ctx["is_already_activated"]:
            logger.info(
                f"[NeoActivation] Cycle '{clean_cycle_id}' is already activated. Replaying existing state."
            )
            return self._build_idempotent_replay_response(clean_cycle_id, validation_ctx)

        # Step 3: Atomic Transaction Under Distributed Lease
        lease_owner_id = self.primary_service._acquire_cross_process_lease(
            cycle_id=clean_cycle_id,
            idempotency_key=idempotency_key,
        )

        try:
            # Re-fetch fresh state inside lease to guard against race conditions
            fresh_cycle = self.cycle_repo.get_fresh(clean_cycle_id)
            if fresh_cycle is None:
                raise NeoActivationCycleNotFoundError(f"Medical cycle '{clean_cycle_id}' not found.")

            fresh_exec_logs = self.report_repo.list_server_only(
                cycle_id=clean_cycle_id,
                report_type=NEO_CONSILIUM_EXECUTION_LOG_REPORT_TYPE,
            )
            if fresh_exec_logs and (fresh_exec_logs[0].content or {}).get("is_activated"):
                # Another worker completed activation while lease was pending
                return self._build_idempotent_replay_response(clean_cycle_id, validation_ctx)

            # 3.1 Transition cycle status
            fresh_cycle.status = "active"
            fresh_cycle.updated_at = now_utc

            # 3.2 Update neo_consilium_execution_log
            log_row = fresh_exec_logs[0]
            upd_log = dict(log_row.content or {})
            upd_log["status"] = "activated"
            upd_log["is_activated"] = True
            upd_log["activated_at_utc"] = now_utc.isoformat()
            log_row.content = upd_log
            flag_modified(log_row, "content")

            # 3.3 Update chief_physician_internal
            chief_row = validation_ctx["chief_report"]
            upd_chief = dict(chief_row.content or {})
            upd_chief["is_activated"] = True
            upd_chief["activated_at_utc"] = now_utc.isoformat()
            chief_row.content = upd_chief
            flag_modified(chief_row, "content")

            # 3.4 Publish authoritative athena_active_strategy (SERVER_ONLY)
            existing_strat = self.report_repo.list_server_only(
                cycle_id=clean_cycle_id,
                report_type=ATHENA_ACTIVE_STRATEGY_REPORT_TYPE,
            )
            chief_content = validation_ctx["chief_content"]
            deputy_content = validation_ctx["deputy_content"]
            if not existing_strat:
                self.report_repo.create_next_version(
                    cycle_id=clean_cycle_id,
                    model=self.gateway.pro_model,
                    report_type=ATHENA_ACTIVE_STRATEGY_REPORT_TYPE,
                    privacy_level=PrivacyLevel.SERVER_ONLY,
                    content={
                        "agent_role": AegisAgentRole.CHIEF_PHYSICIAN.value,
                        "source_consilium": "neo_consilium",
                        "strategy_status": "activated",
                        "strategy_version": 1,
                        "active_strategy_text": chief_content.get("pro_general_shelf"),
                        "clinical_background_text": chief_content.get("pro_training_shelf"),
                        "deputy_synthesis_text": deputy_content.get("flash_general_shelf")
                        or deputy_content.get("cross_system_synthesis", ""),
                        "daily_question_limit": 3,
                        "activated_at_utc": now_utc.isoformat(),
                    },
                    commit=False,
                )
                strat_ver = 1
            else:
                strat_ver = existing_strat[0].version

            # 3.5 Record schedule anchor T0 at the exact moment of activation
            self.weekly_service.record_schedule_anchor(
                cycle_id=clean_cycle_id,
                anchor_dt=now_utc,
                source="neo_activation_t0",
                commit=False,
            )

            # 3.6 Create audit report
            self.report_repo.create_next_version(
                cycle_id=clean_cycle_id,
                model=self.gateway.pro_model,
                report_type=NEO_ACTIVATION_AUDIT_REPORT_TYPE,
                privacy_level=PrivacyLevel.SERVER_ONLY,
                content={
                    "cycle_id": clean_cycle_id,
                    "patient_id": fresh_cycle.patient_id,
                    "activated_at_utc": now_utc.isoformat(),
                    "idempotency_key": idempotency_key,
                    "anchor_t0_utc": now_utc.isoformat(),
                    "weekly_consilium_due_utc": (now_utc + timedelta(hours=168)).isoformat(),
                    "prior_cycle_ids": validation_ctx["prior_cycle_ids"],
                },
                commit=False,
            )

            # 3.7 Transactional Outbox: Enqueue post_primary_init atomically in the same SQLite transaction
            post_primary_job = self.automation_service.enqueue_post_primary_init(
                cycle_id=clean_cycle_id,
                patient_id=fresh_cycle.patient_id,
                commit=False,
            )

            # Single atomic commit for all status/strategy/audit and post_primary_init job
            self.db.commit()
            logger.info(
                f"[NeoActivation] Successfully activated Neo cycle '{clean_cycle_id}' for patient '{fresh_cycle.patient_id}'."
            )
        except Exception:
            self.db.rollback()
            raise
        finally:
            self.primary_service._release_cross_process_lease_safe(clean_cycle_id, lease_owner_id)

        # Step 4: Ensure job handle is returned (fallback enqueue if needed)
        if post_primary_job is None:
            post_primary_job = self.automation_service.enqueue_post_primary_init(
                cycle_id=clean_cycle_id,
                patient_id=validation_ctx["cycle"].patient_id,
            )

        return NeoActivationResponse(
            success=True,
            cycle_id=clean_cycle_id,
            status="active",
            activated_at_utc=now_utc.isoformat(),
            strategy_version=strat_ver,
            schedule_anchor_t0_utc=now_utc.isoformat(),
            next_weekly_consilium_due_utc=(now_utc + timedelta(hours=168)).isoformat(),
            post_primary_init_enqueued=post_primary_job is not None,
            is_idempotent_replay=False,
        )

    def _validate_all_preconditions(
        self, cycle_id: str, patient_id: Optional[str]
    ) -> Dict[str, Any]:
        """
        Validates all 12 mandatory activation conditions prior to initiating the transaction.
        Raises specific `NeoActivationError` subclasses upon any discrepancy.
        """
        # Condition 1: Medical cycle exists and patient ownership
        cycle = self.cycle_repo.get(cycle_id)
        if cycle is None:
            raise NeoActivationCycleNotFoundError(f"Medical cycle '{cycle_id}' not found.")

        if patient_id is not None:
            clean_patient = patient_id.strip()
            if clean_patient and cycle.patient_id != clean_patient:
                raise NeoActivationAccessDeniedError(
                    f"Medical cycle '{cycle_id}' does not belong to authenticated patient '{clean_patient}'."
                )

        # Condition 2: Cycle status must be pending_reassessment (or already active if activated)
        if cycle.status not in ("pending_reassessment", "active"):
            raise NeoActivationPreconditionError(
                f"Cycle '{cycle_id}' has invalid status '{cycle.status}' for Neo activation (expected 'pending_reassessment')."
            )

        # Condition 3: Valid neo_cycle_lineage report exists
        lineage_reports = self.report_repo.list_server_only(
            cycle_id=cycle_id,
            report_type=NEO_CYCLE_LINEAGE_REPORT_TYPE,
        )
        if not lineage_reports:
            raise NeoActivationPreconditionError(
                f"Cycle '{cycle_id}' lacks mandatory lineage report '{NEO_CYCLE_LINEAGE_REPORT_TYPE}'."
            )
        lineage_content = lineage_reports[0].content or {}
        prior_cycle_ids = lineage_content.get("prior_cycle_ids") or []
        if not prior_cycle_ids and lineage_content.get("previous_cycle_id"):
            prior_cycle_ids = [lineage_content.get("previous_cycle_id")]

        # Condition 4: Previous cycle is closed
        if not prior_cycle_ids:
            raise NeoActivationPreconditionError(
                f"Cycle '{cycle_id}' lineage has no prior cycle references."
            )
        for prior_id in prior_cycle_ids:
            prior_cycle = self.cycle_repo.get(prior_id)
            if prior_cycle and prior_cycle.status not in ("completed", "archived"):
                raise NeoActivationPreconditionError(
                    f"Previous cycle '{prior_id}' is not closed (current status: '{prior_cycle.status}')."
                )

        # Condition 5: Neo Consilium execution log exists
        exec_logs = self.report_repo.list_server_only(
            cycle_id=cycle_id,
            report_type=NEO_CONSILIUM_EXECUTION_LOG_REPORT_TYPE,
        )
        if not exec_logs:
            raise NeoActivationPreconditionError(
                f"Cycle '{cycle_id}' lacks Neo Consilium execution log '{NEO_CONSILIUM_EXECUTION_LOG_REPORT_TYPE}'."
            )
        exec_content = exec_logs[0].content or {}

        # Condition 6: Neo Consilium is prepared
        if not exec_content.get("is_prepared") or exec_content.get("status") not in ("prepared", "activated"):
            raise NeoActivationPreconditionError(
                f"Neo Consilium for cycle '{cycle_id}' is not in prepared status (status: '{exec_content.get('status')}')."
            )

        # Condition 7: Already activated check
        is_already_activated = bool(exec_content.get("is_activated", False)) or exec_content.get("status") == "activated"
        if cycle.status == "active" and not is_already_activated:
            raise NeoActivationPreconditionError(
                f"Cycle '{cycle_id}' is active but Neo Consilium was never marked activated."
            )

        # Condition 8: 8 specialists, Deputy, and Chief reports exist under cycle_id
        for sk in SYSTEM_KEYS:
            spec_reps = self.report_repo.list_server_only(
                cycle_id=cycle_id,
                report_type=f"system_specialist_{sk}",
            )
            if not spec_reps:
                raise NeoActivationPreconditionError(
                    f"Missing specialist server report for '{sk}' in cycle '{cycle_id}'."
                )

        deputy_reps = self.report_repo.list_server_only(
            cycle_id=cycle_id,
            report_type="deputy_chief_physician_internal",
        )
        if not deputy_reps:
            raise NeoActivationPreconditionError(
                f"Missing deputy chief internal report in cycle '{cycle_id}'."
            )
        deputy_content = deputy_reps[0].content or {}

        chief_reps = self.report_repo.list_server_only(
            cycle_id=cycle_id,
            report_type="chief_physician_internal",
        )
        if not chief_reps:
            raise NeoActivationPreconditionError(
                f"Missing chief physician internal report in cycle '{cycle_id}'."
            )
        chief_content = chief_reps[0].content or {}

        # Condition 9: Pro Quality Gate approved publication
        quality_review = chief_content.get("flash_quality_review") or {}
        if not quality_review.get("approved_for_publication"):
            raise NeoActivationPreconditionError(
                f"Chief Physician Pro quality gate rejected publication for cycle '{cycle_id}'."
            )

        # Condition 10: All 9 client reports exist and validate against ClientReportContent(extra="forbid")
        for sk in SYSTEM_KEYS:
            sys_user_reps = self.report_repo.list_client_facing(
                cycle_id=cycle_id,
                report_type=f"system_user_report_{sk}",
            )
            if not sys_user_reps:
                raise NeoActivationPreconditionError(
                    f"Missing client system report for '{sk}' in cycle '{cycle_id}'."
                )
            try:
                ClientReportContent.model_validate(sys_user_reps[0].content)
            except Exception as exc:
                raise NeoActivationPreconditionError(
                    f"Client system report for '{sk}' failed strict schema validation: {exc}"
                ) from exc

        overall_reps = self.report_repo.list_client_facing(
            cycle_id=cycle_id,
            report_type="user_overall_report",
        )
        if not overall_reps:
            raise NeoActivationPreconditionError(
                f"Missing client user_overall_report in cycle '{cycle_id}'."
            )
        try:
            ClientReportContent.model_validate(overall_reps[0].content)
        except Exception as exc:
            raise NeoActivationPreconditionError(
                f"Client user_overall_report failed strict schema validation: {exc}"
            ) from exc
        overall_content = overall_reps[0].content or {}

        # Condition 11: Authoritative Chief Physician Pro strategy and vitals thresholds
        shelf = chief_content.get("pro_general_shelf")
        if not shelf or not str(shelf).strip():
            raise NeoActivationPreconditionError(
                f"Chief Physician Pro strategy ('pro_general_shelf') is missing or empty in cycle '{cycle_id}'."
            )

        # Authoritative internal source: chief_physician_internal.vitals_thresholds
        vt = chief_content.get("vitals_thresholds") or {}
        vt_min = vt.get("min_pessimum_bpm")
        vt_max = vt.get("max_pessimum_bpm")
        vt_spike = vt.get("max_spike_delta_bpm")

        if vt_min is None or vt_max is None or vt_spike is None:
            raise NeoActivationPreconditionError(
                f"Authoritative vitals thresholds missing from chief_physician_internal in cycle '{cycle_id}'."
            )
        try:
            min_bpm_int = int(vt_min)
            max_bpm_int = int(vt_max)
            spike_bpm_int = int(vt_spike)
        except (ValueError, TypeError) as exc:
            raise NeoActivationPreconditionError(
                f"Invalid vitals thresholds numeric types in chief_physician_internal: {exc}"
            ) from exc

        if not (30 <= min_bpm_int < max_bpm_int <= 220) or spike_bpm_int <= 0:
            raise NeoActivationPreconditionError(
                f"Vitals thresholds out of bounds in chief_physician_internal: min={min_bpm_int}, max={max_bpm_int}, spike={spike_bpm_int}"
            )

        # Verification of equivalence: user_overall_report.metrics must match chief_physician_internal
        metrics = overall_content.get("metrics") or {}
        m_min = metrics.get("min_pessimum_bpm")
        m_max = metrics.get("max_pessimum_bpm")
        m_spike = metrics.get("max_spike_delta_bpm")

        if m_min is None or m_max is None or m_spike is None:
            raise NeoActivationPreconditionError(
                f"Vitals thresholds missing from user_overall_report metrics in cycle '{cycle_id}'."
            )
        try:
            m_min_int = int(m_min)
            m_max_int = int(m_max)
            m_spike_int = int(m_spike)
        except (ValueError, TypeError) as exc:
            raise NeoActivationPreconditionError(
                f"Invalid vitals thresholds numeric types in user_overall_report: {exc}"
            ) from exc

        if (m_min_int != min_bpm_int) or (m_max_int != max_bpm_int) or (m_spike_int != spike_bpm_int):
            raise NeoActivationPreconditionError(
                f"Pro vitals thresholds discrepancy between authoritative internal result "
                f"({min_bpm_int}/{max_bpm_int}/{spike_bpm_int}) and client metrics "
                f"({m_min_int}/{m_max_int}/{m_spike_int}) in cycle '{cycle_id}'."
            )

        # Condition 12: No other active cycle exists for this patient
        other_active_cycles = self.db.execute(
            select(MedicalCycle).where(
                MedicalCycle.patient_id == cycle.patient_id,
                MedicalCycle.status == "active",
                MedicalCycle.cycle_id != cycle.cycle_id,
            )
        ).scalars().all()
        if other_active_cycles:
            raise NeoActivationPreconditionError(
                f"Another medical cycle '{other_active_cycles[0].cycle_id}' is already active for patient '{cycle.patient_id}'."
            )

        return {
            "cycle": cycle,
            "prior_cycle_ids": prior_cycle_ids,
            "exec_log": exec_logs[0],
            "exec_content": exec_content,
            "is_already_activated": is_already_activated,
            "chief_report": chief_reps[0],
            "chief_content": chief_content,
            "deputy_content": deputy_content,
            "overall_content": overall_content,
        }

    def _build_idempotent_replay_response(
        self, cycle_id: str, validation_ctx: Dict[str, Any]
    ) -> NeoActivationResponse:
        """
        Builds and returns the idempotent response for an already activated Neo cycle.
        Preserves original T0 anchor and ensures post_primary_init job is queued.
        """
        cycle = validation_ctx["cycle"]
        exec_content = validation_ctx["exec_content"]

        anchor_dt = self.weekly_service.find_schedule_anchor_timestamp(cycle_id)
        if anchor_dt is not None:
            anchor_iso = anchor_dt.isoformat()
            due_iso = (anchor_dt + timedelta(hours=168)).isoformat()
        else:
            now_iso = self._now_utc().isoformat()
            anchor_iso = exec_content.get("activated_at_utc", now_iso)
            due_iso = (self._now_utc() + timedelta(hours=168)).isoformat()

        strat_reps = self.report_repo.list_server_only(
            cycle_id=cycle_id,
            report_type=ATHENA_ACTIVE_STRATEGY_REPORT_TYPE,
        )
        strat_ver = strat_reps[0].version if strat_reps else 1

        post_primary_job = self.automation_service.enqueue_post_primary_init(
            cycle_id=cycle_id,
            patient_id=cycle.patient_id,
        )

        return NeoActivationResponse(
            success=True,
            cycle_id=cycle_id,
            status=cycle.status,
            activated_at_utc=exec_content.get("activated_at_utc", anchor_iso),
            strategy_version=strat_ver,
            schedule_anchor_t0_utc=anchor_iso,
            next_weekly_consilium_due_utc=due_iso,
            post_primary_init_enqueued=post_primary_job is not None,
            is_idempotent_replay=True,
        )
