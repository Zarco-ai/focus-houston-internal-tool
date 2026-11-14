from alembic import op
import sqlalchemy as sa


revision = 'dc70c033c2f0'
down_revision = '0c7b04fa88ef'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('app_user',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('email', sa.String(length=254), nullable=False),
    sa.Column('role', sa.String(length=10), nullable=False),
    sa.Column('active', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('last_login_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('added_by_id', sa.Integer(), nullable=True),
    sa.CheckConstraint("role IN ('admin', 'staff')", name='ck_app_user_role'),
    sa.CheckConstraint('email = lower(email)', name='ck_app_user_email_lowercase'),
    sa.ForeignKeyConstraint(['added_by_id'], ['app_user.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('email')
    )
    with op.batch_alter_table('audit_log', schema=None) as batch_op:
        batch_op.add_column(sa.Column('action', sa.String(length=20), nullable=False,
                                      server_default='update'))
        batch_op.alter_column('action', server_default=None)
        batch_op.add_column(sa.Column('user_id', sa.Integer(), nullable=True))
        batch_op.create_index(batch_op.f('ix_audit_log_action'), ['action'], unique=False)
        batch_op.create_index(batch_op.f('ix_audit_log_user_id'), ['user_id'], unique=False)
        batch_op.create_foreign_key('fk_audit_log_user_id_app_user', 'app_user',
                                    ['user_id'], ['id'])

    audit_log = sa.table("audit_log", sa.column("id"))
    existing_rows = op.get_bind().execute(
        sa.select(sa.func.count()).select_from(audit_log)
    ).scalar()
    if existing_rows:
        raise RuntimeError(
            f"audit_log already has {existing_rows} rows from before sign-in "
            "existed, and none of them can say who made the change. This only "
            "happens on a development database: reset it with "
            "`python seed.py --reset`."
        )
    op.create_check_constraint(
        "ck_audit_log_user_unless_system",
        "audit_log",
        "user_id IS NOT NULL OR action IN ('sync', 'bootstrap')",
    )


def downgrade():
    op.drop_constraint('ck_audit_log_user_unless_system', 'audit_log', type_='check')
    with op.batch_alter_table('audit_log', schema=None) as batch_op:
        batch_op.drop_constraint('fk_audit_log_user_id_app_user', type_='foreignkey')
        batch_op.drop_index(batch_op.f('ix_audit_log_user_id'))
        batch_op.drop_index(batch_op.f('ix_audit_log_action'))
        batch_op.drop_column('user_id')
        batch_op.drop_column('action')

    op.drop_table('app_user')
