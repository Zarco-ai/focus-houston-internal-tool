"""Card numbers and participants (Phase 5).

The card list is the only link between a mother and the app, so loading it
is checked row by row and all-or-nothing, and a participant can only be
made on a number that is on the list and not yet handed out.
"""

import random

import pytest
from click.testing import CliRunner

from app.extensions import db
from app.models import AuditLog, CardNumber, Participant
from app.services import card_numbers, participants


def card_csv(codes, batch=1, start=1):
    rows = ["list_position,card_number,batch,generated_on"]
    rows += [f"{start + i},{code},{batch},2026-10-20" for i, code in enumerate(codes)]
    return "\n".join(rows) + "\n"


def some_codes(n, seed=1, existing=()):
    return card_numbers.generate(n, existing=existing, rng=random.Random(seed))


# --- Loading the list -----------------------------------------------------------------


def test_a_card_list_loads_in_list_order(app):
    codes = some_codes(5)
    assert participants.load_card_list(card_csv(codes)) == 5

    assert participants.next_unused_code() == codes[0]
    assert CardNumber.query.count() == 5
    assert AuditLog.query.filter_by(table_name="card_number", action="import").count() == 1


def test_loading_the_same_list_twice_changes_nothing(app):
    text = card_csv(some_codes(5))
    participants.load_card_list(text)
    assert participants.load_card_list(text) == 0
    assert CardNumber.query.count() == 5


def test_a_second_batch_is_used_after_the_first(app):
    first = some_codes(2, seed=1)
    second = some_codes(2, seed=2, existing=first)
    participants.load_card_list(card_csv(second, batch=2))
    participants.load_card_list(card_csv(first, batch=1))

    assert participants.next_unused_code() == first[0]


@pytest.mark.parametrize("text, message", [
    ("code,batch\n12345,1\n", "isn't a card list"),
    ("list_position,card_number,batch\n", "no card numbers"),
    ("list_position,card_number,batch\n1,12345,1\n", "isn't a valid card number"),
    ("list_position,card_number,batch\n1,0123,1\n", "isn't a valid card number"),
    ("list_position,card_number,batch\n1,{a},x\n", "whole numbers"),
    ("list_position,card_number,batch\n1,{a},1\n3,{b},1\n", "no gaps"),
    ("list_position,card_number,batch\n1,{a},1\n2,{b},2\n", "One batch per file"),
    ("list_position,card_number,batch\n1,{a},1\n2,{a},1\n", "twice"),
])
def test_a_wrong_list_is_refused_whole(app, text, message):
    a, b = some_codes(2)
    with pytest.raises(participants.CardListError, match=message):
        participants.load_card_list(text.format(a=a, b=b))
    assert CardNumber.query.count() == 0


def test_a_check_digit_typo_in_the_file_is_caught(app):
    good = some_codes(1)[0]
    typo = good[:4] + str((int(good[4]) + 1) % 10)
    with pytest.raises(participants.CardListError, match="isn't a valid card number"):
        participants.load_card_list(card_csv([typo]))


def test_a_loaded_batch_is_never_changed(app):
    codes = some_codes(3)
    participants.load_card_list(card_csv(codes))
    with pytest.raises(participants.CardListError, match="already loaded"):
        participants.load_card_list(card_csv(list(reversed(codes))))


def test_a_number_can_not_be_in_two_batches(app):
    codes = some_codes(3)
    participants.load_card_list(card_csv(codes))
    with pytest.raises(participants.CardListError, match="already loaded in batch 1"):
        participants.load_card_list(card_csv([codes[0]], batch=2))


# --- Handing a number out ------------------------------------------------------------


def test_an_unused_number_on_the_list_is_available(app):
    code = some_codes(1)[0]
    participants.load_card_list(card_csv([code]))
    assert participants.available_code(f" {code[:2]}-{code[2:]} ") == code


def test_a_typo_an_unknown_number_and_a_used_number_are_each_explained(app, admin_user):
    used, spare = some_codes(2)
    participants.load_card_list(card_csv([used]))
    participants.create_participant(used, created_by_id=admin_user.id)

    with pytest.raises(ValueError, match="isn't a card number"):
        participants.available_code("1234")
    with pytest.raises(ValueError, match="isn't on any card list"):
        participants.available_code(spare)
    with pytest.raises(ValueError, match="already been given"):
        participants.available_code(used)


def test_a_participant_must_be_on_the_list_even_past_the_service(app, admin_user):
    """The foreign key is the last line of defence."""
    import sqlalchemy as sa
    db.session.add(Participant(code=some_codes(1)[0], created_by_id=admin_user.id))
    with pytest.raises(sa.exc.IntegrityError):
        db.session.flush()
    db.session.rollback()


def test_next_unused_skips_numbers_given_out(app, admin_user):
    codes = some_codes(3)
    participants.load_card_list(card_csv(codes))
    participants.create_participant(codes[0], created_by_id=admin_user.id)
    assert participants.next_unused_code() == codes[1]
    assert participants.card_list_status()["unused"] == 2


# --- Privacy ---------------------------------------------------------------------------


def test_the_participant_table_has_no_name_column():
    """A mother is a card number and nothing else. A new column here has to
    be added to this list on purpose."""
    columns = {c.name for c in Participant.__table__.columns}
    assert columns == {"id", "code", "created_at", "created_by_id", "merged_into_id"}


def test_the_audit_log_names_a_participant_by_id_not_card_number(app, admin_user):
    code = some_codes(1)[0]
    participants.load_card_list(card_csv([code]))
    participant = participants.create_participant(code, created_by_id=admin_user.id)
    db.session.commit()

    for row in AuditLog.query.all():
        assert code not in " ".join(str(v) for v in (row.old_value, row.new_value, row.description))
    assert repr(participant) == f"<Participant {participant.id}>"


# --- The upload page and the command ------------------------------------------------------


def upload(client, text, name="cards-batch-1.csv"):
    import io
    return client.post("/admin/card-numbers", data={"card_list": (io.BytesIO(text.encode()), name)},
                       content_type="multipart/form-data", follow_redirects=True)


def test_an_admin_uploads_a_card_list(client, seeded_app):
    response = upload(client, card_csv(some_codes(4)))
    assert "Loaded 4 card numbers" in response.get_data(as_text=True)
    again = upload(client, card_csv(some_codes(4)))
    assert "already loaded; nothing changed" in again.get_data(as_text=True)


def test_a_bad_upload_says_why_and_loads_nothing(client, seeded_app):
    response = upload(client, "list_position,card_number,batch\n1,12345,1\n")
    assert "Nothing was loaded" in response.get_data(as_text=True)
    assert CardNumber.query.count() == 0


def test_staff_cannot_load_card_lists(staff_client, seeded_app):
    assert staff_client.get("/admin/card-numbers").status_code == 403
    assert upload(staff_client, card_csv(some_codes(2))).status_code == 403
    assert CardNumber.query.count() == 0


def test_the_command_loads_a_list_as_an_admin(seeded_app, admin_user, tmp_path):
    path = tmp_path / "cards-batch-1.csv"
    path.write_text(card_csv(some_codes(3)))
    runner = CliRunner()
    cli = seeded_app.cli

    refused = runner.invoke(cli, ["cards", "load", str(path), "--as", "nobody@test.example"])
    assert refused.exit_code != 0 and CardNumber.query.count() == 0

    result = runner.invoke(cli, ["cards", "load", str(path), "--as", admin_user.email])
    assert result.exit_code == 0, result.output
    assert "Loaded 3 card numbers" in result.output
