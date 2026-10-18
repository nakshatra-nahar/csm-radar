"""The one policy (design §5.2): only must-act rules and the frozen top-K open cases; the band
is display only. Rules, bands, plays, worklists, suppression and the circuit breaker."""

import statistics
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo

import pandas as pd
from psycopg.types.json import Jsonb

from radar import registry

RULE_PLAYS = {"cancel_announced": "save_call", "dunning_exhausted": "billing_outreach",
              "champion_bounced": "find_new_champion",
              "urgent_ticket_stalled": "support_escalation"}
MUST_ACT = list(RULE_PLAYS)
SOURCE_PLAYS = {"product": "value_review", "zendesk": "support_escalation",
                "gmail": "exec_check_in", "billing": "value_review"}
NO_SIGNAL_PLAY = "exec_check_in"
LIFT_P = 0.10  # a suppression ends when p rises this much (absolute) over p_at_dismiss
CHECK_INS = 2
CHECK_IN_DAYS = 90


@dataclass(frozen=True)
class RuleHit:
    rule: str
    account_id: UUID
    evidence_ids: list[int]
    play: str
    sla_days: int


BANDS = ("red", "yellow", "green", "unknown", "not_in_window")  # every value band() returns


def band(p: float | None, lift: float | None, open_must_act: bool, coverage_ok: bool,
         eligible: bool, rules_score: float | None, cfg: dict) -> str:
    """risk_band v1. A must-act is a known fact, so it beats unknown coverage."""
    if open_must_act:
        return "red"
    if not coverage_ok:
        return "unknown"
    if not eligible:
        return "not_in_window"
    value, cut = ((lift, cfg["band_lift"]) if p is not None
                  else (rules_score, cfg["band_rules"]))
    return "red" if value >= cut["red"] else "yellow" if value >= cut["yellow"] else "green"


def rank_in_book(key: pd.Series, arr: pd.Series, *book: pd.Series) -> pd.Series:
    """1-based rank within each book (rows sharing every `book` value) by key descending, ties
    by arr descending, stable; a missing key ranks last. Served and evaluated ranks use this."""
    frame = pd.DataFrame({"key": key, "arr": arr} | {f"b{i}": b for i, b in enumerate(book)})
    order = frame.sort_values(["key", "arr"], ascending=False, na_position="last", kind="stable")
    return order.groupby(list(frame.columns[2:]), dropna=False).cumcount().add(1).reindex(
        frame.index)


def slot_key(p, rules_score, arr):
    """The ranking key of model slots (rank_in_book): expected ARR at risk, p * arr, with a
    model; rules_score * arr without one."""
    return (rules_score if p is None else p) * arr


def slot_floor(keys: pd.Series, cfg: dict) -> float:
    """Ruling AZ: capacity is a cap, not a quota. The least ranking key an account needs to
    fill a model slot: min_slot_lift x the tenant's mean key over eligible accounts at the
    run. Monotone in the key, so the floor only shortens a book's list, never reorders it."""
    return cfg["min_slot_lift"] * keys.mean()


def choose_play(kind: str, contributions: dict[str, float]) -> str:
    """A must-act kind maps to its play; a model slot to the source family of its largest
    positive behavioural contribution."""
    if kind in RULE_PLAYS:
        return RULE_PLAYS[kind]
    top = registry.top_behavioural(contributions)
    if not top:
        return NO_SIGNAL_PLAY
    source = {d.name: d.source.split("+")[0] for d in registry.FEATURES}
    return SOURCE_PLAYS[source[top[0][0]]]


def rule_hits(facts: pd.DataFrame, cfg: dict) -> list[RuleHit]:
    """One hit per (account, firing rule) in features.rule_facts output."""
    return [RuleHit(rule, account_id, list(facts.at[account_id, f"evidence_{rule}"]),
                    RULE_PLAYS[rule], cfg["sla_business_days"][rule])
            for rule in MUST_ACT for account_id in facts.index[facts[rule]]]


