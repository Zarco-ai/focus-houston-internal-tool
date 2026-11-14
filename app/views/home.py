
from flask import Blueprint, flash, redirect, render_template, request, url_for

from app.constants import STATUS_COMMITTED
from app.extensions import db
from app.models import Batch
from app.services import batches as batch_service
from app.services import clock
from app.services import review as review_service

home_bp = Blueprint("home", __name__)


@home_bp.route("/")
def index():
    drafts = batch_service.draft_batches()

    recent = (
        Batch.query.filter_by(status=STATUS_COMMITTED)
        .order_by(Batch.committed_at.desc())
        .limit(5)
        .all()
    )

    return render_template(
        "home.html",
        working_batch=batch_service.todays_working_batch(),
        stale_drafts=[b for b in drafts if b.is_stale],
        fresh_drafts=[b for b in drafts if not b.is_stale],
        recent_batches=recent,
        flagged_count=review_service.flagged_count(),
        today=clock.local_today(),
    )


@home_bp.route("/batch/new", methods=["POST"])
def new_batch():
    day = None
    if request.form.get("date"):
        try:
            day = batch_service.parse_batch_date(request.form["date"])
        except ValueError as error:
            flash(str(error), "error")
            return redirect(url_for("home.index"))
    batch = batch_service.open_batch(date=day)
    db.session.commit()
    return redirect(url_for("entry.entry_screen", batch_id=batch.id))


@home_bp.route("/batch/resume", methods=["POST"])
def resume_today():
    batch = batch_service.get_or_start_todays_batch()
    db.session.commit()
    return redirect(url_for("entry.entry_screen", batch_id=batch.id))


@home_bp.route("/batch/<int:batch_id>/discard", methods=["POST"])
def discard(batch_id):
    batch = db.get_or_404(Batch, batch_id)
    if batch.is_draft and request.form.get("confirm") == "yes":
        batch_service.discard_batch(batch)
    return redirect(url_for("home.index"))
