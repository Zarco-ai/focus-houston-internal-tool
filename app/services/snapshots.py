
import datetime
import hashlib
import json
from dataclasses import dataclass

from app.extensions import db
from app.models.audit import ACTION_CREATE
from app.models.snapshot import KIND_ANNUAL, KIND_MONTHLY, ReportSnapshot
from app.services import audit, metrics, reports


def _monthly_payload(year, month):
    report = reports.monthly(year, month)
    values = metrics.values(report.start, report.end)
    batches = []
    for row in report.rows:
        cells = {}
        for column in report.columns:
            value = row.cells.get(column.key)
            if value is None:
                continue
            if column.key == "date":
                cells["date"] = value.isoformat()
            elif column.key in ("service", "description"):
                cells[column.key] = value
            else:
                cells[column.key] = reports.as_text(value, column.unit)
        batches.append([row.kind, cells])
    return report.start, report.end, {
        "values": {m.key: reports.as_text(values[m.key], m.unit) for m in metrics.METRICS},
        "batches": batches,
        "labels": {s.key: s.label for s in metrics.settings()},
    }


def _annual_payload(year):
    report = reports.annual(year)
    values, labels = {}, {}
    for group in report.groups:
        for row in group.rows:
            for index, value in enumerate(row.months):
                values[f"{row.key}@{index + 1:02d}"] = reports.as_text(value, row.unit)
            values[f"{row.key}@year"] = reports.as_text(row.year, row.unit)
            labels[row.key] = row.label
    return datetime.date(year, 1, 1), datetime.date(year, 12, 31), {
        "values": values, "batches": [], "labels": labels,
    }


def build(kind, year, month=None):
    if kind == KIND_MONTHLY:
        return _monthly_payload(year, month)
    if kind == KIND_ANNUAL:
        return _annual_payload(year)
    raise ValueError(f"Unknown report kind: {kind!r}")


def fingerprint(payload):
    hashed = {"values": payload["values"], "batches": payload["batches"]}
    text = json.dumps(hashed, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def file(kind, year, month=None, filed_by_id=None, app_version="local"):
    start, end, payload = build(kind, year, month)
    snapshot = ReportSnapshot(kind=kind, period_start=start, period_end=end,
                              filed_by_id=filed_by_id, payload=payload,
                              payload_sha256=fingerprint(payload),
                              app_version=(app_version or "local")[:40])
    db.session.add(snapshot)
    db.session.flush()
    audit.record_event(ACTION_CREATE, "report_snapshot", snapshot.id,
                       f"Filed the {kind} report for {start.isoformat()} to {end.isoformat()}")
    return snapshot


def for_period(kind, start):
    return (ReportSnapshot.query.filter_by(kind=kind, period_start=start)
            .order_by(ReportSnapshot.filed_at.desc(), ReportSnapshot.id.desc()).all())


def all_snapshots(limit=100):
    return (ReportSnapshot.query.order_by(ReportSnapshot.period_start.desc(),
                                          ReportSnapshot.filed_at.desc()).limit(limit).all())


@dataclass
class Comparison:
    snapshot: ReportSnapshot
    changed: bool
    differences: list


def compare(snapshot):
    if snapshot.kind == KIND_MONTHLY:
        _, _, live = build(KIND_MONTHLY, snapshot.period_start.year, snapshot.period_start.month)
    else:
        _, _, live = build(KIND_ANNUAL, snapshot.period_start.year)
    if fingerprint(live) == snapshot.payload_sha256:
        return Comparison(snapshot, False, [])

    filed_values, live_values = snapshot.payload["values"], live["values"]
    labels = {**live.get("labels", {}), **snapshot.payload.get("labels", {})}
    differences = []
    for key in sorted(set(filed_values) | set(live_values)):
        before, after = filed_values.get(key, "—"), live_values.get(key, "—")
        if before != after:
            base, _, part = key.partition("@")
            label = labels.get(base, base)
            if part:
                label += f" ({'year' if part == 'year' else reports.MONTH_NAMES[int(part) - 1]})"
            differences.append((label, before, after))
    if not differences:
        differences.append(("The batch rows (a line moved, or a description changed)", "", ""))
    return Comparison(snapshot, True, differences)
