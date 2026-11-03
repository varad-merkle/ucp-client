"""Login for the shopping client: accounts live in the database, one profile each.

The app_user table is the single source of truth for who can sign in, and an
account already in it is never overwritten - the database always wins over
configuration.

There is no sign-up form yet, so the first account is created from DEMO_USER and
DEMO_PASSWORD in .env the first time the server starts. That is a bootstrap, not
a second source of truth: changing those values later does not touch an account
that already exists. When sign-up arrives it calls create_user() exactly as the
bootstrap does, and nothing else here has to change.

Passwords are stored as a salted PBKDF2 hash, never in plain text. Logging in
returns a token; tokens are held in memory, so a restart logs everyone out and
no token can outlive the process.
"""

import hashlib
import logging
import os
import secrets

import db

logger = logging.getLogger("ucp-client")

_HASH_ROUNDS = 200_000

# Live tokens: token -> username. Cleared whenever the server restarts.
_tokens: dict[str, str] = {}


def _hash_password(password: str, salt: str) -> str:
    """Derive the stored hash for a password. Same salt and password always give the same hash."""
    return hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), _HASH_ROUNDS).hex()


def create_user(username: str, password: str) -> bool:
    """Add an account, returning False if the username is already taken.

    The single place an account is created. The .env bootstrap calls it today and
    a sign-up endpoint would call it tomorrow, so there is one code path to trust.
    """
    salt = secrets.token_hex(16)
    with db.connect() as conn:
        result = conn.execute(
            """INSERT INTO app_user (username, password_salt, password_hash) VALUES (%s, %s, %s)
               ON CONFLICT (username) DO NOTHING""",
            (username, salt, _hash_password(password, salt)),
        )
    return result.rowcount == 1


def init_auth() -> None:
    """Create the user table, then the .env account if it is not there yet."""
    with db.connect() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS app_user (
                username       TEXT PRIMARY KEY,
                password_salt  TEXT NOT NULL,
                password_hash  TEXT NOT NULL
            )
        """)

    username = os.environ.get("DEMO_USER")
    password = os.environ.get("DEMO_PASSWORD")
    if not username or not password:
        logger.warning("No DEMO_USER / DEMO_PASSWORD in .env: no account was created, so nobody can log in.")
        return
    if create_user(username, password):
        logger.info(f"Created the account {username!r} from .env")
    else:
        # Deliberate: the database wins, so a password changed in the app is never
        # reset by a restart. Drop the row to re-create the account from .env.
        logger.info(f"Account {username!r} already exists; .env left it unchanged")


def login(username: str, password: str) -> str | None:
    """Check the credentials and return a new token, or None if they do not match."""
    with db.connect() as conn:
        user = conn.execute("SELECT * FROM app_user WHERE username = %s", (username,)).fetchone()

    # Hash even when the user does not exist, so a wrong username and a wrong
    # password take the same time and neither can be told apart from outside.
    salt = user["password_salt"] if user else secrets.token_hex(16)
    expected = user["password_hash"] if user else ""
    if not secrets.compare_digest(_hash_password(password, salt), expected):
        return None

    token = secrets.token_urlsafe(32)
    _tokens[token] = username
    return token


def username_for(token: str) -> str | None:
    """The logged-in user for a token, or None if the token is unknown or logged out."""
    return _tokens.get(token)


def logout(token: str) -> None:
    """Invalidate a token. Logging out twice is not an error."""
    _tokens.pop(token, None)


def logout_all() -> None:
    """Invalidate every token at once."""
    _tokens.clear()
