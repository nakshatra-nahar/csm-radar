"""How we know it works (design §7): a rolling-origin backtest of the ranking policy against
simple baselines and a latent-health oracle, on the same rows, rendered as a model card.

Run as `radar eval --tenant <slug> [--out docs]` (design §7): it reads
the tenant from the configured database and the simulator truth from data/truth, and writes
docs/model-card-<slug>.md and eval/<slug>.json.
"""

import dataclasses
import json
from datetime import timedelta
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import average_precision_score, brier_score_loss, roc_auc_score
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from radar import diagnostics, model, policy
from radar.asof import Frames, latest, load_frames, resolver
from radar.bootstrap import HISTORY_END
from radar.config import load_settings
from radar.db import Database
from radar.features import compute_features
from radar.labels import MATURITY, churn_decisions, label_rows, snapshot_grid
from radar.registry import FEATURE_NAMES, FEATURES, rules_score
from radar.sim.params import PARAMS
from radar.tenants import TENANTS, tenant_id

__all__ = ["rules_score", "topk_by_book", "slot_floor", "naive_frames", "current_state", "run",
           "acceptance", "render_model_card"]

POLICIES = ["random", "renewal_sort", "arr_sort", "rules", "model", "oracle", "gbm"]
GROUPS = ["product", "zendesk", "gmail", "billing", "crm"]
WEEK = pd.Timedelta(weeks=1)
LATENCY = pd.Timedelta(days=97)  # the 90-day horizon plus 7 days of label latency (§7)
BOOT = 1000
C_GRID = (0.1, 10.0)  # §6: C stays 1.0; these refits are a sensitivity line, not tuning


def load_truth(data_dir: Path, slug: str) -> dict[str, pd.DataFrame]:
    """The simulator's truth tables: accounts, weekly (latent h) and identity."""
    return {name: pd.read_parquet(Path(data_dir) / "truth" / f"{slug}_{name}.parquet")
            for name in ("accounts", "weekly", "identity")}


def _rank(df: pd.DataFrame, key: pd.Series) -> pd.Series:
    """1-based rank per (as_of, owner_user_id) book: the served worklist's rank_in_book."""
    return policy.rank_in_book(key, df["arr_minor"], df["as_of"], df["owner_user_id"])


def topk_by_book(df: pd.DataFrame, score_col: str, k: int,
                 floor: pd.Series | None = None) -> pd.Series:
    """In each CSM's top K by expected ARR at risk (score * arr_minor), per as_of, among the
    rows passing floor (slot_floor): a slot no row qualifies for stays empty."""
    key = df[score_col] * df["arr_minor"]
    if floor is not None:
        key = key.where(floor)
    return (_rank(df, key) <= k) & key.notna()


def slot_floor(t: pd.DataFrame, col: str, cfg: dict) -> pd.Series:
    """policy.slot_floor on backtest rows: the ranking key (col * arr_minor) at least
    min_slot_lift x that week's mean key, for every scored policy.
    shortcut: the mean is over the labelled test rows, not every eligible account as in
    scoring (label_rows drops accounts whose decision is visible); use the served rows if
    that gap ever matters."""
    key = t[col] * t["arr_minor"]
    return key >= key.groupby(t["as_of"]).transform(policy.slot_floor, cfg)


def _weekly_mean(num: pd.Series, den: pd.Series, week: pd.Series) -> float:
    """sum(num) / sum(den) within each week (across all books), averaged over weeks; a week
    with nothing to divide by (no churned ARR) is left out."""
    g = pd.DataFrame({"n": num, "d": den, "week": week}).groupby("week")[["n", "d"]].sum()
    g = g[g["d"] > 0]
    return float((g["n"] / g["d"]).mean())


def _metrics(y: pd.Series, arr: pd.Series, w: pd.Series, week: pd.Series, score=None,
             prob=None) -> dict:
    """w is each row's chance of being in the top-K (0/1, or K/N for random). Recall and
    precision are weekly, averaged over test weeks (§7). Without a score (random) AUC is 0.5
    and PR-AUC the base rate; Brier needs a probability."""
    hit = y * arr
    precision = _weekly_mean(y * w, w, week)
    if score is not None:
        score = score.fillna(score.min() - 1)
    return {"recall_at_k_arr": _weekly_mean(hit * w, hit, week),
            "precision_at_k": float(precision), "lift_at_k": float(precision / y.mean()),
            "roc_auc": 0.5 if score is None else float(roc_auc_score(y, score)),
            "pr_auc": float(y.mean() if score is None else average_precision_score(y, score)),
            "brier": float("nan") if prob is None else float(brier_score_loss(y, prob))}


def _cut(frames: Frames, origin: pd.Timestamp) -> Frames:
    """What Radar had received by the origin: training labels must not use later knowledge."""
    return dataclasses.replace(
        frames, events=frames.events[frames.events["available_at"] <= origin],
        links=frames.links[frames.links["available_at"] <= origin])


def _oracle_fit(train: pd.DataFrame, test: pd.DataFrame, accounts, weekly) -> np.ndarray:
    X = diagnostics.oracle_features(train, accounts, weekly).dropna()
    lr = make_pipeline(StandardScaler(), LogisticRegression()).fit(
        X, train["label"].loc[X.index])
    Xt = diagnostics.oracle_features(test, accounts, weekly)
    return lr.predict_proba(Xt.fillna(X.mean()))[:, 1]


