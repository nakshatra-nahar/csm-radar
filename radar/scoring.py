"""Traceable score runs (design §5, §6): every stored score carries its inputs, its model and
code versions, and contributions that sum exactly to its logit."""

import functools
import subprocess
from datetime import datetime
from pathlib import Path
from uuid import UUID

import numpy as np
import pandas as pd
from psycopg.types.json import Jsonb

from radar import clock, model, policy, registry
from radar.asof import Frames, load_frames
from radar.features import compute_features, watermarks
from radar.registry import FEATURE_NAMES, rules_score

TOLERANCE = 1e-9
MATERIAL = 0.01  # log-odds: a smaller change between runs is not shown
NEGLIGIBLE = 0.005  # log-odds: a smaller contribution is left out of the Why panel and briefs
SCORES = ["rules_score", "p", "logit", "contributions", "lift", "rank_in_book",
          "expected_arr_at_risk_minor", "band"]


class ManifestMismatch(RuntimeError):
    """The active model was trained on feature code that has since changed."""


@functools.cache
def _code_version() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=Path(__file__).parent,
                              capture_output=True, text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def _plain(v):
    """A JSON-ready scalar: NaN/NaT -> None, timestamps -> ISO strings, numpy -> Python."""
    if v is None or (not isinstance(v, list | dict) and pd.isna(v)):
        return None
    if isinstance(v, pd.Timestamp | datetime):
        return v.isoformat()
    if isinstance(v, UUID):
        return str(v)
    return v.item() if isinstance(v, np.generic) else v


def lift_and_band(p, eligible, must_act, coverage_ok, rules_score, cfg: dict) -> tuple:
    """Each account's lift (p over the mean p of the eligible accounts) and band: the one step
    score_frame and the /ops promotion preview share. Arguments are aligned sequences; returns
    (lift as a float array, bands as a list)."""
    p = np.asarray(p, dtype=float)
    eligible = np.asarray(eligible, dtype=bool)
    known = p[eligible & ~np.isnan(p)]  # none without a model: no lift
    lift = p / known.mean() if known.size else np.full(len(p), np.nan)
    band = [policy.band(_plain(pi), _plain(li), bool(m), bool(c), bool(e), r, cfg)
            for pi, li, m, c, e, r in zip(p, lift, must_act, coverage_ok, eligible, rules_score,
                                          strict=True)]
    return lift, band


def score_frame(frames: Frames, tenant: dict, as_of: datetime, artifact: dict | None,
                must_act: set = frozenset()) -> pd.DataFrame:
    """Features, context and scores for every active account at as_of. Ineligible accounts
    get no p; without a model (kitewire) rules_score ranks the books."""
    df = compute_features(frames, tenant, [as_of]).droplevel("as_of")
    df = df[df["active"]].copy()
    eligible = df["eligible"].astype(bool)
    df["rules_score"] = rules_score(df).astype(float)
    df["p"] = df["logit"] = np.nan
    df["contributions"] = None
    if artifact is not None:
        p, c = model.predict(artifact, df[eligible])
        df.loc[eligible, "p"] = p
        df.loc[eligible, "logit"] = np.log(p / (1 - p))
        if not np.allclose(model.intercept(artifact) + c.sum(axis=1), df.loc[eligible, "logit"],
                           rtol=0, atol=TOLERANCE):
            raise RuntimeError("contributions do not sum to logit(p)")
        df["contributions"] = pd.Series(
            [dict(zip(artifact["features"], row.tolist(), strict=True)) for row in c],
            index=df.index[eligible], dtype=object).reindex(df.index)
    df["lift"], df["band"] = lift_and_band(df["p"], eligible, df.index.isin(must_act),
                                           df["coverage_ok"], df["rules_score"],
                                           tenant["config"])
    key = policy.slot_key(None if artifact is None else df["p"], df["rules_score"],
                          df["arr_minor"])
    ranked = eligible & key.notna()  # only these get a rank; the rest stay NaN
    df["rank_in_book"] = policy.rank_in_book(key[ranked], df.loc[ranked, "arr_minor"],
                                             df.loc[ranked, "owner_user_id"])
    df["expected_arr_at_risk_minor"] = (df["p"] * df["arr_minor"]).round()
    return df


