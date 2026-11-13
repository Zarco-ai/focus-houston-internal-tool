"""Security headers on every response, and friendly error pages.

A small after_request hook instead of Flask-Talisman (docs/PRODUCTION_DESIGN.md
section 4): every header is right here, in our own code, and
tests/test_security_headers.py asserts each one.

What each header does:

  Content-Security-Policy   The browser runs only scripts served from this
                            site -- no inline <script>, no onclick="...", no
                            third-party code. If someone ever got text onto a
                            page that looked like a script, it would not run.
                            Styles still allow inline style="" attributes
                            (34 in the templates): a known, low-risk gap,
                            recorded in SECURITY.md.
  frame-ancestors / X-Frame-Options
                            No other site can show these pages inside a frame
                            (clickjacking: tricking someone into clicking a
                            hidden "Approve" button).
  X-Content-Type-Options    The browser trusts our declared file types.
  Referrer-Policy           Our URLs are never sent to another site.
                            "same-origin", not "no-referrer": Flask-WTF checks
                            the Referer on HTTPS form posts, so it must still
                            be sent to ourselves.
  Permissions-Policy        No camera, microphone or location, ever.
  Strict-Transport-Security Production only: the browser refuses plain HTTP
                            for this site for two years.
  Cache-Control: no-store   Pages are never kept in the browser's or a proxy's
                            cache, so the Back button after signing out on a
                            shared computer shows nothing. Static files are
                            left cacheable.
"""

import logging

from flask import render_template, request

log = logging.getLogger(__name__)

CONTENT_SECURITY_POLICY = "; ".join(
    [
        "default-src 'self'",
        "script-src 'self'",
        "style-src 'self' 'unsafe-inline'",
        "img-src 'self' data:",
        "font-src 'self'",
        "connect-src 'self'",
        "form-action 'self'",
        "frame-ancestors 'none'",
        "base-uri 'self'",
        "object-src 'none'",
    ]
)

STANDARD_HEADERS = {
    "Content-Security-Policy": CONTENT_SECURITY_POLICY,
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
}

HSTS = "max-age=63072000"


def register_security(app):
    @app.after_request
    def add_security_headers(response):
        for name, value in STANDARD_HEADERS.items():
            response.headers.setdefault(name, value)
        if app.config.get("IS_PRODUCTION"):
            response.headers.setdefault("Strict-Transport-Security", HSTS)
        if request.endpoint != "static":
            response.headers["Cache-Control"] = "no-store"
        return response

    def error_page(status, template):
        def handler(error):
            return render_template(template), status
        return handler

    app.register_error_handler(403, error_page(403, "errors/403.html"))
    app.register_error_handler(404, error_page(404, "errors/404.html"))
    app.register_error_handler(429, error_page(429, "errors/429.html"))

    @app.errorhandler(500)
    def server_error(error):
        # The traceback goes to the log (Render keeps it); the person sees a
        # plain page. Never the traceback, never debug output.
        log.error("Unhandled error on %s %s", request.method, request.path)
        return render_template("errors/500.html"), 500
