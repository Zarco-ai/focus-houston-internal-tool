
import datetime
import random

from app.extensions import db
from app.models import (AuditLog, Batch, FormResponse, IntakeSession, Participant,
                        ParticipantVisit, ServiceType, SyncRun)
from app.models.participant import VISIT_GOOGLE_FORM
from app.services import batches, card_numbers, catalog, participants

FIFTH, SIXTH = datetime.date(2026, 11, 5), datetime.date(2026, 11, 6)


def approved(day):
    batch = batches.open_batch(date=day, service_type_id=ServiceType.query.first().id)
    batches.set_line(batch, catalog.find_item("Individual Diaper", "diapers"), quantity=30)
    batches.commit_batch(batch)
    return batch


def run(app, *args):
    return app.test_cli_runner().invoke(args=["batches", "delete-date", *args])


def test_without_yes_it_only_lists(seeded_app, admin_user):
    approved(FIFTH)
    result = run(seeded_app, "2026-11-05")
    assert "1 batch dated 2026-11-05" in result.output
    assert "Nothing deleted" in result.output
    assert Batch.query.count() == 1


def test_it_deletes_that_date_and_nothing_else(seeded_app, admin_user):
    fifth = [approved(FIFTH).id, approved(FIFTH).id]
    sixth = approved(SIXTH).id
    draft = batches.open_batch(date=FIFTH, service_type_id=ServiceType.query.first().id)
    db.session.commit()

    result = run(seeded_app, "2026-11-05", "--yes", "--count", "3", "--as", admin_user.email)

    assert result.exit_code == 0, result.output
    assert [b.id for b in Batch.query.all()] == [sixth]
    deleted = AuditLog.query.filter_by(table_name="batch", field="deleted").all()
    assert {row.record_id for row in deleted} == set(fifth) | {draft.id}
    assert all(row.user_id == admin_user.id for row in deleted)


def test_a_count_that_doesnt_match_deletes_nothing(seeded_app, admin_user):
    approved(FIFTH)
    approved(FIFTH)
    result = run(seeded_app, "2026-11-05", "--yes", "--count", "1", "--as", admin_user.email)
    assert result.exit_code != 0 and "Nothing deleted" in result.output
    assert Batch.query.count() == 2


def test_it_needs_an_admin(seeded_app, staff_user):
    approved(FIFTH)
    result = run(seeded_app, "2026-11-05", "--yes", "--count", "1", "--as", staff_user.email)
    assert result.exit_code != 0
    assert Batch.query.count() == 1


def test_an_intake_that_started_a_deleted_batch_stays(seeded_app, admin_user):
    batch = approved(FIFTH)
    session = IntakeSession(started_by_id=admin_user.id, batch_id=batch.id, status="discarded")
    db.session.add(session)
    db.session.commit()

    run(seeded_app, "2026-11-05", "--yes", "--count", "1", "--as", admin_user.email)
    db.session.refresh(session)
    assert session.batch_id is None


def test_the_page_deletes_that_day_and_discards_its_form_responses(client, seeded_app, admin_user):
    fifth, sixth = approved(FIFTH).id, approved(SIXTH).id
    code = card_numbers.generate(1, rng=random.Random(3))[0]
    participants.load_card_list(f"list_position,card_number,batch\n1,{code},1\n")
    mother = participants.create_participant(code, created_by_id=admin_user.id)
    run = SyncRun()
    db.session.add(run)
    db.session.flush()
    responses = []
    for day in (FIFTH, SIXTH):
        response = FormResponse(source_key=f"k{day}", submitted_at=datetime.datetime(2026, 11, day.day, 18,
                                tzinfo=datetime.timezone.utc), submitted_local_date=day,
                                raw_id_text=code, decision="auto", participant_id=mother.id,
                                imported_in_sync_run_id=run.id)
        db.session.add(response)
        db.session.flush()
        participants.add_visit(mother, day, VISIT_GOOGLE_FORM, response.id)
        responses.append(response.id)
    db.session.commit()

    page = client.get("/admin/delete-day?date=2026-11-05").get_data(as_text=True)
    assert "Batches dated November 5, 2026: 1" in page and "Form responses dated November 5, 2026: 1" in page

    form = {"date": "2026-11-05", "batch_count": "1", "response_count": "1"}
    client.post("/admin/delete-day", data=form | {"confirm": "nope"})
    assert Batch.query.count() == 2

    client.post("/admin/delete-day", data=form | {"confirm": "2026-11-05"})
    assert [b.id for b in Batch.query.all()] == [sixth]
    assert db.session.get(FormResponse, responses[0]).decision == "discarded"
    assert db.session.get(FormResponse, responses[1]).decision == "auto"
    assert [v.visit_date for v in ParticipantVisit.query.all()] == [SIXTH]
    assert fifth not in [b.id for b in Batch.query.all()]
    assert Participant.query.count() == 1


def test_the_page_refuses_if_the_day_changed(client, seeded_app):
    approved(FIFTH)
    approved(FIFTH)
    client.post("/admin/delete-day", data={"date": "2026-11-05", "batch_count": "1",
                                           "response_count": "0", "confirm": "2026-11-05"})
    assert Batch.query.count() == 2


def test_staff_cannot_open_the_page(staff_client):
    assert staff_client.get("/admin/delete-day").status_code == 403
