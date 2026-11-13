"""The Totals page and the admin pages behind it (Phase 4).

What the page shows for a period, how the period is picked, and that only
an admin changes what the report contains.
"""

import datetime

from app.extensions import db
from app.models import AnnualMetricValue, MetricDefinition, ServiceType
from app.services import batches, catalog, clock, metrics


def approve(lines=(), counts=None, date=None):
    batch = batches.open_batch(date=date or clock.local_today(),
                               service_type_id=ServiceType.query.first().id)
    for item, quantity, price in lines:
        batches.set_line(batch, item, quantity=quantity, unit_price=price)
    if counts:
        batches.set_attendance(batch, counts)
    batches.commit_batch(batch)
    return batch


def page(client, **params):
    return client.get("/totals", query_string=params).get_data(as_text=True)


# --- What it shows ---------------------------------------------------------------------


def test_the_page_shows_every_group_in_template_order(client, seeded_app):
    for setting in metrics.settings():   # some start off (meals, medical...)
        metrics.update_setting(setting.key, enabled=True)
    text = page(client)
    positions = [text.index(f">{group.label}<") for group in metrics.GROUPS]
    assert positions == sorted(positions)


def test_non_food_gik_on_the_page_is_the_computed_one(client, seeded_app):
    approve([(catalog.find_item("Coat", "clothing_adult"), 1, None),
             (catalog.find_item("Individual Diaper", "diapers"), 10, None),
             (catalog.find_item("Bus Pass", "transportation"), 1, "10.00")])
    text = page(client)

    assert "Non-Food GIK $&#39;s" in text or "Non-Food GIK $'s" in text
    assert "$77.30" in text   # $72 coat + 10 x $0.53, and no bus pass
    assert "$10.00" in text   # the bus pass, on its own line


def test_a_switched_off_metric_is_not_on_the_page(client, seeded_app):
    metrics.update_setting("meals_value", enabled=False)
    db.session.commit()
    assert "Value ($) of Meals Provided" not in page(client)


def test_a_renamed_metric_shows_its_new_label(client, seeded_app):
    metrics.update_setting("parish_donors", label="# of Parishes that gave")
    db.session.commit()
    assert "# of Parishes that gave" in page(client, year=2026)


def test_unpriced_and_unsorted_lines_are_called_out(client, seeded_app):
    batch = batches.open_batch(service_type_id=ServiceType.query.first().id)
    batches.set_line(batch, catalog.find_item("Bus Pass", "transportation"), quantity=1)
    batches.add_custom_line(batch, "Mystery onesie", quantity=2, unit_price=5)
    batches.commit_batch(batch)

    text = " ".join(page(client).split())
    assert "1 line in this period has no value yet" in text
    assert "2 items ($10.00) are not in the" in text


def test_annual_figures_need_a_whole_year(client, seeded_app, admin_user):
    year = clock.local_today().year
    metrics.set_annual_value(year, "advisory_board_members", 9, entered_by_id=admin_user.id)
    db.session.commit()

    assert "whole year only" in page(client)
    year_page = page(client, year=year)
    assert "whole year only" not in year_page
    assert ">9<" in year_page


# --- Picking the period -----------------------------------------------------------------


def test_the_default_is_the_current_month(client, seeded_app):
    today = clock.local_today()
    assert today.strftime("%B %Y") in page(client)


def test_a_month_a_year_and_a_range(client, seeded_app):
    last_year = clock.local_today().year - 1
    approve([(catalog.find_item("Coat", "clothing_adult"), 1, None)],
            date=datetime.date(last_year, 3, 15))

    assert "$72.00" in page(client, month=f"{last_year}-03")
    assert "$72.00" not in page(client, month=f"{last_year}-04")
    assert "$72.00" in page(client, year=last_year)
    assert "$72.00" in page(client, start=f"{last_year}-03-15", end=f"{last_year}-03-15")


def test_a_range_that_is_a_whole_month_is_shown_as_the_month(client, seeded_app):
    assert "March 2025" in page(client, start="2025-03-01", end="2025-03-31")


def test_a_backwards_range_says_so_and_shows_this_month(client, seeded_app):
    text = page(client, start="2025-05-01", end="2025-04-01")
    assert "start date was after the end date" in text
    assert clock.local_today().strftime("%B %Y") in text


def test_nonsense_in_the_address_shows_this_month(client, seeded_app):
    text = page(client, month="2026-13", year="banana")
    assert clock.local_today().strftime("%B %Y") in text


def test_previous_and_next_cross_a_year_boundary(client, seeded_app):
    text = page(client, month="2026-01")
    assert "month=2025-12" in text and "month=2026-02" in text


def test_the_page_prints(client, seeded_app):
    """A print button, a heading only paper sees, and print styles."""
    text = page(client)
    assert "data-print" in text and "print-heading" in text
    css = client.get("/static/css/app.css").get_data(as_text=True)
    assert "@media print" in css


def test_staff_see_totals(staff_client, seeded_app):
    assert staff_client.get("/totals").status_code == 200


# --- The admin pages --------------------------------------------------------------------


def test_the_metrics_page_switches_off_and_renames(client, seeded_app):
    form = {f"enabled-{s.key}": "on" for s in metrics.settings() if s.enabled}
    form.pop("enabled-food_value")
    form["label-parish_donors"] = "# of Parishes that gave"

    response = client.post("/admin/metrics", data=form, follow_redirects=True)

    assert "2 metrics changed" in response.get_data(as_text=True)
    assert not metrics.is_enabled("food_value")
    assert MetricDefinition.query.filter_by(key="parish_donors").one().label == "# of Parishes that gave"


def test_the_annual_page_saves_and_refuses_bad_numbers(client, seeded_app):
    client.post("/admin/annual", data={"year": "2025", "parish_donors": "4"})
    assert AnnualMetricValue.query.filter_by(year=2025).one().value == 4

    response = client.post("/admin/annual", data={"year": "2025", "parish_donors": "-4"},
                           follow_redirects=True)
    assert "Nothing was saved" in response.get_data(as_text=True)
    assert AnnualMetricValue.query.filter_by(year=2025).one().value == 4

    client.post("/admin/annual", data={"year": "2025", "parish_donors": ""})
    assert AnnualMetricValue.query.filter_by(year=2025).count() == 0


def test_staff_cannot_change_metrics_or_annual_figures(staff_client, seeded_app):
    for path in ("/admin/metrics", "/admin/annual"):
        assert staff_client.get(path).status_code == 403
        assert staff_client.post(path, data={}).status_code == 403
    assert MetricDefinition.query.count() == 0
