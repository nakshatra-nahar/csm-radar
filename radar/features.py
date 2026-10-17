"""Point-in-time features, metrics and must-act facts (design §5). Every function here reads a
Snapshot: what was visible at one as_of, attributed to accounts with the links usable then.

Each feature, metric and rule is registered in radar/registry.py under its dotted path; the
model manifest hashes these functions' source, so editing one invalidates trained models.
"""

from datetime import UTC, datetime

import numpy as np
import pandas as pd

from radar import registry
from radar.asof import Frames, resolver, visible
from radar.ingest.normalize import HARD_BOUNCE
from radar.tenants import TENANTS

DAY = pd.Timedelta(days=1)
OPEN = ["new", "open", "pending", "hold"]
URGENT = ["high", "urgent"]
LIVE = ["active", "past_due", "trialing"]  # a subscription the customer still has
BROKEN = ["integration_sync_error", "integration_disconnected"]
FIXED = "integration_reconnected"
EMAIL_CAP = 120.0


class Snapshot:
    """What could be known at T. ev/state results are memoised; so is anything via get()."""

    def __init__(self, frames: Frames, tenant: dict, T: datetime, hindsight: bool = False,
                 by_type: dict | None = None):
        self.T, self.tenant, self.hindsight = pd.Timestamp(T), tenant, hindsight
        self.frames = frames
        self.by_type = by_type if by_type is not None else split(frames)
        self.refs, self.master = resolver(frames.links, None if hindsight else T)
        self.cache: dict = {}
        acct = self.state("crm.account", attributed=False)
        sf_ids = frames.accounts.set_index("sf_account_id")["id"]
        acct = acct.assign(account_id=acct["entity_id"].str.removeprefix("sf_account:")
                           .map(sf_ids)).dropna(subset=["account_id"])
        internal = acct["domain"] == tenant.get("internal_domain")
        gone = acct["is_test"].eq(True) | internal | acct["is_deleted"].eq(True)
        acct = acct[~gone & ~acct["account_id"].isin(self.master.index)]
        self.account = acct.set_index("account_id")  # latest SF account row per account
        self.index = self.account.index

    def ev(self, type_: str, attributed: bool = True) -> pd.DataFrame:
        """Every visible version of every event of a type (with account_id if attributed)."""
        key = ("ev", type_, attributed)
        if key not in self.cache:
            df = self.by_type.get(type_, self.frames.events.iloc[:0])
            df = visible(df, self.T, self.hindsight)
            if attributed:
                df = df.assign(account_id=df["account_ref"].map(self.refs))
                df = df[df["account_id"].isin(self.index)]
            self.cache[key] = df
        return self.cache[key]

    def state(self, type_: str, at: pd.Timestamp | None = None,
              attributed: bool = True) -> pd.DataFrame:
        """Each entity's latest version as of `at` (default T), as known at T."""
        df = self.ev(type_, attributed)
        if at is not None:
            df = df[df["occurred_at"] <= at]
        return df.drop_duplicates("entity_id", keep="last")

    def get(self, fn):
        if fn not in self.cache:
            self.cache[fn] = fn(self)
        return self.cache[fn]

    def series(self, values, fill=np.nan) -> pd.Series:
        return pd.Series(values, dtype=float).reindex(self.index, fill_value=fill)

    def source_start(self, source: str) -> pd.Timestamp:
        return pd.Timestamp(TENANTS[self.tenant["slug"]].source_start[source], tz=UTC)


def split(frames: Frames) -> dict[str, pd.DataFrame]:
    """Events by type, each still sorted by (entity_id, occurred_at, version_key)."""
    return {t: g for t, g in frames.events.groupby("type", sort=False)}


def _lookup(keys: pd.Series, table: pd.Series) -> pd.Series:
    """keys.map(table); pandas 3 cannot map through an empty datetime-valued table."""
    return table.reindex(keys).set_axis(keys.index)


def _month(at: pd.Series) -> pd.Series:
    """A usage row's month number; occurred_at is the UTC midnight after its last day."""
    last = at - DAY
    return last.dt.year * 12 + last.dt.month - 1


def _refs(s: Snapshot, prefix: str) -> pd.Series:
    """The account handles of one source usable at T (ref -> account_id)."""
    return s.refs[s.refs.index.str.startswith(prefix)]


