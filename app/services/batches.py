"""Opening, filling in, and approving a batch.

The rule that shapes this whole module: a committed line never changes by
accident. While a batch is a draft its lines follow the catalog, because a
draft is still being written -- unless someone typed a price or used rate on
the review screen, which then stays as typed. The moment it is committed, the
price, the used rate and the report bucket on every line are frozen, and the
only way to change them afterwards is a correction that writes to the audit
log.

A missing price is not a reason to refuse an approval. A line counted without
one is committed as it stands and flagged for review, and the value is typed in
from the review queue later. What went out the door is a fact; what it was
worth is a question, and the question must never be allowed to lose the fact.
"""

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
    """A used rate typed on the review screen: 0.75, .75 or 75%. None or
    blank means "not given". Raises ValueError with a message for the screen."""
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
    """Raised when a batch cannot be approved yet. Carries the reasons."""

    def __init__(self, reasons):
        self.reasons = reasons
        super().__init__("; ".join(reasons))


# --- Finding and opening batches -------------------------------------------


def open_batch(date=None, service_type_id=None, note=None):
    """Start a new draft. The date defaults to today and is never typed."""
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
    """Every unfinished batch, newest first."""
    return (
        Batch.query.filter_by(status=STATUS_DRAFT)
        .order_by(Batch.date.desc(), Batch.id.desc())
        .all()
    )


def stale_draft_batches():
    """Drafts left over from a previous day. Flagged, never auto-removed."""
    return [batch for batch in draft_batches() if batch.is_stale]


def todays_working_batch():
    """The batch the entry screen should open.

    In auto mode that is today's batch whether or not it has been saved
    already, because in auto mode entries keep flowing into the same session.
    Otherwise it is the newest draft dated today, if there is one.
    """
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


# --- Editing lines ----------------------------------------------------------


def sync_review_flag(line):
    """Match a catalog line's place in the review queue to what it is worth
    right now, in either direction. Returns True if the flag actually moved.

    A catalog line is only ever in the queue because its value was missing, so
    typing a real price finishes it -- whether she types it on the entry screen,
    on the review screen, or in the queue itself. Taking that value back down to
    zero puts the line straight back in, because zero is not an answer. That
    round trip is the whole reason this is two-way and not just a clear.

    Only a committed line is flagged from here. A draft is still being written
    and commit_batch() flags whatever is still missing at approval; flagging
    mid-count would fill the queue with rows she is in the middle of typing.

    A custom line is left alone. It needs a catalog item, and with it a category
    and a used rate, not just a number, so it stays flagged until the queue
    resolves it.
    """
    if line.is_custom:
        return False

    if line.needs_price:
        if line.needs_review or not line.batch.is_committed:
            return False
        line.needs_review = True
        # It is waiting again, so it has not been reviewed. Leaving the old
        # timestamp on would claim otherwise.
        line.reviewed_at = None
        return True

    if not line.needs_review:
        return False

    line.needs_review = False
    line.reviewed_at = clock.now_utc()
    return True


def lines_for(batch, item_id, condition):
    """Every line for one item in one condition, oldest first.

    Usually there is one. A manual-price item can have several: two strollers
    worth $40 and $300 are two lines, because one quantity and one price cannot
    describe both. Fixed-price items never need this -- their value comes from
    the catalog, so two of them are just a quantity of two.
    """
    matches = [
        line
        for line in batch.lines
        if line.item_id == item_id and line.condition == condition
    ]
    # New lines have no id until the session flushes, so keep those last rather
    # than blowing up on a None comparison.
    matches.sort(key=lambda line: (line.id is None, line.id or 0))
    return matches


def find_line(batch, item_id, condition):
    """The first line for one item and condition -- the one the main row on the
    counting screen edits. Extra prices hang off it and are edited by id."""
    matches = lines_for(batch, item_id, condition)
    return matches[0] if matches else None


def lines_for_item(batch, item_id):
    """Every line this batch has for one item, keyed by condition."""
    found = {}
    for line in batch.lines:
        if line.item_id == item_id:
            found.setdefault(line.condition, []).append(line)
    return found


def add_price_group(batch, item, condition=None, quantity=1):
    """Record the same item a second time at a different value.

    Only for manual-price items. A fixed-price item is worth what the catalog
    says, so two of them are a quantity of two, not two lines.
    """
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
        # No price yet. The Center Director types this one, and if she approves
        # before she does it is flagged for review -- exactly like the first one.
        unit_price_at_time=None,
    )
    db.session.add(line)
    line.recalculate()
    db.session.flush()
    return line


