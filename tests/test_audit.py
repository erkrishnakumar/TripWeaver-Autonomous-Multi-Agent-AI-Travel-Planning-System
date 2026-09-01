"""
Tests for app/agents/audit.py -- the AuditLog helpers that will back
TripPlanningFlow's resumable stages (research, plan).
"""

from __future__ import annotations

from datetime import date

import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.base import Base
from app.db.models import Trip
from app.db.models.enums import TripStatus
from app.tools.audit import get_last_completed_payload, log_stage_event


@pytest_asyncio.fixture
async def session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


async def _make_trip(session) -> str:
    trip = Trip(
        origin_iata="JFK",
        destination_iata="ATL",
        depart_date=date(2026, 9, 27),
        adults=1,
        status=TripStatus.DRAFT,
    )
    session.add(trip)
    await session.flush()
    return str(trip.id)


class TestLogStageEvent:
    async def test_writes_a_retrievable_audit_log_row(self, session_factory):
        async with session_factory() as session:
            trip_id = await _make_trip(session)
            await log_stage_event(session, trip_id, "research_started")
            await session.commit()

        async with session_factory() as session:
            payload = await get_last_completed_payload(session, trip_id, "research")
            assert payload is None  # "_started", not "_completed" -- must not match


class TestGetLastCompletedPayload:
    async def test_returns_none_when_stage_never_completed(self, session_factory):
        async with session_factory() as session:
            trip_id = await _make_trip(session)
            payload = await get_last_completed_payload(session, trip_id, "research")
            assert payload is None

    async def test_returns_the_payload_once_completed(self, session_factory):
        async with session_factory() as session:
            trip_id = await _make_trip(session)
            await log_stage_event(
                session, trip_id, "research_completed", payload={"offer_id": "off_123"}
            )
            await session.commit()

        async with session_factory() as session:
            payload = await get_last_completed_payload(session, trip_id, "research")
            assert payload == {"offer_id": "off_123"}

    async def test_returns_the_most_recent_entry_if_logged_twice(self, session_factory):
        """Shouldn't normally happen (a stage completes once), but if a
        retry somehow re-logs completion, the LATEST payload should win,
        not the first."""
        async with session_factory() as session:
            trip_id = await _make_trip(session)
            await log_stage_event(session, trip_id, "research_completed", payload={"v": 1})
            await log_stage_event(session, trip_id, "research_completed", payload={"v": 2})
            await session.commit()

        async with session_factory() as session:
            payload = await get_last_completed_payload(session, trip_id, "research")
            assert payload == {"v": 2}

    async def test_different_stages_are_independent(self, session_factory):
        async with session_factory() as session:
            trip_id = await _make_trip(session)
            await log_stage_event(session, trip_id, "research_completed", payload={"a": 1})
            await log_stage_event(session, trip_id, "plan_completed", payload={"b": 2})
            await session.commit()

        async with session_factory() as session:
            research_payload = await get_last_completed_payload(session, trip_id, "research")
            plan_payload = await get_last_completed_payload(session, trip_id, "plan")
            assert research_payload == {"a": 1}
            assert plan_payload == {"b": 2}
