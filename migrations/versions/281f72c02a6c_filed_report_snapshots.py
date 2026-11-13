"""filed report snapshots

One new table (PRODUCTION_DESIGN 7.6). Rows are never updated or deleted;
the model refuses both. payload is JSONB on PostgreSQL.

Revision ID: 281f72c02a6c
Revises: 9111fde27afd
Create Date: 2026-10-28 16:01:47.045645

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = '281f72c02a6c'
down_revision = '9111fde27afd'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('report_snapshot',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('kind', sa.String(length=10), nullable=False),
    sa.Column('period_start', sa.Date(), nullable=False),
    sa.Column('period_end', sa.Date(), nullable=False),
    sa.Column('filed_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('filed_by_id', sa.Integer(), nullable=False),
    sa.Column('payload', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('payload_sha256', sa.String(length=64), nullable=False),
    sa.Column('app_version', sa.String(length=40), nullable=False),
    sa.CheckConstraint("kind IN ('monthly', 'annual')", name='ck_report_snapshot_kind'),
    sa.CheckConstraint('period_start <= period_end', name='ck_report_snapshot_period'),
    sa.ForeignKeyConstraint(['filed_by_id'], ['app_user.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('report_snapshot', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_report_snapshot_period_start'), ['period_start'], unique=False)



def downgrade():
    with op.batch_alter_table('report_snapshot', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_report_snapshot_period_start'))

    op.drop_table('report_snapshot')
