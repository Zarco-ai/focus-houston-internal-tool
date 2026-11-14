
import datetime

import sqlalchemy as sa
from sqlalchemy import CheckConstraint

from app.constants import (
    CONDITION_NEW,
    STALE_DRAFT_DAYS,
    STATUS_COMMITTED,
    STATUS_DRAFT,
    category_short_label,
)
from app.extensions import db
from app.models.money import Money
from app.services import clock, valuation


class Batch(db.Model):
    __tablename__ = "batch"

    id = db.Column(db.Integer, primary_key=True)
    date = db.Column(
        db.Date, nullable=False, default=clock.local_today, index=True
    )
    service_type_id = db.Column(
        db.Integer, db.ForeignKey("service_type.id"), nullable=True
    )

    note = db.Column(db.Text, nullable=True)

    status = db.Column(
        db.String(20), nullable=False, default=STATUS_DRAFT, index=True
    )
    committed_at = db.Column(db.DateTime(timezone=True), nullable=True)

    service_type = db.relationship("ServiceType")
    lines = db.relationship(
        "LineItem",
        back_populates="batch",
        cascade="all, delete-orphan",
        order_by="LineItem.id",
    )
    attendance = db.relationship(
        "Attendance",
        back_populates="batch",
        uselist=False,
        cascade="all, delete-orphan",
    )


    @property
    def is_draft(self):
        return self.status == STATUS_DRAFT

    @property
    def is_committed(self):
        return self.status == STATUS_COMMITTED

    @property
    def is_stale(self):
        if not self.is_draft:
            return False
        cutoff = clock.local_today() - datetime.timedelta(days=STALE_DRAFT_DAYS)
        return self.date <= cutoff


    @property
    def active_lines(self):
        return [line for line in self.lines if line.quantity > 0]

    @property
    def item_count(self):
        return sum(line.quantity for line in self.active_lines)

    @property
    def total_value(self):
        total = valuation.ZERO
        for line in self.active_lines:
            if line.computed_value is not None:
                total += line.computed_value
        return total

    @property
    def unpriced_lines(self):
        return [line for line in self.active_lines if line.needs_price]

    @property
    def needs_review(self):
        return bool(self.unpriced_lines)

    @property
    def flagged_lines(self):
        return [line for line in self.active_lines if line.needs_review]

    def __repr__(self):
        return f"<Batch {self.id} {self.date} {self.status}>"


class LineItem(db.Model):
    __tablename__ = "line_item"
    __table_args__ = (
        CheckConstraint("quantity >= 0", name="ck_line_item_quantity_not_negative"),
    )

    id = db.Column(db.Integer, primary_key=True)
    batch_id = db.Column(
        db.Integer, db.ForeignKey("batch.id"), nullable=False, index=True
    )

    item_id = db.Column(db.Integer, db.ForeignKey("item.id"), nullable=True)
    custom_name = db.Column(db.String(200), nullable=True)
    custom_note = db.Column(db.Text, nullable=True)

    quantity = db.Column(db.Integer, nullable=False, default=0)
    condition = db.Column(db.String(10), nullable=False, default=CONDITION_NEW)

    unit_price_at_time = db.Column(Money, nullable=True)
    used_multiplier_at_time = db.Column(db.Float, nullable=True)
    computed_value = db.Column(Money, nullable=True)
    report_bucket_at_time = db.Column(db.String(50), nullable=True)

    price_typed = db.Column(db.Boolean, nullable=False, default=False,
                            server_default=sa.false())
    used_rate_typed = db.Column(db.Boolean, nullable=False, default=False,
                                server_default=sa.false())

    needs_review = db.Column(db.Boolean, nullable=False, default=False, index=True)
    reviewed_at = db.Column(db.DateTime(timezone=True), nullable=True)

    batch = db.relationship("Batch", back_populates="lines")
    item = db.relationship("Item")


    @property
    def is_custom(self):
        return self.item_id is None

    @property
    def display_name(self):
        if self.item is not None:
            return self.item.display_name
        return f"{self.custom_name} · not in catalog"

    @property
    def category(self):
        return self.item.category if self.item else None

    @property
    def category_short(self):
        return category_short_label(self.item.category) if self.item else None

    @property
    def report_bucket(self):
        if self.report_bucket_at_time is not None:
            return self.report_bucket_at_time
        return self.item.report_bucket if self.item else None

    @property
    def is_priced(self):
        return self.unit_price_at_time is not None

    @property
    def needs_price(self):
        if self.quantity <= 0:
            return False
        return self.unit_price_at_time is None or self.unit_price_at_time == 0


    def recalculate(self):
        self.computed_value = valuation.line_value(
            quantity=self.quantity,
            unit_price=self.unit_price_at_time,
            used_multiplier=self.used_multiplier_at_time,
            condition=self.condition,
        )
        return self.computed_value

    def __repr__(self):
        return f"<LineItem {self.display_name} x{self.quantity} {self.condition}>"


class Attendance(db.Model):

    __tablename__ = "attendance"
    __table_args__ = tuple(
        CheckConstraint(f"{name} >= 0", name=f"ck_attendance_{name}_not_negative")
        for name in (
            "individuals_served",
            "children_served",
            "education_participants_unique",
            "education_participants_total",
            "education_sessions_held",
            "case_management_served",
            "medical_services_served",
            "temp_employment_placed",
            "meals_value",
            "volunteers_total",
            "volunteer_hours_tenths",
            "volunteer_opportunities",
            "unique_clients_event_count",
            "unique_volunteers_event_count",
            "unique_parishes_event_count",
        )
    )

    id = db.Column(db.Integer, primary_key=True)
    batch_id = db.Column(
        db.Integer, db.ForeignKey("batch.id"), nullable=False, unique=True
    )

    individuals_served = db.Column(db.Integer, nullable=False, default=0, server_default="0")
    children_served = db.Column(db.Integer, nullable=False, default=0, server_default="0")

    education_participants_unique = db.Column(db.Integer, nullable=False, default=0, server_default="0")
    education_participants_total = db.Column(db.Integer, nullable=False, default=0, server_default="0")
    education_sessions_held = db.Column(db.Integer, nullable=False, default=0, server_default="0")
    case_management_served = db.Column(db.Integer, nullable=False, default=0, server_default="0")
    medical_services_served = db.Column(db.Integer, nullable=False, default=0, server_default="0")

    temp_employment_placed = db.Column(db.Integer, nullable=False, default=0, server_default="0")
    meals_value = db.Column(Money, nullable=False, default=0, server_default="0")

    volunteers_total = db.Column(db.Integer, nullable=False, default=0, server_default="0")
    volunteer_hours_tenths = db.Column(db.Integer, nullable=False, default=0, server_default="0")
    volunteer_opportunities = db.Column(db.Integer, nullable=False, default=0, server_default="0")

    unique_clients_event_count = db.Column(db.Integer, nullable=False, default=0, server_default="0")
    unique_volunteers_event_count = db.Column(db.Integer, nullable=False, default=0, server_default="0")
    unique_parishes_event_count = db.Column(db.Integer, nullable=False, default=0, server_default="0")

    batch = db.relationship("Batch", back_populates="attendance")

    @property
    def has_any_count(self):
        from app.services.attendance import FIELDS

        return any(getattr(self, field.name) for field in FIELDS)

    def __repr__(self):
        return f"<Attendance batch={self.batch_id}>"
