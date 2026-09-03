"""
Unit tests for app/tools/confirm_booking.py -- Gate 2.

Same in-memory-SQLite discipline as tests/test_propose_booking.py (real DB,
real persistence/status-transition logic). Unlike propose_booking(),
confirm_booking() calls real provider functions on a real booking type --
those (get_flight_offer/create_flight_order/get_hotel_rate/get_hotel_quote/
create_hotel_booking) are monkeypatched here at their app.tools.
confirm_booking import site, exactly the boundary this project always mocks
(the network/LLM edge), while every DB write and status transition around
them runs for real.

Every test's most important implicit assertion: a Booking only ever reaches
BOOKED when the monkeypatched provider call actually "succeeded" (returned a
non-ToolError) -- a provider failure must land on BOOKING_FAILED, never on
BOOKED and never silently swallowed as PENDING_APPROVAL.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.tools.confirm_booking as confirm_booking_module
from app.db.base import Base
from app.db.models import Approval, AuditLog, Booking
from app.db.models.enums import ApprovalDecision, BookingStatus
from app.tools.confirm_booking import confirm_booking, reject_booking
from app.tools.create_trip import create_trip
from app.tools.propose_booking import propose_booking
from app.tools.schemas import (
    CabinClass,
    CarQuoteResult,
    CarRateOption,
    CarRentalPaymentType,
    DriverDetails,
    FlightOffer,
    FlightSegment,
    HotelGuestDetails,
    HotelListing,
    HotelQuoteResult,
    PassengerDetails,
    PassengerGender,
    PassengerTitle,
    ProposeCarBookingInput,
    ProposeFlightBookingInput,
    ProposeHotelBookingInput,
    ToolError,
)

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def session():
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
        passenger_ids=["pas_001"],
    )


@pytest.fixture
def passenger() -> PassengerDetails:
    return PassengerDetails(
        passenger_id="pas_001",
        title=PassengerTitle.MR,
        gender=PassengerGender.MALE,
        given_name="Jane",
        family_name="Doe",
        date_of_birth=date(1990, 1, 1),
        email="jane.doe@example.com",
        phone_number="+442080160508",
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
        origin_iata="jfk",
        destination_iata="atl",
        depart_date=date.today() + timedelta(days=30),
        adults=1,
    )


async def _propose_flight(session, flight_offer) -> tuple[str, str]:
    trip = await _make_trip(session)
    await session.commit()
    query = ProposeFlightBookingInput(trip_id=str(trip.id), offer=flight_offer)
    result = await propose_booking(session, query)
    await session.commit()
    assert not isinstance(result, ToolError)
    return result.booking_id, result.approval_id


async def _propose_hotel(session, hotel_listing) -> tuple[str, str]:
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
    return result.booking_id, result.approval_id


async def _propose_car(session) -> tuple[str, str]:
    trip = await _make_trip(session)
    await session.commit()
    rate = CarRateOption(
        rate_id="rae_test_001",
        car_description="Compact",
        supplier_name="Test Rentals",
        payment_type=CarRentalPaymentType.PREPAID,
        estimated_price_total_usd=94.62,
        pickup_location_name="Test Pickup",
        dropoff_location_name="Test Dropoff",
        pickup_at="2026-10-01T10:00:00",
        dropoff_at="2026-10-04T10:00:00",
    )
    quote = CarQuoteResult(
        quote_id="qut_test_001",
        rate_id="rae_test_001",
        total_price_usd=94.62,
        payment_type=CarRentalPaymentType.PREPAID,
    )
    driver = DriverDetails(
        given_name="Jane",
        family_name="Doe",
        date_of_birth=date(1990, 1, 1),
        email="jane.doe@example.com",
        phone_number="+442080160508",
    )
    query = ProposeCarBookingInput(trip_id=str(trip.id), rate=rate, quote=quote, driver=driver)
    result = await propose_booking(session, query)
    await session.commit()
    assert not isinstance(result, ToolError)
    return result.booking_id, result.approval_id


class TestRejectBooking:
    async def test_rejects_pending_booking(self, session, flight_offer):
        _, approval_id = await _propose_flight(session, flight_offer)

        result = await reject_booking(session, approval_id, decided_by="ops@tripweaver.test")

        assert not isinstance(result, ToolError)
        assert result.booking_status == "rejected"

        booking = (await session.execute(select(Booking))).scalar_one()
        approval = (await session.execute(select(Approval))).scalar_one()
        assert booking.status == BookingStatus.REJECTED
        assert approval.decision == ApprovalDecision.REJECTED
        assert approval.decided_by == "ops@tripweaver.test"
        assert approval.decided_at is not None

    async def test_never_calls_a_provider(self, session, flight_offer, monkeypatch):
        called = False

        def _boom(*args, **kwargs):
            nonlocal called
            called = True
            raise AssertionError("reject_booking must never call a provider function")

        monkeypatch.setattr(confirm_booking_module, "create_flight_order", _boom)
        monkeypatch.setattr(confirm_booking_module, "get_flight_offer", _boom)

        _, approval_id = await _propose_flight(session, flight_offer)
        await reject_booking(session, approval_id)

        assert called is False

    async def test_rejecting_twice_fails_on_second_call(self, session, flight_offer):
        _, approval_id = await _propose_flight(session, flight_offer)
        await reject_booking(session, approval_id)

        result = await reject_booking(session, approval_id)

        assert isinstance(result, ToolError)
        assert result.error_type == "already_decided"

    async def test_unknown_approval_id_returns_not_found(self, session):
        import uuid

        result = await reject_booking(session, str(uuid.uuid4()))

        assert isinstance(result, ToolError)
        assert result.error_type == "approval_not_found"

    async def test_invalid_approval_id_returns_tool_error(self, session):
        result = await reject_booking(session, "not-a-uuid")

        assert isinstance(result, ToolError)
        assert result.error_type == "invalid_approval_id"


class TestConfirmBookingFlight:
    async def test_successful_confirm_books_and_stores_reference(
        self, session, flight_offer, passenger, monkeypatch
    ):
        monkeypatch.setattr(
            confirm_booking_module, "get_flight_offer", lambda offer_id: flight_offer
        )
        monkeypatch.setattr(
            confirm_booking_module,
            "create_flight_order",
            lambda offer, passengers: {"booking_reference": "ZJWVAT", "id": "ord_001"},
        )

        _, approval_id = await _propose_flight(session, flight_offer)
        result = await confirm_booking(session, approval_id, passengers=[passenger])

        assert not isinstance(result, ToolError)
        assert result.booking_status == "booked"
        assert result.provider_booking_reference == "ZJWVAT"

        booking = (await session.execute(select(Booking))).scalar_one()
        approval = (await session.execute(select(Approval))).scalar_one()
        assert booking.status == BookingStatus.BOOKED
        assert booking.provider_booking_reference == "ZJWVAT"
        assert approval.decision == ApprovalDecision.APPROVED

        audit = (await session.execute(select(AuditLog))).scalars().all()
        assert any(a.event_type == "booking.confirmed" for a in audit)

    async def test_missing_passengers_returns_tool_error_without_calling_provider(
        self, session, flight_offer, monkeypatch
    ):
        def _boom(*args, **kwargs):
            raise AssertionError("must not be called without passengers")

        monkeypatch.setattr(confirm_booking_module, "get_flight_offer", _boom)
        monkeypatch.setattr(confirm_booking_module, "create_flight_order", _boom)

        _, approval_id = await _propose_flight(session, flight_offer)
        result = await confirm_booking(session, approval_id, passengers=None)

        assert isinstance(result, ToolError)
        assert result.error_type == "missing_passengers"

        booking = (await session.execute(select(Booking))).scalar_one()
        assert booking.status == BookingStatus.PENDING_APPROVAL

    async def test_stale_offer_fails_the_booking_not_the_approval(
        self, session, flight_offer, passenger, monkeypatch
    ):
        """A real provider rejection (expired offer) must land on
        BOOKING_FAILED, not silently stay PENDING_APPROVAL and not crash."""
        monkeypatch.setattr(
            confirm_booking_module,
            "get_flight_offer",
            lambda offer_id: ToolError(
                tool_name="get_flight_offer",
                error_type="offer_not_found",
                message="Offer has expired.",
                retryable=False,
            ),
        )

        _, approval_id = await _propose_flight(session, flight_offer)
        result = await confirm_booking(session, approval_id, passengers=[passenger])

        assert not isinstance(result, ToolError)
        assert result.booking_status == "booking_failed"

        booking = (await session.execute(select(Booking))).scalar_one()
        approval = (await session.execute(select(Approval))).scalar_one()
        assert booking.status == BookingStatus.BOOKING_FAILED
        assert booking.failure_reason == "Offer has expired."
        # The human's decision stands even though the provider call failed.
        assert approval.decision == ApprovalDecision.APPROVED

    async def test_declined_order_fails_the_booking(
        self, session, flight_offer, passenger, monkeypatch
    ):
        monkeypatch.setattr(
            confirm_booking_module, "get_flight_offer", lambda offer_id: flight_offer
        )
        monkeypatch.setattr(
            confirm_booking_module,
            "create_flight_order",
            lambda offer, passengers: ToolError(
                tool_name="create_flight_order",
                error_type="duffel_api_error",
                message="Duffel API returned 422: payment declined",
                retryable=False,
            ),
        )

        _, approval_id = await _propose_flight(session, flight_offer)
        result = await confirm_booking(session, approval_id, passengers=[passenger])

        assert not isinstance(result, ToolError)
        assert result.booking_status == "booking_failed"
        booking = (await session.execute(select(Booking))).scalar_one()
        assert booking.provider_booking_reference is None

    async def test_confirming_an_already_booked_approval_fails(
        self, session, flight_offer, passenger, monkeypatch
    ):
        monkeypatch.setattr(
            confirm_booking_module, "get_flight_offer", lambda offer_id: flight_offer
        )
        monkeypatch.setattr(
            confirm_booking_module,
            "create_flight_order",
            lambda offer, passengers: {"booking_reference": "ZJWVAT"},
        )

        _, approval_id = await _propose_flight(session, flight_offer)
        await confirm_booking(session, approval_id, passengers=[passenger])

        result = await confirm_booking(session, approval_id, passengers=[passenger])

        assert isinstance(result, ToolError)
        assert result.error_type == "already_decided"


class TestConfirmBookingHotel:
    async def test_successful_confirm_books_and_stores_reference(
        self, session, hotel_listing, monkeypatch
    ):
        fresh_rate = hotel_listing.model_copy(update={"rate_id": "rat_test_001"})
        monkeypatch.setattr(
            confirm_booking_module,
            "get_hotel_rate",
            lambda search_result_id, nights: fresh_rate,
        )
        monkeypatch.setattr(
            confirm_booking_module,
            "get_hotel_quote",
            lambda rate_id: HotelQuoteResult(
                quote_id="quo_test_001", rate_id=rate_id, total_price_usd=600.0
            ),
        )
        monkeypatch.setattr(
            confirm_booking_module,
            "create_hotel_booking",
            lambda quote_id, email, phone_number, guests: {"reference": "TUI7Z5"},
        )

        _, approval_id = await _propose_hotel(session, hotel_listing)
        guests = [HotelGuestDetails(given_name="Jane", family_name="Doe")]
        result = await confirm_booking(
            session,
            approval_id,
            guests=guests,
            contact_email="jane.doe@example.com",
            contact_phone_number="+442080160508",
        )

        assert not isinstance(result, ToolError)
        assert result.booking_status == "booked"
        assert result.provider_booking_reference == "TUI7Z5"

        booking = (await session.execute(select(Booking))).scalar_one()
        assert booking.status == BookingStatus.BOOKED
        assert booking.provider_booking_reference == "TUI7Z5"

    async def test_missing_guest_details_returns_tool_error(self, session, hotel_listing):
        _, approval_id = await _propose_hotel(session, hotel_listing)

        result = await confirm_booking(session, approval_id, guests=None)

        assert isinstance(result, ToolError)
        assert result.error_type == "missing_guest_details"

    async def test_search_result_with_no_bookable_rate_fails_the_booking(
        self, session, hotel_listing, monkeypatch
    ):
        no_rate = hotel_listing.model_copy(update={"rate_id": None})
        monkeypatch.setattr(
            confirm_booking_module, "get_hotel_rate", lambda search_result_id, nights: no_rate
        )

        _, approval_id = await _propose_hotel(session, hotel_listing)
        guests = [HotelGuestDetails(given_name="Jane", family_name="Doe")]
        result = await confirm_booking(
            session,
            approval_id,
            guests=guests,
            contact_email="jane.doe@example.com",
            contact_phone_number="+442080160508",
        )

        assert not isinstance(result, ToolError)
        assert result.booking_status == "booking_failed"


class TestConfirmBookingCar:
    async def test_missing_payment_token_returns_tool_error(self, session):
        _, approval_id = await _propose_car(session)

        result = await confirm_booking(session, approval_id, three_d_secure_session_id=None)

        assert isinstance(result, ToolError)
        assert result.error_type == "missing_payment_token"

        booking = (await session.execute(select(Booking))).scalar_one()
        assert booking.status == BookingStatus.PENDING_APPROVAL

    async def test_successful_confirm_books_and_stores_reference(self, session, monkeypatch):
        monkeypatch.setattr(
            confirm_booking_module,
            "get_car_rental_quote",
            lambda query: CarQuoteResult(
                quote_id="qut_fresh_001",
                rate_id="rae_test_001",
                total_price_usd=94.62,
                payment_type=CarRentalPaymentType.PREPAID,
            ),
        )
        monkeypatch.setattr(
            confirm_booking_module,
            "create_car_rental_booking",
            lambda quote_id, driver, three_d_secure_session_id: {"reference": "CARBK123"},
        )

        _, approval_id = await _propose_car(session)
        result = await confirm_booking(
            session, approval_id, three_d_secure_session_id="tds_test_session_001"
        )

        assert not isinstance(result, ToolError)
        assert result.booking_status == "booked"
        assert result.provider_booking_reference == "CARBK123"

        booking = (await session.execute(select(Booking))).scalar_one()
        approval = (await session.execute(select(Approval))).scalar_one()
        assert booking.status == BookingStatus.BOOKED
        assert booking.provider_booking_reference == "CARBK123"
        assert approval.decision == ApprovalDecision.APPROVED

    async def test_expired_quote_fails_the_booking_not_the_approval(self, session, monkeypatch):
        monkeypatch.setattr(
            confirm_booking_module,
            "get_car_rental_quote",
            lambda query: ToolError(
                tool_name="get_car_rental_quote",
                error_type="rate_not_found",
                message="Rate has expired.",
                retryable=False,
            ),
        )

        _, approval_id = await _propose_car(session)
        result = await confirm_booking(
            session, approval_id, three_d_secure_session_id="tds_test_session_001"
        )

        assert not isinstance(result, ToolError)
        assert result.booking_status == "booking_failed"

        booking = (await session.execute(select(Booking))).scalar_one()
        approval = (await session.execute(select(Approval))).scalar_one()
        assert booking.status == BookingStatus.BOOKING_FAILED
        assert booking.failure_reason == "Rate has expired."
        assert approval.decision == ApprovalDecision.APPROVED

    async def test_declined_payment_fails_the_booking(self, session, monkeypatch):
        monkeypatch.setattr(
            confirm_booking_module,
            "get_car_rental_quote",
            lambda query: CarQuoteResult(
                quote_id="qut_fresh_001",
                rate_id="rae_test_001",
                total_price_usd=94.62,
                payment_type=CarRentalPaymentType.PREPAID,
            ),
        )
        monkeypatch.setattr(
            confirm_booking_module,
            "create_car_rental_booking",
            lambda quote_id, driver, three_d_secure_session_id: ToolError(
                tool_name="create_car_rental_booking",
                error_type="duffel_api_error",
                message="Duffel Cars API returned 422: payment declined",
                retryable=False,
            ),
        )

        _, approval_id = await _propose_car(session)
        result = await confirm_booking(
            session, approval_id, three_d_secure_session_id="tds_test_session_001"
        )

        assert not isinstance(result, ToolError)
        assert result.booking_status == "booking_failed"
        booking = (await session.execute(select(Booking))).scalar_one()
        assert booking.provider_booking_reference is None
