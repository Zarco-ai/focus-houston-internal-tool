"""Shared test setup.

The tests run against PostgreSQL -- the same database as production, so
time zones, constraint enforcement and case sensitivity behave the way they
will on Render. Start it first:

    docker compose up -d

Once per test run the test database is wiped and rebuilt by running every
migration, start to finish. That makes every test run a check that the
migrations work, and test_migrations.py checks they match the models.

Before each test every table is emptied (TRUNCATE ... RESTART IDENTITY), so
each test starts from an empty database with ids counting from 1 again.
`seeded_app` additionally loads the real catalog CSV, so the tests check the
actual data The Center Director will use rather than made-up fixtures.
"""

import flask_migrate
import pytest
import sqlalchemy as sa

from app import create_app
from app.auth import SESSION_SIGNED_IN_AT, SESSION_USER_ID
from app.config import TestConfig
from app.extensions import db
from app.models import ROLE_ADMIN, ROLE_STAFF, AppUser
from app.services import audit, clock, seeding

# Every test app has one signed-in admin. Service-level tests act as this
# person, so every audited change in the suite has a user behind it -- the
# same rule production enforces.
TEST_ADMIN_EMAIL = "admin@test.example"
TEST_STAFF_EMAIL = "staff@test.example"


class CsrfConfig(TestConfig):
    """Like the test config, but with CSRF protection switched back on."""

    WTF_CSRF_ENABLED = True


def empty_every_table():
    """Remove every row from every app table and restart the id sequences.

    This is the one raw SQL statement in the project, and it is here in the
    test setup, not in app/. The table names come from the app's own model
    metadata, never from input. tests/test_security.py keeps raw SQL out of
    the app itself.
    """
    names = ", ".join(f'"{table.name}"' for table in db.metadata.sorted_tables)
    db.session.execute(sa.text(f"TRUNCATE {names} RESTART IDENTITY CASCADE"))
    db.session.commit()


@pytest.fixture(scope="session")
def _test_database():
    """Wipe the test database and build its schema from the migrations, once
    per test run."""
    application = create_app(TestConfig)
    with application.app_context():
        try:
            with db.engine.connect():
                pass
        except sa.exc.OperationalError:
            pytest.exit(
                "Cannot reach the test database. Start it with:\n\n"
                "    docker compose up -d\n",
                returncode=2,
            )

        # Drop whatever is there, including tables from a migration on some
        # other branch that the models here do not know about.
        existing = sa.MetaData()
        existing.reflect(bind=db.engine)
        existing.drop_all(bind=db.engine)

        flask_migrate.upgrade()
        db.engine.dispose()
    yield


@pytest.fixture
def make_app(_test_database):
    """Build an app on the test database. Every app built here has its
    connection pool closed afterwards -- without that, a few hundred tests
    would leave a few hundred pools of open connections behind."""
    built = []

    def build(config=TestConfig):
        application = create_app(config)
        built.append(application)
        return application

    yield build

    for application in built:
        with application.app_context():
            db.session.remove()
            db.engine.dispose()


def make_user(email, role, active=True):
    user = AppUser(email=email, role=role, active=active)
    db.session.add(user)
    db.session.commit()
    return user


def sign_in(client, user):
    """Sign a test client in as `user`, exactly as a completed Google sign-in
    leaves the session. Every request still goes through the real guard."""
    with client.session_transaction() as session:
        session.clear()
        session[SESSION_USER_ID] = user.id
        session[SESSION_SIGNED_IN_AT] = clock.now_utc().timestamp()


@pytest.fixture
def sign_in_as():
    """The sign_in() helper, for tests that need several browsers."""
    return sign_in


@pytest.fixture
def app(make_app):
    """An empty app with empty tables, plus one admin user that service-level
    code acts as."""
    application = make_app()
    with application.app_context():
        empty_every_table()
        admin = make_user(TEST_ADMIN_EMAIL, ROLE_ADMIN)
        with audit.acting_as(admin.id):
            yield application
        db.session.remove()


@pytest.fixture
def admin_user(app):
    return AppUser.query.filter_by(email=TEST_ADMIN_EMAIL).one()


@pytest.fixture
def staff_user(app):
    return make_user(TEST_STAFF_EMAIL, ROLE_STAFF)


@pytest.fixture
def seeded_app(app):
    """The same app with the real 119-item catalog loaded."""
    seeding.seed_all(app.config["SEED_CSV_PATH"])
    return app


@pytest.fixture
def client(seeded_app, admin_user):
    """A browser signed in as the test admin."""
    test_client = seeded_app.test_client()
    sign_in(test_client, admin_user)
    return test_client


@pytest.fixture
def staff_client(seeded_app, staff_user):
    """A browser signed in as a staff member (not an admin)."""
    test_client = seeded_app.test_client()
    sign_in(test_client, staff_user)
    return test_client


@pytest.fixture
def anonymous_client(seeded_app):
    """A browser nobody has signed in on."""
    return seeded_app.test_client()


@pytest.fixture
def csrf_app(make_app):
    """The seeded app with CSRF protection on, for tests/test_security.py."""
    application = make_app(CsrfConfig)
    with application.app_context():
        empty_every_table()
        admin = make_user(TEST_ADMIN_EMAIL, ROLE_ADMIN)
        with audit.acting_as(admin.id):
            seeding.seed_all(application.config["SEED_CSV_PATH"])
            yield application
        db.session.remove()


@pytest.fixture
def csrf_client(csrf_app):
    """Signed in as the test admin, with CSRF protection on."""
    test_client = csrf_app.test_client()
    sign_in(test_client, AppUser.query.filter_by(email=TEST_ADMIN_EMAIL).one())
    return test_client


@pytest.fixture
def csv_path(app):
    return app.config["SEED_CSV_PATH"]


@pytest.fixture
def today():
    """Today in Houston -- the same answer the app gets."""
    return clock.local_today()
