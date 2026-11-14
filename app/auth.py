
from flask import abort, g, redirect, request, session, url_for

from app.extensions import db
from app.models import AppUser
from app.services import audit, clock

PUBLIC_ENDPOINTS = frozenset(
    {
        "auth.login",
        "auth.start",
        "auth.callback",
        "health.check",
        "static",
    }
)

STAFF_ENDPOINTS = frozenset(
    {
        "auth.logout",
        "home.index",
        "home.new_batch",
        "home.resume_today",
        "home.discard",
        "entry.entry_screen",
        "entry.update_line",
        "entry.update_line_by_id",
        "entry.add_price_group",
        "entry.remove_price_group",
        "entry.set_service_type",
        "entry.add_custom_item",
        "review.review_screen",
        "review.edit_lines",
        "review.set_attendance",
        "review.set_date",
        "review.set_note",
        "review.approve",
        "review.queue",
        "review.resolve",
        "totals.index",
        "batches.view_batch",
        "admin.items",
        "admin.needs_price",
    }
)

SESSION_USER_ID = "user_id"
SESSION_SIGNED_IN_AT = "signed_in_at"


def requires_admin(endpoint):
    if endpoint is None or endpoint in PUBLIC_ENDPOINTS:
        return False
    return endpoint not in STAFF_ENDPOINTS


def start_session(user):
    session.clear()
    session[SESSION_USER_ID] = user.id
    session[SESSION_SIGNED_IN_AT] = clock.now_utc().timestamp()
    session.permanent = True


def end_session():
    session.clear()


def load_signed_in_user(max_hours):
    user_id = session.get(SESSION_USER_ID)
    signed_in_at = session.get(SESSION_SIGNED_IN_AT)
    if user_id is None or signed_in_at is None:
        return None

    age_seconds = clock.now_utc().timestamp() - float(signed_in_at)
    if age_seconds < 0 or age_seconds > max_hours * 3600:
        end_session()
        return None

    user = db.session.get(AppUser, user_id)
    if user is None or not user.active:
        end_session()
        return None
    return user


def register_guard(app):
    @app.before_request
    def require_sign_in():
        endpoint = request.endpoint
        g.user = None

        if endpoint in PUBLIC_ENDPOINTS:
            return None

        user = load_signed_in_user(app.config["SESSION_HOURS"])
        if user is None:
            return redirect(url_for("auth.login"))

        g.user = user
        g.audit_actor_token = audit.current_actor.set(user.id)

        if requires_admin(endpoint) and not user.is_admin:
            abort(403)
        return None

    @app.teardown_request
    def forget_actor(exc):
        token = g.pop("audit_actor_token", None)
        if token is not None:
            audit.current_actor.reset(token)

    @app.context_processor
    def inject_user():
        return {"current_user": g.get("user")}
