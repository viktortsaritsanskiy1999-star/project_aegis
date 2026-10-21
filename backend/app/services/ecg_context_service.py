"""
ECG Consilium Context Service.

Provides a unified, read-safe loader for electrocardiogram context across consilium services
(Primary Consilium, Weekly Consilium, and Lab Revision Consilium).

Addresses audit remediation items (C-3 / C-10):
- Structured context preservation: Delivers all observable parameters (recording_type,
  technical quality, lead_name, heart_rate_bpm, rhythm_type, rhythm_regularity,
  intervals PR/QRS/QT/QTc, electrical_axis, ST/T findings, limitations, uncertainties,
  potential_urgent_warning, and raw_unresolved_metrics) to all consilium models.
- Deterministic PII Defense-in-Depth: Redacts overt personal identifiers (names, emails,
  phones, IDs, dates of birth, organization/hospital labels) from free-form text and nested
  structures. Note: Regex-based sanitization is heuristic and cannot claim absolute mathematical
  guarantee of zero residual PII; it serves as a robust defense-in-depth barrier.
- Report Provenance & Verification: Validates that preparatory ECG records originate from
  trusted server storage with verified agent_role ('vision_ecg'). Clearly distinguishes modern
  Vision ECG reports from supported legacy summary-only reports, documenting the absence of
  discrete cryptographic verification status as a clinical limitation rather than inventing fake status.
- Numeric Safety: Re-uses validated C-10 parsers (_parse_hr_metric, _parse_interval_metric).
  Never performs silent int()/float() truncation that loses raw data. Questionable or out-of-range
  values (NaN, Inf, negative, unit mismatch, extreme rates) are rejected to None with raw data and
  clinical reasons preserved in raw_unresolved_metrics without creating new diagnostic thresholds.
"""

import math
import re
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Set, Tuple
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.repositories.report_repository import ModelReportRepository
from app.schemas.consilium import (
    AegisAgentRole,
    classify_lead_setup,
    _parse_hr_metric,
    _parse_interval_metric,
)
from app.services.primary_consilium_service import (
    PREPARATORY_ECG_REPORT_TYPE,
    ConsiliumContextMissingError,
)

# ------------------------------------------------------------------------------
# DETERMINISTIC PII SANITIZATION
# ------------------------------------------------------------------------------

_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")

_LABELED_PHONE_RE = re.compile(
    r"(?i)\b(?:tel|phone|cell|mobile|тел|моб|факс|fax|контакт|contact)\s*[:=-]?\s*(\+?[\d\s().-]{7,20}\b)"
)
_INTL_PHONE_RE = re.compile(
    r"(?<!\d)(?:\+?\d{1,3}[-.\s]?)?\(?\d{2,4}\)?[-.\s]?\d{3}[-.\s]?\d{2}[-.\s]?\d{2}(?!\d)"
)
_US_PHONE_RE = re.compile(
    r"(?<!\d)(?:\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}(?!\d)"
)
_DIGITS_PHONE_RE = re.compile(r"(?<!\w)\+\d{9,15}(?!\w)")

_LABELED_NAME_EN_RE = re.compile(
    r"(?i)\b(patient(?:\s+name)?|doctor(?:\s+name)?|physician|dr\.|mr\.|mrs\.|ms\.|pt\.?|subject|operator|technician|nurse|name)\s*[:=-]?\s*((?:(?!\b(?:dob|phone|email|tel|cell|fax|id|mrn|hospital|clinic)\b)(?!\S*:)[A-Za-z][A-Za-z.\-']+\s*){1,4})"
)
_LABELED_NAME_RU_RE = re.compile(
    r"(?i)\b(пациент(?:ка)?|фио|больной|врач|доктор|пациент\s+фио|имя|оператор)\s*[:=-]?\s*((?:(?!\b(?:др|дата|рождения|тел|моб|факс|снилс|паспорт|полис|клиника|больница)\b)(?!\S*:)[А-Яа-яЁё][А-Яа-яЁё.\-']+\s*){1,4})"
)
_LABELED_ORG_RE = re.compile(
    r"(?i)\b(hospital|clinic|клиника|больница|госпиталь|медцентр|центр)\s*[:=-]?\s*([A-Za-zА-Яа-яЁё0-9\s\"'«»-]+?)(?=[,\n;]|\.\s|\.$|$)"
)
_ID_RE = re.compile(
    r"(?i)\b(passport|паспорт|снилс|snils|ssn|policy|полис|омс|mrn|patient\s*id|id)\s*[:=-]?\s*([\w\d\s-]{3,20})\b"
)
_DOB_RE = re.compile(
    r"(?i)\b(dob|date of birth|дата рождения|д\.р\.)\s*[:=-]?\s*(\d{2,4}[-./]\d{2}[-./]\d{2,4})\b"
)

