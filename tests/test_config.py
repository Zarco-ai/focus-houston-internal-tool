
import pathlib

import pytest

from app import create_app
from app.config import (
    ConfigError,
    DevelopmentConfig,
    ProductionConfig,
    config_from_environment,
    normalize_database_url,
    require_tls,
)

GOOD_KEY = "k" * 40
RENDER_STYLE_URL = "postgresql://focus:pw@dpg-example-a/focus"
PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent


@pytest.fixture
def production_env(monkeypatch):
    for name in ("SECRET_KEY", "DATABASE_URL", "FOCUS_ENV", "ADMIN_EMAILS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "client-id.apps.googleusercontent.com")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "client-secret")
    return monkeypatch


def test_production_refuses_to_start_without_google_sign_in_credentials(production_env):
    production_env.setenv("SECRET_KEY", GOOD_KEY)
    production_env.setenv("DATABASE_URL", RENDER_STYLE_URL)
    production_env.delenv("GOOGLE_CLIENT_ID")
    production_env.delenv("GOOGLE_CLIENT_SECRET")
    with pytest.raises(ConfigError) as raised:
        create_app(ProductionConfig)
    assert "GOOGLE_CLIENT_ID" in str(raised.value)
    assert "GOOGLE_CLIENT_SECRET" in str(raised.value)


def test_production_session_cookie_is_secure_httponly_and_lax(production_env):
    production_env.setenv("SECRET_KEY", GOOD_KEY)
    production_env.setenv("DATABASE_URL", RENDER_STYLE_URL)
    app = create_app(ProductionConfig)

    assert app.config["SESSION_COOKIE_SECURE"] is True
    assert app.config["SESSION_COOKIE_HTTPONLY"] is True
    assert app.config["SESSION_COOKIE_SAMESITE"] == "Lax"
    assert app.config["SESSION_COOKIE_NAME"].startswith("__Host-")
    assert app.config["PERMANENT_SESSION_LIFETIME"].total_seconds() == 12 * 3600
    assert app.config["WTF_CSRF_TIME_LIMIT"] == 12 * 3600


def test_production_reads_admin_emails_lowercased(production_env):
    production_env.setenv("SECRET_KEY", GOOD_KEY)
    production_env.setenv("DATABASE_URL", RENDER_STYLE_URL)
    production_env.setenv("ADMIN_EMAILS", " First@Example.org, second@example.org ,")
    app = create_app(ProductionConfig)
    assert app.config["ADMIN_EMAILS"] == {"first@example.org", "second@example.org"}


def test_production_refuses_to_start_without_secret_key(production_env):
    production_env.setenv("DATABASE_URL", RENDER_STYLE_URL)
    with pytest.raises(ConfigError, match="SECRET_KEY"):
        create_app(ProductionConfig)


def test_production_refuses_to_start_without_database_url(production_env):
    production_env.setenv("SECRET_KEY", GOOD_KEY)
    with pytest.raises(ConfigError, match="DATABASE_URL"):
        create_app(ProductionConfig)


def test_production_names_both_missing_variables_at_once(production_env):
    with pytest.raises(ConfigError) as raised:
        create_app(ProductionConfig)
    assert "SECRET_KEY" in str(raised.value)
    assert "DATABASE_URL" in str(raised.value)


def test_production_refuses_a_short_secret_key(production_env):
    production_env.setenv("SECRET_KEY", "too-short")
    production_env.setenv("DATABASE_URL", RENDER_STYLE_URL)
    with pytest.raises(ConfigError, match="too short"):
        create_app(ProductionConfig)


def test_production_refuses_a_database_that_is_not_postgresql(production_env):
    production_env.setenv("SECRET_KEY", GOOD_KEY)
    production_env.setenv("DATABASE_URL", "sqlite:///focus.db")
    with pytest.raises(ConfigError, match="PostgreSQL"):
        create_app(ProductionConfig)


def test_production_never_generates_or_reads_a_key_file(production_env):
    import app as app_package

    def must_not_be_called(*args, **kwargs):
        raise AssertionError("production called resolve_secret_key()")

    production_env.setattr(app_package, "resolve_secret_key", must_not_be_called)
    production_env.setenv("SECRET_KEY", GOOD_KEY)
    production_env.setenv("DATABASE_URL", RENDER_STYLE_URL)

    app = create_app(ProductionConfig)
    assert app.config["SECRET_KEY"] == GOOD_KEY


def test_production_uses_the_environment_database_with_tls(production_env):
    production_env.setenv("SECRET_KEY", GOOD_KEY)
    production_env.setenv("DATABASE_URL", RENDER_STYLE_URL)
    app = create_app(ProductionConfig)

    uri = app.config["SQLALCHEMY_DATABASE_URI"]
    assert uri.startswith("postgresql+psycopg://")
    assert uri.endswith("sslmode=require")


def test_production_is_the_default_when_focus_env_is_unset(production_env):
    assert config_from_environment() is ProductionConfig

    production_env.setenv("FOCUS_ENV", "staging")
    assert config_from_environment() is ProductionConfig

    production_env.setenv("FOCUS_ENV", "development")
    assert config_from_environment() is DevelopmentConfig


def test_create_app_with_no_arguments_and_no_environment_refuses_to_start(production_env):
    with pytest.raises(ConfigError):
        create_app()


@pytest.mark.parametrize(
    "given, expected",
    [
        ("postgres://u:p@h/db", "postgresql+psycopg://u:p@h/db"),
        ("postgresql://u:p@h/db", "postgresql+psycopg://u:p@h/db"),
        ("postgresql+psycopg://u:p@h/db", "postgresql+psycopg://u:p@h/db"),
        ("  postgresql://u:p@h/db  ", "postgresql+psycopg://u:p@h/db"),
        (None, None),
    ],
)
def test_render_style_database_urls_are_given_the_psycopg_driver(given, expected):
    assert normalize_database_url(given) == expected


def test_tls_is_required_unless_the_url_already_says_otherwise():
    assert require_tls("postgresql+psycopg://h/db") == "postgresql+psycopg://h/db?sslmode=require"
    assert require_tls("postgresql+psycopg://h/db?a=1") == "postgresql+psycopg://h/db?a=1&sslmode=require"
    assert require_tls("postgresql+psycopg://h/db?sslmode=verify-full") == (
        "postgresql+psycopg://h/db?sslmode=verify-full"
    )


def test_the_schema_is_never_created_at_startup():
    for path in sorted((PROJECT_ROOT / "app").rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        assert ".create_all(" not in source, f"{path.relative_to(PROJECT_ROOT)} calls create_all()"
