from datetime import datetime, timezone, timedelta
from typing import Any, Callable, Dict, List, Optional
from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.repositories.measurement_repository import (
    HealthMeasurementRepository,
    IdempotencyKeyConflictError,
)
from app.schemas.health import (
    HealthMeasurementBatchResponse,
    HealthMeasurementCreate,
    HealthMeasurementCreateBatch,
    HealthMeasurementResponse,
    VitalsStreamResponse,
)
from app.schemas.mode2 import (
    Mode2EpisodeSummaryResponse,
    Mode2WorkoutFinishRequest,
    Mode2WorkoutResponseRequest,
)
from app.security.auth import (
    AuthSession,
    require_scopes,
    verify_cycle_access_for_session,
)
from app.services.gemini_gateway import GeminiGateway
from app.services.mode2_vitals_service import Mode2VitalsMonitoringService

router = APIRouter(prefix="/api/v1/vitals", tags=["Vitals Telemetry"])


def get_gemini_gateway() -> GeminiGateway:
    return GeminiGateway()


def get_server_now_provider() -> Callable[[], datetime]:
    return lambda: datetime.now(timezone.utc)


def _to_utc_datetime(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _build_mode2_summary_response(ep: Any) -> Mode2EpisodeSummaryResponse:
    last_reply = ep.athena_events[-1].athena_reply if ep.athena_events else None
    return Mode2EpisodeSummaryResponse(
        episode_id=ep.episode_id,
        cycle_id=ep.cycle_id,
        session_id=getattr(ep, "session_id", None),
        status=ep.status.value if hasattr(ep.status, "value") else str(ep.status),
        started_at_utc=ep.started_at_utc,
        closed_at_utc=ep.closed_at_utc,
        baseline_pulse=ep.baseline_pulse,
        trigger_pulse=ep.trigger_pulse,
        min_pulse=ep.min_pulse,
        max_pulse=ep.max_pulse,
        latest_pulse=ep.latest_pulse,
        latest_spo2=ep.latest_spo2,
        workout_context=ep.workout_context.value if hasattr(ep.workout_context, "value") else str(ep.workout_context),
        samples_count=len(ep.telemetry_samples),
        events_count=len(ep.athena_events),
        user_statements_count=len(ep.user_statements),
        last_athena_reply=last_reply,
    )


@router.post(
    "/measurement",
    response_model=HealthMeasurementResponse,
    summary="Ingest a single biometric health measurement (Protected, Patient-Isolated)",
    description=(
        "Authorized endpoint receiving an individual biometric health measurement "
        "(pulse, blood pressure, SpO2) from smartwatch sensors, user manual entry, test chips, or simulation. "
        "Strictly enforces patient cycle ownership BEFORE inspecting cycle status or deduplicating, "
        "and enforces technical admissibility ranges without inventing artificial medical alarm thresholds."
    ),
)
async def ingest_measurement_endpoint(
    payload: HealthMeasurementCreate,
    response: Response,
    db: Session = Depends(get_db),
    session: AuthSession = Depends(require_scopes("vitals:write")),
    gateway: GeminiGateway = Depends(get_gemini_gateway),
    now_provider: Callable[[], datetime] = Depends(get_server_now_provider),
) -> HealthMeasurementResponse:
    if not payload.cycle_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Field 'cycle_id' is required for single measurement ingestion.",
        )

    # Verify cycle existence & patient ownership BEFORE inspecting cycle.status or deduplicating
    cycle = verify_cycle_access_for_session(db, session, payload.cycle_id)
    is_archival_delayed = False

    server_now = datetime.now(timezone.utc)
    meas_dt = _to_utc_datetime(payload.measured_at)
    if meas_dt > server_now + timedelta(minutes=1):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Measurement timestamp '{meas_dt.isoformat()}' is in the future.",
        )

    if cycle.status == "completed":
        closed_dt = _to_utc_datetime(cycle.closed_at or cycle.updated_at)
        if closed_dt is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Completed cycle '{payload.cycle_id}' lacks closure timestamp.",
            )
        if meas_dt > closed_dt:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"Delayed measurement timestamp '{meas_dt.isoformat()}' postdates cycle closure "
                    f"'{closed_dt.isoformat()}' for completed cycle '{payload.cycle_id}'. "
                    "New measurements must be submitted to the active reassessment cycle."
                ),
            )
        if not payload.is_historical and cycle.started_at:
            started_dt = _to_utc_datetime(cycle.started_at)
            if started_dt and meas_dt < (started_dt - timedelta(minutes=5)):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=(
                        f"Non-historical measurement timestamp '{meas_dt.isoformat()}' predates cycle start "
                        f"'{started_dt.isoformat()}' for completed cycle '{payload.cycle_id}'."
                    ),
                )
        is_archival_delayed = True
    elif cycle.status == "archived":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Medical cycle '{payload.cycle_id}' is archived (status='archived').",
        )
    elif cycle.status not in ("active", "pending_reassessment"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Medical cycle '{payload.cycle_id}' is not active (status='{cycle.status}').",
        )

    meas_repo = HealthMeasurementRepository(db)
    try:
        rec, is_new = meas_repo.ingest_single(payload, cycle_id=payload.cycle_id)
    except IdempotencyKeyConflictError as ice:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(ice),
        )
    except ValueError as ve:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(ve),
        )

    # Mode 2: Trigger operational vitals monitoring for new live measurements if enabled (skipped for delayed archival)
    if is_new and not rec.is_historical and not is_archival_delayed and settings.enable_mode2_monitoring:
        mode2_svc = Mode2VitalsMonitoringService(db=db, gateway=gateway, now_provider=now_provider)
        await mode2_svc.process_measurement(
            cycle_id=rec.cycle_id or payload.cycle_id,
            pulse=rec.pulse,
            spo2=rec.spo2,
            systolic=rec.systolic,
            diastolic=rec.diastolic,
            source=rec.source,
            is_simulated=rec.is_simulated,
            measured_at=rec.measured_at,
            note=rec.note,
            phase=rec.phase,
            session_id=rec.session_id,
        )

    response.status_code = status.HTTP_201_CREATED if is_new else status.HTTP_200_OK

    return HealthMeasurementResponse(
        id=rec.id,
        cycle_id=rec.cycle_id or payload.cycle_id,
        session_id=rec.session_id,
        pulse=rec.pulse,
        systolic=rec.systolic,
        diastolic=rec.diastolic,
        spo2=rec.spo2,
        source=rec.source,
        is_simulated=rec.is_simulated,
        is_historical=rec.is_historical,
        measured_at=rec.measured_at,
        ingested_at=rec.ingested_at,
        phase=rec.phase,
        note=rec.note,
        status="recorded" if is_new else "deduplicated",
    )


