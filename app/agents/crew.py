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

from crewai import Crew, Process, Task

from app.agents.planner import build_planner_agent
from app.agents.researcher import build_researcher_agent
from app.agents.schemas import BudgetCheckResult, PlanOutput, ResearchOutput


def build_research_crew(trip_request_summary: str) -> Crew:
    """Build the single-agent Crew that researches flights/hotels/weather/
    visa/ground-transport for a trip.

    `trip_request_summary` is a plain-language description of what's being
    researched (origin, destination, dates, adults, budget, etc.) — the
    caller (flow.py) is responsible for producing it from the actual Trip/
    request data before calling this.
    """
    researcher = build_researcher_agent()

    research_task = Task(
        description=(
            "Research this trip request and find the single best flight offer and "
            "single best hotel listing, plus a weather summary, a visa summary, and "
            "a rough ground-transport cost note for getting to/from the airport.\n\n"
            f"Trip request:\n{trip_request_summary}\n\n"
            "Use your tools to search for real options — do not invent flight or "
            "hotel data. If nothing suitable is found for flights or hotels, leave "
            "that field null rather than guessing."
        ),
        expected_output=(
            "A ResearchOutput with your single best flight offer and hotel listing "
            "(or null if none fit), plus weather, visa, and ground-transport summaries."
        ),
        agent=researcher,
        output_pydantic=ResearchOutput,
    )

    return Crew(
        agents=[researcher],
        tasks=[research_task],
        process=Process.sequential,
        verbose=False,
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
            "transport being a rough figure) explicitly in your caveats list, and "
            "state plainly if the plan is over budget."
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
        verbose=False,
    )
