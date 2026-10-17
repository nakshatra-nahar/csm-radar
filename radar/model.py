"""One logistic regression per tenant, stored as JSON (design §6). sklearn trains it; serving
is a numpy dot product, so a score is exactly the intercept plus its contributions."""

import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression

from radar import clock, registry
from radar.registry import FEATURE_NAMES

LABEL_VERSION = "churn_label_90d:v1"


def fit(df: pd.DataFrame, features: list[str] = FEATURE_NAMES, C: float = 1.0) -> dict:
    """Train on df[features] and df['label']. Missing values get the training mean, which is
    also the centring mean, so an imputed feature has z = 0 and contributes exactly 0."""
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
        "n_train": len(df), "n_decisions": int(df["label"].sum()),
        "trained_at": clock.now().isoformat(), "label_version": LABEL_VERSION}


def predict(artifact: dict, X: pd.DataFrame) -> tuple[np.ndarray, np.ndarray]:
    """(p, contributions) with contributions[i, j] = coef_j * z_ij, so intercept + row sum is
    logit(p). shortcut: ignores platt, which fit never sets; apply it here once it does."""
    x = X[artifact["features"]].to_numpy(dtype=float)
    x = np.where(np.isnan(x), artifact["impute"], x)
    z = (x - np.asarray(artifact["mean"])) / np.asarray(artifact["scale"])
    contributions = z * np.asarray(artifact["coef"])
    logit = artifact["intercept"] + contributions.sum(axis=1)
    return 1 / (1 + np.exp(-logit)), contributions


def check_signs(artifact: dict) -> list[str]:
    """Behavioural features whose coefficient sign contradicts their risk_direction."""
    coef = dict(zip(artifact["features"], artifact["coef"], strict=True))
    return [d.name for d in registry.FEATURES
            if d.kind == "behavioural" and d.name in coef and coef[d.name] * d.risk_direction < 0]
