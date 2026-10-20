"""
PROJECT AEGIS — C-4 Exact Evaluated Ledger Version Test Suite.

Verifies:
1. Primary Consilium records exact `evaluated_ledger_version` and `is_synthetic_fallback`
   in `chief_physician_internal`.
2. Synthetic OCR fallback Primary records strictly `evaluated_ledger_version = 0`.
3. Authoritative ledger Primary records exact snapshot version (e.g., 1).
4. Concurrent ledger updates during Primary or Lab Revision remain unreviewed.
5. Failed consiliums (quality gate rejection, gateway error, rollback) never mark
   unverified ledger versions as evaluated.
6. Legacy historical reports without `evaluated_ledger_version` safely return 0.
7. Weekly Consilium handles safe indeterminate state (evaluated_ver=0, ledger has markers):
   `shared_labs_shelf = {}`, `unreviewed_pending_labs = all`, `has_unreviewed_lab_changes = True`.
8. Strict ledger version lookup: requesting missing v2 when v3 exists raises LaboratoryLedgerVersionNotFoundError
   and NEVER falls back to OCR.
9. Weekly Consilium handles missing evaluated ledger version safely without crashing or marking unreviewed as evaluated.
10. Later legacy report after confirmed evaluation v2 does NOT destroy earlier confirmed evaluation v2.
11. Nonexistent evaluated_ledger_version=999 is rejected.
12. Unsuccessful or unconfirmed consilium reports are rejected.
13. Complete multi-step lifecycle: Primary -> new labs -> Weekly -> Lab Revision -> Weekly.
"""

from datetime import datetime, timezone
from typing import Any, Dict, List
import pytest
from sqlalchemy.orm import Session

from app.models.model_report import ModelReport
from app.repositories.cycle_repository import MedicalCycleRepository
from app.repositories.measurement_repository import HealthMeasurementRepository
from app.repositories.report_repository import ModelReportRepository
from app.schemas.consilium import (
    SYSTEM_KEYS,
    AegisAgentRole,
    LabEventType,
    LabReferenceRange,
    LabMarkerMutationItem,
)
from app.schemas.privacy import PrivacyLevel
from app.services.lab_context_service import (
    assemble_consilium_laboratory_context,
    get_last_evaluated_ledger_version,
    LaboratoryLedgerVersionNotFoundError,
)
from app.services.primary_consilium_service import (
    PrimaryConsiliumService,
    ConsiliumQualityRejectionError,
    PREPARATORY_ANAMNESIS_REPORT_TYPE,
    PREPARATORY_OCR_REPORT_TYPE,
    PREPARATORY_ECG_REPORT_TYPE,
)
from app.services.weekly_consilium_service import WeeklyConsiliumService
from app.services.lab_revision_consilium_service import (
    LabRevisionConsiliumService,
    LABORATORY_LEDGER_REPORT_TYPE,
    LAB_REVISION_EXECUTION_LOG_REPORT_TYPE,
)
from tests.test_primary_consilium import (
    _seed_preparatory_cycle_context,
    _build_consilium_mock_gateway,
    _sample_pro_output,
)
from tests.test_lab_revision_consilium import (
    _build_lab_revision_mock_gateway,
    _sample_lab_revision_pro_output,
)
from tests.test_weekly_consilium import (
    _seed_seven_day_athena_logs,
    _build_weekly_mock_gateway,
)


@pytest.mark.anyio
async def test_primary_consilium_synthetic_ocr_fallback_records_zero(db_session: Session):
    """
    C-4 Requirement 1:
    When Primary Consilium runs on synthetic OCR fallback (no authoritative ledger),
    it must record evaluated_ledger_version=0 and is_synthetic_fallback=True.
    get_last_evaluated_ledger_version must return 0 (never claim v1).
    """
    cycle_id = "cycle_c4_synth_fallback"
    _seed_preparatory_cycle_context(db_session, cycle_id=cycle_id, seed_ledger=False)

    recorded_calls: List[Dict[str, Any]] = []
    gateway = _build_consilium_mock_gateway(recorded_calls)
    service = PrimaryConsiliumService(db=db_session, gateway=gateway)
    await service.run_primary_consilium(cycle_id=cycle_id, report_date="2026-10-18")

    report_repo = ModelReportRepository(db_session)
    chief_reports = report_repo.list_server_only(
        cycle_id=cycle_id,
        report_type="chief_physician_internal",
    )
    assert len(chief_reports) == 1
    content = chief_reports[0].content or {}

    assert content.get("evaluated_ledger_version") == 0
    assert content.get("is_synthetic_fallback") is True

    # Evaluated version helper must return 0
    last_eval = get_last_evaluated_ledger_version(db_session, cycle_id)
    assert last_eval == 0


@pytest.mark.anyio
async def test_primary_consilium_authoritative_ledger_records_exact_version(db_session: Session):
    """
    C-4 Requirement 1:
    When Primary Consilium runs with an authoritative ledger v1,
    it records evaluated_ledger_version=1 and is_synthetic_fallback=False.
    """
    cycle_id = "cycle_c4_authoritative_v1"
    _seed_preparatory_cycle_context(db_session, cycle_id=cycle_id, seed_ledger=True)

    recorded_calls: List[Dict[str, Any]] = []
    gateway = _build_consilium_mock_gateway(recorded_calls)
    service = PrimaryConsiliumService(db=db_session, gateway=gateway)
    await service.run_primary_consilium(cycle_id=cycle_id, report_date="2026-10-18")

    report_repo = ModelReportRepository(db_session)
    chief_reports = report_repo.list_server_only(
        cycle_id=cycle_id,
        report_type="chief_physician_internal",
    )
    assert len(chief_reports) == 1
    content = chief_reports[0].content or {}

    assert content.get("evaluated_ledger_version") == 1
    assert content.get("is_synthetic_fallback") is False

    last_eval = get_last_evaluated_ledger_version(db_session, cycle_id)
    assert last_eval == 1


