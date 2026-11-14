
import datetime
import statistics
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from functools import cached_property
from typing import Callable

import sqlalchemy as sa

from app.constants import (
    BUCKET_DIAPERS,
    BUCKET_FOOD,
    BUCKET_FORMULA,
    BUCKET_TRANSPORTATION,
    COMBINED_GOODS_BUCKETS,
    STATUS_COMMITTED,
)
from app.extensions import db
from app.models import AnnualMetricValue, Attendance, Batch, MetricDefinition
from app.services import audit, intake, retention, totals
from app.services.attendance import FIELDS as ATTENDANCE_FIELDS
from app.services.attendance import hours_from_tenths
from app.services.valuation import ZERO

UNIT_COUNT = "count"
UNIT_MONEY = "money"
UNIT_HOURS = "hours"
UNIT_MINUTES = "minutes"
UNIT_PERCENT = "percent"

MONTHLY = "monthly"
ANNUAL = "annual"


@dataclass(frozen=True)
class Group:
    key: str
    label: str
    note: str = ""


GROUPS = (
    Group("SERVED", "SERVED", "From the program director's monthly sheet; not on the FOCUS North America template."),
    Group("FOOD", "FOOD"),
    Group("OCCUPATION", "OCCUPATION"),
    Group("CLOTHING", "CLOTHING"),
    Group("UNDERSTANDING", "UNDERSTANDING"),
    Group("SHELTER", "SHELTER"),
    Group("VOLUNTEERS", "VOLUNTEERS"),
    Group("UNIQUE", "UNIQUE", "Counted at each event and added up, so someone at two "
                              "events counts twice. A true count of different people "
                              "comes with participant IDs."),
    Group("ANNUAL", "ANNUAL METRICS", "Typed once a year by an admin. Shown when the "
                                      "period is a whole calendar year."),
    Group("PROGRAM_HEALTH", "PROGRAM HEALTH", "From intakes and visits (the intake survey and "
                                              "the Google Form). Definitions in docs/METRICS.md."),
)
GROUPS_BY_KEY = {group.key: group for group in GROUPS}


class Period:

    def __init__(self, start, end):
        self.start = start
        self.end = end
        self.buckets = totals.bucket_totals(start, end)
        self.attendance = _attendance_sums(start, end)
        self.year = whole_year(start, end)
        self.annual = _annual_values(self.year) if self.year else {}

    @cached_property
    def retention(self):
        return retention.measure(self.start, self.end)

    @cached_property
    def intake_minutes(self):
        return sorted(intake.counted_intake_minutes(self.start, self.end))

    def bucket_quantity(self, key):
        return self.buckets[key]["quantity"] if key in self.buckets else 0

    def bucket_value(self, key):
        return self.buckets[key]["value"] if key in self.buckets else ZERO


def whole_year(start, end):
    if (start.month, start.day) == (1, 1) and end == datetime.date(start.year, 12, 31):
        return start.year
    return None


def _attendance_sums(start, end):
    columns = [
        sa.func.coalesce(sa.func.sum(getattr(Attendance, f.name)), 0)
        for f in ATTENDANCE_FIELDS
    ]
    row = (
        db.session.query(*columns)
        .select_from(Attendance)
        .join(Batch, Attendance.batch_id == Batch.id)
        .filter(
            Batch.status == STATUS_COMMITTED,
            Batch.date >= start,
            Batch.date <= end,
        )
        .one()
    )
    return dict(zip((f.name for f in ATTENDANCE_FIELDS), row))


def _annual_values(year):
    rows = AnnualMetricValue.query.filter_by(year=year).all()
    return {row.metric_key: row.value for row in rows}


def _attendance(name):
    return lambda period: period.attendance[name]


def goods_count(period):
    return sum(period.bucket_quantity(key) for key in COMBINED_GOODS_BUCKETS)


