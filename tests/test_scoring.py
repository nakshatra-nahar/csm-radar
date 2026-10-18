"""Traceable score runs (design §5, §6) on the trained small world."""

import math
from datetime import UTC, datetime, timedelta

import pytest
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from radar import model, policy, registry, scoring
from radar.bootstrap import backfill_runs, train_and_promote
from radar.db import owner_connect
from radar.registry import FEATURE_NAMES
from radar.tenants import tenant_id

AS_OF = datetime(2026, 10, 12, tzinfo=UTC)  # a Monday before the small world's history end
HEARTHLINE, KITEWIRE = tenant_id("hearthline"), tenant_id("kitewire")
BANDS = set(policy.BANDS)


@pytest.fixture(scope="module")
def owner(trained_world_db):
    with owner_connect(trained_world_db["owner_dsn"], autocommit=True,
                         row_factory=dict_row) as conn:
        yield conn


@pytest.fixture(scope="module")
def hearthline_run(trained_world_db):
    return scoring.score_tenant(trained_world_db["db"], HEARTHLINE, AS_OF, "manual", True)


def rows(owner, run_id):
    return owner.execute("SELECT * FROM account_scores WHERE run_id = %s", (run_id,)).fetchall()


def test_score_run_persists_traceable_rows(trained_world_db, owner, hearthline_run):
    run = owner.execute("SELECT * FROM score_runs WHERE id = %s", (hearthline_run,)).fetchone()
    assert run["model_id"] == trained_world_db["models"]["hearthline"]
    assert (run["status"], run["trigger"], run["actionable"]) == ("done", "manual", True)
    assert run["finished_at"] is not None and run["as_of"] == AS_OF
    assert run["manifest"] == registry.manifest() and run["config_version"] == 1
    assert run["code_version"] and run["crosswalk_watermark"] <= AS_OF
    assert set(run["watermarks"]) >= {"product", "stripe", "zendesk", "gmail", "salesforce"}
    scored = rows(owner, hearthline_run)
    assert run["n_scored"] == len(scored) > 100
    eligible = [r for r in scored if r["eligible"]]
    assert 0 < len(eligible) < len(scored)
    assert all(r["p"] is None and r["contributions"] is None for r in scored
               if not r["eligible"])
    assert math.isclose(sum(r["lift"] for r in eligible) / len(eligible), 1.0)
    for r in eligible:
        assert set(r["contributions"]) == set(FEATURE_NAMES) == set(r["features"])
        assert r["expected_arr_at_risk_minor"] == round(r["p"] * r["arr_minor"])
        assert r["context"]["eligible"] and "owner_user_id" in r["context"]
        assert r["band"] in BANDS and r["coverage"]["ok"] == r["context"]["coverage_ok"]
    by_owner = {}
    for r in sorted(eligible, key=lambda r: r["rank_in_book"]):
        by_owner.setdefault(r["owner_user_id"], []).append(r)
    for book in by_owner.values():  # 1..n by expected ARR at risk within each book
        assert [r["rank_in_book"] for r in book] == list(range(1, len(book) + 1))
        risk = [r["p"] * r["arr_minor"] for r in book]
        assert risk == sorted(risk, reverse=True)


def test_stored_contributions_sum_to_logit(owner, hearthline_run):
    run = owner.execute("SELECT m.artifact FROM score_runs r JOIN models m ON m.id = r.model_id"
                        " WHERE r.id = %s", (hearthline_run,)).fetchone()
    intercept = model.intercept(run["artifact"])
    for r in rows(owner, hearthline_run):
        if r["p"] is not None:
            assert abs(intercept + sum(r["contributions"].values()) - r["logit"]) < 1e-9
            assert abs(r["logit"] - math.log(r["p"] / (1 - r["p"]))) < 1e-9


def test_manifest_mismatch_fails_closed(trained_world_db, owner):
    model_id = trained_world_db["models"]["hearthline"]
    stale = owner.execute("SELECT manifest FROM models WHERE id = %s", (model_id,)).fetchone()[
        "manifest"]
    runs = owner.execute("SELECT count(*) AS n FROM score_runs").fetchone()["n"]
    owner.execute("UPDATE models SET manifest = %s WHERE id = %s",
                  (Jsonb(stale | {"usage_drop": "v1:000000000000"}), model_id))
    try:
        for _ in range(2):  # a second failed sweep does not open a second incident
            with pytest.raises(scoring.ManifestMismatch):
                scoring.score_tenant(trained_world_db["db"], HEARTHLINE, AS_OF, "sweep", True)
        incidents = owner.execute("SELECT details FROM incidents WHERE tenant_id = %s"
                                  " AND kind = 'manifest_mismatch'", (HEARTHLINE,)).fetchall()
        assert [i["details"]["changed"] for i in incidents] == [["usage_drop"]]
        assert owner.execute("SELECT count(*) AS n FROM score_runs").fetchone()["n"] == runs
    finally:
        owner.execute("UPDATE models SET manifest = %s WHERE id = %s", (Jsonb(stale), model_id))
        owner.execute("UPDATE incidents SET closed_at = now() WHERE kind = 'manifest_mismatch'")


