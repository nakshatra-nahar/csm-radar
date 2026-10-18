"""Bootstrap (design §10), the feed buffer (Ruling Y), prune (Ruling AM) and push (Ruling AT)."""

import dataclasses
import os
import uuid
from datetime import UTC, datetime, timedelta
from functools import partial
from types import SimpleNamespace

import psycopg
import pytest
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from radar import clock, model
from radar.agent import handlers
from radar.agent.jobs import Job
from radar.bootstrap import HISTORY_END, bootstrap, replay_feed, seed_tenants_and_users
from radar.cli import main
from radar.config import load_settings
from radar.db import Database, owner_connect
from radar.ingest import feed
from radar.ops import PushError, _tool, prune, push
from radar.tenants import TENANTS, runtime_config, tenant_id
from tests.conftest import ADMIN_DSN, WEB_PASSWORD, WORKER_PASSWORD, _role_dsn, fresh_database

KITE = tenant_id("kitewire")
# pg_dump/pg_restore must match the 16 test server; Homebrew puts 17 first on PATH
PG_BIN = os.environ.get("RADAR_TEST_PG_BIN") or next(
    (d for d in ["/opt/homebrew/opt/postgresql@16/bin"] if os.path.isdir(d)), None)


@pytest.fixture
def blank():
    """A migrated database: (owner dsn, radar_worker Database)."""
    with fresh_database() as dsn:
        db = Database(_role_dsn(dsn, "radar_worker", WORKER_PASSWORD))
        try:
            yield dsn, db
        finally:
            db.close()


def one(dsn, sql, *args):
    with owner_connect(dsn, row_factory=dict_row) as conn:
        return conn.execute(sql, args).fetchone()


def test_seeding_merges_stored_config_with_new_defaults(blank):
    dsn, db = blank
    seed_tenants_and_users(dsn, db, ["kitewire"])
    stored = runtime_config("kitewire")
    del stored["freshness_hours"]  # a key added after this database was seeded
    stored["capacity_per_csm"] = 7  # an edit made in the app
    with owner_connect(dsn, autocommit=True) as conn:
        conn.execute("UPDATE tenants SET config = %s WHERE id = %s", (Jsonb(stored), KITE))
    for _ in range(2):  # the second seed finds nothing new: no second bump
        seed_tenants_and_users(dsn, db, ["kitewire"])
        t = one(dsn, "SELECT config, config_version FROM tenants WHERE id = %s", KITE)
        assert t["config"]["capacity_per_csm"] == 7
        assert t["config"]["freshness_hours"] == runtime_config("kitewire")["freshness_hours"]
        assert t["config"]["config_version"] == t["config_version"] == 2


def comparable(stats, dsn):
    """IngestStats with trigger account ids as Salesforce ids (uuids differ per database)."""
    with owner_connect(dsn) as conn:
        sf = sorted(r[0] for r in conn.execute(
            "SELECT sf_account_id FROM accounts WHERE id = ANY(%s)", (stats.triggers,)))
    return dataclasses.replace(stats, triggers=sf)


def test_feed_buffer_delivers_what_the_files_deliver(small_world_dir):
    """Two databases replayed to the same point; one delivers the next window from the files,
    the other from feed_buffer. Delivered buffer rows are deleted."""
    t1 = HISTORY_END + timedelta(days=10)
    out = {}
    for mode in ("files", "db"):
        with fresh_database() as dsn:
            db = Database(_role_dsn(dsn, "radar_worker", WORKER_PASSWORD))
            try:
                seed_tenants_and_users(dsn, db, ["kitewire"])
                replay_feed(db, KITE, small_world_dir, HISTORY_END)
                if mode == "db":
                    loaded = feed.load_buffer(db, KITE, small_world_dir)
                    assert loaded > 0
                    assert feed.load_buffer(db, KITE, small_world_dir) == 0  # idempotent
                stats = feed.deliver_due(db, KITE, t1, small_world_dir, from_db=mode == "db")
                out[mode] = comparable(stats, dsn)
                if mode == "db":
                    left = one(dsn, "SELECT count(*) AS n, min(deliver_at) AS first"
                               " FROM feed_buffer")
                    assert left["n"] == loaded - stats.received and left["first"] > t1
            finally:
                db.close()
    assert out["files"].received > 0 and out["files"] == out["db"]


