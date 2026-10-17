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
