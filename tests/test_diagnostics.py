"""Feature signal report (Task 5b): per-feature AUC, coefficients and signs, on synthetic rows
and on a generated world."""

import numpy as np
import pandas as pd
import pytest

from radar import diagnostics, evaluate, model
from radar.asof import load_frames
from radar.bootstrap import replay_feed, seed_tenants_and_users
from radar.db import Database
from radar.labels import build_training_set, training_grid
from radar.registry import FEATURE_NAMES, FEATURES
from radar.sim import generate
from radar.sim.params import HISTORY_END
from radar.tenants import tenant_id
from tests.conftest import WORKER_PASSWORD, _role_dsn, fresh_database

SIGN_TENANTS = ["hearthline", "tallyworks"]


def training_set(frames, tenant) -> pd.DataFrame:
    """The served model's training population (bootstrap.train_and_promote) at history end."""
    return build_training_set(frames, tenant, training_grid(tenant["slug"], HISTORY_END),
                              HISTORY_END)


@pytest.fixture(scope="module")
def train():
    rng = np.random.default_rng(3)
    n = 4000
    X = pd.DataFrame(rng.normal(size=(n, len(FEATURE_NAMES))), columns=FEATURE_NAMES)
    X["integration_broken"] = 0.0  # never fires
    X["seat_utilization"] = X["seat_utilization"].mask(rng.random(n) < 0.25)
    logit = -2 - 1.5 * X["seat_utilization"].fillna(0) + X["open_urgent_tickets"]
    X.index = pd.MultiIndex.from_arrays(
        [np.arange(n) // 10, pd.date_range("2024-01-01", periods=10, freq="W-MON").tolist()
         * (n // 10)], names=["account_id", "as_of"])
    return X.assign(label=(rng.random(n) < 1 / (1 + np.exp(-logit.to_numpy()))).astype(int))


def test_feature_table_measures_each_feature(train):
    r = diagnostics.feature_table(train)
    assert list(r.columns) == ["prevalence_nonzero", "missing_share", "single_auc", "coef",
                               "sign_ok"]
    assert list(r.index[: len(FEATURE_NAMES)]) == FEATURE_NAMES
    assert r.loc["integration_broken", "prevalence_nonzero"] == 0
    assert r.loc["seat_utilization", "missing_share"] == pytest.approx(0.25, abs=0.02)
    # risk direction applied: a strong negative-direction driver scores well above 0.5
    assert r.loc["seat_utilization", "single_auc"] > 0.7
    assert r.loc["open_urgent_tickets", "single_auc"] > 0.65
    assert r.loc["usage_drop", "single_auc"] == pytest.approx(0.5, abs=0.05)
    assert r.loc["full_model", "single_auc"] > r.loc["seat_utilization", "single_auc"]
    bad = {d.name for d in FEATURES if r.loc[d.name, "sign_ok"] is False}
    assert bad == set(model.check_signs(model.fit(train)))
    structural = [d.name for d in FEATURES if d.risk_direction is None]
    assert r.loc[structural, "sign_ok"].isna().all()


def test_oracle_auc_reads_latent_health_before_T():
    T = pd.Timestamp("2025-03-03", tz="UTC")  # a Monday
    weeks = pd.date_range("2024-12-02", "2025-03-03", freq="W-MON")
    rng = np.random.default_rng(1)
    rows, train = [], []
    for k in range(400):
        h = rng.normal(0, 1)
        churn = rng.random() < 1 / (1 + np.exp(2 + 2 * h))
        rows += [{"sf_account_id": f"sf{k}", "week_start": w.date(), "h": h} for w in weeks]
        rows[-1]["h"] = 9.0 * churn  # the week starting at T is after T: reading it leaks
        train.append({"account_id": k, "as_of": T, "label": int(churn),
                      "billing_month_to_month": 1.0, "term_end_within_60d": 1.0})
    weekly = pd.DataFrame(rows)
    accounts = pd.DataFrame({"id": range(400), "sf_account_id": [f"sf{k}" for k in range(400)]})
    train = pd.DataFrame(train).set_index(["account_id", "as_of"])
    auc = diagnostics.oracle_auc(train, accounts, weekly)
    assert 0.7 < auc < 0.95


@pytest.fixture(scope="module")
def signs_dir(tmp_path_factory):
    out = tmp_path_factory.mktemp("signs_world")
    generate(out, scale=0.3, tenants=SIGN_TENANTS)
    return out


@pytest.fixture(scope="module")
def signs_world(signs_dir):
    """A scale-0.3 world for the two model tenants: {slug: (frames, tenant row)}."""
    out = signs_dir
    loaded = {}
    with fresh_database() as dsn:
        db = Database(_role_dsn(dsn, "radar_worker", WORKER_PASSWORD))
        try:
            seed_tenants_and_users(dsn, db, SIGN_TENANTS)
            for slug in SIGN_TENANTS:
                tid = tenant_id(slug)
                replay_feed(db, tid, out, HISTORY_END)
                with db.tenant_tx(tid) as conn:
                    tenant = conn.execute("SELECT * FROM tenants WHERE id = %s", (tid,)).fetchone()
                    loaded[slug] = (load_frames(conn, tid), tenant)
        finally:
            db.close()
    return loaded


@pytest.mark.slow
@pytest.mark.parametrize("slug", SIGN_TENANTS)
def test_signs_hold_on_small_world(signs_world, slug):
    """Regression guard for Task 5b's calibration: every behavioural coefficient keeps the
    sign of its risk_direction (the design §6 promotion gate)."""
    report = diagnostics.feature_table(training_set(*signs_world[slug]))
    behavioural = [d.name for d in FEATURES if d.kind == "behavioural"]
    assert report.loc[behavioural, "sign_ok"].tolist() == [True] * len(behavioural), (
        report[["single_auc", "coef", "sign_ok"]].to_string())


@pytest.mark.slow
def test_oracle_auc_in_band(signs_world, signs_dir):
    """Design §3.6 on the oracle the model card reports (diagnostics.oracle_auc): it sees the
    latent health, so it must be good but not perfect."""
    frames, tenant = signs_world["hearthline"]
    weekly = evaluate.load_truth(signs_dir, "hearthline")["weekly"]
    auc = diagnostics.oracle_auc(training_set(frames, tenant), frames.accounts, weekly)
    assert 0.80 <= auc <= 0.92
