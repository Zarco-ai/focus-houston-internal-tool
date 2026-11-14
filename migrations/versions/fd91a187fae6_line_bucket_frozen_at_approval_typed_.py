from alembic import op
import sqlalchemy as sa


revision = 'fd91a187fae6'
down_revision = '5b9cf1012c2a'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('line_item', schema=None) as batch_op:
        batch_op.add_column(sa.Column('report_bucket_at_time', sa.String(length=50), nullable=True))
        batch_op.add_column(sa.Column('price_typed', sa.Boolean(), server_default=sa.text('false'), nullable=False))
        batch_op.add_column(sa.Column('used_rate_typed', sa.Boolean(), server_default=sa.text('false'), nullable=False))

    op.execute(
        """
        UPDATE line_item
           SET report_bucket_at_time = item.report_bucket
          FROM item, batch
         WHERE line_item.item_id = item.id
           AND line_item.batch_id = batch.id
           AND batch.status = 'committed'
        """
    )


def downgrade():
    with op.batch_alter_table('line_item', schema=None) as batch_op:
        batch_op.drop_column('used_rate_typed')
        batch_op.drop_column('price_typed')
        batch_op.drop_column('report_bucket_at_time')
