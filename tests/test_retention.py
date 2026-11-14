
import datetime
import itertools
import random
from decimal import Decimal

import pytest

from app.extensions import db
from app.models import AuditLog
from app.models.participant import VISIT_GOOGLE_FORM, VISIT_INTAKE
from app.services import card_numbers, clock, form_review, intake, metrics, participants, retention

D = datetime.date
_refs = itertools.count(1)


@pytest.fixture
def mom(seeded_app, admin_user):
    codes = iter(card_numbers.generate(400, rng=random.Random(11)))
    loaded = []

    def make(*dates):
        code = next(codes)
        participants.load_card_list(f"list_position,card_number,batch\n1,{code},{len(loaded) + 1}\n")
        loaded.append(code)
        person = participants.create_participant(code, created_by_id=admin_user.id)
        for index, day in enumerate(dates):
            source = VISIT_INTAKE if index == 0 else VISIT_GOOGLE_FORM
            participants.add_visit(person, day, source, f"t{next(_refs)}")
        return person

    return make


def measure(start, end, today=D(2026, 9, 1), window=60):
    return retention.measure(start, end, window=window, today=today)


JANUARY = (D(2026, 1, 1), D(2026, 1, 31))


def test_ana_is_active_exactly_60_days_after_a_visit_and_not_61(mom):
    mom(D(2026, 1, 1))
    people = retention.visits_by_person()
    [dates] = people.values()

    assert retention.is_active(dates, D(2026, 3, 2), 60)
    assert not retention.is_active(dates, D(2026, 3, 3), 60)
    assert retention.separation_dates(dates, 60) == [D(2026, 3, 3)]


def test_a_visit_after_the_day_doesnt_make_her_active_on_it(mom):
    mom(D(2026, 5, 1))
    [dates] = retention.visits_by_person().values()
    assert not retention.is_active(dates, D(2026, 4, 30), 60)


@pytest.fixture
def worked_example(mom):
    for _ in range(8):
        mom(D(2025, 12, 15), D(2026, 1, 20))
    for _ in range(2):
        mom(D(2025, 11, 5))
    for _ in range(3):
        mom(D(2025, 6, 1), D(2025, 9, 1), D(2026, 1, 15))
    for _ in range(3):
        mom(D(2026, 1, 10), D(2026, 2, 1))
    mom(D(2026, 1, 10))
    db.session.commit()


def test_the_worked_example_counts(worked_example):
    r = measure(*JANUARY)
    assert (r.S, r.E, r.N) == (10, 15, 4)
    assert (r.R, r.returned, r.new_still_active) == (8, 3, 4)
    assert r.separations == 2


def test_the_two_retention_rates_differ_by_exactly_the_returners(worked_example):
    r = measure(*JANUARY)
    assert r.retention_focus == Decimal("110.0")
    assert r.retained_rate == Decimal("80.0")


def test_turnover_and_engagement_in_the_worked_example(worked_example):
    r = measure(*JANUARY)
    assert r.average_headcount == Decimal("12.5")
    assert r.turnover == Decimal("16.0")
    assert (r.engaged, r.not_engaged, r.maturing) == (3, 1, 0)
    assert r.engagement == Decimal("75.0")


def test_nobody_active_at_the_start_is_n_a_not_zero(mom):
    mom(D(2026, 1, 10))
    r = measure(*JANUARY)
    assert (r.S, r.E, r.N) == (0, 1, 1)
    assert r.retention_focus is None and r.retained_rate is None
    assert r.turnover == Decimal("0.0")


def test_an_empty_program_is_n_a_everywhere(seeded_app):
    r = measure(*JANUARY)
    assert (r.retention_focus, r.retained_rate, r.turnover, r.engagement) == (None,) * 4


def test_a_separation_stays_in_its_month_when_she_comes_back(mom):
    mom(D(2026, 1, 1), D(2026, 4, 3))
    march = measure(D(2026, 3, 1), D(2026, 3, 31))
    april = measure(D(2026, 4, 1), D(2026, 4, 30))

    assert (march.S, march.E, march.separations) == (1, 0, 1)
    assert (april.S, april.E, april.returned, april.separations) == (0, 1, 1, 0)


def test_a_separation_that_hasnt_happened_yet_isnt_counted(mom):
    mom(D(2026, 1, 1))
    r = measure(D(2026, 3, 1), D(2026, 3, 31), today=D(2026, 3, 1))
    assert r.truncated and r.measured_to == D(2026, 3, 1)
    assert (r.E, r.separations) == (1, 0)


def test_a_period_that_hasnt_started_is_n_a(mom):
    mom(D(2026, 1, 1))
    r = measure(D(2026, 12, 1), D(2026, 12, 31), today=D(2026, 9, 1))
    assert r.future and r.retention_focus is None and r.turnover is None


def test_engagement_counts_a_return_after_the_period_ends(mom):
    mom(D(2026, 1, 28), D(2026, 2, 20))
    assert measure(*JANUARY).engaged == 1


def test_coming_back_on_day_61_isnt_engagement(mom):
    mom(D(2026, 1, 10), D(2026, 3, 12))
    r = measure(*JANUARY)
    assert (r.engaged, r.not_engaged) == (0, 1)


def test_the_same_day_isnt_coming_back(mom):
    fay = mom(D(2026, 1, 10))
    participants.add_visit(fay, D(2026, 1, 10), VISIT_GOOGLE_FORM, "same-day")
    assert measure(*JANUARY).engaged == 0


def test_a_mother_whose_window_is_still_open_is_left_out_until_it_closes(mom):
    mom(D(2026, 8, 20))
    r = measure(D(2026, 8, 1), D(2026, 8, 31), today=D(2026, 9, 1))
    assert (r.engaged, r.not_engaged, r.maturing) == (0, 0, 1)
    assert r.engagement is None


