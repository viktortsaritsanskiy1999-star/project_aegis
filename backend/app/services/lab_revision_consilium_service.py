import asyncio
from datetime import datetime, timedelta, timezone
import json
import math
import re
import threading
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple
import uuid
from sqlalchemy import delete, update
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
    normalize_lab_date,
    is_dimensionless_marker,
    are_lab_values_equivalent,
    to_decimal,
)
from app.schemas.privacy import PrivacyLevel
from app.schemas.reports import ClientModelReportResponse
from app.schemas.lab_scheduling import (
    BackgroundLabRevisionChiefOutput,
    BackgroundLabRevisionDeputyOutput,
    BackgroundLabRevisionReportRecord,
)
from app.services.consilium_prompts import (
    build_lab_revision_system_agent_prompt,
    build_lab_revision_flash_master_prompt,
    build_lab_revision_pro_master_prompt,
    build_background_lab_revision_deputy_prompt,
    build_background_lab_revision_chief_prompt,
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
    CYCLE_CONSILIUM_COORDINATION_LOCK_REPORT_TYPE,
)
from app.services.weekly_consilium_service import (
    ConsiliumCycleInactiveError,
    ATHENA_ACTIVE_STRATEGY_REPORT_TYPE,
    WEEKLY_DEPUTY_INTERNAL_REPORT_TYPE,
    WEEKLY_CHIEF_INTERNAL_REPORT_TYPE,
)


LABORATORY_LEDGER_REPORT_TYPE = "laboratory_ledger"
LABORATORY_DIFF_AUDIT_REPORT_TYPE = "laboratory_diff_audit"
LAB_REVISION_EXECUTION_LOG_REPORT_TYPE = "lab_revision_execution_log"
BACKGROUND_LAB_REVISION_REPORT_TYPE = "background_lab_revision_report"
LAB_REVISION_IN_FLIGHT_LOCK_REPORT_TYPE = "lab_revision_in_flight_lock"
LAB_REVISION_IN_FLIGHT_LOCK_TTL_SECONDS = 900
LAB_REVISION_IN_FLIGHT_LOCK_HEARTBEAT_SECONDS = 300.0

_NUMERIC_UNIT_RE = re.compile(
    r"^\s*([+-]?\d+(?:[.,]\d+)?)\s*([a-zA-Z%/\u03bc\u00b5\d\s\-\^]*)\s*$"
)


