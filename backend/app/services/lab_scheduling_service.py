import asyncio
import base64
from datetime import datetime, timedelta, timezone
import hashlib
import logging
from typing import Any, Callable, Dict, List, Optional, Tuple

from sqlalchemy.orm import Session

from app.models.model_report import ModelReport
from app.models.medical_cycle import MedicalCycle
from app.repositories.cycle_repository import MedicalCycleRepository
from app.repositories.report_repository import ModelReportRepository
from app.schemas.consilium import (
    AegisAgentRole,
    LabEventType,
    LabMarkerMutationItem,
    LabObservationEntry,
    LaboratoryDiffReport,
    are_lab_values_equivalent,
    normalize_lab_date,
    normalize_marker_slug,
)
from app.schemas.lab_scheduling import (
    BackgroundLabRevisionChiefOutput,
    BackgroundLabRevisionDeputyOutput,
    BackgroundLabRevisionReportRecord,
    LabChangeType,
    LabDocumentRecord,
    LabDocumentStatus,
    LabManualEditRequest,
    LabManualEditResponse,
    LabRevisionPendingTask,
    LabSchedulingStatusResponse,
)
from app.schemas.privacy import PrivacyLevel
from app.services.gemini_gateway import GeminiGateway, GeminiGatewayError, GeminiModelRole
from app.services.lab_revision_consilium_service import (
    LAB_REVISION_EXECUTION_LOG_REPORT_TYPE,
    LabRevisionConsiliumService,
)
from app.services.lab_vision_ocr_service import (
    LabVisionOcrService,
    LabVisionValidationError,
    detect_image_mime_type_from_bytes,
)

logger = logging.getLogger("aegis.lab_scheduling")

LAB_DOCUMENT_REPORT_TYPE = "lab_document_record"
LAB_REVISION_PENDING_TASK_REPORT_TYPE = "lab_revision_pending_task"
BACKGROUND_LAB_REVISION_REPORT_TYPE = "background_lab_revision_report"

BACKGROUND_LAB_REVISION_COOLDOWN_HOURS: int = 72
LAB_REVISION_DEBOUNCE_MINUTES: int = 15


def detect_document_mime_type(content_bytes: bytes, fallback_mime: Optional[str] = None) -> str:
    """Detects MIME type for laboratory document (images or PDF)."""
    if len(content_bytes) >= 5 and content_bytes[:5] == b"%PDF-":
        return "application/pdf"
    detected = detect_image_mime_type_from_bytes(content_bytes)
    return detected or fallback_mime or "application/octet-stream"


