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
from datetime import date, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.agents.flow as flow_module
from app.agents.schemas import PlanOutput, ResearchOutput
from app.db.base import Base
from app.db.models import Booking, Trip
from app.db.models.enums import BookingStatus
from app.tools.schemas import (
    CabinClass,
    CarQuoteResult,
    CarRateOption,
    CarRentalPaymentType,
    FlightOffer,
    FlightSegment,
    HotelListing,
    ToolError,
)

pytestmark = pytest.mark.asyncio


class _FakeCrewOutput:
    def __init__(self, pydantic_obj):
        self.pydantic = pydantic_obj


class _FakeCrew:
    def __init__(self, pydantic_obj):
        self._pydantic_obj = pydantic_obj

    def kickoff(self):
        return _FakeCrewOutput(self._pydantic_obj)

    async def kickoff_async(self):
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


def _car_rate() -> CarRateOption:
    return CarRateOption(
        rate_id="rat_flow_test",
        car_description="Compact - Toyota Corolla or similar",
        supplier_name="Hertz",
        payment_type=CarRentalPaymentType.PREPAID,
        estimated_price_total_usd=150.0,
        pickup_location_name="Atlanta",
        dropoff_location_name="Atlanta",
        pickup_at=datetime(2026, 9, 14, 10, 0),
        dropoff_at=datetime(2026, 9, 17, 10, 0),
    )


def _car_quote() -> CarQuoteResult:
    return CarQuoteResult(
        quote_id="qut_flow_test",
        rate_id="rat_flow_test",
        total_price_usd=155.0,
        payment_type=CarRentalPaymentType.PREPAID,
    )


def _driver_kwargs() -> dict:
    return {
        "driver_given_name": "Jane",
        "driver_family_name": "Doe",
        "driver_date_of_birth": "1990-01-01",
        "driver_email": "jane.doe@example.com",
        "driver_phone_number": "+15555550100",
    }


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


def _patch_verification(
    monkeypatch, flight: FlightOffer | None = None, hotel: HotelListing | None = None
) -> None:
    """propose_bookings() now re-verifies a selected flight/hotel by ID via
    get_flight_offer()/get_hotel_rate() before proposing it (a hallucination
    guard — see flow.py's module docstring) rather than trusting the
    researcher-selected object directly. Tests that expect a flight/hotel
    to actually get proposed must simulate that verification succeeding."""
    if flight is not None:
        monkeypatch.setattr(flow_module, "get_flight_offer", lambda offer_id: flight)
    if hotel is not None:
        monkeypatch.setattr(flow_module, "get_hotel_rate", lambda search_result_id, nights: hotel)


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
    call, not from kickoff() completing. Mirrors exactly what
    run_trip_planning_flow()'s CLI entrypoint now does manually. Skips the
    continuation if research/plan already failed, matching that same
    entrypoint's guard -- there's nothing to approve if the plan itself
    couldn't be produced."""
    flow.kickoff(inputs=inputs)
    if flow.state.error is None:
        approved = flow.wait_for_human_approval(flow.state.trip_id)
        await flow.propose_bookings(approved)


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
        _patch_verification(monkeypatch, flight=_flight_offer(), hotel=_hotel_listing())
        monkeypatch.setattr("builtins.input", lambda _: "y")

        flow = flow_module.TripPlanningFlow()
        await _kickoff_and_approve(flow, _kickoff_args())
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
        _patch_verification(monkeypatch, flight=_flight_offer(), hotel=_hotel_listing())
        monkeypatch.setattr("builtins.input", lambda _: "y")

        flow = flow_module.TripPlanningFlow()
        await _kickoff_and_approve(flow, _kickoff_args())

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
        await _kickoff_and_approve(flow, _kickoff_args())
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
        await _kickoff_and_approve(flow, _kickoff_args())

        assert flow.state.approved is False


