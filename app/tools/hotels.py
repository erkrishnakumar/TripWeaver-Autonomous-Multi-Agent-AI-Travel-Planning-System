"""
search_hotels() — deterministic tool function, no LLM involved.

Wraps Duffel Stays' search endpoint — same account, same auth token, same
`duffel_test_` sandbox pattern as flights.py. Kept as a plain typed Python
function (not a CrewAI @tool decorator), same rationale as flights.py: unit
testable in isolation, reusable by the API layer, wrappable by both CrewAI
and FastMCP without duplicating logic.

*** CONTRACT VERIFIED AGAINST REAL DUFFEL DOCS (2026-08) ***
Endpoint, request payload, and response field names below were checked
against Duffel's own API reference for POST /stays/search and the
Accommodation schema, not guessed. Two things are worth understanding about
the real shape, since they differ from how flights.py's contract works:

1. Duffel Stays is NOT a single "search returns bookable offers" flow like
   Flights. A search returns lightweight SearchResults, each with an
   ESTIMATED price (`cheapest_rate_total_amount`) that Duffel's own docs
   describe as a "best effort computation... not guaranteed to be
   accurate... can change when fetching rates." Getting a firm, bookable
   rate requires a SEPARATE call per result:
     POST /stays/search_results/{id}/actions/fetch_all_rates
   That second call is intentionally NOT made here — doing it for every
   result in a search would be N+1 API calls just to show a list. It
   belongs in its own tool (e.g. get_hotel_rates()), called only once a
   user/agent has picked a specific hotel to actually book.

2. Children need a per-child AGE, not just a count — Duffel's guests list
   takes {"type": "child", "age": N} per child. See ChildGuest in schemas.py.

Set USE_MOCK_DATA=true in .env to return local fixture data instead of
calling the real Duffel sandbox — use this for iteration/demos so you don't
depend on network access or burn real sandbox calls, same as flights.py.

Accepts a city name (geocoded via app.tools.geocoding, same helper
weather.py uses) OR explicit lat/lon, since Duffel Stays search takes
lat/lon + radius, not a city name directly.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import httpx
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from app.config import settings
from app.tools.geocoding import GeocodingAPIError, geocode_city
from app.tools.schemas import (
    HotelGuestDetails,
    HotelListing,
    HotelQuoteResult,
    HotelSearchInput,
    HotelSearchResult,
    ToolError,
)

_STAYS_SEARCH_PATH = "/stays/search"
_STAYS_QUOTES_PATH = "/stays/quotes"
_STAYS_BOOKINGS_PATH = "/stays/bookings"

_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
# See the comment at its use site below (search_hotels()) for why this
# exists -- shared with flights.py/car_rentals.py's identical caps.
_MAX_RESULTS_RETURNED = 10
_FIXTURES_PATH = Path(__file__).parent / "fixtures" / "hotel_offers.json"

_STAYS_SEARCH_PATH = "/stays/search"


class DuffelStaysAPIError(Exception):
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


def _is_retryable_stays_error(exc: BaseException) -> bool:
    """Only retry errors explicitly flagged retryable (429/5xx) — never
    retry on 4xx client errors like bad auth or malformed payloads."""
    return isinstance(exc, DuffelStaysAPIError) and exc.retryable


def _build_stays_search_payload(
    query: HotelSearchInput, latitude: float, longitude: float
) -> dict[str, Any]:
    """Matches Duffel's real POST /stays/search contract:
    https://duffel.com/docs/api/v2/search/search-for-accommodation
    """
    guests: list[dict[str, Any]] = [{"type": "adult"} for _ in range(query.adults)]
    guests += [{"type": "child", "age": child.age} for child in query.children]

    return {
        "data": {
            "rooms": query.rooms,
            "location": {
                "radius": query.radius_km,
                "geographic_coordinates": {"latitude": latitude, "longitude": longitude},
            },
            "check_in_date": str(query.check_in),
            "check_out_date": str(query.check_out),
            "guests": guests,
        }
    }


def _extract_cheapest_rate_id(raw: dict[str, Any]) -> str | None:
    """The cheapest bookable rate's own id, nested at
    accommodation.rooms[].rates[].id in Duffel's real fetch_all_rates
    response -- verified live against the sandbox, not assumed. Only
    present when raw came from fetch_all_rates (get_hotel_rate()); a plain
    search_hotels() result has no "rooms" key at all (too lightweight),
    so this correctly returns None for those, matching HotelListing.
    rate_id's own default."""
    rooms = raw.get("accommodation", {}).get("rooms", [])
    all_rates = [rate for room in rooms for rate in room.get("rates", [])]
    if not all_rates:
        return None
    cheapest = min(all_rates, key=lambda r: float(r["total_amount"]))
    return str(cheapest["id"])


