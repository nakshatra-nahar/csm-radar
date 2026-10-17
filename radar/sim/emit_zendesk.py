"""Zendesk incremental-export rows: one ticket row per version, one organization row per
customer that files tickets (design §3.5)."""

from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np

from radar.sim import delivery
from radar.sim.model import local_dt
from radar.sim.names import PERSONAL_DOMAINS
from radar.sim.params import PARAMS

if TYPE_CHECKING:
    from radar.sim.world import World

CHANNELS = ["email", "email", "web", "web", "api"]
USER_ID_BASE = 21_500_000_000_000  # Zendesk user ids are 13-digit


def emit(world: World, rng: np.random.Generator) -> tuple[list[dict], set[tuple[str, str]]]:
    """Feed lines and the identity truth of every `zd_org:` handle they carry."""
    start = delivery.source_start(world, "zendesk")
    accounts = {a.sf_account_id: a for a in world.accounts}
    base = f"https://{world.slug}.zendesk.com/api/v2"
    users: dict[str, int] = {}
    rows, orgs = [], {}
    for t in world.tickets:
        a = accounts[t.sf_account_id]
        # personal-domain requesters are not mapped to an org (~10% of tickets)
        org = None if t.requester_email.split("@")[1] in PERSONAL_DOMAINS else a.zd_org_id
        if org is not None and org not in orgs:
            orgs[org] = (max(start, local_dt(a.start, 16, world.spec.timezone)), a)
        requester = users.setdefault(t.requester_email, USER_ID_BASE + 7 * len(users))
        channel = CHANNELS[rng.integers(len(CHANNELS))]
        for v in t.versions:
            if v.at < start:
                continue
            rows.append((v.at, {
                "id": t.id, "url": f"{base}/tickets/{t.id}.json",
                "created_at": delivery.iso(t.created_at), "updated_at": delivery.iso(v.at),
                "generated_timestamp": delivery.epoch(v.at), "subject": v.subject,
                "description": v.description, "priority": v.priority, "status": v.status,
                "type": v.type, "requester_id": requester, "organization_id": org,
                "tags": list(v.tags),  # 2a shares one list across a ticket's versions
                "via": {"channel": channel}, "satisfaction_rating": {"score": v.csat},
                "problem_id": None,  # Appendix B attr; 2a models no problem/incident links
            }, {"export": "tickets"}))
    share = PARAMS["feed"]["zendesk"]["org_external_id"]
    for org, (at, a) in orgs.items():
        rows.append((at, {
            "id": org, "url": f"{base}/organizations/{org}.json", "name": a.name,
            "external_id": a.sf_account_id if rng.random() < share else None,
            "domain_names": [a.domain], "created_at": delivery.iso(at),
            "updated_at": delivery.iso(at),
        }, {"export": "organizations"}))
    rows.sort(key=lambda r: r[0])
    handles = {(f"zd_org:{org}", a.sf_account_id) for org, (_, a) in orgs.items()}
    return delivery.zendesk(rows, rng), handles
