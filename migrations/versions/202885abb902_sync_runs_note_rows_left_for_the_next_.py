"""sync runs note rows left for the next sync

Found by the Phase 9 stress test: a first sync of thousands of rows outran
gunicorn's 30-second limit. A sync now imports at most 300 new rows and
records how many are left. One new column, default 0.

Revision ID: 202885abb902
Revises: 281f72c02a6c
Create Date: 2026-10-29 12:36:07.243084

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
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

