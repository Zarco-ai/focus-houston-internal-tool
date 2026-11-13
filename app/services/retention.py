"""Who is active, and retention, engagement and turnover.

Every definition here is written out in docs/METRICS.md, with worked
examples; tests/test_retention.py checks each against named mothers whose
dates were worked out by hand. All dates are Houston calendar dates. W is
the activity window, 60 days unless an admin changes it.

  Visit         An intake, or a Google Form response matched to her. A lost
                card merged into a new one: both numbers' visits are hers.
  Joined        Her first visit (her intake, almost always).
  Active on D   Her most recent visit on or before D, v, has D - v <= W. A
                visit exactly W days before still counts; W + 1 does not.
  Separation    v + W + 1, unless she visits again on or before that day. It
                stays in its month even if she comes back later (Phase 0
                answer 4).

For a period P_start..P_end:

  S  active on P_start        E  active on P_end
  N  joined inside the period
  R  active on both           Returned   active at the end, not at the
                                         start, joined before the period
  New still active   active at the end, not at the start, joined inside

  E = R + Returned + New still active, always (tests check it).

  Retention (FOCUS formula)  (E - N) / S     n/a when S = 0
  Retained                    R / S          n/a when S = 0
  Turnover                    separations / ((S + E) / 2)    n/a when 0
  Engagement                  of those who joined in the period, the share
                              with a visit on a later day within W days of
                              joining, even after the period ends (Phase 0
                              answer 5). n/a until everyone who joined has
                              had her full W days ("still maturing").

A period that runs past today is measured up to today: nobody knows
tomorrow's headcount, and a separation that hasn't happened isn't counted.

Everything is computed from the visits each time; nothing is stored, so a
corrected visit corrects every number at once.
"""

import bisect
import datetime
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal

from app.extensions import db
from app.models import Participant, ParticipantVisit, get_setting, set_setting
from app.services import audit, clock

WINDOW_KEY = "activity_window_days"
DEFAULT_WINDOW = 60
MIN_WINDOW, MAX_WINDOW = 7, 365

ONE_DAY = datetime.timedelta(days=1)


# --- The setting ---------------------------------------------------------------------


def window_days():
    try:
        days = int(get_setting(WINDOW_KEY, DEFAULT_WINDOW))
    except (TypeError, ValueError):
        return DEFAULT_WINDOW
    return days if MIN_WINDOW <= days <= MAX_WINDOW else DEFAULT_WINDOW


def set_window_days(days):
    try:
        days = int(str(days).strip())
    except (TypeError, ValueError):
        raise ValueError(f"Type a whole number of days, {MIN_WINDOW} to {MAX_WINDOW}.")
    if not MIN_WINDOW <= days <= MAX_WINDOW:
        raise ValueError(f"Type a whole number of days, {MIN_WINDOW} to {MAX_WINDOW}.")
    old = window_days()
    if days != old:
        audit.record_change(table_name="app_setting", record_id=0, field=WINDOW_KEY,
                            old_value=old, new_value=days,
                            description="Activity window (days)")
        set_setting(WINDOW_KEY, days)
    return days


# --- Visits per person ---------------------------------------------------------------------


def visits_by_person():
    """{person id: sorted list of distinct visit dates}. A merged number's
    visits go to the number it was merged into."""
    rows = (
        db.session.query(Participant.id, Participant.merged_into_id, ParticipantVisit.visit_date)
        .join(ParticipantVisit, ParticipantVisit.participant_id == Participant.id)
        .all()
    )
    people = {}
    for participant_id, merged_into_id, visit_date in rows:
        people.setdefault(merged_into_id or participant_id, set()).add(visit_date)
    return {person: sorted(dates) for person, dates in people.items()}


def last_visit_on_or_before(dates, day):
    index = bisect.bisect_right(dates, day)
    return dates[index - 1] if index else None


def is_active(dates, day, window):
    last = last_visit_on_or_before(dates, day)
    return last is not None and (day - last).days <= window


