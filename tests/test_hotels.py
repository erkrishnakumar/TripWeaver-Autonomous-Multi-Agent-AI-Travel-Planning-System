"""
Unit tests for app/tools/hotels.py.

These never hit the real Duffel Stays API or the real geocoding API — httpx
calls are mocked via pytest-httpx. This means CI can run them with zero
secrets and zero network flakiness.

Request payload shape and response field parsing here match Duffel's real,
verified POST /stays/search contract (see hotels.py module docstring) — not
a guess. A HotelListing is an ESTIMATED price, not a firm bookable offer;
see HotelListing's docstring in schemas.py for why.
"""

import json
import re
from datetime import date, timedelta

import pytest

from app.tools.hotels import search_hotels
from app.tools.schemas import ChildGuest, HotelSearchInput, HotelSearchResult, ToolError

GEOCODE_URL_RE = re.compile(r"^https://geocoding-api\.open-meteo\.com/v1/search(\?.*)?$")
STAYS_SEARCH_URL_RE = re.compile(r"^https://api\.duffel\.com/stays/search(\?.*)?$")

MOCK_GEOCODE_RESPONSE = {
    "results": [
        {
            "name": "Atlanta",
            "latitude": 33.749,
            "longitude": -84.388,
            "admin1": "Georgia",
            "country": "United States",
        }
    ]
}

MOCK_GEOCODE_EMPTY_RESPONSE = {"results": []}

MOCK_STAYS_SEARCH_RESPONSE = {
    "data": {
        "results": [
            {
                "id": "srr_off_001",
                "cheapest_rate_total_amount": "540.00",
                "cheapest_rate_currency": "USD",
                "expires_at": "2099-01-01T00:00:00Z",
                "accommodation": {
                    "name": "Peachtree Grand Hotel",
                    "rating": 4,
                    "review_score": 8.6,
                    "location": {
                        "geographic_coordinates": {"latitude": 33.749, "longitude": -84.388},
                        "address": {"line_one": "210 Peachtree St NW", "city_name": "Atlanta"},
                    },
                },
            },
            {
                "id": "srr_off_002",
                "cheapest_rate_total_amount": "312.00",
                "cheapest_rate_currency": "USD",
                "expires_at": "2099-01-01T00:00:00Z",
                "accommodation": {
                    "name": "Midtown Budget Inn",
                    "rating": 3,
                    "review_score": 7.1,
                    "location": {
                        "geographic_coordinates": {"latitude": 33.782, "longitude": -84.383},
                        "address": {"line_one": "88 14th St NE", "city_name": "Atlanta"},
                    },
                },
            },
        ]
    }
}


@pytest.fixture
def city_query() -> HotelSearchInput:
    return HotelSearchInput(
        city="Atlanta",
        check_in=date.today() + timedelta(days=30),
        check_out=date.today() + timedelta(days=33),
        adults=2,
    )


@pytest.fixture
def coord_query() -> HotelSearchInput:
    return HotelSearchInput(
        latitude=33.749,
        longitude=-84.388,
        check_in=date.today() + timedelta(days=30),
        check_out=date.today() + timedelta(days=33),
        adults=2,
    )


@pytest.fixture(autouse=True)
def _force_real_api_mode():
    """Most tests exercise the Duffel Stays code path, so force mock mode
    off by default. The dedicated mock-mode tests below turn it back on
    explicitly. Mirrors the same fixture in test_flights.py."""
    from app import config

    config.settings.use_mock_data = False
    yield
    config.settings.use_mock_data = False


def test_returns_listings_sorted_by_price(httpx_mock, city_query):
    from app import config

    config.settings.duffel_api_key = "duffel_test_fake_key"

    httpx_mock.add_response(url=GEOCODE_URL_RE, json=MOCK_GEOCODE_RESPONSE, status_code=200)
    httpx_mock.add_response(
        url=STAYS_SEARCH_URL_RE, json=MOCK_STAYS_SEARCH_RESPONSE, status_code=200
    )

    result = search_hotels(city_query)

    assert isinstance(result, HotelSearchResult)
    assert result.is_mock is False
    assert result.resolved_location_name == "Atlanta, Georgia, United States"
    assert len(result.listings) == 2
    # Cheaper listing should be first
    assert result.listings[0].estimated_price_total_usd == 312.00
    assert result.listings[1].estimated_price_total_usd == 540.00
    assert result.listings[0].hotel_name == "Midtown Budget Inn"
    assert result.listings[0].nights == 3
    assert result.listings[0].search_result_id == "srr_off_002"
    assert result.listings[0].city_name == "Atlanta"


