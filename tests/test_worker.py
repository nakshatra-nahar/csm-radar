"""The unattended worker (design §8): schedules, event rules, the daily sweep, the Monday
freeze, SLA escalation, retries and source freshness.

Each test gets a database of its own: a tick claims every queued job and delivers the feed."""

import json
import threading
import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from psycopg.rows import dict_row

from radar import clock, policy
from radar.agent import handlers, jobs, worker
from radar.bootstrap import replay_feed, seed_tenants_and_users
from radar.db import Database, owner_connect
from radar.tenants import tenant_id
from tests.conftest import WORKER_PASSWORD, _role_dsn, fresh_database

T0 = datetime(2026, 10, 24, 12, tzinfo=UTC)  # a Saturday, 13:00 in London
MONDAY = datetime(2026, 10, 26, 6, tzinfo=UTC)  # 06:00 in London, the day after BST ends
TALLY = tenant_id("tallyworks")


@pytest.fixture
def empty(monkeypatch, small_world_dir):
    """A migrated database with no tenants: SimpleNamespace(dsn, db, owner)."""
    monkeypatch.setenv("RADAR_DATA_DIR", str(small_world_dir))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    clock.freeze(T0)
    with fresh_database() as dsn:
        db = Database(_role_dsn(dsn, "radar_worker", WORKER_PASSWORD))
        try:
            with owner_connect(dsn, autocommit=True, row_factory=dict_row) as owner:
                yield SimpleNamespace(dsn=dsn, db=db, owner=owner)
        finally:
            db.close()
            clock.unfreeze()


@pytest.fixture
def world(empty, small_world_dir):
    """Tallyworks (no model: rules-only) seeded and its feed replayed up to T0."""
    seed_tenants_and_users(empty.dsn, empty.db, ["tallyworks"])
    replay_feed(empty.db, TALLY, small_world_dir, T0)
    return empty


def drain(db, at: datetime) -> dict:
    clock.freeze(at)
    return worker.run_for(db, "w1", budget_s=120)


def bootstrap_sweep(db, at: datetime) -> None:
    """What bootstrap does at the end: the first actionable sweep opens the backlog."""
    with db.system_tx() as conn:
        local_date = at.astimezone(ZoneInfo("Europe/London")).date().isoformat()
        jobs.once_per_period(conn, TALLY, "daily_sweep", local_date)
        jobs.enqueue(conn, "daily_sweep", TALLY, {"bootstrap": True}, lane=1, run_at=at)


def rows(owner, sql, *args):
    return owner.execute(sql, args).fetchall()


def must_act(owner):
    return rows(owner, "SELECT * FROM cases WHERE tenant_id = %s AND kind = ANY(%s)",
                TALLY, policy.MUST_ACT)


@pytest.mark.parametrize("bootstrap", [True, False])
def test_bootstrap_sweep_bypasses_breaker_once(world, bootstrap):
    backlog = 12  # must-act facts true at T0; above the breaker's floor of 10
    if bootstrap:
        bootstrap_sweep(world.db, T0)
    out = drain(world.db, T0)
    assert out["jobs"] > 0 and out["ticks"] >= 2  # run_for stops on the first idle tick
    cases = must_act(world.owner)
    audit = rows(world.owner, "SELECT after FROM audit_log WHERE tenant_id = %s"
                 " AND action = 'bootstrap_backlog'", TALLY)
    incidents = rows(world.owner, "SELECT * FROM incidents WHERE kind = 'circuit_breaker'")
    status = sorted(c["status"] for c in cases)
    if bootstrap:
        assert status == ["open"] * backlog and incidents == []
        assert [a["after"]["count"] for a in audit] == [backlog]
    else:
        assert status == ["held"] * 2 + ["open"] * 10 and len(incidents) == 1
        assert audit == []


