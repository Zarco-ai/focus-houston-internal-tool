
import datetime
import json
import random

import pytest
import sqlalchemy as sa

from app.extensions import db
from app.models import (
    AuditLog,
    FormResponse,
    Participant,
    ParticipantVisit,
    ServiceType,
    SyncRun,
)
from app.services import audit, card_numbers, clock, form_review, form_sync, participants

HOUSTON = clock.HOUSTON

HEADERS = [
    "Timestamp",
    "Email Address",
    "What's your ID?",
    "Are you a returning mother?",
    "What event are you here for?",
    "What is your reason for joining us today?",
    "How likely are you to come back to join the overall program (classes, distribution "
    "days, volunteering, etc.) on a scale from 1–10?",
    "Why?",
]


def serial(local):
    return (local - form_sync.SHEETS_EPOCH).total_seconds() / 86400


def row(local, card, returning="Yes", event="Diaper Distribution", reason="Diapers",
        score=9, why="Kind", email="mom@example.com"):
    return [serial(local), email, card, returning, event, reason, score, why]


class FakeSheet:
    def __init__(self, rows=(), headers=HEADERS, timezone="America/Chicago"):
        self.headers = list(headers)
        self.rows = [list(r) for r in rows]
        self.timezone = timezone
        self.fetches = 0

    def fetch(self):
        self.fetches += 1
        return form_sync.SheetData(headers=self.headers, rows=self.rows, timezone=self.timezone)


@pytest.fixture
def cards(seeded_app):
    codes = card_numbers.generate(12, rng=random.Random(9))
    participants.load_card_list("list_position,card_number,batch\n" + "".join(
        f"{i},{c},1\n" for i, c in enumerate(codes, 1)))
    db.session.commit()
    return codes


@pytest.fixture
def known(cards, admin_user):
    people = [participants.create_participant(code, created_by_id=admin_user.id) for code in cards[:3]]
    db.session.commit()
    return people


def at(day, hour, minute=0):
    return datetime.datetime(2026, 10, day, hour, minute)


def sync(sheet, user_id=None):
    run = form_sync.run(sheet, triggered_by_id=user_id)
    return run


def test_a_known_card_number_becomes_a_visit_on_houstons_date(known, cards):
    run = sync(FakeSheet([row(at(14, 21, 30), cards[0])]))

    assert (run.rows_seen, run.rows_imported, run.error) == (1, 1, None)
    response = FormResponse.query.one()
    assert response.decision == "auto"
    assert response.submitted_local_date == datetime.date(2026, 10, 14)
    assert response.submitted_at == datetime.datetime(2026, 10, 15, 2, 30, tzinfo=datetime.timezone.utc)
    visit = ParticipantVisit.query.one()
    assert (visit.participant_id, visit.visit_date, visit.source) == (
        known[0].id, datetime.date(2026, 10, 14), "google_form")


def test_rerunning_a_sync_imports_nothing_new(known, cards):
    sheet = FakeSheet([row(at(14, 10), cards[0]), row(at(14, 11), "99999")])
    sync(sheet)
    second = sync(sheet)

    assert (second.rows_seen, second.rows_imported, second.error) == (2, 0, None)
    assert FormResponse.query.count() == 2
    assert ParticipantVisit.query.count() == 1


def test_a_deleted_row_changes_nothing(known, cards):
    sheet = FakeSheet([row(at(14, 10), cards[0]), row(at(14, 11), cards[1])])
    sync(sheet)
    del sheet.rows[0]
    sync(sheet)
    assert FormResponse.query.count() == 2


def test_an_edited_answer_keeps_the_version_first_imported(known, cards):
    sheet = FakeSheet([row(at(14, 10), cards[0], reason="First")])
    sync(sheet)
    sheet.rows[0][5] = "Edited later"
    sync(sheet)
    assert FormResponse.query.one().reason == "First"


def test_an_email_column_is_dropped_and_never_stored(known, cards):
    run = sync(FakeSheet([row(at(14, 10), cards[0], email="someone@example.com"),
                          row(at(14, 11), "12345", email="other@example.com")]))
    db.session.commit()

    assert "Not read: Email Address" in run.columns_note
    for table in db.metadata.sorted_tables:
        text_columns = [c for c in table.columns if isinstance(c.type, (sa.String, sa.Text))]
        for column in text_columns:
            leaked = db.session.execute(
                sa.select(column).where(column.like("%example.com%"))).first()
            assert leaked is None, f"{table.name}.{column.name}"


