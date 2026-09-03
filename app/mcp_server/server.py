"""
TripWeaver MCP server — wraps the Phase 1 tool layer as FastMCP tools.

Runs as a standalone process, separate from the agent runtime (Phase 3),
so it can be reached by any MCP client — a CrewAI agent, Claude Desktop, or
anything else speaking the protocol — without those clients importing
app.tools directly.

ELEVEN tools are exposed. Four are deterministic/LLM lookups from the
Phase 1 roadmap (search_flights, get_weather_forecast, search_hotels,
check_visa_requirements). create_trip is included because an MCP client
needs a trip_id before it can call any propose_* tool; without it this
server can't be used end-to-end for its actual purpose. Phase 1's "five
tools" framing was about the tool layer, not a cap on what Phase 2
exposes. propose_booking() is split into propose_flight_booking /
propose_hotel_booking / propose_car_booking (see below).
estimate_ground_transport is a Phase 2.1 addition — see its own section
below for why it exists and why it's architecturally different from every
other tool here. search_car_rentals / get_car_rental_quote / propose_car_
booking are the Cars rollout — see their own section below for why Cars
needs two read-only tools instead of one.

propose_booking() is exposed as THREE separate MCP tools rather than one,
because its Python signature takes a Union[ProposeFlightBookingInput,
ProposeHotelBookingInput, ProposeCarBookingInput] — a shape that doesn't
map cleanly onto a single MCP tool's JSON schema (an LLM client would see
one ambiguous "offer_or_listing_or_rate" field instead of three clearly-
typed, clearly-named tools). Three explicit tools give a calling agent an
unambiguous schema and match the project's own three separate
*BookingInput models in schemas.py.

CARS (search_car_rentals / get_car_rental_quote / propose_car_booking):
unlike Flights (search returns bookable offers directly) or Stays (search
-> separate firm-rate fetch), Duffel Cars is a real THREE-step flow —
Search -> Quote -> Booking — see app/tools/car_rentals.py's module
docstring for the full contract. get_car_rental_quote is exposed as its
OWN read-only tool (not folded into propose_car_booking) because Duffel's
own docs say a quote's price can differ from the rate's price, so a
calling agent must fetch and see the firm quote before a human is ever
asked to approve anything — collapsing that into propose_car_booking would
hide the step where the real price is discovered. create_car_rental_booking()
(the real, money-moving Booking step) is NOT exposed here, and never will
be from this file — same permanent principle as every other real-booking
endpoint in this codebase; see app/tools/car_rentals.py's module docstring.

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

AUTH: every tool here requires a real, authenticated TripWeaver user --
the same bearer JWT app/auth/tokens.py issues for the HTTP API's
POST /auth/login, not a separate credential system for MCP clients. Two
reasons this exists, not just "nice to have API hygiene": Duffel's own
Service Agreement requires a closed, authenticated user group (see
docs/Auth_Requirement.md — this file's search_flights/search_hotels/
search_car_rentals/get_car_rental_quote/check_visa_requirements all call a
real, metered third-party API), and without a user_id a trip created here
has no owner, making it permanently unreachable through the real
approval-gate HTTP API (Trip.user_id is None, and every trip/approval
endpoint there 404s on an ownerless resource rather than exposing it to
anyone — see app/api/main.py). _current_user_id() below enforces this at
the tool-call level (checked directly, not only relied on via FastMCP's
own HTTP-level `auth=` gate) so the same requirement holds for every
transport this server might run over, including the in-memory transport
this file's own tests use.
"""

from __future__ import annotations

