from datetime import datetime, timezone
from sqlalchemy.orm import Session
from app.models import MedicalCycle, WeeklySprint, WorkoutSession, ModelReport


def test_medical_cycle_sprints_and_workouts_relationships(db_session: Session):
    """
    Test creating a MedicalCycle with multiple WeeklySprints and WorkoutSessions.
    Verifies that completing a sprint does not close the enclosing MedicalCycle.
    """
    now = datetime.now(timezone.utc)

    # 1. Create a MedicalCycle
    cycle = MedicalCycle(
        cycle_id="cycle_20261016_test01",
        status="active",
        started_at=now,
    )
    db_session.add(cycle)
    db_session.commit()

    # 2. Add multiple WeeklySprints inside the same MedicalCycle
    sprint1 = WeeklySprint(
        sprint_id="sprint_test01_w1",
        cycle_id=cycle.cycle_id,
        sprint_number=1,
        status="completed",
        started_at=now,
        completed_at=now,
    )
    sprint2 = WeeklySprint(
        sprint_id="sprint_test01_w2",
        cycle_id=cycle.cycle_id,
        sprint_number=2,
        status="active",
        started_at=now,
    )
    db_session.add_all([sprint1, sprint2])
    db_session.commit()

    # 3. Add WorkoutSession attached to Sprint 1 and Cycle
    workout = WorkoutSession(
        session_id="ws_1792150000000",
        cycle_id=cycle.cycle_id,
        sprint_id=sprint1.sprint_id,
        anomaly_detected_at=now,
        activity_confirmed_at=now,
        workout_completed_at=now,
        status="completed",
        is_data_collection_finished=True,
    )
    db_session.add(workout)
    db_session.commit()

    # 4. Verify relationships
    retrieved_cycle = db_session.query(MedicalCycle).filter_by(cycle_id="cycle_20261016_test01").one()
    assert len(retrieved_cycle.sprints) == 2
    assert retrieved_cycle.sprints[0].sprint_number == 1
    assert retrieved_cycle.sprints[0].status == "completed"
    assert retrieved_cycle.sprints[1].sprint_number == 2
    assert retrieved_cycle.sprints[1].status == "active"

    # Enclosing MedicalCycle remains active even when Sprint 1 is completed!
    assert retrieved_cycle.status == "active"
    assert retrieved_cycle.closed_at is None

    assert len(retrieved_cycle.workouts) == 1
    assert retrieved_cycle.workouts[0].session_id == "ws_1792150000000"
    assert retrieved_cycle.workouts[0].sprint.sprint_id == "sprint_test01_w1"


def test_model_report_privacy_partitioning(db_session: Session):
    """
    Test storing ModelReports with explicit privacy levels:
    - server_only: internal_analysis, system_training_shelf, pro_training_shelf, athena_handoff
    - client_facing: system_general_shelf, pro_general_shelf, user_workout_report
    """
    now = datetime.now(timezone.utc)
    cycle = MedicalCycle(cycle_id="cycle_privacy_test", status="active", started_at=now)
    db_session.add(cycle)
    db_session.commit()

    # Server-only clinical report
    server_report = ModelReport(
        report_id="rep_server_01",
        cycle_id=cycle.cycle_id,
        model="gemini-3.1-pro",
        report_type="workout_intelligence_internal",
        privacy_level="server_only",
        version=1,
        content={"internal_analysis": {"ischemia_risk": "low", "zone_distrib": "aerobic"}},
    )

    # Client-facing wellness summary
    client_report = ModelReport(
        report_id="rep_client_01",
        cycle_id=cycle.cycle_id,
        model="gemini-3.1-pro",
        report_type="workout_intelligence_user",
        privacy_level="client_facing",
        version=1,
        content={"user_workout_report": {"summary": "Great workout, pulse well-regulated"}},
    )

    db_session.add_all([server_report, client_report])
    db_session.commit()

    server_reports = db_session.query(ModelReport).filter_by(privacy_level="server_only").all()
    client_reports = db_session.query(ModelReport).filter_by(privacy_level="client_facing").all()

    assert len(server_reports) == 1
    assert server_reports[0].report_id == "rep_server_01"
    assert "internal_analysis" in server_reports[0].content

    assert len(client_reports) == 1
    assert client_reports[0].report_id == "rep_client_01"
    assert "user_workout_report" in client_reports[0].content

    from app.schemas.reports import ClientModelReportResponse

    validated_client = ClientModelReportResponse.model_validate(client_reports[0])
    assert validated_client.content.user_workout_report is not None
    assert validated_client.content.user_workout_report.summary == "Great workout, pulse well-regulated"
    assert validated_client.content.summary == "Great workout, pulse well-regulated"
