import json
import logging
import re
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.health_measurement import HealthMeasurement
from app.models.model_report import ModelReport
from app.repositories.cycle_repository import MedicalCycleRepository
from app.repositories.measurement_repository import HealthMeasurementRepository
from app.repositories.report_repository import ModelReportRepository
from app.schemas.privacy import PrivacyLevel
from app.schemas.wellness import (
    ActivitySummary,
    BloodPressureSummary,
    HeartRateSummary,
    PatientTimezoneSetting,
    SleepPhasesSummary,
    SleepSummary,
    SpO2Summary,
    WellnessBiometricsSummary,
    WellnessDailyReport,
    WellnessModelOutput,
    WellnessStatus,
    WellnessTimeWindow,
    WellnessTriggerType,
)
from app.services.consilium_prompts import build_wellness_daily_analysis_prompt
from app.services.gemini_gateway import GeminiGateway, GeminiGatewayError, GeminiModelRole
from app.services.weekly_consilium_service import WeeklyConsiliumService

logger = logging.getLogger("aegis.wellness_daily")

WELLNESS_DAILY_REPORT_TYPE: str = "wellness_daily_report"
PATIENT_TIMEZONE_REPORT_TYPE: str = "patient_timezone"
DEFAULT_USER_TIMEZONE: str = "UTC"


