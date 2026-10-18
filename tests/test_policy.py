"""The canonical policy (design §5.2): bands, rules, worklists, suppression and the breaker."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pandas as pd
import psycopg
import pytest
from psycopg.types.json import Jsonb

from radar import policy, scoring
from radar.asof import attribute, load_frames
from radar.features import rule_facts, watermarks
from radar.sim.params import PARAMS
from radar.tenants import runtime_config, tenant_id

CFG = runtime_config("hearthline")


def band(p=0.1, lift=1.0, must_act=False, coverage=True, eligible=True, rules=None):
    return policy.band(p, lift, must_act, coverage, eligible, rules, CFG)


def test_band_semantics_red_requires_lift3_or_must_act():
    assert band(lift=3.0) == "red"
    assert band(lift=2.99) == "yellow"
    assert band(lift=1.5) == "yellow"
    assert band(lift=1.49) == "green"
    assert band(lift=0.2, must_act=True) == "red"
    assert band(p=None, lift=None, eligible=False, must_act=True) == "red"
    assert band(p=None, lift=None, eligible=False) == "not_in_window"
    # no model (kitewire): rules_score thresholds instead of lift
    assert band(p=None, lift=None, rules=3.0) == "red"
    assert band(p=None, lift=None, rules=1.5) == "yellow"
    assert band(p=None, lift=None, rules=1.0) == "green"


def test_rules_band_thresholds_come_from_tenant_config():
    assert runtime_config("kitewire")["band_rules"] == {"red": 3.0, "yellow": 1.5}
    cfg = CFG | {"band_rules": {"red": 2.0, "yellow": 1.0}}
    assert policy.band(None, None, False, True, True, 2.0, cfg) == "red"
    assert policy.band(None, None, False, True, True, 1.0, cfg) == "yellow"


def test_rank_in_book_per_book_ties_by_arr_missing_last():
    key = pd.Series([5.0, 5.0, None, 1.0, 9.0])
    arr = pd.Series([10, 20, 99, 1, 1])
    owner = pd.Series(["a", "a", "a", "a", "b"])
    assert policy.rank_in_book(key, arr, owner).tolist() == [2, 1, 4, 3, 1]
    week = pd.Series([1, 1, 2, 2, 2])
    assert policy.rank_in_book(key, arr, week, owner).tolist() == [2, 1, 2, 1, 1]


def test_unknown_never_green():
    for lift in (0.0, 1.0, 1.6, 5.0):
        assert band(lift=lift, coverage=False) == "unknown"
    assert band(p=None, lift=None, rules=0.0, coverage=False) == "unknown"
    assert band(p=None, lift=None, eligible=False, coverage=False) == "unknown"


def test_business_days_skip_weekends_in_tenant_tz():
    ny = "America/New_York"
    # Friday 22:00 in New York is already Saturday 02:00 UTC: the local Friday counts
    friday = datetime(2026, 1, 10, 3, tzinfo=UTC)  # Fri 9 Jan 22:00 local
    assert policy.business_days_after(friday, 1, ny) == datetime(2026, 1, 13, 3, tzinfo=UTC)
    assert policy.business_days_after(friday, 5, ny) == datetime(2026, 1, 17, 3, tzinfo=UTC)
    saturday = datetime(2026, 1, 10, 15, tzinfo=UTC)  # Sat 10:00 local
    assert policy.business_days_after(saturday, 1, ny) == datetime(2026, 1, 12, 15, tzinfo=UTC)
    # local wall time is kept across the DST change (8 Mar 2026): 10:00 EST -> 10:00 EDT
    before_dst = datetime(2026, 3, 6, 15, tzinfo=UTC)  # Fri 10:00 EST
    assert policy.business_days_after(before_dst, 1, ny) == datetime(2026, 3, 9, 14, tzinfo=UTC)


def test_rule_hits_carry_play_sla_and_evidence():
    a, b = uuid4(), uuid4()
    facts = pd.DataFrame({
        "cancel_announced": [True, False], "evidence_cancel_announced": [[7, 9], []],
        "dunning_exhausted": [False, False], "evidence_dunning_exhausted": [[], []],
        "champion_bounced": [False, True], "evidence_champion_bounced": [[], [3]],
        "urgent_ticket_stalled": [True, False], "evidence_urgent_ticket_stalled": [[4], []]},
        index=pd.Index([a, b], name="account_id"))
    hits = sorted(policy.rule_hits(facts, CFG), key=lambda h: (str(h.account_id), h.rule))
    assert {(h.account_id, h.rule, tuple(h.evidence_ids), h.play, h.sla_days)
            for h in hits} == {
        (a, "cancel_announced", (7, 9), "save_call", 1),
        (a, "urgent_ticket_stalled", (4,), "support_escalation", 2),
        (b, "champion_bounced", (3,), "find_new_champion", 5)}


def test_model_play_follows_top_positive_behavioural_source():
    assert policy.choose_play("dunning_exhausted", {}) == "billing_outreach"
    assert policy.choose_play("model_topk", {"open_urgent_tickets": 0.4, "usage_drop": 0.9,
                                             "log_arr": 3.0}) == "value_review"
    assert policy.choose_play("model_topk", {"days_since_customer_email": 0.5,
                                             "support_friction_90d": 0.2}) == "exec_check_in"
    assert policy.choose_play("model_topk", {"support_friction_90d": 0.2,
                                             "usage_drop": -1.0}) == "support_escalation"
    assert policy.choose_play("model_topk", {"usage_drop": -0.3, "log_arr": 2.0}) == (
        "exec_check_in")  # no positive behavioural contribution


# --- the ledger: cases, worklists, suppressions (a fresh tenant per test) -------------------

NOW = datetime(2026, 3, 2, 11, tzinfo=UTC)  # a Monday, 06:00 in New York
WEEK = timedelta(weeks=1)


@pytest.fixture
def book(owner_conn, tenant):
    """(tenant_id, the first CSM's id, make_accounts(n) -> [account ids] owned by that CSM)."""
    tid, _ = tenant
    csm = owner_conn.execute("SELECT id FROM users WHERE tenant_id = %s AND role = 'csm'"
                             " ORDER BY email LIMIT 1", (tid,)).fetchone()["id"]

    def make_accounts(n, owner=csm):
        return [owner_conn.execute(
            "INSERT INTO accounts (tenant_id, sf_account_id, name, owner_user_id)"
            " VALUES (%s, %s, 'acct', %s) RETURNING id",
            (tid, uuid4().hex[:18], owner)).fetchone()["id"] for _ in range(n)]
    return tid, csm, make_accounts


def make_run(owner_conn, tid, csm, as_of, rows):
    """A done run with one account_scores row per dict: account, rank (None: ineligible),
    p (None: no model), lift (default 1.0), rules_score (default 0), arr (default 100000),
    owner (default csm), coverage (default True), term_end (default as_of + 300 days)."""
    run = owner_conn.execute(
        "INSERT INTO score_runs (tenant_id, trigger, actionable, as_of, config_version,"
        " status, started_at) VALUES (%s, 'sweep', true, %s, 1, 'done', %s) RETURNING id",
        (tid, as_of, as_of)).fetchone()["id"]
    for r in rows:
        p = r.get("p", 0.1)
        term_end = r.get("term_end", as_of + timedelta(days=300))
        owner_conn.execute(
            "INSERT INTO account_scores (tenant_id, run_id, account_id, as_of, eligible, p,"
            " lift, rules_score, rank_in_book, owner_user_id, arr_minor,"
            " expected_arr_at_risk_minor, contributions, context, coverage) VALUES (%s, %s,"
            " %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (tid, run, r["account"], as_of, r.get("rank") is not None, p,
             None if p is None else r.get("lift", 1.0), r.get("rules_score", 0.0),
             r.get("rank"), r.get("owner", csm), r.get("arr", 100000),
             None if p is None else round(p * r.get("arr", 100000)),
             Jsonb({"usage_drop": 0.5}),
             Jsonb({"term_end": term_end.isoformat()}), Jsonb({"ok": r.get("coverage", True)})))
    return run


