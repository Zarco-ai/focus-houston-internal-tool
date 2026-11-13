"""Filling FOCUS's own monthly report Sheet (the client's request, 11 Nov 2026; D9-11).

The Sheet's template tab ("Sheet1") is FOCUS's monthly format, empty: a band
row, the headers, a rate row, 50 rows for sessions and a Totals row.
Filling a period copies that tab and writes the app's numbers into the
copy -- values, plus the date and dollar formats -- so the borders, colors,
bands and widths are FOCUS's own, exactly.

  * Rows: a row per approved session (date, service, its attendance), then
    a row per kind of item under it, labelled in Description: the monthly
    report's rows (reports.session_rows). The Totals row holds the app's
    sums, never a formula.
  * No names, anywhere (the client's decision): a session's Description is
    left blank. A batch note can hold a mother's name, so it is never
    written.
  * The template's headers are checked first. If FOCUS changes its columns,
    the fill is refused rather than writing numbers under the wrong heading.
  * One batchUpdate: Google applies all of it or none of it.
  * Filling a period again replaces its tab (the client's decision). Only the
    tab with exactly that period's name is ever deleted; never the template.
  * The service account is an Editor of this one Sheet and nothing else.
    It needs the read-write Sheets scope, so this module asks for it; the
    form sync keeps its read-only token.
"""

import datetime
import json
import re
import secrets
from decimal import Decimal
from urllib.parse import quote

from app.models.audit import ACTION_EXPORT
from app.services import audit, reports

SCOPES = ("https://www.googleapis.com/auth/spreadsheets",)
SHEETS_API = "https://sheets.googleapis.com/v4/spreadsheets"
REQUEST_TIMEOUT_SECONDS = 15
DEFAULT_TEMPLATE_TAB = "Sheet1"

# The template's layout, 1-based rows as Sheets shows them.
HEADER_ROW = 2
FIRST_DATA_ROW = 4
TEMPLATE_DATA_ROWS = 50          # rows 4-53; row 54 is Totals
DATE_FORMAT = {"type": "DATE", "pattern": "m/d/yyyy"}
MONEY_FORMAT = {"type": "CURRENCY", "pattern": "$#,##0.00"}

# Columns A to V: the header FOCUS's template has, and what the app puts
# under it (a reports.MONTHLY_COLUMNS key).
COLUMNS = (
    ("Date", "date"),
    ("Service", "service"),
    ("Description", "description"),
    ("# of Individuals Served", "individuals_served"),
    ("# of Children Served", "children_served"),
    ("# of Baby Formula Containers Distributed", "formula_containers"),
    ("Value ($) of Food Distributed (GIK)", "food_value"),
    ("# of Clothing / Hygiene Products / Household Goods Distributed", "goods_count"),
    ("# of Individual Diapers Distributed", "diapers_count"),
    ("Value ($) of Clothing / Hygiene Products / Household Goods Distributed", "goods_value"),
    ("Value ($) of Individual Diapers Distributed", "diapers_value"),
    ("Non-Food GIK $'s", "non_food_gik"),
    ("# of Unique Education Participants", "education_participants_unique"),
    ("# of Total Education Participants", "education_participants_total"),
    ("# of Total Education / Support Group Sessions Held", "education_sessions_held"),
    ("# of Individuals Served with Case Management", "case_management_served"),
    ("# of Total Volunteers", "volunteers_total"),
    ("# of Total Volunteer Hours (# of Event Hours x # of Volunteers)", "volunteer_hours"),
    ("# of Volunteer Opportunities Offered", "volunteer_opportunities"),
    ("# of Unique Clients Served", "unique_clients_event_sum"),
    ("# of Unique Volunteers", "unique_volunteers_event_sum"),
    ("# of Unique Parishes Represented", "unique_parishes_event_sum"),
)
KEYS = tuple(key for _, key in COLUMNS)
NUMBER_KEYS = KEYS[3:]
MONEY_KEYS = ("food_value", "goods_value", "diapers_value", "non_food_gik")
LAST_COLUMN = "V"

SHEETS_EPOCH = datetime.date(1899, 12, 30)


class ReportSheetError(Exception):
    """The fill can't run. The message says what to fix and holds no data."""


# --- The rows -----------------------------------------------------------------------------


