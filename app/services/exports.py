"""The CSV exports: raw tables, one row per thing.

  line items      totals.export_csv -- the prototype's columns, unchanged,
                  so anything reading today's file keeps working.
  retention       one row per month: S, E, N, the breakdown, separations,
                  average headcount and the four rates.
  program health  the Program Health metrics, a row per month and the
                  whole range (replaced the card-number files, 11 Nov 2026).

Every export is admin-only (app/auth.py). None of them carries free
text a mother or the program director typed about a person: no survey reasons, no
qualities, no notes -- the most sensitive data stays on the screen, behind
a sign-in.

CSV injection: a spreadsheet opens a cell starting with = + - or @ as a
formula. Every text cell that came from someone's typing goes through
safe(), which puts an apostrophe in front -- the standard defence (OWASP).
"""

import csv
import datetime
import io

from app.services import clock, metrics, retention, totals

FORMULA_STARTS = ("=", "+", "-", "@", "\t", "\r")


def safe(value):
    """A typed text value, made safe to open in a spreadsheet."""
    if value is None:
        return ""
    text = str(value)
    return "'" + text if text.startswith(FORMULA_STARTS) else text


def _csv(header, rows):
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(header)
    writer.writerows(rows)
    return output.getvalue()


def _blank(value):
    return "" if value is None else value


RETENTION_HEADER = ["month", "measured_to", "S", "E", "N", "retained", "came_back",
                    "new_still_active", "separations", "average_headcount",
                    "retention_focus_pct", "retained_pct", "engaged", "not_engaged",
                    "still_maturing", "engagement_pct", "turnover_pct", "window_days"]


def months_between(start, end):
    month = datetime.date(start.year, start.month, 1)
    while month <= end:
        yield month
        month = (month + datetime.timedelta(days=32)).replace(day=1)


def retention_csv(start, end):
    """One row per month touching the range. Rates blank when n/a."""
    people = retention.visits_by_person()
    window = retention.window_days()
    rows = []
    for month in months_between(start, end):
        first, last = totals.month_range(month.year, month.month)
        r = retention.measure(first, last, window=window, people=people)
        if r.future:
            continue
        rows.append([
            month.strftime("%Y-%m"), r.measured_to.isoformat(), r.S, r.E, r.N, r.R,
            r.returned, r.new_still_active, r.separations, r.average_headcount,
            _blank(r.retention_focus), _blank(r.retained_rate), r.engaged, r.not_engaged,
            r.maturing, _blank(r.engagement), _blank(r.turnover), window,
        ])
    return _csv(RETENTION_HEADER, rows)


def program_health_csv(start, end):
    """The Program Health metrics (the Totals page's last group): a row per
    month touching the range, then the whole range. Replaced the intake-times
    and form-responses files (the client's decision, 11 Nov 2026): figures
    only, no card numbers."""
    rows_metrics = [s for s in metrics.settings()
                    if s.enabled and s.metric.group == "PROGRAM_HEALTH"]
    header = ["period", "from", "to"] + [s.label for s in rows_metrics]
    today = clock.local_today()
    rows = []
    periods = []
    for month in months_between(start, end):
        first, last = totals.month_range(month.year, month.month)
        first, last = max(first, start), min(last, end)
        if first <= today:
            periods.append((month.strftime("%Y-%m"), first, last))
    periods.append(("whole range", start, end))
    for name, first, last in periods:
        period = metrics.Period(first, last)
        rows.append([name, first.isoformat(), last.isoformat()]
                    + [_blank(s.metric.compute(period)) for s in rows_metrics])
    return _csv(header, rows)
