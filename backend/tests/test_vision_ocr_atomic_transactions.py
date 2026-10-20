import asyncio
from datetime import datetime, timezone
import io
import json
from pathlib import Path
import threading
import time
from typing import Any, Dict, List, Optional
import httpx
import pytest
from fastapi import UploadFile
from sqlalchemy import create_engine, event, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from app.database import Base
from app.models.medical_cycle import MedicalCycle
from app.models.model_report import ModelReport
from app.repositories.cycle_repository import MedicalCycleRepository
from app.repositories.report_repository import (
    ModelReportRepository,
    ReportVersionConflictError,
)
from app.schemas.consilium import (
    LabEventType,
    LabMarkerMutationItem,
    LabVisionIntakeClientResponse,
)
from app.schemas.privacy import PrivacyLevel
from app.services.gemini_gateway import GeminiGateway, GeminiModelRole
from app.services.lab_revision_consilium_service import (
    LabRevisionConsiliumService,
    LABORATORY_LEDGER_REPORT_TYPE,
)
from app.services.lab_vision_ocr_service import (
    LabVisionOcrService,
    PREPARATORY_OCR_REPORT_TYPE,
)
from tests.test_primary_consilium import _gemini_envelope


# --- Test Helpers & Session Factory ---

def _build_file_engine(sqlite_url: str, busy_timeout_ms: int = 5000):
    engine = create_engine(
        sqlite_url,
        connect_args={"check_same_thread": False, "timeout": busy_timeout_ms / 1000.0},
        pool_pre_ping=True,
    )

    @event.listens_for(engine, "connect")
    def _set_custom_busy_timeout(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)};")
        cursor.close()

    return engine


def _create_file_session_factory(db_path: Path, busy_timeout_ms: int = 5000):
    sqlite_url = f"sqlite:///{db_path}"
    engine = _build_file_engine(sqlite_url, busy_timeout_ms=busy_timeout_ms)
    Base.metadata.create_all(bind=engine)
    session_factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    return engine, session_factory


def _seed_cycle(session: Session, cycle_id: str) -> MedicalCycle:
    now = datetime.now(timezone.utc)
    cycle = MedicalCycle(
        cycle_id=cycle_id,
        status="active",
        started_at=now,
        created_at=now,
        updated_at=now,
    )
    session.add(cycle)
    session.commit()
    session.refresh(cycle)
    return cycle


def _create_dummy_jpeg_bytes(length: int = 128) -> bytes:
    header = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x01\x00`\x00`\x00\x00"
    if length < len(header):
        return header[:length]
    return header + b"\x00" * (length - len(header))


def _make_upload_file(filename: str = "report.jpg", length: int = 128) -> UploadFile:
    content = _create_dummy_jpeg_bytes(length)
    return UploadFile(filename=filename, file=io.BytesIO(content))


def _sample_vision_ocr_payload(
    sample_date: str = "2026-10-15",
    markers: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    if markers is None:
        markers = [
            {
                "original_name": "Аланинаминотрансфераза (АЛТ)",
                "marker_key": "alt",
                "raw_value": "24.5",
                "numeric_value": 24.5,
                "unit": "Ед/л",
                "reference_range": {"low": 0.0, "high": 41.0, "text": "0 - 41"},
                "sample_date": sample_date,
                "category": "biochemistry",
                "is_uncertain": False,
                "uncertainty_note": None,
                "is_critical_flag": False,
            },
            {
                "original_name": "Аспартатаминотрансфераза (АСТ)",
                "marker_key": "ast",
                "raw_value": "21.0",
                "numeric_value": 21.0,
                "unit": "Ед/л",
                "reference_range": {"low": 0.0, "high": 37.0, "text": "0 - 37"},
                "sample_date": sample_date,
                "category": "biochemistry",
                "is_uncertain": False,
                "uncertainty_note": None,
                "is_critical_flag": False,
            },
        ]
    return {
        "document_metadata": {
            "sample_date": sample_date,
            "issue_date": "2026-10-16",
            "laboratory_name": "Test Lab",
            "page_count": 1,
            "notes": "Verified scan",
        },
        "markers": markers,
    }


def _build_mock_gateway(payload: Dict[str, Any]) -> GeminiGateway:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status_code=200,
            json=_gemini_envelope(payload),
        )

    transport = httpx.MockTransport(handler)
    return GeminiGateway(api_key="test-key", transport=transport)