def business_days_after(start: datetime, days: int, tz: str) -> datetime:
    """start plus `days` Monday-to-Friday days in the tenant's timezone, at the same local
    wall time (so a DST change does not move it), in UTC."""
    local = start.astimezone(ZoneInfo(tz))
    while days > 0:
        local += timedelta(days=1)
        days -= local.weekday() < 5
    return local.astimezone(UTC)


def _insert_case(conn, account_id, kind: str, status: str, play: str, opened_by: str,
                 now: datetime, sla_days: int, evidence: list[int], cfg: dict) -> UUID | None:
    """The new case's id, or None if the account already has a live case of this kind."""
    row = conn.execute(
        "INSERT INTO cases (account_id, kind, status, play, opened_at, opened_by,"
        " owner_user_id, sla_due_at, evidence_event_ids)"
        " SELECT %s, %s, %s, %s, %s, %s, owner_user_id, %s, %s FROM accounts WHERE id = %s"
        " ON CONFLICT (tenant_id, account_id, kind) WHERE status IN ('open', 'held')"
        " DO NOTHING RETURNING id",
        (account_id, kind, status, play, now, opened_by,
         business_days_after(now, sla_days, cfg["timezone"]), evidence, account_id)).fetchone()
    return row and row["id"]


def _breaker(conn, tenant_id, now: datetime, cfg: dict) -> tuple[int, float, float]:
    """(must-act cases opened in the last 24 h, trailing 28-day daily median, threshold)."""
    rows = conn.execute(
        "SELECT floor(extract(epoch FROM %(now)s - opened_at) / 86400)::int AS day,"
        " count(*) AS n FROM cases WHERE tenant_id = %(t)s AND kind = ANY(%(kinds)s)"
        " AND opened_by NOT LIKE 'bootstrap:%%' AND opened_at <= %(now)s"
        " AND opened_at > %(now)s - interval '29 days' GROUP BY 1",
        {"now": now, "t": tenant_id, "kinds": MUST_ACT}).fetchall()
    per_day = {r["day"]: r["n"] for r in rows}
    median = statistics.median(per_day.get(d, 0) for d in range(1, 29))
    b = cfg["breaker"]
    return per_day.get(0, 0), median, max(b["min"], b["multiple"] * median)


def _append_new_since_monday(conn, account_id, kind: str, case: UUID, now: datetime,
                             cfg: dict) -> None:
    """Ruling BH: a must-act opened after the owner's list froze this (tenant-local ISO) week
    joins it at the end, marked new since Monday, overflow past K like the freeze. Nothing
    is re-sorted or removed: an account already listed as a model slot or check-in has that
    row upgraded in place (same position) to the must-act; one already listed as a must-act
    keeps its row. With no list yet, Monday's freeze picks the case up."""
    today = now.astimezone(ZoneInfo(cfg["timezone"])).date()
    conn.execute(
        "INSERT INTO worklist_items (worklist_id, account_id, position, kind, reason, p,"
        " expected_arr_at_risk_minor, case_id, added_at, new_since_monday)"
        " SELECT w.id, a.id,"
        "  (SELECT coalesce(max(position), 0) + 1 FROM worklist_items WHERE worklist_id = w.id),"
        "  'must_act', CASE WHEN (SELECT count(*) FROM worklist_items WHERE worklist_id = w.id"
        "  AND kind = 'must_act') >= %(k)s THEN 'overflow:' ELSE '' END || %(kind)s,"
        "  s.p, s.expected_arr_at_risk_minor, %(case)s, %(now)s, true"
        " FROM accounts a JOIN worklists w ON w.user_id = a.owner_user_id"
        "  AND w.week_start = %(week)s"
        " LEFT JOIN account_scores s ON s.run_id = w.run_id AND s.account_id = a.id"
        " WHERE a.id = %(a)s ON CONFLICT (tenant_id, worklist_id, account_id) DO UPDATE"
        " SET kind = 'must_act', reason = excluded.reason, case_id = excluded.case_id,"
        "  new_since_monday = true WHERE worklist_items.kind <> 'must_act'",
        {"k": cfg["capacity_per_csm"], "kind": kind, "case": case, "now": now,
         "week": today - timedelta(days=today.weekday()), "a": account_id})