@pytest.mark.anyio
async def test_concurrent_ledger_update_during_primary_remains_unreviewed(db_session: Session):
    """
    C-4 Requirements 1 & 5:
    If a concurrent ledger version (v2) is added while Primary is running on snapshot v1,
    Primary evaluates only v1, and v2 remains unreviewed.
    """
    cycle_id = "cycle_c4_concurrent_primary"
    _seed_preparatory_cycle_context(db_session, cycle_id=cycle_id, seed_ledger=True)

    recorded_calls: List[Dict[str, Any]] = []
    gateway = _build_consilium_mock_gateway(recorded_calls)
    service = PrimaryConsiliumService(db=db_session, gateway=gateway)

    # Simulate concurrent ledger update occurring during AI inference
    original_generate_structured = gateway.generate_structured

    async def concurrent_ledger_mutator(*args, **kwargs):
        # On the 5th model call, inject ledger v2
        if len(recorded_calls) == 4:
            lab_service = LabRevisionConsiliumService(db=db_session)
            lab_service.upsert_laboratory_markers(
                cycle_id=cycle_id,
                mutations=[
                    LabMarkerMutationItem(
                        marker_key="hba1c",
                        display_name="Glycated Hemoglobin",
                        value="5.6 %",
                        event_type=LabEventType.INITIAL,
                        sample_date="2026-10-18",
                    )
                ],
            )
        return await original_generate_structured(*args, **kwargs)

    gateway.generate_structured = concurrent_ledger_mutator  # type: ignore[method-assign]

    await service.run_primary_consilium(cycle_id=cycle_id, report_date="2026-10-18")

    # Primary evaluated snapshot v1
    last_eval = get_last_evaluated_ledger_version(db_session, cycle_id)
    assert last_eval == 1

    # Ledger state confirms ledger is v2 and has unreviewed changes
    lab_service = LabRevisionConsiliumService(db=db_session)
    ledger_state = lab_service.get_lab_ledger_state(cycle_id)
    assert ledger_state.ledger_version == 2
    assert ledger_state.last_evaluated_ledger_version == 1
    assert ledger_state.has_unreviewed_lab_changes is True
    assert "hba1c" in ledger_state.active_markers


@pytest.mark.anyio
async def test_concurrent_ledger_update_during_lab_revision_remains_unreviewed(db_session: Session):
    """
    C-4 Requirements 2 & 5:
    When Lab Revision evaluates snapshot v2, if a concurrent ledger version (v3) arrives,
    the execution log records evaluated_ledger_version=2, and v3 remains unreviewed.
    """
    cycle_id = "cycle_c4_concurrent_lab_rev"
    _seed_preparatory_cycle_context(db_session, cycle_id=cycle_id, seed_ledger=True)

    # 1. Run baseline Primary (evaluated v1)
    primary_calls: List[Dict[str, Any]] = []
    primary_gateway = _build_consilium_mock_gateway(primary_calls)
    primary_service = PrimaryConsiliumService(db=db_session, gateway=primary_gateway)
    await primary_service.run_primary_consilium(cycle_id=cycle_id, report_date="2026-10-18")

    # 2. Add legitimate mutations -> ledger v2
    lab_service = LabRevisionConsiliumService(
        db=db_session,
        now_provider=lambda: datetime(2026, 10, 20, 10, 0, 0, tzinfo=timezone.utc),
    )
    lab_service.upsert_laboratory_markers(
        cycle_id=cycle_id,
        mutations=[
            LabMarkerMutationItem(
                marker_key="ferritin",
                display_name="Serum Ferritin",
                value="28.5 ng/mL",
                event_type=LabEventType.CORRECTION,
                sample_date="2026-10-18",
            )
        ],
    )

    state_before_revision = lab_service.get_lab_ledger_state(cycle_id)
    assert state_before_revision.ledger_version == 2
    assert state_before_revision.last_evaluated_ledger_version == 1
    assert state_before_revision.has_unreviewed_lab_changes is True

    # 3. Run Lab Revision Consilium with concurrent mutation injected during Pro execution
    rev_calls: List[Dict[str, Any]] = []
    rev_gateway = _build_lab_revision_mock_gateway(rev_calls)
    original_rev_generate = rev_gateway.generate_structured

    async def concurrent_v3_mutator(*args, **kwargs):
        # When Pro runs (call index 9), inject ledger v3
        if len(rev_calls) == 9:
            lab_service.upsert_laboratory_markers(
                cycle_id=cycle_id,
                mutations=[
                    LabMarkerMutationItem(
                        marker_key="crp",
                        display_name="C-Reactive Protein",
                        value="1.2 mg/L",
                        event_type=LabEventType.INITIAL,
                        sample_date="2026-10-20",
                    )
                ],
            )
        return await original_rev_generate(*args, **kwargs)

    rev_gateway.generate_structured = concurrent_v3_mutator  # type: ignore[method-assign]
    revision_service = LabRevisionConsiliumService(
        db=db_session,
        gateway=rev_gateway,
        now_provider=lambda: datetime(2026, 10, 20, 11, 0, 0, tzinfo=timezone.utc),
    )
    await revision_service.run_lab_revision_consilium(cycle_id=cycle_id)

    # 4. Evaluated version must be strictly 2 (the version evaluated by models), NOT 3
    last_eval = get_last_evaluated_ledger_version(db_session, cycle_id)
    assert last_eval == 2

    # And ledger v3 remains unreviewed
    state_after = lab_service.get_lab_ledger_state(cycle_id)
    assert state_after.ledger_version == 3
    assert state_after.last_evaluated_ledger_version == 2
    assert state_after.has_unreviewed_lab_changes is True
    assert "crp" in state_after.active_markers


