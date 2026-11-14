
import datetime
import hashlib
import json
from decimal import ROUND_HALF_UP, Decimal

import sqlalchemy as sa

from app.constants import (
    BUCKET_CLOTHING,
    BUCKET_DIAPERS,
    BUCKET_FOOD,
    BUCKET_FORMULA,
    BUCKET_HOUSEHOLD,
    BUCKET_HYGIENE,
    BUCKET_TRANSPORTATION,
    CONDITION_NEW,
    STATUS_COMMITTED,
)
from app.extensions import db
from app.models import Batch, Item, LineItem, ServiceType, get_setting, set_setting
from app.models.audit import ACTION_IMPORT
from app.services import attendance, audit, batches, catalog, clock

FORMAT = "focus-history"
FORMAT_VERSION = 1
EXPORT_VERSION = 2
HISTORY_CATEGORY = "sheet_2026"
CENT = Decimal("0.01")

KINDS = {
    "diapers": ("Diapers (2026 sheet)", BUCKET_DIAPERS, None),
    "wipes": ("Wipes (2026 sheet)", BUCKET_HYGIENE, BUCKET_DIAPERS),
    "hygiene": ("Hygiene (2026 sheet)", BUCKET_HYGIENE, None),
    "clothing": ("Clothing (2026 sheet)", BUCKET_CLOTHING, None),
    "household": ("Household goods (2026 sheet)", BUCKET_HOUSEHOLD, None),
    "toys": ("Toys (2026 sheet)", BUCKET_HOUSEHOLD, None),
    "car_seat": ("Car seats (2026 sheet)", BUCKET_HOUSEHOLD, None),
    "formula": ("Formula (2026 sheet)", BUCKET_FORMULA, None),
    "food": ("Food (2026 sheet)", BUCKET_FOOD, None),
    "transport": ("Bus / metro passes (2026 sheet)", BUCKET_TRANSPORTATION, None),
}
UNCATEGORIZED = "uncategorized"

SERVICE_RULES = (
    ("intake", "Intake"),
    ("esl", "ESL Class"),
    ("nutrition", "Nutrition Class"),
    ("bike", "Bike Giveaway"),
    ("volunteer", "Volunteer Help"),
    ("client", "Walk-in"),
    ("diaper", "Diaper Distribution"),
    ("wipe", "Diaper Distribution"),
    ("distribution", "Diaper Distribution"),
)
OTHER_EVENT = "Other event"


class HistoryError(Exception):
    pass


