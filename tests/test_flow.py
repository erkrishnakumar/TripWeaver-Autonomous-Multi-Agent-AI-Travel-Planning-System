"""
Tests for app/agents/flow.py — the human-approval-gate Flow.

The Crew.kickoff() calls (research + planning) are monkeypatched with a
fake Crew that returns a canned CrewOutput-like object exposing `.pydantic`
— there is no real LLM available in a test environment, and this project's
other test suites establish the same convention: mock the LLM/network-
dependent boundary, but run everything else (here: real DB writes via a
real in-memory SQLite database, real budget-check logic, the real
approval-gate control flow) for real. See tests/test_mcp_server.py and
tests/test_propose_booking.py for the same principle applied elsewhere.

input() is monkeypatched to simulate the human's y/n answer at the
approval step — this is the CLI placeholder described in flow.py's module
docstring, not a mock of anything that will exist post-Phase-8.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import date, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.agents.flow as flow_module
from app.agents.schemas import PlanOutput, ResearchOutput
from app.db.base import Base
from app.db.models import Booking, Trip
from app.db.models.enums import BookingStatus
from app.tools.schemas import CabinClass, FlightOffer, FlightSegment, HotelListing

pytestmark = pytest.mark.asyncio


class _FakeCrewOutput:
    def __init__(self, pydantic_obj):
        self.pydantic = pydantic_obj


class _FakeCrew:
    def __init__(self, pydantic_obj):
        self._pydantic_obj = pydantic_obj

    def kickoff(self):
        return _FakeCrewOutput(self._pydantic_obj)


def _flight_offer() -> FlightOffer:
    return FlightOffer(
        offer_id="off_flow_test",
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


def _hotel_listing() -> HotelListing:
    return HotelListing(
        search_result_id="srr_flow_test",
        hotel_name="Flow Test Hotel",
        latitude=1.0,
        longitude=1.0,
        estimated_price_total_usd=300.0,
        nights=3,
    )


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
        flow_module, "build_research_crew", lambda summary: _FakeCrew(research_output)
    )
    monkeypatch.setattr(
        flow_module,
        "build_planning_crew",
        lambda summary, research, budget: _FakeCrew(plan_output),
    )


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


class TestTripPlanningFlowApproved:
    async def test_approved_flow_creates_trip_and_proposes_both_bookings(
        self, monkeypatch, sqlite_session_override
    ):
        research_output = ResearchOutput(
            selected_flight=_flight_offer(),
            selected_hotel=_hotel_listing(),
            weather_summary="Sunny, low chance of rain.",
            visa_summary="No visa required for tourism.",
        )
        plan_output = PlanOutput(
            itinerary_summary="A 3-day trip to Atlanta.",
            day_by_day=["Day 1: arrive, check in", "Day 2: explore", "Day 3: depart"],
            caveats=["Hotel price is an estimate.", "Visa info is informational only."],
        )
        _patch_crews(monkeypatch, research_output, plan_output)
        monkeypatch.setattr("builtins.input", lambda _: "y")

        flow = flow_module.TripPlanningFlow()
        flow.kickoff(inputs=_kickoff_args())
        state = flow.state

        assert state.approved is True
        assert state.trip_id != ""
        assert state.flight_booking is not None
        assert state.flight_booking.status == "pending_approval"
        assert state.hotel_booking is not None
        assert state.hotel_booking.status == "pending_approval"
        assert state.error is None

    async def test_trip_row_actually_persisted_in_draft_then_bookings_pending(
        self, monkeypatch, sqlite_session_override
    ):
        research_output = ResearchOutput(
            selected_flight=_flight_offer(), selected_hotel=_hotel_listing()
        )
        plan_output = PlanOutput(itinerary_summary="Trip summary.")
        _patch_crews(monkeypatch, research_output, plan_output)
        monkeypatch.setattr("builtins.input", lambda _: "y")

        flow = flow_module.TripPlanningFlow()
        flow.kickoff(inputs=_kickoff_args())

        session_factory = sqlite_session_override
        async with session_factory() as session:
            trip = (await session.execute(select(Trip))).scalar_one()
            assert trip.origin_iata == "JFK"
            assert trip.destination_iata == "ATL"

            bookings = (await session.execute(select(Booking))).scalars().all()
            assert len(bookings) == 2
            assert all(b.status == BookingStatus.PENDING_APPROVAL for b in bookings)


class TestTripPlanningFlowRejected:
    async def test_rejected_flow_creates_trip_but_no_bookings(
        self, monkeypatch, sqlite_session_override
    ):
        research_output = ResearchOutput(
            selected_flight=_flight_offer(), selected_hotel=_hotel_listing()
        )
        plan_output = PlanOutput(itinerary_summary="Trip summary.")
        _patch_crews(monkeypatch, research_output, plan_output)
        monkeypatch.setattr("builtins.input", lambda _: "n")

        flow = flow_module.TripPlanningFlow()
        flow.kickoff(inputs=_kickoff_args())
        state = flow.state

        assert state.approved is False
        assert state.trip_id != ""  # trip IS created before the approval gate
        assert state.flight_booking is None
        assert state.hotel_booking is None
        assert state.error is not None

        session_factory = sqlite_session_override
        async with session_factory() as session:
            bookings = (await session.execute(select(Booking))).scalars().all()
            assert len(bookings) == 0

    async def test_anything_other_than_lowercase_y_is_treated_as_rejection(
        self, monkeypatch, sqlite_session_override
    ):
        research_output = ResearchOutput(selected_flight=_flight_offer())
        plan_output = PlanOutput(itinerary_summary="Trip summary.")
        _patch_crews(monkeypatch, research_output, plan_output)
        monkeypatch.setattr("builtins.input", lambda _: "")  # just pressing enter

        flow = flow_module.TripPlanningFlow()
        flow.kickoff(inputs=_kickoff_args())

        assert flow.state.approved is False


class TestTripPlanningFlowPartialSelection:
    async def test_only_flight_selected_only_proposes_flight_booking(
        self, monkeypatch, sqlite_session_override
    ):
        research_output = ResearchOutput(selected_flight=_flight_offer(), selected_hotel=None)
        plan_output = PlanOutput(itinerary_summary="Flight-only trip.")
        _patch_crews(monkeypatch, research_output, plan_output)
        monkeypatch.setattr("builtins.input", lambda _: "y")

        flow = flow_module.TripPlanningFlow()
        flow.kickoff(inputs=_kickoff_args())
        state = flow.state

        assert state.flight_booking is not None
        assert state.hotel_booking is None


class TestBudgetIntegration:
    async def test_over_budget_still_produces_a_plan_with_verdict_surfaced(
        self, monkeypatch, sqlite_session_override
    ):
        expensive_flight = _flight_offer().model_copy(update={"total_price_usd": 5000.0})
        research_output = ResearchOutput(selected_flight=expensive_flight, selected_hotel=None)
        plan_output = PlanOutput(itinerary_summary="Over-budget trip.")
        _patch_crews(monkeypatch, research_output, plan_output)
        monkeypatch.setattr("builtins.input", lambda _: "n")  # human sees it's over, rejects

        flow = flow_module.TripPlanningFlow()
        flow.kickoff(inputs=_kickoff_args())
        state = flow.state

        assert state.budget_check is not None
        assert state.budget_check.within_budget is False
        assert "over the" in state.budget_check.message
