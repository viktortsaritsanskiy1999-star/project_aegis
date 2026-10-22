from datetime import datetime, timezone
from typing import List, Optional, Dict, Any, Tuple
from sqlalchemy.orm import Session
from sqlalchemy import select
from app.models.health_measurement import HealthMeasurement
from app.schemas.health import HealthMeasurementCreate


class IdempotencyKeyConflictError(ValueError):
    """
    Raised when an idempotency key is reused with conflicting measurement attributes
    (differing measured_at, source, pulse, blood pressure, SpO2, historical, or simulated flags).
    """
    pass


class HealthMeasurementRepository:
    def __init__(self, db: Session):
        self.db = db

    @staticmethod
    def _normalize_dt(dt: datetime) -> datetime:
        if dt.tzinfo is not None:
            return dt.astimezone(timezone.utc).replace(tzinfo=None)
        return dt

    def find_duplicate(
        self,
        cycle_id: str,
        measured_at: datetime,
        source: str,
        pulse: Optional[int] = None,
        systolic: Optional[int] = None,
        diastolic: Optional[int] = None,
        spo2: Optional[int] = None,
        is_historical: bool = False,
        is_simulated: bool = False,
        idempotency_key: Optional[str] = None,
    ) -> Optional[HealthMeasurement]:
        """
        Locates an existing identical measurement to protect against duplicate delivery.
        Matches either by client-provided idempotency_key in raw_metadata or by exact
        physical attributes (cycle_id, normalized measured_at, source, pulse, systolic,
        diastolic, spo2, is_historical, is_simulated).
        """
        norm_dt = self._normalize_dt(measured_at)

        if idempotency_key:
            stmt_idem = select(HealthMeasurement).where(
                HealthMeasurement.cycle_id == cycle_id,
            )
            for candidate in self.db.execute(stmt_idem).scalars().all():
                if (
                    candidate.raw_metadata
                    and isinstance(candidate.raw_metadata, dict)
                    and candidate.raw_metadata.get("idempotency_key") == idempotency_key
                ):
                    cand_dt = self._normalize_dt(candidate.measured_at)
                    cand_hist = bool(candidate.is_historical)
                    cand_sim = bool(candidate.is_simulated)
                    is_identical = (
                        candidate.cycle_id == cycle_id
                        and cand_dt == norm_dt
                        and candidate.source == source
                        and candidate.pulse == pulse
                        and candidate.systolic == systolic
                        and candidate.diastolic == diastolic
                        and candidate.spo2 == spo2
                        and cand_hist == is_historical
                        and cand_sim == is_simulated
                    )
                    if is_identical:
                        return candidate
                    else:
                        raise IdempotencyKeyConflictError(
                            f"Idempotency key '{idempotency_key}' conflicts with existing record (id={candidate.id}). "
                            f"Existing: measured_at={cand_dt}, source={candidate.source}, "
                            f"pulse={candidate.pulse}, systolic={candidate.systolic}, diastolic={candidate.diastolic}, "
                            f"spo2={candidate.spo2}, historical={cand_hist}, simulated={cand_sim}. "
                            f"Incoming: measured_at={norm_dt}, source={source}, pulse={pulse}, "
                            f"systolic={systolic}, diastolic={diastolic}, spo2={spo2}, "
                            f"historical={is_historical}, simulated={is_simulated}."
                        )

        stmt = select(HealthMeasurement).where(
            HealthMeasurement.cycle_id == cycle_id,
            HealthMeasurement.measured_at == norm_dt,
            HealthMeasurement.source == source,
            HealthMeasurement.pulse == pulse,
            HealthMeasurement.systolic == systolic,
            HealthMeasurement.diastolic == diastolic,
            HealthMeasurement.spo2 == spo2,
            HealthMeasurement.is_historical == is_historical,
            HealthMeasurement.is_simulated == is_simulated,
        )
        return self.db.execute(stmt).scalars().first()

    def ingest_single(
        self,
        m: HealthMeasurementCreate,
        cycle_id: str,
    ) -> Tuple[HealthMeasurement, bool]:
        """
        Idempotently ingests a single measurement.
        Enforces cycle isolation, historical flag semantics, and simulated data isolation.
        Returns (record, is_new: bool).
        """
        if m.cycle_id is not None and m.cycle_id != cycle_id:
            raise ValueError(
                f"Measurement cycle_id '{m.cycle_id}' conflicts with target cycle_id '{cycle_id}'."
            )

        target_cycle = cycle_id
        src_str = m.source.value if hasattr(m.source, "value") else str(m.source)
        hist_flag = m.is_historical if m.is_historical is not None else False
        sim_flag = True if (m.is_simulated or src_str in ("test_chip", "simulated")) else False

        idempotency_key = (
            m.raw_metadata.get("idempotency_key")
            if m.raw_metadata and isinstance(m.raw_metadata, dict)
            else None
        )

        existing = self.find_duplicate(
            cycle_id=target_cycle,
            measured_at=m.measured_at,
            source=src_str,
            pulse=m.pulse,
            systolic=m.systolic,
            diastolic=m.diastolic,
            spo2=m.spo2,
            is_historical=hist_flag,
            is_simulated=sim_flag,
            idempotency_key=idempotency_key,
        )
        if existing:
            return existing, False

        norm_measured_at = self._normalize_dt(m.measured_at)
        now_ingested = datetime.now(timezone.utc).replace(tzinfo=None)

        rec = HealthMeasurement(
            session_id=m.session_id,
            cycle_id=target_cycle,
            pulse=m.pulse,
            systolic=m.systolic,
            diastolic=m.diastolic,
            spo2=m.spo2,
            source=src_str,
            is_simulated=sim_flag,
            is_historical=hist_flag,
            measured_at=norm_measured_at,
            ingested_at=now_ingested,
            phase=m.phase,
            note=m.note,
            raw_metadata=m.raw_metadata,
        )
        try:
            self.db.add(rec)
            self.db.commit()
            self.db.refresh(rec)
            return rec, True
        except Exception:
            self.db.rollback()
            raise

    def ingest_batch(
        self,
        measurements: List[HealthMeasurementCreate],
        cycle_id: str,
        default_session_id: Optional[str] = None,
        as_historical: Optional[bool] = None,
    ) -> Tuple[List[HealthMeasurement], List[bool]]:
        """
        Atomically ingests a batch of measurements with cycle isolation, deduplication,
        historical consistency checks, and rollback on failure.
        Returns (records, is_new_flags).
        """
        # Validate cycle isolation and envelope historical consistency before beginning transaction
        for idx, m in enumerate(measurements):
            if m.cycle_id is not None and m.cycle_id != cycle_id:
                raise ValueError(
                    f"Batch item at index {idx} has cycle_id '{m.cycle_id}' "
                    f"which conflicts with batch cycle_id '{cycle_id}'."
                )
            if (
                as_historical is not None
                and m.is_historical is not None
                and m.is_historical != as_historical
            ):
                raise ValueError(
                    f"Batch item at index {idx} specifies is_historical={m.is_historical} "
                    f"which conflicts with batch envelope as_historical={as_historical}."
                )

        records: List[HealthMeasurement] = []
        is_new_flags: List[bool] = []
        batch_seen_keys: set = set()
        batch_seen_idempotency: Dict[str, Tuple[Any, ...]] = {}
        now_ingested = datetime.now(timezone.utc).replace(tzinfo=None)

        try:
            for m in measurements:
                target_cycle = cycle_id
                target_session = m.session_id or default_session_id
                hist_flag = (
                    as_historical
                    if as_historical is not None
                    else (m.is_historical if m.is_historical is not None else False)
                )
                src_str = m.source.value if hasattr(m.source, "value") else str(m.source)
                sim_flag = True if (m.is_simulated or src_str in ("test_chip", "simulated")) else False
                norm_dt = self._normalize_dt(m.measured_at)

                idempotency_key = (
                    m.raw_metadata.get("idempotency_key")
                    if m.raw_metadata and isinstance(m.raw_metadata, dict)
                    else None
                )

                dedup_key = (
                    target_cycle,
                    norm_dt,
                    src_str,
                    m.pulse,
                    m.systolic,
                    m.diastolic,
                    m.spo2,
                    hist_flag,
                    sim_flag,
                )

                # Check intra-batch idempotency key conflict
                if idempotency_key:
                    if idempotency_key in batch_seen_idempotency:
                        prev_norm_tuple = batch_seen_idempotency[idempotency_key]
                        if prev_norm_tuple != dedup_key:
                            raise IdempotencyKeyConflictError(
                                f"Batch contains conflicting items for idempotency key '{idempotency_key}'."
                            )
                    else:
                        batch_seen_idempotency[idempotency_key] = dedup_key

                # Check existing in DB
                existing = self.find_duplicate(
                    cycle_id=target_cycle,
                    measured_at=m.measured_at,
                    source=src_str,
                    pulse=m.pulse,
                    systolic=m.systolic,
                    diastolic=m.diastolic,
                    spo2=m.spo2,
                    is_historical=hist_flag,
                    is_simulated=sim_flag,
                    idempotency_key=idempotency_key,
                )

                if existing is not None:
                    records.append(existing)
                    is_new_flags.append(False)
                elif dedup_key in batch_seen_keys or (idempotency_key and any(
                    r.raw_metadata and r.raw_metadata.get("idempotency_key") == idempotency_key for r in records
                )):
                    # Legitimate identical duplicate within the same incoming batch
                    match_record = next(
                        r for r in records
                        if (
                            (
                                r.cycle_id == target_cycle
                                and r.measured_at == norm_dt
                                and r.source == src_str
                                and r.pulse == m.pulse
                                and r.systolic == m.systolic
                                and r.diastolic == m.diastolic
                                and r.spo2 == m.spo2
                                and r.is_historical == hist_flag
                                and r.is_simulated == sim_flag
                            )
                            or (
                                idempotency_key
                                and r.raw_metadata
                                and r.raw_metadata.get("idempotency_key") == idempotency_key
                            )
                        )
                    )
                    records.append(match_record)
                    is_new_flags.append(False)
                else:
                    batch_seen_keys.add(dedup_key)
                    rec = HealthMeasurement(
                        session_id=target_session,
                        cycle_id=target_cycle,
                        pulse=m.pulse,
                        systolic=m.systolic,
                        diastolic=m.diastolic,
                        spo2=m.spo2,
                        source=src_str,
                        is_simulated=sim_flag,
                        is_historical=hist_flag,
                        measured_at=norm_dt,
                        ingested_at=now_ingested,
                        phase=m.phase,
                        note=m.note,
                        raw_metadata=m.raw_metadata,
                    )
                    self.db.add(rec)
                    records.append(rec)
                    is_new_flags.append(True)

            self.db.commit()
            for r in records:
                self.db.refresh(r)
            return records, is_new_flags
        except Exception:
            self.db.rollback()
            raise

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
            measured_at=self._normalize_dt(measured_at),
            ingested_at=self._normalize_dt(now_ingested),
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

        try:
            for m_data in measurements:
                raw_measured = m_data.get("measured_at", now_utc)
                raw_ingested = m_data.get("ingested_at", now_utc)
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
                    measured_at=self._normalize_dt(raw_measured),
                    ingested_at=self._normalize_dt(raw_ingested),
                    phase=m_data.get("phase", "historical"),
                    note=m_data.get("note", ""),
                    raw_metadata=m_data.get("raw_metadata"),
                )
                self.db.add(rec)
                created_records.append(rec)

            self.db.commit()
            return created_records
        except Exception:
            self.db.rollback()
            raise

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
        is_simulated: Optional[bool] = None,
        limit: int = 1000,
    ) -> List[HealthMeasurement]:
        """
        Retrieves the latest measurements for a cycle within the specified limit,
        while strictly returning them in chronological (measured_at asc) order.
        Filters out historical or simulated data when requested.
        """
        stmt = select(HealthMeasurement).where(HealthMeasurement.cycle_id == cycle_id)
        if is_historical is not None:
            stmt = stmt.where(HealthMeasurement.is_historical == is_historical)
        if is_simulated is not None:
            if is_simulated is False:
                stmt = stmt.where(HealthMeasurement.is_simulated.is_(False))
                stmt = stmt.where(~HealthMeasurement.source.in_(["test_chip", "simulated"]))
            else:
                stmt = stmt.where(
                    (HealthMeasurement.is_simulated.is_(True))
                    | (HealthMeasurement.source.in_(["test_chip", "simulated"]))
                )

        if limit and limit > 0:
            stmt = stmt.order_by(
                HealthMeasurement.measured_at.desc(),
                HealthMeasurement.id.desc(),
            ).limit(limit)
            records = list(self.db.execute(stmt).scalars().all())
            records.reverse()
            return records
        else:
            stmt = stmt.order_by(
                HealthMeasurement.measured_at.asc(),
                HealthMeasurement.id.asc(),
            )
            return list(self.db.execute(stmt).scalars().all())
