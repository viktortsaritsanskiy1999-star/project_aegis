from datetime import datetime, timezone, timedelta
from typing import Any, Dict
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import MedicalCycle, HealthMeasurement
from app.repositories.cycle_repository import MedicalCycleRepository
from app.repositories.measurement_repository import HealthMeasurementRepository
from app.schemas.health import (
    HealthDataSource,
    HealthMeasurementCreate,
    HealthMeasurementCreateBatch,
    PULSE_MIN,
    PULSE_MAX,
    SYSTOLIC_MIN,
    SYSTOLIC_MAX,
    DIASTOLIC_MIN,
    DIASTOLIC_MAX,
    SPO2_MIN,
    SPO2_MAX,
)
from app.services.primary_consilium_service import PrimaryConsiliumService


def _create_active_cycle(db: Session, cycle_id: str = "cycle_vitals_test_01") -> MedicalCycle:
    repo = MedicalCycleRepository(db)
    return repo.create(cycle_id=cycle_id, status="active")


# ------------------------------------------------------------------------------
# 1. ОДИНОЧНОЕ ИЗМЕРЕНИЕ
# ------------------------------------------------------------------------------

def test_single_measurement_ingestion_success(
    client: TestClient,
    db_session: Session,
    auth_headers: dict[str, str],
):
    cycle = _create_active_cycle(db_session, "cycle_single_01")
    now_utc = datetime.now(timezone.utc)

    payload = {
        "cycle_id": cycle.cycle_id,
        "measured_at": now_utc.isoformat(),
        "source": "watch_sensor",
        "pulse": 72,
        "systolic": 120,
        "diastolic": 80,
        "spo2": 98,
        "phase": "standby",
        "note": "Resting pulse reading",
    }

    resp = client.post("/api/v1/vitals/measurement", json=payload, headers=auth_headers)
    assert resp.status_code == 201, resp.text
    data = resp.json()

    assert data["cycle_id"] == cycle.cycle_id
    assert data["pulse"] == 72
    assert data["systolic"] == 120
    assert data["diastolic"] == 80
    assert data["spo2"] == 98
    assert data["source"] == "watch_sensor"
    assert data["is_historical"] is False
    assert data["is_simulated"] is False
    assert data["status"] == "recorded"
    assert "id" in data

    # Verify persistence in isolated test DB
    meas_repo = HealthMeasurementRepository(db_session)
    stored = meas_repo.list_by_cycle(cycle.cycle_id)
    assert len(stored) == 1
    assert stored[0].pulse == 72
    assert stored[0].systolic == 120


# ------------------------------------------------------------------------------
# 2. ПАКЕТНАЯ ОБРАБОТКА (BATCH)
# ------------------------------------------------------------------------------

def test_batch_measurement_ingestion_success(
    client: TestClient,
    db_session: Session,
    auth_headers: dict[str, str],
):
    cycle = _create_active_cycle(db_session, "cycle_batch_01")
    t0 = datetime.now(timezone.utc) - timedelta(minutes=10)

    items = [
        {
            "measured_at": (t0 + timedelta(minutes=i)).isoformat(),
            "source": "watch_sensor",
            "pulse": 70 + i * 2,
            "systolic": 118 + i,
            "diastolic": 76,
            "spo2": 99,
        }
        for i in range(5)
    ]

    batch_payload = {
        "cycle_id": cycle.cycle_id,
        "measurements": items,
    }

    resp = client.post("/api/v1/vitals/batch", json=batch_payload, headers=auth_headers)
    assert resp.status_code == 201, resp.text
    data = resp.json()

    assert data["cycle_id"] == cycle.cycle_id
    assert data["total_received"] == 5
    assert data["recorded_count"] == 5
    assert data["deduplicated_count"] == 0
    assert len(data["items"]) == 5

    meas_repo = HealthMeasurementRepository(db_session)
    stored = meas_repo.list_by_cycle(cycle.cycle_id)
    assert len(stored) == 5


# ------------------------------------------------------------------------------
# 3. ПОВТОРНАЯ ДОСТАВКА И ИДЕМПОТЕНТНОСТЬ
# ------------------------------------------------------------------------------

def test_duplicate_delivery_idempotency_single(
    client: TestClient,
    db_session: Session,
    auth_headers: dict[str, str],
):
    cycle = _create_active_cycle(db_session, "cycle_dedup_01")
    now_utc = datetime.now(timezone.utc) - timedelta(minutes=2)

    payload = {
        "cycle_id": cycle.cycle_id,
        "measured_at": now_utc.isoformat(),
        "source": "watch_sensor",
        "pulse": 68,
        "systolic": 115,
        "diastolic": 75,
        "spo2": 98,
        "raw_metadata": {"idempotency_key": "device_evt_9981"},
    }

    # 1. First delivery -> 201 Created
    r1 = client.post("/api/v1/vitals/measurement", json=payload, headers=auth_headers)
    assert r1.status_code == 201, r1.text
    d1 = r1.json()
    assert d1["status"] == "recorded"
    meas_id = d1["id"]

    # 2. Re-delivery of identical payload -> 200 OK (deduplicated)
    r2 = client.post("/api/v1/vitals/measurement", json=payload, headers=auth_headers)
    assert r2.status_code == 200, r2.text
    d2 = r2.json()
    assert d2["status"] == "deduplicated"
    assert d2["id"] == meas_id

    # Verify no duplicate row was created
    meas_repo = HealthMeasurementRepository(db_session)
    records = meas_repo.list_by_cycle(cycle.cycle_id)
    assert len(records) == 1


