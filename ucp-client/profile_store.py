"""PostgreSQL persistence for the buyer profile: one record per logged-in user.

Schema version 1: buyer information and market/language preferences.
The table is created automatically when init_db() is called at startup, and a
user's empty profile appears the first time it is read.

Every function takes the username that owns the profile, so one user never sees
or overwrites another's details. Who that user is comes from auth.py; this module
only trusts the name it is given.

Timestamps are stored as ISO 8601 UTC strings (ending in 'Z').
"""

import logging
import re
from datetime import datetime, timezone

import psycopg

import db

logger = logging.getLogger("ucp-client")

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


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _drop_shared_table(conn: psycopg.Connection) -> None:
    """Remove the pre-login table, which held one profile shared by everyone at id = 1.

    That single row has no owner, so there is nothing to carry over: each user
    now starts with an empty profile of their own. Runs once; after the table has
    been rebuilt with a username column there is nothing left to match.
    """
    shared = conn.execute(
        """SELECT 1 FROM information_schema.columns
           WHERE table_schema = %s AND table_name = 'buyer_profile' AND column_name = 'id'""",
        (db.SCHEMA,),
    ).fetchone()
    if shared:
        conn.execute("DROP TABLE buyer_profile")
        logger.info("Replaced the old shared buyer profile with per-user profiles")


def init_db() -> None:
    """Create the profile table. Call after auth.init_auth(), which creates app_user.

    Safe to call on every startup: existing profiles are never overwritten.
    Future schema changes should add an explicit migration here rather than
    altering this function, so the upgrade path stays visible and deliberate.
    """
    with db.connect() as conn:
        _drop_shared_table(conn)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS buyer_profile (
                username            TEXT PRIMARY KEY REFERENCES app_user(username) ON DELETE CASCADE,
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


def _create_if_absent(conn: psycopg.Connection, username: str) -> None:
    """Give this user an empty profile if they do not have one yet."""
    conn.execute(
        """INSERT INTO buyer_profile (username, schema_version, updated_at) VALUES (%s, %s, %s)
           ON CONFLICT (username) DO NOTHING""",
        (username, SCHEMA_VERSION, _now()),
    )


def _row_to_dict(r: dict) -> dict:
    return {
        "username": r["username"],
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


def get_profile(username: str) -> dict:
    """Return this user's profile as a structured dict, empty on first use."""
    with db.connect() as conn:
        _create_if_absent(conn, username)
        row = conn.execute("SELECT * FROM buyer_profile WHERE username = %s", (username,)).fetchone()
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


def update_profile(username: str, buyer: dict | None = None, market: dict | None = None) -> dict:
    """Partial update of one user's profile: only fields present in buyer/market change.

    Semantics:
    - A key present with a non-None value sets that field.
    - A key present with None clears (nulls) that field.
    - A key absent from the dict leaves the stored value unchanged.

    Raises ValueError for unknown field names or invalid values.
    Returns the user's complete updated profile.
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
        return get_profile(username)  # nothing to change

    columns["updated_at"] = _now()
    set_clause = ", ".join(f"{col} = %s" for col in columns)
    values = [*columns.values(), username]

    with db.connect() as conn:
        _create_if_absent(conn, username)
        conn.execute(f"UPDATE buyer_profile SET {set_clause} WHERE username = %s", values)  # noqa: S608
    return get_profile(username)
