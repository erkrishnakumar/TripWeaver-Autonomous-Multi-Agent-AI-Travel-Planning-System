"""
planner.py — the CrewAI Agent that sequences the researched, budget-
checked options into a traveler-facing itinerary.

Has NO tools — by design. The planner reasons over what the researcher
already found (plus the budget check's verdict) and produces prose/
structure; it doesn't need to call any external API or DB itself. Giving
it tools it doesn't need would just widen the surface for the LLM to do
something unexpected.
"""

from __future__ import annotations

from crewai import LLM, Agent

from app.agents.researcher import build_researcher_llm


def build_planner_agent(llm: LLM | None = None) -> Agent:
    """Build the itinerary-planner Agent.

    Reuses build_researcher_llm()'s Ollama configuration rather than
    duplicating it — both agents reason with the same local model in this
    phase; that's a starting point, not a permanent constraint (a future
    phase could give the planner a different/larger model without
    touching the researcher).
    """
    return Agent(
        role="Itinerary Planner",
        goal=(
            "Turn the researcher's findings and the budget check's verdict into a "
            "clear, traveler-facing itinerary summary, a rough day-by-day plan, and "
            "an explicit list of caveats the traveler should know before approving "
            "(e.g. a visa disclaimer, an estimated hotel price, a rough ground-"
            "transport figure, or being over budget)."
        ),
        backstory=(
            "A careful trip planner who never hides an estimate behind confident "
            "language — every uncertain figure or non-authoritative source gets "
            "surfaced explicitly as a caveat, not smoothed over."
        ),
        tools=[],
        llm=llm or build_researcher_llm(),
        verbose=False,
    )
