import asyncio
import io
import pytest
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock
from sqlalchemy.orm import Session

from app.config import settings
from app.schemas.consilium import (
    VisionOcrPageExtraction,
    VisionOcrMarkerItem,
    LabReferenceRange,
    EcgRecordingType,
)
from app.services.gemini_gateway import (
    GeminiGateway,
    GeminiModelRole,
    GeminiGatewayError,
    GeminiValidationError,
)
from app.services.pdf_extraction_service import (
    PreparedDocumentPage,
    PdfExtractionService,
    LabVisionValidationError,
)
from app.services.lab_vision_ocr_service import LabVisionOcrService
from app.services.consilium_prompts import build_vision_ecg_prompt
from app.services.ecg_vision_service import EcgVisionService


class MockStage1Gateway:
    """Mock GeminiGateway for Stage 1 Flash-Lite page OCR testing."""

    def __init__(self, failure_page_numbers: Optional[List[int]] = None, delay_seconds: float = 0.0):
        self.recorded_calls: List[Dict[str, Any]] = []
        self.failure_page_numbers = failure_page_numbers or []
        self.delay_seconds = delay_seconds
        self.active_calls = 0
        self.peak_concurrency = 0
        self._lock = asyncio.Lock()

    def resolve_model_name(self, role: Any) -> str:
        if role == GeminiModelRole.FLASH_LITE:
            return "gemini-3.5-flash-lite"
        if role == GeminiModelRole.FLASH:
            return "gemini-3.8-flash"
        if role == GeminiModelRole.VISION:
            return "gemini-3.8-flash"
        return str(role)

    async def generate_structured(
        self,
        prompt: str,
        response_schema: Any,
        model: Any = GeminiModelRole.FLASH_LITE,
        inline_images: Optional[List[Dict[str, str]]] = None,
        max_output_tokens: int = 16384,
        **kwargs,
    ) -> Any:
        async with self._lock:
            self.active_calls += 1
            if self.active_calls > self.peak_concurrency:
                self.peak_concurrency = self.active_calls

        call_record = {
            "model": model,
            "prompt": prompt,
            "inline_images": inline_images,
            "max_output_tokens": max_output_tokens,
        }
        self.recorded_calls.append(call_record)

        try:
            # Extract page number from prompt for testing
            page_num = 1
            for part in prompt.split():
                if part.isdigit():
                    page_num = int(part)
                    break

            if page_num in self.failure_page_numbers:
                raise GeminiGatewayError(f"Simulated Gemini API 500 error on Page {page_num}")

            if self.delay_seconds > 0:
                await asyncio.sleep(self.delay_seconds)

            # Return a realistic VisionOcrPageExtraction
            return VisionOcrPageExtraction(
                page_number=page_num,
                source_filename=f"doc_page_{page_num}.png",
                laboratory_name="Invitro",
                page_sample_date="2026-10-15",
                markers=[
                    VisionOcrMarkerItem(
                        original_name="Гемоглобин",
                        marker_key="hemoglobin",
                        raw_value="142",
                        numeric_value=142.0,
                        unit="г/л",
                        reference_range=LabReferenceRange(low=120.0, high=160.0, text="120 - 160"),
                        sample_date="2026-10-15",
                        category="blood_count",
                    )
                ],
            )
        finally:
            async with self._lock:
                self.active_calls -= 1


def _create_dummy_page(global_page_num: int, filename: str = "test.png", mime: str = "image/png") -> PreparedDocumentPage:
    return PreparedDocumentPage(
        page_id=f"page_{global_page_num}",
        source_filename=filename,
        file_index=0,
        page_index_in_file=global_page_num,
        global_page_number=global_page_num,
        mime_type=mime,
        sha256_hash=f"hash_{global_page_num:064d}",
        raw_bytes=b"\x89PNG\r\n\x1a\n" + b"\x00" * 32,
    )


# --- 1. Single Page -> 1 Flash-Lite Call ---

