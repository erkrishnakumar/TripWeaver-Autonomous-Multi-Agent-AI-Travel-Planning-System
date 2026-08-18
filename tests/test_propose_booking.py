"""
Unit tests for app/tools/propose_booking.py and app/tools/create_trip.py.

Unlike the other tool tests (which mock httpx or the Groq client), these
tests run against a REAL database — an in-memory async SQLite instance,
created fresh per test. This is deliberate: propose_booking()'s entire job
is correct persistence and idempotency, which can only be genuinely
verified by writing to and reading back from a real database, not by
mocking the session. SQLite in-memory is fast enough to do this per-test
with zero setup cost and zero dependency on a running Postgres instance.

Every test's most important implicit assertion is the same one:
NO REAL BOOKING ENDPOINT IS EVER CALLED. There is no code path in
propose_booking() that could call one — this is verified structurally by
the tool only ever touching the database — but tests here also confirm the
resulting Booking rows always land in PENDING_APPROVAL, never BOOKED.
"""

from datetime import date, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.base import Base
from app.db.models import Approval, AuditLog, Booking, FlightOption, HotelOption
from app.db.models.enums import ApprovalDecision, BookingStatus
from app.tools.create_trip import create_trip
from app.tools.propose_booking import propose_booking
from app.tools.schemas import (
    CabinClass,
    FlightOffer,
    FlightSegment,
    HotelListing,
    ProposeFlightBookingInput,
    ProposeHotelBookingInput,
    ToolError,
)


