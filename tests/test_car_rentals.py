"""
Unit tests for app/tools/car_rentals.py.

These never hit the real Duffel Cars API or the real geocoding API — httpx
calls are mocked via pytest-httpx. This means CI can run them with zero
secrets and zero network flakiness.

Request payload shape and response field parsing here match Duffel's real,
verified POST /cars/search and POST /cars/quotes contracts (see
car_rentals.py module docstring) — not a guess. A CarRateOption is an
ESTIMATED price; get_car_rental_quote() must be called for a firm one.
"""

import json
import re
from datetime import datetime, timedelta

import pytest

from app.tools.car_rentals import get_car_rental_quote, search_car_rentals
from app.tools.schemas import (
    CarQuoteInput,
    CarRentalPaymentType,
    CarRentalSearchInput,
    ProposeCarBookingInput,
    ToolError,
)

GEOCODE_URL_RE = re.compile(r"^https://geocoding-api\.open-meteo\.com/v1/search(\?.*)?$")
CARS_SEARCH_URL_RE = re.compile(r"^https://api\.duffel\.com/cars/search(\?.*)?$")
CARS_QUOTES_URL_RE = re.compile(r"^https://api\.duffel\.com/cars/quotes(\?.*)?$")

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

MOCK_CARS_SEARCH_RESPONSE = {
    "data": {
        "id": "car_srch_001",
        "rates": [
            {
                "id": "rat_001",
                "total_amount": "145.00",
                "total_currency": "USD",
                "payment_type": "prepaid",
                "supplier": {"name": "Hertz"},
                "car": {"name": "Toyota Corolla", "category": "Compact"},
            },
            {
                "id": "rat_002",
                "total_amount": "89.50",
                "total_currency": "USD",
                "payment_type": "postpaid",
                "supplier": {"name": "Enterprise"},
                "car": {"name": "Chevrolet Spark", "category": "Economy"},
            },
        ],
    }
}

MOCK_CARS_QUOTE_RESPONSE = {
    "data": {
        "id": "qut_001",
        "total_amount": "92.00",
        "total_currency": "USD",
        "payment_type": "postpaid",
    }
}


def _pickup_at() -> datetime:
    return datetime.now() + timedelta(days=30)


def _dropoff_at() -> datetime:
    return datetime.now() + timedelta(days=33)


def _query(**overrides) -> CarRentalSearchInput:
    defaults = dict(
        pickup_city="Atlanta",
        pickup_at=_pickup_at(),
        dropoff_at=_dropoff_at(),
        driver_age=30,
        driver_country_code="US",
    )
    defaults.update(overrides)
    return CarRentalSearchInput(**defaults)


class TestSearchCarRentals:
    def test_returns_rates_sorted_by_price(self, httpx_mock):
        from app import config

        config.settings.duffel_api_key = "duffel_test_fake_key"

        httpx_mock.add_response(url=GEOCODE_URL_RE, json=MOCK_GEOCODE_RESPONSE, status_code=200)
        httpx_mock.add_response(
            url=CARS_SEARCH_URL_RE, json=MOCK_CARS_SEARCH_RESPONSE, status_code=200
        )

        result = search_car_rentals(_query())

        assert not isinstance(result, ToolError)
        assert result.resolved_pickup_location_name == "Atlanta, Georgia, United States"
        assert result.resolved_dropoff_location_name == "Atlanta, Georgia, United States"
        assert [r.rate_id for r in result.rates] == ["rat_002", "rat_001"]
        assert result.rates[0].estimated_price_total_usd == 89.5
        assert result.rates[0].payment_type == CarRentalPaymentType.POSTPAID
        assert result.rates[0].car_description == "Economy - Chevrolet Spark or similar"

    def test_request_payload_matches_real_duffel_contract(self, httpx_mock):
        """Locks in the verified request shape: separate pickup_date/
        pickup_time (not a single datetime), location.radius alongside
        geographic_coordinates, and a nested driver object."""
        from app import config

        config.settings.duffel_api_key = "duffel_test_fake_key"

        httpx_mock.add_response(url=GEOCODE_URL_RE, json=MOCK_GEOCODE_RESPONSE, status_code=200)
        httpx_mock.add_response(
            url=CARS_SEARCH_URL_RE, json=MOCK_CARS_SEARCH_RESPONSE, status_code=200
        )

        pickup_at = datetime(2026, 9, 14, 10, 30)
        dropoff_at = datetime(2026, 9, 17, 15, 0)
        search_car_rentals(_query(pickup_at=pickup_at, dropoff_at=dropoff_at, radius_km=10))

        requests = httpx_mock.get_requests(url=CARS_SEARCH_URL_RE)
        assert len(requests) == 1
        payload = json.loads(requests[0].content)["data"]

        assert payload["pickup_date"] == "2026-09-14"
        assert payload["pickup_time"] == "10:30"
        assert payload["dropoff_date"] == "2026-09-17"
        assert payload["dropoff_time"] == "15:00"
        assert payload["pickup_location"]["radius"] == 10
        assert payload["pickup_location"]["geographic_coordinates"] == {
            "latitude": 33.749,
            "longitude": -84.388,
        }
        assert payload["driver"] == {"age": 30, "residence_country_code": "US"}

    def test_omitted_dropoff_defaults_to_pickup(self, httpx_mock):
        from app import config

        config.settings.duffel_api_key = "duffel_test_fake_key"

        httpx_mock.add_response(url=GEOCODE_URL_RE, json=MOCK_GEOCODE_RESPONSE, status_code=200)
        httpx_mock.add_response(
            url=CARS_SEARCH_URL_RE, json=MOCK_CARS_SEARCH_RESPONSE, status_code=200
        )

        result = search_car_rentals(_query())

        assert result.resolved_pickup_location_name == result.resolved_dropoff_location_name

    def test_pickup_location_not_found_returns_tool_error(self, httpx_mock):
        from app import config

        config.settings.duffel_api_key = "duffel_test_fake_key"

        httpx_mock.add_response(
            url=GEOCODE_URL_RE, json=MOCK_GEOCODE_EMPTY_RESPONSE, status_code=200
        )

        result = search_car_rentals(_query(pickup_city="Nowhereville"))

        assert isinstance(result, ToolError)
        assert result.error_type == "location_not_found"

    def test_mock_mode_makes_no_network_calls(self, httpx_mock, monkeypatch):
        from app import config

        monkeypatch.setattr(config.settings, "use_mock_data", True)

        result = search_car_rentals(_query())

        assert not isinstance(result, ToolError)
        assert result.is_mock is True
        assert len(httpx_mock.get_requests()) == 0


