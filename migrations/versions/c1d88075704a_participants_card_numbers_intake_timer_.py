"""participants, card numbers, intake timer and survey

New tables only (PRODUCTION_DESIGN 7.2-7.4); nothing existing changes.
participant has no name column, by design. intake_session has a partial
unique index: one running or paused timer per person (Phase 0 answer 3).

Revision ID: c1d88075704a
Revises: bbdded7d15ce
Create Date: 2026-10-27 22:28:11.146954

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'c1d88075704a'
down_revision = 'bbdded7d15ce'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('card_number',
    sa.Column('code', sa.String(length=5), nullable=False),
    sa.Column('batch', sa.Integer(), nullable=False),
    sa.Column('list_position', sa.Integer(), nullable=False),
    sa.Column('loaded_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint("code ~ '^[1-9][0-9]{4}$'", name='ck_card_number_format'),
    sa.CheckConstraint('list_position >= 1', name='ck_card_number_position'),
    sa.PrimaryKeyConstraint('code'),
    sa.UniqueConstraint('batch', 'list_position', name='uq_card_number_batch_position')
    )
    op.create_table('quality_option',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('label', sa.String(length=100), nullable=False),
    sa.Column('active', sa.Boolean(), nullable=False),
    sa.Column('sort_order', sa.Integer(), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('label')
    )
    op.create_table('participant',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('code', sa.String(length=5), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('created_by_id', sa.Integer(), nullable=False),
    sa.Column('merged_into_id', sa.Integer(), nullable=True),
    sa.ForeignKeyConstraint(['code'], ['card_number.code'], ),
    sa.ForeignKeyConstraint(['created_by_id'], ['app_user.id'], ),
    sa.ForeignKeyConstraint(['merged_into_id'], ['participant.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('code')
    )
    op.create_table('intake_session',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('started_by_id', sa.Integer(), nullable=False),
    sa.Column('batch_id', sa.Integer(), nullable=True),
    sa.Column('status', sa.String(length=10), nullable=False),
    sa.Column('started_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('stopped_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('paused_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('paused_seconds', sa.Integer(), server_default='0', nullable=False),
    sa.Column('timer_seconds', sa.Integer(), nullable=True),
    sa.Column('slider_minutes', sa.Integer(), nullable=True),
    sa.Column('confirmed_by_id', sa.Integer(), nullable=True),
    sa.Column('confirmed_at', sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint("status IN ('running', 'paused', 'stopped', 'discarded')", name='ck_intake_session_status'),
    sa.CheckConstraint('paused_seconds >= 0', name='ck_intake_session_paused_seconds'),
    sa.CheckConstraint('slider_minutes IS NULL OR (slider_minutes BETWEEN 0 AND 180 AND mod(slider_minutes, 15) = 0)', name='ck_intake_session_slider'),
    sa.CheckConstraint('timer_seconds IS NULL OR timer_seconds >= 0', name='ck_intake_session_timer_seconds'),
    sa.ForeignKeyConstraint(['batch_id'], ['batch.id'], ),
    sa.ForeignKeyConstraint(['confirmed_by_id'], ['app_user.id'], ),
    sa.ForeignKeyConstraint(['started_by_id'], ['app_user.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('intake_session', schema=None) as batch_op:
        batch_op.create_index('uq_intake_session_one_active_per_person', ['started_by_id'], unique=True, postgresql_where=sa.text("status IN ('running', 'paused')"))

    op.create_table('participant_visit',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('participant_id', sa.Integer(), nullable=False),
    sa.Column('visit_date', sa.Date(), nullable=False),
    sa.Column('source', sa.String(length=20), nullable=False),
    sa.Column('service_type_id', sa.Integer(), nullable=True),
    sa.Column('source_ref', sa.String(length=64), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint("source IN ('intake', 'google_form')", name='ck_participant_visit_source'),
    sa.ForeignKeyConstraint(['participant_id'], ['participant.id'], ),
    sa.ForeignKeyConstraint(['service_type_id'], ['service_type.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('source', 'source_ref', name='uq_participant_visit_source_ref')
    )
    with op.batch_alter_table('participant_visit', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_participant_visit_participant_id'), ['participant_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_participant_visit_visit_date'), ['visit_date'], unique=False)

    op.create_table('intake_survey',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('participant_id', sa.Integer(), nullable=False),
    sa.Column('intake_session_id', sa.Integer(), nullable=True),
    sa.Column('survey_date', sa.Date(), nullable=False),
    sa.Column('reason_for_joining', sa.String(length=300), nullable=True),
    sa.Column('come_back_score', sa.Integer(), nullable=True),
    sa.Column('come_back_reason', sa.String(length=300), nullable=True),
    sa.Column('qualities_note', sa.String(length=200), nullable=True),
    sa.Column('created_by_id', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint('come_back_score IS NULL OR come_back_score BETWEEN 1 AND 10', name='ck_intake_survey_score'),
    sa.ForeignKeyConstraint(['created_by_id'], ['app_user.id'], ),
    sa.ForeignKeyConstraint(['intake_session_id'], ['intake_session.id'], ),
    sa.ForeignKeyConstraint(['participant_id'], ['participant.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('intake_session_id')
    )
    with op.batch_alter_table('intake_survey', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_intake_survey_participant_id'), ['participant_id'], unique=False)

    op.create_table('intake_survey_quality',
    sa.Column('intake_survey_id', sa.Integer(), nullable=False),
    sa.Column('quality_option_id', sa.Integer(), nullable=False),
    sa.ForeignKeyConstraint(['intake_survey_id'], ['intake_survey.id'], ),
    sa.ForeignKeyConstraint(['quality_option_id'], ['quality_option.id'], ),
    sa.PrimaryKeyConstraint('intake_survey_id', 'quality_option_id')
    )


def downgrade():
    op.drop_table('intake_survey_quality')
    with op.batch_alter_table('intake_survey', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_intake_survey_participant_id'))

    op.drop_table('intake_survey')
    with op.batch_alter_table('participant_visit', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_participant_visit_visit_date'))
        batch_op.drop_index(batch_op.f('ix_participant_visit_participant_id'))

    op.drop_table('participant_visit')
    with op.batch_alter_table('intake_session', schema=None) as batch_op:
        batch_op.drop_index('uq_intake_session_one_active_per_person', postgresql_where=sa.text("status IN ('running', 'paused')"))

    op.drop_table('intake_session')
    op.drop_table('participant')
    op.drop_table('quality_option')
    op.drop_table('card_number')
