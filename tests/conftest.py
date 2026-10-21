import pytest

import profile_store


@pytest.fixture(autouse=True)
def profile_db(tmp_path, monkeypatch):
    """Every test gets its own empty database, so the real buyer_profile.db is never touched."""
    monkeypatch.setattr(profile_store, "DB_PATH", tmp_path / "buyer_profile.db")
    profile_store.init_db()