def goods_value(period):
    total = ZERO
    for key in COMBINED_GOODS_BUCKETS:
        total += period.bucket_value(key)
    return total


def diapers_value(period):
    return period.bucket_value(BUCKET_DIAPERS)


def non_food_gik(period):
    return goods_value(period) + diapers_value(period)


def food_value(period):
    return period.bucket_value(BUCKET_FOOD) + period.bucket_value(BUCKET_FORMULA)


def volunteer_hours(period):
    return hours_from_tenths(period.attendance["volunteer_hours_tenths"])


def _annual(key):
    return lambda period: period.annual.get(key)


def _one_place(value):
    return Decimal(value).quantize(Decimal("0.1"), ROUND_HALF_UP)


def intake_count(period):
    return len(period.intake_minutes)


def intake_hours_total(period):
    return _one_place(Decimal(sum(period.intake_minutes)) / 60)


def intake_minutes_mean(period):
    minutes = period.intake_minutes
    return _one_place(Decimal(sum(minutes)) / len(minutes)) if minutes else None


def intake_minutes_median(period):
    minutes = period.intake_minutes
    return _one_place(Decimal(str(statistics.median(minutes)))) if minutes else None


def _intake_detail(period):
    waiting = intake.waiting_to_be_checked(period.start, period.end)
    return f"{waiting} more waiting to be checked, not counted" if waiting else ""


def _as_of(r):
    return f" (measured to {r.measured_to.strftime('%b %-d')})" if r.truncated else ""


def _retention_detail(period):
    r = period.retention
    if r.future:
        return "The period hasn't started."
    return f"E {r.E} − N {r.N}, ÷ S {r.S}{_as_of(r)}"


def _retained_detail(period):
    r = period.retention
    if r.future:
        return "The period hasn't started."
    return (f"{r.R} of S {r.S} active at both ends. Of E {r.E} at the end: {r.R} retained + "
            f"{r.returned} came back + {r.new_still_active} new{_as_of(r)}")


def _engagement_detail(period):
    r = period.retention
    if r.maturing:
        return (f"Too early: {r.maturing} of {r.joined} who joined are still within their "
                f"{r.window} days. So far {r.engaged} came back. The rate appears once "
                f"everyone's {r.window} days are over.")
    return f"{r.engaged} of {r.engaged + r.not_engaged} came back within {r.window} days"


def _turnover_detail(period):
    r = period.retention
    if r.future:
        return "The period hasn't started."
    return f"{r.separations} separations ÷ average headcount {r.average_headcount}{_as_of(r)}"


ITEMS = "Counted items"
INTAKES = "Intake page"
VISITS = "Visits (intake survey and Google Form)"
REVIEW = "Review screen, per batch"
TYPED_YEARLY = "Typed once a year (Settings → Annual figures)"


@dataclass(frozen=True)
class Metric:
    key: str
    label: str
    group: str
    unit: str
    source: str
    compute: Callable = field(repr=False)
    definition: str = ""
    frequency: str = MONTHLY
    detail: Callable = field(default=None, repr=False)
    on_by_default: bool = True


