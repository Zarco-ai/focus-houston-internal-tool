"""qualities listed A-Z: drop sort_order

The client asked for the unique-qualities checklist in alphabetical order
instead of a number per quality (28 Oct 2026), so the number goes. Nothing
reads it any more. The downgrade puts it back at 0 for every row.

Revision ID: fdde63f7ce76
Revises: c1d88075704a
Create Date: 2026-10-28 12:53:28.402848

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'fdde63f7ce76'
down_revision = 'c1d88075704a'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('quality_option', schema=None) as batch_op:
        batch_op.drop_column('sort_order')



def downgrade():
    with op.batch_alter_table('quality_option', schema=None) as batch_op:
        batch_op.add_column(sa.Column('sort_order', sa.INTEGER(), server_default='0', nullable=False))

