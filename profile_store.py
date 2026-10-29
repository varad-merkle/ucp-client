"""PostgreSQL persistence for the buyer profile (single record, id = 1).

Schema version 1: buyer information and market/language preferences.
The table is created automatically when init_db() is called at startup.
Connection settings come from the DB_* variables in .env (see .env.example).
Timestamps are stored as ISO 8601 UTC strings (ending in 'Z').

This profile is application-wide; it is not isolated by authenticated user.
For this POC, one shared profile per running instance is intentional.
"""

import os
import re
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator

import psycopg
from psycopg.rows import dict_row

# Schema holding the buyer_profile table. Tests point this at a throwaway schema via monkeypatch.
SCHEMA = "public"

SCHEMA_VERSION = 1

# Accepted field names per section. Used to reject unknown keys.
BUYER_FIELDS = frozenset({"first_name", "last_name", "email", "phone"})
MARKET_FIELDS = frozenset({"country", "preferred_currency", "language", "region", "postal_code"})

# Validation patterns
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_PHONE_RE = re.compile(r"^\+?[\d\s\-().]{6,20}$")
_COUNTRY_RE = re.compile(r"^[A-Z]{2,3}$")          # ISO 3166-1 alpha-2 or alpha-3
_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")            # ISO 4217
_LANGUAGE_RE = re.compile(r"^[a-z]{2,3}(-[A-Za-z]{2,4})?$")  # BCP 47 subset

_REQUIRED_SETTINGS = ("DB_HOST", "DB_NAME", "DB_USER", "DB_PASSWORD")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _connection_settings() -> dict:
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
def _connect() -> Iterator[psycopg.Connection]:
    """Open a connection that commits on success, rolls back on error, and is always closed."""
    conn = psycopg.connect(
        **_connection_settings(),
        options=f"-c search_path={SCHEMA}",
        row_factory=dict_row,
    )
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def _init_schema(conn: psycopg.Connection) -> None:
    """Create the table and the single profile row (id = 1) if absent."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS buyer_profile (
            id                  INTEGER PRIMARY KEY,
            schema_version      INTEGER NOT NULL DEFAULT 1,
            first_name          TEXT,
            last_name           TEXT,
            email               TEXT,
            phone               TEXT,
            country             TEXT,
            preferred_currency  TEXT,
            language            TEXT,
            region              TEXT,
            postal_code         TEXT,
            updated_at          TEXT NOT NULL
        )
    """)
    # ON CONFLICT DO NOTHING: leaves existing data intact on restart.
    conn.execute(
        "INSERT INTO buyer_profile (id, schema_version, updated_at) VALUES (1, %s, %s) ON CONFLICT (id) DO NOTHING",
        (SCHEMA_VERSION, _now()),
    )


def init_db() -> None:
    """Create the table and seed row on first run.

    Safe to call on every startup: existing data is never overwritten.
    Future schema changes should add an explicit migration here rather than
    altering this function, so the upgrade path stays visible and deliberate.
    """
    with _connect() as conn:
        _init_schema(conn)


def _row_to_dict(r: dict) -> dict:
    return {
        "id": r["id"],
        "schema_version": r["schema_version"],
        "buyer": {
            "first_name": r["first_name"],
            "last_name": r["last_name"],
            "email": r["email"],
            "phone": r["phone"],
        },
        "market": {
            "country": r["country"],
            "preferred_currency": r["preferred_currency"],
            "language": r["language"],
            "region": r["region"],
            "postal_code": r["postal_code"],
        },
        "updated_at": r["updated_at"],
    }


def get_profile() -> dict:
    """Return the persisted profile as a structured dict."""
    with _connect() as conn:
        row = conn.execute("SELECT * FROM buyer_profile WHERE id = 1").fetchone()
    return _row_to_dict(row)


def _validate(buyer: dict, market: dict) -> list[str]:
    errors: list[str] = []
    if "email" in buyer and buyer["email"] is not None:
        if not _EMAIL_RE.match(buyer["email"]):
            errors.append("email: invalid format")
    if "phone" in buyer and buyer["phone"] is not None:
        if not _PHONE_RE.match(str(buyer["phone"])):
            errors.append("phone: invalid format (use digits, spaces, hyphens, parentheses, or leading +)")
    if "country" in market and market["country"] is not None:
        if not _COUNTRY_RE.match(str(market["country"]).upper()):
            errors.append("country: use a 2-letter ISO 3166-1 alpha-2 code (e.g. US, IN, GB)")
    if "preferred_currency" in market and market["preferred_currency"] is not None:
        if not _CURRENCY_RE.match(str(market["preferred_currency"]).upper()):
            errors.append("preferred_currency: use a 3-letter ISO 4217 code (e.g. USD, INR, GBP)")
    if "language" in market and market["language"] is not None:
        if not _LANGUAGE_RE.match(str(market["language"])):
            errors.append("language: use a BCP 47 tag (e.g. en, en-US, en-IN, fr-CA)")
    return errors


def update_profile(buyer: dict | None = None, market: dict | None = None) -> dict:
    """Partial update: only fields present in buyer/market are changed.

    Semantics:
    - A key present with a non-None value sets that field.
    - A key present with None clears (nulls) that field.
    - A key absent from the dict leaves the stored value unchanged.

    Raises ValueError for unknown field names or invalid values.
    Returns the complete updated profile.
    """
    buyer = buyer or {}
    market = market or {}

    unknown_buyer = set(buyer) - BUYER_FIELDS
    unknown_market = set(market) - MARKET_FIELDS
    if unknown_buyer or unknown_market:
        raise ValueError(f"Unknown fields: {', '.join(sorted(unknown_buyer | unknown_market))}")

    errors = _validate(buyer, market)
    if errors:
        raise ValueError("; ".join(errors))

    # Normalize to canonical casing before storing.
    if "country" in market and market["country"] is not None:
        market = {**market, "country": market["country"].upper()}
    if "preferred_currency" in market and market["preferred_currency"] is not None:
        market = {**market, "preferred_currency": market["preferred_currency"].upper()}

    columns: dict = {**buyer, **market}
    if not columns:
        return get_profile()  # nothing to change

    columns["updated_at"] = _now()
    set_clause = ", ".join(f"{col} = %s" for col in columns)
    values = [*columns.values(), 1]  # WHERE id = 1

    with _connect() as conn:
        conn.execute(f"UPDATE buyer_profile SET {set_clause} WHERE id = %s", values)  # noqa: S608
    return get_profile()
