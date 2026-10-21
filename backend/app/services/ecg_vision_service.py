import asyncio
import base64
from datetime import datetime, timezone
import hashlib
import logging
from typing import Callable, Dict, Optional, Tuple
from fastapi import UploadFile
from sqlalchemy.orm import Session

from app.models.model_report import ModelReport
from app.repositories.cycle_repository import MedicalCycleRepository
from app.repositories.report_repository import ModelReportRepository
from app.schemas.consilium import (
    AegisAgentRole,
    EcgVisionIntakeClientResponse,
    VisionEcgModelOutput,
)
from app.schemas.privacy import PrivacyLevel
from app.services.consilium_prompts import build_vision_ecg_prompt
from app.services.gemini_gateway import GeminiGateway, GeminiModelRole
from app.services.lab_vision_ocr_service import detect_image_mime_type_from_bytes
from app.services.primary_consilium_service import (
    ConsiliumError,
    ConsiliumCycleNotFoundError,
    PREPARATORY_ECG_REPORT_TYPE,
    PrimaryConsiliumService,
)

logger = logging.getLogger("aegis.vision_ecg")

MAX_ECG_IMAGE_BYTES = 10 * 1024 * 1024  # 10 MB per ECG image


class EcgVisionValidationError(ConsiliumError):
    """Raised when uploaded electrocardiogram image fails file format, size, or magic bytes validation."""


# Note on concurrency:
# _ecg_intake_locks provides in-flight concurrency protection within a single Python process.
# In a multi-worker deployment (e.g. multi-process Uvicorn/Gunicorn), cross-process serialization
# relies on database-level transactions, and subsequent full cross-process distributed locks
# will be coordinated in future stages.
_ecg_intake_locks: Dict[str, asyncio.Lock] = {}


