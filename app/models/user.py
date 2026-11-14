
from sqlalchemy import CheckConstraint

from app.extensions import db
from app.services import clock

ROLE_ADMIN = "admin"
ROLE_STAFF = "staff"
ROLES = (ROLE_ADMIN, ROLE_STAFF)

ROLE_LABELS = {
    ROLE_ADMIN: "Admin",
    ROLE_STAFF: "Staff",
}


class AppUser(db.Model):
    __tablename__ = "app_user"
    __table_args__ = (
        CheckConstraint("role IN ('admin', 'staff')", name="ck_app_user_role"),
        CheckConstraint("email = lower(email)", name="ck_app_user_email_lowercase"),
    )

    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(254), nullable=False, unique=True)
    role = db.Column(db.String(10), nullable=False, default=ROLE_STAFF)
    active = db.Column(db.Boolean, nullable=False, default=True)

    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=clock.now_utc)
    last_login_at = db.Column(db.DateTime(timezone=True), nullable=True)

    added_by_id = db.Column(db.Integer, db.ForeignKey("app_user.id"), nullable=True)
    added_by = db.relationship("AppUser", remote_side=[id])

    @property
    def is_admin(self):
        return self.role == ROLE_ADMIN

    @property
    def role_label(self):
        return ROLE_LABELS.get(self.role, self.role)

    def __repr__(self):
        return f"<AppUser {self.id} {self.role}{'' if self.active else ' inactive'}>"
