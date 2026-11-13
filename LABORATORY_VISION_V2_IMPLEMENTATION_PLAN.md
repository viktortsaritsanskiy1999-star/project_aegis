# Project Aegis — Laboratory Vision OCR v2 Architecture & Implementation Plan
## Two-Stage Pipeline: Parallel Gemini 3.5 Flash-Lite (Per-Page) + Single Document-Level Gemini 3.8 Flash (Reconciler)

- **Document Version:** 2.1.0
- **Status:** Architecture Updated & Validated through Phase 4
- **Operating Target:** `E:\aegis_health`
- **Component:** Stage 4.1B Laboratory Vision Intake & Ledger Synchronization
- **Authoring:** Aegis Architectural Working Group (Final Architecture)

---

## 1. Executive Summary & Purpose

Project Aegis processes medical laboratory reports using an optimized two-stage cascade architecture:
1. **Stage 1 (Per-Page Optical Extraction):** Each uploaded document page is processed independently by `gemini-3.5-flash-lite` (`GeminiModelRole.FLASH_LITE`) with concurrency managed by `asyncio.Semaphore(6)`. Pages are sliced into sequential batches of maximum 6 pages to preserve optimal pipeline throughput.
2. **Stage 2 (Single Document-Level Reconciliation):** Once all pages of the document have successfully completed Stage 1, **EXACTLY ONE Gemini 3.8 Flash call** (`GeminiModelRole.FLASH`) is executed covering all extracted pages (1 to 15 pages) in a single structured JSON request (no images resent).
3. **Deterministic Python Safeguards:** Python validates the output of Flash, enforcing Zero Silent Dropping (recovering any omitted markers), Anti-Subversion (locking original raw optical values against unauthorized changes), and complete provenance tracking via `source_row_ids`.
4. **Intake Limits:** Strictly 1 to 15 pages per single intake operation. Uploads exceeding 15 pages are rejected at intake.

### Target Architecture (v2) Overview

```
                      +---------------------------------------+
                      | Multi-Format Ingestion (PDF / Images) |
                      |    (Up to 15 pages, max 60 MB)        |
                      +-------------------+-------------------+
                                          |
                                          v
                      +---------------------------------------+
                      |   In-Memory Page Splitter & Hasher    |
                      |  (Zero disk IO; pypdf native memory)  |
                      +-------------------+-------------------+
                                          |
                                          v
                      +---------------------------------------+
                      |     Batch Slicer (Max 6 pages/batch)  |
                      +-------------------+-------------------+
                                          |
        +---------------------------------+---------------------------------+
        |                                                                   |
        | [Batch 1: Pages 1..6]                                             | [Batch 2..k: Pages 7..15]
        v                                                                   v
+-------------------------------+                                   +-------------------------------+
|  Stage 1: Flash-Lite Workers  |                                   |  Stage 1: Flash-Lite Workers  |
|  (asyncio.Semaphore(6))       |                                   |  (asyncio.Semaphore(6))       |
|  gemini-3.5-flash-lite        |                                   |  gemini-3.5-flash-lite        |
|  Page-level tabular OCR       |                                   |  Page-level tabular OCR       |
+---------------+---------------+                                   +---------------+---------------+
                |                                                                   |
        [Raw Page Extractions]                                              [Raw Page Extractions]
                |                                                                   |
                +---------------------------------+---------------------------------+
                                                  |
                                                  v
                      +---------------------------------------+
                      | All Page Extractions (1 to 15 pages)  |
                      | (Structured JSON, in-memory)          |
                      +-------------------+-------------------+
                                          |
                                          v
                      +---------------------------------------+
                      | Stage 2: Single Flash Reconciler      |
                      | gemini-3.8-flash (EXACTLY ONE CALL)   |
                      | Cross-page stitching & study linkages |
                      +-------------------+-------------------+
                                          |
                                          v
                      +---------------------------------------+
                      | Python Deterministic Set Diff         |
                      |  - Zero Silent Marker Dropping        |
                      |  - Anti-Subversion: raw values locked |
                      |  - Full source_row_ids provenance     |
                      +-------------------+-------------------+
                                          |
                                          v
                      +---------------------------------------+
                      | Single Atomic DB Transaction Commit   |
                      |  - preparatory_vision_ocr report      |
                      |  - laboratory_ledger version mutation |
                      +-------------------+-------------------+
```

---

## 2. Model Call Economics and Scaling

| Page Count | Stage 1 Flash-Lite Batches | Stage 1 Flash-Lite Calls | Stage 2 Flash Calls | Total AI Model Calls |
| :---: | :---: | :---: | :---: | :---: |
| **1 page** | 1 batch (1 page) | 1 call | **1 call** | **2 calls** |
| **4 pages** | 1 batch (4 pages) | 4 calls | **1 call** | **5 calls** |
| **6 pages** | 1 batch (6 pages) | 6 calls | **1 call** | **7 calls** |
| **7 pages** | 2 batches (6 + 1 pages) | 7 calls | **1 call** | **8 calls** |
| **10 pages** | 2 batches (6 + 4 pages) | 10 calls | **1 call** | **11 calls** |
| **15 pages** | 3 batches (6 + 6 + 3 pages)| 15 calls | **1 call** | **16 calls** |
| **16+ pages** | *Rejected at intake* | 0 calls | 0 calls | **0 calls (Rejected)** |

