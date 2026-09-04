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
ARCHIVE_URL_RE = re.compile(r"^https://archive-api\.open-meteo\.com/v1/archive(\?.*)?$")


def _archive_response_for_month_day(
    month: int,
    day: int,
    *,
    max_c: float,
    min_c: float,
    precip_mm: float,
    code: int,
    years: int = 10,
) -> dict:
    """Builds a fake multi-year archive-API response with the SAME
    observed values repeated on the given month/day across `years`
    consecutive past years -- enough for _climate_average_for_day() to
    find every sample and average them (trivially, since they're all
    identical here; TestClimateAverageVariesAcrossYears below covers a
    genuinely varying sample)."""
    end_year = date.today().year - 1
    start_year = end_year - years + 1
    time_, max_t, min_t, precip, codes = [], [], [], [], []
    for year in range(start_year, end_year + 1):
        time_.append(f"{year:04d}-{month:02d}-{day:02d}")
        max_t.append(max_c)
        min_t.append(min_c)
        precip.append(precip_mm)
        codes.append(code)
    return {
        "daily": {
            "time": time_,
            "temperature_2m_max": max_t,
            "temperature_2m_min": min_t,
            "precipitation_sum": precip,
            "weather_code": codes,
        }
    }


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


class TestClimateAverageFallback:
    """Dates beyond the ~15-day real forecast horizon fall back to
    historical climate averages (app/tools/weather.py's
    _build_climate_average_result()) instead of just erroring out — this
    is the COMMON case for real trip bookings, not an edge case."""

    def test_beyond_horizon_returns_climate_average_not_an_error(self, httpx_mock, city_query):
        target = date.today() + timedelta(days=40)
        query = WeatherSearchInput(city="Atlanta", start_date=target, end_date=target)
        httpx_mock.add_response(url=GEOCODE_URL_RE, json=MOCK_GEOCODE_RESPONSE, status_code=200)
        httpx_mock.add_response(
            url=ARCHIVE_URL_RE,
            json=_archive_response_for_month_day(
                target.month, target.day, max_c=28.0, min_c=18.0, precip_mm=0.0, code=1
            ),
            status_code=200,
        )

        result = get_weather_forecast(query)

        assert isinstance(result, WeatherForecastResult)
        assert result.is_climate_average is True
        assert result.disclaimer is not None
        assert "NOT a real weather forecast" in result.disclaimer
        assert len(result.daily) == 1
        assert result.daily[0].date == target
        assert result.daily[0].temp_max_c == 28.0
        assert result.daily[0].temp_min_c == 18.0
        assert result.daily[0].precipitation_probability_pct == 0
        assert result.daily[0].weather_description == "Mainly clear"

    def test_climate_average_covers_the_whole_requested_range(self, httpx_mock):
        start = date.today() + timedelta(days=40)
        end = start + timedelta(days=2)
        query = WeatherSearchInput(
            latitude=33.749, longitude=-84.388, start_date=start, end_date=end
        )
        # Build one archive response covering all three target month/days.
        base = _archive_response_for_month_day(
            start.month, start.day, max_c=20.0, min_c=10.0, precip_mm=0.0, code=0
        )
        for offset in (1, 2):
            extra_day = start + timedelta(days=offset)
            extra = _archive_response_for_month_day(
                extra_day.month, extra_day.day, max_c=20.0, min_c=10.0, precip_mm=0.0, code=0
            )
            base["daily"]["time"] += extra["daily"]["time"]
            base["daily"]["temperature_2m_max"] += extra["daily"]["temperature_2m_max"]
            base["daily"]["temperature_2m_min"] += extra["daily"]["temperature_2m_min"]
            base["daily"]["precipitation_sum"] += extra["daily"]["precipitation_sum"]
            base["daily"]["weather_code"] += extra["daily"]["weather_code"]
        httpx_mock.add_response(url=ARCHIVE_URL_RE, json=base, status_code=200)

        result = get_weather_forecast(query)

        assert isinstance(result, WeatherForecastResult)
        assert [d.date for d in result.daily] == [start, start + timedelta(days=1), end]

    def test_rainy_fraction_reflects_how_many_sampled_years_had_rain(self, httpx_mock, coord_query):
        target = date.today() + timedelta(days=40)
        query = WeatherSearchInput(
            latitude=33.749, longitude=-84.388, start_date=target, end_date=target
        )
        response = _archive_response_for_month_day(
            target.month, target.day, max_c=25.0, min_c=15.0, precip_mm=0.0, code=1, years=10
        )
        # Half the sampled years had measurable rain, half didn't.
        for i in range(0, 10, 2):
            response["daily"]["precipitation_sum"][i] = 5.0
        httpx_mock.add_response(url=ARCHIVE_URL_RE, json=response, status_code=200)

        result = get_weather_forecast(query)

        assert isinstance(result, WeatherForecastResult)
        assert result.daily[0].precipitation_probability_pct == 50

    def test_archive_api_error_returns_tool_error(self, httpx_mock, coord_query):
        target = date.today() + timedelta(days=40)
        query = WeatherSearchInput(
            latitude=33.749, longitude=-84.388, start_date=target, end_date=target
        )
        httpx_mock.add_response(
            url=ARCHIVE_URL_RE, status_code=400, json={"reason": "invalid parameter"}
        )

        result = get_weather_forecast(query)

        assert isinstance(result, ToolError)
        assert result.tool_name == "get_weather_forecast"
        assert result.error_type == "open_meteo_api_error"
        assert result.retryable is False


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