def test_an_unknown_number_never_creates_a_participant(cards):
    before = Participant.query.count()
    run = sync(FakeSheet([row(at(14, 10), cards[5]), row(at(14, 11), "12345"),
                          row(at(14, 12), "1234")]))
    assert Participant.query.count() == before
    assert ParticipantVisit.query.count() == 0
    assert run.rows_flagged == 3
    assert FormResponse.query.filter(FormResponse.decision.is_(None)).count() == 3


def test_the_queue_says_which_kind_of_unknown(known, cards):
    on_list_not_given = cards[5]
    valid_not_on_list = card_numbers.generate(1, existing=cards, rng=random.Random(77))[0]
    sync(FakeSheet([row(at(14, 10), on_list_not_given), row(at(14, 11), "12345"),
                    row(at(14, 12), valid_not_on_list)]))

    kinds = {item.response.raw_id_text: item.kinds for item in form_review.review_queue()}
    assert kinds[on_list_not_given] == [form_review.KIND_UNASSIGNED]
    assert kinds["12345"] == [form_review.KIND_INVALID]
    assert kinds[valid_not_on_list] == [form_review.KIND_NOT_ON_LIST]


def test_a_number_typed_with_spaces_or_a_dash_still_matches(known, cards):
    code = cards[0]
    sync(FakeSheet([row(at(14, 10), f"{code[:3]}-{code[3:]}"), row(at(14, 11), int(cards[1]))]))
    assert ParticipantVisit.query.count() == 2


def test_known_card_but_not_returning_is_recorded_and_listed(known, cards):
    sync(FakeSheet([row(at(14, 10), cards[0], returning="No")]))
    assert ParticipantVisit.query.count() == 1
    [item] = form_review.review_queue()
    assert item.kinds == [form_review.KIND_RETURNING_NO]

    form_review.mark_checked(item.response)
    assert form_review.review_queue() == []
    assert ParticipantVisit.query.count() == 1


def test_a_second_submission_within_two_minutes_is_listed(known, cards):
    sync(FakeSheet([row(at(14, 10, 0), cards[0]), row(at(14, 10, 1), cards[0]),
                    row(at(14, 10, 5), cards[0])]))
    queue = form_review.review_queue()
    assert [item.kinds for item in queue] == [[form_review.KIND_DUPLICATE]]
    assert ParticipantVisit.query.count() == 3


def test_a_waiting_response_is_matched_once_her_intake_is_entered(cards, admin_user):
    sheet = FakeSheet([row(at(14, 10), cards[6])])
    sync(sheet)
    assert FormResponse.query.one().decision is None

    participants.create_participant(cards[6], created_by_id=admin_user.id)
    db.session.commit()
    sync(sheet)
    assert FormResponse.query.one().decision == "auto"
    assert ParticipantVisit.query.count() == 1


def test_a_missing_id_column_fails_loudly_and_imports_nothing(cards):
    headers = [h for h in HEADERS if h != "What's your ID?"]
    run = sync(FakeSheet([[serial(at(14, 10)), "x", "Yes", "Event", "r", 9, "w"]], headers=headers))
    assert "ID question" in run.error
    assert FormResponse.query.count() == 0
    assert SyncRun.query.one().finished_at is not None


def test_a_reworded_optional_question_is_noted_not_fatal(known, cards):
    headers = HEADERS[:5] + ["Tell us why you came"] + HEADERS[6:]
    run = sync(FakeSheet([row(at(14, 10), cards[0])], headers=headers))
    assert run.error is None
    assert "Not found, left blank: reason" in run.columns_note
    assert FormResponse.query.one().reason is None


def test_small_changes_to_a_question_are_tolerated(known, cards):
    headers = ["TIMESTAMP", "What is your ID", "Are you a returning mother", "What event are you here for",
               "What's your reason for joining us today", "How likely are you to come back? (1-10)",
               "Why did you choose that number?"]
    run = sync(FakeSheet([[serial(at(14, 10)), cards[0], "yes", "ESL", "r", "7", "w"]], headers=headers))
    assert run.columns_note is None
    response = FormResponse.query.one()
    assert (response.returning, response.score, response.score_reason) == (True, 7, "w")