def separation_dates(dates, window):
    """Every date she became inactive: the day after her window closes,
    v + W + 1, if she has no visit on or before it.

    "On or before": a visit ON day W + 1 means she was never inactive -- she
    came back the very day she would have lapsed. Found in Phase 9 by the
    brute-force check on 2,000 made-up mothers; with `> window` here, a
    return on exactly day 61 was counted as a separation as well.
    """
    gaps = []
    for index, visit in enumerate(dates):
        following = dates[index + 1] if index + 1 < len(dates) else None
        if following is None or (following - visit).days > window + 1:
            gaps.append(visit + datetime.timedelta(days=window + 1))
    return gaps


def headcount(people, day, window):
    return sum(1 for dates in people.values() if is_active(dates, day, window))


# --- A period ---------------------------------------------------------------------------------


def _rate(numerator, denominator):
    """A percentage to one decimal place, or None ("n/a") when dividing by 0."""
    if not denominator:
        return None
    value = Decimal(numerator) * 100 / Decimal(denominator)
    return value.quantize(Decimal("0.1"), ROUND_HALF_UP)


@dataclass
class Retention:
    start: datetime.date
    end: datetime.date            # the period's end
    measured_to: datetime.date    # min(end, today)
    window: int
    S: int = 0
    E: int = 0
    N: int = 0
    R: int = 0
    returned: int = 0
    new_still_active: int = 0
    separations: int = 0
    joined: int = 0               # = N
    engaged: int = 0
    not_engaged: int = 0
    maturing: int = 0
    future: bool = False          # the period hasn't started yet
    people: dict = field(default_factory=dict, repr=False)

    @property
    def truncated(self):
        return self.measured_to < self.end

    @property
    def average_headcount(self):
        return Decimal(self.S + self.E) / 2

    @property
    def retention_focus(self):
        return None if self.future else _rate(self.E - self.N, self.S)

    @property
    def retained_rate(self):
        return None if self.future else _rate(self.R, self.S)

    @property
    def turnover(self):
        return None if self.future else _rate(self.separations, self.average_headcount)

    @property
    def engagement(self):
        """n/a while anyone who joined is still within her W days.

        Found with made-up data (Phase 9): leaving the still-maturing out of
        a rate that is shown anyway biases it upward -- a mother who already
        came back counts as engaged, one who hasn't yet is left out -- so
        every recent month read close to 100%. The rate waits until the
        whole group's W days are over; the counts so far are shown."""
        if self.maturing:
            return None
        return _rate(self.engaged, self.engaged + self.not_engaged)


def measure(start, end, window=None, today=None, people=None):
    """Everything above for one period, from the visits."""
    window = window or window_days()
    today = today or clock.local_today()
    people = visits_by_person() if people is None else people
    measured_to = min(end, today)
    result = Retention(start=start, end=end, measured_to=measured_to, window=window, people=people)
    if start > today:
        result.future = True
        return result

    for dates in people.values():
        joined = dates[0]
        at_start = is_active(dates, start, window)
        at_end = is_active(dates, measured_to, window)
        joined_inside = start <= joined <= end

        result.S += at_start
        result.E += at_end
        if joined_inside:
            result.N += 1
        if at_start and at_end:
            result.R += 1
        elif at_end and joined < start:
            result.returned += 1
        elif at_end:
            result.new_still_active += 1

        result.separations += sum(1 for day in separation_dates(dates, window)
                                  if start <= day <= measured_to)

        if joined_inside:
            window_end = joined + datetime.timedelta(days=window)
            came_back = any(joined < visit <= window_end for visit in dates)
            if came_back:
                result.engaged += 1
            elif window_end < today:
                result.not_engaged += 1
            else:
                result.maturing += 1

    result.joined = result.N
    return result


def distinct_people_with_visits(start, end):
    """# of unique clients served: different people with a visit in the
    period. A merged number is one person."""
    return sum(1 for dates in visits_by_person().values()
               if any(start <= day <= end for day in dates))
