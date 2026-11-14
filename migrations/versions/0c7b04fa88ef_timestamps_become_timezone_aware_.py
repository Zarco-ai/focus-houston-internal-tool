from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '0c7b04fa88ef'
down_revision = '6c62b8a8b901'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('audit_log', schema=None) as batch_op:
        batch_op.alter_column('changed_at',
               existing_type=postgresql.TIMESTAMP(),
               type_=sa.DateTime(timezone=True),
               postgresql_using="changed_at AT TIME ZONE 'UTC'",
               existing_nullable=False)

    with op.batch_alter_table('batch', schema=None) as batch_op:
        batch_op.alter_column('committed_at',
               existing_type=postgresql.TIMESTAMP(),
               type_=sa.DateTime(timezone=True),
               postgresql_using="committed_at AT TIME ZONE 'UTC'",
               existing_nullable=True)

    with op.batch_alter_table('line_item', schema=None) as batch_op:
        batch_op.alter_column('reviewed_at',
               existing_type=postgresql.TIMESTAMP(),
               type_=sa.DateTime(timezone=True),
               postgresql_using="reviewed_at AT TIME ZONE 'UTC'",
               existing_nullable=True)


def downgrade():
    with op.batch_alter_table('line_item', schema=None) as batch_op:
        batch_op.alter_column('reviewed_at',
               existing_type=sa.DateTime(timezone=True),
               type_=postgresql.TIMESTAMP(),
               postgresql_using="reviewed_at AT TIME ZONE 'UTC'",
               existing_nullable=True)

    with op.batch_alter_table('batch', schema=None) as batch_op:
        batch_op.alter_column('committed_at',
               existing_type=sa.DateTime(timezone=True),
               type_=postgresql.TIMESTAMP(),
               postgresql_using="committed_at AT TIME ZONE 'UTC'",
               existing_nullable=True)

    with op.batch_alter_table('audit_log', schema=None) as batch_op:
        batch_op.alter_column('changed_at',
               existing_type=sa.DateTime(timezone=True),
               type_=postgresql.TIMESTAMP(),
               postgresql_using="changed_at AT TIME ZONE 'UTC'",
               existing_nullable=False)