METRICS = (
    Metric("individuals_served", "# of Individuals Served", "SERVED", UNIT_COUNT, REVIEW,
           _attendance("individuals_served")),
    Metric("children_served", "# of Children Served", "SERVED", UNIT_COUNT, REVIEW,
           _attendance("children_served")),
    Metric("formula_containers", "# of Baby Formula Containers Distributed", "FOOD",
           UNIT_COUNT, ITEMS, lambda p: p.bucket_quantity(BUCKET_FORMULA),
           "Quantity of items in the Baby Formula bucket."),
    Metric("meals_value", "Value ($) of Meals Provided (GIK)", "FOOD", UNIT_MONEY, REVIEW,
           _attendance("meals_value"), on_by_default=False),
    Metric("food_value", "Value ($) of Food Distributed (GIK)", "FOOD", UNIT_MONEY, ITEMS,
           food_value, "Food and Baby Formula buckets. Formula is Food GIK, never Non-Food."),
    Metric("temp_employment_placed", "# of Temporary Employment Assignments Placed",
           "OCCUPATION", UNIT_COUNT, REVIEW, _attendance("temp_employment_placed"),
           on_by_default=False),
    Metric("goods_count", "# of Clothing / Hygiene Products / Household Goods Distributed",
           "CLOTHING", UNIT_COUNT, ITEMS, goods_count,
           "Clothing, Hygiene and Household Goods buckets."),
    Metric("diapers_count", "# of Individual Diapers Distributed", "CLOTHING", UNIT_COUNT,
           ITEMS, lambda p: p.bucket_quantity(BUCKET_DIAPERS), "Individual Diapers bucket."),
    Metric("goods_value",
           "Value ($) of Clothing / Hygiene Products / Household Goods Distributed",
           "CLOTHING", UNIT_MONEY, ITEMS, goods_value,
           "Clothing, Hygiene and Household Goods buckets."),
    Metric("diapers_value", "Value ($) of Individual Diapers Distributed", "CLOTHING",
           UNIT_MONEY, ITEMS, diapers_value, "Individual Diapers bucket."),
    Metric("non_food_gik", "Non-Food GIK $'s", "CLOTHING", UNIT_MONEY, "Computed",
           non_food_gik, "Value of clothing / hygiene / household + value of diapers. "
                         "Never typed."),
    Metric("education_participants_unique", "# of Unique Education Participants",
           "UNDERSTANDING", UNIT_COUNT, REVIEW, _attendance("education_participants_unique"),
           "Different people at each class, added up over the period."),
    Metric("education_participants_total", "# of Total Education Participants",
           "UNDERSTANDING", UNIT_COUNT, REVIEW, _attendance("education_participants_total")),
    Metric("education_sessions_held", "# of Total Education / Support Group Sessions Held",
           "UNDERSTANDING", UNIT_COUNT, REVIEW, _attendance("education_sessions_held")),
    Metric("medical_services_served",
           "# of Individuals Served with Medical Services (Partnership)", "UNDERSTANDING",
           UNIT_COUNT, REVIEW, _attendance("medical_services_served"), on_by_default=False),
    Metric("case_management_served", "# of Individuals Served with Case Management",
           "UNDERSTANDING", UNIT_COUNT, REVIEW, _attendance("case_management_served")),
    Metric("bus_passes_count", "# of Bus Passes / Transportation Assistance Distributed",
           "SHELTER", UNIT_COUNT, ITEMS, lambda p: p.bucket_quantity(BUCKET_TRANSPORTATION),
           "Transportation Assistance bucket.", on_by_default=False),
    Metric("transportation_value", "Value ($) of Transportation Assistance", "SHELTER",
           UNIT_MONEY, ITEMS, lambda p: p.bucket_value(BUCKET_TRANSPORTATION),
           "Transportation Assistance bucket. Not goods in kind: never in Non-Food GIK.",
           on_by_default=False),
    Metric("volunteers_total", "# of Total Volunteers", "VOLUNTEERS", UNIT_COUNT, REVIEW,
           _attendance("volunteers_total")),
    Metric("volunteer_hours", "# of Total Volunteer Hours (# of Event Hours x # of Volunteers)",
           "VOLUNTEERS", UNIT_HOURS, REVIEW, volunteer_hours,
           "Typed per event as total hours (Phase 0 answer 7)."),
    Metric("volunteer_opportunities", "# of Volunteer Opportunities Offered", "VOLUNTEERS",
           UNIT_COUNT, REVIEW, _attendance("volunteer_opportunities")),
    Metric("unique_clients_distinct", "# of Unique Clients Served", "UNIQUE", UNIT_COUNT,
           "Visits (intake survey and Google Form)",
           lambda p: retention.distinct_people_with_visits(p.start, p.end),
           "Different mothers with a visit in the period, by card number (Phase 0 answer 6). "
           "A merged card counts once."),
    Metric("unique_clients_event_sum", "# of Unique Clients Served (sum of per-event counts)",
           "UNIQUE", UNIT_COUNT, REVIEW, _attendance("unique_clients_event_count")),
    Metric("unique_volunteers_event_sum", "# of Unique Volunteers (sum of per-event counts)",
           "UNIQUE", UNIT_COUNT, REVIEW, _attendance("unique_volunteers_event_count")),
    Metric("unique_parishes_event_sum",
           "# of Unique Parishes Represented (sum of per-event counts)", "UNIQUE",
           UNIT_COUNT, REVIEW, _attendance("unique_parishes_event_count")),
    Metric("advisory_board_members", "# of Advisory Board Members", "ANNUAL", UNIT_COUNT,
           TYPED_YEARLY, _annual("advisory_board_members"), frequency=ANNUAL),
    Metric("advisory_board_parishes", "# of Parishes Represented on Advisory Board",
           "ANNUAL", UNIT_COUNT, TYPED_YEARLY, _annual("advisory_board_parishes"),
           frequency=ANNUAL),
    Metric("one_time_donors", "# of One-Time Individual Donors", "ANNUAL", UNIT_COUNT,
           TYPED_YEARLY, _annual("one_time_donors"), frequency=ANNUAL),
    Metric("monthly_donors", "# of Monthly Individual Donors", "ANNUAL", UNIT_COUNT,
           TYPED_YEARLY, _annual("monthly_donors"), frequency=ANNUAL),
    Metric("parish_donors", "# of Parish Donors", "ANNUAL", UNIT_COUNT, TYPED_YEARLY,
           _annual("parish_donors"), frequency=ANNUAL),
    Metric("intakes_timed", "# of Intakes (with a counted time)", "PROGRAM_HEALTH", UNIT_COUNT,
           INTAKES, intake_count, "Intake surveys dated in the period with a time that counts.",
           detail=_intake_detail),
    Metric("intake_hours_total", "Total intake time (hours)", "PROGRAM_HEALTH", UNIT_HOURS,
           INTAKES, intake_hours_total),
    Metric("intake_minutes_mean", "Average intake time (minutes)", "PROGRAM_HEALTH",
           UNIT_MINUTES, INTAKES, intake_minutes_mean),
    Metric("intake_minutes_median", "Median intake time (minutes)", "PROGRAM_HEALTH",
           UNIT_MINUTES, INTAKES, intake_minutes_median,
           "The middle time: one forgotten timer barely moves it."),
    Metric("retention_focus", "Retention (FOCUS formula)", "PROGRAM_HEALTH", UNIT_PERCENT,
           VISITS, lambda p: p.retention.retention_focus,
           "(E − N) ÷ S. Can pass 100% when mothers who had lapsed come back.",
           detail=_retention_detail,
           on_by_default=False),
    Metric("retained", "Retained (active at both start and end)", "PROGRAM_HEALTH",
           UNIT_PERCENT, VISITS, lambda p: p.retention.retained_rate,
           "R ÷ S: of the mothers active at the start, the share still active at the end.",
           detail=_retained_detail),
    Metric("engagement", "Engagement", "PROGRAM_HEALTH", UNIT_PERCENT, VISITS,
           lambda p: p.retention.engagement,
           "Of the mothers who joined in the period, the share who came back within the "
           "activity window.", detail=_engagement_detail),
    Metric("turnover", "Turnover", "PROGRAM_HEALTH", UNIT_PERCENT, VISITS,
           lambda p: p.retention.turnover,
           "Separations ÷ average headcount.", detail=_turnover_detail),
)

