"""The data dictionary (design §5): one versioned definition per feature, metric and rule.

Definitions name their implementation by dotted path ("module:function"); manifest() imports
it lazily, so this module imports nothing of ours and every other module may import it.
"""

import hashlib
import importlib
import inspect
import sys
from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class Definition:
    name: str
    version: int
    kind: str  # behavioural | structural | coverage | metric | rule | banned
    source: str
    unit: str
    risk_direction: int | None  # +1: a higher value raises risk; None: no sign gate
    description: str
    fn: str | None = None  # "module:function"
    banned_reason: str | None = None


def _f(name, kind, source, unit, risk, description, version=1):
    return Definition(name, version, kind, source, unit, risk, description,
                      f"radar.features:{name}")


FEATURES = [
    _f("usage_drop", "behavioural", "product", "log ratio", +1,
       "max(0, -(log((active_users in the last complete month + 1) / (mean of the 3 prior"
       " complete months + 1)) minus the tenant median of the same at T)); growth reads 0;"
       " NaN with < 4 complete months", version=2),
    _f("seat_utilization", "behavioural", "product+billing", "ratio", -1,
       "mean active_users over the last 3 complete months (those reported) / seats of the"
       " active subscription at T", version=2),
    _f("integration_broken", "behavioural", "product", "0/1", +1,
       "1 if the latest integration event leaves the account in sync_error/disconnected and"
       " it has been broken for >= 7 days"),
    _f("not_live_after_30d", "behavioural", "product", "0/1", +1,
       "1 if the contract started on or after the product source start, tenure >= 30 days,"
       " and no workspace_went_live is visible"),
    _f("open_urgent_tickets", "behavioural", "zendesk", "log1p(count)", +1,
       "log1p(tickets whose current version is new/open/pending/hold and high/urgent)"),
    _f("support_friction_90d", "behavioural", "zendesk", "log1p(count)", +1,
       "log1p(tickets with a version in (T-90d, T] rated bad or reopened after solved)"),
    _f("days_since_customer_email", "behavioural", "gmail", "days", +1,
       "min(120, days from the last inbound non-auto-reply email to the Gmail watermark);"
       " 120 if none since Gmail was connected"),
    _f("seat_change_180d", "behavioural", "billing", "ratio", -1,
       "(seats at T - seats at T-180d) / seats at T-180d, clipped to [-1, 1]"),
    _f("log_arr", "structural", "billing", "ln(major units)", None,
       "ln(arr v1) in tenant currency, major units"),
    _f("tenure_years", "structural", "crm", "log1p(years)", None,
       "log1p(years since the first contract start)"),
    _f("term_end_within_60d", "structural", "crm+billing", "0/1", None,
       "1 if term_end v1 is <= 60 days away; month-to-month is always 1"),
    _f("billing_month_to_month", "structural", "billing", "0/1", None,
       "1 if the account has no contract term"),
    _f("no_telemetry", "coverage", "product", "0/1", None,
       "1 if no workspace is linked at T; product features are then imputed by the model"),
]
FEATURE_NAMES = [d.name for d in FEATURES]


def _m(name, description, fn=None, kind="metric", source="derived", unit=""):
    return Definition(name, 1, kind, source, unit, None, description, fn)


