"""Owner-only maintenance: prune old scores (Ruling AM) and push a bootstrapped database to
a remote one in bulk (Ruling AT)."""

import os
import re
import shutil
import subprocess
import tempfile
from datetime import timedelta
from pathlib import Path

from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.types.json import Jsonb

from radar import clock
from radar.db import Database, owner_connect
from radar.migrate import migrate

# The runs the app still reads: the latest done run of each kind (latest_scores reads the
# actionable one), any run still being written, and every run a worklist was frozen from.
PRUNABLE = """
SELECT id FROM score_runs r WHERE as_of < %s AND status <> 'running'
  AND NOT EXISTS (SELECT 1 FROM worklists w WHERE w.run_id = r.id)
  AND id NOT IN (SELECT DISTINCT ON (actionable) id FROM score_runs WHERE status = 'done'
                 ORDER BY actionable, as_of DESC, finished_at DESC, id)
"""


def prune(owner_dsn: str, older_than_days: int) -> dict[str, dict[str, int]]:
    """Delete the account_scores and score_runs of runs older than the cutoff that nothing
    reads, one audited transaction per tenant; returns the counts per tenant slug."""
    cutoff, out = clock.now() - timedelta(days=older_than_days), {}
    db = Database(owner_dsn, max_size=1)  # tenant_tx: RLS scopes each tenant's deletes
    try:
        with db.system_tx() as conn:
            tenants = conn.execute("SELECT id, slug FROM tenants ORDER BY slug").fetchall()
        for t in tenants:
            with db.tenant_tx(t["id"], actor="system:prune") as conn:
                runs = [r["id"] for r in conn.execute(PRUNABLE, (cutoff,))]
                counts = {
                    "score_runs": len(runs),
                    "account_scores": conn.execute(
                        "DELETE FROM account_scores WHERE run_id = ANY(%s)", (runs,)).rowcount}
                conn.execute("DELETE FROM score_runs WHERE id = ANY(%s)", (runs,))
                conn.execute(
                    "INSERT INTO audit_log (at, actor, action, entity_type, after)"
                    " VALUES (%s, 'system:prune', 'prune', 'score_runs', %s)",
                    (clock.now(), Jsonb(counts | {"older_than_days": older_than_days})))
            out[t["slug"]] = counts
    finally:
        db.close()
    return out


class PushError(RuntimeError):
    """push refused or failed; the message says why."""


def _tool(name: str, pg_bin: str | None, source_major: int, target_major: int) -> str:
    """The client tool's path. Its major version must be at least the source server's (an
    older pg_dump refuses a newer server) and at most the target's (pg_restore 17 sets
    transaction_timeout, which a 16 server rejects)."""
    path = shutil.which(name, path=pg_bin)
    if path is None:
        raise PushError(f"{name} not found on {'--pg-bin ' + pg_bin if pg_bin else 'PATH'}")
    out = subprocess.run([path, "--version"], capture_output=True, text=True,
                         check=True).stdout
    major = int(re.search(r"\) (\d+)", out).group(1))
    if not source_major <= major <= target_major:
        raise PushError(f"{path} is version {major}; it must be {source_major} to"
                        f" {target_major} (source and target servers): pass --pg-bin")
    return path


def _run(cmd: list[str], dsn: str) -> None:
    """Run a client tool on dsn; the password goes in the environment, not on argv."""
    params = conninfo_to_dict(dsn)
    env = os.environ | ({"PGPASSWORD": params.pop("password")} if "password" in params else {})
    done = subprocess.run([*cmd, "--dbname", make_conninfo(**params)], env=env,
                          capture_output=True, text=True)
    if done.returncode:
        raise PushError(f"{Path(cmd[0]).name} failed:\n{done.stderr}")


def _tables(conn) -> list[str]:
    return [r[0] for r in conn.execute(
        "SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY 1")]


def _counts(conn) -> dict[str, int]:
    return {t: conn.execute(sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(t))
                            ).fetchone()[0] for t in _tables(conn)}


def push(target: str, source: str, web_password: str, worker_password: str,
         pg_bin: str | None = None, force: bool = False) -> dict[str, int]:
    """Copy every row of the bootstrapped source database into target (owner DSNs) and
    return the row count per table, which must match the source's. The target gets the schema
    and app roles from migrate. Its owner must bypass RLS to restore into FORCE RLS tables
    (on Neon the project's default role does, through neon_superuser)."""
    with owner_connect(source) as src, owner_connect(target) as dst:
        majors = src.info.server_version // 10000, dst.info.server_version // 10000
        src_migrations = {r[0] for r in src.execute("SELECT name FROM schema_migrations")}
    dump, restore = (_tool(name, pg_bin, *majors) for name in ("pg_dump", "pg_restore"))
    migrate(target, web_password, worker_password)
    with owner_connect(target, autocommit=True) as dst:
        if not dst.execute("SELECT rolsuper OR rolbypassrls FROM pg_roles"
                           " WHERE rolname = current_user").fetchone()[0]:
            raise PushError("the target role cannot bypass row-level security")
        if {r[0] for r in dst.execute("SELECT name FROM schema_migrations")} != src_migrations:
            raise PushError("source and target schemas differ: run `radar migrate` on the source")
        if dst.execute("SELECT 1 FROM tenants LIMIT 1").fetchone():
            if not force:
                raise PushError("the target already holds tenant data; --force replaces it")
            dst.execute(sql.SQL("TRUNCATE {} CASCADE").format(sql.SQL(", ").join(
                sql.Identifier(t) for t in _tables(dst) if t != "schema_migrations")))
        audited = [r[0] for r in dst.execute(
            "SELECT DISTINCT tgrelid::regclass::text FROM pg_trigger WHERE NOT tgisinternal")]
        with tempfile.TemporaryDirectory() as tmp:
            archive = str(Path(tmp) / "radar.dump")
            _run([dump, "--format=custom", "--data-only",
                  "--exclude-table-data=schema_migrations", "--file", archive], source)
            # restored rows already carry their audit_log rows: the triggers would add twins
            for t in audited:
                dst.execute(sql.SQL("ALTER TABLE {} DISABLE TRIGGER USER").format(
                    sql.Identifier(t)))
            try:
                _run([restore, "--data-only", "--no-owner", "--no-privileges",
                      "--single-transaction", "--exit-on-error", archive], target)
            finally:
                for t in audited:
                    dst.execute(sql.SQL("ALTER TABLE {} ENABLE TRIGGER USER").format(
                        sql.Identifier(t)))
        dst.execute("ANALYZE")
        copied = _counts(dst)
    with owner_connect(source) as src:
        expected = _counts(src)
    if copied != expected:
        diff = {t: (expected.get(t), copied.get(t)) for t in expected.keys() | copied.keys()
                if expected.get(t) != copied.get(t)}
        raise PushError(f"row counts differ (source, target): {diff}")
    return copied
