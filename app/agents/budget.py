"""
budget.py — validates researched options against the trip's stated
budget.

Deliberately plain Python, not a CrewAI Agent/LLM call — per the project's
own Phase 3 roadmap note: "decided to be plain Python logic, not an LLM
call." A budget check is arithmetic, not reasoning: there's nothing here
an LLM would do better than a comparison, and using one would add latency,
cost, and a source of non-determinism to a step that should be exact.

This deliberately does NOT reject a plan when it's over budget — it
reports the fact via BudgetCheckResult.within_budget and a human-readable
message, and lets app/agents/flow.py decide what to do with that
information (currently: surface it to the human at the approval step,
rather than silently blocking). A plan slightly over budget with a great
flight/hotel match may still be worth showing the traveler.
"""

from __future__ import annotations

from app.agents.schemas import BudgetCheckResult
from app.tools.schemas import CarRateOption, FlightOffer, HotelListing


def validate_budget(
    max_budget_usd: float | None,
    flight: FlightOffer | None,
    hotel: HotelListing | None,
    car_rentals: list[CarRateOption] | None = None,
) -> BudgetCheckResult:
    """Compare a selected flight offer + hotel listing + car rental rates
    against the trip's max_budget_usd (Trip.max_budget_usd — None means no
    budget was set, in which case everything is considered within budget by
    definition).

    hotel.estimated_price_total_usd and each car rental's
    estimated_price_total_usd are themselves ESTIMATES (see HotelListing's
    and CarRateOption's own docstrings in app/tools/schemas.py) — this
    function doesn't add any further uncertainty on top of that, it just
    sums what the tools already returned. car_rentals defaults to None/empty
    since a trip can have zero, one, or more (e.g. one to reach the
    departure airport, a separate one at the destination), unlike flight/
    hotel which are single selections.
    """
    flight_cost = flight.total_price_usd if flight is not None else 0.0
    hotel_cost = hotel.estimated_price_total_usd if hotel is not None else 0.0
    car_rental_cost = sum(c.estimated_price_total_usd for c in (car_rentals or []))
    total_cost = flight_cost + hotel_cost + car_rental_cost

    if max_budget_usd is None:
        return BudgetCheckResult(
            within_budget=True,
            total_cost_usd=total_cost,
            max_budget_usd=None,
            flight_cost_usd=flight_cost,
            hotel_cost_usd=hotel_cost,
            car_rental_cost_usd=car_rental_cost,
            message="No budget was set for this trip, so no budget check was applied.",
        )

    within_budget = total_cost <= max_budget_usd

    if within_budget:
        message = (
            f"Total estimated cost ${total_cost:.2f} is within the ${max_budget_usd:.2f} budget."
        )
    else:
        over_by = total_cost - max_budget_usd
        message = (
            f"Total estimated cost ${total_cost:.2f} is ${over_by:.2f} over the "
            f"${max_budget_usd:.2f} budget."
        )

    return BudgetCheckResult(
        within_budget=within_budget,
        total_cost_usd=total_cost,
        max_budget_usd=max_budget_usd,
        flight_cost_usd=flight_cost,
        hotel_cost_usd=hotel_cost,
        car_rental_cost_usd=car_rental_cost,
        message=message,
    )