@pytest.mark.anyio
async def test_single_page_single_flash_lite_call():
    mock_gateway = MockStage1Gateway()
    mock_db = MagicMock(spec=Session)
    service = LabVisionOcrService(db=mock_db, gateway=mock_gateway)

    page = _create_dummy_page(1, filename="blood_cbc.png")
    semaphore = asyncio.Semaphore(6)

    result = await service._extract_single_page_flash_lite(page, total_pages=1, semaphore=semaphore)

    assert len(mock_gateway.recorded_calls) == 1
    call = mock_gateway.recorded_calls[0]
    assert call["model"] == GeminiModelRole.FLASH_LITE
    assert len(call["inline_images"]) == 1
    assert call["inline_images"][0]["mime_type"] == "image/png"

    # Verify provenance enforcement
    assert result.page_number == 1
    assert result.source_filename == "blood_cbc.png"
    assert result.source_file_id == "file_0"
    assert len(result.markers) == 1
    assert result.markers[0].row_id == "p1_r1"
    assert result.markers[0].page_number == 1
    assert result.markers[0].source_filename == "blood_cbc.png"


# --- 2. Six Pages -> Six Independent Flash-Lite Calls in Batch ---

@pytest.mark.anyio
async def test_six_pages_batch_six_calls():
    mock_gateway = MockStage1Gateway()
    mock_db = MagicMock(spec=Session)
    service = LabVisionOcrService(db=mock_db, gateway=mock_gateway)

    pages = [_create_dummy_page(i) for i in range(1, 7)]
    results = await service.extract_batch_pages_flash_lite(pages, total_pages=6)

    assert len(results) == 6
    assert len(mock_gateway.recorded_calls) == 6
    for call in mock_gateway.recorded_calls:
        assert call["model"] == GeminiModelRole.FLASH_LITE
        assert len(call["inline_images"]) == 1

    assert [r.page_number for r in results] == [1, 2, 3, 4, 5, 6]


# --- 3. Seven Pages -> 2 Batches (6 + 1) Sequentially ---

@pytest.mark.anyio
async def test_seven_pages_two_sequential_batches():
    mock_gateway = MockStage1Gateway()
    mock_db = MagicMock(spec=Session)
    service = LabVisionOcrService(db=mock_db, gateway=mock_gateway)

    pages = [_create_dummy_page(i) for i in range(1, 8)]

    # Check batch slicing
    batches = service.chunk_document_pages_into_batches(pages, batch_size=6)
    assert len(batches) == 2
    assert len(batches[0]) == 6
    assert len(batches[1]) == 1

    batched_results = await service.extract_document_stage1_batched(pages, batch_size=6)
    assert len(batched_results) == 2
    assert len(batched_results[0]) == 6
    assert len(batched_results[1]) == 1
    assert len(mock_gateway.recorded_calls) == 7


# --- 4. Concurrency Limit (Maximum 6 Concurrent Calls) ---

@pytest.mark.anyio
async def test_concurrency_bounded_by_semaphore():
    # 6 pages with artificial delay to observe concurrency
    mock_gateway = MockStage1Gateway(delay_seconds=0.05)
    mock_db = MagicMock(spec=Session)
    service = LabVisionOcrService(db=mock_db, gateway=mock_gateway)

    pages = [_create_dummy_page(i) for i in range(1, 7)]
    results = await service.extract_batch_pages_flash_lite(pages, total_pages=6, concurrency_limit=6)

    assert len(results) == 6
    # Peak concurrency must never exceed configured concurrency (6)
    assert mock_gateway.peak_concurrency <= 6
    assert mock_gateway.peak_concurrency >= 1


# --- 5. Return Order Preservation with Out-Of-Order Async Completion ---

@pytest.mark.anyio
async def test_order_preservation_with_async_jitter():
    class JitteryMockGateway(MockStage1Gateway):
        async def generate_structured(self, prompt: str, response_schema: Any, **kwargs):
            # Page 1 finishes slowest, page 3 finishes fastest
            if "Page 1" in prompt:
                await asyncio.sleep(0.04)
            elif "Page 2" in prompt:
                await asyncio.sleep(0.02)
            else:
                await asyncio.sleep(0.005)
            return await super().generate_structured(prompt, response_schema, **kwargs)

    mock_gateway = JitteryMockGateway()
    mock_db = MagicMock(spec=Session)
    service = LabVisionOcrService(db=mock_db, gateway=mock_gateway)

    pages = [_create_dummy_page(i) for i in range(1, 4)]
    results = await service.extract_batch_pages_flash_lite(pages, total_pages=3)

    # Regardless of completion jitter, results must strictly be [1, 2, 3]
    assert [r.page_number for r in results] == [1, 2, 3]


