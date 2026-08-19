"""
TripWeaver MCP server — wraps the Phase 1 tool layer as FastMCP tools.

Runs as a standalone process, separate from the agent runtime (Phase 3),
so it can be reached by any MCP client — a CrewAI agent, Claude Desktop, or
anything else speaking the protocol — without those clients importing
app.tools directly.

EIGHT tools are exposed. Four are deterministic/LLM lookups from the
Phase 1 roadmap (search_flights, get_weather_forecast, search_hotels,
check_visa_requirements). create_trip is included because an MCP client
needs a trip_id before it can call either propose_* tool; without it this
server can't be used end-to-end for its actual purpose. Phase 1's "five
tools" framing was about the tool layer, not a cap on what Phase 2
exposes. propose_booking() is split into propose_flight_booking /
propose_hotel_booking (see below). estimate_ground_transport is a Phase
2.1 addition — see its own section below for why it exists and why it's
architecturally different from every other tool here.

propose_booking() is exposed as TWO separate MCP tools rather than one,
because its Python signature takes a Union[ProposeFlightBookingInput,
ProposeHotelBookingInput] — a shape that doesn't map cleanly onto a single
MCP tool's JSON schema (an LLM client would see one ambiguous "offer_or_
listing" field instead of two clearly-typed, clearly-named tools). Two
explicit tools give a calling agent an unambiguous schema and match the
project's own two separate *BookingInput models in schemas.py.

GROUND TRANSPORT (estimate_ground_transport): TripWeaver does NOT
integrate a ride-hailing API for home<->airport / airport<->hotel legs.
Duffel Cars (a real product) was considered and rejected for this — it's
self-drive car rental (Avis/Hertz/Sixt/etc.), not a chauffeured transfer,
so using it here would be technically possible but practically wrong.
Real ride-hailing APIs don't fit either: Ola/Rapido have no public booking
API, and Uber's requires an enterprise partnership, not a self-serve
token. So this tool gives a rough, disclaimed cost ESTIMATE instead of a
real booking — see app/tools/ground_transport.py's module docstring for
the full rationale. Unlike every other stateful concept in this file, it
has NO approval-gate counterpart: there is nothing to approve, because
nothing here is ever bookable. It's also the only tool in this file with
zero external dependency (no Duffel, no Groq, no new API key) — it only
calls the geocoding path weather.py/hotels.py already use, plus local math.

ERROR HANDLING CONTRACT — READ BEFORE ADDING A NEW TOOL HERE:
Every underlying app.tools function returns `Result | ToolError` (a
Pydantic model) rather than raising. FastMCP has its own, differently-
named ToolError (fastmcp.exceptions.ToolError) — an *exception* that is
FastMCP's sanctioned way of sending a clear, client-visible failure
message. Every tool function in this file MUST route its return value
through `_unwrap()` before returning, with NO exceptions, even on a branch
that "obviously" won't be reached. Returning a domain ToolError directly
(instead of via `_unwrap()`) does not surface its message: FastMCP tries to
validate the raw ToolError against the tool's declared success return type,
fails, and raises its own generic "Output validation error" instead of the
actual reason — silently swallowing the real error message. This was
caught and verified with a standalone reproduction before this file was
written; see the module test suite for the corresponding regression test.

DB SESSION LIFECYCLE: create_trip, propose_flight_booking, and
propose_hotel_booking each open their own `async with get_session()` block
per call and commit at the end of that single call — session-per-call, not
a session held open across this (long-running) server process's lifetime.
This matches how propose_booking()'s own tests use it (caller owns the
transaction boundary) and avoids a stale, long-lived session accumulating
state across unrelated calls.
"""

from __future__ import annotations

import logging
from datetime import date

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError as MCPToolError

