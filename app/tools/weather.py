"""
get_weather_forecast() — deterministic tool function, no LLM involved.

Wraps Open-Meteo's free forecast API (no API key required). Geocoding a
city name to lat/lon is delegated to app.tools.geocoding, shared with
hotels.py so there's only one geocoding code path in the whole app.

Kept as a plain typed Python function, same shape as search_flights(), so
it can be reused directly by the API layer and wrapped separately by both
CrewAI and FastMCP without duplicating logic.

Open-Meteo's real forecast endpoint only covers roughly the next 16 days —
but most real trips are booked well beyond that (this is the COMMON case,
not an edge case). Rather than just returning an honest-but-useless
ToolError for every such trip, dates beyond that horizon fall back to
Open-Meteo's free ARCHIVE (historical) API: the last _CLIMATE_AVERAGE_
YEARS_BACK years of actual observed weather on that same calendar day are
averaged into a clearly-labeled "typical conditions for this time of year"
result (WeatherForecastResult.is_climate_average=True, with a mandatory
disclaimer) instead of a real forecast. This is honest climatology, not a
prediction — see _build_climate_average_result()'s docstring.

No USE_MOCK_DATA support here (unlike flights.py) — Open-Meteo needs no key
and has no meaningful rate limit for this app's scale, so there's no
reliability upside to mocking it, only fixture upkeep for no benefit.
"""

from __future__ import annotations

from collections import Counter
from datetime import date, timedelta
from typing import Any, cast

import httpx
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from app.tools.geocoding import GeocodingAPIError, geocode_city
from app.tools.schemas import DailyForecast, ToolError, WeatherForecastResult, WeatherSearchInput

_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
_ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"

_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}

# Open-Meteo's forecast horizon. Padded slightly under the real ~16-day max
# so we fail clearly instead of riding the exact edge of what the API allows.
_MAX_FORECAST_DAYS_OUT = 15

# How many past full calendar years to average over for the climate-average
# fallback below. 10 is a real, verified-live tradeoff: enough samples for a
# meaningful "typical for this time of year" average (and always includes at
# least 2 leap years, so a Feb 29 request isn't starved of samples), while
# keeping the single archive API call's response small (~110KB, ~2.5s
# verified live for a 10-year daily pull at one location).
_CLIMATE_AVERAGE_YEARS_BACK = 10

# A day counts as "rainy" for the historical rainy-day-fraction below at this
# measured precipitation threshold (mm) -- filters out trace/measurement
# noise, not a meaningful rain event.
_RAINY_DAY_THRESHOLD_MM = 0.1

_WEATHER_CODE_DESCRIPTIONS: dict[int, str] = {
    0: "Clear sky",
    1: "Mainly clear",
    2: "Partly cloudy",
    3: "Overcast",
    45: "Fog",
    48: "Depositing rime fog",
    51: "Light drizzle",
    53: "Moderate drizzle",
    55: "Dense drizzle",
    61: "Slight rain",
    63: "Moderate rain",
    65: "Heavy rain",
    71: "Slight snow",
    73: "Moderate snow",
    75: "Heavy snow",
    80: "Slight rain showers",
    81: "Moderate rain showers",
    82: "Violent rain showers",
    95: "Thunderstorm",
    96: "Thunderstorm with slight hail",
    99: "Thunderstorm with heavy hail",
}


class OpenMeteoAPIError(Exception):
    def __init__(self, status_code: int, message: str, retryable: bool):
        self.status_code = status_code
        self.message = message
        self.retryable = retryable
        super().__init__(message)


def _is_retryable_open_meteo_error(exc: BaseException) -> bool:
    """Only retry errors explicitly flagged retryable (429/5xx) — never
    retry on 4xx client errors like a malformed query."""
    return isinstance(exc, OpenMeteoAPIError) and exc.retryable


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=8),
    retry=retry_if_exception(_is_retryable_open_meteo_error),
    reraise=True,
)
def _get(url: str, params: dict[str, Any]) -> dict[str, Any]:
    with httpx.Client(timeout=15.0) as client:
        resp = client.get(url, params=params)
    if resp.status_code >= 400:
        retryable = resp.status_code in _RETRYABLE_STATUS_CODES
        message = f"Open-Meteo API returned {resp.status_code}: {resp.text[:300]}"
        raise OpenMeteoAPIError(resp.status_code, message, retryable)
    return cast(dict[str, Any], resp.json())


