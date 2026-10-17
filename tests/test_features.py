"""Feature semantics on tiny hand-built frames (no database)."""

import math
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pandas as pd
import pytest

from radar import asof, features, registry
from radar.asof import frames_from_rows
from radar.features import compute_features, rule_facts, watermarks
from radar.registry import BANNED, FEATURE_NAMES, manifest
from radar.tenants import runtime_config

T = datetime(2025, 6, 2, 12, tzinfo=UTC)  # a Monday
DAY = timedelta(days=1)
SOURCE = {"crm": "salesforce", "billing": "stripe", "support": "zendesk", "email": "gmail",
          "product": "product"}


def tenant(slug="hearthline", domain="hearthline.io"):
    return {"slug": slug, "internal_domain": domain, "config": runtime_config(slug)}


class World:
    """Events, links and accounts as load_frames would return them."""

    def __init__(self):
        self.events, self.links, self.accounts, self.users = [], [], [], []

    def ev(self, type_, entity, ref, occurred, available=None, version_key=None, **cols):
        eid = len(self.events) + 1
        self.events.append({
            "id": eid, "source": SOURCE[type_.split(".")[0]], "type": type_,
            "entity_id": entity, "account_ref": ref, "occurred_at": occurred,
            "available_at": available or occurred,
            "version_key": version_key or f"{occurred.isoformat()}:{eid:06d}"} | cols)
        return eid

    def link(self, ref, account_id, at, method="sf_id"):
        self.links.append({"id": len(self.links) + 1, "account_ref": ref,
                           "account_id": account_id, "method": method, "available_at": at})

    def account(self, sf, at=T - 400 * DAY, **attrs):
        """An SF account (visible from `at`) with its sf: link."""
        account_id = uuid4()
        self.accounts.append({"id": account_id, "sf_account_id": sf})
        self.ev("crm.account", f"sf_account:{sf}", f"sf:{sf}", at,
                **{"is_test": False, "is_deleted": False, "domain": f"{sf}.com"} | attrs)
        self.link(f"sf:{sf}", account_id, at)
        return account_id

    def sub(self, sub_id, ref, occurred, qty=1, status="active", unit=1000, interval="month",
            available=None, **cols):
        items = [{"unit_amount": unit, "quantity": qty, "interval": interval,
                  "interval_count": 1}]
        return self.ev("billing.subscription", f"stripe_sub:{sub_id}", ref, occurred, available,
                       status=status, quantity=qty, items=items,
                       **{"cancel_at_period_end": False, "start_date": T - 300 * DAY} | cols)

    def ticket(self, tid, ref, occurred, status, priority="normal", available=None, **cols):
        return self.ev("support.ticket", f"zd_ticket:{tid}", ref, occurred, available,
                       status=status, priority=priority,
                       **{"tags": [], "ticket_created_at": occurred} | cols)

    def usage(self, ws, last_day, active_users, available=None, version=1):
        end = datetime.combine(last_day + DAY, datetime.min.time(), UTC)
        return self.ev("product.usage", f"usage:{ws}:{last_day}", f"ws:{ws}", end,
                       available or end + 3 * DAY, f"{version:04d}", active_users=active_users)

    def frames(self):
        return frames_from_rows(self.events, self.links, self.accounts, [], self.users)


def feats(w, at=T, slug="hearthline", **kw):
    df = compute_features(w.frames(), tenant(slug), [at], **kw)
    return df.xs(pd.Timestamp(at), level="as_of")


MONTHS = [datetime(2025, m, 1).date() for m in range(1, 6)]  # Jan..May 2025


def month_end(first):
    nxt = (first.replace(day=28) + 4 * DAY).replace(day=1)
    return nxt - DAY


def with_usage(w, sf, series, seats=None):
    """An account with a linked workspace reporting `series` for Jan.. (None = no row)."""
    a = w.account(sf)
    w.link(f"ws:{sf}", a, T - 300 * DAY, "product_metadata")
    for first, n in zip(MONTHS, series, strict=False):
        if n is not None:
            w.usage(sf, month_end(first), n)
    if seats:
        w.link(f"stripe:{sf}", a, T - 300 * DAY, "stripe_metadata")
        w.sub(sf, f"stripe:{sf}", T - 200 * DAY, qty=seats)
    return a


