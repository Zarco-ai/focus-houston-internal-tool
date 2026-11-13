"""The intake timer, the slider, and the survey (Phase 5).

Time is frozen through the clock module, so "three hours later" and "past
midnight in Houston" are exact.
"""

import datetime
import random

import pytest
import sqlalchemy as sa

from app.extensions import db
from app.models import (
    AuditLog,
    IntakeSession,
    IntakeSurvey,
    Participant,
    ParticipantVisit,
    QualityOption,
)
from app.services import audit, card_numbers, clock, intake, participants

HOUSTON = clock.HOUSTON


def at(hour, minute=0, second=0, day=14):
    """A moment on 14 October 2026 in Houston."""
    return datetime.datetime(2026, 10, day, hour, minute, second, tzinfo=HOUSTON)


@pytest.fixture
def cards(seeded_app):
    codes = card_numbers.generate(10, rng=random.Random(5))
    text = "list_position,card_number,batch\n" + "".join(
        f"{i},{code},1\n" for i, code in enumerate(codes, start=1))
    participants.load_card_list(text)
    db.session.commit()
    return codes


# --- The timer --------------------------------------------------------------------------


def test_elapsed_time_leaves_out_pauses(seeded_app):
    with clock.freeze(at(10)):
        session = intake.start()
    with clock.freeze(at(10, 10)):
        intake.pause(session)
    with clock.freeze(at(10, 25)):
        assert intake.elapsed_seconds(session) == 600       # frozen while paused
        intake.resume(session)
    with clock.freeze(at(10, 30)):
        intake.stop(session)

    assert session.timer_seconds == 15 * 60
    assert session.paused_seconds == 15 * 60
    assert intake.final_minutes(session) == 15
    assert intake.duration_source(session) == intake.SOURCE_TIMER


def test_stopping_while_paused_counts_the_pause_out(seeded_app):
    with clock.freeze(at(10)):
        session = intake.start()
    with clock.freeze(at(10, 20)):
        intake.pause(session)
    with clock.freeze(at(11)):
        intake.stop(session)
    assert session.timer_seconds == 20 * 60


def test_the_timer_lives_on_the_server_and_survives_a_new_request(client, seeded_app):
    """Start it in one request; any later request -- a refresh, another
    device -- reads the same row and the same elapsed time."""
    # Relative to now: a frozen date before the test's sign-in would read
    # as an expired session.
    start = clock.now_utc() + datetime.timedelta(seconds=1)
    with clock.freeze(start):
        client.post("/intake/start")
    with clock.freeze(start + datetime.timedelta(minutes=12, seconds=5)):
        page = client.get("/intake").get_data(as_text=True)
        other_page = client.get("/totals").get_data(as_text=True)

    assert "0:12:05" in page
    assert "0:12:05" in other_page            # the banner, on another page
    assert "Back to intake" in other_page
    assert IntakeSession.query.one().is_running


def test_one_timer_per_person(seeded_app):
    intake.start()
    with pytest.raises(intake.IntakeError):
        intake.start()


def test_the_database_itself_refuses_a_second_running_timer(seeded_app, admin_user):
    """Two taps on two devices at once would get past the page's check."""
    now = clock.now_utc()
    db.session.add(IntakeSession(started_by_id=admin_user.id, status="running", started_at=now))
    db.session.flush()
    db.session.add(IntakeSession(started_by_id=admin_user.id, status="paused", started_at=now,
                                 paused_at=now))
    with pytest.raises(sa.exc.IntegrityError):
        db.session.flush()
    db.session.rollback()


def test_two_people_can_each_run_one(seeded_app, staff_user):
    intake.start()
    with audit.acting_as(staff_user.id):
        intake.start()
    assert IntakeSession.query.count() == 2


# --- Flagged times -------------------------------------------------------------------------


def test_exactly_three_hours_is_not_flagged_and_a_second_more_is(seeded_app):
    with clock.freeze(at(9)):
        session = intake.start()
    with clock.freeze(at(12)):
        assert not intake.needs_confirmation(session)
    with clock.freeze(at(12, 0, 1)):
        assert intake.needs_confirmation(session)


