"""Rolling-origin evaluation and model card (design §7) on a scale-0.2 hearthline world."""

import json
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from radar import evaluate
from radar.asof import Frames, load_frames
from radar.bootstrap import replay_feed, seed_tenants_and_users
from radar.db import Database
from radar.labels import MATURITY
from radar.registry import FEATURE_NAMES, rules_score
from radar.sim import generate
from radar.sim.params import HISTORY_END
from radar.tenants import runtime_config, tenant_id
from tests.conftest import WORKER_PASSWORD, _role_dsn, fresh_database

POLICIES = ["random", "renewal_sort", "arr_sort", "rules", "model", "oracle", "gbm"]
METRICS = ["recall_at_k_arr", "precision_at_k", "lift_at_k", "roc_auc", "pr_auc", "brier"]


def test_rules_score_matches_the_registered_formula():
    df = pd.DataFrame({
        "integration_broken": [1.0, np.nan, 0.0], "open_urgent_tickets": [0.7, 0.0, 0.0],
        "support_friction_90d": [0.7, 0.0, 0.0], "days_since_customer_email": [60, np.nan, 59],
        "usage_drop": [0.31, np.nan, 0.3], "seat_change_180d": [-0.2, 0.0, -0.1],
        "not_live_after_30d": [1.0, np.nan, 0.0]})
    # every signal fires on row 0; missing values (no telemetry, no Gmail) count as no signal
    assert rules_score(df).tolist() == [9.0, 0.0, 0.0]


def test_topk_by_book_respects_k_and_owner():
    T1, T2 = pd.Timestamp("2026-01-05", tz="UTC"), pd.Timestamp("2026-01-12", tz="UTC")
    df = pd.DataFrame({
        "as_of": [T1] * 5 + [T2] * 3, "owner_user_id": ["a", "a", "a", "b", "b", "a", "a", "a"],
        "score": [0.1, 0.2, 0.1, 0.9, 0.0, 0.2, 0.1, 0.05],
        "arr_minor": [100, 100, 300, 10, 10, 100, 100, 200]})
    top = evaluate.topk_by_book(df, "score", k=2)
    # book (T1, a): values 10, 20, 30 -> rows 2 and 1; book (T1, b) has only 2 accounts;
    # at T2 values 20, 10, 10: the tie for the second slot goes to the larger ARR
    assert top.tolist() == [False, True, True, True, True, True, False, True]
    assert top.index.equals(df.index)


def test_topk_by_book_leaves_slots_empty_below_the_floor():
    """Ruling AZ, as served: a row below the slot floor never fills a top-K slot, even in a
    book with room for it."""
    T = pd.Timestamp("2026-01-05", tz="UTC")
    df = pd.DataFrame({"as_of": [T] * 3, "owner_user_id": ["a"] * 3,
                       "score": [0.3, 0.2, 0.1], "arr_minor": [100] * 3})
    floor = pd.Series([True, False, True])
    assert evaluate.topk_by_book(df, "score", k=10, floor=floor).tolist() == [True, False, True]


def test_slot_floor_is_on_the_ranking_key():
    """Ruling AZ: a slot needs its key (score * arr_minor) at least min_slot_lift x that
    week's mean key, for a probability column and the rules heuristic alike. Row 1 has a
    below-average p (lift 0.375) but the largest ARR, so it keeps its slot."""
    T1, T2 = pd.Timestamp("2026-01-05", tz="UTC"), pd.Timestamp("2026-01-12", tz="UTC")
    t = pd.DataFrame({"as_of": [T1, T1, T1, T2, T2], "model": [0.5, 0.1, 0.2, 0.05, 0.15],
                      "rules": [3.0, 1.0, 0.0, 3.0, 1.5],
                      "arr_minor": [100, 1000, 100, 100, 100]})
    cfg = runtime_config("hearthline")
    # model keys 50, 100, 20 (mean 56.7) | 5, 15; rules keys 300, 1000, 0 (mean 433) | 300, 150
    assert evaluate.slot_floor(t, "model", cfg).tolist() == [False, True, False, False, True]
    assert evaluate.slot_floor(t, "rules", cfg).tolist() == [False, True, False, True, False]