@router.post(
    "/batch",
    response_model=HealthMeasurementBatchResponse,
    summary="Atomically ingest a batch of biometric measurements (Protected, Patient-Isolated)",
    description=(
        "Authorized endpoint receiving a batch of biometric measurements (smartwatch batch sync or historical data). "
        "Verifies patient cycle ownership BEFORE inspecting cycle status or batch items, validates technical ranges, "
        "deduplicates identical records, and atomically persists all fresh measurements."
    ),
)
async def ingest_batch_endpoint(
    payload: HealthMeasurementCreateBatch,
    response: Response,
    db: Session = Depends(get_db),
    session: AuthSession = Depends(require_scopes("vitals:write")),
    gateway: GeminiGateway = Depends(get_gemini_gateway),
    now_provider: Callable[[], datetime] = Depends(get_server_now_provider),
) -> HealthMeasurementBatchResponse:
    # Verify cycle existence & patient ownership BEFORE inspecting cycle.status or batch items
    cycle = verify_cycle_access_for_session(db, session, payload.cycle_id)
    is_archival_delayed = False

    server_now = datetime.now(timezone.utc)
    for idx, m in enumerate(payload.measurements):
        m_dt = _to_utc_datetime(m.measured_at)
        if m_dt > server_now + timedelta(minutes=1):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Batch item at index {idx} with timestamp '{m_dt.isoformat()}' is in the future.",
            )

    if cycle.status == "completed":
        closed_dt = _to_utc_datetime(cycle.closed_at or cycle.updated_at)
        if closed_dt is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Completed cycle '{payload.cycle_id}' lacks closure timestamp.",
            )
        for idx, m in enumerate(payload.measurements):
            meas_dt = _to_utc_datetime(m.measured_at)
            if meas_dt > closed_dt:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=(
                        f"Delayed batch item at index {idx} with timestamp '{meas_dt.isoformat()}' "
                        f"postdates cycle closure '{closed_dt.isoformat()}' for completed cycle '{payload.cycle_id}'. "
                        "New measurements must be submitted to the active reassessment cycle."
                    ),
                )
            is_hist = bool(m.is_historical or payload.as_historical)
            if not is_hist and cycle.started_at:
                started_dt = _to_utc_datetime(cycle.started_at)
                if started_dt and meas_dt < (started_dt - timedelta(minutes=5)):
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail=(
                            f"Non-historical batch item at index {idx} with timestamp '{meas_dt.isoformat()}' "
                            f"predates cycle start '{started_dt.isoformat()}' for completed cycle '{payload.cycle_id}'."
                        ),
                    )
        is_archival_delayed = True
    elif cycle.status == "archived":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Medical cycle '{payload.cycle_id}' is archived (status='archived').",
        )
    elif cycle.status not in ("active", "pending_reassessment"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Medical cycle '{payload.cycle_id}' is not active (status='{cycle.status}').",
        )

    # Enforce cycle isolation and historical envelope consistency
    for idx, m in enumerate(payload.measurements):
        if m.cycle_id is not None and m.cycle_id != payload.cycle_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"Batch item at index {idx} specifies cycle_id '{m.cycle_id}' "
                    f"which conflicts with batch cycle_id '{payload.cycle_id}'."
                ),
            )
        if (
            payload.as_historical is not None
            and m.is_historical is not None
            and m.is_historical != payload.as_historical
        ):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"Batch item at index {idx} specifies is_historical={m.is_historical} "
                    f"which conflicts with batch envelope as_historical={payload.as_historical}."
                ),
            )

    meas_repo = HealthMeasurementRepository(db)
    try:
        records, is_new_flags = meas_repo.ingest_batch(
            measurements=payload.measurements,
            cycle_id=payload.cycle_id,
            default_session_id=payload.session_id,
            as_historical=payload.as_historical,
        )
    except IdempotencyKeyConflictError as ice:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(ice),
        )
    except ValueError as ve:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(ve),
        )

    recorded_count = sum(1 for f in is_new_flags if f)
    deduplicated_count = len(is_new_flags) - recorded_count

    # Mode 2: Trigger operational vitals monitoring for new live measurements if enabled (skipped for delayed archival)
    if not is_archival_delayed and settings.enable_mode2_monitoring:
        mode2_svc = Mode2VitalsMonitoringService(db=db, gateway=gateway, now_provider=now_provider)
        for rec, is_new in zip(records, is_new_flags):
            if is_new and not rec.is_historical:
                await mode2_svc.process_measurement(
                    cycle_id=rec.cycle_id or payload.cycle_id,
                    pulse=rec.pulse,
                    spo2=rec.spo2,
                    systolic=rec.systolic,
                    diastolic=rec.diastolic,
                    source=rec.source,
                    is_simulated=rec.is_simulated,
                    measured_at=rec.measured_at,
                    note=rec.note,
                    phase=rec.phase,
                    session_id=rec.session_id,
                )

    response.status_code = status.HTTP_201_CREATED if recorded_count > 0 else status.HTTP_200_OK

    items = [
        HealthMeasurementResponse(
            id=rec.id,
            cycle_id=rec.cycle_id or payload.cycle_id,
            session_id=rec.session_id,
            pulse=rec.pulse,
            systolic=rec.systolic,
            diastolic=rec.diastolic,
            spo2=rec.spo2,
            source=rec.source,
            is_simulated=rec.is_simulated,
            is_historical=rec.is_historical,
            measured_at=rec.measured_at,
            ingested_at=rec.ingested_at,
            phase=rec.phase,
            note=rec.note,
            status="recorded" if is_new else "deduplicated",
        )
        for rec, is_new in zip(records, is_new_flags)
    ]

    return HealthMeasurementBatchResponse(
        cycle_id=payload.cycle_id,
        total_received=len(payload.measurements),
        recorded_count=recorded_count,
        deduplicated_count=deduplicated_count,
        items=items,
    )


