"""Idempotent landing, canonical events, feed cursor and webhook signatures (design §4.1)."""

import json
import random
from datetime import UTC, datetime, timedelta

import pytest

from radar.bootstrap import seed_tenants_and_users
from radar.db import Database
from radar.ingest import feed, webhooks
from radar.ingest.normalize import normalize
from radar.sim import staff
from radar.sim.params import HISTORY_END
from radar.tenants import tenant_id
from tests.conftest import WORKER_PASSWORD, _role_dsn, fresh_database
from tests.payloads import FIXTURES, T0, account, at, golden, ingest, iso, rows, sf, signed, stripe

SF_ID = "0015g0000YUAeKDAQ1"
TYPES = {"stripe_customer_created": "billing.customer", "stripe_customer_updated":
         "billing.customer", "stripe_customer_subscription": "billing.subscription",
         "stripe_invoice": "billing.invoice", "zendesk_organization": "support.organization",
         "zendesk_ticket": "support.ticket", "salesforce_account": "crm.account",
         "salesforce_contact": "crm.contact", "salesforce_opportunityhistory":
         "crm.opportunity_history", "salesforce_opportunity": "crm.opportunity",
         "salesforce_user": "crm.user", "gmail": "email.message", "product_usage":
         "product.usage", "product_track": "product.event"}
REF_PREFIX = {"billing": "stripe:", "support": "zd_org:", "email": "email:", "product": "ws:"}


@pytest.mark.parametrize("path", sorted(FIXTURES.glob("*.json")), ids=lambda p: p.stem)
def test_golden_payloads_normalize(path):
    rec = json.loads(path.read_text())
    [ev] = normalize(rec["source"], rec["body"], rec["meta"])
    assert ev.type == next(t for k, t in TYPES.items() if path.stem.startswith(k))
    assert ev.occurred_at.tzinfo is not None and ev.version_key
    assert ev.occurred_at <= datetime.fromisoformat(rec["deliver_at"])
    if ev.account_ref is not None:
        prefix = REF_PREFIX.get(ev.type.split(".")[0], "sf")
        assert ev.account_ref.startswith(prefix)
    a = ev.attrs
    if ev.type == "billing.subscription":  # top-level before basil, on the item from basil on
        assert a["current_period_end"] >= rec["body"]["created"] and ev.quantity > 0
        assert a["start_date"] == rec["body"]["data"]["object"]["start_date"]  # contract start
    if ev.type == "billing.invoice":
        assert a["subscription_id"].startswith("sub_") and ev.amount_minor > 0
        assert a["paid"] is (rec["body"]["type"] == "invoice.paid")
    if ev.type == "product.usage":  # v1 field names are mapped to v2's
        assert "active_users" in a["metrics"] and "lead_score_avg" not in a["metrics"]
        assert ev.version_key == f"{a['version']:04d}"
    if ev.type == "email.message":
        assert a["is_dsn"] is path.stem.startswith("gmail_dsn")
        assert a["is_auto_reply"] is (path.stem == "gmail_auto_reply")


def test_subscription_versions_in_both_api_shapes():
    old = normalize("stripe", golden("stripe_customer_subscription_updated_pre_basil")["body"], {})
    new = normalize("stripe", golden("stripe_customer_subscription_updated_basil")["body"], {})
    for [ev] in (old, new):
        assert ev.entity_id.startswith("stripe_sub:sub_")
        assert ev.version_key.split(":")[0] == f"{int(ev.occurred_at.timestamp()):010d}"


def test_duplicate_counted_not_stored(worker_db, tenant):
    tid, _ = tenant
    rec = at(golden("zendesk_ticket"), T0)
    first = ingest(worker_db, tid, rec, rec)
    second = ingest(worker_db, tid, rec)
    assert (first.accepted, first.duplicates, second.duplicates) == (1, 1, 1)
    assert len(rows(worker_db, tid, "SELECT id FROM raw_records")) == 1
    assert len(rows(worker_db, tid, "SELECT id FROM events")) == 1
    [status] = rows(worker_db, tid, "SELECT counters FROM source_status WHERE source = 'zendesk'")
    assert status["counters"]["received"] == 3 and status["counters"]["duplicates"] == 2


def test_conflicting_resend_quarantined(worker_db, tenant):
    tid, _ = tenant
    rec = at(golden("zendesk_ticket"), T0)
    changed = at(rec, T0 + timedelta(minutes=5))
    changed["body"]["subject"] = "a different body under the same id"
    stats = ingest(worker_db, tid, rec, changed)
    assert stats.conflicts == 1 and stats.quarantined["conflicting_resend"] == 1
    [q] = rows(worker_db, tid,
               "SELECT reason, details FROM quarantine WHERE reason = 'conflicting_resend'")
    assert q["reason"] == "conflicting_resend"
    assert q["details"]["payload"]["subject"] == changed["body"]["subject"]  # nothing lost