def test_lat_lon_input_skips_geocoding(httpx_mock, coord_query):
    """No geocoding mock registered on purpose — if the code tried to
    geocode when lat/lon were already given, this test would fail on an
    unmatched request, same pattern as test_weather.py."""
    from app import config

    config.settings.duffel_api_key = "duffel_test_fake_key"

    httpx_mock.add_response(
        url=STAYS_SEARCH_URL_RE, json=MOCK_STAYS_SEARCH_RESPONSE, status_code=200
    )

    result = search_hotels(coord_query)

    assert isinstance(result, HotelSearchResult)
    assert result.resolved_location_name == "33.7490, -84.3880"
    assert len(result.listings) == 2


def test_request_payload_matches_real_duffel_contract(httpx_mock):
    """Locks in the verified request shape: rooms (int), location.radius
    (not radius_km), guests as typed list with per-child age, snake_case
    dates as strings. No top-level "accommodation" key for a location-based
    search — Duffel's real API treats "accommodation" as the alternate,
    mutually-exclusive ID-based search mode and 422s with "accommodation.ids
    can't be blank" if it's present without ids, as confirmed against a live
    sandbox call."""
    from app import config

    config.settings.duffel_api_key = "duffel_test_fake_key"

    httpx_mock.add_response(url=GEOCODE_URL_RE, json=MOCK_GEOCODE_RESPONSE, status_code=200)
    httpx_mock.add_response(
        url=STAYS_SEARCH_URL_RE, json=MOCK_STAYS_SEARCH_RESPONSE, status_code=200
    )

    query = HotelSearchInput(
        city="Atlanta",
        check_in=date.today() + timedelta(days=30),
        check_out=date.today() + timedelta(days=33),
        adults=2,
        children=[ChildGuest(age=7)],
        rooms=2,
        radius_km=15,
    )
    search_hotels(query)

    requests = httpx_mock.get_requests(url=STAYS_SEARCH_URL_RE)
    assert len(requests) == 1
    payload = json.loads(requests[0].content)["data"]

    assert payload["rooms"] == 2
    assert payload["location"]["radius"] == 15
    assert payload["location"]["geographic_coordinates"] == {
        "latitude": 33.749,
        "longitude": -84.388,
    }
    assert "accommodation" not in payload
    assert {"type": "adult"} in payload["guests"]
    assert {"type": "child", "age": 7} in payload["guests"]
    assert sum(1 for g in payload["guests"] if g["type"] == "adult") == 2


def test_budget_filter_is_per_night_not_total(httpx_mock):
    """The $540/3-night listing is $180/night, the $312/3-night listing is
    $104/night. A $150/night cap should exclude only the pricier one."""
    from app import config

    config.settings.duffel_api_key = "duffel_test_fake_key"

    httpx_mock.add_response(url=GEOCODE_URL_RE, json=MOCK_GEOCODE_RESPONSE, status_code=200)
    httpx_mock.add_response(
        url=STAYS_SEARCH_URL_RE, json=MOCK_STAYS_SEARCH_RESPONSE, status_code=200
    )

    query = HotelSearchInput(
        city="Atlanta",
        check_in=date.today() + timedelta(days=30),
        check_out=date.today() + timedelta(days=33),
        max_budget_usd_per_night=150.0,
    )
    result = search_hotels(query)

    assert isinstance(result, HotelSearchResult)
    assert len(result.listings) == 1
    assert result.listings[0].hotel_name == "Midtown Budget Inn"


def test_city_not_found_returns_tool_error(httpx_mock):
    from app import config

    config.settings.duffel_api_key = "duffel_test_fake_key"

    httpx_mock.add_response(url=GEOCODE_URL_RE, json=MOCK_GEOCODE_EMPTY_RESPONSE, status_code=200)

    query = HotelSearchInput(
        city="Nowhereville",
        check_in=date.today() + timedelta(days=30),
        check_out=date.today() + timedelta(days=33),
    )
    result = search_hotels(query)

    assert isinstance(result, ToolError)
    assert result.tool_name == "search_hotels"
    assert result.error_type == "location_not_found"
    assert result.retryable is False