def test_duplicate_delivery_idempotency_batch(
    client: TestClient,
    db_session: Session,
    auth_headers: dict[str, str],
):
    cycle = _create_active_cycle(db_session, "cycle_dedup_batch")
    t0 = datetime.now(timezone.utc) - timedelta(minutes=5)

    items = [
        {
            "measured_at": (t0 + timedelta(minutes=1)).isoformat(),
            "source": "watch_sensor",
            "pulse": 65,
            "raw_metadata": {"idempotency_key": "item_1"},
        },
        {
            "measured_at": (t0 + timedelta(minutes=2)).isoformat(),
            "source": "watch_sensor",
            "pulse": 70,
            "raw_metadata": {"idempotency_key": "item_2"},
        },
    ]

    # Initial batch
    r1 = client.post(
        "/api/v1/vitals/batch",
        json={"cycle_id": cycle.cycle_id, "measurements": items},
        headers=auth_headers,
    )
    assert r1.status_code == 201
    assert r1.json()["recorded_count"] == 2

    # Second batch containing 2 existing items + 1 new item
    items_with_new = items + [
        {
            "measured_at": (t0 + timedelta(minutes=3)).isoformat(),
            "source": "watch_sensor",
            "pulse": 75,
            "raw_metadata": {"idempotency_key": "item_3"},
        }
    ]

    r2 = client.post(
        "/api/v1/vitals/batch",
        json={"cycle_id": cycle.cycle_id, "measurements": items_with_new},
        headers=auth_headers,
    )
    assert r2.status_code == 201
    d2 = r2.json()
    assert d2["total_received"] == 3
    assert d2["recorded_count"] == 1
    assert d2["deduplicated_count"] == 2

    meas_repo = HealthMeasurementRepository(db_session)
    assert len(meas_repo.list_by_cycle(cycle.cycle_id)) == 3


def test_batch_internal_duplicate_deduplication(
    client: TestClient,
    db_session: Session,
    auth_headers: dict[str, str],
):
    cycle = _create_active_cycle(db_session, "cycle_internal_dup")
    t = (datetime.now(timezone.utc) - timedelta(minutes=3)).isoformat()

    identical_item = {
        "measured_at": t,
        "source": "watch_sensor",
        "pulse": 82,
        "systolic": 125,
        "diastolic": 80,
    }

    # Batch with two identical items in the same payload
    r = client.post(
        "/api/v1/vitals/batch",
        json={"cycle_id": cycle.cycle_id, "measurements": [identical_item, identical_item]},
        headers=auth_headers,
    )
    assert r.status_code == 201
    d = r.json()
    assert d["total_received"] == 2
    assert d["recorded_count"] == 1
    assert d["deduplicated_count"] == 1

    meas_repo = HealthMeasurementRepository(db_session)
    assert len(meas_repo.list_by_cycle(cycle.cycle_id)) == 1


# ------------------------------------------------------------------------------
# 4. РАЗНЫЕ ИСТОЧНИКИ (SOURCES)
# ------------------------------------------------------------------------------

def test_different_sources_preservation(
    client: TestClient,
    db_session: Session,
    auth_headers: dict[str, str],
):
    cycle = _create_active_cycle(db_session, "cycle_sources_test")
    t0 = datetime.now(timezone.utc) - timedelta(minutes=10)

    sources_to_test = [
        ("watch_sensor", False, 72),
        ("manual_input", False, 75),
        ("test_chip", True, 140),
        ("simulated", True, 88),
        ("unknown", False, 64),
    ]

    for idx, (src, is_sim, pulse_val) in enumerate(sources_to_test):
        payload = {
            "cycle_id": cycle.cycle_id,
            "measured_at": (t0 + timedelta(minutes=idx)).isoformat(),
            "source": src,
            "pulse": pulse_val,
            "is_simulated": is_sim,
        }
        res = client.post("/api/v1/vitals/measurement", json=payload, headers=auth_headers)
        assert res.status_code == 201, f"Failed for source '{src}': {res.text}"
        data = res.json()
        assert data["source"] == src
        assert data["is_simulated"] == is_sim

    meas_repo = HealthMeasurementRepository(db_session)
    all_recs = meas_repo.list_by_cycle(cycle.cycle_id)
    assert len(all_recs) == 5

    # Confirm unknown is preserved as unknown, not defaulted to watch_sensor
    unknown_rec = next(r for r in all_recs if r.source == "unknown")
    assert unknown_rec.source == "unknown"


# ------------------------------------------------------------------------------
# 5. ИСТОРИЧЕСКИЕ ИЗМЕРЕНИЯ И ИХ ИСКЛЮЧЕНИЕ ИЗ LIVE STREAM
# ------------------------------------------------------------------------------

