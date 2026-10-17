"""Churn decisions drawn from latent health (design §3.2).

Contract accounts decide at term_end - notice; month-to-month accounts by a weekly hazard;
unforecastable churn is drawn independently of h; monthly card payers can churn involuntarily.
"""

import itertools
from datetime import date, timedelta

import numpy as np
import pandas as pd

from radar.sim.model import Account, PaymentFailure, Shock, add_months, local_dt
from radar.sim.names import VP_TERM_ENDS
from radar.sim.params import PARAMS
from radar.tenants import TenantSpec


def sigmoid(x):
    return 1 / (1 + np.exp(-x))


def _period_end_after(a: Account, day: date) -> date:
    step = 12 if a.interval == "year" else 1
    k = max(1, ((day.year - a.start.year) * 12 + day.month - a.start.month) // step)
    while add_months(a.start, k * step) <= day:
        k += 1
    return add_months(a.start, k * step)


def _cause(slug: str, a: Account, shocks: list[Shock], dw: int, s: int,
           rng: np.random.Generator) -> str:
    """The shock behind an h-driven churn if one is recent, else a generic loss reason."""
    recent = {x.kind for x in shocks if dw - 26 <= x.week <= dw}
    for kind, reason in PARAMS["shock_reasons"][slug].items():
        if kind in recent:
            return reason
    reasons = PARAMS["loss_reasons"][slug]
    if a.onboarding_failed and dw - s < 60 and "onboarding" in " ".join(reasons):
        return next(r for r in reasons if r.startswith("onboarding"))
    return reasons[rng.integers(len(reasons))]


def decide(spec: TenantSpec, accounts: list[Account], starts: np.ndarray, h: np.ndarray,
           weeks: list[date], shocks: list[Shock], rng: np.random.Generator):
    """Renewal decisions, voluntary churn and involuntary (payment) churn for every account."""
    slug, tz, n_weeks = spec.slug, spec.timezone, len(weeks)
    ch, uf, pay = PARAMS["churn"][slug], PARAMS["unforecastable"], PARAMS["payments"]
    a_m, b_m = ch.get("month_to_month", (0.0, 0.0))
    with np.errstate(invalid="ignore"):
        m2m_hits = rng.random(h.shape) < sigmoid(a_m + b_m * h)  # NaN (not started) -> False
    by_account: dict[int, list[Shock]] = {}
    for x in shocks:
        by_account.setdefault(x.account, []).append(x)
    modes, mode_p = list(PARAMS["notice_modes"]), list(PARAMS["notice_modes"].values())
    uf_causes = [c for c in uf["causes"] if slug == "hearthline" or c != "brokerage_switch"]
    decisions, failures = [], []
    for i, (a, s) in enumerate(zip(accounts, starts, strict=True)):
        mine, rows, churn = by_account.get(i, []), [], None
        u_week = n_weeks if a.vp_case else s + int(rng.geometric(uf["rate"][slug] / 52))
        if a.contract:
            alpha, beta = ch[a.billing]
            for k in itertools.count(1):
                term_end = add_months(a.start, 12 * k)
                day = term_end - timedelta(days=35 if a.vp_case else int(
                    rng.integers(PARAMS["notice_days"][0], PARAMS["notice_days"][1] + 1)))
                dw = (day - weeks[0]).days // 7
                if dw >= min(u_week, n_weeks):
                    break
                p = float(sigmoid(alpha + beta * np.mean(h[max(s, dw - 8) : dw, i])))
                churned = term_end in VP_TERM_ENDS if a.vp_case else rng.random() < p
                row = {"decided_at": local_dt(day, 10 + 7 * rng.random(), tz),
                       "term_end": term_end, "effective_at": local_dt(term_end, 15, tz),
                       "p_churn": p}
                if churned:  # the VP cases are forced to churn here (design §3.3)
                    churn = row | {"cause": _cause(slug, a, mine, dw, s, rng),
                                   "unforecastable": False}
                    break
                rows.append(row | {"kind": "renew", "save": bool(rng.random() <
                                                                  PARAMS["save_share"] * p)})
        else:
            hit = np.flatnonzero(m2m_hits[s + 1 :, i])
            if hit.size and s + 1 + hit[0] < u_week:
                dw = s + 1 + int(hit[0])
                day = weeks[dw] + timedelta(days=int(rng.integers(5)))
                churn = {"decided_at": local_dt(day, 10 + 7 * rng.random(), tz),
                         "cause": _cause(slug, a, mine, dw, s, rng), "unforecastable": False}
        if churn is None and u_week < n_weeks:
            day = weeks[u_week] + timedelta(days=int(rng.integers(5)))
            churn = {"decided_at": local_dt(day, 10 + 7 * rng.random(), tz),
                     "cause": uf_causes[rng.integers(len(uf_causes))], "unforecastable": True}
        if churn is not None:
            day = churn["decided_at"].date()
            end = churn.get("term_end") or _period_end_after(a, day)
            churn |= {"kind": "churn", "term_end": end,
                      "effective_at": local_dt(end, 15, tz), "involuntary": False,
                      "mode": "stripe_cancel" if a.vp_case else modes[rng.choice(3, p=mode_p)]}
        if a.interval == "month":  # card payers: Smart Retries, then cancellation
            limit = churn["decided_at"].date() if churn else weeks[-1]
            for m in itertools.count(1):
                inv = add_months(a.start, m)
                if inv >= limit:
                    break
                if rng.random() >= pay["fail_monthly"]:
                    continue
                recovered = bool(rng.random() < pay["recover"])
                inv_at = local_dt(inv, 15, tz)
                failures.append(PaymentFailure(a.sf_account_id, inv_at, int(
                    rng.integers(2, pay["max_attempts"] + 1)) if recovered
                    else pay["max_attempts"], recovered))
                if not recovered:
                    end_at = inv_at + timedelta(days=pay["retry_days"])
                    churn = {"kind": "churn", "decided_at": end_at, "effective_at": end_at,
                             "term_end": None, "cause": "payment_failed", "involuntary": True,
                             "unforecastable": False, "mode": "dunning"}
                    rows = [r for r in rows if r["decided_at"] < end_at]
                    break
        for r in rows + ([churn] if churn else []):
            decisions.append({"sf_account_id": a.sf_account_id, "involuntary": False,
                              "save": False, "mode": None, "cause": None,
                              "unforecastable": False, "p_churn": np.nan} | r)
    return pd.DataFrame(decisions), failures
