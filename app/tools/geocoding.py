"""
Shared city-name geocoding, used by any tool that accepts a city name but
needs lat/lon to call its underlying provider (weather.py, hotels.py, and
presumably future location-based tools).

Wraps Open-Meteo's free geocoding API (no API key required) — same provider
family as get_weather_forecast(), so the whole app has exactly one
geocoding code path instead of one per tool.
"""

from __future__ import annotations

import httpx
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

_GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"

_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}


class GeocodingAPIError(Exception):
    def __init__(self, status_code: int, message: str, retryable: bool):
        self.status_code = status_code
        self.message = message
        self.retryable = retryable
        super().__init__(message)


def _is_retryable_geocoding_error(exc: BaseException) -> bool:
    """Only retry errors explicitly flagged retryable (429/5xx) — never
    retry on 4xx client errors like a malformed query."""
    return isinstance(exc, GeocodingAPIError) and exc.retryable


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=8),
    retry=retry_if_exception(_is_retryable_geocoding_error),
    reraise=True,
)
def geocode_city(city: str) -> tuple[float, float, str] | None:
    """Resolve a city name to (latitude, longitude, resolved_display_name).

    Returns None if no match was found — this is a normal, expected outcome
    (typo, obscure place name) and callers should turn it into a ToolError
    with error_type="location_not_found" rather than treating it as a crash.

    Raises GeocodingAPIError for actual API failures (network, 4xx/5xx) —
    callers should catch this and convert it to their own ToolError, same
    pattern as DuffelAPIError / OpenMeteoAPIError elsewhere in app/tools/.
    """
    with httpx.Client(timeout=15.0) as client:
        resp = client.get(
            _GEOCODING_URL,
            params={"name": city, "count": 1, "language": "en", "format": "json"},
        )
    if resp.status_code >= 400:
        retryable = resp.status_code in _RETRYABLE_STATUS_CODES
        message = f"Open-Meteo geocoding API returned {resp.status_code}: {resp.text[:300]}"
        raise GeocodingAPIError(resp.status_code, message, retryable)

    data = resp.json()
    results = data.get("results") or []
    if not results:
        return None

    top = results[0]
    resolved_name_parts = [top.get("name")]
    if top.get("admin1"):
        resolved_name_parts.append(top["admin1"])
    if top.get("country"):
        resolved_name_parts.append(top["country"])
    resolved_name = ", ".join(p for p in resolved_name_parts if p)

    return top["latitude"], top["longitude"], resolved_name
