import io
import pytest
import pypdf
from pydantic import ValidationError

from app.config import settings
from app.services.pdf_extraction_service import (
    PdfExtractionService,
    PreparedDocumentPage,
    LabVisionValidationError,
    detect_document_mime_type_from_bytes,
)
from app.services.lab_vision_ocr_service import detect_image_mime_type_from_bytes
from app.services.consilium_prompts import build_vision_ecg_prompt
from app.schemas.consilium import EcgRecordingType


def _create_dummy_jpeg(length: int = 64) -> bytes:
    header = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x01\x00`\x00`\x00\x00"
    return header + b"\x00" * max(0, length - len(header))


def _create_dummy_png(length: int = 64) -> bytes:
    header = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
    return header + b"\x00" * max(0, length - len(header))


def _create_dummy_webp(length: int = 64) -> bytes:
    header = b"RIFF\x30\x00\x00\x00WEBPVP8 "
    return header + b"\x00" * max(0, length - len(header))


def _create_dummy_heic(length: int = 64) -> bytes:
    header = b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00mif1heic"
    return header + b"\x00" * max(0, length - len(header))


def _create_dummy_pdf(page_count: int = 1) -> bytes:
    writer = pypdf.PdfWriter()
    for _ in range(page_count):
        writer.add_blank_page(width=200, height=300)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def _create_encrypted_pdf() -> bytes:
    writer = pypdf.PdfWriter()
    writer.add_blank_page(width=200, height=300)
    writer.encrypt("secure_password_123")
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


# --- Test 1: Single Image Preparation ---

def test_prepare_single_image():
    raw_jpg = _create_dummy_jpeg(512)
    files = [("lab_page.jpg", raw_jpg)]

    pages = PdfExtractionService.prepare_document_pages(files)
    assert len(pages) == 1
    p = pages[0]
    assert p.source_filename == "lab_page.jpg"
    assert p.file_index == 0
    assert p.page_index_in_file == 1
    assert p.global_page_number == 1
    assert p.mime_type == "image/jpeg"
    assert len(p.raw_bytes) == 512
    assert p.byte_size == 512
    assert len(p.sha256_hash) == 64


# --- Test 2: Multiple Images Preparation ---

def test_prepare_multiple_images():
    files = [
        ("page1.jpg", _create_dummy_jpeg(256)),
        ("page2.png", _create_dummy_png(256)),
        ("page3.webp", _create_dummy_webp(256)),
    ]

    pages = PdfExtractionService.prepare_document_pages(files)
    assert len(pages) == 3

    assert [p.global_page_number for p in pages] == [1, 2, 3]
    assert [p.source_filename for p in pages] == ["page1.jpg", "page2.png", "page3.webp"]
    assert [p.file_index for p in pages] == [0, 1, 2]
    assert [p.page_index_in_file for p in pages] == [1, 1, 1]
    assert [p.mime_type for p in pages] == ["image/jpeg", "image/png", "image/webp"]


# --- Test 3: Single-Page PDF Preparation ---

def test_prepare_single_page_pdf():
    pdf_bytes = _create_dummy_pdf(page_count=1)
    files = [("report_single.pdf", pdf_bytes)]

    pages = PdfExtractionService.prepare_document_pages(files)
    assert len(pages) == 1
    p = pages[0]
    assert p.source_filename == "report_single.pdf"
    assert p.file_index == 0
    assert p.page_index_in_file == 1
    assert p.global_page_number == 1
    assert p.mime_type == "application/pdf"
    # Ensure extracted single page is itself a valid 1-page PDF
    reader = pypdf.PdfReader(io.BytesIO(p.raw_bytes))
    assert len(reader.pages) == 1


# --- Test 4: Multi-Page PDF In-Memory Splitting ---

def test_prepare_multipage_pdf():
    # 4-page PDF
    pdf_bytes = _create_dummy_pdf(page_count=4)
    files = [("full_study_4pages.pdf", pdf_bytes)]

    pages = PdfExtractionService.prepare_document_pages(files)
    assert len(pages) == 4

    for idx, p in enumerate(pages, start=1):
        assert p.source_filename == "full_study_4pages.pdf"
        assert p.file_index == 0
        assert p.page_index_in_file == idx
        assert p.global_page_number == idx
        assert p.mime_type == "application/pdf"

        # Each extracted page must be a valid standalone 1-page PDF
        sub_reader = pypdf.PdfReader(io.BytesIO(p.raw_bytes))
        assert len(sub_reader.pages) == 1


# --- Test 5: Mixed Upload (PDF + Images) ---

def test_prepare_mixed_pdf_and_images():
    files = [
        ("cover_page.jpg", _create_dummy_jpeg(128)),
        ("clinical_labs_3p.pdf", _create_dummy_pdf(page_count=3)),
        ("conclusion.png", _create_dummy_png(128)),
    ]

    pages = PdfExtractionService.prepare_document_pages(files)
    assert len(pages) == 5  # 1 image + 3 PDF pages + 1 image

    # Verification of global page numbers in chronological order
    assert [p.global_page_number for p in pages] == [1, 2, 3, 4, 5]

    # File indices
    assert [p.file_index for p in pages] == [0, 1, 1, 1, 2]

    # Page indices within file
    assert [p.page_index_in_file for p in pages] == [1, 1, 2, 3, 1]

    # MIME types
    assert [p.mime_type for p in pages] == [
        "image/jpeg",
        "application/pdf",
        "application/pdf",
        "application/pdf",
        "image/png",
    ]


