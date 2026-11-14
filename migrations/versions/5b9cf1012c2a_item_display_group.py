from alembic import op
import sqlalchemy as sa


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
