"""Add auth_rate_limits table for persistent pairing and authentication rate limiting

Revision ID: 0004_auth_rate_limits
Revises: 0003_identity_and_device_pairing
Create Date: 2026-10-25 22:55:00.000000

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = '0004_auth_rate_limits'
down_revision: Union[str, None] = '0003_identity_and_device_pairing'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'auth_rate_limits',
        sa.Column('bucket_key', sa.String(length=128), primary_key=True),
        sa.Column('scope', sa.String(length=32), nullable=False),
        sa.Column('failures_count', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('window_started_at', sa.DateTime(), nullable=False),
        sa.Column('locked_until', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
    )
    op.create_index('ix_auth_rate_limits_bucket_key', 'auth_rate_limits', ['bucket_key'], unique=False)
    op.create_index('ix_auth_rate_limits_scope', 'auth_rate_limits', ['scope'], unique=False)
    op.create_index('ix_auth_rate_limits_locked_until', 'auth_rate_limits', ['locked_until'], unique=False)
    op.create_index('ix_auth_rate_limits_updated_at', 'auth_rate_limits', ['updated_at'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_auth_rate_limits_updated_at', table_name='auth_rate_limits')
    op.drop_index('ix_auth_rate_limits_locked_until', table_name='auth_rate_limits')
    op.drop_index('ix_auth_rate_limits_scope', table_name='auth_rate_limits')
    op.drop_index('ix_auth_rate_limits_bucket_key', table_name='auth_rate_limits')
    op.drop_table('auth_rate_limits')