def test_stripe_semantic_twin_dropped(worker_db, tenant):
    tid, slug = tenant
    rec = stripe("stripe_customer_subscription_updated_basil", slug, T0)
    twin = at(rec, T0 + timedelta(seconds=20))
    twin["body"] |= {"id": "evt_twin000000000000000000", "created": rec["body"]["created"] + 1}
    stats = ingest(worker_db, tid, rec, signed(twin, slug))
    assert (stats.accepted, stats.twins) == (2, 1)
    assert len(rows(worker_db, tid, "SELECT id FROM events")) == 1


def test_out_of_order_subscription_terminal_state_wins(worker_db, tenant):
    tid, slug = tenant
    deleted = stripe("stripe_customer_subscription_deleted_basil", slug, T0)
    sub = deleted["body"]["data"]["object"]["id"]
    late = stripe("stripe_customer_subscription_updated_basil", slug, T0 + timedelta(hours=30),
                  id=sub, status="active")
    late["body"]["created"] = deleted["body"]["created"] - 3600  # older state, arrives later
    same_second = stripe("stripe_customer_subscription_updated_basil", slug,
                         T0 + timedelta(hours=31), id=sub, status="active",
                         cancel_at_period_end=True)  # not a twin of `late`
    same_second["body"] |= {"created": deleted["body"]["created"], "id": "evt_" + "z" * 24}
    ingest(worker_db, tid, deleted)
    ingest(worker_db, tid, signed(late, slug), signed(same_second, slug))
    latest = rows(worker_db, tid, "SELECT status FROM events WHERE entity_id = %s"
                  " ORDER BY occurred_at DESC, version_key DESC LIMIT 1", "stripe_sub:" + sub)
    assert latest == [{"status": "canceled"}]


def test_gmail_same_message_two_mailboxes_one_event(worker_db, tenant):
    tid, _ = tenant
    stats = ingest(worker_db, tid, golden("gmail_two_mailbox_a"), golden("gmail_two_mailbox_b"))
    assert stats.accepted == 2
    assert len(rows(worker_db, tid, "SELECT id FROM raw_records")) == 2
    assert len(rows(worker_db, tid, "SELECT id FROM events")) == 1


def _header(rec, name):
    return next(h["value"] for h in rec["body"]["payload"]["headers"] if h["name"] == name)


def test_auto_reply_and_non_final_dsn_flags(worker_db, tenant):
    tid, _ = tenant
    hard, delayed = golden("gmail_dsn_5_1_1"), golden("gmail_dsn_4_4_7")
    before = datetime(2024, 10, 1, tzinfo=UTC)
    ingest(worker_db, tid, account(SF_ID, "example.com", when=before), *[
        sf("contact", before, Id=f"0035g0000NVkyM{i}AQZ", AccountId=SF_ID,
           Email=_header(r, "X-Failed-Recipients")) for i, r in enumerate((hard, delayed))])
    ingest(worker_db, tid, golden("gmail_auto_reply"), delayed, hard)
    flags = {r["k"]: r["attrs"] for r in rows(
        worker_db, tid, "SELECT r.source_record_id AS k, e.attrs FROM events e"
        " JOIN raw_records r ON r.id = e.raw_record_id WHERE e.type = 'email.message'")}
    by_name = {name: flags[f"{golden(name)['meta']['mailbox']}:{golden(name)['body']['id']}"]
               for name in ("gmail_auto_reply", "gmail_dsn_4_4_7", "gmail_dsn_5_1_1")}
    auto, delay = by_name["gmail_auto_reply"], by_name["gmail_dsn_4_4_7"]
    assert auto["is_auto_reply"] and not auto["is_dsn"]
    assert delay["is_dsn"] and delay["dsn_status"] == "4.4.7" and not delay["is_auto_reply"]
    bounced = {r["email"]: r["hard_bounced_at"] for r in rows(
        worker_db, tid, "SELECT email, hard_bounced_at FROM contacts")}
    assert bounced[_header(delayed, "X-Failed-Recipients")] is None
    sent = int(hard["body"]["internalDate"]) / 1000
    assert bounced[_header(hard, "X-Failed-Recipients")].timestamp() == sent
    [status] = rows(worker_db, tid, "SELECT counters FROM source_status WHERE source = 'gmail'")
    assert (status["counters"]["auto_replies"], status["counters"]["dsn"]) == (1, 2)


