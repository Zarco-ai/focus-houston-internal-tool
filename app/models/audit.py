"""A record of every change made after the fact -- and who made it.

A committed batch can still be corrected -- The Center Director will miscount, and
pretending otherwise just means she edits the spreadsheet instead. But every
correction to committed data, every change to the catalog, every change to
who may sign in, and every sign-in writes a row here: what table, what
record, what field, from what, to what, when, and by whom.

The log is append-only. Nothing in the app updates or deletes a row.
"""

from sqlalchemy import CheckConstraint

from app.extensions import db
from app.services import clock

# What kind of thing happened. A short, fixed vocabulary so the log can be
# filtered ("every sign-in this week", "every user change").
ACTION_CREATE = "create"
ACTION_UPDATE = "update"
ACTION_LOGIN = "login"
ACTION_USER = "user"          # someone added, removed, or given another role
ACTION_BOOTSTRAP = "bootstrap"  # an admin created from ADMIN_EMAILS
ACTION_EXPORT = "export"
ACTION_SYNC = "sync"          # the only action allowed with no user: the app itself
ACTION_IMPORT = "import"      # a catalog change made by the Valuations import
ACTIONS = (
    ACTION_CREATE,
    ACTION_UPDATE,
    ACTION_LOGIN,
    ACTION_USER,
    ACTION_BOOTSTRAP,
    ACTION_EXPORT,
    ACTION_SYNC,
    ACTION_IMPORT,
)

# Actions the app performs on its own, with no person behind them.
SYSTEM_ACTIONS = (ACTION_SYNC, ACTION_BOOTSTRAP)


class AuditLog(db.Model):
    __tablename__ = "audit_log"
    __table_args__ = (
        # Belt and braces for the rule in app/services/audit.py: a row with no
        # person behind it must be something the app did by itself.
        CheckConstraint(
            "user_id IS NOT NULL OR action IN ('sync', 'bootstrap')",
            name="ck_audit_log_user_unless_system",
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    table_name = db.Column(db.String(50), nullable=False, index=True)
    record_id = db.Column(db.Integer, nullable=False, index=True)
    field = db.Column(db.String(50), nullable=False)
    old_value = db.Column(db.Text, nullable=True)
    new_value = db.Column(db.Text, nullable=True)
    changed_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=clock.now_utc, index=True
    )

    # Plain English description of what was being changed, so the log is
    # readable without cross-referencing ids by hand.
    description = db.Column(db.String(300), nullable=True)

    action = db.Column(db.String(20), nullable=False, default=ACTION_UPDATE, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("app_user.id"), nullable=True, index=True)
    user = db.relationship("AppUser")

    def __repr__(self):
        return (
            f"<AuditLog {self.action} {self.table_name}#{self.record_id} {self.field}: "
            f"{self.old_value} -> {self.new_value} by {self.user_id}>"
        )
