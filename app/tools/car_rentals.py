"""
search_car_rentals() / get_car_rental_quote() / create_car_rental_booking()
— wraps Duffel's real Cars API.

*** CONTRACT VERIFIED AGAINST REAL DUFFEL DOCS (2026-08) ***
Cars is a THREE-step flow, unlike Flights (search returns bookable offers
directly) or Stays (search -> separate firm-rate fetch):

    1. Search  POST /cars/search   -> returns "rates" (ESTIMATES)
    2. Quote   POST /cars/quotes   -> rate_id -> a FIRM, bookable price
    3. Booking POST /cars/bookings -> quote_id + driver PII -> confirmed booking

Duffel's own docs are explicit: the final price on a quote can differ from
the rate's price, so a quote must be re-fetched and its price shown, never
the original rate's price, before anyone approves anything.

create_car_rental_booking() below is written against the real, verified
/cars/bookings contract but is NOT CALLED ANYWHERE in this codebase. Same
permanent principle as propose_booking(): a real booking is only ever
allowed to happen from a separate, explicitly human-triggered path (Phase
8), never automatically. This function exists so that contract is proven
now rather than guessed later, not so it can be wired up casually.

Set USE_MOCK_DATA=true in .env to return local fixture data instead of
calling the real Duffel sandbox, same as flights.py/hotels.py.

*** SANDBOX TEST DATA ONLY EXISTS AT ONE SPECIFIC COORDINATE ***
Verified live (2026-08): searching a real city (e.g. Atlanta) against the
real Duffel Cars sandbox returns a well-formed, successful response with
ZERO rates — not an error, just no seeded test inventory there. Duffel's
own "Test Hotels" guide documents a fixed coordinate for Stays test data
(-24.38, -128.32); the same coordinate also works for Cars — a live
sandbox call there returns real rates from a location literally named
"Duffel Test Drive Dropoff" on "Henderson Island". Use that coordinate for
any real (non-mock) manual testing; see the __main__ block below.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, cast

import httpx
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from app.config import settings
from app.tools.geocoding import GeocodingAPIError, geocode_city
from app.tools.schemas import (
    CarQuoteInput,
    CarQuoteResult,
    CarRateOption,
    CarRentalPaymentType,
    CarRentalSearchInput,
    CarRentalSearchResult,
    DriverDetails,
    ToolError,
)

_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
_SEARCH_FIXTURES_PATH = Path(__file__).parent / "fixtures" / "car_rental_rates.json"

_CARS_SEARCH_PATH = "/cars/search"
_CARS_QUOTES_PATH = "/cars/quotes"
_CARS_BOOKINGS_PATH = "/cars/bookings"


class DuffelCarsAPIError(Exception):
    def __init__(self, status_code: int, message: str, retryable: bool):
        self.status_code = status_code
        self.message = message
        self.retryable = retryable
        super().__init__(message)


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {settings.duffel_api_key}",
        "Duffel-Version": settings.duffel_api_version,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _is_retryable_cars_error(exc: BaseException) -> bool:
    """Only retry errors explicitly flagged retryable (429/5xx) — never
    retry on 4xx client errors like bad auth or malformed payloads."""
    return isinstance(exc, DuffelCarsAPIError) and exc.retryable


def _location_payload(latitude: float, longitude: float, radius_km: int) -> dict[str, Any]:
    return {
        "geographic_coordinates": {"latitude": latitude, "longitude": longitude},
        "radius": radius_km,
    }


def _build_search_payload(
    query: CarRentalSearchInput,
    pickup_lat: float,
    pickup_lon: float,
    dropoff_lat: float,
    dropoff_lon: float,
) -> dict[str, Any]:
    """Matches Duffel's real POST /cars/search contract: separate date and
    time strings per leg (not a single ISO datetime), and a nested
    driver.age/residence_country_code object."""
    return {
        "data": {
            "pickup_date": query.pickup_at.date().isoformat(),
            "pickup_time": query.pickup_at.strftime("%H:%M"),
            "pickup_location": _location_payload(pickup_lat, pickup_lon, query.radius_km),
            "dropoff_date": query.dropoff_at.date().isoformat(),
            "dropoff_time": query.dropoff_at.strftime("%H:%M"),
            "dropoff_location": _location_payload(dropoff_lat, dropoff_lon, query.radius_km),
            "driver": {
                "age": query.driver_age,
                "residence_country_code": query.driver_country_code,
            },
        }
    }


def _parse_rate(
    raw: dict[str, Any],
    pickup_location_name: str,
    dropoff_location_name: str,
    pickup_at: datetime,
    dropoff_at: datetime,
) -> CarRateOption:
    """Matches Duffel's real Cars rate schema: prices come back as strings
    (total_amount), not numbers — same convention as Duffel's Flights
    offers — and the rate's own id field is just "id", not "rate_id" (that's
    our contract's name for it, not Duffel's)."""
    car = raw.get("car", {})
    supplier = raw.get("supplier", {})
    category = car.get("category", "Car")
    name = car.get("name", "Unknown vehicle")

    return CarRateOption(
        rate_id=raw["id"],
        car_description=f"{category} - {name} or similar",
        supplier_name=supplier.get("name", "Unknown supplier"),
        payment_type=CarRentalPaymentType(raw["payment_type"]),
        estimated_price_total_usd=float(raw["total_amount"]),
        price_currency_original=raw.get("total_currency", "USD"),
        pickup_location_name=pickup_location_name,
        dropoff_location_name=dropoff_location_name,
        pickup_at=pickup_at,
        dropoff_at=dropoff_at,
    )


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=8),
    retry=retry_if_exception(_is_retryable_cars_error),
    reraise=True,
)
def _post_cars(path: str, payload: dict[str, Any]) -> dict[str, Any]:
    with httpx.Client(timeout=15.0) as client:
        resp = client.post(
            f"{settings.duffel_base_url}{path}",
            headers=_headers(),
            json=payload,
        )
    if resp.status_code >= 400:
        retryable = resp.status_code in _RETRYABLE_STATUS_CODES
        message = f"Duffel Cars API returned {resp.status_code}: {resp.text[:300]}"
        raise DuffelCarsAPIError(resp.status_code, message, retryable)
    return cast(dict[str, Any], resp.json())


def _load_mock_rates(location_key: str) -> list[dict[str, Any]]:
    with open(_SEARCH_FIXTURES_PATH) as f:
        fixtures = json.load(f)
    if location_key not in fixtures and "DEFAULT" not in fixtures:
        raise RuntimeError(
            f"No fixture entry for '{location_key}' and no 'DEFAULT' fallback exists in "
            f"{_SEARCH_FIXTURES_PATH.name} — add one or the other."
        )
    return cast(list[dict[str, Any]], fixtures.get(location_key, fixtures.get("DEFAULT", [])))


def _resolve_pickup_and_dropoff(
    query: CarRentalSearchInput,
) -> tuple[float, float, str, float, float, str] | ToolError:
    """Resolves both legs to (lat, lon, display_name). Dropoff defaults to
    the pickup location entirely when omitted — a same-location rental,
    the common case — per CarRentalSearchInput's own docstring."""
    if query.pickup_city is not None:
        try:
            geocoded = geocode_city(query.pickup_city)
        except GeocodingAPIError as e:
            return ToolError(
                tool_name="search_car_rentals",
                error_type="geocoding_api_error",
                message=e.message,
                retryable=e.retryable,
            )
        if geocoded is None:
            return ToolError(
                tool_name="search_car_rentals",
                error_type="location_not_found",
                message=f"Couldn't find a pickup location matching '{query.pickup_city}'.",
                retryable=False,
            )
        pickup_lat, pickup_lon, pickup_name = geocoded
    else:
        assert query.pickup_latitude is not None and query.pickup_longitude is not None
        pickup_lat, pickup_lon = query.pickup_latitude, query.pickup_longitude
        pickup_name = f"{pickup_lat:.4f}, {pickup_lon:.4f}"

    dropoff_omitted = (
        query.dropoff_city is None
        and query.dropoff_latitude is None
        and query.dropoff_longitude is None
    )
    if dropoff_omitted:
        return pickup_lat, pickup_lon, pickup_name, pickup_lat, pickup_lon, pickup_name

    if query.dropoff_city is not None:
        try:
            geocoded = geocode_city(query.dropoff_city)
        except GeocodingAPIError as e:
            return ToolError(
                tool_name="search_car_rentals",
                error_type="geocoding_api_error",
                message=e.message,
                retryable=e.retryable,
            )
        if geocoded is None:
            return ToolError(
                tool_name="search_car_rentals",
                error_type="location_not_found",
                message=f"Couldn't find a dropoff location matching '{query.dropoff_city}'.",
                retryable=False,
            )
        dropoff_lat, dropoff_lon, dropoff_name = geocoded
    else:
        assert query.dropoff_latitude is not None and query.dropoff_longitude is not None
        dropoff_lat, dropoff_lon = query.dropoff_latitude, query.dropoff_longitude
        dropoff_name = f"{dropoff_lat:.4f}, {dropoff_lon:.4f}"

    return pickup_lat, pickup_lon, pickup_name, dropoff_lat, dropoff_lon, dropoff_name


