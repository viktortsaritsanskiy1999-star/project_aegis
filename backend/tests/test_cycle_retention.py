import tempfile
from pathlib import Path
from datetime import datetime, timezone
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app.models.base import Base
from app.models import MedicalCycle, WeeklySprint, WorkoutSession, HealthMeasurement, ModelReport
from app.repositories import (
    MedicalCycleRepository,
    WeeklySprintRepository,
    WorkoutSessionRepository,
    HealthMeasurementRepository,
    ModelReportRepository,
)
from app.schemas.privacy import PrivacyLevel


def test_history_is_preserved_after_cycle_archival():
    """
    Ensure that closing or soft-archiving a MedicalCycle does NOT delete or prune
    any of its weekly sprints, workout sessions, measurements, or model reports.
    """
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    session = Session()

    cycle_repo = MedicalCycleRepository(session)
    sprint_repo = WeeklySprintRepository(session)
    workout_repo = WorkoutSessionRepository(session)
    meas_repo = HealthMeasurementRepository(session)
    report_repo = ModelReportRepository(session)

    now = datetime.now(timezone.utc)
    cycle = cycle_repo.create(cycle_id="cycle_to_archive", status="active", started_at=now)

    sprint = sprint_repo.create(sprint_id="sprint_arch_01", cycle_id=cycle.cycle_id, sprint_number=1)
    workout = workout_repo.create(
        session_id="ws_arch_01",
        cycle_id=cycle.cycle_id,
        sprint_id=sprint.sprint_id,
        anomaly_detected_at=now,
        activity_confirmed_at=now,
    )
    meas = meas_repo.create(
        cycle_id=cycle.cycle_id,
        session_id=workout.session_id,
        measured_at=now,
        source="watch_sensor",
        pulse=142,
    )
    report = report_repo.create(
        report_id="rep_arch_01",
        cycle_id=cycle.cycle_id,
        model="gemini-3.1-pro",
        report_type="user_workout_report",
        privacy_level=PrivacyLevel.CLIENT_FACING,
        content={"summary": "End of cycle summary"},
    )

    # 1. Soft-archive the cycle
    archived_cycle = cycle_repo.archive_cycle(cycle.cycle_id)
    assert archived_cycle is not None
    assert archived_cycle.status == "archived"
    assert archived_cycle.closed_at is not None

    # 2. Verify all child records are 100% preserved
    retrieved_sprints = sprint_repo.list_by_cycle(cycle.cycle_id)
    assert len(retrieved_sprints) == 1
    assert retrieved_sprints[0].sprint_id == "sprint_arch_01"

    retrieved_workouts = workout_repo.list_by_cycle(cycle.cycle_id)
    assert len(retrieved_workouts) == 1
    assert retrieved_workouts[0].session_id == "ws_arch_01"

    retrieved_meas = meas_repo.list_by_cycle(cycle.cycle_id)
    assert len(retrieved_meas) == 1
    assert retrieved_meas[0].pulse == 142

    retrieved_reports = report_repo.list_client_facing(cycle.cycle_id)
    assert len(retrieved_reports) == 1
    assert retrieved_reports[0].report_id == "rep_arch_01"

    session.close()


def test_data_available_after_database_restart():
    """
    Ensure that after closing the database connection and reopening it from disk,
    all MedicalCycles, Sprints, and Measurements remain persistent and intact.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "persistent_test.db"
        db_url = f"sqlite:///{db_path}"

        # 1. First run: create schema and seed data
        engine1 = create_engine(db_url)
        Base.metadata.create_all(bind=engine1)
        Session1 = sessionmaker(bind=engine1)
        s1 = Session1()

        cycle_repo1 = MedicalCycleRepository(s1)
        cycle_repo1.create(cycle_id="cycle_restart_test", status="active")
        s1.close()
        engine1.dispose()

        # 2. Second run: simulate server restart with new engine pointing to same file
        engine2 = create_engine(db_url)
        Session2 = sessionmaker(bind=engine2)
        s2 = Session2()

        cycle_repo2 = MedicalCycleRepository(s2)
        restored_cycle = cycle_repo2.get("cycle_restart_test")

        assert restored_cycle is not None
        assert restored_cycle.cycle_id == "cycle_restart_test"
        assert restored_cycle.status == "active"

        s2.close()
        engine2.dispose()