@router.get(
    "/latest",
    response_model=VitalsStreamResponse,
    summary="Get latest live vitals stream for a medical cycle (Protected, Patient-Isolated)",
    description=(
        "Returns the authoritative latest live biometric vitals stream (pulse, blood pressure, SpO2) "
        "for the specified cycle. Historical measurements are strictly excluded from the live stream."
    ),
)
def get_latest_vitals_stream_endpoint(
    cycle_id: str = Query(..., min_length=3, max_length=64, description="Target medical cycle"),
    include_simulated: bool = Query(
        default=False,
        description="Whether to include simulated/test data in the stream (strictly false by default)",
    ),
    db: Session = Depends(get_db),
    session: AuthSession = Depends(require_scopes("vitals:read")),
) -> VitalsStreamResponse:
    verify_cycle_access_for_session(db, session, cycle_id)

    meas_repo = HealthMeasurementRepository(db)
    is_sim_filter = None if include_simulated else False
    live_measurements = meas_repo.list_by_cycle(
        cycle_id=cycle_id,
        is_historical=False,
        is_simulated=is_sim_filter,
        limit=50,
    )

    if not live_measurements:
        # Zero measurements found: is_live is strictly False, recency_status is 'no_data'
        return VitalsStreamResponse(
            cycle_id=cycle_id,
            blood_pressure="--/-- mmHg",
            heart_rate="-- bpm",
            spo2="--%",
            latest_measured_at=None,
            source=None,
            is_live=False,
            recency_status="no_data",
        )

    systolic: Optional[int] = None
    diastolic: Optional[int] = None
    pulse: Optional[int] = None
    spo2: Optional[int] = None
    latest_dt: Optional[datetime] = None
    latest_source: Optional[str] = None

    for m in reversed(live_measurements):
        if systolic is None and m.systolic is not None:
            systolic = m.systolic
        if diastolic is None and m.diastolic is not None:
            diastolic = m.diastolic
        if pulse is None and m.pulse is not None:
            pulse = m.pulse
        if spo2 is None and m.spo2 is not None:
            spo2 = m.spo2
        if latest_dt is None:
            latest_dt = m.measured_at
            latest_source = m.source
        if all(v is not None for v in (systolic, diastolic, pulse, spo2)):
            break

    return VitalsStreamResponse(
        cycle_id=cycle_id,
        blood_pressure=f"{systolic if systolic is not None else '--'}/{diastolic if diastolic is not None else '--'} mmHg",
        heart_rate=f"{pulse if pulse is not None else '--'} bpm",
        spo2=f"{spo2 if spo2 is not None else '--'}%",
        latest_measured_at=latest_dt,
        source=latest_source,
        is_live=None,
        recency_status="unknown",
    )