# ==============================================================================
# C-6 AUDIT VERIFICATION TESTS
# ==============================================================================

@pytest.mark.anyio
async def test_scenario_1_successful_ocr_atomic_single_transaction(tmp_path: Path):
    """
    1. Успешный OCR:
    В единой транзакции сохраняются и preparatory_vision_ocr, и laboratory_ledger.
    Проверка через новую Session: оба отчёта сохранены, версии согласованы.
    """
    db_path = tmp_path / "scenario_1.db"
    engine, SessionFactory = _create_file_session_factory(db_path)
    cycle_id = "cycle_c6_scen1"

    with SessionFactory() as init_session:
        _seed_cycle(init_session, cycle_id)

    gateway = _build_mock_gateway(_sample_vision_ocr_payload())

    with SessionFactory() as intake_session:
        service = LabVisionOcrService(db=intake_session, gateway=gateway)
        res = await service.process_lab_images(
            cycle_id=cycle_id,
            files=[_make_upload_file()],
        )
        assert res.status == "processed"
        assert res.ledger_version == 1
        assert res.has_unreviewed_lab_changes is True

    # Проверка через НОВУЮ сессию
    with SessionFactory() as fresh_session:
        reports = fresh_session.execute(
            select(ModelReport).where(ModelReport.cycle_id == cycle_id)
        ).scalars().all()

        assert len(reports) == 2
        ocr_rep = next(r for r in reports if r.report_type == PREPARATORY_OCR_REPORT_TYPE)
        ledger_rep = next(r for r in reports if r.report_type == LABORATORY_LEDGER_REPORT_TYPE)

        assert ocr_rep.version == 1
        assert ledger_rep.version == 1
        assert ledger_rep.content["source"] == "preparatory_vision_ocr"
        assert "alt" in ledger_rep.content["active_markers"]
        assert "ast" in ledger_rep.content["active_markers"]


@pytest.mark.anyio
async def test_scenario_2_failure_after_ocr_before_ledger_leaves_zero_records(tmp_path: Path):
    """
    2. Сбой после формирования OCR-отчёта, но до обновления ledger:
    В базе не остаётся ничего (проверка через новую Session).
    """
    db_path = tmp_path / "scenario_2.db"
    engine, SessionFactory = _create_file_session_factory(db_path)
    cycle_id = "cycle_c6_scen2"

    with SessionFactory() as init_session:
        _seed_cycle(init_session, cycle_id)

    gateway = _build_mock_gateway(_sample_vision_ocr_payload())

    with SessionFactory() as intake_session:
        service = LabVisionOcrService(db=intake_session, gateway=gateway)

        # Hook: force error immediately after create_next_version for preparatory OCR
        orig_create = service.report_repo.create_next_version

        def _hooked_create(*args, **kwargs):
            rep = orig_create(*args, **kwargs)
            if kwargs.get("report_type") == PREPARATORY_OCR_REPORT_TYPE:
                raise RuntimeError("Simulated crash right after flushed OCR report")
            return rep

        service.report_repo.create_next_version = _hooked_create

        with pytest.raises(RuntimeError, match="Simulated crash right after flushed OCR report"):
            await service.process_lab_images(
                cycle_id=cycle_id,
                files=[_make_upload_file()],
            )

    # Проверка через НОВУЮ сессию: ноль записей
    with SessionFactory() as fresh_session:
        reports = fresh_session.execute(
            select(ModelReport).where(ModelReport.cycle_id == cycle_id)
        ).scalars().all()
        assert len(reports) == 0, f"Expected 0 reports in fresh session, got {len(reports)}"