def test_historical_measurements_flag_and_exclusion(
    client: TestClient,
    db_session: Session,
    auth_headers: dict[str, str],
):
    cycle = _create_active_cycle(db_session, "cycle_hist_test")
    t_live = datetime.now(timezone.utc) - timedelta(minutes=1)
    t_past = datetime.now(timezone.utc) - timedelta(days=5)

    # 1. Ingest historical batch with as_historical=True
    historical_items = [
        {
            "measured_at": (t_past + timedelta(hours=i)).isoformat(),
            "source": "manual_input",
            "pulse": 140,  # Old elevated pulse
            "systolic": 160,
            "diastolic": 100,
        }
        for i in range(2)
    ]

    r_hist = client.post(
        "/api/v1/vitals/batch",
        json={
            "cycle_id": cycle.cycle_id,
            "as_historical": True,
            "measurements": historical_items,
        },
        headers=auth_headers,
    )
    assert r_hist.status_code == 201
    for item in r_hist.json()["items"]:
        assert item["is_historical"] is True

    # Check live stream when ONLY historical records exist:
    # Must NOT pretend old measurements are live vitals stream!
    r_stream_before_live = client.get(
        f"/api/v1/vitals/latest?cycle_id={cycle.cycle_id}",
        headers=auth_headers,
    )
    assert r_stream_before_live.status_code == 200
    st_before = r_stream_before_live.json()
    assert st_before["heart_rate"] == "-- bpm"
    assert st_before["blood_pressure"] == "--/-- mmHg"

    # 2. Ingest live reading
    r_live = client.post(
        "/api/v1/vitals/measurement",
        json={
            "cycle_id": cycle.cycle_id,
            "measured_at": t_live.isoformat(),
            "source": "watch_sensor",
            "pulse": 72,
            "systolic": 118,
            "diastolic": 78,
            "spo2": 99,
            "is_historical": False,
        },
        headers=auth_headers,
    )
    assert r_live.status_code == 201

    # Check live stream now: must return ONLY the live values (72 bpm, 118/78 mmHg),
    # strictly ignoring historical 140 bpm and 160/100 mmHg!
    r_stream_after_live = client.get(
        f"/api/v1/vitals/latest?cycle_id={cycle.cycle_id}",
        headers=auth_headers,
    )
    assert r_stream_after_live.status_code == 200
    st_after = r_stream_after_live.json()
    assert st_after["heart_rate"] == "72 bpm"
    assert st_after["blood_pressure"] == "118/78 mmHg"
    assert st_after["spo2"] == "99%"


# ------------------------------------------------------------------------------
# 6. НЕКОРРЕКТНЫЕ TIMESTAMPS И ДИАПАЗОНЫ ТЕХНИЧЕСКОЙ ДОПУСТИМОСТИ
# ------------------------------------------------------------------------------

def test_invalid_timestamps_validation(
    client: TestClient,
    db_session: Session,
    auth_headers: dict[str, str],
):
    cycle = _create_active_cycle(db_session, "cycle_bad_time")

    # Future timestamp (+2 hours)
    future_time = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
    r_future = client.post(
        "/api/v1/vitals/measurement",
        json={
            "cycle_id": cycle.cycle_id,
            "measured_at": future_time,
            "source": "watch_sensor",
            "pulse": 70,
        },
        headers=auth_headers,
    )
    assert r_future.status_code == 422
    assert "cannot be in the future" in r_future.text

    # Unacceptable past year (e.g. 1850)
    r_old = client.post(
        "/api/v1/vitals/measurement",
        json={
            "cycle_id": cycle.cycle_id,
            "measured_at": "1850-01-01T12:00:00Z",
            "source": "watch_sensor",
            "pulse": 70,
        },
        headers=auth_headers,
    )
    assert r_old.status_code == 422


def test_technical_range_and_metrics_validation(
    client: TestClient,
    db_session: Session,
    auth_headers: dict[str, str],
):
    cycle = _create_active_cycle(db_session, "cycle_range_check")
    now_str = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()

    # 1. Pulse outside technical equipment limits (e.g. 10 or 350)
    r_low_pulse = client.post(
        "/api/v1/vitals/measurement",
        json={"cycle_id": cycle.cycle_id, "measured_at": now_str, "pulse": PULSE_MIN - 1},
        headers=auth_headers,
    )
    assert r_low_pulse.status_code == 422

    r_high_pulse = client.post(
        "/api/v1/vitals/measurement",
        json={"cycle_id": cycle.cycle_id, "measured_at": now_str, "pulse": PULSE_MAX + 1},
        headers=auth_headers,
    )
    assert r_high_pulse.status_code == 422

    # 2. Systolic < Diastolic (impossible physical reading)
    r_inv_bp = client.post(
        "/api/v1/vitals/measurement",
        json={
            "cycle_id": cycle.cycle_id,
            "measured_at": now_str,
            "systolic": 80,
            "diastolic": 120,
        },
        headers=auth_headers,
    )
    assert r_inv_bp.status_code == 422
    assert "cannot be lower than diastolic" in r_inv_bp.text

    # 3. All metrics null
    r_no_metric = client.post(
        "/api/v1/vitals/measurement",
        json={"cycle_id": cycle.cycle_id, "measured_at": now_str},
        headers=auth_headers,
    )
    assert r_no_metric.status_code == 422
    assert "At least one biometric metric" in r_no_metric.text


# ------------------------------------------------------------------------------
# 7. АВТОРИЗАЦИЯ И ДОСТУП К ЦИКЛАМ
# ------------------------------------------------------------------------------

def test_unauthorized_access(client: TestClient):
    payload = {
        "cycle_id": "any_cycle",
        "measured_at": datetime.now(timezone.utc).isoformat(),
        "pulse": 70,
    }

    # No token
    r_no_auth = client.post("/api/v1/vitals/measurement", json=payload)
    assert r_no_auth.status_code == 401

    # Invalid token
    r_bad_token = client.post(
        "/api/v1/vitals/measurement",
        json=payload,
        headers={"Authorization": "Bearer bad_secret_token_xyz"},
    )
    assert r_bad_token.status_code == 401


