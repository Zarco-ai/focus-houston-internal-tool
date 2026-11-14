
import pathlib

import flask_migrate
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext

from app.extensions import db

VERSIONS = pathlib.Path(__file__).resolve().parent.parent / "migrations" / "versions"


def schema_differences():
    with db.engine.connect() as connection:
        context = MigrationContext.configure(connection, opts={"compare_type": True})
        return compare_metadata(context, db.metadata)


def test_the_migrations_produce_exactly_the_schema_the_models_describe(app):
    differences = schema_differences()
    assert differences == [], (
        "The models and the migrations disagree. Generate a migration with\n"
        "  flask --app run db migrate -m 'what changed'\n"
        f"Differences: {differences}"
    )


def test_every_migration_downgrades_and_upgrades_again(app):
    db.session.remove()
    flask_migrate.downgrade(revision="base")
    flask_migrate.upgrade()
    assert schema_differences() == []


def test_a_migration_never_imports_app_code():
    offenders = []
    for path in sorted(VERSIONS.glob("*.py")):
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith(("from app", "import app")) or "app.models" in stripped:
                offenders.append(f"{path.name}:{line_number}")
    assert not offenders, "migrations importing app code: " + ", ".join(offenders)


def test_there_is_exactly_one_head():
    from alembic.script import ScriptDirectory

    from app import MIGRATIONS_DIR
    from alembic.config import Config

    config = Config()
    config.set_main_option("script_location", MIGRATIONS_DIR)
    assert len(ScriptDirectory.from_config(config).get_heads()) == 1
