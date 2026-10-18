"""The three demo tenants (design §3.1) and their runtime config."""

from dataclasses import dataclass
from datetime import date
from uuid import NAMESPACE_URL, UUID, uuid5


@dataclass(frozen=True)
class TenantSpec:
    slug: str
    name: str
    vertical: str
    n_accounts: int
    n_csms: int
    n_managers: int
    currency: str
    timezone: str
    internal_domain: str
    billing: dict[str, float]  # month_to_month / annual / monthly_on_contract shares, sum 1
    source_start: dict[str, date]  # salesforce / stripe / zendesk / product / gmail
    has_model: bool
    csm_field: str  # Salesforce field holding the CSM's user id


TENANTS: dict[str, TenantSpec] = {
    "hearthline": TenantSpec(
        slug="hearthline",
        name="Hearthline",
        vertical="real-estate team platform",
        n_accounts=2600,
        n_csms=16,
        n_managers=2,
        currency="USD",
        timezone="America/New_York",
        internal_domain="hearthline.io",
        billing={"month_to_month": 0.0, "annual": 0.6, "monthly_on_contract": 0.4},
        source_start={
            "salesforce": date(2023, 10, 16),
            "stripe": date(2023, 10, 16),
            "zendesk": date(2023, 10, 16),
            "product": date(2024, 4, 1),
            "gmail": date(2024, 10, 1),
        },
        has_model=True,
        csm_field="OwnerId",
    ),
    "tallyworks": TenantSpec(
        slug="tallyworks",
        name="Tallyworks",
        vertical="SMB accounting",
        n_accounts=2000,
        n_csms=12,
        n_managers=2,
        currency="GBP",
        timezone="Europe/London",
        internal_domain="tallyworks.co.uk",
        billing={"month_to_month": 0.6, "annual": 0.4, "monthly_on_contract": 0.0},
        source_start={
            "salesforce": date(2023, 10, 16),
            "stripe": date(2024, 1, 1),  # billing migration
            "zendesk": date(2024, 1, 1),
            "product": date(2024, 4, 1),
            "gmail": date(2024, 10, 1),
        },
        has_model=True,
        csm_field="CSM__c",
    ),
    "kitewire": TenantSpec(
        slug="kitewire",
        name="Kitewire",
        vertical="developer tooling",
        n_accounts=400,
        n_csms=3,
        n_managers=1,
        currency="USD",
        timezone="America/Los_Angeles",
        internal_domain="kitewire.dev",
        billing={"month_to_month": 0.7, "annual": 0.3, "monthly_on_contract": 0.0},
        source_start=dict.fromkeys(
            ("salesforce", "stripe", "zendesk", "product", "gmail"), date(2026, 2, 1)
        ),
        has_model=False,
        csm_field="OwnerId",
    ),
}


def tenant_id(slug: str) -> UUID:
    return uuid5(NAMESPACE_URL, "radar:" + slug)


def runtime_config(slug: str) -> dict:
    """Initial `tenants.config` (config_version 1); later edits live in the database."""
    spec = TENANTS[slug]
    return {
        "timezone": spec.timezone,
        "currency": spec.currency,
        "capacity_per_csm": 10,
        "sweep_hour_local": 6,
        "sla_business_days": {
            "cancel_announced": 1,
            "dunning_exhausted": 2,
            "champion_bounced": 5,
            "urgent_ticket_stalled": 2,
            "model_topk": 5,
        },
        "band_lift": {"red": 3.0, "yellow": 1.5},
        "band_rules": {"red": 3.0, "yellow": 1.5},  # rules_score points, tenants without a model
        "min_slot_lift": 1.0,  # Ruling AP: a model slot needs at least the tenant base rate
        "eligibility_window_days": 150,
        "sf": {"csm_field": spec.csm_field},
        "llm": {"enabled": True, "daily_token_budget": 200000},
        "breaker": {"min": 10, "multiple": 3.0},
        "model": {"min_decisions": 100},
        # Ruling AH: hours since the latest delivery before a source counts as stale (product
        # is the monthly batch rule in radar/agent/handlers.py). Production would set
        # per-source expectations from each source's observed volume instead.
        "freshness_hours": {"stripe": 24, "zendesk": 24, "gmail": 24, "salesforce": 26},
        "config_version": 1,
    }
