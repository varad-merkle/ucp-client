"""PostgreSQL connection handling, shared by auth.py and profile_store.py.

Connection settings come from the DB_* variables in .env (see .env.example).
Every table this application owns lives in SCHEMA; tests point that at a
throwaway schema so a test run never touches real data.
"""

import os
from contextlib import contextmanager
from typing import Iterator

import psycopg
from psycopg.rows import dict_row

# Schema holding this application's tables. Tests point this at a throwaway schema.
SCHEMA = "public"

_REQUIRED_SETTINGS = ("DB_HOST", "DB_NAME", "DB_USER", "DB_PASSWORD")


def settings() -> dict:
    """Read the connection settings from the environment. DB_PORT is optional (default 5432)."""
    missing = [name for name in _REQUIRED_SETTINGS if not os.environ.get(name)]
    if missing:
        raise RuntimeError(f"Missing database settings in .env: {', '.join(missing)}")
    return {
        "host": os.environ["DB_HOST"],
        "port": os.environ.get("DB_PORT") or "5432",
        "dbname": os.environ["DB_NAME"],
        "user": os.environ["DB_USER"],
        "password": os.environ["DB_PASSWORD"],
    }


@contextmanager
def connect() -> Iterator[psycopg.Connection]:
    """Open a connection that commits on success, rolls back on error, and is always closed."""
    conn = psycopg.connect(**settings(), options=f"-c search_path={SCHEMA}", row_factory=dict_row)
    try:
        with conn:
            yield conn
    finally:
        conn.close()
