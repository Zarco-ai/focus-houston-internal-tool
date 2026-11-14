
import datetime
import re
import time

import pytest
import sqlalchemy as sa
from flask import redirect

from app.auth import PUBLIC_ENDPOINTS, STAFF_ENDPOINTS, requires_admin
from app.config import TestConfig
from app.extensions import db, oauth
from app.models import ROLE_ADMIN, ROLE_STAFF, AppUser, AuditLog, ServiceType
from app.models.audit import ACTION_BOOTSTRAP, ACTION_LOGIN, SYSTEM_ACTIONS
from app.services import audit, batches, clock, users

HOUSTON = clock.HOUSTON


def google_says(monkeypatch, email, verified=True):
    monkeypatch.setattr(
        oauth.google,
        "authorize_access_token",
        lambda **kwargs: {"userinfo": {"email": email, "email_verified": verified}},
    )


def complete_google_sign_in(client):
    return client.get("/auth/callback")


def example_url(rule):
    values = {}
    for name in rule.arguments:
        converter = rule._converters.get(name)
        values[name] = 1 if converter and converter.__class__.__name__ == "IntegerConverter" else "x"
    return rule.build(values, append_unknown=False)[1]


def every_route(app):
    for rule in app.url_map.iter_rules():
        for method in sorted(rule.methods & {"GET", "POST"}):
            yield rule, method


def is_redirect_to_sign_in(response):
    return response.status_code == 302 and response.headers["Location"].endswith("/login")


def test_an_unauthenticated_request_is_redirected_to_login_on_every_route(anonymous_client, seeded_app):
    checked = 0
    for rule, method in every_route(seeded_app):
        if rule.endpoint in PUBLIC_ENDPOINTS:
            continue
        response = anonymous_client.open(example_url(rule), method=method)
        assert is_redirect_to_sign_in(response), f"{method} {rule.rule} -> {response.status_code}"
        checked += 1
    assert checked > 30


def test_the_public_list_is_only_sign_in_health_and_static():
    assert PUBLIC_ENDPOINTS == {
        "auth.login", "auth.start", "auth.callback", "health.check", "static",
    }


def test_an_unknown_url_tells_an_anonymous_visitor_nothing(anonymous_client):
    assert is_redirect_to_sign_in(anonymous_client.get("/no-such-page"))


def test_an_allowlisted_google_account_signs_in(anonymous_client, staff_user, monkeypatch):
    google_says(monkeypatch, staff_user.email)
    response = complete_google_sign_in(anonymous_client)

    assert response.status_code == 302
    assert response.headers["Location"].endswith("/")
    assert anonymous_client.get("/").status_code == 200


def test_a_non_allowlisted_google_account_is_refused(anonymous_client, monkeypatch):
    google_says(monkeypatch, "stranger@gmail.com")
    response = complete_google_sign_in(anonymous_client)

    assert response.status_code == 403
    assert "This account doesn" in response.get_data(as_text=True)
    assert users.find_by_email("stranger@gmail.com") is None
    assert is_redirect_to_sign_in(anonymous_client.get("/"))


def test_an_unverified_google_email_is_refused_even_if_allowlisted(anonymous_client, staff_user, monkeypatch):
    google_says(monkeypatch, staff_user.email, verified=False)
    assert complete_google_sign_in(anonymous_client).status_code == 403
    assert is_redirect_to_sign_in(anonymous_client.get("/"))


def test_sign_in_compares_emails_case_insensitively(anonymous_client, staff_user, monkeypatch):
    google_says(monkeypatch, staff_user.email.upper())
    assert complete_google_sign_in(anonymous_client).status_code == 302
    assert anonymous_client.get("/").status_code == 200


def test_a_removed_user_cannot_sign_in(anonymous_client, staff_user, monkeypatch):
    users.remove_user(staff_user)
    google_says(monkeypatch, staff_user.email)
    assert complete_google_sign_in(anonymous_client).status_code == 403


def test_signing_in_starts_a_fresh_session(anonymous_client, staff_user, monkeypatch):
    with anonymous_client.session_transaction() as session:
        session["planted"] = "by someone else"
    google_says(monkeypatch, staff_user.email)
    complete_google_sign_in(anonymous_client)

    with anonymous_client.session_transaction() as session:
        assert "planted" not in session
        assert session["user_id"] == staff_user.id


