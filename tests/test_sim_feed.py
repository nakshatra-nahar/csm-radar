import hashlib
import hmac
import json
import re
from collections import defaultdict
from datetime import UTC, datetime, time, timedelta
from email.utils import parseaddr
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from radar.sim import INJECTION_ACCOUNT_NAME, VP_CASE_NAMES, generate
from radar.sim.delivery import stripe_signature
from radar.sim.names import INJECTION_TEXT
from radar.tenants import TENANTS

SOURCES = ("salesforce", "stripe", "zendesk", "gmail", "product")
ENVELOPE = {"deliver_at", "source", "channel", "headers", "body", "meta"}
HISTORY_END = datetime(2026, 10, 17, tzinfo=UTC)
ISO_Z = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ")


def _ts(s: str) -> datetime:
    return datetime.fromisoformat(s)


@pytest.fixture(scope="module")
def small_world(tmp_path_factory):
    out = tmp_path_factory.mktemp("radar")
    summary = generate(out, scale=0.15)
    raw = {(slug, src): (out / "feed" / slug / f"{src}.jsonl").read_text().splitlines()
           for slug in TENANTS for src in SOURCES}
    lines = {k: [json.loads(x) for x in v] for k, v in raw.items()}
    identity = {slug: pd.read_parquet(out / "truth" / f"{slug}_identity.parquet")
                for slug in TENANTS}
    return {"dir": out, "summary": summary, "raw": raw, "lines": lines, "identity": identity}


def _bodies(small_world, src, slug=None):
    return [x["body"] for (s, k), v in small_world["lines"].items()
            if k == src and slug in (None, s) for x in v]


def _headers(msg) -> dict:
    return {h["name"]: h["value"] for h in msg["payload"]["headers"]}


def test_files_deterministic(tmp_path):
    def digests(out: Path) -> dict:
        generate(out, seed=3, scale=0.05)
        return {p.relative_to(out): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in sorted(out.rglob("*")) if p.is_file()}

    a, b = digests(tmp_path / "a"), digests(tmp_path / "b")
    assert a == b and len(a) == 3 * (len(SOURCES) + 3)


def test_feed_files_sorted_and_envelopes_valid(small_world):
    for (slug, src), lines in small_world["lines"].items():
        stamps = [x["deliver_at"] for x in lines]
        assert stamps == sorted(stamps), (slug, src)
        for x in lines:
            assert set(x) == ENVELOPE and x["source"] == src
            assert ISO_Z.fullmatch(x["deliver_at"])
            assert x["channel"] in ("webhook", "batch", "sync")
            assert isinstance(x["headers"], dict) and isinstance(x["meta"], dict)
            if src == "gmail":
                assert x["meta"]["mailbox"].endswith("@" + TENANTS[slug].internal_domain)
            if src == "zendesk" and "requester_id" in x["body"]:  # a ticket row
                assert "problem_id" in x["body"]  # Appendix B support.ticket attr
        for raw in small_world["raw"][slug, src]:  # one compact object per line
            assert raw == json.dumps(json.loads(raw), separators=(",", ":"), sort_keys=True)
    for slug, row in small_world["summary"]["tenants"].items():
        counts = {src: len(small_world["lines"][slug, src]) for src in SOURCES}
        assert row["records_by_source"] == counts
        assert row["history_records"] == sum(
            _ts(x["deliver_at"]) <= HISTORY_END
            for src in SOURCES for x in small_world["lines"][slug, src])


def _occurred(src: str, body: dict, tz: ZoneInfo) -> datetime:
    if src == "salesforce":
        return _ts(body["SystemModstamp"])
    if src == "stripe":
        return datetime.fromtimestamp(body["created"], UTC)
    if src == "zendesk":
        return _ts(body["updated_at"])
    if src == "gmail":
        return datetime.fromtimestamp(int(body["internalDate"]) / 1000, UTC)
    if "schema" in body:  # a calendar month: compare it as a date
        return datetime.combine(_ts(body["period_start"]), time(), tz)
    return _ts(body["timestamp"])


def test_every_source_present_with_start_dates(small_world):
    for slug, spec in TENANTS.items():
        for src in SOURCES:
            lines = small_world["lines"][slug, src]
            assert lines, (slug, src)
            tz = ZoneInfo(spec.timezone)
            start = datetime.combine(spec.source_start[src], time(), tz)
            first = min(_occurred(src, x["body"], tz) for x in lines)
            assert first >= start, (slug, src, first)
            assert min(_ts(x["deliver_at"]) for x in lines) >= start