_PII_TOKENS: Set[str] = {
    "name",
    "patient",
    "doctor",
    "physician",
    "clinic",
    "hospital",
    "phone",
    "contact",
    "email",
    "address",
    "dob",
    "birth",
    "passport",
    "snils",
    "ssn",
    "policy",
    "hash",
    "ingested_at",
    "mrn",
    "fio",
    "фио",
    "пациент",
    "врач",
    "доктор",
    "клиника",
    "больница",
    "телефон",
    "контакт",
    "почта",
    "адрес",
    "снилс",
    "полис",
}


def sanitize_pii_text(text: Optional[str]) -> Optional[str]:
    """
    Deterministic defense-in-depth sanitization of common overt personal identifiers.
    Preserves clinically relevant metrics, dates, and device indicators.
    """
    if text is None:
        return None
    if not isinstance(text, str):
        text = str(text)

    # Protect ISO dates from phone regex collisions
    dates: List[str] = []

    def _protect_date(m: re.Match) -> str:
        dates.append(m.group(0))
        return f"__ISO_DATE_{len(dates)-1}__"

    cleaned = re.sub(r"\b\d{4}-\d{2}-\d{2}\b", _protect_date, text)

    # 1. Emails
    cleaned = _EMAIL_RE.sub("[REDACTED_EMAIL]", cleaned)

    # 2. Phones
    cleaned = _LABELED_PHONE_RE.sub(
        lambda m: m.group(0).replace(m.group(1), "[REDACTED_PHONE]"), cleaned
    )
    cleaned = _INTL_PHONE_RE.sub("[REDACTED_PHONE]", cleaned)
    cleaned = _US_PHONE_RE.sub("[REDACTED_PHONE]", cleaned)
    cleaned = _DIGITS_PHONE_RE.sub("[REDACTED_PHONE]", cleaned)

    # Restore ISO dates
    for idx, d in enumerate(dates):
        cleaned = cleaned.replace(f"__ISO_DATE_{idx}__", d)

    # 3. Labeled names & organizations
    cleaned = _LABELED_NAME_EN_RE.sub(r"\1: [REDACTED_NAME]", cleaned)
    cleaned = _LABELED_NAME_RU_RE.sub(r"\1: [REDACTED_NAME]", cleaned)
    cleaned = _LABELED_ORG_RE.sub(r"\1: [REDACTED_ORGANIZATION]", cleaned)

    # 4. IDs & DOB
    cleaned = _ID_RE.sub(r"\1: [REDACTED_ID]", cleaned)
    cleaned = _DOB_RE.sub(r"\1: [REDACTED_DOB]", cleaned)

    return cleaned.strip()


def is_pii_key(key: str) -> bool:
    """Checks whether a dictionary key represents personal identification data."""
    key_lower = str(key).lower()
    tokens = set(re.split(r"[_\-\s.]+", key_lower))
    return bool(tokens & _PII_TOKENS)


def sanitize_pii_value(val: Any) -> Any:
    """
    Recursively cleans nested structures (e.g. raw_unresolved_metrics),
    stripping PII keys and redacting text values.
    Where safe sanitization is impossible, returns an explicit safe refusal.
    """
    if val is None or isinstance(val, (int, float, bool)):
        return val
    if isinstance(val, str):
        return sanitize_pii_text(val)
    if isinstance(val, list):
        return [sanitize_pii_value(item) for item in val]
    if isinstance(val, tuple):
        return [sanitize_pii_value(item) for item in val]
    if isinstance(val, set):
        return [sanitize_pii_value(item) for item in sorted(val, key=str)]
    if isinstance(val, dict):
        clean_dict: Dict[str, Any] = {}
        for k, v in val.items():
            if is_pii_key(str(k)):
                continue
            clean_dict[str(k)] = sanitize_pii_value(v)
        return clean_dict
    # Explicit safe refusal for unhandled / unsafe data types
    return "[REDACTED_UNSUPPORTED_DATA]"


