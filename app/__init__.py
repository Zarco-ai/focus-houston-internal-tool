
import logging
import os
import secrets
import stat

import click
from flask import Flask
from werkzeug.middleware.proxy_fix import ProxyFix

from app.config import config_from_environment
from app.extensions import csrf, db, limiter, migrate

MIGRATIONS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "migrations"
)


def create_app(config_object=None):
    config_object = config_object or config_from_environment()

    app = Flask(__name__, instance_relative_config=True)
    app.config.from_object(config_object)

    if app.config.get("IS_PRODUCTION"):
        app.config.update(config_object.load())
    elif not app.config.get("SECRET_KEY"):
        os.makedirs(app.instance_path, exist_ok=True)
        app.config["SECRET_KEY"] = resolve_secret_key(app.instance_path)

    if app.config.get("IS_PRODUCTION"):
        hops = app.config["PROXY_HOPS"]
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=hops, x_proto=hops, x_host=hops)
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        )

    db.init_app(app)
    csrf.init_app(app)
    migrate.init_app(app, db, directory=MIGRATIONS_DIR, compare_type=True)
    limiter.init_app(app)

    from app import models  # noqa: F401

    from app.auth import register_guard
    from app.security import register_security
    from app.views.auth import register_google

    register_google(app)
    register_guard(app)
    register_security(app)
    register_template_helpers(app)
    register_blueprints(app)
    register_commands(app)
    register_card_commands(app)
    register_backup_commands(app)
    register_demo_commands(app)
    register_history_commands(app)
    register_batch_commands(app)


    return app


def resolve_secret_key(instance_path):
    from_env = os.environ.get("SECRET_KEY")
    if from_env:
        return from_env

    key_path = os.path.join(instance_path, "secret_key")

    if os.path.exists(key_path):
        with open(key_path, "r", encoding="utf-8") as handle:
            existing = handle.read().strip()
        if existing:
            return existing

    key = secrets.token_hex(32)
    with open(key_path, "w", encoding="utf-8") as handle:
        handle.write(key)

    os.chmod(key_path, stat.S_IRUSR | stat.S_IWUSR)

    return key


def register_blueprints(app):
    from app.views.admin import admin_bp
    from app.views.auth import auth_bp
    from app.views.batches import batches_bp
    from app.views.entry import entry_bp
    from app.views.health import health_bp
    from app.views.home import home_bp
    from app.views.intake import intake_bp
    from app.views.reports import reports_bp
    from app.views.responses import responses_bp
    from app.views.review import review_bp
    from app.views.totals import totals_bp
    from app.views.users import users_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(health_bp)
    app.register_blueprint(users_bp)
    app.register_blueprint(home_bp)
    app.register_blueprint(entry_bp)
    app.register_blueprint(intake_bp)
    app.register_blueprint(responses_bp)
    app.register_blueprint(reports_bp)
    app.register_blueprint(batches_bp)
    app.register_blueprint(review_bp)
    app.register_blueprint(totals_bp)
    app.register_blueprint(admin_bp)


def register_commands(app):

    @app.cli.group("users")
    def users_group():
        pass

    @users_group.command("grant-admin")
    @click.argument("email")
    def grant_admin(email):
        from app.services import users

        user = users.grant_admin_break_glass(email)
        click.echo(f"{user.email} is now an active admin.")

    @app.cli.group("catalog")
    def catalog_group():
        pass

    @catalog_group.command("load-if-empty")
    def load_catalog_if_empty():
        from app.services import seeding

        summary = seeding.load_if_empty(app.config["SEED_CSV_PATH"])
        if summary is None:
            click.echo("The catalog is already loaded; nothing changed.")
        else:
            click.echo(
                f"Loaded {summary['items_added']} items and "
                f"{summary['service_types_added']} service types."
            )

    @catalog_group.command("export-csv")
    @click.argument("path", required=False)
    def export_catalog_csv(path):
        from app.models import Item
        from app.services import seeding

        path = path or app.config["SEED_CSV_PATH"]
        count = seeding.write_catalog_csv(path, Item.query.all())
        click.echo(f"Wrote {count} items to {path}.")

    @catalog_group.command("import-valuations")
    @click.argument("path", type=click.Path(exists=True, dir_okay=False))
    @click.option("--apply", "apply_", is_flag=True,
                  help="Make the changes. Without this, nothing is written.")
    @click.option("--as", "as_email", metavar="EMAIL",
                  help="The admin the changes are recorded under. Required with --apply.")
    def import_valuations(path, apply_, as_email):
        from app.extensions import db
        from app.models import Item
        from app.services import audit, clock, users, valuations

        admin = None
        if apply_:
            admin = users.find_by_email(as_email) if as_email else None
            if admin is None or not admin.active or not admin.is_admin:
                raise click.UsageError(
                    "--apply needs --as EMAIL, the email of an active admin. "
                    "Every change is recorded in the audit log under that person."
                )

        try:
            rows = valuations.read_workbook(path)
        except valuations.ImportError_ as error:
            raise click.ClickException(str(error))
        plan = valuations.plan_import(rows, Item.query.all())
        for line in valuations.describe(plan):
            click.echo(line)

        if not apply_ or plan.is_empty:
            return
        if plan.blocked:
            raise click.ClickException("Not applied: resolve the blocking rows first.")

        try:
            with audit.acting_as(admin.id):
                valuations.apply_plan(plan, effective_from=clock.local_today())
            db.session.commit()
        except Exception:
            db.session.rollback()
            raise
        click.echo(
            f"APPLIED as {admin.email}: {len(plan.new_items)} new item(s), "
            f"{len(plan.price_changes)} price change(s), "
            f"{len(plan.other_changes)} other change(s)."
        )


def register_backup_commands(app):
    @app.cli.group("backup")
    def backup_group():
        pass

    @backup_group.command("create")
    def backup_create():
        import sys

        from app.services import backup

        if backup.output_is_terminal(sys.stdout):
            raise click.UsageError(
                "Refusing to print a backup on screen. Pipe it into an encryption "
                "tool -- see docs/RUNBOOK.md, 'Weekly backup'.")
        try:
            document = backup.create()
        except backup.BackupError as error:
            raise click.ClickException(str(error))
        sys.stdout.write(backup.dumps(document))
        sys.stdout.write("\n")
        rows = sum(backup.summary(document).values())
        click.echo(f"Backed up {rows} rows at migration {document['alembic_revision']}.", err=True)

    @backup_group.command("restore")
    @click.argument("source", type=click.File("r", encoding="utf-8"), default="-")
    def backup_restore(source):
        import json

        from app.services import backup

        try:
            counts = backup.restore(json.load(source))
        except (ValueError, backup.BackupError) as error:
            raise click.ClickException(f"Nothing was restored. {error}")
        click.echo(f"Restored {sum(counts.values())} rows into {len(counts)} tables.")


def register_demo_commands(app):

    @app.cli.group("demo")
    def demo_group():
        pass

    def refuse_production():
        if app.config.get("IS_PRODUCTION"):
            raise click.UsageError("The demo commands never run against production or staging.")

    @demo_group.command("load")
    @click.option("--mothers", default=300, show_default=True)
    @click.option("--seed", default=2026, show_default=True)
    @click.option("--as", "as_email", required=True, metavar="EMAIL",
                  help="An active admin in this database; changes are recorded under them.")
    def demo_load(mothers, seed, as_email):
        import time

        from app.services import audit, demo, seeding, users

        refuse_production()
        admin = users.find_by_email(as_email)
        if admin is None or not admin.active or not admin.is_admin:
            raise click.UsageError("--as must be an active admin in this database.")
        began = time.perf_counter()
        with audit.acting_as(admin.id):
            seeding.load_if_empty(app.config["SEED_CSV_PATH"])
            try:
                summary = demo.load(count=mothers, seed=seed, admin_id=admin.id,
                                    progress=lambda message: click.echo(f"  {message}"))
            except demo.DemoError as error:
                raise click.ClickException(str(error))
        click.echo(f"Loaded in {time.perf_counter() - began:.1f}s: {summary}")

    @demo_group.command("check")
    @click.option("--from", "start", default="2025-01-01", show_default=True)
    def demo_check(start):
        import datetime

        from app.services import metrics, reports, retention, totals, verify

        start = datetime.date.fromisoformat(start)
        today = clock_today()
        people = retention.visits_by_person()
        window = retention.window_days()
        failures = 0
        click.echo(f"{len(people)} mothers, window {window} days.\n")
        click.echo(f"{'month':8} {'S':>4} {'E':>4} {'N':>4} {'R':>4} {'back':>4} {'sep':>4} "
                   f"{'FOCUS%':>8} {'retain%':>8} {'engage%':>8} {'turn%':>7}  check")
        month = datetime.date(start.year, start.month, 1)
        while month <= today:
            first, last = totals.month_range(month.year, month.month)
            fast = retention.measure(first, last, window=window, today=today, people=people)
            slow = verify.measure(people, first, last, window, today)
            differences = verify.compare(fast, slow)

            values = metrics.values(first, last)
            report = reports.monthly(month.year, month.month)
            if values["non_food_gik"] != values["goods_value"] + values["diapers_value"]:
                differences.append(("non_food_gik", values["non_food_gik"], "goods + diapers"))
            for column in report.columns:
                if column not in reports.FIXED and report.totals.get(column.key, 0) != values[column.key]:
                    differences.append((f"report {column.key}", report.totals.get(column.key, 0),
                                        values[column.key]))
            if fast.E != fast.R + fast.returned + fast.new_still_active:
                differences.append(("E partition", fast.E, "R + back + new"))
            unique = sum(1 for dates in people.values() if any(first <= d <= last for d in dates))
            if values["unique_clients_distinct"] != unique:
                differences.append(("unique clients", values["unique_clients_distinct"], unique))
            minutes = verify.intake_minutes(first, last)
            if values["intakes_timed"] != len(minutes) or (
                    minutes and values["intake_minutes_median"] != verify.median(minutes)):
                differences.append(("intake times", values["intakes_timed"], len(minutes)))

            show = lambda v: "n/a" if v is None else f"{v}"  # noqa: E731
            click.echo(f"{month:%Y-%m} {fast.S:>4} {fast.E:>4} {fast.N:>4} {fast.R:>4} "
                       f"{fast.returned:>4} {fast.separations:>4} {show(fast.retention_focus):>8} "
                       f"{show(fast.retained_rate):>8} {show(fast.engagement):>8} "
                       f"{show(fast.turnover):>7}  {'ok' if not differences else differences}")
            failures += bool(differences)
            month = (month + datetime.timedelta(days=32)).replace(day=1)

        click.echo("\nWhole years (where the two retention formulas part ways most):")
        for year in range(start.year, today.year + 1):
            first, last = datetime.date(year, 1, 1), datetime.date(year, 12, 31)
            r = retention.measure(first, last, window=window, today=today, people=people)
            slow = verify.measure(people, first, last, window, today)
            mismatch = verify.compare(r, slow)
            failures += bool(mismatch)
            click.echo(f"  {year}: S {r.S}, E {r.E}, N {r.N}, R {r.R}, came back {r.returned} -> "
                       f"FOCUS formula {show(r.retention_focus)}%, retained {show(r.retained_rate)}%"
                       f"{'' if not mismatch else f'  MISMATCH {mismatch}'}")

        for year in range(start.year, today.year + 1):
            for group in reports.annual(year).groups:
                for row in group.rows:
                    if reports.is_additive(row) and row.year != sum(v or 0 for v in row.months):
                        click.echo(f"{year} {row.key}: year {row.year} != sum of months")
                        failures += 1
        click.echo("\nEvery check passed." if not failures else f"\n{failures} check(s) FAILED.")
        if failures:
            raise SystemExit(1)

    @demo_group.command("stress")
    @click.option("--year", type=int, default=None)
    def demo_stress(year):
        import time

        from app.services import backup, exports, metrics, reports, totals

        refuse_production()
        today = clock_today()
        year = year or today.year
        month_start, month_end = totals.month_range(today.year, today.month)
        year_start, year_end = (totals.month_range(year, 1)[0], totals.month_range(year, 12)[1])

        def timed(label, work):
            began = time.perf_counter()
            result = work()
            click.echo(f"  {label:<44} {time.perf_counter() - began:6.2f}s")
            return result

        click.echo("Timings (one run each, this machine):")
        timed("Totals page numbers, this month", lambda: metrics.report(month_start, month_end))
        timed("Totals page numbers, whole year", lambda: metrics.report(year_start, year_end))
        timed("Monthly report", lambda: reports.monthly(today.year, today.month))
        timed("Annual report", lambda: reports.annual(year))
        timed("Line-item CSV, whole year", lambda: totals.export_csv(year_start, year_end))
        timed("Retention CSV, whole year", lambda: exports.retention_csv(year_start, year_end))
        timed("Program Health CSV, whole year", lambda: exports.program_health_csv(year_start, year_end))
        size = timed("Backup of the whole database", lambda: len(backup.dumps(backup.create())))
        click.echo(f"  (backup is {size / 1_000_000:.1f} MB before encryption)")