# --- Test 6: Preservation of Document Reading Order vs Hash Fingerprint ---

def test_reading_order_preservation_and_fingerprint():
    # Create two different pages
    file_a = ("first_doc.jpg", _create_dummy_jpeg(256))
    file_b = ("second_doc.png", _create_dummy_png(512))

    pages_forward = PdfExtractionService.prepare_document_pages([file_a, file_b])
    pages_reverse = PdfExtractionService.prepare_document_pages([file_b, file_a])

    # Reading order must strictly reflect upload sequence (NOT sorted by hash)
    assert pages_forward[0].source_filename == "first_doc.jpg"
    assert pages_forward[1].source_filename == "second_doc.png"
    assert pages_reverse[0].source_filename == "second_doc.png"
    assert pages_reverse[1].source_filename == "first_doc.jpg"

    # However, order-invariant deduplication fingerprint must be IDENTICAL for both sets
    fp_forward = PdfExtractionService.compute_document_fingerprint(pages_forward)
    fp_reverse = PdfExtractionService.compute_document_fingerprint(pages_reverse)
    assert fp_forward == fp_reverse
    assert len(fp_forward) == 64


# --- Test 7: Source File Lineage and Unique IDs ---

def test_source_file_lineage_and_page_ids():
    pdf_bytes = _create_dummy_pdf(page_count=2)
    files = [("order_panel.pdf", pdf_bytes)]

    pages = PdfExtractionService.prepare_document_pages(files)
    assert len(pages) == 2
    p1, p2 = pages[0], pages[1]

    assert p1.page_id != p2.page_id
    assert "doc0_p1" in p1.page_id
    assert "doc0_p2" in p2.page_id
    assert p1.source_filename == "order_panel.pdf"
    assert p2.source_filename == "order_panel.pdf"
    assert p1.sha256_hash != ""
    assert p2.sha256_hash != ""


# --- Test 8: Corrupted, Encrypted, and Empty PDF Rejection ---

def test_corrupted_and_encrypted_pdf_rejection():
    # A. Encrypted / password-protected PDF
    enc_pdf = _create_encrypted_pdf()
    with pytest.raises(LabVisionValidationError) as exc_enc:
        PdfExtractionService.prepare_document_pages([("secure.pdf", enc_pdf)])
    assert "password-protected or encrypted" in str(exc_enc.value).lower()

    # B. Corrupted PDF bytes
    corrupt_pdf = b"%PDF-1.4\ncorrupted_garbage_trailer_not_found_here"
    with pytest.raises(LabVisionValidationError) as exc_corrupt:
        PdfExtractionService.prepare_document_pages([("bad.pdf", corrupt_pdf)])
    assert "corrupted or not a valid pdf" in str(exc_corrupt.value).lower()

    # C. Empty file (0 bytes)
    with pytest.raises(LabVisionValidationError) as exc_empty:
        PdfExtractionService.prepare_document_pages([("empty.pdf", b"")])
    assert "empty (0 bytes)" in str(exc_empty.value).lower()

    # D. Non-medical / unsupported executable or script
    fake_exe = b"MZ\x90\x00\x03\x00\x00\x00\x04\x00\x00\x00\xff\xff\x00\x00"
    with pytest.raises(LabVisionValidationError) as exc_exe:
        PdfExtractionService.prepare_document_pages([("malware.exe", fake_exe)])
    assert "invalid content or unsupported format" in str(exc_exe.value).lower()


# --- Test 9: Resource and Quota Limits Enforcement ---

def test_quota_limits_enforcement():
    # A. Single file exceeding size limit (override to 1000 bytes)
    big_file = _create_dummy_jpeg(2000)
    with pytest.raises(LabVisionValidationError) as exc_single:
        PdfExtractionService.prepare_document_pages(
            files=[("large.jpg", big_file)],
            max_single_file_bytes=1000,
        )
    assert "exceeds maximum allowed single file size" in str(exc_single.value).lower()

    # B. Cumulative bytes exceeding total limit (override to 3000 bytes)
    f1 = ("f1.jpg", _create_dummy_jpeg(1500))
    f2 = ("f2.jpg", _create_dummy_jpeg(2000))
    with pytest.raises(LabVisionValidationError) as exc_tot:
        PdfExtractionService.prepare_document_pages(
            files=[f1, f2],
            max_total_bytes=3000,
        )
    assert "cumulative upload size exceeds" in str(exc_tot.value).lower()

    # C. Exceeding max document page limit (override to 3 pages)
    pdf_5p = _create_dummy_pdf(page_count=5)
    with pytest.raises(LabVisionValidationError) as exc_pages:
        PdfExtractionService.prepare_document_pages(
            files=[("5pages.pdf", pdf_5p)],
            max_document_pages=3,
        )
    assert "exceeds the maximum allowed limit of 3 pages" in str(exc_pages.value).lower()


# --- Test 10: ECG Vision Strictly Untouched ---

def test_ecg_vision_behavior_untouched():
    ecg_prompt = build_vision_ecg_prompt()
    assert "Electrocardiogram Vision Digitization & Recognition Engine" in ecg_prompt
    assert EcgRecordingType.SINGLE_LEAD_SMARTWATCH == "single_lead_smartwatch"
    assert EcgRecordingType.STANDARD_12_LEAD == "standard_12_lead"
    # Ensure settings for ECG vision model remains gemini-3.8-flash
    assert settings.gemini_vision_model == "gemini-3.8-flash"