def test_floored_top_k_is_a_prefix_of_the_ranking():
    """The floor is monotone in the ranking key, so on any book it only shortens the top K:
    the floored list is the first n accounts of the unfloored one, never a substitute."""
    rng = np.random.default_rng(7)
    cfg = runtime_config("hearthline")
    weeks = pd.date_range("2026-01-05", periods=3, freq="7D", tz="UTC")
    for _ in range(50):
        n = int(rng.integers(5, 60))
        df = pd.DataFrame({"as_of": rng.choice(weeks, n),
                           "owner_user_id": rng.choice(list("abcd"), n),
                           "model": rng.beta(1, 8, n),
                           "arr_minor": rng.lognormal(10, 1.5, n).round()})
        rank = evaluate._rank(df, df["model"] * df["arr_minor"])
        k = int(rng.integers(1, 12))
        full = evaluate.topk_by_book(df, "model", k)
        floored = evaluate.topk_by_book(df, "model", k, evaluate.slot_floor(df, "model", cfg))
        assert not (floored & ~full).any()
        served = floored.groupby([df["as_of"], df["owner_user_id"]]).transform("sum")
        assert (floored == (rank <= served)).all()  # the first n of each book's ranking


def test_naive_frames_show_todays_salesforce_row_at_every_snapshot():
    """The naive leakage row: each crm.account's history-end version is visible from the first
    event on, so the registry features (compute_features) read it at every T; other events
    keep their own times."""
    old = pd.Timestamp("2025-01-01", tz="UTC")
    at = [old, pd.Timestamp("2026-06-01", tz="UTC"), pd.Timestamp("2025-03-01", tz="UTC")]
    events = pd.DataFrame({
        "type": ["crm.account", "crm.account", "billing.invoice"],
        "entity_id": ["sf_account:A", "sf_account:A", "in_1"], "occurred_at": at,
        "available_at": at, "version_key": ["1", "2", "1"],
        "status": ["Customer", "Former Customer", "paid"]})
    got = evaluate.naive_frames(Frames(events, None, None, None)).events
    crm = got[got["type"] == "crm.account"]
    assert crm["status"].tolist() == ["Former Customer"]
    assert crm["occurred_at"].tolist() == crm["available_at"].tolist() == [old]
    assert got.loc[got["type"] == "billing.invoice", "occurred_at"].tolist() == [at[2]]


def test_current_state_takes_registry_features_from_the_naive_build():
    """No second feature definition: every registry feature comes from the naive
    compute_features rows; only former_customer is new (Type at history end)."""
    T = pd.Timestamp("2026-01-05", tz="UTC")
    at = [T - pd.Timedelta(days=400), pd.Timestamp("2026-06-01", tz="UTC")]
    events = pd.DataFrame({
        "type": "crm.account", "entity_id": "sf_account:A", "occurred_at": at,
        "available_at": at, "status": ["Customer", "Former Customer"]})
    frames = SimpleNamespace(events=events,
                             accounts=pd.DataFrame({"id": ["a1"], "sf_account_id": ["A"]}))
    index = pd.MultiIndex.from_tuples([("a1", T)], names=["account_id", "as_of"])
    rows = pd.DataFrame({name: 0.0 for name in FEATURE_NAMES} | {"label": [1]}, index=index)
    naive = pd.DataFrame({name: 1.0 for name in FEATURE_NAMES}, index=index)
    got = evaluate.current_state(rows, naive, frames)
    assert (got[FEATURE_NAMES] == 1.0).all(axis=None)
    assert got["former_customer"].tolist() == [1.0] and got["label"].tolist() == [1]


def test_vp_ranks_apply_the_slot_floor(monkeypatch):
    """Ruling AZ in the VP narrative: a week where the VP account's key (p * arr) is under
    min_slot_lift x the book's mean key gets no model rank: null plus below_floor, never a
    string in the numeric field."""
    T1, T2 = pd.Timestamp("2026-01-05", tz="UTC"), pd.Timestamp("2026-01-12", tz="UTC")
    feats = pd.DataFrame({
        "account_id": ["v", "a", "b"] * 2, "as_of": [T1] * 3 + [T2] * 3,
        "p": [0.1, 0.3, 0.2, 0.05, 0.3, 0.1], "arr_minor": [10, 10, 10, 1000, 10, 10],
        "owner_user_id": "u", "eligible": True, "decision_visible": False,
        "term_end": pd.Timestamp("2026-06-01", tz="UTC")}).set_index(["account_id", "as_of"])
    monkeypatch.setattr(evaluate.model, "predict", lambda art, X: (X["p"].to_numpy(), None))
    accounts = pd.DataFrame({"vp_case": [True], "sf_account_id": ["V"], "name": ["Vee"],
                             "cause": ["x"], "decision_at": [T2 + pd.Timedelta(weeks=1)]})
    cases = evaluate._vp_cases(feats, {T1: None}, accounts, pd.Series({"v": "V"}), 2,
                               runtime_config("hearthline"))
    weeks = cases[0]["weeks"]
    # T1 keys 1, 3, 2 (mean 2); T2 keys 50, 3, 1: low p but the largest key, rank 1
    assert [w["model"] for w in weeks] == [None, 1]
    assert [w["below_floor"] for w in weeks] == [True, False]
    assert "below floor" in "\n".join(evaluate._vp_lines(cases[0], 2))


