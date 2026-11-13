"""Editing a line's price, used rate and bucket after counting (Phase 3).

Two rules, both from the client's answers on 27 October 2026:

  * Every price and used rate can be changed on the review screen. On a
    draft a typed value is kept: the catalog stops overwriting it. On an
    approved batch the change goes through the audit log.
  * A line's report bucket is frozen when its batch is approved, like its
    price and rate. Changing an item's bucket sorts future batches and leaves
    months already reported alone.
"""

import datetime
from decimal import Decimal

import pytest

from app.extensions import db
from app.models import AuditLog, ServiceType
from app.services import batches, catalog, review, totals
from app.services import clock


def a_batch():
    return batches.open_batch(service_type_id=ServiceType.query.first().id)


def bottles():
    return catalog.find_item("Bottles", "baby_essentials")  # $8.50, fixed, 0.5


def today_range():
    today = clock.local_today()
    return today, today


# --- The typed rate ----------------------------------------------------------------


@pytest.mark.parametrize("typed, rate", [("0.75", 0.75), (".5", 0.5), ("75%", 0.75),
                                         (" 100% ", 1.0), ("", None), (None, None)])
def test_a_used_rate_can_be_typed_as_a_fraction_or_a_percent(typed, rate):
    assert batches.parse_used_rate(typed) == rate


@pytest.mark.parametrize("typed", ["0", "1.5", "150%", "-0.5", "half"])
def test_a_used_rate_outside_0_to_1_is_refused(typed):
    with pytest.raises(ValueError):
        batches.parse_used_rate(typed)


# --- Drafts: a typed value is kept --------------------------------------------------


def test_a_price_typed_on_a_fixed_item_is_kept(seeded_app):
    batch = a_batch()
    line = batches.set_line(batch, bottles(), quantity=2)

    batches.update_draft_line(line, unit_price="6.00")
    batches.update_draft_line(line, quantity=3, unit_price="6.00")  # a later save
    batches.set_line(batch, bottles(), quantity=4)                   # the counting screen

    assert line.unit_price_at_time == Decimal("6.00")
    assert line.computed_value == Decimal("24.00")


def test_a_price_box_left_alone_keeps_following_the_catalog(seeded_app):
    batch = a_batch()
    line = batches.set_line(batch, bottles(), quantity=1)
    batches.update_draft_line(line, unit_price="8.50")  # the box, sent back unchanged

    catalog.change_price(bottles(), "9.00")
    batches.update_draft_line(line, quantity=1)

    assert not line.price_typed
    assert line.unit_price_at_time == Decimal("9.00")


def test_a_used_rate_typed_on_a_draft_is_kept_and_used(seeded_app):
    batch = a_batch()
    line = batches.set_line(batch, bottles(), quantity=2, condition="used")
    assert line.computed_value == Decimal("8.50")  # 2 x $8.50 x 0.5

    batches.update_draft_line(line, used_multiplier=0.75)
    batches.set_line(batch, bottles(), quantity=2, condition="used")

    assert line.used_multiplier_at_time == 0.75
    assert line.computed_value == Decimal("12.75")


# --- Approved batches: corrections are audited ---------------------------------------


def test_correcting_an_approved_lines_used_rate_is_audited(seeded_app):
    batch = a_batch()
    line = batches.set_line(batch, bottles(), quantity=2, condition="used")
    batches.commit_batch(batch)

    batches.correct_line(line, used_multiplier=0.25)

    assert line.computed_value == Decimal("4.25")
    fields = {row.field for row in AuditLog.query.filter_by(table_name="line_item", record_id=line.id)}
    assert {"used_multiplier_at_time", "computed_value"} <= fields


# --- Buckets freeze at approval -------------------------------------------------------


def test_approval_freezes_the_bucket(seeded_app):
    batch = a_batch()
    line = batches.set_line(batch, bottles(), quantity=1)
    assert line.report_bucket_at_time is None  # a draft follows its item
    batches.commit_batch(batch)
    assert line.report_bucket_at_time == "household"


