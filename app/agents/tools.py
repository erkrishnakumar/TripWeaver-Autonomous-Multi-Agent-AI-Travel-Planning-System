"""
CrewAI @tool wrappers around the read-only tool functions.

These wrap app.tools.* functions DIRECTLY, not through the MCP server
(app/mcp_server/server.py) — matching Phase 1's own stated design goal of
being "wrappable by both CrewAI and FastMCP without duplicating logic."
The two wrappers are independent, parallel consumers of the same
underlying business logic; CrewAI does not route through MCP here.

ONLY read-only tools are wrapped here: search_flights,
get_weather_forecast, search_hotels, check_visa_requirements,
estimate_ground_transport, search_car_rentals, and get_car_rental_quote.
get_car_rental_quote() IS read-only despite being step 2 of Cars' three-step
flow — it only calls Duffel's Quote endpoint to firm up a price, it never
writes to TripWeaver's own DB, so it carries no more autonomy risk than any
other search tool. create_trip, propose_flight_booking/propose_hotel_booking/
propose_car_booking, and create_car_rental_booking are DELIBERATELY NOT
exposed as agent tools — see app/agents/flow.py's module docstring for why.
Giving an LLM agent direct tool access to booking/trip-creation would mean
the LLM decides when a DB write (or, for create_car_rental_booking, a REAL
booking) happens; this project's human-approval-gate design instead has
flow.py (plain Python, not the LLM) call those functions only after an
explicit human approval step. That is a deliberate, permanent boundary,
not a temporary gap to fill in later.

RETURN VALUE CONTRACT: CrewAI's @tool decorator does not stringify a
tool's return value for you (verified empirically before writing this —
tool.run() returns the raw Python object untouched). Every wrapper below
therefore returns a plain string itself: model_dump_json() on success, or
"ERROR [<error_type>]: <message>" on a domain ToolError — so the agent
always receives readable text regardless of any internal CrewAI
stringification behavior we haven't independently verified for the full
agent-execution loop.

ARGUMENT CONTRACT: CrewAI's BaseTool.run() -> _validate_kwargs() does
`self.args_schema.model_validate(kwargs).model_dump()` before calling this
module's wrapped function — and Pydantic's model_dump() ALWAYS recursively
serializes nested BaseModel fields down to plain dicts, never preserving
them as model instances. That means every wrapper's `query` parameter
arrives as a plain dict on any real call through CrewAI's actual
tool-invocation path (i.e. a real agent run), not as the FlightSearchInput/
etc. instance its type hint promises. Every wrapper below therefore
re-validates `query` through its schema class before use
(`FlightSearchInput.model_validate(query)`, etc.) — model_validate() safely
accepts either a dict OR an already-valid instance of the same class, so
this is a no-op when called directly with a real instance (e.g. from a
test) and the actual fix when called through a real agent. This was caught
by calling .run() with a real (non-monkeypatched) underlying function, not
by inspecting args_schema or running the existing test suite — every
existing test monkeypatches the underlying function with a lambda that
ignores its argument's shape, which is exactly what hid this.

NO `from __future__ import annotations` IN THIS FILE — DELIBERATELY.
Every other file in this project uses it, but it breaks CrewAI 1.15.16's
runtime tool-argument validation: a tool function's parameter annotation
(e.g. `query: GroundTransportEstimateInput`) becomes a lazy string under
postponed evaluation, and CrewAI's @tool decorator fails to resolve that
string back to the real class when actually validating a call — raising
"`<ToolName>` is not fully defined ... call `<ToolName>.model_rebuild()`"
at call time, even though schema generation alone (.args_schema) looks
completely fine and gives no warning at import time. This was caught by
actually calling .run() on a wrapped tool in a test, not just inspecting
its generated schema — see tests/test_agent_tools.py.
"""

from crewai.tools import tool
from pydantic import BaseModel