def test_messiness_is_planted(small_world):
    stripe = [x for (_, s), v in small_world["lines"].items() if s == "stripe" for x in v]
    by_id = defaultdict(list)
    for x in stripe:
        by_id[x["body"]["id"]].append(x["body"])
    assert any(len(v) > 1 and all(b == v[0] for b in v) for v in by_id.values())
    assert any(_ts(x["deliver_at"]) - datetime.fromtimestamp(x["body"]["created"], UTC)
               > timedelta(hours=24) for x in stripe)
    states = defaultdict(set)
    for x in stripe:
        b = x["body"]
        if b["type"] == "customer.subscription.updated":
            states[b["type"], json.dumps(b["data"]["object"], sort_keys=True)].add(b["id"])
    assert any(len(ids) > 1 for ids in states.values())  # semantic twins
    meta = [x["body"]["data"]["object"]["metadata"] for x in stripe
            if x["body"]["type"] in ("customer.created", "customer.updated")]
    values = [v for m in meta for v in m.values()]
    assert any(m == {} for m in meta) and any("salesforce_id" in m for m in meta)
    assert any(len(v) == 15 for v in values) and any(v != v.strip() for v in values)
    assert any(v.islower() and len(v) == 18 for v in values)
    for slug in ("hearthline", "tallyworks"):
        versions = {b["api_version"] for b in _bodies(small_world, "stripe", slug)}
        assert versions == {"2024-06-20", "2025-03-31.basil"}
    assert {b["api_version"] for b in _bodies(small_world, "stripe", "kitewire")} == {
        "2026-03-25.dahlia"}

    zendesk = _bodies(small_world, "zendesk")
    assert any("status" in b and b["organization_id"] is None for b in zendesk)
    sf = _bodies(small_world, "salesforce")
    keys = [(b["Id"], b["SystemModstamp"]) for b in sf]
    assert len(set(keys)) < len(keys)  # unchanged-row re-exports
    accounts = [b for b in sf if b["attributes"]["type"] == "Account"]
    assert any(b["IsDeleted"] and b["MasterRecordId"] for b in accounts)
    assert any(b["Is_Test__c"] for b in accounts)

    mail = [(x["meta"]["mailbox"], x["body"]) for (_, s), v in small_world["lines"].items()
            if s == "gmail" for x in v]
    heads = [(box, b["id"], _headers(b), b) for box, b in mail]
    assert any(h.get("Auto-Submitted") == "auto-replied" and "mailer-daemon" not in h["From"]
               for _, _, h, _ in heads)
    statuses = {_dsn_status(b) for _, _, h, b in heads
                if "mailer-daemon@googlemail.com" in h["From"]
                and h["Content-Type"].startswith("multipart/report")}
    assert "5.1.1" in statuses and any(s.startswith("4.") for s in statuses)
    copies = defaultdict(set)
    for box, gid, h, _ in heads:
        copies[h["Message-ID"]].add((box, gid))
    assert any(len({b for b, _ in c}) > 1 and len({g for _, g in c}) > 1
               for c in copies.values())

    usage = [x for (_, s), v in small_world["lines"].items() if s == "product"
             for x in v if "schema" in x["body"]]
    assert {x["body"]["schema"] for x in usage} == {"usage_summary.v1", "usage_summary.v2"}
    hl = [b for b in _bodies(small_world, "product", "hearthline") if "schema" in b]
    assert all(("signal_intent_high" in b["metrics"]) == (b["schema"] == "usage_summary.v2")
               for b in hl)
    assert any(x["body"]["version"] > 1 for x in usage)
    assert any(x["body"]["version"] == 1 and _ts(x["deliver_at"]).date()
               > _ts(x["body"]["period_end"]).date() + timedelta(days=3) for x in usage)
    tracks = [b for b in _bodies(small_world, "product") if b.get("type") == "track"]
    assert any(_ts(b["timestamp"]) > _ts(b["receivedAt"]) for b in tracks)


def _dsn_status(msg) -> str:
    report = next(p for p in msg["payload"]["parts"] if p["mimeType"] == "message/delivery-status")
    return {h["name"]: h["value"] for h in report["headers"]}["Status"]


def test_stripe_signatures_verify(small_world):
    for slug in TENANTS:
        secret = f"whsec_demo_{slug}".encode()
        for raw, x in zip(small_world["raw"][slug, "stripe"],
                          small_world["lines"][slug, "stripe"], strict=True):
            body = json.dumps(x["body"], separators=(",", ":"), sort_keys=True)
            assert body in raw  # the verifier recomputes over exactly these bytes
            t = int(_ts(x["deliver_at"]).timestamp())
            fields = dict(p.split("=", 1) for p in x["headers"]["Stripe-Signature"].split(","))
            assert fields["t"] == str(t) and fields["kid"] == f"{slug}-stripe"
            expected = hmac.new(secret, f"{t}.{body}".encode(), hashlib.sha256).hexdigest()
            assert hmac.compare_digest(fields["v1"], expected)
            assert stripe_signature(slug, x["body"], t) == x["headers"]["Stripe-Signature"]