def _parse_record_date(raw_val: Any) -> Tuple[Optional[str], Optional[str], Optional[Dict[str, Any]]]:
    """
    Validates and normalizes ECG record_date into ISO format (YYYY-MM-DD).
    Accepts:
    - ISO date: 'YYYY-MM-DD'
    - ISO datetime: 'YYYY-MM-DDTHH:MM:SS...' or 'YYYY-MM-DD HH:MM:SS...'
    - European/Russian date: 'DD.MM.YYYY' or 'DD/MM/YYYY' or 'DD-MM-YYYY'
    - Slash date: 'YYYY/MM/DD'
    Rejects:
    - Arbitrary free text, PII, invalid calendar dates, non-date strings.
    Returns: (normalized_date_str, error_reason, raw_info_metadata).
    """
    if raw_val is None:
        return (None, None, None)
    if isinstance(raw_val, (date, datetime)):
        return (raw_val.strftime("%Y-%m-%d"), None, None)
    if not isinstance(raw_val, str):
        return (
            None,
            "ECG study record_date format is invalid or unverified; excluded from structured context.",
            {
                "raw_value": sanitize_pii_value(raw_val),
                "reason": "Non-string or unsupported date type; expected ISO date (YYYY-MM-DD).",
            },
        )
    val_str = raw_val.strip()
    if not val_str:
        return (None, None, None)

    # 1. Strict ISO date: YYYY-MM-DD
    m_iso = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", val_str)
    if m_iso:
        y, m, d = int(m_iso.group(1)), int(m_iso.group(2)), int(m_iso.group(3))
        try:
            parsed_d = date(y, m, d)
            if 1900 <= parsed_d.year <= 2100:
                return (parsed_d.isoformat(), None, None)
        except ValueError:
            pass

    # 2. Strict ISO datetime: YYYY-MM-DD[T ]HH:MM(:SS)?(Z|[+-]HH:MM)?$
    m_dt = re.match(
        r"^(\d{4})-(\d{2})-(\d{2})[T\s](\d{2}):(\d{2})(?::(\d{2})(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?$",
        val_str,
    )
    if m_dt:
        y, m, d = int(m_dt.group(1)), int(m_dt.group(2)), int(m_dt.group(3))
        hr, mn = int(m_dt.group(4)), int(m_dt.group(5))
        sec = int(m_dt.group(6)) if m_dt.group(6) else 0
        if 0 <= hr <= 23 and 0 <= mn <= 59 and 0 <= sec <= 59:
            try:
                parsed_d = date(y, m, d)
                if 1900 <= parsed_d.year <= 2100:
                    return (parsed_d.isoformat(), None, None)
            except ValueError:
                pass

    # 3. European/Russian: DD.MM.YYYY or DD/MM/YYYY or DD-MM-YYYY
    m_dmy = re.match(r"^(\d{2})[./-](\d{2})[./-](\d{4})$", val_str)
    if m_dmy:
        d, m, y = int(m_dmy.group(1)), int(m_dmy.group(2)), int(m_dmy.group(3))
        try:
            parsed_d = date(y, m, d)
            if 1900 <= parsed_d.year <= 2100:
                return (parsed_d.isoformat(), None, None)
        except ValueError:
            pass

    # 4. Slash: YYYY/MM/DD
    m_ymd = re.match(r"^(\d{4})/(\d{2})/(\d{2})$", val_str)
    if m_ymd:
        y, m, d = int(m_ymd.group(1)), int(m_ymd.group(2)), int(m_ymd.group(3))
        try:
            parsed_d = date(y, m, d)
            if 1900 <= parsed_d.year <= 2100:
                return (parsed_d.isoformat(), None, None)
        except ValueError:
            pass

    # Rejection: arbitrary text, unclosed timestamps, or invalid calendar date
    # raw_value is strictly None to prevent arbitrary strings entering model context
    # (original unparsed string remains stored in server-only database record)
    return (
        None,
        "ECG study record_date format is invalid or unverified; excluded from structured context.",
        {
            "raw_value": None,
            "reason": "Invalid or unverified study date format; expected ISO date (YYYY-MM-DD).",
        },
    )


# ------------------------------------------------------------------------------
# ALLOWLIST DATA BOUNDARY FOR UNRESOLVED METRICS
# ------------------------------------------------------------------------------

