from datetime import datetime, timezone
from sqlalchemy import Boolean, DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from app.models.base import Base


class ClinicalAIAuditReview(Base):
    """
    Persistent server-side medical quality review record for a specific AI model inference.
    Represents human clinical evaluation (NOT automatic certification).
    """
    __tablename__ = "clinical_ai_audit_reviews"

    review_id: Mapped[str] = mapped_column(String(64), primary_key=True, index=True)
    call_id: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    patient_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    cycle_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    sprint_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    stage: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # Status: NOT_REVIEWED, REVIEWED_PASS, REVIEWED_FAIL, NEEDS_CLINICAL_REVIEW
    status: Mapped[str] = mapped_column(String(32), default="NOT_REVIEWED", index=True)
    reviewer_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Human clinical criteria checklist
    facts_accurate: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    units_correct: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    unjustified_conclusions_found: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    cross_agent_consistent: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    red_flags_identified: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    strategy_aligns_with_inputs: Mapped[bool | None] = mapped_column(Boolean, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )
