"""The web app (design §9): login, RBAC 404s, My week, the account trace, case actions,
webhooks and the tick endpoint, against the small trained world after a Monday sweep."""

import dataclasses
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from psycopg.rows import dict_row

from radar import clock
from radar.agent import brief, handlers
from radar.agent.jobs import Job
from radar.config import load_settings
from radar.db import Database, owner_connect
from radar.ingest import webhooks
from radar.scoring import score_tenant
from radar.sim.names import INJECTION_ACCOUNT_NAME
from radar.tenants import tenant_id
from radar.web.app import CSP, create_app
from tests.conftest import (
    WEB_PASSWORD,
    WORKER_PASSWORD,
    _role_dsn,
    add_event,
    fresh_database,
)
from tests.payloads import golden

MONDAY = datetime(2026, 10, 19, 12, tzinfo=UTC)  # 08:00 New York, 13:00 London, 05:00 LA
HEARTH, TALLY, KITE = (tenant_id(s) for s in ("hearthline", "tallyworks", "kitewire"))
PASSCODE = "test-passcode"
SECRETS = {"hearthline-1": "whsec_h_live", "tallyworks-1": "whsec_t_live"}


def settings(**changes):
    return dataclasses.replace(load_settings(), **{
        "secret_key": "test-secret", "demo_passcode": PASSCODE, "webhook_secrets": SECRETS,
        "tick_secret": "tick-secret", "tick_budget_s": 5.0, "production": False} | changes)


@pytest.fixture(scope="module")
def web(trained_world_db):
    """Hearthline and tallyworks swept on a Monday (lists frozen, backlog opened) after a
    run a week earlier; template briefs for one CSM's list; kitewire left unswept."""
    w, db = trained_world_db, trained_world_db["db"]
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("RADAR_DATA_DIR", str(w["data_dir"]))
        mp.delenv("ANTHROPIC_API_KEY", raising=False)
        clock.freeze(MONDAY)
        score_tenant(db, HEARTH, datetime(2026, 10, 12, 12, tzinfo=UTC), "backfill", False)
        for tid in (HEARTH, TALLY):
            handlers.daily_sweep(db, Job(0, tid, "daily_sweep",
                                         {"period": "2026-10-19", "bootstrap": True}, 1, MONDAY))
        owner = owner_connect(w["owner_dsn"], autocommit=True, row_factory=dict_row)
        csm = owner.execute(  # the hearthline CSM with the most drafts-to-be on their list
            "SELECT w.user_id, u.manager_id FROM worklists w JOIN users u ON u.id = w.user_id"
            " JOIN worklist_items i ON i.worklist_id = w.id WHERE w.tenant_id = %s"
            " AND i.case_id IS NOT NULL GROUP BY 1, 2 ORDER BY count(*) DESC, 1 LIMIT 1",
            (HEARTH,)).fetchone()
        cases = [r["case_id"] for r in owner.execute(
            "SELECT i.case_id FROM worklist_items i JOIN worklists w ON w.id = i.worklist_id"
            " WHERE w.user_id = %s AND i.case_id IS NOT NULL", (csm["user_id"],))]
        for case in cases:
            brief.generate_brief(db, HEARTH, case, None)
        web_db = Database(_role_dsn(w["owner_dsn"], "radar_web", WEB_PASSWORD))
        try:
            yield SimpleNamespace(owner=owner, db=db, web_db=web_db, csm=csm["user_id"],
                                  manager=csm["manager_id"], cases=cases,
                                  app=create_app(settings(), web_db, db))
        finally:
            web_db.close()
            owner.close()
            clock.unfreeze()


def one(web, sql, *args):
    return web.owner.execute(sql, args).fetchone()


def client_as(web, user_id) -> TestClient:
    """A client logged in as the user, through the login form."""
    u = one(web, "SELECT tenant_id, id FROM users WHERE id = %s", user_id)
    c = TestClient(web.app)
    c.post("/login", data={"passcode": PASSCODE})
    assert c.post("/login/user", data={"user": f"{u['tenant_id']}:{u['id']}"}).status_code == 200
    return c


def user_of(web, tid, role):
    return one(web, "SELECT id FROM users WHERE tenant_id = %s AND role = %s ORDER BY email"
               " LIMIT 1", tid, role)["id"]


def own_account(web):
    """The first account on the CSM's frozen list."""
    return one(web, "SELECT i.account_id FROM worklist_items i JOIN worklists w"
               " ON w.id = i.worklist_id WHERE w.user_id = %s ORDER BY i.position LIMIT 1",
               web.csm)["account_id"]


