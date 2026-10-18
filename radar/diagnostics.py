"""How much honest signal each feature carries (Task 5b), and the design §3.6 difficulty
numbers: the best single feature, the full observable model and the latent-health oracle."""


import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import roc_auc_score
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from radar import model, registry
from radar.asof import Frames
from radar.labels import build_training_set, training_grid
from radar.sim.params import HISTORY_END

COLUMNS = ["prevalence_nonzero", "missing_share", "single_auc", "coef", "sign_ok"]


def training_set(frames: Frames, tenant: dict) -> pd.DataFrame:
    """Monday snapshots from 120 days after the last source connected until the last settled
    window before history_end (the Task 5 training population)."""
    return build_training_set(frames, tenant, training_grid(tenant["slug"], HISTORY_END),
                              HISTORY_END)


def _auc(y: pd.Series, x: pd.Series, direction: int | None) -> float:
    """ROC-AUC of x as a risk score on rows where it is known; a feature with no risk
    direction is scored in whichever direction separates better."""
    seen = x.notna()
    if y[seen].nunique() < 2:
        return np.nan
    auc = roc_auc_score(y[seen], x[seen] * (direction or 1))
    return max(auc, 1 - auc) if direction is None else auc


def feature_table(train: pd.DataFrame) -> pd.DataFrame:
    """One row per feature (registry order), plus a full_model row with the in-sample AUC of
    the fitted observable model. sign_ok is NA for features without a risk direction."""
    artifact = model.fit(train)
    coef = dict(zip(artifact["features"], artifact["coef"], strict=True))
    y = train["label"]
    rows = {}
    for d in registry.FEATURES:
        x = train[d.name].astype(float)
        rows[d.name] = {
            "prevalence_nonzero": float((x.fillna(0) != 0).mean()),
            "missing_share": float(x.isna().mean()),
            "single_auc": _auc(y, x, d.risk_direction),
            "coef": coef[d.name],
            "sign_ok": pd.NA if d.risk_direction is None else coef[d.name] * d.risk_direction >= 0}
    rows["full_model"] = {"single_auc": roc_auc_score(y, model.predict(artifact, train)[0])}
    return pd.DataFrame.from_dict(rows, orient="index").reindex(columns=COLUMNS)


ORACLE = ["h", "mean8", "slope", "billing_month_to_month", "term_end_within_60d"]


def oracle_features(rows: pd.DataFrame, accounts: pd.DataFrame,
                    weekly: pd.DataFrame) -> pd.DataFrame:
    """Per row: latent health known before T (the last week's h, its 8-week mean and slope)
    plus contract state (design §3.6). NaN h where the truth has no week before T."""
    w = weekly.assign(week=pd.to_datetime(weekly["week_start"])).sort_values(
        ["sf_account_id", "week"])
    g = w.groupby("sf_account_id")["h"]
    w = w.assign(mean8=g.transform(lambda h: h.rolling(8, min_periods=1).mean()),
                 slope=g.diff(7).fillna(0) / 7).set_index(["sf_account_id", "week"])
    sf = accounts.set_index("id")["sf_account_id"]
    T = rows.index.get_level_values("as_of")
    key = pd.MultiIndex.from_arrays([sf.reindex(rows.index.get_level_values("account_id")),
                                     (T - pd.Timedelta(days=7)).tz_localize(None).normalize()])
    X = w[["h", "mean8", "slope"]].reindex(key).set_axis(rows.index)
    return X.join(rows[["billing_month_to_month", "term_end_within_60d"]].fillna(0))[ORACLE]


def oracle_auc(train: pd.DataFrame, accounts: pd.DataFrame, weekly: pd.DataFrame) -> float:
    """In-sample AUC of a logistic regression on oracle_features (design §3.6)."""
    X = oracle_features(train, accounts, weekly).dropna()
    y = train["label"].loc[X.index]
    lr = make_pipeline(StandardScaler(), LogisticRegression()).fit(X, y)
    return roc_auc_score(y, lr.predict_proba(X)[:, 1])


def feature_report(frames: Frames, tenant: dict, truth_world=None) -> pd.DataFrame:
    """feature_table on the tenant's training set; with the simulator's World, an oracle row
    too. Task 6 reads the §3.6 bands from the single_auc column."""
    train = training_set(frames, tenant)
    report = feature_table(train)
    if truth_world is not None:
        report.loc["oracle", "single_auc"] = oracle_auc(train, frames.accounts,
                                                        truth_world.weekly)
    return report
