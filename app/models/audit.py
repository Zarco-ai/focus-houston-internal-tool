
from sqlalchemy import CheckConstraint

from app.extensions import db
from app.services import clock

ACTION_CREATE = "create"
ACTION_UPDATE = "update"
ACTION_LOGIN = "login"
ACTION_USER = "user"
ACTION_BOOTSTRAP = "bootstrap"
ACTION_EXPORT = "export"
ACTION_SYNC = "sync"
ACTION_IMPORT = "import"
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

SYSTEM_ACTIONS = (ACTION_SYNC, ACTION_BOOTSTRAP)


class AuditLog(db.Model):
    __tablename__ = "audit_log"
    __table_args__ = (
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

    description = db.Column(db.String(300), nullable=True)

    action = db.Column(db.String(20), nullable=False, default=ACTION_UPDATE, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("app_user.id"), nullable=True, index=True)
    user = db.relationship("AppUser")

    def __repr__(self):
        return (
            f"<AuditLog {self.action} {self.table_name}#{self.record_id} {self.field}: "
            f"{self.old_value} -> {self.new_value} by {self.user_id}>"
        )
