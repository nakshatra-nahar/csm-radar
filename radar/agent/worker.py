"""The unattended worker (design §8): reap, schedule the periodic jobs, run due jobs.

tick() is one pass. run_for() repeats it within a time budget (the Vercel tick endpoint,
Ruling Y); run_forever() is the long-lived worker process.
"""

import logging
import threading
import time
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import psycopg

from radar import clock
from radar.agent import jobs
from radar.agent.handlers import HANDLERS
from radar.db import Database

MAX_JOBS_PER_TICK = 20
SCORE_RUN_TIMEOUT = timedelta(minutes=30)
MAX_BACKOFF_S = 60.0
log = logging.getLogger(__name__)


def reap(db: Database, now: datetime) -> None:
    """Expired job leases back to the queue (or dead), and score runs a crash left
    'running' marked failed."""
    with db.system_tx() as conn:
        jobs.reap(conn, now)
        tenants = [r["id"] for r in conn.execute("SELECT id FROM tenants")]
    for tid in tenants:  # score_runs is under RLS: one tenant at a time
        with db.tenant_tx(tid) as conn:
            conn.execute("UPDATE score_runs SET status = 'failed', finished_at = %s"
                         " WHERE status = 'running' AND started_at < %s",
                         (now, now - SCORE_RUN_TIMEOUT))


def schedule(db: Database, now: datetime) -> None:
    """Enqueue each tenant's periodic jobs once per period: feed_tick per UTC minute,
    sla_check per UTC hour, and daily_sweep once its local day reaches sweep_hour_local.
    Per tenant, so one tenant's failure leaves the others' work running. A periodic job
    still queued absorbs the next one (same dedupe key)."""
    utc = now.astimezone(UTC)
    with db.system_tx() as conn:
        for t in conn.execute("SELECT id, config FROM tenants ORDER BY slug").fetchall():
            for kind, period in (("feed_tick", f"{utc:%Y-%m-%dT%H:%M}"),
                                 ("sla_check", f"{utc:%Y-%m-%dT%H}")):
                if jobs.once_per_period(conn, t["id"], kind, period):
                    jobs.enqueue(conn, kind, t["id"], {}, dedupe_key=kind, lane=0, run_at=now)
            local = now.astimezone(ZoneInfo(t["config"]["timezone"]))
            period = local.date().isoformat()
            if (local.hour >= t["config"]["sweep_hour_local"]
                    and jobs.once_per_period(conn, t["id"], "daily_sweep", period)):
                jobs.enqueue(conn, "daily_sweep", t["id"], {"period": period},
                             dedupe_key="daily_sweep", lane=1, run_at=now)


def tick(db: Database, worker_id: str, now: datetime) -> int:
    """Reap, schedule what is due at `now`, then claim and run up to MAX_JOBS_PER_TICK jobs
    one at a time (so jobs they enqueue run in this tick too, live lane first). Returns how
    many ran. One time source: claims and finishes use `now` plus the clock's progress since
    the tick began, so a lease still starts when its job does."""
    skew = now - clock.now()

    def at() -> datetime:
        return clock.now() + skew
    reap(db, now)
    schedule(db, now)
    for ran in range(MAX_JOBS_PER_TICK):
        with db.system_tx() as conn:
            claimed = jobs.claim(conn, worker_id, n=1, now=at())
        if not claimed:
            return ran
        job = claimed[0]
        try:
            HANDLERS[job.kind](db, job)
        except Exception as e:  # any failure: retry with backoff, dead after max attempts
            with db.system_tx() as conn:
                jobs.fail(conn, job, f"{type(e).__name__}: {e}", now=at())
        else:
            with db.system_tx() as conn:
                jobs.complete(conn, job, now=at())
    return MAX_JOBS_PER_TICK


def run_for(db: Database, worker_id: str, budget_s: float) -> dict:
    """Tick until a tick runs nothing or budget_s of wall time has passed (one tick may
    overrun it)."""
    start, ticks, ran = time.monotonic(), 0, 0
    while True:
        n = tick(db, worker_id, clock.now())
        ticks, ran = ticks + 1, ran + n
        if n == 0 or time.monotonic() - start >= budget_s:
            return {"ticks": ticks, "jobs": ran,
                    "elapsed_s": round(time.monotonic() - start, 3)}


def run_forever(db: Database, worker_id: str, stop: threading.Event,
                idle_sleep: float = 1.0) -> None:
    """Tick until stopped. A transient database error (lost connection, pool timeout) is
    logged and retried after a capped exponential backoff instead of ending the worker."""
    failures = 0
    while not stop.is_set():
        try:
            ran = tick(db, worker_id, clock.now())
        except (psycopg.OperationalError, psycopg.InterfaceError):  # InterfaceError: conn closed
            failures += 1
            log.exception("tick failed (%d in a row); backing off", failures)
            stop.wait(min(MAX_BACKOFF_S, idle_sleep * 2**failures))
            continue
        failures = 0
        if ran == 0:
            stop.wait(idle_sleep)
