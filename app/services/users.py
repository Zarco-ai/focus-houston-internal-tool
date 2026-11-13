"""Who may sign in, and what they may do.

Google proves who someone is. This module decides whether that person gets
in. The answer is "yes" only for an email on the allowlist (the app_user
table) whose row is active. Everything else is refused, including a Google
account whose email Google itself has not verified.

ADMIN_EMAILS -- the first admins, so nobody is locked out on day one
------------------------------------------------------------------------
The emails in the ADMIN_EMAILS environment variable become admins the first
time they sign in, and only then. After that they are ordinary admins: they
can add and remove people, change roles, and remove themselves. Once
removed, ADMIN_EMAILS does NOT bring them back -- their row still exists
(inactive), and that row is what tells this module "this person was here and
was deliberately removed". Only another admin can restore them.

The one thing nobody can do is remove or demote the LAST active admin.
Without that rule, the last two admins could each remove themselves and
leave nobody able to manage the app; the only way back would be the
break-glass command run from Render (flask users grant-admin).
"""

from app.config import parse_admin_emails  # noqa: F401  (re-exported for callers)
from app.extensions import db
from app.models import ROLE_ADMIN, ROLES, AppUser
from app.models.audit import ACTION_BOOTSTRAP, ACTION_LOGIN, ACTION_USER
from app.services import audit, clock

MAX_EMAIL_LENGTH = 254


class AccessDenied(Exception):
    """This Google account may not sign in. Deliberately vague to the person
    signing in -- the page never says whether an email is on the list."""


class UserError(Exception):
    """Something about a user change was not allowed. The message is shown to
    the admin who tried it."""


# --- Emails -------------------------------------------------------------------


def normalize_email(raw):
    """Lowercase and trim. Google treats email addresses case-insensitively,
    and so do we: Pat@Example.org and pat@example.org are one person."""
    return (raw or "").strip().lower()


def validate_email(raw):
    """A normalized email, or UserError. Deliberately simple: Google decides
    what a real address is; this only stops obvious typos."""
    email = normalize_email(raw)
    local, at, domain = email.partition("@")
    if (
        not at
        or not local
        or "." not in domain
        or " " in email
        or "@" in domain
        or len(email) > MAX_EMAIL_LENGTH
    ):
        raise UserError(f"'{raw}' doesn't look like an email address.")
    return email


# --- Reading ------------------------------------------------------------------


def find_by_email(email):
    return AppUser.query.filter_by(email=normalize_email(email)).one_or_none()


def all_users():
    """Active people first, then removed ones; alphabetical within each."""
    return AppUser.query.order_by(AppUser.active.desc(), AppUser.email).all()


def active_admin_count():
    return AppUser.query.filter_by(role=ROLE_ADMIN, active=True).count()


def _is_last_active_admin(user):
    return user.active and user.is_admin and active_admin_count() <= 1


# --- Signing in -----------------------------------------------------------------


def sign_in(email, email_verified, admin_emails=()):
    """Decide whether a Google-verified identity gets in. Returns the user,
    or raises AccessDenied. Records the sign-in in the audit log."""
    if email_verified is not True:
        raise AccessDenied("Google has not verified this email address.")

    email = normalize_email(email)
    if not email:
        raise AccessDenied("No email address came back from Google.")

    user = find_by_email(email)

    if user is None and email in admin_emails:
        user = _bootstrap_admin(email)

    if user is None or not user.active:
        raise AccessDenied("This account is not on the list.")

    user.last_login_at = clock.now_utc()
    with audit.acting_as(user.id):
        audit.record_event(
            ACTION_LOGIN, "app_user", user.id, f"{user.email} signed in"
        )
    db.session.commit()
    return user


def _bootstrap_admin(email):
    """Create an admin from ADMIN_EMAILS. Only ever called when there is no
    row at all for this email -- a removed admin has a row, so is never
    re-created here."""
    user = AppUser(email=email, role=ROLE_ADMIN, active=True, added_by_id=None)
    db.session.add(user)
    db.session.flush()
    with audit.acting_as(None):
        audit.record_event(
            ACTION_BOOTSTRAP,
            "app_user",
            user.id,
            f"{email} made an admin from ADMIN_EMAILS on first sign-in",
        )
    return user


# --- Managing people (admins only; the views enforce that) ---------------------


def add_user(email, role):
    """Put someone on the list, or restore someone who was removed."""
    email = validate_email(email)
    if role not in ROLES:
        raise UserError("Pick a role: admin or staff.")

    existing = find_by_email(email)
    if existing is not None and existing.active:
        raise UserError(f"{email} already has access.")

    if existing is not None:
        audit.apply_change(existing, "active", True, "app_user",
                           f"Restored access for {email}", action=ACTION_USER)
        audit.apply_change(existing, "role", role, "app_user",
                           f"Role for {email}", action=ACTION_USER)
        db.session.commit()
        return existing

    user = AppUser(
        email=email,
        role=role,
        active=True,
        added_by_id=audit.current_actor.get(),
    )
    db.session.add(user)
    db.session.flush()
    audit.record_change(
        "app_user", user.id, "created", None, f"{email} ({role})",
        description=f"Added {email} as {role}", action=ACTION_USER,
    )
    db.session.commit()
    return user


def set_role(user, role):
    if role not in ROLES:
        raise UserError("Pick a role: admin or staff.")
    if role != ROLE_ADMIN and _is_last_active_admin(user):
        raise UserError(
            f"{user.email} is the only admin left. Make someone else an admin "
            f"first, so the app always has one."
        )
    changed = audit.apply_change(user, "role", role, "app_user",
                                 f"Role for {user.email}", action=ACTION_USER)
    db.session.commit()
    return changed


def remove_user(user):
    """Take away someone's access. Their row stays, inactive. Takes effect on
    their very next request -- the sign-in guard re-reads this row every
    time."""
    if _is_last_active_admin(user):
        raise UserError(
            f"{user.email} is the only admin left. Make someone else an admin "
            f"first, so the app always has one."
        )
    changed = audit.apply_change(user, "active", False, "app_user",
                                 f"Removed access for {user.email}", action=ACTION_USER)
    db.session.commit()
    return changed


def grant_admin_break_glass(email):
    """The emergency way back in, for when no admin can sign in. Run from
    Render's shell by whoever controls the hosting account:

        flask --app wsgi users grant-admin someone@example.org

    Creates or restores the person as an active admin, and records it in the
    audit log as a system action.
    """
    email = validate_email(email)
    user = find_by_email(email)
    with audit.acting_as(None):
        if user is None:
            user = AppUser(email=email, role=ROLE_ADMIN, active=True)
            db.session.add(user)
            db.session.flush()
        else:
            user.active = True
            user.role = ROLE_ADMIN
        audit.record_event(
            ACTION_BOOTSTRAP, "app_user", user.id,
            f"{email} made an admin with the break-glass command",
        )
    db.session.commit()
    return user