def clock_today():
    from app.services import clock

    return clock.local_today()


def register_history_commands(app):
    @app.cli.group("history")
    def history_group():
        pass

    @history_group.command("import")
    @click.argument("path", type=click.Path(exists=True, dir_okay=False))
    @click.option("--as", "as_email", required=True, metavar="EMAIL",
                  help="The admin the import is recorded under.")
    def history_import(path, as_email):
        from app.services import audit, history, users

        admin = users.find_by_email(as_email)
        if admin is None or not admin.active or not admin.is_admin:
            raise click.UsageError("--as must be the email of an active admin.")
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
        try:
            with audit.acting_as(admin.id):
                summary = history.import_history(text)
        except history.HistoryError as error:
            raise click.ClickException(f"Nothing was imported. {error}")
        click.echo(f"Imported {summary['sessions']} sessions, {summary['lines']} rows; "
                   f"{summary['to_review']} lines are waiting in the review queue.")

    @history_group.command("export")
    @click.argument("path", type=click.Path(dir_okay=False, writable=True))
    def history_export(path):
        from app.services import history

        with open(path, "w", encoding="utf-8") as handle:
            handle.write(history.export_text())
        click.echo(f"Wrote every approved session to {path}.")


def register_batch_commands(app):
    @app.cli.group("batches")
    def batches_group():
        pass

    @batches_group.command("delete-date")
    @click.argument("day")
    @click.option("--yes", is_flag=True, help="Really delete. Without it, only lists.")
    @click.option("--count", type=int, help="How many batches you expect (required with --yes).")
    @click.option("--as", "as_email", metavar="EMAIL", help="The admin it is recorded under.")
    def batches_delete_date(day, yes, count, as_email):
        import datetime

        from app.models import Batch
        from app.services import audit, batches, users

        try:
            date = datetime.date.fromisoformat(day)
        except ValueError:
            raise click.UsageError("DAY must look like 2026-11-05.")
        found = Batch.query.filter_by(date=date).order_by(Batch.id).all()
        click.echo(f"{len(found)} batch{'es' if len(found) != 1 else ''} dated {date.isoformat()}:")
        for batch in found:
            service = batch.service_type.name if batch.service_type else "no service type"
            click.echo(f"  #{batch.id}  {batch.status:9}  {service:22}  "
                       f"{len(batch.lines):3} lines  ${batch.total_value:,.2f}")
        if not yes:
            click.echo(f"Nothing deleted. To delete exactly these, run again with "
                       f"--yes --count {len(found)} --as ADMIN_EMAIL.")
            return
        if count != len(found):
            raise click.ClickException(f"--count is {count} but {len(found)} batches are dated "
                                       f"{date.isoformat()}. Nothing deleted.")
        admin = users.find_by_email(as_email or "")
        if admin is None or not admin.active or not admin.is_admin:
            raise click.UsageError("--as must be the email of an active admin.")
        with audit.acting_as(admin.id):
            for batch in found:
                batches.delete_for_good(batch)
        db.session.commit()
        click.echo(f"Deleted {len(found)} batches dated {date.isoformat()}. No other date was touched.")