def items(owner_conn, tid, csm, week_start):
    return owner_conn.execute(
        "SELECT i.* FROM worklist_items i JOIN worklists w ON w.tenant_id = i.tenant_id"
        " AND w.id = i.worklist_id WHERE w.tenant_id = %s AND w.user_id = %s"
        " AND w.week_start = %s ORDER BY i.position", (tid, csm, week_start)).fetchall()


def hit(account_id, rule="urgent_ticket_stalled"):
    return policy.RuleHit(rule, account_id, [1], policy.RULE_PLAYS[rule], 2)


def open_cases(worker_db, tid, hits, now, cfg=CFG):
    with worker_db.tenant_tx(tid) as conn:
        return policy.open_cases(conn, tid, hits, "test", now, cfg)


def freeze(worker_db, tid, run, now, cfg=CFG):
    with worker_db.tenant_tx(tid) as conn:
        return policy.freeze_worklists(conn, tid, run, now.date(), now, cfg)


def test_worklist_must_acts_first_then_checkins_then_model_slots_capped(
        owner_conn, worker_db, book):
    tid, csm, make_accounts = book
    acts, blind, model_accts = make_accounts(2), make_accounts(4), make_accounts(4)
    open_cases(worker_db, tid, [hit(acts[0], "cancel_announced"), hit(acts[1])], NOW)
    soon = NOW + timedelta(days=30)
    rows = ([{"account": a} for a in acts]
            + [{"account": a, "coverage": False, "term_end": soon + i * timedelta(days=1)}
               for i, a in enumerate(blind[:3])]
            + [{"account": blind[3], "coverage": False, "term_end": NOW + timedelta(days=91)}]
            + [{"account": a, "rank": i + 1, "p": 0.5 - i / 10}
               for i, a in enumerate(model_accts)])
    run = make_run(owner_conn, tid, csm, NOW, rows)
    cfg = CFG | {"capacity_per_csm": 5}
    csms = owner_conn.execute("SELECT count(*) AS n FROM users WHERE tenant_id = %s"
                              " AND role = 'csm'", (tid,)).fetchone()["n"]
    assert freeze(worker_db, tid, run, NOW, cfg) == csms  # one worklist per CSM
    got = items(owner_conn, tid, csm, NOW.date())
    assert [i["kind"] for i in got] == ["must_act"] * 2 + ["check_in"] * 2 + ["model"]
    assert [i["position"] for i in got] == [1, 2, 3, 4, 5]
    assert {i["account_id"] for i in got[:2]} == set(acts)
    assert [i["account_id"] for i in got[2:4]] == blind[:2]  # soonest term end first
    assert got[4]["account_id"] == model_accts[0] and got[4]["p"] == 0.5
    case = owner_conn.execute("SELECT * FROM cases WHERE tenant_id = %s AND id = %s",
                              (tid, got[4]["case_id"])).fetchone()
    assert (case["kind"], case["status"], case["play"]) == ("model_topk", "open",
                                                            "value_review")
    assert case["opened_at"] == NOW
    assert case["sla_due_at"] == policy.business_days_after(NOW, 5, CFG["timezone"])
    assert freeze(worker_db, tid, run, NOW, cfg) == 0  # idempotent per (user, week)
    assert len(items(owner_conn, tid, csm, NOW.date())) == 5


