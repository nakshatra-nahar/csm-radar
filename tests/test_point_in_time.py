"""Point-in-time correctness on the ingested small world: what was not known at T never
changes features at T."""

from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from radar.asof import load_frames
from radar.bootstrap import replay_feed, seed_tenants_and_users
from radar.db import Database
from radar.features import compute_features
from radar.registry import FEATURE_NAMES
from radar.tenants import tenant_id
from tests.conftest import WORKER_PASSWORD, _role_dsn, fresh_database

TID = tenant_id("hearthline")
MONDAYS = [datetime(2024, 11, 4, tzinfo=UTC), datetime(2025, 6, 2, tzinfo=UTC),
           datetime(2026, 3, 2, tzinfo=UTC)]


class Rollback(Exception):
    pass


def tenant_row(conn):
    return conn.execute("SELECT * FROM tenants WHERE id = %s", (TID,)).fetchone()


def features_of(db, as_of, **kw):
    with db.tenant_tx(TID) as conn:
        frames, tenant = load_frames(conn, TID), tenant_row(conn)
    return frames, compute_features(frames, tenant, as_of, **kw)


def by_sf(frames, df):
    """Account uuids differ between databases; sf ids do not."""
    sf = frames.accounts.set_index("id")["sf_account_id"]
    out = df.reset_index()
    out["account_id"] = out["account_id"].map(sf)
    return out.set_index(["account_id", "as_of"]).sort_index()


def test_as_of_invariance(small_world_db):
    db, at = small_world_db["db"], MONDAYS[1]
    _, before = features_of(db, [at])
    late = at + timedelta(days=1)
    with pytest.raises(Rollback), db.tenant_tx(TID) as conn:
        # late arrivals about the past: every recent event again, materially changed
        conn.execute(
            "INSERT INTO events (raw_record_id, source, type, entity_id, account_ref,"
            " occurred_at, available_at, version_key, status, priority, amount_minor,"
            " quantity, attrs)"
            " SELECT raw_record_id, source, type, entity_id, account_ref, occurred_at, %s,"
            " version_key || ':late', 'open', 'urgent', amount_minor, quantity * 3,"
            " jsonb_set(attrs, '{metrics,active_users}', '0')"
            " FROM events WHERE occurred_at BETWEEN %s AND %s",
            (late, at - timedelta(days=120), at))
        conn.execute(  # and every account handle re-linked to one account
            "INSERT INTO identity_links (source, account_ref, account_id, method, available_at)"
            " SELECT source, account_ref, (SELECT min(id::text)::uuid FROM accounts), 'domain',"
            " %s FROM identity_links", (late,))
        frames = load_frames(conn, TID)
        after = compute_features(frames, tenant_row(conn), [at])
        assert len(frames.events) > 0
        raise Rollback
    pd.testing.assert_frame_equal(before, after)


def test_truncation_equivalence(small_world_db):
    """Features from the full database equal those from a database that only ever received
    the records delivered by T, for 30 sampled (account, T)."""
    full_frames, full = features_of(small_world_db["db"], MONDAYS)
    full = by_sf(full_frames, full)
    rng = np.random.default_rng(4)
    with fresh_database() as dsn:
        db = Database(_role_dsn(dsn, "radar_worker", WORKER_PASSWORD))
        try:
            seed_tenants_and_users(dsn, db, ["hearthline"])
            for at in MONDAYS:  # the feed cursor resumes, so each replay adds to the last
                replay_feed(db, TID, small_world_db["data_dir"], at)
                frames, cut = features_of(db, [at])
                cut = by_sf(frames, cut)
                sample = rng.choice(cut.index.get_level_values(0), 10, replace=False)
                keys = [(sf, pd.Timestamp(at)) for sf in sample]
                pd.testing.assert_frame_equal(cut.loc[keys, FEATURE_NAMES],
                                              full.loc[keys, FEATURE_NAMES])
        finally:
            db.close()


def test_hindsight_differs_somewhere(small_world_db):
    db = small_world_db["db"]
    _, pit = features_of(db, MONDAYS)
    _, hindsight = features_of(db, MONDAYS, hindsight=True)
    pit, hindsight = pit.align(hindsight, join="inner")
    a, b = pit[FEATURE_NAMES], hindsight[FEATURE_NAMES]
    differs = ~((a == b) | (a.isna() & b.isna()))
    assert differs.to_numpy().mean() >= 0.01