@pytest.mark.anyio
async def test_failed_consilium_rollback_preserves_previous_evaluated_version(db_session: Session):
    """
    C-4 Requirements 1 & 2:
    If a consilium fails (e.g. Pro quality rejection), the evaluated ledger version
    must NOT be incremented or corrupted.
    """
    cycle_id = "cycle_c4_failed_rollback"
    _seed_preparatory_cycle_context(db_session, cycle_id=cycle_id, seed_ledger=True)

    # 1. Baseline Primary succeeds at v1
    p_calls: List[Dict[str, Any]] = []
    p_gateway = _build_consilium_mock_gateway(p_calls)
    p_service = PrimaryConsiliumService(db=db_session, gateway=p_gateway)
    await p_service.run_primary_consilium(cycle_id=cycle_id, report_date="2026-10-18")
    assert get_last_evaluated_ledger_version(db_session, cycle_id) == 1

    # 2. Add changes -> v2
    lab_service = LabRevisionConsiliumService(
        db=db_session,
        now_provider=lambda: datetime(2026, 10, 20, 10, 0, 0, tzinfo=timezone.utc),
    )
    lab_service.upsert_laboratory_markers(
        cycle_id=cycle_id,
        mutations=[
            LabMarkerMutationItem(
                marker_key="potassium",
                display_name="Potassium",
                value="4.4 mmol/L",
                event_type=LabEventType.INITIAL,
                sample_date="2026-10-20",
            )
        ],
    )

    # 3. Lab Revision rejected by Pro quality gate
    rev_calls: List[Dict[str, Any]] = []
    rejected_pro = _sample_lab_revision_pro_output(
        approved=False,
        rejection_reason="Clinical rejection for test",
    )
    fail_gateway = _build_lab_revision_mock_gateway(rev_calls, pro_override=rejected_pro)
    fail_service = LabRevisionConsiliumService(
        db=db_session,
        gateway=fail_gateway,
        now_provider=lambda: datetime(2026, 10, 20, 12, 0, 0, tzinfo=timezone.utc),
    )

    with pytest.raises(ConsiliumQualityRejectionError):
        await fail_service.run_lab_revision_consilium(cycle_id=cycle_id)

    # 4. Evaluated version remains strictly 1, v2 remains unreviewed
    assert get_last_evaluated_ledger_version(db_session, cycle_id) == 1
    state = lab_service.get_lab_ledger_state(cycle_id)
    assert state.ledger_version == 2
    assert state.last_evaluated_ledger_version == 1
    assert state.has_unreviewed_lab_changes is True


def test_legacy_historical_report_without_evaluated_version_returns_zero(db_session: Session):
    """
    C-4 Requirement 3:
    If explicit evaluated_ledger_version is missing in a legacy historical report,
    get_last_evaluated_ledger_version must safely return 0 without guessing versions.
    """
    cycle_id = "cycle_c4_legacy_report"
    cycle_repo = MedicalCycleRepository(db_session)
    cycle_repo.create(cycle_id=cycle_id, status="active")

    report_repo = ModelReportRepository(db_session)
    # Create baseline ledger v1 so authoritative ledgers exist
    report_repo.create_next_version(
        cycle_id=cycle_id,
        model="aegis-test",
        report_type=LABORATORY_LEDGER_REPORT_TYPE,
        privacy_level=PrivacyLevel.SERVER_ONLY,
        content={"ledger_version": 1, "active_markers": {}},
    )

    # Legacy report without evaluated_ledger_version key
    report_repo.create_next_version(
        cycle_id=cycle_id,
        model="legacy-model",
        report_type="chief_physician_internal",
        privacy_level=PrivacyLevel.SERVER_ONLY,
        content={
            "agent_role": AegisAgentRole.CHIEF_PHYSICIAN.value,
            "pro_general_shelf": "Legacy strategy",
        },
    )

    assert get_last_evaluated_ledger_version(db_session, cycle_id) == 0


@pytest.mark.anyio
async def test_weekly_consilium_safe_indeterminate_state(db_session: Session):
    """
    C-4 Requirement 4:
    If last_evaluated_ver == 0 and the ledger contains markers:
    - shared_labs_shelf = {}
    - unreviewed_pending_labs = all markers
    - has_unreviewed_lab_changes = True
    Never declares unreviewed markers as evaluated.
    """
    cycle_id = "cycle_c4_weekly_indeterminate"
    # Seed preparatory context with NO ledger
    _seed_preparatory_cycle_context(db_session, cycle_id=cycle_id, seed_ledger=False)

    # Run Primary on fallback -> evaluated_ledger_version = 0
    p_calls: List[Dict[str, Any]] = []
    p_gateway = _build_consilium_mock_gateway(p_calls)
    p_service = PrimaryConsiliumService(db=db_session, gateway=p_gateway)
    await p_service.run_primary_consilium(cycle_id=cycle_id, report_date="2026-10-18")

    # Now add an authoritative ledger with markers (never evaluated)
    lab_service = LabRevisionConsiliumService(db=db_session)
    lab_service.upsert_laboratory_markers(
        cycle_id=cycle_id,
        mutations=[
            LabMarkerMutationItem(
                marker_key="glucose",
                display_name="Fasting Glucose",
                value="5.4 mmol/L",
                event_type=LabEventType.INITIAL,
                sample_date="2026-10-25",
            ),
            LabMarkerMutationItem(
                marker_key="hba1c",
                display_name="HbA1c",
                value="5.7 %",
                event_type=LabEventType.INITIAL,
                sample_date="2026-10-25",
            ),
        ],
    )

    assert get_last_evaluated_ledger_version(db_session, cycle_id) == 0

    # Run Weekly Consilium context loader
    weekly_service = WeeklyConsiliumService(db=db_session)
    context = weekly_service._load_previous_consilium_context(cycle_id)

    # Check safe indeterminate state: shared_labs_shelf is empty, all ledger markers are unreviewed
    assert context["shared_labs_shelf"] == {}
    expected_all_markers = set(assemble_consilium_laboratory_context(db_session, cycle_id).shared_labs_table.keys())
    assert set(context["unreviewed_pending_labs"].keys()) == expected_all_markers
    assert context["has_unreviewed_lab_changes"] is True