def _parse_search_result(raw: dict[str, Any], nights: int) -> HotelListing:
    """Matches Duffel's real Search Result schema:
    https://duffel.com/docs/api/v2/search-result/schema

    A SearchResult nests a full Accommodation object under "accommodation"
    (name, rating, review_score, location.address, location.geographic_
    coordinates) — see:
    https://duffel.com/docs/api/v2/accommodation/schema
    """
    accommodation = raw.get("accommodation", {})
    location = accommodation.get("location", {})
    coords = location.get("geographic_coordinates", {})
    address = location.get("address", {})

    return HotelListing(
        search_result_id=raw["id"],
        hotel_name=accommodation.get("name", "Unknown hotel"),
        hotel_rating=accommodation.get("rating"),
        review_score=accommodation.get("review_score"),
        address_line=address.get("line_one"),
        city_name=address.get("city_name"),
        latitude=coords.get("latitude", 0.0),
        longitude=coords.get("longitude", 0.0),
        estimated_price_total_usd=float(raw["cheapest_rate_total_amount"]),
        price_currency_original=raw.get("cheapest_rate_currency", "USD"),
        nights=nights,
        expires_at=raw.get("expires_at"),
        rate_id=_extract_cheapest_rate_id(raw),
    )


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=8),
    retry=retry_if_exception(_is_retryable_stays_error),
    reraise=True,
)
def _post_stays_search(payload: dict[str, Any]) -> dict[str, Any]:
    with httpx.Client(timeout=15.0) as client:
        resp = client.post(
            f"{settings.duffel_base_url}{_STAYS_SEARCH_PATH}",
            headers=_headers(),
            json=payload,
        )
    if resp.status_code >= 400:
        retryable = resp.status_code in _RETRYABLE_STATUS_CODES
        message = f"Duffel Stays API returned {resp.status_code}: {resp.text[:300]}"
        raise DuffelStaysAPIError(resp.status_code, message, retryable)
    return cast(dict[str, Any], resp.json())


def _load_mock_results(location_key: str) -> list[dict[str, Any]]:
    with open(_FIXTURES_PATH) as f:
        fixtures = json.load(f)
    if location_key not in fixtures and "DEFAULT" not in fixtures:
        raise RuntimeError(
            f"No fixture entry for '{location_key}' and no 'DEFAULT' fallback exists in "
            f"{_FIXTURES_PATH.name} — add one or the other."
        )
    return cast(list[dict[str, Any]], fixtures.get(location_key, fixtures.get("DEFAULT", [])))


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=8),
    retry=retry_if_exception(_is_retryable_stays_error),
    reraise=True,
)
def _post_fetch_all_rates(search_result_id: str) -> dict[str, Any]:
    with httpx.Client(timeout=15.0) as client:
        resp = client.post(
            f"{settings.duffel_base_url}/stays/search_results/{search_result_id}"
            "/actions/fetch_all_rates",
            headers=_headers(),
        )
    if resp.status_code >= 400:
        retryable = resp.status_code in _RETRYABLE_STATUS_CODES
        message = f"Duffel Stays API returned {resp.status_code}: {resp.text[:300]}"
        raise DuffelStaysAPIError(resp.status_code, message, retryable)
    return cast(dict[str, Any], resp.json())


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=8),
    retry=retry_if_exception(_is_retryable_stays_error),
    reraise=True,
)
def _post_stays_quote(rate_id: str) -> dict[str, Any]:
    with httpx.Client(timeout=15.0) as client:
        resp = client.post(
            f"{settings.duffel_base_url}{_STAYS_QUOTES_PATH}",
            headers=_headers(),
            json={"data": {"rate_id": rate_id}},
        )
    if resp.status_code >= 400:
        retryable = resp.status_code in _RETRYABLE_STATUS_CODES
        message = f"Duffel Stays API returned {resp.status_code}: {resp.text[:300]}"
        raise DuffelStaysAPIError(resp.status_code, message, retryable)
    return cast(dict[str, Any], resp.json())


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=8),
    retry=retry_if_exception(_is_retryable_stays_error),
    reraise=True,
)
def _post_stays_booking(payload: dict[str, Any]) -> dict[str, Any]:
    with httpx.Client(timeout=15.0) as client:
        resp = client.post(
            f"{settings.duffel_base_url}{_STAYS_BOOKINGS_PATH}",
            headers=_headers(),
            json=payload,
        )
    if resp.status_code >= 400:
        retryable = resp.status_code in _RETRYABLE_STATUS_CODES
        message = f"Duffel Stays API returned {resp.status_code}: {resp.text[:300]}"
        raise DuffelStaysAPIError(resp.status_code, message, retryable)
    return cast(dict[str, Any], resp.json())