def naive_frames(frames: Frames) -> Frames:
    """frames with each Salesforce account's history-end row visible from the first event on:
    what a naive pipeline that joins today's CRM table to old snapshots would see."""
    ev = frames.events
    crm = ev["type"] == "crm.account"
    start = ev["occurred_at"].min()
    now = latest(ev[crm], HISTORY_END).assign(occurred_at=start, available_at=start)
    return dataclasses.replace(frames, events=pd.concat([ev[~crm], now]).sort_values(
        ["entity_id", "occurred_at", "version_key"], kind="stable"))


def current_state(rows: pd.DataFrame, naive: pd.DataFrame, frames: Frames) -> pd.DataFrame:
    """rows with every registry feature taken from naive (compute_features on naive_frames)
    plus former_customer (Type at history end): what the naive pipeline trains on."""
    sf = latest(frames.events[frames.events["type"] == "crm.account"], HISTORY_END)
    ids = sf["entity_id"].str.removeprefix("sf_account:").map(
        frames.accounts.set_index("sf_account_id")["id"])
    status = sf.set_index(ids)["status"].reindex(rows.index.get_level_values("account_id"))
    return rows.assign(**naive.reindex(rows.index)[FEATURE_NAMES],
                       former_customer=(status == "Former Customer").astype(float).to_numpy())


def _naive_fit(train: pd.DataFrame, test: pd.DataFrame, naive: pd.DataFrame,
               frames: Frames) -> np.ndarray:
    cols = [*FEATURE_NAMES, "former_customer"]
    X = current_state(train, naive, frames)[cols]
    lr = make_pipeline(SimpleImputer(), StandardScaler(), LogisticRegression(max_iter=2000))
    return lr.fit(X, train["label"]).predict_proba(
        current_state(test, naive, frames)[cols])[:, 1]


def _without(group: str) -> list[str]:
    return [d.name for d in FEATURES if group not in d.source.split("+")]


def _grids(tenant: dict, truth) -> tuple[list, list]:
    """(matured Mondays, Mondays to compute features for): training starts 120 days after the
    last source connected; a label at T is settled at T + 127 days; the VP cases need weeks
    up to their decisions, which may not have matured."""
    start = pd.Timestamp(max(TENANTS[tenant["slug"]].source_start.values()), tz="UTC")
    matured = snapshot_grid(start + timedelta(days=120), HISTORY_END - MATURITY)
    end = matured[-1]
    if truth is not None:
        end = max([end, *truth["accounts"].loc[truth["accounts"]["vp_case"], "decision_at"]])
    return matured, snapshot_grid(start + timedelta(days=120), end)