def register_card_commands(app):
    @app.cli.group("cards")
    def cards_group():
        pass

    @cards_group.command("load")
    @click.argument("path", type=click.Path(exists=True, dir_okay=False))
    @click.option("--as", "as_email", metavar="EMAIL", required=True,
                  help="The admin the load is recorded under.")
    def load_cards(path, as_email):
        from app.extensions import db
        from app.services import audit, participants, users

        admin = users.find_by_email(as_email)
        if admin is None or not admin.active or not admin.is_admin:
            raise click.UsageError("--as must be the email of an active admin.")
        with open(path, encoding="utf-8-sig") as handle:
            text = handle.read()
        try:
            with audit.acting_as(admin.id):
                added = participants.load_card_list(text)
            db.session.commit()
        except participants.CardListError as error:
            db.session.rollback()
            raise click.ClickException(f"Nothing was loaded. {error}")
        click.echo(f"Loaded {added} card numbers." if added
                   else "That batch is already loaded; nothing changed.")


def register_template_helpers(app):

    from app.constants import (
        CONDITION_USED,
        bucket_label,
        category_label,
        category_short_label,
    )

    @app.template_filter("money")
    def money_filter(value):
        if value is None:
            return "—"
        return "${:,.2f}".format(value)

    @app.template_filter("local_datetime")
    def local_datetime_filter(moment):
        from app.services import clock

        if moment is None:
            return "—"
        return clock.to_local(moment).strftime("%b %-d, %Y %-I:%M %p")

    @app.template_filter("duration")
    def duration_filter(seconds):
        seconds = max(0, int(seconds or 0))
        return f"{seconds // 3600}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"

    @app.template_filter("category_label")
    def category_label_filter(slug):
        return category_label(slug)

    @app.template_filter("category_short")
    def category_short_filter(slug):
        return category_short_label(slug)

    @app.template_filter("bucket_label")
    def bucket_label_filter(slug):
        return bucket_label(slug)

    @app.context_processor
    def inject_constants():
        from app.models import get_commit_mode

        from app.services import clock

        return {
            "CONDITION_USED": CONDITION_USED,
            "commit_mode": get_commit_mode(),
            "today": clock.local_today(),
        }
