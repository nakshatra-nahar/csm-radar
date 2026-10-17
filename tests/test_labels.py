"""Churn-decision labels (design §5.2) on tiny hand-built frames (no database)."""

from datetime import UTC, datetime

import pandas as pd

from radar.labels import build_training_set, churn_decisions, snapshot_grid
from tests.test_features import DAY, T, World, tenant


def customer(w: World, sf: str, start=T - 300 * DAY, **sub):
    """An SF account with a live monthly Stripe subscription and a Zendesk org."""
    a = w.account(sf)
    w.link(f"stripe:{sf}", a, start, "stripe_metadata")
    w.link(f"zd_org:{sf}", a, start, "zendesk_external_id")
    w.sub(sf, f"stripe:{sf}", start, **sub)
    return a


def ended(w: World, sf: str, at, reason="cancellation_requested", **cols):
    w.sub(sf, f"stripe:{sf}", at, status="canceled", ended_at=at, cancellation_reason=reason,
          **cols)


def cancel_ticket(w: World, sf: str, at, **kw):
    w.ticket(f"c{sf}", f"zd_org:{sf}", at, "open", tags=["cancellation_request"], **kw)


def closed_lost(w: World, sf: str, at, a):
    w.ev("crm.opportunity", f"sf_opp:o{sf}", f"sf:{sf}", at - 30 * DAY, opp_type="Renewal",
         status="Prospecting")
    w.link(f"sf_opp:o{sf}", a, at - 30 * DAY, "sf_opportunity")
    w.ev("crm.opportunity_history", f"sf_opp_hist:h{sf}", f"sf_opp:o{sf}", at,
         status="Closed Lost")


def decisions(w: World) -> pd.DataFrame:
    return churn_decisions(w.frames()).set_index("account_id")


def test_decision_is_earliest_of_four_sources():
    w, t1 = World(), T + 10 * DAY
    a = customer(w, "A")  # notice, then a ticket: the notice is earlier
    w.sub("A", "stripe:A", t1, cancel_at_period_end=True)
    cancel_ticket(w, "A", t1 + 5 * DAY)
    b = customer(w, "B")
    closed_lost(w, "B", t1 + DAY, b)
    c = customer(w, "C")
    cancel_ticket(w, "C", t1 + 2 * DAY)
    d = customer(w, "D")  # ends with no notice
    ended(w, "D", t1 + 3 * DAY)
    e = customer(w, "E")  # a ticket before the Closed Lost row
    cancel_ticket(w, "E", t1)
    closed_lost(w, "E", t1 + 4 * DAY, e)
    got = decisions(w)
    expect = {a: (t1, "stripe_cancel"), b: (t1 + DAY, "closed_lost"),
              c: (t1 + 2 * DAY, "cancellation_ticket"), d: (t1 + 3 * DAY, "no_notice"),
              e: (t1, "cancellation_ticket")}
    for account, (at, source) in expect.items():
        assert got.loc[account, "decision_at"] == at
        assert got.loc[account, "decision_source"] == source
    assert got.loc[d, "ended_at"] == t1 + 3 * DAY


def test_reversed_cancellation_is_not_a_decision():
    w, t1 = World(), T + 10 * DAY
    a = customer(w, "A")  # scheduled, then reversed: saved
    w.sub("A", "stripe:A", t1, cancel_at_period_end=True)
    w.sub("A", "stripe:A", t1 + 14 * DAY)
    b = customer(w, "B")  # reversed, then cancelled again with cancel_at, then ended
    w.sub("B", "stripe:B", t1, cancel_at=t1 + 60 * DAY)
    w.sub("B", "stripe:B", t1 + 7 * DAY)
    w.sub("B", "stripe:B", t1 + 20 * DAY, cancel_at=t1 + 60 * DAY)
    ended(w, "B", t1 + 60 * DAY, cancel_at=t1 + 60 * DAY)
    got = decisions(w)
    assert pd.isna(got.loc[a, "decision_at"])
    assert got.loc[b, "decision_at"] == t1 + 20 * DAY
    assert got.loc[b, "decision_source"] == "stripe_cancel"


