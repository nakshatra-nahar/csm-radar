"""Accounts, contacts, staff, weekly latent health, shocks and churn decisions (design §3.2).

Strictly forward: shocks are drawn independently of health, health is an AR(1) process plus
shock offsets, and churn decisions are drawn from health. Observables come later, in activity.py.
"""

import itertools
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

import numpy as np
import pandas as pd

from radar.sim.activity import spans, tickets
from radar.sim.billing import billing_events
from radar.sim.churn import decide
from radar.sim.crm import crm_changes
from radar.sim.model import (
    Account,
    BillingEvent,
    Contact,
    CrmChange,
    Email,
    PaymentFailure,
    ProductEvent,
    Shock,
    Ticket,
    UsageMonth,
    add_months,
    local_dt,
)
from radar.sim.names import (
    FIRST,
    INJECTION_ACCOUNT_NAME,
    INTEGRATIONS,
    LAST,
    NOUNS,
    PERSONAL_DOMAINS,
    PLACES,
    TITLES,
    VP_CASE_NAMES,
    VP_TERM_ENDS,
    sf_id,
    token,
)
from radar.sim.params import PARAMS
from radar.sim.signals import emails, product_events, usage
from radar.tenants import TENANTS, TenantSpec


@dataclass
class World:
    slug: str
    spec: TenantSpec
    seed: int
    scale: float
    history_end: datetime
    sim_end: date
    staff: list[dict]
    accounts: list[Account]
    contacts: list[Contact]
    weeks: list[date]  # week_start of each week index
    shocks: list[Shock]
    payment_failures: list[PaymentFailure]
    weekly: pd.DataFrame  # sf_account_id, week_start, h (active weeks only)
    decisions: pd.DataFrame  # one row per renewal decision or churn
    truth: pd.DataFrame  # one row per account
    crm: list[CrmChange] = field(default_factory=list)
    billing: list[BillingEvent] = field(default_factory=list)
    tickets: list[Ticket] = field(default_factory=list)
    emails: list[Email] = field(default_factory=list)
    usage: list[UsageMonth] = field(default_factory=list)
    product_events: list[ProductEvent] = field(default_factory=list)

    @property
    def telemetry_linked(self) -> set[str]:
        return {a.sf_account_id for a in self.accounts if a.workspace_id}

    def estimated_history_records(self) -> int:
        """Source records 2b emits with delivery up to history_end, before duplicates/twins.

        Usage summaries are counted when their batch (3rd of the next month) is due.
        """
        end = self.history_end
        n = sum(c.at <= end for c in self.crm) + sum(b.at <= end for b in self.billing)
        n += sum(v.at <= end for t in self.tickets for v in t.versions)
        n += sum(len(e.mailboxes) for e in self.emails if e.sent_at <= end)
        n += sum(u.period_end + timedelta(days=2) <= end.date() for u in self.usage)
        return n + sum(p.at <= end for p in self.product_events)


def tenant_rng(slug: str, seed: int, salt: int = 0) -> np.random.Generator:
    return np.random.Generator(np.random.PCG64(seed * 1000 + list(TENANTS).index(slug) + salt))


def staff(slug: str, seed: int = 7) -> list[dict]:
    """CSMs, managers (each CSM reports to one) and one CS-ops admin. Deterministic."""
    spec = TENANTS[slug]
    rng = tenant_rng(slug, seed, salt=500)
    pairs = [(f, la) for f in FIRST for la in LAST]
    n = spec.n_csms + spec.n_managers + 1
    picks = [pairs[i] for i in rng.choice(len(pairs), n, replace=False)]
    roles = ["manager"] * spec.n_managers + ["csm"] * spec.n_csms + ["admin"]
    base = 10_000_000 * list(TENANTS).index(slug)
    rows = []
    for i, ((first, last), role) in enumerate(zip(picks, roles, strict=True)):
        rows.append({
            "email": f"{first}.{last}@{spec.internal_domain}".lower(),
            "name": f"{first} {last}",
            "role": role,
            "manager_email": None,
            "sf_user_id": sf_id("005", base + i + 1),
        })
    managers = rows[: spec.n_managers]
    for i, row in enumerate(r for r in rows if r["role"] == "csm"):
        row["manager_email"] = managers[i % len(managers)]["email"]
    return rows


def _company_name(slug: str, rng: np.random.Generator, taken: set[str]) -> str:
    noun = NOUNS[slug]
    while True:
        a, b = LAST[rng.integers(len(LAST))], LAST[rng.integers(len(LAST))]
        place = PLACES[rng.integers(len(PLACES))]
        n = noun[rng.integers(len(noun))]
        name = [f"{a} {n}", f"{place} {n}", f"{a} & {b} {n}", f"{place} {a} {n}"][
            rng.integers(4)]
        if name not in taken:
            taken.add(name)
            return name