def test_cycle_not_found_and_inactive_cycle(
    client: TestClient,
    db_session: Session,
    auth_headers: dict[str, str],
):
    # Non-existent cycle
    r_not_found = client.post(
        "/api/v1/vitals/measurement",
        json={
            "cycle_id": "non_existent_cycle_999",
            "measured_at": (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(),
            "pulse": 70,
        },
        headers=auth_headers,
    )
    assert r_not_found.status_code == 404
    assert "not found" in r_not_found.text

    # Inactive/closed cycle
    cycle_repo = MedicalCycleRepository(db_session)
    closed_cycle = cycle_repo.create(cycle_id="cycle_closed_01", status="closed")
    r_closed = client.post(
        "/api/v1/vitals/measurement",
        json={
            "cycle_id": closed_cycle.cycle_id,
            "measured_at": (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(),
            "pulse": 70,
        },
        headers=auth_headers,
    )
    assert r_closed.status_code == 400
    assert "not active" in r_closed.text


# ------------------------------------------------------------------------------
# 8. АТОМАРНОСТЬ И ROLLBACK ПРИ ОШИБКЕ BATCH
# ------------------------------------------------------------------------------

def test_batch_rollback_on_failure(
    db_session: Session,
):
    cycle = _create_active_cycle(db_session, "cycle_rollback_test")
    meas_repo = HealthMeasurementRepository(db_session)
    t0 = datetime.now(timezone.utc) - timedelta(minutes=10)

    # Valid item
    m_valid = HealthMeasurementCreate(
        cycle_id=cycle.cycle_id,
        measured_at=t0,
        pulse=70,
        source=HealthDataSource.WATCH_SENSOR,
    )

    # Create batch items
    items = [m_valid]

    # Monkeypatch session to fail on commit
    original_commit = db_session.commit

    def failing_commit():
        raise RuntimeError("Simulated database failure during batch commit!")

    db_session.commit = failing_commit

    with pytest.raises(RuntimeError, match="Simulated database failure"):
        meas_repo.ingest_batch(items, cycle_id=cycle.cycle_id)

    db_session.commit = original_commit

    # Verify zero records were saved due to atomic rollback
    persisted = meas_repo.list_by_cycle(cycle.cycle_id)
    assert len(persisted) == 0


# ------------------------------------------------------------------------------
# 9. РЕАЛЬНОЕ ЧТЕНИЕ ДАННЫХ PRIMARY CONSILIUM CONTEXT SERVICE
# ------------------------------------------------------------------------------

def test_real_reading_by_primary_consilium_context_service(
    client: TestClient,
    db_session: Session,
    auth_headers: dict[str, str],
):
    cycle = _create_active_cycle(db_session, "cycle_consilium_ctx")
    t_past = datetime.now(timezone.utc) - timedelta(days=2)
    t_live = datetime.now(timezone.utc) - timedelta(minutes=5)

    # 1. Ingest historical measurement via API (should NOT be used as live stream)
    r_hist = client.post(
        "/api/v1/vitals/measurement",
        json={
            "cycle_id": cycle.cycle_id,
            "measured_at": t_past.isoformat(),
            "source": "manual_input",
            "pulse": 130,
            "systolic": 155,
            "diastolic": 95,
            "spo2": 95,
            "is_historical": True,
        },
        headers=auth_headers,
    )
    assert r_hist.status_code == 201

    # 2. Ingest live measurement via API
    r_live = client.post(
        "/api/v1/vitals/measurement",
        json={
            "cycle_id": cycle.cycle_id,
            "measured_at": t_live.isoformat(),
            "source": "watch_sensor",
            "pulse": 68,
            "systolic": 118,
            "diastolic": 76,
            "spo2": 99,
            "is_historical": False,
        },
        headers=auth_headers,
    )
    assert r_live.status_code == 201

    # 3. Instantiate PrimaryConsiliumService and test _load_latest_vitals_stream
    service = PrimaryConsiliumService(db=db_session)
    vitals_stream = service._load_latest_vitals_stream(
        cycle_id=cycle.cycle_id,
        ecg_summary="Sinus rhythm 68 bpm, no ischemic ST changes",
    )

    # Verify that the service receives real ingested live measurements, NOT defaults!
    assert vitals_stream["blood_pressure"] == "118/76 mmHg"
    assert vitals_stream["heart_rate"] == "68 bpm"
    assert vitals_stream["spo2"] == "99%"
    assert vitals_stream["ecg_summary"] == "Sinus rhythm 68 bpm, no ischemic ST changes"

    # Confirm historical 130 bpm and 155/95 mmHg were strictly excluded from the live stream
    assert "130" not in vitals_stream["heart_rate"]
    assert "155" not in vitals_stream["blood_pressure"]


# ------------------------------------------------------------------------------
# 10. CYCLE ISOLATION (BATCH ITEM CYCLE CONFLICT & INACTIVE CYCLE)
# ------------------------------------------------------------------------------

def test_batch_cycle_id_conflict_rejection(
    client: TestClient,
    db_session: Session,
    auth_headers: dict[str, str],
):
    cycle_a = _create_active_cycle(db_session, "cycle_iso_a")
    cycle_b = _create_active_cycle(db_session, "cycle_iso_b")
    t0 = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()

    # Batch targeting cycle_a, but contains an item specifying cycle_b
    payload = {
        "cycle_id": cycle_a.cycle_id,
        "measurements": [
            {
                "cycle_id": cycle_a.cycle_id,
                "measured_at": t0,
                "source": "watch_sensor",
                "pulse": 75,
            },
            {
                "cycle_id": cycle_b.cycle_id,  # CONFLICT!
                "measured_at": t0,
                "source": "watch_sensor",
                "pulse": 85,
            },
        ],
    }

    resp = client.post("/api/v1/vitals/batch", json=payload, headers=auth_headers)
    assert resp.status_code in (400, 422), resp.text
    assert "conflicts with batch" in resp.text or "cycle_id" in resp.text

    # Verify zero records written to either cycle
    meas_repo = HealthMeasurementRepository(db_session)
    assert len(meas_repo.list_by_cycle(cycle_a.cycle_id)) == 0
    assert len(meas_repo.list_by_cycle(cycle_b.cycle_id)) == 0

    # Test repository level rejection
    item_conflict = HealthMeasurementCreate(
        cycle_id=cycle_b.cycle_id,
        measured_at=datetime.now(timezone.utc) - timedelta(minutes=1),
        pulse=70,
    )
    with pytest.raises(ValueError, match="conflicts with batch cycle_id"):
        meas_repo.ingest_batch([item_conflict], cycle_id=cycle_a.cycle_id)


# ------------------------------------------------------------------------------
# 11. SIMULATED DATA ISOLATION FROM MEDICAL CONTEXT
# ------------------------------------------------------------------------------

def test_simulated_data_isolation_from_consiliums_and_latest(
    client: TestClient,
    db_session: Session,
    auth_headers: dict[str, str],
):
    cycle = _create_active_cycle(db_session, "cycle_sim_iso")
    t_base = datetime.now(timezone.utc) - timedelta(minutes=20)

    # 1. Ingest test_chip data (synthetic)
    r1 = client.post(
        "/api/v1/vitals/measurement",
        json={
            "cycle_id": cycle.cycle_id,
            "measured_at": (t_base + timedelta(minutes=1)).isoformat(),
            "source": "test_chip",
            "pulse": 180,
            "systolic": 190,
            "diastolic": 110,
            "spo2": 88,
        },
        headers=auth_headers,
    )
    assert r1.status_code == 201
    assert r1.json()["is_simulated"] is True
    assert r1.json()["source"] == "test_chip"

    # 2. Ingest simulated data
    r2 = client.post(
        "/api/v1/vitals/measurement",
        json={
            "cycle_id": cycle.cycle_id,
            "measured_at": (t_base + timedelta(minutes=2)).isoformat(),
            "source": "simulated",
            "pulse": 175,
            "systolic": 185,
            "diastolic": 105,
            "spo2": 89,
        },
        headers=auth_headers,
    )
    assert r2.status_code == 201
    assert r2.json()["is_simulated"] is True
    assert r2.json()["source"] == "simulated"

    # 3. Ingest watch sensor marked is_simulated=True
    r3 = client.post(
        "/api/v1/vitals/measurement",
        json={
            "cycle_id": cycle.cycle_id,
            "measured_at": (t_base + timedelta(minutes=3)).isoformat(),
            "source": "watch_sensor",
            "pulse": 170,
            "systolic": 180,
            "diastolic": 100,
            "spo2": 90,
            "is_simulated": True,
        },
        headers=auth_headers,
    )
    assert r3.status_code == 201
    assert r3.json()["is_simulated"] is True

    # 4. Ingest genuine live measurement (is_simulated=False)
    r4 = client.post(
        "/api/v1/vitals/measurement",
        json={
            "cycle_id": cycle.cycle_id,
            "measured_at": (t_base + timedelta(minutes=4)).isoformat(),
            "source": "watch_sensor",
            "pulse": 72,
            "systolic": 120,
            "diastolic": 80,
            "spo2": 98,
            "is_simulated": False,
        },
        headers=auth_headers,
    )
    assert r4.status_code == 201
    assert r4.json()["is_simulated"] is False

    meas_repo = HealthMeasurementRepository(db_session)
    all_stored = meas_repo.list_by_cycle(cycle.cycle_id)
    assert len(all_stored) == 4

    # Check domain admissibility property
    admissible_list = [m for m in all_stored if m.is_medically_admissible]
    assert len(admissible_list) == 1
    assert admissible_list[0].pulse == 72

    # Check Primary Consilium vitals stream
    primary_svc = PrimaryConsiliumService(db=db_session)
    primary_stream = primary_svc._load_latest_vitals_stream(
        cycle_id=cycle.cycle_id,
        ecg_summary="Normal sinus",
    )
    assert primary_stream["heart_rate"] == "72 bpm"
    assert primary_stream["blood_pressure"] == "120/80 mmHg"
    assert primary_stream["spo2"] == "98%"
    assert "180" not in primary_stream["heart_rate"]
    assert "190" not in primary_stream["blood_pressure"]

    # Check Weekly Consilium vitals retrieval
    from app.services.weekly_consilium_service import WeeklyConsiliumService
    from app.services.lab_revision_consilium_service import LabRevisionConsiliumService

    weekly_svc = WeeklyConsiliumService(db=db_session)
    weekly_meas = weekly_svc.measurement_repo.list_by_cycle(
        cycle_id=cycle.cycle_id, is_historical=False, is_simulated=False, limit=100
    )
    assert len(weekly_meas) == 1
    assert weekly_meas[0].pulse == 72

    # Check Lab Revision Consilium vitals retrieval
    lab_rev_svc = LabRevisionConsiliumService(db=db_session)
    lab_rev_meas = lab_rev_svc.measurement_repo.list_by_cycle(
        cycle_id=cycle.cycle_id, is_historical=False, is_simulated=False, limit=50
    )
    assert len(lab_rev_meas) == 1
    assert lab_rev_meas[0].pulse == 72

    # Check /api/v1/vitals/latest default (excludes simulated)
    r_latest = client.post(
        f"/api/v1/vitals/latest?cycle_id={cycle.cycle_id}",
        headers=auth_headers,
    )
    # router uses GET for /latest
    r_latest_get = client.get(
        f"/api/v1/vitals/latest?cycle_id={cycle.cycle_id}",
        headers=auth_headers,
    )
    assert r_latest_get.status_code == 200
    d_latest = r_latest_get.json()
    assert d_latest["heart_rate"] == "72 bpm"
    assert d_latest["blood_pressure"] == "120/80 mmHg"


# ------------------------------------------------------------------------------
# 12. LATEST MEASUREMENTS WITH COUNT GREATER THAN LIMIT
# ------------------------------------------------------------------------------

def test_latest_measurements_exceeding_limit_chronological_order(
    client: TestClient,
    db_session: Session,
    auth_headers: dict[str, str],
):
    cycle = _create_active_cycle(db_session, "cycle_limit_exceed")
    t0 = datetime.now(timezone.utc) - timedelta(hours=2)

    # Ingest 70 measurements: pulse from 60 to 129
    items = [
        {
            "measured_at": (t0 + timedelta(minutes=i)).isoformat(),
            "source": "watch_sensor",
            "pulse": 60 + i,
            "systolic": 110 + (i % 20),
            "diastolic": 70,
            "spo2": 98,
        }
        for i in range(70)
    ]

    r_batch = client.post(
        "/api/v1/vitals/batch",
        json={"cycle_id": cycle.cycle_id, "measurements": items},
        headers=auth_headers,
    )
    assert r_batch.status_code == 201
    assert r_batch.json()["recorded_count"] == 70

    meas_repo = HealthMeasurementRepository(db_session)

    # Query with limit=50: MUST return the 50 most recent records (indices 20 to 69)
    # and MUST return them in ascending chronological order!
    limited_meas = meas_repo.list_by_cycle(cycle_id=cycle.cycle_id, limit=50)
    assert len(limited_meas) == 50
    # First item in returned list is index 20 (pulse = 60 + 20 = 80)
    assert limited_meas[0].pulse == 80
    # Last item in returned list is index 69 (pulse = 60 + 69 = 129)
    assert limited_meas[-1].pulse == 129

    # Verify chronological ordering
    for i in range(len(limited_meas) - 1):
        assert limited_meas[i].measured_at <= limited_meas[i + 1].measured_at

    # Verify that reversed(limited_meas) starts with the newest (129 bpm)
    rev_first = next(m for m in reversed(limited_meas))
    assert rev_first.pulse == 129

    # Verify PrimaryConsiliumService gets the latest (129 bpm), NOT the oldest (60 bpm)
    primary_svc = PrimaryConsiliumService(db=db_session)
    stream = primary_svc._load_latest_vitals_stream(cycle_id=cycle.cycle_id, ecg_summary="")
    assert stream["heart_rate"] == "129 bpm"

    # Verify /api/v1/vitals/latest gets the latest (129 bpm)
    r_lat = client.get(f"/api/v1/vitals/latest?cycle_id={cycle.cycle_id}", headers=auth_headers)
    assert r_lat.status_code == 200
    assert r_lat.json()["heart_rate"] == "129 bpm"


# ------------------------------------------------------------------------------
# 13. HISTORICAL DEDUPLICATION & INDEPENDENT LIVE MEASUREMENTS
# ------------------------------------------------------------------------------

def test_historical_deduplication_and_live_independence(
    client: TestClient,
    db_session: Session,
    auth_headers: dict[str, str],
):
    cycle = _create_active_cycle(db_session, "cycle_hist_dedup")
    t0 = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()

    hist_payload = {
        "cycle_id": cycle.cycle_id,
        "measured_at": t0,
        "source": "watch_sensor",
        "pulse": 75,
        "systolic": 120,
        "diastolic": 80,
        "spo2": 98,
        "is_historical": True,
    }

    # 1. Ingest historical measurement
    r1 = client.post("/api/v1/vitals/measurement", json=hist_payload, headers=auth_headers)
    assert r1.status_code == 201
    assert r1.json()["status"] == "recorded"
    assert r1.json()["is_historical"] is True

    # 2. Re-send exact same historical measurement -> must deduplicate
    r2 = client.post("/api/v1/vitals/measurement", json=hist_payload, headers=auth_headers)
    assert r2.status_code == 200
    assert r2.json()["status"] == "deduplicated"
    assert r2.json()["is_historical"] is True

    meas_repo = HealthMeasurementRepository(db_session)
    assert len(meas_repo.list_by_cycle(cycle.cycle_id)) == 1

    # 3. Ingest an independent live measurement with the EXACT same timestamp, source, and values
    # but with is_historical=False
    live_payload = {
        "cycle_id": cycle.cycle_id,
        "measured_at": t0,
        "source": "watch_sensor",
        "pulse": 75,
        "systolic": 120,
        "diastolic": 80,
        "spo2": 98,
        "is_historical": False,
    }
    r3 = client.post("/api/v1/vitals/measurement", json=live_payload, headers=auth_headers)
    # Must NOT be absorbed by historical measurement! Must be recorded as fresh live measurement
    assert r3.status_code == 201
    assert r3.json()["status"] == "recorded"
    assert r3.json()["is_historical"] is False

    # Verify both records exist independently in the DB
    all_recs = meas_repo.list_by_cycle(cycle.cycle_id)
    assert len(all_recs) == 2
    hist_recs = [r for r in all_recs if r.is_historical]
    live_recs = [r for r in all_recs if not r.is_historical]
    assert len(hist_recs) == 1
    assert len(live_recs) == 1

    # 4. Re-send live measurement -> deduplicates against live record without altering is_historical
    r4 = client.post("/api/v1/vitals/measurement", json=live_payload, headers=auth_headers)
    assert r4.status_code == 200
    assert r4.json()["status"] == "deduplicated"
    assert r4.json()["is_historical"] is False

    all_recs_after = meas_repo.list_by_cycle(cycle.cycle_id)
    assert len(all_recs_after) == 2
    assert sum(1 for r in all_recs_after if r.is_historical) == 1

    # 5. Batch envelope as_historical conflict with per-item is_historical
    r_conflict_batch = client.post(
        "/api/v1/vitals/batch",
        json={
            "cycle_id": cycle.cycle_id,
            "as_historical": True,
            "measurements": [
                {
                    "measured_at": (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(),
                    "source": "watch_sensor",
                    "pulse": 70,
                    "is_historical": False,  # CONFLICT with as_historical=True!
                }
            ],
        },
        headers=auth_headers,
    )
    assert r_conflict_batch.status_code in (400, 422), r_conflict_batch.text
    assert "conflicts with batch" in r_conflict_batch.text or "is_historical" in r_conflict_batch.text


# ------------------------------------------------------------------------------
# 14. IDEMPOTENCY KEY CONFLICT SAFETY (RFC 7231 / 409 CONFLICT)
# ------------------------------------------------------------------------------

def test_idempotency_key_identical_payload_delivery(
    client: TestClient,
    db_session: Session,
    auth_headers: dict[str, str],
):
    cycle = _create_active_cycle(db_session, "cycle_idem_ok")
    t0 = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    payload = {
        "cycle_id": cycle.cycle_id,
        "measured_at": t0,
        "source": "watch_sensor",
        "pulse": 72,
        "systolic": 120,
        "diastolic": 80,
        "raw_metadata": {"idempotency_key": "safe_retry_key_01"},
    }

    # First delivery
    r1 = client.post("/api/v1/vitals/measurement", json=payload, headers=auth_headers)
    assert r1.status_code == 201
    assert r1.json()["status"] == "recorded"

    # Second delivery: identical key and identical normalized payload -> deduplicated
    r2 = client.post("/api/v1/vitals/measurement", json=payload, headers=auth_headers)
    assert r2.status_code == 200
    assert r2.json()["status"] == "deduplicated"

    meas_repo = HealthMeasurementRepository(db_session)
    assert len(meas_repo.list_by_cycle(cycle.cycle_id)) == 1


def test_idempotency_key_conflicting_pulse_returns_409(
    client: TestClient,
    db_session: Session,
    auth_headers: dict[str, str],
):
    cycle = _create_active_cycle(db_session, "cycle_idem_pulse_conflict")
    t0 = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()

    p1 = {
        "cycle_id": cycle.cycle_id,
        "measured_at": t0,
        "source": "watch_sensor",
        "pulse": 72,
        "raw_metadata": {"idempotency_key": "conflict_key_pulse"},
    }
    r1 = client.post("/api/v1/vitals/measurement", json=p1, headers=auth_headers)
    assert r1.status_code == 201

    # Same key, but different pulse
    p2 = {
        "cycle_id": cycle.cycle_id,
        "measured_at": t0,
        "source": "watch_sensor",
        "pulse": 95,  # CONFLICT!
        "raw_metadata": {"idempotency_key": "conflict_key_pulse"},
    }
    r2 = client.post("/api/v1/vitals/measurement", json=p2, headers=auth_headers)
    assert r2.status_code == 409, r2.text
    assert "conflicts with existing record" in r2.text or "pulse" in r2.text

    # Verify existing DB record was NOT mutated
    meas_repo = HealthMeasurementRepository(db_session)
    recs = meas_repo.list_by_cycle(cycle.cycle_id)
    assert len(recs) == 1
    assert recs[0].pulse == 72


def test_idempotency_key_conflicting_measured_at_returns_409(
    client: TestClient,
    db_session: Session,
    auth_headers: dict[str, str],
):
    cycle = _create_active_cycle(db_session, "cycle_idem_time_conflict")
    t1 = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    t2 = (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()

    p1 = {
        "cycle_id": cycle.cycle_id,
        "measured_at": t1,
        "source": "watch_sensor",
        "pulse": 70,
        "raw_metadata": {"idempotency_key": "conflict_key_time"},
    }
    r1 = client.post("/api/v1/vitals/measurement", json=p1, headers=auth_headers)
    assert r1.status_code == 201

    # Same key, different measured_at
    p2 = {
        "cycle_id": cycle.cycle_id,
        "measured_at": t2,  # CONFLICT!
        "source": "watch_sensor",
        "pulse": 70,
        "raw_metadata": {"idempotency_key": "conflict_key_time"},
    }
    r2 = client.post("/api/v1/vitals/measurement", json=p2, headers=auth_headers)
    assert r2.status_code == 409, r2.text
    assert "conflicts with existing record" in r2.text or "measured_at" in r2.text


def test_idempotency_key_conflicting_simulated_vs_real_returns_409(
    client: TestClient,
    db_session: Session,
    auth_headers: dict[str, str],
):
    cycle = _create_active_cycle(db_session, "cycle_idem_sim_conflict")
    t0 = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()

    p1 = {
        "cycle_id": cycle.cycle_id,
        "measured_at": t0,
        "source": "watch_sensor",
        "pulse": 80,
        "is_simulated": False,
        "raw_metadata": {"idempotency_key": "conflict_key_sim"},
    }
    r1 = client.post("/api/v1/vitals/measurement", json=p1, headers=auth_headers)
    assert r1.status_code == 201

    # Same key, but test_chip / simulated
    p2 = {
        "cycle_id": cycle.cycle_id,
        "measured_at": t0,
        "source": "test_chip",
        "pulse": 80,
        "is_simulated": True,
        "raw_metadata": {"idempotency_key": "conflict_key_sim"},
    }
    r2 = client.post("/api/v1/vitals/measurement", json=p2, headers=auth_headers)
    assert r2.status_code == 409, r2.text

    # Verify stored record remains real watch_sensor
    meas_repo = HealthMeasurementRepository(db_session)
    recs = meas_repo.list_by_cycle(cycle.cycle_id)
    assert len(recs) == 1
    assert recs[0].source == "watch_sensor"
    assert recs[0].is_simulated is False


def test_batch_internal_idempotency_key_conflict_rollback_409(
    client: TestClient,
    db_session: Session,
    auth_headers: dict[str, str],
):
    cycle = _create_active_cycle(db_session, "cycle_batch_idem_conflict")
    t0 = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()

    # Batch with two items having same idempotency key but different values
    batch_payload = {
        "cycle_id": cycle.cycle_id,
        "measurements": [
            {
                "measured_at": t0,
                "source": "watch_sensor",
                "pulse": 70,
                "raw_metadata": {"idempotency_key": "batch_shared_key"},
            },
            {
                "measured_at": t0,
                "source": "watch_sensor",
                "pulse": 90,  # CONFLICT within same batch!
                "raw_metadata": {"idempotency_key": "batch_shared_key"},
            },
        ],
    }

    r = client.post("/api/v1/vitals/batch", json=batch_payload, headers=auth_headers)
    assert r.status_code == 409, r.text
    assert "conflicting items" in r.text or "idempotency" in r.text

    # Verify atomic rollback: 0 items persisted
    meas_repo = HealthMeasurementRepository(db_session)
    assert len(meas_repo.list_by_cycle(cycle.cycle_id)) == 0


# ------------------------------------------------------------------------------
# 15. LIVE DATA SEMANTICS (GET /vitals/latest SAFE SEMANTICS)
# ------------------------------------------------------------------------------

def test_get_vitals_latest_empty_cycle(
    client: TestClient,
    db_session: Session,
    auth_headers: dict[str, str],
):
    cycle = _create_active_cycle(db_session, "cycle_empty_vitals")

    r = client.get(f"/api/v1/vitals/latest?cycle_id={cycle.cycle_id}", headers=auth_headers)
    assert r.status_code == 200
    data = r.json()

    assert data["cycle_id"] == cycle.cycle_id
    assert data["blood_pressure"] == "--/-- mmHg"
    assert data["heart_rate"] == "-- bpm"
    assert data["spo2"] == "--%"
    assert data["latest_measured_at"] is None
    assert data["source"] is None
    # Crucial: is_live MUST NOT be True when there are no measurements!
    assert data["is_live"] is False
    assert data["recency_status"] == "no_data"


def test_get_vitals_latest_old_non_historical_measurement(
    client: TestClient,
    db_session: Session,
    auth_headers: dict[str, str],
):
    cycle = _create_active_cycle(db_session, "cycle_old_vitals")
    t_old = datetime.now(timezone.utc) - timedelta(days=30)

    # Ingest measurement from 30 days ago with is_historical=False
    r_ingest = client.post(
        "/api/v1/vitals/measurement",
        json={
            "cycle_id": cycle.cycle_id,
            "measured_at": t_old.isoformat(),
            "source": "watch_sensor",
            "pulse": 75,
            "systolic": 120,
            "diastolic": 80,
            "is_historical": False,
        },
        headers=auth_headers,
    )
    assert r_ingest.status_code == 201

    # Query latest endpoint
    r_lat = client.get(f"/api/v1/vitals/latest?cycle_id={cycle.cycle_id}", headers=auth_headers)
    assert r_lat.status_code == 200
    data = r_lat.json()

    # Returns latest values and timestamp
    assert data["heart_rate"] == "75 bpm"
    assert data["blood_pressure"] == "120/80 mmHg"
    assert data["latest_measured_at"] is not None

    # Crucial: Must NOT declare old measurements live purely on is_historical=False.
    # Without an established monitoring policy, is_live is None (null) and recency_status is unknown.
    assert data["is_live"] is None
    assert data["recency_status"] == "unknown"
