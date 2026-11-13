"""Backup and restore (Phase 9).

The test that matters: back up a database with something in every kind of
table, empty it, restore, and get back exactly what was there -- the same
rows, the same values, the same next ids.
"""

import datetime
import json
import random
from decimal import Decimal

import pytest
from click.testing import CliRunner

from app.extensions import db
from app.models import Batch, LineItem, ParticipantVisit, ReportSnapshot, ServiceType
from app.services import (
    backup,
    batches,
    card_numbers,
    catalog,
    intake,
    participants,
    snapshots,
)
from tests.conftest import empty_every_table


def some_of_everything(admin_user):
    """A batch with lines and attendance, a participant with an intake and a
    quality, a filed snapshot, audit rows: every type of column."""
    codes = card_numbers.generate(3, rng=random.Random(21))
    participants.load_card_list("list_position,card_number,batch\n" + "".join(
        f"{i},{c},1\n" for i, c in enumerate(codes, 1)))
    quality = intake.add_quality("First pregnancy")
    intake.save_survey(codes[0], reason="Expecting", score="9", quality_ids=[str(quality.id)],
                       slider_minutes="45")
    batch = batches.open_batch(service_type_id=ServiceType.query.first().id, note="Donation")
    batches.set_line(batch, catalog.find_item("Coat", "clothing_adult"), quantity=2, condition="used")
    batches.set_attendance(batch, {"children_served": 4, "meals_value": Decimal("12.34"),
                                   "volunteer_hours_tenths": 25})
    batches.commit_batch(batch)
    snapshots.file("annual", 2026, filed_by_id=admin_user.id, app_version="test")
    db.session.commit()


def test_a_backup_restores_to_exactly_the_same_database(seeded_app, admin_user):
    some_of_everything(admin_user)
    before = backup.create()

    empty_every_table()
    counts = backup.restore(json.loads(backup.dumps(before)))

    db.session.expire_all()
    after = backup.create()
    assert backup.dumps(after["tables"]) == backup.dumps(before["tables"])
    assert counts["line_item"] == 1 and counts["report_snapshot"] == 1


def test_values_come_back_as_the_same_types(seeded_app, admin_user):
    some_of_everything(admin_user)
    document = json.loads(backup.dumps(backup.create()))
    empty_every_table()
    backup.restore(document)

    line = LineItem.query.one()
    assert line.computed_value == Decimal("108.00")
    assert isinstance(Batch.query.one().committed_at, datetime.datetime)
    assert Batch.query.one().attendance.meals_value == Decimal("12.34")
    assert isinstance(ParticipantVisit.query.one().visit_date, datetime.date)
    assert ReportSnapshot.query.one().payload["values"]


def test_new_rows_after_a_restore_get_new_ids(seeded_app, admin_user):
    some_of_everything(admin_user)
    document = json.loads(backup.dumps(backup.create()))
    empty_every_table()
    backup.restore(document)

    fresh = batches.open_batch(service_type_id=ServiceType.query.first().id)
    db.session.commit()
    assert fresh.id == 2


def test_restore_refuses_a_database_with_data(seeded_app, admin_user):
    some_of_everything(admin_user)
    document = json.loads(backup.dumps(backup.create()))
    with pytest.raises(backup.BackupError, match="isn't empty"):
        backup.restore(document)


@pytest.mark.parametrize("change, message", [
    ({"format": "something-else"}, "isn't a FOCUS backup"),
    ({"format_version": 99}, "isn't one this version"),
    ({"alembic_revision": "0000"}, "migration"),
])
def test_restore_refuses_the_wrong_file(seeded_app, change, message):
    document = json.loads(backup.dumps(backup.create()))
    empty_every_table()
    document.update(change)
    with pytest.raises(backup.BackupError, match=message):
        backup.restore(document)


def test_a_failed_restore_leaves_the_database_empty(seeded_app, admin_user):
    some_of_everything(admin_user)
    document = json.loads(backup.dumps(backup.create()))
    empty_every_table()
    document["tables"]["line_item"]["rows"][0][0] = "not an id"
    with pytest.raises(Exception):
        backup.restore(document)
    assert Batch.query.count() == 0


def test_the_command_wont_print_a_backup_on_screen(seeded_app, monkeypatch):
    monkeypatch.setattr(backup, "output_is_terminal", lambda stream: True)
    result = CliRunner().invoke(seeded_app.cli, ["backup", "create"])
    assert result.exit_code != 0
    assert "Refusing to print a backup on screen" in result.output


def test_the_commands_round_trip(seeded_app, admin_user, tmp_path):
    some_of_everything(admin_user)
    runner = CliRunner()
    created = runner.invoke(seeded_app.cli, ["backup", "create"])
    assert created.exit_code == 0, created.output
    path = tmp_path / "focus.json"
    path.write_text(created.stdout.split("\n")[0])

    empty_every_table()
    restored = runner.invoke(seeded_app.cli, ["backup", "restore", str(path)])
    assert restored.exit_code == 0, restored.output
    assert "Restored" in restored.output
    assert LineItem.query.count() == 1