def test_rules_only_tenant_scores(trained_world_db, owner):
    assert trained_world_db["models"]["kitewire"] is None  # no model for kitewire
    run_id = scoring.score_tenant(trained_world_db["db"], KITEWIRE, AS_OF, "manual", True)
    run = owner.execute("SELECT * FROM score_runs WHERE id = %s", (run_id,)).fetchone()
    assert run["model_id"] is None and run["status"] == "done"
    scored = rows(owner, run_id)
    assert scored and all(r["p"] is None and r["logit"] is None and r["lift"] is None
                          and r["rules_score"] is not None for r in scored)
    for r in scored:
        if r["eligible"]:
            assert r["rank_in_book"] >= 1
        if r["eligible"] and r["coverage"]["ok"] and r["band"] != "red":
            assert r["band"] == ("yellow" if r["rules_score"] >= 1.5 else "green")
    assert {r["band"] for r in scored} <= BANDS


def test_recompute_check_matches(trained_world_db, owner, hearthline_run):
    db = trained_world_db["db"]
    account = next(r["account_id"] for r in rows(owner, hearthline_run) if r["eligible"])
    check = scoring.recompute_check(db, HEARTHLINE, hearthline_run, account)
    assert check == {"features_match": True, "p_match": True, "diffs": {}}
    run_id = scoring.score_tenant(db, HEARTHLINE, AS_OF, "manual", False)
    owner.execute("UPDATE account_scores SET features = jsonb_set(features, '{log_arr}', '1.0')"
                  " WHERE run_id = %s AND account_id = %s", (run_id, account))
    check = scoring.recompute_check(db, HEARTHLINE, run_id, account)
    assert not check["features_match"] and check["p_match"]
    assert list(check["diffs"]) == ["log_arr"] and check["diffs"]["log_arr"]["stored"] == 1.0


def test_diff_runs_explains_the_logit_change(trained_world_db, owner, hearthline_run):
    db = trained_world_db["db"]
    before = scoring.score_tenant(db, HEARTHLINE, AS_OF - timedelta(weeks=1), "manual", False)
    now = {r["account_id"]: r for r in rows(owner, hearthline_run) if r["eligible"]}
    prev = {r["account_id"]: r for r in rows(owner, before) if r["eligible"]}
    account = max(now.keys() & prev.keys(),
                  key=lambda a: abs(now[a]["logit"] - prev[a]["logit"]))
    with db.tenant_tx(HEARTHLINE) as conn:
        diff = scoring.diff_runs(conn, account, hearthline_run)
    assert diff["prev_run_id"] == before
    delta = diff["delta_logit_by_feature"]
    assert abs(sum(delta.values()) - (now[account]["logit"] - prev[account]["logit"])) < 1e-9
    sizes = [abs(c["delta_contribution"]) for c in diff["changed_features"]]
    assert sizes and sizes == sorted(sizes, reverse=True)
    assert diff["version_changes"] == {"model": None, "manifest": [], "config_version": None}


def test_backfill_writes_one_non_actionable_run_per_monday(trained_world_db, owner):
    end = datetime(2026, 10, 14, tzinfo=UTC)
    run_ids = backfill_runs(trained_world_db["db"], KITEWIRE, weeks=3, end=end)
    runs = owner.execute("SELECT as_of, trigger, actionable, status FROM score_runs"
                         " WHERE id = ANY(%s) ORDER BY as_of", (run_ids,)).fetchall()
    assert [r["as_of"] for r in runs] == [datetime(2026, 9, 28, tzinfo=UTC) + i * timedelta(
        weeks=1) for i in range(3)]
    assert {(r["trigger"], r["actionable"], r["status"]) for r in runs} == {
        ("backfill", False, "done")}


def test_wrong_sign_fit_is_rejected_not_promoted(trained_world_db, owner, monkeypatch):
    fit = model.fit
    flip = next(d for d in registry.FEATURES if d.kind == "behavioural")

    def wrong_sign(train):  # every behavioural sign right except flip's
        artifact = fit(train)
        for d in registry.FEATURES:
            if d.kind == "behavioural":
                sign = -1 if d is flip else 1
                artifact["coef"][artifact["features"].index(d.name)] = sign * d.risk_direction
        return artifact

    monkeypatch.setattr(model, "fit", wrong_sign)
    active = trained_world_db["models"]["hearthline"]
    assert train_and_promote(trained_world_db["db"], HEARTHLINE, AS_OF) is None
    rows = owner.execute("SELECT id, status, metrics FROM models WHERE tenant_id = %s"
                         " ORDER BY version DESC", (HEARTHLINE,)).fetchall()
    assert rows[0]["status"] == "rejected"
    assert rows[0]["metrics"]["wrong_signs"] == [flip.name]
    assert [r["id"] for r in rows if r["status"] == "active"] == [active]


def test_lift_and_band_is_the_one_p_to_band_step():
    """Ruling AR(a): score_frame and the /ops promotion preview share this step."""
    cfg = {"band_lift": {"red": 1.4, "yellow": 1.1}}
    lift, band = scoring.lift_and_band(
        [0.1, 0.3, 0.2, None], eligible=[True, True, True, False],
        must_act=[False, False, True, False], coverage_ok=[True, True, True, True],
        rules_score=[0.0, 0.0, 0.0, 0.0], cfg=cfg)
    assert [round(x, 3) for x in lift[:3]] == [0.5, 1.5, 1.0] and math.isnan(lift[3])
    assert band == ["green", "red", "red", "not_in_window"]  # a must-act is red whatever p says
