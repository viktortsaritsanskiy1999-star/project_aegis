import json
import logging
import os
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import re
from typing import Any, Dict, Generator, List, Optional
import uuid

from app.config import settings
from app.schemas.audit import AuditRecord, AuditRecordSummary, ClinicalReviewStatus

logger = logging.getLogger("aegis.audit_trail")

_SAFE_ID_REGEX = re.compile(r"^[a-zA-Z0-9_\-]+$")

_SECRET_PATTERNS = [
    re.compile(r"AIza[0-9A-Za-z\-_]{35}"),  # Google API keys
    re.compile(r"Bearer\s+[A-Za-z0-9\-\._~\+\/]+=*", re.IGNORECASE),  # Bearer tokens
    re.compile(r"eyJ[A-Za-z0-9\-_=]+\.eyJ[A-Za-z0-9\-_=]+\.[A-Za-z0-9\-_=]+"),  # JWT tokens
]


def redact_sensitive_text(text: Optional[str]) -> Optional[str]:
    """Redacts API keys, tokens, and secrets from text to prevent leakage into audit logs."""
    if not text or not isinstance(text, str):
        return text
    scrubbed = text
    for secret in (
        getattr(settings, "gemini_api_key", None),
        getattr(settings, "api_secret", None),
        getattr(settings, "pairing_secret", None),
        getattr(settings, "jwt_secret", None),
    ):
        if secret and len(secret) > 4 and secret != "YOUR_API_KEY_HERE":
            scrubbed = scrubbed.replace(secret, "[REDACTED_SECRET]")
    for pat in _SECRET_PATTERNS:
        scrubbed = pat.sub("[REDACTED_CREDENTIAL]", scrubbed)
    return scrubbed


def redact_sensitive_data(data: Any) -> Any:
    """Recursively redacts secrets in dicts, lists, and strings."""
    if isinstance(data, str):
        return redact_sensitive_text(data)
    elif isinstance(data, dict):
        return {k: redact_sensitive_data(v) for k, v in data.items()}
    elif isinstance(data, list):
        return [redact_sensitive_data(v) for v in data]
    return data


class AuditStorageError(Exception):
    """Raised when atomic audit trail write or retrieval fails."""


@dataclass
class AuditContext:
    """Thread-safe / async-safe inference context propagated across service calls."""
    patient_id: str
    cycle_id: str
    sprint_id: Optional[str] = None
    session_id: Optional[str] = None
    stage: str = "general"
    input_context_summary: Optional[Dict[str, Any]] = None
    report_id: Optional[str] = None
    last_call_id: Optional[str] = None
    call_ids: Optional[List[str]] = None
    audit_service: Optional[Any] = None

    def __post_init__(self):
        if self.call_ids is None:
            self.call_ids = []


# ContextVar for async-safe propagation
_current_audit_context: ContextVar[Optional[AuditContext]] = ContextVar(
    "_current_audit_context", default=None
)


def get_current_audit_context() -> Optional[AuditContext]:
    """Retrieves the active audit context from ContextVar if set."""
    return _current_audit_context.get()


def set_current_audit_context(context: Optional[AuditContext]):
    """Sets the active audit context."""
    return _current_audit_context.set(context)


def reset_current_audit_context(token):
    """Resets the active audit context using the token returned by set_current_audit_context."""
    if token is not None:
        _current_audit_context.reset(token)


@contextmanager
def audit_scope(
    patient_id: str,
    cycle_id: str,
    sprint_id: Optional[str] = None,
    session_id: Optional[str] = None,
    stage: str = "general",
    input_context_summary: Optional[Dict[str, Any]] = None,
    report_id: Optional[str] = None,
) -> Generator[AuditContext, None, None]:
    """
    Context manager to bind an inference session to patient/cycle/stage
    for transparent, zero-signature-break audit recording.
    """
    ctx = AuditContext(
        patient_id=patient_id,
        cycle_id=cycle_id,
        sprint_id=sprint_id,
        session_id=session_id,
        stage=stage,
        input_context_summary=input_context_summary,
        report_id=report_id,
    )
    token = _current_audit_context.set(ctx)
    try:
        yield ctx
    finally:
        _current_audit_context.reset(token)