_ALLOWED_UNRESOLVED_METRICS: Set[str] = {
    "heart_rate_bpm",
    "pr_interval_ms",
    "qrs_duration_ms",
    "qt_interval_ms",
    "qtc_interval_ms",
    "electrical_axis",
    "record_date",
}

_ALLOWED_UNRESOLVED_FIELDS: Set[str] = {
    "raw_value",
    "reason",
    "conversion",
}

_NUMERIC_HR_STR_RE = re.compile(
    r"^([+-]?[0-9]+(?:\.[0-9]+)?)\s*(?:bpm|уд/?мин|уд)?$", re.IGNORECASE
)
_NUMERIC_INTERVAL_STR_RE = re.compile(
    r"^([+-]?[0-9]+(?:\.[0-9]+)?)\s*(?:ms|мс|s|sec|сек|с)?$", re.IGNORECASE
)
_NUMERIC_AXIS_STR_RE = re.compile(
    r"^(?:(?:normal|left|right|extreme|horizontal|vertical|indeterminate|нормальная|вертикальная|горизонтальная|влево|вправо)(?:\s+(?:axis|deviation|отклонение|ось))*)*(?:\s*\(?[~≈]?[+-]?\d+(?:\.\d+)?\s*(?:deg|°|град)?\)?)?$",
    re.IGNORECASE,
)

_SAFE_REASON_TEMPLATES: Dict[str, str] = {
    "out_of_range": "Reading is outside standard computational range; requires clinical verification.",
    "mismatched_unit": "Mismatched unit for ECG metric.",
    "unreadable_quality": "Tracing quality is unreadable; numeric value cannot be verified.",
    "single_lead_axis": "Single-lead recording cannot determine electrical heart axis.",
    "undetermined_lead": "Recording lead configuration is undetermined; frontal electrical axis cannot be calculated.",
    "invalid_date": "Invalid or unverified study date format; expected ISO date (YYYY-MM-DD).",
    "non_numeric": "Non-numeric, NaN or Infinite metric format.",
    "unverified": "Metric value cannot be verified from ECG recording.",
}

_SAFE_CONVERSION_TEMPLATES: Dict[str, str] = {
    "seconds_to_ms": "Converted from seconds to milliseconds.",
    "fractional_hr": "Fractional heart rate rounded to nearest integer.",
    "standard": "Measurement converted according to standard clinical units.",
}


def _validate_safe_raw_value(metric_key: str, val: Any) -> Optional[Any]:
    """
    Validates that raw_value is strictly a safe numeric value or numeric string
    with a recognized unit. Arbitrary free-text strings are strictly rejected to None.
    """
    if val is None:
        return None
    if isinstance(val, (int, float)):
        if math.isnan(val) or math.isinf(val):
            return None
        return val
    if not isinstance(val, str):
        return None

    val_clean = val.strip()
    if not val_clean:
        return None

    if metric_key == "record_date":
        return None

    if metric_key == "heart_rate_bpm":
        if _NUMERIC_HR_STR_RE.match(val_clean):
            return val_clean
        return None

    if metric_key in ("pr_interval_ms", "qrs_duration_ms", "qt_interval_ms", "qtc_interval_ms"):
        if _NUMERIC_INTERVAL_STR_RE.match(val_clean):
            return val_clean
        return None

    if metric_key == "electrical_axis":
        if _NUMERIC_AXIS_STR_RE.match(val_clean) and val_clean.lower() not in ("", "axis", "ось"):
            return val_clean
        return None

    return None


def _sanitize_reason_to_template(raw_reason: Optional[Any]) -> Optional[str]:
    """
    Maps arbitrary reason strings to deterministic fixed technical templates,
    preventing any unredacted or unlabeled free text from entering consilium context.
    """
    if raw_reason is None:
        return None
    r_str = str(raw_reason).lower().strip()
    if not r_str:
        return None
    if "outside standard computational range" in r_str or "out of range" in r_str:
        return _SAFE_REASON_TEMPLATES["out_of_range"]
    if "mismatched unit" in r_str or "mismatched" in r_str:
        return _SAFE_REASON_TEMPLATES["mismatched_unit"]
    if "unreadable" in r_str or "noise" in r_str:
        return _SAFE_REASON_TEMPLATES["unreadable_quality"]
    if "single-lead" in r_str or "single lead" in r_str:
        return _SAFE_REASON_TEMPLATES["single_lead_axis"]
    if "undetermined" in r_str or "conflict" in r_str or "contradict" in r_str or "unknown" in r_str:
        return _SAFE_REASON_TEMPLATES["undetermined_lead"]
    if "date" in r_str:
        return _SAFE_REASON_TEMPLATES["invalid_date"]
    if "nan" in r_str or "infinite" in r_str or "non-numeric" in r_str or "unrecognized" in r_str:
        return _SAFE_REASON_TEMPLATES["non_numeric"]
    return _SAFE_REASON_TEMPLATES["unverified"]


