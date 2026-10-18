"""Create the app roles and apply radar/migrations/*.sql in order, as the database owner."""

from pathlib import Path

import psycopg
from psycopg import sql

from radar.db import owner_connect

MIGRATIONS = Path(__file__).parent / "migrations"


SAFE = {"rolsuper": "NOSUPERUSER", "rolbypassrls": "NOBYPASSRLS", "rolinherit": "NOINHERIT",
        "rolcreatedb": "NOCREATEDB", "rolcreaterole": "NOCREATEROLE"}


def _ensure_roles(conn: psycopg.Connection, passwords: dict[str, str]) -> None:
    # Roles are cluster-global: another database (or an earlier test run) may already
    # have created them, so create if missing and always re-assert attributes and password.
    for role, password in passwords.items():
        query = sql.SQL("SELECT {} FROM pg_roles WHERE rolname = %s").format(
            sql.SQL(", ").join(map(sql.Identifier, SAFE)))
        row = conn.execute(query, (role,)).fetchone()
        if row is None:
            conn.execute(sql.SQL("CREATE ROLE {}").format(sql.Identifier(role)))
            row = conn.execute(query, (role,)).fetchone()
        # State only the attributes that are unsafe now: a non-superuser owner (Neon's
        # neondb_owner) may not even restate NOSUPERUSER, and a fresh role already has it.
        fix = sql.SQL(" ").join(sql.SQL(SAFE[k]) for k, on in zip(SAFE, row, strict=True) if on)
        conn.execute(
            sql.SQL("ALTER ROLE {} LOGIN {} PASSWORD {}").format(
                sql.Identifier(role), fix, sql.Literal(password))
        )
        # every session of the role reads timestamptz in UTC, pooled or not (radar.db)
        conn.execute(sql.SQL("ALTER ROLE {} SET timezone TO 'UTC'").format(sql.Identifier(role)))


def migrate(owner_dsn: str, web_password: str, worker_password: str) -> list[str]:
    """Apply pending migrations; returns the file names applied this call."""
    applied: list[str] = []
    with owner_connect(owner_dsn, autocommit=True) as conn:
        # Serialise concurrent starters (web and worker boot together); released on close.
        conn.execute("SELECT pg_advisory_lock(hashtext('radar.migrate'))")
        _ensure_roles(conn, {"radar_web": web_password, "radar_worker": worker_password})
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations"
            " (name text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
        )
        done = {r[0] for r in conn.execute("SELECT name FROM schema_migrations")}
        for path in sorted(MIGRATIONS.glob("*.sql")):
            if path.name in done:
                continue
            with conn.transaction():
                conn.execute(path.read_text())
                conn.execute("INSERT INTO schema_migrations (name) VALUES (%s)", (path.name,))
            applied.append(path.name)
    return applied