class AuditTrailService:
    """
    Local file-based clinical audit trail service.
    Guarantees:
    - Path traversal prevention
    - Atomic write via temporary file + os.replace
    - Overwrite protection
    - Separate verbatim raw model output preservation
    - Strict test mode error propagation
    """

    def __init__(
        self,
        storage_dir: Optional[str | Path] = None,
        enabled: Optional[bool] = None,
        strict_test_mode: Optional[bool] = None,
    ) -> None:
        raw_path = str(storage_dir or settings.audit_storage_dir)
        path_obj = Path(raw_path)
        if not path_obj.is_absolute():
            backend_dir = Path(__file__).resolve().parents[2]
            clean_rel = raw_path.replace("\\", "/")
            if clean_rel.startswith("backend/"):
                clean_rel = clean_rel[8:]
            self.storage_dir = (backend_dir / clean_rel).resolve()
        else:
            self.storage_dir = path_obj.resolve()
        self.enabled = (
            enabled if enabled is not None else settings.enable_clinical_audit
        )
        self.strict_test_mode = (
            strict_test_mode
            if strict_test_mode is not None
            else settings.audit_strict_test_mode
        )

    def validate_identifier(self, identifier: str, param_name: str = "identifier") -> str:
        """
        Validates an identifier to strictly forbid path traversal, separators, or invalid characters.
        """
        if not isinstance(identifier, str) or not identifier.strip():
            raise ValueError(f"Invalid {param_name}: identifier must be a non-empty string.")

        clean = identifier.strip()
        if ".." in clean or "/" in clean or "\\" in clean or "\0" in clean:
            raise ValueError(
                f"Path traversal detected in {param_name}: '{identifier}' contains forbidden characters."
            )

        if not _SAFE_ID_REGEX.match(clean):
            raise ValueError(
                f"Invalid {param_name}: '{identifier}' contains characters outside [a-zA-Z0-9_-]."
            )

        return clean

    def _resolve_record_path(self, patient_id: str, cycle_id: str, call_id: str) -> Path:
        """Resolves target file path and strictly verifies it remains within storage_dir."""
        clean_pat = self.validate_identifier(patient_id, "patient_id")
        clean_cyc = self.validate_identifier(cycle_id, "cycle_id")
        clean_call = self.validate_identifier(call_id, "call_id")

        target_dir = self.storage_dir / clean_pat / clean_cyc
        target_path = target_dir / f"{clean_call}.json"

        # Verify no escape from storage_dir
        try:
            target_path.resolve().relative_to(self.storage_dir)
        except ValueError as exc:
            raise ValueError(
                f"Path traversal detected: resolved path escapes audit root: {target_path}"
            ) from exc

        return target_path

    def record_call(
        self,
        record: AuditRecord,
        overwrite: bool = False,
    ) -> Path:
        """
        Atomically records an audit record to disk.
        Uses write-to-temp + fsync + os.replace pattern.
        """
        if not self.enabled:
            return Path("")

        try:
            target_path = self._resolve_record_path(
                patient_id=record.patient_id,
                cycle_id=record.cycle_id,
                call_id=record.call_id,
            )

            if target_path.exists() and not overwrite:
                raise FileExistsError(
                    f"Audit record already exists for call_id '{record.call_id}' and overwrite is False."
                )

            target_dir = target_path.parent
            target_dir.mkdir(parents=True, exist_ok=True)

            # Redact credentials and secrets from prompt, responses, and metadata
            record.prompt = redact_sensitive_text(record.prompt)
            record.system_instruction = redact_sensitive_text(record.system_instruction)
            record.raw_response_text = redact_sensitive_text(record.raw_response_text) or ""
            record.cleaned_json_text = redact_sensitive_text(record.cleaned_json_text)
            if record.parsed_json:
                record.parsed_json = redact_sensitive_data(record.parsed_json)
            if record.input_context_summary:
                record.input_context_summary = redact_sensitive_data(record.input_context_summary)
            if record.error_diagnostic_log:
                record.error_diagnostic_log = redact_sensitive_text(record.error_diagnostic_log)

            temp_path = target_dir / f"{target_path.name}.tmp.{uuid.uuid4().hex}"
            payload_str = record.model_dump_json(indent=2)

            with open(temp_path, "w", encoding="utf-8") as f:
                f.write(payload_str)
                f.flush()
                os.fsync(f.fileno())

            os.replace(temp_path, target_path)
            logger.debug(f"Audit record written atomically to: {target_path}")
            return target_path

        except FileExistsError:
            raise
        except Exception as exc:
            error_msg = f"Failed to record clinical audit trail for call '{record.call_id}': {exc}"
            logger.error(error_msg, exc_info=True)
            if self.strict_test_mode:
                raise AuditStorageError(error_msg) from exc
            return Path("")

    def link_report(
        self,
        call_id: str,
        report_id: str,
        patient_id: Optional[str] = None,
        cycle_id: Optional[str] = None,
        privacy_level: Optional[str] = None,
    ) -> bool:
        """
        Updates an existing audit record with the persisted SQLite report_id and privacy_level.
        """
        if not self.enabled:
            return False

        clean_call = self.validate_identifier(call_id, "call_id")
        clean_rep = self.validate_identifier(report_id, "report_id")

        record_path: Optional[Path] = None
        if patient_id and cycle_id:
            record_path = self._resolve_record_path(patient_id, cycle_id, clean_call)
        else:
            # Search for the call_id across directories
            matches = list(self.storage_dir.glob(f"*/*/{clean_call}.json"))
            if matches:
                record_path = matches[0]

        if not record_path or not record_path.exists():
            if self.strict_test_mode:
                raise AuditStorageError(f"Cannot link report: audit file for '{call_id}' not found.")
            return False

        try:
            with open(record_path, "r", encoding="utf-8") as f:
                data = json.load(f)

            data["report_id"] = clean_rep
            if privacy_level:
                data["privacy_level"] = privacy_level

            updated_record = AuditRecord.model_validate(data)
            self.record_call(updated_record, overwrite=True)
            return True
        except Exception as exc:
            error_msg = f"Failed to link report '{clean_rep}' to call '{clean_call}': {exc}"
            logger.error(error_msg, exc_info=True)
            if self.strict_test_mode:
                raise AuditStorageError(error_msg) from exc
            return False

    def get_call(
        self,
        call_id: str,
        patient_id: Optional[str] = None,
        cycle_id: Optional[str] = None,
    ) -> Optional[AuditRecord]:
        """Reads a specific audit record from storage."""
        clean_call = self.validate_identifier(call_id, "call_id")

        if patient_id and cycle_id:
            target_path = self._resolve_record_path(patient_id, cycle_id, clean_call)
            if not target_path.exists():
                return None
            try:
                with open(target_path, "r", encoding="utf-8") as f:
                    return AuditRecord.model_validate(json.load(f))
            except Exception as exc:
                logger.error(f"Failed to read audit record from {target_path}: {exc}")
                return None

        # Search across patient/cycle directories
        matches = list(self.storage_dir.glob(f"*/*/{clean_call}.json"))
        if not matches:
            return None

        target_path = matches[0]
        try:
            with open(target_path, "r", encoding="utf-8") as f:
                return AuditRecord.model_validate(json.load(f))
        except Exception as exc:
            logger.error(f"Failed to read audit record from {target_path}: {exc}")
            return None

    def list_patients(self) -> List[str]:
        """Lists all patient identifiers that have audit records."""
        if not self.storage_dir.exists():
            return []
        patients: List[str] = []
        for entry in self.storage_dir.iterdir():
            if entry.is_dir() and _SAFE_ID_REGEX.match(entry.name):
                patients.append(entry.name)
        return sorted(patients)

    def list_cycles(self, patient_id: str) -> List[str]:
        """Lists all cycle identifiers for a specific patient."""
        clean_pat = self.validate_identifier(patient_id, "patient_id")
        patient_dir = self.storage_dir / clean_pat
        if not patient_dir.exists() or not patient_dir.is_dir():
            return []
        cycles: List[str] = []
        for entry in patient_dir.iterdir():
            if entry.is_dir() and _SAFE_ID_REGEX.match(entry.name):
                cycles.append(entry.name)
        return sorted(cycles)

    def list_calls(
        self,
        patient_id: Optional[str] = None,
        cycle_id: Optional[str] = None,
        stage: Optional[str] = None,
        sprint_id: Optional[str] = None,
    ) -> List[AuditRecordSummary]:
        """Lists audit record summaries with optional filtering."""
        if not self.storage_dir.exists():
            return []

        search_pattern = "*/*/*.json"
        if patient_id and cycle_id:
            clean_pat = self.validate_identifier(patient_id, "patient_id")
            clean_cyc = self.validate_identifier(cycle_id, "cycle_id")
            search_pattern = f"{clean_pat}/{clean_cyc}/*.json"
        elif patient_id:
            clean_pat = self.validate_identifier(patient_id, "patient_id")
            search_pattern = f"{clean_pat}/*/*.json"

        summaries: List[AuditRecordSummary] = []
        for file_path in self.storage_dir.glob(search_pattern):
            if file_path.name.endswith(".tmp") or ".tmp." in file_path.name:
                continue
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    data = json.load(f)

                if patient_id and data.get("patient_id") != patient_id:
                    continue
                if cycle_id and data.get("cycle_id") != cycle_id:
                    continue
                if stage and data.get("stage") != stage:
                    continue
                if sprint_id and data.get("sprint_id") != sprint_id:
                    continue

                summaries.append(
                    AuditRecordSummary(
                        call_id=data.get("call_id", file_path.stem),
                        patient_id=data.get("patient_id", file_path.parent.parent.name),
                        cycle_id=data.get("cycle_id", file_path.parent.name),
                        sprint_id=data.get("sprint_id"),
                        stage=data.get("stage", "general"),
                        model_role=data.get("model_role", "unknown"),
                        model_name=data.get("model_name", "unknown"),
                        status=data.get("status", "UNKNOWN"),
                        schema_name=data.get("schema_name"),
                        latency_ms=data.get("latency_ms", 0.0),
                        created_at_utc=data.get("created_at_utc", ""),
                        report_id=data.get("report_id"),
                        review_status=ClinicalReviewStatus.NOT_REVIEWED.value,
                    )
                )
            except Exception as exc:
                logger.warning(f"Skipping malformed audit file {file_path}: {exc}")

        # Sort chronologically by created_at_utc
        return sorted(summaries, key=lambda s: s.created_at_utc)


# Singleton factory
_audit_trail_service: Optional[AuditTrailService] = None


def get_audit_trail_service() -> AuditTrailService:
    global _audit_trail_service
    if _audit_trail_service is None:
        _audit_trail_service = AuditTrailService()
    return _audit_trail_service
