from datetime import datetime, timezone
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query, status, Response
from sqlalchemy.orm import Session

from app.database import get_db
from app.repositories.cycle_repository import MedicalCycleRepository
from app.repositories.measurement_repository import HealthMeasurementRepository, IdempotencyKeyConflictError
from app.schemas.health import (
    HealthMeasurementCreate,
    HealthMeasurementCreateBatch,
    HealthMeasurementResponse,
    HealthMeasurementBatchResponse,
    VitalsStreamResponse,
)
from app.security.auth import verify_client_token

router = APIRouter(prefix="/api/v1/vitals", tags=["Vitals Telemetry"])


@router.post(
    "/measurement",
    response_model=HealthMeasurementResponse,
    summary="Ingest a single biometric health measurement (Protected)",
    description=(
        "Authorized endpoint receiving an individual biometric health measurement "
        "(pulse, blood pressure, SpO2) from smartwatch sensors, user manual entry, test chips, or simulation. "
        "Strictly enforces technical admissibility ranges without inventing artificial medical alarm thresholds. "
        "Guarantees duplicate delivery protection (idempotency) and binds to the active medical cycle."
    ),
)
def ingest_measurement_endpoint(
    payload: HealthMeasurementCreate,
    response: Response,
    db: Session = Depends(get_db),
    _token: str = Depends(verify_client_token),
) -> HealthMeasurementResponse:
    if not payload.cycle_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Field 'cycle_id' is required for single measurement ingestion.",
        )

    cycle_repo = MedicalCycleRepository(db)
    cycle = cycle_repo.get(payload.cycle_id)
    if not cycle:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Medical cycle '{payload.cycle_id}' not found.",
        )
    if cycle.status != "active":
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
    summary="Atomically ingest a batch of biometric measurements (Protected)",
    description=(
        "Authorized endpoint receiving a batch of biometric measurements (smartwatch batch sync or historical data). "
        "Validates technical ranges, deduplicates identical records, and atomically persists all fresh measurements. "
        "If any unexpected failure occurs during the batch, all insertions are rolled back."
    ),
)
def ingest_batch_endpoint(
    payload: HealthMeasurementCreateBatch,
    response: Response,
    db: Session = Depends(get_db),
    _token: str = Depends(verify_client_token),
) -> HealthMeasurementBatchResponse:
    cycle_repo = MedicalCycleRepository(db)
    cycle = cycle_repo.get(payload.cycle_id)
    if not cycle:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Medical cycle '{payload.cycle_id}' not found.",
        )
    if cycle.status != "active":
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
    summary="Get latest live vitals stream for a medical cycle (Protected)",
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
    _token: str = Depends(verify_client_token),
) -> VitalsStreamResponse:
    cycle_repo = MedicalCycleRepository(db)
    cycle = cycle_repo.get(cycle_id)
    if not cycle:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Medical cycle '{cycle_id}' not found.",
        )

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

    # We do NOT declare measurements live purely based on is_historical=False.
    # Without an established live telemetry heartbeat / monitoring policy,
    # is_live is None (safe unknown actuality) while latest_measured_at provides
    # the exact physical measurement timestamp for backward compatibility.
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
