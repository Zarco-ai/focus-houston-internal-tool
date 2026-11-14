
import datetime
from decimal import Decimal

import pytest
from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models import Item
from app.services import catalog
from app.services import clock


def test_three_different_pants_coexist_at_three_different_prices(seeded_app):
    adult = catalog.find_item("Pants", "clothing_adult")
    children = catalog.find_item("Pants", "clothing_children")
    infant = catalog.find_item("Pants", "clothing_infant")

    assert adult.id != children.id != infant.id
    assert adult.current_price() == Decimal("24.00")
    assert children.current_price() == Decimal("21.50")
    assert infant.current_price() == Decimal("8.50")


def test_the_repeated_names_all_exist_in_all_three_clothing_categories(seeded_app):
    for name in ["Pants", "Coat", "Socks", "Shoes", "Belt", "Light Jacket"]:
        found = Item.query.filter_by(name=name).all()
        categories = {item.category for item in found}
        assert len(found) >= 2, f"{name} should repeat across categories"
        assert len(categories) == len(found), f"{name} duplicated inside a category"


def test_adult_and_infant_coats_never_resolve_to_each_other(seeded_app):
    adult = catalog.find_item("Coat", "clothing_adult")
    infant = catalog.find_item("Coat", "clothing_infant")

    assert adult.current_price() == Decimal("72.00")
    assert infant.current_price() == Decimal("18.00")
    assert adult.display_name != infant.display_name


def test_the_database_refuses_a_duplicate_name_in_one_category(seeded_app):
    db.session.add(
        Item(
            name="Coat",
            category="clothing_adult",
            report_bucket="clothing",
            used_multiplier=0.75,
            price_entry="fixed",
        )
    )
    with pytest.raises(IntegrityError):
        db.session.flush()
    db.session.rollback()


def test_creating_a_duplicate_through_the_service_gives_a_readable_message(
    seeded_app,
):
    with pytest.raises(catalog.CatalogError, match="already exists"):
        catalog.create_item(
            name="Coat",
            category="clothing_adult",
            report_bucket="clothing",
            used_multiplier=0.75,
            price_entry="fixed",
        )


def test_the_same_name_in_a_different_category_is_allowed(seeded_app):
    item = catalog.create_item(
        name="Coat",
        category="home_goods",
        report_bucket="household",
        used_multiplier=0.5,
        price_entry="fixed",
        unit_price_new=30,
    )
    assert item.id is not None
    assert catalog.find_item("Coat", "clothing_adult").current_price() == Decimal(
        "72.00"
    )


def test_a_diaper_can_be_added_with_no_schema_change(seeded_app):
    item = catalog.create_item(
        name="Diapers Size 3",
        category="diapers",
        report_bucket="diapers",
        used_multiplier=0.5,
        price_entry="fixed",
        unit_price_new=0.25,
    )

    assert item.report_bucket == "diapers"
    assert item.current_price() == Decimal("0.25")
    assert "diapers" in catalog.categories_in_use()


def test_food_can_be_added_too(seeded_app):
    item = catalog.create_item(
        name="Food Box",
        category="food",
        report_bucket="food",
        used_multiplier=0.5,
        price_entry="fixed",
        unit_price_new=35,
    )
    assert item.report_bucket == "food"


def test_a_bad_used_rate_is_refused(seeded_app):
    for bad_rate in ["0", "-1", "2", "banana"]:
        with pytest.raises(catalog.CatalogError):
            catalog.create_item(
                name=f"Thing {bad_rate}",
                category="home_goods",
                report_bucket="household",
                used_multiplier=bad_rate,
                price_entry="fixed",
            )


def test_a_bad_report_bucket_is_refused(seeded_app):
    with pytest.raises(catalog.CatalogError, match="report bucket"):
        catalog.create_item(
            name="Thing",
            category="home_goods",
            report_bucket="miscellaneous",
            used_multiplier=0.5,
            price_entry="fixed",
        )


