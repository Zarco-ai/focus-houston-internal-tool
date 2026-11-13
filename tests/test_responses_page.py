"""The Responses page and the admin pages behind it (Phase 6)."""

import datetime
import random

import pytest

from app.config import TestConfig
from app.extensions import db
from app.models import FormResponse, ParticipantVisit, ServiceType, SyncRun
from app.services import card_numbers, form_review, form_sync, participants
from tests.conftest import TEST_ADMIN_EMAIL, make_user, sign_in
from tests.test_form_sync import FakeSheet, at, row


@pytest.fixture
def cards(seeded_app, admin_user):
    codes = card_numbers.generate(6, rng=random.Random(4))
    participants.load_card_list("list_position,card_number,batch\n" + "".join(
        f"{i},{c},1\n" for i, c in enumerate(codes, 1)))
    participants.create_participant(codes[0], created_by_id=admin_user.id)
    participants.create_participant(codes[1], created_by_id=admin_user.id)
    db.session.commit()
    return codes


@pytest.fixture
def sheet(monkeypatch):
    """Make the app think the sync is set up, reading a fake Sheet."""
    fake = FakeSheet()
    fake.service_account_email = "focus-sync@example.iam.gserviceaccount.com"
    monkeypatch.setattr(form_sync, "configured_source", lambda config: fake)
    return fake


def page(client):
    return client.get("/responses").get_data(as_text=True)


def test_without_setup_the_page_says_so(client, seeded_app):
    text = page(client)
    assert "isn't set up yet" in text
    assert SyncRun.query.count() == 0


def test_opening_the_page_syncs_when_stale_and_not_again_at_once(client, cards, sheet):
    sheet.rows = [row(at(14, 10), cards[0])]
    page(client)
    page(client)
    assert sheet.fetches == 1
    assert FormResponse.query.count() == 1


def test_sync_now(client, cards, sheet):
    sheet.rows = [row(at(14, 10), cards[0]), row(at(14, 11), "12345")]
    response = client.post("/responses/sync", follow_redirects=True)
    assert "Synced. 2 new responses, 1 to review." in response.get_data(as_text=True)


def test_a_failed_sync_shows_on_the_page(client, cards, sheet):
    sheet.timezone = ""
    client.post("/responses/sync")
    assert "Couldn&#39;t read the Sheet&#39;s time zone" in page(client) or \
        "Couldn't read the Sheet's time zone" in page(client)


def test_the_queue_offers_the_right_actions(client, cards, sheet):
    sheet.rows = [row(at(14, 10), cards[4]), row(at(14, 11), "12345")]
    client.post("/responses/sync")
    text = page(client)
    assert f"Create her on {cards[4]}" in text
    assert text.count("Match</button>") == 2
    assert "Not a valid card number" in text


def test_reviewing_from_the_page(client, cards, sheet):
    sheet.rows = [row(at(14, 10), "12345"), row(at(14, 11), cards[4])]
    client.post("/responses/sync")
    typo = FormResponse.query.filter_by(raw_id_text="12345").one()
    new = FormResponse.query.filter_by(raw_id_text=cards[4]).one()

    client.post(f"/responses/{typo.id}/match", data={"card_number": cards[1]})
    client.post(f"/responses/{new.id}/create")

    assert {r.decision for r in FormResponse.query} == {"matched", "created"}
    assert ParticipantVisit.query.count() == 2
    assert "Nothing to review" in page(client)


def test_a_wrong_match_says_why(client, cards, sheet):
    sheet.rows = [row(at(14, 10), "12345")]
    client.post("/responses/sync")
    response_id = FormResponse.query.one().id
    reply = client.post(f"/responses/{response_id}/match", data={"card_number": "99999"},
                        follow_redirects=True)
    assert "Nobody in the app has card number" in reply.get_data(as_text=True)


def test_responses_are_admin_only(staff_client, cards, sheet):
    sheet.rows = [row(at(14, 10), "12345")]
    assert staff_client.get("/responses").status_code == 403
    assert staff_client.post("/responses/sync").status_code == 403
    assert FormResponse.query.count() == 0
    assert staff_client.get("/admin/form-events").status_code == 403
    assert staff_client.post("/admin/merge", data={"retiring": cards[0],
                                                   "keeping": cards[1]}).status_code == 403


def test_an_admin_maps_an_event_name(client, cards, sheet):
    sheet.rows = [row(at(14, 10), cards[0], event="Diaper day")]
    client.post("/responses/sync")
    assert "isn’t matched to a service type" in page(client)

    diapers = ServiceType.query.filter_by(name="Diaper Distribution").one()
    client.post("/admin/form-events", data={"label": "Diaper day", "service_type_id": diapers.id})
    assert ParticipantVisit.query.one().service_type_id == diapers.id
    assert "tag-confirm\">not matched" not in client.get("/admin/form-events").get_data(as_text=True)


def test_an_admin_merges_and_undoes(client, cards, seeded_app):
    reply = client.post("/admin/merge", data={"retiring": cards[0], "keeping": cards[1]},
                        follow_redirects=True)
    assert f"{cards[0]}&#39;s visits now count toward {cards[1]}" in reply.get_data(as_text=True) or \
        f"{cards[0]}'s visits now count toward {cards[1]}" in reply.get_data(as_text=True)

    retired = participants.find_by_code(cards[0])
    client.post(f"/admin/merge/{retired.id}/undo")
    db.session.refresh(retired)
    assert retired.merged_into_id is None


class SyncLimitedConfig(TestConfig):
    RATELIMIT_ENABLED = True
    SYNC_RATE_LIMIT = "2 per minute"


def test_sync_now_is_rate_limited(make_app, monkeypatch):
    from app.models import AppUser
    from app.services import seeding

    limited = make_app(SyncLimitedConfig)
    with limited.app_context():
        from tests.conftest import empty_every_table
        empty_every_table()
        admin = make_user(TEST_ADMIN_EMAIL, "admin")
        monkeypatch.setattr(form_sync, "configured_source", lambda config: FakeSheet())
        browser = limited.test_client()
        sign_in(browser, admin)
        statuses = [browser.post("/responses/sync").status_code for _ in range(3)]
        db.session.remove()
    assert statuses == [302, 302, 429]
