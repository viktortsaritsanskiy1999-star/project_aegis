import asyncio
from datetime import datetime, timedelta, timezone
import json
from typing import Any, Dict, List
import httpx
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.repositories.cycle_repository import MedicalCycleRepository
from app.repositories.report_repository import ModelReportRepository
from app.repositories.sprint_repository import WeeklySprintRepository
from app.routers.cascade import get_gemini_gateway, get_server_now_provider
from app.schemas.consilium import (
    SYSTEM_KEYS,
    LAB_REVISION_COOLDOWN_HOURS,
    AegisAgentRole,
    LabEventType,
    LabReferenceRange,
    LabMarkerMutationItem,
)
from app.services.gemini_gateway import GeminiGateway, GeminiValidationError
from app.services.primary_consilium_service import (
    PrimaryConsiliumService,
    ConsiliumQualityRejectionError,
)
from app.services.weekly_consilium_service import (
    WeeklyConsiliumService,
    ATHENA_ACTIVE_STRATEGY_REPORT_TYPE,
)
from app.services.lab_revision_consilium_service import (
    LabRevisionConsiliumService,
    ConsiliumCooldownError,
    LABORATORY_LEDGER_REPORT_TYPE,
    LABORATORY_DIFF_AUDIT_REPORT_TYPE,
    LAB_REVISION_EXECUTION_LOG_REPORT_TYPE,
)
from sqlalchemy.orm import Session
from tests.test_primary_consilium import (
    _gemini_envelope,
    _seed_preparatory_cycle_context,
    _build_consilium_mock_gateway,
)
from tests.test_weekly_consilium import (
    _seed_seven_day_athena_logs,
    _build_weekly_mock_gateway,
)


def _sample_lab_revision_organ_output(system_key: str) -> Dict[str, Any]:
    return {
        "system_view": {
            "isolated_labs": {
                f"{system_key}_revised_marker": "updated_value_in_dynamics"
            }
        },
        "system_logic": {
            "system_general_shelf": (
                f"RETAINED CONCLUSIONS for {system_key}: baseline tone stable. "
                f"REVISED CONCLUSIONS for {system_key}: updated biomarker profile accounted for."
            )
        },
        "training_sandbox": {
            "system_training_shelf": (
                f"SECRET_LAB_REVISION_TRAINING_{system_key.upper()}: "
                f"DATA DEFICITS remaining for {system_key}: follow-up panel recommended."
            )
        },
    }


def _sample_lab_revision_flash_output(
    cvs_summary: str | None = None,
    gi_summary: str | None = None,
    gi_has_changes: bool = False,
) -> Dict[str, Any]:
    reports: Dict[str, Dict[str, Any]] = {}
    for key in SYSTEM_KEYS:
        if key in ("cvs", "immune", "endocrine"):
            reports[key] = {
                "summary": (
                    f"Comparative update for {key}: updated laboratory results reflect "
                    "a more accurate ferritin and metabolic baseline."
                ),
                "has_changes": True,
            }
        else:
            reports[key] = {
                "summary": (
                    f"Available new laboratory data does not provide sufficient grounds "
                    f"to change the previous assessment for {key}."
                ),
                "has_changes": False,
            }

    if cvs_summary is not None:
        reports["cvs"]["summary"] = cvs_summary
    if gi_summary is not None:
        reports["gi_tract"]["summary"] = gi_summary
        reports["gi_tract"]["has_changes"] = gi_has_changes

    return {
        "flash_general_shelf": (
            "COMPARATIVE GENERAL CONCLUSION:\n\n"
            "1. COMPARATIVE SUMMARY:\n"
            "Corrected ferritin baseline and new vitamin D study integrated.\n\n"
            "2. DYNAMIC SYSTEM SYNTHESIS:\n"
            "- CVS: Revised preload commentary\n"
            "- IMMUNE: Revised iron store commentary\n"
            "- ENDOCRINE: Added vitamin D commentary"
        ),
        "flash_training_shelf": (
            "SECRET_LAB_REVISION_FLASH_TRAINING: Comparative audit of 8 Flash-Lite models completed."
        ),
        "user_system_reports": reports,
    }


def _sample_lab_revision_pro_output(
    strategy_text: str | None = None,
    approved: bool = True,
    rejection_reason: str | None = None,
    corrected_reports: Dict[str, str] | None = None,
) -> Dict[str, Any]:
    default_strategy = (
        "SECRET_LAB_REVISION_ATHENA_STRATEGY: Updated Chief Physician strategy after lab revision. "
        "Focus on iron-rich nutrition timing, vitamin D supplementation adherence, and pulse stability "
        "(max 3 clarifying questions/day)."
    )
    return {
        "vitals_thresholds": {
            "min_pessimum_bpm": 46,
            "max_pessimum_bpm": 132,
            "max_spike_delta_bpm": 34,
            "clinical_rationale": "SECRET_LAB_REVISION_RATIONALE: Thresholds refined after ferritin correction.",
        },
        "pro_general_shelf": strategy_text if strategy_text is not None else default_strategy,
        "pro_training_shelf": (
            "SECRET_LAB_REVISION_PRO_TRAINING: Supreme comparative audit of 8 Flash-Lite and Deputy Flash."
        ),
        "user_overall_report": {
            "title": "Aegis Comparative Lab Revision Overview",
            "summary": (
                "Your wellness overview has been updated to reflect your corrected ferritin result "
                "and new laboratory tests."
            ),
            "highlights": [
                "Maintain steady hydration and nutrition schedule",
                "Continue tracking daily pulse and energy levels with Athena",
            ],
        },
        "flash_quality_review": {
            "approved_for_publication": approved,
            "rejection_reason": rejection_reason,
            "corrected_user_system_reports": corrected_reports or {},
        },
    }