def _pick(table: list[tuple], seats: int):
    return next(row for row in table if seats <= row[1])


def _make_accounts(spec: TenantSpec, n: int, rng: np.random.Generator,
                   roster: list[dict], seq) -> list[Account]:
    slug, p = spec.slug, PARAMS["accounts"][spec.slug]
    lo, hi = PARAMS["start_window"][slug]
    csms = [r for r in roster if r["role"] == "csm"]
    planted = list(VP_CASE_NAMES) + [INJECTION_ACCOUNT_NAME] if slug == "hearthline" else []
    taken = set(planted)
    billing_types, shares = list(spec.billing), list(spec.billing.values())
    accounts = []
    for i in range(n):
        name = planted[i] if i < len(planted) else _company_name(slug, rng, taken)
        median, sd, smin, smax = p["seats"]
        seats = int(np.clip(round(rng.lognormal(np.log(median), sd)), smin, smax))
        plan, _, monthly = _pick(p["plans"], seats)
        start = lo + timedelta(days=int(rng.integers((hi - lo).days)))
        billing = billing_types[rng.choice(len(shares), p=shares)]
        csm = csms[rng.integers(len(csms))]
        lat, ob = PARAMS["latent"], PARAMS["onboarding"]
        acct = Account(
            sf_account_id=sf_id("001", next(seq)),
            name=name,
            domain="".join(c for c in name.lower() if c.isalnum()) + p["domain"],
            segment=_pick(p["segments"], seats)[0],
            billing=billing,
            plan=plan,
            seats=seats,
            unit_amount=monthly,
            start=start.replace(day=min(start.day, 28)),
            csm_email=csm["email"],
            csm_sf_id=csm["sf_user_id"],
            mu=float(rng.normal(lat["mu_mean"], lat["mu_sd"])),
            onboarding_failed=bool(rng.random() < ob["fail_rate"]),
            integration=INTEGRATIONS[slug][rng.integers(len(INTEGRATIONS[slug]))],
            stripe_customer_id="cus_" + token(rng, 14),
            stripe_subscription_id="sub_" + token(rng, 24),
            zd_org_id=360_000_000_000 + i * 17,
            workspace_id=("ws_" + token(rng, 12, "0123456789abcdef")
                          if rng.random() < PARAMS["telemetry_share"] else None),
            silent=bool(rng.random() < PARAMS["tickets"]["silent_share"]),
        )
        if i < len(VP_CASE_NAMES) and slug == "hearthline":
            acct.vp_case, acct.billing, acct.onboarding_failed = True, "annual", False
            acct.mu = PARAMS["latent"]["mu_mean"]  # an ordinary account until the shocks
            acct.start = add_months(VP_TERM_ENDS[i], -36 + 12 * i)  # 3rd and 2nd term ends
        if name == INJECTION_ACCOUNT_NAME:  # a long-standing customer by the planted ticket
            acct.start, acct.billing = date(2024, 3, 11), "annual"
        if name in planted:
            acct.workspace_id = acct.workspace_id or "ws_" + token(rng, 12, "0123456789abcdef")
            acct.integration = "follow_up_boss"
        if acct.billing == "annual":
            acct.unit_amount = round(monthly * 12 * PARAMS["annual_discount"])
        accounts.append(acct)
    return accounts


def _make_contacts(spec: TenantSpec, accounts: list[Account], rng: np.random.Generator,
                   seq) -> list[Contact]:
    lo, hi = PARAMS["contacts_per_account"]
    contacts = []
    for a in accounts:
        for k in range(int(rng.integers(lo, hi + 1))):
            contacts.append(_new_contact(spec, a, rng, seq, champion=k == 0,
                                         at=local_dt(a.start, 15, spec.timezone)))
    return contacts


def _new_contact(spec: TenantSpec, a: Account, rng: np.random.Generator, seq,
                 champion: bool, at: datetime) -> Contact:
    first, last = FIRST[rng.integers(len(FIRST))], LAST[rng.integers(len(LAST))]
    domain = (PERSONAL_DOMAINS[rng.integers(len(PERSONAL_DOMAINS))]
              if rng.random() < PARAMS["personal_email_share"] else a.domain)
    titles = TITLES[spec.slug]
    return Contact(
        sf_contact_id=sf_id("003", next(seq)),
        sf_account_id=a.sf_account_id,
        email=f"{first}.{last}{rng.integers(10, 99) if domain != a.domain else ''}@{domain}"
        .lower(),
        first_name=first,
        last_name=last,
        title=titles[0] if champion else titles[rng.integers(1, len(titles))],
        champion=champion,
        created_at=at,
    )


