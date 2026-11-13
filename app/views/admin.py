"""Item management, the "Needs a price" list, settings, and the audit log.

The Center Director runs this herself. Adding a diaper is a form on this screen, not a
phone call to the developer.
"""

from flask import (
    Blueprint,
    current_app,
    flash,
    g,
    redirect,
    render_template,
    request,
    url_for,
)

from app.constants import (
    CATEGORY_ORDER,
    COMMIT_MODES,
    PRICE_ENTRY_CHOICES,
    REPORT_BUCKETS,
)
from app.extensions import db
from app.models import (
    Item,
    Participant,
    QualityOption,
    ServiceType,
    get_commit_mode,
    set_commit_mode,
)
from app.models.audit import ACTION_EXPORT
from app.services import (
    audit,
    catalog,
    clock,
    form_review,
    intake,
    metrics,
    participants,
    retention,
    service_types,
    snapshots,
)

# A card list of 9,000 numbers is about 250 KB. Anything far bigger isn't one.
MAX_CARD_FILE_BYTES = 1_000_000
# The January-September 2026 file is about 150 KB.
MAX_HISTORY_FILE_BYTES = 5_000_000

admin_bp = Blueprint("admin", __name__, url_prefix="/admin")


@admin_bp.route("/items")
def items():
    """The whole catalog, including deactivated items."""
    search = (request.args.get("q") or "").strip().lower()
    all_items = catalog.all_items()

    if search:
        all_items = [
            item
            for item in all_items
            if search in item.name.lower() or search in item.category.lower()
        ]

    return render_template(
        "admin/items.html",
        items=all_items,
        search=search,
        needs_price_count=len(catalog.items_needing_a_price()),
    )


@admin_bp.route("/items/new", methods=["GET", "POST"])
def new_item():
    if request.method == "POST":
        try:
            item = catalog.create_item(
                name=request.form.get("name"),
                category=request.form.get("category"),
                report_bucket=request.form.get("report_bucket"),
                used_multiplier=request.form.get("used_multiplier"),
                price_entry=request.form.get("price_entry"),
                unit_price_new=request.form.get("unit_price_new"),
                note=request.form.get("note"),
            )
        except catalog.CatalogError as error:
            flash(str(error), "error")
            return render_template(
                "admin/item_form.html",
                item=None,
                form=request.form,
                categories=_category_choices(),
                report_buckets=REPORT_BUCKETS,
                price_entry_choices=PRICE_ENTRY_CHOICES,
            )

        flash(f"Added {item.display_name}.", "success")
        return redirect(url_for("admin.items"))

    return render_template(
        "admin/item_form.html",
        item=None,
        form={},
        categories=_category_choices(),
        report_buckets=REPORT_BUCKETS,
        price_entry_choices=PRICE_ENTRY_CHOICES,
    )


@admin_bp.route("/items/<int:item_id>", methods=["GET", "POST"])
def edit_item(item_id):
    item = db.get_or_404(Item, item_id)

    if request.method == "POST":
        try:
            catalog.update_item(
                item,
                name=request.form.get("name"),
                category=request.form.get("category"),
                report_bucket=request.form.get("report_bucket"),
                used_multiplier=request.form.get("used_multiplier"),
                price_entry=request.form.get("price_entry"),
                note=request.form.get("note"),
                active=request.form.get("active") == "on",
                display_group=request.form.get("display_group"),
            )
        except catalog.CatalogError as error:
            flash(str(error), "error")
        else:
            flash(f"Saved {item.display_name}.", "success")
            return redirect(url_for("admin.items"))

    return render_template(
        "admin/item_form.html",
        item=item,
        form={},
        categories=_category_choices(),
        report_buckets=REPORT_BUCKETS,
        price_entry_choices=PRICE_ENTRY_CHOICES,
        history=audit.history_for("item", item.id),
    )


@admin_bp.route("/items/<int:item_id>/price", methods=["POST"])
def change_price(item_id):
    """Set a new price. Adds a row to item_price -- old batches keep their
    old numbers."""
    item = db.get_or_404(Item, item_id)

    try:
        catalog.change_price(item, request.form.get("unit_price_new"))
    except catalog.CatalogError as error:
        flash(str(error), "error")
    else:
        flash(
            f"New price for {item.display_name}. Batches approved before today "
            f"keep the price they were approved at.",
            "success",
        )

    return redirect(request.referrer or url_for("admin.items"))


@admin_bp.route("/needs-price")
def needs_price():
    """Every manual-price item and the note explaining why.

    This list is the agenda for the next conversation with The Center Director. It should
    shrink as she supplies real values.
    """
    return render_template(
        "admin/needs_price.html",
        items=catalog.items_needing_a_price(),
    )