def fingerprint(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _setting_key(sha):
    return f"history_import_{sha[:16]}"


def already_imported(sha):
    return get_setting(_setting_key(sha)) is not None


def _service_type(text, cache):
    lowered = (text or "").lower()
    name = next((target for word, target in SERVICE_RULES if word in lowered), OTHER_EVENT)
    if name not in cache:
        found = ServiceType.query.filter(sa.func.lower(ServiceType.name) == name.lower()).first()
        if found is None:
            found = ServiceType(name=name, active=True)
            db.session.add(found)
            db.session.flush()
        cache[name] = found
    return cache[name]


def _items():
    found = {}
    for kind, (name, bucket, group) in KINDS.items():
        item = catalog.find_item(name, HISTORY_CATEGORY)
        if item is None:
            item = catalog.create_item(name=name, category=HISTORY_CATEGORY, report_bucket=bucket,
                                       used_multiplier=0.5, price_entry="manual",
                                       note="Imported from the 2026 sheets; a category, not one item.")
            catalog.update_item(item, active=False, display_group=group or "")
        found[kind] = item
    return found


def _split(quantity, value):
    if quantity <= 0:
        raise HistoryError("A line needs a count of at least 1.")
    price = (value / quantity).quantize(CENT, ROUND_HALF_UP)
    if price * quantity == value or quantity == 1:
        return [(quantity, price if quantity > 1 else value)]
    rest = value - price * (quantity - 1)
    if rest < 0:
        price = (value / quantity).quantize(CENT, rounding="ROUND_DOWN")
        rest = value - price * (quantity - 1)
    return [(quantity - 1, price), (1, rest)]


def _add_lines(batch, item, quantity, value, label, note):
    for count, price in _split(quantity, value):
        if item is None:
            batches.add_custom_line(batch, label, quantity=count, unit_price=price,
                                    note=note or "From the 2026 sheet: sort it into a category.")
        else:
            line = LineItem(batch=batch, item_id=item.id, quantity=count, condition=CONDITION_NEW,
                            unit_price_at_time=price, used_multiplier_at_time=item.used_multiplier,
                            price_typed=True)
            db.session.add(line)
            line.recalculate()
    db.session.flush()


def read(text):
    try:
        document = json.loads(text)
    except ValueError:
        raise HistoryError("That file isn't a history file (not JSON).")
    if not isinstance(document, dict) or document.get("format") != FORMAT:
        raise HistoryError("That file isn't a FOCUS history file.")
    if document.get("format_version") not in (FORMAT_VERSION, EXPORT_VERSION):
        raise HistoryError("That history file is from a different version of the app.")
    return document


def check(document):
    if document.get("format_version") == EXPORT_VERSION:
        return _check_export(document)
    today = clock.local_today()
    for number, entry in enumerate(document.get("batches", []), start=1):
        try:
            day = datetime.date.fromisoformat(entry["date"])
            for line in entry.get("lines", []):
                if line.get("kind") != UNCATEGORIZED and line.get("kind") not in KINDS:
                    raise ValueError(f"unknown kind of row {line.get('kind')!r}")
                if int(line["quantity"]) < 1 or Decimal(str(line["value"])) < 0:
                    raise ValueError("a count below 1 or a negative value")
            for value in (entry.get("attendance") or {}).values():
                if int(value) < 0:
                    raise ValueError("a negative attendance count")
        except (KeyError, ValueError, ArithmeticError) as error:
            raise HistoryError(f"Session {number} in the file can't be read: {error}.")
        if day > today:
            raise HistoryError(f"Session {number} is dated in the future ({day}).")


def import_history(text):
    sha = fingerprint(text)
    if already_imported(sha):
        raise HistoryError("This file has already been imported. Nothing was changed.")
    document = read(text)
    check(document)
    _refuse_overlap(document)
    write = _write_export if document["format_version"] == EXPORT_VERSION else _write
    try:
        summary = write(document)
    except Exception:
        db.session.rollback()
        raise
    set_setting(_setting_key(sha), clock.local_today().isoformat())
    audit.record_event(ACTION_IMPORT, "batch", 0,
                       f"Imported {summary['sessions']} sessions from {document.get('source', 'a history file')} "
                       f"(file {sha[:12]})")
    db.session.commit()
    return summary


def _refuse_overlap(document):
    days = [datetime.date.fromisoformat(entry["date"]) for entry in document.get("batches", [])]
    if not days:
        return
    clash = Batch.query.filter(Batch.status == STATUS_COMMITTED,
                               Batch.date.between(min(days), max(days))).count()
    if clash:
        raise HistoryError(
            f"The app already has {clash} approved sessions between {min(days):%-d %B %Y} and "
            f"{max(days):%-d %B %Y}, so this file would count them twice. Nothing was changed. "
            "Import a history file only into a copy of the app that doesn't have those months yet.")


def _write(document):
    items = _items()
    services = {}
    summary = {"sessions": 0, "lines": 0, "to_review": 0, "skipped_empty": 0}

    for entry in document.get("batches", []):
        day = datetime.date.fromisoformat(entry["date"])
        counts = {k: v for k, v in (entry.get("attendance") or {}).items()
                  if k in attendance.FIELDS_BY_NAME and v}
        if not entry.get("lines") and not counts:
            summary["skipped_empty"] += 1
            continue
        batch = batches.open_batch(date=day, service_type_id=_service_type(
            entry.get("service_text"), services).id, note=(entry.get("note") or None))
        for line in entry.get("lines", []):
            kind = line.get("kind")
            item = items.get(kind) if kind != UNCATEGORIZED else None
            if kind != UNCATEGORIZED and item is None:
                raise HistoryError(f"Unknown kind of row: {kind!r}.")
            _add_lines(batch, item, int(line["quantity"]), Decimal(str(line["value"])),
                       line.get("label") or "(no label)", line.get("note"))
            summary["lines"] += 1
        if counts:
            batches.set_attendance(batch, counts)
        batches.commit_batch(batch, commit=False)
        summary["sessions"] += 1
        summary["to_review"] += len(batch.flagged_lines)
    return summary


def _money_text(value):
    return None if value is None else str(value)


def _time_text(value):
    return None if value is None else value.isoformat()


def _item_entry(item):
    price = item.current_price()
    return {"name": item.name, "category": item.category, "report_bucket": item.report_bucket,
            "display_group": item.display_group, "used_multiplier": item.used_multiplier,
            "price_entry": item.price_entry, "unit_price_new": _money_text(price),
            "note": item.note, "active": item.active}


def export_document():
    items, entries = {}, []
    sessions = (Batch.query.filter_by(status=STATUS_COMMITTED)
                .order_by(Batch.date, Batch.id).all())
    for batch in sessions:
        lines = []
        for line in batch.lines:
            if line.item is not None:
                items.setdefault((line.item.name, line.item.category), _item_entry(line.item))
            lines.append({
                "item": [line.item.name, line.item.category] if line.item else None,
                "custom_name": line.custom_name, "custom_note": line.custom_note,
                "quantity": line.quantity, "condition": line.condition,
                "unit_price": _money_text(line.unit_price_at_time),
                "used_multiplier": line.used_multiplier_at_time,
                "value": _money_text(line.computed_value),
                "bucket": line.report_bucket_at_time,
                "price_typed": line.price_typed, "used_rate_typed": line.used_rate_typed,
                "needs_review": line.needs_review, "reviewed_at": _time_text(line.reviewed_at),
            })
        counts = {}
        if batch.attendance is not None:
            for name, spec in attendance.FIELDS_BY_NAME.items():
                value = getattr(batch.attendance, name)
                if value:
                    counts[name] = str(value) if spec.kind == attendance.KIND_MONEY else int(value)
        entries.append({
            "date": batch.date.isoformat(),
            "service_type": batch.service_type.name if batch.service_type else None,
            "note": batch.note, "committed_at": _time_text(batch.committed_at),
            "attendance": counts, "lines": lines,
        })
    today = clock.local_today()
    return {"format": FORMAT, "format_version": EXPORT_VERSION,
            "source": f"Every approved session in the app, exported {today:%-d %B %Y}",
            "exported_at": _time_text(clock.now_utc()),
            "items": list(items.values()), "batches": entries}


def export_text():
    return json.dumps(export_document(), indent=1, ensure_ascii=False)


def _check_export(document):
    today = clock.local_today()
    known = {(i.get("name"), i.get("category")) for i in document.get("items", [])}
    for number, entry in enumerate(document.get("batches", []), start=1):
        try:
            day = datetime.date.fromisoformat(entry["date"])
            for line in entry.get("lines", []):
                if line.get("item") is not None and tuple(line["item"]) not in known:
                    raise ValueError(f"a line's item {line['item']!r} isn't in the file's item list")
                if line.get("item") is None and not line.get("custom_name"):
                    raise ValueError("a line with no item and no name")
                if int(line["quantity"]) < 1:
                    raise ValueError("a count below 1")
                for key in ("unit_price", "value"):
                    if line.get(key) is not None and Decimal(line[key]) < 0:
                        raise ValueError("a negative price or value")
            for name, value in (entry.get("attendance") or {}).items():
                if name not in attendance.FIELDS_BY_NAME:
                    raise ValueError(f"unknown attendance box {name!r}")
                if Decimal(str(value)) < 0:
                    raise ValueError("a negative attendance count")
        except (KeyError, TypeError, ValueError, ArithmeticError) as error:
            raise HistoryError(f"Session {number} in the file can't be read: {error}.")
        if day > today:
            raise HistoryError(f"Session {number} is dated in the future ({day}).")


def _export_items(document):
    found = {}
    for entry in document.get("items", []):
        item = catalog.find_item(entry["name"], entry["category"])
        if item is None:
            item = catalog.create_item(name=entry["name"], category=entry["category"],
                                       report_bucket=entry["report_bucket"],
                                       used_multiplier=entry["used_multiplier"],
                                       price_entry=entry["price_entry"],
                                       unit_price_new=entry.get("unit_price_new"),
                                       note=entry.get("note"))
            catalog.update_item(item, active=entry.get("active", True),
                                display_group=entry.get("display_group") or "")
        found[(entry["name"], entry["category"])] = item
    return found


def _parse_time(text):
    return None if text is None else datetime.datetime.fromisoformat(text)


def _write_export(document):
    items = _export_items(document)
    services = {}
    summary = {"sessions": 0, "lines": 0, "to_review": 0, "skipped_empty": 0}

    for entry in document.get("batches", []):
        name = entry.get("service_type")
        if name and name not in services:
            services[name] = (ServiceType.query.filter(
                sa.func.lower(ServiceType.name) == name.lower()).first() or _new_service_type(name))
        batch = batches.open_batch(date=datetime.date.fromisoformat(entry["date"]),
                                   service_type_id=services[name].id if name else None,
                                   note=entry.get("note") or None)
        restored = []
        for data in entry.get("lines", []):
            item = items[tuple(data["item"])] if data.get("item") else None
            line = LineItem(
                batch=batch, item_id=item.id if item else None,
                custom_name=data.get("custom_name"), custom_note=data.get("custom_note"),
                quantity=int(data["quantity"]), condition=data.get("condition") or CONDITION_NEW,
                unit_price_at_time=None if data.get("unit_price") is None else Decimal(data["unit_price"]),
                used_multiplier_at_time=data.get("used_multiplier"),
                report_bucket_at_time=data.get("bucket"),
                price_typed=bool(data.get("price_typed")),
                used_rate_typed=bool(data.get("used_rate_typed")))
            db.session.add(line)
            line.recalculate()
            expected = None if data.get("value") is None else Decimal(data["value"])
            if expected is not None and line.computed_value != expected:
                raise HistoryError(f"A line on {entry['date']} adds up to {line.computed_value}, "
                                   f"not the {expected} in the file. Nothing was imported.")
            restored.append((line, data))
        db.session.flush()
        counts = {}
        for field_name, value in (entry.get("attendance") or {}).items():
            kind = attendance.FIELDS_BY_NAME[field_name].kind
            counts[field_name] = Decimal(str(value)) if kind == attendance.KIND_MONEY else int(value)
        if counts:
            batches.set_attendance(batch, counts)
        batches.commit_batch(batch, commit=False)
        for line, data in restored:
            line.needs_review = bool(data.get("needs_review"))
            line.reviewed_at = _parse_time(data.get("reviewed_at"))
        if entry.get("committed_at"):
            batch.committed_at = _parse_time(entry["committed_at"])
        db.session.flush()
        summary["sessions"] += 1
        summary["lines"] += len(restored)
        summary["to_review"] += sum(1 for line, _ in restored if line.needs_review)
    return summary


def _new_service_type(name):
    found = ServiceType(name=name, active=True)
    db.session.add(found)
    db.session.flush()
    return found
