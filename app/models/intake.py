"""Intakes: the timer, the survey, and the qualities checklist.

  intake_session         One intake's timer. Its state is timestamps on the
                         server, so a refresh, a sleeping phone or another
                         device changes nothing. Pauses are accumulated in
                         paused_seconds, not stored one row each: nothing
                         reports on individual pauses (PRODUCTION_DESIGN 7.3).
                         One running or paused timer per person, enforced by
                         a partial unique index, not just the page.

  intake_survey          What the program director records with the mother: her card
                         number, the date, why she joined, how likely she is
                         to come back and why. Saving it creates the
                         participant and her first visit.

  quality_option         The "unique qualities" checklist. The program director manages
  intake_survey_quality  the options; the link table holds the ticks.

Free text is short, labelled "no names" on the form, never logged and in no
export. The qualities are the most sensitive thing in the app: visible to
signed-in users only.

Final minutes, where the duration came from, and "needs confirmation" are
computed (app/services/intake.py), never stored: both raw values are kept,
so the derived ones can't disagree with them.
"""

import sqlalchemy as sa
from sqlalchemy import CheckConstraint

from app.extensions import db
from app.services import clock

STATUS_RUNNING = "running"
STATUS_PAUSED = "paused"
STATUS_STOPPED = "stopped"
STATUS_DISCARDED = "discarded"
STATUSES = (STATUS_RUNNING, STATUS_PAUSED, STATUS_STOPPED, STATUS_DISCARDED)
ACTIVE_STATUSES = (STATUS_RUNNING, STATUS_PAUSED)

SLIDER_STEP = 15
SLIDER_MAX = 180  # shown as "3+ hours"

REASON_MAX = 300
NOTE_MAX = 200

intake_survey_quality = db.Table(
    "intake_survey_quality",
    db.Column("intake_survey_id", db.Integer, db.ForeignKey("intake_survey.id"),
              primary_key=True),
    db.Column("quality_option_id", db.Integer, db.ForeignKey("quality_option.id"),
              primary_key=True),
)


class IntakeSession(db.Model):
    __tablename__ = "intake_session"
    __table_args__ = (
        CheckConstraint(
            "status IN ('running', 'paused', 'stopped', 'discarded')",
            name="ck_intake_session_status",
        ),
        CheckConstraint("paused_seconds >= 0", name="ck_intake_session_paused_seconds"),
        CheckConstraint("timer_seconds IS NULL OR timer_seconds >= 0",
                        name="ck_intake_session_timer_seconds"),
        CheckConstraint(
            f"slider_minutes IS NULL OR (slider_minutes BETWEEN 0 AND {SLIDER_MAX} "
            f"AND mod(slider_minutes, {SLIDER_STEP}) = 0)",
            name="ck_intake_session_slider",
        ),
        # Phase 0 answer 3: one timer per person. The page checks too, but
        # two taps on two devices at once would get past a page check.
        sa.Index(
            "uq_intake_session_one_active_per_person",
            "started_by_id",
            unique=True,
            postgresql_where=sa.text("status IN ('running', 'paused')"),
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    started_by_id = db.Column(db.Integer, db.ForeignKey("app_user.id"), nullable=False)
    batch_id = db.Column(db.Integer, db.ForeignKey("batch.id"), nullable=True)

    status = db.Column(db.String(10), nullable=False, default=STATUS_RUNNING)
    started_at = db.Column(db.DateTime(timezone=True), nullable=False, default=clock.now_utc)
    stopped_at = db.Column(db.DateTime(timezone=True), nullable=True)
    paused_at = db.Column(db.DateTime(timezone=True), nullable=True)
    paused_seconds = db.Column(db.Integer, nullable=False, default=0, server_default="0")

    # The two raw values. NULL timer_seconds means the timer wasn't used
    # (a time typed afterwards with the slider); NULL slider_minutes means
    # nobody corrected the timer.
    timer_seconds = db.Column(db.Integer, nullable=True)
    slider_minutes = db.Column(db.Integer, nullable=True)

    confirmed_by_id = db.Column(db.Integer, db.ForeignKey("app_user.id"), nullable=True)
    confirmed_at = db.Column(db.DateTime(timezone=True), nullable=True)

    # The survey answers typed so far but not yet saved, carried by every
    # timer button so stopping the timer doesn't wipe them. Cleared when the
    # survey is saved (the answers move into intake_survey) or the intake is
    # discarded. Same "no names" rule as the survey itself.
    survey_draft = db.Column(db.JSON, nullable=True)

    started_by = db.relationship("AppUser", foreign_keys=[started_by_id])
    confirmed_by = db.relationship("AppUser", foreign_keys=[confirmed_by_id])
    batch = db.relationship("Batch")
    survey = db.relationship("IntakeSurvey", back_populates="session", uselist=False)

    @property
    def is_active(self):
        return self.status in ACTIVE_STATUSES

    @property
    def is_running(self):
        return self.status == STATUS_RUNNING

    @property
    def is_paused(self):
        return self.status == STATUS_PAUSED

    @property
    def is_stopped(self):
        return self.status == STATUS_STOPPED

    def __repr__(self):
        return f"<IntakeSession {self.id} {self.status}>"


class QualityOption(db.Model):
    __tablename__ = "quality_option"

    id = db.Column(db.Integer, primary_key=True)
    label = db.Column(db.String(100), nullable=False, unique=True)
    active = db.Column(db.Boolean, nullable=False, default=True)

    def __repr__(self):
        return f"<QualityOption {self.id}>"


class IntakeSurvey(db.Model):
    __tablename__ = "intake_survey"
    __table_args__ = (
        CheckConstraint("come_back_score IS NULL OR come_back_score BETWEEN 1 AND 10",
                        name="ck_intake_survey_score"),
    )

    id = db.Column(db.Integer, primary_key=True)
    participant_id = db.Column(db.Integer, db.ForeignKey("participant.id"),
                               nullable=False, index=True)
    intake_session_id = db.Column(db.Integer, db.ForeignKey("intake_session.id"),
                                  nullable=True, unique=True)
    survey_date = db.Column(db.Date, nullable=False)

    reason_for_joining = db.Column(db.String(REASON_MAX), nullable=True)
    come_back_score = db.Column(db.Integer, nullable=True)
    come_back_reason = db.Column(db.String(REASON_MAX), nullable=True)
    qualities_note = db.Column(db.String(NOTE_MAX), nullable=True)

    created_by_id = db.Column(db.Integer, db.ForeignKey("app_user.id"), nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=clock.now_utc)

    participant = db.relationship("Participant")
    session = db.relationship("IntakeSession", back_populates="survey")
    created_by = db.relationship("AppUser")
    qualities = db.relationship("QualityOption", secondary=intake_survey_quality,
                                order_by=sa.func.lower(QualityOption.label))

    def __repr__(self):
        # Ids only. Never the free text, never the code.
        return f"<IntakeSurvey {self.id} participant={self.participant_id}>"
