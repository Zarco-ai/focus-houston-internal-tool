"""Mothers in the program, known only by a card number.

  card_number        The pre-generated list (generate_cards.py). Loaded from
                     cards-batch-N.csv, never typed and never edited. Whether
                     a number has been handed out is not stored here: it is
                     "a participant has this code", so it can't drift.

  participant        One mother, as a card number and nothing else. There is
                     no name column and there must never be one -- the paper
                     sheet in the office is the only place a name meets a
                     number. tests/test_participants.py checks the columns.

  participant_visit  One day she was here. Her intake is the first; Google
                     Form responses (Phase 6) add the rest. Whether she is
                     active on a date is computed from these, never stored.
"""

from sqlalchemy import CheckConstraint, UniqueConstraint

from app.extensions import db
from app.services import clock

VISIT_INTAKE = "intake"
VISIT_GOOGLE_FORM = "google_form"
VISIT_SOURCES = (VISIT_INTAKE, VISIT_GOOGLE_FORM)


class CardNumber(db.Model):
    __tablename__ = "card_number"
    __table_args__ = (
        UniqueConstraint("batch", "list_position", name="uq_card_number_batch_position"),
        CheckConstraint("code ~ '^[1-9][0-9]{4}$'", name="ck_card_number_format"),
        CheckConstraint("list_position >= 1", name="ck_card_number_position"),
    )

    code = db.Column(db.String(5), primary_key=True)
    batch = db.Column(db.Integer, nullable=False)
    # Its row on the printed sheet. The survey suggests the next unused
    # number in this order, so the app and the paper move down together.
    list_position = db.Column(db.Integer, nullable=False)
    loaded_at = db.Column(db.DateTime(timezone=True), nullable=False, default=clock.now_utc)

    participant = db.relationship("Participant", back_populates="card", uselist=False)

    def __repr__(self):
        return f"<CardNumber batch {self.batch} #{self.list_position}>"


class Participant(db.Model):
    __tablename__ = "participant"

    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(5), db.ForeignKey("card_number.code"),
                     nullable=False, unique=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=clock.now_utc)
    created_by_id = db.Column(db.Integer, db.ForeignKey("app_user.id"), nullable=False)
    # A lost card fixed by merging two codes (PRODUCTION_DESIGN 7.2). The
    # column is here from the start; merging itself comes with the review
    # queue in Phase 6.
    merged_into_id = db.Column(db.Integer, db.ForeignKey("participant.id"), nullable=True)

    card = db.relationship("CardNumber", back_populates="participant")
    visits = db.relationship("ParticipantVisit", back_populates="participant",
                             order_by="ParticipantVisit.visit_date")

    def __repr__(self):
        # The id, never the code: reprs end up in logs and tracebacks.
        return f"<Participant {self.id}>"


class ParticipantVisit(db.Model):
    __tablename__ = "participant_visit"
    __table_args__ = (
        # An import can't make the same visit twice.
        UniqueConstraint("source", "source_ref", name="uq_participant_visit_source_ref"),
        CheckConstraint("source IN ('intake', 'google_form')", name="ck_participant_visit_source"),
    )

    id = db.Column(db.Integer, primary_key=True)
    participant_id = db.Column(db.Integer, db.ForeignKey("participant.id"),
                               nullable=False, index=True)
    visit_date = db.Column(db.Date, nullable=False, index=True)
    source = db.Column(db.String(20), nullable=False)
    service_type_id = db.Column(db.Integer, db.ForeignKey("service_type.id"), nullable=True)
    # The intake_survey.id or form_response.id this visit came from.
    source_ref = db.Column(db.String(64), nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=clock.now_utc)

    participant = db.relationship("Participant", back_populates="visits")
    service_type = db.relationship("ServiceType")

    def __repr__(self):
        return f"<ParticipantVisit participant={self.participant_id} {self.visit_date}>"
