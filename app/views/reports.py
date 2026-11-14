
import datetime

from flask import Blueprint, Response, abort, render_template, request

from app.models.snapshot import ReportSnapshot
from app.services import clock, exports, reports, snapshots, totals

reports_bp = Blueprint("reports", __name__)


def requested_month():
    raw = request.args.get("month", "")
    try:
        year, month = (int(part) for part in raw.split("-"))
        if 2000 <= year <= 2100 and 1 <= month <= 12:
            return year, month
    except ValueError:
        pass
    today = clock.local_today()
    return today.year, today.month


def requested_year():
    try:
        year = int(request.args.get("year", ""))
        if 2000 <= year <= 2100:
            return year
    except ValueError:
        pass
    return clock.local_today().year


def requested_range():
    today = clock.local_today()
    start, end = totals.month_range(today.year, today.month)
    try:
        start = datetime.date.fromisoformat(request.args.get("start", "")) if request.args.get("start") else start
        end = datetime.date.fromisoformat(request.args.get("end", "")) if request.args.get("end") else end
    except ValueError:
        abort(400)
    if start > end:
        abort(400)
    return start, end


def download(body, filename, mimetype):
    return Response(body, mimetype=mimetype,
                    headers={"Content-Disposition": f"attachment; filename={filename}"})


@reports_bp.route("/reports/monthly")
def monthly_page():
    year, month = requested_month()
    report = reports.monthly(year, month)
    return render_template("reports/monthly.html", report=report, as_text=reports.as_text,
                           filed=_comparisons("monthly", report.start))


@reports_bp.route("/reports/annual")
def annual_page():
    year = requested_year()
    report = reports.annual(year)
    return render_template("reports/annual.html", report=report, as_text=reports.as_text,
                           month_names=reports.MONTH_NAMES,
                           filed=_comparisons("annual", datetime.date(year, 1, 1)))


@reports_bp.route("/reports/retention.csv")
def retention_csv():
    start, end = requested_range()
    return download(exports.retention_csv(start, end),
                    f"focus-retention-{start.isoformat()}-to-{end.isoformat()}.csv", "text/csv")


@reports_bp.route("/reports/program-health.csv")
def program_health_csv():
    start, end = requested_range()
    return download(exports.program_health_csv(start, end),
                    f"focus-program-health-{start.isoformat()}-to-{end.isoformat()}.csv", "text/csv")


@reports_bp.route("/reports/filed")
def filed_list():
    return render_template("reports/filed.html",
                           comparisons=[snapshots.compare(s) for s in snapshots.all_snapshots()])


@reports_bp.route("/reports/filed/<int:snapshot_id>")
def filed_one(snapshot_id):
    from app.extensions import db

    snapshot = db.get_or_404(ReportSnapshot, snapshot_id)
    return render_template("reports/filed_one.html", comparison=snapshots.compare(snapshot))


def _comparisons(kind, start):
    return [snapshots.compare(s) for s in snapshots.for_period(kind, start)]
