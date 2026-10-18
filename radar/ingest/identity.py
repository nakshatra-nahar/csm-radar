"""Exact-key account identity (design §4.2). No fuzzy matching: a wrong match leaks one
customer's data into another's account; a missing match is visible and fixable.

Links are append-only; resolution at T takes the latest link per account_ref with
available_at <= T. A link's available_at is when the record that made the match arrived.
"""

import string
from datetime import datetime
from uuid import UUID

from psycopg.types.json import Jsonb

FREE_MAIL = frozenset({
    "gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "live.com", "yahoo.com",
    "icloud.com", "me.com", "aol.com", "proton.me", "protonmail.com",
    # real-estate franchises: every agent of the brand shares the domain
    "kw.com", "remax.net", "compass.com", "exprealty.com", "coldwellbanker.com",
})
IDENTITY_REASONS = ["no_key", "unknown_external_id", "ambiguous_domain", "free_mail_domain"]
EXTERNAL_ID_METHOD = {"stripe": "stripe_metadata", "zd_org": "zendesk_external_id",
                      "ws": "product_metadata"}
_CHECKSUM = string.ascii_uppercase + "012345"


def normalize_sf_id(value: str | None) -> str | None:
    """A Salesforce Account id as typed into other tools -> its 18-char form, else None.

    The 18-char suffix encodes the case of the first 15, so even a lowercased id is recovered.
    """
    v = (value or "").strip()
    if len(v) not in (15, 18) or not v.isalnum():
        return None
    if len(v) == 15:
        suffix = "".join(_CHECKSUM[sum(1 << i for i, c in enumerate(v[k:k + 5]) if c.isupper())]
                         for k in (0, 5, 10))
        body = v
    else:
        suffix = v[15:].upper()
        if any(c not in _CHECKSUM for c in suffix):
            return None
        bits = [_CHECKSUM.index(c) for c in suffix]
        body = "".join(c.upper() if bits[i // 5] >> (i % 5) & 1 else c.lower()
                       for i, c in enumerate(v[:15]))
    return body + suffix if body.startswith("001") else None


def match_keys(account_ref: str, hints: dict) -> list[str]:
    """What an arriving SF account or contact would have to carry to resolve this ref."""
    prefix, key = account_ref.split(":", 1)
    keys = {normalize_sf_id(hints.get("sf_account_id")), *hints.get("domains", [])}
    if prefix == "sf":
        keys.add(normalize_sf_id(key))
    if prefix == "email":
        keys |= {key, key.rsplit("@", 1)[-1]}
    return sorted(k for k in keys if k)


def resolve_ref(conn, tenant_id, source: str, account_ref: str, hints: dict,
                available_at: datetime) -> tuple[UUID | None, str | None]:
    """(account id, None) when resolved, else (None, quarantine reason). A ref linked by the
    domain heuristic is re-matched when its carrier record later names the account: an exact
    key then appends an exact link."""
    row = conn.execute(
        "SELECT account_id, method FROM identity_links WHERE tenant_id = %s AND account_ref = %s"
        " ORDER BY available_at DESC, id DESC LIMIT 1", (tenant_id, account_ref)).fetchone()
    if row and not (row["method"] == "domain" and normalize_sf_id(hints.get("sf_account_id"))):
        return row["account_id"], None
    if row:
        found, method, _ = _match(conn, tenant_id, account_ref, hints)
        if found is None:  # the named id is unknown: keep the domain link
            return row["account_id"], None
        link(conn, tenant_id, source, account_ref, found, method, available_at)
        return found, None
    account_id, method, reason = _match(conn, tenant_id, account_ref, hints)
    if account_id is None:
        return None, reason
    link(conn, tenant_id, source, account_ref, account_id, method, available_at)
    return account_id, None


def _match(conn, tenant_id, account_ref: str, hints: dict):
    prefix, key = account_ref.split(":", 1)
    if prefix == "sf":
        found = _account_by_sf_id(conn, tenant_id, normalize_sf_id(key))
        return (found, "sf_id", None) if found else (None, None, "unknown_external_id")
    sf_id = normalize_sf_id(hints.get("sf_account_id"))
    if sf_id:  # an explicit id is authoritative: never fall back to a domain guess
        found = _account_by_sf_id(conn, tenant_id, sf_id)
        method = EXTERNAL_ID_METHOD[prefix]
        return (found, method, None) if found else (None, None, "unknown_external_id")
    domains = hints.get("domains", [])
    if prefix == "email":
        rows = conn.execute(
            "SELECT DISTINCT account_id FROM contacts WHERE tenant_id = %s AND email = %s"
            " AND account_id IS NOT NULL LIMIT 2", (tenant_id, key)).fetchall()
        if len(rows) == 1:
            return rows[0]["account_id"], "contact_email", None
        domains = [key.rsplit("@", 1)[-1]]
    if not hints and prefix != "email":  # the carrier record (org, customer) is not known yet
        return None, None, "unknown_external_id"
    corporate = [d for d in domains if d not in FREE_MAIL]
    if not corporate:
        return None, None, "free_mail_domain" if domains else "no_key"
    rows = conn.execute(
        "SELECT DISTINCT id FROM accounts WHERE tenant_id = %s AND domain = ANY(%s)"
        " AND NOT is_test AND merged_into IS NULL LIMIT 2", (tenant_id, corporate)).fetchall()
    if len(rows) == 1:
        return rows[0]["id"], "domain", None
    return None, None, "ambiguous_domain" if rows else "no_key"


def _account_by_sf_id(conn, tenant_id, sf_id: str | None) -> UUID | None:
    if sf_id is None:
        return None
    row = conn.execute("SELECT id FROM accounts WHERE tenant_id = %s AND sf_account_id = %s",
                       (tenant_id, sf_id)).fetchone()
    return row["id"] if row else None


def link(conn, tenant_id, source: str, account_ref: str, account_id: UUID, method: str,
         available_at: datetime) -> int:
    """Append a link and close the ref's open quarantine rows with it."""
    link_id = conn.execute(
        "INSERT INTO identity_links (source, account_ref, account_id, method, available_at)"
        " VALUES (%s, %s, %s, %s, %s) RETURNING id",
        (source, account_ref, account_id, method, available_at)).fetchone()["id"]
    conn.execute(
        "UPDATE quarantine SET resolved_at = %s, resolved_link_id = %s"
        " WHERE tenant_id = %s AND account_ref = %s AND resolved_at IS NULL",
        (available_at, link_id, tenant_id, account_ref))
    return link_id


def quarantine(conn, raw_record_id: int | None, source: str, account_ref: str | None,
               reason: str, available_at: datetime, details: dict) -> None:
    conn.execute(
        "INSERT INTO quarantine (raw_record_id, source, account_ref, reason, details,"
        " available_at) VALUES (%s, %s, %s, %s, %s, %s)",
        (raw_record_id, source, account_ref, reason, Jsonb(details), available_at))


def retry_quarantine(conn, tenant_id, available_at: datetime, keys: list[str]) -> int:
    """Re-resolve open identity quarantine; returns the number of refs resolved.

    `keys` (sf ids, domains, emails of the SF rows that just arrived) limits the retry to refs
    those rows could resolve, so a nightly batch does not re-try every open ref.
    """
    refs = conn.execute(
        "SELECT DISTINCT ON (account_ref) source, account_ref, details->'hints' AS hints"
        " FROM quarantine WHERE tenant_id = %s AND resolved_at IS NULL"
        " AND account_ref IS NOT NULL AND reason = ANY(%s) AND details->'keys' ?| %s"
        " ORDER BY account_ref, details->'hints' = '{}'::jsonb, id",
        (tenant_id, IDENTITY_REASONS, keys)).fetchall()
    return sum(resolve_ref(conn, tenant_id, r["source"], r["account_ref"], r["hints"] or {},
                           available_at)[0] is not None for r in refs)