def test_strict_lookup_missing_v2_when_v3_exists_raises_error(db_session: Session):
    """
    C-4 Fix 1:
    When authoritative ledger v3 exists, but missing v2 is requested via `ledger_version=2`,
    assemble_consilium_laboratory_context must:
    - NOT fallback to OCR;
    - NOT return v3 or any other version;
    - Strictly raise LaboratoryLedgerVersionNotFoundError.
    """
    cycle_id = "cycle_c4_strict_lookup_missing_v2"
    cycle_repo = MedicalCycleRepository(db_session)
    cycle_repo.create(cycle_id=cycle_id, status="active")

    report_repo = ModelReportRepository(db_session)
    # Seed preparatory OCR fallback (must NOT be used when specific version is requested)
    report_repo.create_next_version(
        cycle_id=cycle_id,
        model="aegis-ocr",
        report_type=PREPARATORY_OCR_REPORT_TYPE,
        privacy_level=PrivacyLevel.SERVER_ONLY,
        content={
            "agent_role": AegisAgentRole.VISION_OCR.value,
            "shared_labs_table": {"glucose": {"value": "5.0 mmol/L"}},
        },
    )

    # Create ledger v1 and ledger v3 (v2 is missing)
    report_repo.create_next_version(
        cycle_id=cycle_id,
        model="aegis-ledger",
        report_type=LABORATORY_LEDGER_REPORT_TYPE,
        privacy_level=PrivacyLevel.SERVER_ONLY,
        content={"ledger_version": 1, "active_markers": {"glucose": {"value": "5.0 mmol/L"}}},
    )
    report_repo.create_next_version(
        cycle_id=cycle_id,
        model="aegis-ledger",
        report_type=LABORATORY_LEDGER_REPORT_TYPE,
        privacy_level=PrivacyLevel.SERVER_ONLY,
        content={"ledger_version": 3, "active_markers": {"glucose": {"value": "4.8 mmol/L"}}},
    )

    # Requesting existing v1 succeeds
    ctx_v1 = assemble_consilium_laboratory_context(db_session, cycle_id, ledger_version=1)
    assert ctx_v1.ledger_version == 1

    # Requesting existing v3 succeeds
    ctx_v3 = assemble_consilium_laboratory_context(db_session, cycle_id, ledger_version=3)
    assert ctx_v3.ledger_version == 3

    # Requesting missing v2 MUST raise LaboratoryLedgerVersionNotFoundError
    with pytest.raises(LaboratoryLedgerVersionNotFoundError, match="Authoritative laboratory ledger version 2 was requested"):
        assemble_consilium_laboratory_context(db_session, cycle_id, ledger_version=2)


@pytest.mark.anyio
async def test_weekly_handles_missing_evaluated_ledger_version_safely(db_session: Session):
    """
    C-4 Fix 1:
    If Weekly Consilium needs evaluated snapshot v2 (e.g. from an anomalous report),
    but v2 does not exist in storage (only v3 exists):
    Weekly Consilium catches LaboratoryLedgerVersionNotFoundError and enters safe
    indeterminate state:
    - shared_labs_shelf = {}
    - unreviewed_pending_labs = all markers from v3
    - has_unreviewed_lab_changes = True
    Does NOT crash and does NOT declare unreviewed markers as evaluated.
    """
    cycle_id = "cycle_c4_weekly_missing_evaluated_v2"
    _seed_preparatory_cycle_context(db_session, cycle_id=cycle_id, seed_ledger=True)
    _seed_seven_day_athena_logs(db_session, cycle_id=cycle_id, sprint_id="sprint_c4_w_missing")

    # Run Primary -> evaluated v1
    p_calls: List[Dict[str, Any]] = []
    p_gateway = _build_consilium_mock_gateway(p_calls)
    p_service = PrimaryConsiliumService(db=db_session, gateway=p_gateway)
    await p_service.run_primary_consilium(cycle_id=cycle_id, report_date="2026-10-18")

    # Directly create ledger v3 in DB (skipping v2)
    report_repo = ModelReportRepository(db_session)
    report_repo.create_next_version(
        cycle_id=cycle_id,
        model="aegis-ledger",
        report_type=LABORATORY_LEDGER_REPORT_TYPE,
        privacy_level=PrivacyLevel.SERVER_ONLY,
        content={
            "ledger_version": 3,
            "active_markers": {
                "glucose": {"marker_key": "glucose", "value": "4.9 mmol/L"},
                "hba1c": {"marker_key": "hba1c", "value": "5.5 %"},
            },
            "observations_history": [],
        },
    )

    # Inject execution log claiming evaluated version 2 (which does not exist in ledger!)
    # But because version 2 does NOT exist, get_last_evaluated_ledger_version will return 1!
    assert get_last_evaluated_ledger_version(db_session, cycle_id) == 1

    # Weekly Consilium will diff against v1 (which does exist)
    weekly_service = WeeklyConsiliumService(db=db_session)
    ctx = weekly_service._load_previous_consilium_context(cycle_id)
    assert ctx["evaluated_ledger_version"] == 1
    assert ctx["latest_ledger_version"] == 3
    assert ctx["has_unreviewed_lab_changes"] is True

    # Now verify that assemble_consilium_laboratory_context raises LaboratoryLedgerVersionNotFoundError for v2
    with pytest.raises(LaboratoryLedgerVersionNotFoundError):
        assemble_consilium_laboratory_context(db_session, cycle_id, ledger_version=2)

    # And verify that if Weekly encounters LaboratoryLedgerVersionNotFoundError, it catches it safely
    # and enters safe indeterminate state (shared_labs_shelf={}, unreviewed_pending_labs=all)
    from unittest.mock import patch
    with patch("app.services.lab_context_service.get_last_evaluated_ledger_version", return_value=2):
        ctx_missing_v2 = weekly_service._load_previous_consilium_context(cycle_id)
        assert ctx_missing_v2["shared_labs_shelf"] == {}
        assert "glucose" in ctx_missing_v2["unreviewed_pending_labs"]
        assert "hba1c" in ctx_missing_v2["unreviewed_pending_labs"]
        assert ctx_missing_v2["has_unreviewed_lab_changes"] is True


