"""The measurements behind docs/measurements.md that no other command makes. Each prints JSON:

    uv run python scripts/measure_10x.py world <slug> [--scale 10]  # generator counts, no files
    uv run python scripts/measure_10x.py tile [--copies 10]   # hearthline frames tiled in memory
    uv run python scripts/measure_10x.py explain              # hot queries as the app roles
    uv run python scripts/measure_10x.py briefs               # brief evidence wall time
    uv run python scripts/measure_10x.py sweep --dsn <a copy> # one daily sweep per tenant

--dsn is a superuser's DSN for the bootstrapped database (default radar_full). `explain` and
`briefs` run as radar_web and radar_worker (RADAR_WEB_PASSWORD, RADAR_WORKER_PASSWORD), so
row-level security applies as in the app. Only `sweep` writes (scores, cases, lists), so run
it on a copy (CREATE DATABASE … TEMPLATE radar_full), never on radar_full itself.
"""

import argparse
import json
import resource
import time
import uuid
from datetime import UTC, datetime

import pandas as pd
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row

from radar import clock
from radar.agent import evidence, handlers
from radar.agent.jobs import Job
from radar.asof import Frames, load_frames
from radar.config import load_settings
from radar.db import Database, owner_connect
from radar.features import compute_features, rule_facts
from radar.scoring import score_frame
from radar.sim import generate
from radar.tenants import tenant_id
from radar.web import queries

DSN = "postgresql://postgres:postgres@localhost:54329/radar_full"
T = datetime(2026, 10, 18, 7, 50, 42, tzinfo=UTC)  # radar_full's bootstrap, last job done
KEYS = ["entity_id", "account_ref", "email", "subscription_id", "failed_recipient"]


def rss_mib() -> int:
    return round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 2**20)  # bytes on macOS


def timed(fn):
    t0 = time.monotonic()
    out = fn()
    return out, round(time.monotonic() - t0, 3)


def world(slug: str, scale: float) -> dict:
    out, wall = timed(lambda: generate("/nonexistent", seed=7, scale=scale, tenants=[slug],
                                       count_only=True))
    return out | {"wall_s": wall, "rss_peak_mib": rss_mib()}


# --- tile: copy k of hearthline gets new ids and keys, so copies never join each other -----

def _tag(s: pd.Series, k: int) -> pd.Series:  # sf:001… -> sf:x3.001…
    return s.str.replace(r"^([^:]*:)?", rf"\1x{k}.", regex=True)


def _remap(s: pd.Series, k: int) -> pd.Series:
    return s.map(lambda u: uuid.uuid5(uuid.NAMESPACE_OID, f"{u}:{k}"))


def _copy(f: Frames, k: int) -> Frames:
    if k == 0:
        return f
    ev = f.events.assign(id=f.events["id"] + k * 10**9,
                         **{c: _tag(f.events[c], k) for c in KEYS})
    ln = f.links.assign(id=f.links["id"] + k * 10**9, account_ref=_tag(f.links["account_ref"], k),
                        account_id=_remap(f.links["account_id"], k))
    ac = f.accounts.assign(id=_remap(f.accounts["id"], k),
                           sf_account_id=_tag(f.accounts["sf_account_id"], k))
    return Frames(ev, ln, ac, f.users)  # users are not tiled: each book is n times larger


def _tile(f: Frames, n: int) -> Frames:
    parts = [_copy(f, k) for k in range(n)]
    ev = pd.concat([p.events for p in parts], ignore_index=True).sort_values(
        ["entity_id", "occurred_at", "version_key"], kind="stable", ignore_index=True)
    ln = pd.concat([p.links for p in parts], ignore_index=True).sort_values(
        ["account_ref", "available_at", "id"], ignore_index=True)
    return Frames(ev, ln, pd.concat([p.accounts for p in parts], ignore_index=True), f.users)


