"""CSM mailbox traffic, monthly product usage and discrete product events."""

from __future__ import annotations

import itertools
from datetime import UTC, date, datetime, time, timedelta
from typing import TYPE_CHECKING

import numpy as np

from radar.sim.activity import business_time, week_mask
from radar.sim.model import Email, ProductEvent, UsageMonth, add_months
from radar.sim.names import EMAIL_SUBJECTS, SILENT_INTEGRATIONS
from radar.sim.params import PARAMS

if TYPE_CHECKING:
    from radar.sim.world import World

MAILER_DAEMON = "mailer-daemon@googlemail.com"
INBOUND_SUBJECTS = ["Question about our account", "Following up", "Invoice question",
                    "Can we get a training session?", "Adding a new teammate"]


def emails(world: World, rng: np.random.Generator, h: np.ndarray, spans) -> list[Email]:
    """CSM outreach, customer replies and inbound mail, bounces and auto-replies.

    A departed champion's inbound stops; the first email sent to them afterwards hard-bounces
    (5.1.1), and the CSM then writes to the successor or another contact.
    """
    ep = PARAMS["email"]
    mask = week_mask(world, spans, "gmail")
    hz = np.nan_to_num(h)
    median = PARAMS["accounts"][world.slug]["seats"][0]
    size = np.array([(a.seats / median) ** 0.3 for a in world.accounts])
    n_out = rng.poisson(ep["outbound_weekly"] * size[None, :] * mask)
    n_in = rng.poisson(ep["inbound_weekly"] * np.exp(0.3 * hz) * mask)
    manager = {r["email"]: r["manager_email"] for r in world.staff}
    suppressed: dict[int, date] = {}  # VP cases: CSM outbound stops at the first planted shock
    for s in world.shocks:
        if s.info.get("planted"):
            suppressed.setdefault(s.account, world.weeks[s.week])
    contacts: dict[str, list] = {}
    for c in world.contacts:
        contacts.setdefault(c.sf_account_id, []).append(c)
    end = datetime.combine(world.sim_end, time.max, UTC)
    out: list[Email] = []
    for i, a in enumerate(world.accounts):
        moments = sorted([(business_time(world, t, rng), "out")
                          for t in np.repeat(np.arange(len(world.weeks)), n_out[:, i])]
                         + [(business_time(world, t, rng), "in")
                            for t in np.repeat(np.arange(len(world.weeks)), n_in[:, i])])
        people, bounced, csm = contacts[a.sf_account_id], set(), a.csm_email
        quiet_from = suppressed.get(i)
        for n, (at, direction) in enumerate(moments):
            thread = f"{a.sf_account_id}:{n}"
            week = (at.date() - world.weeks[0]).days // 7
            if direction == "in":
                present = [c for c in people if c.created_at <= at
                           and not (c.departed_at and c.departed_at <= at)]
                if present:
                    who = present[rng.integers(len(present))]
                    out.append(Email(a.sf_account_id, at, "inbound", [csm], who.email, [csm], [],
                                     INBOUND_SUBJECTS[rng.integers(len(INBOUND_SUBJECTS))],
                                     thread))
                continue
            if quiet_from and quiet_from <= at.date() < quiet_from + timedelta(
                    days=ep["suppress_days"]):
                continue  # the VP cases: the CSM went quiet
            to = _target(people, at, bounced)
            if to is None:
                continue
            cc = [manager[csm]] if manager.get(csm) and rng.random() < ep["manager_cc"] else []
            boxes, subject = [csm, *cc], EMAIL_SUBJECTS[rng.integers(len(EMAIL_SUBJECTS))]
            out.append(Email(a.sf_account_id, at, "outbound", boxes, csm, [to.email], cc,
                             subject, thread))
            soon = at + timedelta(minutes=float(rng.uniform(1, 5)))
            if to.departed_at and to.departed_at <= at:
                bounced.add(to.sf_contact_id)
                out.append(_dsn(a, soon, csm, to.email, "5.1.1", thread))
                continue
            r = rng.random()
            if r < ep["soft_bounce"] + ep["policy_reject"]:
                status = "4.4.7" if r < ep["soft_bounce"] else "5.7.1"
                out.append(_dsn(a, soon, csm, to.email, status, thread))
            elif r < ep["soft_bounce"] + ep["policy_reject"] + ep["auto_reply"]:
                out.append(Email(a.sf_account_id, soon, "inbound", [csm], to.email, [csm], [],
                                 "Automatic reply: " + subject, thread, is_auto_reply=True))
            elif rng.random() < 1 / (1 + np.exp(-(ep["reply_base"] + ep["reply_h"] * hz[
                    min(week, len(world.weeks) - 1), i]))):
                reply_at = at + timedelta(hours=float(6 * rng.lognormal(0, 1)))
                out.append(Email(a.sf_account_id, reply_at, "inbound", boxes, to.email, [csm],
                                 cc, "Re: " + subject, thread))
    return sorted((e for e in out if e.sent_at <= end), key=lambda e: e.sent_at)


def _target(people: list, at: datetime, bounced: set):
    """Whom the CSM writes to: the newest champion they still believe in, else anyone."""
    known = [c for c in people if c.created_at <= at and c.sf_contact_id not in bounced
             and not (c.sf_inactive_at and c.sf_inactive_at <= at)]
    champions = [c for c in known if c.champion]
    return (champions or known or [None])[-1]


def _dsn(a, at: datetime, csm: str, recipient: str, status: str, thread: str) -> Email:
    kind = "Delay" if status.startswith("4") else "Failure"
    return Email(a.sf_account_id, at, "inbound", [csm], MAILER_DAEMON, [csm], [],
                 f"Delivery Status Notification ({kind})", thread, dsn_status=status,
                 failed_recipient=recipient)


