
import datetime
from decimal import ROUND_HALF_UP, Decimal

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models import (
    IntakeSession,
    IntakeSurvey,
    QualityOption,
    ServiceType,
    get_setting,
    set_setting,
)
from app.models.audit import ACTION_CREATE
from app.models.intake import (
    ACTIVE_STATUSES,
    NOTE_MAX,
    REASON_MAX,
    SLIDER_MAX,
    SLIDER_STEP,
    STATUS_DISCARDED,
    STATUS_PAUSED,
    STATUS_RUNNING,
    STATUS_STOPPED,
)
from app.models.participant import VISIT_INTAKE
from app.services import audit, batches, clock, participants

FLAG_HOURS_KEY = "intake_flag_hours"
DEFAULT_FLAG_HOURS = 3
INTAKE_SERVICE_TYPE = "Intake"

SOURCE_TIMER = "timer"
SOURCE_TIMER_CORRECTED = "timer_corrected"
SOURCE_MANUAL = "manual"

SOURCE_LABELS = {
    SOURCE_TIMER: "timer",
    SOURCE_TIMER_CORRECTED: "timer, corrected",
    SOURCE_MANUAL: "typed",
}


class IntakeError(Exception):
    pass


def _actor():
    user_id = audit.current_actor.get()
    if user_id is None:
        raise audit.NoActor("An intake needs a signed-in person.")
    return user_id


def flag_hours():
    try:
        hours = int(get_setting(FLAG_HOURS_KEY, DEFAULT_FLAG_HOURS))
    except (TypeError, ValueError):
        return DEFAULT_FLAG_HOURS
    return hours if 1 <= hours <= 12 else DEFAULT_FLAG_HOURS


def set_flag_hours(hours):
    try:
        hours = int(str(hours).strip())
    except (TypeError, ValueError):
        raise IntakeError("Type a whole number of hours, 1 to 12.")
    if not 1 <= hours <= 12:
        raise IntakeError("Type a whole number of hours, 1 to 12.")
    old = flag_hours()
    if hours != old:
        audit.record_change(table_name="app_setting", record_id=0, field=FLAG_HOURS_KEY,
                            old_value=old, new_value=hours,
                            description="Intake timers flagged after (hours)")
        set_setting(FLAG_HOURS_KEY, hours)
    return hours


def active_session_for(user_id):
    return IntakeSession.query.filter(
        IntakeSession.started_by_id == user_id,
        IntakeSession.status.in_(ACTIVE_STATUSES),
    ).one_or_none()


def unfinished_sessions_for(user_id):
    return (
        IntakeSession.query.outerjoin(IntakeSurvey)
        .filter(
            IntakeSession.started_by_id == user_id,
            IntakeSession.status != STATUS_DISCARDED,
            IntakeSurvey.id.is_(None),
        )
        .order_by(IntakeSession.started_at.desc(), IntakeSession.id.desc())
        .all()
    )


def sessions_needing_confirmation():
    candidates = IntakeSession.query.filter(
        IntakeSession.status != STATUS_DISCARDED,
        IntakeSession.confirmed_at.is_(None),
        IntakeSession.slider_minutes.is_(None),
    ).order_by(IntakeSession.started_at).all()
    return [session for session in candidates if needs_confirmation(session)]


def recent_surveys(limit=20):
    return (
        IntakeSurvey.query.order_by(IntakeSurvey.survey_date.desc(), IntakeSurvey.id.desc())
        .limit(limit).all()
    )


def start():
    user_id = _actor()
    if active_session_for(user_id) is not None:
        raise IntakeError("You already have an intake timer going. Stop it before starting another.")
    session = IntakeSession(started_by_id=user_id, status=STATUS_RUNNING,
                            started_at=clock.now_utc(), paused_seconds=0)
    db.session.add(session)
    try:
        db.session.flush()
    except IntegrityError:
        db.session.rollback()
        raise IntakeError("You already have an intake timer going. Stop it before starting another.")
    return session