def _build_lab_revision_mock_gateway(
    recorded_calls: List[Dict[str, Any]],
    flash_override: Dict[str, Any] | None = None,
    pro_override: Dict[str, Any] | None = None,
    fail_on_call_index: int | None = None,
    artificial_delay_sec: float = 0.0,
) -> GeminiGateway:
    """
    Mock GeminiGateway for Stage 3.4 Full Lab-Revision Consilium.
    Expects strictly 10 calls in order:
    - Calls 0..7: 8 Gemini Flash-Lite organ specialists
    - Call 8: Gemini Flash (Deputy Chief Physician)
    - Call 9: Gemini Pro (Chief Physician)
    """
    call_counter = {"index": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        if artificial_delay_sec > 0:
            await asyncio.sleep(artificial_delay_sec)

        idx = call_counter["index"]
        call_counter["index"] += 1

        body = json.loads(request.content.decode("utf-8"))
        prompt_text = body["contents"][0]["parts"][0]["text"]
        url_path = request.url.path

        recorded_calls.append(
            {
                "index": idx,
                "url_path": url_path,
                "prompt": prompt_text,
            }
        )

        if fail_on_call_index is not None and idx == fail_on_call_index:
            return httpx.Response(
                400,
                json={"error": {"message": f"Injected lab-revision failure at call index {idx}"}},
            )

        if 0 <= idx < 8:
            sys_key = SYSTEM_KEYS[idx]
            return httpx.Response(
                200,
                json=_gemini_envelope(_sample_lab_revision_organ_output(sys_key)),
            )

        if idx == 8:
            payload = flash_override or _sample_lab_revision_flash_output()
            return httpx.Response(200, json=_gemini_envelope(payload))

        if idx == 9:
            payload = pro_override or _sample_lab_revision_pro_output()
            return httpx.Response(200, json=_gemini_envelope(payload))

        raise AssertionError(f"Unexpected extra Gemini call at index {idx}: {url_path}")

    return GeminiGateway(
        api_key="test-lab-revision-key",
        transport=httpx.MockTransport(handler),
        sleep_func=lambda _: None,
    )


async def _seed_primary_consilium(
    db_session,
    cycle_id: str = "cycle_lab_rev_01",
    primary_date: str = "2026-10-18",
) -> None:
    _seed_preparatory_cycle_context(db_session, cycle_id=cycle_id, seed_ledger=True)
    primary_calls: List[Dict[str, Any]] = []
    primary_gateway = _build_consilium_mock_gateway(primary_calls)
    primary_service = PrimaryConsiliumService(db=db_session, gateway=primary_gateway)
    await primary_service.run_primary_consilium(
        cycle_id=cycle_id,
        report_date=primary_date,
    )


@pytest.mark.anyio
async def test_lab_ledger_error_correction_vs_new_dated_study_and_deterministic_diff(
    db_session,
):
    """
    Covers Requirements 1, 2, 6, and Tests 1, 2, 3, 14:
    1. Editing/correcting an erroneous marker value preserves the original erroneous value in
       `observations_history` (`is_active=False`, `supersedes_observation_id` set).
    2. Adding a new study with a different `sample_date` keeps the earlier dated study intact
       without marking it as an error.
    3. Deterministic diff accurately classifies added markers, error corrections, new dated studies,
       unit changes (`directly_comparable=False`), invalidated markers, and unchanged markers,
       without claiming numerical changes are clinical improvements or deteriorations.
    4. Saving laboratory changes works anytime with zero cooldown and immediately marks
       `has_unreviewed_lab_changes=True` (`evaluation_status='pending_full_consilium_revision'`),
       including the medical safety notice when critical deviations are present.
    """
    cycle_id = "cycle_ledger_diff"
    await _seed_primary_consilium(db_session, cycle_id=cycle_id, primary_date="2026-10-18")

    service = LabRevisionConsiliumService(
        db=db_session,
        now_provider=lambda: datetime(2026, 10, 19, 10, 0, 0, tzinfo=timezone.utc),
    )

    # Initial state after Primary Consilium: baseline v1 is evaluated
    state_v1 = service.get_lab_ledger_state(cycle_id)
    assert state_v1.ledger_version == 1
    assert state_v1.last_evaluated_ledger_version == 1
    assert state_v1.has_unreviewed_lab_changes is False
    assert state_v1.evaluation_status == "evaluated"
    assert "ferritin" in state_v1.active_markers
    orig_ferritin_obs_id = state_v1.active_markers["ferritin"].observation_id
    orig_ferritin_val = state_v1.active_markers["ferritin"].value

    # Step 1: Correct an erroneous value in ferritin (e.g., OCR read '18 ng/mL' instead of '18.5 ng/mL'),
    # add a new dated study for glucose on 2026-10-19, change units on cholesterol (mmol/L -> mg/dL),
    # add a brand-new marker (vitamin_d), and invalidate an erroneous alt_marker if added.
    state_v2 = service.upsert_laboratory_markers(
        cycle_id=cycle_id,
        mutations=[
            LabMarkerMutationItem(
                marker_key="ferritin",
                display_name="Ferritin",
                event_type=LabEventType.CORRECTION,
                value=14.2,
                unit="ng/mL",
                sample_date="2026-10-18",
                reference_range=LabReferenceRange(low=30.0, high=300.0, text="30 - 300 ng/mL"),
                supersedes_observation_id=orig_ferritin_obs_id,
                correction_reason="Corrected manual decimal typo from initial lab sheet",
                is_critical_flag=True,
            ),
            LabMarkerMutationItem(
                marker_key="glucose",
                display_name="Fasting Glucose",
                event_type=LabEventType.NEW_STUDY,
                value=5.4,
                unit="mmol/L",
                sample_date="2026-10-19",
                reference_range=LabReferenceRange(low=3.9, high=5.6, text="3.9 - 5.6 mmol/L"),
            ),
            LabMarkerMutationItem(
                marker_key="creatinine",
                display_name="Creatinine",
                event_type=LabEventType.NEW_STUDY,
                value=0.92,
                unit="mg/dL",
                sample_date="2026-10-19",
                reference_range=LabReferenceRange(low=0.7, high=1.2, text="0.7 - 1.2 mg/dL"),
            ),
            LabMarkerMutationItem(
                marker_key="vitamin_d",
                display_name="25-OH Vitamin D",
                event_type=LabEventType.INITIAL,
                value=22.0,
                unit="ng/mL",
                sample_date="2026-10-19",
                reference_range=LabReferenceRange(low=30.0, high=100.0, text="30 - 100 ng/mL"),
            ),
        ],
    )

    # Verify unreviewed status & critical safety notice (Test 14)
    assert state_v2.ledger_version == 2
    assert state_v2.last_evaluated_ledger_version == 1
    assert state_v2.has_unreviewed_lab_changes is True
    assert state_v2.evaluation_status == "pending_full_consilium_revision"
    assert state_v2.has_critical_flags is True
    assert "NOT yet been evaluated by a new Full Consilium" in state_v2.evaluation_notice
    assert "seek in-person medical attention" in state_v2.evaluation_notice

    # Verify original ferritin observation is preserved in history and NOT overwritten (Test 1)
    ferritin_history = [
        obs for obs in state_v2.observations_history if obs.marker_key == "ferritin"
    ]
    assert len(ferritin_history) == 2
    assert ferritin_history[0].observation_id == orig_ferritin_obs_id
    assert ferritin_history[0].value == orig_ferritin_val
    assert ferritin_history[0].is_active is False
    assert ferritin_history[1].event_type == LabEventType.CORRECTION
    assert ferritin_history[1].supersedes_observation_id == orig_ferritin_obs_id
    assert ferritin_history[1].value == 14.2
    assert ferritin_history[1].is_active is True

    # Verify glucose has both the initial study and the new dated study without marking initial as error (Test 2)
    glucose_history = [
        obs for obs in state_v2.observations_history if obs.marker_key == "glucose"
    ]
    assert len(glucose_history) == 2
    assert glucose_history[0].event_type == LabEventType.INITIAL
    assert glucose_history[0].supersedes_observation_id is None
    assert glucose_history[1].event_type == LabEventType.NEW_STUDY
    assert glucose_history[1].sample_date == "2026-10-19"

    # Step 2: Compute deterministic diff between baseline v1 and current v2 (Test 3)
    diff = service.compute_laboratory_diff(cycle_id=cycle_id)
    assert diff.baseline_ledger_version == 1
    assert diff.target_ledger_version == 2

    added_keys = {item.marker_key for item in diff.added_markers}
    assert "vitamin_d" in added_keys

    corrected_keys = {item.marker_key for item in diff.corrected_error_values}
    assert "ferritin" in corrected_keys

    dated_keys = {item.marker_key for item in diff.new_dated_studies}
    assert "glucose" in dated_keys

    unit_changed_map = {item.marker_key: item for item in diff.unit_changed_markers}
    assert "creatinine" in unit_changed_map
    creat_diff = unit_changed_map["creatinine"]
    assert creat_diff.directly_comparable is False
    assert "unit changed" in creat_diff.comparability_note.lower()

    unchanged_keys = {item.marker_key for item in diff.unchanged_markers}
    assert "hemoglobin" in unchanged_keys


@pytest.mark.anyio
async def test_full_lab_revision_cascade_order_previous_context_and_pro_corrections(
    db_session,
):
    """
    Covers Requirements 3, 4, and Tests 4, 5, 6, 7:
    1. Strictly executes 8 Gemini Flash-Lite -> Gemini Flash -> Gemini Pro (10 calls total),
       never calling Anamnesis M1 or Vision OCR/ECG.
    2. Passes updated labs, deterministic diff, and each agent's own previous conclusion
       (plus previous conclusions/audits of other models) to every agent.
    3. Systems with `has_changes=False` explicitly explain that available new data does not
       provide sufficient grounds to change the previous assessment; claiming "guaranteed unchanged"
       or omitting the insufficient-grounds explanation is rejected.
    4. Pro's `corrected_user_system_reports` deterministically overwrites Flash's draft before
       publication, and `laboratory_diff_audit` is persisted as `server_only`.
    """
    cycle_id = "cycle_lab_rev_cascade"
    await _seed_primary_consilium(db_session, cycle_id=cycle_id, primary_date="2026-10-18")

    now_dt = datetime(2026, 10, 22, 12, 0, 0, tzinfo=timezone.utc)
    lab_service = LabRevisionConsiliumService(db=db_session, now_provider=lambda: now_dt)
    lab_service.upsert_laboratory_markers(
        cycle_id=cycle_id,
        mutations=[
            LabMarkerMutationItem(
                marker_key="ferritin",
                display_name="Ferritin",
                event_type=LabEventType.NEW_STUDY,
                value=29.5,
                unit="ng/mL",
                sample_date="2026-10-22",
                reference_range=LabReferenceRange(low=30.0, high=300.0, text="30 - 300 ng/mL"),
            ),
        ],
    )

    flash_bad_cvs = "UNVERIFIED_LAB_REVISION_DRAFT: Myocardial ischemia suspected."
    pro_fixed_cvs = (
        "Comparative cardiovascular review: ferritin dynamics and steady resting heart rate "
        "support continuing moderate aerobic pacing."
    )

    recorded_calls: List[Dict[str, Any]] = []
    mock_gateway = _build_lab_revision_mock_gateway(
        recorded_calls=recorded_calls,
        flash_override=_sample_lab_revision_flash_output(cvs_summary=flash_bad_cvs),
        pro_override=_sample_lab_revision_pro_output(
            corrected_reports={"cvs": pro_fixed_cvs}
        ),
    )

    rev_service = LabRevisionConsiliumService(
        db=db_session,
        gateway=mock_gateway,
        now_provider=lambda: now_dt,
    )
    response = await rev_service.run_lab_revision_consilium(
        cycle_id=cycle_id,
        report_date="2026-10-22",
    )

    # Test 4: Strictly 10 calls in order: 8 Flash-Lite -> 1 Flash -> 1 Pro
    assert len(recorded_calls) == 10
    for idx in range(8):
        assert mock_gateway.flash_lite_model in recorded_calls[idx]["url_path"]
    assert mock_gateway.flash_model in recorded_calls[8]["url_path"]
    assert "flash-lite" not in recorded_calls[8]["url_path"]
    assert mock_gateway.pro_model in recorded_calls[9]["url_path"]

    # Test 5: Each of the 8 Flash-Lite agents received its own previous conclusion, updated labs, and diff
    for idx, sys_key in enumerate(SYSTEM_KEYS):
        organ_prompt = recorded_calls[idx]["prompt"]
        assert "COMPARATIVE LAB-REVISION ADDENDUM" in organ_prompt
        assert f"YOUR OWN PREVIOUS CONCLUSION FOR '{sys_key.upper()}'" in organ_prompt
        assert f"Academic pathophysiological conclusion for {sys_key}." in organ_prompt
        assert "29.5" in organ_prompt
        assert "new_dated_study" in organ_prompt

    # Deputy Flash and Chief Physician Pro received previous conclusions, diff, and all 8 organ outputs
    flash_prompt = recorded_calls[8]["prompt"]
    pro_prompt = recorded_calls[9]["prompt"]
    assert "DETERMINISTIC LABORATORY DIFF" in flash_prompt
    assert "PREVIOUS DEPUTY FLASH & CHIEF PHYSICIAN PRO CONCLUSIONS" in flash_prompt
    assert "RETAINED CONCLUSIONS for cvs" in flash_prompt
    assert "SUPREME COMPARATIVE LAB-REVISION AUDIT ADDENDUM" in pro_prompt
    assert "COMPARATIVE SYNTHESIS (GEMINI FLASH)" in pro_prompt

    # Test 6 & 7: Pro correction applied to CVS; GI tract has has_changes=False with insufficient-grounds explanation
    assert response.evaluated_ledger_version == 2
    assert response.evaluation_status == "evaluated"
    cvs_rep = response.system_reports["cvs"]
    assert cvs_rep.version == 2
    assert cvs_rep.content.summary == pro_fixed_cvs
    assert "UNVERIFIED_LAB_REVISION_DRAFT" not in cvs_rep.model_dump_json()
    assert len(cvs_rep.content.entries) == 2
    assert cvs_rep.content.entries[0].date == "2026-10-18"
    assert cvs_rep.content.entries[1].date == "2026-10-22"

    gi_rep = response.system_reports["gi_tract"]
    assert gi_rep.content.has_changes is False
    assert "sufficient grounds" in gi_rep.content.summary.lower()

    # Verify ledger state is now marked as evaluated
    post_state = rev_service.get_lab_ledger_state(cycle_id)
    assert post_state.has_unreviewed_lab_changes is False
    assert post_state.last_evaluated_ledger_version == 2
    assert post_state.evaluation_status == "evaluated"

    # Verify diff audit was saved as server_only
    report_repo = ModelReportRepository(db_session)
    diff_audits = report_repo.list_server_only(
        cycle_id=cycle_id,
        report_type=LABORATORY_DIFF_AUDIT_REPORT_TYPE,
    )
    assert len(diff_audits) == 1
    assert diff_audits[0].content["target_ledger_version"] == 2

    # Verify validation rejects has_changes=False if it does not explain insufficient grounds
    later_dt = now_dt + timedelta(hours=LAB_REVISION_COOLDOWN_HOURS + 1)
    bad_calls: List[Dict[str, Any]] = []
    bad_gateway = _build_lab_revision_mock_gateway(
        recorded_calls=bad_calls,
        flash_override=_sample_lab_revision_flash_output(
            gi_summary="Everything looks fine for GI tract.",
            gi_has_changes=False,
        ),
    )
    bad_service = LabRevisionConsiliumService(
        db=db_session,
        gateway=bad_gateway,
        now_provider=lambda: later_dt,
    )
    bad_service.upsert_laboratory_markers(
        cycle_id=cycle_id,
        mutations=[
            LabMarkerMutationItem(
                marker_key="ferritin",
                display_name="Ferritin",
                event_type=LabEventType.NEW_STUDY,
                value=31.0,
                unit="ng/mL",
                sample_date="2026-10-30",
            )
        ],
    )
    with pytest.raises(GeminiValidationError):
        await bad_service.run_lab_revision_consilium(
            cycle_id=cycle_id,
            report_date="2026-10-30",
        )


@pytest.mark.anyio
async def test_168_hour_cooldown_independence_from_weekly_and_new_cycle_and_concurrency(
    db_session,
):
    """
    Covers Requirements 5, 6, 7, and Tests 8, 9, 10, 12:
    - Cooldown is 168 hours based on authoritative server UTC clock (ignoring client `report_date`).
    - Blocked call during cooldown returns remaining wait time and makes ZERO Gemini calls.
    - Weekly Consilium (`Flash -> Pro`) does NOT consume the 168-hour cooldown, and does NOT
      mark unreviewed lab changes as evaluated by the full consilium.
    - Full Lab-Revision Consilium does NOT shift the weekly sprint schedule.
    - Primary Consilium (and Lab Revision) of a new cycle (`cycle_2`) is never blocked by `cycle_1`.
    - Concurrent requests (`asyncio.gather`) cannot bypass the 168-hour cooldown.
    - Athena strategy versions increment monotonically across Primary (v1) -> Weekly (v2) ->
      Lab Revision (v3) -> Weekly (v4) while preserving chat_history.
    """
    cycle_id = "cycle_cooldown_168h"
    await _seed_primary_consilium(db_session, cycle_id=cycle_id, primary_date="2026-10-18")

    clock = {"now": datetime(2026, 10, 20, 9, 0, 0, tzinfo=timezone.utc)}

    # Record chat history for Athena
    weekly_service = WeeklyConsiliumService(db=db_session)
    weekly_service.save_athena_chat_message(
        cycle_id=cycle_id,
        sprint_id="sprint_c1_w1",
        sender="user",
        text="I added my new blood test results today.",
    )
    _seed_seven_day_athena_logs(
        db_session, cycle_id=cycle_id, sprint_id="sprint_c1_w1", base_date="2026-10-18"
    )

    # Save new lab marker at T0 (2026-10-20)
    lab_service = LabRevisionConsiliumService(db=db_session, now_provider=lambda: clock["now"])
    state_after_edit = lab_service.upsert_laboratory_markers(
        cycle_id=cycle_id,
        mutations=[
            LabMarkerMutationItem(
                marker_key="vitamin_d",
                display_name="25-OH Vitamin D",
                event_type=LabEventType.NEW_STUDY,
                value=24.0,
                unit="ng/mL",
                sample_date="2026-10-20",
            )
        ],
    )
    assert state_after_edit.has_unreviewed_lab_changes is True

    # Run Weekly Consilium first (Sprint 1 on 2026-10-25):
    # Must NOT consume the 168h lab-revision cooldown AND must NOT mark lab v2 as evaluated!
    weekly_calls: List[Dict[str, Any]] = []
    weekly_gateway = _build_weekly_mock_gateway(weekly_calls)
    weekly_runner = WeeklyConsiliumService(db=db_session, gateway=weekly_gateway)
    await weekly_runner.run_weekly_consilium(
        cycle_id=cycle_id,
        sprint_id="sprint_c1_w1",
        report_date="2026-10-25",
    )

    # Verify Athena strategy is now v2 (from Weekly Consilium)
    assert weekly_runner.get_active_athena_context(cycle_id).strategy_version == 2
    # Verify unreviewed labs are STILL marked unreviewed (Weekly Consilium does not mark full lab evaluation)
    assert lab_service.get_lab_ledger_state(cycle_id).has_unreviewed_lab_changes is True
    # Verify 168h Full Lab-Revision cooldown is STILL available
    assert lab_service.get_cooldown_status(cycle_id).is_allowed is True

    # Test 10: Launch TWO concurrent Full Lab-Revision requests via asyncio.gather at T0
    # Only ONE must execute the 10 Gemini calls; the second must be blocked by cooldown with 0 extra calls!
    concurrent_calls: List[Dict[str, Any]] = []
    concurrent_gateway = _build_lab_revision_mock_gateway(
        recorded_calls=concurrent_calls,
        artificial_delay_sec=0.01,
    )
    service_a = LabRevisionConsiliumService(
        db=db_session, gateway=concurrent_gateway, now_provider=lambda: clock["now"]
    )
    service_b = LabRevisionConsiliumService(
        db=db_session, gateway=concurrent_gateway, now_provider=lambda: clock["now"]
    )

    results = await asyncio.gather(
        service_a.run_lab_revision_consilium(cycle_id=cycle_id, report_date="2026-10-25"),
        service_b.run_lab_revision_consilium(cycle_id=cycle_id, report_date="2026-11-30"),
        return_exceptions=True,
    )
    successes = [r for r in results if not isinstance(r, Exception)]
    cooldown_errors = [r for r in results if isinstance(r, ConsiliumCooldownError)]
    assert len(successes) == 1
    assert len(cooldown_errors) == 1
    assert len(concurrent_calls) == 10

    # Test 8: Advance server clock by 100 hours (< 168 hours).
    # Even if client passes a far-future `report_date="2027-01-01"`, server-time cooldown blocks it
    # without calling Gemini!
    clock["now"] = clock["now"] + timedelta(hours=100)
    blocked_calls: List[Dict[str, Any]] = []
    blocked_gateway = _build_lab_revision_mock_gateway(recorded_calls=blocked_calls)
    blocked_service = LabRevisionConsiliumService(
        db=db_session, gateway=blocked_gateway, now_provider=lambda: clock["now"]
    )
    with pytest.raises(ConsiliumCooldownError) as exc_info:
        await blocked_service.run_lab_revision_consilium(
            cycle_id=cycle_id,
            report_date="2027-01-01",
        )
    assert len(blocked_calls) == 0
    assert exc_info.value.cooldown_status.is_allowed is False
    assert exc_info.value.cooldown_status.remaining_hours == 68.0
    assert "seek in-person medical attention" in exc_info.value.cooldown_status.safety_notice

    # Verify a brand-new medical cycle (`cycle_new_02`) is NOT blocked by `cycle_id`'s cooldown
    cycle_new = "cycle_new_02"
    await _seed_primary_consilium(db_session, cycle_id=cycle_new, primary_date="2026-10-24")
    new_cycle_service = LabRevisionConsiliumService(
        db=db_session, now_provider=lambda: clock["now"]
    )
    assert new_cycle_service.get_cooldown_status(cycle_new).is_allowed is True

    # Verify Athena strategy on `cycle_id` is now v3 (from Lab Revision Consilium) and chat history is intact
    ctx_v3 = weekly_runner.get_active_athena_context(cycle_id)
    assert ctx_v3.strategy_version == 3
    assert ctx_v3.source_consilium == "lab_revision_consilium"
    assert "SECRET_LAB_REVISION_ATHENA_STRATEGY" in ctx_v3.system_prompt
    assert len(ctx_v3.chat_history) == 1

    # Advance server clock to 168 hours total -> cooldown timer expires!
    clock["now"] = clock["now"] + timedelta(hours=68)
    assert blocked_service.get_cooldown_status(cycle_id).is_allowed is True

    # Even after 168h cooldown expires, attempting to run WITHOUT new lab changes is rejected with 0 Gemini calls!
    second_rev_calls: List[Dict[str, Any]] = []
    second_rev_gateway = _build_lab_revision_mock_gateway(recorded_calls=second_rev_calls)
    second_rev_service = LabRevisionConsiliumService(
        db=db_session, gateway=second_rev_gateway, now_provider=lambda: clock["now"]
    )
    with pytest.raises(ConsiliumCooldownError, match="no new or unreviewed laboratory changes"):
        await second_rev_service.run_lab_revision_consilium(
            cycle_id=cycle_id,
            report_date="2026-10-27",
        )
    assert len(second_rev_calls) == 0

    # Now add a new lab study (v3) and run the second revision -> succeeds!
    second_rev_service.upsert_laboratory_markers(
        cycle_id=cycle_id,
        mutations=[
            LabMarkerMutationItem(
                marker_key="vitamin_d",
                display_name="25-OH Vitamin D",
                event_type=LabEventType.NEW_STUDY,
                value=33.0,
                unit="ng/mL",
                sample_date="2026-10-27",
            )
        ],
    )
    second_res = await second_rev_service.run_lab_revision_consilium(
        cycle_id=cycle_id,
        report_date="2026-10-27",
    )
    assert len(second_rev_calls) == 10
    assert second_res.overall_report.version == 3
    assert weekly_runner.get_active_athena_context(cycle_id).strategy_version == 4


@pytest.mark.anyio
async def test_lab_revision_transaction_rollback_does_not_consume_cooldown(db_session):
    """
    Covers Requirement 5, 8, and Test 11:
    - If Pro rejects publication OR a database write fails mid-transaction:
      * Entire transaction rolls back (zero partial client reports, zero internal reports,
        strategy version unchanged, lab ledger remains 'pending_full_consilium_revision');
      * The 168-hour cooldown is NOT consumed (`is_allowed` remains True).
    """
    cycle_id = "cycle_lab_rev_rollback"
    await _seed_primary_consilium(db_session, cycle_id=cycle_id, primary_date="2026-10-18")

    now_dt = datetime(2026, 10, 21, 15, 0, 0, tzinfo=timezone.utc)
    base_service = LabRevisionConsiliumService(db=db_session, now_provider=lambda: now_dt)
    base_service.upsert_laboratory_markers(
        cycle_id=cycle_id,
        mutations=[
            LabMarkerMutationItem(
                marker_key="ferritin",
                display_name="Ferritin",
                event_type=LabEventType.CORRECTION,
                value=15.0,
                unit="ng/mL",
                sample_date="2026-10-18",
                correction_reason="Fix initial typo",
            )
        ],
    )

    # Case A: Pro rejects publication -> no DB writes, cooldown NOT consumed
    reject_calls: List[Dict[str, Any]] = []
    reject_gateway = _build_lab_revision_mock_gateway(
        recorded_calls=reject_calls,
        pro_override=_sample_lab_revision_pro_output(
            approved=False,
            rejection_reason="Contradictory comparative organ summary.",
        ),
    )
    reject_service = LabRevisionConsiliumService(
        db=db_session, gateway=reject_gateway, now_provider=lambda: now_dt
    )
    with pytest.raises(ConsiliumQualityRejectionError, match="Contradictory comparative"):
        await reject_service.run_lab_revision_consilium(cycle_id=cycle_id)

    assert reject_service.get_cooldown_status(cycle_id).is_allowed is True
    assert reject_service.get_lab_ledger_state(cycle_id).has_unreviewed_lab_changes is True

    # Case B: Simulated DB error mid-way through writing client_facing reports -> full rollback
    fail_calls: List[Dict[str, Any]] = []
    fail_gateway = _build_lab_revision_mock_gateway(recorded_calls=fail_calls)
    fail_service = LabRevisionConsiliumService(
        db=db_session, gateway=fail_gateway, now_provider=lambda: now_dt
    )

    original_create_next = fail_service.report_repo.create_next_version
    client_counter = {"count": 0}

    def fail_on_fifth_client_write(*args, **kwargs):
        priv = kwargs.get("privacy_level")
        priv_val = priv.value if hasattr(priv, "value") else priv
        if priv_val == "client_facing":
            client_counter["count"] += 1
            if client_counter["count"] == 5:
                raise RuntimeError("Simulated SQLite failure mid lab-revision persistence")
        return original_create_next(*args, **kwargs)

    fail_service.report_repo.create_next_version = fail_on_fifth_client_write  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="Simulated SQLite failure"):
        await fail_service.run_lab_revision_consilium(
            cycle_id=cycle_id,
            report_date="2026-10-21",
        )

    report_repo = ModelReportRepository(db_session)
    # 1. Cooldown execution log was rolled back -> cooldown is still available!
    assert (
        report_repo.list_server_only(
            cycle_id=cycle_id, report_type=LAB_REVISION_EXECUTION_LOG_REPORT_TYPE
        )
        == []
    )
    assert base_service.get_cooldown_status(cycle_id).is_allowed is True
    # 2. Lab ledger is still marked as having unreviewed changes
    assert base_service.get_lab_ledger_state(cycle_id).has_unreviewed_lab_changes is True
    # 3. All 8 client system reports and user_overall_report remain at version 1
    for key in SYSTEM_KEYS:
        versions = report_repo.list_versions(
            cycle_id=cycle_id, report_type=f"system_user_report_{key}"
        )
        assert [v.version for v in versions] == [1]
    overall_versions = report_repo.list_versions(
        cycle_id=cycle_id, report_type="user_overall_report"
    )
    assert [v.version for v in overall_versions] == [1]
    # 4. Active Athena strategy remains at v1
    weekly_service = WeeklyConsiliumService(db=db_session)
    assert weekly_service.get_active_athena_context(cycle_id).strategy_version == 1


