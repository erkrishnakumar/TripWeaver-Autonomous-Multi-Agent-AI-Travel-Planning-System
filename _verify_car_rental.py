"""
One-off manual verification script — NOT part of the test suite, delete
after use.

Proves propose_bookings()'s car-rental branch actually succeeds end to end
against real Duffel APIs, using a real rate from Duffel's known sandbox
test coordinate (-24.38, -128.32) instead of relying on the LLM to
research its way there (which it won't, since a real trip's destination
geocodes to a real city with no sandbox car inventory).

Everything downstream of the research step is 100% real: get_car_rental_quote()
hits the real Duffel Cars API, propose_booking() writes to the real Postgres
database. Only the researcher/planner LLM calls are substituted with fixed
output, same "mock the Crew, run everything else for real" pattern already
used throughout tests/test_flow.py.

Run with: uv run python _verify_car_rental.py
"""

import builtins
from datetime import datetime, timedelta

import app.agents.flow as flow_module
from app.agents.schemas import PlanOutput, ResearchOutput
from app.tools.car_rentals import search_car_rentals
from app.tools.schemas import CarRentalSearchInput, ToolError

# Step 1: get a REAL, currently-valid rate from Duffel's sandbox test location.
query = CarRentalSearchInput(
    pickup_latitude=-24.38,
    pickup_longitude=-128.32,
    pickup_at=datetime.now() + timedelta(days=30),
    dropoff_at=datetime.now() + timedelta(days=33),
    driver_age=30,
    driver_country_code="US",
    radius_km=10,
)
search_result = search_car_rentals(query)
assert not isinstance(search_result, ToolError), f"Search failed: {search_result}"
rate = search_result.rates[0]

message = (
    f"Using real rate: {rate.rate_id} — {rate.car_description} — ${rate.estimated_price_total_usd}"
)
print(message)

research_output = ResearchOutput(selected_car_rental=rate)
plan_output = PlanOutput(itinerary_summary="Test trip exercising the car rental proposal path.")


class _FakeCrewOutput:
    def __init__(self, pydantic_obj):
        self.pydantic = pydantic_obj


class _FakeCrew:
    def __init__(self, pydantic_obj):
        self._obj = pydantic_obj

    async def kickoff_async(self):
        return _FakeCrewOutput(self._obj)


# Substitute only the LLM research/planning steps — everything else (budget
# check, DB writes, quote re-fetch, propose_booking) runs for real.
flow_module.build_research_crew = lambda summary, car_rental_override=None: _FakeCrew(
    research_output
)
flow_module.build_planning_crew = lambda *a, **k: _FakeCrew(plan_output)

# Auto-approve the human-approval prompt.
builtins.input = lambda _: "y"

flow = flow_module.TripPlanningFlow()
flow.kickoff(
    inputs={
        "origin_iata": "JFK",
        "destination_iata": "ATL",
        "depart_date": (datetime.now() + timedelta(days=30)).date().isoformat(),
        "adults": 1,
        "max_budget_usd": 1500.0,
        "driver_given_name": "Jane",
        "driver_family_name": "Doe",
        "driver_date_of_birth": "1990-01-01",
        "driver_email": "jane.doe@example.com",
        "driver_phone_number": "+15555550100",
    }
)

print("\nFinal state:")
print(flow.state.model_dump_json(indent=2))
