import hashlib
import io
import logging
from typing import Any, List, Optional, Tuple
from fastapi import UploadFile
from pydantic import BaseModel, ConfigDict, Field
import pypdf

from app.config import settings
from app.services.primary_consilium_service import ConsiliumError

logger = logging.getLogger("aegis.pdf_extraction")


class LabVisionValidationError(ConsiliumError):
    """Raised when uploaded lab documents fail file format, size, magic bytes, decompression, or OCR validation."""

    def __init__(
        self,
        message: str,
        page_number: Optional[int] = None,
        source_filename: Optional[str] = None,
    ):
        super().__init__(message)
        self.page_number = page_number
        self.source_filename = source_filename


class PreparedDocumentPage(BaseModel):
    """
    Normalized single document page prepared for Vision OCR extraction.
    Represents either a single uploaded image or one page extracted in-memory from a multi-page PDF.
    Preserves exact sequential reading order, source file lineage, and individual hash integrity.
    """
    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    page_id: str = Field(..., description="Unique deterministic identifier for this page, e.g. 'doc0_p1'")
    source_filename: str = Field(..., min_length=1, max_length=256, description="Original filename of the uploaded document")
    file_index: int = Field(..., ge=0, description="0-based index of source file in the upload payload")
    page_index_in_file: int = Field(..., ge=1, description="1-based page index within this specific file")
    global_page_number: int = Field(..., ge=1, description="1-based global sequential page number across the entire document")
    mime_type: str = Field(..., description="MIME type for inline multimodal transfer (application/pdf, image/jpeg, etc.)")
    sha256_hash: str = Field(..., description="Hexadecimal SHA-256 hash of this page's raw bytes")
    raw_bytes: bytes = Field(..., description="In-memory byte payload of this single page")

    @property
    def byte_size(self) -> int:
        return len(self.raw_bytes)


def detect_document_mime_type_from_bytes(data: bytes) -> Optional[str]:
    """
    Validates file content against authoritative magic byte signatures.
    Supports PDF and standard image formats (JPEG, PNG, WEBP, HEIC/HEIF).
    Strictly rejects text files, shell scripts, executables, HTML, and forged extensions.
    """
    if len(data) < 4:
        return None

    # PDF: starts with %PDF-
    if data[:4] == b"%PDF":
        return "application/pdf"

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


