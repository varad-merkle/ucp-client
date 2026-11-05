import pytest
from fastapi.testclient import TestClient

from client import app
from conftest import OTHER_PASSWORD, OTHER_USER, PASSWORD, USER


@pytest.fixture
def anon():
    # Without the `with` block the startup (ngrok, store discovery) does not run.
    return TestClient(app)


def log_in(client, username, password):
    """Return a TestClient that sends this user's token on every request."""
    token = client.post("/api/login", json={"username": username, "password": password}).json()["token"]
    client.headers["Authorization"] = f"Bearer {token}"
    return client


@pytest.fixture
def api(anon):
    return log_in(anon, USER, PASSWORD)


def test_get_profile(api):
    response = api.get("/api/profile")
    assert response.status_code == 200
    assert set(response.json()) == {"username", "schema_version", "buyer", "market", "updated_at"}
    assert response.json()["username"] == USER


def test_patch_updates_and_returns_the_whole_profile(api):
    api.patch("/api/profile", json={"buyer": {"first_name": "Alex"}})
    response = api.patch("/api/profile", json={"market": {"country": "in", "preferred_currency": "inr"}})
    assert response.status_code == 200
    body = response.json()
    assert body["buyer"]["first_name"] == "Alex"
    assert body["market"]["country"] == "IN"
    assert body["market"]["preferred_currency"] == "INR"


def test_patch_null_clears_a_field(api):
    api.patch("/api/profile", json={"buyer": {"phone": "+1 217 555 0123"}})
    response = api.patch("/api/profile", json={"buyer": {"phone": None}})
    assert response.json()["buyer"]["phone"] is None


def test_invalid_value_returns_422_with_reason(api):
    response = api.patch("/api/profile", json={"market": {"preferred_currency": "DOLLAR"}})
    assert response.status_code == 422
    assert response.json()["detail"].startswith("preferred_currency:")


@pytest.mark.parametrize("body", [{"buyer": {"nickname": "x"}}, {"payment": {"card": "x"}}])
def test_unknown_fields_return_422(api, body):
    assert api.patch("/api/profile", json=body).status_code == 422


# ------------------------------------------------------------- per user


def test_the_profile_returned_belongs_to_the_token(anon):
    mine = log_in(anon, USER, PASSWORD)
    mine.patch("/api/profile", json={"buyer": {"first_name": "Alex"}})

    theirs = log_in(TestClient(app), OTHER_USER, OTHER_PASSWORD)
    assert theirs.get("/api/profile").json()["buyer"]["first_name"] is None

    theirs.patch("/api/profile", json={"buyer": {"first_name": "Sam"}})
    assert mine.get("/api/profile").json()["buyer"]["first_name"] == "Alex"


def test_profile_needs_a_token(anon):
    assert anon.get("/api/profile").status_code == 401
    assert anon.patch("/api/profile", json={"buyer": {"first_name": "Alex"}}).status_code == 401


@pytest.mark.parametrize("header", ["", "Bearer not-a-real-token", "nonsense"])
def test_a_bad_token_is_rejected(anon, header):
    assert anon.get("/api/profile", headers={"Authorization": header}).status_code == 401
