"""Users & Access: who may sign in, and as what. Admins only -- the sign-in
guard (app/auth.py) enforces that for this whole blueprint, on the server.

Every change here is written to the audit log with the admin who made it.
The rules (the last admin can't be removed, ADMIN_EMAILS never re-adds a
removed admin) live in app/services/users.py.
"""

from flask import Blueprint, flash, g, redirect, render_template, request, url_for

from app.extensions import db
from app.models import ROLES, AppUser
from app.services import audit, users

users_bp = Blueprint("users", __name__, url_prefix="/admin/users")


@users_bp.route("")
def index():
    return render_template(
        "admin/users.html",
        people=users.all_users(),
        roles=ROLES,
        history=audit.recent_changes_for_table("app_user", limit=50),
    )


@users_bp.route("/add", methods=["POST"])
def add():
    try:
        person = users.add_user(request.form.get("email"), request.form.get("role"))
    except users.UserError as error:
        db.session.rollback()
        flash(str(error), "error")
    else:
        flash(f"{person.email} can now sign in as {person.role_label.lower()}.", "success")
    return redirect(url_for("users.index"))


@users_bp.route("/<int:user_id>/role", methods=["POST"])
def change_role(user_id):
    person = db.get_or_404(AppUser, user_id)
    try:
        changed = users.set_role(person, request.form.get("role"))
    except users.UserError as error:
        db.session.rollback()
        flash(str(error), "error")
    else:
        if changed:
            flash(f"{person.email} is now {person.role_label.lower()}.", "success")
    return redirect(url_for("users.index"))


@users_bp.route("/<int:user_id>/remove", methods=["POST"])
def remove(user_id):
    person = db.get_or_404(AppUser, user_id)
    if request.form.get("confirm") != "yes":
        flash("Tick the box to confirm removing access.", "error")
        return redirect(url_for("users.index"))
    try:
        users.remove_user(person)
    except users.UserError as error:
        db.session.rollback()
        flash(str(error), "error")
        return redirect(url_for("users.index"))

    if person.id == g.user.id:
        # Removing yourself works, and takes effect now: the guard sees an
        # inactive user on the next request and signs them out.
        flash("You removed your own access.", "success")
    else:
        flash(f"{person.email} can no longer sign in.", "success")
    return redirect(url_for("users.index"))
