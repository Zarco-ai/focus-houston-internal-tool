
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from app.models.money import to_money

KIND_COUNT = "count"
KIND_MONEY = "money"
KIND_HOURS = "hours"

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

MAX_COUNT = 100_000
MAX_MONEY = Decimal("100000.00")


@dataclass(frozen=True)
class Field:
    name: str
    label: str
    kind: str
    section: str
    metric: str
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

    tenths = number * 10
    if tenths != tenths.to_integral_value():
        raise ValueError(f"{field.label}: hours to one decimal place, like 2.5 (got '{text}').")
    if tenths > MAX_COUNT:
        raise ValueError(f"{field.label}: {number} hours is more than this app accepts.")
    return int(tenths)


def parse_form(form, fields):
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
    if field.kind == KIND_HOURS:
        return hours_from_tenths(stored)
    if field.kind == KIND_MONEY:
        return f"{to_money(stored or 0):.2f}"
    return stored or 0


def hours_from_tenths(tenths):
    hours = Decimal(tenths or 0) / 10
    return hours.quantize(Decimal("1")) if hours == hours.to_integral_value() else hours
