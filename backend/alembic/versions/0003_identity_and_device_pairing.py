"""Add patients, devices, pairing_codes, device_refresh_tokens and link medical_cycles to patients

Revision ID: 0003_identity_and_device_pairing
Revises: 0002_model_reports_version_unique
Create Date: 2026-10-23 23:00:00.000000

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = '0003_identity_and_device_pairing'
down_revision: Union[str, None] = '0002_model_reports_version_unique'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Create patients table
    op.create_table(
        'patients',
        sa.Column('patient_id', sa.String(length=64), primary_key=True),
        sa.Column('status', sa.String(length=32), nullable=False, server_default='active'),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
    )
    op.create_index('ix_patients_patient_id', 'patients', ['patient_id'], unique=False)
    op.create_index('ix_patients_status', 'patients', ['status'], unique=False)

    # 2. Add patient_id to medical_cycles (nullable=True for backward compatibility with legacy unassigned cycles)
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        op.execute(
            "ALTER TABLE medical_cycles ADD COLUMN patient_id VARCHAR(64) "
            "REFERENCES patients(patient_id) ON DELETE RESTRICT"
        )
        op.create_index('ix_medical_cycles_patient_id', 'medical_cycles', ['patient_id'], unique=False)
    else:
        with op.batch_alter_table('medical_cycles', schema=None) as batch_op:
            batch_op.add_column(sa.Column('patient_id', sa.String(length=64), nullable=True))
            batch_op.create_foreign_key(
                'fk_medical_cycles_patient_id',
                'patients',
                ['patient_id'],
                ['patient_id'],
                ondelete='RESTRICT',
            )
            batch_op.create_index('ix_medical_cycles_patient_id', ['patient_id'], unique=False)

    # 3. Create devices table
    op.create_table(
        'devices',
        sa.Column('device_id', sa.String(length=64), primary_key=True),
        sa.Column('patient_id', sa.String(length=64), sa.ForeignKey('patients.patient_id', ondelete='CASCADE'), nullable=False),
        sa.Column('device_name', sa.String(length=128), nullable=False),
        sa.Column('platform', sa.String(length=32), nullable=False, server_default='android'),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default=sa.text('1')),
        sa.Column('paired_at', sa.DateTime(), nullable=False),
        sa.Column('revoked_at', sa.DateTime(), nullable=True),
        sa.Column('last_seen_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
    )
    op.create_index('ix_devices_device_id', 'devices', ['device_id'], unique=False)
    op.create_index('ix_devices_patient_id', 'devices', ['patient_id'], unique=False)
    op.create_index('ix_devices_is_active', 'devices', ['is_active'], unique=False)

    # 4. Create pairing_codes table
    op.create_table(
        'pairing_codes',
        sa.Column('code_id', sa.String(length=64), primary_key=True),
        sa.Column('patient_id', sa.String(length=64), sa.ForeignKey('patients.patient_id', ondelete='CASCADE'), nullable=False),
        sa.Column('code_hash', sa.String(length=128), nullable=False),
        sa.Column('active_code_hash', sa.String(length=128), nullable=True),
        sa.Column('expires_at', sa.DateTime(), nullable=False),
        sa.Column('consumed_at', sa.DateTime(), nullable=True),
        sa.Column('attempts_count', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('max_attempts', sa.Integer(), nullable=False, server_default='5'),
        sa.Column('created_at', sa.DateTime(), nullable=False),
    )
    op.create_index('ix_pairing_codes_code_id', 'pairing_codes', ['code_id'], unique=False)
    op.create_index('ix_pairing_codes_patient_id', 'pairing_codes', ['patient_id'], unique=False)
    op.create_index('ix_pairing_codes_code_hash', 'pairing_codes', ['code_hash'], unique=False)
    op.create_index('ix_pairing_codes_active_code_hash', 'pairing_codes', ['active_code_hash'], unique=True)

    # 5. Create device_refresh_tokens table
    op.create_table(
        'device_refresh_tokens',
        sa.Column('token_id', sa.String(length=64), primary_key=True),
        sa.Column('device_id', sa.String(length=64), sa.ForeignKey('devices.device_id', ondelete='CASCADE'), nullable=False),
        sa.Column('token_hash', sa.String(length=128), nullable=False),
        sa.Column('family_id', sa.String(length=64), nullable=False),
        sa.Column('replaced_by_token_id', sa.String(length=64), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('expires_at', sa.DateTime(), nullable=False),
        sa.Column('revoked_at', sa.DateTime(), nullable=True),
    )
    op.create_index('ix_device_refresh_tokens_token_id', 'device_refresh_tokens', ['token_id'], unique=False)
    op.create_index('ix_device_refresh_tokens_device_id', 'device_refresh_tokens', ['device_id'], unique=False)
    op.create_index('ix_device_refresh_tokens_family_id', 'device_refresh_tokens', ['family_id'], unique=False)
    op.create_index('ix_device_refresh_tokens_token_hash', 'device_refresh_tokens', ['token_hash'], unique=True)


def downgrade() -> None:
    op.drop_table('device_refresh_tokens')
    op.drop_table('pairing_codes')
    op.drop_table('devices')
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        op.drop_index('ix_medical_cycles_patient_id', table_name='medical_cycles')
        op.execute("ALTER TABLE medical_cycles DROP COLUMN patient_id")
    else:
        with op.batch_alter_table('medical_cycles', schema=None) as batch_op:
            batch_op.drop_constraint('fk_medical_cycles_patient_id', type_='foreignkey')
            batch_op.drop_index('ix_medical_cycles_patient_id')
            batch_op.drop_column('patient_id')
    op.drop_table('patients')
