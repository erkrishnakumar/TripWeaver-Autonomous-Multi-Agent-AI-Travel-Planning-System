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

ARGUMENT CONTRACT: every wrapper takes its underlying tool's fields as
FLAT, top-level parameters (origin, destination, depart_date, ...) instead
of one nested `query: SomeInput` object, and builds the SomeInput instance
itself before calling the wrapped function. This is deliberate, not
incidental: a single nested-object parameter makes CrewAI generate a tool
schema shaped like {"query": {"$ref": "#/$defs/FlightSearchInput"}}, and
in practice models (this was observed with both a local Ollama qwen3:14b
and hosted Groq models) reliably flatten that extra nesting level away and
call the tool with the inner fields directly at the top level, which then
fails Pydantic validation on `query` being a required field that's simply
missing. Flat parameters match the tool-call shape models actually
produce. Each wrapper still constructs and validates the real Input model
before calling the wrapped function, so every domain validator (e.g. "give
either city or lat/lon, not both") still runs — only the OUTER nesting is
flattened, not the validation.

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

from datetime import date, datetime

from crewai.tools import tool
from pydantic import BaseModel

from app.tools.car_rentals import get_car_rental_quote as _get_car_rental_quote
from app.tools.car_rentals import search_car_rentals as _search_car_rentals
from app.tools.flights import search_flights as _search_flights
from app.tools.ground_transport import estimate_ground_transport as _estimate_ground_transport
from app.tools.hotels import search_hotels as _search_hotels
from app.tools.schemas import (
    CabinClass,
    CarQuoteInput,
    CarRentalSearchInput,
    ChildGuest,
    FlightSearchInput,
    GroundTransportEstimateInput,
    HotelSearchInput,
    ToolError,
    TravelPurpose,
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
def search_flights_tool(
    origin: str,
    destination: str,
    depart_date: date,
    adults: int = 1,
    return_date: date | None = None,
    cabin_class: CabinClass = CabinClass.ECONOMY,
    max_budget_usd: float | None = None,
) -> str:
    """Search for bookable flight offers between two IATA airport codes.
    Returns offers sorted by price ascending as JSON, or an ERROR string
    starting with 'ERROR [' if the search failed — check for that prefix
    before treating the result as a successful offer list."""
    query = FlightSearchInput(
        origin=origin,
        destination=destination,
        depart_date=depart_date,
        adults=adults,
        return_date=return_date,
        cabin_class=cabin_class,
        max_budget_usd=max_budget_usd,
    )
    return _stringify(_search_flights(query))


@tool("Get Weather Forecast")
def get_weather_forecast_tool(
    start_date: date,
    end_date: date,
    city: str | None = None,
    latitude: float | None = None,
    longitude: float | None = None,
) -> str:
    """Get a daily weather forecast for a city or explicit lat/lon (give
    EITHER a city name OR both latitude and longitude, never both forms).
    A real forecast only covers roughly the next 15 days — for dates
    further out, the result instead holds historical climate averages for
    this time of year (is_climate_average will be true, with a disclaimer
    field). That is NOT a real forecast: report it as a historical average
    and relay the disclaimer verbatim, never as an actual prediction."""
    query = WeatherSearchInput(
        city=city,
        latitude=latitude,
        longitude=longitude,
        start_date=start_date,
        end_date=end_date,
    )
    return _stringify(_get_weather_forecast(query))


@tool("Search Hotels")
def search_hotels_tool(
    check_in: date,
    check_out: date,
    city: str | None = None,
    latitude: float | None = None,
    longitude: float | None = None,
    adults: int = 1,
    child_ages: list[int] | None = None,
    rooms: int = 1,
    max_budget_usd_per_night: float | None = None,
    radius_km: int = 5,
) -> str:
    """Search for hotels near a city or lat/lon (give EITHER a city name OR
    both latitude and longitude, never both forms). child_ages is one age
    (0-17) per child traveler, or omit/leave empty if there are no
    children. IMPORTANT: the returned price on each listing is Duffel's own
    ESTIMATE, not a guaranteed bookable rate — always describe it to the
    traveler as an estimate."""
    query = HotelSearchInput(
        city=city,
        latitude=latitude,
        longitude=longitude,
        check_in=check_in,
        check_out=check_out,
        adults=adults,
        children=[ChildGuest(age=age) for age in (child_ages or [])],
        rooms=rooms,
        max_budget_usd_per_night=max_budget_usd_per_night,
        radius_km=radius_km,
    )
    return _stringify(_search_hotels(query))


@tool("Check Visa Requirements")
def check_visa_requirements_tool(
    passport_country: str,
    destination_country: str,
    purpose: TravelPurpose = TravelPurpose.TOURISM,
) -> str:
    """Get an AI-generated, informational-only visa requirement estimate.
    This is NEVER authoritative — always relay the disclaimer field in the
    result to the traveler, and treat a null visa_required as 'unknown,
    check an official source', not as an error."""
    query = VisaCheckInput(
        passport_country=passport_country,
        destination_country=destination_country,
        purpose=purpose,
    )
    return _stringify(_check_visa_requirements(query))


@tool("Estimate Ground Transport")
def estimate_ground_transport_tool(
    origin_city: str | None = None,
    origin_latitude: float | None = None,
    origin_longitude: float | None = None,
    destination_city: str | None = None,
    destination_latitude: float | None = None,
    destination_longitude: float | None = None,
) -> str:
    """Get a rough, NON-BOOKABLE cost estimate for a ground-transport leg
    (e.g. home to airport, or airport to hotel). For both origin and
    destination, give EITHER a city/place name OR both latitude and
    longitude, never both forms. There is no ride-hailing API behind this —
    always relay the disclaimer field, and never present the cost range as
    a fare quote."""
    query = GroundTransportEstimateInput(
        origin_city=origin_city,
        origin_latitude=origin_latitude,
        origin_longitude=origin_longitude,
        destination_city=destination_city,
        destination_latitude=destination_latitude,
        destination_longitude=destination_longitude,
    )
    return _stringify(_estimate_ground_transport(query))


@tool("Search Car Rentals")
def search_car_rentals_tool(
    pickup_at: datetime,
    dropoff_at: datetime,
    driver_age: int,
    driver_country_code: str,
    pickup_city: str | None = None,
    pickup_latitude: float | None = None,
    pickup_longitude: float | None = None,
    dropoff_city: str | None = None,
    dropoff_latitude: float | None = None,
    dropoff_longitude: float | None = None,
    radius_km: int = 5,
) -> str:
    """Search for car rental rates for a pickup (and optional dropoff)
    location and time window. For pickup, and for dropoff if given, use
    EITHER a city name OR both latitude and longitude, never both forms —
    omit all three dropoff fields entirely for a same-location rental.
    Returns rates sorted by price ascending as JSON, or an ERROR string.
    IMPORTANT: every rate's price is Duffel's own ESTIMATE — always call
    Get Car Rental Quote on the chosen rate_id before treating any price as
    firm."""
    query = CarRentalSearchInput(
        pickup_city=pickup_city,
        pickup_latitude=pickup_latitude,
        pickup_longitude=pickup_longitude,
        dropoff_city=dropoff_city,
        dropoff_latitude=dropoff_latitude,
        dropoff_longitude=dropoff_longitude,
        pickup_at=pickup_at,
        dropoff_at=dropoff_at,
        radius_km=radius_km,
        driver_age=driver_age,
        driver_country_code=driver_country_code,
    )
    return _stringify(_search_car_rentals(query))


@tool("Get Car Rental Quote")
def get_car_rental_quote_tool(rate_id: str) -> str:
    """Firm up a car rental rate's price before it can be proposed for
    booking. Takes the rate_id from a rate returned by Search Car Rentals.
    Duffel's quote price can differ from the original rate's price — use
    THIS tool's price, never the rate's own estimated_price_total_usd, once
    a specific rate has been chosen."""
    return _stringify(_get_car_rental_quote(CarQuoteInput(rate_id=rate_id)))


ALL_RESEARCH_TOOLS = [
    search_flights_tool,
    get_weather_forecast_tool,
    search_hotels_tool,
    check_visa_requirements_tool,
    estimate_ground_transport_tool,
    search_car_rentals_tool,
    get_car_rental_quote_tool,
]
