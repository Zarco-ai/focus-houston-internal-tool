
from app.constants import CONDITION_USED
from app.extensions import db
from app.models import LineItem
from app.models.money import to_money
from app.services import audit
from app.services import batches as batch_service
from app.services import catalog


def flagged_lines():
    return (
        LineItem.query.filter_by(needs_review=True)
        .join(LineItem.batch)
        .order_by(LineItem.id)
        .all()
    )


def flagged_count():
    return LineItem.query.filter_by(needs_review=True).count()


def price_line(line, unit_price=None):
    if line.is_custom:
        raise ValueError(
            "This item is not in the catalog yet, so match it to one first."
        )

    if unit_price is None:
        unit_price = line.item.current_price()
    if unit_price is None:
        raise ValueError(f"Type a price for {line.display_name}.")

    was_committed = line.batch.is_committed
    label = f"{line.display_name} in batch {line.batch_id}"

    old_price = line.unit_price_at_time
    old_value = line.computed_value

    line.unit_price_at_time = to_money(unit_price)
    if line.used_multiplier_at_time is None:
        line.used_multiplier_at_time = line.item.used_multiplier
    line.recalculate()
    batch_service.sync_review_flag(line)

    if was_committed:
        audit.record_change(
            table_name="line_item",
            record_id=line.id,
            field="unit_price_at_time",
            old_value=old_price,
            new_value=line.unit_price_at_time,
            description=f"Priced {label}",
        )
        audit.record_change(
            table_name="line_item",
            record_id=line.id,
            field="computed_value",
            old_value=old_value,
            new_value=line.computed_value,
            description=f"Priced {label}",
        )

    db.session.commit()
    return line


def resolve_line(line, item=None, unit_price=None, new_item_fields=None):
    if not line.is_custom:
        raise ValueError(
            f"{line.display_name} is already in the catalog. It is waiting for a "
            f"price, not for an item to match."
        )

    if item is None and new_item_fields:
        item = catalog.create_item(**new_item_fields)

    if item is None:
        raise ValueError("Pick an item to match this to, or create a new one.")

    was_committed = line.batch.is_committed
    label = f"custom line '{line.custom_name}' in batch {line.batch_id}"

    if unit_price is None:
        unit_price = item.current_price()
    if unit_price is None and line.unit_price_at_time:
        unit_price = line.unit_price_at_time

    if unit_price is None:
        raise ValueError(
            f"{item.display_name} has no price on file, so type one here."
        )

    old_value = line.computed_value

    line.item_id = item.id
    line.item = item
    line.used_multiplier_at_time = item.used_multiplier
    if was_committed:
        line.report_bucket_at_time = item.report_bucket
    line.unit_price_at_time = to_money(unit_price)
    line.recalculate()
    batch_service.sync_review_flag(line)

    if was_committed:
        audit.record_change(
            table_name="line_item",
            record_id=line.id,
            field="item_id",
            old_value=None,
            new_value=item.display_name,
            description=f"Resolved {label}",
        )
        audit.record_change(
            table_name="line_item",
            record_id=line.id,
            field="computed_value",
            old_value=old_value,
            new_value=line.computed_value,
            description=f"Resolved {label}",
        )

    db.session.commit()
    return line


def can_value_used(line):
    return not (line.condition == CONDITION_USED and line.used_multiplier_at_time is None)