def get_hotel_rate(search_result_id: str, nights: int) -> HotelListing | ToolError:
    """
    Re-fetch a specific search result by ID (POST /stays/search_results/
    {id}/actions/fetch_all_rates) to confirm it's a REAL result Duffel
    actually returned, and get its current best price, before proposing it
    for booking.

    THIS EXISTS SPECIFICALLY AS A HALLUCINATION GUARD — same rationale as
    get_flight_offer() in app/tools/flights.py: an LLM agent selecting "the
    best listing" from search_hotels() results can fabricate a plausible-
    looking search_result_id instead of copying a real one from its own
    tool results. propose_booking() has no way to tell a real HotelListing
    from an invented one on its own. Calling this first and using ITS
    result (never the caller-supplied HotelListing) means a fabricated
    search_result_id gets a real 404 from Duffel and is rejected before
    anything is ever written to the database.

    The fetch_all_rates response shape (id, cheapest_rate_total_amount/
    currency, accommodation) matches search_hotels()'s own search result
    shape closely enough to reuse _parse_search_result() directly — verified
    against Duffel's real docs, not assumed.

    Mirrors search_hotels()'s mock-mode support: with USE_MOCK_DATA=true,
    looks the result up across every fixture location instead of calling
    the real API.
    """
    if settings.use_mock_data:
        with open(_FIXTURES_PATH) as f:
            fixtures = json.load(f)
        for results in fixtures.values():
            for raw in results:
                if raw["id"] == search_result_id:
                    return _parse_search_result(raw, nights)
        return ToolError(
            tool_name="get_hotel_rate",
            error_type="search_result_not_found",
            message=f"No mock search result found with id '{search_result_id}'.",
            retryable=False,
        )

    try:
        settings.validate_duffel()
    except RuntimeError as e:
        return ToolError(
            tool_name="get_hotel_rate",
            error_type="config_error",
            message=str(e),
            retryable=False,
        )

    try:
        raw = _post_fetch_all_rates(search_result_id)
    except DuffelStaysAPIError as e:
        return ToolError(
            tool_name="get_hotel_rate",
            error_type="duffel_api_error",
            message=e.message,
            retryable=e.retryable,
        )
    except httpx.TimeoutException:
        return ToolError(
            tool_name="get_hotel_rate",
            error_type="timeout",
            message="Duffel Stays API did not respond within 15s",
            retryable=True,
        )

    return _parse_search_result(raw.get("data", raw), nights)


