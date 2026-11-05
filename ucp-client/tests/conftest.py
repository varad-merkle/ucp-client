import uuid

import psycopg
import pytest
from dotenv import load_dotenv

import auth
import db
import profile_store

load_dotenv()

# Two accounts every test starts with, for the tests that need a logged-in user.
USER, PASSWORD = "demo", "demo-test-password"
OTHER_USER, OTHER_PASSWORD = "om", "om-test-password"


@pytest.fixture(autouse=True)
def database(monkeypatch):
    """Every test gets its own schema in the configured database, dropped afterwards.

    The real tables in the public schema are never touched.
    """
    # The .env bootstrap account is covered by its own tests, so keep it out of the way here.
    monkeypatch.delenv("DEMO_USER", raising=False)
    monkeypatch.delenv("DEMO_PASSWORD", raising=False)

    schema = f"test_{uuid.uuid4().hex}"
    with psycopg.connect(**db.settings(), autocommit=True) as conn:
        conn.execute(f'CREATE SCHEMA "{schema}"')
    monkeypatch.setattr(db, "SCHEMA", schema)
    auth.init_auth()      # accounts first: a profile references its user
    profile_store.init_db()
    auth.create_user(USER, PASSWORD)
    auth.create_user(OTHER_USER, OTHER_PASSWORD)
    yield
    auth.logout_all()     # tokens live in memory, so they outlast the schema
    with psycopg.connect(**db.settings(), autocommit=True) as conn:
        conn.execute(f'DROP SCHEMA "{schema}" CASCADE')
