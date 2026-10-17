"""One entrypoint for every record, replayed or live: land it in raw_records idempotently,
normalise it, resolve identity, update projections and counters, all in the caller's
transaction.

Records are applied in (available_at, source, source_record_id) order, so a shuffled or
duplicated delivery of the same records produces the same events and links.
"""

import hashlib
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID

from psycopg.types.json import Jsonb

from radar import clock
from radar.config import load_settings
from radar.ingest import identity, webhooks
from radar.ingest.normalize import (
    HARD_BOUNCE,
    CanonicalEvent,
    NormalizeError,
    normalize,
    source_record_id,
)


@dataclass
class IngestStats:
    received: int = 0
    accepted: int = 0  # newly landed in raw_records (quarantined and twins included)
    duplicates: int = 0
    conflicts: int = 0
    twins: int = 0
    quarantined: dict[str, int] = field(default_factory=dict)
    triggers: list[UUID] = field(default_factory=list)  # accounts needing rule evaluation

    def __iadd__(self, other: "IngestStats") -> "IngestStats":
        for name in ("received", "accepted", "duplicates", "conflicts", "twins"):
            setattr(self, name, getattr(self, name) + getattr(other, name))
        self.quarantined = _merge(self.quarantined, other.quarantined)
        self.triggers += [a for a in other.triggers if a not in self.triggers]
        return self


@dataclass
class _Record:
    source: str
    body: dict
    meta: dict
    channel: str | None
    headers: dict
    at: datetime  # available_at
    srid: str
    digest: str
    events: list[CanonicalEvent]
    error: str | None


def _merge(a: dict, b: dict) -> dict:
    """Add nested counter dicts."""
    out = dict(a)
    for k, v in b.items():
        out[k] = _merge(out.get(k, {}), v) if isinstance(v, dict) else out.get(k, 0) + v
    return out


def _prepare(rec: dict, available_at: datetime | None) -> _Record:
    source, body, meta = rec["source"], rec["body"], rec.get("meta") or {}
    digest = hashlib.sha256(webhooks.body_bytes(body)).hexdigest()
    srid, events, error = f"unparseable:{digest}", [], None
    try:
        srid = source_record_id(source, body, meta)
        events = normalize(source, body, meta)
    except NormalizeError as e:
        error = e.reason
    at = available_at or datetime.fromisoformat(rec["deliver_at"])
    return _Record(source, body, meta, rec.get("channel"), rec.get("headers") or {}, at, srid,
                   digest, events, error)


def ingest(conn, tenant_id, records: Iterable[dict],
           available_at: datetime | None = None) -> IngestStats:
    """available_at=None means each record's deliver_at (feed replay); the live webhook path
    passes clock.now()."""
    prepared = sorted((_prepare(r, available_at) for r in records),
                      key=lambda r: (r.at, r.source, r.srid, r.digest))
    run = _Run(conn, tenant_id)
    for rec, raw_id in run.land(prepared):
        run.apply(rec, raw_id)
    return run.finish()


RAW_INSERT = """
INSERT INTO raw_records (source, source_record_id, payload, payload_hash, channel, headers,
                         meta, occurred_at, available_at)
SELECT * FROM unnest(%s::text[], %s::text[], %s::jsonb[], %s::text[], %s::text[], %s::jsonb[],
                     %s::jsonb[], %s::timestamptz[], %s::timestamptz[])
ON CONFLICT (tenant_id, source, source_record_id) DO NOTHING
RETURNING id, source, source_record_id
"""
EVENT_INSERT = """
INSERT INTO events (raw_record_id, source, type, entity_id, account_ref, occurred_at,
                    available_at, version_key, status, priority, amount_minor, quantity, attrs)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
"""


