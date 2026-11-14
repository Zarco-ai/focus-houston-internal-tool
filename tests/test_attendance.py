
from decimal import Decimal

import pytest

from app.extensions import db
from app.models import AuditLog, Batch, ServiceType
from app.services import attendance, batches, catalog, metrics
from app.services.attendance import FIELDS_BY_NAME


def a_batch():
    return batches.open_batch(service_type_id=ServiceType.query.first().id)


def field(name):
    return FIELDS_BY_NAME[name]


@pytest.mark.parametrize("name, typed, stored", [
    ("children_served", "", 0),
    ("children_served", " 12 ", 12),
    ("volunteer_hours_tenths", "2.5", 25),
    ("volunteer_hours_tenths", "80", 800),
    ("volunteer_hours_tenths", ".5", 5),
    ("meals_value", "$1,250.50", Decimal("1250.50")),
    ("meals_value", "", Decimal("0.00")),
])
def test_a_box_stores_what_was_typed(name, typed, stored):
    assert attendance.parse_value(field(name), typed) == stored


@pytest.mark.parametrize("name, typed", [
    ("children_served", "-3"),
    ("children_served", "12.5"),
    ("children_served", "twelve"),
    ("children_served", "²"),
    ("children_served", "1000000"),
    ("volunteer_hours_tenths", "2.25"),
    ("volunteer_hours_tenths", "-1"),
    ("volunteer_hours_tenths", "NaN"),
    ("meals_value", "10.005"),
    ("meals_value", "-5"),
    ("meals_value", "Infinity"),
])
def test_a_wrong_box_is_refused_not_rounded(name, typed):
    with pytest.raises(ValueError):
        attendance.parse_value(field(name), typed)


def test_a_class_with_counts_and_no_goods_can_be_approved(seeded_app):
    batch = a_batch()
    batches.set_attendance(batch, {"education_participants_total": 12,
                                   "education_sessions_held": 1})
    batches.commit_batch(batch)

    assert batch.is_committed
    assert batch.lines == []


def test_a_batch_with_no_goods_and_no_counts_is_still_refused(seeded_app):
    batch = a_batch()
    batches.set_attendance(batch, {"children_served": 0})

    with pytest.raises(batches.BatchNotReady) as raised:
        batches.commit_batch(batch)
    assert raised.value.reasons == ["Add at least one item, or an attendance count."]


def test_a_money_figure_alone_is_enough_to_approve(seeded_app):
    batch = a_batch()
    batches.set_attendance(batch, {"meals_value": Decimal("30.00")})
    assert batches.can_commit(batch)


def test_a_draft_saves_without_audit_rows(seeded_app):
    batch = a_batch()
    batches.set_attendance(batch, {"children_served": 4})
    db.session.commit()

    assert batch.attendance.children_served == 4
    assert AuditLog.query.filter_by(table_name="attendance").count() == 0


def test_a_change_on_an_approved_batch_is_audited(seeded_app, admin_user):
    batch = a_batch()
    batches.set_attendance(batch, {"children_served": 4})
    batches.commit_batch(batch)

    batches.set_attendance(batch, {"children_served": 5, "volunteer_hours_tenths": 25})
    db.session.commit()

    rows = {r.field: r for r in AuditLog.query.filter_by(table_name="attendance")}
    assert set(rows) == {"children_served", "volunteer_hours_tenths"}
    assert (rows["children_served"].old_value, rows["children_served"].new_value) == ("4", "5")
    assert rows["children_served"].user_id == admin_user.id


def test_a_field_not_sent_is_left_alone(seeded_app):
    batch = a_batch()
    batches.set_attendance(batch, {"children_served": 4, "meals_value": Decimal("12.00")})
    batches.set_attendance(batch, {"children_served": 6})

    assert batch.attendance.meals_value == Decimal("12.00")


def test_an_unknown_field_is_refused(seeded_app):
    with pytest.raises(ValueError):
        batches.set_attendance(a_batch(), {"mother_name": "x"})


def review_page(client, batch):
    return client.get(f"/batch/{batch.id}/review").get_data(as_text=True)


def test_the_review_screen_offers_every_switched_on_box(client, seeded_app):
    for setting in metrics.settings():
        metrics.update_setting(setting.key, enabled=True)
    batch = a_batch()
    db.session.commit()
    page = review_page(client, batch)

    for name in FIELDS_BY_NAME:
        assert f'name="{name}"' in page
    assert "Understanding" in page and "Volunteers" in page and "Unique" in page


def test_a_switched_off_metric_hides_its_box_and_keeps_its_value(client, seeded_app):
    batch = a_batch()
    batches.set_attendance(batch, {"meals_value": Decimal("25.00")})
    metrics.update_setting("meals_value", enabled=False)
    db.session.commit()

    assert 'name="meals_value"' not in review_page(client, batch)

    client.post(f"/batch/{batch.id}/attendance", data={"children_served": "3"})
    db.session.refresh(batch.attendance)
    assert batch.attendance.meals_value == Decimal("25.00")
    assert batch.attendance.children_served == 3


def test_the_review_screen_saves_every_kind_of_box(client, seeded_app):
    for setting in metrics.settings():
        metrics.update_setting(setting.key, enabled=True)
    batch = a_batch()
    db.session.commit()

    client.post(f"/batch/{batch.id}/attendance", data={
        "children_served": "3", "volunteer_hours_tenths": "80", "meals_value": "45.10",
        "unique_parishes_event_count": "2",
    })
    record = db.session.get(Batch, batch.id).attendance
    assert (record.children_served, record.volunteer_hours_tenths, record.meals_value,
            record.unique_parishes_event_count) == (3, 800, Decimal("45.10"), 2)

    page = review_page(client, batch)
    assert 'value="80"' in page and 'value="45.10"' in page


def test_one_bad_box_saves_nothing(client, seeded_app):
    batch = a_batch()
    batches.set_attendance(batch, {"children_served": 2})
    db.session.commit()

    response = client.post(f"/batch/{batch.id}/attendance", data={
        "children_served": "9", "volunteers_total": "-1",
    }, follow_redirects=True)

    text = response.get_data(as_text=True)
    assert "Volunteers: type a whole number" in text
    assert "Nothing was saved" in text
    db.session.refresh(batch.attendance)
    assert batch.attendance.children_served == 2


def test_staff_can_record_attendance(staff_client, seeded_app):
    batch = a_batch()
    db.session.commit()
    staff_client.post(f"/batch/{batch.id}/attendance", data={"children_served": "1"})
    assert db.session.get(Batch, batch.id).attendance.children_served == 1


def test_an_attendance_only_batch_is_approved_from_the_review_screen(client, seeded_app):
    batch = a_batch()
    db.session.commit()

    page = review_page(client, batch)
    assert "can still be approved once it has" in page

    client.post(f"/batch/{batch.id}/attendance", data={"education_participants_total": "8"})
    client.post(f"/batch/{batch.id}/approve")
    assert db.session.get(Batch, batch.id).is_committed

    totals = client.get("/totals").get_data(as_text=True)
    assert "counts only" in totals


def test_the_batch_page_shows_the_counts_and_their_history(client, seeded_app):
    batch = a_batch()
    batches.set_line(batch, catalog.find_item("Coat", "clothing_adult"), quantity=1)
    batches.set_attendance(batch, {"children_served": 2})
    batches.commit_batch(batch)
    batches.set_attendance(batch, {"children_served": 3})
    db.session.commit()

    page = client.get(f"/batches/{batch.id}").get_data(as_text=True)
    assert "Children served" in page
    assert "children_served" in page
