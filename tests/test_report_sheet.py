"""Filling FOCUS's own monthly report Sheet from Totals (D9-11).

The rules: the numbers are the Totals page's; a batch note (which can hold a
mother's name) is never written; the template is never deleted or written
to; and a changed template is refused rather than filled wrongly.
"""

import datetime
import json

import pytest

from app.models import AuditLog, ServiceType
from app.services import batches, catalog, metrics, report_sheet
from app.services.report_sheet import COLUMNS, KEYS, NUMBER_KEYS

OCTOBER = (datetime.date(2026, 10, 1), datetime.date(2026, 10, 31))
TEMPLATE_ID, OLD_ID = 0, 777


class FakeTarget:
    template_tab = "Sheet1"

    def __init__(self):
        self.tab_ids = {"Sheet1": TEMPLATE_ID}
        self.headers = [header for header, _ in COLUMNS]
        self.applied = []

    def tabs(self):
        return dict(self.tab_ids)

    def header_row(self, tab):
        assert tab == "Sheet1"
        return self.headers

    def apply(self, requests):
        self.applied.append(requests)

    def link(self, tab_id=None):
        return f"https://docs.google.com/spreadsheets/d/x/edit#gid={tab_id}"


def item(name, category):
    return catalog.find_item(name, category)


def approved(day, lines, counts=None, note=None):
    batch = batches.open_batch(date=datetime.date(2026, 10, day),
                               service_type_id=ServiceType.query.first().id)
    for thing, quantity, price in lines:
        batches.set_line(batch, thing, quantity=quantity, unit_price=price)
    if counts:
        batches.set_attendance(batch, counts)
    if note:
        batch.note = note
    batches.commit_batch(batch)
    return batch


@pytest.fixture
def october(seeded_app):
    approved(6, [(item("Individual Diaper", "diapers"), 120, None),
                 (item("Individual Pack of Wipes", "baby_essentials"), 4, None),
                 (item("Coat", "clothing_adult"), 1, None)],
             counts={"individuals_served": 14, "children_served": 20,
                     "volunteers_total": 2, "volunteer_hours_tenths": 25},
             note="NAME-IN-THE-NOTE pickup")
    approved(9, [(item("Formula - Similac 7oz", "baby_essentials"), 3, "11.50"),
                 (item("Bus Pass", "transportation"), 2, "10.00")])
    approved(13, [], counts={"education_participants_total": 8, "education_sessions_held": 1})


def written(requests):
    return json.dumps(requests)


# --- The rows -----------------------------------------------------------------------------


def test_the_totals_row_is_the_totals_pages_numbers(october):
    _, totals = report_sheet.sheet_rows(*OCTOBER)
    values = metrics.values(*OCTOBER)
    for key in NUMBER_KEYS:
        assert totals.get(key, 0) == values[key], key


def test_a_session_row_then_its_items_with_no_description_on_the_session(october):
    rows, _ = report_sheet.sheet_rows(*OCTOBER)
    first = rows[0]
    assert first[0] == datetime.date(2026, 10, 6)
    assert first[KEYS.index("description")] is None
    assert first[KEYS.index("individuals_served")] == 14
    labels = [row[KEYS.index("description")] for row in rows if row[0] is None]
    assert "Diapers" in labels and "Clothing" in labels


def test_bus_passes_get_no_row_of_their_own(october):
    """The Sheet has no bus-pass column; the session row stays."""
    rows, _ = report_sheet.sheet_rows(*OCTOBER)
    labels = [row[KEYS.index("description")] for row in rows]
    assert "Bus passes" not in labels
    assert datetime.date(2026, 10, 9) in [row[0] for row in rows]


def test_a_batch_note_is_never_written(october):
    target = FakeTarget()
    report_sheet.fill(target, *OCTOBER, "October 2026")
    assert "NAME-IN-THE-NOTE" not in written(target.applied)


# --- The requests -------------------------------------------------------------------------


def test_the_template_is_copied_and_never_written(october):
    target = FakeTarget()
    link = report_sheet.fill(target, *OCTOBER, "October 2026")

    (requests,) = target.applied
    duplicate = requests[0]["duplicateSheet"]
    assert duplicate["sourceSheetId"] == TEMPLATE_ID
    assert duplicate["newSheetName"] == "October 2026"
    new_id = duplicate["newSheetId"]
    for request in requests[1:]:
        body = next(iter(request.values()))
        sheet_id = body.get("start", body.get("range", {})).get("sheetId")
        assert sheet_id == new_id
    assert link.endswith(f"#gid={new_id}")


def test_rows_start_at_row_4_and_totals_land_on_row_54(october):
    target = FakeTarget()
    report_sheet.fill(target, *OCTOBER, "October 2026")
    starts = [r["updateCells"]["start"] for r in target.applied[0] if "updateCells" in r]
    assert (starts[0]["rowIndex"], starts[0]["columnIndex"]) == (3, 0)
    assert (starts[1]["rowIndex"], starts[1]["columnIndex"]) == (53, 3)


def test_more_than_50_rows_insert_rows_above_totals():
    rows = [[datetime.date(2026, 10, 1)] + [None] * 21 for _ in range(53)]
    requests = report_sheet.build_requests(rows, {}, TEMPLATE_ID, 5, "October 2026")
    insert = next(r["insertDimension"] for r in requests if "insertDimension" in r)
    assert (insert["range"]["startIndex"], insert["range"]["endIndex"]) == (53, 56)
    assert insert["inheritFromBefore"] is True
    totals = [r["updateCells"]["start"] for r in requests if "updateCells" in r][-1]
    assert totals["rowIndex"] == 56


