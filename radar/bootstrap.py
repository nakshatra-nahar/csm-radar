"""Bootstrap a demo database (design §10): tenants, staff, the feed replayed up to now, a
first model per tenant, a history of weekly score runs, the first sweep, this week's lists
and their briefs, and the rest of the feed buffered in the database. Idempotent."""

import logging
import math
from datetime import datetime, time, timedelta
from pathlib import Path
from uuid import UUID
from zoneinfo import ZoneInfo

from psycopg.types.json import Jsonb

from radar import clock, model
from radar.agent import jobs, worker
from radar.agent.handlers import freeze_week
from radar.asof import load_frames
from radar.config import Settings
from radar.db import Database, assert_role_safety, owner_connect
from radar.ingest import feed
from radar.ingest.pipeline import IngestStats
from radar.labels import build_training_set, snapshot_grid, training_grid
from radar.migrate import migrate
from radar.scoring import score_tenant
from radar.sim import generate, staff
from radar.tenants import TENANTS, runtime_config, tenant_id

log = logging.getLogger(__name__)


def merge_config(defaults: dict, stored: dict) -> dict:
    """Stored values win; keys only in the defaults are added, at any depth."""
    return defaults | {k: merge_config(defaults[k], v)
                       if isinstance(v, dict) and isinstance(defaults.get(k), dict) else v
                       for k, v in stored.items()}


def seed_tenants_and_users(owner_dsn: str, db: Database, slugs: list[str], seed: int = 7) -> None:
    """Idempotent. Tenants go in as the owner: the app roles may not insert tenants. An
    existing tenant's config gains the config keys added since it was seeded (a new
    config_version when that changes it)."""
    with owner_connect(owner_dsn) as conn:
        for slug in slugs:
            spec, config = TENANTS[slug], runtime_config(slug)
            with conn.transaction():
                conn.execute(
                    "INSERT INTO tenants (id, slug, name, timezone, currency, internal_domain,"
                    " config, config_version) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)"
                    " ON CONFLICT (id) DO NOTHING",
                    (tenant_id(slug), slug, spec.name, spec.timezone, spec.currency,
                     spec.internal_domain, Jsonb(config), config["config_version"]))
                stored, version = conn.execute(
                    "SELECT config, config_version FROM tenants WHERE id = %s FOR UPDATE",
                    (tenant_id(slug),)).fetchone()
                merged = merge_config(config, stored)
                if merged != stored:
                    merged["config_version"] = version + 1
                    conn.execute("UPDATE tenants SET config = %s, config_version = %s"
                                 " WHERE id = %s", (Jsonb(merged), merged["config_version"],
                                                    tenant_id(slug)))
    for slug in slugs:
        with db.tenant_tx(tenant_id(slug)) as conn:
            for user in staff(slug, seed):  # managers come first, so their ids exist
                conn.execute(
                    "INSERT INTO users (email, name, role, sf_user_id, manager_id)"
                    " VALUES (%s, %s, %s, %s, (SELECT id FROM users WHERE email = %s))"
                    " ON CONFLICT (tenant_id, email) DO NOTHING",
                    (user["email"], user["name"], user["role"], user["sf_user_id"],
                     user["manager_email"]))


def replay_feed(db: Database, tenant_id, data_dir: Path, until: datetime) -> IngestStats:
    """Deliver every feed record with deliver_at <= until (db should be radar_worker)."""
    return feed.deliver_due(db, tenant_id, until, data_dir)


def train_and_promote(db: Database, tenant_id, as_of: datetime) -> UUID | None:
    """Fit the tenant's model on every matured Monday up to as_of and make it the active one,
    promoted by 'system'. None for a tenant without a model, with too few decisions, or whose fit
    fails the sign gate (stored as 'rejected' with its wrong_signs in metrics)."""
    with db.tenant_tx(tenant_id) as conn:
        tenant = conn.execute("SELECT * FROM tenants WHERE id = %s", (tenant_id,)).fetchone()
        if not TENANTS[tenant["slug"]].has_model:
            return None
        frames = load_frames(conn, tenant_id)
    grid = training_grid(tenant["slug"], as_of)
    train = build_training_set(frames, tenant, grid, as_of)
    decisions = model.decisions(train)
    if decisions < tenant["config"]["model"]["min_decisions"]:
        return None
    artifact = model.fit(train)
    # design §6 gate: every behavioural sign matches risk_direction. shortcut: the recall@K
    # gate needs the rolling-origin eval and is deferred, add it when retraining is scheduled
    wrong = model.check_signs(artifact)
    metrics = {"wrong_signs": wrong} if wrong else {}
    if not wrong:
        artifact["platt"], metrics = model.platt(artifact, train)
    status, now = ("rejected" if wrong else "active"), clock.now()
    with db.tenant_tx(tenant_id) as conn:
        if not wrong:
            conn.execute("UPDATE models SET status = 'retired' WHERE status = 'active'")
        model_id = conn.execute(
            "INSERT INTO models (version, status, artifact, manifest, label_version,"
            " train_window, metrics, trained_at, promoted_at, promoted_by)"
            " SELECT coalesce(max(version), 0) + 1, %s, %s, %s, %s, %s, %s, %s, %s, %s"
            " FROM models RETURNING id",
            (status, Jsonb(artifact), Jsonb(artifact["manifest"]), artifact["label_version"],
             Jsonb({"first": grid[0].isoformat(), "last": grid[-1].isoformat(),
                    "label_cutoff": as_of.isoformat()}),
             Jsonb(metrics | {"n_train": artifact["n_train"], "n_decisions": decisions}),
             now, None if wrong else now, None if wrong else "system")).fetchone()["id"]
    return None if wrong else model_id