def pause(session):
    if not session.is_running:
        raise IntakeError("That timer isn't running.")
    session.paused_at = clock.now_utc()
    session.status = STATUS_PAUSED
    db.session.flush()


def resume(session):
    if not session.is_paused:
        raise IntakeError("That timer isn't paused.")
    _fold_in_pause(session, clock.now_utc())
    session.status = STATUS_RUNNING
    db.session.flush()


def stop(session):
    if session.is_stopped:
        return
    if not session.is_active:
        raise IntakeError("That intake was discarded.")
    now = clock.now_utc()
    if session.is_paused:
        _fold_in_pause(session, now)
    session.stopped_at = now
    session.timer_seconds = elapsed_seconds(session, now)
    session.status = STATUS_STOPPED
    db.session.flush()


def discard(session):
    if session.survey is not None:
        raise IntakeError("This intake has a saved survey, so it can't be discarded.")
    session.survey_draft = None
    session.status = STATUS_DISCARDED
    session.paused_at = None
    if session.stopped_at is None:
        session.stopped_at = clock.now_utc()
    db.session.flush()


def _fold_in_pause(session, now):
    session.paused_seconds += max(0, int((now - session.paused_at).total_seconds()))
    session.paused_at = None


def elapsed_seconds(session, now=None):
    if session.status == STATUS_STOPPED and session.timer_seconds is not None:
        return session.timer_seconds
    if session.status == STATUS_STOPPED or session.started_at is None:
        return 0
    now = now or clock.now_utc()
    end = session.stopped_at or now
    seconds = (end - session.started_at).total_seconds() - session.paused_seconds
    if session.paused_at is not None:
        seconds -= (end - session.paused_at).total_seconds()
    return max(0, int(seconds))


DRAFT_TEXT_FIELDS = {
    "card_number": 12,
    "survey_date": 10,
    "reason_for_joining": REASON_MAX,
    "come_back_score": 2,
    "come_back_reason": REASON_MAX,
    "qualities_note": NOTE_MAX,
}
DRAFT_MAX_QUALITIES = 100
SURVEY_FORM_MARKER = "survey_form"


def keep_draft(session, form):
    if form.get(SURVEY_FORM_MARKER) != "1":
        return
    draft = {}
    for name, limit in DRAFT_TEXT_FIELDS.items():
        value = (form.get(name) or "").strip()[:limit]
        if value:
            draft[name] = value
    qualities = [q for q in form.getlist("quality") if q.isascii() and q.isdigit()]
    if qualities:
        draft["quality"] = qualities[:DRAFT_MAX_QUALITIES]
    if form.get("use_slider"):
        draft["use_slider"] = "1"
        draft["slider_minutes"] = (form.get("slider_minutes") or "")[:3]
    session.survey_draft = draft or None
    db.session.flush()


class DraftAnswers:

    def __init__(self, draft):
        self._draft = draft or {}

    def get(self, name, default=None):
        value = self._draft.get(name, default)
        return value[0] if isinstance(value, list) and value else value

    def getlist(self, name):
        value = self._draft.get(name)
        if value is None:
            return []
        return list(value) if isinstance(value, list) else [value]

    def __bool__(self):
        return bool(self._draft)


def draft_answers(session):
    return DraftAnswers(session.survey_draft if session is not None else None)


def parse_slider(raw):
    if raw in (None, ""):
        return None
    try:
        minutes = int(raw)
    except (TypeError, ValueError):
        raise IntakeError("The time slider sent something that isn't a number of minutes.")
    if not 0 <= minutes <= SLIDER_MAX or minutes % SLIDER_STEP:
        raise IntakeError(f"The time must be 0 to {SLIDER_MAX} minutes in steps of {SLIDER_STEP}.")
    return minutes


def set_slider(session, minutes):
    minutes = parse_slider(minutes)
    if session.is_stopped:
        audit.apply_change(session, "slider_minutes", minutes, table_name="intake_session",
                           description=f"Intake {session.id} time corrected (minutes)")
    else:
        session.slider_minutes = minutes
    db.session.flush()