class LabSchedulingService:
    """
    Coordinates Stage 6A: Lab Input, Document Storage, OCR, Change Detection, and Scheduling.
    
    Invariants:
    1. Debounce (15 minutes): Every meaningful clinical change resets the debounce countdown
       to now + 15 minutes. Empty or duplicate changes do NOT reset the timer.
    2. Cooldown (72 hours): Independent cooldown of 72 hours after the last successful Lab Revision.
       During cooldown, new documents and values are saved without delay, OCR runs, and changes
       accumulate in the pending task. Effective ready time is max(debounce_until, last_revision + 72h).
    3. Document Preservation: Original document bytes are permanently preserved on the server.
       If OCR fails, the document record is retained with status='error' for controlled retry.
    4. Exact Duplicate Suppression: Exact file duplicates (cryptographic SHA-256 match within patient)
       skip OCR and do not reset the debounce timer.
    5. User Correction Protection: Automated OCR never silently overwrites previously user-corrected
       values on the same specimen date.
    6. Non-blocking & Server-Only: All internal scheduling state is strictly SERVER_ONLY in model_reports.
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
        self.lab_revision_service = LabRevisionConsiliumService(
            db=db, gateway=self.gateway, now_provider=self.now_provider
        )
        self.vision_ocr_service = LabVisionOcrService(
            db=db, gateway=self.gateway, now_provider=self.now_provider
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

    # =========================================================================
    # 1. DOCUMENT STORAGE AND DUPLICATE DETECTION
    # =========================================================================

    def get_document_records(self, cycle_id: str) -> List[LabDocumentRecord]:
        """Returns all persisted laboratory document records for cycle_id (latest version per document)."""
        reports = self.report_repo.list_server_only(
            cycle_id=cycle_id,
            report_type=LAB_DOCUMENT_REPORT_TYPE,
        )
        records: List[LabDocumentRecord] = []
        seen_ids = set()
        for r in reports:
            if r.content:
                try:
                    rec = LabDocumentRecord.model_validate(r.content)
                    if rec.document_id not in seen_ids:
                        seen_ids.add(rec.document_id)
                        records.append(rec)
                except Exception as exc:
                    logger.warning("Failed to parse LabDocumentRecord '%s': %s", r.report_id, exc)
        return records

    def find_document_by_hash(self, cycle_id: str, file_sha256: str) -> Optional[LabDocumentRecord]:
        """Finds any existing document record with matching cryptographic file hash in cycle."""
        docs = self.get_document_records(cycle_id)
        for d in docs:
            if d.file_sha256 == file_sha256 and d.status != LabDocumentStatus.ERROR:
                return d
        return None

    def save_uploaded_document(
        self,
        cycle_id: str,
        patient_id: str,
        filename: str,
        content_bytes: bytes,
        mime_type: Optional[str] = None,
    ) -> LabDocumentRecord:
        """
        Stores an uploaded document on the server with cryptographic SHA-256 hash.
        If an exact file duplicate exists for this patient/cycle, marks it DUPLICATE.
        Permanently retains original file bytes encoded in base64.
        """
        now_dt = self._now_utc()
        now_iso = now_dt.isoformat()
        file_sha256 = hashlib.sha256(content_bytes).hexdigest()
        byte_size = len(content_bytes)

        detected_mime = detect_document_mime_type(content_bytes, mime_type)

        existing_match = self.find_document_by_hash(cycle_id, file_sha256)
        if existing_match is not None:
            doc_id = f"doc_{file_sha256[:16]}_{int(now_dt.timestamp())}"
            dup_record = LabDocumentRecord(
                document_id=doc_id,
                cycle_id=cycle_id,
                patient_id=patient_id,
                filename=filename,
                mime_type=detected_mime,
                file_sha256=file_sha256,
                byte_size=byte_size,
                raw_data_base64=None,
                status=LabDocumentStatus.DUPLICATE,
                error_message=None,
                uploaded_at_utc=now_iso,
                processed_at_utc=now_iso,
                extracted_markers_count=0,
                ocr_report_id=existing_match.ocr_report_id,
                is_duplicate=True,
                duplicate_of_document_id=existing_match.document_id,
            )
            return dup_record

        doc_id = f"doc_{file_sha256[:16]}"
        record = LabDocumentRecord(
            document_id=doc_id,
            cycle_id=cycle_id,
            patient_id=patient_id,
            filename=filename,
            mime_type=detected_mime,
            file_sha256=file_sha256,
            byte_size=byte_size,
            raw_data_base64=base64.b64encode(content_bytes).decode("ascii"),
            status=LabDocumentStatus.UPLOADED,
            error_message=None,
            uploaded_at_utc=now_iso,
            processed_at_utc=None,
            extracted_markers_count=0,
            ocr_report_id=None,
            is_duplicate=False,
            duplicate_of_document_id=None,
        )

        self.report_repo.create_next_version(
            cycle_id=cycle_id,
            model="aegis-lab-intake",
            report_type=LAB_DOCUMENT_REPORT_TYPE,
            privacy_level=PrivacyLevel.SERVER_ONLY,
            content=record.model_dump(),
            created_at=now_dt,
            commit=True,
        )
        return record

    def update_document_record(self, record: LabDocumentRecord) -> None:
        """Persists updated state for a document record."""
        now_dt = self._now_utc()
        self.report_repo.create_next_version(
            cycle_id=record.cycle_id,
            model="aegis-lab-intake",
            report_type=LAB_DOCUMENT_REPORT_TYPE,
            privacy_level=PrivacyLevel.SERVER_ONLY,
            content=record.model_dump(),
            created_at=now_dt,
            commit=True,
        )

    # =========================================================================
    # 2. LAB REVISION PENDING TASK SCHEDULING (DEBOUNCE + COOLDOWN)
    # =========================================================================

    def get_last_successful_revision_timestamp(self, cycle_id: str) -> Optional[datetime]:
        """Returns the completion timestamp of the last successful Lab Revision in this cycle."""
        exec_logs = self.report_repo.list_server_only(
            cycle_id=cycle_id,
            report_type=LAB_REVISION_EXECUTION_LOG_REPORT_TYPE,
        )
        if not exec_logs:
            return None
        latest = exec_logs[0]
        c = latest.content or {}
        comp_iso = c.get("completed_at_utc")
        if comp_iso:
            return self._parse_iso_utc(comp_iso)
        raw_created = latest.created_at
        return (
            raw_created.replace(tzinfo=timezone.utc)
            if raw_created.tzinfo is None
            else raw_created.astimezone(timezone.utc)
        )

    def get_pending_task(self, cycle_id: str) -> Optional[LabRevisionPendingTask]:
        """Fetches the active pending task for cycle_id if one exists."""
        reports = self.report_repo.list_server_only(
            cycle_id=cycle_id,
            report_type=LAB_REVISION_PENDING_TASK_REPORT_TYPE,
        )
        if not reports:
            return None
        content = reports[0].content or {}
        try:
            return LabRevisionPendingTask.model_validate(content)
        except Exception as exc:
            logger.warning("Failed to parse LabRevisionPendingTask for cycle '%s': %s", cycle_id, exc)
            return None

    def update_or_create_pending_task(
        self,
        cycle_id: str,
        patient_id: str,
        is_meaningful_change: bool,
        change_descriptions: Optional[List[str]] = None,
        pending_doc_id: Optional[str] = None,
    ) -> Optional[LabRevisionPendingTask]:
        """
        Creates or updates the persistent pending Lab Revision task:
        - If is_meaningful_change is True:
          * Sets / restarts debounce timer: debounce_until = now + 15 minutes.
          * Computes effective_ready_at = max(debounce_until, last_successful_revision + 72 hours).
          * Increments accumulated_changes_count.
        - If is_meaningful_change is False:
          * Debounce timer is NOT reset.
        - Persists task in model_reports (SERVER_ONLY).
        """
        now_dt = self._now_utc()
        now_iso = now_dt.isoformat()

        last_rev_dt = self.get_last_successful_revision_timestamp(cycle_id)
        cooldown_until_dt = (
            last_rev_dt + timedelta(hours=BACKGROUND_LAB_REVISION_COOLDOWN_HOURS)
            if last_rev_dt is not None
            else None
        )

        existing_task = self.get_pending_task(cycle_id)

        # Check pending documents
        all_docs = self.get_document_records(cycle_id)
        has_pending_ocr = any(
            d.status in (LabDocumentStatus.UPLOADED, LabDocumentStatus.PROCESSING)
            for d in all_docs
        )
        pending_doc_ids = [
            d.document_id for d in all_docs
            if d.status in (LabDocumentStatus.UPLOADED, LabDocumentStatus.PROCESSING)
        ]

        ledger_state = self.lab_revision_service.get_lab_ledger_state(cycle_id)
        target_ledger_version = ledger_state.ledger_version

        if not is_meaningful_change and existing_task is None:
            return None

        if is_meaningful_change:
            debounce_until_dt = now_dt + timedelta(minutes=LAB_REVISION_DEBOUNCE_MINUTES)
            if cooldown_until_dt is not None and cooldown_until_dt > debounce_until_dt:
                effective_ready_dt = cooldown_until_dt
            else:
                effective_ready_dt = debounce_until_dt

            first_change_iso = (
                existing_task.first_meaningful_change_at_utc
                if existing_task and existing_task.first_meaningful_change_at_utc
                else now_iso
            )
            accumulated = (existing_task.accumulated_changes_count + 1) if existing_task else 1
            summary = list(existing_task.change_summary) if existing_task else []
            if change_descriptions:
                summary.extend(change_descriptions)

            task = LabRevisionPendingTask(
                task_id=f"lab_rev_task_{cycle_id}",
                cycle_id=cycle_id,
                patient_id=patient_id,
                status="pending",
                accumulated_changes_count=accumulated,
                first_meaningful_change_at_utc=first_change_iso,
                last_meaningful_change_at_utc=now_iso,
                debounce_until_utc=debounce_until_dt.isoformat(),
                cooldown_until_utc=cooldown_until_dt.isoformat() if cooldown_until_dt else None,
                effective_ready_at_utc=effective_ready_dt.isoformat(),
                has_pending_ocr=has_pending_ocr,
                pending_document_ids=pending_doc_ids,
                target_ledger_version=target_ledger_version,
                change_summary=summary,
                created_at_utc=existing_task.created_at_utc if existing_task else now_iso,
                updated_at_utc=now_iso,
            )
        else:
            assert existing_task is not None
            task = existing_task
            task.has_pending_ocr = has_pending_ocr
            task.pending_document_ids = pending_doc_ids
            task.target_ledger_version = target_ledger_version
            task.updated_at_utc = now_iso

        self.report_repo.create_next_version(
            cycle_id=cycle_id,
            model="aegis-lab-scheduler",
            report_type=LAB_REVISION_PENDING_TASK_REPORT_TYPE,
            privacy_level=PrivacyLevel.SERVER_ONLY,
            content=task.model_dump(),
            created_at=now_dt,
            commit=True,
        )
        return task

    def update_pending_task(self, task: LabRevisionPendingTask) -> None:
        """Persists updated state for a pending task."""
        now_dt = self._now_utc()
        self.report_repo.create_next_version(
            cycle_id=task.cycle_id,
            model="aegis-lab-scheduler",
            report_type=LAB_REVISION_PENDING_TASK_REPORT_TYPE,
            privacy_level=PrivacyLevel.SERVER_ONLY,
            content=task.model_dump(),
            created_at=now_dt,
            commit=True,
        )

    def evaluate_task_readiness(self, cycle_id: str) -> Tuple[bool, str]:
        """
        Determines whether the pending Lab Revision task is ready for execution.
        Returns (is_ready, reason).
        Not ready if:
        - No pending task exists or status is completed.
        - Pending or in-flight OCR operations exist.
        - Current time is before debounce_until_utc.
        - Current time is before cooldown_until_utc.
        - No unreviewed laboratory changes exist in ledger.
        """
        task = self.get_pending_task(cycle_id)
        if task is None or task.status == "completed":
            return False, "No active pending lab revision task found."

        docs = self.get_document_records(cycle_id)
        has_active_ocr = any(
            d.status in (LabDocumentStatus.UPLOADED, LabDocumentStatus.PROCESSING)
            for d in docs
        )
        if has_active_ocr:
            return False, "Pending OCR extraction operations are in progress."

        has_failed_ocr = any(d.status == LabDocumentStatus.ERROR for d in docs)
        ledger_state = self.lab_revision_service.get_lab_ledger_state(cycle_id)
        if has_failed_ocr:
            if not ledger_state.has_unreviewed_lab_changes:
                return False, "Document OCR has failed and requires controlled resolution before revision."
            # If valid unreviewed changes exist from other documents or manual edits,
            # permanent OCR error does not indefinitely block revision of the valid data.

        if not ledger_state.has_unreviewed_lab_changes:
            return False, "No unreviewed laboratory changes in ledger."

        now_dt = self._now_utc()
        debounce_dt = self._parse_iso_utc(task.debounce_until_utc)
        if debounce_dt is not None and now_dt < debounce_dt:
            rem_sec = int((debounce_dt - now_dt).total_seconds())
            return False, f"Debounce period active ({rem_sec}s remaining)."

        cooldown_dt = self._parse_iso_utc(task.cooldown_until_utc)
        if cooldown_dt is not None and now_dt < cooldown_dt:
            rem_sec = int((cooldown_dt - now_dt).total_seconds())
            return False, f"72-hour cooldown period active ({rem_sec // 3600}h remaining)."

        return True, "Ready for background Lab Revision Consilium execution."

    def get_ready_tasks_for_execution(self) -> List[LabRevisionPendingTask]:
        """
        Returns all LabRevisionPendingTask instances across active cycles that are
        ready for background execution (debounce elapsed, 72h cooldown elapsed,
        no in-flight OCR, valid ledger changes exist).
        """
        active_cycles = (
            self.db.query(MedicalCycle)
            .filter(MedicalCycle.status == "active")
            .all()
        )
        ready_tasks: List[LabRevisionPendingTask] = []
        for cycle in active_cycles:
            is_ready, _reason = self.evaluate_task_readiness(cycle.cycle_id)
            if is_ready:
                task = self.get_pending_task(cycle.cycle_id)
                if task is not None and task.status != "completed":
                    ready_tasks.append(task)
        return ready_tasks

    def complete_revision_task(
        self,
        cycle_id: str,
        evaluated_ledger_version: int,
    ) -> Optional[LabRevisionPendingTask]:
        """
        Completes the background Lab Revision task:
        1. Records the completion timestamp and evaluated ledger version in the execution log.
        2. Starts the authoritative 72-hour cooldown.
        3. If ledger version changed during execution (N vs N+1):
           Keeps task active for version N+1 with its own debounce and 72-hour cooldown.
        4. If no newer data arrived:
           Marks the pending task status as 'completed'.
        """
        now_dt = self._now_utc()
        now_iso = now_dt.isoformat()
        cooldown_until_dt = now_dt + timedelta(hours=BACKGROUND_LAB_REVISION_COOLDOWN_HOURS)

        # 1. Authoritative execution log (SERVER_ONLY) establishing 72-hour cooldown
        self.report_repo.create_next_version(
            cycle_id=cycle_id,
            model="aegis-lab-scheduler",
            report_type=LAB_REVISION_EXECUTION_LOG_REPORT_TYPE,
            privacy_level=PrivacyLevel.SERVER_ONLY,
            content={
                "completed_at_utc": now_iso,
                "evaluated_ledger_version": evaluated_ledger_version,
                "cooldown_hours": BACKGROUND_LAB_REVISION_COOLDOWN_HOURS,
                "consilium_kind": "background_lab_revision",
            },
            created_at=now_dt,
            commit=True,
        )

        task = self.get_pending_task(cycle_id)
        if task is None:
            return None

        current_ledger = self.lab_revision_service.get_lab_ledger_state(cycle_id)

        if current_ledger.ledger_version <= evaluated_ledger_version:
            # Everything up to evaluated_ledger_version is completed
            task.status = "completed"
            task.cooldown_until_utc = cooldown_until_dt.isoformat()
            task.effective_ready_at_utc = cooldown_until_dt.isoformat()
            task.accumulated_changes_count = 0
            task.change_summary = []
            task.updated_at_utc = now_iso
            self.update_pending_task(task)
            return task
        else:
            # Newer data (N+1) arrived while version N was being revised!
            task.status = "pending"
            task.target_ledger_version = current_ledger.ledger_version
            task.cooldown_until_utc = cooldown_until_dt.isoformat()
            debounce_dt = self._parse_iso_utc(task.debounce_until_utc) or (now_dt + timedelta(minutes=LAB_REVISION_DEBOUNCE_MINUTES))
            task.effective_ready_at_utc = max(debounce_dt, cooldown_until_dt).isoformat()
            task.updated_at_utc = now_iso
            self.update_pending_task(task)
            return task

    def get_scheduling_status(self, cycle_id: str) -> LabSchedulingStatusResponse:
        """Client-safe query of scheduling readiness."""
        is_ready, reason = self.evaluate_task_readiness(cycle_id)
        task = self.get_pending_task(cycle_id)
        docs = self.get_document_records(cycle_id)
        unread_docs = sum(1 for d in docs if d.status in (LabDocumentStatus.UPLOADED, LabDocumentStatus.PROCESSING))
        ledger_state = self.lab_revision_service.get_lab_ledger_state(cycle_id)

        return LabSchedulingStatusResponse(
            cycle_id=cycle_id,
            has_pending_task=task is not None and task.status != "completed",
            is_ready_for_revision=is_ready,
            debounce_until_utc=task.debounce_until_utc if task else None,
            cooldown_until_utc=task.cooldown_until_utc if task else None,
            effective_ready_at_utc=task.effective_ready_at_utc if task else None,
            has_pending_ocr=unread_docs > 0,
            accumulated_changes_count=task.accumulated_changes_count if task else 0,
            unread_documents_count=unread_docs,
            evaluation_status=ledger_state.evaluation_status,
            readiness_reason=reason,
        )

    # =========================================================================
    # 3. MANUAL EDITING WITH CHANGE DETECTION
    # =========================================================================

    def process_manual_edit(
        self,
        cycle_id: str,
        patient_id: str,
        mutations: List[LabMarkerMutationItem],
    ) -> LabManualEditResponse:
        """
        Processes client-submitted manual edits or corrections to laboratory markers:
        1. Compares proposed mutations against current active ledger markers.
        2. Detects meaningful changes (value, unit, date, ref range, or new marker).
        3. If all proposed values are identical to current active records (e.g. 4 -> 4):
           -> Returns NO_CHANGE without mutating ledger or resetting debounce timer.
        4. If at least one meaningful change exists (e.g. 4 -> 4.5):
           -> Marks mutation as user-corrected (is_user_corrected=True).
           -> Ingests mutations into versioned laboratory_ledger.
           -> Resets/updates pending task debounce countdown (now + 15 minutes).
           -> Returns MEANINGFUL_CHANGE.
        """
        if not mutations:
            raise ValueError("At least one laboratory marker mutation must be provided.")

        ledger_state = self.lab_revision_service.get_lab_ledger_state(cycle_id)
        active_markers = ledger_state.active_markers

        meaningful_changes: List[str] = []

        for mut in mutations:
            norm_key = normalize_marker_slug(mut.marker_key)
            prior = active_markers.get(norm_key)

            if prior is None:
                meaningful_changes.append(f"Added new marker '{mut.marker_key}' with value {mut.value}")
                continue

            # Compare value, unit, sample_date, and reference_range
            mut_num, resolved_unit = self.lab_revision_service._parse_numeric_and_unit(mut.value, mut.unit)
            effective_unit = resolved_unit or mut.unit
            is_same_val = are_lab_values_equivalent(
                prior.value,
                prior.numeric_value,
                prior.unit,
                mut.value,
                mut_num,
                effective_unit,
            )
            is_same_date = (prior.sample_date or "").strip() == (mut.sample_date or "").strip()

            ref_differ = self.lab_revision_service._reference_ranges_differ(
                prior.reference_range, mut.reference_range
            )

            if not is_same_val or not is_same_date or ref_differ:
                meaningful_changes.append(
                    f"Modified marker '{mut.marker_key}': {prior.value} -> {mut.value} "
                    f"(unit: {mut.unit}, date: {mut.sample_date})"
                )

        if not meaningful_changes:
            # NO_CHANGE: e.g. 4 -> 4
            return LabManualEditResponse(
                cycle_id=cycle_id,
                status="processed",
                change_type=LabChangeType.NO_CHANGE,
                ledger_version=ledger_state.ledger_version,
                mutations_applied=0,
                has_unreviewed_lab_changes=ledger_state.has_unreviewed_lab_changes,
                summary="No meaningful changes detected; laboratory ledger remains unchanged.",
            )

        # MEANINGFUL_CHANGE: apply user corrections to ledger
        prepared_mutations: List[LabMarkerMutationItem] = []
        for mut in mutations:
            mut_dict = mut.model_dump()
            mut_dict["is_user_corrected"] = True
            norm_key = normalize_marker_slug(mut.marker_key)
            prior = active_markers.get(norm_key)
            if prior is not None:
                mut_dict["event_type"] = LabEventType.CORRECTION
                mut_dict["supersedes_observation_id"] = prior.observation_id
                mut_dict["correction_reason"] = mut.correction_reason or "Manual user edit"
            prepared_mutations.append(LabMarkerMutationItem.model_validate(mut_dict))

        updated_state = self.lab_revision_service.upsert_laboratory_markers(
            cycle_id=cycle_id,
            mutations=prepared_mutations,
            commit=True,
        )

        # Update debounce timer in pending task
        self.update_or_create_pending_task(
            cycle_id=cycle_id,
            patient_id=patient_id,
            is_meaningful_change=True,
            change_descriptions=meaningful_changes,
        )

        return LabManualEditResponse(
            cycle_id=cycle_id,
            status="processed",
            change_type=LabChangeType.MEANINGFUL_CHANGE,
            ledger_version=updated_state.ledger_version,
            mutations_applied=len(prepared_mutations),
            has_unreviewed_lab_changes=updated_state.has_unreviewed_lab_changes,
            summary=f"Applied {len(prepared_mutations)} manual updates; debounce timer set to 15 minutes.",
        )

    # =========================================================================
    # 4. DOCUMENT OCR PROCESSING WITH OVERWRITE PROTECTION & ERROR RETENTION
    # =========================================================================

    async def process_document_ocr(
        self,
        cycle_id: str,
        patient_id: str,
        doc_record: LabDocumentRecord,
        is_correction: bool = False,
        sample_date_override: Optional[str] = None,
    ) -> Tuple[LabChangeType, LabDocumentRecord]:
        """
        Executes OCR on an uploaded document:
        - If DUPLICATE -> skips OCR and returns DUPLICATE_FILE.
        - Updates document status to PROCESSING.
        - Invokes Gemini Vision OCR.
        - If OCR fails -> saves document with status ERROR and error_message.
          DOES NOT delete or discard original file bytes!
        - If OCR succeeds ->
          * Preserves user-corrected values against silent overwrite.
          * Ingests confirmed observations into ledger.
          * Detects meaningful changes.
          * Updates debounce timer if meaningful change.
        """
        if doc_record.is_duplicate:
            return LabChangeType.DUPLICATE_FILE, doc_record

        now_dt = self._now_utc()
        doc_record.status = LabDocumentStatus.PROCESSING
        self.update_document_record(doc_record)

        if not doc_record.raw_data_base64:
            doc_record.status = LabDocumentStatus.ERROR
            doc_record.error_message = "Corrupted document record: raw_data_base64 is empty."
            self.update_document_record(doc_record)
            return LabChangeType.NO_CHANGE, doc_record

        raw_bytes = base64.b64decode(doc_record.raw_data_base64)
        page_count = 1

        try:
            prompt = f"Analyze laboratory report page and extract all markers in structured format."
            inline_images = [{
                "mime_type": doc_record.mime_type,
                "data_base64": doc_record.raw_data_base64,
            }]

            from app.schemas.consilium import VisionOcrModelOutput
            from app.services.consilium_prompts import build_lab_vision_ocr_prompt

            ocr_prompt = build_lab_vision_ocr_prompt(page_count=1)
            parsed_output: VisionOcrModelOutput = await self.gateway.generate_structured(
                prompt=ocr_prompt,
                response_schema=VisionOcrModelOutput,
                model=GeminiModelRole.VISION,
                inline_images=inline_images,
            )
        except Exception as exc:
            logger.error("OCR extraction failed for document '%s': %s", doc_record.document_id, exc)
            doc_record.status = LabDocumentStatus.ERROR
            doc_record.error_message = str(exc)
            self.update_document_record(doc_record)
            # Re-evaluate pending task so it reflects uncompleted OCR
            self.update_or_create_pending_task(cycle_id, patient_id, is_meaningful_change=False)
            return LabChangeType.NO_CHANGE, doc_record

        # OCR extraction succeeded
        doc_metadata = parsed_output.document_metadata
        doc_metadata.document_fingerprint = doc_record.file_sha256

        # Date normalization
        if sample_date_override and sample_date_override.strip():
            norm_ovr, is_v_ovr = normalize_lab_date(sample_date_override)
            doc_metadata.sample_date = norm_ovr if is_v_ovr else None
        else:
            norm_doc_d, _ = normalize_lab_date(doc_metadata.sample_date)
            doc_metadata.sample_date = norm_doc_d

        for marker in parsed_output.markers:
            if sample_date_override and sample_date_override.strip():
                marker_sample_date = doc_metadata.sample_date
            elif marker.sample_date and marker.sample_date.strip():
                norm_m_d, _ = normalize_lab_date(marker.sample_date)
                marker_sample_date = norm_m_d
            elif doc_metadata.sample_date and doc_metadata.sample_date.strip():
                marker_sample_date = doc_metadata.sample_date
            else:
                marker_sample_date = None
            marker.sample_date = marker_sample_date
            marker.evaluate_verification_requirement()

        confirmed_markers = [m for m in parsed_output.markers if not m.requires_user_verification]

        # Check whole-study duplicate against existing ledger records (different photo, same data)
        current_ledger_state = self.lab_revision_service.get_lab_ledger_state(cycle_id)
        is_dup_study = self.lab_vision_service._is_reliable_study_duplicate(
            confirmed_markers=confirmed_markers,
            doc_metadata=doc_metadata,
            ledger_state=current_ledger_state,
        ) if (not is_correction and current_ledger_state.ledger_version > 0 and confirmed_markers) else False

        if is_dup_study:
            doc_record.status = LabDocumentStatus.PROCESSED
            doc_record.processed_at_utc = self._now_utc().isoformat()
            doc_record.extracted_markers_count = len(confirmed_markers)
            self.update_document_record(doc_record)
            return LabChangeType.NO_CHANGE, doc_record

        # Overwrite protection: do NOT overwrite previously user-corrected values with OCR!
        active_markers = current_ledger_state.active_markers
        mutations: List[LabMarkerMutationItem] = []

        for marker in confirmed_markers:
            norm_key = normalize_marker_slug(marker.marker_key)
            prior = active_markers.get(norm_key)

            # Invariant 5: Protection for user-corrected values
            if (
                prior is not None
                and getattr(prior, "is_user_corrected", False)
                and not is_correction
            ):
                logger.info(
                    "Skipping OCR overwrite of user-corrected marker '%s' (active value: %s)",
                    norm_key, prior.value
                )
                continue

            if prior is not None and is_correction:
                event_type = LabEventType.CORRECTION
                supersedes_id = prior.observation_id
            elif prior is not None:
                event_type = LabEventType.NEW_STUDY
                supersedes_id = None
            else:
                event_type = LabEventType.INITIAL if current_ledger_state.ledger_version == 0 else LabEventType.NEW_STUDY
                supersedes_id = None

            mutations.append(
                LabMarkerMutationItem(
                    marker_key=marker.marker_key,
                    display_name=marker.original_name,
                    event_type=event_type,
                    value=marker.raw_value,
                    unit=marker.unit,
                    sample_date=marker.sample_date,
                    reference_range=marker.reference_range,
                    supersedes_observation_id=supersedes_id,
                    correction_reason="Extracted via OCR document scan",
                    is_critical_flag=marker.is_critical_flag,
                    is_uncertain=marker.is_uncertain,
                    uncertainty_note=marker.uncertainty_note,
                    is_recognition_ambiguous=marker.is_recognition_ambiguous,
                    study_id=doc_metadata.order_id,
                    is_user_corrected=False,
                )
            )

        if not mutations:
            doc_record.status = LabDocumentStatus.PROCESSED
            doc_record.processed_at_utc = self._now_utc().isoformat()
            doc_record.extracted_markers_count = 0
            self.update_document_record(doc_record)
            return LabChangeType.NO_CHANGE, doc_record

        pre_version = current_ledger_state.ledger_version
        updated_state = self.lab_revision_service.upsert_laboratory_markers(
            cycle_id=cycle_id,
            mutations=mutations,
            commit=True,
        )

        # Change detection via diff report
        diff = self.lab_revision_service.compute_laboratory_diff(
            cycle_id=cycle_id,
            baseline_version=pre_version,
            target_version=updated_state.ledger_version,
        )

        has_meaningful_diff = bool(
            diff.added_markers
            or diff.modified_values
            or diff.corrected_error_values
            or diff.new_dated_studies
            or diff.unit_changed_markers
            or diff.removed_or_invalidated_markers
        )

        change_type = LabChangeType.MEANINGFUL_CHANGE if has_meaningful_diff else LabChangeType.NO_CHANGE

        # Update pending task if meaningful change
        if change_type == LabChangeType.MEANINGFUL_CHANGE:
            change_summaries = [item.neutral_summary for item in (
                diff.added_markers + diff.modified_values + diff.corrected_error_values + diff.new_dated_studies
            )]
            self.update_or_create_pending_task(
                cycle_id=cycle_id,
                patient_id=patient_id,
                is_meaningful_change=True,
                change_descriptions=change_summaries,
            )

        doc_record.status = LabDocumentStatus.PROCESSED
        doc_record.processed_at_utc = self._now_utc().isoformat()
        doc_record.extracted_markers_count = len(confirmed_markers)
        self.update_document_record(doc_record)

        return change_type, doc_record

    @property
    def lab_vision_service(self) -> LabVisionOcrService:
        return self.vision_ocr_service
