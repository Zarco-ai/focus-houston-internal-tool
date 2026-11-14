
import datetime
import pathlib
import re

import pytest

from app.models import Batch
from app.services import batches, clock

HOUSTON = clock.HOUSTON
APP_DIR = pathlib.Path(__file__).resolve().parent.parent / "app"


def houston(year, month, day, hour, minute=0):
    return datetime.datetime(year, month, day, hour, minute, tzinfo=HOUSTON)


def test_local_today_at_23_30_houston_is_still_the_same_day():
    moment = houston(2026, 10, 20, 23, 30)
    assert moment.astimezone(datetime.timezone.utc).date() == datetime.date(2026, 10, 21)

    with clock.freeze(moment):
        assert clock.local_today() == datetime.date(2026, 10, 20)


def test_local_today_at_00_30_houston_is_the_next_day():
    with clock.freeze(houston(2026, 10, 21, 0, 30)):
        assert clock.local_today() == datetime.date(2026, 10, 21)


def test_local_today_at_23_30_in_winter_is_still_the_same_day():
    with clock.freeze(houston(2026, 1, 15, 23, 30)):
        assert clock.local_today() == datetime.date(2026, 1, 15)


@pytest.mark.parametrize(
    "moment, expected",
    [
        (houston(2026, 3, 8, 23, 30), datetime.date(2026, 3, 8)),
        (houston(2026, 3, 9, 0, 30), datetime.date(2026, 3, 9)),
        (houston(2026, 11, 1, 23, 30), datetime.date(2026, 11, 1)),
        (houston(2026, 11, 2, 0, 30), datetime.date(2026, 11, 2)),
    ],
)
def test_local_today_is_right_on_both_daylight_saving_changeover_days(moment, expected):
    with clock.freeze(moment):
        assert clock.local_today() == expected


def test_now_utc_is_timezone_aware_utc():
    now = clock.now_utc()
    assert now.tzinfo is not None
    assert now.utcoffset() == datetime.timedelta(0)


def test_to_local_refuses_a_naive_datetime():
    with pytest.raises(ValueError):
        clock.to_local(datetime.datetime(2026, 10, 20, 23, 30))


def test_to_local_converts_utc_to_houston():
    moment = datetime.datetime(2026, 10, 21, 4, 30, tzinfo=datetime.timezone.utc)
    assert clock.to_local(moment) == houston(2026, 10, 20, 23, 30)


def test_freeze_refuses_a_naive_datetime():
    with pytest.raises(ValueError):
        with clock.freeze(datetime.datetime(2026, 10, 20, 23, 30)):
            pass


def test_freeze_puts_the_real_clock_back_afterwards():
    with clock.freeze(houston(2000, 1, 1, 12)):
        pass
    assert clock.local_today().year > 2000


def test_an_evening_batch_gets_houstons_date_not_the_servers(seeded_app):
    with clock.freeze(houston(2026, 10, 20, 19, 45)):
        batch = batches.open_batch()
    assert batch.date == datetime.date(2026, 10, 20)


def test_a_draft_from_yesterday_is_stale_just_after_houston_midnight(seeded_app):
    with clock.freeze(houston(2026, 10, 20, 23, 30)):
        batch = batches.open_batch()
        assert not batch.is_stale

    with clock.freeze(houston(2026, 10, 21, 0, 30)):
        assert batch.is_stale


def test_committed_at_is_stored_as_utc_and_comes_back_timezone_aware(seeded_app):
    from app.extensions import db
    from app.services import catalog

    batch = batches.open_batch()
    batches.set_line(batch, catalog.find_item("Coat", "clothing_adult"), quantity=1)
    batch.service_type_id = 1
    with clock.freeze(houston(2026, 10, 20, 23, 30)):
        batches.commit_batch(batch)

    db.session.expire_all()
    stored = db.session.get(Batch, batch.id).committed_at
    assert stored.tzinfo is not None
    assert stored == houston(2026, 10, 20, 23, 30)
    assert clock.to_local(stored).date() == datetime.date(2026, 10, 20)


SERVER_CLOCK_CALLS = [
    (r"\bdate\.today\b", "date.today"),
    (r"\bdatetime\.today\b", "datetime.today"),
    (r"\butcnow\b", "datetime.utcnow"),
    (r"\.now\b(?!\(\s*[A-Za-z_])", "datetime.now with no time zone"),
]


def test_no_date_today_or_naive_datetime_now_outside_the_clock_module():
    offenders = []
    for path in sorted(APP_DIR.rglob("*.py")):
        if path.name == "clock.py" and path.parent.name == "services":
            continue
        source = path.read_text(encoding="utf-8")
        for line_number, line in enumerate(source.splitlines(), start=1):
            for pattern, label in SERVER_CLOCK_CALLS:
                if re.search(pattern, line):
                    offenders.append(f"{path.relative_to(APP_DIR.parent)}:{line_number} {label}")

    assert not offenders, "server clock read outside clock.py:\n" + "\n".join(offenders)


def test_the_clock_scan_would_catch_each_forbidden_call():
    samples = [
        "x = datetime.date.today()",
        "x = date.today()",
        "x = datetime.datetime.today()",
        "x = datetime.datetime.utcnow()",
        "x = datetime.datetime.now()",
        "default=datetime.datetime.now",
        "default=datetime.date.today",
        "from datetime import date; d = date.today()",
    ]
    for sample in samples:
        assert any(re.search(pattern, sample) for pattern, _ in SERVER_CLOCK_CALLS), sample

    allowed = [
        "x = datetime.datetime.now(UTC)",
        "x = clock.now_utc()",
        "x = clock.local_now()",
        "now = clock.now_utc()",
    ]
    for sample in allowed:
        assert not any(re.search(pattern, sample) for pattern, _ in SERVER_CLOCK_CALLS), sample
