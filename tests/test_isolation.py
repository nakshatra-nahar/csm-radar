import uuid

import psycopg
import pytest

from radar.config import load_settings
from radar.db import Database, UnsafeRole, assert_role_safety, owner_connect
from radar.migrate import migrate
from radar.web.app import create_app
from tests.conftest import WEB_PASSWORD, WORKER_PASSWORD, _role_dsn

TENANT_TABLES_SQL = """
SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity,
       (SELECT count(*) FROM pg_policy p WHERE p.polrelid = c.oid) AS n_policies
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
JOIN pg_attribute a ON a.attrelid = c.oid AND a.attname = 'tenant_id' AND NOT a.attisdropped
WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p')
"""

CONTRACT_TABLES = {
    "tenants", "users", "raw_records", "events", "identity_links", "quarantine", "accounts",
    "contacts", "source_status", "models", "score_runs", "account_scores", "jobs",
    "schedule_runs", "cases", "suppressions", "worklists", "worklist_items", "briefs",
    "llm_calls", "outbox", "incidents", "audit_log",
}
APPEND_ONLY = [
    "raw_records", "events", "identity_links", "score_runs", "account_scores", "llm_calls",
    "audit_log",
]


def test_schema_has_every_contract_table(owner_conn):
    rows = owner_conn.execute(
        "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
    ).fetchall()
    assert CONTRACT_TABLES <= {r["tablename"] for r in rows}


def test_every_tenant_table_has_forced_rls_and_policy(owner_conn):
    rows = owner_conn.execute(TENANT_TABLES_SQL).fetchall()
    assert rows, "no tenant tables found"
    for r in rows:
        assert r["relrowsecurity"] and r["relforcerowsecurity"], r["relname"]
        assert r["n_policies"] >= 1, r["relname"]


def test_views_are_security_invoker_and_no_matviews(owner_conn):
    views = owner_conn.execute(
        "SELECT c.relname, c.relkind, c.reloptions FROM pg_class c"
        " JOIN pg_namespace n ON n.oid = c.relnamespace"
        " WHERE n.nspname = 'public' AND c.relkind IN ('v', 'm')"
    ).fetchall()
    assert "latest_scores" in {v["relname"] for v in views}
    for v in views:
        assert v["relkind"] == "v", f"materialized view {v['relname']}"
        assert "security_invoker=true" in (v["reloptions"] or []), v["relname"]
    definers = owner_conn.execute(
        "SELECT p.proname FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace"
        " WHERE n.nspname = 'public' AND p.prosecdef"
    ).fetchall()
    assert definers == []


def test_app_roles_are_not_superuser_bypassrls_or_owner(web_db, worker_db, owner_conn):
    for db in (web_db, worker_db):
        with db.system_tx() as conn:
            assert_role_safety(conn)
    with pytest.raises(RuntimeError):
        assert_role_safety(owner_conn)  # the test owner is a superuser


@pytest.mark.parametrize("attrs, owns", [("BYPASSRLS", False), ("", True)])
def test_web_and_worker_refuse_a_neon_style_owner(owner_dsn, owner_conn, attrs, owns):
    """Neon's owner is no superuser: it bypasses RLS and owns the tables (after radar push).
    Either one alone must stop the app from starting (Ruling AU)."""
    role = f"owner_{uuid.uuid4().hex[:8]}"
    owner_conn.execute(f"CREATE ROLE {role} LOGIN PASSWORD 'pw' NOSUPERUSER {attrs}")
    if owns:
        owner_conn.execute(f"CREATE TABLE {role}_t (id int); ALTER TABLE {role}_t OWNER TO {role}")
    db = Database(_role_dsn(owner_dsn, role, "pw"))
    try:
        with pytest.raises(UnsafeRole):
            create_app(load_settings(), db)  # as the web pool
        web = Database(_role_dsn(owner_dsn, "radar_web", WEB_PASSWORD))
        try:
            with pytest.raises(UnsafeRole):
                create_app(load_settings(), web, db)  # as the worker pool
        finally:
            web.close()
    finally:
        db.close()
        owner_conn.execute(f"DROP TABLE IF EXISTS {role}_t; DROP ROLE {role}")


def test_migrate_is_idempotent(owner_dsn):
    assert migrate(owner_dsn, WEB_PASSWORD, WORKER_PASSWORD) == []


def test_no_tenant_set_reads_zero_rows(web_db, two_tenants_seeded):
    with web_db.system_tx() as conn:
        assert conn.execute("SELECT count(*) AS n FROM accounts").fetchone()["n"] == 0


def test_tenant_a_cannot_read_tenant_b(web_db, two_tenants_seeded):
    t = two_tenants_seeded
    with web_db.tenant_tx(t["a"]) as conn:
        rows = conn.execute("SELECT tenant_id, id FROM accounts").fetchall()
        assert [(r["tenant_id"], r["id"]) for r in rows] == [(t["a"], t["a_account"])]
        b = conn.execute("SELECT 1 FROM accounts WHERE id = %s", (t["b_account"],)).fetchall()
        assert b == []


def test_cross_tenant_insert_rejected(web_db, two_tenants_seeded):
    t = two_tenants_seeded
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with web_db.tenant_tx(t["a"]) as conn:
            conn.execute(
                "INSERT INTO accounts (tenant_id, sf_account_id, name) VALUES (%s, 'x', 'x')",
                (t["b"],),
            )


def test_cross_tenant_fk_rejected(web_db, two_tenants_seeded):
    t = two_tenants_seeded
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        with web_db.tenant_tx(t["a"]) as conn:
            conn.execute(
                "INSERT INTO cases (account_id, kind, status, opened_by)"
                " VALUES (%s, 'cancel_announced', 'open', 'agent')",
                (t["b_account"],),
            )


