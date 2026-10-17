"""Native source payloads -> canonical events (design Appendix B). Pure functions, no I/O.

The generator's feed (radar/sim) is the contract for payload shapes.
"""

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from email.utils import getaddresses, parseaddr


@dataclass(frozen=True)
class CanonicalEvent:
    source: str
    type: str
    entity_id: str
    account_ref: str | None
    occurred_at: datetime
    version_key: str
    status: str | None = None
    priority: str | None = None
    amount_minor: int | None = None
    quantity: int | None = None
    attrs: dict = field(default_factory=dict)


class NormalizeError(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def source_record_id(source: str, body: dict, meta: dict) -> str:
    """The ledger's idempotency key per source (design §4.1)."""
    try:
        match source:
            case "stripe":
                return body["id"]
            case "zendesk" if meta.get("export") == "organizations":
                return f"org:{body['id']}:{body['updated_at']}"
            case "zendesk":
                return f"{body['id']}:{body['updated_at']}:{body['generated_timestamp']}"
            case "salesforce":
                return f"{body['Id']}:{body['SystemModstamp']}"
            case "gmail":
                return f"{meta['mailbox']}:{body['id']}"
            case "product" if "messageId" in body:
                return body["messageId"]
            case "product":
                return f"{body['workspace_id']}:{body['period_end']}:v{body['version']}"
    except (KeyError, TypeError) as e:
        raise NormalizeError("unparseable") from e
    raise NormalizeError("unparseable")


def normalize(source: str, body: dict, meta: dict) -> list[CanonicalEvent]:
    parsers = {"stripe": _stripe, "zendesk": _zendesk, "salesforce": _salesforce,
               "gmail": _gmail, "product": _product}
    if source not in parsers:
        raise NormalizeError("unparseable")
    try:
        return parsers[source](body, meta)
    except (KeyError, TypeError, ValueError, AttributeError, IndexError) as e:
        raise NormalizeError("unparseable") from e


def _ts(value: str) -> datetime:
    """ISO 8601 with Z or +0000 offsets, always tz-aware UTC."""
    at = datetime.fromisoformat(value)
    if at.tzinfo is None:
        raise ValueError(f"naive timestamp {value!r}")
    return at.astimezone(UTC)


def _epoch(value: int) -> datetime:
    return datetime.fromtimestamp(value, UTC)


def _domain(email: str | None) -> str | None:
    return email.rsplit("@", 1)[1].lower() if email and "@" in email else None


# --- Stripe -------------------------------------------------------------------------------

# Same-second ties: the terminal state sorts last, so it is the version that wins.
STATE_RANK = {"canceled": 2, "past_due": 1, "unpaid": 1}


def _stripe(body: dict, meta: dict) -> list[CanonicalEvent]:
    kind, obj, created, evt = body["type"], body["data"]["object"], body["created"], body["id"]
    common = {"source": "stripe", "occurred_at": _epoch(created),
              "account_ref": f"stripe:{obj['customer'] if 'customer' in obj else obj['id']}"}
    # Semantic twins (new evt id, same type and data) are detected on this hash.
    state = hashlib.sha256(json.dumps([kind, body["data"]], sort_keys=True).encode()).hexdigest()
    base = {"event_type": kind, "api_version": body["api_version"], "state_hash": state}
    if kind.startswith("customer.subscription."):
        items = obj["items"]["data"]
        ends = [obj.get("current_period_end")] + [i.get("current_period_end") for i in items]
        rank = STATE_RANK.get(obj["status"], 0)
        return [CanonicalEvent(
            **common, type="billing.subscription", entity_id=f"stripe_sub:{obj['id']}",
            version_key=f"{created:010d}:{rank}:{evt}", status=obj["status"],
            quantity=sum(i["quantity"] for i in items),
            attrs=base | {
                "items": [{"id": i["id"], "price_id": i["price"]["id"],
                           "unit_amount": i["price"]["unit_amount"],
                           "currency": i["price"]["currency"],
                           "interval": i["price"]["recurring"]["interval"],
                           "interval_count": i["price"]["recurring"]["interval_count"],
                           "quantity": i["quantity"]} for i in items],
                "cancel_at_period_end": obj["cancel_at_period_end"],
                "cancel_at": obj["cancel_at"], "canceled_at": obj["canceled_at"],
                "ended_at": obj["ended_at"],
                "cancellation_reason": (obj.get("cancellation_details") or {}).get("reason"),
                "current_period_end": max(e for e in ends if e is not None)})]
    version = f"{created:010d}:{evt}"
    if kind.startswith("invoice."):
        sub = obj.get("subscription") or obj["parent"]["subscription_details"]["subscription"]
        return [CanonicalEvent(
            **common, type="billing.invoice", entity_id=f"stripe_inv:{obj['id']}",
            version_key=version, status=obj["status"], amount_minor=obj["amount_due"],
            attrs=base | {"attempt_count": obj["attempt_count"],
                          "next_payment_attempt": obj["next_payment_attempt"],
                          "paid": obj.get("paid", obj["status"] == "paid"),
                          "subscription_id": sub, "currency": obj["currency"],
                          "period_end": obj["lines"]["data"][0]["period"]["end"]})]
    if kind.startswith("customer."):
        md = obj.get("metadata") or {}
        return [CanonicalEvent(
            **common, type="billing.customer", entity_id=f"stripe_cus:{obj['id']}",
            version_key=version,
            attrs=base | {"email": obj["email"], "email_domain": _domain(obj["email"]),
                          "name": obj["name"], "currency": obj["currency"],
                          "sf_account_id_raw": md.get("sf_account_id") or md.get("salesforce_id")})]
    raise ValueError(f"unsupported Stripe event type {kind}")


# --- Zendesk ------------------------------------------------------------------------------

def _zendesk(body: dict, meta: dict) -> list[CanonicalEvent]:
    at = _ts(body["updated_at"])
    if meta.get("export") == "organizations":
        ref = f"zd_org:{body['id']}"
        return [CanonicalEvent(
            "zendesk", "support.organization", ref, ref, at, body["updated_at"],
            attrs={"name": body["name"], "external_id_raw": body["external_id"],
                   "domain_names": [d.lower() for d in body["domain_names"]]})]
    org = body["organization_id"]
    return [CanonicalEvent(
        "zendesk", "support.ticket", f"zd_ticket:{body['id']}",
        f"zd_org:{org}" if org is not None else None, at,
        f"{body['updated_at']}:{body['generated_timestamp']}",
        status=body["status"], priority=body["priority"],
        attrs={"ticket_id": body["id"], "subject": (body["subject"] or "")[:200],
               "description_excerpt": (body["description"] or "")[:500],
               "tags": body["tags"], "type": body["type"],
               "via_channel": (body.get("via") or {}).get("channel"),
               "csat": (body.get("satisfaction_rating") or {}).get("score"),
               "created_at": body["created_at"], "problem_id": body.get("problem_id")})]


# --- Salesforce ---------------------------------------------------------------------------

def _minor(amount: float | None) -> int | None:
    return None if amount is None else round(amount * 100)


def _website_domain(url: str | None) -> str | None:
    if not url:
        return None
    host = url.split("://", 1)[-1].split("/", 1)[0].lower()
    return host.removeprefix("www.")


def _salesforce(body: dict, meta: dict) -> list[CanonicalEvent]:
    kind, sf_id, stamp = body["attributes"]["type"], body["Id"], body["SystemModstamp"]
    at, deleted = _ts(stamp), body["IsDeleted"]
    if kind == "Account":
        return [CanonicalEvent(
            "salesforce", "crm.account", f"sf_account:{sf_id}", f"sf:{sf_id}", at, stamp,
            status=body["Type"],
            attrs={"name": body["Name"], "website": body["Website"],
                   "domain": _website_domain(body["Website"]), "owner_sf_id": body["OwnerId"],
                   "csm_sf_id": body["CSM__c"], "segment": body["Segment__c"],
                   "is_test": bool(body["Is_Test__c"]), "is_deleted": deleted,
                   "master_record_id": body["MasterRecordId"],
                   "arr_minor": _minor(body["ARR__c"]),
                   "renewal_date": body["Renewal_Date__c"]})]
    if kind == "Contact":
        return [CanonicalEvent(
            "salesforce", "crm.contact", f"sf_contact:{sf_id}", f"sf:{body['AccountId']}",
            at, stamp,
            attrs={"email": (body["Email"] or "").lower() or None,
                   "first_name": body["FirstName"], "last_name": body["LastName"],
                   "title": body["Title"], "champion": bool(body["Champion__c"]),
                   "opted_out": bool(body["HasOptedOutOfEmail"]), "is_deleted": deleted})]
    if kind == "Opportunity":
        return [CanonicalEvent(
            "salesforce", "crm.opportunity", f"sf_opp:{sf_id}", f"sf:{body['AccountId']}",
            at, stamp, status=body["StageName"], amount_minor=_minor(body["Amount"]),
            attrs={"opp_id": sf_id, "type": body["Type"], "close_date": body["CloseDate"],
                   "is_closed": body["IsClosed"], "is_won": body["IsWon"],
                   "loss_reason": body["Loss_Reason__c"]})]
    if kind == "OpportunityHistory":
        created = body["CreatedDate"]
        return [CanonicalEvent(
            "salesforce", "crm.opportunity_history", f"sf_opp_hist:{sf_id}",
            f"sf_opp:{body['OpportunityId']}", _ts(created), created,
            status=body["StageName"], attrs={"opp_id": body["OpportunityId"]})]
    if kind == "User":
        return [CanonicalEvent(
            "salesforce", "crm.user", f"sf_user:{sf_id}", None, at, stamp,
            attrs={"email": body["Email"].lower(), "name": body["Name"],
                   "manager_sf_id": body["ManagerId"]})]
    raise ValueError(f"unsupported Salesforce object {kind}")


# --- Gmail --------------------------------------------------------------------------------

HARD_BOUNCE = {"5.1.1", "5.2.1"}  # the address does not exist / mailbox disabled


def _gmail(body: dict, meta: dict) -> list[CanonicalEvent]:
    payload = body["payload"]
    h = {x["name"].lower(): x["value"] for x in payload["headers"]}
    internal = "@" + meta["mailbox"].split("@")[1].lower()  # mailboxes are staff addresses
    sender = parseaddr(h["from"])[1].lower()
    to = [a.lower() for _, a in getaddresses([h.get("to", "")]) if a]
    cc = [a.lower() for _, a in getaddresses([h.get("cc", "")]) if a]
    report = next((p for p in payload.get("parts", [])
                   if p["mimeType"] == "message/delivery-status"), None)
    status = None
    if report:
        status = next((x["value"] for x in report["headers"] if x["name"] == "Status"), None)
    failed = (h.get("x-failed-recipients") or "").strip().lower() or None
    outbound = sender.endswith(internal)
    if report:
        counterparty = failed
    elif outbound:
        counterparty = next((a for a in to if not a.endswith(internal)), None)
    else:
        counterparty = sender
    auto = (h.get("auto-submitted") or "no").lower() != "no"
    return [CanonicalEvent(
        "gmail", "email.message", f"gmail:{h['message-id'].strip('<> ').lower()}",
        f"email:{counterparty}" if counterparty else None,
        _epoch(int(body["internalDate"]) / 1000),  # never the Date header (sender's clock)
        f"{int(body['internalDate']):013d}",
        attrs={"direction": "outbound" if outbound else "inbound", "mailbox": meta["mailbox"],
               "from": sender, "to": to, "cc": cc, "is_auto_reply": auto and not report,
               "is_dsn": report is not None, "dsn_status": status, "failed_recipient": failed,
               "thread_id": body["threadId"]})]


# --- Product ------------------------------------------------------------------------------

V1_TO_V2 = {"lead_score_avg": "signal_intent_high"}  # usage_summary.v2 renamed this field


def _product(body: dict, meta: dict) -> list[CanonicalEvent]:
    if "messageId" in body:
        ws, msg = body["context"]["groupId"], body["messageId"]
        return [CanonicalEvent(
            "product", "product.event", f"product_event:{msg}", f"ws:{ws}",
            _ts(body["timestamp"]), body["timestamp"],  # Segment's skew-corrected time
            attrs={"event": body["event"].lower().replace(" ", "_"),
                   "properties": body["properties"], "received_at": body["receivedAt"]})]
    ws, last_day = body["workspace_id"], body["period_end"]
    # period_end is the inclusive last day; the period ends at the next UTC midnight
    end = datetime.combine(date.fromisoformat(last_day) + timedelta(days=1), datetime.min.time(),
                           UTC)
    metrics = {V1_TO_V2.get(k, k): v for k, v in body["metrics"].items()}
    return [CanonicalEvent(
        "product", "product.usage", f"usage:{ws}:{last_day}", f"ws:{ws}", end,
        f"{body['version']:04d}",
        attrs={"schema": body["schema"], "period_start": body["period_start"],
               "period_end": last_day, "version": body["version"],
               "computed_at": body["computed_at"], "metrics": metrics,
               "sf_account_id_raw": body["sf_account_id"]})]