def test_overflow_flagged_when_must_acts_exceed_k(owner_conn, worker_db, book):
    tid, csm, make_accounts = book
    acts = make_accounts(3)
    open_cases(worker_db, tid, [hit(a) for a in acts], NOW)
    run = make_run(owner_conn, tid, csm, NOW, [{"account": a, "rank": i + 1}
                                               for i, a in enumerate(acts)])
    freeze(worker_db, tid, run, NOW, CFG | {"capacity_per_csm": 2})
    got = items(owner_conn, tid, csm, NOW.date())
    assert [i["kind"] for i in got] == ["must_act"] * 3
    assert ["overflow" in i["reason"] for i in got] == [False, False, True]


def test_account_with_two_must_acts_is_listed_once(owner_conn, worker_db, book):
    """worklist_items holds one row per account: the soonest-due must-act stands for it."""
    tid, csm, make_accounts = book
    (account,) = make_accounts(1)
    open_cases(worker_db, tid, [hit(account, "cancel_announced"), hit(account)], NOW)
    run = make_run(owner_conn, tid, csm, NOW, [{"account": account, "rank": 1}])
    freeze(worker_db, tid, run, NOW)
    got = items(owner_conn, tid, csm, NOW.date())
    assert [(i["kind"], i["reason"]) for i in got] == [("must_act", "cancel_announced")]


