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


@pytest.mark.parametrize(
    "forbidden_payload",
    [
        {
            "title": "Summary",
            "summary": "Looks fine",
            "pro_training_shelf": "Hidden syndrome hypothesis",
        },
        {
            "title": "Summary",
            "summary": "Looks fine",
            "flash_training_shelf": "Audit of 8 models",
        },
        {
            "title": "Summary",
            "summary": "Looks fine",
            "system_training_shelf": "Missing lab markers",
        },
        {
            "title": "Summary",
            "summary": "Looks fine",
            "internal_analysis": {"st_segment": "depressed"},
        },
        {
            "title": "Summary",
            "summary": "Looks fine",
            "athena_handoff": "Do not mention arrhythmia",
        },
        {
            "title": "Summary",
            "summary": "Looks fine",
            "unapproved_extra_key": "Arbitrary dict data is forbidden by allowlist",
        },
        {
            "title": "Summary",
            "summary": "Looks fine",
            "metrics": {"nested_clinical_object": {"risk": "high"}},
        },
        {
            "user_workout_report": {
                "summary": "Great workout, pulse well-regulated",
                "internal_analysis": {"ischemia_risk": "low"},
            }
        },
        {
            "user_workout_report": {
                "summary": "Great workout, pulse well-regulated",
                "athena_handoff": "Keep load moderate",
            }
        },
        {
            "user_workout_report": {
                "summary": "Great workout, pulse well-regulated",
                "training_shelf": "Hidden workout audit",
            }
        },
        {
            "user_workout_report": {
                "summary": "Great workout, pulse well-regulated",
            },
            "internal_analysis": {"ischemia_risk": "low"},
        },
    ],
)
def test_client_report_allowlist_strictly_rejects_unapproved_or_nested_fields(
    db_session: Session,
    forbidden_payload: dict,
):
    """
    Verify that client_facing report schemas and repository creation operate on a strict
    allowlist (extra='forbid') and reject any unapproved keys or nested structures in metrics.
    """
    from datetime import datetime, timezone
    from pydantic import ValidationError

    # 1. Direct schema rejection
    with pytest.raises(ValidationError):
        ClientModelReportResponse(
            report_id="rep_allowlist_check",
            cycle_id="cycle_allowlist",
            model="gemini-3.1-pro",
            report_type="pro_general_shelf",
            privacy_level=PrivacyLevel.CLIENT_FACING,
            version=1,
            content=forbidden_payload,
            created_at=datetime.now(timezone.utc),
        )

    # 2. Repository creation rejection for client_facing reports
    cycle_repo = MedicalCycleRepository(db_session)
    report_repo = ModelReportRepository(db_session)
    cycle = cycle_repo.get("cycle_allowlist_repo") or cycle_repo.create(cycle_id="cycle_allowlist_repo")

    with pytest.raises(ValidationError):
        report_repo.create(
            report_id="rep_bad_client_content",
            cycle_id=cycle.cycle_id,
            model="gemini-3.1-pro",
            report_type="pro_general_shelf",
            privacy_level=PrivacyLevel.CLIENT_FACING,
            content=forbidden_payload,
        )


def test_client_report_allowlist_accepts_dated_organ_entries(db_session: Session):
    """
    Verify that approved user-facing organ system entries (including friendly no-change updates)
    pass allowlist validation cleanly.
    """
    cycle_repo = MedicalCycleRepository(db_session)
    report_repo = ModelReportRepository(db_session)
    cycle = cycle_repo.create(cycle_id="cycle_organ_allowlist")

    rep = report_repo.create(
        report_id="rep_cvs_user_v1",
        cycle_id=cycle.cycle_id,
        model="gemini-3.6-flash",
        report_type="system_general_shelf",
        privacy_level=PrivacyLevel.CLIENT_FACING,
        content={
            "title": "Cardiovascular System",
            "summary": "Indicators remain stable this week.",
            "system_key": "cvs",
            "report_date": "2026-10-18",
            "has_changes": False,
            "entries": [
                {
                    "date": "2026-10-11",
                    "summary": "Initial cardiovascular wellness overview.",
                    "has_changes": True,
                },
                {
                    "date": "2026-10-18",
                    "summary": "Over the past week, no new changes were detected in this system.",
                    "has_changes": False,
                },
            ],
        },
    )

    validated = ClientModelReportResponse.model_validate(rep)
    assert validated.content.system_key == "cvs"
    assert len(validated.content.entries) == 2
    assert validated.content.entries[1].has_changes is False