def tile(dsn: str, copies: int) -> dict:
    tid, out = tenant_id("hearthline"), {"rss_imports_mib": rss_mib()}
    with owner_connect(dsn, row_factory=dict_row) as conn:
        tenant = conn.execute("SELECT * FROM tenants WHERE id = %s", (tid,)).fetchone()
        artifact = conn.execute("SELECT artifact FROM models WHERE tenant_id = %s"
                                " AND status = 'active'", (tid,)).fetchone()["artifact"]
        f1, out["load_frames_1x_s"] = timed(lambda: load_frames(conn, tid))
    base, out["features_1x_s"] = timed(lambda: compute_features(f1, tenant, [T]))
    _, out["score_frame_1x_s"] = timed(lambda: score_frame(f1, tenant, T, artifact))
    _, out["rule_facts_1x_s"] = timed(lambda: rule_facts(f1, tenant, T))
    out["rss_after_1x_mib"] = rss_mib()
    f = _tile(f1, copies)
    out["rows"] = {"events": len(f.events), "links": len(f.links), "accounts": len(f.accounts)}
    feats, out["features_s"] = timed(lambda: compute_features(f, tenant, [T]))
    scored, out["score_frame_s"] = timed(lambda: score_frame(f, tenant, T, artifact))
    _, out["rule_facts_s"] = timed(lambda: rule_facts(f, tenant, T))
    out |= {"rss_peak_mib": rss_mib(), "scored_active": len(scored),
            "eligible": int(scored["eligible"].astype(bool).sum())}
    # check: copy k of every account has exactly the 1x features
    b, k = base.droplevel("as_of"), min(3, copies - 1)
    got = feats.droplevel("as_of").loc[_remap(b.index.to_series(), k) if k else b.index]
    cols = [c for c in b.columns if b[c].dtype.kind in "fib"]
    same = (got[cols].to_numpy() == b[cols].to_numpy()) | (
        pd.isna(got[cols]).to_numpy() & pd.isna(b[cols]).to_numpy())
    return out | {"copy_matches_1x": bool(same.all())}


# --- explain and briefs: the app's own queries, captured as the app runs them --------------

class Capture:
    """A connection that records each (sql, params) it runs, so EXPLAIN sees the app's SQL."""
    def __init__(self, conn):
        self.conn, self.calls = conn, []

    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        return self.conn.execute(sql, params)


def _as(dsn: str, role: str, password: str) -> Database:
    return Database(make_conninfo(dsn, user=role, password=password), max_size=1)


def _first_query(db: Database, tid, call) -> tuple[str, object]:
    with db.tenant_tx(tid) as conn:
        cap = Capture(conn)
        call(cap)
        return cap.calls[0]


def _explain(db: Database, tid, sql: str, params) -> dict:
    """The first run (cold, after other work) and the second (warm)."""
    out = {}
    for run in ("cold", "warm"):
        with db.tenant_tx(tid) as conn:
            plan = next(iter(conn.execute("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + sql,
                                          params).fetchone().values()))[0]
        top = plan["Plan"]
        out[run] = {"ms": round(plan["Execution Time"], 1), "rows": top["Actual Rows"],
                    "buffers_hit": top["Shared Hit Blocks"],
                    "buffers_read": top["Shared Read Blocks"]}
    with db.tenant_tx(tid) as conn:
        out["plan"] = [next(iter(r.values())) for r in conn.execute("EXPLAIN " + sql, params)]
    return out


def _model_slots(conn, n: int = 3) -> list[dict]:
    """The account_scores rows of the first n model slots of the latest frozen lists."""
    return conn.execute(
        "SELECT s.* FROM worklist_items i JOIN worklists w ON w.id = i.worklist_id"
        " JOIN account_scores s ON s.run_id = w.run_id AND s.account_id = i.account_id"
        " WHERE i.kind = 'model' ORDER BY w.week_start DESC, i.position, w.id LIMIT %s",
        (n,)).fetchall()