from app.db.session import get_session
from app.tools.create_trip import create_trip as _create_trip
from app.tools.flights import search_flights as _search_flights
from app.tools.ground_transport import estimate_ground_transport as _estimate_ground_transport
from app.tools.hotels import search_hotels as _search_hotels
from app.tools.propose_booking import propose_booking as _propose_booking
from app.tools.schemas import (
    FlightOffer,
    FlightSearchInput,
    FlightSearchResult,
    GroundTransportEstimateInput,
    GroundTransportEstimateResult,
    HotelListing,
    HotelSearchInput,
    HotelSearchResult,
    ProposeBookingResult,
    ProposeFlightBookingInput,
    ProposeHotelBookingInput,
    TripSummary,
    VisaCheckInput,
    VisaCheckResult,
    WeatherForecastResult,
    WeatherSearchInput,
)
from app.tools.schemas import ToolError as DomainToolError
from app.tools.visa import check_visa_requirements as _check_visa_requirements
from app.tools.weather import get_weather_forecast as _get_weather_forecast

logger = logging.getLogger(__name__)

mcp = FastMCP(
    name="TripWeaver",
    instructions=(
        "Travel-planning tools for TripWeaver. search_flights, search_hotels, and "
        "get_weather_forecast are read-only lookups. check_visa_requirements returns an "
        "AI-generated, informational-only estimate — always relay its disclaimer field "
        "verbatim to the traveler, and treat a null visa_required as 'unknown, check an "
        "official source', not as a failure. create_trip starts a new trip and returns "
        "the trip_id every other stateful tool needs. propose_flight_booking and "
        "propose_hotel_booking NEVER book anything for real: they only create a "
        "PENDING_APPROVAL record that a separate, human-triggered step must confirm. "
        "There is no tool here — and there will never be one — that completes a real "
        "booking. estimate_ground_transport gives a rough, non-bookable cost estimate "
        "for legs like home-to-airport or airport-to-hotel — always relay its disclaimer "
        "field too, and never present the range as a fare quote."
    ),
)


def _unwrap(result):
    """Translate a domain ToolError (a Pydantic model, returned by value)
    into FastMCP's ToolError (an exception, raised), so a tool failure is
    surfaced to the calling agent/LLM as an actual failed tool call instead
    of a 'successful' response that happens to contain error fields.

    MUST be the last thing every tool function below calls on its return
    path — see the module docstring for why skipping it silently discards
    the real error message.
    """
    if isinstance(result, DomainToolError):
        raise MCPToolError(f"[{result.error_type}] {result.message}")
    return result


# ---------------------------------------------------------------------------
# Deterministic / LLM tools — thin wrappers, no DB involved.
# ---------------------------------------------------------------------------


@mcp.tool
def search_flights(query: FlightSearchInput) -> FlightSearchResult:
    """Search for bookable flight offers between two IATA airport codes.

    Returns offers sorted by price ascending, optionally filtered by
    max_budget_usd. Runs against local fixtures if the server's
    USE_MOCK_DATA env var is true, otherwise the real Duffel sandbox.
    """
    return _unwrap(_search_flights(query))


@mcp.tool
def get_weather_forecast(query: WeatherSearchInput) -> WeatherForecastResult:
    """Get a daily weather forecast for a city name or explicit lat/lon.

    Only covers roughly the next 15 days out — dates beyond that return a
    tool-call error explaining the limitation rather than a misleading or
    empty result.
    """
    return _unwrap(_get_weather_forecast(query))


@mcp.tool
def search_hotels(query: HotelSearchInput) -> HotelSearchResult:
    """Search for hotels near a city name or explicit lat/lon.

    IMPORTANT: estimated_price_total_usd on each listing is Duffel's own
    best-effort estimate, not a guaranteed bookable rate — present it to
    the traveler as an estimate, never as a firm price.
    """
    return _unwrap(_search_hotels(query))


@mcp.tool
def check_visa_requirements(query: VisaCheckInput) -> VisaCheckResult:
    """Get an AI-generated, informational-only visa requirement estimate.

    This is NEVER authoritative. Always relay the returned `disclaimer`
    field to the traveler alongside `summary`, and treat
    `visa_required: null` as 'unknown — check an official source', not as
    an error or a false negative.
    """
    return _unwrap(_check_visa_requirements(query))