@pytest_asyncio.fixture
async def session():
    """Fresh in-memory SQLite database per test — no shared state, no
    cleanup needed, no dependency on a running Postgres instance."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as s:
        yield s

    await engine.dispose()


@pytest.fixture
def flight_offer() -> FlightOffer:
    return FlightOffer(
        offer_id="off_test_001",
        total_price_usd=450.00,
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


@pytest.fixture
def hotel_listing() -> HotelListing:
    return HotelListing(
        search_result_id="srr_test_001",
        hotel_name="Test Hotel",
        hotel_rating=4.0,
        latitude=13.75,
        longitude=100.5,
        estimated_price_total_usd=600.0,
        nights=3,
    )


async def _make_trip(session):
    return await create_trip(
        session,
        origin_iata="jfk",  # lowercase on purpose — tests the uppercase behavior
        destination_iata="atl",
        depart_date=date.today() + timedelta(days=30),
        adults=1,
    )


class TestCreateTrip:
    async def test_creates_trip_in_draft_status(self, session):
        trip = await _make_trip(session)
        await session.commit()

        assert trip.origin_iata == "JFK"
        assert trip.destination_iata == "ATL"
        from app.db.models.enums import TripStatus

        assert trip.status == TripStatus.DRAFT


class TestProposeBookingFlight:
    async def test_creates_pending_booking(self, session, flight_offer):
        trip = await _make_trip(session)
        await session.commit()

        query = ProposeFlightBookingInput(trip_id=str(trip.id), offer=flight_offer)
        result = await propose_booking(session, query)
        await session.commit()

        assert not isinstance(result, ToolError)
        assert result.status == "pending_approval"
        assert result.was_existing is False

    async def test_booking_row_is_pending_approval_never_booked(self, session, flight_offer):
        """The core safety property: propose_booking() must never produce a
        BOOKED status, under any circumstances."""
        trip = await _make_trip(session)
        await session.commit()

        query = ProposeFlightBookingInput(trip_id=str(trip.id), offer=flight_offer)
        await propose_booking(session, query)
        await session.commit()

        booking = (await session.execute(select(Booking))).scalar_one()
        assert booking.status == BookingStatus.PENDING_APPROVAL
        assert booking.status != BookingStatus.BOOKED
        assert booking.provider_booking_reference is None

    async def test_approval_row_created_as_pending(self, session, flight_offer):
        trip = await _make_trip(session)
        await session.commit()

        query = ProposeFlightBookingInput(trip_id=str(trip.id), offer=flight_offer)
        await propose_booking(session, query)
        await session.commit()

        approval = (await session.execute(select(Approval))).scalar_one()
        assert approval.decision == ApprovalDecision.PENDING
        assert approval.decided_by is None
        assert approval.decided_at is None

    async def test_flight_option_persisted_with_correct_data(self, session, flight_offer):
        trip = await _make_trip(session)
        await session.commit()

        query = ProposeFlightBookingInput(trip_id=str(trip.id), offer=flight_offer)
        await propose_booking(session, query)
        await session.commit()

        option = (await session.execute(select(FlightOption))).scalar_one()
        assert option.provider_offer_id == "off_test_001"
        assert option.total_price_usd == 450.00
        assert option.cabin_class == "economy"
        assert len(option.segments) == 1

    async def test_audit_log_written(self, session, flight_offer):
        trip = await _make_trip(session)
        await session.commit()

        query = ProposeFlightBookingInput(trip_id=str(trip.id), offer=flight_offer)
        await propose_booking(session, query)
        await session.commit()

        audit = (await session.execute(select(AuditLog))).scalar_one()
        assert audit.event_type == "booking.proposed"
        assert audit.payload["booking_type"] == "flight"
        assert audit.payload["provider_offer_id"] == "off_test_001"

    async def test_repeated_call_is_idempotent(self, session, flight_offer):
        """Calling propose_booking() twice with the same trip+offer must
        NOT create a second Booking or FlightOption row — it should return
        the existing pending booking instead."""
        trip = await _make_trip(session)
        await session.commit()

        query = ProposeFlightBookingInput(trip_id=str(trip.id), offer=flight_offer)
        result1 = await propose_booking(session, query)
        await session.commit()
        result2 = await propose_booking(session, query)
        await session.commit()

        assert result1.booking_id == result2.booking_id
        assert result1.was_existing is False
        assert result2.was_existing is True

        all_bookings = (await session.execute(select(Booking))).scalars().all()
        all_options = (await session.execute(select(FlightOption))).scalars().all()
        assert len(all_bookings) == 1
        assert len(all_options) == 1

    async def test_different_offers_same_trip_create_separate_bookings(self, session, flight_offer):
        """Idempotency should be scoped to (trip, offer) — a genuinely
        different offer on the same trip must create its own booking, not
        collide with an unrelated one."""
        trip = await _make_trip(session)
        await session.commit()

        query1 = ProposeFlightBookingInput(trip_id=str(trip.id), offer=flight_offer)
        await propose_booking(session, query1)
        await session.commit()

        different_offer = flight_offer.model_copy(update={"offer_id": "off_different_002"})
        query2 = ProposeFlightBookingInput(trip_id=str(trip.id), offer=different_offer)
        result2 = await propose_booking(session, query2)
        await session.commit()

        assert result2.was_existing is False
        all_bookings = (await session.execute(select(Booking))).scalars().all()
        assert len(all_bookings) == 2

    async def test_invalid_trip_id_returns_tool_error_not_exception(self, session, flight_offer):
        query = ProposeFlightBookingInput(trip_id="not-a-valid-uuid", offer=flight_offer)
        result = await propose_booking(session, query)

        assert isinstance(result, ToolError)
        assert result.tool_name == "propose_booking"
        assert result.error_type == "invalid_trip_id"
        assert result.retryable is False


class TestProposeBookingHotel:
    async def test_creates_pending_hotel_booking(self, session, hotel_listing):
        trip = await _make_trip(session)
        await session.commit()

        query = ProposeHotelBookingInput(
            trip_id=str(trip.id),
            listing=hotel_listing,
            check_in=date.today() + timedelta(days=30),
            check_out=date.today() + timedelta(days=33),
        )
        result = await propose_booking(session, query)
        await session.commit()

        assert not isinstance(result, ToolError)
        assert result.status == "pending_approval"

    async def test_hotel_option_persisted_with_correct_data(self, session, hotel_listing):
        trip = await _make_trip(session)
        await session.commit()

        check_in = date.today() + timedelta(days=30)
        check_out = date.today() + timedelta(days=33)
        query = ProposeHotelBookingInput(
            trip_id=str(trip.id), listing=hotel_listing, check_in=check_in, check_out=check_out
        )
        await propose_booking(session, query)
        await session.commit()

        option = (await session.execute(select(HotelOption))).scalar_one()
        assert option.name == "Test Hotel"
        assert option.total_price_usd == 600.0
        assert option.check_in == check_in
        assert option.check_out == check_out
        assert option.star_rating == 4.0

    async def test_hotel_booking_never_reaches_booked_status(self, session, hotel_listing):
        trip = await _make_trip(session)
        await session.commit()

        query = ProposeHotelBookingInput(
            trip_id=str(trip.id),
            listing=hotel_listing,
            check_in=date.today() + timedelta(days=30),
            check_out=date.today() + timedelta(days=33),
        )
        await propose_booking(session, query)
        await session.commit()

        booking = (await session.execute(select(Booking))).scalar_one()
        assert booking.status == BookingStatus.PENDING_APPROVAL
        assert booking.provider_booking_reference is None