def test_headline_metrics_are_weekly_means():
    """Design §7: recall and precision are computed per week, then averaged over weeks. Pooled
    over rows, a heavy week would dominate (recall 0.1, precision 1/3 here)."""
    T1, T2 = pd.Timestamp("2026-01-05", tz="UTC"), pd.Timestamp("2026-01-12", tz="UTC")
    week = pd.Series([T1, T1, T2, T2, T2])
    y = pd.Series([1, 0, 1, 0, 0])
    arr = pd.Series([100.0, 100, 900, 100, 100])
    w = pd.Series([1.0, 0, 0, 1, 1])
    m = evaluate._metrics(y, arr, w, week)
    assert m["recall_at_k_arr"] == pytest.approx(0.5)  # weeks: 1.0 and 0.0
    assert m["precision_at_k"] == pytest.approx(0.5)  # weeks: 1/1 and 0/2
    assert m["lift_at_k"] == pytest.approx(0.5 / 0.4)


def test_acceptance_reports_each_violation():
    good = {"difficulty": {"oracle_auc": 0.85, "max_single_auc": 0.70,
                           "max_single_feature": "usage_drop", "full_model_auc": 0.80},
            "counts": {"origins": [{"recall_model": 0.5, "recall_renewal_sort": 0.3}]},
            "signs": []}
    assert evaluate.acceptance(good) == []
    bad = {"difficulty": {"oracle_auc": 0.95, "max_single_auc": 0.75,
                          "max_single_feature": "usage_drop", "full_model_auc": 0.90},
           "counts": {"origins": [{"recall_model": 0.3, "recall_renewal_sort": 0.3}]},
           "signs": ["usage_drop"]}
    assert len(evaluate.acceptance(bad)) == 5


@pytest.fixture(scope="module")
def eval_result(tmp_path_factory):
    out = tmp_path_factory.mktemp("eval_world")
    generate(out, scale=0.2, tenants=["hearthline"])
    with fresh_database() as dsn:
        db = Database(_role_dsn(dsn, "radar_worker", WORKER_PASSWORD))
        try:
            seed_tenants_and_users(dsn, db, ["hearthline"])
            tid = tenant_id("hearthline")
            replay_feed(db, tid, out, HISTORY_END)
            with db.tenant_tx(tid) as conn:
                frames = load_frames(conn, tid)
                tenant = conn.execute("SELECT * FROM tenants WHERE id = %s", (tid,)).fetchone()
        finally:
            db.close()
    truth = evaluate.load_truth(out, "hearthline")
    # CSMs are not scaled down, so books hold a fifth of their full ~50 eligible accounts and
    # K = 10 would select nearly all of them; scale K with the world instead
    return evaluate.run(frames, tenant, truth, k=2)


