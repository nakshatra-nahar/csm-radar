"""Rolling-origin evaluation and model card (design §7) on a scale-0.2 hearthline world."""

import numpy as np
import pandas as pd
import pytest

from radar import evaluate
from radar.asof import load_frames
from radar.bootstrap import HISTORY_END, replay_feed, seed_tenants_and_users
from radar.db import Database
from radar.registry import rules_score
from radar.sim import generate
from radar.tenants import tenant_id
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
    assert evaluate.rules_score is rules_score


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


def test_acceptance_reports_each_violation():
    good = {"difficulty": {"oracle_auc": 0.85, "max_single_auc": 0.70,
                           "max_single_feature": "usage_drop", "full_model_auc": 0.80},
            "policies": {"model": {"recall_at_k_arr": 0.5},
                         "renewal_sort": {"recall_at_k_arr": 0.3}},
            "signs": []}
    assert evaluate.acceptance(good) == []
    bad = {"difficulty": {"oracle_auc": 0.95, "max_single_auc": 0.75,
                          "max_single_feature": "usage_drop", "full_model_auc": 0.90},
           "policies": {"model": {"recall_at_k_arr": 0.3},
                        "renewal_sort": {"recall_at_k_arr": 0.3}},
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
    assert r["counts"]["origins"] and len(r["counts"]["origins"]) == 4
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
def test_model_card_has_sections_in_order(eval_result):
    card = evaluate.render_model_card("hearthline", eval_result)
    positions = [card.find(f"## {s}") for s in evaluate.SECTIONS]
    assert -1 not in positions and positions == sorted(positions)
    assert "95% CI" in card and "seat" in card  # Ruling W is stated
    p = eval_result["policies"]
    if p["renewal_sort"]["recall_at_k_arr"] < p["random"]["recall_at_k_arr"]:
        assert "already announced" in card  # why the renewal sort loses to random