class ConsiliumCooldownError(ConsiliumConcurrencyConflictError):
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

        observations_history: List[Dict[str, Any]] = []
        active_markers: Dict[str, Dict[str, Any]] = {}

        if isinstance(raw_confirmed_list, list) and raw_confirmed_list:
            for idx, raw_entry in enumerate(raw_confirmed_list, start=1):
                marker_key = self._normalize_marker_key(str(raw_entry.get("marker_key", "")))
                display_name = str(raw_entry.get("display_name") or marker_key).strip()
                obs_id = f"obs_{marker_key}_v1_{idx}"
                val = raw_entry.get("value", raw_entry.get("raw_value", ""))
                explicit_unit = raw_entry.get("unit")
                raw_s_date = raw_entry.get("sample_date")
                norm_s_date, is_valid_date = normalize_lab_date(raw_s_date) if raw_s_date else (None, True)
                sample_date = norm_s_date if is_valid_date else None
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

                is_unc = bool(raw_entry.get("is_uncertain", False)) or (sample_date is None)
                unc_note = raw_entry.get("uncertainty_note")
                if sample_date is None and (not unc_note or "Missing specimen collection date" not in unc_note):
                    unc_note = f"{unc_note}; Missing specimen collection date on laboratory report" if unc_note else "Missing specimen collection date on laboratory report"
                if num_val is not None and not resolved_unit and not is_dimensionless_marker(marker_key):
                    is_unc = True
                    if not unc_note or "Missing unit of measurement" not in unc_note:
                        unc_note = f"{unc_note}; Missing unit of measurement for numeric value on laboratory report" if unc_note else "Missing unit of measurement for numeric value on laboratory report"

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
                    is_uncertain=is_unc,
                    uncertainty_note=unc_note,
                    is_recognition_ambiguous=bool(raw_entry.get("is_recognition_ambiguous", False)),
                    recorded_at=recorded_at,
                    study_id=raw_entry.get("study_id") or raw_entry.get("order_id"),
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
                    raw_s_date = raw_entry.get("sample_date")
                    norm_s_date, is_valid_date = normalize_lab_date(raw_s_date) if raw_s_date else (None, True)
                    sample_date = norm_s_date if is_valid_date else None
                    ref_raw = raw_entry.get("reference_range")
                    ref_obj = (
                        LabReferenceRange.model_validate(ref_raw)
                        if isinstance(ref_raw, dict)
                        else None
                    )
                    crit_flag = bool(raw_entry.get("is_critical_flag", False))
                    num_val, resolved_unit = self._parse_numeric_and_unit(val, explicit_unit)
                    is_unc = bool(raw_entry.get("is_uncertain", False)) or (sample_date is None)
                    unc_note = raw_entry.get("uncertainty_note")
                    if sample_date is None and (not unc_note or "Missing specimen collection date" not in unc_note):
                        unc_note = f"{unc_note}; Missing specimen collection date on laboratory report" if unc_note else "Missing specimen collection date on laboratory report"
                    if num_val is not None and not resolved_unit and not is_dimensionless_marker(marker_key):
                        is_unc = True
                        if not unc_note or "Missing unit of measurement" not in unc_note:
                            unc_note = f"{unc_note}; Missing unit of measurement for numeric value on laboratory report" if unc_note else "Missing unit of measurement for numeric value on laboratory report"
                    is_rec_amb = bool(raw_entry.get("is_recognition_ambiguous", False))
                else:
                    val = raw_entry
                    num_val, resolved_unit = self._parse_numeric_and_unit(val, None)
                    sample_date = None
                    ref_obj = None
                    crit_flag = False
                    is_unc = True
                    unc_note = "Missing specimen collection date on laboratory report"
                    is_rec_amb = False

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
                    is_uncertain=is_unc,
                    uncertainty_note=unc_note,
                    is_recognition_ambiguous=is_rec_amb,
                    recorded_at=recorded_at,
                    study_id=raw_entry.get("study_id") or raw_entry.get("order_id") if isinstance(raw_entry, dict) else None,
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
                    "recorded_at": self._now_utc().isoformat(),
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
                "is_uncertain": bool(obs.get("is_uncertain", False)),
                "uncertainty_note": obs.get("uncertainty_note"),
                "is_recognition_ambiguous": bool(obs.get("is_recognition_ambiguous", False)),
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

        active_markers = {}
        for k, v in raw_active.items():
            if isinstance(v, dict):
                v_copy = dict(v)
                v_copy.setdefault("observation_id", f"obs_{k}")
                v_copy.setdefault("display_name", k)
                v_copy.setdefault("recorded_at", content.get("recorded_at") or self._now_utc().isoformat())
                try:
                    active_markers[k] = LabObservationEntry.model_validate(v_copy)
                except Exception:
                    pass
            elif isinstance(v, LabObservationEntry):
                active_markers[k] = v

        observations_history = []
        for item in raw_history:
            if isinstance(item, dict):
                item_copy = dict(item)
                item_copy.setdefault("observation_id", f"obs_hist_{len(observations_history)}")
                item_copy.setdefault("display_name", item_copy.get("marker_key", "unknown"))
                item_copy.setdefault("recorded_at", content.get("recorded_at") or self._now_utc().isoformat())
                try:
                    observations_history.append(LabObservationEntry.model_validate(item_copy))
                except Exception:
                    pass
            elif isinstance(item, LabObservationEntry):
                observations_history.append(item)

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

    def is_reliable_study_duplicate(
        self,
        markers: List[Any],
        sample_date: Optional[str],
        study_id: Optional[str],
        observations_history: List[Dict[str, Any] | LabObservationEntry],
    ) -> bool:
        """
        Determines whether an incoming set of markers on sample_date (and optional study_id)
        is an exact duplicate of ONE concrete previously registered study in the ledger history.

        Whole-Study Identity Rules (C-7/C-8 V3):
        1. Date identity: sample_date must be present and non-empty. Unknown date is never merged.
        2. Single-marker ambiguous update: if len(markers) < 2 and not study_id, never merge automatically.
        3. One-to-one study matching: Candidate historical studies are partitioned by:
           - Explicit study_id: ("study_id", sample_date, study_id)
           - Ingestion transaction: ("recorded_at", sample_date, root_recorded_at)
           Matches are NEVER assembled by cherry-picking individual observations across different studies.
        4. Composition match: The set of normalized marker keys in the incoming study must match
           the candidate historical study's marker key set EXACTLY (in_keys == h_keys).
        5. Value equivalence: All marker values and units must match using exact Decimal comparison
           (are_lab_values_equivalent).
        """
        if not markers:
            return False
        if not sample_date or not sample_date.strip():
            return False

        norm_s_date, is_valid_date = normalize_lab_date(sample_date)
        if not is_valid_date or not norm_s_date:
            return False

        # Single marker without explicit study_id / order_id cannot be safely deduplicated
        clean_sid = str(study_id).strip() if (study_id and str(study_id).strip()) else None
        if len(markers) < 2 and not clean_sid:
            return False

        # Build lookup of history entries by observation_id to trace root recorded_at for corrections
        history_by_id: Dict[str, Dict[str, Any]] = {}
        for h in observations_history:
            h_dict = h.model_dump() if hasattr(h, "model_dump") else h
            obs_id = h_dict.get("observation_id")
            if obs_id:
                history_by_id[obs_id] = h_dict

        def _get_root_recorded_at(obs: Dict[str, Any]) -> str:
            curr = obs
            visited = set()
            while curr.get("supersedes_observation_id") and curr.get("supersedes_observation_id") in history_by_id:
                prev_id = curr["supersedes_observation_id"]
                if prev_id in visited:
                    break
                visited.add(prev_id)
                curr = history_by_id[prev_id]
            return curr.get("recorded_at") or obs.get("recorded_at") or ""

        # Group currently ACTIVE historical observations by their concrete registered study
        hist_studies: Dict[Tuple[str, str, str], Dict[str, Dict[str, Any]]] = {}
        for h in observations_history:
            h_dict = h.model_dump() if hasattr(h, "model_dump") else h
            if not h_dict.get("is_active", True):
                continue

            h_date = h_dict.get("sample_date")
            if h_date != norm_s_date:
                continue

            h_sid_raw = h_dict.get("study_id")
            h_sid = str(h_sid_raw).strip() if (h_sid_raw and str(h_sid_raw).strip()) else None
            if h_sid:
                study_key = ("study_id", norm_s_date, h_sid)
            else:
                root_rec = _get_root_recorded_at(h_dict)
                study_key = ("recorded_at", norm_s_date, root_rec)

            norm_k = self._normalize_marker_key(h_dict.get("marker_key", ""))
            hist_studies.setdefault(study_key, {})[norm_k] = h_dict

        # Collect incoming study normalized markers
        in_markers_map: Dict[str, Tuple[Any, Optional[float], Optional[str]]] = {}
        for m in markers:
            m_k = getattr(m, "marker_key", "")
            norm_k = self._normalize_marker_key(m_k)
            val = getattr(m, "value", None)
            if val is None:
                val = getattr(m, "raw_value", "")
            unit = getattr(m, "unit", None)
            num_val = getattr(m, "numeric_value", None)
            if num_val is None:
                num_val, resolved_unit = self._parse_numeric_and_unit(val, unit)
                if unit is None:
                    unit = resolved_unit
            in_markers_map[norm_k] = (val, num_val, unit)

        in_keys = set(in_markers_map.keys())

        # Check against each concrete registered historical study
        for h_study_key, h_markers in hist_studies.items():
            # 1. Study ID check
            key_type, _, key_id = h_study_key
            if clean_sid:
                if key_type != "study_id" or key_id != clean_sid:
                    continue
            else:
                if key_type == "study_id":
                    continue

            # 2. Composition check: sets of markers must match EXACTLY
            h_keys = set(h_markers.keys())
            if in_keys != h_keys:
                continue

            # 3. Values and units must match for all markers
            all_match = True
            for k, (in_val, in_num, in_unit) in in_markers_map.items():
                h_obs = h_markers[k]
                h_val = h_obs.get("value")
                h_num = h_obs.get("numeric_value")
                h_unit = h_obs.get("unit")
                if not are_lab_values_equivalent(in_val, in_num, in_unit, h_val, h_num, h_unit):
                    all_match = False
                    break

            if all_match:
                return True

        return False

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

                    # Determine duplicate studies across validated_mutations as whole studies (Task 1):
                    study_groups: Dict[Tuple[Optional[str], Optional[str]], List[LabMarkerMutationItem]] = {}
                    for mut in validated_mutations:
                        if mut.event_type not in (LabEventType.INVALIDATED, LabEventType.CORRECTION):
                            study_groups.setdefault((mut.sample_date, mut.study_id), []).append(mut)

                    duplicate_study_keys = set()
                    for (s_date, study_id), group in study_groups.items():
                        if self.is_reliable_study_duplicate(
                            markers=group,
                            sample_date=s_date,
                            study_id=study_id,
                            observations_history=history_list,
                        ):
                            duplicate_study_keys.add((s_date, study_id))

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
                                is_uncertain=prior_active.get("is_uncertain", False) if prior_active else mut.is_uncertain,
                                uncertainty_note=prior_active.get("uncertainty_note") if prior_active else mut.uncertainty_note,
                                is_recognition_ambiguous=prior_active.get("is_recognition_ambiguous", False) if prior_active else mut.is_recognition_ambiguous,
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

                            # CORRECTION explicit unit semantics (Task 3):
                            # 1. Field unit omitted in request -> preserve superseded observation's unit
                            # 2. Field unit explicitly passed as null -> reset unit to None
                            # 3. Field unit explicitly passed as string -> set specified unit
                            if "unit" not in mut.model_fields_set:
                                fallback_unit = superseded_obs.get("unit")
                                num_val, resolved_unit = self._parse_numeric_and_unit(mut.value, fallback_unit)
                            elif mut.unit is None:
                                num_val, _ = self._parse_numeric_and_unit(mut.value, None)
                                resolved_unit = None
                            else:
                                num_val, resolved_unit = self._parse_numeric_and_unit(mut.value, mut.unit)
                            resolved_date = mut.sample_date or superseded_obs.get("sample_date")
                            resolved_ref = mut.reference_range or (
                                LabReferenceRange.model_validate(superseded_obs["reference_range"])
                                if superseded_obs.get("reference_range")
                                else None
                            )
                            crit = self._detect_critical_deviation(num_val, resolved_ref, mut.is_critical_flag)

                            is_unc = mut.is_uncertain or (resolved_date is None)
                            unc_note = mut.uncertainty_note
                            if resolved_date is None and (not unc_note or "Missing specimen collection date" not in unc_note):
                                unc_note = f"{unc_note}; Missing specimen collection date on laboratory report" if unc_note else "Missing specimen collection date on laboratory report"
                            if (
                                num_val is not None
                                and not resolved_unit
                                and not is_dimensionless_marker(marker_key)
                                and (not unc_note or "Missing unit of measurement" not in unc_note)
                            ):
                                is_unc = True
                                unc_note = f"{unc_note}; Missing unit of measurement for numeric value on laboratory report" if unc_note else "Missing unit of measurement for numeric value on laboratory report"

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
                                is_uncertain=is_unc,
                                uncertainty_note=unc_note,
                                is_recognition_ambiguous=mut.is_recognition_ambiguous,
                                recorded_at=now_iso,
                                study_id=mut.study_id,
                                is_user_corrected=mut.is_user_corrected,
                            )
                            dumped_corr = corr_entry.model_dump()
                            history_list.append(dumped_corr)
                            active_map[marker_key] = dumped_corr
                            continue

                        # LabEventType.INITIAL or LabEventType.NEW_STUDY
                        # Whole-study duplicate check (Task 1):
                        # Deduplication operates on the whole study group, not marker-by-marker.
                        if (mut.sample_date, mut.study_id) in duplicate_study_keys:
                            continue

                        # Strictly forbid inheriting units from prior_active for initial/new study (Task 2)
                        num_val, resolved_unit = self._parse_numeric_and_unit(mut.value, mut.unit)
                        resolved_date = mut.sample_date

                        effective_event = (
                            LabEventType.INITIAL if prior_active is None and mut.event_type == LabEventType.INITIAL else (
                                LabEventType.NEW_STUDY if prior_active is not None else mut.event_type
                            )
                        )
                        resolved_ref = mut.reference_range or (
                            LabReferenceRange.model_validate(prior_active["reference_range"])
                            if prior_active and prior_active.get("reference_range")
                            else None
                        )
                        crit = self._detect_critical_deviation(num_val, resolved_ref, mut.is_critical_flag)

                        is_unc = mut.is_uncertain or (resolved_date is None)
                        unc_note = mut.uncertainty_note
                        if resolved_date is None and (not unc_note or "Missing specimen collection date" not in unc_note):
                            unc_note = f"{unc_note}; Missing specimen collection date on laboratory report" if unc_note else "Missing specimen collection date on laboratory report"
                        if (
                            num_val is not None
                            and not resolved_unit
                            and not is_dimensionless_marker(marker_key)
                            and (not unc_note or "Missing unit of measurement" not in unc_note)
                        ):
                            is_unc = True
                            unc_note = f"{unc_note}; Missing unit of measurement for numeric value on laboratory report" if unc_note else "Missing unit of measurement for numeric value on laboratory report"

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
                            is_uncertain=is_unc,
                            uncertainty_note=unc_note,
                            is_recognition_ambiguous=mut.is_recognition_ambiguous,
                            recorded_at=now_iso,
                            study_id=mut.study_id,
                            is_user_corrected=mut.is_user_corrected,
                        )
                        dumped_new = new_entry.model_dump()
                        history_list.append(dumped_new)
                        if (
                            prior_active is None
                            or prior_active.get("sample_date") is None
                            or resolved_date is None
                            or str(resolved_date) >= str(prior_active.get("sample_date", ""))
                        ):
                            active_map[marker_key] = dumped_new

                    # If all mutations were exact active duplicates and no changes were made:
                    if len(history_list) == len(current_state.observations_history):
                        return current_state

                    shared_labs_table = self._build_shared_labs_table_from_active(active_map)
                    self.report_repo.create_next_version(
                        cycle_id=cycle_id,
                        model="aegis-lab-ledger",
                        report_type=LABORATORY_LEDGER_REPORT_TYPE,
                        privacy_level=PrivacyLevel.SERVER_ONLY,
                        content={
                            "ledger_version": next_ledger_ver,
                            "source": "user_or_clinical_update",
                            "recorded_at": self._now_utc().isoformat(),
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
            is_new_study_event = (not same_observation) and (not is_error_correction) and (
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

            if is_error_correction:
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
    def _coordination_lock_id(cycle_id: str) -> str:
        return f"lock_consilium_coord_{cycle_id}"[:64]

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

    def _fetch_fresh_coordination_lock_row(self, cycle_id: str) -> Optional[ModelReport]:
        """
        Queries the cross-process cycle coordination lock row directly from SQLite using `.populate_existing()`.
        """
        coord_id = self._coordination_lock_id(cycle_id)
        return (
            self.db.query(ModelReport)
            .filter(ModelReport.report_id == coord_id)
            .populate_existing()
            .first()
        )

    def _build_lease_conflict_status(
        self, cycle_id: str, message: str
    ) -> LabRevisionCooldownStatus:
        """
        Builds a fail-closed LabRevisionCooldownStatus for lease/fencing conflicts
        without touching `self.db` (safe to call from background heartbeat sessions).
        """
        now_iso = self._now_utc().isoformat()
        return LabRevisionCooldownStatus(
            cycle_id=cycle_id,
            cooldown_hours=LAB_REVISION_COOLDOWN_HOURS,
            is_allowed=False,
            remaining_seconds=self.lease_ttl_seconds,
            remaining_hours=round(self.lease_ttl_seconds / 3600.0, 2),
            last_completed_at_utc=now_iso,
            next_available_at_utc=now_iso,
            has_unreviewed_lab_changes=True,
            evaluation_status="pending_full_consilium_revision",
            message=message,
            safety_notice=MEDICAL_SAFETY_COOLDOWN_NOTICE,
        )

    def _get_active_in_flight_lock(self, cycle_id: str) -> Optional[ModelReport]:
        """
        Checks for an active cross-process execution lease row in `model_reports`.
        Checks both the cycle coordination lock and lab-revision-specific lock.
        If an expired lease row exists from a hard-terminated or hung process (> TTL without renewal),
        cleans it up atomically using a WHERE condition matching the exact stale owner and expiration,
        ensuring that a concurrent worker's active or renewed lease is NEVER deleted.
        """
        coord_id = self._coordination_lock_id(cycle_id)
        lock_id = self._lock_report_id(cycle_id)
        all_cycle_lock_ids = [
            coord_id,
            lock_id,
            f"lock_primary_{cycle_id}"[:64],
            f"lock_weekly_{cycle_id}"[:64],
        ]
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
            expires_dt = self._parse_iso_utc(stale_expires_iso)
            if expires_dt is not None and expires_dt <= now_dt and stale_owner_id and stale_expires_iso:
                stmt = (
                    delete(ModelReport)
                    .where(ModelReport.report_id.in_(all_cycle_lock_ids))
                    .where(ModelReport.content["lease_owner_id"].as_string() == stale_owner_id)
                    .where(ModelReport.content["expires_at_utc"].as_string() == stale_expires_iso)
                )
                self.db.execute(stmt)
                self.db.commit()
                coord_row = None

        # Cleanup expired lab-revision lock if stale
        if lock_row is not None:
            lock_content = lock_row.content or {}
            stale_owner_id = lock_content.get("lease_owner_id")
            stale_expires_iso = lock_content.get("expires_at_utc")
            expires_dt = self._parse_iso_utc(stale_expires_iso)
            if expires_dt is not None and expires_dt <= now_dt and stale_owner_id and stale_expires_iso:
                stmt = (
                    delete(ModelReport)
                    .where(ModelReport.report_id.in_([coord_id, lock_id]))
                    .where(ModelReport.content["lease_owner_id"].as_string() == stale_owner_id)
                    .where(ModelReport.content["expires_at_utc"].as_string() == stale_expires_iso)
                )
                self.db.execute(stmt)
                self.db.commit()
                lock_row = None

        # Re-verify active rows
        fresh_lock = (
            self.db.query(ModelReport)
            .filter(ModelReport.report_id == lock_id)
            .populate_existing()
            .first()
        )
        if fresh_lock is not None:
            exp = self._parse_iso_utc((fresh_lock.content or {}).get("expires_at_utc"))
            if exp is not None and exp > now_dt:
                return fresh_lock

        fresh_coord = (
            self.db.query(ModelReport)
            .filter(ModelReport.report_id == coord_id)
            .populate_existing()
            .first()
        )
        if fresh_coord is not None:
            exp = self._parse_iso_utc((fresh_coord.content or {}).get("expires_at_utc"))
            if exp is not None and exp > now_dt:
                return fresh_coord

        return None

    def _acquire_cross_process_lease(
        self,
        cycle_id: str,
        check_cooldown: bool = True,
    ) -> str:
        """
        Acquires an atomic database-level execution lease backed by the SQLite PRIMARY KEY
        constraint on `model_reports.report_id` for both the master cycle coordination lock
        (`report_id = lock_consilium_coord_{cycle_id}`) and lab-revision-specific lock
        (`report_id = lock_lab_rev_{cycle_id}`), tagged with a unique `lease_owner_id`
        fencing token. Guarantees zero cross-consilium race conditions with Primary and Weekly Consilium.
        """
        if check_cooldown:
            cooldown_status = self.get_cooldown_status(cycle_id)
            if not cooldown_status.is_allowed:
                raise ConsiliumCooldownError(cooldown_status)
        else:
            active_in_flight = self._get_active_in_flight_lock(cycle_id)
            if active_in_flight is not None:
                raise ConsiliumConcurrencyConflictError(
                    f"Consilium already in progress for cycle '{cycle_id}'."
                )

        ledger_state = self.get_lab_ledger_state(cycle_id)
        if ledger_state.ledger_version == 0:
            raise ConsiliumContextMissingError(
                f"Cannot run Lab-Revision Consilium for cycle '{cycle_id}': no laboratory data is stored."
            )
        if check_cooldown and not ledger_state.has_unreviewed_lab_changes:
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
        coord_id = self._coordination_lock_id(cycle_id)
        lock_id = self._lock_report_id(cycle_id)
        lease_owner_id = f"lease_lab_rev_{uuid.uuid4().hex}"

        try:
            lock_payload = {
                "lease_owner_id": lease_owner_id,
                "consilium_type": "lab_revision",
                "acquired_at_utc": now_dt.isoformat(),
                "renewed_at_utc": now_dt.isoformat(),
                "expires_at_utc": expires_dt.isoformat(),
                "renewal_count": 0,
            }
            coord_report = ModelReport(
                report_id=coord_id,
                cycle_id=cycle_id,
                sprint_id=None,
                session_id=None,
                model="aegis-concurrency-guard",
                report_type=CYCLE_CONSILIUM_COORDINATION_LOCK_REPORT_TYPE,
                privacy_level=PrivacyLevel.SERVER_ONLY.value,
                version=1,
                content=dict(lock_payload),
                created_at=now_dt,
            )
            lock_report = ModelReport(
                report_id=lock_id,
                cycle_id=cycle_id,
                sprint_id=None,
                session_id=None,
                model="aegis-concurrency-guard",
                report_type=LAB_REVISION_IN_FLIGHT_LOCK_REPORT_TYPE,
                privacy_level=PrivacyLevel.SERVER_ONLY.value,
                version=1,
                content=dict(lock_payload),
                created_at=now_dt,
            )
            self.db.add(coord_report)
            self.db.add(lock_report)
            self.db.commit()
            return lease_owner_id
        except (IntegrityError, OperationalError):
            self.db.rollback()
            status_after = self.get_cooldown_status(cycle_id)
            if status_after.is_allowed:
                status_after = status_after.model_copy(
                    update={
                        "is_allowed": False,
                        "message": (
                            f"Concurrent consilium execution conflict for cycle '{cycle_id}'. "
                            f"Another consilium is already in progress. {MEDICAL_SAFETY_COOLDOWN_NOTICE}"
                        ),
                    }
                )
            raise ConsiliumCooldownError(status_after)

    def _renew_cross_process_lease(
        self, cycle_id: str, lease_owner_id: str, db: Optional[Session] = None
    ) -> None:
        """
        Verifies that `lease_owner_id` still owns both SQLite execution lease rows
        (`lock_consilium_coord_{cycle_id}` and `lock_lab_rev_{cycle_id}`) and that neither
        lease has expired, then extends `expires_at_utc` by `self.lease_ttl_seconds` using
        an atomic SQLite UPDATE. If either lease row was lost, expired, or replaced by another
        owner, `res.rowcount != 2`, raising `ConsiliumCooldownError` immediately (fencing guard).
        Never uses `self.db` when an isolated `db` session is provided by the background heartbeat.
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
            raise ConsiliumCooldownError(
                self._build_lease_conflict_status(
                    cycle_id=cycle_id,
                    message=(
                        f"Execution lease '{lease_owner_id}' for cycle '{cycle_id}' is no longer "
                        "owned by this worker; aborting stale consilium execution."
                    ),
                )
            )

        now_dt = self._now_utc()
        lock_exp = self._parse_iso_utc(lock_content.get("expires_at_utc"))
        if lock_exp is not None and lock_exp <= now_dt:
            raise ConsiliumCooldownError(
                self._build_lease_conflict_status(
                    cycle_id=cycle_id,
                    message=(
                        f"Execution lease '{lease_owner_id}' for cycle '{cycle_id}' has expired "
                        "and is no longer owned by this worker; aborting stale consilium execution."
                    ),
                )
            )

        new_expires_dt = now_dt + timedelta(seconds=self.lease_ttl_seconds)
        updated_content = dict(lock_content)
        updated_content["renewed_at_utc"] = now_dt.isoformat()
        updated_content["expires_at_utc"] = new_expires_dt.isoformat()
        updated_content["renewal_count"] = int(updated_content.get("renewal_count", 0)) + 1

        stmt = (
            update(ModelReport)
            .where(ModelReport.report_id.in_([coord_id, lock_id]))
            .where(ModelReport.content["lease_owner_id"].as_string() == lease_owner_id)
            .where(ModelReport.content["expires_at_utc"].as_string() > now_dt.isoformat())
            .values(content=updated_content, version=ModelReport.version + 1)
        )
        res = target_db.execute(stmt)
        if res.rowcount != 2:
            target_db.rollback()
            raise ConsiliumCooldownError(
                self._build_lease_conflict_status(
                    cycle_id=cycle_id,
                    message=(
                        f"Execution lease '{lease_owner_id}' for cycle '{cycle_id}' was partially or completely lost "
                        f"or is no longer owned by this worker (expected 2 lock rows renewed, got {res.rowcount}); "
                        "aborting stale consilium execution."
                    ),
                )
            )
        target_db.commit()

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
        Fencing check before final atomic persistence: verifies that both the cycle
        coordination lease and the lab-revision lease are still owned by `lease_owner_id`
        and unexpired, and deletes both rows inside the uncommitted transaction using
        atomic predicates matching `lease_owner_id` and `expires_at_utc > now_iso`.
        If rowcount < 2, the lease was lost, expired, or superseded; rolls back any
        partial delete and raises ConsiliumCooldownError, blocking publication of all
        clinical reports and Athena strategy.
        Never deletes a lease owned by another worker.
        """
        coord_id = self._coordination_lock_id(cycle_id)
        lock_id = self._lock_report_id(cycle_id)
        now_iso = self._now_utc().isoformat()
        stmt = (
            delete(ModelReport)
            .where(ModelReport.report_id.in_([coord_id, lock_id]))
            .where(ModelReport.content["lease_owner_id"].as_string() == lease_owner_id)
            .where(ModelReport.content["expires_at_utc"].as_string() > now_iso)
        )
        res = self.db.execute(stmt)
        if res.rowcount < 2:
            self.db.rollback()
            raise ConsiliumCooldownError(
                self._build_lease_conflict_status(
                    cycle_id=cycle_id,
                    message=(
                        f"Cannot commit Lab-Revision Consilium for cycle '{cycle_id}': "
                        f"execution lease '{lease_owner_id}' was lost or superseded by another owner."
                    ),
                )
            )

    def _release_cross_process_lease_safe(self, cycle_id: str, lease_owner_id: str) -> None:
        """
        Safely releases the cross-process execution lease on error ONLY if the lock rows
        are still owned by `lease_owner_id`. Never deletes a lock acquired by another worker.
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
            self.db.expire_all()
        except Exception:
            pass

    def get_cooldown_status(self, cycle_id: str) -> LabRevisionCooldownStatus:
        """
        Evaluates the 168-hour (7-day) cooldown for user-initiated Full Lab-Revision Consilium:
        - Uses authoritative server UTC time (`self._now_utc()`), never client `report_date`.
        - Checks for any active cross-process in-flight execution lease (`lab_revision_in_flight_lock`
          or `cycle_consilium_coordination_lock`).
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
            consilium_type = lock_c.get("consilium_type", "lab_revision")
            acquired_iso = str(lock_c.get("acquired_at_utc") or now_dt.isoformat())
            expires_dt = self._parse_iso_utc(lock_c.get("expires_at_utc")) or (
                now_dt + timedelta(seconds=self.lease_ttl_seconds)
            )
            rem_sec = max(1, int(math.ceil((expires_dt - now_dt).total_seconds())))
            rem_hrs = round(rem_sec / 3600.0, 2)
            if consilium_type == "primary":
                in_progress_label = f"Primary Consilium is already in progress for cycle '{cycle_id}'."
            elif consilium_type == "weekly":
                in_progress_label = f"Weekly Consilium is already in progress for cycle '{cycle_id}'."
            else:
                in_progress_label = f"Full Lab-Revision Consilium is already in progress for cycle '{cycle_id}'."
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
                    f"{in_progress_label} "
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

    def _load_preparatory_ecg_and_vitals(self, cycle_id: str) -> Dict[str, Any]:
        from app.services.ecg_context_service import assemble_consilium_ecg_context

        ecg_ctx = assemble_consilium_ecg_context(self.db, cycle_id)
        ecg_summary = ecg_ctx.ecg_summary

        measurements = self.measurement_repo.list_by_cycle(cycle_id=cycle_id, is_historical=False, is_simulated=False, limit=50)
        systolic = next((m.systolic for m in reversed(measurements) if m.systolic is not None), None)
        diastolic = next((m.diastolic for m in reversed(measurements) if m.diastolic is not None), None)
        pulse = next((m.pulse for m in reversed(measurements) if m.pulse is not None), None)
        spo2 = next((m.spo2 for m in reversed(measurements) if m.spo2 is not None), None)

        return {
            "blood_pressure": f"{systolic if systolic is not None else '--'}/{diastolic if diastolic is not None else '--'} mmHg",
            "heart_rate": f"{pulse if pulse is not None else '--'} bpm",
            "spo2": f"{spo2 if spo2 is not None else '--'}%",
            "ecg_summary": ecg_summary,
            "ecg_context": ecg_ctx.to_dict(),
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
                ecg_context_dict = vitals_stream.get("ecg_context") or {}

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
                        "ecg_context": ecg_context_dict,
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
                            "ecg_context": ecg_context_dict,
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

                    # Re-verify cycle active status right before DB commit
                    fresh_cycle = self.cycle_repo.get_fresh(cycle_id)
                    if fresh_cycle is None or fresh_cycle.status != "active":
                        self.db.rollback()
                        raise ConsiliumCycleInactiveError(
                            f"Lab-Revision Consilium aborting commit for cycle '{cycle_id}': "
                            f"cycle is no longer active (status='{getattr(fresh_cycle, 'status', None)}')."
                        )

                    self.db.commit()
                except BaseException:
                    self.db.rollback()
                    raise
            except BaseException:
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

    # ==========================================================================
    # 6. STAGE 6B: AUTOMATIC TEN-MODEL BACKGROUND LAB REVISION
    # ==========================================================================

    def _load_continuity_context(self, cycle_id: str) -> Dict[str, Any]:
        """
        Loads previous conclusions for all 3 tiers with versions and timestamps:
        - 8 organ specialists
        - Deputy Chief Physician (Flash)
        - Chief Physician (Pro)
        - Active Pro Strategy (read-only baseline)
        If any prior conclusion is absent, explicitly marks it as absent with clear notation.
        """
        specialists: Dict[str, Dict[str, Any]] = {}
        for system_key in SYSTEM_KEYS:
            role_val = AegisAgentRole.for_system_key(system_key).value
            sys_reports = self.report_repo.list_server_only(
                cycle_id=cycle_id,
                report_type=role_val,
            )
            if sys_reports:
                latest = sys_reports[0]
                c = latest.content or {}
                specialists[system_key] = {
                    "status": "present",
                    "report_id": latest.report_id,
                    "version": latest.version,
                    "created_at_utc": latest.created_at.isoformat() if latest.created_at else None,
                    "summary": (c.get("system_logic") or {}).get("system_general_shelf") or c.get("summary") or "N/A",
                    "system_view": c.get("system_view", {}),
                    "system_logic": c.get("system_logic", {}),
                    "training_sandbox": c.get("training_sandbox", {}),
                }
            else:
                specialists[system_key] = {
                    "status": "absent",
                    "note": f"No previous specialist report available for '{system_key}'.",
                    "summary": "N/A - No previous specialist report available",
                }

        # Deputy Flash
        deputy_reports = self.report_repo.list_server_only(
            cycle_id=cycle_id,
            report_type="deputy_chief_physician_internal",
        )
        if not deputy_reports:
            deputy_reports = self.report_repo.list_server_only(
                cycle_id=cycle_id,
                report_type="lab_revision_deputy_internal",
            )
        if deputy_reports:
            latest_d = deputy_reports[0]
            dc = latest_d.content or {}
            prev_flash = {
                "status": "present",
                "report_id": latest_d.report_id,
                "version": latest_d.version,
                "created_at_utc": latest_d.created_at.isoformat() if latest_d.created_at else None,
                "summary": dc.get("flash_general_shelf") or dc.get("deputy_synthesis") or "N/A",
                "training_shelf": dc.get("flash_training_shelf") or "N/A",
            }
        else:
            prev_flash = {
                "status": "absent",
                "note": "No previous deputy conclusion available.",
                "summary": "N/A - No previous deputy conclusion available",
            }

        # Chief Pro
        pro_reports = self.report_repo.list_server_only(
            cycle_id=cycle_id,
            report_type="chief_physician_internal",
        )
        if not pro_reports:
            pro_reports = self.report_repo.list_server_only(
                cycle_id=cycle_id,
                report_type="lab_revision_chief_internal",
            )
        if pro_reports:
            latest_p = pro_reports[0]
            pc = latest_p.content or {}
            prev_pro = {
                "status": "present",
                "report_id": latest_p.report_id,
                "version": latest_p.version,
                "created_at_utc": latest_p.created_at.isoformat() if latest_p.created_at else None,
                "summary": pc.get("pro_general_shelf") or pc.get("analytical_summary") or "N/A",
                "training_shelf": pc.get("pro_training_shelf") or "N/A",
            }
        else:
            prev_pro = {
                "status": "absent",
                "note": "No previous chief physician report available.",
                "summary": "N/A - No previous chief physician report available",
            }

        # Active Pro Strategy (read-only baseline)
        strat_reports = self.report_repo.list_server_only(
            cycle_id=cycle_id,
            report_type=ATHENA_ACTIVE_STRATEGY_REPORT_TYPE,
        )
        if strat_reports:
            latest_s = strat_reports[0]
            sc = latest_s.content or {}
            active_strat = {
                "status": "present",
                "report_id": latest_s.report_id,
                "version": latest_s.version,
                "created_at_utc": latest_s.created_at.isoformat() if latest_s.created_at else None,
                "strategy_text": sc.get("active_strategy_text", "N/A"),
                "summary": sc.get("active_strategy_text", "N/A"),
            }
        else:
            active_strat = {
                "status": "absent",
                "note": "No active strategy available.",
                "summary": "N/A - No active strategy available",
            }

        return {
            "specialists": specialists,
            "previous_flash": prev_flash,
            "previous_pro": prev_pro,
            "active_strategy": active_strat,
        }

    async def execute_lab_revision_consilium(
        self,
        cycle_id: str,
        target_ledger_version: Optional[int] = None,
        lease_owner_id: Optional[str] = None,
        source_document_ids: Optional[List[str]] = None,
    ) -> BackgroundLabRevisionReportRecord:
        """
        Executes the Stage 6B background Ten-Model Lab Revision Consilium:
        8 Gemini Flash-Lite specialists -> Gemini Flash deputy -> Gemini Pro chief.
        
        Invariants:
        1. Strictly analytical: does NOT update active Pro strategy, weekly plan, vitals thresholds,
           or generate user-facing messages.
        2. Preserves medical continuity: previous conclusions of all 3 tiers (with dates/versions)
           are passed into the models. If absent, explicitly designated as absent.
        3. Persisted SERVER_ONLY in ModelReportRepository under 'background_lab_revision_report'.
        4. Fenced concurrency: protected by in-process asyncio.Lock and cross-process SQLite lease.
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
            own_lease = False
            effective_lease = lease_owner_id
            if effective_lease is None:
                effective_lease = self._acquire_cross_process_lease(cycle_id, check_cooldown=False)
                own_lease = True
            else:
                self._renew_cross_process_lease(cycle_id, effective_lease)

            try:
                started_at_iso = self._now_utc().isoformat()
                ledger_state = self.get_lab_ledger_state(cycle_id)
                eval_version = target_ledger_version if target_ledger_version is not None else ledger_state.ledger_version
                baseline_version = ledger_state.last_evaluated_ledger_version

                if eval_version <= baseline_version:
                    raise ValueError(
                        f"No unreviewed laboratory changes to evaluate: target ledger v{eval_version} "
                        f"<= baseline evaluated v{baseline_version}."
                    )

                diff_report = self.compute_laboratory_diff(
                    cycle_id=cycle_id,
                    baseline_version=baseline_version,
                    target_version=eval_version,
                )
                diff_summary_dict = diff_report.model_dump()

                anamnesis_m1 = self._load_preparatory_anamnesis(cycle_id)
                vitals_stream = self._load_preparatory_ecg_and_vitals(cycle_id)
                ecg_context_dict = vitals_stream.get("ecg_context") or {}

                active_markers_raw = {
                    k: obs.model_dump() for k, obs in ledger_state.active_markers.items()
                }
                shared_labs_table = self._build_shared_labs_table_from_active(active_markers_raw)
                continuity_ctx = self._load_continuity_context(cycle_id)

                # 1. STAGE 1: 8 Organ Specialists (Gemini Flash-Lite)
                specialist_outputs: Dict[str, SystemAgentOutput] = {}
                for system_key in SYSTEM_KEYS:
                    organ_fact = getattr(anamnesis_m1.system_logic, system_key)
                    input_package = {
                        "system_key": system_key,
                        "facts": organ_fact.data,
                        "first_model_short_summary": organ_fact.internal_comment,
                        "shared_labs_table": shared_labs_table,
                        "vitals_stream": vitals_stream,
                        "ecg_context": ecg_context_dict,
                    }
                    prev_organ = continuity_ctx["specialists"].get(system_key, {})
                    prev_consilium_summary = {
                        "previous_deputy_flash_summary": continuity_ctx["previous_flash"].get("summary", "N/A"),
                        "previous_chief_pro_summary": continuity_ctx["previous_pro"].get("summary", "N/A"),
                        "active_pro_strategy": continuity_ctx["active_strategy"].get("summary", "N/A"),
                    }
                    system_prompt = build_lab_revision_system_agent_prompt(
                        system_key=system_key,
                        input_package=input_package,
                        lab_diff_summary=diff_summary_dict,
                        previous_organ_output=prev_organ,
                        previous_consilium_summary=prev_consilium_summary,
                    )
                    organ_result: SystemAgentOutput = await self._execute_with_lease_renewal(
                        cycle_id,
                        effective_lease,
                        lambda sp=system_prompt: self.gateway.generate_structured(
                            prompt=sp,
                            response_schema=SystemAgentOutput,
                            model=GeminiModelRole.FLASH_LITE,
                        ),
                    )
                    specialist_outputs[system_key] = organ_result

                # 2. STAGE 2: Deputy Chief Physician (Gemini Flash)
                comparative_flash_lines = [
                    "=== ANAMNESIS AND LABORATORY DATA ===",
                    json.dumps(
                        {
                            "user_view": anamnesis_m1.user_view.model_dump(),
                            "training_sandbox": anamnesis_m1.training_sandbox.model_dump(),
                            "shared_labs_shelf": shared_labs_table,
                            "ecg_context": ecg_context_dict,
                        },
                        ensure_ascii=False,
                    ),
                    "",
                    "=== DETERMINISTIC LABORATORY DIFF ===",
                    json.dumps(diff_summary_dict, ensure_ascii=False),
                    "",
                    "=== VITALS STREAM ===",
                    f"Blood pressure: {vitals_stream['blood_pressure']}",
                    f"Heart rate: {vitals_stream['heart_rate']}",
                    f"SpO2: {vitals_stream['spo2']}",
                    f"ECG: {vitals_stream['ecg_summary']}",
                    "",
                    "=== PREVIOUS DEPUTY & CHIEF CONCLUSIONS ===",
                    json.dumps(
                        {
                            "previous_deputy_flash": continuity_ctx["previous_flash"],
                            "previous_chief_pro": continuity_ctx["previous_pro"],
                            "active_pro_strategy": continuity_ctx["active_strategy"],
                        },
                        ensure_ascii=False,
                    ),
                    "",
                    "=== 8 SPECIALIST REPORTS (CURRENT EVALUATION) ===",
                ]
                for key in SYSTEM_KEYS:
                    prev_o = continuity_ctx["specialists"].get(key, {})
                    new_o = specialist_outputs[key]
                    comparative_flash_lines.append(f"--- SYSTEM: {key.upper()} ---")
                    comparative_flash_lines.append(f"Previous conclusion: {prev_o.get('summary', 'N/A')}")
                    comparative_flash_lines.append(f"Current conclusion: {new_o.system_logic.system_general_shelf}")
                    comparative_flash_lines.append(f"Training/rationale: {new_o.training_sandbox.system_training_shelf}")
                    comparative_flash_lines.append("")

                deputy_context = "\n".join(comparative_flash_lines)
                deputy_prompt = build_background_lab_revision_deputy_prompt(deputy_context)

                deputy_output: BackgroundLabRevisionDeputyOutput = await self._execute_with_lease_renewal(
                    cycle_id,
                    effective_lease,
                    lambda: self.gateway.generate_structured(
                        prompt=deputy_prompt,
                        response_schema=BackgroundLabRevisionDeputyOutput,
                        model=GeminiModelRole.FLASH,
                    ),
                )

                # 3. STAGE 3: Chief Physician (Gemini Pro)
                pro_context_lines = [
                    deputy_context,
                    "",
                    "=== DEPUTY CHIEF PHYSICIAN SYNTHESIS (GEMINI FLASH) ===",
                    f"Deputy Synthesis: {deputy_output.deputy_synthesis}",
                    f"Identified Discrepancies: {json.dumps(deputy_output.specialist_discrepancies, ensure_ascii=False)}",
                    f"Clinical Correlations: {json.dumps(deputy_output.clinical_correlations, ensure_ascii=False)}",
                    f"Data Limitations: {json.dumps(deputy_output.data_limitations, ensure_ascii=False)}",
                    f"Draft Hypotheses: {json.dumps(deputy_output.draft_hypotheses, ensure_ascii=False)}",
                ]
                pro_context = "\n".join(pro_context_lines)
                pro_prompt = build_background_lab_revision_chief_prompt(pro_context)

                chief_output: BackgroundLabRevisionChiefOutput = await self._execute_with_lease_renewal(
                    cycle_id,
                    effective_lease,
                    lambda: self.gateway.generate_structured(
                        prompt=pro_prompt,
                        response_schema=BackgroundLabRevisionChiefOutput,
                        model=GeminiModelRole.PRO,
                    ),
                )

                # 4. Atomic Single-Transaction Persistence (SERVER_ONLY)
                completed_at_iso = self._now_utc().isoformat()
                report_id = f"lab_rev_rep_{cycle_id}_v{eval_version}_{uuid.uuid4().hex[:8]}"

                doc_ids = list(source_document_ids or [])
                if not doc_ids:
                    doc_ids = [
                        item.new_observation_id
                        for item in (diff_report.added_markers + diff_report.modified_values + diff_report.new_dated_studies)
                        if item.new_observation_id
                    ]

                try:
                    if own_lease:
                        self._verify_and_delete_cross_process_lease_uncommitted(cycle_id, effective_lease)

                    # 4.1 Save deterministic lab diff audit
                    self.report_repo.create_next_version(
                        cycle_id=cycle_id,
                        model="aegis-lab-diff-engine",
                        report_type=LABORATORY_DIFF_AUDIT_REPORT_TYPE,
                        privacy_level=PrivacyLevel.SERVER_ONLY,
                        content=diff_summary_dict,
                        commit=False,
                    )

                    # 4.2 Save 8 Flash-Lite specialists
                    specialist_summaries = {}
                    for system_key in SYSTEM_KEYS:
                        organ_out = specialist_outputs[system_key]
                        agent_role = AegisAgentRole.for_system_key(system_key)
                        saved_spec = self.report_repo.create_next_version(
                            cycle_id=cycle_id,
                            model=self.gateway.flash_lite_model,
                            report_type=agent_role.value,
                            privacy_level=PrivacyLevel.SERVER_ONLY,
                            content={
                                "agent_role": agent_role.value,
                                "consilium_kind": "background_lab_revision",
                                "evaluated_ledger_version": eval_version,
                                "system_key": system_key,
                                "system_view": organ_out.system_view.model_dump(),
                                "system_logic": organ_out.system_logic.model_dump(),
                                "training_sandbox": organ_out.training_sandbox.model_dump(),
                            },
                            commit=False,
                        )
                        specialist_summaries[system_key] = {
                            "report_id": saved_spec.report_id,
                            "version": saved_spec.version,
                            "summary": organ_out.system_logic.system_general_shelf,
                        }

                    # 4.3 Save Deputy Flash
                    saved_deputy = self.report_repo.create_next_version(
                        cycle_id=cycle_id,
                        model=self.gateway.flash_model,
                        report_type="lab_revision_deputy_internal",
                        privacy_level=PrivacyLevel.SERVER_ONLY,
                        content={
                            "agent_role": AegisAgentRole.DEPUTY_CHIEF_PHYSICIAN.value,
                            "consilium_kind": "background_lab_revision",
                            "evaluated_ledger_version": eval_version,
                            **deputy_output.model_dump(),
                        },
                        commit=False,
                    )

                    # 4.4 Save Chief Pro
                    saved_chief = self.report_repo.create_next_version(
                        cycle_id=cycle_id,
                        model=self.gateway.pro_model,
                        report_type="lab_revision_chief_internal",
                        privacy_level=PrivacyLevel.SERVER_ONLY,
                        content={
                            "agent_role": AegisAgentRole.CHIEF_PHYSICIAN.value,
                            "consilium_kind": "background_lab_revision",
                            "evaluated_ledger_version": eval_version,
                            **chief_output.model_dump(),
                        },
                        commit=False,
                    )

                    # 4.5 Save Overall Background Lab Revision Report
                    record = BackgroundLabRevisionReportRecord(
                        report_id=report_id,
                        cycle_id=cycle_id,
                        patient_id=cycle.patient_id,
                        evaluated_ledger_version=eval_version,
                        baseline_ledger_version=baseline_version,
                        source_document_ids=doc_ids,
                        study_ids=[item.marker_key for item in diff_report.new_dated_studies],
                        status="completed",
                        started_at_utc=started_at_iso,
                        completed_at_utc=completed_at_iso,
                        specialist_reports=specialist_summaries,
                        deputy_report={
                            "report_id": saved_deputy.report_id,
                            **deputy_output.model_dump(),
                        },
                        chief_report={
                            "report_id": saved_chief.report_id,
                            **chief_output.model_dump(),
                        },
                        summary=chief_output.analytical_summary,
                    )

                    self.report_repo.create_next_version(
                        cycle_id=cycle_id,
                        model=self.gateway.pro_model,
                        report_type=BACKGROUND_LAB_REVISION_REPORT_TYPE,
                        privacy_level=PrivacyLevel.SERVER_ONLY,
                        content=record.model_dump(),
                        commit=False,
                    )

                    # Re-verify cycle active status right before DB commit
                    fresh_cycle = self.cycle_repo.get_fresh(cycle_id)
                    if fresh_cycle is None or fresh_cycle.status != "active":
                        self.db.rollback()
                        raise ConsiliumCycleInactiveError(
                            f"Lab-Revision Consilium aborting commit for cycle '{cycle_id}': "
                            f"cycle is no longer active (status='{getattr(fresh_cycle, 'status', None)}')."
                        )

                    self.db.commit()
                    return record
                except BaseException:
                    self.db.rollback()
                    raise
            except BaseException:
                if own_lease:
                    self._release_cross_process_lease_safe(cycle_id, effective_lease)
                raise
