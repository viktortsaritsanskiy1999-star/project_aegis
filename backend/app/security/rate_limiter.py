from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
import ipaddress
import math
import threading
import time
from typing import Callable, List, Mapping, Optional, Union
from sqlalchemy import delete, select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError, SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker
from app.config import settings
from app.models.auth_rate_limit import AuthRateLimit


class RateLimitError(RuntimeError):
    """Base error for rate limiting."""


class RateLimiterUnavailableError(RateLimitError):
    """Raised when the persistent rate-limit storage is unavailable or corrupted (fail-closed)."""


class RateLimitExceededError(RateLimitError):
    """Raised when an IP, device, or global failure budget is exceeded."""

    def __init__(
        self,
        message: str,
        *,
        scope: str,
        bucket_key: str,
        retry_after: int,
        locked_until: datetime,
    ) -> None:
        super().__init__(message)
        self.scope = scope
        self.bucket_key = bucket_key
        self.retry_after = max(1, int(retry_after))
        self.locked_until = locked_until


@dataclass(frozen=True)
class RateLimitConfig:
    """
    Unified conservative rate-limiting policy for Project Aegis local MVP:
    - Per-IP (`pair:ip:<ip>`): 5 failed attempts per 10-minute window (600s) -> 15-minute lockout (900s).
    - Per-Device (`pair:device:<id>`): 5 failed attempts per 10-minute window (600s) -> 15-minute lockout (900s).
      NOTE: Because `device_id` is client-supplied and untrusted prior to pairing, Per-Device is strictly an
      auxiliary defense and never substitutes for Per-IP or Global rate limiting.
    - Global Budget (`pair:global`): 25 failed attempts per 10-minute window (600s) -> 5-minute cooldown (300s).
    - Stale Bucket Retention: 24 hours (86400s).
    """
    ip_max_failures: int = 5
    ip_window_seconds: int = 600          # 10 minutes
    ip_lockout_seconds: int = 900         # 15 minutes

    device_max_failures: int = 5
    device_window_seconds: int = 600      # 10 minutes
    device_lockout_seconds: int = 900     # 15 minutes

    global_max_failures: int = 25
    global_window_seconds: int = 600      # 10 minutes
    global_lockout_seconds: int = 300     # 5 minutes

    stale_retention_seconds: int = 86400  # 24 hours


@dataclass(frozen=True)
class RateLimitAttemptTicket:
    """Ticket representing a reserved rate-limit attempt slot across IP, device, and global buckets."""
    client_ip: str
    device_id: Optional[str]
    ip_bucket_key: str
    device_bucket_key: Optional[str]
    global_bucket_key: str
    acquired_at: datetime


_PROCESS_RLOCK = threading.RLock()


def _ensure_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _parse_trusted_networks(raw_proxies: str) -> List[Union[ipaddress.IPv4Network, ipaddress.IPv6Network]]:
    networks: List[Union[ipaddress.IPv4Network, ipaddress.IPv6Network]] = []
    if not isinstance(raw_proxies, str) or not raw_proxies.strip():
        return networks
    for part in raw_proxies.split(","):
        token = part.strip()
        if not token:
            continue
        try:
            networks.append(ipaddress.ip_network(token, strict=False))
        except ValueError:
            continue
    return networks


def _is_ip_in_networks(
    ip_str: str,
    networks: List[Union[ipaddress.IPv4Network, ipaddress.IPv6Network]],
) -> bool:
    if not networks:
        return False
    try:
        addr = ipaddress.ip_address(ip_str)
    except ValueError:
        return False
    return any(addr in net for net in networks)


def resolve_client_ip(
    client_host: Optional[str],
    headers: Optional[Mapping[str, str]] = None,
    *,
    trusted_proxies: Optional[str] = None,
) -> str:
    """
    Determines the authoritative client IP address for rate limiting.
    - By default (`AEGIS_TRUSTED_PROXIES=""`), strictly ignores `X-Forwarded-For`,
      `X-Real-IP`, and all other client-supplied proxy headers to prevent IP spoofing.
    - Only inspects `X-Forwarded-For` when `client_host` belongs to an explicitly
      configured trusted proxy network, selecting the rightmost non-trusted valid IP.
    """
    raw_host = (client_host or "").strip()
    if not raw_host:
        return "unknown"

    effective_proxies = settings.trusted_proxy_ips if trusted_proxies is None else trusted_proxies
    trusted_nets = _parse_trusted_networks(effective_proxies)

    # Default secure path: do NOT trust proxy headers
    if not trusted_nets or not _is_ip_in_networks(raw_host, trusted_nets):
        return raw_host[:64]

    if not headers:
        return raw_host[:64]

    xff_value: Optional[str] = None
    for k, v in headers.items():
        if k.lower() == "x-forwarded-for" and isinstance(v, str):
            xff_value = v
            break

    if not xff_value or not xff_value.strip():
        return raw_host[:64]

    hops = [hop.strip() for hop in xff_value.split(",") if hop.strip()]
    if not hops:
        return raw_host[:64]

    # Walk from right to left (closest to our trusted proxy first)
    for candidate in reversed(hops):
        try:
            ipaddress.ip_address(candidate)
        except ValueError:
            # Malformed hop in XFF header: fall back to direct peer IP
            return raw_host[:64]
        if not _is_ip_in_networks(candidate, trusted_nets):
            return candidate[:64]

    return hops[0][:64]