def sheet_rows(start, end):
    """([row as a list of 22 values or None], totals {key: value}) for the
    period. Session rows have no Description; item rows have only their label."""
    columns = [c for c in reports.MONTHLY_COLUMNS if c.key in KEYS]
    rows, _ = reports.session_rows(start, end, columns)
    out, totals = [], {}
    for row in rows:
        cells = dict(row.cells)
        if row.kind == "batch":
            cells["description"] = None      # never the note: it can hold a name
        elif not any(cells.get(key) for key in NUMBER_KEYS):
            continue                         # e.g. bus passes: no column here
        for key in NUMBER_KEYS:
            if cells.get(key):
                totals[key] = totals.get(key, 0) + cells[key]
        out.append([cells.get(key) or None for key in KEYS])
    return out, totals


def tab_title(label):
    """The new tab's name: the Totals page's label for the period, without
    the characters Sheets refuses in a tab name."""
    return re.sub(r"[\[\]*?/\\:]", "-", label)[:100]


# --- The requests -------------------------------------------------------------------------


def _cell(value):
    if value is None or value == "":
        return {}
    if isinstance(value, datetime.date):
        return {"userEnteredValue": {"numberValue": (value - SHEETS_EPOCH).days}}
    if isinstance(value, (int, float, Decimal)):
        return {"userEnteredValue": {"numberValue": float(value)}}
    return {"userEnteredValue": {"stringValue": str(value)}}


def _format(sheet_id, column, first_row0, end_row0, number_format):
    return {"repeatCell": {
        "range": {"sheetId": sheet_id, "startRowIndex": first_row0, "endRowIndex": end_row0,
                  "startColumnIndex": column, "endColumnIndex": column + 1},
        "cell": {"userEnteredFormat": {"numberFormat": number_format}},
        "fields": "userEnteredFormat.numberFormat",
    }}


def build_requests(rows, totals, template_id, new_id, title, replace_id=None):
    """The one batchUpdate that makes the period's tab."""
    requests = []
    if replace_id is not None:
        requests.append({"deleteSheet": {"sheetId": replace_id}})
    requests.append({"duplicateSheet": {"sourceSheetId": template_id, "insertSheetIndex": 1,
                                        "newSheetId": new_id, "newSheetName": title}})
    first0 = FIRST_DATA_ROW - 1
    extra = max(0, len(rows) - TEMPLATE_DATA_ROWS)
    totals0 = first0 + TEMPLATE_DATA_ROWS + extra
    if extra:
        # New rows above Totals, formatted like the row before them.
        requests.append({"insertDimension": {
            "range": {"sheetId": new_id, "dimension": "ROWS",
                      "startIndex": first0 + TEMPLATE_DATA_ROWS, "endIndex": totals0},
            "inheritFromBefore": True}})
    if rows:
        requests.append({"updateCells": {
            "start": {"sheetId": new_id, "rowIndex": first0, "columnIndex": 0},
            "rows": [{"values": [_cell(v) for v in row]} for row in rows],
            "fields": "userEnteredValue"}})
        requests.append(_format(new_id, 0, first0, first0 + len(rows), DATE_FORMAT))
    requests.append({"updateCells": {
        "start": {"sheetId": new_id, "rowIndex": totals0, "columnIndex": 3},
        "rows": [{"values": [_cell(totals.get(key, 0)) for key in NUMBER_KEYS]}],
        "fields": "userEnteredValue"}})
    for key in MONEY_KEYS:
        requests.append(_format(new_id, KEYS.index(key), first0, totals0 + 1, MONEY_FORMAT))
    return requests


# --- The fill -----------------------------------------------------------------------------


def _tidy(text):
    return re.sub(r"[^a-z0-9]+", " ", str(text).lower()).strip()


def check_headers(headers):
    expected = [header for header, _ in COLUMNS]
    found = list(headers) + [""] * (len(expected) - len(headers))
    for column, (want, got) in enumerate(zip(expected, found)):
        if _tidy(want) != _tidy(got):
            letter = chr(ord("A") + column)
            raise ReportSheetError(
                f"The template's column {letter} is headed “{str(got)[:60]}”, not "
                f"“{want}”. Nothing was written. If FOCUS changed its format, the app "
                "needs the new columns first (docs/GOOGLE_REPORT_SHEET_SETUP.md).")


