from datetime import datetime, timedelta, timezone
import hashlib
import json
from typing import Any, Dict, List, Optional
import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.models import MedicalCycle, Patient
from app.models.model_report import ModelReport
from app.repositories.cycle_repository import MedicalCycleRepository
from app.repositories.patient_repository import PatientRepository
from app.repositories.report_repository import ModelReportRepository
from app.schemas.consilium import (
    AegisAgentRole,
    LabEventType,
    LabMarkerMutationItem,
    LabReferenceRange,
)
from app.schemas.lab_scheduling import (
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
from app.services.lab_scheduling_service import (
    BACKGROUND_LAB_REVISION_COOLDOWN_HOURS,
    LAB_DOCUMENT_REPORT_TYPE,
    LAB_REVISION_DEBOUNCE_MINUTES,
    LAB_REVISION_PENDING_TASK_REPORT_TYPE,
    LabSchedulingService,
)


class MockClock:
    def __init__(self, start: datetime):
        self._current = start

    def now(self) -> datetime:
        return self._current

    def advance(self, minutes: int = 0, hours: int = 0, seconds: int = 0) -> datetime:
        self._current += timedelta(minutes=minutes, hours=hours, seconds=seconds)
        return self._current

    def set(self, dt: datetime) -> None:
        self._current = dt


def _gemini_structured_envelope(payload: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "candidates": [
            {
                "content": {
                    "parts": [{"text": json.dumps(payload)}],
                    "role": "model",
                },
                "finishReason": "STOP",
            }
        ]
    }


def _make_sample_ocr_output(markers: List[Dict[str, Any]], sample_date: str = "2026-11-06") -> Dict[str, Any]:
    return {
        "document_metadata": {
            "order_id": "ORDER-101",
            "laboratory_name": "InVitro Diagnostics",
            "sample_date": sample_date,
            "page_count": 1,
            "notes": "Legible laboratory report",
        },
        "markers": markers,
    }


def _make_gemini_gateway(handler) -> GeminiGateway:
    transport = httpx.MockTransport(handler)
    return GeminiGateway(api_key="mock-key", transport=transport)


@pytest.fixture
def db_session() -> Session:
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def _seed_patient_and_cycle(
    session: Session,
    patient_id: str,
    cycle_id: str,
    anchor_dt: datetime,
) -> None:
    p_repo = PatientRepository(session)
    if not p_repo.get(patient_id):
        p_repo.create(patient_id=patient_id, status="active")

    c_repo = MedicalCycleRepository(session)
    if not c_repo.get(cycle_id):
        c_repo.create(
            cycle_id=cycle_id,
            patient_id=patient_id,
            status="active",
            started_at=anchor_dt,
        )


# ==============================================================================
# TEST 1: CONFIRM AND CANCEL MANUAL EDIT
# ==============================================================================

@pytest.mark.anyio
async def test_01_confirm_and_cancel_manual_edit(db_session: Session):
    """
    Scenario 1: Confirm vs Cancel of manual edit.
    - Cancel: No mutations sent to server, ledger remains version 0, no pending task.
    - Confirm: Mutation sent, ledger bumps to v1, debounce timer established.
    """
    t0 = datetime(2026, 11, 6, 8, 0, tzinfo=timezone.utc)
    clock = MockClock(t0)
    cycle_id = "cyc_s6a_01"
    patient_id = "pat_s6a_01"
    _seed_patient_and_cycle(db_session, patient_id, cycle_id, t0)

    sched_svc = LabSchedulingService(db=db_session, now_provider=clock.now)

    # Cancel case: User cancels dialog, nothing is submitted to process_manual_edit
    assert sched_svc.get_pending_task(cycle_id) is None
    assert sched_svc.lab_revision_service.get_lab_ledger_state(cycle_id).ledger_version == 0

    # Confirm case: User clicks "Apply changes", mutations submitted
    resp = sched_svc.process_manual_edit(
        cycle_id=cycle_id,
        patient_id=patient_id,
        mutations=[
            LabMarkerMutationItem(
                marker_key="glucose",
                display_name="Glucose",
                value=5.4,
                unit="mmol/L",
                sample_date="2026-11-06",
            )
        ],
    )
    assert resp.change_type == LabChangeType.MEANINGFUL_CHANGE
    assert resp.ledger_version == 1

    task = sched_svc.get_pending_task(cycle_id)
    assert task is not None
    assert task.status == "pending"
    assert task.debounce_until_utc == (t0 + timedelta(minutes=15)).isoformat()


# ==============================================================================
# TEST 2: CONFIRM AND CANCEL DOCUMENT UPLOAD
# ==============================================================================

@pytest.mark.anyio
async def test_02_confirm_and_cancel_document_upload(db_session: Session):
    """
    Scenario 2: Confirm vs Cancel of document upload.
    - Cancel: Upload is aborted by user, no document record created, no OCR run.
    - Confirm: Document record saved, OCR executed, changes detected.
    """
    t0 = datetime(2026, 11, 6, 8, 0, tzinfo=timezone.utc)
    clock = MockClock(t0)
    cycle_id = "cyc_s6a_02"
    patient_id = "pat_s6a_02"
    _seed_patient_and_cycle(db_session, patient_id, cycle_id, t0)

    def gemini_handler(req: httpx.Request):
        return httpx.Response(200, json=_gemini_structured_envelope(_make_sample_ocr_output([
            {"marker_key": "alt", "original_name": "ALT", "raw_value": "28", "numeric_value": 28.0, "unit": "U/L", "sample_date": "2026-11-06", "requires_user_verification": False}
        ])))

    gw = _make_gemini_gateway(gemini_handler)
    sched_svc = LabSchedulingService(db=db_session, gateway=gw, now_provider=clock.now)

    # Cancel case: User clicks Cancel, no documents uploaded
    assert len(sched_svc.get_document_records(cycle_id)) == 0

    # Confirm case: User clicks Upload
    dummy_bytes = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x01\x00\x60\x00\x60\x00\x00fake_jpeg_content_02"
    doc_rec = sched_svc.save_uploaded_document(
        cycle_id=cycle_id,
        patient_id=patient_id,
        filename="blood_test.jpg",
        content_bytes=dummy_bytes,
        mime_type="image/jpeg",
    )
    assert doc_rec.status == LabDocumentStatus.UPLOADED
    assert len(sched_svc.get_document_records(cycle_id)) == 1

    change_type, updated_doc = await sched_svc.process_document_ocr(
        cycle_id=cycle_id,
        patient_id=patient_id,
        doc_record=doc_rec,
    )
    assert change_type == LabChangeType.MEANINGFUL_CHANGE
    assert updated_doc.status == LabDocumentStatus.PROCESSED
    assert updated_doc.extracted_markers_count == 1


# ==============================================================================
# TEST 3: NO_CHANGE ON 4 -> 4
# ==============================================================================

@pytest.mark.anyio
async def test_03_no_change_value_four_to_four(db_session: Session):
    """
    Scenario 3: Change from 4 -> 4 is classified as NO_CHANGE.
    Debounce timer is NOT started or reset.
    """
    t0 = datetime(2026, 11, 6, 8, 0, tzinfo=timezone.utc)
    clock = MockClock(t0)
    cycle_id = "cyc_s6a_03"
    patient_id = "pat_s6a_03"
    _seed_patient_and_cycle(db_session, patient_id, cycle_id, t0)

    sched_svc = LabSchedulingService(db=db_session, now_provider=clock.now)

    # Initial baseline mutation: RBC = 4.0
    sched_svc.lab_revision_service.upsert_laboratory_markers(
        cycle_id=cycle_id,
        mutations=[
            LabMarkerMutationItem(
                marker_key="rbc",
                display_name="Red Blood Cells",
                value=4.0,
                unit="10^12/L",
                sample_date="2026-11-06",
            )
        ],
    )

    # User submits identical value 4.0 -> 4.0
    resp = sched_svc.process_manual_edit(
        cycle_id=cycle_id,
        patient_id=patient_id,
        mutations=[
            LabMarkerMutationItem(
                marker_key="rbc",
                display_name="Red Blood Cells",
                value=4.0,
                unit="10^12/L",
                sample_date="2026-11-06",
            )
        ],
    )
    assert resp.change_type == LabChangeType.NO_CHANGE
    assert resp.mutations_applied == 0
    # No pending task created
    assert sched_svc.get_pending_task(cycle_id) is None


# ==============================================================================
# TEST 4: MEANINGFUL_CHANGE ON 4 -> 4.5
# ==============================================================================

@pytest.mark.anyio
async def test_04_meaningful_change_value_four_to_four_point_five(db_session: Session):
    """
    Scenario 4: Change from 4 -> 4.5 is classified as MEANINGFUL_CHANGE.
    Debounce timer is set to now + 15 minutes.
    """
    t0 = datetime(2026, 11, 6, 8, 0, tzinfo=timezone.utc)
    clock = MockClock(t0)
    cycle_id = "cyc_s6a_04"
    patient_id = "pat_s6a_04"
    _seed_patient_and_cycle(db_session, patient_id, cycle_id, t0)

    sched_svc = LabSchedulingService(db=db_session, now_provider=clock.now)

    # Initial baseline mutation: RBC = 4.0
    sched_svc.lab_revision_service.upsert_laboratory_markers(
        cycle_id=cycle_id,
        mutations=[
            LabMarkerMutationItem(
                marker_key="rbc",
                display_name="Red Blood Cells",
                value=4.0,
                unit="10^12/L",
                sample_date="2026-11-06",
            )
        ],
    )

    # User changes 4.0 -> 4.5
    resp = sched_svc.process_manual_edit(
        cycle_id=cycle_id,
        patient_id=patient_id,
        mutations=[
            LabMarkerMutationItem(
                marker_key="rbc",
                display_name="Red Blood Cells",
                value=4.5,
                unit="10^12/L",
                sample_date="2026-11-06",
            )
        ],
    )
    assert resp.change_type == LabChangeType.MEANINGFUL_CHANGE
    assert resp.mutations_applied == 1
    task = sched_svc.get_pending_task(cycle_id)
    assert task is not None
    assert task.debounce_until_utc == (t0 + timedelta(minutes=15)).isoformat()


# ==============================================================================
# TEST 5: EXACT DUPLICATE PHOTO SKIPS OCR
# ==============================================================================

@pytest.mark.anyio
async def test_05_exact_duplicate_photo_skips_ocr(db_session: Session):
    """
    Scenario 5: Byte-for-byte exact duplicate photo:
    - First upload executes OCR.
    - Second upload with identical bytes detects DUPLICATE_FILE by SHA-256 hash.
    - Zero new Gemini Vision calls made; debounce not reset.
    """
    t0 = datetime(2026, 11, 6, 8, 0, tzinfo=timezone.utc)
    clock = MockClock(t0)
    cycle_id = "cyc_s6a_05"
    patient_id = "pat_s6a_05"
    _seed_patient_and_cycle(db_session, patient_id, cycle_id, t0)

    call_count = 0

    def gemini_handler(req: httpx.Request):
        nonlocal call_count
        call_count += 1
        return httpx.Response(200, json=_gemini_structured_envelope(_make_sample_ocr_output([
            {"marker_key": "ast", "original_name": "AST", "raw_value": "31", "numeric_value": 31.0, "unit": "U/L", "sample_date": "2026-11-06", "requires_user_verification": False}
        ])))

    gw = _make_gemini_gateway(gemini_handler)
    sched_svc = LabSchedulingService(db=db_session, gateway=gw, now_provider=clock.now)

    file_bytes = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x01\x00\x60\x00\x60\x00\x00unique_photo_05_bytes"

    # First upload
    doc1 = sched_svc.save_uploaded_document(cycle_id, patient_id, "doc.jpg", file_bytes, "image/jpeg")
    assert doc1.is_duplicate is False
    c_type1, _ = await sched_svc.process_document_ocr(cycle_id, patient_id, doc1)
    assert c_type1 == LabChangeType.MEANINGFUL_CHANGE
    assert call_count == 1

    # Second upload with identical bytes
    clock.advance(minutes=5)
    doc2 = sched_svc.save_uploaded_document(cycle_id, patient_id, "doc_again.jpg", file_bytes, "image/jpeg")
    assert doc2.is_duplicate is True
    assert doc2.status == LabDocumentStatus.DUPLICATE

    c_type2, _ = await sched_svc.process_document_ocr(cycle_id, patient_id, doc2)
    assert c_type2 == LabChangeType.DUPLICATE_FILE
    assert call_count == 1  # No extra Gemini Vision call!


# ==============================================================================
# TEST 6: DIFFERENT PHOTO WITH SAME MEDICAL DATA
# ==============================================================================

@pytest.mark.anyio
async def test_06_different_photo_with_same_medical_data(db_session: Session):
    """
    Scenario 6: Different photo (different SHA-256) but exact same clinical data and date.
    - OCR runs on new image.
    - Content comparison matches existing active study -> NO_CHANGE.
    - No new revision scheduled; debounce not reset.
    """
    t0 = datetime(2026, 11, 6, 8, 0, tzinfo=timezone.utc)
    clock = MockClock(t0)
    cycle_id = "cyc_s6a_06"
    patient_id = "pat_s6a_06"
    _seed_patient_and_cycle(db_session, patient_id, cycle_id, t0)

    def gemini_handler(req: httpx.Request):
        return httpx.Response(200, json=_gemini_structured_envelope(_make_sample_ocr_output([
            {"marker_key": "wbc", "original_name": "White Blood Cells", "raw_value": "6.5", "numeric_value": 6.5, "unit": "10^9/L", "sample_date": "2026-11-06", "requires_user_verification": False},
            {"marker_key": "plt", "original_name": "Platelets", "raw_value": "240", "numeric_value": 240.0, "unit": "10^9/L", "sample_date": "2026-11-06", "requires_user_verification": False},
        ])))

    gw = _make_gemini_gateway(gemini_handler)
    sched_svc = LabSchedulingService(db=db_session, gateway=gw, now_provider=clock.now)

    # First photo
    bytes_a = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x01\x00\x60\x00\x60\x00\x00photo_a_bytes"
    doc_a = sched_svc.save_uploaded_document(cycle_id, patient_id, "photo_a.jpg", bytes_a, "image/jpeg")
    c_type_a, _ = await sched_svc.process_document_ocr(cycle_id, patient_id, doc_a)
    assert c_type_a == LabChangeType.MEANINGFUL_CHANGE
    task_a = sched_svc.get_pending_task(cycle_id)
    debounce_a = task_a.debounce_until_utc

    # Second photo: different bytes, but returns identical WBC 6.5 and PLT 240 on 2026-11-06
    clock.advance(minutes=4)
    bytes_b = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x01\x00\x60\x00\x60\x00\x00photo_b_different_angle"
    doc_b = sched_svc.save_uploaded_document(cycle_id, patient_id, "photo_b.jpg", bytes_b, "image/jpeg")
    c_type_b, _ = await sched_svc.process_document_ocr(cycle_id, patient_id, doc_b)
    assert c_type_b == LabChangeType.NO_CHANGE

    task_b = sched_svc.get_pending_task(cycle_id)
    # Debounce timer was NOT reset by the identical clinical content
    assert task_b.debounce_until_utc == debounce_a


# ==============================================================================
# TEST 7: NEW MARKER IN DOCUMENT
# ==============================================================================

@pytest.mark.anyio
async def test_07_new_marker_in_document(db_session: Session):
    """
    Scenario 7: Document introduces a brand new biomarker not previously in ledger.
    - Classified as MEANINGFUL_CHANGE.
    - Pending task created/updated with debounce.
    """
    t0 = datetime(2026, 11, 6, 8, 0, tzinfo=timezone.utc)
    clock = MockClock(t0)
    cycle_id = "cyc_s6a_07"
    patient_id = "pat_s6a_07"
    _seed_patient_and_cycle(db_session, patient_id, cycle_id, t0)

    def gemini_handler(req: httpx.Request):
        return httpx.Response(200, json=_gemini_structured_envelope(_make_sample_ocr_output([
            {"marker_key": "crp", "original_name": "C-Reactive Protein", "raw_value": "1.2", "numeric_value": 1.2, "unit": "mg/L", "sample_date": "2026-11-06", "requires_user_verification": False}
        ])))

    gw = _make_gemini_gateway(gemini_handler)
    sched_svc = LabSchedulingService(db=db_session, gateway=gw, now_provider=clock.now)

    doc = sched_svc.save_uploaded_document(
        cycle_id, patient_id, "crp_test.jpg", b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x01\x00\x60\x00\x60\x00\x00crp_bytes", "image/jpeg"
    )
    c_type, _ = await sched_svc.process_document_ocr(cycle_id, patient_id, doc)
    assert c_type == LabChangeType.MEANINGFUL_CHANGE

    task = sched_svc.get_pending_task(cycle_id)
    assert task is not None
    assert task.accumulated_changes_count >= 1


# ==============================================================================
# TEST 8: NEW ANALYSIS WITH DIFFERENT DATE
# ==============================================================================

@pytest.mark.anyio
async def test_08_new_analysis_with_different_date(db_session: Session):
    """
    Scenario 8: New study with different sample_date, even if numerical value matches prior study.
    - Longitudinal new study is a MEANINGFUL_CHANGE.
    - Debounce timer is set.
    """
    t0 = datetime(2026, 11, 6, 8, 0, tzinfo=timezone.utc)
    clock = MockClock(t0)
    cycle_id = "cyc_s6a_08"
    patient_id = "pat_s6a_08"
    _seed_patient_and_cycle(db_session, patient_id, cycle_id, t0)

    sched_svc = LabSchedulingService(db=db_session, now_provider=clock.now)

    # Initial baseline study: Creatinine = 80 on 2026-10-01
    sched_svc.lab_revision_service.upsert_laboratory_markers(
        cycle_id=cycle_id,
        mutations=[
            LabMarkerMutationItem(
                marker_key="creatinine",
                display_name="Creatinine",
                value=80.0,
                unit="umol/L",
                sample_date="2026-10-01",
            )
        ],
    )

    # New study with identical value 80.0 but NEW date 2026-11-06
    resp = sched_svc.process_manual_edit(
        cycle_id=cycle_id,
        patient_id=patient_id,
        mutations=[
            LabMarkerMutationItem(
                marker_key="creatinine",
                display_name="Creatinine",
                value=80.0,
                unit="umol/L",
                sample_date="2026-11-06",
            )
        ],
    )
    assert resp.change_type == LabChangeType.MEANINGFUL_CHANGE
    task = sched_svc.get_pending_task(cycle_id)
    assert task is not None
    assert task.debounce_until_utc == (t0 + timedelta(minutes=15)).isoformat()


# ==============================================================================
# TEST 9: MULTIPLE CHANGES WITHIN 15 MINUTES RESETS DEBOUNCE
# ==============================================================================

@pytest.mark.anyio
async def test_09_multiple_changes_within_15_minutes_resets_debounce(db_session: Session):
    """
    Scenario 9: Multiple meaningful changes arriving sequentially:
    - Change at t = 0 min sets debounce to t0 + 15 min.
    - Change at t = 5 min resets debounce to t0 + 20 min.
    - Change at t = 12 min resets debounce to t0 + 27 min.
    """
    t0 = datetime(2026, 11, 6, 8, 0, tzinfo=timezone.utc)
    clock = MockClock(t0)
    cycle_id = "cyc_s6a_09"
    patient_id = "pat_s6a_09"
    _seed_patient_and_cycle(db_session, patient_id, cycle_id, t0)

    sched_svc = LabSchedulingService(db=db_session, now_provider=clock.now)

    # Change 1 at t=0
    sched_svc.process_manual_edit(
        cycle_id, patient_id,
        [LabMarkerMutationItem(marker_key="hgb", display_name="Hemoglobin", value=135, unit="g/L", sample_date="2026-11-06")]
    )
    task1 = sched_svc.get_pending_task(cycle_id)
    assert task1.debounce_until_utc == (t0 + timedelta(minutes=15)).isoformat()

    # Change 2 at t=5 min
    clock.advance(minutes=5)
    sched_svc.process_manual_edit(
        cycle_id, patient_id,
        [LabMarkerMutationItem(marker_key="cholesterol", display_name="Total Cholesterol", value=5.2, unit="mmol/L", sample_date="2026-11-06")]
    )
    task2 = sched_svc.get_pending_task(cycle_id)
    assert task2.debounce_until_utc == (t0 + timedelta(minutes=20)).isoformat()
    assert task2.accumulated_changes_count == 2

    # Change 3 at t=12 min
    clock.advance(minutes=7)
    sched_svc.process_manual_edit(
        cycle_id, patient_id,
        [LabMarkerMutationItem(marker_key="tg", display_name="Triglycerides", value=1.7, unit="mmol/L", sample_date="2026-11-06")]
    )
    task3 = sched_svc.get_pending_task(cycle_id)
    assert task3.debounce_until_utc == (t0 + timedelta(minutes=27)).isoformat()
    assert task3.accumulated_changes_count == 3


# ==============================================================================
# TEST 10: DUPLICATE DOES NOT RESET DEBOUNCE
# ==============================================================================

@pytest.mark.anyio
async def test_10_duplicate_does_not_reset_debounce(db_session: Session):
    """
    Scenario 10: Duplicate change arrives during debounce:
    - Meaningful change at t = 0 min sets debounce to t0 + 15 min.
    - Duplicate arrives at t = 7 min.
    - Debounce timer remains t0 + 15 min (not reset).
    """
    t0 = datetime(2026, 11, 6, 8, 0, tzinfo=timezone.utc)
    clock = MockClock(t0)
    cycle_id = "cyc_s6a_10"
    patient_id = "pat_s6a_10"
    _seed_patient_and_cycle(db_session, patient_id, cycle_id, t0)

    sched_svc = LabSchedulingService(db=db_session, now_provider=clock.now)

    # Initial change at t=0
    sched_svc.process_manual_edit(
        cycle_id, patient_id,
        [LabMarkerMutationItem(marker_key="iron", display_name="Serum Iron", value=18, unit="umol/L", sample_date="2026-11-06")]
    )
    expected_debounce = (t0 + timedelta(minutes=15)).isoformat()
    assert sched_svc.get_pending_task(cycle_id).debounce_until_utc == expected_debounce

    # Duplicate submission at t=7 min (iron = 18)
    clock.advance(minutes=7)
    resp = sched_svc.process_manual_edit(
        cycle_id, patient_id,
        [LabMarkerMutationItem(marker_key="iron", display_name="Serum Iron", value=18, unit="umol/L", sample_date="2026-11-06")]
    )
    assert resp.change_type == LabChangeType.NO_CHANGE
    task = sched_svc.get_pending_task(cycle_id)
    assert task.debounce_until_utc == expected_debounce


# ==============================================================================
# TEST 11: FIRST LAB REVISION WITHOUT PRIOR COOLDOWN
# ==============================================================================

@pytest.mark.anyio
async def test_11_first_lab_revision_without_prior_cooldown(db_session: Session):
    """
    Scenario 11: First Lab Revision in a cycle without previous completed revision.
    - No 72-hour cooldown applies.
    - Effective ready time equals debounce_until (t0 + 15 min).
    """
    t0 = datetime(2026, 11, 6, 8, 0, tzinfo=timezone.utc)
    clock = MockClock(t0)
    cycle_id = "cyc_s6a_11"
    patient_id = "pat_s6a_11"
    _seed_patient_and_cycle(db_session, patient_id, cycle_id, t0)

    sched_svc = LabSchedulingService(db=db_session, now_provider=clock.now)

    sched_svc.process_manual_edit(
        cycle_id, patient_id,
        [LabMarkerMutationItem(marker_key="ferritin", display_name="Ferritin", value=55, unit="ng/mL", sample_date="2026-11-06")]
    )
    task = sched_svc.get_pending_task(cycle_id)
    assert task.cooldown_until_utc is None
    assert task.effective_ready_at_utc == task.debounce_until_utc

    # At t0 + 10 min: not ready
    clock.set(t0 + timedelta(minutes=10))
    is_ready, _ = sched_svc.evaluate_task_readiness(cycle_id)
    assert is_ready is False

    # At t0 + 15 min: ready!
    clock.set(t0 + timedelta(minutes=15))
    is_ready, _ = sched_svc.evaluate_task_readiness(cycle_id)
    assert is_ready is True


# ==============================================================================
# TEST 12: NEW DATA DURING 72-HOUR COOLDOWN
# ==============================================================================

@pytest.mark.anyio
async def test_12_new_data_during_72h_cooldown(db_session: Session):
    """
    Scenario 12: Prior Lab Revision completed at t_rev.
    - 72-hour cooldown active until t_rev + 72 hours.
    - New data arrives at t_rev + 2 hours:
      * Saved immediately without delay.
      * Debounce is set to (t_rev + 2h) + 15 min.
      * Effective ready time is max(debounce, cooldown) = t_rev + 72 hours.
    """
    t_rev = datetime(2026, 11, 4, 12, 0, tzinfo=timezone.utc)
    t0 = t_rev + timedelta(hours=2)
    clock = MockClock(t0)
    cycle_id = "cyc_s6a_12"
    patient_id = "pat_s6a_12"
    _seed_patient_and_cycle(db_session, patient_id, cycle_id, t_rev)

    # Record completed previous revision log in model_reports
    report_repo = ModelReportRepository(db_session)
    report_repo.create_next_version(
        cycle_id=cycle_id,
        model="gemini-pro",
        report_type=LAB_REVISION_EXECUTION_LOG_REPORT_TYPE,
        privacy_level=PrivacyLevel.SERVER_ONLY,
        content={"completed_at_utc": t_rev.isoformat(), "revision_number": 1},
        created_at=t_rev,
        commit=True,
    )

    sched_svc = LabSchedulingService(db=db_session, now_provider=clock.now)

    # Change arrives during cooldown at t_rev + 2h
    sched_svc.process_manual_edit(
        cycle_id, patient_id,
        [LabMarkerMutationItem(marker_key="tsh", display_name="TSH", value=2.1, unit="uIU/mL", sample_date="2026-11-06")]
    )
    task = sched_svc.get_pending_task(cycle_id)
    assert task is not None
    expected_cooldown_end = t_rev + timedelta(hours=BACKGROUND_LAB_REVISION_COOLDOWN_HOURS)
    assert task.cooldown_until_utc == expected_cooldown_end.isoformat()
    assert task.effective_ready_at_utc == expected_cooldown_end.isoformat()

    # At t_rev + 2h 20m (after debounce, but during 72h cooldown): NOT ready
    clock.set(t_rev + timedelta(hours=2, minutes=20))
    is_ready, reason = sched_svc.evaluate_task_readiness(cycle_id)
    assert is_ready is False
    assert "cooldown" in reason.lower()

    # At t_rev + 72h: ready!
    clock.set(expected_cooldown_end)
    is_ready, reason = sched_svc.evaluate_task_readiness(cycle_id)
    assert is_ready is True


# ==============================================================================
# TEST 13: QUEUE RESTORED AFTER SERVER RESTART
# ==============================================================================

@pytest.mark.anyio
async def test_13_queue_restored_after_server_restart(db_session: Session):
    """
    Scenario 13: Persistent queue survives server restart.
    - Sched service 1 schedules changes.
    - Sched service 2 instantiated afresh reads exact pending state.
    """
    t0 = datetime(2026, 11, 6, 8, 0, tzinfo=timezone.utc)
    clock = MockClock(t0)
    cycle_id = "cyc_s6a_13"
    patient_id = "pat_s6a_13"
    _seed_patient_and_cycle(db_session, patient_id, cycle_id, t0)

    sched_svc1 = LabSchedulingService(db=db_session, now_provider=clock.now)
    sched_svc1.process_manual_edit(
        cycle_id, patient_id,
        [LabMarkerMutationItem(marker_key="ast", display_name="AST", value=35, unit="U/L", sample_date="2026-11-06")]
    )

    # Server restart: new instance with clean memory
    sched_svc2 = LabSchedulingService(db=db_session, now_provider=clock.now)
    restored_task = sched_svc2.get_pending_task(cycle_id)
    assert restored_task is not None
    assert restored_task.accumulated_changes_count == 1
    assert restored_task.debounce_until_utc == (t0 + timedelta(minutes=15)).isoformat()


# ==============================================================================
# TEST 14: CONCURRENT EVENTS WITHOUT DUPLICATE TASK
# ==============================================================================

@pytest.mark.anyio
async def test_14_concurrent_events_without_duplicate_task(db_session: Session):
    """
    Scenario 14: Multiple concurrent updates result in exactly one pending task.
    """
    t0 = datetime(2026, 11, 6, 8, 0, tzinfo=timezone.utc)
    clock = MockClock(t0)
    cycle_id = "cyc_s6a_14"
    patient_id = "pat_s6a_14"
    _seed_patient_and_cycle(db_session, patient_id, cycle_id, t0)

    sched_svc = LabSchedulingService(db=db_session, now_provider=clock.now)

    # Update A
    sched_svc.process_manual_edit(
        cycle_id, patient_id,
        [LabMarkerMutationItem(marker_key="k", display_name="Potassium", value=4.2, unit="mmol/L", sample_date="2026-11-06")]
    )
    # Update B
    sched_svc.process_manual_edit(
        cycle_id, patient_id,
        [LabMarkerMutationItem(marker_key="na", display_name="Sodium", value=140, unit="mmol/L", sample_date="2026-11-06")]
    )

    # Exactly one latest task record exists
    task = sched_svc.get_pending_task(cycle_id)
    assert task is not None
    assert task.task_id == f"lab_rev_task_{cycle_id}"
    assert task.accumulated_changes_count == 2


# ==============================================================================
# TEST 15: OCR FAILURE PRESERVES ORIGINAL DOCUMENT
# ==============================================================================

@pytest.mark.anyio
async def test_15_ocr_failure_preserves_original_document(db_session: Session):
    """
    Scenario 15: When OCR fails (e.g. gateway error), original document bytes
    remain permanently preserved with status='error'.
    """
    t0 = datetime(2026, 11, 6, 8, 0, tzinfo=timezone.utc)
    clock = MockClock(t0)
    cycle_id = "cyc_s6a_15"
    patient_id = "pat_s6a_15"
    _seed_patient_and_cycle(db_session, patient_id, cycle_id, t0)

    def failing_gemini_handler(req: httpx.Request):
        return httpx.Response(502, json={"error": "Model overloaded"})

    gw = _make_gemini_gateway(failing_gemini_handler)
    sched_svc = LabSchedulingService(db=db_session, gateway=gw, now_provider=clock.now)

    raw_bytes = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x01\x00\x60\x00\x60\x00\x00precious_original_file"
    doc_rec = sched_svc.save_uploaded_document(cycle_id, patient_id, "scan.jpg", raw_bytes, "image/jpeg")

    # Run OCR which fails
    c_type, updated_doc = await sched_svc.process_document_ocr(cycle_id, patient_id, doc_rec)
    assert c_type == LabChangeType.NO_CHANGE
    assert updated_doc.status == LabDocumentStatus.ERROR
    assert updated_doc.error_message is not None

    # Verify original document bytes are STILL intact on server
    persisted_docs = sched_svc.get_document_records(cycle_id)
    assert len(persisted_docs) == 1
    p_doc = persisted_docs[0]
    assert p_doc.raw_data_base64 is not None
    assert p_doc.file_sha256 == hashlib.sha256(raw_bytes).hexdigest()

    # Readiness is blocked while failed/incomplete OCR exists
    is_ready, reason = sched_svc.evaluate_task_readiness(cycle_id)
    assert is_ready is False


# ==============================================================================
# TEST 16: USER ISOLATION AND SERVER_ONLY PRIVACY
# ==============================================================================

@pytest.mark.anyio
async def test_16_user_isolation_and_server_only(db_session: Session):
    """
    Scenario 16: Patient/Cycle isolation and SERVER_ONLY privacy level.
    Cycle 1 cannot see Cycle 2 documents or pending tasks.
    """
    t0 = datetime(2026, 11, 6, 8, 0, tzinfo=timezone.utc)
    clock = MockClock(t0)

    # Cycle 1
    _seed_patient_and_cycle(db_session, "pat_user_1", "cyc_user_1", t0)
    # Cycle 2
    _seed_patient_and_cycle(db_session, "pat_user_2", "cyc_user_2", t0)

    sched_svc = LabSchedulingService(db=db_session, now_provider=clock.now)

    # Upload for Cycle 1
    sched_svc.save_uploaded_document("cyc_user_1", "pat_user_1", "p1.jpg", b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x01\x00\x60\x00\x60\x00\x00p1_data", "image/jpeg")
    sched_svc.process_manual_edit("cyc_user_1", "pat_user_1", [
        LabMarkerMutationItem(marker_key="glu", display_name="Glucose", value=5.1, unit="mmol/L", sample_date="2026-11-06")
    ])

    # Check Cycle 1 has documents and pending task
    assert len(sched_svc.get_document_records("cyc_user_1")) == 1
    assert sched_svc.get_pending_task("cyc_user_1") is not None

    # Check Cycle 2 sees zero documents and no pending task
    assert len(sched_svc.get_document_records("cyc_user_2")) == 0
    assert sched_svc.get_pending_task("cyc_user_2") is None

    # Check report privacy level in database
    report_repo = ModelReportRepository(db_session)
    c1_reports = report_repo.list_server_only("cyc_user_1")
    for r in c1_reports:
        assert r.privacy_level == PrivacyLevel.SERVER_ONLY.value
