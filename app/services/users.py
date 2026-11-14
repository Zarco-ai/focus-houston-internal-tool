
from app.config import parse_admin_emails  # noqa: F401  (re-exported for callers)
from app.extensions import db
from app.models import ROLE_ADMIN, ROLES, AppUser
from app.models.audit import ACTION_BOOTSTRAP, ACTION_LOGIN, ACTION_USER
from app.services import audit, clock

MAX_EMAIL_LENGTH = 254


class AccessDenied(Exception):
    pass


class UserError(Exception):
    pass


def normalize_email(raw):
    return (raw or "").strip().lower()


def validate_email(raw):
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


def find_by_email(email):
    return AppUser.query.filter_by(email=normalize_email(email)).one_or_none()


def all_users():
    return AppUser.query.order_by(AppUser.active.desc(), AppUser.email).all()


def active_admin_count():
    return AppUser.query.filter_by(role=ROLE_ADMIN, active=True).count()


def _is_last_active_admin(user):
    return user.active and user.is_admin and active_admin_count() <= 1


def sign_in(email, email_verified, admin_emails=()):
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


def add_user(email, role):
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