def run(frames: Frames, tenant: dict, truth: dict | None = None, k: int = 10,
        origins: int = 4, step_weeks: int = 8) -> dict:
    """The backtest. truth (load_truth) adds the oracle, recall by cause, the VP cases,
    identity accuracy and the oracle difficulty band; without it those are left out."""
    matured, grid = _grids(tenant, truth)
    feats = compute_features(frames, tenant, grid)  # once; sliced per origin
    hind = compute_features(frames, tenant, matured, hindsight=True)
    naive = compute_features(naive_frames(frames), tenant, matured)
    decisions = churn_decisions(frames)  # full hindsight: test labels only
    as_of = feats.index.get_level_values("as_of")
    step = step_weeks * WEEK
    starts = matured[-origins * step_weeks:][::step_weeks]
    tests, artifacts, counts = [], {}, []
    for origin in starts:
        train = label_rows(feats[as_of < origin - LATENCY], churn_decisions(_cut(frames, origin)))
        test = label_rows(feats[(as_of >= origin) & (as_of < origin + step)], decisions)
        artifacts[origin] = model.fit(train)
        gbm = HistGradientBoostingClassifier(random_state=0).fit(train[FEATURE_NAMES],
                                                                 train["label"])
        # reindex: hindsight applies later merges, so a merged-away account may be missing
        hind_model = model.fit(hind.reindex(train.index).assign(label=train["label"]))
        test = test.assign(
            model=model.predict(artifacts[origin], test)[0], rules=rules_score(test),
            gbm=gbm.predict_proba(test[FEATURE_NAMES])[:, 1],
            hindsight=model.predict(hind_model, hind.reindex(test.index))[0],
            naive=_naive_fit(train, test, naive, frames),
            **{f"without_{g}": model.predict(model.fit(train, _without(g)), test)[0]
               for g in GROUPS},
            **{f"c_{c}": model.predict(model.fit(train, C=c), test)[0] for c in C_GRID})
        if truth is not None:
            test["oracle"] = _oracle_fit(train, test, frames.accounts, truth["weekly"])
        tests.append(test)
        counts.append({"origin": origin, "train_from": train.index.get_level_values("as_of").min(),
                       "train_to": train.index.get_level_values("as_of").max(),
                       "test_from": origin, "test_to": origin + step - WEEK,
                       "train_rows": len(train), "train_decisions": _decisions(train),
                       "test_rows": len(test), "test_decisions": _decisions(test)})
    t = pd.concat(tests).reset_index()
    arr = t["arr_minor"].astype(float)
    t["renewal"] = -(t["term_end"] - t["as_of"]).dt.days  # sooner term end first
    book = t.groupby(["as_of", "owner_user_id"], dropna=False)["account_id"].transform("size")
    t["w_random"] = np.minimum(1.0, k / book)
    t["w_renewal_sort"] = _rank(t, t["renewal"]) <= k
    t["w_arr_sort"] = _rank(t, arr) <= k
    scores = {"renewal_sort": t["renewal"], "arr_sort": arr}
    for p in ("rules", "model", "oracle", "gbm", "naive", "hindsight",
              *(f"without_{g}" for g in GROUPS),
              *(f"c_{c}" for c in C_GRID)):
        if p in t:
            t[f"w_{p}"] = topk_by_book(t, p, k, slot_floor(t, p, tenant["config"]))
            scores[p] = t[p]
    policies = {p: _metrics(t["label"], arr, t[f"w_{p}"].astype(float), t["as_of"],
                            scores.get(p), t[p] if p in ("model", "oracle", "gbm") else None)
                for p in POLICIES if f"w_{p}" in t}
    recall = policies["model"]["recall_at_k_arr"]

    def recall_of(col: str) -> float:
        return _metrics(t["label"], arr, t[f"w_{col}"], t["as_of"])["recall_at_k_arr"]

    ablated = {g: recall_of(f"without_{g}") for g in GROUPS}

    train_all = label_rows(feats[as_of.isin(matured)], decisions)
    final = model.fit(train_all)
    table = diagnostics.feature_table(train_all)
    single = table.drop("full_model")["single_auc"]
    result = {
        "tenant": tenant["slug"], "k": k, "policies": policies,
        "ci": _ci(t, np.random.default_rng(0)),
        "reliability": _reliability(t), "lead_time": _lead_time(t),
        "hindsight_vs_pit": {"pit_auc": policies["model"]["roc_auc"],
                             "hindsight_auc": float(roc_auc_score(t["label"], t["hindsight"])),
                             "naive_auc": float(roc_auc_score(t["label"], t["naive"])),
                             "pit_recall_at_k_arr": recall,
                             "hindsight_recall_at_k_arr": recall_of("hindsight"),
                             "naive_recall_at_k_arr": recall_of("naive")},
        "ablation": {g: {"recall_at_k_arr": a, "delta": a - recall} for g, a in ablated.items()},
        "c_sensitivity": {str(c): recall if c == 1.0 else recall_of(f"c_{c}")
                          for c in sorted((1.0, *C_GRID))},
        "outage_week": _outage(t, tenant["slug"]),
        "coef": dict(zip(final["features"], final["coef"], strict=True)),
        "signs": model.check_signs(final),
        "difficulty": {"max_single_auc": float(single.max()),
                       "max_single_feature": single.idxmax(),
                       "full_model_auc": float(table.loc["full_model", "single_auc"]),
                       "single_auc": single.to_dict()},
        "counts": {"origins": counts, "final_train_rows": len(train_all),
                   "final_train_decisions": _decisions(train_all),
                   "coverage": {"telemetry_linked": float(t["telemetry_linked"].mean()),
                                "gmail": float(t["days_since_customer_email"].notna().mean()),
                                "coverage_ok": float(t["coverage_ok"].mean())}}}
    if truth is not None:
        result["difficulty"]["oracle_auc"] = diagnostics.oracle_auc(
            train_all, frames.accounts, truth["weekly"])
        sf = frames.accounts.set_index("id")["sf_account_id"]
        result["by_cause"] = _by_cause(t, truth["accounts"], sf)
        churned = truth["accounts"].dropna(subset=["decision_at"])
        result["unforecastable_share"] = float(churned.loc[~churned["involuntary"],
                                                           "unforecastable"].mean())
        result["vp_cases"] = _vp_cases(feats, artifacts, truth["accounts"], sf, k,
                                       tenant["config"])
        result["identity"] = _identity(frames, truth["identity"])
    return result


def _renewal_vs_random(recall_ci: dict) -> str | None:
    """'worse than random' only where renewal sort's interval lies wholly below random's, 'no
    better' where they overlap, nothing where renewal sort is clearly better (Ruling BG)."""
    rs, rnd = recall_ci["renewal_sort"], recall_ci["random"]
    if rs["lo"] > rnd["hi"]:
        return None
    verdict = "worse than" if rs["hi"] < rnd["lo"] else "no better than"
    return (f"- Renewal sort does {verdict} random because accounts near renewal have mostly"
            " already announced their decision and left the worklist, so the ones left are"
            " mostly renewers.")


def _decisions(rows: pd.DataFrame) -> int:
    return int(rows.index.get_level_values("account_id")[rows["label"] == 1].nunique())


