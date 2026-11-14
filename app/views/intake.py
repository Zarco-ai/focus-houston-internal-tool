
from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for

from app.extensions import db
from app.models import IntakeSession
from app.models.intake import SLIDER_MAX, SLIDER_STEP
from app.services import intake, participants

intake_bp = Blueprint("intake", __name__)


@intake_bp.app_context_processor
def intake_banner():
    user = g.get("user")
    if user is None or not user.is_admin or request.endpoint in (None, "static"):
        return {"intake_banner": None}
    session = intake.active_session_for(user.id)
    if session is None:
        return {"intake_banner": None}
    return {"intake_banner": {
        "session": session,
        "seconds": intake.elapsed_seconds(session),
        "on_intake_page": request.blueprint == "intake",
    }}


@intake_bp.route("/intake")
def page():
    unfinished = intake.unfinished_sessions_for(g.user.id)
    current = _chosen_session(unfinished)
    form = intake.draft_answers(current) if current is not None and current.survey_draft else request.args
    return _render(current, unfinished, form)


def _render(current, unfinished, form, status=200):
    user = g.user
    return render_template(
        "intake/intake.html",
        current=current,
        others=[s for s in unfinished if current is None or s.id != current.id],
        flagged=[s for s in intake.sessions_needing_confirmation()
                 if s.started_by_id == user.id or user.is_admin],
        card_status=participants.card_list_status(),
        qualities=intake.active_qualities(),
        recent=intake.recent_surveys(),
        elapsed=intake.elapsed_seconds,
        final_minutes=intake.final_minutes,
        duration_source=intake.duration_source,
        source_labels=intake.SOURCE_LABELS,
        needs_confirmation=intake.needs_confirmation,
        flag_hours=intake.flag_hours(),
        slider_max=SLIDER_MAX,
        slider_step=SLIDER_STEP,
        form=form,
    ), status


def _chosen_session(unfinished):
    wanted = request.args.get("session", type=int)
    if wanted is not None:
        for session in unfinished:
            if session.id == wanted:
                return session
    for session in unfinished:
        if session.is_active:
            return session
    return unfinished[0] if unfinished else None


def _session_or_403(session_id):
    session = db.get_or_404(IntakeSession, session_id)
    if session.started_by_id != g.user.id and not g.user.is_admin:
        abort(403)
    return session


def _back(session=None):
    if session is not None and session.status != "discarded":
        return redirect(url_for("intake.page", session=session.id))
    return redirect(url_for("intake.page"))


@intake_bp.route("/intake/start", methods=["POST"])
def start():
    try:
        session = intake.start()
    except intake.IntakeError as error:
        flash(str(error), "error")
        return _back()
    intake.keep_draft(session, request.form)
    db.session.commit()
    return _back(session)


def _timer_action(name, action, message=None):
    def view(session_id):
        session = _session_or_403(session_id)
        intake.keep_draft(session, request.form)
        try:
            action(session)
        except intake.IntakeError as error:
            db.session.commit()
            flash(str(error), "error")
            return _back(session)
        db.session.commit()
        if message:
            flash(message, "success")
        return _back(session)

    view.__name__ = name
    intake_bp.add_url_rule(f"/intake/<int:session_id>/{name}", name, view, methods=["POST"])


_timer_action("pause", intake.pause)
_timer_action("resume", intake.resume)
_timer_action("stop", intake.stop, "Timer stopped.")
_timer_action("discard", intake.discard, "Intake discarded.")
_timer_action("confirm", intake.confirm, "Time confirmed.")


@intake_bp.route("/intake/<int:session_id>/time", methods=["POST"])
def correct_time(session_id):
    session = _session_or_403(session_id)
    try:
        intake.set_slider(session, request.form.get("slider_minutes"))
    except intake.IntakeError as error:
        flash(str(error), "error")
        return _back(session)
    db.session.commit()
    flash("Time corrected.", "success")
    return redirect(url_for("intake.page"))


@intake_bp.route("/intake/<int:session_id>/batch", methods=["POST"])
def start_batch(session_id):
    session = _session_or_403(session_id)
    intake.keep_draft(session, request.form)
    try:
        batch = intake.start_batch(session)
    except intake.IntakeError as error:
        db.session.commit()
        flash(str(error), "error")
        return _back(session)
    db.session.commit()
    return redirect(url_for("entry.entry_screen", batch_id=batch.id))


@intake_bp.route("/intake/survey", methods=["POST"])
def save_survey():
    form = request.form
    session = None
    session_id = form.get("session_id", type=int)
    if session_id:
        session = _session_or_403(session_id)

    try:
        survey = intake.save_survey(
            code=form.get("card_number"),
            survey_date=form.get("survey_date"),
            reason=form.get("reason_for_joining"),
            score=form.get("come_back_score"),
            score_reason=form.get("come_back_reason"),
            quality_ids=form.getlist("quality"),
            qualities_note=form.get("qualities_note"),
            session=session,
            slider_minutes=form.get("slider_minutes") if form.get("use_slider") else None,
        )
    except intake.IntakeError as error:
        db.session.rollback()
        flash(str(error), "error")
        flash("The survey was not saved. Your answers are still in the form below.", "error")
        return _render_with_answers(session)

    db.session.commit()
    flash(f"Intake saved for card {survey.participant.code}. Her 60-day clock starts "
          f"{survey.survey_date.strftime('%b %-d')}.", "success")
    return redirect(url_for("intake.page"))


def _render_with_answers(session):
    unfinished = intake.unfinished_sessions_for(g.user.id)
    current = session if session is not None and session.status != "discarded" else (
        unfinished[0] if unfinished else None)
    return _render(current, unfinished, request.form, status=400)
