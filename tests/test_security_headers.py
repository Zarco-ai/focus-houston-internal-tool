"""Security headers, the Content-Security-Policy, and error pages.

The headers are set in one place (app/security.py). These tests check they
arrive on every kind of response -- a signed-in page, the sign-in page, an
error page, a static file -- and that nothing in the templates would break
under the policy (an inline script would simply not run).
"""

import pathlib
import re

import pytest
from flask import abort

from app import create_app
from app.config import ProductionConfig, TestConfig
from app.security import CONTENT_SECURITY_POLICY

TEMPLATES = pathlib.Path(__file__).resolve().parent.parent / "app" / "templates"

REQUIRED = {
    "Content-Security-Policy": CONTENT_SECURITY_POLICY,
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
}


def assert_security_headers(response):
    for name, value in REQUIRED.items():
        assert response.headers.get(name) == value, f"{name} on {response.request.path}"


def test_security_headers_are_on_a_signed_in_page(client):
    assert_security_headers(client.get("/"))


def test_security_headers_are_on_the_sign_in_page(anonymous_client):
    assert_security_headers(anonymous_client.get("/login"))


def test_security_headers_are_on_error_pages(staff_client):
    response = staff_client.get("/admin/users")
    assert response.status_code == 403
    assert_security_headers(response)


def test_security_headers_are_on_static_files(anonymous_client):
    assert_security_headers(anonymous_client.get("/static/css/app.css"))


def test_the_policy_allows_no_inline_or_outside_scripts():
    script_src = re.search(r"script-src ([^;]+)", CONTENT_SECURITY_POLICY).group(1)
    assert script_src.strip() == "'self'"
    assert "frame-ancestors 'none'" in CONTENT_SECURITY_POLICY
    assert "object-src 'none'" in CONTENT_SECURITY_POLICY


def test_no_template_has_inline_javascript():
    """Under script-src 'self' an inline script or onclick= silently does
    nothing. This catches one before it ships as a dead button."""
    offenders = []
    for path in sorted(TEMPLATES.rglob("*.html")):
        source = path.read_text(encoding="utf-8")
        if re.search(r"<script(?![^>]*\bsrc=)[^>]*>", source):
            offenders.append(f"{path.name}: inline <script>")
        if re.search(r"\son[a-z]+\s*=", source):
            offenders.append(f"{path.name}: on...= handler")
        if "javascript:" in source:
            offenders.append(f"{path.name}: javascript: URL")
    assert not offenders, offenders


def test_pages_are_not_cached_but_static_files_may_be(client):
    """Back button after signing out on a shared computer shows nothing."""
    assert client.get("/").headers["Cache-Control"] == "no-store"
    assert client.get("/static/css/app.css").headers.get("Cache-Control") != "no-store"


def test_hsts_is_not_sent_in_development(client):
    """Sending HSTS from http://localhost would make the browser insist on
    HTTPS for localhost for two years."""
    assert "Strict-Transport-Security" not in client.get("/").headers


@pytest.fixture
def production_app(monkeypatch):
    monkeypatch.delenv("FOCUS_ENV", raising=False)
    monkeypatch.setenv("SECRET_KEY", "p" * 40)
    # The real test database (pages read a setting from it). sslmode=disable
    # only because the local Docker database has no certificate; production
    # adds sslmode=require when the URL doesn't say.
    from app.config import LOCAL_TEST_DATABASE_URL
    import os
    monkeypatch.setenv(
        "DATABASE_URL",
        os.environ.get("TEST_DATABASE_URL", LOCAL_TEST_DATABASE_URL) + "?sslmode=disable",
    )
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "id")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "secret")
    return create_app(ProductionConfig)


def test_hsts_is_sent_in_production(production_app):
    response = production_app.test_client().get(
        "/login", headers={"X-Forwarded-Proto": "https"}
    )
    assert response.headers["Strict-Transport-Security"] == "max-age=63072000"
    assert_security_headers(response)


