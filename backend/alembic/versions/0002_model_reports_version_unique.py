"""Add unique constraint on model_reports (cycle_id, report_type, privacy_level, version)

Revision ID: 0002_model_reports_version_unique
Revises: 0001_initial_schema
Create Date: 2026-10-19 17:00:00.000000

"""
from typing import Sequence, Union
from alembic import op
from app.database import assert_no_duplicate_report_versions

revision: str = '0002_model_reports_version_unique'
down_revision: Union[str, None] = '0001_initial_schema'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Read-only safety check: abort migration without deleting/merging data if duplicates exist
    bind = op.get_bind()
    assert_no_duplicate_report_versions(bind)

    with op.batch_alter_table('model_reports', schema=None) as batch_op:
        batch_op.create_unique_constraint(
            'uq_model_reports_cycle_type_privacy_version',
            ['cycle_id', 'report_type', 'privacy_level', 'version'],
        )


def downgrade() -> None:
    with op.batch_alter_table('model_reports', schema=None) as batch_op:
        batch_op.drop_constraint(
            'uq_model_reports_cycle_type_privacy_version',
            type_='unique',
        )