@admin_bp.route("/settings", methods=["GET", "POST"])
def settings():
    """Which commit mode to use, and when an intake timer is flagged."""
    if request.method == "POST":
        if "activity_window_days" in request.form:
            try:
                retention.set_window_days(request.form["activity_window_days"])
            except ValueError as error:
                flash(str(error), "error")
                return redirect(url_for("admin.settings"))
            db.session.commit()
            flash("Saved.", "success")
            return redirect(url_for("admin.settings"))

        if "intake_flag_hours" in request.form:
            try:
                intake.set_flag_hours(request.form["intake_flag_hours"])
            except intake.IntakeError as error:
                flash(str(error), "error")
                return redirect(url_for("admin.settings"))
            db.session.commit()
            flash("Saved.", "success")
            return redirect(url_for("admin.settings"))

        mode = request.form.get("commit_mode")
        if mode in COMMIT_MODES:
            set_commit_mode(mode)
            db.session.commit()
            flash("Saved.", "success")
        else:
            flash("Unknown mode.", "error")
        return redirect(url_for("admin.settings"))

    return render_template("admin/settings.html", current_mode=get_commit_mode(),
                           flag_hours=intake.flag_hours(), window_days=retention.window_days())


@admin_bp.route("/card-numbers", methods=["GET", "POST"])
def card_numbers():
    """Load a cards-batch-N.csv made by generate_cards.py.

    An upload, because Render's free plan has no shell to run a command in.
    All or nothing; loading the same file twice changes nothing.
    """
    if request.method == "POST":
        upload = request.files.get("card_list")
        if upload is None or not upload.filename:
            flash("Choose the cards-batch-N.csv file first.", "error")
            return redirect(url_for("admin.card_numbers"))
        raw = upload.stream.read(MAX_CARD_FILE_BYTES + 1)
        if len(raw) > MAX_CARD_FILE_BYTES:
            flash("That file is far too big to be a card list.", "error")
            return redirect(url_for("admin.card_numbers"))
        try:
            added = participants.load_card_list(raw.decode("utf-8-sig"))
        except UnicodeDecodeError:
            flash("That file isn't a text CSV.", "error")
            return redirect(url_for("admin.card_numbers"))
        except participants.CardListError as error:
            db.session.rollback()
            flash(f"Nothing was loaded. {error}", "error")
            return redirect(url_for("admin.card_numbers"))
        db.session.commit()
        flash(f"Loaded {added} card numbers." if added
              else "That batch is already loaded; nothing changed.", "success")
        return redirect(url_for("admin.card_numbers"))

    return render_template("admin/card_numbers.html", status=participants.card_list_status())


def _day_contents(day):
    """The batches and Google Form responses dated `day`."""
    from app.models import Batch, FormResponse

    found = Batch.query.filter_by(date=day).order_by(Batch.id).all()
    responses = (FormResponse.query.filter_by(submitted_local_date=day)
                 .filter(FormResponse.decision.is_distinct_from("discarded"))
                 .order_by(FormResponse.id).all())
    return found, responses


@admin_bp.route("/delete-day", methods=["GET", "POST"])
def delete_day():
    """Delete one day's test batches for good, and discard that day's Form
    responses (their visits go with them). D9-13: the same rules as
    `flask batches delete-date`, as a page because Render's free plan has no
    shell. Only the date given; only if the counts still match the page."""
    import datetime

    from app.services import batches as batch_service

    raw = request.values.get("date", "")
    try:
        day = datetime.date.fromisoformat(raw) if raw else None
    except ValueError:
        flash("Pick a date.", "error")
        return redirect(url_for("admin.delete_day"))
    found, responses = _day_contents(day) if day else ([], [])

    if request.method == "POST":
        expected = (request.form.get("batch_count"), request.form.get("response_count"))
        if day is None or expected != (str(len(found)), str(len(responses))):
            flash("What's dated that day changed since the page opened. Nothing was deleted; "
                  "check the list again.", "error")
            return redirect(url_for("admin.delete_day", date=raw))
        if request.form.get("confirm", "").strip() != day.isoformat():
            flash(f"Type {day.isoformat()} in the box to confirm. Nothing was deleted.", "error")
            return redirect(url_for("admin.delete_day", date=raw))
        for batch in found:
            batch_service.delete_for_good(batch)
        for response in responses:
            form_review.discard(response)
        db.session.commit()
        flash(f"Deleted {len(found)} batch{'es' if len(found) != 1 else ''} and discarded "
              f"{len(responses)} form response{'s' if len(responses) != 1 else ''} dated "
              f"{day.isoformat()}. No other date was touched.", "success")
        return redirect(url_for("admin.delete_day", date=raw))

    return render_template("admin/delete_day.html", day=day, batches=found, responses=responses)


