"""Stripe snapshot Events for customers, subscriptions and invoices (design §3.5)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

import numpy as np

from radar.sim import delivery
from radar.sim.delivery import epoch
from radar.sim.model import Account, BillingEvent, add_months, local_dt
from radar.sim.names import PERSONAL_DOMAINS, token
from radar.sim.params import PARAMS

if TYPE_CHECKING:
    from radar.sim.world import World

PRE_BASIL, BASIL = "2024-06-20", "2025-03-31.basil"
UPGRADED = datetime(2025, 6, 1, tzinfo=UTC)  # hearthline and tallyworks move to basil
HEX = "0123456789abcdef"
FRANCHISE_DOMAINS =["kw.com", "remax.net", "compass.com", "exprealty.com"]
TYPES = {"sub_created": "customer.subscription.created",
         "sub_deleted": "customer.subscription.deleted",
         "invoice_paid": "invoice.paid", "invoice_failed": "invoice.payment_failed"}
UPDATED = "customer.subscription.updated"  # seat changes, cancels, reversals, renewals
# made by a person or the API; renewals, dunning and period-end deletions are automatic
REQUESTED = {"customer_created", "sub_created", "seat_change", "cancel_scheduled",
             "cancel_reversed"}


def api_version(slug: str, at: datetime) -> str:
    if slug == "kitewire":
        return "2026-03-25.dahlia"
    return BASIL if at >= UPGRADED else PRE_BASIL


def emit(world: World, rng: np.random.Generator) -> tuple[list[dict], set[tuple[str, str]]]:
    """Feed lines and the identity truth of every `stripe:` customer handle."""
    start, tz = delivery.source_start(world, "stripe"), world.spec.timezone
    sim_end = datetime.combine(world.sim_end, datetime.max.time(), UTC)
    accounts = {a.sf_account_id: a for a in world.accounts}
    fixed = PARAMS["feed"]["stripe"]["metadata_fixed"]
    subs: dict[str, dict] = {}  # created, scheduled (cancel request time), last (b, scheduled)
    events, handles = [], set()

    def add(at: datetime, kind: str, obj: dict, prev: dict | None, requested: bool) -> None:
        req = ({"id": "req_" + token(rng, 14), "idempotency_key": token(rng, 32, HEX)}
               if requested else {"id": None, "idempotency_key": None})
        data = {"object": obj} | ({"previous_attributes": prev} if prev is not None else {})
        events.append({"id": "evt_" + token(rng, 24), "object": "event",
                       "api_version": api_version(world.slug, at), "created": epoch(at),
                       "type": kind, "data": data, "livemode": True, "pending_webhooks": 1,
                       "request": req})

    for b in world.billing:
        at, a = max(b.at, start), accounts[b.sf_account_id]
        ver = api_version(world.slug, at)
        if b.kind == "customer_created":
            customer = _customer(world, a, at, rng)
            add(at, "customer.created", customer, None, True)
            handles.add(("stripe:" + b.customer_id, b.sf_account_id))
            later = at + timedelta(days=float(rng.uniform(14, 180)))
            if not customer["metadata"] and rng.random() < fixed and later <= sim_end:
                add(later, "customer.updated", customer | {
                    "metadata": {"sf_account_id": a.sf_account_id}}, {"metadata": {}}, True)
            continue
        st = subs.setdefault(b.subscription_id, {"created": epoch(at), "scheduled": None,
                                                 "last": None})
        if b.kind == "cancel_scheduled" or (b.kind == "sub_created"
                                            and (b.cancel_at_period_end or b.cancel_at)):
            st["scheduled"] = at
        elif b.kind == "cancel_reversed":
            st["scheduled"] = None
        if b.kind.startswith("invoice"):
            if st["last"] and b.status != st["last"][0].status:  # dunning moves the sub
                _sub_event(add, a, st, b, at, ver, tz, UPDATED, False)
            add(at, TYPES[b.kind], _invoice(b, a, ver, tz), None, False)
        else:
            _sub_event(add, a, st, b, at, ver, tz, TYPES.get(b.kind, UPDATED),
                       b.kind in REQUESTED)
    return delivery.stripe(world, events, rng), handles


def _sub_event(add, a: Account, st: dict, b: BillingEvent, at: datetime, ver: str, tz: str,
               kind: str, requested: bool) -> None:
    obj = _subscription(b, a, at, st["created"], st["scheduled"], ver, tz)
    prev = None
    if kind == UPDATED and st["last"]:
        old_b, old_scheduled = st["last"]
        old = _subscription(old_b, a, old_b.at, st["created"], old_scheduled, ver, tz)
        prev = {k: v for k, v in old.items() if obj[k] != v}  # whole `items` if any changed
    add(at, kind, obj, prev, requested)
    st["last"] = (b, st["scheduled"])


def _customer(world: World, a: Account, at: datetime, rng: np.random.Generator) -> dict:
    """~85% carry metadata.sf_account_id (sometimes mangled or under `salesforce_id`); the
    rest are matched by email domain, unless the billing email is free-mail or franchise."""
    p = PARAMS["feed"]["stripe"]
    domain = a.domain
    if rng.random() < p["free_mail"]:
        pool = PERSONAL_DOMAINS + (FRANCHISE_DOMAINS if world.slug == "hearthline" else [])
        domain = pool[rng.integers(len(pool))]
    user = "billing" if domain == a.domain else "".join(c for c in a.name.lower()
                                                         if c.isalnum())[:14]
    metadata = {}
    if rng.random() < p["metadata"]:
        key = "salesforce_id" if rng.random() < p["salesforce_id_key"] else "sf_account_id"
        metadata = {key: delivery.messy_id(a.sf_account_id, rng, p["messy_id"])}
    return {"id": a.stripe_customer_id, "object": "customer", "email": f"{user}@{domain}",
            "name": a.name, "currency": world.spec.currency.lower(), "created": epoch(at),
            "metadata": metadata, "livemode": True}


def _subscription(b: BillingEvent, a: Account, at: datetime, created: int,
                  scheduled: datetime | None, ver: str, tz: str) -> dict:
    """Pre-basil versions put current_period_end on the subscription, later ones on items."""
    ended = b.kind == "sub_deleted"
    item = {"id": "si_" + b.subscription_id[4:18], "object": "subscription_item",
            "price": {"id": f"price_{b.plan}_{b.interval}ly", "object": "price",
                      "unit_amount": b.unit_amount, "currency": b.currency,
                      "recurring": {"interval": b.interval, "interval_count": 1}},
            "quantity": b.quantity}
    obj = {"id": b.subscription_id, "object": "subscription", "customer": b.customer_id,
           "status": b.status, "created": created, "start_date": epoch(local_dt(a.start, 15, tz)),
           "cancel_at_period_end": b.cancel_at_period_end, "cancel_at": epoch(b.cancel_at),
           # canceled_at is the cancel request time, even while the sub is still active
           "canceled_at": epoch(scheduled or (at if ended else None)),
           "ended_at": epoch(at) if ended else None,
           "cancellation_details": {"reason": b.cancellation_reason or (
               "cancellation_requested" if scheduled else None)},
           "items": {"object": "list", "data": [item]}}
    end = epoch(_period_end(b, at, tz))
    if ver == PRE_BASIL:
        obj["current_period_end"] = end
    else:
        item["current_period_end"] = end
    return obj


def _period_end(b: BillingEvent, at: datetime, tz: str) -> datetime:
    """2a's initial-load snapshot keeps the period end of its last pre-start fact, and monthly
    subs have no renewal facts, so roll forward one interval at a time (the same boundaries
    as the invoice.paid line periods) until the period contains `at`."""
    end = b.current_period_end
    while end < at:
        end = local_dt(add_months(end.astimezone(ZoneInfo(tz)).date(),
                                  12 if b.interval == "year" else 1), 15, tz)
    return end


def _invoice(b: BillingEvent, a: Account, ver: str, tz: str) -> dict:
    """The line period is the one the invoice pays for. Monthly subscriptions emit no renewal
    event, so their current_period_end is rolled forward from invoice.paid lines."""
    paid = b.kind == "invoice_paid"
    end = b.current_period_end.astimezone(ZoneInfo(tz)).date()
    period_start = local_dt(add_months(end, -12 if b.interval == "year" else -1), 15, tz)
    first = period_start == local_dt(a.start, 15, tz)
    inv = {"id": b.invoice_id, "object": "invoice", "customer": b.customer_id,
           "status": "paid" if paid else "open", "amount_due": b.amount_due,
           "amount_paid": b.amount_due if paid else 0, "currency": b.currency,
           "attempt_count": b.attempt_count,
           "next_payment_attempt": epoch(b.next_payment_attempt),
           "billing_reason": "subscription_create" if first else "subscription_cycle",
           "lines": {"object": "list", "data": [{"period": {
               "start": epoch(period_start), "end": epoch(b.current_period_end)}}]}}
    if ver == PRE_BASIL:
        inv |= {"subscription": b.subscription_id, "paid": paid}
    else:
        inv["parent"] = {"type": "subscription_details",
                         "subscription_details": {"subscription": b.subscription_id}}
    return inv
