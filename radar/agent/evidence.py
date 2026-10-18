"""Deterministic evidence packs (design §8): everything a brief may say, pre-rendered as
display strings the model copies (it never computes a number), each tagged with its trust."""

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta, tzinfo
from uuid import UUID
from zoneinfo import ZoneInfo

from radar import asof, clock, features, policy, registry, scoring

# Identity methods that prove which account an item belongs to (design §4.2); 'domain' is
# only a heuristic, so its items never go into a customer email.
EXACT_METHODS = {"sf_id", "stripe_metadata", "zendesk_external_id", "product_metadata",
                 "contact_email", "merge", "sf_opportunity"}
TEXT_CAP = 280
# a scheme or www., or a bare domain such as bit.ly/x; a letter TLD keeps 3.1 or 0.42 out
URL_RE = re.compile(r"(?:https?://|www\.)\S+|\b[\w-]+(?:\.[\w-]+)*\.[a-z]{2,}\b(?:/\S*)?",
                    re.I)
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
# ten or more digits with single separators, or a 7-9 digit local number (555-1234); never an
# ISO date, a decimal or a year range such as 2026-2027
PHONE_RE = re.compile(r"\+?\(?\d(?:[\s().-]?\d){9,}"
                      r"|(?<![\d.,-])(?!(?:19|20)\d\d-(?:19|20)\d\d\b)\d{3,5}[ -]\d{4}(?![\d.,-])")
TOP = 3  # changes and contributions shown


@dataclass(frozen=True)
class EvidenceItem:
    id: str  # E1..En
    source: str
    kind: str
    display: str
    trust: str  # system | untrusted_customer_text
    match_method: str | None
    customer_safe: bool
    event_id: int | None


@dataclass(frozen=True)
class EvidencePack:
    account: dict
    score: dict
    changes: list[str]  # ids of the kind='change' items: what changed since the last run
    play: str
    items: list[EvidenceItem]
    ledger: dict

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True, default=str)

    def hash(self) -> str:
        return hashlib.sha256(self.to_json().encode()).hexdigest()


def redact(text: str) -> str:
    for pattern in (EMAIL_RE, URL_RE, PHONE_RE):  # emails first: a URL would eat the domain
        text = pattern.sub("[redacted]", text)
    return text


def _num(v) -> str:
    return "missing" if v is None else f"{v:.2f}" if isinstance(v, float) else str(v)


def _money(minor, currency: str) -> str:
    return "unknown" if minor is None else f"{currency} {minor / 100:,.2f}"


def _ticket(e: dict) -> str:
    return (f"Ticket {e['attrs'].get('ticket_id')} ({e['status'] or 'unknown'},"
            f" {e['priority'] or 'unknown'})")


def _label(e: dict, currency: str) -> tuple[str, str]:
    """(label, detail) of a system event in plain words; detail may be empty."""
    a, t, status = e["attrs"], e["type"], e["status"]
    if t == "billing.invoice":
        attempt = a.get("attempt_count")
        retry = f", payment attempt {attempt}" if status != "paid" and attempt else ""
        return f"Invoice {status or 'updated'}", f"{_money(e['amount_minor'], currency)}{retry}"
    if t == "billing.subscription":
        if a.get("cancel_at_period_end"):
            return "Subscription set to cancel at period end", ""
        q = e["quantity"]
        return f"Subscription {status or 'updated'}", f"{q} seat{'' if q == 1 else 's'}"
    if t == "email.message":
        if a.get("is_dsn"):
            return "An email to a contact bounced", a.get("dsn_status") or ""
        to = "to" if a.get("direction") == "outbound" else "from"
        return f"{'Auto-reply' if a.get('is_auto_reply') else 'Email'} {to} customer", ""
    if t == "crm.opportunity_history":
        # shortcut: history rows carry no opportunity type and the CRM sync only sends renewal
        # opportunities (sim/crm.py); join to crm.opportunity's type if new business is synced
        return "Renewal opportunity", status or ""
    if t == "crm.opportunity":
        return f"{a.get('type') or 'CRM'} opportunity", status or ""
    if t == "product.event":
        return "Product event", (a.get("event") or "").replace("_", " ")
    plain = {"crm.account": "CRM account updated", "crm.contact": "CRM contact updated",
             "crm.user": "CRM user updated", "product.usage": "Monthly usage reported",
             "billing.customer": "Billing customer updated",
             "support.organization": "Support organization updated"}
    return plain.get(t, t.replace(".", " ").replace("_", " ")), status or ""


