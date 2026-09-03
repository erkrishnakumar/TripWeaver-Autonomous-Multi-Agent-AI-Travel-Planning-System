"""
Phase 7 — Agent evals: fixed, recorded scenarios encoding real LLM/provider
failure modes that were actually observed live (against real Groq/Gemini/
Ollama runs) during this project's development, turned into deterministic
regression tests.

WHAT THIS IS: each test below is a "replay this exact real incident and
assert the safety net still catches it" scenario — the same "mock the LLM/
network boundary, run everything else for real" convention tests/test_flow.py
already uses (real in-memory SQLite writes, real budget-check logic, real
approval-gate control flow). A short comment on each test class names the
specific real incident it's a regression test for.

WHAT THIS IS NOT (yet): a live-LLM-output-quality eval suite that actually
calls a real provider and grades the response. That would need real API
keys in CI, would be non-deterministic across model/provider updates, and
would be slow/costly to run on every push — a natural v2 for this phase,
not a substitute for having *some* automated protection against known
failure modes today. See docs/TripWeaver_Roadmap.md's Phase 7 entry.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta

import pytest
import pytest_asyncio
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.agents.crew as crew_module
import app.agents.flow as flow_module
from app.agents.schemas import PlanOutput, ResearchOutput
from app.db.base import Base
from app.db.models import Booking
from app.tools.schemas import (
    CabinClass,
    CarRateOption,
    CarRentalPaymentType,
    FlightOffer,
    FlightSegment,
    ToolError,
)

# NOTE: no module-level `pytestmark = pytest.mark.asyncio` here (unlike
# test_flow.py) -- pyproject.toml's asyncio_mode = "auto" already detects
# async tests without it, and this file deliberately mixes async (full
# Flow.kickoff scenarios) and sync (pure schema/prompt-string) tests; a
# blanket module-level mark applied to the sync ones raises a pytest
# warning ("marked with asyncio but it is not an async function").


# ---------------------------------------------------------------------------
# Shared fixtures/helpers — deliberately self-contained (not imported from
# tests/test_flow.py) so this file stays independent of that suite's helpers
# changing shape later, matching this project's existing per-file convention.
# ---------------------------------------------------------------------------


class _FakeCrewOutput:
    def __init__(self, pydantic_obj):
        self.pydantic = pydantic_obj


class _FakeCrew:
    def __init__(self, pydantic_obj):
        self._pydantic_obj = pydantic_obj

    async def kickoff_async(self):
        return _FakeCrewOutput(self._pydantic_obj)


class _ExplodingCrew:
    """Simulates a real provider rate-limit/API error breaking out of
    crew.kickoff_async() as a bare exception, not a domain ToolError or a
    pydantic ValidationError — verified live: both Gemini's
    RESOURCE_EXHAUSTED and Groq's RateLimitError surface exactly this way,
    from deep inside CrewAI's internals."""

    def __init__(self, exc: Exception):
        self._exc = exc

    async def kickoff_async(self):
        raise self._exc


def _flight_offer(offer_id: str = "off_eval_test") -> FlightOffer:
    return FlightOffer(
        offer_id=offer_id,
        total_price_usd=400.0,
        cabin_class=CabinClass.ECONOMY,
        stops_outbound=0,
        segments=[
            FlightSegment(
                carrier_iata="DL",
                carrier_name="Delta",
                flight_number="123",
                origin_iata="JFK",
                destination_iata="ATL",
                departs_at="2026-09-14T08:00:00",
                arrives_at="2026-09-14T10:30:00",
            )
        ],
    )


def _car_rate(rate_id: str = "rat_eval_test") -> CarRateOption:
    return CarRateOption(
        rate_id=rate_id,
        car_description="Compact - Toyota Corolla or similar",
        supplier_name="Hertz",
        payment_type=CarRentalPaymentType.PREPAID,
        estimated_price_total_usd=150.0,
        pickup_location_name="Atlanta",
        dropoff_location_name="Atlanta",
        pickup_at=datetime(2026, 9, 14, 10, 0),
        dropoff_at=datetime(2026, 9, 17, 10, 0),
    )


def _driver_kwargs() -> dict:
    return {
        "driver_given_name": "Jane",
        "driver_family_name": "Doe",
        "driver_date_of_birth": "1990-01-01",
        "driver_email": "jane.doe@example.com",
        "driver_phone_number": "+15555550100",
    }