def final_minutes(session):
    if session.slider_minutes is not None:
        return session.slider_minutes
    if session.timer_seconds is not None:
        return int((Decimal(session.timer_seconds) / 60).quantize(Decimal("1"), ROUND_HALF_UP))
    return None


def duration_source(session):
    if session.slider_minutes is not None:
        return SOURCE_TIMER_CORRECTED if session.timer_seconds is not None else SOURCE_MANUAL
    if session.timer_seconds is not None:
        return SOURCE_TIMER
    return None


def needs_confirmation(session, now=None):
    if session.status == STATUS_DISCARDED or session.confirmed_at is not None:
        return False
    if session.slider_minutes is not None:
        return False
    now = now or clock.now_utc()
    if elapsed_seconds(session, now) > flag_hours() * 3600:
        return True
    end = session.stopped_at or now
    return clock.to_local(session.started_at).date() != clock.to_local(end).date()


def confirm(session):
    if not session.is_stopped:
        raise IntakeError("Stop the timer before confirming its time.")
    session.confirmed_by_id = _actor()
    session.confirmed_at = clock.now_utc()
    audit.record_change(table_name="intake_session", record_id=session.id, field="confirmed",
                        old_value=None, new_value=f"{final_minutes(session)} minutes",
                        description=f"Intake {session.id} long time confirmed")
    db.session.flush()


def start_batch(session):
    if session.batch_id is not None:
        return session.batch
    service_type = ServiceType.query.filter(
        sa.func.lower(ServiceType.name) == INTAKE_SERVICE_TYPE.lower(),
        ServiceType.active.is_(True),
    ).one_or_none()
    if service_type is None:
        raise IntakeError(
            "There is no active service type called Intake. An admin can add or "
            "switch it back on under Settings → Service types."
        )
    batch = batches.open_batch(service_type_id=service_type.id)
    session.batch_id = batch.id
    db.session.flush()
    return batch


def _short_text(raw, limit, label):
    text = " ".join((raw or "").split())
    if len(text) > limit:
        raise IntakeError(f"{label} can be at most {limit} characters (it has {len(text)}).")
    return text or None


def parse_survey_date(raw):
    today = clock.local_today()
    if not raw:
        return today
    try:
        day = datetime.date.fromisoformat(str(raw))
    except ValueError:
        raise IntakeError("The survey date isn't a date.")
    if day > today:
        raise IntakeError("The survey date can't be in the future.")
    if day.year < 2000:
        raise IntakeError("The survey date is too far in the past.")
    return day


def save_survey(code, survey_date=None, reason=None, score=None, score_reason=None,
                quality_ids=(), qualities_note=None, session=None, slider_minutes=None):
    user_id = _actor()
    try:
        code = participants.available_code(code)
    except ValueError as error:
        raise IntakeError(str(error))
    survey_date = parse_survey_date(survey_date)
    reason = _short_text(reason, REASON_MAX, "Reason for joining")
    score_reason = _short_text(score_reason, REASON_MAX, "The reason for the score")
    qualities_note = _short_text(qualities_note, NOTE_MAX, "The note")
    score = _parse_score(score)
    slider_minutes = parse_slider(slider_minutes)
    qualities = _qualities(quality_ids)

    if session is not None:
        if session.survey is not None:
            raise IntakeError("This intake already has a saved survey.")
        if session.status == STATUS_DISCARDED:
            raise IntakeError("This intake was discarded.")
        stop(session)
    elif slider_minutes is not None:
        now = clock.now_utc()
        session = IntakeSession(started_by_id=user_id, status=STATUS_STOPPED,
                                started_at=now, stopped_at=now, paused_seconds=0)
        db.session.add(session)
        db.session.flush()

    if session is not None:
        session.survey_draft = None
        if slider_minutes is not None:
            session.slider_minutes = slider_minutes

    try:
        participant = participants.create_participant(code, created_by_id=user_id)
    except IntegrityError:
        db.session.rollback()
        raise IntakeError(f"{code} was just given to someone else. Use the next unused number.")

    survey = IntakeSurvey(
        participant=participant, session=session, survey_date=survey_date,
        reason_for_joining=reason, come_back_score=score, come_back_reason=score_reason,
        qualities_note=qualities_note, created_by_id=user_id, qualities=qualities,
    )
    db.session.add(survey)
    db.session.flush()

    intake_type = ServiceType.query.filter(
        sa.func.lower(ServiceType.name) == INTAKE_SERVICE_TYPE.lower()).first()
    participants.add_visit(participant, survey_date, VISIT_INTAKE, survey.id,
                           service_type_id=intake_type.id if intake_type else None)

    backdated = survey_date < clock.local_today()
    audit.record_change(
        table_name="intake_survey", record_id=survey.id, field="survey_date",
        old_value=None, new_value=survey_date.isoformat(), action=ACTION_CREATE,
        description=(f"Intake survey for participant {participant.id}"
                     + (" (backdated)" if backdated else "")),
    )
    return survey


