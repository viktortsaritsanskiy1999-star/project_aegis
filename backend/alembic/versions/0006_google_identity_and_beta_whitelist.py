"""Add patient_google_identities and beta_access_whitelist tables

Revision ID: 0006_google_identity_and_beta_whitelist
Revises: 0005_clinical_ai_audit_reviews
Create Date: 2026-11-11 09:00:00.000000

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = '0006_google_identity_and_beta_whitelist'
down_revision: Union[str, None] = '0005_clinical_ai_audit_reviews'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'patient_google_identities',
        sa.Column('id', sa.String(length=64), primary_key=True),
        sa.Column('google_sub', sa.String(length=255), nullable=False),
        sa.Column('google_issuer', sa.String(length=255), nullable=False, server_default='https://accounts.google.com'),
        sa.Column('patient_id', sa.String(length=64), nullable=False),
        sa.Column('email', sa.String(length=255), nullable=True),
        sa.Column('email_verified', sa.Boolean(), nullable=False, server_default='0'),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('last_authenticated_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['patient_id'], ['patients.patient_id'], ondelete='CASCADE'),
    )
    op.create_index('ix_patient_google_identities_id', 'patient_google_identities', ['id'], unique=False)
    op.create_index('ix_patient_google_identities_google_sub', 'patient_google_identities', ['google_sub'], unique=True)
    op.create_index('ix_patient_google_identities_patient_id', 'patient_google_identities', ['patient_id'], unique=True)
    op.create_index('ix_patient_google_identities_email', 'patient_google_identities', ['email'], unique=False)

    op.create_table(
        'beta_access_whitelist',
        sa.Column('id', sa.String(length=64), primary_key=True),
        sa.Column('identifier_type', sa.String(length=32), nullable=False),
        sa.Column('identifier_value', sa.String(length=255), nullable=False),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default='1'),
        sa.Column('granted_by', sa.String(length=64), nullable=False, server_default='admin'),
        sa.Column('notes', sa.String(length=255), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('revoked_at', sa.DateTime(), nullable=True),
    )
    op.create_index('ix_beta_access_whitelist_id', 'beta_access_whitelist', ['id'], unique=False)
    op.create_index('ix_beta_access_whitelist_identifier_value', 'beta_access_whitelist', ['identifier_value'], unique=False)
    op.create_index('ix_beta_access_whitelist_is_active', 'beta_access_whitelist', ['is_active'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_beta_access_whitelist_is_active', table_name='beta_access_whitelist')
    op.drop_index('ix_beta_access_whitelist_identifier_value', table_name='beta_access_whitelist')
    op.drop_index('ix_beta_access_whitelist_id', table_name='beta_access_whitelist')
    op.drop_table('beta_access_whitelist')

    op.drop_index('ix_patient_google_identities_email', table_name='patient_google_identities')
    op.drop_index('ix_patient_google_identities_patient_id', table_name='patient_google_identities')
    op.drop_index('ix_patient_google_identities_google_sub', table_name='patient_google_identities')
    op.drop_index('ix_patient_google_identities_id', table_name='patient_google_identities')
    op.drop_table('patient_google_identities')
