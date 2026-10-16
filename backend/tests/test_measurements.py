from datetime import datetime, timezone, timedelta
from sqlalchemy.orm import Session
from app.models import HealthMeasurement, MedicalCycle


def test_health_measurement_sources_and_timestamp_preservation(db_session: Session):
    """
    Test storing HealthMeasurements with distinct sources (manual_input, test_chip,
    simulated, watch_sensor, unknown) and preserving measured_at vs ingested_at.
    """
    past_measurement_time = datetime(2026, 10, 15, 14, 30, 0, tzinfo=timezone.utc)
    current_time = datetime.now(timezone.utc)

    cycle = MedicalCycle(cycle_id="cycle_meas_test", status="active", started_at=current_time)
    db_session.add(cycle)
    db_session.commit()

    # 1. Manual input with past measurement timestamp
    m_manual = HealthMeasurement(
        cycle_id=cycle.cycle_id,
        pulse=76,
        systolic=120,
        diastolic=80,
        source="manual_input",
        is_simulated=False,
        is_historical=True,
        measured_at=past_measurement_time,
        ingested_at=current_time,
        phase="historical",
        note="User entered from paper diary",
    )

    # 2. Watch sensor live reading
    m_sensor = HealthMeasurement(
        cycle_id=cycle.cycle_id,
        pulse=72,
        spo2=99,
        source="watch_sensor",
        is_simulated=False,
        is_historical=False,
        measured_at=current_time,
        ingested_at=current_time,
        phase="standby",
        note="Live watch sensor",
    )

    # 3. Test chip
    m_chip = HealthMeasurement(
        cycle_id=cycle.cycle_id,
        pulse=142,
        source="test_chip",
        is_simulated=True,
        is_historical=False,
        measured_at=current_time,
        ingested_at=current_time,
        phase="workout",
        note="Debug chip tap",
    )

    # 4. Unknown source fallback
    m_unknown = HealthMeasurement(
        cycle_id=cycle.cycle_id,
        pulse=65,
        source="unknown",
        is_simulated=False,
        is_historical=False,
        measured_at=current_time,
        ingested_at=current_time,
        phase="standby",
        note="Unrecognized device protocol",
    )

    db_session.add_all([m_manual, m_sensor, m_chip, m_unknown])
    db_session.commit()

    records = db_session.query(HealthMeasurement).all()
    assert len(records) == 4

    # Verify manual measurement preserved exact past timestamp
    manual_rec = db_session.query(HealthMeasurement).filter_by(source="manual_input").one()
    assert manual_rec.measured_at == past_measurement_time.replace(tzinfo=None)
    assert manual_rec.is_historical is True
    assert manual_rec.is_simulated is False
    assert manual_rec.measured_at != manual_rec.ingested_at

    # Verify test chip has is_simulated=True
    chip_rec = db_session.query(HealthMeasurement).filter_by(source="test_chip").one()
    assert chip_rec.is_simulated is True

    # Verify unknown source is stored as unknown, NOT defaulted to watch_sensor
    unknown_rec = db_session.query(HealthMeasurement).filter_by(source="unknown").one()
    assert unknown_rec.source == "unknown"
    assert unknown_rec.source != "watch_sensor"
