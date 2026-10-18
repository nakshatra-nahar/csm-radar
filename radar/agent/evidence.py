"""Deterministic evidence packs (design §8): everything a brief may say, pre-rendered as
display strings the model copies (it never computes a number), each tagged with its trust."""

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from uuid import UUID

from radar import clock, policy, scoring

# Identity methods that prove which account an item belongs to (design §4.2); 'domain' is
# only a heuristic, so its items never go into a customer email.
EXACT_METHODS = {"sf_id", "stripe_metadata", "zendesk_external_id", "product_metadata",
                 "contact_email", "merge", "sf_opportunity"}
TEXT_CAP = 280
# a scheme or www., or a bare domain such as bit.ly/x; a letter TLD keeps 3.1 or 0.42 out
URL_RE = re.compile(r"(?:https?://|www\.)\S+|\b[\w-]+(?:\.[\w-]+)*\.[a-z]{2,}\b(?:/\S*)?",
                    re.I)
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
# ten or more digits with single separators: phone numbers, never an ISO date
PHONE_RE = re.compile(r"\+?\(?\d(?:[\s().-]?\d){9,}")
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


def _render(e: dict, currency: str) -> tuple[str, str]:
    """(display, trust) for one event; ticket text is the customer's and is never trusted."""
    a, day = e["attrs"], e["occurred_at"].date().isoformat()
    if e["type"] == "support.ticket":
        text = redact(f"{a.get('subject', '')}: {a.get('description_excerpt', '')}")
        head = f"Ticket {a.get('ticket_id')} ({e['status']}, {e['priority']}) on {day}: "
        return (head + text)[:TEXT_CAP], "untrusted_customer_text"
    if e["type"] == "billing.invoice":
        return (f"Invoice {e['status']} on {day}: {_money(e['amount_minor'], currency)} due,"
                f" payment attempt {a.get('attempt_count')}"), "system"
    if e["type"] == "billing.subscription":
        cancel = ", set to cancel at period end" if a.get("cancel_at_period_end") else ""
        return f"Subscription {e['status']} on {day}: {e['quantity']} seats{cancel}", "system"
    if e["type"] == "email.message" and a.get("is_dsn"):
        return f"An email to a contact bounced on {day} ({a.get('dsn_status')})", "system"
    return f"{e['type']} event ({e['status'] or 'no status'}) on {day}", "system"


def _score_items(name: str, score: dict, changes: dict, currency: str) -> list[tuple]:
    """(source, kind, display) for the account, its score, what changed and the top
    contributions."""
    term_end = ((score.get("context") or {}).get("term_end") or "unknown")[:10]
    out = [("crm", "account", f"{name}: ARR {_money(score.get('arr_minor'), currency)},"
            f" term ends {term_end}")]
    if not score:
        return out
    out.append(("radar", "score", f"Risk band {score['band']}; p {score['p']:.2f}; lift"
                f" {score['lift']:.1f}x; rank {_num(score['rank_in_book'])} in book"
                if score["p"] is not None else
                f"Risk band {score['band']}; rules score {_num(score['rules_score'])}"))
    out += [("radar", "change", f"{c['name']} moved from {_num(c['before'])} to"
             f" {_num(c['after'])} since the previous run")
            for c in changes["changed_features"] if c["before"] != c["after"]][:TOP]
    top = sorted((score["contributions"] or {}).items(), key=lambda kv: (-kv[1], kv[0]))
    out += [("radar", "contribution", f"{name} is {_num(score['features'].get(name))} and adds"
             f" {c:.2f} to the log-odds") for name, c in top if c > 0][:TOP]
    return out


def _ledger(conn, account_id) -> dict:
    def q(sql):
        return [{k: str(v) if v is not None else None for k, v in r.items()}
                for r in conn.execute(sql, (account_id,)).fetchall()]
    return {
        "open_cases": q("SELECT kind, status, opened_at::date AS opened, sla_due_at::date AS"
                        " sla_due FROM cases WHERE account_id = %s AND status IN ('open', 'held')"
                        " ORDER BY opened_at, kind"),
        "last_outcomes": q("SELECT kind, outcome, closed_at::date AS closed FROM cases"
                           " WHERE account_id = %s AND outcome IS NOT NULL"
                           " ORDER BY closed_at DESC LIMIT 3"),
        "suppressions": q("SELECT reason, created_at::date AS since FROM suppressions"
                          " WHERE account_id = %s AND lifted_at IS NULL ORDER BY created_at")}


def build_pack(conn, tenant_id: UUID, case_id: UUID) -> EvidencePack:
    """The case's evidence as of now: the latest actionable score, its change since the
    previous run, and the case's events visible now, each with how it was matched."""
    now = clock.now()
    case = conn.execute("SELECT * FROM cases WHERE id = %s", (case_id,)).fetchone()
    tenant = conn.execute("SELECT config FROM tenants WHERE id = %s", (tenant_id,)).fetchone()
    currency = tenant["config"]["currency"]
    account = conn.execute("SELECT name, segment FROM accounts WHERE id = %s",
                           (case["account_id"],)).fetchone()
    score = conn.execute("SELECT * FROM latest_scores WHERE account_id = %s",
                         (case["account_id"],)).fetchone() or {}
    changes = (scoring.diff_runs(conn, case["account_id"], score["run_id"]) if score
               else {"changed_features": []})
    events = conn.execute(
        "SELECT e.*, l.method FROM events e LEFT JOIN LATERAL (SELECT method FROM"
        " identity_links l WHERE l.account_ref = e.account_ref AND l.available_at <= %(now)s"
        " ORDER BY l.available_at DESC, l.id DESC LIMIT 1) l ON true"
        " WHERE e.id = ANY(%(ids)s) AND e.available_at <= %(now)s AND e.occurred_at <= %(now)s"
        " ORDER BY e.occurred_at, e.id", {"now": now, "ids": case["evidence_event_ids"]})
    items = [EvidenceItem(f"E{n}", src, kind, display, "system", None, False, None)
             for n, (src, kind, display)
             in enumerate(_score_items(account["name"], score, changes, currency), start=1)]
    for e in events:
        display, trust = _render(e, currency)
        items.append(EvidenceItem(f"E{len(items) + 1}", e["source"], e["type"].split(".")[1],
                                  display, trust, e["method"], e["method"] in EXACT_METHODS,
                                  e["id"]))
    return EvidencePack(
        account=account,
        score={k: score.get(k) for k in ("band", "p", "lift", "rank_in_book")},
        changes=[i.id for i in items if i.kind == "change"],
        play=case["play"] or policy.choose_play(case["kind"], score.get("contributions") or {}),
        items=items, ledger=_ledger(conn, case["account_id"]))
