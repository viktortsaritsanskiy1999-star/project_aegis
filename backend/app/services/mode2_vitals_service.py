import asyncio
from datetime import datetime, timezone, timedelta
from typing import Any, Callable, Dict, List, Optional, Tuple
import uuid

from sqlalchemy.orm import Session

from app.models.model_report import ModelReport
from app.models.workout_session import WorkoutSession
from app.repositories.cycle_repository import MedicalCycleRepository
from app.repositories.measurement_repository import HealthMeasurementRepository
from app.repositories.report_repository import ModelReportRepository
from app.repositories.workout_repository import WorkoutSessionRepository
from app.schemas.privacy import PrivacyLevel
from app.schemas.chat import AthenaChatRequest, AthenaVitalsAlertContext
from app.schemas.mode2 import (
    Mode2AthenaEvent,
    Mode2EpisodeRecord,
    Mode2EpisodeStatus,
    Mode2TelemetryPoint,
    Mode2UserStatement,
    Mode2WorkoutContext,
)
from app.services.athena_chat_service import AthenaChatService
from app.services.gemini_gateway import GeminiGateway

ATHENA_MODE2_EPISODE_REPORT_TYPE = "athena_mode2_episode"

# Independent hard safety bounds (never bypassed, regardless of Pro thresholds)
CRITICAL_BRADYCARDIA_BPM = 40
CRITICAL_TACHYCARDIA_BPM = 140
EXTREME_TACHYCARDIA_BPM = 160
CRITICAL_SPO2 = 90
WARNING_SPO2 = 92

# Default fallback bounds when Pro thresholds are absent
FALLBACK_MIN_PESSIMUM_BPM = 45
FALLBACK_MAX_PESSIMUM_BPM = 115
FALLBACK_MAX_SPIKE_DELTA_BPM = 25

# Red flag keywords for dangerous symptoms during Mode 2
RED_FLAG_KEYWORDS = [
    "боль в груди",
    "давит за грудиной",
    "жжение за грудиной",
    "одышка",
    "задыхаюсь",
    "нехватка воздуха",
    "темнеет в глазах",
    "головокружение",
    "потеря сознания",
    "обморок",
    "онемение руки",
    "сильная слабость",
    "холодный пот",
    "аритмия",
    "перебои в сердце",
    # English terms
    "chest pain",
    "pressure behind sternum",
    "burning in chest",
    "shortness of breath",
    "breathless",
    "dizzy",
    "dizziness",
    "fainting",
    "loss of consciousness",
    "syncope",
    "numbness",
    "severe weakness",
    "cold sweat",
    "arrhythmia",
    "palpitations",
]

WORKOUT_AFFIRMATIVE_KEYWORDS = [
    "тренир",
    "бег",
    "кардио",
    "занима",
    "спорт",
    "упражнен",
    "зал",
    "дорожк",
    "workout",
    "exercise",
]

WORKOUT_NEGATIVE_KEYWORDS = [
    "не тренир",
    "не занима",
    "нет",
    "сижу",
    "отдыха",
    "работаю",
    "лежу",
    "поко",
]

WORKOUT_FINISH_KEYWORDS = [
    "тренировка завершена",
    "тренировку завершил",
    "закончил тренировку",
    "закончила тренировку",
    "завершил тренировку",
    "завершила тренировку",
    "тренировка окончена",
    "закончил заниматься",
    "закончила заниматься",
    "отзанимался",
    "отзанималась",
    "завершить тренировку",
    "workout finished",
    "finish workout",
]


