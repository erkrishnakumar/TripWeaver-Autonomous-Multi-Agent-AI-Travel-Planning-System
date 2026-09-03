"""
researcher.py — the CrewAI Agent that calls search_flights/search_hotels/
get_weather_forecast/check_visa_requirements/estimate_ground_transport.

A factory function (build_researcher_agent()), not a module-level
singleton, so tests can construct a fresh Agent per test without shared
mutable state, and so the LLM/tool wiring is explicit at the call site
rather than hidden behind import-time side effects.

Defaults to Ollama (local-first, per app/config.py's ollama_base_url/
ollama_model settings) — matching the project's stated local-first
architecture. Set LLM_PROVIDER=groq or LLM_PROVIDER=gemini in .env to
switch both the researcher and planner agents to a hosted model instead
(e.g. for a quick end-to-end test when the local model is too small to
reason reliably through multi-step tool calls). This is independent of
Groq's OTHER, narrow use in check_visa_requirements() (app/tools/visa.py),
which always calls Groq directly regardless of this setting.
"""

from __future__ import annotations

from crewai import LLM, Agent

from app.agents.tools import ALL_RESEARCH_TOOLS
from app.config import settings


def build_researcher_llm(provider: str | None = None, temperature: float | None = None) -> LLM:
    """Builds the LLM shared by both the researcher and planner agents
    (planner.py imports this function directly rather than duplicating the
    provider-selection logic). Provider is chosen via LLM_PROVIDER by
    default; unset or unrecognized values default to Ollama, never silently
    to a cloud provider. Pass an explicit `provider` to override this for
    one specific caller -- planner.py uses this to optionally run on a
    different provider than the researcher (PLANNER_LLM_PROVIDER).

    `temperature` defaults to each provider's own default (None -> not
    passed) but can be pinned low for a call site that needs deterministic
    copying rather than creative reasoning -- crew.py's format_task uses
    temperature=0.0 for exactly this, since asking the model to transcribe
    prior findings into a strict schema is a copy operation, not one that
    benefits from sampling variance."""
    provider = provider or settings.llm_provider

    if provider == "groq":
        if not settings.groq_api_key:
            raise RuntimeError(
                "LLM_PROVIDER=groq requires GROQ_API_KEY to be set in .env "
                "(get a free key from https://console.groq.com/keys)."
            )
        return LLM(
            model=f"groq/{settings.groq_agent_model}",
            api_key=settings.groq_api_key,
            temperature=temperature,
        )

    if provider == "gemini":
        if not settings.gemini_api_key:
            raise RuntimeError(
                "LLM_PROVIDER=gemini requires GEMINI_API_KEY to be set in .env "
                "(get a free key from https://aistudio.google.com/apikey)."
            )
        return LLM(
            model=f"gemini/{settings.gemini_model}",
            api_key=settings.gemini_api_key,
            temperature=temperature,
        )

    if provider != "ollama":
        raise RuntimeError(
            f"Unknown LLM_PROVIDER '{provider}' -- expected 'ollama', 'groq', or 'gemini'."
        )

    return LLM(
        model=f"ollama/{settings.ollama_model}",
        base_url=settings.ollama_base_url,
        temperature=temperature,
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
            "a rough ground-transport cost note. Also consider a car rental if it "
            "would genuinely help the traveler get around at the destination — pick "
            "one or more rates if genuinely needed for distinct legs of the trip "
            "(e.g. home to the departure airport, then a separate rental at the "
            "destination), leave the list empty otherwise; car rentals are optional, "
            "unlike the flight and hotel. Prefer options within the traveler's "
            "stated budget when possible, and be explicit when nothing fits."
        ),
        backstory=(
            "An experienced, meticulous travel researcher who always checks weather "
            "and visa requirements alongside flights and hotels, considers a rental "
            "car only when it's actually useful for the trip, and who never presents "
            "a hotel price, car rental rate, or ground-transport figure as a "
            "guaranteed final cost — only as what the underlying tool actually "
            "returned."
        ),
        tools=ALL_RESEARCH_TOOLS,
        llm=llm or build_researcher_llm(),
        verbose=True,
    )


def build_formatter_agent(llm: LLM | None = None) -> Agent:
    """Build a second, tool-less Agent whose only job is transcribing the
    researcher's plain-text findings into ResearchOutput's structured shape
    (used by crew.py's format_task).

    Separate from build_researcher_agent() -- and pinned to temperature=0
    by default -- because this is a copy operation (preserve IDs, prices,
    dates exactly), not a creative or reasoning one; sampling variance here
    is what was observed silently paraphrasing/dropping required nested
    fields (e.g. a FlightOffer's segments) instead of copying them
    verbatim."""
    return Agent(
        role="Research Formatter",
        goal=(
            "Transcribe the researcher's findings into the exact structured output "
            "shape required, without inventing, changing, or paraphrasing any value."
        ),
        backstory=(
            "A meticulous transcriptionist who copies IDs, prices, dates, and text "
            "exactly as given, never summarizing or filling in a plausible-looking "
            "value for something that wasn't provided."
        ),
        tools=[],
        llm=llm or build_researcher_llm(temperature=0.0),
        verbose=True,
    )