def test_workout_intelligence_and_all_three_client_report_formats_end_to_end(
    client: TestClient,
    db_session: Session,
    auth_headers: dict[str, str],
):
    """
    End-to-end integration test verifying backward compatibility of all 3 existing client_facing
    report formats (full flat, minimal flat, and nested Workout Intelligence user_workout_report):
    creation -> SQLite persistence -> GET /api/v1/reports/client -> exact user content check,
    plus strict isolation of internal_analysis and athena_handoff.
    """
    cycle_repo = MedicalCycleRepository(db_session)
    report_repo = ModelReportRepository(db_session)
    cycle = cycle_repo.create(cycle_id="cycle_workout_e2e")

    # 1. Server-only Workout Intelligence report (must NEVER leak via client endpoint)
    report_repo.create(
        report_id="rep_workout_internal_e2e",
        cycle_id=cycle.cycle_id,
        model="gemini-3.6-flash",
        report_type="workout_intelligence_internal",
        privacy_level=PrivacyLevel.SERVER_ONLY,
        content={
            "internal_analysis": {"ischemia_risk": "low", "zone_distrib": "aerobic"},
            "athena_handoff": {"directive": "Praise recovery pace, avoid cardiac jargon"},
        },
    )

    # 2. Format A: Nested Workout Intelligence user report (from test_models.py)
    report_repo.create(
        report_id="rep_workout_user_e2e",
        cycle_id=cycle.cycle_id,
        model="gemini-3.6-flash",
        report_type="workout_intelligence_user",
        privacy_level=PrivacyLevel.CLIENT_FACING,
        content={
            "user_workout_report": {
                "summary": "Great workout, pulse well-regulated",
            }
        },
    )

    # 3. Format B: Minimal flat user report (from test_cycle_retention.py)
    report_repo.create(
        report_id="rep_minimal_flat_e2e",
        cycle_id=cycle.cycle_id,
        model="gemini-3.1-pro",
        report_type="user_workout_report",
        privacy_level=PrivacyLevel.CLIENT_FACING,
        content={"summary": "End of cycle summary"},
    )

    # 4. Format C: Full flat user report (from test_privacy_isolation.py)
    report_repo.create(
        report_id="rep_full_flat_e2e",
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

    # 5. Fetch via GET /api/v1/reports/client
    resp = client.get(
        f"/api/v1/reports/client?cycle_id={cycle.cycle_id}",
        headers=auth_headers,
    )
    assert resp.status_code == 200
    items = resp.json()
    assert len(items) == 3

    by_id = {item["report_id"]: item for item in items}
    assert "rep_workout_internal_e2e" not in by_id
    assert "internal_analysis" not in str(items)
    assert "athena_handoff" not in str(items)

    # Verify exact content of Format A (Workout Intelligence nested format)
    workout_item = by_id["rep_workout_user_e2e"]
    assert workout_item["content"]["user_workout_report"]["summary"] == "Great workout, pulse well-regulated"
    assert workout_item["content"]["summary"] == "Great workout, pulse well-regulated"
    assert workout_item["content"]["title"] == "Workout Summary"

    # Verify exact content of Format B (Minimal flat format)
    minimal_item = by_id["rep_minimal_flat_e2e"]
    assert minimal_item["content"]["summary"] == "End of cycle summary"
    assert minimal_item["content"]["title"] == "Aegis Wellness Summary"

    # Verify exact content of Format C (Full flat format)
    full_item = by_id["rep_full_flat_e2e"]
    assert full_item["content"]["title"] == "Weekly Strategy"
    assert full_item["content"]["summary"] == "Cardiac rhythm stable during rest"
    assert full_item["content"]["highlights"] == ["Walk 30 mins daily", "Stay hydrated"]
    assert full_item["content"]["metrics"] == {"avg_resting_hr": 68}

    # 6. Verify direct single-report endpoint for Workout Intelligence user report vs internal report
    resp_single_user = client.get(
        "/api/v1/reports/client/rep_workout_user_e2e",
        headers=auth_headers,
    )
    assert resp_single_user.status_code == 200
    assert (
        resp_single_user.json()["content"]["user_workout_report"]["summary"]
        == "Great workout, pulse well-regulated"
    )

    resp_single_internal = client.get(
        "/api/v1/reports/client/rep_workout_internal_e2e",
        headers=auth_headers,
    )
    assert resp_single_internal.status_code == 403
