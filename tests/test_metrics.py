
import datetime
from decimal import Decimal

import pytest

from app.extensions import db
from app.models import AuditLog, MetricDefinition, ServiceType
from app.services import attendance, batches, catalog, clock, metrics
from app.services.attendance import FIELDS


def item(name, category):
    found = catalog.find_item(name, category)
    assert found is not None, f"{name} ({category}) is not in the catalog"
    return found


def committed(lines=(), counts=None, date=None):
    batch = batches.open_batch(date=date or clock.local_today(),
                               service_type_id=ServiceType.query.first().id)
    for thing, quantity, condition, price in lines:
        batches.set_line(batch, thing, quantity=quantity, condition=condition, unit_price=price)
    if counts:
        batches.set_attendance(batch, counts)
    batches.commit_batch(batch)
    return batch


def this_month():
    today = clock.local_today()
    from app.services.totals import month_range
    return month_range(today.year, today.month)


def a_mixed_month():
    committed([
        (item("Coat", "clothing_adult"), 1, "new", None),
        (item("Individual Pack of Wipes", "baby_essentials"), 4, "new", None),
        (item("Bottles", "baby_essentials"), 2, "used", None),
        (item("Individual Diaper", "diapers"), 100, "new", None),
        (item("Formula - Similac 7oz", "baby_essentials"), 3, "new", "11.50"),
        (item("Bus Pass", "transportation"), 2, "new", "10.00"),
    ])


def test_every_metric_key_is_unique_and_every_group_exists():
    keys = [metric.key for metric in metrics.METRICS]
    assert len(keys) == len(set(keys))
    assert {metric.group for metric in metrics.METRICS} <= set(metrics.GROUPS_BY_KEY)


def test_every_metric_has_a_function_and_a_label():
    for metric in metrics.METRICS:
        assert callable(metric.compute), metric.key
        assert metric.label.strip(), metric.key
        assert metric.unit in (metrics.UNIT_COUNT, metrics.UNIT_MONEY, metrics.UNIT_HOURS,
                               metrics.UNIT_MINUTES, metrics.UNIT_PERCENT)


def test_every_attendance_box_belongs_to_a_metric():
    for field in FIELDS:
        assert field.metric in metrics.REGISTRY, field.name


def test_the_template_order_is_kept():
    order = [group.key for group in metrics.GROUPS]
    assert order == ["SERVED", "FOOD", "OCCUPATION", "CLOTHING", "UNDERSTANDING",
                     "SHELTER", "VOLUNTEERS", "UNIQUE", "ANNUAL", "PROGRAM_HEALTH"]


def test_every_metric_works_out_on_an_empty_database(app):
    values = metrics.values(*this_month())
    assert set(values) == set(metrics.REGISTRY)
    for key, value in values.items():
        metric = metrics.REGISTRY[key]
        if metric.frequency == metrics.ANNUAL:
            assert value is None
        elif metric.unit == metrics.UNIT_PERCENT:
            assert value is None, key
        elif key in ("intake_minutes_mean", "intake_minutes_median"):
            assert value is None, key
        else:
            assert value == 0, key


def test_non_food_gik_is_goods_value_plus_diaper_value(seeded_app):
    a_mixed_month()
    values = metrics.values(*this_month())

    assert values["non_food_gik"] == values["goods_value"] + values["diapers_value"]
    assert values["diapers_value"] == Decimal("53.00")


def test_bus_passes_never_count_toward_non_food_gik(seeded_app):
    without = metrics.values(*this_month())["non_food_gik"]
    committed([(item("Bus Pass", "transportation"), 5, "new", "25.00")])
    values = metrics.values(*this_month())

    assert values["non_food_gik"] == without
    assert values["bus_passes_count"] == 5
    assert values["transportation_value"] == Decimal("125.00")


def test_formula_is_food_gik_and_never_non_food(seeded_app):
    committed([(item("Formula - Similac 7oz", "baby_essentials"), 3, "new", "12.00")])
    values = metrics.values(*this_month())

    assert values["non_food_gik"] == 0
    assert values["formula_containers"] == 3
    assert values["food_value"] == Decimal("36.00")


def test_food_value_is_food_plus_formula(seeded_app):
    rice = catalog.create_item(name="Rice 2lb", category="food", report_bucket="food",
                               used_multiplier=0.5, price_entry="fixed", unit_price_new=3)
    committed([(rice, 2, "new", None),
               (item("Formula - Similac 7oz", "baby_essentials"), 1, "new", "11.50")])

    assert metrics.values(*this_month())["food_value"] == Decimal("17.50")


def test_goods_count_is_clothing_hygiene_and_household_only(seeded_app):
    a_mixed_month()
    values = metrics.values(*this_month())

    assert values["goods_count"] == 1 + 4 + 2
    assert values["diapers_count"] == 100


def test_wipes_listed_with_diapers_are_still_hygiene_money(seeded_app):
    committed([(item("Individual Pack of Wipes", "baby_essentials"), 1, "new", None)])
    values = metrics.values(*this_month())

    assert values["diapers_count"] == 0
    assert values["goods_count"] == 1


def test_a_month_outside_the_period_is_not_counted(seeded_app):
    last_month = clock.local_today().replace(day=1) - datetime.timedelta(days=1)
    committed([(item("Coat", "clothing_adult"), 1, "new", None)], date=last_month)

    assert metrics.values(*this_month())["goods_count"] == 0