def search_car_rentals(query: CarRentalSearchInput) -> CarRentalSearchResult | ToolError:
    """
    Search for car rental rates near a pickup (and optional dropoff) point.

    Returns CarRentalSearchResult on success, or ToolError on failure — same
    contract as every other tool in app/tools/. IMPORTANT: every returned
    rate is Duffel's own ESTIMATE (see CarRateOption's docstring) — call
    get_car_rental_quote() before treating any price as firm.
    """
    if settings.use_mock_data:
        # Mock mode makes ZERO network calls, including geocoding — same
        # principle as flights.py/hotels.py's mock paths. If a city was
        # given, use it directly as the fixture lookup key rather than
        # resolving it via a real API call; if lat/lon were given, there's
        # no city name to display, so fall back to the coordinates
        # themselves. Dropoff defaults to pickup, same as the real path.
        location_key = query.pickup_city.upper() if query.pickup_city else None
        if query.pickup_city:
            pickup_name = query.pickup_city
        else:
            assert query.pickup_latitude is not None and query.pickup_longitude is not None
            pickup_name = f"{query.pickup_latitude:.4f}, {query.pickup_longitude:.4f}"

        dropoff_omitted = (
            query.dropoff_city is None
            and query.dropoff_latitude is None
            and query.dropoff_longitude is None
        )
        if dropoff_omitted:
            dropoff_name = pickup_name
        elif query.dropoff_city:
            dropoff_name = query.dropoff_city
        else:
            assert query.dropoff_latitude is not None and query.dropoff_longitude is not None
            dropoff_name = f"{query.dropoff_latitude:.4f}, {query.dropoff_longitude:.4f}"

        rates_raw = _load_mock_rates(location_key or "DEFAULT")
        is_mock = True
    else:
        try:
            settings.validate_duffel()
        except RuntimeError as e:
            return ToolError(
                tool_name="search_car_rentals",
                error_type="config_error",
                message=str(e),
                retryable=False,
            )

        resolved = _resolve_pickup_and_dropoff(query)
        if isinstance(resolved, ToolError):
            return resolved
        pickup_lat, pickup_lon, pickup_name, dropoff_lat, dropoff_lon, dropoff_name = resolved

        payload = _build_search_payload(query, pickup_lat, pickup_lon, dropoff_lat, dropoff_lon)
        try:
            raw = _post_cars(_CARS_SEARCH_PATH, payload)
        except DuffelCarsAPIError as e:
            return ToolError(
                tool_name="search_car_rentals",
                error_type="duffel_api_error",
                message=e.message,
                retryable=e.retryable,
            )
        except httpx.TimeoutException:
            return ToolError(
                tool_name="search_car_rentals",
                error_type="timeout",
                message="Duffel Cars API did not respond within 15s",
                retryable=True,
            )
        rates_raw = raw.get("data", {}).get("rates", [])
        is_mock = False

    rates = [
        _parse_rate(r, pickup_name, dropoff_name, query.pickup_at, query.dropoff_at)
        for r in rates_raw
    ]
    rates.sort(key=lambda r: r.estimated_price_total_usd)

    return CarRentalSearchResult(
        query=query,
        resolved_pickup_location_name=pickup_name,
        resolved_dropoff_location_name=dropoff_name,
        rates=rates,
        provider="duffel",
        is_sandbox=True,
        is_mock=is_mock,
    )


