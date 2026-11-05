"""The development tools (exchange log, direct /api/mcp/... store calls) stay out of production.

The exchange log holds buyers' personal details, and /api/mcp/... calls the stores on the
server's behalf, so both exist only with DEV_ENDPOINTS=1 and even then need a login.
"""

import pytest
from conftest import PASSWORD, USER
from fastapi.testclient import TestClient

import auth
import client

DEV_ROUTES = [("get", "/api/exchanges"), ("delete", "/api/exchanges"),
              ("post", "/api/mcp/cart/get"), ("post", "/api/mcp/checkout/get")]


@pytest.fixture
def anon():
    # Without the `with` block the startup (ngrok, store discovery) does not run.
    return TestClient(client.app)


def logged_in(api: TestClient) -> TestClient:
    token = api.post("/api/login", json={"username": USER, "password": PASSWORD}).json()["token"]
    api.headers["Authorization"] = f"Bearer {token}"
    return api


@pytest.mark.parametrize(("method", "path"), DEV_ROUTES)
def test_dev_tools_do_not_exist_by_default(monkeypatch, anon, method, path):
    monkeypatch.setattr(client, "DEV_ENDPOINTS", False)
    assert getattr(logged_in(anon), method)(path).status_code == 404


@pytest.mark.parametrize(("method", "path"), DEV_ROUTES)
def test_dev_tools_need_a_login_even_when_on(monkeypatch, anon, method, path):
    monkeypatch.setattr(client, "DEV_ENDPOINTS", True)
    assert getattr(anon, method)(path).status_code == 401


def test_exchange_log_works_for_a_logged_in_developer(monkeypatch, anon):
    monkeypatch.setattr(client, "DEV_ENDPOINTS", True)
    response = logged_in(anon).get("/api/exchanges")
    assert response.status_code == 200
    assert set(response.json()) == {"exchanges", "total"}


def test_nothing_is_recorded_while_the_tools_are_off(monkeypatch):
    monkeypatch.setattr(client, "DEV_ENDPOINTS", False)
    monkeypatch.setattr(client, "exchanges", [])
    store = client.registry.all()[0]
    client.log_exchange(store, "get_cart", {"id": "c1"}, started=0.0, status=200,
                        response={"buyer": {"email": "someone@example.com"}})
    assert client.exchanges == []


def test_a_login_works_on_every_worker():
    """Tokens are in the database, not in one process: a second app instance accepts them."""
    token = auth.login(USER, PASSWORD)
    assert token
    assert auth.username_for(token) == USER       # what any other worker would look up
    auth.logout(token)
    assert auth.username_for(token) is None