def test_drafts_are_not_counted(seeded_app):
    batch = batches.open_batch(service_type_id=ServiceType.query.first().id)
    batches.set_line(batch, item("Coat", "clothing_adult"), quantity=1)
    batches.set_attendance(batch, {"individuals_served": 5})

    values = metrics.values(*this_month())
    assert values["goods_count"] == 0
    assert values["individuals_served"] == 0


def test_attendance_figures_add_up_across_batches(seeded_app):
    committed(counts={"individuals_served": 3, "volunteer_hours_tenths": 25,
                      "meals_value": Decimal("40.50")})
    committed(counts={"individuals_served": 4, "volunteer_hours_tenths": 10,
                      "meals_value": Decimal("9.50")})
    values = metrics.values(*this_month())

    assert values["individuals_served"] == 7
    assert values["volunteer_hours"] == Decimal("3.5")
    assert values["meals_value"] == Decimal("50.00")


def test_unique_counts_are_labelled_as_sums_of_per_event_counts():
    for key in ("unique_clients_event_sum", "unique_volunteers_event_sum",
                "unique_parishes_event_sum"):
        assert "sum of per-event counts" in metrics.REGISTRY[key].label


def test_an_annual_figure_shows_only_for_a_whole_year(app, admin_user):
    year = clock.local_today().year
    metrics.set_annual_value(year, "parish_donors", 12, entered_by_id=admin_user.id)

    whole = metrics.values(datetime.date(year, 1, 1), datetime.date(year, 12, 31))
    month = metrics.values(*this_month())
    assert whole["parish_donors"] == 12
    assert month["parish_donors"] is None


def test_annual_figures_are_audited_and_can_be_cleared(app, admin_user):
    metrics.set_annual_value(2026, "one_time_donors", 40, entered_by_id=admin_user.id)
    metrics.set_annual_value(2026, "one_time_donors", 41, entered_by_id=admin_user.id)
    metrics.set_annual_value(2026, "one_time_donors", None, entered_by_id=admin_user.id)
    db.session.commit()

    rows = AuditLog.query.filter_by(table_name="annual_metric_value").order_by(AuditLog.id).all()
    assert [(r.old_value, r.new_value) for r in rows] == [(None, "40"), ("40", "41"), ("41", None)]
    assert all(r.user_id == admin_user.id for r in rows)
    assert metrics.annual_values(2026) == {}


@pytest.mark.parametrize("key, value", [("individuals_served", 3), ("parish_donors", -1),
                                        ("parish_donors", 1.5)])
def test_a_wrong_annual_figure_is_refused(app, admin_user, key, value):
    with pytest.raises(ValueError):
        metrics.set_annual_value(2026, key, value, entered_by_id=admin_user.id)


def test_every_metric_is_on_by_default_except_the_ones_focus_doesnt_report(app):
    off = {s.key for s in metrics.settings() if not s.enabled}
    assert off == {"retention_focus", "meals_value", "temp_employment_placed",
                   "medical_services_served", "bus_passes_count", "transportation_value"}
    assert MetricDefinition.query.count() == 0
    metrics.update_setting("retention_focus", enabled=True)
    assert metrics.is_enabled("retention_focus")


def test_a_switched_off_metric_leaves_the_report(app):
    metrics.update_setting("food_value", enabled=False)
    keys = {row.key for group in metrics.report(*this_month()) for row in group.rows}

    assert "food_value" not in keys
    assert "non_food_gik" in keys


def test_a_group_with_nothing_switched_on_leaves_the_report(app):
    metrics.update_setting("temp_employment_placed", enabled=False)
    groups = [group.group.key for group in metrics.report(*this_month())]
    assert "OCCUPATION" not in groups


def test_a_new_label_is_used_and_a_blank_one_puts_the_default_back(app):
    metrics.update_setting("parish_donors", label="  # of Parishes   that gave ")
    assert _setting("parish_donors").label == "# of Parishes that gave"

    metrics.update_setting("parish_donors", label="")
    assert _setting("parish_donors").label == metrics.REGISTRY["parish_donors"].label
    assert MetricDefinition.query.filter_by(key="parish_donors").one().label is None


def test_metric_changes_are_audited(app, admin_user):
    metrics.update_setting("food_value", enabled=False)
    db.session.commit()

    row = AuditLog.query.filter_by(table_name="metric_definition", field="enabled").one()
    assert (row.old_value, row.new_value, row.user_id) == ("yes", "no", admin_user.id)


def test_an_unknown_metric_key_is_refused(app):
    with pytest.raises(ValueError):
        metrics.update_setting("snacks_value", enabled=False)


def test_a_switched_off_metric_still_has_its_value(seeded_app):
    committed(counts={"meals_value": Decimal("20.00")})
    metrics.update_setting("meals_value", enabled=False)
    assert metrics.values(*this_month())["meals_value"] == Decimal("20.00")


def _setting(key):
    return next(s for s in metrics.settings() if s.key == key)


@pytest.mark.parametrize("start, end, year", [
    (datetime.date(2026, 1, 1), datetime.date(2026, 12, 31), 2026),
    (datetime.date(2026, 1, 1), datetime.date(2026, 12, 30), None),
    (datetime.date(2026, 1, 2), datetime.date(2026, 12, 31), None),
    (datetime.date(2025, 1, 1), datetime.date(2026, 12, 31), None),
])
def test_whole_year(start, end, year):
    assert metrics.whole_year(start, end) == year


def test_hours_are_shown_without_a_trailing_point_zero():
    assert str(attendance.hours_from_tenths(30)) == "3"
    assert str(attendance.hours_from_tenths(25)) == "2.5"
