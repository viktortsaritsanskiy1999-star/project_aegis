import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from app.models import MedicalCycle, ModelReport
from app.repositories import ModelReportRepository, MedicalCycleRepository
from app.schemas.privacy import PrivacyLevel
from app.schemas.reports import ClientModelReportResponse


def test_server_only_report_is_strictly_excluded_from_client_response(
    client: TestClient,
    db_session: Session,
    auth_headers: dict[str, str],
):
    """
    Ensure that:
    1. Querying client reports via GET /api/v1/reports/client strictly excludes server_only reports.
    2. Directly requesting a server_only report via client API is rejected with 403 Forbidden.
    """
    cycle_repo = MedicalCycleRepository(db_session)
    report_repo = ModelReportRepository(db_session)

    cycle = cycle_repo.create(cycle_id="cycle_privacy_eval")

    # 1. Create a server_only clinical reasoning report
    report_repo.create(
        report_id="rep_server_secret_01",
        cycle_id=cycle.cycle_id,
        model="gemini-3.1-pro",
        report_type="pro_training_shelf",
        privacy_level=PrivacyLevel.SERVER_ONLY,
        content={"clinical_hypotheses": ["ischemia risk", "st_depression_observed"]},
    )

    # 2. Create a client_facing wellness summary report
    report_repo.create(
        report_id="rep_client_public_01",
        cycle_id=cycle.cycle_id,
        model="gemini-3.1-pro",
        report_type="pro_general_shelf",
        privacy_level=PrivacyLevel.CLIENT_FACING,
        content={
            "title": "Weekly Strategy",
            "summary": "Cardiac rhythm stable during rest",
            "highlights": ["Walk 30 mins daily", "Stay hydrated"],
            "metrics": {"avg_resting_hr": 68},
        },
    )

    # 3. Request client reports list
    resp = client.get(
        f"/api/v1/reports/client?cycle_id={cycle.cycle_id}",
        headers=auth_headers,
    )
    assert resp.status_code == 200
    reports = resp.json()

    # Verify only the client_facing report is returned
    assert len(reports) == 1
    assert reports[0]["report_id"] == "rep_client_public_01"
    assert reports[0]["privacy_level"] == "client_facing"
    assert "clinical_hypotheses" not in str(reports)

    # 4. Attempt direct access to server_only report by ID via client endpoint -> 403 Forbidden!
    resp_direct_secret = client.get(
        "/api/v1/reports/client/rep_server_secret_01",
        headers=auth_headers,
    )
    assert resp_direct_secret.status_code == 403
    assert "Security Violation" in resp_direct_secret.json()["detail"]


def test_unknown_privacy_level_is_strictly_rejected(db_session: Session):
    """
    Verify that any unknown or invalid privacy_level is rejected with ValueError
    both at the enum parser level and in repository creation.
    """
    # 1. Enum parser rejection
    with pytest.raises(ValueError) as excinfo:
        PrivacyLevel.from_str("confidential")
    assert "Invalid privacy level 'confidential'" in str(excinfo.value)

    with pytest.raises(ValueError):
        PrivacyLevel.from_str("public")

    # 2. Repository level rejection
    cycle_repo = MedicalCycleRepository(db_session)
    report_repo = ModelReportRepository(db_session)
    cycle = cycle_repo.create(cycle_id="cycle_unknown_privacy")

    with pytest.raises(ValueError):
        report_repo.create(
            report_id="rep_invalid_privacy",
            cycle_id=cycle.cycle_id,
            model="gemini",
            report_type="test",
            privacy_level="unrecognized_level",
            content={"test": 123},
        )


def test_client_schema_validator_rejects_server_only_model():
    """
    Verify that ClientModelReportResponse Pydantic validator refuses to serialize
    any report marked as server_only.
    """
    from datetime import datetime, timezone

    with pytest.raises(ValueError) as excinfo:
        ClientModelReportResponse(
            report_id="rep_leak_attempt",
            cycle_id="cycle_123",
            model="gemini",
            report_type="internal",
            privacy_level=PrivacyLevel.SERVER_ONLY,
            version=1,
            content={"title": "Test", "summary": "Leak"},
            created_at=datetime.now(timezone.utc),
        )
    assert "Security Violation" in str(excinfo.value)