def other_account(web):
    """A hearthline account owned by a CSM outside the CSM's manager's team."""
    return one(web, "SELECT a.id FROM accounts a JOIN users u ON u.id = a.owner_user_id"
               " WHERE a.tenant_id = %s AND u.manager_id <> %s ORDER BY a.name LIMIT 1",
               HEARTH, web.manager)["id"]


def test_login_requires_passcode_and_sets_session(web):
    c = TestClient(web.app)
    assert c.get("/week", follow_redirects=False).headers["location"] == "/login"
    assert c.post("/login", data={"passcode": "wrong"}).status_code == 401
    choice = {"user": f"{HEARTH}:{web.csm}"}
    assert c.post("/login/user", data=choice).status_code == 401  # passcode first
    picker = c.post("/login", data={"passcode": PASSCODE})
    assert picker.status_code == 200 and "Hearthline" in picker.text
    # a tenant and a user of another tenant: RLS finds no such user
    assert c.post("/login/user", data={"user": f"{TALLY}:{web.csm}"}).status_code == 400
    page = c.post("/login/user", data=choice)
    name = one(web, "SELECT name FROM users WHERE id = %s", web.csm)["name"]
    assert page.url.path == "/week" and name in page.text
    # the tenant never comes from the query string
    assert name in c.get("/week", params={"tenant_id": str(TALLY)}).text
    c.post("/logout")
    assert c.get("/week", follow_redirects=False).status_code == 303


ROLE_ROUTE_MATRIX = [
    (role, route, status) for role, row in {
        "csm": (200, 200, 200, 404), "manager": (200, 200, 200, 404),
        "admin": (200, 200, 200, 200)}.items()
    for route, status in zip(("/week", "/book", "own", "other"), row, strict=True)]


@pytest.mark.parametrize("role,route,status", ROLE_ROUTE_MATRIX)
def test_role_route_matrix(web, role, route, status):
    who = {"csm": web.csm, "manager": web.manager, "admin": user_of(web, HEARTH, "admin")}
    path = {"own": f"/accounts/{own_account(web)}",
            "other": f"/accounts/{other_account(web)}"}.get(route, route)
    assert client_as(web, who[role]).get(path).status_code == status


def test_idor_other_tenant_account_is_404(web):
    c = client_as(web, user_of(web, HEARTH, "admin"))
    case = one(web, "SELECT id, account_id FROM cases WHERE tenant_id = %s AND status = 'open'"
               " LIMIT 1", TALLY)
    assert c.get(f"/accounts/{case['account_id']}").status_code == 404
    r = c.post(f"/accounts/{case['account_id']}/cases/{case['id']}/outcome",
               data={"outcome": "called", "reason": "x"})
    assert r.status_code == 404
    assert one(web, "SELECT status FROM cases WHERE id = %s", case["id"])["status"] == "open"
    # a case of another account cannot be reached through an account the user may see
    mine = own_account(web)
    r = c.post(f"/accounts/{mine}/cases/{case['id']}/outcome",
               data={"outcome": "called", "reason": "x"})
    assert r.status_code == 404


def test_week_shows_frozen_list_with_reasons_and_draft_status(web):
    page = client_as(web, web.csm).get("/week").text
    items = web.owner.execute(
        "SELECT a.name, i.kind, i.reason FROM worklist_items i JOIN worklists w"
        " ON w.id = i.worklist_id JOIN accounts a ON a.id = i.account_id WHERE w.user_id = %s"
        " ORDER BY i.position", (web.csm,)).fetchall()
    assert items and all(i["name"].replace("&", "&amp;") in page for i in items)
    assert "Data freshness" in page and "Week of 2026-10-19" in page
    assert "Draft ready" in page  # template briefs wrote customer drafts
    must = [i for i in items if i["kind"] == "must_act"]
    assert all(i["reason"].replace("_", " ") in page for i in must)


def test_account_trace_shows_run_model_watermarks_and_recompute_ok(web):
    account = own_account(web)
    c = client_as(web, web.csm)
    page = c.get(f"/accounts/{account}").text
    run = one(web, "SELECT r.id, r.crosswalk_watermark, m.version FROM latest_scores s"
              " JOIN score_runs r ON r.tenant_id = s.tenant_id AND r.id = s.run_id"
              " JOIN models m ON m.tenant_id = r.tenant_id AND m.id = r.model_id"
              " WHERE s.account_id = %s", account)
    assert str(run["id"]) in page and f"Model v{run['version']}" in page
    assert "Crosswalk watermark" in page and "usage_drop" in page and "zendesk" in page
    assert "What changed since" in page
    checked = c.post(f"/accounts/{account}/recompute").text
    assert "Recompute from raw: match" in checked