def test_duffel_api_error_returns_tool_error_not_exception(httpx_mock, coord_query):
    from app import config

    config.settings.duffel_api_key = "duffel_test_fake_key"

    httpx_mock.add_response(
        url=STAYS_SEARCH_URL_RE,
        status_code=422,
        json={"errors": [{"message": "invalid location"}]},
    )

    result = search_hotels(coord_query)

    assert isinstance(result, ToolError)
    assert result.tool_name == "search_hotels"
    assert result.error_type == "duffel_api_error"
    assert result.retryable is False


def test_missing_api_key_returns_tool_error_not_exception(coord_query):
    from app import config

    config.settings.duffel_api_key = ""

    result = search_hotels(coord_query)

    assert isinstance(result, ToolError)
    assert result.tool_name == "search_hotels"
    assert result.error_type == "config_error"
    assert result.retryable is False


def test_mock_mode_returns_fixture_data_with_no_network_call(city_query):
    """No httpx_mock fixture used here on purpose — if the code accidentally
    tried a real network call in mock mode, this test would hang/fail,
    same pattern as test_flights.py."""
    from app import config

    config.settings.use_mock_data = True

    result = search_hotels(city_query)

    assert isinstance(result, HotelSearchResult)
    assert result.is_mock is True
    assert len(result.listings) > 0
    assert result.listings[0].hotel_name in {"Midtown Budget Inn", "Peachtree Grand Hotel"}


def test_mock_mode_falls_back_to_default_for_unknown_city():
    """No httpx_mock fixture used here on purpose — mock mode must never
    geocode, even for a city with no matching fixture entry. It should fall
    straight through to the DEFAULT fixture instead of calling the real
    geocoding API to resolve a display name."""
    from app import config

    config.settings.use_mock_data = True

    query = HotelSearchInput(
        city="Nowhereville",
        check_in=date.today() + timedelta(days=30),
        check_out=date.today() + timedelta(days=33),
    )
    result = search_hotels(query)

    assert isinstance(result, HotelSearchResult)
    assert result.listings[0].search_result_id == "srr_mock_generic_001"
    assert result.resolved_location_name == "Nowhereville"


def test_mock_mode_with_lat_lon_falls_back_to_default(coord_query):
    """No city given means no fixture key to look up by, so this should
    always land on DEFAULT regardless of which coordinates were passed."""
    from app import config

    config.settings.use_mock_data = True

    result = search_hotels(coord_query)

    assert isinstance(result, HotelSearchResult)
    assert result.listings[0].search_result_id == "srr_mock_generic_001"


def test_nights_calculated_correctly(city_query):
    from app import config

    config.settings.use_mock_data = True

    result = search_hotels(city_query)

    assert isinstance(result, HotelSearchResult)
    # city_query is a 3-night stay (check_in +30 to check_out +33)
    for listing in result.listings:
        assert listing.nights == 3


def test_rejects_check_out_before_check_in():
    with pytest.raises(ValueError, match="check_out must be after check_in"):
        HotelSearchInput(
            city="Atlanta",
            check_in=date.today() + timedelta(days=30),
            check_out=date.today() + timedelta(days=30),
        )


def test_rejects_both_city_and_coords():
    with pytest.raises(ValueError, match="provide either city or lat/lon, not both"):
        HotelSearchInput(
            city="Atlanta",
            latitude=33.749,
            longitude=-84.388,
            check_in=date.today() + timedelta(days=30),
            check_out=date.today() + timedelta(days=33),
        )


def test_rejects_neither_city_nor_coords():
    with pytest.raises(ValueError, match="provide either city or both latitude and longitude"):
        HotelSearchInput(
            check_in=date.today() + timedelta(days=30),
            check_out=date.today() + timedelta(days=33),
        )


def test_rejects_partial_coords():
    with pytest.raises(ValueError, match="latitude and longitude must both be provided together"):
        HotelSearchInput(
            latitude=33.749,
            check_in=date.today() + timedelta(days=30),
            check_out=date.today() + timedelta(days=33),
        )


def test_rejects_radius_over_max():
    with pytest.raises(ValueError):
        HotelSearchInput(
            city="Atlanta",
            check_in=date.today() + timedelta(days=30),
            check_out=date.today() + timedelta(days=33),
            radius_km=150,
        )


def test_children_default_to_empty_list(city_query):
    assert city_query.children == []
    assert city_query.adults == 2


def test_child_guest_requires_age():
    with pytest.raises(ValueError):
        ChildGuest()


def test_child_guest_rejects_negative_age():
    with pytest.raises(ValueError):
        ChildGuest(age=-1)
