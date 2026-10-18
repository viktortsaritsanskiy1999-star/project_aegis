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
from app.routers.cascade import get_gemini_gateway
from app.schemas.consilium import SYSTEM_KEYS
from app.services.gemini_gateway import GeminiGateway, GeminiValidationError
from app.services.primary_consilium_service import (
    PrimaryConsiliumService,
    ConsiliumQualityRejectionError,
)
from app.services.weekly_consilium_service import (
    WeeklyConsiliumService,
    ATHENA_CLINICAL_LOG_REPORT_TYPE,
    ATHENA_ACTIVE_STRATEGY_REPORT_TYPE,
    WEEKLY_DEPUTY_INTERNAL_REPORT_TYPE,
    WEEKLY_CHIEF_INTERNAL_REPORT_TYPE,
    WEEKLY_OVERALL_CLIENT_REPORT_TYPE,
)
from tests.test_primary_consilium import (
    _gemini_envelope,
    _seed_preparatory_cycle_context,
    _build_consilium_mock_gateway,
)


def _sample_weekly_flash_output(
    cvs_summary: str | None = None,
    endocrine_summary: str | None = None,
) -> Dict[str, Any]:
    reports: Dict[str, Dict[str, Any]] = {}
    for key in SYSTEM_KEYS:
        if key in ("cvs", "nervous"):
            reports[key] = {
                "summary": f"Weekly update for {key}: positive adaptation to walking routine and steadier evening pulse.",
                "has_changes": True,
            }
        else:
            reports[key] = {
                "summary": (
                    f"No new observations or notable changes were recorded for {key} over the past 7 days. "
                    "Continue routine wellness tracking."
                ),
                "has_changes": False,
            }

    if cvs_summary is not None:
        reports["cvs"]["summary"] = cvs_summary
    if endocrine_summary is not None:
        reports["endocrine"]["summary"] = endocrine_summary

    return {
        "weekly_deputy_analysis": (
            "WEEK/WEEKLY SUMMARY:\n\n"
            "1. SYMPTOM DYNAMICS OVER 7 DAYS:\n"
            "Resting heart rate stabilized around 62-64 bpm; evening fatigue decreased.\n\n"
            "2. BOTTLENECKS AND DEVIATIONS:\n"
            "Mild sleep latency on Day 4 after late caffeine intake.\n\n"
            "3. ADJUSTMENT PROPOSAL:\n"
            "Maintain aerobic pacing and shift Athena's focus to sleep hygiene and hydration timing."
        ),
        "user_system_reports": reports,
    }


def _sample_weekly_pro_output(
    strategy_text: str | None = None,
    strategy_status: str = "updated",
    approved: bool = True,
    rejection_reason: str | None = None,
    corrected_reports: Dict[str, str] | None = None,
    overall_title: str = "Aegis Week 1 Wellness Summary",
) -> Dict[str, Any]:
    default_strategy = (
        "UPDATED WEEKLY STRATEGY (WEEK 2 PRIORITIES FOR ATHENA):\n"
        "1. ATTENTION FOCUS: Monitor evening caffeine cutoff, sleep onset latency, and post-walk pulse recovery.\n"
        "2. REQUIRED CLARIFICATIONS: Clarify hydration volume in the afternoon and any orthostatic dizziness upon waking.\n"
        "3. TRACKED CHANGES: Track resting pulse stability (target <68 bpm) and subjective energy score (1-10).\n"
        "4. RESPONSE EVALUATION RULES:\n"
        "   - HIGH-QUALITY response: Record the fact and advance to the next daily milestone.\n"
        "   - LOW-QUALITY / unclear / contradictory response: Do NOT advance; break down the question with simple options.\n"
        "5. DAILY QUESTION LIMIT: Strictly no more than 3 clarifying questions per day (1 routine daily pulse question)."
    )
    return {
        "updated_weekly_strategy": strategy_text if strategy_text is not None else default_strategy,
        "strategy_status": strategy_status,
        "weekly_overall_report": {
            "title": overall_title,
            "summary": (
                "Great progress over the past 7 days: resting heart rate and energy levels showed steady improvement. "
                "For the upcoming week, focus on earlier evening wind-down and consistent hydration."
            ),
            "highlights": [
                "Keep evening caffeine at least 6 hours before sleep",
                "Continue 30-minute moderate walks while tracking pulse recovery",
            ],
        },
        "flash_quality_review": {
            "approved_for_publication": approved,
            "rejection_reason": rejection_reason,
            "corrected_user_system_reports": corrected_reports or {},
        },
    }


