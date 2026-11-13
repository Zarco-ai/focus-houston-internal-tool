"""line bucket frozen at approval; typed price and rate

report_bucket_at_time: a line's report bucket, frozen when its batch is
approved, like its price and used rate. Before this, a line read its bucket
from the item every time, so changing an item's bucket re-sorted every past
month -- including reports already sent.

Lines that are already approved get their item's bucket as it is today. That
is exactly what they were reporting under until now, so no total moves.
Drafts and unsorted custom lines stay NULL: they follow their item until
approval.

price_typed / used_rate_typed: set when someone types a price or used rate on
the review screen, so saving the draft again doesn't put the catalog's value
back. Existing lines start false, which is today's behavior.

Revision ID: fd91a187fae6
Revises: 5b9cf1012c2a
Create Date: 2026-10-27 17:20:42.505725

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
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