def _usage_rows(s: Snapshot) -> pd.DataFrame:
    """Each (workspace, month)'s latest visible restatement, for workspaces linked at T."""
    u = s.state("product.usage", attributed=False)
    u = u[u["account_ref"].isin(_refs(s, "ws:").index)]
    return u.assign(month=_month(u["occurred_at"]))


def watermarks(frames: Frames, T: datetime, hindsight: bool = False,
               snapshot: Snapshot | None = None) -> dict[str, datetime | None]:
    """Product: the end of the latest month that >= 90% of the linked workspaces reporting the
    month before have reported. Other sources: the max visible available_at."""
    s = snapshot or Snapshot(frames, {}, T, hindsight)
    out = {src: g["available_at"].max() for src, g in
           visible(frames.events, s.T, hindsight).groupby("source")}
    u = s.get(_usage_rows)
    seen = pd.crosstab(u["account_ref"], u["month"]).astype(bool)
    if seen.shape[1]:
        seen = seen.reindex(columns=range(seen.columns.min(), seen.columns.max() + 1),
                            fill_value=False)
    # expected reporters: the workspaces that reported the month before (churned ones stop)
    before = seen.shift(1, axis=1, fill_value=False)
    coverage = (seen & before).sum() / before.sum().replace(0, np.nan)
    complete = coverage.index[coverage >= 0.9]
    out["product"] = (u.loc[u["month"] == complete.max(), "occurred_at"].max()
                      if len(complete) else None)
    return out


def _watermarks(s: Snapshot) -> dict:
    return watermarks(s.frames, s.T, s.hindsight, snapshot=s)


def _monthly_users(s: Snapshot) -> pd.DataFrame:
    """active_users per account for the last 4 complete months (columns 0 = latest)."""
    end = s.get(_watermarks)["product"]
    cols = range(4)
    if end is None:
        return pd.DataFrame(np.nan, index=s.index, columns=cols)
    u = s.get(_usage_rows)
    u = u.assign(account_id=u["account_ref"].map(s.refs))
    last = _month(pd.Series([end]))[0]
    u = u[(u["month"] <= last) & (u["month"] > last - 4)]
    table = u.pivot_table(index="account_id", columns="month", values="active_users",
                          aggfunc="sum")
    table = table.reindex(index=s.index, columns=range(last, last - 4, -1))
    table.columns = cols
    return table


def _telemetry(s: Snapshot) -> pd.Series:
    """True if a workspace is linked to the account at T."""
    return pd.Series(s.index.isin(_refs(s, "ws:")), index=s.index)


def _product(s: Snapshot, values: pd.Series) -> pd.Series:
    """Product features are unknown, not zero, for accounts without telemetry."""
    return values.where(s.get(_telemetry))


def _subs(s: Snapshot, at: pd.Timestamp | None = None) -> pd.DataFrame:
    """Subscriptions the customer still has as of `at` (default T)."""
    subs = s.state("billing.subscription", at)
    return subs[subs["status"].isin(LIVE)]


def _seats(s: Snapshot, at: pd.Timestamp | None = None) -> pd.Series:
    return s.series(_subs(s, at).groupby("account_id")["quantity"].sum())


def _contract_start(s: Snapshot) -> pd.Series:
    return s.ev("billing.subscription").groupby("account_id")["start_date"].min().reindex(
        s.index)


def _stripe_linked(s: Snapshot) -> pd.Series:
    return pd.Series(s.index.isin(_refs(s, "stripe:")), index=s.index)


# --- features (design §5.1, in registry order) ---------------------------------------------

def usage_drop(s: Snapshot) -> pd.Series:
    """A hinge: growth reads 0, so it cannot offset a low level in the model (Task 5b)."""
    m = s.get(_monthly_users)
    raw = np.log((m[0] + 1) / (m[[1, 2, 3]].mean(axis=1, skipna=False) + 1))
    return _product(s, np.maximum(0.0, -(raw - raw.median())))


def seat_utilization(s: Snapshot) -> pd.Series:
    """A 3-month level: with one noisy month as the level, the earlier months behind
    usage_drop measure the level too and its sign flips (Task 5b)."""
    level = s.get(_monthly_users)[[0, 1, 2]].mean(axis=1)
    return _product(s, level / _seats(s).replace(0, np.nan))


