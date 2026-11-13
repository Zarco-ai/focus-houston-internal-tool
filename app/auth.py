"""The sign-in guard: one check, in front of every request.

Deny by default. Only the endpoints in PUBLIC_ENDPOINTS can be reached
without signing in -- the sign-in pages themselves, the health check, and
static files. Everything else, including any page added in the future,
requires a signed-in user whose row is active. This is one before_request
hook, not a decorator on each view, because a decorator is the kind of
thing someone forgets on the one page that mattered.
tests/test_auth.py walks every route in the app to prove it.

On every request the guard:

  1. re-reads the user from the database -- so removing someone, or
     changing their role, takes effect on their very next click;
  2. ends the session if it is older than SESSION_HOURS, counted from
     sign-in (absolute, not sliding -- a session always ends overnight);
  3. refuses staff anything not in STAFF_ENDPOINTS with a 403, on the
     server, whatever the page's links look like;
  4. tells the audit log who is acting (audit.current_actor).
"""

from flask import abort, g, redirect, request, session, url_for

from app.extensions import db
from app.models import AppUser
from app.services import audit, clock

# The only endpoints reachable without signing in. Adding one here is a
# security decision -- say why in the commit.
PUBLIC_ENDPOINTS = frozenset(
    {
        "auth.login",      # the sign-in page
        "auth.start",      # sends the browser to Google
        "auth.callback",   # where Google sends it back
        "health.check",    # Render's health check; says nothing but ok/not ok
        "static",          # CSS and JavaScript
    }
)

# The only endpoints staff can reach; everything else is admin-only. Deny by
# default: a new page is admin-only until it is added here (the client's
# decision, 11 Nov 2026: staff count, review and see the totals).
STAFF_ENDPOINTS = frozenset(
    {
        "auth.logout",
        # Counting: start, count, review and approve a batch
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
        # The review queue
        "review.queue",
        "review.resolve",
        # Totals: the date range and the metrics only (the page hides the rest)
        "totals.index",
        # An approved batch, from Home's "Recently approved"
        "batches.view_batch",
        # The catalog, read-only (editing stays admin-only)
        "admin.items",
        "admin.needs_price",
    }
)

SESSION_USER_ID = "user_id"
SESSION_SIGNED_IN_AT = "signed_in_at"


def requires_admin(endpoint):
    # None is an unknown URL: a 404, not a 403.
    if endpoint is None or endpoint in PUBLIC_ENDPOINTS:
        return False
    return endpoint not in STAFF_ENDPOINTS


def start_session(user):
    """Called once Google and the allowlist both say yes."""
    # A fresh session, so nothing from before sign-in (including anything an
    # attacker planted) carries over: protection against session fixation.
    session.clear()
    session[SESSION_USER_ID] = user.id
    session[SESSION_SIGNED_IN_AT] = clock.now_utc().timestamp()
    session.permanent = True


def end_session():
    session.clear()


def load_signed_in_user(max_hours):
    """The signed-in user, or None. Clears the session if it has expired or
    the user has been removed."""
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
            # Includes unknown URLs: an anonymous visitor learns nothing about
            # which pages exist.
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
