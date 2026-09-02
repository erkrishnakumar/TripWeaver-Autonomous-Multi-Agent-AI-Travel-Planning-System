"""
FastAPI app — Phase 8. Starts with the smallest correct slice: a health
check and a real, read-only GET /trips/{id}, proving the DB wiring works
end to end before tackling POST /trips or the approval endpoints (both
need a real design decision first — how a long-running CrewAI Flow that
currently blocks on a synchronous CLI input() gets triggered and later
resumed over HTTP — not something to improvise here).
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db
from app.api.schemas import TripCreate, TripProceedResponse, TripRead
from app.db.models import Trip
from app.db.models.enums import TripStatus
from app.tools.create_trip import create_trip
from app.worker.tasks import propose_trip_bookings, run_trip_planning

app = FastAPI(title="TripWeaver API")


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/trips/{trip_id}", response_model=TripRead)
async def get_trip(trip_id: uuid.UUID, db: Annotated[AsyncSession, Depends(get_db)]) -> Trip:
    trip = await db.get(Trip, trip_id)
    if trip is None:
        raise HTTPException(status_code=404, detail="Trip not found")
    return trip


@app.post("/trips", response_model=TripRead, status_code=201)
async def create_trip_endpoint(
    body: TripCreate, db: Annotated[AsyncSession, Depends(get_db)]
) -> Trip:
    trip = await create_trip(
        db,
        origin_iata=body.origin_iata,
        destination_iata=body.destination_iata,
        depart_date=body.depart_date,
        return_date=body.return_date,
        adults=body.adults,
        max_budget_usd=body.max_budget_usd,
        requester_email=body.requester_email,
        wants_car_rental=body.wants_car_rental,
    )
    await db.commit()
    run_trip_planning.delay(str(trip.id))
    return trip


@app.post("/trips/{trip_id}/proceed", response_model=TripProceedResponse)
async def proceed_with_trip(
    trip_id: uuid.UUID, db: Annotated[AsyncSession, Depends(get_db)]
) -> TripProceedResponse:
    trip = await db.get(Trip, trip_id)
    if trip is None:
        raise HTTPException(status_code=404, detail="Trip not found")
    if trip.status != TripStatus.AWAITING_APPROVAL:
        raise HTTPException(
            status_code=409,
            detail=f"Trip is not awaiting approval (current status: {trip.status.value})",
        )
    propose_trip_bookings.delay(str(trip_id))
    return TripProceedResponse(
        trip_id=trip_id, status="approved", message="Bookings are being proposed."
    )