def test_feed_tick_reads_the_buffer_when_feed_source_is_db(blank, small_world_dir,
                                                          monkeypatch):
    dsn, db = blank
    seed_tenants_and_users(dsn, db, ["kitewire"])
    replay_feed(db, KITE, small_world_dir, HISTORY_END)
    monkeypatch.setenv("RADAR_DATA_DIR", str(small_world_dir))  # the files are there
    monkeypatch.setenv("FEED_SOURCE", "db")
    clock.freeze(HISTORY_END + timedelta(days=10))
    job = Job(id=0, tenant_id=KITE, kind="feed_tick", payload={}, attempts=1,
              lease_expires_at=clock.now())

    def raw():
        return one(dsn, "SELECT count(*) AS n FROM raw_records")["n"]
    try:
        before = raw()
        handlers.feed_tick(db, job)
        assert raw() == before  # empty buffer: nothing, though the files hold due lines
        feed.load_buffer(db, KITE, small_world_dir)
        handlers.feed_tick(db, job)
        assert raw() > before
    finally:
        clock.unfreeze()


def test_prune_keeps_frozen_latest_and_monday_runs_per_tenant(blank):
    dsn, db = blank
    seed_tenants_and_users(dsn, db, ["hearthline", "kitewire"])
    hearth, now = tenant_id("hearthline"), datetime(2026, 10, 17, 18, tzinfo=UTC)  # a Saturday
    with owner_connect(dsn, autocommit=True, row_factory=dict_row) as conn:
        csm = conn.execute("SELECT id FROM users WHERE role = 'csm' AND tenant_id = %s"
                           " LIMIT 1", (KITE,)).fetchone()["id"]
        accounts = {t: conn.execute("INSERT INTO accounts (tenant_id, sf_account_id, name)"
                                    " VALUES (%s, 'A1', 'A') RETURNING id", (t,)
                                    ).fetchone()["id"] for t in (KITE, hearth)}

        def run(tid, at, actionable, trigger="sweep"):
            at = now - timedelta(days=at) if isinstance(at, int) else at
            rid = conn.execute(
                "INSERT INTO score_runs (tenant_id, trigger, actionable, as_of, config_version,"
                " status, finished_at) VALUES (%s, %s, %s, %s, 1, 'done', %s)"
                " RETURNING id", (tid, trigger, actionable, at, at)).fetchone()["id"]
            conn.execute("INSERT INTO account_scores (tenant_id, run_id, account_id, as_of,"
                         " eligible) VALUES (%s, %s, %s, %s, true)",
                         (tid, rid, accounts[tid], at))
            return rid
        # kitewire (Los Angeles): two old weekday runs go; a frozen run, the latest of each
        # kind and a sweep on a local Monday (75 days back) stay
        gone = [run(KITE, 90, False), run(KITE, 70, True)]
        kept = [run(KITE, 80, True), run(KITE, 41, True), run(KITE, 1, False),
                run(KITE, 75, True)]
        conn.execute("INSERT INTO worklists (tenant_id, user_id, week_start, run_id)"
                     " VALUES (%s, %s, '2026-07-20', %s)", (KITE, csm, kept[0]))
        # hearthline (New York): its latest runs are older than the cutoff and stay, as does
        # a backfill run at Monday 00:00 UTC (Sunday evening in New York)
        gone += [run(hearth, 65, False), run(hearth, 66, True)]
        kept += [run(hearth, 60, True), run(hearth, 50, False),
                 run(hearth, datetime(2026, 8, 10, tzinfo=UTC), False, "backfill")]
        clock.freeze(now)
        try:
            counts = prune(dsn, older_than_days=30)
        finally:
            clock.unfreeze()
        two = {"score_runs": 2, "account_scores": 2}
        assert counts == {"hearthline": two, "kitewire": two}
        left = {r["id"] for r in conn.execute("SELECT id FROM score_runs")}
        assert left == set(kept) and not left & set(gone)
        assert conn.execute("SELECT count(*) AS n FROM account_scores").fetchone()["n"] == 7
        audit = conn.execute("SELECT tenant_id, actor, after FROM audit_log"
                             " WHERE action = 'prune' ORDER BY tenant_id").fetchall()
        assert sorted([hearth, KITE]) == [a["tenant_id"] for a in audit]
        assert all(a["actor"] == "system:prune" and a["after"] == two | {"older_than_days": 30}
                   for a in audit)
        for role in ("radar_web", "radar_worker"):  # the app roles still cannot delete scores
            assert not conn.execute("SELECT has_table_privilege(%s, 'account_scores', 'DELETE')"
                                    " AS ok", (role,)).fetchone()["ok"]


