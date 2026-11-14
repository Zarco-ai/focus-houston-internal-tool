
import datetime

import pytest

from app.extensions import db
from app.models import AuditLog, Batch, ServiceType
from app.services import batches, catalog, clock, totals


def committed(day=None):
    batch = batches.open_batch(date=day, service_type_id=ServiceType.query.first().id)
    batches.set_line(batch, catalog.find_item("Coat", "clothing_adult"), quantity=1)
    batches.commit_batch(batch)
    return batch


def test_a_batch_can_be_dated_in_the_past(seeded_app):
    batch = batches.open_batch()
    batches.set_batch_date(batch, "2026-03-04")
    assert batch.date == datetime.date(2026, 3, 4)


@pytest.mark.parametrize("raw, message", [
    ("2999-01-01", "future"),
    ("1999-12-31", "too far"),
    ("03/04/2026", "isn't a date"),
    ("", "isn't a date"),
])
def test_a_future_or_impossible_date_is_refused(seeded_app, raw, message):
    with pytest.raises(ValueError, match=message):
        batches.set_batch_date(batches.open_batch(), raw)


def test_redating_an_approved_batch_is_audited_and_moves_its_month(seeded_app, admin_user):
    batch = committed()
    batches.set_batch_date(batch, "2026-01-15")
    db.session.commit()

    row = AuditLog.query.filter_by(table_name="batch", field="date").one()
    assert (row.new_value, row.user_id) == ("2026-01-15", admin_user.id)
    assert totals.summary(datetime.date(2026, 1, 1), datetime.date(2026, 1, 31))["total_quantity"] == 1


def test_the_home_page_starts_a_past_session(client, seeded_app):
    response = client.post("/batch/new", data={"date": "2026-02-10"})
    batch = Batch.query.one()
    assert batch.date == datetime.date(2026, 2, 10)
    assert response.headers["Location"].endswith(f"/batch/{batch.id}")


def test_the_home_page_refuses_a_future_session(client, seeded_app):
    tomorrow = (clock.local_today() + datetime.timedelta(days=1)).isoformat()
    reply = client.post("/batch/new", data={"date": tomorrow}, follow_redirects=True)
    assert "can&#39;t be dated in the future" in reply.get_data(as_text=True) or \
        "can't be dated in the future" in reply.get_data(as_text=True)
    assert Batch.query.count() == 0


def test_the_review_screen_changes_the_date(client, seeded_app):
    batch = committed()
    db.session.commit()
    page = client.get(f"/batch/{batch.id}/review").get_data(as_text=True)
    assert 'name="date"' in page
    client.post(f"/batch/{batch.id}/date", data={"date": "2026-04-01"})
    db.session.refresh(batch)
    assert batch.date == datetime.date(2026, 4, 1)
