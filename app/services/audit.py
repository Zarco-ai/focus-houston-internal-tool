
import contextlib
import contextvars

from app.extensions import db
from app.models import AuditLog
from app.models.audit import (
    ACTION_CREATE,
    ACTION_IMPORT,
    ACTION_UPDATE,
    ACTIONS,
    SYSTEM_ACTIONS,
)

current_actor = contextvars.ContextVar("current_actor", default=None)


class NoActor(RuntimeError):
    pass


@contextlib.contextmanager
def acting_as(user_id):
    token = current_actor.set(user_id)
    try:
        yield
    finally:
        current_actor.reset(token)


def _actor_for(action):
    user_id = current_actor.get()
    if user_id is None and action not in SYSTEM_ACTIONS:
        raise NoActor(
            f"Refusing to write a '{action}' audit row with nobody signed in. "
            "Changes to recorded data must say who made them."
        )
    return user_id


def _as_text(value):
    if value is None:
        return None
    if isinstance(value, bool):
        return "yes" if value else "no"
    return str(value)


def record_change(table_name, record_id, field, old_value, new_value,
                  description=None, action=ACTION_UPDATE):
    if action not in ACTIONS:
        raise ValueError(f"Unknown audit action: {action!r}")

    old_text = _as_text(old_value)
    new_text = _as_text(new_value)

    if old_text == new_text:
        return None

    entry = AuditLog(
        table_name=table_name,
        record_id=record_id,
        field=field,
        old_value=old_text,
        new_value=new_text,
        description=description,
        action=action,
        user_id=_actor_for(action),
    )
    db.session.add(entry)
    return entry


def record_event(action, table_name, record_id, description, field="event"):
    if action not in ACTIONS:
        raise ValueError(f"Unknown audit action: {action!r}")

    entry = AuditLog(
        table_name=table_name,
        record_id=record_id,
        field=field,
        old_value=None,
        new_value=None,
        description=description,
        action=action,
        user_id=_actor_for(action),
    )
    db.session.add(entry)
    return entry


def apply_change(record, field, new_value, table_name, description=None,
                 action=ACTION_UPDATE):
    old_value = getattr(record, field)
    entry = record_change(
        table_name=table_name,
        record_id=record.id,
        field=field,
        old_value=old_value,
        new_value=new_value,
        description=description,
        action=action,
    )
    if entry is None:
        return False
    setattr(record, field, new_value)
    return True


def history_for(table_name, record_id):
    return (
        AuditLog.query.filter_by(table_name=table_name, record_id=record_id)
        .order_by(AuditLog.changed_at.desc(), AuditLog.id.desc())
        .all()
    )


def recent_changes_for_table(table_name, limit=50):
    return (
        AuditLog.query.filter_by(table_name=table_name)
        .order_by(AuditLog.changed_at.desc(), AuditLog.id.desc())
        .limit(limit)
        .all()
    )


def recent_changes(limit=100):
    return (
        AuditLog.query.order_by(AuditLog.changed_at.desc(), AuditLog.id.desc())
        .limit(limit)
        .all()
    )


__all__ = [
    "ACTION_CREATE",
    "NoActor",
    "acting_as",
    "apply_change",
    "current_actor",
    "history_for",
    "recent_changes",
    "recent_changes_for_table",
    "record_change",
    "record_event",
]