def _seat_timeline(world: World) -> dict[str, list]:
    seats: dict[str, list] = {}
    for b in world.billing:
        if b.kind in ("sub_created", "seat_change"):
            seats.setdefault(b.sf_account_id, []).append((b.at, b.quantity))
    return seats


def _seats_at(timeline: list, at: datetime, default: int) -> int:
    return next((q for t, q in reversed(timeline) if t <= at), default)


def usage(world: World, rng: np.random.Generator, h: np.ndarray, spans) -> list[UsageMonth]:
    """Monthly summaries for linked workspaces; hearthline engagement metrics are seasonal."""
    up, slug, w0 = PARAMS["usage"], world.slug, world.weeks[0]
    lo, hi = up["utilisation"]
    first = world.spec.source_start["product"].replace(day=1)
    seats = _seat_timeline(world)
    out = []
    for i, a in enumerate(world.accounts):
        if not a.workspace_id:
            continue
        s, e = spans[i]
        month = max(first, a.start.replace(day=1))
        while True:
            nxt = add_months(month, 1)
            if nxt > world.sim_end + timedelta(days=1) or (month - w0).days // 7 > e:
                break
            weeks = [t for t in range(max(s, -(-(month - w0).days // 7)),
                                      min(e, (nxt - w0).days // 7 - 1) + 1)]
            hbar = float(np.mean(h[weeks or [max(s, (month - w0).days // 7)], i]))
            util = lo + (hi - lo) / (1 + np.exp(-up["slope"] * hbar))
            n_seats = _seats_at(seats.get(a.sf_account_id, []),
                                datetime.combine(month, time(), UTC), a.seats)
            season = PARAMS["seasonality"][month.month - 1] if slug == "hearthline" else 1.0
            metrics = {"active_users": int(rng.binomial(n_seats, util))}
            for name, per_seat in up["per_seat"][slug].items():
                factor = season if name in up["seasonal"] else 1.0
                metrics[name] = int(round(n_seats * per_seat * util * factor
                                          * rng.lognormal(0, up["noise_sd"])))
            if slug == "hearthline":
                sent = metrics["emails_sent"]
                metrics["emails_delivered"] = int(sent * rng.uniform(0.95, 0.99))
                metrics["email_opens"] = int(metrics["emails_delivered"] * (0.1 + 0.25 * util))
                metrics["felix_handoffs"] = int(rng.poisson(metrics["felix_calls_made"] * 0.025))
                metrics["lead_score_avg"] = round(float(30 + 50 * util + rng.normal(0, 3)), 1)
            out.append(UsageMonth(a.sf_account_id, a.workspace_id, month, nxt, metrics))
            month = nxt
    return out


SYNC_ERRORS = {"hearthline": ["invalid_field", "api_key_error", "service_disruption"],
               "tallyworks": ["consent_expired", "bank_api_error"],
               "kitewire": ["token_revoked", "webhook_failed"]}


def product_events(world: World, rng: np.random.Generator, h: np.ndarray,
                   spans) -> list[ProductEvent]:
    """Go-live, integration breaks (native integrations only), seat suspensions and Sales
    Agent pauses, for linked workspaces."""
    slug, out = world.slug, []
    seats = _seat_timeline(world)
    for s in world.shocks:
        a = world.accounts[s.account]
        if not a.workspace_id:
            continue
        at = business_time(world, s.week, rng)
        if s.kind == "integration_break" and a.integration not in SILENT_INTEGRATIONS:
            props = {"integration": a.integration,
                     "reason": SYNC_ERRORS[slug][rng.integers(len(SYNC_ERRORS[slug]))]}
            out.append((s.account, at, "integration_sync_error", props))
            out.append((s.account, at + timedelta(days=float(rng.uniform(1, 5))),
                        "integration_disconnected", {"integration": a.integration}))
            if s.weeks is not None and s.week + s.weeks < len(world.weeks):
                out.append((s.account, business_time(world, s.week + s.weeks, rng),
                            "integration_reconnected", {"integration": a.integration}))
        elif slug == "hearthline" and s.kind in PARAMS["sales_agent_pause"] and (
                rng.random() < PARAMS["sales_agent_pause"][s.kind]):
            out.append((s.account, at, "sales_agent_paused", {"scope": "account"}))
    index = {a.sf_account_id: i for i, a in enumerate(world.accounts)}
    for key, timeline in seats.items():
        for (_, before), (at, after) in itertools.pairwise(timeline):
            for k in range(min(5, before - after)):  # one event per suspended user
                out.append((index[key], at + timedelta(minutes=3 * k), "user_suspended",
                            {"seats_after": after}))
    for i, (s, e) in enumerate(spans):  # first week health crosses zero
        live = np.flatnonzero(h[s : e + 1, i] > 0)
        if live.size and world.accounts[i].workspace_id:
            at = business_time(world, s + int(live[0]), rng)
            out.append((i, at, "workspace_went_live",
                        {"days_since_contract": (at.date() - world.accounts[i].start).days}))
    start = datetime.combine(world.spec.source_start["product"], time(), UTC)
    end = datetime.combine(world.sim_end, time.max, UTC)
    events = []
    for i, at, event, props in out:
        a, (_, e) = world.accounts[i], spans[i]
        if a.workspace_id and start <= at <= end and (at.date() - world.weeks[0]).days // 7 <= e:
            events.append(ProductEvent(a.sf_account_id, a.workspace_id, at, event, props))
    return sorted(events, key=lambda p: (p.at, p.sf_account_id, p.event))