def test_held_must_act_account_takes_no_model_slot(owner_conn, worker_db, book):
    """A held case is still a must-act fact on the account: a model slot would list it
    twice once the admin releases the held case."""
    tid, csm, make_accounts = book
    held, other = make_accounts(2)
    open_cases(worker_db, tid, [hit(held)], NOW, CFG | {"breaker": {"min": 0, "multiple": 0}})
    run = make_run(owner_conn, tid, csm, NOW, [{"account": held, "rank": 1},
                                               {"account": other, "rank": 2}])
    freeze(worker_db, tid, run, NOW)
    assert [(i["kind"], i["account_id"]) for i in items(owner_conn, tid, csm, NOW.date())] == [
        ("model", other)]


TUESDAY = NOW + timedelta(days=1)


def test_mid_week_must_act_appends_new_since_monday(owner_conn, worker_db, book):
    """Ruling BH: a must-act opened mid-week joins the owner's frozen list at the end, marked
    new since Monday; the Monday rows are untouched; past K it counts as overflow."""
    tid, csm, make_accounts = book
    monday_act, model_acct, tuesday, wednesday = make_accounts(4)
    open_cases(worker_db, tid, [hit(monday_act)], NOW)
    run = make_run(owner_conn, tid, csm, NOW, [{"account": monday_act},
                                               {"account": model_acct, "rank": 1, "p": 0.5}])
    freeze(worker_db, tid, run, NOW, CFG | {"capacity_per_csm": 2})
    before = items(owner_conn, tid, csm, NOW.date())
    assert [i["kind"] for i in before] == ["must_act", "model"]
    cfg = CFG | {"capacity_per_csm": 2}
    (case,) = open_cases(worker_db, tid, [hit(tuesday, "cancel_announced")], TUESDAY, cfg)
    open_cases(worker_db, tid, [hit(wednesday, "dunning_exhausted")], TUESDAY, cfg)
    after = items(owner_conn, tid, csm, NOW.date())
    assert after[:2] == before
    assert [(i["account_id"], i["position"], i["kind"], i["reason"], i["new_since_monday"])
            for i in after[2:]] == [
        (tuesday, 3, "must_act", "cancel_announced", True),
        (wednesday, 4, "must_act", "overflow:dunning_exhausted", True)]
    assert after[2]["case_id"] == case and after[2]["added_at"] == TUESDAY


def test_second_mid_week_case_or_listed_must_act_appends_nothing(owner_conn, worker_db, book):
    tid, csm, make_accounts = book
    listed, fresh = make_accounts(2)
    open_cases(worker_db, tid, [hit(listed)], NOW)
    freeze(worker_db, tid, make_run(owner_conn, tid, csm, NOW, [{"account": listed}]), NOW)
    open_cases(worker_db, tid, [hit(fresh)], TUESDAY)
    before = items(owner_conn, tid, csm, NOW.date())
    assert [i["account_id"] for i in before] == [listed, fresh]
    # a second rule on the appended account, and a new rule on the Monday must-act
    new = open_cases(worker_db, tid, [hit(fresh, "cancel_announced"),
                                      hit(listed, "cancel_announced")], TUESDAY)
    assert len(new) == 2 and items(owner_conn, tid, csm, NOW.date()) == before


def test_mid_week_must_act_upgrades_its_model_slot_in_place(owner_conn, worker_db, book):
    """An account already on the list as a model slot that gets a must-act mid-week keeps
    its position, but its row becomes the must-act, new since Monday: the urgency shows."""
    tid, csm, make_accounts = book
    first, second = make_accounts(2)
    run = make_run(owner_conn, tid, csm, NOW, [{"account": first, "rank": 1, "p": 0.6},
                                               {"account": second, "rank": 2, "p": 0.1}])
    freeze(worker_db, tid, run, NOW)
    before = items(owner_conn, tid, csm, NOW.date())
    assert [(i["account_id"], i["kind"]) for i in before] == [(first, "model")]
    (case,) = open_cases(worker_db, tid, [hit(first, "cancel_announced")], TUESDAY)
    after = items(owner_conn, tid, csm, NOW.date())
    assert len(after) == 1 and {k: after[0][k] for k in ("account_id", "position", "kind", "reason", "case_id",
                                     "new_since_monday", "p")} == {
        "account_id": first, "position": 1, "kind": "must_act", "reason": "cancel_announced",
        "case_id": case, "new_since_monday": True, "p": 0.6}