def test_later_legacy_report_does_not_destroy_earlier_confirmed_evaluation_v2(db_session: Session):
    """
    C-4 Fix 2:
    If a confirmed evaluation of ledger v2 exists from Lab Revision at 11:00,
    and a later legacy report (without evaluated_ledger_version) is saved at 12:00,
    get_last_evaluated_ledger_version MUST still return confirmed version 2.
    The later legacy report must NOT destroy or reset the verified evaluation.
    """
    cycle_id = "cycle_c4_legacy_after_v2"
    cycle_repo = MedicalCycleRepository(db_session)
    cycle_repo.create(cycle_id=cycle_id, status="active")

    report_repo = ModelReportRepository(db_session)
    # Authoritative ledgers v1 and v2 exist
    report_repo.create_next_version(
        cycle_id=cycle_id,
        model="aegis-test",
        report_type=LABORATORY_LEDGER_REPORT_TYPE,
        privacy_level=PrivacyLevel.SERVER_ONLY,
        content={"ledger_version": 1, "active_markers": {}},
        created_at=datetime(2026, 10, 20, 9, 0, tzinfo=timezone.utc),
    )
    report_repo.create_next_version(
        cycle_id=cycle_id,
        model="aegis-test",
        report_type=LABORATORY_LEDGER_REPORT_TYPE,
        privacy_level=PrivacyLevel.SERVER_ONLY,
        content={"ledger_version": 2, "active_markers": {}},
        created_at=datetime(2026, 10, 20, 10, 0, tzinfo=timezone.utc),
    )

    # 1. At 11:00: confirmed Lab Revision evaluation of v2
    report_repo.create_next_version(
        cycle_id=cycle_id,
        model="gemini-pro",
        report_type=LAB_REVISION_EXECUTION_LOG_REPORT_TYPE,
        privacy_level=PrivacyLevel.SERVER_ONLY,
        content={
            "completed_at_utc": "2026-10-20T11:00:00+00:00",
            "evaluated_ledger_version": 2,
            "baseline_ledger_version": 1,
            "cooldown_hours": 168,
        },
        created_at=datetime(2026, 10, 20, 11, 0, tzinfo=timezone.utc),
    )

    # Confirm v2 is verified
    assert get_last_evaluated_ledger_version(db_session, cycle_id) == 2

    # 2. At 12:00: a later legacy report without evaluated_ledger_version is inserted
    report_repo.create_next_version(
        cycle_id=cycle_id,
        model="legacy-pro",
        report_type="chief_physician_internal",
        privacy_level=PrivacyLevel.SERVER_ONLY,
        content={
            "agent_role": AegisAgentRole.CHIEF_PHYSICIAN.value,
            "pro_general_shelf": "Legacy notes without version tracking",
        },
        created_at=datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc),
    )

    # Must still return 2! The later legacy report did not destroy confirmed v2!
    assert get_last_evaluated_ledger_version(db_session, cycle_id) == 2


def test_nonexistent_evaluated_ledger_version_999_is_rejected(db_session: Session):
    """
    C-4 Fix 3:
    If a report claims evaluated_ledger_version = 999, but ledger v999 does not exist in storage:
    get_last_evaluated_ledger_version must REJECT 999!
    If an earlier confirmed version (e.g. v1) exists, it returns v1.
    If no earlier confirmed version exists, it returns 0.
    """
    cycle_id = "cycle_c4_reject_999"
    cycle_repo = MedicalCycleRepository(db_session)
    cycle_repo.create(cycle_id=cycle_id, status="active")

    report_repo = ModelReportRepository(db_session)
    # Only ledger v1 exists
    report_repo.create_next_version(
        cycle_id=cycle_id,
        model="aegis-test",
        report_type=LABORATORY_LEDGER_REPORT_TYPE,
        privacy_level=PrivacyLevel.SERVER_ONLY,
        content={"ledger_version": 1, "active_markers": {}},
        created_at=datetime(2026, 10, 20, 9, 0, tzinfo=timezone.utc),
    )

    # Baseline confirmed v1 at 10:00
    report_repo.create_next_version(
        cycle_id=cycle_id,
        model="gemini-pro",
        report_type="chief_physician_internal",
        privacy_level=PrivacyLevel.SERVER_ONLY,
        content={
            "agent_role": AegisAgentRole.CHIEF_PHYSICIAN.value,
            "flash_quality_review": {"approved_for_publication": True},
            "evaluated_ledger_version": 1,
            "is_synthetic_fallback": False,
        },
        created_at=datetime(2026, 10, 20, 10, 0, tzinfo=timezone.utc),
    )
    assert get_last_evaluated_ledger_version(db_session, cycle_id) == 1

    # Later fraudulent or anomalous report claiming v999 at 11:00
    report_repo.create_next_version(
        cycle_id=cycle_id,
        model="gemini-pro",
        report_type=LAB_REVISION_EXECUTION_LOG_REPORT_TYPE,
        privacy_level=PrivacyLevel.SERVER_ONLY,
        content={
            "completed_at_utc": "2026-10-20T11:00:00+00:00",
            "evaluated_ledger_version": 999,
            "cooldown_hours": 168,
        },
        created_at=datetime(2026, 10, 20, 11, 0, tzinfo=timezone.utc),
    )

    # 999 is rejected because authoritative ledger v999 does not exist!
    # Returns confirmed version 1!
    assert get_last_evaluated_ledger_version(db_session, cycle_id) == 1