class TestGetCarRentalQuote:
    def test_returns_firm_quote(self, httpx_mock):
        from app import config

        config.settings.duffel_api_key = "duffel_test_fake_key"

        httpx_mock.add_response(
            url=CARS_QUOTES_URL_RE, json=MOCK_CARS_QUOTE_RESPONSE, status_code=200
        )

        result = get_car_rental_quote(CarQuoteInput(rate_id="rat_001"))

        assert not isinstance(result, ToolError)
        assert result.quote_id == "qut_001"
        assert result.rate_id == "rat_001"
        assert result.total_price_usd == 92.0
        assert result.payment_type == CarRentalPaymentType.POSTPAID

    def test_request_payload_sends_rate_id(self, httpx_mock):
        from app import config

        config.settings.duffel_api_key = "duffel_test_fake_key"

        httpx_mock.add_response(
            url=CARS_QUOTES_URL_RE, json=MOCK_CARS_QUOTE_RESPONSE, status_code=200
        )

        get_car_rental_quote(CarQuoteInput(rate_id="rat_001"))

        requests = httpx_mock.get_requests(url=CARS_QUOTES_URL_RE)
        payload = json.loads(requests[0].content)["data"]
        assert payload == {"rate_id": "rat_001"}

    def test_mock_mode_rate_not_found_returns_tool_error(self, monkeypatch):
        from app import config

        monkeypatch.setattr(config.settings, "use_mock_data", True)

        result = get_car_rental_quote(CarQuoteInput(rate_id="does_not_exist"))

        assert isinstance(result, ToolError)
        assert result.error_type == "rate_not_found"

    def test_mock_mode_finds_rate_by_id(self, monkeypatch):
        from app import config

        monkeypatch.setattr(config.settings, "use_mock_data", True)

        search_result = search_car_rentals(_query())
        assert not isinstance(search_result, ToolError)
        rate_id = search_result.rates[0].rate_id

        result = get_car_rental_quote(CarQuoteInput(rate_id=rate_id))

        assert not isinstance(result, ToolError)
        assert result.rate_id == rate_id


class TestCarRentalSearchInputValidation:
    def test_partial_pickup_coordinates_rejected(self):
        with pytest.raises(ValueError, match="pickup_latitude and pickup_longitude"):
            CarRentalSearchInput(
                pickup_latitude=33.7,
                pickup_at=_pickup_at(),
                dropoff_at=_dropoff_at(),
                driver_age=30,
                driver_country_code="US",
            )

    def test_both_pickup_city_and_coordinates_rejected(self):
        with pytest.raises(ValueError, match="pickup_city or pickup lat/lon"):
            CarRentalSearchInput(
                pickup_city="Atlanta",
                pickup_latitude=33.7,
                pickup_longitude=-84.3,
                pickup_at=_pickup_at(),
                dropoff_at=_dropoff_at(),
                driver_age=30,
                driver_country_code="US",
            )

    def test_dropoff_before_pickup_rejected(self):
        with pytest.raises(ValueError, match="dropoff_at must be after pickup_at"):
            _query(pickup_at=_dropoff_at(), dropoff_at=_pickup_at())

    def test_driver_age_below_minimum_rejected(self):
        with pytest.raises(ValueError):
            _query(driver_age=10)


class TestProposeCarBookingInputValidation:
    def test_quote_rate_id_mismatch_rejected(self):
        from datetime import date as date_cls

        from app.tools.schemas import CarQuoteResult, CarRateOption, DriverDetails

        rate = CarRateOption(
            rate_id="rat_A",
            car_description="Compact",
            supplier_name="Hertz",
            payment_type=CarRentalPaymentType.PREPAID,
            estimated_price_total_usd=100.0,
            pickup_location_name="Atlanta",
            dropoff_location_name="Atlanta",
            pickup_at=_pickup_at(),
            dropoff_at=_dropoff_at(),
        )
        mismatched_quote = CarQuoteResult(
            quote_id="qut_1",
            rate_id="rat_B",
            total_price_usd=100.0,
            payment_type=CarRentalPaymentType.PREPAID,
        )
        driver = DriverDetails(
            given_name="A",
            family_name="B",
            date_of_birth=date_cls(1990, 1, 1),
            email="a@example.com",
            phone_number="+15555555555",
        )

        with pytest.raises(ValueError, match="does not match"):
            ProposeCarBookingInput(
                trip_id="00000000-0000-0000-0000-000000000000",
                rate=rate,
                quote=mismatched_quote,
                driver=driver,
            )