def describe(e: dict, currency: str, cap: int | None = TEXT_CAP,
             dated: bool = True, tz: tzinfo = UTC) -> tuple[str, str]:
    """(display, trust) for one event; ticket text is the customer's and is never trusted.
    cap=None keeps the whole (redacted) ticket text; dated=False leaves the date out, for the
    timeline, which shows it in its own column. The date is the tenant's local day (tz)."""
    a = e["attrs"]
    on = f" on {_day(e['occurred_at'], tz)}" if dated else ""
    if e["type"] == "support.ticket":
        text = redact(f"{a.get('subject', '')}: {a.get('description_excerpt', '')}")
        return f"{_ticket(e)}{on}: {text}"[:cap], "untrusted_customer_text"
    label, detail = _label(e, currency)
    return f"{label}{on}{': ' + detail if detail else ''}", "system"


def _score_items(name: str, score: dict, changes: dict, currency: str) -> list[tuple]:
    """(source, kind, display) for the account, its score, what changed and the top
    behavioural contributions."""
    term_end = ((score.get("context") or {}).get("term_end") or "unknown")[:10]
    out = [("crm", "account", f"{name}: ARR {_money(score.get('arr_minor'), currency)},"
            f" term ends {term_end}")]
    if not score:
        return out
    out.append(("radar", "score", f"Risk band {score['band']}; p {score['p']:.2f}; lift"
                f" {score['lift']:.1f}x; rank {_num(score['rank_in_book'])} in book"
                if score["p"] is not None else
                f"Risk band {score['band']}; rules score {_num(score['rules_score'])}"))
    out += [("radar", "change", f"{c['label']} moved from {c['shown_before']} to"
             f" {c['shown_after']} since the previous run")
            for c in scoring.material(changes["changed_features"], currency)][:TOP]
    # behavioural terms only: structural ones stay collapsed, as in the Why panel (Ruling BF)
    behavioural = {d.name for d in registry.FEATURES if d.kind == "behavioural"}
    top = sorted(((n, c) for n, c in (score["contributions"] or {}).items() if n in behavioural),
                 key=lambda kv: (-kv[1], kv[0]))
    out += [("radar", "contribution", f"{registry.LABELS[name]} is"
             f" {registry.shown(name, score['features'].get(name), currency)} and adds"
             f" {c:.2f} to the log-odds") for name, c in top if c >= scoring.NEGLIGIBLE][:TOP]
    return out


def _ledger(conn, account_id, tz: str) -> dict:
    """Open cases, the last outcomes and live suppressions, dated by the tenant's local day."""
    def q(sql):
        return [{k: str(v) if v is not None else None for k, v in r.items()}
                for r in conn.execute(sql, {"a": account_id, "tz": tz}).fetchall()]
    return {
        "open_cases": q("SELECT kind, status, (opened_at AT TIME ZONE %(tz)s)::date AS opened,"
                        " (sla_due_at AT TIME ZONE %(tz)s)::date AS sla_due FROM cases"
                        " WHERE account_id = %(a)s AND status IN ('open', 'held')"
                        " ORDER BY opened_at, kind"),
        "last_outcomes": q("SELECT kind, outcome, (closed_at AT TIME ZONE %(tz)s)::date AS"
                           " closed FROM cases WHERE account_id = %(a)s AND outcome IS NOT NULL"
                           " ORDER BY closed_at DESC LIMIT 3"),
        "suppressions": q("SELECT reason, (created_at AT TIME ZONE %(tz)s)::date AS since"
                          " FROM suppressions WHERE account_id = %(a)s AND lifted_at IS NULL"
                          " ORDER BY created_at")}