class TestTripPlanningFlowPartialSelection:
    async def test_only_flight_selected_only_proposes_flight_booking(
        self, monkeypatch, sqlite_session_override
    ):
        research_output = ResearchOutput(selected_flight=_flight_offer(), selected_hotel=None)
        plan_output = PlanOutput(itinerary_summary="Flight-only trip.")
        _patch_crews(monkeypatch, research_output, plan_output)
        _patch_verification(monkeypatch, flight=_flight_offer())
        monkeypatch.setattr("builtins.input", lambda _: "y")

        flow = flow_module.TripPlanningFlow()
        await _kickoff_and_approve(flow, _kickoff_args())
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


class TestTripPlanningFlowCarRental:
    async def test_selected_car_rental_with_driver_details_is_proposed(
        self, monkeypatch, sqlite_session_override
    ):
        research_output = ResearchOutput(selected_car_rental=_car_rate())
        plan_output = PlanOutput(itinerary_summary="Car rental trip.")
        _patch_crews(monkeypatch, research_output, plan_output)
        monkeypatch.setattr(flow_module, "get_car_rental_quote", lambda query: _car_quote())
        monkeypatch.setattr("builtins.input", lambda _: "y")

        flow = flow_module.TripPlanningFlow()
        await _kickoff_and_approve(flow, {**_kickoff_args(), **_driver_kwargs()})
        state = flow.state

        assert state.car_rental_booking is not None
        assert state.car_rental_booking.status == "pending_approval"
        assert state.error is None

        session_factory = sqlite_session_override
        async with session_factory() as session:
            bookings = (await session.execute(select(Booking))).scalars().all()
            assert len(bookings) == 1
            assert bookings[0].status == BookingStatus.PENDING_APPROVAL

    async def test_selected_car_rental_without_driver_details_is_not_proposed(
        self, monkeypatch, sqlite_session_override
    ):
        research_output = ResearchOutput(selected_car_rental=_car_rate())
        plan_output = PlanOutput(itinerary_summary="Car rental trip.")
        _patch_crews(monkeypatch, research_output, plan_output)
        monkeypatch.setattr("builtins.input", lambda _: "y")

        flow = flow_module.TripPlanningFlow()
        await _kickoff_and_approve(flow, _kickoff_args())  # no driver_* kwargs
        state = flow.state

        assert state.car_rental_booking is None
        assert state.error is not None
        assert "driver details were not provided" in state.error

    async def test_car_rental_quote_failure_surfaces_as_error(
        self, monkeypatch, sqlite_session_override
    ):
        research_output = ResearchOutput(selected_car_rental=_car_rate())
        plan_output = PlanOutput(itinerary_summary="Car rental trip.")
        _patch_crews(monkeypatch, research_output, plan_output)
        monkeypatch.setattr(
            flow_module,
            "get_car_rental_quote",
            lambda query: ToolError(
                tool_name="get_car_rental_quote",
                error_type="rate_not_found",
                message="Rate expired.",
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

    async def test_nothing_selected_at_all_surfaces_an_informational_error(
        self, monkeypatch, sqlite_session_override
    ):
        research_output = ResearchOutput()  # flight, hotel, and car rental all None
        plan_output = PlanOutput(itinerary_summary="Nothing found.")
        _patch_crews(monkeypatch, research_output, plan_output)
        monkeypatch.setattr("builtins.input", lambda _: "y")

        flow = flow_module.TripPlanningFlow()
        await _kickoff_and_approve(flow, _kickoff_args())
        state = flow.state

        assert state.flight_booking is None
        assert state.hotel_booking is None
        assert state.car_rental_booking is None
        assert state.error is not None
        assert "nothing was proposed for booking" in state.error


class TestHallucinationGuard:
    """propose_bookings() must re-verify a selected flight/hotel by ID
    before proposing it, never trusting the researcher-selected object
    directly (see flow.py's module docstring). These tests simulate an LLM
    fabricating a plausible-looking but fake offer_id/search_result_id --
    exactly what happened in a real run of this Flow -- and confirm it's
    rejected instead of silently persisted as a real PENDING_APPROVAL row.
    """

    async def test_fabricated_flight_offer_is_rejected_not_proposed(
        self, monkeypatch, sqlite_session_override
    ):
        research_output = ResearchOutput(selected_flight=_flight_offer())
        plan_output = PlanOutput(itinerary_summary="Trip summary.")
        _patch_crews(monkeypatch, research_output, plan_output)
        monkeypatch.setattr(
            flow_module,
            "get_flight_offer",
            lambda offer_id: ToolError(
                tool_name="get_flight_offer",
                error_type="offer_not_found",
                message="Duffel API returned 404: not found.",
                retryable=False,
            ),
        )
        monkeypatch.setattr("builtins.input", lambda _: "y")

        flow = flow_module.TripPlanningFlow()
        await _kickoff_and_approve(flow, _kickoff_args())
        state = flow.state

        assert state.flight_booking is None
        assert state.error is not None
        assert "Flight verification failed" in state.error

        session_factory = sqlite_session_override
        async with session_factory() as session:
            bookings = (await session.execute(select(Booking))).scalars().all()
            assert len(bookings) == 0

    async def test_fabricated_hotel_listing_is_rejected_not_proposed(
        self, monkeypatch, sqlite_session_override
    ):
        research_output = ResearchOutput(selected_hotel=_hotel_listing())
        plan_output = PlanOutput(itinerary_summary="Trip summary.")
        _patch_crews(monkeypatch, research_output, plan_output)
        monkeypatch.setattr(
            flow_module,
            "get_hotel_rate",
            lambda search_result_id, nights: ToolError(
                tool_name="get_hotel_rate",
                error_type="search_result_not_found",
                message="Duffel Stays API returned 404: not found.",
                retryable=False,
            ),
        )
        monkeypatch.setattr("builtins.input", lambda _: "y")

        flow = flow_module.TripPlanningFlow()
        await _kickoff_and_approve(flow, _kickoff_args())
        state = flow.state

        assert state.hotel_booking is None
        assert state.error is not None
        assert "Hotel verification failed" in state.error

        session_factory = sqlite_session_override
        async with session_factory() as session:
            bookings = (await session.execute(select(Booking))).scalars().all()
            assert len(bookings) == 0

    async def test_verified_flight_data_is_what_actually_gets_persisted(
        self, monkeypatch, sqlite_session_override
    ):
        """The re-fetched, verified offer is what gets proposed -- not the
        researcher's original (potentially fabricated) selection -- even
        when both happen to share the same offer_id."""
        stale_offer = _flight_offer()
        fresh_offer = _flight_offer().model_copy(update={"total_price_usd": 999.0})
        research_output = ResearchOutput(selected_flight=stale_offer)
        plan_output = PlanOutput(itinerary_summary="Trip summary.")
        _patch_crews(monkeypatch, research_output, plan_output)
        _patch_verification(monkeypatch, flight=fresh_offer)
        monkeypatch.setattr("builtins.input", lambda _: "y")

        flow = flow_module.TripPlanningFlow()
        await _kickoff_and_approve(flow, _kickoff_args())

        session_factory = sqlite_session_override
        async with session_factory() as session:
            from app.db.models import FlightOption

            option = (await session.execute(select(FlightOption))).scalar_one()
            assert option.total_price_usd == 999.0


class TestTripRequestSummaryCarRentalSignal:
    """Without an explicit signal in the trip request, the researcher has
    no reason to ever select a car rental (it's told to leave it null
    unless useful) -- wants_car_rental is that signal."""

    async def test_includes_car_rental_request_when_wanted(self):
        flow = flow_module.TripPlanningFlow()
        flow.state.origin_iata = "JFK"
        flow.state.destination_iata = "ATL"
        flow.state.depart_date = "2026-09-24"
        flow.state.wants_car_rental = True

        summary = flow._trip_request_summary()

        assert "traveler has explicitly requested a car rental" in summary

    async def test_omits_car_rental_mention_by_default(self):
        flow = flow_module.TripPlanningFlow()
        flow.state.origin_iata = "JFK"
        flow.state.destination_iata = "ATL"
        flow.state.depart_date = "2026-09-24"

        summary = flow._trip_request_summary()

        assert "car rental" not in summary
