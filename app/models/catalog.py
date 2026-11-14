
from sqlalchemy import UniqueConstraint

from app.constants import (
    PRICE_ENTRY_FIXED,
    PRICE_ENTRY_MANUAL,
    category_label,
    category_short_label,
)
from app.extensions import db
from app.models.money import Money
from app.services import clock


class ServiceType(db.Model):
    __tablename__ = "service_type"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False, unique=True)
    active = db.Column(db.Boolean, nullable=False, default=True)

    def __repr__(self):
        return f"<ServiceType {self.name}>"


class Item(db.Model):
    __tablename__ = "item"
    __table_args__ = (
        UniqueConstraint("name", "category", name="uq_item_name_category"),
    )

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), nullable=False)
    category = db.Column(db.String(100), nullable=False)
    report_bucket = db.Column(db.String(50), nullable=False)

    display_group = db.Column(db.String(50), nullable=True)

    used_multiplier = db.Column(db.Float, nullable=False)

    price_entry = db.Column(db.String(20), nullable=False, default=PRICE_ENTRY_FIXED)

    note = db.Column(db.Text, nullable=True)

    active = db.Column(db.Boolean, nullable=False, default=True)

    prices = db.relationship(
        "ItemPrice",
        back_populates="item",
        cascade="all, delete-orphan",
        order_by="ItemPrice.effective_from",
    )


    @property
    def display_name(self):
        return f"{self.name} · {category_short_label(self.category)}"

    @property
    def report_group(self):
        return self.display_group or self.report_bucket

    @property
    def category_label(self):
        return category_label(self.category)

    @property
    def category_short(self):
        return category_short_label(self.category)


    @property
    def needs_manual_price(self):
        return self.price_entry == PRICE_ENTRY_MANUAL

    def current_price(self, on_date=None):
        on_date = on_date or clock.local_today()
        in_effect = [p for p in self.prices if p.effective_from <= on_date]
        if not in_effect:
            return None
        newest = max(in_effect, key=lambda p: (p.effective_from, p.id is None, p.id or 0))
        return newest.unit_price_new

    def set_price(self, unit_price_new, effective_from=None):
        effective_from = effective_from or clock.local_today()
        price = ItemPrice(
            item=self,
            unit_price_new=unit_price_new,
            effective_from=effective_from,
        )
        db.session.add(price)
        return price

    def __repr__(self):
        return f"<Item {self.name} ({self.category})>"


class ItemPrice(db.Model):
    __tablename__ = "item_price"

    id = db.Column(db.Integer, primary_key=True)
    item_id = db.Column(
        db.Integer, db.ForeignKey("item.id"), nullable=False, index=True
    )

    unit_price_new = db.Column(Money, nullable=True)

    effective_from = db.Column(db.Date, nullable=False, default=clock.local_today)

    item = db.relationship("Item", back_populates="prices")

    def __repr__(self):
        return f"<ItemPrice item={self.item_id} {self.unit_price_new} from {self.effective_from}>"


__all__ = [
    "ServiceType",
    "Item",
    "ItemPrice",
    "PRICE_ENTRY_FIXED",
    "PRICE_ENTRY_MANUAL",
]
