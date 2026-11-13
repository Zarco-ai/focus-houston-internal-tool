"""Application configuration.

Three configurations, chosen by the FOCUS_ENV environment variable:

    FOCUS_ENV=development   run.py and seed.py on a laptop. Talks to the local
                            Docker database and, if no SECRET_KEY is set,
                            generates one into instance/ (see
                            resolve_secret_key() in app/__init__.py).
    anything else, or unset PRODUCTION. Every secret must come from the
                            environment, and the app refuses to start without
                            them.
    (tests)                 TestConfig, passed in directly by the tests.

Production is the default on purpose. If someone forgets to set FOCUS_ENV on
Render, the safe failure is "refuses to start, says why" -- not "quietly
generates a signing key on a disk Render wipes at every restart, logging
everyone out in the middle of a distribution day".

Nothing secret is written in this file.
"""

import datetime
import os

# The local Docker database from docker-compose.yml. Not a secret: it only
# exists on this laptop, bound to 127.0.0.1.
LOCAL_DEV_DATABASE_URL = "postgresql+psycopg://focus:focus-local-only@127.0.0.1:5433/focus_dev"
LOCAL_TEST_DATABASE_URL = "postgresql+psycopg://focus:focus-local-only@127.0.0.1:5433/focus_test"


class ConfigError(RuntimeError):
    """The app is misconfigured and must not start. The message says how to
    fix it, because whoever reads it will be looking at a Render deploy log."""


def normalize_database_url(url):
    """Make a PostgreSQL URL from Render (or anywhere) usable by SQLAlchemy.

    Render hands out URLs starting postgres:// or postgresql://. SQLAlchemy
    needs to be told which driver to use, and this app uses psycopg 3.
    """
    if url is None:
        return None
    url = url.strip()
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix):]
    return url


def require_tls(url):
    """Add sslmode=require unless the URL already says what it wants.

    Render encrypts external connections always, and internal ones only if
    asked. Asking costs nothing, and "require" means a misconfiguration fails
    instead of falling back to plaintext.
    """
    if "sslmode=" in url:
        return url
    return url + ("&" if "?" in url else "?") + "sslmode=require"