METRICS = [
    _m("arr", "Sum over active Stripe subscription items of unit_amount * quantity * 12 /"
       " months_per_interval as of T; SF ARR__c as of T only for accounts with no Stripe link"
       " (arr_source=salesforce)", "radar.features:arr", unit="minor units"),
    _m("term_end", "Yearly subscriptions: Stripe current_period_end; 12-month contracts paid"
       " monthly: SF Renewal_Date__c as of T; month-to-month: the current period end",
       "radar.features:term_end", unit="timestamp"),
    _m("completeness_watermark", "Product: the latest month reported by >= 90% of the linked"
       " workspaces that reported the previous month (churned accounts' workspaces stay linked"
       " but stop reporting, so they leave the denominator); other sources: the max visible"
       " available_at",
       "radar.features:watermarks", unit="timestamp"),
    _m("eligible", "Active subscription, not test, not merged, not in open dunning, and"
       " month-to-month or term end <= eligibility_window_days (150) away; training also"
       " needs every source connected 120 days before T", "radar.features:eligible",
       unit="bool"),
    _m("coverage", "billing plus at least one of: product telemetry, a Zendesk ticket in 365"
       " days, an inbound email in 180 days", "radar.features:coverage_ok", unit="bool"),
    _m("churn_decision", "Earliest of: a Stripe cancellation announced and not reversed, the"
       " first Closed Lost row on a renewal opportunity, a cancellation_request ticket, or the"
       " subscription ending without notice; not merges, involuntary churn or reactivation"
       " within 30 days (hindsight, labels only; radar/labels.py, Task 5)"),
    _m("churn_label_90d", "1 if a churn decision falls in (T, T+90d]; excluded if already"
       " visible at T, involuntary or merged in the window, or censored at the label cutoff"
       " (radar/labels.py, Task 5)"),
    _m("risk_band", "Display only: red (open must-act or lift >= 3), yellow (lift >= 1.5),"
       " green, unknown (coverage fails), not_in_window (radar/policy.py, Task 7)"),
    _m("worklist", "Per CSM per ISO week: open must-acts, up to 2 renewal blind-spot"
       " check-ins, then model slots by p * arr up to K (radar/policy.py, Task 7)"),
    _m("play", "Must-act kind -> its play; model slots by the source family of the top"
       " behavioural contribution (radar/policy.py, Task 7)"),
    _m("suppression", "A reasoned dismissal holds the account out of model slots until p rises"
       " >= 0.10 or a must-act fires (radar/policy.py, Task 7)"),
    _m("circuit_breaker", "New must-acts in 24 h above max(10, 3 x trailing 28-day daily"
       " median) open as held with one incident (radar/policy.py, Task 7)"),
    _m("source_freshness", "Tenant config freshness_hours (Stripe, Zendesk, Gmail 24 h;"
       " Salesforce 26 h); product the monthly batch due on the 3rd + 3 days; checked hourly"
       " (radar/agent/handlers.py, Task 9, Ruling AH)"),
    _m("rules_score", "2*integration_broken + 1.5*(open_urgent_tickets>0) +"
       " 1*(support_friction_90d>0) + 1*(days_since_customer_email>=60) + 1.5*(usage_drop>0.3)"
       " + 1*(seat_change_180d<-0.1) + 1*not_live_after_30d; a missing feature counts as no"
       " signal. The rules baseline in evaluation and kitewire's cold-start ranking",
       "radar.registry:rules_score", unit="points"),
]


def rules_score(df):
    """rules_score v1 on a frame of model features (one row per account snapshot)."""
    return (2 * (df["integration_broken"] > 0) + 1.5 * (df["open_urgent_tickets"] > 0)
            + 1 * (df["support_friction_90d"] > 0) + 1 * (df["days_since_customer_email"] >= 60)
            + 1.5 * (df["usage_drop"] > 0.3) + 1 * (df["seat_change_180d"] < -0.1)
            + 1 * (df["not_live_after_30d"] > 0))


def _r(name, source, description):
    return Definition(name, 1, "rule", source, "bool", None, description,
                      f"radar.features:{name}")


RULES = [
    _r("cancel_announced", "billing+zendesk", "A subscription active/past_due/trialing with"
       " cancel_at_period_end or cancel_at set, or an open ticket tagged cancellation_request."
       " SLA 1 business day; play save_call"),
    _r("dunning_exhausted", "billing", "An open invoice with attempt_count >= 3 or no next"
       " attempt after a failure, or past_due for more than 7 days. SLA 2; play"
       " billing_outreach"),
    _r("champion_bounced", "gmail+crm", "A hard bounce (5.1.1/5.2.1) for a champion contact"
       " in the last 30 days. SLA 5; play find_new_champion (internal, no customer draft)"),
    _r("urgent_ticket_stalled", "zendesk", "An urgent or high ticket open for more than 3"
       " days. SLA 2; play support_escalation"),
]


