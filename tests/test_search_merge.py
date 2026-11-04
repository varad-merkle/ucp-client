"""Several searches in one chat turn are shown together.

The model is asked to batch "shoes and a couch" into one search_products call, but it
sometimes makes two calls instead. The second call must add its results to the first
(side by side) instead of replacing them: a shopper once asked for shoes and a couch
and saw only couches.
"""

import asyncio

from conftest import USER
from merchants import CART, CHECKOUT, SEARCH, Merchant, Registry
from shop import Shop


def product(pid: str, title: str) -> dict:
    return {"id": pid, "title": title, "variants": [{"id": f"{pid}-v", "price": {"amount": 10000, "currency": "USD"}}]}


CATALOG = {
    "allbirds": [product("shoe-1", "Tree Runner"), product("shoe-2", "Wool Runner")],
    "furniturecity": [product("sofa-1", "Morehead Sofa")],
}


def make_shop():
    registry = Registry()
    registry.merchants = {
        "allbirds": Merchant(id="allbirds", name="Allbirds", url="https://a.test", categories=["shoes"],
                             endpoint="https://a.test/mcp", capabilities={CART, CHECKOUT, SEARCH}),
        "furniturecity": Merchant(id="furniturecity", name="Furniture City", url="https://f.test",
                                  categories=["sofas"], endpoint="https://f.test/mcp",
                                  capabilities={CART, CHECKOUT, SEARCH}),
    }
    registry.categories = ["shoes", "sofas"]
    calls: list[str] = []

    async def call_tool(merchant, tool, arguments):
        calls.append(merchant.id)
        return {"ucp": {"status": "ok"}, "products": CATALOG[merchant.id], "messages": []}

    return Shop(registry, call_tool), calls


def wanted(query: str, category: str) -> list[dict]:
    return [{"query": query, "categories": [category], "low": None, "high": None}]


def cards(blocks):
    return next(b for b in blocks if b["type"] == "products")


def test_a_second_search_in_the_same_turn_keeps_the_first():
    shop, calls = make_shop()
    session = shop.session(None, USER)
    asyncio.run(shop.search(session, wanted("shoes", "shoes")))
    blocks = asyncio.run(shop.search(session, wanted("couch", "sofas"), add=True))

    shown = cards(blocks)
    assert [g["title"] for g in shown["groups"]] == ["shoes", "couch"]
    assert [p["id"] for p in shown["products"]] == ["shoe-1", "shoe-2", "sofa-1"]
    assert calls == ["allbirds", "furniturecity"]          # the shoes were not searched again
    # Follow-ups ("tell me about the first one", "show more") still know about the shoes.
    assert [s.label for s in session.searches] == ["shoes", "couch"]
    assert shop.find_product(session, "shoe-1")


def test_a_new_turn_replaces_the_previous_results():
    shop, _ = make_shop()
    session = shop.session(None, USER)
    asyncio.run(shop.search(session, wanted("shoes", "shoes")))
    blocks = asyncio.run(shop.search(session, wanted("couch", "sofas")))     # add=False: a new message
    shown = cards(blocks)
    assert "groups" not in shown
    assert [p["id"] for p in shown["products"]] == ["sofa-1"]


def test_asking_again_for_the_same_thing_replaces_it():
    shop, _ = make_shop()
    session = shop.session(None, USER)
    asyncio.run(shop.search(session, wanted("shoes", "shoes")))
    blocks = asyncio.run(shop.search(session, wanted("Shoes", "shoes"), add=True))
    assert "groups" not in cards(blocks)                    # refined search, not a second column