def open_cases(conn, tenant_id, hits: list[RuleHit], run_or_event: str, now: datetime,
               cfg: dict, breaker: bool = True) -> list[UUID]:
    """Open a case per hit unless one is live (idempotent). Past the circuit breaker's
    threshold new cases open as held, with one circuit_breaker incident. A new must-act
    lifts the account's suppressions, and an open one joins the owner's list if this week's
    is already frozen (Ruling BH). Callers hold the tenant lock. Returns the new case ids.

    breaker=False is the bootstrap backlog (Ruling AB): every case opens, marked
    'bootstrap:' so the breaker's later counts leave it out (it is not a storm)."""
    recent, median, threshold = _breaker(conn, tenant_id, now, cfg)
    opened_by = run_or_event if breaker else "bootstrap:" + run_or_event
    new, held = [], 0
    for h in hits:
        status = "open" if not breaker or recent < threshold else "held"
        case = _insert_case(conn, h.account_id, h.rule, status, h.play, opened_by, now,
                            h.sla_days, h.evidence_ids, cfg)
        if case is None:
            continue
        new.append(case)
        recent += 1
        held += status == "held"
        if status == "open":
            _append_new_since_monday(conn, h.account_id, h.rule, case, now, cfg)
        conn.execute("UPDATE suppressions SET lifted_at = %s, lifted_reason = %s"
                     " WHERE account_id = %s AND lifted_at IS NULL",
                     (now, f"must_act:{h.rule}", h.account_id))
    if held:
        conn.execute(
            "INSERT INTO incidents (kind, details, opened_at) VALUES ('circuit_breaker', %s, %s)"
            " ON CONFLICT (tenant_id, kind, source) WHERE closed_at IS NULL DO NOTHING",
            (Jsonb({"new_24h": recent, "trailing_median": median, "threshold": threshold,
                    "held": held}), now))
    return new


def _auto_close(conn, tenant_id, run_id, week_start, now: datetime, k: int) -> None:
    """Close model_topk cases (no outcome) whose account was outside the top 2K at this
    Monday's freeze and the previous one."""
    prev = conn.execute("SELECT run_id FROM worklists WHERE tenant_id = %s AND week_start = %s"
                        " LIMIT 1", (tenant_id, week_start - timedelta(weeks=1))).fetchone()
    if prev is None:
        return
    conn.execute(
        "UPDATE cases c SET status = 'auto_closed', closed_at = %(now)s"
        " WHERE tenant_id = %(t)s AND kind = 'model_topk' AND status = 'open'"
        " AND outcome IS NULL AND NOT EXISTS (SELECT 1 FROM account_scores s"
        "  WHERE s.tenant_id = c.tenant_id AND s.account_id = c.account_id"
        "  AND s.run_id IN (%(run)s, %(prev)s) AND s.rank_in_book <= %(top)s)",
        {"now": now, "t": tenant_id, "run": run_id, "prev": prev["run_id"], "top": 2 * k})


def _suppressed(conn, book: dict, now: datetime) -> set:
    """Accounts of the book still held out of model slots; lifts those whose p has risen
    by LIFT_P over p_at_dismiss."""
    held = set()
    for s in conn.execute("SELECT id, account_id, p_at_dismiss FROM suppressions"
                          " WHERE account_id = ANY(%s) AND lifted_at IS NULL",
                          (list(book),)).fetchall():
        p = book[s["account_id"]]["p"]
        if p is not None and s["p_at_dismiss"] is not None and p >= s["p_at_dismiss"] + LIFT_P:
            conn.execute("UPDATE suppressions SET lifted_at = %s, lifted_reason = 'p_increase'"
                         " WHERE id = %s", (now, s["id"]))
        else:
            held.add(s["account_id"])
    return held


def _live_case(conn, account_id, kind: str) -> UUID:
    return conn.execute("SELECT id FROM cases WHERE account_id = %s AND kind = %s"
                        " AND status IN ('open', 'held')", (account_id, kind)).fetchone()["id"]