@pytest.fixture(scope="module")
def booted(small_world_dir):
    """A scale-0.1 bootstrap at HISTORY_END (a Saturday). Models need the lowered decision
    gate and sign gate of conftest.train_world: the gate is set in the stored config, which
    bootstrap's seeding keeps. SimpleNamespace(dsn, settings, first: bootstrap's result)."""
    with fresh_database() as dsn, pytest.MonkeyPatch.context() as mp:
        for name in ("RADAR_WEB_DSN", "RADAR_WORKER_DSN", "ANTHROPIC_API_KEY", "FEED_SOURCE"):
            mp.delenv(name, raising=False)
        mp.setenv("RADAR_OWNER_DSN", dsn)
        mp.setenv("RADAR_WEB_PASSWORD", WEB_PASSWORD)
        mp.setenv("RADAR_WORKER_PASSWORD", WORKER_PASSWORD)
        mp.setenv("RADAR_DATA_DIR", str(small_world_dir))
        mp.setattr(model, "check_signs", lambda artifact: [])
        settings = load_settings()
        db = Database(settings.worker_dsn)
        seed_tenants_and_users(dsn, db, list(TENANTS))
        db.close()
        with owner_connect(dsn, autocommit=True) as conn:
            conn.execute("UPDATE tenants SET config"
                         " = jsonb_set(config, '{model,min_decisions}', '20')")
        clock.freeze(HISTORY_END)
        try:
            first = bootstrap(settings)
        finally:
            clock.unfreeze()
        yield SimpleNamespace(dsn=dsn, settings=settings, first=first)


def table_counts(dsn) -> dict[str, int]:
    with owner_connect(dsn) as conn:
        tables = [r[0] for r in conn.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY 1")]
        return {t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in tables}


def test_bootstrap_is_idempotent(booted):
    before = table_counts(booted.dsn)
    clock.freeze(HISTORY_END)
    try:
        second = bootstrap(booted.settings)
    finally:
        clock.unfreeze()
    assert table_counts(booted.dsn) == before
    assert set(second["ingested"].values()) == {0}
    # what the first run made
    assert min(booted.first["ingested"].values()) > 0
    assert before["models"] == 2 and before["feed_buffer"] > 0 and before["briefs"] > 0
    q = partial(one, booted.dsn)
    assert q("SELECT count(*) AS n FROM score_runs WHERE trigger = 'backfill'")["n"] == 3 * 12
    assert q("SELECT count(*) AS n FROM audit_log WHERE action = 'bootstrap_backlog'")["n"] == 3
    # Ruling AG: a Saturday bootstrap froze this ISO week's list for every CSM
    assert q("SELECT count(*) AS n FROM users WHERE role = 'csm'")["n"] == q(
        "SELECT count(*) AS n FROM worklists WHERE week_start = '2026-10-12'")["n"] > 0
    assert q("SELECT count(*) AS n FROM jobs WHERE status <> 'done'")["n"] == 0  # drained


@pytest.fixture
def empty_target():
    """A new database with nothing in it, not even the schema: the owner dsn."""
    name = f"radar_push_{uuid.uuid4().hex[:8]}"
    with psycopg.connect(ADMIN_DSN, autocommit=True) as admin:
        admin.execute(f"CREATE DATABASE {name}")
    try:
        yield make_conninfo(ADMIN_DSN, dbname=name)
    finally:
        with psycopg.connect(ADMIN_DSN, autocommit=True) as admin:
            admin.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")


def test_push_copies_every_row_once_and_refuses_a_loaded_target(booted, empty_target):
    def go(force=False):
        return push(empty_target, booted.dsn, WEB_PASSWORD, WORKER_PASSWORD, PG_BIN,
                    force=force)
    source = table_counts(booted.dsn)
    assert go() == source == table_counts(empty_target)  # audit_log too: no trigger re-fired
    assert source["audit_log"] > 0 and source["raw_records"] > 0
    triggers = one(empty_target, "SELECT count(*) AS n, count(*) FILTER (WHERE tgenabled = 'O')"
                   " AS enabled FROM pg_trigger WHERE NOT tgisinternal")
    assert triggers["n"] == triggers["enabled"] == 5
    with pytest.raises(PushError, match="already holds tenant data"):
        go()
    assert go(force=True) == source == table_counts(empty_target)


def test_push_refuses_a_client_tool_of_the_wrong_version():
    with pytest.raises(PushError, match="pass --pg-bin"):
        _tool("pg_dump", PG_BIN, 99, 99)


def test_train_retires_only_its_own_tenants_model(booted):
    """Last in the module: it adds a model to booted."""
    active = ("SELECT tenant_id, version FROM models WHERE status = 'active'"
              " ORDER BY tenant_id")
    with owner_connect(booted.dsn, row_factory=dict_row) as conn:
        before = conn.execute(active).fetchall()
    clock.freeze(HISTORY_END)
    try:
        main(["train", "--tenant", "hearthline"])
    finally:
        clock.unfreeze()
    with owner_connect(booted.dsn, row_factory=dict_row) as conn:
        after = conn.execute(active).fetchall()
    hearth = tenant_id("hearthline")
    assert [r for r in after if r["tenant_id"] != hearth] == [
        r for r in before if r["tenant_id"] != hearth] != []
    assert [r["version"] for r in after if r["tenant_id"] == hearth] == [2]  # per tenant
