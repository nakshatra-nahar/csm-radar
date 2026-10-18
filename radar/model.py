"""One logistic regression per tenant, stored as JSON (design §6). sklearn trains it; serving
is a numpy dot product, so a score is exactly the intercept plus its contributions."""

import numpy as np
import pandas as pd

from radar import clock, registry
from radar.registry import FEATURE_NAMES

LABEL_VERSION = "churn_label_90d:v1"
CALIBRATION_WEEKS = 8
ECE_MAX, ECE_MIN_ROWS = 0.02, 300  # design §6: Platt only past this error, on enough rows


def fit(df: pd.DataFrame, features: list[str] = FEATURE_NAMES, C: float = 1.0) -> dict:
    """Train on df[features] and df['label']. Missing values get the training mean, which is
    also the centring mean, so an imputed feature has z = 0 and contributes exactly 0."""
    # imported here: serving (predict) must run without the `ml` extra, as on Vercel
    from sklearn.linear_model import LogisticRegression

    X = df[features].astype(float)
    impute = X.mean().fillna(0.0)  # a feature never observed in training: 0
    filled = X.fillna(impute)
    scale = filled.std(ddof=0).replace(0.0, 1.0)  # a constant feature: z = 0
    z = (filled - impute) / scale
    lr = LogisticRegression(C=C, max_iter=2000).fit(z.to_numpy(), df["label"].to_numpy())
    hashes = registry.manifest()
    return {
        "features": list(features),
        "manifest": {f: hashes[f] for f in features},
        "impute": impute.tolist(), "mean": impute.tolist(), "scale": scale.tolist(),
        "coef": lr.coef_[0].tolist(), "intercept": float(lr.intercept_[0]),
        "platt": None,  # set only if calibration needs it (design §6)
        # one decision per account, so decisions = accounts with a positive row (design §6 gate)
        "n_train": len(df),
        "n_decisions": decisions(df),
        "trained_at": clock.now().isoformat(), "label_version": LABEL_VERSION}


def decisions(rows: pd.DataFrame) -> int:
    """Churn decisions in labelled rows: one per account, so accounts with a positive row."""
    return int(rows.index.get_level_values("account_id")[rows["label"] == 1].nunique())


def intercept(artifact: dict) -> float:
    """The intercept that, with predict's contributions, sums to logit(p)."""
    platt = artifact["platt"]
    return artifact["intercept"] if platt is None else (
        platt["a"] + platt["b"] * artifact["intercept"])


def predict(artifact: dict, X: pd.DataFrame) -> tuple[np.ndarray, np.ndarray]:
    """(p, contributions) with contributions[i, j] = coef_j * z_ij, so intercept(artifact) +
    row sum is logit(p). With Platt (a, b > 0) the calibrated logit is a + b * logit: the
    contributions scale by b, and the order by p is unchanged (the order by p x ARR can
    change)."""
    x = X[artifact["features"]].to_numpy(dtype=float)
    x = np.where(np.isnan(x), artifact["impute"], x)
    z = (x - np.asarray(artifact["mean"])) / np.asarray(artifact["scale"])
    contributions = z * np.asarray(artifact["coef"])
    if artifact["platt"] is not None:
        contributions = contributions * artifact["platt"]["b"]
    logit = intercept(artifact) + contributions.sum(axis=1)
    return 1 / (1 + np.exp(-logit)), contributions


def check_signs(artifact: dict) -> list[str]:
    """Behavioural features whose coefficient sign contradicts their risk_direction."""
    coef = dict(zip(artifact["features"], artifact["coef"], strict=True))
    return [d.name for d in registry.FEATURES
            if d.kind == "behavioural" and d.name in coef and coef[d.name] * d.risk_direction < 0]


def reliability(t: pd.DataFrame) -> dict:
    """Model p (column 'model') in 10 quantile bins per Stripe billing interval (design §6),
    with the expected calibration error. Accounts without a Stripe subscription form the
    'none' segment."""
    out = {}
    for name, seg in t.groupby(t["billing_interval"].fillna("none")):
        bins = seg.groupby(pd.qcut(seg["model"], 10, duplicates="drop"), observed=True).agg(
            mean_p=("model", "mean"), rate=("label", "mean"), rows=("label", "size"))
        ece = float((bins["rows"] * (bins["mean_p"] - bins["rate"]).abs()).sum() / len(seg))
        out[name] = {"rows": len(seg), "ece": ece, "bins": bins.to_dict("records")}
    return out


def _ece(artifact: dict, rows: pd.DataFrame) -> dict[str, dict]:
    t = pd.DataFrame({"model": predict(artifact, rows)[0], "label": rows["label"].to_numpy(),
                      "billing_interval": rows["billing_interval"].to_numpy()})
    return {seg: {"rows": v["rows"], "ece": v["ece"]} for seg, v in reliability(t).items()}


def _worst(ece: dict) -> float:
    return max((v["ece"] for v in ece.values() if v["rows"] >= ECE_MIN_ROWS), default=0.0)


def platt(artifact: dict, train: pd.DataFrame) -> tuple[dict | None, dict]:
    """(platt, metrics): Platt (a, b) fitted on the latest matured weeks when a billing
    interval with enough rows is miscalibrated there (design §6, Ruling Z), and kept only if
    it lowers the worst such segment's ECE. shortcut: judged on the slice it was fitted on;
    hold out weeks if in-sample acceptance ever lets a harmful map through."""
    from sklearn.linear_model import LogisticRegression  # training only, as in fit()

    weeks = sorted(set(train.index.get_level_values("as_of")))[-CALIBRATION_WEEKS:]
    recent = train[train.index.get_level_values("as_of").isin(weeks)]
    before = _ece(artifact, recent)
    if _worst(before) <= ECE_MAX:
        return None, {"ece": before}
    p, _ = predict(artifact, recent)
    logit = np.log(p / (1 - p)).reshape(-1, 1)
    lr = LogisticRegression(C=np.inf).fit(logit, recent["label"])  # unpenalised
    pair = {"a": float(lr.intercept_[0]), "b": float(lr.coef_[0, 0])}
    if pair["b"] <= 0:  # a non-increasing map would reorder the top-K: leave p as it is
        return None, {"ece": before, "platt_rejected": pair}
    after = _ece(artifact | {"platt": pair}, recent)
    if _worst(after) >= _worst(before):  # one pooled sigmoid made a segment worse
        return None, {"ece": before, "ece_platt": after, "platt_rejected": pair}
    return pair, {"ece": before, "ece_platt": after}
