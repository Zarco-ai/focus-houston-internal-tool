
import logging

import sqlalchemy as sa
from flask import Blueprint

from app.extensions import db, limiter

health_bp = Blueprint("health", __name__)
log = logging.getLogger(__name__)


@health_bp.route("/healthz")
@limiter.exempt
def check():
    try:
        db.session.execute(sa.select(1))
    except sa.exc.SQLAlchemyError:
        log.exception("Health check: the database did not answer.")
        db.session.rollback()
        return "database unavailable", 503, {"Content-Type": "text/plain"}
    return "ok", 200, {"Content-Type": "text/plain"}
