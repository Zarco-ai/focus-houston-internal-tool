"""The intake timer, the backup slider, and saving the intake survey.

The timer is timestamps on the server. Elapsed time is always worked out
from them:

    elapsed = (stopped_at or now) - started_at - paused_seconds
              - (now - paused_at, while paused)

so the page can be refreshed, the phone can sleep, and the program director can pick
the intake up on another device: every button is a POST that changes the
row, and every page reads the row.

Three things are computed, never stored (PRODUCTION_DESIGN 7.3):

  final_minutes      the slider if it was set, else the timer, in minutes
  duration_source    "timer", "timer_corrected" (both) or "manual" (slider only)
  needs_confirmation ran longer than intake_flag_hours (3) or past midnight,
                     and nobody has confirmed or corrected it

Only the person who started an intake works its timer -- or an admin, for
one left running by someone who has gone home. The views enforce that.
"""

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
    """Something the person asked for can't be done. The message says why."""


def _actor():
    user_id = audit.current_actor.get()
    if user_id is None:
        raise audit.NoActor("An intake needs a signed-in person.")
    return user_id


# --- The setting ------------------------------------------------------------------


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


# --- Finding sessions ------------------------------------------------------------------


def active_session_for(user_id):
    """This person's running or paused timer, if any (at most one)."""
    return IntakeSession.query.filter(
        IntakeSession.started_by_id == user_id,
        IntakeSession.status.in_(ACTIVE_STATUSES),
    ).one_or_none()


def unfinished_sessions_for(user_id):
    """This person's intakes with no survey yet that weren't discarded,
    newest first: the one being worked on, and any left behind."""
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
    """Every intake anyone ran that is flagged, oldest first."""
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


# --- The timer -----------------------------------------------------------------------


def start():
    """Start a timer for the signed-in person. One at a time per person."""
    user_id = _actor()
    if active_session_for(user_id) is not None:
        raise IntakeError("You already have an intake timer going. Stop it before starting another.")
    session = IntakeSession(started_by_id=user_id, status=STATUS_RUNNING,
                            started_at=clock.now_utc(), paused_seconds=0)
    db.session.add(session)
    try:
        db.session.flush()
    except IntegrityError:
        # Two taps at once, on two devices: the database's index caught it.
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
    """Stop the timer and freeze its value. Stopping twice changes nothing."""
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
    """Throw away an intake started by mistake. Not once a survey is saved.
    Any answers typed and not saved go with it."""
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
    """Seconds the timer has counted. Frozen once stopped."""
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


# --- Answers typed but not saved yet ----------------------------------------------------

# Every survey box, and the most a draft keeps of it. Nothing here is
# checked -- a draft is whatever was typed, half-finished included; the
# checks run when the survey is saved. The caps only stop a draft growing
# past what the survey itself would accept.
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
    """Keep the survey answers that came along with a timer button.

    Staging found the bug this fixes: Pause, Resume and Stop each reload the
    page, and answers typed but not saved were lost. Each button now submits
    the survey form's boxes too, and they are kept on the session until the
    survey is saved or the intake discarded. A request without the survey's
    boxes leaves the draft alone.
    """
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
    # The slider always sends a value; keep it only if its box was ticked,
    # or an untouched slider would come back looking like a correction.
    if form.get("use_slider"):
        draft["use_slider"] = "1"
        draft["slider_minutes"] = (form.get("slider_minutes") or "")[:3]
    session.survey_draft = draft or None
    db.session.flush()


class DraftAnswers:
    """Kept answers, read the way the page reads a submitted form:
    get(name, default) and getlist(name)."""

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


# --- The slider, and what the intake took ----------------------------------------------


def parse_slider(raw):
    """The slider's value, or None. 0..180 in steps of 15."""
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
    """Correct the time by hand. Both raw values are kept; once the intake
    is stopped the change is audited."""
    minutes = parse_slider(minutes)
    if session.is_stopped:
        audit.apply_change(session, "slider_minutes", minutes, table_name="intake_session",
                           description=f"Intake {session.id} time corrected (minutes)")
    else:
        session.slider_minutes = minutes
    db.session.flush()


def final_minutes(session):
    """The slider if set, else the timer rounded to the minute, else None."""
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
    """Ran longer than the flag setting, or across local midnight, and
    nobody has confirmed it or typed a corrected time.

    A forgotten timer must not count as a nine-hour intake on its own say-so.
    """
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
    """Someone looked at a flagged time and says it is right."""
    if not session.is_stopped:
        raise IntakeError("Stop the timer before confirming its time.")
    session.confirmed_by_id = _actor()
    session.confirmed_at = clock.now_utc()
    audit.record_change(table_name="intake_session", record_id=session.id, field="confirmed",
                        old_value=None, new_value=f"{final_minutes(session)} minutes",
                        description=f"Intake {session.id} long time confirmed")
    db.session.flush()


def start_batch(session):
    """Open a batch for this intake, service type Intake, linked to it."""
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


# --- The survey ------------------------------------------------------------------------


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
    """Save an intake survey: the participant, the survey and her first
    visit, together. Raises IntakeError (with nothing written) if anything
    is wrong.

    With a session, its timer is stopped and the survey linked to it. With
    no session but a slider time, a "typed" session is made to hold it.
    With neither, the intake has no time, which is allowed: a mother being
    re-carded months after joining has no intake to time.
    """
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
        # The answers now live in the survey; the draft has done its job.
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


# --- The qualities checklist -------------------------------------------------------------


QUALITY_MAX = 100


# A-Z, ignoring capitals. The list is short enough that alphabetical is
# the order anyone would look for, and it needs no upkeep.
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



# --- Intake time, for Program Health (Phase 7) -------------------------------------------


def counted_intake_minutes(start, end):
    """Final minutes of every intake whose survey is dated in the period
    and whose time counts: it has one, and it isn't waiting to be checked.
    A forgotten timer doesn't count until someone confirms or corrects it."""
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
    """Intakes in the period left out because their time needs checking."""
    return sum(
        1 for survey in IntakeSurvey.query.filter(
            IntakeSurvey.survey_date >= start, IntakeSurvey.survey_date <= end,
            IntakeSurvey.intake_session_id.isnot(None))
        if needs_confirmation(survey.session)
    )