class Config:
    """What every environment shares."""

    SECRET_KEY = None
    SQLALCHEMY_DATABASE_URI = None
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    SQLALCHEMY_ENGINE_OPTIONS = {
        # A connection that died while idle (a database restart, a network
        # blip) is replaced instead of failing the next page load.
        "pool_pre_ping": True,
        # Every connection talks to PostgreSQL in UTC, whatever the server's
        # own setting. Times are stored as UTC and converted to Houston time
        # only when shown -- see app/services/clock.py.
        "connect_args": {"options": "-c timezone=UTC"},
    }

    # --- Sign-in sessions (docs/PRODUCTION_DESIGN.md 6.2) ---------------------
    # Sign in in the morning; it lasts the whole distribution day and expires
    # overnight. ABSOLUTE: counted from sign-in, not from the last click, so a
    # session always ends -- the guard in app/auth.py enforces it.
    SESSION_HOURS = 12
    PERMANENT_SESSION_LIFETIME = datetime.timedelta(hours=SESSION_HOURS)
    SESSION_COOKIE_HTTPONLY = True     # page scripts can't read the cookie
    SESSION_COOKIE_SAMESITE = "Lax"    # other sites can't send it with a POST
    SESSION_COOKIE_SECURE = False      # True in production (HTTPS only)
    SESSION_COOKIE_NAME = "focus_session"

    # A CSRF token lives exactly as long as the sign-in it belongs to. The
    # counting screen can sit open all day without its forms dying under her.
    WTF_CSRF_TIME_LIMIT = SESSION_HOURS * 3600

    # --- Google sign-in ------------------------------------------------------
    GOOGLE_CLIENT_ID = None
    GOOGLE_CLIENT_SECRET = None
    GOOGLE_DISCOVERY_URL = "https://accounts.google.com/.well-known/openid-configuration"
    # Emails made admin on their first sign-in, and only then. See
    # app/services/users.py.
    ADMIN_EMAILS = frozenset()

    # --- Rate limits -----------------------------------------------------------
    # In memory, which is exact because gunicorn runs ONE worker process (with
    # threads). If workers are ever increased, each keeps its own count and
    # the limits loosen -- move this to shared storage first.
    RATELIMIT_STORAGE_URI = "memory://"
    RATELIMIT_ENABLED = True
    RATELIMIT_HEADERS_ENABLED = True
    SIGN_IN_RATE_LIMIT = "10 per minute;60 per hour"
    # Sync now calls Google; a stuck finger or a script shouldn't.
    SYNC_RATE_LIMIT = "6 per minute;60 per hour"

    # --- Google Form sync (Phase 6) ---------------------------------------------
    # All three optional: without the Sheet ID and the key, the Responses
    # page says the sync isn't set up and everything else works. See
    # docs/GOOGLE_FORM_SETUP.md. The key is a secret: environment only,
    # never logged, never shown.
    FORM_SHEET_ID = None
    # Leave unset: the sync finds the form's tab itself (form_sync.choose_tab).
    FORM_SHEET_TAB = None
    GOOGLE_SERVICE_ACCOUNT_JSON = None
    # A page open triggers a sync if the last attempt is older than this.
    FORM_SYNC_STALE_MINUTES = 5

    # --- The report Sheet (D9-11) -----------------------------------------------
    # FOCUS's own monthly report Sheet, filled from Totals. Optional: without
    # it the button says it isn't set up. Same service account key as the
    # form sync, an EDITOR of this one Sheet. docs/GOOGLE_REPORT_SHEET_SETUP.md.
    REPORT_SHEET_ID = None
    REPORT_SHEET_TAB = None          # the template tab; unset means "Sheet1"
    REPORT_SHEET_RATE_LIMIT = "6 per minute;60 per hour"

    # Which code produced a filed report. Render sets RENDER_GIT_COMMIT on
    # every deploy; on a laptop it's "local".
    APP_VERSION = (os.environ.get("RENDER_GIT_COMMIT") or "local")[:12]

    # How many proxies in front of the app set X-Forwarded-Proto and -Host.
    # Render's is one. Too low and the app thinks requests are plain http,
    # so Google rejects the http:// return address and nobody can sign in.
    PROXY_HOPS = 1

    # A request header that names the visitor, set by a proxy we trust. None
    # means "use the address ProxyFix found", which is right on a laptop.
    # Production sets it -- see ProductionConfig.
    CLIENT_IP_HEADER = None

    IS_PRODUCTION = False

    # Where seed.py reads the catalog from: a made-up sample catalog.
    SEED_CSV_PATH = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "data", "sample_catalog.csv",
    )


def parse_admin_emails(raw):
    """The ADMIN_EMAILS environment variable: comma-separated emails,
    compared lowercased (Google treats addresses case-insensitively)."""
    return frozenset(
        part.strip().lower() for part in (raw or "").split(",") if part.strip()
    )


def _admin_emails_from_environment():
    return parse_admin_emails(os.environ.get("ADMIN_EMAILS"))


class DevelopmentConfig(Config):
    SQLALCHEMY_DATABASE_URI = normalize_database_url(
        os.environ.get("DATABASE_URL", LOCAL_DEV_DATABASE_URL)
    )
    # Optional on a laptop: without them the sign-in page explains what to
    # set up. See docs/GOOGLE_SIGN_IN_SETUP.md.
    GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID") or None
    GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET") or None
    ADMIN_EMAILS = _admin_emails_from_environment()
    FORM_SHEET_ID = os.environ.get("FORM_SHEET_ID") or None
    FORM_SHEET_TAB = os.environ.get("FORM_SHEET_TAB") or None
    GOOGLE_SERVICE_ACCOUNT_JSON = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON") or None
    REPORT_SHEET_ID = os.environ.get("REPORT_SHEET_ID") or None
    REPORT_SHEET_TAB = os.environ.get("REPORT_SHEET_TAB") or None


