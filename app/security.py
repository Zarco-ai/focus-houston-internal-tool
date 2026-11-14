
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
        log.error("Unhandled error on %s %s", request.method, request.path)
        return render_template("errors/500.html"), 500
