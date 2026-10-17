"""Knowledge times (deliver_at) per source and the delivery mess (design §3.5).

Emitters build native bodies with their source timestamps; these functions decide when Radar
could first have seen each one: Stripe webhooks within seconds (with a backoff tail,
re-deliveries and semantic twins), Zendesk export polls, the nightly Salesforce batch, the
15-minute Gmail sync and the monthly product batch.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
from datetime import UTC, date, datetime, time, timedelta
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

import numpy as np

from radar.sim.model import local_dt
from radar.sim.names import token
from radar.sim.params import PARAMS

if TYPE_CHECKING:
    from radar.sim.world import World

QUARTER_HOUR = 900


def iso(at: datetime) -> str:
    return at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def iso_ms(at: datetime) -> str:
    return at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.") + f"{at.microsecond // 1000:03d}Z"


def epoch(at: datetime | None) -> int | None:
    return None if at is None else int(at.timestamp())


def source_start(world: World, source: str) -> datetime:
    """Tenant-local midnight of the source's first day: nothing is dated before it."""
    return local_dt(world.spec.source_start[source], 0, world.spec.timezone)


def pick(rng: np.random.Generator, n: int, rate: float) -> set[int]:
    """Indices of about `rate * n` records, at least one, so rare mess is always planted."""
    if n == 0:
        return set()
    return set(rng.choice(n, size=min(n, max(1, round(n * rate))), replace=False).tolist())


def messy_id(sf_id: str, rng: np.random.Generator, rate: float) -> str:
    """A Salesforce id as people type it into other tools: sometimes 15-char, lowercase or
    padded with whitespace. Each variant is still recoverable (design §4.2)."""
    if rng.random() >= rate:
        return sf_id
    return [sf_id[:15], sf_id.lower(), f" {sf_id}  "][int(rng.integers(3))]


def envelope(deliver_at: datetime, source: str, channel: str, body: dict,
             headers: dict | None = None, meta: dict | None = None) -> dict:
    return {"deliver_at": deliver_at.replace(microsecond=0), "source": source,
            "channel": channel, "headers": headers or {}, "body": body, "meta": meta or {}}


def body_bytes(body: dict) -> bytes:
    return json.dumps(body, separators=(",", ":"), sort_keys=True).encode()


def stripe_signature(slug: str, body: dict, t: int) -> str:
    secret = f"whsec_demo_{slug}".encode()
    mac = hmac.new(secret, f"{t}.".encode() + body_bytes(body), hashlib.sha256).hexdigest()
    return f"t={t},kid={slug}-stripe,v1={mac}"


def stripe(world: World, events: list[dict], rng: np.random.Generator) -> list[dict]:
    """Webhooks seconds after `created`, so same-second bursts arrive shuffled; a backoff tail
    of up to 72 h; exact re-deliveries; semantic twins (new evt id, same object state)."""
    p, n = PARAMS["feed"]["stripe"], len(events)
    updates = [i for i, e in enumerate(events) if e["type"] == "customer.subscription.updated"]
    twins = {updates[i] for i in pick(rng, len(updates), p["twin"])}
    tail, again = pick(rng, n, p["tail"]), pick(rng, n, p["redeliver"])
    out = []
    for i, body in enumerate(events):
        lag = (min(72 * 3600, 60 + rng.exponential(p["tail_mean_hours"] * 3600)) if i in tail
               else rng.integers(1, 30))
        t = body["created"] + int(lag)
        out.append(_signed(world.slug, body, t))
        if i in again:  # every delivery attempt is signed afresh
            out.append(_signed(world.slug, body, t + int(rng.integers(60, 6 * 3600))))
        if i in twins:
            twin = body | {"id": "evt_" + token(rng, 24),
                           "created": body["created"] + int(rng.integers(0, 3))}
            out.append(_signed(world.slug, twin, twin["created"] + int(rng.integers(1, 30))))
    return out


def _signed(slug: str, body: dict, t: int) -> dict:
    return envelope(datetime.fromtimestamp(t, UTC), "stripe", "webhook", body,
                    {"Stripe-Signature": stripe_signature(slug, body, t)})


def zendesk(rows: list[tuple[datetime, dict, dict]], rng: np.random.Generator) -> list[dict]:
    """Incremental-export polls a few minutes behind (the API serves nothing from the last
    minute); the same row is sometimes exported twice."""
    again = pick(rng, len(rows), PARAMS["feed"]["zendesk"]["duplicate"])
    out = []
    for i, (at, body, meta) in enumerate(rows):
        seen = at + timedelta(seconds=float(rng.uniform(60, 600)))
        out.append(envelope(seen, "zendesk", "sync", body, meta=meta))
        if i in again:
            out.append(envelope(seen + timedelta(minutes=float(rng.uniform(5, 60))), "zendesk",
                                "sync", body, meta=meta))
    return out


def salesforce(world: World, rows: list[tuple[datetime, dict]],
               rng: np.random.Generator) -> list[dict]:
    """Nightly batch at 02:00 tenant-local the day after the change; ~2% of rows are exported
    again unchanged (same Id and SystemModstamp) on a later night."""
    tz = world.spec.timezone
    again = pick(rng, len(rows), PARAMS["feed"]["salesforce"]["reexport"])
    out = []
    for i, (at, body) in enumerate(rows):
        day = at.astimezone(ZoneInfo(tz)).date()
        nights = [1, 1 + int(rng.integers(1, 4))] if i in again else [1]
        out += [envelope(local_dt(day + timedelta(days=k), 2, tz), "salesforce", "batch", body)
                for k in nights]
    return out


def gmail(rows: list[tuple[datetime, dict, str]], rng: np.random.Generator) -> list[dict]:
    """Each mailbox syncs every ~15 minutes."""
    out = []
    for at, body, mailbox in rows:
        tick = math.ceil(at.timestamp() / QUARTER_HOUR) * QUARTER_HOUR + int(rng.integers(0, 60))
        out.append(envelope(datetime.fromtimestamp(tick, UTC), "gmail", "sync", body,
                            meta={"mailbox": mailbox}))
    return out


def usage_batches(months: list[date], rng: np.random.Generator) -> dict[date, datetime]:
    """When each month's usage batch lands: 03:00 UTC on the 3rd of the next month, ~10% of
    batches 1-9 days late. Keyed by period_start."""
    late = pick(rng, len(months), PARAMS["feed"]["product"]["late_batch"])
    out = {}
    for i, month in enumerate(sorted(months)):
        nxt = (month.replace(day=28) + timedelta(days=4)).replace(day=3)
        out[month] = datetime.combine(nxt, time(3), UTC) + timedelta(
            days=int(rng.integers(1, 10)) if i in late else 0)
    return out


def track(received: datetime, rng: np.random.Generator) -> datetime:
    """Segment webhook destinations forward events within minutes of receipt."""
    return received + timedelta(seconds=float(rng.uniform(5, 300)))
