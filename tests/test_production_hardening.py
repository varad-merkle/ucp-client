"""Protections for real customers: password guessing, login expiry, slow chat turns, pooling."""

import asyncio
from types import SimpleNamespace

import pytest
from conftest import OTHER_PASSWORD, OTHER_USER, PASSWORD, USER
from fastapi.testclient import TestClient

import agent as agent_module
import auth
import client
import db
from agent import TOO_SLOW, Agent
from merchants import Registry
from shop import Shop

# ------------------------------------------------------------ password guessing


def test_an_account_is_locked_after_too_many_wrong_passwords():
    for _ in range(auth.MAX_FAILURES_PER_ACCOUNT):
        assert auth.login(USER, "wrong") is None
    with pytest.raises(auth.TooManyAttemptsError):
        auth.login(USER, PASSWORD)                      # even the right password waits
    assert auth.login(OTHER_USER, OTHER_PASSWORD)       # other accounts are unaffected


def test_a_successful_login_resets_the_count():
    for _ in range(auth.MAX_FAILURES_PER_ACCOUNT - 1):
        auth.login(USER, "wrong")
    assert auth.login(USER, PASSWORD)
    for _ in range(auth.MAX_FAILURES_PER_ACCOUNT - 1):
        auth.login(USER, "wrong")
    assert auth.login(USER, PASSWORD)                   # not locked: the count started again


def test_one_address_guessing_many_accounts_is_stopped():
    for n in range(auth.MAX_FAILURES_PER_IP):
        auth.login(f"guess-{n}", "wrong", ip="203.0.113.9")
    with pytest.raises(auth.TooManyAttemptsError):
        auth.login(USER, PASSWORD, ip="203.0.113.9")
    assert auth.login(USER, PASSWORD, ip="198.51.100.1")   # another address still gets in


def test_the_api_answers_429_with_retry_after():
    api = TestClient(client.app)
    for _ in range(auth.MAX_FAILURES_PER_ACCOUNT):
        assert api.post("/api/login", json={"username": USER, "password": "wrong"}).status_code == 401
    response = api.post("/api/login", json={"username": USER, "password": PASSWORD})
    assert response.status_code == 429
    assert response.headers["retry-after"] == str(auth.FAILURE_WINDOW_MINUTES * 60)


# ----------------------------------------------------------------- expiry


def test_a_login_expires():
    token = auth.login(USER, PASSWORD)
    assert token and auth.username_for(token) == USER
    with db.connect() as conn:
        conn.execute("UPDATE app_session SET created_at = now() - make_interval(hours => %s)", (auth.LOGIN_HOURS + 1,))
    assert auth.username_for(token) is None


# ---------------------------------------------------------- slow chat turns


def test_a_slow_chat_turn_answers_before_heroku_gives_up(monkeypatch):
    async def never_answers(**_: object) -> None:
        await asyncio.sleep(5)

    chat = Agent(Shop(Registry(), call_tool=None))
    chat.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=never_answers)))  # type: ignore[assignment]
    monkeypatch.setattr(agent_module, "CHAT_DEADLINE_SECONDS", 0.2)

    reply = asyncio.run(chat.reply("a sofa", None, USER))
    assert reply["blocks"] == [{"type": "notice", "tone": "warning", "text": TOO_SLOW}]


# ------------------------------------------------------------------ pooling


def test_connections_are_reused():
    with db.connect() as conn:
        conn.execute("SELECT 1")
    opened = db._pool().get_stats().get("connections_num", 0)   # pyright: ignore[reportPrivateUsage]
    for _ in range(10):
        with db.connect() as conn:
            conn.execute("SELECT 1")
    assert db._pool().get_stats().get("connections_num", 0) == opened   # pyright: ignore[reportPrivateUsage]
