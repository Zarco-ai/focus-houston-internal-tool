"""The Responses page: the Google Form sync and its review queue.

Everyone signed in can see it and review responses (reviewing is part of
staff's job). Mapping event names and merging two card numbers are admin
pages (app/views/admin.py), because they change what past visits count as.
"""

from flask import Blueprint, current_app, flash, g, redirect, render_template, request, url_for

from app.extensions import db, limiter
from app.models import FormResponse, ServiceType
from app.services import form_review, form_sync

responses_bp = Blueprint("responses", __name__)


def _sync_limit():
    return current_app.config["SYNC_RATE_LIMIT"]


def sync_if_stale():
    """Sync when the page opens, if the last attempt is a few minutes old.
    Returns a sentence about the setup if the sync can't be configured."""
    try:
        source = form_sync.configured_source(current_app.config)
    except form_sync.SyncError as error:
        return str(error)
    if source is None:
        return None
    if form_sync.is_stale(current_app.config["FORM_SYNC_STALE_MINUTES"]):
        form_sync.run(source, triggered_by_id=g.user.id)
    return None


@responses_bp.route("/responses")
def page():
    setup_error = sync_if_stale()
    try:
        source = form_sync.configured_source(current_app.config)
    except form_sync.SyncError:
        source = None
    return render_template(
        "responses/responses.html",
        configured=source is not None,
        service_account=source.service_account_email if source else None,
        setup_error=setup_error,
        last_run=form_sync.last_run(),
        last_success=form_sync.last_success(),
        runs=form_sync.recent_runs(),
        queue=form_review.review_queue(),
        kind_text=form_review.KIND_TEXT,
        unmapped=form_review.unmapped_label_count(),
        recent=form_review.recent_responses(),
        merges=form_review.merges() if g.user.is_admin else [],
        service_types=ServiceType.query.order_by(ServiceType.name).all(),
    )


@responses_bp.route("/responses/sync", methods=["POST"])
@limiter.limit(_sync_limit)
def sync_now():
    try:
        source = form_sync.configured_source(current_app.config)
    except form_sync.SyncError as error:
        flash(str(error), "error")
        return redirect(url_for("responses.page"))
    if source is None:
        flash("The Google Form sync isn't set up yet. See docs/GOOGLE_FORM_SETUP.md.", "error")
        return redirect(url_for("responses.page"))

    run = form_sync.run(source, triggered_by_id=g.user.id)
    if run is None:
        flash("A sync is already running. Give it a moment and refresh.", "warn")
    elif run.error:
        flash(f"Sync finished with a problem: {run.error}", "error")
    else:
        more = f" {run.rows_remaining} more to come: sync again." if run.rows_remaining else ""
        flash(f"Synced. {run.rows_imported} new response{'s' if run.rows_imported != 1 else ''}"
              f"{f', {run.rows_flagged} to review' if run.rows_flagged else ''}.{more}", "success")
    return redirect(url_for("responses.page"))


def _decide(response_id, action, message):
    response = db.get_or_404(FormResponse, response_id)
    try:
        action(response)
    except form_review.ReviewError as error:
        db.session.rollback()
        flash(str(error), "error")
    else:
        db.session.commit()
        flash(message, "success")
    return redirect(url_for("responses.page") + "#review")


@responses_bp.route("/responses/<int:response_id>/match", methods=["POST"])
def match(response_id):
    code = request.form.get("card_number", "")
    return _decide(response_id, lambda r: form_review.match(r, code),
                   "Matched. The visit is recorded on that card number.")


@responses_bp.route("/responses/<int:response_id>/create", methods=["POST"])
def create(response_id):
    return _decide(response_id, form_review.create,
                   "Created her on that card number, with this visit.")


@responses_bp.route("/responses/<int:response_id>/discard", methods=["POST"])
def discard(response_id):
    return _decide(response_id, form_review.discard, "Discarded.")


@responses_bp.route("/responses/<int:response_id>/checked", methods=["POST"])
def checked(response_id):
    return _decide(response_id, form_review.mark_checked, "Marked as checked. The visit stands.")
