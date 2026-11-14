
import datetime
import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass, field
from urllib.parse import quote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import sqlalchemy as sa

from app.extensions import db
from app.models import FormResponse, SyncRun
from app.models.form import LABEL_MAX, RAW_ID_MAX, TEXT_MAX
from app.services import card_numbers, clock, form_review

SCOPES = ("https://www.googleapis.com/auth/spreadsheets.readonly",)
SHEETS_API = "https://sheets.googleapis.com/v4/spreadsheets"
REQUEST_TIMEOUT_SECONDS = 15
DEFAULT_TAB = "form responses 1"

ADVISORY_LOCK_KEY = 6_242_026

DEFAULT_STALE_MINUTES = 5

MAX_NEW_ROWS_PER_SYNC = 300

SHEETS_EPOCH = datetime.datetime(1899, 12, 30)


class SyncError(Exception):
    pass


def _tidy_header(text):
    text = unicodedata.normalize("NFKC", str(text or "")).lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


@dataclass(frozen=True)
class Column:
    field: str
    titles: tuple
    prefixes: tuple = ()
    required: bool = False

    def matches(self, tidied):
        return tidied in self.titles or any(tidied.startswith(p) for p in self.prefixes)


COLUMNS = (
    Column("timestamp", ("timestamp",), required=True),
    Column("raw_id", ("what s your id", "whats your id", "what is your id", "your id",
                      "id", "card number", "your card number", "id number"),
           required=True),
    Column("returning", ("are you a returning mother", "returning mother")),
    Column("event", ("what event are you here for", "event")),
    Column("reason", ("what is your reason for joining us today",
                      "what s your reason for joining us today", "reason for joining us today")),
    Column("score", (), prefixes=("how likely are you to come back",)),
    Column("score_reason", ("why", "why did you choose that number", "why that number"),
           prefixes=("why ",)),
)


def find_columns(headers):
    found, unread = {}, []
    for index, header in enumerate(headers):
        tidied = _tidy_header(header)
        if not tidied:
            continue
        column = next((c for c in COLUMNS if c.field not in found and c.matches(tidied)), None)
        if column is None:
            unread.append(str(header).strip()[:60])
        else:
            found[column.field] = index

    missing = [c for c in COLUMNS if c.field not in found]
    required = [c.field for c in missing if c.required]
    if required:
        raise SyncError(
            "The response Sheet has no "
            + " or ".join("Timestamp" if f == "timestamp" else "ID question" for f in required)
            + " column. Check the form's questions against docs/GOOGLE_FORM_SETUP.md."
        )

    notes = []
    if missing:
        notes.append("Not found, left blank: " + ", ".join(c.field.replace("_", " ") for c in missing))
    if unread:
        notes.append("Not read: " + ", ".join(unread))
    return found, ("; ".join(notes)[:500] or None)


@dataclass
class ParsedRow:
    submitted_local: datetime.datetime
    submitted_at: datetime.datetime
    raw_id_text: str
    returning: object
    event: str
    reason: str
    score: object
    score_reason: str

    @property
    def source_key(self):
        stamp = self.submitted_local.replace(microsecond=0).isoformat()
        text = f"{stamp}|{card_numbers.normalize(self.raw_id_text)}"
        return hashlib.sha256(text.encode("utf-8")).hexdigest()


class RowError(Exception):
    pass


def _cell(row, columns, name):
    index = columns.get(name)
    if index is None or index >= len(row):
        return None
    value = row[index]
    return None if value == "" else value


def _text(value, limit):
    if value is None:
        return None
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    text = " ".join(str(value).split())
    return text[:limit] or None


def _timestamp(value, zone):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RowError("Timestamp isn't a date and time")
    local = SHEETS_EPOCH + datetime.timedelta(days=float(value))
    local = (local + datetime.timedelta(microseconds=500_000)).replace(microsecond=0)
    return local, local.replace(tzinfo=zone).astimezone(datetime.timezone.utc)


def _returning(value):
    if isinstance(value, bool):
        return value
    text = _tidy_header(value)
    if text in ("yes", "y", "si"):
        return True
    if text in ("no", "n"):
        return False
    return None


