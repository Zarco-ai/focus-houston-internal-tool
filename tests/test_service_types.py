
import pytest

from app.extensions import db
from app.models import AuditLog, Batch, ServiceType
from app.services import batches, service_types


def named(name):
    return ServiceType.query.filter_by(name=name).one()


def test_an_admin_adds_one_and_it_is_audited(seeded_app, admin_user):
    created = service_types.create("  Mom's   Conference ")
    db.session.commit()

    assert created.name == "Mom's Conference" and created.active
    row = AuditLog.query.filter_by(table_name="service_type", record_id=created.id).one()
    assert (row.action, row.new_value, row.user_id) == ("create", "Mom's Conference", admin_user.id)


@pytest.mark.parametrize("name", ["", "   ", "esl class", "ESL CLASS", "x" * 101])
def test_a_blank_long_or_duplicate_name_is_refused(seeded_app, name):
    with pytest.raises(ValueError):
        service_types.create(name)


def test_renaming_renames_it_on_past_batches_too(seeded_app):
    esl = named("ESL Class")
    batch = batches.open_batch(service_type_id=esl.id)
    batches.set_attendance(batch, {"education_participants_total": 5})
    batches.commit_batch(batch)

    service_types.rename(esl, "English Class")
    db.session.commit()

    assert db.session.get(Batch, batch.id).service_type.name == "English Class"
    row = AuditLog.query.filter_by(table_name="service_type", field="name").one()
    assert (row.old_value, row.new_value) == ("ESL Class", "English Class")


def test_renaming_to_its_own_name_in_another_case_is_allowed(seeded_app):
    esl = named("ESL Class")
    assert service_types.rename(esl, "ESL class")


def test_a_switched_off_type_is_not_offered_for_a_new_batch(client, seeded_app):
    service_types.set_active(named("Bike Giveaway"), False)
    batch = batches.open_batch()
    db.session.commit()

    page = client.get(f"/batch/{batch.id}").get_data(as_text=True)
    assert "Bike Giveaway" not in page

    response = client.post(f"/batch/{batch.id}/service-type",
                           data={"service_type_id": named("Bike Giveaway").id},
                           follow_redirects=True)
    assert "switched off in Settings" in response.get_data(as_text=True)
    assert db.session.get(Batch, batch.id).service_type_id is None


def test_a_batch_keeps_and_still_shows_a_type_switched_off_later(client, seeded_app):
    bikes = named("Bike Giveaway")
    batch = batches.open_batch(service_type_id=bikes.id)
    batches.set_attendance(batch, {"individuals_served": 2})
    batches.commit_batch(batch)
    service_types.set_active(bikes, False)
    db.session.commit()

    page = client.get(f"/batch/{batch.id}/review").get_data(as_text=True)
    assert "Bike Giveaway" in page
    assert db.session.get(Batch, batch.id).service_type_id == bikes.id


def test_switching_off_and_on_is_audited(seeded_app):
    bikes = named("Bike Giveaway")
    service_types.set_active(bikes, False)
    service_types.set_active(bikes, True)
    db.session.commit()

    rows = AuditLog.query.filter_by(table_name="service_type", field="active").order_by(AuditLog.id)
    assert [(r.old_value, r.new_value) for r in rows] == [("yes", "no"), ("no", "yes")]


def test_the_page_adds_renames_and_switches_off(client, seeded_app):
    client.post("/admin/service-types", data={"name": "Mom's Conference"})
    conference = named("Mom's Conference")

    client.post(f"/admin/service-types/{conference.id}", data={"name": "Moms Conference"})
    client.post(f"/admin/service-types/{conference.id}", data={"active": "off"})

    db.session.refresh(conference)
    assert (conference.name, conference.active) == ("Moms Conference", False)
    page = client.get("/admin/service-types").get_data(as_text=True)
    assert "Switch back on" in page


def test_the_page_says_why_a_name_was_refused(client, seeded_app):
    response = client.post("/admin/service-types", data={"name": "intake"},
                           follow_redirects=True)
    assert "There is already a service type called" in response.get_data(as_text=True)


def test_staff_cannot_manage_service_types(staff_client, seeded_app):
    assert staff_client.get("/admin/service-types").status_code == 403
    assert staff_client.post("/admin/service-types", data={"name": "X"}).status_code == 403
    assert ServiceType.query.filter_by(name="X").count() == 0