def test_unsuccessful_or_unconfirmed_consilium_is_rejected(db_session: Session):
    """
    C-4 Fix 2 & 3:
    Reports from unsuccessful or unconfirmed consiliums must be rejected:
    - chief_physician_internal with flash_quality_review.approved_for_publication = False
    - lab_revision_execution_log with missing or null completed_at_utc
    """
    cycle_id = "cycle_c4_unconfirmed_rejected"
    cycle_repo = MedicalCycleRepository(db_session)
    cycle_repo.create(cycle_id=cycle_id, status="active")

    report_repo = ModelReportRepository(db_session)
    report_repo.create_next_version(
        cycle_id=cycle_id,
        model="aegis-test",
        report_type=LABORATORY_LEDGER_REPORT_TYPE,
        privacy_level=PrivacyLevel.SERVER_ONLY,
        content={"ledger_version": 1, "active_markers": {}},
    )

    # Unapproved Primary report
    report_repo.create_next_version(
        cycle_id=cycle_id,
        model="gemini-pro",
        report_type="chief_physician_internal",
        privacy_level=PrivacyLevel.SERVER_ONLY,
        content={
            "agent_role": AegisAgentRole.CHIEF_PHYSICIAN.value,
            "flash_quality_review": {"approved_for_publication": False, "rejection_reason": "Failed"},
            "evaluated_ledger_version": 1,
            "is_synthetic_fallback": False,
        },
        created_at=datetime(2026, 10, 20, 10, 0, tzinfo=timezone.utc),
    )
    # Must be 0 because quality review rejected publication
    assert get_last_evaluated_ledger_version(db_session, cycle_id) == 0

    # Incomplete Lab Revision execution log without completed_at_utc
    report_repo.create_next_version(
        cycle_id=cycle_id,
        model="gemini-pro",
        report_type=LAB_REVISION_EXECUTION_LOG_REPORT_TYPE,
        privacy_level=PrivacyLevel.SERVER_ONLY,
        content={
            "completed_at_utc": None,
            "evaluated_ledger_version": 1,
        },
        created_at=datetime(2026, 10, 20, 11, 0, tzinfo=timezone.utc),
    )
    # Must still be 0
    assert get_last_evaluated_ledger_version(db_session, cycle_id) == 0


def test_primary_quality_review_strict_boolean_validation(db_session: Session):
    """
    C-4 Final Validation:
    Verification of chief_physician_internal requires strict positive proof of success:
    flash_quality_review.approved_for_publication must be strictly boolean True.
    All of the following must be rejected (returning 0):
    - approved_for_publication = False
    - missing flash_quality_review
    - flash_quality_review = None or non-dict
    - missing approved_for_publication in dict
    - approved_for_publication = None
    - invalid types: "true", "True", 1, 0, [True]
    Only strictly approved_for_publication = True is accepted.
    """
    cycle_id_base = "cycle_c4_strict_bool_"
    cycle_repo = MedicalCycleRepository(db_session)
    report_repo = ModelReportRepository(db_session)

    # 1. Missing flash_quality_review
    c1 = f"{cycle_id_base}missing_qr"
    cycle_repo.create(cycle_id=c1, status="active")
    report_repo.create_next_version(
        cycle_id=c1,
        model="aegis-test",
        report_type=LABORATORY_LEDGER_REPORT_TYPE,
        privacy_level=PrivacyLevel.SERVER_ONLY,
        content={"ledger_version": 1, "active_markers": {}},
    )
    report_repo.create_next_version(
        cycle_id=c1,
        model="gemini-pro",
        report_type="chief_physician_internal",
        privacy_level=PrivacyLevel.SERVER_ONLY,
        content={
            "agent_role": AegisAgentRole.CHIEF_PHYSICIAN.value,
            "evaluated_ledger_version": 1,
            "is_synthetic_fallback": False,
        },
    )
    assert get_last_evaluated_ledger_version(db_session, c1) == 0

    # 2. flash_quality_review is None
    c2 = f"{cycle_id_base}none_qr"
    cycle_repo.create(cycle_id=c2, status="active")
    report_repo.create_next_version(
        cycle_id=c2,
        model="aegis-test",
        report_type=LABORATORY_LEDGER_REPORT_TYPE,
        privacy_level=PrivacyLevel.SERVER_ONLY,
        content={"ledger_version": 1, "active_markers": {}},
    )
    report_repo.create_next_version(
        cycle_id=c2,
        model="gemini-pro",
        report_type="chief_physician_internal",
        privacy_level=PrivacyLevel.SERVER_ONLY,
        content={
            "agent_role": AegisAgentRole.CHIEF_PHYSICIAN.value,
            "flash_quality_review": None,
            "evaluated_ledger_version": 1,
            "is_synthetic_fallback": False,
        },
    )
    assert get_last_evaluated_ledger_version(db_session, c2) == 0

    # 3. flash_quality_review is non-dict (string)
    c3 = f"{cycle_id_base}string_qr"
    cycle_repo.create(cycle_id=c3, status="active")
    report_repo.create_next_version(
        cycle_id=c3,
        model="aegis-test",
        report_type=LABORATORY_LEDGER_REPORT_TYPE,
        privacy_level=PrivacyLevel.SERVER_ONLY,
        content={"ledger_version": 1, "active_markers": {}},
    )
    report_repo.create_next_version(
        cycle_id=c3,
        model="gemini-pro",
        report_type="chief_physician_internal",
        privacy_level=PrivacyLevel.SERVER_ONLY,
        content={
            "agent_role": AegisAgentRole.CHIEF_PHYSICIAN.value,
            "flash_quality_review": "approved",
            "evaluated_ledger_version": 1,
            "is_synthetic_fallback": False,
        },
    )
    assert get_last_evaluated_ledger_version(db_session, c3) == 0

    # 4. Missing approved_for_publication in dict
    c4 = f"{cycle_id_base}missing_approved"
    cycle_repo.create(cycle_id=c4, status="active")
    report_repo.create_next_version(
        cycle_id=c4,
        model="aegis-test",
        report_type=LABORATORY_LEDGER_REPORT_TYPE,
        privacy_level=PrivacyLevel.SERVER_ONLY,
        content={"ledger_version": 1, "active_markers": {}},
    )
    report_repo.create_next_version(
        cycle_id=c4,
        model="gemini-pro",
        report_type="chief_physician_internal",
        privacy_level=PrivacyLevel.SERVER_ONLY,
        content={
            "agent_role": AegisAgentRole.CHIEF_PHYSICIAN.value,
            "flash_quality_review": {"rejection_reason": None},
            "evaluated_ledger_version": 1,
            "is_synthetic_fallback": False,
        },
    )
    assert get_last_evaluated_ledger_version(db_session, c4) == 0

    # 5. Invalid types: None, "true", "True", 1, 0, list
    for idx, invalid_val in enumerate([None, "true", "True", 1, 0, [True]]):
        c_inv = f"{cycle_id_base}invalid_{idx}"
        cycle_repo.create(cycle_id=c_inv, status="active")
        report_repo.create_next_version(
            cycle_id=c_inv,
            model="aegis-test",
            report_type=LABORATORY_LEDGER_REPORT_TYPE,
            privacy_level=PrivacyLevel.SERVER_ONLY,
            content={"ledger_version": 1, "active_markers": {}},
        )
        report_repo.create_next_version(
            cycle_id=c_inv,
            model="gemini-pro",
            report_type="chief_physician_internal",
            privacy_level=PrivacyLevel.SERVER_ONLY,
            content={
                "agent_role": AegisAgentRole.CHIEF_PHYSICIAN.value,
                "flash_quality_review": {"approved_for_publication": invalid_val},
                "evaluated_ledger_version": 1,
                "is_synthetic_fallback": False,
            },
        )
        assert get_last_evaluated_ledger_version(db_session, c_inv) == 0

    # 6. Strictly True: Accepted
    c_ok = f"{cycle_id_base}strict_true"
    cycle_repo.create(cycle_id=c_ok, status="active")
    report_repo.create_next_version(
        cycle_id=c_ok,
        model="aegis-test",
        report_type=LABORATORY_LEDGER_REPORT_TYPE,
        privacy_level=PrivacyLevel.SERVER_ONLY,
        content={"ledger_version": 1, "active_markers": {}},
    )
    report_repo.create_next_version(
        cycle_id=c_ok,
        model="gemini-pro",
        report_type="chief_physician_internal",
        privacy_level=PrivacyLevel.SERVER_ONLY,
        content={
            "agent_role": AegisAgentRole.CHIEF_PHYSICIAN.value,
            "flash_quality_review": {"approved_for_publication": True},
            "evaluated_ledger_version": 1,
            "is_synthetic_fallback": False,
        },
    )
    assert get_last_evaluated_ledger_version(db_session, c_ok) == 1