def _b(name, source, reason):
    return Definition(name, 1, "banned", source, "", None, f"Not a model input: {reason}",
                      banned_reason=reason)


BANNED = [
    _b("renewal_opportunity_stage", "crm", "human judgement, often written after the outcome"),
    _b("forecast_category", "crm", "human judgement, often written after the outcome"),
    _b("close_date", "crm", "human judgement, often written after the outcome"),
    _b("loss_reason", "crm", "written after the outcome"),
    _b("sf_type_current", "crm", "post-churn cleanup sets Type, so current state leaks the"
       " label"),
    _b("sf_arr_current", "crm", "post-churn cleanup zeroes ARR__c, so current state leaks the"
       " label"),
    _b("csm_outbound_email_recency", "gmail", "CSM-controlled: the model would learn our own"
       " policy (shown as context instead)"),
    _b("owner_changes", "crm", "they often follow the churn decision"),
    _b("llm_output", "llm", "anything an LLM wrote is never a model input"),
]


def impl(d: Definition) -> Callable:
    module, _, name = d.fn.partition(":")
    return getattr(importlib.import_module(module), name)


_OURS = ("radar.features", "radar.asof")
_PLAIN = (int, float, str, list, tuple, set, frozenset)  # not dicts: TENANTS is config


def _names(code) -> set[str]:
    return set(code.co_names).union(*(_names(c) for c in code.co_consts if inspect.iscode(c)))


def _closure_source(fn: Callable) -> str:
    """fn's source plus every radar.features/radar.asof function or class it reaches through
    globals or annotations, and the plain constants they read, so editing a helper changes
    the hash too (design §5: the scorer fails closed on any semantic change)."""
    parts: dict[str, str] = {}
    todo = [fn]
    while todo:
        obj = todo.pop()
        key = f"{obj.__module__}.{obj.__qualname__}"
        if key in parts:
            continue
        parts[key] = inspect.getsource(obj)
        g = vars(sys.modules[obj.__module__])
        fns = ([v for v in vars(obj).values() if inspect.isfunction(v)]
               if inspect.isclass(obj) else [obj])
        for f in fns:
            todo += [v for v in f.__annotations__.values() if _ours(v)]
            for n in _names(f.__code__) & g.keys():
                v = g[n]
                if _ours(v):
                    todo.append(v)
                elif isinstance(v, _PLAIN):
                    plain = sorted(v) if isinstance(v, set | frozenset) else v
                    parts[f"{obj.__module__}:{n}"] = repr(plain)
    return "\n".join(parts[k] for k in sorted(parts))


def _ours(v) -> bool:
    return (inspect.isfunction(v) or inspect.isclass(v)) and v.__module__ in _OURS


def manifest() -> dict[str, str]:
    """name -> "v<version>:<sha256 of the implementation's source closure, 12 hex>"."""
    return {d.name: f"v{d.version}:"
            + hashlib.sha256(_closure_source(impl(d)).encode()).hexdigest()[:12]
            for d in FEATURES + METRICS + RULES if d.fn}


def render_dictionary_md() -> str:
    """docs/data-dictionary.md and the dictionary page are generated from this."""
    out = ["# Data dictionary", "", "Generated from `radar/registry.py`.", ""]
    hashes = manifest()
    for title, defs in (("Model features", FEATURES), ("Metrics", METRICS),
                        ("Must-act rules", RULES), ("Banned inputs", BANNED)):
        out += [f"## {title}", "", "| name | version | kind | source | unit | risk | definition"
                " | implementation |", "|---|---|---|---|---|---|---|---|"]
        for d in defs:
            risk = "" if d.risk_direction is None else f"{d.risk_direction:+d}"
            code = f"`{d.fn}` {hashes[d.name]}" if d.fn else ""
            out.append(f"| {d.name} | {d.version} | {d.kind} | {d.source} | {d.unit} | {risk}"
                       f" | {d.description} | {code} |")
        out.append("")
    return "\n".join(out)
