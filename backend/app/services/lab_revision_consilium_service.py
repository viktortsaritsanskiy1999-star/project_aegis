import asyncio
from datetime import datetime, timedelta, timezone
import json
import math
import re
import threading
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional
import uuid
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from app.models.model_report import ModelReport
from app.repositories.cycle_repository import MedicalCycleRepository
from app.repositories.measurement_repository import HealthMeasurementRepository
from app.repositories.report_repository import (
    ModelReportRepository,
    ReportVersionConflictError,
)
from app.schemas.consilium import (
    SYSTEM_KEYS,
    SYSTEM_TITLES,
    LAB_REVISION_COOLDOWN_HOURS,
    MEDICAL_SAFETY_COOLDOWN_NOTICE,
    AegisAgentRole,
    AnamnesisModelOutput,
    SystemAgentOutput,
    LabEventType,
    LabReferenceRange,
    LabObservationEntry,
    LabMarkerMutationItem,
    LabLedgerStateResponse,
    LabDiffItem,
    LaboratoryDiffReport,
    LabRevisionFlashMasterOutput,
    ProMasterOutput,
    ClientVitalsThresholds,
    LabRevisionCooldownStatus,
    LabRevisionConsiliumClientResponse,
    validate_lab_revision_unchanged_explanation,
)
from app.schemas.privacy import PrivacyLevel
from app.schemas.reports import ClientModelReportResponse
from app.services.consilium_prompts import (
    build_lab_revision_system_agent_prompt,
    build_lab_revision_flash_master_prompt,
    build_lab_revision_pro_master_prompt,
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
from app.services.weekly_consilium_service import (
    ATHENA_ACTIVE_STRATEGY_REPORT_TYPE,
    WEEKLY_DEPUTY_INTERNAL_REPORT_TYPE,
    WEEKLY_CHIEF_INTERNAL_REPORT_TYPE,
)


LABORATORY_LEDGER_REPORT_TYPE = "laboratory_ledger"
LABORATORY_DIFF_AUDIT_REPORT_TYPE = "laboratory_diff_audit"
LAB_REVISION_EXECUTION_LOG_REPORT_TYPE = "lab_revision_execution_log"
LAB_REVISION_IN_FLIGHT_LOCK_REPORT_TYPE = "lab_revision_in_flight_lock"
LAB_REVISION_IN_FLIGHT_LOCK_TTL_SECONDS = 900
LAB_REVISION_IN_FLIGHT_LOCK_HEARTBEAT_SECONDS = 300.0

_NUMERIC_UNIT_RE = re.compile(
    r"^\s*([+-]?\d+(?:[.,]\d+)?)\s*([a-zA-Z%/\u03bc\u00b5\d\s\-\^]*)\s*$"
)


class ConsiliumCooldownError(ConsiliumError):
    """
    Raised when a user-initiated Full Lab-Revision Consilium is requested before
    the 168-hour (7-day) cooldown has elapsed since the last successful full lab revision
    in the active medical cycle, or when a concurrent run is already in progress.
    """

    def __init__(self, cooldown_status: LabRevisionCooldownStatus):
        self.cooldown_status = cooldown_status
        super().__init__(cooldown_status.message)


class LabRevisionConsiliumService:
    """
    Server-side orchestrator for Stage 3.4 — Lab Revision Consilium:
    1. Versioned laboratory ledger supporting new markers, corrections of prior erroneous
       values (without erasing original entries), and new dated studies, anytime with zero cooldown.
    2. Explicit tracking of unreviewed laboratory changes so old AI conclusions are never
       presented as reflecting unanalyzed new labs, and Weekly Consilium never marks new labs
       as fully evaluated.
    3. Deterministic laboratory diff engine (added, modified, error corrections vs dated studies,
       unit changes, invalidated entries, unchanged markers) saved as a server_only audit record.
    4. Full comparative AI cascade: 8 Gemini Flash-Lite -> Gemini Flash -> Gemini Pro
       with previous conclusions passed to each agent.
    5. Authoritative server-time 168-hour cooldown with two-layer concurrency protection:
       in-process `asyncio.Lock` + cross-process SQLite primary-key execution lease
       (`lab_revision_in_flight_lock`) with owner fencing (`lease_owner_id`) and active renewal.
    """

    _CYCLE_LOCKS: Dict[str, asyncio.Lock] = {}
    _LEDGER_THREAD_LOCKS: Dict[str, threading.RLock] = {}
    _LEDGER_META_LOCK = threading.Lock()

    def __init__(
        self,
        db: Session,
        gateway: Optional[GeminiGateway] = None,
        now_provider: Optional[Callable[[], datetime]] = None,
        lock_registry: Optional[Dict[str, asyncio.Lock]] = None,
        ledger_lock_registry: Optional[Dict[str, threading.RLock]] = None,
        lease_ttl_seconds: int = LAB_REVISION_IN_FLIGHT_LOCK_TTL_SECONDS,
        lease_heartbeat_interval_seconds: float = LAB_REVISION_IN_FLIGHT_LOCK_HEARTBEAT_SECONDS,
    ):
        self.db = db
        self.gateway = gateway or GeminiGateway()
        self.now_provider = now_provider or (lambda: datetime.now(timezone.utc))
        self._lock_registry = lock_registry if lock_registry is not None else self._CYCLE_LOCKS
        self._ledger_lock_registry = (
            ledger_lock_registry
            if ledger_lock_registry is not None
            else self._LEDGER_THREAD_LOCKS
        )
        self.lease_ttl_seconds = max(1, int(lease_ttl_seconds))
        self.lease_heartbeat_interval_seconds = max(0.001, float(lease_heartbeat_interval_seconds))
        self.cycle_repo = MedicalCycleRepository(db)
        self.report_repo = ModelReportRepository(db)
        self.measurement_repo = HealthMeasurementRepository(db)

    def _get_cycle_lock(self, cycle_id: str) -> asyncio.Lock:
        lock = self._lock_registry.get(cycle_id)
        if lock is None:
            lock = asyncio.Lock()
            self._lock_registry[cycle_id] = lock
        return lock

    def _get_ledger_thread_lock(self, cycle_id: str) -> threading.RLock:
        with self._LEDGER_META_LOCK:
            lock = self._ledger_lock_registry.get(cycle_id)
            if lock is None:
                lock = threading.RLock()
                self._ledger_lock_registry[cycle_id] = lock
            return lock

    def _now_utc(self) -> datetime:
        dt = self.now_provider()
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)

    @staticmethod
    def _normalize_marker_key(raw_key: str) -> str:
        cleaned = re.sub(r"[^a-z0-9_]+", "_", raw_key.strip().lower()).strip("_")
        if not cleaned:
            raise ValueError(f"Invalid laboratory marker_key: '{raw_key}'.")
        return cleaned

    @staticmethod
    def _parse_numeric_and_unit(
        raw_value: Any, explicit_unit: Optional[str] = None
    ) -> tuple[Optional[float], Optional[str]]:
        unit = explicit_unit.strip() if isinstance(explicit_unit, str) and explicit_unit.strip() else None
        if isinstance(raw_value, (int, float)) and not isinstance(raw_value, bool):
            return float(raw_value), unit
        if isinstance(raw_value, str):
            m = _NUMERIC_UNIT_RE.match(raw_value.strip())
            if m:
                try:
                    num = float(m.group(1).replace(",", "."))
                    inferred_unit = m.group(2).strip() or None
                    return num, (unit or inferred_unit)
                except ValueError:
                    pass
        return None, unit

    @staticmethod
    def _detect_critical_deviation(
        numeric_value: Optional[float],
        reference_range: Optional[LabReferenceRange],
        explicit_flag: bool = False,
    ) -> bool:
        if explicit_flag:
            return True
        if numeric_value is None or reference_range is None:
            return False
        if reference_range.low is not None and reference_range.low > 0:
            if numeric_value < reference_range.low * 0.5:
                return True
        if reference_range.high is not None and reference_range.high > 0:
            if numeric_value > reference_range.high * 2.0:
                return True
        return False

    # ==========================================================================
    # 1. VERSIONED LABORATORY LEDGER & UNREVIEWED STATUS
    # ==========================================================================

    def _ensure_baseline_ledger_seeded(
        self, cycle_id: str, commit: bool = True
    ) -> Optional[ModelReport]:
        """
        If `laboratory_ledger` has not been created yet for `cycle_id`, seeds v1 from
        the preparatory Vision OCR report (`preparatory_vision_ocr`) if present.
        """
        existing_ledgers = self.report_repo.list_server_only(
            cycle_id=cycle_id,
            report_type=LABORATORY_LEDGER_REPORT_TYPE,
        )
        if existing_ledgers:
            return existing_ledgers[0]

        ocr_reports = self.report_repo.list_server_only(
            cycle_id=cycle_id,
            report_type=PREPARATORY_OCR_REPORT_TYPE,
        )
        if not ocr_reports:
            return None

        ocr_report = ocr_reports[0]
        raw_confirmed_list = (ocr_report.content or {}).get("confirmed_observations")
        raw_table = (ocr_report.content or {}).get("shared_labs_table")
        if not raw_confirmed_list and (not isinstance(raw_table, dict) or not raw_table):
            return None

        recorded_at = (
            ocr_report.created_at.replace(tzinfo=timezone.utc).isoformat()
            if ocr_report.created_at.tzinfo is None
            else ocr_report.created_at.astimezone(timezone.utc).isoformat()
        )
        sample_date_default = recorded_at[:10]

        observations_history: List[Dict[str, Any]] = []
        active_markers: Dict[str, Dict[str, Any]] = {}

        if isinstance(raw_confirmed_list, list) and raw_confirmed_list:
            for idx, raw_entry in enumerate(raw_confirmed_list, start=1):
                marker_key = self._normalize_marker_key(str(raw_entry.get("marker_key", "")))
                display_name = str(raw_entry.get("display_name") or marker_key).strip()
                obs_id = f"obs_{marker_key}_v1_{idx}"
                val = raw_entry.get("value", raw_entry.get("raw_value", ""))
                explicit_unit = raw_entry.get("unit")
                sample_date = raw_entry.get("sample_date") or sample_date_default
                ref_raw = raw_entry.get("reference_range")
                ref_obj = (
                    LabReferenceRange.model_validate(ref_raw)
                    if isinstance(ref_raw, dict)
                    else None
                )
                crit_flag = bool(raw_entry.get("is_critical_flag", False))
                num_val = raw_entry.get("numeric_value")
                if num_val is None:
                    num_val, resolved_unit = self._parse_numeric_and_unit(val, explicit_unit)
                else:
                    resolved_unit = explicit_unit

                prior_active = active_markers.get(marker_key)
                event_type = LabEventType.INITIAL if prior_active is None else LabEventType.NEW_STUDY

                entry = LabObservationEntry(
                    observation_id=obs_id,
                    marker_key=marker_key,
                    display_name=display_name,
                    value=val,
                    numeric_value=num_val,
                    unit=resolved_unit,
                    sample_date=sample_date,
                    reference_range=ref_obj,
                    event_type=event_type,
                    supersedes_observation_id=None,
                    correction_reason=None,
                    is_active=True,
                    is_critical_flag=self._detect_critical_deviation(num_val, ref_obj, crit_flag),
                    recorded_at=recorded_at,
                )
                dumped = entry.model_dump()
                observations_history.append(dumped)
                active_markers[marker_key] = dumped
        else:
            for idx, (raw_key, raw_entry) in enumerate(raw_table.items(), start=1):
                marker_key = self._normalize_marker_key(str(raw_key))
                display_name = str(raw_key).strip()
                obs_id = f"obs_{marker_key}_v1_{idx}"

                if isinstance(raw_entry, dict):
                    val = raw_entry.get("value", raw_entry.get("raw_value", ""))
                    explicit_unit = raw_entry.get("unit")
                    sample_date = raw_entry.get("sample_date") or sample_date_default
                    ref_raw = raw_entry.get("reference_range")
                    ref_obj = (
                        LabReferenceRange.model_validate(ref_raw)
                        if isinstance(ref_raw, dict)
                        else None
                    )
                    crit_flag = bool(raw_entry.get("is_critical_flag", False))
                    num_val, resolved_unit = self._parse_numeric_and_unit(val, explicit_unit)
                else:
                    val = raw_entry
                    num_val, resolved_unit = self._parse_numeric_and_unit(val, None)
                    sample_date = sample_date_default
                    ref_obj = None
                    crit_flag = False

                entry = LabObservationEntry(
                    observation_id=obs_id,
                    marker_key=marker_key,
                    display_name=display_name,
                    value=val,
                    numeric_value=num_val,
                    unit=resolved_unit,
                    sample_date=sample_date,
                    reference_range=ref_obj,
                    event_type=LabEventType.INITIAL,
                    supersedes_observation_id=None,
                    correction_reason=None,
                    is_active=True,
                    is_critical_flag=self._detect_critical_deviation(num_val, ref_obj, crit_flag),
                    recorded_at=recorded_at,
                )
                dumped = entry.model_dump()
                observations_history.append(dumped)
                active_markers[marker_key] = dumped

        shared_labs_table = self._build_shared_labs_table_from_active(active_markers)
        try:
            return self.report_repo.create_next_version(
                cycle_id=cycle_id,
                model="aegis-lab-ledger",
                report_type=LABORATORY_LEDGER_REPORT_TYPE,
                privacy_level=PrivacyLevel.SERVER_ONLY,
                content={
                    "ledger_version": 1,
                    "source": "preparatory_vision_ocr",
                    "active_markers": active_markers,
                    "observations_history": observations_history,
                    "shared_labs_table": shared_labs_table,
                },
                expected_version=1,
                max_retries=0,
                commit=commit,
            )
        except (ReportVersionConflictError, IntegrityError):
            if commit:
                self.db.rollback()
                self.db.expire_all()
                existing_ledgers = self.report_repo.list_server_only(
                    cycle_id=cycle_id,
                    report_type=LABORATORY_LEDGER_REPORT_TYPE,
                )
                if existing_ledgers:
                    return existing_ledgers[0]
            raise

    @staticmethod
    def _build_shared_labs_table_from_active(
        active_markers: Dict[str, Dict[str, Any]]
    ) -> Dict[str, Any]:
        table: Dict[str, Any] = {}
        for key, obs in active_markers.items():
            table[key] = {
                "display_name": obs.get("display_name", key),
                "value": obs.get("value"),
                "numeric_value": obs.get("numeric_value"),
                "unit": obs.get("unit"),
                "sample_date": obs.get("sample_date"),
                "reference_range": obs.get("reference_range"),
                "event_type": obs.get("event_type"),
                "observation_id": obs.get("observation_id"),
            }
        return table

    def _get_last_evaluated_ledger_version(self, cycle_id: str) -> int:
        from app.services.lab_context_service import get_last_evaluated_ledger_version

        return get_last_evaluated_ledger_version(self.db, cycle_id)

    def get_lab_ledger_state(
        self, cycle_id: str, commit: bool = True
    ) -> LabLedgerStateResponse:
        if not self.cycle_repo.get(cycle_id):
            raise ConsiliumCycleNotFoundError(f"Medical cycle '{cycle_id}' not found.")

        latest_ledger_report = (
            self._ensure_baseline_ledger_seeded(cycle_id, commit=False)
            if not commit
            else self._ensure_baseline_ledger_seeded(cycle_id)
        )
        last_evaluated_ver = self._get_last_evaluated_ledger_version(cycle_id)

        if latest_ledger_report is None:
            return LabLedgerStateResponse(
                cycle_id=cycle_id,
                ledger_version=0,
                last_evaluated_ledger_version=last_evaluated_ver,
                has_unreviewed_lab_changes=False,
                evaluation_status="evaluated",
                evaluation_notice="No laboratory markers recorded yet.",
                has_critical_flags=False,
                safety_notice=MEDICAL_SAFETY_COOLDOWN_NOTICE,
                active_markers={},
                observations_history=[],
            )

        content = latest_ledger_report.content or {}
        ledger_version = int(content.get("ledger_version", latest_ledger_report.version))
        raw_active = content.get("active_markers") or {}
        raw_history = content.get("observations_history") or []

        active_markers = {
            k: LabObservationEntry.model_validate(v) for k, v in raw_active.items()
        }
        observations_history = [
            LabObservationEntry.model_validate(item) for item in raw_history
        ]

        has_unreviewed = ledger_version > last_evaluated_ver
        has_critical = any(obs.is_critical_flag for obs in active_markers.values())

        if has_unreviewed:
            evaluation_status = "pending_full_consilium_revision"
            evaluation_notice = (
                f"New or updated laboratory results (ledger v{ledger_version}) are saved on the server "
                f"but have NOT yet been evaluated by a new Full Consilium (last evaluated: v{last_evaluated_ver}). "
                "Existing AI reports reflect only the previously evaluated laboratory state."
            )
            if has_critical:
                evaluation_notice += f" {MEDICAL_SAFETY_COOLDOWN_NOTICE}"
        else:
            evaluation_status = "evaluated"
            evaluation_notice = (
                f"All current laboratory results (ledger v{ledger_version}) have been evaluated by the Full Consilium."
            )

        return LabLedgerStateResponse(
            cycle_id=cycle_id,
            ledger_version=ledger_version,
            last_evaluated_ledger_version=last_evaluated_ver,
            has_unreviewed_lab_changes=has_unreviewed,
            evaluation_status=evaluation_status,
            evaluation_notice=evaluation_notice,
            has_critical_flags=has_critical,
            safety_notice=MEDICAL_SAFETY_COOLDOWN_NOTICE,
            active_markers=active_markers,
            observations_history=observations_history,
        )

    def upsert_laboratory_markers(
        self,
        cycle_id: str,
        mutations: List[LabMarkerMutationItem | Dict[str, Any]],
        max_retries: int = 5,
        commit: bool = True,
    ) -> LabLedgerStateResponse:
        """
        Saves new laboratory markers, new dated studies, corrections of erroneous values,
        or invalidations as a new immutable version of `laboratory_ledger`.
        - Never overwrites or deletes historical observations in place.
        - Corrections mark the prior observation `is_active=False` in history and record
          `supersedes_observation_id`, keeping the original erroneous value intact for audit.
        - New dated studies add a chronological observation without marking prior studies as errors.
        - Always allowed immediately with zero cooldown.
        - Uses per-cycle thread serialization and bounded optimistic read-modify-write retries
          with `expected_version` so concurrent upserts never overwrite each other or lose markers.
        """
        if not mutations:
            raise ValueError("At least one laboratory mutation must be provided.")

        validated_mutations: List[LabMarkerMutationItem] = [
            m if isinstance(m, LabMarkerMutationItem) else LabMarkerMutationItem.model_validate(m)
            for m in mutations
        ]

        effective_retries = max(0, int(max_retries)) if commit else 0
        with self._get_ledger_thread_lock(cycle_id):
            for attempt in range(effective_retries + 1):
                if attempt > 0:
                    self.db.rollback()
                    self.db.expire_all()
                    time.sleep(0.015 * attempt)

                try:
                    cycle = self.cycle_repo.get(cycle_id)
                    if not cycle:
                        raise ConsiliumCycleNotFoundError(f"Medical cycle '{cycle_id}' not found.")

                    current_state = (
                        self.get_lab_ledger_state(cycle_id, commit=False)
                        if not commit
                        else self.get_lab_ledger_state(cycle_id)
                    )
                    next_ledger_ver = current_state.ledger_version + 1
                    now_iso = self._now_utc().isoformat()

                    history_list: List[Dict[str, Any]] = [
                        obs.model_dump() for obs in current_state.observations_history
                    ]
                    active_map: Dict[str, Dict[str, Any]] = {
                        k: obs.model_dump() for k, obs in current_state.active_markers.items()
                    }

                    for idx, mut in enumerate(validated_mutations, start=1):
                        marker_key = self._normalize_marker_key(mut.marker_key)
                        prior_active = active_map.get(marker_key)
                        display_name = (
                            mut.display_name
                            or (prior_active.get("display_name") if prior_active else None)
                            or mut.marker_key.strip()
                        )
                        obs_id = f"obs_{marker_key}_v{next_ledger_ver}_{idx}"

                        if mut.event_type == LabEventType.INVALIDATED:
                            target_prev_id = mut.supersedes_observation_id or (
                                prior_active.get("observation_id") if prior_active else None
                            )
                            if target_prev_id is None:
                                raise ValueError(
                                    f"Cannot invalidate marker '{marker_key}': no active observation found."
                                )
                            for h_item in history_list:
                                if h_item.get("observation_id") == target_prev_id:
                                    h_item["is_active"] = False
                                    h_item["superseded_by_observation_id"] = obs_id
                            active_map.pop(marker_key, None)

                            inv_entry = LabObservationEntry(
                                observation_id=obs_id,
                                marker_key=marker_key,
                                display_name=display_name,
                                value=prior_active.get("value") if prior_active else None,
                                numeric_value=prior_active.get("numeric_value") if prior_active else None,
                                unit=prior_active.get("unit") if prior_active else mut.unit,
                                sample_date=prior_active.get("sample_date") if prior_active else mut.sample_date,
                                reference_range=(
                                    LabReferenceRange.model_validate(prior_active["reference_range"])
                                    if prior_active and prior_active.get("reference_range")
                                    else mut.reference_range
                                ),
                                event_type=LabEventType.INVALIDATED,
                                supersedes_observation_id=target_prev_id,
                                correction_reason=mut.correction_reason or "Marked as erroneous/invalidated",
                                is_active=False,
                                is_critical_flag=False,
                                recorded_at=now_iso,
                            )
                            history_list.append(inv_entry.model_dump())
                            continue

                        if mut.event_type == LabEventType.CORRECTION:
                            target_prev_id = mut.supersedes_observation_id or (
                                prior_active.get("observation_id") if prior_active else None
                            )
                            if target_prev_id is None:
                                raise ValueError(
                                    f"Cannot apply error correction for marker '{marker_key}': no prior observation exists to correct."
                                )
                            # Mark the erroneous observation inactive in history while preserving its original value
                            superseded_obs: Optional[Dict[str, Any]] = None
                            for h_item in history_list:
                                if h_item.get("observation_id") == target_prev_id:
                                    h_item["is_active"] = False
                                    h_item["superseded_by_observation_id"] = obs_id
                                    superseded_obs = h_item
                                    break
                            if superseded_obs is None:
                                raise ValueError(
                                    f"Observation '{target_prev_id}' not found in history for marker '{marker_key}'."
                                )

                            fallback_unit = mut.unit if mut.unit is not None else superseded_obs.get("unit")
                            num_val, resolved_unit = self._parse_numeric_and_unit(mut.value, fallback_unit)
                            resolved_date = mut.sample_date or superseded_obs.get("sample_date") or now_iso[:10]
                            resolved_ref = mut.reference_range or (
                                LabReferenceRange.model_validate(superseded_obs["reference_range"])
                                if superseded_obs.get("reference_range")
                                else None
                            )
                            crit = self._detect_critical_deviation(num_val, resolved_ref, mut.is_critical_flag)

                            corr_entry = LabObservationEntry(
                                observation_id=obs_id,
                                marker_key=marker_key,
                                display_name=display_name,
                                value=mut.value,
                                numeric_value=num_val,
                                unit=resolved_unit,
                                sample_date=resolved_date,
                                reference_range=resolved_ref,
                                event_type=LabEventType.CORRECTION,
                                supersedes_observation_id=target_prev_id,
                                correction_reason=mut.correction_reason or "Correction of prior input/OCR value",
                                is_active=True,
                                is_critical_flag=crit,
                                recorded_at=now_iso,
                            )
                            dumped_corr = corr_entry.model_dump()
                            history_list.append(dumped_corr)
                            active_map[marker_key] = dumped_corr
                            continue

                        # LabEventType.INITIAL or LabEventType.NEW_STUDY
                        # Note: If a prior active study exists on an earlier date, it remains a valid historical
                        # study (NOT marked as an error), while the new study becomes the current active marker.
                        effective_event = (
                            LabEventType.INITIAL if prior_active is None and mut.event_type == LabEventType.INITIAL else (
                                LabEventType.NEW_STUDY if prior_active is not None else mut.event_type
                            )
                        )
                        fallback_unit = mut.unit if mut.unit is not None else (
                            prior_active.get("unit") if prior_active else None
                        )
                        num_val, resolved_unit = self._parse_numeric_and_unit(mut.value, fallback_unit)
                        resolved_date = mut.sample_date or now_iso[:10]
                        resolved_ref = mut.reference_range or (
                            LabReferenceRange.model_validate(prior_active["reference_range"])
                            if prior_active and prior_active.get("reference_range")
                            else None
                        )
                        crit = self._detect_critical_deviation(num_val, resolved_ref, mut.is_critical_flag)

                        new_entry = LabObservationEntry(
                            observation_id=obs_id,
                            marker_key=marker_key,
                            display_name=display_name,
                            value=mut.value,
                            numeric_value=num_val,
                            unit=resolved_unit,
                            sample_date=resolved_date,
                            reference_range=resolved_ref,
                            event_type=effective_event,
                            supersedes_observation_id=None,
                            correction_reason=None,
                            is_active=True,
                            is_critical_flag=crit,
                            recorded_at=now_iso,
                        )
                        dumped_new = new_entry.model_dump()
                        history_list.append(dumped_new)
                        active_map[marker_key] = dumped_new

                    shared_labs_table = self._build_shared_labs_table_from_active(active_map)
                    self.report_repo.create_next_version(
                        cycle_id=cycle_id,
                        model="aegis-lab-ledger",
                        report_type=LABORATORY_LEDGER_REPORT_TYPE,
                        privacy_level=PrivacyLevel.SERVER_ONLY,
                        content={
                            "ledger_version": next_ledger_ver,
                            "source": "user_or_clinical_update",
                            "active_markers": active_map,
                            "observations_history": history_list,
                            "shared_labs_table": shared_labs_table,
                        },
                        expected_version=next_ledger_ver,
                        max_retries=0,
                        commit=commit,
                    )
                    return (
                        self.get_lab_ledger_state(cycle_id, commit=False)
                        if not commit
                        else self.get_lab_ledger_state(cycle_id)
                    )
                except (ReportVersionConflictError, OperationalError, IntegrityError) as exc:
                    if commit:
                        self.db.rollback()
                        if attempt >= effective_retries:
                            raise ConsiliumConcurrencyConflictError(
                                f"Concurrent laboratory ledger update conflict for cycle '{cycle_id}' "
                                f"after {effective_retries + 1} attempts: {exc}"
                            ) from exc
                    else:
                        raise ConsiliumConcurrencyConflictError(
                            f"Concurrent laboratory ledger update conflict for cycle '{cycle_id}': {exc}"
                        ) from exc

    # ==========================================================================
    # 2. DETERMINISTIC LABORATORY DIFF ENGINE
    # ==========================================================================

    @staticmethod
    def _normalize_unit_for_comparison(unit: Optional[str]) -> str:
        if not unit:
            return ""
        return unit.strip().lower().replace("\u03bc", "u").replace("\u00b5", "u")

    @staticmethod
    def _reference_ranges_differ(
        ref_a: Optional[LabReferenceRange], ref_b: Optional[LabReferenceRange]
    ) -> bool:
        if ref_a is None and ref_b is None:
            return False
        if (ref_a is None) != (ref_b is None):
            return True
        assert ref_a is not None and ref_b is not None
        return (
            ref_a.low != ref_b.low
            or ref_a.high != ref_b.high
            or (ref_a.text or "") != (ref_a.text or "")
        )

    def _get_ledger_active_markers_by_version(
        self, cycle_id: str, target_version: int
    ) -> Dict[str, LabObservationEntry]:
        if target_version <= 0:
            return {}
        versions = self.report_repo.list_versions(
            cycle_id=cycle_id,
            report_type=LABORATORY_LEDGER_REPORT_TYPE,
            privacy_level=PrivacyLevel.SERVER_ONLY,
        )
        for r in versions:
            ver = int((r.content or {}).get("ledger_version", r.version))
            if ver == target_version:
                raw_active = (r.content or {}).get("active_markers") or {}
                return {
                    k: LabObservationEntry.model_validate(v) for k, v in raw_active.items()
                }
        return {}

    def _get_ledger_history_by_version(
        self, cycle_id: str, target_version: int
    ) -> List[LabObservationEntry]:
        if target_version <= 0:
            return []
        versions = self.report_repo.list_versions(
            cycle_id=cycle_id,
            report_type=LABORATORY_LEDGER_REPORT_TYPE,
            privacy_level=PrivacyLevel.SERVER_ONLY,
        )
        for r in versions:
            ver = int((r.content or {}).get("ledger_version", r.version))
            if ver == target_version:
                raw_hist = (r.content or {}).get("observations_history") or []
                return [LabObservationEntry.model_validate(item) for item in raw_hist]
        return []

    def compute_laboratory_diff(
        self,
        cycle_id: str,
        baseline_version: Optional[int] = None,
        target_version: Optional[int] = None,
    ) -> LaboratoryDiffReport:
        """
        Deterministically compares `baseline_version` (defaults to `last_evaluated_ledger_version`)
        against `target_version` (defaults to current `ledger_version`).
        Classifies:
        - added_markers
        - modified_values
        - corrected_error_values (input/OCR error corrections)
        - new_dated_studies (longitudinal studies with a new sample_date)
        - unit_changed_markers (incompatible/changed units)
        - removed_or_invalidated_markers
        - unchanged_markers
        Never claims a numerical change is a clinical improvement or deterioration.
        """
        current_state = self.get_lab_ledger_state(cycle_id)
        resolved_baseline = (
            baseline_version
            if baseline_version is not None
            else current_state.last_evaluated_ledger_version
        )
        resolved_target = (
            target_version if target_version is not None else current_state.ledger_version
        )

        baseline_active = self._get_ledger_active_markers_by_version(cycle_id, resolved_baseline)
        target_active = (
            current_state.active_markers
            if resolved_target == current_state.ledger_version
            else self._get_ledger_active_markers_by_version(cycle_id, resolved_target)
        )
        target_history = (
            current_state.observations_history
            if resolved_target == current_state.ledger_version
            else self._get_ledger_history_by_version(cycle_id, resolved_target)
        )

        added_markers: List[LabDiffItem] = []
        modified_values: List[LabDiffItem] = []
        corrected_error_values: List[LabDiffItem] = []
        new_dated_studies: List[LabDiffItem] = []
        unit_changed_markers: List[LabDiffItem] = []
        removed_or_invalidated_markers: List[LabDiffItem] = []
        unchanged_markers: List[LabDiffItem] = []

        all_keys = sorted(set(baseline_active.keys()) | set(target_active.keys()))

        for key in all_keys:
            prev_obs = baseline_active.get(key)
            curr_obs = target_active.get(key)

            # 1. Added marker
            if prev_obs is None and curr_obs is not None:
                item = LabDiffItem(
                    marker_key=key,
                    display_name=curr_obs.display_name,
                    change_type="added",
                    previous_observation_id=None,
                    new_observation_id=curr_obs.observation_id,
                    previous_value=None,
                    new_value=curr_obs.value,
                    previous_unit=None,
                    new_unit=curr_obs.unit,
                    previous_sample_date=None,
                    new_sample_date=curr_obs.sample_date,
                    previous_reference_range=None,
                    new_reference_range=curr_obs.reference_range,
                    directly_comparable=False,
                    comparability_note="Newly added laboratory marker with no prior baseline in this cycle.",
                    neutral_summary=(
                        f"Added marker '{curr_obs.display_name}': {curr_obs.value} "
                        f"{curr_obs.unit or ''} (sample date: {curr_obs.sample_date or 'unspecified'})."
                    ).strip(),
                )
                added_markers.append(item)
                continue

            # 2. Removed or invalidated marker
            if prev_obs is not None and curr_obs is None:
                item = LabDiffItem(
                    marker_key=key,
                    display_name=prev_obs.display_name,
                    change_type="removed_or_invalidated",
                    previous_observation_id=prev_obs.observation_id,
                    new_observation_id=None,
                    previous_value=prev_obs.value,
                    new_value=None,
                    previous_unit=prev_obs.unit,
                    new_unit=None,
                    previous_sample_date=prev_obs.sample_date,
                    new_sample_date=None,
                    previous_reference_range=prev_obs.reference_range,
                    new_reference_range=None,
                    directly_comparable=False,
                    comparability_note=(
                        "Previous value was invalidated as erroneous or removed; "
                        "prior conclusion relying on this value requires re-evaluation."
                    ),
                    neutral_summary=(
                        f"Marker '{prev_obs.display_name}' (previous value: {prev_obs.value} "
                        f"{prev_obs.unit or ''}) was invalidated/removed from active labs."
                    ).strip(),
                )
                removed_or_invalidated_markers.append(item)
                continue

            assert prev_obs is not None and curr_obs is not None

            same_observation = prev_obs.observation_id == curr_obs.observation_id
            unit_changed = self._normalize_unit_for_comparison(
                prev_obs.unit
            ) != self._normalize_unit_for_comparison(curr_obs.unit)
            ref_changed = self._reference_ranges_differ(
                prev_obs.reference_range, curr_obs.reference_range
            )
            date_changed = (prev_obs.sample_date or "") != (curr_obs.sample_date or "")
            value_changed = str(prev_obs.value) != str(curr_obs.value) or (
                prev_obs.numeric_value != curr_obs.numeric_value
            )
            had_intermediate_correction = (not same_observation) and any(
                h.marker_key == key
                and h.event_type == LabEventType.CORRECTION
                and h.supersedes_observation_id == prev_obs.observation_id
                for h in target_history
            )
            is_error_correction = (not same_observation) and (
                curr_obs.event_type == LabEventType.CORRECTION or had_intermediate_correction
            )
            is_new_study_event = (not same_observation) and (
                date_changed or curr_obs.event_type == LabEventType.NEW_STUDY
            )

            # Unchanged marker (same observation or identical attributes with no new event)
            if same_observation or not (
                unit_changed
                or ref_changed
                or date_changed
                or value_changed
                or is_error_correction
                or is_new_study_event
            ):
                unchanged_markers.append(
                    LabDiffItem(
                        marker_key=key,
                        display_name=curr_obs.display_name,
                        change_type="unchanged",
                        previous_observation_id=prev_obs.observation_id,
                        new_observation_id=curr_obs.observation_id,
                        previous_value=prev_obs.value,
                        new_value=curr_obs.value,
                        previous_unit=prev_obs.unit,
                        new_unit=curr_obs.unit,
                        previous_sample_date=prev_obs.sample_date,
                        new_sample_date=curr_obs.sample_date,
                        previous_reference_range=prev_obs.reference_range,
                        new_reference_range=curr_obs.reference_range,
                        directly_comparable=True,
                        comparability_note="No change in value, unit, sample date, or reference range.",
                        neutral_summary=(
                            f"Marker '{curr_obs.display_name}' unchanged at {curr_obs.value} "
                            f"{curr_obs.unit or ''} (sample date: {curr_obs.sample_date or 'unspecified'})."
                        ).strip(),
                    )
                )
                continue

            # Determine comparability & classification
            notes: List[str] = []
            directly_comparable = True

            if unit_changed:
                directly_comparable = False
                notes.append(
                    f"Measurement unit changed from '{prev_obs.unit or 'none'}' to '{curr_obs.unit or 'none'}'. "
                    "Direct numerical subtraction/comparison is invalid without unit conversion."
                )
            if ref_changed:
                directly_comparable = False
                notes.append(
                    "Laboratory reference range changed between entries; interpret each result against its own assay reference interval."
                )
            if is_error_correction:
                notes.append(
                    f"Correction of prior erroneous value ({curr_obs.correction_reason or 'data entry correction'}). "
                    "Treat as baseline correction, NOT biological dynamics over time."
                )
            if is_new_study_event:
                notes.append(
                    f"New study from sample date '{curr_obs.sample_date}' compared to prior sample date '{prev_obs.sample_date}'. "
                    "Do not assume numeric shift is clinical improvement or deterioration without full clinical context."
                )
            if not is_error_correction and not is_new_study_event:
                notes.append(
                    "Updated value recorded; evaluate in clinical context without assuming automatic improvement or deterioration."
                )

            if is_error_correction and not is_new_study_event:
                primary_change_type = "error_correction"
            elif unit_changed:
                primary_change_type = "unit_changed"
            elif is_new_study_event:
                primary_change_type = "new_dated_study"
            else:
                primary_change_type = "value_updated"

            diff_item = LabDiffItem(
                marker_key=key,
                display_name=curr_obs.display_name,
                change_type=primary_change_type,
                previous_observation_id=prev_obs.observation_id,
                new_observation_id=curr_obs.observation_id,
                previous_value=prev_obs.value,
                new_value=curr_obs.value,
                previous_unit=prev_obs.unit,
                new_unit=curr_obs.unit,
                previous_sample_date=prev_obs.sample_date,
                new_sample_date=curr_obs.sample_date,
                previous_reference_range=prev_obs.reference_range,
                new_reference_range=curr_obs.reference_range,
                directly_comparable=directly_comparable,
                comparability_note=" ".join(notes),
                neutral_summary=(
                    f"Marker '{curr_obs.display_name}' ({primary_change_type}): "
                    f"previous={prev_obs.value} {prev_obs.unit or ''} (date={prev_obs.sample_date or 'N/A'}) -> "
                    f"current={curr_obs.value} {curr_obs.unit or ''} (date={curr_obs.sample_date or 'N/A'})."
                ),
            )

            modified_values.append(diff_item)
            if is_error_correction:
                corrected_error_values.append(diff_item)
            if is_new_study_event:
                new_dated_studies.append(diff_item)
            if unit_changed:
                unit_changed_markers.append(diff_item)

        return LaboratoryDiffReport(
            cycle_id=cycle_id,
            baseline_ledger_version=resolved_baseline,
            target_ledger_version=resolved_target,
            generated_at_utc=self._now_utc().isoformat(),
            added_markers=added_markers,
            modified_values=modified_values,
            corrected_error_values=corrected_error_values,
            new_dated_studies=new_dated_studies,
            unit_changed_markers=unit_changed_markers,
            removed_or_invalidated_markers=removed_or_invalidated_markers,
            unchanged_markers=unchanged_markers,
        )

    # ==========================================================================
    # 3. 168-HOUR COOLDOWN CHECK (SERVER TIME, CYCLE-ISOLATED)
    # ==========================================================================

    @staticmethod
    def _parse_iso_utc(raw_str: Optional[str]) -> Optional[datetime]:
        if not raw_str or not isinstance(raw_str, str):
            return None
        text = raw_str.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(text)
            if dt.tzinfo is None:
                return dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except ValueError:
            return None

    @staticmethod
    def _lock_report_id(cycle_id: str) -> str:
        return f"lock_lab_rev_{cycle_id}"[:64]

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
        If an expired lease row exists from a hard-terminated process (> TTL without renewal),
        cleans it up so a crashed worker never permanently blocks future revisions.
        """
        lock_row = self._fetch_fresh_lock_row(cycle_id)
        if lock_row is None:
            return None
        now_dt = self._now_utc()
        expires_dt = self._parse_iso_utc((lock_row.content or {}).get("expires_at_utc"))
        if expires_dt is not None and expires_dt <= now_dt:
            self.db.delete(lock_row)
            self.db.commit()
            return None
        return lock_row

    def _acquire_cross_process_lease(self, cycle_id: str) -> str:
        """
        Acquires an atomic database-level execution lease (`report_id = lock_lab_rev_{cycle_id}`)
        backed by the SQLite PRIMARY KEY constraint on `model_reports.report_id` and tagged
        with a unique `lease_owner_id` fencing token.
        """
        cooldown_status = self.get_cooldown_status(cycle_id)
        if not cooldown_status.is_allowed:
            raise ConsiliumCooldownError(cooldown_status)

        ledger_state = self.get_lab_ledger_state(cycle_id)
        if ledger_state.ledger_version == 0:
            raise ConsiliumContextMissingError(
                f"Cannot run Lab-Revision Consilium for cycle '{cycle_id}': no laboratory data is stored."
            )
        if not ledger_state.has_unreviewed_lab_changes:
            raise ConsiliumCooldownError(
                cooldown_status.model_copy(
                    update={
                        "is_allowed": False,
                        "has_unreviewed_lab_changes": False,
                        "message": (
                            f"Cannot run Full Lab-Revision Consilium for cycle '{cycle_id}': "
                            f"no new or unreviewed laboratory changes exist (ledger v{ledger_state.ledger_version} "
                            f"matches last evaluated v{ledger_state.last_evaluated_ledger_version})."
                        ),
                    }
                )
            )

        now_dt = self._now_utc()
        expires_dt = now_dt + timedelta(seconds=self.lease_ttl_seconds)
        lock_id = self._lock_report_id(cycle_id)
        lease_owner_id = f"lease_{uuid.uuid4().hex}"

        try:
            lock_report = ModelReport(
                report_id=lock_id,
                cycle_id=cycle_id,
                sprint_id=None,
                session_id=None,
                model="aegis-concurrency-guard",
                report_type=LAB_REVISION_IN_FLIGHT_LOCK_REPORT_TYPE,
                privacy_level=PrivacyLevel.SERVER_ONLY.value,
                version=1,
                content={
                    "lease_owner_id": lease_owner_id,
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
        except IntegrityError:
            self.db.rollback()
            raise ConsiliumCooldownError(self.get_cooldown_status(cycle_id))

    def _renew_cross_process_lease(self, cycle_id: str, lease_owner_id: str) -> None:
        """
        Verifies that `lease_owner_id` still owns the SQLite execution lease and extends
        `expires_at_utc` by `self.lease_ttl_seconds`. If the lease was lost or replaced
        by another owner, raises `ConsiliumCooldownError` immediately (fencing guard).
        """
        lock_row = self._fetch_fresh_lock_row(cycle_id)
        lock_content = dict(lock_row.content or {}) if lock_row is not None else {}
        if lock_row is None or lock_content.get("lease_owner_id") != lease_owner_id:
            raise ConsiliumCooldownError(
                self.get_cooldown_status(cycle_id).model_copy(
                    update={
                        "is_allowed": False,
                        "message": (
                            f"Execution lease '{lease_owner_id}' for cycle '{cycle_id}' is no longer "
                            "owned by this worker; aborting stale consilium execution."
                        ),
                    }
                )
            )

        now_dt = self._now_utc()
        new_expires_dt = now_dt + timedelta(seconds=self.lease_ttl_seconds)
        lock_content["renewed_at_utc"] = now_dt.isoformat()
        lock_content["expires_at_utc"] = new_expires_dt.isoformat()
        lock_content["renewal_count"] = int(lock_content.get("renewal_count", 0)) + 1
        lock_row.content = lock_content
        lock_row.version = int(lock_row.version or 1) + 1
        self.db.add(lock_row)
        self.db.commit()

    async def _execute_with_lease_renewal(
        self,
        cycle_id: str,
        lease_owner_id: str,
        coro_factory: Callable[[], Awaitable[Any]],
    ) -> Any:
        """
        Executes an async Gemini gateway call while keeping the SQLite execution lease alive:
        - Renews & verifies `lease_owner_id` before the call;
        - Periodically renews `lease_owner_id` via a background heartbeat while awaiting I/O;
        - Renews & verifies `lease_owner_id` immediately after the call returns.
        """
        self._renew_cross_process_lease(cycle_id, lease_owner_id)
        stop_event = asyncio.Event()

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
                        self._renew_cross_process_lease(cycle_id, lease_owner_id)

        hb_task = asyncio.create_task(_heartbeat())
        try:
            result = await coro_factory()
        finally:
            stop_event.set()
            await hb_task
        self._renew_cross_process_lease(cycle_id, lease_owner_id)
        return result

    def _verify_and_delete_cross_process_lease_uncommitted(
        self, cycle_id: str, lease_owner_id: str
    ) -> None:
        """
        Fencing check before final atomic persistence: verifies the SQLite lease is still
        owned by `lease_owner_id` and marks it for deletion inside the uncommitted transaction.
        Never deletes a lease owned by another worker.
        """
        lock_row = self._fetch_fresh_lock_row(cycle_id)
        if lock_row is None or (lock_row.content or {}).get("lease_owner_id") != lease_owner_id:
            raise ConsiliumCooldownError(
                self.get_cooldown_status(cycle_id).model_copy(
                    update={
                        "is_allowed": False,
                        "message": (
                            f"Cannot commit Lab-Revision Consilium for cycle '{cycle_id}': "
                            f"execution lease '{lease_owner_id}' was lost or superseded by another owner."
                        ),
                    }
                )
            )
        self.db.delete(lock_row)

    def _release_cross_process_lease_safe(self, cycle_id: str, lease_owner_id: str) -> None:
        """
        Safely releases the cross-process execution lease on error ONLY if the lock row
        is still owned by `lease_owner_id`. Never deletes a lock acquired by another worker.
        """
        try:
            lock_row = self._fetch_fresh_lock_row(cycle_id)
            if (
                lock_row is not None
                and (lock_row.content or {}).get("lease_owner_id") == lease_owner_id
            ):
                self.db.delete(lock_row)
                self.db.commit()
        except Exception:
            self.db.rollback()

    def get_cooldown_status(self, cycle_id: str) -> LabRevisionCooldownStatus:
        """
        Evaluates the 168-hour (7-day) cooldown for user-initiated Full Lab-Revision Consilium:
        - Uses authoritative server UTC time (`self._now_utc()`), never client `report_date`.
        - Checks for any active cross-process in-flight execution lease (`lab_revision_in_flight_lock`).
        - Scoped strictly to `cycle_id` and `LAB_REVISION_EXECUTION_LOG_REPORT_TYPE` so:
          * Primary Consilium of a new cycle is never blocked by a previous cycle;
          * Weekly Consilium (`Flash -> Pro`) never consumes this cooldown;
          * Failed full revisions never consume this cooldown.
        """
        if not self.cycle_repo.get(cycle_id):
            raise ConsiliumCycleNotFoundError(f"Medical cycle '{cycle_id}' not found.")

        ledger_state = self.get_lab_ledger_state(cycle_id)

        active_lock = self._get_active_in_flight_lock(cycle_id)
        if active_lock is not None:
            now_dt = self._now_utc()
            lock_c = active_lock.content or {}
            acquired_iso = str(lock_c.get("acquired_at_utc") or now_dt.isoformat())
            expires_dt = self._parse_iso_utc(lock_c.get("expires_at_utc")) or (
                now_dt + timedelta(seconds=self.lease_ttl_seconds)
            )
            rem_sec = max(1, int(math.ceil((expires_dt - now_dt).total_seconds())))
            rem_hrs = round(rem_sec / 3600.0, 2)
            return LabRevisionCooldownStatus(
                cycle_id=cycle_id,
                cooldown_hours=LAB_REVISION_COOLDOWN_HOURS,
                is_allowed=False,
                remaining_seconds=rem_sec,
                remaining_hours=rem_hrs,
                last_completed_at_utc=acquired_iso,
                next_available_at_utc=expires_dt.isoformat(),
                has_unreviewed_lab_changes=ledger_state.has_unreviewed_lab_changes,
                evaluation_status=ledger_state.evaluation_status,
                message=(
                    f"Full Lab-Revision Consilium is already in progress for cycle '{cycle_id}'. "
                    f"Concurrent execution across requests or worker processes is blocked. "
                    f"{MEDICAL_SAFETY_COOLDOWN_NOTICE}"
                ),
                safety_notice=MEDICAL_SAFETY_COOLDOWN_NOTICE,
            )

        exec_logs = self.report_repo.list_server_only(
            cycle_id=cycle_id,
            report_type=LAB_REVISION_EXECUTION_LOG_REPORT_TYPE,
        )

        if not exec_logs:
            return LabRevisionCooldownStatus(
                cycle_id=cycle_id,
                cooldown_hours=LAB_REVISION_COOLDOWN_HOURS,
                is_allowed=True,
                remaining_seconds=0,
                remaining_hours=0.0,
                last_completed_at_utc=None,
                next_available_at_utc=None,
                has_unreviewed_lab_changes=ledger_state.has_unreviewed_lab_changes,
                evaluation_status=ledger_state.evaluation_status,
                message="Full Lab-Revision Consilium is available.",
                safety_notice=MEDICAL_SAFETY_COOLDOWN_NOTICE,
            )

        latest_log = exec_logs[0]
        log_content = latest_log.content or {}
        last_completed_dt = self._parse_iso_utc(log_content.get("completed_at_utc"))
        if last_completed_dt is None:
            raw_created = latest_log.created_at
            last_completed_dt = (
                raw_created.replace(tzinfo=timezone.utc)
                if raw_created.tzinfo is None
                else raw_created.astimezone(timezone.utc)
            )

        cooldown_delta = timedelta(hours=LAB_REVISION_COOLDOWN_HOURS)
        next_available_dt = last_completed_dt + cooldown_delta
        now_dt = self._now_utc()

        if now_dt >= next_available_dt:
            return LabRevisionCooldownStatus(
                cycle_id=cycle_id,
                cooldown_hours=LAB_REVISION_COOLDOWN_HOURS,
                is_allowed=True,
                remaining_seconds=0,
                remaining_hours=0.0,
                last_completed_at_utc=last_completed_dt.isoformat(),
                next_available_at_utc=next_available_dt.isoformat(),
                has_unreviewed_lab_changes=ledger_state.has_unreviewed_lab_changes,
                evaluation_status=ledger_state.evaluation_status,
                message="168-hour cooldown has elapsed; Full Lab-Revision Consilium is available.",
                safety_notice=MEDICAL_SAFETY_COOLDOWN_NOTICE,
            )

        remaining_sec = max(1, int(math.ceil((next_available_dt - now_dt).total_seconds())))
        remaining_hrs = round(remaining_sec / 3600.0, 2)
        msg = (
            f"Full Lab-Revision Consilium is on cooldown (limit: once every {LAB_REVISION_COOLDOWN_HOURS} hours). "
            f"Remaining wait time: {remaining_hrs} hours ({remaining_sec} seconds). "
            f"Next available at: {next_available_dt.isoformat()}. "
            f"{MEDICAL_SAFETY_COOLDOWN_NOTICE}"
        )
        return LabRevisionCooldownStatus(
            cycle_id=cycle_id,
            cooldown_hours=LAB_REVISION_COOLDOWN_HOURS,
            is_allowed=False,
            remaining_seconds=remaining_sec,
            remaining_hours=remaining_hrs,
            last_completed_at_utc=last_completed_dt.isoformat(),
            next_available_at_utc=next_available_dt.isoformat(),
            has_unreviewed_lab_changes=ledger_state.has_unreviewed_lab_changes,
            evaluation_status=ledger_state.evaluation_status,
            message=msg,
            safety_notice=MEDICAL_SAFETY_COOLDOWN_NOTICE,
        )

    # ==========================================================================
    # 4. PREVIOUS CONSILIUM CONTEXT LOADERS
    # ==========================================================================

    def _load_preparatory_anamnesis(self, cycle_id: str) -> AnamnesisModelOutput:
        reports = self.report_repo.list_server_only(
            cycle_id=cycle_id,
            report_type=PREPARATORY_ANAMNESIS_REPORT_TYPE,
        )
        if not reports:
            raise ConsiliumContextMissingError(
                f"Cannot run Lab-Revision Consilium for cycle '{cycle_id}': verified Anamnesis (M1) "
                f"report ('{PREPARATORY_ANAMNESIS_REPORT_TYPE}') is missing in server storage."
            )
        latest_content = dict(reports[0].content or {})
        stored_role = latest_content.pop("agent_role", None)
        if stored_role != AegisAgentRole.ANAMNESIS_M1.value:
            raise ConsiliumContextMissingError(
                f"Invalid agent_role '{stored_role}' on preparatory Anamnesis M1 report."
            )
        return AnamnesisModelOutput.model_validate(latest_content)

    def _load_preparatory_ecg_and_vitals(self, cycle_id: str) -> Dict[str, str]:
        ecg_reports = self.report_repo.list_server_only(
            cycle_id=cycle_id,
            report_type=PREPARATORY_ECG_REPORT_TYPE,
        )
        ecg_summary = "Not conducted"
        if ecg_reports:
            c = ecg_reports[0].content or {}
            if c.get("agent_role") != AegisAgentRole.VISION_ECG.value:
                raise ConsiliumContextMissingError("Invalid agent_role on preparatory Vision ECG report.")
            ecg_summary = str(c.get("ecg_summary") or "Not conducted").strip()

        measurements = self.measurement_repo.list_by_cycle(cycle_id=cycle_id)
        systolic = next((m.systolic for m in reversed(measurements) if m.systolic is not None), None)
        diastolic = next((m.diastolic for m in reversed(measurements) if m.diastolic is not None), None)
        pulse = next((m.pulse for m in reversed(measurements) if m.pulse is not None), None)
        spo2 = next((m.spo2 for m in reversed(measurements) if m.spo2 is not None), None)

        return {
            "blood_pressure": f"{systolic if systolic is not None else '--'}/{diastolic if diastolic is not None else '--'} mmHg",
            "heart_rate": f"{pulse if pulse is not None else '--'} bpm",
            "spo2": f"{spo2 if spo2 is not None else '--'}%",
            "ecg_summary": ecg_summary,
        }

    def _load_previous_models_state(self, cycle_id: str) -> Dict[str, Any]:
        """
        Loads the latest completed Primary / Lab-Revision / Weekly consilium outputs
        from server storage so every agent receives its own previous output and relevant
        previous conclusions/audits of other models.
        """
        pro_list = self.report_repo.list_server_only(
            cycle_id=cycle_id,
            report_type="chief_physician_internal",
        )
        flash_list = self.report_repo.list_server_only(
            cycle_id=cycle_id,
            report_type="deputy_chief_physician_internal",
        )
        if not pro_list or not flash_list:
            raise ConsiliumContextMissingError(
                f"Cannot run Lab-Revision Consilium for cycle '{cycle_id}': Primary Consilium "
                "must be completed first before running a comparative lab revision."
            )

        latest_pro = pro_list[0].content or {}
        latest_flash = flash_list[0].content or {}
        primary_pro = pro_list[-1].content or {}
        primary_flash = flash_list[-1].content or {}

        previous_organs: Dict[str, Dict[str, Any]] = {}
        for system_key in SYSTEM_KEYS:
            role_val = AegisAgentRole.for_system_key(system_key).value
            sys_reports = self.report_repo.list_server_only(
                cycle_id=cycle_id,
                report_type=role_val,
            )
            if sys_reports:
                sys_c = sys_reports[0].content or {}
                previous_organs[system_key] = {
                    "version": sys_reports[0].version,
                    "system_view": sys_c.get("system_view", {}),
                    "system_logic": sys_c.get("system_logic", {}),
                    "training_sandbox": sys_c.get("training_sandbox", {}),
                }
            else:
                previous_organs[system_key] = {}

        weekly_flash_list = self.report_repo.list_server_only(
            cycle_id=cycle_id,
            report_type=WEEKLY_DEPUTY_INTERNAL_REPORT_TYPE,
        )
        weekly_pro_list = self.report_repo.list_server_only(
            cycle_id=cycle_id,
            report_type=WEEKLY_CHIEF_INTERNAL_REPORT_TYPE,
        )

        return {
            "previous_organs": previous_organs,
            "previous_flash": {
                "version": flash_list[0].version,
                "flash_general_shelf": latest_flash.get("flash_general_shelf", ""),
                "flash_training_shelf": latest_flash.get("flash_training_shelf", ""),
                "draft_user_system_reports": latest_flash.get("draft_user_system_reports", {}),
            },
            "previous_pro": {
                "version": pro_list[0].version,
                "pro_general_shelf": latest_pro.get("pro_general_shelf", ""),
                "pro_training_shelf": latest_pro.get("pro_training_shelf", ""),
                "vitals_thresholds": latest_pro.get("vitals_thresholds", {}),
                "flash_quality_review": latest_pro.get("flash_quality_review", {}),
            },
            "latest_weekly_flash": (
                weekly_flash_list[0].content.get("weekly_deputy_analysis", "")
                if weekly_flash_list
                else None
            ),
            "latest_weekly_pro_strategy": (
                weekly_pro_list[0].content.get("updated_weekly_strategy", "")
                if weekly_pro_list
                else None
            ),
            "primary_pro_general": str(primary_pro.get("pro_general_shelf", "")),
            "primary_pro_training": str(primary_pro.get("pro_training_shelf", "")),
            "primary_flash_general": str(primary_flash.get("flash_general_shelf", "")),
        }

    # ==========================================================================
    # 5. FULL LAB-REVISION CONSILIUM (8 FLASH-LITE -> DEPUTY FLASH -> PRO)
    # ==========================================================================

    async def run_lab_revision_consilium(
        self,
        cycle_id: str,
        report_date: Optional[str] = None,
    ) -> LabRevisionConsiliumClientResponse:
        """
        Executes the user-initiated Full Lab-Revision Consilium:
        1. Acquires per-cycle concurrency lock (`asyncio.Lock` + cross-process SQLite PK lease)
           and verifies the 168-hour server-time cooldown. If on cooldown or already in flight,
           raises ConsiliumCooldownError without calling Gemini.
        2. Computes deterministic laboratory diff against the last evaluated ledger version.
        3. Executes strictly: 8 Gemini Flash-Lite -> Gemini Flash -> Gemini Pro.
           Never re-runs Anamnesis M1 or Vision OCR/ECG.
        4. Enforces Pro's quality gate and deterministic corrections before any DB writes.
        5. Atomically persists the diff audit, 10 internal reports, updated active Athena
           strategy version, execution log (recording cooldown & evaluated ledger version),
           8 dated Atlas system reports, and the new overall Vault report.
        """
        cycle = self.cycle_repo.get(cycle_id)
        if not cycle:
            raise ConsiliumCycleNotFoundError(f"Medical cycle '{cycle_id}' not found.")
        if cycle.status != "active":
            raise ConsiliumError(
                f"Cannot run Lab-Revision Consilium on non-active cycle '{cycle_id}' (status='{cycle.status}')."
            )

        lock = self._get_cycle_lock(cycle_id)
        async with lock:
            # 1. Authoritative server-time 168-hour cooldown & cross-process lease acquisition
            lease_owner_id = self._acquire_cross_process_lease(cycle_id)

            try:
                resolved_date = (
                    report_date.strip()
                    if isinstance(report_date, str) and report_date.strip()
                    else self._now_utc().strftime("%Y-%m-%d")
                )

                # 2. Load preparatory context, current lab ledger, deterministic diff, and previous conclusions
                anamnesis_m1 = self._load_preparatory_anamnesis(cycle_id)
                vitals_stream = self._load_preparatory_ecg_and_vitals(cycle_id)
                ledger_state = self.get_lab_ledger_state(cycle_id)
                if ledger_state.ledger_version == 0:
                    raise ConsiliumContextMissingError(
                        f"Cannot run Lab-Revision Consilium for cycle '{cycle_id}': no laboratory data is stored."
                    )

                diff_report = self.compute_laboratory_diff(
                    cycle_id=cycle_id,
                    baseline_version=ledger_state.last_evaluated_ledger_version,
                    target_version=ledger_state.ledger_version,
                )
                diff_summary_dict = diff_report.model_dump()

                active_markers_raw = {
                    k: obs.model_dump() for k, obs in ledger_state.active_markers.items()
                }
                shared_labs_table = self._build_shared_labs_table_from_active(active_markers_raw)
                prev_models = self._load_previous_models_state(cycle_id)

                # 3. STAGE 1: Execute 8 Organ Specialists (Gemini Flash-Lite)
                system_outputs: Dict[str, SystemAgentOutput] = {}
                for system_key in SYSTEM_KEYS:
                    organ_fact = getattr(anamnesis_m1.system_logic, system_key)
                    input_package = {
                        "system_key": system_key,
                        "facts": organ_fact.data,
                        "first_model_short_summary": organ_fact.internal_comment,
                        "shared_labs_table": shared_labs_table,
                        "vitals_stream": vitals_stream,
                    }
                    previous_organ_output = prev_models["previous_organs"].get(system_key, {})
                    previous_consilium_summary = {
                        "previous_deputy_flash_general_shelf": prev_models["previous_flash"]["flash_general_shelf"],
                        "previous_deputy_flash_training_audit": prev_models["previous_flash"]["flash_training_shelf"],
                        "previous_chief_pro_strategy": (
                            prev_models["latest_weekly_pro_strategy"]
                            or prev_models["previous_pro"]["pro_general_shelf"]
                        ),
                        "previous_chief_pro_audit": prev_models["previous_pro"]["pro_training_shelf"],
                    }
                    system_prompt = build_lab_revision_system_agent_prompt(
                        system_key=system_key,
                        input_package=input_package,
                        lab_diff_summary=diff_summary_dict,
                        previous_organ_output=previous_organ_output,
                        previous_consilium_summary=previous_consilium_summary,
                    )
                    organ_result: SystemAgentOutput = await self._execute_with_lease_renewal(
                        cycle_id,
                        lease_owner_id,
                        lambda sp=system_prompt: self.gateway.generate_structured(
                            prompt=sp,
                            response_schema=SystemAgentOutput,
                            model=GeminiModelRole.FLASH_LITE,
                        ),
                    )
                    system_outputs[system_key] = organ_result

                # 4. STAGE 2: Execute Deputy Chief Physician (Gemini Flash)
                comparative_flash_lines = [
                    "=== ANAMNESIS AND UPDATED LABORATORY DATA ===",
                    json.dumps(
                        {
                            "user_view": anamnesis_m1.user_view.model_dump(),
                            "training_sandbox": anamnesis_m1.training_sandbox.model_dump(),
                            "shared_labs_shelf": shared_labs_table,
                        },
                        ensure_ascii=False,
                    ),
                    "",
                    "=== DETERMINISTIC LABORATORY DIFF (BASELINE VS CURRENT) ===",
                    json.dumps(diff_summary_dict, ensure_ascii=False),
                    "",
                    "=== VITAL MARKERS (SMART WATCH / LIVE) ===",
                    f"Blood pressure: {vitals_stream['blood_pressure']}",
                    f"Heart rate (Pulse): {vitals_stream['heart_rate']}",
                    f"Oxygen saturation (SpO2): {vitals_stream['spo2']}",
                    f"ECG status: {vitals_stream['ecg_summary']}",
                    "",
                    "=== PREVIOUS DEPUTY FLASH & CHIEF PHYSICIAN PRO CONCLUSIONS & AUDIT ===",
                    json.dumps(
                        {
                            "previous_deputy_flash": prev_models["previous_flash"],
                            "previous_chief_pro": prev_models["previous_pro"],
                            "latest_weekly_flash": prev_models["latest_weekly_flash"],
                            "latest_weekly_pro_strategy": prev_models["latest_weekly_pro_strategy"],
                        },
                        ensure_ascii=False,
                    ),
                    "",
                    "=== PREVIOUS AND NEW CONCLUSIONS OF 8 ORGAN SYSTEM AGENTS ===",
                ]
                for key in SYSTEM_KEYS:
                    prev_org = prev_models["previous_organs"].get(key, {})
                    new_org = system_outputs[key]
                    comparative_flash_lines.append(f"--- BLOCK: {key.upper()} ---")
                    comparative_flash_lines.append(
                        f"Previous conclusion: {(prev_org.get('system_logic') or {}).get('system_general_shelf', 'N/A')}"
                    )
                    comparative_flash_lines.append(
                        f"New comparative conclusion: {new_org.system_logic.system_general_shelf}"
                    )
                    comparative_flash_lines.append(
                        f"New comparative training log: {new_org.training_sandbox.system_training_shelf}"
                    )
                    comparative_flash_lines.append("")

                comparative_flash_context = "\n".join(comparative_flash_lines)
                flash_prompt = build_lab_revision_flash_master_prompt(comparative_flash_context)
                flash_output: LabRevisionFlashMasterOutput = await self._execute_with_lease_renewal(
                    cycle_id,
                    lease_owner_id,
                    lambda: self.gateway.generate_structured(
                        prompt=flash_prompt,
                        response_schema=LabRevisionFlashMasterOutput,
                        model=GeminiModelRole.FLASH,
                    ),
                )

                # 5. STAGE 3: Execute Chief Physician (Gemini Pro)
                draft_system_reports_dict = flash_output.user_system_reports.to_dict()
                pro_context_lines = [
                    comparative_flash_context,
                    "=== COMPARATIVE SYNTHESIS (GEMINI FLASH) ===",
                    "Main conclusion:",
                    flash_output.flash_general_shelf,
                    "",
                    "Training output:",
                    flash_output.flash_training_shelf,
                    "",
                    "=== DRAFT 8 USER SYSTEM REPORTS FROM DEPUTY FLASH (FOR PRO QUALITY REVIEW) ===",
                    json.dumps(draft_system_reports_dict, ensure_ascii=False),
                    "",
                ]
                pro_prompt = build_lab_revision_pro_master_prompt("\n".join(pro_context_lines))
                pro_output: ProMasterOutput = await self._execute_with_lease_renewal(
                    cycle_id,
                    lease_owner_id,
                    lambda: self.gateway.generate_structured(
                        prompt=pro_prompt,
                        response_schema=ProMasterOutput,
                        model=GeminiModelRole.PRO,
                    ),
                )

                # 6. Chief Physician Quality Gate (BEFORE any database writes; failure does NOT consume cooldown)
                if not pro_output.flash_quality_review.approved_for_publication:
                    reason = (
                        pro_output.flash_quality_review.rejection_reason
                        or "Chief Physician (Pro) rejected publication of lab-revision user-facing reports."
                    )
                    raise ConsiliumQualityRejectionError(reason)

                # Apply Pro's deterministic corrections over Flash's 8 comparative system reports
                final_system_updates: Dict[str, Dict[str, Any]] = {
                    k: dict(v) for k, v in draft_system_reports_dict.items()
                }
                for corrected_key, corrected_text in (
                    pro_output.flash_quality_review.corrected_user_system_reports.items()
                ):
                    if corrected_key not in SYSTEM_KEYS:
                        raise ConsiliumQualityRejectionError(
                            f"Invalid system key '{corrected_key}' in Pro lab-revision quality review."
                        )
                    has_chg = bool(final_system_updates[corrected_key].get("has_changes", True))
                    validated_text = validate_lab_revision_unchanged_explanation(
                        corrected_text,
                        has_chg,
                        f"Corrected lab-revision summary for '{corrected_key}'",
                    )
                    final_system_updates[corrected_key]["summary"] = validated_text

                if set(final_system_updates.keys()) != set(SYSTEM_KEYS) or any(
                    not isinstance(final_system_updates.get(k, {}).get("summary"), str)
                    or not final_system_updates[k]["summary"].strip()
                    for k in SYSTEM_KEYS
                ):
                    raise ConsiliumQualityRejectionError(
                        "Incomplete 8-system lab-revision reports after quality review; aborting publication."
                    )

                completed_at_iso = self._now_utc().isoformat()

                # 7. Atomic single-transaction persistence
                try:
                    # Verify lease ownership (`lease_owner_id`) and release in-flight lease row as part of the atomic commit
                    self._verify_and_delete_cross_process_lease_uncommitted(
                        cycle_id, lease_owner_id
                    )

                    # 7.1 Save deterministic laboratory diff audit (server_only)
                    self.report_repo.create_next_version(
                        cycle_id=cycle_id,
                        model="aegis-lab-diff-engine",
                        report_type=LABORATORY_DIFF_AUDIT_REPORT_TYPE,
                        privacy_level=PrivacyLevel.SERVER_ONLY,
                        content=diff_summary_dict,
                        commit=False,
                    )

                    # 7.2 Save 8 Flash-Lite organ specialist outputs (server_only)
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
                                "consilium_kind": "lab_revision_consilium",
                                "evaluated_ledger_version": ledger_state.ledger_version,
                                "system_key": system_key,
                                "system_view": organ_out.system_view.model_dump(),
                                "system_logic": organ_out.system_logic.model_dump(),
                                "training_sandbox": organ_out.training_sandbox.model_dump(),
                            },
                            commit=False,
                        )

                    # 7.3 Save Deputy Chief Physician Flash output (server_only)
                    self.report_repo.create_next_version(
                        cycle_id=cycle_id,
                        model=self.gateway.flash_model,
                        report_type="deputy_chief_physician_internal",
                        privacy_level=PrivacyLevel.SERVER_ONLY,
                        content={
                            "agent_role": AegisAgentRole.DEPUTY_CHIEF_PHYSICIAN.value,
                            "consilium_kind": "lab_revision_consilium",
                            "evaluated_ledger_version": ledger_state.ledger_version,
                            "flash_general_shelf": flash_output.flash_general_shelf,
                            "flash_training_shelf": flash_output.flash_training_shelf,
                            "draft_user_system_reports": draft_system_reports_dict,
                        },
                        commit=False,
                    )

                    # 7.4 Save Chief Physician Pro output (server_only)
                    self.report_repo.create_next_version(
                        cycle_id=cycle_id,
                        model=self.gateway.pro_model,
                        report_type="chief_physician_internal",
                        privacy_level=PrivacyLevel.SERVER_ONLY,
                        content={
                            "agent_role": AegisAgentRole.CHIEF_PHYSICIAN.value,
                            "consilium_kind": "lab_revision_consilium",
                            "evaluated_ledger_version": ledger_state.ledger_version,
                            "pro_general_shelf": pro_output.pro_general_shelf,
                            "pro_training_shelf": pro_output.pro_training_shelf,
                            "vitals_thresholds": pro_output.vitals_thresholds.model_dump(),
                            "flash_quality_review": pro_output.flash_quality_review.model_dump(),
                        },
                        commit=False,
                    )

                    # 7.5 Ensure v1 baseline of athena_active_strategy exists, then save next monotonic version
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
                                "active_strategy_text": prev_models["primary_pro_general"],
                                "clinical_background_text": prev_models["primary_pro_training"],
                                "deputy_synthesis_text": prev_models["primary_flash_general"],
                                "daily_question_limit": 3,
                            },
                            commit=False,
                        )

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
                        sprint_id=None,
                        model=self.gateway.pro_model,
                        report_type=ATHENA_ACTIVE_STRATEGY_REPORT_TYPE,
                        privacy_level=PrivacyLevel.SERVER_ONLY,
                        content={
                            "agent_role": AegisAgentRole.CHIEF_PHYSICIAN.value,
                            "source_consilium": "lab_revision_consilium",
                            "strategy_status": "updated",
                            "strategy_version": next_strategy_ver,
                            "evaluated_ledger_version": ledger_state.ledger_version,
                            "active_strategy_text": pro_output.pro_general_shelf,
                            "clinical_background_text": pro_output.pro_training_shelf,
                            "deputy_synthesis_text": flash_output.flash_general_shelf,
                            "daily_question_limit": 3,
                        },
                        commit=False,
                    )

                    # 7.6 Save authoritative execution log (marks ledger_version as evaluated & starts 168h cooldown)
                    self.report_repo.create_next_version(
                        cycle_id=cycle_id,
                        model=self.gateway.pro_model,
                        report_type=LAB_REVISION_EXECUTION_LOG_REPORT_TYPE,
                        privacy_level=PrivacyLevel.SERVER_ONLY,
                        content={
                            "completed_at_utc": completed_at_iso,
                            "evaluated_ledger_version": ledger_state.ledger_version,
                            "baseline_ledger_version": ledger_state.last_evaluated_ledger_version,
                            "cooldown_hours": LAB_REVISION_COOLDOWN_HOURS,
                            "lease_owner_id": lease_owner_id,
                        },
                        created_at=self._now_utc(),
                        commit=False,
                    )

                    # 7.7 Save 8 dated client_facing system reports for Atlas (appending to historical entries)
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
                                historical_entries = [
                                    dict(e) for e in latest_prior_entries if isinstance(e, dict)
                                ]

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

                    # 7.8 Save new version of client_facing user_overall_report for the Vault
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
                                "evaluated_ledger_version": ledger_state.ledger_version,
                            },
                        },
                        commit=False,
                    )
                    validated_overall = ClientModelReportResponse.model_validate(saved_overall_report)

                    self.db.commit()
                except Exception:
                    self.db.rollback()
                    raise
            except Exception:
                self._release_cross_process_lease_safe(cycle_id, lease_owner_id)
                raise

            updated_cooldown = self.get_cooldown_status(cycle_id)
            return LabRevisionConsiliumClientResponse(
                cycle_id=cycle_id,
                report_date=resolved_date,
                evaluated_ledger_version=ledger_state.ledger_version,
                evaluation_status="evaluated",
                overall_report=validated_overall,
                system_reports=client_system_reports,
                vitals_thresholds=ClientVitalsThresholds(
                    min_pessimum_bpm=pro_output.vitals_thresholds.min_pessimum_bpm,
                    max_pessimum_bpm=pro_output.vitals_thresholds.max_pessimum_bpm,
                    max_spike_delta_bpm=pro_output.vitals_thresholds.max_spike_delta_bpm,
                ),
                cooldown=updated_cooldown,
            )
