import uuid

import psycopg
import pytest
from dotenv import load_dotenv

import profile_store

load_dotenv()


@pytest.fixture(autouse=True)
def profile_db(monkeypatch):
    """Every test gets its own schema in the configured database, dropped afterwards.

    The real buyer_profile table in the public schema is never touched.
    """
    schema = f"test_{uuid.uuid4().hex}"
    settings = profile_store._connection_settings()
    with psycopg.connect(**settings, autocommit=True) as conn:
        conn.execute(f'CREATE SCHEMA "{schema}"')
    monkeypatch.setattr(profile_store, "SCHEMA", schema)
    profile_store.init_db()
    yield
    with psycopg.connect(**settings, autocommit=True) as conn:
        conn.execute(f'DROP SCHEMA "{schema}" CASCADE')
