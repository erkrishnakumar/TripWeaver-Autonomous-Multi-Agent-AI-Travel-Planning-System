"""
Pydantic response models for the API layer — never return a SQLAlchemy
ORM object directly from an endpoint; this is the explicit, versioned
boundary between the DB schema and what a client actually sees.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime

from pydantic import BaseModel, ConfigDict

from app.db.models.enums import TripStatus


class TripRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    origin_iata: str
    destination_iata: str
    depart_date: date
    return_date: date | None
    adults: int
    max_budget_usd: float | None
    status: TripStatus
    created_at: datetime
    updated_at: datetime


class TripCreate(BaseModel):
    origin_iata: str
    destination_iata: str
    depart_date: date
    return_date: date | None = None
    adults: int = 1
    max_budget_usd: float | None = None
    requester_email: str | None = None
    wants_car_rental: bool = False


class TripProceedResponse(BaseModel):
    trip_id: uuid.UUID
    status: str
    message: str