# --- the events behind a model score (Ruling AC) -------------------------------------------
# Selectors only pick which canonical events to show for a feature radar/registry.py defines;
# each gets the account's events visible at the score run's as_of, in (entity, time) order,
# and returns (event, display) pairs, most relevant first.
SHOWN_TYPES = ["product.usage", "product.event", "support.ticket", "email.message",
               "billing.subscription"]
PER_FEATURE, FEATURE_ITEMS = 3, 10
DAY = timedelta(days=1)


def _day(at, tz: tzinfo = UTC) -> str:
    """The local calendar day of a timestamp (sessions read timestamptz in UTC)."""
    return at.astimezone(tz).date().isoformat()


def _of(rows: list[dict], type_: str) -> list[dict]:
    return [r for r in rows if r["type"] == type_]


def _current(rows: list[dict]) -> list[dict]:
    """Each entity's latest version (rows are in entity, time, version order)."""
    return list({r["entity_id"]: r for r in rows}.values())


def _month_no(at) -> int:  # a usage row's occurred_at is the midnight after its month
    last = at - DAY
    return last.year * 12 + last.month - 1


def _month_name(n: int, year: bool = False) -> str:
    return date(n // 12, n % 12 + 1, 1).strftime("%b %Y" if year else "%b")


def _usage(rows, ctx):
    """The last 4 complete months under the run's product watermark (usage_drop v2)."""
    if ctx["product_mark"] is None:
        return []
    last = _month_no(ctx["product_mark"])
    months: dict[int, list[dict]] = {}
    for r in _current(_of(rows, "product.usage")):
        if last - 4 < _month_no(r["occurred_at"]) <= last and r["active_users"] is not None:
            months.setdefault(_month_no(r["occurred_at"]), []).append(r)
    if sorted(months) != list(range(last - 3, last + 1)):
        return []
    users = {m: sum(r["active_users"] for r in rs) for m, rs in months.items()}
    before = sum(users[m] for m in range(last - 3, last)) / 3
    shown = max(months[last], key=lambda r: (r["active_users"], -r["id"]))
    return [(shown, f"Active users: {users[last]} in {_month_name(last, True)} vs"
                    f" {before:.0f} a month in {_month_name(last - 3)}-{_month_name(last - 1)}")]


def _open_urgent(rows, ctx):
    t = [r for r in _current(_of(rows, "support.ticket"))
         if r["status"] in features.OPEN and r["priority"] in features.URGENT]
    return [(r, f"{_ticket(r)} opened on"
                f" {_day(r['ticket_created_at'] or r['occurred_at'], ctx['tz'])}")
            for r in sorted(t, key=lambda r: (r["occurred_at"], r["id"]), reverse=True)]


def _friction(rows, ctx):
    """Tickets rated bad or reopened after solved in the last 90 days (latest hit each)."""
    solved, hits = set(), {}
    for r in _of(rows, "support.ticket"):
        reopened = r["status"] in ("new", "open") and r["entity_id"] in solved
        if r["status"] == "solved":
            solved.add(r["entity_id"])
        if (reopened or r["csat"] == "bad") and r["occurred_at"] > ctx["as_of"] - 90 * DAY:
            what = "reopened" if reopened else "rated bad"
            hits[r["entity_id"]] = (r, f"Ticket {r['attrs'].get('ticket_id')} {what} on"
                                       f" {_day(r['occurred_at'], ctx['tz'])}")
    return sorted(hits.values(), key=lambda h: (h[0]["occurred_at"], h[0]["id"]), reverse=True)


def _last_email(rows, ctx):
    """Its date and the sender's role, never its subject or body."""
    inbound = [r for r in _of(rows, "email.message") if r["direction"] == "inbound"
               and not r["is_auto_reply"] and not r["is_dsn"]]
    if not inbound:
        return []
    r = max(inbound, key=lambda r: (r["occurred_at"], r["id"]))
    title = ctx["titles"].get((r["attrs"].get("from") or "").lower())
    role = f"their {redact(title)[:60]}" if title else "a contact"
    return [(r, f"Last customer email on {_day(r['occurred_at'], ctx['tz'])}, from {role}")]


def _integration(rows, ctx):
    e = sorted((r for r in _of(rows, "product.event")
                if r["product_event"] in [*features.BROKEN, features.FIXED]),
               key=lambda r: (r["occurred_at"], r["id"]))
    if not e or e[-1]["product_event"] not in features.BROKEN:
        return []
    what = e[-1]["product_event"].removeprefix("integration_").replace("_", " ")
    return [(e[-1], f"Integration reported {what} on {_day(e[-1]['occurred_at'], ctx['tz'])}")]


def _seat_change(rows, ctx):
    changes, before = [], {}
    for r in _of(rows, "billing.subscription"):
        prev = before.get(r["entity_id"])
        if prev is not None and r["quantity"] != prev and (
                r["occurred_at"] > ctx["as_of"] - 180 * DAY):
            changes.append((r, f"Subscription seats changed from {prev} to {r['quantity']}"
                               f" on {_day(r['occurred_at'], ctx['tz'])}"))
        before[r["entity_id"]] = r["quantity"]
    return sorted(changes, key=lambda c: (c[0]["occurred_at"], c[0]["id"]), reverse=True)


def _contract_start(rows, ctx):
    subs = [r for r in _of(rows, "billing.subscription") if r["start_date"] is not None]
    if not subs:
        return []
    r = min(subs, key=lambda r: (r["start_date"], r["id"]))
    # start_date is a calendar date stored as UTC midnight: its UTC day is the date
    return [(r, f"Contract started on {_day(r['start_date'])}")]


SELECTORS = {"usage_drop": _usage, "seat_utilization": _usage,
             "open_urgent_tickets": _open_urgent, "support_friction_90d": _friction,
             "days_since_customer_email": _last_email, "integration_broken": _integration,
             "seat_change_180d": _seat_change, "not_live_after_30d": _contract_start}


# The account's refs as of the run (any link to it, then the ref's latest link must still be
# to it), then their events: never the whole tenant's (Ruling BB). Both link lookups use
# identity_links_ref_idx; `n` is the link usable now (Ruling AF).
FEATURE_EVENTS = (
    "WITH refs AS (SELECT DISTINCT account_ref FROM identity_links"
    " WHERE account_id = %(a)s AND available_at <= %(t)s)"
    " SELECT e.*, n.method, n.account_id AS link_account," + asof.ATTRS +
    " FROM refs r JOIN LATERAL (SELECT account_id FROM identity_links l"
    " WHERE l.account_ref = r.account_ref AND l.available_at <= %(t)s"
    " ORDER BY l.available_at DESC, l.id DESC LIMIT 1) l ON l.account_id = %(a)s"
    " JOIN events e ON e.account_ref = r.account_ref"
    " LEFT JOIN LATERAL (SELECT method, account_id FROM identity_links l"
    " WHERE l.account_ref = e.account_ref AND l.available_at <= %(now)s"
    " ORDER BY l.available_at DESC, l.id DESC LIMIT 1) n ON true"
    " WHERE e.type = ANY(%(types)s) AND e.available_at <= %(t)s AND e.occurred_at <= %(t)s"
    " ORDER BY e.entity_id, e.occurred_at, e.version_key, e.id")


def _feature_events(conn, account_id, score: dict, now, tz: tzinfo) -> list[tuple[dict, str]]:
    """The events behind the score's top behavioural contributions, as they stood at its run's
    as_of: at most PER_FEATURE per feature and FEATURE_ITEMS in all, by contribution. Each
    row's method and link_account are the link usable now (Ruling AF), as for case events."""
    as_of = score["as_of"]
    # shortcut: links resolve to this account only, not through merges; add asof.resolver's
    # merge chain if merged accounts need their losers' events shown.
    rows = conn.execute(FEATURE_EVENTS, {"t": as_of, "now": now, "a": account_id,
                                         "types": SHOWN_TYPES}).fetchall()
    mark = conn.execute("SELECT watermarks->>'product' AS m FROM score_runs WHERE id = %s",
                        (score["run_id"],)).fetchone()["m"]
    ctx = {"as_of": as_of, "tz": tz, "product_mark": mark and datetime.fromisoformat(mark),
           "titles": {r["email"].lower(): r["title"] for r in conn.execute(
               "SELECT email, title FROM contacts WHERE account_id = %s AND email IS NOT NULL"
               " AND title IS NOT NULL", (account_id,))}}
    behavioural = {d.name for d in registry.FEATURES if d.kind == "behavioural"}
    top = sorted(((n, c) for n, c in (score["contributions"] or {}).items()
                  if n in behavioural and c > 0), key=lambda kv: (-kv[1], kv[0]))
    out, seen = [], set()
    for name, _ in top:
        picked = [(e, d) for e, d in SELECTORS[name](rows, ctx) if e["id"] not in seen]
        for e, d in picked[:PER_FEATURE]:
            seen.add(e["id"])
            out.append((e, d))
    return out[:FEATURE_ITEMS]


def build_pack(conn, tenant_id: UUID, case_id: UUID) -> EvidencePack:
    """The case's evidence as of now: the latest actionable score, its change since the
    previous run, for a model_topk case the events behind its top contributions (as of the
    run), and the case's events visible now, each with how it was matched."""
    now = clock.now()
    case = conn.execute("SELECT * FROM cases WHERE id = %s", (case_id,)).fetchone()
    tenant = conn.execute("SELECT config FROM tenants WHERE id = %s", (tenant_id,)).fetchone()
    currency, tz = tenant["config"]["currency"], ZoneInfo(tenant["config"]["timezone"])
    account = conn.execute("SELECT name, segment FROM accounts WHERE id = %s",
                           (case["account_id"],)).fetchone()
    score = conn.execute("SELECT * FROM latest_scores WHERE account_id = %s",
                         (case["account_id"],)).fetchone() or {}
    changes = (scoring.diff_runs(conn, case["account_id"], score["run_id"]) if score
               else {"changed_features": []})
    events = conn.execute(
        "SELECT e.*, l.method, l.account_id AS link_account FROM events e LEFT JOIN LATERAL"
        " (SELECT method, account_id FROM identity_links l WHERE l.account_ref = e.account_ref"
        " AND l.available_at <= %(now)s ORDER BY l.available_at DESC, l.id DESC LIMIT 1) l"
        " ON true WHERE e.id = ANY(%(ids)s) AND e.available_at <= %(now)s"
        " AND e.occurred_at <= %(now)s ORDER BY e.occurred_at, e.id",
        {"now": now, "ids": case["evidence_event_ids"]}).fetchall()
    items = [EvidenceItem(f"E{n}", src, kind, display, "system", None, False, None)
             for n, (src, kind, display)
             in enumerate(_score_items(account["name"], score, changes, currency), start=1)]
    shown = [(e, None) for e in events]
    if case["kind"] == "model_topk" and score:
        shown = _feature_events(conn, case["account_id"], score, now, tz) + shown
    for e, display in shown:
        display, trust = (display, "system") if display else describe(e, currency, tz=tz)
        # safe only if an exact key links the item to this case's own account right now
        safe = e["method"] in EXACT_METHODS and e["link_account"] == case["account_id"]
        items.append(EvidenceItem(f"E{len(items) + 1}", e["source"], e["type"].split(".")[1],
                                  display, trust, e["method"], safe, e["id"]))
    return EvidencePack(
        account=account,
        score={k: score.get(k) for k in ("band", "p", "lift", "rank_in_book")},
        changes=[i.id for i in items if i.kind == "change"],
        play=case["play"] or policy.choose_play(case["kind"], score.get("contributions") or {}),
        items=items, ledger=_ledger(conn, case["account_id"], tenant["config"]["timezone"]))