@pytest.mark.slow
def test_eval_computes_all_policies_and_metrics(eval_result):
    r = eval_result
    assert list(r["policies"]) == POLICIES
    for name in POLICIES:
        m = r["policies"][name]
        assert list(m) == METRICS
        assert 0 <= m["recall_at_k_arr"] <= 1 and 0 <= m["precision_at_k"] <= 1, name
    for name in ("model", "oracle", "gbm"):
        assert np.isfinite([r["policies"][name][k] for k in METRICS]).all(), name
    for vs in ("renewal_sort", "rules"):
        ci = r["ci"][f"model_minus_{vs}"]
        assert ci["lo"] <= ci["diff"] <= ci["hi"]
    for name in POLICIES:  # the bootstrap resamples the headline (weekly-mean) statistic
        assert r["ci"]["recall"][name]["diff"] == pytest.approx(
            r["policies"][name]["recall_at_k_arr"]), name
    assert set(r["c_sensitivity"]) == {"0.1", "1.0", "10.0"}  # design §6
    assert r["c_sensitivity"]["1.0"] == pytest.approx(r["policies"]["model"]["recall_at_k_arr"])
    assert r["counts"]["origins"] and len(r["counts"]["origins"]) == 4
    assert all(o["origin"] - o["train_to"] >= MATURITY for o in r["counts"]["origins"])
    assert all(o["train_decisions"] > 0 and o["test_rows"] > 0 for o in r["counts"]["origins"])
    assert {"year", "month"} <= set(r["reliability"])  # per Stripe billing interval
    assert all(len(s["bins"]) <= 10 for s in r["reliability"].values())
    assert set(r["lead_time"]) >= {"decisions", "median_weeks", "share_6w"}
    assert set(r["ablation"]) == {"product", "zendesk", "gmail", "billing", "crm"}
    assert len(r["vp_cases"]) == 2
    assert all({"renewal_sort", "model"} <= set(w) for c in r["vp_cases"] for w in c["weeks"])
    assert {"outage_week", "outage_max_new", "median_max_new"} <= set(r["outage_week"])
    assert set(r["coef"]) >= {"usage_drop", "seat_utilization"}
    assert "stripe_metadata (stripe)" in r["identity"]
    assert all(0 <= v["precision"] <= 1 for v in r["identity"].values())
    recall = pd.Series({key: v["recall"] for key, v in r["identity"].items()})
    by_source = recall.groupby(recall.index.str.extract(r"\((\w+)\)")[0].to_numpy()).sum()
    assert (by_source <= 1 + 1e-9).all(), by_source  # each true reference counted once


@pytest.mark.slow
def test_model_beats_random_and_signs_pass(eval_result):
    p = eval_result["policies"]
    assert p["model"]["recall_at_k_arr"] > p["random"]["recall_at_k_arr"]
    assert p["model"]["roc_auc"] > 0.6
    assert eval_result["signs"] == []


@pytest.mark.slow
def test_unforecastable_causes_near_random(eval_result):
    """The cheapest leakage alarm (design §7): churn drawn independently of health (acquired,
    business closed, ...) must not be found much better than by chance. Pooled, because one
    cause has only a handful of decisions in a small world."""
    pooled = eval_result["by_cause"]["all unforecastable"]
    assert pooled["decisions"] >= 3
    assert pooled["recall_at_k_arr"] <= 2.5 * pooled["random_recall"]


@pytest.mark.slow
def test_hindsight_auc_not_lower_than_pit(eval_result):
    h = eval_result["hindsight_vs_pit"]
    assert h["hindsight_auc"] >= h["pit_auc"]


@pytest.mark.slow
def test_naive_current_state_row_shows_the_optimism(eval_result):
    """Reading today's Salesforce row into old snapshots leaks the outcome (a churned account
    is already a Former Customer), so the naive model looks better than the deployed one."""
    h = eval_result["hindsight_vs_pit"]
    assert h["naive_auc"] > h["pit_auc"] + 0.02
    assert h["naive_recall_at_k_arr"] > h["pit_recall_at_k_arr"]
    card = evaluate.render_model_card("hearthline", eval_result)
    assert "naive current-state" in card


@pytest.mark.slow
def test_card_says_so_when_the_model_trails_the_rules(eval_result):
    """A negative model-minus-rules gets one plain Summary sentence; the hindsight row has its
    recall@K cell (Task 13a review minors)."""
    trails = "the deployed model holds less churned ARR than the rules heuristic"
    ci = eval_result["ci"] | {"model_minus_rules": {"diff": -0.04, "lo": -0.11, "hi": 0.03}}
    assert trails in evaluate.render_model_card("hearthline", eval_result | {"ci": ci})
    ci["model_minus_rules"] = {"diff": 0.04, "lo": 0.01, "hi": 0.07}
    card = evaluate.render_model_card("hearthline", eval_result | {"ci": ci})
    assert trails not in card
    h = eval_result["hindsight_vs_pit"]["hindsight_recall_at_k_arr"]
    assert f"| hindsight: late data arrives on time | {evaluate._pct(h)} |" in card


@pytest.mark.slow
def test_model_card_has_sections_in_order(eval_result):
    card = evaluate.render_model_card("hearthline", eval_result)
    positions = [card.find(f"## {s}") for s in evaluate.SECTIONS]
    assert -1 not in positions and positions == sorted(positions)
    assert "95% CI" in card and "seat" in card  # Ruling W is stated
    assert "C = 0.1" in card and "averaged over test weeks" in card
    assert "of this tenant's voluntary churn decisions" in card  # measured, not "~20%"
    assert "Generated by `radar eval --tenant hearthline`" in card  # the real CLI
    p = eval_result["policies"]
    if p["renewal_sort"]["recall_at_k_arr"] < p["random"]["recall_at_k_arr"]:
        assert "already announced" in card  # why the renewal sort loses to random


