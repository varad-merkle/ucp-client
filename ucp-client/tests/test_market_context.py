import asyncio

from conftest import OTHER_USER, USER
from merchants import CART, CHECKOUT, SEARCH, Merchant, Registry
from profile_store import update_profile
from shop import Session, Shop


def make_shop():
    return Shop(Registry(), call_tool=None)


def make_country_shop(merchants):
    registry = Registry()
    registry.merchants = {merchant.id: merchant for merchant in merchants}
    registry.categories = sorted({category for merchant in merchants for category in merchant.categories})
    return Shop(registry, call_tool=None)


def test_no_context_when_profile_is_empty():
    assert make_shop()._market_context(USER) == {}


def test_registry_loads_merchant_supported_countries():
    registry = Registry()

    assert registry.get("allbirds").supported_countries == ["US"]


def test_stores_for_only_returns_merchants_supporting_country():
    supported = Merchant("supported", "Supported", "https://supported.test", ["furniture"],
                         supported_countries=["US"], capabilities={SEARCH})
    unsupported = Merchant("unsupported", "Unsupported", "https://unsupported.test", ["furniture"],
                           supported_countries=["CA"], capabilities={SEARCH})
    shop = make_country_shop([supported, unsupported])

    stores, _ = shop.stores_for(["furniture"], "us")

    assert [store.id for store in stores] == ["supported"]


def test_stores_for_returns_empty_when_no_merchant_supports_country():
    unsupported = Merchant("unsupported", "Unsupported", "https://unsupported.test", ["furniture"],
                           supported_countries=["CA"], capabilities={SEARCH})
    shop = make_country_shop([unsupported])

    stores, reason = shop.stores_for(["furniture"], "US")

    assert stores == []
    assert "No merchant is listed as supporting US" in reason


def test_stores_for_keeps_all_category_matches_when_country_is_missing():
    supported = Merchant("supported", "Supported", "https://supported.test", ["furniture"],
                         supported_countries=["US"], capabilities={SEARCH})
    unsupported = Merchant("unsupported", "Unsupported", "https://unsupported.test", ["furniture"],
                           supported_countries=["CA"], capabilities={SEARCH})
    shop = make_country_shop([supported, unsupported])

    stores, _ = shop.stores_for(["furniture"])

    assert [store.id for store in stores] == ["supported", "unsupported"]


def test_market_preferences_map_to_ucp_context():
    update_profile(USER, market={"country": "IN", "preferred_currency": "INR", "language": "en-IN",
                                 "region": "MH", "postal_code": "411027"})
    assert make_shop()._market_context(USER) == {
        "currency": "INR",
        "address_country": "IN",
        "language": "en-IN",
        "address_region": "MH",
        "postal_code": "411027",
    }


def test_only_saved_preferences_are_sent():
    update_profile(USER, market={"preferred_currency": "GBP"})
    assert make_shop()._market_context(USER) == {"currency": "GBP"}


def test_buyer_prefill_uses_ucp_field_names():
    update_profile(USER, buyer={"first_name": "Alex", "email": "alex@example.com", "phone": "+91 98765 43210"})
    assert make_shop()._profile_buyer(USER) == {
        "first_name": "Alex",
        "email": "alex@example.com",
        "phone_number": "+91 98765 43210",
    }


# --------------------------------------------------------------- per user


def test_each_user_sends_their_own_context_to_the_stores():
    update_profile(USER, market={"preferred_currency": "INR", "country": "IN"})
    update_profile(OTHER_USER, market={"preferred_currency": "GBP", "country": "GB"})
    shop = make_shop()
    assert shop._market_context(USER) == {"currency": "INR", "address_country": "IN"}
    assert shop._market_context(OTHER_USER) == {"currency": "GBP", "address_country": "GB"}


def test_each_user_prefills_checkout_with_their_own_details():
    update_profile(USER, buyer={"first_name": "Alex"})
    update_profile(OTHER_USER, buyer={"first_name": "Sam"})
    shop = make_shop()
    assert shop._profile_buyer(USER) == {"first_name": "Alex"}
    assert shop._profile_buyer(OTHER_USER) == {"first_name": "Sam"}


# --------------------------------------------------------------- sessions


def test_a_session_belongs_to_the_user_who_created_it():
    shop = make_shop()
    session = shop.session(None, USER)
    assert session.username == USER
    assert shop.session(session.id, USER) is session


def test_another_users_session_id_starts_a_fresh_session():
    shop = make_shop()
    mine = shop.session(None, USER)
    mine.carts["pier1"] = {"id": "cart-1"}

    theirs = shop.session(mine.id, OTHER_USER)
    assert theirs is not mine
    assert theirs.username == OTHER_USER
    assert theirs.carts == {}      # the other user's cart is not visible
    assert mine.carts != {}        # and is left untouched


def test_an_unknown_session_id_starts_a_new_session():
    shop = make_shop()
    assert shop.session("no-such-session", USER).username == USER


def test_session_context_uses_the_sessions_own_user():
    """A Session carries its owner, which is how shop methods reach the right profile."""
    update_profile(USER, market={"preferred_currency": "INR"})
    session = Session(id="s1", username=USER)
    assert make_shop()._market_context(session.username) == {"currency": "INR"}


# ------------------------------------------- what actually reaches the store


def fake_store_shop():
    """A Shop wired to one fake store that records the UCP arguments it is sent."""
    store = Merchant(id="pier1", name="Pier 1", url="https://example.test", categories=["shoes"],
                     endpoint="https://example.test/mcp", capabilities={CART, CHECKOUT, SEARCH})
    registry = Registry()
    registry.merchants = {store.id: store}
    sent: list[dict] = []

    async def call_tool(merchant, tool, arguments):
        sent.append({"tool": tool, "arguments": arguments})
        return {"ucp": {"status": "ok"}, "catalog": {"items": []}, "messages": []}

    return Shop(registry, call_tool), sent


async def search_as(shop, username):
    """Run a catalog search for this user and return the UCP catalog request body."""
    session = shop.session(None, username)
    await shop.search(session, [{"query": "running shoes", "categories": ["shoes"], "low": None, "high": None}])


def test_each_users_search_carries_their_own_context_to_the_store():
    """The whole point of the per-user profile: the store sees the shopper's own market."""
    update_profile(USER, market={"country": "IN", "preferred_currency": "INR", "language": "en-IN"})
    update_profile(OTHER_USER, market={"country": "GB", "preferred_currency": "GBP", "language": "en-GB"})
    shop, sent = fake_store_shop()

    asyncio.run(search_as(shop, USER))
    assert sent[-1]["arguments"]["catalog"]["context"] == {
        "currency": "INR", "address_country": "IN", "language": "en-IN",
    }

    asyncio.run(search_as(shop, OTHER_USER))
    assert sent[-1]["arguments"]["catalog"]["context"] == {
        "currency": "GBP", "address_country": "GB", "language": "en-GB",
    }


def test_a_search_sends_no_context_when_that_user_saved_nothing():
    update_profile(USER, market={"preferred_currency": "INR"})
    shop, sent = fake_store_shop()
    asyncio.run(search_as(shop, OTHER_USER))
    assert "context" not in sent[-1]["arguments"]["catalog"]