def _broken_since(df: pd.DataFrame, broken: pd.Series, key: str) -> pd.Series:
    """When each key's current run of broken rows began (only keys whose latest row is
    broken, since a later healthy row ends the run)."""
    last_ok = df["occurred_at"].where(~broken).groupby(df[key]).transform("max")
    after = ~(df["occurred_at"] <= last_ok)  # NaT: never healthy
    return df[broken & after].groupby(key)["occurred_at"].min()


def integration_broken(s: Snapshot) -> pd.Series:
    e = s.ev("product.event")
    e = e[e["product_event"].isin([*BROKEN, FIXED])]
    since = _broken_since(e, e["product_event"].isin(BROKEN), "account_id")
    return _product(s, s.series(s.T - since >= 7 * DAY, fill=0.0))


def not_live_after_30d(s: Snapshot) -> pd.Series:
    """Only for contracts that started once product telemetry existed: before that the
    go-live milestone could not have been observed."""
    start = s.get(_contract_start)
    e = s.ev("product.event")
    live = e.loc[e["product_event"] == "workspace_went_live", "account_id"]
    flag = (start >= s.source_start("product")) & (s.T - start >= 30 * DAY)
    return _product(s, (flag & ~s.index.isin(live)).astype(float))


def open_urgent_tickets(s: Snapshot) -> pd.Series:
    t = s.state("support.ticket")
    t = t[t["status"].isin(OPEN) & t["priority"].isin(URGENT)]
    return np.log1p(s.series(t.groupby("account_id").size(), fill=0.0))


def support_friction_90d(s: Snapshot) -> pd.Series:
    t = s.ev("support.ticket")  # every visible version, in (ticket, time) order
    solved = t["status"] == "solved"
    solved_before = (solved.astype(int).groupby(t["entity_id"]).cumsum() - solved) > 0
    reopened = t["status"].isin(["new", "open"]) & solved_before
    hit = (reopened | (t["csat"] == "bad")) & (t["occurred_at"] > s.T - 90 * DAY)
    return np.log1p(s.series(t[hit].groupby("account_id")["entity_id"].nunique(), fill=0.0))


def days_since_customer_email(s: Snapshot) -> pd.Series:
    """Measured to the Gmail watermark, so a late mailbox sync does not look like silence."""
    mark = min(s.get(_watermarks).get("gmail") or s.T, s.T)
    e = _inbound(s)
    e = e[e["occurred_at"] <= mark]
    days = ((mark - e.groupby("account_id")["occurred_at"].max()) / DAY).clip(upper=EMAIL_CAP)
    connected = s.source_start("gmail") <= s.T
    return s.series(days, fill=EMAIL_CAP if connected else np.nan)


def seat_change_180d(s: Snapshot) -> pd.Series:
    before = _seats(s, s.T - 180 * DAY).replace(0, np.nan)
    return ((_seats(s) - before) / before).clip(-1, 1)


def arr(s: Snapshot) -> pd.DataFrame:
    """arr v1 in minor units with its source. Stripe items; SF ARR__c only without a Stripe
    link (an SF value read as of T, never current state)."""
    subs = _subs(s).explode("items").dropna(subset=["items"])
    items = pd.DataFrame(subs["items"].tolist(), index=subs["account_id"],
                         columns=["unit_amount", "quantity", "interval", "interval_count"])
    months = items["interval_count"] * np.where(items["interval"] == "year", 12, 1)
    stripe = (items["unit_amount"] * items["quantity"] * 12 / months).groupby(level=0).sum()
    linked = s.get(_stripe_linked)
    minor = s.series(stripe).where(linked, s.account["arr_minor"].astype(float))
    source = np.select([linked & minor.notna(), ~linked & minor.notna()],
                       ["stripe", "salesforce"], None)
    return pd.DataFrame({"arr_minor": minor, "arr_source": source}, index=s.index)


def log_arr(s: Snapshot) -> pd.Series:
    minor = s.get(arr)["arr_minor"]
    return np.log(minor.where(minor > 0) / 100)


def tenure_years(s: Snapshot) -> pd.Series:
    return np.log1p((s.T - s.get(_contract_start)) / (365.25 * DAY))


