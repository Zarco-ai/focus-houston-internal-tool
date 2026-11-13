"""Review and approve a batch, and work through the review queue."""

from flask import Blueprint, flash, redirect, render_template, request, url_for

from app.constants import (
    COMMIT_MODE_PER_MOTHER,
    CONDITIONS,
    REPORT_BUCKETS,
)
from app.extensions import db
from app.models import Batch, Item, LineItem, get_commit_mode
from app.services import batches as batch_service
from app.services import attendance, catalog, metrics
from app.services import review as review_service
from app.services import service_types
from app.services.valuation import parse_price


review_bp = Blueprint("review", __name__)


@review_bp.route("/batch/<int:batch_id>/review")
def review_screen(batch_id):
    """Every line, its quantity, condition, unit price and value, plus the
    batch total. Everything on this page is editable."""
    batch = db.get_or_404(Batch, batch_id)

    fields = _attendance_fields()
    return render_template(
        "review/review.html",
        batch=batch,
        lines=batch.active_lines,
        blocking_reasons=batch_service.blocking_reasons(batch),
        approval_warnings=batch_service.approval_warnings(batch),
        service_types=service_types.choices_for(batch),
        report_buckets=REPORT_BUCKETS,
        attendance_sections=[
            (section, [f for f in fields if f.section == section])
            for section in attendance.SECTIONS
            if any(f.section == section for f in fields)
        ],
        display_value=attendance.display_value,
    )


@review_bp.route("/batch/<int:batch_id>/review/lines", methods=["POST"])
def edit_lines(batch_id):
    """Save the whole review table at once.

    One form covers every line, so the fields are named quantity-<line id>,
    condition-<line id>, unit_price-<line id>, used_rate-<line id> and, on an
    approved batch, bucket-<line id>. The Remove buttons submit the same form
    with remove=<line id>.
    """
    batch = db.get_or_404(Batch, batch_id)

    remove_id = request.form.get("remove")
    if remove_id:
        line = LineItem.query.filter_by(
            id=int(remove_id), batch_id=batch.id
        ).first_or_404()
        try:
            batch_service.remove_line(line)
            db.session.commit()
            flash("Line removed.", "success")
        except ValueError as error:
            flash(str(error), "error")
        return redirect(url_for("review.review_screen", batch_id=batch.id))

    for line in list(batch.lines):
        quantity = request.form.get(f"quantity-{line.id}")
        condition = request.form.get(f"condition-{line.id}")
        unit_price = parse_price(request.form.get(f"unit_price-{line.id}"))
        bucket = request.form.get(f"bucket-{line.id}") or None

        if condition and condition not in CONDITIONS:
            flash("Unknown condition.", "error")
            return redirect(url_for("review.review_screen", batch_id=batch.id))

        try:
            used_rate = batch_service.parse_used_rate(request.form.get(f"used_rate-{line.id}"))
            _apply_line_edit(batch, line, quantity, condition, unit_price, used_rate, bucket)
        except ValueError as error:
            flash(str(error), "error")
            return redirect(url_for("review.review_screen", batch_id=batch.id))

    db.session.commit()
    flash("Saved.", "success")

    # A line she has just taken back down to $0.00 is in the review queue now.
    # Saying so here is the only warning she gets before leaving the page. A
    # draft does not need it -- the banner at the top of this page already lists
    # what approving will leave unfinished.
    if batch.is_committed and batch.unpriced_lines:
        names = ", ".join(line.display_name for line in batch.unpriced_lines)
        flash(
            f"Recorded at $0.00 and waiting in the review queue: {names}.", "warn"
        )

    return redirect(url_for("review.review_screen", batch_id=batch.id))


def _apply_line_edit(batch, line, quantity, condition, unit_price, used_rate, bucket):
    """One line's worth of edits, taking the committed/draft difference into
    account."""
    if batch.is_committed:
        # Goes through the audit log.
        batch_service.correct_line(
            line, quantity=quantity, condition=condition, unit_price=unit_price,
            used_multiplier=used_rate, report_bucket=bucket,
        )
        return

    # Edits this exact line rather than looking one up by condition, because
    # here The Center Director is correcting the row she is looking at. A
    # draft's bucket follows its item until approval, so there's none to set.
    batch_service.update_draft_line(
        line, quantity=quantity, condition=condition, unit_price=unit_price,
        used_multiplier=used_rate,
    )


@review_bp.route("/batch/<int:batch_id>/attendance", methods=["POST"])
def set_attendance(batch_id):
    """Head counts and per-event figures. Counts only -- no names anywhere
    on this form. Nothing is saved unless every box is valid."""
    batch = db.get_or_404(Batch, batch_id)
    values, errors = attendance.parse_form(request.form, _attendance_fields())
    if errors:
        for error in errors:
            flash(error, "error")
        flash("Nothing was saved. Fix the boxes above and save again.", "error")
        return redirect(url_for("review.review_screen", batch_id=batch.id) + "#attendance")

    batch_service.set_attendance(batch, values)
    db.session.commit()
    flash("Counts saved.", "success")
    return redirect(url_for("review.review_screen", batch_id=batch.id) + "#attendance")


