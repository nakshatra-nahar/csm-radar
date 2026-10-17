"""Churn-decision labels (design §5.2 churn_decision v1 and churn_label_90d v1).

Labels use hindsight: every event counts, whenever it arrived. Accounts are attributed with
every identity link, merges included, through the same resolver the features use.
"""

from datetime import datetime

import pandas as pd

from radar.asof import Frames, resolver
from radar.features import DAY, LIVE, _lookup, _tagged, compute_features

WINDOW = 90 * DAY
REACTIVATION = 30 * DAY
# a label at T is settled once its window, the reactivation check and a week of delivery lag
# have passed (design §5.2: T + 90d + 30d + 7d)
MATURITY = WINDOW + REACTIVATION + 7 * DAY


def _subscriptions(subs: pd.DataFrame) -> pd.DataFrame:
    """Per subscription: account, first live time, end, involuntary, standing notice, and
    whether another live subscription of the account covers its end (reactivation)."""
    live = subs["status"].isin(LIVE)
    notice = live & (subs["cancel_at_period_end"].eq(True) | subs["cancel_at"].notna())
    # a later live version without notice reverses the cancellation (it was saved)
    cleared = subs["occurred_at"].where(live & ~notice).groupby(subs["entity_id"]).transform(
        "max")
    standing = notice & ~(subs["occurred_at"] <= cleared)
    s = pd.DataFrame({
        "account_id": subs.groupby("entity_id")["account_id"].first(),
        "live_from": subs[live].groupby("entity_id")["occurred_at"].min(),
        "end": subs.groupby("entity_id")["ended_at"].min(),
        "notice_at": subs[standing].groupby("entity_id")["occurred_at"].min()})
    s["involuntary"] = s.index.isin(
        subs.loc[subs["cancellation_reason"] == "payment_failed", "entity_id"])
    pairs = s.reset_index().merge(s.reset_index(), on="account_id", suffixes=("", "_other"))
    pairs = pairs[pairs["entity_id"] != pairs["entity_id_other"]]
    covers = ((pairs["live_from_other"] <= pairs["end"] + REACTIVATION)
              & ~(pairs["end_other"] <= pairs["end"]))
    s["covered"] = covers.groupby(pairs["entity_id"]).any().reindex(s.index, fill_value=False)
    return s


def _merged(frames: Frames) -> pd.Series:
    """When each merged-away account and its master were merged (the merge link's time)."""
    m = frames.links[frames.links["method"] == "merge"]
    sf_ids = frames.accounts.set_index("sf_account_id")["id"]
    loser = m["account_ref"].str.removeprefix("sf:").map(sf_ids)
    at = pd.concat([pd.Series(m["available_at"].to_numpy(), index=loser),
                    pd.Series(m["available_at"].to_numpy(), index=m["account_id"])])
    return at.groupby(level=0).min()


def churn_decisions(frames: Frames) -> pd.DataFrame:
    """One row per account: decision_at (the earliest of a standing Stripe cancellation, the
    first Closed Lost history row on a renewal opportunity, a cancellation_request ticket, or
    an end with no notice), decision_source, involuntary_at, merged (the merge time), ended_at
    and reactivated.

    shortcut: one decision per account (its first): an account that churns, returns as a new
    customer and churns again keeps only the first; track churn episodes if returns appear."""
    refs, _ = resolver(frames.links, None)
    ev = frames.events.assign(account_id=frames.events["account_ref"].map(refs))
    ev = ev.dropna(subset=["account_id"])
    s = _subscriptions(ev[ev["type"] == "billing.subscription"])
    churned = s[s["end"].notna() & ~s["covered"]]
    involuntary_at = churned[churned["involuntary"]].groupby("account_id")["end"].min()

    opp = frames.events[frames.events["type"] == "crm.opportunity"].drop_duplicates(
        "entity_id", keep="last")
    renewals = opp.loc[opp["opp_type"] == "Renewal", "entity_id"]
    hist = ev[(ev["type"] == "crm.opportunity_history") & (ev["status"] == "Closed Lost")
              & ev["account_ref"].isin(renewals)]
    tickets = ev[ev["type"] == "support.ticket"]
    tickets = tickets[_tagged(tickets, "cancellation_request").astype(bool)]  # may be empty
    notice = s[~s["covered"] & s["notice_at"].notna()]
    silent = churned[~churned["involuntary"] & churned["notice_at"].isna()]

    def candidates(account_id, at, source):
        return pd.DataFrame({"account_id": account_id.to_numpy(), "at": at.to_numpy(),
                             "source": source})

    c = pd.concat([candidates(notice["account_id"], notice["notice_at"], "stripe_cancel"),
                   candidates(hist["account_id"], hist["occurred_at"], "closed_lost"),
                   candidates(tickets["account_id"], tickets["occurred_at"],
                              "cancellation_ticket"),
                   candidates(silent["account_id"], silent["end"], "no_notice")])
    # after a payment-failure cancellation the renewal is booked lost: bookkeeping, not churn
    c = c[~(c["at"] >= _lookup(c["account_id"], involuntary_at))]
    first = c.sort_values("at", kind="stable").drop_duplicates("account_id").set_index(
        "account_id")

    out = pd.DataFrame(index=pd.Index(frames.accounts["id"], name="account_id"))
    out["decision_at"] = first["at"]
    out["decision_source"] = first["source"]
    out["involuntary_at"] = involuntary_at
    out["merged"] = _merged(frames)
    out["ended_at"] = churned.groupby("account_id")["end"].min()
    out["reactivated"] = out.index.isin(s.loc[s["covered"], "account_id"])
    return out.reset_index()


def snapshot_grid(start: datetime, end: datetime) -> list[datetime]:
    """Mondays 00:00 UTC in [start, end]."""
    return list(pd.date_range(pd.Timestamp(start).ceil("D"), end, freq="W-MON"))


def build_training_set(frames: Frames, tenant: dict, snapshots: list[datetime],
                       label_cutoff: datetime) -> pd.DataFrame:
    """Features and context per (account_id, as_of) with label (1 iff a churn decision falls in
    (T, T+90d]) and decision_at. Only eligible snapshots of fully connected tenants; excluded:
    a decision already visible at T, dunning (eligible already excludes it), an involuntary
    churn or merge inside the window, and windows not settled by label_cutoff."""
    snapshots = [T for T in snapshots if pd.Timestamp(T) + MATURITY <= label_cutoff]
    df = compute_features(frames, tenant, snapshots)
    df = df[df["eligible"] & df["sources_connected"] & ~df["decision_visible"]]
    d = churn_decisions(frames).set_index("account_id")
    accounts = df.index.get_level_values("account_id")
    T = pd.Series(df.index.get_level_values("as_of"), index=df.index)

    def at(col: str) -> pd.Series:
        return d[col].reindex(accounts).set_axis(df.index)

    def in_window(col: str) -> pd.Series:
        return (at(col) > T) & (at(col) <= T + WINDOW)

    # a decision made by T but not yet visible cannot be predicted, and is not a 0 either
    drop = in_window("involuntary_at") | in_window("merged") | (at("decision_at") <= T)
    return df[~drop].assign(label=in_window("decision_at")[~drop].astype(int),
                            decision_at=at("decision_at")[~drop])