def set_line(batch, item, quantity=None, condition=None, unit_price=None):
    """Set the quantity and (for manual items) the price of one item in one
    condition.

    The condition selects WHICH line is being edited, it does not change an
    existing line's condition -- counting a used one never disturbs the new
    ones already counted. Defaults to new.

    Passing None for quantity or price leaves that part alone. Returns the
    line, which may have a quantity of zero; zero lines are kept while drafting
    so the screen remembers a typed price, and are dropped at approval.
    """
    _prepare_for_edit(batch)

    condition = condition or CONDITION_NEW
    if condition not in CONDITIONS:
        raise ValueError(f"Unknown condition: {condition!r}")

    line = find_line(batch, item.id, condition)
    if line is None:
        # Passing batch=batch puts the line into batch.lines for us. Do not
        # also append it -- that would make it show up twice and double the
        # batch total.
        line = LineItem(
            batch=batch,
            item_id=item.id,
            quantity=0,
            condition=condition,
        )
        db.session.add(line)

    if quantity is not None:
        line.quantity = valuation.clamp_quantity(quantity)

    # The used rate comes from the item, re-snapshotted on every edit while
    # the batch is a draft -- unless it was typed on the review screen.
    if not line.used_rate_typed:
        line.used_multiplier_at_time = item.used_multiplier

    if line.price_typed:
        pass  # typed on the review screen; the catalog doesn't overwrite it
    elif item.needs_manual_price:
        # The Center Director types this one. Only overwrite when a price was actually
        # supplied, so changing the quantity does not wipe the price she typed.
        # Each condition holds its own price -- a used stroller is not simply
        # half of whatever she typed for a new one.
        if unit_price is not None:
            line.unit_price_at_time = to_money(unit_price)
    else:
        # Trusted catalog price, refreshed while the batch is still a draft.
        line.unit_price_at_time = item.current_price()

    line.recalculate()
    sync_review_flag(line)
    db.session.flush()
    return line


def update_draft_line(line, quantity=None, condition=None, unit_price=None,
                      used_multiplier=None):
    """Edit one specific draft line in place.

    This is what the review screen uses. Unlike set_line() it changes the line
    you hand it rather than looking one up by condition, because on the review
    screen The Center Director is correcting a line she is looking at.

    A price or used rate that differs from what the line already shows is a
    deliberate edit: it is kept, and the catalog stops overwriting it. The
    screen sends every box back on each save, so an unchanged box is not an
    edit.
    """
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
        # A custom line has no catalog item to read a price or rate from.
        line.unit_price_at_time = to_money(unit_price)

    line.recalculate()
    sync_review_flag(line)
    db.session.flush()
    return line


def set_line_price(line, unit_price):
    """Type a price onto one line. Used by the entry screen and by review."""
    _prepare_for_edit(line.batch)
    line.unit_price_at_time = to_money(unit_price) if unit_price is not None else None
    line.recalculate()
    sync_review_flag(line)
    db.session.flush()
    return line


def add_custom_line(batch, name, quantity=1, unit_price=None, note=None,
                    condition=CONDITION_NEW):
    """Add something that is not in the catalog.

    The line is flagged needs_review and shows up in the review queue, where a
    name, category and price can be filled in later. It keeps the date of the
    batch it was entered in -- the date is never typed by hand.

    The price defaults to zero rather than to nothing. A custom item is
    something The Center Director is adding mid-distribution without knowing what it is
    worth, so making her invent a number before she can approve the batch would
    be worse than recording it at zero and pricing it properly in the review
    queue afterwards.
    """
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
        # No catalog item means no used rate of its own yet. The review queue
        # assigns one along with the category. Until then a zero price is worth
        # zero whatever the condition, which is what keeps this line from
        # blocking approval.
        used_multiplier_at_time=None,
        needs_review=True,
    )
    db.session.add(line)
    line.recalculate()

    db.session.flush()
    return line


def remove_line(line):
    """Take a line out of a draft entirely."""
    batch = line.batch
    _prepare_for_edit(batch)

    # Take it out of the batch's own list as well as the database. Deleting it
    # only from the session leaves it sitting in batch.lines until something
    # commits, and until then the batch total still counts it.
    if line in batch.lines:
        batch.lines.remove(line)

    db.session.delete(line)
    db.session.flush()


def set_service_type(batch, service_type_id):
    """Say what kind of session this batch is. Returns True if it moved.

    Optional while counting, required to approve -- see blocking_reasons().
    Which is why it cannot be taken back off a batch that has already been
    approved: that would leave a committed batch in a state approval would have
    refused, and the monthly report reads this field.

    Changing it on an approved batch is a change to reported data, so it is
    written to the audit log like any other correction.
    """
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
            # The name, not the id. Nobody reading the log knows what a 3 is.
            old_value=_service_type_name(batch.service_type_id),
            new_value=_service_type_name(service_type_id),
            description=f"Batch {batch.id} service type",
        )

    batch.service_type_id = service_type_id
    db.session.flush()
    return True