def test_must_act_before_mondays_freeze_waits_for_the_freeze(owner_conn, worker_db, book):
    """No list frozen for the current week: nothing is appended (not even to last week's
    list); Monday's freeze lists the case as an ordinary must-act."""
    tid, csm, make_accounts = book
    old, account = make_accounts(2)
    last = NOW - WEEK
    freeze(worker_db, tid, make_run(owner_conn, tid, csm, last, [{"account": old}]), last)
    open_cases(worker_db, tid, [hit(account)], NOW)
    assert items(owner_conn, tid, csm, last.date()) == []
    freeze(worker_db, tid, make_run(owner_conn, tid, csm, NOW, [{"account": account}]), NOW)
    assert [(i["account_id"], i["new_since_monday"])
            for i in items(owner_conn, tid, csm, NOW.date())] == [(account, False)]


def test_a_low_risk_book_gets_fewer_model_slots(owner_conn, worker_db, book):
    """Ruling AZ: capacity is a cap. A model slot needs a ranking key (p * arr) at least
    min_slot_lift x the tenant's mean key over eligible accounts, so a book whose keys sit
    below that gets fewer than K; another CSM's high-risk book raises the mean."""
    tid, csm, make_accounts = book
    other_csm = owner_conn.execute("SELECT id FROM users WHERE tenant_id = %s AND role = 'csm'"
                                   " AND id <> %s ORDER BY email LIMIT 1",
                                   (tid, csm)).fetchone()["id"]
    mine, (theirs,) = make_accounts(3), make_accounts(1, owner=other_csm)
    # every p in my book is above the mean p (lift 1.2), but the keys are 60000, 9000, 9000
    # against their 100000: tenant mean key 44500
    run = make_run(owner_conn, tid, csm, NOW, [
        {"account": a, "rank": i + 1, "p": 0.3, "lift": 1.2, "arr": arr}
        for i, (a, arr) in enumerate(zip(mine, [200000, 30000, 30000], strict=True))]
        + [{"account": theirs, "rank": 1, "p": 0.1, "lift": 0.4, "arr": 1000000,
            "owner": other_csm}])
    assert CFG["min_slot_lift"] == 1.0
    freeze(worker_db, tid, run, NOW)  # K = 10
    assert [i["account_id"] for i in items(owner_conn, tid, csm, NOW.date())] == mine[:1]
    assert [i["account_id"] for i in items(owner_conn, tid, other_csm, NOW.date())] == [theirs]


def test_large_account_with_below_average_p_keeps_its_slot(owner_conn, worker_db, book):
    """The floor is on expected ARR at risk, not on lift: a big account with p under the
    tenant mean p but a key above the mean key keeps its slot."""
    tid, csm, make_accounts = book
    big, small, tiny = make_accounts(3)
    # p 0.05, 0.4, 0.1 (mean 0.183: big has lift 0.27); keys 50000, 40000, 10000 (mean 33333)
    run = make_run(owner_conn, tid, csm, NOW, [
        {"account": big, "rank": 1, "p": 0.05, "lift": 0.27, "arr": 1000000},
        {"account": small, "rank": 2, "p": 0.4, "lift": 2.18},
        {"account": tiny, "rank": 3, "p": 0.1, "lift": 0.55}])
    freeze(worker_db, tid, run, NOW)
    assert [i["account_id"] for i in items(owner_conn, tid, csm, NOW.date())] == [big, small]


def test_without_a_model_the_floor_is_on_rules_score_times_arr(owner_conn, worker_db, book):
    """Without a model the key is rules_score * arr, and the same floor applies to it."""
    tid, csm, make_accounts = book
    accts = make_accounts(3)
    # keys 300000, 150000, 100000: mean 183333
    run = make_run(owner_conn, tid, csm, NOW, [
        {"account": a, "rank": i + 1, "p": None, "rules_score": r}
        for i, (a, r) in enumerate(zip(accts, [3.0, 1.5, 1.0], strict=True))])
    freeze(worker_db, tid, run, NOW)
    assert [i["account_id"] for i in items(owner_conn, tid, csm, NOW.date())] == accts[:1]


def suppress(owner_conn, tid, account, p):
    owner_conn.execute("INSERT INTO suppressions (tenant_id, account_id, reason, p_at_dismiss,"
                       " created_at) VALUES (%s, %s, 'already handled', %s, %s)",
                       (tid, account, p, NOW - WEEK))


