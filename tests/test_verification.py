
import datetime
import random

import pytest

from app.services import retention, verify

D = datetime.date
EDGE_GAPS = (1, 7, 30, 58, 59, 60, 61, 62, 63, 90, 120)


def random_people(rng, count):
    people = {}
    for person in range(count):
        day = D(2025, 1, 1) + datetime.timedelta(days=rng.randint(0, 400))
        dates = [day]
        for _ in range(rng.randint(0, 6)):
            day += datetime.timedelta(days=rng.choice(EDGE_GAPS))
            dates.append(day)
        people[person] = sorted(set(dates))
    return people


@pytest.mark.parametrize("seed", range(5))
def test_fast_and_brute_force_agree(seed):
    rng = random.Random(seed)
    people = random_people(rng, 150)
    today = D(2026, 6, 1)
    for _ in range(12):
        start = D(2025, 1, 1) + datetime.timedelta(days=rng.randint(0, 480))
        end = start + datetime.timedelta(days=rng.choice((0, 6, 29, 30, 90, 364)))
        fast = retention.measure(start, end, window=60, today=today, people=people)
        slow = verify.measure(people, start, end, 60, today)
        assert verify.compare(fast, slow) == [], (seed, start, end)


def test_brute_force_median_matches_the_apps():
    import statistics
    from decimal import Decimal
    for values in ([45], [30, 60], [15, 45, 45, 180], [30, 45, 60, 75, 90]):
        app = Decimal(str(statistics.median(values))).quantize(Decimal("0.1"))
        assert verify.median(values) == app
