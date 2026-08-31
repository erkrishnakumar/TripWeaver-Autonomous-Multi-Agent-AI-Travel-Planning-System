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
from app.api.schemas import TripRead
from app.db.models import Trip

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
