"""Every page read (design §9). Each function takes a connection from the user's tenant_tx, so
RLS has already limited it to the session's tenant; callers apply rbac.authorize per account."""

from datetime import date, datetime, time, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from radar import clock, model, policy, registry, scoring
from radar.agent import brief
from radar.agent.evidence import EXACT_METHODS, describe

KINDS = {d.name: d.kind for d in registry.FEATURES}
TIMELINE_ITEMS = 40
DRIVER_FLOOR = 0.1  # log-odds: a smaller behavioural term does not explain a model slot


def tenant(conn, tenant_id: UUID) -> dict:
    return conn.execute("SELECT * FROM tenants WHERE id = %s", (tenant_id,)).fetchone()


def active_model(conn) -> dict | None:
    return conn.execute("SELECT id, version FROM models WHERE status = 'active'").fetchone()


def week_start(tz: str) -> date:
    """Monday of the current ISO week in the tenant's timezone."""
    today = clock.now().astimezone(ZoneInfo(tz)).date()
    return today - timedelta(days=today.weekday())


def freshness(conn) -> list[dict]:
    """Each source's latest delivery, stale while its source_stale incident is open."""
    return conn.execute(
        "SELECT s.source, s.last_deliver_at, i.id IS NOT NULL AS stale FROM source_status s"
        " LEFT JOIN incidents i ON i.kind = 'source_stale' AND i.source = s.source"
        " AND i.closed_at IS NULL ORDER BY s.source").fetchall()


def worklist(conn, user_id: UUID, week: date) -> dict | None:
    return conn.execute("SELECT * FROM worklists WHERE user_id = %s AND week_start = %s",
                        (user_id, week)).fetchone()


def any_worklist(conn, week: date) -> bool:
    """Whether the sweep froze any list in the tenant this week."""
    return conn.execute("SELECT 1 FROM worklists WHERE week_start = %s LIMIT 1",
                        (week,)).fetchone() is not None


def worklist_rows(conn, wl: dict, currency: str) -> list[dict]:
    """The frozen items in list order, scored by the run the list was frozen from."""
    rows = conn.execute(
        "SELECT i.*, a.name, a.owner_user_id, s.arr_minor, s.band, s.lift, s.features,"
        " s.contributions,"
        " (s.context->>'term_end')::date AS term_end, c.play, c.status AS case_status,"
        " EXISTS (SELECT 1 FROM outbox o WHERE o.case_id = i.case_id"
        "  AND o.kind = 'customer_email' AND o.status = 'draft') AS draft_ready,"
        # shortcut: counts the weeks this user listed it, not consecutive weeks; count streaks
        # if lists start skipping weeks
        " (SELECT count(*) FROM worklist_items j JOIN worklists v ON v.id = j.worklist_id"
        "  WHERE v.user_id = w.user_id AND v.week_start <= w.week_start"
        "  AND j.account_id = i.account_id) AS weeks_listed,"
        " (SELECT t.outcome || ' on ' || to_char(t.closed_at, 'YYYY-MM-DD') FROM cases t"
        "  WHERE t.account_id = i.account_id AND t.outcome IS NOT NULL"
        "  ORDER BY t.closed_at DESC LIMIT 1) AS last_touch"
        " FROM worklist_items i JOIN worklists w ON w.id = i.worklist_id"
        " JOIN accounts a ON a.id = i.account_id"
        " LEFT JOIN account_scores s ON s.run_id = w.run_id AND s.account_id = i.account_id"
        " LEFT JOIN cases c ON c.id = i.case_id WHERE i.worklist_id = %s ORDER BY i.position",
        (wl["id"],)).fetchall()
    for r in rows:
        r["why_now"] = why_now(conn, r, wl["run_id"], currency)
    return rows


def why_now(conn, row: dict, run_id: UUID, currency: str) -> str | None:
    """The largest material change since the last run (Ruling AQ), else driver()."""
    changed = scoring.diff_runs(conn, row["account_id"], run_id)["changed_features"]
    if shown := scoring.material(changed, currency):
        c = shown[0]
        return (f"{c['label']} {'up' if c['delta_contribution'] > 0 else 'down'}:"
                f" {c['shown_before']} → {c['shown_after']}")
    return driver(row, currency)