def test_without_the_sheets_time_zone_nothing_is_guessed(cards):
    run = sync(FakeSheet([row(at(14, 10), cards[0])], timezone=""))
    assert "time zone" in run.error
    assert FormResponse.query.count() == 0


def test_an_unreadable_row_is_skipped_and_the_rest_come_in(known, cards):
    sheet = FakeSheet([row(at(14, 10), cards[0]), ["14/10/2026 typed by hand", "", cards[1]],
                       row(at(14, 12), cards[2])])
    run = sync(sheet)
    assert run.rows_imported == 2
    assert "1 row(s) couldn't be imported" in run.error and "row 3" in run.error

    sheet.rows[1] = row(at(14, 11), cards[1])
    assert sync(sheet).rows_imported == 1


def test_a_row_that_fails_to_save_is_rolled_back_alone(known, cards, monkeypatch):
    real = form_review.match_on_import

    def explode_on_second(response):
        if response.raw_id_text == cards[1]:
            raise sa.exc.IntegrityError("simulated", None, Exception())
        real(response)

    monkeypatch.setattr(form_review, "match_on_import", explode_on_second)
    run = sync(FakeSheet([row(at(14, 10), cards[0]), row(at(14, 11), cards[1]),
                          row(at(14, 12), cards[2])]))
    assert run.rows_imported == 2
    assert {r.raw_id_text for r in FormResponse.query} == {cards[0], cards[2]}
    assert ParticipantVisit.query.count() == 2


def test_strange_answers_are_kept_blank_not_guessed(known, cards):
    sync(FakeSheet([row(at(14, 10), cards[0], returning="Maybe", score=11),
                    row(at(14, 11), cards[1], score="9.5")]))
    first, second = FormResponse.query.order_by(FormResponse.id)
    assert (first.returning, first.score, second.score) == (None, None, None)


def test_long_answers_are_cut_to_the_limit(known, cards):
    sync(FakeSheet([row(at(14, 10), cards[0], reason="x" * 1000)]))
    assert len(FormResponse.query.one().reason) == 300


def test_a_second_sync_waits_for_the_first(seeded_app):
    other = db.engine.connect()
    transaction = other.begin()
    other.execute(sa.text("SELECT pg_advisory_xact_lock(:k)"), {"k": form_sync.ADVISORY_LOCK_KEY})
    try:
        sheet = FakeSheet()
        assert form_sync.run(sheet) is None
        assert sheet.fetches == 0
    finally:
        transaction.rollback()
        other.close()
    assert form_sync.run(FakeSheet()) is not None


def test_a_sync_is_stale_after_five_minutes(seeded_app):
    with clock.freeze(datetime.datetime(2026, 10, 14, 10, tzinfo=HOUSTON)):
        form_sync.run(FakeSheet())
    with clock.freeze(datetime.datetime(2026, 10, 14, 10, 4, tzinfo=HOUSTON)):
        assert not form_sync.is_stale(5)
    with clock.freeze(datetime.datetime(2026, 10, 14, 10, 6, tzinfo=HOUSTON)):
        assert form_sync.is_stale(5)


def waiting(card):
    return FormResponse.query.filter_by(raw_id_text=card).one()


def test_match_puts_the_visit_on_her_real_number(known, cards, admin_user):
    sync(FakeSheet([row(at(14, 10), "12345")]))
    response = waiting("12345")
    form_review.match(response, cards[1])
    db.session.commit()

    assert response.decision == "matched" and response.participant_id == known[1].id
    assert ParticipantVisit.query.one().participant_id == known[1].id
    row_ = AuditLog.query.filter_by(table_name="form_response").one()
    assert row_.user_id == admin_user.id


def test_match_to_a_number_nobody_has_is_refused(known, cards):
    sync(FakeSheet([row(at(14, 10), "12345")]))
    with pytest.raises(form_review.ReviewError):
        form_review.match(waiting("12345"), cards[9])


def test_create_only_for_a_listed_number_nobody_has(known, cards):
    sync(FakeSheet([row(at(14, 10), cards[7]), row(at(14, 11), "12345")]))
    participant = form_review.create(waiting(cards[7]))
    assert participant.code == cards[7]
    assert ParticipantVisit.query.filter_by(participant_id=participant.id).count() == 1

    with pytest.raises(form_review.ReviewError):
        form_review.create(waiting("12345"))