def test_involuntary_and_merge_excluded_reactivation_not_churn():
    w, t1 = World(), T + 10 * DAY
    a = customer(w, "A")  # payment failure; the renewal is booked lost afterwards
    ended(w, "A", t1, reason="payment_failed")
    closed_lost(w, "A", t1 + 5 * DAY, a)
    master = customer(w, "M")  # a duplicate SF record merged into M
    loser = w.account("L")
    w.link("sf:L", master, t1, "merge")
    r = customer(w, "R")  # ends, then a new subscription 10 days later
    ended(w, "R", t1)
    w.sub("R2", "stripe:R", t1 + 10 * DAY)
    got = decisions(w)
    assert pd.isna(got.loc[a, "decision_at"]) and got.loc[a, "involuntary_at"] == t1
    assert got.loc[loser, "merged"] == t1 and got.loc[master, "merged"] == t1
    assert pd.isna(got.loc[loser, "decision_at"]) and pd.isna(got.loc[master, "decision_at"])
    assert got.loc[r, "reactivated"] and pd.isna(got.loc[r, "decision_at"])
    assert pd.isna(got.loc[r, "ended_at"])


def test_snapshot_grid_is_mondays_at_midnight_utc():
    grid = snapshot_grid(datetime(2025, 6, 3, 9, tzinfo=UTC), datetime(2025, 6, 23, tzinfo=UTC))
    assert grid == [datetime(2025, 6, 9, tzinfo=UTC), datetime(2025, 6, 16, tzinfo=UTC),
                    datetime(2025, 6, 23, tzinfo=UTC)]


def training(w: World, snapshots, cutoff):
    return build_training_set(w.frames(), tenant(), snapshots, cutoff)


def test_label_window_and_censoring():
    w = World()
    a = customer(w, "A")  # decision exactly at T + 90d: in the window
    cancel_ticket(w, "A", T + 90 * DAY)
    b = customer(w, "B")  # T + 91d: outside it
    cancel_ticket(w, "B", T + 91 * DAY)
    customer(w, "C")  # an involuntary churn inside the window
    ended(w, "C", T + 30 * DAY, reason="payment_failed")
    m = customer(w, "M")  # a merge inside the window
    w.account("L")
    w.link("sf:L", m, T + 20 * DAY, "merge")
    df = training(w, [T, T + 7 * DAY], T + 127 * DAY)
    assert set(df.index.get_level_values("as_of")) == {pd.Timestamp(T)}  # T+7d censored
    labels = df["label"].xs(pd.Timestamp(T), level="as_of")
    assert labels.to_dict() == {a: 1, b: 0}
    assert df.loc[(a, pd.Timestamp(T)), "decision_at"] == T + 90 * DAY


def test_snapshots_after_visible_decision_excluded():
    w = World()
    a = customer(w, "A")  # cancellation visible at T
    w.sub("A", "stripe:A", T - 10 * DAY, cancel_at_period_end=True)
    b = customer(w, "B")  # decided before T but not yet delivered: also no label
    cancel_ticket(w, "B", T - DAY, available=T + DAY)
    c = customer(w, "C")
    df = training(w, [T], T + 200 * DAY)
    assert list(df.index.get_level_values("account_id")) == [c]
    assert a not in df.index.get_level_values("account_id")
    assert b not in df.index.get_level_values("account_id")


def test_annual_out_of_window_not_eligible():
    w = World()
    far = customer(w, "F", interval="year", current_period_end=T + 300 * DAY)
    near = customer(w, "N", interval="year", current_period_end=T + 100 * DAY)
    df = training(w, [T], T + 200 * DAY)
    accounts = set(df.index.get_level_values("account_id"))
    assert near in accounts and far not in accounts
