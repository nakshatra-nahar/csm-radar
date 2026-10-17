"""The as-of rule (design §2), in one place: a record is visible at T iff
available_at <= T and occurred_at <= T; an entity's state at T is its latest visible version by
(occurred_at, version_key); an identity link is usable at T iff available_at <= T.
"""

import re
from dataclasses import dataclass
from datetime import datetime

import pandas as pd

# attrs the features read, expanded to columns (design Appendix B; names that clash with an
# event column or between types are prefixed)
ATTRS = """
 (attrs->>'is_test')::bool AS is_test, attrs->>'domain' AS domain,
 attrs->>'owner_sf_id' AS owner_sf_id, attrs->>'csm_sf_id' AS csm_sf_id,
 (attrs->>'arr_minor')::bigint AS arr_minor, (attrs->>'renewal_date')::date AS renewal_date,
 (attrs->>'is_deleted')::bool AS is_deleted,
 attrs->>'email' AS email, (attrs->>'champion')::bool AS champion, attrs->>'type' AS opp_type,
 attrs->'items' AS items, (attrs->>'cancel_at_period_end')::bool AS cancel_at_period_end,
 to_timestamp((attrs->>'cancel_at')::bigint) AS cancel_at,
 to_timestamp((attrs->>'current_period_end')::bigint) AS current_period_end,
 to_timestamp((attrs->>'start_date')::bigint) AS start_date,
 attrs->>'event_type' AS event_type, (attrs->>'attempt_count')::int AS attempt_count,
 to_timestamp((attrs->>'next_payment_attempt')::bigint) AS next_payment_attempt,
 attrs->>'subscription_id' AS subscription_id,
 CASE WHEN type = 'billing.invoice'
      THEN to_timestamp((attrs->>'period_end')::bigint) END AS invoice_period_end,
 attrs->'tags' AS tags, attrs->>'csat' AS csat,
 (attrs->>'created_at')::timestamptz AS ticket_created_at,
 attrs->>'direction' AS direction, (attrs->>'is_auto_reply')::bool AS is_auto_reply,
 (attrs->>'is_dsn')::bool AS is_dsn, attrs->>'dsn_status' AS dsn_status,
 attrs->>'failed_recipient' AS failed_recipient,
 (attrs->'metrics'->>'active_users')::int AS active_users, attrs->>'event' AS product_event
"""
EVENT_COLUMNS = ["id", "source", "type", "entity_id", "account_ref", "occurred_at",
                 "available_at", "version_key", "status", "priority", "amount_minor",
                 "quantity", *re.findall(r" AS (\w+)", ATTRS)]
TIMES = ["occurred_at", "available_at", "renewal_date", "cancel_at", "current_period_end",
         "start_date", "next_payment_attempt", "invoice_period_end", "ticket_created_at"]


@dataclass
class Frames:
    events: pd.DataFrame  # sorted by (entity_id, occurred_at, version_key)
    links: pd.DataFrame  # sorted by (account_ref, available_at, id)
    accounts: pd.DataFrame  # id, sf_account_id: the key crosswalk only, never its state
    contacts: pd.DataFrame
    users: pd.DataFrame  # id, sf_user_id


def frames_from_rows(events, links, accounts, contacts, users) -> Frames:
    ev = pd.DataFrame(events, columns=EVENT_COLUMNS)
    for col in TIMES:  # renewal_date (a date) becomes UTC midnight
        ev[col] = pd.to_datetime(ev[col], utc=True)
    ln = pd.DataFrame(links, columns=["id", "account_ref", "account_id", "method",
                                      "available_at"])
    ln["available_at"] = pd.to_datetime(ln["available_at"], utc=True)
    return Frames(
        ev.sort_values(["entity_id", "occurred_at", "version_key"], kind="stable",
                       ignore_index=True),
        ln.sort_values(["account_ref", "available_at", "id"], ignore_index=True),
        pd.DataFrame(accounts, columns=["id", "sf_account_id"]),
        pd.DataFrame(contacts, columns=["id", "account_id", "sf_contact_id", "email"]),
        pd.DataFrame(users, columns=["id", "sf_user_id"]))


def load_frames(conn, tenant_id) -> Frames:
    """One query per table, in the caller's tenant transaction."""
    def q(sql):
        return conn.execute(sql, (tenant_id,)).fetchall()
    return frames_from_rows(
        q("SELECT id, source, type, entity_id, account_ref, occurred_at, available_at,"
          " version_key, status, priority, amount_minor, quantity," + ATTRS +
          " FROM events WHERE tenant_id = %s"),
        q("SELECT id, account_ref, account_id, method, available_at FROM identity_links"
          " WHERE tenant_id = %s"),
        q("SELECT id, sf_account_id FROM accounts WHERE tenant_id = %s"),
        q("SELECT id, account_id, sf_contact_id, email FROM contacts WHERE tenant_id = %s"),
        q("SELECT id, sf_user_id FROM users WHERE tenant_id = %s"))


def visible(df: pd.DataFrame, T: datetime, hindsight: bool = False) -> pd.DataFrame:
    """Rows visible at T. hindsight=True ignores available_at (the leakage comparison only)."""
    mask = df["occurred_at"] <= T
    if not hindsight:
        mask &= df["available_at"] <= T
    return df[mask]


def latest(df: pd.DataFrame, T: datetime, key: str = "entity_id",
           hindsight: bool = False) -> pd.DataFrame:
    """Each entity's latest visible version (df sorted by key, occurred_at, version_key)."""
    return visible(df, T, hindsight).drop_duplicates(key, keep="last")


def resolver(links: pd.DataFrame, T: datetime | None) -> tuple[pd.Series, pd.Series]:
    """(account_ref -> account_id, merged loser -> master) at T (None: every link, for
    hindsight). Once a merge link for an account's sf: ref is usable, all its refs go to the
    master."""
    usable = links if T is None else links[links["available_at"] <= T]
    current = usable.drop_duplicates("account_ref", keep="last").set_index("account_ref")
    merges = current[current["method"] == "merge"]
    losers = usable[usable["account_ref"].isin(merges.index) & (usable["method"] != "merge")]
    master = pd.Series(merges.loc[losers["account_ref"], "account_id"].to_numpy(),
                       index=losers["account_id"].to_numpy())
    master = master[~master.index.duplicated(keep="last")]
    target = current["account_id"]
    for _ in range(3):  # a master merged again later; chains are rare and short
        target = target.map(master).fillna(target)
    return target, master


def attribute(df: pd.DataFrame, links: pd.DataFrame, T: datetime | None) -> pd.Series:
    """account_id per row, from the links usable at T."""
    return df["account_ref"].map(resolver(links, T)[0])
