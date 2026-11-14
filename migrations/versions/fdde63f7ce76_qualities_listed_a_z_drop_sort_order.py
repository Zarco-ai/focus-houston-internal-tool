from alembic import op
import sqlalchemy as sa


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
