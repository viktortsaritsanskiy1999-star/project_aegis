"""
Laboratory Consilium Context Service.

Provides a unified, read-safe loader for laboratory context across consilium services
(Primary Consilium, Weekly Consilium, and downstream AI agents).

Addresses audit remediation items:
- C-1: Complete laboratory history from versioned laboratory_ledger or multiple OCR intakes,
  avoiding truncation to only the latest single preparatory_vision_ocr.
- C-2: Preserving chronological observations history (all dated studies and error corrections)
  alongside the active marker table (shared_labs_table), preventing overwrite of multiple
  measurements of the same marker across different dates.
- PII Sanitization: Strictly strips personal identifiers (patient_name, patient_id, dob,
  doctor, hospital, raw_image_url, internal debug fields) from both active shelf and history.
- Non-speculative Fallback: Distinguishes synthetic OCR fallback (is_synthetic_fallback=True,
  ledger_version=0) from authoritative ledgers, and never invents speculative event_type dynamics.
"""

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.repositories.report_repository import ModelReportRepository
from app.schemas.consilium import AegisAgentRole
from app.schemas.privacy import PrivacyLevel
from app.services.ecg_context_service import sanitize_pii_text
from app.services.primary_consilium_service import ConsiliumContextMissingError


class LaboratoryLedgerVersionNotFoundError(ConsiliumContextMissingError):
    pass

LABORATORY_LEDGER_REPORT_TYPE = "laboratory_ledger"
PREPARATORY_OCR_REPORT_TYPE = "preparatory_vision_ocr"

# Safe clinical reference range keys
SAFE_REFERENCE_RANGE_FIELDS = {"low", "high", "text"}

# Safe clinical shared marker keys (shared_labs_table)
SAFE_SHARED_MARKER_FIELDS = {
    "marker_key",
    "display_name",
    "value",
    "numeric_value",
    "unit",
    "sample_date",
    "reference_range",
    "event_type",
    "observation_id",
    "is_uncertain",
    "uncertainty_note",
    "is_recognition_ambiguous",
}

# Safe clinical observation keys for laboratory_history
SAFE_OBSERVATION_FIELDS = {
    "observation_id",
    "marker_key",
    "display_name",
    "value",
    "numeric_value",
    "unit",
    "sample_date",
    "reference_range",
    "event_type",
    "supersedes_observation_id",
    "correction_reason",
    "is_active",
    "is_critical_flag",
    "is_uncertain",
    "uncertainty_note",
    "is_recognition_ambiguous",
    "recorded_at",
}


def sanitize_uncertainty_note(note: Optional[str]) -> Optional[str]:
    """
    Sanitizes uncertainty_note before passing into consilium context models.
    Strictly guarantees zero leakage of OCR free text, unlabelled PII, addresses, or IDs:
    - Eliminates all arbitrary free text from being passed into consilium prompts.
    - Strictly maps to deterministic, safe, fixed technical clinical formulations.
    - Full raw OCR text remains intact only in server_only preparatory reports for audit.
    """
    if note is None:
        return None
    if not isinstance(note, str):
        note = str(note)
    cleaned = note.strip()
    if not cleaned:
        return None

    lower = cleaned.lower()
    standard_phrases: List[str] = []

    if any(k in lower for k in ("recognition ambiguity", "unclear print", "damaged document", "размыт", "неразборчив", "пятно", "blur", "smudg", "дефект бланка", "поврежд")):
        standard_phrases.append("Recognition ambiguity: unclear print or damaged document")

    if any(k in lower for k in ("missing unit", "missing unit of measurement", "отсутствует единица", "без единицы", "нет единиц")):
        standard_phrases.append("Missing unit of measurement for numeric value on laboratory report")

    if any(k in lower for k in ("missing specimen collection date", "missing sample date", "дата забора не указана", "отсутствует дата", "нет даты")):
        standard_phrases.append("Missing specimen collection date on laboratory report")

    if any(k in lower for k in ("borderline qualitative", "indeterminate qualitative", "сомнительн", "borderline", "слабоположительн", "equivocal", "неопределен")):
        standard_phrases.append("Borderline or indeterminate qualitative finding")

    if any(k in lower for k in ("non-standard text", "нестандартн")):
        standard_phrases.append("Non-standard text value requiring verification")

    if standard_phrases:
        return "; ".join(standard_phrases)

    # When arbitrary/unrecognized free text is encountered (e.g. unlabelled names, addresses, IDs),
    # never leak the string to consilium prompts. Map strictly to the safe fixed technical formulation.
    return "Non-standard text value requiring verification"