def _sanitize_conversion_to_template(raw_conv: Optional[Any]) -> Optional[str]:
    """
    Maps arbitrary conversion strings to deterministic fixed technical templates.
    """
    if raw_conv is None:
        return None
    c_str = str(raw_conv).lower().strip()
    if not c_str:
        return None
    if "second" in c_str or "to milliseconds" in c_str or "секунд" in c_str:
        return _SAFE_CONVERSION_TEMPLATES["seconds_to_ms"]
    if "fractional" in c_str or "round" in c_str or "дробн" in c_str:
        return _SAFE_CONVERSION_TEMPLATES["fractional_hr"]
    return _SAFE_CONVERSION_TEMPLATES["standard"]


def filter_unresolved_metrics_allowlist(raw_unresolved: Dict[str, Any]) -> Dict[str, Any]:
    """
    Enforces a strict allowlist boundary on raw_unresolved_metrics passed to AI consiliums:
    - Only known clinical metrics (heart_rate_bpm, intervals, electrical_axis, record_date)
    - Only known fields (raw_value, reason, conversion)
    - raw_value is strictly numeric or a valid numeric string with known unit (or None).
    - reason and conversion are mapped to fixed deterministic safe templates.
    All free-form OCR dumps, unverified text strings, and non-metric keys are purged.
    """
    clean_unresolved: Dict[str, Any] = {}
    for metric_key, metric_dict in raw_unresolved.items():
        if metric_key not in _ALLOWED_UNRESOLVED_METRICS:
            continue
        if isinstance(metric_dict, dict):
            clean_entry: Dict[str, Any] = {}
            for field_key, field_val in metric_dict.items():
                if field_key not in _ALLOWED_UNRESOLVED_FIELDS:
                    continue
                if field_key == "raw_value":
                    clean_entry[field_key] = _validate_safe_raw_value(metric_key, field_val)
                elif field_key == "reason":
                    clean_entry[field_key] = _sanitize_reason_to_template(field_val)
                elif field_key == "conversion":
                    clean_entry[field_key] = _sanitize_conversion_to_template(field_val)
            clean_unresolved[metric_key] = clean_entry
        elif isinstance(metric_dict, (int, float)) and not (math.isnan(metric_dict) or math.isinf(metric_dict)):
            clean_unresolved[metric_key] = metric_dict

    return clean_unresolved


# ------------------------------------------------------------------------------
# CONTEXT MODEL
# ------------------------------------------------------------------------------

class EcgConsiliumContext(BaseModel):
    """
    Unified structured electrocardiogram context passed to AI consiliums.
    Contains all observable measurements, technical limits, and uncertainties
    from the verified preparatory Vision ECG report without PII leakage.
    """
    model_config = ConfigDict(extra="forbid")

    is_conducted: bool = False
    ecg_summary: str = "Not conducted"
    recording_type: Optional[str] = None
    recording_quality: Optional[str] = None
    lead_name: Optional[str] = None
    record_date: Optional[str] = None
    heart_rate_bpm: Optional[int] = None
    rhythm_type: Optional[str] = None
    rhythm_regularity: Optional[str] = None
    intervals: Dict[str, Optional[float]] = Field(
        default_factory=lambda: {
            "pr_interval_ms": None,
            "qrs_duration_ms": None,
            "qt_interval_ms": None,
            "qtc_interval_ms": None,
        }
    )
    electrical_axis: Optional[str] = None
    st_segment_findings: Optional[str] = None
    t_wave_findings: Optional[str] = None
    limitations: List[str] = Field(default_factory=list)
    uncertainties: List[str] = Field(default_factory=list)
    potential_urgent_warning: Optional[str] = None
    raw_unresolved_metrics: Dict[str, Any] = Field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """Returns clean dict representation for serialization into agent input packages."""
        return self.model_dump()


