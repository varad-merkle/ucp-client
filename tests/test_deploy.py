"""Settings for the Heroku (backend) + Vercel (frontend) deployment."""

from fastapi.testclient import TestClient

import client
import db


# The fake settings are undone at the end of each `with`, before the test database is cleaned up.


def test_database_url_wins_over_the_db_settings(monkeypatch):
    """Heroku Postgres sets DATABASE_URL; it is used as given."""
    with monkeypatch.context() as env:
        env.setenv("DATABASE_URL", "postgres://u:p@heroku-host:5432/d1")
        assert db.conninfo() == "postgres://u:p@heroku-host:5432/d1"


def test_db_settings_are_used_without_a_database_url(monkeypatch):
    with monkeypatch.context() as env:
        env.delenv("DATABASE_URL", raising=False)
        for name, value in {"DB_HOST": "h", "DB_PORT": "5433", "DB_NAME": "n", "DB_USER": "u",
                            "DB_PASSWORD": "p"}.items():
            env.setenv(name, value)
        info = db.conninfo()
    for part in ("host=h", "port=5433", "dbname=n", "user=u", "password=p"):
        assert part in info


def test_no_other_site_may_call_the_api_from_a_browser():
    """The frontend calls the API through its own address, so CORS stays off by default."""
    response = TestClient(client.app).get("/oauth/client.json", headers={"Origin": "https://evil.example"})
    assert response.status_code == 200
    assert "access-control-allow-origin" not in response.headers
