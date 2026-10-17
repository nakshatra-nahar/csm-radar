"""Salesforce rows as a Bulk API queryAll export returns them (design §3.5)."""

from __future__ import annotations

from datetime import date, datetime
from typing import TYPE_CHECKING

import numpy as np

from radar.sim import delivery

if TYPE_CHECKING:
    from radar.sim.world import World


def modstamp(at: datetime) -> str:
    """SF REST datetime format: millisecond precision, '+0000' offset."""
    return delivery.iso(at)[:-1] + ".000+0000"


def _value(v):
    if isinstance(v, datetime):
        return modstamp(v)
    return v.isoformat() if isinstance(v, date) else v


def emit(world: World, rng: np.random.Generator) -> tuple[list[dict], set[tuple[str, str]]]:
    """Feed lines and the identity truth of every Account Id they carry (`sf:` handles)."""
    start = delivery.source_start(world, "salesforce")
    master = {c.sf_id: c.fields["MasterRecordId"] for c in world.crm if c.deleted}
    rows, handles = [], set()
    for c in world.crm:
        at = max(c.at, start)  # 2a's initial load sits at 06:00 UTC, before a western midnight
        body = {"attributes": {"type": c.object}, "Id": c.sf_id, "SystemModstamp": modstamp(at),
                "IsDeleted": c.deleted} | {k: _value(v) for k, v in c.fields.items()}
        rows.append((at, body))
        if c.object == "Account":  # test accounts are their own truth
            handles.add(("sf:" + c.sf_id, master.get(c.sf_id, c.sf_id)))
    return delivery.salesforce(world, rows, rng), handles