---

## 3. Stage 1 Extraction & Four Structural Cues

In Stage 1, `build_lab_vision_ocr_page_prompt()` directs `gemini-3.5-flash-lite` to extract every laboratory indicator with row-level provenance (`row_id`), and capture four structural cues on each page:

1. **date:**
   - `page_sample_date`: Specimen collection date printed on page header (YYYY-MM-DD or null).
   - `page_issue_date`: Report printing/issue date (YYYY-MM-DD or null).
   - `raw_date_str`: Raw unparsed string if date type or format is ambiguous.
   - *Safety:* Never use upload date or today's date as study date.
2. **order_id:**
   - `order_id`: Printed accession number, barcode, order number, or patient medical card.
   - `order_id_type`: Type of identifier ("order_number", "barcode", "medical_card", "accession", or null).
3. **printed_page_num:**
   - Physical page number printed on the form itself (e.g. 1 from "Стр. 1 из 3").
   - `total_printed_pages`: Total pages printed on form if indicated.
   - *Safety:* Distinct from backend-assigned `global_page_number`.
4. **table_state:**
   - `START`: Page begins a new table or explicitly titled section/panel.
   - `CONTINUATION`: Page continues a table split from a previous page without a header.
   - `null`: Indeterminate.
   - `section_name`: Printed laboratory section title (e.g. "Общий анализ крови", "Биохимия").

---

## 4. Stage 2: Single Document-Level Flash Reconciler

Stage 2 executes **exactly ONE call** to Gemini 3.8 Flash (`GeminiModelRole.FLASH`) using `build_lab_vision_ocr_reconciliation_prompt()`:
- Input: JSON payload of all `VisionOcrPageExtraction` items from all pages (no images resent).
- Output: `VisionOcrReconciliationOutput`.
- Responsibilities:
  1. Correlate pages by laboratory, dates, order IDs, and table continuity.
  2. Detect split tables (e.g. CBC starting on page 1, continuing on page 2) and stitch rows.
  3. Separate distinct studies (e.g. tests collected on different dates or under different order IDs) into `study_linkages`.
  4. Track provenance via `source_row_ids` for every indicator.
  5. For unknown dates, set null and note that the date is unestablished.

---

## 5. Python Deterministic Safeguards & Anti-Subversion Engine

`LabVisionOcrService.validate_and_recover_reconciliation()` performs a rigorous audit:
1. **Zero Silent Dropping:** Compares all Stage 1 `row_id`s with `source_row_ids`. Any omitted marker is automatically restored with `requires_user_verification=True` and an audit note.
2. **Anti-Subversion (Raw Value Locking):** If Flash proposed an alteration to `raw_value`, `numeric_value`, `unit`, or `reference_range` without justification, Python reverts to the authoritative Stage 1 optical extraction, preserves the Reconciler's proposal in `uncertainty_note`, and sets `requires_user_verification=True`.
3. **Dangling Reference Pruning:** Validates that all `source_row_ids` point to real Stage 1 rows; flags fabricated rows.
4. **Date Integrity:** Preserves row-specific specimen dates; prevents fabricated dates.

---

## 6. Multi-Format Ingestion Engine (`PdfExtractionService`)

- Accepts JPEG, PNG, WEBP, HEIC, and single/multi-page PDF.
- Uses `pypdf 7.0.0` for pure in-memory splitting without disk I/O.
- Enforces strict limits:
  - Max single file: 10 MB.
  - Max total upload: 60 MB.
  - Max document pages: **15 pages**.
- Preserves chronological reading order in `PreparedDocumentPage.global_page_number`.
- Computes order-invariant multi-page `document_fingerprint` for re-upload idempotency.

---

## 7. Strict ECG Vision Isolation

- `EcgVisionService` continues using `GeminiModelRole.VISION` (`gemini-3.8-flash`).
- `build_vision_ecg_prompt()` and ECG schemas remain 100% untouched.
- All 40 unit and integration tests in `test_ecg_vision.py` pass without modification.

---

## 8. Implementation Phases Status

- [x] **Phase 1:** Schemas (`VisionOcrPageExtraction`, `VisionOcrBatchReconciliationOutput`), config, prompts.
- [x] **Phase 2:** Multi-format ingestion, in-memory PDF splitting (`PdfExtractionService`), quota enforcement.
- [x] **Phase 3:** Stage 1 Parallel Flash-Lite page OCR (`_extract_single_page_flash_lite`, `extract_batch_pages_flash_lite`, semaphore concurrency 6).
- [x] **Phase 4:** Stage 2 Single Flash Reconciler (`reconcile_document_stage2_flash`), 4 structural cues, Python deterministic set diff and anti-subversion validator (`validate_and_recover_reconciliation`).
- [x] **Phase 5:** Laboratory Ledger integration, atomic transaction commit, client intake response.
- [ ] **Phase 6:** End-to-end integration and comparative benchmarking with Synthetic Patient 001.
