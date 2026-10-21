import pytest
from fastapi.testclient import TestClient

from client import app


@pytest.fixture
def api():
    # Without the `with` block the startup (ngrok, store discovery) does not run.
    return TestClient(app)


def test_get_profile(api):
    response = api.get("/api/profile")
    assert response.status_code == 200
    assert set(response.json()) == {"id", "schema_version", "buyer", "market", "updated_at"}


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
