
from app.constants import (
    PRICE_ENTRY_CHOICES,
    PRICE_ENTRY_MANUAL,
    REPORT_BUCKETS,
    sort_categories,
)
from app.extensions import db
from app.models import Item
from app.services import audit


class CatalogError(Exception):
    pass


def active_items():
    items = Item.query.filter_by(active=True).all()
    return _sorted(items)


def all_items():
    return _sorted(Item.query.all())


def _sorted(items):
    order = {slug: index for index, slug in enumerate(
        sort_categories({item.category for item in items})
    )}
    return sorted(items, key=lambda item: (order[item.category], item.name.lower()))


def group_by_category(items):
    groups = {}
    for item in items:
        groups.setdefault(item.category, []).append(item)
    return [(slug, groups[slug]) for slug in sort_categories(groups.keys())]


def categories_in_use():
    slugs = {row[0] for row in db.session.query(Item.category).distinct()}
    return sort_categories(slugs)


def items_needing_a_price():
    items = Item.query.filter_by(price_entry=PRICE_ENTRY_MANUAL, active=True).all()
    return _sorted(items)


def find_item(name, category):
    return Item.query.filter_by(name=name, category=category).one_or_none()


def _validate(name, category, report_bucket, used_multiplier, price_entry):
    name = (name or "").strip()
    category = (category or "").strip()

    if not name:
        raise CatalogError("The item needs a name.")
    if not category:
        raise CatalogError("The item needs a category.")
    if report_bucket not in REPORT_BUCKETS:
        raise CatalogError(
            f"'{report_bucket}' is not a report bucket. Pick one of: "
            f"{', '.join(REPORT_BUCKETS)}."
        )
    if price_entry not in PRICE_ENTRY_CHOICES:
        raise CatalogError("Price entry must be 'fixed' or 'manual'.")

    try:
        multiplier = float(used_multiplier)
    except (TypeError, ValueError):
        raise CatalogError("The used rate must be a number, for example 0.5.")
    if not 0 < multiplier <= 1:
        raise CatalogError("The used rate must be greater than 0 and at most 1.")

    return name, category, multiplier


def create_item(name, category, report_bucket, used_multiplier, price_entry,
                unit_price_new=None, note=None):
    name, category, multiplier = _validate(
        name, category, report_bucket, used_multiplier, price_entry
    )

    if find_item(name, category) is not None:
        raise CatalogError(
            f"'{name}' already exists in {category}. Names can repeat across "
            f"categories but not inside one."
        )

    item = Item(
        name=name,
        category=category,
        report_bucket=report_bucket,
        used_multiplier=multiplier,
        price_entry=price_entry,
        note=(note or "").strip() or None,
        active=True,
    )
    db.session.add(item)
    db.session.flush()

    item.set_price(_parse_optional_price(unit_price_new))

    audit.record_change(
        table_name="item",
        record_id=item.id,
        field="created",
        old_value=None,
        new_value=item.display_name,
        description=f"Added {item.display_name} to the catalog",
        action=audit.ACTION_CREATE,
    )

    db.session.commit()
    return item


def update_item(item, name=None, category=None, report_bucket=None,
                used_multiplier=None, price_entry=None, note=None, active=None,
                display_group=None):
    new_name = item.name if name is None else (name or "").strip()
    new_category = item.category if category is None else (category or "").strip()
    new_bucket = item.report_bucket if report_bucket is None else report_bucket
    new_multiplier = (
        item.used_multiplier if used_multiplier is None else used_multiplier
    )
    new_entry = item.price_entry if price_entry is None else price_entry

    new_name, new_category, new_multiplier = _validate(
        new_name, new_category, new_bucket, new_multiplier, new_entry
    )

    if (new_name, new_category) != (item.name, item.category):
        clash = find_item(new_name, new_category)
        if clash is not None and clash.id != item.id:
            raise CatalogError(
                f"'{new_name}' already exists in {new_category}."
            )

    label = item.display_name
    audit.apply_change(item, "name", new_name, "item", label)
    audit.apply_change(item, "category", new_category, "item", label)
    audit.apply_change(item, "report_bucket", new_bucket, "item", label)
    audit.apply_change(item, "used_multiplier", new_multiplier, "item", label)
    audit.apply_change(item, "price_entry", new_entry, "item", label)

    if note is not None:
        audit.apply_change(item, "note", (note or "").strip() or None, "item", label)

    if active is not None:
        audit.apply_change(item, "active", bool(active), "item", label)

    if display_group is not None:
        group = display_group.strip() or None
        if group is not None and group not in REPORT_BUCKETS:
            raise CatalogError(
                f"'{group}' is not a report group. Pick one of: "
                f"{', '.join(REPORT_BUCKETS)}, or leave it as the bucket."
            )
        audit.apply_change(item, "display_group", group, "item", label)

    db.session.commit()
    return item


def change_price(item, unit_price_new, effective_from=None):
    price = _parse_optional_price(unit_price_new)
    old_price = item.current_price()

    item.set_price(price, effective_from=effective_from)

    audit.record_change(
        table_name="item",
        record_id=item.id,
        field="unit_price_new",
        old_value=old_price,
        new_value=price,
        description=f"Price change for {item.display_name}",
    )

    db.session.commit()
    return item


def set_active(item, active):
    audit.apply_change(item, "active", bool(active), "item", item.display_name)
    db.session.commit()
    return item


def _parse_optional_price(value):
    if value is None:
        return None
    if isinstance(value, str):
        value = value.strip().replace("$", "").replace(",", "")
        if not value:
            return None
    try:
        price = float(value)
    except (TypeError, ValueError):
        raise CatalogError("The price must be a number, for example 12.50.")
    if price < 0:
        raise CatalogError("A price cannot be negative.")
    return price