def test_suppressed_account_returns_only_on_material_increase(owner_conn, worker_db, book):
    tid, csm, make_accounts = book
    top, other, acted, filler = make_accounts(4)
    suppress(owner_conn, tid, top, 0.25)
    suppress(owner_conn, tid, acted, 0.5)
    cfg = CFG | {"capacity_per_csm": 1}
    slots = []
    for week, p in enumerate([0.30, 0.3499, 0.35]):  # +0.10 is material, +0.0999 is not
        now = NOW + week * WEEK
        # filler keeps other's key above the tenant mean key (the slot floor)
        run = make_run(owner_conn, tid, csm, now, [{"account": top, "rank": 1, "p": p},
                                                   {"account": other, "rank": 2, "p": 0.2},
                                                   {"account": filler, "rank": 3, "p": 0.0}])
        freeze(worker_db, tid, run, now, cfg)
        slots.append(items(owner_conn, tid, csm, now.date())[0]["account_id"])
    assert slots == [other, other, top]
    lifted = owner_conn.execute("SELECT account_id, lifted_at, lifted_reason FROM suppressions"
                                " WHERE tenant_id = %s", (tid,)).fetchall()
    by_account = {r["account_id"]: r for r in lifted}
    assert by_account[top]["lifted_at"] == NOW + 2 * WEEK
    assert by_account[top]["lifted_reason"] == "p_increase"
    assert by_account[acted]["lifted_at"] is None
    open_cases(worker_db, tid, [hit(acted, "dunning_exhausted")], NOW + 2 * WEEK)
    row = owner_conn.execute("SELECT lifted_at, lifted_reason FROM suppressions"
                             " WHERE tenant_id = %s AND account_id = %s", (tid, acted)).fetchone()
    assert row == {"lifted_at": NOW + 2 * WEEK, "lifted_reason": "must_act:dunning_exhausted"}


def test_model_case_auto_closes_after_two_mondays_outside_2k(owner_conn, worker_db, book):
    tid, csm, make_accounts = book
    fallen, a, b, c = make_accounts(4)
    cfg = CFG | {"capacity_per_csm": 1}  # 2K = 2
    ranks = [[fallen, a, b, c], [a, b, fallen, c], [a, b, fallen, c]]
    states = []
    for week, order in enumerate(ranks):
        now = NOW + week * WEEK
        run = make_run(owner_conn, tid, csm, now, [{"account": x, "rank": r + 1}
                                                   for r, x in enumerate(order)])
        freeze(worker_db, tid, run, now, cfg)
        states.append(owner_conn.execute(
            "SELECT status, closed_at FROM cases WHERE tenant_id = %s AND account_id = %s"
            " AND kind = 'model_topk'", (tid, fallen)).fetchone())
    assert [s["status"] for s in states] == ["open", "open", "auto_closed"]
    assert states[2]["closed_at"] == NOW + 2 * WEEK


def test_breaker_holds_cases_and_opens_one_incident(owner_conn, worker_db, book):
    tid, _, make_accounts = book
    cfg = CFG | {"breaker": {"min": 1, "multiple": 3.0}}
    history = make_accounts(28)
    for day, account in enumerate(history):  # one new case a day: median 1, threshold 3
        open_cases(worker_db, tid, [hit(account)], NOW - (28 - day) * timedelta(days=1), cfg)
    storm = make_accounts(5)
    new = open_cases(worker_db, tid, [hit(a) for a in storm], NOW, cfg)
    assert len(new) == 5
    assert open_cases(worker_db, tid, [hit(a) for a in storm], NOW, cfg) == []  # idempotent
    later = make_accounts(2)
    open_cases(worker_db, tid, [hit(a) for a in later], NOW + timedelta(hours=1), cfg)
    status = owner_conn.execute(
        "SELECT account_id, status FROM cases WHERE tenant_id = %s", (tid,)).fetchall()
    status = {r["account_id"]: r["status"] for r in status}
    assert {status[a] for a in history} == {"open"}
    assert [status[a] for a in storm] == ["open"] * 3 + ["held"] * 2
    assert {status[a] for a in later} == {"held"}
    incidents = owner_conn.execute("SELECT kind, opened_at, details FROM incidents"
                                   " WHERE tenant_id = %s", (tid,)).fetchall()
    assert [(i["kind"], i["opened_at"]) for i in incidents] == [("circuit_breaker", NOW)]
    assert incidents[0]["details"]["threshold"] == 3