def third_failure(data_dir):
    """(deliver_at, customer) of the first payment failure after T0 on its third attempt."""
    for line in (data_dir / "feed" / "tallyworks" / "stripe.jsonl").open():
        rec = json.loads(line)
        obj = rec["body"]["data"]["object"]
        if (rec["deliver_at"] > T0.strftime("%Y-%m-%dT%H:%M:%SZ")
                and rec["body"]["type"] == "invoice.payment_failed"
                and obj["attempt_count"] >= 3):
            return datetime.fromisoformat(rec["deliver_at"]), obj["customer"]


def test_feed_tick_delivers_and_triggers_rule_case_and_brief(world, small_world_dir):
    bootstrap_sweep(world.db, T0)
    drain(world.db, T0)
    delivered_at, customer = third_failure(small_world_dir)
    assert delivered_at.date() == T0.date()  # same local day: no second sweep runs
    drain(world.db, delivered_at + timedelta(minutes=1))
    case = world.owner.execute(
        "SELECT c.* FROM cases c JOIN events e ON e.tenant_id = c.tenant_id"
        " AND e.id = ANY(c.evidence_event_ids) WHERE c.tenant_id = %s"
        " AND c.kind = 'dunning_exhausted' AND e.account_ref = %s",
        (TALLY, f"stripe:{customer}")).fetchone()
    assert case["status"] == "open"  # the bootstrap backlog does not trip the breaker
    assert case["opened_by"].startswith("job:")  # the event path, not a sweep
    assert case["opened_at"] == delivered_at + timedelta(minutes=1)
    job = world.owner.execute("SELECT * FROM jobs WHERE kind = 'evaluate_rules'"
                              " AND dedupe_key = %s", (f"rules:{case['account_id']}",)).fetchone()
    assert job["status"] == "done" and job["lane"] == 0
    brief = world.owner.execute("SELECT * FROM briefs WHERE case_id = %s",
                                (case["id"],)).fetchone()
    assert (brief["status"], brief["generator"]) == ("ready", "template")


def test_sweep_runs_once_per_period(world):
    for at in (T0, T0 + timedelta(hours=2), T0 + timedelta(hours=11)):  # 23:00 in London
        drain(world.db, at)
    assert len(rows(world.owner, "SELECT * FROM score_runs WHERE trigger = 'sweep'")) == 1
    drain(world.db, T0 + timedelta(days=1))
    assert len(rows(world.owner, "SELECT * FROM score_runs WHERE trigger = 'sweep'")) == 2


