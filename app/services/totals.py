
import csv
import datetime
import io

from app.constants import (
    COMBINED_GOODS_BUCKETS,
    REPORT_BUCKETS,
    STATUS_COMMITTED,
    bucket_label,
)
from app.models import Batch, LineItem
from app.services.valuation import ZERO

UNSORTED = "unsorted"
UNSORTED_LABEL = "Not yet sorted"


def committed_batches(start_date, end_date):
    return (
        Batch.query.filter(
            Batch.status == STATUS_COMMITTED,
            Batch.date >= start_date,
            Batch.date <= end_date,
        )
        .order_by(Batch.date, Batch.id)
        .all()
    )


def committed_lines(start_date, end_date):
    return (
        LineItem.query.join(Batch, LineItem.batch_id == Batch.id)
        .filter(
            Batch.status == STATUS_COMMITTED,
            Batch.date >= start_date,
            Batch.date <= end_date,
            LineItem.quantity > 0,
        )
        .order_by(Batch.date, LineItem.id)
        .all()
    )


def bucket_key(line):
    return line.report_bucket or UNSORTED


def bucket_totals(start_date, end_date):
    totals = {}

    for line in committed_lines(start_date, end_date):
        key = bucket_key(line)
        entry = totals.setdefault(
            key,
            {
                "bucket": key,
                "label": UNSORTED_LABEL if key == UNSORTED else bucket_label(key),
                "quantity": 0,
                "value": ZERO,
            },
        )
        entry["quantity"] += line.quantity
        if line.computed_value is not None:
            entry["value"] += line.computed_value

    return totals


def summary(start_date, end_date):
    lines = committed_lines(start_date, end_date)
    buckets = bucket_totals(start_date, end_date)

    total_quantity = sum(line.quantity for line in lines)
    total_value = ZERO
    for line in lines:
        if line.computed_value is not None:
            total_value += line.computed_value

    ordered = [buckets[key] for key in REPORT_BUCKETS if key in buckets]
    ordered += [
        buckets[key] for key in sorted(buckets) if key not in REPORT_BUCKETS
    ]

    combined_quantity = sum(
        buckets[key]["quantity"] for key in COMBINED_GOODS_BUCKETS if key in buckets
    )
    combined_value = ZERO
    for key in COMBINED_GOODS_BUCKETS:
        if key in buckets:
            combined_value += buckets[key]["value"]

    return {
        "start_date": start_date,
        "end_date": end_date,
        "batches": committed_batches(start_date, end_date),
        "buckets": ordered,
        "total_quantity": total_quantity,
        "total_value": total_value,
        "combined_goods": {
            "label": "Clothing / Hygiene / Household Goods",
            "quantity": combined_quantity,
            "value": combined_value,
        },
    }


def month_range(year, month):
    start = datetime.date(year, month, 1)
    if month == 12:
        end = datetime.date(year, 12, 31)
    else:
        end = datetime.date(year, month + 1, 1) - datetime.timedelta(days=1)
    return start, end


def export_csv(start_date, end_date):
    from app.services.exports import safe

    output = io.StringIO()
    writer = csv.writer(output)

    writer.writerow(
        [
            "batch_date",
            "batch_id",
            "service_type",
            "item_name",
            "category",
            "report_bucket",
            "quantity",
            "condition",
            "unit_price_at_time",
            "used_multiplier_at_time",
            "computed_value",
            "needs_review",
        ]
    )

    for line in committed_lines(start_date, end_date):
        batch = line.batch
        writer.writerow(
            [
                batch.date.isoformat(),
                batch.id,
                safe(batch.service_type.name if batch.service_type else ""),
                safe(line.item.name if line.item else line.custom_name),
                line.item.category if line.item else "not in catalog",
                line.report_bucket or UNSORTED,
                line.quantity,
                line.condition,
                line.unit_price_at_time if line.unit_price_at_time is not None else "",
                line.used_multiplier_at_time
                if line.used_multiplier_at_time is not None
                else "",
                line.computed_value if line.computed_value is not None else "",
                "yes" if line.needs_review else "no",
            ]
        )

    return output.getvalue()