class ProductionConfig(Config):
    """Everything comes from the environment. validate() is called by
    create_app() before anything else happens."""

    IS_PRODUCTION = True

    # Render puts Cloudflare in front of every service. Counting entries in
    # X-Forwarded-For picked a Cloudflare address on staging (a different one
    # almost every request), so each visitor's sign-in attempts were spread
    # over many counters. Cloudflare sets CF-Connecting-IP to the address that
    # connected to it and overwrites any value the visitor sends, so it names
    # the visitor however many proxies sit behind it. Found on staging (Phase 2).
    CLIENT_IP_HEADER = "CF-Connecting-IP"

    @classmethod
    def load(cls):
        """Read the environment now, not at import time, so a test can set
        variables and then build a production app."""
        secret_key = os.environ.get("SECRET_KEY", "").strip()
        database_url = normalize_database_url(os.environ.get("DATABASE_URL"))
        google_client_id = os.environ.get("GOOGLE_CLIENT_ID", "").strip()
        google_client_secret = os.environ.get("GOOGLE_CLIENT_SECRET", "").strip()

        missing = []
        if not secret_key:
            missing.append("SECRET_KEY")
        if not database_url:
            missing.append("DATABASE_URL")
        # Without these nobody can sign in at all, so it is better to refuse
        # to start than to come up looking healthy and lock everyone out.
        if not google_client_id:
            missing.append("GOOGLE_CLIENT_ID")
        if not google_client_secret:
            missing.append("GOOGLE_CLIENT_SECRET")
        if missing:
            raise ConfigError(
                "Refusing to start in production: "
                + ", ".join(missing)
                + " must be set as environment variables (in Render: the "
                "service's Environment tab). If this is a laptop, set "
                "FOCUS_ENV=development."
            )
        if len(secret_key) < 32:
            raise ConfigError(
                "Refusing to start in production: SECRET_KEY is too short. "
                "Use at least 32 random characters -- Render's 'Generate' "
                "button for environment variables does this."
            )
        if not database_url.startswith("postgresql+psycopg://"):
            raise ConfigError(
                "Refusing to start in production: DATABASE_URL must be a "
                "PostgreSQL URL."
            )

        return {
            "SECRET_KEY": secret_key,
            "SQLALCHEMY_DATABASE_URI": require_tls(database_url),
            "GOOGLE_CLIENT_ID": google_client_id,
            "GOOGLE_CLIENT_SECRET": google_client_secret,
            "ADMIN_EMAILS": _admin_emails_from_environment(),
            "FORM_SHEET_ID": os.environ.get("FORM_SHEET_ID", "").strip() or None,
            "FORM_SHEET_TAB": os.environ.get("FORM_SHEET_TAB", "").strip() or None,
            "GOOGLE_SERVICE_ACCOUNT_JSON": os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip() or None,
            "REPORT_SHEET_ID": os.environ.get("REPORT_SHEET_ID", "").strip() or None,
            "REPORT_SHEET_TAB": os.environ.get("REPORT_SHEET_TAB", "").strip() or None,
            # HTTPS only. The __Host- prefix makes the browser itself refuse
            # the cookie unless it is Secure, has no Domain and Path is /.
            "SESSION_COOKIE_SECURE": True,
            "SESSION_COOKIE_NAME": "__Host-focus",
            "PREFERRED_URL_SCHEME": "https",
        }


class TestConfig(Config):
    TESTING = True

    # A separate database from development, emptied between every test. Can
    # be pointed elsewhere (for CI) with TEST_DATABASE_URL.
    SQLALCHEMY_DATABASE_URI = normalize_database_url(
        os.environ.get("TEST_DATABASE_URL", LOCAL_TEST_DATABASE_URL)
    )

    # Fixed so tests are repeatable, and so the test run never writes a key
    # file. This value is never used by the real app.
    SECRET_KEY = "testing-only-not-a-real-key"  # nosec B105

    # Off by default so the existing tests can post forms directly. The tests
    # that specifically prove CSRF works turn it back on -- see
    # tests/test_security.py.
    WTF_CSRF_ENABLED = False

    # Off so a test suite that signs in hundreds of times is not throttled.
    # tests/test_auth.py turns it on to prove the limit works.
    RATELIMIT_ENABLED = False

    # Fake, so the sign-in route is "configured". Tests never talk to Google:
    # they replace the token exchange (tests/test_auth.py).
    GOOGLE_CLIENT_ID = "test-client-id"
    GOOGLE_CLIENT_SECRET = "test-client-secret"  # nosec B105


def config_from_environment():
    """Which configuration FOCUS_ENV asks for. Production unless explicitly
    told otherwise -- see the module docstring for why."""
    if os.environ.get("FOCUS_ENV", "").strip().lower() == "development":
        return DevelopmentConfig
    return ProductionConfig