def get_hotel_quote(rate_id: str) -> HotelQuoteResult | ToolError:
    """
    Firms up a hotel rate's price before it can be booked (POST
    /stays/quotes) -- the missing middle step between get_hotel_rate()
    (fetch_all_rates) and a real booking. Verified live against the real
    sandbox (2026-09): Stays turns out to have the SAME 3-step
    search -> quote -> book flow Cars already has, not the 2-step flow
    this module's own docstring originally assumed.

    Always use THIS result's price, never the original rate's estimated
    price, once a specific rate has been chosen -- same principle as
    get_car_rental_quote().
    """
    try:
        settings.validate_duffel()
    except RuntimeError as e:
        return ToolError(
            tool_name="get_hotel_quote",
            error_type="config_error",
            message=str(e),
            retryable=False,
        )

    try:
        raw = _post_stays_quote(rate_id)
    except DuffelStaysAPIError as e:
        return ToolError(
            tool_name="get_hotel_quote",
            error_type="duffel_api_error",
            message=e.message,
            retryable=e.retryable,
        )
    except httpx.TimeoutException:
        return ToolError(
            tool_name="get_hotel_quote",
            error_type="timeout",
            message="Duffel Stays API did not respond within 15s",
            retryable=True,
        )

    data = raw.get("data", raw)
    return HotelQuoteResult(
        quote_id=data["id"],
        rate_id=rate_id,
        total_price_usd=float(data["total_amount"]),
        price_currency_original=data.get("total_currency", "USD"),
        expires_at=data.get("expires_at"),
    )


def create_hotel_booking(
    quote_id: str, email: str, phone_number: str, guests: list[HotelGuestDetails]
) -> dict[str, Any] | ToolError:
    """
    Creates a REAL, CONFIRMED hotel booking against Duffel's actual
    /stays/bookings endpoint. THIS ACTUALLY BOOKS SOMETHING FOR REAL.

    NOT CALLED ANYWHERE IN THIS CODEBASE (yet). Same permanent principle as
    create_car_rental_booking()/create_flight_order(): a real booking is
    only ever allowed to happen from a separate, explicitly human-triggered
    path (Gate 2), never automatically.

    *** CONTRACT VERIFIED AGAINST REAL DUFFEL DOCS (2026-09) ***
    email/phone_number are BOOKING-level (the lead guest's contact info),
    NOT per-guest -- unlike flights, where every passenger needs their own
    contact info. `payment` is deliberately omitted from the payload
    entirely for balance payment -- a real, confirmed asymmetry with
    flights, which requires an explicit `payments` array even for balance.
    """
    try:
        settings.validate_duffel()
    except RuntimeError as e:
        return ToolError(
            tool_name="create_hotel_booking",
            error_type="config_error",
            message=str(e),
            retryable=False,
        )

    payload = {
        "data": {
            "quote_id": quote_id,
            "email": email,
            "phone_number": phone_number,
            "guests": [{"given_name": g.given_name, "family_name": g.family_name} for g in guests],
        }
    }
    try:
        raw = _post_stays_booking(payload)
    except DuffelStaysAPIError as e:
        return ToolError(
            tool_name="create_hotel_booking",
            error_type="duffel_api_error",
            message=e.message,
            retryable=e.retryable,
        )
    except httpx.TimeoutException:
        return ToolError(
            tool_name="create_hotel_booking",
            error_type="timeout",
            message="Duffel Stays API did not respond within 15s",
            retryable=True,
        )

    return cast(dict[str, Any], raw.get("data", raw))