def test_product_future_timestamp_quarantined(worker_db, tenant):
    tid, _ = tenant
    rec = golden("product_track")
    future = datetime.fromisoformat(rec["deliver_at"]) + timedelta(hours=5)
    rec["body"] |= {"timestamp": iso(future), "originalTimestamp": iso(future)}
    stats = ingest(worker_db, tid, rec)
    assert stats.quarantined == {"future_timestamp": 1}
    assert rows(worker_db, tid, "SELECT id FROM events") == []
    [status] = rows(worker_db, tid, "SELECT counters FROM source_status WHERE source = 'product'")
    assert status["counters"]["future_timestamps"] == 1


def test_account_projection_owner_and_newer_modstamp(worker_db, tenant):
    tid, _ = tenant
    csm = next(u for u in staff("hearthline") if u["role"] == "csm")
    newer = account(SF_ID, "clark.com", when=T0 + timedelta(days=1), Name="New name",
                    OwnerId=csm["sf_user_id"])
    older = at(account(SF_ID, "clark.com", when=T0, Name="Old name"), T0 + timedelta(days=3))
    ingest(worker_db, tid, newer)
    ingest(worker_db, tid, older)
    [row] = rows(worker_db, tid, "SELECT a.name, a.domain, u.email FROM accounts a"
                 " JOIN users u ON u.id = a.owner_user_id")
    assert row == {"name": "New name", "domain": "clark.com", "email": csm["email"]}


def test_payment_failure_triggers_rule_evaluation(worker_db, tenant):
    tid, slug = tenant
    ingest(worker_db, tid, account(SF_ID, "clark.com"))
    later = T0 + timedelta(days=1)
    customer = stripe("stripe_customer_created_basil", slug, later, id="cus_x",
                      metadata={"sf_account_id": SF_ID})
    failed = stripe("stripe_invoice_payment_failed_basil", slug, later + timedelta(minutes=1),
                    customer="cus_x")
    paid = stripe("stripe_invoice_paid_basil", slug, later + timedelta(minutes=2),
                  customer="cus_x")
    assert ingest(worker_db, tid, customer, paid).triggers == []
    [acc] = rows(worker_db, tid, "SELECT id FROM accounts")
    assert ingest(worker_db, tid, failed).triggers == [acc["id"]]


def test_webhook_sign_verify_and_tolerance():
    body, secrets = b'{"a":1}', {"k1": "s3cret"}
    header = webhooks.sign("s3cret", "k1", body, 1000)
    assert header.startswith("t=1000,kid=k1,v1=")
    assert webhooks.verify(header, body, secrets, now=1300) == "k1"
    bad = [(header, body, secrets, 1301), (header, body, secrets, 699),
           (header, body, {"k1": "other"}, 1000), (header, body, {}, 1000),
           (header, b'{"a":2}', secrets, 1000), ("garbage", body, secrets, 1000),
           ("t=1000,kid=k1,v1=é", body, secrets, 1000),
           ("t=1000,kid=k1,v1=\ud800", body, secrets, 1000)]
    for args in bad:
        with pytest.raises(webhooks.WebhookError):
            webhooks.verify(*args)


def test_stripe_secrets_are_per_tenant():
    configured = {"acme-stripe": "whsec_rotated", "other-stripe": "whsec_other"}
    assert webhooks.stripe_secrets("acme", configured) == {"acme-stripe": "whsec_rotated"}
    assert webhooks.stripe_secrets("beta", configured) == {"beta-stripe": "whsec_demo_beta"}


def test_bad_signature_quarantined(worker_db, tenant):
    tid, slug = tenant
    foreign = golden("stripe_invoice_paid_basil")  # signed with hearthline's key id
    stale = at(stripe("stripe_invoice_paid_basil", slug, T0), T0 + timedelta(minutes=6))
    stats = ingest(worker_db, tid, foreign, stale)
    assert stats.quarantined == {"bad_signature": 2}
    assert rows(worker_db, tid, "SELECT id FROM events") == []


def test_bad_signature_never_lands_so_valid_resend_applies(worker_db, tenant):
    """A stale or forged delivery must not claim the evt id ahead of the genuine one."""
    tid, slug = tenant
    stale = at(stripe("stripe_invoice_paid_basil", slug, T0), T0 + timedelta(minutes=6))
    assert ingest(worker_db, tid, stale).quarantined == {"bad_signature": 1}
    [q] = rows(worker_db, tid, "SELECT raw_record_id, details FROM quarantine"
               " WHERE reason = 'bad_signature'")
    assert q["raw_record_id"] is None and q["details"]["payload"] == stale["body"]
    assert rows(worker_db, tid, "SELECT id FROM raw_records") == []
    resend = signed(at(stale, T0 + timedelta(minutes=7)), slug)
    stats = ingest(worker_db, tid, resend)
    assert (stats.accepted, stats.duplicates, stats.conflicts) == (1, 0, 0)
    assert len(rows(worker_db, tid, "SELECT id FROM events")) == 1