def _ci(t: pd.DataFrame, rng: np.random.Generator) -> dict:
    """Paired cluster bootstrap over accounts (an account's weeks move together): 95%
    percentile intervals for each policy's weekly-mean recall and for model minus
    renewal_sort / rules, the same statistic as the headline (_metrics)."""
    acct = pd.factorize(t["account_id"])[0]
    week = pd.factorize(t["as_of"])[0]
    n, n_weeks = acct.max() + 1, week.max() + 1
    hit = (t["label"] * t["arr_minor"].astype(float)).to_numpy()
    counts = rng.multinomial(n, np.full(n, 1 / n), size=BOOT)  # resampled accounts

    def by_account_week(values: np.ndarray) -> np.ndarray:
        m = np.zeros((n, n_weeks))
        np.add.at(m, (acct, week), values)
        return m

    den = by_account_week(hit)

    def weekly_mean(num: np.ndarray, c: np.ndarray) -> np.ndarray:
        d = c @ den
        with np.errstate(invalid="ignore", divide="ignore"):
            return np.nanmean(np.where(d > 0, (c @ num) / d, np.nan), axis=-1)

    nums = {p: by_account_week(hit * t[f"w_{p}"].to_numpy(float))
            for p in POLICIES if f"w_{p}" in t}
    recall = {p: weekly_mean(m, counts) for p, m in nums.items()}
    point = {p: float(weekly_mean(m, np.ones(n))) for p, m in nums.items()}

    def interval(sample, at):
        lo, hi = np.percentile(sample, [2.5, 97.5])
        return {"diff": float(at), "lo": float(lo), "hi": float(hi)}

    out = {"recall": {p: interval(r, point[p]) for p, r in recall.items()}}
    for vs in ("renewal_sort", "rules"):
        out[f"model_minus_{vs}"] = interval(recall["model"] - recall[vs],
                                            point["model"] - point[vs])
    return out


def _reliability(t: pd.DataFrame) -> dict:
    """Model p in 10 quantile bins per Stripe billing interval (design §6), with the expected
    calibration error. Accounts without a Stripe subscription form the 'none' segment."""
    out = {}
    for name, seg in t.groupby(t["billing_interval"].fillna("none")):
        bins = seg.groupby(pd.qcut(seg["model"], 10, duplicates="drop"), observed=True).agg(
            mean_p=("model", "mean"), rate=("label", "mean"), rows=("label", "size"))
        ece = float((bins["rows"] * (bins["mean_p"] - bins["rate"]).abs()).sum() / len(seg))
        out[name] = {"rows": len(seg), "ece": ece, "bins": bins.to_dict("records")}
    return out


def _lead_time(t: pd.DataFrame) -> dict:
    """For each decision in the test windows: weeks from the first of 2 consecutive weeks in
    the owner's model top-K to the decision. Unflagged decisions count against share_6w."""
    flagged = t.loc[t["w_model"], ["account_id", "as_of"]]
    seen = set(zip(flagged["account_id"], flagged["as_of"], strict=True))
    sustained = flagged[[(a, T + WEEK) in seen for a, T in zip(
        flagged["account_id"], flagged["as_of"], strict=True)]]
    decided = t[t["label"] == 1].groupby("account_id")["decision_at"].first()
    first = sustained.groupby("account_id")["as_of"].min().reindex(decided.index)
    lead = (decided - first).dt.total_seconds() / WEEK.total_seconds()
    return {"decisions": len(decided), "flagged": int(lead.notna().sum()),
            "median_weeks": float(lead.median()), "share_6w": float((lead >= 6).mean())}


def _outage(t: pd.DataFrame, slug: str) -> dict:
    """Accounts newly entering a CSM's model top-K each week; the max over CSMs in the week
    the outage tickets first show (the Monday after the outage week starts) vs the median."""
    flagged = t.loc[t["w_model"], ["account_id", "as_of", "owner_user_id"]]
    before = set(zip(flagged["account_id"], flagged["as_of"] + WEEK, strict=True))
    new = flagged[[(a, T) not in before for a, T in zip(
        flagged["account_id"], flagged["as_of"], strict=True)]]
    new = new[new["as_of"] > t["as_of"].min()]  # the first week has no previous week
    per_week = new.groupby(["as_of", "owner_user_id"]).size().groupby("as_of").max().reindex(
        sorted(t["as_of"].unique())[1:], fill_value=0)
    week = PARAMS["outage"].get(slug)
    week = None if week is None else pd.Timestamp(week, tz="UTC") + WEEK
    return {"outage_week": week, "outage_max_new": None if week not in per_week.index
            else int(per_week[week]), "median_max_new": float(per_week.median()),
            "by_week": {str(T.date()): int(v) for T, v in per_week.items()}}


def _by_cause(t: pd.DataFrame, accounts: pd.DataFrame, sf: pd.Series) -> dict:
    """Recall for positives grouped by their true churn cause, model and random, plus all
    unforecastable causes pooled (single causes have few decisions)."""
    truth = accounts.set_index("sf_account_id")
    pos = t[t["label"] == 1]
    cause = pos["account_id"].map(sf).map(truth["cause"]).fillna("unknown")
    unforecastable = set(truth.loc[truth["unforecastable"], "cause"])

    def recall(rows: pd.DataFrame) -> dict:
        arr = rows["arr_minor"].astype(float)
        return {"decisions": rows["account_id"].nunique(),
                "recall_at_k_arr": float((arr * rows["w_model"]).sum() / arr.sum()),
                "random_recall": float((arr * rows["w_random"]).sum() / arr.sum())}

    out = {c: recall(rows) | {"unforecastable": c in unforecastable}
           for c, rows in pos.groupby(cause)}
    out["all unforecastable"] = recall(pos[cause.isin(unforecastable)]) | {
        "unforecastable": True}
    return out


