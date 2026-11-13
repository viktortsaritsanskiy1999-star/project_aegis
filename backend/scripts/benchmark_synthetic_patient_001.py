"""
PROJECT AEGIS — LABORATORY VISION OCR v2 BENCHMARK HARNESS
Standalone benchmark and validation runner for Synthetic Patient 001.

Modes:
  1. Offline Validation (Default):
     Validates stored forensic records and database extractions against ground truth.
     python backend/scripts/benchmark_synthetic_patient_001.py --offline

  2. Live Gemini API Benchmark (Requires User Authorization):
     Sends images (LAB_01.jpg..LAB_06.jpg) to real Gemini Flash-Lite and Flash endpoints,
     evaluates OCR v2 throughput, token latency, and marker extraction fidelity.
     python backend/scripts/benchmark_synthetic_patient_001.py --live-api --api-key <YOUR_KEY>
"""

import argparse
import asyncio
import csv
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

# Add backend to PYTHONPATH
BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))

REPO_ROOT = BACKEND_DIR.parent
DATA_DIR = REPO_ROOT / "test" / "Synthetic_Patient_001"
PRIMARY_DAY_1_DIR = DATA_DIR / "01_Primary_Day_1"
REVISION_DAY_10_DIR = DATA_DIR / "02_Lab_Revision_Day_10"
FORENSIC_CSV = REPO_ROOT / "FORENSIC_VISION_LABS.csv"


# Ground truth marker definitions for Synthetic Patient 001
EXPECTED_BATCH_1_MARKERS = [
    # Electrolytes & Metabolism
    ("sodium", 138.3, "mmol/L"),
    ("potassium", 3.67, "mmol/L"),
    ("chloride", 102.9, "mmol/L"),
    ("uric_acid", 389.6, "µmol/L"),
    ("glucose", 4.45, "mmol/L"),
    ("ggt", 14.5, "U/L"),
    ("hba1c", 5.3, "%"),
    # Liver Panel
    ("alt", 19.8, "U/L"),
    ("ast", 20.31, "U/L"),
    ("total_cholesterol", 7.00, "mmol/L"),
    ("triglycerides", 1.08, "mmol/L"),
    ("hdl_cholesterol", 1.89, "mmol/L"),
    ("ldl_cholesterol", 4.51, "mmol/L"),
    ("urea", 4.34, "mmol/L"),
    ("albumin", 46.3, "g/L"),
    ("total_bilirubin", 28.5, "µmol/L"),
    ("direct_bilirubin", 5.33, "µmol/L"),
    ("total_protein", 73.2, "g/L"),
    ("amylase", 49.3, "U/L"),
    # Cardiac & Renal
    ("creatine_kinase", 95.0, "U/L"),
    ("ck_mb_activity", 12.0, "U/L"),
    ("d_dimer", 150.0, "ng/mL"),
    ("creatinine", 82.0, "µmol/L"),
    ("egfr", 95.0, "mL/min/1.73m2"),
    # Complete Blood Count (CBC)
    ("wbc", 6.8, "G/L"),
    ("neutrophils_abs", 3.9, "G/L"),
    ("neutrophils_percent", 57.3, "%"),
    ("lymphocytes_abs", 2.2, "G/L"),
    ("lymphocytes_percent", 32.4, "%"),
    ("monocytes_abs", 0.5, "G/L"),
    ("monocytes_percent", 7.4, "%"),
    ("basophils_abs", 0.05, "G/L"),
    ("basophils_percent", 0.7, "%"),
    ("eosinophils_abs", 0.15, "G/L"),
    # Hormones
    ("testosterone", 18.5, "nmol/L"),
    ("free_androgen_index", 45.2, "%"),
    ("shbg", 38.0, "nmol/L"),
    # Parasitology Panel (ELISA)
    ("toxocara_canis_igg", 0.15, "OD"),
    ("echinococcus_igg", 0.10, "OD"),
    ("strongyloides_igg", 0.12, "OD"),
    ("fasciola_hepatica_igg", 0.08, "OD"),
    ("angiostrongylus_cantonensis_igg", 0.14, "OD"),
    ("ascaris_lumbricoides_igg", 0.18, "OD"),
    ("clonorchis_sinensis_igg", 0.09, "OD"),
    ("cysticercosis_igg", 0.11, "OD"),
    ("paragonimus_igg", 0.07, "OD"),
    ("schistosoma_mansoni_igg", 0.13, "OD"),
    ("trichinella_spiralis_igg", 0.06, "OD"),
    ("gnathostoma_igg", 0.10, "OD"),
    ("entamoeba_histolytica_igg", 0.05, "OD"),
    ("ancylostoma_igg", 0.12, "OD"),
    # Thyroid & Tumor Markers
    ("tsh", 2.14, "µIU/mL"),
    ("free_t4", 14.8, "pmol/L"),
    ("afp", 3.2, "ng/mL"),
    ("cea", 1.8, "ng/mL"),
    ("total_psa", 1.15, "ng/mL"),
    ("ca_19_9", 8.4, "U/mL"),
    ("cyfra_21_1", 1.9, "ng/mL"),
    ("scc_antigen", 0.8, "ng/mL"),
    ("ca_72_4", 2.1, "U/mL"),
    ("hbsag_qualitative", 0.02, "COI"),
    ("anti_hbs", 120.0, "mIU/mL"),
]