def test_later_unconfirmed_report_preserves_earlier_confirmed_evaluation(db_session: Session):
    """
    C-4 Final Validation:
    If an earlier confirmed consilium exists (e.g. Primary v1 or Lab Revision v2),
    a subsequent unconfirmed report (or failed quality review) must NOT hide,
    overwrite, or destroy the evaluated_ledger_version.
    """
    cycle_id = "cycle_c4_preserve_earlier_eval"
    cycle_repo = MedicalCycleRepository(db_session)
    cycle_repo.create(cycle_id=cycle_id, status="active")

    report_repo = ModelReportRepository(db_session)
    # 1. Seed ledger v1 and v2
    report_repo.create_next_version(
        cycle_id=cycle_id,
        model="aegis-test",
        report_type=LABORATORY_LEDGER_REPORT_TYPE,
        privacy_level=PrivacyLevel.SERVER_ONLY,
        content={"ledger_version": 1, "active_markers": {}},
        created_at=datetime(2026, 10, 20, 9, 0, tzinfo=timezone.utc),
    )
    report_repo.create_next_version(
        cycle_id=cycle_id,
        model="aegis-test",
        report_type=LABORATORY_LEDGER_REPORT_TYPE,
        privacy_level=PrivacyLevel.SERVER_ONLY,
        content={"ledger_version": 2, "active_markers": {}},
        created_at=datetime(2026, 10, 20, 10, 0, tzinfo=timezone.utc),
    )

    # 2. Primary successfully evaluates ledger v1 at 10:30
    report_repo.create_next_version(
        cycle_id=cycle_id,
        model="gemini-pro",
        report_type="chief_physician_internal",
        privacy_level=PrivacyLevel.SERVER_ONLY,
        content={
            "agent_role": AegisAgentRole.CHIEF_PHYSICIAN.value,
            "flash_quality_review": {"approved_for_publication": True},
            "evaluated_ledger_version": 1,
            "is_synthetic_fallback": False,
        },
        created_at=datetime(2026, 10, 20, 10, 30, tzinfo=timezone.utc),
    )
    assert get_last_evaluated_ledger_version(db_session, cycle_id) == 1

    # 3. Later, an unconfirmed Primary report arrives at 11:00 with approved_for_publication=False
    report_repo.create_next_version(
        cycle_id=cycle_id,
        model="gemini-pro",
        report_type="chief_physician_internal",
        privacy_level=PrivacyLevel.SERVER_ONLY,
        content={
            "agent_role": AegisAgentRole.CHIEF_PHYSICIAN.value,
            "flash_quality_review": {"approved_for_publication": False},
            "evaluated_ledger_version": 2,
            "is_synthetic_fallback": False,
        },
        created_at=datetime(2026, 10, 20, 11, 0, tzinfo=timezone.utc),
    )
    # Must still return 1, because v1 was confirmed and the 11:00 report was rejected
    assert get_last_evaluated_ledger_version(db_session, cycle_id) == 1

    # 4. Later, an incomplete Lab Revision execution log arrives at 11:30 (completed_at_utc=None)
    report_repo.create_next_version(
        cycle_id=cycle_id,
        model="gemini-pro",
        report_type=LAB_REVISION_EXECUTION_LOG_REPORT_TYPE,
        privacy_level=PrivacyLevel.SERVER_ONLY,
        content={
            "completed_at_utc": None,
            "evaluated_ledger_version": 2,
        },
        created_at=datetime(2026, 10, 20, 11, 30, tzinfo=timezone.utc),
    )
    # Must still return 1
    assert get_last_evaluated_ledger_version(db_session, cycle_id) == 1

    # 5. Later, an unconfirmed report with missing flash_quality_review arrives at 12:00
    report_repo.create_next_version(
        cycle_id=cycle_id,
        model="gemini-pro",
        report_type="chief_physician_internal",
        privacy_level=PrivacyLevel.SERVER_ONLY,
        content={
            "agent_role": AegisAgentRole.CHIEF_PHYSICIAN.value,
            "evaluated_ledger_version": 2,
            "is_synthetic_fallback": False,
        },
        created_at=datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc),
    )
    # Must still return 1
    assert get_last_evaluated_ledger_version(db_session, cycle_id) == 1

    # 6. Now Lab Revision successfully completes evaluation of v2 at 12:30
    report_repo.create_next_version(
        cycle_id=cycle_id,
        model="gemini-pro",
        report_type=LAB_REVISION_EXECUTION_LOG_REPORT_TYPE,
        privacy_level=PrivacyLevel.SERVER_ONLY,
        content={
            "completed_at_utc": "2026-10-20T12:30:00Z",
            "evaluated_ledger_version": 2,
        },
        created_at=datetime(2026, 10, 20, 12, 30, tzinfo=timezone.utc),
    )
    # Now confirmed evaluation advances to 2
    assert get_last_evaluated_ledger_version(db_session, cycle_id) == 2

    # 7. Even if another unconfirmed report arrives at 13:00 (e.g. invalid type 1)
    report_repo.create_next_version(
        cycle_id=cycle_id,
        model="gemini-pro",
        report_type="chief_physician_internal",
        privacy_level=PrivacyLevel.SERVER_ONLY,
        content={
            "agent_role": AegisAgentRole.CHIEF_PHYSICIAN.value,
            "flash_quality_review": {"approved_for_publication": 1},
            "evaluated_ledger_version": 2,
            "is_synthetic_fallback": False,
        },
        created_at=datetime(2026, 10, 20, 13, 0, tzinfo=timezone.utc),
    )
    # Must still return 2
    assert get_last_evaluated_ledger_version(db_session, cycle_id) == 2