def test_usage_drop_is_a_tenant_relative_hinge_on_complete_months():
    w = World()
    a = with_usage(w, "a", [10, 10, 10, 4], seats=8)
    b = with_usage(w, "b", [10, 10, 10, 10])
    c = with_usage(w, "c", [10, 10, 10, 21])
    d = with_usage(w, "d", [None, 10, 10, 10], seats=5)  # three months only
    w.usage("a", month_end(MONTHS[4]), 50, available=T - DAY)  # May, early, for one ws only
    f = feats(w)
    assert f.loc[a, "usage_drop"] == pytest.approx(math.log(11 / 5))
    assert f.loc[b, "usage_drop"] == pytest.approx(0.0)  # the median account
    assert f.loc[c, "usage_drop"] == 0.0  # growth is not negative risk
    assert math.isnan(f.loc[d, "usage_drop"])
    # the level is the mean of the last 3 complete months: Feb..Apr, not the early May
    assert f.loc[a, "seat_utilization"] == pytest.approx((10 + 10 + 4) / 3 / 8)
    assert f.loc[d, "seat_utilization"] == pytest.approx(10 / 5)
    assert math.isnan(f.loc[b, "seat_utilization"])  # no seats known


def test_late_product_batch_does_not_create_a_drop():
    w = World()
    accounts = [with_usage(w, s, [10, 10, 10, 10]) for s in "abcd"]
    for s in "abcd":  # May's batch lands 8 days after the month ends
        w.usage(s, month_end(MONTHS[4]), 10, available=datetime(2025, 6, 9, tzinfo=UTC))
    early, late = T, datetime(2025, 6, 10, tzinfo=UTC)
    assert watermarks(w.frames(), early)["product"] == datetime(2025, 5, 1, tzinfo=UTC)
    assert watermarks(w.frames(), late)["product"] == datetime(2025, 6, 1, tzinfo=UTC)
    for at in (early, late):
        f = feats(w, at)
        assert (f.loc[accounts, "usage_drop"] == 0).all()


def test_product_watermark_ignores_churned_workspaces_but_waits_for_a_late_batch():
    """Ruling T: the denominator is last month's reporters, so churned accounts' workspaces
    (still linked, never reporting again) cannot freeze the watermark."""
    w = World()
    for i in range(20):  # one workspace churns after each of Jan, Feb, Mar: 17 report April
        with_usage(w, f"w{i:02d}", [10] * (1 + i if i < 3 else 4))
    late = datetime(2025, 6, 10, tzinfo=UTC)
    for i in range(3, 19):  # May: 5 of April's 17 early, 11 late, w19 churns too
        w.usage(f"w{i:02d}", month_end(MONTHS[4]), 10,
                available=T - DAY if i < 8 else late - DAY)
    # all-linked would give April 17/20 = 85% and hold at March; last-month reporters: 17/18
    assert watermarks(w.frames(), T)["product"] == datetime(2025, 5, 1, tzinfo=UTC)
    # 16 of April's 17 reporters delivered May (94%) although only 16 of 20 linked did
    assert watermarks(w.frames(), late)["product"] == datetime(2025, 6, 1, tzinfo=UTC)


def test_open_urgent_tickets_reads_ticket_version_current_at_T():
    w = World()
    a = w.account("a")
    w.link("zd_org:1", a, T - 300 * DAY, "zendesk_external_id")
    w.ticket(1, "zd_org:1", T - 5 * DAY, "new", "urgent")
    w.ticket(1, "zd_org:1", T - DAY, "solved", "urgent", available=T + 2 * DAY)  # late export
    w.ticket(2, "zd_org:1", T - 5 * DAY, "open", "high")
    w.ticket(2, "zd_org:1", T - 2 * DAY, "solved", "high")
    w.ticket(3, "zd_org:1", T - 5 * DAY, "open", "normal")
    w.ticket(4, "zd_org:1", T - 5 * DAY, "hold", "high")
    w.ticket(4, "zd_org:1", T - 4 * DAY, "deleted", "high")
    assert feats(w).loc[a, "open_urgent_tickets"] == pytest.approx(math.log1p(1))
    assert feats(w, T + 3 * DAY).loc[a, "open_urgent_tickets"] == 0


