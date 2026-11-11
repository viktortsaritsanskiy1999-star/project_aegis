"""Add clinical_ai_audit_reviews table for manual clinical quality audit

Revision ID: 0005_clinical_ai_audit_reviews
Revises: 0004_auth_rate_limits
Create Date: 2026-11-10 20:45:00.000000

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = '0005_clinical_ai_audit_reviews'
down_revision: Union[str, None] = '0004_auth_rate_limits'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'clinical_ai_audit_reviews',
        sa.Column('review_id', sa.String(length=64), primary_key=True),
        sa.Column('call_id', sa.String(length=64), nullable=False),
        sa.Column('patient_id', sa.String(length=64), nullable=False),
        sa.Column('cycle_id', sa.String(length=64), nullable=False),
        sa.Column('sprint_id', sa.String(length=64), nullable=True),
        sa.Column('stage', sa.String(length=64), nullable=True),
        sa.Column('status', sa.String(length=32), nullable=False, server_default='NOT_REVIEWED'),
        sa.Column('reviewer_id', sa.String(length=64), nullable=True),
        sa.Column('reviewed_at', sa.DateTime(), nullable=True),
        sa.Column('notes', sa.Text(), nullable=True),
        sa.Column('facts_accurate', sa.Boolean(), nullable=True),
        sa.Column('units_correct', sa.Boolean(), nullable=True),
        sa.Column('unjustified_conclusions_found', sa.Boolean(), nullable=True),
        sa.Column('cross_agent_consistent', sa.Boolean(), nullable=True),
        sa.Column('red_flags_identified', sa.Boolean(), nullable=True),
        sa.Column('strategy_aligns_with_inputs', sa.Boolean(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
    )
    op.create_index('ix_clinical_ai_audit_reviews_review_id', 'clinical_ai_audit_reviews', ['review_id'], unique=False)
    op.create_index('ix_clinical_ai_audit_reviews_call_id', 'clinical_ai_audit_reviews', ['call_id'], unique=True)
    op.create_index('ix_clinical_ai_audit_reviews_patient_id', 'clinical_ai_audit_reviews', ['patient_id'], unique=False)
    op.create_index('ix_clinical_ai_audit_reviews_cycle_id', 'clinical_ai_audit_reviews', ['cycle_id'], unique=False)
    op.create_index('ix_clinical_ai_audit_reviews_sprint_id', 'clinical_ai_audit_reviews', ['sprint_id'], unique=False)
    op.create_index('ix_clinical_ai_audit_reviews_status', 'clinical_ai_audit_reviews', ['status'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_clinical_ai_audit_reviews_status', table_name='clinical_ai_audit_reviews')
    op.drop_index('ix_clinical_ai_audit_reviews_sprint_id', table_name='clinical_ai_audit_reviews')
    op.drop_index('ix_clinical_ai_audit_reviews_cycle_id', table_name='clinical_ai_audit_reviews')
    op.drop_index('ix_clinical_ai_audit_reviews_patient_id', table_name='clinical_ai_audit_reviews')
    op.drop_index('ix_clinical_ai_audit_reviews_call_id', table_name='clinical_ai_audit_reviews')
    op.drop_index('ix_clinical_ai_audit_reviews_review_id', table_name='clinical_ai_audit_reviews')
    op.drop_table('clinical_ai_audit_reviews')
