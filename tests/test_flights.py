"""
Unit tests for app/tools/flights.py.

These never hit the real Duffel API — httpx calls are mocked via pytest-httpx.
This means CI can run them with zero secrets and zero network flakiness.
"""

from datetime import date, timedelta

import pytest

from app.tools.flights import search_flights
from app.tools.schemas import FlightSearchInput, FlightSearchResult, ToolError

MOCK_OFFER_RESPONSE = {
    "data": {
        "offers": [
            {
                "id": "off_00009htYpSCXrwaB9DnUm0",
                "total_amount": "450.00",
                "total_currency": "USD",
                "cabin_class": "economy",
                "expires_at": "2026-08-10T12:00:00Z",
                "slices": [
                    {
                        "segments": [
                            {
                                "marketing_carrier": {"iata_code": "ZZ", "name": "Duffel Airways"},
                                "marketing_carrier_flight_number": "123",
                                "origin": {"iata_code": "JFK"},
                                "destination": {"iata_code": "ATL"},
                                "departing_at": "2026-08-14T08:00:00",
                                "arriving_at": "2026-08-14T10:30:00",
                            }
                        ]
                    }
                ],
            },
            {
                "id": "off_00009htYpSCXrwaB9DnUm1",
                "total_amount": "999.00",
                "total_currency": "USD",
                "cabin_class": "economy",
                "expires_at": "2026-08-10T12:00:00Z",
                "slices": [
                    {
                        "segments": [
                            {
                                "marketing_carrier": {"iata_code": "ZZ", "name": "Duffel Airways"},
                                "marketing_carrier_flight_number": "456",
                                "origin": {"iata_code": "JFK"},
                                "destination": {"iata_code": "ATL"},
                                "departing_at": "2026-08-14T09:00:00",
                                "arriving_at": "2026-08-14T11:30:00",
                            }
                        ]
                    }
                ],
            },
        ]
    }
}


@pytest.fixture
def query() -> FlightSearchInput:
    return FlightSearchInput(
        origin="jfk",  # lowercase on purpose — tests the uppercase validator
        destination="atl",
        depart_date=date.today() + timedelta(days=30),
        adults=1,
    )


@pytest.fixture(autouse=True)
def _force_real_api_mode():
    """Most tests exercise the Duffel code path, so force mock mode off by
    default. The dedicated mock-mode tests below turn it back on explicitly."""
    from app import config

    config.settings.use_mock_data = False
    yield
    config.settings.use_mock_data = False


def test_returns_offers_sorted_by_price(httpx_mock, query, monkeypatch):
    monkeypatch.setenv("DUFFEL_API_KEY", "duffel_test_fake_key")
    from app import config

    config.settings.duffel_api_key = "duffel_test_fake_key"

    httpx_mock.add_response(
        url="https://api.duffel.com/air/offer_requests?return_offers=true",
        json=MOCK_OFFER_RESPONSE,
        status_code=200,
    )

    result = search_flights(query)

    assert isinstance(result, FlightSearchResult)
    assert result.is_mock is False
    assert len(result.offers) == 2
    # Cheaper offer should be first
    assert result.offers[0].total_price_usd == 450.00
    assert result.offers[1].total_price_usd == 999.00
    assert result.offers[0].segments[0].origin_iata == "JFK"


def test_origin_is_uppercased(query):
    assert query.origin == "JFK"
    assert query.destination == "ATL"


def test_budget_filter_excludes_expensive_offers(httpx_mock, monkeypatch):
    from app import config

    config.settings.duffel_api_key = "duffel_test_fake_key"

    httpx_mock.add_response(
        url="https://api.duffel.com/air/offer_requests?return_offers=true",
        json=MOCK_OFFER_RESPONSE,
        status_code=200,
    )

    query = FlightSearchInput(
        origin="JFK",
        destination="ATL",
        depart_date=date.today() + timedelta(days=30),
        max_budget_usd=500.0,
    )
    result = search_flights(query)

    assert isinstance(result, FlightSearchResult)
    assert len(result.offers) == 1
    assert result.offers[0].total_price_usd == 450.00


def test_api_error_returns_tool_error_not_exception(httpx_mock, monkeypatch, query):
    from app import config

    config.settings.duffel_api_key = "duffel_test_fake_key"

    httpx_mock.add_response(
        url="https://api.duffel.com/air/offer_requests?return_offers=true",
        status_code=422,
        json={"errors": [{"message": "invalid airport code"}]},
    )

    result = search_flights(query)

    assert isinstance(result, ToolError)
    assert result.tool_name == "search_flights"
    assert result.retryable is False


def test_rejects_return_date_before_depart_date():
    with pytest.raises(ValueError, match="return_date cannot be before depart_date"):
        FlightSearchInput(
            origin="JFK",
            destination="ATL",
            depart_date=date.today() + timedelta(days=30),
            return_date=date.today() + timedelta(days=1),
        )


def test_missing_api_key_raises_clear_error(monkeypatch, query):
    from app import config

    config.settings.duffel_api_key = ""

    with pytest.raises(RuntimeError, match="DUFFEL_API_KEY is not set"):
        search_flights(query)


def test_mock_mode_returns_fixture_data_with_no_network_call(query):
    """No httpx_mock fixture used here on purpose — if the code accidentally
    tried a real network call in mock mode, this test would hang/fail."""
    from app import config

    config.settings.use_mock_data = True

    result = search_flights(query)

    assert isinstance(result, FlightSearchResult)
    assert result.is_mock is True
    assert len(result.offers) > 0


def test_mock_mode_falls_back_to_default_for_unknown_route():
    from app import config

    config.settings.use_mock_data = True

    query = FlightSearchInput(
        origin="XXX",
        destination="YYY",
        depart_date=date.today() + timedelta(days=30),
    )
    result = search_flights(query)

    assert isinstance(result, FlightSearchResult)
    assert result.offers[0].offer_id == "mock_off_generic_001"