def test_reopen_and_bad_csat_count_in_90d():
    w = World()
    a = w.account("a")
    w.link("zd_org:1", a, T - 300 * DAY, "zendesk_external_id")
    w.ticket(1, "zd_org:1", T - 10 * DAY, "solved", csat="bad")
    w.ticket(2, "zd_org:1", T - 30 * DAY, "new")
    w.ticket(2, "zd_org:1", T - 20 * DAY, "solved")
    w.ticket(2, "zd_org:1", T - 5 * DAY, "open")  # reopened
    w.ticket(3, "zd_org:1", T - 100 * DAY, "solved", csat="bad")  # outside the window
    w.ticket(4, "zd_org:1", T - 30 * DAY, "new")
    w.ticket(4, "zd_org:1", T - 25 * DAY, "pending")  # pending before solved is no reopen
    w.ticket(4, "zd_org:1", T - 20 * DAY, "solved", csat="good")
    assert feats(w).loc[a, "support_friction_90d"] == pytest.approx(math.log1p(2))


def mail(w, ref, at, direction="inbound", **cols):
    return w.ev("email.message", f"gmail:{uuid4()}", ref, at, at + timedelta(minutes=5),
                direction=direction, **{"is_auto_reply": False, "is_dsn": False} | cols)


def test_days_since_customer_email_ignores_auto_replies_and_caps_at_120():
    w = World()
    a, b, c = w.account("a"), w.account("b"), w.account("c")
    w.link("email:pat@a.com", a, T - 300 * DAY, "contact_email")
    w.link("email:sam@b.com", b, T - 300 * DAY, "contact_email")
    mail(w, "email:pat@a.com", T - 10 * DAY)
    mail(w, "email:pat@a.com", T - 2 * DAY, is_auto_reply=True)
    mail(w, "email:pat@a.com", T - 2 * DAY, is_dsn=True, dsn_status="5.1.1")
    mail(w, "email:pat@a.com", T - timedelta(hours=1), "outbound")  # sets the watermark
    mail(w, "email:sam@b.com", T - 200 * DAY)
    f = feats(w)
    assert f.loc[a, "days_since_customer_email"] == pytest.approx(
        (timedelta(days=10) - timedelta(minutes=55)) / DAY)
    assert f.loc[b, "days_since_customer_email"] == 120
    assert f.loc[c, "days_since_customer_email"] == 120  # Gmail connected, silence is signal
    before_gmail = datetime(2024, 9, 2, tzinfo=UTC)
    assert math.isnan(feats(w, before_gmail).loc[c, "days_since_customer_email"])


def test_seat_change_uses_quantity_as_of_both_dates():
    w = World()
    ids = {}
    for sf, qtys in {"a": (10, 15), "b": (4, 1), "d": (1, 10)}.items():
        ids[sf] = w.account(sf)
        w.link(f"stripe:{sf}", ids[sf], T - 300 * DAY, "stripe_metadata")
        w.sub(sf, f"stripe:{sf}", T - 300 * DAY, qty=qtys[0])
        w.sub(sf, f"stripe:{sf}", T - 100 * DAY, qty=qtys[1])
    w.sub("a", "stripe:a", T - 5 * DAY, qty=40, available=T + DAY)  # not known at T
    ids["c"] = w.account("c")
    w.link("stripe:c", ids["c"], T - 300 * DAY, "stripe_metadata")
    w.sub("c", "stripe:c", T - 50 * DAY, qty=5)  # no seats 180 days ago
    f = feats(w)
    assert f.loc[ids["a"], "seat_change_180d"] == pytest.approx(0.5)
    assert f.loc[ids["b"], "seat_change_180d"] == pytest.approx(-0.75)
    assert f.loc[ids["d"], "seat_change_180d"] == 1.0  # clipped
    assert math.isnan(f.loc[ids["c"], "seat_change_180d"])


def test_no_telemetry_imputes_and_flags():
    """Usage features stay NaN here; the model artifact holds the imputation values."""
    w = World()
    a = with_usage(w, "a", [10, 10, 10, 10])
    b = w.account("b")
    w.link("ws:b", b, T + DAY, "product_metadata")  # linked only after T
    f = feats(w)
    assert (f.loc[a, "no_telemetry"], f.loc[b, "no_telemetry"]) == (0, 1)
    assert f.loc[b, ["usage_drop", "seat_utilization", "integration_broken",
                     "not_live_after_30d"]].isna().all()
    assert not f.loc[b, "telemetry_linked"] and f.loc[a, "telemetry_linked"]


