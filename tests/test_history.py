"""Importing earlier months from the sheets (decided 2 Nov 2026: as recorded).

A small made-up history file stands in for the real one, which holds
FOCUS's data and is never committed.
"""

import datetime
import io
import json
from decimal import Decimal

import pytest

from app.extensions import db
from app.models import AuditLog, Batch, Item, LineItem, ServiceType
from app.services import history, metrics, totals

D = datetime.date


def entry(date, service, note="", attendance=None, lines=()):
    return {"date": date, "sheet_date": date, "service_text": service, "note": note,
            "attendance": attendance or {}, "lines": list(lines)}


def line(kind, label, quantity, value, note=None):
    out = {"kind": kind, "label": label, "quantity": quantity, "value": value}
    if note:
        out["note"] = note
    return out


def document(*entries):
    return json.dumps({"format": "focus-history", "format_version": 1,
                       "source": "test sheet", "batches": list(entries)})


SHEET = document(
    entry("2026-01-05", "Monday Diaper/Wipe Pick up", "Monday Diaper/Wipe Pick up",
          {"individuals_served": 14, "children_served": 20},
          [line("diapers", "Diapers", 479, "210.76"), line("wipes", "Wipes", 4, "12.00"),
           line("hygiene", "Hygine", 4, "30.00"), line("clothing", "Clothing", 3, "10.00")]),
    entry("2026-01-07", "CLIENT pick up", "CLIENT pick up",
          lines=[line("formula", "Formula", 1, "39.94"), line("transport", "metro card", 1, "10.00"),
                 line("diapers", "Diapers", 180, "0.00"),
                 line("uncategorized", "Car Seat", 1, "130.00")]),
    entry("2026-01-12", "Nutrition", "Nutrition — Nutrition Class",
          {"education_participants_total": 6, "volunteer_hours_tenths": 25}),
)


def test_every_count_and_dollar_lands_as_recorded(seeded_app):
    summary = history.import_history(SHEET)
    assert summary["sessions"] == 3

    values = metrics.values(D(2026, 1, 1), D(2026, 1, 31))
    assert values["diapers_count"] == 479 + 180
    assert values["diapers_value"] == Decimal("210.76")          # the $0 row stays $0
    assert values["goods_count"] == 4 + 4 + 3                    # wipes counted as hygiene
    assert values["goods_value"] == Decimal("52.00")
    assert values["non_food_gik"] == Decimal("262.76")
    assert values["formula_containers"] == 1 and values["food_value"] == Decimal("39.94")
    assert values["bus_passes_count"] == 1                       # never in Non-Food
    assert values["individuals_served"] == 14
    assert values["volunteer_hours"] == Decimal("2.5")


def test_a_value_that_doesnt_divide_evenly_is_kept_to_the_cent(seeded_app):
    history.import_history(document(entry("2026-02-02", "x", lines=[
        line("clothing", "Clothing", 3, "10.00")])))
    lines = LineItem.query.all()
    assert sum(l.quantity for l in lines) == 3
    assert sum(l.computed_value for l in lines) == Decimal("10.00")


def test_doubtful_rows_wait_in_the_review_queue(seeded_app):
    summary = history.import_history(SHEET)
    flagged = LineItem.query.filter_by(needs_review=True).all()
    assert summary["to_review"] == 2
    assert {(l.custom_name or l.item.name) for l in flagged} == {"Car Seat", "Diapers (2026 sheet)"}


def test_service_types_follow_the_sheet_and_her_wording_is_the_note(seeded_app):
    history.import_history(SHEET)
    by_date = {b.date: b for b in Batch.query}
    assert by_date[D(2026, 1, 5)].service_type.name == "Diaper Distribution"
    assert by_date[D(2026, 1, 7)].service_type.name == "Walk-in"
    assert by_date[D(2026, 1, 12)].service_type.name == "Nutrition Class"
    assert by_date[D(2026, 1, 12)].note == "Nutrition — Nutrition Class"
    assert all(b.is_committed for b in by_date.values())


def test_the_category_items_are_hidden_from_counting(seeded_app):
    history.import_history(SHEET)
    hidden = Item.query.filter_by(category=history.HISTORY_CATEGORY).all()
    assert len(hidden) == len(history.KINDS)
    assert not any(item.active for item in hidden)
    wipes = next(i for i in hidden if i.name.startswith("Wipes"))
    assert (wipes.report_bucket, wipes.display_group) == ("hygiene", "diapers")


def test_the_same_file_twice_changes_nothing(seeded_app):
    history.import_history(SHEET)
    with pytest.raises(history.HistoryError, match="already been imported"):
        history.import_history(SHEET)
    assert Batch.query.count() == 3