class _Run:
    """State for one ingest() call: counters, pending event rows and identity memo."""

    def __init__(self, conn, tenant_id):
        self.conn, self.tenant_id = conn, tenant_id
        tenant = conn.execute("SELECT slug, internal_domain, config FROM tenants WHERE id = %s",
                              (tenant_id,)).fetchone()
        self.internal_domain = tenant["internal_domain"]
        self.csm_field = tenant["config"]["sf"]["csm_field"]  # seeded from runtime_config()
        self.secrets = webhooks.stripe_secrets(tenant["slug"], load_settings().webhook_secrets)
        self.counts: dict[str, dict] = {}
        self.last_at: dict[str, datetime] = {}
        self.events: list[tuple] = []
        self.memo: dict[str, UUID] = {}  # account_ref -> account id, resolved in this call
        self.seen: set = set()  # gmail messages and Stripe states applied in this call
        self.tickets: dict[str, tuple] = {}  # ticket entity -> (status, priority, tags)
        self.triggers: list[UUID] = []
        self.retry_keys: set[str] = set()
        self.retry_at: datetime | None = None

    def bump(self, source: str, key: str, sub: str | None = None) -> None:
        c = self.counts.setdefault(source, {})
        if sub is None:
            c[key] = c.get(key, 0) + 1
        else:
            d = c.setdefault(key, {})
            d[sub] = d.get(sub, 0) + 1

    def quarantine(self, rec: _Record, raw_id, reason: str, ref: str | None = None,
                   details: dict | None = None) -> None:
        self.bump(rec.source, "quarantined", reason)
        identity.quarantine(self.conn, raw_id, rec.source, ref, reason, rec.at, details or {})

    def land(self, recs: list[_Record]) -> list[tuple[_Record, int]]:
        """Insert unseen records; count duplicates; quarantine same-id, different-body resends."""
        keys = {(r.source, r.srid) for r in recs}
        existing = {(r["source"], r["source_record_id"]): (r["id"], r["payload_hash"])
                    for r in self.conn.execute(
                        "SELECT id, source, source_record_id, payload_hash FROM raw_records"
                        " WHERE (source, source_record_id) IN"
                        " (SELECT * FROM unnest(%s::text[], %s::text[]))",
                        ([k[0] for k in keys], [k[1] for k in keys]))}
        fresh, conflicts, seen = [], [], {}
        for r in recs:
            self.bump(r.source, "received")
            self.last_at[r.source] = max(r.at, self.last_at.get(r.source, r.at))
            prior = seen.get((r.source, r.srid)) or existing.get((r.source, r.srid), (0, None))[1]
            if prior is None:
                seen[(r.source, r.srid)] = r.digest
                fresh.append(r)
            elif prior == r.digest:
                self.bump(r.source, "duplicates")
            else:
                conflicts.append(r)
        ids = {}
        if fresh:
            cols = zip(*[(r.source, r.srid, Jsonb(r.body), r.digest, r.channel,
                          Jsonb(r.headers), Jsonb(r.meta),
                          min((e.occurred_at for e in r.events), default=None), r.at)
                         for r in fresh], strict=True)
            ids = {(x["source"], x["source_record_id"]): x["id"]
                   for x in self.conn.execute(RAW_INSERT, [list(c) for c in cols]).fetchall()}
        for r in conflicts:
            self.bump(r.source, "conflicts")
            key = (r.source, r.srid)
            original = ids.get(key) or existing.get(key, (None,))[0]
            self.quarantine(r, original, "conflicting_resend",
                            details={"payload": r.body, "payload_hash": r.digest})
        out = []
        for r in fresh:
            if (r.source, r.srid) in ids:
                self.bump(r.source, "accepted")
                out.append((r, ids[(r.source, r.srid)]))
            else:  # a concurrent transaction landed it first
                self.bump(r.source, "duplicates")
        return out

    def apply(self, rec: _Record, raw_id: int) -> None:
        if self.retry_at and not _is_sf_party(rec):
            self.retry()
        if rec.error:
            return self.quarantine(rec, raw_id, rec.error)
        if rec.source == "stripe" and not self.signed(rec):
            return self.quarantine(rec, raw_id, "bad_signature")
        for ev in rec.events:
            if ev.occurred_at > rec.at:  # known before it happened: a bad device clock
                self.bump(rec.source, "future_timestamps")
                self.quarantine(rec, raw_id, "future_timestamp", ev.account_ref)
                continue
            if self.is_copy(ev):
                continue
            self.count(rec, ev)
            if ev.type == "crm.account":
                self.project_account(ev, rec)
            account_id = self.resolve(ev, rec, raw_id)
            if ev.type == "crm.contact":
                self.project_contact(ev, account_id, rec)
            elif ev.type == "crm.opportunity" and account_id and ev.entity_id not in self.memo:
                if identity.resolve_ref(self.conn, self.tenant_id, "salesforce", ev.entity_id,
                                        {}, rec.at)[0] is None:
                    identity.link(self.conn, self.tenant_id, "salesforce", ev.entity_id,
                                  account_id, "sf_opportunity", rec.at)
                self.memo[ev.entity_id] = account_id
            elif ev.type == "email.message" and ev.attrs["dsn_status"] in HARD_BOUNCE:
                self.conn.execute(
                    "UPDATE contacts SET hard_bounced_at = %s"
                    " WHERE email = %s AND hard_bounced_at IS NULL",
                    (ev.occurred_at, ev.attrs["failed_recipient"]))
            if account_id and account_id not in self.triggers and self.trigger_worthy(ev):
                self.triggers.append(account_id)
            self.events.append((raw_id, ev.source, ev.type, ev.entity_id, ev.account_ref,
                                ev.occurred_at, rec.at, ev.version_key, ev.status, ev.priority,
                                ev.amount_minor, ev.quantity, Jsonb(ev.attrs)))

    def signed(self, rec: _Record) -> bool:
        try:
            webhooks.verify(rec.headers.get("Stripe-Signature", ""),
                            webhooks.body_bytes(rec.body), self.secrets, int(rec.at.timestamp()))
            return True
        except webhooks.WebhookError:
            return False

    def is_copy(self, ev: CanonicalEvent) -> bool:
        """A Gmail message already seen in another mailbox, or a Stripe semantic twin."""
        if ev.type == "email.message":
            key, sql, args = ev.entity_id, "SELECT 1 FROM events WHERE entity_id = %s", ()
        elif ev.source == "stripe":
            key = (ev.entity_id, ev.attrs["state_hash"])
            sql, args = ("SELECT 1 FROM events WHERE entity_id = %s"
                         " AND attrs->>'state_hash' = %s", (ev.attrs["state_hash"],))
        else:
            return False
        copy = key in self.seen or self.conn.execute(
            sql + " LIMIT 1", (ev.entity_id, *args)).fetchone() is not None
        self.seen.add(key)
        if copy and ev.source == "stripe":
            self.bump("stripe", "twins")
        return copy

    def count(self, rec: _Record, ev: CanonicalEvent) -> None:
        a = ev.attrs
        if "api_version" in a or "schema" in a:
            self.bump(rec.source, "schema_versions", a.get("api_version") or a["schema"])
        if ev.type == "product.usage" and a["version"] > 1:
            self.bump(rec.source, "restatements")
        if ev.type == "email.message":
            if a["is_auto_reply"]:
                self.bump(rec.source, "auto_replies")
            if a["is_dsn"]:
                self.bump(rec.source, "dsn")

    def resolve(self, ev: CanonicalEvent, rec: _Record, raw_id: int) -> UUID | None:
        ref = ev.account_ref
        if ref is None:
            if ev.type == "support.ticket":
                self.quarantine(rec, raw_id, "no_org")
            return None
        if ref in self.memo:
            return self.memo[ref]
        hints = _hints(ev)
        account_id, reason = identity.resolve_ref(self.conn, self.tenant_id, rec.source, ref,
                                                  hints, rec.at)
        if account_id is None:
            self.quarantine(rec, raw_id, reason, ref,
                            {"hints": hints, "keys": identity.match_keys(ref, hints)})
        else:
            self.memo[ref] = account_id
        return account_id

    def project_account(self, ev: CanonicalEvent, rec: _Record) -> None:
        """Upsert the routing projection (newer SystemModstamp wins), link sf:<Id>, merge."""
        a, sf_id = ev.attrs, ev.entity_id.split(":", 1)[1]
        owner = rec.body.get(self.csm_field)
        self.conn.execute(
            "INSERT INTO accounts (sf_account_id, name, domain, owner_user_id, owner_sf_user_id,"
            " segment, is_test, source_modstamp, created_at, updated_at)"
            " VALUES (%(sf)s, %(name)s, %(domain)s,"
            " (SELECT id FROM users WHERE sf_user_id = %(owner)s), %(owner)s, %(segment)s,"
            " %(test)s, %(stamp)s, %(at)s, %(at)s)"
            " ON CONFLICT (tenant_id, sf_account_id) DO UPDATE SET name = EXCLUDED.name,"
            " domain = EXCLUDED.domain, owner_user_id = EXCLUDED.owner_user_id,"
            " owner_sf_user_id = EXCLUDED.owner_sf_user_id, segment = EXCLUDED.segment,"
            " is_test = EXCLUDED.is_test, source_modstamp = EXCLUDED.source_modstamp,"
            " updated_at = EXCLUDED.updated_at"
            " WHERE accounts.source_modstamp < EXCLUDED.source_modstamp",
            {"sf": sf_id, "name": a["name"], "domain": a["domain"], "owner": owner,
             "segment": a["segment"], "stamp": ev.occurred_at, "at": rec.at,
             "test": a["is_test"] or a["domain"] == self.internal_domain})
        self.retry_keys |= {k for k in (sf_id, a["domain"]) if k}
        self.retry_at = rec.at
        master_sf = a["master_record_id"] if a["is_deleted"] else None
        if not master_sf:
            return
        master = self.conn.execute("SELECT id FROM accounts WHERE sf_account_id = %s",
                                   (master_sf,)).fetchone()
        # shortcut: a merge whose master is not loaded yet is skipped; SF exports the master
        # first, so this only matters for a hand-built feed
        if master and self.resolve(ev, rec, None) != master["id"]:
            self.conn.execute("UPDATE accounts SET merged_into = %s WHERE sf_account_id = %s",
                              (master["id"], sf_id))
            identity.link(self.conn, self.tenant_id, "salesforce", ev.account_ref, master["id"],
                          "merge", rec.at)
            self.memo[ev.account_ref] = master["id"]

    def project_contact(self, ev: CanonicalEvent, account_id: UUID | None, rec: _Record) -> None:
        a = ev.attrs
        # shortcut: departed = opted out or deleted (the generator unflags and opts out a contact
        # who left); account_id stays NULL if the contact arrives before its account
        self.conn.execute(
            "INSERT INTO contacts (account_id, sf_contact_id, email, first_name, last_name, title,"
            " is_champion, opted_out, departed, source_modstamp)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
            " ON CONFLICT (tenant_id, sf_contact_id) DO UPDATE SET"
            " account_id = EXCLUDED.account_id, email = EXCLUDED.email,"
            " first_name = EXCLUDED.first_name, last_name = EXCLUDED.last_name,"
            " title = EXCLUDED.title, is_champion = EXCLUDED.is_champion,"
            " opted_out = EXCLUDED.opted_out, departed = EXCLUDED.departed,"
            " source_modstamp = EXCLUDED.source_modstamp"
            " WHERE contacts.source_modstamp < EXCLUDED.source_modstamp",
            (account_id, ev.entity_id.split(":", 1)[1], a["email"], a["first_name"],
             a["last_name"], a["title"], a["champion"], a["opted_out"],
             a["opted_out"] or a["is_deleted"], ev.occurred_at))
        if a["email"]:
            self.retry_keys.add(a["email"])
        self.retry_at = rec.at

    def retry(self) -> None:
        """SF accounts or contacts just arrived: re-resolve the open refs they could match.
        The resulting links carry the SF rows' available_at."""
        identity.retry_quarantine(self.conn, self.tenant_id, self.retry_at,
                                  sorted(self.retry_keys))
        self.retry_keys, self.retry_at = set(), None

    def trigger_worthy(self, ev: CanonicalEvent) -> bool:
        """Design §8: payment failure, subscription change, ticket status/priority/tag
        change, DSN."""
        if ev.type == "billing.subscription":
            return True
        if ev.type == "billing.invoice":
            return ev.attrs["event_type"] == "invoice.payment_failed"
        if ev.type == "email.message":
            return ev.attrs["is_dsn"]
        if ev.type != "support.ticket":
            return False
        state = (ev.status, ev.priority, sorted(ev.attrs["tags"]))
        prev = self.tickets.get(ev.entity_id)
        if prev is None:
            row = self.conn.execute(
                "SELECT status, priority, attrs->'tags' AS tags FROM events WHERE entity_id = %s"
                " ORDER BY occurred_at DESC, version_key DESC LIMIT 1", (ev.entity_id,)).fetchone()
            prev = row and (row["status"], row["priority"], sorted(row["tags"]))
        self.tickets[ev.entity_id] = state
        return prev != state

    def finish(self) -> IngestStats:
        if self.retry_at:
            self.retry()
        if self.events:
            with self.conn.cursor() as cur:
                cur.executemany(EVENT_INSERT, self.events)
        stats = IngestStats(triggers=self.triggers)
        for source, counts in self.counts.items():
            for name in ("received", "accepted", "duplicates", "conflicts", "twins"):
                setattr(stats, name, getattr(stats, name) + counts.get(name, 0))
            stats.quarantined = _merge(stats.quarantined, counts.get("quarantined", {}))
            self.conn.execute(
                "INSERT INTO source_status (source) VALUES (%s) ON CONFLICT DO NOTHING", (source,))
            row = self.conn.execute("SELECT counters FROM source_status WHERE source = %s"
                                    " FOR UPDATE", (source,)).fetchone()
            self.conn.execute(
                "UPDATE source_status SET counters = %s, last_ingest_at = %s,"
                " last_deliver_at = GREATEST(last_deliver_at, %s) WHERE source = %s",
                (Jsonb(_merge(row["counters"], counts)), clock.now(), self.last_at[source],
                 source))
        return stats


def _is_sf_party(rec: _Record) -> bool:
    return rec.source == "salesforce" and rec.body.get("attributes", {}).get("type") in (
        "Account", "Contact")


def _hints(ev: CanonicalEvent) -> dict:
    """The exact keys a record carries for its account_ref (design §4.2)."""
    a = ev.attrs
    if ev.type == "billing.customer":
        return {"sf_account_id": a["sf_account_id_raw"],
                "domains": [a["email_domain"]] if a["email_domain"] else []}
    if ev.type == "support.organization":
        return {"sf_account_id": a["external_id_raw"], "domains": a["domain_names"]}
    if ev.type == "product.usage":
        return {"sf_account_id": a["sf_account_id_raw"]}
    return {}