def test_a_timer_left_running_past_midnight_is_flagged(seeded_app):
    with clock.freeze(at(23, 50)):
        session = intake.start()
    with clock.freeze(at(0, 5, day=15)):
        intake.stop(session)
    assert session.timer_seconds == 15 * 60
    assert intake.needs_confirmation(session)


def test_midnight_is_houstons_not_the_servers(seeded_app):
    """19:30 to 20:30 in Houston crosses midnight UTC, and is not flagged."""
    with clock.freeze(at(19, 30)):
        session = intake.start()
    with clock.freeze(at(20, 30)):
        intake.stop(session)
    assert not intake.needs_confirmation(session)


def test_a_flagged_time_is_cleared_by_confirming_or_correcting(seeded_app):
    with clock.freeze(at(8)):
        first = intake.start()
    with clock.freeze(at(17)):
        intake.stop(first)
        assert intake.needs_confirmation(first)
        intake.confirm(first)
    assert not intake.needs_confirmation(first)
    assert intake.final_minutes(first) == 9 * 60

    with clock.freeze(at(8, day=16)):
        second = intake.start()
    with clock.freeze(at(17, day=16)):
        intake.stop(second)
        intake.set_slider(second, 45)
    assert not intake.needs_confirmation(second)
    assert intake.final_minutes(second) == 45


def test_the_flag_setting_is_used_and_audited(seeded_app):
    intake.set_flag_hours(1)
    db.session.commit()
    with clock.freeze(at(9)):
        session = intake.start()
    with clock.freeze(at(10, 1)):
        assert intake.needs_confirmation(session)
    assert AuditLog.query.filter_by(field="intake_flag_hours").one().new_value == "1"

    with pytest.raises(intake.IntakeError):
        intake.set_flag_hours(0)


# --- The slider -------------------------------------------------------------------------------


def test_a_slider_correction_keeps_both_raw_values(seeded_app):
    with clock.freeze(at(10)):
        session = intake.start()
    with clock.freeze(at(10, 22)):
        intake.stop(session)
    intake.set_slider(session, 30)

    assert (session.timer_seconds, session.slider_minutes) == (22 * 60, 30)
    assert intake.final_minutes(session) == 30
    assert intake.duration_source(session) == intake.SOURCE_TIMER_CORRECTED
    assert AuditLog.query.filter_by(table_name="intake_session", field="slider_minutes").count() == 1


@pytest.mark.parametrize("value", [-15, 10, 195, "abc"])
def test_the_slider_only_takes_quarter_hours_up_to_three(seeded_app, value):
    session = intake.start()
    with pytest.raises(intake.IntakeError):
        intake.set_slider(session, value)


def test_the_timer_is_rounded_to_the_nearest_minute(seeded_app):
    session = IntakeSession(timer_seconds=89, slider_minutes=None)
    assert intake.final_minutes(session) == 1
    session.timer_seconds = 90
    assert intake.final_minutes(session) == 2


# --- The survey ----------------------------------------------------------------------------------


def test_saving_the_survey_creates_her_visit_and_stops_the_timer(seeded_app, cards):
    with clock.freeze(at(10)):
        session = intake.start()
    with clock.freeze(at(10, 40)):
        survey = intake.save_survey(cards[0], reason="New baby", score="9",
                                    score_reason="Kind people", session=session)
        db.session.commit()

    participant = Participant.query.one()
    visit = ParticipantVisit.query.one()
    assert participant.code == cards[0]
    assert (visit.participant_id, visit.visit_date, visit.source) == (
        participant.id, datetime.date(2026, 10, 14), "intake")
    assert visit.source_ref == str(survey.id)
    assert session.is_stopped and session.timer_seconds == 40 * 60
    assert survey.session is session


def test_a_survey_with_a_slider_and_no_timer_records_a_typed_time(seeded_app, cards):
    survey = intake.save_survey(cards[0], slider_minutes="45")
    assert intake.final_minutes(survey.session) == 45
    assert intake.duration_source(survey.session) == intake.SOURCE_MANUAL