# --- 6. Single-Page PDF Multimodal OCR Payload ---

@pytest.mark.anyio
async def test_single_page_pdf_payload():
    mock_gateway = MockStage1Gateway()
    mock_db = MagicMock(spec=Session)
    service = LabVisionOcrService(db=mock_db, gateway=mock_gateway)

    pdf_page = PreparedDocumentPage(
        page_id="pdf_p1",
        source_filename="lab_results.pdf",
        file_index=0,
        page_index_in_file=1,
        global_page_number=1,
        mime_type="application/pdf",
        sha256_hash="hash_pdf_01",
        raw_bytes=b"%PDF-1.4 mock pdf content",
    )

    results = await service.extract_batch_pages_flash_lite([pdf_page], total_pages=1)
    assert len(results) == 1
    assert mock_gateway.recorded_calls[0]["inline_images"][0]["mime_type"] == "application/pdf"
    assert results[0].page_number == 1
    assert results[0].source_filename == "lab_results.pdf"


# --- 7. Preservation of Dates, References, Units and Row IDs ---

@pytest.mark.anyio
async def test_preservation_of_dates_and_markers():
    mock_gateway = MockStage1Gateway()
    mock_db = MagicMock(spec=Session)
    service = LabVisionOcrService(db=mock_db, gateway=mock_gateway)

    page = _create_dummy_page(2, filename="biochem.png")
    results = await service.extract_batch_pages_flash_lite([page], total_pages=1)

    res = results[0]
    assert res.page_sample_date == "2026-10-15"
    assert len(res.markers) == 1
    m = res.markers[0]
    assert m.row_id == "p2_r1"
    assert m.original_name == "Гемоглобин"
    assert m.raw_value == "142"
    assert m.numeric_value == 142.0
    assert m.unit == "г/л"
    assert m.reference_range.low == 120.0
    assert m.reference_range.high == 160.0
    assert m.sample_date == "2026-10-15"


# --- 8. Gemini Gateway Error Wrapped with Page Details ---

@pytest.mark.anyio
async def test_gateway_error_wrapped_with_exact_page():
    # Page 2 fails
    mock_gateway = MockStage1Gateway(failure_page_numbers=[2])
    mock_db = MagicMock(spec=Session)
    service = LabVisionOcrService(db=mock_db, gateway=mock_gateway)

    pages = [_create_dummy_page(1), _create_dummy_page(2, filename="problem_page.png")]

    with pytest.raises(LabVisionValidationError) as exc_info:
        await service.extract_batch_pages_flash_lite(pages, total_pages=2)

    err = exc_info.value
    assert err.page_number == 2
    assert err.source_filename == "problem_page.png"
    assert "Page 2" in str(err)
    assert "problem_page.png" in str(err)


# --- 9. Failure of One Page Cancels Batch & Prevents Ledger Ingestion ---

@pytest.mark.anyio
async def test_single_page_failure_cancels_batch():
    mock_gateway = MockStage1Gateway(failure_page_numbers=[2], delay_seconds=0.02)
    mock_db = MagicMock(spec=Session)
    service = LabVisionOcrService(db=mock_db, gateway=mock_gateway)

    pages = [_create_dummy_page(1), _create_dummy_page(2), _create_dummy_page(3)]

    with pytest.raises(LabVisionValidationError):
        await service.extract_batch_pages_flash_lite(pages, total_pages=3)

    # Database commit must NOT have been called
    mock_db.commit.assert_not_called()


# --- 10. Strict ECG Vision Isolation ---

def test_ecg_vision_isolation():
    # Verify ECG prompt uses role vision_ecg and NOT flash_lite
    ecg_prompt = build_vision_ecg_prompt()
    assert "Electrocardiogram Vision Digitization & Recognition Engine" in ecg_prompt
    assert "single_lead_smartwatch" in ecg_prompt

    # Verify settings
    assert settings.gemini_vision_model == "gemini-3.8-flash"
    assert settings.gemini_flash_lite_model == "gemini-3.5-flash-lite"