class LaboratoryConsiliumContext(BaseModel):
    """
    Unified laboratory context passed to AI consiliums.
    - `ledger_version`: Authoritative ledger version (0 if fallback / unseeded).
    - `shared_labs_table`: Active marker state keyed by `marker_key` for 100% backward compatibility.
    - `laboratory_history`: Complete chronological list of observations (including past dated
      studies and superseded erroneous values) to eliminate marker overwrite defect (C-2).
    - `is_synthetic_fallback`: True if synthesized from preparatory OCR reports rather than an authoritative ledger.
    - `source`: "laboratory_ledger" | "preparatory_vision_ocr_fallback" | "empty"
    """
    ledger_version: int = 0
    shared_labs_table: Dict[str, Any] = Field(default_factory=dict)
    laboratory_history: List[Dict[str, Any]] = Field(default_factory=list)
    is_synthetic_fallback: bool = False
    source: str = "laboratory_ledger"


def _sanitize_reference_range(ref: Any) -> Optional[Dict[str, Any]]:
    """Sanitizes reference range to strictly low, high, text. Strips all metadata/PII."""
    if ref is None:
        return None
    if hasattr(ref, "model_dump"):
        ref = ref.model_dump()
    elif not isinstance(ref, dict):
        return {"low": None, "high": None, "text": str(ref)}

    return {
        "low": ref.get("low"),
        "high": ref.get("high"),
        "text": ref.get("text"),
    }


def _sanitize_shared_marker(marker_key: str, data: Any) -> Dict[str, Any]:
    """Sanitizes an active marker entry for shared_labs_table, stripping any non-clinical PII."""
    if not isinstance(data, dict):
        return {
            "marker_key": marker_key,
            "display_name": marker_key,
            "value": data,
            "numeric_value": None,
            "unit": None,
            "sample_date": None,
            "reference_range": None,
            "event_type": None,
            "observation_id": None,
            "is_uncertain": False,
            "uncertainty_note": None,
            "is_recognition_ambiguous": False,
        }

    clean_marker: Dict[str, Any] = {}
    for k in SAFE_SHARED_MARKER_FIELDS:
        if k in data:
            val = data[k]
            if k == "reference_range":
                val = _sanitize_reference_range(val)
            elif k == "uncertainty_note":
                val = sanitize_uncertainty_note(val)
            elif k == "display_name" and isinstance(val, str):
                val = sanitize_pii_text(val)
            elif hasattr(val, "value"):
                val = val.value
            elif hasattr(val, "model_dump"):
                val = val.model_dump()
            clean_marker[k] = val

    # Ensure baseline identifiers are present and clean
    if "marker_key" not in clean_marker:
        clean_marker["marker_key"] = marker_key
    if "display_name" not in clean_marker:
        clean_marker["display_name"] = marker_key
    if "is_uncertain" not in clean_marker:
        clean_marker["is_uncertain"] = False
    if "uncertainty_note" not in clean_marker:
        clean_marker["uncertainty_note"] = None
    if "is_recognition_ambiguous" not in clean_marker:
        clean_marker["is_recognition_ambiguous"] = False

    return clean_marker


def _sanitize_observation(obs: Dict[str, Any]) -> Dict[str, Any]:
    """Filters observation dictionary to only clinically safe fields and serializable types."""
    clean_obs: Dict[str, Any] = {}
    for k in SAFE_OBSERVATION_FIELDS:
        if k in obs:
            val = obs[k]
            if k == "reference_range":
                val = _sanitize_reference_range(val)
            elif k == "uncertainty_note":
                val = sanitize_uncertainty_note(val)
            elif k in ("correction_reason", "display_name") and isinstance(val, str):
                val = sanitize_pii_text(val)
            elif hasattr(val, "value"):
                val = val.value
            elif hasattr(val, "model_dump"):
                val = val.model_dump()
            clean_obs[k] = val
    if "is_uncertain" not in clean_obs:
        clean_obs["is_uncertain"] = False
    if "uncertainty_note" not in clean_obs:
        clean_obs["uncertainty_note"] = None
    if "is_recognition_ambiguous" not in clean_obs:
        clean_obs["is_recognition_ambiguous"] = False
    return clean_obs


