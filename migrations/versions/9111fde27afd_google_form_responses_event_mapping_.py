from alembic import op
import sqlalchemy as sa


revision = '9111fde27afd'
down_revision = '99bb93236555'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('form_event_mapping',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('label', sa.String(length=200), nullable=False),
    sa.Column('service_type_id', sa.Integer(), nullable=False),
    sa.ForeignKeyConstraint(['service_type_id'], ['service_type.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('label')
    )
    op.create_table('sync_run',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('started_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('rows_seen', sa.Integer(), nullable=False),
    sa.Column('rows_imported', sa.Integer(), nullable=False),
    sa.Column('rows_flagged', sa.Integer(), nullable=False),
    sa.Column('error', sa.String(length=500), nullable=True),
    sa.Column('columns_note', sa.String(length=500), nullable=True),
    sa.Column('triggered_by_id', sa.Integer(), nullable=True),
    sa.ForeignKeyConstraint(['triggered_by_id'], ['app_user.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('form_response',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('source_key', sa.String(length=64), nullable=False),
    sa.Column('submitted_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('submitted_local_date', sa.Date(), nullable=False),
    sa.Column('raw_id_text', sa.String(length=50), nullable=True),
    sa.Column('returning', sa.Boolean(), nullable=True),
    sa.Column('raw_event_label', sa.String(length=200), nullable=True),
    sa.Column('reason', sa.String(length=300), nullable=True),
    sa.Column('score', sa.Integer(), nullable=True),
    sa.Column('score_reason', sa.String(length=300), nullable=True),
    sa.Column('decision', sa.String(length=10), nullable=True),
    sa.Column('participant_id', sa.Integer(), nullable=True),
    sa.Column('reviewed_by_id', sa.Integer(), nullable=True),
    sa.Column('reviewed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('imported_in_sync_run_id', sa.Integer(), nullable=False),
    sa.Column('imported_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint("decision IS NULL OR decision IN ('auto', 'matched', 'created', 'discarded')", name='ck_form_response_decision'),
    sa.CheckConstraint('score IS NULL OR score BETWEEN 1 AND 10', name='ck_form_response_score'),
    sa.ForeignKeyConstraint(['imported_in_sync_run_id'], ['sync_run.id'], ),
    sa.ForeignKeyConstraint(['participant_id'], ['participant.id'], ),
    sa.ForeignKeyConstraint(['reviewed_by_id'], ['app_user.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('source_key')
    )
    with op.batch_alter_table('form_response', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_form_response_decision'), ['decision'], unique=False)
        batch_op.create_index(batch_op.f('ix_form_response_submitted_at'), ['submitted_at'], unique=False)
        batch_op.create_index(batch_op.f('ix_form_response_submitted_local_date'), ['submitted_local_date'], unique=False)


def downgrade():
    with op.batch_alter_table('form_response', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_form_response_submitted_local_date'))
        batch_op.drop_index(batch_op.f('ix_form_response_submitted_at'))
        batch_op.drop_index(batch_op.f('ix_form_response_decision'))

    op.drop_table('form_response')
    op.drop_table('sync_run')
    op.drop_table('form_event_mapping')
