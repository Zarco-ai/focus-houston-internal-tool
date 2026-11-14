
import logging

from authlib.integrations.base_client.errors import OAuthError
from flask import (
    Blueprint,
    current_app,
    flash,
    g,
    redirect,
    render_template,
    url_for,
)

from app.auth import end_session, start_session
from app.extensions import limiter, oauth
from app.services import users

auth_bp = Blueprint("auth", __name__)
log = logging.getLogger(__name__)


def google_configured():
    return bool(
        current_app.config.get("GOOGLE_CLIENT_ID")
        and current_app.config.get("GOOGLE_CLIENT_SECRET")
    )


def register_google(app):
    oauth.init_app(app)
    oauth.register(
        "google",
        client_id=app.config.get("GOOGLE_CLIENT_ID"),
        client_secret=app.config.get("GOOGLE_CLIENT_SECRET"),
        server_metadata_url=app.config["GOOGLE_DISCOVERY_URL"],
        client_kwargs={"scope": "openid email"},
        overwrite=True,
    )


def _sign_in_limit():
    return current_app.config["SIGN_IN_RATE_LIMIT"]


@auth_bp.route("/login")
def login():
    if g.get("user") is not None:
        return redirect(url_for("home.index"))
    return render_template("auth/login.html", configured=google_configured())


@auth_bp.route("/login/google")
@limiter.limit(_sign_in_limit)
def start():
    if not google_configured():
        return redirect(url_for("auth.login"))
    redirect_uri = url_for("auth.callback", _external=True)
    return oauth.google.authorize_redirect(redirect_uri)


@auth_bp.route("/auth/callback")
@limiter.limit(_sign_in_limit)
def callback():
    if not google_configured():
        return redirect(url_for("auth.login"))

    try:
        token = oauth.google.authorize_access_token()
    except OAuthError as error:
        log.warning("Google sign-in did not complete: %s", error.error)
        flash("Sign-in didn't complete. Please try again.", "error")
        return redirect(url_for("auth.login"))

    claims = token.get("userinfo") or {}
    try:
        user = users.sign_in(
            email=claims.get("email"),
            email_verified=claims.get("email_verified"),
            admin_emails=current_app.config.get("ADMIN_EMAILS", frozenset()),
        )
    except users.AccessDenied:
        log.info("Refused a sign-in from an account that is not on the list.")
        end_session()
        return render_template(
            "auth/denied.html", email=users.normalize_email(claims.get("email"))
        ), 403

    start_session(user)
    return redirect(url_for("home.index"))


@auth_bp.route("/logout", methods=["POST"])
def logout():
    end_session()
    flash("Signed out.", "success")
    return redirect(url_for("auth.login"))
