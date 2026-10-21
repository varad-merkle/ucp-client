from merchants import Registry
from profile_store import update_profile
from shop import Shop


def make_shop():
    return Shop(Registry(), call_tool=None)


def test_no_context_when_profile_is_empty():
    assert make_shop()._market_context() == {}


def test_market_preferences_map_to_ucp_context():
    update_profile(market={"country": "IN", "preferred_currency": "INR", "language": "en-IN",
                           "region": "MH", "postal_code": "411027"})
    assert make_shop()._market_context() == {
        "currency": "INR",
        "address_country": "IN",
        "language": "en-IN",
        "address_region": "MH",
        "postal_code": "411027",
    }


def test_only_saved_preferences_are_sent():
    update_profile(market={"preferred_currency": "GBP"})
    assert make_shop()._market_context() == {"currency": "GBP"}


def test_buyer_prefill_uses_ucp_field_names():
    update_profile(buyer={"first_name": "Alex", "email": "alex@example.com", "phone": "+91 98765 43210"})
    assert make_shop()._profile_buyer() == {
        "first_name": "Alex",
        "email": "alex@example.com",
        "phone_number": "+91 98765 43210",
    }
