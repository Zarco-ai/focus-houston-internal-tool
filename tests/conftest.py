
import flask_migrate
import pytest
import sqlalchemy as sa

from app import create_app
from app.auth import SESSION_SIGNED_IN_AT, SESSION_USER_ID
from app.config import TestConfig
from app.extensions import db
from app.models import ROLE_ADMIN, ROLE_STAFF, AppUser
from app.services import audit, clock, seeding

TEST_ADMIN_EMAIL = "admin@test.example"
TEST_STAFF_EMAIL = "staff@test.example"


class CsrfConfig(TestConfig):

    WTF_CSRF_ENABLED = True


def empty_every_table():
    names = ", ".join(f'"{table.name}"' for table in db.metadata.sorted_tables)
    db.session.execute(sa.text(f"TRUNCATE {names} RESTART IDENTITY CASCADE"))
    db.session.commit()


@pytest.fixture(scope="session")
def _test_database():
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

        existing = sa.MetaData()
        existing.reflect(bind=db.engine)
        existing.drop_all(bind=db.engine)

        flask_migrate.upgrade()
        db.engine.dispose()
    yield


@pytest.fixture
def make_app(_test_database):
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
    with client.session_transaction() as session:
        session.clear()
        session[SESSION_USER_ID] = user.id
        session[SESSION_SIGNED_IN_AT] = clock.now_utc().timestamp()


@pytest.fixture
def sign_in_as():
    return sign_in


@pytest.fixture
def app(make_app):
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
    seeding.seed_all(app.config["SEED_CSV_PATH"])
    return app


@pytest.fixture
def client(seeded_app, admin_user):
    test_client = seeded_app.test_client()
    sign_in(test_client, admin_user)
    return test_client


@pytest.fixture
def staff_client(seeded_app, staff_user):
    test_client = seeded_app.test_client()
    sign_in(test_client, staff_user)
    return test_client


@pytest.fixture
def anonymous_client(seeded_app):
    return seeded_app.test_client()


@pytest.fixture
def csrf_app(make_app):
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
    test_client = csrf_app.test_client()
    sign_in(test_client, AppUser.query.filter_by(email=TEST_ADMIN_EMAIL).one())
    return test_client


@pytest.fixture
def csv_path(app):
    return app.config["SEED_CSV_PATH"]


@pytest.fixture
def today():
    return clock.local_today()
