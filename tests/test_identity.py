"""Exact-key identity resolution (design §4.2)."""

from datetime import datetime, timedelta

from radar.ingest.identity import normalize_sf_id
from tests.payloads import T0, account, at, golden, ingest, rows, sf, stripe

SF_ID = "0015g0000YUAeKDAQ1"  # a real 18-char id from the generator
CUSTOMER = "stripe_customer_created_basil"


def links(db, tid, ref):
    return rows(db, tid, "SELECT account_id, method, available_at FROM identity_links"
                " WHERE account_ref = %s ORDER BY available_at, id", ref)


def quarantined(db, tid):
    return rows(db, tid, "SELECT account_ref, reason, resolved_at FROM quarantine ORDER BY id")


def account_id(db, tid, sf_id):
    return rows(db, tid, "SELECT id FROM accounts WHERE sf_account_id = %s", sf_id)[0]["id"]


def test_normalize_sf_id_15_to_18_and_garbage():
    assert normalize_sf_id(SF_ID) == SF_ID
    assert normalize_sf_id(SF_ID[:15]) == SF_ID
    assert normalize_sf_id(f"  {SF_ID} ") == SF_ID
    assert normalize_sf_id(SF_ID.lower()) == SF_ID  # the checksum restores the case
    assert normalize_sf_id("0035g0000NVkyMZAQZ") is None  # a contact id, not an account
    assert normalize_sf_id("not-an-id") is None
    assert normalize_sf_id("") is None
    assert normalize_sf_id(None) is None


def test_resolution_order_external_id_then_email_then_domain(worker_db, tenant):
    tid, slug = tenant
    other = "0015g0000eEHMGnAAP"
    ingest(worker_db, tid, account(SF_ID, "clarkhome.com"), account(other, "meadow.com"))
    later = T0 + timedelta(days=1)
    # metadata id wins over a domain that points at another account
    ingest(worker_db, tid, stripe(CUSTOMER, slug, later, id="cus_a", email="billing@meadow.com",
                                  metadata={"salesforce_id": SF_ID.lower()}))
    # no metadata: the corporate email domain decides
    ingest(worker_db, tid, stripe(CUSTOMER, slug, later, id="cus_b", email="ap@meadow.com",
                                  metadata={}))
    # Gmail: the exact address of an SF contact beats its domain
    ingest(worker_db, tid, sf("contact", T0, AccountId=SF_ID, Email="Jo@Meadow.com"))
    mail = at(golden("gmail_inbound"), later)
    headers = mail["body"]["payload"]["headers"]
    for h in headers:
        if h["name"] == "From":
            h["value"] = "Jo <jo@meadow.com>"
    ingest(worker_db, tid, mail)
    assert [(r["account_id"], r["method"]) for r in links(worker_db, tid, "stripe:cus_a")] == [
        (account_id(worker_db, tid, SF_ID), "stripe_metadata")]
    assert [(r["account_id"], r["method"]) for r in links(worker_db, tid, "stripe:cus_b")] == [
        (account_id(worker_db, tid, other), "domain")]
    assert [(r["account_id"], r["method"]) for r in links(worker_db, tid, "email:jo@meadow.com")
            ] == [(account_id(worker_db, tid, SF_ID), "contact_email")]


def test_free_mail_and_franchise_domains_quarantined(worker_db, tenant):
    tid, slug = tenant
    ingest(worker_db, tid, account(SF_ID, "gmail.com"), account("0015g0000eEHMGnAAP", "kw.com"))
    later = T0 + timedelta(days=1)
    stats = ingest(worker_db, tid,
                   stripe(CUSTOMER, slug, later, id="cus_a", email="a@gmail.com", metadata={}),
                   stripe(CUSTOMER, slug, later, id="cus_b", email="b@kw.com", metadata={}))
    assert stats.quarantined == {"free_mail_domain": 2}
    assert [r["reason"] for r in quarantined(worker_db, tid)] == ["free_mail_domain"] * 2


def test_ambiguous_domain_quarantined(worker_db, tenant):
    tid, slug = tenant
    ingest(worker_db, tid, account(SF_ID, "twin.com"), account("0015g0000eEHMGnAAP", "twin.com"))
    ingest(worker_db, tid, stripe(CUSTOMER, slug, T0 + timedelta(days=1), id="cus_a",
                                  email="billing@twin.com", metadata={}))
    assert [(r["account_ref"], r["reason"]) for r in quarantined(worker_db, tid)] == [
        ("stripe:cus_a", "ambiguous_domain")]
    assert links(worker_db, tid, "stripe:cus_a") == []


def test_late_salesforce_account_resolves_quarantined_ticket(worker_db, tenant):
    tid, _ = tenant
    org, ticket = at(golden("zendesk_organization"), T0), at(golden("zendesk_ticket"), T0)
    org["body"] |= {"external_id": SF_ID, "id": 77, "domain_names": ["clarkhome.com"]}
    ticket["body"]["organization_id"] = 77
    stats = ingest(worker_db, tid, org, ticket)
    assert stats.quarantined == {"unknown_external_id": 2}
    late = T0 + timedelta(days=3)
    arrived = account(SF_ID, "clarkhome.com", when=late)
    ingest(worker_db, tid, arrived)
    [link] = links(worker_db, tid, "zd_org:77")
    assert link["method"] == "zendesk_external_id"
    assert link["account_id"] == account_id(worker_db, tid, SF_ID)
    assert link["available_at"] == datetime.fromisoformat(arrived["deliver_at"])
    assert all(r["resolved_at"] is not None for r in quarantined(worker_db, tid))


def test_merge_maps_loser_to_master(worker_db, tenant):
    tid, _ = tenant
    loser = golden("salesforce_account_merge_loser")["body"]
    master = loser["MasterRecordId"]
    ingest(worker_db, tid, account(master, "ashford.com"),
           account(loser["Id"], "ashford.com", Type="Prospect"))
    merged = sf("account_merge_loser", T0 + timedelta(days=20))
    ingest(worker_db, tid, merged)
    ref = links(worker_db, tid, "sf:" + loser["Id"])
    assert [(r["method"], r["account_id"]) for r in ref] == [
        ("sf_id", account_id(worker_db, tid, loser["Id"])),
        ("merge", account_id(worker_db, tid, master))]
    [row] = rows(worker_db, tid, "SELECT merged_into FROM accounts WHERE sf_account_id = %s",
                 loser["Id"])
    assert row["merged_into"] == account_id(worker_db, tid, master)


def test_domain_link_upgrades_when_the_carrier_later_names_the_account(worker_db, tenant):
    """A ref first matched by domain gets an exact link once a later version of its carrier
    record (customer.updated) brings the Salesforce id; the crosswalk stays append-only."""
    tid, slug = tenant
    ingest(worker_db, tid, account(SF_ID, "clarkhome.com"))
    later = T0 + timedelta(days=1)
    ingest(worker_db, tid, stripe(CUSTOMER, slug, later, id="cus_up", email="ap@clarkhome.com",
                                  metadata={}))
    ingest(worker_db, tid, stripe(CUSTOMER, slug, later + timedelta(days=1), id="cus_up",
                                  email="ap@clarkhome.com", metadata={"salesforce_id": SF_ID}))
    sf_account = account_id(worker_db, tid, SF_ID)
    assert [(r["account_id"], r["method"]) for r in links(worker_db, tid, "stripe:cus_up")] == [
        (sf_account, "domain"), (sf_account, "stripe_metadata")]
