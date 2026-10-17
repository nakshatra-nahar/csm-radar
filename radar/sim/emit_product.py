"""Product telemetry: monthly usage summaries (batch) and Segment track events (design §3.5)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING

import numpy as np

from radar.sim import delivery
from radar.sim.delivery import iso_ms
from radar.sim.model import UsageMonth
from radar.sim.names import token
from radar.sim.params import PARAMS

if TYPE_CHECKING:
    from radar.sim.world import World

V2_FROM = datetime(2025, 9, 1, tzinfo=UTC)  # usage_summary.v2: lead_score_avg is renamed


def emit(world: World, rng: np.random.Generator) -> tuple[list[dict], set[tuple[str, str]]]:
    """Feed lines and the identity truth of every `ws:` workspace handle."""
    out = _usage(world, rng) + _track(world, rng)
    handles = {("ws:" + u.workspace_id, u.sf_account_id) for u in world.usage}
    start = delivery.source_start(world, "product")
    handles |= {("ws:" + e.workspace_id, e.sf_account_id) for e in world.product_events
                if e.at >= start}
    return out, handles


def _usage(world: World, rng: np.random.Generator) -> list[dict]:
    """One row per workspace-month in the month's batch; ~3% restated (version+1) within
    3 days; one whole month per tenant reprocessed ~30 days after its batch."""
    p = PARAMS["feed"]["product"]
    months = sorted({u.period_start for u in world.usage})
    batch = delivery.usage_batches(months, rng)
    deep = [m for m in months if batch[m] + timedelta(days=30) <= world.history_end]
    deep_month = deep[rng.integers(len(deep))] if deep else None
    restated = delivery.pick(rng, len(world.usage), p["restate"])
    linked: dict[str, str | None] = {}  # workspace -> sf_account_id as the product stores it
    out = []
    for i, u in enumerate(world.usage):
        if u.workspace_id not in linked:
            linked[u.workspace_id] = (delivery.messy_id(u.sf_account_id, rng, p["messy_id"])
                                      if rng.random() < p["sf_account_id"] else None)
        versions = [batch[u.period_start]]
        if i in restated:
            versions.append(versions[0] + timedelta(days=float(rng.uniform(1, 3))))
        if u.period_start == deep_month:
            versions.append(batch[u.period_start] + timedelta(days=30))
        for version, at in enumerate(versions, start=1):
            metrics = u.metrics if version == 1 else _restate(u.metrics, rng)
            body = _summary(world, u, linked[u.workspace_id], version,
                            at - timedelta(minutes=30), metrics)
            out.append(delivery.envelope(at, "product", "batch", body))
    return out


def _restate(metrics: dict, rng: np.random.Generator) -> dict:
    """A recomputation moves the counts a little; active_users is stable."""
    out = {}
    for k, v in metrics.items():
        f = float(rng.uniform(0.95, 1.05))
        out[k] = v if k == "active_users" else (round(v * f, 1) if isinstance(v, float)
                                                 else int(round(v * f)))
    return out


def _summary(world: World, u: UsageMonth, sf_account_id: str | None, version: int,
             computed_at: datetime, metrics: dict) -> dict:
    v2 = computed_at >= V2_FROM
    if v2 and "lead_score_avg" in metrics:
        metrics = dict(metrics)
        metrics["signal_intent_high"] = metrics.pop("lead_score_avg")
    last_day: date = u.period_end - timedelta(days=1)
    return {"schema": "usage_summary.v2" if v2 else "usage_summary.v1",
            "workspace_id": u.workspace_id, "sf_account_id": sf_account_id,
            "period_start": u.period_start.isoformat(), "period_end": last_day.isoformat(),
            "version": version, "computed_at": delivery.iso(computed_at), "metrics": metrics}


def _track(world: World, rng: np.random.Generator) -> list[dict]:
    """Segment track calls; ~0.1% carry an explicit future `timestamp` (bad device clock)."""
    start = delivery.source_start(world, "product")
    future = delivery.pick(rng, len(world.product_events), PARAMS["feed"]["product"]["future"])
    out = []
    for i, e in enumerate(world.product_events):
        if e.at < start:
            continue
        sent = e.at + timedelta(milliseconds=float(rng.uniform(5, 400)))
        received = sent + timedelta(milliseconds=float(rng.uniform(50, 300)))
        original = e.at
        timestamp = received - (sent - original)  # Segment's clock-skew correction
        if i in future:
            original = timestamp = e.at + timedelta(hours=float(rng.uniform(2, 72)))
        body = {"messageId": "msg_" + token(rng, 24), "type": "track",
                "event": e.event.replace("_", " ").title(),
                "userId": "usr_" + token(rng, 10), "context": {"groupId": e.workspace_id},
                "properties": e.properties, "timestamp": iso_ms(timestamp),
                "sentAt": iso_ms(sent), "receivedAt": iso_ms(received),
                "originalTimestamp": iso_ms(original)}
        out.append(delivery.envelope(delivery.track(received, rng), "product", "webhook", body))
    return out
