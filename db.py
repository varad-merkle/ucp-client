"""PostgreSQL connection handling, shared by auth.py, profile_store.py, identity.py and shop.py.

The database is DATABASE_URL when it is set (Heroku Postgres sets it), otherwise the
DB_* variables in .env (see .env.example). Every table this application owns lives in
SCHEMA; tests point that at a throwaway schema so a test run never touches real data.

Connections come from a pool (DB_POOL_SIZE per worker process, default 5), so a request
reuses open connections instead of connecting to the database for every query. Mind the
database's connection limit: workers x DB_POOL_SIZE must stay below it.
"""

import os
import threading
from collections.abc import Generator
from contextlib import contextmanager

import psycopg
from psycopg.conninfo import make_conninfo
from psycopg.rows import DictRow, dict_row
from psycopg_pool import ConnectionPool

# Schema holding this application's tables. Tests point this at a throwaway schema.
SCHEMA = "public"

# A row read through connect(): column name -> value.
Row = DictRow
Connection = psycopg.Connection[DictRow]

_REQUIRED_SETTINGS = ("DB_HOST", "DB_NAME", "DB_USER", "DB_PASSWORD")

# Any fixed number: the advisory lock taken while the tables are created (see schema_lock).
_SCHEMA_LOCK_ID = 7_000_001


def settings() -> dict[str, str]:
    """Read the DB_* connection settings from the environment. DB_PORT is optional (default 5432)."""
    missing = [name for name in _REQUIRED_SETTINGS if not os.environ.get(name)]
    if missing:
        raise RuntimeError(f"Missing database settings: set DATABASE_URL, or {', '.join(missing)}")
    return {
        "host": os.environ["DB_HOST"],
        "port": os.environ.get("DB_PORT") or "5432",
        "dbname": os.environ["DB_NAME"],
        "user": os.environ["DB_USER"],
        "password": os.environ["DB_PASSWORD"],
    }


def conninfo() -> str:
    """Where the database is: DATABASE_URL (e.g. Heroku Postgres) if set, else the DB_* settings."""
    return (os.environ.get("DATABASE_URL") or "").strip() or make_conninfo(**settings())


# One pool per (database, schema). In production that is a single pool; tests use a
# throwaway schema each and close their pool afterwards (close_pools).
_pools: dict[tuple[str, str], ConnectionPool[Connection]] = {}
_pools_lock = threading.Lock()   # FastAPI runs sync dependencies in threads


def _check(conn: Connection) -> None:
    """Run before a pooled connection is handed out; raises if the server has dropped it, so
    the pool opens a fresh one. Does what ConnectionPool.check_connection does, typed for dict rows."""
    autocommit = conn.autocommit
    conn.autocommit = True
    try:
        conn.execute("")
    finally:
        conn.autocommit = autocommit


def _pool() -> ConnectionPool[Connection]:
    key = (conninfo(), SCHEMA)
    with _pools_lock:
        pool = _pools.get(key)
        if pool is None:
            pool = ConnectionPool(
                key[0],
                connection_class=Connection,
                kwargs={"options": f"-c search_path={SCHEMA}", "row_factory": dict_row},
                min_size=1,
                # At least 2: schema_lock holds one connection while the tables are created on another.
                max_size=max(2, int(os.environ.get("DB_POOL_SIZE") or 5)),
                max_idle=300,                              # close connections idle for 5 minutes
                check=_check,                              # replace connections the server dropped
                open=True,
            )
            _pools[key] = pool
    return pool


def close_pools() -> None:
    """Close every pool (shutdown, and the end of each test)."""
    with _pools_lock:
        while _pools:
            _pools.popitem()[1].close()


@contextmanager
def connect() -> Generator[Connection, None, None]:
    """A pooled connection that commits on success and rolls back on error.

    Rows come back as dicts (column name -> value).
    """
    with _pool().connection() as conn:
        yield conn


@contextmanager
def schema_lock() -> Generator[None, None, None]:
    """Let one worker at a time create the tables on startup.

    Every worker process runs the startup, and two CREATE TABLE IF NOT EXISTS for the same
    table at the same moment can still fail in PostgreSQL, so they take turns.
    """
    with connect() as conn:
        conn.execute("SELECT pg_advisory_lock(%s)", (_SCHEMA_LOCK_ID,))
        try:
            yield
        finally:
            conn.execute("SELECT pg_advisory_unlock(%s)", (_SCHEMA_LOCK_ID,))