def test_pooled_connection_reuse_does_not_leak(web_db_size1, two_tenants_seeded):
    with web_db_size1.tenant_tx(two_tenants_seeded["a"]) as conn:
        assert conn.execute("SELECT count(*) AS n FROM accounts").fetchone()["n"] == 1
    with web_db_size1.system_tx() as conn:  # same physical connection, setting now ''
        assert conn.execute("SELECT count(*) AS n FROM accounts").fetchone()["n"] == 0


@pytest.mark.parametrize("table", APPEND_ONLY)
def test_append_only_tables_reject_update(web_db, two_tenants_seeded, table):
    for stmt in (f"UPDATE {table} SET tenant_id = tenant_id", f"DELETE FROM {table}"):
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with web_db.tenant_tx(two_tenants_seeded["a"]) as conn:
                conn.execute(stmt)


def test_worker_sees_all_jobs_web_sees_own(web_db, worker_db, two_tenants_seeded):
    t = two_tenants_seeded
    sql = "SELECT DISTINCT tenant_id FROM jobs"
    with web_db.tenant_tx(t["a"]) as conn:
        assert {r["tenant_id"] for r in conn.execute(sql)} == {t["a"]}
    with web_db.system_tx() as conn:
        assert conn.execute(sql).fetchall() == []
    with worker_db.system_tx() as conn:
        assert {r["tenant_id"] for r in conn.execute(sql)} >= {t["a"], t["b"]}


def test_audit_trigger_records_actor(web_db, two_tenants_seeded):
    t = two_tenants_seeded
    with web_db.tenant_tx(t["a"], actor="user:test") as conn:
        case_id = conn.execute(
            "INSERT INTO cases (account_id, kind, status, opened_by)"
            " VALUES (%s, 'audit_probe', 'done', 'agent') RETURNING id",
            (t["a_account"],),
        ).fetchone()["id"]
        conn.execute("UPDATE cases SET outcome = 'saved' WHERE id = %s", (case_id,))
        rows = conn.execute(
            "SELECT actor, action, entity_type, before, after FROM audit_log"
            " WHERE entity_id = %s ORDER BY id",
            (str(case_id),),
        ).fetchall()
    assert [(r["actor"], r["action"], r["entity_type"]) for r in rows] == [
        ("user:test", "insert", "cases"),
        ("user:test", "update", "cases"),
    ]
    assert rows[1]["before"]["outcome"] is None and rows[1]["after"]["outcome"] == "saved"


def test_score_run_lifecycle_columns_updatable_rest_frozen(web_db, two_tenants_seeded):
    with web_db.tenant_tx(two_tenants_seeded["a"]) as conn:
        run_id = conn.execute(
            "INSERT INTO score_runs (trigger, actionable, as_of, config_version)"
            " VALUES ('manual', false, now(), 1) RETURNING id"
        ).fetchone()["id"]
        n = conn.execute(
            "UPDATE score_runs SET status = 'done', finished_at = now(), n_scored = 0"
            " WHERE id = %s",
            (run_id,),
        ).rowcount
        assert n == 1
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with web_db.tenant_tx(two_tenants_seeded["a"]) as conn:
            conn.execute("UPDATE score_runs SET as_of = now() WHERE id = %s", (run_id,))


def test_tenant_tx_updates_only_its_own_tenant_row(web_db, two_tenants_seeded):
    t = two_tenants_seeded
    sql = "UPDATE tenants SET config_version = config_version WHERE id = %s"
    with web_db.tenant_tx(t["a"]) as conn:
        assert conn.execute("SELECT count(*) AS n FROM tenants").fetchone()["n"] >= 2
        assert conn.execute(sql, (t["a"],)).rowcount == 1
        assert conn.execute(sql, (t["b"],)).rowcount == 0
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with web_db.tenant_tx(t["a"]) as conn:
            conn.execute(
                "INSERT INTO tenants (id, slug, name, timezone, currency, internal_domain, config)"
                " VALUES (gen_random_uuid(), 'rogue', 'x', 'UTC', 'USD', 'x', '{}')"
            )


def test_enqueue_on_conflict_clause_coalesces_tenant_and_system_jobs(
    worker_db, two_tenants_seeded
):
    # The exact clause Task 8's enqueue uses (design Appendix A): it must name the index predicate.
    sql = (
        "INSERT INTO jobs (tenant_id, kind, dedupe_key) VALUES (%s, 'probe', %s)"
        " ON CONFLICT (tenant_id, dedupe_key) WHERE status = 'queued' AND dedupe_key IS NOT NULL"
        " DO NOTHING RETURNING id"
    )
    key = f"probe:{uuid.uuid4()}"
    with worker_db.system_tx() as conn:
        for tid in (two_tenants_seeded["a"], None):
            assert conn.execute(sql, (tid, key)).fetchone() is not None
            assert conn.execute(sql, (tid, key)).fetchone() is None


def test_timestamps_read_back_in_utc(web_db, owner_dsn, two_tenants_seeded):
    """The server's own time zone (Asia/Kolkata here) must never leak into app timestamps."""
    with web_db.tenant_tx(two_tenants_seeded["a"]) as conn:
        at = conn.execute("SELECT available_at FROM raw_records LIMIT 1").fetchone()["available_at"]
    assert at.utcoffset().total_seconds() == 0
    with owner_connect(owner_dsn) as conn:
        assert conn.execute("SELECT now()").fetchone()[0].utcoffset().total_seconds() == 0
