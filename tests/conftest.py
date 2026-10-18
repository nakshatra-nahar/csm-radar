import os
import uuid
from contextlib import contextmanager
from datetime import timedelta

import psycopg
import pytest
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from radar import clock, model
from radar.bootstrap import replay_feed, seed_tenants_and_users, train_and_promote
from radar.db import Database, owner_connect
from radar.migrate import migrate
from radar.sim import generate, staff
from radar.sim.params import HISTORY_END
from radar.tenants import TENANTS, runtime_config, tenant_id

ADMIN_DSN = os.environ.get(
    "RADAR_TEST_ADMIN_DSN", "postgresql://postgres:postgres@localhost:54329/postgres"
)
WEB_PASSWORD = os.environ.get("RADAR_WEB_PASSWORD", "radar_web_test")
WORKER_PASSWORD = os.environ.get("RADAR_WORKER_PASSWORD", "radar_worker_test")


@contextmanager
def fresh_database():
    """A new, migrated database (owner DSN), dropped afterwards."""
    name = f"radar_test_{uuid.uuid4().hex[:8]}"
    with psycopg.connect(ADMIN_DSN, autocommit=True) as admin:
        admin.execute(f"CREATE DATABASE {name}")
    dsn = make_conninfo(ADMIN_DSN, dbname=name)
    try:
        migrate(dsn, WEB_PASSWORD, WORKER_PASSWORD)
        yield dsn
    finally:
        with psycopg.connect(ADMIN_DSN, autocommit=True) as admin:
            admin.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")


@pytest.fixture(scope="session")
def owner_dsn():
    with fresh_database() as dsn:
        yield dsn


def _role_dsn(owner_dsn: str, role: str, password: str) -> str:
    return make_conninfo(owner_dsn, user=role, password=password)


@pytest.fixture(scope="session")
def owner_conn(owner_dsn):
    with owner_connect(owner_dsn, autocommit=True, row_factory=dict_row) as conn:
        yield conn


@pytest.fixture(scope="session")
def web_db(owner_dsn):
    db = Database(_role_dsn(owner_dsn, "radar_web", WEB_PASSWORD))
    yield db
    db.close()


@pytest.fixture(scope="session")
def worker_db(owner_dsn):
    db = Database(_role_dsn(owner_dsn, "radar_worker", WORKER_PASSWORD))
    yield db
    db.close()


@pytest.fixture
def web_db_size1(owner_dsn):
    db = Database(_role_dsn(owner_dsn, "radar_web", WEB_PASSWORD), min_size=1, max_size=1)
    yield db
    db.close()


@pytest.fixture(scope="session")
def two_tenants_seeded(owner_conn):
    """Tenants hearthline (A) and tallyworks (B), each with one account, raw record and job.

    Seeded as the owner (a superuser in tests), which bypasses RLS.
    Returns {"a": tenant_id, "b": tenant_id, "a_account": uuid, "b_account": uuid}.
    """
    out = {}
    for key, slug in (("a", "hearthline"), ("b", "tallyworks")):
        spec, tid = TENANTS[slug], tenant_id(slug)
        owner_conn.execute(
            "INSERT INTO tenants (id, slug, name, timezone, currency, internal_domain, config)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (tid, slug, spec.name, spec.timezone, spec.currency, spec.internal_domain,
             Jsonb(runtime_config(slug))),
        )
        account = owner_conn.execute(
            "INSERT INTO accounts (tenant_id, sf_account_id, name) VALUES (%s, %s, %s)"
            " RETURNING id",
            (tid, f"001{key.upper()}00000000000AAA", f"{slug} customer"),
        ).fetchone()["id"]
        owner_conn.execute(
            "INSERT INTO raw_records (tenant_id, source, source_record_id, payload, payload_hash,"
            " available_at) VALUES (%s, 'stripe', %s, '{}', 'h', now())",
            (tid, f"evt_{key}"),
        )
        owner_conn.execute(
            "INSERT INTO jobs (tenant_id, kind, payload) VALUES (%s, 'score_account', '{}')",
            (tid,),
        )
        out[key], out[f"{key}_account"] = tid, account
    return out