@pytest.mark.anyio
async def test_scenario_3_failure_inside_ledger_update_rolls_back_everything(tmp_path: Path):
    """
    3. Сбой внутри обновления ledger:
    В базе не остаётся ничего (проверка через новую Session).
    """
    db_path = tmp_path / "scenario_3.db"
    engine, SessionFactory = _create_file_session_factory(db_path)
    cycle_id = "cycle_c6_scen3"

    with SessionFactory() as init_session:
        _seed_cycle(init_session, cycle_id)

    gateway = _build_mock_gateway(_sample_vision_ocr_payload())

    with SessionFactory() as intake_session:
        service = LabVisionOcrService(db=intake_session, gateway=gateway)

        # Hook: simulate failure inside ledger seeding
        orig_seed = service.lab_revision_service._ensure_baseline_ledger_seeded

        def _exploding_seed(*args, **kwargs):
            raise RuntimeError("Database error inside ledger seeding")

        service.lab_revision_service._ensure_baseline_ledger_seeded = _exploding_seed

        with pytest.raises(RuntimeError, match="Database error inside ledger seeding"):
            await service.process_lab_images(
                cycle_id=cycle_id,
                files=[_make_upload_file()],
            )

    # Проверка через НОВУЮ сессию: ноль записей
    with SessionFactory() as fresh_session:
        reports = fresh_session.execute(
            select(ModelReport).where(ModelReport.cycle_id == cycle_id)
        ).scalars().all()
        assert len(reports) == 0, f"Expected 0 reports in fresh session, got {len(reports)}"


@pytest.mark.anyio
async def test_scenario_4_failure_immediately_before_final_commit_rolls_back_everything(tmp_path: Path):
    """
    4. Сбой непосредственно перед финальным commit():
    В базе не остаётся ничего (проверка через новую Session).
    """
    db_path = tmp_path / "scenario_4.db"
    engine, SessionFactory = _create_file_session_factory(db_path)
    cycle_id = "cycle_c6_scen4"

    with SessionFactory() as init_session:
        _seed_cycle(init_session, cycle_id)

    gateway = _build_mock_gateway(_sample_vision_ocr_payload())

    with SessionFactory() as intake_session:
        service = LabVisionOcrService(db=intake_session, gateway=gateway)

        # Monkeypatch commit to explode immediately
        def _failing_commit():
            raise RuntimeError("Network/process crash immediately before commit completes")

        intake_session.commit = _failing_commit

        with pytest.raises(RuntimeError, match="Network/process crash immediately before commit completes"):
            await service.process_lab_images(
                cycle_id=cycle_id,
                files=[_make_upload_file()],
            )

    # Проверка через НОВУЮ сессию: ноль записей
    with SessionFactory() as fresh_session:
        reports = fresh_session.execute(
            select(ModelReport).where(ModelReport.cycle_id == cycle_id)
        ).scalars().all()
        assert len(reports) == 0, f"Expected 0 reports in fresh session, got {len(reports)}"


@pytest.mark.anyio
async def test_scenario_5_failure_during_final_commit_leaves_zero_partial_records(tmp_path: Path):
    """
    5. Сбой самого final commit (DB operational error / locked):
    Откат, отсутствие частичных записей (проверка через новую Session).
    """
    db_path = tmp_path / "scenario_5.db"
    engine, SessionFactory = _create_file_session_factory(db_path)
    cycle_id = "cycle_c6_scen5"

    with SessionFactory() as init_session:
        _seed_cycle(init_session, cycle_id)

    gateway = _build_mock_gateway(_sample_vision_ocr_payload())

    with SessionFactory() as intake_session:
        service = LabVisionOcrService(db=intake_session, gateway=gateway)

        # Simulate SQLite OperationalError on commit
        orig_commit = intake_session.commit

        def _locked_commit():
            raise OperationalError("PRAGMA commit", {}, Exception("database is locked"))

        intake_session.commit = _locked_commit

        with pytest.raises(OperationalError, match="database is locked"):
            await service.process_lab_images(
                cycle_id=cycle_id,
                files=[_make_upload_file()],
            )

    # Проверка через НОВУЮ сессию: ноль записей
    with SessionFactory() as fresh_session:
        reports = fresh_session.execute(
            select(ModelReport).where(ModelReport.cycle_id == cycle_id)
        ).scalars().all()
        assert len(reports) == 0, f"Expected 0 reports in fresh session, got {len(reports)}"