def test_discarding_a_recorded_visit_removes_it(known, cards):
    sync(FakeSheet([row(at(14, 10), cards[0], returning="No")]))
    form_review.discard(FormResponse.query.one())
    assert ParticipantVisit.query.count() == 0
    assert form_review.review_queue() == []


def test_a_decided_response_cant_be_decided_again(known, cards):
    sync(FakeSheet([row(at(14, 10), "12345")]))
    response = waiting("12345")
    form_review.discard(response)
    with pytest.raises(form_review.ReviewError):
        form_review.match(response, cards[0])


def test_mapping_an_event_name_relabels_past_visits(known, cards):
    sync(FakeSheet([row(at(14, 10), cards[0], event="Diaper day"),
                    row(at(14, 11), cards[1], event=" diaper  DAY ")]))
    assert form_review.unmapped_label_count() == 1
    assert {v.service_type_id for v in ParticipantVisit.query} == {None}

    diapers = ServiceType.query.filter_by(name="Diaper Distribution").one()
    assert form_review.map_event("Diaper day", diapers.id) == 2
    db.session.commit()

    assert {v.service_type_id for v in ParticipantVisit.query} == {diapers.id}
    assert form_review.unmapped_label_count() == 0
    sync(FakeSheet([row(at(15, 10), cards[2], event="Diaper Day")]))
    assert ParticipantVisit.query.filter_by(service_type_id=diapers.id).count() == 3


def test_merging_keeps_visits_and_can_be_undone(known, cards):
    old, new = known[0], known[1]
    form_review.merge(old.code, new.code)
    assert form_review.survivor(old) is new

    form_review.unmerge(old)
    assert form_review.survivor(old) is old


def test_merges_are_one_level_deep(known):
    a, b, c = known
    form_review.merge(a.code, b.code)
    with pytest.raises(form_review.ReviewError, match="itself been merged"):
        form_review.merge(c.code, a.code)
    form_review.merge(b.code, c.code)
    assert a.merged_into_id == c.id and b.merged_into_id == c.id


@pytest.mark.parametrize("pair", [("same", "same"), ("12345", "known")])
def test_a_bad_merge_is_refused(known, pair):
    codes = {"same": known[0].code, "known": known[1].code, "12345": "12345"}
    with pytest.raises(form_review.ReviewError):
        form_review.merge(codes[pair[0]], codes[pair[1]])


def test_without_settings_the_sync_is_simply_off():
    assert form_sync.configured_source({"FORM_SHEET_ID": None, "GOOGLE_SERVICE_ACCOUNT_JSON": None}) is None


@pytest.mark.parametrize("key", ["not json {", json.dumps({"type": "authorized_user"})])
def test_a_bad_key_is_explained_without_echoing_it(key):
    with pytest.raises(form_sync.SyncError) as raised:
        form_sync.configured_source({"FORM_SHEET_ID": "abc", "GOOGLE_SERVICE_ACCOUNT_JSON": key})
    assert key not in str(raised.value)


def test_the_real_source_reads_unformatted_values_read_only(monkeypatch):
    calls = {}

    class FakeCredentials:
        @staticmethod
        def from_service_account_info(info, scopes):
            calls["scopes"] = scopes
            return object()

    class FakeResponse:
        def __init__(self, payload, status=200):
            self.payload, self.status_code = payload, status

        def json(self):
            return self.payload

    class FakeSession:
        def __init__(self, credentials):
            pass

        def get(self, url, params, timeout):
            calls.setdefault("urls", []).append((url, params))
            if "/values/" in url:
                return FakeResponse({"values": [["Timestamp", "What's your ID?"], [46000.5, 12345]]})
            return FakeResponse({"properties": {"timeZone": "America/Chicago"},
                                 "sheets": [{"properties": {"title": "Form responses 1"}}]})

    import google.auth.transport.requests as transport
    import google.oauth2.service_account as service_account
    monkeypatch.setattr(service_account, "Credentials", FakeCredentials)
    monkeypatch.setattr(transport, "AuthorizedSession", FakeSession)

    source = form_sync.GoogleSheetSource("sheet-id", None, {"client_email": "x@y"})
    data = source.fetch()

    assert calls["scopes"] == form_sync.SCOPES == ("https://www.googleapis.com/auth/spreadsheets.readonly",)
    values_url, values_call = [(u, p) for u, p in calls["urls"] if "/values/" in u][0]
    assert values_url.endswith("/values/Form%20responses%201")
    assert values_call["valueRenderOption"] == "UNFORMATTED_VALUE"
    assert values_call["dateTimeRenderOption"] == "SERIAL_NUMBER"
    assert (data.headers, data.rows, data.timezone) == (
        ["Timestamp", "What's your ID?"], [[46000.5, 12345]], "America/Chicago")


