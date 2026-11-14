from alembic import op
import sqlalchemy as sa


revision = '202885abb902'
down_revision = '281f72c02a6c'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('sync_run', schema=None) as batch_op:
        batch_op.add_column(sa.Column('rows_remaining', sa.Integer(), server_default='0', nullable=False))


def downgrade():
    with op.batch_alter_table('sync_run', schema=None) as batch_op:
        batch_op.drop_column('rows_remaining')