def driver(row: dict, currency: str) -> str | None:
    """The largest behavioural contribution with its value if it reaches DRIVER_FLOOR, else the
    slot's expected ARR at risk (its sort key); structural terms never headline (Ruling BF)."""
    up = [(c, f) for f, c in (row["contributions"] or {}).items()
          if KINDS.get(f) == "behavioural" and c >= DRIVER_FLOOR]
    if up:
        c, f = max(up)
        return (f"{registry.LABELS[f]}: {registry.shown(f, row['features'].get(f), currency)}"
                f" ({c:+.2f})")
    at_risk = row.get("expected_arr_at_risk_minor")
    return None if at_risk is None else (
        f"large account: expected ARR at risk {currency} {at_risk / 100:,.0f}")


def book(conn, band: str | None) -> list[dict]:
    """Every live account with its latest score (None before the first sweep), riskiest ARR
    first; behavioural chips are the features pushing its risk up."""
    rows = conn.execute(
        "SELECT a.id, a.name, a.owner_user_id, s.band, s.p, s.lift, s.arr_minor, s.coverage,"
        " s.contributions FROM accounts a LEFT JOIN latest_scores s ON s.account_id = a.id"
        " WHERE a.merged_into IS NULL AND NOT a.is_test"
        " AND (%(band)s::text IS NULL OR s.band = %(band)s)"
        " ORDER BY s.expected_arr_at_risk_minor DESC NULLS LAST, a.name",
        {"band": band}).fetchall()
    for r in rows:
        up = [(c, f) for f, c in (r["contributions"] or {}).items()
              if KINDS.get(f) == "behavioural" and c > 0]
        r["chips"] = [f for _, f in sorted(up, reverse=True)[:3]]
    return rows


def account(conn, account_id: UUID) -> dict | None:
    return conn.execute("SELECT a.*, u.name AS owner_name FROM accounts a LEFT JOIN users u"
                        " ON u.id = a.owner_user_id WHERE a.id = %s", (account_id,)).fetchone()


def score(conn, account_id: UUID) -> dict | None:
    """The account's latest actionable score with its run's trace fields."""
    return conn.execute(
        "SELECT s.*, r.trigger, r.watermarks, r.crosswalk_watermark, r.manifest,"
        " r.config_version, r.code_version, m.version AS model_version FROM latest_scores s"
        " JOIN score_runs r ON r.tenant_id = s.tenant_id AND r.id = s.run_id"
        " LEFT JOIN models m ON m.tenant_id = r.tenant_id AND m.id = r.model_id"
        " WHERE s.account_id = %s", (account_id,)).fetchone()


def why(contributions: dict | None) -> list[dict]:
    """Contributions (log-odds) grouped by source, largest effect first; the structural terms
    form one 'structure' group the page shows collapsed. A group's total counts every term but
    lists only those of at least NEGLIGIBLE (the trace table keeps them all)."""
    groups: dict[str, list] = {}
    for d in registry.FEATURES:
        if contributions and d.name in contributions:
            key = "structure" if d.kind == "structural" else d.source.split("+")[0]
            groups.setdefault(key, []).append((registry.LABELS[d.name], contributions[d.name]))
    out = [{"source": k, "total": sum(c for _, c in terms),
            "terms": [(n, c) for n, c in terms if abs(c) >= scoring.NEGLIGIBLE]}
           for k, terms in groups.items()]
    return sorted(out, key=lambda g: -abs(g["total"]))


TIMELINE = """
WITH refs AS (SELECT DISTINCT ON (account_ref) account_ref, account_id, method
              FROM identity_links WHERE available_at <= %(now)s
              ORDER BY account_ref, available_at DESC, id DESC)
SELECT e.*, r.method FROM events e JOIN refs r ON r.account_ref = e.account_ref
WHERE r.account_id = %(a)s AND e.available_at <= %(now)s AND e.occurred_at <= %(now)s
ORDER BY e.occurred_at DESC, e.id DESC LIMIT %(n)s
"""


def timeline(conn, account_id: UUID, as_of, currency: str) -> list[dict]:
    """The account's latest events through the identity links usable now. 'late' marks
    records that arrived after the run's as_of (the run could not see them); 'exact' whether
    an exact key, not a name or domain heuristic, matched the record."""
    rows = conn.execute(TIMELINE, {"now": clock.now(), "a": account_id,
                                   "n": TIMELINE_ITEMS}).fetchall()
    return [{"at": e["occurred_at"], "source": e["source"], "method": e["method"],
             "text": describe(e, currency, cap=None, dated=False)[0],
             "exact": e["method"] in EXACT_METHODS,
             "late": as_of is not None and e["available_at"] > as_of} for e in rows]


