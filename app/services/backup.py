
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
    pass


def _tables():
    return [t for t in db.metadata.sorted_tables]


_ALEMBIC_VERSION = sa.Table("alembic_version", sa.MetaData(),
                            sa.Column("version_num", sa.String(32)))


def current_revision():
    try:
        return db.session.execute(sa.select(_ALEMBIC_VERSION.c.version_num)).scalar()
    except sa.exc.SQLAlchemyError:
        db.session.rollback()
        return None


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


def create():
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
    return stream.isatty()


def dumps(document):
    return json.dumps(document, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def summary(document):
    return {name: len(table["rows"]) for name, table in document["tables"].items()}


def _has_data():
    for table in _tables():
        if db.session.execute(sa.select(sa.func.count()).select_from(table)).scalar():
            return table.name
    return None


def restore(document):
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
    for table in _tables():
        primary = list(table.primary_key.columns)
        if len(primary) != 1 or not isinstance(primary[0].type, sa.Integer):
            continue
        column = primary[0]
        next_id = sa.select(sa.func.coalesce(sa.func.max(column), 0) + 1).scalar_subquery()
        db.session.execute(sa.select(sa.func.setval(
            sa.func.pg_get_serial_sequence(table.name, column.name), next_id, False)))