def _store(conn, run_id: UUID, as_of: datetime, df: pd.DataFrame) -> None:
    context = [c for c in df.columns if c not in FEATURE_NAMES and c not in SCORES]
    conn.cursor().executemany(
        "INSERT INTO account_scores (run_id, account_id, as_of, eligible, p, logit, lift,"
        " rules_score, band, rank_in_book, owner_user_id, arr_minor,"
        " expected_arr_at_risk_minor, features, contributions, context, coverage)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        [(run_id, a, as_of, bool(r["eligible"]), *(_plain(r[c]) for c in (
            "p", "logit", "lift", "rules_score", "band", "rank_in_book", "owner_user_id",
            "arr_minor", "expected_arr_at_risk_minor")),
          Jsonb({f: _plain(r[f]) for f in FEATURE_NAMES}),
          None if _plain(r["contributions"]) is None else Jsonb(r["contributions"]),
          Jsonb({c: _plain(r[c]) for c in context}),
          Jsonb({"ok": bool(r["coverage_ok"]), "telemetry": bool(r["telemetry_linked"])}))
         for a, r in df.iterrows()])


def _load(conn, tenant_id) -> tuple[dict, dict | None, Frames]:
    tenant = conn.execute("SELECT * FROM tenants WHERE id = %s", (tenant_id,)).fetchone()
    active = conn.execute("SELECT id, artifact, manifest FROM models WHERE tenant_id = %s"
                          " AND status = 'active'", (tenant_id,)).fetchone()
    return tenant, active, load_frames(conn, tenant_id)


def stale_features(manifest: dict) -> list[str]:
    """The features whose code changed since a model's manifest was taken: a model trained on
    them must not score (score_tenant) or be promoted."""
    current = registry.manifest()
    return sorted(f for f, h in manifest.items() if current.get(f) != h)


def score_tenant(db, tenant_id: UUID, as_of: datetime, trigger: str, actionable: bool) -> UUID:
    """Score every active account at as_of with the active model (rules_score if none) and
    store the run. Fails closed on a stale model: one incident, ManifestMismatch, no run."""
    with db.tenant_tx(tenant_id) as conn:
        tenant, active, frames = _load(conn, tenant_id)
        must_act = {r["account_id"] for r in conn.execute(
            "SELECT account_id FROM cases WHERE tenant_id = %s AND kind = ANY(%s)"
            " AND status IN ('open', 'held') AND opened_at <= %s",
            (tenant_id, policy.MUST_ACT, as_of)).fetchall()}
    if active is not None:
        if changed := stale_features(active["manifest"]):
            with db.tenant_tx(tenant_id) as conn:
                conn.execute(
                    "INSERT INTO incidents (kind, details, opened_at)"
                    " VALUES ('manifest_mismatch', %s, %s) ON CONFLICT (tenant_id, kind, source)"
                    " WHERE closed_at IS NULL DO NOTHING",
                    (Jsonb({"model_id": str(active["id"]), "changed": changed}), clock.now()))
            raise ManifestMismatch(f"model {active['id']} is stale for {changed}")
    links = frames.links.loc[frames.links["available_at"] <= as_of, "available_at"]
    with db.tenant_tx(tenant_id) as conn:
        run_id = conn.execute(
            "INSERT INTO score_runs (model_id, trigger, actionable, as_of, watermarks,"
            " crosswalk_watermark, config_version, manifest, code_version, started_at)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (active and active["id"], trigger, actionable, as_of,
             Jsonb({k: _plain(v) for k, v in watermarks(frames, as_of).items()}),
             _plain(links.max()), tenant["config_version"], Jsonb(registry.manifest()),
             _code_version(),
             clock.now())).fetchone()["id"]
    try:
        df = score_frame(frames, tenant, as_of, active and active["artifact"], must_act)
        with db.tenant_tx(tenant_id) as conn:
            _store(conn, run_id, as_of, df)
            conn.execute("UPDATE score_runs SET status = 'done', finished_at = %s,"
                         " n_scored = %s WHERE id = %s", (clock.now(), len(df), run_id))
    except Exception:
        with db.tenant_tx(tenant_id) as conn:
            conn.execute("UPDATE score_runs SET status = 'failed', finished_at = %s"
                         " WHERE id = %s", (clock.now(), run_id))
        raise
    return run_id