@mcp.tool
def estimate_ground_transport(
    query: GroundTransportEstimateInput,
) -> GroundTransportEstimateResult:
    """Get a rough, non-bookable cost estimate for a ground-transport leg
    (e.g. home -> airport, or airport -> hotel).

    THIS NEVER BOOKS A RIDE, and there is no path here that ever will —
    TripWeaver doesn't integrate a ride-hailing API (see
    app/tools/ground_transport.py for why). Always relay the returned
    `disclaimer` field to the traveler alongside
    estimated_cost_usd_low/high, and present the range as a rough budgeting
    figure, never as a fare quote.
    """
    return _unwrap(_estimate_ground_transport(query))


# ---------------------------------------------------------------------------
# DB-backed tools — one session per call, committed within that call.
# ---------------------------------------------------------------------------


@mcp.tool
async def create_trip(
    origin_iata: str,
    destination_iata: str,
    depart_date: date,
    return_date: date | None = None,
    adults: int = 1,
    max_budget_usd: float | None = None,
    requester_email: str | None = None,
) -> TripSummary:
    """Create a new trip in DRAFT status and return its id.

    This is the required entry point for any stateful flow through this
    server: the returned `id` is the trip_id that propose_flight_booking
    and propose_hotel_booking need. IATA codes are upper-cased
    automatically.
    """
    async with get_session() as session:
        try:
            trip = await _create_trip(
                session,
                origin_iata=origin_iata,
                destination_iata=destination_iata,
                depart_date=depart_date,
                return_date=return_date,
                adults=adults,
                max_budget_usd=max_budget_usd,
                requester_email=requester_email,
            )
            await session.commit()
        except Exception:
            await session.rollback()
            logger.exception("create_trip failed")
            raise MCPToolError(
                "Could not create the trip due to a database error. Check server logs."
            ) from None

        return TripSummary(
            id=str(trip.id),
            status=trip.status.value,
            origin_iata=trip.origin_iata,
            destination_iata=trip.destination_iata,
            depart_date=trip.depart_date,
            return_date=trip.return_date,
            adults=trip.adults,
            max_budget_usd=trip.max_budget_usd,
            requester_email=trip.requester_email,
        )


@mcp.tool
async def propose_flight_booking(trip_id: str, offer: FlightOffer) -> ProposeBookingResult:
    """Propose a flight booking for human approval.

    Writes a PENDING_APPROVAL booking row — this NEVER calls a real
    provider booking endpoint, and never will. `offer` should be one of the
    FlightOffer objects returned by search_flights, unmodified. Calling
    this twice with the same trip_id + offer is safe: the second call
    returns the same pending booking with was_existing=true instead of
    creating a duplicate.
    """
    query = ProposeFlightBookingInput(trip_id=trip_id, offer=offer)
    async with get_session() as session:
        result = await _propose_booking(session, query)
        if isinstance(result, DomainToolError):
            await session.rollback()
            return _unwrap(result)
        try:
            await session.commit()
        except Exception:
            await session.rollback()
            logger.exception("propose_flight_booking commit failed")
            raise MCPToolError(
                "Could not persist the booking proposal due to a database error."
            ) from None
        return result


@mcp.tool
async def propose_hotel_booking(
    trip_id: str, listing: HotelListing, check_in: date, check_out: date
) -> ProposeBookingResult:
    """Propose a hotel booking for human approval.

    Writes a PENDING_APPROVAL booking row — this NEVER calls a real
    provider booking endpoint, and never will. `listing` should be one of
    the HotelListing objects returned by search_hotels, unmodified; its
    price is an ESTIMATE (see search_hotels), so surface that to the human
    approver rather than presenting it as a firm price. Calling this twice
    with the same trip_id + listing is safe: the second call returns the
    same pending booking with was_existing=true instead of creating a
    duplicate.
    """
    query = ProposeHotelBookingInput(
        trip_id=trip_id, listing=listing, check_in=check_in, check_out=check_out
    )
    async with get_session() as session:
        result = await _propose_booking(session, query)
        if isinstance(result, DomainToolError):
            await session.rollback()
            return _unwrap(result)
        try:
            await session.commit()
        except Exception:
            await session.rollback()
            logger.exception("propose_hotel_booking commit failed")
            raise MCPToolError(
                "Could not persist the booking proposal due to a database error."
            ) from None
        return result


def main() -> None:
    """Entrypoint for `uv run python -m app.mcp_server.server`."""
    mcp.run()


if __name__ == "__main__":
    main()