def test_filling_a_period_again_replaces_only_its_own_tab(october):
    target = FakeTarget()
    target.tab_ids["October 2026"] = OLD_ID
    target.tab_ids["September 2026"] = 888
    report_sheet.fill(target, *OCTOBER, "October 2026")
    deletes = [r["deleteSheet"]["sheetId"] for r in target.applied[0] if "deleteSheet" in r]
    assert deletes == [OLD_ID]


def test_a_changed_template_is_refused_and_nothing_written(october):
    target = FakeTarget()
    target.headers[5] = "# of Car Seats"
    with pytest.raises(report_sheet.ReportSheetError, match="column F"):
        report_sheet.fill(target, *OCTOBER, "October 2026")
    assert target.applied == []


def test_a_missing_template_tab_is_refused(october):
    target = FakeTarget()
    target.tab_ids = {"Other": 3}
    with pytest.raises(report_sheet.ReportSheetError, match="no tab called"):
        report_sheet.fill(target, *OCTOBER, "October 2026")


def test_a_tab_name_has_no_characters_sheets_refuses():
    assert report_sheet.tab_title("Oct 1 – Oct 15, 2026") == "Oct 1 – Oct 15, 2026"
    assert report_sheet.tab_title("a/b:c") == "a-b-c"


# --- The button ---------------------------------------------------------------------------


@pytest.fixture
def target(monkeypatch, seeded_app, october):
    fake = FakeTarget()
    monkeypatch.setitem(seeded_app.config, "REPORT_SHEET_ID", "test-sheet")
    monkeypatch.setattr(report_sheet, "configured_target", lambda config: fake)
    return fake


def test_an_admin_fills_the_sheet_and_it_is_audited(client, target, admin_user):
    response = client.post("/totals/report-sheet?month=2026-10", follow_redirects=True)
    text = response.get_data(as_text=True)
    assert "Filled “October 2026” in the report Sheet." in text
    assert "Open the new tab" in text
    entry = AuditLog.query.filter_by(action="export").one()
    assert entry.user_id == admin_user.id and "October 2026" in entry.description


def test_staff_cannot_fill_the_sheet(staff_client, target):
    assert staff_client.post("/totals/report-sheet?month=2026-10").status_code == 403
    assert target.applied == []


def test_without_setup_the_page_says_so(client, seeded_app):
    text = client.get("/totals").get_data(as_text=True)
    assert "report Sheet isn't set up yet" in text


def test_a_google_error_is_shown_and_nothing_is_logged(client, target):
    def refuse(requests):
        raise report_sheet.ReportSheetError("Google said 500. Nothing was written.")
    target.apply = refuse
    text = client.post("/totals/report-sheet?month=2026-10", follow_redirects=True).get_data(as_text=True)
    assert "Google said 500" in text
    assert AuditLog.query.filter_by(action="export").count() == 0


# --- Google -------------------------------------------------------------------------------


def test_the_real_sheet_asks_for_write_access_and_sends_one_batch_update(monkeypatch):
    calls = []

    class FakeCredentials:
        @staticmethod
        def from_service_account_info(info, scopes):
            calls.append(("scopes", scopes))
            return object()

    class FakeResponse:
        status_code = 200

        def __init__(self, payload):
            self.payload = payload

        def json(self):
            return self.payload

    class FakeSession:
        def __init__(self, credentials):
            pass

        def request(self, method, url, timeout, **kwargs):
            calls.append((method, url))
            if url.endswith(":batchUpdate"):
                return FakeResponse({})
            if "/values/" in url:
                return FakeResponse({"values": [[h for h, _ in COLUMNS]]})
            return FakeResponse({"sheets": [{"properties": {"title": "Sheet1", "sheetId": 0}}]})

    import google.auth.transport.requests as transport
    import google.oauth2.service_account as service_account
    monkeypatch.setattr(service_account, "Credentials", FakeCredentials)
    monkeypatch.setattr(transport, "AuthorizedSession", FakeSession)

    sheet = report_sheet.GoogleReportSheet("sheet-id", None, {"client_email": "x@y"})
    assert sheet.tabs() == {"Sheet1": 0}
    assert sheet.header_row("Sheet1")[0] == "Date"
    sheet.apply([{"duplicateSheet": {}}])

    assert calls[0] == ("scopes", ("https://www.googleapis.com/auth/spreadsheets",))
    assert ("POST", f"{report_sheet.SHEETS_API}/sheet-id:batchUpdate") in calls
    assert "%27Sheet1%27%21A2%3AV2" in calls[2][1]


def test_configured_target_needs_the_sheet_and_the_key():
    assert report_sheet.configured_target({"REPORT_SHEET_ID": None,
                                           "GOOGLE_SERVICE_ACCOUNT_JSON": "{}"}) is None
    with pytest.raises(report_sheet.ReportSheetError):
        report_sheet.configured_target({"REPORT_SHEET_ID": "abc",
                                        "GOOGLE_SERVICE_ACCOUNT_JSON": "{\"type\": \"user\"}"})


def test_a_name_typed_like_a_formula_is_written_as_text(october):
    """A formula in a report is how the 2026 sheet broke, and how a
    malicious cell runs. An admin can name a service type anything."""
    ServiceType.query.first().name = '=HYPERLINK("http://example.invalid","click")'
    target = FakeTarget()
    report_sheet.fill(target, *OCTOBER, "October 2026")
    text = written(target.applied)
    assert '"stringValue": "=HYPERLINK' in text
    assert "formulaValue" not in text