def test_a_survey_can_have_no_time(seeded_app, cards):
    """A mother re-carded long after she joined has no intake to time."""
    survey = intake.save_survey(cards[0], survey_date="2026-03-02")
    assert survey.session is None


def test_a_backdated_survey_dates_the_visit_and_is_audited(seeded_app, cards):
    with clock.freeze(at(12)):
        survey = intake.save_survey(cards[0], survey_date="2026-10-01")
        db.session.commit()
    assert ParticipantVisit.query.one().visit_date == datetime.date(2026, 10, 1)
    row = AuditLog.query.filter_by(table_name="intake_survey", record_id=survey.id).one()
    assert "backdated" in row.description


def test_a_future_date_is_refused(seeded_app, cards):
    with clock.freeze(at(23, 30)):
        with pytest.raises(intake.IntakeError, match="future"):
            intake.save_survey(cards[0], survey_date="2026-10-15")
        # 23:30 in Houston is still the 14th, whatever UTC says.
        assert intake.save_survey(cards[1]).survey_date == datetime.date(2026, 10, 14)


def test_a_number_already_given_out_is_refused_and_nothing_is_written(seeded_app, cards):
    intake.save_survey(cards[0])
    db.session.commit()
    session = intake.start()
    db.session.commit()

    with pytest.raises(intake.IntakeError, match="already been given"):
        intake.save_survey(cards[0], session=session)
    db.session.rollback()

    assert Participant.query.count() == 1 and IntakeSurvey.query.count() == 1
    assert db.session.get(IntakeSession, session.id).is_running


@pytest.mark.parametrize("field, value, message", [
    ("reason", "x" * 301, "at most 300"),
    ("qualities_note", "x" * 201, "at most 200"),
    ("score", "11", "1 to 10"),
    ("score", "nine", "1 to 10"),
    ("quality_ids", ["999"], "isn't on the list"),
])
def test_survey_answers_are_checked(seeded_app, cards, field, value, message):
    with pytest.raises(intake.IntakeError, match=message):
        intake.save_survey(cards[0], **{field: value})
    assert Participant.query.count() == 0


def test_a_survey_is_saved_once_per_intake(seeded_app, cards):
    session = intake.start()
    intake.save_survey(cards[0], session=session)
    with pytest.raises(intake.IntakeError, match="already has a saved survey"):
        intake.save_survey(cards[1], session=session)


def test_qualities_are_ticked_and_a_switched_off_one_is_refused(seeded_app, cards):
    first = intake.add_quality("First pregnancy")
    second = intake.add_quality("Recently moved")
    survey = intake.save_survey(cards[0], quality_ids=[str(first.id)])
    assert survey.qualities == [first]

    intake.update_quality(second, active=False)
    with pytest.raises(intake.IntakeError):
        intake.save_survey(cards[1], quality_ids=[str(second.id)])


def test_the_survey_tables_have_no_name_column():
    for table in (IntakeSurvey.__table__, IntakeSession.__table__, QualityOption.__table__):
        for column in table.columns:
            assert "name" not in column.name, f"{table.name}.{column.name}"


# --- The page --------------------------------------------------------------------------------


def test_the_page_saves_a_survey_end_to_end(client, seeded_app, cards):
    client.post("/intake/start")
    session = IntakeSession.query.one()
    page = client.get("/intake").get_data(as_text=True)
    assert f'value="{cards[0]}"' in page     # the next number, filled in

    response = client.post("/intake/survey", data={
        "session_id": session.id, "card_number": cards[0],
        "survey_date": clock.local_today().isoformat(),
        "reason_for_joining": "Expecting", "come_back_score": "8",
    }, follow_redirects=True)

    assert f"Intake saved for card {cards[0]}" in response.get_data(as_text=True)
    assert Participant.query.count() == 1
    assert "Back to intake" not in client.get("/totals").get_data(as_text=True)