def _weather_description(code: int) -> str:
    return _WEATHER_CODE_DESCRIPTIONS.get(code, f"Unknown conditions (code {code})")


def _parse_daily_forecast(raw_daily: dict[str, Any]) -> list[DailyForecast]:
    days: list[DailyForecast] = []
    dates = raw_daily.get("time", [])
    for i, day_str in enumerate(dates):
        precip = raw_daily.get("precipitation_probability_max", [None] * len(dates))[i]
        days.append(
            DailyForecast(
                date=day_str,
                temp_max_c=raw_daily["temperature_2m_max"][i],
                temp_min_c=raw_daily["temperature_2m_min"][i],
                precipitation_probability_pct=precip,
                weather_code=raw_daily["weather_code"][i],
                weather_description=_weather_description(raw_daily["weather_code"][i]),
            )
        )
    return days


def _climate_average_for_day(
    reference_daily: dict[str, Any], target_date: date
) -> DailyForecast | None:
    """Average every historical entry in reference_daily (a multi-year
    archive-API response) that falls on the SAME calendar month/day as
    target_date, across whatever years the archive call covered. Returns
    None if the archive genuinely has no matching day (e.g. a request that
    somehow slipped through with no data at all) rather than crashing on
    an empty sample.
    """
    dates = reference_daily.get("time", [])
    max_temps: list[float] = []
    min_temps: list[float] = []
    precip_amounts: list[float] = []
    codes: list[int] = []

    for i, day_str in enumerate(dates):
        _year_str, month_str, day_num_str = day_str.split("-")
        if int(month_str) != target_date.month or int(day_num_str) != target_date.day:
            continue
        max_temp = reference_daily["temperature_2m_max"][i]
        min_temp = reference_daily["temperature_2m_min"][i]
        code = reference_daily["weather_code"][i]
        if max_temp is None or min_temp is None or code is None:
            continue  # a real gap in the archive for that specific year -- skip it
        max_temps.append(max_temp)
        min_temps.append(min_temp)
        precip = reference_daily["precipitation_sum"][i]
        precip_amounts.append(precip if precip is not None else 0.0)
        codes.append(code)

    if not max_temps:
        return None

    rainy_fraction = sum(1 for p in precip_amounts if p > _RAINY_DAY_THRESHOLD_MM) / len(
        precip_amounts
    )
    typical_code = Counter(codes).most_common(1)[0][0]

    return DailyForecast(
        date=target_date,
        temp_max_c=round(sum(max_temps) / len(max_temps), 1),
        temp_min_c=round(sum(min_temps) / len(min_temps), 1),
        precipitation_probability_pct=round(rainy_fraction * 100),
        weather_code=typical_code,
        weather_description=_weather_description(typical_code),
    )


def _build_climate_average_result(
    query: WeatherSearchInput, latitude: float, longitude: float, resolved_name: str
) -> WeatherForecastResult | ToolError:
    """Fallback for trip dates beyond Open-Meteo's real forecast horizon —
    see the module docstring for why this exists at all instead of just
    erroring out. Fetches the last _CLIMATE_AVERAGE_YEARS_BACK full
    calendar years of ACTUAL observed weather at this location from
    Open-Meteo's free archive API in one call, then averages whichever
    historical entries land on the same calendar day as each requested
    date. This is honest climatology (a real average of real past
    measurements), not a prediction — WeatherForecastResult.is_climate_
    average and .disclaimer exist specifically so no caller can present
    this as an actual forecast by mistake.
    """
    today = date.today()
    reference_end_year = today.year - 1
    reference_start_year = reference_end_year - _CLIMATE_AVERAGE_YEARS_BACK + 1

    try:
        reference_raw = _get(
            _ARCHIVE_URL,
            {
                "latitude": latitude,
                "longitude": longitude,
                "daily": "temperature_2m_max,temperature_2m_min,precipitation_sum,weather_code",
                "timezone": "auto",
                "start_date": date(reference_start_year, 1, 1).isoformat(),
                "end_date": date(reference_end_year, 12, 31).isoformat(),
            },
        )
    except OpenMeteoAPIError as e:
        return ToolError(
            tool_name="get_weather_forecast",
            error_type="open_meteo_api_error",
            message=e.message,
            retryable=e.retryable,
        )
    except httpx.TimeoutException:
        return ToolError(
            tool_name="get_weather_forecast",
            error_type="timeout",
            message="Open-Meteo archive API did not respond within 15s",
            retryable=True,
        )

    reference_daily = reference_raw.get("daily", {})
    daily: list[DailyForecast] = []
    current = query.start_date
    while current <= query.end_date:
        day_average = _climate_average_for_day(reference_daily, current)
        if day_average is not None:
            daily.append(day_average)
        current += timedelta(days=1)

    if not daily:
        return ToolError(
            tool_name="get_weather_forecast",
            error_type="climate_average_unavailable",
            message=f"No historical weather data available for {resolved_name} on these dates.",
            retryable=False,
        )

    return WeatherForecastResult(
        resolved_location_name=resolved_name,
        latitude=latitude,
        longitude=longitude,
        daily=daily,
        is_climate_average=True,
        disclaimer=(
            f"This is NOT a real weather forecast — {query.end_date.isoformat()} is more "
            f"than {_MAX_FORECAST_DAYS_OUT} days out, beyond what any real forecast can "
            f"predict. These numbers are historical averages for this time of year, "
            f"computed from the last {_CLIMATE_AVERAGE_YEARS_BACK} years of actual "
            f"observed weather at this location — actual conditions on the real trip "
            f"dates may differ."
        ),
    )


