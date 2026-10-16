"""Initial Project Aegis schema

Revision ID: 0001_initial_schema
Revises: 
Create Date: 2026-10-16 12:00:00.000000

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = '0001_initial_schema'
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. medical_cycles
    op.create_table(
        'medical_cycles',
        sa.Column('cycle_id', sa.String(length=64), primary_key=True),
        sa.Column('status', sa.String(length=32), nullable=False, server_default='active'),
        sa.Column('started_at', sa.DateTime(), nullable=False),
        sa.Column('closed_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
    )
    op.create_index('ix_medical_cycles_cycle_id', 'medical_cycles', ['cycle_id'], unique=False)
    op.create_index('ix_medical_cycles_status', 'medical_cycles', ['status'], unique=False)

    # 2. weekly_sprints
    op.create_table(
        'weekly_sprints',
        sa.Column('sprint_id', sa.String(length=64), primary_key=True),
        sa.Column('cycle_id', sa.String(length=64), sa.ForeignKey('medical_cycles.cycle_id', ondelete='CASCADE'), nullable=False),
        sa.Column('sprint_number', sa.Integer(), nullable=False, server_default='1'),
        sa.Column('status', sa.String(length=32), nullable=False, server_default='active'),
        sa.Column('started_at', sa.DateTime(), nullable=False),
        sa.Column('completed_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
    )
    op.create_index('ix_weekly_sprints_sprint_id', 'weekly_sprints', ['sprint_id'], unique=False)
    op.create_index('ix_weekly_sprints_cycle_id', 'weekly_sprints', ['cycle_id'], unique=False)
    op.create_index('ix_weekly_sprints_status', 'weekly_sprints', ['status'], unique=False)

    # 3. workout_sessions
    op.create_table(
        'workout_sessions',
        sa.Column('session_id', sa.String(length=64), primary_key=True),
        sa.Column('cycle_id', sa.String(length=64), sa.ForeignKey('medical_cycles.cycle_id', ondelete='CASCADE'), nullable=False),
        sa.Column('sprint_id', sa.String(length=64), sa.ForeignKey('weekly_sprints.sprint_id', ondelete='SET NULL'), nullable=True),
        sa.Column('anomaly_detected_at', sa.DateTime(), nullable=False),
        sa.Column('activity_confirmed_at', sa.DateTime(), nullable=False),
        sa.Column('workout_completed_at', sa.DateTime(), nullable=True),
        sa.Column('physiological_end_time', sa.DateTime(), nullable=True),
        sa.Column('status', sa.String(length=32), nullable=False, server_default='active'),
        sa.Column('is_data_collection_finished', sa.Boolean(), nullable=False, server_default=sa.text('0')),
        sa.Column('created_at', sa.DateTime(), nullable=False),
    )
    op.create_index('ix_workout_sessions_session_id', 'workout_sessions', ['session_id'], unique=False)
    op.create_index('ix_workout_sessions_cycle_id', 'workout_sessions', ['cycle_id'], unique=False)
    op.create_index('ix_workout_sessions_sprint_id', 'workout_sessions', ['sprint_id'], unique=False)
    op.create_index('ix_workout_sessions_status', 'workout_sessions', ['status'], unique=False)

    # 4. health_measurements
    op.create_table(
        'health_measurements',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('session_id', sa.String(length=64), sa.ForeignKey('workout_sessions.session_id', ondelete='SET NULL'), nullable=True),
        sa.Column('cycle_id', sa.String(length=64), sa.ForeignKey('medical_cycles.cycle_id', ondelete='SET NULL'), nullable=True),
        sa.Column('pulse', sa.Integer(), nullable=True),
        sa.Column('systolic', sa.Integer(), nullable=True),
        sa.Column('diastolic', sa.Integer(), nullable=True),
        sa.Column('spo2', sa.Integer(), nullable=True),
        sa.Column('source', sa.String(length=32), nullable=False),
        sa.Column('is_simulated', sa.Boolean(), nullable=False, server_default=sa.text('0')),
        sa.Column('is_historical', sa.Boolean(), nullable=False, server_default=sa.text('0')),
        sa.Column('measured_at', sa.DateTime(), nullable=False),
        sa.Column('ingested_at', sa.DateTime(), nullable=False),
        sa.Column('phase', sa.String(length=64), nullable=False, server_default='standby'),
        sa.Column('note', sa.String(length=255), nullable=False, server_default=''),
        sa.Column('raw_metadata', sa.JSON(), nullable=True),
    )
    op.create_index('ix_health_measurements_session_id', 'health_measurements', ['session_id'], unique=False)
    op.create_index('ix_health_measurements_cycle_id', 'health_measurements', ['cycle_id'], unique=False)
    op.create_index('ix_health_measurements_source', 'health_measurements', ['source'], unique=False)
    op.create_index('ix_health_measurements_measured_at', 'health_measurements', ['measured_at'], unique=False)

    # 5. model_reports
    op.create_table(
        'model_reports',
        sa.Column('report_id', sa.String(length=64), primary_key=True),
        sa.Column('cycle_id', sa.String(length=64), sa.ForeignKey('medical_cycles.cycle_id', ondelete='CASCADE'), nullable=False),
        sa.Column('sprint_id', sa.String(length=64), sa.ForeignKey('weekly_sprints.sprint_id', ondelete='SET NULL'), nullable=True),
        sa.Column('session_id', sa.String(length=64), sa.ForeignKey('workout_sessions.session_id', ondelete='SET NULL'), nullable=True),
        sa.Column('model', sa.String(length=64), nullable=False),
        sa.Column('report_type', sa.String(length=64), nullable=False),
        sa.Column('privacy_level', sa.String(length=32), nullable=False),
        sa.Column('version', sa.Integer(), nullable=False, server_default='1'),
        sa.Column('content', sa.JSON(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
    )
    op.create_index('ix_model_reports_report_id', 'model_reports', ['report_id'], unique=False)
    op.create_index('ix_model_reports_cycle_id', 'model_reports', ['cycle_id'], unique=False)
    op.create_index('ix_model_reports_report_type', 'model_reports', ['report_type'], unique=False)
    op.create_index('ix_model_reports_privacy_level', 'model_reports', ['privacy_level'], unique=False)


def downgrade() -> None:
    op.drop_table('model_reports')
    op.drop_table('health_measurements')
    op.drop_table('workout_sessions')
    op.drop_table('weekly_sprints')
    op.drop_table('medical_cycles')
