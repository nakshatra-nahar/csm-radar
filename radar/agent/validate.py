"""The brief schema and its deterministic validator (design §8). The model gets no recipient
or play field: code chooses both."""

import re
from decimal import Decimal, InvalidOperation
from typing import Annotated

import anthropic
from pydantic import AfterValidator, BaseModel, Field, ValidationError

from radar.agent.evidence import EMAIL_RE, PHONE_RE, URL_RE, EvidencePack

# Evidence ids are the schema's one enum; a model that writes "e2" means E2.
EvidenceId = Annotated[str, AfterValidator(lambda s: s.strip().upper())]


class Point(BaseModel):
    evidence_ids: list[EvidenceId] = Field(min_length=1)
    text: str


class Email(BaseModel):
    subject: str
    body: str
    evidence_ids: list[EvidenceId]


class Brief(BaseModel):
    summary: list[Point] = Field(min_length=1)
    email: Email | None
    insufficient_evidence: bool
    injection_suspected: bool


SCHEMA = anthropic.transform_schema(Brief)
CAPS = {"summary_points": 5, "point_text": 300, "subject": 120, "body": 1200}
BANNED = re.compile(r"\b(churn|risk|health score|at-risk|discount|refund|free month|model"
                    r"|flagged|score)", re.I)
UNSAFE = {"URL": URL_RE, "email address": EMAIL_RE, "phone number": PHONE_RE,
          "HTML tag": re.compile(r"<[^>]*>"), "[E#] marker": re.compile(r"\[E\d+\]", re.I)}
# One number pattern for both sides: model text and the display strings it cites.
NUMBER_RE = re.compile(r"(?<!\w)\d+(?:[.,]\d+)*")
RULES = ["stop_reason", "parses", "ids_exist", "email_customer_safe", "numbers_cited",
         "banned_phrase", "email_clean", "lengths"]


def _value(token: str) -> Decimal | str:
    try:
        return Decimal(token.replace(",", ""))
    except InvalidOperation:  # 1.2.3: compare as written
        return token


def numbers(text: str) -> set[Decimal | str]:
    """Numbers by value, so 1,200.00 matches 1200 and 09 matches 9; E2 and Q3 are not numbers."""
    return {_value(m) for m in NUMBER_RE.findall(text)}


def rule_results(errors: list[str]) -> dict[str, bool | None]:
    """Per rule: True passed, False failed, None not reached (a gate before it failed)."""
    failed = {e.split(":")[0] for e in errors}
    gate = next((r for r in RULES[:2] if r in failed), None)
    after_gate = RULES[RULES.index(gate) + 1:] if gate else []
    return {r: None if r in after_gate else r not in failed for r in RULES}


def check(text: str | None, stop_reason: str, pack: EvidencePack) -> tuple[dict | None, list[str]]:
    """(the parsed brief, or None if it does not parse; errors as 'rule: detail', [] if valid).
    Rules run in design §8 order; a bad stop_reason or unparseable JSON stops the rest."""
    if stop_reason != "end_turn":
        return None, [f"stop_reason: {stop_reason}, expected end_turn"]
    try:
        brief = Brief.model_validate_json(text or "")
    except ValidationError as e:
        return None, [f"parses: {e.errors()[0]['msg']} at {e.errors()[0]['loc']}"]
    items = {i.id: i for i in pack.items}
    email = brief.email
    cited = [(p.text, p.evidence_ids) for p in brief.summary]
    if email:
        cited.append((f"{email.subject}\n{email.body}", email.evidence_ids))
    errors = []
    unknown = sorted({i for _, ids in cited for i in ids} - items.keys())
    if unknown:
        errors.append(f"ids_exist: {unknown} are not in the evidence pack")
    if email and (unsafe := [i for i in email.evidence_ids
                             if i in items and not items[i].customer_safe]):
        errors.append(f"email_customer_safe: the email may not cite {unsafe}")
    for said, ids in cited:
        known = set().union(*(numbers(items[i].display) for i in ids if i in items))
        if missing := sorted(map(str, numbers(said) - known)):
            errors.append(f"numbers_cited: {missing} do not appear in the cited items {ids}")
    if email:
        words = f"{email.subject}\n{email.body}"
        if hits := sorted({m.lower() for m in BANNED.findall(words)}):
            errors.append(f"banned_phrase: the email says {hits}")
        if found := [name for name, pattern in UNSAFE.items() if pattern.search(words)]:
            errors.append(f"email_clean: the email contains {found}")
    sizes = {"summary_points": len(brief.summary),
             "point_text": max(len(p.text) for p in brief.summary),
             "subject": len(email.subject) if email else 0,
             "body": len(email.body) if email else 0}
    if over := [f"{k} {v} > {CAPS[k]}" for k, v in sizes.items() if v > CAPS[k]]:
        errors.append(f"lengths: {over}")
    return brief.model_dump(), errors
