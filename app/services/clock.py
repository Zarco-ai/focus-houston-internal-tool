
import contextlib
import datetime
from zoneinfo import ZoneInfo

HOUSTON = ZoneInfo("America/Chicago")

UTC = datetime.timezone.utc

_frozen_at = None


def now_utc():
    if _frozen_at is not None:
        return _frozen_at
    return datetime.datetime.now(UTC)


def local_now():
    return now_utc().astimezone(HOUSTON)


def local_today():
    return local_now().date()


def to_local(moment):
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
    global _frozen_at
    if moment.tzinfo is None:
        raise ValueError("freeze() needs a timezone-aware datetime.")
    previous = _frozen_at
    _frozen_at = moment.astimezone(UTC)
    try:
        yield
    finally:
        _frozen_at = previous