def search_hotels(query: HotelSearchInput) -> HotelSearchResult | ToolError:
    """
    Search for hotels near a city or lat/lon, with an ESTIMATED price per
    listing (see module docstring — this is not a firm bookable rate).

    Returns HotelSearchResult on success, or ToolError on failure so callers
    (agents, API handlers) can branch on outcome without a try/except around
    every call site — same contract as search_flights() and
    get_weather_forecast().

    Behavior depends on settings.use_mock_data:
      - True:  returns local fixture data, no network call, always succeeds
      - False: calls the real Duffel Stays sandbox (requires DUFFEL_API_KEY)
    """
    nights = (query.check_out - query.check_in).days

    if settings.use_mock_data:
        # Mock mode makes ZERO network calls, including geocoding — same
        # principle as flights.py's mock path. If a city was given, use it
        # directly as the fixture lookup key rather than resolving it via a
        # real API call; if lat/lon were given, there's no city name to
        # display, so fall back to the coordinates themselves.
        location_key = query.city.upper() if query.city else None
        resolved_name = query.city if query.city else f"{query.latitude:.4f}, {query.longitude:.4f}"
        results_raw = _load_mock_results(location_key or "DEFAULT")
        listings = [_parse_search_result(r, nights) for r in results_raw]
        is_mock = True
    else:
        try:
            settings.validate_duffel()
        except RuntimeError as e:
            return ToolError(
                tool_name="search_hotels",
                error_type="config_error",
                message=str(e),
                retryable=False,
            )

        try:
            if query.city is not None:
                geocoded = geocode_city(query.city)
                if geocoded is None:
                    return ToolError(
                        tool_name="search_hotels",
                        error_type="location_not_found",
                        message=f"Couldn't find a location matching '{query.city}'.",
                        retryable=False,
                    )
                latitude, longitude, resolved_name = geocoded
            else:
                # Guaranteed non-None here by HotelSearchInput.exactly_one_location.
                assert query.latitude is not None and query.longitude is not None
                latitude, longitude = query.latitude, query.longitude
                resolved_name = f"{latitude:.4f}, {longitude:.4f}"
        except GeocodingAPIError as e:
            return ToolError(
                tool_name="search_hotels",
                error_type="geocoding_api_error",
                message=e.message,
                retryable=e.retryable,
            )
        except httpx.TimeoutException:
            return ToolError(
                tool_name="search_hotels",
                error_type="timeout",
                message="Geocoding API did not respond within 15s",
                retryable=True,
            )

        payload = _build_stays_search_payload(query, latitude, longitude)
        try:
            raw = _post_stays_search(payload)
        except DuffelStaysAPIError as e:
            return ToolError(
                tool_name="search_hotels",
                error_type="duffel_api_error",
                message=e.message,
                retryable=e.retryable,
            )
        except httpx.TimeoutException:
            return ToolError(
                tool_name="search_hotels",
                error_type="timeout",
                message="Duffel Stays API did not respond within 15s",
                retryable=True,
            )

        results_raw = raw.get("data", {}).get("results", [])
        listings = [_parse_search_result(r, nights) for r in results_raw]
        is_mock = False

    if query.max_budget_usd_per_night is not None:
        listings = [
            listing
            for listing in listings
            if (listing.estimated_price_total_usd / max(nights, 1))
            <= query.max_budget_usd_per_night
        ]

    listings.sort(key=lambda listing: listing.estimated_price_total_usd)
    # Cap what's handed back to the agent -- verified live: an uncapped
    # Duffel Stays search can return 50+ listings, and feeding all of them
    # back into a hosted LLM's next completion call is, by itself, enough to
    # blow a free-tier per-minute token budget (Groq's 8000 TPM, hit
    # instantly by a single 52-listing result). Already sorted cheapest
    # first, so keeping the top N keeps the most relevant options anyway.
    listings = listings[:_MAX_RESULTS_RETURNED]

    return HotelSearchResult(
        query=query,
        resolved_location_name=resolved_name,
        listings=listings,
        provider="duffel",
        is_sandbox=True,
        is_mock=is_mock,
    )


if __name__ == "__main__":
    # Manual smoke test: run `uv run python -m app.tools.hotels`
    from datetime import date, timedelta

    demo_query = HotelSearchInput(
        city="Atlanta",
        check_in=date.today() + timedelta(days=30),
        check_out=date.today() + timedelta(days=33),
        adults=2,
    )
    result = search_hotels(demo_query)
    if isinstance(result, ToolError):
        print(f"[ERROR] {result.error_type}: {result.message}")
    else:
        print(
            f"Found {len(result.listings)} listings near "
            f"{result.resolved_location_name} (mock={result.is_mock}):"
        )
        for listing in result.listings[:5]:
            print(
                f"  {listing.hotel_name}: ~${listing.estimated_price_total_usd} "
                f"for {listing.nights} night(s) [ESTIMATE]"
            )
