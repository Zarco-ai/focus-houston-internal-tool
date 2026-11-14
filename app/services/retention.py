
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


def visits_by_person():
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
    gaps = []
    for index, visit in enumerate(dates):
        following = dates[index + 1] if index + 1 < len(dates) else None
        if following is None or (following - visit).days > window + 1:
            gaps.append(visit + datetime.timedelta(days=window + 1))
    return gaps


def headcount(people, day, window):
    return sum(1 for dates in people.values() if is_active(dates, day, window))


def _rate(numerator, denominator):
    if not denominator:
        return None
    value = Decimal(numerator) * 100 / Decimal(denominator)
    return value.quantize(Decimal("0.1"), ROUND_HALF_UP)


@dataclass
class Retention:
    start: datetime.date
    end: datetime.date
    measured_to: datetime.date
    window: int
    S: int = 0
    E: int = 0
    N: int = 0
    R: int = 0
    returned: int = 0
    new_still_active: int = 0
    separations: int = 0
    joined: int = 0
    engaged: int = 0
    not_engaged: int = 0
    maturing: int = 0
    future: bool = False
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
        if self.maturing:
            return None
        return _rate(self.engaged, self.engaged + self.not_engaged)


def measure(start, end, window=None, today=None, people=None):
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
    return sum(1 for dates in visits_by_person().values()
               if any(start <= day <= end for day in dates))
