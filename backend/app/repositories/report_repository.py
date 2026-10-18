import uuid
from datetime import datetime, timezone
from typing import List, Optional, Dict, Any
from sqlalchemy.orm import Session
from sqlalchemy import select, func
from app.models.model_report import ModelReport
from app.schemas.privacy import PrivacyLevel
from app.schemas.reports import ClientReportContent


class ModelReportRepository:
    def __init__(self, db: Session):
        self.db = db

    @staticmethod
    def _validate_privacy_level(privacy_level: PrivacyLevel | str) -> str:
        if isinstance(privacy_level, str):
            return PrivacyLevel.from_str(privacy_level).value
        if isinstance(privacy_level, PrivacyLevel):
            return privacy_level.value
        raise ValueError(f"Invalid privacy level type: {type(privacy_level)}")

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
        validated_privacy = self._validate_privacy_level(privacy_level)

        if version < 1:
            raise ValueError(f"Report version must be >= 1, got {version}")

        # Enforce strict allowlist on client_facing report content
        if validated_privacy == PrivacyLevel.CLIENT_FACING.value:
            ClientReportContent.model_validate(content)

        # Protect against report_id collision and silent overwrite
        existing = self.get(report_id)
        if existing is not None:
            raise ValueError(
                f"Report ID collision: report '{report_id}' already exists. "
                "Existing reports are immutable and cannot be overwritten."
            )

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

    def get_latest_version_number(
        self,
        cycle_id: str,
        report_type: str,
        privacy_level: Optional[PrivacyLevel | str] = None,
    ) -> int:
        """Returns the highest version number for the given cycle and report_type (0 if none)."""
        stmt = select(func.max(ModelReport.version)).where(
            ModelReport.cycle_id == cycle_id,
            ModelReport.report_type == report_type,
        )
        if privacy_level is not None:
            validated_privacy = self._validate_privacy_level(privacy_level)
            stmt = stmt.where(ModelReport.privacy_level == validated_privacy)

        max_ver = self.db.execute(stmt).scalar_one_or_none()
        return int(max_ver) if max_ver is not None else 0

    def create_next_version(
        self,
        cycle_id: str,
        model: str,
        report_type: str,
        privacy_level: PrivacyLevel | str,
        content: Dict[str, Any],
        report_id: Optional[str] = None,
        sprint_id: Optional[str] = None,
        session_id: Optional[str] = None,
        created_at: Optional[datetime] = None,
    ) -> ModelReport:
        """
        Safely creates the next sequential version (v1, v2, v3...) of a report
        without overwriting previous versions and with automatic report_id collision protection.
        """
        validated_privacy = self._validate_privacy_level(privacy_level)
        next_version = self.get_latest_version_number(
            cycle_id=cycle_id,
            report_type=report_type,
            privacy_level=validated_privacy,
        ) + 1

        if report_id is not None:
            if self.get(report_id) is not None:
                raise ValueError(
                    f"Report ID collision: report '{report_id}' already exists. "
                    "Cannot overwrite existing report version."
                )
            candidate_id = report_id
        else:
            base_id = f"rep_{cycle_id}_{report_type}_v{next_version}"
            if len(base_id) <= 55 and self.get(base_id) is None:
                candidate_id = base_id
            else:
                suffix = uuid.uuid4().hex[:8]
                prefix = f"rep_{report_type[:20]}_v{next_version}_{suffix}"
                candidate_id = prefix[:64]
                while self.get(candidate_id) is not None:
                    candidate_id = f"rep_v{next_version}_{uuid.uuid4().hex[:12]}"[:64]

        return self.create(
            report_id=candidate_id,
            cycle_id=cycle_id,
            model=model,
            report_type=report_type,
            privacy_level=validated_privacy,
            content=content,
            version=next_version,
            sprint_id=sprint_id,
            session_id=session_id,
            created_at=created_at,
        )

    def list_versions(
        self,
        cycle_id: str,
        report_type: str,
        privacy_level: Optional[PrivacyLevel | str] = None,
    ) -> List[ModelReport]:
        """Returns all historical versions of a report ordered from v1 to latest."""
        stmt = select(ModelReport).where(
            ModelReport.cycle_id == cycle_id,
            ModelReport.report_type == report_type,
        )
        if privacy_level is not None:
            validated_privacy = self._validate_privacy_level(privacy_level)
            stmt = stmt.where(ModelReport.privacy_level == validated_privacy)
        stmt = stmt.order_by(ModelReport.version.asc(), ModelReport.created_at.asc())
        return list(self.db.execute(stmt).scalars().all())

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
        stmt = stmt.order_by(ModelReport.version.desc(), ModelReport.created_at.desc())
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
        stmt = stmt.order_by(ModelReport.version.desc(), ModelReport.created_at.desc())
        return list(self.db.execute(stmt).scalars().all())