def term_end(s: Snapshot) -> pd.DataFrame:
    """term_end v1 and whether the account is month-to-month."""
    subs = _subs(s)
    subs = subs.assign(interval=subs["items"].map(lambda items: items[0]["interval"]))
    yearly = subs[subs["interval"] == "year"].groupby("account_id")["current_period_end"].max()
    # monthly subs roll their period forward on invoice lines only (no renewal event)
    inv = s.ev("billing.invoice").groupby("subscription_id")["invoice_period_end"].max()
    monthly = subs[subs["interval"] != "year"]
    rolled = _lookup(monthly["entity_id"].str.removeprefix("stripe_sub:"), inv)
    period = pd.concat([monthly["current_period_end"], rolled], axis=1).max(axis=1)
    period = period.groupby(monthly["account_id"]).max().reindex(s.index)
    renewal = s.account["renewal_date"]  # 12-month contracts billed monthly
    yearly = yearly.reindex(s.index)
    out = pd.DataFrame({"term_end": yearly.fillna(renewal).fillna(period)}, index=s.index)
    out["month_to_month"] = yearly.isna() & renewal.isna()
    return out


def term_end_within_60d(s: Snapshot) -> pd.Series:
    t = s.get(term_end)
    soon = (t["term_end"] - s.T <= 60 * DAY).astype(float).where(t["term_end"].notna())
    return soon.mask(t["month_to_month"], 1.0)


def billing_month_to_month(s: Snapshot) -> pd.Series:
    return s.get(term_end)["month_to_month"].astype(float)


def no_telemetry(s: Snapshot) -> pd.Series:
    return (~s.get(_telemetry)).astype(float)


# --- context (design §5.2) -----------------------------------------------------------------

def _active(s: Snapshot) -> pd.Series:
    return pd.Series(s.index.isin(_subs(s)["account_id"]), index=s.index)


def _in_dunning(s: Snapshot) -> pd.Series:
    inv = s.state("billing.invoice")
    failing = inv[(inv["status"] == "open") & (inv["event_type"] == "invoice.payment_failed")]
    subs = _subs(s)
    due = subs[subs["status"] == "past_due"]
    return pd.Series(s.index.isin(failing["account_id"]) | s.index.isin(due["account_id"]),
                     index=s.index)


def _tagged(tickets: pd.DataFrame, tag: str) -> pd.Series:
    return tickets["tags"].map(lambda tags: tag in (tags or []))


def _decision_visible(s: Snapshot) -> pd.Series:
    """A churn decision (design §5.2 churn_decision v1) is already known at T."""
    t = s.ev("support.ticket")
    opp = s.state("crm.opportunity")
    lost = opp[(opp["opp_type"] == "Renewal") & (opp["status"] == "Closed Lost")]
    seen = (s.index.isin(s.get(cancel_announced).index)
            | s.index.isin(t.loc[_tagged(t, "cancellation_request"), "account_id"])
            | s.index.isin(lost["account_id"]))
    return pd.Series(seen, index=s.index)


def _inbound(s: Snapshot) -> pd.DataFrame:
    e = s.ev("email.message")
    return e[(e["direction"] == "inbound") & ~e["is_auto_reply"].eq(True)
             & ~e["is_dsn"].eq(True)]


def coverage_ok(s: Snapshot) -> pd.Series:
    """Billing plus at least one of telemetry, a ticket in 365 days, an inbound email in 180."""
    t = s.ev("support.ticket")
    e = _inbound(s)
    other = (s.get(_telemetry)
             | s.index.isin(t.loc[t["ticket_created_at"] > s.T - 365 * DAY, "account_id"])
             | s.index.isin(e.loc[e["occurred_at"] > s.T - 180 * DAY, "account_id"]))
    return s.get(_stripe_linked) & other


def eligible(s: Snapshot) -> pd.Series:
    window = s.tenant["config"]["eligibility_window_days"] * DAY
    t = s.get(term_end)
    in_window = t["month_to_month"] | (t["term_end"] - s.T <= window)
    return s.get(_active) & ~s.get(_in_dunning) & in_window


def _sources_connected(s: Snapshot) -> bool:
    """Every source connected 120 days before T (Task 5's training population)."""
    sources = TENANTS[s.tenant["slug"]].source_start
    return all(s.source_start(source) <= s.T - 120 * DAY for source in sources)


def _owner(s: Snapshot) -> pd.Series:
    column = {"OwnerId": "owner_sf_id", "CSM__c": "csm_sf_id"}[
        s.tenant["config"]["sf"]["csm_field"]]
    users = s.frames.users.dropna(subset=["sf_user_id"]).set_index("sf_user_id")["id"]
    return s.account[column].map(users)


