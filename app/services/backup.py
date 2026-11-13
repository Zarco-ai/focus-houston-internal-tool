"""Backing the database up to one file, and restoring it.

Render's paid PostgreSQL keeps 3 days of point-in-time recovery (design doc
10.3). That covers "yesterday's mistake", not "last month's". This is the
longer safety net: a weekly file FOCUS keeps somewhere it controls.

Why the app does this itself, rather than pg_dump: pg_dump is a separate
program that has to match the server's PostgreSQL version and be installed
wherever the backup runs. This needs only the app, so it works the same
from Render's shell, a laptop, or a test -- and tests/test_backup.py proves
a full round trip.

  create   every row of every app table, plus the migration revision, as
           one JSON document. Written to standard output only, so it goes
           straight into an encryption tool (docs/RUNBOOK.md):

               flask --app wsgi backup create | age -r "$FOCUS_BACKUP_KEY" > focus.json.age

           It refuses to print to a terminal: the backup holds card
           numbers, survey answers and the audit log.

  restore  reads such a document (decrypted on the way in) into a database
           at the SAME migration revision that has NO data in it. It never
           merges into, or overwrites, a database in use.

Values are written with type tags ({"$date": ...}, {"$decimal": ...}) so
they come back as exactly the same values. Money, as everywhere, never
passes through a float.
"""

import base64
import datetime
import json
from decimal import Decimal

import sqlalchemy as sa

from app.extensions import db
from app.services import clock

FORMAT = "focus-backup"
FORMAT_VERSION = 1


class BackupError(Exception):
    """The backup can't be made or restored. The message says why."""


def _tables():
    """App tables in an order that satisfies foreign keys when inserting."""
    return [t for t in db.metadata.sorted_tables]


# Alembic's own one-row table, described here so it can be read without raw
# SQL (tests/test_security.py keeps raw SQL out of app/).
_ALEMBIC_VERSION = sa.Table("alembic_version", sa.MetaData(),
                            sa.Column("version_num", sa.String(32)))


def current_revision():
    try:
        return db.session.execute(sa.select(_ALEMBIC_VERSION.c.version_num)).scalar()
    except sa.exc.SQLAlchemyError:
        db.session.rollback()
        return None


# --- Encoding --------------------------------------------------------------------------


def _encode(value):
    if isinstance(value, datetime.datetime):
        return {"$datetime": value.isoformat()}
    if isinstance(value, datetime.date):
        return {"$date": value.isoformat()}
    if isinstance(value, Decimal):
        return {"$decimal": str(value)}
    if isinstance(value, bytes):
        return {"$bytes": base64.b64encode(value).decode("ascii")}
    if isinstance(value, (dict, list)):
        return {"$json": value}
    return value


def _decode(value):
    if isinstance(value, dict) and len(value) == 1:
        (tag, raw), = value.items()
        if tag == "$datetime":
            return datetime.datetime.fromisoformat(raw)
        if tag == "$date":
            return datetime.date.fromisoformat(raw)
        if tag == "$decimal":
            return Decimal(raw)
        if tag == "$bytes":
            return base64.b64decode(raw)
        if tag == "$json":
            return raw
    return value


# --- Create ---------------------------------------------------------------------------------


def create():
    """The whole database as a dict, ready for json.dumps."""
    revision = current_revision()
    if revision is None:
        raise BackupError("This database has no migration revision; is it the app's database?")
    tables = {}
    for table in _tables():
        columns = [c.name for c in table.columns]
        order = [c for c in table.primary_key.columns] or list(table.columns)
        rows = db.session.execute(sa.select(table).order_by(*order)).all()
        tables[table.name] = {
            "columns": columns,
            "rows": [[_encode(row._mapping[c]) for c in columns] for row in rows],
        }
    return {
        "format": FORMAT,
        "format_version": FORMAT_VERSION,
        "created_at": clock.now_utc().isoformat(),
        "alembic_revision": revision,
        "tables": tables,
    }


def output_is_terminal(stream):
    """True if the backup would be printed on a screen."""
    return stream.isatty()


def dumps(document):
    return json.dumps(document, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def summary(document):
    """How many rows of each table: printed by restore, never the rows."""
    return {name: len(table["rows"]) for name, table in document["tables"].items()}


# --- Restore --------------------------------------------------------------------------------


def _has_data():
    for table in _tables():
        if db.session.execute(sa.select(sa.func.count()).select_from(table)).scalar():
            return table.name
    return None


def restore(document):
    """Load a backup into an empty database at the same revision. All or
    nothing: one transaction, committed only if every row goes in."""
    if not isinstance(document, dict) or document.get("format") != FORMAT:
        raise BackupError("That isn't a FOCUS backup file.")
    if document.get("format_version") != FORMAT_VERSION:
        raise BackupError(f"Backup format {document.get('format_version')} isn't one this "
                          f"version of the app reads ({FORMAT_VERSION}).")

    revision = current_revision()
    if revision != document.get("alembic_revision"):
        raise BackupError(
            f"The database is at migration {revision}; the backup was made at "
            f"{document.get('alembic_revision')}. Bring an empty database to that revision "
            f"first: flask --app wsgi db upgrade {document.get('alembic_revision')}")
    busy = _has_data()
    if busy:
        raise BackupError(f"The database isn't empty ({busy} has rows). Restore only into a new, "
                          "empty database, then point the app at it (docs/RUNBOOK.md).")

    known = {t.name: t for t in _tables()}
    unknown = set(document["tables"]) - set(known)
    if unknown:
        raise BackupError(f"The backup has tables this app doesn't: {', '.join(sorted(unknown))}.")

    try:
        for table in _tables():
            data = document["tables"].get(table.name)
            if not data or not data["rows"]:
                continue
            columns = data["columns"]
            rows = [dict(zip(columns, (_decode(v) for v in row))) for row in data["rows"]]
            db.session.execute(table.insert(), rows)
        _reset_sequences()
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise
    return summary(document)


def _reset_sequences():
    """Make each table's next id follow on from the restored rows, so the
    next batch isn't id 1 colliding with a restored batch 1."""
    for table in _tables():
        primary = list(table.primary_key.columns)
        if len(primary) != 1 or not isinstance(primary[0].type, sa.Integer):
            continue
        column = primary[0]
        next_id = sa.select(sa.func.coalesce(sa.func.max(column), 0) + 1).scalar_subquery()
        db.session.execute(sa.select(sa.func.setval(
            sa.func.pg_get_serial_sequence(table.name, column.name), next_id, False)))
