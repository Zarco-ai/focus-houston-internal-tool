
from flask import Blueprint, render_template

from app.extensions import db
from app.models import Batch
from app.services import attendance, audit

batches_bp = Blueprint("batches", __name__)


@batches_bp.route("/batches")
def list_batches():
    return render_template(
        "batches/list.html",
        batches=Batch.query.order_by(Batch.date.desc(), Batch.id.desc()).all(),
    )


@batches_bp.route("/batches/<int:batch_id>")
def view_batch(batch_id):
    batch = db.get_or_404(Batch, batch_id)

    history = audit.history_for("batch", batch.id)
    for line in batch.lines:
        history.extend(audit.history_for("line_item", line.id))
    if batch.attendance is not None:
        history.extend(audit.history_for("attendance", batch.attendance.id))
    history.sort(key=lambda entry: (entry.changed_at, entry.id), reverse=True)

    return render_template(
        "batches/detail.html",
        batch=batch,
        history=history,
        attendance_rows=_attendance_rows(batch),
    )


def _attendance_rows(batch):
    if batch.attendance is None:
        return []
    rows = []
    for field in attendance.FIELDS:
        stored = getattr(batch.attendance, field.name)
        if not stored:
            continue
        shown = attendance.display_value(field, stored)
        if field.kind == attendance.KIND_MONEY:
            shown = f"${shown}"
        rows.append((field.label, shown))
    return rows