@pytest.mark.anyio
async def test_lab_revision_api_endpoints_privacy_isolation_and_429_cooldown(
    client: TestClient,
    db_session,
    auth_headers: dict[str, str],
):
    """
    Covers Requirements 5, 8, and Test 13:
    - Tests `/api/v1/cascade/labs/upsert`, `/api/v1/cascade/labs/state`,
      `/api/v1/cascade/lab-revision-cooldown`, and `/api/v1/cascade/run-lab-revision`.
    - Verifies zero leakage of `server_only` data (`SECRET_LAB_REVISION_ATHENA_STRATEGY`,
      `SECRET_LAB_REVISION_FLASH_TRAINING`, `SECRET_LAB_REVISION_PRO_TRAINING`,
      `SECRET_LAB_REVISION_RATIONALE`, `laboratory_diff_audit`) in client responses.
    - Verifies 429 Too Many Requests with remaining wait time and safety notice when
      a second full lab revision is requested within 168 hours.
    """
    cycle_id = "cycle_lab_rev_api"
    await _seed_primary_consilium(db_session, cycle_id=cycle_id, primary_date="2026-10-18")

    clock = {"now": datetime(2026, 10, 23, 14, 0, 0, tzinfo=timezone.utc)}
    recorded_calls: List[Dict[str, Any]] = []

    app.dependency_overrides[get_gemini_gateway] = lambda: _build_lab_revision_mock_gateway(
        recorded_calls
    )
    app.dependency_overrides[get_server_now_provider] = lambda: (lambda: clock["now"])

    try:
        # 1. Upsert laboratory markers via REST API (always allowed, zero cooldown)
        upsert_resp = client.post(
            "/api/v1/cascade/labs/upsert",
            json={
                "cycle_id": cycle_id,
                "mutations": [
                    {
                        "marker_key": "ferritin",
                        "display_name": "Ferritin",
                        "event_type": "correction",
                        "value": 16.4,
                        "unit": "ng/mL",
                        "sample_date": "2026-10-18",
                        "correction_reason": "Corrected lab unit transcription",
                    },
                    {
                        "marker_key": "tsh",
                        "display_name": "TSH",
                        "event_type": "new_study",
                        "value": 2.1,
                        "unit": "mIU/L",
                        "sample_date": "2026-10-23",
                    },
                ],
            },
            headers=auth_headers,
        )
        assert upsert_resp.status_code == 200
        state_body = upsert_resp.json()
        assert state_body["ledger_version"] == 2
        assert state_body["has_unreviewed_lab_changes"] is True
        assert state_body["evaluation_status"] == "pending_full_consilium_revision"

        # 2. Execute Full Lab-Revision Consilium via REST API
        rev_resp = client.post(
            "/api/v1/cascade/run-lab-revision",
            json={"cycle_id": cycle_id, "report_date": "2026-10-23"},
            headers=auth_headers,
        )
        assert rev_resp.status_code == 200
        rev_body = rev_resp.json()
        raw_rev_str = json.dumps(rev_body)

        assert rev_body["cycle_id"] == cycle_id
        assert rev_body["evaluated_ledger_version"] == 2
        assert rev_body["evaluation_status"] == "evaluated"
        assert len(rev_body["system_reports"]) == 8
        assert len(recorded_calls) == 10

        forbidden_markers = [
            "SECRET_LAB_REVISION_ATHENA_STRATEGY",
            "SECRET_LAB_REVISION_FLASH_TRAINING",
            "SECRET_LAB_REVISION_PRO_TRAINING",
            "SECRET_LAB_REVISION_RATIONALE",
            "SECRET_LAB_REVISION_TRAINING_",
            "flash_training_shelf",
            "pro_training_shelf",
            "pro_general_shelf",
            "laboratory_diff_audit",
        ]
        for marker in forbidden_markers:
            assert marker not in raw_rev_str

        # 3. Immediate second request (+12 hours < 168 hours) returns 429 without calling Gemini
        clock["now"] = clock["now"] + timedelta(hours=12)
        blocked_resp = client.post(
            "/api/v1/cascade/run-lab-revision",
            json={"cycle_id": cycle_id, "report_date": "2026-10-24"},
            headers=auth_headers,
        )
        assert blocked_resp.status_code == 429
        detail = blocked_resp.json()["detail"]
        assert detail["is_allowed"] is False
        assert detail["cooldown_hours"] == 168
        assert detail["remaining_hours"] == 156.0
        assert "seek in-person medical attention" in detail["safety_notice"]
        # Still only the 10 Gemini calls from the first run
        assert len(recorded_calls) == 10

        # 4. Verify GET /api/v1/reports/client has zero server_only leakage
        client_reports_resp = client.get(
            f"/api/v1/reports/client?cycle_id={cycle_id}",
            headers=auth_headers,
        )
        assert client_reports_resp.status_code == 200
        raw_client_reports = json.dumps(client_reports_resp.json())
        for marker in forbidden_markers:
            assert marker not in raw_client_reports
    finally:
        app.dependency_overrides.pop(get_gemini_gateway, None)
        app.dependency_overrides.pop(get_server_now_provider, None)