@admin_bp.route("/history", methods=["GET", "POST"])
def history_import():
    """Upload a focus-history file: earlier months from the sheets, as
    recorded. An upload because Render's free plan has no shell."""
    from app.services import history

    if request.method == "POST":
        upload = request.files.get("history_file")
        if upload is None or not upload.filename:
            flash("Choose the history file first.", "error")
            return redirect(url_for("admin.history_import"))
        raw = upload.stream.read(MAX_HISTORY_FILE_BYTES + 1)
        if len(raw) > MAX_HISTORY_FILE_BYTES:
            flash("That file is far too big to be a history file.", "error")
            return redirect(url_for("admin.history_import"))
        try:
            summary = history.import_history(raw.decode("utf-8"))
        except (UnicodeDecodeError, history.HistoryError) as error:
            db.session.rollback()
            flash(f"Nothing was imported. {error}", "error")
            return redirect(url_for("admin.history_import"))
        flash(f"Imported {summary['sessions']} sessions. {summary['to_review']} lines are waiting "
              f"in the review queue for a value or a category.", "success")
        return redirect(url_for("review.queue"))
    return render_template("admin/history.html")


@admin_bp.route("/history/export.json")
def history_export():
    """Every approved session, exactly as it is now (review-queue flags
    included), as a history file to keep or to load into another copy."""
    from app.services import history
    from app.views.reports import download

    audit.record_event(ACTION_EXPORT, "export", 0, "Downloaded every approved session (history file)")
    db.session.commit()
    return download(history.export_text(),
                    f"focus-sessions-{clock.local_today().isoformat()}.json", "application/json")


@admin_bp.route("/qualities", methods=["GET", "POST"])
def qualities():
    """The "unique qualities" checklist on the intake survey."""
    if request.method == "POST":
        try:
            option = intake.add_quality(request.form.get("label"))
        except intake.IntakeError as error:
            flash(str(error), "error")
        else:
            db.session.commit()
            flash(f"Added “{option.label}”.", "success")
        return redirect(url_for("admin.qualities"))
    return render_template("admin/qualities.html", qualities=intake.all_qualities())


@admin_bp.route("/qualities/<int:quality_id>", methods=["POST"])
def edit_quality(quality_id):
    option = db.get_or_404(QualityOption, quality_id)
    try:
        if "active" in request.form:
            intake.update_quality(option, active=request.form["active"] == "on")
        else:
            intake.update_quality(option, label=request.form.get("label"))
    except intake.IntakeError as error:
        db.session.rollback()
        flash(str(error), "error")
    else:
        db.session.commit()
        flash("Saved.", "success")
    return redirect(url_for("admin.qualities"))


@admin_bp.route("/service-types", methods=["GET", "POST"])
def service_type_list():
    """Add a service type, or see them all."""
    if request.method == "POST":
        try:
            created = service_types.create(request.form.get("name"))
        except ValueError as error:
            flash(str(error), "error")
        else:
            db.session.commit()
            flash(f"Added {created.name}.", "success")
        return redirect(url_for("admin.service_type_list"))

    return render_template(
        "admin/service_types.html", service_types=service_types.all_service_types()
    )


@admin_bp.route("/service-types/<int:service_type_id>", methods=["POST"])
def edit_service_type(service_type_id):
    """Rename one, or switch it on or off."""
    service_type = db.get_or_404(ServiceType, service_type_id)
    try:
        if "active" in request.form:
            service_types.set_active(service_type, request.form["active"] == "on")
            message = f"{service_type.name} switched {'on' if service_type.active else 'off'}."
        else:
            service_types.rename(service_type, request.form.get("name"))
            message = f"Saved {service_type.name}."
    except ValueError as error:
        db.session.rollback()
        flash(str(error), "error")
    else:
        db.session.commit()
        flash(message, "success")
    return redirect(url_for("admin.service_type_list"))


@admin_bp.route("/metrics", methods=["GET", "POST"])
def metric_settings():
    """Switch report rows on and off, and change their wording.

    One form for every metric. A box left with the default wording, or
    emptied, keeps the default.
    """
    if request.method == "POST":
        changed = 0
        try:
            for setting in metrics.settings():
                key = setting.key
                changed += metrics.update_setting(
                    key,
                    label=request.form.get(f"label-{key}", setting.label),
                    enabled=request.form.get(f"enabled-{key}") == "on",
                )
        except ValueError as error:
            db.session.rollback()
            flash(str(error), "error")
        else:
            db.session.commit()
            flash(f"Saved. {changed} metric{'s' if changed != 1 else ''} changed."
                  if changed else "Nothing changed.", "success")
        return redirect(url_for("admin.metric_settings"))

    return render_template(
        "admin/metrics.html",
        groups=[
            (group, [s for s in metrics.settings() if s.metric.group == group.key])
            for group in metrics.GROUPS
        ],
    )


