"""One checkout track for every store (UCP continue_url).

Fixable messages are resolved here first; then every store's card offers "Continue to
payment on <store>", whether the store says ready_for_complete (Auto-Xpert) or
requires_escalation (Pier 1, whose checkout extension needs a click).
"""

from conftest import USER
from merchants import CART, CHECKOUT, SEARCH, Merchant, Registry
from shop import Shop, store_checkout_url

PIER1_URL = "https://p1-dev.myshopify.com/cart/c/abc?key=xyz"
AUTOXPERT_URL = "https://auto-xpert.myshopify.com/cart/c/def?key=uvw"
EXTENSION = {"type": "error", "code": "extension_interaction_required",
             "content": "An extension interaction is required to complete the checkout.",
             "severity": "requires_buyer_input"}
NO_ADDRESS = {"type": "error", "code": "delivery_address_required",
              "content": "A destination address is required in order to continue.", "severity": "recoverable"}

BUYER = {"email": "om@example.com", "first_name": "Om", "last_name": "W", "phone_number": "+1 217 555 0123"}
ADDRESS = {"street_address": "1 Main St", "address_locality": "Los Angeles", "address_region": "CA",
           "postal_code": "90012", "address_country": "US"}


def checkout(status, url, messages=()):
    return {"id": "gid://shopify/Checkout/x?key=y", "status": status, "currency": "USD",
            "continue_url": url, "messages": list(messages), "line_items": [], "totals": []}


def reply(store_id, response, complete=True):
    """Run checkout_reply for a session at this store; returns (card, text for the model)."""
    registry = Registry()
    registry.merchants = {store_id: Merchant(id=store_id, name={"pier1": "Pier 1"}.get(store_id, "Auto-Xpert"),
                                             url="https://s.test", categories=[], endpoint="https://s.test/mcp",
                                             capabilities={CART, CHECKOUT, SEARCH})}
    shop = Shop(registry, call_tool=None)
    session = shop.session(None, USER)
    session.checkout_store = store_id
    if complete:
        session.buyer, session.address = dict(BUYER), dict(ADDRESS)
    blocks = shop.checkout_reply(session, response)
    card = next(b for b in blocks if b["type"] == "checkout")["checkout"]
    said = " ".join(b["text"] for b in blocks if b["type"] == "text")
    return card, said


def test_both_stores_continue_to_payment_on_their_own_page():
    pier1, pier1_text = reply("pier1", checkout("requires_escalation", PIER1_URL, [EXTENSION]))
    autoxpert, autoxpert_text = reply("autoxpert", checkout("ready_for_complete", AUTOXPERT_URL))

    assert pier1["continue_url"] == PIER1_URL
    assert autoxpert["continue_url"] == AUTOXPERT_URL
    assert "Continue to payment on Pier 1" in pier1_text
    assert "Continue to payment on Auto-Xpert" in autoxpert_text
    # Pier 1's extra step is passed on; Auto-Xpert has none.
    assert "extension interaction" in pier1_text
    assert "also asks" not in autoxpert_text


def test_fixable_problems_come_first():
    card, said = reply("pier1", checkout("requires_escalation", PIER1_URL, [EXTENSION, NO_ADDRESS]),
                       complete=False)
    assert card["continue_url"] is None
    assert "still needs" in said


def test_no_link_means_no_button():
    card, said = reply("autoxpert", checkout("ready_for_complete", None))
    assert card["continue_url"] is None
    assert "gave no checkout page" in said


def test_only_https_links_are_passed_on():
    assert store_checkout_url({"continue_url": PIER1_URL}) == PIER1_URL
    for url in ("javascript:alert(1)", "http://p1-dev.myshopify.com/cart/c/abc", "", None):
        assert store_checkout_url({"continue_url": url}) is None
