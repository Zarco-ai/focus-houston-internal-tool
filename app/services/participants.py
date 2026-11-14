
import csv
import io

import sqlalchemy as sa

from app.extensions import db
from app.models import CardNumber, Participant, ParticipantVisit
from app.models.audit import ACTION_CREATE, ACTION_IMPORT
from app.services import audit, card_numbers

CARD_CSV_COLUMNS = ("list_position", "card_number", "batch")
MAX_CARDS_PER_FILE = card_numbers.POSSIBLE_CODES


class CardListError(Exception):
    pass


def read_card_csv(text):
    reader = csv.DictReader(io.StringIO(text))
    missing = set(CARD_CSV_COLUMNS) - set(reader.fieldnames or ())
    if missing:
        raise CardListError(
            f"This isn't a card list from generate_cards.py: no {', '.join(sorted(missing))} column."
        )

    rows, seen_codes, batches = [], set(), set()
    for row_number, row in enumerate(reader, start=2):
        code = card_numbers.normalize(row.get("card_number"))
        if not card_numbers.is_valid(code):
            raise CardListError(f"Row {row_number}: '{row.get('card_number')}' isn't a valid card number.")
        try:
            batch = int(row["batch"])
            position = int(row["list_position"])
        except (TypeError, ValueError):
            raise CardListError(f"Row {row_number}: batch and list_position must be whole numbers.")
        if batch < 1 or position < 1:
            raise CardListError(f"Row {row_number}: batch and list_position start at 1.")
        if code in seen_codes:
            raise CardListError(f"Row {row_number}: card number {code} is in this file twice.")
        seen_codes.add(code)
        batches.add(batch)
        rows.append((batch, position, code))
        if len(rows) > MAX_CARDS_PER_FILE:
            raise CardListError("This file has more numbers than the format allows.")

    if not rows:
        raise CardListError("The file has no card numbers in it.")
    if len(batches) != 1:
        raise CardListError(f"One batch per file, please; this one has batches {sorted(batches)}.")
    positions = sorted(position for _, position, _ in rows)
    if positions != list(range(1, len(rows) + 1)):
        raise CardListError("list_position must run 1, 2, 3... with no gaps or repeats.")
    return rows


def load_card_list(text):
    rows = read_card_csv(text)
    batch = rows[0][0]
    codes = [code for _, _, code in rows]

    existing = {card.code: card for card in CardNumber.query.filter(CardNumber.code.in_(codes))}
    in_batch = CardNumber.query.filter_by(batch=batch).count()

    if existing and len(existing) == len(rows) == in_batch:
        mismatched = [c for b, p, c in rows
                      if (existing[c].batch, existing[c].list_position) != (b, p)]
        if not mismatched:
            return 0

    if in_batch:
        raise CardListError(
            f"Batch {batch} is already loaded and this file doesn't match it. "
            "A batch is never changed once loaded; generate a new batch instead."
        )
    if existing:
        clash = sorted(existing)[0]
        raise CardListError(
            f"Card number {clash} is already loaded in batch {existing[clash].batch}. "
            "Nothing was loaded."
        )

    for batch_number, position, code in rows:
        db.session.add(CardNumber(code=code, batch=batch_number, list_position=position))
    db.session.flush()
    audit.record_event(
        action=ACTION_IMPORT, table_name="card_number", record_id=batch,
        description=f"Loaded card batch {batch}: {len(rows)} numbers",
    )
    return len(rows)


def _unused():
    return (
        CardNumber.query.outerjoin(Participant, Participant.code == CardNumber.code)
        .filter(Participant.id.is_(None))
    )


def next_unused_code():
    card = _unused().order_by(CardNumber.batch, CardNumber.list_position).first()
    return card.code if card else None


def card_list_status():
    loaded = CardNumber.query.count()
    assigned = Participant.query.count()
    return {
        "loaded": loaded,
        "assigned": assigned,
        "unused": loaded - assigned,
        "batches": [b for (b,) in db.session.query(CardNumber.batch).distinct().order_by(CardNumber.batch)],
        "next": next_unused_code(),
    }


def available_code(raw):
    code = card_numbers.normalize(raw)
    if not card_numbers.is_valid(code):
        raise ValueError(
            f"'{raw}' isn't a card number. Card numbers are 5 digits; check the "
            "number on the card and the sheet."
        )
    card = db.session.get(CardNumber, code)
    if card is None:
        raise ValueError(
            f"{code} isn't on any card list loaded into the app. Check the "
            "printed sheet, or load its batch under Settings → Card numbers."
        )
    if card.participant is not None:
        raise ValueError(
            f"{code} has already been given to someone. Use the next unused "
            "number on the sheet."
        )
    return code


def create_participant(code, created_by_id, via="intake survey"):
    participant = Participant(code=code, created_by_id=created_by_id)
    db.session.add(participant)
    db.session.flush()
    audit.record_change(
        table_name="participant", record_id=participant.id, field="created",
        old_value=None, new_value=via, action=ACTION_CREATE,
        description=f"Participant {participant.id} created",
    )
    return participant


def add_visit(participant, visit_date, source, source_ref, service_type_id=None):
    visit = ParticipantVisit(
        participant=participant, visit_date=visit_date, source=source,
        source_ref=str(source_ref), service_type_id=service_type_id,
    )
    db.session.add(visit)
    db.session.flush()
    return visit


def find_by_code(raw):
    code = card_numbers.normalize(raw)
    return Participant.query.filter_by(code=code).one_or_none() if code else None


def participant_count():
    return db.session.scalar(sa.select(sa.func.count(Participant.id)))