@pytest.fixture
def tenant(owner_conn):
    """A fresh tenant with hearthline's config (internal domain hearthline.io) and staff.

    Returns (tenant_id, slug). Stripe test records are signed with the slug's demo secret.
    """
    slug, tid = f"t{uuid.uuid4().hex[:10]}", uuid.uuid4()
    spec = TENANTS["hearthline"]
    owner_conn.execute(
        "INSERT INTO tenants (id, slug, name, timezone, currency, internal_domain, config)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s)",
        (tid, slug, slug, spec.timezone, spec.currency, spec.internal_domain,
         Jsonb(runtime_config("hearthline"))),
    )
    for user in staff("hearthline"):
        owner_conn.execute(
            "INSERT INTO users (tenant_id, email, name, role, sf_user_id)"
            " VALUES (%s, %s, %s, %s, %s)",
            (tid, user["email"], user["name"], user["role"], user["sf_user_id"]),
        )
    return tid, slug


@pytest.fixture(scope="session")
def small_world_dir(tmp_path_factory):
    """A scale-0.1 world (all three tenants), generated once per session."""
    out = tmp_path_factory.mktemp("small_world")
    generate(out, scale=0.1)
    return out


@contextmanager
def world_database(data_dir):
    """A fresh database with the world in data_dir seeded and replayed up to history_end,
    dropped afterwards. Yields {"owner_dsn", "db" (radar_worker Database), "data_dir",
    "stats" {slug: IngestStats}}."""
    with fresh_database() as dsn:
        db = Database(_role_dsn(dsn, "radar_worker", WORKER_PASSWORD))
        try:
            slugs = list(TENANTS)
            seed_tenants_and_users(dsn, db, slugs)
            stats = {s: replay_feed(db, tenant_id(s), data_dir, HISTORY_END) for s in slugs}
            yield {"owner_dsn": dsn, "db": db, "data_dir": data_dir, "stats": stats}
        finally:
            db.close()


def train_world(world: dict) -> dict:
    """The world plus a model trained and promoted per tenant as of history_end.

    The small world has ~33 matured decisions per tenant, under the default gate of 100, so
    each tenant's config gate is lowered to 20 first. At that size some behavioural signs flip
    (they hold from scale 0.3, see test_signs_hold_on_small_world), so the sign gate is lifted
    here too. Adds "models" {slug: model id | None}.
    """
    with owner_connect(world["owner_dsn"], autocommit=True) as conn:
        conn.execute("UPDATE tenants SET config = jsonb_set(config, '{model,min_decisions}', '20')")
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(model, "check_signs", lambda artifact: [])
        return world | {"models": {s: train_and_promote(world["db"], tenant_id(s), HISTORY_END)
                                   for s in TENANTS}}


@pytest.fixture(scope="session")
def small_world_db(small_world_dir):
    """A fresh database with the small world seeded and replayed up to history_end."""
    with world_database(small_world_dir) as world:
        yield world


@pytest.fixture(scope="session")
def trained_world_db(small_world_db):
    """small_world_db plus a model trained and promoted per tenant (train_world)."""
    return train_world(small_world_db)


# --- briefs and outbox (Task 10) ------------------------------------------------------------

def add_event(owner_conn, tid, account_id, account_ref, method, type_, attrs, status=None,
              priority=None, amount_minor=None, days_ago=2):
    """An event (with its raw record) attributed to the account through `method`."""
    at = clock.now() - timedelta(days=days_ago)
    raw = owner_conn.execute(
        "INSERT INTO raw_records (tenant_id, source, source_record_id, payload, payload_hash,"
        " occurred_at, available_at) VALUES (%s, %s, %s, '{}', 'h', %s, %s) RETURNING id",
        (tid, type_.split(".")[0], uuid.uuid4().hex, at, at)).fetchone()["id"]
    owner_conn.execute(
        "INSERT INTO identity_links (tenant_id, source, account_ref, account_id, method,"
        " available_at) VALUES (%s, 'test', %s, %s, %s, %s)",
        (tid, account_ref, account_id, method, at - timedelta(days=30)))
    return owner_conn.execute(
        "INSERT INTO events (tenant_id, raw_record_id, source, type, entity_id, account_ref,"
        " occurred_at, available_at, version_key, status, priority, amount_minor, attrs)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'v1', %s, %s, %s, %s) RETURNING id",
        (tid, raw, type_.split(".")[0], type_, uuid.uuid4().hex, account_ref, at, at, status,
         priority, amount_minor, Jsonb(attrs))).fetchone()["id"]