# ==============================================================================
# MODE 2: OPERATIONAL VITALS MONITORING ENDPOINTS
# ==============================================================================

@router.post(
    "/mode2/workout-response",
    response_model=Mode2EpisodeSummaryResponse,
    summary="Submit user workout clarification and subjective symptoms for active Mode 2 episode",
    description=(
        "Registers patient response regarding physical exertion (training vs rest) "
        "and subjective symptoms. Stores original answers verbatim with timestamps. "
        "Triggers immediate Athena operational guidance without consuming daily planned question quota."
    ),
)
async def submit_mode2_workout_response_endpoint(
    payload: Mode2WorkoutResponseRequest,
    db: Session = Depends(get_db),
    session: AuthSession = Depends(require_scopes("vitals:write")),
    gateway: GeminiGateway = Depends(get_gemini_gateway),
    now_provider: Callable[[], datetime] = Depends(get_server_now_provider),
) -> Mode2EpisodeSummaryResponse:
    verify_cycle_access_for_session(db, session, payload.cycle_id)
    mode2_svc = Mode2VitalsMonitoringService(db=db, gateway=gateway, now_provider=now_provider)
    active_ep = mode2_svc.get_active_episode(payload.cycle_id)
    if not active_ep:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No active Mode 2 episode found for cycle '{payload.cycle_id}'.",
        )
    msg = payload.user_message or ("Да, тренируюсь" if payload.is_workout else "Нет, не тренируюсь")
    updated_ep = await mode2_svc.handle_user_statement(
        cycle_id=payload.cycle_id,
        user_text=msg,
        is_workout=payload.is_workout,
        symptoms=payload.symptoms,
    )
    if not updated_ep:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to update Mode 2 episode with user statement.",
        )
    return _build_mode2_summary_response(updated_ep)