def test_a_merged_card_is_one_mother(mom):
    old = mom(D(2025, 12, 1))
    new = mom(D(2026, 1, 20))
    form_review.merge(old.code, new.code)

    r = measure(*JANUARY)
    assert (r.S, r.E, r.N, r.R) == (1, 1, 0, 1)
    assert retention.distinct_people_with_visits(*JANUARY) == 1


def test_e_is_always_retained_plus_returned_plus_new(mom):
    rng = random.Random(2026)
    base = D(2025, 1, 1)
    for _ in range(40):
        days = sorted(rng.sample(range(0, 600), rng.randint(1, 6)))
        mom(*[base + datetime.timedelta(days=d) for d in days])
    db.session.commit()

    for _ in range(30):
        start = base + datetime.timedelta(days=rng.randint(0, 600))
        end = start + datetime.timedelta(days=rng.randint(0, 120))
        r = retention.measure(start, end, window=60, today=D(2027, 1, 1))
        assert r.E == r.R + r.returned + r.new_still_active, (start, end)
        assert r.S >= r.R
        assert r.engaged + r.not_engaged + r.maturing == r.N


def test_the_window_is_a_setting_and_its_change_is_audited(mom):
    mom(D(2026, 1, 1))
    retention.set_window_days(30)
    db.session.commit()
    [dates] = retention.visits_by_person().values()

    assert retention.window_days() == 30
    assert retention.separation_dates(dates, retention.window_days()) == [D(2026, 2, 1)]
    assert AuditLog.query.filter_by(field="activity_window_days").one().new_value == "30"
    with pytest.raises(ValueError):
        retention.set_window_days(3)


def _timed_intake(code, minutes, day):
    survey = intake.save_survey(code, survey_date=day.isoformat(), slider_minutes=str(minutes))
    return survey


def test_intake_times_mean_and_median(seeded_app, admin_user):
    codes = card_numbers.generate(5, rng=random.Random(12))
    participants.load_card_list("list_position,card_number,batch\n" + "".join(
        f"{i},{c},1\n" for i, c in enumerate(codes, 1)))
    for code, minutes in zip(codes, (30, 45, 45, 60, 180)):
        _timed_intake(code, minutes, D(2026, 3, 10))
    db.session.commit()

    values = metrics.values(D(2026, 3, 1), D(2026, 3, 31))
    assert values["intakes_timed"] == 5
    assert values["intake_hours_total"] == Decimal("6.0")
    assert values["intake_minutes_mean"] == Decimal("72.0")
    assert values["intake_minutes_median"] == Decimal("45.0")


def test_an_intake_waiting_to_be_checked_isnt_counted(seeded_app, admin_user):
    codes = card_numbers.generate(1, rng=random.Random(13))
    participants.load_card_list(f"list_position,card_number,batch\n1,{codes[0]},1\n")
    with clock.freeze(datetime.datetime(2026, 3, 10, 8, tzinfo=clock.HOUSTON)):
        session = intake.start()
    with clock.freeze(datetime.datetime(2026, 3, 10, 18, tzinfo=clock.HOUSTON)):
        intake.save_survey(codes[0], survey_date="2026-03-10", session=session)
    db.session.commit()

    period = metrics.Period(D(2026, 3, 1), D(2026, 3, 31))
    assert period.intake_minutes == []
    assert "1 more waiting to be checked" in metrics._intake_detail(period)


def test_unique_clients_is_a_true_count_beside_the_event_sum(mom):
    mom(D(2026, 1, 5), D(2026, 1, 19))
    mom(D(2026, 1, 12))
    values = metrics.values(*JANUARY)
    assert values["unique_clients_distinct"] == 2


def test_totals_shows_program_health_with_its_counts(client, mom):
    mom(D(2025, 12, 15), D(2026, 1, 20))
    mom(D(2025, 11, 5))
    db.session.commit()
    page = client.get("/totals", query_string={"month": "2026-01"}).get_data(as_text=True)

    assert "PROGRAM HEALTH" in page
    assert "1 of S 2 active at both ends" in page
    assert "50.0%" in page
    assert "Retention (FOCUS formula)" not in page
    assert "n/a" in page


def test_the_settings_page_changes_the_window(client, seeded_app):
    client.post("/admin/settings", data={"activity_window_days": "45"})
    assert retention.window_days() == 45
    reply = client.post("/admin/settings", data={"activity_window_days": "500"}, follow_redirects=True)
    assert "7 to 365" in reply.get_data(as_text=True)


def test_engagement_waits_until_everyones_window_is_over(mom):
    mom(D(2026, 8, 3), D(2026, 8, 20))
    mom(D(2026, 8, 25))
    r = measure(D(2026, 8, 1), D(2026, 8, 31), today=D(2026, 9, 1))
    assert (r.engaged, r.maturing) == (1, 1)
    assert r.engagement is None
    later = measure(D(2026, 8, 1), D(2026, 8, 31), today=D(2026, 11, 1))
    assert later.engagement == Decimal("50.0")


def test_coming_back_on_the_day_she_would_have_lapsed_is_not_a_separation(mom):
    mom(D(2026, 1, 1), D(2026, 3, 3))
    [dates] = retention.visits_by_person().values()
    assert retention.separation_dates(dates, 60) == [D(2026, 5, 3)]
    assert measure(D(2026, 3, 1), D(2026, 3, 31)).separations == 0
    assert retention.separation_dates([D(2026, 1, 1), D(2026, 3, 4)], 60)[0] == D(2026, 3, 3)