def test_live_path_uses_server_time(worker_db, tenant):
    tid, _ = tenant
    now = T0 + timedelta(days=9)
    ingest(worker_db, tid, at(golden("zendesk_ticket"), T0), available_at=now)
    assert {r["available_at"] for r in rows(worker_db, tid, "SELECT available_at FROM events")
            } == {now}


def _write_feed(data_dir, slug, source, recs):
    folder = data_dir / "feed" / slug
    folder.mkdir(parents=True, exist_ok=True)
    lines = sorted(json.dumps(r, separators=(",", ":"), sort_keys=True) for r in recs)
    (folder / f"{source}.jsonl").write_text("".join(x + "\n" for x in lines))


def _feed_world(tmp_path, slug):
    tickets = []
    for i in range(3):
        rec = at(golden("zendesk_ticket"), T0 + timedelta(hours=i))
        rec["body"]["id"] = 900 + i
        tickets.append(rec)
    _write_feed(tmp_path, slug, "zendesk", tickets)
    _write_feed(tmp_path, slug, "gmail", [at(golden("gmail_inbound"), T0 + timedelta(minutes=30))])


def test_feed_cursor_resumes(worker_db, tenant, tmp_path):
    tid, slug = tenant
    _feed_world(tmp_path, slug)
    first = feed.deliver_due(worker_db, tid, T0 + timedelta(hours=1), tmp_path, batch=1)
    again = feed.deliver_due(worker_db, tid, T0 + timedelta(hours=1), tmp_path, batch=1)
    rest = feed.deliver_due(worker_db, tid, T0 + timedelta(days=1), tmp_path, batch=1)
    assert (first.received, again.received, rest.received) == (3, 0, 1)
    offsets = {r["source"]: r["feed_offset"] for r in rows(
        worker_db, tid, "SELECT source, feed_offset FROM source_status")}
    for source in ("zendesk", "gmail"):
        assert offsets[source] == (tmp_path / "feed" / slug / f"{source}.jsonl").stat().st_size


def test_feed_crash_mid_replay_neither_skips_nor_repeats(worker_db, tenant, tmp_path,
                                                          monkeypatch):
    tid, slug = tenant
    _feed_world(tmp_path, slug)
    real, calls = feed.pipeline.ingest, []

    def crash_on_second_batch(*args, **kwargs):
        calls.append(1)
        if len(calls) == 2:
            raise RuntimeError("worker killed")
        return real(*args, **kwargs)

    monkeypatch.setattr(feed.pipeline, "ingest", crash_on_second_batch)
    with pytest.raises(RuntimeError):
        feed.deliver_due(worker_db, tid, T0 + timedelta(days=1), tmp_path, batch=1)
    monkeypatch.undo()
    resumed = feed.deliver_due(worker_db, tid, T0 + timedelta(days=1), tmp_path, batch=1)
    assert resumed.received == 3 and resumed.duplicates == 0
    assert len(rows(worker_db, tid, "SELECT id FROM raw_records")) == 4


def _checksum(db, tid):
    events = rows(db, tid, "SELECT md5(string_agg(t, '|' ORDER BY t)) AS h FROM (SELECT row("
                  "source, type, entity_id, account_ref, occurred_at, available_at, version_key,"
                  " status, priority, amount_minor, quantity, attrs)::text AS t FROM events) x")
    links = rows(db, tid, "SELECT md5(string_agg(t, '|' ORDER BY t)) AS h FROM (SELECT row("
                 "l.source, l.account_ref, a.sf_account_id, l.method, l.available_at)::text AS t"
                 " FROM identity_links l JOIN accounts a ON a.id = l.account_id) x")
    return events[0]["h"], links[0]["h"]


def test_shuffled_duplicated_replay_is_identical(small_world_db):
    slug, tid = "kitewire", tenant_id("kitewire")
    cutoff = iso(HISTORY_END)
    records = [json.loads(line)
               for path in sorted((small_world_db["data_dir"] / "feed" / slug).glob("*.jsonl"))
               for line in path.read_text().splitlines()]
    records = [r for r in records if r["deliver_at"] <= cutoff]
    rng = random.Random(3)
    mess = records + rng.sample(records, len(records) // 10)
    rng.shuffle(mess)
    with fresh_database() as dsn:
        db = Database(_role_dsn(dsn, "radar_worker", WORKER_PASSWORD))
        try:
            seed_tenants_and_users(dsn, db, [slug])
            stats = ingest(db, tid, *mess)
            assert stats.duplicates >= len(records) // 10
            assert _checksum(db, tid) == _checksum(small_world_db["db"], tid)
        finally:
            db.close()
