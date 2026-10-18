"""The only way app code reaches Postgres: tenant_tx() or system_tx() on a pooled connection."""

from collections.abc import Iterator
from contextlib import contextmanager
from uuid import UUID

import psycopg
from psycopg.rows import DictRow, dict_row, tuple_row
from psycopg_pool import ConnectionPool

Conn = psycopg.Connection[DictRow]
# Every session reads timestamptz in UTC, whatever the server's own time zone is.
UTC_SESSION = "-c timezone=UTC"


def owner_connect(dsn: str, **kwargs) -> psycopg.Connection:
    """A direct connection for migrate/bootstrap/eval helpers, in UTC like the pool."""
    return psycopg.connect(dsn, options=UTC_SESSION, **kwargs)


class Database:
    def __init__(self, dsn: str, min_size: int = 1, max_size: int = 8,
                 pgbouncer: bool = False) -> None:
        # autocommit: transaction() below is the only transaction boundary.
        kwargs = {"autocommit": True, "row_factory": dict_row}
        if pgbouncer:
            # Transaction mode: the next transaction may run on another server connection, which
            # has not prepared our statements; and PgBouncer refuses untracked startup options,
            # so the timezone comes from the app role's own setting (migrate).
            kwargs["prepare_threshold"] = None
        else:
            kwargs["options"] = UTC_SESSION
        self.pool = ConnectionPool(
            dsn,
            connection_class=Conn,
            kwargs=kwargs,
            min_size=min_size,
            max_size=max_size,
            # a connection the server closed (Neon suspends when idle) is replaced, not used
            check=ConnectionPool.check_connection,
            open=True,
        )

    @contextmanager
    def tenant_tx(self, tenant_id: UUID | str, actor: str = "system") -> Iterator[Conn]:
        """One transaction scoped to a tenant; RLS policies read app.tenant_id."""
        with self.pool.connection() as conn, conn.transaction():
            # is_local=true: both settings end with the transaction, so a pooled
            # connection never carries one tenant into the next checkout.
            conn.execute(
                "SELECT set_config('app.tenant_id', %s, true), set_config('app.actor', %s, true)",
                (str(tenant_id), actor),
            )
            yield conn

    @contextmanager
    def system_tx(self) -> Iterator[Conn]:
        """A transaction with no tenant set: tenant tables read as empty."""
        with self.pool.connection() as conn, conn.transaction():
            yield conn

    def close(self) -> None:
        self.pool.close()


def open_checked(dsn: str, pgbouncer: bool = False) -> Database:
    """A worker pool, refused (and closed) for a role RLS would not bind (design §4.3)."""
    db = Database(dsn, pgbouncer=pgbouncer)
    try:
        with db.system_tx() as conn:
            assert_role_safety(conn)
    except Exception:
        db.close()
        raise
    return db


class UnsafeRole(RuntimeError):
    """The connection's role is one RLS would not constrain (design §4.3)."""


def assert_role_safety(conn: psycopg.Connection) -> None:
    """Refuse to run as a role that RLS would not constrain."""
    row = conn.cursor(row_factory=tuple_row).execute(
        "SELECT r.rolname, r.rolsuper, r.rolbypassrls,"
        " EXISTS (SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
        "         WHERE n.nspname = 'public' AND c.relowner = r.oid) AS owns_tables"
        " FROM pg_roles r WHERE r.rolname = current_user"
    ).fetchone()
    name, superuser, bypassrls, owns = row
    if superuser or bypassrls or owns:
        raise UnsafeRole(
            f"role {name} is unsafe for the app (superuser={superuser},"
            f" bypassrls={bypassrls}, owns_tables={owns})"
        )
