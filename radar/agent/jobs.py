"""The Postgres job queue: coalesced enqueue, SKIP LOCKED claims, leases with fencing, backoff.

Every call runs on the caller's connection (system_tx for the worker). The database's now()
ignores radar.clock, so each timestamp is passed in from `now`.
"""

import random
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

import psycopg
from psycopg.rows import class_row, scalar_row
from psycopg.types.json import Jsonb

from radar import clock

BACKOFF_BASE_S, BACKOFF_CAP_S = 5, 3600
# A twin with the same key, queued or running and newer, does this job's work: requeueing the
# job instead would collide with the queued one on jobs_dedupe_idx and wedge fail() and reap().
_HAS_TWIN = ("EXISTS (SELECT 1 FROM jobs t WHERE t.dedupe_key = jobs.dedupe_key"
             " AND t.tenant_id IS NOT DISTINCT FROM jobs.tenant_id AND t.id <> jobs.id"
             " AND (t.status = 'queued' OR (t.status = 'running' AND t.id > jobs.id)))")


@dataclass(frozen=True)
class Job:
    id: int
    tenant_id: UUID | None
    kind: str
    payload: dict
    attempts: int  # this claim's attempt number: the fencing token for complete() and fail()
    lease_expires_at: datetime


def enqueue(conn: psycopg.Connection, kind: str, tenant_id: UUID | None, payload: dict,
            dedupe_key: str | None = None, lane: int = 0,
            run_at: datetime | None = None) -> int | None:
    """Insert a job; None when an identical queued job (same tenant and key) already exists."""
    # The ON CONFLICT target repeats jobs_dedupe_idx's predicate exactly, or Postgres
    # cannot infer the partial index.
    with conn.cursor(row_factory=scalar_row) as cur:
        return cur.execute(
            "INSERT INTO jobs (tenant_id, kind, payload, dedupe_key, lane, run_at)"
            " VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (tenant_id, dedupe_key)"
            " WHERE status = 'queued' AND dedupe_key IS NOT NULL DO NOTHING RETURNING id",
            (tenant_id, kind, Jsonb(payload), dedupe_key, lane, run_at or clock.now()),
        ).fetchone()


def claim(conn: psycopg.Connection, n: int = 10, lease_s: int = 300,
          now: datetime | None = None) -> list[Job]:
    """Lease up to n due jobs, live lane first. Concurrent claimers skip each other's rows;
    the attempts fence, not a worker id, tells two claimers apart."""
    now = now or clock.now()
    with conn.cursor(row_factory=class_row(Job)) as cur:
        return cur.execute(
            "WITH due AS ("
            "  SELECT id FROM jobs WHERE status = 'queued' AND run_at <= %(now)s"
            "  ORDER BY lane, run_at, id LIMIT %(n)s FOR NO KEY UPDATE SKIP LOCKED"
            "), claimed AS ("
            "  UPDATE jobs SET status = 'running', attempts = jobs.attempts + 1,"
            "    lease_expires_at = %(lease)s"
            "  FROM due WHERE jobs.id = due.id"
            "  RETURNING jobs.id, jobs.tenant_id, jobs.kind, jobs.payload, jobs.attempts,"
            "    jobs.lease_expires_at, jobs.lane, jobs.run_at"
            ") SELECT id, tenant_id, kind, payload, attempts, lease_expires_at FROM claimed"
            " ORDER BY lane, run_at, id",
            {"now": now, "n": n, "lease": now + timedelta(seconds=lease_s)},
        ).fetchall()


def complete(conn: psycopg.Connection, job: Job, now: datetime | None = None) -> bool:
    """Mark done; False if the lease was lost (reaped, maybe re-claimed by another worker)."""
    cur = conn.execute(
        "UPDATE jobs SET status = 'done', finished_at = %s, lease_expires_at = NULL"
        " WHERE id = %s AND attempts = %s AND status = 'running'",
        (now or clock.now(), job.id, job.attempts),
    )
    return cur.rowcount == 1


def fail(conn: psycopg.Connection, job: Job, error: str, now: datetime | None = None) -> None:
    """Requeue after a full-jitter backoff, or mark dead once attempts reach the row's
    max_attempts (the column reap() reads too). Fenced like complete()."""
    now = now or clock.now()
    wait = random.uniform(0, min(BACKOFF_CAP_S, BACKOFF_BASE_S * 2**job.attempts))
    args = {"id": job.id, "n": job.attempts, "now": now, "error": error,
            "run_at": now + timedelta(seconds=wait)}
    fence = " WHERE id = %(id)s AND attempts = %(n)s AND status = 'running'"
    dead = ("UPDATE jobs SET status = 'dead', finished_at = %(now)s, lease_expires_at = NULL,"
            " last_error = ")
    if conn.execute(dead + "%(error)s" + fence + " AND attempts >= max_attempts",
                    args).rowcount:
        return
    superseded = dead + "'superseded: ' || %(error)s" + fence
    if conn.execute(superseded + " AND " + _HAS_TWIN, args).rowcount:
        return
    try:
        with conn.transaction():  # a savepoint: a twin committed since the check takes the slot
            conn.execute("UPDATE jobs SET status = 'queued', run_at = %(run_at)s,"
                         " lease_expires_at = NULL, last_error = %(error)s" + fence, args)
    except psycopg.errors.UniqueViolation:
        conn.execute(superseded, args)


def reap(conn: psycopg.Connection, now: datetime | None = None) -> int:
    """Return expired leases to the queue; a job out of attempts goes dead instead.

    Without the dead branch a job that kills its worker every time would be retried forever.
    A twin committed between the two statements collides on jobs_dedupe_idx: one retry sees
    it and supersedes instead.
    """
    now = now or clock.now()
    try:
        with conn.transaction():  # a savepoint, so the retry runs in a clean transaction
            return _reap(conn, now)
    except psycopg.errors.UniqueViolation:
        return _reap(conn, now)


def _reap(conn: psycopg.Connection, now: datetime) -> int:
    superseded = conn.execute(
        "UPDATE jobs SET status = 'dead', finished_at = %(now)s, lease_expires_at = NULL,"
        " last_error = 'superseded' WHERE status = 'running' AND lease_expires_at < %(now)s"
        " AND " + _HAS_TWIN, {"now": now}).rowcount
    return superseded + conn.execute(
        "UPDATE jobs SET lease_expires_at = NULL,"
        " status = CASE WHEN attempts >= max_attempts THEN 'dead' ELSE 'queued' END,"
        " finished_at = CASE WHEN attempts >= max_attempts THEN %(now)s END,"
        " last_error = CASE WHEN attempts >= max_attempts THEN 'lease expired' ELSE last_error END"
        " WHERE status = 'running' AND lease_expires_at < %(now)s",
        {"now": now},
    ).rowcount


def once_per_period(conn: psycopg.Connection, tenant_id: UUID | None, kind: str,
                    period_key: str) -> bool:
    """True for the one call that records (tenant, kind, period); False for every later one."""
    return conn.execute(
        "INSERT INTO schedule_runs (tenant_id, kind, period_key) VALUES (%s, %s, %s)"
        " ON CONFLICT DO NOTHING",
        (tenant_id, kind, period_key),
    ).rowcount == 1
