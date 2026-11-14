
import datetime
import os

LOCAL_DEV_DATABASE_URL = "postgresql+psycopg://focus:focus-local-only@127.0.0.1:5433/focus_dev"
LOCAL_TEST_DATABASE_URL = "postgresql+psycopg://focus:focus-local-only@127.0.0.1:5433/focus_test"


class ConfigError(RuntimeError):
    pass


def normalize_database_url(url):
    if url is None:
        return None
    url = url.strip()
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix):]
    return url


def require_tls(url):
    if "sslmode=" in url:
        return url
    return url + ("&" if "?" in url else "?") + "sslmode=require"


class Config:

    SECRET_KEY = None
    SQLALCHEMY_DATABASE_URI = None
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    SQLALCHEMY_ENGINE_OPTIONS = {
        "pool_pre_ping": True,
        "connect_args": {"options": "-c timezone=UTC"},
    }

    SESSION_HOURS = 12
    PERMANENT_SESSION_LIFETIME = datetime.timedelta(hours=SESSION_HOURS)
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    SESSION_COOKIE_SECURE = False
    SESSION_COOKIE_NAME = "focus_session"

    WTF_CSRF_TIME_LIMIT = SESSION_HOURS * 3600

    GOOGLE_CLIENT_ID = None
    GOOGLE_CLIENT_SECRET = None
    GOOGLE_DISCOVERY_URL = "https://accounts.google.com/.well-known/openid-configuration"
    ADMIN_EMAILS = frozenset()

    RATELIMIT_STORAGE_URI = "memory://"
    RATELIMIT_ENABLED = True
    RATELIMIT_HEADERS_ENABLED = True
    SIGN_IN_RATE_LIMIT = "10 per minute;60 per hour"
    SYNC_RATE_LIMIT = "6 per minute;60 per hour"

    FORM_SHEET_ID = None
    FORM_SHEET_TAB = None
    GOOGLE_SERVICE_ACCOUNT_JSON = None
    FORM_SYNC_STALE_MINUTES = 5

    REPORT_SHEET_ID = None
    REPORT_SHEET_TAB = None
    REPORT_SHEET_RATE_LIMIT = "6 per minute;60 per hour"

    APP_VERSION = (os.environ.get("RENDER_GIT_COMMIT") or "local")[:12]

    PROXY_HOPS = 1

    CLIENT_IP_HEADER = None

    IS_PRODUCTION = False

    SEED_CSV_PATH = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "data", "sample_catalog.csv",
    )


def parse_admin_emails(raw):
    return frozenset(
        part.strip().lower() for part in (raw or "").split(",") if part.strip()
    )


def _admin_emails_from_environment():
    return parse_admin_emails(os.environ.get("ADMIN_EMAILS"))


class DevelopmentConfig(Config):
    SQLALCHEMY_DATABASE_URI = normalize_database_url(
        os.environ.get("DATABASE_URL", LOCAL_DEV_DATABASE_URL)
    )
    GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID") or None
    GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET") or None
    ADMIN_EMAILS = _admin_emails_from_environment()
    FORM_SHEET_ID = os.environ.get("FORM_SHEET_ID") or None
    FORM_SHEET_TAB = os.environ.get("FORM_SHEET_TAB") or None
    GOOGLE_SERVICE_ACCOUNT_JSON = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON") or None
    REPORT_SHEET_ID = os.environ.get("REPORT_SHEET_ID") or None
    REPORT_SHEET_TAB = os.environ.get("REPORT_SHEET_TAB") or None


class ProductionConfig(Config):

    IS_PRODUCTION = True

    CLIENT_IP_HEADER = "CF-Connecting-IP"

    @classmethod
    def load(cls):
        secret_key = os.environ.get("SECRET_KEY", "").strip()
        database_url = normalize_database_url(os.environ.get("DATABASE_URL"))
        google_client_id = os.environ.get("GOOGLE_CLIENT_ID", "").strip()
        google_client_secret = os.environ.get("GOOGLE_CLIENT_SECRET", "").strip()

        missing = []
        if not secret_key:
            missing.append("SECRET_KEY")
        if not database_url:
            missing.append("DATABASE_URL")
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
            "SESSION_COOKIE_SECURE": True,
            "SESSION_COOKIE_NAME": "__Host-focus",
            "PREFERRED_URL_SCHEME": "https",
        }


class TestConfig(Config):
    TESTING = True

    SQLALCHEMY_DATABASE_URI = normalize_database_url(
        os.environ.get("TEST_DATABASE_URL", LOCAL_TEST_DATABASE_URL)
    )

    SECRET_KEY = "testing-only-not-a-real-key"  # nosec B105

    WTF_CSRF_ENABLED = False

    RATELIMIT_ENABLED = False

    GOOGLE_CLIENT_ID = "test-client-id"
    GOOGLE_CLIENT_SECRET = "test-client-secret"  # nosec B105


def config_from_environment():
    if os.environ.get("FOCUS_ENV", "").strip().lower() == "development":
        return DevelopmentConfig
    return ProductionConfig