def test_timeline_greys_late_records_and_badges_heuristic_matches(web):
    account = own_account(web)
    clock.freeze(MONDAY + timedelta(hours=1))  # an hour after the run's as_of
    try:
        add_event(web.owner, HEARTH, account, "stripe:cus_web_late", "domain",
                  "billing.invoice", {"attempt_count": 1}, status="open", days_ago=0)
        page = client_as(web, web.csm).get(f"/accounts/{account}").text
    finally:
        clock.freeze(MONDAY)
    late = page.split('<li class="late">', 1)[1].split("</li>", 1)[0]
    assert "Invoice open" in late and "arrived after the run" in late
    assert "matched by name/domain" in late


def test_ticket_html_is_escaped(web):
    account = one(web, "SELECT id FROM accounts WHERE tenant_id = %s AND name = %s",
                  HEARTH, INJECTION_ACCOUNT_NAME)["id"]
    page = client_as(web, user_of(web, HEARTH, "admin")).get(f"/accounts/{account}").text
    assert "&lt;img src=x onerror" in page and "<img src=x onerror" not in page


def test_pages_render_empty_states(web):
    kite = client_as(web, user_of(web, KITE, "csm"))
    assert "not frozen yet" in kite.get("/week").text
    assert "No active model" in kite.get("/book").text
    csm = user_of(web, KITE, "csm")  # an account created after every run has no score
    account = one(web, "INSERT INTO accounts (tenant_id, sf_account_id, name, owner_user_id)"
                  " VALUES (%s, '001KITEWEBNEW00000', 'Fresh Signup Ltd', %s) RETURNING id",
                  KITE, csm)
    page = kite.get(f"/accounts/{account['id']}")
    assert page.status_code == 200 and "Not scored yet" in page.text
    new = one(web, "INSERT INTO users (tenant_id, email, name, role) VALUES"
              " (%s, 'new.csm@hearthline.io', 'New CSM', 'csm') RETURNING id", HEARTH)["id"]
    c = client_as(web, new)
    assert "You have no accounts" in c.get("/book").text
    assert "No list for you this week" in c.get("/week").text


def open_case_of(web):
    """An open case on the CSM's list, and its account."""
    return one(web, "SELECT c.id, c.account_id FROM worklist_items i JOIN cases c"
               " ON c.id = i.case_id JOIN worklists w ON w.id = i.worklist_id"
               " WHERE w.user_id = %s AND c.status = 'open' ORDER BY i.position LIMIT 1",
               web.csm)


def test_outcome_logging_closes_case_and_audits(web):
    case = open_case_of(web)
    c = client_as(web, web.csm)
    url = f"/accounts/{case['account_id']}/cases/{case['id']}/outcome"
    assert c.post(url, data={"outcome": "sent_flowers"}).status_code == 400
    assert c.post(url, data={"outcome": "no_action"}).status_code == 400  # needs a reason
    assert c.post(url, data={"outcome": "called", "reason": "spoke to the champion"}).status_code \
        == 200
    row = one(web, "SELECT * FROM cases WHERE id = %s", case["id"])
    assert (row["status"], row["outcome"], row["outcome_reason"], row["closed_by"]) == (
        "done", "called", "spoke to the champion", web.csm)
    audit = one(web, "SELECT actor FROM audit_log WHERE entity_type = 'cases' AND entity_id = %s"
                " AND after->>'status' = 'done'", str(case["id"]))
    assert audit["actor"] == f"user:{web.csm}"


def test_dismissal_creates_a_suppression(web):
    case = open_case_of(web)
    c = client_as(web, web.csm)
    url = f"/accounts/{case['account_id']}/cases/{case['id']}/dismiss"
    assert c.post(url, data={}).status_code == 400  # a reason is required
    assert c.post(url, data={"reason": "not_a_risk"}).status_code == 200
    assert one(web, "SELECT status FROM cases WHERE id = %s", case["id"])["status"] == "dismissed"
    s = one(web, "SELECT * FROM suppressions WHERE account_id = %s AND lifted_at IS NULL",
            case["account_id"])
    assert (s["created_by"], s["reason"]) == (web.csm, "not_a_risk")


