#!/usr/bin/env python3

import argparse
import os
import sys

os.environ.setdefault("FOCUS_ENV", "development")

import flask_migrate  # noqa: E402
from sqlalchemy import MetaData, Table, inspect  # noqa: E402

from app import create_app  # noqa: E402
from app.extensions import db  # noqa: E402
from app.services.seeding import SeedError, seed_all  # noqa: E402


def rebuild_schema():
    db.drop_all()
    Table("alembic_version", MetaData()).drop(db.engine, checkfirst=True)
    flask_migrate.upgrade()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--reset",
        action="store_true",
        help="drop all tables first (deletes every batch you have entered)",
    )
    parser.add_argument(
        "--csv",
        default=None,
        help="path to the catalog CSV (defaults to the one in this folder)",
    )
    args = parser.parse_args()

    app = create_app()
    csv_path = args.csv or app.config["SEED_CSV_PATH"]

    if args.reset:
        if app.config.get("IS_PRODUCTION"):
            print("Refusing to --reset a production database.", file=sys.stderr)
            return 1
        answer = input(
            "This deletes every batch in the database. Type 'reset' to confirm: "
        )
        if answer.strip().lower() != "reset":
            print("Cancelled. Nothing was changed.")
            return 0

    with app.app_context():
        if args.reset:
            rebuild_schema()
        elif not inspect(db.engine).has_table("item"):
            print(
                "The database has no tables yet. Create them first with:\n"
                "  flask --app run db upgrade",
                file=sys.stderr,
            )
            return 1

        try:
            summary = seed_all(csv_path)
        except SeedError as error:
            print(f"Could not load the catalog:\n  {error}", file=sys.stderr)
            return 1
        except FileNotFoundError:
            print(f"Catalog file not found: {csv_path}", file=sys.stderr)
            return 1

    print(f"Read {summary['rows_read']} rows from {csv_path}")
    print(f"  items added   : {summary['items_added']}")
    print(f"  items skipped : {summary['items_skipped']} (already in the catalog)")
    print(f"  prices added  : {summary['prices_added']}")
    print(f"  of those, with no price yet: {summary['blank_prices']}")
    print(f"  service types added: {summary['service_types_added']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
