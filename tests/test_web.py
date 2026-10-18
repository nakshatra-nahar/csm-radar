"""The web app (design §9): login, RBAC 404s, My week, the account trace, case actions,
webhooks and the tick endpoint, against the small trained world after a Monday sweep."""

import dataclasses
import json
import re
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from radar import clock, policy, registry
from radar.agent import brief, handlers, jobs, worker
from radar.agent.jobs import Job
from radar.bootstrap import seed_tenants_and_users
from radar.config import load_settings
from radar.db import Database, owner_connect
from radar.ingest import webhooks
from radar.scoring import score_tenant
from radar.sim.names import INJECTION_ACCOUNT_NAME
from radar.tenants import tenant_id
from radar.web import queries
from radar.web.app import CSP, create_app
from tests.conftest import (
    WEB_PASSWORD,
    WORKER_PASSWORD,
    _role_dsn,
    add_case,
    add_event,
    fresh_database,
    train_world,
    world_database,
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
def web(small_world_dir):
    """Its own trained small world (the web tests write to it), with hearthline and
    tallyworks swept on a Monday (lists frozen, backlog opened) after a run a week earlier;
    template briefs for one CSM's list; kitewire left unswept."""
    with world_database(small_world_dir) as world:
        yield from _swept(train_world(world))


def _swept(w):
    db = w["db"]
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


def client_as(web, user_id, **kwargs) -> TestClient:
    """A client logged in as the user, through the login form."""
    u = one(web, "SELECT tenant_id, id FROM users WHERE id = %s", user_id)
    c = TestClient(web.app, **kwargs)
    c.post("/login", data={"passcode": PASSCODE})
    assert c.post("/login/user", data={"user": f"{u['tenant_id']}:{u['id']}"}).status_code == 200
    return c


def user_of(web, tid, role):
    return one(web, "SELECT id FROM users WHERE tenant_id = %s AND role = %s AND active"
               " AND sf_user_id IS NOT NULL ORDER BY email LIMIT 1", tid, role)["id"]


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


ROUTES = ("/week", "/book", "/team", "/ops", "/model", "/dictionary", "own", "other",
          "promote")
ROLE_ROUTE_MATRIX = [
    (role, route, status) for role, row in {
        "csm": (200, 200, 404, 404, 200, 200, 200, 404, 404),
        "manager": (200, 200, 200, 404, 200, 200, 200, 404, 404),
        "admin": (200, 200, 200, 200, 200, 200, 200, 200, 200)}.items()
    for route, status in zip(ROUTES, row, strict=True)]


def candidate(web, coef_scale=1.0):
    """A new hearthline candidate model: the active artifact with its coefficients scaled."""
    active = one(web, "SELECT artifact, manifest FROM models WHERE tenant_id = %s"
                 " AND status = 'active'", HEARTH)
    artifact = active["artifact"] | {"coef": [c * coef_scale for c in active["artifact"]["coef"]]}
    return one(web, "INSERT INTO models (tenant_id, version, status, artifact, manifest)"
               " SELECT %s, max(version) + 1, 'candidate', %s, %s FROM models"
               " WHERE tenant_id = %s RETURNING id", HEARTH, Jsonb(artifact),
               Jsonb(active["manifest"]), HEARTH)["id"]


@pytest.mark.parametrize("role,route,status", ROLE_ROUTE_MATRIX)
def test_role_route_matrix(web, role, route, status):
    who = {"csm": web.csm, "manager": web.manager, "admin": user_of(web, HEARTH, "admin")}
    c = client_as(web, who[role])
    if route == "promote":
        model = candidate(web)
        assert c.post(f"/admin/models/{model}/promote").status_code == status
        promoted = one(web, "SELECT status FROM models WHERE id = %s", model)["status"]
        assert promoted == ("active" if status == 200 else "candidate")
        return
    path = {"own": f"/accounts/{own_account(web)}",
            "other": f"/accounts/{other_account(web)}"}.get(route, route)
    assert c.get(path).status_code == status


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


def new_case(web, account_id=None):
    """A fresh open case on one of the CSM's accounts (a unique kind, so tests never share one)."""
    account = account_id or own_account(web)
    case = add_case(web.owner, HEARTH, account, f"test_{uuid.uuid4().hex[:8]}", "save_call", [])
    return {"id": case, "account_id": account}


def test_outcome_logging_closes_case_and_audits(web):
    case = new_case(web)
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
    case = new_case(web)
    c = client_as(web, web.csm)
    url = f"/accounts/{case['account_id']}/cases/{case['id']}/dismiss"
    assert c.post(url, data={}).status_code == 400  # a reason is required
    assert c.post(url, data={"reason": "not_a_risk"}).status_code == 200
    assert one(web, "SELECT status FROM cases WHERE id = %s", case["id"])["status"] == "dismissed"
    audit = one(web, "SELECT after FROM audit_log WHERE entity_type = 'suppressions'"
                " AND after->>'account_id' = %s ORDER BY id DESC LIMIT 1",
                str(case["account_id"]))["after"]
    assert (audit["created_by"], audit["reason"]) == (str(web.csm), "not_a_risk")


def test_draft_edit_then_approve_sends_the_filled_preview(web):
    draft = one(web, "SELECT o.id, a.id AS account_id, t.first_name FROM outbox o"
                " JOIN cases c ON c.id = o.case_id JOIN accounts a ON a.id = c.account_id"
                " JOIN contacts t ON t.id = o.to_contact_id WHERE o.case_id = ANY(%s)"
                " AND o.status = 'draft' AND o.kind = 'customer_email' AND c.status = 'open'"
                " LIMIT 1", web.cases)
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


def test_tick_endpoint_requires_the_bearer_and_runs_a_job(web, tmp_path, monkeypatch):
    monkeypatch.setenv("RADAR_DATA_DIR", str(tmp_path))  # no feed: feed_tick delivers nothing
    with fresh_database() as dsn:
        worker_dsn = _role_dsn(dsn, "radar_worker", WORKER_PASSWORD)
        seeder = Database(worker_dsn)
        seed_tenants_and_users(dsn, seeder, ["kitewire"])
        app = create_app(settings(worker_dsn=worker_dsn), web.web_db)  # worker db made lazily
        c = TestClient(app)
        try:
            assert c.post("/internal/tick").status_code == 401
            bad = {"Authorization": "Bearer nope"}
            assert c.get("/internal/tick", headers=bad).status_code == 401
            ok = {"Authorization": "Bearer tick-secret"}
            for method in (c.get, c.post):  # Vercel cron GETs, GitHub Actions POSTs
                with seeder.tenant_tx(KITE) as conn:
                    job = jobs.enqueue(conn, "sla_check", KITE, {}, dedupe_key="sla_check")
                r = method("/internal/tick", headers=ok)
                assert r.status_code == 200 and r.json()["jobs"] >= 1
                with seeder.system_tx() as conn:
                    assert conn.execute("SELECT status FROM jobs WHERE id = %s",
                                        (job,)).fetchone()["status"] == "done"
            with app.state.db_worker.system_tx() as conn:
                assert conn.execute("SELECT current_user AS u").fetchone()["u"] == "radar_worker"
        finally:
            seeder.close()
            if app.state.db_worker is not None:
                app.state.db_worker.close()
    unset = TestClient(create_app(settings(tick_secret=None), web.web_db, web.db))
    assert unset.post("/internal/tick", headers=ok).status_code == 503


def test_tick_and_app_refuse_an_unsafe_worker_role(web):
    """Design §4.3: a worker DSN for the owner (a superuser here) must never run the jobs."""
    ok = {"Authorization": "Bearer tick-secret"}
    with fresh_database() as dsn:
        app = create_app(settings(worker_dsn=dsn), web.web_db)
        r = TestClient(app).post("/internal/tick", headers=ok)
        assert r.status_code == 503 and "unsafe" in r.json()["detail"]
        assert app.state.db_worker is None  # the refused pool is closed, not kept
        owner_db = Database(dsn)
        try:
            with pytest.raises(RuntimeError, match="unsafe"):
                create_app(settings(), web.web_db, owner_db)
        finally:
            owner_db.close()


def test_csp_header_present(web):
    r = TestClient(web.app).get("/login")
    assert r.headers["content-security-policy"] == CSP
    assert CSP == ("default-src 'self'; style-src 'self'; img-src 'self' data:;"
                   " object-src 'none'; base-uri 'none'; form-action 'self';"
                   " frame-ancestors 'none'")
    health = TestClient(web.app).get("/healthz")
    assert health.status_code == 200 and health.json() == {"ok": True}


# --- Part A review minors (Ruling AO) -------------------------------------------------------

def suppressions(web, account_id) -> int:
    return one(web, "SELECT count(*) AS n FROM suppressions WHERE account_id = %s",
               account_id)["n"]


def test_closing_a_case_is_race_free(web, monkeypatch):
    case = new_case(web)
    c = client_as(web, web.csm)
    base = f"/accounts/{case['account_id']}/cases/{case['id']}"
    assert c.post(f"{base}/outcome", data={"outcome": "called"}).status_code == 200
    before = suppressions(web, case["account_id"])
    # a second post that read the case as open just before the first one closed it
    monkeypatch.setattr(queries, "case_of", lambda conn, a, cid: {"id": cid, "status": "open"})
    assert c.post(f"{base}/outcome", data={"outcome": "met"}).status_code == 409
    assert c.post(f"{base}/dismiss", data={"reason": "not_a_risk"}).status_code == 409
    closes = one(web, "SELECT count(*) AS n FROM audit_log WHERE entity_type = 'cases'"
                 " AND entity_id = %s AND action = 'update'", str(case["id"]))
    assert closes["n"] == 1
    assert one(web, "SELECT outcome FROM cases WHERE id = %s", case["id"])["outcome"] == "called"
    assert suppressions(web, case["account_id"]) == before


def test_one_click_outcome_returns_to_week_and_never_off_site(web):
    c = client_as(web, web.csm)
    assert 'name="next" value="/week"' in c.get("/week").text
    for nxt, location in (("/week", "/week"), ("https://evil.example/", None), ("//evil", None)):
        case = new_case(web)
        r = c.post(f"/accounts/{case['account_id']}/cases/{case['id']}/outcome",
                   data={"outcome": "called", "next": nxt}, follow_redirects=False)
        assert r.headers["location"] == (location or f"/accounts/{case['account_id']}")


def test_pages_use_the_tenant_currency_column(web):
    """tenants.currency is the one source; the config copy must not be read."""
    web.owner.execute("UPDATE tenants SET config = jsonb_set(config, '{currency}', '\"XXX\"')"
                      " WHERE id = %s", (HEARTH,))
    try:
        page = client_as(web, web.csm).get(f"/accounts/{own_account(web)}").text
    finally:
        web.owner.execute("UPDATE tenants SET config = jsonb_set(config, '{currency}',"
                          " to_jsonb(currency)) WHERE id = %s", (HEARTH,))
    assert "XXX" not in page and "USD " in page


def test_session_user_is_rechecked_on_every_request(web):
    uid = one(web, "INSERT INTO users (tenant_id, email, name, role) VALUES (%s, %s,"
              " 'Rechecked User', 'csm') RETURNING id", HEARTH,
              f"{uuid.uuid4().hex[:8]}@hearthline.io")["id"]
    c = client_as(web, uid)
    assert c.get("/ops").status_code == 404
    web.owner.execute("UPDATE users SET role = 'admin' WHERE id = %s", (uid,))
    assert c.get("/ops").status_code == 200  # the new role applies at once
    web.owner.execute("UPDATE users SET active = false WHERE id = %s", (uid,))
    r = c.get("/book", follow_redirects=False)
    assert (r.status_code, r.headers["location"]) == (303, "/login")


def peer_csm(web):
    """Another CSM on the same manager's team."""
    return one(web, "SELECT id FROM users WHERE manager_id = %s AND id <> %s AND role = 'csm'"
               " AND active AND sf_user_id IS NOT NULL ORDER BY email LIMIT 1", web.manager,
               web.csm)["id"]


def test_in_tenant_writes_need_the_book(web):
    case = new_case(web)
    draft = one(web, "SELECT o.id, c.account_id FROM outbox o JOIN cases c ON c.id = o.case_id"
                " WHERE o.case_id = ANY(%s) AND o.status = 'draft' AND c.status = 'open'"
                " AND o.kind = 'customer_email' ORDER BY o.id DESC LIMIT 1", web.cases)
    peer = client_as(web, peer_csm(web))  # same team, someone else's book
    base = f"/accounts/{case['account_id']}/cases/{case['id']}"
    d = f"/accounts/{draft['account_id']}/drafts/{draft['id']}"
    for url, data in ((f"{base}/outcome", {"outcome": "called"}),
                      (f"{base}/dismiss", {"reason": "not_a_risk"}),
                      (f"{d}/edit", {"subject": "x", "body": "y"}), (f"{d}/approve", {})):
        assert peer.post(url, data=data).status_code == 404
    assert one(web, "SELECT status FROM cases WHERE id = %s", case["id"])["status"] == "open"
    assert one(web, "SELECT status, subject FROM outbox WHERE id = %s", draft["id"])[
        "status"] == "draft"
    manager = client_as(web, web.manager)
    assert manager.post(f"{base}/outcome", data={"outcome": "met"}).status_code == 200
    elsewhere = new_case(web, other_account(web))
    other = f"/accounts/{elsewhere['account_id']}/cases/{elsewhere['id']}/outcome"
    assert manager.post(other, data={"outcome": "met"}).status_code == 404
    admin = client_as(web, user_of(web, HEARTH, "admin"))
    assert admin.post(other, data={"outcome": "met"}).status_code == 200


def test_html_errors_render_the_page_and_machine_routes_keep_json(web, monkeypatch):
    c = client_as(web, web.csm, raise_server_exceptions=False)
    r = c.get(f"/accounts/{other_account(web)}")
    assert r.status_code == 404 and r.headers["content-type"].startswith("text/html")
    assert "Not found" in r.text and "/static/style.css" in r.text
    case = new_case(web)
    web.owner.execute("UPDATE cases SET status = 'done' WHERE id = %s", (case["id"],))
    r = c.post(f"/accounts/{case['account_id']}/cases/{case['id']}/outcome",
               data={"outcome": "called"})
    assert r.status_code == 409 and "no longer open" in r.text and "<main" in r.text
    monkeypatch.setattr(queries, "book", lambda conn, band: 1 / 0)
    r = c.get("/book")
    assert r.status_code == 500 and "Something went wrong" in r.text
    assert "ZeroDivisionError" not in r.text
    hook = TestClient(web.app).post("/webhooks/stripe", content=b"{}")
    assert hook.status_code == 401 and hook.json() == {"detail": "Unauthorized"}
    monkeypatch.setattr(worker, "run_for", lambda *args, **kwargs: 1 / 0)
    tick = TestClient(web.app, raise_server_exceptions=False).post(
        "/internal/tick", headers={"Authorization": "Bearer tick-secret"})
    assert tick.status_code == 500 and tick.json() == {"detail": "Internal Server Error"}


# --- team, ops, model card, dictionary (Part B) ---------------------------------------------

def test_team_page_shows_the_manager_team_only(web):
    client_as(web, web.csm).get("/week")  # the CSM opens this week's list
    page = client_as(web, web.manager).get("/team").text
    team = web.owner.execute("SELECT name, manager_id FROM users WHERE tenant_id = %s"
                             " AND role = 'csm' AND sf_user_id IS NOT NULL", (HEARTH,)).fetchall()
    assert all((u["name"] in page) == (u["manager_id"] == web.manager) for u in team)
    row = page.split(one(web, "SELECT name FROM users WHERE id = %s", web.csm)["name"], 1)[1]
    assert "opened " in row.split("</tr>", 1)[0]
    assert "Expected ARR at risk" in page and "Last week" in page
    assert "no run a week earlier" not in page  # the backfill run a week before the sweep
    assert "Renewals in the next 90 days" in page and "USD " in page
    assert "SLA breaches" in page and "Overflow" in page and "Dismissed this week" in page


def test_team_page_figures_match_the_database(web):
    """web.csm's row and the team's ARR at risk equal what direct SQL says."""
    case = new_case(web)
    web.owner.execute("UPDATE cases SET sla_due_at = %s WHERE id = %s",
                      (clock.now() - timedelta(days=1), case["id"]))
    wl = one(web, "SELECT id FROM worklists WHERE user_id = %s AND week_start = %s",
             web.csm, MONDAY.date())["id"]
    extra = one(web, "INSERT INTO worklist_items (tenant_id, worklist_id, account_id, position,"
                " kind, reason) SELECT %s, %s, a.id, 999, 'must_act', 'overflow:test'"
                " FROM accounts a WHERE a.owner_user_id = %s AND NOT EXISTS (SELECT 1 FROM"
                " worklist_items i WHERE i.worklist_id = %s AND i.account_id = a.id) LIMIT 1"
                " RETURNING id", HEARTH, wl, web.csm, wl)["id"]
    try:
        _check_team_row(web, wl)
    finally:
        web.owner.execute("DELETE FROM worklist_items WHERE id = %s", (extra,))
        web.owner.execute("DELETE FROM cases WHERE id = %s", (case["id"],))


def _check_team_row(web, wl):
    page = client_as(web, web.manager).get("/team").text
    cur = one(web, "SELECT currency FROM tenants WHERE id = %s", HEARTH)["currency"]
    money = lambda minor: f"{cur} {minor / 100:,.0f}"  # noqa: E731 - macros.money
    csm = one(web, "SELECT name FROM users WHERE id = %s", web.csm)["name"]
    cells = re.findall(r'<td class="n">(.*?)</td>', page.split(csm, 1)[1].split("</tr>", 1)[0])
    books = {r["band"]: r for r in web.owner.execute(
        "SELECT s.band, count(*) AS n, coalesce(sum(s.arr_minor), 0) AS arr FROM latest_scores s"
        " JOIN accounts a ON a.id = s.account_id WHERE a.owner_user_id = %s"
        " AND a.merged_into IS NULL AND NOT a.is_test GROUP BY s.band", (web.csm,)).fetchall()}
    assert cells[:4] == [f'{books[b]["n"]}<span class="arr">{money(books[b]["arr"])}</span>'
                         if b in books else "—" for b in ("red", "yellow", "green", "unknown")]
    counts = one(web, "SELECT"
                 " count(*) FILTER (WHERE c.kind = ANY(%s) AND c.status = 'open') AS must,"
                 " count(*) FILTER (WHERE c.status = 'open' AND c.sla_due_at < %s) AS sla,"
                 " count(*) FILTER (WHERE c.status = 'dismissed' AND c.closed_at >="
                 " date_trunc('week', %s AT TIME ZONE t.timezone) AT TIME ZONE t.timezone) AS dis"
                 " FROM cases c"
                 " JOIN tenants t ON t.id = c.tenant_id WHERE c.owner_user_id = %s",
                 list(policy.MUST_ACT), clock.now(), clock.now(), web.csm)
    overflow = one(web, "SELECT count(*) AS n FROM worklist_items WHERE worklist_id = %s"
                   " AND reason LIKE 'overflow:%%'", wl)["n"]
    assert counts["sla"] >= 1 and overflow >= 1
    assert cells[4:] == [str(counts["must"]), f'<span class="tag gap">{counts["sla"]} late</span>',
                         str(overflow), str(counts["dis"])]
    at_risk = one(web, "SELECT sum(s.expected_arr_at_risk_minor) AS v FROM latest_scores s"
                  " JOIN accounts a ON a.id = s.account_id JOIN users u ON u.id = a.owner_user_id"
                  " WHERE u.manager_id = %s AND u.active AND a.merged_into IS NULL"
                  " AND NOT a.is_test", web.manager)["v"]
    assert f"(p × ARR), this week</dt><dd>{money(at_risk)}</dd>" in page


def test_team_page_lists_risky_accounts_without_a_working_owner(web):
    left = one(web, "INSERT INTO users (tenant_id, email, name, role, manager_id, active)"
               " VALUES (%s, %s, 'Departed Csm', 'csm', %s, false) RETURNING id", HEARTH,
               f"{uuid.uuid4().hex[:8]}@hearthline.io", web.manager)["id"]
    red = web.owner.execute(
        "SELECT a.id, a.name, a.owner_user_id FROM latest_scores s JOIN accounts a"
        " ON a.id = s.account_id WHERE s.tenant_id = %s AND s.band IN ('red', 'unknown')"
        " AND a.owner_user_id <> %s ORDER BY a.name LIMIT 2", (HEARTH, web.csm)).fetchall()
    try:
        web.owner.execute("UPDATE accounts SET owner_user_id = %s WHERE id = %s",
                          (left, red[0]["id"]))
        web.owner.execute("UPDATE accounts SET owner_user_id = NULL WHERE id = %s",
                          (red[1]["id"],))
        manager = client_as(web, web.manager).get("/team").text.split("without a working", 1)[1]
        admin = client_as(web, user_of(web, HEARTH, "admin")).get("/team").text
        admin = admin.split("without a working", 1)[1]
    finally:
        for a in red:
            web.owner.execute("UPDATE accounts SET owner_user_id = %s WHERE id = %s",
                              (a["owner_user_id"], a["id"]))
    names = [a["name"].replace("&", "&amp;") for a in red]
    assert names[0] in manager and names[1] not in manager  # a departed report's account
    assert names[0] in admin and names[1] in admin and "no owner" in admin


def test_ops_page_and_incident_acknowledgement(web):
    incident = one(web, "INSERT INTO incidents (tenant_id, kind, source, opened_at) VALUES"
                   " (%s, 'source_stale', 'web_test', %s) RETURNING id", HEARTH, clock.now())["id"]
    admin = client_as(web, user_of(web, HEARTH, "admin"))
    page = admin.get("/ops").text
    for heading in ("Sources", "Quarantine", "Identity matches by method", "Jobs",
                    "LLM spend today", "Incidents", "Models", "Agent activity"):
        assert heading in page
    links = web.owner.execute("SELECT method, count(*) AS n FROM identity_links"
                              " WHERE tenant_id = %s GROUP BY method", (HEARTH,)).fetchall()
    total = sum(r["n"] for r in links)
    assert total
    for r in links:  # each method's count and its share of all links (design §9 match rates)
        assert (f"<span>{r['method'].replace('_', ' ')}</span> <b class=\"n\">{r['n']}</b>"
                f" ({r['n'] / total:.1%})") in page
    assert f"/admin/incidents/{incident}/ack" in page
    url = f"/admin/incidents/{incident}/ack"
    assert client_as(web, web.manager).post(url).status_code == 404
    assert admin.post(url).status_code == 200
    row = one(web, "SELECT acknowledged_by FROM incidents WHERE id = %s", incident)
    assert row["acknowledged_by"] == user_of(web, HEARTH, "admin")
    assert admin.post(url).status_code == 409
    audit = one(web, "SELECT actor FROM audit_log WHERE entity_type = 'incidents'"
                " AND entity_id = %s AND action = 'update'", str(incident))
    assert audit["actor"] == f"user:{user_of(web, HEARTH, 'admin')}"


def test_promote_shows_band_changes_first_then_promotes(web):
    same, flipped = candidate(web), candidate(web, coef_scale=-1.0)
    admin_id = user_of(web, HEARTH, "admin")
    admin = client_as(web, admin_id)
    page = admin.get("/ops").text

    def changes(model_id):
        form = page.split(f"/admin/models/{model_id}/promote", 1)[1]
        return int(form.split("Would change band for <b>", 1)[1].split("<", 1)[0])
    assert changes(same) == 0  # the active model's own artifact changes nothing
    assert changes(flipped) > 0
    old = one(web, "SELECT id FROM models WHERE tenant_id = %s AND status = 'active'", HEARTH)
    assert admin.post(f"/admin/models/{flipped}/promote").status_code == 200
    assert one(web, "SELECT status FROM models WHERE id = %s", old["id"])["status"] == "retired"
    new = one(web, "SELECT status, promoted_by FROM models WHERE id = %s", flipped)
    assert (new["status"], new["promoted_by"]) == ("active", f"user:{admin_id}")
    assert admin.post(f"/admin/models/{flipped}/promote").status_code == 404  # not a candidate
    # put the original model back for the tests that follow
    web.owner.execute("UPDATE models SET status = 'retired' WHERE id = %s", (flipped,))
    web.owner.execute("UPDATE models SET status = 'active' WHERE id = %s", (old["id"],))


def test_model_card_and_dictionary_render_markdown_with_html_escaped(web, monkeypatch):
    hearth = client_as(web, web.csm)
    assert "<h1>Model card: hearthline</h1>" in hearth.get("/model").text
    assert "<h1>Model card: tallyworks</h1>" in client_as(web, user_of(web, TALLY, "csm")).get(
        "/model").text
    kite = client_as(web, user_of(web, KITE, "csm")).get("/model").text
    assert "No model: rules only" in kite and "integration_broken" in kite
    page = hearth.get("/dictionary").text
    assert "<table>" in page and "usage_drop" in page
    monkeypatch.setattr(registry, "render_dictionary_md", lambda: "# D\n\n<script>x()</script>")
    page = hearth.get("/dictionary").text
    assert "&lt;script&gt;x()&lt;/script&gt;" in page and "<script>" not in page


def test_team_and_ops_render_empty_states(web):
    admin = client_as(web, user_of(web, KITE, "admin"))  # kitewire: never swept, no model
    team = admin.get("/team")
    assert team.status_code == 200 and "No scores yet" in team.text
    ops = admin.get("/ops")
    assert ops.status_code == 200 and "No models: this tenant is ranked by rules only" in ops.text
    boss = one(web, "INSERT INTO users (tenant_id, email, name, role) VALUES (%s, %s,"
               " 'Lonely Manager', 'manager') RETURNING id", HEARTH,
               f"{uuid.uuid4().hex[:8]}@hearthline.io")["id"]
    assert "No CSMs on this team" in client_as(web, boss).get("/team").text