def test_behind_renders_proxy_the_google_return_address_is_https(production_app, monkeypatch):
    """Render's proxy terminates HTTPS and forwards plain HTTP to the app.
    ProxyFix makes the app believe the forwarded scheme -- without it, the
    return address sent to Google would be http://, which Google rejects."""
    from flask import redirect

    from app.extensions import oauth

    sent = {}

    def capture(redirect_uri):
        sent["redirect_uri"] = redirect_uri
        return redirect("https://accounts.google.com/")

    with production_app.app_context():
        monkeypatch.setattr(oauth.google, "authorize_redirect", capture)
        production_app.test_client().get(
            "/login/google",
            headers={
                "X-Forwarded-Proto": "https",
                "X-Forwarded-Host": "focus.onrender.com",
                "X-Forwarded-For": "203.0.113.9",
            },
        )

    assert sent["redirect_uri"] == "https://focus.onrender.com/auth/callback"


def test_behind_cloudflare_the_rate_limit_counts_each_visitor(production_app, monkeypatch):
    """Cloudflare names the visitor in CF-Connecting-IP. X-Forwarded-For
    showed a different Cloudflare address on almost every request on
    staging, so counting by it spread one visitor over many counters (the
    first visitor below would never be refused). Each visitor must get their
    own count however their requests are routed."""
    from flask import redirect

    from app.extensions import oauth

    production_app.config["SIGN_IN_RATE_LIMIT"] = "2 per minute"

    def sign_in(browser, visitor, edge):
        return browser.get(
            "/login/google",
            headers={
                "X-Forwarded-Proto": "https",
                "X-Forwarded-For": f"198.51.100.{edge}, 104.23.204.{edge}",
                "CF-Connecting-IP": visitor,
            },
        ).status_code

    with production_app.app_context():
        monkeypatch.setattr(oauth.google, "authorize_redirect",
                            lambda uri: redirect("https://accounts.google.com/"))
        browser = production_app.test_client()
        # The same visitor each time, arriving through a different edge.
        first_visitor = [sign_in(browser, "203.0.113.20", edge) for edge in range(3)]
        second_visitor = sign_in(browser, "203.0.113.21", 0)

    assert first_visitor == [302, 302, 429]
    assert second_visitor == 302


def test_on_a_laptop_the_rate_limit_ignores_cloudflares_header(make_app, monkeypatch):
    """Only production trusts CF-Connecting-IP. Anywhere else nothing sets it,
    so a value in a request was typed by whoever sent it -- changing it must
    not reset the count."""
    from flask import redirect

    from app.config import TestConfig
    from app.extensions import oauth

    class Limited(TestConfig):
        RATELIMIT_ENABLED = True
        SIGN_IN_RATE_LIMIT = "2 per minute"

    app = make_app(Limited)
    with app.app_context():
        monkeypatch.setattr(oauth.google, "authorize_redirect",
                            lambda uri: redirect("https://accounts.google.com/"))
        browser = app.test_client()
        statuses = [
            browser.get("/login/google", headers={"CF-Connecting-IP": f"203.0.113.{n}"}).status_code
            for n in range(3)
        ]

    assert statuses == [302, 302, 429]


# --- Error pages ------------------------------------------------------------------


class NoPropagateConfig(TestConfig):
    PROPAGATE_EXCEPTIONS = False


def test_an_unexpected_error_shows_a_friendly_page_not_a_traceback(make_app, sign_in_as):
    from app.extensions import db
    from app.models import ROLE_ADMIN, AppUser

    broken = make_app(NoPropagateConfig)

    @broken.route("/__boom")
    def boom():
        raise RuntimeError("secret internals that must not reach the page")

    with broken.app_context():
        db.session.add(AppUser(email="err@example.org", role=ROLE_ADMIN))
        db.session.commit()
        user = AppUser.query.filter_by(email="err@example.org").one()
        browser = broken.test_client()
        sign_in_as(browser, user)
        response = browser.get("/__boom")
        db.session.delete(user)
        db.session.commit()

    page = response.get_data(as_text=True)
    assert response.status_code == 500
    assert "Something went wrong" in page
    assert "secret internals" not in page
    assert "Traceback" not in page


def test_a_forbidden_page_is_friendly(staff_client):
    response = staff_client.get("/admin/settings")
    assert response.status_code == 403
    assert "for admins" in response.get_data(as_text=True)


def test_a_missing_page_is_friendly_for_a_signed_in_user(client):
    response = client.get("/no-such-page")
    assert response.status_code == 404
    assert "Page not found" in response.get_data(as_text=True)


def test_abort_is_importable_for_views():
    # Views use flask.abort(403/404); the handlers above turn it into a page.
    assert callable(abort)
