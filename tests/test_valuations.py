
import pathlib
from decimal import Decimal

import openpyxl
import pytest

from app.constants import COMBINED_GOODS_BUCKETS
from app.extensions import db
from app.models import AuditLog, Item, ItemPrice
from app.models.audit import ACTION_IMPORT
from app.services import audit, clock, valuations
from app.services.valuations import SourceRow, plan_import, read_price, read_workbook

REAL_WORKBOOK = pathlib.Path(__file__).resolve().parent.parent / "Valuations - FOCUS Houston.xlsx"


def make_workbook(path, goods=(), clothing=(), extra_sheets=()):
    book = openpyxl.Workbook()
    sheet = book.active
    sheet.title = "Baby Essentials"
    sheet.append(["Item", "Estimated Avg Retail Price New", "Quantity", "$ Received"])
    sheet.append([None, "all depends on size & quantity"])
    for row in goods:
        sheet.append(list(row))
    clothes = book.create_sheet("Clothing Val")
    clothes.append(["FOCUS North America"])
    clothes.append([None, None, "Retail", "Used (75%)"])
    for row in clothing:
        clothes.append(list(row))
    for name in extra_sheets:
        book.create_sheet(name)
    book.save(path)
    return path


def row(new, quantity=None, received=None, rate="0.50"):
    return SourceRow(sheet="Baby Essentials", row=7, category="baby_mom_items",
                     name="Thing", new_cell=new, used_rate=Decimal(rate),
                     quantity_cell=quantity, received_cell=received)


@pytest.fixture
def before_import(seeded_app):
    db.session.delete(Item.query.filter_by(name="Thermometer").one())
    db.session.commit()
    return seeded_app


def plan_for(path):
    return plan_import(read_workbook(path), Item.query.all())


def apply(plan):
    valuations.apply_plan(plan, effective_from=clock.local_today())
    db.session.commit()


def test_a_plain_price_is_fixed():
    reading = read_price(row(20))
    assert (reading.price, reading.price_entry, reading.reason) == (Decimal("20.00"), "fixed", None)


@pytest.mark.parametrize("cell, says", [
    (None, "No price"),
    ("Manual price", "Manual price"),
    ("$40-$1,000", "range"),
    ("$5-49", "range"),
    ("..00000", "isn't a price"),
])
def test_anything_that_isnt_a_clear_price_becomes_manual_with_the_reason(cell, says):
    reading = read_price(row(cell))
    assert reading.price is None
    assert reading.price_entry == "manual"
    assert says in reading.reason
    assert "Baby Essentials row 7" in reading.reason


def test_received_divided_by_quantity_is_one_items_price():
    reading = read_price(row(200, quantity=2, received=200))
    assert (reading.price, reading.price_entry) == (Decimal("100.00"), "fixed")


def test_received_with_no_quantity_is_for_one_item():
    assert read_price(row(30, received=15)).price == Decimal("15.00")


def test_a_per_item_price_rounds_the_way_the_app_rounds_money():
    assert read_price(row(10, quantity=3, received=10)).price == Decimal("3.33")
    assert read_price(row(10, quantity=8, received=1)).price == Decimal("0.13")


def test_received_beside_a_range_is_a_pre_fill_not_a_fixed_price():
    reading = read_price(row("$40-$1,000", quantity=3, received=180))
    assert (reading.price, reading.price_entry) == (Decimal("60.00"), "manual")
    assert "range" in reading.reason and "$60.00" in reading.reason


def test_received_that_isnt_a_number_is_manual():
    reading = read_price(row(20, received="..00000"))
    assert reading.price_entry == "manual"
    assert "..00000" in reading.reason
    assert reading.price == Decimal("20.00")


def test_clothings_used_column_is_not_imported(tmp_path):
    path = make_workbook(tmp_path / "v.xlsx", clothing=[
        ("Adult", None, None, None), (None, "Cap/hat", 25, 7)])
    [cap] = read_workbook(path)
    reading = read_price(cap)
    assert (reading.price, reading.price_entry, cap.used_rate) == (Decimal("25.00"), "fixed", Decimal("0.75"))


