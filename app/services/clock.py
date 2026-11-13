"""The only place in this app that reads a clock.

Why this exists: Render's servers run on UTC. Houston is five or six hours
behind. Asking the server "what is today?" after about 7pm Houston time gets
TOMORROW's date -- so an evening class would be recorded on the wrong day,
and a mother's 60-day clock would be off by one. date.today() and
datetime.now() both ask the server, so neither is allowed anywhere else in
app/. tests/test_clock.py fails the build if one appears.

The rules:

  * A moment in time (when something was approved, edited, signed in) is a
    timezone-aware UTC datetime -- now_utc() -- stored in a TIMESTAMPTZ
    column. Convert to Houston time only to show it: to_local().
  * A calendar day (a batch date, a visit date) is Houston's date --
    local_today() -- stored in a DATE column.

Tests can stop the clock with freeze(), so a test about "23:30 in Houston"
really runs at 23:30 in Houston, whatever the machine's own clock says.
"""

import contextlib
import datetime
from zoneinfo import ZoneInfo

# FOCUS Houston's time zone. America/Chicago is the IANA name for US Central
# time; it handles the switch between CST (UTC-6) and CDT (UTC-5) itself.
HOUSTON = ZoneInfo("America/Chicago")

UTC = datetime.timezone.utc

# Set only by freeze(), only in tests. When None, the real clock is used.
_frozen_at = None


def now_utc():
    """This moment, as a timezone-aware UTC datetime."""
    if _frozen_at is not None:
        return _frozen_at
    return datetime.datetime.now(UTC)


def local_now():
    """This moment, in Houston."""
    return now_utc().astimezone(HOUSTON)


def local_today():
    """Today's date in Houston -- not on the server."""
    return local_now().date()


def to_local(moment):
    """A stored timestamp, converted to Houston time for display.

    Refuses a naive datetime: one without a zone is ambiguous by definition,
    and guessing is how the off-by-one-day bug gets back in.
    """
    if moment is None:
        return None
    if moment.tzinfo is None:
        raise ValueError(
            "Got a datetime with no time zone. Timestamps in this app are "
            "always timezone-aware UTC -- see app/services/clock.py."
        )
    return moment.astimezone(HOUSTON)


@contextlib.contextmanager
def freeze(moment):
    """Stop the clock at `moment` for the duration of a `with` block.

    For tests. `moment` must be timezone-aware; it can be in any zone (say,
    23:30 in Houston) and is converted to UTC like a real timestamp would be.
    """
    global _frozen_at
    if moment.tzinfo is None:
        raise ValueError("freeze() needs a timezone-aware datetime.")
    previous = _frozen_at
    _frozen_at = moment.astimezone(UTC)
    try:
        yield
    finally:
        _frozen_at = previous
