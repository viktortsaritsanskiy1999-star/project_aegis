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


def test_model_report_versioning_preserves_history_and_prevents_id_collisions():
    """
    Verifies that:
    1. create_next_version() increments version numbers (v1, v2, v3) sequentially
       and preserves all previous report versions intact without overwriting.
    2. Attempting to create a report with a duplicate report_id raises ValueError
       instead of silently overwriting or dropping the new report.
    """
    import pytest

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    session = Session()

    try:
        cycle_repo = MedicalCycleRepository(session)
        report_repo = ModelReportRepository(session)
        cycle = cycle_repo.create(cycle_id="cycle_ver_test", status="active")

        v1 = report_repo.create_next_version(
            cycle_id=cycle.cycle_id,
            model="gemini-3.1-pro-preview",
            report_type="pro_general_shelf",
            privacy_level=PrivacyLevel.CLIENT_FACING,
            content={"title": "Initial Plan", "summary": "Week 1 baseline plan"},
        )
        v2 = report_repo.create_next_version(
            cycle_id=cycle.cycle_id,
            model="gemini-3.1-pro-preview",
            report_type="pro_general_shelf",
            privacy_level=PrivacyLevel.CLIENT_FACING,
            content={"title": "Week 2 Update", "summary": "Week 2 adjusted plan"},
        )
        v3 = report_repo.create_next_version(
            cycle_id=cycle.cycle_id,
            model="gemini-3.1-pro-preview",
            report_type="pro_general_shelf",
            privacy_level=PrivacyLevel.CLIENT_FACING,
            content={"title": "Lab Revision", "summary": "Updated after lab review"},
        )

        assert v1.version == 1
        assert v2.version == 2
        assert v3.version == 3
        assert len({v1.report_id, v2.report_id, v3.report_id}) == 3

        history = report_repo.list_versions(
            cycle_id=cycle.cycle_id,
            report_type="pro_general_shelf",
            privacy_level=PrivacyLevel.CLIENT_FACING,
        )
        assert [r.version for r in history] == [1, 2, 3]
        assert history[0].content["summary"] == "Week 1 baseline plan"
        assert history[1].content["summary"] == "Week 2 adjusted plan"
        assert history[2].content["summary"] == "Updated after lab review"

        # Attempting to reuse an existing report_id must raise ValueError
        with pytest.raises(ValueError) as excinfo:
            report_repo.create(
                report_id=v1.report_id,
                cycle_id=cycle.cycle_id,
                model="gemini-3.1-pro-preview",
                report_type="pro_general_shelf",
                privacy_level=PrivacyLevel.CLIENT_FACING,
                content={"title": "Overwrite attempt", "summary": "Should be rejected"},
            )
        assert "Report ID collision" in str(excinfo.value)

        with pytest.raises(ValueError):
            report_repo.create_next_version(
                cycle_id=cycle.cycle_id,
                model="gemini-3.1-pro-preview",
                report_type="pro_general_shelf",
                privacy_level=PrivacyLevel.CLIENT_FACING,
                report_id=v2.report_id,
                content={"title": "Overwrite attempt 2", "summary": "Should be rejected"},
            )
    finally:
        session.close()
        engine.dispose()