# --- the small world --------------------------------------------------------------------------

def test_outage_week_replay_caps_new_cases_per_csm(small_world_db):
    """Replay the daily rule check around tallyworks' outage. The outage's urgent tickets
    mostly close within hours, so it storms nothing by itself; as the worst case, every
    outage ticket is also taken to stall 3 days later. The breaker holds the excess and
    raises one incident; no CSM gets more than K new open cases that week."""
    db, tid = small_world_db["db"], tenant_id("tallyworks")
    outage = datetime.combine(PARAMS["outage"]["tallyworks"], datetime.min.time(), UTC)
    with db.tenant_tx(tid) as conn:
        frames = load_frames(conn, tid)
        tenant = conn.execute("SELECT * FROM tenants WHERE id = %s", (tid,)).fetchone()
    cfg = tenant["config"]
    tickets = frames.events[frames.events["type"] == "support.ticket"]
    tagged = tickets[tickets["tags"].map(lambda t: f"outage-{outage.date()}" in (t or []))]
    stalled_at = outage + timedelta(days=3, hours=11)
    storm = {a: policy.RuleHit("urgent_ticket_stalled", a, [int(i)], "support_escalation", 2)
             for a, i in zip(attribute(tagged, frames.links, stalled_at), tagged["id"],
                             strict=True) if isinstance(a, UUID)}
    days = [outage + timedelta(days=d, hours=11) for d in range(-28, 14)]
    hits = {T: policy.rule_hits(rule_facts(frames, tenant, T), cfg) for T in days}
    hits[stalled_at] += list(storm.values())
    with db.tenant_tx(tid) as conn:  # rolled back below: the shared world stays clean
        for T in days:
            policy.open_cases(conn, tid, hits[T], "replay", T, cfg)
        new = conn.execute(
            "SELECT owner_user_id, count(*) FILTER (WHERE status = 'open') AS open,"
            " count(*) FILTER (WHERE status = 'held') AS held FROM cases"
            " WHERE opened_at >= %s AND opened_at < %s GROUP BY 1",
            (outage, outage + WEEK)).fetchall()
        incidents = conn.execute("SELECT details FROM incidents"
                                 " WHERE kind = 'circuit_breaker'").fetchall()
        assert len(storm) > 2 * cfg["breaker"]["min"]  # a real storm ...
        assert sum(r["held"] for r in new) >= len(storm) - cfg["breaker"]["min"]  # ... held
        assert max(r["open"] for r in new) <= cfg["capacity_per_csm"]
        assert len(incidents) == 1
        raise psycopg.Rollback()


def test_late_product_batch_moves_no_ranks_via_usage(trained_world_db):
    """Half of the latest monthly usage batch arriving (the rest late) moves nothing: usage
    reads only months under the completeness watermark, so ranks match a missing batch."""
    tid = tenant_id("hearthline")
    T = datetime(2026, 10, 12, tzinfo=UTC)
    with trained_world_db["db"].tenant_tx(tid) as conn:
        frames = load_frames(conn, tid)
        tenant = conn.execute("SELECT * FROM tenants WHERE id = %s", (tid,)).fetchone()
        artifact = conn.execute("SELECT artifact FROM models WHERE status = 'active'"
                                ).fetchone()["artifact"]
    mark = watermarks(frames, T)["product"]
    ev = frames.events
    batch = (ev["type"] == "product.usage") & (ev["occurred_at"] == mark) & (
        ev["available_at"] <= T)
    refs = sorted(ev.loc[batch, "account_ref"].unique())

    def delayed(late_refs):
        late = batch & ev["account_ref"].isin(late_refs)
        return replace(frames, events=ev.assign(
            available_at=ev["available_at"].mask(late, T + timedelta(days=5))))

    half, none = delayed(refs[::2]), delayed(refs)
    assert watermarks(half, T)["product"] == watermarks(none, T)["product"] < mark
    full, partial, missing = (scoring.score_frame(f, tenant, T, artifact)
                              for f in (frames, half, none))
    usage = ["usage_drop", "seat_utilization"]
    assert not full[usage].equals(missing[usage])  # the complete batch does move usage
    pd.testing.assert_frame_equal(partial[usage], missing[usage])
    pd.testing.assert_series_equal(partial["rank_in_book"], missing["rank_in_book"])
