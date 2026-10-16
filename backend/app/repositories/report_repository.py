from datetime import datetime, timezone
from typing import List, Optional, Dict, Any
from sqlalchemy.orm import Session
from sqlalchemy import select
from app.models.model_report import ModelReport
from app.schemas.privacy import PrivacyLevel


class ModelReportRepository:
    def __init__(self, db: Session):
        self.db = db

    def get(self, report_id: str) -> Optional[ModelReport]:
        """Internal server fetch of any report by ID."""
        return self.db.execute(
            select(ModelReport).where(ModelReport.report_id == report_id)
        ).scalar_one_or_none()

    def get_client_facing(self, report_id: str) -> Optional[ModelReport]:
        """
        Safe fetch for client delivery.
        Strictly forbids returning server_only reports to the client layer.
        """
        report = self.get(report_id)
        if not report:
            return None

        if report.privacy_level != PrivacyLevel.CLIENT_FACING.value:
            raise PermissionError(
                f"Security Violation: Report '{report_id}' has privacy_level='{report.privacy_level}' "
                "and is restricted to server-only access."
            )
        return report

    def create(
        self,
        report_id: str,
        cycle_id: str,
        model: str,
        report_type: str,
        privacy_level: PrivacyLevel | str,
        content: Dict[str, Any],
        version: int = 1,
        sprint_id: Optional[str] = None,
        session_id: Optional[str] = None,
        created_at: Optional[datetime] = None,
    ) -> ModelReport:
        # Validate privacy level strictly; reject unknown values
        if isinstance(privacy_level, str):
            validated_privacy = PrivacyLevel.from_str(privacy_level).value
        elif isinstance(privacy_level, PrivacyLevel):
            validated_privacy = privacy_level.value
        else:
            raise ValueError(f"Invalid privacy level type: {type(privacy_level)}")

        existing = self.get(report_id)
        if existing:
            return existing

        now = created_at or datetime.now(timezone.utc)
        report = ModelReport(
            report_id=report_id,
            cycle_id=cycle_id,
            sprint_id=sprint_id,
            session_id=session_id,
            model=model,
            report_type=report_type,
            privacy_level=validated_privacy,
            version=version,
            content=content,
            created_at=now,
        )
        self.db.add(report)
        self.db.commit()
        self.db.refresh(report)
        return report

    def list_client_facing(
        self,
        cycle_id: str,
        report_type: Optional[str] = None,
    ) -> List[ModelReport]:
        """Returns only reports approved for client delivery."""
        stmt = select(ModelReport).where(
            ModelReport.cycle_id == cycle_id,
            ModelReport.privacy_level == PrivacyLevel.CLIENT_FACING.value,
        )
        if report_type:
            stmt = stmt.where(ModelReport.report_type == report_type)
        stmt = stmt.order_by(ModelReport.created_at.desc())
        return list(self.db.execute(stmt).scalars().all())

    def list_server_only(
        self,
        cycle_id: str,
        report_type: Optional[str] = None,
    ) -> List[ModelReport]:
        """Internal server-only query."""
        stmt = select(ModelReport).where(
            ModelReport.cycle_id == cycle_id,
            ModelReport.privacy_level == PrivacyLevel.SERVER_ONLY.value,
        )
        if report_type:
            stmt = stmt.where(ModelReport.report_type == report_type)
        stmt = stmt.order_by(ModelReport.created_at.desc())
        return list(self.db.execute(stmt).scalars().all())