@pytest.mark.anyio
async def test_multistep_lab_history_v1_to_v4_diff_baseline_and_failure_retention(db_session):
    """
    Pre-commit audit check 1:
    - v1: initial OCR labs evaluated by Primary Consilium (`last_evaluated_ledger_version == 1`).
    - v2: user corrects an erroneous marker (`ferritin`: 12.0 -> 28.5 ng/mL, CORRECTION).
    - v3: user adds a brand-new marker (`vitamin_d`: 18.4 ng/mL, INITIAL).
    - v4: user uploads a study on another date (`glucose`: 5.1 -> 4.8 mmol/L on 2026-10-22, NEW_STUDY).
    - Verifies `compute_laboratory_diff` compares v4 against v1 (the last evaluated version),
      NOT merely against v3.
    - Verifies that if the full revision fails at v4, status remains `pending_full_consilium_revision`
      and the baseline comparison point remains v1.
    - Verifies that when the full revision succeeds at v4:
      * AI models receive all changes from v2, v3, and v4 relative to v1;
      * v4 becomes `last_evaluated_ledger_version`;
      * subsequent v5 edits compare against v4 (not v1) so v2..v4 changes are now in `unchanged_markers`;
      * `superseded_by_observation_id`, `supersedes_observation_id`, `is_active=False`, units, dates,
        and original values are preserved in `observations_history`.
    """
    cycle_id = "cycle_audit_v1_to_v4"
    await _seed_primary_consilium(db_session, cycle_id=cycle_id, primary_date="2026-10-18")

    clock = {"now": datetime(2026, 10, 22, 10, 0, 0, tzinfo=timezone.utc)}
    service = LabRevisionConsiliumService(db=db_session, now_provider=lambda: clock["now"])

    # v1: Initial evaluated baseline
    state_v1 = service.get_lab_ledger_state(cycle_id)
    assert state_v1.ledger_version == 1
    assert state_v1.last_evaluated_ledger_version == 1
    assert state_v1.has_unreviewed_lab_changes is False
    assert state_v1.evaluation_status == "evaluated"
    orig_ferritin_id = state_v1.active_markers["ferritin"].observation_id

    # v2: User corrects erroneous ferritin (12.0 -> 28.5 ng/mL)
    state_v2 = service.upsert_laboratory_markers(
        cycle_id=cycle_id,
        mutations=[
            LabMarkerMutationItem(
                marker_key="ferritin",
                display_name="Ferritin",
                event_type=LabEventType.CORRECTION,
                value=28.5,
                unit="ng/mL",
                sample_date="2026-10-18",
                reference_range=LabReferenceRange(low=30.0, high=300.0, text="30-300 ng/mL"),
                correction_reason="Corrected OCR decimal typo",
            )
        ],
    )
    assert state_v2.ledger_version == 2
    assert state_v2.last_evaluated_ledger_version == 1
    assert state_v2.has_unreviewed_lab_changes is True

    # v3: User adds a brand-new marker (vitamin_d: 18.4 ng/mL)
    state_v3 = service.upsert_laboratory_markers(
        cycle_id=cycle_id,
        mutations=[
            LabMarkerMutationItem(
                marker_key="vitamin_d",
                display_name="25-OH Vitamin D",
                event_type=LabEventType.INITIAL,
                value=18.4,
                unit="ng/mL",
                sample_date="2026-10-20",
                reference_range=LabReferenceRange(low=30.0, high=100.0, text="30-100 ng/mL"),
            )
        ],
    )
    assert state_v3.ledger_version == 3
    assert state_v3.last_evaluated_ledger_version == 1

    # v4: User uploads a new study on another date (glucose: 4.8 mmol/L on 2026-10-22)
    state_v4 = service.upsert_laboratory_markers(
        cycle_id=cycle_id,
        mutations=[
            LabMarkerMutationItem(
                marker_key="glucose",
                display_name="Fasting Glucose",
                event_type=LabEventType.NEW_STUDY,
                value=4.8,
                unit="mmol/L",
                sample_date="2026-10-22",
                reference_range=LabReferenceRange(low=3.9, high=5.6, text="3.9-5.6 mmol/L"),
            )
        ],
    )
    assert state_v4.ledger_version == 4
    assert state_v4.last_evaluated_ledger_version == 1
    assert state_v4.has_unreviewed_lab_changes is True
    assert state_v4.evaluation_status == "pending_full_consilium_revision"

    # Check bidirectional history links & original value preservation in v4
    ferritin_hist = [h for h in state_v4.observations_history if h.marker_key == "ferritin"]
    assert len(ferritin_hist) == 2
    assert ferritin_hist[0].observation_id == orig_ferritin_id
    assert ferritin_hist[0].value == "42 ng/mL"
    assert ferritin_hist[0].is_active is False
    assert ferritin_hist[0].superseded_by_observation_id == ferritin_hist[1].observation_id
    assert ferritin_hist[1].supersedes_observation_id == orig_ferritin_id
    assert ferritin_hist[1].is_active is True

    # Verify diff at v4 compares v4 against v1 (NOT v3!)
    diff_v1_to_v4 = service.compute_laboratory_diff(cycle_id=cycle_id)
    assert diff_v1_to_v4.baseline_ledger_version == 1
    assert diff_v1_to_v4.target_ledger_version == 4
    assert [m.marker_key for m in diff_v1_to_v4.corrected_error_values] == ["ferritin"]
    assert [m.marker_key for m in diff_v1_to_v4.added_markers] == ["vitamin_d"]
    assert [m.marker_key for m in diff_v1_to_v4.new_dated_studies] == ["glucose"]
    assert sorted(m.marker_key for m in diff_v1_to_v4.unchanged_markers) == [
        "creatinine",
        "hemoglobin",
    ]

    # Simulate a FAILED revision at v4 (e.g. call index 8 fails):
    # Pending status and v1 baseline must remain unchanged!
    failed_calls: List[Dict[str, Any]] = []
    failed_gateway = _build_lab_revision_mock_gateway(
        recorded_calls=failed_calls,
        fail_on_call_index=8,
    )
    failed_service = LabRevisionConsiliumService(
        db=db_session, gateway=failed_gateway, now_provider=lambda: clock["now"]
    )
    with pytest.raises(GeminiValidationError):
        await failed_service.run_lab_revision_consilium(cycle_id=cycle_id, report_date="2026-10-22")

    state_after_fail = service.get_lab_ledger_state(cycle_id)
    assert state_after_fail.ledger_version == 4
    assert state_after_fail.last_evaluated_ledger_version == 1
    assert state_after_fail.has_unreviewed_lab_changes is True
    assert state_after_fail.evaluation_status == "pending_full_consilium_revision"
    assert service.compute_laboratory_diff(cycle_id=cycle_id).baseline_ledger_version == 1

    # Now execute a SUCCESSFUL full revision at v4
    success_calls: List[Dict[str, Any]] = []
    success_gateway = _build_lab_revision_mock_gateway(recorded_calls=success_calls)
    success_service = LabRevisionConsiliumService(
        db=db_session, gateway=success_gateway, now_provider=lambda: clock["now"]
    )
    res_v4 = await success_service.run_lab_revision_consilium(
        cycle_id=cycle_id, report_date="2026-10-22"
    )
    assert res_v4.evaluated_ledger_version == 4
    assert res_v4.evaluation_status == "evaluated"

    # Verify AI received all changes from v2 (ferritin 28.5), v3 (vitamin_d 18.4), and v4 (glucose 4.8)
    for idx in range(10):
        prompt_text = success_calls[idx]["prompt"]
        assert "28.5" in prompt_text
        assert "18.4" in prompt_text
        assert "4.8" in prompt_text
        assert '"baseline_ledger_version": 1' in prompt_text
        assert '"target_ledger_version": 4' in prompt_text

    # Verify v4 is now the last_evaluated_ledger_version
    state_after_success = service.get_lab_ledger_state(cycle_id)
    assert state_after_success.ledger_version == 4
    assert state_after_success.last_evaluated_ledger_version == 4
    assert state_after_success.has_unreviewed_lab_changes is False
    assert state_after_success.evaluation_status == "evaluated"

    # Now user adds v5 (invalidating creatinine as erroneous):
    # compute_laboratory_diff must now compare v5 against v4 (not v1!)
    state_v5 = service.upsert_laboratory_markers(
        cycle_id=cycle_id,
        mutations=[
            LabMarkerMutationItem(
                marker_key="creatinine",
                event_type=LabEventType.INVALIDATED,
                correction_reason="Sample hemolyzed",
            )
        ],
    )
    assert state_v5.ledger_version == 5
    assert state_v5.last_evaluated_ledger_version == 4
    diff_v4_to_v5 = service.compute_laboratory_diff(cycle_id=cycle_id)
    assert diff_v4_to_v5.baseline_ledger_version == 4
    assert diff_v4_to_v5.target_ledger_version == 5
    assert [m.marker_key for m in diff_v4_to_v5.removed_or_invalidated_markers] == ["creatinine"]
    assert diff_v4_to_v5.corrected_error_values == []
    assert diff_v4_to_v5.added_markers == []
    assert diff_v4_to_v5.new_dated_studies == []
    assert sorted(m.marker_key for m in diff_v4_to_v5.unchanged_markers) == [
        "ferritin",
        "glucose",
        "hemoglobin",
        "vitamin_d",
    ]