def assemble_consilium_laboratory_context(
    db: Session,
    cycle_id: str,
    ledger_version: Optional[int] = None,
) -> LaboratoryConsiliumContext:
    """
    Assembles the complete laboratory context for consilium models without side effects:
    - Pure SELECT queries on ModelReportRepository; never commits or rolls back transactions.
    - Reads the authoritative `laboratory_ledger` when available (optionally targeted to `ledger_version`).
    - Gracefully falls back to synthesizing chronological history from all `preparatory_vision_ocr`
      reports if `laboratory_ledger` has not been seeded yet.
    - Strictly sanitizes all data structures (stripping PII, metadata, doctor/hospital info).
    - Returns an empty context if no laboratory records exist for the given cycle.
    """
    report_repo = ModelReportRepository(db)

    # 1. Primary path: Read authoritative laboratory_ledger
    target_ledger = None
    if ledger_version is not None and ledger_version > 0:
        ledger_versions = report_repo.list_versions(
            cycle_id=cycle_id,
            report_type=LABORATORY_LEDGER_REPORT_TYPE,
            privacy_level=PrivacyLevel.SERVER_ONLY,
        )
        for r in ledger_versions:
            r_ver = int((r.content or {}).get("ledger_version", r.version))
            if r_ver == ledger_version:
                target_ledger = r
                break
        if target_ledger is None:
            raise LaboratoryLedgerVersionNotFoundError(
                f"Authoritative laboratory ledger version {ledger_version} was requested for cycle '{cycle_id}', "
                f"but does not exist in server storage."
            )
    else:
        ledgers = report_repo.list_server_only(
            cycle_id=cycle_id,
            report_type=LABORATORY_LEDGER_REPORT_TYPE,
        )
        if ledgers:
            target_ledger = ledgers[0]

    if target_ledger is not None:
        content = target_ledger.content or {}
        resolved_version = int(content.get("ledger_version", target_ledger.version))
        raw_history = content.get("observations_history") or []
        raw_active = content.get("active_markers") or {}
        shared_labs = content.get("shared_labs_table")

        sanitized_shared_labs: Dict[str, Any] = {}
        if isinstance(shared_labs, dict) and shared_labs:
            for k, marker_data in shared_labs.items():
                sanitized_shared_labs[k] = _sanitize_shared_marker(k, marker_data)
        else:
            for k, obs in raw_active.items():
                sanitized_shared_labs[k] = _sanitize_shared_marker(k, obs)

        sanitized_history = [
            _sanitize_observation(item) for item in raw_history if isinstance(item, dict)
        ]

        return LaboratoryConsiliumContext(
            ledger_version=resolved_version,
            shared_labs_table=sanitized_shared_labs,
            laboratory_history=sanitized_history,
            is_synthetic_fallback=False,
            source="laboratory_ledger",
        )

    # 2. Fallback path: Synthesize context across all preparatory_vision_ocr reports
    ocr_reports = report_repo.list_versions(
        cycle_id=cycle_id,
        report_type=PREPARATORY_OCR_REPORT_TYPE,
        privacy_level=PrivacyLevel.SERVER_ONLY,
    )
    if not ocr_reports:
        return LaboratoryConsiliumContext(
            ledger_version=0,
            shared_labs_table={},
            laboratory_history=[],
            is_synthetic_fallback=False,
            source="empty",
        )

    chronological_ocr = list(reversed(ocr_reports))
    combined_active: Dict[str, Dict[str, Any]] = {}
    synthesized_history: List[Dict[str, Any]] = []
    obs_counter = 0

    for rep in chronological_ocr:
        c = rep.content or {}
        role = c.get("agent_role")
        if role is not None and role != AegisAgentRole.VISION_OCR.value:
            from app.services.primary_consilium_service import ConsiliumContextMissingError
            raise ConsiliumContextMissingError(
                f"Invalid agent_role '{role}' on preparatory Vision OCR report; "
                f"expected '{AegisAgentRole.VISION_OCR.value}'."
            )
        rec_at = rep.created_at.isoformat() if rep.created_at else datetime.now(timezone.utc).isoformat()
        confirmed = c.get("confirmed_observations")

        if isinstance(confirmed, list) and confirmed:
            for marker in confirmed:
                if not isinstance(marker, dict):
                    continue
                obs_counter += 1
                marker_key = str(marker.get("marker_key", "")).strip()
                if not marker_key:
                    continue

                raw_event = marker.get("event_type")
                if hasattr(raw_event, "value"):
                    event_type_val = raw_event.value
                elif raw_event is not None and str(raw_event).strip():
                    event_type_val = str(raw_event).strip()
                else:
                    event_type_val = None

                obs_id = marker.get("observation_id") or f"obs_syn_{marker_key}_{obs_counter}"
                entry = {
                    "observation_id": obs_id,
                    "marker_key": marker_key,
                    "display_name": marker.get("display_name", marker_key),
                    "value": marker.get("value"),
                    "numeric_value": marker.get("numeric_value"),
                    "unit": marker.get("unit"),
                    "sample_date": marker.get("sample_date"),
                    "reference_range": marker.get("reference_range"),
                    "event_type": event_type_val,
                    "supersedes_observation_id": marker.get("supersedes_observation_id"),
                    "correction_reason": marker.get("correction_reason"),
                    "is_active": True,
                    "is_critical_flag": bool(marker.get("is_critical_flag", False)),
                    "is_uncertain": bool(marker.get("is_uncertain", False)),
                    "uncertainty_note": marker.get("uncertainty_note"),
                    "is_recognition_ambiguous": bool(marker.get("is_recognition_ambiguous", False)),
                    "recorded_at": rec_at,
                }
                sanitized_obs = _sanitize_observation(entry)
                synthesized_history.append(sanitized_obs)
                combined_active[marker_key] = _sanitize_shared_marker(marker_key, entry)
        else:
            table = c.get("shared_labs_table")
            if isinstance(table, dict):
                for raw_key, raw_val in table.items():
                    obs_counter += 1
                    marker_key = str(raw_key).strip()
                    if not marker_key:
                        continue

                    obs_id = f"obs_syn_{marker_key}_{obs_counter}"
                    if isinstance(raw_val, dict):
                        raw_event = raw_val.get("event_type")
                        if hasattr(raw_event, "value"):
                            event_type_val = raw_event.value
                        elif raw_event is not None and str(raw_event).strip():
                            event_type_val = str(raw_event).strip()
                        else:
                            event_type_val = None

                        entry = {
                            "observation_id": raw_val.get("observation_id") or obs_id,
                            "marker_key": marker_key,
                            "display_name": raw_val.get("display_name", marker_key),
                            "value": raw_val.get("value"),
                            "numeric_value": raw_val.get("numeric_value"),
                            "unit": raw_val.get("unit"),
                            "sample_date": raw_val.get("sample_date"),
                            "reference_range": raw_val.get("reference_range"),
                            "event_type": event_type_val,
                            "supersedes_observation_id": raw_val.get("supersedes_observation_id"),
                            "correction_reason": raw_val.get("correction_reason"),
                            "is_active": bool(raw_val.get("is_active", True)),
                            "is_critical_flag": bool(raw_val.get("is_critical_flag", False)),
                            "is_uncertain": bool(raw_val.get("is_uncertain", False)),
                            "uncertainty_note": raw_val.get("uncertainty_note"),
                            "is_recognition_ambiguous": bool(raw_val.get("is_recognition_ambiguous", False)),
                            "recorded_at": rec_at,
                        }
                    else:
                        entry = {
                            "observation_id": obs_id,
                            "marker_key": marker_key,
                            "display_name": marker_key,
                            "value": raw_val,
                            "numeric_value": None,
                            "unit": None,
                            "sample_date": None,
                            "reference_range": None,
                            "event_type": None,
                            "supersedes_observation_id": None,
                            "correction_reason": None,
                            "is_active": True,
                            "is_critical_flag": False,
                            "is_uncertain": False,
                            "uncertainty_note": None,
                            "is_recognition_ambiguous": False,
                            "recorded_at": rec_at,
                        }
                    sanitized_obs = _sanitize_observation(entry)
                    synthesized_history.append(sanitized_obs)
                    combined_active[marker_key] = _sanitize_shared_marker(marker_key, entry)

    shared_labs_table: Dict[str, Any] = {}
    for k, obs in combined_active.items():
        shared_labs_table[k] = _sanitize_shared_marker(k, obs)

    return LaboratoryConsiliumContext(
        ledger_version=0,
        shared_labs_table=shared_labs_table,
        laboratory_history=synthesized_history,
        is_synthetic_fallback=True,
        source="preparatory_vision_ocr_fallback",
    )


