"""The people allowed to sign in.

  app_user  One row per email address on the allowlist. Sign-in itself is
            done by Google -- there are no passwords here, ever. A Google
            account whose email is not in this table, or whose row is not
            active, is refused.

Removing someone sets active = False. The row is never deleted: the audit
log points at it (who corrected that batch?), and its existence is also what
stops ADMIN_EMAILS from re-creating an admin who was deliberately removed --
see app/services/users.py.

These are staff and volunteers who run the app. Nothing about the mothers is
stored here or anywhere else in the app by name.
"""

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
        # Emails are compared lowercased everywhere; storing them that way
        # means the unique constraint really means "one row per person".
        CheckConstraint("email = lower(email)", name="ck_app_user_email_lowercase"),
    )

    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(254), nullable=False, unique=True)
    role = db.Column(db.String(10), nullable=False, default=ROLE_STAFF)
    active = db.Column(db.Boolean, nullable=False, default=True)

    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=clock.now_utc)
    last_login_at = db.Column(db.DateTime(timezone=True), nullable=True)

    # Who put this person on the list. NULL for an admin created from the
    # ADMIN_EMAILS environment variable on their first sign-in.
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