def test_numbers_special_dashes_and_spaces_compare_as_plain_ones():
    assert valuations.match_key("Baby Wash/Shampoo (2‑in‑1)  ") == \
        valuations.match_key("baby wash/shampoo (2-in-1)")


def test_section_headings_set_the_category(tmp_path):
    path = make_workbook(tmp_path / "v.xlsx", goods=[
        ("Thermometer", 9),
        ("Womens Hygine", None),
        ("Toothbrush", 2),
        ("Baby/Mom Items", "Range"),
        ("Crib", 200),
    ], clothing=[
        ("Infant", None, None, None),
        (None, "Pants", 7, 5.25),
    ])
    rows = read_workbook(path)
    assert [(r.category, r.name) for r in rows] == [
        ("baby_essentials", "Thermometer"),
        ("womens_hygiene", "Toothbrush"),
        ("baby_mom_items", "Crib"),
        ("clothing_infant", "Pants"),
    ]
    assert [r.used_rate for r in rows] == [Decimal("0.50")] * 3 + [Decimal("0.75")]


def test_an_unknown_section_heading_stops_the_import(tmp_path):
    path = make_workbook(tmp_path / "v.xlsx", clothing=[
        ("Adult", None, None, None),
        (None, "Pants", 20, 15),
        ("Teens", None, None, None),
        (None, "Pants", 18, 13.5),
    ])
    with pytest.raises(valuations.ImportError_, match="Teens"):
        read_workbook(path)


def test_an_unknown_sheet_stops_the_import(tmp_path):
    path = make_workbook(tmp_path / "v.xlsx", extra_sheets=["Medical"])
    with pytest.raises(valuations.ImportError_, match="Medical"):
        read_workbook(path)


def test_a_file_that_isnt_a_workbook_is_refused_cleanly(tmp_path):
    path = tmp_path / "v.xlsx"
    path.write_text("not a spreadsheet")
    with pytest.raises(valuations.ImportError_, match="Can't open"):
        read_workbook(path)


def test_the_dry_run_changes_nothing(before_import, tmp_path):
    path = make_workbook(tmp_path / "v.xlsx", goods=[("Thermometer", 9), ("Bottles", 8)])
    items, prices, logs = Item.query.count(), ItemPrice.query.count(), AuditLog.query.count()

    plan = plan_for(path)
    valuations.describe(plan)

    assert plan.new_items and plan.price_changes
    assert (Item.query.count(), ItemPrice.query.count(), AuditLog.query.count()) == (items, prices, logs)


def test_a_differently_spelled_name_matches_through_the_aliases(seeded_app, tmp_path):
    path = make_workbook(tmp_path / "v.xlsx", goods=[("Formula (7‑oz Similac )", 12)])
    plan = plan_for(path)
    assert not plan.unmatched_rows
    assert not any("Formula" in n.name for n in plan.new_items)
    assert [c.item_label for c in plan.price_changes] == ["Formula - Similac 7oz · baby_essentials"]


def test_a_row_no_table_covers_blocks_apply(seeded_app, tmp_path):
    path = make_workbook(tmp_path / "v.xlsx", goods=[("Mystery Gadget", 4)])
    plan = plan_for(path)
    assert plan.blocked
    assert "BLOCKING" in "\n".join(valuations.describe(plan))
    with pytest.raises(valuations.ImportError_):
        apply(plan)


def test_an_item_listed_twice_becomes_manual_with_both_prices(seeded_app, tmp_path):
    path = make_workbook(tmp_path / "v.xlsx", goods=[
        ("Womens Hygine", None),
        ("Dispossable Underwear", 10),
        ("Disposable underwear", 23),
    ])
    reading = valuations._reading_for(
        [r for r in read_workbook(path)]
    )
    assert reading.price_entry == "manual"
    assert "$10.00" in reading.reason and "$23.00" in reading.reason