def test_a_wrong_number_keeps_the_answers_typed(client, seeded_app, cards):
    response = client.post("/intake/survey", data={
        "card_number": "12345", "reason_for_joining": "Expecting her second",
    })
    text = response.get_data(as_text=True)
    assert response.status_code == 400
    assert "isn&#39;t a card number" in text or "isn't a card number" in text
    assert "Expecting her second" in text


def test_someone_elses_timer_is_theirs_to_stop(seeded_app, admin_user, staff_user,
                                              staff_client, client):
    with audit.acting_as(admin_user.id):
        session = intake.start()
    db.session.commit()

    assert staff_client.post(f"/intake/{session.id}/stop").status_code == 403
    assert db.session.get(IntakeSession, session.id).is_running


def test_an_admin_can_stop_a_timer_someone_forgot(seeded_app, staff_user, client):
    with audit.acting_as(staff_user.id):
        session = intake.start()
    db.session.commit()
    client.post(f"/intake/{session.id}/stop")
    assert db.session.get(IntakeSession, session.id).is_stopped


def test_intake_is_admin_only(staff_client, seeded_app, cards):
    """The client's decision, 11 Nov 2026: staff count, review and see totals."""
    assert staff_client.get("/intake").status_code == 403
    assert staff_client.post("/intake/start").status_code == 403
    assert IntakeSession.query.count() == 0
    assert 'href="/intake"' not in staff_client.get("/").get_data(as_text=True)


def test_a_batch_started_from_an_intake_is_an_intake_batch(client, seeded_app):
    client.post("/intake/start")
    session = IntakeSession.query.one()
    response = client.post(f"/intake/{session.id}/batch")

    db.session.refresh(session)
    assert session.batch.service_type.name == "Intake"
    assert response.headers["Location"].endswith(f"/batch/{session.batch_id}")


def test_discarding_an_intake_frees_the_timer(client, seeded_app):
    client.post("/intake/start")
    session = IntakeSession.query.one()
    client.post(f"/intake/{session.id}/discard")
    client.post("/intake/start")
    assert IntakeSession.query.filter_by(status="running").count() == 1


def test_a_flagged_intake_is_listed_for_checking(client, seeded_app):
    start = clock.now_utc()
    with clock.freeze(start):
        client.post("/intake/start")
    session = IntakeSession.query.one()
    with clock.freeze(start + datetime.timedelta(hours=10)):
        client.post(f"/intake/{session.id}/stop")
        page = client.get("/intake").get_data(as_text=True)
        assert "Times to check" in page

        client.post(f"/intake/{session.id}/time", data={"slider_minutes": "60"})
        assert "Times to check" not in client.get("/intake").get_data(as_text=True)


def test_the_qualities_page_is_admin_only(client, staff_client, seeded_app):
    client.post("/admin/qualities", data={"label": "First pregnancy"})
    assert QualityOption.query.one().label == "First pregnancy"
    assert staff_client.get("/admin/qualities").status_code == 403


def test_without_a_card_list_the_page_says_so(client, seeded_app):
    page = client.get("/intake").get_data(as_text=True)
    assert "No card numbers are loaded yet" in page


def test_qualities_are_listed_a_to_z_ignoring_capitals(client, seeded_app, cards):
    """The client's choice (28 Oct 2026): alphabetical, no order numbers."""
    for label in ("Recently moved", "first pregnancy", "Has other children"):
        intake.add_quality(label)
    db.session.commit()

    expected = ["first pregnancy", "Has other children", "Recently moved"]
    assert [q.label for q in intake.active_qualities()] == expected

    page = client.get("/intake").get_data(as_text=True)
    assert [page.index(label) for label in expected] == sorted(page.index(label) for label in expected)
    assert 'name="sort_order"' not in client.get("/admin/qualities").get_data(as_text=True)

    survey = intake.save_survey(cards[0], quality_ids=[str(q.id) for q in intake.active_qualities()][::-1])
    assert [q.label for q in survey.qualities] == expected


# --- Answers survive the timer buttons (staging, 28 Oct 2026) -------------------------------
#
# Found on staging: answers typed into the survey were lost when the timer
# was stopped, because each timer button reloads the page. Every timer
# button now submits the survey's boxes, kept until saved or discarded.