def _kickoff_args() -> dict:
    return {
        "origin_iata": "jfk",
        "destination_iata": "atl",
        "depart_date": (date.today() + timedelta(days=30)).isoformat(),
        "return_date": (date.today() + timedelta(days=33)).isoformat(),
        "adults": 1,
        "max_budget_usd": 1000.0,
        "requester_email": "test@example.com",
    }


async def _kickoff_and_approve(flow: flow_module.TripPlanningFlow, inputs: dict) -> None:
    """Runs kickoff() (research -> check_budget -> plan ->
    mark_awaiting_approval), then manually chains the two steps that used
    to auto-run via @listen before flow.py's Celery-resumability redesign
    removed them from the automated chain -- a worker has no terminal to
    block on input() with, so real approval now comes from a separate HTTP
    call, not from kickoff() completing. Skips the continuation if
    research/plan already failed, matching run_trip_planning_flow()'s CLI
    entrypoint's own guard."""
    flow.kickoff(inputs=inputs)
    if flow.state.error is None:
        approved = flow.wait_for_human_approval(flow.state.trip_id)
        await flow.propose_bookings(approved)


@pytest_asyncio.fixture
async def sqlite_session_override(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    @asynccontextmanager
    async def _get_session():
        async with session_factory() as session:
            yield session

    monkeypatch.setattr(flow_module, "get_session", _get_session)
    yield session_factory
    await engine.dispose()


def _patch_crews(monkeypatch, research_output: ResearchOutput, plan_output: PlanOutput):
    monkeypatch.setattr(
        flow_module,
        "build_research_crew",
        lambda summary, car_rental_override=None: _FakeCrew(research_output),
    )
    monkeypatch.setattr(
        flow_module,
        "build_planning_crew",
        lambda summary, research, budget: _FakeCrew(plan_output),
    )


# ---------------------------------------------------------------------------
# 1. Hotel checkout defaulting — pure logic, no LLM/network involved at all.
# ---------------------------------------------------------------------------


class TestHotelCheckoutDefaulting:
    """Regression test for a real incident: a live run with no return_date
    came back with selected_hotel=null because the hotel task had no
    checkout date to search with at all ("a return date is required" —
    something no amount of prompt tuning or a better model can fix, since
    the information genuinely wasn't there). flow.py's
    _effective_hotel_checkout()/_trip_request_summary() fix this in plain,
    deterministic Python; these tests pin that fix in place."""

    async def test_no_return_date_defaults_to_one_night_stay(self):
        flow = flow_module.TripPlanningFlow()
        flow.state.depart_date = "2026-09-27"
        flow.state.return_date = None
        checkout, was_defaulted = flow._effective_hotel_checkout()
        assert checkout == "2026-09-28"
        assert was_defaulted is True

    async def test_return_date_given_is_used_as_is_not_defaulted(self):
        flow = flow_module.TripPlanningFlow()
        flow.state.depart_date = "2026-09-27"
        flow.state.return_date = "2026-10-02"
        checkout, was_defaulted = flow._effective_hotel_checkout()
        assert checkout == "2026-10-02"
        assert was_defaulted is False

    async def test_trip_summary_tells_the_researcher_the_stay_was_assumed(self):
        flow = flow_module.TripPlanningFlow()
        flow.state.origin_iata = "JFK"
        flow.state.destination_iata = "ATL"
        flow.state.depart_date = "2026-09-27"
        flow.state.return_date = None
        summary = flow._trip_request_summary()
        assert "DEFAULTED" in summary
        assert "check-out 2026-09-28" in summary


# ---------------------------------------------------------------------------
# 2. Graceful degradation when a provider call fails outright (rate limit,
#    quota exhaustion, ...) rather than returning malformed output.
# ---------------------------------------------------------------------------


class TestGracefulDegradationOnProviderErrors:
    """Regression test for a real incident: a Gemini 429 RESOURCE_EXHAUSTED
    (and, separately, a Groq RateLimitError) used to crash the entire Flow
    with a raw traceback, since research()/plan() only used to catch
    ValidationError/AssertionError, not a bare provider exception. See
    those methods' docstrings in flow.py for the fix."""

    async def test_research_step_survives_a_provider_rate_limit(
        self, monkeypatch, sqlite_session_override
    ):
        monkeypatch.setattr(
            flow_module,
            "build_research_crew",
            lambda summary, car_rental_override=None: _ExplodingCrew(
                RuntimeError("429 RESOURCE_EXHAUSTED: quota exceeded")
            ),
        )
        monkeypatch.setattr(
            flow_module,
            "build_planning_crew",
            lambda summary, research, budget: _FakeCrew(
                PlanOutput(itinerary_summary="(plan could not be generated)")
            ),
        )
        monkeypatch.setattr("builtins.input", lambda _: "n")

        flow = flow_module.TripPlanningFlow()
        flow.kickoff(inputs=_kickoff_args())  # must not raise

        assert flow.state.research_output is not None
        assert flow.state.research_output.selected_flight is None
        assert flow.state.error is not None
        assert "Research step failed" in flow.state.error

    async def test_planning_step_survives_a_provider_rate_limit(
        self, monkeypatch, sqlite_session_override
    ):
        research_output = ResearchOutput(selected_flight=_flight_offer())
        monkeypatch.setattr(
            flow_module,
            "build_research_crew",
            lambda summary, car_rental_override=None: _FakeCrew(research_output),
        )
        monkeypatch.setattr(
            flow_module,
            "build_planning_crew",
            lambda summary, research, budget: _ExplodingCrew(
                RuntimeError("Groq RateLimitError: tokens per minute exceeded")
            ),
        )
        monkeypatch.setattr("builtins.input", lambda _: "n")

        flow = flow_module.TripPlanningFlow()
        flow.kickoff(inputs=_kickoff_args())  # must not raise

        assert flow.state.plan is not None
        assert flow.state.plan.itinerary_summary == "(plan could not be generated)"
        assert flow.state.error is not None
        assert "Planning step failed" in flow.state.error


# ---------------------------------------------------------------------------
# 3. Fabricated/placeholder car rental ids — same guard family as
#    TestHallucinationGuard in test_flow.py (flight/hotel), extended to
#    cover car rentals and the placeholder-id variant specifically.
# ---------------------------------------------------------------------------


class TestHallucinationGuardCarRental:
    """propose_bookings() re-fetches a firm quote via get_car_rental_quote()
    before ever proposing a car rental booking, never trusting the
    researcher/formatter-selected object directly."""

    async def test_fabricated_car_rate_is_rejected_not_proposed(
        self, monkeypatch, sqlite_session_override
    ):
        research_output = ResearchOutput(selected_car_rental=_car_rate())
        plan_output = PlanOutput(itinerary_summary="Trip summary.")
        _patch_crews(monkeypatch, research_output, plan_output)
        monkeypatch.setattr(
            flow_module,
            "get_car_rental_quote",
            lambda rate_id: ToolError(
                tool_name="get_car_rental_quote",
                error_type="rate_not_found",
                message="Duffel Cars API returned 404: not found.",
                retryable=False,
            ),
        )
        monkeypatch.setattr("builtins.input", lambda _: "y")

        flow = flow_module.TripPlanningFlow()
        await _kickoff_and_approve(flow, {**_kickoff_args(), **_driver_kwargs()})
        state = flow.state

        assert state.car_rental_booking is None
        assert state.error is not None
        assert "Car rental quote failed" in state.error

        session_factory = sqlite_session_override
        async with session_factory() as session:
            bookings = (await session.execute(select(Booking))).scalars().all()
            assert len(bookings) == 0

    async def test_placeholder_looking_rate_id_is_equally_rejected(
        self, monkeypatch, sqlite_session_override
    ):
        """Regression test for a real incident: the formatter emitted a
        placeholder-style id ('rate_12345') instead of leaving
        selected_car_rental null when the researcher genuinely found no
        rate. A placeholder id is exactly as dangerous as a fully invented
        one — this asserts it's caught the same way (real provider
        verification), not by pattern-matching what the id looks like."""
        research_output = ResearchOutput(selected_car_rental=_car_rate(rate_id="rate_12345"))
        plan_output = PlanOutput(itinerary_summary="Trip summary.")
        _patch_crews(monkeypatch, research_output, plan_output)
        monkeypatch.setattr(
            flow_module,
            "get_car_rental_quote",
            lambda rate_id: ToolError(
                tool_name="get_car_rental_quote",
                error_type="rate_not_found",
                message="Duffel Cars API returned 404: not found.",
                retryable=False,
            ),
        )
        monkeypatch.setattr("builtins.input", lambda _: "y")

        flow = flow_module.TripPlanningFlow()
        await _kickoff_and_approve(flow, {**_kickoff_args(), **_driver_kwargs()})

        assert flow.state.car_rental_booking is None
        assert flow.state.error is not None
        assert "Car rental quote failed" in flow.state.error


# ---------------------------------------------------------------------------
# 4. ResearchOutput's {} -> None coercion, at the schema level.
# ---------------------------------------------------------------------------


class TestResearchOutputEmptyDictCoercion:
    """Regression test for a real incident: a local Ollama model formatted
    "nothing found" as {} instead of null for selected_flight/
    selected_hotel/selected_car_rental — an empty dict otherwise fails
    validation against FlightOffer/HotelListing/CarRateOption's required
    fields with a confusing error instead of just meaning null."""

    def test_empty_dict_for_each_optional_field_becomes_none(self):
        output = ResearchOutput.model_validate(
            {
                "selected_flight": {},
                "selected_hotel": {},
                "selected_car_rental": {},
                "weather_summary": "sunny",
            }
        )
        assert output.selected_flight is None
        assert output.selected_hotel is None
        assert output.selected_car_rental is None
        assert output.weather_summary == "sunny"

    def test_partially_filled_dict_still_fails_loudly(self):
        """Only a fully EMPTY dict means "nothing found" — a partially
        filled one is real (if incomplete/hallucinated) data and must still
        fail validation, not be silently swallowed to null too."""
        with pytest.raises(ValidationError):
            ResearchOutput.model_validate({"selected_flight": {"offer_id": "off_123"}})


# ---------------------------------------------------------------------------
# 5. Prompt-level safeguards actually present in the built Crew — cheap,
#    deterministic checks that can't verify an LLM obeys them, but do catch
#    someone accidentally deleting the instruction in a future refactor.
# ---------------------------------------------------------------------------


class TestCrewPromptSafeguards:
    def test_formatter_task_forbids_placeholder_ids(self):
        """Regression test for a real incident: the formatter once filled
        an id field with the literal string 'unknown' instead of leaving
        the whole object null. See crew.py's format_task description."""
        crew = crew_module.build_research_crew("JFK -> ATL, departing 2026-09-27")
        format_task = next(t for t in crew.tasks if t.agent.role == "Research Formatter")
        assert "placeholder" in format_task.description.lower()

    def test_car_rental_sandbox_override_reaches_only_the_car_task(self):
        """Regression test for the testing-only Duffel-sandbox-coordinate
        override (see TripPlanningState.car_rental_sandbox_test_latitude/
        longitude in flow.py) — it must land in the car rental task's
        description only, never in flight/hotel/context/formatter, which
        have no use for it and would only get confused by it."""
        note = "TESTING OVERRIDE: use pickup_latitude=-24.38"
        crew = crew_module.build_research_crew(
            "JFK -> ATL, departing 2026-09-27", car_rental_override=note
        )
        flight_task, hotel_task, car_rental_task, context_task, format_task = crew.tasks
        assert note in car_rental_task.description
        assert note not in flight_task.description
        assert note not in hotel_task.description
        assert note not in context_task.description
        assert note not in format_task.description

    def test_gather_tasks_warn_against_answering_from_memory(self):
        """Regression test for a real incident (2026-09-03): a live run on
        qwen3:14b produced a fully fabricated research_completed payload --
        placeholder-shaped ids (FL123456, HOT789012, CAR112233) AND every
        date stamped 2023 instead of the requested 2026 trip, meaning the
        model answered from memorized "what a typical offer looks like"
        knowledge instead of actually using its tool's real response. The
        hallucination guard (re-fetch by id before ever proposing a
        booking) still caught it downstream, but this pins the prompt-level
        defense added at the source: flight/hotel/car_rental tasks must all
        warn against this specific failure mode, not just "don't invent an
        offer" generically."""
        crew = crew_module.build_research_crew("JFK -> ATL, departing 2026-09-27")
        flight_task, hotel_task, car_rental_task, context_task, format_task = crew.tasks
        for task in (flight_task, hotel_task, car_rental_task):
            assert "prior knowledge" in task.description.lower()
            assert "different year" in task.description.lower()