# --- must-act rules (design §5.2): each returns the evidence event ids of firing accounts --

def _evidence(df: pd.DataFrame) -> pd.Series:
    return df.groupby("account_id")["id"].agg(lambda ids: sorted(int(i) for i in ids))


def cancel_announced(s: Snapshot) -> pd.Series:
    subs = _subs(s)
    cancelling = subs[subs["cancel_at_period_end"].eq(True) | subs["cancel_at"].notna()]
    t = s.state("support.ticket")
    asked = t[t["status"].isin(OPEN) & _tagged(t, "cancellation_request")]
    return _evidence(pd.concat([cancelling, asked]))


def dunning_exhausted(s: Snapshot) -> pd.Series:
    """Not on the first failure: Smart Retries are still running then."""
    inv = s.state("billing.invoice")
    inv = inv[(inv["status"] == "open") & (inv["event_type"] == "invoice.payment_failed")
              & ((inv["attempt_count"] >= 3) | inv["next_payment_attempt"].isna())]
    versions = s.ev("billing.subscription")
    since = _broken_since(versions, versions["status"] == "past_due", "entity_id")
    subs = _subs(s)
    overdue = subs[_lookup(subs["entity_id"], s.T - since) > 7 * DAY]
    hits = pd.concat([inv, overdue])
    return _evidence(hits[hits["account_id"].isin(subs["account_id"])])


def champion_bounced(s: Snapshot) -> pd.Series:
    contacts = s.state("crm.contact", attributed=False)
    champions = contacts.loc[contacts["champion"].eq(True), "email"]
    e = s.ev("email.message")
    hard = e[e["is_dsn"].eq(True) & e["dsn_status"].isin(HARD_BOUNCE)
             & e["failed_recipient"].isin(champions) & (e["occurred_at"] > s.T - 30 * DAY)]
    return _evidence(hard)


def urgent_ticket_stalled(s: Snapshot) -> pd.Series:
    t = s.state("support.ticket")
    return _evidence(t[t["status"].isin(OPEN) & t["priority"].isin(URGENT)
                       & (t["ticket_created_at"] < s.T - 3 * DAY)])


# --- entry points --------------------------------------------------------------------------

def _one(s: Snapshot) -> pd.DataFrame:
    df = pd.DataFrame({d.name: registry.impl(d)(s) for d in registry.FEATURES}, index=s.index)
    t = s.get(term_end)
    return df.assign(
        arr_minor=s.get(arr)["arr_minor"], arr_source=s.get(arr)["arr_source"],
        term_end=t["term_end"], month_to_month=t["month_to_month"], owner_user_id=s.get(_owner),
        active=s.get(_active), in_dunning=s.get(_in_dunning),
        decision_visible=s.get(_decision_visible), coverage_ok=coverage_ok(s),
        telemetry_linked=s.get(_telemetry), eligible=eligible(s),
        sources_connected=_sources_connected(s))


def compute_features(frames: Frames, tenant: dict, as_of: list[datetime],
                     account_ids: list | None = None, hindsight: bool = False) -> pd.DataFrame:
    """Features (registry order) and context per (account_id, as_of). The tenant median in
    usage_drop is over every account, whatever account_ids selects."""
    by_type = split(frames)
    out = []
    for T in as_of:
        df = _one(Snapshot(frames, tenant, T, hindsight, by_type))
        if account_ids is not None:
            df = df[df.index.isin(account_ids)]
        out.append(df.assign(as_of=pd.Timestamp(T)).set_index("as_of", append=True))
    return pd.concat(out)


def rule_facts(frames: Frames, tenant: dict, T: datetime, account_ids=None) -> pd.DataFrame:
    """Per account at T: a bool per must-act rule and evidence_<rule> (event ids)."""
    s = Snapshot(frames, tenant, T)
    out = pd.DataFrame(index=s.index)
    for d in registry.RULES:
        evidence = registry.impl(d)(s).reindex(s.index)
        out[d.name] = evidence.notna()
        out[f"evidence_{d.name}"] = evidence.map(lambda ids: ids if isinstance(ids, list)
                                                 else [])
    return out if account_ids is None else out[out.index.isin(account_ids)]
