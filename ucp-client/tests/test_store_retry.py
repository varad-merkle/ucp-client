import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from mcp.types import TextContent

import client
from merchants import Merchant


class FakeStore:
    """Stands in for the MCP client: fails with the given errors, then answers."""

    def __init__(self, errors):
        self.errors = list(errors)
        self.calls = []

    def __call__(self, endpoint):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def call_tool(self, tool, arguments):
        self.calls.append(arguments)
        if self.errors:
            raise RuntimeError(self.errors.pop(0))
        response = {"ucp": {"status": "success"}, "id": "checkout-1", "line_items": [], "status": "incomplete"}
        return SimpleNamespace(content=[TextContent(type="text", text="")], structured_content=response,
                               is_error=False)


@pytest.fixture
def store(monkeypatch):
    monkeypatch.setattr(client, "RETRY_DELAY", 0)

    def use(*errors):
        fake = FakeStore(errors)
        monkeypatch.setattr(client, "Client", fake)
        return fake

    return use


def merchant():
    return Merchant(id="furniturecity", name="Furniture City", url="https://furniturecity.com",
                    categories=[], endpoint="https://example.test/mcp")


def test_internal_error_is_tried_once_more(store):
    fake = store("Internal error")
    response = asyncio.run(client.call_mcp(merchant(), "create_checkout", {"checkout": {}}))
    assert response["id"] == "checkout-1"
    assert len(fake.calls) == 2
    # The second try is the same call, idempotency key included.
    assert fake.calls[0] == fake.calls[1]


def test_gives_up_after_the_second_internal_error(store):
    fake = store("Internal error", "Internal error")
    with pytest.raises(HTTPException) as failure:
        asyncio.run(client.call_mcp(merchant(), "create_checkout", {"checkout": {}}))
    assert failure.value.detail == "Furniture City create_checkout failed: Internal error"
    assert len(fake.calls) == 2


def test_other_errors_are_not_retried(store):
    fake = store("Rate limit exceeded")
    with pytest.raises(HTTPException):
        asyncio.run(client.call_mcp(merchant(), "create_checkout", {"checkout": {}}))
    assert len(fake.calls) == 1