import logging
import uuid
from datetime import date
from typing import TypeVar

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError as MCPToolError
from fastmcp.server.auth import AccessToken as MCPAccessToken
from fastmcp.server.auth import TokenVerifier
from fastmcp.server.dependencies import get_access_token
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.tokens import InvalidTokenError, decode_access_token
from app.db.models import Trip
from app.db.session import get_session
from app.tools.car_rentals import get_car_rental_quote as _get_car_rental_quote
from app.tools.car_rentals import search_car_rentals as _search_car_rentals
from app.tools.create_trip import create_trip as _create_trip
from app.tools.flights import search_flights as _search_flights
from app.tools.ground_transport import estimate_ground_transport as _estimate_ground_transport
from app.tools.hotels import search_hotels as _search_hotels
from app.tools.propose_booking import propose_booking as _propose_booking
from app.tools.schemas import (
    CarQuoteInput,
    CarQuoteResult,
    CarRateOption,
    CarRentalSearchInput,
    CarRentalSearchResult,
    DriverDetails,
    FlightOffer,
    FlightSearchInput,
    FlightSearchResult,
    GroundTransportEstimateInput,
    GroundTransportEstimateResult,
    HotelListing,
    HotelSearchInput,
    HotelSearchResult,
    ProposeBookingResult,
    ProposeCarBookingInput,
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


class _JWTBearerVerifier(TokenVerifier):
    """Verifies the same HS256 JWT bearer tokens app/auth/tokens.py issues
    for the HTTP API. Deliberately NOT fastmcp's own JWTVerifier, which
    needs a public_key/secret handed to it eagerly at construction time --
    that would make this module fail to import outright wherever
    JWT_SECRET_KEY isn't set yet (e.g. a clean CI checkout that never
    touches this file's own JWT settings). decode_access_token() instead
    reads settings.jwt_secret_key fresh on every call, same as
    app/api/deps.py's get_current_user() does for the HTTP API.
    """

    async def verify_token(self, token: str) -> MCPAccessToken | None:
        try:
            user_id = decode_access_token(token)
        except InvalidTokenError:
            return None
        except RuntimeError:
            # settings.validate_jwt() failure -- JWT_SECRET_KEY isn't
            # configured at all. Reject the token rather than 500ing.
            return None
        return MCPAccessToken(token=token, client_id=user_id, scopes=[], claims={"sub": user_id})


def _current_user_id() -> uuid.UUID:
    """Every tool below calls this first. Raises a client-visible
    MCPToolError (never returns a domain ToolError -- there's no
    "graceful" outcome for a missing/invalid identity) if the caller isn't
    a real, authenticated TripWeaver user. See the module docstring's AUTH
    section for why this exists."""
    token = get_access_token()
    if token is None:
        raise MCPToolError(
            "Authentication required. Call POST /auth/login on the TripWeaver "
            "API to get a bearer token, then send it as this MCP connection's "
            "Authorization header."
        )
    try:
        return uuid.UUID(token.client_id)
    except ValueError:
        raise MCPToolError("Authenticated token's subject is not a valid user id.") from None


async def _load_owned_trip(session: AsyncSession, trip_id: str, user_id: uuid.UUID) -> Trip:
    """Same ownership discipline as app/api/main.py: 404-shaped (a generic
    "no trip found"), never 403 -- existence isn't confirmed to a
    non-owner, and an ownerless trip (Trip.user_id is None, e.g. one
    created before this check existed) is equally unreachable rather than
    being treated as belonging to anyone who asks."""
    try:
        trip_uuid = uuid.UUID(trip_id)
    except ValueError:
        raise MCPToolError(f"[invalid_trip_id] '{trip_id}' is not a valid UUID.") from None
    trip = await session.get(Trip, trip_uuid)
    if trip is None or trip.user_id != user_id:
        raise MCPToolError(f"[invalid_trip_id] No trip found with id '{trip_id}'.")
    return trip


mcp = FastMCP(
    name="TripWeaver",
    auth=_JWTBearerVerifier(),
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
        "field too, and never present the range as a fare quote. search_car_rentals "
        "returns car rental rates that are ESTIMATES — always call get_car_rental_quote "
        "on the chosen rate_id to get a firm price before proposing it for booking via "
        "propose_car_booking, which NEVER books anything for real either."
    ),
)


_ResultT = TypeVar("_ResultT", bound=BaseModel)


def _unwrap(result: _ResultT | DomainToolError) -> _ResultT:
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
    _current_user_id()
    return _unwrap(_search_flights(query))


@mcp.tool
def get_weather_forecast(query: WeatherSearchInput) -> WeatherForecastResult:
    """Get a daily weather forecast for a city name or explicit lat/lon.

    Only covers roughly the next 15 days out — dates beyond that return a
    tool-call error explaining the limitation rather than a misleading or
    empty result.
    """
    _current_user_id()
    return _unwrap(_get_weather_forecast(query))


@mcp.tool
def search_hotels(query: HotelSearchInput) -> HotelSearchResult:
    """Search for hotels near a city name or explicit lat/lon.

    IMPORTANT: estimated_price_total_usd on each listing is Duffel's own
    best-effort estimate, not a guaranteed bookable rate — present it to
    the traveler as an estimate, never as a firm price.
    """
    _current_user_id()
    return _unwrap(_search_hotels(query))