def backfill_runs(db: Database, tenant_id, weeks: int = 12, *, end: datetime) -> list[UUID]:
    """One non-actionable score run per Monday for the `weeks` Mondays up to end, so history
    and "what changed" work from the first day."""
    mondays = snapshot_grid(end - timedelta(weeks=weeks), end)[-weeks:]
    with db.tenant_tx(tenant_id) as conn:
        done = {r["as_of"] for r in conn.execute(
            "SELECT as_of FROM score_runs WHERE trigger = 'backfill' AND status = 'done'")}
    return [score_tenant(db, tenant_id, T.to_pydatetime(), "backfill", False)
            for T in mondays if T.to_pydatetime() not in done]


def _bootstrap_sweep(db: Database, tid, now: datetime) -> None:
    """Queue the one actionable sweep that opens the backlog past the breaker (Ruling AB).
    It counts as that local day's sweep (Ruling BC), so the scheduled one does not run too."""
    with db.tenant_tx(tid) as conn:
        if conn.execute("SELECT 1 FROM audit_log WHERE action = 'bootstrap_backlog'"
                        " LIMIT 1").fetchone():
            return
        tz = conn.execute("SELECT config->>'timezone' AS tz FROM tenants WHERE id = %s",
                          (tid,)).fetchone()["tz"]
        jobs.once_per_period(conn, tid, "daily_sweep", now.astimezone(ZoneInfo(tz)).date()
                             .isoformat())
        jobs.enqueue(conn, "daily_sweep", tid, {"bootstrap": True}, dedupe_key="daily_sweep",
                     lane=1, run_at=now)


def _freeze_this_week(db: Database, tid, now: datetime) -> None:
    """Ruling AG: freeze the current ISO week from the latest actionable run even when today
    is not Monday, so the app shows a frozen list on day one. Only a run from this local week
    counts: a list is never frozen from stale or missing scores (as in daily_sweep)."""
    with db.tenant_tx(tid, actor="agent") as conn:
        cfg = conn.execute("SELECT config FROM tenants WHERE id = %s",
                           (tid,)).fetchone()["config"]
        tz = ZoneInfo(cfg["timezone"])
        today = now.astimezone(tz).date()
        monday = today - timedelta(days=today.weekday())
        start = datetime.combine(monday, time(), tz)
        run = conn.execute("SELECT id FROM score_runs WHERE status = 'done' AND actionable"
                           " AND as_of >= %s AND as_of < %s"
                           " ORDER BY as_of DESC, finished_at DESC LIMIT 1",
                           (start, start + timedelta(weeks=1))).fetchone()
        if run is None:
            log.info("bootstrap: no actionable run in the week of %s for tenant %s;"
                     " not freezing", monday, tid)
            return
        freeze_week(conn, tid, run["id"], monday, now, cfg)


def bootstrap(settings: Settings, scale: float = 1.0, tenants: list[str] | None = None) -> dict:
    """Each step skips what an earlier run already did, so a re-run adds nothing. `tenants`
    limits the run to some tenants: a free Neon project (512 MB) holds two of the three."""
    slugs = list(tenants or TENANTS)
    unknown = sorted(set(slugs) - set(TENANTS))
    if unknown:
        raise ValueError(f"unknown tenants: {', '.join(unknown)}")
    now = clock.now()
    applied = migrate(settings.owner_dsn, settings.web_password, settings.worker_password)
    db = Database(settings.worker_dsn)
    try:
        with db.system_tx() as conn:  # as `radar worker`: never run jobs past RLS
            assert_role_safety(conn)
        seed_tenants_and_users(settings.owner_dsn, db, slugs)
        if not (settings.data_dir / "feed").is_dir():
            generate(settings.data_dir, seed=7, scale=scale)
        ingested = {s: replay_feed(db, tenant_id(s), settings.data_dir, now).accepted
                    for s in slugs}
        for slug in slugs:
            tid = tenant_id(slug)
            with db.tenant_tx(tid) as conn:
                trained = conn.execute("SELECT 1 FROM models LIMIT 1").fetchone()
            if not trained and TENANTS[slug].has_model:
                train_and_promote(db, tid, now)
            backfill_runs(db, tid, end=now)
            _bootstrap_sweep(db, tid, now)
        swept = worker.run_for(db, budget_s=math.inf)  # until the queue is idle
        for slug in slugs:
            _freeze_this_week(db, tenant_id(slug), now)
        briefs = worker.run_for(db, budget_s=math.inf)
        buffered = {s: feed.load_buffer(db, tenant_id(s), settings.data_dir) for s in slugs}
    finally:
        db.close()
    return {"migrations": applied, "ingested": ingested, "buffered": buffered,
            "jobs": swept["jobs"] + briefs["jobs"]}