def _same(a, b) -> bool:
    return a == b if a is None or b is None else abs(a - b) <= TOLERANCE


def recompute_check(db, tenant_id: UUID, run_id: UUID, account_id: UUID) -> dict:
    """Recompute the account's features and p from raw events as of the run's as_of, with
    the run's model, and compare them with what the run stored."""
    with db.tenant_tx(tenant_id) as conn:
        stored = conn.execute(
            "SELECT s.features, s.p, r.as_of, m.artifact FROM account_scores s"
            " JOIN score_runs r ON r.tenant_id = s.tenant_id AND r.id = s.run_id"
            " LEFT JOIN models m ON m.tenant_id = r.tenant_id AND m.id = r.model_id"
            " WHERE s.tenant_id = %s AND s.run_id = %s AND s.account_id = %s",
            (tenant_id, run_id, account_id)).fetchone()
        tenant, _, frames = _load(conn, tenant_id)
    fresh = score_frame(frames, tenant, stored["as_of"], stored["artifact"]).loc[account_id]
    diffs = {f: {"stored": stored["features"][f], "recomputed": _plain(fresh[f])}
             for f in FEATURE_NAMES if not _same(stored["features"][f], _plain(fresh[f]))}
    p_match = _same(stored["p"], _plain(fresh["p"]))
    if not p_match:
        diffs["p"] = {"stored": stored["p"], "recomputed": _plain(fresh["p"])}
    return {"features_match": not (diffs.keys() - {"p"}), "p_match": p_match, "diffs": diffs}


_ROW = ("SELECT s.run_id, s.features, s.contributions, r.model_id, r.manifest,"
        " r.config_version, r.as_of FROM account_scores s JOIN score_runs r"
        " ON r.tenant_id = s.tenant_id AND r.id = s.run_id WHERE s.account_id = %s AND ")


def diff_runs(conn, account_id: UUID, run_id: UUID, prev_run_id: UUID | None = None) -> dict:
    """What changed for the account since the previous run (default: its latest done run at
    least 6 days older), per feature in log-odds, largest change first. Exact for a logistic
    regression; an ineligible side has no contributions and counts as 0."""
    now = conn.execute(_ROW + "s.run_id = %s", (account_id, run_id)).fetchone()
    if prev_run_id is not None:
        prev = conn.execute(_ROW + "s.run_id = %s", (account_id, prev_run_id)).fetchone()
    else:
        prev = conn.execute(_ROW + "r.status = 'done' AND r.as_of <= %s - interval '6 days'"
                            " ORDER BY r.as_of DESC, r.finished_at DESC LIMIT 1",
                            (account_id, now["as_of"])).fetchone()
    if prev is None:
        return {"prev_run_id": None, "delta_logit_by_feature": {}, "changed_features": [],
                "version_changes": {}}
    after, before = now["contributions"] or {}, prev["contributions"] or {}
    delta = {f: after.get(f, 0.0) - before.get(f, 0.0) for f in FEATURE_NAMES}
    order = sorted(FEATURE_NAMES, key=lambda f: -abs(delta[f]))

    def change(key):
        return None if prev[key] == now[key] else {"before": prev[key], "after": now[key]}
    return {
        "prev_run_id": prev["run_id"],
        "delta_logit_by_feature": {f: delta[f] for f in order},
        "changed_features": [
            {"name": f, "before": prev["features"].get(f), "after": now["features"].get(f),
             "delta_contribution": delta[f]} for f in order
            if delta[f] or prev["features"].get(f) != now["features"].get(f)],
        "version_changes": {
            "model": change("model_id"), "config_version": change("config_version"),
            "manifest": sorted(k for k in now["manifest"].keys() | prev["manifest"].keys()
                               if now["manifest"].get(k) != prev["manifest"].get(k))}}


def material(changed: list[dict], currency: str) -> list[dict]:
    """The changes a person should see (Ruling AQ): at least MATERIAL log-odds and a different
    displayed value, so drift such as tenure_years never headlines. Each gains its label and
    displayed before/after."""
    out = []
    for c in changed:
        before, after = (registry.shown(c["name"], c[k], currency) for k in ("before", "after"))
        if abs(c["delta_contribution"]) >= MATERIAL and before != after:
            out.append(c | {"label": registry.LABELS[c["name"]], "shown_before": before,
                            "shown_after": after})
    return out