def _parse_score(raw):
    if raw in (None, ""):
        return None
    try:
        score = int(raw)
    except (TypeError, ValueError):
        raise IntakeError("The score is a number from 1 to 10.")
    if not 1 <= score <= 10:
        raise IntakeError("The score is a number from 1 to 10.")
    return score


def _qualities(ids):
    wanted = set()
    for raw in ids or ():
        try:
            wanted.add(int(raw))
        except (TypeError, ValueError):
            raise IntakeError("One of the qualities ticked isn't on the list.")
    if not wanted:
        return []
    found = QualityOption.query.filter(QualityOption.id.in_(wanted),
                                       QualityOption.active.is_(True)).order_by(
        sa.func.lower(QualityOption.label)).all()
    if len(found) != len(wanted):
        raise IntakeError("One of the qualities ticked isn't on the list any more.")
    return found


QUALITY_MAX = 100


ALPHABETICAL = sa.func.lower(QualityOption.label)


def all_qualities():
    return QualityOption.query.order_by(QualityOption.active.desc(), ALPHABETICAL).all()


def active_qualities():
    return QualityOption.query.filter_by(active=True).order_by(ALPHABETICAL).all()


def _quality_label(label, exclude_id=None):
    label = " ".join((label or "").split())
    if not label:
        raise IntakeError("A quality needs some words.")
    if len(label) > QUALITY_MAX:
        raise IntakeError(f"A quality can be at most {QUALITY_MAX} characters.")
    clash = QualityOption.query.filter(sa.func.lower(QualityOption.label) == label.lower(),
                                       QualityOption.id != (exclude_id or 0)).first()
    if clash is not None:
        raise IntakeError(f"'{clash.label}' is already on the list.")
    return label


def add_quality(label):
    option = QualityOption(label=_quality_label(label), active=True)
    db.session.add(option)
    db.session.flush()
    audit.record_change(table_name="quality_option", record_id=option.id, field="label",
                        old_value=None, new_value=option.label, action=ACTION_CREATE,
                        description="Quality added")
    return option


def update_quality(option, label=None, active=None):
    if label is not None:
        audit.apply_change(option, "label", _quality_label(label, exclude_id=option.id),
                           table_name="quality_option", description="Quality reworded")
    if active is not None:
        audit.apply_change(option, "active", bool(active), table_name="quality_option",
                           description="Quality switched on/off")
    db.session.flush()


def counted_intake_minutes(start, end):
    surveys = IntakeSurvey.query.filter(
        IntakeSurvey.survey_date >= start,
        IntakeSurvey.survey_date <= end,
        IntakeSurvey.intake_session_id.isnot(None),
    ).all()
    minutes = []
    for survey in surveys:
        session = survey.session
        value = final_minutes(session)
        if value is not None and not needs_confirmation(session):
            minutes.append(value)
    return minutes


def waiting_to_be_checked(start, end):
    return sum(
        1 for survey in IntakeSurvey.query.filter(
            IntakeSurvey.survey_date >= start, IntakeSurvey.survey_date <= end,
            IntakeSurvey.intake_session_id.isnot(None))
        if needs_confirmation(survey.session)
    )