REGISTRY = {metric.key: metric for metric in METRICS}
ANNUAL_METRICS = tuple(metric for metric in METRICS if metric.frequency == ANNUAL)


@dataclass
class Setting:

    metric: Metric
    label: str
    enabled: bool

    @property
    def key(self):
        return self.metric.key

    @property
    def label_changed(self):
        return self.label != self.metric.label


def settings():
    rows = {row.key: row for row in MetricDefinition.query.all()}
    result = []
    for metric in METRICS:
        row = rows.get(metric.key)
        result.append(Setting(
            metric=metric,
            label=(row.label if row and row.label else metric.label),
            enabled=(row.enabled if row else metric.on_by_default),
        ))
    return result


def enabled_keys():
    return {setting.key for setting in settings() if setting.enabled}


def is_enabled(key):
    row = MetricDefinition.query.filter_by(key=key).one_or_none()
    return REGISTRY[key].on_by_default if row is None else row.enabled


def update_setting(key, label=None, enabled=None):
    metric = REGISTRY.get(key)
    if metric is None:
        raise ValueError(f"There is no metric called '{key}'.")

    row = MetricDefinition.query.filter_by(key=key).one_or_none()
    if row is None:
        row = MetricDefinition(key=key, label=None, enabled=metric.on_by_default)
        db.session.add(row)
        db.session.flush()

    changed = False
    if label is not None:
        label = " ".join(label.split())
        if len(label) > 200:
            raise ValueError("A label can be at most 200 characters.")
        new_label = None if label in ("", metric.label) else label
        changed |= audit.apply_change(row, "label", new_label, table_name="metric_definition",
                                      description=f"Metric {key} label")
    if enabled is not None:
        changed |= audit.apply_change(row, "enabled", bool(enabled),
                                      table_name="metric_definition",
                                      description=f"Metric {key} switched on/off")
    db.session.flush()
    return changed