def _score(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(str(value).strip())
    except ValueError:
        return None
    return int(number) if number.is_integer() and 1 <= number <= 10 else None


def parse_row(row, columns, zone):
    local, utc = _timestamp(_cell(row, columns, "timestamp"), zone)
    return ParsedRow(
        submitted_local=local,
        submitted_at=utc,
        raw_id_text=_text(_cell(row, columns, "raw_id"), RAW_ID_MAX) or "",
        returning=_returning(_cell(row, columns, "returning")),
        event=_text(_cell(row, columns, "event"), LABEL_MAX),
        reason=_text(_cell(row, columns, "reason"), TEXT_MAX),
        score=_score(_cell(row, columns, "score")),
        score_reason=_text(_cell(row, columns, "score_reason"), TEXT_MAX),
    )


@dataclass
class SheetData:
    headers: list
    rows: list = field(default_factory=list)
    timezone: str = ""


class GoogleSheetSource:

    def __init__(self, sheet_id, tab, credentials_info):
        self.sheet_id = sheet_id
        self.tab = tab
        self.credentials_info = credentials_info

    @property
    def service_account_email(self):
        return self.credentials_info.get("client_email")

    def fetch(self):
        from google.auth.exceptions import GoogleAuthError
        from google.auth.transport.requests import AuthorizedSession
        from google.oauth2 import service_account
        import requests

        try:
            credentials = service_account.Credentials.from_service_account_info(
                self.credentials_info, scopes=SCOPES)
            session = AuthorizedSession(credentials)
            meta = session.get(f"{SHEETS_API}/{quote(self.sheet_id)}",
                               params={"fields": "properties.timeZone,sheets.properties.title"},
                               timeout=REQUEST_TIMEOUT_SECONDS)
            _raise_for(meta, self.service_account_email)
            titles = [s.get("properties", {}).get("title", "") for s in meta.json().get("sheets") or []]
            tab = choose_tab(titles, self.tab)
            values = session.get(
                f"{SHEETS_API}/{quote(self.sheet_id)}/values/{quote(tab)}",
                params={"valueRenderOption": "UNFORMATTED_VALUE",
                        "dateTimeRenderOption": "SERIAL_NUMBER",
                        "majorDimension": "ROWS"},
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
            _raise_for(values, self.service_account_email)
        except (GoogleAuthError, ValueError) as error:
            raise SyncError(f"Google refused the service account's key ({type(error).__name__}). "
                            "See docs/GOOGLE_FORM_SETUP.md.")
        except requests.RequestException as error:
            raise SyncError(f"Couldn't reach Google ({type(error).__name__}). It will retry.")

        grid = values.json().get("values") or []
        return SheetData(
            headers=list(grid[0]) if grid else [],
            rows=[list(row) for row in grid[1:]],
            timezone=(meta.json().get("properties") or {}).get("timeZone", ""),
        )


def choose_tab(titles, wanted=None):
    by_lower = {t.lower(): t for t in titles}
    if wanted:
        if wanted.lower() in by_lower:
            return by_lower[wanted.lower()]
        raise SyncError(f"The Sheet has no tab called \u201c{wanted}\u201d. Its tabs are: "
                        f"{', '.join(titles) or 'none'}. Fix FORM_SHEET_TAB on Render, or delete it.")
    if DEFAULT_TAB in by_lower:
        return by_lower[DEFAULT_TAB]
    if len(titles) == 1:
        return titles[0]
    raise SyncError(f"The Sheet has several tabs ({', '.join(titles)}). Set FORM_SHEET_TAB on "
                    "Render to the one the form writes to.")


def _google_reason(response):
    try:
        error = response.json().get("error") or {}
    except ValueError:
        return ""
    for detail in error.get("details") or []:
        if detail.get("reason"):
            return detail["reason"]
    return error.get("status", "")


def _raise_for(response, account):
    if response.status_code == 200:
        return
    account = account or "the service account"
    reason = _google_reason(response)
    if reason == "SERVICE_DISABLED":
        raise SyncError("The Google Sheets API isn't turned on in the Google Cloud project. "
                        "Cloud console → APIs & Services → Library → Google Sheets API → Enable, "
                        "wait a few minutes, then Sync now. (Setup guide, step 2.1.)")
    if response.status_code == 403:
        raise SyncError(f"Google said the Sheet isn't shared with {account}. In the Sheet: Share → "
                        "paste that address → Viewer → Share. (Setup guide, step 3.)")
    if response.status_code == 404:
        raise SyncError("Google can't find that Sheet: FORM_SHEET_ID on Render is wrong. It's the "
                        "long part of the Sheet's address between /d/ and /edit.")
    raise SyncError(f"Google said {response.status_code}. It will retry.")


def configured_source(config):
    sheet_id = config.get("FORM_SHEET_ID")
    raw_key = config.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not sheet_id or not raw_key:
        return None
    try:
        info = json.loads(raw_key)
    except ValueError:
        raise SyncError("GOOGLE_SERVICE_ACCOUNT_JSON isn't valid JSON. Paste the whole key file.")
    if not isinstance(info, dict) or info.get("type") != "service_account":
        raise SyncError("GOOGLE_SERVICE_ACCOUNT_JSON isn't a service account key.")
    return GoogleSheetSource(sheet_id, config.get("FORM_SHEET_TAB"), info)


def last_run():
    return SyncRun.query.order_by(SyncRun.started_at.desc(), SyncRun.id.desc()).first()


def last_success():
    return (SyncRun.query.filter(SyncRun.finished_at.isnot(None), SyncRun.error.is_(None))
            .order_by(SyncRun.started_at.desc(), SyncRun.id.desc()).first())


def recent_runs(limit=10):
    return SyncRun.query.order_by(SyncRun.started_at.desc(), SyncRun.id.desc()).limit(limit).all()


def is_stale(minutes=DEFAULT_STALE_MINUTES, now=None):
    run = last_run()
    now = now or clock.now_utc()
    if run is not None and run.rows_remaining:
        return True
    return run is None or now - run.started_at > datetime.timedelta(minutes=minutes)


def _take_lock():
    return db.session.execute(sa.select(sa.func.pg_try_advisory_xact_lock(ADVISORY_LOCK_KEY))).scalar()


def run(source, triggered_by_id=None):
    if not _take_lock():
        db.session.rollback()
        return None

    sync = SyncRun(started_at=clock.now_utc(), triggered_by_id=triggered_by_id)
    db.session.add(sync)
    db.session.flush()

    try:
        data = source.fetch()
        if not data.headers:
            raise SyncError("The response Sheet is empty: no header row.")
        try:
            zone = ZoneInfo(data.timezone)
        except (ZoneInfoNotFoundError, ValueError):
            raise SyncError("Couldn't read the Sheet's time zone (File → Settings in the Sheet).")
        columns, sync.columns_note = find_columns(data.headers)
        _import_rows(sync, data.rows, columns, zone)
    except SyncError as error:
        sync.error = str(error)[:500]

    form_review.match_waiting_responses()

    sync.finished_at = clock.now_utc()
    db.session.commit()
    return sync


def _import_rows(sync, rows, columns, zone):
    known = {key for (key,) in db.session.query(FormResponse.source_key)}
    unreadable = []

    for number, row in enumerate(rows, start=2):
        if not any(cell not in ("", None) for cell in row):
            continue
        sync.rows_seen += 1
        try:
            parsed = parse_row(row, columns, zone)
        except RowError as error:
            unreadable.append(f"row {number} ({error})")
            continue
        if parsed.source_key in known:
            continue
        if sync.rows_imported >= MAX_NEW_ROWS_PER_SYNC:
            sync.rows_remaining += 1
            continue

        try:
            with db.session.begin_nested():
                response = FormResponse(
                    source_key=parsed.source_key,
                    submitted_at=parsed.submitted_at,
                    submitted_local_date=parsed.submitted_local.date(),
                    raw_id_text=parsed.raw_id_text or None,
                    returning=parsed.returning,
                    raw_event_label=parsed.event,
                    reason=parsed.reason,
                    score=parsed.score,
                    score_reason=parsed.score_reason,
                    imported_in_sync_run_id=sync.id,
                )
                db.session.add(response)
                db.session.flush()
                form_review.match_on_import(response)
        except sa.exc.SQLAlchemyError as error:
            unreadable.append(f"row {number} (couldn't be saved: {type(error).__name__})")
            continue

        known.add(parsed.source_key)
        sync.rows_imported += 1
        if form_review.needs_review(response):
            sync.rows_flagged += 1

    if unreadable:
        shown = ", ".join(unreadable[:5]) + (" …" if len(unreadable) > 5 else "")
        sync.error = (f"{len(unreadable)} row(s) couldn't be imported (fix them in the "
                      f"Sheet and they come in on the next sync): {shown}")[:500]
