"""
create_trip() — minimal helper for creating a Trip row.

This exists to unblock testing propose_booking(): Phase 3's agent
orchestration doesn't exist yet, so nothing else in the system currently
creates a Trip row for propose_booking() to attach a Booking to. This is
NOT meant to be the final word on trip creation — Phase 8's API layer
(POST /trips) will likely supersede or wrap this — but it's the smallest
correct piece needed right now, and it's a real, reusable building block
either way.

Deliberately NOT a deterministic "tool" in the same sense as
search_flights()/search_hotels() — it's DB-only, no external API call, so
it doesn't return a ToolError; a DB failure here is treated as a genuine
exception, since a Trip failing to persist is not something an agent
should try to gracefully route around the way it would an API rate limit.
"""

from __future__ import annotations

import uuid
from datetime import date

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Trip
from app.db.models.enums import TripStatus


async def create_trip(
    session: AsyncSession,
    *,
    origin_iata: str,
    destination_iata: str,
    depart_date: date,
    return_date: date | None = None,
    adults: int = 1,
    max_budget_usd: float | None = None,
    requester_email: str | None = None,
    wants_car_rental: bool = False,
    user_id: uuid.UUID | None = None,
) -> Trip:
    """Create and persist a new Trip row in DRAFT status.

    Caller is responsible for committing (or the caller's `get_session()`
    context manager, if used with commit-on-exit semantics) — this
    function only adds and flushes so the caller can read back trip.id
    immediately without necessarily committing yet, matching how
    multi-step flows (create trip, then search, then propose booking)
    likely want to compose.
    """
    trip = Trip(
        id=uuid.uuid4(),
        origin_iata=origin_iata.upper(),
        destination_iata=destination_iata.upper(),
        depart_date=depart_date,
        return_date=return_date,
        adults=adults,
        max_budget_usd=max_budget_usd,
        requester_email=requester_email,
        wants_car_rental=wants_car_rental,
        user_id=user_id,
        status=TripStatus.DRAFT,
    )
    session.add(trip)
    await session.flush()
    return trip