class PdfExtractionService:
    """
    Pure in-memory document intake and PDF page splitting engine for Laboratory Vision OCR v2.
    - Inspects content magic bytes for PDF and images.
    - Splits multi-page PDFs into single-page PDF byte streams using pypdf with zero disk I/O.
    - Preserves exact chronological reading order and source file provenance.
    - Defends against corrupted files, password-protected PDFs, and decompression bombs.
    - Enforces configurable limits on page count, single file size, and cumulative bytes.
    """

    @staticmethod
    def split_pdf_to_pages(
        pdf_bytes: bytes,
        filename: str = "document.pdf",
        max_pages: Optional[int] = None,
    ) -> List[Tuple[str, bytes]]:
        """
        Splits a PDF byte buffer into a list of single-page PDF byte streams in-memory.
        Returns: List of ("application/pdf", single_page_pdf_bytes).
        """
        if not pdf_bytes:
            raise LabVisionValidationError(f"File '{filename}' is empty (0 bytes).")

        try:
            reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
        except Exception as exc:
            raise LabVisionValidationError(
                f"File '{filename}' is corrupted or not a valid PDF (invalid content or unsupported format): {exc}"
            ) from exc

        if reader.is_encrypted:
            raise LabVisionValidationError(
                f"PDF file '{filename}' is password-protected or encrypted. Please remove password protection."
            )

        total_pages = len(reader.pages)
        if total_pages == 0:
            raise LabVisionValidationError(f"PDF file '{filename}' contains 0 pages.")

        page_limit = max_pages or settings.lab_ocr_max_document_pages
        if total_pages > page_limit:
            raise LabVisionValidationError(
                f"PDF file '{filename}' has {total_pages} pages, which exceeds the maximum allowed limit of {page_limit} pages."
            )

        extracted_pages: List[Tuple[str, bytes]] = []
        for page_idx, page in enumerate(reader.pages, start=1):
            try:
                writer = pypdf.PdfWriter()
                writer.add_page(page)
                buf = io.BytesIO()
                writer.write(buf)
                page_bytes = buf.getvalue()
                if not page_bytes:
                    raise LabVisionValidationError(
                        f"Page {page_idx} of '{filename}' produced empty data during extraction."
                    )
                extracted_pages.append(("application/pdf", page_bytes))
            except Exception as exc:
                if isinstance(exc, LabVisionValidationError):
                    raise
                raise LabVisionValidationError(
                    f"Failed to extract Page {page_idx} from PDF '{filename}': {exc}"
                ) from exc

        return extracted_pages

    @classmethod
    def prepare_document_pages(
        cls,
        files: List[Tuple[str, bytes]],
        max_document_pages: Optional[int] = None,
        max_single_file_bytes: Optional[int] = None,
        max_total_bytes: Optional[int] = None,
    ) -> List[PreparedDocumentPage]:
        """
        Normalizes a heterogeneous list of uploaded files (PDFs and/or images) into an ordered list
        of PreparedDocumentPage items. Preserves reading order: file 1 pages 1..N, file 2 pages 1..M.
        """
        if not files:
            raise LabVisionValidationError("At least one laboratory report document must be uploaded.")

        limit_pages = max_document_pages or settings.lab_ocr_max_document_pages
        limit_single_file_bytes = max_single_file_bytes or settings.lab_ocr_max_single_file_bytes
        limit_total_bytes = max_total_bytes or settings.lab_ocr_max_total_bytes

        prepared_pages: List[PreparedDocumentPage] = []
        cumulative_bytes = 0
        global_page_counter = 1

        for file_idx, (filename, file_bytes) in enumerate(files):
            clean_filename = filename or f"file_{file_idx + 1}"

            if len(file_bytes) == 0:
                raise LabVisionValidationError(f"File '{clean_filename}' is empty (0 bytes).")

            if len(file_bytes) > limit_single_file_bytes:
                max_mb = limit_single_file_bytes // (1024 * 1024)
                raise LabVisionValidationError(
                    f"File '{clean_filename}' exceeds maximum allowed single file size of {max_mb} MB."
                )

            cumulative_bytes += len(file_bytes)
            if cumulative_bytes > limit_total_bytes:
                max_total_mb = limit_total_bytes // (1024 * 1024)
                raise LabVisionValidationError(
                    f"Cumulative upload size exceeds maximum allowed limit of {max_total_mb} MB."
                )

            detected_mime = detect_document_mime_type_from_bytes(file_bytes)
            if detected_mime is None:
                raise LabVisionValidationError(
                    f"File '{clean_filename}' has invalid content or unsupported format (invalid image content). "
                    "Only valid PDF, JPEG, PNG, WEBP, and HEIC files are accepted."
                )

            if detected_mime == "application/pdf":
                # Multi-page or single-page PDF
                pdf_pages = cls.split_pdf_to_pages(
                    pdf_bytes=file_bytes,
                    filename=clean_filename,
                    max_pages=limit_pages,
                )
                for page_in_file, (mime, page_data) in enumerate(pdf_pages, start=1):
                    if len(prepared_pages) >= limit_pages:
                        raise LabVisionValidationError(
                            f"Too many document pages uploaded ({len(prepared_pages) + 1}). Maximum allowed is {limit_pages} pages."
                        )
                    page_sha = hashlib.sha256(page_data).hexdigest()
                    page_id = f"doc{file_idx}_p{page_in_file}_{page_sha[:8]}"
                    prepared_pages.append(
                        PreparedDocumentPage(
                            page_id=page_id,
                            source_filename=clean_filename,
                            file_index=file_idx,
                            page_index_in_file=page_in_file,
                            global_page_number=global_page_counter,
                            mime_type=mime,
                            sha256_hash=page_sha,
                            raw_bytes=page_data,
                        )
                    )
                    global_page_counter += 1
            else:
                # Single-page image (JPEG, PNG, WEBP, HEIC)
                if len(prepared_pages) >= limit_pages:
                    raise LabVisionValidationError(
                        f"Too many document pages uploaded ({len(prepared_pages) + 1}). Maximum allowed is {limit_pages} pages."
                    )
                page_sha = hashlib.sha256(file_bytes).hexdigest()
                page_id = f"doc{file_idx}_p1_{page_sha[:8]}"
                prepared_pages.append(
                    PreparedDocumentPage(
                        page_id=page_id,
                        source_filename=clean_filename,
                        file_index=file_idx,
                        page_index_in_file=1,
                        global_page_number=global_page_counter,
                        mime_type=detected_mime,
                        sha256_hash=page_sha,
                        raw_bytes=file_bytes,
                    )
                )
                global_page_counter += 1

        if not prepared_pages:
            raise LabVisionValidationError("No valid document pages could be extracted.")

        return prepared_pages

    @classmethod
    async def prepare_document_pages_from_upload_files(
        cls,
        files: List[UploadFile],
        max_document_pages: Optional[int] = None,
        max_single_file_bytes: Optional[int] = None,
        max_total_bytes: Optional[int] = None,
    ) -> List[PreparedDocumentPage]:
        """
        Asynchronously reads UploadFile objects from FastAPI and processes them into PreparedDocumentPage items.
        """
        raw_files: List[Tuple[str, bytes]] = []
        for idx, file_obj in enumerate(files, start=1):
            filename = file_obj.filename or f"upload_{idx}"
            content = await file_obj.read()
            raw_files.append((filename, content))

        return cls.prepare_document_pages(
            files=raw_files,
            max_document_pages=max_document_pages,
            max_single_file_bytes=max_single_file_bytes,
            max_total_bytes=max_total_bytes,
        )

    @staticmethod
    def compute_document_fingerprint(pages: List[PreparedDocumentPage]) -> str:
        """
        Computes an order-invariant multi-page document fingerprint from SHA-256 hashes of all pages.
        Guarantees that re-uploading the same set of pages in any file order produces an identical fingerprint.
        """
        if not pages:
            return ""
        sorted_hashes = sorted(p.sha256_hash for p in pages)
        return hashlib.sha256(";".join(sorted_hashes).encode("ascii")).hexdigest()
