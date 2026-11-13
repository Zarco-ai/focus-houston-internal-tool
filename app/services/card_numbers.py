"""Card numbers: the only identifier a mother has in this app.

Every mother -- a new intake or someone already in the program -- is handed a
number from a pre-generated list. The Center Director writes the mother's name next
to that number on a paper sheet that stays locked in the office. The app only
ever sees the number. That paper sheet is the one place a name and a number
meet, and it never goes near a computer.

The format is five digits:

    4 8 2 7 | 3
    random    check digit

Mothers type this into a Google Form on their own phones, so the code has to
survive a thumb on a small keypad. The check digit (the Damm algorithm) catches
every single mistyped digit and every swap of two neighbouring digits. A
consequence worth knowing: no two valid numbers differ in only one digit, so
one slip can never turn one mother's number into another real mother's number
-- it turns it into a number that fails validation and goes to the review
queue.

The first digit is never 0. Google Sheets drops leading zeros, so "04827"
would arrive as "4827" and never match.

The four random digits come from `secrets`, not `random`. The order carries no
meaning -- a number does not reveal how many mothers are enrolled or who came
first.

Pure Python. No Flask, no database: generate_cards.py writes the list to a
file, and the app loads that file into its own table.
"""

import secrets

CODE_LENGTH = 5

# The Damm algorithm's operation table: a quasigroup of order 10 with a zero
# diagonal. Running a number through it digit by digit lands on 0 exactly when
# the number is valid. This is the standard table from H. Michael Damm's 2004
# thesis; tests/test_card_numbers.py checks it against the published example
# (572 -> check digit 4).
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

# Four random digits with no leading zero: 1000..9999.
_BASE_LOW = 1000
_BASE_HIGH = 9999
POSSIBLE_CODES = _BASE_HIGH - _BASE_LOW + 1


class CardNumberError(Exception):
    """A request the generator cannot satisfy. The message says why."""


def _interim(digits):
    interim = 0
    for digit in digits:
        interim = _DAMM_TABLE[interim][int(digit)]
    return interim


def check_digit(base):
    """The Damm check digit for a string of digits."""
    if not base or not base.isdigit():
        raise ValueError(f"Expected digits, got {base!r}.")
    return str(_interim(base))


def make_code(base_number):
    """1000..9999 -> the full five-digit card number."""
    if not _BASE_LOW <= base_number <= _BASE_HIGH:
        raise ValueError(f"Base must be {_BASE_LOW}-{_BASE_HIGH}, got {base_number}.")
    base = str(base_number)
    return base + check_digit(base)


def normalize(raw):
    """What a mother typed, cleaned up enough to compare.

    Strips spaces, dashes and dots, because people type "4827-3" or
    "48 273". Anything else is left alone so is_valid() can reject it --
    guessing what a garbled entry meant is exactly how one mother's visit
    ends up on another mother's number.
    """
    if raw is None:
        return ""
    return "".join(ch for ch in str(raw).strip() if ch not in " -.")


def is_valid(raw):
    """True for a well-formed card number. Says nothing about whether the
    number has been handed out -- that is the app's table, not arithmetic."""
    code = normalize(raw)
    return (
        len(code) == CODE_LENGTH
        and code.isdigit()
        and code[0] != "0"
        and _interim(code) == 0
    )


def generate(count, existing=(), rng=None):
    """`count` new card numbers, none of them in `existing`, in random order.

    `existing` is every number already issued, so a second batch never
    reuses one from the first. `rng` exists for tests; the real thing uses
    the operating system's secure random source.
    """
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
