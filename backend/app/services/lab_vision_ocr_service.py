import asyncio
import base64
from datetime import datetime, timezone
import logging
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple
import weakref
from fastapi import UploadFile
from sqlalchemy.exc import OperationalError, IntegrityError
from sqlalchemy.orm import Session

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
)
from app.schemas.privacy import PrivacyLevel
from app.services.consilium_prompts import build_lab_vision_ocr_prompt
from app.services.gemini_gateway import GeminiGateway, GeminiModelRole
from app.services.lab_revision_consilium_service import (
    LabRevisionConsiliumService,
    PREPARATORY_OCR_REPORT_TYPE,
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


class LabVisionValidationError(ConsiliumError):
    """Raised when uploaded lab images fail file format, size, or magic bytes validation."""


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

    async def process_lab_images(
        self,
        cycle_id: str,
        files: List[UploadFile],
        is_correction: bool = False,
        sample_date_override: Optional[str] = None,
    ) -> LabVisionIntakeClientResponse:
        """
        Executes server-side Vision OCR on uploaded laboratory report pages:
        1. Validates cycle and image files.
        2. Dispatches multimodal structured request to Gemini Vision (Gemini 3.8 Flash).
        3. Reconstructs dates and strictly separates confirmed vs unconfirmed markers.
        4. Ingests ONLY confirmed markers into laboratory_ledger, avoiding unverified guesses in consilium.
        5. Persists server_only preparatory_vision_ocr report.
        6. Ensures strict transactional atomicity (rollback on failure).
        """
        cycle = self.cycle_repo.get(cycle_id)
        if not cycle:
            raise ConsiliumCycleNotFoundError(f"Medical cycle '{cycle_id}' not found.")

        # 1. Validate and read image pages in-memory
        validated_pages = await self.validate_and_read_image_files(files)
        page_count = len(validated_pages)

        # 2. Build multimodal inline images payload
        inline_images: List[Dict[str, str]] = []
        for mime_type, raw_bytes in validated_pages:
            inline_images.append({
                "mime_type": mime_type,
                "data_base64": base64.b64encode(raw_bytes).decode("ascii"),
            })

        # 3. Construct prompt and execute Gemini Vision OCR
        prompt = build_lab_vision_ocr_prompt(page_count=page_count)
        parsed_output: VisionOcrModelOutput = await self.gateway.generate_structured(
            prompt=prompt,
            response_schema=VisionOcrModelOutput,
            model=GeminiModelRole.VISION,
            inline_images=inline_images,
        )

        async with self._get_intake_lock(cycle_id):
            with self.lab_revision_service._get_ledger_thread_lock(cycle_id):
                doc_metadata = parsed_output.document_metadata
                if sample_date_override and sample_date_override.strip():
                    doc_metadata.sample_date = sample_date_override.strip()

                # 4. Resolve dates without blindly defaulting to today if absent
                for marker in parsed_output.markers:
                    if sample_date_override and sample_date_override.strip():
                        marker_sample_date = sample_date_override.strip()
                    elif marker.sample_date and marker.sample_date.strip():
                        marker_sample_date = marker.sample_date.strip()
                    elif doc_metadata.sample_date and doc_metadata.sample_date.strip():
                        marker_sample_date = doc_metadata.sample_date.strip()
                    else:
                        # Do NOT default to today: preserve date ambiguity for clinical safety
                        marker_sample_date = None

                    marker.sample_date = marker_sample_date
                    marker.evaluate_verification_requirement()

                # Separate confirmed structured results from those requiring user verification
                confirmed_markers = [m for m in parsed_output.markers if not m.requires_user_verification]
                unconfirmed_markers = [m for m in parsed_output.markers if m.requires_user_verification]

                max_intake_retries = 3
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

                        # Idempotency check: if all confirmed markers are already present with exact same values and dates,
                        # and no corrections or unconfirmed markers exist, return current state without duplicating history
                        if (
                            confirmed_markers
                            and not unconfirmed_markers
                            and not is_correction
                            and current_ledger_state.ledger_version > 0
                        ):
                            all_match = True
                            for m in confirmed_markers:
                                act = active_markers.get(m.marker_key)
                                if (
                                    not act
                                    or act.value != m.raw_value
                                    or act.sample_date != m.sample_date
                                    or act.unit != m.unit
                                ):
                                    all_match = False
                                    break
                            if all_match and len(confirmed_markers) == len(active_markers):
                                return LabVisionIntakeClientResponse(
                                    cycle_id=cycle_id,
                                    status="processed",
                                    ledger_version=current_ledger_state.ledger_version,
                                    extracted_count=len(parsed_output.markers),
                                    requires_verification_count=len(unconfirmed_markers),
                                    document_metadata=doc_metadata,
                                    markers=parsed_output.markers,
                                    confirmed_markers=confirmed_markers,
                                    unconfirmed_markers=unconfirmed_markers,
                                    has_unreviewed_lab_changes=current_ledger_state.has_unreviewed_lab_changes,
                                )

                        # Sort confirmed markers chronologically so multi-date studies are processed in sequence
                        sorted_confirmed = sorted(confirmed_markers, key=lambda m: m.sample_date or "")

                        shared_labs_table: Dict[str, Any] = {}
                        for marker in sorted_confirmed:
                            ref_dict = marker.reference_range.model_dump() if marker.reference_range else None
                            shared_labs_table[marker.marker_key] = {
                                "value": marker.raw_value,
                                "numeric_value": marker.numeric_value,
                                "unit": marker.unit,
                                "sample_date": marker.sample_date,
                                "reference_range": ref_dict,
                                "is_critical_flag": marker.is_critical_flag,
                                "category": marker.category,
                                "is_uncertain": marker.is_uncertain,
                            }

                        mutations: List[LabMarkerMutationItem] = []
                        for marker in sorted_confirmed:
                            prior_active = active_markers.get(marker.marker_key)
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
                                )
                            )

                        confirmed_observations: List[Dict[str, Any]] = []
                        for marker in sorted_confirmed:
                            confirmed_observations.append({
                                "marker_key": marker.marker_key,
                                "display_name": marker.original_name,
                                "value": marker.raw_value,
                                "numeric_value": marker.numeric_value,
                                "unit": marker.unit,
                                "sample_date": marker.sample_date,
                                "reference_range": marker.reference_range.model_dump() if marker.reference_range else None,
                                "is_critical_flag": marker.is_critical_flag,
                            })

                        # 5. Persist server_only preparatory_vision_ocr report (flushed, NOT committed)
                        ocr_payload = {
                            "agent_role": AegisAgentRole.VISION_OCR.value,
                            "document_metadata": doc_metadata.model_dump(),
                            "extracted_count": len(parsed_output.markers),
                            "confirmed_count": len(confirmed_markers),
                            "unconfirmed_count": len(unconfirmed_markers),
                            "shared_labs_table": shared_labs_table,
                            "confirmed_observations": confirmed_observations,
                            "unconfirmed_markers": [m.model_dump() for m in unconfirmed_markers],
                            "ingested_at": now_iso,
                        }
                        self.report_repo.create_next_version(
                            cycle_id=cycle_id,
                            model=self.gateway.resolve_model_name(GeminiModelRole.VISION),
                            report_type=PREPARATORY_OCR_REPORT_TYPE,
                            privacy_level=PrivacyLevel.SERVER_ONLY,
                            content=ocr_payload,
                            created_at=now_dt,
                            commit=False,
                        )

                        # 6. Ingest ONLY confirmed markers into versioned laboratory_ledger (flushed, NOT committed)
                        if current_ledger_state.ledger_version == 0 and mutations:
                            updated_state = self.lab_revision_service.get_lab_ledger_state(cycle_id, commit=False)
                            ledger_version = updated_state.ledger_version
                        elif mutations:
                            updated_state = self.lab_revision_service.upsert_laboratory_markers(
                                cycle_id=cycle_id,
                                mutations=mutations,
                                commit=False,
                            )
                            ledger_version = updated_state.ledger_version
                        else:
                            ledger_version = current_ledger_state.ledger_version

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
                    extracted_count=len(parsed_output.markers),
                    requires_verification_count=len(unconfirmed_markers),
                    document_metadata=doc_metadata,
                    markers=parsed_output.markers,
                    confirmed_markers=confirmed_markers,
                    unconfirmed_markers=unconfirmed_markers,
                    has_unreviewed_lab_changes=True,
                )