class WellnessDailyService:
    """
    Dedicated server-side WellnessDailyService (Stage 3).
    Analyzes verified objective patient telemetry and manual measurements,
    generating a versioned, durable daily report for consumption by Athena Daily Journal.

    Key Invariants:
    1. Strictly separates verified measured physical facts from Gemini Flash clinical interpretation.
    2. Never fabricates missing metrics and never treats absence of data as normal/healthy.
    3. NO_DATA report is generated deterministically with STRICTLY 0 calls to Gemini.
    4. Primary trigger: confirmed awakening / sleep-end event (does not treat arbitrary first measurement as wake-up).
    5. Fallback trigger: 12:00 in user's explicitly configured local timezone (server timezone is never used).
    6. Persisted as SERVER_ONLY in ModelReportRepository, strictly isolated from external client APIs.
    7. Idempotent: late measurements do not silently overwrite closed reports.
    """

    def __init__(
        self,
        db: Session,
        gateway: Optional[GeminiGateway] = None,
        now_provider: Optional[Callable[[], datetime]] = None,
        journal_service: Optional[Any] = None,
        mode2_service: Optional[Any] = None,
    ):
        self.db = db
        self.gateway = gateway or GeminiGateway()
        self.now_provider = now_provider or (lambda: datetime.now(timezone.utc))
        self.journal_service = journal_service
        self._mode2_service = mode2_service

        self.report_repo = ModelReportRepository(db)
        self.measurement_repo = HealthMeasurementRepository(db)
        self.cycle_repo = MedicalCycleRepository(db)
        self.weekly_service = WeeklyConsiliumService(
            db=db, gateway=self.gateway, now_provider=self.now_provider
        )

    @property
    def mode2_service(self) -> Any:
        if self._mode2_service is None:
            from app.services.mode2_vitals_service import Mode2VitalsMonitoringService
            self._mode2_service = Mode2VitalsMonitoringService(
                db=self.db, gateway=self.gateway, now_provider=self.now_provider
            )
        return self._mode2_service

    @mode2_service.setter
    def mode2_service(self, service: Any) -> None:
        self._mode2_service = service

    def _now_utc(self) -> datetime:
        dt = self.now_provider()
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)

    @staticmethod
    def _parse_iso_utc(raw_value: Optional[str | datetime]) -> Optional[datetime]:
        if raw_value is None:
            return None
        if isinstance(raw_value, datetime):
            if raw_value.tzinfo is None:
                return raw_value.replace(tzinfo=timezone.utc)
            return raw_value.astimezone(timezone.utc)
        if not isinstance(raw_value, str) or not raw_value.strip():
            return None
        try:
            dt = datetime.fromisoformat(raw_value.strip())
            if dt.tzinfo is None:
                return dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except Exception:
            return None

    # ==========================================================================
    # 1. TIMEZONE MANAGEMENT
    # ==========================================================================

    def save_patient_timezone(
        self,
        cycle_id: str,
        timezone_name: str,
        patient_id: Optional[str] = None,
        commit: bool = True,
    ) -> PatientTimezoneSetting:
        """
        Explicitly saves and validates the patient's local timezone.
        Stored under report_type='patient_timezone' (SERVER_ONLY).
        """
        clean_cycle = (cycle_id or "").strip()
        clean_tz = (timezone_name or "").strip()
        if not clean_cycle:
            raise ValueError("cycle_id must be non-empty.")
        if not clean_tz:
            raise ValueError("timezone_name must be non-empty.")

        try:
            ZoneInfo(clean_tz)
        except Exception as exc:
            raise ValueError(f"Invalid IANA timezone name: '{clean_tz}'.") from exc

        eff_patient_id = patient_id
        if not eff_patient_id:
            cycle = self.cycle_repo.get(clean_cycle)
            eff_patient_id = cycle.patient_id if cycle else "unknown"

        setting = PatientTimezoneSetting(
            cycle_id=clean_cycle,
            patient_id=eff_patient_id or "unknown",
            timezone=clean_tz,
            updated_at_utc=self._now_utc().isoformat(),
        )

        self.report_repo.create_next_version(
            cycle_id=clean_cycle,
            model="aegis-patient-timezone",
            report_type=PATIENT_TIMEZONE_REPORT_TYPE,
            privacy_level=PrivacyLevel.SERVER_ONLY,
            content=setting.model_dump(),
            commit=commit,
        )
        return setting

    def get_patient_timezone(self, cycle_id: str) -> str:
        """
        Returns the explicitly stored patient timezone for cycle_id.
        Defaults to 'UTC' if no explicit setting exists.
        """
        clean_cycle = (cycle_id or "").strip()
        reports = self.report_repo.list_server_only(
            cycle_id=clean_cycle,
            report_type=PATIENT_TIMEZONE_REPORT_TYPE,
        )
        if reports:
            c = reports[0].content or {}
            tz_str = str(c.get("timezone") or "").strip()
            if tz_str:
                try:
                    ZoneInfo(tz_str)
                    return tz_str
                except Exception:
                    pass
        return DEFAULT_USER_TIMEZONE

    # ==========================================================================
    # 2. OBSERVATION WINDOW & TIME TRIGGERS
    # ==========================================================================

    def resolve_observation_window(
        self,
        cycle_id: str,
        calendar_week: int,
        calendar_day: int,
    ) -> Tuple[datetime, datetime]:
        """
        Resolves the 24-hour observation window [start_dt, end_dt) for (week, day)
        relative to schedule anchor T0: [T0 + k*24h, T0 + (k+1)*24h).
        Includes a 10-minute backward buffer for Week 1 Day 1.
        """
        anchor_dt = self.weekly_service.find_schedule_anchor_timestamp(cycle_id)
        if anchor_dt is None:
            raise ValueError(
                f"Cannot resolve observation window for cycle '{cycle_id}': "
                "schedule anchor T0 has not been established."
            )

        offset_days = (calendar_week - 1) * 7 + (calendar_day - 1)
        start_dt = anchor_dt + timedelta(days=offset_days)
        end_dt = anchor_dt + timedelta(days=offset_days + 1)

        if calendar_week == 1 and calendar_day == 1:
            start_dt = anchor_dt - timedelta(minutes=10)

        return start_dt, end_dt

    def resolve_midday_fallback_utc(
        self,
        cycle_id: str,
        calendar_week: int,
        calendar_day: int,
    ) -> datetime:
        """
        Computes the exact UTC timestamp of 12:00:00 in the user's local timezone
        corresponding to (calendar_week, calendar_day).
        Guaranteed to use the user's configured timezone, NOT server local time.
        """
        window_start, _ = self.resolve_observation_window(
            cycle_id, calendar_week, calendar_day
        )
        tz_name = self.get_patient_timezone(cycle_id)
        user_tz = ZoneInfo(tz_name)

        # Convert window start (or anchor-relative date) to user local time
        # The nominal calendar date in user timezone for this observation day:
        user_local_dt = window_start.astimezone(user_tz)
        local_date = user_local_dt.date()

        # Local midday (12:00:00) on that local date
        local_midday = datetime(
            local_date.year,
            local_date.month,
            local_date.day,
            12,
            0,
            0,
            tzinfo=user_tz,
        )
        return local_midday.astimezone(timezone.utc)

    # ==========================================================================
    # 3. TRIGGER EVALUATION: WAKE-UP VS MIDDAY FALLBACK
    # ==========================================================================

    @staticmethod
    def inspect_measurements_for_confirmed_wake_up(
        measurements: List[HealthMeasurement],
        window_start: datetime,
        window_end: datetime,
    ) -> Tuple[bool, Optional[datetime], Optional[Dict[str, Any]]]:
        """
        Inspects measurements to detect a confirmed awakening / sleep end event.
        STRICT RULE: An arbitrary first measurement (e.g. single pulse or BP point)
        is NEVER treated as wake-up if data is insufficient.
        Requires explicit sleep-end metadata or completed sleep session.
        """
        for m in measurements:
            meta = m.raw_metadata or {}
            if not isinstance(meta, dict):
                continue

            event_type = str(meta.get("event") or "").lower()
            if event_type in ("sleep_end", "wake_up", "awakening"):
                wake_dt = WellnessDailyService._parse_iso_utc(
                    meta.get("wake_up_time") or meta.get("sleep_end") or m.measured_at
                )
                return True, wake_dt or m.measured_at.replace(tzinfo=timezone.utc), meta

            phase = str(m.phase or "").lower()
            if phase in ("wake_up", "awakening"):
                return True, m.measured_at.replace(tzinfo=timezone.utc), meta

            if meta.get("sleep_end") or meta.get("wake_up_time"):
                wake_dt = WellnessDailyService._parse_iso_utc(
                    meta.get("wake_up_time") or meta.get("sleep_end")
                )
                if wake_dt:
                    return True, wake_dt, meta

            # Completed sleep session with explicit sleep hours and end
            if "sleep_hours" in meta and (
                meta.get("status") == "completed" or meta.get("is_complete") is True
            ):
                return True, m.measured_at.replace(tzinfo=timezone.utc), meta

        return False, None, None

    def evaluate_trigger(
        self,
        cycle_id: str,
        calendar_week: int,
        calendar_day: int,
        as_of: Optional[datetime] = None,
    ) -> Optional[WellnessTriggerType]:
        """
        Evaluates whether daily Wellness analysis should run for (week, day).
        Returns WellnessTriggerType.WAKE_UP, WellnessTriggerType.SCHEDULED_MIDDAY, or None.
        """
        effective_now = as_of or self._now_utc()
        nominal_start, nominal_end = self.resolve_observation_window(
            cycle_id, calendar_week, calendar_day
        )
        prev_rep = self.get_previous_daily_wellness_report(
            cycle_id, calendar_week, calendar_day
        )
        if prev_rep is not None and prev_rep.window and prev_rep.window.end_utc:
            prev_end = self._parse_iso_utc(prev_rep.window.end_utc)
            window_start = prev_end if prev_end is not None else nominal_start
        else:
            window_start = nominal_start

        # Do not evaluate future windows
        if effective_now < window_start:
            return None

        # 1. Check for confirmed wake-up in measurements collected so far
        measurements = self.gather_genuine_measurements(
            cycle_id=cycle_id,
            start_dt=window_start,
            end_dt=min(effective_now, nominal_end),
        )
        is_wake_up, _, _ = self.inspect_measurements_for_confirmed_wake_up(
            measurements, window_start, nominal_end
        )
        if is_wake_up:
            return WellnessTriggerType.WAKE_UP

        # 2. Check fallback midday trigger (12:00 user local time)
        midday_utc = self.resolve_midday_fallback_utc(
            cycle_id, calendar_week, calendar_day
        )
        if effective_now >= midday_utc:
            return WellnessTriggerType.SCHEDULED_MIDDAY

        return None

    # ==========================================================================
    # 4. MEASUREMENT GATHERING & FACTUAL SUMMARY EXTRACTION
    # ==========================================================================

    def gather_genuine_measurements(
        self,
        cycle_id: str,
        start_dt: datetime,
        end_dt: datetime,
    ) -> List[HealthMeasurement]:
        """
        Retrieves live, admissible biometric telemetry from HealthMeasurementRepository.
        Filters by normalized measurement timestamp [start_dt, end_dt).
        """
        norm_start = self.measurement_repo._normalize_dt(start_dt)
        norm_end = self.measurement_repo._normalize_dt(end_dt)

        all_measurements = self.measurement_repo.list_by_cycle(
            cycle_id=cycle_id,
            is_historical=False,
            is_simulated=False,
            limit=5000,
        )

        records_in_window: List[HealthMeasurement] = []
        for m in all_measurements:
            m_dt = self.measurement_repo._normalize_dt(m.measured_at)
            if norm_start <= m_dt < norm_end:
                records_in_window.append(m)

        records_in_window.sort(
            key=lambda item: self.measurement_repo._normalize_dt(item.measured_at)
        )
        return records_in_window

    def build_factual_biometrics_summary(
        self,
        measurements: List[HealthMeasurement],
        mode2_episodes: Optional[List[Any]] = None,
    ) -> WellnessBiometricsSummary:
        """
        Builds a strictly factual summary of measured numerical biometrics.
        Separates raw numbers from clinical interpretation.
        Flags questionable physiological outliers.
        """
        pulse_vals: List[int] = []
        sys_vals: List[int] = []
        dia_vals: List[int] = []
        spo2_vals: List[int] = []
        sources_seen: set[str] = set()
        questionable_count = 0

        sleep_hours_list: List[float] = []
        sleep_start_iso: Optional[str] = None
        sleep_end_iso: Optional[str] = None
        sleep_quality: Optional[str] = None
        phases_deep: Optional[float] = None
        phases_rem: Optional[float] = None
        phases_light: Optional[float] = None
        phases_awake: Optional[float] = None
        is_wake_up = False

        total_steps = 0
        has_steps = False
        total_active_mins = 0
        has_activity = False
        workout_pulses: List[int] = []
        recovery_pulses: List[int] = []
        detected_workout_types: List[str] = []

        for m in measurements:
            src = m.source or "unknown"
            sources_seen.add(src)

            if m.pulse is not None:
                pulse_vals.append(m.pulse)
                if m.pulse < 35 or m.pulse > 210:
                    questionable_count += 1

            if m.systolic is not None and m.diastolic is not None:
                sys_vals.append(m.systolic)
                dia_vals.append(m.diastolic)
                if m.systolic < 65 or m.systolic > 250 or m.diastolic < 35 or m.diastolic > 150:
                    questionable_count += 1
            elif m.systolic is not None:
                sys_vals.append(m.systolic)
            elif m.diastolic is not None:
                dia_vals.append(m.diastolic)

            if m.spo2 is not None:
                spo2_vals.append(m.spo2)
                if m.spo2 < 65 or m.spo2 > 100:
                    questionable_count += 1

            meta = m.raw_metadata or {}
            if isinstance(meta, dict):
                if "sleep_hours" in meta:
                    try:
                        sleep_hours_list.append(float(meta["sleep_hours"]))
                    except (ValueError, TypeError):
                        pass
                if "quality" in meta and not sleep_quality:
                    sleep_quality = str(meta["quality"])
                if "sleep_start" in meta and not sleep_start_iso:
                    sleep_start_iso = str(meta["sleep_start"])
                if "sleep_end" in meta and not sleep_end_iso:
                    sleep_end_iso = str(meta["sleep_end"])
                if "deep_hours" in meta:
                    phases_deep = float(meta["deep_hours"])
                if "rem_hours" in meta:
                    phases_rem = float(meta["rem_hours"])
                if "light_hours" in meta:
                    phases_light = float(meta["light_hours"])
                if "awake_hours" in meta:
                    phases_awake = float(meta["awake_hours"])

                event_str = str(meta.get("event") or "").lower()
                if event_str in ("sleep_end", "wake_up", "awakening") or m.phase in ("wake_up", "awakening"):
                    is_wake_up = True
                    if not sleep_end_iso:
                        sleep_end_iso = m.measured_at.isoformat()

                if "steps" in meta or "step_count" in meta:
                    try:
                        val = int(meta.get("steps") or meta.get("step_count") or 0)
                        total_steps += val
                        has_steps = True
                    except (ValueError, TypeError):
                        pass

                if "activity_minutes" in meta or "active_minutes" in meta:
                    try:
                        val = int(meta.get("activity_minutes") or meta.get("active_minutes") or 0)
                        total_active_mins += val
                        has_activity = True
                    except (ValueError, TypeError):
                        pass

                w_type = meta.get("workout_type") or meta.get("sport")
                if w_type and str(w_type) not in detected_workout_types:
                    detected_workout_types.append(str(w_type))

            m_phase = str(m.phase or "").lower()
            meta_phase = str(meta.get("phase") or "").lower() if isinstance(meta, dict) else ""
            if m.pulse is not None:
                if m_phase == "workout" or meta_phase == "workout":
                    workout_pulses.append(m.pulse)
                elif (
                    m_phase in ("recovery_0_10", "recovery_10_50", "recovery")
                    or "recovery" in meta_phase
                ):
                    recovery_pulses.append(m.pulse)

        # Sub-summaries
        hr_summary = None
        if pulse_vals:
            hr_summary = HeartRateSummary(
                min_bpm=min(pulse_vals),
                avg_bpm=round(sum(pulse_vals) / len(pulse_vals), 1),
                max_bpm=max(pulse_vals),
                sample_count=len(pulse_vals),
            )

        bp_summary = None
        if sys_vals and dia_vals:
            bp_summary = BloodPressureSummary(
                min_systolic=min(sys_vals),
                avg_systolic=round(sum(sys_vals) / len(sys_vals), 1),
                max_systolic=max(sys_vals),
                min_diastolic=min(dia_vals),
                avg_diastolic=round(sum(dia_vals) / len(dia_vals), 1),
                max_diastolic=max(dia_vals),
                sample_count=min(len(sys_vals), len(dia_vals)),
            )

        spo2_summary = None
        if spo2_vals:
            spo2_summary = SpO2Summary(
                min_spo2=min(spo2_vals),
                avg_spo2=round(sum(spo2_vals) / len(spo2_vals), 1),
                max_spo2=max(spo2_vals),
                sample_count=len(spo2_vals),
            )

        sleep_summary = None
        if sleep_hours_list or sleep_start_iso or sleep_end_iso:
            tot_sleep = sum(sleep_hours_list) if sleep_hours_list else 0.0
            phases_summary = None
            if any(p is not None for p in (phases_deep, phases_rem, phases_light, phases_awake)):
                phases_summary = SleepPhasesSummary(
                    deep_hours=phases_deep,
                    rem_hours=phases_rem,
                    light_hours=phases_light,
                    awake_hours=phases_awake,
                )
            sleep_summary = SleepSummary(
                sleep_hours=round(tot_sleep, 2),
                sleep_start_utc=sleep_start_iso,
                sleep_end_utc=sleep_end_iso,
                phases=phases_summary,
                quality=sleep_quality,
                is_confirmed_wake_up=is_wake_up,
            )

        activity_summary = None
        has_workout_data = bool(workout_pulses or recovery_pulses)
        if has_steps or has_activity or has_workout_data:
            w_avg = round(sum(workout_pulses) / len(workout_pulses), 1) if workout_pulses else None
            w_max = max(workout_pulses) if workout_pulses else None
            r_min = min(recovery_pulses) if recovery_pulses else None
            w_type_str = detected_workout_types[0] if detected_workout_types else ("workout" if has_workout_data else None)

            activity_summary = ActivitySummary(
                step_count=total_steps if has_steps else None,
                activity_minutes=total_active_mins if has_activity else None,
                workout_type=w_type_str,
                workout_avg_bpm=w_avg,
                workout_max_bpm=w_max,
                recovery_min_bpm=r_min,
            )

        return WellnessBiometricsSummary(
            heart_rate=hr_summary,
            blood_pressure=bp_summary,
            spo2=spo2_summary,
            sleep=sleep_summary,
            activity=activity_summary,
            mode2_episodes=mode2_episodes or [],
            sources_present=sorted(list(sources_seen)),
            has_manual_input="manual_input" in sources_seen,
            has_sensor_data=bool(sources_seen - {"manual_input", "unknown"}),
            total_measurements=len(measurements),
            questionable_datapoints_count=questionable_count,
        )

    # ==========================================================================
    # 5. REPORT GENERATION & PERSISTENCE
    # ==========================================================================

    def get_daily_wellness_report(
        self,
        cycle_id: str,
        calendar_week: int,
        calendar_day: int,
    ) -> Optional[WellnessDailyReport]:
        """
        Retrieves the latest persisted WellnessDailyReport for (cycle, week, day).
        """
        reports = self.report_repo.list_server_only(
            cycle_id=cycle_id,
            report_type=WELLNESS_DAILY_REPORT_TYPE,
        )
        for r in reports:
            c = r.content or {}
            if c.get("calendar_week") == calendar_week and c.get("calendar_day") == calendar_day:
                try:
                    rep = WellnessDailyReport.model_validate(c)
                    rep.report_id = r.report_id
                    return rep
                except Exception:
                    continue
        return None

    def get_previous_daily_wellness_report(
        self,
        cycle_id: str,
        calendar_week: int,
        calendar_day: int,
    ) -> Optional[WellnessDailyReport]:
        """
        Finds the most recent daily wellness report that preceded (calendar_week, calendar_day).
        For Week 1 Day 1, returns None.
        For Day > 1, checks (week, day - 1).
        For Day 1 (week > 1), checks (week - 1, 7).
        Falls back to any earlier completed wellness report if intermediate days are missing.
        """
        if calendar_day > 1:
            prev_w = calendar_week
            prev_d = calendar_day - 1
        else:
            if calendar_week > 1:
                prev_w = calendar_week - 1
                prev_d = 7
            else:
                return None

        immediate_prev = self.get_daily_wellness_report(cycle_id, prev_w, prev_d)
        if immediate_prev is not None:
            return immediate_prev

        # Backward search within same week
        for d in range(prev_d - 1, 0, -1):
            cand = self.get_daily_wellness_report(cycle_id, prev_w, d)
            if cand is not None:
                return cand

        # Backward search in previous week
        if prev_w > 1:
            for d in range(7, 0, -1):
                cand = self.get_daily_wellness_report(cycle_id, prev_w - 1, d)
                if cand is not None:
                    return cand

        return None

    def list_wellness_reports(self, cycle_id: str) -> List[WellnessDailyReport]:
        """
        Returns all persisted WellnessDailyReport records for cycle_id in chronological order.
        """
        reports = self.report_repo.list_server_only(
            cycle_id=cycle_id,
            report_type=WELLNESS_DAILY_REPORT_TYPE,
        )
        result: List[WellnessDailyReport] = []
        for r in reversed(reports):
            c = r.content or {}
            try:
                rep = WellnessDailyReport.model_validate(c)
                rep.report_id = r.report_id
                result.append(rep)
            except Exception:
                continue
        return result

    async def _execute_gemini_wellness_analysis(
        self,
        summary: WellnessBiometricsSummary,
        calendar_week: int,
        calendar_day: int,
    ) -> WellnessModelOutput:
        """
        Calls Gemini Flash to generate structured clinical trends, gaps, and summary for Athena.
        Separates factual numbers from clinical interpretation.
        """
        system_inst, user_prompt = build_wellness_daily_analysis_prompt(
            biometrics_summary=summary.model_dump(exclude_none=True),
            calendar_week=calendar_week,
            calendar_day=calendar_day,
        )

        raw_response = await self.gateway.generate_structured(
            system_instruction=system_inst,
            prompt=user_prompt,
            response_schema=WellnessModelOutput,
            model=GeminiModelRole.FLASH,
        )

        if isinstance(raw_response, WellnessModelOutput):
            return raw_response
        if isinstance(raw_response, dict):
            return WellnessModelOutput.model_validate(raw_response)
        if isinstance(raw_response, str):
            clean = raw_response.strip()
            if clean.startswith("```"):
                clean = re.sub(r"^```(?:json)?\s*", "", clean)
                clean = re.sub(r"\s*```$", "", clean)
            parsed = json.loads(clean)
            return WellnessModelOutput.model_validate(parsed)

        raise GeminiGatewayError("Unexpected output format from Gemini Flash for Wellness analysis.")

    async def process_wellness_for_day(
        self,
        cycle_id: str,
        calendar_week: int,
        calendar_day: int,
        trigger: Optional[WellnessTriggerType] = None,
        as_of: Optional[datetime] = None,
        commit: bool = True,
        auto_trigger_journal: bool = False,
    ) -> Tuple[WellnessDailyReport, bool]:
        """
        Executes daily Wellness analysis for (week, day).
        Returns (report, is_new: bool).

        Key Behaviors:
        - If report already exists for (week, day), returns existing without overwriting (idempotent).
        - If measurements are completely absent:
          Forms structured NO_DATA report with 0 Gemini calls.
        - If measurements exist:
          Summarizes factual numbers, executes Gemini Flash analysis, persists READY report.
        """
        clean_cycle = (cycle_id or "").strip()
        effective_now = as_of or self._now_utc()

        # 1. Check existing report (idempotency & late data protection)
        existing = self.get_daily_wellness_report(clean_cycle, calendar_week, calendar_day)
        if existing is not None:
            return existing, False

        cycle = self.cycle_repo.get(clean_cycle)
        patient_id = cycle.patient_id if cycle else "unknown"
        tz_name = self.get_patient_timezone(clean_cycle)

        nominal_start, nominal_end = self.resolve_observation_window(
            clean_cycle, calendar_week, calendar_day
        )
        prev_rep = self.get_previous_daily_wellness_report(
            clean_cycle, calendar_week, calendar_day
        )
        if prev_rep is not None and prev_rep.window and prev_rep.window.end_utc:
            prev_end = self._parse_iso_utc(prev_rep.window.end_utc)
            window_start = prev_end if prev_end is not None else nominal_start
        else:
            window_start = nominal_start

        resolved_trigger = trigger or self.evaluate_trigger(
            clean_cycle, calendar_week, calendar_day, as_of=effective_now
        ) or WellnessTriggerType.MANUAL

        if effective_now < nominal_end:
            actual_end = max(effective_now, window_start + timedelta(seconds=1))
        else:
            actual_end = max(nominal_end, window_start + timedelta(seconds=1))

        time_window = WellnessTimeWindow(
            start_utc=window_start.isoformat(),
            end_utc=actual_end.isoformat(),
            user_timezone=tz_name,
            calendar_week=calendar_week,
            calendar_day=calendar_day,
        )

        # 2. Gather measurements up to actual_end
        measurements = self.gather_genuine_measurements(
            cycle_id=clean_cycle,
            start_dt=window_start,
            end_dt=actual_end,
        )

        mode2_episodes = self.mode2_service.get_wellness_episodes_for_window(
            cycle_id=clean_cycle,
            start_dt=window_start,
            end_dt=actual_end,
        )

        now_iso = effective_now.isoformat()
        report_id = f"rep_wellness_{clean_cycle}_w{calendar_week}_d{calendar_day}_{uuid.uuid4().hex[:8]}"

        # 3. Absence of data -> Deterministic NO_DATA report (0 Gemini calls!)
        if not measurements and not mode2_episodes:
            empty_summary = WellnessBiometricsSummary()
            no_data_report = WellnessDailyReport(
                report_id=report_id,
                cycle_id=clean_cycle,
                patient_id=patient_id,
                calendar_week=calendar_week,
                calendar_day=calendar_day,
                status=WellnessStatus.NO_DATA,
                trigger=resolved_trigger,
                window=time_window,
                factual_summary=empty_summary,
                interpretation=None,
                summary="NO_DATA: No objective biometric measurements recorded for this day.",
                assessment=None,
                created_at_utc=now_iso,
            )

            self.report_repo.create_next_version(
                cycle_id=clean_cycle,
                model="aegis-wellness-analyzer",
                report_type=WELLNESS_DAILY_REPORT_TYPE,
                privacy_level=PrivacyLevel.SERVER_ONLY,
                content=no_data_report.model_dump(),
                report_id=report_id,
                commit=commit,
            )

            if auto_trigger_journal:
                await self._auto_trigger_journal(clean_cycle, calendar_week, calendar_day, effective_now)

            return no_data_report, True

        # 4. Data present -> Factual summary + Gemini Flash clinical interpretation
        factual_summary = self.build_factual_biometrics_summary(
            measurements=measurements,
            mode2_episodes=mode2_episodes,
        )
        interpretation = await self._execute_gemini_wellness_analysis(
            summary=factual_summary,
            calendar_week=calendar_week,
            calendar_day=calendar_day,
        )

        ready_report = WellnessDailyReport(
            report_id=report_id,
            cycle_id=clean_cycle,
            patient_id=patient_id,
            calendar_week=calendar_week,
            calendar_day=calendar_day,
            status=WellnessStatus.READY,
            trigger=resolved_trigger,
            window=time_window,
            factual_summary=factual_summary,
            interpretation=interpretation,
            summary=interpretation.summary_for_athena,
            assessment=interpretation.summary_for_athena,
            created_at_utc=now_iso,
        )

        self.report_repo.create_next_version(
            cycle_id=clean_cycle,
            model=self.gateway.flash_model,
            report_type=WELLNESS_DAILY_REPORT_TYPE,
            privacy_level=PrivacyLevel.SERVER_ONLY,
            content=ready_report.model_dump(),
            report_id=report_id,
            commit=commit,
        )

        if auto_trigger_journal:
            await self._auto_trigger_journal(clean_cycle, calendar_week, calendar_day, effective_now)

        return ready_report, True

    async def _auto_trigger_journal(
        self,
        cycle_id: str,
        calendar_week: int,
        calendar_day: int,
        as_of: datetime,
    ) -> None:
        if self.journal_service is None:
            from app.services.athena_daily_journal_service import AthenaDailyJournalService
            self.journal_service = AthenaDailyJournalService(
                db=self.db,
                gateway=self.gateway,
                now_provider=self.now_provider,
            )
        try:
            await self.journal_service.close_daily_journal(
                cycle_id=cycle_id,
                calendar_week=calendar_week,
                calendar_day=calendar_day,
                as_of=as_of,
            )
        except Exception as j_err:
            logger.warning(
                "Auto-trigger Athena Daily Journal for cycle %s w%d d%d failed: %s",
                cycle_id,
                calendar_week,
                calendar_day,
                j_err,
            )

    # ==========================================================================
    # 6. DISCOVERY & SCHEDULING HELPER
    # ==========================================================================

    def get_pending_wellness_windows(
        self,
        cycle_id: str,
        as_of: Optional[datetime] = None,
    ) -> List[Tuple[int, int, WellnessTriggerType]]:
        """
        Discovers all (week, day) observation periods for cycle_id that have
        met a valid trigger (wake-up or midday fallback) and do NOT yet have
        a persisted WellnessDailyReport.
        """
        clean_cycle = (cycle_id or "").strip()
        anchor_dt = self.weekly_service.find_schedule_anchor_timestamp(clean_cycle)
        if anchor_dt is None:
            return []

        effective_now = as_of or self._now_utc()
        if effective_now <= anchor_dt:
            return []

        hours_elapsed = (effective_now - anchor_dt).total_seconds() / 3600.0
        existing_reports = self.list_wellness_reports(clean_cycle)
        total_days_elapsed = max(int(hours_elapsed // 24) + 1, len(existing_reports) + 1)
        existing_days = {(r.calendar_week, r.calendar_day) for r in existing_reports}

        pending: List[Tuple[int, int, WellnessTriggerType]] = []
        for day_idx in range(1, total_days_elapsed + 1):
            w = (day_idx - 1) // 7 + 1
            d = (day_idx - 1) % 7 + 1
            if (w, d) in existing_days:
                continue

            trig = self.evaluate_trigger(clean_cycle, w, d, as_of=effective_now)
            if trig is not None:
                pending.append((w, d, trig))

        return pending
