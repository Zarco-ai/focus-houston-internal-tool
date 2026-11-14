
from app.constants import (
    COMMIT_MODE_AUTO,
    CONDITION_NEW,
    CONDITIONS,
    REPORT_BUCKETS,
    STATUS_COMMITTED,
    STATUS_DRAFT,
)
from app.extensions import db
from app.models import (
    Attendance,
    Batch,
    Item,
    LineItem,
    ServiceType,
    get_commit_mode,
)
from app.models.money import to_money
from app.services import audit, clock, valuation


def parse_used_rate(value):
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    percent = text.endswith("%")
    try:
        rate = float(text.rstrip("%").strip())
    except ValueError:
        raise ValueError(f"'{text}' isn't a used rate. Type it like 0.75 or 75%.")
    if percent:
        rate /= 100
    if not 0 < rate <= 1:
        raise ValueError(f"A used rate must be more than 0 and at most 1 (100%); got '{text}'.")
    return rate


class BatchNotReady(Exception):

    def __init__(self, reasons):
        self.reasons = reasons
        super().__init__("; ".join(reasons))


def open_batch(date=None, service_type_id=None, note=None):
    batch = Batch(
        date=date or clock.local_today(),
        service_type_id=service_type_id,
        note=note,
        status=STATUS_DRAFT,
    )
    db.session.add(batch)
    db.session.flush()
    return batch


def draft_batches():
    return (
        Batch.query.filter_by(status=STATUS_DRAFT)
        .order_by(Batch.date.desc(), Batch.id.desc())
        .all()
    )


def stale_draft_batches():
    return [batch for batch in draft_batches() if batch.is_stale]


def todays_working_batch():
    today = clock.local_today()

    if get_commit_mode() == COMMIT_MODE_AUTO:
        batch = (
            Batch.query.filter_by(date=today)
            .order_by(Batch.id.desc())
            .first()
        )
        if batch is not None:
            return batch
    else:
        batch = (
            Batch.query.filter_by(date=today, status=STATUS_DRAFT)
            .order_by(Batch.id.desc())
            .first()
        )
        if batch is not None:
            return batch

    return None


def get_or_start_todays_batch():
    return todays_working_batch() or open_batch()


def sync_review_flag(line):
    if line.is_custom:
        return False

    if line.needs_price:
        if line.needs_review or not line.batch.is_committed:
            return False
        line.needs_review = True
        line.reviewed_at = None
        return True

    if not line.needs_review:
        return False

    line.needs_review = False
    line.reviewed_at = clock.now_utc()
    return True


def lines_for(batch, item_id, condition):
    matches = [
        line
        for line in batch.lines
        if line.item_id == item_id and line.condition == condition
    ]
    matches.sort(key=lambda line: (line.id is None, line.id or 0))
    return matches


def find_line(batch, item_id, condition):
    matches = lines_for(batch, item_id, condition)
    return matches[0] if matches else None


def lines_for_item(batch, item_id):
    found = {}
    for line in batch.lines:
        if line.item_id == item_id:
            found.setdefault(line.condition, []).append(line)
    return found


def add_price_group(batch, item, condition=None, quantity=1):
    _prepare_for_edit(batch)

    condition = condition or CONDITION_NEW
    if condition not in CONDITIONS:
        raise ValueError(f"Unknown condition: {condition!r}")

    if not item.needs_manual_price:
        raise ValueError(
            f"{item.display_name} has a set price, so use the quantity instead "
            f"of adding a second price."
        )

    line = LineItem(
        batch=batch,
        item_id=item.id,
        quantity=valuation.clamp_quantity(quantity),
        condition=condition,
        used_multiplier_at_time=item.used_multiplier,
        unit_price_at_time=None,
    )
    db.session.add(line)
    line.recalculate()
    db.session.flush()
    return line


def set_line(batch, item, quantity=None, condition=None, unit_price=None):
    _prepare_for_edit(batch)

    condition = condition or CONDITION_NEW
    if condition not in CONDITIONS:
        raise ValueError(f"Unknown condition: {condition!r}")

    line = find_line(batch, item.id, condition)
    if line is None:
        line = LineItem(
            batch=batch,
            item_id=item.id,
            quantity=0,
            condition=condition,
        )
        db.session.add(line)

    if quantity is not None:
        line.quantity = valuation.clamp_quantity(quantity)

    if not line.used_rate_typed:
        line.used_multiplier_at_time = item.used_multiplier

    if line.price_typed:
        pass
    elif item.needs_manual_price:
        if unit_price is not None:
            line.unit_price_at_time = to_money(unit_price)
    else:
        line.unit_price_at_time = item.current_price()

    line.recalculate()
    sync_review_flag(line)
    db.session.flush()
    return line