def test_draft_edit_then_approve_sends_the_filled_preview(web):
    draft = one(web, "SELECT o.id, a.id AS account_id, t.first_name FROM outbox o"
                " JOIN cases c ON c.id = o.case_id JOIN accounts a ON a.id = c.account_id"
                " JOIN contacts t ON t.id = o.to_contact_id WHERE o.case_id = ANY(%s)"
                " AND o.status = 'draft' AND o.kind = 'customer_email' LIMIT 1", web.cases)
    c = client_as(web, web.csm)
    base = f"/accounts/{draft['account_id']}"
    name = one(web, "SELECT name FROM users WHERE id = %s", web.csm)["name"]
    assert f"Hi {draft['first_name']}," in c.get(base).text  # the filled preview
    edited = "Hi {first_name},\n\nShort note from us.\n\n{sender_name}"
    c.post(f"{base}/drafts/{draft['id']}/edit", data={"subject": "Hello", "body": edited})
    assert one(web, "SELECT body FROM outbox WHERE id = %s", draft["id"])["body"] == edited
    assert c.post(f"{base}/drafts/{draft['id']}/approve").status_code == 200
    sent = one(web, "SELECT * FROM outbox WHERE id = %s", draft["id"])
    assert (sent["status"], sent["approved_by"], sent["subject"]) == ("sent", web.csm, "Hello")
    assert sent["body"] == f"Hi {draft['first_name']},\n\nShort note from us.\n\n{name}"
    other = client_as(web, user_of(web, KITE, "admin"))  # another tenant cannot approve it
    assert other.post(f"{base}/drafts/{draft['id']}/approve").status_code == 404


def post_webhook(c, body: bytes, secret: str, kid: str, header="stripe-signature"):
    sig = webhooks.sign(secret, kid, body, int(clock.now().timestamp()))
    return c.post("/webhooks/stripe", content=body, headers={header: sig})


def test_webhook_rejects_bad_signature_and_routes_tenant_by_kid(web):
    c = TestClient(web.app)
    event = golden("stripe_customer_created_basil")["body"] | {"id": "evt_web_route_1"}
    # not the canonical bytes: verified over the raw body as received
    body = json.dumps(event, indent=1).encode()
    landed = "SELECT tenant_id FROM raw_records WHERE source_record_id = 'evt_web_route_1'"
    assert post_webhook(c, body, "wrong", "tallyworks-1").status_code == 401
    # the demo fallback secret is for simulated feed records only
    assert post_webhook(c, body, "whsec_demo_tallyworks", "tallyworks-stripe").status_code == 401
    assert web.owner.execute(landed).fetchall() == []
    assert post_webhook(c, body, SECRETS["tallyworks-1"], "tallyworks-1",
                        header="Stripe-Signature").status_code == 202
    assert [r["tenant_id"] for r in web.owner.execute(landed)] == [TALLY]
    quarantined = one(web, "SELECT count(*) AS n FROM quarantine WHERE reason = 'bad_signature'"
                      " AND details->'payload'->>'id' = 'evt_web_route_1'")
    assert quarantined["n"] == 0


def test_tick_endpoint_requires_the_bearer(web):
    with fresh_database() as dsn:
        app = create_app(settings(worker_dsn=_role_dsn(dsn, "radar_worker", WORKER_PASSWORD)),
                         web.web_db)  # the worker Database is created on first use
        c = TestClient(app)
        try:
            assert c.post("/internal/tick").status_code == 401
            bad = {"Authorization": "Bearer nope"}
            assert c.get("/internal/tick", headers=bad).status_code == 401
            ok = {"Authorization": "Bearer tick-secret"}
            for method in (c.get, c.post):  # Vercel cron GETs, GitHub Actions POSTs
                r = method("/internal/tick", headers=ok)
                assert r.status_code == 200 and r.json()["jobs"] == 0
        finally:
            app.state.db_worker.close()
    unset = TestClient(create_app(settings(tick_secret=None), web.web_db, web.db))
    assert unset.post("/internal/tick", headers=ok).status_code == 503


def test_csp_header_present(web):
    r = TestClient(web.app).get("/login")
    assert r.headers["content-security-policy"] == CSP
    assert CSP == ("default-src 'self'; style-src 'self'; img-src 'self' data:;"
                   " object-src 'none'; base-uri 'none'; form-action 'self';"
                   " frame-ancestors 'none'")
    health = TestClient(web.app).get("/healthz")
    assert health.status_code == 200 and health.json() == {"ok": True}
