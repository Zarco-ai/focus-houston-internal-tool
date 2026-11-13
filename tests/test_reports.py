"""Reports and exports (Phase 8).

The invariants first: the monthly report's totals row is the Totals page's
numbers; the year column is the year's own numbers; no cell anywhere is a
formula; a filed report never changes.
"""

import csv
import datetime
import io
import random
from decimal import Decimal

import pytest

from app.extensions import db
from app.models import AuditLog, ReportSnapshot, ServiceType
from app.models.participant import VISIT_GOOGLE_FORM, VISIT_INTAKE
from app.models.snapshot import SnapshotIsFrozen
from app.services import (
    batches,
    card_numbers,
    catalog,
    exports,
    metrics,
    participants,
    reports,
    snapshots,
    totals,
)

D = datetime.date


def item(name, category):
    return catalog.find_item(name, category)


def a_month(day=None):
    """A mixed month: diapers, wipes (listed with diapers, counted as
    hygiene), clothing, formula with a typed price, bus passes, a line with
    no price yet, an unsorted custom item, and attendance."""
    from app.services import clock
    day = day or clock.local_today().replace(day=1)
    service = ServiceType.query.filter_by(name="Diaper Distribution").one()
    first = batches.open_batch(date=day, service_type_id=service.id, note="Large donation")
    batches.set_line(first, item("Individual Diaper", "diapers"), quantity=240)
    batches.set_line(first, item("Individual Pack of Wipes", "baby_essentials"), quantity=6)
    batches.set_line(first, item("Coat", "clothing_adult"), quantity=2, condition="used")
    batches.set_line(first, item("Formula - Similac 7oz", "baby_essentials"), quantity=3, unit_price="11.50")
    batches.set_line(first, item("Bus Pass", "transportation"), quantity=2, unit_price="10.00")
    batches.set_attendance(first, {"individuals_served": 14, "children_served": 21,
                                   "volunteer_hours_tenths": 125, "meals_value": Decimal("40.00"),
                                   "unique_clients_event_count": 12})
    batches.commit_batch(first)

    second = batches.open_batch(date=day + datetime.timedelta(days=2), service_type_id=service.id)
    batches.set_line(second, item("Stroller", "baby_mom_items"), quantity=1)      # no price yet
    batches.add_custom_line(second, "Mystery onesie", quantity=2, unit_price=5)   # unsorted
    batches.set_attendance(second, {"individuals_served": 3})
    batches.commit_batch(second)
    db.session.commit()
    return day


# --- The monthly report -------------------------------------------------------------------


def test_the_monthly_totals_row_is_the_totals_page(seeded_app):
    """The monthly report's totals row equals the computed totals."""
    day = a_month()
    report = reports.monthly(day.year, day.month)
    computed = metrics.values(report.start, report.end)

    for column in report.columns:
        if column in reports.FIXED:
            continue
        assert report.totals.get(column.key, 0) == computed[column.key], column.key


def test_rows_follow_the_directors_sheet(seeded_app):
    day = a_month()
    report = reports.monthly(day.year, day.month)
    kinds = [(row.kind, row.cells.get("description")) for row in report.rows]

    assert kinds[0] == ("batch", "")       # never the note: it can hold a name
    assert report.rows[0].cells["service"] == "Diaper Distribution"
    # Wipes are listed with the diapers (decision 9) but counted as hygiene.
    diapers = next(row for row in report.rows if row.cells.get("description") == "Diapers")
    assert (diapers.cells["diapers_count"], diapers.cells["goods_count"]) == (240, 6)
    assert [k for k in kinds if k[0] == "item"][:3] == [
        ("item", "Diapers"), ("item", "Clothing"), ("item", "Formula")]


def test_the_rate_row_comes_from_todays_catalog(seeded_app):
    text = reports.rate_text()
    assert "Individual Diaper $0.53" in text
    assert "Individual Pack of Wipes $3.50" in text


def test_lines_with_no_value_or_no_category_are_noted(seeded_app):
    day = a_month()
    notes = " ".join(reports.monthly(day.year, day.month).notes)
    assert "Stroller" in notes and "no value yet" in notes
    assert "Mystery onesie is not in the catalog yet" in notes


def test_a_switched_off_metric_leaves_the_monthly_columns(seeded_app):
    metrics.update_setting("meals_value", enabled=False)
    report = reports.monthly(2026, 1)
    assert "meals_value" not in [c.key for c in report.columns]


# --- The annual report ------------------------------------------------------------------------