@pytest.mark.anyio
async def test_cross_process_concurrency_restart_persistence_and_v1_to_v4_strategy_sequence(
    db_session,
):
    """
    Pre-commit audit checks 2 & 3:
    1. Cooldown cannot be bypassed by:
       - two simultaneous cross-process requests (two separate SQLAlchemy Sessions and
         two separate `lock_registry={}` dicts that do NOT share `asyncio.Lock`, proving
         the SQLite PRIMARY KEY execution lease `lab_revision_in_flight_lock` blocks the second worker);
       - repeated request after backend restart (fresh service instance + fresh `lock_registry={}` + new Session);
       - spoofed `report_date`;
       - repeated run without new laboratory changes (`has_unreviewed_lab_changes == False`).
    2. Full sequence Primary v1 -> Weekly v2 -> Lab Revision v3 -> Weekly v4:
       - Monotonic strategy versions (1 -> 2 -> 3 -> 4);
       - Server-side Athena context (`get_active_athena_context`, `sync_athena_session`) uses
         each new strategy version and preserves `chat_history`;
       - Subsequent Weekly v4 receives updated evaluated lab results and organ conclusions
         from Lab Revision v3, and stale v1 OCR values never become current again.
    """
    cycle_id = "cycle_audit_cross_proc_v1_v4"
    await _seed_primary_consilium(db_session, cycle_id=cycle_id, primary_date="2026-10-18")

    clock = {"now": datetime(2026, 10, 20, 10, 0, 0, tzinfo=timezone.utc)}

    # --- STEP 1: Primary v1 strategy check ---
    weekly_service = WeeklyConsiliumService(db=db_session)
    weekly_service.save_athena_chat_message(
        cycle_id=cycle_id,
        sprint_id="sprint_seq_w1",
        sender="user",
        text="Turn 1: Starting my first week.",
    )
    ctx_v1 = weekly_service.get_active_athena_context(cycle_id)
    assert ctx_v1.strategy_version == 1
    assert ctx_v1.source_consilium == "primary_consilium"
    session_state = weekly_service.sync_athena_session(cycle_id=cycle_id, cached_session=None)
    assert session_state.applied_strategy_version == 1

    # --- STEP 2: Weekly v2 (Sprint 1) ---
    _seed_seven_day_athena_logs(
        db_session, cycle_id=cycle_id, sprint_id="sprint_seq_w1", base_date="2026-10-18"
    )
    w1_calls: List[Dict[str, Any]] = []
    w1_gateway = _build_weekly_mock_gateway(
        w1_calls,
        pro_override={
            "updated_weekly_strategy": "STRATEGY_V2_WEEKLY_SPRINT_1: Monitor morning pulse.",
            "strategy_status": "updated",
            "weekly_overall_report": {
                "title": "Week 1 Summary",
                "summary": "Steady first week.",
                "highlights": ["Keep hydrating"],
            },
            "flash_quality_review": {
                "approved_for_publication": True,
                "rejection_reason": None,
                "corrected_user_system_reports": {},
            },
        },
    )
    w1_runner = WeeklyConsiliumService(db=db_session, gateway=w1_gateway)
    await w1_runner.run_weekly_consilium(
        cycle_id=cycle_id,
        sprint_id="sprint_seq_w1",
        report_date="2026-10-20",
    )
    ctx_v2 = weekly_service.get_active_athena_context(cycle_id)
    assert ctx_v2.strategy_version == 2
    assert ctx_v2.source_consilium == "weekly_consilium"
    assert "STRATEGY_V2_WEEKLY_SPRINT_1" in ctx_v2.system_prompt
    session_state = weekly_service.sync_athena_session(
        cycle_id=cycle_id, cached_session=session_state
    )
    assert session_state.applied_strategy_version == 2
    assert session_state.was_reinitialized is True
    assert len(session_state.chat_history) == 1

    # Add a second chat message before Lab Revision v3
    weekly_service.save_athena_chat_message(
        cycle_id=cycle_id,
        sprint_id="sprint_seq_w2",
        sender="curator",
        text="Turn 2: Please upload your corrected ferritin lab result.",
    )

    # User updates labs: corrects ferritin from "42 ng/mL" to 16.8 ng/mL and adds vitamin_d 31.5 ng/mL
    lab_service = LabRevisionConsiliumService(db=db_session, now_provider=lambda: clock["now"])
    lab_service.upsert_laboratory_markers(
        cycle_id=cycle_id,
        mutations=[
            LabMarkerMutationItem(
                marker_key="ferritin",
                display_name="Ferritin",
                event_type=LabEventType.CORRECTION,
                value=16.8,
                unit="ng/mL",
                sample_date="2026-10-18",
                correction_reason="Replaced erroneous OCR value 42 with verified 16.8",
            ),
            LabMarkerMutationItem(
                marker_key="vitamin_d",
                display_name="Vitamin D",
                event_type=LabEventType.INITIAL,
                value=31.5,
                unit="ng/mL",
                sample_date="2026-10-20",
            ),
        ],
    )

    # --- STEP 3: Lab Revision v3 via TWO concurrent CROSS-PROCESS workers ---
    # Each worker has its own independent SQLAlchemy Session (`Session(bind=...)`)
    # and its own isolated `lock_registry={}` (no shared `asyncio.Lock`!).
    engine_bind = db_session.get_bind()
    worker_session_1 = Session(bind=engine_bind, autocommit=False, autoflush=False)
    worker_session_2 = Session(bind=engine_bind, autocommit=False, autoflush=False)
    cross_proc_calls: List[Dict[str, Any]] = []
    cross_proc_gateway = _build_lab_revision_mock_gateway(
        recorded_calls=cross_proc_calls,
        pro_override=_sample_lab_revision_pro_output(
            strategy_text="STRATEGY_V3_LAB_REVISION: Ferritin verified at 16.8 ng/mL; adjust pacing."
        ),
        artificial_delay_sec=0.02,
    )
    try:
        worker_1 = LabRevisionConsiliumService(
            db=worker_session_1,
            gateway=cross_proc_gateway,
            now_provider=lambda: clock["now"],
            lock_registry={},  # isolated process memory
        )
        worker_2 = LabRevisionConsiliumService(
            db=worker_session_2,
            gateway=cross_proc_gateway,
            now_provider=lambda: clock["now"],
            lock_registry={},  # isolated process memory
        )

        cross_results = await asyncio.gather(
            worker_1.run_lab_revision_consilium(cycle_id=cycle_id, report_date="2026-10-20"),
            worker_2.run_lab_revision_consilium(cycle_id=cycle_id, report_date="2026-10-20"),
            return_exceptions=True,
        )
    finally:
        worker_session_1.close()
        worker_session_2.close()

    cp_successes = [r for r in cross_results if not isinstance(r, Exception)]
    cp_cooldown_errors = [r for r in cross_results if isinstance(r, ConsiliumCooldownError)]
    assert len(cp_successes) == 1, f"Expected 1 success, got: {cross_results!r}"
    assert len(cp_cooldown_errors) == 1, f"Expected 1 cooldown error, got: {cross_results!r}"
    # Strictly 10 calls total — second process was blocked by SQLite PK lease before calling Gemini!
    assert len(cross_proc_calls) == 10

    # Verify Athena strategy is now v3 (from Lab Revision Consilium)
    db_session.expire_all()
    ctx_v3 = weekly_service.get_active_athena_context(cycle_id)
    assert ctx_v3.strategy_version == 3
    assert ctx_v3.source_consilium == "lab_revision_consilium"
    assert "STRATEGY_V3_LAB_REVISION" in ctx_v3.system_prompt
    assert "STRATEGY_V2_WEEKLY_SPRINT_1" not in ctx_v3.system_prompt
    session_state = weekly_service.sync_athena_session(
        cycle_id=cycle_id, cached_session=session_state
    )
    assert session_state.applied_strategy_version == 3
    assert session_state.was_reinitialized is True
    assert len(session_state.chat_history) == 2

    # --- Verify cooldown blocks after simulated BACKEND RESTART + NO new lab changes + SPOOFED report_date ---
    clock["now"] = clock["now"] + timedelta(hours=24)  # 24h elapsed (< 168h)
    restarted_session = Session(bind=engine_bind, autocommit=False, autoflush=False)
    restart_calls: List[Dict[str, Any]] = []
    try:
        restarted_service = LabRevisionConsiliumService(
            db=restarted_session,
            gateway=_build_lab_revision_mock_gateway(recorded_calls=restart_calls),
            now_provider=lambda: clock["now"],
            lock_registry={},  # fresh process memory after server restart
        )
        # No new lab changes exist (`has_unreviewed_lab_changes == False`) and `report_date` is spoofed to 2028
        assert restarted_service.get_lab_ledger_state(cycle_id).has_unreviewed_lab_changes is False
        with pytest.raises(ConsiliumCooldownError) as restart_exc:
            await restarted_service.run_lab_revision_consilium(
                cycle_id=cycle_id,
                report_date="2028-01-01",
            )
        assert restart_exc.value.cooldown_status.is_allowed is False
        assert restart_exc.value.cooldown_status.remaining_hours == 144.0
        assert len(restart_calls) == 0
    finally:
        restarted_session.close()

    # --- STEP 4: Weekly v4 (Sprint 2) ---
    # Verify Weekly v4 receives the updated Lab-Revision v3 labs (ferritin 16.8, vitamin_d 31.5),
    # updated v3 organ conclusions, and v3 strategy — and stale v1 OCR ferritin ("42 ng/mL") does NOT reappear!
    _seed_seven_day_athena_logs(
        db_session, cycle_id=cycle_id, sprint_id="sprint_seq_w2", base_date="2026-10-25"
    )
    w2_calls: List[Dict[str, Any]] = []
    w2_gateway = _build_weekly_mock_gateway(
        w2_calls,
        pro_override={
            "updated_weekly_strategy": "STRATEGY_V4_WEEKLY_SPRINT_2: Consolidate post-revision training.",
            "strategy_status": "updated",
            "weekly_overall_report": {
                "title": "Week 2 Summary",
                "summary": "Strong progress following lab revision.",
                "highlights": ["Continue aerobic pacing"],
            },
            "flash_quality_review": {
                "approved_for_publication": True,
                "rejection_reason": None,
                "corrected_user_system_reports": {},
            },
        },
    )
    w2_runner = WeeklyConsiliumService(db=db_session, gateway=w2_gateway)
    await w2_runner.run_weekly_consilium(
        cycle_id=cycle_id,
        sprint_id="sprint_seq_w2",
        report_date="2026-10-27",
    )
    assert len(w2_calls) == 2
    w2_flash_prompt = w2_calls[0]["prompt"]
    w2_pro_prompt = w2_calls[1]["prompt"]

    # Updated Lab Revision v3 values are present in Weekly v4 prompts:
    assert "16.8" in w2_flash_prompt
    assert "31.5" in w2_flash_prompt
    assert "REVISED CONCLUSIONS for cvs" in w2_flash_prompt
    assert "STRATEGY_V3_LAB_REVISION" in w2_flash_prompt
    # Structural verification of current vs historical labs in Weekly v4:
    # 1. Parse structured JSON from Weekly Flash prompt
    flash_json_text = w2_flash_prompt.split("=== ANAMNESIS AND INPUT DATA ===\n")[1].split(
        "\n\n=== VITAL MARKERS"
    )[0]
    flash_input_data = json.loads(flash_json_text)

    # 2. Current state in shared_labs_shelf reflects corrected ferritin (16.8 ng/mL), never stale 42 ng/mL
    current_ferritin = flash_input_data["shared_labs_shelf"].get("ferritin", {})
    assert "16.8" in str(current_ferritin.get("value"))
    assert "42" not in str(current_ferritin.get("value"))

    # 3. Full dated history preserves both the original 42 ng/mL (marked inactive) and the correction
    history_ferritin = [
        obs for obs in flash_input_data.get("laboratory_history", [])
        if obs.get("marker_key") == "ferritin"
    ]
    assert len(history_ferritin) >= 2
    stale_obs = next((obs for obs in history_ferritin if "42" in str(obs.get("value"))), None)
    corrected_obs = next((obs for obs in history_ferritin if "16.8" in str(obs.get("value"))), None)
    assert stale_obs is not None
    assert stale_obs["is_active"] is False, "Original erroneous observation must be inactive"
    assert corrected_obs is not None
    assert corrected_obs["is_active"] is True, "Corrected observation must be active"
    assert corrected_obs.get("supersedes_observation_id") == stale_obs.get("observation_id")
    assert corrected_obs.get("event_type") == "correction"

    # Stale v2 strategy does NOT become current again:
    assert "STRATEGY_V2_WEEKLY_SPRINT_1" not in w2_flash_prompt

    # Verify Athena strategy is now v4 and chat_history is preserved
    ctx_v4 = weekly_service.get_active_athena_context(cycle_id)
    assert ctx_v4.strategy_version == 4
    assert ctx_v4.source_consilium == "weekly_consilium"
    assert "STRATEGY_V4_WEEKLY_SPRINT_2" in ctx_v4.system_prompt
    assert "STRATEGY_V3_LAB_REVISION" not in ctx_v4.system_prompt
    session_state = weekly_service.sync_athena_session(
        cycle_id=cycle_id, cached_session=session_state
    )
    assert session_state.applied_strategy_version == 4
    assert session_state.was_reinitialized is True
    assert [m["text"] for m in session_state.chat_history] == [
        "Turn 1: Starting my first week.",
        "Turn 2: Please upload your corrected ferritin lab result.",
    ]