def billed(w, sf, **sub):
    a = w.account(sf, **sub.pop("sf_attrs", {}))
    w.link(f"stripe:{sf}", a, T - 300 * DAY, "stripe_metadata")
    w.sub(sf, f"stripe:{sf}", T - 200 * DAY, **sub)
    return a


@pytest.mark.parametrize("slug,domain", [("hearthline", "hearthline.io"),
                                         ("tallyworks", "tallyworks.co.uk")])
def test_arr_from_stripe_items_minor_units_interval(slug, domain):
    w = World()
    yearly = billed(w, "y", qty=3, unit=120_000, interval="year")
    monthly = billed(w, "m", qty=2, unit=5_000, interval="month")
    ended = billed(w, "e", qty=2, unit=5_000, status="canceled", sf_attrs={"arr_minor": 999_00})
    sf_only = w.account("s", arr_minor=2_500_00)
    f = compute_features(w.frames(), tenant(slug, domain), [T]).xs(pd.Timestamp(T),
                                                                  level="as_of")
    assert f.loc[yearly, "arr_minor"] == 360_000
    assert f.loc[yearly, "log_arr"] == pytest.approx(math.log(3_600))
    assert f.loc[monthly, "log_arr"] == pytest.approx(math.log(1_200))  # 5000 * 2 * 12 / 100
    assert f.loc[sf_only, "log_arr"] == pytest.approx(math.log(2_500))
    assert (f.loc[sf_only, "arr_source"], f.loc[yearly, "arr_source"]) == ("salesforce",
                                                                           "stripe")
    assert math.isnan(f.loc[ended, "log_arr"])  # Stripe-linked: no fallback to ARR__c
    assert not f.loc[ended, "active"] and f.loc[monthly, "active"]


def test_term_end_precedence():
    w = World()
    yearly = billed(w, "y", interval="year", current_period_end=T + 40 * DAY)
    contract = billed(w, "c", interval="month", current_period_end=T + 20 * DAY,
                      sf_attrs={"renewal_date": T + 100 * DAY})
    w.ev("crm.account", "sf_account:c", "sf:c", T - DAY, T + DAY, renewal_date=T + 20 * DAY,
         is_test=False, is_deleted=False)  # a newer Renewal_Date__c, not yet known at T
    m2m = billed(w, "m", interval="month", current_period_end=T + 20 * DAY)
    f = feats(w)
    assert f.loc[yearly, "term_end"] == T + 40 * DAY
    assert f.loc[contract, "term_end"] == T + 100 * DAY
    assert f.loc[m2m, "term_end"] == T + 20 * DAY
    assert list(f.loc[[yearly, contract, m2m], "term_end_within_60d"]) == [1, 0, 1]
    assert list(f.loc[[yearly, contract, m2m], "billing_month_to_month"]) == [0, 0, 1]
    # the Stripe interval, for reliability per billing interval (design §6)
    assert list(f.loc[[yearly, contract, m2m], "billing_interval"]) == ["year", "month", "month"]
    assert f.loc[contract, "eligible"] and f.loc[yearly, "eligible"]


def test_rule_facts_cancel_both_stripe_shapes_and_ticket_tag():
    w = World()
    at_end = billed(w, "a", cancel_at_period_end=True)
    at_date = billed(w, "b", cancel_at=T + 30 * DAY)
    ticket = w.account("c")
    w.link("zd_org:3", ticket, T - 300 * DAY, "zendesk_external_id")
    tid = w.ticket(3, "zd_org:3", T - DAY, "open", tags=["cancellation_request"])
    gone = billed(w, "d", status="canceled", cancel_at_period_end=True)
    reversed_ = billed(w, "e", cancel_at_period_end=True)
    w.sub("e", "stripe:e", T - 10 * DAY)  # cancel reversed
    facts = rule_facts(w.frames(), tenant(), T)
    assert facts.loc[[at_end, at_date, ticket], "cancel_announced"].all()
    assert not facts.loc[[gone, reversed_], "cancel_announced"].any()
    assert facts.loc[ticket, "evidence_cancel_announced"] == [tid]


def invoice(w, ref, inv, at, attempts, next_attempt, failed=True, sub="s"):
    return w.ev("billing.invoice", f"stripe_inv:{inv}", ref, at, status="open" if failed
                else "paid", event_type="invoice.payment_failed" if failed else "invoice.paid",
                attempt_count=attempts, next_payment_attempt=next_attempt, subscription_id=sub)


