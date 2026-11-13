"""attendance fields, metric settings, annual figures

Attendance gains the per-event figures from the program director's sheet and the FOCUS
North America template (PRODUCTION_DESIGN 7.1). education_participants is
RENAMED to education_participants_total, not dropped and re-added:
autogenerate wrote it as a drop, which would have thrown away every class
count already entered. Every count gets a CHECK (>= 0), the existing two
included.

metric_definition holds an admin's label and on/off for a metric; a metric
nobody has touched has no row. annual_metric_value holds the figures typed
once a year.

Revision ID: bbdded7d15ce
Revises: fd91a187fae6
Create Date: 2026-10-27 19:52:18.554700

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'bbdded7d15ce'
down_revision = 'fd91a187fae6'
branch_labels = None
depends_on = None

NEW_COUNTS = (
    'education_participants_unique',
    'education_sessions_held',
    'case_management_served',
    'medical_services_served',
    'temp_employment_placed',
    'meals_value',
    'volunteers_total',
    'volunteer_hours_tenths',
    'volunteer_opportunities',
    'unique_clients_event_count',
    'unique_volunteers_event_count',
    'unique_parishes_event_count',
)

EXISTING_COUNTS = ('individuals_served', 'children_served', 'education_participants_total')


def upgrade():
    op.create_table('metric_definition',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('key', sa.String(length=64), nullable=False),
    sa.Column('label', sa.String(length=200), nullable=True),
    sa.Column('enabled', sa.Boolean(), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('key')
    )
    op.create_table('annual_metric_value',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('year', sa.Integer(), nullable=False),
    sa.Column('metric_key', sa.String(length=64), nullable=False),
    sa.Column('value', sa.Integer(), nullable=False),
    sa.Column('entered_by_id', sa.Integer(), nullable=False),
    sa.Column('entered_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint('value >= 0', name='ck_annual_metric_value_not_negative'),
    sa.CheckConstraint('year BETWEEN 2000 AND 2100', name='ck_annual_metric_value_year'),
    sa.ForeignKeyConstraint(['entered_by_id'], ['app_user.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('year', 'metric_key', name='uq_annual_metric_value_year_key')
    )
    with op.batch_alter_table('attendance', schema=None) as batch_op:
        batch_op.alter_column('education_participants',
                              new_column_name='education_participants_total')
        for name in NEW_COUNTS:
            batch_op.add_column(sa.Column(name, sa.Integer(), server_default='0', nullable=False))

    with op.batch_alter_table('attendance', schema=None) as batch_op:
        for name in EXISTING_COUNTS:
            batch_op.alter_column(name, server_default='0')
        for name in EXISTING_COUNTS + NEW_COUNTS:
            batch_op.create_check_constraint(f'ck_attendance_{name}_not_negative', f'{name} >= 0')


def downgrade():
    with op.batch_alter_table('attendance', schema=None) as batch_op:
        for name in EXISTING_COUNTS + NEW_COUNTS:
            batch_op.drop_constraint(f'ck_attendance_{name}_not_negative', type_='check')
        for name in EXISTING_COUNTS:
            batch_op.alter_column(name, server_default=None)
        for name in reversed(NEW_COUNTS):
            batch_op.drop_column(name)
        batch_op.alter_column('education_participants_total',
                              new_column_name='education_participants')

    op.drop_table('annual_metric_value')
    op.drop_table('metric_definition')