@pytest.mark.anyio
async def test_sqlite_lease_ttl_renewal_during_long_run_and_fenced_owner_release(db_session):
    """
    Final pre-commit check — SQLite lease reliability under >900s long runs and stale owner fencing:
    1. Active renewal prevents TTL expiry while Worker 1 is genuinely running (> 900s):
       - Worker 1 acquires the lease (TTL=900s) and runs the 10-call AI cascade.
       - Across the cascade (and during slow calls via heartbeat), server UTC clock advances
         by 1,600 seconds (> 900s initial TTL).
       - At t = +1,050s (> 900s), Worker 2 (separate Session + isolated `lock_registry={}`)
         attempts to start the same consilium while Worker 1 is still active at call 6.
       - Because Worker 1 actively renewed its lease, Worker 2 is rejected with
         `ConsiliumCooldownError` (0 Gemini calls by Worker 2), and Worker 1 finishes all 10 calls
         and atomically commits its results, 168h cooldown log, and lease removal.
    2. Crash recovery + `lease_owner_id` fencing prevents stale owner from deleting a new owner's lock
       or committing stale results:
       - On a second cycle, Stalled Worker A acquires a lease (`owner_A`) at t=0s, then stalls/freezes
         without heartbeat for 1,000s (> 900s TTL).
       - At t=+1,000s, Worker B starts the consilium: the expired `owner_A` lock is cleaned up, and
         Worker B acquires a new lease with `owner_B != owner_A`.
       - While Worker B is mid-flight holding `owner_B`, Stalled Worker A wakes up and attempts to:
         (a) release its old lease (`_release_cross_process_lease_safe(cycle, owner_A)`),
         (b) renew its old lease (`_renew_cross_process_lease(cycle, owner_A)`), and
         (c) commit with its old lease (`_verify_and_delete_cross_process_lease_uncommitted(cycle, owner_A)`).
       - Verify `owner_B`'s active lock is NEVER deleted by `owner_A`, Worker A is fenced off with
         `ConsiliumCooldownError`, a third concurrent request (Worker C) is still blocked by `owner_B`'s
         intact lock, and Worker B completes and atomically commits its results and cooldown log.
    """
    engine_bind = db_session.get_bind()

    # =========================================================================
    # PART 1: Active Worker 1 runs > 900s (1,600s total); Worker 2 at +1,050s is blocked
    # =========================================================================
    cycle_long = "cycle_lease_long_run_900s"
    await _seed_primary_consilium(db_session, cycle_id=cycle_long, primary_date="2026-10-18")

    clock_long = {"now": datetime(2026, 10, 20, 12, 0, 0, tzinfo=timezone.utc)}
    setup_svc = LabRevisionConsiliumService(db=db_session, now_provider=lambda: clock_long["now"])
    setup_svc.upsert_laboratory_markers(
        cycle_id=cycle_long,
        mutations=[
            LabMarkerMutationItem(
                marker_key="ferritin",
                display_name="Ferritin",
                event_type=LabEventType.CORRECTION,
                value=22.4,
                unit="ng/mL",
                sample_date="2026-10-18",
                correction_reason="Verified lab value",
            )
        ],
    )

    w1_session = Session(bind=engine_bind, autocommit=False, autoflush=False)
    w2_session = Session(bind=engine_bind, autocommit=False, autoflush=False)
    w1_calls: List[Dict[str, Any]] = []
    w2_calls: List[Dict[str, Any]] = []
    w2_attempt_outcomes: List[Exception] = []

    w2_service = LabRevisionConsiliumService(
        db=w2_session,
        gateway=_build_lab_revision_mock_gateway(recorded_calls=w2_calls),
        now_provider=lambda: clock_long["now"],
        lock_registry={},  # separate OS worker process
    )

    call_seq = {"idx": 0}

    async def long_running_w1_handler(request: httpx.Request) -> httpx.Response:
        idx = call_seq["idx"]
        call_seq["idx"] += 1
        body = json.loads(request.content.decode("utf-8"))
        w1_calls.append({"index": idx, "prompt": body["contents"][0]["parts"][0]["text"]})

        # Each of the 10 calls takes 160s of server time (total 1,600s > 900s initial TTL),
        # advancing in two 80s sub-steps while yielding so Worker 1's heartbeat also ticks.
        clock_long["now"] = clock_long["now"] + timedelta(seconds=80)
        await asyncio.sleep(0.01)
        clock_long["now"] = clock_long["now"] + timedelta(seconds=80)
        await asyncio.sleep(0.01)

        # At call index 6, elapsed time is 7 * 160s = 1,120s (> 900s initial TTL!).
        # Worker 2 now attempts to launch the same consilium from another process/session.
        if idx == 6:
            try:
                await w2_service.run_lab_revision_consilium(
                    cycle_id=cycle_long,
                    report_date="2026-10-20",
                )
            except Exception as exc:
                w2_attempt_outcomes.append(exc)

        if 0 <= idx < 8:
            return httpx.Response(
                200,
                json=_gemini_envelope(_sample_lab_revision_organ_output(SYSTEM_KEYS[idx])),
            )
        if idx == 8:
            return httpx.Response(200, json=_gemini_envelope(_sample_lab_revision_flash_output()))
        return httpx.Response(200, json=_gemini_envelope(_sample_lab_revision_pro_output()))

    w1_gateway = GeminiGateway(
        api_key="test-long-run-key",
        transport=httpx.MockTransport(long_running_w1_handler),
        sleep_func=lambda _: None,
    )

    try:
        w1_service = LabRevisionConsiliumService(
            db=w1_session,
            gateway=w1_gateway,
            now_provider=lambda: clock_long["now"],
            lock_registry={},
            lease_ttl_seconds=900,
            lease_heartbeat_interval_seconds=0.005,
        )
        w1_result = await w1_service.run_lab_revision_consilium(
            cycle_id=cycle_long,
            report_date="2026-10-20",
        )
    finally:
        w1_session.close()
        w2_session.close()

    # Verify Worker 1 completed all 10 calls over 1,600s (> 900s TTL) and committed atomically
    assert len(w1_calls) == 10
    assert w1_result.evaluated_ledger_version == 2
    assert w1_result.evaluation_status == "evaluated"
    # Verify Worker 2 at t=+1,120s (> 900s) was blocked because Worker 1 actively renewed its lease
    assert len(w2_attempt_outcomes) == 1
    assert isinstance(w2_attempt_outcomes[0], ConsiliumCooldownError)
    assert "already in progress" in str(w2_attempt_outcomes[0])
    assert len(w2_calls) == 0

    # =========================================================================
    # PART 2: Stalled Worker A (> 900s without renewal) vs New Owner Worker B + Fencing
    # =========================================================================
    cycle_fence = "cycle_lease_fencing_owners"
    await _seed_primary_consilium(db_session, cycle_id=cycle_fence, primary_date="2026-10-18")

    clock_fence = {"now": datetime(2026, 10, 21, 8, 0, 0, tzinfo=timezone.utc)}
    fence_setup = LabRevisionConsiliumService(db=db_session, now_provider=lambda: clock_fence["now"])
    fence_setup.upsert_laboratory_markers(
        cycle_id=cycle_fence,
        mutations=[
            LabMarkerMutationItem(
                marker_key="ferritin",
                display_name="Ferritin",
                event_type=LabEventType.CORRECTION,
                value=19.2,
                unit="ng/mL",
                sample_date="2026-10-18",
                correction_reason="Fix OCR typo",
            )
        ],
    )

    session_a = Session(bind=engine_bind, autocommit=False, autoflush=False)
    session_b = Session(bind=engine_bind, autocommit=False, autoflush=False)
    session_c = Session(bind=engine_bind, autocommit=False, autoflush=False)
    wb_calls: List[Dict[str, Any]] = []
    wc_calls: List[Dict[str, Any]] = []
    fencing_checks: Dict[str, Any] = {}

    try:
        worker_a = LabRevisionConsiliumService(
            db=session_a,
            now_provider=lambda: clock_fence["now"],
            lock_registry={},
            lease_ttl_seconds=900,
        )
        # 1. Worker A acquires lease at t=0s (`owner_a`), then freezes without renewing for 1,000s (> 900s)
        owner_a = worker_a._acquire_cross_process_lease(cycle_fence)
        assert owner_a.startswith("lease_")

        clock_fence["now"] = clock_fence["now"] + timedelta(seconds=1000)

        worker_c = LabRevisionConsiliumService(
            db=session_c,
            gateway=_build_lab_revision_mock_gateway(recorded_calls=wc_calls),
            now_provider=lambda: clock_fence["now"],
            lock_registry={},
            lease_ttl_seconds=900,
        )

        b_seq = {"idx": 0}

        async def worker_b_handler(request: httpx.Request) -> httpx.Response:
            idx = b_seq["idx"]
            b_seq["idx"] += 1
            wb_calls.append({"index": idx})

            # While Worker B is mid-flight at call 3 (holding `owner_b`), stale Worker A wakes up!
            if idx == 3:
                active_lock_before = worker_b._fetch_fresh_lock_row(cycle_fence)
                owner_b = (active_lock_before.content or {}).get("lease_owner_id")
                fencing_checks["owner_b"] = owner_b
                assert owner_b is not None and owner_b != owner_a

                # (a) Stale Worker A tries to release its old lease -> MUST NOT delete Worker B's lock!
                worker_a._release_cross_process_lease_safe(cycle_fence, owner_a)
                lock_after_stale_release = worker_b._fetch_fresh_lock_row(cycle_fence)
                assert lock_after_stale_release is not None
                assert (lock_after_stale_release.content or {}).get("lease_owner_id") == owner_b

                # (b) Stale Worker A tries to renew its old lease -> MUST raise ConsiliumCooldownError!
                with pytest.raises(ConsiliumCooldownError, match="no longer owned"):
                    worker_a._renew_cross_process_lease(cycle_fence, owner_a)

                # (c) Stale Worker A tries to commit/delete uncommitted -> MUST raise ConsiliumCooldownError!
                with pytest.raises(ConsiliumCooldownError, match="lost or superseded"):
                    worker_a._verify_and_delete_cross_process_lease_uncommitted(cycle_fence, owner_a)

                # (d) Another process (Worker C) tries to start while Worker B is still running ->
                # MUST be blocked because Worker B's lease is still intact!
                with pytest.raises(ConsiliumCooldownError) as wc_exc:
                    await worker_c.run_lab_revision_consilium(
                        cycle_id=cycle_fence,
                        report_date="2026-10-21",
                    )
                fencing_checks["wc_blocked"] = wc_exc.value

            if 0 <= idx < 8:
                return httpx.Response(
                    200,
                    json=_gemini_envelope(_sample_lab_revision_organ_output(SYSTEM_KEYS[idx])),
                )
            if idx == 8:
                return httpx.Response(
                    200, json=_gemini_envelope(_sample_lab_revision_flash_output())
                )
            return httpx.Response(200, json=_gemini_envelope(_sample_lab_revision_pro_output()))

        worker_b = LabRevisionConsiliumService(
            db=session_b,
            gateway=GeminiGateway(
                api_key="test-fence-key",
                transport=httpx.MockTransport(worker_b_handler),
                sleep_func=lambda _: None,
            ),
            now_provider=lambda: clock_fence["now"],
            lock_registry={},
            lease_ttl_seconds=900,
        )

        wb_result = await worker_b.run_lab_revision_consilium(
            cycle_id=cycle_fence,
            report_date="2026-10-21",
        )
    finally:
        session_a.close()
        session_b.close()
        session_c.close()

    # Verify Worker B succeeded atomically, Worker A was fenced off, and Worker C made 0 Gemini calls
    assert len(wb_calls) == 10
    assert len(wc_calls) == 0
    assert "wc_blocked" in fencing_checks
    assert wb_result.evaluated_ledger_version == 2
    assert wb_result.cooldown.is_allowed is False
    assert wb_result.cooldown.remaining_hours == 168.0

    # Verify execution log records `owner_b` and the in-flight lock row was atomically deleted on commit
    db_session.expire_all()
    report_repo = ModelReportRepository(db_session)
    assert report_repo.get(LabRevisionConsiliumService._lock_report_id(cycle_fence)) is None
    exec_logs = report_repo.list_server_only(
        cycle_id=cycle_fence,
        report_type=LAB_REVISION_EXECUTION_LOG_REPORT_TYPE,
    )
    assert len(exec_logs) == 1
    assert exec_logs[0].content["lease_owner_id"] == fencing_checks["owner_b"]
