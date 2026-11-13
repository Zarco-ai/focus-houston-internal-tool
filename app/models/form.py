"""Google Form responses, and the sync that brings them in.

  form_response       One row of the form's response Sheet, as imported.
                      Only the allowlisted columns are ever read (see
                      app/services/form_sync.py), so there is no email
                      column here and there never will be. Whether it needs
                      review is computed from what it holds and what the app
                      knows now; only a person's decision is stored.

  form_event_mapping  "What event are you here for?" answers, mapped once by
                      an admin to a service type.

  sync_run            Every sync, finished or failed, so a broken sync is
                      visible instead of silent.
"""

from sqlalchemy import CheckConstraint

from app.extensions import db
from app.services import clock

DECISION_AUTO = "auto"            # a known card number; the visit was recorded
DECISION_MATCHED = "matched"      # a reviewer matched it to a participant
DECISION_CREATED = "created"      # a reviewer created her on this number
DECISION_DISCARDED = "discarded"  # a reviewer threw it away
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
    # sha256 of the submission time and the typed ID: what makes a re-run
    # sync import nothing twice. PRODUCTION_DESIGN section 9.
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
    # A known ID with "returning: no", or a possible duplicate, is recorded
    # automatically and still listed; someone pressing "Checked" clears it.
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
    # Stored tidied (spaces collapsed); compared without regard to case.
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
    # New rows left for the next sync (form_sync.MAX_NEW_ROWS_PER_SYNC).
    rows_remaining = db.Column(db.Integer, nullable=False, default=0, server_default="0")
    # Short, and never participant data: what went wrong, not with whom.
    error = db.Column(db.String(500), nullable=True)
    # Headers only, never values: questions the app expected and didn't
    # find (a reworded question), and columns it didn't read (an email
    # column switched back on is noticed here).
    columns_note = db.Column(db.String(500), nullable=True)
    triggered_by_id = db.Column(db.Integer, db.ForeignKey("app_user.id"), nullable=True)

    triggered_by = db.relationship("AppUser")

    @property
    def succeeded(self):
        return self.finished_at is not None and self.error is None

    def __repr__(self):
        return f"<SyncRun {self.id}>"
