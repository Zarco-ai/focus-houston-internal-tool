"""item display group: which sub-row an item is listed under on reports

Nullable, and NULL means "the same as the item's report bucket", so every
existing item keeps its current place without a data change. Wipes is the
one item set differently, by the Valuations import (Phase 3).

Revision ID: 5b9cf1012c2a
Revises: dc70c033c2f0
Create Date: 2026-10-27 16:51:35.447289

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '5b9cf1012c2a'
down_revision = 'dc70c033c2f0'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('item', schema=None) as batch_op:
        batch_op.add_column(sa.Column('display_group', sa.String(length=50), nullable=True))



def downgrade():
    with op.batch_alter_table('item', schema=None) as batch_op:
        batch_op.drop_column('display_group')

