"""Matching form responses to mothers, and the review queue.

A response whose card number belongs to a participant is a visit, recorded
automatically. Everything else waits for a person -- the app never creates a
mother from a form response on its own, because a typo would make a fake one.

Why a response needs review is COMPUTED each time, from what it holds and
what the app knows now: a participant created later, or a label mapped
later, changes the answer without anyone editing the response. Only a
person's decision is stored (decision, reviewed_by, reviewed_at).

The three kinds of unknown number need different fixes, so the queue says
which one it is (PRODUCTION_DESIGN 7.5):

  invalid      fails the check digit -- almost always a typo. Match or discard.
  unassigned   on a card list, but no participant yet -- she was handed a
               card and her intake wasn't entered. Create her, match, discard.
  not on list  well-formed, but on no card list. Match or discard.

Also listed, though their visit is already recorded: a known number that
answered "returning: no" (a borrowed card?), and a likely double
submission (the same number within two minutes).

Merging two numbers (a lost card) lives here too: it is how a reviewer
fixes the "she has two numbers" case.
"""

import datetime
from dataclasses import dataclass

import sqlalchemy as sa

from app.extensions import db
from app.models import (
    CardNumber,
    FormEventMapping,
    FormResponse,
    Participant,
    ParticipantVisit,
    ServiceType,
)
from app.models.form import (
    DECISION_AUTO,
    DECISION_CREATED,
    DECISION_DISCARDED,
    DECISION_MATCHED,
    LABEL_MAX,
)
from app.models.participant import VISIT_GOOGLE_FORM
from app.services import audit, card_numbers, clock, participants

DUPLICATE_WINDOW = datetime.timedelta(minutes=2)

KIND_INVALID = "invalid"
KIND_UNASSIGNED = "unassigned"
KIND_NOT_ON_LIST = "not_on_list"
KIND_RETURNING_NO = "returning_no"
KIND_DUPLICATE = "duplicate"

KIND_TEXT = {
    KIND_INVALID: "Not a valid card number — probably a typo.",
    KIND_UNASSIGNED: "On a card list, but nobody has this number in the app yet. "
                     "She may have been given the card without her intake being entered.",
    KIND_NOT_ON_LIST: "Looks like a card number, but it isn't on any card list.",
    KIND_RETURNING_NO: "Answered “not a returning mother”, but this card is already hers. "
                       "The visit is recorded. A borrowed card?",
    KIND_DUPLICATE: "Same card number within two minutes of another response — "
                    "probably sent twice. The visit is recorded.",
}


class ReviewError(Exception):
    """A review action can't be done. The message says why."""


def _actor():
    user_id = audit.current_actor.get()
    if user_id is None:
        raise audit.NoActor("Reviewing a response needs a signed-in person.")
    return user_id


def tidy_label(label):
    return " ".join((label or "").split())[:LABEL_MAX]


def code_of(response):
    return card_numbers.normalize(response.raw_id_text)


# --- Visits -----------------------------------------------------------------------------


def _service_type_id_for(label):
    label = tidy_label(label)
    if not label:
        return None
    mapping = FormEventMapping.query.filter(
        sa.func.lower(FormEventMapping.label) == label.lower()).one_or_none()
    return mapping.service_type_id if mapping else None


def visit_for(response):
    return ParticipantVisit.query.filter_by(
        source=VISIT_GOOGLE_FORM, source_ref=str(response.id)).one_or_none()


def _record_visit(response, participant, decision):
    participants.add_visit(participant, response.submitted_local_date, VISIT_GOOGLE_FORM,
                           response.id, service_type_id=_service_type_id_for(response.raw_event_label))
    response.participant_id = participant.id
    response.decision = decision


def _known_participant(response):
    code = code_of(response)
    if not card_numbers.is_valid(code):
        return None
    return Participant.query.filter_by(code=code).one_or_none()


def match_on_import(response):
    """Called by the sync for each new response: a known number is a visit."""
    participant = _known_participant(response)
    if participant is not None:
        _record_visit(response, participant, DECISION_AUTO)
        db.session.flush()


def match_waiting_responses():
    """Waiting responses whose number has since been given to someone (her
    intake was entered after she filled in the form) become visits."""
    matched = 0
    for response in FormResponse.query.filter(FormResponse.decision.is_(None)):
        participant = _known_participant(response)
        if participant is not None:
            _record_visit(response, participant, DECISION_AUTO)
            matched += 1
    db.session.flush()
    return matched


# --- Why a response is in the queue -------------------------------------------------------


