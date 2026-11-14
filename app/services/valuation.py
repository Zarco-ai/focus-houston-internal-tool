
from decimal import Decimal

from app.constants import CONDITION_NEW, CONDITION_USED
from app.models.money import to_money

ZERO = Decimal("0.00")

NEW_CONDITION_MULTIPLIER = Decimal("1")


def effective_multiplier(condition, used_multiplier):
    if condition == CONDITION_USED:
        if used_multiplier is None:
            raise ValueError(
                "A used line needs a used_multiplier. It comes from the item, "
                "never from a constant."
            )
        return Decimal(str(used_multiplier))

    if condition != CONDITION_NEW:
        raise ValueError(f"Unknown condition: {condition!r}")

    return NEW_CONDITION_MULTIPLIER


def line_value(quantity, unit_price, used_multiplier, condition):
    if unit_price is None:
        return None
    if not quantity:
        return ZERO

    if Decimal(str(unit_price)) == 0:
        return ZERO

    multiplier = effective_multiplier(condition, used_multiplier)
    raw = Decimal(int(quantity)) * Decimal(str(unit_price)) * multiplier
    return to_money(raw)


def clamp_quantity(value):
    try:
        quantity = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, quantity)


def parse_price(raw):
    if raw is None:
        return None
    if isinstance(raw, str):
        raw = raw.strip().replace("$", "").replace(",", "")
        if not raw:
            return None
    try:
        price = float(raw)
    except (TypeError, ValueError):
        return None
    return max(0.0, price)


def total_value(lines):
    total = ZERO
    for line in lines:
        if line.computed_value is not None:
            total += line.computed_value
    return total


def total_quantity(lines):
    return sum(line.quantity for line in lines)