def test_a_bad_file_imports_nothing(seeded_app):
    bad = document(entry("2026-01-05", "x", lines=[line("clothing", "C", 1, "5.00")]),
                   entry("2999-01-01", "x", lines=[line("clothing", "C", 1, "5.00")]))
    with pytest.raises(history.HistoryError, match="future"):
        history.import_history(bad)
    assert Batch.query.count() == 0
    with pytest.raises(history.HistoryError, match="isn't a FOCUS history file"):
        history.import_history(json.dumps({"format": "something"}))


def test_the_import_is_audited(seeded_app, admin_user):
    history.import_history(SHEET)
    row = AuditLog.query.filter_by(action="import", table_name="batch").one()
    assert row.user_id == admin_user.id and "3 sessions" in row.description


def test_an_admin_uploads_it_and_staff_cannot(client, staff_client, seeded_app):
    upload = lambda c: c.post("/admin/history", data={"history_file": (io.BytesIO(SHEET.encode()), "h.json")},
                              content_type="multipart/form-data", follow_redirects=True)
    assert upload(staff_client).status_code == 403
    reply = upload(client)
    assert "Imported 3 sessions" in reply.get_data(as_text=True)
    assert totals.summary(D(2026, 1, 1), D(2026, 1, 31))["batches"]


# --- Exporting every session (format version 2) -----------------------------------------------


def reviewed_sheet():
    """The sheet imported, then part of its review queue done: the car seat
    sorted into a new catalog item. The $0.00 diapers are still waiting."""
    from app.services import review

    history.import_history(SHEET)
    car_seat = LineItem.query.filter_by(custom_name="Car Seat").one()
    review.resolve_line(car_seat, new_item_fields={
        "name": "Car seat", "category": "Baby gear", "report_bucket": "household",
        "used_multiplier": 0.5, "price_entry": "manual"}, unit_price="130.00")
    db.session.commit()


def snapshot():
    return sorted((b.date, b.service_type.name if b.service_type else None, b.note,
                   l.item.name if l.item else l.custom_name, l.quantity, l.computed_value,
                   l.report_bucket_at_time, l.needs_review)
                  for b in Batch.query for l in b.lines)


def start_again():
    """Another copy of the app: no sessions yet."""
    for batch in Batch.query.all():
        db.session.delete(batch)
    db.session.commit()


def test_an_export_brings_every_session_back_exactly(seeded_app):
    reviewed_sheet()
    before, values = snapshot(), metrics.values(D(2026, 1, 1), D(2026, 1, 31))
    text = history.export_text()

    start_again()
    summary = history.import_history(text)

    assert summary["sessions"] == 3
    assert snapshot() == before
    assert metrics.values(D(2026, 1, 1), D(2026, 1, 31)) == values


def test_what_was_waiting_in_the_review_queue_is_still_waiting(seeded_app):
    reviewed_sheet()
    text = history.export_text()
    start_again()
    history.import_history(text)

    flagged = LineItem.query.filter_by(needs_review=True).all()
    assert [l.item.name for l in flagged] == ["Diapers (2026 sheet)"]
    car_seat = LineItem.query.join(Item).filter(Item.name == "Car seat").one()
    assert not car_seat.needs_review and car_seat.reviewed_at is not None


def test_an_item_made_while_reviewing_is_added_to_the_other_copy(seeded_app):
    reviewed_sheet()
    document_ = json.loads(history.export_text())
    for item in document_["items"]:
        if item["name"] == "Car seat":
            item["name"] = "Car seat (other copy)"
    for entry_ in document_["batches"]:
        for line_ in entry_["lines"]:
            if line_["item"] == ["Car seat", "Baby gear"]:
                line_["item"] = ["Car seat (other copy)", "Baby gear"]
    start_again()
    history.import_history(json.dumps(document_))

    item = Item.query.filter_by(name="Car seat (other copy)").one()
    assert item.report_bucket == "household"
    assert LineItem.query.filter_by(item_id=item.id).one().computed_value == Decimal("130.00")


def test_a_file_whose_months_are_already_in_the_app_is_refused(seeded_app):
    reviewed_sheet()
    with pytest.raises(history.HistoryError, match="count them twice"):
        history.import_history(history.export_text())
    assert Batch.query.count() == 3


def test_only_admins_download_every_session_and_it_is_audited(client, staff_client, seeded_app):
    history.import_history(SHEET)
    assert staff_client.get("/admin/history/export.json").status_code == 403

    reply = client.get("/admin/history/export.json")
    assert reply.status_code == 200 and "attachment" in reply.headers["Content-Disposition"]
    assert len(json.loads(reply.get_data(as_text=True))["batches"]) == 3
    assert AuditLog.query.filter_by(action="export").count() == 1