def test_concurrent_ticks_do_not_double_open(world):
    """Two ticks at once (a GitHub Actions tick and a Vercel cron tick): event rule jobs and
    the sweep open cases side by side. Each fact opens one case, and the breaker's count
    holds: never more than its threshold (10) open in 24 h."""
    at = T0 + timedelta(hours=1, minutes=10)
    clock.freeze(at)
    start = threading.Barrier(2)

    def run(name):
        start.wait()
        while worker.tick(world.db, name, at):
            pass
    threads = [threading.Thread(target=run, args=(f"w{i}",)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    cases = must_act(world.owner)
    assert len({(c["account_id"], c["kind"]) for c in cases}) == len(cases)
    assert sum(c["status"] == "open" for c in cases) == 10
    assert len(rows(world.owner, "SELECT * FROM score_runs WHERE trigger = 'sweep'")) == 1
    breakers = rows(world.owner, "SELECT * FROM incidents WHERE kind = 'circuit_breaker'")
    assert len(breakers) == 1


def test_case_opening_waits_for_the_tenant_lock(world):
    """While another transaction holds the tenant's lock, a sweep scores but opens nothing;
    it opens its cases once the lock is released."""
    clock.freeze(T0)
    sweep = threading.Thread(target=worker.tick, args=(world.db, "w1", T0))
    with world.db.tenant_tx(TALLY) as conn:
        handlers._lock(conn, TALLY)
        sweep.start()
        deadline = time.monotonic() + 60
        while not rows(world.owner, "SELECT 1 FROM pg_locks WHERE locktype = 'advisory'"
                       " AND NOT granted"):
            assert time.monotonic() < deadline and sweep.is_alive()
            time.sleep(0.05)
        assert must_act(world.owner) == []
    sweep.join()
    assert len(must_act(world.owner)) == 12


def sweep_due(empty, at: datetime, slug: str) -> bool:
    """Whether the scheduler records the tenant's daily sweep at `at`."""
    def recorded():
        return rows(empty.owner, "SELECT s.period_key FROM schedule_runs s JOIN tenants t"
                    " ON t.id = s.tenant_id WHERE s.kind = 'daily_sweep' AND t.slug = %s", slug)
    before = len(recorded())
    worker.schedule(empty.db, at)
    return len(recorded()) > before


def test_schedules_respect_tenant_timezone_and_dst(empty):
    seed_tenants_and_users(empty.dsn, empty.db, ["tallyworks", "kitewire"])

    def utc(*args):
        return datetime(2026, *args, tzinfo=UTC)
    # London: BST (UTC+1) until 2026-10-25 01:00 UTC, then GMT
    assert not sweep_due(empty, utc(10, 24, 4, 59), "tallyworks")
    assert sweep_due(empty, utc(10, 24, 5), "tallyworks")
    assert not sweep_due(empty, utc(10, 24, 6), "tallyworks")  # done today
    assert not sweep_due(empty, utc(10, 25, 5, 30), "tallyworks")
    assert sweep_due(empty, utc(10, 25, 6), "tallyworks")
    # Los Angeles: PDT (UTC-7) until 2026-11-01 09:00 UTC, then PST (UTC-8)
    assert not sweep_due(empty, utc(10, 31, 12, 59), "kitewire")
    assert sweep_due(empty, utc(10, 31, 13), "kitewire")
    assert not sweep_due(empty, utc(11, 1, 13, 30), "kitewire")
    assert sweep_due(empty, utc(11, 1, 14), "kitewire")
    periods = rows(empty.owner, "SELECT kind, count(*) AS n FROM schedule_runs"
                   " WHERE kind <> 'daily_sweep' GROUP BY kind ORDER BY kind")
    assert [(p["kind"], p["n"]) for p in periods] == [("feed_tick", 18), ("sla_check", 18)]
    queued = rows(empty.owner, "SELECT kind, tenant_id, lane FROM jobs ORDER BY kind, lane")
    # a periodic job still queued absorbs the next period's (the worker is not running here)
    assert [(q["kind"], q["lane"]) for q in queued] == [
        ("daily_sweep", 1), ("daily_sweep", 1), ("feed_tick", 0), ("feed_tick", 0),
        ("sla_check", 0), ("sla_check", 0)]
    assert {q["tenant_id"] for q in queued} == {TALLY, tenant_id("kitewire")}


def test_monday_sweep_freezes_worklists_and_digests(world):
    bootstrap_sweep(world.db, T0)
    drain(world.db, T0)
    overdue = must_act(world.owner)[0]
    world.owner.execute("UPDATE cases SET sla_due_at = %s WHERE tenant_id = %s AND id = %s",
                        (MONDAY - timedelta(hours=1), TALLY, overdue["id"]))
    drain(world.db, MONDAY)  # sla_check (lane 0) escalates before the sweep (lane 1) runs
    drain(world.db, MONDAY + timedelta(hours=1))  # nothing is frozen or sent twice
    week = MONDAY.date()
    csms = rows(world.owner, "SELECT id, manager_id FROM users WHERE tenant_id = %s"
                " AND role = 'csm' AND active", TALLY)
    managers = rows(world.owner, "SELECT id FROM users WHERE tenant_id = %s"
                    " AND role = 'manager'", TALLY)
    lists = rows(world.owner, "SELECT user_id FROM worklists WHERE week_start = %s", week)
    assert sorted(w["user_id"] for w in lists) == sorted(c["id"] for c in csms)
    digests = rows(world.owner, "SELECT * FROM outbox WHERE kind <> 'customer_email'")
    by_kind = {k: sorted(d["to_user_id"] for d in digests if d["kind"] == k)
               for k in ("csm_digest", "manager_digest")}
    assert by_kind == {"csm_digest": sorted(c["id"] for c in csms),
                       "manager_digest": sorted(m["id"] for m in managers)}
    assert {d["transport"] for d in digests} == {"dry_run"}
    items = rows(world.owner,
                 "SELECT i.*, a.name, w.user_id FROM worklist_items i"
                 " JOIN worklists w ON w.tenant_id = i.tenant_id AND w.id = i.worklist_id"
                 " JOIN accounts a ON a.tenant_id = i.tenant_id AND a.id = i.account_id"
                 " WHERE w.week_start = %s", week)
    assert {i["kind"] for i in items} >= {"must_act", "model"}
    body = {d["to_user_id"]: d["body"] for d in digests}
    for i in items:  # each frozen item is listed for its CSM and the CSM's manager
        manager = next(c["manager_id"] for c in csms if c["id"] == i["user_id"])
        assert i["name"] in body[i["user_id"]] and i["name"] in body[manager]
    case = world.owner.execute("SELECT c.*, a.name, u.manager_id FROM cases c"
                               " JOIN accounts a ON a.tenant_id = c.tenant_id"
                               " AND a.id = c.account_id JOIN users u"
                               " ON u.tenant_id = c.tenant_id AND u.id = c.owner_user_id"
                               " WHERE c.tenant_id = %s AND c.id = %s",
                               (TALLY, overdue["id"])).fetchone()
    assert case["escalated_at"] == MONDAY
    escalated = body[case["manager_id"]].split("Past due")[1]
    assert case["name"] in escalated
    briefs = {b["case_id"] for b in rows(world.owner, "SELECT case_id FROM briefs")}
    assert {i["case_id"] for i in items if i["kind"] == "model"} <= briefs


def test_sla_escalation_fires_exactly_once(world):
    bootstrap_sweep(world.db, T0)
    drain(world.db, T0)
    case = must_act(world.owner)[0]
    world.owner.execute("UPDATE cases SET sla_due_at = %s WHERE tenant_id = %s AND id = %s",
                        (T0 + timedelta(minutes=30), TALLY, case["id"]))
    for hours in (1, 2, 3):
        drain(world.db, T0 + timedelta(hours=hours))
    row = world.owner.execute("SELECT escalated_at FROM cases WHERE tenant_id = %s"
                              " AND id = %s", (TALLY, case["id"])).fetchone()
    assert row["escalated_at"] == T0 + timedelta(hours=1)
    audit = rows(world.owner, "SELECT at, after FROM audit_log WHERE action = 'sla_escalated'"
                 " AND entity_id = %s", str(case["id"]))
    assert [a["at"] for a in audit] == [T0 + timedelta(hours=1)]


def test_handler_failure_retries_then_dead(empty, monkeypatch):
    def boom(db, job):
        raise RuntimeError("boom")
    monkeypatch.setitem(handlers.HANDLERS, "explode", boom)
    with empty.db.system_tx() as conn:
        job_id = jobs.enqueue(conn, "explode", None, {}, run_at=T0)
    seen = []
    for n in range(5):  # backoff is capped at 1 h, so each hour the retry is due
        clock.freeze(T0 + n * timedelta(hours=1, minutes=1))
        worker.tick(empty.db, "w1", clock.now())
        seen.append(empty.owner.execute("SELECT status, attempts, last_error FROM jobs"
                                        " WHERE id = %s", (job_id,)).fetchone())
    assert [s["status"] for s in seen] == ["queued"] * 4 + ["dead"]
    assert seen[-1]["attempts"] == 5 and "boom" in seen[-1]["last_error"]


def test_reaper_fails_score_runs_stuck_past_30_minutes(world):
    def run(started):
        return world.owner.execute(
            "INSERT INTO score_runs (tenant_id, trigger, actionable, as_of, config_version,"
            " started_at) VALUES (%s, 'manual', false, %s, 1, %s) RETURNING id",
            (TALLY, started, started)).fetchone()["id"]
    stuck, recent = run(T0 - timedelta(minutes=31)), run(T0 - timedelta(minutes=29))
    worker.reap(world.db, T0)
    status = {r["id"]: (r["status"], r["finished_at"]) for r in rows(
        world.owner, "SELECT id, status, finished_at FROM score_runs")}
    assert status == {stuck: ("failed", T0), recent: ("running", None)}


def test_stale_source_opens_one_incident(world):
    """Every source fresh but Zendesk, which stops delivering: one source_stale incident,
    however many sweeps see it, with the data My Week's banner shows; it closes itself once
    Zendesk catches up."""
    fresh = {"stripe": T0, "gmail": T0, "salesforce": T0 - timedelta(hours=25),
             "product": datetime(2026, 10, 3, 9, tzinfo=UTC), "zendesk": T0}
    world.owner.execute("UPDATE source_status SET last_deliver_at = (%s::jsonb ->> source)"
                        "::timestamptz WHERE tenant_id = %s",
                        (json.dumps({k: v.isoformat() for k, v in fresh.items()}), TALLY))
    stop = T0 - timedelta(hours=1, minutes=1)
    world.owner.execute("UPDATE source_status SET last_deliver_at = %s WHERE tenant_id = %s"
                        " AND source = 'zendesk'", (stop, TALLY))

    def check(at):
        with world.db.tenant_tx(TALLY) as conn:
            handlers.check_freshness(conn, at)
        return rows(world.owner, "SELECT * FROM incidents WHERE kind = 'source_stale'")
    for at in (T0, T0 + timedelta(minutes=1)):  # two sweeps see it
        incidents = check(at)
    assert [(i["source"], i["opened_at"], i["closed_at"]) for i in incidents] == [
        ("zendesk", T0, None)]
    assert incidents[0]["details"] == {"last_available_at": stop.isoformat(),
                                       "expected_by": (stop + timedelta(hours=1)).isoformat()}
    world.owner.execute("UPDATE source_status SET last_deliver_at = %s WHERE tenant_id = %s"
                        " AND source = 'zendesk'", (T0 + timedelta(minutes=2), TALLY))
    assert [i["closed_at"] for i in check(T0 + timedelta(minutes=2))] == [
        T0 + timedelta(minutes=2)]


@pytest.mark.parametrize(("last", "now", "stale"), [
    ("2026-09-30", "2026-10-05T23:59", False),  # October's batch is due by the 6th
    ("2026-09-30", "2026-10-06T00:00", True),
    ("2026-10-01", "2026-10-06T00:00", False),
    ("2026-08-30", "2026-10-05T00:00", True),  # September's batch never came
])
def test_product_is_stale_after_the_monthly_batch_grace(last, now, stale):
    def at(s):
        return datetime.fromisoformat(s).replace(tzinfo=UTC)
    assert handlers.is_stale("product", at(last), at(now)) is stale


def test_feed_triggers_commit_with_their_batch(world, small_world_dir, monkeypatch):
    """Design §8: a batch's rule jobs are enqueued in the transaction that delivers it, so a
    later batch failing (its offsets already advanced) loses none of them."""
    from functools import partial

    from radar.ingest import feed
    delivered_at, _ = third_failure(small_world_dir)
    monkeypatch.setattr(feed, "deliver_due", partial(feed.deliver_due, batch=1))
    real, seen = feed.pipeline.ingest, []

    def ingest_then_fail(conn, tid, records):
        if seen:  # the batch after the first one with a trigger
            raise RuntimeError("poison record")
        stats = real(conn, tid, records)
        seen.extend(stats.triggers)
        return stats
    monkeypatch.setattr(feed.pipeline, "ingest", ingest_then_fail)
    clock.freeze(delivered_at + timedelta(days=1))  # records due after the trigger too
    job = jobs.Job(id=0, tenant_id=TALLY, kind="feed_tick", payload={}, attempts=1,
                   lease_expires_at=T0)
    with pytest.raises(RuntimeError, match="poison"):
        handlers.feed_tick(world.db, job)
    queued = rows(world.owner, "SELECT payload FROM jobs WHERE kind = 'evaluate_rules'")
    assert seen and [q["payload"]["account_id"] for q in queued] == [str(a) for a in seen]


def test_one_tenant_failing_does_not_stop_the_others(world, small_world_dir, monkeypatch):
    """Kitewire's feed delivery and agent transactions fail every time; Tallyworks still
    gets its feed and its SLA escalation, every period."""
    from radar.ingest import feed
    kite = tenant_id("kitewire")
    seed_tenants_and_users(world.dsn, world.db, ["kitewire"])
    bootstrap_sweep(world.db, T0)
    drain(world.db, T0)
    case = must_act(world.owner)[0]
    world.owner.execute("UPDATE cases SET sla_due_at = %s WHERE tenant_id = %s AND id = %s",
                        (T0 + timedelta(minutes=30), TALLY, case["id"]))
    real_deliver, real_tx = feed.deliver_due, world.db.tenant_tx

    def deliver(db, tid, *args, **kwargs):
        if tid == kite:
            raise RuntimeError("poison record")
        return real_deliver(db, tid, *args, **kwargs)

    def tenant_tx(tid, actor="system"):
        if tid == kite and actor == "agent":
            raise RuntimeError("poison tenant")
        return real_tx(tid, actor)
    monkeypatch.setattr(feed, "deliver_due", deliver)
    monkeypatch.setattr(world.db, "tenant_tx", tenant_tx)

    def offset():
        return world.owner.execute("SELECT sum(feed_offset) AS n FROM source_status"
                                   " WHERE tenant_id = %s", (TALLY,)).fetchone()["n"]
    seen = [offset()]
    for hours in (1, 2):
        drain(world.db, T0 + timedelta(hours=hours))
        seen.append(offset())
    assert seen[0] < seen[-1]  # its feed moved on
    ticks = rows(world.owner, "SELECT status FROM jobs WHERE kind IN ('feed_tick', 'sla_check')"
                 " AND tenant_id = %s", TALLY)
    assert len(ticks) == 6 and {t["status"] for t in ticks} == {"done"}  # every period ran
    escalated = world.owner.execute("SELECT escalated_at FROM cases WHERE tenant_id = %s"
                                    " AND id = %s", (TALLY, case["id"])).fetchone()
    assert escalated["escalated_at"] == T0 + timedelta(hours=1)


def test_stale_model_still_runs_the_rules(world, monkeypatch):
    """A stale model fails scoring closed (design §5); the sweep still rechecks every rule and
    opens cases, and skips the Monday freeze, which needs a score run."""
    from radar.scoring import ManifestMismatch

    def stale(*args, **kwargs):
        raise ManifestMismatch("model is stale")
    monkeypatch.setattr(handlers, "score_tenant", stale)
    bootstrap_sweep(world.db, MONDAY)
    drain(world.db, MONDAY)
    sweeps = rows(world.owner, "SELECT status FROM jobs WHERE kind = 'daily_sweep'")
    assert [s["status"] for s in sweeps] == ["done"]
    cases = must_act(world.owner)
    by_sweep = [c for c in cases if c["opened_by"] == f"bootstrap:sweep:{MONDAY.date()}"]
    assert len(by_sweep) == 12
    assert rows(world.owner, "SELECT * FROM worklists") == []
