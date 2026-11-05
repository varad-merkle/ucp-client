import re

import pytest

from conftest import OTHER_USER, USER
from profile_store import SCHEMA_VERSION, get_profile, init_db, update_profile


def test_new_profile_is_empty():
    profile = get_profile(USER)
    assert profile["username"] == USER
    assert profile["schema_version"] == SCHEMA_VERSION
    assert set(profile["buyer"].values()) == {None}
    assert set(profile["market"].values()) == {None}
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", profile["updated_at"])


def test_init_keeps_existing_data():
    update_profile(USER, buyer={"first_name": "Alex"})
    init_db()
    assert get_profile(USER)["buyer"]["first_name"] == "Alex"


def test_update_changes_only_the_given_fields():
    update_profile(USER, buyer={"first_name": "Alex", "email": "alex@example.com"}, market={"country": "IN"})
    profile = update_profile(USER, market={"preferred_currency": "INR"})
    assert profile["buyer"]["first_name"] == "Alex"
    assert profile["buyer"]["email"] == "alex@example.com"
    assert profile["market"]["country"] == "IN"
    assert profile["market"]["preferred_currency"] == "INR"


def test_null_clears_a_field():
    update_profile(USER, buyer={"first_name": "Alex", "phone": "+91 98765 43210"})
    profile = update_profile(USER, buyer={"phone": None})
    assert profile["buyer"]["phone"] is None
    assert profile["buyer"]["first_name"] == "Alex"


def test_country_and_currency_are_uppercased():
    profile = update_profile(USER, market={"country": "in", "preferred_currency": "inr"})
    assert profile["market"]["country"] == "IN"
    assert profile["market"]["preferred_currency"] == "INR"


def test_empty_update_changes_nothing():
    before = update_profile(USER, buyer={"first_name": "Alex"})
    assert update_profile(USER) == before


def test_unknown_field_is_rejected():
    with pytest.raises(ValueError, match="Unknown fields: nickname"):
        update_profile(USER, buyer={"nickname": "Al"})


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
        update_profile(USER, buyer=buyer, market=market)
    assert set(get_profile(USER)["buyer"].values()) == {None}


@pytest.mark.parametrize("language", ["en", "en-IN", "fr-CA"])
def test_valid_languages_are_accepted(language):
    assert update_profile(USER, market={"language": language})["market"]["language"] == language


# --------------------------------------------------------------- per user


def test_each_user_has_their_own_profile():
    update_profile(USER, buyer={"first_name": "Alex"}, market={"preferred_currency": "INR"})
    update_profile(OTHER_USER, buyer={"first_name": "Sam"}, market={"preferred_currency": "GBP"})

    assert get_profile(USER)["buyer"]["first_name"] == "Alex"
    assert get_profile(USER)["market"]["preferred_currency"] == "INR"
    assert get_profile(OTHER_USER)["buyer"]["first_name"] == "Sam"
    assert get_profile(OTHER_USER)["market"]["preferred_currency"] == "GBP"


def test_one_user_does_not_clear_anothers_fields():
    update_profile(USER, buyer={"phone": "+91 98765 43210"})
    update_profile(OTHER_USER, buyer={"phone": None})
    assert get_profile(USER)["buyer"]["phone"] == "+91 98765 43210"


def test_a_users_profile_starts_empty_even_when_others_are_filled():
    update_profile(USER, buyer={"first_name": "Alex", "email": "alex@example.com"})
    assert set(get_profile(OTHER_USER)["buyer"].values()) == {None}