class PersistentRateLimiter:
    """
    SQLite-persistent multi-bucket rate limiter for device pairing and authentication.

    SECURITY GUARANTEES:
    1. Multi-layer Buckets:
       - Per-IP (`pair:ip:<ip>`): stops single-origin brute force.
       - Per-Device (`pair:device:<device_id>`): auxiliary barrier against IP-rotating clients
         sending a fixed `device_id`.
       - Global (`pair:global`): caps aggregate failed attempts across all IPs and devices
         to defeat distributed botnet enumeration of the 6-digit pairing code space.
    2. Atomic Concurrency Serialization:
       - Acquires attempt slots atomically BEFORE code verification so parallel requests
         cannot bypass thresholds via TOCTOU races.
       - Checks Per-IP and Per-Device lockouts BEFORE incrementing the global bucket so a
         single banned IP cannot exhaust the global budget via repeated blocked requests.
       - Successful code verification resets the caller's IP and Device buckets and refunds
         at most the single reserved slot (`failures_count - 1`) on `pair:global`, ensuring
         a valid code can NEVER reset prior brute-force failures in the global bucket.
    3. Persistence & Fail-Closed:
       - Uses an isolated database session/transaction per limiter operation so rolling back
         a failed pairing transaction never rolls back recorded rate-limit failures.
       - Fails closed (`RateLimiterUnavailableError`) if the `auth_rate_limits` table is
         missing, unreachable, or corrupted.
    """

    def __init__(
        self,
        bind_or_session_factory: Union[Engine, sessionmaker, Session],
        config: Optional[RateLimitConfig] = None,
        now_provider: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self._config = config or RateLimitConfig()
        self._now_provider = now_provider or (lambda: datetime.now(timezone.utc))

        if isinstance(bind_or_session_factory, sessionmaker):
            self._session_factory: Optional[sessionmaker] = bind_or_session_factory
            self._shared_session: Optional[Session] = None
        elif isinstance(bind_or_session_factory, Engine):
            self._session_factory = sessionmaker(
                autocommit=False,
                autoflush=False,
                bind=bind_or_session_factory,
            )
            self._shared_session = None
        elif isinstance(bind_or_session_factory, Session):
            bind = bind_or_session_factory.get_bind()
            self._session_factory = sessionmaker(
                autocommit=False,
                autoflush=False,
                bind=bind,
            )
            self._shared_session = bind_or_session_factory
        else:
            raise RateLimiterUnavailableError("Invalid database bind provided to PersistentRateLimiter.")

    @property
    def config(self) -> RateLimitConfig:
        return self._config

    def verify_ready(self) -> None:
        """
        Probes the `auth_rate_limits` table in an isolated session and fails closed
        with `RateLimiterUnavailableError` if the table is missing or the SQLite
        database is unreachable/corrupted.
        """
        def _probe(session: Session) -> None:
            session.execute(select(AuthRateLimit.bucket_key).limit(1)).first()

        self._run_in_isolated_session(_probe)

    def _now(self) -> datetime:
        return _ensure_utc(self._now_provider())

    @staticmethod
    def _normalize_key_part(raw: Optional[str], fallback: str = "unknown") -> str:
        if not isinstance(raw, str):
            return fallback
        clean = raw.strip()
        if not clean:
            return fallback
        return clean[:64]

    def _run_in_isolated_session(self, fn: Callable[[Session], object]) -> object:
        if self._session_factory is None:
            raise RateLimiterUnavailableError("Rate limiter session factory is not initialized.")

        max_lock_retries = 5
        with _PROCESS_RLOCK:
            for attempt in range(max_lock_retries + 1):
                session: Session = self._session_factory()
                try:
                    result = fn(session)
                    if self._shared_session is not None:
                        self._shared_session.expire_all()
                    return result
                except RateLimitExceededError:
                    raise
                except OperationalError as exc:
                    session.rollback()
                    err_msg = str(exc).lower()
                    if attempt < max_lock_retries and ("locked" in err_msg or "busy" in err_msg):
                        time.sleep(0.015 * (attempt + 1))
                        continue
                    raise RateLimiterUnavailableError(
                        "Persistent rate limiter storage is unavailable due to database operational error."
                    ) from exc
                except SQLAlchemyError as exc:
                    session.rollback()
                    raise RateLimiterUnavailableError(
                        "Persistent rate limiter storage failed closed due to database error."
                    ) from exc
                except Exception as exc:
                    session.rollback()
                    if isinstance(exc, RateLimitError):
                        raise
                    raise RateLimiterUnavailableError(
                        "Persistent rate limiter encountered an unexpected storage error."
                    ) from exc
                finally:
                    session.close()

        raise RateLimiterUnavailableError("Persistent rate limiter exhausted database lock retries.")

    def _get_or_init_bucket(
        self,
        session: Session,
        bucket_key: str,
        scope: str,
        window_seconds: int,
        now: datetime,
    ) -> AuthRateLimit:
        bucket = session.execute(
            select(AuthRateLimit).where(AuthRateLimit.bucket_key == bucket_key)
        ).scalar_one_or_none()

        if bucket is None:
            bucket = AuthRateLimit(
                bucket_key=bucket_key,
                scope=scope,
                failures_count=0,
                window_started_at=now,
                locked_until=None,
                updated_at=now,
            )
            session.add(bucket)
            session.flush()
            return bucket

        locked_until = _ensure_utc(bucket.locked_until) if bucket.locked_until is not None else None
        window_start = _ensure_utc(bucket.window_started_at)

        # If a previous lockout has expired, reset the bucket for a fresh window
        if locked_until is not None and locked_until <= now:
            bucket.failures_count = 0
            bucket.window_started_at = now
            bucket.locked_until = None
            bucket.updated_at = now
            session.flush()
            return bucket

        # If not locked and the rolling window has elapsed, reset the window
        if locked_until is None and (window_start + timedelta(seconds=window_seconds)) <= now:
            bucket.failures_count = 0
            bucket.window_started_at = now
            bucket.updated_at = now
            session.flush()

        return bucket

    @staticmethod
    def _check_bucket_not_locked(bucket: AuthRateLimit, now: datetime) -> None:
        if bucket.locked_until is not None:
            locked_until = _ensure_utc(bucket.locked_until)
            if locked_until > now:
                remaining = math.ceil((locked_until - now).total_seconds())
                raise RateLimitExceededError(
                    f"Too many failed pairing attempts ({bucket.scope} limit). Please retry later.",
                    scope=bucket.scope,
                    bucket_key=bucket.bucket_key,
                    retry_after=max(1, remaining),
                    locked_until=locked_until,
                )

    def acquire_attempt(
        self,
        client_ip: str,
        device_id: Optional[str] = None,
        *,
        now: Optional[datetime] = None,
    ) -> RateLimitAttemptTicket:
        """
        Atomically checks rate limits and reserves an attempt slot across IP, Device, and Global buckets.
        - If any bucket is currently locked, commits any expired-window resets and raises
          RateLimitExceededError with exact `retry_after` seconds BEFORE incrementing active buckets.
        - Otherwise increments `failures_count` on all active buckets and locks any bucket
          that reaches its failure threshold (`failures_count >= max_failures`) so subsequent
          attempts are blocked until `locked_until`.
        """
        effective_now = _ensure_utc(now) if now is not None else self._now()
        clean_ip = self._normalize_key_part(client_ip, fallback="unknown")
        clean_dev = (
            self._normalize_key_part(device_id, fallback="")
            if device_id is not None and isinstance(device_id, str) and device_id.strip()
            else None
        )

        ip_key = f"pair:ip:{clean_ip}"
        dev_key = f"pair:device:{clean_dev}" if clean_dev else None
        global_key = "pair:global"

        def _tx(session: Session) -> RateLimitAttemptTicket:
            # 1. Opportunistically clean up expired unlocked records older than retention threshold
            cutoff = effective_now - timedelta(seconds=self._config.stale_retention_seconds)
            session.execute(
                delete(AuthRateLimit).where(
                    AuthRateLimit.scope != "global",
                    AuthRateLimit.updated_at <= cutoff,
                    (AuthRateLimit.locked_until.is_(None)) | (AuthRateLimit.locked_until <= effective_now),
                )
            )

            # 2. Load and normalize buckets
            ip_bucket = self._get_or_init_bucket(
                session, ip_key, "ip", self._config.ip_window_seconds, effective_now
            )
            dev_bucket = (
                self._get_or_init_bucket(
                    session, dev_key, "device", self._config.device_window_seconds, effective_now
                )
                if dev_key is not None
                else None
            )
            global_bucket = self._get_or_init_bucket(
                session, global_key, "global", self._config.global_window_seconds, effective_now
            )

            # 3. Check lockouts in strict order: IP -> Device -> Global.
            # Rejecting locked IP/Device first prevents a locked attacker from incrementing `pair:global`.
            try:
                self._check_bucket_not_locked(ip_bucket, effective_now)
                if dev_bucket is not None:
                    self._check_bucket_not_locked(dev_bucket, effective_now)
                self._check_bucket_not_locked(global_bucket, effective_now)
            except RateLimitExceededError:
                session.commit()
                raise

            # 4. Reserve attempt slot atomically on all buckets
            ip_bucket.failures_count += 1
            ip_bucket.updated_at = effective_now
            if ip_bucket.failures_count >= self._config.ip_max_failures:
                ip_bucket.locked_until = effective_now + timedelta(seconds=self._config.ip_lockout_seconds)

            if dev_bucket is not None:
                dev_bucket.failures_count += 1
                dev_bucket.updated_at = effective_now
                if dev_bucket.failures_count >= self._config.device_max_failures:
                    dev_bucket.locked_until = effective_now + timedelta(
                        seconds=self._config.device_lockout_seconds
                    )

            global_bucket.failures_count += 1
            global_bucket.updated_at = effective_now
            if global_bucket.failures_count >= self._config.global_max_failures:
                global_bucket.locked_until = effective_now + timedelta(
                    seconds=self._config.global_lockout_seconds
                )

            session.commit()
            return RateLimitAttemptTicket(
                client_ip=clean_ip,
                device_id=clean_dev,
                ip_bucket_key=ip_key,
                device_bucket_key=dev_key,
                global_bucket_key=global_key,
                acquired_at=effective_now,
            )

        res = self._run_in_isolated_session(_tx)
        assert isinstance(res, RateLimitAttemptTicket)
        return res

    def record_success(
        self,
        ticket: RateLimitAttemptTicket,
        *,
        now: Optional[datetime] = None,
    ) -> None:
        """
        Called ONLY when a pairing code is cryptographically verified and consumed.
        - Clears failure counters and lockouts on the caller's IP and Device buckets.
        - Refunds only the single slot (`failures_count - 1`) on `pair:global` without
          ever resetting prior global failures or clearing a global lockout.
        """
        effective_now = _ensure_utc(now) if now is not None else self._now()

        def _tx(session: Session) -> None:
            ip_bucket = session.execute(
                select(AuthRateLimit).where(AuthRateLimit.bucket_key == ticket.ip_bucket_key)
            ).scalar_one_or_none()
            if ip_bucket is not None:
                ip_bucket.failures_count = 0
                ip_bucket.locked_until = None
                ip_bucket.window_started_at = effective_now
                ip_bucket.updated_at = effective_now

            if ticket.device_bucket_key is not None:
                dev_bucket = session.execute(
                    select(AuthRateLimit).where(AuthRateLimit.bucket_key == ticket.device_bucket_key)
                ).scalar_one_or_none()
                if dev_bucket is not None:
                    dev_bucket.failures_count = 0
                    dev_bucket.locked_until = None
                    dev_bucket.window_started_at = effective_now
                    dev_bucket.updated_at = effective_now

            global_bucket = session.execute(
                select(AuthRateLimit).where(AuthRateLimit.bucket_key == ticket.global_bucket_key)
            ).scalar_one_or_none()
            if global_bucket is not None and global_bucket.failures_count > 0:
                global_bucket.failures_count = max(0, global_bucket.failures_count - 1)
                if global_bucket.failures_count < self._config.global_max_failures:
                    global_bucket.locked_until = None
                global_bucket.updated_at = effective_now

            session.commit()

        self._run_in_isolated_session(_tx)

    def cleanup_expired(self, *, now: Optional[datetime] = None) -> int:
        """Explicitly purges expired, unlocked rate-limit buckets older than `stale_retention_seconds`."""
        effective_now = _ensure_utc(now) if now is not None else self._now()
        cutoff = effective_now - timedelta(seconds=self._config.stale_retention_seconds)

        def _tx(session: Session) -> int:
            res = session.execute(
                delete(AuthRateLimit).where(
                    AuthRateLimit.updated_at <= cutoff,
                    (AuthRateLimit.locked_until.is_(None)) | (AuthRateLimit.locked_until <= effective_now),
                )
            )
            session.commit()
            return int(res.rowcount or 0)

        deleted = self._run_in_isolated_session(_tx)
        return int(deleted)  # type: ignore[arg-type]
