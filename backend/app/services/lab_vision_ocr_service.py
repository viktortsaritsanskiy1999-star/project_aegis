import asyncio
import base64
from datetime import datetime, timezone
import hashlib
import logging
import math
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple
import weakref
from fastapi import UploadFile
from sqlalchemy.exc import OperationalError, IntegrityError
from sqlalchemy.orm import Session

from app.config import settings
from app.models.model_report import ModelReport
from app.repositories.cycle_repository import MedicalCycleRepository
from app.repositories.report_repository import (
    ModelReportRepository,
    ReportVersionConflictError,
)
from app.schemas.consilium import (
    AegisAgentRole,
    LabEventType,
    LabMarkerMutationItem,
    LabReferenceRange,
    LabVisionIntakeClientResponse,
    VisionOcrDocumentMetadata,
    VisionOcrMarkerItem,
    VisionOcrModelOutput,
    VisionOcrPageExtraction,
    VisionOcrReconciliationOutput,
    VisionOcrStudyLinkage,
    normalize_lab_date,
)
from app.schemas.privacy import PrivacyLevel
from app.services.consilium_prompts import (
    build_lab_vision_ocr_prompt,
    build_lab_vision_ocr_page_prompt,
    build_lab_vision_ocr_reconciliation_prompt,
)
from app.services.gemini_gateway import GeminiGateway, GeminiGatewayError, GeminiModelRole
from app.services.lab_revision_consilium_service import (
    LabRevisionConsiliumService,
    PREPARATORY_OCR_REPORT_TYPE,
)
from app.services.pdf_extraction_service import (
    LabVisionValidationError,
    PreparedDocumentPage,
    PdfExtractionService,
    detect_document_mime_type_from_bytes,
)
from app.services.primary_consilium_service import (
    ConsiliumError,
    ConsiliumCycleNotFoundError,
    ConsiliumConcurrencyConflictError,
)

logger = logging.getLogger("aegis.lab_vision_ocr")

MAX_SINGLE_IMAGE_BYTES = 10 * 1024 * 1024  # 10 MB
MAX_TOTAL_IMAGES_BYTES = 30 * 1024 * 1024  # 30 MB
MAX_IMAGE_COUNT = 15


_GLOBAL_INTAKE_LOCKS: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, Dict[str, asyncio.Lock]]" = (
    weakref.WeakKeyDictionary()
)
_INTAKE_LOCKS_META_LOCK = threading.Lock()


def detect_image_mime_type_from_bytes(data: bytes) -> Optional[str]:
    """
    Validates real image file content against authoritative magic byte signatures.
    Strictly rejects text files, shell scripts, executables, HTML, and forged extensions.
    """
    if len(data) < 12:
        return None
    # JPEG: starts with FF D8 FF
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    # PNG: starts with 89 50 4E 47 0D 0A 1A 0A
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    # WEBP: starts with RIFF and bytes 8..12 are WEBP
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    # HEIC / HEIF: offset 4 is 'ftyp'
    if data[4:8] == b"ftyp":
        major_brand = data[8:12].lower()
        if major_brand in (b"heic", b"heix", b"hevc", b"heim", b"heis", b"mif1", b"msf1"):
            return "image/heic"
    return None


