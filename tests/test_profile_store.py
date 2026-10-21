import re

import pytest

from profile_store import SCHEMA_VERSION, get_profile, init_db, update_profile


def test_new_profile_is_empty():
    profile = get_profile()
    assert profile["id"] == 1
    assert profile["schema_version"] == SCHEMA_VERSION
    assert set(profile["buyer"].values()) == {None}
    assert set(profile["market"].values()) == {None}
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", profile["updated_at"])


def test_init_keeps_existing_data():
    update_profile(buyer={"first_name": "Alex"})
    init_db()
    assert get_profile()["buyer"]["first_name"] == "Alex"


def test_update_changes_only_the_given_fields():
    update_profile(buyer={"first_name": "Alex", "email": "alex@example.com"}, market={"country": "IN"})
    profile = update_profile(market={"preferred_currency": "INR"})
    assert profile["buyer"]["first_name"] == "Alex"
    assert profile["buyer"]["email"] == "alex@example.com"
    assert profile["market"]["country"] == "IN"
    assert profile["market"]["preferred_currency"] == "INR"


def test_null_clears_a_field():
    update_profile(buyer={"first_name": "Alex", "phone": "+91 98765 43210"})
    profile = update_profile(buyer={"phone": None})
    assert profile["buyer"]["phone"] is None
    assert profile["buyer"]["first_name"] == "Alex"


def test_country_and_currency_are_uppercased():
    profile = update_profile(market={"country": "in", "preferred_currency": "inr"})
    assert profile["market"]["country"] == "IN"
    assert profile["market"]["preferred_currency"] == "INR"


def test_empty_update_changes_nothing():
    before = update_profile(buyer={"first_name": "Alex"})
    assert update_profile() == before


def test_unknown_field_is_rejected():
    with pytest.raises(ValueError, match="Unknown fields: nickname"):
        update_profile(buyer={"nickname": "Al"})


@pytest.mark.parametrize(
    ("buyer", "market", "message"),
    [
        ({"email": "not-an-email"}, {}, "email"),
        ({"phone": "abc"}, {}, "phone"),
        ({}, {"country": "INDIA"}, "country"),
        ({}, {"preferred_currency": "DOLLAR"}, "preferred_currency"),
        ({}, {"language": "english"}, "language"),
    ],
)
def test_invalid_values_are_rejected(buyer, market, message):
    with pytest.raises(ValueError, match=message):
        update_profile(buyer=buyer, market=market)
    assert set(get_profile()["buyer"].values()) == {None}


@pytest.mark.parametrize("language", ["en", "en-IN", "fr-CA"])
def test_valid_languages_are_accepted(language):
    assert update_profile(market={"language": language})["market"]["language"] == language
