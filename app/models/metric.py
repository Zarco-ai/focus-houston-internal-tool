"""What an admin has chosen about each report metric, and the figures typed
once a year.

  metric_definition    One row per metric an admin has touched: its label
                       and whether it is switched on. Everything else about
                       a metric -- its group, its unit, its definition and
                       the math -- is code, in app/services/metrics.py. A
                       metric nobody has touched has no row and shows with
                       its default label, switched on (Phase 0 answer 16).

  annual_metric_value  The ANNUAL METRICS group: advisory board members,
                       donors and so on. Nothing in the app counts these, so
                       an admin types them, once per year, and each change is
                       audited.
"""

from sqlalchemy import CheckConstraint, UniqueConstraint

from app.extensions import db
from app.services import clock


class MetricDefinition(db.Model):
    __tablename__ = "metric_definition"

    # An integer id as well as the key, because the audit log points at rows
    # by integer id.
    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(64), nullable=False, unique=True)
    # NULL means "the default label from the code".
    label = db.Column(db.String(200), nullable=True)
    enabled = db.Column(db.Boolean, nullable=False, default=True)

    def __repr__(self):
        return f"<MetricDefinition {self.key} {'on' if self.enabled else 'off'}>"


class AnnualMetricValue(db.Model):
    __tablename__ = "annual_metric_value"
    __table_args__ = (
        UniqueConstraint("year", "metric_key", name="uq_annual_metric_value_year_key"),
        CheckConstraint("value >= 0", name="ck_annual_metric_value_not_negative"),
        CheckConstraint("year BETWEEN 2000 AND 2100", name="ck_annual_metric_value_year"),
    )

    id = db.Column(db.Integer, primary_key=True)
    year = db.Column(db.Integer, nullable=False)
    metric_key = db.Column(db.String(64), nullable=False)
    value = db.Column(db.Integer, nullable=False)
    entered_by_id = db.Column(db.Integer, db.ForeignKey("app_user.id"), nullable=False)
    entered_at = db.Column(db.DateTime(timezone=True), nullable=False,
                           default=clock.now_utc, onupdate=clock.now_utc)

    entered_by = db.relationship("AppUser")

    def __repr__(self):
        return f"<AnnualMetricValue {self.year} {self.metric_key}={self.value}>"
