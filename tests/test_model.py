"""The JSON logistic regression (design §6): numpy serving equals sklearn, exactly additive."""

import inspect
import json

import numpy as np
import pandas as pd
import pytest
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from radar import model
from radar.registry import FEATURE_NAMES, FEATURES, manifest


@pytest.fixture(scope="module")
def train():
    rng = np.random.default_rng(7)
    X = pd.DataFrame(rng.normal(size=(600, len(FEATURE_NAMES))), columns=FEATURE_NAMES)
    X["usage_drop"] = X["usage_drop"].mask(rng.random(600) < 0.3)  # no telemetry
    logit = -2 + 1.5 * X["usage_drop"].fillna(0) + X["open_urgent_tickets"]
    X.index = pd.MultiIndex.from_arrays(  # 60 accounts x 10 weekly snapshots
        [np.arange(600) // 10, pd.date_range("2024-01-01", periods=10, freq="W-MON").tolist() * 60],
        names=["account_id", "as_of"])
    return X.assign(label=(rng.random(600) < 1 / (1 + np.exp(-logit.to_numpy()))).astype(int))


def test_json_predict_matches_sklearn(train):
    artifact = model.fit(train)
    sk = make_pipeline(SimpleImputer(), StandardScaler(),
                       LogisticRegression(C=1.0, max_iter=2000))
    sk.fit(train[FEATURE_NAMES], train["label"])
    p, _ = model.predict(artifact, train)
    assert np.abs(p - sk.predict_proba(train[FEATURE_NAMES])[:, 1]).max() < 1e-9


def test_contributions_sum_to_logit(train):
    artifact = model.fit(train)
    p, contrib = model.predict(artifact, train)
    assert contrib.shape == (len(train), len(FEATURE_NAMES))
    assert np.abs(artifact["intercept"] + contrib.sum(axis=1) - np.log(p / (1 - p))).max() < 1e-9
    missing = train["usage_drop"].isna().to_numpy()
    assert (contrib[missing, FEATURE_NAMES.index("usage_drop")] == 0).all()  # imputed: 0


def test_artifact_is_json_serialisable_and_has_no_pickle(train):
    artifact = model.fit(train)
    loaded = json.loads(json.dumps(artifact))
    assert loaded["features"] == FEATURE_NAMES and loaded["platt"] is None
    assert loaded["manifest"] == {f: manifest()[f] for f in FEATURE_NAMES}
    assert loaded["label_version"] == "churn_label_90d:v1"
    assert loaded["n_train"] == len(train)
    # one decision per account: positive accounts, not positive weekly rows (design §6 gate)
    positive = train.index.get_level_values("account_id")[train["label"] == 1]
    assert loaded["n_decisions"] == positive.nunique() < train["label"].sum()
    assert {"impute", "mean", "scale", "coef", "intercept", "trained_at"} <= loaded.keys()
    np.testing.assert_array_equal(model.predict(loaded, train)[0],
                                  model.predict(artifact, train)[0])
    source = inspect.getsource(model)
    assert "pickle" not in source and "joblib" not in source


def test_check_signs_flags_wrong_direction(train):
    artifact = model.fit(train)
    right = {d.name: 0.3 * (d.risk_direction or 1) for d in FEATURES}
    assert model.check_signs(artifact | {"coef": list(right.values())}) == []
    wrong = right | {"usage_drop": -0.5, "log_arr": -3.0}  # log_arr is structural: no gate
    assert model.check_signs(artifact | {"coef": list(wrong.values())}) == ["usage_drop"]


def test_platt_none_leaves_predictions_unchanged(train):
    artifact = model.fit(train)
    p, contrib = model.predict(artifact, train)
    assert model.intercept(artifact) == artifact["intercept"]
    raw = artifact["intercept"] + contrib.sum(axis=1)
    np.testing.assert_array_equal(p, 1 / (1 + np.exp(-raw)))


def test_platt_keeps_additivity_and_ranking(train):
    plain = model.fit(train)
    calibrated = plain | {"platt": {"a": -0.4, "b": 0.7}}
    p0, _ = model.predict(plain, train)
    p, contrib = model.predict(calibrated, train)
    assert model.intercept(calibrated) == pytest.approx(-0.4 + 0.7 * plain["intercept"])
    logit = np.log(p / (1 - p))
    assert np.abs(model.intercept(calibrated) + contrib.sum(axis=1) - logit).max() < 1e-9
    assert not np.allclose(p, p0)  # calibration moves the probabilities, not their order
    np.testing.assert_array_equal(np.argsort(p, kind="stable"), np.argsort(p0, kind="stable"))


def test_platt_fitted_only_for_a_miscalibrated_segment_with_enough_rows(train):
    fitted = model.fit(train)
    shifted = fitted | {"intercept": fitted["intercept"] + 1.5}  # overstates every p
    rows = train.assign(billing_interval="year")  # last 8 weeks: 480 rows in one segment
    platt, metrics = model.platt(shifted, rows)
    assert platt is not None and metrics["ece"]["year"]["ece"] > 0.02
    assert metrics["ece_platt"]["year"]["ece"] < metrics["ece"]["year"]["ece"]
    split = train.assign(billing_interval=np.where(train.index.get_level_values(
        "account_id") % 2 == 0, "year", "month"))  # 240 rows each: too few to calibrate
    assert model.platt(shifted, split)[0] is None


def test_platt_is_rejected_when_it_worsens_the_worst_segment():
    """One pooled sigmoid cannot fix segments miscalibrated in opposite directions: Platt is
    kept only if the worst qualifying segment's ECE falls."""
    rng = np.random.default_rng(3)
    n = 6000
    x = rng.normal(size=n)
    year = np.arange(n) % 3 == 0  # p understates year (2,000 rows), overstates month
    true_logit = np.where(year, -1.0, -3.0) + x
    rows = pd.DataFrame({"usage_drop": x, "billing_interval": np.where(year, "year", "month"),
                         "label": (rng.random(n) < 1 / (1 + np.exp(-true_logit))).astype(int)},
                        index=pd.MultiIndex.from_arrays(
                            [np.arange(n), [pd.Timestamp("2026-01-05", tz="UTC")] * n],
                            names=["account_id", "as_of"]))
    artifact = {"features": ["usage_drop"], "impute": [0.0], "mean": [0.0], "scale": [1.0],
                "coef": [1.0], "intercept": -2.0, "platt": None}
    platt, metrics = model.platt(artifact, rows)
    assert platt is None and "platt_rejected" in metrics
    assert metrics["ece_platt"]["year"]["ece"] > metrics["ece"]["year"]["ece"]
