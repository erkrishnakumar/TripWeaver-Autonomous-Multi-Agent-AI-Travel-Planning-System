"""
Unit tests for app/tools/weather.py.

These never hit the real Open-Meteo API — httpx calls are mocked via
pytest-httpx. This means CI can run them with zero secrets and zero network
flakiness. Unlike flights.py, weather.py has no USE_MOCK_DATA / fixture-file
mode — Open-Meteo needs no key, so there is no "mock mode" to test, only the
real-call code path with httpx mocked underneath it.
"""

import re
from datetime import date, timedelta

import pytest

from app.tools.schemas import ToolError, WeatherForecastResult, WeatherSearchInput
from app.tools.weather import get_weather_forecast

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

MOCK_FORECAST_RESPONSE = {
    "daily": {
        "time": ["2026-08-19", "2026-08-20"],
        "temperature_2m_max": [31.2, 29.8],
        "temperature_2m_min": [21.5, 20.1],
        "precipitation_probability_max": [40, 10],
        "weather_code": [61, 1],
    }
}

# pytest-httpx treats a bare string `url=` as an EXACT match, including the
# full query string. Our real requests always carry query params, so a bare
# string here would never match and the (unmocked) request would silently
# fall through to a real, slow-timing-out network call. Match on path only,
# regardless of query string, via regex instead.
GEOCODE_URL_RE = re.compile(r"^https://geocoding-api\.open-meteo\.com/v1/search(\?.*)?$")
FORECAST_URL_RE = re.compile(r"^https://api\.open-meteo\.com/v1/forecast(\?.*)?$")


@pytest.fixture
def city_query() -> WeatherSearchInput:
    return WeatherSearchInput(
        city="Atlanta",
        start_date=date.today() + timedelta(days=2),
        end_date=date.today() + timedelta(days=3),
    )


@pytest.fixture
def coord_query() -> WeatherSearchInput:
    return WeatherSearchInput(
        latitude=33.749,
        longitude=-84.388,
        start_date=date.today() + timedelta(days=2),
        end_date=date.today() + timedelta(days=3),
    )


def test_returns_forecast_for_city(httpx_mock, city_query):
    httpx_mock.add_response(
        url=GEOCODE_URL_RE,
        json=MOCK_GEOCODE_RESPONSE,
        status_code=200,
        is_reusable=True,
    )
    httpx_mock.add_response(
        url=FORECAST_URL_RE,
        json=MOCK_FORECAST_RESPONSE,
        status_code=200,
        is_reusable=True,
    )

    result = get_weather_forecast(city_query)

    assert isinstance(result, WeatherForecastResult)
    assert result.resolved_location_name == "Atlanta, Georgia, United States"
    assert len(result.daily) == 2
    assert result.daily[0].temp_max_c == 31.2
    assert result.daily[0].weather_description == "Slight rain"
    assert result.daily[1].weather_description == "Mainly clear"


def test_returns_forecast_for_lat_lon_skips_geocoding(httpx_mock, coord_query):
    """No geocoding mock registered on purpose — if the code tried to
    geocode when lat/lon were already given, this test would fail on an
    unmatched request."""
    httpx_mock.add_response(
        url=FORECAST_URL_RE,
        json=MOCK_FORECAST_RESPONSE,
        status_code=200,
    )

    result = get_weather_forecast(coord_query)

    assert isinstance(result, WeatherForecastResult)
    assert result.resolved_location_name == "33.7490, -84.3880"
    assert len(result.daily) == 2


def test_city_not_found_returns_tool_error(httpx_mock):
    httpx_mock.add_response(
        url=GEOCODE_URL_RE,
        json=MOCK_GEOCODE_EMPTY_RESPONSE,
        status_code=200,
    )

    query = WeatherSearchInput(
        city="Nowhereville",
        start_date=date.today() + timedelta(days=2),
        end_date=date.today() + timedelta(days=3),
    )
    result = get_weather_forecast(query)

    assert isinstance(result, ToolError)
    assert result.tool_name == "get_weather_forecast"
    assert result.error_type == "location_not_found"
    assert result.retryable is False


def test_api_error_returns_tool_error_not_exception(httpx_mock, coord_query):
    httpx_mock.add_response(
        url=FORECAST_URL_RE,
        status_code=400,
        json={"reason": "invalid parameter"},
    )

    result = get_weather_forecast(coord_query)

    assert isinstance(result, ToolError)
    assert result.tool_name == "get_weather_forecast"
    assert result.error_type == "open_meteo_api_error"
    assert result.retryable is False


def test_dates_beyond_forecast_horizon_return_tool_error_without_network_call():
    """No httpx_mock fixture used here on purpose — if the code accidentally
    tried a real network call for out-of-range dates, this test would
    hang/fail instead of hitting the intended fast, offline-safe early
    return."""
    query = WeatherSearchInput(
        city="Atlanta",
        start_date=date.today() + timedelta(days=40),
        end_date=date.today() + timedelta(days=45),
    )

    result = get_weather_forecast(query)

    assert isinstance(result, ToolError)
    assert result.tool_name == "get_weather_forecast"
    assert result.error_type == "forecast_range_exceeded"
    assert result.retryable is False
    assert "days out" in result.message


def test_rejects_end_date_before_start_date():
    with pytest.raises(ValueError, match="end_date cannot be before start_date"):
        WeatherSearchInput(
            city="Atlanta",
            start_date=date.today() + timedelta(days=5),
            end_date=date.today() + timedelta(days=1),
        )


def test_rejects_both_city_and_coords():
    with pytest.raises(ValueError, match="provide either city or lat/lon, not both"):
        WeatherSearchInput(
            city="Atlanta",
            latitude=33.749,
            longitude=-84.388,
            start_date=date.today() + timedelta(days=2),
            end_date=date.today() + timedelta(days=3),
        )


def test_rejects_neither_city_nor_coords():
    with pytest.raises(ValueError, match="provide either city or both latitude and longitude"):
        WeatherSearchInput(
            start_date=date.today() + timedelta(days=2),
            end_date=date.today() + timedelta(days=3),
        )


def test_rejects_partial_coords():
    with pytest.raises(ValueError, match="latitude and longitude must both be provided together"):
        WeatherSearchInput(
            latitude=33.749,
            start_date=date.today() + timedelta(days=2),
            end_date=date.today() + timedelta(days=3),
        )


def test_unknown_weather_code_falls_back_to_generic_description(httpx_mock, coord_query):
    forecast_with_unknown_code = {
        "daily": {
            "time": ["2026-08-19"],
            "temperature_2m_max": [25.0],
            "temperature_2m_min": [15.0],
            "precipitation_probability_max": [0],
            "weather_code": [777],
        }
    }
    httpx_mock.add_response(
        url=FORECAST_URL_RE,
        json=forecast_with_unknown_code,
        status_code=200,
    )

    result = get_weather_forecast(coord_query)

    assert isinstance(result, WeatherForecastResult)
    assert result.daily[0].weather_description == "Unknown conditions (code 777)"