@pytest.mark.anyio
async def test_full_sequence_primary_labs_weekly_revision_weekly(db_session: Session):
    """
    Full integration lifecycle:
    1. Primary runs on ledger v1 -> evaluated v1.
    2. New labs added -> ledger v2.
    3. Weekly runs -> shared_labs_shelf is v1, unreviewed_pending_labs has delta, has_unreviewed_lab_changes is True.
    4. Lab Revision runs on v2 -> evaluated v2.
    5. Weekly runs -> shared_labs_shelf is v2, unreviewed_pending_labs is empty, has_unreviewed_lab_changes is False.
    """
    cycle_id = "cycle_c4_full_lifecycle"
    _seed_preparatory_cycle_context(db_session, cycle_id=cycle_id, seed_ledger=True)
    _seed_seven_day_athena_logs(db_session, cycle_id=cycle_id, sprint_id="sprint_c4_1")

    # Step 1: Primary Consilium
    p_calls: List[Dict[str, Any]] = []
    p_gateway = _build_consilium_mock_gateway(p_calls)
    p_service = PrimaryConsiliumService(db=db_session, gateway=p_gateway)
    await p_service.run_primary_consilium(cycle_id=cycle_id, report_date="2026-10-18")
    assert get_last_evaluated_ledger_version(db_session, cycle_id) == 1

    # Step 2: Add new dated study -> ledger v2
    lab_service = LabRevisionConsiliumService(
        db=db_session,
        now_provider=lambda: datetime(2026, 10, 22, 10, 0, 0, tzinfo=timezone.utc),
    )
    lab_service.upsert_laboratory_markers(
        cycle_id=cycle_id,
        mutations=[
            LabMarkerMutationItem(
                marker_key="glucose",
                display_name="Glucose",
                value="4.8 mmol/L",
                event_type=LabEventType.NEW_STUDY,
                sample_date="2026-10-22",
            )
        ],
    )
    assert lab_service.get_lab_ledger_state(cycle_id).ledger_version == 2
    assert get_last_evaluated_ledger_version(db_session, cycle_id) == 1

    # Step 3: Weekly Consilium sees evaluated v1 and unreviewed v2
    weekly_service = WeeklyConsiliumService(db=db_session)
    w_context_1 = weekly_service._load_previous_consilium_context(cycle_id)
    assert w_context_1["shared_labs_shelf"]["glucose"]["value"] == "5.1 mmol/L"
    assert "glucose" in w_context_1["unreviewed_pending_labs"]
    assert w_context_1["unreviewed_pending_labs"]["glucose"]["value"] == "4.8 mmol/L"
    assert w_context_1["has_unreviewed_lab_changes"] is True

    # Step 4: Lab Revision Consilium evaluates v2
    rev_calls: List[Dict[str, Any]] = []
    rev_gateway = _build_lab_revision_mock_gateway(rev_calls)
    revision_service = LabRevisionConsiliumService(
        db=db_session,
        gateway=rev_gateway,
        now_provider=lambda: datetime(2026, 10, 25, 14, 0, 0, tzinfo=timezone.utc),
    )
    await revision_service.run_lab_revision_consilium(cycle_id=cycle_id)
    assert get_last_evaluated_ledger_version(db_session, cycle_id) == 2

    # Step 5: Weekly Consilium now sees v2 as evaluated, with zero unreviewed changes
    w_context_2 = weekly_service._load_previous_consilium_context(cycle_id)
    assert w_context_2["shared_labs_shelf"]["glucose"]["value"] == "4.8 mmol/L"
    assert w_context_2["unreviewed_pending_labs"] == {}
    assert w_context_2["has_unreviewed_lab_changes"] is False