def annual_values(year):
    return {row.metric_key: row for row in AnnualMetricValue.query.filter_by(year=year)}


def set_annual_value(year, key, value, entered_by_id):
    if key not in REGISTRY or REGISTRY[key].frequency != ANNUAL:
        raise ValueError(f"'{key}' isn't an annual figure.")
    if not 2000 <= int(year) <= 2100:
        raise ValueError(f"{year} isn't a year this app reports on.")
    if value is not None and (not isinstance(value, int) or value < 0):
        raise ValueError("An annual figure is a whole number, 0 or more.")

    row = AnnualMetricValue.query.filter_by(year=year, metric_key=key).one_or_none()
    old = row.value if row else None
    if old == value:
        return False

    if row is None:
        row = AnnualMetricValue(year=year, metric_key=key, value=value,
                                entered_by_id=entered_by_id)
        db.session.add(row)
        db.session.flush()
    elif value is None:
        db.session.delete(row)
    else:
        row.value = value
        row.entered_by_id = entered_by_id

    audit.record_change(
        table_name="annual_metric_value",
        record_id=row.id,
        field=key,
        old_value=old,
        new_value=value,
        description=f"{year} {REGISTRY[key].label}",
    )
    db.session.flush()
    return True


@dataclass
class Row:
    key: str
    label: str
    unit: str
    value: object
    source: str
    definition: str
    frequency: str
    detail: str = ""

    @property
    def is_money(self):
        return self.unit == UNIT_MONEY


@dataclass
class ReportGroup:
    group: Group
    rows: list


def report(start, end, period=None):
    period = period or Period(start, end)
    by_group = {}
    for setting in settings():
        if not setting.enabled:
            continue
        metric = setting.metric
        by_group.setdefault(metric.group, []).append(Row(
            key=metric.key,
            label=setting.label,
            unit=metric.unit,
            value=metric.compute(period),
            source=metric.source,
            definition=metric.definition,
            frequency=metric.frequency,
            detail=metric.detail(period) if metric.detail else "",
        ))
    return [ReportGroup(group, by_group[group.key]) for group in GROUPS if group.key in by_group]


def values(start, end):
    period = Period(start, end)
    return {metric.key: metric.compute(period) for metric in METRICS}