def test_the_year_is_the_sum_of_the_months_where_adding_makes_sense(seeded_app):
    for setting in metrics.settings():   # some start off (meals, medical...)
        metrics.update_setting(setting.key, enabled=True)
    a_month(D(2026, 1, 5))
    a_month(D(2026, 3, 9))
    report = reports.annual(2026)
    checked = 0
    for group in report.groups:
        for row in group.rows:
            if reports.is_additive(row):
                assert row.year == sum(v or 0 for v in row.months), row.key
                checked += 1
    assert checked > 20


def test_unique_clients_counts_a_mother_once_a_year_not_once_a_month(seeded_app, admin_user):
    """Finding F7: the 2025 annual sheet added the monthly unique counts."""
    code = card_numbers.generate(1, rng=random.Random(8))[0]
    participants.load_card_list(f"list_position,card_number,batch\n1,{code},1\n")
    mother = participants.create_participant(code, created_by_id=admin_user.id)
    participants.add_visit(mother, D(2026, 1, 10), VISIT_INTAKE, "a")
    participants.add_visit(mother, D(2026, 2, 10), VISIT_GOOGLE_FORM, "b")
    db.session.commit()

    row = next(r for g in reports.annual(2026).groups for r in g.rows
               if r.key == "unique_clients_distinct")
    assert row.months[:2] == [1, 1]
    assert row.year == 1


# --- The CSVs --------------------------------------------------------------------------------------


def read(text):
    return list(csv.reader(io.StringIO(text)))


def test_the_line_item_csv_keeps_its_columns(seeded_app):
    """Whatever reads today's export must keep working."""
    header = read(totals.export_csv(D(2026, 1, 1), D(2026, 1, 31)))[0]
    assert header == ["batch_date", "batch_id", "service_type", "item_name", "category",
                      "report_bucket", "quantity", "condition", "unit_price_at_time",
                      "used_multiplier_at_time", "computed_value", "needs_review"]


@pytest.mark.parametrize("typed", ["=1+1", "+cmd", "-2", "@SUM(A1)"])
def test_typed_text_cant_become_a_formula_in_a_csv(typed):
    assert exports.safe(typed) == "'" + typed
    assert exports.safe("Diapers") == "Diapers"


def test_a_custom_item_named_like_a_formula_is_neutralised(seeded_app):
    day = a_month()
    batch = batches.open_batch(date=day, service_type_id=ServiceType.query.first().id)
    batches.add_custom_line(batch, "=cmd|' /C calc'!A0", quantity=1, unit_price=1)
    batches.commit_batch(batch)
    rows = read(totals.export_csv(*totals.month_range(day.year, day.month)))
    assert any(row[3] == "'=cmd|' /C calc'!A0" for row in rows)


def test_the_retention_csv_has_a_row_per_month_and_blanks_for_n_a(seeded_app):
    rows = read(exports.retention_csv(D(2026, 1, 1), D(2026, 3, 31)))
    assert rows[0] == exports.RETENTION_HEADER
    assert [r[0] for r in rows[1:]] == ["2026-01", "2026-02", "2026-03"]
    assert rows[1][exports.RETENTION_HEADER.index("retention_focus_pct")] == ""


# --- Who may download what ----------------------------------------------------------------------------


def test_reports_and_exports_are_admin_only(staff_client, seeded_app):
    assert staff_client.get("/reports/monthly?month=2026-01").status_code == 403
    assert staff_client.get("/reports/retention.csv").status_code == 403
    assert staff_client.get("/reports/program-health.csv").status_code == 403
    assert staff_client.get("/reports/filed").status_code == 403
    assert staff_client.get("/totals/export.csv").status_code == 403


def test_the_xlsx_and_card_number_downloads_are_gone(client, seeded_app):
    for path in ("/reports/monthly.xlsx?month=2026-01", "/reports/annual.xlsx?year=2026",
                 "/admin/exports/intake-times.csv", "/admin/exports/form-responses.csv"):
        assert client.get(path).status_code == 404, path
    page = client.get("/totals").get_data(as_text=True)
    assert ".xlsx" not in page and "Program Health (.csv)" in page


def test_the_program_health_csv_has_only_program_health_figures(client, seeded_app):
    rows = read(client.get("/reports/program-health.csv?start=2026-01-01&end=2026-02-28")
                .get_data(as_text=True))
    labels = [s.label for s in metrics.settings()
              if s.enabled and s.metric.group == "PROGRAM_HEALTH"]
    assert rows[0] == ["period", "from", "to"] + labels
    assert [r[0] for r in rows[1:]] == ["2026-01", "2026-02", "whole range"]
    assert "card" not in " ".join(rows[0]).lower()


