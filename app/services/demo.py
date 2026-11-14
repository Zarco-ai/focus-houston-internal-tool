
import datetime
import random
from dataclasses import dataclass, field
from decimal import Decimal

from app.extensions import db
from app.models import Item, Participant, ServiceType
from app.services import (
    batches,
    card_numbers,
    clock,
    form_review,
    form_sync,
    intake,
    participants,
)

EVENTS = ("Diaper Distribution", "ESL class", "Nutrition Class", "Coffee Social", "Walk-in")
REASONS = ("Diapers for the baby", "Classes", "Friends here", "Help with the baby",
           "Clothes", "Community")

PERSONAS = (
    ("regular", 0.35),
    ("drifter", 0.25),
    ("one_time", 0.20),
    ("returner", 0.15),
    ("burst", 0.05),
)


class DemoError(Exception):
    pass


@dataclass
class Mother:
    code: str
    persona: str
    intake: datetime.date
    visits: list = field(default_factory=list)


def _gap(rng, low, high):
    return datetime.timedelta(days=rng.randint(low, high))


def plan_mothers(rng, count, start, end):
    codes = card_numbers.generate(count + count // 10 + 20, rng=rng)
    mothers = []
    names, weights = zip(*PERSONAS)
    for index in range(count):
        persona = rng.choices(names, weights)[0]
        joined = start + datetime.timedelta(days=rng.randint(0, (end - start).days))
        day, visits = joined, []
        if persona == "regular":
            while True:
                day += _gap(rng, 7, 35)
                if day > end or rng.random() < 0.04:
                    break
                visits.append(day)
        elif persona == "drifter":
            for low, high in ((10, 25), (25, 45), (40, 70), (60, 120)):
                day += _gap(rng, low, high)
                if day > end or rng.random() < 0.25:
                    break
                visits.append(day)
        elif persona == "returner":
            for _ in range(rng.randint(2, 4)):
                day += _gap(rng, 10, 30)
                visits.append(day)
            day += _gap(rng, 90, 200)
            for _ in range(rng.randint(1, 4)):
                visits.append(day)
                day += _gap(rng, 10, 30)
        elif persona == "burst":
            for _ in range(rng.randint(3, 6)):
                day += _gap(rng, 1, 6)
                visits.append(day)
        mothers.append(Mother(codes[index], persona, joined,
                              [v for v in visits if v <= end]))
    spare = codes[count:]
    return mothers, spare


def _serial(local):
    return (local - form_sync.SHEETS_EPOCH).total_seconds() / 86400


def _typo(rng, code):
    position = rng.randrange(5)
    digit = str((int(code[position]) + rng.randint(1, 9)) % 10)
    return code[:position] + digit + code[position + 1:]


def form_rows(rng, mothers, spare_codes):
    rows = []
    for mother in mothers:
        for visit in mother.visits:
            local = datetime.datetime.combine(visit, datetime.time(rng.randint(9, 18), rng.randint(0, 59)))
            typed = mother.code
            if rng.random() < 0.05:
                typed = _typo(rng, mother.code)
            returning = "Yes" if rng.random() < 0.97 else "No"
            row = [_serial(local), typed, returning, rng.choice(EVENTS), rng.choice(REASONS),
                   rng.randint(5, 10), "Kind people"]
            rows.append(row)
            if rng.random() < 0.02:
                again = list(row)
                again[0] = _serial(local + datetime.timedelta(minutes=1))
                rows.append(again)
    for code in spare_codes[:5]:
        rows.append([_serial(datetime.datetime(2026, 9, 1, 11)), code, "No", "Walk-in",
                     "First time", 8, "Heard about it"])
    rows.sort(key=lambda r: r[0])
    return rows


class _DemoSheet:

    HEADERS = ["Timestamp", "What's your ID?", "Are you a returning mother?",
               "What event are you here for?", "What is your reason for joining us today?",
               "How likely are you to come back to join the overall program on a scale from 1–10?",
               "Why?"]

    def __init__(self, rows):
        self.rows = rows

    def fetch(self):
        return form_sync.SheetData(headers=self.HEADERS, rows=self.rows, timezone="America/Chicago")


def _intake_minutes(rng):
    return min(180, max(15, int(round(rng.gauss(45, 15) / 15)) * 15))


def load(count=300, seed=2026, start=datetime.date(2025, 1, 1), end=None, admin_id=None,
         progress=lambda message: None):
    if Participant.query.count():
        raise DemoError("This database already has participants. Use an empty one.")
    end = end or (clock.local_today() - datetime.timedelta(days=1))
    rng = random.Random(seed)  # nosec B311 -- repeatable made-up data, not security; real card numbers use secrets
    mothers, spare = plan_mothers(rng, count, start, end)

    all_codes = [m.code for m in mothers] + spare
    participants.load_card_list("list_position,card_number,batch\n" + "".join(
        f"{i},{c},1\n" for i, c in enumerate(all_codes, 1)))
    for label in ("First pregnancy", "Has other children", "Recently moved to Houston"):
        intake.add_quality(label)
    qualities = intake.active_qualities()
    db.session.commit()
    progress(f"{len(all_codes)} card numbers loaded.")

    forgotten = 0
    for number, mother in enumerate(sorted(mothers, key=lambda m: m.intake), start=1):
        ticked = [str(q.id) for q in qualities if rng.random() < 0.3]
        if rng.random() < 0.03:
            begin = datetime.datetime.combine(mother.intake, datetime.time(10), tzinfo=clock.HOUSTON)
            with clock.freeze(begin):
                session = intake.start()
            with clock.freeze(begin + datetime.timedelta(hours=9)):
                intake.save_survey(mother.code, survey_date=mother.intake.isoformat(),
                                   session=session, quality_ids=ticked,
                                   reason=rng.choice(REASONS), score=str(rng.randint(5, 10)))
            forgotten += 1
        else:
            intake.save_survey(mother.code, survey_date=mother.intake.isoformat(),
                               slider_minutes=str(_intake_minutes(rng)), quality_ids=ticked,
                               reason=rng.choice(REASONS), score=str(rng.randint(5, 10)))
        if number % 100 == 0:
            db.session.commit()
            progress(f"{number} intakes.")
    db.session.commit()

    rows = form_rows(rng, mothers, spare)
    run = form_sync.run(_DemoSheet(rows), triggered_by_id=admin_id)
    progress(f"{run.rows_imported} form responses synced, {run.rows_flagged} to review.")
    for event in EVENTS:
        service = ServiceType.query.filter(db.func.lower(ServiceType.name) == event.lower()).first()
        if service is not None:
            form_review.map_event(event, service.id)
    db.session.commit()

    batch_count = _distributions(rng, start, end)
    progress(f"{batch_count} approved batches.")
    return {
        "mothers": len(mothers),
        "personas": {name: sum(1 for m in mothers if m.persona == name) for name, _ in PERSONAS},
        "form_rows": len(rows),
        "imported": run.rows_imported,
        "flagged": run.rows_flagged,
        "forgotten_timers": forgotten,
        "batches": batch_count,
    }


def _distributions(rng, start, end):
    fixed = [i for i in Item.query.filter_by(active=True, price_entry="fixed").all()
             if i.current_price() is not None]
    diaper = next(i for i in fixed if i.report_bucket == "diapers")
    service = ServiceType.query.filter_by(name="Diaper Distribution").one()
    count = 0
    day = start + datetime.timedelta(days=(7 - start.weekday()) % 7)
    while day <= end:
        batch = batches.open_batch(date=day, service_type_id=service.id)
        batches.set_line(batch, diaper, quantity=rng.randint(150, 600))
        for item in rng.sample(fixed, 6):
            if item.id != diaper.id:
                batches.set_line(batch, item, quantity=rng.randint(1, 12),
                                 condition=rng.choice(("new", "new", "used")))
        batches.set_attendance(batch, {
            "individuals_served": rng.randint(5, 25), "children_served": rng.randint(5, 35),
            "volunteers_total": rng.randint(2, 8), "volunteer_hours_tenths": rng.randint(20, 160),
            "unique_clients_event_count": rng.randint(3, 20),
            "meals_value": Decimal(rng.randint(0, 80)),
        })
        batches.commit_batch(batch)
        count += 1
        day += datetime.timedelta(days=7)
    return count