def _attendance_fields():
    """The boxes the review screen offers: one per switched-on metric."""
    enabled = metrics.enabled_keys()
    return [field for field in attendance.FIELDS if field.metric in enabled]


@review_bp.route("/batch/<int:batch_id>/date", methods=["POST"])
def set_date(batch_id):
    """Date the session: today or any earlier day, never a future one."""
    batch = db.get_or_404(Batch, batch_id)
    try:
        changed = batch_service.set_batch_date(batch, request.form.get("date"))
    except ValueError as error:
        flash(str(error), "error")
    else:
        db.session.commit()
        if changed:
            flash(f"Dated {batch.date.strftime('%B %-d, %Y')}.", "success")
    return redirect(url_for("review.review_screen", batch_id=batch.id))


@review_bp.route("/batch/<int:batch_id>/note", methods=["POST"])
def set_note(batch_id):
    batch = db.get_or_404(Batch, batch_id)
    batch.note = (request.form.get("note") or "").strip() or None
    db.session.commit()
    return redirect(url_for("review.review_screen", batch_id=batch.id))


@review_bp.route("/batch/<int:batch_id>/approve", methods=["POST"])
def approve(batch_id):
    """Commit the batch. What happens next depends on the commit mode."""
    batch = db.get_or_404(Batch, batch_id)

    try:
        batch_service.commit_batch(batch)
    except batch_service.BatchNotReady as not_ready:
        for reason in not_ready.reasons:
            flash(reason, "error")
        return redirect(url_for("review.review_screen", batch_id=batch.id))

    flash(f"Batch approved. {batch.item_count} items, ${batch.total_value:,.2f}.",
          "success")

    if get_commit_mode() == COMMIT_MODE_PER_MOTHER:
        # Straight into the next one without going back to the home screen.
        next_batch = batch_service.open_batch(service_type_id=batch.service_type_id)
        db.session.commit()
        return redirect(url_for("entry.entry_screen", batch_id=next_batch.id))

    return redirect(url_for("home.index"))


# --- The review queue ------------------------------------------------------


@review_bp.route("/review-queue")
def queue():
    """Lines waiting to be finished: custom items that need an identity, and
    catalog items that were approved before anyone knew their price."""
    lines = review_service.flagged_lines()

    return render_template(
        "review/queue.html",
        custom_lines=[line for line in lines if line.is_custom],
        priceless_lines=[line for line in lines if not line.is_custom],
        items=catalog.active_items(),
        categories=catalog.categories_in_use(),
        report_buckets=REPORT_BUCKETS,
    )


@review_bp.route("/review-queue/<int:line_id>/resolve", methods=["POST"])
def resolve(line_id):
    line = db.get_or_404(LineItem, line_id)
    unit_price = parse_price(request.form.get("unit_price"))

    if not line.is_custom:
        # A catalog line is only ever in the queue because its price was
        # missing, so the number is all this form collects.
        try:
            review_service.price_line(line, unit_price=unit_price)
        except ValueError as error:
            flash(str(error), "error")
            return redirect(url_for("review.queue"))

        flash(_priced_message(line), "warn" if line.needs_review else "success")
        return redirect(url_for("review.queue"))

    item = None
    item_id = request.form.get("item_id")
    new_item_fields = None

    if item_id:
        item = db.session.get(Item, int(item_id))
    elif request.form.get("new_name"):
        new_item_fields = {
            "name": request.form.get("new_name"),
            "category": request.form.get("new_category"),
            "report_bucket": request.form.get("new_report_bucket"),
            "used_multiplier": request.form.get("new_used_multiplier"),
            "price_entry": request.form.get("new_price_entry", "fixed"),
            "unit_price_new": request.form.get("new_unit_price"),
            "note": request.form.get("new_note"),
        }

    try:
        review_service.resolve_line(
            line,
            item=item,
            unit_price=unit_price,
            new_item_fields=new_item_fields,
        )
    except (ValueError, catalog.CatalogError) as error:
        flash(str(error), "error")
        return redirect(url_for("review.queue"))

    if line.needs_review:
        # Matched to an item, but still recorded at nothing, so it has not left
        # the queue. Say that rather than letting the card reappear unexplained.
        flash(
            f"Matched to {line.display_name}, but it is still recorded at $0.00, "
            f"so it stays here until it has a value.",
            "warn",
        )
    else:
        flash(f"Resolved as {line.display_name}.", "success")
    return redirect(url_for("review.queue"))


def _priced_message(line):
    """What the queue says back after a price is typed. A zero is not a price,
    and the line is still sitting there, so it does not get told it is done."""
    if line.needs_review:
        return (
            f"{line.display_name} is still recorded at $0.00, so it stays in the "
            f"review queue until it has a value."
        )
    return f"Priced {line.display_name} at ${line.unit_price_at_time:,.2f} each."
