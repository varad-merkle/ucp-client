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
returns a token. Tokens are kept in the app_session table, not in process memory,
so every worker and dyno accepts the same tokens and a restart or deploy logs
nobody out. Only a SHA-256 of each token is stored, so the table cannot be used
to sign in.
"""

import hashlib
import logging
import os
import secrets

import db

logger = logging.getLogger("ucp-client")

_HASH_ROUNDS = 200_000

# A login lasts this long; then the frontend sends the user back to the login screen.
LOGIN_HOURS = int(os.environ.get("LOGIN_HOURS") or 24 * 7)

# Password guessing: failed logins allowed per account, and per IP address, within the window.
# The counts are in the database, so they hold across every worker and restart.
MAX_FAILURES_PER_ACCOUNT = 5
MAX_FAILURES_PER_IP = 20
FAILURE_WINDOW_MINUTES = 15


class TooManyAttemptsError(Exception):
    """Too many failed logins for this account or address. Try again after FAILURE_WINDOW_MINUTES."""


def _token_key(token: str) -> str:
    """What app_session stores for a token. Tokens are random, so a plain SHA-256 is enough."""
    return hashlib.sha256(token.encode()).hexdigest()


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
        conn.execute("""
            CREATE TABLE IF NOT EXISTS app_session (
                token_hash  TEXT PRIMARY KEY,
                username    TEXT NOT NULL REFERENCES app_user (username) ON DELETE CASCADE,
                created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS login_failure (
                key        TEXT NOT NULL,          -- 'account:<username>' or 'ip:<address>'
                failed_at  TIMESTAMPTZ NOT NULL DEFAULT now()
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS login_failure_key ON login_failure (key, failed_at)")

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


def _failure_limits(username: str, ip: str | None) -> list[tuple[str, int]]:
    """The failure counters a login attempt counts against, with their limits."""
    limits = [(f"account:{username}", MAX_FAILURES_PER_ACCOUNT)]
    if ip:
        limits.append((f"ip:{ip}", MAX_FAILURES_PER_IP))
    return limits


def _blocked(conn: db.Connection, limits: list[tuple[str, int]]) -> bool:
    for key, limit in limits:
        row = conn.execute(
            "SELECT count(*) AS failures FROM login_failure WHERE key = %s AND failed_at > now() - make_interval(mins => %s)",
            (key, FAILURE_WINDOW_MINUTES),
        ).fetchone()
        if row and row["failures"] >= limit:
            return True
    return False


def login(username: str, password: str, ip: str | None = None) -> str | None:
    """Check the credentials and return a new token, or None if they do not match.

    Raises TooManyAttemptsError once this account, or this IP address, has failed too often
    within FAILURE_WINDOW_MINUTES; the password is then not even checked.
    """
    limits = _failure_limits(username, ip)
    with db.connect() as conn:
        conn.execute("DELETE FROM login_failure WHERE failed_at < now() - make_interval(mins => %s)",
                     (FAILURE_WINDOW_MINUTES,))
        if _blocked(conn, limits):
            raise TooManyAttemptsError
        user = conn.execute("SELECT * FROM app_user WHERE username = %s", (username,)).fetchone()

    # Hash even when the user does not exist, so a wrong username and a wrong
    # password take the same time and neither can be told apart from outside.
    salt = user["password_salt"] if user else secrets.token_hex(16)
    expected = user["password_hash"] if user else ""
    if not secrets.compare_digest(_hash_password(password, salt), expected):
        with db.connect() as conn:
            for key, _ in limits:
                conn.execute("INSERT INTO login_failure (key) VALUES (%s)", (key,))
        return None

    token = secrets.token_urlsafe(32)
    with db.connect() as conn:
        conn.execute("DELETE FROM login_failure WHERE key = %s", (limits[0][0],))   # this account starts afresh
        conn.execute("DELETE FROM app_session WHERE created_at < now() - make_interval(hours => %s)", (LOGIN_HOURS,))
        conn.execute("INSERT INTO app_session (token_hash, username) VALUES (%s, %s)", (_token_key(token), username))
    return token


def username_for(token: str) -> str | None:
    """The logged-in user for a token, or None if the token is unknown, expired or logged out."""
    with db.connect() as conn:
        row = conn.execute(
            """SELECT username FROM app_session
               WHERE token_hash = %s AND created_at > now() - make_interval(hours => %s)""",
            (_token_key(token), LOGIN_HOURS),
        ).fetchone()
    return row["username"] if row else None


def logout(token: str) -> None:
    """Invalidate a token. Logging out twice is not an error."""
    with db.connect() as conn:
        conn.execute("DELETE FROM app_session WHERE token_hash = %s", (_token_key(token),))


def logout_all() -> None:
    """Invalidate every token at once."""
    with db.connect() as conn:
        conn.execute("DELETE FROM app_session")
