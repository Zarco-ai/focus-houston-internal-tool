
import datetime
from dataclasses import dataclass, field
from decimal import Decimal

from app.constants import (
    BUCKET_CLOTHING,
    BUCKET_DIAPERS,
    BUCKET_FOOD,
    BUCKET_FORMULA,
    BUCKET_HOUSEHOLD,
    BUCKET_HYGIENE,
    BUCKET_TRANSPORTATION,
)
from app.models import Item
from app.services import attendance, clock, metrics, totals
from app.services.valuation import ZERO

MONTH_NAMES = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
               "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")

NOTES_TEXT = ("Anything outside the categories above (an Amazon wish-list item, a "
              "one-off donation): add it here, and enter it in the app so it is "
              "counted next time.")


@dataclass(frozen=True)
class Column:
    key: str
    group: str = ""
    unit: str = metrics.UNIT_COUNT
    attendance: str = ""


FIXED = (Column("date"), Column("service"), Column("description"))

MONTHLY_COLUMNS = FIXED + (
    Column("individuals_served", "SERVED", attendance="individuals_served"),
    Column("children_served", "SERVED", attendance="children_served"),
    Column("formula_containers", "FOOD"),
    Column("food_value", "FOOD", metrics.UNIT_MONEY),
    Column("meals_value", "FOOD", metrics.UNIT_MONEY, attendance="meals_value"),
    Column("temp_employment_placed", "OCCUPATION", attendance="temp_employment_placed"),
    Column("goods_count", "CLOTHING"),
    Column("diapers_count", "CLOTHING"),
    Column("goods_value", "CLOTHING", metrics.UNIT_MONEY),
    Column("diapers_value", "CLOTHING", metrics.UNIT_MONEY),
    Column("non_food_gik", "CLOTHING", metrics.UNIT_MONEY),
    Column("education_participants_unique", "UNDERSTANDING",
           attendance="education_participants_unique"),
    Column("education_participants_total", "UNDERSTANDING",
           attendance="education_participants_total"),
    Column("education_sessions_held", "UNDERSTANDING", attendance="education_sessions_held"),
    Column("medical_services_served", "UNDERSTANDING", attendance="medical_services_served"),
    Column("case_management_served", "UNDERSTANDING", attendance="case_management_served"),
    Column("bus_passes_count", "SHELTER"),
    Column("transportation_value", "SHELTER", metrics.UNIT_MONEY),
    Column("volunteers_total", "VOLUNTEERS", attendance="volunteers_total"),
    Column("volunteer_hours", "VOLUNTEERS", metrics.UNIT_HOURS, attendance="volunteer_hours_tenths"),
    Column("volunteer_opportunities", "VOLUNTEERS", attendance="volunteer_opportunities"),
    Column("unique_clients_event_sum", "UNIQUE", attendance="unique_clients_event_count"),
    Column("unique_volunteers_event_sum", "UNIQUE", attendance="unique_volunteers_event_count"),
    Column("unique_parishes_event_sum", "UNIQUE", attendance="unique_parishes_event_count"),
)

GOODS_BUCKETS = (BUCKET_CLOTHING, BUCKET_HYGIENE, BUCKET_HOUSEHOLD)

GROUP_LABELS = {
    BUCKET_DIAPERS: "Diapers",
    BUCKET_HYGIENE: "Hygiene",
    BUCKET_CLOTHING: "Clothing",
    BUCKET_HOUSEHOLD: "Household Goods",
    BUCKET_FORMULA: "Formula",
    BUCKET_FOOD: "Food",
    BUCKET_TRANSPORTATION: "Bus passes",
}
GROUP_ORDER = (BUCKET_DIAPERS, BUCKET_HYGIENE, BUCKET_CLOTHING, BUCKET_HOUSEHOLD,
               BUCKET_FORMULA, BUCKET_FOOD, BUCKET_TRANSPORTATION)


def _add_line(cells, line):
    bucket = line.report_bucket
    quantity = line.quantity
    value = line.computed_value or ZERO

    def add(key, amount):
        cells[key] = cells.get(key, 0) + amount

    if bucket == BUCKET_FORMULA:
        add("formula_containers", quantity)
        add("food_value", value)
    elif bucket == BUCKET_FOOD:
        add("food_value", value)
    elif bucket == BUCKET_DIAPERS:
        add("diapers_count", quantity)
        add("diapers_value", value)
        add("non_food_gik", value)
    elif bucket in GOODS_BUCKETS:
        add("goods_count", quantity)
        add("goods_value", value)
        add("non_food_gik", value)
    elif bucket == BUCKET_TRANSPORTATION:
        add("bus_passes_count", quantity)
        add("transportation_value", value)


@dataclass
class Row:
    kind: str
    cells: dict = field(default_factory=dict)
    batch_id: int = None


@dataclass
class MonthlyReport:
    year: int
    month: int
    columns: list
    labels: dict
    rows: list
    totals: dict
    rate_text: str
    unique_clients: int
    notes: list

    @property
    def start(self):
        return datetime.date(self.year, self.month, 1)

    @property
    def end(self):
        return totals.month_range(self.year, self.month)[1]

    @property
    def title(self):
        return f"FOCUS Houston — {self.start.strftime('%B %Y')}"