def _truth(small_world, slug, source) -> dict[str, str]:
    df = small_world["identity"][slug]
    df = df[df.source == source]
    return dict(zip(df.account_ref, df.true_sf_account_id, strict=True))


def test_vp_cases_and_injection_in_feed(small_world):
    sf = _bodies(small_world, "salesforce", "hearthline")
    names = {b["Id"]: b["Name"] for b in sf if b["attributes"]["type"] == "Account"}
    vp = {i for i, n in names.items() if n in VP_CASE_NAMES}
    assert len(vp) == 2
    lost = {b["AccountId"] for b in sf if b["attributes"]["type"] == "Opportunity"
            and b["StageName"] == "Closed Lost"}
    assert vp <= lost
    customers = {ref[len("stripe:"):] for ref, true in
                 _truth(small_world, "hearthline", "stripe").items() if true in vp}
    cancelled = {b["data"]["object"]["customer"] for b in _bodies(small_world, "stripe",
                                                                   "hearthline")
                 if b["type"] == "customer.subscription.updated"
                 and (b["data"]["object"]["cancel_at_period_end"]
                      or b["data"]["object"]["cancel_at"])}
    assert len(customers) == 2 and customers <= cancelled
    orgs = _truth(small_world, "hearthline", "zendesk")
    target = next(i for i, n in names.items() if n == INJECTION_ACCOUNT_NAME)
    hits = [b for b in _bodies(small_world, "zendesk", "hearthline")
            if INJECTION_TEXT in (b.get("description") or "")]
    assert len(hits) >= 2
    assert all(orgs.get(f"zd_org:{b['organization_id']}") == target for b in hits)


def _handles(src: str, x: dict, internal: str):
    """The account_ref values Appendix B derives from one feed line."""
    b = x["body"]
    if src == "salesforce":
        kind = b["attributes"]["type"]
        if kind == "Account":
            yield "sf:" + b["Id"]
        elif kind in ("Contact", "Opportunity"):
            yield "sf:" + b["AccountId"]
    elif src == "stripe":
        obj = b["data"]["object"]
        yield "stripe:" + (obj["id"] if obj["object"] == "customer" else obj["customer"])
    elif src == "zendesk":
        org = b["id"] if x["meta"]["export"] == "organizations" else b["organization_id"]
        if org is not None:
            yield f"zd_org:{org}"
    elif src == "gmail":
        h = _headers(b)
        sender = parseaddr(h["From"])[1].lower()
        if sender == "mailer-daemon@googlemail.com":
            yield "email:" + h["X-Failed-Recipients"].lower()
        elif sender.endswith("@" + internal):
            to = [a.strip().lower() for a in h["To"].split(",")]
            yield "email:" + next(a for a in to if not a.endswith("@" + internal))
        else:
            yield "email:" + sender
    else:
        yield "ws:" + (b["workspace_id"] if "schema" in b else b["context"]["groupId"])


def test_identity_truth_covers_every_handle(small_world):
    for slug, spec in TENANTS.items():
        df = small_world["identity"][slug]
        assert list(df.columns) == ["source", "account_ref", "true_sf_account_id"]
        known = set(zip(df.source, df.account_ref, strict=True))
        sf_ids = {b["Id"] for b in _bodies(small_world, "salesforce", slug)
                  if b["attributes"]["type"] == "Account"}
        assert set(df.true_sf_account_id) <= sf_ids
        for src in SOURCES:
            seen = {(src, ref) for x in small_world["lines"][slug, src]
                    for ref in _handles(src, x, spec.internal_domain)}
            assert seen and seen <= known, (slug, src, sorted(seen - known)[:3])


def test_full_scale_volume(tmp_path):
    out = tmp_path / "never"
    rows = generate(out, scale=1.0, count_only=True)["tenants"]
    assert not out.exists()
    assert 4800 <= sum(r["accounts"] for r in rows.values()) <= 5200
    assert 180_000 <= sum(r["history_records"] for r in rows.values()) <= 240_000


def test_subscription_period_end_is_current(small_world):
    """Monthly subs have no renewal facts (2a); every rendered subscription must still show
    the period it is in, not one that ended before the source start."""
    for slug in TENANTS:
        for b in _bodies(small_world, "stripe", slug):
            obj = b["data"]["object"]
            if obj["object"] != "subscription":
                continue
            end = obj.get("current_period_end") or obj["items"]["data"][0]["current_period_end"]
            assert end >= b["created"], (slug, b["id"])
            assert end - b["created"] <= 366 * 86400