def _vp_cases(feats: pd.DataFrame, artifacts: dict, accounts: pd.DataFrame, sf: pd.Series,
              k: int, cfg: dict) -> list[dict]:
    """Each VP account's weekly rank (1 = top) in its owner's book over the 20 weeks before
    its decision, under renewal_sort and the model of the latest origin at or before that
    week (every origin's training ends long before the decisions). None: not in window, or
    below_floor: its key (p * arr) is under the slot floor (Ruling AZ), so the model would
    not serve it."""
    by_sf = pd.Series(sf.index, index=sf.to_numpy())
    starts = sorted(artifacts)
    as_of = feats.index.get_level_values("as_of")
    out = []
    for case in accounts[accounts["vp_case"]].itertuples():
        aid, weeks = by_sf[case.sf_account_id], []
        for T in sorted(set(as_of[(as_of >= case.decision_at - 20 * WEEK)
                                  & (as_of < case.decision_at)])):
            book = feats[as_of == T].reset_index()
            book = book[book["eligible"] & ~book["decision_visible"]]
            mine = book["account_id"] == aid
            week = {"as_of": T, "renewal_sort": None, "model": None, "below_floor": False,
                    "book_size": None}
            if mine.any():
                artifact = artifacts[max([o for o in starts if o <= T], default=starts[0])]
                book = book.assign(model=model.predict(artifact, book)[0])
                key = (book["model"] * book["arr_minor"]).where(slot_floor(book, "model", cfg))
                renewal = -(book["term_end"] - book["as_of"]).dt.days
                week["renewal_sort"] = int(_rank(book, renewal)[mine].iloc[0])
                week["below_floor"] = bool(key[mine].isna().iloc[0])
                if not week["below_floor"]:
                    week["model"] = int(_rank(book, key)[mine].iloc[0])
                owner = book.loc[mine, "owner_user_id"].iloc[0]
                week["book_size"] = int((book["owner_user_id"] == owner).sum())
            weeks.append(week)
        out.append({"sf_account_id": case.sf_account_id, "name": case.name,
                    "cause": case.cause, "decision_at": case.decision_at, "k": k,
                    "weeks": weeks})
    return out


def _identity(frames: Frames, truth: pd.DataFrame) -> dict:
    """Per match method and source: links, precision (the link resolves, through merges, to
    the true account) and recall (correct links / the source's true references)."""
    current = frames.links.drop_duplicates("account_ref", keep="last").set_index("account_ref")
    target = resolver(frames.links, None)[0]
    want = truth.set_index("account_ref")
    want_id = want["true_sf_account_id"].map(frames.accounts.set_index("sf_account_id")["id"])
    linked = current.join(want[["source"]], how="inner")
    correct = target.reindex(linked.index) == want_id.reindex(linked.index)
    refs = want["source"].value_counts()
    out = {}
    for (method, source), rows in linked.groupby(["method", "source"]):
        ok = correct[rows.index]
        out[f"{method} ({source})"] = {"links": len(rows), "precision": float(ok.mean()),
                                       "recall": float(ok.sum() / refs[source])}
    return out


def acceptance(result: dict) -> list[str]:
    """Violations of the design §3.6 difficulty bands and of the §6 promotion floors."""
    d, p, out = result["difficulty"], result["policies"], []
    if "oracle_auc" in d and not 0.80 <= d["oracle_auc"] <= 0.92:
        out.append(f"oracle AUC {d['oracle_auc']:.3f} is outside [0.80, 0.92]")
    if d["max_single_auc"] > 0.72:
        out.append(f"{d['max_single_feature']} alone has AUC {d['max_single_auc']:.3f} > 0.72")
    if d["full_model_auc"] > 0.86:
        out.append(f"the full observable model has AUC {d['full_model_auc']:.3f} > 0.86")
    if not p["model"]["recall_at_k_arr"] > p["renewal_sort"]["recall_at_k_arr"]:
        out.append("model recall@K does not beat the renewal-date sort")
    if result["signs"]:
        out.append(f"coefficients with the wrong sign: {', '.join(result['signs'])}")
    return out


SECTIONS = ["Summary", "How it was tested", "Recall@K by policy", "Ranking quality and calibration",
            "Lead time", "Confidence intervals", "Recall by churn cause",
            "Leakage check: hindsight, point-in-time and naive current-state",
            "Ablation: which sources matter",
            "The VP's two lost renewals", "Outage week", "Coefficients",
            "Identity resolution", "Acceptance gates", "Known limits"]
LABELS = {"random": "Random (K/N per book)", "renewal_sort": "Renewal date (soonest first)",
          "arr_sort": "ARR (largest first)", "rules": "Rules heuristic (rules_score v1)",
          "model": "**Model (deployed)**", "oracle": "Oracle (sees true health; not deployable)",
          "gbm": "Gradient boosting (diagnostic only)"}