def parse_batch_date(raw):
    """A session date typed by a person: today or earlier, never later.

    The prototype never let a batch date be typed, so a slip like "2024"
    couldn't happen (reconciliation R5/R23). Phase 9: past dates are allowed
    so earlier months' sheets can be entered, and the paper fallback works;
    a future date is still refused, and so is anything before 2000.
    """
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
    """Move a batch to another (past) day. On an approved batch it's a
    change to reported data, so it goes in the audit log. Returns True if
    the date changed."""
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
    """Save head counts and per-event figures: {column: value}, already
    checked by attendance.parse_form(). Columns not in `values` are left as
    they are.

    Counts only -- there are no name fields here and there must never be
    any. On an approved batch these are reported numbers, so every change
    goes through the audit log like a line correction does.
    """
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


# --- Approving --------------------------------------------------------------


def blocking_reasons(batch):
    """Everything standing between this batch and being approved.

    Returns a list of plain sentences, empty when the batch is ready.

    A missing price is deliberately not on this list. It is a thing to finish
    later, not a thing that stops the batch -- see approval_warnings().
    """
    reasons = []

    if batch.service_type_id is None:
        reasons.append("Pick a service type.")

    # A class or a volunteer event can hand out nothing and still be worth
    # reporting, so counts alone are enough. Neither at all is a mistake.
    has_counts = batch.attendance is not None and batch.attendance.has_any_count
    if not batch.active_lines and not has_counts:
        reasons.append("Add at least one item, or an attendance count.")

    return reasons


def can_commit(batch):
    return not blocking_reasons(batch)


def approval_warnings(batch):
    """What approving this batch will leave unfinished.

    Plain sentences, same shape as blocking_reasons(), but these do not stop
    anything. The review screen shows them next to an Approve button that still
    works, so nothing is a surprise afterwards.
    """
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
    """Approve a batch.

    Drops any line whose quantity fell back to zero, flags anything still
    missing a price for review, freezes the price and the used rate on
    everything that is left, and marks the batch committed.
    """
    reasons = blocking_reasons(batch)
    if reasons:
        raise BatchNotReady(reasons)

    # A quantity of zero contributes nothing and does not appear in the batch.
    for line in list(batch.lines):
        if line.quantity == 0:
            db.session.delete(line)
            batch.lines.remove(line)

    # A line with no price is committed anyway, flagged so the review queue
    # will ask for the number. The quantity is what she actually handed out and
    # it is not worth losing over a price nobody knows yet.
    for line in batch.active_lines:
        if line.needs_price:
            line.needs_review = True

    # Freeze. From here on these numbers are history and are never looked up
    # from the catalog again. A bucket already frozen (auto mode re-saves the
    # same batch all day) keeps its first value.
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
        db.session.flush()   # the caller commits (the history import, all or nothing)
    return batch


def maybe_autocommit(batch):
    """In auto mode, save as she goes.

    Only a missing service type or an empty batch holds this back, and the entry
    screen says which. An unpriced line does not: it is saved flagged, the same
    as it would be through the Approve button.
    """
    if get_commit_mode() != COMMIT_MODE_AUTO:
        return False
    if not can_commit(batch):
        return False
    commit_batch(batch)
    return True


def discard_batch(batch):
    """Throw away a draft. Committed batches are never deleted."""
    if batch.is_committed:
        raise ValueError("A committed batch cannot be discarded.")
    db.session.delete(batch)
    db.session.commit()


def delete_for_good(batch):
    """Permanently delete a batch, approved or not: the one exception to
    "committed batches are never deleted" (the client's decision, 11 Nov 2026,
    for test batches; D9-13). Command line only (flask batches delete-date),
    never a page. The audit log keeps a row saying what was deleted. An
    intake that started the batch stays, unlinked. The caller commits."""
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


# --- Correcting committed data ---------------------------------------------


def correct_line(line, quantity=None, condition=None, unit_price=None,
                 used_multiplier=None, report_bucket=None):
    """Change a line on a batch that has already been approved.

    Every field that actually moves is written to the audit log, along with the
    recomputed value. report_bucket re-sorts this one line only; the item's
    own bucket, and every other month, are untouched.
    """
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
        # Not `changed |=`: moving a line between buckets doesn't change what
        # it was worth, so there's no value to recompute -- but it is logged.
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

        # Supplying the price a line was flagged for finishes the review, and
        # taking the value back off puts it back in the queue. Either way the
        # flag moved on committed data, so it is logged like any other change.
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
    """Make sure this batch is safe to write to, or refuse.

    A draft is always fine. A committed batch is not -- with one exception: in
    auto mode today's batch gets saved after every change, so the next item she
    counts has to be able to land in the same batch. Reopening it for a moment
    inside the same session is the honest way to do that, and it is re-saved
    before the request ends.
    """
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