@pytest.mark.anyio
async def test_scenario_6_idempotent_reupload_no_duplicate_versions(tmp_path: Path):
    """
    6. Повторная отправка идентичного документа:
    Идемпотентность, 200 OK, отсутствие дубликатов версий.
    """
    db_path = tmp_path / "scenario_6.db"
    engine, SessionFactory = _create_file_session_factory(db_path)
    cycle_id = "cycle_c6_scen6"

    with SessionFactory() as init_session:
        _seed_cycle(init_session, cycle_id)

    gateway = _build_mock_gateway(_sample_vision_ocr_payload())

    # First intake
    with SessionFactory() as session_1:
        service_1 = LabVisionOcrService(db=session_1, gateway=gateway)
        res_1 = await service_1.process_lab_images(
            cycle_id=cycle_id,
            files=[_make_upload_file()],
        )
        assert res_1.ledger_version == 1

    # Second identical intake
    with SessionFactory() as session_2:
        service_2 = LabVisionOcrService(db=session_2, gateway=gateway)
        res_2 = await service_2.process_lab_images(
            cycle_id=cycle_id,
            files=[_make_upload_file()],
        )
        assert res_2.status == "processed"
        assert res_2.ledger_version == 1

    # Проверка через НОВУЮ сессию: ровно 2 отчёта (preparatory v1 и ledger v1, без дублей)
    with SessionFactory() as fresh_session:
        reports = fresh_session.execute(
            select(ModelReport).where(ModelReport.cycle_id == cycle_id)
        ).scalars().all()

        assert len(reports) == 2
        types = [r.report_type for r in reports]
        assert types.count(PREPARATORY_OCR_REPORT_TYPE) == 1
        assert types.count(LABORATORY_LEDGER_REPORT_TYPE) == 1