def add_case(owner_conn, tid, account_id, kind, play, event_ids):
    return owner_conn.execute(
        "INSERT INTO cases (tenant_id, account_id, kind, status, play, opened_by, owner_user_id,"
        " evidence_event_ids) SELECT %s, %s, %s, 'open', %s, 'test', owner_user_id, %s"
        " FROM accounts WHERE tenant_id = %s AND id = %s RETURNING id",
        (tid, account_id, kind, play, event_ids, tid, account_id)).fetchone()["id"]


@pytest.fixture
def brief_world(owner_conn, tenant):
    """One account owned by the first CSM with a scored run, a champion and a backup contact,
    a Zendesk ticket (exact key) and a Stripe invoice matched by domain, and an open
    urgent_ticket_stalled case citing both. Returns a dict of ids."""
    tid, _ = tenant
    csm = owner_conn.execute("SELECT id FROM users WHERE tenant_id = %s AND role = 'csm'"
                             " ORDER BY email LIMIT 1", (tid,)).fetchone()["id"]
    account = owner_conn.execute(
        "INSERT INTO accounts (tenant_id, sf_account_id, name, segment, owner_user_id)"
        " VALUES (%s, %s, 'Oak Lane Homes', 'smb', %s) RETURNING id",
        (tid, uuid.uuid4().hex[:18], csm)).fetchone()["id"]
    contacts = [owner_conn.execute(
        "INSERT INTO contacts (tenant_id, id, account_id, sf_contact_id, email, first_name,"
        " is_champion) VALUES (%s, gen_random_uuid(), %s, %s, %s, %s, %s) RETURNING id",
        (tid, account, f"003{name}", f"{name}@oaklane.com", name, champ)).fetchone()["id"]
        for name, champ in (("ana", True), ("ben", False))]
    ticket = add_event(owner_conn, tid, account, "zd_org:1", "zendesk_external_id",
                       "support.ticket", {"ticket_id": 4521, "subject": "Report export fails",
                                          "description_excerpt": "Exports time out since Monday."},
                       status="open", priority="urgent")
    invoice = add_event(owner_conn, tid, account, "stripe:cus_1", "domain", "billing.invoice",
                        {"attempt_count": 2}, status="open", amount_minor=129900)
    run = owner_conn.execute(
        "INSERT INTO score_runs (tenant_id, trigger, actionable, as_of, config_version, status,"
        " started_at, finished_at, manifest) VALUES (%s, 'sweep', true, %s, 1, 'done', %s, %s,"
        " '{}') RETURNING id", (tid, clock.now(), clock.now(), clock.now())).fetchone()["id"]
    owner_conn.execute(
        "INSERT INTO account_scores (tenant_id, run_id, account_id, as_of, eligible, p, lift,"
        " band, rank_in_book, owner_user_id, arr_minor, features, contributions, context,"
        " coverage) VALUES (%s, %s, %s, %s, true, 0.42, 3.1, 'red', 2, %s, 1200000, %s, %s,"
        " %s, '{\"ok\": true}')",
        (tid, run, account, clock.now(), csm,
         Jsonb({"usage_drop": 0.35, "open_urgent_tickets": 0.69}),
         Jsonb({"usage_drop": 0.8, "open_urgent_tickets": 0.5, "log_arr": -0.2}),
         Jsonb({"term_end": "2027-01-31T00:00:00+00:00"})))
    case = add_case(owner_conn, tid, account, "urgent_ticket_stalled", "support_escalation",
                    [ticket, invoice])
    return {"tid": tid, "csm": csm, "account": account, "contacts": contacts, "ticket": ticket,
            "invoice": invoice, "case": case}
