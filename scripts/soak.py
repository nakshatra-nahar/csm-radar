"""Unattended soak: run the worker over simulated days on a bootstrapped database.

Freezes radar.clock at the bootstrap's last finished job and advances it in steps, calling
worker.run_for at each step the way the GitHub Actions schedule calls /internal/tick. Prints
a JSON summary:

    uv run python scripts/soak.py --dsn <owner DSN> [--days 7] [--step-minutes 15]

The DSN must be a superuser's (a local database): the summary reads every tenant past RLS.
The worker runs as radar_worker (password from RADAR_WORKER_PASSWORD). Feed files are read
from RADAR_DATA_DIR. Without ANTHROPIC_API_KEY every brief is a template.
"""

import argparse
import json
import time
from collections import defaultdict
from datetime import timedelta

from psycopg.conninfo import make_conninfo

from radar import clock
from radar.agent import worker
from radar.agent.handlers import HANDLERS
from radar.config import load_settings
from radar.db import Database, owner_connect


def _timed(kind: str, fn, times: list):
    def run(db, job):
        t0 = time.monotonic()
        try:
            fn(db, job)
        finally:
            times.append((kind, job.tenant_id, time.monotonic() - t0))
    return run


def _nested(rows) -> dict:
    """[(a, b, n)] -> {a: {b: n}}"""
    out: dict = defaultdict(dict)
    for a, b, n in rows:
        out[a][str(b)] = n
    return dict(out)


def _marks(conn) -> dict:
    """High-water marks before the soak: rows past them are the soak's."""
    one = lambda sql: conn.execute(sql).fetchone()[0]  # noqa: E731
    return {"raw": one("SELECT coalesce(max(id), 0) FROM raw_records"),
            "job": one("SELECT coalesce(max(id), 0) FROM jobs"),
            "briefs": dict(conn.execute(
                "SELECT t.slug, count(b.id) FROM tenants t LEFT JOIN briefs b"
                " ON b.tenant_id = t.id GROUP BY 1").fetchall())}


def _summary(conn, start, marks: dict) -> dict:
    q = lambda sql, *args: conn.execute(sql, args).fetchall()  # noqa: E731
    briefs = dict(q("SELECT t.slug, count(b.id) FROM tenants t LEFT JOIN briefs b"
                    " ON b.tenant_id = t.id GROUP BY 1"))
    return {
        "delivered": _nested(q(
            "SELECT t.slug, r.source, count(*) FROM raw_records r JOIN tenants t"
            " ON t.id = r.tenant_id WHERE r.id > %s GROUP BY 1, 2 ORDER BY 1, 2", marks["raw"])),
        "jobs": _nested(q("SELECT kind, status, count(*) FROM jobs WHERE id > %s"
                          " GROUP BY 1, 2 ORDER BY 1, 2", marks["job"])),
        "sweeps": _nested(q(
            "SELECT t.slug, j.payload->>'period', count(*) FROM jobs j JOIN tenants t"
            " ON t.id = j.tenant_id WHERE j.id > %s AND j.kind = 'daily_sweep'"
            " AND j.status = 'done' GROUP BY 1, 2 ORDER BY 1, 2", marks["job"])),
        "lists_frozen": _nested(q(
            "SELECT t.slug, w.week_start, count(*) FROM worklists w JOIN tenants t"
            " ON t.id = w.tenant_id WHERE w.frozen_at > %s GROUP BY 1, 2 ORDER BY 1, 2", start)),
        "escalated": dict(q("SELECT t.slug, count(*) FROM cases c JOIN tenants t"
                            " ON t.id = c.tenant_id WHERE c.escalated_at > %s GROUP BY 1", start)),
        "stale_opened": _nested(q(
            "SELECT t.slug, i.source, count(*) FROM incidents i JOIN tenants t"
            " ON t.id = i.tenant_id WHERE i.kind = 'source_stale' AND i.opened_at > %s"
            " GROUP BY 1, 2 ORDER BY 1, 2", start)),
        "stale_closed": _nested(q(
            "SELECT t.slug, i.source, count(*) FROM incidents i JOIN tenants t"
            " ON t.id = i.tenant_id WHERE i.kind = 'source_stale' AND i.closed_at > %s"
            " GROUP BY 1, 2 ORDER BY 1, 2", start)),
        "dead_jobs": q("SELECT count(*) FROM jobs WHERE status = 'dead'")[0][0],
        "score_runs_running": q("SELECT count(*) FROM score_runs WHERE status = 'running'")[0][0],
        "briefs": {s: n - marks["briefs"].get(s, 0) for s, n in briefs.items()},
    }


def run(dsn: str, days: int = 7, step_minutes: int = 15) -> dict:
    settings = load_settings()
    step = timedelta(minutes=step_minutes)
    per_day = timedelta(days=1) // step
    times: list = []
    db = Database(make_conninfo(dsn, user="radar_worker", password=settings.worker_password))
    saved = dict(HANDLERS)
    HANDLERS.update({kind: _timed(kind, fn, times) for kind, fn in saved.items()})
    try:
        with owner_connect(dsn, autocommit=True) as conn:
            start = conn.execute("SELECT max(finished_at) FROM jobs"
                                 " WHERE status = 'done'").fetchone()[0]
            if start is None:
                raise SystemExit("soak: no finished job in this database; run `radar"
                                 " bootstrap` on it first")
            marks = _marks(conn)
            size = lambda: conn.execute(  # noqa: E731
                "SELECT pg_database_size(current_database())").fetchone()[0]
            sizes, t0 = [size()], time.monotonic()
            for i in range(1, days * per_day + 1):
                clock.freeze(start + i * step)
                worker.run_for(db, settings.tick_budget_s)
                if i % per_day == 0:
                    sizes.append(size())
            wall = time.monotonic() - t0
            out = _summary(conn, start, marks)
            slugs = dict(conn.execute("SELECT id, slug FROM tenants").fetchall())
    finally:
        clock.unfreeze()
        HANDLERS.update(saved)
        db.close()
    secs: dict = defaultdict(list)
    sweep_s: dict = defaultdict(list)
    for kind, tid, s in times:
        secs[kind].append(s)
        if kind == "daily_sweep":
            sweep_s[slugs[tid]].append(round(s, 1))
    return {"start": start.isoformat(), "end": (start + days * per_day * step).isoformat(),
            "wall_s": round(wall, 1), **out, "db_bytes_per_day": sizes,
            "handler_s": {k: {"n": len(v), "total": round(sum(v), 1), "max": round(max(v), 1)}
                          for k, v in sorted(secs.items())},
            "sweep_s": dict(sweep_s)}


def main(argv=None) -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--dsn", required=True, help="superuser owner DSN of a bootstrapped database")
    p.add_argument("--days", type=int, default=7)
    p.add_argument("--step-minutes", type=int, default=15)
    a = p.parse_args(argv)
    print(json.dumps(run(a.dsn, a.days, a.step_minutes), indent=1, default=str))


if __name__ == "__main__":
    main()