def test_scenario_7_concurrent_ocr_uploads_zero_lost_markers_across_threads(tmp_path: Path):
    """
    7. Конкурентные одновременные OCR-загрузки:
    Обе завершаются корректно, нет дубликатов версий, маркеры не теряются.
    """
    db_path = tmp_path / "scenario_7.db"
    engine, SessionFactory = _create_file_session_factory(db_path)
    cycle_id = "cycle_c6_scen7_uuid_99b0c678-7578-43da-a0f5-095a1ee594cb"

    with SessionFactory() as init_session:
        _seed_cycle(init_session, cycle_id)

    worker_payloads = [
        _sample_vision_ocr_payload(
            sample_date="2026-10-15",
            markers=[
                {
                    "original_name": "Аланинаминотрансфераза (АЛТ)",
                    "marker_key": "alt",
                    "raw_value": "24.5",
                    "numeric_value": 24.5,
                    "unit": "Ед/л",
                    "reference_range": {"low": 0.0, "high": 41.0, "text": "0 - 41"},
                    "sample_date": "2026-10-15",
                    "category": "biochemistry",
                    "is_uncertain": False,
                    "uncertainty_note": None,
                    "is_critical_flag": False,
                }
            ],
        ),
        _sample_vision_ocr_payload(
            sample_date="2026-10-16",
            markers=[
                {
                    "original_name": "Глюкоза (венозная кровь)",
                    "marker_key": "glucose",
                    "raw_value": "5.1",
                    "numeric_value": 5.1,
                    "unit": "ммоль/л",
                    "reference_range": {"low": 4.1, "high": 5.9, "text": "4.1 - 5.9"},
                    "sample_date": "2026-10-16",
                    "category": "endocrinology",
                    "is_uncertain": False,
                    "uncertainty_note": None,
                    "is_critical_flag": False,
                }
            ],
        ),
    ]

    barrier = threading.Barrier(len(worker_payloads))
    errors: List[Exception] = []
    results: List[LabVisionIntakeClientResponse] = []
    lock = threading.Lock()

    def _ocr_worker(payload: Dict[str, Any]):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        session = SessionFactory()
        try:
            gateway = _build_mock_gateway(payload)
            service = LabVisionOcrService(db=session, gateway=gateway)
            barrier.wait(timeout=5.0)

            res = loop.run_until_complete(
                service.process_lab_images(
                    cycle_id=cycle_id,
                    files=[_make_upload_file()],
                )
            )
            with lock:
                results.append(res)
        except Exception as exc:
            with lock:
                errors.append(exc)
        finally:
            session.close()
            loop.close()

    threads = [
        threading.Thread(target=_ocr_worker, args=(p,), daemon=True)
        for p in worker_payloads
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15.0)
    for t in threads:
        assert not t.is_alive(), f"Thread {t.name} did not terminate cleanly within timeout"

    assert errors == [], f"Unexpected errors during concurrent OCR: {errors}"
    assert len(results) == 2

    # Проверка через НОВУЮ сессию
    with SessionFactory() as fresh_session:
        reports = fresh_session.execute(
            select(ModelReport)
            .where(ModelReport.cycle_id == cycle_id)
            .order_by(ModelReport.report_type.asc(), ModelReport.version.asc())
        ).scalars().all()

        ocr_reps = [r for r in reports if r.report_type == PREPARATORY_OCR_REPORT_TYPE]
        ledger_reps = [r for r in reports if r.report_type == LABORATORY_LEDGER_REPORT_TYPE]

        # Both preparatory reports exist with sequential versions (v1, v2)
        assert len(ocr_reps) == 2
        assert [r.version for r in ocr_reps] == [1, 2]

        # Both ledger reports exist with sequential versions (v1, v2)
        assert len(ledger_reps) == 2
        assert [r.version for r in ledger_reps] == [1, 2]

        # Latest ledger contains markers from BOTH concurrent uploads (zero lost markers!)
        latest_ledger = ledger_reps[-1]
        active_markers = latest_ledger.content.get("active_markers", {})
        assert "alt" in active_markers, "Marker 'alt' missing from final ledger!"
        assert "glucose" in active_markers, "Marker 'glucose' missing from final ledger!"


@pytest.mark.anyio
async def test_scenario_8_compatibility_standalone_upsert_and_ledger_state(tmp_path: Path):
    """
    8. Совместимость:
    Одиночные запросы /labs/upsert и get_lab_ledger_state продолжают работать корректно
    (с default commit=True) поверх базы с существующим OCR.
    """
    db_path = tmp_path / "scenario_8.db"
    engine, SessionFactory = _create_file_session_factory(db_path)
    cycle_id = "cycle_c6_scen8"

    with SessionFactory() as init_session:
        _seed_cycle(init_session, cycle_id)

    gateway = _build_mock_gateway(_sample_vision_ocr_payload())

    # 1. OCR intake
    with SessionFactory() as session_1:
        service_1 = LabVisionOcrService(db=session_1, gateway=gateway)
        res_1 = await service_1.process_lab_images(
            cycle_id=cycle_id,
            files=[_make_upload_file()],
        )
        assert res_1.ledger_version == 1

    # 2. Standalone upsert_laboratory_markers (default commit=True)
    with SessionFactory() as session_2:
        revision_service = LabRevisionConsiliumService(db=session_2)
        res_upsert = revision_service.upsert_laboratory_markers(
            cycle_id=cycle_id,
            mutations=[
                LabMarkerMutationItem(
                    marker_key="ferritin",
                    display_name="Ferritin",
                    value=75.0,
                    unit="ng/mL",
                    sample_date="2026-10-18",
                    event_type=LabEventType.INITIAL,
                )
            ],
        )
        assert res_upsert.ledger_version == 2

    # 3. Standalone get_lab_ledger_state (default commit=True)
    with SessionFactory() as session_3:
        revision_service_3 = LabRevisionConsiliumService(db=session_3)
        state_3 = revision_service_3.get_lab_ledger_state(cycle_id)
        assert state_3.ledger_version == 2
        assert "alt" in state_3.active_markers
        assert "ast" in state_3.active_markers
        assert "ferritin" in state_3.active_markers

    # 4. Проверка через НОВУЮ сессию
    with SessionFactory() as fresh_session:
        reports = fresh_session.execute(
            select(ModelReport)
            .where(ModelReport.cycle_id == cycle_id, ModelReport.report_type == LABORATORY_LEDGER_REPORT_TYPE)
            .order_by(ModelReport.version.asc())
        ).scalars().all()

        assert len(reports) == 2
        assert reports[0].version == 1
        assert reports[1].version == 2
        assert "ferritin" in reports[1].content["active_markers"]