def get_car_rental_quote(query: CarQuoteInput) -> CarQuoteResult | ToolError:
    """
    Firm up a rate's price before it can be proposed for booking.

    Duffel's docs are explicit that a quote's price can differ from the
    original rate's price — always call this and use ITS price, never the
    original CarRateOption's estimated_price_total_usd, before presenting
    anything to a human for approval.
    """
    if settings.use_mock_data:
        # Mock mode: look the rate up across every fixture location and
        # return its own price as the "firm" quote — no markup simulated,
        # since the point here is exercising the code path, not pricing
        # realism.
        with open(_SEARCH_FIXTURES_PATH) as f:
            fixtures = json.load(f)
        for rates in fixtures.values():
            for raw in rates:
                if raw["id"] == query.rate_id:
                    return CarQuoteResult(
                        quote_id=f"mock_quote_{query.rate_id}",
                        rate_id=query.rate_id,
                        total_price_usd=float(raw["total_amount"]),
                        price_currency_original=raw.get("total_currency", "USD"),
                        payment_type=CarRentalPaymentType(raw["payment_type"]),
                        expires_at=None,
                    )
        return ToolError(
            tool_name="get_car_rental_quote",
            error_type="rate_not_found",
            message=f"No mock rate found with id '{query.rate_id}'.",
            retryable=False,
        )

    try:
        settings.validate_duffel()
    except RuntimeError as e:
        return ToolError(
            tool_name="get_car_rental_quote",
            error_type="config_error",
            message=str(e),
            retryable=False,
        )

    payload = {"data": {"rate_id": query.rate_id}}
    try:
        raw = _post_cars(_CARS_QUOTES_PATH, payload)
    except DuffelCarsAPIError as e:
        return ToolError(
            tool_name="get_car_rental_quote",
            error_type="duffel_api_error",
            message=e.message,
            retryable=e.retryable,
        )
    except httpx.TimeoutException:
        return ToolError(
            tool_name="get_car_rental_quote",
            error_type="timeout",
            message="Duffel Cars API did not respond within 15s",
            retryable=True,
        )

    data = raw.get("data", {})
    return CarQuoteResult(
        quote_id=data["id"],
        rate_id=query.rate_id,
        total_price_usd=float(data["total_amount"]),
        price_currency_original=data.get("total_currency", "USD"),
        payment_type=CarRentalPaymentType(data["payment_type"]),
        expires_at=data.get("expires_at"),
    )