class _Lookups:
    """What reasons() needs to know, fetched in a few queries for a whole
    list of responses instead of several queries per response.

    Found by the Phase 9 stress test: with 903 responses to review, asking
    the database about each one separately (is it on the list? is there an
    earlier one within two minutes?) took the Responses page 10 seconds.
    """

    def __init__(self, responses):
        self.listed = set()
        self.nearby = {}
        if not responses:
            return
        codes = {code_of(r) for r in responses if code_of(r)}
        self.listed = {c for (c,) in db.session.query(CardNumber.code)
                       .filter(CardNumber.code.in_(codes))}
        earliest = min(r.submitted_at for r in responses) - DUPLICATE_WINDOW
        latest = max(r.submitted_at for r in responses) + DUPLICATE_WINDOW
        for other_id, submitted_at, raw_id, decision in db.session.query(
                FormResponse.id, FormResponse.submitted_at, FormResponse.raw_id_text,
                FormResponse.decision).filter(FormResponse.submitted_at.between(earliest, latest)):
            if decision != DECISION_DISCARDED:
                code = card_numbers.normalize(raw_id)
                if code:
                    self.nearby.setdefault(code, []).append((other_id, submitted_at))

    def on_list(self, code):
        return code in self.listed

    def earlier_within_window(self, response):
        code = code_of(response)
        return any(other_id < response.id and abs(at - response.submitted_at) <= DUPLICATE_WINDOW
                   for other_id, at in self.nearby.get(code, ()))


def unknown_kind(response, lookups=None):
    lookups = lookups or _Lookups([response])
    code = code_of(response)
    if not card_numbers.is_valid(code):
        return KIND_INVALID
    if not lookups.on_list(code):
        return KIND_NOT_ON_LIST
    return KIND_UNASSIGNED


def is_possible_duplicate(response, lookups=None):
    """Another, earlier response with the same number within two minutes.
    The earlier one stands; this one is the suspect."""
    if not code_of(response):
        return False
    return (lookups or _Lookups([response])).earlier_within_window(response)


def reasons(response, lookups=None):
    """Why this response is in front of a person, as kinds (see KIND_TEXT)."""
    if response.decision == DECISION_DISCARDED or response.reviewed_at is not None:
        return []
    lookups = lookups or _Lookups([response])
    found = []
    if response.decision is None:
        found.append(unknown_kind(response, lookups))
    elif response.decision == DECISION_AUTO and response.returning is False:
        found.append(KIND_RETURNING_NO)
    if response.decision in (None, DECISION_AUTO) and is_possible_duplicate(response, lookups):
        found.append(KIND_DUPLICATE)
    return found


def needs_review(response):
    return bool(reasons(response))


@dataclass
class ReviewItem:
    response: FormResponse
    kinds: list

    @property
    def is_waiting(self):
        return self.response.decision is None

    @property
    def can_create(self):
        return KIND_UNASSIGNED in self.kinds

    @property
    def says_returning_but_unknown(self):
        return self.is_waiting and self.response.returning is True


def review_queue():
    """Everything a person needs to look at, oldest first."""
    candidates = FormResponse.query.filter(
        FormResponse.reviewed_at.is_(None),
        sa.or_(FormResponse.decision.is_(None), FormResponse.decision == DECISION_AUTO),
    ).order_by(FormResponse.submitted_at, FormResponse.id).all()
    lookups = _Lookups(candidates)
    items = []
    for response in candidates:
        kinds = reasons(response, lookups)
        if kinds:
            items.append(ReviewItem(response, kinds))
    return items


def recent_responses(limit=30):
    return FormResponse.query.order_by(FormResponse.submitted_at.desc(),
                                       FormResponse.id.desc()).limit(limit).all()


# --- A person's decisions -----------------------------------------------------------------


def _decided(response, old, decision, note):
    """Record who decided, and audit it. `old` is passed in because the
    decision has usually been set on the response by the time this runs."""
    response.reviewed_by_id = _actor()
    response.reviewed_at = clock.now_utc()
    audit.record_change(table_name="form_response", record_id=response.id, field="decision",
                        old_value=old or "waiting", new_value=decision,
                        description=f"Form response {response.id}: {note}")
    db.session.flush()


def match(response, raw_code):
    """She is a participant already; the number on the response was wrong.
    The visit goes on the number the reviewer typed."""
    if response.decision is not None:
        raise ReviewError("This response has already been dealt with.")
    participant = participants.find_by_code(raw_code)
    if participant is None:
        raise ReviewError(f"Nobody in the app has card number '{raw_code}'. Check the number, "
                          "or use Create if this is her new card.")
    _record_visit(response, participant, DECISION_MATCHED)
    _decided(response, None, DECISION_MATCHED, f"matched to participant {participant.id}")
    return participant


def create(response):
    """Make her a participant on the number she typed. Only for a number on
    a card list that nobody has yet."""
    if response.decision is not None:
        raise ReviewError("This response has already been dealt with.")
    try:
        code = participants.available_code(response.raw_id_text)
    except ValueError as error:
        raise ReviewError(str(error))
    participant = participants.create_participant(code, created_by_id=_actor(),
                                                  via="form response")
    _record_visit(response, participant, DECISION_CREATED)
    _decided(response, None, DECISION_CREATED, f"participant {participant.id} created")
    return participant


def discard(response):
    """Not a real visit. If one was recorded automatically, it goes too."""
    if response.decision == DECISION_DISCARDED:
        return
    old = response.decision
    visit = visit_for(response)
    if visit is not None:
        db.session.delete(visit)
    response.participant_id = None
    response.decision = DECISION_DISCARDED
    _decided(response, old, DECISION_DISCARDED, "discarded" + (" and its visit removed" if visit else ""))


