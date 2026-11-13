"""The Totals page and the line-item CSV export.

Every switched-on metric for a period, grouped like the FOCUS North America
template (app/services/metrics.py), then the detail by report bucket and the
batches behind it. The period is a month (the default), a whole year, or any
range; the page prints cleanly for filing.
"""

import datetime

from flask import (Blueprint, Response, current_app, flash, redirect, render_template, request,
                   session, url_for)

from app.extensions import db, limiter
from app.services import clock, metrics, report_sheet
from app.services import totals as totals_service

totals_bp = Blueprint("totals", __name__)

MONTH_NAMES = ("January", "February", "March", "April", "May", "June", "July",
               "August", "September", "October", "November", "December")


@totals_bp.route("/totals")
def index():
    # Program Health counts visits from the Google Form, so bring them in
    # first if the last sync is a few minutes old (D6-3: from Phase 7).
    from app.views.responses import sync_if_stale

    sync_if_stale()
    period = _period_from_request()
    start_date, end_date = period["start"], period["end"]
    summary = totals_service.summary(start_date, end_date)
    lines = totals_service.committed_lines(start_date, end_date)
    unsorted = next((b for b in summary["buckets"] if b["bucket"] == totals_service.UNSORTED), None)

    return render_template(
        "totals/totals.html",
        period=period,
        summary=summary,
        groups=metrics.report(start_date, end_date),
        start_date=start_date,
        end_date=end_date,
        unpriced_count=sum(1 for line in lines if line.needs_price),
        unsorted=unsorted,
        printed_on=clock.local_today(),
        report_sheet_ready=bool(current_app.config.get("REPORT_SHEET_ID")),
        report_sheet_link=session.pop("report_sheet_link", None),
    )


def _report_sheet_limit():
    return current_app.config["REPORT_SHEET_RATE_LIMIT"]


@totals_bp.route("/totals/report-sheet", methods=["POST"])
@limiter.limit(_report_sheet_limit)
def fill_report_sheet():
    """Admin-only (app/auth.py). The period comes in the query string, the
    same as the page's own."""
    period = _period_from_request()
    back = redirect(url_for("totals.index", **request.args) + "#reports")
    try:
        target = report_sheet.configured_target(current_app.config)
        if target is None:
            flash("The report Sheet isn't set up yet. See docs/GOOGLE_REPORT_SHEET_SETUP.md.", "error")
            return back
        link = report_sheet.fill(target, period["start"], period["end"], period["label"])
    except report_sheet.ReportSheetError as error:
        db.session.rollback()
        flash(str(error), "error")
        return back
    db.session.commit()
    session["report_sheet_link"] = link
    flash(f"Filled \u201c{report_sheet.tab_title(period['label'])}\u201d in the report Sheet.", "success")
    return back


@totals_bp.route("/totals/export.csv")
def export():
    period = _period_from_request()
    start_date, end_date = period["start"], period["end"]
    csv_text = totals_service.export_csv(start_date, end_date)

    filename = f"focus-gik-{start_date.isoformat()}-to-{end_date.isoformat()}.csv"
    return Response(
        csv_text,
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


def _period_from_request():
    """The period asked for: ?month=2026-10, ?year=2026, or ?start=&end=.

    Anything missing or unreadable falls back to the current month -- the
    page always shows something, and says what it is showing.
    """
    today = clock.local_today()
    args = request.args
    error = None

    month = _parse_month(args.get("month"))
    year = _parse_year(args.get("year"))

    if month:
        start, end = totals_service.month_range(*month)
        kind = "month"
    elif year:
        start, end = datetime.date(year, 1, 1), datetime.date(year, 12, 31)
        kind = "year"
    elif args.get("start") or args.get("end"):
        default_start, default_end = totals_service.month_range(today.year, today.month)
        start = _parse_date(args.get("start"), default_start)
        end = _parse_date(args.get("end"), default_end)
        kind = "custom"
        if start > end:
            error = "The start date was after the end date, so this shows the current month."
            start, end, kind = default_start, default_end, "month"
    else:
        start, end = totals_service.month_range(today.year, today.month)
        kind = "month"

    # A custom range that happens to be a whole month or year is shown as one.
    if kind == "custom":
        if metrics.whole_year(start, end):
            kind = "year"
        elif start.day == 1 and (start, end) == totals_service.month_range(start.year, start.month):
            kind = "month"

    return {
        "kind": kind,
        "start": start,
        "end": end,
        "label": _label(kind, start, end),
        "error": error,
        "month_value": f"{start.year:04d}-{start.month:02d}",
        "year_value": start.year,
        "previous": _step_url(kind, start, -1),
        "next": _step_url(kind, start, 1),
    }


def _label(kind, start, end):
    if kind == "month":
        return f"{MONTH_NAMES[start.month - 1]} {start.year}"
    if kind == "year":
        return str(start.year)
    if start.year == end.year:
        return f"{start.strftime('%b %-d')} – {end.strftime('%b %-d, %Y')}"
    return f"{start.strftime('%b %-d, %Y')} – {end.strftime('%b %-d, %Y')}"


def _step_url(kind, start, step):
    """The month or year before / after. None for a custom range."""
    if kind == "month":
        index = start.year * 12 + (start.month - 1) + step
        return url_for("totals.index", month=f"{index // 12:04d}-{index % 12 + 1:02d}")
    if kind == "year":
        return url_for("totals.index", year=start.year + step)
    return None


def _parse_month(raw):
    try:
        year, month = (int(part) for part in (raw or "").split("-"))
    except ValueError:
        return None
    if 2000 <= year <= 2100 and 1 <= month <= 12:
        return year, month
    return None


def _parse_year(raw):
    try:
        year = int(raw)
    except (TypeError, ValueError):
        return None
    return year if 2000 <= year <= 2100 else None


def _parse_date(raw, fallback):
    if not raw:
        return fallback
    try:
        return datetime.date.fromisoformat(raw)
    except ValueError:
        return fallback