def test_changing_an_items_bucket_leaves_reported_months_alone(seeded_app):
    last_month = clock.local_today().replace(day=1) - datetime.timedelta(days=1)
    old = batches.open_batch(date=last_month, service_type_id=ServiceType.query.first().id)
    batches.set_line(old, bottles(), quantity=1)
    batches.commit_batch(old)

    catalog.update_item(bottles(), report_bucket="hygiene")

    new = a_batch()
    batches.set_line(new, bottles(), quantity=1)
    batches.commit_batch(new)

    before = totals.bucket_totals(last_month, last_month)
    assert set(before) == {"household"}
    assert set(totals.bucket_totals(*today_range())) == {"hygiene"}


def test_one_approved_line_can_be_re_sorted_and_it_is_audited(seeded_app):
    batch = a_batch()
    line = batches.set_line(batch, bottles(), quantity=1)
    batches.commit_batch(batch)

    batches.correct_line(line, report_bucket="hygiene")

    assert line.report_bucket == "hygiene"
    assert bottles().report_bucket == "household"  # the item itself is unchanged
    assert AuditLog.query.filter_by(table_name="line_item", record_id=line.id,
                                    field="report_bucket_at_time").count() == 1


def test_an_unknown_bucket_is_refused(seeded_app):
    batch = a_batch()
    line = batches.set_line(batch, bottles(), quantity=1)
    batches.commit_batch(batch)
    with pytest.raises(ValueError):
        batches.correct_line(line, report_bucket="snacks")


def test_resolving_an_approved_custom_line_freezes_its_bucket(seeded_app):
    batch = a_batch()
    line = batches.add_custom_line(batch, "Mystery onesie", quantity=1, unit_price=5)
    batches.commit_batch(batch)

    review.resolve_line(line, item=catalog.find_item("Pants", "clothing_infant"))

    assert line.report_bucket_at_time == "clothing"


# --- The review screen ------------------------------------------------------------------


def test_the_review_screen_saves_a_typed_used_rate(client, seeded_app):
    batch = a_batch()
    line = batches.set_line(batch, bottles(), quantity=2, condition="used")
    db.session.commit()

    page = client.get(f"/batch/{batch.id}/review").get_data(as_text=True)
    assert f'name="used_rate-{line.id}"' in page
    assert f'name="bucket-{line.id}"' not in page  # a draft has no frozen bucket yet

    client.post(f"/batch/{batch.id}/review/lines", data={
        f"quantity-{line.id}": "2", f"condition-{line.id}": "used",
        f"unit_price-{line.id}": "7.00", f"used_rate-{line.id}": "75%",
    })
    db.session.refresh(line)
    assert line.used_multiplier_at_time == 0.75
    assert line.computed_value == Decimal("10.50")


def test_the_review_screen_refuses_a_bad_used_rate(client, seeded_app):
    batch = a_batch()
    line = batches.set_line(batch, bottles(), quantity=2, condition="used")
    db.session.commit()

    response = client.post(f"/batch/{batch.id}/review/lines", data={
        f"quantity-{line.id}": "2", f"used_rate-{line.id}": "2",
    }, follow_redirects=True)

    assert "at most 1" in response.get_data(as_text=True)
    db.session.refresh(line)
    assert line.used_multiplier_at_time == 0.5


def test_an_approved_batch_offers_a_bucket_per_line(client, seeded_app):
    batch = a_batch()
    line = batches.set_line(batch, bottles(), quantity=1)
    batches.commit_batch(batch)

    page = client.get(f"/batch/{batch.id}/review").get_data(as_text=True)
    assert f'name="bucket-{line.id}"' in page

    client.post(f"/batch/{batch.id}/review/lines", data={
        f"quantity-{line.id}": "1", f"unit_price-{line.id}": "7.00",
        f"used_rate-{line.id}": "0.50", f"bucket-{line.id}": "hygiene",
    })
    db.session.refresh(line)
    assert line.report_bucket == "hygiene"