@mcp.tool
def check_visa_requirements(query: VisaCheckInput) -> VisaCheckResult:
    """Get an AI-generated, informational-only visa requirement estimate.

    This is NEVER authoritative. Always relay the returned `disclaimer`
    field to the traveler alongside `summary`, and treat
    `visa_required: null` as 'unknown — check an official source', not as
    an error or a false negative.
    """
    _current_user_id()
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
    _current_user_id()
    return _unwrap(_estimate_ground_transport(query))


@mcp.tool
def search_car_rentals(query: CarRentalSearchInput) -> CarRentalSearchResult:
    """Search for car rental rates for a pickup (and optional dropoff)
    location and time window.

    IMPORTANT: every rate's price is Duffel's own ESTIMATE — always call
    get_car_rental_quote on the chosen rate_id before proposing it for
    booking or presenting a firm price to the traveler.
    """
    _current_user_id()
    return _unwrap(_search_car_rentals(query))


@mcp.tool
def get_car_rental_quote(query: CarQuoteInput) -> CarQuoteResult:
    """Firm up a car rental rate's price — the second step of Duffel's
    Search -> Quote -> Booking Cars flow.

    Duffel's docs are explicit that a quote's price can differ from the
    original rate's price — always use THIS tool's price, never the rate's
    own estimated_price_total_usd, once a specific rate has been chosen.
    """
    _current_user_id()
    return _unwrap(_get_car_rental_quote(query))


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
    automatically. Owned by the authenticated caller -- see the module
    docstring's AUTH section for why this matters.
    """
    user_id = _current_user_id()
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
                user_id=user_id,
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
    user_id = _current_user_id()
    query = ProposeFlightBookingInput(trip_id=trip_id, offer=offer)
    async with get_session() as session:
        await _load_owned_trip(session, trip_id, user_id)
        result = await _propose_booking(session, query)
        if isinstance(result, DomainToolError):
            await session.rollback()
            raise MCPToolError(f"[{result.error_type}] {result.message}")
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
    user_id = _current_user_id()
    query = ProposeHotelBookingInput(
        trip_id=trip_id, listing=listing, check_in=check_in, check_out=check_out
    )
    async with get_session() as session:
        await _load_owned_trip(session, trip_id, user_id)
        result = await _propose_booking(session, query)
        if isinstance(result, DomainToolError):
            await session.rollback()
            raise MCPToolError(f"[{result.error_type}] {result.message}")
        try:
            await session.commit()
        except Exception:
            await session.rollback()
            logger.exception("propose_hotel_booking commit failed")
            raise MCPToolError(
                "Could not persist the booking proposal due to a database error."
            ) from None
        return result


@mcp.tool
async def propose_car_booking(
    trip_id: str, rate: CarRateOption, quote: CarQuoteResult, driver: DriverDetails
) -> ProposeBookingResult:
    """Propose a car rental booking for human approval.

    Writes a PENDING_APPROVAL booking row — this NEVER calls a real
    provider booking endpoint, and never will. `rate` should be one of the
    CarRateOption objects returned by search_car_rentals, and `quote` MUST
    be the CarQuoteResult from calling get_car_rental_quote on that exact
    rate's rate_id (a mismatch is rejected). `driver` PII is required by
    Duffel's real booking contract even though search/quote never needed
    it — this is more sensitive data than any other tool in this file
    collects. Calling this twice with the same trip_id + quote is safe: the
    second call returns the same pending booking with was_existing=true
    instead of creating a duplicate.
    """
    user_id = _current_user_id()
    query = ProposeCarBookingInput(trip_id=trip_id, rate=rate, quote=quote, driver=driver)
    async with get_session() as session:
        await _load_owned_trip(session, trip_id, user_id)
        result = await _propose_booking(session, query)
        if isinstance(result, DomainToolError):
            await session.rollback()
            raise MCPToolError(f"[{result.error_type}] {result.message}")
        try:
            await session.commit()
        except Exception:
            await session.rollback()
            logger.exception("propose_car_booking commit failed")
            raise MCPToolError(
                "Could not persist the booking proposal due to a database error."
            ) from None
        return result


def main() -> None:
    """Entrypoint for `uv run python -m app.mcp_server.server`."""
    mcp.run()


if __name__ == "__main__":
    main()
