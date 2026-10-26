from datetime import datetime, timezone
from sqlalchemy import String, Integer, DateTime
from sqlalchemy.orm import Mapped, mapped_column
from app.models.base import Base


class AuthRateLimit(Base):
    """
    Persistent rate-limiting bucket for protecting authentication and device pairing
    against brute-force, credential stuffing, and distributed enumeration attacks.
    Persisted in SQLite so rate limits and lockouts survive backend restarts and
    serialize atomically across concurrent HTTP requests.
    """
    __tablename__ = "auth_rate_limits"

    bucket_key: Mapped[str] = mapped_column(
        String(128),
        primary_key=True,
        index=True,
    )
    scope: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        index=True,
    )  # global, ip, device
    failures_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
    )
    window_started_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    locked_until: Mapped[datetime | None] = mapped_column(
        DateTime,
        nullable=True,
        index=True,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        index=True,
    )