def test_every_decision_is_audited_with_its_before_and_after(known, cards, admin_user):
    sync(FakeSheet([row(at(14, 10), "12345"), row(at(14, 11), cards[7]),
                    row(at(14, 12), cards[0], returning="No")]))
    form_review.match(waiting("12345"), cards[1])
    form_review.create(waiting(cards[7]))
    form_review.discard(waiting(cards[0]))
    db.session.commit()

    rows = AuditLog.query.filter_by(table_name="form_response", field="decision").order_by(AuditLog.id)
    assert [(r.old_value, r.new_value) for r in rows] == [
        ("waiting", "matched"), ("waiting", "created"), ("auto", "discarded")]
    assert {r.user_id for r in rows} == {admin_user.id}


@pytest.mark.parametrize("titles, wanted, chosen", [
    (["Form Responses 1"], None, "Form Responses 1"),
    (["Notes", "Form responses 1"], None, "Form responses 1"),
    (["Respuestas de formulario 1"], None, "Respuestas de formulario 1"),
    (["Notes", "Answers"], "answers", "Answers"),
])
def test_the_tab_is_found_without_a_setting(titles, wanted, chosen):
    assert form_sync.choose_tab(titles, wanted) == chosen


@pytest.mark.parametrize("titles, wanted, message", [
    (["Notes", "Answers"], None, "several tabs"),
    (["Answers"], "Responses", "no tab called"),
])
def test_an_unclear_tab_is_never_guessed(titles, wanted, message):
    with pytest.raises(form_sync.SyncError, match=message):
        form_sync.choose_tab(titles, wanted)


class GoogleError:
    def __init__(self, status, reason=None):
        self.status_code = status
        self._reason = reason

    def json(self):
        details = [{"reason": self._reason}] if self._reason else []
        return {"error": {"status": "PERMISSION_DENIED", "message": "not shown", "details": details}}


@pytest.mark.parametrize("response, message", [
    (GoogleError(403, "SERVICE_DISABLED"), "Google Sheets API isn't turned on"),
    (GoogleError(403), "isn't shared with svc@example.iam"),
    (GoogleError(404), "FORM_SHEET_ID on Render is wrong"),
])
def test_each_google_error_says_what_to_fix(response, message):
    with pytest.raises(form_sync.SyncError, match=message) as raised:
        form_sync._raise_for(response, "svc@example.iam")
    assert "not shown" not in str(raised.value)


def test_a_big_backlog_comes_in_a_few_hundred_rows_at_a_time(cards, monkeypatch):
    monkeypatch.setattr(form_sync, "MAX_NEW_ROWS_PER_SYNC", 4)
    sheet = FakeSheet([row(at(14, 10, minute), "12345") for minute in range(10)])

    first = sync(sheet)
    assert (first.rows_imported, first.rows_remaining) == (4, 6)
    assert form_sync.is_stale(5)

    second = sync(sheet)
    third = sync(sheet)
    assert (second.rows_imported, third.rows_imported, third.rows_remaining) == (4, 2, 0)
    assert FormResponse.query.count() == 10
    assert not form_sync.is_stale(5)


def test_the_review_queue_asks_the_database_a_few_times_not_per_response(known, cards):
    rows = [row(at(14, 9, minute), "12345") for minute in range(0, 50, 3)]
    sync(FakeSheet(rows))
    statements = []

    def count(conn, cursor, statement, *rest):
        statements.append(statement)

    sa.event.listen(db.engine, "before_cursor_execute", count)
    try:
        queue = form_review.review_queue()
    finally:
        sa.event.remove(db.engine, "before_cursor_execute", count)
    assert len(queue) == len(rows)
    assert len(statements) < 10
