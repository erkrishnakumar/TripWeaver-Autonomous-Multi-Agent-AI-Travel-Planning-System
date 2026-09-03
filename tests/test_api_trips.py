"""
Tests for GET /trips/{id}/bookings and GET /trips/{id}/audit-log -- both
built to close a real gap hit while live-verifying Gate 2/observability
through the actual running API (see docs/Gate2_Live_Verification.md and
docs/TripWeaver_Roadmap.md's Phase 6 notes): there was no way to see what
got proposed for a trip, or what happened to it, without a raw DB query.

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
from app.tools.schemas import FlightOffer, ProposeFlightBookingInput


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
