
import secrets

CODE_LENGTH = 5

_DAMM_TABLE = (
    (0, 3, 1, 7, 5, 9, 8, 6, 4, 2),
    (7, 0, 9, 2, 1, 5, 4, 8, 6, 3),
    (4, 2, 0, 6, 8, 7, 1, 3, 5, 9),
    (1, 7, 5, 0, 9, 8, 3, 4, 2, 6),
    (6, 1, 2, 3, 0, 4, 5, 9, 7, 8),
    (3, 6, 7, 4, 2, 0, 9, 5, 8, 1),
    (5, 8, 6, 9, 7, 2, 0, 1, 3, 4),
    (8, 9, 4, 5, 3, 6, 2, 0, 1, 7),
    (9, 4, 3, 8, 6, 1, 7, 2, 0, 5),
    (2, 5, 8, 1, 4, 3, 6, 7, 9, 0),
)

_BASE_LOW = 1000
_BASE_HIGH = 9999
POSSIBLE_CODES = _BASE_HIGH - _BASE_LOW + 1


class CardNumberError(Exception):
    pass


def _interim(digits):
    interim = 0
    for digit in digits:
        interim = _DAMM_TABLE[interim][int(digit)]
    return interim


def check_digit(base):
    if not base or not base.isdigit():
        raise ValueError(f"Expected digits, got {base!r}.")
    return str(_interim(base))


def make_code(base_number):
    if not _BASE_LOW <= base_number <= _BASE_HIGH:
        raise ValueError(f"Base must be {_BASE_LOW}-{_BASE_HIGH}, got {base_number}.")
    base = str(base_number)
    return base + check_digit(base)


def normalize(raw):
    if raw is None:
        return ""
    return "".join(ch for ch in str(raw).strip() if ch not in " -.")


def is_valid(raw):
    code = normalize(raw)
    return (
        len(code) == CODE_LENGTH
        and code.isdigit()
        and code[0] != "0"
        and _interim(code) == 0
    )


def generate(count, existing=(), rng=None):
    if count < 1:
        raise CardNumberError("Ask for at least one card number.")

    taken = {normalize(code) for code in existing}
    available = POSSIBLE_CODES - len(taken)
    if count > available:
        raise CardNumberError(
            f"Only {available} unused card numbers are left in the five-digit "
            f"format; {count} were asked for."
        )

    rng = rng or secrets.SystemRandom()
    fresh = []
    seen = set(taken)
    while len(fresh) < count:
        code = make_code(rng.randint(_BASE_LOW, _BASE_HIGH))
        if code not in seen:
            seen.add(code)
            fresh.append(code)
    return fresh