def test_sign_in_is_recorded_in_the_audit_log(anonymous_client, staff_user, monkeypatch):
    google_says(monkeypatch, staff_user.email)
    complete_google_sign_in(anonymous_client)

    entry = AuditLog.query.filter_by(action=ACTION_LOGIN).one()
    assert entry.user_id == staff_user.id
    assert db.session.get(AppUser, staff_user.id).last_login_at is not None


def test_sign_in_asks_google_for_identity_only(anonymous_client, seeded_app):
    oauth.google.server_metadata.update(
        {
            "_loaded_at": time.time(),
            "authorization_endpoint": "https://accounts.google.com/o/oauth2/v2/auth",
            "token_endpoint": "https://oauth2.googleapis.com/token",
            "jwks_uri": "https://www.googleapis.com/oauth2/v3/certs",
            "issuer": "https://accounts.google.com",
        }
    )
    response = anonymous_client.get("/login/google")
    location = response.headers["Location"]

    assert location.startswith("https://accounts.google.com/")
    assert re.search(r"[?&]scope=openid(\+|%20)email(&|$)", location)
    assert "state=" in location and "nonce=" in location
    assert "redirect_uri=http%3A%2F%2Flocalhost%2Fauth%2Fcallback" in location


def test_a_failed_google_handshake_sends_you_back_to_sign_in(anonymous_client, monkeypatch):
    from authlib.integrations.base_client.errors import OAuthError

    def fails(**kwargs):
        raise OAuthError(error="access_denied")

    monkeypatch.setattr(oauth.google, "authorize_access_token", fails)
    assert is_redirect_to_sign_in(complete_google_sign_in(anonymous_client))


@pytest.fixture
def with_admin_emails(seeded_app):
    seeded_app.config["ADMIN_EMAILS"] = frozenset({"first.admin@gmail.com"})
    return seeded_app


def test_an_admin_email_becomes_an_admin_on_first_sign_in(with_admin_emails, anonymous_client, monkeypatch):
    google_says(monkeypatch, "First.Admin@gmail.com")
    assert complete_google_sign_in(anonymous_client).status_code == 302

    user = users.find_by_email("first.admin@gmail.com")
    assert user.role == ROLE_ADMIN and user.active
    assert user.added_by_id is None
    assert AuditLog.query.filter_by(action=ACTION_BOOTSTRAP, record_id=user.id).count() == 1


def test_a_removed_admin_email_is_never_re_added_by_admin_emails(with_admin_emails, anonymous_client, monkeypatch, admin_user):
    google_says(monkeypatch, "first.admin@gmail.com")
    complete_google_sign_in(anonymous_client)
    first = users.find_by_email("first.admin@gmail.com")

    users.remove_user(first)
    fresh = with_admin_emails.test_client()
    google_says(monkeypatch, "first.admin@gmail.com")
    assert complete_google_sign_in(fresh).status_code == 403
    assert not users.find_by_email("first.admin@gmail.com").active

    users.add_user("first.admin@gmail.com", ROLE_ADMIN)
    assert complete_google_sign_in(with_admin_emails.test_client()).status_code == 302


def test_staff_cannot_open_admin_routes(staff_client, seeded_app):
    checked = 0
    for rule, method in every_route(seeded_app):
        if not requires_admin(rule.endpoint):
            continue
        response = staff_client.open(example_url(rule), method=method)
        assert response.status_code == 403, f"{method} {rule.rule} -> {response.status_code}"
        checked += 1
    assert checked >= 8


def test_staff_can_read_the_catalog_but_not_edit_it(staff_client):
    assert staff_client.get("/admin/items").status_code == 200
    assert staff_client.get("/admin/needs-price").status_code == 200
    assert staff_client.get("/admin/items/new").status_code == 403
    assert staff_client.get("/admin/items/1").status_code == 403
    assert staff_client.get("/admin/users").status_code == 403

    page = staff_client.get("/admin/items").get_data(as_text=True)
    assert "Add an item" not in page
    assert ">Edit<" not in page


