"""
researcher.py — the CrewAI Agent that calls search_flights/search_hotels/
get_weather_forecast/check_visa_requirements/estimate_ground_transport.

A factory function (build_researcher_agent()), not a module-level
singleton, so tests can construct a fresh Agent per test without shared
mutable state, and so the LLM/tool wiring is explicit at the call site
rather than hidden behind import-time side effects.

Uses Ollama (local-first, per app/config.py's ollama_base_url/ollama_model
settings) — matching the project's stated local-first architecture. Groq
remains the narrow, explicit exception used only by check_visa_requirements
internally; agent reasoning itself stays local.
"""

from __future__ import annotations

from crewai import LLM, Agent

from app.agents.tools import ALL_RESEARCH_TOOLS
from app.config import settings


def build_researcher_llm() -> LLM:
    return LLM(
        model=f"ollama/{settings.ollama_model}",
        base_url=settings.ollama_base_url,
    )


def build_researcher_agent(llm: LLM | None = None) -> Agent:
    """Build the travel-researcher Agent.

    `llm` is accepted as an optional override (rather than always
    constructed internally) so tests can inject a stub/fake LLM without
    needing a real Ollama instance running.
    """
    return Agent(
        role="Travel Researcher",
        goal=(
            "Find one well-matched flight offer and one well-matched hotel listing "
            "for the trip request, along with a weather summary, a visa summary, and "
            "a rough ground-transport cost note. Prefer options within the traveler's "
            "stated budget when possible, and be explicit when nothing fits."
        ),
        backstory=(
            "An experienced, meticulous travel researcher who always checks weather "
            "and visa requirements alongside flights and hotels, and who never "
            "presents a hotel price or a ground-transport figure as a guaranteed "
            "final cost — only as what the underlying tool actually returned."
        ),
        tools=ALL_RESEARCH_TOOLS,
        llm=llm or build_researcher_llm(),
        verbose=False,
    )
