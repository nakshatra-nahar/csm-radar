"""Gmail messages in format=metadata, one per CSM mailbox that holds them (design §3.5)."""

from __future__ import annotations

from datetime import UTC, timedelta
from email.utils import format_datetime
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

import numpy as np

from radar.sim import delivery
from radar.sim.model import Email
from radar.sim.names import token
from radar.sim.params import PARAMS
from radar.sim.signals import MAILER_DAEMON

if TYPE_CHECKING:
    from radar.sim.world import World

HEX = "0123456789abcdef"
CUSTOMER_ZONES = ["America/New_York", "America/Chicago", "America/Denver",
                  "America/Los_Angeles", "Europe/London", "Asia/Kolkata", "UTC"]


def emit(world: World, rng: np.random.Generator) -> tuple[list[dict], set[tuple[str, str]]]:
    """Feed lines and the identity truth of every `email:` counterparty handle."""
    start = delivery.source_start(world, "gmail")
    names = {r["email"]: r["name"] for r in world.staff}
    last_in_thread: dict[str, str] = {}  # thread_key -> Message-ID, for In-Reply-To
    thread_ids: dict[tuple[str, str], str] = {}  # (mailbox, thread_key) -> Gmail threadId
    rows, handles = [], set()
    for e in world.emails:
        if e.sent_at < start:
            continue
        domain = "mail.gmail.com" if e.from_addr in names or e.dsn_status else \
            e.from_addr.split("@")[1]
        message_id = f"<{token(rng, 20)}@{domain}>"
        headers = _headers(world, e, names, message_id, last_in_thread.get(e.thread_key), rng)
        last_in_thread[e.thread_key] = message_id
        for box in e.mailboxes:
            gid = token(rng, 16, HEX)
            body = {"id": gid, "threadId": thread_ids.setdefault((box, e.thread_key), gid),
                    "labelIds": ["SENT"] if box == e.from_addr else ["INBOX"],
                    "internalDate": str(int(e.sent_at.timestamp() * 1000)),
                    "payload": _payload(e, headers)}
            rows.append((e.sent_at, body, box))
        handles.add(("email:" + _counterparty(world, e), e.sf_account_id))
    return delivery.gmail(rows, rng), handles


def _counterparty(world: World, e: Email) -> str:
    """Appendix B's account handle: the bounced recipient, else the non-CSM side."""
    if e.dsn_status:
        return e.failed_recipient
    if e.direction == "outbound":
        return next(a for a in e.to if not a.endswith("@" + world.spec.internal_domain))
    return e.from_addr


def _headers(world: World, e: Email, names: dict, message_id: str, parent: str | None,
             rng: np.random.Generator) -> list[dict]:
    if e.dsn_status:
        sender, zone = f"Mail Delivery Subsystem <{MAILER_DAEMON}>", "UTC"
    elif e.from_addr in names:
        sender, zone = f"{names[e.from_addr]} <{e.from_addr}>", world.spec.timezone
    else:
        sender, zone = e.from_addr, CUSTOMER_ZONES[rng.integers(len(CUSTOMER_ZONES))]
    h = {"From": sender, "To": ", ".join(e.to), "Cc": ", ".join(e.cc),
         "Date": _date(e, zone, rng), "Subject": e.subject, "Message-ID": message_id,
         "In-Reply-To": parent,
         "Auto-Submitted": "auto-replied" if e.is_auto_reply or e.dsn_status else None,
         "X-Failed-Recipients": e.failed_recipient,
         "Content-Type": ('multipart/report; report-type=delivery-status; boundary="b1"'
                          if e.dsn_status else 'multipart/alternative; boundary="b1"')}
    return [{"name": k, "value": v} for k, v in h.items() if v]


def _date(e: Email, zone: str, rng: np.random.Generator) -> str:
    """The sender's clock, sometimes minutes off; internalDate is the truth."""
    at = e.sent_at
    if rng.random() < PARAMS["feed"]["gmail"]["date_skew"]:
        at += timedelta(minutes=float(rng.uniform(-9, 9)))
    r = rng.random()
    if r < 0.1:  # '-0000': no information about the sender's zone
        return format_datetime(at.astimezone(UTC).replace(tzinfo=None))
    if r < 0.15:
        return format_datetime(at.astimezone(UTC)) + " (UTC)"
    return format_datetime(at.astimezone(ZoneInfo(zone)))


def _payload(e: Email, headers: list[dict]) -> dict:
    """A DSN's per-recipient status lives in its message/delivery-status part (RFC 3464)."""
    if not e.dsn_status:
        return {"mimeType": "multipart/alternative", "headers": headers}
    action = "delayed" if e.dsn_status.startswith("4") else "failed"
    report = [{"name": "Final-Recipient", "value": "rfc822; " + e.failed_recipient},
              {"name": "Action", "value": action}, {"name": "Status", "value": e.dsn_status}]
    return {"mimeType": "multipart/report", "headers": headers,
            "parts": [{"partId": "0", "mimeType": "text/plain", "headers": []},
                      {"partId": "1", "mimeType": "message/delivery-status",
                       "headers": report}]}