def _build_weekly_mock_gateway(
    recorded_calls: List[Dict[str, Any]],
    flash_override: Dict[str, Any] | None = None,
    pro_override: Dict[str, Any] | None = None,
    fail_on_call_index: int | None = None,
) -> GeminiGateway:
    """
    Mock GeminiGateway for Stage 3.3 Weekly Consilium.
    Expects strictly 2 calls:
    - Call 0: Gemini Flash (Weekly Deputy Chief Physician)
    - Call 1: Gemini Pro (Weekly Chief Physician)
    Raises AssertionError if Flash-Lite or any extra call is made.
    """
    call_counter = {"index": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
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

        assert "flash-lite" not in url_path, (
            f"Forbidden Flash-Lite call detected during Weekly Consilium: {url_path}"
        )

        if fail_on_call_index is not None and idx == fail_on_call_index:
            return httpx.Response(
                400,
                json={"error": {"message": f"Injected weekly failure at call index {idx}"}},
            )

        if idx == 0:
            payload = flash_override or _sample_weekly_flash_output()
            return httpx.Response(200, json=_gemini_envelope(payload))

        if idx == 1:
            payload = pro_override or _sample_weekly_pro_output()
            return httpx.Response(200, json=_gemini_envelope(payload))

        raise AssertionError(f"Unexpected extra Gemini call at index {idx}: {url_path}")

    return GeminiGateway(
        api_key="test-weekly-consilium-key",
        transport=httpx.MockTransport(handler),
        sleep_func=lambda _: None,
    )


async def _seed_completed_primary_consilium(
    db_session,
    cycle_id: str = "cycle_weekly_01",
    primary_date: str = "2026-10-18",
) -> None:
    _seed_preparatory_cycle_context(db_session, cycle_id=cycle_id)
    primary_calls: List[Dict[str, Any]] = []
    primary_gateway = _build_consilium_mock_gateway(primary_calls)
    primary_service = PrimaryConsiliumService(db=db_session, gateway=primary_gateway)
    await primary_service.run_primary_consilium(
        cycle_id=cycle_id,
        report_date=primary_date,
    )


def _seed_seven_day_athena_logs(
    db_session,
    cycle_id: str,
    sprint_id: str = "sprint_w1",
    base_date: str = "2026-10-18",
) -> None:
    weekly_service = WeeklyConsiliumService(db=db_session)
    start_dt = datetime.strptime(base_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    for day in range(1, 8):
        log_dt = start_dt + timedelta(days=day)
        weekly_service.save_athena_clinical_log(
            cycle_id=cycle_id,
            sprint_id=sprint_id,
            log_entry={
                "status": "ALERT" if day == 4 else "NORMAL",
                "symptoms_mentioned": ["late sleep"] if day == 4 else [],
                "subjective_wellbeing_score": 6 if day == 4 else 8,
                "adherence_to_plan": "MEDIUM" if day == 4 else "HIGH",
                "red_flags": False,
                "summary_for_master": f"Day {day} Athena observation: pulse calm, adherence tracked.",
                "timestamp": log_dt.strftime("%Y-%m-%dT20:00:00Z"),
                "sprint_day": day,
            },
        )


@pytest.mark.anyio
async def test_weekly_consilium_strictly_two_calls_flash_then_pro_and_no_flash_lite(
    db_session,
):
    """
    Verifies:
    1. Strictly 2 Gemini calls in order: Gemini Flash (Deputy) -> Gemini Pro (Chief Physician).
    2. Zero calls to the 8 Flash-Lite organ specialists, Anamnesis M1, or Vision.
    3. Accurate 7-day Athena diary is passed to both Weekly Flash and Weekly Pro.
    """
    cycle_id = "cycle_weekly_two_calls"
    sprint_id = "sprint_w1"
    await _seed_completed_primary_consilium(db_session, cycle_id=cycle_id)
    _seed_seven_day_athena_logs(db_session, cycle_id=cycle_id, sprint_id=sprint_id)

    weekly_calls: List[Dict[str, Any]] = []
    weekly_gateway = _build_weekly_mock_gateway(weekly_calls)
    weekly_service = WeeklyConsiliumService(db=db_session, gateway=weekly_gateway)

    result = await weekly_service.run_weekly_consilium(
        cycle_id=cycle_id,
        sprint_id=sprint_id,
        report_date="2026-10-25",
    )

    # 1. Strictly 2 calls: Flash -> Pro
    assert len(weekly_calls) == 2
    assert weekly_gateway.flash_model in weekly_calls[0]["url_path"]
    assert "flash-lite" not in weekly_calls[0]["url_path"]
    assert weekly_gateway.pro_model in weekly_calls[1]["url_path"]

    # 2. Both Flash and Pro receive the 7-day Athena diary and previous conclusions
    flash_prompt = weekly_calls[0]["prompt"]
    pro_prompt = weekly_calls[1]["prompt"]
    assert "=== CURATOR CLINICAL OBSERVATION DIARY (ATHENA LOGS) ===" in flash_prompt
    assert "=== CURATOR CLINICAL OBSERVATION DIARY (ATHENA LOGS) ===" in pro_prompt
    for day in range(1, 8):
        assert f"Day {day} Athena observation" in flash_prompt
        assert f"Day {day} Athena observation" in pro_prompt
    assert "SECRET_ATHENA_STRATEGY" in flash_prompt
    assert "SYMPTOM DYNAMICS OVER 7 DAYS" in pro_prompt

    # 3. Verify response structure
    assert result.cycle_id == cycle_id
    assert result.sprint_id == sprint_id
    assert result.sprint_number == 1
    assert result.report_date == "2026-10-25"
    assert set(result.system_reports.keys()) == set(SYSTEM_KEYS)
    assert result.overall_report.report_type == WEEKLY_OVERALL_CLIENT_REPORT_TYPE


@pytest.mark.anyio
async def test_eight_dated_entries_friendly_no_change_messages_and_pro_correction(
    db_session,
):
    """
    Verifies:
    1. All 8 Atlas systems receive a new dated entry ('2026-10-25') appended to their entries history.
    2. Systems without new changes have has_changes=False and a friendly message without claiming
       guaranteed health invariance.
    3. False guarantees ('guaranteed unchanged' / 'гарантированно не изменилось') are rejected.
    4. Pro's quality review corrections deterministically overwrite Flash's weekly draft before publication.
    """
    cycle_id = "cycle_weekly_dated_entries"
    await _seed_completed_primary_consilium(
        db_session, cycle_id=cycle_id, primary_date="2026-10-18"
    )
    _seed_seven_day_athena_logs(db_session, cycle_id=cycle_id, sprint_id="sprint_w1")

    flash_mistake = "UNVERIFIED_WEEKLY_FLASH_CLAIM: Acute arrhythmia suspected."
    pro_fixed_cvs = (
        "Weekly cardiovascular tracking shows steadier evening heart rate (62-64 bpm) "
        "during routine walks."
    )

    weekly_calls: List[Dict[str, Any]] = []
    weekly_gateway = _build_weekly_mock_gateway(
        recorded_calls=weekly_calls,
        flash_override=_sample_weekly_flash_output(cvs_summary=flash_mistake),
        pro_override=_sample_weekly_pro_output(
            corrected_reports={"cvs": pro_fixed_cvs}
        ),
    )
    weekly_service = WeeklyConsiliumService(db=db_session, gateway=weekly_gateway)

    res = await weekly_service.run_weekly_consilium(
        cycle_id=cycle_id,
        sprint_id="sprint_w1",
        report_date="2026-10-25",
    )

    # CVS has Pro's corrected summary and 2 chronological dated entries (primary + week 1)
    cvs_rep = res.system_reports["cvs"]
    assert cvs_rep.version == 2
    assert cvs_rep.content.summary == pro_fixed_cvs
    assert cvs_rep.content.has_changes is True
    assert len(cvs_rep.content.entries) == 2
    assert cvs_rep.content.entries[0].date == "2026-10-18"
    assert cvs_rep.content.entries[1].date == "2026-10-25"
    assert cvs_rep.content.entries[1].summary == pro_fixed_cvs
    assert "UNVERIFIED_WEEKLY_FLASH_CLAIM" not in cvs_rep.model_dump_json()

    # GI tract had no new changes -> has_changes is False with friendly message
    gi_rep = res.system_reports["gi_tract"]
    assert gi_rep.content.has_changes is False
    assert "No new observations or notable changes" in gi_rep.content.summary
    assert "guaranteed unchanged" not in gi_rep.content.summary.lower()
    assert len(gi_rep.content.entries) == 2
    assert gi_rep.content.entries[1].has_changes is False

    # Verify that claiming "health is guaranteed unchanged" is rejected by validation
    bad_calls: List[Dict[str, Any]] = []
    bad_gateway = _build_weekly_mock_gateway(
        recorded_calls=bad_calls,
        flash_override=_sample_weekly_flash_output(
            endocrine_summary="Your endocrine health is guaranteed unchanged this week."
        ),
    )
    bad_service = WeeklyConsiliumService(db=db_session, gateway=bad_gateway)
    with pytest.raises(GeminiValidationError):
        await bad_service.run_weekly_consilium(
            cycle_id=cycle_id,
            sprint_id="sprint_w2_invalid",
            report_date="2026-11-01",
        )


@pytest.mark.anyio
async def test_athena_active_strategy_update_and_session_reinit_preserves_chat_history(
    db_session,
):
    """
    Verifies Requirement 4 & Pre-Commit Audit Item 1 & Item 3:
    - Before any Weekly Consilium runs, Stage 3.2 has saved `pro_general_shelf` in
      `chief_physician_internal` (with zero `athena_active_strategy` rows yet).
      `get_active_athena_context()` and `sync_athena_session()` accurately initialize v1 from
      `chief_physician_internal`.
    - Proves the exact sequence v1 (Primary Consilium) -> v2 (Week 1) -> v3 (Week 2):
      at every stage `get_active_athena_context()` and `sync_athena_session()` return ONLY
      the latest active strategy and never use an older strategy after a version bump.
    - Subsequent Athena interactions automatically pick up the updated strategy and force
      system_prompt refresh when strategy_version increments, while avoiding unnecessary
      re-init when strategy_version is unchanged.
    - Zero chat history is lost across strategy updates.
    """
    cycle_id = "cycle_athena_handoff"
    await _seed_completed_primary_consilium(db_session, cycle_id=cycle_id)

    report_repo = ModelReportRepository(db_session)
    # Confirm Stage 3.2 did not create a separate athena_active_strategy row yet
    assert (
        report_repo.list_server_only(
            cycle_id=cycle_id, report_type=ATHENA_ACTIVE_STRATEGY_REPORT_TYPE
        )
        == []
    )

    weekly_service = WeeklyConsiliumService(db=db_session)

    # 1. Stage v1: Before first Weekly Consilium, v1 is resolved from chief_physician_internal
    ctx_v1 = weekly_service.get_active_athena_context(cycle_id=cycle_id)
    assert ctx_v1.strategy_version == 1
    assert ctx_v1.source_consilium == "primary_consilium"
    assert ctx_v1.strategy_status == "initial"
    assert "SECRET_ATHENA_STRATEGY" in ctx_v1.active_strategy_text
    assert "SECRET_ATHENA_STRATEGY" in ctx_v1.system_prompt

    session_v1 = weekly_service.sync_athena_session(cycle_id=cycle_id, cached_session=None)
    assert session_v1.applied_strategy_version == 1
    assert session_v1.was_reinitialized is True
    assert "SECRET_ATHENA_STRATEGY" in session_v1.system_prompt

    # Subsequent call on same strategy v1 does NOT unnecessarily reinit
    session_v1_repeat = weekly_service.sync_athena_session(
        cycle_id=cycle_id, cached_session=session_v1
    )
    assert session_v1_repeat.applied_strategy_version == 1
    assert session_v1_repeat.was_reinitialized is False

    # 2. Record multi-turn chat history and 7-day logs during Sprint 1
    weekly_service.save_athena_chat_message(
        cycle_id=cycle_id,
        sprint_id="sprint_w1",
        sender="curator",
        text="Hello! I am Athena. How did you sleep last night?",
    )
    weekly_service.save_athena_chat_message(
        cycle_id=cycle_id,
        sprint_id="sprint_w1",
        sender="user",
        text="I slept 7.5 hours and felt much more rested today.",
    )
    _seed_seven_day_athena_logs(
        db_session,
        cycle_id=cycle_id,
        sprint_id="sprint_w1",
        base_date="2026-10-18",
    )

    # 3. Run Weekly Consilium Sprint 1 -> materializes v1 baseline and saves v2 strategy
    calls_w1: List[Dict[str, Any]] = []
    gateway_w1 = _build_weekly_mock_gateway(calls_w1)
    service_w1 = WeeklyConsiliumService(db=db_session, gateway=gateway_w1)
    await service_w1.run_weekly_consilium(
        cycle_id=cycle_id,
        sprint_id="sprint_w1",
        report_date="2026-10-25",
    )

    # 4. Stage v2: Active context and session must return v2 and never stale v1
    ctx_v2 = service_w1.get_active_athena_context(cycle_id=cycle_id)
    assert ctx_v2.strategy_version == 2
    assert ctx_v2.sprint_id == "sprint_w1"
    assert ctx_v2.strategy_status == "updated"
    assert "UPDATED WEEKLY STRATEGY (WEEK 2 PRIORITIES FOR ATHENA)" in ctx_v2.system_prompt
    assert "SECRET_ATHENA_STRATEGY" not in ctx_v2.system_prompt

    session_v2 = service_w1.sync_athena_session(
        cycle_id=cycle_id,
        cached_session=session_v1_repeat,
    )
    assert session_v2.applied_strategy_version == 2
    assert session_v2.sprint_id == "sprint_w1"
    assert session_v2.was_reinitialized is True
    assert "UPDATED WEEKLY STRATEGY (WEEK 2 PRIORITIES FOR ATHENA)" in session_v2.system_prompt
    assert "SECRET_ATHENA_STRATEGY" not in session_v2.system_prompt
    assert len(session_v2.chat_history) == 2
    assert session_v2.chat_history[0]["sender"] == "curator"
    assert session_v2.chat_history[1]["text"] == "I slept 7.5 hours and felt much more rested today."

    # Calling sync_athena_session again on v2 does not re-init
    session_v2_repeat = service_w1.sync_athena_session(
        cycle_id=cycle_id,
        cached_session=session_v2,
    )
    assert session_v2_repeat.applied_strategy_version == 2
    assert session_v2_repeat.was_reinitialized is False

    # 5. Run Weekly Consilium Sprint 2 -> updates strategy to v3 ('continued')
    _seed_seven_day_athena_logs(
        db_session,
        cycle_id=cycle_id,
        sprint_id="sprint_w2",
        base_date="2026-10-25",
    )
    continued_strategy_text = (
        "CONTINUED WEEKLY STRATEGY (CONFIRMED FOR WEEK 3):\n"
        "Current route remains effective; continue tracking evening caffeine cutoff, "
        "hydration, and post-walk pulse recovery with max 3 clarifying questions/day."
    )
    calls_w2: List[Dict[str, Any]] = []
    gateway_w2 = _build_weekly_mock_gateway(
        recorded_calls=calls_w2,
        pro_override=_sample_weekly_pro_output(
            strategy_text=continued_strategy_text,
            strategy_status="continued",
            overall_title="Aegis Week 2 Wellness Summary",
        ),
    )
    service_w2 = WeeklyConsiliumService(db=db_session, gateway=gateway_w2)
    await service_w2.run_weekly_consilium(
        cycle_id=cycle_id,
        sprint_id="sprint_w2",
        report_date="2026-11-01",
    )

    # 6. Stage v3: Active context and session must return v3 and never stale v1 or v2
    ctx_v3 = service_w2.get_active_athena_context(cycle_id)
    assert ctx_v3.strategy_version == 3
    assert ctx_v3.sprint_id == "sprint_w2"
    assert ctx_v3.strategy_status == "continued"
    assert continued_strategy_text in ctx_v3.system_prompt
    assert "UPDATED WEEKLY STRATEGY (WEEK 2 PRIORITIES FOR ATHENA)" not in ctx_v3.system_prompt
    assert "SECRET_ATHENA_STRATEGY" not in ctx_v3.system_prompt
    assert len(ctx_v3.chat_history) == 2

    session_v3 = service_w2.sync_athena_session(
        cycle_id=cycle_id,
        cached_session=session_v2_repeat,
    )
    assert session_v3.applied_strategy_version == 3
    assert session_v3.sprint_id == "sprint_w2"
    assert session_v3.was_reinitialized is True
    assert continued_strategy_text in session_v3.system_prompt
    assert "UPDATED WEEKLY STRATEGY (WEEK 2 PRIORITIES FOR ATHENA)" not in session_v3.system_prompt
    assert len(session_v3.chat_history) == 2

    # Verify all 3 strategy versions (v1 primary, v2 sprint_w1, v3 sprint_w2) are preserved in DB
    strategy_versions = report_repo.list_versions(
        cycle_id=cycle_id,
        report_type=ATHENA_ACTIVE_STRATEGY_REPORT_TYPE,
    )
    assert [r.version for r in strategy_versions] == [1, 2, 3]
    assert strategy_versions[0].content["source_consilium"] == "primary_consilium"
    assert strategy_versions[1].sprint_id == "sprint_w1"
    assert strategy_versions[2].sprint_id == "sprint_w2"

    # Verify medical cycle remains active after completing both weekly sprints
    cycle_repo = MedicalCycleRepository(db_session)
    cycle = cycle_repo.get(cycle_id)
    assert cycle is not None
    assert cycle.status == "active"
    assert cycle.closed_at is None


@pytest.mark.anyio
async def test_weekly_transaction_rollback_on_db_error_or_pro_rejection(db_session):
    """
    Verifies Requirement 5 & 6.10:
    - If a database error occurs mid-way through persisting the weekly consilium results,
      the entire transaction rolls back:
      * Zero weekly client_facing reports are published;
      * Zero weekly server_only reports remain;
      * The active sprint remains 'active' (not prematurely committed as 'completed');
      * Athena's active strategy remains at version 1.
    - Same atomic guarantee holds if Pro rejects publication (approved_for_publication=False).
    """
    cycle_id = "cycle_weekly_rollback"
    sprint_id = "sprint_rollback_01"
    await _seed_completed_primary_consilium(db_session, cycle_id=cycle_id)

    sprint_repo = WeeklySprintRepository(db_session)
    sprint_repo.create(
        sprint_id=sprint_id,
        cycle_id=cycle_id,
        sprint_number=1,
        status="active",
    )
    _seed_seven_day_athena_logs(db_session, cycle_id=cycle_id, sprint_id=sprint_id)

    # Case A: Pro rejects publication -> zero weekly reports saved, sprint remains active
    calls_reject: List[Dict[str, Any]] = []
    gateway_reject = _build_weekly_mock_gateway(
        recorded_calls=calls_reject,
        pro_override=_sample_weekly_pro_output(
            approved=False,
            rejection_reason="Contradictory weekly symptom summary; unsafe for user publication.",
        ),
    )
    service_reject = WeeklyConsiliumService(db=db_session, gateway=gateway_reject)
    with pytest.raises(ConsiliumQualityRejectionError, match="Contradictory weekly"):
        await service_reject.run_weekly_consilium(
            cycle_id=cycle_id,
            sprint_id=sprint_id,
            report_date="2026-10-25",
        )

    report_repo = ModelReportRepository(db_session)
    assert (
        report_repo.list_client_facing(
            cycle_id=cycle_id, report_type=WEEKLY_OVERALL_CLIENT_REPORT_TYPE
        )
        == []
    )
    assert sprint_repo.get(sprint_id).status == "active"

    # Case B: Simulated DB error mid-way through saving the 8 client system updates
    calls_db_fail: List[Dict[str, Any]] = []
    gateway_db_fail = _build_weekly_mock_gateway(recorded_calls=calls_db_fail)
    service_db_fail = WeeklyConsiliumService(db=db_session, gateway=gateway_db_fail)

    original_create_next_version = service_db_fail.report_repo.create_next_version
    client_write_counter = {"count": 0}

    def fail_on_fourth_client_report(*args, **kwargs):
        priv = kwargs.get("privacy_level")
        priv_val = priv.value if hasattr(priv, "value") else priv
        if priv_val == "client_facing":
            client_write_counter["count"] += 1
            if client_write_counter["count"] == 4:
                raise RuntimeError("Simulated SQLite write error during weekly persistence")
        return original_create_next_version(*args, **kwargs)

    service_db_fail.report_repo.create_next_version = fail_on_fourth_client_report  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="Simulated SQLite write error"):
        await service_db_fail.run_weekly_consilium(
            cycle_id=cycle_id,
            sprint_id=sprint_id,
            report_date="2026-10-25",
        )

    # Verify complete rollback:
    # 1. No weekly overall report exists
    assert (
        report_repo.list_client_facing(
            cycle_id=cycle_id, report_type=WEEKLY_OVERALL_CLIENT_REPORT_TYPE
        )
        == []
    )
    # 2. All 8 system_user_report_<key> remain at version 1 (from Primary Consilium)
    for key in SYSTEM_KEYS:
        versions = report_repo.list_versions(
            cycle_id=cycle_id,
            report_type=f"system_user_report_{key}",
        )
        assert [v.version for v in versions] == [1]

    # 3. No weekly_deputy_internal, weekly_chief_physician_internal, or athena_active_strategy rows remain
    assert (
        report_repo.list_server_only(
            cycle_id=cycle_id, report_type=WEEKLY_DEPUTY_INTERNAL_REPORT_TYPE
        )
        == []
    )
    assert (
        report_repo.list_server_only(
            cycle_id=cycle_id, report_type=WEEKLY_CHIEF_INTERNAL_REPORT_TYPE
        )
        == []
    )
    assert (
        report_repo.list_server_only(
            cycle_id=cycle_id, report_type=ATHENA_ACTIVE_STRATEGY_REPORT_TYPE
        )
        == []
    )
    # 4. Sprint status rolled back to 'active' and active Athena context is still v1
    assert sprint_repo.get(sprint_id).status == "active"
    assert service_reject.get_active_athena_context(cycle_id).strategy_version == 1


@pytest.mark.anyio
async def test_weekly_api_strict_privacy_isolation_and_history(
    client: TestClient,
    db_session,
    auth_headers: dict[str, str],
):
    """
    End-to-end FastAPI test for POST /api/v1/cascade/run-weekly and GET /api/v1/reports/client:
    - Verifies authentication and strict input validation (extra='forbid').
    - Verifies zero leakage of weekly_deputy_analysis, updated_weekly_strategy,
      flash_quality_review, or any server_only data through client endpoints.
    - Verifies multi-week history preservation across Sprint 1 and Sprint 2.
    """
    cycle_id = "cycle_weekly_api"
    await _seed_completed_primary_consilium(
        db_session, cycle_id=cycle_id, primary_date="2026-10-18"
    )
    _seed_seven_day_athena_logs(db_session, cycle_id=cycle_id, sprint_id="sprint_api_w1")

    recorded_calls: List[Dict[str, Any]] = []
    app.dependency_overrides[get_gemini_gateway] = lambda: _build_weekly_mock_gateway(
        recorded_calls
    )

    try:
        # 1. Unauthorized call rejected
        unauth = client.post(
            "/api/v1/cascade/run-weekly",
            json={"cycle_id": cycle_id, "sprint_id": "sprint_api_w1"},
        )
        assert unauth.status_code == 401

        # 2. Extra field injection rejected (422)
        injected = client.post(
            "/api/v1/cascade/run-weekly",
            json={
                "cycle_id": cycle_id,
                "sprint_id": "sprint_api_w1",
                "updated_weekly_strategy": "HACKED_STRATEGY",
            },
            headers=auth_headers,
        )
        assert injected.status_code == 422

        # 3. Authorized Weekly Consilium run (Week 1)
        resp = client.post(
            "/api/v1/cascade/run-weekly",
            json={
                "cycle_id": cycle_id,
                "sprint_id": "sprint_api_w1",
                "report_date": "2026-10-25",
            },
            headers=auth_headers,
        )
        assert resp.status_code == 200
        body = resp.json()
        raw_str = json.dumps(body)

        assert body["cycle_id"] == cycle_id
        assert body["sprint_id"] == "sprint_api_w1"
        assert body["sprint_number"] == 1
        assert body["overall_report"]["report_type"] == WEEKLY_OVERALL_CLIENT_REPORT_TYPE
        assert len(body["system_reports"]) == 8

        forbidden_markers = [
            "WEEK/WEEKLY SUMMARY",
            "SYMPTOM DYNAMICS OVER 7 DAYS",
            "UPDATED WEEKLY STRATEGY",
            "WEEK 2 PRIORITIES FOR ATHENA",
            "weekly_deputy_analysis",
            "updated_weekly_strategy",
            "flash_quality_review",
            "athena_active_strategy",
            "SECRET_ATHENA_STRATEGY",
        ]
        for marker in forbidden_markers:
            assert marker not in raw_str

        # 4. Verify GET /api/v1/reports/client contains both primary and weekly client reports
        # and zero internal shelves
        get_resp = client.get(
            f"/api/v1/reports/client?cycle_id={cycle_id}",
            headers=auth_headers,
        )
        assert get_resp.status_code == 200
        client_reports = get_resp.json()
        # 9 primary client reports + 9 weekly client reports = 18
        assert len(client_reports) == 18
        raw_get_str = json.dumps(client_reports)
        for marker in forbidden_markers:
            assert marker not in raw_get_str
    finally:
        app.dependency_overrides.pop(get_gemini_gateway, None)


@pytest.mark.anyio
async def test_seven_day_athena_diary_filtering_empty_future_old_and_other_cycle(
    db_session,
):
    """
    Verifies Pre-Commit Audit Item 2:
    1. Empty logs: when no logs exist for the target sprint (even if another sprint had logs),
       Weekly Flash and Pro explicitly receive "Athena's hidden clinical logs are missing."
       and never fabricate or reuse logs from a previous week.
    2. Logs from another cycle_id are strictly excluded.
    3. Logs with future dates (> report_date end-of-day) are strictly excluded.
    4. Old logs outside the 7-day period (< report_date - 7 days) or from an earlier sprint_id
       are strictly excluded from the current week's slice.
    5. Historical logs from previous weeks remain intact in the database.
    """
    cycle_id = "cycle_diary_audit"
    other_cycle_id = "cycle_diary_other_99"
    await _seed_completed_primary_consilium(
        db_session, cycle_id=cycle_id, primary_date="2026-10-18"
    )
    await _seed_completed_primary_consilium(
        db_session, cycle_id=other_cycle_id, primary_date="2026-10-18"
    )

    weekly_service = WeeklyConsiliumService(db=db_session)

    # A. Seed a log in another cycle_id within the same date range -> must NEVER leak into cycle_id
    weekly_service.save_athena_clinical_log(
        cycle_id=other_cycle_id,
        sprint_id="sprint_other_w1",
        log_entry={
            "status": "ALERT",
            "symptoms_mentioned": ["OTHER_CYCLE_SYMPTOM_LEAK"],
            "subjective_wellbeing_score": 3,
            "adherence_to_plan": "LOW",
            "red_flags": False,
            "summary_for_master": "OTHER_CYCLE_SUMMARY_LEAK",
            "timestamp": "2026-10-22T12:00:00Z",
            "sprint_day": 4,
        },
    )

    # Also verify cross-cycle sprint_id hijacking is blocked at the service layer
    with pytest.raises(Exception, match="belongs to cycle"):
        weekly_service.save_athena_clinical_log(
            cycle_id=cycle_id,
            sprint_id="sprint_other_w1",
            log_entry={
                "status": "NORMAL",
                "symptoms_mentioned": [],
                "subjective_wellbeing_score": 8,
                "adherence_to_plan": "HIGH",
                "red_flags": False,
                "summary_for_master": "Attempted cross-cycle sprint reuse",
                "timestamp": "2026-10-22T12:00:00Z",
                "sprint_day": 4,
            },
        )

    # B. Seed an OLD log outside the 7-day window (2026-10-10 when report_date is 2026-10-25)
    weekly_service.save_athena_clinical_log(
        cycle_id=cycle_id,
        sprint_id="sprint_audit_w1",
        log_entry={
            "status": "NORMAL",
            "symptoms_mentioned": ["STALE_OUT_OF_WINDOW_SYMPTOM"],
            "subjective_wellbeing_score": 7,
            "adherence_to_plan": "HIGH",
            "red_flags": False,
            "summary_for_master": "STALE_LOG_FROM_2026_10_10",
            "timestamp": "2026-10-10T12:00:00Z",
            "sprint_day": 1,
        },
    )

    # C. Seed a FUTURE log after report_date (2026-10-29 when report_date is 2026-10-25)
    weekly_service.save_athena_clinical_log(
        cycle_id=cycle_id,
        sprint_id="sprint_audit_w1",
        log_entry={
            "status": "ALERT",
            "symptoms_mentioned": ["FUTURE_TIMESTAMP_SYMPTOM"],
            "subjective_wellbeing_score": 5,
            "adherence_to_plan": "MEDIUM",
            "red_flags": False,
            "summary_for_master": "FUTURE_LOG_FROM_2026_10_29",
            "timestamp": "2026-10-29T12:00:00Z",
            "sprint_day": 7,
        },
    )

    # D. Seed a valid log inside the 7-day window [2026-10-18 .. 2026-10-25] for sprint_audit_w1
    weekly_service.save_athena_clinical_log(
        cycle_id=cycle_id,
        sprint_id="sprint_audit_w1",
        log_entry={
            "status": "NORMAL",
            "symptoms_mentioned": ["mild evening tiredness"],
            "subjective_wellbeing_score": 8,
            "adherence_to_plan": "HIGH",
            "red_flags": False,
            "summary_for_master": "VALID_W1_LOG_2026_10_23",
            "timestamp": "2026-10-23T19:00:00Z",
            "sprint_day": 5,
        },
    )

    # Verify get_seven_day_athena_logs (both with sprint_id and sprint_id=None) returns ONLY the valid log
    for target_sprint in ("sprint_audit_w1", None):
        w1_logs = weekly_service.get_seven_day_athena_logs(
            cycle_id=cycle_id,
            sprint_id=target_sprint,
            reference_date="2026-10-25",
        )
        assert len(w1_logs) == 1
        assert w1_logs[0].summary_for_master == "VALID_W1_LOG_2026_10_23"

    # Run Weekly Consilium for sprint_audit_w1 and verify prompt content
    calls_w1: List[Dict[str, Any]] = []
    gateway_w1 = _build_weekly_mock_gateway(calls_w1)
    service_w1 = WeeklyConsiliumService(db=db_session, gateway=gateway_w1)
    await service_w1.run_weekly_consilium(
        cycle_id=cycle_id,
        sprint_id="sprint_audit_w1",
        report_date="2026-10-25",
    )

    for call in calls_w1:
        prompt = call["prompt"]
        assert "VALID_W1_LOG_2026_10_23" in prompt
        assert "OTHER_CYCLE_SUMMARY_LEAK" not in prompt
        assert "STALE_LOG_FROM_2026_10_10" not in prompt
        assert "FUTURE_LOG_FROM_2026_10_29" not in prompt

    # E. Run Weekly Consilium for sprint_w2 (report_date="2026-11-01") with ZERO logs recorded for sprint_w2
    # Must explicitly pass "Athena's hidden clinical logs are missing." and NOT reuse sprint_w1's logs!
    calls_w2_empty: List[Dict[str, Any]] = []
    gateway_w2_empty = _build_weekly_mock_gateway(calls_w2_empty)
    service_w2_empty = WeeklyConsiliumService(db=db_session, gateway=gateway_w2_empty)
    await service_w2_empty.run_weekly_consilium(
        cycle_id=cycle_id,
        sprint_id="sprint_w2_empty",
        report_date="2026-11-01",
    )

    for call in calls_w2_empty:
        prompt = call["prompt"]
        assert "Athena's hidden clinical logs are missing." in prompt
        assert "VALID_W1_LOG_2026_10_23" not in prompt

    # F. Verify all historical logs for cycle_id remain preserved in DB
    report_repo = ModelReportRepository(db_session)
    all_cycle_logs = report_repo.list_versions(
        cycle_id=cycle_id,
        report_type=ATHENA_CLINICAL_LOG_REPORT_TYPE,
    )
    assert len(all_cycle_logs) == 3