class Mode2VitalsMonitoringService:
    """
    Server-side deterministic operational vitals monitoring service (Mode 2).
    Invariants:
    1. Compares pulse deterministically against Pro thresholds (user_overall_report)
       and independent hard safety bounds (bradycardia <= 40, tachycardia >= 140, SpO2 <= 90).
       Absence of Pro thresholds falls back safely to default medical bounds.
    2. Significant deviation activates a durable Mode 2 episode with unique episode_id,
       persisted in SQLite (model_reports, SERVER_ONLY) resilient across server restarts.
    3. First event is delivered directly to Athena (AthenaChatService). Wellness is NOT an intermediary.
    4. Clarifies workout context and subjective well-being in plain Russian. Answers are stored verbatim.
    5. Confirmed workout does NOT cancel response to dangerous symptoms or alarmingly severe vitals.
    6. Filters subsequent telemetry: delivers 2-minute summary, then ONLY significant changes
       (delta HR >= 15-20 bpm, SpO2 drop <= 92% or >= 3%, red flags, or sustained normalization).
       Minor fluctuations (< 15 bpm) are recorded silently with ZERO Gemini calls.
    7. Sustained normalization closes the episode without false claims of complete cure.
    8. A subsequent spike after closure creates a brand NEW Mode 2 episode with a new episode_id.
    9. Mode 2 questions and interactions DO NOT consume Athena's 3-question daily plan limit.
    10. Enforces strict patient and cycle isolation; zero client leakage of SERVER_ONLY data.
    """

    def __init__(
        self,
        db: Session,
        gateway: Optional[GeminiGateway] = None,
        now_provider: Optional[Callable[[], datetime]] = None,
    ):
        self.db = db
        self.gateway = gateway or GeminiGateway()
        self.now_provider = now_provider
        self.cycle_repo = MedicalCycleRepository(db)
        self.report_repo = ModelReportRepository(db)
        self.meas_repo = HealthMeasurementRepository(db)
        self.workout_repo = WorkoutSessionRepository(db)
        self.athena_chat_service = AthenaChatService(
            db=db, gateway=self.gateway, now_provider=now_provider
        )

    def _now_utc(self) -> datetime:
        if self.now_provider is not None:
            now = self.now_provider()
            if now.tzinfo is None:
                return now.replace(tzinfo=timezone.utc)
            return now.astimezone(timezone.utc)
        return datetime.now(timezone.utc)

    # ==========================================================================
    # WORKOUT SESSION & CONTEXT LIFECYCLE MANAGEMENT
    # ==========================================================================

    def get_active_workout_session(
        self,
        cycle_id: str,
        as_of: Optional[datetime] = None,
    ) -> Optional[WorkoutSession]:
        """
        Retrieves the currently active workout session for the medical cycle.
        A session is active if its status is 'active' and it has not timed out (> 3 hours).
        Timed out sessions are automatically finished to prevent state leak.
        """
        now_dt = as_of or self._now_utc()
        sessions = self.workout_repo.list_by_cycle(cycle_id)
        for s in sessions:
            if s.status == "active":
                ref_time = s.activity_confirmed_at or s.anomaly_detected_at or s.created_at
                if ref_time.tzinfo is None:
                    ref_time = ref_time.replace(tzinfo=timezone.utc)
                if (now_dt - ref_time) > timedelta(hours=3):
                    self.workout_repo.finish_data_collection(s.session_id)
                    continue
                return s
        return None

    def _ensure_active_workout_session(
        self,
        cycle_id: str,
        confirmed_at: datetime,
        session_id: Optional[str] = None,
        anomaly_detected_at: Optional[datetime] = None,
    ) -> WorkoutSession:
        act = self.get_active_workout_session(cycle_id, as_of=confirmed_at)
        if act is not None:
            return act

        clean_dt = confirmed_at if confirmed_at.tzinfo is not None else confirmed_at.replace(tzinfo=timezone.utc)
        detected_dt = anomaly_detected_at or clean_dt
        if detected_dt.tzinfo is None:
            detected_dt = detected_dt.replace(tzinfo=timezone.utc)

        sid = session_id or f"ws_{cycle_id}_{uuid.uuid4().hex[:10]}"
        return self.workout_repo.create(
            session_id=sid,
            cycle_id=cycle_id,
            anomaly_detected_at=detected_dt,
            activity_confirmed_at=clean_dt,
            status="active",
        )

    def complete_workout_session(
        self,
        cycle_id: str,
        session_id: Optional[str] = None,
        completed_at: Optional[datetime] = None,
        physiological_end_time: Optional[datetime] = None,
    ) -> Optional[WorkoutSession]:
        """
        Transitions active workout session to 'recovering'.
        Matches client finishWorkout flow.
        Idempotent: if session is already recovering or completed, returns it safely.
        """
        now_dt = completed_at or self._now_utc()
        if session_id:
            ws = self.workout_repo.complete_workout(
                session_id=session_id,
                workout_completed_at=now_dt,
                physiological_end_time=physiological_end_time,
            )
            if ws:
                return ws
        act = self.get_active_workout_session(cycle_id, as_of=now_dt)
        if act:
            return self.workout_repo.complete_workout(
                session_id=act.session_id,
                workout_completed_at=now_dt,
                physiological_end_time=physiological_end_time,
            )
        # Idempotent fallback: find most recent recovering/completed session for cycle
        recent_sessions = self.workout_repo.list_by_cycle(cycle_id)
        for s in recent_sessions:
            if s.status in ("recovering", "completed"):
                return s
        return None

    def finish_workout(
        self,
        cycle_id: str,
        session_id: Optional[str] = None,
        completed_at: Optional[datetime] = None,
    ) -> Optional[WorkoutSession]:
        """Convenience alias matching client-side finishWorkout."""
        return self.complete_workout_session(
            cycle_id=cycle_id,
            session_id=session_id,
            completed_at=completed_at,
        )

    def finish_workout_recovery(
        self,
        cycle_id: str,
        session_id: Optional[str] = None,
    ) -> Optional[WorkoutSession]:
        """
        Transitions a recovering workout session to 'completed'.
        """
        if session_id:
            return self.workout_repo.finish_data_collection(session_id)
        sessions = self.workout_repo.list_by_cycle(cycle_id)
        for s in sessions:
            if s.status == "recovering":
                return self.workout_repo.finish_data_collection(s.session_id)
        return None

    def resolve_thresholds(self, cycle_id: str) -> Dict[str, Any]:
        """
        Extracts personal vitals thresholds established by Pro in user_overall_report.
        If absent, applies safe fallback bounds. Always includes hard safety bounds.
        """
        overall_reports = self.report_repo.list_client_facing(
            cycle_id=cycle_id,
            report_type="user_overall_report",
        )
        if overall_reports and overall_reports[0].content:
            metrics = overall_reports[0].content.get("metrics") or {}
            if "min_pessimum_bpm" in metrics and "max_pessimum_bpm" in metrics:
                return {
                    "source": "pro",
                    "min_pessimum_bpm": int(metrics.get("min_pessimum_bpm", FALLBACK_MIN_PESSIMUM_BPM)),
                    "max_pessimum_bpm": int(metrics.get("max_pessimum_bpm", FALLBACK_MAX_PESSIMUM_BPM)),
                    "max_spike_delta_bpm": int(metrics.get("max_spike_delta_bpm", FALLBACK_MAX_SPIKE_DELTA_BPM)),
                    "critical_bradycardia_bpm": CRITICAL_BRADYCARDIA_BPM,
                    "critical_tachycardia_bpm": CRITICAL_TACHYCARDIA_BPM,
                    "extreme_tachycardia_bpm": EXTREME_TACHYCARDIA_BPM,
                    "critical_spo2": CRITICAL_SPO2,
                    "warning_spo2": WARNING_SPO2,
                }

        return {
            "source": "fallback",
            "min_pessimum_bpm": FALLBACK_MIN_PESSIMUM_BPM,
            "max_pessimum_bpm": FALLBACK_MAX_PESSIMUM_BPM,
            "max_spike_delta_bpm": FALLBACK_MAX_SPIKE_DELTA_BPM,
            "critical_bradycardia_bpm": CRITICAL_BRADYCARDIA_BPM,
            "critical_tachycardia_bpm": CRITICAL_TACHYCARDIA_BPM,
            "extreme_tachycardia_bpm": EXTREME_TACHYCARDIA_BPM,
            "critical_spo2": CRITICAL_SPO2,
            "warning_spo2": WARNING_SPO2,
        }

    def get_active_episode(self, cycle_id: str) -> Optional[Mode2EpisodeRecord]:
        """
        Retrieves the current active Mode 2 episode for the cycle, or None if no active episode.
        Survives server restart by querying SQLite model_reports directly.
        Inspects the latest version of each unique episode_id.
        """
        reports = self.report_repo.list_server_only(
            cycle_id=cycle_id,
            report_type=ATHENA_MODE2_EPISODE_REPORT_TYPE,
        )
        seen_episodes = set()
        for r in reports:
            if not r.content or "episode_id" not in r.content:
                continue
            ep_id = r.content["episode_id"]
            if ep_id in seen_episodes:
                continue
            seen_episodes.add(ep_id)
            # Since reports is ordered by version.desc(), this is the latest version of ep_id
            if r.content.get("status") == Mode2EpisodeStatus.ACTIVE.value:
                try:
                    return Mode2EpisodeRecord.model_validate(r.content)
                except Exception:
                    continue
        return None

    def get_episode_history(self, cycle_id: str) -> List[Mode2EpisodeRecord]:
        """
        Retrieves all Mode 2 episodes (active and closed) for the cycle in chronological order.
        Takes the latest version of each distinct episode_id.
        """
        reports = self.report_repo.list_server_only(
            cycle_id=cycle_id,
            report_type=ATHENA_MODE2_EPISODE_REPORT_TYPE,
        )
        latest_per_episode: Dict[str, Mode2EpisodeRecord] = {}
        for r in reports:
            if not r.content or "episode_id" not in r.content:
                continue
            ep_id = r.content["episode_id"]
            if ep_id in latest_per_episode:
                continue
            try:
                latest_per_episode[ep_id] = Mode2EpisodeRecord.model_validate(r.content)
            except Exception:
                continue
        episodes = list(latest_per_episode.values())
        episodes.sort(key=lambda ep: ep.started_at_utc)
        return episodes

    def save_episode(
        self,
        episode: Mode2EpisodeRecord,
        commit: bool = True,
    ) -> ModelReport:
        """
        Persists or updates the Mode 2 episode record in model_reports (SERVER_ONLY).
        Uses create_next_version to maintain versioned auditability.
        """
        report = self.report_repo.create_next_version(
            cycle_id=episode.cycle_id,
            model=self.gateway.flash_model,
            report_type=ATHENA_MODE2_EPISODE_REPORT_TYPE,
            privacy_level=PrivacyLevel.SERVER_ONLY,
            content=episode.model_dump(),
            commit=commit,
        )
        return report

    def determine_baseline_pulse(self, cycle_id: str, default_pulse: int = 70) -> int:
        """
        Determines the resting baseline pulse by inspecting recent non-historical measurements.
        Falls back safely to normal resting (default 70 bpm).
        """
        recent = self.meas_repo.list_by_cycle(
            cycle_id=cycle_id,
            is_historical=False,
            limit=15,
        )
        valid_pulses = [
            m.pulse for m in recent
            if m.pulse is not None and 45 <= m.pulse <= 100
        ]
        if valid_pulses:
            return sorted(valid_pulses)[len(valid_pulses) // 2]
        return min(default_pulse, 70) if default_pulse > 0 else 70

    def is_deviation_detected(
        self,
        pulse: int,
        spo2: Optional[int],
        baseline_pulse: int,
        thresholds: Dict[str, Any],
    ) -> Tuple[bool, str]:
        """
        Evaluates whether a pulse/vitals measurement represents an abnormal deviation.
        Returns (is_deviation: bool, reason: str).
        """
        # 1. Hard independent safety limits
        if pulse <= thresholds["critical_bradycardia_bpm"]:
            return True, f"Критическая брадикардия: пульс {pulse} уд/мин (ниже абсолютного порога безопасности {thresholds['critical_bradycardia_bpm']} уд/мин)."
        if pulse >= thresholds["critical_tachycardia_bpm"]:
            return True, f"Выраженная тахикардия: пульс {pulse} уд/мин (выше абсолютного порога безопасности {thresholds['critical_tachycardia_bpm']} уд/мин)."
        if spo2 is not None and spo2 <= thresholds["critical_spo2"]:
            return True, f"Критическое падение сатурации: SpO2 {spo2}% (ниже порога {thresholds['critical_spo2']}%)."

        # 2. Personal / Fallback thresholds
        if pulse < thresholds["min_pessimum_bpm"]:
            return True, f"Пульс {pulse} уд/мин ниже минимальной границы ({thresholds['min_pessimum_bpm']} уд/мин, источник: {thresholds['source']})."
        if pulse > thresholds["max_pessimum_bpm"]:
            return True, f"Пульс {pulse} уд/мин превышает верхнюю границу ({thresholds['max_pessimum_bpm']} уд/мин, источник: {thresholds['source']})."

        delta = pulse - baseline_pulse
        if delta >= thresholds["max_spike_delta_bpm"]:
            return True, f"Скачок пульса на +{delta} уд/мин от исходного уровня {baseline_pulse} уд/мин (порог дельты: +{thresholds['max_spike_delta_bpm']} уд/мин)."

        return False, ""

    async def process_measurement(
        self,
        cycle_id: str,
        pulse: Optional[int],
        spo2: Optional[int] = None,
        systolic: Optional[int] = None,
        diastolic: Optional[int] = None,
        source: str = "watch_sensor",
        is_simulated: bool = False,
        measured_at: Optional[datetime] = None,
        note: Optional[str] = None,
        phase: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> Optional[Mode2EpisodeRecord]:
        """
        Core operational ingestion method.
        Analyzes fresh measurement against active episode state and thresholds:
        - If no active episode and deviation detected: activates new Mode 2 episode and notifies Athena.
        - If active episode exists: records telemetry, delivers 2-minute summary, filters subsequent deltas,
          and closes upon sustained normalization.
        """
        now_dt = measured_at or self._now_utc()
        if now_dt.tzinfo is None:
            now_dt = now_dt.replace(tzinfo=timezone.utc)
        now_iso = now_dt.isoformat()

        if pulse is None or pulse <= 0:
            return self.get_active_episode(cycle_id)

        thresholds = self.resolve_thresholds(cycle_id)
        active_ep = self.get_active_episode(cycle_id)

        # Check if incoming telemetry indicates transition to post-workout recovery
        if phase and any(r in phase.lower() for r in ("recovery", "recovering")):
            act_w = self.get_active_workout_session(cycle_id, as_of=now_dt)
            if act_w is not None:
                self.complete_workout_session(cycle_id, session_id=act_w.session_id, completed_at=now_dt)

        # ----------------------------------------------------------------------
        # SCENARIO A: NO ACTIVE EPISODE
        # ----------------------------------------------------------------------
        if active_ep is None:
            baseline = self.determine_baseline_pulse(cycle_id, default_pulse=pulse)
            is_dev, reason = self.is_deviation_detected(
                pulse=pulse,
                spo2=spo2,
                baseline_pulse=baseline,
                thresholds=thresholds,
            )
            if not is_dev:
                # Normal reading, standby mode
                return None

            active_workout = self.get_active_workout_session(cycle_id, as_of=now_dt)
            is_confirmed_workout = (active_workout is not None)

            # Open a brand NEW Mode 2 Episode
            cycle = self.cycle_repo.get(cycle_id)
            patient_id = cycle.patient_id if cycle else "unknown_patient"
            episode_id = f"ep_mode2_{cycle_id}_{uuid.uuid4().hex[:12]}"

            resolved_context = (
                Mode2WorkoutContext.CONFIRMED_WORKOUT
                if is_confirmed_workout
                else Mode2WorkoutContext.UNKNOWN
            )

            new_ep = Mode2EpisodeRecord(
                episode_id=episode_id,
                cycle_id=cycle_id,
                patient_id=patient_id,
                session_id=active_workout.session_id if active_workout else session_id,
                status=Mode2EpisodeStatus.ACTIVE,
                started_at_utc=now_iso,
                baseline_pulse=baseline,
                trigger_pulse=pulse,
                trigger_spo2=spo2,
                trigger_source=source,
                trigger_reason=reason,
                min_pulse=pulse,
                max_pulse=pulse,
                latest_pulse=pulse,
                latest_spo2=spo2,
                thresholds_applied=thresholds,
                workout_context=resolved_context,
                telemetry_samples=[
                    Mode2TelemetryPoint(
                        timestamp_utc=now_iso,
                        pulse=pulse,
                        spo2=spo2,
                        systolic=systolic,
                        diastolic=diastolic,
                        source=source,
                        phase=phase,
                        is_simulated=is_simulated,
                        note=note,
                    )
                ],
                athena_events=[],
                last_notified_at_utc=now_iso,
                last_notified_pulse=pulse,
                last_notified_spo2=spo2,
                initial_two_minute_update_sent=False,
                consecutive_normal_readings=0,
            )

            is_critical = (
                pulse <= thresholds["critical_bradycardia_bpm"]
                or pulse >= thresholds["critical_tachycardia_bpm"]
                or (spo2 is not None and spo2 <= thresholds["critical_spo2"])
            )

            if not is_confirmed_workout or is_critical:
                # Direct delivery to Athena (AthenaChatService)
                athena_alert_request = AthenaChatRequest(
                    event_type="vitals_alert",
                    vitals_alert=AthenaVitalsAlertContext(
                        current_pulse=pulse,
                        baseline_pulse=baseline,
                        measured_at=now_iso,
                        notes=(
                            f"CRITICAL DURING CONFIRMED WORKOUT: {reason}"
                            if is_confirmed_workout
                            else (f"{reason} (SpO2: {spo2}%" if spo2 else reason)
                        ),
                    ),
                    message=None,
                )

                athena_resp = await self.athena_chat_service.handle_turn(
                    patient_id=patient_id,
                    request=athena_alert_request,
                    cycle_id=cycle_id,
                )

                new_ep.athena_events.append(
                    Mode2AthenaEvent(
                        event_type="initial_alert",
                        timestamp_utc=now_iso,
                        prompt_summary=f"HR spike: {pulse} bpm (baseline {baseline} bpm). {reason}",
                        athena_reply=athena_resp.reply,
                        emergency_trigger=athena_resp.emergency_trigger,
                    )
                )
            else:
                # Confirmed workout continuation: silent logging, NO duplicate question to user!
                new_ep.athena_events.append(
                    Mode2AthenaEvent(
                        event_type="workout_continuation",
                        timestamp_utc=now_iso,
                        prompt_summary=f"Confirmed workout continued: pulse {pulse} bpm (baseline {baseline} bpm). {reason}",
                        athena_reply="[Mode 2: Confirmed workout active, continuous observation without redundant prompt]",
                        emergency_trigger=False,
                    )
                )

            self.save_episode(new_ep, commit=True)
            return new_ep

        # ----------------------------------------------------------------------
        # SCENARIO B: ACTIVE EPISODE EXISTS
        # ----------------------------------------------------------------------
        if active_ep.workout_context == Mode2WorkoutContext.UNKNOWN:
            act_w = self.get_active_workout_session(cycle_id, as_of=now_dt)
            if act_w is not None:
                active_ep.workout_context = Mode2WorkoutContext.CONFIRMED_WORKOUT
                if not active_ep.session_id:
                    active_ep.session_id = act_w.session_id

        active_ep.telemetry_samples.append(
            Mode2TelemetryPoint(
                timestamp_utc=now_iso,
                pulse=pulse,
                spo2=spo2,
                systolic=systolic,
                diastolic=diastolic,
                source=source,
                phase=phase,
                is_simulated=is_simulated,
                note=note,
            )
        )
        active_ep.min_pulse = min(active_ep.min_pulse, pulse)
        active_ep.max_pulse = max(active_ep.max_pulse, pulse)
        active_ep.latest_pulse = pulse
        if spo2 is not None:
            active_ep.latest_spo2 = spo2

        started_dt = datetime.fromisoformat(active_ep.started_at_utc)
        if started_dt.tzinfo is None:
            started_dt = started_dt.replace(tzinfo=timezone.utc)
        elapsed_sec = (now_dt - started_dt).total_seconds()

        # Check Subcase 1: Initial 2-minute summary delivery
        if not active_ep.initial_two_minute_update_sent and elapsed_sec >= 110:
            active_ep.initial_two_minute_update_sent = True
            active_ep.last_notified_at_utc = now_iso
            active_ep.last_notified_pulse = pulse
            active_ep.last_notified_spo2 = spo2

            is_normal_now = (
                thresholds["min_pessimum_bpm"] <= pulse <= thresholds["max_pessimum_bpm"]
                and (spo2 is None or spo2 >= thresholds["warning_spo2"])
            )
            active_ep.consecutive_normal_readings = 1 if is_normal_now else 0

            summary_msg = (
                f"Сводка телеметрии через {int(elapsed_sec // 60)} мин наблюдения:\n"
                f"- Текущий пульс: {pulse} уд/мин (за эпизод: мин {active_ep.min_pulse}, макс {active_ep.max_pulse})\n"
                f"- Исходный пульс до скачка: {active_ep.baseline_pulse} уд/мин\n"
                f"- Сатурация SpO2: {f'{spo2}%' if spo2 is not None else 'не зафиксирована'}\n"
                f"- Статус физической активности: {active_ep.workout_context.value}\n"
            )
            if active_ep.user_statements:
                last_user_stmt = active_ep.user_statements[-1].text
                summary_msg += f"- Ответ пользователя: «{last_user_stmt}»\n"

            athena_update_request = AthenaChatRequest(
                event_type="mode2_update",
                message=summary_msg,
            )
            athena_resp = await self.athena_chat_service.handle_turn(
                patient_id=active_ep.patient_id,
                request=athena_update_request,
                cycle_id=cycle_id,
            )

            active_ep.athena_events.append(
                Mode2AthenaEvent(
                    event_type="two_minute_summary",
                    timestamp_utc=now_iso,
                    prompt_summary=summary_msg,
                    athena_reply=athena_resp.reply,
                    emergency_trigger=athena_resp.emergency_trigger,
                )
            )

            self.save_episode(active_ep, commit=True)
            return active_ep

        # Check Subcase 2: After 2-minute summary -> Telemetry Filtering Rules
        is_normal = (
            thresholds["min_pessimum_bpm"] <= pulse <= thresholds["max_pessimum_bpm"]
            and (spo2 is None or spo2 >= thresholds["warning_spo2"])
        )

        if is_normal:
            active_ep.consecutive_normal_readings += 1
            if active_ep.consecutive_normal_readings >= 2:
                # Sustained normalization -> Clean episode closure
                norm_msg = (
                    f"Стойкая нормализация показателей:\n"
                    f"- Пульс стабилизировался на уровне {pulse} уд/мин (безопасный диапазон {thresholds['min_pessimum_bpm']}–{thresholds['max_pessimum_bpm']} уд/мин).\n"
                    f"- SpO2: {f'{spo2}%' if spo2 is not None else 'в норме'}.\n"
                    f"- Завершение эпизода Mode 2 ({active_ep.episode_id}).\n"
                    f"Напомните пользователю продолжать прислушиваться к своему состоянию. "
                    f"ЗАПРЕЩЕНО заявлять о полном выздоровлении или восстановлении сердечно-сосудистой системы."
                )

                athena_norm_request = AthenaChatRequest(
                    event_type="mode2_update",
                    message=norm_msg,
                )
                athena_resp = await self.athena_chat_service.handle_turn(
                    patient_id=active_ep.patient_id,
                    request=athena_norm_request,
                    cycle_id=cycle_id,
                )

                active_ep.athena_events.append(
                    Mode2AthenaEvent(
                        event_type="sustained_normalization",
                        timestamp_utc=now_iso,
                        prompt_summary=f"Normalization to {pulse} bpm",
                        athena_reply=athena_resp.reply,
                        emergency_trigger=athena_resp.emergency_trigger,
                    )
                )

                active_ep.status = Mode2EpisodeStatus.CLOSED
                active_ep.closed_at_utc = now_iso
                self.save_episode(active_ep, commit=True)
                return active_ep
            else:
                # First normal reading: record silently, await sustained confirmation
                self.save_episode(active_ep, commit=True)
                return active_ep

        # Still outside normal range
        active_ep.consecutive_normal_readings = 0
        last_notified = active_ep.last_notified_pulse or active_ep.trigger_pulse
        delta_hr = abs(pulse - last_notified)

        spo2_dropped = False
        if active_ep.last_notified_spo2 is not None and spo2 is not None:
            if (active_ep.last_notified_spo2 - spo2) >= 3 or spo2 <= thresholds["critical_spo2"]:
                spo2_dropped = True

        is_extreme = (pulse >= EXTREME_TACHYCARDIA_BPM or pulse <= CRITICAL_BRADYCARDIA_BPM)

        # Significant deterioration / change threshold: delta >= 15 bpm or SpO2 drop or extreme
        if delta_hr >= 15 or spo2_dropped or is_extreme:
            deterioration_reason = []
            if delta_hr >= 15:
                deterioration_reason.append(f"изменение ЧСС на {delta_hr} уд/мин ({last_notified} -> {pulse} уд/мин)")
            if spo2_dropped:
                deterioration_reason.append(f"падение SpO2 до {spo2}%")
            if is_extreme:
                deterioration_reason.append(f"экстремальный пульс {pulse} уд/мин")

            update_msg = (
                f"Значимое изменение телеметрии во время Mode 2:\n"
                f"- Причина уведомления: {', '.join(deterioration_reason)}\n"
                f"- Текущий пульс: {pulse} уд/мин, SpO2: {f'{spo2}%' if spo2 is not None else 'нет данных'}\n"
                f"- Контекст тренировки: {active_ep.workout_context.value}\n"
            )

            athena_det_request = AthenaChatRequest(
                event_type="mode2_update",
                message=update_msg,
            )
            athena_resp = await self.athena_chat_service.handle_turn(
                patient_id=active_ep.patient_id,
                request=athena_det_request,
                cycle_id=cycle_id,
            )

            active_ep.athena_events.append(
                Mode2AthenaEvent(
                    event_type="significant_change",
                    timestamp_utc=now_iso,
                    prompt_summary=update_msg,
                    athena_reply=athena_resp.reply,
                    emergency_trigger=athena_resp.emergency_trigger,
                )
            )

            active_ep.last_notified_at_utc = now_iso
            active_ep.last_notified_pulse = pulse
            active_ep.last_notified_spo2 = spo2

            self.save_episode(active_ep, commit=True)
            return active_ep

        # Minor fluctuation (< 15 bpm delta, no SpO2 drop): save silently without calling Gemini
        self.save_episode(active_ep, commit=True)
        return active_ep

    async def handle_user_statement(
        self,
        cycle_id: str,
        user_text: str,
        is_workout: Optional[bool] = None,
        symptoms: Optional[str] = None,
        as_of: Optional[datetime] = None,
    ) -> Optional[Mode2EpisodeRecord]:
        """
        Records user statements verbatim with timestamp into the active Mode 2 episode.
        Resolves physical activity context ('confirmed_workout' vs 'not_working_out').
        Filters for dangerous symptoms (red flags). Confirmed workout does NOT cancel safety response.
        """
        active_ep = self.get_active_episode(cycle_id)
        if active_ep is None:
            return None

        now_dt = as_of or self._now_utc()
        if now_dt.tzinfo is None:
            now_dt = now_dt.replace(tzinfo=timezone.utc)
        now_iso = now_dt.isoformat()

        # 1. Store statement verbatim
        active_ep.user_statements.append(
            Mode2UserStatement(
                text=user_text,
                timestamp_utc=now_iso,
                is_workout_confirmation=is_workout,
                symptoms=symptoms,
            )
        )

        # 2. Resolve workout context
        lower_text = user_text.lower()
        if is_workout is True:
            active_ep.workout_context = Mode2WorkoutContext.CONFIRMED_WORKOUT
            act_w = self._ensure_active_workout_session(
                cycle_id=cycle_id,
                confirmed_at=now_dt,
                session_id=active_ep.session_id,
            )
            if not active_ep.session_id:
                active_ep.session_id = act_w.session_id
        elif is_workout is False:
            active_ep.workout_context = Mode2WorkoutContext.NOT_WORKING_OUT
        else:
            if any(kw in lower_text for kw in WORKOUT_NEGATIVE_KEYWORDS):
                active_ep.workout_context = Mode2WorkoutContext.NOT_WORKING_OUT
            elif any(kw in lower_text for kw in WORKOUT_AFFIRMATIVE_KEYWORDS):
                active_ep.workout_context = Mode2WorkoutContext.CONFIRMED_WORKOUT
                act_w = self._ensure_active_workout_session(
                    cycle_id=cycle_id,
                    confirmed_at=now_dt,
                    session_id=active_ep.session_id,
                )
                if not active_ep.session_id:
                    active_ep.session_id = act_w.session_id

        # Check if user statement signals workout completion
        if any(kw in lower_text for kw in WORKOUT_FINISH_KEYWORDS):
            self.complete_workout_session(cycle_id=cycle_id, completed_at=now_dt)

        # 3. Detect dangerous symptoms / red flags
        combined_text = f"{user_text} {symptoms or ''}".lower()
        has_red_flags = any(rf in combined_text for rf in RED_FLAG_KEYWORDS)
        is_extreme_vitals = (
            active_ep.latest_pulse >= EXTREME_TACHYCARDIA_BPM
            or active_ep.latest_pulse <= CRITICAL_BRADYCARDIA_BPM
            or (active_ep.latest_spo2 is not None and active_ep.latest_spo2 <= CRITICAL_SPO2)
        )

        # 4. Formulate Athena instruction
        prompt_lines = [
            f"Ответ пользователя в режиме мониторинга пульса (Mode 2):\n«{user_text}»\n",
            f"- Зафиксированный контекст тренировки: {active_ep.workout_context.value}",
            f"- Текущий пульс: {active_ep.latest_pulse} уд/мин (пик: {active_ep.max_pulse} уд/мин, исходный: {active_ep.baseline_pulse} уд/мин)",
            f"- SpO2: {f'{active_ep.latest_spo2}%' if active_ep.latest_spo2 is not None else 'нет данных'}",
        ]

        if has_red_flags or is_extreme_vitals:
            prompt_lines.append(
                "\nТРЕВОЖНЫЙ СИГНАЛ: Пользователь сообщил об опасных симптомах или показатели критические! "
                "Подтверждённая тренировка НЕ отменяет экстренную реакцию. "
                "Немедленно рекомендуйте прекратить любую физическую нагрузку, сесть, обеспечить доступ воздуха "
                "и при нарастании симптомов вызвать скорую медицинскую помощь (112). "
                "Не ставьте диагнозов и не назначайте лекарств. "
                "Если упоминалась скорая, ЗАПРЕЩЕНО заявлять о прибытии помощи без подтверждения от пользователя."
            )
        elif active_ep.workout_context == Mode2WorkoutContext.CONFIRMED_WORKOUT:
            prompt_lines.append(
                "\nТренировка подтверждена пользователем, критических симптомов не заявлено. "
                "Поддержите, порекомендуйте следить за темпом и водным балансом, плавно снизить интенсивность, "
                "если пульс не снижается. Напомните, что вы продолжаете фоновый мониторинг."
            )
        else:
            prompt_lines.append(
                "\nПользователь не тренируется. Помогите заземлиться, успокоиться, сесть удобно и подышать. "
                "Уточните, как меняется самочувствие."
            )

        full_prompt = "\n".join(prompt_lines)

        athena_request = AthenaChatRequest(
            event_type="mode2_update",
            message=full_prompt,
        )

        athena_resp = await self.athena_chat_service.handle_turn(
            patient_id=active_ep.patient_id,
            request=athena_request,
            cycle_id=cycle_id,
        )

        active_ep.athena_events.append(
            Mode2AthenaEvent(
                event_type="user_statement_reply",
                timestamp_utc=now_iso,
                prompt_summary=user_text,
                athena_reply=athena_resp.reply,
                emergency_trigger=athena_resp.emergency_trigger,
            )
        )

        self.save_episode(active_ep, commit=True)
        return active_ep

    def record_user_statement_sync(
        self,
        cycle_id: str,
        user_text: str,
        as_of: Optional[datetime] = None,
    ) -> Optional[Mode2EpisodeRecord]:
        """
        Synchronous helper to record a user statement into an active Mode 2 episode
        when chat turns occur through AthenaChatService.
        """
        active_ep = self.get_active_episode(cycle_id)
        if active_ep is None:
            return None

        now_dt = as_of or self._now_utc()
        if now_dt.tzinfo is None:
            now_dt = now_dt.replace(tzinfo=timezone.utc)
        now_iso = now_dt.isoformat()

        active_ep.user_statements.append(
            Mode2UserStatement(
                text=user_text,
                timestamp_utc=now_iso,
            )
        )

        lower = user_text.lower()
        if any(kw in lower for kw in WORKOUT_NEGATIVE_KEYWORDS):
            active_ep.workout_context = Mode2WorkoutContext.NOT_WORKING_OUT
        elif any(kw in lower for kw in WORKOUT_AFFIRMATIVE_KEYWORDS):
            active_ep.workout_context = Mode2WorkoutContext.CONFIRMED_WORKOUT
            act_w = self._ensure_active_workout_session(
                cycle_id=cycle_id,
                confirmed_at=now_dt,
                session_id=active_ep.session_id,
            )
            if not active_ep.session_id:
                active_ep.session_id = act_w.session_id

        if any(kw in lower for kw in WORKOUT_FINISH_KEYWORDS):
            self.complete_workout_session(cycle_id=cycle_id, completed_at=now_dt)

        self.save_episode(active_ep, commit=True)
        return active_ep

    def has_active_episode(self, cycle_id: str) -> bool:
        """Checks if a Mode 2 episode is currently active for the cycle."""
        return self.get_active_episode(cycle_id) is not None

    def close_episode(
        self,
        cycle_id: str,
        reason: str = "manual_closure",
    ) -> Optional[Mode2EpisodeRecord]:
        """Manually closes the active Mode 2 episode if one exists."""
        active_ep = self.get_active_episode(cycle_id)
        if active_ep is None:
            return None

        now_iso = self._now_utc().isoformat()
        active_ep.status = Mode2EpisodeStatus.CLOSED
        active_ep.closed_at_utc = now_iso
        self.save_episode(active_ep, commit=True)
        return active_ep

    def build_wellness_summary(self, episode: Mode2EpisodeRecord) -> Any:
        """
        Transforms a Mode 2 episode record into a structured Mode2EpisodeWellnessSummary
        strictly separating:
        1. Objective measurements
        2. User statements & confirmed facts
        3. Model interpretations & Athena events
        """
        from app.schemas.wellness import (
            Mode2EpisodeWellnessSummary,
            Mode2ModelContext,
            Mode2ObjectiveMetrics,
            Mode2UserFacts,
        )

        # 1. Objective measurements
        pulse_dyn = [
            {
                "timestamp_utc": p.timestamp_utc,
                "pulse": p.pulse,
                "phase": p.phase or "monitoring",
            }
            for p in episode.telemetry_samples
        ]

        spo2_history: List[Dict[str, Any]] = []
        if episode.trigger_spo2 is not None:
            spo2_history.append({
                "timestamp_utc": episode.started_at_utc,
                "spo2": episode.trigger_spo2,
            })
        for p in episode.telemetry_samples:
            if p.spo2 is not None:
                if not any(h["timestamp_utc"] == p.timestamp_utc for h in spo2_history):
                    spo2_history.append({
                        "timestamp_utc": p.timestamp_utc,
                        "spo2": p.spo2,
                    })

        spo2_vals = [h["spo2"] for h in spo2_history]
        spo2_min = min(spo2_vals) if spo2_vals else None
        spo2_max = max(spo2_vals) if spo2_vals else None

        started_dt = datetime.fromisoformat(episode.started_at_utc)
        if started_dt.tzinfo is None:
            started_dt = started_dt.replace(tzinfo=timezone.utc)

        if episode.closed_at_utc:
            end_obs_dt = datetime.fromisoformat(episode.closed_at_utc)
            if end_obs_dt.tzinfo is None:
                end_obs_dt = end_obs_dt.replace(tzinfo=timezone.utc)
        elif episode.telemetry_samples:
            last_ts = datetime.fromisoformat(episode.telemetry_samples[-1].timestamp_utc)
            end_obs_dt = last_ts if last_ts.tzinfo is not None else last_ts.replace(tzinfo=timezone.utc)
        else:
            end_obs_dt = started_dt

        duration_mins = max(round((end_obs_dt - started_dt).total_seconds() / 60.0, 1), 0.0)

        # Telemetry gaps detection
        gaps: List[str] = []
        if len(episode.telemetry_samples) >= 2:
            sorted_samples = sorted(
                episode.telemetry_samples,
                key=lambda s: datetime.fromisoformat(s.timestamp_utc),
            )
            for i in range(len(sorted_samples) - 1):
                t1 = datetime.fromisoformat(sorted_samples[i].timestamp_utc)
                t2 = datetime.fromisoformat(sorted_samples[i + 1].timestamp_utc)
                delta_m = (t2 - t1).total_seconds() / 60.0
                phase_i = sorted_samples[i].phase or ""
                phase_next = sorted_samples[i + 1].phase or ""
                if "recovery_10_50" in phase_i or "recovery_10_50" in phase_next:
                    gap_threshold = 7.0
                elif "recovery_0_10" in phase_i or "recovery_0_10" in phase_next:
                    gap_threshold = 4.0
                elif phase_i == "workout":
                    gap_threshold = 3.0
                elif episode.workout_context == Mode2WorkoutContext.CONFIRMED_WORKOUT and not phase_i:
                    gap_threshold = 3.0
                else:
                    gap_threshold = 12.0
                if delta_m > gap_threshold:
                    gaps.append(
                        f"Пробел {round(delta_m, 1)} мин между {sorted_samples[i].timestamp_utc} и {sorted_samples[i+1].timestamp_utc}"
                    )

        obj_metrics = Mode2ObjectiveMetrics(
            min_pulse=episode.min_pulse,
            max_pulse=episode.max_pulse,
            trigger_pulse=episode.trigger_pulse,
            baseline_pulse=episode.baseline_pulse,
            pulse_dynamics=pulse_dyn,
            spo2_min=spo2_min,
            spo2_max=spo2_max,
            spo2_history=spo2_history,
            duration_minutes=duration_mins,
            telemetry_samples_count=len(episode.telemetry_samples),
            telemetry_gaps=gaps,
        )

        # 2. User statements & confirmed facts
        user_stmts = [s.text for s in episode.user_statements]
        symptoms_found: List[str] = []
        for s in episode.user_statements:
            s_low = f"{s.text} {s.symptoms or ''}".lower()
            for kw in RED_FLAG_KEYWORDS:
                if kw in s_low and kw not in symptoms_found:
                    symptoms_found.append(kw)

        user_facts = Mode2UserFacts(
            workout_context=episode.workout_context.value,
            user_statements=user_stmts,
            confirmed_symptoms=symptoms_found,
        )

        # 3. Model interpretations & context
        changes: List[str] = []
        for ev in episode.athena_events:
            changes.append(f"[{ev.event_type}] {ev.prompt_summary}")
        if episode.status == Mode2EpisodeStatus.CLOSED and episode.closed_at_utc:
            changes.append(f"[closed] Завершение эпизода (нормализация ЧСС: {episode.latest_pulse} уд/мин)")

        model_ctx = Mode2ModelContext(
            athena_events_count=len(episode.athena_events),
            significant_changes=changes,
            is_normalized=(episode.status == Mode2EpisodeStatus.CLOSED),
            model_notes="Эпизод завершён со стойкой нормализацией" if episode.status == Mode2EpisodeStatus.CLOSED else "Эпизод активен",
        )

        return Mode2EpisodeWellnessSummary(
            episode_id=episode.episode_id,
            started_at_utc=episode.started_at_utc,
            closed_at_utc=episode.closed_at_utc,
            event_type=episode.trigger_reason,
            measurement_source=episode.trigger_source,
            objective_metrics=obj_metrics,
            user_facts=user_facts,
            model_context=model_ctx,
        )

    def get_wellness_episodes_for_window(
        self,
        cycle_id: str,
        start_dt: datetime,
        end_dt: datetime,
    ) -> List[Any]:
        """
        Retrieves Mode 2 episodes that occurred or were active during [start_dt, end_dt).
        Transforms each episode into Mode2EpisodeWellnessSummary for Wellness Daily analysis.
        """
        norm_start = start_dt if start_dt.tzinfo is not None else start_dt.replace(tzinfo=timezone.utc)
        norm_end = end_dt if end_dt.tzinfo is not None else end_dt.replace(tzinfo=timezone.utc)

        all_episodes = self.get_episode_history(cycle_id)
        matching_summaries: List[Any] = []

        for ep in all_episodes:
            try:
                ep_start = datetime.fromisoformat(ep.started_at_utc)
                if ep_start.tzinfo is None:
                    ep_start = ep_start.replace(tzinfo=timezone.utc)
                ep_closed = None
                if ep.closed_at_utc:
                    ep_closed = datetime.fromisoformat(ep.closed_at_utc)
                    if ep_closed.tzinfo is None:
                        ep_closed = ep_closed.replace(tzinfo=timezone.utc)
            except Exception:
                continue

            # Overlap check: started on/before end_dt AND (closed_dt is None or closed_dt >= start_dt)
            if ep_start <= norm_end and (ep_closed is None or ep_closed >= norm_start):
                matching_summaries.append(self.build_wellness_summary(ep))

        matching_summaries.sort(key=lambda s: s.started_at_utc)
        return matching_summaries
