
import csv
from decimal import Decimal

import pytest

from app.models import Item, ItemPrice, ServiceType
from app.services import catalog, seeding


def rows_in_file(path):
    with open(path, newline="", encoding="utf-8-sig") as handle:
        return [row for row in csv.DictReader(handle) if row["item_name"].strip()]


def test_every_row_in_the_csv_becomes_an_item(app, csv_path):
    expected = len(rows_in_file(csv_path))

    summary = seeding.seed_all(csv_path)

    assert summary["rows_read"] == expected
    assert summary["items_added"] == expected
    assert Item.query.count() == expected


def test_blank_prices_load_as_no_price_rather_than_zero(app, csv_path):
    seeding.seed_all(csv_path)

    stroller = catalog.find_item("Booster Seat 2-in-1", "baby_mom_items")
    assert stroller is not None
    assert stroller.current_price() is None
    assert stroller.price_entry == "manual"

    assert len(stroller.prices) == 1
    assert stroller.prices[0].unit_price_new is None


def test_items_with_a_blank_price_are_counted(app, csv_path):
    blanks = [
        row for row in rows_in_file(csv_path) if not row["unit_price_new"].strip()
    ]
    summary = seeding.seed_all(csv_path)
    assert summary["blank_prices"] == len(blanks)
    assert summary["blank_prices"] > 0


def test_every_item_gets_a_price_row(app, csv_path):
    seeding.seed_all(csv_path)
    assert ItemPrice.query.count() == Item.query.count()


def test_service_types_are_seeded(app, csv_path):
    seeding.seed_all(csv_path)
    names = {service.name for service in ServiceType.query.all()}
    assert "Diaper Distribution" in names
    assert "Bike Giveaway" in names
    assert len(names) == len(seeding.SERVICE_TYPES)


def test_used_multipliers_come_from_the_file(app, csv_path):
    seeding.seed_all(csv_path)

    for row in rows_in_file(csv_path):
        item = catalog.find_item(row["item_name"].strip(), row["category"].strip())
        assert item is not None
        assert item.used_multiplier == pytest.approx(float(row["used_multiplier"]))


def test_reseeding_does_not_duplicate_or_overwrite(app, csv_path):
    seeding.seed_all(csv_path)
    count = Item.query.count()

    coat = catalog.find_item("Coat", "clothing_adult")
    catalog.change_price(coat, 99.00)

    summary = seeding.seed_all(csv_path)

    assert Item.query.count() == count
    assert summary["items_added"] == 0
    assert summary["items_skipped"] == count
    assert catalog.find_item("Coat", "clothing_adult").current_price() == 99


def test_a_bad_report_bucket_is_refused(app, tmp_path):
    bad = tmp_path / "bad.csv"
    bad.write_text(
        "item_name,category,report_bucket,unit_price_new,used_multiplier,"
        "price_entry,notes\n"
        "Thing,home_goods,not_a_bucket,5.00,0.5,fixed,\n"
    )
    with pytest.raises(seeding.SeedError, match="report_bucket"):
        seeding.read_catalog_csv(str(bad))


def test_a_missing_used_rate_is_refused(app, tmp_path):
    bad = tmp_path / "bad.csv"
    bad.write_text(
        "item_name,category,report_bucket,unit_price_new,used_multiplier,"
        "price_entry,notes\n"
        "Thing,home_goods,household,5.00,,fixed,\n"
    )
    with pytest.raises(seeding.SeedError, match="used_multiplier"):
        seeding.read_catalog_csv(str(bad))


def test_a_missing_column_is_refused(app, tmp_path):
    bad = tmp_path / "bad.csv"
    bad.write_text("item_name,category\nThing,home_goods\n")
    with pytest.raises(seeding.SeedError, match="missing columns"):
        seeding.read_catalog_csv(str(bad))


def test_load_if_empty_loads_a_new_database(app, csv_path):
    from app.services.seeding import load_if_empty

    summary = load_if_empty(csv_path)
    assert summary["items_added"] == len(rows_in_file(csv_path))


def test_load_if_empty_never_touches_an_existing_catalog(seeded_app):
    from app.models import Item
    from app.services import catalog
    from app.services.seeding import load_if_empty

    coat = catalog.find_item("Coat", "clothing_adult")
    catalog.update_item(coat, name="Winter Coat")
    count = Item.query.count()

    assert load_if_empty(seeded_app.config["SEED_CSV_PATH"]) is None
    assert Item.query.count() == count
    assert catalog.find_item("Coat", "clothing_adult") is None


def test_the_written_file_loads_back_as_the_same_catalog(seeded_app, tmp_path):
    from app.models import Item
    from app.services import catalog, seeding

    wipes = catalog.find_item("Individual Pack of Wipes", "baby_essentials")
    catalog.update_item(wipes, display_group="diapers")
    before = {(i.name, i.category, i.report_bucket, i.display_group, i.current_price(),
               i.used_multiplier, i.price_entry, i.note) for i in Item.query.all()}

    path = tmp_path / "catalog.csv"
    assert seeding.write_catalog_csv(path, Item.query.all()) == len(before)
    rows = seeding.read_catalog_csv(path)

    after = {(r["name"], r["category"], r["report_bucket"], r["display_group"],
              None if r["unit_price_new"] is None else Decimal(str(r["unit_price_new"])).quantize(Decimal("0.01")),
              r["used_multiplier"], r["price_entry"], r["note"]) for r in rows}
    assert after == before


def test_a_bad_display_group_is_refused(app, tmp_path):
    from app.services.seeding import SeedError, read_catalog_csv

    path = tmp_path / "bad.csv"
    path.write_text(
        "item_name,category,report_bucket,unit_price_new,used_multiplier,price_entry,notes,display_group\n"
        "Wipes,baby_essentials,hygiene,3.00,0.50,fixed,,snacks\n"
    )
    with pytest.raises(SeedError, match="display_group"):
        read_catalog_csv(path)