def test_formula_becomes_typed_per_receipt(seeded_app, tmp_path):
    path = make_workbook(tmp_path / "v.xlsx", goods=[("Formula (20 oz Enfamil)", 32.49)])
    apply(plan_for(path))
    formula = Item.query.filter_by(name="Formula - Enfamil 20oz").one()
    assert formula.price_entry == "manual"
    assert formula.current_price() == Decimal("32.49")
    assert formula.report_bucket == "formula"


def test_the_toy_cars_and_wind_chime_stay_typed(seeded_app, tmp_path):
    path = make_workbook(tmp_path / "v.xlsx", goods=[
        ("HOME GOODS", None), ("Small Rideable Toy Cars", 50),
        ("Baby/Mom Items", "Range"), ("Crib Wind Chime", 22, None, 15)])
    chime = Item.query.filter_by(name="Crib Wind Chime").one()
    note = chime.note

    apply(plan_for(path))

    db.session.refresh(chime)
    assert (chime.price_entry, chime.note) == ("manual", note)
    assert chime.current_price() == Decimal("15.00")
    assert Item.query.filter_by(name="Small Rideable Toy Cars").one().price_entry == "manual"


def test_catalog_items_the_workbook_doesnt_list_are_left_alone(before_import, tmp_path):
    path = make_workbook(tmp_path / "v.xlsx", goods=[("Thermometer", 9)])
    coat = Item.query.filter_by(name="Coat", category="clothing_adult").one()
    before = (coat.current_price(), coat.price_entry, coat.active)

    plan = plan_for(path)
    apply(plan)

    assert "Coat · clothing_adult" in plan.left_alone
    db.session.refresh(coat)
    assert (coat.current_price(), coat.price_entry, coat.active) == before


def test_a_price_change_adds_a_row_and_keeps_the_old_one(seeded_app, tmp_path):
    bottles = Item.query.filter_by(name="Bottles").one()
    old_rows = len(bottles.prices)

    apply(plan_for(make_workbook(tmp_path / "v.xlsx", goods=[("Bottles", 8)])))

    db.session.refresh(bottles)
    assert len(bottles.prices) == old_rows + 1
    assert [p.unit_price_new for p in bottles.prices][:old_rows] == [Decimal("8.50")]
    assert bottles.current_price() == Decimal("8.00")


def test_running_it_twice_changes_nothing_the_second_time(before_import, tmp_path):
    path = make_workbook(tmp_path / "v.xlsx", goods=[("Thermometer", 9), ("Bottles", 8),
                                                     ("Baby/Mom Items", "Range"), ("Crib", 200, 2, 200)],
                         clothing=[("Adult", None, None, None), (None, "Cap/hat", 25, 7)])
    apply(plan_for(path))
    counts = (Item.query.count(), ItemPrice.query.count(), AuditLog.query.count())

    again = plan_for(path)

    assert again.is_empty
    assert "Nothing to change" in "\n".join(valuations.describe(again))
    assert (Item.query.count(), ItemPrice.query.count(), AuditLog.query.count()) == counts


def test_every_change_is_in_the_audit_log_under_the_admin(before_import, admin_user, tmp_path):
    apply(plan_for(make_workbook(tmp_path / "v.xlsx", goods=[("Thermometer", 9), ("Bottles", 8)])))

    rows = AuditLog.query.filter_by(action=ACTION_IMPORT).all()
    assert rows
    assert {row.user_id for row in rows} == {admin_user.id}


def test_with_nobody_to_record_it_under_nothing_is_written(before_import, tmp_path):
    plan = plan_for(make_workbook(tmp_path / "v.xlsx", goods=[("Thermometer", 9)]))
    items = Item.query.count()

    with audit.acting_as(None):
        with pytest.raises(audit.NoActor):
            valuations.apply_plan(plan, effective_from=clock.local_today())
    db.session.rollback()

    assert Item.query.count() == items


