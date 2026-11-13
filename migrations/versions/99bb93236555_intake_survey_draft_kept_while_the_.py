"""intake survey draft kept while the timer runs

Staging found it (28 Oct 2026): answers typed into the intake survey were
lost when the timer was stopped, because each timer button reloads the
page. Every timer button now carries the typed answers, kept here until the
survey is saved or the intake discarded. One nullable column; nothing
existing changes.

Revision ID: 99bb93236555
Revises: fdde63f7ce76
Create Date: 2026-10-28 13:09:51.771348

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
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

