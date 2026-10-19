import hashlib
import time
import uuid
from datetime import datetime, timezone
from typing import List, Optional, Dict, Any
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session
from sqlalchemy import select, func
from app.models.model_report import ModelReport
from app.schemas.privacy import PrivacyLevel
from app.schemas.reports import ClientReportContent


class ReportVersionConflictError(ValueError):
    """
    Raised when creating a ModelReport fails due to a version uniqueness conflict
    on (cycle_id, report_type, privacy_level, version), a report_id collision,
    or database lock contention during version persistence.
    Subclasses ValueError for backward compatibility with existing callers.
    """


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

    @staticmethod
    def build_deterministic_report_id(
        cycle_id: str,
        report_type: str,
        privacy_level: PrivacyLevel | str,
        version: int,
        force_hash: bool = False,
    ) -> str:
        """
        Builds a deterministic report_id (max 64 chars) for a given
        (cycle_id, report_type, privacy_level, version).
        Never uses random UUIDs for versioned reports so long cycle_ids (UUIDs)
        cannot bypass primary key collision checks.
        """
        validated_privacy = ModelReportRepository._validate_privacy_level(privacy_level)
        base_id = f"rep_{cycle_id}_{report_type}_v{version}"
        if not force_hash and len(base_id) <= 64:
            return base_id
        digest = hashlib.sha256(
            f"{cycle_id}|{report_type}|{validated_privacy}|v{version}".encode("utf-8")
        ).hexdigest()[:16]
        return f"rep_{report_type[:24]}_{digest}_v{version}"[:64]

    def _resolve_candidate_report_id(
        self,
        cycle_id: str,
        report_type: str,
        validated_privacy: str,
        version: int,
    ) -> str:
        base_id = f"rep_{cycle_id}_{report_type}_v{version}"
        if len(base_id) <= 64:
            existing = self.get(base_id)
            if existing is None or (
                existing.cycle_id == cycle_id
                and existing.report_type == report_type
                and existing.privacy_level == validated_privacy
                and existing.version == version
            ):
                return base_id
        return self.build_deterministic_report_id(
            cycle_id=cycle_id,
            report_type=report_type,
            privacy_level=validated_privacy,
            version=version,
            force_hash=True,
        )

    def get(self, report_id: str) -> Optional[ModelReport]:
        """Internal server fetch of any report by ID."""
        return self.db.execute(
            select(ModelReport).where(ModelReport.report_id == report_id)
        ).scalar_one_or_none()

    def get_by_composite_version(
        self,
        cycle_id: str,
        report_type: str,
        privacy_level: PrivacyLevel | str,
        version: int,
    ) -> Optional[ModelReport]:
        """Internal server fetch of a report by (cycle_id, report_type, privacy_level, version)."""
        validated_privacy = self._validate_privacy_level(privacy_level)
        return self.db.execute(
            select(ModelReport).where(
                ModelReport.cycle_id == cycle_id,
                ModelReport.report_type == report_type,
                ModelReport.privacy_level == validated_privacy,
                ModelReport.version == version,
            )
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
        commit: bool = True,
    ) -> ModelReport:
        validated_privacy = self._validate_privacy_level(privacy_level)

        if version < 1:
            raise ValueError(f"Report version must be >= 1, got {version}")

        # Enforce strict allowlist on client_facing report content
        if validated_privacy == PrivacyLevel.CLIENT_FACING.value:
            ClientReportContent.model_validate(content)

        try:
            # Protect against report_id collision and silent overwrite
            existing = self.get(report_id)
            if existing is not None:
                raise ReportVersionConflictError(
                    f"Report ID collision: report '{report_id}' already exists. "
                    "Existing reports are immutable and cannot be overwritten."
                )

            # Protect against composite (cycle_id, report_type, privacy_level, version) collision
            existing_version = self.get_by_composite_version(
                cycle_id=cycle_id,
                report_type=report_type,
                privacy_level=validated_privacy,
                version=version,
            )
            if existing_version is not None:
                raise ReportVersionConflictError(
                    f"Report version conflict: report for cycle_id='{cycle_id}', "
                    f"report_type='{report_type}', privacy_level='{validated_privacy}', "
                    f"version={version} already exists (report_id='{existing_version.report_id}'). "
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
            if commit:
                self.db.commit()
                self.db.refresh(report)
            else:
                self.db.flush()
            return report
        except ReportVersionConflictError:
            if commit:
                self.db.rollback()
            raise
        except IntegrityError as exc:
            if commit:
                self.db.rollback()
            raise ReportVersionConflictError(
                f"Report version or ID conflict while saving report '{report_id}' "
                f"(cycle_id='{cycle_id}', report_type='{report_type}', "
                f"privacy_level='{validated_privacy}', version={version}): {exc}"
            ) from exc
        except OperationalError as exc:
            if commit:
                self.db.rollback()
            raise ReportVersionConflictError(
                f"Database operational error while saving report '{report_id}' "
                f"(cycle_id='{cycle_id}', report_type='{report_type}', "
                f"privacy_level='{validated_privacy}', version={version}): {exc}"
            ) from exc
        except Exception:
            if commit:
                self.db.rollback()
            raise

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
        commit: bool = True,
        expected_version: Optional[int] = None,
        max_retries: int = 3,
    ) -> ModelReport:
        """
        Safely creates the next sequential version (v1, v2, v3...) of a report
        without overwriting previous versions and with DB-level composite version
        and deterministic report_id collision protection.
        - Never auto-commits when commit=False (caller manages outer transaction).
        - When commit=True and expected_version is None and report_id is None, uses
          bounded retries on concurrent version collision to assign the next version cleanly.
        """
        validated_privacy = self._validate_privacy_level(privacy_level)

        if report_id is not None and self.get(report_id) is not None:
            raise ReportVersionConflictError(
                f"Report ID collision: report '{report_id}' already exists. "
                "Cannot overwrite existing report version."
            )

        effective_retries = (
            max(0, int(max_retries))
            if (commit and expected_version is None and report_id is None)
            else 0
        )

        for attempt in range(effective_retries + 1):
            if attempt > 0:
                self.db.rollback()
                self.db.expire_all()
                time.sleep(0.01 * attempt)

            try:
                latest_version = self.get_latest_version_number(
                    cycle_id=cycle_id,
                    report_type=report_type,
                    privacy_level=validated_privacy,
                )
                next_version = latest_version + 1

                if expected_version is not None and next_version != expected_version:
                    raise ReportVersionConflictError(
                        f"Report version conflict for cycle_id='{cycle_id}', "
                        f"report_type='{report_type}', privacy_level='{validated_privacy}': "
                        f"expected next version {expected_version}, but current next version is {next_version}."
                    )

                if report_id is not None:
                    candidate_id = report_id
                else:
                    candidate_id = self._resolve_candidate_report_id(
                        cycle_id=cycle_id,
                        report_type=report_type,
                        validated_privacy=validated_privacy,
                        version=next_version,
                    )

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
                    commit=commit,
                )
            except ReportVersionConflictError:
                if attempt < effective_retries:
                    continue
                raise
            except OperationalError as exc:
                if commit:
                    self.db.rollback()
                if attempt < effective_retries:
                    continue
                raise ReportVersionConflictError(
                    f"Database operational error while creating next report version "
                    f"for cycle_id='{cycle_id}', report_type='{report_type}': {exc}"
                ) from exc

        raise ReportVersionConflictError(
            f"Exhausted retries ({effective_retries}) creating next version for "
            f"cycle_id='{cycle_id}', report_type='{report_type}'."
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