def test_dunning_exhausted_not_on_first_failure():
    w = World()
    first = billed(w, "a")
    w.sub("a", "stripe:a", T - timedelta(hours=1), status="past_due")
    invoice(w, "stripe:a", "a", T - timedelta(hours=1), 1, T + 3 * DAY)
    third = billed(w, "b")
    eid = invoice(w, "stripe:b", "b", T - DAY, 3, T + 3 * DAY)
    final = billed(w, "c")
    invoice(w, "stripe:c", "c", T - DAY, 2, None)
    long_due = billed(w, "d")
    w.sub("d", "stripe:d", T - 8 * DAY, status="past_due")
    recovered = billed(w, "e")
    invoice(w, "stripe:e", "e", T - 2 * DAY, 3, T + DAY)
    invoice(w, "stripe:e", "e", T - DAY, 4, None, failed=False)
    facts = rule_facts(w.frames(), tenant(), T)
    assert not facts.loc[[first, recovered], "dunning_exhausted"].any()
    assert facts.loc[[third, final, long_due], "dunning_exhausted"].all()
    assert facts.loc[third, "evidence_dunning_exhausted"] == [eid]
    f = feats(w)
    assert f.loc[first, "in_dunning"] and not f.loc[first, "eligible"]


def product_event(w, ref, at, name, available=None):
    return w.ev("product.event", f"product_event:{uuid4()}", ref, at, available,
                product_event=name)


def test_integration_broken_needs_seven_days():
    w = World()
    old, new, fixed = (with_usage(w, s, [10, 10, 10, 10]) for s in "onf")
    product_event(w, "ws:o", T - 9 * DAY, "integration_sync_error")
    product_event(w, "ws:o", T - 6 * DAY, "integration_disconnected")
    product_event(w, "ws:n", T - 3 * DAY, "integration_sync_error")
    product_event(w, "ws:f", T - 20 * DAY, "integration_disconnected")
    product_event(w, "ws:f", T - 10 * DAY, "integration_reconnected")
    f = feats(w)
    assert list(f.loc[[old, new, fixed], "integration_broken"]) == [1, 0, 0]


def test_not_live_only_for_contracts_after_product_start():
    w = World()
    late, live, before, young = (with_usage(w, s, [10, 10, 10, 10]) for s in "lvby")
    product_start = datetime(2024, 4, 1, tzinfo=UTC)
    for account, sf, start in ((late, "l", T - 60 * DAY), (live, "v", T - 60 * DAY),
                               (before, "b", product_start - DAY), (young, "y", T - 10 * DAY)):
        w.link(f"stripe:{sf}", account, start, "stripe_metadata")
        w.sub(sf, f"stripe:{sf}", start, start_date=start)
    product_event(w, "ws:v", T - 20 * DAY, "workspace_went_live")
    f = feats(w)
    assert list(f.loc[[late, live, before, young], "not_live_after_30d"]) == [1, 0, 0, 0]
    assert f.loc[late, "tenure_years"] == pytest.approx(math.log1p(60 / 365.25))


def test_attribution_follows_merge_and_excludes_losers_and_tests():
    w = World()
    master = w.account("m")
    loser = w.account("l")
    w.account("t", is_test=True)
    w.account("i", domain="hearthline.io")  # an internal sandbox
    w.link("zd_org:9", loser, T - 300 * DAY, "zendesk_external_id")
    w.ticket(9, "zd_org:9", T - 2 * DAY, "open", "urgent")
    merged_at = T - 10 * DAY
    w.ev("crm.account", "sf_account:l", "sf:l", merged_at, is_test=False, is_deleted=True)
    w.link("sf:l", master, merged_at, "merge")
    f = feats(w)
    assert list(f.index) == [master]
    assert f.loc[master, "open_urgent_tickets"] == pytest.approx(math.log1p(1))
    before = feats(w, merged_at - DAY)
    assert set(before.index) == {master, loser}
    assert before.loc[loser, "open_urgent_tickets"] == 0  # the ticket did not exist yet