def update_draft_line(line, quantity=None, condition=None, unit_price=None,
                      used_multiplier=None):
    batch = line.batch
    _prepare_for_edit(batch)

    if condition is not None and condition != line.condition:
        if condition not in CONDITIONS:
            raise ValueError(f"Unknown condition: {condition!r}")
        if line.item_id is not None:
            clash = find_line(batch, line.item_id, condition)
            if clash is not None:
                raise ValueError(
                    f"This batch already has a {condition} line for "
                    f"{line.display_name}. Change the quantity on that one "
                    f"instead."
                )
        line.condition = condition

    if quantity is not None:
        line.quantity = valuation.clamp_quantity(quantity)

    if line.item is not None:
        if used_multiplier is not None and used_multiplier != line.used_multiplier_at_time:
            line.used_multiplier_at_time = used_multiplier
            line.used_rate_typed = True
        elif not line.used_rate_typed:
            line.used_multiplier_at_time = line.item.used_multiplier

        if unit_price is not None and to_money(unit_price) != line.unit_price_at_time:
            line.unit_price_at_time = to_money(unit_price)
            line.price_typed = True
        elif not line.price_typed and not line.item.needs_manual_price:
            line.unit_price_at_time = line.item.current_price()
    elif unit_price is not None:
        line.unit_price_at_time = to_money(unit_price)

    line.recalculate()
    sync_review_flag(line)
    db.session.flush()
    return line


def set_line_price(line, unit_price):
    _prepare_for_edit(line.batch)
    line.unit_price_at_time = to_money(unit_price) if unit_price is not None else None
    line.recalculate()
    sync_review_flag(line)
    db.session.flush()
    return line


def add_custom_line(batch, name, quantity=1, unit_price=None, note=None,
                    condition=CONDITION_NEW):
    _prepare_for_edit(batch)

    name = (name or "").strip()
    if not name:
        raise ValueError("A custom item needs a name.")
    if condition not in CONDITIONS:
        raise ValueError(f"Unknown condition: {condition!r}")

    line = LineItem(
        batch=batch,
        item_id=None,
        custom_name=name,
        custom_note=(note or "").strip() or None,
        quantity=valuation.clamp_quantity(quantity),
        condition=condition,
        unit_price_at_time=to_money(unit_price if unit_price is not None else 0),
        used_multiplier_at_time=None,
        needs_review=True,
    )
    db.session.add(line)
    line.recalculate()

    db.session.flush()
    return line


def remove_line(line):
    batch = line.batch
    _prepare_for_edit(batch)

    if line in batch.lines:
        batch.lines.remove(line)

    db.session.delete(line)
    db.session.flush()


def set_service_type(batch, service_type_id):
    if service_type_id == batch.service_type_id:
        return False

    if service_type_id is None and batch.is_committed:
        raise ValueError(
            "An approved batch has to keep a service type. Pick a different one "
            "instead of clearing it."
        )

    if batch.is_committed:
        audit.record_change(
            table_name="batch",
            record_id=batch.id,
            field="service_type_id",
            old_value=_service_type_name(batch.service_type_id),
            new_value=_service_type_name(service_type_id),
            description=f"Batch {batch.id} service type",
        )

    batch.service_type_id = service_type_id
    db.session.flush()
    return True


def parse_batch_date(raw):
    import datetime

    try:
        day = datetime.date.fromisoformat(str(raw or "").strip())
    except ValueError:
        raise ValueError("That isn't a date.")
    if day > clock.local_today():
        raise ValueError("A session can't be dated in the future.")
    if day.year < 2000:
        raise ValueError("That date is too far in the past.")
    return day


def set_batch_date(batch, raw):
    day = parse_batch_date(raw)
    if day == batch.date:
        return False
    if batch.is_committed:
        audit.apply_change(batch, "date", day, table_name="batch",
                           description=f"Batch {batch.id} date")
    else:
        batch.date = day
    db.session.flush()
    return True


def _service_type_name(service_type_id):
    if service_type_id is None:
        return None
    service_type = db.session.get(ServiceType, service_type_id)
    return service_type.name if service_type else str(service_type_id)


def set_attendance(batch, values):
    from app.services.attendance import FIELDS_BY_NAME

    unknown = set(values) - set(FIELDS_BY_NAME)
    if unknown:
        raise ValueError(f"Not an attendance field: {', '.join(sorted(unknown))}")

    record = batch.attendance
    if record is None:
        record = Attendance(batch=batch)
        for name in FIELDS_BY_NAME:
            setattr(record, name, 0)
        db.session.add(record)
        db.session.flush()

    for name, value in values.items():
        if batch.is_committed:
            audit.apply_change(
                record, name, value, table_name="attendance",
                description=f"Batch {batch.id} {FIELDS_BY_NAME[name].label.lower()}",
            )
        else:
            setattr(record, name, value)
    db.session.flush()
    return record


