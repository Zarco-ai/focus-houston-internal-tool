
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
SLIDER_MAX = 180

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

    timer_seconds = db.Column(db.Integer, nullable=True)
    slider_minutes = db.Column(db.Integer, nullable=True)

    confirmed_by_id = db.Column(db.Integer, db.ForeignKey("app_user.id"), nullable=True)
    confirmed_at = db.Column(db.DateTime(timezone=True), nullable=True)

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
        return f"<IntakeSurvey {self.id} participant={self.participant_id}>"