BILLED = {"year": "billed yearly", "month": "billed monthly", "none": "no Stripe subscription"}


def _pct(x: float) -> str:
    return f"{100 * x:.0f}%"


def _ci_text(c: dict, points: bool = False) -> str:
    if points:
        return (f"{100 * c['diff']:+.0f} points (95% CI {100 * c['lo']:+.0f} to"
                f" {100 * c['hi']:+.0f})")
    return f"{_pct(c['diff'])} (95% CI {_pct(c['lo'])} to {_pct(c['hi'])})"


def _day(t) -> str:
    return str(pd.Timestamp(t).date())


def _num(x, fmt: str) -> str:
    return "" if x is None or np.isnan(x) else format(x, fmt)

def _vp_lines(case: dict, k: int) -> list[str]:
    weeks, decided = case["weeks"], pd.Timestamp(case["decision_at"])

    def ranks(policy: str) -> str:
        return ", ".join("below floor" if policy == "model" and w["below_floor"]
                         else "-" if w[policy] is None else str(w[policy]) for w in weeks)

    def first(policy: str) -> str:
        at = next((w["as_of"] for w in weeks if isinstance(w[policy], int) and w[policy] <= k),
                  None)
        if at is None:
            return "never"
        n = (decided - pd.Timestamp(at)).days // 7
        return f"from {_day(at)} ({n} week{'s' * (n != 1)} before)"

    return [f"**{case['name']}** (cause: {case['cause']}) decided {_day(decided)}. Top {k}"
            f" under the model {first('model')}; under renewal sort {first('renewal_sort')}.",
            "", "Weekly rank in the CSM's book over the 20 weeks before the decision (- means"
            " not yet in the renewal window; below floor means its expected ARR at risk is"
            " under the slot floor, so the model would not serve it):", "",
            f"- renewal sort: {ranks('renewal_sort')}", f"- model: {ranks('model')}", ""]