from app.tools.car_rentals import get_car_rental_quote as _get_car_rental_quote
from app.tools.car_rentals import search_car_rentals as _search_car_rentals
from app.tools.flights import search_flights as _search_flights
from app.tools.ground_transport import estimate_ground_transport as _estimate_ground_transport
from app.tools.hotels import search_hotels as _search_hotels
from app.tools.schemas import (
    CarQuoteInput,
    CarRentalSearchInput,
    FlightSearchInput,
    GroundTransportEstimateInput,
    HotelSearchInput,
    ToolError,
    VisaCheckInput,
    WeatherSearchInput,
)
from app.tools.visa import check_visa_requirements as _check_visa_requirements
from app.tools.weather import get_weather_forecast as _get_weather_forecast


def _stringify(result: BaseModel) -> str:
    """Shared success/error -> string conversion, used by every wrapper
    below so the agent always gets predictable, readable tool output."""
    if isinstance(result, ToolError):
        return f"ERROR [{result.error_type}]: {result.message}"
    return result.model_dump_json()


@tool("Search Flights")
def search_flights_tool(query: FlightSearchInput) -> str:
    """Search for bookable flight offers between two IATA airport codes.
    Returns offers sorted by price ascending as JSON, or an ERROR string
    starting with 'ERROR [' if the search failed — check for that prefix
    before treating the result as a successful offer list."""
    return _stringify(_search_flights(FlightSearchInput.model_validate(query)))


@tool("Get Weather Forecast")
def get_weather_forecast_tool(query: WeatherSearchInput) -> str:
    """Get a daily weather forecast for a city or explicit lat/lon. Only
    covers roughly the next 15 days — an ERROR result for dates further
    out means the forecast isn't available yet, not that the tool failed."""
    return _stringify(_get_weather_forecast(WeatherSearchInput.model_validate(query)))


@tool("Search Hotels")
def search_hotels_tool(query: HotelSearchInput) -> str:
    """Search for hotels near a city or lat/lon. IMPORTANT: the returned
    price on each listing is Duffel's own ESTIMATE, not a guaranteed
    bookable rate — always describe it to the traveler as an estimate."""
    return _stringify(_search_hotels(HotelSearchInput.model_validate(query)))


@tool("Check Visa Requirements")
def check_visa_requirements_tool(query: VisaCheckInput) -> str:
    """Get an AI-generated, informational-only visa requirement estimate.
    This is NEVER authoritative — always relay the disclaimer field in the
    result to the traveler, and treat a null visa_required as 'unknown,
    check an official source', not as an error."""
    return _stringify(_check_visa_requirements(VisaCheckInput.model_validate(query)))


@tool("Estimate Ground Transport")
def estimate_ground_transport_tool(query: GroundTransportEstimateInput) -> str:
    """Get a rough, NON-BOOKABLE cost estimate for a ground-transport leg
    (e.g. home to airport, or airport to hotel). There is no ride-hailing
    API behind this — always relay the disclaimer field, and never present
    the cost range as a fare quote."""
    return _stringify(
        _estimate_ground_transport(GroundTransportEstimateInput.model_validate(query))
    )


@tool("Search Car Rentals")
def search_car_rentals_tool(query: CarRentalSearchInput) -> str:
    """Search for car rental rates for a pickup (and optional dropoff)
    location and time window. Returns rates sorted by price ascending as
    JSON, or an ERROR string. IMPORTANT: every rate's price is Duffel's own
    ESTIMATE — always call Get Car Rental Quote on the chosen rate_id before
    treating any price as firm."""
    return _stringify(_search_car_rentals(CarRentalSearchInput.model_validate(query)))


@tool("Get Car Rental Quote")
def get_car_rental_quote_tool(query: CarQuoteInput) -> str:
    """Firm up a car rental rate's price before it can be proposed for
    booking. Takes the rate_id from a rate returned by Search Car Rentals.
    Duffel's quote price can differ from the original rate's price — use
    THIS tool's price, never the rate's own estimated_price_total_usd, once
    a specific rate has been chosen."""
    return _stringify(_get_car_rental_quote(CarQuoteInput.model_validate(query)))


ALL_RESEARCH_TOOLS = [
    search_flights_tool,
    get_weather_forecast_tool,
    search_hotels_tool,
    check_visa_requirements_tool,
    estimate_ground_transport_tool,
    search_car_rentals_tool,
    get_car_rental_quote_tool,
]