def test_scenario_9_cross_event_loop_shared_registry_no_deadlock(tmp_path: Path):
    """
    9. Регрессионный тест кросс-event-loop блокировок с явно переданным общим registry:
    Два системных потока с РАЗНЫМИ asyncio event loop используют ОДИН общий registry словарь.
    Благодаря loop-изоляции в LabVisionOcrService deadlock не возникает, оба запроса
    успешно завершаются, и в registry формируются раздельные записи по реальным объектам loop.
    """
    db_path = tmp_path / "scenario_9.db"
    engine, SessionFactory = _create_file_session_factory(db_path)
    cycle_id = "cycle_c6_scen9_shared_reg"

    with SessionFactory() as init_session:
        _seed_cycle(init_session, cycle_id)

    shared_registry: Dict[Any, Any] = {}
    barrier = threading.Barrier(2)
    errors: List[Exception] = []
    results: List[LabVisionIntakeClientResponse] = []
    lock = threading.Lock()

    worker_payloads = [
        _sample_vision_ocr_payload(
            sample_date="2026-10-15",
            markers=[
                {
                    "original_name": "ALT",
                    "marker_key": "alt",
                    "raw_value": "20.0",
                    "numeric_value": 20.0,
                    "unit": "U/L",
                    "reference_range": {"low": 0.0, "high": 40.0, "text": "0 - 40"},
                    "sample_date": "2026-10-15",
                    "category": "biochemistry",
                    "is_uncertain": False,
                    "uncertainty_note": None,
                    "is_critical_flag": False,
                }
            ],
        ),
        _sample_vision_ocr_payload(
            sample_date="2026-10-16",
            markers=[
                {
                    "original_name": "AST",
                    "marker_key": "ast",
                    "raw_value": "22.0",
                    "numeric_value": 22.0,
                    "unit": "U/L",
                    "reference_range": {"low": 0.0, "high": 40.0, "text": "0 - 40"},
                    "sample_date": "2026-10-16",
                    "category": "biochemistry",
                    "is_uncertain": False,
                    "uncertainty_note": None,
                    "is_critical_flag": False,
                }
            ],
        ),
    ]

    def _worker(payload: Dict[str, Any]):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        session = SessionFactory()
        try:
            gateway = _build_mock_gateway(payload)
            # Shared intake_lock_registry explicitly passed to both workers across distinct loops
            service = LabVisionOcrService(
                db=session,
                gateway=gateway,
                intake_lock_registry=shared_registry,
            )
            barrier.wait(timeout=5.0)
            res = loop.run_until_complete(
                service.process_lab_images(
                    cycle_id=cycle_id,
                    files=[_make_upload_file()],
                )
            )
            with lock:
                results.append(res)
        except Exception as exc:
            with lock:
                errors.append(exc)
        finally:
            session.close()
            loop.close()

    threads = [
        threading.Thread(target=_worker, args=(p,), daemon=True)
        for p in worker_payloads
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10.0)
    for t in threads:
        assert not t.is_alive(), f"Thread {t.name} did not terminate cleanly within timeout"

    assert errors == [], f"Unexpected errors during cross-loop shared registry OCR: {errors}"
    assert len(results) == 2
    # Verify both loops were recorded separately in the shared registry
    assert len(shared_registry) == 2
    for loop_obj, locks in shared_registry.items():
        assert isinstance(locks, dict)
        assert cycle_id in locks
        assert isinstance(locks[cycle_id], asyncio.Lock)


