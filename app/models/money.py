
from decimal import Decimal, ROUND_HALF_UP

from sqlalchemy import types

CENTS = Decimal("100")
PENNY = Decimal("0.01")


def to_money(value):
    if value is None:
        return None
    return Decimal(str(value)).quantize(PENNY, rounding=ROUND_HALF_UP)


class Money(types.TypeDecorator):

    impl = types.Integer
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        dollars = Decimal(str(value))
        return int((dollars * CENTS).quantize(Decimal("1"), rounding=ROUND_HALF_UP))

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return (Decimal(value) / CENTS).quantize(PENNY)