def _draw_shocks(slug: str, accounts: list[Account], starts: np.ndarray, weeks: list[date],
                 rng: np.random.Generator) -> list[Shock]:
    """Weekly Poisson onsets from each account's first week; details drawn at onset."""
    n_weeks, sp = len(weeks), PARAMS["shocks"]
    live = np.arange(n_weeks)[:, None] >= starts[None, :]
    shocks = []
    for kind, cfg in sp.items():
        hits = (rng.random((n_weeks, len(accounts))) < cfg["rate"] / 52) & live
        for week, a in zip(*np.nonzero(hits), strict=True):
            if not accounts[a].vp_case:  # the VP cases carry only their planted shocks
                shocks.append(_detail(Shock(int(a), kind, int(week), None), rng))
    outage = PARAMS["outage"].get(slug)
    if outage:
        week = weeks.index(outage)
        for a in np.nonzero(live[week] & (rng.random(len(accounts)) <
                                          PARAMS["outage"]["account_share"]))[0]:
            shocks.append(Shock(int(a), "outage", week, 1))
    for i, a in enumerate(accounts):
        if a.vp_case:  # forward injection 18 weeks before the term end the VP lost
            week = weeks.index(VP_TERM_ENDS[i] - timedelta(weeks=18, days=VP_TERM_ENDS[i]
                                                            .weekday()))
            planted = {"planted": True, "ticket": True, "sf_lag_weeks": None}
            shocks += [Shock(i, "integration_break", week, None, dict(planted)),
                       Shock(i, "champion_departure", week + 1, sp["champion_departure"]["weeks"],
                             dict(planted)),
                       Shock(i, "bug_episode", week + 2, sp["bug_episode"]["weeks"], dict(planted))]
    return sorted(shocks, key=lambda s: (s.week, s.account, s.kind))


def _detail(s: Shock, rng: np.random.Generator) -> Shock:
    cfg = PARAMS["shocks"][s.kind]
    if s.kind == "integration_break":
        if rng.random() < cfg["repair_share"]:
            s.weeks = int(np.ceil(rng.exponential(cfg["repair_mean_weeks"])))
        s.info["ticket"] = bool(rng.random() < cfg["ticket_share"])
    elif s.kind == "bug_episode":
        s.weeks = int(rng.integers(1, 3)) if rng.random() < cfg["resolved_share"] else cfg["weeks"]
    elif s.kind == "champion_departure":
        s.weeks = cfg["weeks"]
        lo, hi = cfg["sf_lag_weeks"]
        s.info["sf_lag_weeks"] = (int(rng.integers(lo, hi + 1))
                                  if rng.random() < cfg["sf_mark_share"] else None)
    elif s.kind == "competitor_eval":
        s.weeks = cfg["weeks"] + cfg["recover_weeks"]
    else:  # budget_cut, expansion
        s.weeks = cfg["weeks"]
    return s


def _profile(s: Shock, n: int) -> np.ndarray:
    """Health offset of a shock over its weeks (n weeks remain in the simulation)."""
    cfg = PARAMS["shocks"].get(s.kind, {})
    k = np.arange(min(n, s.weeks or n))
    if s.kind in ("champion_departure", "budget_cut", "expansion"):
        return cfg["effect"] * (1 - k / s.weeks)  # linear decay back to baseline
    if s.kind == "competitor_eval":
        w, r = cfg["weeks"], cfg["recover_weeks"]
        return np.where(k < w, cfg["drift"] * (k + 1), cfg["drift"] * w * (1 - (k - w) / r))
    if s.kind == "outage":
        return np.full(len(k), PARAMS["outage"]["effect"])
    return np.full(len(k), cfg["effect"])  # integration_break, bug_episode: flat while active


def _latent(accounts: list[Account], starts: np.ndarray, shocks: list[Shock], n_weeks: int,
            rng: np.random.Generator) -> np.ndarray:
    """h[t, a] = mu_a + x + offsets, x an AR(1) (phi, sigma) started at its stationary law.

    Equivalent to h_t = mu_t + phi*(h_{t-1} - mu_{t-1}) + sigma*eps_t with the shock offsets
    folded into a time-varying mean. NaN before an account starts.
    """
    lat, ob = PARAMS["latent"], PARAMS["onboarding"]
    n = len(accounts)
    x = np.empty((n_weeks, n))
    x[0] = rng.normal(0, lat["sigma"] / np.sqrt(1 - lat["phi"] ** 2), n)
    eps = rng.normal(0, lat["sigma"], (n_weeks, n))
    for t in range(1, n_weeks):
        x[t] = lat["phi"] * x[t - 1] + eps[t]
    offset = np.zeros((n_weeks, n))
    ramp = ob["start_offset"] * (1 - np.arange(ob["ramp_weeks"]) / ob["ramp_weeks"])
    for a, (acct, s) in enumerate(zip(accounts, starts, strict=True)):
        offset[s : s + len(ramp), a] += ramp[: n_weeks - s]
        if acct.onboarding_failed:
            offset[s : s + ob["fail_weeks"], a] += ob["fail_offset"]
    for s in shocks:
        prof = _profile(s, n_weeks - s.week)
        offset[s.week : s.week + len(prof), s.account] += prof
    h = np.array([a.mu for a in accounts])[None, :] + x + offset
    h[np.arange(n_weeks)[:, None] < starts[None, :]] = np.nan
    return h