def render_model_card(tenant_slug: str, result: dict) -> str:
    """Markdown in design §7 order, short sentences, numbers with their intervals."""
    r, k = result, result["k"]
    p, ci, origins = r["policies"], r["ci"], r["counts"]["origins"]
    model_recall = ci["recall"]["model"]
    violations = acceptance(r)
    out = [f"# Model card: {tenant_slug}", "",
           f"Generated by `radar eval --tenant {tenant_slug}` from the synthetic world. Each"
           f" CSM works up to {k} accounts from their book each week.", ""]

    def section(title: str, *lines: str) -> None:
        assert title in SECTIONS
        out.extend([f"## {title}", "", *lines, ""])

    section("Summary",
            f"- In an average test week the model's list of up to {k} per CSM held"
            f" {_ci_text(model_recall)} of the ARR that went on to churn within 90 days.",
            f"- Sorting by renewal date held {_pct(p['renewal_sort']['recall_at_k_arr'])}, the"
            f" rules heuristic {_pct(p['rules']['recall_at_k_arr'])} and random picks"
            f" {_pct(p['random']['recall_at_k_arr'])}.",
            *filter(None, [_renewal_vs_random(ci["recall"])]),
            f"- Model minus renewal sort: {_ci_text(ci['model_minus_renewal_sort'], True)}."
            f" Model minus rules: {_ci_text(ci['model_minus_rules'], True)}.",
            *(["- For this tenant the deployed model holds less churned ARR than the rules"
               " heuristic."] if ci["model_minus_rules"]["diff"] < 0 else []),
            *([f"- An oracle that sees true account health reaches"
               f" {_pct(p['oracle']['recall_at_k_arr'])}: the gap is what better data could"
               " still buy."] if "oracle" in p else []),
            "- " + ("All acceptance gates pass." if not violations
                    else "Acceptance gates failed: " + "; ".join(violations) + "."))
    section("How it was tested",
            f"Snapshots are Mondays. {len(origins)} origins, 8 weeks apart, cover the last"
            f" {8 * len(origins)} weeks with settled labels ({_day(origins[0]['test_from'])} to"
            f" {_day(origins[-1]['test_to'])}). At each origin the model is refit on snapshots"
            " at least 97 days older, labelled only with what had arrived by the origin, then"
            " scored on the next 8 weeks. Test labels use full hindsight. A book is the"
            " accounts a CSM owns at the snapshot.", "",
            "| origin | train snapshots | train rows | train decisions | test rows |"
            " test decisions |", "|---|---|---|---|---|---|",
            *[f"| {_day(o['origin'])} | {_day(o['train_from'])} to {_day(o['train_to'])} |"
              f" {o['train_rows']:,} | {o['train_decisions']} | {o['test_rows']:,} |"
              f" {o['test_decisions']} |" for o in origins], "",
            "Share of test rows with product telemetry linked:"
            f" {_pct(r['counts']['coverage']['telemetry_linked'])}; with Gmail connected:"
            f" {_pct(r['counts']['coverage']['gmail'])}; passing the coverage rule:"
            f" {_pct(r['counts']['coverage']['coverage_ok'])}.")
    section("Recall@K by policy",
            "Recall (ARR-weighted) and precision are computed each week across every CSM's"
            " book, then averaged over test weeks. Lift is precision over the base rate."
            " Every policy ranks the same rows by its expected ARR at risk (score x ARR). As"
            " served, a slot takes only an account whose key is at least min_slot_lift (1.0)"
            " x the tenant's mean key that week, so a low-risk book gets fewer than K; the"
            " floor only shortens a list, never reorders it. Random and the sorts have no"
            " floor. Here the mean key is over the labelled test rows, which leave out"
            " accounts whose decision is already visible (a shortcut: the served mean is over"
            " every eligible account).", "",
            "| policy | recall@K (95% CI) | precision@K | lift | ROC-AUC | PR-AUC | Brier |",
            "|---|---|---|---|---|---|---|",
            *[f"| {LABELS[name]} | {_ci_text(ci['recall'][name])} | {_pct(m['precision_at_k'])}"
              f" | {m['lift_at_k']:.1f}x | {m['roc_auc']:.2f} | {m['pr_auc']:.2f} |"
              f" {_num(m['brier'], '.3f')} |"
              for name, m in p.items()])

    lead, rel = r["lead_time"], r["reliability"]
    section("Ranking quality and calibration",
            "Reliability of the model's probability, 10 quantile bins per segment. Design §6"
            " adds a Platt step only if the error (ECE) exceeds 0.02 in a segment with at"
            " least 300 rows.", "",
            *[f"- {BILLED[seg]}: {v['rows']:,} rows, ECE {v['ece']:.3f}"
              + (" (needs Platt)" if v["ece"] > 0.02 and v["rows"] >= 300 else "")
              for seg, v in rel.items()])
    section("Lead time",
            f"Of {lead['decisions']} churn decisions in the test weeks, the model flagged"
            f" {lead['flagged']} (on its list for 2 weeks in a row) before the decision."
            f" Median lead: {_num(lead['median_weeks'], '.1f')} weeks."
            f" {_pct(lead['share_6w'])} of all decisions were flagged at least 6 weeks ahead."
            " The label horizon caps lead time at about 13 weeks.")
    section("Confidence intervals",
            "Paired cluster bootstrap over accounts, 1,000 resamples: an account's weeks stay"
            " together, and both policies are scored on the same resample.", "",
            f"- Model minus renewal sort: {_ci_text(ci['model_minus_renewal_sort'], True)}.",
            f"- Model minus rules: {_ci_text(ci['model_minus_rules'], True)}.")
    if "by_cause" in r:
        causes = r["by_cause"]
        pooled = causes["all unforecastable"]
        section("Recall by churn cause",
                "Causes marked * are drawn independently of account health (acquired, business"
                " closed, ...). They should sit near random: a large gap would mean leakage."
                f" They are {_pct(r['unforecastable_share'])} of this tenant's voluntary churn"
                " decisions in the simulator's truth.",
                "", "| cause | decisions | model recall | random recall |", "|---|---|---|---|",
                *[f"| {c}{' *' if v['unforecastable'] else ''} | {v['decisions']} |"
                  f" {_pct(v['recall_at_k_arr'])} | {_pct(v['random_recall'])} |"
                  for c, v in sorted(causes.items(), key=lambda cv: -cv[1]["decisions"])
                  if c != "all unforecastable"], "",
                f"All unforecastable causes pooled: model {_pct(pooled['recall_at_k_arr'])},"
                f" random {_pct(pooled['random_recall'])} ({pooled['decisions']} decisions).")
    h = r["hindsight_vs_pit"]
    section("Leakage check: hindsight, point-in-time and naive current-state",
            "| features | recall@K | ROC-AUC |", "|---|---|---|",
            f"| point-in-time (deployed) | {_pct(h['pit_recall_at_k_arr'])} |"
            f" {h['pit_auc']:.3f} |",
            "| hindsight: late data arrives on time |"
            f" {_pct(h['hindsight_recall_at_k_arr'])} | {h['hindsight_auc']:.3f} |",
            f"| naive current-state: today's Salesforce row | {_pct(h['naive_recall_at_k_arr'])}"
            f" | {h['naive_auc']:.3f} |", "",
            "Hindsight features use every record as if it had arrived when it happened:"
            f" {h['hindsight_auc'] - h['pit_auc']:+.3f} AUC, so late arrivals barely change"
            " this world's weekly snapshots.",
            "The naive current-state model sees each Salesforce account row (Type, ARR__c,"
            " Renewal_Date__c) as it stands at history end in every snapshot. The registry"
            " computes the same features from it, plus former_customer, so a churned account"
            f" is already a Former Customer; its {h['naive_auc'] - h['pit_auc']:+.3f} AUC is"
            " the optimism the as-of rule prevents.")
    section("Ablation: which sources matter",
            "Each source's features removed in turn, model refit at every origin. A bigger"
            " drop means the integration matters more.", "",
            "| source removed | recall@K | change |", "|---|---|---|",
            *[f"| {g} | {_pct(v['recall_at_k_arr'])} | {100 * v['delta']:+.1f} points |"
              for g, v in sorted(r["ablation"].items(), key=lambda gv: gv[1]["delta"])])
    if r.get("vp_cases") == []:
        section("The VP's two lost renewals", "The two planted cases are hearthline accounts.")
    elif "vp_cases" in r:
        lines = [line for c in r["vp_cases"] for line in _vp_lines(c, k)]
        section("The VP's two lost renewals",
                "Each week is scored by the model of the latest origin at or before it; every"
                " origin's training ended before these decisions.", "", *lines[:-1])
    o = r["outage_week"]
    section("Outage week",
            "No outage week falls in the test windows." if o["outage_max_new"] is None else
            f"In the week the outage tickets arrived ({_day(o['outage_week'])}), at most"
            f" {o['outage_max_new']} account(s) newly entered any one CSM's top {k}. A median"
            f" week has {_num(o['median_max_new'], '.0f')}. The outage does not flood the"
            " worklist.")
    risk = {d.name: d.risk_direction for d in FEATURES}
    section("Coefficients",
            f"One final model on all {r['counts']['final_train_rows']:,} settled rows"
            f" ({r['counts']['final_train_decisions']} decisions). Coefficients are per"
            " standard deviation, in log-odds.", "",
            "| feature | coefficient | risk direction | alone: ROC-AUC |", "|---|---|---|---|",
            *[f"| {f} | {c:+.2f} | {'' if risk[f] is None else f'{risk[f]:+d}'} |"
              f" {_num(r['difficulty']['single_auc'][f], '.2f')} |"
              for f, c in r["coef"].items()], "",
            "Every behavioural sign matches its risk direction." if not r["signs"] else
            f"Wrong signs: {', '.join(r['signs'])}.", "",
            "Sensitivity to the L2 strength C (fixed at 1.0, not tuned): model recall@K is "
            + ", ".join(f"{_pct(v)} at C = {float(c):g}" for c, v in r["c_sensitivity"].items())
            + ".")
    if "identity" in r:
        section("Identity resolution",
                "Each source record is linked to a Salesforce account by exact keys only."
                " Precision: links that reach the true account. Recall: correct links over"
                " the source's true references.", "",
                "| method (source) | links | precision | recall |", "|---|---|---|---|",
                *[f"| {m} | {v['links']:,} | {v['precision']:.3f} |"
                  f" {v['recall']:.3f} |" for m, v in r["identity"].items()])
    d = r["difficulty"]
    beats = p["model"]["recall_at_k_arr"] > p["renewal_sort"]["recall_at_k_arr"]
    section("Acceptance gates",
            *([f"- Oracle ROC-AUC {d['oracle_auc']:.3f} (band 0.80 to 0.92)."]
              if "oracle_auc" in d else []),
            f"- Best single feature: {d['max_single_feature']}, ROC-AUC"
            f" {d['max_single_auc']:.3f} (at most 0.72).",
            f"- Full observable model, in sample: ROC-AUC {d['full_model_auc']:.3f} (at most"
            " 0.86).",
            f"- Model recall@K beats renewal sort: {'yes' if beats else 'no'}.",
            f"- Coefficient signs: {'all match' if not r['signs'] else ', '.join(r['signs'])}.",
            "", "Result: " + ("all gates pass." if not violations else "; ".join(violations) + "."))
    section("Known limits",
            "- Seat changes carry weak signal: seat_change_180d alone has ROC-AUC"
            f" {d['single_auc']['seat_change_180d']:.2f}. usage_drop also moves when a customer"
            " cuts seats, so some seat-cut risk shows up as a usage drop.",
            "- Capacity is a cap: a slot goes only to an account whose expected ARR at risk is"
            " at least the tenant mean, so a healthy book gets fewer than K. The backtest takes"
            " that mean over labelled test rows, not every eligible account.",
            "- Gradient boosting is a diagnostic row: does the linear model miss structure? It"
            " has no serving path.",
            "- Causes with few decisions have noisy recall. The world is synthetic: these"
            " numbers show the method works, not what a real book will do.")
    return "\n".join(out)


