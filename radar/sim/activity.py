"""Observable timelines derived forward from the latent state: shared helpers and tickets.

Salesforce and Stripe facts live in crm.py and billing.py; mail, usage and product events in
signals.py. Week t's observables read only state at or before week t, every source starts at
TenantSpec.source_start, and nothing is dated after sim_end.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from typing import TYPE_CHECKING

import numpy as np
import pandas as pd

from radar.sim.model import Ticket, TicketVersion, contacts_by_account, local_dt, present
from radar.sim.names import (
    COMPLAINT_TAGS,
    INJECTION_ACCOUNT_NAME,
    INJECTION_TEXT,
    SUBJECTS,
    TICKET_BODIES,
)
from radar.sim.params import PARAMS

if TYPE_CHECKING:
    from radar.sim.world import World

INJECTION_TICKET_DAY = date(2026, 9, 8)


def spans(world: World) -> list[tuple[int, int]]:
    """(first, last) active week index per account; last is the week the account ends."""
    w0, n = world.weeks[0], len(world.weeks)
    out = []
    for a, eff in zip(world.accounts, world.truth.effective_at, strict=True):
        last = n - 1 if pd.isna(eff) else min(n - 1, (eff.date() - w0).days // 7)
        out.append(((a.start - w0).days // 7, last))
    return out


def week_mask(world: World, spans: list[tuple[int, int]], source: str) -> np.ndarray:
    """(weeks x accounts) True where the account is active and the source has started."""
    t = np.arange(len(world.weeks))[:, None]
    first = np.array([s for s, _ in spans])[None, :]
    last = np.array([e for _, e in spans])[None, :]
    start = -(-(world.spec.source_start[source] - world.weeks[0]).days // 7)  # first full week
    return (t >= np.maximum(first, start)) & (t <= last)


def business_time(world: World, week: int, rng: np.random.Generator) -> datetime:
    """A random weekday working-hours moment in week `week`, tenant-local, as UTC."""
    day = world.weeks[week] + timedelta(days=int(rng.integers(5)))
    return local_dt(day, float(rng.uniform(8.5, 18)), world.spec.timezone)


# kind -> (ticket type, priorities to draw from); solve times are in PARAMS["tickets"]
TICKET_KINDS = {
    "how_to": ("question", ["low", "normal", "normal"]),
    "complaint": ("question", ["normal", "high"]),
    "bug": ("incident", ["high", "urgent"]),
    "integration": ("incident", ["high"]),
    "outage": ("incident", ["high", "urgent", "urgent"]),
    "cancellation": ("task", ["normal"]),
}


def tickets(world: World, rng: np.random.Generator, h: np.ndarray, spans) -> list[Ticket]:
    """Engaged accounts ask how-to questions, unhappy ones complain, quiet unhappy ones
    file little of either; shocks add their own tickets."""
    tp, slug = PARAMS["tickets"], world.slug
    mask = week_mask(world, spans, "zendesk")
    hz = np.nan_to_num(h)
    size = np.sqrt([a.seats / PARAMS["accounts"][slug]["seats"][0] for a in world.accounts])
    quiet = np.array([a.silent for a in world.accounts])[None, :] & (hz < 0)
    rate = np.where(quiet, tp["silent_factor"], 1.0) * size[None, :]
    asks = []  # (created_at, account index, kind, fixed_at or None)
    for kind, lam in (("how_to", tp["howto_weekly"] * np.exp(tp["howto_h"] * hz)),
                      ("complaint", tp["complaint_weekly"] * np.exp(tp["complaint_h"] * hz))):
        counts = rng.poisson(lam * rate) * mask
        for t, a in zip(*np.nonzero(counts), strict=True):
            asks += [(business_time(world, t, rng), a, kind, None)
                     for _ in range(counts[t, a])]
    for s in world.shocks:
        fixed = None
        if s.kind in ("integration_break", "bug_episode"):  # open until the fix ships
            end = s.week + (s.weeks if s.weeks is not None else tp["unfixed_close_weeks"])
            fixed = (business_time(world, end, rng) if end < len(world.weeks)
                     else datetime.combine(world.sim_end + timedelta(days=1), time(), UTC))
        if s.kind == "integration_break" and s.info.get("ticket"):
            weeks, kind = [s.week], "integration"
        elif s.kind == "outage":
            weeks, kind = [s.week], "outage"
        elif s.kind == "bug_episode":
            n = rng.integers(tp["bug_burst"][0], tp["bug_burst"][1] + 1)
            # the first report comes the week it starts; the rest while it lasts
            weeks, kind = [s.week, *(s.week + rng.integers(0, s.weeks, n - 1))], "bug"
        else:
            continue
        asks += [(business_time(world, t, rng), s.account, kind, fixed) for t in weeks
                 if t < len(world.weeks) and mask[t, s.account]]
    index = {a.sf_account_id: i for i, a in enumerate(world.accounts)}
    for r in world.decisions[world.decisions["mode"] == "cancellation_ticket"].itertuples():
        asks.append((r.decided_at, index[r.sf_account_id], "cancellation", None))
    contacts = contacts_by_account(world.contacts)
    zd_start = datetime.combine(world.spec.source_start["zendesk"], time(), UTC)
    out = []
    for created, a, kind, fixed in sorted(asks, key=lambda x: (x[0], x[1], x[2])):
        if created < zd_start:
            continue
        week = min(len(world.weeks) - 1, (created.date() - world.weeks[0]).days // 7)
        out.append(_ticket(world, world.accounts[a], contacts[world.accounts[a].sf_account_id],
                           created, kind, float(np.nan_to_num(h[week, a])), rng, fixed))
    if world.slug == "hearthline":
        out.append(_injection_ticket(world, contacts))
    end = datetime.combine(world.sim_end, time.max, UTC)
    out = sorted((t for t in out if t.created_at <= end), key=lambda t: t.created_at)
    for i, t in enumerate(out):
        t.id = 10_001 + i
        t.versions = [v for v in t.versions if v.at <= end]
    return out


def _requester(contacts: list, at: datetime) -> str:
    return (present(contacts, at) or contacts)[0].email


def _ticket(world: World, a, contacts: list, created: datetime, kind: str, h: float,
            rng: np.random.Generator, fixed: datetime | None = None) -> Ticket:
    """A ticket's versions. One waiting on a fix goes on hold and is solved once it ships."""
    ttype, priorities = TICKET_KINDS[kind]
    slug, tp = world.slug, PARAMS["tickets"]
    if kind == "complaint":
        j = int(rng.integers(len(COMPLAINT_TAGS[slug])))
        subject, tags = SUBJECTS[slug]["complaint"][j], [COMPLAINT_TAGS[slug][j]]
    elif kind in ("outage", "cancellation"):
        outage = PARAMS["outage"].get(slug)
        subject, tags = {"outage": ("App is down for our whole team", [f"outage-{outage}"]),
                         "cancellation": ("Cancel our subscription at renewal",
                                          ["cancellation_request"])}[kind]
    else:
        pool = SUBJECTS[slug][kind]
        subject = pool[rng.integers(len(pool))].format(
            integration=a.integration.replace("_", " ").title())
        tags = [{"integration": "crm_sync" if slug == "hearthline" else "integration"}
                .get(kind, kind)]
    description = f"{subject}. {TICKET_BODIES[kind]}"
    priority = priorities[rng.integers(len(priorities))]
    t = Ticket(0, a.sf_account_id, _requester(contacts, created), created)

    def version(at: datetime, status: str, csat: str = "unoffered") -> TicketVersion:
        return TicketVersion(at, status, priority, ttype, tags, csat, subject, description)

    t.versions.append(version(created, "new"))
    solve = created + timedelta(hours=float(tp["solve_hours"][kind]
                                            * rng.lognormal(0, tp["solve_sigma"])))
    if rng.random() < tp["reopen_share"]:  # waiting on the customer first
        t.versions.append(version(created + (solve - created) / 2, "pending"))
    if fixed is not None and fixed > solve:  # escalated to engineering
        t.versions.append(version(solve, "hold"))
        solve = fixed
    csat = "unoffered"
    if rng.random() < tp["csat_offered"]:
        a_bad, b_bad = tp["csat_bad"]
        bad = 1 / (1 + np.exp(a_bad + b_bad * h)) + (tp["csat_incident_bad"]
                                                     if kind != "how_to" else 0.0)
        csat = ("offered" if rng.random() < 1 - tp["csat_answered"]
                else "bad" if rng.random() < bad else "good")
    t.versions.append(version(solve, "solved", csat))
    return t


def _injection_ticket(world: World, contacts: dict) -> Ticket:
    """Prompt-injection fixture (design §3.4): in the description and a later comment."""
    a = next(a for a in world.accounts if a.name == INJECTION_ACCOUNT_NAME)
    at = local_dt(INJECTION_TICKET_DAY, 10.5, world.spec.timezone)
    subject, tags = "Question about our account", ["account_question"]
    first = f"Hi team,\n\n{INJECTION_TEXT}"
    later = f"{first}\n\nCustomer comment: {INJECTION_TEXT}"
    t = Ticket(0, a.sf_account_id, _requester(contacts[a.sf_account_id], at), at)
    for offset, status, description in ((0, "new", first), (26, "open", later),
                                         (70, "solved", later)):
        t.versions.append(TicketVersion(at + timedelta(hours=offset), status, "normal",
                                        "question", tags, "unoffered", subject, description))
    return t