@router.get(
    "/mode2/active",
    response_model=Optional[Mode2EpisodeSummaryResponse],
    summary="Retrieve active Mode 2 episode for medical cycle",
)
def get_mode2_active_episode_endpoint(
    cycle_id: str = Query(..., min_length=3, max_length=64),
    db: Session = Depends(get_db),
    session: AuthSession = Depends(require_scopes("vitals:read")),
) -> Optional[Mode2EpisodeSummaryResponse]:
    verify_cycle_access_for_session(db, session, cycle_id)
    mode2_svc = Mode2VitalsMonitoringService(db=db)
    active_ep = mode2_svc.get_active_episode(cycle_id)
    if not active_ep:
        return None
    return _build_mode2_summary_response(active_ep)


@router.get(
    "/mode2/history",
    response_model=List[Mode2EpisodeSummaryResponse],
    summary="Retrieve full Mode 2 episode history for medical cycle",
)
def get_mode2_episode_history_endpoint(
    cycle_id: str = Query(..., min_length=3, max_length=64),
    db: Session = Depends(get_db),
    session: AuthSession = Depends(require_scopes("vitals:read")),
) -> List[Mode2EpisodeSummaryResponse]:
    verify_cycle_access_for_session(db, session, cycle_id)
    mode2_svc = Mode2VitalsMonitoringService(db=db)
    episodes = mode2_svc.get_episode_history(cycle_id)
    return [_build_mode2_summary_response(ep) for ep in episodes]


@router.post(
    "/mode2/workout-finish",
    summary="Complete active workout session and transition to recovery",
    description="Transitions an active workout session to recovering status, matching client finishWorkout.",
)
def finish_mode2_workout_endpoint(
    payload: Optional[Mode2WorkoutFinishRequest] = None,
    cycle_id: Optional[str] = Query(default=None, min_length=3, max_length=64),
    session_id: Optional[str] = Query(default=None),
    db: Session = Depends(get_db),
    session: AuthSession = Depends(require_scopes("vitals:write")),
    now_provider: Callable[[], datetime] = Depends(get_server_now_provider),
) -> Dict[str, Any]:
    resolved_cycle_id = (payload.cycle_id if payload and payload.cycle_id else None) or cycle_id
    if not resolved_cycle_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="cycle_id is required either in payload body or query parameter.",
        )
    resolved_session_id = (payload.session_id if payload and payload.session_id else None) or session_id
    verify_cycle_access_for_session(db, session, resolved_cycle_id)
    mode2_svc = Mode2VitalsMonitoringService(db=db, now_provider=now_provider)
    ws = mode2_svc.complete_workout_session(resolved_cycle_id, session_id=resolved_session_id)
    return {
        "status": "success",
        "message": "Workout session completed, transitioned to recovery.",
        "session_id": ws.session_id if ws else resolved_session_id,
        "workout_status": ws.status if ws else "recovering",
    }

