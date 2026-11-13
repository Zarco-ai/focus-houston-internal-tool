"""A second, deliberately simple way of computing the Program Health numbers.

app/services/retention.py is written to be quick: it bisects sorted visit
dates. This module answers the same questions the slow, obvious way -- walk
every day, ask "was her last visit within the window?" -- so that the two
can be compared on thousands of made-up mothers (flask demo check, and
tests/test_verification.py). If they ever disagree, one of them is wrong,
and the disagreement says on which day for which mother.

Nothing in the app uses this to report anything.
"""

import datetime

ONE_DAY = datetime.timedelta(days=1)


def active_on(dates, day, window):
    """Walk backwards from `day` for `window` days looking for a visit."""
    visits = set(dates)
    for back in range(window + 1):
        if day - datetime.timedelta(days=back) in visits:
            return True
    return False


def separations_between(dates, start, end, window):
    """Days in start..end on which she went from active to not active."""
    count = 0
    day = start
    while day <= end:
        if active_on(dates, day - ONE_DAY, window) and not active_on(dates, day, window):
            count += 1
        day += ONE_DAY
    return count


def measure(people, start, end, window, today):
    """The same numbers as retention.measure(), computed by brute force."""
    measured_to = min(end, today)
    out = dict(S=0, E=0, N=0, R=0, returned=0, new_still_active=0, separations=0,
               engaged=0, not_engaged=0, maturing=0)
    for dates in people.values():
        joined = min(dates)
        at_start = active_on(dates, start, window)
        at_end = active_on(dates, measured_to, window)
        inside = start <= joined <= end
        out["S"] += at_start
        out["E"] += at_end
        out["N"] += inside
        if at_start and at_end:
            out["R"] += 1
        elif at_end and joined < start:
            out["returned"] += 1
        elif at_end:
            out["new_still_active"] += 1
        out["separations"] += separations_between(dates, start, measured_to, window)
        if inside:
            later = [d for d in dates if joined < d <= joined + datetime.timedelta(days=window)]
            if later:
                out["engaged"] += 1
            elif joined + datetime.timedelta(days=window) < today:
                out["not_engaged"] += 1
            else:
                out["maturing"] += 1
    return out


FIELDS = ("S", "E", "N", "R", "returned", "new_still_active", "separations",
          "engaged", "not_engaged", "maturing")


def compare(fast, slow):
    """[(field, fast value, slow value)] for every field that differs."""
    return [(f, getattr(fast, f), slow[f]) for f in FIELDS if getattr(fast, f) != slow[f]]


def intake_minutes(start, end):
    """Counted intake times, worked out straight from the rows: surveys in
    the period whose session has a time and isn't waiting to be checked."""
    from app.models import IntakeSurvey
    from app.services import intake

    found = []
    for survey in IntakeSurvey.query.filter(IntakeSurvey.survey_date.between(start, end)):
        session = survey.session
        if session is None:
            continue
        minutes = session.slider_minutes
        if minutes is None and session.timer_seconds is not None:
            minutes = int((session.timer_seconds + 30) // 60)
        if minutes is not None and not intake.needs_confirmation(session):
            found.append(minutes)
    return found


def median(values):
    """Middle value (mean of the middle two), to a tenth: written out by hand
    rather than with statistics.median, which the app uses."""
    from decimal import ROUND_HALF_UP, Decimal

    ordered = sorted(values)
    middle = len(ordered) // 2
    value = Decimal(ordered[middle]) if len(ordered) % 2 else (
        Decimal(ordered[middle - 1] + ordered[middle]) / 2)
    return value.quantize(Decimal("0.1"), ROUND_HALF_UP)