def cases(conn, account_id: UUID, user_id: UUID) -> list[dict]:
    """Open and held cases with their latest ready brief and customer draft (Ruling AJ: never
    a digest); the draft's preview is filled as approving it now would send it."""
    rows = conn.execute("SELECT * FROM cases WHERE account_id = %s AND status IN"
                        " ('open', 'held') ORDER BY opened_at", (account_id,)).fetchall()
    for c in rows:
        c["brief"] = conn.execute(
            "SELECT generator, output FROM briefs WHERE case_id = %s AND status = 'ready'"
            " ORDER BY updated_at DESC LIMIT 1", (c["id"],)).fetchone()
        c["draft"] = conn.execute(
            "SELECT o.id, o.subject, o.body, t.email FROM outbox o LEFT JOIN contacts t"
            " ON t.tenant_id = o.tenant_id AND t.id = o.to_contact_id WHERE o.case_id = %s"
            " AND o.kind = 'customer_email' AND o.status = 'draft'"
            " ORDER BY o.created_at DESC LIMIT 1", (c["id"],)).fetchone()
        if c["draft"]:
            c["draft"]["preview"] = brief.filled(conn, c["draft"]["id"], user_id)
    return rows


def case_of(conn, account_id: UUID, case_id: UUID) -> dict | None:
    return conn.execute("SELECT * FROM cases WHERE id = %s AND account_id = %s",
                        (case_id, account_id)).fetchone()


def draft_of(conn, account_id: UUID, outbox_id: UUID) -> dict | None:
    """A customer draft of one of the account's cases."""
    return conn.execute(
        "SELECT o.* FROM outbox o JOIN cases c ON c.tenant_id = o.tenant_id AND c.id = o.case_id"
        " WHERE o.id = %s AND c.account_id = %s AND o.kind = 'customer_email'",
        (outbox_id, account_id)).fetchone()


# --- team page (design §9: the VP's dashboard) ---------------------------------------------

def latest_run(conn) -> dict | None:
    """The run behind latest_scores, and the newest done run at least 6 days older (the
    'last week' diff_runs compares with)."""
    run = conn.execute("SELECT run_id AS id, as_of FROM latest_scores LIMIT 1").fetchone()
    if run:
        run["prev"] = conn.execute(
            "SELECT id FROM score_runs WHERE status = 'done' AND as_of <= %s - interval '6 days'"
            " ORDER BY as_of DESC, finished_at DESC LIMIT 1", (run["as_of"],)).fetchone()
    return run


def run_scores(conn, run_id: UUID) -> list[dict]:
    """Each live account's score in the run, under its current owner."""
    return conn.execute(
        "SELECT a.id, a.name, a.owner_user_id, u.active AS owner_active, s.band, s.arr_minor,"
        " s.expected_arr_at_risk_minor AS at_risk, (s.context->>'term_end')::date AS term_end"
        " FROM account_scores s JOIN accounts a ON a.tenant_id = s.tenant_id"
        " AND a.id = s.account_id LEFT JOIN users u ON u.tenant_id = a.tenant_id"
        " AND u.id = a.owner_user_id WHERE s.run_id = %s AND a.merged_into IS NULL"
        " AND NOT a.is_test ORDER BY s.arr_minor DESC NULLS LAST, a.name", (run_id,)).fetchall()


def csm_activity(conn, week: date, tz: str) -> list[dict]:
    """One row per active CSM: open must-acts, SLA breaches, must-acts beyond capacity on this
    week's list (overflow), dismissals since Monday (tenant time), and whether they opened
    this week's list."""
    return conn.execute(
        "SELECT u.id, u.name, u.manager_id,"
        " (SELECT count(*) FROM cases c WHERE c.owner_user_id = u.id AND c.status = 'open'"
        "  AND c.kind = ANY(%(must)s)) AS must_acts,"
        " (SELECT count(*) FROM cases c WHERE c.owner_user_id = u.id AND c.status = 'open'"
        "  AND c.sla_due_at < %(now)s) AS sla_breaches,"
        " (SELECT count(*) FROM worklist_items i WHERE i.worklist_id = w.id"
        "  AND i.reason LIKE 'overflow:%%') AS overflow,"
        " (SELECT count(*) FROM cases c WHERE c.owner_user_id = u.id"
        "  AND c.status = 'dismissed' AND c.closed_at >= %(monday)s) AS dismissals,"
        " w.id IS NOT NULL AS listed, w.viewed_at"
        " FROM users u LEFT JOIN worklists w ON w.tenant_id = u.tenant_id AND w.user_id = u.id"
        " AND w.week_start = %(week)s WHERE u.role = 'csm' AND u.active ORDER BY u.name",
        {"must": policy.MUST_ACT, "now": clock.now(), "week": week,
         "monday": datetime.combine(week, time(), ZoneInfo(tz))}).fetchall()