def test_staff_can_use_every_non_admin_page(staff_client):
    for path in ("/", "/totals", "/review-queue"):
        assert staff_client.get(path).status_code == 200, path


def test_staff_see_no_admin_links(staff_client):
    page = staff_client.get("/").get_data(as_text=True)
    for path in ("/admin/users", "/admin/settings", 'href="/intake"', 'href="/responses"'):
        assert path not in page, path


def test_every_staff_endpoint_exists(app):
    assert STAFF_ENDPOINTS <= set(app.view_functions)


@pytest.mark.parametrize("path", ["/intake", "/responses", "/batches", "/reports/monthly",
                                  "/totals/export.csv", "/admin/settings"])
def test_staff_only_count_review_and_see_totals(staff_client, path):
    assert staff_client.get(path).status_code == 403


def test_staff_see_the_range_and_metrics_but_not_the_rest_of_totals(staff_client, client):
    staff_page = staff_client.get("/totals").get_data(as_text=True)
    admin_page = client.get("/totals").get_data(as_text=True)
    for hidden in ("Reports &amp; exports", "By report bucket", "Batches in this range"):
        assert hidden in admin_page and hidden not in staff_page, hidden
    assert 'class="period-form' in staff_page and 'class="report"' in staff_page


def test_a_deactivated_user_is_out_on_the_next_request(staff_client, staff_user):
    assert staff_client.get("/").status_code == 200
    users.remove_user(staff_user)
    assert is_redirect_to_sign_in(staff_client.get("/"))


def test_a_role_change_takes_effect_on_the_next_request(client, admin_user):
    second_admin = users.add_user("second@example.org", ROLE_ADMIN)
    assert second_admin.is_admin
    assert client.get("/admin/users").status_code == 200

    users.set_role(admin_user, ROLE_STAFF)
    assert client.get("/admin/users").status_code == 403


def test_a_session_older_than_12_hours_is_signed_out(seeded_app, staff_user, sign_in_as):
    browser = seeded_app.test_client()
    signed_in = datetime.datetime(2026, 10, 20, 7, 0, tzinfo=HOUSTON)
    with clock.freeze(signed_in):
        sign_in_as(browser, staff_user)

    with clock.freeze(signed_in + datetime.timedelta(hours=11, minutes=59)):
        assert browser.get("/").status_code == 200
    with clock.freeze(signed_in + datetime.timedelta(hours=12, minutes=1)):
        assert is_redirect_to_sign_in(browser.get("/"))
    with clock.freeze(signed_in + datetime.timedelta(hours=1)):
        assert is_redirect_to_sign_in(browser.get("/"))


def test_signing_out_needs_a_post_and_ends_the_session(client):
    assert client.get("/logout").status_code == 405
    response = client.post("/logout")
    assert is_redirect_to_sign_in(response)
    assert is_redirect_to_sign_in(client.get("/"))


def test_an_admin_can_add_change_and_remove_people(client, admin_user):
    client.post("/admin/users/add", data={"email": "New.Person@Gmail.com", "role": "staff"})
    person = users.find_by_email("new.person@gmail.com")
    assert person is not None and person.role == ROLE_STAFF
    assert person.added_by_id == admin_user.id

    client.post(f"/admin/users/{person.id}/role", data={"role": "admin"})
    assert db.session.get(AppUser, person.id).role == ROLE_ADMIN

    client.post(f"/admin/users/{person.id}/remove", data={"confirm": "yes"})
    assert not db.session.get(AppUser, person.id).active


def test_removing_someone_needs_the_confirm_box(client):
    person = users.add_user("someone@example.org", ROLE_STAFF)
    client.post(f"/admin/users/{person.id}/remove", data={})
    assert db.session.get(AppUser, person.id).active


def test_the_last_active_admin_cannot_remove_or_demote_themselves(client, admin_user):
    with pytest.raises(users.UserError):
        users.remove_user(admin_user)
    with pytest.raises(users.UserError):
        users.set_role(admin_user, ROLE_STAFF)

    client.post(f"/admin/users/{admin_user.id}/remove", data={"confirm": "yes"})
    assert db.session.get(AppUser, admin_user.id).active
    assert client.get("/admin/users").status_code == 200


