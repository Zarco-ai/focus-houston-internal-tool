"""Writing to the audit log.

Call record_change() whenever something that has already been committed gets
edited, and whenever the catalog changes. Draft batches are not audited --
they are still being written, and logging every tap of the + button would bury
the changes that matter. record_event() is for things that are not a field
changing, like a sign-in.

Every row records WHO. That person comes from `current_actor`, which is set
once per request by the sign-in guard (app/auth.py), and by a command or a
test with acting_as(). Services never take a `user` argument and never
import Flask: the actor travels with the request instead of through every
function signature.

Writing a row with no actor is refused -- unless it is something the app
does by itself (a scheduled sync, creating an admin from ADMIN_EMAILS). A
change nobody can be held to account for is exactly what the log exists to
prevent.
"""

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

# The id of the person making changes right now, or None. A ContextVar rather
# than a plain global because gunicorn serves several requests at once on
# separate threads, and each must see only its own user.
current_actor = contextvars.ContextVar("current_actor", default=None)


class NoActor(RuntimeError):
    """An audited change was attempted with nobody signed in."""


@contextlib.contextmanager
def acting_as(user_id):
    """Make changes as this user for the length of a `with` block.

    For commands and tests. Requests get their actor from the sign-in guard.
    """
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
    """Everything in the log is stored as readable text."""
    if value is None:
        return None
    if isinstance(value, bool):
        return "yes" if value else "no"
    return str(value)


def record_change(table_name, record_id, field, old_value, new_value,
                  description=None, action=ACTION_UPDATE):
    """Log one field changing on one record. Returns None if nothing moved."""
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
    """Log something that happened rather than a field that changed -- a
    sign-in, an export. Always written, even though nothing "moved"."""
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
    """Set a field on a record and log the change in one step.

    Returns True if the value actually changed.
    """
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
    """Every logged change to one record, newest first."""
    return (
        AuditLog.query.filter_by(table_name=table_name, record_id=record_id)
        .order_by(AuditLog.changed_at.desc(), AuditLog.id.desc())
        .all()
    )


def recent_changes_for_table(table_name, limit=50):
    """Newest-first changes to one kind of record -- e.g. every user change,
    for the Users & Access page."""
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