@pytest.mark.anyio
async def test_scenario_10_single_event_loop_coroutine_serialization(tmp_path: Path):
    """
    10. Регрессионный тест сериализации двух корутин внутри одного event loop:
    Две корутины запускаются конкурентно (asyncio.gather) с ОДИНАКОВЫМ cycle_id
    внутри одного event loop. Обе сериализуются через _get_intake_lock(cycle_id),
    выполняются без взаимных коллизий и последовательно увеличивают версию ledger (v1, затем v2).
    """
    db_path = tmp_path / "scenario_10.db"
    engine, SessionFactory = _create_file_session_factory(db_path)
    cycle_id = "cycle_c6_scen10_single_loop_serialization"

    with SessionFactory() as init_session:
        _seed_cycle(init_session, cycle_id)

    payload_1 = _sample_vision_ocr_payload(
        sample_date="2026-10-15",
        markers=[
            {
                "original_name": "ALT",
                "marker_key": "alt",
                "raw_value": "35.0",
                "numeric_value": 35.0,
                "unit": "U/L",
                "reference_range": {"low": 0.0, "high": 40.0, "text": "0 - 40"},
                "sample_date": "2026-10-15",
                "category": "biochemistry",
                "is_uncertain": False,
                "uncertainty_note": None,
                "is_critical_flag": False,
            }
        ],
    )
    payload_2 = _sample_vision_ocr_payload(
        sample_date="2026-10-16",
        markers=[
            {
                "original_name": "AST",
                "marker_key": "ast",
                "raw_value": "42.0",
                "numeric_value": 42.0,
                "unit": "U/L",
                "reference_range": {"low": 0.0, "high": 40.0, "text": "0 - 40"},
                "sample_date": "2026-10-16",
                "category": "biochemistry",
                "is_uncertain": False,
                "uncertainty_note": None,
                "is_critical_flag": False,
            }
        ],
    )

    shared_intake_locks: Dict[Any, Any] = {}
    shared_ledger_locks: Dict[str, Any] = {}

    async def _run_intake(payload: Dict[str, Any]) -> LabVisionIntakeClientResponse:
        with SessionFactory() as session:
            gateway = _build_mock_gateway(payload)
            service = LabVisionOcrService(
                db=session,
                gateway=gateway,
                intake_lock_registry=shared_intake_locks,
                ledger_lock_registry=shared_ledger_locks,
            )
            return await service.process_lab_images(
                cycle_id=cycle_id,
                files=[_make_upload_file()],
            )

    res_1, res_2 = await asyncio.gather(_run_intake(payload_1), _run_intake(payload_2))

    assert {res_1.ledger_version, res_2.ledger_version} == {1, 2}
    assert res_1.status == "processed"
    assert res_2.status == "processed"

    with SessionFactory() as verify_session:
        reports = verify_session.execute(
            select(ModelReport)
            .where(ModelReport.cycle_id == cycle_id, ModelReport.report_type == LABORATORY_LEDGER_REPORT_TYPE)
            .order_by(ModelReport.version.asc())
        ).scalars().all()

        assert len(reports) == 2
        assert reports[0].version == 1
        assert reports[1].version == 2
        final_content = reports[1].content
        assert "alt" in final_content["active_markers"]
        assert "ast" in final_content["active_markers"]