def test_an_admin_can_remove_themselves_while_another_admin_remains(client, admin_user):
    users.add_user("other.admin@example.org", ROLE_ADMIN)
    client.post(f"/admin/users/{admin_user.id}/remove", data={"confirm": "yes"})

    assert not db.session.get(AppUser, admin_user.id).active
    assert is_redirect_to_sign_in(client.get("/"))


def test_adding_someone_who_already_has_access_is_refused(app, admin_user):
    with pytest.raises(users.UserError):
        users.add_user(admin_user.email.upper(), ROLE_STAFF)


@pytest.mark.parametrize("bad", ["", "no-at-sign", "two@@example.org", "a@b", "has space@example.org"])
def test_obviously_wrong_emails_are_refused(app, bad):
    with pytest.raises(users.UserError):
        users.add_user(bad, ROLE_STAFF)


def test_the_break_glass_command_restores_an_admin(seeded_app, admin_user):
    users.add_user("director@example.org", ROLE_ADMIN)
    director = users.find_by_email("director@example.org")
    users.remove_user(director)

    result = seeded_app.test_cli_runner().invoke(
        args=["users", "grant-admin", "Director@Example.org"]
    )
    assert result.exit_code == 0, result.output
    assert db.session.get(AppUser, director.id).active
    assert AuditLog.query.filter_by(action=ACTION_BOOTSTRAP, record_id=director.id).count() == 1


def test_every_audited_change_records_the_user(client, admin_user):
    client.post("/admin/items/new", data={
        "name": "Diapers Size 3", "category": "diapers", "report_bucket": "diapers",
        "used_multiplier": "0.5", "price_entry": "fixed", "unit_price_new": "0.44",
    })
    from app.models import Item

    item = Item.query.filter_by(name="Diapers Size 3").one()
    client.post(f"/admin/items/{item.id}/price", data={"unit_price_new": "0.50"})
    client.post("/admin/users/add", data={"email": "helper@example.org", "role": "staff"})

    rows = AuditLog.query.all()
    assert len(rows) >= 3
    for row in rows:
        if row.action in SYSTEM_ACTIONS:
            continue
        assert row.user_id == admin_user.id, row


def test_writing_an_audit_row_with_nobody_signed_in_is_refused(app):
    with audit.acting_as(None):
        with pytest.raises(audit.NoActor):
            audit.record_change("item", 1, "name", "a", "b")


def test_the_database_itself_refuses_an_audit_row_with_no_user(app):
    db.session.add(AuditLog(table_name="item", record_id=1, field="name",
                            old_value="a", new_value="b", action="update", user_id=None))
    with pytest.raises(sa.exc.IntegrityError):
        db.session.commit()
    db.session.rollback()


class RateLimitedConfig(TestConfig):
    RATELIMIT_ENABLED = True
    SIGN_IN_RATE_LIMIT = "3 per minute"


def test_sign_in_is_rate_limited(make_app, monkeypatch):
    limited = make_app(RateLimitedConfig)
    with limited.app_context():
        monkeypatch.setattr(oauth.google, "authorize_redirect",
                            lambda uri: redirect("https://accounts.google.com/"))
        browser = limited.test_client()
        statuses = [browser.get("/login/google").status_code for _ in range(4)]

    assert statuses == [302, 302, 302, 429]


def test_the_health_check_is_public_and_says_only_ok(anonymous_client):
    response = anonymous_client.get("/healthz")
    assert response.status_code == 200
    assert response.get_data(as_text=True) == "ok"


def test_staff_can_open_and_edit_an_approved_batch(staff_client, seeded_app):
    batch = batches.open_batch(service_type_id=ServiceType.query.first().id)
    batches.set_attendance(batch, {"children_served": 2})
    batches.commit_batch(batch)

    assert staff_client.get(f"/batches/{batch.id}").status_code == 200
    assert staff_client.get(f"/batch/{batch.id}/review").status_code == 200
    staff_client.post(f"/batch/{batch.id}/attendance", data={"children_served": "3"})
    db.session.refresh(batch.attendance)
    assert batch.attendance.children_served == 3