def freeze_worklists(conn, tenant_id, run_id, week_start: date, now: datetime,
                     cfg: dict) -> int:
    """Freeze each CSM's list for the week from the run (worklist v1); returns how many were
    frozen. Idempotent per (user, week_start): a frozen list is never rebuilt."""
    k = cfg["capacity_per_csm"]
    _auto_close(conn, tenant_id, run_id, week_start, now, k)
    scores = conn.execute(
        "SELECT account_id, owner_user_id, eligible, p, rules_score, rank_in_book, as_of,"
        " arr_minor,"
        " expected_arr_at_risk_minor, contributions, (coverage->>'ok')::bool AS coverage_ok,"
        " (context->>'term_end')::timestamptz AS term_end FROM account_scores"
        " WHERE tenant_id = %s AND run_id = %s", (tenant_id, run_id)).fetchall()
    key = {s["account_id"]: slot_key(s["p"], s["rules_score"], s["arr_minor"])
           for s in scores if s["eligible"] and s["rank_in_book"]}
    floor = slot_floor(pd.Series(key, dtype=float), cfg)
    frozen = 0
    for user in conn.execute("SELECT id FROM users WHERE tenant_id = %s AND role = 'csm'"
                             " AND active ORDER BY email", (tenant_id,)).fetchall():
        worklist = conn.execute(
            "INSERT INTO worklists (user_id, week_start, run_id, frozen_at)"
            " VALUES (%s, %s, %s, %s) ON CONFLICT (tenant_id, user_id, week_start) DO NOTHING"
            " RETURNING id", (user["id"], week_start, run_id, now)).fetchone()
        if worklist is None:
            continue
        book = {s["account_id"]: s for s in scores if s["owner_user_id"] == user["id"]}
        must = conn.execute(
            "SELECT id, account_id, kind, status FROM cases WHERE account_id = ANY(%s)"
            " AND kind = ANY(%s) AND status IN ('open', 'held')"
            " ORDER BY sla_due_at, opened_at, array_position(%s, kind)",
            (list(book), MUST_ACT, MUST_ACT)).fetchall()
        # one item per account (its soonest-due open must-act) and every such account is
        # listed; those beyond K are flagged for the team page
        first: dict = {}
        for c in must:
            if c["status"] == "open":
                first.setdefault(c["account_id"], c)
        listed = [(a, "must_act", ("overflow:" if i >= k else "") + c["kind"], c["id"])
                  for i, (a, c) in enumerate(first.items())]
        # a held must-act is not listed, but its account takes no model slot either
        seen = {c["account_id"] for c in must}
        blind = sorted((s for s in book.values() if s["account_id"] not in seen
                        and s["coverage_ok"] is False and s["term_end"] is not None
                        and s["term_end"] <= s["as_of"] + timedelta(days=CHECK_IN_DAYS)),
                       key=lambda s: s["term_end"])[:CHECK_INS]
        listed += [(s["account_id"], "check_in", "renewal_blind_spot", None) for s in blind]
        seen |= {s["account_id"] for s in blind}
        held = _suppressed(conn, book, now)
        ranked = sorted((s for s in book.values() if s["eligible"] and s["rank_in_book"]
                         and s["account_id"] not in seen | held and key[s["account_id"]] >= floor),
                        key=lambda s: s["rank_in_book"])
        for s in ranked[:max(0, k - len(listed))]:
            play = choose_play("model_topk", s["contributions"] or {})
            case = _insert_case(conn, s["account_id"], "model_topk", "open", play,
                                f"run:{run_id}", now, cfg["sla_business_days"]["model_topk"],
                                [], cfg) or _live_case(conn, s["account_id"], "model_topk")
            listed.append((s["account_id"], "model", "model_topk", case))
        for position, (account_id, kind, reason, case) in enumerate(listed, start=1):
            s = book[account_id]
            conn.execute(
                "INSERT INTO worklist_items (worklist_id, account_id, position, kind, reason,"
                " p, expected_arr_at_risk_minor, case_id, added_at)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (worklist["id"], account_id, position, kind, reason, s["p"],
                 s["expected_arr_at_risk_minor"], case, now))
        frozen += 1
    return frozen
