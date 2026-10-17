"""Stripe-level billing facts derived from accounts, seat shocks and decisions."""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from dataclasses import replace
from datetime import UTC, datetime, time, timedelta
from typing import TYPE_CHECKING

import numpy as np

from radar.sim.model import Account, BillingEvent, add_months, clip_to_start, local_dt
from radar.sim.names import token
from radar.sim.params import PARAMS

if TYPE_CHECKING:
    from radar.sim.world import World

ATTEMPT_GAP = timedelta(hours=PARAMS["payments"]["attempt_gap_hours"])


def billing_events(world: World, rng: np.random.Generator) -> list[BillingEvent]:
    sim_end = datetime.combine(world.sim_end, time.max, UTC)
    decisions = {k: g.to_dict("records") for k, g in world.decisions.groupby("sf_account_id")}
    fails: dict[str, list] = {}
    for f in world.payment_failures:
        fails.setdefault(f.sf_account_id, []).append(f)
    seat_shocks: dict[int, list] = {}
    for s in world.shocks:
        if s.kind in ("budget_cut", "expansion"):
            seat_shocks.setdefault(s.account, []).append(s)
    out = []
    for i, a in enumerate(world.accounts):
        key = a.sf_account_id
        out += _account_events(world, a, decisions.get(key, []), fails.get(key, []),
                               seat_shocks.get(i, []), sim_end, rng)
    return clip_to_start(out, world.spec.source_start["stripe"], _entity, _initial_load)


def _entity(e: BillingEvent):
    return None if e.kind.startswith("invoice") else (e.kind == "customer_created", e.customer_id)


def _initial_load(e: BillingEvent, at: datetime) -> BillingEvent | None:
    """Subscriptions alive at the source start appear as created then (billing migration)."""
    if e.kind == "sub_deleted":
        return None
    return replace(e, at=at, kind="customer_created" if e.kind == "customer_created"
                   else "sub_created")


def _account_events(world: World, a: Account, rows: list[dict], fails: list,
                    seat_shocks: list, sim_end: datetime, rng: np.random.Generator):
    tz = world.spec.timezone
    churn = next((r for r in rows if r["kind"] == "churn"), None)
    end = min(churn["effective_at"], sim_end) if churn else sim_end
    t0 = local_dt(a.start, 15, tz)
    step = 12 if a.interval == "year" else 1
    boundaries = []
    for k in range(10_000):
        b = local_dt(add_months(a.start, k * step), 15, tz)
        boundaries.append(b)
        if b > end:
            break

    seats = [(t0, a.seats)]
    for s in seat_shocks:
        at = local_dt(world.weeks[s.week] + timedelta(days=int(rng.integers(5))), 11, tz)
        if t0 < at < end:
            q = seats[-1][1]
            d = max(1, round(q * PARAMS["shocks"][s.kind].get("seat_cut", 0)
                             + q * PARAMS["shocks"][s.kind].get("seat_add", 0)))
            seats.append((at, max(1, q - d) if s.kind == "budget_cut" else q + d))

    # (scheduled_at, reversed_at or None, cancel_at or None for cancel_at_period_end).
    # A 12-month contract billed monthly must use cancel_at: its period end is next month.
    cancels = []
    for r in rows:
        saved = r["kind"] == "renew" and r["save"]
        if saved or (r["kind"] == "churn" and r["mode"] == "stripe_cancel"):
            at = r["decided_at"] + timedelta(hours=float(rng.uniform(0.2, 30)))
            back = at + timedelta(days=int(rng.integers(7, 22))) if saved else None
            use_at = a.billing == "monthly_on_contract" or rng.random() < PARAMS["cancel_at_share"]
            cancels.append((at, back, r["effective_at"] if use_at else None))
    past_due = []  # (from, to): Smart Retries running, or until the unrecovered sub ends
    for f in fails:
        lo = f.invoice_at + timedelta(hours=1)
        past_due.append((lo, lo + ATTEMPT_GAP * (f.attempts - 1) if f.recovered
                         else f.invoice_at + timedelta(days=PARAMS["payments"]["retry_days"])))

    def event(kind: str, at: datetime, **kw) -> BillingEvent:
        qty = next(q for t, q in reversed(seats) if t <= at) if at >= t0 else a.seats
        # a deletion at a period boundary ends that period; anything else is inside one
        nxt = (bisect_left if kind == "sub_deleted" else bisect_right)(boundaries, at)
        pend = boundaries[min(nxt, len(boundaries) - 1)]
        sched = [c for c in cancels if c[0] <= at and (c[1] is None or at < c[1])]
        status = "past_due" if any(lo <= at < hi for lo, hi in past_due) else "active"
        return replace(BillingEvent(
            at=at, kind=kind, sf_account_id=a.sf_account_id, customer_id=a.stripe_customer_id,
            subscription_id=a.stripe_subscription_id, plan=a.plan, interval=a.interval,
            unit_amount=a.unit_amount, currency=world.spec.currency.lower(), quantity=qty,
            status=status, current_period_end=pend,
            cancel_at_period_end=bool(sched) and sched[0][2] is None,
            cancel_at=sched[0][2] if sched else None,
        ), **kw)

    acct_events = [event("customer_created", t0), event("sub_created", t0)]
    acct_events += [event("seat_change", at) for at, _ in seats[1:]]
    for at, back, _ in cancels:
        acct_events.append(event("cancel_scheduled", at))
        if back is not None and back < end:
            acct_events.append(event("cancel_reversed", back))
    acct_events += _invoices(a, boundaries, end, fails, event, rng)
    if churn and churn["effective_at"] <= sim_end:
        reason = "payment_failed" if churn["involuntary"] else "cancellation_requested"
        acct_events.append(event("sub_deleted", churn["effective_at"], status="canceled",
                                 cancellation_reason=reason))
    return acct_events


def _invoices(a: Account, boundaries: list[datetime], end: datetime, fails: list, event,
              rng: np.random.Generator) -> list[BillingEvent]:
    """One invoice per period; failed ones retry (Smart Retries) and recover or end the sub."""
    out = []
    failed = {f.invoice_at: f for f in fails}
    for k, b in enumerate(boundaries):
        if b >= end:
            break
        if k and a.interval == "year":
            # shortcut: monthly period rolls are not emitted (their invoice.paid stands in);
            # add them if a feature ever needs current_period_end changes on monthly subs
            out.append(event("period_renewed", b))
        inv = {"invoice_id": "in_" + token(rng, 24),
               "amount_due": a.unit_amount * event("invoice_paid", b).quantity}
        f = failed.get(b)
        n_failed = (f.attempts - 1 if f.recovered else f.attempts) if f else 0
        for j in range(n_failed):
            at = b + timedelta(hours=1) + ATTEMPT_GAP * j
            nxt = at + ATTEMPT_GAP if (f.recovered or j + 1 < f.attempts) else None
            out.append(event("invoice_failed", at, attempt_count=j + 1,
                             next_payment_attempt=nxt, **inv))
        if not f or f.recovered:
            at = b + timedelta(hours=1) + ATTEMPT_GAP * n_failed
            out.append(event("invoice_paid", at, attempt_count=n_failed + 1, **inv))
    return out