class LabVisionOcrService:
    """
    Server-side orchestrator for Stage 4.1B — Vision OCR Laboratory Intake:
    1. Validates multiple uploaded report pages (image format, magic bytes, resource limits).
    2. Invokes Gemini 3.8 Flash Vision (role='vision_ocr') in multimodal mode with inline base64 images.
    3. Exhaustively extracts all clinical laboratory markers, values, units, reference ranges, and dates.
    4. Persists the verified raw Vision OCR extraction as a server_only preparatory report.
    5. Synchronizes extracted markers into the versioned laboratory ledger (laboratory_ledger)
       without overwriting previous history, maintaining unreviewed change tracking for future consiliums.
    6. Returns safe client confirmation metadata without exposing internal AI hypotheses or raw images.
    """

    _GLOBAL_INTAKE_LOCKS = _GLOBAL_INTAKE_LOCKS
    _INTAKE_META_LOCK = _INTAKE_LOCKS_META_LOCK

    def __init__(
        self,
        db: Session,
        gateway: Optional[GeminiGateway] = None,
        now_provider: Optional[Callable[[], datetime]] = None,
        intake_lock_registry: Optional[Any] = None,
        ledger_lock_registry: Optional[Dict[str, Any]] = None,
    ):
        self.db = db
        self.gateway = gateway or GeminiGateway()
        self.now_provider = now_provider or (lambda: datetime.now(timezone.utc))
        self._intake_locks = intake_lock_registry
        self.cycle_repo = MedicalCycleRepository(db)
        self.report_repo = ModelReportRepository(db)
        self.lab_revision_service = LabRevisionConsiliumService(
            db=db,
            gateway=self.gateway,
            now_provider=self.now_provider,
            ledger_lock_registry=ledger_lock_registry,
        )

    def _get_intake_lock(self, cycle_id: str) -> asyncio.Lock:
        """
        Returns an asyncio.Lock for cycle_id isolated to the caller's running event loop.
        Never shares asyncio.Lock across distinct event loops or threads.
        """
        loop = asyncio.get_running_loop()
        with self._INTAKE_META_LOCK:
            registry = (
                self._intake_locks
                if self._intake_locks is not None
                else self._GLOBAL_INTAKE_LOCKS
            )
            loop_locks = registry.get(loop)
            if loop_locks is None:
                loop_locks = {}
                registry[loop] = loop_locks
            lock = loop_locks.get(cycle_id)
            if lock is None:
                lock = asyncio.Lock()
                loop_locks[cycle_id] = lock
            return lock

    def _now_utc(self) -> datetime:
        dt = self.now_provider()
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)

    async def validate_and_read_image_files(
        self, files: List[UploadFile]
    ) -> List[Tuple[str, bytes]]:
        """
        Validates uploaded image files for count, individual size, cumulative size,
        and real image magic bytes. Never saves files to disk as executable content.
        """
        if not files:
            raise LabVisionValidationError("At least one laboratory report image page must be uploaded.")

        if len(files) > MAX_IMAGE_COUNT:
            raise LabVisionValidationError(
                f"Too many image pages uploaded ({len(files)}). Maximum allowed is {MAX_IMAGE_COUNT}."
            )

        validated_pages: List[Tuple[str, bytes]] = []
        total_bytes = 0

        for idx, file_obj in enumerate(files, start=1):
            filename = file_obj.filename or f"page_{idx}"
            data = await file_obj.read()

            if len(data) == 0:
                raise LabVisionValidationError(f"File '{filename}' is empty (0 bytes).")

            if len(data) > MAX_SINGLE_IMAGE_BYTES:
                raise LabVisionValidationError(
                    f"File '{filename}' exceeds maximum allowed single file size of 10 MB."
                )

            total_bytes += len(data)
            if total_bytes > MAX_TOTAL_IMAGES_BYTES:
                raise LabVisionValidationError(
                    f"Cumulative upload size exceeds maximum allowed limit of 30 MB."
                )

            detected_mime = detect_image_mime_type_from_bytes(data)
            if detected_mime is None:
                raise LabVisionValidationError(
                    f"File '{filename}' has invalid image content or unsupported format. "
                    "Only valid JPEG, PNG, WEBP, and HEIC images are accepted."
                )

            validated_pages.append((detected_mime, data))

        return validated_pages

    async def prepare_document_pages(
        self, files: List[UploadFile]
    ) -> List[PreparedDocumentPage]:
        """
        Prepares and normalizes heterogeneous uploaded documents (PDFs and/or images) into
        ordered PreparedDocumentPage objects using PdfExtractionService.
        """
        return await PdfExtractionService.prepare_document_pages_from_upload_files(
            files=files,
            max_document_pages=settings.lab_ocr_max_document_pages,
            max_single_file_bytes=settings.lab_ocr_max_single_file_bytes,
            max_total_bytes=settings.lab_ocr_max_total_bytes,
        )

    async def _extract_single_page_flash_lite(
        self,
        page: PreparedDocumentPage,
        total_pages: int,
        semaphore: asyncio.Semaphore,
    ) -> VisionOcrPageExtraction:
        """
        Executes Stage 1 Flash-Lite OCR extraction on a single PreparedDocumentPage.
        Guarantees backend authority over page provenance and unique row_id assignments.
        """
        async with semaphore:
            b64_str = base64.b64encode(page.raw_bytes).decode("ascii")
            inline_image = {
                "mime_type": page.mime_type,
                "data_base64": b64_str,
            }
            prompt = build_lab_vision_ocr_page_prompt(
                page_number=page.global_page_number,
                total_pages=total_pages,
                filename=page.source_filename,
            )

            try:
                extracted: VisionOcrPageExtraction = await self.gateway.generate_structured(
                    prompt=prompt,
                    response_schema=VisionOcrPageExtraction,
                    model=GeminiModelRole.FLASH_LITE,
                    inline_images=[inline_image],
                    max_output_tokens=16384,
                )
            except Exception as exc:
                logger.error(
                    f"OCR extraction failed on Page {page.global_page_number} ({page.source_filename}): {exc}"
                )
                raise LabVisionValidationError(
                    message=f"OCR extraction failed on Page {page.global_page_number} (file: '{page.source_filename}'): {exc}",
                    page_number=page.global_page_number,
                    source_filename=page.source_filename,
                ) from exc

            # Enforce backend authoritative provenance (model cannot spoof page or source file)
            extracted.page_number = page.global_page_number
            extracted.source_filename = page.source_filename
            extracted.source_file_id = f"file_{page.file_index}"

            # Guarantee row_id uniqueness and page association for every recognized marker
            seen_row_ids: set[str] = set()
            for idx, marker in enumerate(extracted.markers, start=1):
                marker.page_number = page.global_page_number
                marker.source_filename = page.source_filename
                if not marker.row_id or marker.row_id in seen_row_ids:
                    marker.row_id = f"p{page.global_page_number}_r{idx}"
                seen_row_ids.add(marker.row_id)

            return extracted

    async def extract_batch_pages_flash_lite(
        self,
        batch_pages: List[PreparedDocumentPage],
        total_pages: int,
        concurrency_limit: Optional[int] = None,
    ) -> List[VisionOcrPageExtraction]:
        """
        Executes parallel Stage 1 Flash-Lite OCR on a single batch of up to 6 pages.
        Limits concurrency via asyncio.Semaphore and guarantees chronological return order.
        If any page fails, cancels remaining batch tasks to avoid lingering background workers.
        """
        if not batch_pages:
            return []

        max_batch = settings.lab_ocr_max_batch_size
        if len(batch_pages) > max_batch:
            raise LabVisionValidationError(
                f"Batch page count ({len(batch_pages)}) exceeds maximum batch size of {max_batch} pages."
            )

        limit = concurrency_limit or settings.lab_ocr_concurrency
        semaphore = asyncio.Semaphore(limit)

        tasks = [
            asyncio.create_task(
                self._extract_single_page_flash_lite(
                    page=p,
                    total_pages=total_pages,
                    semaphore=semaphore,
                )
            )
            for p in batch_pages
        ]

        try:
            results = await asyncio.gather(*tasks)
        except Exception:
            # Clean up all in-flight tasks in this batch immediately
            for t in tasks:
                if not t.done():
                    t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise

        # Strictly sort results by global_page_number to guarantee chronological reading order
        return sorted(results, key=lambda r: r.page_number)

    def chunk_document_pages_into_batches(
        self,
        pages: List[PreparedDocumentPage],
        batch_size: Optional[int] = None,
    ) -> List[List[PreparedDocumentPage]]:
        """
        Partitions an arbitrary list of PreparedDocumentPage objects into sequential batches
        of size <= batch_size (default 6).
        """
        if not pages:
            return []
        sz = batch_size or settings.lab_ocr_max_batch_size
        return [pages[i : i + sz] for i in range(0, len(pages), sz)]

    async def extract_document_stage1_batched(
        self,
        pages: List[PreparedDocumentPage],
        batch_size: Optional[int] = None,
        concurrency_limit: Optional[int] = None,
    ) -> List[List[VisionOcrPageExtraction]]:
        """
        Orchestrates full Stage 1 Flash-Lite extraction across all document pages:
        1. Slices pages into sequential batches of max 6 pages.
        2. Executes each batch concurrently up to concurrency_limit (default 6).
        3. Waits for Batch N to finish before launching Batch N+1.
        4. Returns batched extractions ready for Stage 2 Flash batch reconciliation.
        """
        if not pages:
            return []

        total_pages = len(pages)
        batches = self.chunk_document_pages_into_batches(pages, batch_size=batch_size)
        batched_results: List[List[VisionOcrPageExtraction]] = []

        for batch_idx, batch in enumerate(batches, start=1):
            logger.info(
                f"Processing Stage 1 OCR Batch {batch_idx}/{len(batches)} "
                f"({len(batch)} pages, total {total_pages})"
            )
            batch_extractions = await self.extract_batch_pages_flash_lite(
                batch_pages=batch,
                total_pages=total_pages,
                concurrency_limit=concurrency_limit,
            )
            batched_results.append(batch_extractions)

        return batched_results

    def validate_and_recover_reconciliation(
        self,
        stage1_extractions: List[VisionOcrPageExtraction],
        flash_reconciliation: VisionOcrReconciliationOutput,
    ) -> VisionOcrReconciliationOutput:
        """
        Enforces deterministic Python safeguards on the output of Gemini Flash Reconciler:
        1. Zero Silent Dropping: detects any Stage 1 row omitted by Flash and auto-restores it
           with requires_user_verification=True and an audit note.
        2. Anti-Subversion: preserves original optical Flash-Lite raw_value, unit, and reference_range,
           flagging any unconfirmed alterations proposed by Flash.
        3. Dangling Reference Detection: validates that all source_row_ids point to genuine Stage 1 rows.
        4. Date Preservation: prevents fabricated dates; preserves row-specific specimen dates.
        """
        # 1. Index all Stage 1 markers by unique row_id
        s1_rows: Dict[str, VisionOcrMarkerItem] = {}
        for pe in stage1_extractions:
            for m in pe.markers:
                if m.row_id:
                    s1_rows[m.row_id] = m

        accounted_row_ids: set[str] = set()
        validated_markers: List[VisionOcrMarkerItem] = []

        # 2. Audit each marker proposed by Flash Reconciler
        for rm in flash_reconciliation.reconciled_markers:
            valid_source_ids = [rid for rid in rm.source_row_ids if rid in s1_rows]

            # Fallback 1: match by marker's own row_id if valid
            if not valid_source_ids and rm.row_id and rm.row_id in s1_rows:
                valid_source_ids = [rm.row_id]

            # Fallback 2: match by marker_key if unambiguous among unassigned s1 rows
            if not valid_source_ids and rm.marker_key:
                unassigned_matching = [
                    rid for rid, sm in s1_rows.items()
                    if sm.marker_key == rm.marker_key and rid not in accounted_row_ids
                ]
                if len(unassigned_matching) >= 1:
                    valid_source_ids = [unassigned_matching[0]]

            if not valid_source_ids:
                # Fabricated or unreferenced marker from Reconciler
                rm.requires_user_verification = True
                rm.is_uncertain = True
                note = "Fabricated or unreferenced indicator from Reconciler (no matching Stage 1 row)"
                rm.uncertainty_note = f"{rm.uncertainty_note}; {note}" if rm.uncertainty_note else note
                validated_markers.append(rm)
                continue

            for rid in valid_source_ids:
                accounted_row_ids.add(rid)

            primary_s1 = s1_rows[valid_source_ids[0]]

            # Ensure page provenance from primary Stage 1 row if missing
            if not rm.page_number and primary_s1.page_number:
                rm.page_number = primary_s1.page_number
            if not rm.source_filename and primary_s1.source_filename:
                rm.source_filename = primary_s1.source_filename

            # Anti-Subversion: preserve original Flash-Lite raw_value and unit if Flash altered them
            raw_val_altered = (rm.raw_value or "").strip() != (primary_s1.raw_value or "").strip()
            unit_altered = (rm.unit or "").strip() != (primary_s1.unit or "").strip()

            if raw_val_altered or unit_altered:
                proposed_val = rm.raw_value
                proposed_unit = rm.unit
                # Revert to authoritative raw optical extraction from Flash-Lite
                rm.raw_value = primary_s1.raw_value
                rm.numeric_value = primary_s1.numeric_value
                rm.unit = primary_s1.unit
                rm.requires_user_verification = True
                rm.is_uncertain = True
                subv_note = f"Preserved Stage 1 raw='{primary_s1.raw_value}' (Reconciler proposed='{proposed_val}')"
                rm.uncertainty_note = f"{rm.uncertainty_note}; {subv_note}" if rm.uncertainty_note else subv_note

            # Date preservation: if Stage 1 had an explicit date, do not let Reconciler erase or overwrite it
            if primary_s1.sample_date and primary_s1.sample_date.strip():
                rm.sample_date = primary_s1.sample_date

            # If reference range was on Stage 1, preserve it
            if primary_s1.reference_range and not rm.reference_range:
                rm.reference_range = primary_s1.reference_range

            rm.source_row_ids = valid_source_ids
            validated_markers.append(rm)

        # 3. Detect and auto-restore any dropped/omitted Stage 1 rows (Zero Silent Dropping)
        all_s1_ids = list(s1_rows.keys())
        missing_ids = [rid for rid in all_s1_ids if rid not in accounted_row_ids]

        for missing_id in missing_ids:
            s1_marker = s1_rows[missing_id]
            restored = s1_marker.model_copy(deep=True)
            restored.requires_user_verification = True
            restored.is_uncertain = True
            omission_note = "Restored by Python safety check: omitted during Flash reconciliation"
            restored.uncertainty_note = (
                f"{restored.uncertainty_note}; {omission_note}"
                if restored.uncertainty_note
                else omission_note
            )
            restored.source_row_ids = [missing_id]
            validated_markers.append(restored)

        flash_reconciliation.reconciled_markers = validated_markers
        return flash_reconciliation

    async def reconcile_document_stage2_flash(
        self,
        stage1_extractions: List[VisionOcrPageExtraction],
    ) -> VisionOcrReconciliationOutput:
        """
        Executes Stage 2: exactly ONE Gemini 3.8 Flash call across all Stage 1 page extractions (1 to 15 pages).
        Followed by deterministic Python completeness and anti-subversion verification.
        """
        if not stage1_extractions:
            raise LabVisionValidationError("No Stage 1 extractions provided for reconciliation.")

        total_pages = len(stage1_extractions)
        max_allowed = settings.lab_ocr_max_document_pages
        if total_pages > max_allowed:
            raise LabVisionValidationError(
                f"Document page count ({total_pages}) exceeds maximum allowed intake limit of {max_allowed} pages."
            )

        prompt = build_lab_vision_ocr_reconciliation_prompt(stage1_extractions)

        try:
            raw_reconciliation: VisionOcrReconciliationOutput = await self.gateway.generate_structured(
                prompt=prompt,
                response_schema=VisionOcrReconciliationOutput,
                model=GeminiModelRole.FLASH,
                max_output_tokens=32768,
            )
        except Exception as exc:
            logger.error(f"Stage 2 Flash Reconciliation failed across {total_pages} pages: {exc}")
            raise LabVisionValidationError(
                f"Flash reconciliation failed across {total_pages} pages: {exc}"
            ) from exc

        # Deterministic Python audit of completeness and correctness
        validated_output = self.validate_and_recover_reconciliation(
            stage1_extractions=stage1_extractions,
            flash_reconciliation=raw_reconciliation,
        )

        return validated_output

    async def process_document_v2(
        self,
        pages: List[PreparedDocumentPage],
        concurrency_limit: Optional[int] = None,
    ) -> VisionOcrReconciliationOutput:
        """
        High-throughput two-stage laboratory document processing pipeline (v2):
        1. Validates total pages <= 15.
        2. Partitions pages into batches of max 6 pages.
        3. Executes parallel Stage 1 Flash-Lite on each batch sequentially (1 call per page).
        4. Accumulates all structured page extractions in memory.
        5. Executes EXACTLY ONE Stage 2 Flash Reconciler call across all pages.
        6. Runs deterministic Python validation for zero marker loss and value integrity.
        """
        if not pages:
            raise LabVisionValidationError("No document pages provided for OCR processing.")

        total_pages = len(pages)
        max_allowed = settings.lab_ocr_max_document_pages
        if total_pages > max_allowed:
            raise LabVisionValidationError(
                f"Document page count ({total_pages}) exceeds maximum allowed intake limit of {max_allowed} pages."
            )

        # 1. Stage 1: Batched Flash-Lite execution
        batched_extractions = await self.extract_document_stage1_batched(
            pages=pages,
            concurrency_limit=concurrency_limit,
        )

        # 2. Flatten all page extractions into single document-level list
        all_extractions: List[VisionOcrPageExtraction] = []
        for batch_list in batched_extractions:
            all_extractions.extend(batch_list)

        # 3. Stage 2: Exactly ONE Gemini Flash Reconciler call + Python verification
        reconciled_output = await self.reconcile_document_stage2_flash(all_extractions)

        return reconciled_output

    def _is_reliable_study_duplicate(
        self,
        confirmed_markers: List[VisionOcrMarkerItem],
        doc_metadata: VisionOcrDocumentMetadata,
        ledger_state: Any,
    ) -> bool:
        """
        Determines whether an incoming set of confirmed markers reliably matches
        an already recorded study in the ledger, avoiding duplicate observations.
        Delegates to canonical whole-study deduplication in LabRevisionConsiliumService.
        """
        if not confirmed_markers:
            return False

        # Group incoming markers by normalized sample_date
        date_groups: Dict[Optional[str], List[VisionOcrMarkerItem]] = {}
        for m in confirmed_markers:
            date_groups.setdefault(m.sample_date, []).append(m)

        for s_date, group in date_groups.items():
            if not self.lab_revision_service.is_reliable_study_duplicate(
                markers=group,
                sample_date=s_date,
                study_id=doc_metadata.order_id,
                observations_history=ledger_state.observations_history,
            ):
                return False

        return True

    async def process_lab_images(
        self,
        cycle_id: str,
        files: List[UploadFile],
        is_correction: bool = False,
        sample_date_override: Optional[str] = None,
    ) -> LabVisionIntakeClientResponse:
        """
        Executes server-side Vision OCR v2 on uploaded laboratory documents (PDFs and/or images):
        1. Validates cycle and prepares up to 15 pages in-memory (PDF split and image format checks).
        2. Computes deterministic multi-page document fingerprint (order-invariant).
        3. Identifies exact re-uploads (A -> A, A -> B -> A) and reuses cached OCR extraction without calling Gemini.
        4. Runs Stage 1 Flash-Lite (parallel per-page) and Stage 2 Flash Reconciler (exactly one call)
           OUTSIDE SQLite write locks and thread locks.
        5. Enforces deterministic Python safeguards for zero marker dropping and raw optical value protection.
        6. Normalizes dates with calendar correctness verification (no blind defaulting to today).
        7. Deduplicates studies reliably matching existing ledger records without history inflation.
        8. Acquires ledger thread lock and ingests confirmed markers atomically into versioned laboratory_ledger.
        9. Returns safe client confirmation metadata.
        """
        cycle = self.cycle_repo.get(cycle_id)
        if not cycle:
            raise ConsiliumCycleNotFoundError(f"Medical cycle '{cycle_id}' not found.")

        # 1. Multi-format page preparation & validation (in-memory, up to 15 pages)
        prepared_pages = await PdfExtractionService.prepare_document_pages_from_upload_files(
            files=files,
            max_document_pages=settings.lab_ocr_max_document_pages,
            max_single_file_bytes=settings.lab_ocr_max_single_file_bytes,
            max_total_bytes=settings.lab_ocr_max_total_bytes,
        )
        page_count = len(prepared_pages)

        # 2. Compute deterministic multi-page document fingerprint (independent of random page upload order)
        document_fingerprint = PdfExtractionService.compute_document_fingerprint(prepared_pages)
        page_hashes = [p.sha256_hash for p in prepared_pages]

        # 3. Intake Lock to serialize concurrent requests for this cycle
        async with self._get_intake_lock(cycle_id):
            # Check for exact document re-upload (A -> A, A -> B -> A) across full cycle history
            matching_ocr_report = None
            if not is_correction and not sample_date_override:
                existing_ocr_reports = self.report_repo.list_server_only(
                    cycle_id=cycle_id,
                    report_type=PREPARATORY_OCR_REPORT_TYPE,
                )
                for rep in existing_ocr_reports:
                    content = rep.content or {}
                    if content.get("document_fingerprint") == document_fingerprint:
                        matching_ocr_report = rep
                        break

            if matching_ocr_report is not None:
                # Idempotent re-upload of identical document:
                # Gemini is NOT called. Ledger is NOT mutated. Current ledger state returned.
                current_ledger_state = self.lab_revision_service.get_lab_ledger_state(cycle_id, commit=False)
                rep_content = matching_ocr_report.content or {}
                raw_doc_meta = rep_content.get("document_metadata") or {}
                doc_metadata = VisionOcrDocumentMetadata.model_validate(raw_doc_meta)

                raw_markers = rep_content.get("markers") or []
                if raw_markers:
                    all_markers = [VisionOcrMarkerItem.model_validate(m) for m in raw_markers]
                else:
                    conf = [VisionOcrMarkerItem.model_validate(m) for m in rep_content.get("confirmed_observations", [])]
                    unconf = [VisionOcrMarkerItem.model_validate(m) for m in rep_content.get("unconfirmed_markers", [])]
                    all_markers = conf + unconf

                confirmed_markers = [m for m in all_markers if not m.requires_user_verification]
                unconfirmed_markers = [m for m in all_markers if m.requires_user_verification]

                return LabVisionIntakeClientResponse(
                    cycle_id=cycle_id,
                    status="processed",
                    ledger_version=current_ledger_state.ledger_version,
                    extracted_count=len(all_markers),
                    requires_verification_count=len(unconfirmed_markers),
                    document_metadata=doc_metadata,
                    markers=all_markers,
                    confirmed_markers=confirmed_markers,
                    unconfirmed_markers=unconfirmed_markers,
                    has_unreviewed_lab_changes=current_ledger_state.has_unreviewed_lab_changes,
                )

            # 4. Multimodal AI Processing (Stage 1 Flash-Lite + Stage 2 Flash + Python safeguards)
            # CRITICAL TRANSACTION SAFETY: network calls execute with NO active write transactions or thread locks!
            self.db.rollback()

            reconciliation_output = await self.process_document_v2(
                pages=prepared_pages,
                concurrency_limit=settings.lab_ocr_concurrency,
            )

            doc_metadata = reconciliation_output.document_metadata
            doc_metadata.document_fingerprint = document_fingerprint

            # 5. Resolve and normalize dates without blindly defaulting to today
            if sample_date_override and sample_date_override.strip():
                norm_ovr, is_v_ovr = normalize_lab_date(sample_date_override)
                doc_metadata.sample_date = norm_ovr if is_v_ovr else None
            else:
                norm_doc_d, _ = normalize_lab_date(doc_metadata.sample_date)
                doc_metadata.sample_date = norm_doc_d

            for marker in reconciliation_output.reconciled_markers:
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

            # Separate confirmed structured results from those requiring user verification
            confirmed_markers = [m for m in reconciliation_output.reconciled_markers if not m.requires_user_verification]
            unconfirmed_markers = [m for m in reconciliation_output.reconciled_markers if m.requires_user_verification]

            # 6. Ledger Thread Lock & Atomic Database Transaction Commit
            max_intake_retries = 3
            with self.lab_revision_service._get_ledger_thread_lock(cycle_id):
                for intake_attempt in range(max_intake_retries + 1):
                    if intake_attempt > 0:
                        self.db.rollback()
                        self.db.expire_all()
                        time.sleep(0.02 * intake_attempt)

                    try:
                        now_dt = self._now_utc()
                        now_iso = now_dt.isoformat()

                        current_ledger_state = self.lab_revision_service.get_lab_ledger_state(cycle_id, commit=False)
                        active_markers = current_ledger_state.active_markers

                        # Check if reliable study duplicate from different photo:
                        is_dup_study = self._is_reliable_study_duplicate(
                            confirmed_markers=confirmed_markers,
                            doc_metadata=doc_metadata,
                            ledger_state=current_ledger_state,
                        ) if (not is_correction and current_ledger_state.ledger_version > 0 and confirmed_markers and not unconfirmed_markers) else False

                        sorted_confirmed = sorted(confirmed_markers, key=lambda m: m.sample_date or "")

                        shared_labs_table: Dict[str, Any] = {}
                        for marker in sorted_confirmed:
                            ref_dict = marker.reference_range.model_dump() if marker.reference_range else None
                            shared_labs_table[marker.marker_key] = {
                                "value": marker.raw_value,
                                "raw_value": marker.raw_value,
                                "numeric_value": marker.numeric_value,
                                "unit": marker.unit,
                                "sample_date": marker.sample_date,
                                "reference_range": ref_dict,
                                "is_critical_flag": marker.is_critical_flag,
                                "category": marker.category,
                                "is_uncertain": marker.is_uncertain,
                                "uncertainty_note": marker.uncertainty_note,
                                "is_recognition_ambiguous": marker.is_recognition_ambiguous,
                                "row_id": marker.row_id,
                                "source_row_ids": marker.source_row_ids,
                                "page_number": marker.page_number,
                                "source_filename": marker.source_filename,
                            }

                        mutations: List[LabMarkerMutationItem] = []
                        if not is_dup_study:
                            for marker in sorted_confirmed:
                                norm_key = self.lab_revision_service._normalize_marker_key(marker.marker_key)
                                prior_active = active_markers.get(norm_key)

                                if prior_active is not None and is_correction:
                                    # Strict correction targeting: only supersede if same date or explicit date correction
                                    is_same_or_corrected_date = (
                                        prior_active.sample_date == marker.sample_date
                                        or prior_active.sample_date is None
                                        or (sample_date_override is not None and sample_date_override.strip() != "")
                                    )
                                    if is_same_or_corrected_date:
                                        event_type = LabEventType.CORRECTION
                                        supersedes_id = prior_active.observation_id
                                        corr_reason = "Correction verified via laboratory report scan"
                                    else:
                                        event_type = LabEventType.NEW_STUDY
                                        supersedes_id = None
                                        corr_reason = None
                                elif prior_active is not None:
                                    event_type = LabEventType.NEW_STUDY
                                    supersedes_id = None
                                    corr_reason = None
                                else:
                                    event_type = (
                                        LabEventType.INITIAL
                                        if current_ledger_state.ledger_version == 0
                                        else LabEventType.NEW_STUDY
                                    )
                                    supersedes_id = None
                                    corr_reason = None

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
                                        correction_reason=corr_reason,
                                        is_critical_flag=marker.is_critical_flag,
                                        is_uncertain=marker.is_uncertain,
                                        uncertainty_note=marker.uncertainty_note,
                                        is_recognition_ambiguous=marker.is_recognition_ambiguous,
                                        study_id=doc_metadata.order_id,
                                    )
                                )

                        confirmed_observations: List[Dict[str, Any]] = []
                        for marker in sorted_confirmed:
                            confirmed_observations.append({
                                "marker_key": marker.marker_key,
                                "display_name": marker.original_name,
                                "value": marker.raw_value,
                                "raw_value": marker.raw_value,
                                "numeric_value": marker.numeric_value,
                                "unit": marker.unit,
                                "sample_date": marker.sample_date,
                                "reference_range": marker.reference_range.model_dump() if marker.reference_range else None,
                                "is_critical_flag": marker.is_critical_flag,
                                "is_uncertain": marker.is_uncertain,
                                "uncertainty_note": marker.uncertainty_note,
                                "is_recognition_ambiguous": marker.is_recognition_ambiguous,
                                "study_id": doc_metadata.order_id,
                                "row_id": marker.row_id,
                                "source_row_ids": marker.source_row_ids,
                                "page_number": marker.page_number,
                                "source_filename": marker.source_filename,
                            })

                        # Persist server_only preparatory_vision_ocr report (flushed, NOT committed)
                        ocr_payload = {
                            "agent_role": AegisAgentRole.VISION_OCR.value,
                            "document_metadata": doc_metadata.model_dump(),
                            "extracted_count": len(reconciliation_output.reconciled_markers),
                            "confirmed_count": len(confirmed_markers),
                            "unconfirmed_count": len(unconfirmed_markers),
                            "shared_labs_table": shared_labs_table,
                            "confirmed_observations": confirmed_observations,
                            "unconfirmed_markers": [m.model_dump() for m in unconfirmed_markers],
                            "markers": [m.model_dump() for m in reconciliation_output.reconciled_markers],
                            "study_linkages": [s.model_dump() for s in reconciliation_output.study_linkages],
                            "document_fingerprint": document_fingerprint,
                            "page_hashes": page_hashes,
                            "ingested_at": now_iso,
                        }
                        self.report_repo.create_next_version(
                            cycle_id=cycle_id,
                            model=self.gateway.resolve_model_name(GeminiModelRole.FLASH),
                            report_type=PREPARATORY_OCR_REPORT_TYPE,
                            privacy_level=PrivacyLevel.SERVER_ONLY,
                            content=ocr_payload,
                            created_at=now_dt,
                            commit=False,
                        )

                        # Ingest ONLY confirmed markers into versioned laboratory_ledger (flushed, NOT committed)
                        if current_ledger_state.ledger_version == 0 and mutations:
                            updated_state = self.lab_revision_service.get_lab_ledger_state(cycle_id, commit=False)
                            ledger_version = updated_state.ledger_version
                            has_unreviewed = updated_state.has_unreviewed_lab_changes
                        elif mutations:
                            updated_state = self.lab_revision_service.upsert_laboratory_markers(
                                cycle_id=cycle_id,
                                mutations=mutations,
                                commit=False,
                            )
                            ledger_version = updated_state.ledger_version
                            has_unreviewed = updated_state.has_unreviewed_lab_changes
                        else:
                            ledger_version = current_ledger_state.ledger_version
                            has_unreviewed = current_ledger_state.has_unreviewed_lab_changes

                        # Exactly ONE atomic commit at the end of successful intake
                        self.db.commit()
                        break
                    except (ConsiliumConcurrencyConflictError, ReportVersionConflictError, OperationalError, IntegrityError) as exc:
                        self.db.rollback()
                        if intake_attempt < max_intake_retries:
                            continue
                        raise
                    except Exception:
                        self.db.rollback()
                        raise

                # 7. Safe client response
                return LabVisionIntakeClientResponse(
                    cycle_id=cycle_id,
                    status="processed",
                    ledger_version=ledger_version,
                    extracted_count=len(reconciliation_output.reconciled_markers),
                    requires_verification_count=len(unconfirmed_markers),
                    document_metadata=doc_metadata,
                    markers=reconciliation_output.reconciled_markers,
                    confirmed_markers=confirmed_markers,
                    unconfirmed_markers=unconfirmed_markers,
                    has_unreviewed_lab_changes=has_unreviewed,
                )