def test_the_monthly_report_never_shows_a_batch_note(client, seeded_app):
    day = a_month()
    page = client.get(f"/reports/monthly?month={day.strftime('%Y-%m')}").get_data(as_text=True)
    assert "Large donation" not in page


def test_the_report_pages_open(client, seeded_app):
    a_month()
    assert "Monthly report" in client.get("/reports/monthly").get_data(as_text=True)
    assert "Annual report" in client.get("/reports/annual").get_data(as_text=True)
    assert "Reports &amp; exports" in client.get("/totals").get_data(as_text=True)


# --- Filed snapshots ------------------------------------------------------------------------------------


def test_a_filed_report_doesnt_change_when_a_batch_is_corrected(seeded_app, admin_user):
    day = a_month()
    snapshot = snapshots.file("monthly", day.year, day.month, filed_by_id=admin_user.id,
                              app_version="abc1234")
    db.session.commit()
    filed_hash, filed_values = snapshot.payload_sha256, dict(snapshot.payload["values"])

    first = totals.committed_batches(*totals.month_range(day.year, day.month))[0]
    coat_line = next(line for line in first.lines if line.item and line.item.name == "Coat")
    batches.correct_line(coat_line, quantity=5)
    db.session.commit()
    db.session.expire_all()

    stored = db.session.get(ReportSnapshot, snapshot.id)
    assert stored.payload_sha256 == filed_hash and stored.payload["values"] == filed_values
    comparison = snapshots.compare(stored)
    assert comparison.changed
    labels = [label for label, _, _ in comparison.differences]
    assert "Non-Food GIK $'s" in labels


def test_an_unchanged_month_still_matches(seeded_app, admin_user):
    day = a_month()
    snapshot = snapshots.file("monthly", day.year, day.month, filed_by_id=admin_user.id)
    assert not snapshots.compare(snapshot).changed


def test_renaming_a_row_doesnt_make_a_filed_report_look_changed(seeded_app, admin_user):
    snapshot = snapshots.file("annual", 2026, filed_by_id=admin_user.id)
    metrics.update_setting("parish_donors", label="# of Parishes that gave")
    assert not snapshots.compare(snapshot).changed


def test_a_snapshot_cant_be_edited_or_deleted(seeded_app, admin_user):
    snapshot = snapshots.file("annual", 2026, filed_by_id=admin_user.id)
    db.session.commit()

    snapshot.payload_sha256 = "0" * 64
    with pytest.raises(SnapshotIsFrozen):
        db.session.flush()
    db.session.rollback()

    with pytest.raises(SnapshotIsFrozen):
        db.session.delete(db.session.get(ReportSnapshot, snapshot.id))
        db.session.flush()
    db.session.rollback()
    assert ReportSnapshot.query.count() == 1


def test_filing_twice_keeps_both(seeded_app, admin_user):
    snapshots.file("annual", 2026, filed_by_id=admin_user.id)
    snapshots.file("annual", 2026, filed_by_id=admin_user.id)
    assert len(snapshots.for_period("annual", D(2026, 1, 1))) == 2


def test_an_admin_files_from_the_page_and_staff_cant(client, staff_client, seeded_app):
    assert staff_client.post("/admin/reports/file", data={"kind": "annual", "year": "2026"}).status_code == 403

    reply = client.post("/admin/reports/file", data={"kind": "monthly", "year": "2026", "month": "1"},
                        follow_redirects=True)
    assert "Filed." in reply.get_data(as_text=True)
    snapshot = ReportSnapshot.query.one()
    assert (snapshot.kind, snapshot.period_start) == ("monthly", D(2026, 1, 1))
    assert AuditLog.query.filter_by(table_name="report_snapshot").count() == 1


def test_changed_since_filed_shows_on_the_report_page(client, seeded_app, admin_user):
    day = a_month()
    snapshots.file("monthly", day.year, day.month, filed_by_id=admin_user.id)
    first = totals.committed_batches(*totals.month_range(day.year, day.month))[0]
    batches.set_attendance(first, {"children_served": 99})
    db.session.commit()

    page = client.get(f"/reports/monthly?month={day.strftime('%Y-%m')}").get_data(as_text=True)
    assert "changed since filed" in page
