"""A filed report: exactly what was sent to the funder, frozen.

  report_snapshot  kind (monthly / annual), the period, who filed it and
                   when, the app version that produced it, the report's
                   numbers (payload) and a sha256 of them.

Reports are computed from the data every time they're opened, so they
follow corrections -- which is right for the live numbers and wrong for
"what did we send in October?". A snapshot answers that question.

A snapshot is never changed and never deleted. The model refuses both (an
event listener below), and "changed since filed" is worked out by
recomputing the live report and comparing hashes, not by marking the
snapshot. Filing again after a correction adds a new snapshot; the old one
stays as the record of what was sent before.
"""

import sqlalchemy as sa
from sqlalchemy import CheckConstraint
from sqlalchemy.dialects.postgresql import JSONB

from app.extensions import db
from app.services import clock

KIND_MONTHLY = "monthly"
KIND_ANNUAL = "annual"
KINDS = (KIND_MONTHLY, KIND_ANNUAL)


class SnapshotIsFrozen(RuntimeError):
    """Something tried to change or delete a filed report."""


class ReportSnapshot(db.Model):
    __tablename__ = "report_snapshot"
    __table_args__ = (
        CheckConstraint("kind IN ('monthly', 'annual')", name="ck_report_snapshot_kind"),
        CheckConstraint("period_start <= period_end", name="ck_report_snapshot_period"),
    )

    id = db.Column(db.Integer, primary_key=True)
    kind = db.Column(db.String(10), nullable=False)
    period_start = db.Column(db.Date, nullable=False, index=True)
    period_end = db.Column(db.Date, nullable=False)
    filed_at = db.Column(db.DateTime(timezone=True), nullable=False, default=clock.now_utc)
    filed_by_id = db.Column(db.Integer, db.ForeignKey("app_user.id"), nullable=False)
    payload = db.Column(sa.JSON().with_variant(JSONB(), "postgresql"), nullable=False)
    payload_sha256 = db.Column(db.String(64), nullable=False)
    # Render's git commit (RENDER_GIT_COMMIT), so a filed report can be
    # traced to the code that computed it.
    app_version = db.Column(db.String(40), nullable=False)

    filed_by = db.relationship("AppUser")

    def __repr__(self):
        return f"<ReportSnapshot {self.id} {self.kind} {self.period_start}>"


@sa.event.listens_for(ReportSnapshot, "before_update")
def _refuse_update(mapper, connection, target):
    raise SnapshotIsFrozen("A filed report is never changed. File a new one instead.")


@sa.event.listens_for(ReportSnapshot, "before_delete")
def _refuse_delete(mapper, connection, target):
    raise SnapshotIsFrozen("A filed report is never deleted.")
