"""Salesforce-level facts: users, accounts, contacts, renewal opportunities, merges, tests."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import numpy as np

from radar.sim.model import Account, CrmChange, clip_to_start, local_dt
from radar.sim.names import sf_id
from radar.sim.params import PARAMS

if TYPE_CHECKING:
    from radar.sim.world import World

OPP_OPEN_DAYS = 120  # renewal opportunity opens this long before the term end


def crm_changes(world: World, rng: np.random.Generator, seq) -> list[CrmChange]:
    spec, tz = world.spec, world.spec.timezone
    sim_end = datetime.combine(world.sim_end, datetime.max.time(), UTC)
    by_email = {r["email"]: r for r in world.staff}
    first = datetime.combine(world.weeks[0], datetime.min.time(), UTC)
    out = [CrmChange(first, "User", r["sf_user_id"],
                     {"Email": r["email"], "Name": r["name"],
                      "ManagerId": by_email[r["manager_email"]]["sf_user_id"]
                      if r["manager_email"] else None}) for r in world.staff]
    owner = next(r["sf_user_id"] for r in world.staff if r["role"] == "admin")
    seats = {}
    for b in world.billing:
        if b.kind in ("sub_created", "seat_change"):
            seats.setdefault(b.sf_account_id, []).append((b.at, b.quantity))
    decisions = {k: g.to_dict("records") for k, g in world.decisions.groupby("sf_account_id")}

    for a in world.accounts:
        rows = decisions.get(a.sf_account_id, [])
        churn = next((r for r in rows if r["kind"] == "churn"), None)
        row = {"Name": a.name, "Website": "https://www." + a.domain,
               "OwnerId": a.csm_sf_id if spec.csm_field == "OwnerId" else owner,
               "CSM__c": a.csm_sf_id if spec.csm_field == "CSM__c" else None,
               "Type": "Customer", "Segment__c": a.segment, "Is_Test__c": False,
               "MasterRecordId": None, "ARR__c": _arr(a, a.seats),
               "Renewal_Date__c": a.next_term_end(a.start)}
        changes = [(local_dt(a.start, 16, tz), dict(row))]
        for at, qty in seats.get(a.sf_account_id, [])[1:]:  # CS ops books seat changes
            changes.append((at + timedelta(days=int(rng.integers(1, 4))), {"ARR__c": _arr(a, qty)}))
        for r in rows:
            if r["kind"] == "renew":  # renewal booked: next term's date
                changes.append((r["effective_at"], {"Renewal_Date__c": a.next_term_end(
                    r["term_end"])}))
        if churn:
            changes.append((churn["effective_at"] + timedelta(hours=2),
                            {"Type": "Former Customer"}))
        for at, delta in sorted(changes, key=lambda c: c[0]):
            row |= delta
            out.append(CrmChange(at, "Account", a.sf_account_id, dict(row)))
        if a.contract:
            out += _opportunities(world, a, rows, rng, seq)
    out += _contacts(world)
    out += _merges_and_tests(world, rng, seq, owner)
    out = [c for c in out if c.at <= sim_end]
    return clip_to_start(out, spec.source_start["salesforce"], _entity, _initial_load)


def _arr(a: Account, qty: int) -> float:
    per_year = 1 if a.interval == "year" else 12
    return round(a.unit_amount * qty * per_year / 100, 2)


def _entity(c: CrmChange):
    return None if c.object == "OpportunityHistory" else (c.object, c.sf_id)


def _initial_load(c: CrmChange, at: datetime) -> CrmChange | None:
    return None if c.deleted else replace(c, at=at)


def _opportunities(world: World, a: Account, rows: list[dict], rng: np.random.Generator,
                   seq) -> list[CrmChange]:
    """One renewal opportunity per decided term: open, negotiate, close won/lost."""
    tz, slug, out = world.spec.timezone, world.slug, []
    pool = PARAMS["loss_reasons"][slug] + list(PARAMS["shock_reasons"][slug].values())

    def days(lo: int, hi: int) -> timedelta:
        return timedelta(days=int(rng.integers(lo, hi + 1)))

    for r in rows:
        won = r["kind"] == "renew"
        term_end = r["term_end"] if won else a.next_term_end(r["decided_at"].date())
        if won:
            closed, reason = r["decided_at"] + days(0, 10), None
        else:
            reason = "payment_failed" if r["involuntary"] else r["cause"]
            noisy = rng.random() < PARAMS["loss_reason_noise"]
            if noisy and not (r["involuntary"] or a.vp_case):
                reason = pool[rng.integers(len(pool))]  # the CSM books a different reason
            closed = {"no_notice": r["effective_at"],
                      "stripe_cancel": r["decided_at"] + days(2, 14)}.get(
                r["mode"], r["decided_at"] + days(1, 10))
        opened = min(local_dt(term_end - timedelta(days=OPP_OPEN_DAYS), 9.5, tz),
                     closed - timedelta(days=1))
        stages = [(opened, "Prospecting"), (closed, "Closed Won" if won else "Closed Lost")]
        opp_id = sf_id("006", next(seq))
        for at, stage in stages:
            lost = stage == "Closed Lost"
            out.append(CrmChange(at, "Opportunity", opp_id, {
                "AccountId": a.sf_account_id, "Type": "Renewal", "StageName": stage,
                "CloseDate": term_end, "Amount": _arr(a, a.seats),
                "IsClosed": stage.startswith("Closed"), "IsWon": stage == "Closed Won",
                "Loss_Reason__c": reason if lost else None}))
            out.append(CrmChange(at, "OpportunityHistory", sf_id("008", next(seq)),
                                 {"OpportunityId": opp_id, "StageName": stage,
                                  "CreatedDate": at}))
    return out


def _contacts(world: World) -> list[CrmChange]:
    out = []
    for c in world.contacts:
        row = {"AccountId": c.sf_account_id, "Email": c.email, "FirstName": c.first_name,
               "LastName": c.last_name, "Title": c.title, "Champion__c": c.champion,
               "HasOptedOutOfEmail": False}
        out.append(CrmChange(c.created_at, "Contact", c.sf_contact_id, dict(row)))
        if c.sf_inactive_at:  # CS ops learns the person left: unflag and stop emailing
            row |= {"Champion__c": False, "HasOptedOutOfEmail": True}
            out.append(CrmChange(c.sf_inactive_at, "Contact", c.sf_contact_id, row))
    return out


def _merges_and_tests(world: World, rng: np.random.Generator, seq, owner: str):
    """A few duplicate accounts merged into their master later; a few test accounts."""
    tz, out = world.spec.timezone, []
    n = max(1, round(len(world.accounts) * PARAMS["merge_share"]))
    for i in rng.choice(len(world.accounts), n, replace=False):
        a = world.accounts[i]
        at = local_dt(a.start + timedelta(days=int(rng.integers(30, 400))), 13, tz)
        loser = sf_id("001", next(seq))
        row = {"Name": a.name, "Website": "http://" + a.domain, "OwnerId": owner,
               "CSM__c": None, "Type": "Prospect", "Segment__c": None, "Is_Test__c": False,
               "MasterRecordId": None, "ARR__c": None, "Renewal_Date__c": None}
        out.append(CrmChange(at, "Account", loser, dict(row)))
        out.append(CrmChange(at + timedelta(days=int(rng.integers(14, 61))), "Account", loser,
                             row | {"MasterRecordId": a.sf_account_id}, deleted=True))
    spec = world.spec
    lo = spec.source_start["salesforce"]
    tests = [("Test Account - Do Not Use", True), (f"{spec.name} Internal Sandbox", True),
             ("QA Demo Co", False)][: PARAMS["test_accounts"]]
    for name, flagged in tests:
        day = lo + timedelta(days=int(rng.integers((world.history_end.date() - lo).days)))
        out.append(CrmChange(local_dt(day, 12, tz), "Account", sf_id("001", next(seq)), {
            "Name": name, "Website": "https://" + spec.internal_domain, "OwnerId": owner,
            "CSM__c": None, "Type": "Customer", "Segment__c": None, "Is_Test__c": flagged,
            "MasterRecordId": None, "ARR__c": 0.0, "Renewal_Date__c": None}))
    return out