ANSWERS = {
    "survey_form": "1",
    "reason_for_joining": "Expecting her first",
    "come_back_score": "8",
    "come_back_reason": "Felt welcome",
    "qualities_note": "Prefers Spanish",
}


def _page(client):
    return client.get("/intake").get_data(as_text=True)


@pytest.mark.parametrize("button", ["stop", "pause"])
def test_answers_typed_before_a_timer_button_stay_on_the_page(client, seeded_app, cards, button):
    client.post("/intake/start")
    session = IntakeSession.query.one()

    client.post(f"/intake/{session.id}/{button}", data={**ANSWERS, "card_number": cards[3]})

    page = _page(client)
    for value in ("Expecting her first", "Felt welcome", "Prefers Spanish", cards[3]):
        assert value in page
    assert 'value="8" selected' in page


def test_answers_survive_pause_resume_and_stop_in_turn(client, seeded_app, cards):
    client.post("/intake/start", data=ANSWERS)
    session = IntakeSession.query.one()
    for button in ("pause", "resume", "stop"):
        client.post(f"/intake/{session.id}/{button}", data=ANSWERS)
    assert "Expecting her first" in _page(client)


def test_answers_typed_before_starting_the_timer_are_kept(client, seeded_app, cards):
    client.post("/intake/start", data=ANSWERS)
    assert "Felt welcome" in _page(client)


def test_ticked_qualities_and_a_ticked_slider_are_kept(client, seeded_app, cards):
    quality = intake.add_quality("First pregnancy")
    db.session.commit()
    client.post("/intake/start")
    session = IntakeSession.query.one()

    client.post(f"/intake/{session.id}/stop", data={
        **ANSWERS, "quality": str(quality.id), "use_slider": "1", "slider_minutes": "45"})

    page = _page(client)
    import re
    assert re.search(rf'name="quality" value="{quality.id}"\s+checked', page)
    assert 'value="45"' in page


def test_an_untouched_slider_is_not_kept_as_a_correction(seeded_app):
    session = intake.start()
    intake.keep_draft(session, _form({**ANSWERS, "slider_minutes": "30"}))
    assert "slider_minutes" not in session.survey_draft


def test_the_kept_answers_are_saved_with_the_survey_and_then_cleared(client, seeded_app, cards):
    client.post("/intake/start")
    session = IntakeSession.query.one()
    client.post(f"/intake/{session.id}/stop", data=ANSWERS)

    client.post("/intake/survey", data={**ANSWERS, "session_id": session.id,
                                         "card_number": cards[0]})

    survey = IntakeSurvey.query.one()
    assert (survey.reason_for_joining, survey.come_back_score) == ("Expecting her first", 8)
    db.session.refresh(session)
    assert session.survey_draft is None


def test_discarding_throws_the_kept_answers_away(client, seeded_app):
    client.post("/intake/start", data=ANSWERS)
    session = IntakeSession.query.one()
    client.post(f"/intake/{session.id}/discard")
    db.session.refresh(session)
    assert session.survey_draft is None


def test_kept_answers_are_capped_at_what_the_survey_accepts(seeded_app):
    session = intake.start()
    intake.keep_draft(session, _form({"survey_form": "1", "reason_for_joining": "x" * 5000}))
    assert len(session.survey_draft["reason_for_joining"]) == 300


def test_a_button_without_the_survey_leaves_the_kept_answers_alone(client, seeded_app):
    client.post("/intake/start", data=ANSWERS)
    session = IntakeSession.query.one()
    client.post(f"/intake/{session.id}/pause")   # e.g. from an old open tab
    assert "Felt welcome" in _page(client)


def test_the_timer_buttons_submit_the_survey_form(client, seeded_app):
    client.post("/intake/start")
    page = _page(client)
    assert 'id="survey-form"' in page
    for button in ("pause", "stop", "batch"):
        assert f'form="survey-form" formaction="/intake/' in page
        assert f'/{button}"' in page


def _form(data):
    from werkzeug.datastructures import MultiDict
    return MultiDict(data)
