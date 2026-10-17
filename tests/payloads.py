"""Golden feed envelopes (copied from a generated world) and small builders for tests."""

import copy
import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from radar.ingest import pipeline, webhooks

FIXTURES = Path(__file__).parent / "fixtures" / "payloads"
T0 = datetime(2025, 3, 3, 7, 0, tzinfo=UTC)


def golden(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text())


def iso(at: datetime) -> str:
    return at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def at(rec: dict, when: datetime) -> dict:
    """The same record delivered at `when`."""
    return copy.deepcopy(rec) | {"deliver_at": iso(when)}


def sf(kind: str, when: datetime, **fields) -> dict:
    """A Salesforce row of `kind` (account, contact, ...) changed at `when`, delivered 1 h on."""
    rec = at(golden(f"salesforce_{kind}"), when + timedelta(hours=1))
    stamp = iso(when)[:-1] + ".000+0000"
    rec["body"] |= {"SystemModstamp": stamp} | fields
    return rec


def account(sf_id: str, domain: str, when: datetime = T0, **fields) -> dict:
    return sf("account", when, Id=sf_id, Website=f"https://www.{domain}", **fields)


def stripe(name: str, slug: str, when: datetime, **obj) -> dict:
    """A Stripe golden event re-dated to `when`, data.object fields overridden, re-signed."""
    rec = at(golden(name), when)
    rec["body"] |= {"id": "evt_" + uuid.uuid4().hex[:24], "created": int(when.timestamp()) - 5}
    rec["body"]["data"]["object"] |= obj
    return signed(rec, slug)


def signed(rec: dict, slug: str) -> dict:
    t = int(datetime.fromisoformat(rec["deliver_at"]).timestamp())
    header = webhooks.sign(f"whsec_demo_{slug}", f"{slug}-stripe",
                           webhooks.body_bytes(rec["body"]), t)
    rec["headers"] = {"Stripe-Signature": header}
    return rec


def ingest(db, tenant_id, *recs, available_at=None):
    with db.tenant_tx(tenant_id) as conn:
        return pipeline.ingest(conn, tenant_id, list(recs), available_at)


def rows(db, tenant_id, sql: str, *params) -> list[dict]:
    with db.tenant_tx(tenant_id) as conn:
        return conn.execute(sql, params).fetchall()