def get_weather_forecast(query: WeatherSearchInput) -> WeatherForecastResult | ToolError:
    """
    Get a daily weather forecast for a city or explicit lat/lon, across a
    date range.

    Returns WeatherForecastResult on success. Dates beyond Open-Meteo's real
    ~16-day forecast horizon fall back to a clearly-labeled historical
    climate average instead of erroring out — see
    _build_climate_average_result()'s docstring and
    WeatherForecastResult.is_climate_average. Still returns ToolError for a
    genuine failure (unknown location, provider error, timeout).
    """
    try:
        if query.city is not None:
            geocoded = geocode_city(query.city)
            if geocoded is None:
                return ToolError(
                    tool_name="get_weather_forecast",
                    error_type="location_not_found",
                    message=f"Couldn't find a location matching '{query.city}'.",
                    retryable=False,
                )
            latitude, longitude, resolved_name = geocoded
        else:
            # Guaranteed non-None here by WeatherSearchInput.exactly_one_location.
            assert query.latitude is not None and query.longitude is not None
            latitude, longitude = query.latitude, query.longitude
            resolved_name = f"{latitude:.4f}, {longitude:.4f}"
    except GeocodingAPIError as e:
        return ToolError(
            tool_name="get_weather_forecast",
            error_type="geocoding_api_error",
            message=e.message,
            retryable=e.retryable,
        )

    today = date.today()
    furthest_out = (query.end_date - today).days
    if furthest_out > _MAX_FORECAST_DAYS_OUT:
        return _build_climate_average_result(query, latitude, longitude, resolved_name)

    try:
        forecast_raw = _get(
            _FORECAST_URL,
            {
                "latitude": latitude,
                "longitude": longitude,
                "daily": "temperature_2m_max,temperature_2m_min,"
                "precipitation_probability_max,weather_code",
                "timezone": "auto",
                "start_date": str(query.start_date),
                "end_date": str(query.end_date),
            },
        )
    except OpenMeteoAPIError as e:
        return ToolError(
            tool_name="get_weather_forecast",
            error_type="open_meteo_api_error",
            message=e.message,
            retryable=e.retryable,
        )
    except httpx.TimeoutException:
        return ToolError(
            tool_name="get_weather_forecast",
            error_type="timeout",
            message="Open-Meteo API did not respond within 15s",
            retryable=True,
        )

    daily = _parse_daily_forecast(forecast_raw.get("daily", {}))

    return WeatherForecastResult(
        resolved_location_name=resolved_name,
        latitude=latitude,
        longitude=longitude,
        daily=daily,
    )


if __name__ == "__main__":
    # Manual smoke test: run `uv run python -m app.tools.weather`
    demo_query = WeatherSearchInput(
        city="Atlanta",
        start_date=date.today() + timedelta(days=2),
        end_date=date.today() + timedelta(days=5),
    )
    result = get_weather_forecast(demo_query)
    if isinstance(result, ToolError):
        print(f"[ERROR] {result.error_type}: {result.message}")
    else:
        print(
            f"Forecast for {result.resolved_location_name}"
            f"({result.latitude:.2f}, {result.longitude:.2f}):"
        )
        for day in result.daily:
            print(
                f"  {day.date}: {day.temp_min_c}–{day.temp_max_c}°C, "
                f"{day.weather_description} (precip {day.precipitation_probability_pct}%)"
            )
