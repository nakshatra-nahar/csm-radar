"""Bootstrap a demo database (design §10): tenants, staff, the feed replayed up to now, a
first model per tenant, a history of weekly score runs, the first sweep, this week's lists
and their briefs, and the rest of the feed buffered in the database. Idempotent."""

import math
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
from psycopg.types.json import Jsonb
from sklearn.linear_model import LogisticRegression

from radar import clock, model
from radar.agent import jobs, worker
from radar.agent.handlers import freeze_week
from radar.asof import load_frames
from radar.config import Settings
from radar.db import Database, owner_connect
from radar.ingest import feed
from radar.ingest.pipeline import IngestStats
from radar.labels import MATURITY, build_training_set, snapshot_grid
from radar.migrate import migrate
from radar.scoring import score_tenant
from radar.sim import generate, staff
from radar.tenants import TENANTS, runtime_config, tenant_id

HISTORY_END = datetime(2026, 10, 17, tzinfo=UTC)  # the generator's default "now"
CALIBRATION_WEEKS = 8
ECE_MAX, ECE_MIN_ROWS = 0.02, 300  # design §6: Platt only past this error, on enough rows


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


def _ece(artifact: dict, rows: pd.DataFrame) -> dict[str, dict]:
    from radar.evaluate import _reliability  # evaluate imports this module: import late
    p, _ = model.predict(artifact, rows)
    t = pd.DataFrame({"model": p, "label": rows["label"].to_numpy(),
                      "billing_interval": rows["billing_interval"].to_numpy()})
    return {seg: {"rows": v["rows"], "ece": v["ece"]} for seg, v in _reliability(t).items()}


def _platt(artifact: dict, train: pd.DataFrame) -> tuple[dict | None, dict]:
    """(platt, metrics): Platt (a, b) fitted on the latest matured weeks when a billing
    interval with enough rows is miscalibrated there (design §6, Ruling Z)."""
    weeks = sorted(set(train.index.get_level_values("as_of")))[-CALIBRATION_WEEKS:]
    recent = train[train.index.get_level_values("as_of").isin(weeks)]
    before = _ece(artifact, recent)
    if not any(v["rows"] >= ECE_MIN_ROWS and v["ece"] > ECE_MAX for v in before.values()):
        return None, {"ece": before}
    p, _ = model.predict(artifact, recent)
    logit = np.log(p / (1 - p)).reshape(-1, 1)
    lr = LogisticRegression(C=np.inf).fit(logit, recent["label"])  # unpenalised
    platt = {"a": float(lr.intercept_[0]), "b": float(lr.coef_[0, 0])}
    if platt["b"] <= 0:  # a non-increasing map would reorder the top-K: leave p as it is
        return None, {"ece": before, "platt_rejected": platt}
    return platt, {"ece": before, "ece_platt": _ece(artifact | {"platt": platt}, recent)}


def train_and_promote(db: Database, tenant_id, as_of: datetime) -> UUID | None:
    """Fit the tenant's model on every matured Monday up to as_of and make it the active one,
    promoted by 'system'. None for a tenant without a model, with too few decisions, or whose fit
    fails the sign gate (stored as 'rejected' with its wrong_signs in metrics)."""
    with db.tenant_tx(tenant_id) as conn:
        tenant = conn.execute("SELECT * FROM tenants WHERE id = %s", (tenant_id,)).fetchone()
        frames = load_frames(conn, tenant_id)
    spec = TENANTS[tenant["slug"]]
    if not spec.has_model:
        return None
    # training starts once every source has been connected for 120 days (Task 5)
    start = datetime.combine(max(spec.source_start.values()), datetime.min.time(), UTC)
    grid = snapshot_grid(start + timedelta(days=120), as_of - MATURITY)
    train = build_training_set(frames, tenant, grid, as_of)
    decisions = train.index.get_level_values("account_id")[train["label"] == 1].nunique()
    if decisions < tenant["config"]["model"]["min_decisions"]:
        return None
    artifact = model.fit(train)
    # design §6 gate: every behavioural sign matches risk_direction. shortcut: the recall@K
    # gate needs the rolling-origin eval and is deferred, add it when retraining is scheduled
    wrong = model.check_signs(artifact)
    metrics = {"wrong_signs": wrong} if wrong else {}
    if not wrong:
        artifact["platt"], metrics = _platt(artifact, train)
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
    Its dedupe key absorbs the day's scheduled sweep while it waits."""
    with db.tenant_tx(tid) as conn:
        if conn.execute("SELECT 1 FROM audit_log WHERE action = 'bootstrap_backlog'"
                        " LIMIT 1").fetchone():
            return
        jobs.enqueue(conn, "daily_sweep", tid, {"bootstrap": True}, dedupe_key="daily_sweep",
                     lane=1, run_at=now)


def _freeze_this_week(db: Database, tid, now: datetime) -> None:
    """Ruling AG: freeze the current ISO week from the latest actionable run even when today
    is not Monday, so the app shows a frozen list on day one."""
    with db.tenant_tx(tid, actor="agent") as conn:
        cfg = conn.execute("SELECT config FROM tenants WHERE id = %s",
                           (tid,)).fetchone()["config"]
        run = conn.execute("SELECT id FROM score_runs WHERE status = 'done' AND actionable"
                           " ORDER BY as_of DESC, finished_at DESC LIMIT 1").fetchone()
        if run:
            today = now.astimezone(ZoneInfo(cfg["timezone"])).date()
            freeze_week(conn, tid, run["id"], today - timedelta(days=today.weekday()), now, cfg)


def bootstrap(settings: Settings, scale: float = 1.0) -> dict:
    """Each step skips what an earlier run already did, so a re-run adds nothing."""
    now, slugs = clock.now(), list(TENANTS)
    applied = migrate(settings.owner_dsn, settings.web_password, settings.worker_password)
    db = Database(settings.worker_dsn)
    try:
        seed_tenants_and_users(settings.owner_dsn, db, slugs)
        if not (settings.data_dir / "feed").is_dir():
            generate(settings.data_dir, seed=7, scale=scale)
        ingested = {s: replay_feed(db, tenant_id(s), settings.data_dir, now).accepted
                    for s in slugs}
        for slug in slugs:
            tid = tenant_id(slug)
            with db.tenant_tx(tid) as conn:
                trained = conn.execute("SELECT 1 FROM models LIMIT 1").fetchone()
            if not trained:
                train_and_promote(db, tid, now)
            backfill_runs(db, tid, end=now)
            _bootstrap_sweep(db, tid, now)
        swept = worker.run_for(db, "bootstrap", budget_s=math.inf)  # until the queue is idle
        for slug in slugs:
            _freeze_this_week(db, tenant_id(slug), now)
        briefs = worker.run_for(db, "bootstrap", budget_s=math.inf)
        buffered = {s: feed.load_buffer(db, tenant_id(s), settings.data_dir) for s in slugs}
    finally:
        db.close()
    return {"migrations": applied, "ingested": ingested, "buffered": buffered,
            "jobs": swept["jobs"] + briefs["jobs"]}
