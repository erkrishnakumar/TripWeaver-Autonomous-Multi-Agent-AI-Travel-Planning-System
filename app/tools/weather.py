"""
get_weather_forecast() — deterministic tool function, no LLM involved.

Wraps Open-Meteo's free forecast API (no API key required). Geocoding a
city name to lat/lon is delegated to app.tools.geocoding, shared with
hotels.py so there's only one geocoding code path in the whole app.

Kept as a plain typed Python function, same shape as search_flights(), so
it can be reused directly by the API layer and wrapped separately by both
CrewAI and FastMCP without duplicating logic.

Open-Meteo's forecast endpoint only covers roughly the next 16 days. Trip
dates further out than that return a ToolError with a clear, non-crashing
message rather than an empty or misleading result — an agent can relay
that to the user instead of the call blowing up.

No USE_MOCK_DATA support here (unlike flights.py) — Open-Meteo needs no key
and has no meaningful rate limit for this app's scale, so there's no
reliability upside to mocking it, only fixture upkeep for no benefit.
"""

from __future__ import annotations

from datetime import date, timedelta

import httpx
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from app.tools.geocoding import GeocodingAPIError, geocode_city
from app.tools.schemas import DailyForecast, ToolError, WeatherForecastResult, WeatherSearchInput

_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}

# Open-Meteo's forecast horizon. Padded slightly under the real ~16-day max
# so we fail clearly instead of riding the exact edge of what the API allows.
_MAX_FORECAST_DAYS_OUT = 15

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
def _get(url: str, params: dict) -> dict:
    with httpx.Client(timeout=15.0) as client:
        resp = client.get(url, params=params)
    if resp.status_code >= 400:
        retryable = resp.status_code in _RETRYABLE_STATUS_CODES
        message = f"Open-Meteo API returned {resp.status_code}: {resp.text[:300]}"
        raise OpenMeteoAPIError(resp.status_code, message, retryable)
    return resp.json()


def _weather_description(code: int) -> str:
    return _WEATHER_CODE_DESCRIPTIONS.get(code, f"Unknown conditions (code {code})")


def _parse_daily_forecast(raw_daily: dict) -> list[DailyForecast]:
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


def get_weather_forecast(query: WeatherSearchInput) -> WeatherForecastResult | ToolError:
    """
    Get a daily weather forecast for a city or explicit lat/lon, across a
    date range.

    Returns WeatherForecastResult on success, or ToolError on failure —
    including when the requested dates are beyond Open-Meteo's ~16-day
    forecast horizon — so callers can branch on outcome without a
    try/except around every call site.
    """
    today = date.today()
    furthest_out = (query.end_date - today).days
    if furthest_out > _MAX_FORECAST_DAYS_OUT:
        return ToolError(
            tool_name="get_weather_forecast",
            error_type="forecast_range_exceeded",
            message=(
                f"Weather forecasts are only available up to about "
                f"{_MAX_FORECAST_DAYS_OUT} days out. {query.end_date.isoformat()} is "
                f"{furthest_out} days away, so I can't get a forecast for it yet — "
                f"try again closer to the trip date."
            ),
            retryable=False,
        )

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
            latitude, longitude = query.latitude, query.longitude
            resolved_name = f"{latitude:.4f}, {longitude:.4f}"

        forecast_raw = _get(
            _FORECAST_URL,
            {
                "latitude": latitude,
                "longitude": longitude,
                "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max,weather_code",
                "timezone": "auto",
                "start_date": str(query.start_date),
                "end_date": str(query.end_date),
            },
        )
    except GeocodingAPIError as e:
        return ToolError(
            tool_name="get_weather_forecast",
            error_type="geocoding_api_error",
            message=e.message,
            retryable=e.retryable,
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
        print(f"Forecast for {result.resolved_location_name} ({result.latitude:.2f}, {result.longitude:.2f}):")
        for day in result.daily:
            print(
                f"  {day.date}: {day.temp_min_c}–{day.temp_max_c}°C, "
                f"{day.weather_description} (precip {day.precipitation_probability_pct}%)"
            )