def test_eval_json_writes_null_for_nan():
    out = evaluate.to_json({"a": float("nan"), "b": [np.float64("nan"), np.int64(3)],
                            "c": float("inf")})
    assert json.loads(out) == {"a": None, "b": [None, 3], "c": None}
    assert "NaN" not in out and "Infinity" not in out


def test_renewal_sort_claim_follows_the_intervals():
    """'worse than random' only where renewal sort's CI lies below random's (Ruling BG)."""
    def ci(lo, hi):
        return {"diff": (lo + hi) / 2, "lo": lo, "hi": hi}
    worse = evaluate._renewal_vs_random({"renewal_sort": ci(0.10, 0.19), "random": ci(0.25, 0.27)})
    level = evaluate._renewal_vs_random({"renewal_sort": ci(0.15, 0.22), "random": ci(0.19, 0.20)})
    assert worse.startswith("- Renewal sort does worse than random because")
    assert level.startswith("- Renewal sort does no better than random because")
    assert evaluate._renewal_vs_random({"renewal_sort": ci(0.30, 0.40),
                                        "random": ci(0.19, 0.20)}) is None


def test_lead_time_counts_only_flags_inside_the_label_window():
    """A flag on a label-0 row (more than 90 days before the decision) is not a lead: every
    lead is at most the 90-day horizon (13 weeks)."""
    T0 = pd.Timestamp("2026-01-05", tz="UTC")
    weeks = [T0 + i * evaluate.WEEK for i in range(40)]
    decided = T0 + 40 * evaluate.WEEK
    t = pd.DataFrame({"account_id": "a", "as_of": weeks, "decision_at": decided,
                      "label": [int(decided - T <= pd.Timedelta(days=90)) for T in weeks],
                      "w_model": [i in (0, 1, 30, 31) for i in range(40)]})
    lead = evaluate._lead_time(t)
    assert lead["flagged"] == 1 and lead["median_weeks"] == 10.0
    assert lead["median_weeks"] <= 90 / 7


def test_training_rows_are_settled_and_labelled_only_from_what_arrived_by_the_origin(
        monkeypatch):
    """The rolling-origin leakage guard (design §7): a training snapshot T needs T +
    MATURITY <= origin, and its label comes from frames cut at the origin, so a decision that
    arrives after the origin trains as a 0."""
    origin = pd.Timestamp("2026-04-20", tz="UTC")
    settled, unsettled = origin - pd.Timedelta(days=203), origin - pd.Timedelta(days=105)
    feats = pd.DataFrame({"eligible": True, "sources_connected": True,
                          "decision_visible": False},
                         index=pd.MultiIndex.from_tuples(
                             [("a", settled), ("b", settled), ("a", unsettled)],
                             names=["account_id", "as_of"]))
    events = pd.DataFrame({"account_id": ["a", "b"],
                           "decision_at": [settled + pd.Timedelta(days=30)] * 2,
                           "available_at": [origin + pd.Timedelta(days=1),  # arrives late
                                            settled + pd.Timedelta(days=31)]})
    frames = Frames(events=events, links=events.iloc[:0], accounts=None, users=None)
    def decided(f):  # churn_decisions' columns from the planted events that arrived
        never = f.events["decision_at"] + pd.NaT
        return f.events.assign(involuntary_at=never, merged=never)[
            ["account_id", "decision_at", "involuntary_at", "merged"]]
    monkeypatch.setattr(evaluate, "churn_decisions", decided)
    train = evaluate._train_rows(feats, frames, origin)
    assert train["label"].to_dict() == {("a", settled): 0, ("b", settled): 1}


def test_recall_gate_is_on_the_latest_origin():
    """Design §6: recall@K beats the renewal-date baseline on the latest origin, not only
    pooled over all four."""
    origins = [{"recall_model": 0.6, "recall_renewal_sort": 0.2}] * 3 + [
        {"recall_model": 0.2, "recall_renewal_sort": 0.3}]
    result = {"difficulty": {"max_single_auc": 0.6, "max_single_feature": "x",
                             "full_model_auc": 0.7},
              "signs": [], "counts": {"origins": origins}}
    assert evaluate.acceptance(result) == [
        "model recall@K does not beat the renewal-date sort on the latest origin"]
    origins[-1] = {"recall_model": 0.4, "recall_renewal_sort": 0.3}
    assert evaluate.acceptance(result) == []
