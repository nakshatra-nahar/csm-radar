import os
import uuid
from contextlib import contextmanager

import psycopg
import pytest
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from radar.bootstrap import HISTORY_END, replay_feed, seed_tenants_and_users
from radar.db import Database
from radar.migrate import migrate
from radar.sim import generate, staff
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
    with psycopg.connect(owner_dsn, autocommit=True, row_factory=dict_row) as conn:
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


@pytest.fixture(scope="session")
def small_world_db(small_world_dir):
    """A fresh database with the small world seeded and replayed up to history_end.

    Yields {"owner_dsn", "db" (radar_worker Database), "data_dir", "stats" {slug: IngestStats}}.
    """
    with fresh_database() as dsn:
        db = Database(_role_dsn(dsn, "radar_worker", WORKER_PASSWORD))
        try:
            slugs = list(TENANTS)
            seed_tenants_and_users(dsn, db, slugs)
            stats = {s: replay_feed(db, tenant_id(s), small_world_dir, HISTORY_END) for s in slugs}
            yield {"owner_dsn": dsn, "db": db, "data_dir": small_world_dir, "stats": stats}
        finally:
            db.close()
