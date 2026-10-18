"""Print the README's generator table from radar/sim/params.py (knob, value, meaning):

    uv run python scripts/params_table.py

tests/test_docs.py checks that README.md holds this output verbatim, so the table cannot
drift from the knobs the simulator actually reads. A number in a meaning is written as
{path} and printed from PARAMS, so no value is typed twice."""

import re

from radar.sim.params import PARAMS

ROWS = [
    ("sim_start", "first simulated week (a Monday)"),
    ("latent.phi", "weekly persistence of latent health h"),
    ("latent.sigma", "weekly noise on h"),
    ("latent.mu_mean", "mean of each account's long-run health"),
    ("latent.mu_sd", "spread of long-run health across accounts"),
    ("onboarding.ramp_weeks", "weeks for a new account's health to ramp up"),
    ("onboarding.fail_rate", "share of accounts that fail onboarding"),
    ("onboarding.fail_offset", "health offset for a failed onboarding"),
    ("shocks.champion_departure.rate", "annual rate: the champion leaves"),
    ("shocks.champion_departure.effect", "health effect of a champion leaving"),
    ("shocks.integration_break.rate", "annual rate: the CRM integration breaks"),
    ("shocks.integration_break.repair_share", "share of breaks that get repaired"),
    ("shocks.budget_cut.rate", "annual rate: budget cut, seats cut"),
    ("shocks.budget_cut.seat_cut", "share of seats removed by a budget cut"),
    ("shocks.competitor_eval.rate", "annual rate: a slow slide during a competitor trial"),
    ("shocks.competitor_eval.drift", "health drift per week while evaluating"),
    ("shocks.bug_episode.rate", "annual rate: a product bug episode with urgent tickets"),
    ("shocks.expansion.rate", "annual rate: expansion, seats added"),
    ("outage.hearthline", "hearthline's tenant-wide outage week"),
    ("outage.tallyworks", "tallyworks' tenant-wide outage week"),
    ("outage.account_share", "share of accounts that file outage tickets"),
    ("churn.hearthline.annual", "(alpha, beta): P(churn at renewal) = sigmoid(a + b * mean h)"),
    ("churn.tallyworks.month_to_month", "(alpha, beta): weekly hazard for month-to-month"),
    ("notice_days", "days before term end that a contract account decides"),
    ("unforecastable.rate", "annual rate of churn drawn independently of health"),
    ("notice_modes", "how a decision first shows up"),
    ("save_share", "a renewer schedules a cancel, then reverses it, with p = this x P(churn)"),
    ("loss_reason_noise", "Salesforce loss reason differs from the true cause"),
    ("payments.fail_monthly", "monthly card payment failure rate"),
    ("payments.recover", "share of failures Smart Retries recovers"),
    ("telemetry_share", "share of accounts with a product workspace"),
    ("personal_email_share", "contacts on a personal email domain"),
    ("merge_share", "accounts that get a duplicate Salesforce record, merged later"),
    ("tickets.silent_share",
     "accounts that file fewer tickets of every kind (x {tickets.silent_factor})"),
    ("email.suppress_days", "days the CSM's outbound mail stops for the VP's two cases"),
    ("feed.stripe.metadata", "Stripe customers carrying a Salesforce id"),
    ("feed.stripe.messy_id", "of those, ids in the short form, lowercase or padded"),
    ("feed.stripe.tail",
     "Stripe events delivered late (backoff tail, mean {feed.stripe.tail_mean_hours} h)"),
    ("feed.stripe.redeliver", "Stripe events delivered twice with the same id"),
    ("feed.stripe.twin", "Stripe semantic twins (new id, same state)"),
    ("feed.zendesk.org_external_id", "Zendesk organisations carrying a Salesforce id"),
    ("feed.salesforce.reexport", "Salesforce rows exported again unchanged"),
    ("feed.gmail.date_skew", "Gmail Date headers with a wrong offset"),
    ("feed.product.sf_account_id", "usage rows carrying a Salesforce id"),
    ("feed.product.late_batch", "monthly usage batches delivered days late"),
    ("feed.product.restate", "usage rows restated with a higher version"),
    ("feed.product.future", "product events with a future timestamp (device clock)"),
]


def _get(path: str):
    value = PARAMS
    for key in path.split("."):
        value = value[key]
    return value


def _show(value) -> str:
    if isinstance(value, dict):
        return ", ".join(f"{k} {_show(v)}" for k, v in value.items())
    if isinstance(value, tuple):
        return "(" + ", ".join(_show(v) for v in value) + ")"
    return str(value)


def _fill(meaning: str) -> str:
    """The meaning with each {path} replaced by that knob's value."""
    return re.sub(r"\{([\w.]+)\}", lambda m: _show(_get(m.group(1))), meaning)


def table() -> str:
    lines = ["| knob | value | meaning |", "|---|---|---|"]
    lines += [f"| `{path}` | {_show(_get(path))} | {_fill(meaning)} |" for path, meaning in ROWS]
    return "\n".join(lines)


if __name__ == "__main__":
    print(table())
