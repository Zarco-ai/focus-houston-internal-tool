
from sqlalchemy import CheckConstraint

from app.extensions import db
from app.services import clock

DECISION_AUTO = "auto"
DECISION_MATCHED = "matched"
DECISION_CREATED = "created"
DECISION_DISCARDED = "discarded"
DECISIONS = (DECISION_AUTO, DECISION_MATCHED, DECISION_CREATED, DECISION_DISCARDED)

TEXT_MAX = 300
RAW_ID_MAX = 50
LABEL_MAX = 200


class FormResponse(db.Model):
    __tablename__ = "form_response"
    __table_args__ = (
        CheckConstraint(
            "decision IS NULL OR decision IN ('auto', 'matched', 'created', 'discarded')",
            name="ck_form_response_decision",
        ),
        CheckConstraint("score IS NULL OR score BETWEEN 1 AND 10", name="ck_form_response_score"),
    )

    id = db.Column(db.Integer, primary_key=True)
    source_key = db.Column(db.String(64), nullable=False, unique=True)
    submitted_at = db.Column(db.DateTime(timezone=True), nullable=False, index=True)
    submitted_local_date = db.Column(db.Date, nullable=False, index=True)

    raw_id_text = db.Column(db.String(RAW_ID_MAX), nullable=True)
    returning = db.Column(db.Boolean, nullable=True)
    raw_event_label = db.Column(db.String(LABEL_MAX), nullable=True)
    reason = db.Column(db.String(TEXT_MAX), nullable=True)
    score = db.Column(db.Integer, nullable=True)
    score_reason = db.Column(db.String(TEXT_MAX), nullable=True)

    decision = db.Column(db.String(10), nullable=True, index=True)
    participant_id = db.Column(db.Integer, db.ForeignKey("participant.id"), nullable=True)
    reviewed_by_id = db.Column(db.Integer, db.ForeignKey("app_user.id"), nullable=True)
    reviewed_at = db.Column(db.DateTime(timezone=True), nullable=True)

    imported_in_sync_run_id = db.Column(db.Integer, db.ForeignKey("sync_run.id"), nullable=False)
    imported_at = db.Column(db.DateTime(timezone=True), nullable=False, default=clock.now_utc)

    participant = db.relationship("Participant")
    reviewed_by = db.relationship("AppUser")

    def __repr__(self):
        return f"<FormResponse {self.id} {self.decision or 'waiting'}>"


class FormEventMapping(db.Model):
    __tablename__ = "form_event_mapping"

    id = db.Column(db.Integer, primary_key=True)
    label = db.Column(db.String(LABEL_MAX), nullable=False, unique=True)
    service_type_id = db.Column(db.Integer, db.ForeignKey("service_type.id"), nullable=False)

    service_type = db.relationship("ServiceType")

    def __repr__(self):
        return f"<FormEventMapping {self.id}>"


class SyncRun(db.Model):
    __tablename__ = "sync_run"

    id = db.Column(db.Integer, primary_key=True)
    started_at = db.Column(db.DateTime(timezone=True), nullable=False, default=clock.now_utc)
    finished_at = db.Column(db.DateTime(timezone=True), nullable=True)
    rows_seen = db.Column(db.Integer, nullable=False, default=0)
    rows_imported = db.Column(db.Integer, nullable=False, default=0)
    rows_flagged = db.Column(db.Integer, nullable=False, default=0)
    rows_remaining = db.Column(db.Integer, nullable=False, default=0, server_default="0")
    error = db.Column(db.String(500), nullable=True)
    columns_note = db.Column(db.String(500), nullable=True)
    triggered_by_id = db.Column(db.Integer, db.ForeignKey("app_user.id"), nullable=True)

    triggered_by = db.relationship("AppUser")

    @property
    def succeeded(self):
        return self.finished_at is not None and self.error is None

    def __repr__(self):
        return f"<SyncRun {self.id}>"
