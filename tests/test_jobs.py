import threading
import uuid
from datetime import UTC, datetime, timedelta

import psycopg
import pytest

from radar.agent import jobs
from radar.db import Database
from tests.conftest import WORKER_PASSWORD, _role_dsn, fresh_database

T0 = datetime(2026, 3, 2, 9, 0, tzinfo=UTC)


@pytest.fixture(scope="module")
def queue_dsn():
    """A database of its own: claim() takes any queued job, so other modules' jobs would leak in."""
    with fresh_database() as dsn:
        yield dsn


@pytest.fixture(scope="module")
def db(queue_dsn):
    db = Database(_role_dsn(queue_dsn, "radar_worker", WORKER_PASSWORD))
    yield db
    db.close()


@pytest.fixture(scope="module")
def a_tenant(queue_dsn):
    tid = uuid.uuid4()
    with psycopg.connect(queue_dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO tenants (id, slug, name, timezone, currency, internal_domain, config)"
            " VALUES (%s, 'queue', 'Queue', 'UTC', 'USD', 'queue.test', '{}')",
            (tid,),
        )
    return tid


@pytest.fixture(autouse=True)
def empty_queue(queue_dsn):
    with psycopg.connect(queue_dsn, autocommit=True) as conn:
        conn.execute("TRUNCATE jobs, schedule_runs")


def status(db, job_id):
    with db.system_tx() as conn:
        return conn.execute("SELECT * FROM jobs WHERE id = %s", (job_id,)).fetchone()


def test_enqueue_coalesces_while_queued(db):
    with db.system_tx() as conn:
        first = jobs.enqueue(conn, "feed_tick", None, {}, dedupe_key="tick", run_at=T0)
        second = jobs.enqueue(conn, "feed_tick", None, {}, dedupe_key="tick", run_at=T0)
        unkeyed = [jobs.enqueue(conn, "feed_tick", None, {}, run_at=T0) for _ in range(2)]
    assert first is not None and second is None
    assert None not in unkeyed


def test_requeue_after_running_allowed(db):
    with db.system_tx() as conn:
        first = jobs.enqueue(conn, "evaluate_rules", None, {"account_id": "a1"},
                             dedupe_key="a1", run_at=T0)
        [claimed] = jobs.claim(conn, "w1", now=T0)
        again = jobs.enqueue(conn, "evaluate_rules", None, {"account_id": "a1"},
                             dedupe_key="a1", run_at=T0)
    assert claimed.id == first
    assert claimed.payload == {"account_id": "a1"}
    assert again is not None and again != first


def test_claim_skips_jobs_not_yet_due(db):
    with db.system_tx() as conn:
        jobs.enqueue(conn, "later", None, {}, run_at=T0 + timedelta(minutes=1))
        assert jobs.claim(conn, "w1", now=T0) == []
        assert len(jobs.claim(conn, "w1", now=T0 + timedelta(minutes=1))) == 1


def test_two_workers_never_claim_same_job(db):
    with db.system_tx() as conn:
        ids = {jobs.enqueue(conn, "bulk", None, {"i": i}, run_at=T0) for i in range(200)}
    start = threading.Barrier(2)
    claimed = {"w1": [], "w2": []}

    def work(worker_id):
        start.wait()
        while True:
            with db.system_tx() as conn:
                batch = jobs.claim(conn, worker_id, n=7, now=T0)
            if not batch:
                return
            claimed[worker_id] += [job.id for job in batch]

    threads = [threading.Thread(target=work, args=(w,)) for w in claimed]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    w1, w2 = claimed["w1"], claimed["w2"]
    assert len(w1) + len(w2) == 200
    assert set(w1).isdisjoint(w2)
    assert set(w1) | set(w2) == ids


def test_lane_priority(db):
    with db.system_tx() as conn:
        bulk = jobs.enqueue(conn, "bulk", None, {}, lane=2, run_at=T0 - timedelta(hours=1))
        sweep = jobs.enqueue(conn, "sweep", None, {}, lane=1, run_at=T0 - timedelta(hours=1))
        live = jobs.enqueue(conn, "live", None, {}, lane=0, run_at=T0)
        assert [j.id for j in jobs.claim(conn, "w1", n=1, now=T0)] == [live]
        assert [j.id for j in jobs.claim(conn, "w1", n=5, now=T0)] == [sweep, bulk]


def test_fenced_completion_after_lease_expiry(db):
    with db.system_tx() as conn:
        job_id = jobs.enqueue(conn, "brief", None, {}, run_at=T0)
        [stale] = jobs.claim(conn, "w1", lease_s=300, now=T0)
    assert stale.attempts == 1 and stale.lease_expires_at == T0 + timedelta(seconds=300)
    with db.system_tx() as conn:
        assert jobs.reap(conn, now=T0 + timedelta(seconds=299)) == 0
        assert jobs.reap(conn, now=T0 + timedelta(seconds=301)) == 1
        [fresh] = jobs.claim(conn, "w2", now=T0 + timedelta(seconds=302))
    assert fresh.id == job_id and fresh.attempts == 2
    with db.system_tx() as conn:
        jobs.fail(conn, stale, "late failure", now=T0 + timedelta(seconds=303))
        assert jobs.complete(conn, stale, now=T0 + timedelta(seconds=303)) is False
        assert jobs.complete(conn, fresh, now=T0 + timedelta(seconds=304)) is True
    row = status(db, job_id)
    assert row["status"] == "done" and row["finished_at"] == T0 + timedelta(seconds=304)


