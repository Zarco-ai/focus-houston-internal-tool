from alembic import op
import sqlalchemy as sa


revision = '99bb93236555'
down_revision = 'fdde63f7ce76'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('intake_session', schema=None) as batch_op:
        batch_op.add_column(sa.Column('survey_draft', sa.JSON(), nullable=True))


def downgrade():
    with op.batch_alter_table('intake_session', schema=None) as batch_op:
        batch_op.drop_column('survey_draft')