class EcgVisionService:
    """
    Server-side orchestrator for Stage 4.1C — Vision ECG Electrocardiogram Intake:
    1. Validates uploaded ECG image file (format, magic bytes, 10MB size limit).
    2. Invokes Gemini 3.8 Flash Vision (role='vision_ecg') in multimodal mode with inline base64 image.
    3. Accurately digitizes observable metrics (rate, rhythm, intervals, morphology) without fabricating
       unobservable measurements (strictly forbidding electrical axis estimation on single-lead recordings).
    4. Persists the structured result as a server_only preparatory report (preparatory_vision_ecg),
       maintaining full backwards-compatibility with Primary Consilium, Weekly Consilium, and CVS organ specialist.
    5. Returns safe client confirmation metadata without exposing internal AI hypotheses, raw images, or CoT.
    6. Ensures strict transactional atomicity and idempotency.
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
        self.primary_service = PrimaryConsiliumService(db=db, gateway=self.gateway)

    @classmethod
    def _get_intake_lock(cls, cycle_id: str) -> asyncio.Lock:
        if cycle_id not in _ecg_intake_locks:
            _ecg_intake_locks[cycle_id] = asyncio.Lock()
        return _ecg_intake_locks[cycle_id]

    def _now_utc(self) -> datetime:
        dt = self.now_provider()
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)

    async def validate_and_read_image_file(self, file: UploadFile) -> Tuple[str, bytes]:
        """
        Validates file presence, size limits, and authoritative magic bytes.
        Returns (mime_type, raw_bytes).
        """
        if not file or not file.filename:
            raise EcgVisionValidationError("No file uploaded or file has empty filename.")

        try:
            content = await file.read()
        except Exception as exc:
            raise EcgVisionValidationError(f"Failed to read uploaded file '{file.filename}': {exc}") from exc

        if not content:
            raise EcgVisionValidationError(f"File '{file.filename}' is empty (0 bytes).")

        if len(content) > MAX_ECG_IMAGE_BYTES:
            max_mb = MAX_ECG_IMAGE_BYTES // (1024 * 1024)
            raise EcgVisionValidationError(
                f"File '{file.filename}' exceeds maximum allowed size of {max_mb} MB "
                f"({len(content)} bytes)."
            )

        detected_mime = detect_image_mime_type_from_bytes(content)
        if not detected_mime:
            raise EcgVisionValidationError(
                f"File '{file.filename}' is not a supported valid image format. "
                "Supported formats are JPEG, PNG, WEBP, and HEIC/HEIF with authentic magic bytes."
            )

        return detected_mime, content

    async def process_ecg_image(
        self,
        cycle_id: str,
        file: UploadFile,
        record_date_override: Optional[str] = None,
    ) -> EcgVisionIntakeClientResponse:
        """
        Executes end-to-end server-side ECG vision intake:
        1. Validates medical cycle existence.
        2. Validates image file format, size, and magic bytes in-memory.
        3. Enforces idempotency via image SHA256 hash.
        4. Invokes Gemini Vision with clinical directives.
        5. Persists server_only preparatory_vision_ecg report atomically.
        6. Returns safe client response.
        """
        cycle = self.cycle_repo.get(cycle_id)
        if not cycle:
            raise ConsiliumCycleNotFoundError(f"Medical cycle '{cycle_id}' not found.")

        # 1. Validate and read image
        mime_type, raw_bytes = await self.validate_and_read_image_file(file)
        image_hash = hashlib.sha256(raw_bytes).hexdigest()

        async with self._get_intake_lock(cycle_id):
            now_dt = self._now_utc()
            now_iso = now_dt.isoformat()
            primary_concluded = self.primary_service.is_primary_consilium_completed(cycle_id)

            # 2. Check for idempotent re-upload of identical ECG image
            existing_reports = self.report_repo.list_server_only(
                cycle_id=cycle_id,
                report_type=PREPARATORY_ECG_REPORT_TYPE,
            )
            if existing_reports:
                latest = existing_reports[0]
                latest_content = latest.content or {}
                if latest_content.get("image_hash") == image_hash:
                    logger.info(
                        "Idempotent ECG upload detected for cycle '%s' (version %d, hash %.10s).",
                        cycle_id,
                        latest.version,
                        image_hash,
                    )
                    created_at_iso = (
                        latest.created_at.replace(tzinfo=timezone.utc).isoformat()
                        if latest.created_at and latest.created_at.tzinfo is None
                        else (latest.created_at.astimezone(timezone.utc).isoformat() if latest.created_at else now_iso)
                    )
                    return EcgVisionIntakeClientResponse(
                        cycle_id=cycle_id,
                        status="processed",
                        intake_version=latest.version,
                        recording_type=str(latest_content.get("recording_type", "unknown")),
                        recording_quality=str(latest_content.get("recording_quality", "moderate")),
                        created_at_utc=created_at_iso,
                        primary_consilium_concluded=primary_concluded,
                    )

            # 3. Multimodal Gemini Vision call
            inline_images = [{
                "mime_type": mime_type,
                "data_base64": base64.b64encode(raw_bytes).decode("ascii"),
            }]
            prompt = build_vision_ecg_prompt()
            parsed_output: VisionEcgModelOutput = await self.gateway.generate_structured(
                prompt=prompt,
                response_schema=VisionEcgModelOutput,
                model=GeminiModelRole.VISION,
                inline_images=inline_images,
            )

            # 4. Construct DB storage payload (strictly server_only)
            resolved_record_date = (
                record_date_override.strip()
                if record_date_override and record_date_override.strip()
                else now_iso[:10]
            )

            ecg_payload = {
                "agent_role": AegisAgentRole.VISION_ECG.value,
                "ecg_summary": parsed_output.ecg_summary,
                "recording_type": parsed_output.recording_type,
                "recording_quality": parsed_output.recording_quality,
                "lead_name": parsed_output.lead_name,
                "heart_rate_bpm": parsed_output.heart_rate_bpm,
                "rhythm_type": parsed_output.rhythm_type,
                "rhythm_regularity": parsed_output.rhythm_regularity,
                "intervals": {
                    "pr_interval_ms": parsed_output.pr_interval_ms,
                    "qrs_duration_ms": parsed_output.qrs_duration_ms,
                    "qt_interval_ms": parsed_output.qt_interval_ms,
                    "qtc_interval_ms": parsed_output.qtc_interval_ms,
                },
                "electrical_axis": parsed_output.electrical_axis,
                "st_segment_findings": parsed_output.st_segment_findings,
                "t_wave_findings": parsed_output.t_wave_findings,
                "limitations": parsed_output.limitations,
                "uncertainties": parsed_output.uncertainties,
                "potential_urgent_warning": parsed_output.potential_urgent_warning,
                "raw_unresolved_metrics": parsed_output.raw_unresolved_metrics,
                "record_date": resolved_record_date,
                "image_hash": image_hash,
                "ingested_at": now_iso,
            }

            try:
                new_report = self.report_repo.create_next_version(
                    cycle_id=cycle_id,
                    model=self.gateway.resolve_model_name(GeminiModelRole.VISION),
                    report_type=PREPARATORY_ECG_REPORT_TYPE,
                    privacy_level=PrivacyLevel.SERVER_ONLY,
                    content=ecg_payload,
                    created_at=now_dt,
                )
                self.db.commit()
            except Exception:
                self.db.rollback()
                raise

            # 5. Safe client response (pure operational metadata, no intermediate medical inferences)
            created_at_iso = (
                new_report.created_at.replace(tzinfo=timezone.utc).isoformat()
                if new_report.created_at and new_report.created_at.tzinfo is None
                else (new_report.created_at.astimezone(timezone.utc).isoformat() if new_report.created_at else now_iso)
            )

            return EcgVisionIntakeClientResponse(
                cycle_id=cycle_id,
                status="processed",
                intake_version=new_report.version,
                recording_type=parsed_output.recording_type,
                recording_quality=parsed_output.recording_quality,
                created_at_utc=created_at_iso,
                primary_consilium_concluded=primary_concluded,
            )