def create_car_rental_booking(quote_id: str, driver: DriverDetails) -> dict[str, Any] | ToolError:
    """
    Creates a REAL, CONFIRMED car rental booking against Duffel's actual
    /cars/bookings endpoint. THIS ACTUALLY BOOKS SOMETHING FOR REAL.

    NOT CALLED ANYWHERE IN THIS CODEBASE. Written now to prove the real
    contract (verified against Duffel's docs, 2026-08) rather than guess it
    later, but must only ever be invoked from a separate, explicitly
    human-triggered path (Phase 8) — never from propose_booking(), never
    from an agent, never automatically. Adding a call to this function
    anywhere else in the codebase defeats TripWeaver's entire human-
    approval-gate design.
    """
    try:
        settings.validate_duffel()
    except RuntimeError as e:
        return ToolError(
            tool_name="create_car_rental_booking",
            error_type="config_error",
            message=str(e),
            retryable=False,
        )

    payload = {
        "data": {
            "quote_id": quote_id,
            "driver": {
                "given_name": driver.given_name,
                "family_name": driver.family_name,
                "date_of_birth": driver.date_of_birth.isoformat(),
                "email": driver.email,
                "phone_number": driver.phone_number,
            },
        }
    }
    try:
        raw = _post_cars(_CARS_BOOKINGS_PATH, payload)
    except DuffelCarsAPIError as e:
        return ToolError(
            tool_name="create_car_rental_booking",
            error_type="duffel_api_error",
            message=e.message,
            retryable=e.retryable,
        )
    except httpx.TimeoutException:
        return ToolError(
            tool_name="create_car_rental_booking",
            error_type="timeout",
            message="Duffel Cars API did not respond within 15s",
            retryable=True,
        )

    return cast(dict[str, Any], raw.get("data", {}))


if __name__ == "__main__":
    # Manual smoke test: run `uv run python -m app.tools.car_rentals`
    # Add USE_MOCK_DATA=true to .env to test without hitting the real API.
    #
    # Uses Duffel's real Cars sandbox test coordinate (see module docstring)
    # rather than a real city — a real city returns a well-formed, empty
    # result in test mode, since there's no seeded inventory there.
    from datetime import timedelta

    demo_query = CarRentalSearchInput(
        pickup_latitude=-24.38,
        pickup_longitude=-128.32,
        pickup_at=datetime.now() + timedelta(days=30),
        dropoff_at=datetime.now() + timedelta(days=33),
        driver_age=30,
        driver_country_code="US",
        radius_km=10,
    )
    result = search_car_rentals(demo_query)
    if isinstance(result, ToolError):
        print(f"[ERROR] {result.error_type}: {result.message}")
    else:
        print(
            f"Found {len(result.rates)} rates near "
            f"{result.resolved_pickup_location_name} (mock={result.is_mock}):"
        )
        for rate in result.rates[:5]:
            print(
                f"  {rate.rate_id}: {rate.car_description} ({rate.supplier_name}) — "
                f"~${rate.estimated_price_total_usd} [{rate.payment_type}] ESTIMATE"
            )

        if result.rates:
            quote = get_car_rental_quote(CarQuoteInput(rate_id=result.rates[0].rate_id))
            if isinstance(quote, ToolError):
                print(f"\n[QUOTE ERROR] {quote.error_type}: {quote.message}")
            else:
                print(f"\nFirm quote for {result.rates[0].rate_id}: ${quote.total_price_usd}")
