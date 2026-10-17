"""Bootstrap a demo database: tenants, staff, and the feed replayed up to a point in time."""

from datetime import UTC, datetime
from pathlib import Path

import psycopg
from psycopg.types.json import Jsonb

from radar.db import Database
from radar.ingest import feed
from radar.ingest.pipeline import IngestStats
from radar.sim import staff
from radar.tenants import TENANTS, runtime_config, tenant_id

HISTORY_END = datetime(2026, 10, 17, tzinfo=UTC)  # the generator's default "now"


def seed_tenants_and_users(owner_dsn: str, db: Database, slugs: list[str], seed: int = 7) -> None:
    """Idempotent. Tenants go in as the owner: the app roles may not insert tenants."""
    with psycopg.connect(owner_dsn, autocommit=True) as conn:
        for slug in slugs:
            spec, config = TENANTS[slug], runtime_config(slug)
            conn.execute(
                "INSERT INTO tenants (id, slug, name, timezone, currency, internal_domain,"
                " config, config_version) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)"
                " ON CONFLICT (id) DO NOTHING",
                (tenant_id(slug), slug, spec.name, spec.timezone, spec.currency,
                 spec.internal_domain, Jsonb(config), config["config_version"]))
    for slug in slugs:
        with db.tenant_tx(tenant_id(slug)) as conn:
            for user in staff(slug, seed):  # managers come first, so their ids exist
                conn.execute(
                    "INSERT INTO users (email, name, role, sf_user_id, manager_id)"
                    " VALUES (%s, %s, %s, %s, (SELECT id FROM users WHERE email = %s))"
                    " ON CONFLICT (tenant_id, email) DO NOTHING",
                    (user["email"], user["name"], user["role"], user["sf_user_id"],
                     user["manager_email"]))


def replay_feed(db: Database, tenant_id, data_dir: Path, until: datetime) -> IngestStats:
    """Deliver every feed record with deliver_at <= until (db should be radar_worker)."""
    return feed.deliver_due(db, tenant_id, until, data_dir)
