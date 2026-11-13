"""The extension objects, on their own so models and the app factory can both
import them without importing each other."""

from authlib.integrations.flask_client import OAuth
from flask import current_app, request
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_migrate import Migrate
from flask_sqlalchemy import SQLAlchemy
from flask_wtf.csrf import CSRFProtect

db = SQLAlchemy()

# Rejects any POST that does not carry a valid token. Every form in
# app/templates includes one; the counting screen sends it as an X-CSRFToken
# header from entry.js.
csrf = CSRFProtect()

# Alembic, through Flask-Migrate. The schema is changed only by migrations in
# migrations/versions/, applied with `flask db upgrade` as a deploy step.
migrate = Migrate()

# "Sign in with Google" (OpenID Connect). Authlib does the whole handshake,
# including checking the ID token's signature against Google's published
# keys, its issuer, audience, expiry and nonce. See app/views/auth.py.
oauth = OAuth()

# Limits on the sign-in routes (and, from Phase 6, "Sync now"). Keyed on the
# visitor's address: Cloudflare's CF-Connecting-IP in production, the plain
# connecting address on a laptop. See CLIENT_IP_HEADER in config.py.
# No default limits: only routes that ask for one get one.
def visitor_address():
    header = current_app.config["CLIENT_IP_HEADER"]
    return (header and request.headers.get(header)) or get_remote_address()


limiter = Limiter(key_func=visitor_address)
