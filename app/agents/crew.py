"""
crew.py — wires the researcher and planner Agents into their Crews.

DEVIATION FROM THE ORIGINAL README WORDING, DOCUMENTED HERE ON PURPOSE:
the README describes crew.py as wiring "the three agents together"
(researcher, budget, planner). budget.py is deliberately plain Python, not
an Agent (see its own module docstring) — so there are only two real
Agents to wire. More importantly, budget validation has to run BETWEEN
research and planning (the planner needs to know whether the researched
options are within budget), and a single CrewAI Crew executing multiple
agent Tasks sequentially doesn't give a clean, verified insertion point
for external plain-Python logic to run between two agent steps. So this
file builds TWO separate single-agent Crews — build_research_crew() and
build_planning_crew() — and app/agents/flow.py is what sequences them with
budget.validate_budget() running in between as ordinary Python. Two small,
single-purpose Crews are also easier to test and reason about in isolation
than one larger multi-agent Crew.

Both Tasks use output_pydantic (a real, verified CrewAI 1.15.16 Task
field) so the Crew's output is a structured ResearchOutput/PlanOutput
object, not free text the caller has to parse — matching this project's
consistent "structured contracts over parsed text" convention.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from crewai import Crew, Process, Task

from app.agents.planner import build_planner_agent
from app.agents.researcher import (
    build_formatter_agent,
    build_researcher_agent,
    build_researcher_llm,
)
from app.agents.schemas import BudgetCheckResult, PlanOutput, ResearchOutput
from app.config import settings

if TYPE_CHECKING:
    from crewai.tasks.task_output import TaskOutput

# Every research Task below is given a matching `name=` (see each Task's
# name= kwarg) so a task_callback can identify which one just finished via
# TaskOutput.name -- output.agent can't be used for this, since all five
# gather agents share the same role ("Travel Researcher", see
# build_researcher_agent()) and only differ in which LLM backs them.
RESEARCH_TASK_NAMES = ("flight", "hotel", "car_rental", "context", "format")


def build_research_crew(
    trip_request_summary: str,
    car_rental_override: str | None = None,
    task_callback: Callable[[TaskOutput], None] | None = None,
) -> Crew:
    """Build the single-agent Crew that researches flights/hotels/weather/
    visa/ground-transport for a trip.

    `trip_request_summary` is a plain-language description of what's being
    researched (origin, destination, dates, adults, budget, etc.) — the
    caller (flow.py) is responsible for producing it from the actual Trip/
    request data before calling this.

    `car_rental_override`, when given, is appended ONLY to the car rental
    task's description (not the shared trip_request_summary, so it doesn't
    leak into flight/hotel/context tasks that have no use for it) -- see
    TripPlanningState.car_rental_sandbox_test_latitude/longitude in
    flow.py for why this exists (Duffel's sandbox has no car rental
    inventory at any real city, only at one fixed test coordinate).
    """
    # Each gather task gets its OWN Agent instance (same role/tools, a
    # potentially different LLM) rather than sharing one "researcher"
    # Agent -- an Agent's llm is fixed at construction, so per-task-category
    # provider routing (see config.py's resolve_research_provider) requires
    # separate instances. Money-committing, hallucination-sensitive tasks
    # (flight/car) default toward a stronger hosted provider; lower-stakes,
    # informational tasks (hotel, weather/visa/ground-transport) can run on
    # a cheaper/local one -- controlled entirely via .env, defaulting to
    # llm_provider for anyone who hasn't set the per-task overrides.
    flight_agent = build_researcher_agent(
        llm=build_researcher_llm(
            settings.resolve_research_provider(settings.research_flight_llm_provider)
        )
    )
    hotel_agent = build_researcher_agent(
        llm=build_researcher_llm(
            settings.resolve_research_provider(settings.research_hotel_llm_provider)
        )
    )
    car_agent = build_researcher_agent(
        llm=build_researcher_llm(
            settings.resolve_research_provider(settings.research_car_llm_provider)
        )
    )
    context_agent = build_researcher_agent(
        llm=build_researcher_llm(
            settings.resolve_research_provider(settings.research_context_llm_provider)
        )
    )
    formatter = build_formatter_agent(
        llm=build_researcher_llm(
            settings.resolve_research_provider(settings.research_formatter_llm_provider),
            temperature=0.0,
        )
    )

    # Split into small, focused tasks rather than one Task doing everything.
    # Two lessons learned the hard way, both from live runs against a small
    # local model (qwen3:14b) and documented in git history:
    #
    # 1. Asking a model to run a multi-step tool-use loop AND satisfy a
    #    strict structured-output contract in the SAME turn makes it skip
    #    the tools entirely and hallucinate values that merely fit the
    #    schema shape. So only the LAST task (format_task) uses
    #    output_pydantic; every other task produces plain text.
    # 2. Even without output_pydantic, asking a model to juggle 6+ different
    #    tools and decide which apply in one turn makes it give up early
    #    ("this info is missing") or fabricate a plausible-looking summary
    #    instead of actually calling every tool. So each gather task below
    #    is scoped to ONE tool-domain — flight OR hotel OR car rental OR
    #    weather/visa/ground-transport — which is a small enough job for
    #    the model to reliably actually do instead of shortcut.
    #
    # format_task's context is left unset (CrewAI's default) rather than
    # pinned to a specific task list, so it automatically receives every
    # prior task's raw output — see Crew._get_context()/NOT_SPECIFIED in
    # crewai/crew.py and crewai/task.py.
    flight_task = Task(
        name="flight",
        description=(
            "Find the single best flight offer for this trip request using the "
            "Search Flights tool.\n\n"
            f"Trip request:\n{trip_request_summary}\n\n"
            "Use the tool to search for real offers — do not invent an offer. "
            "Report the winning offer's full details exactly as the tool returned "
            "them (offer_id, price, cabin class, every segment, expires_at). If no "
            "offer was found or none fit the budget, say so explicitly instead of "
            "guessing.\n\n"
            "You MUST actually call the tool and copy its real JSON response — "
            "never answer from what a typical flight offer looks like based on "
            "prior knowledge. A reliable check: every date you report (departure, "
            "arrival, expires_at) must fall on or after the trip request's own "
            "departure date above. If any date you're about to report is from a "
            "different year than the trip request, you have NOT used the tool's "
            "real output — call the tool again and report what it actually "
            "returned."
        ),
        expected_output=(
            "A plain-text report of the single best flight offer with all its "
            "fields and its exact offer_id, or an explicit statement that none "
            "was found."
        ),
        agent=flight_agent,
    )

    hotel_task = Task(
        name="hotel",
        description=(
            "Find the single best hotel listing for this trip request using the "
            "Search Hotels tool.\n\n"
            f"Trip request:\n{trip_request_summary}\n\n"
            "Use the tool to search for real listings — do not invent one. Report "
            "the winning listing's full details exactly as the tool returned them "
            "(search_result_id, name, price, nights, expires_at). If no listing was "
            "found or none fit the budget, say so explicitly instead of guessing.\n\n"
            "When you call the tool, include EVERY one of its parameters in the "
            "call explicitly — pass null for any you don't need rather than "
            "omitting them. Some providers reject a tool call outright if any "
            "optional parameter is left out entirely.\n\n"
            "You MUST actually call the tool and copy its real JSON response — "
            "never answer from what a typical hotel listing looks like based on "
            "prior knowledge. A reliable check: the check-in date you report must "
            "match the trip request's own check-in date above. If it's from a "
            "different year, you have NOT used the tool's real output — call the "
            "tool again and report what it actually returned."
        ),
        expected_output=(
            "A plain-text report of the single best hotel listing with all its "
            "fields and its exact search_result_id, or an explicit statement that "
            "none was found."
        ),
        agent=hotel_agent,
    )

    car_rental_task = Task(
        name="car_rental",
        description=(
            "Decide whether a car rental is useful for this trip request, using "
            "the Search Car Rentals tool (and Get Car Rental Quote if you select "
            "a rate).\n\n"
            f"Trip request:\n{trip_request_summary}\n\n"
            "If the trip request explicitly says the traveler has requested a car "
            "rental, you MUST search for one and select a rate. Otherwise, only "
            "search for one if it would genuinely help the traveler get around at "
            "the destination — if a car rental isn't needed, say so explicitly "
            "rather than searching for one anyway. Use the tool to search for a "
            "real rate — do not invent one. You may select MORE THAN ONE car "
            "rental if the trip genuinely needs it — e.g. one to reach the "
            "departure airport, a separate one to get around at the destination. "
            "Each must have clearly different pickup/dropoff locations or times; "
            "don't select more rentals than the trip actually needs. Report each "
            "winning rate's full details exactly as the tool returned them "
            "(rate_id, supplier, price, pickup/dropoff location and time). If "
            "nothing suitable was found, say so explicitly instead of guessing.\n\n"
            "When you call the tool, include EVERY one of its parameters in the "
            "call explicitly — pass null for any you don't need rather than "
            "omitting them. Some providers reject a tool call outright if any "
            "optional parameter is left out entirely.\n\n"
            "You MUST actually call the tool and copy its real JSON response — "
            "never answer from what a typical car rental listing looks like based "
            "on prior knowledge. A reliable check: the pickup date you report must "
            "match the trip request's own dates above. If it's from a different "
            "year, you have NOT used the tool's real output — call the tool again "
            "and report what it actually returned."
            + (f"\n\n{car_rental_override}" if car_rental_override else "")
        ),
        expected_output=(
            "A plain-text report of the best car rental rate(s) — one per "
            "distinct leg that genuinely needs a rental — with all fields and "
            "exact rate_ids, or an explicit statement that a car rental isn't "
            "needed or none was found."
        ),
        agent=car_agent,
    )

    context_task = Task(
        name="context",
        description=(
            "Gather weather, visa, and ground-transport context for this trip "
            "request using the Get Weather Forecast, Check Visa Requirements, and "
            "Estimate Ground Transport tools.\n\n"
            f"Trip request:\n{trip_request_summary}\n\n"
            "The trip request does not include the traveler's passport country. "
            "If Check Visa Requirements needs it, do not guess a nationality — "
            "instead report that visa requirements are unknown without a passport "
            "country. If the weather tool's result has is_climate_average set to "
            "true, it is NOT a real forecast — it's a historical average for this "
            "time of year. Report it as such and relay its disclaimer field "
            "verbatim; never present it as an actual prediction for the trip dates. "
            "If the weather tool errors outright, report that plainly rather than "
            "inventing a forecast. Report all three findings in plain text; do not "
            "invent any of them."
        ),
        expected_output=(
            "A plain-text weather summary, visa summary (or a note that it's "
            "unknown without passport country), and ground-transport cost note."
        ),
        agent=context_agent,
    )

    format_task = Task(
        name="format",
        description=(
            "Convert the research findings given to you as context into the "
            "required structured format. Do not call any tools and do not invent "
            "or change any values — copy the IDs, prices, dates, and text from the "
            "findings exactly. If a finding explicitly says nothing was found or "
            "nothing is needed, leave that field null rather than inventing "
            "something to fill it. This also applies when a finding describes an "
            "option (a hotel, a car) but its report text never actually states a "
            "real search_result_id/rate_id: you MUST leave the WHOLE object out "
            "(selected_hotel null, or that car rental left out of "
            "selected_car_rentals entirely) in that case — never fill an id field "
            "with a placeholder like 'unknown', 'N/A', 'TBD', or "
            "any other non-real value. A placeholder id is exactly as dangerous "
            "as a fabricated one: this project re-fetches every id from the real "
            "provider before ever proposing a booking, so any id that isn't a "
            "real one copied verbatim from the findings will fail that check and "
            "silently block the booking instead of surfacing that the id was "
            "simply never reported."
        ),
        expected_output=(
            "A ResearchOutput with the single best flight offer and hotel listing "
            "(or null if none fit), zero or more car rental rates in "
            "selected_car_rentals (empty if none needed), plus weather, visa, and "
            "ground-transport summaries."
        ),
        agent=formatter,
        output_pydantic=ResearchOutput,
    )

    return Crew(
        agents=[flight_agent, hotel_agent, car_agent, context_agent, formatter],
        tasks=[flight_task, hotel_task, car_rental_task, context_task, format_task],
        process=Process.sequential,
        verbose=True,
        task_callback=task_callback,
    )


def build_planning_crew(
    trip_request_summary: str,
    research_output: ResearchOutput,
    budget_check: BudgetCheckResult,
) -> Crew:
    """Build the single-agent Crew that turns research + budget results
    into a traveler-facing itinerary."""
    planner = build_planner_agent()

    planning_task = Task(
        description=(
            "Turn this research and budget check into a clear itinerary for the "
            "traveler.\n\n"
            f"Trip request:\n{trip_request_summary}\n\n"
            f"Research findings:\n{research_output.model_dump_json(indent=2)}\n\n"
            f"Budget check:\n{budget_check.model_dump_json(indent=2)}\n\n"
            "Always include every relevant disclaimer/estimate caveat from the "
            "research (visa disclaimer, hotel price being an estimate, ground "
            "transport being a rough figure, car rental rate being an estimate if "
            "one was selected) explicitly in your caveats list, and state plainly "
            "if the plan is over budget."
        ),
        expected_output=(
            "A PlanOutput with an itinerary_summary, a day_by_day list, and a "
            "caveats list covering every disclaimer/estimate/budget issue."
        ),
        agent=planner,
        output_pydantic=PlanOutput,
    )

    return Crew(
        agents=[planner],
        tasks=[planning_task],
        process=Process.sequential,
        verbose=True,
    )
