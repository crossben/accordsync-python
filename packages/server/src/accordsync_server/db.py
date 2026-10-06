"""The connection pool: psycopg 3, autocommit connections, explicit transactions."""

from __future__ import annotations

from psycopg_pool import ConnectionPool

from .sync import Conn, Pool


def _configure(conn: Conn) -> None:
    # Every statement outside `with conn.transaction()` commits on its own; transactions are
    # always explicit (BEGIN ... COMMIT), so nothing is ever left open between requests.
    conn.autocommit = True


def create_pool(database_url: str, *, size: int = 20, open: bool = True) -> Pool:
    """A pool of up to `size` connections (the TypeScript server's default is 20)."""
    pool: Pool = ConnectionPool(
        database_url,
        min_size=1,
        max_size=size,
        configure=_configure,
        open=open,
    )
    return pool