def former_reports(conn, manager_id: UUID) -> set:
    """Deactivated users who reported to the manager: their accounts still need an owner."""
    return {r["id"] for r in conn.execute("SELECT id FROM users WHERE manager_id = %s"
                                          " AND NOT active", (manager_id,)).fetchall()}


# --- ops page (admin) -----------------------------------------------------------------------

TOKENS = ("input_tokens", "output_tokens", "cache_creation_input_tokens",
          "cache_read_input_tokens")
OPS = {
    "sources": "SELECT * FROM source_status ORDER BY source",
    "quarantine": "SELECT reason, count(*) AS n FROM quarantine WHERE resolved_at IS NULL"
                  " GROUP BY reason ORDER BY n DESC, reason",
    "links": "SELECT method, count(*) AS n, count(*)::float8 / sum(count(*)) OVER () AS share"
             " FROM identity_links GROUP BY method ORDER BY n DESC, method",
    "jobs": "SELECT status, count(*) AS n FROM jobs GROUP BY status ORDER BY status",
    "agent": "SELECT at, action, entity_type, entity_id, after->>'status' AS status"
             " FROM audit_log WHERE actor = 'agent' ORDER BY at DESC, id DESC LIMIT 20",
    "llm": "SELECT count(*) AS calls, coalesce(sum(cost_usd), 0) AS cost_usd, coalesce(sum("
           + " + ".join(f"coalesce((usage->>'{t}')::bigint, 0)" for t in TOKENS)
           + "), 0) AS tokens FROM llm_calls WHERE created_at >= %(since)s",
    "incidents": "SELECT * FROM incidents WHERE closed_at IS NULL OR opened_at >= %(since)s"
                 " - interval '7 days' ORDER BY closed_at IS NULL DESC, opened_at DESC",
    "models": "SELECT id, version, status, metrics, trained_at, promoted_at, promoted_by,"
              " artifact FROM models ORDER BY version DESC",
    "digests": "SELECT kind, status, subject, created_at FROM outbox WHERE kind <> 'customer_email'"
               " ORDER BY created_at DESC LIMIT 10",
}


def ops(conn, tz: str) -> dict:
    """Everything the ops page shows; 'today' starts at the tenant's local midnight."""
    since = datetime.combine(clock.now().astimezone(ZoneInfo(tz)).date(), time(), ZoneInfo(tz))
    out = {k: conn.execute(q, {"since": since}).fetchall() for k, q in OPS.items()}
    out["llm"] = out["llm"][0]
    return out


def _bands(rows: list[dict], artifact: dict, cfg: dict) -> dict:
    """Each account's band had `artifact` scored the run's stored features, through the same
    scoring.lift_and_band step as a score run."""
    eligible = [bool(r["eligible"]) for r in rows]
    p = np.full(len(rows), np.nan)
    if any(eligible):
        frame = pd.DataFrame([r["features"] for r, e in zip(rows, eligible, strict=True) if e],
                             dtype=float)
        p[eligible] = model.predict(artifact, frame)[0]
    _, bands = scoring.lift_and_band(p, eligible, [r["must_act"] for r in rows],
                                     [r["coverage_ok"] for r in rows],
                                     [r["rules_score"] for r in rows], cfg)
    return {r["account_id"]: b for r, b in zip(rows, bands, strict=True)}


def band_changes(conn, candidate: dict, active: dict | None, cfg: dict) -> int:
    """How many accounts of the latest run would change band if the candidate replaced the
    active model (the stored bands when there is none), on the same stored inputs and today's
    open must-acts, so only the model differs."""
    rows = conn.execute(
        "SELECT s.account_id, s.eligible, s.features, s.rules_score, s.band,"
        " coalesce((s.coverage->>'ok')::bool, false) AS coverage_ok, EXISTS (SELECT 1"
        "  FROM cases c WHERE c.account_id = s.account_id AND c.kind = ANY(%s)"
        "  AND c.status IN ('open', 'held')) AS must_act FROM latest_scores s",
        (policy.MUST_ACT,)).fetchall()
    before = (_bands(rows, active, cfg) if active
              else {r["account_id"]: r["band"] for r in rows})
    after = _bands(rows, candidate, cfg)
    return sum(after[a] != before[a] for a in after)