def _depart_champions(spec: TenantSpec, accounts: list[Account], contacts: list[Contact],
                      shocks: list[Shock], weeks: list[date], rng: np.random.Generator,
                      seq) -> None:
    """Champion departure: the current champion leaves; a successor appears weeks later."""
    champion = {c.sf_account_id: c for c in contacts if c.champion}
    for s in shocks:
        if s.kind != "champion_departure":
            continue
        a = accounts[s.account]
        old = champion[a.sf_account_id]
        if old.departed_at:  # the successor has not arrived yet
            continue
        old.departed_at = local_dt(weeks[s.week] + timedelta(days=int(rng.integers(5))), 17,
                                   spec.timezone)
        lag = s.info["sf_lag_weeks"]
        old.sf_inactive_at = old.departed_at + timedelta(weeks=lag) if lag else None
        lo, hi = PARAMS["shocks"]["champion_departure"]["successor_weeks"]
        new = _new_contact(spec, a, rng, seq, champion=True,
                           at=old.departed_at + timedelta(weeks=int(rng.integers(lo, hi + 1))))
        contacts.append(new)
        champion[a.sf_account_id] = new


def simulate(slug: str, seed: int = 7, scale: float = 1.0, sim_end: date = date(2027, 4, 30),
             history_end: datetime = datetime(2026, 10, 17, tzinfo=UTC)) -> World:
    spec = TENANTS[slug]
    rng = tenant_rng(slug, seed)
    seq = itertools.count(10_000_000 * list(TENANTS).index(slug) + 1000)
    weeks = [PARAMS["sim_start"] + timedelta(weeks=k)
             for k in range((sim_end - PARAMS["sim_start"]).days // 7 + 1)]
    roster = staff(slug, seed)
    accounts = _make_accounts(spec, round(spec.n_accounts * scale), rng, roster, seq)
    contacts = _make_contacts(spec, accounts, rng, seq)
    starts = np.array([(a.start - weeks[0]).days // 7 for a in accounts])
    shocks = _draw_shocks(slug, accounts, starts, weeks, rng)
    _depart_champions(spec, accounts, contacts, shocks, weeks, rng, seq)
    h = _latent(accounts, starts, shocks, len(weeks), rng)
    decisions, failures = decide(spec, accounts, starts, h, weeks, shocks, rng)

    churn = decisions[decisions.kind == "churn"].set_index("sf_account_id")
    truth = pd.DataFrame({
        "sf_account_id": [a.sf_account_id for a in accounts],
        "name": [a.name for a in accounts],
        "start_date": [a.start for a in accounts],
        "vp_case": [a.vp_case for a in accounts],
    }).join(churn[["cause", "decided_at", "effective_at", "involuntary", "unforecastable"]],
            on="sf_account_id").rename(columns={"decided_at": "decision_at"})
    truth["involuntary"] = truth.involuntary.astype("boolean").fillna(False).astype(bool)
    truth["unforecastable"] = truth.unforecastable.astype("boolean").fillna(False).astype(bool)

    last = [min(len(weeks) - 1, (eff.date() - weeks[0]).days // 7) if pd.notna(eff)
            else len(weeks) - 1 for eff in truth.effective_at]
    idx = [(i, t) for i, s in enumerate(starts) for t in range(s, last[i] + 1)]
    ai, ti = np.array(idx).T
    weekly = pd.DataFrame({"sf_account_id": truth.sf_account_id.to_numpy()[ai],
                           "week_start": np.array(weeks, dtype=object)[ti], "h": h[ti, ai]})
    world = World(slug, spec, seed, scale, history_end, sim_end, roster, accounts, contacts,
                  weeks, shocks, failures, weekly, decisions, truth)
    seq = itertools.count(10_000_000 * list(TENANTS).index(slug) + 5_000_000)
    live = spans(world)
    world.billing = sorted(billing_events(world, rng), key=lambda e: e.at)
    world.crm = sorted(crm_changes(world, rng, seq), key=lambda c: c.at)  # reads billing
    world.tickets = tickets(world, rng, h, live)
    world.emails = emails(world, rng, h, live)
    world.usage = usage(world, rng, h, live)
    world.product_events = product_events(world, rng, h, live)
    return world