def explain(dsn: str) -> dict:
    s, tid = load_settings(), tenant_id("hearthline")
    web, worker = (_as(dsn, "radar_web", s.web_password),
                   _as(dsn, "radar_worker", s.worker_password))
    try:
        with worker.tenant_tx(tid) as conn:
            currency = conn.execute("SELECT config->>'currency' AS c FROM tenants WHERE id = %s",
                                    (tid,)).fetchone()["c"]
            wl = conn.execute("SELECT * FROM worklists ORDER BY week_start DESC, id"
                              " LIMIT 1").fetchone()
            score = _model_slots(conn, 1)[0]
        queries_ = {
            "asof_load": (worker, lambda c: load_frames(c, tid)),
            "week_list": (web, lambda c: queries.worklist_rows(c, wl, currency)),
            "timeline": (web, lambda c: queries.timeline(c, score["account_id"], T, currency)),
            "brief_evidence": (worker, lambda c: evidence._feature_events(
                c, score["account_id"], score, T))}
        return {name: _explain(db, tid, *_first_query(db, tid, call))
                for name, (db, call) in queries_.items()}
    finally:
        web.pool.close()
        worker.pool.close()


def briefs(dsn: str) -> dict:
    """Per tenant: the evidence query for 3 model-slot accounts (at their run's as_of), and
    build_pack for 3 model_topk cases (the whole pack, as a brief job builds it)."""
    s, out = load_settings(), {}
    worker = _as(dsn, "radar_worker", s.worker_password)
    try:
        for slug in ("kitewire", "tallyworks", "hearthline"):
            tid = tenant_id(slug)
            with worker.tenant_tx(tid) as conn:
                query = [timed(lambda r=r: conn.execute(evidence.FEATURE_EVENTS, {
                    "t": r["as_of"], "now": r["as_of"], "a": r["account_id"],
                    "types": evidence.SHOWN_TYPES}).fetchall())[1] for r in _model_slots(conn)]
                cases = [r["id"] for r in conn.execute(
                    "SELECT id FROM cases WHERE kind = 'model_topk' ORDER BY opened_at, id"
                    " LIMIT 3")]
                pack = [timed(lambda c=c, t=tid: evidence.build_pack(conn, t, c))[1] for c in cases]
            out[slug] = {"evidence_query_s": query, "build_pack_s": pack}
    finally:
        worker.pool.close()
    return out


def sweep(dsn: str) -> dict:
    """handlers.daily_sweep per tenant with the clock at Monday 2026-10-26 12:00 UTC (a
    freezing sweep where it is Monday locally), timing score_tenant and the rule facts."""
    if dsn == DSN:
        raise SystemExit("sweep writes: pass --dsn of a copy, not radar_full")
    times, out = {}, {}

    def timer(name, fn):
        def run(*args, **kwargs):
            t0 = time.monotonic()
            try:
                return fn(*args, **kwargs)
            finally:
                times[name] = round(time.monotonic() - t0, 2)
        return run
    s = load_settings()
    worker = _as(dsn, "radar_worker", s.worker_password)
    saved = handlers.score_tenant, handlers._facts
    handlers.score_tenant = timer("score_tenant", handlers.score_tenant)
    handlers._facts = timer("rule_facts", handlers._facts)
    clock.freeze(datetime(2026, 10, 26, 12, tzinfo=UTC))
    try:
        for slug in ("hearthline", "tallyworks", "kitewire"):
            job = Job(0, tenant_id(slug), "daily_sweep", {}, 1, clock.now())
            _, total = timed(lambda j=job: handlers.daily_sweep(worker, j))
            out[slug] = times | {"total": round(total, 2),
                                 "rest": round(total - sum(times.values()), 2)}
    finally:
        handlers.score_tenant, handlers._facts = saved
        clock.unfreeze()
        worker.pool.close()
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("what", choices=["world", "tile", "explain", "briefs", "sweep"])
    ap.add_argument("slug", nargs="?", default="hearthline")
    ap.add_argument("--scale", type=float, default=10)
    ap.add_argument("--copies", type=int, default=10)
    ap.add_argument("--dsn", default=DSN)
    a = ap.parse_args()
    out = {"world": lambda: world(a.slug, a.scale), "tile": lambda: tile(a.dsn, a.copies),
           "explain": lambda: explain(a.dsn), "briefs": lambda: briefs(a.dsn),
           "sweep": lambda: sweep(a.dsn)}[a.what]()
    print(json.dumps(out, indent=1, default=str))


if __name__ == "__main__":
    main()
