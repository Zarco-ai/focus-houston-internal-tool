"""Head counts and per-event figures typed on the review screen.

FIELDS is the one list of them. The review form, the checks on what was
typed, the audit log and the report rows all read it, so a new count is one
column, one line here and one metric -- not five places to keep in step.

Every field belongs to a metric (app/services/metrics.py). When an admin
switches that metric off, its box disappears from the review screen and its
row from Totals; what was already typed stays in the database untouched.

Counts only. No field here holds a name, and none ever may.
"""

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from app.models.money import to_money

KIND_COUNT = "count"
KIND_MONEY = "money"
KIND_HOURS = "hours"  # stored as whole tenths of an hour

# Sections of the review form, in order.
SECTION_SERVED = "Served"
SECTION_FOOD = "Food and occupation"
SECTION_UNDERSTANDING = "Understanding"
SECTION_VOLUNTEERS = "Volunteers"
SECTION_UNIQUE = "Unique (as counted at this event)"
SECTIONS = (
    SECTION_SERVED,
    SECTION_FOOD,
    SECTION_UNDERSTANDING,
    SECTION_VOLUNTEERS,
    SECTION_UNIQUE,
)

# The largest number any one box accepts. Far above anything a session can
# produce, and well inside a 32-bit column; a typo of six extra zeros is
# refused instead of stored.
MAX_COUNT = 100_000
MAX_MONEY = Decimal("100000.00")


@dataclass(frozen=True)
class Field:
    name: str      # the attendance column
    label: str     # the box on the review screen
    kind: str
    section: str
    metric: str    # the metric that shows it, and switches it off
    help: str = ""


FIELDS = (
    Field("individuals_served", "Individuals served", KIND_COUNT, SECTION_SERVED,
          "individuals_served"),
    Field("children_served", "Children served", KIND_COUNT, SECTION_SERVED,
          "children_served"),
    Field("meals_value", "Value of meals provided ($)", KIND_MONEY, SECTION_FOOD,
          "meals_value", "Donated meals only. Food handed out is counted as items."),
    Field("temp_employment_placed", "Temporary employment assignments placed",
          KIND_COUNT, SECTION_FOOD, "temp_employment_placed"),
    Field("education_participants_unique", "Unique education participants",
          KIND_COUNT, SECTION_UNDERSTANDING, "education_participants_unique",
          "Different people in the class today."),
    Field("education_participants_total", "Total education participants",
          KIND_COUNT, SECTION_UNDERSTANDING, "education_participants_total"),
    Field("education_sessions_held", "Education / support group sessions held",
          KIND_COUNT, SECTION_UNDERSTANDING, "education_sessions_held"),
    Field("medical_services_served", "Served with medical services (partnership)",
          KIND_COUNT, SECTION_UNDERSTANDING, "medical_services_served"),
    Field("case_management_served", "Served with case management",
          KIND_COUNT, SECTION_UNDERSTANDING, "case_management_served"),
    Field("volunteers_total", "Volunteers", KIND_COUNT, SECTION_VOLUNTEERS,
          "volunteers_total"),
    Field("volunteer_hours_tenths", "Volunteer hours", KIND_HOURS, SECTION_VOLUNTEERS,
          "volunteer_hours", "All volunteers together, e.g. 4 people x 2.5 hours = 10."),
    Field("volunteer_opportunities", "Volunteer opportunities offered", KIND_COUNT,
          SECTION_VOLUNTEERS, "volunteer_opportunities"),
    Field("unique_clients_event_count", "Unique clients", KIND_COUNT, SECTION_UNIQUE,
          "unique_clients_event_sum"),
    Field("unique_volunteers_event_count", "Unique volunteers", KIND_COUNT,
          SECTION_UNIQUE, "unique_volunteers_event_sum"),
    Field("unique_parishes_event_count", "Unique parishes represented", KIND_COUNT,
          SECTION_UNIQUE, "unique_parishes_event_sum"),
)

FIELDS_BY_NAME = {field.name: field for field in FIELDS}


def parse_value(field, raw):
    """What was typed in one box, as the value to store. Blank is 0.

    Raises ValueError with a sentence for the screen. Unlike a quantity on
    the counting screen, nothing here is quietly clamped: these numbers go
    to the funder as typed, so a "-3" or a "12.5 children" is refused and
    shown back rather than turned into something nobody typed.
    """
    text = "" if raw is None else str(raw).strip()
    if field.kind == KIND_MONEY:
        text = text.replace("$", "").replace(",", "")
    if not text:
        return to_money(0) if field.kind == KIND_MONEY else 0

    if field.kind == KIND_COUNT:
        if not (text.isascii() and text.isdigit()):
            raise ValueError(f"{field.label}: type a whole number, 0 or more (got '{text}').")
        value = int(text)
        if value > MAX_COUNT:
            raise ValueError(f"{field.label}: {value:,} is more than this app accepts.")
        return value

    try:
        number = Decimal(text)
    except InvalidOperation:
        raise ValueError(f"{field.label}: '{text}' isn't a number.")
    if not number.is_finite() or number < 0:
        raise ValueError(f"{field.label}: type 0 or more (got '{text}').")

    if field.kind == KIND_MONEY:
        if number != number.quantize(Decimal("0.01")):
            raise ValueError(f"{field.label}: dollars and cents only (got '{text}').")
        if number > MAX_MONEY:
            raise ValueError(f"{field.label}: ${number:,} is more than this app accepts.")
        return to_money(number)

    # Hours, to a tenth: 2.5 is 25 tenths. 2.25 can't be stored exactly, so
    # it is refused rather than rounded.
    tenths = number * 10
    if tenths != tenths.to_integral_value():
        raise ValueError(f"{field.label}: hours to one decimal place, like 2.5 (got '{text}').")
    if tenths > MAX_COUNT:
        raise ValueError(f"{field.label}: {number} hours is more than this app accepts.")
    return int(tenths)


def parse_form(form, fields):
    """Read every box in `fields` from a submitted form.

    Returns ({name: value}, [error sentences]). A field missing from the form
    altogether is left out of the result, so it is left alone when saving --
    that is what keeps a value typed before its metric was switched off.
    """
    values, errors = {}, []
    for field in fields:
        if field.name not in form:
            continue
        try:
            values[field.name] = parse_value(field, form.get(field.name))
        except ValueError as error:
            errors.append(str(error))
    return values, errors


def display_value(field, stored):
    """A stored value as the review box shows it."""
    if field.kind == KIND_HOURS:
        return hours_from_tenths(stored)
    if field.kind == KIND_MONEY:
        return f"{to_money(stored or 0):.2f}"
    return stored or 0


def hours_from_tenths(tenths):
    """25 -> Decimal('2.5'). Whole hours show without the .0."""
    hours = Decimal(tenths or 0) / 10
    return hours.quantize(Decimal("1")) if hours == hours.to_integral_value() else hours
