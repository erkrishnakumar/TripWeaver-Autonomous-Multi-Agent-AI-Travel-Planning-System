"""
Tests for GET /trips/{id}/bookings, GET /trips/{id}/audit-log, and
GET /trips/{id}/bookings/{id}/confirm-info -- all built to close a real
gap hit while live-verifying Gate 2/observability through the actual
running API (see docs/Gate2_Live_Verification.md and
docs/TripWeaver_Roadmap.md's Phase 6/open-items notes): there was no way
to see what got proposed for a trip, what happened to it, or what real
provider ids a booking needs to be confirmed, without a raw DB query
and/or a one-off script.

Same real-ASGI-request discipline as tests/test_api_auth.py: a real app,
a real (temporary) database, dependency-injected through httpx.AsyncClient
+ ASGITransport rather than calling the endpoint functions directly.
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.deps import get_db
from app.api.main import app
from app.config import settings
from app.db.base import Base
from app.tools.audit import log_stage_event
from app.tools.create_trip import create_trip
from app.tools.propose_booking import propose_booking
from app.tools.schemas import (
    CarQuoteResult,
    CarRateOption,
    CarRentalPaymentType,
    DriverDetails,
    FlightOffer,
    HotelListing,
    ProposeCarBookingInput,
    ProposeFlightBookingInput,
    ProposeHotelBookingInput,
    ToolError,
)


@pytest.fixture(autouse=True)
def _jwt_secret(monkeypatch):
    monkeypatch.setattr(settings, "jwt_secret_key", "test-secret-key-not-for-real-use")


@pytest_asyncio.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as s:
        yield s

    await engine.dispose()


@pytest_asyncio.fixture
async def client(session):
    async def _override_get_db():
        yield session

    app.dependency_overrides[get_db] = _override_get_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


async def _register_and_login(client: AsyncClient, email: str) -> tuple[str, uuid.UUID]:
    password = "correcthorse"
    resp = await client.post("/auth/register", json={"email": email, "password": password})
    assert resp.status_code == 201, resp.text
    user_id = uuid.UUID(resp.json()["id"])
    resp = await client.post("/auth/login", json={"email": email, "password": password})
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"], user_id


class TestListTripBookings:
    async def test_lists_a_proposed_booking_with_its_approval(self, client, session):
        token, user_id = await _register_and_login(client, "jane@example.com")
        trip = await create_trip(
            session,
            origin_iata="JFK",
            destination_iata="ATL",
            depart_date=date.today() + timedelta(days=30),
            user_id=user_id,
        )
        offer = FlightOffer(
            offer_id="off_test_001",
            total_price_usd=450.0,
            cabin_class="economy",
            stops_outbound=0,
            segments=[],
        )
        result = await propose_booking(
            session, ProposeFlightBookingInput(trip_id=str(trip.id), offer=offer)
        )
        await session.commit()

        resp = await client.get(
            f"/trips/{trip.id}/bookings", headers={"Authorization": f"Bearer {token}"}
        )
        assert resp.status_code == 200
        body = resp.json()
        assert len(body) == 1
        assert body[0]["booking_id"] == result.booking_id
        assert body[0]["approval_id"] == result.approval_id
        assert body[0]["status"] == "pending_approval"
        assert body[0]["booking_type"] == "flight"

    async def test_empty_list_for_a_trip_with_no_proposals(self, client, session):
        token, user_id = await _register_and_login(client, "jane@example.com")
        trip = await create_trip(
            session,
            origin_iata="JFK",
            destination_iata="ATL",
            depart_date=date.today() + timedelta(days=30),
            user_id=user_id,
        )
        await session.commit()

        resp = await client.get(
            f"/trips/{trip.id}/bookings", headers={"Authorization": f"Bearer {token}"}
        )
        assert resp.status_code == 200
        assert resp.json() == []

    async def test_another_users_bookings_are_not_visible(self, client, session):
        _, owner_id = await _register_and_login(client, "owner@example.com")
        trip = await create_trip(
            session,
            origin_iata="JFK",
            destination_iata="ATL",
            depart_date=date.today() + timedelta(days=30),
            user_id=owner_id,
        )
        await session.commit()

        other_token, _ = await _register_and_login(client, "someone-else@example.com")
        resp = await client.get(
            f"/trips/{trip.id}/bookings", headers={"Authorization": f"Bearer {other_token}"}
        )
        assert resp.status_code == 404


class TestGetTripAuditLog:
    async def test_returns_events_in_sequence_order(self, client, session):
        token, user_id = await _register_and_login(client, "jane@example.com")
        trip = await create_trip(
            session,
            origin_iata="JFK",
            destination_iata="ATL",
            depart_date=date.today() + timedelta(days=30),
            user_id=user_id,
        )
        await session.commit()

        await log_stage_event(session, str(trip.id), "research_started", payload={})
        await log_stage_event(session, str(trip.id), "research_completed", payload={"found": True})
        await session.commit()

        resp = await client.get(
            f"/trips/{trip.id}/audit-log", headers={"Authorization": f"Bearer {token}"}
        )
        assert resp.status_code == 200
        body = resp.json()
        assert [e["event_type"] for e in body] == ["research_started", "research_completed"]
        assert body[1]["payload"] == {"found": True}
        assert body[0]["sequence"] < body[1]["sequence"]

    async def test_empty_for_a_trip_with_no_events(self, client, session):
        token, user_id = await _register_and_login(client, "jane@example.com")
        trip = await create_trip(
            session,
            origin_iata="JFK",
            destination_iata="ATL",
            depart_date=date.today() + timedelta(days=30),
            user_id=user_id,
        )
        await session.commit()

        resp = await client.get(
            f"/trips/{trip.id}/audit-log", headers={"Authorization": f"Bearer {token}"}
        )
        assert resp.status_code == 200
        assert resp.json() == []

    async def test_another_users_audit_log_is_not_visible(self, client, session):
        _, owner_id = await _register_and_login(client, "owner@example.com")
        trip = await create_trip(
            session,
            origin_iata="JFK",
            destination_iata="ATL",
            depart_date=date.today() + timedelta(days=30),
            user_id=owner_id,
        )
        await log_stage_event(session, str(trip.id), "research_started", payload={})
        await session.commit()

        other_token, _ = await _register_and_login(client, "someone-else@example.com")
        resp = await client.get(
            f"/trips/{trip.id}/audit-log", headers={"Authorization": f"Bearer {other_token}"}
        )
        assert resp.status_code == 404

    async def test_unknown_trip_returns_404(self, client):
        token, _ = await _register_and_login(client, "jane@example.com")
        resp = await client.get(
            f"/trips/{uuid.uuid4()}/audit-log", headers={"Authorization": f"Bearer {token}"}
        )
        assert resp.status_code == 404


class TestGetBookingConfirmInfo:
    async def test_flight_booking_returns_live_passenger_ids(self, client, session, monkeypatch):
        import app.api.main as main_module

        token, user_id = await _register_and_login(client, "jane@example.com")
        trip = await create_trip(
            session,
            origin_iata="JFK",
            destination_iata="ATL",
            depart_date=date.today() + timedelta(days=30),
            user_id=user_id,
        )
        offer = FlightOffer(
            offer_id="off_test_001",
            total_price_usd=450.0,
            cabin_class="economy",
            stops_outbound=0,
            segments=[],
            passenger_ids=["pas_stale_cached_id"],
        )
        result = await propose_booking(
            session, ProposeFlightBookingInput(trip_id=str(trip.id), offer=offer)
        )
        await session.commit()

        fresh_offer = offer.model_copy(update={"passenger_ids": ["pas_fresh_from_duffel"]})
        monkeypatch.setattr(main_module, "get_flight_offer", lambda offer_id: fresh_offer)

        resp = await client.get(
            f"/trips/{trip.id}/bookings/{result.booking_id}/confirm-info",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["booking_type"] == "flight"
        assert body["approval_id"] == result.approval_id
        # Live re-fetched value, not the one cached at propose time -- the
        # whole point of this endpoint.
        assert body["passenger_ids"] == ["pas_fresh_from_duffel"]

    async def test_stale_flight_offer_returns_422(self, client, session, monkeypatch):
        import app.api.main as main_module

        token, user_id = await _register_and_login(client, "jane@example.com")
        trip = await create_trip(
            session,
            origin_iata="JFK",
            destination_iata="ATL",
            depart_date=date.today() + timedelta(days=30),
            user_id=user_id,
        )
        offer = FlightOffer(
            offer_id="off_test_001",
            total_price_usd=450.0,
            cabin_class="economy",
            stops_outbound=0,
            segments=[],
        )
        result = await propose_booking(
            session, ProposeFlightBookingInput(trip_id=str(trip.id), offer=offer)
        )
        await session.commit()

        monkeypatch.setattr(
            main_module,
            "get_flight_offer",
            lambda offer_id: ToolError(
                tool_name="get_flight_offer",
                error_type="offer_not_found",
                message="Offer has expired.",
                retryable=False,
            ),
        )

        resp = await client.get(
            f"/trips/{trip.id}/bookings/{result.booking_id}/confirm-info",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 422

    async def test_hotel_booking_needs_no_passenger_ids(self, client, session):
        token, user_id = await _register_and_login(client, "jane@example.com")
        trip = await create_trip(
            session,
            origin_iata="JFK",
            destination_iata="ATL",
            depart_date=date.today() + timedelta(days=30),
            user_id=user_id,
        )
        listing = HotelListing(
            search_result_id="srr_test_001",
            hotel_name="Test Hotel",
            latitude=13.75,
            longitude=100.5,
            estimated_price_total_usd=600.0,
            nights=3,
        )
        check_in = date.today() + timedelta(days=30)
        check_out = date.today() + timedelta(days=33)
        result = await propose_booking(
            session,
            ProposeHotelBookingInput(
                trip_id=str(trip.id), listing=listing, check_in=check_in, check_out=check_out
            ),
        )
        await session.commit()

        resp = await client.get(
            f"/trips/{trip.id}/bookings/{result.booking_id}/confirm-info",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["booking_type"] == "hotel"
        assert body["passenger_ids"] is None
        assert "guests" in body["note"]

    async def test_car_booking_returns_fresh_quote_id(self, client, session, monkeypatch):
        import app.api.main as main_module

        token, user_id = await _register_and_login(client, "jane@example.com")
        trip = await create_trip(
            session,
            origin_iata="JFK",
            destination_iata="ATL",
            depart_date=date.today() + timedelta(days=30),
            user_id=user_id,
        )
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
        result = await propose_booking(
            session,
            ProposeCarBookingInput(trip_id=str(trip.id), rate=rate, quote=quote, driver=driver),
        )
        await session.commit()

        monkeypatch.setattr(
            main_module,
            "get_car_rental_quote",
            lambda query: CarQuoteResult(
                quote_id="qut_fresh_002",
                rate_id="rae_test_001",
                total_price_usd=94.62,
                payment_type=CarRentalPaymentType.PREPAID,
            ),
        )

        resp = await client.get(
            f"/trips/{trip.id}/bookings/{result.booking_id}/confirm-info",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["booking_type"] == "car"
        # Live re-fetched value, not the one stored at propose time.
        assert body["car_quote_id"] == "qut_fresh_002"

    async def test_unknown_booking_returns_404(self, client):
        token, _ = await _register_and_login(client, "jane@example.com")
        resp = await client.get(
            f"/trips/{uuid.uuid4()}/bookings/{uuid.uuid4()}/confirm-info",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 404

    async def test_another_users_booking_confirm_info_is_not_visible(self, client, session):
        _, owner_id = await _register_and_login(client, "owner@example.com")
        trip = await create_trip(
            session,
            origin_iata="JFK",
            destination_iata="ATL",
            depart_date=date.today() + timedelta(days=30),
            user_id=owner_id,
        )
        offer = FlightOffer(
            offer_id="off_test_001",
            total_price_usd=450.0,
            cabin_class="economy",
            stops_outbound=0,
            segments=[],
        )
        result = await propose_booking(
            session, ProposeFlightBookingInput(trip_id=str(trip.id), offer=offer)
        )
        await session.commit()

        other_token, _ = await _register_and_login(client, "someone-else@example.com")
        resp = await client.get(
            f"/trips/{trip.id}/bookings/{result.booking_id}/confirm-info",
            headers={"Authorization": f"Bearer {other_token}"},
        )
        assert resp.status_code == 404