def _normalize_dt(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def get_last_evaluated_ledger_version(db: Session, cycle_id: str) -> int:
    report_repo = ModelReportRepository(db)

    # 1. Collect all authoritative laboratory_ledger versions actually present in storage
    ledger_versions = report_repo.list_versions(
        cycle_id=cycle_id,
        report_type=LABORATORY_LEDGER_REPORT_TYPE,
        privacy_level=PrivacyLevel.SERVER_ONLY,
    )
    existing_ledger_versions: set[int] = set()
    for lv in ledger_versions:
        c = lv.content or {}
        v = c.get("ledger_version", lv.version)
        try:
            parsed_v = int(v)
            if parsed_v > 0:
                existing_ledger_versions.add(parsed_v)
        except (ValueError, TypeError):
            continue

    if not existing_ledger_versions:
        return 0

    confirmed_evaluations: List[tuple[datetime, int, int, str]] = []

    # 2. Check Lab Revision execution logs
    exec_logs = report_repo.list_server_only(
        cycle_id=cycle_id,
        report_type="lab_revision_execution_log",
    )
    for rep in exec_logs:
        c = rep.content or {}
        completed_at = c.get("completed_at_utc")
        if not completed_at or not isinstance(completed_at, str) or not completed_at.strip():
            continue

        raw_ver = c.get("evaluated_ledger_version")
        if raw_ver is None:
            continue
        try:
            eval_ver = int(raw_ver)
        except (ValueError, TypeError):
            continue

        if eval_ver <= 0 or eval_ver not in existing_ledger_versions:
            continue

        dt = _normalize_dt(rep.created_at)
        if dt is None:
            try:
                dt = _normalize_dt(datetime.fromisoformat(completed_at))
            except Exception:
                pass

        resolved_dt = dt or datetime.min.replace(tzinfo=timezone.utc)
        confirmed_evaluations.append((resolved_dt, rep.version, eval_ver, "lab_revision_execution_log"))

    # 3. Check Primary Consilium chief_physician_internal reports
    pro_primary = report_repo.list_server_only(
        cycle_id=cycle_id,
        report_type="chief_physician_internal",
    )
    for rep in pro_primary:
        c = rep.content or {}
        if c.get("is_synthetic_fallback"):
            continue

        qr = c.get("flash_quality_review")
        if not isinstance(qr, dict) or qr.get("approved_for_publication") is not True:
            continue

        raw_ver = c.get("evaluated_ledger_version")
        if raw_ver is None:
            continue
        try:
            eval_ver = int(raw_ver)
        except (ValueError, TypeError):
            continue

        if eval_ver <= 0 or eval_ver not in existing_ledger_versions:
            continue

        resolved_dt = _normalize_dt(rep.created_at) or datetime.min.replace(tzinfo=timezone.utc)
        confirmed_evaluations.append((resolved_dt, rep.version, eval_ver, "chief_physician_internal"))

    if not confirmed_evaluations:
        return 0

    confirmed_evaluations.sort(key=lambda item: (item[0], item[1]))
    return confirmed_evaluations[-1][2]
