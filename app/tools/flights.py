"""
search_flights() — deterministic tool function, no LLM involved.

Wraps the Duffel Offer Request API. Kept as a plain typed Python function
(not a CrewAI @tool decorator) so it can be:
  - unit tested in isolation (see tests/test_flights.py)
  - reused directly by the API layer
  - wrapped separately by both CrewAI and FastMCP without duplicating logic

Set USE_MOCK_DATA=true in .env to return local fixture data instead of
calling the real Duffel sandbox — use this for iteration/demos so you don't
depend on network access or burn real sandbox calls.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import httpx
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from app.config import settings
from app.tools.schemas import (
    CabinClass,
    FlightOffer,
    FlightSearchInput,
    FlightSearchResult,
    FlightSegment,
    ToolError,
)

_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
# See the comment at its use site below (search_flights()) for why this
# exists -- shared with hotels.py/car_rentals.py's identical caps.
_MAX_RESULTS_RETURNED = 10
_FIXTURES_PATH = Path(__file__).parent / "fixtures" / "flight_offers.json"


class DuffelAPIError(Exception):
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


def _build_offer_request_payload(query: FlightSearchInput) -> dict[str, Any]:
    slices = [
        {
            "origin": query.origin,
            "destination": query.destination,
            "departure_date": str(query.depart_date),
        }
    ]
    if query.return_date:
        slices.append(
            {
                "origin": query.destination,
                "destination": query.origin,
                "departure_date": str(query.return_date),
            }
        )
    return {
        "data": {
            "slices": slices,
            "passengers": [{"type": "adult"} for _ in range(query.adults)],
            "cabin_class": query.cabin_class.value,
        }
    }


def _extract_cabin_class(raw: dict[str, Any]) -> CabinClass:
    """Cabin class lives per-passenger, per-segment in Duffel's real Offer
    schema (slices[].segments[].passengers[].cabin_class) — verified
    against Duffel's own Offers schema docs. There is no top-level
    cabin_class field on an offer; the previous code guessed one existed
    and silently defaulted every offer to "economy" as a result.

    Multiple passengers/segments could technically have different cabin
    classes on a mixed-cabin itinerary; we take the first outbound
    segment's first passenger as representative, which matches what
    search_flights() actually requested (a single cabin_class for the
    whole search)."""
    slices = raw.get("slices", [])
    if not slices:
        return CabinClass.ECONOMY
    segments = slices[0].get("segments", [])
    if not segments:
        return CabinClass.ECONOMY
    passengers = segments[0].get("passengers", [])
    if not passengers:
        return CabinClass.ECONOMY
    return CabinClass(passengers[0].get("cabin_class", "economy"))


def _parse_offer(raw: dict[str, Any]) -> FlightOffer:
    segments: list[FlightSegment] = []
    stops_outbound = 0
    for i, sl in enumerate(raw.get("slices", [])):
        segs = sl.get("segments", [])
        if i == 0:
            stops_outbound = max(0, len(segs) - 1)
        for seg in segs:
            segments.append(
                FlightSegment(
                    carrier_iata=seg["marketing_carrier"]["iata_code"],
                    carrier_name=seg["marketing_carrier"]["name"],
                    flight_number=seg["marketing_carrier_flight_number"],
                    origin_iata=seg["origin"]["iata_code"],
                    destination_iata=seg["destination"]["iata_code"],
                    departs_at=seg["departing_at"],
                    arrives_at=seg["arriving_at"],
                )
            )
    return FlightOffer(
        offer_id=raw["id"],
        total_price_usd=float(raw["total_amount"]),
        price_currency_original=raw.get("total_currency", "USD"),
        cabin_class=_extract_cabin_class(raw),
        stops_outbound=stops_outbound,
        segments=segments,
        expires_at=raw.get("expires_at"),
    )


def _is_retryable_duffel_error(exc: BaseException) -> bool:
    """Only retry Duffel errors explicitly flagged retryable (429/5xx) —
    never retry on 4xx client errors like bad auth or malformed payloads."""
    return isinstance(exc, DuffelAPIError) and exc.retryable


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=8),
    retry=retry_if_exception(_is_retryable_duffel_error),
    reraise=True,
)
def _post_offer_request(payload: dict[str, Any]) -> dict[str, Any]:
    with httpx.Client(timeout=15.0) as client:
        resp = client.post(
            f"{settings.duffel_base_url}/air/offer_requests",
            headers=_headers(),
            params={"return_offers": "true"},
            json=payload,
        )
    if resp.status_code >= 400:
        retryable = resp.status_code in _RETRYABLE_STATUS_CODES
        message = f"Duffel API returned {resp.status_code}: {resp.text[:300]}"
        raise DuffelAPIError(resp.status_code, message, retryable)
    return cast(dict[str, Any], resp.json())


def _load_mock_offers(query: FlightSearchInput) -> list[dict[str, Any]]:
    with open(_FIXTURES_PATH) as f:
        fixtures = json.load(f)
    key = f"{query.origin}-{query.destination}"
    return cast(list[dict[str, Any]], fixtures.get(key, fixtures["DEFAULT"]))


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=8),
    retry=retry_if_exception(_is_retryable_duffel_error),
    reraise=True,
)
def _get_offer(offer_id: str) -> dict[str, Any]:
    with httpx.Client(timeout=15.0) as client:
        resp = client.get(
            f"{settings.duffel_base_url}/air/offers/{offer_id}",
            headers=_headers(),
        )
    if resp.status_code >= 400:
        retryable = resp.status_code in _RETRYABLE_STATUS_CODES
        message = f"Duffel API returned {resp.status_code}: {resp.text[:300]}"
        raise DuffelAPIError(resp.status_code, message, retryable)
    return cast(dict[str, Any], resp.json())


def get_flight_offer(offer_id: str) -> FlightOffer | ToolError:
    """
    Re-fetch a specific offer by ID (GET /air/offers/{id}) to confirm it's a
    REAL offer Duffel actually issued, and get its current price, before
    proposing it for booking.

    THIS EXISTS SPECIFICALLY AS A HALLUCINATION GUARD: an LLM agent
    selecting "the best offer" from search_flights() results can, and in
    practice does, occasionally fabricate a plausible-looking offer_id
    instead of copying a real one from its own tool results. propose_
    booking() has no way to tell a real FlightOffer from an invented one
    on its own -- it just persists whatever object it's handed. Calling
    this first and using ITS result (never the caller-supplied FlightOffer)
    means a fabricated offer_id gets a real 404 from Duffel and is rejected
    before anything is ever written to the database, the same protection
    car rentals already had via their mandatory quote re-fetch step.

    Mirrors search_flights()'s mock-mode support: with USE_MOCK_DATA=true,
    looks the offer up across every fixture location instead of calling
    the real API.
    """
    if settings.use_mock_data:
        with open(_FIXTURES_PATH) as f:
            fixtures = json.load(f)
        for offers in fixtures.values():
            for raw in offers:
                if raw["id"] == offer_id:
                    return _parse_offer(raw)
        return ToolError(
            tool_name="get_flight_offer",
            error_type="offer_not_found",
            message=f"No mock offer found with id '{offer_id}'.",
            retryable=False,
        )

    try:
        settings.validate_duffel()
    except RuntimeError as e:
        return ToolError(
            tool_name="get_flight_offer",
            error_type="config_error",
            message=str(e),
            retryable=False,
        )

    try:
        raw = _get_offer(offer_id)
    except DuffelAPIError as e:
        return ToolError(
            tool_name="get_flight_offer",
            error_type="duffel_api_error",
            message=e.message,
            retryable=e.retryable,
        )
    except httpx.TimeoutException:
        return ToolError(
            tool_name="get_flight_offer",
            error_type="timeout",
            message="Duffel API did not respond within 15s",
            retryable=True,
        )

    return _parse_offer(raw.get("data", raw))


def search_flights(query: FlightSearchInput) -> FlightSearchResult | ToolError:
    """
    Search for flight offers.

    Returns FlightSearchResult on success, or ToolError on failure so callers
    (agents, API handlers) can branch on outcome without a try/except around
    every call site.

    Behavior depends on settings.use_mock_data:
      - True:  returns local fixture data, no network call, always succeeds
      - False: calls the real Duffel sandbox (requires DUFFEL_API_KEY)
    """
    if settings.use_mock_data:
        offers_raw = _load_mock_offers(query)
        offers = [_parse_offer(o) for o in offers_raw]
        is_mock = True
    else:
        try:
            settings.validate_duffel()
        except RuntimeError as e:
            return ToolError(
                tool_name="search_flights",
                error_type="config_error",
                message=str(e),
                retryable=False,
            )
        payload = _build_offer_request_payload(query)
        try:
            raw = _post_offer_request(payload)
        except DuffelAPIError as e:
            return ToolError(
                tool_name="search_flights",
                error_type="duffel_api_error",
                message=e.message,
                retryable=e.retryable,
            )
        except httpx.TimeoutException:
            return ToolError(
                tool_name="search_flights",
                error_type="timeout",
                message="Duffel API did not respond within 15s",
                retryable=True,
            )
        offers_raw = raw.get("data", {}).get("offers", [])
        offers = [_parse_offer(o) for o in offers_raw]
        is_mock = False

    if query.max_budget_usd is not None:
        offers = [o for o in offers if o.total_price_usd <= query.max_budget_usd]

    offers.sort(key=lambda o: o.total_price_usd)
    # Cap what's handed back to the agent -- see hotels.py's search_hotels()
    # for the full rationale (an uncapped result can, by itself, blow a
    # hosted LLM's per-minute token budget). Already sorted cheapest first.
    offers = offers[:_MAX_RESULTS_RETURNED]

    return FlightSearchResult(
        query=query, offers=offers, provider="duffel", is_sandbox=True, is_mock=is_mock
    )


if __name__ == "__main__":
    # Manual smoke test: run `uv run python -m app.tools.flights`
    # Add USE_MOCK_DATA=true to .env to test without hitting the real API.
    from datetime import date, timedelta

    demo_query = FlightSearchInput(
        origin="JFK",
        destination="ATL",
        depart_date=date.today() + timedelta(days=30),
        return_date=date.today() + timedelta(days=37),
        adults=1,
    )
    result = search_flights(demo_query)
    if isinstance(result, ToolError):
        print(f"[ERROR] {result.error_type}: {result.message}")
    else:
        print(f"Found {len(result.offers)} offers (mock={result.is_mock}):")
        for offer in result.offers[:5]:
            print(
                f"  {offer.offer_id}: ${offer.total_price_usd} — {len(offer.segments)} segment(s)"
            )
