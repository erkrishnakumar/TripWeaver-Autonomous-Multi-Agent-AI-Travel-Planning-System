"""
Schemas specific to the agent layer (Phase 3) — how CrewAI Tasks hand
structured data between the research step, the plain-Python budget check,
and the planning step.

These are deliberately separate from app/tools/schemas.py: that file is
the contract between tools and everything that calls them (agents, the MCP
server, the future API layer). This file is the contract between agent
Tasks specifically — it exists because CrewAI's Task(output_pydantic=...)
needs a target shape to coerce the LLM's output into, and that shape is
an agent-orchestration concern, not a tool-layer one.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, model_validator

from app.tools.schemas import CarRateOption, FlightOffer, HotelListing


class ResearchOutput(BaseModel):
    """What the researcher Agent's Task is expected to produce.

    The researcher has tool access to search_flights/search_hotels/
    get_weather_forecast/check_visa_requirements/estimate_ground_transport/
    search_car_rentals/get_car_rental_quote (see app/agents/tools.py) and is
    expected to pick ONE flight offer and ONE hotel listing it considers the
    best fit for the trip request — not return every option it found. A car
    rental is OPTIONAL: only pick one if it's actually useful for this trip
    (e.g. getting around at the destination), leave it None otherwise. Any
    of the three can be None if nothing suitable was found (e.g. no flights
    under budget, or no car needed) — that's a legitimate outcome the
    budget/planning steps must handle, not a failure.

    selected_car_rental is a RATE (an estimate, see CarRateOption's own
    docstring) — propose_bookings() in app/agents/flow.py is responsible
    for re-fetching a firm quote before ever proposing it for booking, the
    researcher/planner never see or handle a firm quote.
    """

    selected_flight: FlightOffer | None = None
    selected_hotel: HotelListing | None = None
    selected_car_rental: CarRateOption | None = None
    weather_summary: str = Field(
        default="", description="Plain-language weather summary for the trip dates"
    )
    visa_summary: str = Field(
        default="", description="Plain-language visa summary, including the disclaimer"
    )
    ground_transport_notes: str = Field(
        default="", description="Plain-language notes on estimated ground-transport cost"
    )
    research_notes: str = Field(
        default="", description="Any other context the researcher wants the planner to know"
    )

    @model_validator(mode="before")
    @classmethod
    def _empty_dict_means_none(cls, data: Any) -> Any:
        """The researcher LLM sometimes writes `{}` for selected_flight/
        selected_hotel/selected_car_rental when it found nothing to select,
        instead of `null` (observed with a local Ollama model formatting
        this output) -- an empty object still fails FlightOffer/HotelListing/
        CarRateOption's required fields. Only an EMPTY dict is coerced here;
        a partially-filled dict is left alone so it still fails loudly, since
        that indicates real hallucinated/incomplete data, not just the
        model's "nothing found" idiom."""
        if not isinstance(data, dict):
            return data
        for key in ("selected_flight", "selected_hotel", "selected_car_rental"):
            if data.get(key) == {}:
                data[key] = None
        return data


class BudgetCheckResult(BaseModel):
    """Output of app.agents.budget.validate_budget() — plain Python, no LLM."""

    within_budget: bool
    total_cost_usd: float
    max_budget_usd: float | None
    flight_cost_usd: float
    hotel_cost_usd: float
    car_rental_cost_usd: float = 0.0
    message: str


class PlanOutput(BaseModel):
    """What the planner Agent's Task is expected to produce."""

    itinerary_summary: str = Field(description="A traveler-facing summary of the full plan")
    day_by_day: list[str] = Field(
        default_factory=list, description="Rough day-by-day plan, one entry per day"
    )
    caveats: list[str] = Field(
        default_factory=list,
        description=(
            "Things the traveler should know before approving — e.g. visa disclaimer, "
            "hotel price being an estimate, ground transport being a rough figure"
        ),
    )