def to_json(result: dict) -> str:
    """JSON with null for NaN and infinity (strict parsers reject the bare tokens)."""
    text = json.dumps(result, default=lambda o: o.item() if isinstance(o, np.generic)
                      else str(o))
    return json.dumps(json.loads(text, parse_constant=lambda _: None), indent=1,
                      allow_nan=False)


def write_report(slug: str, out: Path = Path("docs")) -> list[str]:
    """Evaluate a tenant already replayed into the configured database: writes
    out/model-card-<slug>.md and eval/<slug>.json; returns the acceptance violations."""
    settings = load_settings()
    db, tid = Database(settings.worker_dsn), tenant_id(slug)
    try:
        with db.tenant_tx(tid) as conn:
            frames = load_frames(conn, tid)
            tenant = conn.execute("SELECT * FROM tenants WHERE id = %s", (tid,)).fetchone()
    finally:
        db.close()
    result = run(frames, tenant, load_truth(settings.data_dir, slug),
                 k=tenant["config"]["capacity_per_csm"])
    out.mkdir(parents=True, exist_ok=True)
    (out / f"model-card-{slug}.md").write_text(render_model_card(slug, result))
    Path("eval").mkdir(exist_ok=True)
    Path("eval", f"{slug}.json").write_text(to_json(result))
    return acceptance(result)