# ------------------------------------------------------------------------------
# CONTEXT BUILDER
# ------------------------------------------------------------------------------

def assemble_consilium_ecg_context(
    db: Session,
    cycle_id: str,
) -> EcgConsiliumContext:
    """
    Builds the unified, read-safe structured ECG context for all AI consiliums.
    Loads the latest preparatory_vision_ecg report from server storage,
    sanitizes PII, preserves C-10 safety resolutions, handles legacy reports,
    and returns an EcgConsiliumContext.
    """
    repo = ModelReportRepository(db)
    reports = repo.list_server_only(
        cycle_id=cycle_id,
        report_type=PREPARATORY_ECG_REPORT_TYPE,
    )
    if not reports:
        return EcgConsiliumContext(
            is_conducted=False,
            ecg_summary="Not conducted",
        )

    latest_report = reports[0]
    if not isinstance(latest_report.content, dict):
        raise ConsiliumContextMissingError(
            f"Preparatory Vision ECG report content for cycle '{cycle_id}' is malformed or empty."
        )

    content = dict(latest_report.content)

    # 1. Provenance and agent_role validation:
    # Absence of agent_role is NOT treated as proof of verification.
    stored_role = content.get("agent_role")
    if stored_role != AegisAgentRole.VISION_ECG.value:
        raise ConsiliumContextMissingError(
            f"Invalid or untrusted preparatory Vision ECG report for cycle '{cycle_id}': "
            f"expected agent_role='{AegisAgentRole.VISION_ECG.value}', got '{stored_role}'."
        )

    raw_summary = content.get("ecg_summary")
    raw_summary_str = str(raw_summary).strip() if raw_summary and str(raw_summary).strip() else ""

    # Detect whether this is a structured report or legacy unstructured report
    has_structured_fields = any(
        content.get(k) is not None
        for k in (
            "recording_type",
            "intervals",
            "heart_rate_bpm",
            "rhythm_type",
            "electrical_axis",
            "raw_unresolved_metrics",
            "st_segment_findings",
            "t_wave_findings",
        )
    )

    is_conducted_pre = bool(
        raw_summary_str
        and raw_summary_str.lower() not in ("not conducted", "not performed", "--", "none")
    )

    # Start collecting limitations and uncertainties
    raw_limits = content.get("limitations") or []
    limitations = [
        sanitize_pii_text(str(item).strip())
        for item in raw_limits
        if isinstance(item, (str, int, float)) and str(item).strip()
    ]

    raw_unc = content.get("uncertainties") or []
    uncertainties = [
        sanitize_pii_text(str(item).strip())
        for item in raw_unc
        if isinstance(item, (str, int, float)) and str(item).strip()
    ]

    # Explicit report provenance notice when independent external verification is missing:
    if is_conducted_pre or has_structured_fields:
        if not content.get("verification_status"):
            prov_note = (
                f"Report provenance limitation: Preparatory ECG record (version {latest_report.version}) "
                "loaded from server storage without discrete cryptographic verification status."
            )
            if not any("provenance" in l.lower() for l in limitations):
                limitations.append(prov_note)

    # Legacy report format handling
    if not has_structured_fields:
        if not is_conducted_pre:
            return EcgConsiliumContext(
                is_conducted=False,
                ecg_summary="Not conducted",
            )
        legacy_lim = "Legacy ECG record: discrete interval, lead setup, and axis metrics were not captured."
        if not any("legacy" in l.lower() for l in limitations):
            limitations.append(legacy_lim)

        clean_limits: List[str] = []
        seen_l: Set[str] = set()
        for l in limitations:
            if l is None:
                continue
            cl = sanitize_pii_text(str(l).strip())
            if cl and cl not in seen_l:
                seen_l.add(cl)
                clean_limits.append(cl)

        clean_unc: List[str] = []
        seen_u: Set[str] = set()
        for u in uncertainties:
            if u is None:
                continue
            cu = sanitize_pii_text(str(u).strip())
            if cu and cu not in seen_u:
                seen_u.add(cu)
                clean_unc.append(cu)

        return EcgConsiliumContext(
            is_conducted=True,
            ecg_summary=sanitize_pii_text(raw_summary_str) or "Not conducted",
            limitations=clean_limits,
            uncertainties=clean_unc,
        )

    # Modern structured report processing
    raw_unresolved: Dict[str, Any] = dict(content.get("raw_unresolved_metrics") or {})

    # 2. NUMERIC SAFETY (Adhering strictly to C-10 standards)
    # Heart Rate parsing
    raw_hr = content.get("heart_rate_bpm")
    heart_rate_bpm: Optional[int] = None
    if raw_hr is not None:
        parsed_hr, hr_err, hr_info = _parse_hr_metric(raw_hr)
        if hr_err is not None:
            safe_hr_err = _sanitize_reason_to_template(hr_err)
            safe_raw_hr = _validate_safe_raw_value("heart_rate_bpm", raw_hr)
            raw_unresolved.setdefault("heart_rate_bpm", {
                "raw_value": safe_raw_hr,
                "reason": safe_hr_err,
            })
            note = f"Heart rate reading cannot be verified: {safe_hr_err}."
            if note not in uncertainties:
                uncertainties.append(note)
        else:
            assert parsed_hr is not None
            # Standard computational range (20-300 bpm) from C-10
            if parsed_hr < 20.0 or parsed_hr > 300.0:
                safe_raw_hr = sanitize_pii_value(raw_hr)
                raw_unresolved.setdefault("heart_rate_bpm", {
                    "raw_value": safe_raw_hr,
                    "reason": (
                        f"Reading ({parsed_hr} bpm) is outside standard computational range "
                        f"(20-300 bpm); requires clinical verification."
                    ),
                })
                note = (
                    f"Heart rate reading ({parsed_hr} bpm) is outside standard computational "
                    f"range (20-300 bpm); requires verification."
                )
                if note not in uncertainties:
                    uncertainties.append(note)
            else:
                heart_rate_bpm = int(round(parsed_hr))
                if hr_info is not None and "heart_rate_bpm" not in raw_unresolved:
                    raw_unresolved["heart_rate_bpm"] = sanitize_pii_value(hr_info)

    # Intervals parsing
    interval_specs = [
        ("pr_interval_ms", 40.0, 600.0, "ms"),
        ("qrs_duration_ms", 30.0, 400.0, "ms"),
        ("qt_interval_ms", 150.0, 800.0, "ms"),
        ("qtc_interval_ms", 200.0, 800.0, "ms"),
    ]
    intervals_clean: Dict[str, Optional[float]] = {
        "pr_interval_ms": None,
        "qrs_duration_ms": None,
        "qt_interval_ms": None,
        "qtc_interval_ms": None,
    }

    intervals_in = content.get("intervals")
    raw_intervals = intervals_in if isinstance(intervals_in, dict) else content

    for field_name, min_v, max_v, unit in interval_specs:
        raw_val = raw_intervals.get(field_name)
        if raw_val is None:
            continue
        parsed_val, parse_err, conv_info = _parse_interval_metric(raw_val)
        if parse_err is not None:
            safe_parse_err = _sanitize_reason_to_template(parse_err)
            safe_raw_val = _validate_safe_raw_value(field_name, raw_val)
            raw_unresolved.setdefault(field_name, {
                "raw_value": safe_raw_val,
                "reason": safe_parse_err,
            })
            note = f"ECG interval '{field_name}' reading cannot be verified: {safe_parse_err}."
            if note not in uncertainties:
                uncertainties.append(note)
        else:
            assert parsed_val is not None
            if parsed_val < min_v or parsed_val > max_v:
                safe_raw_val = sanitize_pii_value(raw_val)
                raw_unresolved.setdefault(field_name, {
                    "raw_value": safe_raw_val,
                    "reason": (
                        f"Reading ({parsed_val} {unit}) is outside standard computational range "
                        f"({min_v}-{max_v} {unit}); requires clinical verification."
                    ),
                })
                note = (
                    f"ECG interval '{field_name}' reading ({parsed_val} {unit}) is outside standard computational "
                    f"range ({min_v}-{max_v} {unit}); requires verification."
                )
                if note not in uncertainties:
                    uncertainties.append(note)
            else:
                intervals_clean[field_name] = round(parsed_val, 1)
                if conv_info is not None and field_name not in raw_unresolved:
                    raw_unresolved[field_name] = sanitize_pii_value(conv_info)

    # Study record_date validation & normalization
    raw_record_date = content.get("record_date")
    record_date, date_err, date_info = _parse_record_date(raw_record_date)
    if date_err is not None:
        safe_date_err = sanitize_pii_text(date_err)
        if safe_date_err and safe_date_err not in uncertainties:
            uncertainties.append(safe_date_err)
        if date_info is not None and "record_date" not in raw_unresolved:
            raw_unresolved["record_date"] = sanitize_pii_value(date_info)

    # String fields with deterministic PII sanitization
    recording_type = sanitize_pii_text(str(content["recording_type"]).strip()) if content.get("recording_type") is not None else None
    recording_quality = sanitize_pii_text(str(content["recording_quality"]).strip()) if content.get("recording_quality") is not None else None
    lead_name = sanitize_pii_text(str(content["lead_name"]).strip()) if content.get("lead_name") is not None else None
    rhythm_type = sanitize_pii_text(str(content["rhythm_type"]).strip()) if content.get("rhythm_type") is not None else None
    rhythm_regularity = sanitize_pii_text(str(content["rhythm_regularity"]).strip()) if content.get("rhythm_regularity") is not None else None
    electrical_axis = sanitize_pii_text(str(content["electrical_axis"]).strip()) if content.get("electrical_axis") is not None else None
    st_segment = sanitize_pii_text(str(content["st_segment_findings"]).strip()) if content.get("st_segment_findings") is not None else None
    t_wave = sanitize_pii_text(str(content["t_wave_findings"]).strip()) if content.get("t_wave_findings") is not None else None
    urgent_warning = sanitize_pii_text(str(content["potential_urgent_warning"]).strip()) if content.get("potential_urgent_warning") is not None else None

    # C-10 & C-3 SAFETY: electrical axis can ONLY be confirmed for 12-lead studies.
    # Single-lead, contradictory (conflict), and undetermined/unknown lead setups MUST NOT report axis.
    lead_setup = classify_lead_setup(recording_type, lead_name)
    if not lead_setup["is_confirmed_12_lead"]:
        if electrical_axis is not None:
            safe_axis_reason = (
                _SAFE_REASON_TEMPLATES["single_lead_axis"]
                if lead_setup["is_single_lead"]
                else _SAFE_REASON_TEMPLATES["undetermined_lead"]
            )
            raw_unresolved.setdefault("electrical_axis", {
                "raw_value": electrical_axis,
                "reason": safe_axis_reason,
            })
            axis_lim = (
                "Electrical heart axis cannot be determined from single-lead recording."
                if lead_setup["is_single_lead"]
                else "Electrical heart axis cannot be determined from undetermined or contradictory lead configuration."
            )
            if not any("axis" in lim.lower() for lim in limitations):
                limitations.append(axis_lim)
            electrical_axis = None

    # Final defense-in-depth sanitization of all messages, limitations, and uncertainties
    clean_limitations: List[str] = []
    seen_limits: Set[str] = set()
    for item in limitations:
        if item is None:
            continue
        c = sanitize_pii_text(str(item).strip())
        if c and c not in seen_limits:
            seen_limits.add(c)
            clean_limitations.append(c)

    clean_uncertainties: List[str] = []
    seen_unc: Set[str] = set()
    for item in uncertainties:
        if item is None:
            continue
        c = sanitize_pii_text(str(item).strip())
        if c and c not in seen_unc:
            seen_unc.add(c)
            clean_uncertainties.append(c)

    clean_unresolved = filter_unresolved_metrics_allowlist(sanitize_pii_value(raw_unresolved))

    is_conducted = bool(
        is_conducted_pre
        or recording_type
        or heart_rate_bpm is not None
        or rhythm_type
    )

    clean_summary = (
        sanitize_pii_text(raw_summary_str)
        if is_conducted and raw_summary_str
        else ("Not conducted" if not is_conducted else "ECG study recorded")
    )

    return EcgConsiliumContext(
        is_conducted=is_conducted,
        ecg_summary=clean_summary,
        recording_type=recording_type,
        recording_quality=recording_quality,
        lead_name=lead_name,
        record_date=record_date,
        heart_rate_bpm=heart_rate_bpm,
        rhythm_type=rhythm_type,
        rhythm_regularity=rhythm_regularity,
        intervals=intervals_clean,
        electrical_axis=electrical_axis,
        st_segment_findings=st_segment,
        t_wave_findings=t_wave,
        limitations=clean_limitations,
        uncertainties=clean_uncertainties,
        potential_urgent_warning=urgent_warning,
        raw_unresolved_metrics=clean_unresolved,
    )