def test_the_decided_items_arrive_with_the_first_import(seeded_app, tmp_path):
    apply(plan_for(make_workbook(tmp_path / "v.xlsx")))

    diaper = Item.query.filter_by(name="Individual Diaper").one()
    assert (diaper.category, diaper.report_bucket, diaper.price_entry) == ("diapers", "diapers", "fixed")
    assert diaper.current_price() == Decimal("0.53")

    bus = Item.query.filter_by(name="Bus Pass").one()
    assert (bus.report_bucket, bus.price_entry) == ("transportation", "manual")
    assert bus.current_price() == Decimal("12.00")


def test_wipes_are_listed_with_the_diapers_but_stay_hygiene_money(seeded_app, tmp_path):
    apply(plan_for(make_workbook(tmp_path / "v.xlsx", goods=[("Individual Pack of Wipes", 3)])))
    wipes = Item.query.filter_by(name="Individual Pack of Wipes").one()
    assert (wipes.report_bucket, wipes.report_group) == ("hygiene", "diapers")


def test_bus_passes_and_formula_are_never_combined_goods():
    assert "transportation" not in COMBINED_GOODS_BUCKETS
    assert "formula" not in COMBINED_GOODS_BUCKETS


def test_the_command_is_a_dry_run_unless_told_otherwise(before_import, tmp_path):
    path = make_workbook(tmp_path / "v.xlsx", goods=[("Thermometer", 9)])
    result = before_import.test_cli_runner().invoke(args=["catalog", "import-valuations", str(path)])
    assert result.exit_code == 0, result.output
    assert "DRY RUN" in result.output
    assert Item.query.filter_by(name="Thermometer").count() == 0


def test_the_command_wont_apply_without_an_admin_to_record_it_under(before_import, staff_user, tmp_path):
    path = make_workbook(tmp_path / "v.xlsx", goods=[("Thermometer", 9)])
    runner = before_import.test_cli_runner()
    for extra in ([], ["--as", staff_user.email], ["--as", "nobody@example.org"]):
        result = runner.invoke(args=["catalog", "import-valuations", str(path), "--apply", *extra])
        assert result.exit_code != 0
        assert "active admin" in result.output
    assert Item.query.filter_by(name="Thermometer").count() == 0


def test_the_command_applies_as_the_named_admin(before_import, admin_user, tmp_path):
    path = make_workbook(tmp_path / "v.xlsx", goods=[("Thermometer", 9)])
    result = before_import.test_cli_runner().invoke(
        args=["catalog", "import-valuations", str(path), "--apply", "--as", admin_user.email])
    assert result.exit_code == 0, result.output
    assert "APPLIED" in result.output
    assert Item.query.filter_by(name="Thermometer").one().current_price() == Decimal("9.00")


@pytest.mark.skipif(not REAL_WORKBOOK.exists(), reason="the Valuations workbook isn't in git")
def test_every_row_of_the_real_workbook_is_covered_by_the_reviewed_tables(seeded_app):
    plan = plan_for(REAL_WORKBOOK)
    assert not plan.unmatched_rows, [r.where for r in plan.unmatched_rows]


def test_an_admin_can_set_and_clear_the_display_group(seeded_app):
    from app.services import catalog

    wipes = Item.query.filter_by(name="Individual Pack of Wipes").one()
    catalog.update_item(wipes, display_group="diapers")
    assert (wipes.report_bucket, wipes.report_group) == ("hygiene", "diapers")

    catalog.update_item(wipes, display_group="")
    assert wipes.display_group is None and wipes.report_group == "hygiene"

    with pytest.raises(catalog.CatalogError):
        catalog.update_item(wipes, display_group="snacks")


def test_the_item_page_offers_the_display_group(client):
    wipes = Item.query.filter_by(name="Individual Pack of Wipes").one()
    page = client.get(f"/admin/items/{wipes.id}").get_data(as_text=True)
    assert 'name="display_group"' in page
