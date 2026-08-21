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

from pydantic import BaseModel, Field

from app.tools.schemas import FlightOffer, HotelListing


class ResearchOutput(BaseModel):
    """What the researcher Agent's Task is expected to produce.

    The researcher has tool access to search_flights/search_hotels/
    get_weather_forecast/check_visa_requirements/estimate_ground_transport
    (see app/agents/tools.py) and is expected to pick ONE flight offer and
    ONE hotel listing it considers the best fit for the trip request — not
    return every option it found. Either can be None if nothing suitable
    was found (e.g. no flights under budget) — that's a legitimate outcome
    the budget/planning steps must handle, not a failure.
    """

    selected_flight: FlightOffer | None = None
    selected_hotel: HotelListing | None = None
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


class BudgetCheckResult(BaseModel):
    """Output of app.agents.budget.validate_budget() — plain Python, no LLM."""

    within_budget: bool
    total_cost_usd: float
    max_budget_usd: float | None
    flight_cost_usd: float
    hotel_cost_usd: float
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
