from datetime import datetime, timezone
from typing import List, Optional, Dict, Any
from sqlalchemy.orm import Session
from sqlalchemy import select
from app.models.health_measurement import HealthMeasurement


class HealthMeasurementRepository:
    def __init__(self, db: Session):
        self.db = db

    def create(
        self,
        measured_at: datetime,
        source: str,
        pulse: Optional[int] = None,
        systolic: Optional[int] = None,
        diastolic: Optional[int] = None,
        spo2: Optional[int] = None,
        session_id: Optional[str] = None,
        cycle_id: Optional[str] = None,
        is_simulated: bool = False,
        is_historical: bool = False,
        ingested_at: Optional[datetime] = None,
        phase: str = "standby",
        note: str = "",
        raw_metadata: Optional[Dict[str, Any]] = None,
    ) -> HealthMeasurement:
        now_ingested = ingested_at or datetime.now(timezone.utc)
        measurement = HealthMeasurement(
            session_id=session_id,
            cycle_id=cycle_id,
            pulse=pulse,
            systolic=systolic,
            diastolic=diastolic,
            spo2=spo2,
            source=source,
            is_simulated=is_simulated,
            is_historical=is_historical,
            measured_at=measured_at,
            ingested_at=now_ingested,
            phase=phase,
            note=note,
            raw_metadata=raw_metadata,
        )
        self.db.add(measurement)
        self.db.commit()
        self.db.refresh(measurement)
        return measurement

    def create_batch(
        self,
        measurements: List[Dict[str, Any]],
        default_cycle_id: Optional[str] = None,
        default_session_id: Optional[str] = None,
    ) -> List[HealthMeasurement]:
        created_records: List[HealthMeasurement] = []
        now_utc = datetime.now(timezone.utc)

        for m_data in measurements:
            rec = HealthMeasurement(
                session_id=m_data.get("session_id", default_session_id),
                cycle_id=m_data.get("cycle_id", default_cycle_id),
                pulse=m_data.get("pulse"),
                systolic=m_data.get("systolic"),
                diastolic=m_data.get("diastolic"),
                spo2=m_data.get("spo2"),
                source=m_data.get("source", "unknown"),
                is_simulated=m_data.get("is_simulated", False),
                is_historical=m_data.get("is_historical", False),
                measured_at=m_data.get("measured_at", now_utc),
                ingested_at=m_data.get("ingested_at", now_utc),
                phase=m_data.get("phase", "historical"),
                note=m_data.get("note", ""),
                raw_metadata=m_data.get("raw_metadata"),
            )
            self.db.add(rec)
            created_records.append(rec)

        self.db.commit()
        return created_records

    def list_by_session(self, session_id: str) -> List[HealthMeasurement]:
        return list(self.db.execute(
            select(HealthMeasurement)
            .where(HealthMeasurement.session_id == session_id)
            .order_by(HealthMeasurement.measured_at.asc())
        ).scalars().all())

    def list_by_cycle(
        self,
        cycle_id: str,
        is_historical: Optional[bool] = None,
        limit: int = 1000,
    ) -> List[HealthMeasurement]:
        stmt = select(HealthMeasurement).where(HealthMeasurement.cycle_id == cycle_id)
        if is_historical is not None:
            stmt = stmt.where(HealthMeasurement.is_historical == is_historical)
        stmt = stmt.order_by(HealthMeasurement.measured_at.asc()).limit(limit)
        return list(self.db.execute(stmt).scalars().all())