def blocking_reasons(batch):
    reasons = []

    if batch.service_type_id is None:
        reasons.append("Pick a service type.")

    has_counts = batch.attendance is not None and batch.attendance.has_any_count
    if not batch.active_lines and not has_counts:
        reasons.append("Add at least one item, or an attendance count.")

    return reasons


def can_commit(batch):
    return not blocking_reasons(batch)


def approval_warnings(batch):
    warnings = []

    unpriced = batch.unpriced_lines
    if unpriced:
        names = ", ".join(line.display_name for line in unpriced)
        warnings.append(
            f"These will be saved without a value and listed in the review "
            f"queue: {names}."
        )

    return warnings


def commit_batch(batch, when=None, commit=True):
    reasons = blocking_reasons(batch)
    if reasons:
        raise BatchNotReady(reasons)

    for line in list(batch.lines):
        if line.quantity == 0:
            db.session.delete(line)
            batch.lines.remove(line)

    for line in batch.active_lines:
        if line.needs_price:
            line.needs_review = True

    for line in batch.lines:
        if line.item is not None and line.report_bucket_at_time is None:
            line.report_bucket_at_time = line.item.report_bucket
        line.recalculate()

    if batch.status != STATUS_COMMITTED:
        batch.status = STATUS_COMMITTED
        batch.committed_at = when or clock.now_utc()

    if commit:
        db.session.commit()
    else:
        db.session.flush()
    return batch


def maybe_autocommit(batch):
    if get_commit_mode() != COMMIT_MODE_AUTO:
        return False
    if not can_commit(batch):
        return False
    commit_batch(batch)
    return True


def discard_batch(batch):
    if batch.is_committed:
        raise ValueError("A committed batch cannot be discarded.")
    db.session.delete(batch)
    db.session.commit()


def delete_for_good(batch):
    from app.models import IntakeSession
    from app.models.audit import ACTION_UPDATE

    service = batch.service_type.name if batch.service_type else "no service type"
    audit.record_event(
        ACTION_UPDATE, "batch", batch.id,
        f"Deleted batch {batch.id} for good: {batch.date.isoformat()}, {service}, "
        f"{batch.status}, {len(batch.lines)} lines, ${batch.total_value:,.2f}",
        field="deleted")
    for session in IntakeSession.query.filter_by(batch_id=batch.id):
        session.batch_id = None
    db.session.delete(batch)


def correct_line(line, quantity=None, condition=None, unit_price=None,
                 used_multiplier=None, report_bucket=None):
    if not line.batch.is_committed:
        raise ValueError("Use set_line() for a draft batch.")

    label = f"{line.display_name} in batch {line.batch_id}"
    changed = False

    if quantity is not None:
        changed |= audit.apply_change(
            line, "quantity", valuation.clamp_quantity(quantity),
            table_name="line_item", description=label,
        )

    if condition is not None:
        if condition not in CONDITIONS:
            raise ValueError(f"Unknown condition: {condition!r}")
        changed |= audit.apply_change(
            line, "condition", condition, table_name="line_item", description=label,
        )

    if unit_price is not None:
        changed |= audit.apply_change(
            line, "unit_price_at_time", to_money(unit_price),
            table_name="line_item", description=label,
        )

    if used_multiplier is not None and line.item is not None:
        if audit.apply_change(
            line, "used_multiplier_at_time", float(used_multiplier),
            table_name="line_item", description=label,
        ):
            line.used_rate_typed = True
            changed = True

    if report_bucket is not None and line.item is not None:
        if report_bucket not in REPORT_BUCKETS:
            raise ValueError(f"'{report_bucket}' is not a report bucket.")
        if audit.apply_change(
            line, "report_bucket_at_time", report_bucket,
            table_name="line_item", description=label,
        ):
            db.session.commit()

    if changed:
        old_value = line.computed_value
        line.recalculate()
        audit.record_change(
            table_name="line_item",
            record_id=line.id,
            field="computed_value",
            old_value=old_value,
            new_value=line.computed_value,
            description=label,
        )

        if sync_review_flag(line):
            audit.record_change(
                table_name="line_item",
                record_id=line.id,
                field="needs_review",
                old_value=not line.needs_review,
                new_value=line.needs_review,
                description=label,
            )

        db.session.commit()

    return line


def _prepare_for_edit(batch):
    if not batch.is_committed:
        return

    if get_commit_mode() == COMMIT_MODE_AUTO and batch.date == clock.local_today():
        batch.status = STATUS_DRAFT
        batch.committed_at = None
        return

    raise ValueError(
        "This batch has already been approved. Use correct_line() so the "
        "change is written to the audit log."
    )