def fill(target, start, end, label):
    """Make (or remake) the period's tab. Returns the tab's link."""
    tabs = target.tabs()                      # {title: sheetId}
    by_lower = {title.lower(): title for title in tabs}
    template = by_lower.get(target.template_tab.lower())
    if template is None:
        raise ReportSheetError(f"The Sheet has no tab called “{target.template_tab}”. "
                               f"Its tabs are: {', '.join(tabs) or 'none'}.")
    check_headers(target.header_row(template))

    title = tab_title(label)
    if title.lower() == template.lower():
        raise ReportSheetError("That period's name is the template's name. Rename the template tab.")
    existing = by_lower.get(title.lower())
    new_id = _new_sheet_id(set(tabs.values()))
    rows, totals = sheet_rows(start, end)
    target.apply(build_requests(rows, totals, tabs[template], new_id, title,
                                replace_id=tabs[existing] if existing else None))

    audit.record_event(ACTION_EXPORT, "export", 0,
                       f"Filled the report Sheet: “{title}”, "
                       f"{start.isoformat()} to {end.isoformat()} ({len(rows)} rows)")
    return target.link(new_id)


def _new_sheet_id(taken):
    while True:
        candidate = secrets.randbelow(2**31 - 2) + 1
        if candidate not in taken:
            return candidate


# --- Google -------------------------------------------------------------------------------


class GoogleReportSheet:
    """The real Sheet, written with the service account's read-write token."""

    def __init__(self, sheet_id, template_tab, credentials_info):
        self.sheet_id = sheet_id
        self.template_tab = template_tab or DEFAULT_TEMPLATE_TAB
        self.credentials_info = credentials_info
        self._session = None

    @property
    def service_account_email(self):
        return self.credentials_info.get("client_email")

    def link(self, tab_id=None):
        url = f"https://docs.google.com/spreadsheets/d/{quote(self.sheet_id)}/edit"
        return f"{url}#gid={tab_id}" if tab_id is not None else url

    def _call(self, method, url, **kwargs):
        from google.auth.exceptions import GoogleAuthError
        from google.auth.transport.requests import AuthorizedSession
        from google.oauth2 import service_account
        import requests

        try:
            if self._session is None:
                credentials = service_account.Credentials.from_service_account_info(
                    self.credentials_info, scopes=SCOPES)
                self._session = AuthorizedSession(credentials)
            response = self._session.request(method, url, timeout=REQUEST_TIMEOUT_SECONDS, **kwargs)
        except (GoogleAuthError, ValueError) as error:
            raise ReportSheetError(f"Google refused the service account's key "
                                   f"({type(error).__name__}).")
        except requests.RequestException as error:
            raise ReportSheetError(f"Couldn't reach Google ({type(error).__name__}). Try again.")
        _raise_for(response, self.service_account_email)
        return response.json()

    def tabs(self):
        meta = self._call("GET", f"{SHEETS_API}/{quote(self.sheet_id)}",
                          params={"fields": "sheets.properties(sheetId,title)"})
        return {s["properties"]["title"]: s["properties"]["sheetId"]
                for s in meta.get("sheets") or []}

    def header_row(self, tab):
        a1 = f"'{tab.replace(chr(39), chr(39) * 2)}'!A{HEADER_ROW}:{LAST_COLUMN}{HEADER_ROW}"
        values = self._call("GET", f"{SHEETS_API}/{quote(self.sheet_id)}/values/{quote(a1)}")
        return (values.get("values") or [[]])[0]

    def apply(self, requests):
        self._call("POST", f"{SHEETS_API}/{quote(self.sheet_id)}:batchUpdate",
                   json={"requests": requests})


def _raise_for(response, account):
    if response.status_code == 200:
        return
    account = account or "the service account"
    if response.status_code == 403:
        raise ReportSheetError(f"Google said the report Sheet isn't shared with {account} as an "
                               "Editor. In the Sheet: Share → paste that address → Editor → Share.")
    if response.status_code == 404:
        raise ReportSheetError("Google can't find the report Sheet: REPORT_SHEET_ID on Render is "
                               "wrong. It's the long part of the Sheet's address between /d/ and /edit.")
    raise ReportSheetError(f"Google said {response.status_code}. Nothing was written.")


def configured_target(config):
    """The report Sheet from the app's settings, or None if not set up."""
    sheet_id = config.get("REPORT_SHEET_ID")
    raw_key = config.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not sheet_id or not raw_key:
        return None
    try:
        info = json.loads(raw_key)
    except ValueError:
        raise ReportSheetError("GOOGLE_SERVICE_ACCOUNT_JSON isn't valid JSON.")
    if not isinstance(info, dict) or info.get("type") != "service_account":
        raise ReportSheetError("GOOGLE_SERVICE_ACCOUNT_JSON isn't a service account key.")
    return GoogleReportSheet(sheet_id, config.get("REPORT_SHEET_TAB"), info)