def mark_checked(response):
    """Looked at it; the recorded visit stands."""
    if response.decision != DECISION_AUTO:
        raise ReviewError("Only a recorded visit can be marked as checked.")
    response.reviewed_by_id = _actor()
    response.reviewed_at = clock.now_utc()
    audit.record_change(table_name="form_response", record_id=response.id, field="checked",
                        old_value=None, new_value="visit stands",
                        description=f"Form response {response.id} checked")
    db.session.flush()


# --- Event names ----------------------------------------------------------------------------


@dataclass
class EventLabel:
    label: str
    responses: int
    mapping: object


def event_labels():
    """Every event answer the form has sent, with how many responses gave
    it and what it is mapped to. Unmapped first."""
    counts = {}
    for (label,) in db.session.query(FormResponse.raw_event_label).filter(
            FormResponse.raw_event_label.isnot(None)):
        tidied = tidy_label(label)
        key = tidied.lower()
        shown, count = counts.get(key, (tidied, 0))
        counts[key] = (shown, count + 1)
    mappings = {m.label.lower(): m for m in FormEventMapping.query.all()}
    for key, mapping in mappings.items():
        counts.setdefault(key, (mapping.label, 0))
    labels = [EventLabel(shown, count, mappings.get(key)) for key, (shown, count) in counts.items()]
    return sorted(labels, key=lambda e: (e.mapping is not None, e.label.lower()))


def unmapped_label_count():
    return sum(1 for e in event_labels() if e.mapping is None and e.responses)


def map_event(label, service_type_id):
    """Map an event answer to a service type (None removes the mapping).
    Every visit from a response with that answer, past ones included,
    picks it up."""
    label = tidy_label(label)
    if not label:
        raise ReviewError("An event name is needed.")
    if service_type_id is not None and db.session.get(ServiceType, service_type_id) is None:
        raise ReviewError("That service type doesn't exist.")

    mapping = FormEventMapping.query.filter(
        sa.func.lower(FormEventMapping.label) == label.lower()).one_or_none()
    old = mapping.service_type.name if mapping else None
    if service_type_id is None:
        if mapping is not None:
            db.session.delete(mapping)
    elif mapping is None:
        mapping = FormEventMapping(label=label, service_type_id=service_type_id)
        db.session.add(mapping)
    else:
        mapping.service_type_id = service_type_id
    db.session.flush()

    new = db.session.get(ServiceType, service_type_id).name if service_type_id else None
    if old != new:
        audit.record_change(table_name="form_event_mapping", record_id=mapping.id if mapping else 0,
                            field="service_type", old_value=old, new_value=new,
                            description=f"Form event “{label}”")

    # Re-label the visits that came from responses with this answer.
    response_ids = [str(r.id) for r in FormResponse.query.filter(
        sa.func.lower(sa.func.trim(FormResponse.raw_event_label)) == label.lower())]
    if response_ids:
        ParticipantVisit.query.filter(
            ParticipantVisit.source == VISIT_GOOGLE_FORM,
            ParticipantVisit.source_ref.in_(response_ids),
        ).update({ParticipantVisit.service_type_id: service_type_id}, synchronize_session=False)
    db.session.flush()
    return len(response_ids)


# --- Merging two numbers (a lost card) ---------------------------------------------------------


def survivor(participant):
    """The participant a visit counts toward: herself, or who she was merged into."""
    if participant is not None and participant.merged_into_id is not None:
        return db.session.get(Participant, participant.merged_into_id)
    return participant


def merge(retiring_code, surviving_code):
    """She has two numbers; keep `surviving_code`. Visits are not rewritten:
    every count resolves a participant through merged_into_id, so the
    merge is visible and can be undone. Kept one level deep."""
    retiring = participants.find_by_code(retiring_code)
    keeping = participants.find_by_code(surviving_code)
    if retiring is None or keeping is None:
        raise ReviewError("Both numbers must belong to someone in the app.")
    if retiring.id == keeping.id:
        raise ReviewError("Those are the same number.")
    if keeping.merged_into_id is not None:
        raise ReviewError("The number to keep has itself been merged into another. "
                          "Keep that one instead.")
    if retiring.merged_into_id is not None:
        raise ReviewError("The number to retire has already been merged.")

    for earlier in Participant.query.filter_by(merged_into_id=retiring.id):
        audit.apply_change(earlier, "merged_into_id", keeping.id, table_name="participant",
                           description=f"Participant {earlier.id} re-pointed by a merge")
    audit.apply_change(retiring, "merged_into_id", keeping.id, table_name="participant",
                       description=f"Participant {retiring.id} merged into {keeping.id}")
    db.session.flush()
    return retiring, keeping


def unmerge(participant):
    if participant.merged_into_id is None:
        raise ReviewError("That number isn't merged.")
    audit.apply_change(participant, "merged_into_id", None, table_name="participant",
                       description=f"Participant {participant.id} merge undone")
    db.session.flush()


def merges():
    return Participant.query.filter(Participant.merged_into_id.isnot(None)).order_by(Participant.id).all()