def _labels():
    labels = {"date": "Date", "service": "Service", "description": "Description"}
    labels.update({s.key: s.label for s in metrics.settings()})
    return labels


def _description(batch):
    return ""


def rate_text():
    items = [i for i in Item.query.filter_by(active=True).all()
             if i.report_group == BUCKET_DIAPERS and i.current_price() is not None]
    items.sort(key=lambda i: (i.report_bucket != BUCKET_DIAPERS, i.name))
    return " · ".join(f"{i.name} ${i.current_price():,.2f}" for i in items)


def monthly(year, month):
    start, end = totals.month_range(year, month)
    enabled = metrics.enabled_keys()
    columns = [c for c in MONTHLY_COLUMNS if c in FIXED or c.key in enabled]
    rows, notes = session_rows(start, end, columns)

    summed = {}
    for row in rows:
        for column in columns:
            if column in FIXED:
                continue
            value = row.cells.get(column.key)
            if value is not None:
                summed[column.key] = summed.get(column.key, 0) + value

    return MonthlyReport(
        year=year, month=month, columns=columns, labels=_labels(), rows=rows,
        totals=summed, rate_text=rate_text(),
        unique_clients=metrics.REGISTRY["unique_clients_distinct"].compute(metrics.Period(start, end)),
        notes=notes,
    )


def session_rows(start, end, columns):
    rows, notes = [], []
    for batch in totals.committed_batches(start, end):
        batch_row = Row("batch", batch_id=batch.id, cells={
            "date": batch.date,
            "service": batch.service_type.name if batch.service_type else "",
            "description": _description(batch),
        })
        if batch.attendance is not None:
            for column in columns:
                if column.attendance:
                    stored = getattr(batch.attendance, column.attendance)
                    if column.unit == metrics.UNIT_HOURS:
                        stored = attendance.hours_from_tenths(stored)
                    if stored:
                        batch_row.cells[column.key] = stored
        rows.append(batch_row)

        by_group = {}
        for line in batch.active_lines:
            if line.report_bucket is None:
                notes.append(f"{batch.date.strftime('%-m/%-d')}: {line.quantity} × "
                             f"{line.custom_name} is not in the catalog yet, so it is in no "
                             f"column. Sort it in the review queue.")
                continue
            if line.needs_price:
                notes.append(f"{batch.date.strftime('%-m/%-d')}: {line.quantity} × "
                             f"{line.display_name} has no value yet (review queue).")
            group = (line.item.display_group if line.item else None) or line.report_bucket
            _add_line(by_group.setdefault(group, {}), line)
        for group in sorted(by_group, key=lambda g: (GROUP_ORDER.index(g) if g in GROUP_ORDER else 99, g)):
            cells = by_group[group]
            cells["description"] = GROUP_LABELS.get(group, group.replace("_", " ").title())
            rows.append(Row("item", cells=cells, batch_id=batch.id))
    return rows, notes


@dataclass
class AnnualRow:
    key: str
    label: str
    unit: str
    frequency: str
    months: list
    year: object


@dataclass
class AnnualGroup:
    group: metrics.Group
    rows: list


@dataclass
class AnnualReport:
    year: int
    groups: list
    measured_to: datetime.date

    @property
    def title(self):
        return f"FOCUS Houston Metrics {self.year}"


def annual(year):
    today = clock.local_today()
    month_values = []
    for month in range(1, 13):
        start, end = totals.month_range(year, month)
        month_values.append(metrics.values(start, end) if start <= today else None)
    year_values = metrics.values(datetime.date(year, 1, 1), datetime.date(year, 12, 31))

    by_group = {}
    for setting in metrics.settings():
        if not setting.enabled:
            continue
        metric = setting.metric
        months = [None if values is None or metric.frequency == metrics.ANNUAL
                  else values[metric.key] for values in month_values]
        by_group.setdefault(metric.group, []).append(AnnualRow(
            key=metric.key, label=setting.label, unit=metric.unit,
            frequency=metric.frequency, months=months, year=year_values[metric.key],
        ))
    groups = [AnnualGroup(g, by_group[g.key]) for g in metrics.GROUPS if g.key in by_group]
    return AnnualReport(year=year, groups=groups, measured_to=min(today, datetime.date(year, 12, 31)))


def is_additive(row):
    return (row.frequency == metrics.MONTHLY
            and row.unit in (metrics.UNIT_COUNT, metrics.UNIT_MONEY, metrics.UNIT_HOURS)
            and row.key not in NOT_ADDITIVE)


NOT_ADDITIVE = {"unique_clients_distinct", "intake_hours_total"}


def as_text(value, unit):
    if value is None:
        return "n/a" if unit == metrics.UNIT_PERCENT else "—"
    if unit == metrics.UNIT_MONEY:
        return f"${Decimal(value):,.2f}"
    if unit == metrics.UNIT_PERCENT:
        return f"{value}%"
    if unit == metrics.UNIT_MINUTES:
        return f"{value} min"
    if isinstance(value, int):
        return f"{value:,}"
    return str(value)