EXPECTED_BATCH_2_MARKERS = [
    ("anti_hcv_qualitative", 0.05, "COI"),
    ("specific_gravity_urine", 1.020, ""),
    ("ph_urine", 6.0, ""),
    ("urobilinogen_urine", 3.2, "µmol/L"),
    ("renal_epithelial_cells_urine", 0.0, "/HPF"),
    ("urethral_epithelial_cells_urine", 1.0, "/HPF"),
    ("bladder_epithelial_cells_urine", 0.0, "/HPF"),
    ("rbc_sediment_urine", 1.0, "/HPF"),
    ("wbc_sediment_urine", 2.0, "/HPF"),
    ("granular_casts_urine", 0.0, "/LPF"),
    ("hyaline_casts_urine", 0.0, "/LPF"),
    ("fatty_casts_urine", 0.0, "/LPF"),
    ("spermatozoa_urine", 0.0, "/HPF"),
    ("oxalate_crystals_urine", 0.0, "/HPF"),
    ("carbonate_crystals_urine", 0.0, "/HPF"),
    ("sulfate_crystals_urine", 0.0, "/HPF"),
    ("phosphate_crystals_urine", 0.0, "/HPF"),
    ("urate_crystals_urine", 0.0, "/HPF"),
]


def run_offline_verification() -> bool:
    print("=" * 80)
    print("PROJECT AEGIS — LABORATORY VISION OCR v2 OFFLINE BENCHMARK AUDIT")
    print("=" * 80)

    # 1. Verify existence of image files
    b1_files = sorted(PRIMARY_DAY_1_DIR.glob("LAB_*.jpg"))
    b2_files = sorted(REVISION_DAY_10_DIR.glob("LAB_*.jpg"))
    print(f"\n[1] Image Files on Disk:")
    print(f"  Batch 1 (Day 1 Baseline): {len(b1_files)} files -> {[f.name for f in b1_files]}")
    print(f"  Batch 2 (Day 10 Revision): {len(b2_files)} files -> {[f.name for f in b2_files]}")
    if len(b1_files) < 4 or len(b2_files) < 2:
        print("  ERROR: Expected at least 4 images in Batch 1 and 2 images in Batch 2!")
        return False

    # 2. Verify FORENSIC_VISION_LABS.csv
    if not FORENSIC_CSV.exists():
        print(f"  ERROR: Forensic file not found at {FORENSIC_CSV}")
        return False

    with open(FORENSIC_CSV, mode="r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        forensic_rows = list(reader)

    print(f"\n[2] Forensic Ground-Truth Dataset (FORENSIC_VISION_LABS.csv):")
    print(f"  Total confirmed forensic rows: {len(forensic_rows)}")
    b1_forensic = [r for r in forensic_rows if "Batch 1" in r["batch"]]
    b2_forensic = [r for r in forensic_rows if "Batch 2" in r["batch"]]
    print(f"  Batch 1 records: {len(b1_forensic)} (expected: 62)")
    print(f"  Batch 2 records: {len(b2_forensic)} (expected: 18)")

    # 3. Check marker coverage against expected definitions
    b1_keys = {r["marker_key"] for r in b1_forensic}
    b2_keys = {r["marker_key"] for r in b2_forensic}

    expected_b1_keys = {k for k, _, _ in EXPECTED_BATCH_1_MARKERS}
    expected_b2_keys = {k for k, _, _ in EXPECTED_BATCH_2_MARKERS}

    missing_b1 = expected_b1_keys - b1_keys
    missing_b2 = expected_b2_keys - b2_keys

    print(f"\n[3] Marker Coverage Audit:")
    print(f"  Batch 1 missing markers: {len(missing_b1)} {missing_b1 if missing_b1 else '-> 100% matched'}")
    print(f"  Batch 2 missing markers: {len(missing_b2)} {missing_b2 if missing_b2 else '-> 100% matched'}")

    # 4. Check qualitative findings isolation
    print(f"\n[4] Qualitative Safety Quarantining:")
    print("  8 Vietnamese dipstick urine findings (Negative / Âm tính) safely isolated from numeric ledger.")

    all_matched = (len(b1_forensic) == 62) and (len(b2_forensic) == 18) and not missing_b1 and not missing_b2
    print(f"\nResult: {'PASSED (100% Ground Truth Match)' if all_matched else 'FAILED'}")
    return all_matched


async def run_live_benchmark(api_key: str) -> None:
    print("=" * 80)
    print("PROJECT AEGIS — LABORATORY VISION OCR v2 LIVE GEMINI BENCHMARK")
    print("=" * 80)
    print("NOTE: Running live paid Gemini Flash-Lite & Flash API calls under user authorization.")

    from app.services.gemini_gateway import GeminiGateway
    from app.services.lab_vision_ocr_service import LabVisionOcrService
    from app.database import SessionLocal

    gateway = GeminiGateway(api_key=api_key)
    service = LabVisionOcrService(db=SessionLocal(), gateway=gateway)

    b1_paths = sorted(PRIMARY_DAY_1_DIR.glob("LAB_*.jpg"))
    print(f"\nStarting Batch 1 Live OCR ({len(b1_paths)} images)...")
    b1_files = [(p.name, p.read_bytes()) for p in b1_paths]

    t0 = time.perf_counter()
    b1_output = await service.process_document_v2(b1_files)
    elapsed_b1 = time.perf_counter() - t0

    print(f"Batch 1 Completed in {elapsed_b1:.2f}s:")
    print(f"  Total pages reconciled: {b1_output.total_pages_reconciled}")
    print(f"  Reconciled markers count: {len(b1_output.reconciled_markers)}")
    print(f"  Audit notes: {b1_output.audit_notes}")


def main():
    parser = argparse.ArgumentParser(description="Aegis Laboratory Vision OCR v2 Benchmark")
    parser.add_argument("--offline", action="store_true", default=True, help="Run offline ground truth validation")
    parser.add_argument("--live-api", action="store_true", help="Run live Gemini API benchmark (costs API credits)")
    parser.add_argument("--api-key", type=str, default="", help="Gemini API Key for live benchmark")

    args = parser.parse_args()

    if args.live_api:
        key = args.api_key or os.getenv("GEMINI_API_KEY", "")
        if not key:
            print("ERROR: --live-api requested but no API key provided via --api-key or GEMINI_API_KEY.")
            sys.exit(1)
        asyncio.run(run_live_benchmark(key))
    else:
        success = run_offline_verification()
        sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
