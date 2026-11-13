"""The kinds of session a batch can be: Diaper Distribution, ESL Class, ...

Admins add, rename and switch these off from Settings (Phase 0: "Admins add
and rename them"). The program director's sheet has about fifteen kinds of event, and a
new one should be a form, not a developer.

A service type is never deleted. Approved batches point at it, and the
monthly report reads its name, so switching one off only stops it being
offered for new batches. Renaming one renames it everywhere, past batches
included -- it is the same kind of session with a better name. Every change
is audited.
"""

from app.extensions import db
from app.models import ServiceType
from app.models.audit import ACTION_CREATE
from app.services import audit

MAX_NAME = 100


def all_service_types():
    """Active first, then switched off; each A-Z."""
    return ServiceType.query.order_by(ServiceType.active.desc(), ServiceType.name).all()


def active_service_types():
    return ServiceType.query.filter_by(active=True).order_by(ServiceType.name).all()


def choices_for(batch):
    """The dropdown for one batch: every active type, plus the batch's own
    type if it has since been switched off, so the page still shows it."""
    choices = active_service_types()
    current = batch.service_type
    if current is not None and not current.active:
        choices = choices + [current]
    return choices


def can_assign(batch, service_type):
    """A switched-off type can't be picked for a batch -- unless it is the
    one the batch already has."""
    return service_type.active or service_type.id == batch.service_type_id


def _clean_name(name, exclude_id=None):
    name = " ".join((name or "").split())
    if not name:
        raise ValueError("A service type needs a name.")
    if len(name) > MAX_NAME:
        raise ValueError(f"A service type name can be at most {MAX_NAME} characters.")
    clash = ServiceType.query.filter(
        db.func.lower(ServiceType.name) == name.lower(),
        ServiceType.id != (exclude_id or 0),
    ).first()
    if clash is not None:
        state = "" if clash.active else " (switched off -- switch it back on instead)"
        raise ValueError(f"There is already a service type called '{clash.name}'{state}.")
    return name


def create(name):
    service_type = ServiceType(name=_clean_name(name), active=True)
    db.session.add(service_type)
    db.session.flush()
    audit.record_change(
        table_name="service_type", record_id=service_type.id, field="name",
        old_value=None, new_value=service_type.name,
        description=f"Service type {service_type.name} added", action=ACTION_CREATE,
    )
    return service_type


def rename(service_type, name):
    new_name = _clean_name(name, exclude_id=service_type.id)
    changed = audit.apply_change(
        service_type, "name", new_name, table_name="service_type",
        description="Service type renamed",
    )
    db.session.flush()
    return changed


def set_active(service_type, active):
    changed = audit.apply_change(
        service_type, "active", bool(active), table_name="service_type",
        description=f"Service type {service_type.name} switched {'on' if active else 'off'}",
    )
    db.session.flush()
    return changed
