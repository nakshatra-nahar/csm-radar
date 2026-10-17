"""World facts in plain Python types. Task 2b turns these into vendor payloads.

Timestamps are tz-aware UTC. Money is in minor units of the tenant currency. Nothing here
knows about delivery times or vendor formatting.
"""

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo


def add_months(day: date, months: int) -> date:
    """Start days are clamped to <= 28 at creation, so no end-of-month handling is needed."""
    y, m = divmod(day.month - 1 + months, 12)
    return day.replace(year=day.year + y, month=m + 1)


def local_dt(day: date, hour: float, tz: str) -> datetime:
    """`hour` o'clock tenant-local on `day`, as UTC."""
    naive = datetime.combine(day, time()) + timedelta(hours=hour)
    return naive.replace(tzinfo=ZoneInfo(tz)).astimezone(UTC)


@dataclass
class Account:
    sf_account_id: str
    name: str
    domain: str
    segment: str
    billing: str  # month_to_month | annual | monthly_on_contract
    plan: str
    seats: int  # at start; changes are billing seat_change events
    unit_amount: int  # per seat per billing interval, minor units
    start: date  # day of month <= 28
    csm_email: str
    csm_sf_id: str
    mu: float
    onboarding_failed: bool
    integration: str  # the customer's connected CRM / bank feed / code host
    stripe_customer_id: str
    stripe_subscription_id: str
    zd_org_id: int
    workspace_id: str | None  # None = no telemetry
    silent: bool  # unhappy-and-quiet trait: files fewer tickets
    vp_case: bool = False

    @property
    def interval(self) -> str:
        return "year" if self.billing == "annual" else "month"

    @property
    def contract(self) -> bool:
        return self.billing != "month_to_month"

    def next_term_end(self, day: date) -> date | None:
        """First 12-month term end strictly after `day`; None for month-to-month."""
        if not self.contract:
            return None
        years = max(1, day.year - self.start.year)
        while add_months(self.start, 12 * years) <= day:
            years += 1
        return add_months(self.start, 12 * years)


@dataclass
class Shock:
    account: int  # index into World.accounts
    kind: str
    week: int  # week index of onset
    weeks: int | None  # duration; None = until the account ends
    info: dict = field(default_factory=dict)


@dataclass
class PaymentFailure:
    sf_account_id: str
    invoice_at: datetime
    attempts: int
    recovered: bool


@dataclass
class Contact:
    sf_contact_id: str
    sf_account_id: str
    email: str
    first_name: str
    last_name: str
    title: str
    champion: bool
    created_at: datetime
    departed_at: datetime | None = None  # truth: the person left the customer
    sf_inactive_at: datetime | None = None  # when SF learns (weeks later, or never)


@dataclass
class CrmChange:
    """One changed Salesforce row; `fields` is the full row state after the change."""

    at: datetime
    object: str  # Account | Contact | Opportunity | OpportunityHistory | User
    sf_id: str
    fields: dict
    deleted: bool = False


@dataclass
class BillingEvent:
    """kind: customer_created, sub_created, seat_change, cancel_scheduled, cancel_reversed,
    period_renewed, sub_deleted, invoice_paid, invoice_failed. Subscription fields are the
    state after the event."""

    at: datetime
    kind: str
    sf_account_id: str
    customer_id: str
    subscription_id: str
    plan: str
    interval: str  # month | year
    unit_amount: int
    currency: str
    quantity: int
    status: str  # active | past_due | canceled
    current_period_end: datetime
    cancel_at_period_end: bool = False
    cancel_at: datetime | None = None
    cancellation_reason: str | None = None  # cancellation_requested | payment_failed
    invoice_id: str | None = None
    amount_due: int | None = None
    attempt_count: int | None = None
    next_payment_attempt: datetime | None = None


@dataclass
class TicketVersion:
    at: datetime
    status: str  # new | open | pending | hold | solved | closed
    priority: str  # low | normal | high | urgent
    type: str  # question | incident | problem | task
    tags: list[str]
    csat: str  # unoffered | offered | good | bad
    subject: str
    description: str


@dataclass
class Ticket:
    id: int
    sf_account_id: str
    requester_email: str
    created_at: datetime
    versions: list[TicketVersion] = field(default_factory=list)


@dataclass
class Email:
    sf_account_id: str
    sent_at: datetime
    direction: str  # inbound | outbound
    mailboxes: list[str]  # CSM mailbox, plus the manager's when cc'd
    from_addr: str
    to: list[str]
    cc: list[str]
    subject: str
    thread_key: str  # emails sharing a key form one thread
    is_auto_reply: bool = False
    dsn_status: str | None = None  # 5.1.1 | 4.4.7 | 5.7.1 for bounces
    failed_recipient: str | None = None


@dataclass
class UsageMonth:
    sf_account_id: str
    workspace_id: str
    period_start: date
    period_end: date  # exclusive: first day of the next month
    metrics: dict


@dataclass
class ProductEvent:
    sf_account_id: str
    workspace_id: str
    at: datetime
    event: str
    properties: dict


def clip_to_start(items: list, start: date, key, snapshot) -> list:
    """Drop facts dated before a source's start date (sorted by `at`).

    The last pre-start fact of each stateful entity (`key(item)` not None) is re-emitted at
    the start as the source's initial load: `snapshot(item, at)` returns it, or None to drop.
    """
    at = datetime.combine(start, time(6), UTC)
    last, out = {}, []
    for item in sorted(items, key=lambda x: x.at):
        if item.at >= at:
            out.append(item)
        elif (k := key(item)) is not None:
            last[k] = item
    return [s for s in (snapshot(i, at) for i in last.values()) if s is not None] + out