def test_backoff_and_dead(db, monkeypatch):
    monkeypatch.setattr(jobs.random, "uniform", lambda low, high: high)  # the longest wait
    with db.system_tx() as conn:
        job_id = jobs.enqueue(conn, "brief", None, {}, run_at=T0)
    now, waits = T0, []
    for _ in range(9):
        with db.system_tx() as conn:
            [job] = jobs.claim(conn, "w1", now=now)
            jobs.fail(conn, job, f"boom {job.attempts}", max_attempts=9, now=now)
        row = status(db, job_id)
        if row["status"] == "queued":
            assert row["lease_expires_at"] is None
            waits.append((row["run_at"] - now).total_seconds())
            now = row["run_at"]
    assert waits == [10, 20, 40, 80, 160, 320, 640, 1280]  # min(3600, 5 * 2**attempts)
    assert row["status"] == "dead" and row["last_error"] == "boom 9"
    assert row["finished_at"] == now and row["attempts"] == 9


def test_backoff_is_capped_at_an_hour(db, monkeypatch):
    monkeypatch.setattr(jobs.random, "uniform", lambda low, high: high)
    with db.system_tx() as conn:
        job_id = jobs.enqueue(conn, "brief", None, {}, run_at=T0)
        conn.execute("UPDATE jobs SET attempts = 10 WHERE id = %s", (job_id,))
        [job] = jobs.claim(conn, "w1", now=T0)
        jobs.fail(conn, job, "boom", max_attempts=20, now=T0)
    assert status(db, job_id)["run_at"] == T0 + timedelta(seconds=3600)


def test_reap_kills_a_job_that_keeps_losing_its_lease(db):
    """A job that crashes the worker every time must not be retried forever."""
    with db.system_tx() as conn:
        job_id = jobs.enqueue(conn, "poison", None, {}, run_at=T0)
    now = T0
    for _ in range(5):  # max_attempts column default
        with db.system_tx() as conn:
            [job] = jobs.claim(conn, "w1", lease_s=60, now=now)
            now += timedelta(seconds=61)
            assert jobs.reap(conn, now=now) == 1
    row = status(db, job_id)
    assert row["status"] == "dead" and row["last_error"] == "lease expired"


def test_once_per_period(db, a_tenant):
    with db.system_tx() as conn:
        assert jobs.once_per_period(conn, None, "daily_sweep", "2026-03-02") is True
        assert jobs.once_per_period(conn, None, "daily_sweep", "2026-03-02") is False
        assert jobs.once_per_period(conn, None, "daily_sweep", "2026-03-03") is True
        assert jobs.once_per_period(conn, a_tenant, "daily_sweep", "2026-03-02") is True
        assert jobs.once_per_period(conn, None, "sla_check", "2026-03-02") is True


LATER = T0 + timedelta(days=30)  # the twin stays queued out of other tests' claims


def _running_with_queued_twin(db, key):
    """(job X running with dedupe key `key`, id of its queued twin Y)."""
    with db.system_tx() as conn:
        jobs.enqueue(conn, "twin", None, {}, dedupe_key=key, run_at=T0)
        [x] = jobs.claim(conn, "w1", n=1, lease_s=60, now=T0)
        y = jobs.enqueue(conn, "twin", None, {}, dedupe_key=key, run_at=LATER)
    return x, y


def test_fail_with_a_queued_twin_is_superseded(db):
    """Requeueing X would collide with Y on jobs_dedupe_idx: Y does the work, X dies."""
    x, y = _running_with_queued_twin(db, "twin:fail")
    with db.system_tx() as conn:
        jobs.fail(conn, x, "boom", now=T0)
    row = status(db, x.id)
    assert (row["status"], row["last_error"]) == ("dead", "superseded: boom")
    assert status(db, y)["status"] == "queued"


def test_reap_with_a_queued_twin_does_not_wedge(db):
    x, y = _running_with_queued_twin(db, "twin:reap")
    with db.system_tx() as conn:
        assert jobs.reap(conn, now=T0 + timedelta(seconds=61)) == 1
    row = status(db, x.id)
    assert (row["status"], row["last_error"]) == ("dead", "superseded")
    assert status(db, y)["status"] == "queued"


def test_reap_two_expired_twins_requeues_the_newer(db):
    """Both twins were claimed and both workers died: one statement must not requeue both."""
    soon = T0 + timedelta(days=10)  # before LATER: the other twins are not claimable
    with db.system_tx() as conn:
        for _ in range(2):
            jobs.enqueue(conn, "twin", None, {}, dedupe_key="twin:both", run_at=soon)
            [_job] = jobs.claim(conn, "w1", n=1, lease_s=60, now=soon)
        older, newer = sorted(r["id"] for r in conn.execute(
            "SELECT id FROM jobs WHERE dedupe_key = 'twin:both'"))
        assert jobs.reap(conn, now=soon + timedelta(seconds=61)) == 2
    assert status(db, older)["status"] == "dead" and status(db, newer)["status"] == "queued"


def test_pool_replaces_a_connection_the_server_closed(queue_dsn):
    """Neon drops idle connections; a warm Vercel instance's pool must hand out a live one
    instead of failing the next tick's first transaction."""
    pool = Database(_role_dsn(queue_dsn, "radar_worker", WORKER_PASSWORD), min_size=1,
                    max_size=1)
    try:
        with pool.system_tx() as conn:
            pid = conn.execute("SELECT pg_backend_pid() AS pid").fetchone()["pid"]
        with psycopg.connect(queue_dsn, autocommit=True) as owner:
            owner.execute("SELECT pg_terminate_backend(%s)", (pid,))
        with pool.system_tx() as conn:
            assert conn.execute("SELECT 1 AS one").fetchone()["one"] == 1
    finally:
        pool.close()