def test_champion_bounced_and_urgent_ticket_stalled():
    w = World()
    a, b = w.account("a"), w.account("b")
    for sf, acct in (("a", a), ("b", b)):
        w.link(f"zd_org:{sf}", acct, T - 300 * DAY, "zendesk_external_id")
        w.link(f"email:boss@{sf}.com", acct, T - 300 * DAY, "contact_email")
    w.ev("crm.contact", "sf_contact:1", "sf:a", T - 300 * DAY, email="boss@a.com",
         champion=True)
    w.ev("crm.contact", "sf_contact:2", "sf:b", T - 300 * DAY, email="boss@b.com",
         champion=False)
    bounce = mail(w, "email:boss@a.com", T - 3 * DAY, is_dsn=True, dsn_status="5.1.1",
                  failed_recipient="boss@a.com")
    mail(w, "email:boss@b.com", T - 3 * DAY, is_dsn=True, dsn_status="5.1.1",
         failed_recipient="boss@b.com")
    stalled = w.ticket("a1", "zd_org:a", T - 4 * DAY, "open", "urgent")
    w.ticket("b1", "zd_org:b", T - 2 * DAY, "open", "urgent")  # open 2 days only
    w.ticket("b2", "zd_org:b", T - 9 * DAY, "pending", "normal")
    facts = rule_facts(w.frames(), tenant(), T)
    assert facts.loc[a, "champion_bounced"] and not facts.loc[b, "champion_bounced"]
    assert facts.loc[a, "evidence_champion_bounced"] == [bounce]
    assert facts.loc[a, "urgent_ticket_stalled"] and not facts.loc[b, "urgent_ticket_stalled"]
    assert facts.loc[a, "evidence_urgent_ticket_stalled"] == [stalled]


def test_manifest_changes_when_function_changes(monkeypatch):
    before = manifest()
    assert set(before) >= set(FEATURE_NAMES)
    assert before["usage_drop"].startswith("v2:") and len(before["usage_drop"]) == 15

    def usage_drop(s):  # a different implementation under the same dotted path
        return None

    monkeypatch.setattr(features, "usage_drop", usage_drop)
    after = manifest()
    assert after["usage_drop"] != before["usage_drop"]
    assert {k: v for k, v in after.items() if k != "usage_drop"} == {
        k: v for k, v in before.items() if k != "usage_drop"}


def test_manifest_covers_helpers_a_feature_reaches(monkeypatch):
    before = manifest()

    def _monthly_users(s):  # e.g. 3 months instead of 4
        return None

    _monthly_users.__module__ = "radar.features"  # as if edited in place
    monkeypatch.setattr(features, "_monthly_users", _monthly_users)
    after = manifest()
    assert after["usage_drop"] != before["usage_drop"]
    assert after["seat_utilization"] != before["seat_utilization"]
    assert after["open_urgent_tickets"] == before["open_urgent_tickets"]


def test_manifest_covers_constants_a_feature_reads(monkeypatch):
    before = manifest()
    monkeypatch.setattr(features, "EMAIL_CAP", 90.0)
    after = manifest()
    assert after["days_since_customer_email"] != before["days_since_customer_email"]
    assert after["open_urgent_tickets"] == before["open_urgent_tickets"]


def test_banned_inputs_not_in_features():
    assert len(FEATURE_NAMES) == 13 and registry.FEATURES[0].name == "usage_drop"
    assert not set(FEATURE_NAMES) & {b.name for b in BANNED}
    assert all(b.banned_reason for b in BANNED)
    assert "| usage_drop |" in registry.render_dictionary_md()


def test_asof_helpers_visible_latest_attribute():
    w = World()
    master, loser = w.account("m"), w.account("l")
    w.ticket(1, "zd_org:1", T - 3 * DAY, "new")
    w.ticket(1, "zd_org:1", T - 2 * DAY, "open")
    w.ticket(1, "zd_org:1", T - DAY, "solved", available=T + DAY)
    w.link("zd_org:1", loser, T - 300 * DAY, "zendesk_external_id")
    w.link("sf:l", master, T - 5 * DAY, "merge")
    f = w.frames()
    tickets = f.events[f.events["type"] == "support.ticket"]
    assert len(asof.visible(tickets, T)) == 2 and len(asof.visible(tickets, T, True)) == 3
    assert list(asof.latest(tickets, T)["status"]) == ["open"]
    assert set(asof.attribute(tickets, f.links, T)) == {master}
    assert set(asof.attribute(tickets, f.links, T - 6 * DAY)) == {loser}