@admin_bp.route("/annual", methods=["GET", "POST"])
def annual_figures():
    """The ANNUAL METRICS group, typed once a year. Blank means not entered."""
    year = _year_from(request.values.get("year"))

    if request.method == "POST":
        errors, typed = [], {}
        for metric in metrics.ANNUAL_METRICS:
            raw = (request.form.get(metric.key) or "").strip().replace(",", "")
            if raw == "":
                typed[metric.key] = None
            elif raw.isascii() and raw.isdigit():
                typed[metric.key] = int(raw)
            else:
                errors.append(f"{metric.label}: type a whole number, 0 or more, or "
                              f"leave it blank (got '{raw}').")
        if errors:
            for error in errors:
                flash(error, "error")
            flash("Nothing was saved.", "error")
        else:
            changed = sum(
                metrics.set_annual_value(year, key, value, entered_by_id=g.user.id)
                for key, value in typed.items()
            )
            db.session.commit()
            flash(f"Saved {year}." if changed else "Nothing changed.", "success")
        return redirect(url_for("admin.annual_figures", year=year))

    return render_template(
        "admin/annual.html",
        year=year,
        annual_metrics=metrics.ANNUAL_METRICS,
        entered=metrics.annual_values(year),
    )


def _year_from(raw):
    try:
        year = int(raw)
    except (TypeError, ValueError):
        return clock.local_today().year
    return year if 2000 <= year <= 2100 else clock.local_today().year


@admin_bp.route("/form-events", methods=["GET", "POST"])
def form_events():
    """Map each "What event are you here for?" answer to a service type,
    once. Every visit from that answer, past ones included, follows."""
    if request.method == "POST":
        raw_type = request.form.get("service_type_id") or None
        try:
            service_type_id = int(raw_type) if raw_type else None
            changed = form_review.map_event(request.form.get("label"), service_type_id)
        except (ValueError, form_review.ReviewError) as error:
            db.session.rollback()
            flash(str(error), "error")
        else:
            db.session.commit()
            flash(f"Saved. {changed} response{'s' if changed != 1 else ''} follow{'s' if changed == 1 else ''} it.",
                  "success")
        return redirect(url_for("admin.form_events"))

    return render_template(
        "admin/form_events.html",
        labels=form_review.event_labels(),
        service_types=service_types.active_service_types(),
    )


@admin_bp.route("/merge", methods=["POST"])
def merge_participants():
    """She has two card numbers (a lost card): keep one, retire the other."""
    try:
        retiring, keeping = form_review.merge(request.form.get("retiring"),
                                              request.form.get("keeping"))
    except form_review.ReviewError as error:
        db.session.rollback()
        flash(str(error), "error")
    else:
        db.session.commit()
        flash(f"Merged: {retiring.code}'s visits now count toward {keeping.code}.", "success")
    return redirect(url_for("responses.page") + "#merge")


@admin_bp.route("/merge/<int:participant_id>/undo", methods=["POST"])
def unmerge_participant(participant_id):
    participant = db.get_or_404(Participant, participant_id)
    try:
        form_review.unmerge(participant)
    except form_review.ReviewError as error:
        flash(str(error), "error")
    else:
        db.session.commit()
        flash(f"Merge undone: {participant.code} counts on its own again.", "success")
    return redirect(url_for("responses.page") + "#merge")


# --- Filing (Phase 8) ---------------------------------------------------------------------


@admin_bp.route("/reports/file", methods=["POST"])
def file_report():
    """Save exactly what is being sent, frozen. Filing again later adds a
    new snapshot; it never replaces one."""
    kind = request.form.get("kind")
    try:
        year = int(request.form.get("year", ""))
        month = int(request.form["month"]) if kind == "monthly" else None
        if not 2000 <= year <= 2100 or (month is not None and not 1 <= month <= 12):
            raise ValueError
        snapshot = snapshots.file(kind, year, month, filed_by_id=g.user.id,
                                  app_version=current_app.config["APP_VERSION"])
    except (KeyError, ValueError):
        flash("That isn't a report that can be filed.", "error")
        return redirect(url_for("totals.index"))
    db.session.commit()
    flash(f"Filed. This is now the record of what was sent for "
          f"{snapshot.period_start.strftime('%B %Y') if kind == 'monthly' else year}.", "success")
    return redirect(url_for("reports.filed_one", snapshot_id=snapshot.id))


@admin_bp.route("/audit")
def audit_log():
    return render_template("admin/audit.html", entries=audit.recent_changes(200))


def _category_choices():
    """Categories already in use, plus the standard ones, so a new category is
    still possible by typing one."""
    in_use = catalog.categories_in_use()
    extra = [slug for slug in CATEGORY_ORDER if slug not in in_use]
    return in_use + extra