def test_an_item_with_no_name_is_refused(seeded_app):
    with pytest.raises(catalog.CatalogError, match="name"):
        catalog.create_item(
            name="   ",
            category="home_goods",
            report_bucket="household",
            used_multiplier=0.5,
            price_entry="fixed",
        )


def test_renaming_an_item_is_written_to_the_audit_log(seeded_app):
    from app.services import audit

    coat = catalog.find_item("Coat", "clothing_adult")
    catalog.update_item(coat, name="Winter Coat")

    history = audit.history_for("item", coat.id)
    fields = {entry.field: (entry.old_value, entry.new_value) for entry in history}
    assert fields["name"] == ("Coat", "Winter Coat")


def test_deactivating_hides_an_item_without_deleting_it(seeded_app):
    coat = catalog.find_item("Coat", "clothing_adult")
    catalog.set_active(coat, False)

    assert coat.id is not None
    assert coat not in catalog.active_items()
    assert coat in catalog.all_items()


def test_changing_a_price_adds_a_row_and_keeps_the_old_one(seeded_app):
    coat = catalog.find_item("Coat", "clothing_adult")
    assert len(coat.prices) == 1

    catalog.change_price(coat, 70.00)

    assert len(coat.prices) == 2
    assert coat.current_price() == Decimal("70.00")


def test_a_price_dated_in_the_future_does_not_apply_yet(seeded_app):
    coat = catalog.find_item("Coat", "clothing_adult")
    tomorrow = clock.local_today() + datetime.timedelta(days=1)

    catalog.change_price(coat, 70.00, effective_from=tomorrow)

    assert coat.current_price() == Decimal("72.00")
    assert coat.current_price(on_date=tomorrow) == Decimal("70.00")


def test_needs_a_price_lists_every_manual_item(seeded_app):
    needing = catalog.items_needing_a_price()

    names = {item.name for item in needing}
    assert "Stroller" in names
    assert "Toys Large" in names
    assert "Small Rideable Toy Cars" in names
    assert all(item.price_entry == "manual" for item in needing)

    assert "Coat" not in names


def test_the_needs_a_price_list_shrinks_when_a_price_is_supplied(seeded_app):
    before = len(catalog.items_needing_a_price())

    stroller = catalog.find_item("Stroller", "baby_mom_items")
    catalog.change_price(stroller, 120.00)
    catalog.update_item(stroller, price_entry="fixed")

    assert len(catalog.items_needing_a_price()) == before - 1


def test_manual_items_keep_the_note_explaining_why(seeded_app):
    for item in catalog.items_needing_a_price():
        assert item.note, f"{item.display_name} has no note explaining why"


def test_every_item_is_displayed_with_its_category(seeded_app):
    for item in catalog.active_items():
        assert "·" in item.display_name


def test_an_admin_adds_an_item_through_the_page(client):
    client.post("/admin/items/new", data={
        "name": "Diapers - Size 3", "category": "diapers", "report_bucket": "diapers",
        "used_multiplier": "0.5", "price_entry": "fixed", "unit_price_new": "0.30",
    })
    added = catalog.find_item("Diapers - Size 3", "diapers")
    assert added is not None and added.current_price() == Decimal("0.30")


def test_an_admin_changes_an_items_bucket_and_used_rate_through_the_page(client):
    bottles = catalog.find_item("Bottles", "baby_essentials")
    client.post(f"/admin/items/{bottles.id}", data={
        "name": bottles.name, "category": bottles.category, "report_bucket": "hygiene",
        "used_multiplier": "0.40", "price_entry": bottles.price_entry, "note": "",
        "active": "on", "display_group": "",
    })
    db.session.refresh(bottles)
    assert (bottles.report_bucket, bottles.used_multiplier) == ("hygiene", 0.4)


def test_an_admin_changes_an_items_price_through_the_page(client):
    bottles = catalog.find_item("Bottles", "baby_essentials")
    client.post(f"/admin/items/{bottles.id}/price", data={"unit_price_new": "9.25"})
    db.session.refresh(bottles)
    assert bottles.current_price() == Decimal("9.25")
    assert len(bottles.prices) == 2
